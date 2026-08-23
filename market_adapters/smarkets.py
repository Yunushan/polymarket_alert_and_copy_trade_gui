from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .base import MarketAdapter
from .catalog import get_market_metadata
from .errors import MarketConfigurationError, MarketHTTPError, UnsupportedFeatureError
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


DEFAULT_SMARKETS_API_BASE_URL = "https://api.smarkets.com/v3"
SMARKETS_ACCOUNT_OPERATIONS = ("order_history", "account")
SMARKETS_ORDER_MANAGEMENT_OPERATIONS = ("cancel_order", "cancel_orders")
SMARKETS_ORDER_MANAGEMENT_CONFIRMATION = "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
SMARKETS_ORDER_STATES = ("created", "filled", "partial", "cancelled", "rejected", "expired")
SMARKETS_DEFAULT_ORDER_STATES = ("created", "filled", "partial", "cancelled", "rejected", "expired")
SMARKETS_REFERENCES = (
    "https://docs.smarkets.com/",
    "https://help.smarkets.com/hc/en-gb/articles/34720906181021-Smarkets-API-Documentation-Resources",
    "https://help.smarkets.com/hc/en-gb/articles/34697834941085-Smarkets-API-Access-Integration-T-Cs",
    "https://github.com/smarkets/smk_trading_bot/blob/master/client.py",
)


