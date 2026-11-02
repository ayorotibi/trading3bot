"""Upcoming events that change how the wheel trades a stock.

* Earnings dates, from Finnhub's free earnings calendar (set FINNHUB_API_KEY).
  Without a key the bot can't see earnings and says so in the logs.
* Ex-dividend dates, from Alpaca (see broker.upcoming_dividends).
* Blackout dates you list in config.yaml (e.g. Fed meetings), on which no
  new positions are opened.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from typing import Optional

import requests

log = logging.getLogger(__name__)

FINNHUB_URL = "https://finnhub.io/api/v1/calendar/earnings"


class EarningsCalendar:
    def __init__(self, api_key: Optional[str] = None, days_ahead: int = 60, timeout: int = 15):
        self.key = api_key if api_key is not None else os.getenv("FINNHUB_API_KEY", "").strip()
        self.days_ahead = days_ahead
        self.timeout = timeout
        self._cache: dict[str, Optional[date]] = {}
        self._warned = False
        self.failed: set[str] = set()   # symbols whose lookup errored (date unknown)
        self.last_error: Optional[str] = None

    @property
    def available(self) -> bool:
        return bool(self.key)

    def next_earnings(self, symbol: str, today: Optional[date] = None) -> Optional[date]:
        """Next earnings date within days_ahead, or None (none scheduled, or no data)."""
        if symbol in self._cache:
            return self._cache[symbol]
        if not self.key:
            if not self._warned:
                log.warning("FINNHUB_API_KEY not set: earnings dates unknown, the wheel can't avoid earnings")
                self._warned = True
            return None
        today = today or date.today()
        try:
            resp = requests.get(
                FINNHUB_URL,
                params={"from": (today - timedelta(days=1)).isoformat(),
                        "to": (today + timedelta(days=self.days_ahead)).isoformat(),
                        "symbol": symbol, "token": self.key},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            dates = []
            for row in resp.json().get("earningsCalendar") or []:
                if (row.get("symbol") or "").upper() == symbol.upper() and row.get("date"):
                    dates.append(datetime.strptime(row["date"], "%Y-%m-%d").date())
            result = min((d for d in dates if d >= today), default=None)
        except Exception as exc:
            msg = str(exc).replace(self.key, "***") if self.key else str(exc)  # never leak the key
            log.warning("Earnings lookup for %s failed: %s", symbol, msg)
            self.failed.add(symbol)
            self.last_error = msg[:150]
            result = None
        self._cache[symbol] = result
        return result

    def health_check(self) -> Optional[str]:
        """None if the calendar works, else a short problem description for Telegram.

        Apple reports every quarter, so a 120-day window must contain a date.
        """
        if not self.key:
            return "FINNHUB_API_KEY is not set: the wheel can't see earnings dates"
        probe = EarningsCalendar(self.key, days_ahead=120, timeout=self.timeout)
        found = probe.next_earnings("AAPL")
        if probe.failed:
            return f"Earnings calendar error ({probe.last_error})"
        if found is None:
            return "Earnings calendar returned no dates (check FINNHUB_API_KEY)"
        return None


class StaticEarnings:
    """Fixed earnings dates (used in tests)."""

    def __init__(self, dates: Optional[dict[str, date]] = None):
        self.dates = dates or {}
        self.available = True
        self.failed: set[str] = set()

    def health_check(self):
        return None

    def next_earnings(self, symbol: str, today: Optional[date] = None) -> Optional[date]:
        return self.dates.get(symbol)
