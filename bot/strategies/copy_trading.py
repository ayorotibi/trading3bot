"""Politician copy trading.

Reads recent public disclosures for the politicians in your config and
mirrors them with a fixed dollar amount per trade:
  * a disclosed BUY  -> buy amount_per_trade_usd of that stock
  * a disclosed SELL -> sell your whole position in it (if mirror_sells is on)

Each disclosure gets a deterministic Alpaca client_order_id, so Alpaca's own
order history is the memory of what was already copied: the bot never copies
the same disclosure twice, even though Render cron jobs keep no files.

Remember: members of Congress can take up to 45 days to disclose a trade, so
you are always buying after they did.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger(__name__)

DEFAULTS = {
    "politicians": [],
    "source": "capitol_trades",
    "amount_per_trade_usd": 500,
    "lookback_days": 7,
    "max_trade_age_days": 60,
    "mirror_sells": True,
    "max_open_positions": 20,
    "skip_if_already_held": False,
    "check_every_minutes": 30,
    "capitol_trades_pages": 1,
}


def client_order_id(prefix: str, source_id: str) -> str:
    return f"{prefix}-{hashlib.sha1(source_id.encode()).hexdigest()[:24]}"


class CopyTradingStrategy:
    name = "copy_trading"

    def __init__(self, broker, config: dict, notifier, source, protected_symbols: Optional[set] = None):
        self.broker = broker
        self.cfg = {**DEFAULTS, **(config or {})}
        self.notify = notifier
        self.source = source
        # Symbols the wheel owns; copy trading never sells these.
        self.protected = {s.upper() for s in (protected_symbols or set())}

    def due(self, now: datetime) -> bool:
        every = int(self.cfg["check_every_minutes"] or 0)
        if every <= 5:
            return True
        return (now.hour * 60 + now.minute) % every < 5

    def run(self, now: Optional[datetime] = None, force: bool = False) -> None:
        now = now or datetime.now(timezone.utc)
        if not force and not self.due(now):
            log.info("Copy trading: not due this run (checks every %s min)", self.cfg["check_every_minutes"])
            return
        today = now.date()
        disclosures = self.source.fetch(today)

        published_cutoff = today - timedelta(days=int(self.cfg["lookback_days"]))
        traded_cutoff = today - timedelta(days=int(self.cfg["max_trade_age_days"]))
        fresh = []
        for d in disclosures:
            when = d.published_date or d.traded_date
            if when is None or when < published_cutoff:
                continue
            if d.traded_date and d.traded_date < traded_cutoff:
                continue
            fresh.append(d)
        log.info("Copy trading: %d disclosures, %d within lookback", len(disclosures), len(fresh))
        if not fresh:
            return

        positions = {p.symbol: p for p in self.broker.positions() if not p.is_option}
        copy_positions = [s for s in positions if s not in self.protected]

        # Oldest first, so if a buy and a later sell are both new they apply in order.
        fresh.sort(key=lambda d: (d.traded_date or d.published_date, d.side == "sell"))
        for d in fresh:
            try:
                if d.side == "buy":
                    if self._buy(d, positions, copy_positions):
                        copy_positions.append(d.ticker)
                elif d.side == "sell" and self.cfg["mirror_sells"]:
                    self._sell(d, positions)
            except Exception as exc:
                log.exception("Copy trading error on %s", d.ticker)
                self.notify.send(f"⚠️ Copy trade error on {d.ticker}: {exc}")

    def _buy(self, d, positions, copy_positions) -> bool:
        coid = client_order_id("ct-buy", d.source_id)
        if self.broker.order_exists(coid):
            return False
        if d.ticker in self.protected:
            log.info("Copy trading: %s is a wheel symbol, not copying", d.ticker)
            return False
        if self.cfg["skip_if_already_held"] and d.ticker in positions:
            log.info("Copy trading: already hold %s, skipping", d.ticker)
            return False
        if d.ticker not in copy_positions and len(set(copy_positions)) >= int(self.cfg["max_open_positions"]):
            log.info("Copy trading: max_open_positions reached, skipping %s", d.ticker)
            return False

        asset = self.broker.asset_info(d.ticker)
        if not asset or not asset["tradable"]:
            log.info("Copy trading: %s not tradable on Alpaca, skipping", d.ticker)
            return False

        amount = float(self.cfg["amount_per_trade_usd"])
        if asset["fractionable"]:
            self.broker.submit_stock_market(d.ticker, "buy", coid, notional=amount)
            what = f"${amount:,.0f}"
        else:
            price = self.broker.latest_price(d.ticker)
            qty = int(amount // price) if price > 0 else 0
            if qty < 1:
                log.info("Copy trading: $%.0f buys no whole shares of %s @ %.2f", amount, d.ticker, price)
                return False
            self.broker.submit_stock_market(d.ticker, "buy", coid, qty=qty)
            what = f"{qty} shares"
        self.notify.send(
            f"🏛️ Copy trade BUY {what} of {d.ticker} — {d.politician} bought on {d.traded_date} "
            f"(disclosed {d.published_date}, size {d.size or 'n/a'})"
        )
        return True

    def _sell(self, d, positions) -> None:
        pos = positions.get(d.ticker)
        if not pos or pos.qty <= 0 or d.ticker in self.protected:
            return
        coid = client_order_id("ct-sell", d.source_id)
        if self.broker.order_exists(coid):
            return
        self.broker.submit_stock_market(d.ticker, "sell", coid, qty=pos.qty)
        self.notify.send(
            f"🏛️ Copy trade SELL {pos.qty:g} shares of {d.ticker} — {d.politician} sold on {d.traded_date} "
            f"(disclosed {d.published_date})"
        )
