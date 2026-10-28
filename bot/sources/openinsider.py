"""Company insiders buying their own stock (SEC Form 4), via openinsider.com.

Officers and directors must report open-market purchases within 2 business
days, so this is much fresher than congressional disclosures. OpenInsider
publishes the SEC data as tables; this reads its screener for purchases
("P - Purchase") above a minimum value. There's no official API, so if the
page layout changes this source returns nothing and logs a warning; the
other sources keep working.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

URL = "http://openinsider.com/screener"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; personal-trading-bot/1.0)"}


@dataclass
class InsiderBuy:
    ticker: str
    insider: str
    title: str
    filing_date: Optional[date]
    trade_date: Optional[date]
    value: float


def _date(text: str) -> Optional[date]:
    m = re.search(r"(\d{4}-\d{2}-\d{2})", text or "")
    return datetime.strptime(m.group(1), "%Y-%m-%d").date() if m else None


def _money(text: str) -> float:
    t = (text or "").replace("$", "").replace(",", "").replace("+", "").strip()
    try:
        return abs(float(t))
    except ValueError:
        return 0.0


def _norm(h: str) -> str:
    return re.sub(r"[^a-z]", "", h.replace("\xa0", " ").lower())


def parse_screener_html(html: str) -> list[InsiderBuy]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.tinytable")
    if table is None:
        return []
    headers = [_norm(th.get_text(" ", strip=True)) for th in table.select("thead th")]
    if not headers:
        first = table.find("tr")
        headers = [_norm(c.get_text(" ", strip=True)) for c in first.find_all(["th", "td"])] if first else []

    def col(*names):
        for n in names:
            if n in headers:
                return headers.index(n)
        return None

    i_filing, i_trade, i_ticker = col("filingdate"), col("tradedate"), col("ticker")
    i_name, i_title, i_type, i_value = col("insidername"), col("title"), col("tradetype"), col("value")
    if None in (i_ticker, i_type, i_value):
        log.warning("OpenInsider: unexpected table columns %s", headers)
        return []

    out = []
    rows = table.select("tbody tr") or table.find_all("tr")[1:]
    for tr in rows:
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(cells) < len(headers):
            continue
        if not cells[i_type].upper().startswith("P"):
            continue  # purchases only
        ticker = cells[i_ticker].upper().strip()
        if not re.fullmatch(r"[A-Z.]{1,6}", ticker):
            continue
        out.append(
            InsiderBuy(
                ticker=ticker,
                insider=cells[i_name] if i_name is not None else "",
                title=cells[i_title] if i_title is not None else "",
                filing_date=_date(cells[i_filing]) if i_filing is not None else None,
                trade_date=_date(cells[i_trade]) if i_trade is not None else None,
                value=_money(cells[i_value]),
            )
        )
    return out


class OpenInsiderSource:
    def __init__(self, lookback_days: int = 14, min_value: float = 25_000, max_rows: int = 1000, timeout: int = 30):
        self.lookback_days = lookback_days
        self.min_value = min_value
        self.max_rows = max_rows
        self.timeout = timeout

    def fetch(self, today: Optional[date] = None) -> list[InsiderBuy]:
        params = {
            "fd": self.lookback_days,      # filed in the last N days
            "xp": 1,                       # purchases
            "vl": int(self.min_value // 1000),  # minimum value, $ thousands
            "cnt": self.max_rows,
            "sortcol": 0,
            "page": 1,
        }
        resp = requests.get(URL, params=params, headers=HEADERS, timeout=self.timeout)
        resp.raise_for_status()
        buys = [b for b in parse_screener_html(resp.text) if b.value >= self.min_value]
        if not buys:
            log.warning("OpenInsider returned no purchases; the site may be blocking requests or changed layout")
        log.info("OpenInsider: %d insider purchases", len(buys))
        return buys
