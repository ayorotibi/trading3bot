"""Politician trades scraped from capitoltrades.com (free, no API key).

Capitol Trades has no official API, so this reads the public trades page.
If they change their page layout this parser may need updating; the Quiver
source (quiver.py) is the more reliable paid alternative.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import date, datetime, timedelta
from typing import Optional

import requests
from bs4 import BeautifulSoup

from ..models import Disclosure

log = logging.getLogger(__name__)

BASE_URL = "https://www.capitoltrades.com/trades"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; personal-trading-bot/1.0)",
    "Accept": "text/html,application/xhtml+xml",
}
_DATE_RE = re.compile(r"(\d{1,2})\s*([A-Za-z]{3})[a-z]*\s*(20\d{2})")
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def parse_date(text: str, today: date) -> Optional[date]:
    t = (text or "").strip().lower()
    if not t:
        return None
    if t.startswith("today"):
        return today
    if t.startswith("yesterday"):
        return today - timedelta(days=1)
    m = _DATE_RE.search(text)
    if not m:
        return None
    day, mon, year = m.groups()
    month = _MONTHS.get(mon.lower()[:3])
    if not month:
        return None
    try:
        return date(int(year), month, int(day))
    except ValueError:
        return None


def parse_trades_html(html: str, today: date) -> list[Disclosure]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for row in soup.select("tbody tr"):
        cells = row.find_all("td")
        if len(cells) < 7:
            continue
        name_el = row.select_one(".politician-name a, .politician-name, .politician a")
        ticker_el = row.select_one(".issuer-ticker")
        tx_el = row.select_one(".tx-type")
        if not (name_el and ticker_el and tx_el):
            continue

        ticker = ticker_el.get_text(strip=True).upper()
        if ":" in ticker:
            ticker, market = ticker.split(":", 1)
            if market not in ("US", ""):
                continue
        if not ticker or ticker in ("N/A", "--"):
            continue

        tx = tx_el.get_text(strip=True).lower()
        side = "buy" if tx.startswith("buy") else "sell" if tx.startswith("sell") else None
        if side is None:  # exchanges, receives, etc.
            continue

        # Column order on the site: politician, issuer, published, traded, filed after, owner, type, size, price
        published = parse_date(cells[2].get_text(" ", strip=True), today)
        traded = parse_date(cells[3].get_text(" ", strip=True), today)

        link = row.select_one('a[href*="/trades/"]')
        trade_id = None
        if link:
            m = re.search(r"/trades/(\d+)", link.get("href", ""))
            trade_id = m.group(1) if m else None
        politician = name_el.get_text(strip=True)
        if not trade_id:
            raw = f"{politician}|{ticker}|{side}|{traded}|{published}"
            trade_id = hashlib.sha1(raw.encode()).hexdigest()[:16]

        size_el = row.select_one(".trade-size")
        out.append(
            Disclosure(
                source_id=f"capitoltrades:{trade_id}",
                politician=politician,
                ticker=ticker.replace("/", "."),
                side=side,
                traded_date=traded,
                published_date=published,
                size=size_el.get_text(" ", strip=True) if size_el else "",
            )
        )
    return out


class CapitolTradesSource:
    def __init__(self, politicians: list[dict], pages: int = 1, timeout: int = 20):
        self.politicians = politicians
        self.pages = pages
        self.timeout = timeout

    def fetch(self, today: Optional[date] = None) -> list[Disclosure]:
        today = today or datetime.utcnow().date()
        results: list[Disclosure] = []
        for p in self.politicians:
            pid = p.get("capitol_trades_id")
            if not pid:
                log.warning("Capitol Trades: %s has no capitol_trades_id, skipping", p.get("name"))
                continue
            for page in range(1, self.pages + 1):
                params = {"politician": pid, "page": page}
                resp = requests.get(BASE_URL, params=params, headers=HEADERS, timeout=self.timeout)
                resp.raise_for_status()
                trades = parse_trades_html(resp.text, today)
                log.info("Capitol Trades: %d trades for %s (page %d)", len(trades), p.get("name", pid), page)
                if not trades:
                    if page == 1:
                        log.warning("Capitol Trades returned no parsable rows for %s; the page layout may have changed", pid)
                    break
                results.extend(trades)
        return results
