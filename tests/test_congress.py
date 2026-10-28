from datetime import date, datetime, timedelta, timezone

from bot.models import Disclosure, Position
from bot.notify import MemoryNotifier
from bot.sources.capitol_trades import parse_date, parse_trades_html
from bot.sources.quiver import parse_rows
from bot.strategies.congress import rank, size_lower_bound, size_weight
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


