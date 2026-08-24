from __future__ import annotations

import math
import re
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
    MarketTrade,
    PaperOrderRequest,
    PaperOrderResult,
    PriceSnapshot,
)


DEFAULT_XMARKET_BASE_URL = "https://engine.xmarket.app/api/v1"
DEFAULT_XMARKET_AUTH_BASE_URL = "https://engine.xmarket.app/openapi/v1"
XMARKET_REFERENCES = (
    "https://docs.xmarket.app/developers/quick-start",
    "https://docs.xmarket.app/developers/markets",
    "https://docs.xmarket.app/developers/orderbook",
    "https://docs.xmarket.app/developers/orders",
    "https://docs.xmarket.app/developers/positions",
)

XMARKET_ACCOUNT_OPERATIONS = ("positions", "user_orders", "market_orders")
XMARKET_ACCOUNT_STATUSES = ("all", "open", "partially_filled", "filled", "cancelled", "expired")
XMARKET_POSITION_STATUSES = ("open", "closed", "settled")
XMARKET_ORDER_MANAGEMENT_OPERATIONS = ("batch_create_orders", "batch_cancel_orders")
XMARKET_ORDER_MANAGEMENT_CONFIRMATION = "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
XMARKET_ORDER_MANAGEMENT_MAX_BATCH = 100
XMARKET_HISTORY_MAX_LIMIT = 100


