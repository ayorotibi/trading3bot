from datetime import date, datetime, timedelta, timezone

from bot import occ
from bot.events import StaticEarnings
from bot.ledger import Ledger
from bot.models import Position
from bot.notify import MemoryNotifier
from bot.strategies.wheel import WheelStrategy, choose_contract, round_to_tick, target_delta
from bot.market import Regime

from .fakes import FakeBroker

NOW = datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc)
TODAY = NOW.date()


def make(broker, earnings=None, **cfg):
    base = {"symbols": ["SOFI"], "contracts_per_symbol": 1}
    base.update(cfg)
    return WheelStrategy(broker, base, MemoryNotifier(), StaticEarnings(earnings), Ledger(broker))


def rows(prices, volume=5_000_000):
    start = TODAY - timedelta(days=len(prices) - 1)
    return [(start + timedelta(days=i), p, volume) for i, p in enumerate(prices)]


def uptrend(end=100.0, n=260):
    return rows([end * (0.6 + 0.4 * i / (n - 1)) for i in range(n)])


def downtrend(end=50.0, n=260):
    return rows([end * (1.6 - 0.6 * i / (n - 1)) for i in range(n)])


def weak(end=95.0, n=260):
    """Long uptrend, then a dip below the 50-day average but still above the 200-day."""
    p = [60 + 40 * i / 229 for i in range(230)] + [100 - (100 - end) * i / 29 for i in range(30)]
    return rows(p)


def put_sym(sym, dte, strike):
    return occ.build(sym, TODAY + timedelta(days=dte), "put", strike)


def call_sym(sym, dte, strike):
    return occ.build(sym, TODAY + timedelta(days=dte), "call", strike)


# ---- basics ---------------------------------------------------------------
def test_occ_round_trip():
    s = occ.build("SOFI", date(2026, 11, 27), "put", 12.5)
    assert s == "SOFI261127P00012500"
    p = occ.parse(s)
    assert (p.underlying, p.expiry, p.type, p.strike) == ("SOFI", date(2026, 11, 27), "put", 12.5)


def test_tick_rounding():
    assert round_to_tick(0.437, up=False) == 0.43
    assert round_to_tick(3.12, up=False) == 3.10
    assert round_to_tick(3.12, up=True) == 3.15


# ---- choosing contracts ---------------------------------------------------
def test_sells_put_closest_to_target_delta_and_expiry():
    b = FakeBroker(prices={"SOFI": 15.0})
    b.add_chain("SOFI", "put", TODAY, 25, [(14, -0.40, 0.80), (13, -0.29, 0.45), (12, -0.15, 0.20)])
    b.add_chain("SOFI", "put", TODAY, 44, [(13, -0.33, 0.70)])
    make(b).run(NOW)
    assert len(b.submitted) == 1
    o = b.submitted[0]
    assert o["symbol"] == put_sym("SOFI", 25, 13)
    assert o["intent"] == "sell_to_open" and o["price"] == 0.47


def test_delta_adapts_to_conditions():
    normal, defensive = Regime("normal", ""), Regime("defensive", "")
    from bot.market import technicals
    up, wk = technicals(uptrend()), technicals(weak())
    assert up.trend == "up" and wk.trend == "weak"
    cfg = WheelStrategy(FakeBroker(), {}, MemoryNotifier()).cfg
    assert target_delta("put", cfg, up, normal, 1.0)[0] == 0.30
    assert target_delta("put", cfg, wk, normal, 1.0)[0] == 0.25
    assert target_delta("put", cfg, wk, defensive, 1.5)[0] == 0.15
    assert target_delta("call", cfg, up, normal, None)[0] == 0.25      # leave room to rise
    assert target_delta("call", cfg, None, defensive, None)[0] == 0.35  # collect more


