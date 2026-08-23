from __future__ import annotations

import math
import re
import secrets
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .base import MarketAdapter
from .catalog import get_market_metadata
from .errors import MarketConfigurationError, UnsupportedFeatureError
from .types import (
    MarketCandle,
    MarketContract,
    MarketEvent,
    MarketTrade,
    OrderBookLevel,
    OrderBookSnapshot,
    PaperOrderRequest,
    PaperOrderResult,
    PriceSnapshot,
)


DEFAULT_SX_BET_BASE_URL = "https://api.sx.bet"
DEFAULT_SX_BET_WS_URL = "wss://realtime.sx.bet/connection/websocket"
DEFAULT_SX_BET_CHAIN_ID = 4162
DEFAULT_SX_BET_EXPIRY = 2209006800
DEFAULT_SX_BET_BASE_TOKEN = "0x6629Ce1Cf35Cc1329ebB4F63202F3f197b3F050B"
DEFAULT_SX_BET_EXECUTOR = "0x52adf738AAD93c31f798a30b2C74D658e1E9a562"
ODDS_SCALE = 10**20
SX_BET_REFERENCES = (
    "https://docs.sx.bet/api-reference/introduction",
    "https://docs.sx.bet/api-reference/get-markets-active",
    "https://docs.sx.bet/api-reference/get-markets-find",
    "https://docs.sx.bet/api-reference/get-orders",
    "https://docs.sx.bet/api-reference/get-best-odds",
    "https://docs.sx.bet/api-reference/get-trades-v3-public",
    "https://docs.sx.bet/api-reference/post-new-order",
    "https://docs.sx.bet/api-reference/api-key",
    "https://docs.sx.bet/api-reference/centrifugo-order-book-updates",
    "https://docs.sx.bet/api-reference/eip712-signing",
    "https://docs.sx.bet/api-reference/get-orders-v3",
    "https://docs.sx.bet/api-reference/get-order-v3",
    "https://docs.sx.bet/api-reference/get-order-v3-by-client-id",
    "https://docs.sx.bet/api-reference/get-trades-v3",
    "https://docs.sx.bet/api-reference/get-fills-v3",
    "https://docs.sx.bet/api-reference/get-positions-v3",
    "https://docs.sx.bet/api-reference/get-user-balance-v3",
    "https://docs.sx.bet/api-reference/delete-orders-v3",
    "https://docs.sx.bet/api-reference/delete-orders-v3-event",
    "https://docs.sx.bet/api-reference/delete-orders-v3-all",
)

SX_BET_ACCOUNT_OPERATIONS = (
    "balance",
    "active_orders",
    "order_detail",
    "order_by_client_id",
    "order_history",
    "fills",
    "positions",
)
SX_BET_ORDER_MANAGEMENT_OPERATIONS = (
    "cancel_order",
    "cancel_orders",
    "cancel_event_orders",
    "cancel_all_orders",
)


