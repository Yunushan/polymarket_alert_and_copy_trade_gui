from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .base import MarketAdapter
from .catalog import get_market_metadata
from .errors import MarketConfigurationError
from .types import (
    MarketContract,
    MarketCandle,
    MarketEvent,
    OrderBookLevel,
    OrderBookSnapshot,
    PaperOrderRequest,
    PaperOrderResult,
    PriceSnapshot,
    MarketTrade,
)


DEFAULT_GEMINI_BASE_URL = "https://api.gemini.com"
GEMINI_REFERENCES = (
    "https://developer.gemini.com/prediction-markets-spec",
    "https://developer.gemini.com/prediction-markets-spec/markets",
    "https://developer.gemini.com/prediction-markets-spec/trading",
    "https://developer.gemini.com/prediction-markets-spec/terms",
    "https://developer.gemini.com/prediction-markets/websocket/streams",
)
GEMINI_ORDER_MANAGEMENT_OPERATIONS = ("cancel_order", "batch_cancel_orders")
GEMINI_ORDER_MANAGEMENT_CONFIRMATION = "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
GEMINI_ORDER_MANAGEMENT_MAX_BATCH = 20
GEMINI_ORDER_MANAGEMENT_REFERENCES = (
    "https://developer.gemini.com/rest-api/prediction-markets/order-management/cancel-order",
    "https://developer.gemini.com/rest-api/prediction-markets/order-management/cancel-batch-orders",
)