def test_weak_stock_gets_further_out_of_the_money_put():
    b = FakeBroker(prices={"SOFI": 95.0})
    b.bars = {"SOFI": weak(), "SPY": uptrend(600)}
    b.add_chain("SOFI", "put", TODAY, 30, [(90, -0.30, 1.50), (85, -0.24, 1.00), (80, -0.15, 0.60)])
    make(b, max_symbols=1, max_cash_pct=1.0).run(NOW)
    assert b.submitted[0]["symbol"] == put_sym("SOFI", 30, 85)


def test_minimum_yield_rejects_poor_premium():
    b = FakeBroker(prices={"SOFI": 15.0})
    b.add_chain("SOFI", "put", TODAY, 30, [(13, -0.30, 0.10)])  # ~9%/yr on $13 cash
    make(b).run(NOW)
    assert b.submitted == []


def test_prefers_strike_below_support():
    chain_b = FakeBroker()
    chain_b.add_chain("X", "put", TODAY, 30, [(96, -0.31, 2.0), (94, -0.27, 1.6)])
    q, _ = choose_contract(chain_b.chains[("X", "put")], "put", 100.0, TODAY, 0.30, 30, 0.1, 0.3,
                           support=95.0)
    assert q.strike == 94


# ---- earnings ---------------------------------------------------------------
def test_chooses_expiry_before_earnings():
    b = FakeBroker(prices={"SOFI": 15.0})
    b.add_chain("SOFI", "put", TODAY, 9, [(14, -0.30, 0.30)])
    b.add_chain("SOFI", "put", TODAY, 30, [(13, -0.30, 0.45)])
    s = make(b, earnings={"SOFI": TODAY + timedelta(days=20)})
    s.run(NOW)
    assert b.submitted[0]["symbol"] == put_sym("SOFI", 9, 14)
    assert "before earnings" in s.notify.messages[0]


def test_waits_when_earnings_too_close():
    b = FakeBroker(prices={"SOFI": 15.0})
    b.add_chain("SOFI", "put", TODAY, 30, [(13, -0.30, 0.45)])
    make(b, earnings={"SOFI": TODAY + timedelta(days=5)}).run(NOW)
    assert b.submitted == []


def test_closes_winner_before_earnings():
    b = FakeBroker(prices={"SOFI": 16.0})
    put = put_sym("SOFI", 20, 13)
    b.pos.append(Position(put, -1, 0.50, 0.33, True))
    b.quotes[put] = (0.31, 0.34)  # ~35% profit, below the 50% take-profit
    make(b, earnings={"SOFI": TODAY + timedelta(days=2)}).run(NOW)
    assert [(o["symbol"], o["intent"]) for o in b.submitted] == [(put, "buy_to_close")]


# ---- managing open contracts ---------------------------------------------
def test_take_profit_buys_back_at_half_premium():
    b = FakeBroker(prices={"SOFI": 16.0})
    put = put_sym("SOFI", 20, 13)
    b.pos.append(Position(put, -1, 0.50, 0.20, True))
    b.quotes[put] = (0.18, 0.22)
    make(b).run(NOW)
    assert [(o["symbol"], o["intent"], o["price"]) for o in b.submitted] == [(put, "buy_to_close", 0.22)]


def test_cheap_option_bought_back_early_but_left_to_expire_on_expiry_day():
    b = FakeBroker(prices={"SOFI": 16.0})
    p1, p0 = put_sym("SOFI", 3, 13), put_sym("SOFI", 0, 12)
    b.pos += [Position(p1, -1, 0.08, 0.04, True), Position(p0, -1, 0.06, 0.01, True)]
    b.quotes[p1], b.quotes[p0] = (0.03, 0.04), (0.00, 0.01)
    make(b, take_profit_pct=0).run(NOW)
    assert [o["symbol"] for o in b.submitted] == [p1]


