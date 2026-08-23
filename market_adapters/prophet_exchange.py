"""Official ProphetX/Prophet Exchange adapter.

ProphetX publishes separate read-only Market Data and authenticated Trading API
contracts.  This adapter uses the documented affiliate endpoints for event,
market, price, and available-quantity reads, keeps paper orders local, and
submits only the documented market-maker order shape behind the shared live
safety gate.  It never scrapes the consumer site or stores credentials.
"""

from __future__ import annotations

import math
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .base import MarketAdapter
from .catalog import get_market_metadata
from .errors import MarketConfigurationError, UnsupportedFeatureError
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


DEFAULT_PROPHET_EXCHANGE_API_BASE_URL = "https://cash.api.prophetx.co/partner"
DEFAULT_PROPHET_EXCHANGE_API_VERSION = "v3"
PROPHET_EXCHANGE_REFERENCES = (
    "https://docs.prophetx.co/",
    "https://docs.prophetx.co/docs/market-data-integration",
    "https://docs.prophetx.co/docs/integration",
    "https://docs.prophetx.co/docs/wallets",
    "https://docs.prophetx.co/reference/post_auth-login-2",
    "https://docs.prophetx.co/reference/get_mm-search-markets-2",
    "https://partner-docs.prophetx.co/swagger/mm/index.html",
)

PROPHET_EXCHANGE_ACCOUNT_OPERATIONS = ("balance", "transactions")
PROPHET_EXCHANGE_ORDER_MANAGEMENT_OPERATIONS = ("cancel_order", "cancel_orders")
PROPHET_EXCHANGE_ORDER_MANAGEMENT_CONFIRMATION = "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
PROPHET_EXCHANGE_TRANSACTION_LIMIT_MAX = 500
PROPHET_EXCHANGE_TRADE_LIMIT_MAX = 100
PROPHET_EXCHANGE_CANCEL_BATCH_MAX = 100

