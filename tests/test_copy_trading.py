from datetime import date, datetime, timedelta, timezone

from bot.models import Disclosure, Position
from bot.notify import MemoryNotifier
from bot.sources.capitol_trades import parse_date, parse_trades_html
from bot.sources.quiver import parse_rows
from bot.strategies.copy_trading import CopyTradingStrategy

from .fakes import FakeBroker

NOW = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)
TODAY = NOW.date()


class ListSource:
    def __init__(self, items):
        self.items = items

    def fetch(self, today=None):
        return list(self.items)


def d(id_, ticker, side, traded_ago=10, published_ago=1):
    return Disclosure(
        source_id=f"test:{id_}", politician="Michael McCaul", ticker=ticker, side=side,
        traded_date=TODAY - timedelta(days=traded_ago),
        published_date=TODAY - timedelta(days=published_ago), size="15K–50K",
    )


def make(broker, items, protected=None, **cfg):
    return CopyTradingStrategy(broker, cfg, MemoryNotifier(), ListSource(items), protected)


def test_buys_new_disclosure_once():
    b = FakeBroker(prices={"NVDA": 100.0})
    s = make(b, [d(1, "NVDA", "buy")])
    s.run(NOW, force=True)
    s.run(NOW, force=True)  # second run must not duplicate
    assert len(b.submitted) == 1
    assert b.submitted[0]["notional"] == 500 and b.submitted[0]["side"] == "buy"


def test_ignores_old_disclosures():
    b = FakeBroker()
    make(b, [d(1, "NVDA", "buy", published_ago=30), d(2, "AAPL", "buy", traded_ago=90, published_ago=2)]).run(NOW, force=True)
    assert b.submitted == []


def test_whole_shares_when_not_fractionable():
    b = FakeBroker(prices={"XYZ": 120.0})
    b.assets["XYZ"] = {"tradable": True, "fractionable": False}
    make(b, [d(1, "XYZ", "buy")]).run(NOW, force=True)
    assert b.submitted[0]["qty"] == 4 and b.submitted[0]["notional"] is None


def test_mirror_sell_closes_position_but_not_wheel_symbols():
    b = FakeBroker()
    b.pos += [Position("NVDA", 3.5, 100, 110, False), Position("SOFI", 100, 13, 14, False)]
    make(b, [d(1, "NVDA", "sell"), d(2, "SOFI", "sell")], protected={"SOFI"}).run(NOW, force=True)
    assert [(o["symbol"], o["side"], o["qty"]) for o in b.submitted] == [("NVDA", "sell", 3.5)]


def test_max_open_positions():
    b = FakeBroker()
    b.pos += [Position("AAA", 1, 1, 1, False)]
    make(b, [d(1, "NVDA", "buy")], max_open_positions=1).run(NOW, force=True)
    assert b.submitted == []


def test_only_due_every_n_minutes():
    s = make(FakeBroker(), [], check_every_minutes=30)
    assert s.due(NOW.replace(minute=0)) and s.due(NOW.replace(minute=32))
    assert not s.due(NOW.replace(minute=15))


def test_parse_dates():
    assert parse_date("23 Oct2025", TODAY) == date(2025, 10, 23)
    assert parse_date("23 Oct 2025", TODAY) == date(2025, 10, 23)
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
  <td><h2 class="politician-name"><a>Michael McCaul</a></h2></td>
  <td><span class="issuer-ticker">N/A</span></td>
  <td>20 Oct2026</td><td>1 Oct2026</td><td>19 days</td><td>Self</td>
  <td><span class="tx-type">buy</span></td><td><span class="trade-size">1K–15K</span></td><td></td>
</tr>
<tr>
  <td><h2 class="politician-name"><a>Michael McCaul</a></h2></td>
  <td><span class="issuer-ticker">MSFT:US</span></td>
  <td>20 Oct2026</td><td>1 Oct2026</td><td>19 days</td><td>Self</td>
  <td><span class="tx-type">sell</span></td><td><span class="trade-size">1K–15K</span></td><td></td>
</tr>
</tbody></table>
"""


def test_parse_capitol_trades_html():
    rows = parse_trades_html(CAPITOL_HTML, TODAY)
    assert [(r.ticker, r.side) for r in rows] == [("NVDA", "buy"), ("MSFT", "sell")]
    assert rows[0].source_id == "capitoltrades:20003801566"
    assert rows[0].published_date == TODAY and rows[0].traded_date == date(2026, 10, 14)
    assert rows[1].source_id.startswith("capitoltrades:")


def test_parse_quiver_rows():
    rows = parse_rows(
        [
            {"Representative": "Michael T. McCaul", "Ticker": "AAPL", "Transaction": "Purchase",
             "TransactionDate": "2026-10-01", "ReportDate": "2026-10-20", "Range": "$1,001 - $15,000"},
            {"Representative": "Michael T. McCaul", "Ticker": "MSFT", "Transaction": "Sale (Partial)",
             "TransactionDate": "2026-10-02", "ReportDate": "2026-10-20"},
            {"Representative": "Someone Else", "Ticker": "TSLA", "Transaction": "Purchase"},
        ],
        ["McCaul"],
    )
    assert [(r.ticker, r.side) for r in rows] == [("AAPL", "buy"), ("MSFT", "sell")]
    assert rows[0].published_date == date(2026, 10, 20)
