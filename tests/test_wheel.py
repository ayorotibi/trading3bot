from datetime import date, datetime, timedelta, timezone

from bot import occ
from bot.models import Position
from bot.notify import MemoryNotifier
from bot.strategies.wheel import WheelStrategy, round_to_tick

from .fakes import FakeBroker

NOW = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)
TODAY = NOW.date()


def make(broker, **cfg):
    base = {"symbols": ["SOFI"], "contracts_per_symbol": 1}
    base.update(cfg)
    return WheelStrategy(broker, base, MemoryNotifier())


def test_occ_round_trip():
    s = occ.build("SOFI", date(2026, 11, 27), "put", 12.5)
    assert s == "SOFI261127P00012500"
    p = occ.parse(s)
    assert (p.underlying, p.expiry, p.type, p.strike) == ("SOFI", date(2026, 11, 27), "put", 12.5)


def test_tick_rounding():
    assert round_to_tick(0.437, up=False) == 0.43
    assert round_to_tick(3.12, up=False) == 3.10
    assert round_to_tick(3.12, up=True) == 3.15


def test_sells_put_closest_to_target_delta_and_expiry():
    b = FakeBroker(prices={"SOFI": 15.0})
    b.add_chain("SOFI", "put", TODAY, 25, [(14, -0.40, 0.80), (13, -0.29, 0.45), (12, -0.15, 0.20)])
    b.add_chain("SOFI", "put", TODAY, 44, [(13, -0.33, 0.70)])
    make(b).run(NOW)
    assert len(b.submitted) == 1
    o = b.submitted[0]
    # target_dte 30 -> the 25-day expiry wins; delta 0.29 is closest to 0.30
    assert o["symbol"] == occ.build("SOFI", TODAY + timedelta(days=25), "put", 13)
    assert o["intent"] == "sell_to_open" and o["side"] == "sell"
    assert o["price"] == 0.47  # mid of 0.45/0.49


def test_no_put_when_not_enough_cash():
    b = FakeBroker(prices={"SOFI": 15.0}, equity=1000, cash=1000)
    b.add_chain("SOFI", "put", TODAY, 30, [(13, -0.3, 0.45)])
    make(b).run(NOW)
    assert b.submitted == []


def test_skips_picks_too_expensive_for_their_budget_slice():
    b = FakeBroker(prices={"SOFI": 15.0, "F": 9.0}, equity=4000, cash=4000)
    b.add_chain("SOFI", "put", TODAY, 30, [(13, -0.3, 0.45)])
    b.add_chain("F", "put", TODAY, 30, [(8, -0.3, 0.25)])
    # slice = 4000 x 50% / 2 stocks = $1,000: SOFI (~$1,500) skipped, F (~$900) fits
    make(b, symbols=["SOFI", "F"], max_symbols=2).run(NOW)
    assert [o["symbol"][:1] for o in b.submitted] == ["F"]


def test_moves_to_next_pick_when_options_are_illiquid():
    b = FakeBroker(prices={"AAA": 20.0, "BBB": 20.0})
    b.picks = ["AAA", "BBB"]
    b.add_chain("AAA", "put", TODAY, 30, [(18, -0.3, 0.20)], spread=0.50)  # too wide
    b.add_chain("BBB", "put", TODAY, 30, [(18, -0.3, 0.40)])
    WheelStrategy(b, {"max_symbols": 1}, MemoryNotifier()).run(NOW)
    assert [o["symbol"][:3] for o in b.submitted] == ["BBB"]


def test_waits_while_short_put_open():
    b = FakeBroker(prices={"SOFI": 15.0})
    put = occ.build("SOFI", TODAY + timedelta(days=20), "put", 13)
    b.pos.append(Position(put, -1, 0.50, 0.40, True))
    b.quotes[put] = (0.38, 0.42)
    b.add_chain("SOFI", "put", TODAY, 30, [(13, -0.3, 0.45)])
    make(b).run(NOW)
    assert b.submitted == []


def test_take_profit_buys_back_at_half_premium():
    b = FakeBroker(prices={"SOFI": 16.0})
    put = occ.build("SOFI", TODAY + timedelta(days=20), "put", 13)
    b.pos.append(Position(put, -1, 0.50, 0.20, True))
    b.quotes[put] = (0.18, 0.22)
    make(b).run(NOW)
    assert len(b.submitted) == 1
    o = b.submitted[0]
    assert (o["symbol"], o["side"], o["intent"], o["price"]) == (put, "buy", "buy_to_close", 0.22)