def test_tested_put_on_wanted_stock_accepts_assignment():
    b = FakeBroker(prices={"SOFI": 12.9})
    b.picks = ["SOFI"]
    put = put_sym("SOFI", 5, 13)
    b.pos.append(Position(put, -1, 0.50, 0.30, True))
    b.quotes[put] = (0.28, 0.32)
    b.add_chain("SOFI", "put", TODAY, 33, [(12, -0.3, 0.60)])
    make(b).run(NOW)
    assert b.submitted == []


def test_tested_put_on_unwanted_stock_rolls_down_and_out_for_credit():
    b = FakeBroker(prices={"SOFI": 12.9})
    b.picks = ["OTHER"]
    put = put_sym("SOFI", 5, 13)
    b.pos.append(Position(put, -1, 0.50, 0.30, True))
    b.quotes[put] = (0.28, 0.32)
    b.add_chain("SOFI", "put", TODAY, 33, [(13, -0.45, 0.80), (12, -0.3, 0.45), (11, -0.15, 0.20)])
    s = make(b)
    s.run(NOW)
    assert len(b.submitted) == 1
    o = b.submitted[0]
    assert (o["symbol"], o["intent"]) == (put, "buy_to_close")
    assert o["coid"].startswith(f"wh-rc-{put}-{put_sym('SOFI', 33, 12)}-")  # lowest strike that pays a credit
    assert "Rolling" in s.notify.messages[0]


def test_roll_opens_new_leg_once_close_has_filled():
    b = FakeBroker(prices={"SOFI": 12.9})
    old, new = put_sym("SOFI", 5, 13), put_sym("SOFI", 33, 12)
    b.filled.append({"symbol": old, "coid": f"wh-rc-{old}-{new}-1500"})
    b.quotes[new] = (0.45, 0.49)
    make(b).run(NOW)
    assert [(o["symbol"], o["intent"]) for o in b.submitted] == [(new, "sell_to_open")]
    # once the new leg has filled it is never opened again
    b2 = FakeBroker(prices={"SOFI": 12.9})
    b2.filled += [{"symbol": old, "coid": f"wh-rc-{old}-{new}-1500"},
                  {"symbol": new, "coid": f"wh-ro-{new}-1505"}]
    b2.quotes[new] = (0.45, 0.49)
    make(b2, symbols=[]).run(NOW)
    assert b2.submitted == []


def test_max_rolls_then_accept_assignment():
    b = FakeBroker(prices={"SOFI": 12.9})
    b.picks = ["OTHER"]
    put = put_sym("SOFI", 5, 13)
    b.pos.append(Position(put, -1, 0.50, 0.30, True))
    b.quotes[put] = (0.28, 0.32)
    b.add_chain("SOFI", "put", TODAY, 33, [(12, -0.3, 0.45)])
    b.filled += [{"symbol": put_sym("SOFI", 5 + i, 14), "coid": f"wh-rc-x-y-{i}"} for i in range(3)]
    make(b).run(NOW)
    assert b.submitted == []


def test_tested_call_on_strong_wanted_stock_rolls_up_and_out():
    b = FakeBroker(prices={"SOFI": 100.5})
    b.picks = ["SOFI"]
    b.bars = {"SOFI": uptrend(100.5), "SPY": uptrend(600)}
    call = call_sym("SOFI", 4, 100)
    b.pos += [Position("SOFI", 100, 90.0, 100.5, False), Position(call, -1, 1.20, 1.10, True)]
    b.quotes[call] = (1.05, 1.15)
    b.add_chain("SOFI", "call", TODAY, 32, [(100, 0.5, 3.00), (103, 0.4, 1.80), (106, 0.3, 1.00)])
    make(b).run(NOW)
    o = b.submitted[0]
    assert o["intent"] == "buy_to_close" and f"-{call_sym('SOFI', 32, 103)}-" in o["coid"]


