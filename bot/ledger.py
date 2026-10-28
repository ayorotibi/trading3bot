"""Wheel ledger: cost basis and profit for each stock, across every cycle.

Rebuilt on demand from Alpaca's account activities (no database):
  FILL   option fills (premium received or paid) and stock fills
  OPASN  an option of yours was assigned
  OPEXC  an option was exercised
  OPEXP  an option expired worthless
  OPTRD  the share trade caused by an assignment or exercise

A "campaign" on a stock starts when the wheel first trades it while you hold
nothing in it, and ends when you hold no shares and no options in it again
(the puts expired, or the shares were called away). For the current
campaign:

  adjusted cost basis per share
      = (cost of the shares you hold - net premium collected this campaign)
        / shares held

e.g. assigned 100 shares at $50 after collecting $1.20 on the put, then
$0.80 on a covered call: adjusted basis = $50 - $2.00 = $48.00. Covered calls
are never sold below this, so being called away still nets a profit for the
campaign as a whole.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from . import occ

log = logging.getLogger(__name__)


@dataclass
class Campaign:
    ticker: str
    start: Optional[date] = None
    end: Optional[date] = None
    premium: float = 0.0      # net option premium, dollars
    stock_pnl: float = 0.0    # realised gain/loss on shares, dollars
    puts_sold: int = 0
    calls_sold: int = 0
    assignments: int = 0

    @property
    def profit(self) -> float:
        return self.premium + self.stock_pnl


@dataclass
class TickerLedger:
    ticker: str
    shares: float = 0.0
    share_cost: float = 0.0   # total cost of shares held
    open_options: int = 0     # net short contracts
    current: Optional[Campaign] = None
    completed: list[Campaign] = field(default_factory=list)

    @property
    def flat(self) -> bool:
        return abs(self.shares) < 1e-9 and self.open_options == 0

    @property
    def avg_cost(self) -> Optional[float]:
        return self.share_cost / self.shares if self.shares > 0 else None

    @property
    def adjusted_basis(self) -> Optional[float]:
        if self.shares <= 0:
            return None
        premium = self.current.premium if self.current else 0.0
        return (self.share_cost - premium) / self.shares

    @property
    def total_premium(self) -> float:
        return sum(c.premium for c in self.completed) + (self.current.premium if self.current else 0.0)

    @property
    def realized_profit(self) -> float:
        return sum(c.profit for c in self.completed)


def _time(a: dict) -> str:
    return str(a.get("transaction_time") or a.get("date") or "")


def _day(a: dict) -> Optional[date]:
    t = _time(a)
    try:
        return datetime.fromisoformat(t[:10]).date()
    except ValueError:
        return None


def _num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def build_ledger(activities: list[dict]) -> dict[str, TickerLedger]:
    books: dict[str, TickerLedger] = {}

    def book(ticker: str) -> TickerLedger:
        return books.setdefault(ticker, TickerLedger(ticker))

    def touch(b: TickerLedger, day):
        if b.current is None:
            b.current = Campaign(b.ticker, start=day)

    def maybe_close(b: TickerLedger, day):
        if b.flat and b.current is not None:
            b.current.end = day
            b.completed.append(b.current)
            b.current = None
            b.share_cost = 0.0

    def shares_change(b: TickerLedger, qty: float, price: float):
        if qty > 0:
            b.shares += qty
            b.share_cost += qty * price
        elif qty < 0:
            sold = min(-qty, b.shares) if b.shares > 0 else -qty
            avg = b.avg_cost or price
            if b.current:
                b.current.stock_pnl += (price - avg) * sold
            b.share_cost -= avg * sold
            b.shares += qty

    for a in sorted(activities, key=_time):
        kind = a.get("activity_type")
        sym = a.get("symbol") or ""
        day = _day(a)
        qty = _num(a.get("qty"))
        is_opt = occ.is_option_symbol(sym)
        ticker = occ.parse(sym).underlying if is_opt else sym
        if not ticker:
            continue
        b = book(ticker)

        if kind == "FILL":
            side = (a.get("side") or "").lower()
            price = _num(a.get("price"))
            if is_opt:
                touch(b, day)
                sign = 1 if side.startswith("sell") else -1
                b.current.premium += sign * price * 100 * abs(qty)
                if sign > 0:
                    b.open_options += int(abs(qty))
                    if occ.parse(sym).type == "put":
                        b.current.puts_sold += int(abs(qty))
                    else:
                        b.current.calls_sold += int(abs(qty))
                else:
                    b.open_options -= int(abs(qty))
            else:
                if b.current is None and side.startswith("sell"):
                    continue  # selling shares the wheel never tracked
                touch(b, day)
                shares_change(b, abs(qty) if side == "buy" else -abs(qty), price)
            maybe_close(b, day)
        elif kind in ("OPASN", "OPEXC", "OPEXP") and is_opt:
            touch(b, day)
            b.open_options -= int(abs(qty))
            if kind == "OPASN":
                b.current.assignments += 1
            if kind == "OPEXP":  # assignment/exercise are followed by an OPTRD share trade
                maybe_close(b, day)
        elif kind == "OPTRD" and not is_opt:
            touch(b, day)
            shares_change(b, qty, _num(a.get("price")))
            maybe_close(b, day)
    return books


class Ledger:
    """Lazy, cached ledger for one bot run."""

    def __init__(self, broker):
        self.broker = broker
        self._books: Optional[dict[str, TickerLedger]] = None

    @property
    def books(self) -> dict[str, TickerLedger]:
        if self._books is None:
            try:
                self._books = build_ledger(self.broker.account_activities())
            except Exception as exc:
                log.warning("Couldn't build the wheel ledger: %s", exc)
                self._books = {}
        return self._books

    def adjusted_basis(self, ticker: str) -> Optional[float]:
        b = self.books.get(ticker)
        return b.adjusted_basis if b else None
