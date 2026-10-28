from datetime import date, datetime, timedelta, timezone

from bot.models import Disclosure, Position
from bot.notify import MemoryNotifier
from bot.sources.capitol_trades import parse_date, parse_trades_html
from bot.sources.quiver import parse_rows
from bot.strategies.congress import CongressStrategy, rank, size_lower_bound, size_weight
from bot.strategies.politicians import PriceHistory, trade_excess_return, track_records

from .fakes import FakeBroker

NOW = datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc)
TODAY = NOW.date()


class ListSource:
    def __init__(self, items):
        self.items = items

    def fetch(self, today=None):
        return list(self.items)


_n = 0


def d(who, ticker, side, size="1K–15K", traded_ago=10, published_ago=2):
    global _n
    _n += 1
    return Disclosure(f"t:{_n}", who, ticker, side, TODAY - timedelta(days=traded_ago),
                      TODAY - timedelta(days=published_ago), size)


def crowd(ticker, buyers, side="buy", **kw):
    return [d(f"Pol {ticker} {i}", ticker, side, **kw) for i in range(buyers)]


def make(broker, items, **cfg):
    cfg = {"buy_stocks": True, "use_track_records": False, **cfg}
    return CongressStrategy(broker, cfg, MemoryNotifier(), ListSource(items))


def prices(*tickers, price=100.0):
    return {t: price for t in tickers}


# ---- sizes & ranking -------------------------------------------------------
def test_size_parsing():
    assert size_lower_bound("15K–50K") == 15_000
    assert size_lower_bound("$1,001 - $15,000") == 1001
    assert size_lower_bound("1M–5M") == 1_000_000
    assert size_lower_bound("< 1K") == 0
    assert size_weight("1K–15K") == 1.0 and size_weight("1M–5M") == 4.0
    assert 2.1 < size_weight("15K–50K") < 2.2


def test_rank_counts_distinct_politicians_and_subtracts_sellers():
    items = (
        crowd("NVDA", 4)
        + [d("Same Person", "AAPL", "buy") for _ in range(5)]  # one person 5 times = 1 buyer
        + crowd("MSFT", 3) + [d("Seller A", "MSFT", "sell"), d("Seller B", "MSFT", "sell")]
    )
    picks = {p.ticker: p for p in rank(items, TODAY, 30, 90)}
    assert len(picks["NVDA"].buyers) == 4 and picks["NVDA"].score == 4
    assert len(picks["AAPL"].buyers) == 1 and picks["AAPL"].score == 1
    assert picks["MSFT"].score == 1 and picks["MSFT"].sellers == ["Seller A", "Seller B"]
    assert [p.ticker for p in rank(items, TODAY, 30, 90)][0] == "NVDA"


def test_big_trades_count_more():
    items = crowd("SMALL", 2, size="1K–15K") + crowd("BIG", 2, size="1M–5M")
    assert [p.ticker for p in rank(items, TODAY, 30, 90)] == ["BIG", "SMALL"]


def test_rank_ignores_old_and_excluded():
    items = crowd("OLD", 5, published_ago=40) + crowd("STALE", 5, traded_ago=120) + crowd("GME", 5)
    assert rank(items, TODAY, 30, 90, exclude={"gme"}) == []


# ---- rebalancing -----------------------------------------------------------
def test_buys_top_n_with_budget_split_equally():
    items = crowd("AAA", 5) + crowd("BBB", 4) + crowd("CCC", 3) + crowd("ONE", 1)
    b = FakeBroker(prices=prices("AAA", "BBB", "CCC", "ONE"), equity=100_000)
    picks = make(b, items, top_n=2, budget_pct=0.30).run(NOW)
    assert [p.ticker for p in picks] == ["AAA", "BBB", "CCC"]  # ONE has a single buyer
    assert [(o["symbol"], o["side"], o["notional"]) for o in b.submitted] == [
        ("AAA", "buy", 15_000), ("BBB", "buy", 15_000)]
    assert b.picks == ["AAA", "BBB", "CCC"]


def test_no_duplicate_buys_on_same_day():
    b = FakeBroker(prices=prices("AAA"))
    s = make(b, crowd("AAA", 3))
    s.run(NOW)
    s.run(NOW, force=True)
    assert len(b.submitted) == 1


def test_keeps_holdings_within_hold_rank_and_sells_dropouts():
    items = crowd("AAA", 6) + crowd("BBB", 5) + crowd("CCC", 4) + crowd("SOLD", 3, side="sell")
    b = FakeBroker(prices=prices("AAA", "BBB", "CCC", "SOLD", "MINE"))
    b.holdings = {"CCC": 10, "SOLD": 7}
    b.pos += [Position("CCC", 10, 90, 100, False), Position("SOLD", 7, 90, 100, False),
              Position("MINE", 50, 90, 100, False)]  # MINE was bought by hand: never touched
    make(b, items, top_n=2, hold_rank=3).run(NOW)
    orders = [(o["symbol"], o["side"]) for o in b.submitted]
    assert ("SOLD", "sell") in orders            # politicians now selling it
    assert ("CCC", "sell") not in orders         # rank 3 <= hold_rank, kept
    assert ("AAA", "buy") in orders and ("BBB", "buy") not in orders  # CCC + AAA = top_n 2
    assert not any(sym == "MINE" for sym, _ in orders)


