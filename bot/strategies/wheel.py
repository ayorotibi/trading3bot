"""The wheel: the bot's main strategy.

  sell a cash-secured put -> (assigned) own 100 shares -> sell covered calls
  -> (called away) back to puts ... and repeat.

Every run (every 5 minutes in market hours) it:

1. MANAGES every open contract:
   - Take profit: buy back once `take_profit_pct` of the premium is captured.
   - Nearly worthless: buy back at <= `close_cheap_at` to free the cash early
     (or simply let it expire on expiry day).
   - Earnings: close a winning contract `earnings_close_days` before earnings.
   - Ex-dividend: a covered call in the money with less time value than the
     dividend is likely to be assigned early; roll it if the stock is still
     wanted, otherwise let the shares go.
   - Approaching the strike (within `tested_buffer`, `manage_dte` days or
     less to expiry), it decides:
       Put:  stock still wanted and not collapsing -> accept assignment
             (we wanted to own it; covered calls come next).
             Otherwise -> roll down and out for a net credit, or accept
             assignment if no roll pays.
       Call: stock still wanted and trending up -> roll up and out for a net
             credit to keep the shares; otherwise let them be called away
             (strike is above the adjusted cost basis, so the cycle profits).
     A position is rolled at most `max_rolls` times.
   Rolls are done in two steps: buy back the old contract, and once that has
   filled, sell the new one (next run).

2. KEEPS CYCLES GOING: covered calls on any uncovered wheel shares, never
   below the adjusted cost basis (purchase cost minus all premium collected
   in this campaign, from the ledger).

3. STARTS NEW CYCLES on the best "Wheel Picks" it can afford, up to
   `max_symbols` (fewer in a defensive market), skipping blackout days.

Choosing a contract:
  * Expiry nearest `target_dte` (21-45 days), but always BEFORE the next
    earnings date (down to `earnings_min_dte` days) if `avoid_earnings`.
  * Delta target starts at 0.30 and adapts: further out of the money when
    the stock is below its 50-day average, the market is defensive, or
    premiums are rich (implied vol well above realised vol); covered calls go
    further out in strong uptrends to leave room to rise.
  * Must pay at least `min_annual_yield` on the cash (puts) or shares (calls).
  * Puts prefer strikes at or below recent support (20-day low).
"""
from __future__ import annotations

import logging
import math
import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from .. import occ
from ..events import EarningsCalendar
from ..ledger import Ledger
from ..market import Regime, Technicals, market_regime, technicals
from ..models import OptionQuote

log = logging.getLogger(__name__)

DEFAULTS = {
    "symbols_from_picks": True,
    "symbols": [],
    "max_symbols": 5,
    "defensive_max_symbols": 3,
    "max_share_price": None,
    "contracts_per_symbol": 1,
    "max_cash_pct": 0.50,
    # contract choice
    "target_dte": 30,
    "min_dte": 21,
    "max_dte": 45,
    "put_target_delta": 0.30,
    "call_target_delta": 0.30,
    "min_delta": 0.10,
    "delta_step": 0.05,
    "rich_iv_ratio": 1.3,
    "otm_pct_if_no_delta": 0.05,
    "min_premium": 0.10,
    "max_spread_pct": 0.30,
    "put_min_annual_yield": 0.10,
    "call_min_annual_yield": 0.04,
    "prefer_support": True,
    "call_floor": "adjusted",  # "adjusted" (after premiums) or "purchase" (assignment price)
    # events
    "avoid_earnings": True,
    "earnings_min_dte": 7,
    "earnings_close_days": 2,
    "earnings_close_profit_pct": 0.25,
    "blackout_dates": [],
    # management
    "take_profit_pct": 0.50,
    "close_cheap_at": 0.05,
    "manage_dte": 7,
    "tested_buffer": 0.02,
    "deep_itm_pct": 0.05,
    "max_rolls": 3,
    "min_roll_credit": 0.05,
    "roll_min_dte": 14,
    "roll_max_dte": 45,
    "ex_div_days": 2,
    "reprice_every_minutes": 10,
}


# ---------------------------------------------------------------------------
# pricing helpers
# ---------------------------------------------------------------------------
def round_to_tick(price: float, up: bool) -> float:
    tick = 0.05 if price >= 3 else 0.01
    fn = math.ceil if up else math.floor
    return round(fn(round(price / tick, 6)) * tick, 2)


