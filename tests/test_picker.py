from datetime import date, datetime, timedelta, timezone

from bot.events import StaticEarnings
from bot.ledger import Ledger, build_ledger
from bot.models import Disclosure
from bot.notify import MemoryNotifier
from bot.report import WeeklyReport
from bot.sources.openinsider import InsiderBuy, parse_screener_html
from bot.strategies.picker import StockPicker, insider_scores
from bot.strategies.wheel import WheelStrategy
from bot import occ

from .fakes import FakeBroker
from .test_wheel import downtrend, uptrend

NOW = datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc)
TODAY = NOW.date()


class ListSource:
    def __init__(self, items):
        self.items = items

    def fetch(self, today=None):
        return list(self.items)


class FailingSource:
    def fetch(self, today=None):
        raise RuntimeError("blocked")


_n = 0


def cong(who, ticker, side="buy", published_ago=3):
    global _n
    _n += 1
    return Disclosure(f"c{_n}", who, ticker, side, TODAY - timedelta(days=published_ago + 5),
                      TODAY - timedelta(days=published_ago), "15K–50K")


def ins(who, ticker, value=100_000, title="Dir", filed_ago=2):
    return InsiderBuy(ticker, who, title, TODAY - timedelta(days=filed_ago), TODAY - timedelta(days=filed_ago + 1), value)


def picker(broker, congress=(), insiders=(), **cfg):
    cfg = {"congress": {"use_track_records": False}, **cfg}
    return StockPicker(broker, cfg, MemoryNotifier(), ListSource(congress), ListSource(insiders))


def healthy_broker(*tickers):
    b = FakeBroker()
    b.bars = {t: uptrend(50) for t in tickers}
    b.bars["SPY"] = uptrend(600)
    return b


# ---- insiders -------------------------------------------------------------
def test_insider_cluster_and_executive_buys_score_higher():
    buys = [ins("A", "ONE"), ins("B", "TWO"), ins("C", "TWO"), ins("CEO Person", "THREE", title="CEO")]
    s = insider_scores(buys, TODAY, 14)
    assert s["TWO"].score > s["THREE"].score > s["ONE"].score
    assert s["TWO"].insiders == ["B", "C"]


def test_insider_old_filings_ignored():
    assert insider_scores([ins("A", "OLD", filed_ago=30)], TODAY, 14) == {}


OPENINSIDER_HTML = """
<table class="tinytable"><thead><tr>
<th>X</th><th>Filing&nbsp;Date</th><th>Trade&nbsp;Date</th><th>Ticker</th><th>Insider&nbsp;Name</th>
<th>Title</th><th>Trade&nbsp;Type</th><th>Price</th><th>Qty</th><th>Owned</th><th>ΔOwn</th><th>Value</th>
</tr></thead><tbody>
<tr><td>M</td><td>2026-10-27 16:05:11</td><td>2026-10-24</td><td>ABCD</td><td>Smith John</td><td>CEO</td>
<td>P - Purchase</td><td>$12.50</td><td>+20,000</td><td>120,000</td><td>+20%</td><td>+$250,000</td></tr>
<tr><td></td><td>2026-10-27 09:00:00</td><td>2026-10-23</td><td>WXYZ</td><td>Doe Jane</td><td>Dir</td>
<td>S - Sale</td><td>$40.00</td><td>-1,000</td><td>5,000</td><td>-17%</td><td>-$40,000</td></tr>
</tbody></table>
"""


def test_parse_openinsider_by_column_names():
    rows = parse_screener_html(OPENINSIDER_HTML)
    assert len(rows) == 1  # sales skipped
    r = rows[0]
    assert (r.ticker, r.insider, r.title, r.value) == ("ABCD", "Smith John", "CEO", 250_000)
    assert r.filing_date == date(2026, 10, 27) and r.trade_date == date(2026, 10, 24)


# ---- blending ------------------------------------------------------------
def test_stock_with_both_sources_ranks_first():
    b = healthy_broker("BOTH", "CONG", "INS")
    congress = [cong(f"P{i}", "BOTH") for i in range(2)] + [cong(f"Q{i}", "CONG") for i in range(3)]
    insiders = [ins("X", "BOTH"), ins("Y", "INS"), ins("Z", "INS")]
    picks = picker(b, congress, insiders).run(NOW)
    assert picks[0].ticker == "BOTH" and set(picks[0].sources) == {"congress", "insiders"}
    assert b.picks[0] == "BOTH"