class SmarketsAdapter(MarketAdapter):
    """Smarkets REST exchange adapter with explicit API-approval gates.

    Smarkets prices and quantities are represented in exchange integer units
    (probability/quantity scaled by 10,000).  The adapter normalizes those
    values into probabilities and stake sizes, keeps paper mode local, and only
    submits a guarded order after the operator supplies an approved session
    token.  No browser/private-session scraping is used.
    """

    metadata = get_market_metadata("smarkets")
    live_order_sides = ("BUY", "SELL")
    account_recovery_operations = SMARKETS_ACCOUNT_OPERATIONS
    order_management_operations = SMARKETS_ORDER_MANAGEMENT_OPERATIONS

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        credential = self.resolve_credential(
            "smarkets_session_token",
            ("SMARKETS_SESSION_TOKEN", "SMARKETS_API_TOKEN"),
            label="SMARKETS_SESSION_TOKEN",
        )
        health.update(
            {
                "api_base_url": self.api_base_url,
                "session_token_configured": bool(credential),
                "session_token_source": credential.source if credential else "missing",
                "references": list(SMARKETS_REFERENCES),
                "api_approval_required": True,
                "live_trading_supported": True,
                "live_trading_enabled": self.config_bool("live_trading_enabled", False),
                "account_recovery_operations": list(self.account_recovery_operations),
                "authenticated_account_endpoints": ["/orders/", "/accounts/"],
                "order_management_operations": list(self.order_management_operations),
                "order_management_enabled": self.config_bool("smarkets_order_management_enabled", False),
                "authenticated_order_management_endpoints": [
                    "/orders/{order_id}/",
                    "/orders/?market_id={market_id}",
                ],
            }
        )
        return health

    @property
    def api_base_url(self) -> str:
        configured = self.config.get("smarkets_api_base_url") or self.config.get("api_base_url")
        return str(configured or DEFAULT_SMARKETS_API_BASE_URL).rstrip("/")

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = max(1, min(int(limit or 50), 100))
        params: Dict[str, Any] = {"limit": desired}
        state = str(self.config.get("smarkets_event_state") or "upcoming").strip()
        if state:
            params["state"] = state
        if str(query or "").strip():
            params["search"] = str(query).strip()
        payload = self._get("/events/", params=params)
        events = self._rows(payload, "events", "data")
        needle = str(query or "").strip().lower()
        if needle:
            events = [event for event in events if needle in self._search_text(event)]
        return [self._event_from_payload(event) for event in events[:desired]]

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        event = self._required_id(event_id, "event")
        markets_payload = self._get(f"/events/{event}/markets/", params={"limit": 100})
        markets = self._rows(markets_payload, "markets", "data")
        contracts: List[MarketContract] = []
        for market in markets:
            market_id = self._id(market, "market_id")
            if not market_id:
                continue
            rows = self._rows(market, "contracts")
            if not rows:
                contracts_payload = self._get(f"/markets/{market_id}/contracts/", params={"limit": 100})
                rows = self._rows(contracts_payload, "contracts", "data")
            contracts.extend(self._contracts_from_rows(event, market, rows))
        return contracts

    def get_orderbook(self, contract_id: str) -> OrderBookSnapshot:
        self.ensure_capability("orderbook_reading")
        market_id, contract_ref = self._split_contract_id(contract_id)
        payload = self._get(f"/markets/{market_id}/quotes/", params=None)
        quote = self._quote_for_contract(payload, contract_ref)
        bids = self._levels(self._value(quote, "back_offers", "backOffers", "bids", "buy"), reverse=True)
        asks = self._levels(self._value(quote, "lay_offers", "layOffers", "asks", "sell", "offers"), reverse=False)
        return OrderBookSnapshot(
            market_id=self.market_id,
            contract_id=self._contract_id(market_id, contract_ref),
            bids=bids,
            asks=asks,
            raw={"quote": dict(quote)},
        )

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        book = self.get_orderbook(contract_id)
        bid = book.bids[0].price if book.bids else None
        ask = book.asks[0].price if book.asks else None
        midpoint = (bid + ask) / 2.0 if bid is not None and ask is not None else None
        quote = book.raw.get("quote") if isinstance(book.raw.get("quote"), Mapping) else {}
        last = self._probability(self._value(quote, "last_executed_price", "lastExecutedPrice", "last_price"))
        if last is None:
            last = midpoint or bid or ask
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=book.contract_id,
            last=last,
            bid=bid,
            ask=ask,
            midpoint=midpoint,
            source="smarkets_v3_quotes",
            raw=book.raw,
        )

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        market_id, contract_id = self._validate_order(order)
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=self._contract_id(market_id, contract_id),
            accepted=True,
            message=(
                f"DRY RUN: would place Smarkets {str(order.side).upper()} "
                f"for {float(order.size):.4f} stake"
                + (f" at probability {float(order.limit_price):.4f}" if order.limit_price is not None else "")
            ),
            raw={"request": self._order_payload(order, signed=False), "dry_run": True},
        )

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        self.ensure_capability("live_trading")
        market_id, contract_id = self._validate_order(order)
        preflight = self.preflight_live_order(order)
        payload = self._order_payload(order, signed=False)
        response = self._request_json("POST", "/orders/", payload, auth=True)
        return {
            "market_id": self.market_id,
            "contract_id": self._contract_id(market_id, contract_id),
            "live": True,
            "preflight": preflight,
            "request": payload,
            "response": response,
        }

    def list_orders(self, *, status: str = "", limit: int = 50) -> Any:
        """Read the authenticated Smarkets order feed without changing state.

        The official client exposes ``GET /orders/`` with repeated ``states``
        filters and a bounded ``limit``.  The raw response is preserved so
        callers can inspect venue-specific pagination and order fields.
        """

        states = self._order_states(status)
        params: Dict[str, Any] = {"limit": self._account_limit(limit)}
        if states:
            params["states"] = states
        return self._get("/orders/", params=params)

    def list_trades(
        self,
        contract_id: str,
        *,
        limit: int = 50,
        before: Optional[float] = None,
        after: Optional[float] = None,
    ) -> List[MarketTrade]:
        """Normalize executed Smarkets orders into account trade records.

        Smarkets' authenticated ``GET /orders/`` feed is an order/execution
        feed rather than a public trade tape.  Only rows with a positive
        executed quantity, an executed price, and an execution timestamp are
        returned.  Unmatched/resting orders are deliberately excluded.
        """

        self.ensure_capability("trade_history")
        market_id, contract_ref = self._split_contract_id(contract_id)
        desired = self._account_limit(limit)
        before_ts = self._history_timestamp(before, "before") if before is not None else None
        after_ts = self._history_timestamp(after, "after") if after is not None else None
        if before_ts is not None and after_ts is not None and before_ts < after_ts:
            raise MarketConfigurationError("Smarkets trade history requires before to be at or after after.")

        payload = self.list_orders(status="filled,partial", limit=desired)
        rows = self._rows(payload, "orders", "data")
        trades: List[MarketTrade] = []
        for raw in rows:
            if not isinstance(raw, Mapping):
                continue
            row_market = str(self._value(raw, "market_id", "marketId", "market") or "").strip()
            row_contract = str(self._value(raw, "contract_id", "contractId", "contract") or "").strip()
            if row_market and row_market != market_id:
                continue
            if row_contract and row_contract != contract_ref:
                continue
            side = self._trade_side(self._value(raw, "side", "order_side"))
            size = self._executed_quantity(raw)
            price = self._probability(
                self._value(raw, "average_executed_price", "averageExecutedPrice", "executed_price", "price")
            )
            timestamp = self._timestamp_seconds(
                self._value(
                    raw,
                    "executed_at",
                    "executedAt",
                    "last_executed_at",
                    "lastExecutedAt",
                    "updated_at",
                    "updatedAt",
                    "created_at",
                    "createdAt",
                )
            )
            trade_id = self._id(raw, "order_id", "trade_id", "tradeId")
            if not trade_id or side is None or size is None or price is None or timestamp is None:
                continue
            if after_ts is not None and timestamp < after_ts:
                continue
            if before_ts is not None and timestamp > before_ts:
                continue
            trades.append(
                MarketTrade(
                    market_id=self.market_id,
                    contract_id=self._contract_id(market_id, contract_ref),
                    trade_id=trade_id,
                    side=side,
                    price=price,
                    size=size,
                    timestamp=timestamp,
                    raw=dict(raw),
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
        """Derive bounded OHLCV candles from authenticated executed orders."""

        self.ensure_capability("candle_history")
        market_id, contract_ref = self._split_contract_id(contract_id)
        interval = self._candle_interval(resolution)
        start_ts = self._history_timestamp(from_timestamp, "from_timestamp") if from_timestamp is not None else None
        end_ts = self._history_timestamp(to_timestamp, "to_timestamp") if to_timestamp is not None else None
        if start_ts is not None and end_ts is not None and end_ts < start_ts:
            raise MarketConfigurationError("Smarkets candle history requires to_timestamp to be at or after from_timestamp.")

        trades = self.list_trades(
            contract_id,
            limit=self._account_limit(self.config.get("smarkets_candle_trade_limit", 1000)),
            before=end_ts,
            after=start_ts,
        )
        buckets: Dict[int, Dict[str, Any]] = {}
        for trade in trades:
            if trade.timestamp is None:
                continue
            bucket_timestamp = int(float(trade.timestamp) // interval * interval)
            bucket = buckets.setdefault(
                bucket_timestamp,
                {
                    "open": trade.price,
                    "high": trade.price,
                    "low": trade.price,
                    "close": trade.price,
                    "volume": 0.0,
                    "trade_ids": [],
                },
            )
            bucket["high"] = max(float(bucket["high"]), trade.price)
            bucket["low"] = min(float(bucket["low"]), trade.price)
            bucket["close"] = trade.price
            bucket["volume"] += trade.size
            bucket["trade_ids"].append(trade.trade_id)

        canonical = self._contract_id(market_id, contract_ref)
        return [
            MarketCandle(
                market_id=self.market_id,
                contract_id=canonical,
                timestamp=float(bucket_timestamp),
                open=float(bucket["open"]),
                high=float(bucket["high"]),
                low=float(bucket["low"]),
                close=float(bucket["close"]),
                volume=float(bucket["volume"]),
                raw={
                    "source": "smarkets_authenticated_executed_orders",
                    "derived": True,
                    "resolution": str(resolution or "").strip().lower(),
                    "interval_seconds": interval,
                    "trade_ids": list(bucket["trade_ids"]),
                },
            )
            for bucket_timestamp, bucket in sorted(buckets.items())
        ]

    def get_account(self) -> Any:
        """Read the authenticated Smarkets account summary."""

        return self._get("/accounts/", params=None)

    def account_recovery(self, operation: str, **kwargs: Any) -> Any:
        normalized = str(operation or "").strip().lower()
        if normalized == "order_history":
            return self.list_orders(
                status=str(kwargs.get("status") or ""),
                limit=kwargs.get("limit", 50),
            )
        if normalized == "account":
            return self.get_account()
        supported = ", ".join(self.account_recovery_operations)
        raise MarketConfigurationError(f"Smarkets account recovery supports only: {supported}.")

    def manage_orders(self, operation: str, **kwargs: Any) -> Dict[str, Any]:
        """Run a guarded documented Smarkets cancellation mutation.

        Smarkets' official client documents a single-order ``DELETE
        /orders/{order_id}/`` route and a market-scoped ``DELETE /orders/``
        route with ``market_id``.  There is deliberately no unscoped global
        cancellation operation here.
        """

        normalized = str(operation or "").strip().lower()
        if normalized not in self.order_management_operations:
            supported = ", ".join(self.order_management_operations)
            raise MarketConfigurationError(
                f"Smarkets order-management operation must be one of: {supported}."
            )
        self.ensure_capability("live_trading")
        if not self.config_bool("smarkets_order_management_enabled", False):
            raise MarketConfigurationError(
                "Smarkets order management is disabled by adapter config. "
                "Set smarkets_order_management_enabled=true only after reviewing live-order risk controls."
            )
        self.ensure_live_trading_enabled("Smarkets order management")
        if str(kwargs.get("confirm_order_management") or "").strip() != SMARKETS_ORDER_MANAGEMENT_CONFIRMATION:
            raise MarketConfigurationError(
                "Smarkets order management requires exact confirmation text "
                f"{SMARKETS_ORDER_MANAGEMENT_CONFIRMATION}."
            )

        request_params: Dict[str, Any] = {}
        if normalized == "cancel_order":
            order_id = self._safe_identifier(kwargs.get("order_id"), "order")
            path = f"/orders/{order_id}"
            response = self._request_json("DELETE", path, params=None, auth=True)
            request_params = {"order_id": order_id}
        else:
            market_id = self._safe_identifier(kwargs.get("market_id"), "market")
            request_params = {"market_id": market_id}
            response = self._request_json("DELETE", "/orders/", params=request_params, auth=True)

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
                "references": list(SMARKETS_REFERENCES),
            },
            "request": {"params": request_params},
            "response": response,
        }

    def copy_trade_from_activity(self, activity: Mapping[str, Any]) -> PaperOrderResult:
        raise UnsupportedFeatureError(
            self.market_id,
            "copy_trading",
            "Smarkets copy trading is unsupported because account activity mirroring is not an official adapter feature.",
        )

    def _get(self, path: str, *, params: Optional[Mapping[str, Any]]) -> Any:
        return self.runtime.get_json(
            self._url(self.api_base_url, path),
            params=params,
            headers=self._headers(required=True),
        )

    def _request_json(
        self,
        method: str,
        path: str,
        body: Optional[Mapping[str, Any]] = None,
        *,
        auth: bool,
        params: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        self.runtime.rate_limiter.wait()
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if auth:
            headers.update(self._headers(required=True))
        request_kwargs: Dict[str, Any] = {
            "json": dict(body) if body is not None else None,
            "headers": {"User-Agent": self.runtime.user_agent, **headers},
            "timeout": self.runtime.timeout_seconds,
        }
        if params is not None:
            request_kwargs["params"] = dict(params)
        try:
            response = self.runtime.session.request(
                method.upper(),
                self._url(self.api_base_url, path),
                **request_kwargs,
            )
        except Exception as exc:
            raise MarketHTTPError(f"{self.market_id} HTTP request failed: {exc}") from exc
        status = int(getattr(response, "status_code", 0) or 0)
        if status >= 400:
            raise MarketHTTPError(f"{self.market_id} HTTP {status}: {str(getattr(response, 'text', ''))[:200]}")
        try:
            return response.json()
        except ValueError as exc:
            raise MarketHTTPError(f"{self.market_id} response was not valid JSON.") from exc

    def _headers(self, *, required: bool) -> Dict[str, str]:
        credential = self.resolve_credential(
            "smarkets_session_token",
            ("SMARKETS_SESSION_TOKEN", "SMARKETS_API_TOKEN"),
            required=required,
            label="SMARKETS_SESSION_TOKEN",
        )
        return {"Authorization": f"Session-Token {credential.value}"} if credential else {}

    @classmethod
    def _trade_side(cls, value: Any) -> Optional[str]:
        normalized = str(value or "").strip().upper()
        if normalized in {"BUY", "BACK"}:
            return "BUY"
        if normalized in {"SELL", "LAY"}:
            return "SELL"
        return None

    def _executed_quantity(self, raw: Mapping[str, Any]) -> Optional[float]:
        value = self._value(
            raw,
            "total_executed_quantity",
            "totalExecutedQuantity",
            "executed_quantity",
            "executedQuantity",
            "matched_quantity",
            "matchedQuantity",
        )
        if value in (None, ""):
            return None
        try:
            quantity = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(quantity) or quantity <= 0:
            return None
        return quantity / self._positive_scale("smarkets_quantity_scale", 10_000.0)

    @staticmethod
    def _timestamp_seconds(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
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
        except ValueError:
            number = None
        if number is not None and math.isfinite(number):
            return number / 1000.0 if number > 100_000_000_000 else number
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()

    @staticmethod
    def _history_timestamp(value: Any, label: str) -> float:
        timestamp = SmarketsAdapter._timestamp_seconds(value)
        if timestamp is None or timestamp < 0 or not math.isfinite(timestamp):
            raise MarketConfigurationError(f"Smarkets {label} timestamp must be a valid non-negative epoch or ISO time.")
        return timestamp

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
            allowed = ", ".join(intervals)
            raise MarketConfigurationError(f"Smarkets candle resolution must be one of: {allowed}.")
        return intervals[normalized]

    def _validate_order(self, order: PaperOrderRequest) -> Tuple[str, str]:
        self.ensure_order_market(order)
        market_id, contract_id = self._split_contract_id(order.contract_id)
        if str(order.side or "").upper() not in self.live_order_sides:
            raise MarketConfigurationError("Smarkets order side must be BUY or SELL.")
        try:
            size = float(order.size)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Smarkets order quantity must be numeric.") from exc
        if not math.isfinite(size) or size <= 0:
            raise MarketConfigurationError("Smarkets order quantity must be positive and finite.")
        if order.limit_price is not None and self._probability(order.limit_price) is None:
            raise MarketConfigurationError("Smarkets order price must be between 0 and 1.")
        return market_id, contract_id

    def _order_payload(self, order: PaperOrderRequest, *, signed: bool) -> Dict[str, Any]:
        existing = order.metadata.get("smarkets_order")
        if isinstance(existing, Mapping):
            return dict(existing)
        market_id, contract_id = self._split_contract_id(order.contract_id)
        probability = self._probability(order.limit_price)
        if probability is None:
            raise MarketConfigurationError("Smarkets live/paper order requires a limit probability.")
        price_scale = self._positive_scale("smarkets_price_scale", 10_000.0)
        quantity_scale = self._positive_scale("smarkets_quantity_scale", 10_000.0)
        return {
            "market_id": market_id,
            "contract_id": contract_id,
            "side": "buy" if str(order.side).upper() == "BUY" else "sell",
            "quantity": str(round(float(order.size) * quantity_scale)),
            "price": str(round(probability * price_scale)),
            "type": str(order.metadata.get("order_type") or "limit").lower(),
        }

    @classmethod
    def _contracts_from_rows(
        cls,
        event_id: str,
        market: Mapping[str, Any],
        rows: List[Mapping[str, Any]],
    ) -> List[MarketContract]:
        market_id = cls._id(market, "market_id")
        title = str(cls._value(market, "name", "title", "market_name") or market_id)
        status = str(cls._value(market, "state", "status") or "").lower()
        contracts: List[MarketContract] = []
        for row in rows:
            contract_id = cls._id(row, "contract_id")
            if not contract_id:
                continue
            outcome = str(cls._value(row, "name", "title", "contract_name", "label") or contract_id)
            contracts.append(
                MarketContract(
                    market_id=cls.metadata.market_id,
                    contract_id=cls._contract_id(market_id, contract_id),
                    event_id=event_id,
                    title=f"{title} - {outcome}",
                    outcome=outcome,
                    url=str(cls._value(market, "url", "slug") or market_id),
                    status=status,
                    raw={"market": dict(market), "contract": dict(row)},
                )
            )
        return contracts

    def _event_from_payload(self, event: Mapping[str, Any]) -> MarketEvent:
        event_id = self._id(event, "event_id")
        return MarketEvent(
            market_id=self.market_id,
            event_id=event_id,
            title=str(self._value(event, "name", "title", "description") or event_id),
            url=str(self._value(event, "url", "slug") or event_id),
            status=str(self._value(event, "state", "status") or "").lower(),
            raw=dict(event),
        )

    @classmethod
    def _quote_for_contract(cls, payload: Any, contract_id: str) -> Mapping[str, Any]:
        rows = cls._rows(payload, "quotes", "data")
        if not rows:
            mapping = cls._mapping_payload(payload)
            for key, value in mapping.items():
                if str(key) == contract_id and isinstance(value, Mapping):
                    return value
        for row in rows:
            if cls._id(row, "contract_id") == contract_id:
                return row
        return rows[0] if len(rows) == 1 else {}

    @classmethod
    def _levels(cls, value: Any, *, reverse: bool) -> List[OrderBookLevel]:
        if isinstance(value, Mapping):
            value = value.get("levels") or value.get("offers") or value.get("orders") or []
        if not isinstance(value, list):
            return []
        levels: List[OrderBookLevel] = []
        for row in value:
            if isinstance(row, Mapping):
                price_value = cls._value(row, "price", "odds", "rate")
                size_value = cls._value(row, "quantity", "size", "amount", "stake", "volume")
            elif isinstance(row, (list, tuple)) and len(row) >= 2:
                price_value, size_value = row[0], row[1]
            else:
                continue
            price = cls._probability(price_value)
            try:
                size = float(size_value)
            except (TypeError, ValueError):
                continue
            if price is None or not math.isfinite(size) or size <= 0:
                continue
            levels.append(OrderBookLevel(price=price, size=size))
        levels.sort(key=lambda level: level.price, reverse=reverse)
        return levels

    @staticmethod
    def _mapping_payload(payload: Any) -> Dict[str, Any]:
        if isinstance(payload, Mapping):
            data = payload.get("data")
            if isinstance(data, Mapping):
                return dict(data)
            return dict(payload)
        return {}

    @classmethod
    def _rows(cls, payload: Any, *keys: str) -> List[Mapping[str, Any]]:
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, Mapping)]
        mapping = cls._mapping_payload(payload)
        for key in keys:
            rows = mapping.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, Mapping)]
            if isinstance(rows, Mapping):
                return [dict(value, **({"id": key_id} if isinstance(value, Mapping) else {})) for key_id, value in rows.items() if isinstance(value, Mapping)]
        return []

    @classmethod
    def _search_text(cls, payload: Mapping[str, Any]) -> str:
        values = [
            payload.get("id"),
            payload.get("event_id"),
            payload.get("name"),
            payload.get("title"),
            payload.get("description"),
            payload.get("slug"),
        ]
        return " ".join(str(value or "") for value in values).lower()

    @staticmethod
    def _id(payload: Mapping[str, Any], *aliases: str) -> str:
        for key in ("id", *aliases):
            value = payload.get(key)
            if value not in (None, ""):
                return str(value).strip()
        return ""

    @staticmethod
    def _value(payload: Mapping[str, Any], *keys: str) -> Any:
        for key in keys:
            value = payload.get(key)
            if value not in (None, ""):
                return value
        return None

    @staticmethod
    def _probability(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        if number > 1.0:
            number /= 10_000.0
        return number if 0.0 <= number <= 1.0 else None

    def _positive_scale(self, key: str, default: float) -> float:
        value = self.config.get(key, default)
        try:
            value = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"Smarkets config {key} must be numeric.") from exc
        if not math.isfinite(value) or value <= 0:
            raise MarketConfigurationError(f"Smarkets config {key} must be greater than 0.")
        return value

    @staticmethod
    def _required_id(value: Any, label: str) -> str:
        return SmarketsAdapter._safe_identifier(value, label)

    @classmethod
    def _safe_identifier(cls, value: Any, label: str) -> str:
        clean = str(value or "").strip()
        if not clean:
            raise MarketConfigurationError(f"Smarkets {label} id cannot be empty.")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,199}", clean):
            raise MarketConfigurationError(f"Smarkets {label} id contains unsupported path characters.")
        return clean

    @classmethod
    def _order_states(cls, value: Any) -> List[str]:
        raw = str(value or "").strip().lower()
        if not raw or raw == "all":
            return list(SMARKETS_DEFAULT_ORDER_STATES)
        states = [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]
        if not states or any(state not in SMARKETS_ORDER_STATES for state in states):
            allowed = ", ".join(SMARKETS_ORDER_STATES)
            raise MarketConfigurationError(f"Smarkets order states must be all or a comma-separated list of: {allowed}.")
        return list(dict.fromkeys(states))

    @staticmethod
    def _account_limit(value: Any) -> int:
        try:
            limit = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Smarkets account limit must be an integer.") from exc
        if limit < 1 or limit > 1000:
            raise MarketConfigurationError("Smarkets account limit must be between 1 and 1000.")
        return limit

    @staticmethod
    def _contract_id(market_id: str, contract_id: str) -> str:
        return f"{market_id}:{contract_id}"

    @staticmethod
    def _split_contract_id(contract_id: str) -> Tuple[str, str]:
        parts = [part.strip() for part in str(contract_id or "").split(":")]
        if len(parts) != 2 or any(not part for part in parts):
            raise MarketConfigurationError("Smarkets contract id must be MARKET_ID:CONTRACT_ID.")
        return SmarketsAdapter._safe_identifier(parts[0], "market"), SmarketsAdapter._safe_identifier(parts[1], "contract")

    @staticmethod
    def _url(base: str, path: str) -> str:
        return f"{base.rstrip('/')}/{str(path or '').strip('/')}/"
