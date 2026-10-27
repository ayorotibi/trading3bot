"""An in-memory stand-in for AlpacaBroker used by the tests."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from bot import occ
from bot.models import Account, OpenOrder, OptionQuote, Position


class FakeBroker:
    def __init__(self, prices=None, equity=100_000.0, cash=100_000.0):
        self.prices = prices or {}
        self.acct = Account(equity=equity, cash=cash, options_buying_power=cash)
        self.pos: list[Position] = []
        self.orders: list[OpenOrder] = []
        self.submitted: list[dict] = []
        self.cancelled: list[str] = []
        self.history: set[str] = set()
        self.chains: dict[tuple, list[OptionQuote]] = {}
        self.quotes: dict[str, tuple[float, float]] = {}
        self.assets: dict[str, dict] = {}

    # broker API used by strategies
    def market_is_open(self):
        return True

    def account(self):
        return Account(self.acct.equity, self.acct.cash, self.acct.options_buying_power)

    def positions(self):
        return list(self.pos)

    def open_orders(self):
        return [o for o in self.orders if o.id not in self.cancelled]

    def order_exists(self, coid):
        return coid in self.history

    def asset_info(self, symbol):
        return self.assets.get(symbol, {"tradable": True, "fractionable": True})

    def latest_price(self, symbol):
        return self.prices[symbol]

    def option_chain(self, underlying, type_, exp_from, exp_to, strike_min=None, strike_max=None):
        out = []
        for q in self.chains.get((underlying, type_), []):
            if not (exp_from <= q.expiry <= exp_to):
                continue
            if strike_min is not None and q.strike < strike_min:
                continue
            if strike_max is not None and q.strike > strike_max:
                continue
            out.append(q)
        return out

    def option_quote(self, symbol):
        return self.quotes[symbol]

    def submit_option_limit(self, symbol, qty, side, limit_price, intent, client_order_id):
        self.submitted.append(dict(symbol=symbol, qty=qty, side=side, price=limit_price,
                                   intent=intent, coid=client_order_id))
        self.history.add(client_order_id)
        return "id"

    def submit_stock_market(self, symbol, side, client_order_id, notional=None, qty=None):
        self.submitted.append(dict(symbol=symbol, side=side, notional=notional, qty=qty, coid=client_order_id))
        self.history.add(client_order_id)
        return "id"

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)

    # helpers for building scenarios
    def add_chain(self, underlying, type_, today: date, dte: int, strikes_deltas_bids, spread=0.04):
        expiry = today + timedelta(days=dte)
        for strike, delta, bid in strikes_deltas_bids:
            self.chains.setdefault((underlying, type_), []).append(
                OptionQuote(
                    symbol=occ.build(underlying, expiry, type_, strike),
                    underlying=underlying, type=type_, strike=strike, expiry=expiry,
                    bid=bid, ask=bid + spread, delta=delta,
                )
            )
        return expiry

    def add_order(self, symbol, coid, created_at):
        self.orders.append(OpenOrder(
            id=f"o{len(self.orders)}", symbol=symbol, side="sell",
            created_at=created_at,
            client_order_id=coid, is_option=occ.is_option_symbol(symbol),
        ))
