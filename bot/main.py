"""Entry point.

  python -m bot.main            run once (what Render's cron job does)
  python -m bot.main --loop     keep running every 5 minutes (for your own computer)
  python -m bot.main --dry-run  log decisions without placing orders
  python -m bot.main --force    run even when the market is closed (dry runs only)
  python -m bot.main --picks    print today's congress ranking and exit (no trading)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

import yaml

from .notify import Notifier

log = logging.getLogger("bot")

LIVE_CONFIRM_PHRASE = "I-UNDERSTAND-THIS-USES-REAL-MONEY"


def env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


def build(config: dict, dry_run: bool):
    from .broker import AlpacaBroker
    from .sources.capitol_trades import CapitolTradesSource
    from .sources.quiver import QuiverSource
    from .strategies.congress import CongressStrategy
    from .strategies.wheel import WheelStrategy

    key = os.getenv("ALPACA_API_KEY", "")
    secret = os.getenv("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        sys.exit("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set")

    paper = env_bool("ALPACA_PAPER", True)
    if not paper and os.getenv("LIVE_TRADING_CONFIRM", "") != LIVE_CONFIRM_PHRASE:
        sys.exit(
            "ALPACA_PAPER is false but LIVE_TRADING_CONFIRM is not set to "
            f"{LIVE_CONFIRM_PHRASE!r}. Refusing to trade real money."
        )

    data_cfg = config.get("data", {})
    broker = AlpacaBroker(
        key,
        secret,
        paper=paper,
        dry_run=dry_run,
        options_feed=data_cfg.get("options_feed", "indicative"),
        stock_feed=data_cfg.get("stock_feed", "iex"),
    )
    mode = "PAPER" if paper else "LIVE"
    notifier = Notifier(prefix=f"[{mode}{' DRY-RUN' if dry_run else ''}] ")

    strategies = []
    congress_cfg = config.get("congress", {})
    if congress_cfg.get("enabled"):
        if congress_cfg.get("source", "capitol_trades") == "quiver":
            source = QuiverSource()
        else:
            source = CapitolTradesSource(
                lookback_days=int(congress_cfg.get("lookback_days", 30)),
                max_trade_age_days=int(congress_cfg.get("max_trade_age_days", 90)),
                max_pages=int(congress_cfg.get("capitol_trades_max_pages", 60)),
            )
        # Runs first, so on rebalance days the wheel sees today's picks.
        strategies.append(CongressStrategy(broker, congress_cfg, notifier, source))

    wheel_cfg = config.get("wheel", {})
    if wheel_cfg.get("enabled"):
        strategies.append(WheelStrategy(broker, wheel_cfg, notifier))

    return broker, notifier, strategies, mode


def run_once(config: dict, dry_run: bool, force: bool) -> None:
    broker, notifier, strategies, mode = build(config, dry_run)
    if not broker.market_is_open():
        if not force:
            log.info("Market is closed, nothing to do")
            return
        if not dry_run:
            log.warning("--force without --dry-run: switching to dry run, orders can't fill while the market is closed")
            broker.dry_run = True

    log.info("Running %d strategies in %s mode%s", len(strategies), mode, " (dry run)" if broker.dry_run else "")
    for strat in strategies:
        try:
            if strat.name == "congress":
                strat.run(force=force)
            else:
                strat.run()
        except Exception as exc:
            log.exception("Strategy %s failed", strat.name)
            notifier.send(f"❌ {strat.name} failed: {type(exc).__name__}: {exc}")


def show_picks(config: dict) -> None:
    """Print the ranking straight from the data source; needs no Alpaca keys."""
    from datetime import date

    from .sources.capitol_trades import CapitolTradesSource
    from .sources.quiver import QuiverSource
    from .strategies.congress import DEFAULTS, rank

    cfg = {**DEFAULTS, **config.get("congress", {})}
    source = QuiverSource() if cfg["source"] == "quiver" else CapitolTradesSource(
        lookback_days=int(cfg["lookback_days"]), max_trade_age_days=int(cfg["max_trade_age_days"]),
        max_pages=int(cfg["capitol_trades_max_pages"]))
    today = date.today()
    picks = rank(source.fetch(today), today, int(cfg["lookback_days"]), int(cfg["max_trade_age_days"]),
                 set(cfg["exclude"]))
    picks = [p for p in picks if p.score > 0 and len(p.buyers) >= int(cfg["min_buyers"])]
    print(f"{'#':>3}  {'Ticker':<7} {'Score':>6}  Buyers  Sellers  Who's buying")
    for i, p in enumerate(picks[:30], 1):
        print(f"{i:>3}  {p.ticker:<7} {p.score:>6.1f}  {len(p.buyers):>6}  {len(p.sellers):>7}  {', '.join(p.buyers[:5])}")
    if not picks:
        print("No stock had enough different politicians buying it.")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Alpaca wheel + congress follow-the-crowd bot")
    parser.add_argument("--config", default=os.getenv("BOT_CONFIG", "config.yaml"))
    parser.add_argument("--dry-run", action="store_true", help="log orders instead of placing them")
    parser.add_argument("--force", action="store_true", help="run even when the market is closed")
    parser.add_argument("--loop", action="store_true", help="run forever, every --interval minutes")
    parser.add_argument("--interval", type=int, default=5)
    parser.add_argument("--picks", action="store_true", help="print the congress stock ranking and exit")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:  # local runs can use a .env file; on Render, env vars come from the dashboard
        from dotenv import load_dotenv

        load_dotenv(Path.cwd() / ".env")
    except ImportError:
        pass

    config = load_config(args.config)
    dry_run = args.dry_run or env_bool("DRY_RUN", False)

    if args.picks:
        show_picks(config)
        return

    if not args.loop:
        run_once(config, dry_run, args.force)
        return
    log.info("Loop mode: running every %d minutes (Ctrl+C to stop)", args.interval)
    while True:
        try:
            run_once(config, dry_run, args.force)
        except SystemExit:
            raise
        except Exception:
            log.exception("Run failed")
        time.sleep(args.interval * 60)


if __name__ == "__main__":
    main()
