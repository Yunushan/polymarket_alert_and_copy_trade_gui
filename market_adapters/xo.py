from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .base import MarketAdapter
from .catalog import get_market_metadata
from .errors import MarketConfigurationError, MarketHTTPError
from .types import (
    MarketContract,
    MarketCandle,
    MarketEvent,
    MarketTrade,
    OrderBookLevel,
    OrderBookSnapshot,
    PaperOrderRequest,
    PaperOrderResult,
    PriceSnapshot,
)


DEFAULT_XO_BASE_URL = "https://api.xotrade.co/v1"
XO_REFERENCES = (
    "https://xotrade.co/documentation.html",
    "https://xotrade.co",
)
XO_ACCOUNT_OPERATIONS = (
    "account",
    "positions",
    "orders",
    "trades",
    "settlement",
    "settlement_history",
    "audit_logs",
)
XO_ORDER_MANAGEMENT_OPERATIONS = ("cancel_order",)
XO_ORDER_MANAGEMENT_CONFIRMATION = "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
XO_AUDIT_EVENT_TYPES = (
    "order_submitted",
    "order_filled",
    "order_cancelled",
    "balance_change",
    "transfer",
    "market_resolved",
)


class XOMarketAdapter(MarketAdapter):
    """XO Markets adapter using the documented HMAC REST API."""

    metadata = get_market_metadata("xo_market")
    account_recovery_operations = XO_ACCOUNT_OPERATIONS
    order_management_operations = XO_ORDER_MANAGEMENT_OPERATIONS

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        api_key = self.resolve_credential("xo_api_key", ("XO_API_KEY",), label="XO_API_KEY")
        api_secret = self.resolve_credential("xo_api_secret", ("XO_API_SECRET",), label="XO_API_SECRET")
        credential_sources = []
        for credential in (api_key, api_secret):
            if credential:
                credential_sources.append({"name": credential.name, "source": credential.source})
        health.update(
            {
                "api_base_url": self.api_base_url,
                "references": list(XO_REFERENCES),
                "live_trading_enabled": self.config_bool("live_trading_enabled", False),
                "account_recovery_operations": list(self.account_recovery_operations),
                "authenticated_account_endpoints": [
                    "GET /account",
                    "GET /positions",
                    "GET /orders",
                    "GET /trades",
                    "GET /markets/{market_id}/settlement",
                    "GET /markets/{market_id}/settlement/history",
                    "GET /audit/logs",
                ],
                "order_management_operations": list(self.order_management_operations),
                "order_management_enabled": self.config_bool("xo_order_management_enabled", False),
                "authenticated_order_management_endpoints": ["DELETE /orders/{order_id}"],
                "credential_sources": credential_sources,
            }
        )
        return health

    @property
    def api_base_url(self) -> str:
        configured = self.config.get("xo_api_base_url") or self.config.get("api_base_url")
        return str(configured or DEFAULT_XO_BASE_URL).rstrip("/")

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = max(1, min(int(limit or 50), 500))
        params: Dict[str, Any] = {"limit": desired, "status": self.config.get("xo_market_status", "open")}
        if query:
            params["search"] = str(query).strip()
        payload = self._request("GET", "/markets", params=params)
        markets = self._list_from_payload(payload, "markets", "data")
        return [self._event_from_market(market) for market in markets[:desired]]

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        market = self._get_market(event_id)
        return self._contracts_from_market(market)

    def get_orderbook(self, contract_id: str) -> OrderBookSnapshot:
        self.ensure_capability("orderbook_reading")
        market_id, outcome_id = self._split_contract_id(contract_id)
        payload = self._request("GET", f"/markets/{market_id}/outcomes/{outcome_id}/orderbook")
        book = payload.get("orderbook") if isinstance(payload, Mapping) else None
        if not isinstance(book, Mapping):
            book = payload if isinstance(payload, Mapping) else {}
        bids = self._levels(self._value_at(book, "bids", "buy"), descending=True)
        asks = self._levels(self._value_at(book, "asks", "sell"))
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
        orderbook = self.get_orderbook(self._contract_id(market_id, outcome_id))
        bid = orderbook.bids[0].price if orderbook.bids else None
        ask = orderbook.asks[0].price if orderbook.asks else None
        midpoint = (bid + ask) / 2.0 if bid is not None and ask is not None else None
        last = midpoint
        raw: Dict[str, Any] = dict(orderbook.raw)
        if last is None:
            market = self._get_market(market_id)
            outcome = self._find_outcome(market, outcome_id)
            last = self._safe_probability(outcome.get("price")) if outcome else None
            raw["market"] = dict(market)
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=self._contract_id(market_id, outcome_id),
            last=last,
            bid=bid,
            ask=ask,
            midpoint=midpoint,
            source="xo_orderbook",
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
        """Read XO's documented public market trade feed."""

        self.ensure_capability("trade_history")
        market_id, outcome_id = self._split_contract_id(contract_id)
        desired = self._history_limit(limit)
        after_seconds = self._timestamp_seconds(after) if after is not None else None
        before_seconds = self._timestamp_seconds(before) if before is not None else None
        if after is not None and after_seconds is None:
            raise MarketConfigurationError("XO after timestamp must be a finite non-negative number.")
        if before is not None and before_seconds is None:
            raise MarketConfigurationError("XO before timestamp must be a finite non-negative number.")
        if after_seconds is not None and before_seconds is not None and after_seconds > before_seconds:
            raise MarketConfigurationError("XO trade history after must not be after before.")
        # The public endpoint documents only the market path; filters are
        # applied locally rather than sending undocumented query parameters.
        payload = self._request("GET", f"/markets/{market_id}/trades")
        rows = self._list_from_payload(payload, "trades", "data")
        trades: List[MarketTrade] = []
        canonical = self._contract_id(market_id, outcome_id)
        for row in rows:
            row_market = str(row.get("market_id") or row.get("marketId") or "").strip()
            row_outcome = str(row.get("outcome_id") or row.get("outcomeId") or "").strip()
            if row_market and row_market != market_id:
                continue
            if row_outcome and row_outcome != outcome_id:
                continue
            trade_id = str(row.get("trade_id") or row.get("tradeId") or row.get("id") or "").strip()
            side = str(row.get("side") or "").strip().upper()
            price = self._safe_probability(row.get("price"))
            size = self._positive_number(row.get("qty") if row.get("qty") is not None else row.get("quantity"))
            if not trade_id or side not in {"BUY", "SELL"} or price is None or size is None:
                continue
            timestamp = self._timestamp_seconds(
                row.get("executed_at") or row.get("executedAt") or row.get("timestamp")
            )
            if after_seconds is not None and (timestamp is None or timestamp < after_seconds):
                continue
            if before_seconds is not None and (timestamp is None or timestamp > before_seconds):
                continue
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
        return trades[:desired]

    def list_candles(
        self,
        contract_id: str,
        *,
        resolution: str = "1h",
        from_timestamp: Optional[float] = None,
        to_timestamp: Optional[float] = None,
    ) -> List[MarketCandle]:
        """Read XO's documented market OHLCV candle feed."""

        self.ensure_capability("candle_history")
        market_id, outcome_id = self._split_contract_id(contract_id)
        if from_timestamp is None or to_timestamp is None:
            raise MarketConfigurationError(
                "XO candle history requires both from_timestamp and to_timestamp because the official API requires start_time and end_time."
            )
        start = self._iso_timestamp(from_timestamp, "from")
        end = self._iso_timestamp(to_timestamp, "to")
        if float(from_timestamp) > float(to_timestamp):
            raise MarketConfigurationError("XO candle history from_timestamp must not be after to_timestamp.")
        clean_resolution = str(resolution or "").strip().lower()
        if clean_resolution not in {"1m", "5m", "15m", "1h", "4h", "1d"}:
            raise MarketConfigurationError("XO candle resolution must be one of 1m, 5m, 15m, 1h, 4h, or 1d.")
        payload = self._request(
            "GET",
            f"/markets/{market_id}/candles",
            params={
                "outcome_id": outcome_id,
                "interval": clean_resolution,
                "start_time": start,
                "end_time": end,
                "limit": 1000,
            },
        )
        rows = self._list_from_payload(payload, "candles", "data")
        candles: List[MarketCandle] = []
        canonical = self._contract_id(market_id, outcome_id)
        for row in rows:
            values = {
                name: self._safe_probability(row.get(name))
                for name in ("open", "high", "low", "close")
            }
            if any(value is None for value in values.values()):
                continue
            timestamp = self._timestamp_seconds(row.get("timestamp") or row.get("time"))
            if timestamp is None:
                continue
            volume = self._nonnegative_number(row.get("volume"))
            candles.append(
                MarketCandle(
                    market_id=self.market_id,
                    contract_id=canonical,
                    timestamp=timestamp,
                    open=values["open"],
                    high=values["high"],
                    low=values["low"],
                    close=values["close"],
                    volume=volume,
                    raw=dict(row),
                )
            )
        return candles

    def account_recovery(self, operation: str, **kwargs: Any) -> Any:
        """Read one documented authenticated XO account surface.

        The operation names map only to fixed official paths.  Query values
        are bounded/allow-listed before the signed request is made, and
        market identifiers are validated before they enter a URL path.
        """

        normalized = str(operation or "").strip().lower()
        if normalized not in self.account_recovery_operations:
            supported = ", ".join(self.account_recovery_operations)
            raise MarketConfigurationError(f"XO account operation must be one of: {supported}.")

        if normalized == "account":
            return self._request("GET", "/account")
        if normalized == "positions":
            return self._request("GET", "/positions")

        limit = self._history_limit(kwargs.get("limit", 100))
        if normalized == "orders":
            del limit
            return self._request("GET", "/orders")
        if normalized == "trades":
            params = self._account_history_params(kwargs, limit=limit)
            return self._request("GET", "/trades", params=params)
        if normalized == "audit_logs":
            params = self._account_history_params(kwargs, limit=limit)
            event_type = kwargs.get("event_type")
            if event_type not in (None, ""):
                clean_event_type = str(event_type).strip().lower()
                if clean_event_type not in XO_AUDIT_EVENT_TYPES:
                    allowed = ", ".join(XO_AUDIT_EVENT_TYPES)
                    raise MarketConfigurationError(f"XO audit event_type must be one of: {allowed}.")
                params["event_type"] = clean_event_type
            return self._request("GET", "/audit/logs", params=params)

        market_id = self._safe_path_segment(kwargs.get("market_id"), "XO market id")
        if normalized == "settlement":
            return self._request("GET", f"/markets/{market_id}/settlement")
        params: Dict[str, Any] = {"limit": self._history_limit(kwargs.get("limit", 50))}
        cursor = kwargs.get("cursor")
        if cursor not in (None, ""):
            params["cursor"] = self._bounded_query_value(cursor, "XO settlement cursor")
        return self._request("GET", f"/markets/{market_id}/settlement/history", params=params)

    def manage_orders(self, operation: str, **kwargs: Any) -> Dict[str, Any]:
        """Run the documented, single-order cancellation mutation."""

        normalized = str(operation or "").strip().lower()
        if normalized not in self.order_management_operations:
            supported = ", ".join(self.order_management_operations)
            raise MarketConfigurationError(f"XO order-management operation must be one of: {supported}.")
        self.ensure_capability("live_trading")
        if not self.config_bool("xo_order_management_enabled", False):
            raise MarketConfigurationError(
                "XO order management is disabled by adapter config. "
                "Set xo_order_management_enabled=true only after reviewing cancellation risk."
            )
        self.ensure_live_trading_enabled("XO order management")
        if str(kwargs.get("confirm_order_management") or "").strip() != XO_ORDER_MANAGEMENT_CONFIRMATION:
            raise MarketConfigurationError(
                "XO order management requires exact confirmation text "
                f"{XO_ORDER_MANAGEMENT_CONFIRMATION}."
            )
        if bool(kwargs.get("async_request")):
            raise MarketConfigurationError("XO order-management requests are synchronous.")

        order_id = self._safe_path_segment(kwargs.get("order_id"), "XO order id")
        response = self._request("DELETE", f"/orders/{order_id}")
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
                "references": list(XO_REFERENCES),
            },
            "request": {"order_id": order_id},
            "response": response,
        }

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        self._validate_order(order)
        payload = self._order_payload(order)
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=order.contract_id,
            accepted=True,
            message=(
                f"DRY RUN: would place XO {order.side.upper()} order for "
                f"${order.size:.2f}"
                + (f" at limit {order.limit_price:.2f}" if order.limit_price is not None else "")
            ),
            raw={"request": payload},
        )

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        self.ensure_capability("live_trading")
        self._validate_order(order)
        preflight = self.preflight_live_order(order)
        payload = self._order_payload(order)
        response = self._request("POST", "/orders", json_body=payload)
        return {
            "market_id": self.market_id,
            "contract_id": order.contract_id,
            "live": True,
            "preflight": preflight,
            "request": payload,
            "response": response,
        }

    def copy_trade_from_activity(self, activity: Mapping[str, Any]) -> PaperOrderResult:
        """Build a local copy preview from XO's authenticated trade feed.

        XO exposes complete account trade rows through its documented
        ``GET /trades`` endpoint.  The shared copy surface remains
        simulation-first: this method validates and forwards the normalized
        fill to ``place_paper_order`` and never calls the live order route.
        """

        self.ensure_capability("copy_trading")
        market_id = str(activity.get("market_id") or activity.get("marketId") or "").strip()
        outcome_id = str(activity.get("outcome_id") or activity.get("outcomeId") or "").strip()
        contract_id = str(activity.get("contract_id") or "").strip()
        if not contract_id:
            if not market_id or not outcome_id:
                raise MarketConfigurationError(
                    "XO activity requires contract_id or both market_id and outcome_id."
                )
            contract_id = self._contract_id(market_id, outcome_id)
        else:
            parsed_market, parsed_outcome = self._split_contract_id(contract_id)
            if market_id and parsed_market != market_id:
                raise MarketConfigurationError("XO activity market_id does not match contract_id.")
            if outcome_id and parsed_outcome != outcome_id:
                raise MarketConfigurationError("XO activity outcome_id does not match contract_id.")
            market_id, outcome_id = parsed_market, parsed_outcome

        side = str(activity.get("side") or "").strip().upper()
        if side not in {"BUY", "SELL"}:
            raise MarketConfigurationError("XO activity side must be BUY or SELL.")
        size = self._positive_number(
            activity.get("size")
            if activity.get("size") is not None
            else activity.get("qty") if activity.get("qty") is not None else activity.get("quantity")
        )
        if size is None:
            raise MarketConfigurationError("XO activity quantity must be positive and finite.")
        price = self._safe_probability(activity.get("price"))
        if price is None or price <= 0.0 or price >= 1.0:
            raise MarketConfigurationError("XO activity price must be between 0 and 1.")
        if not str(activity.get("trade_id") or activity.get("tradeId") or activity.get("id") or "").strip():
            raise MarketConfigurationError("XO activity requires a documented trade id.")

        preview = self.place_paper_order(
            PaperOrderRequest(
                market_id=self.market_id,
                contract_id=contract_id,
                side=side,
                size=size,
                limit_price=price,
                metadata={"activity": dict(activity), "source": "xo_account_trades"},
            )
        )
        preview.raw["source"] = "xo_account_trades"
        preview.raw["activity"] = dict(activity)
        return preview

    def _get_market(self, market_id: str) -> Mapping[str, Any]:
        clean = str(market_id or "").strip()
        if not clean:
            raise MarketConfigurationError("XO market id cannot be empty.")
        payload = self._request("GET", f"/markets/{clean}")
        market = payload.get("market") if isinstance(payload, Mapping) else None
        if isinstance(market, Mapping):
            return market
        if isinstance(payload, Mapping):
            return payload
        raise MarketConfigurationError(f"XO market {clean!r} was not found.")

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Any = None,
    ) -> Any:
        body = "" if json_body is None else self._canonical_json(json_body)
        headers = self._auth_headers(method, path, body)
        if json_body is None and method.upper() == "GET":
            return self.runtime.get_json(self._url(path), params=params, headers=headers)
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        self.runtime.rate_limiter.wait()
        try:
            response = self.runtime.session.request(
                method.upper(),
                self._url(path),
                params=dict(params or {}),
                data=body,
                headers=headers,
                timeout=self.runtime.timeout_seconds,
            )
        except Exception as exc:
            raise MarketHTTPError(f"{self.market_id} HTTP request failed: {exc}") from exc
        status = int(getattr(response, "status_code", 0) or 0)
        if status >= 400:
            raise MarketHTTPError(f"{self.market_id} HTTP {status}: {str(getattr(response, 'text', ''))[:200]}")
        if status == 204 or not str(getattr(response, "text", "") or "").strip():
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise MarketHTTPError(f"{self.market_id} response was not valid JSON.") from exc

    def _url(self, path: str) -> str:
        clean_path = "/" + str(path or "").strip("/")
        return f"{self.api_base_url}{clean_path}"

    def _auth_headers(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        api_key = self.resolve_credential("xo_api_key", ("XO_API_KEY",), required=True, label="XO_API_KEY")
        api_secret = self.resolve_credential("xo_api_secret", ("XO_API_SECRET",), required=True, label="XO_API_SECRET")
        timestamp = str(int(time.time()))
        request_path = "/" + str(path or "").strip("/")
        message = f"{timestamp}{method.upper()}{request_path}{body}"
        signature = hmac.new(api_secret.value.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
        return {
            "XO-API-KEY": api_key.value,
            "XO-TIMESTAMP": timestamp,
            "XO-SIGNATURE": signature,
        }

    def _account_history_params(self, kwargs: Mapping[str, Any], *, limit: int) -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": limit}
        market_id = kwargs.get("market_id")
        if market_id not in (None, ""):
            params["market_id"] = self._safe_query_identifier(market_id, "XO market id")
        outcome_id = kwargs.get("outcome_id")
        if outcome_id not in (None, ""):
            params["outcome_id"] = self._safe_query_identifier(outcome_id, "XO outcome id")
        if kwargs.get("start_time") is not None:
            params["start_time"] = self._iso_timestamp(kwargs["start_time"], "start_time")
        if kwargs.get("end_time") is not None:
            params["end_time"] = self._iso_timestamp(kwargs["end_time"], "end_time")
        if kwargs.get("start_time") is not None and kwargs.get("end_time") is not None:
            start_seconds = self._timestamp_seconds(kwargs["start_time"])
            end_seconds = self._timestamp_seconds(kwargs["end_time"])
            if start_seconds is None or end_seconds is None:
                raise MarketConfigurationError("XO history timestamps must be valid ISO 8601 or numeric values.")
            if start_seconds > end_seconds:
                raise MarketConfigurationError("XO history start_time must not be after end_time.")
        return params

    @staticmethod
    def _bounded_query_value(value: Any, label: str, *, max_length: int = 500) -> str:
        clean = str(value or "").strip()
        if not clean or len(clean) > max_length or any(ord(char) < 32 for char in clean):
            raise MarketConfigurationError(f"{label} must be a non-empty bounded text value.")
        return clean

    @classmethod
    def _safe_query_identifier(cls, value: Any, label: str) -> str:
        clean = cls._bounded_query_value(value, label, max_length=200)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", clean):
            raise MarketConfigurationError(f"{label} contains unsupported characters.")
        return clean

    @classmethod
    def _safe_path_segment(cls, value: Any, label: str) -> str:
        clean = cls._safe_query_identifier(value, label)
        if clean in {".", ".."}:
            raise MarketConfigurationError(f"{label} cannot be a path traversal segment.")
        return clean

    def _event_from_market(self, market: Mapping[str, Any]) -> MarketEvent:
        market_id = self._market_id(market)
        return MarketEvent(
            market_id=self.market_id,
            event_id=market_id,
            title=str(market.get("title") or market.get("name") or market_id),
            url=self._market_url(market),
            status=str(market.get("status") or "").strip().lower(),
            raw=dict(market),
        )

    def _contracts_from_market(self, market: Mapping[str, Any]) -> List[MarketContract]:
        market_id = self._market_id(market)
        title = str(market.get("title") or market.get("name") or market_id)
        contracts: List[MarketContract] = []
        for outcome in self._outcomes_from_market(market):
            outcome_id = self._outcome_id(outcome)
            if not outcome_id:
                continue
            name = str(outcome.get("name") or outcome.get("title") or outcome_id)
            contracts.append(
                MarketContract(
                    market_id=self.market_id,
                    contract_id=self._contract_id(market_id, outcome_id),
                    event_id=market_id,
                    title=f"{title} - {name}",
                    outcome=name,
                    url=self._market_url(market),
                    status=str(market.get("status") or "").strip().lower(),
                    raw={"market": dict(market), "outcome": dict(outcome)},
                )
            )
        return contracts

    def _order_payload(self, order: PaperOrderRequest) -> Dict[str, Any]:
        market_id, outcome_id = self._split_contract_id(order.contract_id)
        payload: Dict[str, Any] = {
            "market_id": market_id,
            "outcome_id": outcome_id,
            "side": str(order.side or "").lower(),
            "type": str(order.metadata.get("type") or ("limit" if order.limit_price is not None else "market")),
            "amount_usd": float(order.size),
            "time_in_force": str(order.metadata.get("time_in_force") or "GTC"),
        }
        if order.limit_price is not None:
            payload["limit_price"] = self._limit_probability(order.limit_price)
        if "client_order_id" in order.metadata:
            payload["client_order_id"] = str(order.metadata["client_order_id"])
        return payload

    def _validate_order(self, order: PaperOrderRequest) -> None:
        self.ensure_order_market(order)
        self._split_contract_id(order.contract_id)
        if str(order.side or "").upper() not in {"BUY", "SELL"}:
            raise MarketConfigurationError("XO order side must be BUY or SELL.")
        if not self._is_positive_number(order.size):
            raise MarketConfigurationError("XO order amount_usd must be positive.")
        if order.limit_price is not None:
            self._limit_probability(order.limit_price)

    @staticmethod
    def _market_id(market: Mapping[str, Any]) -> str:
        return str(market.get("id") or market.get("market_id") or "").strip()

    @staticmethod
    def _outcome_id(outcome: Mapping[str, Any]) -> str:
        return str(outcome.get("id") or outcome.get("outcome_id") or "").strip()

    @staticmethod
    def _contract_id(market_id: str, outcome_id: str) -> str:
        return f"{market_id}:{outcome_id}"

    @staticmethod
    def _split_contract_id(contract_id: str) -> Tuple[str, str]:
        parts = [part.strip() for part in str(contract_id or "").split(":")]
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise MarketConfigurationError("XO contract id must be MARKET_ID:OUTCOME_ID.")
        return parts[0], parts[1]

    @staticmethod
    def _outcomes_from_market(market: Mapping[str, Any]) -> List[Mapping[str, Any]]:
        outcomes = market.get("outcomes")
        return [outcome for outcome in outcomes if isinstance(outcome, Mapping)] if isinstance(outcomes, list) else []

    @staticmethod
    def _find_outcome(market: Mapping[str, Any], outcome_id: str) -> Optional[Mapping[str, Any]]:
        for outcome in XOMarketAdapter._outcomes_from_market(market):
            if XOMarketAdapter._outcome_id(outcome) == str(outcome_id):
                return outcome
        return None

    @staticmethod
    def _list_from_payload(payload: Any, *keys: str) -> List[Mapping[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, Mapping)]
        if isinstance(payload, Mapping):
            for key in keys:
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, Mapping)]
        return []

    @staticmethod
    def _market_url(market: Mapping[str, Any]) -> str:
        raw = str(market.get("url") or "").strip()
        if raw:
            return raw
        market_id = XOMarketAdapter._market_id(market)
        return f"https://app.xotrade.co/markets/{market_id}" if market_id else "https://xotrade.co"

    @staticmethod
    def _value_at(data: Mapping[str, Any], *keys: str) -> Any:
        for key in keys:
            value = data.get(key)
            if value is not None:
                return value
        return []

    @staticmethod
    def _levels(raw: Any, *, descending: bool = False) -> List[OrderBookLevel]:
        levels: List[OrderBookLevel] = []
        if not isinstance(raw, list):
            return levels
        for item in raw:
            price = size = None
            if isinstance(item, Mapping):
                price = item.get("price")
                size = item.get("size") or item.get("qty") or item.get("quantity") or item.get("amount")
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                price, size = item[0], item[1]
            parsed_price = XOMarketAdapter._safe_probability(price)
            try:
                parsed_size = float(size)
            except (TypeError, ValueError):
                continue
            if parsed_price is not None and XOMarketAdapter._is_positive_number(parsed_size):
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
        if number > 1.0 and number <= 100.0:
            number /= 100.0
        return number if 0.0 <= number <= 1.0 else None

    @staticmethod
    def _limit_probability(value: Any) -> float:
        probability = XOMarketAdapter._safe_probability(value)
        if probability is None or probability <= 0.0 or probability >= 1.0:
            raise MarketConfigurationError("XO limit price must be between 0 and 1.")
        return probability

    @staticmethod
    def _is_positive_number(value: Any) -> bool:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False
        return math.isfinite(number) and number > 0

    @staticmethod
    def _positive_number(value: Any) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and number > 0 else None

    @staticmethod
    def _nonnegative_number(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and number >= 0 else None

    @staticmethod
    def _history_limit(value: Any) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("XO history limit must be an integer between 1 and 1000.") from exc
        if number < 1 or number > 1000:
            raise MarketConfigurationError("XO history limit must be between 1 and 1000.")
        return number

    @staticmethod
    def _timestamp_seconds(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        if not math.isfinite(number) or number < 0:
            return None
        return number / 1000.0 if number > 10_000_000_000 else number

    @classmethod
    def _iso_timestamp(cls, value: Any, label: str) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError as exc:
                raise MarketConfigurationError(f"XO {label} timestamp must be ISO 8601 or numeric.") from exc
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        if not math.isfinite(number) or number < 0:
            raise MarketConfigurationError(f"XO {label} timestamp must be a finite non-negative number.")
        return datetime.fromtimestamp(number, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    @staticmethod
    def _canonical_json(data: Mapping[str, Any]) -> str:
        return json.dumps(data, separators=(",", ":"), sort_keys=True)
