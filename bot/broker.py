"""Thin wrapper around the Alpaca SDK.

Strategies only talk to this class, so swapping brokers or testing with a fake
broker only means re-implementing these methods. When dry_run is on, nothing
is sent to Alpaca: order methods log what they would have done.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Optional

from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed, OptionsFeed
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import (
    OptionChainRequest,
    OptionLatestQuoteRequest,
    StockLatestTradeRequest,
)
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    AssetClass,
    ContractType,
    OrderSide,
    PositionIntent,
    QueryOrderStatus,
    TimeInForce,
)
from alpaca.trading.requests import GetOrdersRequest, LimitOrderRequest, MarketOrderRequest

from . import occ
from .models import Account, OpenOrder, OptionQuote, Position

log = logging.getLogger(__name__)


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
        self.trading = TradingClient(api_key, secret_key, paper=paper)
        self.stock_data = StockHistoricalDataClient(api_key, secret_key)
        self.option_data = OptionHistoricalDataClient(api_key, secret_key)
        self.options_feed = OptionsFeed(options_feed)
        self.stock_feed = DataFeed(stock_feed)

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
            )
            for o in orders
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

    # ---- market data ------------------------------------------------------
    def latest_price(self, symbol: str) -> float:
        res = self.stock_data.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=symbol, feed=self.stock_feed)
        )
        return _f(res[symbol].price)

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

    def cancel_order(self, order_id: str) -> None:
        if self.dry_run:
            log.info("[DRY RUN] would cancel order %s", order_id)
            return
        self.trading.cancel_order_by_id(order_id)