def test_downtrend_thin_and_untradable_stocks_dropped():
    b = healthy_broker("GOOD", "THIN", "OTC")
    b.bars["FALL"] = downtrend(50)
    b.bars["THIN"] = uptrend(50)
    b.bars["THIN"] = [(d, c, 10) for d, c, _ in b.bars["THIN"]]  # almost no volume
    b.assets["OTC"] = {"tradable": False, "fractionable": False}
    insiders = [ins("A", t) for t in ("GOOD", "FALL", "THIN", "OTC")]
    picks = picker(b, insiders=insiders).run(NOW)
    assert [c.ticker for c in picks] == ["GOOD"]


def test_most_actives_used_when_other_sources_fail():
    b = healthy_broker("AAPL", "MSFT")
    b.actives = ["AAPL", "MSFT"]
    s = StockPicker(b, {"congress": {"use_track_records": False}}, MemoryNotifier(), FailingSource(), FailingSource())
    picks = s.run(NOW)
    assert {c.ticker for c in picks} == {"AAPL", "MSFT"}
    assert "Congress data unavailable" in s.notify.messages[0] and "Insider data unavailable" in s.notify.messages[0]


def test_picker_refreshes_once_a_day_and_retries_hourly():
    b = FakeBroker()
    s = picker(b)
    assert s.due(NOW.replace(hour=14, minute=37))       # never saved -> now
    b.picks_date = TODAY - timedelta(days=1)
    assert s.due(NOW.replace(hour=15, minute=2))
    assert not s.due(NOW.replace(hour=15, minute=7))
    b.picks_date = TODAY
    assert not s.due(NOW.replace(hour=16, minute=0))


def test_picker_places_no_orders():
    b = healthy_broker("AAA")
    picker(b, insiders=[ins("A", "AAA")]).run(NOW)
    assert b.submitted == []


# ---- ledger --------------------------------------------------------------
def act(kind, symbol, qty, price=None, when="2026-10-01", side=None):
    a = {"activity_type": kind, "symbol": symbol, "qty": str(qty)}
    if kind == "FILL":
        a["transaction_time"] = f"{when}T15:00:00Z"
        a["side"] = side
    else:
        a["date"] = when
    if price is not None:
        a["price"] = str(price)
    return a


def test_ledger_tracks_full_wheel_cycle_across_campaigns():
    p1, c1 = "SOFI261016P00013000", "SOFI261120C00013000"
    p2 = "SOFI261218P00012000"
    acts = [
        act("FILL", p1, 1, 0.80, "2026-09-15", "sell_short"),       # sell put
        act("OPASN", p1, 1, when="2026-10-16"),                      # assigned
        act("OPTRD", "SOFI", 100, 13, "2026-10-16"),                 # buy 100 @ 13
        act("FILL", c1, 1, 0.50, "2026-10-17", "sell_short"),        # covered call
        act("OPASN", c1, 1, when="2026-11-20"),                      # called away
        act("OPTRD", "SOFI", -100, 13, "2026-11-20"),                # sell 100 @ 13
        act("FILL", p2, 1, 0.40, "2026-11-21", "sell_short"),        # new cycle
    ]
    b = build_ledger(acts)["SOFI"]
    assert len(b.completed) == 1
    first = b.completed[0]
    assert round(first.premium, 2) == 130.0 and round(first.stock_pnl, 2) == 0.0 and first.assignments == 2
    assert round(b.current.premium, 2) == 40.0
    assert round(b.total_premium, 2) == 170.0 and round(b.realized_profit, 2) == 130.0


def test_ledger_adjusted_basis_mid_campaign():
    p1, c1 = "SOFI261016P00013000", "SOFI261120C00013500"
    acts = [act("FILL", p1, 1, 0.80, "2026-09-15", "sell_short"), act("OPASN", p1, 1, when="2026-10-16"),
            act("OPTRD", "SOFI", 100, 13, "2026-10-16"), act("FILL", c1, 1, 0.50, "2026-10-17", "sell_short")]
    b = build_ledger(acts)["SOFI"]
    assert b.shares == 100 and round(b.adjusted_basis, 2) == 11.70  # 13 - 0.80 - 0.50


def test_ledger_put_expiring_worthless_closes_campaign():
    p1 = "F261016P00010000"
    b = build_ledger([act("FILL", p1, 2, 0.25, "2026-09-15", "sell_short"), act("OPEXP", p1, 2, when="2026-10-16")])["F"]
    assert len(b.completed) == 1 and round(b.completed[0].profit, 2) == 50.0 and b.current is None


def test_weekly_report_mentions_basis_and_premium():
    from bot.models import Position
    br = FakeBroker()
    br.pos.append(Position("SOFI", 100, 13.0, 12.5, False))
    p1 = "SOFI261016P00013000"
    br.activities = [act("FILL", p1, 1, 0.80, "2026-09-15", "sell_short"), act("OPASN", p1, 1, when="2026-10-16"),
                     act("OPTRD", "SOFI", 100, 13, "2026-10-16")]
    text = WeeklyReport(br, MemoryNotifier(), Ledger(br)).build()
    assert "SOFI: 100 shares, bought @ $13.00, adjusted $12.20" in text
    assert "premium $80" in text


