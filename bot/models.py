"""Plain data objects passed between the broker wrapper and the strategies.

Keeping these independent of the Alpaca SDK makes the strategies easy to test.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional


@dataclass
class Account:
    equity: float
    cash: float
    options_buying_power: float


@dataclass
class Position:
    symbol: str
    qty: float  # negative for short positions
    avg_entry_price: float
    current_price: float
    is_option: bool


@dataclass
class OpenOrder:
    id: str
    symbol: str
    side: str  # "buy" / "sell"
    created_at: datetime
    client_order_id: str
    is_option: bool
    position_intent: Optional[str] = None
    limit_price: Optional[float] = None


@dataclass
class OptionQuote:
    symbol: str
    underlying: str
    type: str  # "call" / "put"
    strike: float
    expiry: date
    bid: float
    ask: float
    delta: Optional[float] = None
    iv: Optional[float] = None  # implied volatility, e.g. 0.45 = 45%/yr

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


@dataclass
class Disclosure:
    """One publicly disclosed trade by a politician."""

    source_id: str
    politician: str
    ticker: str
    side: str  # "buy" / "sell"
    traded_date: Optional[date]
    published_date: Optional[date]
    size: str = ""
    extra: dict = field(default_factory=dict)
