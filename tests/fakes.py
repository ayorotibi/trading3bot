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
        self.picks = None
        self.picks_date = None
        self.other_lists: dict = {}   # other watchlists: {name: (symbols, date saved)}
        self.today_date = date(2026, 10, 28)
        self.bars: dict = {}          # {symbol: [(date, close, volume)]}
        self.actives: list[str] = []
        self.dividends: dict = {}     # {symbol: (ex_date, cash)}
        self.activities: list[dict] = []
        self.filled: list[dict] = []  # [{"symbol", "coid", ...}] for filled_orders()
        self.today: list[dict] = []   # earlier orders today, for orders_today()
        self.replaced: list[tuple] = []
        self.close_in = None          # minutes to the close

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

    def load_picks(self, name="Wheel Picks"):
        if name != "Wheel Picks":
            v = self.other_lists.get(name)
            return None if v is None else list(v[0])
        return None if self.picks is None else list(self.picks)

    def save_picks(self, symbols, name="Wheel Picks"):
        if name != "Wheel Picks":
            self.other_lists[name] = (list(symbols), self.today_date)
            return
        self.picks = list(symbols)
        self.picks_date = "saved"

    def picks_incomplete(self):
        return getattr(self, "incomplete", False)

    def mark_picks_incomplete(self, incomplete):
        self.incomplete = incomplete

    def picks_updated_on(self, name="Wheel Picks"):
        if name != "Wheel Picks":
            v = self.other_lists.get(name)
            return None if v is None else v[1]
        return self.picks_date

    def daily_bars(self, symbols, start):
        return {s: v for s, v in self.bars.items() if s in symbols}

    def daily_closes(self, symbols, start):
        return {s: [(d, c) for d, c, *_ in v] for s, v in self.daily_bars(symbols, start).items()}

    def most_actives(self, top=50):
        return list(self.actives)

    def upcoming_dividends(self, symbols, days=45):
        return {s: v for s, v in self.dividends.items() if s in symbols}

    def account_activities(self, days=400):
        return list(self.activities)

    def filled_orders(self, prefix, days=30):
        return [o for o in self.filled if o["coid"].startswith(prefix)]

    def orders_today(self, prefix="wh-"):
        made = [{"symbol": o["symbol"], "coid": o["coid"], "status": "new", "side": o["side"],
                 "filled_qty": 0.0, "price": 0.0} for o in self.submitted]
        return [o for o in self.today + made if o["coid"].startswith(prefix)]

    def minutes_to_close(self):
        return self.close_in

    def replace_option_limit(self, order_id, limit_price, client_order_id):
        self.replaced.append((order_id, limit_price, client_order_id))

    def portfolio_change(self, period="1W"):
        return None

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
    def add_chain(self, underlying, type_, today: date, dte: int, strikes_deltas_bids, spread=0.04, iv=None):
        expiry = today + timedelta(days=dte)
        for strike, delta, bid in strikes_deltas_bids:
            self.chains.setdefault((underlying, type_), []).append(
                OptionQuote(
                    symbol=occ.build(underlying, expiry, type_, strike),
                    underlying=underlying, type=type_, strike=strike, expiry=expiry,
                    bid=bid, ask=bid + spread, delta=delta, iv=iv,
                )
            )
        return expiry

    def add_order(self, symbol, coid, created_at, limit_price=None, side="sell"):
        self.orders.append(OpenOrder(
            id=f"o{len(self.orders)}", symbol=symbol, side=side, limit_price=limit_price,
            created_at=created_at,
            client_order_id=coid, is_option=occ.is_option_symbol(symbol),
        ))