def test_tested_call_on_unwanted_stock_lets_shares_go():
    b = FakeBroker(prices={"SOFI": 100.5})
    b.picks = []
    call = call_sym("SOFI", 4, 100)
    b.pos += [Position("SOFI", 100, 90.0, 100.5, False), Position(call, -1, 1.20, 1.10, True)]
    b.quotes[call] = (1.05, 1.15)
    b.add_chain("SOFI", "call", TODAY, 32, [(103, 0.4, 1.80)])
    make(b, symbols=[]).run(NOW)
    assert b.submitted == []


def test_ex_dividend_risk_rolls_covered_call():
    b = FakeBroker(prices={"KO": 70.0})
    b.picks = ["KO"]
    call = call_sym("KO", 20, 65)
    b.pos += [Position("KO", 100, 60.0, 70.0, False), Position(call, -1, 2.00, 5.10, True)]
    b.quotes[call] = (5.05, 5.15)  # time value ~$0.10 < $0.50 dividend
    b.dividends = {"KO": (TODAY + timedelta(days=1), 0.50)}
    b.add_chain("KO", "call", TODAY, 40, [(67, 0.6, 5.40), (70, 0.5, 3.50)])
    make(b, symbols=[]).run(NOW)
    o = b.submitted[0]
    assert o["intent"] == "buy_to_close" and o["symbol"] == call


# ---- cost basis -----------------------------------------------------------
def test_covered_call_uses_adjusted_cost_basis_from_ledger():
    b = FakeBroker(prices={"SOFI": 12.0})
    b.pos.append(Position("SOFI", 100, 13.0, 12.0, False))
    put = put_sym("SOFI", -10, 13)
    b.activities = [
        {"activity_type": "FILL", "symbol": put, "side": "sell_short", "qty": "1", "price": "0.80",
         "transaction_time": "2026-09-20T15:00:00Z"},
        {"activity_type": "OPASN", "symbol": put, "qty": "1", "date": "2026-10-18"},
        {"activity_type": "OPTRD", "symbol": "SOFI", "qty": "100", "price": "13", "date": "2026-10-18"},
    ]
    # adjusted basis = 13.00 - 0.80 = 12.20: the $12.50 call is now allowed, $12 isn't
    b.add_chain("SOFI", "call", TODAY, 30, [(12.0, 0.5, 0.60), (12.5, 0.33, 0.35), (13, 0.2, 0.15)])
    make(b, symbols=[]).run(NOW)
    assert b.submitted[0]["symbol"] == call_sym("SOFI", 30, 12.5)


def test_purchase_price_floor_option():
    b = FakeBroker(prices={"SOFI": 12.0})
    b.pos.append(Position("SOFI", 100, 13.0, 12.0, False))
    b.add_chain("SOFI", "call", TODAY, 30, [(12.5, 0.33, 0.35), (13, 0.2, 0.15)])
    make(b, symbols=[], call_floor="purchase").run(NOW)
    assert b.submitted[0]["symbol"] == call_sym("SOFI", 30, 13)


def test_partially_covered_shares_only_sell_remaining_calls():
    b = FakeBroker(prices={"SOFI": 12.0})
    b.pos.append(Position("SOFI", 200, 11.0, 12.0, False))
    call = call_sym("SOFI", 10, 13)
    b.pos.append(Position(call, -1, 0.30, 0.25, True))
    b.quotes[call] = (0.24, 0.27)
    b.add_chain("SOFI", "call", TODAY, 30, [(13, 0.3, 0.35)])
    make(b).run(NOW)
    assert [o["qty"] for o in b.submitted] == [1]


# ---- choosing stocks & sizing --------------------------------------------
def test_wheel_picks_affordable_stocks_up_to_max_symbols():
    b = FakeBroker(prices={"NVDA": 180.0, "SOFI": 15.0, "F": 11.0, "BAC": 40.0}, equity=40_000, cash=40_000)
    b.picks = ["NVDA", "SOFI", "F", "BAC"]  # NVDA put needs $18k, slice is $10k
    for sym, strike, bid in (("SOFI", 13, 0.45), ("F", 10, 0.30), ("BAC", 37, 0.90)):
        b.add_chain(sym, "put", TODAY, 30, [(strike, -0.3, bid)])
    make(b, max_symbols=2, max_cash_pct=0.5).run(NOW)
    assert [occ.parse(o["symbol"]).underlying for o in b.submitted] == ["SOFI", "F"]


