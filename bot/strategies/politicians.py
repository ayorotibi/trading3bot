"""Politician track records: who is active, and whose picks actually worked.

For every stock a politician BOUGHT, we measure what happened from the day it
was publicly disclosed (the earliest day anyone could have copied it) over
the next `horizon_days`, compared with the S&P 500 (SPY) over the same days.

  excess return = stock return - SPY return

Each politician gets:
  * scored_trades: buys old enough to measure (at least min_hold_days)
  * avg_excess:    average excess return of those buys
  * hit_rate:      share of buys that beat SPY
  * recent_trades: trades disclosed in the last `active_days` (activity)

and a weight used when ranking stocks. Politicians with few scored trades are
pulled towards neutral (1.0), so one lucky trade doesn't make a "top trader".
"""
from __future__ import annotations

import bisect
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from ..models import Disclosure

log = logging.getLogger(__name__)

BENCHMARK = "SPY"

DEFAULTS = {
    "horizon_days": 90,
    "min_hold_days": 30,
    "min_scored_trades": 5,
    "shrink_trades": 10,
    "weight_per_excess": 5.0,
    "min_weight": 0.25,
    "max_weight": 3.0,
    "active_days": 90,
}


@dataclass
class TrackRecord:
    politician: str
    scored_trades: int = 0
    avg_excess: float = 0.0
    hit_rate: float = 0.0
    recent_trades: int = 0
    weight: float = 1.0


class PriceHistory:
    """Daily closes per symbol: {symbol: [(date, close), ...]} sorted by date."""

    def __init__(self, closes: dict[str, list[tuple[date, float]]]):
        self.closes = {s: sorted(v) for s, v in closes.items() if v}
        self._dates = {s: [d for d, _ in v] for s, v in self.closes.items()}

    def on_or_after(self, symbol: str, day: date) -> Optional[tuple[date, float]]:
        dates = self._dates.get(symbol)
        if not dates:
            return None
        i = bisect.bisect_left(dates, day)
        return self.closes[symbol][i] if i < len(dates) else None

    def on_or_before(self, symbol: str, day: date) -> Optional[tuple[date, float]]:
        dates = self._dates.get(symbol)
        if not dates:
            return None
        i = bisect.bisect_right(dates, day) - 1
        return self.closes[symbol][i] if i >= 0 else None


def trade_excess_return(prices: PriceHistory, ticker: str, published: date, today: date,
                        horizon_days: int, min_hold_days: int) -> Optional[float]:
    if published > today - timedelta(days=min_hold_days):
        return None  # too recent to judge
    entry = prices.on_or_after(ticker, published)
    bench_entry = prices.on_or_after(BENCHMARK, published)
    if not entry or not bench_entry or entry[1] <= 0 or bench_entry[1] <= 0:
        return None
    exit_day = min(published + timedelta(days=horizon_days), today)
    exit_ = prices.on_or_before(ticker, exit_day)
    bench_exit = prices.on_or_before(BENCHMARK, exit_day)
    if not exit_ or not bench_exit or exit_[0] <= entry[0]:
        return None
    return (exit_[1] / entry[1] - 1) - (bench_exit[1] / bench_entry[1] - 1)


def track_records(disclosures: list[Disclosure], prices: PriceHistory, today: date,
                  config: Optional[dict] = None) -> dict[str, TrackRecord]:
    cfg = {**DEFAULTS, **(config or {})}
    active_cutoff = today - timedelta(days=int(cfg["active_days"]))
    results: dict[str, list[float]] = {}
    records: dict[str, TrackRecord] = {}
    seen: set[tuple] = set()

    for d in disclosures:
        rec = records.setdefault(d.politician, TrackRecord(d.politician))
        published = d.published_date or d.traded_date
        if published and published >= active_cutoff:
            rec.recent_trades += 1
        if d.side != "buy" or not published:
            continue
        key = (d.politician, d.ticker, published)  # same stock, same filing = one trade
        if key in seen:
            continue
        seen.add(key)
        r = trade_excess_return(prices, d.ticker, published, today,
                                int(cfg["horizon_days"]), int(cfg["min_hold_days"]))
        if r is not None:
            results.setdefault(d.politician, []).append(r)

    for name, rets in results.items():
        rec = records[name]
        rec.scored_trades = len(rets)
        rec.avg_excess = sum(rets) / len(rets)
        rec.hit_rate = sum(1 for r in rets if r > 0) / len(rets)
    for rec in records.values():
        rec.weight = politician_weight(rec, cfg)
    return records


def politician_weight(rec: TrackRecord, cfg: dict) -> float:
    """1.0 = neutral. Beating SPY by 10% on average -> ~1.5; lagging by 10% -> ~0.5."""
    n = rec.scored_trades
    if n < int(cfg["min_scored_trades"]):
        return 1.0
    shrunk = rec.avg_excess * n / (n + float(cfg["shrink_trades"]))
    w = 1.0 + float(cfg["weight_per_excess"]) * shrunk
    return round(min(float(cfg["max_weight"]), max(float(cfg["min_weight"]), w)), 3)


def top_traders(records: dict[str, TrackRecord], n: int = 5, min_trades: int = 5) -> list[TrackRecord]:
    judged = [r for r in records.values() if r.scored_trades >= min_trades and r.recent_trades > 0]
    return sorted(judged, key=lambda r: -r.weight)[:n]
