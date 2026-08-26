from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlencode

from .base import MarketAdapter
from .catalog import get_market_metadata
from .errors import MarketConfigurationError
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


DEFAULT_PROBABLE_MARKET_BASE_URL = "https://market-api.probable.markets/public/api/v1"
DEFAULT_PROBABLE_CLOB_BASE_URL = "https://api.probable.markets/public/api/v1"
PROBABLE_CHAIN_ID = 56
PROBABLE_ACCOUNT_OPERATIONS = ("open_orders", "order")
PROBABLE_ORDER_MANAGEMENT_OPERATIONS = ("cancel_order", "cancel_orders", "cancel_all_orders")
PROBABLE_ORDER_MANAGEMENT_CONFIRMATION = "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
PROBABLE_GLOBAL_CANCEL_CONFIRMATION = "CANCEL ALL PROBABLE ORDERS"
PROBABLE_MAX_ORDER_BATCH = 50
PROBABLE_ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
PROBABLE_SIGNED_ORDER_FIELDS = frozenset(
    {
        "salt",
        "maker",
        "signer",
        "taker",
        "tokenId",
        "makerAmount",
        "takerAmount",
        "expiration",
        "nonce",
        "feeRateBps",
        "side",
        "signatureType",
        "signature",
    }
)
PROBABLE_ORDER_PAYLOAD_FIELDS = frozenset({"deferExec", "order", "owner", "orderType"})
PROBABLE_REFERENCES = (
    "https://developer.probable.markets/",
    "https://www.npmjs.com/package/@prob/clob",
    "https://github.com/0xprobable/clob-examples",
)


