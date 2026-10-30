from datetime import date, datetime, timedelta, timezone

import pytest

import bot.sources.disclosed_capitol as dcmod
from bot.notify import MemoryNotifier
from bot.sources.disclosed_capitol import DisclosedCapitolSource, parse_trades, politician_weights
from bot.strategies.picker import StockPicker

from .fakes import FakeBroker
from .test_picker import ListSource, healthy_broker, ins

NOW = datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc)
TODAY = NOW.date()


def row(i, name, ticker, kind="Buy", asset="Stock", pid=None, days_ago=3):
    return {"id": i, "politician_id": pid or name[:3], "politician_name": name, "ticker": ticker,
            "trade_type": kind, "asset_type": asset, "amount_range": "$15,001 - $50,000",
            "amount_low": 15001, "transaction_date": str(TODAY - timedelta(days=days_ago + 10)),
            "disclosure_date": str(TODAY - timedelta(days=days_ago))}


def test_parse_trades_keeps_stock_buys_and_sells_only():
    rows = [row(1, "A Person", "NVDA"), row(2, "B Person", "MSFT", "Sale"), row(3, "C", "XYZ", "Exchange"),
            row(4, "D", "UST", asset="Treasury Bill"), row(5, "E", "BRK/B", "Purchase")]
    t = parse_trades(rows)
    assert [(d.ticker, d.side) for d in t] == [("NVDA", "buy"), ("MSFT", "sell"), ("BRK.B", "buy")]
    assert t[0].source_id == "dc:1" and t[0].published_date == TODAY - timedelta(days=3)
    assert t[0].size == "$15,001 - $50,000"


def test_politician_weights_from_alpha_percent_or_fraction():
    w = politician_weights([
        {"id": 1, "name": "Winner", "alpha_1y": 12.0, "trade_count": 40},     # percent
        {"id": 2, "name": "Loser", "alpha": -0.15, "total_trades": 30},       # fraction
        {"id": 3, "name": "Newbie", "alpha": 0.50, "trade_count": 2},         # too few trades
        {"id": 4, "name": "No Stats"},
    ])
    assert w["Winner"] > 1.4 and w["Loser"] < 0.5 and w["Newbie"] == 1.0
    assert "No Stats" not in w and w["id:1"] == w["Winner"]


class Resp:
    def __init__(self, status, data=None, headers=None):
        self.status_code, self._data, self.headers = status, data, headers or {}

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(str(self.status_code))


def fake_get(monkeypatch, responses):
    calls = []

    def get(url, headers=None, params=None, timeout=None):
        calls.append((url, dict(params), headers))
        return responses.pop(0)
    monkeypatch.setattr(dcmod.requests, "get", get)
    return calls


def test_fetch_pages_through_results_and_reads_credits(monkeypatch):
    page1 = [row(i, f"P{i % 7}", "NVDA") for i in range(1000)]
    page2 = [row(1000 + i, "Q", "AAPL") for i in range(5)]
    calls = fake_get(monkeypatch, [Resp(200, page1), Resp(200, page2, {"X-Credits-Remaining": "6200"})])
    src = DisclosedCapitolSource(api_key="k")
    out = src.fetch(TODAY)
    assert len(out) == 1005 and src.credits_left == 6200
    assert calls[0][1] == {"days": 30, "limit": 1000, "offset": 0} and calls[1][1]["offset"] == 1000
    assert calls[0][2] == {"DC-API-Key": "k"}


def test_fetch_errors_are_clear(monkeypatch):
    fake_get(monkeypatch, [Resp(402)])
    with pytest.raises(RuntimeError, match="out of credits"):
        DisclosedCapitolSource(api_key="k").fetch(TODAY)
    fake_get(monkeypatch, [Resp(403)])
    with pytest.raises(RuntimeError, match="key missing or invalid"):
        DisclosedCapitolSource(api_key="k").fetch(TODAY)
    with pytest.raises(RuntimeError, match="not set"):
        DisclosedCapitolSource(api_key="").fetch(TODAY)


