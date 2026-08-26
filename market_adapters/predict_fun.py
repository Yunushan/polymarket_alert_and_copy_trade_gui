from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .base import MarketAdapter
from .catalog import get_market_metadata
from .errors import MarketConfigurationError
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


DEFAULT_PREDICT_FUN_BASE_URL = "https://api.predict.fun/v1"
PREDICT_FUN_REFERENCES = (
    "https://docs.predict.fun/developers/predict-rest-api",
    "https://dev.predict.fun/get-markets-25326905e0",
    "https://dev.predict.fun/get-the-orderbook-for-a-market-25326908e0",
    "https://dev.predict.fun/get-market-timeseries-25326910e0",
    "https://dev.predict.fun/get-order-match-events-25663812e0",
    "https://dev.predict.fun/get-orders-25326902e0",
    "https://dev.predict.fun/get-positions-32675933e0",
    "https://dev.predict.fun/get-account-activity-32534697e0",
    "https://dev.predict.fun/remove-orders-from-the-orderbook-25326904e0",
    "https://dev.predict.fun/remove-orders-by-hash-38139973e0",
)
PREDICT_FUN_ACCOUNT_RECOVERY_OPERATIONS = (
    "account",
    "active_orders",
    "order_detail",
    "account_activity",
    "positions",
    "positions_by_address",
)
PREDICT_FUN_ORDER_MANAGEMENT_OPERATIONS = ("remove_orders", "remove_orders_by_hash")
PREDICT_FUN_ORDER_MANAGEMENT_CONFIRMATION = "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
PREDICT_FUN_MAX_ORDER_BATCH = 100
PREDICT_FUN_STATUS_VALUES = {"OPEN", "FILLED", "CANCELLED", "CANCELED", "EXPIRED", "MATCHED"}
PREDICT_FUN_POSITION_SORT_VALUES = {"VALUE_DESC", "VALUE_ASC", "UPDATED_DESC", "UPDATED_ASC"}
PREDICT_FUN_ORDER_SCALE = Decimal(10**18)


