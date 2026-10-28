from datetime import date, datetime, timedelta, timezone

from bot.models import Disclosure, Position
from bot.notify import MemoryNotifier
from bot.sources.capitol_trades import parse_date, parse_trades_html
from bot.sources.quiver import parse_rows
from bot.strategies.congress import CongressStrategy, rank, size_lower_bound, size_weight

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


def test_runs_once_a_day_unless_no_picks_saved():
    b = FakeBroker()
    s = make(b, [])
    assert s.due(NOW.replace(hour=10))           # nothing saved yet -> run now
    b.picks = ["AAA"]
    assert s.due(NOW.replace(hour=15, minute=3))
    assert not s.due(NOW.replace(hour=15, minute=5))
    assert not s.due(NOW.replace(hour=18))


def test_sends_daily_summary():
    b = FakeBroker(prices=prices("AAA"))
    s = make(b, crowd("AAA", 3))
    s.run(NOW)
    assert "Congress picks" in s.notify.messages[0] and "BUY" in s.notify.messages[0]


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
