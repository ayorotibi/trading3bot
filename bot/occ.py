"""Helpers for OCC option symbols, e.g. AAPL250117P00150000.

Format: ROOT (1-6 letters) + YYMMDD + C/P + strike * 1000 padded to 8 digits.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

_OCC_RE = re.compile(r"^([A-Z.]{1,6})(\d{6})([CP])(\d{8})$")


@dataclass(frozen=True)
class OptionSymbol:
    underlying: str
    expiry: date
    type: str  # "call" or "put"
    strike: float


def is_option_symbol(symbol: str) -> bool:
    return bool(_OCC_RE.match(symbol or ""))


def parse(symbol: str) -> OptionSymbol:
    m = _OCC_RE.match(symbol)
    if not m:
        raise ValueError(f"Not an OCC option symbol: {symbol!r}")
    root, yymmdd, cp, strike = m.groups()
    expiry = date(2000 + int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6]))
    return OptionSymbol(
        underlying=root,
        expiry=expiry,
        type="call" if cp == "C" else "put",
        strike=int(strike) / 1000.0,
    )


def build(underlying: str, expiry: date, type_: str, strike: float) -> str:
    cp = "C" if type_ == "call" else "P"
    return f"{underlying}{expiry:%y%m%d}{cp}{int(round(strike * 1000)):08d}"