class SxBetAdapter(MarketAdapter):
    """SX Bet adapter using documented public REST and Centrifugo WebSocket APIs."""

    metadata = get_market_metadata("sx_bet")
    account_recovery_operations = SX_BET_ACCOUNT_OPERATIONS
    order_management_operations = SX_BET_ORDER_MANAGEMENT_OPERATIONS

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        api_key = self.resolve_credential("sx_bet_api_key", ("SX_BET_API_KEY",), label="SX_BET_API_KEY")
        maker = self.resolve_credential("sx_bet_maker_address", ("SX_BET_MAKER_ADDRESS",), label="SX_BET_MAKER_ADDRESS")
        private_key = self.resolve_credential("sx_bet_private_key", ("SX_BET_PRIVATE_KEY",), label="SX_BET_PRIVATE_KEY")
        credential_sources = []
        for credential in (api_key, maker, private_key):
            if credential:
                credential_sources.append({"name": credential.name, "source": credential.source})
        health.update(
            {
                "api_base_url": self.api_base_url,
                "websocket_url": self.websocket_url,
                "references": list(SX_BET_REFERENCES),
                "live_trading_enabled": self.config_bool("live_trading_enabled", False),
                "credential_sources": credential_sources,
                "copy_trading_supported": False,
                "account_recovery_operations": list(self.account_recovery_operations),
                "order_management_operations": list(self.order_management_operations),
                "account_api_version": "v3",
                "account_endpoints": {
                    "balance": "/user/balance-v3",
                    "active_orders": "/orders-v3",
                    "order_detail": "/orders-v3/{orderId}",
                    "order_by_client_id": "/orders-v3/client/{clientOrderId}",
                    "order_history": "/trades-v3",
                    "fills": "/fills-v3",
                    "positions": "/positions-v3",
                },
                "order_management_endpoints": {
                    "cancel_order": "DELETE /orders-v3",
                    "cancel_orders": "DELETE /orders-v3",
                    "cancel_event_orders": "DELETE /orders-v3/event",
                    "cancel_all_orders": "DELETE /orders-v3/all",
                },
                "order_management_enabled": self.config_bool("sx_bet_order_management_enabled", False),
            }
        )
        return health

    @property
    def api_base_url(self) -> str:
        configured = self.config.get("sx_bet_api_base_url") or self.config.get("api_base_url")
        return str(configured or DEFAULT_SX_BET_BASE_URL).rstrip("/")

    @property
    def websocket_url(self) -> str:
        configured = self.config.get("sx_bet_ws_url") or self.config.get("websocket_url")
        return str(configured or DEFAULT_SX_BET_WS_URL).rstrip("/")

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = max(1, min(int(limit or 50), 100))
        markets = self._fetch_active_markets(limit=desired)
        q = str(query or "").strip().lower()
        if q:
            markets = [market for market in markets if self._market_matches_query(market, q)]
        return [self._event_from_market(market) for market in markets[:desired]]

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        market = self._get_market(str(event_id or "").strip())
        if not market:
            return []
        return self._contracts_from_market(market)

    def get_orderbook(self, contract_id: str) -> OrderBookSnapshot:
        self.ensure_capability("orderbook_reading")
        market_hash, outcome = self._split_contract_id(contract_id)
        orders = self._fetch_orders(market_hash)
        bids, asks = self._book_for_outcome(orders, outcome)
        return OrderBookSnapshot(
            market_id=self.market_id,
            contract_id=self._contract_id(market_hash, outcome),
            bids=bids,
            asks=asks,
            raw={"orders": [dict(order) for order in orders]},
        )

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        market_hash, outcome = self._split_contract_id(contract_id)
        orderbook = self.get_orderbook(self._contract_id(market_hash, outcome))
        bid = orderbook.bids[0].price if orderbook.bids else None
        ask = orderbook.asks[0].price if orderbook.asks else None
        midpoint = (bid + ask) / 2.0 if bid is not None and ask is not None else None
        last = midpoint
        if last is None:
            last = self._best_odds_price(market_hash, outcome)
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=self._contract_id(market_hash, outcome),
            last=last,
            bid=bid,
            ask=ask,
            midpoint=midpoint,
            source="sx_bet_orderbook",
            raw=orderbook.raw,
        )

    def list_trades(
        self,
        contract_id: str,
        *,
        limit: int = 50,
        before: Optional[float] = None,
        after: Optional[float] = None,
    ) -> List[MarketTrade]:
        """Return recent public SX Bet trades for one market outcome.

        SX Bet's public tape supports market/outcome selection and cursor
        pagination, but does not expose timestamp query parameters.  The
        shared history bounds are therefore validated and applied locally to
        the returned page.
        """

        self.ensure_capability("trade_history")
        market_hash, outcome = self._split_contract_id(contract_id)
        try:
            desired = int(limit)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("SX Bet trade history limit must be an integer between 1 and 100.") from exc
        if desired < 1 or desired > 100:
            raise MarketConfigurationError("SX Bet trade history limit must be between 1 and 100.")

        before_ts = self._history_timestamp(before, "before") if before is not None else None
        after_ts = self._history_timestamp(after, "after") if after is not None else None
        if before_ts is not None and after_ts is not None and before_ts < after_ts:
            raise MarketConfigurationError("SX Bet trade history requires before to be at or after after.")

        payload = self.runtime.get_json(
            self._url("/trades-v3/public"),
            params={"marketHash": market_hash, "perPage": desired},
        )
        data = payload.get("data") if isinstance(payload, Mapping) else None
        rows = data.get("trades") if isinstance(data, Mapping) else []
        if not isinstance(rows, list):
            return []

        canonical = self._contract_id(market_hash, outcome)
        desired_is_one = outcome == "ONE"
        trades: List[MarketTrade] = []
        for raw in rows:
            if not isinstance(raw, Mapping):
                continue
            if str(raw.get("marketHash") or "").strip().lower() != market_hash.lower():
                continue
            is_outcome_one = self._boolean(raw.get("isBettingOutcomeOne"))
            if is_outcome_one is None or is_outcome_one != desired_is_one:
                continue
            trade_id = str(raw.get("tradeId") or raw.get("id") or "").strip()
            price = self._scaled_probability(raw.get("weightedAverageOdds"))
            stake = self._safe_float(raw.get("totalStake"))
            if not trade_id or price is None or stake is None or stake <= 0:
                continue
            timestamp = self._timestamp_seconds(raw.get("betTime") or raw.get("createdAt"))
            if timestamp is not None:
                if after_ts is not None and timestamp < after_ts:
                    continue
                if before_ts is not None and timestamp > before_ts:
                    continue
            trades.append(
                MarketTrade(
                    market_id=self.market_id,
                    contract_id=canonical,
                    trade_id=trade_id,
                    side="BUY",
                    price=price,
                    size=self._from_base_units(stake),
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
        """Derive bounded OHLCV candles from SX Bet's public trade tape.

        SX Bet documents public trades, but not an exchange candle endpoint.
        This method deliberately derives candles from the normalized trade
        page, marks the result as derived, and retains source trade ids in
        ``raw`` so callers can audit the aggregation.  The upstream page is
        bounded to the configured trade limit (at most 100 rows).
        """

        self.ensure_capability("candle_history")
        interval = self._candle_interval(resolution)
        start_ts = self._history_timestamp(from_timestamp, "from_timestamp") if from_timestamp is not None else None
        end_ts = self._history_timestamp(to_timestamp, "to_timestamp") if to_timestamp is not None else None
        if start_ts is not None and end_ts is not None and end_ts < start_ts:
            raise MarketConfigurationError("SX Bet candle history requires to_timestamp to be at or after from_timestamp.")

        trades = self.list_trades(
            contract_id,
            limit=self._candle_trade_limit(),
            before=end_ts,
            after=start_ts,
        )
        buckets: Dict[int, Dict[str, Any]] = {}
        for trade in trades:
            timestamp = trade.timestamp
            if timestamp is None or not math.isfinite(float(timestamp)) or timestamp < 0:
                continue
            bucket_timestamp = int(float(timestamp) // interval * interval)
            bucket = buckets.setdefault(
                bucket_timestamp,
                {"open": trade.price, "high": trade.price, "low": trade.price, "close": trade.price, "volume": 0.0, "trade_ids": []},
            )
            bucket["high"] = max(float(bucket["high"]), trade.price)
            bucket["low"] = min(float(bucket["low"]), trade.price)
            bucket["close"] = trade.price
            bucket["volume"] += max(0.0, float(trade.size))
            bucket["trade_ids"].append(trade.trade_id)

        canonical_contract_id = self._split_contract_id(contract_id)
        contract = self._contract_id(*canonical_contract_id)
        candles: List[MarketCandle] = []
        for bucket_timestamp in sorted(buckets):
            bucket = buckets[bucket_timestamp]
            candles.append(
                MarketCandle(
                    market_id=self.market_id,
                    contract_id=contract,
                    timestamp=float(bucket_timestamp),
                    open=float(bucket["open"]),
                    high=float(bucket["high"]),
                    low=float(bucket["low"]),
                    close=float(bucket["close"]),
                    volume=float(bucket["volume"]),
                    raw={
                        "source": "sx_bet_public_trade_tape",
                        "derived": True,
                        "resolution": str(resolution or "").strip().lower(),
                        "interval_seconds": interval,
                        "trade_ids": list(bucket["trade_ids"]),
                    },
                )
            )
        return candles

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        self._validate_order(order)
        market_hash, outcome = self._split_contract_id(order.contract_id)
        payload = self._build_unsigned_order(order, dry_run=True)
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=self._contract_id(market_hash, outcome),
            accepted=True,
            message=(
                f"DRY RUN: would place SX Bet {order.side.upper()} "
                f"for {order.size:.4f} {outcome} shares"
                + (f" at limit {order.limit_price:.4f}" if order.limit_price is not None else "")
            ),
            filled_size=0.0,
            average_price=None,
            raw={"request": payload},
        )

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        self.ensure_capability("live_trading")
        self._validate_order(order)
        preflight = self.preflight_live_order(order)
        signed_order = self._signed_order(order)
        response = self.runtime.request_json(
            "POST",
            self._url("/orders/new"),
            json_body={"orders": [signed_order]},
            headers={"Content-Type": "application/json"},
        )
        return {
            "market_id": self.market_id,
            "contract_id": self._contract_id(*self._split_contract_id(order.contract_id)),
            "live": True,
            "preflight": preflight,
            "request": {"orders": [signed_order]},
            "response": response,
        }

    def account_recovery(self, operation: str, **kwargs: Any) -> Any:
        """Read SX Bet's documented authenticated v3 account surfaces.

        SX Bet v3 account routes are API-key scoped and intentionally use a
        fixed endpoint set.  User-controlled values are restricted to the
        documented query/path shapes before they reach the runtime.
        """

        normalized = str(operation or "").strip().lower()
        if normalized not in self.account_recovery_operations:
            supported = ", ".join(self.account_recovery_operations)
            raise MarketConfigurationError(f"SX Bet account recovery supports only: {supported}.")
        headers = self._v3_headers()
        if normalized == "balance":
            return self.runtime.get_json(self._url("/user/balance-v3"), headers=headers)
        if normalized == "active_orders":
            params = self._v3_page_params(kwargs)
            self._optional_v3_hash_param(params, "marketHash", kwargs.get("market_hash") or kwargs.get("market_id"))
            event_id = self._optional_event_id(kwargs.get("event_id"))
            if event_id:
                params["eventId"] = event_id
            return self.runtime.get_json(self._url("/orders-v3"), params=params, headers=headers)
        if normalized == "order_detail":
            order_id = self._v3_order_id(kwargs.get("order_id"))
            return self.runtime.get_json(self._url(f"/orders-v3/{order_id}"), headers=headers)
        if normalized == "order_by_client_id":
            client_order_id = self._v3_client_order_id(kwargs.get("client_order_id"))
            return self.runtime.get_json(self._url(f"/orders-v3/client/{client_order_id}"), headers=headers)
        if normalized == "order_history":
            params = self._v3_page_params(kwargs)
            self._optional_v3_hash_param(params, "marketHash", kwargs.get("market_hash") or kwargs.get("market_id"))
            status = str(kwargs.get("status") or "").strip().upper()
            if status:
                if status not in {"MATCHED", "LOCKED", "SETTLED", "FAILED"}:
                    raise MarketConfigurationError("SX Bet order_history status must be MATCHED, LOCKED, SETTLED, or FAILED.")
                params["status"] = status
            start_date, end_date = self._v3_date_bounds(kwargs.get("start_date"), kwargs.get("end_date"))
            if start_date:
                params["startDate"] = start_date
            if end_date:
                params["endDate"] = end_date
            params["sortAsc"] = self._bool_query(kwargs.get("sort_asc"), True)
            return self.runtime.get_json(self._url("/trades-v3"), params=params, headers=headers)
        if normalized == "fills":
            params = self._v3_page_params(kwargs)
            trade_id = self._optional_v3_hash(kwargs.get("trade_id"), "trade_id")
            order_id = self._optional_v3_hash(kwargs.get("order_id"), "order_id")
            if trade_id:
                params["tradeId"] = trade_id
            if order_id:
                params["orderId"] = order_id
            start_date, end_date = self._v3_date_bounds(kwargs.get("start_date"), kwargs.get("end_date"))
            if start_date:
                params["startDate"] = start_date
            if end_date:
                params["endDate"] = end_date
            params["sortAsc"] = self._bool_query(kwargs.get("sort_asc"), True)
            return self.runtime.get_json(self._url("/fills-v3"), params=params, headers=headers)
        if normalized == "positions":
            params = self._v3_page_params(kwargs)
            status = self._position_status(kwargs.get("status"))
            params["status"] = status
            event_id = self._optional_event_id(kwargs.get("event_id"))
            if event_id:
                params["eventId"] = event_id
            params["sortAsc"] = self._bool_query(kwargs.get("sort_asc"), False)
            return self.runtime.get_json(self._url("/positions-v3"), params=params, headers=headers)
        raise MarketConfigurationError(f"Unsupported SX Bet account operation: {normalized}.")

    def manage_orders(self, operation: str, **kwargs: Any) -> Any:
        """Run one explicit, guarded SX Bet v3 cancellation operation."""

        normalized = str(operation or "").strip().lower()
        if normalized not in self.order_management_operations:
            supported = ", ".join(self.order_management_operations)
            raise MarketConfigurationError(f"SX Bet order management supports only: {supported}.")
        self.ensure_capability("live_trading")
        if not self.config_bool("sx_bet_order_management_enabled", False):
            raise MarketConfigurationError(
                "SX Bet order management is disabled by adapter config. "
                "Set sx_bet_order_management_enabled=true only after reviewing cancellation risk."
            )
        self.ensure_live_trading_enabled("SX Bet order management")
        confirmation = str(kwargs.get("confirm_order_management") or "").strip()
        if confirmation != "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS":
            raise MarketConfigurationError(
                "SX Bet order management requires confirm_order_management="
                "'I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS'."
            )
        headers = self._v3_headers(content_type=normalized in {"cancel_order", "cancel_orders"})
        if normalized in {"cancel_order", "cancel_orders"}:
            order_ids = self._v3_order_ids(kwargs.get("order_ids"), kwargs.get("order_id"), kwargs.get("orders"), kwargs.get("instructions"))
            if not order_ids:
                raise MarketConfigurationError("SX Bet cancellation requires at least one order id.")
            if len(order_ids) > 100:
                raise MarketConfigurationError("SX Bet cancellation accepts at most 100 unique order ids.")
            response = self.runtime.request_json(
                "DELETE",
                self._url("/orders-v3"),
                json_body={"orders": [{"orderId": value} for value in order_ids]},
                headers=headers,
            )
            return {"operation": normalized, "order_ids": order_ids, "response": response}
        if normalized == "cancel_event_orders":
            event_id = self._required_event_id(kwargs.get("event_id"))
            response = self.runtime.request_json(
                "DELETE",
                self._url("/orders-v3/event"),
                params={"eventId": event_id},
                headers=self._v3_headers(),
            )
            return {"operation": normalized, "event_id": event_id, "response": response}
        response = self.runtime.request_json(
            "DELETE",
            self._url("/orders-v3/all"),
            headers=self._v3_headers(),
        )
        return {"operation": normalized, "response": response}

    def _v3_headers(self, *, content_type: bool = False) -> Dict[str, str]:
        credential = self.resolve_credential(
            "sx_bet_api_key",
            ("SX_BET_API_KEY",),
            required=True,
            label="SX_BET_API_KEY",
        )
        headers = {"x-sx-api-key": credential.value}
        if content_type:
            headers["Content-Type"] = "application/json"
        return headers

    @staticmethod
    def _v3_page_params(kwargs: Mapping[str, Any]) -> Dict[str, Any]:
        raw_limit = kwargs.get("per_page", kwargs.get("limit", 50))
        try:
            per_page = int(raw_limit)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("SX Bet account per_page must be an integer between 1 and 100.") from exc
        if per_page < 1 or per_page > 100:
            raise MarketConfigurationError("SX Bet account per_page must be between 1 and 100.")
        cursor = str(kwargs.get("next_key", kwargs.get("cursor", "")) or "").strip()
        if len(cursor) > 2048 or any(char.isspace() for char in cursor):
            raise MarketConfigurationError("SX Bet account cursor must be a compact opaque token.")
        params: Dict[str, Any] = {"perPage": per_page}
        if cursor:
            params["nextKey"] = cursor
        return params

    @staticmethod
    def _optional_v3_hash(value: Any, label: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if not re.fullmatch(r"0x[a-fA-F0-9]{64}", text):
            raise MarketConfigurationError(f"SX Bet {label} must be a 32-byte 0x-prefixed hash.")
        return text

    @classmethod
    def _optional_v3_hash_param(cls, params: Dict[str, Any], key: str, value: Any) -> None:
        normalized = cls._optional_v3_hash(value, key)
        if normalized:
            params[key] = normalized

    @staticmethod
    def _optional_event_id(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if len(text) < 2 or len(text) > 64 or not re.fullmatch(r"[A-Za-z0-9:_-]+", text):
            raise MarketConfigurationError("SX Bet event_id must be 2-64 safe identifier characters.")
        return text

    @classmethod
    def _required_event_id(cls, value: Any) -> str:
        event_id = cls._optional_event_id(value)
        if not event_id:
            raise MarketConfigurationError("SX Bet cancel_event_orders requires event_id.")
        return event_id

    @staticmethod
    def _v3_order_id(value: Any) -> str:
        order_id = str(value or "").strip()
        if not re.fullmatch(r"0x[a-fA-F0-9]{64}", order_id):
            raise MarketConfigurationError("SX Bet order_id must be a 32-byte 0x-prefixed hash.")
        return order_id

    @staticmethod
    def _v3_client_order_id(value: Any) -> str:
        client_id = str(value or "").strip()
        if len(client_id) > 64 or not re.fullmatch(r"[A-Za-z0-9_-]+", client_id):
            raise MarketConfigurationError("SX Bet client_order_id must match [A-Za-z0-9_-]+ and be at most 64 characters.")
        return client_id

    @classmethod
    def _v3_order_ids(cls, order_ids: Any, order_id: Any, orders: Any, instructions: Any) -> List[str]:
        values: List[Any] = []
        for candidate in (order_ids, order_id):
            if candidate not in (None, ""):
                values.extend(candidate if isinstance(candidate, (list, tuple, set)) else str(candidate).split(","))
        for candidate in (orders, instructions):
            if isinstance(candidate, Mapping):
                values.append(candidate)
            elif isinstance(candidate, list):
                values.extend(candidate)
        normalized: List[str] = []
        for value in values:
            if isinstance(value, Mapping):
                value = value.get("orderId", value.get("order_id"))
            item = cls._v3_order_id(value)
            if item not in normalized:
                normalized.append(item)
        return normalized

    @staticmethod
    def _bool_query(value: Any, default: bool) -> bool:
        if value in (None, ""):
            return default
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        raise MarketConfigurationError("SX Bet boolean query values must be true or false.")

    @staticmethod
    def _v3_date_bounds(start: Any, end: Any) -> Tuple[str, str]:
        start_text = str(start or "").strip()
        end_text = str(end or "").strip()
        for label, value in (("start_date", start_text), ("end_date", end_text)):
            if value:
                try:
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise MarketConfigurationError(f"SX Bet {label} must be ISO-8601.") from exc
                if parsed.tzinfo is None:
                    raise MarketConfigurationError(f"SX Bet {label} must include a timezone.")
        if start_text and end_text:
            start_dt = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end_text.replace("Z", "+00:00"))
            if end_dt < start_dt:
                raise MarketConfigurationError("SX Bet end_date must not be earlier than start_date.")
        return start_text, end_text

    @staticmethod
    def _position_status(value: Any) -> str:
        raw = str(value or "").strip().upper()
        if not raw:
            raise MarketConfigurationError(
                "SX Bet positions requires status (comma-separated MATCHED, LOCKED, SETTLED, or FAILED)."
            )
        statuses = [item.strip().upper() for item in raw.split(",") if item.strip()]
        allowed = {"MATCHED", "LOCKED", "SETTLED", "FAILED"}
        if not statuses or any(item not in allowed for item in statuses):
            raise MarketConfigurationError("SX Bet positions status values must be MATCHED, LOCKED, SETTLED, or FAILED.")
        unique = list(dict.fromkeys(statuses))
        return ",".join(unique)

    def copy_trade_from_activity(self, activity: Mapping[str, Any]) -> PaperOrderResult:
        raise UnsupportedFeatureError(
            self.market_id,
            "copy_trading",
            "SX Bet copy trading is unsupported because this adapter has no official account activity mirroring model.",
        )

    def websocket_connection_info(
        self,
        *,
        market_hashes: Optional[List[str]] = None,
        event_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        channels = self.websocket_channels(market_hashes=market_hashes, event_ids=event_ids)
        credential = self.resolve_credential("sx_bet_api_key", ("SX_BET_API_KEY",), label="SX_BET_API_KEY")
        return {
            "url": self.websocket_url,
            "token_endpoint": f"{self.api_base_url}/user/realtime-token/api-key",
            "requires_api_key_header": "X-Api-Key",
            "credential_source": credential.source if credential else None,
            "channels": channels,
            "subscription_options": {"positioned": True, "recoverable": True},
        }

    @staticmethod
    def websocket_channels(
        *,
        market_hashes: Optional[List[str]] = None,
        event_ids: Optional[List[str]] = None,
    ) -> List[str]:
        markets = [str(value).strip() for value in (market_hashes or []) if str(value).strip()]
        events = [str(value).strip() for value in (event_ids or []) if str(value).strip()]
        if not markets and not events:
            raise MarketConfigurationError("SX Bet WebSocket subscription requires market hashes or event ids.")
        channels = [f"order_book:market_{market}" for market in markets]
        channels.extend(f"order_book:event_{event_id}" for event_id in events)
        return channels

    def _fetch_active_markets(self, *, limit: int) -> List[Mapping[str, Any]]:
        params: Dict[str, Any] = {
            "pageSize": max(1, min(int(limit or 50), 100)),
        }
        for config_key, api_key in (
            ("sx_bet_only_main_line", "onlyMainLine"),
            ("sx_bet_live_only", "liveOnly"),
            ("sx_bet_league_id", "leagueId"),
            ("sx_bet_sport_ids", "sportIds"),
            ("sx_bet_bet_group", "betGroup"),
        ):
            value = self.config.get(config_key)
            if value not in (None, ""):
                params[api_key] = value
        data = self.runtime.get_json(self._url("/markets/active"), params=params)
        markets = data.get("data", {}).get("markets") if isinstance(data, Mapping) else []
        return [market for market in markets if isinstance(market, Mapping)] if isinstance(markets, list) else []

    def _get_market(self, market_hash: str) -> Mapping[str, Any]:
        clean_hash = str(market_hash or "").strip()
        if not clean_hash:
            raise MarketConfigurationError("SX Bet market hash cannot be empty.")
        data = self.runtime.get_json(self._url("/markets/find"), params={"marketHashes": clean_hash})
        markets = data.get("data") if isinstance(data, Mapping) else []
        if isinstance(markets, list):
            for market in markets:
                if isinstance(market, Mapping) and self._market_hash(market).lower() == clean_hash.lower():
                    return market
        raise MarketConfigurationError(f"SX Bet market {clean_hash!r} was not found.")

    def _fetch_orders(self, market_hash: str) -> List[Mapping[str, Any]]:
        params = {
            "marketHashes": market_hash,
            "baseToken": self.base_token,
            "perPage": max(1, min(int(self.config.get("sx_bet_orderbook_depth") or 100), 1000)),
            "sortBy": "percentage_odds",
            "sortAsc": False,
        }
        data = self.runtime.get_json(self._url("/orders"), params=params)
        orders = data.get("data") if isinstance(data, Mapping) else []
        return [order for order in orders if isinstance(order, Mapping)] if isinstance(orders, list) else []

    def _best_odds_price(self, market_hash: str, outcome: str) -> Optional[float]:
        data = self.runtime.get_json(
            self._url("/orders/odds/best"),
            params={"marketHashes": market_hash, "baseToken": self.base_token},
        )
        odds = data.get("data", {}).get("bestOdds") if isinstance(data, Mapping) else []
        if not isinstance(odds, list):
            return None
        for item in odds:
            if not isinstance(item, Mapping) or str(item.get("marketHash") or "").lower() != market_hash.lower():
                continue
            key = "outcomeOne" if outcome == "ONE" else "outcomeTwo"
            outcome_data = item.get(key)
            if isinstance(outcome_data, Mapping):
                return self._scaled_probability(outcome_data.get("percentageOdds"))
        return None

    def _url(self, path: str) -> str:
        clean_path = "/" + str(path or "").strip("/")
        return f"{self.api_base_url}{clean_path}"

    @property
    def base_token(self) -> str:
        return str(self.config.get("sx_bet_base_token") or DEFAULT_SX_BET_BASE_TOKEN)

    @property
    def executor_address(self) -> str:
        return str(self.config.get("sx_bet_executor_address") or DEFAULT_SX_BET_EXECUTOR)

    @property
    def base_token_decimals(self) -> int:
        return int(self.config.get("sx_bet_base_token_decimals") or 6)

    def _event_from_market(self, market: Mapping[str, Any]) -> MarketEvent:
        market_hash = self._market_hash(market)
        return MarketEvent(
            market_id=self.market_id,
            event_id=market_hash,
            title=self._market_title(market),
            url=self._market_url(market),
            status=self._status_from_market(market),
            raw=dict(market),
        )

    def _contracts_from_market(self, market: Mapping[str, Any]) -> List[MarketContract]:
        market_hash = self._market_hash(market)
        title = self._market_title(market)
        status = self._status_from_market(market)
        return [
            MarketContract(
                market_id=self.market_id,
                contract_id=self._contract_id(market_hash, "ONE"),
                event_id=market_hash,
                title=f"{title} - {str(market.get('outcomeOneName') or 'Outcome One')}",
                outcome=str(market.get("outcomeOneName") or "Outcome One"),
                url=self._market_url(market),
                status=status,
                raw={"market": dict(market), "outcome": "ONE"},
            ),
            MarketContract(
                market_id=self.market_id,
                contract_id=self._contract_id(market_hash, "TWO"),
                event_id=market_hash,
                title=f"{title} - {str(market.get('outcomeTwoName') or 'Outcome Two')}",
                outcome=str(market.get("outcomeTwoName") or "Outcome Two"),
                url=self._market_url(market),
                status=status,
                raw={"market": dict(market), "outcome": "TWO"},
            ),
        ]

    def _book_for_outcome(self, orders: List[Mapping[str, Any]], outcome: str) -> Tuple[List[OrderBookLevel], List[OrderBookLevel]]:
        desired_is_one = outcome == "ONE"
        bids: List[OrderBookLevel] = []
        asks: List[OrderBookLevel] = []
        for raw_order in orders:
            maker_is_one = bool(raw_order.get("isMakerBettingOutcomeOne"))
            maker_odds = self._scaled_probability(raw_order.get("percentageOdds"))
            remaining = self._remaining_order_size(raw_order)
            if maker_odds is None or remaining <= 0:
                continue
            if maker_is_one == desired_is_one:
                bids.append(OrderBookLevel(price=maker_odds, size=self._from_base_units(remaining)))
            else:
                taker_price = round(1.0 - maker_odds, 10)
                taker_size = self._taker_space(remaining, maker_odds)
                asks.append(OrderBookLevel(price=taker_price, size=self._from_base_units(taker_size)))
        bids.sort(key=lambda level: level.price, reverse=True)
        asks.sort(key=lambda level: level.price)
        return bids, asks

    def _build_unsigned_order(self, order: PaperOrderRequest, *, dry_run: bool) -> Dict[str, Any]:
        market_hash, outcome = self._split_contract_id(order.contract_id)
        side = str(order.side or "").upper()
        if order.limit_price is None:
            raise MarketConfigurationError("SX Bet orders require a limit price.")
        selected_price = self._limit_probability(order.limit_price)
        maker_is_one = outcome == "ONE" if side == "BUY" else outcome != "ONE"
        maker_price = selected_price if side == "BUY" else 1.0 - selected_price
        if maker_price <= 0.0 or maker_price >= 1.0:
            raise MarketConfigurationError("SX Bet maker odds must be between 0 and 1.")
        maker_address = str(order.metadata.get("maker") or self._maker_address(required=not dry_run))
        return {
            "marketHash": market_hash,
            "maker": maker_address,
            "totalBetSize": str(self._to_base_units(order.size)),
            "percentageOdds": str(self._to_odds_units(maker_price)),
            "baseToken": str(order.metadata.get("base_token") or self.base_token),
            "apiExpiry": int(order.metadata.get("api_expiry") or (time.time() + 3600)),
            "expiry": int(order.metadata.get("expiry") or DEFAULT_SX_BET_EXPIRY),
            "executor": str(order.metadata.get("executor") or self.executor_address),
            "isMakerBettingOutcomeOne": maker_is_one,
            "salt": str(order.metadata.get("salt") or secrets.randbits(256)),
        }

    def _signed_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        unsigned = self._build_unsigned_order(order, dry_run=False)
        explicit_signature = order.metadata.get("signature")
        if explicit_signature:
            return {**unsigned, "signature": str(explicit_signature)}
        private_key = self.resolve_credential(
            "sx_bet_private_key",
            ("SX_BET_PRIVATE_KEY",),
            required=True,
            label="SX_BET_PRIVATE_KEY",
        )
        return {**unsigned, "signature": self._sign_order(unsigned, private_key.value)}

    def _sign_order(self, order: Mapping[str, Any], private_key: str) -> str:
        try:
            from eth_abi.packed import encode_packed
            from eth_account import Account
            from eth_account.messages import encode_defunct
            from eth_utils import keccak, to_checksum_address
        except Exception as exc:
            raise MarketConfigurationError(
                "SX Bet live trading requires eth-account and eth-abi. Install project dependencies first."
            ) from exc
        encoded = encode_packed(
            ["bytes32", "address", "uint256", "uint256", "uint256", "uint256", "address", "address", "bool"],
            [
                bytes.fromhex(str(order["marketHash"]).removeprefix("0x")),
                to_checksum_address(str(order["baseToken"])),
                int(order["totalBetSize"]),
                int(order["percentageOdds"]),
                int(order["expiry"]),
                int(order["salt"]),
                to_checksum_address(str(order["maker"])),
                to_checksum_address(str(order["executor"])),
                bool(order["isMakerBettingOutcomeOne"]),
            ],
        )
        order_hash = keccak(encoded)
        signature = Account.sign_message(encode_defunct(primitive=order_hash), private_key=private_key).signature.hex()
        return signature if signature.startswith("0x") else f"0x{signature}"

    def _validate_order(self, order: PaperOrderRequest) -> None:
        self.ensure_order_market(order)
        self._split_contract_id(order.contract_id)
        side = str(order.side or "").upper()
        if side not in {"BUY", "SELL"}:
            raise MarketConfigurationError("SX Bet order side must be BUY or SELL.")
        if not self._is_positive_number(order.size):
            raise MarketConfigurationError("SX Bet order size must be positive.")
        if order.limit_price is not None:
            self._limit_probability(order.limit_price)

    def _maker_address(self, *, required: bool) -> str:
        credential = self.resolve_credential(
            "sx_bet_maker_address",
            ("SX_BET_MAKER_ADDRESS",),
            required=required,
            label="SX_BET_MAKER_ADDRESS",
        )
        if credential:
            return credential.value
        return "0x0000000000000000000000000000000000000000"

    @staticmethod
    def _market_matches_query(market: Mapping[str, Any], query: str) -> bool:
        haystack = " ".join(
            str(market.get(key) or "")
            for key in (
                "marketHash",
                "outcomeOneName",
                "outcomeTwoName",
                "teamOneName",
                "teamTwoName",
                "sportLabel",
                "leagueLabel",
                "group1",
                "sportXeventId",
            )
        ).lower()
        return query in haystack

    @staticmethod
    def _status_from_market(market: Mapping[str, Any]) -> str:
        status = str(market.get("status") or "").strip().lower()
        return "active" if status == "active" else status

    @staticmethod
    def _market_hash(market: Mapping[str, Any]) -> str:
        return str(market.get("marketHash") or "").strip()

    @staticmethod
    def _market_title(market: Mapping[str, Any]) -> str:
        one = str(market.get("outcomeOneName") or market.get("teamOneName") or "Outcome One")
        two = str(market.get("outcomeTwoName") or market.get("teamTwoName") or "Outcome Two")
        league = str(market.get("leagueLabel") or market.get("sportLabel") or "").strip()
        suffix = f" ({league})" if league else ""
        return f"{one} vs {two}{suffix}"

    @staticmethod
    def _market_url(market: Mapping[str, Any]) -> str:
        event_id = str(market.get("sportXeventId") or "").strip()
        if event_id:
            return f"https://sx.bet/event/{event_id}"
        return "https://sx.bet"

    @staticmethod
    def _split_contract_id(contract_id: str) -> Tuple[str, str]:
        raw = str(contract_id or "").strip()
        if not raw:
            raise MarketConfigurationError("SX Bet order requires a contract id.")
        if ":" in raw:
            market_hash, outcome = raw.rsplit(":", 1)
        else:
            market_hash, outcome = raw, "ONE"
        market_hash = market_hash.strip()
        outcome = outcome.strip().upper()
        if not market_hash:
            raise MarketConfigurationError("SX Bet contract id must include a market hash.")
        if outcome not in {"ONE", "TWO"}:
            raise MarketConfigurationError("SX Bet contract outcome must be ONE or TWO.")
        return market_hash, outcome

    @staticmethod
    def _contract_id(market_hash: str, outcome: str) -> str:
        return f"{market_hash}:{outcome.upper()}"

    @staticmethod
    def _remaining_order_size(order: Mapping[str, Any]) -> float:
        total = SxBetAdapter._safe_float(order.get("totalBetSize"))
        filled = SxBetAdapter._safe_float(order.get("fillAmount")) or 0.0
        pending = SxBetAdapter._safe_float(order.get("pendingFillAmount")) or 0.0
        return max(0.0, (total or 0.0) - filled - pending)

    @staticmethod
    def _taker_space(remaining_maker_size: float, maker_odds: float) -> float:
        if maker_odds <= 0:
            return 0.0
        return max(0.0, (remaining_maker_size * ODDS_SCALE / (maker_odds * ODDS_SCALE)) - remaining_maker_size)

    def _from_base_units(self, value: float) -> float:
        return float(value) / float(10**self.base_token_decimals)

    @staticmethod
    def _history_timestamp(value: Any, label: str) -> float:
        try:
            timestamp = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"SX Bet {label} timestamp must be numeric epoch seconds.") from exc
        if not math.isfinite(timestamp) or timestamp < 0:
            raise MarketConfigurationError(f"SX Bet {label} timestamp must be a finite nonnegative epoch second.")
        return timestamp

    @staticmethod
    def _candle_interval(resolution: str) -> int:
        intervals = {
            "1m": 60,
            "5m": 300,
            "15m": 900,
            "30m": 1800,
            "1h": 3600,
            "4h": 14400,
            "1d": 86400,
            "1w": 604800,
        }
        normalized = str(resolution or "").strip().lower()
        try:
            return intervals[normalized]
        except KeyError as exc:
            supported = ", ".join(intervals)
            raise MarketConfigurationError(f"SX Bet candle resolution must be one of: {supported}.") from exc

    def _candle_trade_limit(self) -> int:
        raw_limit = self.config.get("sx_bet_candle_trade_limit", 100)
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("SX Bet candle trade limit must be an integer between 1 and 100.") from exc
        if limit < 1 or limit > 100:
            raise MarketConfigurationError("SX Bet candle trade limit must be between 1 and 100.")
        return limit

    @staticmethod
    def _timestamp_seconds(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)):
            number = float(value)
            return number if math.isfinite(number) and number >= 0 else None
        text = str(value).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                number = float(text)
            except ValueError:
                return None
            return number if math.isfinite(number) and number >= 0 else None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()

    @staticmethod
    def _boolean(value: Any) -> Optional[bool]:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        text = str(value or "").strip().lower()
        if text in {"true", "1", "yes"}:
            return True
        if text in {"false", "0", "no"}:
            return False
        return None

    def _to_base_units(self, value: Any) -> int:
        amount = Decimal(str(value))
        units = amount * Decimal(10**self.base_token_decimals)
        return int(units.quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    @staticmethod
    def _to_odds_units(value: Any) -> int:
        odds = Decimal(str(value)) * Decimal(ODDS_SCALE)
        return int(odds.quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    @staticmethod
    def _scaled_probability(value: Any) -> Optional[float]:
        number = SxBetAdapter._safe_float(value)
        if number is None or not math.isfinite(number):
            return None
        if number > 1.0:
            number = number / ODDS_SCALE
        if number < 0.0 or number > 1.0:
            return None
        return number

    @staticmethod
    def _limit_probability(value: Any) -> float:
        number = SxBetAdapter._safe_float(value)
        if number is None or number <= 0.0 or number >= 1.0:
            raise MarketConfigurationError("SX Bet limit price must be between 0 and 1.")
        return number

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _is_positive_number(value: Any) -> bool:
        number = SxBetAdapter._safe_float(value)
        return number is not None and math.isfinite(number) and number > 0
