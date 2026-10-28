"""Congress signal: score stocks by what members of Congress are buying.

Each different politician buying a stock in the last `lookback_days` adds
(trade-size points x that politician's track-record weight); each one
selling subtracts the same way. Used by the stock picker (picker.py).
Disclosures arrive up to 45 days after the trade, which is why the picker
blends this with fresher sources.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from ..models import Disclosure

log = logging.getLogger(__name__)


DEFAULTS = {
    "enabled": True,
    "source": "capitol_trades",
    "lookback_days": 30,
    "max_trade_age_days": 90,
    "history_days": 365,
    "use_track_records": True,
    "min_buyers": 2,
    "capitol_trades_max_pages": 200,
    "track_record": {},
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
