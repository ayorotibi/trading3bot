"""Weekly wheel report (sent on Mondays with the morning picks)."""
from __future__ import annotations

import logging

from . import occ
from .ledger import Ledger

log = logging.getLogger(__name__)


class WeeklyReport:
    def __init__(self, broker, notifier, ledger=None):
        self.broker = broker
        self.notify = notifier
        self.ledger = ledger or Ledger(broker)

    def build(self) -> str:
        books = self.ledger.books
        lines = ["📊 Weekly wheel report"]
        change = self.broker.portfolio_change("1W")
        acct = self.broker.account()
        if change:
            start, end = change
            lines.append(f"Account ${end:,.0f} ({end - start:+,.0f}, {(end / start - 1) if start else 0:+.1%} this week)")
        else:
            lines.append(f"Account ${acct.equity:,.0f}")

        positions = self.broker.positions()
        opts = [p for p in positions if p.is_option and p.qty < 0]
        if opts:
            lines.append("\nOpen contracts:")
            for p in opts:
                o = occ.parse(p.symbol)
                lines.append(f"• {o.underlying} ${o.strike:g} {o.type} exp {o.expiry:%d %b}: sold @ ${p.avg_entry_price:.2f},"
                             f" now ${p.current_price:.2f}")
        held = [b for b in books.values() if b.shares > 0]
        if held:
            lines.append("\nShares held (adjusted cost basis after premiums):")
            for b in held:
                lines.append(f"• {b.ticker}: {b.shares:g} shares, bought @ ${b.avg_cost or 0:.2f}, "
                             f"adjusted ${b.adjusted_basis or 0:.2f}")
        done = [b for b in books.values() if b.completed or (b.current and b.current.premium)]
        if done:
            lines.append("\nBy stock (all cycles):")
            for b in sorted(done, key=lambda b: -b.total_premium):
                lines.append(f"• {b.ticker}: {len(b.completed)} cycles done, premium ${b.total_premium:,.0f}, "
                             f"realised profit ${b.realized_profit:,.0f}")
            total_p = sum(b.total_premium for b in books.values())
            total_r = sum(b.realized_profit for b in books.values())
            lines.append(f"\nTotal premium collected ${total_p:,.0f} · realised profit from finished cycles ${total_r:,.0f}")
        if len(lines) <= 2:
            lines.append("No wheel trades yet.")
        return "\n".join(lines)

    def send_weekly(self) -> None:
        self.notify.send(self.build())
