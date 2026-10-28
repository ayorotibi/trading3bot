"""Stock picker: decides WHICH stocks the wheel trades. Places no orders.

Once a day (first top-of-the-hour run after the market opens) it blends:

  1. Congress  - stocks members of Congress are buying, weighted by each
                 politician's track record (congress.py, politicians.py).
                 Up to 45 days old.
  2. Insiders  - company officers and directors buying their own stock
                 (SEC Form 4 via OpenInsider). Filed within 2 business days.
  3. Market    - live Alpaca price data: trend, 3-month strength vs the
                 S&P 500, and trading volume. Also adds Alpaca's most active
                 stocks as candidates, so there are always fresh ideas.

score = w_congress x congress + w_insiders x insiders + w_market x market
        + multi_source_bonus for each extra source agreeing on the stock

Each source's score is scaled 0-1 (best stock = 1) before blending. Stocks
in a downtrend (below the 200-day average), below min_price, or too thinly
traded are dropped. The ranked list is saved to the Alpaca watchlist
"Wheel Picks", and a Telegram summary is sent. On Mondays the weekly wheel
report goes out at the same time.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from ..market import Technicals, technicals
from .congress import DEFAULTS as CONGRESS_DEFAULTS
from .congress import rank as congress_rank
from .politicians import DEFAULTS as TRACK_DEFAULTS
from .politicians import PriceHistory, TrackRecord, top_traders, track_records

log = logging.getLogger(__name__)

DEFAULTS = {
    "keep_picks": 30,
    "min_price": 5.0,
    "min_avg_dollar_volume": 20_000_000,
    "require_uptrend": True,
    "exclude": [],
    "weights": {"congress": 1.0, "insiders": 1.0, "market": 0.5},
    "multi_source_bonus": 0.25,
    "congress": {},
    "insiders": {"enabled": True, "lookback_days": 14, "min_value": 25_000, "cluster_bonus": 1.5,
                 "executive_bonus": 1.5},
    "market": {"most_actives": True, "top": 50},
}

EXECUTIVE_TITLES = ("CEO", "CFO", "COO", "PRES", "CHAIR", "CHIEF")


@dataclass
class InsiderScore:
    ticker: str
    score: float
    insiders: list[str] = field(default_factory=list)
    total_value: float = 0.0


def insider_scores(buys, today: date, lookback_days: int, cluster_bonus: float = 1.5,
                   executive_bonus: float = 1.5) -> dict[str, InsiderScore]:
    cutoff = today - timedelta(days=lookback_days)
    per: dict[str, dict[str, float]] = {}
    totals: dict[str, float] = {}
    for b in buys:
        when = b.filing_date or b.trade_date
        if when is None or when < cutoff or b.value <= 0:
            continue
        w = min(4.0, 1.0 + math.log10(max(b.value, 25_000) / 25_000))
        if any(t in (b.title or "").upper() for t in EXECUTIVE_TITLES):
            w *= executive_bonus
        people = per.setdefault(b.ticker, {})
        people[b.insider] = max(people.get(b.insider, 0.0), w)
        totals[b.ticker] = totals.get(b.ticker, 0.0) + b.value
    out = {}
    for t, people in per.items():
        score = sum(people.values()) * (cluster_bonus if len(people) >= 2 else 1.0)
        out[t] = InsiderScore(t, round(score, 3), sorted(people), totals[t])
    return out


def market_score(tech: Optional[Technicals]) -> float:
    """0-1: half for trend, half for 3-month strength vs SPY (capped at +30%)."""
    if tech is None:
        return 0.0
    strength = min(max(tech.return_3m or 0.0, 0.0), 0.30) / 0.30
    return round(0.5 * tech.trend_score + 0.5 * strength, 3)


@dataclass
class Candidate:
    ticker: str
    score: float = 0.0
    congress: float = 0.0
    insiders: float = 0.0
    market: float = 0.0
    congress_buyers: list[str] = field(default_factory=list)
    insider_names: list[str] = field(default_factory=list)
    trend: str = "unknown"
    sources: list[str] = field(default_factory=list)

    def describe(self) -> str:
        parts = []
        if self.congress_buyers:
            parts.append(f"Congress {len(self.congress_buyers)}")
        if self.insider_names:
            parts.append(f"Insiders {len(self.insider_names)}")
        parts.append(f"{self.trend}trend" if self.trend in ("up", "down") else f"trend {self.trend}")
        return " · ".join(parts)


def blend(congress: dict, insiders: dict[str, InsiderScore], techs: dict[str, Technicals],
          extra: list[str], cfg: dict) -> list[Candidate]:
    w = {**DEFAULTS["weights"], **(cfg.get("weights") or {})}
    bonus = float(cfg.get("multi_source_bonus", 0.25))
    max_c = max([p.score for p in congress.values()] or [0]) or 1.0
    max_i = max([s.score for s in insiders.values()] or [0]) or 1.0
    exclude = {e.upper() for e in cfg.get("exclude", [])}
    out = []
    for t in dict.fromkeys(list(congress) + list(insiders) + list(extra)):
        if t in exclude:
            continue
        c = Candidate(t)
        if t in congress:
            c.congress = congress[t].score / max_c
            c.congress_buyers = congress[t].buyers
            c.sources.append("congress")
        if t in insiders:
            c.insiders = insiders[t].score / max_i
            c.insider_names = insiders[t].insiders
            c.sources.append("insiders")
        tech = techs.get(t)
        c.market = market_score(tech)
        c.trend = tech.trend if tech else "unknown"
        c.score = round(w["congress"] * c.congress + w["insiders"] * c.insiders + w["market"] * c.market
                        + bonus * max(0, len(c.sources) - 1), 4)
        out.append(c)
    out.sort(key=lambda c: (-c.score, c.ticker))
    return out


class StockPicker:
    name = "picker"

    def __init__(self, broker, config: dict, notifier, congress_source=None, insider_source=None, reporter=None):
        self.broker = broker
        self.cfg = {**DEFAULTS, **(config or {})}
        self.cfg["congress"] = {**CONGRESS_DEFAULTS, **(self.cfg.get("congress") or {})}
        self.cfg["insiders"] = {**DEFAULTS["insiders"], **(self.cfg.get("insiders") or {})}
        self.cfg["market"] = {**DEFAULTS["market"], **(self.cfg.get("market") or {})}
        self.notify = notifier
        self.congress_source = congress_source
        self.insider_source = insider_source
        self.reporter = reporter

    def due(self, now: datetime) -> bool:
        """Once a day, on the first top-of-the-hour run with yesterday's (or no) picks; retried hourly."""
        updated = self.broker.picks_updated_on()
        if updated is None:
            return True
        return updated < now.date() and now.minute < 5

    def run(self, now: Optional[datetime] = None, force: bool = False) -> list[Candidate]:
        now = now or datetime.now(timezone.utc)
        if not force and not self.due(now):
            log.info("Picker: today's picks are up to date (%s)", (self.broker.load_picks() or [])[:10])
            return []
        today = now.date()
        warnings: list[str] = []
        ccfg, icfg, mcfg = self.cfg["congress"], self.cfg["insiders"], self.cfg["market"]

        disclosures = []
        if ccfg.get("enabled", True) and self.congress_source:
            try:
                disclosures = self.congress_source.fetch(today)
            except Exception as exc:
                log.exception("Congress source failed")
                warnings.append(f"Congress data unavailable today ({exc})")
        buys = []
        if icfg.get("enabled", True) and self.insider_source:
            try:
                buys = self.insider_source.fetch(today)
            except Exception as exc:
                log.exception("Insider source failed")
                warnings.append(f"Insider data unavailable today ({exc})")
        actives = self.broker.most_actives(int(mcfg["top"])) if mcfg.get("most_actives", True) else []

        ins = insider_scores(buys, today, int(icfg["lookback_days"]), float(icfg["cluster_bonus"]),
                             float(icfg["executive_bonus"]))
        bar_tickers = ({d.ticker for d in disclosures if d.side == "buy"} | set(ins) | set(actives) | {"SPY"})
        start = today - timedelta(days=max(int(ccfg["history_days"]) + 10, 300))
        try:
            bars = self.broker.daily_bars(sorted(bar_tickers), start)
        except Exception as exc:
            log.exception("Price history failed")
            warnings.append(f"Price history unavailable ({exc})")
            bars = {}
        spy = bars.get("SPY")
        techs = {t: technicals(rows, spy) for t, rows in bars.items()}

        records: dict[str, TrackRecord] = {}
        if ccfg.get("use_track_records", True) and disclosures and bars:
            closes = {t: [(r[0], r[1]) for r in rows] for t, rows in bars.items()}
            records = track_records(disclosures, PriceHistory(closes), today,
                                    {**TRACK_DEFAULTS, **(ccfg.get("track_record") or {})})
        weights = {n: r.weight for n, r in records.items()}
        cong = {p.ticker: p for p in congress_rank(disclosures, today, int(ccfg["lookback_days"]),
                                                   int(ccfg["max_trade_age_days"]), None, weights)
                if p.score > 0 and len(p.buyers) >= int(ccfg["min_buyers"])}

        ranked = blend(cong, ins, techs, actives, self.cfg)
        picks = self._filter(ranked, techs)
        self.broker.save_picks([c.ticker for c in picks[: int(self.cfg["keep_picks"])]])
        self._summary(picks, records, warnings, today, len(disclosures), len(buys))
        if self.reporter and today.weekday() == 0:
            try:
                self.reporter.send_weekly()
            except Exception:
                log.exception("Weekly report failed")
        return picks

    def _filter(self, ranked: list[Candidate], techs: dict[str, Technicals]) -> list[Candidate]:
        out = []
        for c in ranked:
            tech = techs.get(c.ticker)
            if tech is None:
                continue
            if tech.price < float(self.cfg["min_price"]):
                continue
            if (tech.avg_dollar_volume or 0) < float(self.cfg["min_avg_dollar_volume"]):
                continue
            if self.cfg["require_uptrend"] and tech.trend == "down":
                continue
            asset = self.broker.asset_info(c.ticker)
            if not asset or not asset["tradable"]:
                continue
            out.append(c)
            if len(out) >= int(self.cfg["keep_picks"]):
                break
        return out

    def _summary(self, picks, records, warnings, today, n_disclosures, n_buys) -> None:
        top = "\n".join(f"{i}. {c.ticker}  {c.score:.2f}  ({c.describe()})"
                        for i, c in enumerate(picks[:10], 1)) or "(no stock passed the filters)"
        traders = "\n".join(
            f"• {r.politician}: {r.avg_excess:+.1%} vs S&P, beat it {r.hit_rate:.0%} of {r.scored_trades} buys"
            for r in top_traders(records, n=3))
        msg = (f"📋 Wheel picks for {today:%a %d %b} "
               f"({n_disclosures} congress trades, {n_buys} insider buys read):\n{top}")
        if traders:
            msg += f"\n\nTop congress traders:\n{traders}"
        if warnings:
            msg += "\n\n⚠️ " + "\n⚠️ ".join(warnings)
        self.notify.send(msg)
