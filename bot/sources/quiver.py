"""Politician trades from the Quiver Quantitative API (paid, official API).

Set QUIVER_API_TOKEN. Uses the live congress trading endpoint and filters it
to the politicians in your config by name (all politicians if none are given).
"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import date, datetime
from typing import Optional

import requests

from ..models import Disclosure

log = logging.getLogger(__name__)

URL = "https://api.quiverquant.com/beta/live/congresstrading"


def _first(row: dict, *keys):
    for k in keys:
        if row.get(k) not in (None, ""):
            return row[k]
    return None


def _date(value) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


def parse_rows(rows: list[dict], wanted_names: list[str]) -> list[Disclosure]:
    wanted = [n.lower() for n in wanted_names]
    out = []
    for row in rows:
        name = str(_first(row, "Representative", "Name", "Politician") or "")
        if wanted and not any(w in name.lower() for w in wanted):
            continue
        ticker = str(_first(row, "Ticker") or "").upper().strip()
        tx = str(_first(row, "Transaction", "TransactionType") or "").lower()
        side = "buy" if tx.startswith(("purchase", "buy")) else "sell" if tx.startswith(("sale", "sell")) else None
        if not ticker or side is None:
            continue
        traded = _date(_first(row, "TransactionDate", "Traded"))
        published = _date(_first(row, "ReportDate", "Filed", "DisclosureDate"))
        size = str(_first(row, "Range", "Amount") or "")
        raw = f"{name}|{ticker}|{side}|{traded}|{published}|{size}"
        out.append(
            Disclosure(
                source_id="quiver:" + hashlib.sha1(raw.encode()).hexdigest()[:16],
                politician=name,
                ticker=ticker,
                side=side,
                traded_date=traded,
                published_date=published,
                size=size,
            )
        )
    return out


class QuiverSource:
    def __init__(self, politicians: Optional[list[dict]] = None, token: Optional[str] = None, timeout: int = 30):
        # Empty list = every politician
        self.names = [p["name"] for p in (politicians or []) if p.get("name")]
        self.token = token or os.getenv("QUIVER_API_TOKEN", "")
        self.timeout = timeout

    def fetch(self, today: Optional[date] = None) -> list[Disclosure]:
        if not self.token:
            raise RuntimeError("QUIVER_API_TOKEN is not set")
        resp = requests.get(
            URL,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        trades = parse_rows(resp.json(), self.names)
        log.info("Quiver: %d matching trades", len(trades))
        return trades
