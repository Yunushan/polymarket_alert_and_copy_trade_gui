from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .base import MarketAdapter
from .catalog import get_market_metadata
from .errors import MarketConfigurationError, MarketHTTPError
from .identity import require_activity_identity
from .types import (
    MarketContract,
    MarketEvent,
    MarketCandle,
    MarketTrade,
    OrderBookLevel,
    OrderBookSnapshot,
    PaperOrderRequest,
    PaperOrderResult,
    PriceSnapshot,
)


DEFAULT_CONTEXT_API_BASE_URL = "https://api.context.markets/v2"
CONTEXT_REFERENCES = (
    "https://docs.context.markets/developers/guides/api-keys",
    "https://docs.context.markets/api-reference/markets/list-markets",
    "https://docs.context.markets/api-reference/markets/get-market-activity",
    "https://docs.context.markets/api-reference/markets/get-market-price-history",
    "https://docs.context.markets/api-reference/orders/create-order",
    "https://docs.context.markets/api-reference/orders/cancel-order",
    "https://github.com/contextwtf/context-sdk/blob/main/skills/api-reference.md",
    "https://docs.context.markets/agents/react-sdk/index",
)


class ContextV2Adapter(MarketAdapter):
    """Context Markets v2 REST adapter with a signed-order boundary.

    Context separates API-key authentication from wallet signing.  The adapter
    therefore accepts a complete, externally signed order payload for live
    submission and never handles a private key.  Read and paper operations are
    deterministic and fixture-friendly; live execution remains disabled by the
    shared safety gate unless the operator explicitly enables it.
    """

    metadata = get_market_metadata("context_v2")
    live_order_sides = ("BUY", "SELL")
    account_recovery_operations = ("orders",)

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        credential = self.resolve_credential(
            "context_api_key", ("CONTEXT_API_KEY",), label="CONTEXT_API_KEY"
        )
        health.update(
            {
                "api_base_url": self.api_base_url,
                "api_key_configured": bool(credential),
                "api_key_source": credential.source if credential else "missing",
                "chain": str(self.config.get("context_chain") or "mainnet"),
                "references": list(CONTEXT_REFERENCES),
                "live_trading_supported": True,
                "live_trading_enabled": self.config_bool("live_trading_enabled", False),
                "signed_order_required": True,
                "private_key_handling": "external_wallet_only",
            }
        )
        return health

    @property
    def api_base_url(self) -> str:
        configured = self.config.get("context_api_base_url") or self.config.get("api_base_url")
        return str(configured or DEFAULT_CONTEXT_API_BASE_URL).rstrip("/")

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = max(1, min(int(limit or 50), 50))
        params: Dict[str, Any] = {"limit": desired}
        status = str(self.config.get("context_market_status") or "active").strip()
        if status:
            params["status"] = status
        if str(query or "").strip():
            params["search"] = str(query).strip()
        payload = self._get("/markets", params=params)
        markets = self._rows(payload, "markets", "data")
        needle = str(query or "").strip().lower()
        if needle:
            markets = [market for market in markets if needle in self._search_text(market)]
        return [self._event_from_market(market) for market in markets[:desired]]

    def list_activity(self, wallet_address: str, *, limit: int = 25) -> List[Dict[str, Any]]:
        """Return normalized filled Context orders for a wallet.

        The official Context SDK documents ``orders.list`` as a read-only
        order feed that accepts ``trader``, ``status``, and ``limit`` filters.
        Only fully filled orders are admitted to the copy workflow.  The
        upstream order wire format uses 1e6-scaled integer price/size fields;
        malformed, partial, cross-wallet, or non-binary rows fail closed.
        """

        self.ensure_capability("copy_trading")
        wallet = require_activity_identity(self.market_id, wallet_address)
        desired = self._bounded_limit(limit, maximum=100, label="Context activity limit")
        payload = self._get(
            "/orders",
            params={"trader": wallet, "status": "filled", "limit": desired},
        )
        return self._normalize_order_activity(wallet, payload, desired)

    def account_recovery(self, operation: str, **kwargs: Any) -> Dict[str, Any]:
        """Read Context's documented, wallet-filtered order history."""

        normalized = str(operation or "").strip().lower()
        if normalized not in self.account_recovery_operations:
            raise MarketConfigurationError(
                "Context account operation must be one of: "
                + ", ".join(self.account_recovery_operations)
                + "."
            )
        wallet = require_activity_identity(
            self.market_id,
            kwargs.get("wallet") or kwargs.get("trader") or kwargs.get("address"),
        )
        desired = self._bounded_limit(kwargs.get("limit", 100), maximum=100, label="Context order limit")
        status = str(kwargs.get("status") or "filled").strip().lower()
        if status not in {"open", "filled", "cancelled", "expired", "voided"}:
            raise MarketConfigurationError(
                "Context order status must be one of open, filled, cancelled, expired, or voided."
            )
        params: Dict[str, Any] = {"trader": wallet, "status": status, "limit": desired}
        market_id = str(kwargs.get("market_id") or kwargs.get("marketId") or "").strip()
        if market_id:
            params["marketId"] = self._required_id(market_id, "market")
        payload = self._get("/orders", params=params)
        return {
            "market_id": self.market_id,
            "operation": normalized,
            "source": "context_orders",
            "endpoint": "/orders",
            "wallet": wallet,
            "parameters": params,
            "orders": [dict(row) for row in self._rows(payload, "orders", "data")],
            "raw": payload,
        }

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        market_id = self._required_id(event_id, "market")
        market = self._get_market(market_id)
        return self._contracts_from_market(market)

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        market_id, outcome_index = self._split_contract_id(contract_id)
        market = self._get_market(market_id)
        row = self._price_row(market, outcome_index)
        bid = self._probability(self._value(row, "bestBid", "best_bid", "buyPrice", "buy_price"))
        ask = self._probability(self._value(row, "bestAsk", "best_ask", "sellPrice", "sell_price"))
        last = self._probability(self._value(row, "lastPrice", "last_price"))
        midpoint = self._probability(self._value(row, "midPrice", "mid_price"))
        if midpoint is None and bid is not None and ask is not None:
            midpoint = (bid + ask) / 2.0
        if last is None:
            last = midpoint or bid or ask
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=self._contract_id(market_id, outcome_index),
            last=last,
            bid=bid,
            ask=ask,
            midpoint=midpoint,
            source="context_markets_v2",
            raw={"market": dict(market), "outcome_index": outcome_index, "price": dict(row)},
        )

    def get_orderbook(self, contract_id: str) -> OrderBookSnapshot:
        self.ensure_capability("orderbook_reading")
        market_id, outcome_index = self._split_contract_id(contract_id)
        payload = self._get(f"/markets/{market_id}/orderbook")
        book = self._orderbook_for_outcome(payload, outcome_index)
        bids = self._levels(book.get("bids"), descending=True)
        asks = self._levels(book.get("asks"), descending=False)
        if not bids and not asks:
            # Some responses expose only the quote summary.  Preserve a useful
            # one-level snapshot rather than silently returning an empty book.
            market = self._get_market(market_id)
            row = self._price_row(market, outcome_index)
            bid = self._probability(self._value(row, "bestBid", "best_bid", "buyPrice", "buy_price"))
            ask = self._probability(self._value(row, "bestAsk", "best_ask", "sellPrice", "sell_price"))
            if bid is not None:
                bids = [OrderBookLevel(price=bid, size=0.0)]
            if ask is not None:
                asks = [OrderBookLevel(price=ask, size=0.0)]
        return OrderBookSnapshot(
            market_id=self.market_id,
            contract_id=self._contract_id(market_id, outcome_index),
            bids=bids,
            asks=asks,
            raw={"orderbook": self._mapping_payload(payload), "outcome_index": outcome_index},
        )

    def list_trades(
        self,
        contract_id: str,
        *,
        limit: int = 50,
        before: Optional[float] = None,
        after: Optional[float] = None,
    ) -> List[MarketTrade]:
        """Return Context's documented market activity trade feed.

        Context's activity schema reports the traded outcome (``yes``/``no``)
        in ``data.side`` rather than a BUY/SELL direction.  We preserve that
        upstream meaning in the normalized ``side`` field instead of guessing
        an order direction that the feed does not publish.  The API supports
        ISO ``startTime``/``endTime`` filters; the shared numeric bounds are
        also applied locally so fixture and live behavior stay identical.
        """

        self.ensure_capability("trade_history")
        market_id, outcome_index = self._split_contract_id(contract_id)
        desired = self._bounded_limit(limit, maximum=500, label="Context trade limit")
        before_ts = self._history_timestamp(before, "before") if before is not None else None
        after_ts = self._history_timestamp(after, "after") if after is not None else None
        if before_ts is not None and after_ts is not None and before_ts < after_ts:
            raise MarketConfigurationError("Context trade history requires before to be at or after after.")

        params: Dict[str, Any] = {"limit": desired, "types": "trade"}
        if after_ts is not None:
            params["startTime"] = self._iso_timestamp(after_ts)
        if before_ts is not None:
            params["endTime"] = self._iso_timestamp(before_ts)
        payload = self._get(f"/markets/{market_id}/activity", params=params)
        rows = self._rows(payload, "activity", "data")
        canonical = self._contract_id(market_id, outcome_index)
        trades: List[MarketTrade] = []
        for index, row in enumerate(rows):
            if str(row.get("type") or "").strip().lower() != "trade":
                continue
            row_market = str(row.get("marketId") or row.get("market_id") or market_id).strip()
            if row_market and row_market != market_id:
                continue
            data = row.get("data") if isinstance(row.get("data"), Mapping) else row
            outcome = str(
                self._value(data, "outcome", "outcomeName", "outcome_name", "side") or ""
            ).strip()
            if outcome and not self._outcome_matches(outcome, outcome_index):
                continue
            price = self._probability(self._value(data, "price", "probability"))
            size = self._positive_number(self._value(data, "contracts", "size", "quantity"))
            timestamp = self._optional_timestamp(row.get("timestamp") or data.get("timestamp"))
            if price is None or size is None or timestamp is None:
                continue
            if before_ts is not None and timestamp > before_ts:
                continue
            if after_ts is not None and timestamp < after_ts:
                continue
            trade_id = str(
                row.get("id")
                or row.get("tradeId")
                or row.get("trade_id")
                or row.get("txHash")
                or row.get("hash")
                or f"context:{market_id}:{int(timestamp * 1000)}:{index}"
            ).strip()
            trades.append(
                MarketTrade(
                    market_id=self.market_id,
                    contract_id=canonical,
                    trade_id=trade_id,
                    side=outcome.upper() if outcome else "TRADE",
                    price=price,
                    size=size,
                    timestamp=timestamp,
                    raw=dict(row),
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
        """Return Context's documented point price history as flat candles.

        The Context API returns one binary-market price series.  Outcome 0 is
        the published series and outcome 1 is its documented complement.  No
        volume or synthetic OHLC movement is fabricated: each point is kept as
        a flat OHLC snapshot with the upstream row in ``raw``.
        """

        self.ensure_capability("candle_history")
        market_id, outcome_index = self._split_contract_id(contract_id)
        if outcome_index not in (0, 1):
            raise MarketConfigurationError("Context price history supports only YES (0) and NO (1) outcomes.")
        clean_resolution = str(resolution or "").strip()
        resolution_map = {"1h": "1h", "6h": "6h", "1d": "1d", "1w": "1w", "1m": "1M", "1M": "1M", "all": "all"}
        if clean_resolution not in resolution_map:
            raise MarketConfigurationError("Context price history resolution must be one of 1h, 6h, 1d, 1w, 1M, or all.")
        start_ts = self._history_timestamp(from_timestamp, "from_timestamp") if from_timestamp is not None else None
        end_ts = self._history_timestamp(to_timestamp, "to_timestamp") if to_timestamp is not None else None
        if start_ts is not None and end_ts is not None and end_ts <= start_ts:
            raise MarketConfigurationError("Context price history requires to_timestamp greater than from_timestamp.")

        payload = self._get(f"/markets/{market_id}/prices", params={"timeframe": resolution_map[clean_resolution]})
        rows = self._rows(payload, "prices", "history", "data")
        if not rows and isinstance(payload, Mapping):
            raw_prices = payload.get("prices")
            if isinstance(raw_prices, list):
                rows = [row for row in raw_prices if isinstance(row, Mapping)]
        canonical = self._contract_id(market_id, outcome_index)
        candles: List[MarketCandle] = []
        for row in rows:
            timestamp = self._optional_timestamp(self._value(row, "time", "timestamp", "ts"))
            price = self._probability(self._value(row, "price", "probability"))
            if timestamp is None or price is None:
                continue
            if outcome_index == 1:
                price = 1.0 - price
            if start_ts is not None and timestamp < start_ts:
                continue
            if end_ts is not None and timestamp > end_ts:
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
        return candles

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        market_id, outcome_index = self._validate_order(order)
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=self._contract_id(market_id, outcome_index),
            accepted=True,
            message=(
                f"DRY RUN: would place Context {str(order.side).upper()} "
                f"for {float(order.size):.4f} shares"
                + (f" at probability {float(order.limit_price):.4f}" if order.limit_price is not None else "")
            ),
            average_price=order.limit_price,
            raw={"request": self._order_payload(order, signed=False), "dry_run": True},
        )

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        self.ensure_capability("live_trading")
        market_id, outcome_index = self._validate_order(order)
        preflight = self.preflight_live_order(order)
        payload = self._order_payload(order, signed=True)
        response = self._request_json("POST", "/orders", payload, auth=True)
        return {
            "market_id": self.market_id,
            "contract_id": self._contract_id(market_id, outcome_index),
            "live": True,
            "preflight": preflight,
            "request": payload,
            "response": response,
        }

    def copy_trade_from_activity(self, activity: Mapping[str, Any]) -> PaperOrderResult:
        """Build a simulation-first paper order from a filled Context order."""

        self.ensure_capability("copy_trading")
        contract_id = str(activity.get("asset") or activity.get("contract_id") or "").strip()
        if not contract_id:
            raise MarketConfigurationError("Context activity has no market/outcome contract id.")
        market_id, outcome_index = self._split_contract_id(contract_id)
        activity_market = str(activity.get("marketId") or activity.get("market_id") or "").strip()
        if activity_market and not self._identifiers_match(activity_market, market_id):
            raise MarketConfigurationError("Context activity market id does not match its contract id.")
        if activity.get("outcomeIndex") not in (None, ""):
            if self._wire_integer(activity.get("outcomeIndex"), "activity outcomeIndex") != outcome_index:
                raise MarketConfigurationError("Context activity outcome index does not match its contract id.")
        status = str(activity.get("status") or "filled").strip().lower()
        if status != "filled":
            raise MarketConfigurationError("Context copy activity must have filled status.")
        side = str(activity.get("side") or "").strip().upper()
        if side not in self.live_order_sides:
            raise MarketConfigurationError("Context activity side must be BUY or SELL.")
        size = self._positive_number(activity.get("size"))
        if size is None:
            raise MarketConfigurationError("Context activity size must be positive and finite.")
        raw_price = activity.get("price")
        limit_price = None if raw_price in (None, "") else self._probability(raw_price)
        if raw_price not in (None, "") and limit_price is None:
            raise MarketConfigurationError("Context activity reference price must be between 0 and 1.")
        return self.place_paper_order(
            PaperOrderRequest(
                market_id=self.market_id,
                contract_id=self._contract_id(market_id, outcome_index),
                side=side,
                size=size,
                limit_price=limit_price,
                metadata={"activity": dict(activity), "source": "context_filled_order_feed"},
            )
        )

    @classmethod
    def _normalize_order_activity(
        cls,
        wallet: str,
        payload: Any,
        limit: int,
    ) -> List[Dict[str, Any]]:
        rows = cls._rows(payload, "orders", "data")
        normalized: List[Dict[str, Any]] = []
        for row in rows:
            status = str(row.get("status") or "").strip().lower()
            if status != "filled":
                continue
            row_wallet = str(row.get("trader") or row.get("wallet") or "").strip()
            if not row_wallet or not cls._identifiers_match(row_wallet, wallet):
                continue
            market_id = str(row.get("marketId") or row.get("market_id") or "").strip()
            if not market_id:
                continue
            try:
                outcome_index = cls._wire_integer(row.get("outcomeIndex"), "order outcomeIndex")
                side_index = cls._wire_integer(row.get("side"), "order side")
            except MarketConfigurationError:
                continue
            if outcome_index not in (0, 1) or side_index not in (0, 1):
                continue
            price = cls._probability(row.get("price"))
            size = cls._wire_size(row.get("filledSize"))
            timestamp = cls._optional_timestamp(
                row.get("insertedAt") or row.get("filledAt") or row.get("timestamp")
            )
            nonce = str(row.get("nonce") or row.get("orderId") or row.get("id") or "").strip()
            if price is None or size is None or timestamp is None or not nonce:
                continue
            canonical = cls._contract_id(market_id, outcome_index)
            normalized.append(
                {
                    "type": "TRADE",
                    "activityType": "TRADE",
                    "proxyWallet": wallet,
                    "wallet": wallet,
                    "asset": canonical,
                    "contract_id": canonical,
                    "marketId": market_id,
                    "outcomeIndex": outcome_index,
                    "outcome": "Yes" if outcome_index == 0 else "No",
                    "side": "BUY" if side_index == 0 else "SELL",
                    "size": size,
                    "price": price,
                    "timestamp": timestamp,
                    "transactionHash": str(
                        row.get("transactionHash") or row.get("txHash") or ""
                    ).strip(),
                    "activityId": f"context:{market_id}:{nonce}",
                    "orderId": nonce,
                    "status": status,
                    "source": "context_filled_order_feed",
                    "raw": dict(row),
                }
            )
            if len(normalized) >= limit:
                break
        return normalized

    def _get_market(self, market_id: str) -> Mapping[str, Any]:
        payload = self._get(f"/markets/{market_id}")
        market = self._mapping_payload(payload)
        if isinstance(market.get("market"), Mapping):
            market = dict(market["market"])
        if not market:
            raise MarketConfigurationError(f"Context market {market_id!r} was not found.")
        return market

    def _get(self, path: str, *, params: Optional[Mapping[str, Any]] = None) -> Any:
        return self.runtime.get_json(
            self._url(self.api_base_url, path),
            params=params,
            headers=self._headers(required=True),
        )

    def _request_json(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any],
        *,
        auth: bool = False,
    ) -> Any:
        self.runtime.rate_limiter.wait()
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if auth:
            headers.update(self._headers(required=True))
        try:
            response = self.runtime.session.request(
                method.upper(),
                self._url(self.api_base_url, path),
                json=dict(body),
                headers={"User-Agent": self.runtime.user_agent, **headers},
                timeout=self.runtime.timeout_seconds,
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
            "context_api_key", ("CONTEXT_API_KEY",), required=required, label="CONTEXT_API_KEY"
        )
        return {"Authorization": f"Bearer {credential.value}"} if credential else {}

    @staticmethod
    def _wire_size(value: Any) -> Optional[float]:
        """Decode Context's 1e6-scaled filled-size field."""

        try:
            number = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return None
        if not number.is_finite() or number <= 0:
            return None
        normalized = number / Decimal(1_000_000)
        try:
            result = float(normalized)
        except (OverflowError, ValueError):
            return None
        return result if math.isfinite(result) and result > 0 else None

    def _validate_order(self, order: PaperOrderRequest) -> Tuple[str, int]:
        self.ensure_order_market(order)
        market_id, outcome_index = self._split_contract_id(order.contract_id)
        side = str(order.side or "").upper()
        if side not in self.live_order_sides:
            raise MarketConfigurationError("Context order side must be BUY or SELL.")
        try:
            size = float(order.size)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Context order size must be numeric.") from exc
        if not math.isfinite(size) or size <= 0:
            raise MarketConfigurationError("Context order size must be positive and finite.")
        if order.limit_price is not None and self._probability(order.limit_price) is None:
            raise MarketConfigurationError("Context order limit price must be between 0 and 1.")
        return market_id, outcome_index

    def _order_payload(self, order: PaperOrderRequest, *, signed: bool) -> Dict[str, Any]:
        existing = order.metadata.get("context_order") or order.metadata.get("signed_order")
        if signed and not isinstance(existing, Mapping):
            raise MarketConfigurationError(
                "Context live orders require order.metadata['context_order'] or ['signed_order'] with a wallet signature."
            )
        market_id, outcome_index = self._split_contract_id(order.contract_id)
        payload: Dict[str, Any] = dict(existing) if isinstance(existing, Mapping) else {}
        price = self._probability(order.limit_price)
        if price is None:
            price = 0.5
        size = float(order.size)
        expected_side = 0 if str(order.side).upper() == "BUY" else 1
        expected_price = round(price * 1_000_000)
        expected_size = round(size * 1_000_000)
        if signed:
            self._validate_signed_order_binding(
                payload,
                market_id=market_id,
                outcome_index=outcome_index,
                side=expected_side,
                price=expected_price,
                size=expected_size,
            )
        payload.setdefault("type", "limit")
        payload.setdefault("marketId", market_id)
        payload.setdefault("outcomeIndex", outcome_index)
        payload.setdefault("side", expected_side)
        payload.setdefault("price", str(expected_price))
        payload.setdefault("size", str(expected_size))
        payload.setdefault("expiry", "0")
        payload.setdefault("maxFee", "0")
        payload.setdefault("makerRoleConstraint", 0)
        payload.setdefault("inventoryModeConstraint", 0)
        payload.setdefault("nonce", str(order.metadata.get("nonce") or "0x0"))
        if signed:
            trader = str(payload.get("trader") or order.metadata.get("trader") or "").strip()
            signature = str(payload.get("signature") or order.metadata.get("signature") or "").strip()
            if not trader or not signature:
                raise MarketConfigurationError(
                    "Context live orders require signed payload fields 'trader' and 'signature'."
                )
            cls_trader = trader[2:] if trader.startswith("0x") else ""
            if len(cls_trader) != 40 or any(
                character not in "0123456789abcdefABCDEF" for character in cls_trader
            ):
                raise MarketConfigurationError(
                    "Context signed order trader must be a 20-byte 0x-prefixed address."
                )
            self._validate_hex_signature(signature)
            payload["trader"] = trader
            payload["signature"] = signature
        return payload

    @classmethod
    def _validate_signed_order_binding(
        cls,
        payload: Mapping[str, Any],
        *,
        market_id: str,
        outcome_index: int,
        side: int,
        price: int,
        size: int,
    ) -> None:
        """Reject a signature whose economic fields differ from preflight."""

        order_type = payload.get("type")
        if order_type not in (None, "") and str(order_type).strip().lower() != "limit":
            raise MarketConfigurationError(
                "Context signed order type does not match the preflighted limit order."
            )
        required_fields = ("marketId", "outcomeIndex", "side", "price", "size")
        missing = [field for field in required_fields if payload.get(field) in (None, "")]
        if missing:
            raise MarketConfigurationError(
                "Context signed order is missing preflight-bound fields: " + ", ".join(missing) + "."
            )
        if not cls._identifiers_match(payload["marketId"], market_id):
            raise MarketConfigurationError(
                "Context signed order marketId does not match the preflighted contract."
            )
        for field, expected, label in (
            ("outcomeIndex", outcome_index, "outcomeIndex"),
            ("side", side, "side"),
            ("price", price, "price"),
            ("size", size, "size"),
        ):
            if cls._wire_integer(payload[field], f"signed order {label}") != expected:
                raise MarketConfigurationError(
                    f"Context signed order {label} does not match the preflighted order."
                )

    @staticmethod
    def _validate_hex_signature(value: Any) -> None:
        signature = str(value or "").strip()
        body = signature[2:] if signature.startswith("0x") else ""
        if not body or len(body) % 2 or any(
            character not in "0123456789abcdefABCDEF" for character in body
        ):
            raise MarketConfigurationError(
                "Context signed order signature must be an even-length 0x-prefixed hexadecimal value."
            )

    @staticmethod
    def _identifiers_match(value: Any, expected: str) -> bool:
        actual = str(value or "").strip()
        canonical = str(expected or "").strip()
        if actual.lower().startswith("0x") and canonical.lower().startswith("0x"):
            return actual.lower() == canonical.lower()
        return actual == canonical

    @staticmethod
    def _wire_integer(value: Any, label: str) -> int:
        if isinstance(value, bool):
            raise MarketConfigurationError(f"Context {label} must be an integer.")
        try:
            number = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"Context {label} must be an integer.") from exc
        if not number.is_finite() or number != number.to_integral_value():
            raise MarketConfigurationError(f"Context {label} must be an integer.")
        return int(number)

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
    def _history_timestamp(value: Any, label: str) -> float:
        try:
            timestamp = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"Context {label} must be a finite Unix timestamp.") from exc
        if not math.isfinite(timestamp) or timestamp < 0:
            raise MarketConfigurationError(f"Context {label} must be a finite, non-negative Unix timestamp.")
        return timestamp

    @classmethod
    def _optional_timestamp(cls, value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)):
            try:
                timestamp = float(value)
            except (TypeError, ValueError):
                return None
            if timestamp > 100_000_000_000:
                timestamp /= 1000.0
            return timestamp if math.isfinite(timestamp) and timestamp >= 0 else None
        text = str(value).strip()
        try:
            timestamp = datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError, OverflowError):
            return None
        return timestamp if math.isfinite(timestamp) and timestamp >= 0 else None

    @staticmethod
    def _iso_timestamp(timestamp: float) -> str:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _positive_number(value: Any) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and number > 0 else None

    @staticmethod
    def _outcome_matches(value: str, outcome_index: int) -> bool:
        clean = str(value or "").strip().lower()
        if clean in {"yes", "y", "0"}:
            return outcome_index == 0
        if clean in {"no", "n", "1"}:
            return outcome_index == 1
        try:
            return int(clean) == outcome_index
        except (TypeError, ValueError):
            return True

    def _event_from_market(self, market: Mapping[str, Any]) -> MarketEvent:
        market_id = self._id(market)
        metadata = market.get("metadata") if isinstance(market.get("metadata"), Mapping) else {}
        slug = self._value(metadata, "slug") or self._value(market, "slug")
        return MarketEvent(
            market_id=self.market_id,
            event_id=market_id,
            title=str(self._value(market, "question", "shortQuestion", "title") or market_id),
            url=str(slug or market_id),
            status=str(self._value(market, "status", "resolutionStatus") or "").lower(),
            raw=dict(market),
        )

    def _contracts_from_market(self, market: Mapping[str, Any]) -> List[MarketContract]:
        market_id = self._id(market)
        title = str(self._value(market, "question", "shortQuestion", "title") or market_id)
        outcome_tokens = market.get("outcomeTokens") or market.get("outcome_tokens") or []
        outcomes = market.get("outcomes") or []
        if not isinstance(outcome_tokens, list):
            outcome_tokens = []
        if not isinstance(outcomes, list):
            outcomes = []
        contracts: List[MarketContract] = []
        count = max(len(outcome_tokens), len(outcomes), len(market.get("outcomePrices") or []))
        for index in range(count):
            token = str(outcome_tokens[index]) if index < len(outcome_tokens) else str(index)
            outcome = str(outcomes[index]) if index < len(outcomes) else f"Outcome {index}"
            contracts.append(
                MarketContract(
                    market_id=self.market_id,
                    contract_id=self._contract_id(market_id, index),
                    event_id=market_id,
                    title=f"{title} - {outcome}",
                    outcome=outcome,
                    url=str(self._value(market, "url", "slug") or market_id),
                    status=str(self._value(market, "status", "resolutionStatus") or "").lower(),
                    raw={"market": dict(market), "outcome_index": index, "outcome_token": token},
                )
            )
        return contracts

    @staticmethod
    def _price_row(market: Mapping[str, Any], outcome_index: int) -> Mapping[str, Any]:
        prices = market.get("outcomePrices") or market.get("outcome_prices") or []
        if not isinstance(prices, list):
            return {}
        for row in prices:
            if isinstance(row, Mapping):
                candidate = row.get("outcomeIndex", row.get("outcome_index", -1))
                try:
                    if int(candidate) == outcome_index:
                        return row
                except (TypeError, ValueError):
                    pass
        return prices[outcome_index] if outcome_index < len(prices) and isinstance(prices[outcome_index], Mapping) else {}

    @classmethod
    def _orderbook_for_outcome(cls, payload: Any, outcome_index: int) -> Mapping[str, Any]:
        book = cls._mapping_payload(payload)
        outcomes = book.get("outcomes") or book.get("orderbooks")
        if isinstance(outcomes, list):
            for row in outcomes:
                if isinstance(row, Mapping):
                    candidate = row.get("outcomeIndex", row.get("outcome_index", -1))
                    try:
                        if int(candidate) == outcome_index:
                            return row
                    except (TypeError, ValueError):
                        pass
        if isinstance(outcomes, Mapping):
            row = outcomes.get(str(outcome_index))
            if isinstance(row, Mapping):
                return row
        return book

    @classmethod
    def _levels(cls, value: Any, *, descending: bool) -> List[OrderBookLevel]:
        if not isinstance(value, list):
            return []
        levels: List[OrderBookLevel] = []
        for row in value:
            if isinstance(row, Mapping):
                price_value = cls._value(row, "price", "p")
                size_value = cls._value(row, "size", "quantity", "amount", "q")
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
        levels.sort(key=lambda level: level.price, reverse=descending)
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
        return []

    @staticmethod
    def _value(payload: Mapping[str, Any], *keys: str) -> Any:
        for key in keys:
            value = payload.get(key)
            if value not in (None, ""):
                return value
        return None

    @classmethod
    def _search_text(cls, market: Mapping[str, Any]) -> str:
        metadata = market.get("metadata") if isinstance(market.get("metadata"), Mapping) else {}
        values = [cls._value(market, "id", "question", "shortQuestion", "title", "status"), cls._value(metadata, "slug", "shortSummary")]
        return " ".join(str(value or "") for value in values).lower()

    @classmethod
    def _probability(cls, value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        if number > 1.0 and number <= 1_000_000.0:
            number /= 1_000_000.0
        return number if 0.0 <= number <= 1.0 else None

    @staticmethod
    def _id(payload: Mapping[str, Any]) -> str:
        return str(payload.get("id") or payload.get("marketId") or "").strip()

    @staticmethod
    def _required_id(value: Any, label: str) -> str:
        clean = str(value or "").strip()
        if not clean:
            raise MarketConfigurationError(f"Context {label} id cannot be empty.")
        return clean

    @staticmethod
    def _contract_id(market_id: str, outcome_index: int) -> str:
        return f"{market_id}:{outcome_index}"

    @staticmethod
    def _split_contract_id(contract_id: str) -> Tuple[str, int]:
        parts = [part.strip() for part in str(contract_id or "").split(":")]
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise MarketConfigurationError("Context contract id must be MARKET_ID:OUTCOME_INDEX.")
        try:
            outcome_index = int(parts[1])
        except ValueError as exc:
            raise MarketConfigurationError("Context outcome index must be an integer.") from exc
        if outcome_index < 0:
            raise MarketConfigurationError("Context outcome index must be non-negative.")
        return parts[0], outcome_index

    @staticmethod
    def _url(base: str, path: str) -> str:
        return f"{base.rstrip('/')}/{str(path or '').strip('/')}"