class ProbableAdapter(MarketAdapter):
    """Probable adapter using the documented market and CLOB APIs.

    Public discovery and orderbook reads do not require credentials.  Live order
    submission accepts an already signed order payload and uses Probable's
    documented HMAC L2 headers; this keeps private-key signing outside the
    adapter until a dedicated, audited BSC signing workflow is provided.
    """

    metadata = get_market_metadata("probable")
    account_recovery_operations = PROBABLE_ACCOUNT_OPERATIONS
    order_management_operations = PROBABLE_ORDER_MANAGEMENT_OPERATIONS

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        credential_sources = []
        for config_key, env_vars, label in (
            ("probable_address", ("PROB_ADDRESS", "PROBABLE_ADDRESS", "PROB_WALLET_ADDRESS"), "PROB_ADDRESS"),
            ("probable_api_key", ("PROB_API_KEY", "PROBABLE_API_KEY"), "PROB_API_KEY"),
            ("probable_api_secret", ("PROB_API_SECRET", "PROBABLE_API_SECRET"), "PROB_API_SECRET"),
            ("probable_api_passphrase", ("PROB_PASSPHRASE", "PROBABLE_API_PASSPHRASE"), "PROB_PASSPHRASE"),
        ):
            credential = self.resolve_credential(config_key, env_vars, label=label)
            if credential:
                credential_sources.append({"name": credential.name, "source": credential.source})
        health.update(
            {
                "market_api_base_url": self.market_api_base_url,
                "clob_api_base_url": self.clob_api_base_url,
                "chain_id": self.chain_id,
                "references": list(PROBABLE_REFERENCES),
                "credential_sources": credential_sources,
                "live_trading_supported": True,
                "live_trading_enabled": self.config_bool("live_trading_enabled", False),
                "signed_order_required": True,
                "signed_order_policy": {
                    "allowed_outer_fields": sorted(PROBABLE_ORDER_PAYLOAD_FIELDS),
                    "allowed_signed_fields": sorted(PROBABLE_SIGNED_ORDER_FIELDS),
                    "order_type": "GTC",
                    "defer_execution": True,
                    "expiration": 0,
                    "taker": PROBABLE_ZERO_ADDRESS,
                    "fee_rate_bps": self._trusted_uint_config(
                        "probable_fee_rate_bps",
                        default=0,
                        maximum=10_000,
                    ),
                    "nonce": self._trusted_uint_config(
                        "probable_order_nonce",
                        default=0,
                    ),
                    "signature_type": self._trusted_uint_config(
                        "probable_signature_type",
                        default=0,
                        maximum=255,
                    ),
                },
                "account_recovery_operations": list(self.account_recovery_operations),
                "authenticated_account_endpoints": [
                    "/orders/{chain_id}/open",
                    "/order/{chain_id}/{order_id}",
                ],
                "order_management_operations": list(self.order_management_operations),
                "order_management_enabled": self.config_bool("probable_order_management_enabled", False),
                "authenticated_order_management_endpoints": [
                    "/order/{chain_id}/{order_id} (DELETE)",
                ],
            }
        )
        return health

    @property
    def market_api_base_url(self) -> str:
        configured = self.config.get("probable_market_api_base_url") or self.config.get("api_base_url")
        return str(configured or DEFAULT_PROBABLE_MARKET_BASE_URL).rstrip("/")

    @property
    def clob_api_base_url(self) -> str:
        configured = self.config.get("probable_clob_api_base_url") or self.config.get("clob_api_base_url")
        return str(configured or DEFAULT_PROBABLE_CLOB_BASE_URL).rstrip("/")

    @property
    def chain_id(self) -> int:
        value = self.config.get("probable_chain_id", PROBABLE_CHAIN_ID)
        try:
            chain_id = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Probable chain id must be an integer.") from exc
        if chain_id <= 0:
            raise MarketConfigurationError("Probable chain id must be positive.")
        return chain_id

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = max(1, min(int(limit or 50), 100))
        params: Dict[str, Any] = {"limit": desired, "closed": False}
        status = str(self.config.get("probable_event_status") or "").strip()
        if status:
            params["status"] = status
        payload = self._public_get("/events", params=params)
        events = self._list_from_payload(payload, "events", "data")
        needle = str(query or "").strip().lower()
        if needle:
            events = [event for event in events if needle in self._search_text(event)]
        return [self._event_from_payload(event) for event in events[:desired]]

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        clean_event_id = str(event_id or "").strip()
        if not clean_event_id:
            raise MarketConfigurationError("Probable event id cannot be empty.")
        event = self._get_event(clean_event_id)
        markets = self._list_from_payload(event, "markets", "data")
        if not markets:
            payload = self._public_get(
                "/markets",
                params={"event_id": clean_event_id, "limit": 100, "closed": False},
            )
            markets = self._list_from_payload(payload, "markets", "data")
        contracts: List[MarketContract] = []
        for market in markets:
            contracts.extend(self._contracts_from_market(market, event_id=clean_event_id))
        return contracts

    def get_orderbook(self, contract_id: str) -> OrderBookSnapshot:
        self.ensure_capability("orderbook_reading")
        market_id, token_ref = self._split_contract_id(contract_id)
        token_id, canonical_contract_id = self._resolve_token(market_id, token_ref)
        payload = self._clob_get("/book", params={"token_id": token_id})
        book = self._mapping_payload(payload)
        return OrderBookSnapshot(
            market_id=self.market_id,
            contract_id=canonical_contract_id,
            bids=self._levels(book.get("bids"), descending=True),
            asks=self._levels(book.get("asks")),
            raw=dict(book),
        )

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        market_id, token_ref = self._split_contract_id(contract_id)
        token_id, canonical_contract_id = self._resolve_token(market_id, token_ref)
        orderbook = self.get_orderbook(canonical_contract_id)
        bid = orderbook.bids[0].price if orderbook.bids else None
        ask = orderbook.asks[0].price if orderbook.asks else None
        if bid is None:
            bid = self._price_from_payload(self._clob_get("/price", params={"token_id": token_id, "side": "BUY"}))
        if ask is None:
            ask = self._price_from_payload(self._clob_get("/price", params={"token_id": token_id, "side": "SELL"}))
        midpoint = (bid + ask) / 2.0 if bid is not None and ask is not None else None
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=canonical_contract_id,
            last=midpoint or bid or ask,
            bid=bid,
            ask=ask,
            midpoint=midpoint,
            source="probable_clob",
            raw=dict(orderbook.raw),
        )

    def list_trades(
        self,
        contract_id: str,
        *,
        limit: int = 50,
        before: Optional[float] = None,
        after: Optional[float] = None,
    ) -> List[MarketTrade]:
        """Return normalized wallet activity from Probable's public feed.

        Probable's documented ``/activity`` endpoint is public, but it is
        wallet-scoped.  The adapter therefore requires an explicit wallet
        address and requests only ``TRADE`` records.  The response is narrowed
        to the requested token locally because the upstream activity contract
        filters by condition id rather than by outcome token.
        """

        self.ensure_capability("trade_history")
        wallet = self._wallet_address()
        market_id, token_ref = self._split_contract_id(contract_id)
        token_id, canonical_contract_id, market = self._resolve_token_details(market_id, token_ref)
        desired = self._bounded_limit(limit, maximum=500, label="Probable trade limit")
        before_ts = self._timestamp_seconds(before, "before") if before is not None else None
        after_ts = self._timestamp_seconds(after, "after") if after is not None else None
        if before_ts is not None and after_ts is not None and before_ts < after_ts:
            raise MarketConfigurationError("Probable trade history requires before to be at or after after.")

        params: Dict[str, Any] = {
            "user": wallet,
            "limit": desired,
            "offset": 0,
            "type": ["TRADE"],
            "sortBy": "TIMESTAMP",
            "sortDirection": "DESC",
        }
        condition_id = self._condition_id(market)
        if condition_id:
            # The SDK types this filter as string[], and requests encodes it
            # as a repeated query parameter for the public activity endpoint.
            params["market"] = [condition_id]

        payload = self._clob_get("/activity", params=params)
        rows = self._list_from_payload(payload, "activity", "transactions", "data")
        trades: List[MarketTrade] = []
        for row in rows:
            raw_asset = str(
                row.get("asset")
                or row.get("tokenId")
                or row.get("token_id")
                or ""
            ).strip()
            if raw_asset and raw_asset != token_id:
                continue
            raw_condition = str(row.get("conditionId") or row.get("condition_id") or "").strip()
            if condition_id and raw_condition and raw_condition != condition_id:
                continue
            activity_type = str(row.get("type") or "").strip().upper()
            if activity_type and activity_type != "TRADE":
                continue
            trade_id = str(
                row.get("transactionHash")
                or row.get("transaction_hash")
                or row.get("tradeId")
                or row.get("trade_id")
                or row.get("id")
                or ""
            ).strip()
            side = str(row.get("side") or "").strip().upper()
            price = self._safe_probability(row.get("price"))
            size = self._positive_number(row.get("size"))
            timestamp = self._optional_timestamp(row.get("timestamp"))
            if not trade_id or side not in {"BUY", "SELL"} or price is None or size is None:
                continue
            if after_ts is not None and (timestamp is None or timestamp < after_ts):
                continue
            if before_ts is not None and (timestamp is None or timestamp > before_ts):
                continue
            trades.append(
                MarketTrade(
                    market_id=self.market_id,
                    contract_id=canonical_contract_id,
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

    def list_activity(self, wallet_address: str, *, limit: int = 25) -> List[Dict[str, Any]]:
        """Return normalized trade activity for an explicit Probable wallet.

        The documented public ``/activity`` feed accepts a wallet and returns
        mixed account events.  Only complete ``TRADE`` records are admitted to
        the wallet/copy workflow; splits, merges, liquidity, and malformed rows
        are ignored.  Contract ids use the upstream market id when present and
        otherwise fall back to the condition id, preserving the token id and
        source identifiers for a later paper preview.
        """

        self.ensure_capability("copy_trading")
        wallet = require_activity_identity(self.market_id, wallet_address)
        desired = self._bounded_limit(limit, maximum=500, label="Probable activity limit")
        payload = self._clob_get(
            "/activity",
            params={
                "user": wallet,
                "limit": desired,
                "offset": 0,
                "type": ["TRADE"],
                "sortBy": "TIMESTAMP",
                "sortDirection": "DESC",
            },
        )
        rows = self._list_from_payload(payload, "activity", "transactions", "data")
        activities: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            activity_type = str(row.get("type") or "").strip().upper()
            if activity_type and activity_type != "TRADE":
                continue
            token_id = str(row.get("asset") or row.get("tokenId") or row.get("token_id") or "").strip()
            condition_id = str(row.get("conditionId") or row.get("condition_id") or "").strip()
            raw_market = row.get("marketId") or row.get("market_id") or row.get("market") or condition_id or ""
            if isinstance(raw_market, Mapping):
                raw_market = raw_market.get("id") or raw_market.get("marketId") or raw_market.get("market_id") or ""
            market_id = str(raw_market).strip()
            trade_id = str(
                row.get("transactionHash")
                or row.get("transaction_hash")
                or row.get("tradeId")
                or row.get("trade_id")
                or row.get("id")
                or ""
            ).strip()
            side = str(row.get("side") or "").strip().upper()
            price = self._safe_probability(row.get("price"))
            size = self._positive_number(row.get("size"))
            timestamp = self._optional_timestamp(row.get("timestamp"))
            if not token_id or not market_id or not trade_id or side not in {"BUY", "SELL"}:
                continue
            if price is None or size is None or timestamp is None:
                continue
            try:
                market_id = self._safe_identifier(market_id, "market")
                token_id = self._safe_identifier(token_id, "token")
            except MarketConfigurationError:
                continue
            activities.append(
                {
                    "proxyWallet": wallet,
                    "asset": f"{market_id}:{token_id}",
                    "contract_id": f"{market_id}:{token_id}",
                    "marketId": market_id,
                    "conditionId": condition_id,
                    "side": side,
                    "size": size,
                    "price": price,
                    "timestamp": timestamp,
                    "transactionHash": trade_id,
                    "outcome": row.get("outcome") or "",
                    "outcomeIndex": row.get("outcomeIndex") if row.get("outcomeIndex") is not None else row.get("outcome_index"),
                    "slug": row.get("slug") or "",
                    "title": row.get("title") or "",
                    "activityType": "TRADE",
                    "raw": dict(row),
                    "source": "probable_public_activity",
                }
            )
            if len(activities) >= desired:
                break
        return activities

    def list_candles(
        self,
        contract_id: str,
        *,
        resolution: str = "1h",
        from_timestamp: Optional[float] = None,
        to_timestamp: Optional[float] = None,
    ) -> List[MarketCandle]:
        """Return Probable's documented point price history as flat candles."""

        self.ensure_capability("candle_history")
        market_id, token_ref = self._split_contract_id(contract_id)
        token_id, canonical_contract_id, _market = self._resolve_token_details(market_id, token_ref)
        clean_resolution = str(resolution or "").strip().lower()
        if clean_resolution not in {"max", "1m", "1w", "1d", "6h", "1h"}:
            raise MarketConfigurationError(
                "Probable price-history interval must be one of max, 1m, 1w, 1d, 6h, or 1h."
            )

        start_ts = self._timestamp_seconds(from_timestamp, "from_timestamp") if from_timestamp is not None else None
        end_ts = self._timestamp_seconds(to_timestamp, "to_timestamp") if to_timestamp is not None else None
        if start_ts is not None and end_ts is not None and end_ts <= start_ts:
            raise MarketConfigurationError(
                "Probable price history requires to_timestamp greater than from_timestamp."
            )
        params: Dict[str, Any] = {"market": token_id, "interval": clean_resolution}
        if start_ts is not None:
            params["startTs"] = self._milliseconds(start_ts)
        if end_ts is not None:
            params["endTs"] = self._milliseconds(end_ts)

        payload = self._clob_get("/prices-history", params=params)
        if isinstance(payload, Mapping):
            history = payload.get("history")
            if not isinstance(history, list):
                history = payload.get("data")
        else:
            history = payload
        if not isinstance(history, list):
            return []

        candles: List[MarketCandle] = []
        for row in history:
            if not isinstance(row, Mapping):
                continue
            timestamp = self._optional_timestamp(row.get("t") if row.get("t") is not None else row.get("timestamp"))
            price_value = row.get("p") if row.get("p") is not None else row.get("price")
            price = self._safe_probability(price_value)
            if timestamp is None or price is None:
                continue
            if start_ts is not None and timestamp < start_ts:
                continue
            if end_ts is not None and timestamp > end_ts:
                continue
            candles.append(
                MarketCandle(
                    market_id=self.market_id,
                    contract_id=canonical_contract_id,
                    timestamp=timestamp,
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    raw=dict(row),
                )
            )
        return candles

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        self._validate_order(order)
        market_id, token_ref = self._split_contract_id(order.contract_id)
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=f"{market_id}:{token_ref}",
            accepted=True,
            message=(
                f"DRY RUN: would place Probable {str(order.side).upper()} "
                f"for {float(order.size):.4f} shares"
                + (f" at limit {float(order.limit_price):.4f}" if order.limit_price is not None else "")
            ),
            raw={"market_id": market_id, "token_ref": token_ref},
        )

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        self.ensure_capability("live_trading")
        self._validate_order(order)
        preflight = self.preflight_live_order(order)
        credentials = self._l2_credentials()
        payload = self._live_order_payload(order, trusted_wallet=credentials["address"])
        path = str(self.config.get("probable_order_path") or f"/orders/{self.chain_id}")
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        headers = self._l2_headers("POST", path, body, credentials)
        response = self._request_json("POST", path, body, headers)
        return {
            "market_id": self.market_id,
            "contract_id": order.contract_id,
            "live": True,
            "preflight": preflight,
            "request": payload,
            "response": response,
        }

    def account_recovery(self, operation: str, **kwargs: Any) -> Any:
        """Read Probable's documented authenticated order endpoints.

        The official SDK exposes ``getOpenOrders`` and ``getOrder`` through
        the L2-authenticated CLOB API.  Requests are signed against the full
        path, including query parameters, and only the fixed order routes are
        accepted here.
        """

        normalized = str(operation or "").strip().lower()
        if normalized not in self.account_recovery_operations:
            supported = ", ".join(self.account_recovery_operations)
            raise MarketConfigurationError(f"Probable account recovery supports only: {supported}.")

        if normalized == "open_orders":
            path = self._open_orders_path(kwargs)
            return self._l2_request("GET", path)

        order_id = self._safe_identifier(kwargs.get("order_id"), "order")
        token_id = self._safe_identifier(kwargs.get("token_id"), "token")
        query: Dict[str, str] = {"tokenId": token_id}
        client_order_id = kwargs.get("client_order_id") or kwargs.get("orig_client_order_id")
        if client_order_id:
            query["origClientOrderId"] = self._safe_identifier(client_order_id, "client order")
        path = self._with_query(f"/order/{self.chain_id}/{order_id}", query)
        return self._l2_request("GET", path)

    def manage_orders(self, operation: str, **kwargs: Any) -> Dict[str, Any]:
        """Run guarded Probable order cancellations through fixed API paths.

        Probable documents a single-order ``DELETE /order/{chain}/{id}``
        operation.  Batch and cancel-all requests are deliberately composed
        from that fixed endpoint after bounded local validation; this avoids
        guessing an undocumented bulk path while preserving the SDK's public
        cancellation surface.
        """

        normalized = str(operation or "").strip().lower()
        if normalized not in self.order_management_operations:
            supported = ", ".join(self.order_management_operations)
            raise MarketConfigurationError(
                f"Probable order-management operation must be one of: {supported}."
            )
        self.ensure_capability("live_trading")
        if not self.config_bool("probable_order_management_enabled", False):
            raise MarketConfigurationError(
                "Probable order management is disabled by adapter config. "
                "Set probable_order_management_enabled=true only after reviewing live-order risk controls."
            )
        self.ensure_live_trading_enabled("Probable order management")
        if str(kwargs.get("confirm_order_management") or "").strip() != PROBABLE_ORDER_MANAGEMENT_CONFIRMATION:
            raise MarketConfigurationError(
                "Probable order management requires exact confirmation text "
                f"{PROBABLE_ORDER_MANAGEMENT_CONFIRMATION}."
            )
        # Resolve credentials before any mutation or account read used by a
        # composed cancellation operation.
        self._l2_credentials()

        responses: List[Any] = []
        requests: List[Dict[str, Any]] = []
        if normalized == "cancel_order":
            order_id, token_id, client_order_id = self._order_identity(kwargs)
            response = self._cancel_order(order_id, token_id, client_order_id)
            responses.append(response)
            requests.append(self._order_request_details(order_id, token_id, client_order_id))
        elif normalized == "cancel_orders":
            identities = self._order_identities(kwargs)
            for order_id, token_id, client_order_id in identities:
                responses.append(self._cancel_order(order_id, token_id, client_order_id))
                requests.append(self._order_request_details(order_id, token_id, client_order_id))
        else:
            if str(kwargs.get("confirm_global_cancel") or "").strip() != PROBABLE_GLOBAL_CANCEL_CONFIRMATION:
                raise MarketConfigurationError(
                    "Probable cancel_all_orders requires exact global confirmation text "
                    f"{PROBABLE_GLOBAL_CANCEL_CONFIRMATION}."
                )
            open_orders = self.account_recovery(
                "open_orders",
                event_id=kwargs.get("event_id"),
                token_ids=kwargs.get("token_ids"),
                page=kwargs.get("page", 1),
                limit=kwargs.get("limit", PROBABLE_MAX_ORDER_BATCH),
            )
            rows = self._list_from_payload(open_orders, "orders", "data")
            identities = [self._order_identity(row) for row in rows]
            if len(identities) > PROBABLE_MAX_ORDER_BATCH:
                raise MarketConfigurationError(
                    f"Probable cancel_all_orders is capped at {PROBABLE_MAX_ORDER_BATCH} open orders per request."
                )
            for order_id, token_id, client_order_id in identities:
                responses.append(self._cancel_order(order_id, token_id, client_order_id))
                requests.append(self._order_request_details(order_id, token_id, client_order_id))

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
                "references": list(PROBABLE_REFERENCES),
            },
            "request": {"orders": requests},
            "response": responses if normalized != "cancel_order" else responses[0],
        }

    def copy_trade_from_activity(self, activity: Mapping[str, Any]) -> PaperOrderResult:
        self.ensure_capability("copy_trading")
        contract_id = str(activity.get("asset") or activity.get("contract_id") or "").strip()
        if not contract_id:
            raise MarketConfigurationError("Probable activity has no market/token contract id.")
        side = str(activity.get("side") or "").strip().upper()
        if side not in {"BUY", "SELL"}:
            raise MarketConfigurationError("Probable activity side must be BUY or SELL.")
        size = self._positive_number(activity.get("size"))
        if size is None:
            raise MarketConfigurationError("Probable activity size must be positive and numeric.")
        raw_price = activity.get("price")
        limit_price = None if raw_price in (None, "") else self._safe_probability(raw_price)
        if raw_price not in (None, "") and limit_price is None:
            raise MarketConfigurationError("Probable activity reference price must be between 0 and 1.")
        return self.place_paper_order(
            PaperOrderRequest(
                market_id=self.market_id,
                contract_id=contract_id,
                side=side,
                size=size,
                limit_price=limit_price,
                metadata={"activity": dict(activity), "source": "probable_public_activity"},
            )
        )

    def _get_event(self, event_id: str) -> Mapping[str, Any]:
        payload = self._public_get(f"/events/{event_id}")
        event = self._mapping_payload(payload)
        if not event:
            raise MarketConfigurationError(f"Probable event {event_id!r} was not found.")
        return event

    def _get_market(self, market_id: str) -> Mapping[str, Any]:
        payload = self._public_get(f"/markets/{market_id}")
        market = self._mapping_payload(payload)
        if not market:
            raise MarketConfigurationError(f"Probable market {market_id!r} was not found.")
        return market

    def _resolve_token(self, market_id: str, token_ref: str) -> Tuple[str, str]:
        token_id, canonical_contract_id, _market = self._resolve_token_details(market_id, token_ref)
        return token_id, canonical_contract_id

    def _resolve_token_details(self, market_id: str, token_ref: str) -> Tuple[str, str, Mapping[str, Any]]:
        clean_ref = str(token_ref or "").strip()
        if not clean_ref:
            raise MarketConfigurationError("Probable contract token or outcome cannot be empty.")
        market = self._get_market(market_id)
        tokens = self._token_rows(market)
        for token in tokens:
            token_id = self._token_id(token)
            outcome = self._outcome_label(token)
            if clean_ref == token_id or clean_ref.upper() == outcome.upper():
                if not token_id:
                    break
                return token_id, f"{market_id}:{token_id}", market
        if clean_ref not in {"YES", "NO"}:
            return clean_ref, f"{market_id}:{clean_ref}", market
        raise MarketConfigurationError(f"Probable market {market_id!r} has no {clean_ref} token.")

    def _wallet_address(self) -> str:
        credential = self.resolve_credential(
            "probable_address",
            ("PROB_ADDRESS", "PROBABLE_ADDRESS", "PROB_WALLET_ADDRESS"),
            required=True,
            label="PROB_ADDRESS",
        )
        assert credential is not None
        wallet = str(credential.value).strip()
        self._validate_wallet_address(wallet, "Probable wallet address")
        return wallet

    @staticmethod
    def _condition_id(market: Mapping[str, Any]) -> str:
        return str(market.get("condition_id") or market.get("conditionId") or "").strip()

    @staticmethod
    def _bounded_limit(value: Any, *, maximum: int, label: str) -> int:
        try:
            desired = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"{label} must be an integer between 1 and {maximum}.") from exc
        if desired < 1 or desired > maximum:
            raise MarketConfigurationError(f"{label} must be between 1 and {maximum}.")
        return desired

    @staticmethod
    def _timestamp_seconds(value: Any, label: str) -> float:
        try:
            timestamp = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"Probable {label} must be a finite Unix timestamp.") from exc
        if not math.isfinite(timestamp) or timestamp < 0:
            raise MarketConfigurationError(f"Probable {label} must be a finite, non-negative Unix timestamp.")
        return timestamp

    @staticmethod
    def _milliseconds(timestamp: float) -> int:
        return int(round(timestamp * 1000.0))

    @staticmethod
    def _optional_timestamp(value: Any) -> Optional[float]:
        try:
            timestamp = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(timestamp) or timestamp < 0:
            return None
        if timestamp > 100_000_000_000:
            timestamp /= 1000.0
        return timestamp

    @staticmethod
    def _positive_number(value: Any) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number) or number <= 0:
            return None
        return number

    def _public_get(self, path: str, *, params: Optional[Mapping[str, Any]] = None) -> Any:
        return self.runtime.get_json(self._url(self.market_api_base_url, path), params=params)

    def _clob_get(self, path: str, *, params: Optional[Mapping[str, Any]] = None) -> Any:
        return self.runtime.get_json(self._url(self.clob_api_base_url, path), params=params)

    def _request_json(self, method: str, path: str, body: Optional[str], headers: Mapping[str, str]) -> Any:
        request_headers = {"Accept": "application/json", "User-Agent": self.runtime.user_agent, **dict(headers)}
        return self.runtime.request_json(
            method.upper(),
            self._url(self.clob_api_base_url, path),
            data=body,
            headers=request_headers,
        )

    def _l2_request(self, method: str, path: str, body: Optional[Any] = None) -> Any:
        credentials = self._l2_credentials()
        if body is None:
            body_text = ""
            request_body = None
        elif isinstance(body, str):
            body_text = body
            request_body = body
        else:
            body_text = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
            request_body = body_text
        headers = self._l2_headers(method, path, body_text, credentials)
        return self._request_json(method, path, request_body, headers)

    def _open_orders_path(self, kwargs: Mapping[str, Any]) -> str:
        page = self._bounded_limit(kwargs.get("page", 1), maximum=10_000, label="Probable order page")
        limit = self._bounded_limit(
            kwargs.get("limit", PROBABLE_MAX_ORDER_BATCH),
            maximum=PROBABLE_MAX_ORDER_BATCH,
            label="Probable open-order limit",
        )
        query: List[Tuple[str, str]] = [("page", str(page)), ("limit", str(limit))]
        event_id = kwargs.get("event_id")
        if event_id:
            query.append(("eventId", self._safe_identifier(event_id, "event")))
        token_ids = self._identifier_list(kwargs.get("token_ids") or kwargs.get("token_id"), "token")
        query.extend(("tokenIds", token_id) for token_id in token_ids)
        return self._with_query(f"/orders/{self.chain_id}/open", query)

    def _cancel_order(self, order_id: str, token_id: str, client_order_id: Optional[str]) -> Any:
        query: Dict[str, str] = {"tokenId": token_id}
        if client_order_id:
            query["origClientOrderId"] = client_order_id
        return self._l2_request("DELETE", self._with_query(f"/order/{self.chain_id}/{order_id}", query))

    def _order_identity(self, value: Mapping[str, Any]) -> Tuple[str, str, Optional[str]]:
        if not isinstance(value, Mapping):
            raise MarketConfigurationError("Probable order identity must be an object.")
        order_id = self._safe_identifier(value.get("order_id") or value.get("orderId"), "order")
        token_id = self._safe_identifier(
            value.get("token_id") or value.get("tokenId") or value.get("ctfTokenId"),
            "token",
        )
        client_order_id = value.get("client_order_id") or value.get("clientOrderId") or value.get("origClientOrderId")
        if client_order_id:
            client_order_id = self._safe_identifier(client_order_id, "client order")
        return order_id, token_id, client_order_id

    def _order_identities(self, kwargs: Mapping[str, Any]) -> List[Tuple[str, str, Optional[str]]]:
        raw_orders = kwargs.get("orders")
        if raw_orders is not None:
            if not isinstance(raw_orders, list) or not raw_orders:
                raise MarketConfigurationError("Probable cancel_orders requires a non-empty orders array.")
            if len(raw_orders) > PROBABLE_MAX_ORDER_BATCH:
                raise MarketConfigurationError(
                    f"Probable cancel_orders is capped at {PROBABLE_MAX_ORDER_BATCH} orders."
                )
            identities = [self._order_identity(row) for row in raw_orders]
        else:
            order_ids = self._identifier_list(kwargs.get("order_ids") or kwargs.get("order_id"), "order")
            if not order_ids:
                raise MarketConfigurationError("Probable cancel_orders requires order_ids.")
            if len(order_ids) > PROBABLE_MAX_ORDER_BATCH:
                raise MarketConfigurationError(
                    f"Probable cancel_orders is capped at {PROBABLE_MAX_ORDER_BATCH} orders."
                )
            token_id = self._safe_identifier(kwargs.get("token_id"), "token")
            identities = [(order_id, token_id, None) for order_id in order_ids]
        seen = set()
        for order_id, token_id, _client_order_id in identities:
            key = (order_id, token_id)
            if key in seen:
                raise MarketConfigurationError("Probable cancel_orders cannot contain duplicate order/token pairs.")
            seen.add(key)
        return identities

    @staticmethod
    def _order_request_details(order_id: str, token_id: str, client_order_id: Optional[str]) -> Dict[str, Any]:
        details: Dict[str, Any] = {"order_id": order_id, "token_id": token_id}
        if client_order_id:
            details["client_order_id"] = client_order_id
        return details

    @staticmethod
    def _identifier_list(value: Any, label: str) -> List[str]:
        if value in (None, ""):
            return []
        raw_values = value if isinstance(value, (list, tuple)) else str(value).split(",")
        if not raw_values:
            return []
        identifiers = []
        for raw in raw_values:
            clean = ProbableAdapter._safe_identifier(raw, label)
            if clean not in identifiers:
                identifiers.append(clean)
        return identifiers

    @staticmethod
    def _safe_identifier(value: Any, label: str) -> str:
        clean = str(value or "").strip()
        if not clean or len(clean) > 256 or not re.fullmatch(r"[A-Za-z0-9._:-]+", clean):
            raise MarketConfigurationError(f"Probable {label} identifier is invalid.")
        return clean

    @staticmethod
    def _with_query(path: str, query: Mapping[str, Any] | List[Tuple[str, str]]) -> str:
        if isinstance(query, Mapping):
            items = list(query.items())
        else:
            items = list(query)
        encoded = urlencode(items, doseq=True)
        return f"{path}?{encoded}" if encoded else path

    def _l2_credentials(self) -> Dict[str, str]:
        values: Dict[str, str] = {}
        for key, config_key, env_vars, label in (
            ("address", "probable_address", ("PROB_ADDRESS", "PROBABLE_ADDRESS", "PROB_WALLET_ADDRESS"), "PROB_ADDRESS"),
            ("api_key", "probable_api_key", ("PROB_API_KEY", "PROBABLE_API_KEY"), "PROB_API_KEY"),
            ("secret", "probable_api_secret", ("PROB_API_SECRET", "PROBABLE_API_SECRET"), "PROB_API_SECRET"),
            ("passphrase", "probable_api_passphrase", ("PROB_PASSPHRASE", "PROBABLE_API_PASSPHRASE"), "PROB_PASSPHRASE"),
        ):
            credential = self.resolve_credential(config_key, env_vars, required=True, label=label)
            assert credential is not None
            values[key] = credential.value
        self._validate_wallet_address(values["address"], "Probable wallet address")
        return values

    @staticmethod
    def _l2_headers(method: str, path: str, body: str, credentials: Mapping[str, str]) -> Dict[str, str]:
        timestamp = str(int(time.time()))
        message = f"{timestamp}{method.upper()}{path}{body}"
        secret = str(credentials["secret"])
        padded = secret + "=" * (-len(secret) % 4)
        try:
            key = base64.b64decode(padded)
        except (ValueError, TypeError):
            key = secret.encode("utf-8")
        digest = hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()
        signature = base64.b64encode(digest).decode("ascii").replace("+", "-").replace("/", "_")
        headers = {
            "Content-Type": "application/json",
            "PROB_ADDRESS": str(credentials["address"]),
            "PROB_SIGNATURE": signature,
            "PROB_TIMESTAMP": timestamp,
            "PROB_API_KEY": str(credentials["api_key"]),
            "PROB_PASSPHRASE": str(credentials["passphrase"]),
        }
        return headers

    def _live_order_payload(
        self,
        order: PaperOrderRequest,
        *,
        trusted_wallet: Optional[str] = None,
    ) -> Dict[str, Any]:
        existing = order.metadata.get("probable_payload")
        if isinstance(existing, Mapping):
            payload = dict(existing)
            signed_order = payload.get("order")
            if not isinstance(signed_order, Mapping):
                raise MarketConfigurationError(
                    "Probable order.metadata['probable_payload'] must contain a signed 'order' object."
                )
        else:
            payload = {}
            signed_order = order.metadata.get("signed_order") or order.metadata.get("order")
        if not isinstance(signed_order, Mapping):
            raise MarketConfigurationError(
                "Probable live orders require order.metadata['signed_order'] with an EIP-712 signature."
            )
        wallet = str(trusted_wallet or self._wallet_address()).strip()
        self._validate_wallet_address(wallet, "Probable trusted wallet address")
        self._validate_live_order_binding(order, payload, signed_order, trusted_wallet=wallet)
        return {
            "deferExec": True,
            "order": dict(signed_order),
            "owner": wallet,
            "orderType": "GTC",
        }

    def _validate_live_order_binding(
        self,
        order: PaperOrderRequest,
        payload: Mapping[str, Any],
        signed_order: Mapping[str, Any],
        *,
        trusted_wallet: str,
    ) -> None:
        """Bind the complete externally signed CLOB order to trusted policy."""

        unknown_outer = sorted(
            str(field) for field in payload if field not in PROBABLE_ORDER_PAYLOAD_FIELDS
        )
        if unknown_outer:
            raise MarketConfigurationError(
                "Probable live-order payload contains unsupported fields: "
                + ", ".join(unknown_outer)
                + "."
            )
        unknown_signed = sorted(
            str(field) for field in signed_order if field not in PROBABLE_SIGNED_ORDER_FIELDS
        )
        if unknown_signed:
            raise MarketConfigurationError(
                "Probable signed order contains unsupported fields: "
                + ", ".join(unknown_signed)
                + "."
            )
        missing = sorted(
            field
            for field in PROBABLE_SIGNED_ORDER_FIELDS
            if signed_order.get(field) in (None, "")
        )
        if missing:
            raise MarketConfigurationError(
                "Probable signed order is missing required fields: " + ", ".join(missing) + "."
            )

        signature = str(signed_order.get("signature") or "").strip()
        if not signature:
            raise MarketConfigurationError("Probable signed order requires an EIP-712 signature.")
        if re.fullmatch(r"0x[0-9a-fA-F]{130}", signature) is None:
            raise MarketConfigurationError(
                "Probable signed order signature must be a 65-byte 0x-prefixed hexadecimal value."
            )

        market_id, token_ref = self._split_contract_id(order.contract_id)
        canonical_token_id, _canonical_contract_id, market = self._resolve_token_details(
            market_id,
            token_ref,
        )
        market_token_ids = {
            self._token_id(token)
            for token in self._token_rows(market)
            if self._token_id(token)
        }
        if canonical_token_id not in market_token_ids:
            raise MarketConfigurationError(
                f"Probable market {market_id!r} does not contain token or outcome {token_ref!r}."
            )
        token_id = str(signed_order.get("tokenId") or "").strip()
        if not token_id:
            raise MarketConfigurationError("Probable signed order requires tokenId.")
        if token_id != canonical_token_id:
            raise MarketConfigurationError(
                "Probable signed order tokenId does not match the preflighted contract."
            )

        for field in ("maker", "signer"):
            address = str(signed_order.get(field) or "").strip()
            self._validate_wallet_address(address, f"Probable signed order {field}")
            if address.lower() != trusted_wallet.lower():
                raise MarketConfigurationError(
                    f"Probable signed order {field} does not match the trusted configured wallet."
                )
        taker = str(signed_order.get("taker") or "").strip()
        self._validate_wallet_address(taker, "Probable signed order taker")
        if taker.lower() != PROBABLE_ZERO_ADDRESS:
            raise MarketConfigurationError(
                "Probable signed order taker must be the zero address under the guarded live-order policy."
            )

        salt = self._wire_uint(signed_order.get("salt"), "salt")
        if salt == 0:
            raise MarketConfigurationError("Probable signed order salt must be positive.")
        for field, expected in (
            ("expiration", 0),
            (
                "nonce",
                self._trusted_uint_config("probable_order_nonce", default=0),
            ),
            (
                "feeRateBps",
                self._trusted_uint_config(
                    "probable_fee_rate_bps",
                    default=0,
                    maximum=10_000,
                ),
            ),
            (
                "signatureType",
                self._trusted_uint_config(
                    "probable_signature_type",
                    default=0,
                    maximum=255,
                ),
            ),
        ):
            if self._wire_uint(signed_order.get(field), field) != expected:
                raise MarketConfigurationError(
                    f"Probable signed order {field} does not match the trusted live-order policy."
                )

        owner = payload.get("owner")
        if owner not in (None, "") and str(owner).strip().lower() != trusted_wallet.lower():
            raise MarketConfigurationError(
                "Probable live-order owner does not match the trusted configured wallet."
            )
        if payload.get("orderType") not in (None, "", "GTC"):
            raise MarketConfigurationError(
                "Probable live-order orderType must be GTC under the guarded live-order policy."
            )
        if "deferExec" in payload and payload["deferExec"] is not True:
            raise MarketConfigurationError(
                "Probable live-order deferExec must be true under the guarded live-order policy."
            )
        metadata_owner = order.metadata.get("owner")
        if (
            metadata_owner not in (None, "")
            and str(metadata_owner).strip().lower() != trusted_wallet.lower()
        ):
            raise MarketConfigurationError(
                "Probable order metadata owner does not match the trusted configured wallet."
            )
        if order.metadata.get("order_type") not in (None, "", "GTC"):
            raise MarketConfigurationError(
                "Probable order metadata order_type must be GTC under the guarded live-order policy."
            )
        if "defer_exec" in order.metadata and order.metadata["defer_exec"] is not True:
            raise MarketConfigurationError(
                "Probable order metadata defer_exec must be true under the guarded live-order policy."
            )
        if order.metadata.get("slippage_tolerance") is not None:
            raise MarketConfigurationError(
                "Probable live orders do not accept an unbound slippage_tolerance modifier."
            )

        expected_side = 0 if str(order.side).upper() == "BUY" else 1
        actual_side = self._wire_side(signed_order.get("side"))
        if actual_side != expected_side:
            raise MarketConfigurationError(
                "Probable signed order side does not match the preflighted order."
            )

        if order.limit_price is None:
            raise MarketConfigurationError(
                "Probable live orders require a limit price so the signed amounts can be bound to preflight."
            )
        maker_amount = self._wire_amount(signed_order.get("makerAmount"), "makerAmount")
        taker_amount = self._wire_amount(signed_order.get("takerAmount"), "takerAmount")
        expected_amounts = self._expected_signed_amounts(order, expected_side)
        if (maker_amount, taker_amount) not in expected_amounts:
            raise MarketConfigurationError(
                "Probable signed order makerAmount/takerAmount do not match the preflighted price and size."
            )

    def _trusted_uint_config(
        self,
        key: str,
        *,
        default: int,
        maximum: int = (2**256) - 1,
    ) -> int:
        value = self.config.get(key, default)
        parsed = self._wire_uint(value, key)
        if parsed > maximum:
            raise MarketConfigurationError(
                f"Probable {key} must be between 0 and {maximum}."
            )
        return parsed

    @staticmethod
    def _wire_uint(value: Any, label: str) -> int:
        if isinstance(value, bool):
            raise MarketConfigurationError(
                f"Probable signed order {label} must be an unsigned integer."
            )
        try:
            number = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise MarketConfigurationError(
                f"Probable signed order {label} must be an unsigned integer."
            ) from exc
        if (
            not number.is_finite()
            or number < 0
            or number != number.to_integral_value()
            or number >= Decimal(2) ** 256
        ):
            raise MarketConfigurationError(
                f"Probable signed order {label} must be an unsigned 256-bit integer."
            )
        return int(number)

    @staticmethod
    def _validate_wallet_address(value: Any, label: str) -> None:
        if re.fullmatch(r"0x[a-fA-F0-9]{40}", str(value or "").strip()) is None:
            raise MarketConfigurationError(
                f"{label} must be a 0x-prefixed 40-hex-character address."
            )

    def _expected_signed_amounts(self, order: PaperOrderRequest, side: int) -> set[Tuple[int, int]]:
        size = Decimal(str(order.size))
        price = Decimal(str(order.limit_price))
        expected: set[Tuple[int, int]] = set()
        for decimals in self._amount_decimal_candidates():
            scale = Decimal(10) ** decimals
            if side == 0:
                maker, taker = size * price * scale, size * scale
            else:
                maker, taker = size * scale, size * price * scale
            if maker == maker.to_integral_value() and taker == taker.to_integral_value():
                expected.add((int(maker), int(taker)))
        return expected

    def _amount_decimal_candidates(self) -> Tuple[int, ...]:
        configured = self.config.get("probable_amount_decimals")
        if configured is not None:
            try:
                decimals = int(configured)
            except (TypeError, ValueError) as exc:
                raise MarketConfigurationError("Probable amount decimals must be an integer.") from exc
            if decimals < 0 or decimals > 36:
                raise MarketConfigurationError("Probable amount decimals must be between 0 and 36.")
            return (decimals,)
        if self.chain_id != 56:
            raise MarketConfigurationError(
                "Probable amount decimals must be explicitly configured for any chain other than BNB mainnet."
            )
        return (18,)

    @staticmethod
    def _wire_side(value: Any) -> int:
        if isinstance(value, bool):
            raise MarketConfigurationError("Probable signed order side must be BUY/SELL or 0/1.")
        text = str(value if value is not None else "").strip().upper()
        if text in {"0", "BUY"}:
            return 0
        if text in {"1", "SELL"}:
            return 1
        raise MarketConfigurationError("Probable signed order side must be BUY/SELL or 0/1.")

    @staticmethod
    def _wire_amount(value: Any, label: str) -> int:
        if isinstance(value, bool):
            raise MarketConfigurationError(f"Probable signed order {label} must be a positive integer.")
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise MarketConfigurationError(
                f"Probable signed order {label} must be a positive integer."
            ) from exc
        if not amount.is_finite() or amount <= 0 or amount != amount.to_integral_value():
            raise MarketConfigurationError(
                f"Probable signed order {label} must be a positive integer."
            )
        return int(amount)

    def _event_from_payload(self, event: Mapping[str, Any]) -> MarketEvent:
        event_id = self._id(event)
        return MarketEvent(
            market_id=self.market_id,
            event_id=event_id,
            title=str(event.get("title") or event.get("name") or event.get("question") or event_id),
            url=str(event.get("url") or event.get("slug") or ""),
            status=self._status(event),
            raw=dict(event),
        )

    def _contracts_from_market(self, market: Mapping[str, Any], *, event_id: str) -> List[MarketContract]:
        market_id = self._id(market)
        title = str(market.get("question") or market.get("title") or market_id)
        contracts: List[MarketContract] = []
        for token in self._token_rows(market):
            token_id = self._token_id(token)
            if not token_id:
                continue
            outcome = self._outcome_label(token)
            contracts.append(
                MarketContract(
                    market_id=self.market_id,
                    contract_id=f"{market_id}:{token_id}",
                    event_id=event_id,
                    title=f"{title} - {outcome}",
                    outcome=outcome,
                    url=str(market.get("url") or market.get("slug") or ""),
                    status=self._status(market),
                    raw={"market": dict(market), "token": dict(token)},
                )
            )
        return contracts

    def _validate_order(self, order: PaperOrderRequest) -> None:
        self.ensure_order_market(order)
        self._split_contract_id(order.contract_id)
        if str(order.side or "").upper() not in {"BUY", "SELL"}:
            raise MarketConfigurationError("Probable order side must be BUY or SELL.")
        try:
            size = float(order.size)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Probable order size must be numeric.") from exc
        if not math.isfinite(size) or size <= 0:
            raise MarketConfigurationError("Probable order size must be positive and finite.")
        if order.limit_price is not None and self._safe_probability(order.limit_price) is None:
            raise MarketConfigurationError("Probable order limit price must be between 0 and 1.")

    @staticmethod
    def _url(base: str, path: str) -> str:
        return f"{base.rstrip('/')}/{str(path or '').strip('/')}"

    @staticmethod
    def _mapping_payload(payload: Any) -> Dict[str, Any]:
        if isinstance(payload, Mapping):
            data = payload.get("data")
            if isinstance(data, Mapping):
                return dict(data)
            return dict(payload)
        return {}

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
            if isinstance(data, Mapping):
                return ProbableAdapter._list_from_payload(data, *keys)
        return []

    @staticmethod
    def _id(payload: Mapping[str, Any]) -> str:
        return str(payload.get("id") or payload.get("marketId") or payload.get("eventId") or "").strip()

    @staticmethod
    def _status(payload: Mapping[str, Any]) -> str:
        value = payload.get("status") or payload.get("state") or payload.get("tradingStatus") or ""
        if isinstance(value, Mapping):
            value = value.get("name") or value.get("status") or value.get("value") or ""
        return str(value).strip().lower()

    @staticmethod
    def _search_text(payload: Mapping[str, Any]) -> str:
        values = [payload.get("id"), payload.get("title"), payload.get("name"), payload.get("question"), payload.get("description"), payload.get("slug")]
        return " ".join(str(value or "") for value in values).lower()

    @staticmethod
    def _token_rows(market: Mapping[str, Any]) -> List[Mapping[str, Any]]:
        tokens = market.get("tokens")
        if isinstance(tokens, list):
            return [dict(token) if isinstance(token, Mapping) else {"token_id": str(token)} for token in tokens]
        token_ids = market.get("clobTokenIds") or market.get("clob_token_ids")
        if isinstance(token_ids, str):
            try:
                token_ids = json.loads(token_ids)
            except json.JSONDecodeError:
                token_ids = [item.strip() for item in token_ids.split(",") if item.strip()]
        outcomes = market.get("outcomes")
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except json.JSONDecodeError:
                outcomes = [item.strip() for item in outcomes.split(",") if item.strip()]
        if not isinstance(token_ids, list):
            return []
        rows: List[Mapping[str, Any]] = []
        for index, token_id in enumerate(token_ids):
            outcome = outcomes[index] if isinstance(outcomes, list) and index < len(outcomes) else ("Yes" if index == 0 else "No")
            rows.append({"token_id": str(token_id), "outcome": str(outcome)})
        return rows

    @staticmethod
    def _token_id(token: Mapping[str, Any]) -> str:
        return str(token.get("token_id") or token.get("tokenId") or token.get("id") or "").strip()

    @staticmethod
    def _outcome_label(token: Mapping[str, Any]) -> str:
        return str(token.get("outcome") or token.get("label") or token.get("name") or "Outcome").strip()

    @staticmethod
    def _split_contract_id(contract_id: str) -> Tuple[str, str]:
        raw = str(contract_id or "").strip()
        if ":" not in raw:
            raise MarketConfigurationError("Probable contract id must be MARKET_ID:TOKEN_ID or MARKET_ID:YES|NO.")
        market_id, token_ref = raw.rsplit(":", 1)
        if not market_id.strip() or not token_ref.strip():
            raise MarketConfigurationError("Probable contract id must be MARKET_ID:TOKEN_ID or MARKET_ID:YES|NO.")
        return (
            ProbableAdapter._safe_identifier(market_id, "market"),
            ProbableAdapter._safe_identifier(token_ref, "token or outcome"),
        )

    @staticmethod
    def _levels(raw: Any, *, descending: bool = False) -> List[OrderBookLevel]:
        rows: List[Any]
        if isinstance(raw, Mapping):
            rows = [[price, size] for price, size in raw.items()]
        elif isinstance(raw, list):
            rows = raw
        else:
            rows = []
        levels: List[OrderBookLevel] = []
        for item in rows:
            if isinstance(item, Mapping):
                price = item.get("price")
                size = item.get("size") or item.get("quantity") or item.get("qty") or item.get("amount")
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                price, size = item[0], item[1]
            else:
                continue
            try:
                parsed_price = float(price)
                parsed_size = float(size)
            except (TypeError, ValueError):
                continue
            if math.isfinite(parsed_price) and math.isfinite(parsed_size) and 0 <= parsed_price <= 1 and parsed_size > 0:
                levels.append(OrderBookLevel(price=parsed_price, size=parsed_size))
        levels.sort(key=lambda level: level.price, reverse=descending)
        return levels

    @staticmethod
    def _price_from_payload(payload: Any) -> Optional[float]:
        value = payload.get("price") if isinstance(payload, Mapping) else payload
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and 0 <= number <= 1 else None

    @staticmethod
    def _safe_probability(value: Any) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and 0 <= number <= 1 else None