_NUMERIC_ID_RE = re.compile(r"^[0-9]{1,32}$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ProphetExchangeAdapter(MarketAdapter):
    """ProphetX REST adapter with guarded Trading API order submission."""

    metadata = get_market_metadata("prophet_exchange")
    # The documented Trading API submit_order shape is a market-maker quote
    # (strike_id, decimal price, quantity), not a consumer sell/close order.
    live_order_sides = ("BUY",)
    account_recovery_operations = PROPHET_EXCHANGE_ACCOUNT_OPERATIONS
    order_management_operations = PROPHET_EXCHANGE_ORDER_MANAGEMENT_OPERATIONS

    def __init__(self, config: Optional[Mapping[str, Any]] = None, **kwargs: Any) -> None:
        super().__init__(config, **kwargs)
        self._access_token: Optional[str] = None

    @property
    def api_base_url(self) -> str:
        configured = self.config.get("prophet_exchange_api_base_url") or self.config.get("api_base_url")
        return str(configured or DEFAULT_PROPHET_EXCHANGE_API_BASE_URL).rstrip("/")

    @property
    def api_version(self) -> str:
        value = str(self.config.get("prophet_exchange_api_version") or DEFAULT_PROPHET_EXCHANGE_API_VERSION).strip()
        if not re.fullmatch(r"v[0-9]{1,3}", value):
            raise MarketConfigurationError("Prophet Exchange API version must look like v3.")
        return value

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        credentials = []
        for credential in (
            self.resolve_credential(
                "prophet_exchange_api_key",
                ("PROPHET_EXCHANGE_API_KEY",),
                label="PROPHET_EXCHANGE_API_KEY",
            ),
            self.resolve_credential(
                "prophet_exchange_access_token",
                ("PROPHET_EXCHANGE_ACCESS_TOKEN",),
                label="PROPHET_EXCHANGE_ACCESS_TOKEN",
            ),
            self.resolve_credential(
                "prophet_exchange_access_key",
                ("PROPHET_EXCHANGE_ACCESS_KEY",),
                label="PROPHET_EXCHANGE_ACCESS_KEY",
            ),
            self.resolve_credential(
                "prophet_exchange_secret_key",
                ("PROPHET_EXCHANGE_SECRET_KEY",),
                label="PROPHET_EXCHANGE_SECRET_KEY",
            ),
        ):
            if credential:
                credentials.append({"name": credential.name, "source": credential.source})
        health.update(
            {
                "api_base_url": self.api_base_url,
                "api_version": self.api_version,
                "credential_sources": credentials,
                "references": list(PROPHET_EXCHANGE_REFERENCES),
                "market_data_api_key_required": True,
                "trading_api_credentials_required": True,
                "live_trading_supported": True,
                "live_trading_enabled": self.config_bool("live_trading_enabled", False),
                "live_order_shape": "external_id + strike_id + decimal price + quantity",
                "account_recovery_operations": list(self.account_recovery_operations),
                "authenticated_account_endpoints": [
                    "GET /v4/mm/get_balance",
                    "GET /v4/mm/get_transactions",
                ],
                "order_management_operations": list(self.order_management_operations),
                "order_management_enabled": self.config_bool(
                    "prophet_exchange_order_management_enabled", False
                ),
                "order_management_endpoints": [
                    "POST /mm/cancel_order",
                    "POST /mm/cancel_multiple_orders",
                ],
            }
        )
        return health

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = max(1, min(int(limit or 50), 1000))
        params: Dict[str, Any] = {}
        tournament_id = self.config.get("prophet_exchange_tournament_id")
        if tournament_id not in (None, ""):
            params["tournament_id"] = self._required_id(tournament_id, "tournament")
        payload = self._get("/affiliate/get_sport_events", params=params or None)
        events = self._rows(payload, "sport_events", "events")
        needle = str(query or "").strip().lower()
        if needle:
            events = [event for event in events if needle in self._search_text(event)]
        return [self._event_from_payload(event) for event in events[:desired]]

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        event = self._required_id(event_id, "event")
        markets = self._markets_for_event(event)
        contracts: List[MarketContract] = []
        for market in markets:
            market_id = self._id(market, "market_id")
            if not market_id:
                continue
            market_title = str(self._value(market, "name", "title", "market_type") or market_id)
            status = str(self._value(market, "state", "status") or "")
            for selection in self._selection_rows(market):
                outcome_id = self._id(selection, "outcome_id", "outcomeId", "id")
                if not outcome_id:
                    continue
                line_id = self._line_id(selection)
                contract_id = self._contract_id(event, market_id, outcome_id, line_id)
                outcome = str(self._value(selection, "name", "label", "title") or outcome_id)
                contracts.append(
                    MarketContract(
                        market_id=self.market_id,
                        contract_id=contract_id,
                        event_id=event,
                        title=f"{market_title} - {outcome}",
                        outcome=outcome,
                        url=str(self._value(market, "url", "slug") or market_id),
                        status=status,
                        raw={"market": dict(market), "selection": dict(selection)},
                    )
                )
        return contracts

    def get_orderbook(self, contract_id: str) -> OrderBookSnapshot:
        self.ensure_capability("orderbook_reading")
        event, market_id, outcome_id, line_id = self._split_contract_id(contract_id)
        selection, market = self._selection_for_contract(event, market_id, outcome_id, line_id)
        probability = self._selection_probability(selection)
        quantity = self._positive_float(self._value(selection, "quantity", "available_quantity", "availableQuantity"))
        asks = [OrderBookLevel(price=probability, size=quantity)] if probability is not None and quantity is not None else []
        canonical = self._contract_id(event, market_id, outcome_id, line_id)
        return OrderBookSnapshot(
            market_id=self.market_id,
            contract_id=canonical,
            bids=[],
            asks=asks,
            raw={"market": dict(market), "selection": dict(selection), "quote_side": "available_quantity"},
        )

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        book = self.get_orderbook(contract_id)
        ask = book.asks[0].price if book.asks else None
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=book.contract_id,
            last=ask,
            bid=None,
            ask=ask,
            midpoint=None,
            source="prophetx_affiliate_market_data",
            raw=book.raw,
        )

    def list_trades(
        self,
        contract_id: str,
        *,
        limit: int = 50,
        before: Optional[float] = None,
        after: Optional[float] = None,
    ) -> List[MarketTrade]:
        """Normalize the documented v4 filled-trade feed.

        ProphetX's ``get_trades`` rows carry the matched price, quantity,
        timestamp, and order id, while the contract identity is returned by
        the documented ``get_order/{id}`` endpoint.  Each row is therefore
        enriched through that fixed path and is discarded unless its event,
        market, outcome, and strike identify the requested contract exactly.
        The market-maker API exposes a BUY quote shape only, so BUY is the
        explicit side for this adapter rather than an inferred sell/close.
        """

        self.ensure_capability("trade_history")
        event_id, market_id, outcome_id, line_id = self._split_contract_id(contract_id)
        desired = self._bounded_trade_limit(limit)
        after_ts = self._history_timestamp(after, "after") if after is not None else None
        before_ts = self._history_timestamp(before, "before") if before is not None else None
        if after_ts is not None and before_ts is not None and before_ts < after_ts:
            raise MarketConfigurationError("Prophet Exchange trade history requires before to be at or after after.")

        params: Dict[str, Any] = {"limit": desired}
        if after_ts is not None:
            params["from"] = int(after_ts)
        if before_ts is not None:
            params["to"] = int(before_ts)
        payload = self._get("/v4/mm/get_trades", params=params, trading=True)
        rows = self._rows(payload, "trades")
        target_selection, _target_market = self._selection_for_contract(
            event_id, market_id, outcome_id, line_id
        )
        target_strike = self._value(target_selection, "strike_id", "strikeId")
        trades: List[MarketTrade] = []
        for raw in rows:
            if not isinstance(raw, Mapping):
                continue
            trade_id = self._id(raw, "trade_id", "tradeId")
            order_id = self._value(raw, "order_id", "orderId")
            price = self._probability(self._value(raw, "price", "odds", "decimal_price", "decimalPrice"))
            size = self._positive_float(self._value(raw, "quantity", "size", "stake", "amount"))
            timestamp = self._timestamp_seconds(
                self._value(raw, "matched_at", "matchedAt", "timestamp", "created_at", "createdAt")
            )
            if not trade_id or order_id in (None, "") or price is None or size is None or timestamp is None:
                continue
            if after_ts is not None and timestamp < after_ts:
                continue
            if before_ts is not None and timestamp > before_ts:
                continue
            order = self._order_for_trade(order_id)
            if not self._order_matches_contract(
                order,
                event_id,
                market_id,
                outcome_id,
                line_id,
                target_strike,
            ):
                continue
            trades.append(
                MarketTrade(
                    market_id=self.market_id,
                    contract_id=self._contract_id(event_id, market_id, outcome_id, line_id),
                    trade_id=trade_id,
                    side="BUY",
                    price=price,
                    size=size,
                    timestamp=timestamp,
                    raw={
                        "source": "prophetx_v4_get_trades",
                        "side_inferred": "BUY_ONLY_MARKET_MAKER_API",
                        "trade": dict(raw),
                        "order": dict(order),
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
        """Derive bounded OHLCV candles from authenticated matched trades."""

        self.ensure_capability("candle_history")
        interval = self._candle_interval(resolution)
        start_ts = self._history_timestamp(from_timestamp, "from_timestamp") if from_timestamp is not None else None
        end_ts = self._history_timestamp(to_timestamp, "to_timestamp") if to_timestamp is not None else None
        if start_ts is not None and end_ts is not None and end_ts < start_ts:
            raise MarketConfigurationError(
                "Prophet Exchange candle history requires to_timestamp to be at or after from_timestamp."
            )
        trades = self.list_trades(
            contract_id,
            limit=self._bounded_trade_limit(self.config.get("prophet_exchange_candle_trade_limit", 100)),
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

        event_id, market_id, outcome_id, line_id = self._split_contract_id(contract_id)
        canonical = self._contract_id(event_id, market_id, outcome_id, line_id)
        normalized_resolution = str(resolution or "").strip().lower()
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
                    "source": "prophetx_v4_authenticated_trades",
                    "derived": True,
                    "resolution": normalized_resolution,
                    "interval_seconds": interval,
                    "trade_ids": list(bucket["trade_ids"]),
                },
            )
            for bucket_timestamp, bucket in sorted(buckets.items())
        ]

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        event, market_id, outcome_id, line_id, selection = self._validate_order(order)
        payload = self._order_payload(order, selection=selection, require_strike=False)
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=self._contract_id(event, market_id, outcome_id, line_id),
            accepted=True,
            message=(
                f"DRY RUN: would place Prophet Exchange BUY for {float(order.size):.4f} quantity"
                + (f" at probability {float(order.limit_price):.4f}" if order.limit_price is not None else "")
            ),
            raw={"request": payload, "dry_run": True},
        )

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        self.ensure_capability("live_trading")
        event, market_id, outcome_id, line_id, selection = self._validate_order(order)
        preflight = self.preflight_live_order(order)
        payload = self._order_payload(order, selection=selection, require_strike=True)
        response = self._request_json("POST", "/mm/submit_order", payload, trading=True)
        return {
            "market_id": self.market_id,
            "contract_id": self._contract_id(event, market_id, outcome_id, line_id),
            "live": True,
            "preflight": preflight,
            "request": payload,
            "response": response,
        }

    def account_recovery(self, operation: str, **kwargs: Any) -> Any:
        """Read the documented ProphetX market-maker wallet feeds."""

        normalized = str(operation or "").strip().lower()
        if normalized not in self.account_recovery_operations:
            raise MarketConfigurationError(
                "Prophet Exchange account operation must be one of: "
                + ", ".join(self.account_recovery_operations)
                + "."
            )
        if normalized == "balance":
            return self._get("/v4/mm/get_balance", trading=True)

        params: Dict[str, Any] = {}
        raw_cursor = kwargs.get("cursor", kwargs.get("next"))
        if raw_cursor not in (None, ""):
            params["next"] = self._non_negative_integer(raw_cursor, "transaction cursor")
        params["limit"] = self._bounded_transaction_limit(kwargs.get("limit", 10))
        return self._get("/v4/mm/get_transactions", params=params, trading=True)

    def manage_orders(self, operation: str, **kwargs: Any) -> Dict[str, Any]:
        """Cancel ProphetX market-maker orders through fixed documented paths."""

        normalized = str(operation or "").strip().lower()
        if normalized not in self.order_management_operations:
            raise MarketConfigurationError(
                "Prophet Exchange order-management operation must be one of: "
                + ", ".join(self.order_management_operations)
                + "."
            )
        self.ensure_capability("live_trading")
        if not self.config_bool("prophet_exchange_order_management_enabled", False):
            raise MarketConfigurationError(
                "Prophet Exchange order management is disabled by adapter config. "
                "Set prophet_exchange_order_management_enabled=true only after reviewing live-order risk controls."
            )
        self.ensure_live_trading_enabled("Prophet Exchange order management")
        if str(kwargs.get("confirm_order_management") or "").strip() != PROPHET_EXCHANGE_ORDER_MANAGEMENT_CONFIRMATION:
            raise MarketConfigurationError(
                "Prophet Exchange order management requires exact confirmation text "
                f"{PROPHET_EXCHANGE_ORDER_MANAGEMENT_CONFIRMATION}."
            )
        if bool(kwargs.get("async_request")):
            raise MarketConfigurationError("Prophet Exchange order cancellation does not support async_request.")

        if normalized == "cancel_order":
            order = self._cancel_order_entry(kwargs.get("order_id"), kwargs.get("external_id"))
            endpoint = "/mm/cancel_order"
            request_body: Dict[str, Any] = order
        else:
            raw_orders = kwargs.get("orders")
            if raw_orders in (None, ""):
                raw_orders = kwargs.get("instructions")
            if not isinstance(raw_orders, list) or not raw_orders:
                raise MarketConfigurationError(
                    "Prophet Exchange cancel_orders requires a non-empty JSON array of order entries."
                )
            if len(raw_orders) > PROPHET_EXCHANGE_CANCEL_BATCH_MAX:
                raise MarketConfigurationError(
                    f"Prophet Exchange cancel_orders is limited to {PROPHET_EXCHANGE_CANCEL_BATCH_MAX} entries."
                )
            orders = []
            seen_order_ids = set()
            seen_external_ids = set()
            for entry in raw_orders:
                if not isinstance(entry, Mapping):
                    raise MarketConfigurationError("Prophet Exchange cancel_orders entries must be objects.")
                order = self._cancel_order_entry(entry.get("order_id"), entry.get("external_id"))
                if order["order_id"] in seen_order_ids or order["external_id"] in seen_external_ids:
                    raise MarketConfigurationError("Prophet Exchange cancel_orders entries must be unique.")
                seen_order_ids.add(order["order_id"])
                seen_external_ids.add(order["external_id"])
                orders.append(order)
            endpoint = "/mm/cancel_multiple_orders"
            request_body = {"data": orders}

        response = self._request_json("POST", endpoint, request_body, trading=True)
        return {
            "market_id": self.market_id,
            "operation": normalized,
            "live": True,
            "request": request_body,
            "preflight": {
                "market_id": self.market_id,
                "display_name": self.display_name,
                "feature": "order management",
                "operation": normalized,
                "live_trading_enabled": True,
                "order_management_enabled": True,
                "confirmed": True,
                "requires_credentials": True,
                "endpoint": f"POST {endpoint}",
                "references": list(PROPHET_EXCHANGE_REFERENCES),
            },
            "response": response,
        }

    def copy_trade_from_activity(self, activity: Mapping[str, Any]) -> PaperOrderResult:
        raise UnsupportedFeatureError(
            self.market_id,
            "copy_trading",
            "Prophet Exchange copy trading is unsupported because the official API does not expose account-activity mirroring.",
        )

    def _markets_for_event(self, event_id: str) -> List[Mapping[str, Any]]:
        payload = self._get(
            f"/{self.api_version}/affiliate/get_markets",
            params={"event_id": int(event_id)},
        )
        rows = self._rows(payload, "markets")
        return rows

    def _selection_for_contract(
        self,
        event_id: str,
        market_id: str,
        outcome_id: str,
        line_id: str,
    ) -> Tuple[Mapping[str, Any], Mapping[str, Any]]:
        for market in self._markets_for_event(event_id):
            if self._id(market, "market_id") != market_id:
                continue
            for selection in self._selection_rows(market):
                if self._id(selection, "outcome_id", "outcomeId", "id") != outcome_id:
                    continue
                if self._line_id(selection) == line_id:
                    return selection, market
        raise MarketConfigurationError(
            f"Prophet Exchange contract {market_id}:{outcome_id}:{line_id} was not found in event {event_id}."
        )

    def _get(
        self,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        trading: bool = False,
    ) -> Any:
        return self.runtime.get_json(
            self._url(self.api_base_url, path),
            params=params,
            headers=self._headers(required=True, trading=trading),
        )

    def _order_for_trade(self, order_id: Any) -> Mapping[str, Any]:
        safe_order_id = self._safe_reference(order_id, "order")
        payload = self._get(f"/v4/mm/get_order/{safe_order_id}", trading=True)
        mapping = self._mapping_payload(payload)
        order = mapping.get("order") if isinstance(mapping.get("order"), Mapping) else mapping
        return dict(order) if isinstance(order, Mapping) else {}

    @classmethod
    def _order_matches_contract(
        cls,
        order: Mapping[str, Any],
        event_id: str,
        market_id: str,
        outcome_id: str,
        line_id: str,
        target_strike: Any,
    ) -> bool:
        row_event = cls._value(order, "sport_event_id", "event_id", "eventId")
        row_market = cls._value(order, "market_id", "marketId")
        row_outcome = cls._value(order, "outcome_id", "outcomeId")
        if str(row_event or "").strip() != event_id:
            return False
        if str(row_market or "").strip() != market_id:
            return False
        if str(row_outcome or "").strip() != outcome_id:
            return False
        row_line = cls._value(order, "line_id", "lineId")
        if row_line not in (None, "") and str(row_line).strip() != line_id:
            return False
        row_strike = cls._value(order, "strike_id", "strikeId")
        if target_strike not in (None, "") and row_strike not in (None, ""):
            return str(row_strike).strip() == str(target_strike).strip()
        return True

    def _request_json(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any],
        *,
        trading: bool,
    ) -> Any:
        return self.runtime.request_json(
            method,
            self._url(self.api_base_url, path),
            json_body=dict(body),
            headers={"Content-Type": "application/json", **self._headers(required=True, trading=trading)},
        )

    def _headers(self, *, required: bool, trading: bool) -> Dict[str, str]:
        token = self.resolve_credential(
            "prophet_exchange_access_token",
            ("PROPHET_EXCHANGE_ACCESS_TOKEN",),
            label="PROPHET_EXCHANGE_ACCESS_TOKEN",
        )
        if token:
            return {"Authorization": token.value}
        if not trading:
            api_key = self.resolve_credential(
                "prophet_exchange_api_key",
                ("PROPHET_EXCHANGE_API_KEY",),
                label="PROPHET_EXCHANGE_API_KEY",
            )
            if api_key:
                return {"Authorization": api_key.value}
        access = self.resolve_credential(
            "prophet_exchange_access_key",
            ("PROPHET_EXCHANGE_ACCESS_KEY",),
            required=required,
            label="PROPHET_EXCHANGE_ACCESS_KEY",
        )
        secret = self.resolve_credential(
            "prophet_exchange_secret_key",
            ("PROPHET_EXCHANGE_SECRET_KEY",),
            required=required,
            label="PROPHET_EXCHANGE_SECRET_KEY",
        )
        if access is None or secret is None:
            return {}
        if self._access_token:
            return {"Authorization": self._access_token}
        payload = self.runtime.request_json(
            "POST",
            self._url(self.api_base_url, "/auth/login"),
            json_body={"access_key": access.value, "secret_key": secret.value},
            headers={"Content-Type": "application/json"},
        )
        mapping = self._mapping_payload(payload)
        value = self._value(mapping, "access_token", "accessToken", "token")
        if not value:
            raise MarketConfigurationError("Prophet Exchange login response did not include an access token.")
        self._access_token = str(value)
        return {"Authorization": self._access_token}

    @classmethod
    def _cancel_order_entry(cls, order_id: Any, external_id: Any) -> Dict[str, str]:
        clean_order_id = cls._safe_reference(order_id, "order")
        clean_external_id = cls._safe_reference(external_id, "external")
        return {"external_id": clean_external_id, "order_id": clean_order_id}

    @staticmethod
    def _safe_reference(value: Any, label: str) -> str:
        clean = str(value or "").strip()
        if not clean or not _SEGMENT_RE.fullmatch(clean):
            raise MarketConfigurationError(
                f"Prophet Exchange {label} reference must be a non-empty path-safe token."
            )
        return clean

    @staticmethod
    def _non_negative_integer(value: Any, label: str) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"Prophet Exchange {label} must be an integer.") from exc
        if number < 0:
            raise MarketConfigurationError(f"Prophet Exchange {label} must be non-negative.")
        return number

    @classmethod
    def _bounded_transaction_limit(cls, value: Any) -> int:
        limit = cls._non_negative_integer(value, "transaction limit")
        if limit < 1 or limit > PROPHET_EXCHANGE_TRANSACTION_LIMIT_MAX:
            raise MarketConfigurationError(
                f"Prophet Exchange transaction limit must be between 1 and {PROPHET_EXCHANGE_TRANSACTION_LIMIT_MAX}."
            )
        return limit

    @classmethod
    def _bounded_trade_limit(cls, value: Any) -> int:
        limit = cls._non_negative_integer(value, "trade limit")
        if limit < 1 or limit > PROPHET_EXCHANGE_TRADE_LIMIT_MAX:
            raise MarketConfigurationError(
                f"Prophet Exchange trade limit must be between 1 and {PROPHET_EXCHANGE_TRADE_LIMIT_MAX}."
            )
        return limit

    @staticmethod
    def _timestamp_seconds(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            number = float(value)
            if not math.isfinite(number):
                return None
            return number / 1000.0 if number > 100_000_000_000 else number
        text = str(value).strip()
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

    @classmethod
    def _history_timestamp(cls, value: Any, label: str) -> float:
        timestamp = cls._timestamp_seconds(value)
        if timestamp is None or timestamp < 0 or not math.isfinite(timestamp):
            raise MarketConfigurationError(
                f"Prophet Exchange {label} timestamp must be a valid non-negative epoch or ISO time."
            )
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
            raise MarketConfigurationError(
                "Prophet Exchange candle resolution must be one of: " + ", ".join(intervals)
            )
        return intervals[normalized]

    def _validate_order(
        self, order: PaperOrderRequest
    ) -> Tuple[str, str, str, str, Mapping[str, Any]]:
        self.ensure_order_market(order)
        event, market_id, outcome_id, line_id = self._split_contract_id(order.contract_id)
        if str(order.side or "").upper() not in self.live_order_sides:
            raise MarketConfigurationError("Prophet Exchange orders currently support BUY only through the documented API.")
        size = self._positive_float(order.size)
        if size is None:
            raise MarketConfigurationError("Prophet Exchange order quantity must be positive and finite.")
        if order.limit_price is not None and self._probability(order.limit_price) is None:
            raise MarketConfigurationError("Prophet Exchange order probability must be between 0 and 1.")
        selection, _market = self._selection_for_contract(event, market_id, outcome_id, line_id)
        return event, market_id, outcome_id, line_id, selection

    def _order_payload(
        self,
        order: PaperOrderRequest,
        *,
        selection: Mapping[str, Any],
        require_strike: bool,
    ) -> Dict[str, Any]:
        existing = order.metadata.get("prophet_exchange_order")
        if isinstance(existing, Mapping):
            return dict(existing)
        probability = self._probability(order.limit_price)
        if probability is None:
            raise MarketConfigurationError("Prophet Exchange orders require a limit probability.")
        strike_id = order.metadata.get("strike_id") or self._value(selection, "strike_id", "strikeId", "line_id", "lineId")
        if require_strike and strike_id in (None, ""):
            raise MarketConfigurationError(
                "Prophet Exchange live orders require the documented strike_id in the market payload or order metadata."
            )
        payload: Dict[str, Any] = {
            "external_id": str(order.metadata.get("external_id") or uuid.uuid4().hex),
            "strike_id": str(strike_id or ""),
            "price": round(1.0 / probability, 8),
            "quantity": float(order.size),
        }
        return payload

    @classmethod
    def _event_from_payload(cls, event: Mapping[str, Any]) -> MarketEvent:
        event_id = cls._id(event, "event_id")
        return MarketEvent(
            market_id=cls.metadata.market_id,
            event_id=event_id,
            title=str(cls._value(event, "name", "title", "description") or event_id),
            url=str(cls._value(event, "url", "slug") or event_id),
            status=str(cls._value(event, "state", "status") or "").lower(),
            raw=dict(event),
        )

    @classmethod
    def _selection_rows(cls, market: Mapping[str, Any]) -> List[Mapping[str, Any]]:
        value = market.get("selections") or market.get("outcomes") or []
        if isinstance(value, Mapping):
            value = list(value.values())
        if not isinstance(value, list):
            return []
        rows: List[Mapping[str, Any]] = []
        for item in value:
            if isinstance(item, Mapping):
                rows.append(item)
            elif isinstance(item, list):
                rows.extend(row for row in item if isinstance(row, Mapping))
        return rows

    @classmethod
    def _rows(cls, payload: Any, *keys: str) -> List[Mapping[str, Any]]:
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, Mapping)]
        if not isinstance(payload, Mapping):
            return []
        data = payload.get("data")
        if isinstance(data, list):
            return [row for row in data if isinstance(row, Mapping)]
        if isinstance(data, Mapping):
            for key in keys:
                value = data.get(key)
                if isinstance(value, list):
                    return [row for row in value if isinstance(row, Mapping)]
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, Mapping)]
        return []

    @classmethod
    def _mapping_payload(cls, payload: Any) -> Dict[str, Any]:
        if isinstance(payload, Mapping):
            data = payload.get("data")
            return dict(data) if isinstance(data, Mapping) else dict(payload)
        return {}

    @classmethod
    def _search_text(cls, payload: Mapping[str, Any]) -> str:
        return " ".join(
            str(cls._value(payload, key) or "")
            for key in ("event_id", "name", "title", "description", "home_team", "away_team", "sport")
        ).lower()

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

    @classmethod
    def _line_id(cls, selection: Mapping[str, Any]) -> str:
        value = cls._value(selection, "line_id", "lineId")
        if value in (None, ""):
            return "default"
        clean = str(value).strip()
        if not _SEGMENT_RE.fullmatch(clean):
            raise MarketConfigurationError("Prophet Exchange line_id contains unsupported characters.")
        return clean

    @classmethod
    def _selection_probability(cls, selection: Mapping[str, Any]) -> Optional[float]:
        return cls._probability(cls._value(selection, "price", "odds", "decimal_price", "decimalPrice"))

    @staticmethod
    def _probability(value: Any) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number) or number <= 0:
            return None
        if number > 1.0:
            number = 1.0 / number
        return number if 0.0 < number <= 1.0 else None

    @staticmethod
    def _positive_float(value: Any) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and number > 0 else None

    @staticmethod
    def _required_id(value: Any, label: str) -> str:
        clean = str(value or "").strip()
        if not _NUMERIC_ID_RE.fullmatch(clean):
            raise MarketConfigurationError(f"Prophet Exchange {label} id must be a positive numeric identifier.")
        return clean

    @classmethod
    def _contract_id(cls, event_id: str, market_id: str, outcome_id: str, line_id: str) -> str:
        return f"{event_id}:{market_id}:{outcome_id}:{line_id}"

    @classmethod
    def _split_contract_id(cls, contract_id: str) -> Tuple[str, str, str, str]:
        parts = [part.strip() for part in str(contract_id or "").split(":")]
        if len(parts) != 4:
            raise MarketConfigurationError(
                "Prophet Exchange contract id must be EVENT_ID:MARKET_ID:OUTCOME_ID:LINE_ID."
            )
        event, market, outcome, line = parts
        cls._required_id(event, "event")
        cls._required_id(market, "market")
        cls._required_id(outcome, "outcome")
        if not _SEGMENT_RE.fullmatch(line):
            raise MarketConfigurationError("Prophet Exchange contract line id is invalid.")
        return event, market, outcome, line

    @staticmethod
    def _url(base: str, path: str) -> str:
        return f"{base.rstrip('/')}/{str(path or '').strip('/')}"