def test_only_sells_its_own_shares():
    b = FakeBroker(prices=prices("AAA", "XYZ"))
    b.holdings = {"XYZ": 5}
    b.pos.append(Position("XYZ", 105, 10, 10, False))  # 100 of these are wheel shares
    make(b, crowd("AAA", 3)).run(NOW)
    assert ("XYZ", "sell", 5) in [(o["symbol"], o["side"], o.get("qty")) for o in b.submitted]


def test_skips_untradable_and_penny_stocks():
    b = FakeBroker(prices={"OTC": 50.0, "PENNY": 2.0, "GOOD": 50.0})
    b.assets["OTC"] = {"tradable": False, "fractionable": False}
    picks = make(b, crowd("OTC", 5) + crowd("PENNY", 5) + crowd("GOOD", 2)).run(NOW)
    assert [p.ticker for p in picks] == ["GOOD"]


def test_whole_shares_when_not_fractionable():
    b = FakeBroker(prices={"XYZ": 120.0}, equity=10_000)
    b.assets["XYZ"] = {"tradable": True, "fractionable": False}
    make(b, crowd("XYZ", 2), top_n=1, budget_pct=0.5).run(NOW)
    assert b.submitted[0]["qty"] == 41  # $5,000 / $120


def test_refreshes_once_a_day():
    b = FakeBroker()
    s = make(b, [])
    assert s.due(NOW.replace(hour=14, minute=37))     # never saved -> run now
    b.picks_date = TODAY - timedelta(days=1)
    assert s.due(NOW.replace(hour=15, minute=2))      # stale -> first top-of-hour run
    assert not s.due(NOW.replace(hour=15, minute=7))  # failed? retry next hour, not every 5 min
    b.picks_date = TODAY
    assert not s.due(NOW.replace(hour=16, minute=0))  # already done today


def test_sends_daily_summary():
    b = FakeBroker(prices=prices("AAA"))
    s = make(b, crowd("AAA", 3))
    s.run(NOW)
    assert "Congress picks" in s.notify.messages[0] and "BUY" in s.notify.messages[0]


def test_picker_only_by_default_places_no_stock_orders():
    b = FakeBroker(prices=prices("AAA", "BBB"))
    CongressStrategy(b, {"use_track_records": False}, MemoryNotifier(),
                     ListSource(crowd("AAA", 3) + crowd("BBB", 2))).run(NOW)
    assert b.submitted == [] and b.picks == ["AAA", "BBB"]


# ---- track records ---------------------------------------------------------
def closes_from(start: date, prices_by_day: list[float]):
    return [(start + timedelta(days=i), p) for i, p in enumerate(prices_by_day)]


def test_track_record_measures_return_vs_spy_from_disclosure():
    start = TODAY - timedelta(days=200)
    flat = closes_from(start, [100.0] * 201)
    rising = closes_from(start, [100.0 + i * 0.5 for i in range(201)])   # +$45 over 90 days (30-39%)
    falling = closes_from(start, [100.0 - i * 0.2 for i in range(201)])  # -18% over 90 days
    prices_ = PriceHistory({"SPY": flat, "WIN": rising, "LOSE": falling})
    trades = []
    for k in range(8):  # 8 judged buys each, published 100..170 days ago
        pub = 100 + k * 10
        trades.append(d("Good Trader", "WIN", "buy", published_ago=pub, traded_ago=pub + 5))
        trades.append(d("Bad Trader", "LOSE", "buy", published_ago=pub, traded_ago=pub + 5))
    trades.append(d("New Person", "WIN", "buy", published_ago=60, traded_ago=65))
    recs = track_records(trades, prices_, TODAY)
    good, bad, new = recs["Good Trader"], recs["Bad Trader"], recs["New Person"]
    assert good.scored_trades == 8 and 0.25 < good.avg_excess < 0.45 and good.hit_rate == 1.0
    assert bad.avg_excess < -0.1 and bad.hit_rate == 0.0
    assert good.weight > 1.5 and bad.weight < 0.6
    assert new.weight == 1.0  # one trade isn't enough to judge


def test_recent_buys_too_new_to_judge_are_skipped():
    start = TODAY - timedelta(days=60)
    prices_ = PriceHistory({"SPY": closes_from(start, [100.0] * 61), "X": closes_from(start, [100.0] * 61)})
    assert trade_excess_return(prices_, "X", TODAY - timedelta(days=10), TODAY, 90, 30) is None


def test_good_traders_outrank_bigger_crowd_of_bad_ones():
    items = ([d(f"Good {i}", "GOOD", "buy") for i in range(2)]
             + [d(f"Bad {i}", "BAD", "buy") for i in range(3)])
    weights = {"Good 0": 2.0, "Good 1": 2.0, "Bad 0": 0.5, "Bad 1": 0.5, "Bad 2": 0.5}
    assert [p.ticker for p in rank(items, TODAY, 30, 90, weights=weights)] == ["GOOD", "BAD"]
    assert [p.ticker for p in rank(items, TODAY, 30, 90)] == ["BAD", "GOOD"]