def test_moves_to_next_pick_when_options_are_illiquid():
    b = FakeBroker(prices={"AAA": 20.0, "BBB": 20.0})
    b.picks = ["AAA", "BBB"]
    b.add_chain("AAA", "put", TODAY, 30, [(18, -0.3, 0.20)], spread=0.50)
    b.add_chain("BBB", "put", TODAY, 30, [(18, -0.3, 0.40)])
    make(b, max_symbols=1).run(NOW)
    assert [occ.parse(o["symbol"]).underlying for o in b.submitted] == ["BBB"]


def test_skips_stocks_in_downtrend():
    b = FakeBroker(prices={"AAA": 50.0, "BBB": 20.0})
    b.picks = ["AAA", "BBB"]
    b.bars = {"AAA": downtrend(50), "BBB": uptrend(20), "SPY": uptrend(600)}
    b.add_chain("AAA", "put", TODAY, 30, [(47, -0.3, 1.0)])
    b.add_chain("BBB", "put", TODAY, 30, [(19, -0.3, 0.40)])
    make(b, max_symbols=1).run(NOW)
    assert [occ.parse(o["symbol"]).underlying for o in b.submitted] == ["BBB"]


def test_defensive_market_limits_new_positions():
    b = FakeBroker(prices={s: 20.0 for s in "ABCDE"})
    b.picks = list("ABCDE")
    b.bars = {"SPY": downtrend(500)}
    for s in "ABCDE":
        b.add_chain(s, "put", TODAY, 30, [(18, -0.2, 0.40)])
    make(b, max_symbols=5, defensive_max_symbols=2).run(NOW)
    assert len(b.submitted) == 2


def test_no_new_positions_on_blackout_dates():
    b = FakeBroker(prices={"SOFI": 15.0})
    b.add_chain("SOFI", "put", TODAY, 30, [(13, -0.3, 0.45)])
    make(b, blackout_dates=[TODAY.isoformat()]).run(NOW)
    assert b.submitted == []


def test_keeps_running_cycle_after_stock_leaves_picks():
    b = FakeBroker(prices={"SOFI": 12.0, "F": 11.0})
    b.picks = ["F"]
    b.pos.append(Position("SOFI", 100, 13.0, 12.0, False))
    b.add_chain("SOFI", "call", TODAY, 30, [(13, 0.3, 0.30)])
    make(b, max_symbols=1).run(NOW)
    assert [occ.parse(o["symbol"]).underlying for o in b.submitted] == ["SOFI"]


def test_skips_symbol_with_working_order_and_reprices_stale_one():
    put = put_sym("SOFI", 30, 13)
    b = FakeBroker(prices={"SOFI": 15.0})
    b.add_chain("SOFI", "put", TODAY, 30, [(13, -0.3, 0.45)])
    b.add_order(put, "wh-put-x", created_at=NOW - timedelta(minutes=5))
    make(b).run(NOW)
    assert b.submitted == [] and b.cancelled == []

    b2 = FakeBroker(prices={"SOFI": 15.0})
    b2.add_chain("SOFI", "put", TODAY, 30, [(13, -0.3, 0.45)])
    b2.add_order(put, "wh-put-x", created_at=NOW - timedelta(minutes=45))
    make(b2).run(NOW)
    assert b2.cancelled == ["o0"] and len(b2.submitted) == 1


def test_no_put_when_not_enough_cash():
    b = FakeBroker(prices={"SOFI": 15.0}, equity=1000, cash=1000)
    b.add_chain("SOFI", "put", TODAY, 30, [(13, -0.3, 0.45)])
    make(b).run(NOW)
    assert b.submitted == []