def sell_price(q: OptionQuote) -> float:
    """Ask for the mid, rounded down, but never below the bid."""
    return max(q.bid, round_to_tick(q.mid, up=False))


def annual_yield(q: OptionQuote, collateral: float, today: date) -> float:
    dte = max(1, (q.expiry - today).days)
    return (q.mid / collateral) * 365 / dte if collateral > 0 else 0.0


def atm_iv(chain: list[OptionQuote], spot: float) -> Optional[float]:
    ivs = [q.iv for q in chain if q.iv and abs(q.strike - spot) <= spot * 0.05]
    return statistics.median(ivs) if ivs else None


def target_delta(type_: str, cfg: dict, tech: Optional[Technicals], regime: Regime,
                 iv_ratio: Optional[float]) -> tuple[float, list[str]]:
    """Adapt the delta target to conditions. Lower delta = further out of the money."""
    step = float(cfg["delta_step"])
    reasons = []
    if type_ == "put":
        d = float(cfg["put_target_delta"])
        if tech and tech.trend == "weak":
            d -= step
            reasons.append("stock below its 50-day average")
        if regime.defensive:
            d -= step
            reasons.append("defensive market")
        if iv_ratio and iv_ratio >= float(cfg["rich_iv_ratio"]):
            d -= step
            reasons.append(f"rich premiums (IV {iv_ratio:.1f}x realised)")
    else:
        d = float(cfg["call_target_delta"])
        if tech and tech.trend == "up" and (tech.return_3m or 0) > 0:
            d -= step
            reasons.append("strong uptrend, leaving room to rise")
        if regime.defensive:
            d += step
            reasons.append("defensive market, collecting more")
    return round(min(0.45, max(float(cfg["min_delta"]), d)), 3), reasons


def choose_contract(
    chain: list[OptionQuote],
    type_: str,
    spot: float,
    today: date,
    delta: float,
    target_dte: int,
    min_premium: float,
    max_spread_pct: float,
    min_annual_yield: float = 0.0,
    min_strike: Optional[float] = None,
    max_strike: Optional[float] = None,
    support: Optional[float] = None,
    expiry_before: Optional[date] = None,
    otm_pct_if_no_delta: float = 0.05,
) -> tuple[Optional[OptionQuote], str]:
    """Returns (contract, "") or (None, reason nothing qualified)."""
    candidates = []
    for q in chain:
        if q.type != type_ or q.bid < min_premium or q.ask <= 0 or q.mid <= 0:
            continue
        if (q.ask - q.bid) / q.mid > max_spread_pct:
            continue
        if type_ == "put" and q.strike >= spot:
            continue
        if type_ == "call" and q.strike <= spot:
            continue
        if min_strike is not None and q.strike < min_strike:
            continue
        if max_strike is not None and q.strike > max_strike:
            continue
        if expiry_before is not None and q.expiry >= expiry_before:
            continue
        collateral = q.strike if type_ == "put" else spot
        if annual_yield(q, collateral, today) < min_annual_yield:
            continue
        candidates.append(q)
    if not candidates:
        why = "no liquid out-of-the-money contract paid enough"
        if expiry_before:
            why += f" with an expiry before earnings on {expiry_before:%d %b}"
        return None, why

    best_expiry = min({q.expiry for q in candidates}, key=lambda e: abs((e - today).days - target_dte))
    same = [q for q in candidates if q.expiry == best_expiry]
    with_delta = [q for q in same if q.delta is not None]
    if with_delta:
        def score(q):
            s = abs(abs(q.delta) - delta)
            if type_ == "put" and support and q.strike > support:
                s += 0.03  # prefer strikes at or below recent support
            return s
        return min(with_delta, key=score), ""
    goal = spot * (1 - otm_pct_if_no_delta) if type_ == "put" else spot * (1 + otm_pct_if_no_delta)
    return min(same, key=lambda q: abs(q.strike - goal)), ""