# ---- end to end ------------------------------------------------------------
def test_end_to_end_picks_feed_the_wheel():
    b = healthy_broker("PRICEY", "AAA", "BBB")
    b.bars["PRICEY"] = uptrend(900)
    b.prices = {"PRICEY": 900.0, "AAA": 50.0, "BBB": 50.0}
    b.add_chain("AAA", "put", TODAY, 30, [(47, -0.3, 0.90)])
    b.add_chain("BBB", "put", TODAY, 30, [(47, -0.3, 0.80)])
    congress = [cong(f"P{i}", "PRICEY") for i in range(5)] + [cong(f"Q{i}", "AAA") for i in range(3)]
    insiders = [ins("X", "BBB"), ins("Y", "BBB")]
    picker(b, congress, insiders).run(NOW)
    WheelStrategy(b, {}, MemoryNotifier(), StaticEarnings(), Ledger(b)).run(NOW)
    assert b.picks[:3] == ["BBB", "PRICEY", "AAA"]  # PRICEY ranks high, but a put needs $90k (slice $10k)
    assert sorted(occ.parse(o["symbol"]).underlying for o in b.submitted) == ["AAA", "BBB"]
    assert all(o["intent"] == "sell_to_open" for o in b.submitted)


# ---- Capitol Trades throttling ------------------------------------------
class FakeResp:
    def __init__(self, status, text="", headers=None):
        self.status_code, self.text, self.headers = status, text, headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"{self.status_code} Client Error: Too Many Requests")


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls += 1
        return self.responses.pop(0)


def _page(ticker, trade_id):
    from .test_congress import CAPITOL_HTML
    return CAPITOL_HTML.replace("NVDA:US", f"{ticker}:US").replace("20003801566", trade_id)


def run_source(monkeypatch, responses, **kw):
    import bot.sources.capitol_trades as ct
    session = FakeSession(responses)
    monkeypatch.setattr(ct.requests, "Session", lambda: session)
    src = ct.CapitolTradesSource(max_pages=3, **kw)
    waits = []
    src._sleep = waits.append
    return src, src.fetch(TODAY), session, waits


def test_capitol_trades_backs_off_and_retries_on_429(monkeypatch):
    src, rows, session, waits = run_source(
        monkeypatch, [FakeResp(429, headers={"Retry-After": "20"}), FakeResp(200, _page("AAA", "1")),
                      FakeResp(200, "<html></html>")])
    assert {r.ticker for r in rows} == {"AAA", "MSFT"} and not src.partial
    assert waits[0] == 20


def test_capitol_trades_keeps_partial_results_when_cut_off(monkeypatch):
    src, rows, _, _ = run_source(monkeypatch, [FakeResp(200, _page("AAA", "1"))] + [FakeResp(429)] * 4, retries=3)
    assert src.partial and "AAA" in {r.ticker for r in rows}


def test_capitol_trades_gives_up_when_first_page_blocked(monkeypatch):
    import pytest
    with pytest.raises(RuntimeError):
        run_source(monkeypatch, [FakeResp(429)] * 4, retries=3)


class PartialSource(ListSource):
    partial = True


def test_picker_retries_hourly_until_all_sources_work():
    b = healthy_broker("AAA", "BBB")
    s = StockPicker(b, {"congress": {"use_track_records": False}}, MemoryNotifier(), FailingSource(),
                    ListSource([ins("A", "AAA")]))
    s.run(NOW.replace(hour=13, minute=35))
    assert b.picks == ["AAA"] and b.incomplete and "Will retry every hour" in s.notify.messages[0]
    b.picks_date = TODAY
    assert s.due(NOW.replace(hour=14, minute=0)) and not s.due(NOW.replace(hour=14, minute=10))
    s.run(NOW.replace(hour=14, minute=0))           # still failing: picks kept, no new message
    assert len(s.notify.messages) == 1
    s.congress_source = ListSource([cong(f"P{i}", "BBB") for i in range(2)])
    s.run(NOW.replace(hour=15, minute=0))           # works now: refreshed, flag cleared
    assert "BBB" in b.picks and not b.incomplete and len(s.notify.messages) == 2


def test_partial_congress_data_is_flagged():
    b = healthy_broker("AAA")
    s = StockPicker(b, {"congress": {"use_track_records": False}}, MemoryNotifier(),
                    PartialSource([cong(f"P{i}", "AAA") for i in range(2)]), ListSource([]))
    s.run(NOW)
    assert b.incomplete and "Congress data incomplete" in s.notify.messages[0]
