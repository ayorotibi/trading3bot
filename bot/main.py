"""Entry point.

  python -m bot.main            run once (what Render's cron job does)
  python -m bot.main --loop     keep running every 5 minutes (for your own computer)
  python -m bot.main --dry-run  log decisions without placing orders
  python -m bot.main --force    run even when the market is closed (dry runs only)
  python -m bot.main --picks    run the stock picker now and print the ranking (no trading)
  python -m bot.main --report   print the wheel report: cost basis, premiums, profit per stock
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


def make_broker(config: dict, dry_run: bool):
    from .broker import AlpacaBroker

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
    broker = AlpacaBroker(key, secret, paper=paper, dry_run=dry_run,
                          options_feed=data_cfg.get("options_feed", "indicative"),
                          stock_feed=data_cfg.get("stock_feed", "iex"))
    return broker, ("PAPER" if paper else "LIVE")


def make_picker(config: dict, broker, notifier, reporter=None):
    from .sources.openinsider import OpenInsiderSource
    from .sources.quiver import QuiverSource
    from .strategies.picker import DEFAULTS, StockPicker

    pcfg = config.get("picker", {})
    ccfg = pcfg.get("congress", {})
    icfg = {**DEFAULTS["insiders"], **(pcfg.get("insiders") or {})}
    congress_source = QuiverSource() if ccfg.get("source") == "quiver" else make_capitol_source(ccfg)
    insider_source = OpenInsiderSource(lookback_days=int(icfg["lookback_days"]), min_value=float(icfg["min_value"]))
    return StockPicker(broker, pcfg, notifier, congress_source, insider_source, reporter)


def build(config: dict, dry_run: bool):
    from .events import EarningsCalendar
    from .ledger import Ledger
    from .report import WeeklyReport
    from .strategies.wheel import WheelStrategy

    broker, mode = make_broker(config, dry_run)
    notifier = Notifier(prefix=f"[{mode}{' DRY-RUN' if dry_run else ''}] ")
    ledger = Ledger(broker)

    strategies = []
    # The picker runs first, so the morning refresh is in place before the wheel trades.
    if config.get("picker", {}).get("enabled", True):
        strategies.append(make_picker(config, broker, notifier, WeeklyReport(broker, notifier, ledger)))
    wheel_cfg = config.get("wheel", {})
    if wheel_cfg.get("enabled", True):
        strategies.append(WheelStrategy(broker, wheel_cfg, notifier, EarningsCalendar(), ledger))
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
            if strat.name == "picker":
                strat.run(force=force)
            else:
                strat.run()
        except Exception as exc:
            log.exception("Strategy %s failed", strat.name)
            notifier.send(f"❌ {strat.name} failed: {type(exc).__name__}: {exc}")


def make_capitol_source(cfg: dict):
    from .sources.capitol_trades import CapitolTradesSource

    history = int(cfg.get("history_days", 365)) if cfg.get("use_track_records", True) else int(cfg.get("lookback_days", 30))
    return CapitolTradesSource(
        lookback_days=history,
        max_trade_age_days=max(history, int(cfg.get("max_trade_age_days", 90))),
        max_pages=int(cfg.get("capitol_trades_max_pages", 200)),
    )


class PrintNotifier:
    def send(self, message: str) -> None:
        print(message + "\n")


def show_picks(config: dict) -> None:
    """Run the stock picker now and print the result. Places no orders, saves nothing."""
    broker, _ = make_broker(config, dry_run=True)
    picks = make_picker(config, broker, PrintNotifier()).run(force=True)
    print(f"{'#':>3}  {'Ticker':<7} {'Score':>5}  Congress  Insiders  Market  Trend")
    for i, c in enumerate(picks, 1):
        print(f"{i:>3}  {c.ticker:<7} {c.score:>5.2f}  {c.congress:>8.2f}  {c.insiders:>8.2f}  {c.market:>6.2f}  {c.trend}")


def show_report(config: dict) -> None:
    from .report import WeeklyReport

    broker, _ = make_broker(config, dry_run=True)
    print(WeeklyReport(broker, PrintNotifier()).build())


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Alpaca options wheel bot")
    parser.add_argument("--config", default=os.getenv("BOT_CONFIG", "config.yaml"))
    parser.add_argument("--dry-run", action="store_true", help="log orders instead of placing them")
    parser.add_argument("--force", action="store_true", help="run even when the market is closed")
    parser.add_argument("--loop", action="store_true", help="run forever, every --interval minutes")
    parser.add_argument("--interval", type=int, default=5)
    parser.add_argument("--picks", action="store_true", help="run the stock picker now, print the ranking, trade nothing")
    parser.add_argument("--report", action="store_true", help="print the wheel report (cost basis, premiums) and exit")
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
    if args.report:
        show_report(config)
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