def find_roll(chain: list[OptionQuote], current: occ.OptionSymbol, close_cost: float, spot: float,
              today: date, min_credit: float, max_spread_pct: float, min_strike: Optional[float] = None,
              expiry_before: Optional[date] = None) -> Optional[OptionQuote]:
    """A later contract that pays more than it costs to close the current one.

    Puts roll down (same or lower strike), calls roll up (same or higher, and
    not below min_strike). Prefers the most favourable strike, then the
    nearest expiry.
    """
    options = []
    for q in chain:
        if q.type != current.type or q.expiry <= current.expiry or q.bid <= 0 or q.mid <= 0:
            continue
        if (q.ask - q.bid) / q.mid > max_spread_pct:
            continue
        if expiry_before and q.expiry >= expiry_before:
            continue
        if current.type == "put" and q.strike > current.strike:
            continue
        if current.type == "call" and (q.strike < current.strike or (min_strike and q.strike < min_strike)):
            continue
        if sell_price(q) - close_cost >= min_credit:
            options.append(q)
    if not options:
        return None
    if current.type == "put":
        return min(options, key=lambda q: (q.strike, q.expiry))
    return max(options, key=lambda q: (q.strike, -q.expiry.toordinal()))


# ---------------------------------------------------------------------------
class WheelStrategy:
    name = "wheel"

    def __init__(self, broker, config: dict, notifier, earnings=None, ledger=None):
        self.broker = broker
        self.cfg = {**DEFAULTS, **(config or {})}
        if "symbols_from_congress" in (config or {}):  # older config name
            self.cfg["symbols_from_picks"] = config["symbols_from_congress"]
        self.notify = notifier
        self.earnings = earnings or EarningsCalendar()
        self.ledger = ledger or Ledger(broker)
        self._techs: dict[str, Optional[Technicals]] = {}
        self._spy_rows = None
        self._positions = []

    # ---- market data helpers -------------------------------------------
    def _bars(self, sym: str):
        return self.broker.daily_bars([sym], self.today - timedelta(days=300)).get(sym)

    def tech(self, sym: str) -> Optional[Technicals]:
        if sym not in self._techs:
            try:
                if self._spy_rows is None:
                    self._spy_rows = self._bars("SPY") or []
                rows = self._spy_rows if sym == "SPY" else self._bars(sym)
                self._techs[sym] = technicals(rows or [], self._spy_rows)
            except Exception as exc:
                log.warning("No price history for %s: %s", sym, exc)
                self._techs[sym] = None
        return self._techs[sym]

    def _earnings_before(self, sym: str) -> Optional[date]:
        if not self.cfg["avoid_earnings"]:
            return None
        e = self.earnings.next_earnings(sym, self.today)
        return e if e and e > self.today else None

    def _chain(self, sym, type_, min_dte, max_dte, **kw):
        return self.broker.option_chain(sym, type_, self.today + timedelta(days=min_dte),
                                        self.today + timedelta(days=max_dte), **kw)

    # ---- main loop --------------------------------------------------------
    def run(self, now: Optional[datetime] = None) -> None:
        now = now or datetime.now(timezone.utc)
        self.now, self.today = now, now.date()
        self._techs, self._spy_rows = {}, None
        self.regime = market_regime(self.tech("SPY"))
        picks = self.broker.load_picks() or []
        self.wanted = set(picks)

        self._today_orders = None
        self._orders_today()  # snapshot before this run places anything
        orders = self.broker.open_orders()
        self._work_orders(orders, now)
        orders = self.broker.open_orders()
        positions = self._positions = self.broker.positions()

        self._manage(positions, orders)
        orders = self.broker.open_orders()
        positions = self._positions = self.broker.positions()
        pending_rolls = self._pending_rolls(positions, orders)

        account = self.broker.account()
        reserved = sum(occ.parse(p.symbol).strike * 100 * abs(p.qty) for p in positions
                       if p.is_option and p.qty < 0 and occ.parse(p.symbol).type == "put")

        # 1. Existing cycles: finish rolls, sell covered calls on uncovered shares
        active = self.active_symbols(positions, orders) | set(pending_rolls)
        for sym in sorted(active):
            try:
                reserved += self._continue_cycle(sym, positions, orders, account, reserved, pending_rolls)
            except Exception as exc:
                log.exception("Wheel error on %s", sym)
                self.notify.send(f"⚠️ Wheel error on {sym}: {exc}")

        # 2. New cycles on the best picks
        self._start_new_cycles(active, positions, orders, account, reserved, picks)

        # 3. End-of-day summary on the last run before the close
        try:
            mins = self.broker.minutes_to_close()
            if mins is not None and 0 < mins <= 5.5:
                self._daily_summary()
        except Exception:
            log.exception("Daily summary failed")

    # ---- 1. managing open contracts ---------------------------------------
    def _manage(self, positions, orders) -> None:
        pending = {o.symbol for o in orders}
        shorts = [p for p in positions if p.is_option and p.qty < 0 and p.symbol not in pending]
        if not shorts:
            return
        call_unders = sorted({occ.parse(p.symbol).underlying for p in shorts if occ.parse(p.symbol).type == "call"})
        dividends = self.broker.upcoming_dividends(call_unders) if call_unders else {}
        for p in shorts:
            try:
                self._manage_one(p, dividends)
            except Exception as exc:
                log.exception("Managing %s failed", p.symbol)
                self.notify.send(f"⚠️ Wheel: couldn't manage {p.symbol}: {exc}")

    def _manage_one(self, p, dividends) -> None:
        info = occ.parse(p.symbol)
        sym, qty = info.underlying, int(abs(p.qty))
        dte = (info.expiry - self.today).days
        bid, ask = self.broker.option_quote(p.symbol)
        spot = self.broker.latest_price(sym)
        entry = p.avg_entry_price
        mark = (bid + ask) / 2 if ask > 0 else bid
        profit_pct = (entry - mark) / entry if entry > 0 else 0.0
        label = f"{sym} ${info.strike:g} {info.type} exp {info.expiry:%d %b}"

        # Take profit
        tp = float(self.cfg["take_profit_pct"] or 0)
        if tp and ask > 0 and ask <= entry * (1 - tp):
            self._buy_back(p.symbol, qty, ask, f"✅ Take profit on {label}: {profit_pct:.0%} of the premium captured")
            return
        # Nearly worthless
        cheap = float(self.cfg["close_cheap_at"] or 0)
        if cheap and 0 < ask <= cheap:
            if dte >= 1:
                self._buy_back(p.symbol, qty, ask, f"🧹 {label} is nearly worthless (${ask:.2f}); "
                                                    "buying back to free the cash for the next trade")
            else:
                log.info("Wheel: %s expires today worthless, letting it expire", label)
            return
        # Earnings coming before expiry
        e = self.earnings.next_earnings(sym, self.today)
        if e and self.today < e <= info.expiry and (e - self.today).days <= int(self.cfg["earnings_close_days"]):
            if profit_pct >= float(self.cfg["earnings_close_profit_pct"]):
                self._buy_back(p.symbol, qty, ask, f"📅 Closing {label} before earnings on {e:%d %b} "
                                                    f"with {profit_pct:.0%} profit")
                return
            log.info("Wheel: %s has earnings on %s; not enough profit to close (%.0f%%), holding", label, e, profit_pct * 100)

        tech = self.tech(sym)
        wanted = sym in self.wanted and (tech is None or tech.trend != "down")

        # Ex-dividend early-assignment risk on covered calls
        if info.type == "call" and sym in dividends:
            ex_date, cash = dividends[sym]
            extrinsic = mark - max(0.0, spot - info.strike)
            if (spot > info.strike and self.today < ex_date <= info.expiry
                    and (ex_date - self.today).days <= int(self.cfg["ex_div_days"]) and extrinsic < cash):
                if wanted and self._try_roll(p, info, ask, spot, f"ex-dividend {ex_date:%d %b}"):
                    return
                log.info("Wheel: %s may be assigned early before ex-dividend %s; letting shares go", label, ex_date)
                return

        if dte > int(self.cfg["manage_dte"]):
            return
        buffer = float(self.cfg["tested_buffer"])
        if info.type == "put":
            if spot > info.strike * (1 + buffer):
                log.info("Wheel: %s out of the money with %d days left, will likely expire worthless", label, dte)
                return
            deep = spot < info.strike * (1 - float(self.cfg["deep_itm_pct"]))
            if wanted and not deep:
                log.info("Wheel: %s near/below strike (%.2f); still a wanted stock, accepting assignment", label, spot)
                return
            why = "stock no longer in the picks" if not wanted else f"stock fell {1 - spot / info.strike:.0%} below the strike"
            if self._try_roll(p, info, ask, spot, why):
                return
            log.info("Wheel: %s tested (%s) but no roll pays a credit; accepting assignment", label, why)
        else:
            if spot < info.strike * (1 - buffer):
                log.info("Wheel: %s out of the money with %d days left, will likely expire worthless", label, dte)
                return
            if wanted and tech is not None and tech.trend == "up":
                if self._try_roll(p, info, ask, spot, "strong stock, keeping the shares"):
                    return
            log.info("Wheel: %s at/above strike (%.2f); letting shares be called away at a profit", label, spot)

    def _buy_back(self, symbol, qty, ask, message) -> None:
        bid, _ = self.broker.option_quote(symbol)
        price = max(0.01, round_to_tick((bid + ask) / 2 if bid > 0 else ask, up=True))  # start at mid
        coid = f"wh-bc-{symbol}-{self.now:%Y%m%d%H%M}"
        self.broker.submit_option_limit(symbol, qty, "buy", price, "buy_to_close", coid)
        self._alert(f"{message} — buying back {qty}x from ${price:.2f}", "wh-bc", symbol)

    def _rolls_done(self, sym: str) -> int:
        try:
            return sum(1 for o in self.broker.filled_orders("wh-rc-", days=120)
                       if occ.is_option_symbol(o["symbol"]) and occ.parse(o["symbol"]).underlying == sym)
        except Exception:
            return 0

    def _try_roll(self, p, info: occ.OptionSymbol, ask: float, spot: float, why: str) -> bool:
        sym = info.underlying
        if self._rolls_done(sym) >= int(self.cfg["max_rolls"]):
            log.info("Wheel: %s already rolled %d times, not rolling again", sym, self.cfg["max_rolls"])
            return False
        close_cost = round_to_tick(ask, up=True)
        floor = None
        if info.type == "call":
            floor = self._call_floor(sym)
        chain = self._chain(sym, info.type, int(self.cfg["roll_min_dte"]), int(self.cfg["roll_max_dte"]))
        target = find_roll(chain, info, close_cost, spot, self.today, float(self.cfg["min_roll_credit"]),
                           float(self.cfg["max_spread_pct"]), min_strike=floor,
                           expiry_before=self._earnings_before(sym))
        if not target:
            return False
        qty = int(abs(p.qty))
        coid = f"wh-rc-{p.symbol}-{target.symbol}-{self.now:%H%M}"
        self.broker.submit_option_limit(p.symbol, qty, "buy", close_cost, "buy_to_close", coid)
        credit = sell_price(target) - close_cost
        direction = "down and out" if info.type == "put" else "up and out"
        self._alert_text = (
            f"🔄 Rolling {sym} {info.type} {direction} ({why}): buying back ${info.strike:g} "
            f"exp {info.expiry:%d %b} @ ${close_cost:.2f}, then selling ${target.strike:g} "
            f"exp {target.expiry:%d %b} @ ~${sell_price(target):.2f} (net credit ~${credit * 100 * qty:.0f})"
        )
        self._alert(self._alert_text, "wh-rc", p.symbol)
        return True

    def _pending_rolls(self, positions, orders) -> dict[str, str]:
        """{underlying: new contract} for rolls whose buy-back filled but new leg isn't open yet."""
        try:
            closes = self.broker.filled_orders("wh-rc-", days=2)
            opened = {o["coid"].split("-")[2] for o in self.broker.filled_orders("wh-ro-", days=3)}
        except Exception:
            return {}
        held = {p.symbol for p in positions}
        working = {o.symbol for o in orders}
        out = {}
        for o in closes:
            parts = o["coid"].split("-")
            if len(parts) < 4 or not occ.is_option_symbol(parts[3]):
                continue
            new = parts[3]
            if new in held or new in working or new in opened:
                continue
            out[occ.parse(new).underlying] = new
        return out

    # ---- 2. continuing cycles --------------------------------------------
    def active_symbols(self, positions, orders) -> set[str]:
        active = set()
        for p in positions:
            if p.is_option and p.qty < 0:
                active.add(occ.parse(p.symbol).underlying)
            elif not p.is_option and p.qty >= 100:
                active.add(p.symbol)
        for o in orders:
            if o.is_option and o.client_order_id.startswith("wh-"):
                active.add(occ.parse(o.symbol).underlying)
        return active

    def _call_floor(self, sym: str) -> Optional[float]:
        """Lowest strike for a covered call: adjusted cost basis (after premiums) or purchase price."""
        shares = next((p for p in self._positions if not p.is_option and p.symbol == sym), None)
        purchase = shares.avg_entry_price if shares else None
        if self.cfg["call_floor"] == "adjusted":
            basis = self.ledger.adjusted_basis(sym)
            if basis is not None and (purchase is None or basis >= purchase * 0.5):
                return round(basis, 2)
            if basis is not None:
                log.warning("Wheel %s: ledger basis $%.2f looks wrong vs purchase $%.2f; using purchase price",
                            sym, basis, purchase)
        return purchase

    def _continue_cycle(self, sym, positions, orders, account, reserved, pending_rolls) -> float:
        if any((occ.is_option_symbol(o.symbol) and occ.parse(o.symbol).underlying == sym) or o.symbol == sym
               for o in orders):
            log.info("Wheel %s: order working, waiting", sym)
            return 0.0
        if sym in pending_rolls:
            return self._open_roll_leg(pending_rolls[sym], positions, account, reserved)

        shares = next((p for p in positions if not p.is_option and p.symbol == sym), None)
        shorts = [p for p in positions if p.is_option and p.qty < 0 and occ.parse(p.symbol).underlying == sym]
        if any(occ.parse(p.symbol).type == "put" for p in shorts):
            return 0.0  # put open: managed above
        covered = sum(abs(p.qty) for p in shorts if occ.parse(p.symbol).type == "call")
        callable_ = int((shares.qty if shares else 0) // 100) - int(covered)
        if callable_ > 0:
            self._sell_call(sym, callable_, shares)
        return 0.0

    def _open_roll_leg(self, new_symbol, positions, account, reserved) -> float:
        info = occ.parse(new_symbol)
        bid, ask = self.broker.option_quote(new_symbol)
        if bid <= 0:
            log.info("Wheel: no bid on roll target %s yet, will retry", new_symbol)
            return 0.0
        q = OptionQuote(new_symbol, info.underlying, info.type, info.strike, info.expiry, bid, ask)
        qty = int(self.cfg["contracts_per_symbol"])
        if info.type == "call":
            shares = next((p for p in positions if not p.is_option and p.symbol == info.underlying), None)
            qty = int((shares.qty if shares else 0) // 100)
            if qty < 1:
                return 0.0
        price = sell_price(q)
        coid = f"wh-ro-{new_symbol}-{self.now:%H%M}"
        self.broker.submit_option_limit(new_symbol, qty, "sell", price, "sell_to_open", coid)
        self._alert(f"🔄 Roll, second step: selling {qty}x {info.underlying} ${info.strike:g} {info.type} "
                    f"exp {info.expiry:%d %b} from ${price:.2f}", "wh-ro", new_symbol)
        return info.strike * 100 * qty if info.type == "put" else 0.0

    # ---- 3. new cycles -----------------------------------------------------
    def candidates(self, picks) -> tuple[list[str], str]:
        if self.cfg["symbols_from_picks"] and picks:
            return picks, "wheel picks"
        return [s.upper() for s in self.cfg["symbols"]], "config symbols"

    def _start_new_cycles(self, active, positions, orders, account, reserved, picks) -> None:
        if self.today.isoformat() in {str(d) for d in self.cfg["blackout_dates"]}:
            log.info("Wheel: %s is a blackout date, no new positions", self.today)
            return
        limit = int(self.cfg["defensive_max_symbols"] if self.regime.defensive else self.cfg["max_symbols"])
        free = limit - len(active)
        if free <= 0:
            log.info("Wheel: %d/%d stocks in play (%s market), not starting new ones",
                     len(active), limit, self.regime.name)
            return
        cands, source = self.candidates(picks)
        per_symbol = account.equity * float(self.cfg["max_cash_pct"]) / max(1, int(self.cfg["max_symbols"]))
        n = int(self.cfg["contracts_per_symbol"])
        started = []
        working = {occ.parse(o.symbol).underlying if occ.is_option_symbol(o.symbol) else o.symbol for o in orders}
        for sym in cands:
            if len(started) >= free:
                break
            if sym in active or sym in working:
                continue
            try:
                spot = self.broker.latest_price(sym)
                if self.cfg["max_share_price"] and spot > float(self.cfg["max_share_price"]):
                    continue
                if spot * 100 * n > per_symbol:
                    log.info("Wheel: skipping %s, a put needs ~$%.0f (budget per stock $%.0f)", sym, spot * 100 * n, per_symbol)
                    continue
                tech = self.tech(sym)
                if tech is not None and tech.trend == "down":
                    log.info("Wheel: skipping %s, it's in a downtrend", sym)
                    continue
                added = self._sell_put(sym, spot, tech, account, reserved)
                if added > 0:
                    reserved += added
                    started.append(sym)
            except Exception as exc:
                log.exception("Wheel error on %s", sym)
                self.notify.send(f"⚠️ Wheel error on {sym}: {exc}")
        log.info("Wheel: in play %s, started %s (from %s, %s market: %s)", sorted(active), started or "none",
                 source, self.regime.name, self.regime.reason)

    # ---- selling contracts -------------------------------------------------
    def _select(self, sym, type_, spot, tech, min_strike=None, max_strike=None):
        before = self._earnings_before(sym)
        min_dte, max_dte = int(self.cfg["min_dte"]), int(self.cfg["max_dte"])
        note = ""
        if before and (before - self.today).days <= max_dte:
            min_dte = min(min_dte, int(self.cfg["earnings_min_dte"]))
            note = f", expires before earnings on {before:%d %b}"
            if (before - self.today).days <= min_dte:
                return None, f"earnings on {before:%d %b} are too close; waiting until after", ""
        chain = self._chain(sym, type_, min_dte, max_dte, strike_min=min_strike, strike_max=max_strike)
        iv = atm_iv(chain, spot)
        iv_ratio = iv / tech.realized_vol if iv and tech and tech.realized_vol else None
        delta, reasons = target_delta(type_, self.cfg, tech, self.regime, iv_ratio)
        q, why = choose_contract(
            chain, type_, spot, self.today, delta, int(self.cfg["target_dte"]),
            float(self.cfg["min_premium"]), float(self.cfg["max_spread_pct"]),
            float(self.cfg[f"{type_}_min_annual_yield"]), min_strike=min_strike, max_strike=max_strike,
            support=tech.support if (tech and type_ == "put" and self.cfg["prefer_support"]) else None,
            expiry_before=before, otm_pct_if_no_delta=float(self.cfg["otm_pct_if_no_delta"]),
        )
        if not q:
            return None, why, ""
        detail = f"target delta {delta:.2f}" + (f" ({'; '.join(reasons)})" if reasons else "") + note
        return q, "", detail

    def _sell_put(self, sym, spot, tech, account, reserved) -> float:
        n = int(self.cfg["contracts_per_symbol"])
        q, why, detail = self._select(sym, "put", spot, tech, max_strike=spot)
        if not q:
            log.info("Wheel %s: no put sold, %s", sym, why)
            return 0.0
        needed = q.strike * 100 * n
        cap = account.equity * float(self.cfg["max_cash_pct"])
        if needed > account.options_buying_power:
            log.info("Wheel %s: need $%.0f to secure put, only $%.0f available", sym, needed, account.options_buying_power)
            return 0.0
        if reserved + needed > cap:
            log.info("Wheel %s: would exceed max_cash_pct cap ($%.0f of $%.0f)", sym, reserved + needed, cap)
            return 0.0
        price = sell_price(q)
        coid = f"wh-put-{q.symbol}-{self.now:%Y%m%d%H%M}"
        self.broker.submit_option_limit(q.symbol, n, "sell", price, "sell_to_open", coid)
        d = f", delta {abs(q.delta):.2f}" if q.delta is not None else ""
        self._alert(
            f"🟢 Selling {n}x {sym} ${q.strike:g} put exp {q.expiry:%d %b} from ${price:.2f}{d} "
            f"(stock ${spot:.2f}, premium ${price * 100 * n:.0f}, {annual_yield(q, q.strike, self.today):.0%}/yr "
            f"on ${needed:,.0f} cash; {detail})", "wh-put", q.symbol, underlying=sym,
        )
        account.options_buying_power -= needed
        return needed

    def _sell_call(self, sym, contracts, shares) -> None:
        spot = self.broker.latest_price(sym)
        tech = self.tech(sym)
        floor = self._call_floor(sym)
        q, why, detail = self._select(sym, "call", spot, tech, min_strike=max(spot, floor or 0))
        if not q:
            log.info("Wheel %s: no covered call sold (cost basis $%.2f), %s", sym, floor or 0, why)
            return
        price = sell_price(q)
        coid = f"wh-call-{q.symbol}-{self.now:%Y%m%d%H%M}"
        self.broker.submit_option_limit(q.symbol, contracts, "sell", price, "sell_to_open", coid)
        self._alert(
            f"🔵 Selling {contracts}x {sym} ${q.strike:g} covered call exp {q.expiry:%d %b} from ${price:.2f} "
            f"(stock ${spot:.2f}, adjusted cost basis ${floor or 0:.2f}; {detail})", "wh-call", q.symbol, underlying=sym,
        )

    # ---- order handling & alerts --------------------------------------------
    def _orders_today(self) -> list[dict]:
        if self._today_orders is None:
            try:
                self._today_orders = self.broker.orders_today("wh-")
            except Exception as exc:
                log.warning("Couldn't read today's orders: %s", exc)
                self._today_orders = []
        return self._today_orders

    def _alert(self, message: str, prefix: str, symbol: str, underlying: Optional[str] = None) -> None:
        """Telegram once per position per day; a repeat (re-selected after a cancel) goes to the log only.

        Today's orders are read at the start of the run, before anything is placed.
        """
        def same(o):
            if not o["coid"].startswith(prefix + "-"):
                return False
            if underlying and occ.is_option_symbol(o["symbol"]):
                return occ.parse(o["symbol"]).underlying == underlying
            return o["symbol"] == symbol
        if any(same(o) for o in self._orders_today()):
            log.info("(already alerted today) %s", message)
            return
        self.notify.send(message)

    def _work_orders(self, orders, now: datetime) -> None:
        """Step unfilled wheel orders towards a fill.

        Every `reprice_every_minutes`, an unfilled sell moves halfway from its
        limit to the bid (a buy: halfway to the ask), at least one tick. An order
        already at the bid (ask) that still hasn't filled is cancelled, and the
        next run picks a fresh contract. Price changes are logged, not alerted.
        """
        every = timedelta(minutes=int(self.cfg["reprice_every_minutes"]))
        for o in orders:
            if not (o.is_option and o.client_order_id.startswith("wh-")) or o.limit_price is None:
                continue
            created = o.created_at if o.created_at.tzinfo else o.created_at.replace(tzinfo=timezone.utc)
            if now - created < every:
                continue
            try:
                bid, ask = self.broker.option_quote(o.symbol)
            except Exception as exc:
                log.warning("No quote for %s: %s", o.symbol, exc)
                continue
            limit = o.limit_price
            tick = 0.05 if limit >= 3 else 0.01
            if o.side == "sell":
                if bid <= 0:
                    continue
                new = max(bid, round_to_tick(limit - max(tick, (limit - bid) / 2), up=False))
                stuck = limit <= bid
            else:
                if ask <= 0:
                    continue
                new = min(ask, round_to_tick(limit + max(tick, (ask - limit) / 2), up=True))
                stuck = limit >= ask
            if stuck or new == limit:
                log.info("Wheel: %s order on %s at %.2f unfilled at the %s; cancelling to re-select",
                         o.side, o.symbol, limit, "bid" if o.side == "sell" else "ask")
                self.broker.cancel_order(o.id)
                continue
            base, _, n = o.client_order_id.rpartition("-r")
            step = int(n) + 1 if n.isdigit() and base else 1
            root = base if n.isdigit() and base else o.client_order_id
            try:
                self.broker.replace_option_limit(o.id, new, f"{root}-r{step}")
                log.info("Wheel: re-priced %s %s from %.2f to %.2f (bid %.2f / ask %.2f)",
                         o.side, o.symbol, limit, new, bid, ask)
            except Exception as exc:
                log.warning("Re-pricing %s failed (%s); cancelling", o.symbol, exc)
                self.broker.cancel_order(o.id)

    def _daily_summary(self) -> None:
        orders = self._orders_today()
        fills = [o for o in orders if o["filled_qty"] > 0]
        lines = [f"🌙 End of day {self.today:%a %d %b}"]
        if fills:
            lines.append("Filled today:")
            labels = {"wh-put": "sold put", "wh-call": "sold call", "wh-bc": "bought back", "wh-tp": "bought back",
                      "wh-rc": "roll: bought back", "wh-ro": "roll: sold"}
            for o in fills:
                kind = next((v for k, v in labels.items() if o["coid"].startswith(k + "-")), o["side"])
                if occ.is_option_symbol(o["symbol"]):
                    i = occ.parse(o["symbol"])
                    what = f"{i.underlying} ${i.strike:g} {i.type} exp {i.expiry:%d %b}"
                else:
                    what = o["symbol"]
                lines.append(f"• {kind} {o['filled_qty']:g}x {what} @ ${o['price']:.2f}")
        else:
            lines.append("No fills today.")
        shorts = [p for p in self._positions if p.is_option and p.qty < 0]
        reserved = sum(occ.parse(p.symbol).strike * 100 * abs(p.qty) for p in shorts
                       if occ.parse(p.symbol).type == "put")
        lines.append(f"Open contracts: {len(shorts)} · cash backing puts ${reserved:,.0f}")
        self.notify.send("\n".join(lines))
