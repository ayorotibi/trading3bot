"""Congress picks: use what members of Congress are buying to choose stocks.

This is a stock-picker for the wheel, not a trading strategy of its own
(unless you turn on buy_stocks). Every trading day:

  1. Read a year of every politician's disclosed trades from Capitol Trades.
  2. Build each politician's track record (politicians.py): how their buys
     did against the S&P 500 from the day they were disclosed. Successful
     traders get a higher weight, unsuccessful ones a lower weight.
  3. Score each stock from the last `lookback_days` (default 30): each
     different politician buying it adds (trade size points x their weight);
     each politician selling it subtracts the same way.
  4. Keep stocks with at least `min_buyers` different politicians buying,
     net buying, tradable on Alpaca, above `min_price`.
  5. Save the ranked list to an Alpaca watchlist ("Congress Picks"). The
     wheel reads it on every 5-minute run and wheels the best ones.

Optional (buy_stocks: true): also hold the top `top_n` stocks with
`budget_pct` of the account. Off by default.

Disclosures arrive up to 45 days after the politician traded.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from ..models import Disclosure
from .politicians import DEFAULTS as TRACK_DEFAULTS
from .politicians import PriceHistory, TrackRecord, top_traders, track_records

log = logging.getLogger(__name__)

PREFIX = "cr-"
WATCHLIST = "Congress Picks"

DEFAULTS = {
    "source": "capitol_trades",
    "lookback_days": 30,
    "max_trade_age_days": 90,
    "history_days": 365,
    "use_track_records": True,
    "min_buyers": 2,
    "min_price": 5.0,
    "exclude": [],
    "keep_picks": 30,
    "capitol_trades_max_pages": 200,
    "track_record": {},
    # optional direct stock buying (off by default: the wheel does the trading)
    "buy_stocks": False,
    "top_n": 10,
    "hold_rank": 15,
    "budget_pct": 0.30,
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
         exclude: Optional[set] = None, weights: Optional[dict[str, float]] = None) -> list[Pick]:
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
        w = size_weight(d.size) * (weights or {}).get(d.politician, 1.0)
        per_pol[d.politician] = max(per_pol.get(d.politician, 0), w)

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
        """Refresh once a day: on the first top-of-the-hour run with yesterday's (or no) picks.

        If the refresh fails it is retried at the next hour, not every 5 minutes.
        """
        updated = self.broker.picks_updated_on()
        if updated is None:
            return True
        return updated < now.date() and now.minute < 5

    def run(self, now: Optional[datetime] = None, force: bool = False) -> list[Pick]:
        now = now or datetime.now(timezone.utc)
        if not force and not self.due(now):
            log.info("Congress: today's picks are up to date (%s)", (self.broker.load_picks() or [])[:10])
            return []
        today = now.date()
        disclosures = self.source.fetch(today)

        records: dict[str, TrackRecord] = {}
        if self.cfg["use_track_records"]:
            records = self._track_records(disclosures, today)
        weights = {name: r.weight for name, r in records.items()}

        picks = rank(disclosures, today, int(self.cfg["lookback_days"]), int(self.cfg["max_trade_age_days"]),
                     set(self.cfg["exclude"]), weights)
        qualified = self._qualify(picks)
        self.broker.save_picks([p.ticker for p in qualified[: int(self.cfg["keep_picks"])]])

        lines = self._rebalance(qualified, today) if self.cfg["buy_stocks"] else []
        self._summary(qualified, records, lines, today)
        return qualified

    def _track_records(self, disclosures: list[Disclosure], today: date) -> dict[str, TrackRecord]:
        cfg = {**TRACK_DEFAULTS, **(self.cfg.get("track_record") or {})}
        start = today - timedelta(days=int(self.cfg["history_days"]) + 10)
        tickers = sorted({d.ticker for d in disclosures if d.side == "buy"} | {"SPY"})
        try:
            closes = self.broker.daily_closes(tickers, start)
        except Exception as exc:
            log.warning("Couldn't load price history, ranking without track records: %s", exc)
            return {}
        records = track_records(disclosures, PriceHistory(closes), today, cfg)
        judged = sum(1 for r in records.values() if r.scored_trades >= int(cfg["min_scored_trades"]))
        log.info("Track records: %d politicians, %d with enough trades to judge", len(records), judged)
        return records

    def _summary(self, qualified, records, lines, today) -> None:
        top = "\n".join(
            f"{i}. {p.ticker}  score {p.score:g}  ({len(p.buyers)} buying, {len(p.sellers)} selling)"
            for i, p in enumerate(qualified[:10], start=1)
        ) or "(no stock had enough politicians buying)"
        traders = "\n".join(
            f"• {r.politician}: {r.avg_excess:+.1%} vs S&P on avg, beat it {r.hit_rate:.0%} of "
            f"{r.scored_trades} buys, weight {r.weight:g}x"
            for r in top_traders(records)
        )
        msg = f"🏛️ Congress picks for {today:%d %b} (the wheel picks from these):\n{top}"
        if traders:
            msg += f"\n\nTop traders by track record:\n{traders}"
        if self.cfg["buy_stocks"]:
            msg += "\n\nStock trades:\n" + ("\n".join(lines) or "No changes today")
        self.notify.send(msg)

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
            if len(out) >= max(int(self.cfg["hold_rank"]), int(self.cfg["keep_picks"])):
                break
        return out

    def _rebalance(self, qualified: list[Pick], today: date) -> list[str]:
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

        return lines