class PredictFunAdapter(MarketAdapter):
    """Predict.fun adapter using the documented REST API for market data."""

    metadata = get_market_metadata("predict_fun")
    account_recovery_operations = PREDICT_FUN_ACCOUNT_RECOVERY_OPERATIONS
    order_management_operations = PREDICT_FUN_ORDER_MANAGEMENT_OPERATIONS

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        api_key = self.resolve_credential("predict_fun_api_key", ("PREDICT_FUN_API_KEY",), label="PREDICT_FUN_API_KEY")
        jwt = self.resolve_credential(
            "predict_fun_jwt",
            ("PREDICT_FUN_JWT", "PREDICT_FUN_ACCESS_TOKEN"),
            label="PREDICT_FUN_JWT",
        )
        credential_sources = [
            {"name": credential.name, "source": credential.source}
            for credential in (api_key, jwt)
            if credential
        ]
        health.update(
            {
                "api_base_url": self.api_base_url,
                "references": list(PREDICT_FUN_REFERENCES),
                "credential_sources": credential_sources,
                "live_trading_supported": True,
                "live_trading_enabled": self.config_bool("live_trading_enabled", False),
                "account_recovery_operations": list(self.account_recovery_operations),
                "order_management_operations": list(self.order_management_operations),
                "order_management_enabled": self.config_bool("predict_fun_order_management_enabled", False),
                "authenticated_account_endpoints": [
                    "GET /v1/account",
                    "GET /v1/orders",
                    "GET /v1/orders/{hash}",
                    "GET /v1/account/activity",
                    "GET /v1/positions",
                    "GET /v1/positions/{address}",
                ],
                "public_trade_endpoints": ["GET /v1/orders/matches"],
                "order_management_endpoints": [
                    "POST /v1/orders/remove",
                    "POST /orders/remove-by-hash",
                ],
            }
        )
        return health

    @property
    def api_base_url(self) -> str:
        configured = self.config.get("predict_fun_api_base_url") or self.config.get("api_base_url")
        return str(configured or DEFAULT_PREDICT_FUN_BASE_URL).rstrip("/")

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = max(1, min(int(limit or 50), 100))
        params: Dict[str, Any] = {"first": desired}
        status = str(self.config.get("predict_fun_market_status") or "").strip()
        if status:
            params["status"] = status
        payload = self._get("/markets", params=params)
        markets = self._list_from_payload(payload, "data", "markets")
        q = str(query or "").strip().lower()
        if q:
            markets = [market for market in markets if q in self._search_text(market)]
        return [self._event_from_market(market) for market in markets[:desired]]

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        market = self._get_market(event_id)
        return self._contracts_from_market(market)

    def get_orderbook(self, contract_id: str) -> OrderBookSnapshot:
        self.ensure_capability("orderbook_reading")
        market_id, outcome = self._split_contract_id(contract_id)
        payload = self._get(f"/markets/{market_id}/orderbook")
        data = payload.get("data") if isinstance(payload, Mapping) else payload
        orderbook = data if isinstance(data, Mapping) else {}
        yes_bids = self._levels(orderbook.get("bids"), descending=True)
        yes_asks = self._levels(orderbook.get("asks"))
        if outcome == "YES":
            bids, asks = yes_bids, yes_asks
        else:
            bids = self._opposite_bids_from_yes_asks(yes_asks)
            asks = self._opposite_asks_from_yes_bids(yes_bids)
        return OrderBookSnapshot(
            market_id=self.market_id,
            contract_id=self._contract_id(market_id, outcome),
            bids=bids,
            asks=asks,
            raw=orderbook,
        )

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        market_id, outcome = self._split_contract_id(contract_id)
        orderbook = self.get_orderbook(self._contract_id(market_id, outcome))
        bid = orderbook.bids[0].price if orderbook.bids else None
        ask = orderbook.asks[0].price if orderbook.asks else None
        midpoint = (bid + ask) / 2.0 if bid is not None and ask is not None else None
        last = midpoint
        raw: Dict[str, Any] = dict(orderbook.raw)
        if last is None:
            market = self._get_market(market_id)
            last = self._price_from_market(market, outcome)
            raw["market"] = dict(market)
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=self._contract_id(market_id, outcome),
            last=last,
            bid=bid,
            ask=ask,
            midpoint=midpoint,
            source="predict_fun_orderbook",
            raw=raw,
        )

    def list_candles(
        self,
        contract_id: str,
        *,
        resolution: str = "1h",
        from_timestamp: Optional[float] = None,
        to_timestamp: Optional[float] = None,
    ) -> List[MarketCandle]:
        """Normalize the documented market timeseries endpoint.

        Predict.fun returns one price point per timestamp.  The shared candle
        contract represents those points as flat OHLC rows and deliberately
        leaves volume unset; no exchange-style resampling is implied.
        """

        self.ensure_capability("candle_history")
        market_id, outcome = self._split_contract_id(contract_id)
        resolution_value = str(resolution or "1h").strip().lower()
        if resolution_value not in {"raw", "price", "1m", "5m", "15m", "1h", "4h", "1d", "1w"}:
            raise MarketConfigurationError(
                "Predict.fun timeseries resolution must be one of raw, price, 1m, 5m, 15m, 1h, 4h, 1d, or 1w."
            )
        lower = self._timestamp_bound(from_timestamp, "from_timestamp")
        upper = self._timestamp_bound(to_timestamp, "to_timestamp")
        if lower is not None and upper is not None and upper < lower:
            raise MarketConfigurationError("Predict.fun timeseries to_timestamp must not precede from_timestamp.")
        params: Dict[str, Any] = {"resolution": resolution_value}
        payload = self._get(f"/markets/{self._path_segment(market_id, 'market id')}/timeseries", params=params)
        rows = self._list_from_payload(payload, "data", "points", "timeseries")
        candles: List[MarketCandle] = []
        canonical = self._contract_id(market_id, outcome)
        for row in rows:
            timestamp = self._timestamp_value(row.get("timestamp", row.get("time")))
            price_value = row.get("price", row.get("value", row.get("close")))
            if isinstance(price_value, Mapping):
                price_value = price_value.get(outcome) or price_value.get(outcome.lower())
            price = self._safe_probability(price_value)
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
                    raw=dict(row),
                )
            )
        candles.sort(key=lambda item: item.timestamp)
        return candles

    def list_trades(
        self,
        contract_id: str,
        *,
        limit: int = 50,
        before: Optional[float] = None,
        after: Optional[float] = None,
    ) -> List[MarketTrade]:
        """Normalize Predict.fun's documented public order-match feed.

        Predict.fun exposes executed matches at ``GET /v1/orders/matches``.
        The feed is market-filterable but does not provide the shared numeric
        time bounds, so the adapter applies those bounds locally and filters
        the requested outcome before returning normalized trades.  ``Bid`` and
        ``Ask`` quote types are mapped to BUY and SELL respectively; the raw
        match payload remains attached for callers that need maker/taker data.
        """

        self.ensure_capability("trade_history")
        market_id, outcome = self._split_contract_id(contract_id)
        safe_market_id = self._market_id_param(market_id)
        desired = self._bounded_int(limit, "trade limit", minimum=1, maximum=100, default=50)
        lower = self._timestamp_bound(after, "after")
        upper = self._timestamp_bound(before, "before")
        if lower is not None and upper is not None and upper < lower:
            raise MarketConfigurationError("Predict.fun trade history before must not precede after.")

        payload = self._get("/orders/matches", params={"first": desired, "marketId": safe_market_id})
        rows = self._list_from_payload(payload, "data", "matches")
        canonical = self._contract_id(market_id, outcome)
        trades: List[MarketTrade] = []
        for index, row in enumerate(rows):
            row_market = row.get("market")
            if not isinstance(row_market, Mapping):
                row_market = {}
            row_market_id = str(row.get("marketId") or row_market.get("id") or row_market.get("marketId") or "").strip()
            if row_market_id and row_market_id != market_id:
                continue

            taker = row.get("taker") if isinstance(row.get("taker"), Mapping) else {}
            outcome_payload: Any = taker.get("outcome") or row.get("outcome")
            if outcome_payload is None:
                makers = row.get("makers")
                if isinstance(makers, list):
                    for maker in makers:
                        if isinstance(maker, Mapping) and maker.get("outcome") is not None:
                            outcome_payload = maker.get("outcome")
                            break
            if outcome_payload is None or not self._outcome_matches(outcome_payload, outcome):
                continue

            price = self._safe_probability(row.get("priceExecuted") or row.get("price") or row.get("executionPrice"))
            size = row.get("amountFilled") or row.get("filledAmount") or row.get("amount") or row.get("size")
            try:
                parsed_size = float(size)
            except (TypeError, ValueError):
                parsed_size = None
            timestamp = self._timestamp_value(row.get("executedAt") or row.get("timestamp") or row.get("time"))
            if price is None or parsed_size is None or not self._is_positive_number(parsed_size) or timestamp is None:
                continue
            if lower is not None and timestamp < lower:
                continue
            if upper is not None and timestamp > upper:
                continue

            quote_type = taker.get("quoteType") or taker.get("quote_type")
            if not quote_type:
                makers = row.get("makers")
                if isinstance(makers, list) and makers and isinstance(makers[0], Mapping):
                    quote_type = makers[0].get("quoteType") or makers[0].get("quote_type")
            side = {"BID": "BUY", "ASK": "SELL"}.get(str(quote_type or "").strip().upper())
            if side is None:
                continue
            trade_id = str(
                row.get("transactionHash")
                or row.get("tradeId")
                or row.get("trade_id")
                or row.get("id")
                or f"predict_fun:{market_id}:{int(timestamp * 1000)}:{index}"
            ).strip()
            if not trade_id:
                continue
            trades.append(
                MarketTrade(
                    market_id=self.market_id,
                    contract_id=canonical,
                    trade_id=trade_id,
                    side=side,
                    price=price,
                    size=parsed_size,
                    timestamp=timestamp,
                    raw=dict(row),
                )
            )
            if len(trades) >= desired:
                break
        return trades

    def account_recovery(self, operation: str, **kwargs: Any) -> Any:
        """Read Predict.fun's documented authenticated account surfaces."""

        normalized = str(operation or "").strip().lower()
        if normalized not in self.account_recovery_operations:
            supported = ", ".join(self.account_recovery_operations)
            raise MarketConfigurationError(
                f"Predict.fun account operation must be one of: {supported}."
            )
        if normalized == "account":
            return self._get("/account", require_jwt=True)
        if normalized == "order_detail":
            order_hash = self._path_segment(kwargs.get("order_id") or kwargs.get("order_hash"), "order hash")
            return self._get(f"/orders/{order_hash}", require_jwt=True)
        if normalized == "active_orders":
            return self._get("/orders", params=self._orders_query(kwargs), require_jwt=True)
        if normalized == "account_activity":
            params = self._page_query(kwargs)
            event_types = self._csv_tokens(kwargs.get("event_types"), "event_types", max_items=20)
            if event_types:
                params["eventTypes"] = ",".join(event_types)
            return self._get("/account/activity", params=params, require_jwt=True)
        if normalized == "positions":
            return self._get("/positions", params=self._positions_query(kwargs), require_jwt=True)
        address = self._wallet_address(kwargs.get("address") or kwargs.get("wallet"))
        return self._get(
            f"/positions/{address}",
            params=self._positions_query(kwargs),
            require_jwt=False,
        )

    def list_activity(self, wallet_address: str, *, limit: int = 25) -> List[Dict[str, Any]]:
        """Return matched activity for the authenticated Predict.fun account.

        Predict.fun's account activity endpoint is private and does not accept
        an arbitrary wallet filter.  The requested identity is therefore
        checked against the authenticated ``/account`` response before any
        activity is exposed to the wallet-tracking/copy workflow.  Only match
        or fill events with a complete market, outcome, side, size, price, and
        timestamp are returned; creates, cancellations, conversions, and
        position-management events are deliberately excluded.
        """

        self.ensure_capability("copy_trading")
        wallet = self._wallet_address(wallet_address).lower()
        desired = self._bounded_int(limit, "activity limit", minimum=1, maximum=100, default=25)
        account_payload = self.account_recovery("account")
        account_data = account_payload.get("data") if isinstance(account_payload, Mapping) else account_payload
        if not isinstance(account_data, Mapping):
            raise MarketConfigurationError("Predict.fun account response did not contain account data.")
        account_address = self._wallet_address(account_data.get("address"))
        if account_address.lower() != wallet:
            raise MarketConfigurationError(
                "Predict.fun activity identity must match the authenticated account address."
            )
        payload = self.account_recovery("account_activity", limit=desired)
        rows = self._list_from_payload(payload, "data", "activities", "activity")
        return self._normalize_account_activity(rows, wallet, desired)

    def manage_orders(self, operation: str, **kwargs: Any) -> Dict[str, Any]:
        """Remove open orders through Predict.fun's documented REST boundary.

        The upstream remove endpoints only remove orders from the relay and do
        not invalidate them on-chain.  This method therefore keeps a separate
        opt-in and exact confirmation, and labels the response clearly so an
        operator cannot mistake it for on-chain cancellation.
        """

        normalized = str(operation or "").strip().lower()
        if normalized not in self.order_management_operations:
            raise MarketConfigurationError(
                "Predict.fun order-management operation must be one of: "
                + ", ".join(self.order_management_operations)
                + "."
            )
        self.ensure_capability("live_trading")
        if not self.config_bool("predict_fun_order_management_enabled", False):
            raise MarketConfigurationError(
                "Predict.fun order management is disabled by adapter config. "
                "Set predict_fun_order_management_enabled=true only after reviewing the relay-only cancellation risk."
            )
        self.ensure_live_trading_enabled("Predict.fun order management")
        if str(kwargs.get("confirm_order_management") or "").strip() != PREDICT_FUN_ORDER_MANAGEMENT_CONFIRMATION:
            raise MarketConfigurationError(
                "Predict.fun order management requires exact confirmation text "
                f"{PREDICT_FUN_ORDER_MANAGEMENT_CONFIRMATION}."
            )
        if normalized == "remove_orders":
            ids = self._order_ids(kwargs.get("orders", kwargs.get("instructions")))
            request = {"data": {"ids": ids}}
            response = self._request_json("POST", "/orders/remove", request, require_jwt=True)
        else:
            hashes = self._order_hashes(kwargs.get("order_hashes", kwargs.get("orders", kwargs.get("instructions"))))
            request = {"data": {"hashes": hashes}}
            response = self._request_json(
                "POST",
                "/orders/remove-by-hash",
                request,
                require_jwt=True,
                versioned=False,
            )
        return {
            "market_id": self.market_id,
            "operation": normalized,
            "live": True,
            "on_chain_cancellation": False,
            "relay_only_warning": "Predict.fun documents that this endpoint does not invalidate orders on-chain.",
            "preflight": {
                "market_id": self.market_id,
                "display_name": self.display_name,
                "feature": "order management",
                "operation": normalized,
                "live_trading_enabled": True,
                "order_management_enabled": True,
                "confirmed": True,
                "requires_credentials": True,
                "references": list(PREDICT_FUN_REFERENCES),
            },
            "request": request,
            "response": response,
        }

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        self._validate_order(order)
        market_id, outcome = self._split_contract_id(order.contract_id)
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=self._contract_id(market_id, outcome),
            accepted=True,
            message=(
                f"DRY RUN: would place Predict.fun {order.side.upper()} "
                f"for {order.size:.4f} {outcome} shares"
                + (f" at limit {order.limit_price:.2f}" if order.limit_price is not None else "")
            ),
            raw={"market_id": market_id, "outcome": outcome},
        )

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        self.ensure_capability("live_trading")
        self._validate_order(order)
        preflight = self.preflight_live_order(order)
        payload = self._live_order_payload(order)
        self._validate_live_order_binding(order, payload)
        response = self._request_json("POST", "/orders", payload)
        return {
            "market_id": self.market_id,
            "contract_id": order.contract_id,
            "live": True,
            "preflight": preflight,
            "request": payload,
            "response": response,
        }

    def copy_trade_from_activity(self, activity: Mapping[str, Any]) -> PaperOrderResult:
        """Build a simulation-first paper order from a normalized account fill."""

        self.ensure_capability("copy_trading")
        contract_id = str(activity.get("asset") or activity.get("contract_id") or "").strip()
        if not contract_id:
            raise MarketConfigurationError("Predict.fun activity has no market/outcome contract id.")
        side = str(activity.get("side") or "").strip().upper()
        if side not in {"BUY", "SELL"}:
            raise MarketConfigurationError("Predict.fun activity side must be BUY or SELL.")
        try:
            size = float(activity.get("size"))
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Predict.fun activity size must be numeric.") from exc
        if not self._is_positive_number(size):
            raise MarketConfigurationError("Predict.fun activity size must be positive.")
        raw_price = activity.get("price")
        limit_price = None if raw_price in (None, "") else self._safe_probability(raw_price)
        if raw_price not in (None, "") and limit_price is None:
            raise MarketConfigurationError("Predict.fun activity reference price must be between 0 and 1.")
        return self.place_paper_order(
            PaperOrderRequest(
                market_id=self.market_id,
                contract_id=contract_id,
                side=side,
                size=size,
                limit_price=limit_price,
                metadata={
                    "activity": dict(activity),
                    "source": "predict_fun_account_activity",
                },
            )
        )

    @classmethod
    def _normalize_account_activity(
        cls,
        rows: List[Mapping[str, Any]],
        wallet: str,
        limit: int,
    ) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        for row in rows:
            event_names = [
                str(row.get(key) or "").strip().upper()
                for key in ("name", "eventName", "event_type", "type")
                if str(row.get(key) or "").strip()
            ]
            event_name = next(
                (
                    candidate
                    for candidate in event_names
                    if "MATCH" in candidate or "FILL" in candidate or candidate == "TRADE"
                ),
                "",
            )
            if not event_name:
                continue
            market_payload = row.get("market") if isinstance(row.get("market"), Mapping) else row
            market_id = cls._market_id(market_payload)
            if not market_id:
                continue
            outcome_payload = row.get("outcome")
            contract_id, outcome = cls._activity_contract_id(market_id, outcome_payload, market_payload)
            if not contract_id:
                continue
            order_payload = row.get("order") if isinstance(row.get("order"), Mapping) else {}
            quote_type = row.get("quoteType") or order_payload.get("quoteType") or row.get("side")
            side = {"BID": "BUY", "BUY": "BUY", "ASK": "SELL", "SELL": "SELL"}.get(
                str(quote_type or "").strip().upper()
            )
            if side is None:
                continue
            price = cls._safe_probability(
                row.get("priceExecuted")
                or row.get("executionPrice")
                or row.get("price")
                or order_payload.get("price")
            )
            try:
                size = float(
                    row.get("amountFilled")
                    or row.get("filledAmount")
                    or row.get("size")
                    or row.get("amount")
                )
            except (TypeError, ValueError):
                size = None
            timestamp = cls._timestamp_value(
                row.get("executedAt") or row.get("createdAt") or row.get("timestamp") or row.get("time")
            )
            if price is None or size is None or not cls._is_positive_number(size) or timestamp is None:
                continue
            transaction_hash = str(
                row.get("transactionHash")
                or row.get("transaction_hash")
                or row.get("id")
                or row.get("orderId")
                or f"predict_fun_activity:{market_id}:{int(timestamp * 1000)}"
            ).strip()
            if not transaction_hash:
                continue
            normalized.append(
                {
                    "proxyWallet": wallet,
                    "asset": contract_id,
                    "side": side,
                    "size": float(size),
                    "price": price,
                    "timestamp": timestamp,
                    "transactionHash": transaction_hash,
                    "outcome": outcome,
                    "slug": str(market_payload.get("categorySlug") or ""),
                    "pseudonym": str(market_payload.get("title") or market_payload.get("question") or ""),
                    "activityType": event_name,
                    "raw": dict(row),
                }
            )
            if len(normalized) >= limit:
                break
        return normalized

    @classmethod
    def _activity_contract_id(
        cls,
        market_id: str,
        outcome_payload: Any,
        market_payload: Mapping[str, Any],
    ) -> Tuple[Optional[str], str]:
        candidate: Any = None
        if isinstance(outcome_payload, Mapping):
            for key in ("id", "outcomeId", "name", "label", "side", "onChainId", "indexSet"):
                if outcome_payload.get(key) not in (None, ""):
                    candidate = outcome_payload.get(key)
                    break
        elif outcome_payload not in (None, ""):
            candidate = outcome_payload
        if candidate in (None, "") and isinstance(outcome_payload, Mapping):
            outcomes = market_payload.get("outcomes")
            if isinstance(outcomes, list):
                for item in outcomes:
                    if not isinstance(item, Mapping):
                        continue
                    if any(
                        outcome_payload.get(key) not in (None, "")
                        and outcome_payload.get(key) == item.get(key)
                        for key in ("id", "outcomeId", "onChainId", "indexSet")
                    ):
                        candidate = item.get("id") or item.get("name") or item.get("label")
                        break
        if candidate in (None, ""):
            return None, ""
        outcome = str(candidate).strip()
        if outcome.upper() in {"Y", "TRUE"}:
            outcome = "YES"
        elif outcome.upper() in {"N", "FALSE"}:
            outcome = "NO"
        return cls._contract_id(market_id, outcome), outcome

    def _get_market(self, market_id: str) -> Mapping[str, Any]:
        clean = str(market_id or "").strip()
        if not clean:
            raise MarketConfigurationError("Predict.fun market id cannot be empty.")
        payload = self._get(f"/markets/{clean}")
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if isinstance(data, Mapping):
            return data
        if isinstance(payload, Mapping):
            return payload
        raise MarketConfigurationError(f"Predict.fun market {clean!r} was not found.")

    def _get(
        self,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        require_jwt: bool = False,
    ) -> Any:
        return self.runtime.get_json(self._url(path), params=params, headers=self._headers(require_jwt=require_jwt))

    def _request_json(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any],
        *,
        require_jwt: bool = False,
        versioned: bool = True,
    ) -> Any:
        headers = {"Content-Type": "application/json", **self._headers(require_jwt=require_jwt)}
        return self.runtime.request_json(
            method,
            self._url(path, versioned=versioned),
            json_body=dict(payload),
            headers=headers,
        )

    def _url(self, path: str, *, versioned: bool = True) -> str:
        clean_path = "/" + str(path or "").strip("/")
        if versioned:
            return f"{self.api_base_url}{clean_path}"
        root = self.api_base_url
        if root.endswith("/v1"):
            root = root[:-3]
        return f"{root}{clean_path}"

    def _headers(self, *, require_jwt: bool = False) -> Dict[str, str]:
        required = "api-testnet.predict.fun" not in self.api_base_url and "api-sepolia.predict.fun" not in self.api_base_url
        credential = self.resolve_credential(
            "predict_fun_api_key",
            ("PREDICT_FUN_API_KEY",),
            required=required,
            label="PREDICT_FUN_API_KEY",
        )
        headers = {"x-api-key": credential.value} if credential else {}
        jwt = self.resolve_credential(
            "predict_fun_jwt",
            ("PREDICT_FUN_JWT", "PREDICT_FUN_ACCESS_TOKEN"),
            required=require_jwt,
            label="PREDICT_FUN_JWT",
        )
        if jwt:
            token = str(jwt.value).strip()
            headers["Authorization"] = token if token.lower().startswith("bearer ") else f"Bearer {token}"
        return headers

    @staticmethod
    def _bounded_int(value: Any, label: str, *, minimum: int, maximum: int, default: int) -> int:
        if value in (None, ""):
            return default
        if isinstance(value, bool):
            raise MarketConfigurationError(f"Predict.fun {label} must be an integer.")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"Predict.fun {label} must be an integer.") from exc
        if parsed < minimum or parsed > maximum:
            raise MarketConfigurationError(f"Predict.fun {label} must be between {minimum} and {maximum}.")
        return parsed

    @classmethod
    def _page_query(cls, kwargs: Mapping[str, Any]) -> Dict[str, Any]:
        params: Dict[str, Any] = {"first": cls._bounded_int(kwargs.get("limit"), "limit", minimum=1, maximum=100, default=50)}
        cursor = str(kwargs.get("cursor") or kwargs.get("after") or "").strip()
        if cursor:
            params["after"] = cls._path_segment(cursor, "cursor")
        return params

    @classmethod
    def _orders_query(cls, kwargs: Mapping[str, Any]) -> Dict[str, Any]:
        params = cls._page_query(kwargs)
        status = str(kwargs.get("status") or "").strip()
        if status:
            statuses = cls._csv_tokens(status, "status", max_items=10)
            invalid = [item for item in statuses if item.upper() not in PREDICT_FUN_STATUS_VALUES]
            if invalid:
                raise MarketConfigurationError(f"Predict.fun order status is unsupported: {', '.join(invalid)}.")
            params["status"] = ",".join(item.upper() for item in statuses)
        market_id = str(kwargs.get("market_id") or "").strip()
        if market_id:
            params["marketId"] = cls._market_id_param(market_id)
        return params

    @classmethod
    def _positions_query(cls, kwargs: Mapping[str, Any]) -> Dict[str, Any]:
        params = cls._page_query(kwargs)
        market_id = str(kwargs.get("market_id") or "").strip()
        if market_id:
            params["marketId"] = cls._market_id_param(market_id)
        if kwargs.get("is_resolved") not in (None, ""):
            value = kwargs.get("is_resolved")
            if isinstance(value, bool):
                params["isResolved"] = str(value).lower()
            elif str(value).strip().lower() in {"true", "false"}:
                params["isResolved"] = str(value).strip().lower()
            else:
                raise MarketConfigurationError("Predict.fun is_resolved must be true or false.")
        sort = str(kwargs.get("sort") or "").strip().upper()
        if sort:
            if sort not in PREDICT_FUN_POSITION_SORT_VALUES:
                raise MarketConfigurationError(
                    "Predict.fun position sort must be one of: " + ", ".join(sorted(PREDICT_FUN_POSITION_SORT_VALUES)) + "."
                )
            params["sort"] = sort
        return params

    @staticmethod
    def _market_id_param(value: str) -> int:
        if not value.isdigit():
            raise MarketConfigurationError("Predict.fun market_id must be a positive integer.")
        parsed = int(value)
        if parsed < 1 or parsed > 2_147_483_647:
            raise MarketConfigurationError("Predict.fun market_id must fit in a positive int32.")
        return parsed

    @staticmethod
    def _path_segment(value: Any, label: str) -> str:
        text = str(value or "").strip()
        if not text or text in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", text):
            raise MarketConfigurationError(f"Predict.fun {label} is invalid or contains path separators.")
        return text

    @staticmethod
    def _wallet_address(value: Any) -> str:
        text = str(value or "").strip()
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", text):
            raise MarketConfigurationError("Predict.fun wallet/address must be a 20-byte 0x-prefixed address.")
        return text

    @classmethod
    def _csv_tokens(cls, value: Any, label: str, *, max_items: int) -> List[str]:
        if isinstance(value, (list, tuple)):
            raw_values = list(value)
        else:
            raw_values = str(value or "").split(",")
        tokens = [str(item).strip() for item in raw_values if str(item).strip()]
        if len(tokens) > max_items:
            raise MarketConfigurationError(f"Predict.fun {label} accepts at most {max_items} values.")
        for token in tokens:
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", token):
                raise MarketConfigurationError(f"Predict.fun {label} contains an invalid value.")
        return tokens

    @classmethod
    def _order_ids(cls, values: Any) -> List[str]:
        if isinstance(values, Mapping):
            values = values.get("ids")
        ids = cls._csv_tokens(values, "order ids", max_items=PREDICT_FUN_MAX_ORDER_BATCH)
        if not ids:
            raise MarketConfigurationError("Predict.fun remove_orders requires at least one order id.")
        if len(set(ids)) != len(ids):
            raise MarketConfigurationError("Predict.fun order ids must be unique.")
        return ids

    @classmethod
    def _order_hashes(cls, values: Any) -> List[str]:
        if isinstance(values, Mapping):
            values = values.get("hashes") or values.get("order_hashes")
        hashes = cls._csv_tokens(values, "order hashes", max_items=PREDICT_FUN_MAX_ORDER_BATCH)
        if not hashes:
            raise MarketConfigurationError("Predict.fun remove_orders_by_hash requires at least one order hash.")
        if len(set(hashes)) != len(hashes):
            raise MarketConfigurationError("Predict.fun order hashes must be unique.")
        return hashes

    @staticmethod
    def _timestamp_bound(value: Any, label: str) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"Predict.fun {label} must be numeric.") from exc
        if not math.isfinite(result) or result < 0:
            raise MarketConfigurationError(f"Predict.fun {label} must be a finite non-negative Unix timestamp.")
        return result

    @classmethod
    def _timestamp_value(cls, value: Any) -> Optional[float]:
        if isinstance(value, str):
            text = value.strip()
            if text:
                try:
                    value = float(text)
                except ValueError:
                    try:
                        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                    except ValueError:
                        return None
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    return parsed.timestamp()
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        # Predict.fun timeseries uses milliseconds in its API responses.
        return number / 1000.0 if number > 10_000_000_000 else number

    @staticmethod
    def _outcome_matches(value: Any, requested: str) -> bool:
        """Match a documented outcome object or label to a contract outcome."""

        requested_value = str(requested or "").strip().upper()
        if not requested_value:
            return False
        candidates: List[Any] = []
        if isinstance(value, Mapping):
            for key in ("name", "title", "label", "side", "outcome", "outcomeName", "id", "outcomeId", "onChainId", "indexSet"):
                if value.get(key) is not None:
                    candidates.append(value.get(key))
        else:
            candidates.append(value)
        for candidate in candidates:
            text = str(candidate).strip().upper()
            if text == requested_value:
                return True
            if requested_value == "YES" and text in {"Y", "TRUE"}:
                return True
            if requested_value == "NO" and text in {"N", "FALSE"}:
                return True
        return False

    def _event_from_market(self, market: Mapping[str, Any]) -> MarketEvent:
        market_id = self._market_id(market)
        return MarketEvent(
            market_id=self.market_id,
            event_id=market_id,
            title=str(market.get("title") or market.get("question") or market_id),
            url=self._market_url(market),
            status=self._status_from_market(market),
            raw=dict(market),
        )

    def _contracts_from_market(self, market: Mapping[str, Any]) -> List[MarketContract]:
        market_id = self._market_id(market)
        title = str(market.get("title") or market.get("question") or market_id)
        contracts: List[MarketContract] = []
        outcomes = self._outcomes_from_market(market)
        if not outcomes:
            outcomes = [{"name": "Yes", "side": "YES"}, {"name": "No", "side": "NO"}]
        for idx, outcome_payload in enumerate(outcomes):
            label = str(
                outcome_payload.get("name")
                or outcome_payload.get("title")
                or outcome_payload.get("label")
                or outcome_payload.get("side")
                or f"Outcome {idx + 1}"
            )
            outcome = "NO" if label.strip().lower() == "no" or str(outcome_payload.get("side")).upper() == "NO" else "YES"
            if len(outcomes) > 2:
                outcome = str(outcome_payload.get("id") or outcome_payload.get("outcomeId") or label).upper()
            contracts.append(
                MarketContract(
                    market_id=self.market_id,
                    contract_id=self._contract_id(market_id, outcome),
                    event_id=market_id,
                    title=f"{title} - {label}",
                    outcome=label,
                    url=self._market_url(market),
                    status=self._status_from_market(market),
                    raw={"market": dict(market), "outcome": dict(outcome_payload)},
                )
            )
        return contracts

    def _validate_order(self, order: PaperOrderRequest) -> None:
        self.ensure_order_market(order)
        self._split_contract_id(order.contract_id)
        if str(order.side or "").upper() not in {"BUY", "SELL"}:
            raise MarketConfigurationError("Predict.fun paper order side must be BUY or SELL.")
        if not self._is_positive_number(order.size):
            raise MarketConfigurationError("Predict.fun paper order size must be positive.")
        if order.limit_price is not None and self._safe_probability(order.limit_price) is None:
            raise MarketConfigurationError("Predict.fun paper order limit price must be between 0 and 1.")

    def _live_order_payload(self, order: PaperOrderRequest) -> Dict[str, Any]:
        existing = order.metadata.get("predict_fun_order_payload") or order.metadata.get("signed_order_payload")
        if isinstance(existing, Mapping):
            return dict(existing)
        signed_order = order.metadata.get("order") or order.metadata.get("signed_order")
        if not isinstance(signed_order, Mapping):
            raise MarketConfigurationError("Predict.fun live orders require order.metadata['order'] with a signed order.")
        data: Dict[str, Any] = {
            "order": dict(signed_order),
            "strategy": str(order.metadata.get("strategy") or "LIMIT"),
            "isFillOrKill": bool(order.metadata.get("is_fill_or_kill", False)),
        }
        if order.limit_price is not None:
            data["pricePerShare"] = str(order.limit_price)
        if "price_per_share" in order.metadata:
            data["pricePerShare"] = str(order.metadata["price_per_share"])
        if "slippage_bps" in order.metadata:
            data["slippageBps"] = str(order.metadata["slippage_bps"])
        elif "slippageBps" in order.metadata:
            data["slippageBps"] = str(order.metadata["slippageBps"])
        return {"data": data}

    def _validate_live_order_binding(
        self,
        order: PaperOrderRequest,
        payload: Mapping[str, Any],
    ) -> None:
        """Bind an externally signed Predict order to the reviewed order intent.

        Predict's EIP-712 order identifies the selected market outcome through
        ``tokenId`` and encodes side/quantity/price in the signed maker/taker
        amounts.  Those signed fields must agree with the outer request that
        passed the shared live-order preflight; the adapter never rewrites them
        because doing so would invalidate the external signature.
        """

        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise MarketConfigurationError("Predict.fun live payload requires a data object.")
        signed_order = data.get("order")
        if not isinstance(signed_order, Mapping):
            raise MarketConfigurationError("Predict.fun live payload requires data.order with a signed order.")

        strategy = str(data.get("strategy") or "").strip().upper()
        if strategy != "LIMIT":
            raise MarketConfigurationError(
                "Predict.fun live payload strategy must be LIMIT; market-order semantics are not bound by this adapter."
            )
        for field in ("isFillOrKill", "isMinAmountOut"):
            if not self._wire_default_false(data.get(field), field):
                raise MarketConfigurationError(
                    f"Predict.fun live payload {field} must remain false for a preflighted limit order."
                )
        slippage = data.get("slippageBps")
        if slippage not in (None, ""):
            try:
                slippage_number = Decimal(str(slippage))
            except (InvalidOperation, ValueError) as exc:
                raise MarketConfigurationError(
                    "Predict.fun live payload slippageBps must be numeric."
                ) from exc
            if not slippage_number.is_finite() or slippage_number != 0:
                raise MarketConfigurationError(
                    "Predict.fun live payload slippageBps must be zero for a preflighted limit order."
                )

        market_id, outcome = self._split_contract_id(order.contract_id)
        market = self._get_market(market_id)
        if self._market_id(market) != market_id:
            raise MarketConfigurationError(
                "Predict.fun signed order market does not match the requested contract."
            )
        for container in (payload, data, signed_order):
            explicit_market = container.get("marketId", container.get("market_id"))
            if explicit_market not in (None, "") and str(explicit_market).strip() != market_id:
                raise MarketConfigurationError(
                    "Predict.fun signed order market does not match the requested contract."
                )
        expected_token = self._outcome_token_id(market, outcome)
        signed_token = self._wire_uint(signed_order.get("tokenId"), "tokenId")
        if signed_token != expected_token:
            raise MarketConfigurationError(
                "Predict.fun signed order tokenId does not match the requested outcome."
            )
        signature = str(signed_order.get("signature") or "").strip()
        if not signature.startswith("0x") or len(signature) <= 2 or any(
            character not in "0123456789abcdefABCDEF" for character in signature[2:]
        ):
            raise MarketConfigurationError(
                "Predict.fun signed order requires a hexadecimal 0x-prefixed signature."
            )

        signed_side = self._wire_uint(signed_order.get("side"), "side")
        expected_side = 0 if str(order.side).upper() == "BUY" else 1
        if signed_side != expected_side:
            raise MarketConfigurationError(
                "Predict.fun signed order side does not match the requested side."
            )

        maker_amount = self._wire_uint(signed_order.get("makerAmount"), "makerAmount")
        taker_amount = self._wire_uint(signed_order.get("takerAmount"), "takerAmount")
        if maker_amount <= 0 or taker_amount <= 0:
            raise MarketConfigurationError("Predict.fun signed order amounts must be positive.")
        share_amount = taker_amount if signed_side == 0 else maker_amount
        signed_size = Decimal(share_amount) / PREDICT_FUN_ORDER_SCALE
        if signed_size != Decimal(str(order.size)):
            raise MarketConfigurationError(
                "Predict.fun signed order size does not match the requested size."
            )

        if order.limit_price is None:
            raise MarketConfigurationError(
                "Predict.fun live orders require a reviewed limit price to bind the signed price."
            )
        signed_price = (
            Decimal(maker_amount) / Decimal(taker_amount)
            if signed_side == 0
            else Decimal(taker_amount) / Decimal(maker_amount)
        )
        wire_price = self._wire_probability(data.get("pricePerShare"), "pricePerShare")
        requested_price = Decimal(str(order.limit_price))
        if not self._decimal_close(signed_price, requested_price) or not self._decimal_close(
            wire_price, requested_price
        ):
            raise MarketConfigurationError(
                "Predict.fun signed order price does not match the requested limit price."
            )

    @classmethod
    def _outcome_token_id(cls, market: Mapping[str, Any], requested: str) -> int:
        for outcome in cls._outcomes_from_market(market):
            if not cls._outcome_matches(outcome, requested):
                continue
            token = outcome.get("onChainId")
            if token in (None, ""):
                break
            return cls._wire_uint(token, "outcome onChainId")
        raise MarketConfigurationError(
            "Predict.fun market did not expose an on-chain token for the requested outcome."
        )

    @staticmethod
    def _wire_uint(value: Any, label: str) -> int:
        text = str(value if value is not None else "").strip()
        if isinstance(value, bool) or not text.isdigit():
            raise MarketConfigurationError(f"Predict.fun signed order {label} must be an unsigned integer.")
        return int(text)

    @staticmethod
    def _wire_probability(value: Any, label: str) -> Decimal:
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise MarketConfigurationError(f"Predict.fun live payload {label} must be numeric.") from exc
        if not number.is_finite() or number <= 0:
            raise MarketConfigurationError(f"Predict.fun live payload {label} must be positive and finite.")
        if number > 1:
            number /= PREDICT_FUN_ORDER_SCALE
        if number > 1:
            raise MarketConfigurationError(f"Predict.fun live payload {label} must represent a probability.")
        return number

    @staticmethod
    def _wire_default_false(value: Any, label: str) -> bool:
        if value in (None, "", False, 0, "0"):
            return True
        if isinstance(value, str) and value.strip().lower() == "false":
            return True
        if value in (True, 1, "1") or (
            isinstance(value, str) and value.strip().lower() == "true"
        ):
            return False
        raise MarketConfigurationError(
            f"Predict.fun live payload {label} must be a boolean."
        )

    @staticmethod
    def _decimal_close(left: Decimal, right: Decimal) -> bool:
        return abs(left - right) <= Decimal("1e-12")

    @staticmethod
    def _market_id(market: Mapping[str, Any]) -> str:
        return str(market.get("id") or market.get("marketId") or "").strip()

    @staticmethod
    def _contract_id(market_id: str, outcome: str) -> str:
        return f"{market_id}:{outcome.upper()}"

    @staticmethod
    def _split_contract_id(contract_id: str) -> Tuple[str, str]:
        raw = str(contract_id or "").strip()
        if ":" in raw:
            market_id, outcome = raw.rsplit(":", 1)
        else:
            market_id, outcome = raw, "YES"
        if not market_id.strip() or not outcome.strip():
            raise MarketConfigurationError("Predict.fun contract id must be MARKET_ID:OUTCOME.")
        return market_id.strip(), outcome.strip().upper()

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
    def _outcomes_from_market(market: Mapping[str, Any]) -> List[Mapping[str, Any]]:
        outcomes = market.get("outcomes")
        return [outcome for outcome in outcomes if isinstance(outcome, Mapping)] if isinstance(outcomes, list) else []

    @staticmethod
    def _status_from_market(market: Mapping[str, Any]) -> str:
        raw = market.get("tradingStatus") or market.get("status") or ""
        if isinstance(raw, Mapping):
            raw = raw.get("name") or raw.get("status") or raw.get("value") or raw.get("label") or ""
        return str(raw).strip().lower()

    @staticmethod
    def _market_url(market: Mapping[str, Any]) -> str:
        raw = str(market.get("url") or "").strip()
        if raw:
            return raw
        market_id = PredictFunAdapter._market_id(market)
        return f"https://predict.fun/markets/{market_id}" if market_id else "https://predict.fun"

    @staticmethod
    def _search_text(market: Mapping[str, Any]) -> str:
        values = [market.get("id"), market.get("title"), market.get("question"), market.get("description"), market.get("categorySlug")]
        return " ".join(str(value or "") for value in values).lower()

    @staticmethod
    def _price_from_market(market: Mapping[str, Any], outcome: str) -> Optional[float]:
        outcomes = PredictFunAdapter._outcomes_from_market(market)
        if outcome == "NO" and len(outcomes) >= 2:
            return PredictFunAdapter._safe_probability(outcomes[1].get("price") or outcomes[1].get("probability"))
        if outcomes:
            return PredictFunAdapter._safe_probability(outcomes[0].get("price") or outcomes[0].get("probability"))
        return None

    @staticmethod
    def _levels(raw: Any, *, descending: bool = False) -> List[OrderBookLevel]:
        levels: List[OrderBookLevel] = []
        if not isinstance(raw, list):
            return levels
        for item in raw:
            price = size = None
            if isinstance(item, Mapping):
                price = item.get("price")
                size = item.get("size") or item.get("shares") or item.get("amount")
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                price, size = item[0], item[1]
            parsed_price = PredictFunAdapter._safe_probability(price)
            try:
                parsed_size = float(size)
            except (TypeError, ValueError):
                continue
            if parsed_price is not None and PredictFunAdapter._is_positive_number(parsed_size):
                levels.append(OrderBookLevel(price=parsed_price, size=parsed_size))
        levels.sort(key=lambda level: level.price, reverse=descending)
        return levels

    @staticmethod
    def _opposite_bids_from_yes_asks(levels: List[OrderBookLevel]) -> List[OrderBookLevel]:
        bids = [OrderBookLevel(price=round(1.0 - level.price, 10), size=level.size) for level in levels]
        bids.sort(key=lambda level: level.price, reverse=True)
        return bids

    @staticmethod
    def _opposite_asks_from_yes_bids(levels: List[OrderBookLevel]) -> List[OrderBookLevel]:
        asks = [OrderBookLevel(price=round(1.0 - level.price, 10), size=level.size) for level in levels]
        asks.sort(key=lambda level: level.price)
        return asks

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
