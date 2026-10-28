"""Market conditions worked out from daily prices (no extra data needed).

For any stock: its trend (vs 50- and 200-day averages), how much it has
moved vs the S&P 500 over 3 months, realised volatility, recent support
(lowest close of the last 20 days) and average dollar volume.

For the market as a whole (SPY): "normal" or "defensive". Defensive means
SPY is below its 50-day average or unusually volatile; the wheel then sells
further out of the money and opens fewer new positions.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Optional

TRADING_DAYS = 252


@dataclass
class Technicals:
    price: float
    sma50: Optional[float]
    sma200: Optional[float]
    return_3m: Optional[float]
    realized_vol: Optional[float]  # annualised, 20 days
    support: Optional[float]       # lowest close of the last 20 days
    avg_dollar_volume: Optional[float]

    @property
    def above_50(self) -> bool:
        return self.sma50 is not None and self.price > self.sma50

    @property
    def above_200(self) -> bool:
        return self.sma200 is not None and self.price > self.sma200

    @property
    def trend(self) -> str:
        """'up' (above both averages, 50 above 200), 'weak' (above 200 only), 'down', or 'unknown'."""
        if self.sma200 is None:
            return "up" if self.above_50 else ("unknown" if self.sma50 is None else "down")
        if self.above_50 and self.above_200 and self.sma50 >= self.sma200:
            return "up"
        if self.above_200:
            return "weak"
        return "down"

    @property
    def trend_score(self) -> float:
        return {"up": 1.0, "weak": 0.5, "unknown": 0.5, "down": 0.0}[self.trend]


def _sma(values: list[float], n: int) -> Optional[float]:
    return sum(values[-n:]) / n if len(values) >= n else None


def technicals(rows: list[tuple], spy_rows: Optional[list[tuple]] = None) -> Optional[Technicals]:
    """rows: [(date, close, volume), ...] oldest first (volume optional)."""
    if not rows:
        return None
    closes = [r[1] for r in rows]
    price = closes[-1]
    vol = None
    if len(closes) >= 21:
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - 20, len(closes)) if closes[i - 1] > 0]
        if len(rets) >= 2:
            vol = statistics.stdev(rets) * math.sqrt(TRADING_DAYS)
    r3 = None
    if len(closes) > 63 and closes[-64] > 0:
        r3 = closes[-1] / closes[-64] - 1
        if spy_rows and len(spy_rows) > 63 and spy_rows[-64][1] > 0:
            r3 -= spy_rows[-1][1] / spy_rows[-64][1] - 1
    dollar_vol = None
    if len(rows[0]) >= 3:
        recent = rows[-20:]
        dollar_vol = sum(r[1] * r[2] for r in recent) / len(recent)
    return Technicals(
        price=price,
        sma50=_sma(closes, 50),
        sma200=_sma(closes, 200),
        return_3m=r3,
        realized_vol=vol,
        support=min(closes[-20:]) if len(closes) >= 20 else None,
        avg_dollar_volume=dollar_vol,
    )


@dataclass
class Regime:
    name: str  # "normal" or "defensive"
    reason: str

    @property
    def defensive(self) -> bool:
        return self.name == "defensive"


def market_regime(spy: Optional[Technicals], high_vol: float = 0.25) -> Regime:
    if spy is None:
        return Regime("normal", "no SPY data")
    if spy.sma50 is not None and spy.price < spy.sma50:
        return Regime("defensive", f"S&P 500 below its 50-day average ({spy.price:.0f} < {spy.sma50:.0f})")
    if spy.realized_vol is not None and spy.realized_vol > high_vol:
        return Regime("defensive", f"S&P 500 volatility high ({spy.realized_vol:.0%})")
    return Regime("normal", "S&P 500 in an uptrend with normal volatility")