class XMarketAdapter(MarketAdapter):
    """Xmarket adapter for documented market-data and guarded order endpoints."""

    metadata = get_market_metadata("xmarket")
    account_recovery_operations = XMARKET_ACCOUNT_OPERATIONS
    order_management_operations = XMARKET_ORDER_MANAGEMENT_OPERATIONS

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        credential = self.resolve_credential("xmarket_api_key", ("XMARKET_API_KEY",), label="XMARKET_API_KEY")
        health.update(
            {
                "api_base_url": self.api_base_url,
                "authenticated_api_base_url": self.authenticated_api_base_url,
                "account_recovery_operations": list(self.account_recovery_operations),
                "authenticated_account_endpoints": [
                    "GET /positions",
                    "GET /order/my-orders",
                    "GET /order/market/:marketId",
                ],
                "trade_history_source": "authenticated_my_orders_filled",
                "trade_history_account_scoped": True,
                "candle_history_source": "bounded_derived_account_fills",
                "candle_history_derived": True,
                "copy_trading_source": "authenticated_my_orders_filled",
                "copy_trading_simulation_only": True,
                "history_page_limit": XMARKET_HISTORY_MAX_LIMIT,
                "references": list(XMARKET_REFERENCES),
                "order_management_operations": list(self.order_management_operations),
                "order_management_enabled": self.config_bool("xmarket_order_management_enabled", False),
                "authenticated_order_management_endpoints": [
                    "POST /order/batch",
                    "POST /order/cancel-batch",
                ],
                "live_trading_enabled": self.config_bool("live_trading_enabled", False),
                "credential_sources": ([{"name": credential.name, "source": credential.source}] if credential else []),
            }
        )
        return health

    @property
    def api_base_url(self) -> str:
        configured = self.config.get("xmarket_api_base_url") or self.config.get("api_base_url")
        return str(configured or DEFAULT_XMARKET_BASE_URL).rstrip("/")

    @property
    def authenticated_api_base_url(self) -> str:
        configured = self.config.get("xmarket_authenticated_api_base_url")
        return str(configured or DEFAULT_XMARKET_AUTH_BASE_URL).rstrip("/")

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = max(1, min(int(limit or 50), 100))
        payload = self._get(
            "/markets",
            params={
                "status": str(self.config.get("xmarket_market_status") or "live"),
                "page": 1,
                "pageSize": desired,
            },
        )
        markets = self._items(payload)
        needle = str(query or "").strip().lower()
        if needle:
            markets = [market for market in markets if self._matches_query(market, needle)]
        return [self._event_from_market(market) for market in markets[:desired]]

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        market = self._get_market(event_id)
        return self._contracts_from_market(market)

    def get_orderbook(self, contract_id: str) -> OrderBookSnapshot:
        self.ensure_capability("orderbook_reading")
        market_id, outcome_id = self._split_contract_id(contract_id)
        payload = self._get(f"/orderbook/{outcome_id}")
        book = self._unwrap_book(payload)
        bids = self._levels(book.get("bids"), descending=True)
        asks = self._levels(book.get("asks"))
        return OrderBookSnapshot(
            market_id=self.market_id,
            contract_id=self._contract_id(market_id, outcome_id),
            bids=bids,
            asks=asks,
            raw=dict(book),
        )

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        market_id, outcome_id = self._split_contract_id(contract_id)
        book = self.get_orderbook(self._contract_id(market_id, outcome_id))
        bid = book.bids[0].price if book.bids else None
        ask = book.asks[0].price if book.asks else None
        midpoint = (bid + ask) / 2.0 if bid is not None and ask is not None else None
        last = midpoint
        raw: Dict[str, Any] = dict(book.raw)
        if last is None:
            market = self._get_market(market_id)
            outcome = self._find_outcome(market, outcome_id)
            last = self._safe_probability(self._value_at(outcome or {}, "price", "probability", "lastPrice"))
            raw["market"] = dict(market)
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=self._contract_id(market_id, outcome_id),
            last=last,
            bid=bid,
            ask=ask,
            midpoint=midpoint,
            source="xmarket_orderbook",
            raw=raw,
        )

    def list_trades(
        self,
        contract_id: str,
        *,
        limit: int = 50,
        before: Optional[float] = None,
        after: Optional[float] = None,
    ) -> List[MarketTrade]:
        """Normalize bounded, account-scoped fills from Xmarket order history.

        Xmarket documents ``GET /order/my-orders`` and its ``filled`` and
        ``partially_filled`` statuses, but it does not publish a public trade
        tape.  We therefore expose only rows belonging to the authenticated
        account and requested contract, requiring explicit fill quantity,
        price, side, and timestamp fields before normalizing them.
        """

        self.ensure_capability("trade_history")
        market_id, outcome_id = self._split_contract_id(contract_id)
        desired = self._history_limit(limit)
        before_ts = self._history_timestamp(before, "before") if before is not None else None
        after_ts = self._history_timestamp(after, "after") if after is not None else None
        if before_ts is not None and after_ts is not None and before_ts < after_ts:
            raise MarketConfigurationError("Xmarket trade history requires before to be at or after after.")

        payload = self._get_authenticated(
            "/order/my-orders",
            params={"status": "all", "page": 1, "pageSize": desired},
        )
        trades: List[MarketTrade] = []
        for row in self._items(payload):
            status = str(row.get("status") or "").strip().lower()
            if status not in {"filled", "partially_filled"}:
                continue
            row_market = str(self._value_at(row, "marketId", "market_id") or "").strip()
            row_outcome = str(self._value_at(row, "outcomeId", "outcome_id") or "").strip()
            if row_market != market_id or row_outcome != outcome_id:
                continue
            side = str(row.get("side") or "").strip().upper()
            if side not in {"BUY", "SELL"}:
                continue
            filled_quantity = self._value_at(
                row,
                "filledQuantity",
                "filled_quantity",
                "executedQuantity",
                "executed_quantity",
            )
            if filled_quantity is None and status == "filled":
                filled_quantity = self._value_at(row, "quantity", "size")
            size = self._positive_number(filled_quantity)
            price = self._safe_probability(
                self._value_at(
                    row,
                    "averagePrice",
                    "average_price",
                    "averageFillPrice",
                    "average_fill_price",
                    "filledPrice",
                    "fillPrice",
                    "price",
                )
            )
            timestamp = self._timestamp_seconds(
                self._value_at(
                    row,
                    "filledAt",
                    "filled_at",
                    "executedAt",
                    "executed_at",
                    "updatedAt",
                    "updated_at",
                    "timestamp",
                    "createdAt",
                    "created_at",
                )
            )
            trade_id = str(
                self._value_at(row, "tradeId", "trade_id", "fillId", "fill_id", "id") or ""
            ).strip()
            if size is None or price is None or price <= 0.0 or price >= 1.0 or timestamp is None or not trade_id:
                continue
            if before_ts is not None and timestamp > before_ts:
                continue
            if after_ts is not None and timestamp < after_ts:
                continue
            trades.append(
                MarketTrade(
                    market_id=self.market_id,
                    contract_id=self._contract_id(market_id, outcome_id),
                    trade_id=trade_id,
                    side=side,
                    price=price,
                    size=size,
                    timestamp=timestamp,
                    raw={
                        "source": "xmarket_authenticated_my_orders",
                        "account_scoped": True,
                        "order": dict(row),
                    },
                )
            )
            if len(trades) >= desired:
                break
        return trades

    def list_candles(
        self,
        contract_id: str,
        *,
        resolution: str = "1h",
        from_timestamp: Optional[float] = None,
        to_timestamp: Optional[float] = None,
    ) -> List[MarketCandle]:
        """Derive bounded OHLCV candles from authenticated Xmarket fills.

        This is intentionally not a public market candle feed: each candle is
        derived only from the account-scoped fills returned by ``my-orders``.
        """

        self.ensure_capability("candle_history")
        interval = self._candle_interval(resolution)
        start_ts = (
            self._history_timestamp(from_timestamp, "from_timestamp")
            if from_timestamp is not None
            else None
        )
        end_ts = (
            self._history_timestamp(to_timestamp, "to_timestamp")
            if to_timestamp is not None
            else None
        )
        if start_ts is not None and end_ts is not None and end_ts < start_ts:
            raise MarketConfigurationError("Xmarket candle history requires to_timestamp at or after from_timestamp.")

        trades = self.list_trades(
            contract_id,
            limit=XMARKET_HISTORY_MAX_LIMIT,
            before=end_ts,
            after=start_ts,
        )
        buckets: Dict[int, Dict[str, Any]] = {}
        for trade in sorted(trades, key=lambda item: (item.timestamp or 0.0, item.trade_id)):
            if trade.timestamp is None:
                continue
            bucket = int(math.floor(trade.timestamp / interval) * interval)
            current = buckets.get(bucket)
            if current is None:
                current = {
                    "open": trade.price,
                    "high": trade.price,
                    "low": trade.price,
                    "close": trade.price,
                    "volume": 0.0,
                    "trade_ids": [],
                }
                buckets[bucket] = current
            current["high"] = max(float(current["high"]), trade.price)
            current["low"] = min(float(current["low"]), trade.price)
            current["close"] = trade.price
            current["volume"] = float(current["volume"]) + trade.size
            current["trade_ids"].append(trade.trade_id)

        market_id, outcome_id = self._split_contract_id(contract_id)
        candles: List[MarketCandle] = []
        for timestamp in sorted(buckets):
            values = buckets[timestamp]
            candles.append(
                MarketCandle(
                    market_id=self.market_id,
                    contract_id=self._contract_id(market_id, outcome_id),
                    timestamp=float(timestamp),
                    open=float(values["open"]),
                    high=float(values["high"]),
                    low=float(values["low"]),
                    close=float(values["close"]),
                    volume=float(values["volume"]),
                    raw={
                        "source": "xmarket_authenticated_my_orders",
                        "account_scoped": True,
                        "derived": True,
                        "trade_ids": list(values["trade_ids"]),
                    },
                )
            )
        return candles

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        self._validate_order(order)
        payload = self._order_payload(order)
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=order.contract_id,
            accepted=True,
            message=(
                f"DRY RUN: would place Xmarket {order.side.upper()} order for {order.size:g} shares"
                + (f" at limit {order.limit_price:.4f}" if order.limit_price is not None else "")
            ),
            raw={"request": payload},
        )

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        self.ensure_capability("live_trading")
        self._validate_order(order)
        preflight = self.preflight_live_order(order)
        payload = self._order_payload(order)
        response = self._post("/order", payload)
        return {
            "market_id": self.market_id,
            "contract_id": order.contract_id,
            "live": True,
            "preflight": preflight,
            "request": payload,
            "response": response,
        }

    def account_recovery(self, operation: str, **kwargs: Any) -> Any:
        """Read Xmarket's documented API-key account surfaces.

        The public ``/api/v1`` surface exposes positions while the documented
        order reads live under ``/openapi/v1``. Each operation is explicitly
        allow-listed and path-bearing market identifiers are validated before
        a request is issued.
        """

        normalized = str(operation or "").strip().lower()
        if normalized not in self.account_recovery_operations:
            supported = ", ".join(self.account_recovery_operations)
            raise MarketConfigurationError(f"Xmarket account operation must be one of: {supported}.")

        page = self._account_page(kwargs.get("page"))
        page_size = self._account_page_size(kwargs.get("page_size", kwargs.get("limit")))
        if normalized == "positions":
            status = self._account_status(
                kwargs.get("status"),
                default="open",
                allowed=XMARKET_POSITION_STATUSES,
                label="position status",
            )
            return self._get(
                "/positions",
                params={"status": status, "page": page, "pageSize": page_size},
            )

        status = self._account_status(
            kwargs.get("status"),
            default="all" if normalized == "user_orders" else "open",
            allowed=XMARKET_ACCOUNT_STATUSES,
            label="order status",
        )
        params = {"status": status, "page": page, "pageSize": page_size}
        if normalized == "user_orders":
            return self._get_authenticated("/order/my-orders", params=params)

        market_id = self._safe_path_segment(
            kwargs.get("market_id") or kwargs.get("marketId"),
            "Xmarket account market id",
        )
        return self._get_authenticated(f"/order/market/{market_id}", params=params)

    def manage_orders(self, operation: str, **kwargs: Any) -> Dict[str, Any]:
        """Run a guarded Xmarket batch order mutation.

        Xmarket documents fixed authenticated ``POST /order/batch`` and
        ``POST /order/cancel-batch`` endpoints.  Only those two operations are
        exposed here; callers cannot provide an arbitrary path or method.
        """

        normalized = str(operation or "").strip().lower()
        if normalized not in self.order_management_operations:
            supported = ", ".join(self.order_management_operations)
            raise MarketConfigurationError(f"Xmarket order-management operation must be one of: {supported}.")
        self.ensure_capability("live_trading")
        if not self.config_bool("xmarket_order_management_enabled", False):
            raise MarketConfigurationError(
                "Xmarket order management is disabled by adapter config. "
                "Set xmarket_order_management_enabled=true only after reviewing live-order risk."
            )
        self.ensure_live_trading_enabled("Xmarket order management")
        if str(kwargs.get("confirm_order_management") or "").strip() != XMARKET_ORDER_MANAGEMENT_CONFIRMATION:
            raise MarketConfigurationError(
                "Xmarket order management requires exact confirmation text "
                f"{XMARKET_ORDER_MANAGEMENT_CONFIRMATION}."
            )
        if bool(kwargs.get("async_request")):
            raise MarketConfigurationError("Xmarket order-management requests are synchronous.")

        if normalized == "batch_create_orders":
            orders = self._batch_order_payloads(kwargs.get("orders", kwargs.get("instructions")))
            request: Dict[str, Any] = {"orders": orders}
            path = "/order/batch"
        else:
            order_ids = self._batch_order_ids(kwargs.get("order_ids", kwargs.get("orders", kwargs.get("instructions"))))
            request = {"orderIds": order_ids}
            path = "/order/cancel-batch"
        response = self._post(path, request)
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
                "references": list(XMARKET_REFERENCES),
            },
            "request": request,
            "response": response,
        }

    def copy_trade_from_activity(self, activity: Mapping[str, Any]) -> PaperOrderResult:
        """Build a local copy preview from an authenticated filled order.

        Xmarket's documented ``GET /order/my-orders`` response contains the
        account's filled/partially-filled order rows.  Copying is deliberately
        simulation-first: this validates one complete fill and forwards it to
        ``place_paper_order``; it never calls a live order endpoint.
        """

        self.ensure_capability("copy_trading")
        status = str(activity.get("status") or "").strip().lower()
        if status not in {"filled", "partially_filled"}:
            raise MarketConfigurationError(
                "Xmarket copy activity must have filled or partially_filled status."
            )

        market_id = str(activity.get("market_id") or activity.get("marketId") or "").strip()
        outcome_id = str(activity.get("outcome_id") or activity.get("outcomeId") or "").strip()
        contract_id = str(activity.get("contract_id") or activity.get("asset") or "").strip()
        if contract_id:
            parsed_market, parsed_outcome = self._split_contract_id(contract_id)
            if market_id and parsed_market != market_id:
                raise MarketConfigurationError("Xmarket activity market_id does not match contract_id.")
            if outcome_id and parsed_outcome != outcome_id:
                raise MarketConfigurationError("Xmarket activity outcome_id does not match contract_id.")
            market_id, outcome_id = parsed_market, parsed_outcome
        elif market_id and outcome_id:
            contract_id = self._contract_id(market_id, outcome_id)
        else:
            raise MarketConfigurationError(
                "Xmarket activity requires contract_id or both market_id and outcome_id."
            )

        side = str(activity.get("side") or "").strip().upper()
        if side not in {"BUY", "SELL"}:
            raise MarketConfigurationError("Xmarket activity side must be BUY or SELL.")
        filled_quantity = self._value_at(
            activity,
            "filledQuantity",
            "filled_quantity",
            "executedQuantity",
            "executed_quantity",
        )
        if filled_quantity is None and status == "filled":
            filled_quantity = self._value_at(activity, "quantity", "size")
        size = self._positive_number(filled_quantity)
        if size is None:
            raise MarketConfigurationError("Xmarket activity filled quantity must be positive and finite.")
        raw_price = self._value_at(
            activity,
            "averagePrice",
            "average_price",
            "averageFillPrice",
            "average_fill_price",
            "filledPrice",
            "fillPrice",
            "price",
        )
        price = self._safe_probability(raw_price)
        if price is None or price <= 0.0 or price >= 1.0:
            raise MarketConfigurationError("Xmarket activity fill price must be between 0 and 1.")
        trade_id = str(
            self._value_at(activity, "tradeId", "trade_id", "fillId", "fill_id", "id") or ""
        ).strip()
        if not trade_id:
            raise MarketConfigurationError("Xmarket activity requires a documented order or fill id.")

        preview = self.place_paper_order(
            PaperOrderRequest(
                market_id=self.market_id,
                contract_id=contract_id,
                side=side,
                size=size,
                limit_price=price,
                metadata={
                    "activity": dict(activity),
                    "source": "xmarket_authenticated_my_orders",
                    "account_scoped": True,
                },
            )
        )
        preview.raw["source"] = "xmarket_authenticated_my_orders"
        preview.raw["account_scoped"] = True
        preview.raw["activity"] = dict(activity)
        return preview

    def _get_market(self, market_id: str) -> Mapping[str, Any]:
        clean = self._safe_path_segment(market_id, "Xmarket market id")
        payload = self._get(f"/markets/{clean}")
        if isinstance(payload, Mapping):
            data = payload.get("data")
            if isinstance(data, Mapping):
                return data
            return payload
        raise MarketConfigurationError(f"Xmarket market {clean!r} was not found.")

    def _get(self, path: str, *, params: Optional[Mapping[str, Any]] = None) -> Any:
        return self.runtime.get_json(self._url(self.api_base_url, path), params=params, headers=self._headers())

    def _get_authenticated(self, path: str, *, params: Optional[Mapping[str, Any]] = None) -> Any:
        return self.runtime.get_json(
            self._url(self.authenticated_api_base_url, path),
            params=params,
            headers=self._headers(),
        )

    def _post(self, path: str, payload: Mapping[str, Any]) -> Any:
        return self.runtime.request_json(
            "POST",
            self._url(self.authenticated_api_base_url, path),
            json_body=dict(payload),
            headers=self._headers(),
        )

    def _headers(self) -> Dict[str, str]:
        key = self.resolve_credential("xmarket_api_key", ("XMARKET_API_KEY",), required=True, label="XMARKET_API_KEY")
        return {"x-api-key": key.value, "Content-Type": "application/json"}

    def _event_from_market(self, market: Mapping[str, Any]) -> MarketEvent:
        market_id = self._market_id(market)
        return MarketEvent(
            market_id=self.market_id,
            event_id=market_id,
            title=str(market.get("name") or market.get("title") or market.get("question") or market_id),
            url=str(market.get("url") or self._market_url(market_id)),
            status=str(market.get("status") or "").strip().lower(),
            raw=dict(market),
        )

    def _contracts_from_market(self, market: Mapping[str, Any]) -> List[MarketContract]:
        market_id = self._market_id(market)
        title = str(market.get("name") or market.get("title") or market.get("question") or market_id)
        contracts: List[MarketContract] = []
        for outcome in self._outcomes(market):
            outcome_id = self._outcome_id(outcome)
            if not outcome_id:
                continue
            label = str(outcome.get("name") or outcome.get("label") or outcome.get("title") or outcome_id)
            contracts.append(
                MarketContract(
                    market_id=self.market_id,
                    contract_id=self._contract_id(market_id, outcome_id),
                    event_id=market_id,
                    title=f"{title} - {label}",
                    outcome=label,
                    url=str(market.get("url") or self._market_url(market_id)),
                    status=str(market.get("status") or "").strip().lower(),
                    raw={"market": dict(market), "outcome": dict(outcome)},
                )
            )
        return contracts

    def _order_payload(self, order: PaperOrderRequest) -> Dict[str, Any]:
        _, outcome_id = self._split_contract_id(order.contract_id)
        side = str(order.side or "").strip().lower()
        order_type = str(order.metadata.get("type") or ("limit" if order.limit_price is not None else "market")).lower()
        payload: Dict[str, Any] = {
            "outcomeId": outcome_id,
            "side": side,
            "type": order_type,
            "quantity": float(order.size),
        }
        if order.limit_price is not None:
            payload["price"] = self._price(order.limit_price)
        return payload

    def _validate_order(self, order: PaperOrderRequest) -> None:
        self.ensure_order_market(order)
        self._split_contract_id(order.contract_id)
        if str(order.side or "").upper() not in {"BUY", "SELL"}:
            raise MarketConfigurationError("Xmarket order side must be BUY or SELL.")
        try:
            quantity = float(order.size)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Xmarket order quantity must be numeric.") from exc
        if not math.isfinite(quantity) or quantity <= 0:
            raise MarketConfigurationError("Xmarket order quantity must be positive and finite.")
        if order.limit_price is not None:
            self._price(order.limit_price)

    @classmethod
    def _batch_order_ids(cls, value: Any) -> List[str]:
        if not isinstance(value, (list, tuple)) or not value:
            raise MarketConfigurationError("Xmarket batch cancellation requires a non-empty order id list.")
        if len(value) > XMARKET_ORDER_MANAGEMENT_MAX_BATCH:
            raise MarketConfigurationError(
                f"Xmarket batch cancellation accepts at most {XMARKET_ORDER_MANAGEMENT_MAX_BATCH} order ids."
            )
        order_ids = [cls._order_management_id(item) for item in value]
        if len(set(order_ids)) != len(order_ids):
            raise MarketConfigurationError("Xmarket batch cancellation order ids must be unique.")
        return order_ids

    @classmethod
    def _order_management_id(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,199}", normalized):
            raise MarketConfigurationError("Xmarket order id must be a short path-safe identifier.")
        return normalized

    def _batch_order_payloads(self, value: Any) -> List[Dict[str, Any]]:
        if not isinstance(value, (list, tuple)) or not value:
            raise MarketConfigurationError("Xmarket batch creation requires a non-empty order list.")
        if len(value) > XMARKET_ORDER_MANAGEMENT_MAX_BATCH:
            raise MarketConfigurationError(
                f"Xmarket batch creation accepts at most {XMARKET_ORDER_MANAGEMENT_MAX_BATCH} orders."
            )
        payloads: List[Dict[str, Any]] = []
        for item in value:
            if not isinstance(item, Mapping):
                raise MarketConfigurationError("Xmarket batch orders must be JSON objects.")
            allowed = {"outcomeId", "outcome_id", "side", "type", "price", "quantity"}
            unknown = sorted(str(key) for key in item.keys() if str(key) not in allowed)
            if unknown:
                raise MarketConfigurationError(
                    "Xmarket batch order contains unsupported fields: " + ", ".join(unknown)
                )
            outcome_id = self._safe_path_segment(
                item.get("outcomeId") or item.get("outcome_id"), "Xmarket outcome id"
            )
            side = str(item.get("side") or "").strip().lower()
            if side not in {"buy", "sell"}:
                raise MarketConfigurationError("Xmarket batch order side must be buy or sell.")
            order_type = str(item.get("type") or "limit").strip().lower()
            if order_type not in {"limit", "market"}:
                raise MarketConfigurationError("Xmarket batch order type must be limit or market.")
            try:
                quantity = float(item.get("quantity"))
            except (TypeError, ValueError) as exc:
                raise MarketConfigurationError("Xmarket batch order quantity must be numeric.") from exc
            if not math.isfinite(quantity) or quantity <= 0:
                raise MarketConfigurationError("Xmarket batch order quantity must be positive and finite.")
            max_size = self._positive_config_float("live_trading_max_size")
            max_notional = self._positive_config_float("live_trading_max_notional")
            if max_size is not None and quantity > max_size:
                raise MarketConfigurationError(
                    f"Xmarket batch order size {quantity:g} exceeds configured max {max_size:g}."
                )
            payload: Dict[str, Any] = {
                "outcomeId": outcome_id,
                "side": side,
                "type": order_type,
                "quantity": quantity,
            }
            if order_type == "limit":
                if item.get("price") is None:
                    raise MarketConfigurationError("Xmarket limit batch orders require a price.")
                payload["price"] = self._price(item.get("price"))
                if max_notional is not None and quantity * payload["price"] > max_notional:
                    raise MarketConfigurationError(
                        "Xmarket batch order notional "
                        f"{quantity * payload['price']:g} exceeds configured max {max_notional:g}."
                    )
            else:
                if item.get("price") is not None:
                    raise MarketConfigurationError("Xmarket market batch orders must not include a price.")
                if max_notional is not None and quantity > max_notional:
                    raise MarketConfigurationError(
                        f"Xmarket batch order notional {quantity:g} exceeds configured max {max_notional:g}."
                    )
            payloads.append(payload)
        return payloads

    @staticmethod
    def _items(payload: Any) -> List[Mapping[str, Any]]:
        if isinstance(payload, Mapping):
            data = payload.get("data")
            if isinstance(data, list):
                payload = data
            elif isinstance(data, Mapping):
                nested = XMarketAdapter._items(data)
                if nested:
                    return nested
                payload = data
            else:
                payload = payload.get(
                    "items",
                    payload.get("markets", payload.get("orders", [])),
                )
            if isinstance(payload, Mapping):
                payload = payload.get("items", payload.get("markets", payload.get("orders", [])))
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, Mapping)]
        return []

    @staticmethod
    def _matches_query(market: Mapping[str, Any], query: str) -> bool:
        values = (market.get("name"), market.get("title"), market.get("question"), market.get("description"), market.get("category"))
        return query in " ".join(str(value or "") for value in values).lower()

    @staticmethod
    def _market_id(market: Mapping[str, Any]) -> str:
        return str(market.get("id") or market.get("marketId") or market.get("market_id") or "").strip()

    @staticmethod
    def _outcomes(market: Mapping[str, Any]) -> List[Mapping[str, Any]]:
        outcomes = market.get("outcomes")
        if not isinstance(outcomes, list):
            return []
        return [outcome if isinstance(outcome, Mapping) else {"name": str(outcome)} for outcome in outcomes]

    @staticmethod
    def _find_outcome(market: Mapping[str, Any], outcome_id: str) -> Optional[Mapping[str, Any]]:
        for outcome in XMarketAdapter._outcomes(market):
            if XMarketAdapter._outcome_id(outcome) == str(outcome_id):
                return outcome
        return None

    @staticmethod
    def _outcome_id(outcome: Mapping[str, Any]) -> str:
        return str(outcome.get("id") or outcome.get("outcomeId") or outcome.get("outcome_id") or "").strip()

    @staticmethod
    def _unwrap_book(payload: Any) -> Mapping[str, Any]:
        if not isinstance(payload, Mapping):
            return {}
        for key in ("orderbook", "data"):
            value = payload.get(key)
            if isinstance(value, Mapping) and ("bids" in value or "asks" in value):
                return value
        return payload

    @staticmethod
    def _levels(raw: Any, *, descending: bool = False) -> List[OrderBookLevel]:
        if not isinstance(raw, list):
            return []
        levels: List[OrderBookLevel] = []
        for item in raw:
            if isinstance(item, Mapping):
                price = item.get("price") or item.get("rate")
                size = item.get("quantity") or item.get("size") or item.get("amount") or item.get("volume")
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                price, size = item[0], item[1]
            else:
                continue
            parsed_price = XMarketAdapter._safe_probability(price)
            try:
                parsed_size = float(size)
            except (TypeError, ValueError):
                continue
            if parsed_price is not None and math.isfinite(parsed_size) and parsed_size > 0:
                levels.append(OrderBookLevel(price=parsed_price, size=parsed_size))
        levels.sort(key=lambda level: level.price, reverse=descending)
        return levels

    @staticmethod
    def _safe_probability(value: Any) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        if 1.0 < number <= 100.0:
            number /= 100.0
        return number if 0.0 <= number <= 1.0 else None

    @staticmethod
    def _positive_number(value: Any) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and number > 0.0 else None

    @staticmethod
    def _timestamp_seconds(value: Any) -> Optional[float]:
        if value in (None, "") or isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            try:
                number = float(value)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(number):
                return None
            return number / 1000.0 if number > 100_000_000_000 else number
        text = str(value).strip()
        if not text:
            return None
        try:
            number = float(text)
        except (TypeError, ValueError):
            number = None
        if number is not None and math.isfinite(number):
            return number / 1000.0 if number > 100_000_000_000 else number
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        timestamp = parsed.timestamp()
        return timestamp if math.isfinite(timestamp) else None

    @classmethod
    def _history_timestamp(cls, value: Any, label: str) -> float:
        timestamp = cls._timestamp_seconds(value)
        if timestamp is None or timestamp < 0.0 or not math.isfinite(timestamp):
            raise MarketConfigurationError(
                f"Xmarket {label} timestamp must be a valid non-negative epoch or ISO time."
            )
        return timestamp

    @staticmethod
    def _history_limit(value: Any) -> int:
        if isinstance(value, bool):
            raise MarketConfigurationError(
                f"Xmarket trade limit must be an integer between 1 and {XMARKET_HISTORY_MAX_LIMIT}."
            )
        try:
            desired = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(
                f"Xmarket trade limit must be an integer between 1 and {XMARKET_HISTORY_MAX_LIMIT}."
            ) from exc
        if desired < 1 or desired > XMARKET_HISTORY_MAX_LIMIT:
            raise MarketConfigurationError(
                f"Xmarket trade limit must be between 1 and {XMARKET_HISTORY_MAX_LIMIT}."
            )
        return desired

    @staticmethod
    def _candle_interval(resolution: Any) -> int:
        normalized = str(resolution or "").strip().lower()
        intervals = {
            "1m": 60,
            "5m": 300,
            "15m": 900,
            "30m": 1800,
            "1h": 3600,
            "4h": 14_400,
            "1d": 86_400,
            "1w": 604_800,
        }
        if normalized not in intervals:
            raise MarketConfigurationError(
                "Xmarket candle resolution must be one of: " + ", ".join(intervals)
            )
        return intervals[normalized]

    @staticmethod
    def _price(value: Any) -> float:
        price = XMarketAdapter._safe_probability(value)
        if price is None or price <= 0.0 or price >= 1.0:
            raise MarketConfigurationError("Xmarket price must be greater than 0 and less than 1.")
        return price

    @staticmethod
    def _value_at(data: Mapping[str, Any], *keys: str) -> Any:
        for key in keys:
            if data.get(key) is not None:
                return data[key]
        return None

    @staticmethod
    def _contract_id(market_id: str, outcome_id: str) -> str:
        return f"{market_id}:{outcome_id}"

    @staticmethod
    def _split_contract_id(contract_id: str) -> Tuple[str, str]:
        market_id, separator, outcome_id = str(contract_id or "").partition(":")
        if not separator or not market_id.strip() or not outcome_id.strip():
            raise MarketConfigurationError("Xmarket contract id must be MARKET_ID:OUTCOME_ID.")
        return (
            XMarketAdapter._safe_path_segment(market_id, "Xmarket market id"),
            XMarketAdapter._safe_path_segment(outcome_id, "Xmarket outcome id"),
        )

    @staticmethod
    def _safe_path_segment(value: Any, label: str) -> str:
        normalized = str(value or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,199}", normalized):
            raise MarketConfigurationError(f"{label} must be a short path-safe identifier.")
        return normalized

    @staticmethod
    def _account_page(value: Any) -> int:
        if value in (None, ""):
            return 1
        try:
            page = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Xmarket account page must be an integer.") from exc
        if page < 1 or page > 10000:
            raise MarketConfigurationError("Xmarket account page must be between 1 and 10000.")
        return page

    @staticmethod
    def _account_page_size(value: Any) -> int:
        if value in (None, ""):
            return 50
        try:
            page_size = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Xmarket account page size must be an integer.") from exc
        if page_size < 1 or page_size > 1000:
            raise MarketConfigurationError("Xmarket account page size must be between 1 and 1000.")
        return page_size

    @staticmethod
    def _account_status(value: Any, *, default: str, allowed: Tuple[str, ...], label: str) -> str:
        status = str(value or default).strip().lower()
        if status not in allowed:
            raise MarketConfigurationError(f"Xmarket {label} must be one of: {', '.join(allowed)}.")
        return status

    @staticmethod
    def _market_url(market_id: str) -> str:
        return f"https://xmarket.app/market/{market_id}" if market_id else "https://xmarket.app"

    @staticmethod
    def _url(base_url: str, path: str) -> str:
        return f"{base_url.rstrip('/')}/{str(path or '').strip('/')}"