def test_assigned_shares_get_covered_call_above_cost_basis():
    b = FakeBroker(prices={"SOFI": 12.0})
    b.pos.append(Position("SOFI", 200, 13.0, 12.0, False))
    # 12.5 strike is closest to delta target but below the 13.00 cost basis
    b.add_chain("SOFI", "call", TODAY, 30, [(12.5, 0.31, 0.40), (13, 0.22, 0.25), (14, 0.10, 0.10)])
    make(b).run(NOW)
    assert len(b.submitted) == 1
    o = b.submitted[0]
    assert o["symbol"] == occ.build("SOFI", TODAY + timedelta(days=30), "call", 13)
    assert o["qty"] == 2  # 200 shares -> 2 contracts


def test_partially_covered_shares_only_sell_remaining_calls():
    b = FakeBroker(prices={"SOFI": 12.0})
    b.pos.append(Position("SOFI", 200, 11.0, 12.0, False))
    call = occ.build("SOFI", TODAY + timedelta(days=10), "call", 13)
    b.pos.append(Position(call, -1, 0.30, 0.25, True))
    b.quotes[call] = (0.24, 0.27)
    b.add_chain("SOFI", "call", TODAY, 30, [(13, 0.3, 0.35)])
    make(b).run(NOW)
    assert [o["qty"] for o in b.submitted] == [1]


def test_skips_symbol_with_working_order_and_reprices_stale_one():
    b = FakeBroker(prices={"SOFI": 15.0})
    b.add_chain("SOFI", "put", TODAY, 30, [(13, -0.3, 0.45)])
    put = occ.build("SOFI", TODAY + timedelta(days=30), "put", 13)
    b.add_order(put, "wh-put-x", created_at=NOW - timedelta(minutes=5))
    make(b).run(NOW)
    assert b.submitted == [] and b.cancelled == []

    # Unfilled for 45 minutes: cancel it, then re-sell at the current mid in the same run
    b2 = FakeBroker(prices={"SOFI": 15.0})
    b2.add_chain("SOFI", "put", TODAY, 30, [(13, -0.3, 0.45)])
    b2.add_order(put, "wh-put-x", created_at=NOW - timedelta(minutes=45))
    make(b2).run(NOW)
    assert b2.cancelled == ["o0"]
    assert len(b2.submitted) == 1


def test_skips_illiquid_contracts():
    b = FakeBroker(prices={"SOFI": 15.0})
    b.add_chain("SOFI", "put", TODAY, 30, [(13, -0.3, 0.20)], spread=0.30)  # spread is > mid
    make(b).run(NOW)
    assert b.submitted == []


def test_wheel_picks_affordable_congress_stocks_up_to_max_symbols():
    b = FakeBroker(prices={"NVDA": 180.0, "SOFI": 15.0, "F": 11.0, "BAC": 40.0})
    b.picks = ["NVDA", "SOFI", "F", "BAC"]  # NVDA is above max_share_price
    for sym, strike in (("SOFI", 13), ("F", 10), ("BAC", 37)):
        b.add_chain(sym, "put", TODAY, 30, [(strike, -0.3, 0.45)])
    WheelStrategy(b, {"max_symbols": 2, "max_share_price": 50}, MemoryNotifier()).run(NOW)
    assert [o["symbol"][:4].rstrip("0123456789") for o in b.submitted] == ["SOFI", "F"]


def test_wheel_keeps_running_cycle_after_stock_leaves_picks():
    b = FakeBroker(prices={"SOFI": 12.0, "F": 11.0})
    b.picks = ["F"]
    b.pos.append(Position("SOFI", 100, 13.0, 12.0, False))  # assigned earlier
    b.add_chain("SOFI", "call", TODAY, 30, [(13, 0.3, 0.30)])
    WheelStrategy(b, {"max_symbols": 1}, MemoryNotifier()).run(NOW)
    assert [o["symbol"][:4] for o in b.submitted] == ["SOFI"]  # F not started, limit is 1


def test_wheel_ignores_shares_owned_by_congress_strategy():
    b = FakeBroker(prices={"SOFI": 12.0})
    b.picks = []
    b.pos.append(Position("SOFI", 150, 12.0, 12.0, False))
    b.holdings = {"SOFI": 150}  # all bought by the congress strategy
    b.add_chain("SOFI", "call", TODAY, 30, [(13, 0.3, 0.30)])
    WheelStrategy(b, {"symbols_from_congress": True, "symbols": []}, MemoryNotifier()).run(NOW)
    assert b.submitted == []  # no covered calls on congress shares