class GeminiPredictionAdapter(MarketAdapter):
    """Gemini Prediction Markets read-only adapter using official public endpoints."""

    metadata = get_market_metadata("gemini_titan")
    account_recovery_operations = (
        "active_orders",
        "order_history",
        "positions",
        "settled_positions",
        "volume_metrics",
    )
    order_management_operations = GEMINI_ORDER_MANAGEMENT_OPERATIONS

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        api_key = self.resolve_credential("gemini_api_key", ("GEMINI_API_KEY",), label="GEMINI_API_KEY")
        health.update(
            {
                "api_base_url": self.api_base_url,
                "references": list(GEMINI_REFERENCES),
                "credential_sources": [{"name": api_key.name, "source": api_key.source}] if api_key else [],
                "live_trading_supported": True,
                "live_trading_enabled": self.config_bool("live_trading_enabled", False),
                "account_recovery_operations": list(self.account_recovery_operations),
                "order_management_operations": list(self.order_management_operations),
                "order_management_enabled": self.config_bool("gemini_order_management_enabled", False),
                "authenticated_account_endpoints": [
                    "POST /v1/prediction-markets/orders/active",
                    "POST /v1/prediction-markets/orders/history",
                    "POST /v1/prediction-markets/positions",
                    "POST /v1/prediction-markets/positions/settled",
                    "POST /v1/prediction-markets/metrics/volume",
                ],
                "order_management_endpoints": [
                    "POST /v1/prediction-markets/order/cancel",
                    "POST /v1/prediction-markets/order/batch/cancel",
                ],
            }
        )
        return health

    @property
    def api_base_url(self) -> str:
        configured = self.config.get("gemini_api_base_url") or self.config.get("api_base_url")
        return str(configured or DEFAULT_GEMINI_BASE_URL).rstrip("/")

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = max(1, min(int(limit or 50), 500))
        params: Dict[str, Any] = {"limit": desired}
        if query:
            params["search"] = str(query).strip()
        status = str(self.config.get("gemini_event_status") or "active").strip()
        if status:
            params["status"] = status
        payload = self._get("/v1/prediction-markets/events", params=params)
        events = self._list_from_payload(payload, "data", "events")
        return [self._event_from_payload(event) for event in events[:desired]]

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        event = self._get_event(event_id)
        return self._contracts_from_event(event)

    def get_orderbook(self, contract_id: str) -> OrderBookSnapshot:
        self.ensure_capability("orderbook_reading")
        event_ticker, instrument_symbol = self._split_contract_id(contract_id)
        event = self._get_event(event_ticker)
        payload = self._contract_orderbook(event, instrument_symbol)
        if payload is None:
            raise MarketConfigurationError(
                f"Gemini event {event_ticker} did not include an orderbook for {instrument_symbol}. "
                "The documented prediction-market REST contract exposes depth in the event response; "
                "live streaming depth remains available through the official WebSocket stream."
            )
        bids = self._book_levels(self._value_at(payload, "bids"), descending=True)
        asks = self._book_levels(self._value_at(payload, "asks"))
        return OrderBookSnapshot(
            market_id=self.market_id,
            contract_id=self._contract_id(event_ticker, instrument_symbol),
            bids=bids,
            asks=asks,
            raw=payload if isinstance(payload, dict) else {},
        )

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        event_ticker, instrument_symbol = self._split_contract_id(contract_id)
        event = self._get_event(event_ticker)
        contract = self._find_contract(event, instrument_symbol)
        prices = contract.get("prices") if isinstance(contract.get("prices"), Mapping) else {}
        bid = self._safe_probability(self._value_at(prices, "bestBid", "best_bid"))
        ask = self._safe_probability(self._value_at(prices, "bestAsk", "best_ask"))
        last = self._safe_probability(self._value_at(prices, "lastTradePrice", "last_trade_price"))
        if bid is None or ask is None:
            orderbook = self._contract_orderbook(event, instrument_symbol)
            if orderbook is not None:
                bids = self._book_levels(self._value_at(orderbook, "bids"), descending=True)
                asks = self._book_levels(self._value_at(orderbook, "asks"))
                bid = bid if bid is not None else (bids[0].price if bids else None)
                ask = ask if ask is not None else (asks[0].price if asks else None)
        midpoint = (bid + ask) / 2.0 if bid is not None and ask is not None else None
        if last is None:
            last = midpoint
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=self._contract_id(event_ticker, instrument_symbol),
            last=last,
            bid=bid,
            ask=ask,
            midpoint=midpoint,
            source="gemini_prediction_contract",
            raw={"event": dict(event), "contract": dict(contract)},
        )

    def list_candles(
        self,
        contract_id: str,
        *,
        resolution: str = "1h",
        from_timestamp: Optional[float] = None,
        to_timestamp: Optional[float] = None,
    ) -> List[MarketCandle]:
        """Return Gemini's documented contract price-history points.

        Gemini exposes irregular ``priceHistory`` snapshots on the contract
        detail response rather than exchange-style OHLCV bars.  Each point is
        represented as a flat candle and volume is intentionally left unset;
        no resampling is claimed.
        """

        self.ensure_capability("candle_history")
        requested_resolution = str(resolution or "1h").strip().lower()
        if requested_resolution not in {"raw", "price", "1h", "1d"}:
            raise MarketConfigurationError(
                "Gemini price history accepts resolution 'raw', 'price', '1h', or '1d'; "
                "the irregular official snapshots are not resampled."
            )
        lower = self._history_timestamp(from_timestamp, "from_timestamp") if from_timestamp is not None else None
        upper = self._history_timestamp(to_timestamp, "to_timestamp") if to_timestamp is not None else None
        if lower is not None and upper is not None and upper < lower:
            raise MarketConfigurationError("Gemini price history to_timestamp must not precede from_timestamp.")

        event_ticker, instrument_symbol = self._split_contract_id(contract_id)
        event = self._get_event(event_ticker)
        contract = self._find_contract(event, instrument_symbol)
        history = contract.get("priceHistory")
        if history is None:
            history = contract.get("price_history")
        if history is None:
            raise MarketConfigurationError(
                f"Gemini event {event_ticker} did not include price history for {instrument_symbol}."
            )
        if not isinstance(history, list):
            raise MarketConfigurationError("Gemini priceHistory must be a list of timestamp/price points.")

        canonical = self._contract_id(event_ticker, instrument_symbol)
        candles: List[MarketCandle] = []
        for point in history:
            if not isinstance(point, Mapping):
                continue
            timestamp = self._history_timestamp(point.get("timestamp"), "timestamp")
            price = self._safe_probability(point.get("price"))
            if timestamp is None or price is None:
                continue
            if lower is not None and timestamp < lower:
                continue
            if upper is not None and timestamp > upper:
                continue
            candles.append(
                MarketCandle(
                    market_id=self.market_id,
                    contract_id=canonical,
                    timestamp=timestamp,
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    volume=None,
                    raw={"source": "gemini_prediction_markets", "resolution_requested": requested_resolution, **dict(point)},
                )
            )
        candles.sort(key=lambda candle: candle.timestamp)
        return candles

    def list_trades(
        self,
        contract_id: str,
        *,
        limit: int = 50,
        before: Optional[float] = None,
        after: Optional[float] = None,
    ) -> List[MarketTrade]:
        """Normalize authenticated filled Gemini orders as account trades.

        Gemini's documented history endpoint is an authenticated order-history
        feed, not a public market tape.  Only filled rows are exposed here;
        the normalized side therefore preserves the account order direction
        (BUY/SELL), while the requested contract and time bounds are checked
        again locally for deterministic fixture/live behavior.
        """

        self.ensure_capability("trade_history")
        event_ticker, instrument_symbol = self._split_contract_id(contract_id)
        desired = self._bounded_int(limit, "trade limit", minimum=1, maximum=1000)
        lower = self._history_timestamp(after, "after") if after is not None else None
        upper = self._history_timestamp(before, "before") if before is not None else None
        if lower is not None and upper is not None and upper < lower:
            raise MarketConfigurationError("Gemini trade history before must not precede after.")
        payload = self.list_order_history(
            status="filled",
            contract_id=self._contract_id(event_ticker, instrument_symbol),
            limit=desired,
            offset=0,
            from_timestamp=lower,
            to_timestamp=upper,
        )
        rows = self._list_from_payload(payload, "orders", "data")
        trades: List[MarketTrade] = []
        canonical = self._contract_id(event_ticker, instrument_symbol)
        for index, row in enumerate(rows):
            status = str(row.get("status") or "filled").strip().lower()
            if status != "filled":
                continue
            row_symbol = str(
                row.get("symbol")
                or row.get("instrumentSymbol")
                or row.get("instrument_symbol")
                or instrument_symbol
            ).strip()
            if row_symbol and row_symbol != instrument_symbol:
                continue
            side = str(row.get("side") or row.get("orderSide") or "").strip().upper()
            if side not in {"BUY", "SELL"}:
                continue
            size = self._positive_number(
                row.get("filledQuantity")
                or row.get("filled_quantity")
                or row.get("executedQuantity")
                or row.get("quantity")
                or row.get("size")
            )
            price = self._safe_probability(
                row.get("averageFillPrice")
                or row.get("average_fill_price")
                or row.get("avgFillPrice")
                or row.get("fillPrice")
                or row.get("price")
            )
            timestamp = self._history_timestamp(
                row.get("filledAt")
                or row.get("filled_at")
                or row.get("executedAt")
                or row.get("updatedAt")
                or row.get("updated_at")
                or row.get("createdAt")
                or row.get("created_at"),
                "trade timestamp",
            )
            if size is None or price is None or timestamp is None:
                continue
            if lower is not None and timestamp < lower:
                continue
            if upper is not None and timestamp > upper:
                continue
            trade_id = str(row.get("orderId") or row.get("order_id") or row.get("id") or "").strip()
            if not trade_id:
                trade_id = f"gemini:{instrument_symbol}:{int(timestamp * 1000)}:{index}"
            trades.append(
                MarketTrade(
                    market_id=self.market_id,
                    contract_id=canonical,
                    trade_id=trade_id,
                    side=side,
                    price=price,
                    size=size,
                    timestamp=timestamp,
                    raw=dict(row),
                )
            )
            if len(trades) >= desired:
                break
        return trades

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        self._validate_order(order)
        event_ticker, instrument_symbol = self._split_contract_id(order.contract_id)
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=self._contract_id(event_ticker, instrument_symbol),
            accepted=True,
            message=(
                f"DRY RUN: would place Gemini Prediction {order.side.upper()} "
                f"for {order.size:.4f} contracts"
                + (f" at limit {order.limit_price:.2f}" if order.limit_price is not None else "")
            ),
            average_price=order.limit_price,
            raw={"event_ticker": event_ticker, "instrument_symbol": instrument_symbol},
        )

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        self.ensure_capability("live_trading")
        self._validate_order(order)
        preflight = self.preflight_live_order(order)
        if order.limit_price is None:
            raise MarketConfigurationError("Gemini live orders require a limit price.")
        event_ticker, instrument_symbol = self._split_contract_id(order.contract_id)
        self._ensure_prediction_terms_accepted()
        payload = self._live_order_payload(
            order,
            instrument_symbol=instrument_symbol,
        )
        response = self._authenticated_post("/v1/prediction-markets/order", payload)
        return {
            "market_id": self.market_id,
            "event_ticker": event_ticker,
            "contract_id": self._contract_id(event_ticker, instrument_symbol),
            "instrument_symbol": instrument_symbol,
            "live": True,
            "preflight": preflight,
            "request": payload,
            "response": response,
        }

    def manage_orders(self, operation: str, **kwargs: Any) -> Dict[str, Any]:
        """Run one guarded Gemini Prediction Markets cancellation mutation.

        Gemini documents cancellation as a separate authenticated REST surface.
        Only the fixed single-order and batch endpoints are exposed here; the
        adapter never accepts a caller-provided path or method. Cancellation is
        opt-in and requires the shared live-safety gates plus exact confirmation.
        """

        normalized = str(operation or "").strip().lower()
        if normalized not in self.order_management_operations:
            supported = ", ".join(self.order_management_operations)
            raise MarketConfigurationError(
                f"Gemini order-management operation must be one of: {supported}."
            )
        self.ensure_capability("live_trading")
        if not self.config_bool("gemini_order_management_enabled", False):
            raise MarketConfigurationError(
                "Gemini order management is disabled by adapter config. "
                "Set gemini_order_management_enabled=true only after reviewing cancellation risk."
            )
        self.ensure_live_trading_enabled("Gemini order management")
        if str(kwargs.get("confirm_order_management") or "").strip() != GEMINI_ORDER_MANAGEMENT_CONFIRMATION:
            raise MarketConfigurationError(
                "Gemini order management requires exact confirmation text "
                f"{GEMINI_ORDER_MANAGEMENT_CONFIRMATION}."
            )
        if bool(kwargs.get("async_request")):
            raise MarketConfigurationError("Gemini order-management requests are synchronous.")

        if normalized == "cancel_order":
            order_id = self._order_management_id(kwargs.get("order_id"))
            request = {"orderId": order_id}
            response = self._authenticated_post("/v1/prediction-markets/order/cancel", request)
        else:
            order_ids = self._order_management_ids(kwargs.get("orders", kwargs.get("instructions")))
            request = {"orderIds": order_ids}
            response = self._authenticated_post("/v1/prediction-markets/order/batch/cancel", request)

        return {
            "market_id": self.market_id,
            "operation": normalized,
            "live": True,
            "preflight": {
                "market_id": self.market_id,
                "display_name": self.display_name,
                "feature": "order management",
                "operation": normalized,
                "live_trading_enabled": True,
                "order_management_enabled": True,
                "confirmed": True,
                "requires_credentials": True,
                "references": list(GEMINI_ORDER_MANAGEMENT_REFERENCES),
            },
            "request": request,
            "response": response,
        }

    def list_active_orders(
        self,
        contract_id: Optional[str] = None,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Any:
        """Recover currently open prediction-market orders for the account."""

        payload: Dict[str, Any] = {
            "limit": self._bounded_int(limit, "limit", minimum=1, maximum=100),
            "offset": self._bounded_int(offset, "offset", minimum=0, maximum=10000),
        }
        if contract_id:
            _, symbol = self._split_contract_id(contract_id)
            payload["symbol"] = symbol
        return self._authenticated_post("/v1/prediction-markets/orders/active", payload)

    def list_order_history(
        self,
        *,
        status: str = "filled",
        contract_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        from_timestamp: Optional[float] = None,
        to_timestamp: Optional[float] = None,
    ) -> Any:
        """Recover filled/cancelled account orders through the documented REST API."""

        normalized_status = str(status or "").strip().lower()
        if normalized_status not in {"filled", "cancelled"}:
            raise MarketConfigurationError("Gemini order history status must be filled or cancelled.")
        payload: Dict[str, Any] = {
            "status": normalized_status,
            "limit": self._bounded_int(limit, "limit", minimum=1, maximum=1000),
            "offset": self._bounded_int(offset, "offset", minimum=0, maximum=10000),
        }
        if contract_id:
            _, symbol = self._split_contract_id(contract_id)
            payload["symbol"] = symbol
        if from_timestamp is not None:
            payload["from"] = self._history_millis(from_timestamp, "from_timestamp")
        if to_timestamp is not None:
            payload["to"] = self._history_millis(to_timestamp, "to_timestamp")
        if "from" in payload and "to" in payload and payload["from"] > payload["to"]:
            raise MarketConfigurationError("Gemini order history to_timestamp must not precede from_timestamp.")
        return self._authenticated_post("/v1/prediction-markets/orders/history", payload)

    def get_positions(
        self,
        event_ticker: str = "",
        *,
        limit: Optional[int] = None,
        offset: int = 0,
        sort: Optional[str] = None,
    ) -> Any:
        """Recover current filled positions for the authenticated account."""

        payload: Dict[str, Any] = {}
        if event_ticker:
            payload["eventTicker"] = self._nonempty_text(event_ticker, "event_ticker")
        if limit is not None:
            payload["limit"] = self._bounded_int(limit, "limit", minimum=1, maximum=1000)
            payload["offset"] = self._bounded_int(offset, "offset", minimum=0, maximum=100000)
        elif offset:
            raise MarketConfigurationError("Gemini positions offset requires limit to be set.")
        if sort is not None:
            normalized_sort = str(sort).strip()
            if normalized_sort.lower() not in {
                "positionvalue",
                "+positionvalue",
                "-positionvalue",
                "unrealizedpnl",
                "+unrealizedpnl",
                "-unrealizedpnl",
                "expirydate",
                "+expirydate",
                "-expirydate",
            }:
                raise MarketConfigurationError("Gemini positions sort is not a documented value.")
            payload["sort"] = normalized_sort
        return self._authenticated_post("/v1/prediction-markets/positions", payload)

    def get_settled_positions(
        self,
        event_ticker: str = "",
        *,
        limit: int = 1000,
        offset: int = 0,
        sort: str = "-date",
        search: str = "",
        category: str = "",
        with_cash_outs: bool = False,
    ) -> Any:
        """Recover historically settled prediction-market positions."""

        payload: Dict[str, Any] = {
            "limit": self._bounded_int(limit, "limit", minimum=1, maximum=1000),
            "offset": self._bounded_int(offset, "offset", minimum=0, maximum=100000),
            "sort": self._settled_sort(sort),
            "withCashOuts": bool(with_cash_outs),
        }
        if event_ticker:
            payload["eventTicker"] = self._nonempty_text(event_ticker, "event_ticker")
        if search:
            payload["search"] = str(search).strip()[:64]
        if category:
            payload["category"] = self._nonempty_text(category, "category")
        return self._authenticated_post("/v1/prediction-markets/positions/settled", payload)

    def get_volume_metrics(
        self,
        event_ticker: str,
        *,
        start_timestamp: Optional[float] = None,
        end_timestamp: Optional[float] = None,
    ) -> Any:
        """Return documented per-contract share-volume metrics for an event."""

        payload: Dict[str, Any] = {"eventTicker": self._nonempty_text(event_ticker, "event_ticker")}
        if start_timestamp is not None:
            payload["startTime"] = self._history_millis(start_timestamp, "start_timestamp")
        if end_timestamp is not None:
            payload["endTime"] = self._history_millis(end_timestamp, "end_timestamp")
        if "startTime" in payload and "endTime" in payload and payload["startTime"] > payload["endTime"]:
            raise MarketConfigurationError("Gemini volume end_timestamp must not precede start_timestamp.")
        return self._authenticated_post("/v1/prediction-markets/metrics/volume", payload)

    def account_recovery(self, operation: str, **kwargs: Any) -> Any:
        """Dispatch an explicitly allow-listed Gemini account read.

        Keeping this small dispatcher beside the validated endpoint methods
        gives the CLI and web API one safe surface without exposing arbitrary
        authenticated paths or allowing callers to bypass parameter checks.
        """

        normalized = str(operation or "").strip().lower()
        if normalized == "active_orders":
            return self.list_active_orders(
                kwargs.get("contract_id"),
                limit=kwargs.get("limit", 50),
                offset=kwargs.get("offset", 0),
            )
        if normalized == "order_history":
            return self.list_order_history(
                status=kwargs.get("status", "filled"),
                contract_id=kwargs.get("contract_id"),
                limit=kwargs.get("limit", 50),
                offset=kwargs.get("offset", 0),
                from_timestamp=kwargs.get("from_timestamp"),
                to_timestamp=kwargs.get("to_timestamp"),
            )
        if normalized == "positions":
            return self.get_positions(
                kwargs.get("event_ticker", ""),
                limit=kwargs.get("limit"),
                offset=kwargs.get("offset", 0),
                sort=kwargs.get("sort"),
            )
        if normalized == "settled_positions":
            return self.get_settled_positions(
                kwargs.get("event_ticker", ""),
                limit=kwargs.get("limit", 1000),
                offset=kwargs.get("offset", 0),
                sort=kwargs.get("sort", "-date"),
                search=kwargs.get("search", ""),
                category=kwargs.get("category", ""),
                with_cash_outs=kwargs.get("with_cash_outs", False),
            )
        if normalized == "volume_metrics":
            return self.get_volume_metrics(
                kwargs.get("event_ticker", ""),
                start_timestamp=kwargs.get("start_timestamp"),
                end_timestamp=kwargs.get("end_timestamp"),
            )
        supported = ", ".join(self.account_recovery_operations)
        raise MarketConfigurationError(
            f"Gemini account recovery operation must be one of: {supported}."
        )

    def copy_trade_from_activity(self, activity: Mapping[str, Any]) -> PaperOrderResult:
        """Build a local copy preview from a filled authenticated order.

        Gemini's documented order-history feed exposes filled order identity,
        event instrument, account direction, filled quantity, execution price,
        and timestamps.  This path validates those fields and only creates a
        local paper preview; it never calls the Gemini order endpoint.
        """

        self.ensure_capability("copy_trading")
        def first_present(*keys: str) -> Any:
            for key in keys:
                value = activity.get(key)
                if value not in (None, ""):
                    return value
            return None

        contract_id = str(activity.get("asset") or activity.get("contract_id") or "").strip()
        event_ticker = str(
            activity.get("event_ticker")
            or activity.get("eventTicker")
            or activity.get("event_id")
            or activity.get("eventId")
            or ""
        ).strip()
        instrument_symbol = str(
            activity.get("symbol")
            or activity.get("instrumentSymbol")
            or activity.get("instrument_symbol")
            or ""
        ).strip()
        if contract_id:
            parsed_event, parsed_symbol = self._split_contract_id(contract_id)
            if event_ticker and parsed_event != event_ticker:
                raise MarketConfigurationError("Gemini activity event ticker does not match contract_id.")
            if instrument_symbol and parsed_symbol != instrument_symbol:
                raise MarketConfigurationError("Gemini activity instrument symbol does not match contract_id.")
            event_ticker, instrument_symbol = parsed_event, parsed_symbol
        elif event_ticker and instrument_symbol:
            contract_id = self._contract_id(event_ticker, instrument_symbol)
        else:
            raise MarketConfigurationError("Gemini activity requires contract_id or event ticker plus symbol.")

        status = str(activity.get("status") or "filled").strip().lower()
        if status != "filled":
            raise MarketConfigurationError("Gemini copy previews require a filled order-history row.")
        side = str(activity.get("side") or activity.get("orderSide") or "").strip().upper()
        if side not in {"BUY", "SELL"}:
            raise MarketConfigurationError("Gemini activity side must be BUY or SELL.")
        size = self._positive_number(
            first_present("filledQuantity", "filled_quantity", "executedQuantity", "quantity", "size")
        )
        if size is None:
            raise MarketConfigurationError("Gemini activity filled quantity must be positive and finite.")
        price = self._safe_probability(
            first_present("averageFillPrice", "average_fill_price", "avgFillPrice", "fillPrice", "price")
        )
        if price is None:
            raise MarketConfigurationError("Gemini activity fill price must be between 0 and 1.")
        timestamp = self._history_timestamp(
            first_present("filledAt", "filled_at", "executedAt", "updatedAt", "updated_at", "createdAt"),
            "activity timestamp",
        )
        if timestamp is None:
            raise MarketConfigurationError("Gemini activity requires a filled/execution timestamp.")
        order_id = str(
            activity.get("orderId")
            or activity.get("order_id")
            or activity.get("trade_id")
            or activity.get("tradeId")
            or activity.get("id")
            or ""
        ).strip()
        if not order_id:
            raise MarketConfigurationError("Gemini activity requires a documented order id.")

        preview = self.place_paper_order(
            PaperOrderRequest(
                market_id=self.market_id,
                contract_id=contract_id,
                side=side,
                size=size,
                limit_price=price,
                metadata={"activity": dict(activity), "source": "gemini_authenticated_filled_orders"},
            )
        )
        preview.raw["source"] = "gemini_authenticated_filled_orders"
        preview.raw["activity"] = dict(activity)
        preview.raw["order_id"] = order_id
        preview.raw["timestamp"] = timestamp
        return preview

    def _get_event(self, event_id: str) -> Mapping[str, Any]:
        ticker = str(event_id or "").strip()
        if not ticker:
            raise MarketConfigurationError("Gemini event ticker cannot be empty.")
        payload = self._get(f"/v1/prediction-markets/events/{ticker}")
        if isinstance(payload, Mapping):
            data = payload.get("data")
            if isinstance(data, Mapping):
                return data
            return payload
        raise MarketConfigurationError(f"Gemini event {ticker!r} was not found.")

    @classmethod
    def _find_contract(cls, event: Mapping[str, Any], instrument_symbol: str) -> Mapping[str, Any]:
        for contract in cls._list_from_payload(event, "contracts"):
            if cls._instrument_symbol(contract) == instrument_symbol:
                return contract
        raise MarketConfigurationError(
            f"Gemini event {cls._event_ticker(event)!r} did not include contract {instrument_symbol!r}."
        )

    @classmethod
    def _contract_orderbook(cls, event: Mapping[str, Any], instrument_symbol: str) -> Optional[Mapping[str, Any]]:
        contract = cls._find_contract(event, instrument_symbol)
        for key in ("orderbook", "orderBook", "book"):
            direct = contract.get(key)
            if isinstance(direct, Mapping):
                return direct
        orderbooks = event.get("contractOrderbooks") or event.get("contract_orderbooks")
        if isinstance(orderbooks, Mapping):
            contract_id = str(contract.get("id") or "").strip()
            contract_ticker = str(contract.get("ticker") or "").strip()
            for key in (instrument_symbol, contract_id, contract_ticker):
                if key and isinstance(orderbooks.get(key), Mapping):
                    return orderbooks[key]
        return None

    def _get(self, path: str, *, params: Optional[Mapping[str, Any]] = None) -> Any:
        return self.runtime.get_json(self._url(path), params=params)

    def _authenticated_post(self, path: str, payload: Mapping[str, Any]) -> Any:
        request_payload = dict(payload)
        request_payload.setdefault("request", path)
        request_payload.setdefault("nonce", self._next_nonce())
        return self._authenticated_request("POST", path, request_payload, send_body=True)

    def _authenticated_get(self, path: str) -> Any:
        request_payload = {"request": path, "nonce": self._next_nonce()}
        return self._authenticated_request("GET", path, request_payload, send_body=False)

    def _authenticated_request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any],
        *,
        send_body: bool,
    ) -> Any:
        body = json.dumps(dict(payload), separators=(",", ":")) if send_body else ""
        headers = self._auth_headers(payload, content_type="application/json" if send_body else "text/plain")
        return self.runtime.request_json(
            method,
            self._url(path),
            data=body,
            headers=headers,
        )

    def _ensure_prediction_terms_accepted(self) -> None:
        status = self._authenticated_get("/v1/prediction-markets/terms/status")
        if not isinstance(status, Mapping) or status.get("hasAcceptedLatest") is not True:
            raise MarketConfigurationError(
                "Gemini Prediction Markets terms are not accepted for this account. "
                "Accept the latest terms through Gemini before enabling live orders."
            )

    def _url(self, path: str) -> str:
        return f"{self.api_base_url}/{'/'.join(part for part in str(path or '').split('/') if part)}"

    def _auth_headers(self, payload: Mapping[str, Any], *, content_type: str = "text/plain") -> Dict[str, str]:
        api_key = self.resolve_credential(
            "gemini_api_key",
            ("GEMINI_API_KEY",),
            required=True,
            label="GEMINI_API_KEY",
        )
        api_secret = self.resolve_credential(
            "gemini_api_secret",
            ("GEMINI_API_SECRET",),
            required=True,
            label="GEMINI_API_SECRET",
        )
        encoded = base64.b64encode(json.dumps(dict(payload), separators=(",", ":")).encode("utf-8"))
        signature = hmac.new(api_secret.value.encode("utf-8"), encoded, hashlib.sha384).hexdigest()
        return {
            "Content-Type": content_type,
            "Cache-Control": "no-cache",
            "X-GEMINI-APIKEY": api_key.value,
            "X-GEMINI-PAYLOAD": encoded.decode("ascii"),
            "X-GEMINI-SIGNATURE": signature,
        }

    def _event_from_payload(self, event: Mapping[str, Any]) -> MarketEvent:
        event_id = self._event_ticker(event)
        return MarketEvent(
            market_id=self.market_id,
            event_id=event_id,
            title=str(event.get("title") or event.get("name") or event_id),
            url=self._event_url(event),
            status=str(event.get("status") or "").strip().lower(),
            raw=dict(event),
        )

    def _contracts_from_event(self, event: Mapping[str, Any]) -> List[MarketContract]:
        event_ticker = self._event_ticker(event)
        title = str(event.get("title") or event_ticker)
        contracts = []
        for contract in self._list_from_payload(event, "contracts"):
            symbol = self._instrument_symbol(contract)
            if not symbol:
                continue
            outcome = str(
                contract.get("outcome")
                or contract.get("name")
                or contract.get("title")
                or contract.get("side")
                or symbol
            )
            contracts.append(
                MarketContract(
                    market_id=self.market_id,
                    contract_id=self._contract_id(event_ticker, symbol),
                    event_id=event_ticker,
                    title=f"{title} - {outcome}",
                    outcome=outcome,
                    url=self._event_url(event),
                    status=str(contract.get("status") or event.get("status") or "").strip().lower(),
                    raw={"event": dict(event), "contract": dict(contract)},
                )
            )
        return contracts

    def _validate_order(self, order: PaperOrderRequest) -> None:
        self.ensure_order_market(order)
        self._split_contract_id(order.contract_id)
        if str(order.side or "").upper() not in {"BUY", "SELL"}:
            raise MarketConfigurationError("Gemini paper order side must be BUY or SELL.")
        if not self._is_positive_number(order.size):
            raise MarketConfigurationError("Gemini paper order size must be positive.")
        if order.limit_price is not None and self._safe_probability(order.limit_price) is None:
            raise MarketConfigurationError("Gemini paper order limit price must be between 0 and 1.")

    def _live_order_payload(
        self,
        order: PaperOrderRequest,
        *,
        instrument_symbol: str,
    ) -> Dict[str, Any]:
        outcome = self._order_outcome(order, instrument_symbol=instrument_symbol)
        order_type = str(order.metadata.get("order_type") or "limit").strip().lower()
        if order_type not in {"limit", "stop-limit"}:
            raise MarketConfigurationError("Gemini prediction order_type must be limit or stop-limit.")
        time_in_force = str(order.metadata.get("time_in_force") or "good-til-cancel").strip().lower()
        if time_in_force not in {"good-til-cancel", "immediate-or-cancel", "fill-or-kill"}:
            raise MarketConfigurationError(
                "Gemini prediction time_in_force must be good-til-cancel, immediate-or-cancel, or fill-or-kill."
            )
        payload: Dict[str, Any] = {
            "symbol": instrument_symbol,
            "orderType": order_type,
            "side": "buy" if str(order.side or "").upper() == "BUY" else "sell",
            "quantity": str(order.size),
            "price": str(order.limit_price),
            "outcome": outcome,
            "timeInForce": time_in_force,
        }
        stop_price = order.metadata.get("stop_price")
        if order_type == "stop-limit":
            parsed_stop = self._safe_probability(stop_price)
            if parsed_stop is None:
                raise MarketConfigurationError("Gemini stop-limit orders require stop_price between 0 and 1.")
            payload["stopPrice"] = str(parsed_stop)
        elif stop_price is not None:
            raise MarketConfigurationError("Gemini stop_price is only valid for stop-limit orders.")
        if "maker_or_cancel" in order.metadata:
            payload["makerOrCancel"] = bool(order.metadata["maker_or_cancel"])
        client_order_id = order.metadata.get("client_order_id") or order.metadata.get("clientOrderId")
        if client_order_id:
            payload["clientOrderId"] = str(client_order_id)
        return payload

    def _order_outcome(self, order: PaperOrderRequest, *, instrument_symbol: str) -> str:
        candidate = order.metadata.get("outcome")
        if candidate is None:
            suffix = instrument_symbol.rsplit("-", 1)[-1].strip().lower()
            if suffix in {"yes", "no"}:
                candidate = suffix
        outcome = str(candidate or "").strip().lower()
        if outcome not in {"yes", "no"}:
            raise MarketConfigurationError(
                "Gemini live orders require metadata['outcome'] set to 'yes' or 'no'; "
                "the event ticker alone does not identify the outcome."
            )
        return outcome

    @staticmethod
    def _order_management_id(value: Any) -> int:
        text = str(value or "").strip()
        if not text or not text.isdigit():
            raise MarketConfigurationError("Gemini order_id must be a positive integer.")
        parsed = int(text)
        if parsed < 1 or parsed > 9_223_372_036_854_775_807:
            raise MarketConfigurationError("Gemini order_id must fit in a positive int64.")
        return parsed

    @classmethod
    def _order_management_ids(cls, values: Any) -> List[int]:
        if not isinstance(values, (list, tuple)):
            raise MarketConfigurationError("Gemini batch cancellation requires an array of order ids.")
        if not values or len(values) > GEMINI_ORDER_MANAGEMENT_MAX_BATCH:
            raise MarketConfigurationError(
                "Gemini batch cancellation requires between 1 and "
                f"{GEMINI_ORDER_MANAGEMENT_MAX_BATCH} order ids."
            )
        parsed = [cls._order_management_id(value) for value in values]
        if len(set(parsed)) != len(parsed):
            raise MarketConfigurationError("Gemini batch cancellation order ids must be unique.")
        return parsed

    def _next_nonce(self, preferred: Any = None) -> int:
        try:
            requested = int(preferred) if preferred is not None else 0
        except (TypeError, ValueError):
            requested = 0
        now = int(time.time() * 1000)
        previous = int(getattr(self, "_last_nonce", 0) or 0)
        nonce = max(now, requested, previous + 1)
        self._last_nonce = nonce
        return nonce

    @staticmethod
    def _bounded_int(value: Any, label: str, *, minimum: int, maximum: int) -> int:
        if isinstance(value, bool):
            raise MarketConfigurationError(f"Gemini {label} must be an integer.")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"Gemini {label} must be an integer.") from exc
        if parsed < minimum or parsed > maximum:
            raise MarketConfigurationError(f"Gemini {label} must be between {minimum} and {maximum}.")
        return parsed

    @staticmethod
    def _nonempty_text(value: Any, label: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise MarketConfigurationError(f"Gemini {label} cannot be empty.")
        if any(char in text for char in ("/", "\\", "\r", "\n")):
            raise MarketConfigurationError(f"Gemini {label} contains unsupported characters.")
        return text

    @classmethod
    def _history_millis(cls, value: Any, label: str) -> int:
        timestamp = cls._history_timestamp(value, label)
        if timestamp is None or timestamp < 0:
            raise MarketConfigurationError(f"Gemini {label} must be a non-negative timestamp.")
        return int(round(timestamp * 1000))

    @staticmethod
    def _settled_sort(value: Any) -> str:
        sort = str(value or "").strip().lower()
        allowed = {"date", "-date", "payout", "+payout", "-payout"}
        if sort not in allowed:
            raise MarketConfigurationError("Gemini settled position sort must be date, -date, payout, +payout, or -payout.")
        return sort

    @staticmethod
    def _event_ticker(event: Mapping[str, Any]) -> str:
        return str(event.get("ticker") or event.get("eventTicker") or event.get("id") or event.get("slug") or "").strip()

    @staticmethod
    def _instrument_symbol(contract: Mapping[str, Any]) -> str:
        return str(
            contract.get("instrumentSymbol")
            or contract.get("instrument_symbol")
            or contract.get("symbol")
            or contract.get("id")
            or ""
        ).strip()

    @staticmethod
    def _contract_id(event_ticker: str, instrument_symbol: str) -> str:
        return f"{event_ticker}:{instrument_symbol}"

    @staticmethod
    def _split_contract_id(contract_id: str) -> Tuple[str, str]:
        raw = str(contract_id or "").strip()
        if not raw:
            raise MarketConfigurationError("Gemini contract id cannot be empty.")
        if ":" not in raw:
            return raw, raw
        event_ticker, instrument_symbol = raw.split(":", 1)
        if not event_ticker.strip() or not instrument_symbol.strip():
            raise MarketConfigurationError("Gemini contract id must be EVENT_TICKER:INSTRUMENT_SYMBOL.")
        return event_ticker.strip(), instrument_symbol.strip()

    @staticmethod
    def _event_url(event: Mapping[str, Any]) -> str:
        raw = str(event.get("url") or "").strip()
        if raw:
            return raw
        ticker = GeminiPredictionAdapter._event_ticker(event)
        return f"https://www.gemini.com/prediction-markets/{ticker}" if ticker else "https://www.gemini.com"

    @staticmethod
    def _list_from_payload(payload: Any, *keys: str) -> List[Mapping[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, Mapping)]
        if isinstance(payload, Mapping):
            for key in keys:
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, Mapping)]
            data = payload.get("data")
            if isinstance(data, list):
                return [item for item in data if isinstance(item, Mapping)]
        return []

    @staticmethod
    def _book_levels(raw: Any, *, descending: bool = False) -> List[OrderBookLevel]:
        levels: List[OrderBookLevel] = []
        if not isinstance(raw, list):
            return levels
        for item in raw:
            price = size = None
            if isinstance(item, Mapping):
                price = item.get("price")
                size = item.get("amount") or item.get("size") or item.get("quantity")
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                price, size = item[0], item[1]
            parsed_price = GeminiPredictionAdapter._safe_probability(price)
            try:
                parsed_size = float(size)
            except (TypeError, ValueError):
                continue
            if parsed_price is not None and GeminiPredictionAdapter._is_positive_number(parsed_size):
                levels.append(OrderBookLevel(price=parsed_price, size=parsed_size))
        levels.sort(key=lambda level: level.price, reverse=descending)
        return levels

    @staticmethod
    def _history_timestamp(value: Any, label: str) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            number = float(value)
            if not math.isfinite(number):
                return None
            return number / 1000.0 if number > 100_000_000_000 else number
        raw = str(value).strip()
        if not raw:
            return None
        try:
            parsed = float(raw)
            if math.isfinite(parsed):
                return parsed / 1000.0 if parsed > 100_000_000_000 else parsed
        except ValueError:
            pass
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except (TypeError, ValueError, OverflowError) as exc:
            raise MarketConfigurationError(
                f"Gemini {label} must be a Unix timestamp or ISO-8601 value."
            ) from exc

    @staticmethod
    def _positive_number(value: Any) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and number > 0 else None

    @staticmethod
    def _value_at(data: Any, *keys: str) -> Any:
        if not isinstance(data, Mapping):
            return []
        for key in keys:
            value = data.get(key)
            if value is not None:
                return value
        return []

    @staticmethod
    def _safe_probability(value: Any) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        if number > 1.0 and number <= 100.0:
            number /= 100.0
        if 0.0 <= number <= 1.0:
            return number
        return None

    @staticmethod
    def _is_positive_number(value: Any) -> bool:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False
        return math.isfinite(number) and number > 0