def test_rate_limit_waits_and_retries(monkeypatch):
    fake_get(monkeypatch, [Resp(429, headers={"Retry-After": "7"}), Resp(200, [row(1, "A", "NVDA")])])
    src = DisclosedCapitolSource(api_key="k")
    waits = []
    src._sleep = waits.append
    assert len(src.fetch(TODAY)) == 1 and waits == [7.0]


# ---- weekly refresh in the picker ------------------------------------------
class WeeklySource:
    weekly, refresh_days, partial, credits_left = True, 7, False, 5000

    def __init__(self, trades, weights=None, fail=False):
        self.trades, self.weights, self.fail, self.calls = trades, weights or {}, fail, 0

    def fetch(self, today=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("Disclosed Capitol: out of credits (HTTP 402)")
        return list(self.trades)

    def politician_weights(self):
        return dict(self.weights)


def dc_trades(*specs):
    out = []
    for i, (name, ticker) in enumerate(specs):
        out += parse_trades([row(i, name, ticker)])
    return out


def test_weekly_refresh_then_cached_ranking_on_other_days():
    b = healthy_broker("GOOD", "BAD")
    src = WeeklySource(dc_trades(("G1", "GOOD"), ("G2", "GOOD"), ("B1", "BAD"), ("B2", "BAD"), ("B3", "BAD")),
                       weights={"G1": 2.5, "G2": 2.5, "B1": 0.5, "B2": 0.5, "B3": 0.5})
    s = StockPicker(b, {}, MemoryNotifier(), src, ListSource([]))
    picks = s.run(NOW)
    assert [c.ticker for c in picks] == ["GOOD", "BAD"]         # track records beat a bigger crowd
    assert b.other_lists["Congress Picks v2"][0] == ["GOOD", "BAD"] and src.calls == 1
    assert "Congress refreshed" in s.notify.messages[0] and "credits left: 5,000" in s.notify.messages[0]

    # next day: no API call, same Congress order from the cache
    b.picks_date = TODAY
    s.run(NOW + timedelta(days=1), force=True)
    assert src.calls == 1 and b.picks[:2] == ["GOOD", "BAD"]
    assert "weekly refresh" in s.notify.messages[-1]

    # a week later: refreshed again
    b.today_date = TODAY + timedelta(days=7)
    s.run(NOW + timedelta(days=7), force=True)
    assert src.calls == 2


def test_failed_refresh_falls_back_to_last_weeks_ranking():
    b = healthy_broker("OLD")
    b.other_lists["Congress Picks v2"] = (["OLD"], TODAY - timedelta(days=9))
    s = StockPicker(b, {}, MemoryNotifier(), WeeklySource([], fail=True), ListSource([]))
    picks = s.run(NOW)
    assert [c.ticker for c in picks] == ["OLD"] and b.incomplete
    msg = s.notify.messages[0]
    assert "out of credits" in msg and "last week's Congress ranking" in msg


def test_rejected_page_size_steps_down(monkeypatch):
    people = [{"id": i, "name": f"P{i}", "alpha": 0.1, "trade_count": 20} for i in range(150)]
    calls = fake_get(monkeypatch, [Resp(422), Resp(422), Resp(422), Resp(200, people[:100]),
                                   Resp(200, people[100:], {"X-Credits-Remaining": "5100"})])
    src = DisclosedCapitolSource(api_key="k")
    w = src.politician_weights()
    assert len([k for k in w if not k.startswith("id:")]) == 150 and src.credits_left == 5100
    assert [c[1].get("limit") for c in calls] == [1000, 500, 250, 100, 100]
    assert calls[-1][1]["offset"] == 100


def test_no_paging_params_as_last_resort(monkeypatch):
    calls = fake_get(monkeypatch, [Resp(422)] * 5 + [Resp(200, [{"name": "A", "alpha": 0.2}])])
    assert DisclosedCapitolSource(api_key="k").politician_weights()["A"] > 1
    assert calls[-1][1] == {}
