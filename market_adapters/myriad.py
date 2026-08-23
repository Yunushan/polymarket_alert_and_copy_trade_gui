from __future__ import annotations

import math
import hashlib
import hmac
import json
import time
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .base import MarketAdapter
from .catalog import get_market_metadata
from .errors import MarketConfigurationError, MarketHTTPError
from .identity import require_activity_identity
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


DEFAULT_MYRIAD_BASE_URL = "https://api-v2.myriadprotocol.com"
MYRIAD_ACCOUNT_OPERATIONS = ("account_activity",)
MYRIAD_ORDER_MANAGEMENT_OPERATIONS = (
    "cancel_order",
    "batch_cancel_orders",
    "cancel_all_orders",
    "batch_modify_orders",
)
MYRIAD_ORDER_MANAGEMENT_CONFIRMATION = "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
MYRIAD_GLOBAL_CANCEL_CONFIRMATION = "CANCEL ALL MYRIAD ORDERS"
MYRIAD_ORDER_MANAGEMENT_MAX_BATCH = 200
MYRIAD_REFERENCES = (
    "https://docs.myriad.markets/builders/myriad-api-reference",
    "https://docs.myriad.markets/builders/myriad-order-book",
    "https://docs.myriad.markets/builders/javascript-sdk",
)


class MyriadAdapter(MarketAdapter):
    """Myriad Markets adapter using the documented public protocol API."""

    metadata = get_market_metadata("myriad_markets")
    account_recovery_operations = MYRIAD_ACCOUNT_OPERATIONS
    order_management_operations = MYRIAD_ORDER_MANAGEMENT_OPERATIONS

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        credentials = []
        for credential in (
            self.resolve_credential("myriad_api_key", ("MYRIAD_API_KEY",), label="MYRIAD_API_KEY"),
            self.resolve_credential("myriad_api_secret", ("MYRIAD_API_SECRET",), label="MYRIAD_API_SECRET"),
            self.resolve_credential("myriad_access_token", ("MYRIAD_ACCESS_TOKEN",), label="MYRIAD_ACCESS_TOKEN"),
        ):
            if credential:
                credentials.append({"name": credential.name, "source": credential.source})
        health.update(
            {
                "api_base_url": self.api_base_url,
                "references": list(MYRIAD_REFERENCES),
                "credential_sources": credentials,
                "live_trading_supported": True,
                "activity_feed_supported": bool(self.capabilities.copy_trading),
                "copy_trading_supported": bool(self.capabilities.copy_trading),
                "live_trading_enabled": self.config_bool("live_trading_enabled", False),
                "account_recovery_operations": list(self.account_recovery_operations),
                "public_account_endpoints": ["GET /users/{address}/events"],
                "order_management_operations": list(self.order_management_operations),
                "order_management_enabled": self.config_bool("myriad_order_management_enabled", False),
                "authenticated_order_management_endpoints": [
                    "/orders/{orderHash}",
                    "/orders/cancel-batch",
                    "/orders/cancel-all",
                    "/orders/batch-modify",
                ],
            }
        )
        return health

    @property
    def api_base_url(self) -> str:
        configured = self.config.get("myriad_api_base_url") or self.config.get("api_base_url")
        return str(configured or DEFAULT_MYRIAD_BASE_URL).rstrip("/")

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = max(1, min(int(limit or 50), 100))
        params: Dict[str, Any] = {"page": 1, "limit": desired}
        if query:
            params["keyword"] = str(query).strip()
        payload = self._get("/questions", params=params)
        questions = self._list_from_payload(payload, "data", "questions", "results")
        return [self._event_from_question(question) for question in questions[:desired]]

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        question = self._get_question(event_id)
        return self._contracts_from_question(question)

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        market_id, outcome_id = self._split_contract_id(contract_id)
        market = self._get_market(market_id)
        outcome = self._find_outcome(market, outcome_id)
        if not outcome:
            raise MarketConfigurationError(f"Myriad outcome {outcome_id!r} was not found in market {market_id!r}.")
        price = self._safe_probability(outcome.get("price"))
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=self._contract_id(market_id, outcome_id),
            last=price,
            bid=None,
            ask=None,
            midpoint=price,
            source="myriad_market_outcome",
            raw={"market": dict(market), "outcome": dict(outcome)},
        )

    def get_orderbook(self, contract_id: str) -> OrderBookSnapshot:
        self.ensure_capability("orderbook_reading")
        market_id, outcome_id = self._split_contract_id(contract_id)
        params: Dict[str, Any] = {"outcome": self._orderbook_outcome_param(outcome_id)}
        network_id = self.config.get("myriad_network_id")
        if network_id not in (None, ""):
            params["network_id"] = network_id
        payload = self._get(f"/markets/{market_id}/orderbook", params=params)
        data = payload.get("data") if isinstance(payload, Mapping) else payload
        orderbook = data if isinstance(data, Mapping) else payload if isinstance(payload, Mapping) else {}
        return OrderBookSnapshot(
            market_id=self.market_id,
            contract_id=self._contract_id(market_id, outcome_id),
            bids=self._book_levels(orderbook.get("bids"), descending=True),
            asks=self._book_levels(orderbook.get("asks")),
            raw=orderbook,
        )

    def list_trades(
        self,
        contract_id: str,
        *,
        limit: int = 50,
        before: Optional[float] = None,
        after: Optional[float] = None,
    ) -> List[MarketTrade]:
        """Return normalized public Myriad order-book trades for one outcome.

        Myriad documents ``GET /markets/:id/trades`` for recent order-book
        matches.  The endpoint accepts only pagination/outcome filters, so the
        shared timestamp bounds are applied locally after parsing the newest
        page.  Amounts are returned in the quote token's smallest unit and are
        normalized to the common human-sized trade model.
        """

        market_id, outcome_id = self._split_contract_id(contract_id)
        desired = self._trade_limit(limit)
        before_ts = self._history_timestamp_bound(before, "before") if before is not None else None
        after_ts = self._history_timestamp_bound(after, "after") if after is not None else None
        if before_ts is not None and after_ts is not None and before_ts <= after_ts:
            raise MarketConfigurationError("Myriad trade history requires before greater than after.")

        params: Dict[str, Any] = {
            "page": 1,
            "limit": desired,
            "outcome": self._orderbook_outcome_param(outcome_id),
        }
        network_id = self.config.get("myriad_network_id")
        if network_id not in (None, ""):
            params["network_id"] = network_id
        payload = self._get(f"/markets/{market_id}/trades", params=params)
        rows = self._list_from_payload(payload, "trades", "data", "results", "items")
        canonical = self._contract_id(market_id, outcome_id)
        trades: List[MarketTrade] = []
        for row in rows:
            raw_outcome = row.get("outcome")
            if raw_outcome not in (None, "") and str(raw_outcome).strip() != str(outcome_id):
                continue
            price = self._safe_probability(row.get("price"))
            size = self._scaled_decimal(row.get("amount") if row.get("amount") is not None else row.get("size"))
            side = str(row.get("side") or "").strip().upper()
            if price is None or size is None or size <= 0 or side not in {"BUY", "SELL", "SPLIT", "MERGE"}:
                continue
            timestamp = self._timestamp_seconds(row.get("timestamp") or row.get("ts"))
            if timestamp <= 0:
                continue
            if before_ts is not None and timestamp > before_ts:
                continue
            if after_ts is not None and timestamp < after_ts:
                continue
            trade_id = str(row.get("txHash") or row.get("tradeId") or row.get("id") or "").strip()
            if not trade_id:
                continue
            trades.append(
                MarketTrade(
                    market_id=self.market_id,
                    contract_id=canonical,
                    trade_id=trade_id,
                    side=side,
                    price=price,
                    size=size,
                    timestamp=float(timestamp),
                    raw=dict(row),
                )
            )
        return trades

    def list_candles(
        self,
        contract_id: str,
        *,
        resolution: str = "1h",
        from_timestamp: Optional[float] = None,
        to_timestamp: Optional[float] = None,
    ) -> List[MarketCandle]:
        """Return normalized candles from Myriad's official price charts.

        ``GET /markets/:id`` embeds the documented ``price_charts`` buckets
        (24h, 7d, 30d, and all) on each outcome.  The generic adapter
        resolution names are mapped to the nearest official bucket; no local
        resampling is performed.  Myriad's chart payload can be either
        OHLCV arrays or keyed objects, so both documented/SDK-compatible
        shapes are parsed while preserving the original row in ``raw``.
        """

        self.ensure_capability("candle_history")
        timeframe = self._chart_timeframe(resolution)
        start = self._history_timestamp_bound(from_timestamp, "from") if from_timestamp is not None else None
        end = self._history_timestamp_bound(to_timestamp, "to") if to_timestamp is not None else None
        if start is not None and end is not None and start > end:
            raise MarketConfigurationError("Myriad price-chart range requires from_timestamp <= to_timestamp.")

        market_id, outcome_id = self._split_contract_id(contract_id)
        market = self._get_market(market_id)
        outcome = self._find_outcome(market, outcome_id)
        if outcome is None:
            raise MarketConfigurationError(f"Myriad outcome {outcome_id!r} was not found in market {market_id!r}.")
        charts = outcome.get("price_charts")
        if charts is None:
            charts = outcome.get("priceCharts")
        rows = self._chart_rows(charts, timeframe)
        if rows is None:
            raise MarketConfigurationError(
                f"Myriad outcome {outcome_id!r} did not include the official {timeframe} price chart."
            )

        canonical = self._contract_id(market_id, outcome_id)
        candles: List[MarketCandle] = []
        for row in rows:
            parsed = self._chart_candle(row)
            if parsed is None:
                continue
            timestamp, values, volume = parsed
            if start is not None and timestamp < start:
                continue
            if end is not None and timestamp > end:
                continue
            candles.append(
                MarketCandle(
                    market_id=self.market_id,
                    contract_id=canonical,
                    timestamp=float(timestamp),
                    open=values[0],
                    high=values[1],
                    low=values[2],
                    close=values[3],
                    volume=volume,
                    raw={
                        "source": "myriad_price_charts",
                        "timeframe": timeframe,
                        "resolution_requested": str(resolution or "1h"),
                        "point": dict(row) if isinstance(row, Mapping) else list(row),
                    },
                )
            )
        candles.sort(key=lambda candle: candle.timestamp)
        return candles

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        self._validate_order(order)
        market_id, outcome_id = self._split_contract_id(order.contract_id)
        quote_payload = self._quote_payload(order, market_id=market_id, outcome_id=outcome_id)
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=self._contract_id(market_id, outcome_id),
            accepted=True,
            message=(
                f"DRY RUN: would request a Myriad {order.side.upper()} quote for "
                f"{order.size:.4f} {'shares' if order.side.upper() == 'SELL' else 'collateral'}"
            ),
            raw={"request": quote_payload, "endpoint": "/markets/quote"},
        )

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        self.ensure_capability("live_trading")
        self._validate_order(order)
        preflight = self.preflight_live_order(order)
        payload = self._live_order_payload(order)
        response = self._post("/orders", payload)
        return {
            "market_id": self.market_id,
            "contract_id": order.contract_id,
            "live": True,
            "preflight": preflight,
            "request": payload,
            "response": response,
        }

    def manage_orders(self, operation: str, **kwargs: Any) -> Dict[str, Any]:
        """Run a guarded Myriad Order Book cancellation or replacement.

        The Myriad API requires the original EIP-712 order and signature for
        cancellation, plus an authenticated account tied to the trader wallet.
        This boundary accepts only the documented fixed endpoints and validates
        signed entries locally before the request is sent.
        """

        normalized = str(operation or "").strip().lower()
        if normalized not in self.order_management_operations:
            supported = ", ".join(self.order_management_operations)
            raise MarketConfigurationError(
                f"Myriad order-management operation must be one of: {supported}."
            )
        self.ensure_capability("live_trading")
        if not self.config_bool("myriad_order_management_enabled", False):
            raise MarketConfigurationError(
                "Myriad order management is disabled by adapter config. "
                "Set myriad_order_management_enabled=true only after reviewing signed-order mutation risk."
            )
        self.ensure_live_trading_enabled("Myriad order management")
        if str(kwargs.get("confirm_order_management") or "").strip() != MYRIAD_ORDER_MANAGEMENT_CONFIRMATION:
            raise MarketConfigurationError(
                "Myriad order management requires exact confirmation text "
                f"{MYRIAD_ORDER_MANAGEMENT_CONFIRMATION}."
            )
        if bool(kwargs.get("async_request")):
            raise MarketConfigurationError("Myriad order-management requests are synchronous.")

        request_body: Dict[str, Any]
        request_path: str
        request_method = "POST"
        if normalized == "cancel_order":
            order_hash = self._order_path_id(kwargs.get("order_hash", kwargs.get("order_id")))
            request_body = self._signed_cancel_entry(kwargs)
            request_path = f"/orders/{order_hash}"
            request_method = "DELETE"
        elif normalized == "batch_cancel_orders":
            entries = self._signed_entry_batch(kwargs.get("orders", kwargs.get("instructions")), cancel=True)
            request_body = {
                "orders": entries,
                **self._network_payload(kwargs),
                "allow_partial": self._allow_partial(kwargs.get("allow_partial", True)),
            }
            request_path = "/orders/cancel-batch"
        elif normalized == "cancel_all_orders":
            if str(kwargs.get("confirm_global_cancel") or "").strip() != MYRIAD_GLOBAL_CANCEL_CONFIRMATION:
                raise MarketConfigurationError(
                    "Myriad global cancellation requires exact confirmation text "
                    f"{MYRIAD_GLOBAL_CANCEL_CONFIRMATION}."
                )
            request_body = self._cancel_all_payload(kwargs)
            request_path = "/orders/cancel-all"
        else:
            modify = kwargs.get("modify")
            if modify is None:
                modify = kwargs.get("instructions")
            if not isinstance(modify, Mapping):
                modify = {
                    "cancel": kwargs.get("cancel"),
                    "place": kwargs.get("place"),
                }
            cancel_entries = self._signed_entry_batch(modify.get("cancel"), cancel=True, allow_empty=True)
            place_entries = self._signed_entry_batch(modify.get("place"), cancel=False, allow_empty=True)
            if not cancel_entries and not place_entries:
                raise MarketConfigurationError("Myriad batch_modify_orders requires cancel or place entries.")
            request_body = {
                "cancel": cancel_entries,
                "place": place_entries,
                **self._network_payload(kwargs),
                "allow_partial": self._allow_partial(kwargs.get("allow_partial", True)),
            }
            request_path = "/orders/batch-modify"

        response = self._request_json(request_method, request_path, body=request_body, auth=True)
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
                "references": list(MYRIAD_REFERENCES),
            },
            "request": {
                "method": request_method,
                "path": request_path,
                "body": request_body,
            },
            "response": response,
        }

    def copy_trade_from_activity(self, activity: Mapping[str, Any]) -> PaperOrderResult:
        self.ensure_capability("copy_trading")
        contract_id = str(activity.get("asset") or activity.get("contract_id") or "").strip()
        if not contract_id:
            raise MarketConfigurationError("Myriad activity has no market/outcome contract id.")
        side = str(activity.get("side") or "").strip().upper()
        if side not in {"BUY", "SELL"}:
            raise MarketConfigurationError("Myriad activity side must be BUY or SELL.")
        size = self._required_positive_number(activity.get("size"), "Myriad activity size")
        raw_price = activity.get("price")
        limit_price = None if raw_price in (None, "") else self._safe_probability(raw_price)
        if raw_price not in (None, "") and limit_price is None:
            raise MarketConfigurationError("Myriad activity reference price must be between 0 and 1.")
        metadata: Dict[str, Any] = {"activity": dict(activity), "source": "myriad_user_event_feed"}
        if side == "SELL":
            metadata["shares"] = activity.get("shares") or size
        return self.place_paper_order(
            PaperOrderRequest(
                market_id=self.market_id,
                contract_id=contract_id,
                side=side,
                size=size,
                limit_price=limit_price,
                metadata=metadata,
            )
        )

    def list_activity(self, wallet_address: str, *, limit: int = 25) -> List[Dict[str, Any]]:
        """Return normalized public buy/sell events for an EVM wallet.

        Myriad documents ``GET /users/:address/events`` as a public read for
        any wallet.  Only trade actions are admitted to the copy workflow;
        liquidity, claim, split, merge, and other account events are ignored.
        The normalized BUY size is collateral value and SELL size is shares,
        matching the Myriad quote contract.
        """

        self.ensure_capability("copy_trading")
        wallet = require_activity_identity(self.market_id, wallet_address)
        desired = self._bounded_activity_limit(limit)
        payload = self._fetch_activity_payload(wallet, desired)
        return self._normalize_activity_payload(wallet, payload, desired)

    def account_recovery(self, operation: str, **kwargs: Any) -> Dict[str, Any]:
        """Read Myriad's documented public wallet activity feed.

        This is a public, wallet-scoped activity read rather than an
        authenticated account endpoint.  The shared account surface still
        uses an explicit operation allow-list so callers cannot turn a
        wallet value into an arbitrary upstream path.
        """

        normalized = str(operation or "").strip().lower()
        if normalized not in self.account_recovery_operations:
            raise MarketConfigurationError(
                "Myriad account operation must be one of: "
                + ", ".join(self.account_recovery_operations)
                + "."
            )
        self.ensure_capability("copy_trading")
        wallet = require_activity_identity(
            self.market_id,
            kwargs.get("wallet") or kwargs.get("address"),
        )
        desired = self._bounded_activity_limit(kwargs.get("limit", 25), strict=True)
        payload = self._fetch_activity_payload(wallet, desired)
        return {
            "source": "myriad_user_event_feed",
            "endpoint": "/users/{address}/events",
            "wallet": wallet,
            "limit": desired,
            "activities": self._normalize_activity_payload(wallet, payload, desired),
            "raw": payload,
        }

    def _fetch_activity_payload(self, wallet: str, desired: int) -> Any:
        params: Dict[str, Any] = {
            "page": 1,
            "limit": desired,
            "trading_model": str(self.config.get("myriad_activity_trading_model") or "all"),
            "only_relevant": "true",
        }
        network_id = self.config.get("myriad_activity_network_id", self.config.get("myriad_network_id"))
        if network_id not in (None, ""):
            params["network_id"] = network_id
        market_id = str(self.config.get("myriad_activity_market_id") or "").strip()
        if market_id:
            params["market_id"] = market_id
        market_slug = str(self.config.get("myriad_activity_market_slug") or "").strip()
        if market_slug:
            params["market_slug"] = market_slug
        return self._get(f"/users/{wallet}/events", params=params)

    def _normalize_activity_payload(
        self,
        wallet: str,
        payload: Any,
        desired: int,
    ) -> List[Dict[str, Any]]:
        activities: List[Dict[str, Any]] = []
        network_id = self.config.get("myriad_activity_network_id", self.config.get("myriad_network_id"))
        configured_network = str(network_id).strip() if network_id not in (None, "") else ""
        for event in self._activity_rows(payload):
            action = str(event.get("action") or "").strip().lower()
            if action not in {"buy", "sell"}:
                continue
            event_network = event.get("networkId") or event.get("network_id")
            if configured_network and event_network not in (None, "") and str(event_network).strip() != configured_network:
                continue
            try:
                activities.append(self._activity_from_event(wallet, event))
            except MarketConfigurationError:
                # A malformed public event must never become an order intent.
                continue
        return activities[:desired]

    @staticmethod
    def _bounded_activity_limit(value: Any, *, strict: bool = False) -> int:
        if value in (None, ""):
            return 25
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Myriad account activity limit must be an integer.") from exc
        if strict and (parsed < 1 or parsed > 100):
            raise MarketConfigurationError("Myriad account activity limit must be between 1 and 100.")
        return max(1, min(parsed, 100))

    def _activity_from_event(self, wallet: str, event: Mapping[str, Any]) -> Dict[str, Any]:
        market_id = str(event.get("marketId") or event.get("market_id") or "").strip()
        outcome_id = str(event.get("outcomeId") or event.get("outcome_id") or "").strip()
        if not market_id or not outcome_id:
            raise MarketConfigurationError("Myriad user event omitted marketId or outcomeId.")
        action = str(event.get("action") or "").strip().lower()
        side = {"buy": "BUY", "sell": "SELL"}.get(action)
        if side is None:
            raise MarketConfigurationError("Myriad user event has an unsupported action.")
        shares = self._finite_number(event.get("shares"))
        value = self._finite_number(event.get("value"))
        size = value if side == "BUY" else shares
        if size is None or size <= 0:
            raise MarketConfigurationError("Myriad user event did not contain a positive trade size.")
        price = None
        if value is not None and shares is not None and shares > 0:
            price = self._safe_probability(value / shares)
        tx_hash = str(event.get("txId") or event.get("txHash") or event.get("transactionHash") or "").strip()
        timestamp = self._timestamp_seconds(event.get("timestamp") or event.get("createdAt"))
        stable_id = tx_hash or f"{market_id}:{outcome_id}:{timestamp}:{side}:{size}"
        contract_id = self._contract_id(market_id, outcome_id)
        return {
            "type": "TRADE",
            "proxyWallet": wallet,
            "wallet": wallet,
            "asset": contract_id,
            "contract_id": contract_id,
            "marketId": market_id,
            "networkId": event.get("networkId") or event.get("network_id"),
            "side": side,
            "size": size,
            "value": value,
            "shares": shares,
            "price": price,
            "timestamp": timestamp,
            "transactionHash": tx_hash or f"myriad-event:{stable_id}",
            "slug": str(event.get("marketSlug") or market_id),
            "outcome": str(event.get("outcomeTitle") or outcome_id),
            "pseudonym": str(event.get("marketTitle") or ""),
            "raw": dict(event),
        }

    def _get_question(self, question_id: str) -> Mapping[str, Any]:
        clean = str(question_id or "").strip()
        if not clean:
            raise MarketConfigurationError("Myriad question id cannot be empty.")
        payload = self._get(f"/questions/{clean}")
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if isinstance(data, Mapping):
            return data
        if isinstance(payload, Mapping):
            return payload
        raise MarketConfigurationError(f"Myriad question {clean!r} was not found.")

    def _get_market(self, market_id: str) -> Mapping[str, Any]:
        clean = str(market_id or "").strip()
        if not clean:
            raise MarketConfigurationError("Myriad market id cannot be empty.")
        params: Dict[str, Any] = {}
        network_id = self.config.get("myriad_network_id")
        if network_id not in (None, ""):
            params["network_id"] = network_id
        payload = self._get(f"/markets/{clean}", params=params or None)
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if isinstance(data, Mapping):
            return data
        if isinstance(payload, Mapping):
            return payload
        raise MarketConfigurationError(f"Myriad market {clean!r} was not found.")

    def _get(self, path: str, *, params: Optional[Mapping[str, Any]] = None) -> Any:
        return self.runtime.get_json(self._url(path), params=params, headers=self._headers())

    def _post(self, path: str, payload: Mapping[str, Any]) -> Any:
        return self._request_json("POST", path, body=payload, auth=True)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        body: Optional[Mapping[str, Any]] = None,
        auth: bool = False,
    ) -> Any:
        clean_path = "/" + str(path or "").strip("/")
        query = ""
        if params:
            from urllib.parse import urlencode

            query = "?" + urlencode(list(params.items()), doseq=True)
        body_payload = dict(body) if body is not None else None
        raw_body = (
            # ``requests`` serializes its ``json=`` argument with the default
            # JSON separators; sign those exact bytes to satisfy Myriad's
            # HMAC contract rather than a compact representation.
            json.dumps(body_payload, allow_nan=False)
            if body_payload is not None
            else ""
        )
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            **self._headers(
                required=auth,
                method=method,
                path=f"{clean_path}{query}",
                body=raw_body,
            ),
        }
        self.runtime.rate_limiter.wait()
        request_kwargs: Dict[str, Any] = {
            "json": body_payload,
            "headers": headers,
            "timeout": self.runtime.timeout_seconds,
        }
        if params is not None:
            request_kwargs["params"] = dict(params)
        try:
            response = self.runtime.session.request(
                method.upper(),
                self._url(clean_path),
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

    def _url(self, path: str) -> str:
        clean_path = "/" + str(path or "").strip("/")
        return f"{self.api_base_url}{clean_path}"

    def _headers(
        self,
        *,
        required: bool = False,
        method: str = "GET",
        path: str = "/",
        body: str = "",
    ) -> Dict[str, str]:
        api_key = self.resolve_credential(
            "myriad_api_key",
            ("MYRIAD_API_KEY",),
            required=False,
            label="MYRIAD_API_KEY",
        )
        api_secret = self.resolve_credential(
            "myriad_api_secret",
            ("MYRIAD_API_SECRET",),
            required=False,
            label="MYRIAD_API_SECRET",
        )
        access_token = self.resolve_credential(
            "myriad_access_token",
            ("MYRIAD_ACCESS_TOKEN",),
            required=False,
            label="MYRIAD_ACCESS_TOKEN",
        )
        if required and access_token is None and (api_key is None or api_secret is None):
            raise MarketConfigurationError(
                "Myriad authenticated requests require MYRIAD_ACCESS_TOKEN or both "
                "MYRIAD_API_KEY and MYRIAD_API_SECRET. Bare API keys are no longer accepted."
            )
        headers: Dict[str, str] = {}
        if api_key:
            headers["x-api-key"] = api_key.value
        if access_token:
            headers["Authorization"] = f"Bearer {access_token.value}"
        if api_key and api_secret:
            timestamp = str(int(time.time()))
            message = f"{timestamp}.{str(method or 'GET').upper()}.{path}.{body}"
            headers["x-api-timestamp"] = timestamp
            headers["x-api-signature"] = hmac.new(
                api_secret.value.encode("utf-8"),
                message.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
        return headers

    @classmethod
    def _order_path_id(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text or len(text) > 128 or any(char in text for char in "/\\?#%"):
            raise MarketConfigurationError("Myriad order hash/client id contains unsafe path characters.")
        if text.startswith("0x"):
            if len(text) != 66 or any(char not in "0123456789abcdefABCDEF" for char in text[2:]):
                raise MarketConfigurationError("Myriad order hash must be 0x followed by 64 hexadecimal characters.")
            return text
        if not all(char.isalnum() or char in "._-" for char in text):
            raise MarketConfigurationError("Myriad client order id contains unsupported characters.")
        return text

    @classmethod
    def _signed_cancel_entry(cls, kwargs: Mapping[str, Any]) -> Dict[str, Any]:
        candidate = kwargs.get("entry", kwargs.get("order_entry"))
        if candidate is None:
            candidate = kwargs.get("instructions")
        if isinstance(candidate, list):
            if len(candidate) != 1:
                raise MarketConfigurationError("Myriad cancel_order requires one signed order entry.")
            candidate = candidate[0]
        if isinstance(candidate, Mapping) and isinstance(candidate.get("order"), Mapping):
            entry = dict(candidate)
        elif isinstance(kwargs.get("order"), Mapping):
            entry = {
                "order": kwargs.get("order"),
                "signature": kwargs.get("signature"),
                "signatureType": kwargs.get("signature_type", kwargs.get("signatureType")),
            }
        else:
            raise MarketConfigurationError(
                "Myriad cancel_order requires a signed entry with order and signature."
            )
        return cls._signed_entry(entry, cancel=True, network_id=kwargs.get("network_id"))

    @classmethod
    def _signed_entry_batch(
        cls,
        value: Any,
        *,
        cancel: bool,
        allow_empty: bool = False,
    ) -> List[Dict[str, Any]]:
        if value in (None, "") and allow_empty:
            return []
        if isinstance(value, Mapping) and isinstance(value.get("orders"), list):
            value = value.get("orders")
        if not isinstance(value, (list, tuple)):
            raise MarketConfigurationError("Myriad signed order entries must be a JSON array.")
        if not value and allow_empty:
            return []
        if not value or len(value) > MYRIAD_ORDER_MANAGEMENT_MAX_BATCH:
            raise MarketConfigurationError(
                f"Myriad signed order batches must contain between 1 and {MYRIAD_ORDER_MANAGEMENT_MAX_BATCH} entries."
            )
        entries = [cls._signed_entry(item, cancel=cancel) for item in value]
        signatures = [entry["signature"] for entry in entries]
        if len(set(signatures)) != len(signatures):
            raise MarketConfigurationError("Myriad signed order batches must not duplicate signatures.")
        return entries

    @classmethod
    def _signed_entry(
        cls,
        value: Any,
        *,
        cancel: bool,
        network_id: Any = None,
    ) -> Dict[str, Any]:
        if not isinstance(value, Mapping) or not isinstance(value.get("order"), Mapping):
            raise MarketConfigurationError("Myriad signed order entry requires an order object.")
        signature = str(value.get("signature") or "").strip()
        if not signature.startswith("0x") or len(signature) < 4 or any(
            char not in "0123456789abcdefABCDEF" for char in signature[2:]
        ):
            raise MarketConfigurationError("Myriad order signature must be hexadecimal and 0x-prefixed.")
        order = cls._signed_order(value["order"])
        entry: Dict[str, Any] = {"order": order, "signature": signature}
        signature_type = value.get("signatureType", value.get("signature_type"))
        if signature_type not in (None, ""):
            if isinstance(signature_type, bool) or str(signature_type) not in {"0", "3"}:
                raise MarketConfigurationError("Myriad signatureType must be 0 (EOA) or 3 (SCW).")
            entry["signatureType"] = int(signature_type)
        if not cancel:
            if value.get("time_in_force", value.get("timeInForce")) not in (None, ""):
                tif = str(value.get("time_in_force", value.get("timeInForce"))).strip().upper()
                if tif not in {"GTC", "GTD", "FOK", "FAK", "PO"}:
                    raise MarketConfigurationError("Myriad time_in_force must be GTC, GTD, FOK, FAK, or PO.")
                entry["time_in_force"] = tif
            if value.get("accept_by", value.get("acceptBy")) not in (None, ""):
                entry["accept_by"] = cls._positive_int(
                    value.get("accept_by", value.get("acceptBy")),
                    "accept_by",
                    allow_zero=True,
                )
        return entry

    @classmethod
    def _signed_order(cls, value: Mapping[str, Any]) -> Dict[str, Any]:
        aliases = {
            "marketId": "market_id",
            "outcomeId": "outcome_id",
            "minFillAmount": "min_fill_amount",
        }
        required = (
            "trader",
            "marketId",
            "outcomeId",
            "side",
            "amount",
            "price",
            "minFillAmount",
            "nonce",
            "expiration",
        )
        result: Dict[str, Any] = {}
        for field in required:
            raw = value.get(field)
            if raw in (None, "") and field in aliases:
                raw = value.get(aliases[field])
            if raw in (None, "") or isinstance(raw, (Mapping, list, tuple)):
                raise MarketConfigurationError(f"Myriad signed order requires scalar {field}.")
            result[field] = raw
        trader = str(result["trader"]).strip()
        if len(trader) != 42 or not trader.startswith("0x") or any(
            char not in "0123456789abcdefABCDEF" for char in trader[2:]
        ):
            raise MarketConfigurationError("Myriad signed order trader must be a 20-byte 0x-prefixed address.")
        for field in ("outcomeId", "side"):
            try:
                parsed = int(result[field])
            except (TypeError, ValueError) as exc:
                raise MarketConfigurationError(f"Myriad signed order {field} must be an integer.") from exc
            if field == "outcomeId" and parsed < 0:
                raise MarketConfigurationError("Myriad signed order outcomeId must be non-negative.")
            if field == "side" and parsed not in {0, 1}:
                raise MarketConfigurationError("Myriad signed order side must be 0 (buy) or 1 (sell).")
            result[field] = parsed
        for field in ("amount", "price", "minFillAmount", "nonce", "expiration"):
            cls._positive_int(result[field], field, allow_zero=field in {"minFillAmount", "expiration"})
        price = int(result["price"])
        if price < 1 or price > 1_000_000_000_000_000_000:
            raise MarketConfigurationError("Myriad signed order price must be an integer between 1 and 1e18.")
        return result

    @classmethod
    def _positive_int(cls, value: Any, label: str, *, allow_zero: bool = False) -> int:
        if isinstance(value, bool) or not str(value).strip().isdigit():
            raise MarketConfigurationError(f"Myriad {label} must be a non-negative integer.")
        parsed = int(str(value).strip())
        if parsed < 0 or (parsed == 0 and not allow_zero):
            raise MarketConfigurationError(
                f"Myriad {label} must be {'a non-negative' if allow_zero else 'a positive'} integer."
            )
        return parsed

    @classmethod
    def _network_payload(cls, kwargs: Mapping[str, Any]) -> Dict[str, Any]:
        value = kwargs.get("network_id")
        if value in (None, ""):
            return {}
        return {"network_id": cls._positive_int(value, "network_id")}

    @staticmethod
    def _allow_partial(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if value in (None, ""):
            return True
        normalized = str(value).strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
        raise MarketConfigurationError("Myriad allow_partial must be a boolean.")

    @classmethod
    def _cancel_all_payload(cls, kwargs: Mapping[str, Any]) -> Dict[str, Any]:
        trader = str(kwargs.get("trader") or "").strip()
        if len(trader) != 42 or not trader.startswith("0x") or any(
            char not in "0123456789abcdefABCDEF" for char in trader[2:]
        ):
            raise MarketConfigurationError("Myriad cancel-all trader must be a 20-byte 0x-prefixed address.")
        timestamp = cls._positive_int(kwargs.get("timestamp"), "timestamp")
        signature = str(kwargs.get("signature") or "").strip()
        if not signature.startswith("0x") or len(signature) < 4 or any(
            char not in "0123456789abcdefABCDEF" for char in signature[2:]
        ):
            raise MarketConfigurationError("Myriad cancel-all signature must be hexadecimal and 0x-prefixed.")
        payload: Dict[str, Any] = {
            "trader": trader,
            "timestamp": str(timestamp),
            "signature": signature,
        }
        market_id = kwargs.get("market_id")
        if market_id not in (None, ""):
            payload["market_id"] = cls._positive_int(market_id, "market_id", allow_zero=True)
        signature_type = kwargs.get("signature_type", kwargs.get("signatureType"))
        if signature_type not in (None, ""):
            if isinstance(signature_type, bool) or str(signature_type) not in {"0", "3"}:
                raise MarketConfigurationError("Myriad signatureType must be 0 (EOA) or 3 (SCW).")
            payload["signatureType"] = int(signature_type)
        payload.update(cls._network_payload(kwargs))
        return payload

    @staticmethod
    def _trade_limit(value: Any) -> int:
        try:
            desired = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Myriad trade limit must be an integer between 1 and 200.") from exc
        if desired < 1 or desired > 200:
            raise MarketConfigurationError("Myriad trade limit must be between 1 and 200.")
        return desired

    @staticmethod
    def _chart_timeframe(value: Any) -> str:
        requested = str(value or "1h").strip().lower()
        # Myriad publishes only these four buckets.  The aliases keep the
        # shared API's common resolutions usable without pretending to
        # resample the upstream series.
        aliases = {
            "5m": "24h",
            "15m": "24h",
            "30m": "24h",
            "1h": "24h",
            "24h": "24h",
            "1d": "7d",
            "day": "7d",
            "7d": "7d",
            "4h": "30d",
            "1w": "30d",
            "30d": "30d",
            "max": "all",
            "all": "all",
        }
        try:
            return aliases[requested]
        except KeyError as exc:
            raise MarketConfigurationError(
                "Myriad price history accepts official buckets 24h, 7d, 30d, or all "
                "(common aliases 5m/15m/30m/1h/1d/4h/1w/max are mapped without resampling)."
            ) from exc

    @staticmethod
    def _chart_rows(charts: Any, timeframe: str) -> Optional[List[Any]]:
        if isinstance(charts, list):
            return charts
        if not isinstance(charts, Mapping):
            return None
        for key in (timeframe, timeframe.replace("h", "H"), timeframe.replace("d", "D")):
            value = charts.get(key)
            if isinstance(value, list):
                return value
        for key in ("data", "history", "candles", "points"):
            nested = charts.get(key)
            if isinstance(nested, Mapping):
                rows = MyriadAdapter._chart_rows(nested, timeframe)
                if rows is not None:
                    return rows
            elif isinstance(nested, list):
                return nested
        return None

    @classmethod
    def _chart_candle(cls, row: Any) -> Optional[Tuple[int, Tuple[float, float, float, float], Optional[float]]]:
        timestamp: Any = None
        open_value = high_value = low_value = close_value = price_value = None
        volume_value: Any = None
        if isinstance(row, Mapping):
            timestamp = row.get("timestamp", row.get("time", row.get("ts", row.get("t"))))
            open_value = row.get("open", row.get("o"))
            high_value = row.get("high", row.get("h"))
            low_value = row.get("low", row.get("l"))
            close_value = row.get("close", row.get("c"))
            price_value = row.get("price", row.get("p"))
            volume_value = row.get("volume", row.get("v"))
            nested_ohlc = row.get("ohlc")
            if isinstance(nested_ohlc, Mapping):
                open_value = open_value if open_value is not None else nested_ohlc.get("open", nested_ohlc.get("o"))
                high_value = high_value if high_value is not None else nested_ohlc.get("high", nested_ohlc.get("h"))
                low_value = low_value if low_value is not None else nested_ohlc.get("low", nested_ohlc.get("l"))
                close_value = close_value if close_value is not None else nested_ohlc.get("close", nested_ohlc.get("c"))
        elif isinstance(row, (list, tuple)):
            if len(row) >= 5:
                timestamp, open_value, high_value, low_value, close_value = row[:5]
                volume_value = row[5] if len(row) >= 6 else None
            elif len(row) >= 2:
                timestamp, price_value = row[:2]
            else:
                return None
        else:
            return None

        parsed_timestamp = cls._timestamp_seconds(timestamp)
        if parsed_timestamp <= 0:
            return None
        if price_value is not None and all(value is None for value in (open_value, high_value, low_value, close_value)):
            open_value = high_value = low_value = close_value = price_value
        values = tuple(cls._safe_probability(value) for value in (open_value, high_value, low_value, close_value))
        if any(value is None for value in values):
            return None
        volume = cls._finite_number(volume_value)
        if volume is not None and volume < 0:
            volume = None
        return parsed_timestamp, (values[0], values[1], values[2], values[3]), volume  # type: ignore[index]

    @staticmethod
    def _history_timestamp_bound(value: Any, label: str) -> int:
        number = MyriadAdapter._finite_number(value)
        if number is None or number <= 0:
            raise MarketConfigurationError(f"Myriad {label} timestamp must be a positive Unix timestamp.")
        return int(number)

    def _event_from_question(self, question: Mapping[str, Any]) -> MarketEvent:
        question_id = self._question_id(question)
        return MarketEvent(
            market_id=self.market_id,
            event_id=question_id,
            title=str(question.get("title") or question.get("question") or question_id),
            url=self._question_url(question),
            status=self._status_from_question(question),
            raw=dict(question),
        )

    def _contracts_from_question(self, question: Mapping[str, Any]) -> List[MarketContract]:
        question_id = self._question_id(question)
        title = str(question.get("title") or question.get("question") or question_id)
        contracts: List[MarketContract] = []
        for market in self._markets_from_question(question):
            market_id = self._market_id(market)
            status = self._status_from_market(market)
            for outcome in self._outcomes_from_market(market):
                outcome_id = self._outcome_id(outcome)
                if not market_id or not outcome_id:
                    continue
                outcome_title = str(outcome.get("title") or outcome.get("name") or outcome_id)
                contracts.append(
                    MarketContract(
                        market_id=self.market_id,
                        contract_id=self._contract_id(market_id, outcome_id),
                        event_id=question_id,
                        title=f"{title} - {outcome_title}",
                        outcome=outcome_title,
                        url=self._market_url(market),
                        status=status,
                        raw={"question": dict(question), "market": dict(market), "outcome": dict(outcome)},
                    )
                )
        return contracts

    def _quote_payload(self, order: PaperOrderRequest, *, market_id: str, outcome_id: str) -> Dict[str, Any]:
        side = str(order.side or "").upper()
        payload: Dict[str, Any] = {
            "market_id": int(market_id) if str(market_id).isdigit() else market_id,
            "outcome_id": int(outcome_id) if str(outcome_id).isdigit() else outcome_id,
            "action": "buy" if side == "BUY" else "sell",
            "slippage": float(order.metadata.get("slippage", self.config.get("myriad_slippage", 0.005))),
        }
        if side == "BUY":
            payload["value"] = float(order.size)
        else:
            payload["shares"] = float(order.size)
        if "network_id" in order.metadata:
            payload["network_id"] = order.metadata["network_id"]
        return payload

    def _live_order_payload(self, order: PaperOrderRequest) -> Dict[str, Any]:
        existing = order.metadata.get("myriad_order_payload") or order.metadata.get("signed_order_payload")
        if isinstance(existing, Mapping):
            return dict(existing)
        signed_order = order.metadata.get("order") or order.metadata.get("signed_order")
        if not isinstance(signed_order, Mapping):
            raise MarketConfigurationError("Myriad live orders require order.metadata['order'] with a signed EIP-712 order.")
        signature = str(order.metadata.get("signature") or "").strip()
        if not signature:
            raise MarketConfigurationError("Myriad live orders require order.metadata['signature'].")
        payload: Dict[str, Any] = {
            "order": dict(signed_order),
            "signature": signature,
            "time_in_force": str(order.metadata.get("time_in_force") or self.config.get("myriad_time_in_force") or "GTC"),
        }
        network_id = order.metadata.get("network_id", self.config.get("myriad_network_id"))
        if network_id not in (None, ""):
            payload["network_id"] = network_id
        return payload

    def _validate_order(self, order: PaperOrderRequest) -> None:
        self.ensure_order_market(order)
        self._split_contract_id(order.contract_id)
        if str(order.side or "").upper() not in {"BUY", "SELL"}:
            raise MarketConfigurationError("Myriad paper order side must be BUY or SELL.")
        if not self._is_positive_number(order.size):
            raise MarketConfigurationError("Myriad paper order size must be positive.")

    @staticmethod
    def _question_id(question: Mapping[str, Any]) -> str:
        return str(question.get("id") or question.get("questionId") or "").strip()

    @staticmethod
    def _market_id(market: Mapping[str, Any]) -> str:
        return str(market.get("id") or market.get("marketId") or market.get("market_id") or "").strip()

    @staticmethod
    def _outcome_id(outcome: Mapping[str, Any]) -> str:
        return str(outcome.get("id") or outcome.get("outcomeId") or outcome.get("outcome_id") or "").strip()

    @staticmethod
    def _contract_id(market_id: str, outcome_id: str) -> str:
        return f"{market_id}:{outcome_id}"

    @staticmethod
    def _split_contract_id(contract_id: str) -> Tuple[str, str]:
        raw = str(contract_id or "").strip()
        parts = [part.strip() for part in raw.split(":")]
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise MarketConfigurationError("Myriad contract id must be MARKET_ID:OUTCOME_ID.")
        return parts[0], parts[1]

    @staticmethod
    def _markets_from_question(question: Mapping[str, Any]) -> List[Mapping[str, Any]]:
        markets = question.get("markets")
        return [market for market in markets if isinstance(market, Mapping)] if isinstance(markets, list) else []

    @staticmethod
    def _outcomes_from_market(market: Mapping[str, Any]) -> List[Mapping[str, Any]]:
        outcomes = market.get("outcomes")
        return [outcome for outcome in outcomes if isinstance(outcome, Mapping)] if isinstance(outcomes, list) else []

    @staticmethod
    def _find_outcome(market: Mapping[str, Any], outcome_id: str) -> Optional[Mapping[str, Any]]:
        for outcome in MyriadAdapter._outcomes_from_market(market):
            if MyriadAdapter._outcome_id(outcome) == str(outcome_id):
                return outcome
        return None

    @staticmethod
    def _book_levels(raw: Any, *, descending: bool = False) -> List[OrderBookLevel]:
        levels: List[OrderBookLevel] = []
        if not isinstance(raw, list):
            return levels
        for item in raw:
            price = size = None
            if isinstance(item, Mapping):
                price = item.get("price")
                size = item.get("remaining_amount") or item.get("size") or item.get("amount")
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                price, size = item[0], item[1]
            parsed_price = MyriadAdapter._scaled_decimal(price)
            parsed_size = MyriadAdapter._scaled_decimal(size)
            if parsed_price is not None and parsed_size is not None and MyriadAdapter._is_positive_number(parsed_size):
                levels.append(OrderBookLevel(price=parsed_price, size=parsed_size))
        levels.sort(key=lambda level: level.price, reverse=descending)
        return levels

    @staticmethod
    def _scaled_decimal(value: Any) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        if number > 10_000_000_000:
            number /= 1_000_000_000_000_000_000
        return number

    @staticmethod
    def _orderbook_outcome_param(outcome_id: str) -> Any:
        clean = str(outcome_id or "").strip()
        return int(clean) if clean.isdigit() else clean

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
    def _activity_rows(payload: Any) -> List[Mapping[str, Any]]:
        """Extract the list from the documented paginated user-events shape."""

        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, Mapping)]
        if isinstance(payload, Mapping):
            for key in ("data", "events", "results", "items"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, Mapping)]
                if isinstance(value, Mapping):
                    for nested_key in ("data", "events", "results", "items"):
                        nested = value.get(nested_key)
                        if isinstance(nested, list):
                            return [item for item in nested if isinstance(item, Mapping)]
        return []

    @staticmethod
    def _finite_number(value: Any) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    @staticmethod
    def _required_positive_number(value: Any, label: str) -> float:
        number = MyriadAdapter._finite_number(value)
        if number is None:
            raise MarketConfigurationError(f"{label} must be numeric.")
        if number <= 0:
            raise MarketConfigurationError(f"{label} must be positive.")
        return number

    @staticmethod
    def _timestamp_seconds(value: Any) -> int:
        number = MyriadAdapter._finite_number(value)
        if number is None or number <= 0:
            return 0
        timestamp = int(number)
        return timestamp // 1000 if timestamp > 10_000_000_000 else timestamp

    @staticmethod
    def _status_from_question(question: Mapping[str, Any]) -> str:
        markets = MyriadAdapter._markets_from_question(question)
        if any(MyriadAdapter._status_from_market(market) == "open" for market in markets):
            return "open"
        return str(question.get("status") or question.get("state") or "").strip().lower()

    @staticmethod
    def _status_from_market(market: Mapping[str, Any]) -> str:
        return str(market.get("state") or market.get("status") or "").strip().lower()

    @staticmethod
    def _question_url(question: Mapping[str, Any]) -> str:
        markets = MyriadAdapter._markets_from_question(question)
        if markets:
            return MyriadAdapter._market_url(markets[0])
        question_id = MyriadAdapter._question_id(question)
        return f"https://myriad.markets/questions/{question_id}" if question_id else "https://myriad.markets"

    @staticmethod
    def _market_url(market: Mapping[str, Any]) -> str:
        raw = str(market.get("url") or "").strip()
        if raw:
            return raw
        slug = str(market.get("slug") or "").strip()
        return f"https://myriad.markets/markets/{slug}" if slug else "https://myriad.markets"

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
    def _is_positive_number(value: Any) -> bool:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False
        return math.isfinite(number) and number > 0
