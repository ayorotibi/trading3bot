"""Send alerts to Telegram and/or Discord.

Set whichever you use as environment variables:
  TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
  DISCORD_WEBHOOK_URL
If none are set, messages are only written to the log.
"""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, prefix: str = ""):
        self.prefix = prefix
        self.telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.telegram_chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.discord_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

    def send(self, message: str) -> None:
        text = f"{self.prefix}{message}"
        log.info("ALERT: %s", text)
        if self.telegram_token and self.telegram_chat:
            try:
                requests.post(
                    f"https://api.telegram.org/bot{self.telegram_token}/sendMessage",
                    json={"chat_id": self.telegram_chat, "text": text},
                    timeout=10,
                ).raise_for_status()
            except Exception as exc:  # alerts must never crash the bot
                log.warning("Telegram alert failed: %s", exc)
        if self.discord_url:
            try:
                requests.post(self.discord_url, json={"content": text[:1900]}, timeout=10).raise_for_status()
            except Exception as exc:
                log.warning("Discord alert failed: %s", exc)


class MemoryNotifier(Notifier):
    """Collects messages instead of sending them (used in tests)."""

    def __init__(self):
        super().__init__()
        self.telegram_token = self.discord_url = ""
        self.messages: list[str] = []

    def send(self, message: str) -> None:
        self.messages.append(message)
