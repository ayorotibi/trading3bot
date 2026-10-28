"""Follow the crowd: trade the stocks members of Congress are buying.

Once a day:
  1. Read every politician's disclosed buys and sells from the last
     `lookback_days` (default 30) from Capitol Trades.
  2. Score each stock. Every different politician buying it adds points
     (bigger trades add more); every politician selling it takes points away.
  3. Hold the `top_n` (default 10) best-scoring stocks that at least
     `min_buyers` different politicians bought, with `budget_pct` of the
     account split equally between them.
  4. Sell a holding when it drops below rank `hold_rank`, or more
     politicians are selling it than buying.

The ranked list is saved to an Alpaca watchlist ("Congress Picks") so the
wheel strategy can use it on every 5-minute run, and you can see it in the
Alpaca app.

Only shares this strategy bought (orders tagged "cr-") are ever sold by it,
so wheel shares and anything you buy yourself are left alone.

Disclosures arrive up to 45 days after the politician traded, so this always
buys after they did.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from ..models import Disclosure

log = logging.getLogger(__name__)

PREFIX = "cr-"
WATCHLIST = "Congress Picks"

DEFAULTS = {
    "source": "capitol_trades",
    "lookback_days": 30,
    "max_trade_age_days": 90,
    "min_buyers": 2,
    "top_n": 10,
    "hold_rank": 15,
    "budget_pct": 0.30,
    "min_price": 5.0,
    "exclude": [],
    "rebalance_time_utc": "15:00",
    "capitol_trades_max_pages": 60,
}

_SIZE_RE = re.compile(r"\$?\s*([\d,.]+)\s*([KkMm])?")


def size_lower_bound(size: str) -> float:
    """'15K–50K' -> 15000, '$1,001 - $15,000' -> 1001, '1M–5M' -> 1e6, '< 1K' -> 0."""
    if not size or size.strip().startswith("<"):
        return 0.0
    m = _SIZE_RE.search(size)
    if not m:
        return 0.0
    try:
        value = float(m.group(1).replace(",", ""))
    except ValueError:
        return 0.0
    unit = (m.group(2) or "").upper()
    return value * {"K": 1e3, "M": 1e6}.get(unit, 1)


def size_weight(size: str) -> float:
    """1 point for a small trade, ~2.2 for $15K+, ~3 for $100K+, 4 for $1M+, max 5."""
    lower = size_lower_bound(size)
    if lower < 1000:
        return 1.0
    return min(5.0, 1.0 + math.log10(lower / 1000))


@dataclass
class Pick:
    ticker: str
    score: float
    buyers: list[str] = field(default_factory=list)
    sellers: list[str] = field(default_factory=list)


def rank(disclosures: list[Disclosure], today: date, lookback_days: int, max_trade_age_days: int,
         exclude: Optional[set] = None) -> list[Pick]:
    exclude = {e.upper() for e in (exclude or set())}
    published_cutoff = today - timedelta(days=lookback_days)
    traded_cutoff = today - timedelta(days=max_trade_age_days)
    buys: dict[str, dict[str, float]] = {}
    sells: dict[str, dict[str, float]] = {}
    for d in disclosures:
        when = d.published_date or d.traded_date
        if when is None or when < published_cutoff:
            continue
        if d.traded_date and d.traded_date < traded_cutoff:
            continue
        t = d.ticker.upper()
        if t in exclude:
            continue
        book = buys if d.side == "buy" else sells
        per_pol = book.setdefault(t, {})
        per_pol[d.politician] = max(per_pol.get(d.politician, 0), size_weight(d.size))

    picks = []
    for t in set(buys) | set(sells):
        b, s = buys.get(t, {}), sells.get(t, {})
        picks.append(Pick(t, round(sum(b.values()) - sum(s.values()), 3), sorted(b), sorted(s)))
    picks.sort(key=lambda p: (-p.score, -len(p.buyers), p.ticker))
    return picks


class CongressStrategy:
    name = "congress"

    def __init__(self, broker, config: dict, notifier, source):
        self.broker = broker
        self.cfg = {**DEFAULTS, **(config or {})}
        self.notify = notifier
        self.source = source

    def due(self, now: datetime) -> bool:
        hh, mm = (int(x) for x in str(self.cfg["rebalance_time_utc"]).split(":"))
        start = hh * 60 + mm
        minute = now.hour * 60 + now.minute
        if start <= minute < start + 5:
            return True
        # First ever run (no saved picks yet): don't wait until tomorrow.
        return self.broker.load_picks() is None

    def run(self, now: Optional[datetime] = None, force: bool = False) -> list[Pick]:
        now = now or datetime.now(timezone.utc)
        if not force and not self.due(now):
            log.info("Congress: next rebalance at %s UTC", self.cfg["rebalance_time_utc"])
            return []
        today = now.date()
        picks = rank(self.source.fetch(today), today, int(self.cfg["lookback_days"]),
                     int(self.cfg["max_trade_age_days"]), set(self.cfg["exclude"]))
        qualified = self._qualify(picks)
        self.broker.save_picks([p.ticker for p in qualified[:30]])
        self._rebalance(qualified, today)
        return qualified

    # ------------------------------------------------------------------
    def _qualify(self, picks: list[Pick]) -> list[Pick]:
        """Keep stocks with enough buyers, net buying, tradable on Alpaca and above min_price."""
        out = []
        for p in picks:
            if p.score <= 0 or len(p.buyers) < int(self.cfg["min_buyers"]):
                continue
            asset = self.broker.asset_info(p.ticker)
            if not asset or not asset["tradable"]:
                continue
            try:
                price = self.broker.latest_price(p.ticker)
            except Exception:
                continue
            if price < float(self.cfg["min_price"]):
                continue
            out.append(p)
            if len(out) >= max(int(self.cfg["hold_rank"]), 30):
                break
        return out

    def _rebalance(self, qualified: list[Pick], today: date) -> None:
        top_n, hold_rank = int(self.cfg["top_n"]), int(self.cfg["hold_rank"])
        ranks = {p.ticker: i + 1 for i, p in enumerate(qualified)}
        held = {s: q for s, q in self.broker.strategy_holdings(PREFIX).items() if q > 1e-6}
        positions = {p.symbol: p for p in self.broker.positions() if not p.is_option}
        stamp = f"{today:%Y%m%d}"
        lines = []

        # Sells first, to free up cash
        for sym, qty in held.items():
            if ranks.get(sym, 10**6) <= hold_rank:
                continue
            pos = positions.get(sym)
            sell_qty = round(min(qty, pos.qty if pos else 0), 9)
            if sell_qty <= 0:
                continue
            coid = f"{PREFIX}sell-{sym}-{stamp}"
            if self.broker.order_exists(coid):
                continue
            self.broker.submit_stock_market(sym, "sell", coid, qty=sell_qty)
            reason = (f"dropped to rank {ranks[sym]}" if sym in ranks
                      else "no longer net-bought by politicians")
            lines.append(f"SELL {sell_qty:g} {sym} ({reason})")

        equity = self.broker.account().equity
        per_stock = equity * float(self.cfg["budget_pct"]) / top_n
        holding_after = [s for s in held if ranks.get(s, 10**6) <= hold_rank]
        for p in qualified[:top_n]:
            if p.ticker in holding_after:
                continue
            if len(holding_after) >= top_n:
                break
            coid = f"{PREFIX}buy-{p.ticker}-{stamp}"
            if self.broker.order_exists(coid):
                holding_after.append(p.ticker)
                continue
            asset = self.broker.asset_info(p.ticker) or {}
            if asset.get("fractionable"):
                self.broker.submit_stock_market(p.ticker, "buy", coid, notional=per_stock)
                amount = f"${per_stock:,.0f}"
            else:
                qty = int(per_stock // self.broker.latest_price(p.ticker))
                if qty < 1:
                    continue
                self.broker.submit_stock_market(p.ticker, "buy", coid, qty=qty)
                amount = f"{qty} shares"
            holding_after.append(p.ticker)
            lines.append(f"BUY {amount} {p.ticker} (bought by {len(p.buyers)}: {', '.join(p.buyers[:4])}"
                         f"{'…' if len(p.buyers) > 4 else ''})")

        top = "\n".join(
            f"{i}. {p.ticker}  score {p.score:g}  ({len(p.buyers)} buying, {len(p.sellers)} selling)"
            for i, p in enumerate(qualified[:top_n], start=1)
        ) or "(no stock had enough politicians buying)"
        trades = "\n".join(lines) or "No changes today"
        self.notify.send(f"🏛️ Congress picks for {today:%d %b}:\n{top}\n\nTrades:\n{trades}")
