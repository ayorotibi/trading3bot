"""Thin wrapper around the Alpaca SDK.

Strategies only talk to this class, so swapping brokers or testing with a fake
broker only means re-implementing these methods. When dry_run is on, nothing
is sent to Alpaca: order methods log what they would have done.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from alpaca.common.exceptions import APIError
from alpaca.common.enums import Sort
from alpaca.data.enums import DataFeed, OptionsFeed
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.enums import Adjustment
from alpaca.data.requests import (
    OptionChainRequest,
    StockBarsRequest,
    OptionLatestQuoteRequest,
    StockLatestTradeRequest,
)
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    AssetClass,
    ContractType,
    OrderSide,
    PositionIntent,
    QueryOrderStatus,
    TimeInForce,
)
from alpaca.trading.requests import (
    CreateWatchlistRequest,
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    UpdateWatchlistRequest,
)

from . import occ
from .models import Account, OpenOrder, OptionQuote, Position

log = logging.getLogger(__name__)

PICKS_WATCHLIST = "Wheel Picks"


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _enum_value(value) -> str:
    return getattr(value, "value", value) or ""


class AlpacaBroker:
    def __init__(
        self,
        api_key: str,
        secret_key: str,
        paper: bool = True,
        dry_run: bool = False,
        options_feed: str = "indicative",
        stock_feed: str = "iex",
    ):
        self.paper = paper
        self.dry_run = dry_run
        self._key, self._secret = api_key, secret_key
        self.trading = TradingClient(api_key, secret_key, paper=paper)
        self.stock_data = StockHistoricalDataClient(api_key, secret_key)
        self.option_data = OptionHistoricalDataClient(api_key, secret_key)
        self.options_feed = OptionsFeed(options_feed)
        self.stock_feed = DataFeed(stock_feed)
        self._picks_cache: dict[str, list[str]] = {}
        self._picks_saved_on: dict[str, date] = {}
        self._incomplete_cache: Optional[bool] = None

    # ---- account & market -------------------------------------------------
    def market_is_open(self) -> bool:
        return bool(self.trading.get_clock().is_open)

    def account(self) -> Account:
        a = self.trading.get_account()
        return Account(
            equity=_f(a.equity),
            cash=_f(a.cash),
            options_buying_power=_f(a.options_buying_power, _f(a.cash)),
        )

    def positions(self) -> list[Position]:
        out = []
        for p in self.trading.get_all_positions():
            qty = _f(p.qty)
            if _enum_value(p.side) == "short" and qty > 0:
                qty = -qty
            out.append(
                Position(
                    symbol=p.symbol,
                    qty=qty,
                    avg_entry_price=_f(p.avg_entry_price),
                    current_price=_f(p.current_price),
                    is_option=_enum_value(p.asset_class) == AssetClass.US_OPTION.value,
                )
            )
        return out

    def open_orders(self) -> list[OpenOrder]:
        orders = self.trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500))
        return [
            OpenOrder(
                id=str(o.id),
                symbol=o.symbol,
                side=_enum_value(o.side),
                created_at=o.created_at or datetime.now(timezone.utc),
                client_order_id=o.client_order_id or "",
                is_option=_enum_value(o.asset_class) == AssetClass.US_OPTION.value
                or occ.is_option_symbol(o.symbol),
                position_intent=_enum_value(o.position_intent) or None,
                limit_price=_f(o.limit_price, None),
            )
            for o in orders
        ]

    def minutes_to_close(self) -> Optional[float]:
        clock = self.trading.get_clock()
        if not clock.is_open or not clock.next_close:
            return None
        return (clock.next_close - clock.timestamp).total_seconds() / 60

    def orders_today(self, prefix: str = "wh-") -> list[dict]:
        """Every order (any status) placed since midnight UTC whose client_order_id starts with prefix."""
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        batch = self.trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, limit=500, after=start))
        return [
            {"symbol": o.symbol, "coid": o.client_order_id or "", "status": _enum_value(o.status),
             "side": _enum_value(o.side), "filled_qty": _f(o.filled_qty), "price": _f(o.filled_avg_price)}
            for o in batch if (o.client_order_id or "").startswith(prefix)
        ]

    def order_exists(self, client_order_id: str) -> bool:
        try:
            self.trading.get_order_by_client_id(client_order_id)
            return True
        except APIError as exc:
            if exc.status_code == 404:
                return False
            raise

    def asset_info(self, symbol: str) -> Optional[dict]:
        """Returns {'tradable': bool, 'fractionable': bool} or None if unknown."""
        try:
            a = self.trading.get_asset(symbol)
        except APIError:
            return None
        return {"tradable": bool(a.tradable), "fractionable": bool(a.fractionable)}

    def filled_orders(self, prefix: str, days: int = 30) -> list[dict]:
        """Orders (any status) with fills, whose client_order_id starts with prefix."""
        after = datetime.now(timezone.utc) - timedelta(days=days)
        out, seen = [], set()
        while True:
            batch = self.trading.get_orders(
                GetOrdersRequest(status=QueryOrderStatus.ALL, limit=500, after=after, direction=Sort.ASC)
            )
            new = [o for o in batch if str(o.id) not in seen]
            for o in new:
                seen.add(str(o.id))
                if (o.client_order_id or "").startswith(prefix) and _f(o.filled_qty) > 0:
                    out.append({"symbol": o.symbol, "coid": o.client_order_id, "filled_at": o.filled_at,
                                "price": _f(o.filled_avg_price), "qty": _f(o.filled_qty), "side": _enum_value(o.side)})
            if len(batch) < 500 or not new:
                break
            after = max(o.submitted_at or o.created_at for o in batch)
        return out

    # ---- saved stock picks (an Alpaca watchlist) --------------------------
    def _find_watchlist(self, name: str):
        for w in self.trading.get_watchlists():
            if w.name == name:
                return w
        return None

    def load_picks(self, name: str = PICKS_WATCHLIST) -> Optional[list[str]]:
        """Ranked symbols saved by the stock picker, or None if never saved."""
        if name in self._picks_cache:
            return list(self._picks_cache[name])
        w = self._find_watchlist(name)
        if w is None:
            return None
        full = self.trading.get_watchlist_by_id(w.id)
        return [a.symbol for a in (full.assets or [])]

    def picks_updated_on(self, name: str = PICKS_WATCHLIST) -> Optional[date]:
        """UTC date the picks were last refreshed, or None if never."""
        if name in self._picks_saved_on:
            return self._picks_saved_on[name]
        w = self._find_watchlist(name)
        if w is None:
            return None
        ts = w.updated_at or w.created_at
        return ts.astimezone(timezone.utc).date() if ts else None

    def picks_incomplete(self) -> bool:
        """True if today's picks were saved without some data source (retry later)."""
        if self._incomplete_cache is not None:
            return self._incomplete_cache
        return self._find_watchlist(PICKS_WATCHLIST + " - retry") is not None

    def mark_picks_incomplete(self, incomplete: bool) -> None:
        self._incomplete_cache = incomplete
        if self.dry_run:
            return
        w = self._find_watchlist(PICKS_WATCHLIST + " - retry")
        if incomplete and w is None:
            self.trading.create_watchlist(CreateWatchlistRequest(name=PICKS_WATCHLIST + " - retry", symbols=["SPY"]))
        elif not incomplete and w is not None:
            self.trading.delete_watchlist_by_id(w.id)

    def save_picks(self, symbols: list[str], name: str = PICKS_WATCHLIST) -> None:
        self._picks_cache[name] = list(symbols)
        self._picks_saved_on[name] = datetime.now(timezone.utc).date()
        if self.dry_run:
            log.info("[DRY RUN] would save %d picks to watchlist %r: %s", len(symbols), name, symbols)
            return
        if not symbols:
            log.info("No picks to save; keeping the previous watchlist")
            return
        w = self._find_watchlist(name)
        if w is None:
            self.trading.create_watchlist(CreateWatchlistRequest(name=name, symbols=symbols))
        else:
            self.trading.update_watchlist_by_id(w.id, UpdateWatchlistRequest(name=name, symbols=symbols))

    # ---- market data ------------------------------------------------------
    def latest_price(self, symbol: str) -> float:
        res = self.stock_data.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=symbol, feed=self.stock_feed)
        )
        return _f(res[symbol].price)

    def daily_bars(self, symbols: list[str], start: date) -> dict[str, list[tuple[date, float, float]]]:
        """Split- and dividend-adjusted daily (date, close, volume), 100 symbols per request."""
        out: dict[str, list[tuple[date, float, float]]] = {}
        symbols = sorted(set(symbols))
        for i in range(0, len(symbols), 100):
            batch = symbols[i : i + 100]
            try:
                bars = self.stock_data.get_stock_bars(
                    StockBarsRequest(
                        symbol_or_symbols=batch,
                        timeframe=TimeFrame.Day,
                        start=datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc),
                        adjustment=Adjustment.ALL,
                        feed=self.stock_feed,
                    )
                )
            except APIError as exc:
                log.warning("Price history batch %d failed: %s", i // 100 + 1, exc)
                continue
            for sym, rows in (bars.data or {}).items():
                out[sym] = [(b.timestamp.date(), _f(b.close), _f(b.volume)) for b in rows if b.close]
        return out

    def daily_closes(self, symbols: list[str], start: date) -> dict[str, list[tuple[date, float]]]:
        return {s: [(d, c) for d, c, _ in rows] for s, rows in self.daily_bars(symbols, start).items()}

    def most_actives(self, top: int = 50) -> list[str]:
        """Alpaca's most actively traded stocks today, (by volume), or [] if unavailable."""
        try:
            from alpaca.data.historical.screener import ScreenerClient
            from alpaca.data.requests import MostActivesRequest

            res = ScreenerClient(self._key, self._secret).get_most_actives(MostActivesRequest(top=top))
            return [m.symbol for m in (res.most_actives or [])]
        except Exception as exc:
            log.warning("Most-actives screener unavailable: %s", exc)
            return []

    def upcoming_dividends(self, symbols: list[str], days: int = 45) -> dict[str, tuple[date, float]]:
        """Next ex-dividend date and cash amount per symbol, within `days`."""
        if not symbols:
            return {}
        from alpaca.trading.enums import CorporateActionDateType, CorporateActionType
        from alpaca.trading.requests import GetCorporateAnnouncementsRequest

        today = datetime.now(timezone.utc).date()
        out: dict[str, tuple[date, float]] = {}
        try:
            anns = self.trading.get_corporate_announcements(
                GetCorporateAnnouncementsRequest(
                    ca_types=[CorporateActionType.DIVIDEND], since=today, until=today + timedelta(days=days),
                    date_type=CorporateActionDateType.EX_DATE,
                )
            )
        except Exception as exc:
            log.warning("Dividend calendar unavailable: %s", exc)
            return {}
        wanted = set(symbols)
        for a in anns or []:
            sym = a.initiating_symbol or a.target_symbol
            if sym in wanted and a.ex_date and (sym not in out or a.ex_date < out[sym][0]):
                out[sym] = (a.ex_date, _f(a.cash))
        return out

    def account_activities(self, days: int = 400) -> list[dict]:
        """Fills, option assignments/expiries and assignment stock trades, oldest first."""
        after = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        out: list[dict] = []
        token = None
        for _ in range(200):
            params = {
                "activity_types": "FILL,OPASN,OPEXP,OPEXC,OPTRD",
                "after": after,
                "direction": "asc",
                "page_size": 100,
            }
            if token:
                params["page_token"] = token
            page = self.trading.get("/account/activities", params) or []
            out.extend(page)
            if len(page) < 100:
                break
            token = page[-1].get("id")
        return out

    def portfolio_change(self, period: str = "1W") -> Optional[tuple[float, float]]:
        """(start equity, end equity) over the period, or None."""
        try:
            from alpaca.trading.requests import GetPortfolioHistoryRequest

            h = self.trading.get_portfolio_history(GetPortfolioHistoryRequest(period=period, timeframe="1D"))
            eq = [e for e in (h.equity or []) if e]
            return (eq[0], eq[-1]) if len(eq) >= 2 else None
        except Exception as exc:
            log.warning("Portfolio history unavailable: %s", exc)
            return None

    def option_chain(
        self,
        underlying: str,
        type_: str,
        exp_from: date,
        exp_to: date,
        strike_min: Optional[float] = None,
        strike_max: Optional[float] = None,
    ) -> list[OptionQuote]:
        snapshots = self.option_data.get_option_chain(
            OptionChainRequest(
                underlying_symbol=underlying,
                type=ContractType.CALL if type_ == "call" else ContractType.PUT,
                expiration_date_gte=exp_from,
                expiration_date_lte=exp_to,
                strike_price_gte=strike_min,
                strike_price_lte=strike_max,
                feed=self.options_feed,
            )
        )
        out = []
        for symbol, snap in (snapshots or {}).items():
            if not occ.is_option_symbol(symbol):
                continue
            info = occ.parse(symbol)
            q = snap.latest_quote
            if q is None:
                continue
            out.append(
                OptionQuote(
                    symbol=symbol,
                    underlying=info.underlying,
                    type=info.type,
                    strike=info.strike,
                    expiry=info.expiry,
                    bid=_f(q.bid_price),
                    ask=_f(q.ask_price),
                    delta=_f(snap.greeks.delta, None) if snap.greeks else None,
                    iv=_f(snap.implied_volatility, None),
                )
            )
        return out

    def option_quote(self, symbol: str) -> tuple[float, float]:
        res = self.option_data.get_option_latest_quote(
            OptionLatestQuoteRequest(symbol_or_symbols=symbol, feed=self.options_feed)
        )
        q = res[symbol]
        return _f(q.bid_price), _f(q.ask_price)

    # ---- orders -----------------------------------------------------------
    def _submit(self, request, description: str) -> Optional[str]:
        if self.dry_run:
            log.info("[DRY RUN] would submit: %s", description)
            return "dry-run"
        order = self.trading.submit_order(request)
        log.info("Submitted %s -> order %s", description, order.id)
        return str(order.id)

    def submit_option_limit(
        self, symbol: str, qty: int, side: str, limit_price: float, intent: str, client_order_id: str
    ) -> Optional[str]:
        req = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide(side),
            time_in_force=TimeInForce.DAY,  # options must be DAY orders
            limit_price=round(limit_price, 2),
            position_intent=PositionIntent(intent),
            client_order_id=client_order_id,
        )
        return self._submit(req, f"{intent} {qty}x {symbol} @ {limit_price:.2f}")

    def submit_stock_market(
        self,
        symbol: str,
        side: str,
        client_order_id: str,
        notional: Optional[float] = None,
        qty: Optional[float] = None,
    ) -> Optional[str]:
        req = MarketOrderRequest(
            symbol=symbol,
            side=OrderSide(side),
            time_in_force=TimeInForce.DAY,
            notional=round(notional, 2) if notional is not None else None,
            qty=qty,
            client_order_id=client_order_id,
        )
        amount = f"${notional:.2f}" if notional is not None else f"{qty} shares"
        return self._submit(req, f"{side} {amount} of {symbol}")

    def replace_option_limit(self, order_id: str, limit_price: float, client_order_id: str) -> None:
        """Change an open order's limit price (keeps its place; no new order)."""
        from alpaca.trading.requests import ReplaceOrderRequest

        if self.dry_run:
            log.info("[DRY RUN] would re-price order %s to %.2f", order_id, limit_price)
            return
        self.trading.replace_order_by_id(
            order_id, ReplaceOrderRequest(limit_price=round(limit_price, 2), client_order_id=client_order_id)
        )

    def cancel_order(self, order_id: str) -> None:
        if self.dry_run:
            log.info("[DRY RUN] would cancel order %s", order_id)
            return
        self.trading.cancel_order_by_id(order_id)
