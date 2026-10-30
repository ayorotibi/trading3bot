"""Congressional trades from the Disclosed Capitol API (https://www.disclosedcapitol.com/developers).

Set DISCLOSED_CAPITOL_API_KEY. Billing is in credits: 15 per call + 1 per
row returned. The Core plan ($6/month) has 7,500 credits a month, so the
picker refreshes Congress data once a week (about 1,000-1,600 credits) and
reuses that ranking on the other days.

Two calls per refresh:
  GET /trades?days=30            every disclosed trade in the last 30 days
  GET /politicians               each member's aggregate stats, including
                                 alpha (performance vs the market), used to
                                 weight successful traders more
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from datetime import date, datetime
from typing import Optional

import requests

from ..models import Disclosure

log = logging.getLogger(__name__)

API = "https://api.disclosedcapitol.com"
PAGE = 1000
PAGE_SIZES = (1000, 500, 250, 100, 50)


def _date(value) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


def _num(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_trades(rows: list[dict]) -> list[Disclosure]:
    out = []
    for r in rows:
        kind = str(r.get("trade_type") or "").lower()
        side = "buy" if kind in ("buy", "purchase") else "sell" if kind.startswith(("sell", "sale")) else None
        ticker = str(r.get("ticker") or "").upper().strip()
        asset = str(r.get("asset_type") or "").lower()
        if side is None or not ticker or ticker in ("N/A", "--"):
            continue
        if asset and not any(a in asset for a in ("stock", "equity", "share", "etf")):
            continue  # bonds, options, funds, etc.
        name = str(r.get("politician_name") or r.get("politician_id") or "")
        low = _num(r.get("amount_low"))
        size = str(r.get("amount_range") or (f"${low:,.0f}" if low else ""))
        rid = r.get("id") or hashlib.sha1(f"{name}|{ticker}|{side}|{r.get('transaction_date')}".encode()).hexdigest()[:16]
        out.append(
            Disclosure(
                source_id=f"dc:{rid}",
                politician=name,
                ticker=ticker.replace("/", "."),
                side=side,
                traded_date=_date(r.get("transaction_date")),
                published_date=_date(r.get("disclosure_date")),
                size=size,
                extra={"politician_id": r.get("politician_id")},
            )
        )
    return out


def _pick(d: dict, words: tuple, avoid: tuple = ()) -> Optional[float]:
    """First numeric field whose name contains all `words` (and none of `avoid`)."""
    for k in sorted(d):
        lk = k.lower()
        if all(w in lk for w in words) and not any(a in lk for a in avoid):
            v = _num(d[k])
            if v is not None:
                return v
    return None


def politician_weights(rows: list[dict], weight_per_alpha: float = 5.0, min_trades: int = 5,
                       shrink: float = 10.0, min_w: float = 0.25, max_w: float = 3.0) -> dict[str, float]:
    """Weight each member by their alpha (return vs the market) from Disclosed Capitol.

    Alpha may be given as a fraction (0.12) or a percentage (12.0); values
    above 1.5 in size are treated as percentages. Few trades -> pulled to 1.0.
    """
    out = {}
    for r in rows:
        name = str(r.get("name") or r.get("politician_name") or r.get("full_name") or "")
        if not name:
            continue
        alpha = _pick(r, ("alpha",), avoid=("rank", "percentile"))
        trades = _pick(r, ("trade",), avoid=("last", "date", "days"))
        if alpha is None:
            continue
        if abs(alpha) > 1.5:
            alpha /= 100.0
        if trades is not None and trades < min_trades:
            w = 1.0
        else:
            n = trades if trades is not None else shrink
            w = round(min(max_w, max(min_w, 1.0 + weight_per_alpha * alpha * n / (n + shrink))), 3)
        out[name] = w
        pid = r.get("id") or r.get("politician_id")
        if pid is not None:
            out[f"id:{pid}"] = w
    return out


class DisclosedCapitolSource:
    weekly = True

    def __init__(self, api_key: Optional[str] = None, days: int = 30, refresh_days: int = 7,
                 timeout: int = 30, track_record: Optional[dict] = None):
        self.key = api_key if api_key is not None else os.getenv("DISCLOSED_CAPITOL_API_KEY", "").strip()
        self.days = days
        self.refresh_days = refresh_days
        self.timeout = timeout
        self.track_cfg = track_record or {}
        self.credits_left: Optional[int] = None
        self.partial = False
        self._sleep = time.sleep

    def _request(self, path: str, params: dict):
        for attempt in range(3):
            resp = requests.get(f"{API}{path}", headers={"DC-API-Key": self.key}, params=params, timeout=self.timeout)
            if resp.status_code == 429 and attempt < 2:
                ra = resp.headers.get("Retry-After", "")
                self._sleep(min(float(ra) if ra.isdigit() else 30, 120))
                continue
            break
        if resp.status_code == 402:
            raise RuntimeError("Disclosed Capitol: out of credits (HTTP 402)")
        if resp.status_code == 403:
            raise RuntimeError("Disclosed Capitol: API key missing or invalid (HTTP 403)")
        return resp

    def _get(self, path: str, params: dict) -> list[dict]:
        """All rows, paging with limit/offset.

        Endpoints allow different page sizes, and a rejected size returns 422
        (failed calls cost no credits), so it steps down until one is accepted.
        If every size is rejected it tries once with no paging parameters.
        """
        sizes = [s for s in PAGE_SIZES if s <= PAGE]
        out, offset, i = [], 0, 0
        while True:
            if i < len(sizes):
                size = sizes[i]
                resp = self._request(path, {**params, "limit": size, "offset": offset})
            else:
                size = None
                resp = self._request(path, params)
            if resp.status_code == 422 and size is not None and offset == 0:
                log.info("Disclosed Capitol %s: page size %d rejected (%s), trying smaller",
                         path, size, getattr(resp, "text", "")[:200])
                i += 1
                continue
            resp.raise_for_status()
            left = resp.headers.get("X-Credits-Remaining")
            if left and left.lstrip("-").isdigit():
                self.credits_left = int(left)
            page = resp.json()
            if isinstance(page, dict):  # tolerate a wrapped response
                page = page.get("data") or page.get("results") or page.get("politicians") or []
            out.extend(page)
            if size is None or len(page) < size:
                return out
            offset += size

    def fetch(self, today: Optional[date] = None) -> list[Disclosure]:
        if not self.key:
            raise RuntimeError("DISCLOSED_CAPITOL_API_KEY is not set")
        rows = self._get("/trades", {"days": self.days})
        trades = parse_trades(rows)
        log.info("Disclosed Capitol: %d trade rows, %d stock buys/sells (credits left: %s)",
                 len(rows), len(trades), self.credits_left)
        return trades

    def politician_weights(self) -> dict[str, float]:
        if not self.key:
            return {}
        rows = self._get("/politicians", {})
        if rows:
            log.info("Disclosed Capitol politician fields: %s", sorted(rows[0].keys()))
        cfg = {k: v for k, v in self.track_cfg.items()
               if k in ("weight_per_alpha", "min_trades", "shrink", "min_w", "max_w")}
        weights = politician_weights(rows, **cfg)
        log.info("Disclosed Capitol: weights for %d politicians (credits left: %s)", len(weights), self.credits_left)
        return weights