def test_strategy_uses_track_records_from_price_history():
    start = TODAY - timedelta(days=200)
    b = FakeBroker(prices=prices("GOOD", "BAD", "WIN"))
    b.closes = {"SPY": closes_from(start, [100.0] * 201),
                "WIN": closes_from(start, [100.0 + i for i in range(201)])}
    history = [d(f"Good {i}", "WIN", "buy", published_ago=100 + k * 10, traded_ago=110 + k * 10)
               for i in range(2) for k in range(6)]
    recent = ([d(f"Good {i}", "GOOD", "buy") for i in range(2)]
              + [d(f"Bad {i}", "BAD", "buy") for i in range(3)])
    s = CongressStrategy(b, {"history_days": 365}, MemoryNotifier(), ListSource(history + recent))
    picks = s.run(NOW)
    assert [p.ticker for p in picks][:2] == ["GOOD", "BAD"]
    assert "Top traders" in s.notify.messages[0]


# ---- data sources ----------------------------------------------------------
def test_parse_dates():
    assert parse_date("23 Oct2025", TODAY) == date(2025, 10, 23)
    assert parse_date("Yesterday 13:05", TODAY) == TODAY - timedelta(days=1)
    assert parse_date("", TODAY) is None


CAPITOL_HTML = """
<table><tbody>
<tr>
  <td><h2 class="politician-name"><a href="/politicians/M001157">Michael McCaul</a></h2></td>
  <td><h3 class="issuer-name"><a href="/issuers/1">NVIDIA Corp</a></h3><span class="issuer-ticker">NVDA:US</span></td>
  <td>Today 13:05</td><td>14 Oct2026</td><td>13 days</td><td>Spouse</td>
  <td><span class="tx-type">buy</span></td><td><span class="trade-size">15K–50K</span></td>
  <td><a href="/trades/20003801566">Goto trade detail</a></td>
</tr>
<tr>
  <td><h2 class="politician-name"><a>Nancy Pelosi</a></h2></td>
  <td><span class="issuer-ticker">N/A</span></td>
  <td>20 Oct2026</td><td>1 Oct2026</td><td>19 days</td><td>Self</td>
  <td><span class="tx-type">buy</span></td><td><span class="trade-size">1K–15K</span></td><td></td>
</tr>
<tr>
  <td><h2 class="politician-name"><a>Ro Khanna</a></h2></td>
  <td><span class="issuer-ticker">MSFT:US</span></td>
  <td>20 Oct2026</td><td>1 Oct2026</td><td>19 days</td><td>Self</td>
  <td><span class="tx-type">sell</span></td><td><span class="trade-size">1K–15K</span></td><td></td>
</tr>
</tbody></table>
"""


def test_parse_capitol_trades_html():
    rows = parse_trades_html(CAPITOL_HTML, TODAY)
    assert [(r.politician, r.ticker, r.side) for r in rows] == [
        ("Michael McCaul", "NVDA", "buy"), ("Ro Khanna", "MSFT", "sell")]
    assert rows[0].source_id == "capitoltrades:20003801566"
    assert rows[0].published_date == TODAY and rows[0].traded_date == date(2026, 10, 14)


def test_parse_quiver_rows_all_politicians():
    rows = parse_rows(
        [{"Representative": "A", "Ticker": "AAPL", "Transaction": "Purchase", "ReportDate": "2026-10-20"},
         {"Representative": "B", "Ticker": "MSFT", "Transaction": "Sale (Partial)", "ReportDate": "2026-10-20"}],
        [],
    )
    assert [(r.politician, r.ticker, r.side) for r in rows] == [("A", "AAPL", "buy"), ("B", "MSFT", "sell")]


def test_end_to_end_picker_feeds_the_wheel():
    from bot.strategies.wheel import WheelStrategy

    b = FakeBroker(prices={"PRICEY": 900.0, "AAA": 30.0, "BBB": 25.0}, equity=100_000, cash=100_000)
    b.add_chain("AAA", "put", TODAY, 30, [(28, -0.3, 0.60)])
    b.add_chain("BBB", "put", TODAY, 30, [(23, -0.3, 0.50)])
    items = crowd("PRICEY", 6) + crowd("AAA", 4) + crowd("BBB", 3)
    CongressStrategy(b, {"use_track_records": False}, MemoryNotifier(), ListSource(items)).run(NOW)
    WheelStrategy(b, {}, MemoryNotifier()).run(NOW)
    # PRICEY ranks first but a put needs ~$90k, more than its $10k slice
    assert b.picks == ["PRICEY", "AAA", "BBB"]
    assert [(o["symbol"][:3], o["intent"]) for o in b.submitted] == [
        ("AAA", "sell_to_open"), ("BBB", "sell_to_open")]
