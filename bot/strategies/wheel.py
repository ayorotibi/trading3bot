"""The wheel strategy.

This is the main strategy. Which stocks: by default the top-ranked "congress
picks" (stocks members of Congress are buying, weighted by each politician's
track record), best first, up to max_symbols at once. A stock is skipped if
one put on it would need more than its share of the cash budget
(equity x max_cash_pct / max_symbols) or no option contract passes the
liquidity filters; the next pick is tried instead. A stock already mid-cycle
keeps going even if it drops off the list.

For each symbol, every run works out where it is in the cycle from your
Alpaca positions (no database needed):

  1. No shares, no short put   -> sell a cash-secured put
  2. Short put open            -> wait (take profit early if it hits the target)
  3. Put assigned, own shares  -> sell covered calls (never below cost basis)
  4. Calls assigned, shares gone -> back to step 1

Expiry and assignment are handled by the broker; the bot just reacts to
whatever positions exist on the next run.
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from .. import occ
from ..models import OptionQuote, Position

log = logging.getLogger(__name__)

DEFAULTS = {
    "symbols_from_congress": True,
    "max_symbols": 5,
    "max_share_price": None,
    "symbols": [],
    "contracts_per_symbol": 1,
    "target_dte": 30,
    "min_dte": 21,
    "max_dte": 45,
    "put_target_delta": 0.30,
    "call_target_delta": 0.30,
    "otm_pct_if_no_delta": 0.05,
    "min_premium": 0.10,
    "max_spread_pct": 0.30,
    "take_profit_pct": 0.50,
    "calls_never_below_cost_basis": True,
    "max_cash_pct": 0.50,
    "reprice_after_minutes": 30,
}


def round_to_tick(price: float, up: bool) -> float:
    tick = 0.05 if price >= 3 else 0.01
    fn = math.ceil if up else math.floor
    return round(fn(round(price / tick, 6)) * tick, 2)


def sell_price(q: OptionQuote) -> float:
    """Ask for the mid, rounded down, but never below the bid."""
    return max(q.bid, round_to_tick(q.mid, up=False))


def pick_contract(
    chain: list[OptionQuote],
    type_: str,
    spot: float,
    target_delta: float,
    target_dte: int,
    today: date,
    min_premium: float,
    max_spread_pct: float,
    min_strike: Optional[float] = None,
    otm_pct_if_no_delta: float = 0.05,
) -> Optional[OptionQuote]:
    """Choose the expiry closest to target_dte, then the strike closest to target delta."""
    candidates = []
    for q in chain:
        if q.type != type_ or q.bid < min_premium or q.ask <= 0:
            continue
        if (q.ask - q.bid) / q.mid > max_spread_pct:
            continue
        if type_ == "put" and q.strike >= spot:  # out of the money only
            continue
        if type_ == "call" and q.strike <= spot:
            continue
        if min_strike is not None and q.strike < min_strike:
            continue
        candidates.append(q)
    if not candidates:
        return None

    best_expiry = min({q.expiry for q in candidates}, key=lambda e: abs((e - today).days - target_dte))
    same_expiry = [q for q in candidates if q.expiry == best_expiry]

    with_delta = [q for q in same_expiry if q.delta is not None]
    if with_delta:
        return min(with_delta, key=lambda q: abs(abs(q.delta) - target_delta))

    target_strike = spot * (1 - otm_pct_if_no_delta) if type_ == "put" else spot * (1 + otm_pct_if_no_delta)
    return min(same_expiry, key=lambda q: abs(q.strike - target_strike))


class WheelStrategy:
    name = "wheel"

    def __init__(self, broker, config: dict, notifier, other_strategy_prefixes=("cr-",)):
        self.broker = broker
        self.cfg = {**DEFAULTS, **(config or {})}
        self.notify = notifier
        self.other_prefixes = other_strategy_prefixes

    def _wheel_shares(self, positions) -> dict[str, Position]:
        """Stock positions minus shares other strategies bought (e.g. congress picks)."""
        other = defaultdict(float)
        for prefix in self.other_prefixes:
            for sym, qty in self.broker.strategy_holdings(prefix).items():
                other[sym] += qty
        out = {}
        for p in positions:
            if p.is_option:
                continue
            qty = p.qty - max(0.0, other.get(p.symbol, 0.0))
            if qty >= 1:
                out[p.symbol] = Position(p.symbol, qty, p.avg_entry_price, p.current_price, False)
        return out

    def active_symbols(self, positions, orders, wheel_shares) -> list[str]:
        """Stocks with a wheel cycle in progress (they keep going even if they leave the picks)."""
        active: list[str] = []
        for p in positions:
            if p.is_option and p.qty < 0:
                active.append(occ.parse(p.symbol).underlying)
        for o in orders:
            if o.is_option and o.client_order_id.startswith("wh-"):
                active.append(occ.parse(o.symbol).underlying)
        for sym, p in wheel_shares.items():
            if p.qty >= 100:
                active.append(sym)
        return list(dict.fromkeys(active))

    def candidates(self) -> tuple[list[str], str]:
        if self.cfg["symbols_from_congress"]:
            picks = self.broker.load_picks() or []
            if picks:
                return picks, "congress picks"
            return [s.upper() for s in self.cfg["symbols"]], "fallback symbols (no congress picks yet)"
        return [s.upper() for s in self.cfg["symbols"]], "config symbols"

    def run(self, now: Optional[datetime] = None) -> None:
        now = now or datetime.now(timezone.utc)
        today = now.date()

        orders = self.broker.open_orders()
        self._cancel_stale_orders(orders, now)
        orders = self.broker.open_orders()
        positions = self.broker.positions()
        self._wheel_share_map = self._wheel_shares(positions)

        self._take_profits(positions, orders, today)
        orders = self.broker.open_orders()

        account = self.broker.account()
        reserved = sum(
            occ.parse(p.symbol).strike * 100 * abs(p.qty)
            for p in positions
            if p.is_option and p.qty < 0 and occ.parse(p.symbol).type == "put"
        )

        # 1. Keep existing cycles moving (covered calls, re-selling puts after a cycle ends)
        active = self.active_symbols(positions, orders, self._wheel_share_map)
        for sym in active:
            try:
                reserved += self._handle_symbol(sym, positions, orders, account, reserved, today)
            except Exception as exc:
                log.exception("Wheel error on %s", sym)
                self.notify.send(f"⚠️ Wheel error on {sym}: {exc}")

        # 2. Start new cycles on the best-ranked stocks until max_symbols are in play
        limit = int(self.cfg["max_symbols"])
        free_slots = limit - len(active)
        cands, source = self.candidates()
        if free_slots <= 0:
            log.info("Wheel: %d/%d stocks in play %s, not starting new ones", len(active), limit, active)
            return
        per_symbol = account.equity * float(self.cfg["max_cash_pct"]) / max(1, limit)
        max_price = self.cfg.get("max_share_price")
        contracts = int(self.cfg["contracts_per_symbol"])
        started = []
        for sym in cands:
            if len(started) >= free_slots:
                break
            if sym in active:
                continue
            try:
                spot = self.broker.latest_price(sym)
                if max_price and spot > float(max_price):
                    continue
                if spot * 100 * contracts > per_symbol:
                    log.info("Wheel: skipping %s, a put at ~$%.0f needs ~$%.0f (budget per stock $%.0f)",
                             sym, spot, spot * 100 * contracts, per_symbol)
                    continue
                added = self._handle_symbol(sym, positions, orders, account, reserved, today)
                if added > 0:
                    reserved += added
                    started.append(sym)
            except Exception as exc:
                log.exception("Wheel error on %s", sym)
                self.notify.send(f"⚠️ Wheel error on {sym}: {exc}")
        log.info("Wheel: in play %s, started %s (candidates from %s)", active, started or "none", source)

    # ---------------------------------------------------------------------
    def _cancel_stale_orders(self, orders, now: datetime) -> None:
        limit = timedelta(minutes=self.cfg["reprice_after_minutes"])
        for o in orders:
            if not (o.is_option and o.client_order_id.startswith("wh-")):
                continue
            created = o.created_at if o.created_at.tzinfo else o.created_at.replace(tzinfo=timezone.utc)
            if now - created > limit:
                log.info("Wheel: cancelling unfilled order %s on %s to re-price", o.id, o.symbol)
                self.broker.cancel_order(o.id)

    def _take_profits(self, positions, orders, today: date) -> None:
        tp = self.cfg["take_profit_pct"]
        if not tp:
            return
        pending = {o.symbol for o in orders}
        for p in positions:
            if not (p.is_option and p.qty < 0) or p.symbol in pending:
                continue
            bid, ask = self.broker.option_quote(p.symbol)
            if ask <= 0:
                continue
            if ask <= p.avg_entry_price * (1 - tp):
                price = min(round_to_tick(ask, up=True), round(p.avg_entry_price, 2))
                qty = int(abs(p.qty))
                coid = f"wh-tp-{p.symbol}-{datetime.now(timezone.utc):%Y%m%d%H%M}"
                self.broker.submit_option_limit(p.symbol, qty, "buy", price, "buy_to_close", coid)
                self.notify.send(
                    f"✅ Wheel take-profit: buying back {qty}x {p.symbol} @ ${price:.2f} "
                    f"(sold @ ${p.avg_entry_price:.2f})"
                )

    def _handle_symbol(self, sym, positions, orders, account, reserved, today) -> float:
        """Returns extra cash reserved by a new put, so later symbols see it."""
        if any(
            (occ.is_option_symbol(o.symbol) and occ.parse(o.symbol).underlying == sym) or o.symbol == sym
            for o in orders
        ):
            log.info("Wheel %s: order already working, waiting", sym)
            return 0.0

        shares = getattr(self, "_wheel_share_map", {}).get(sym)
        share_qty = shares.qty if shares else 0
        shorts = [p for p in positions if p.is_option and p.qty < 0 and occ.parse(p.symbol).underlying == sym]
        short_puts = [p for p in shorts if occ.parse(p.symbol).type == "put"]
        short_calls = [p for p in shorts if occ.parse(p.symbol).type == "call"]

        if short_puts:
            log.info("Wheel %s: short put open, waiting for expiry/assignment", sym)
            return 0.0

        covered = sum(abs(p.qty) for p in short_calls)
        callable_contracts = int(share_qty // 100) - int(covered)
        if callable_contracts > 0:
            self._sell_call(sym, callable_contracts, shares.avg_entry_price, today)
            return 0.0
        if share_qty >= 100:
            log.info("Wheel %s: all shares covered by calls, waiting", sym)
            return 0.0
        return self._sell_put(sym, account, reserved, today)

    def _chain(self, sym, type_, today, **kw):
        return self.broker.option_chain(
            sym,
            type_,
            today + timedelta(days=self.cfg["min_dte"]),
            today + timedelta(days=self.cfg["max_dte"]),
            **kw,
        )

    def _sell_put(self, sym, account, reserved, today) -> float:
        n = int(self.cfg["contracts_per_symbol"])
        spot = self.broker.latest_price(sym)
        chain = self._chain(sym, "put", today, strike_max=spot)
        q = pick_contract(
            chain, "put", spot, self.cfg["put_target_delta"], self.cfg["target_dte"], today,
            self.cfg["min_premium"], self.cfg["max_spread_pct"],
            otm_pct_if_no_delta=self.cfg["otm_pct_if_no_delta"],
        )
        if not q:
            log.info("Wheel %s: no put met the filters (spot %.2f)", sym, spot)
            return 0.0

        needed = q.strike * 100 * n
        cap = account.equity * self.cfg["max_cash_pct"]
        if needed > account.options_buying_power:
            log.info("Wheel %s: need $%.0f to secure put, only $%.0f available", sym, needed, account.options_buying_power)
            return 0.0
        if reserved + needed > cap:
            log.info("Wheel %s: would exceed max_cash_pct cap ($%.0f of $%.0f)", sym, reserved + needed, cap)
            return 0.0

        price = sell_price(q)
        coid = f"wh-put-{q.symbol}-{datetime.now(timezone.utc):%Y%m%d%H%M}"
        self.broker.submit_option_limit(q.symbol, n, "sell", price, "sell_to_open", coid)
        delta = f", delta {q.delta:.2f}" if q.delta is not None else ""
        self.notify.send(
            f"🟢 Wheel: selling {n}x {sym} ${q.strike:g} put exp {q.expiry} @ ${price:.2f}{delta} "
            f"(spot ${spot:.2f}, premium ${price * 100 * n:.0f}, secures ${needed:,.0f})"
        )
        account.options_buying_power -= needed
        return needed

    def _sell_call(self, sym, contracts, cost_basis, today) -> None:
        spot = self.broker.latest_price(sym)
        floor = cost_basis if self.cfg["calls_never_below_cost_basis"] else None
        chain = self._chain(sym, "call", today, strike_min=max(spot, floor or 0))
        q = pick_contract(
            chain, "call", spot, self.cfg["call_target_delta"], self.cfg["target_dte"], today,
            self.cfg["min_premium"], self.cfg["max_spread_pct"], min_strike=floor,
            otm_pct_if_no_delta=self.cfg["otm_pct_if_no_delta"],
        )
        if not q:
            log.info("Wheel %s: no call met the filters (spot %.2f, cost basis %.2f)", sym, spot, cost_basis)
            return
        price = sell_price(q)
        coid = f"wh-call-{q.symbol}-{datetime.now(timezone.utc):%Y%m%d%H%M}"
        self.broker.submit_option_limit(q.symbol, contracts, "sell", price, "sell_to_open", coid)
        self.notify.send(
            f"🔵 Wheel: selling {contracts}x {sym} ${q.strike:g} covered call exp {q.expiry} @ ${price:.2f} "
            f"(spot ${spot:.2f}, cost basis ${cost_basis:.2f})"
        )
