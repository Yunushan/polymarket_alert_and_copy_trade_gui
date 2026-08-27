from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlparse

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


DEFAULT_LIMITLESS_BASE_URL = "https://api.limitless.exchange"
DEFAULT_LIMITLESS_WS_URL = "wss://ws.limitless.exchange"
LIMITLESS_WS_NAMESPACE = "/markets"
LIMITLESS_HISTORY_INTERVALS = ("5m", "1h", "6h", "1d", "1w", "1m", "all")
LIMITLESS_ORDER_MANAGEMENT_OPERATIONS = ("cancel_order", "batch_cancel_orders", "cancel_all_orders")
LIMITLESS_ORDER_MANAGEMENT_CONFIRMATION = "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
LIMITLESS_GLOBAL_CANCEL_CONFIRMATION = "CANCEL ALL LIMITLESS ORDERS"
LIMITLESS_ORDER_MANAGEMENT_MAX_BATCH = 100
LIMITLESS_REFERENCES = (
    "https://docs.limitless.exchange/api-reference/markets/browse-active",
    "https://docs.limitless.exchange/developers/sdk/python/markets",
    "https://docs.limitless.exchange/developers/authentication",
    "https://docs.limitless.exchange/developers/programmatic-api",
    "https://docs.limitless.exchange/developers/migrate-from-polymarket",
    "https://docs.limitless.exchange/developers/quickstart/websocket",
    "https://docs.limitless.exchange/api-reference/trading/cancel-order",
    "https://docs.limitless.exchange/api-reference/trading/cancel-batch",
    "https://docs.limitless.exchange/api-reference/trading/cancel-all",
)


class LimitlessAdapter(MarketAdapter):
    """Limitless Exchange adapter using documented REST market data and HMAC trading APIs."""

    metadata = get_market_metadata("limitless_exchange")
    # Private account reads are exposed only through these documented,
    # validated operations.  The shared CLI/API surfaces consume this
    # allow-list so callers cannot request arbitrary authenticated paths.
    account_recovery_operations = ("positions", "account_history", "user_orders")
    order_management_operations = LIMITLESS_ORDER_MANAGEMENT_OPERATIONS

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        token_id = self.resolve_credential(
            "limitless_token_id",
            ("LIMITLESS_TOKEN_ID", "LMTS_API_KEY"),
            label="LIMITLESS_TOKEN_ID",
        )
        token_secret = self.resolve_credential(
            "limitless_token_secret",
            ("LIMITLESS_TOKEN_SECRET",),
            label="LIMITLESS_TOKEN_SECRET",
        )
        on_behalf_of = self.resolve_credential(
            "limitless_on_behalf_of",
            ("LIMITLESS_ON_BEHALF_OF",),
            label="LIMITLESS_ON_BEHALF_OF",
        )
        credential_sources = []
        for credential in (token_id, token_secret, on_behalf_of):
            if credential:
                credential_sources.append({"name": credential.name, "source": credential.source})
        health.update(
            {
                "api_base_url": self.api_base_url,
                "websocket_url": self.websocket_url,
                "websocket_namespace": LIMITLESS_WS_NAMESPACE,
                "authenticated_account_endpoints": [
                    "/portfolio/positions",
                    "/portfolio/history",
                    "/markets/:slug/user-orders",
                ],
                "account_recovery_operations": list(self.account_recovery_operations),
                "authenticated_order_management_endpoints": [
                    "DELETE /orders/:orderId",
                    "POST /orders/cancel-batch",
                    "DELETE /orders/all/:slug",
                ],
                "order_management_operations": list(self.order_management_operations),
                "order_management_enabled": self.config_bool("limitless_order_management_enabled", False),
                "references": list(LIMITLESS_REFERENCES),
                "live_trading_enabled": self.config_bool("live_trading_enabled", False),
                "credential_sources": credential_sources,
            }
        )
        return health

    @property
    def api_base_url(self) -> str:
        configured = self.config.get("limitless_api_base_url") or self.config.get("api_base_url")
        return str(configured or DEFAULT_LIMITLESS_BASE_URL).rstrip("/")

    @property
    def websocket_url(self) -> str:
        configured = self.config.get("limitless_ws_url") or self.config.get("websocket_url")
        return str(configured or DEFAULT_LIMITLESS_WS_URL).rstrip("/")

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
        slug, outcome = self._split_contract_id(contract_id)
        payload = self._get(f"/markets/{slug}/orderbook")
        yes_bids = self._levels(self._value_at(payload, "bids", "yesBids", "yes_bids"), descending=True)
        yes_asks = self._levels(self._value_at(payload, "asks", "yesAsks", "yes_asks"))

        if outcome == "YES":
            bids = yes_bids
            asks = yes_asks
        else:
            bids = self._opposite_bids_from_yes_asks(yes_asks)
            asks = self._opposite_asks_from_yes_bids(yes_bids)

        return OrderBookSnapshot(
            market_id=self.market_id,
            contract_id=self._contract_id(slug, outcome),
            bids=bids,
            asks=asks,
            raw=payload if isinstance(payload, dict) else {},
        )

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        slug, outcome = self._split_contract_id(contract_id)
        orderbook = self.get_orderbook(self._contract_id(slug, outcome))
        bid = orderbook.bids[0].price if orderbook.bids else None
        ask = orderbook.asks[0].price if orderbook.asks else None
        midpoint = (bid + ask) / 2.0 if bid is not None and ask is not None else None
        last = midpoint
        if last is None:
            market = self._get_market(slug)
            last = self._price_from_market(market, outcome)
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=self._contract_id(slug, outcome),
            last=last,
            bid=bid,
            ask=ask,
            midpoint=midpoint,
            source="limitless_orderbook",
            raw=orderbook.raw,
        )

    def list_candles(
        self,
        contract_id: str,
        *,
        resolution: str = "1d",
        from_timestamp: Optional[float] = None,
        to_timestamp: Optional[float] = None,
    ) -> List[MarketCandle]:
        """Return Limitless's documented historical YES-price series.

        Limitless returns one price point per timestamp (newest first), not
        OHLCV bars.  The shared candle model therefore repeats each point
        across OHLC and leaves volume unset.  The endpoint only exposes YES
        prices; NO candles are derived as the complementary probability.
        ``resolution`` is the documented lookback preset, while optional
        bounds are applied locally because the API does not accept custom
        start/end parameters.
        """

        self.ensure_capability("price_reading")
        slug, outcome = self._split_contract_id(contract_id)
        interval = str(resolution or "").strip().lower()
        if interval not in LIMITLESS_HISTORY_INTERVALS:
            allowed = ", ".join(LIMITLESS_HISTORY_INTERVALS)
            raise MarketConfigurationError(f"Limitless historical-price interval must be one of: {allowed}.")

        start = self._history_timestamp_seconds(from_timestamp, "from_timestamp") if from_timestamp is not None else None
        end = self._history_timestamp_seconds(to_timestamp, "to_timestamp") if to_timestamp is not None else None
        if start is not None and end is not None and end <= start:
            raise MarketConfigurationError("Limitless historical price requires to_timestamp greater than from_timestamp.")

        payload = self.runtime.get_json(
            self._url(f"/markets/{slug}/historical-price"),
            params={"interval": interval},
        )
        series: Optional[Mapping[str, Any]] = payload if isinstance(payload, Mapping) else None
        if series is None and isinstance(payload, list):
            matching = [
                item
                for item in payload
                if isinstance(item, Mapping)
                and str(item.get("slug") or item.get("marketSlug") or "").strip() == slug
            ]
            if len(matching) == 1:
                series = matching[0]
            elif len(payload) == 1 and isinstance(payload[0], Mapping):
                series = payload[0]
        if series is None:
            return []

        prices = series.get("prices")
        if not isinstance(prices, list):
            return []
        canonical = self._contract_id(slug, outcome)
        candles: List[MarketCandle] = []
        for row in prices:
            if not isinstance(row, Mapping):
                continue
            timestamp = self._history_timestamp_seconds(row.get("timestamp"), "timestamp")
            if timestamp is None or (start is not None and timestamp < start) or (end is not None and timestamp > end):
                continue
            price = self._safe_probability(row.get("price"))
            if price is None:
                continue
            if outcome == "NO":
                price = round(1.0 - price, 10)
            candles.append(
                MarketCandle(
                    market_id=self.market_id,
                    contract_id=canonical,
                    timestamp=timestamp,
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    raw=dict(row),
                )
            )
        return candles

    def list_trades(
        self,
        contract_id: str,
        *,
        limit: int = 50,
        before: Optional[float] = None,
        after: Optional[float] = None,
    ) -> List[MarketTrade]:
        """Return finalized public Limitless CLOB events for one outcome.

        The documented market-events endpoint is paginated but does not expose
        timestamp filters.  The adapter requests the first (newest) page and
        applies the shared ``before``/``after`` bounds locally.  Limitless
        reports raw six-decimal token amounts, so ``matchedSize`` is scaled to
        the normalized share size; numeric ``side`` values map to BUY/SELL.
        """

        self.ensure_capability("price_reading")
        slug, outcome = self._split_contract_id(contract_id)
        try:
            desired = int(limit)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Limitless trade history limit must be an integer between 1 and 100.") from exc
        if desired < 1 or desired > 100:
            raise MarketConfigurationError("Limitless trade history limit must be between 1 and 100.")

        start = self._history_timestamp_seconds(after, "after") if after is not None else None
        end = self._history_timestamp_seconds(before, "before") if before is not None else None
        if start is not None and end is not None and end < start:
            raise MarketConfigurationError("Limitless trade history requires before greater than or equal to after.")

        # Market events expose tokenId rather than an outcome label.  Resolve
        # the documented market record once so YES/NO rows cannot be mixed.
        token_id = self._token_id_for_outcome(slug, outcome)
        payload = self.runtime.get_json(
            self._url(f"/markets/{slug}/events"),
            params={"page": 1, "limit": desired},
        )
        rows = payload.get("events") if isinstance(payload, Mapping) else []
        if not isinstance(rows, list):
            return []

        trades: List[MarketTrade] = []
        canonical = self._contract_id(slug, outcome)
        for raw in rows:
            if not isinstance(raw, Mapping):
                continue
            row_token_id = str(raw.get("tokenId") or raw.get("token_id") or "").strip()
            if row_token_id and row_token_id != token_id:
                continue
            side_value = raw.get("side")
            if isinstance(side_value, str):
                side = side_value.strip().upper()
            else:
                try:
                    side = {0: "BUY", 1: "SELL"}[int(side_value)]
                except (KeyError, TypeError, ValueError):
                    continue
            if side not in {"BUY", "SELL"}:
                continue
            price = self._safe_probability(raw.get("price"))
            size = self._limitless_trade_size(raw.get("matchedSize") or raw.get("matched_size"))
            trade_id = str(raw.get("txHash") or raw.get("tx_hash") or raw.get("id") or "").strip()
            if price is None or size is None or not trade_id:
                continue
            timestamp = self._event_timestamp_seconds(raw.get("createdAt") or raw.get("created_at"))
            if timestamp is not None and ((start is not None and timestamp < start) or (end is not None and timestamp > end)):
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
                    raw=dict(raw),
                )
            )
        return trades

    def get_positions(self, *, on_behalf_of: Optional[str] = None) -> Any:
        """Read the authenticated Limitless portfolio positions payload.

        Limitless documents this HMAC-authenticated endpoint for the caller's
        own account.  When ``on_behalf_of`` (or the configured
        ``limitless_on_behalf_of`` profile) is supplied, the request carries the
        documented ``x-on-behalf-of`` header and the API returns the linked
        sub-account's view.  The response is intentionally returned unchanged:
        the official schema contains settlement fields that may evolve, and
        dropping them would make account recovery lossy.
        """

        return self._get_account_json("/portfolio/positions", on_behalf_of=on_behalf_of)

    def list_account_history(self, *, on_behalf_of: Optional[str] = None) -> Any:
        """Read the authenticated Limitless portfolio history payload.

        The endpoint is the documented account-level trade-history surface.
        It is separate from :meth:`list_trades`, which remains the public
        finalized market-events feed and therefore does not require credentials.
        """

        return self._get_account_json("/portfolio/history", on_behalf_of=on_behalf_of)

    def list_user_orders(self, market_slug: str, *, on_behalf_of: Optional[str] = None) -> Any:
        """Read authenticated orders for one documented Limitless market.

        ``market_slug`` is validated before it is interpolated into the URL so
        callers cannot turn an account-read request into a path traversal or
        arbitrary endpoint request.  The raw response is preserved because the
        official order schema includes venue-specific status and fill fields.
        """

        slug = self._validated_market_slug(market_slug)
        return self._get_account_json(f"/markets/{slug}/user-orders", on_behalf_of=on_behalf_of)

    def account_recovery(self, operation: str, **kwargs: Any) -> Any:
        """Dispatch one of the documented authenticated account reads."""

        normalized = str(operation or "").strip().lower()
        on_behalf_of = kwargs.get("on_behalf_of")
        if normalized == "positions":
            return self.get_positions(on_behalf_of=on_behalf_of)
        if normalized == "account_history":
            return self.list_account_history(on_behalf_of=on_behalf_of)
        if normalized == "user_orders":
            return self.list_user_orders(
                str(kwargs.get("market_slug") or ""),
                on_behalf_of=on_behalf_of,
            )
        supported = ", ".join(self.account_recovery_operations)
        raise MarketConfigurationError(
            f"{self.market_id} does not support account operation {normalized or '<empty>'}. "
            f"Supported operations: {supported}."
        )

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        self._validate_order(order)
        slug, outcome = self._split_contract_id(order.contract_id)
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=self._contract_id(slug, outcome),
            accepted=True,
            message=(
                f"DRY RUN: would place Limitless {order.side.upper()} "
                f"for {order.size:.4f} {outcome} shares"
                + (f" at limit {order.limit_price:.2f}" if order.limit_price is not None else "")
            ),
            filled_size=0.0,
            average_price=None,
            raw={"request": self._build_delegated_order_payload(order, dry_run=True)},
        )

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        self.ensure_capability("live_trading")
        self._validate_order(order)
        preflight = self.preflight_live_order(order)
        payload = self._build_delegated_order_payload(order, dry_run=False)
        body = self._canonical_json(payload)
        response = self._post_signed_json("/orders", payload, body=body)
        return {
            "market_id": self.market_id,
            "contract_id": self._contract_id(*self._split_contract_id(order.contract_id)),
            "live": True,
            "preflight": preflight,
            "request": payload,
            "response": response,
        }

    def manage_orders(self, operation: str, **kwargs: Any) -> Dict[str, Any]:
        """Run one guarded, documented Limitless order cancellation mutation.

        Limitless exposes cancellation through fixed HMAC-authenticated REST
        paths.  This method deliberately accepts only the three documented
        operations and validates every path-bearing identifier before loading
        credentials or issuing a request.
        """

        normalized = str(operation or "").strip().lower()
        if normalized not in self.order_management_operations:
            supported = ", ".join(self.order_management_operations)
            raise MarketConfigurationError(
                f"Limitless order-management operation must be one of: {supported}."
            )
        self.ensure_capability("live_trading")
        if not self.config_bool("limitless_order_management_enabled", False):
            raise MarketConfigurationError(
                "Limitless order management is disabled by adapter config. "
                "Set limitless_order_management_enabled=true only after reviewing cancellation risk."
            )
        self.ensure_live_trading_enabled("Limitless order management")
        if str(kwargs.get("confirm_order_management") or "").strip() != LIMITLESS_ORDER_MANAGEMENT_CONFIRMATION:
            raise MarketConfigurationError(
                "Limitless order management requires exact confirmation text "
                f"{LIMITLESS_ORDER_MANAGEMENT_CONFIRMATION}."
            )
        if bool(kwargs.get("async_request")):
            raise MarketConfigurationError("Limitless order-management requests are synchronous.")

        request: Dict[str, Any]
        method: str
        path: str
        body = ""
        if normalized == "cancel_order":
            order_id = self._order_management_id(kwargs.get("order_id"))
            request = {"orderId": order_id}
            method = "DELETE"
            path = f"/orders/{order_id}"
        elif normalized == "batch_cancel_orders":
            order_ids = self._order_management_ids(
                kwargs.get("order_ids", kwargs.get("orders", kwargs.get("instructions")))
            )
            request = {"orderIds": order_ids}
            method = "POST"
            path = "/orders/cancel-batch"
            body = self._canonical_json(request)
        else:
            if str(kwargs.get("confirm_global_cancel") or "").strip() != LIMITLESS_GLOBAL_CANCEL_CONFIRMATION:
                raise MarketConfigurationError(
                    "Limitless market cancellation requires exact confirmation text "
                    f"{LIMITLESS_GLOBAL_CANCEL_CONFIRMATION}."
                )
            slug = self._validated_market_slug(str(kwargs.get("market_slug") or ""))
            request = {"marketSlug": slug}
            method = "DELETE"
            path = f"/orders/all/{slug}"

        response = self._signed_request(method, path, body=body, content_type=bool(body))
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
                "references": list(LIMITLESS_REFERENCES),
            },
            "request": request,
            "response": response,
        }

    def copy_trade_from_activity(self, activity: Mapping[str, Any]) -> PaperOrderResult:
        """Build a local copy preview from Limitless portfolio history.

        The documented HMAC ``GET /portfolio/history`` endpoint returns
        sub-account fills and supports the same delegated profile boundary as
        the other portfolio reads.  This method accepts a normalized history
        row, resolves its market/outcome from the documented token ids when
        needed, and only creates a local paper preview.
        """

        self.ensure_capability("copy_trading")
        contract_id = str(activity.get("asset") or activity.get("contract_id") or "").strip()
        token_id = str(activity.get("token_id") or activity.get("tokenId") or "").strip()
        market_slug = str(activity.get("market_slug") or activity.get("marketSlug") or "").strip()
        if contract_id:
            slug, outcome = self._split_contract_id(contract_id)
            market_slug = slug
            outcome = outcome.upper()
            if outcome not in {"YES", "NO"}:
                raise MarketConfigurationError("Limitless activity outcome must be YES or NO.")
            contract_id = self._contract_id(slug, outcome)
        else:
            market_slug = self._validated_market_slug(market_slug)
            outcome_value = str(activity.get("outcome") or activity.get("position") or "").strip().upper()
            if outcome_value in {"YES", "NO"}:
                outcome = outcome_value
            elif token_id:
                market = self._get_market(market_slug)
                tokens = market.get("tokens") if isinstance(market, Mapping) else None
                positions = market.get("positionIds") or market.get("position_ids") if isinstance(market, Mapping) else None
                yes_token = str(tokens.get("yes") or tokens.get("YES") or "").strip() if isinstance(tokens, Mapping) else ""
                no_token = str(tokens.get("no") or tokens.get("NO") or "").strip() if isinstance(tokens, Mapping) else ""
                if not yes_token and isinstance(positions, list) and len(positions) >= 2:
                    yes_token, no_token = str(positions[0]).strip(), str(positions[1]).strip()
                if token_id == yes_token:
                    outcome = "YES"
                elif token_id == no_token:
                    outcome = "NO"
                else:
                    raise MarketConfigurationError("Limitless activity tokenId does not match the selected market.")
            else:
                raise MarketConfigurationError("Limitless activity requires contract_id or marketSlug plus outcome/tokenId.")
            contract_id = self._contract_id(market_slug, outcome)

        side = str(activity.get("side") or "").strip().upper()
        if side not in {"BUY", "SELL"}:
            raise MarketConfigurationError("Limitless activity side must be BUY or SELL.")
        try:
            size = float(activity.get("size") if activity.get("size") not in (None, "") else activity.get("quantity"))
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Limitless activity size must be numeric.") from exc
        if not self._is_positive_number(size):
            raise MarketConfigurationError("Limitless activity size must be positive and finite.")
        price = self._safe_probability(activity.get("price"))
        if price is None or price <= 0.0 or price >= 1.0:
            raise MarketConfigurationError("Limitless activity price must be between 0 and 1.")
        trade_id = str(activity.get("trade_id") or activity.get("tradeId") or activity.get("id") or "").strip()
        if not trade_id:
            raise MarketConfigurationError("Limitless activity requires a documented trade id.")
        metadata = {"activity": dict(activity), "source": "limitless_portfolio_history"}
        preview = self.place_paper_order(
            PaperOrderRequest(
                market_id=self.market_id,
                contract_id=contract_id,
                side=side,
                size=size,
                limit_price=price,
                metadata=metadata,
            )
        )
        preview.raw["source"] = "limitless_portfolio_history"
        preview.raw["activity"] = dict(activity)
        return preview

    def websocket_connection_info(
        self,
        *,
        market_slugs: Optional[List[str]] = None,
        market_addresses: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Return the documented Socket.IO connection and subscription shape.

        The project does not maintain a long-running Socket.IO client here; this
        method keeps GUI/alert code from hardcoding Limitless channel names.
        """

        payload = self.websocket_market_subscription(
            market_slugs=market_slugs,
            market_addresses=market_addresses,
        )
        safe_websocket_url = self.runtime.validate_endpoint(
            self.websocket_url,
            setting_key="limitless_ws_url",
            kind="websocket",
            base_url=True,
            resolve_addresses=False,
        )
        return {
            "url": safe_websocket_url,
            "namespace": LIMITLESS_WS_NAMESPACE,
            "transports": ["websocket"],
            "events": ["newPriceData", "orderbookUpdate", "system", "exception"],
            "subscribe": payload,
        }

    @staticmethod
    def websocket_market_subscription(
        *,
        market_slugs: Optional[List[str]] = None,
        market_addresses: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        slugs = [str(slug).strip() for slug in (market_slugs or []) if str(slug).strip()]
        addresses = [str(address).strip() for address in (market_addresses or []) if str(address).strip()]
        if not slugs and not addresses:
            raise MarketConfigurationError("Limitless WebSocket market subscription requires slugs or addresses.")
        payload: Dict[str, Any] = {}
        if addresses:
            payload["marketAddresses"] = addresses
        if slugs:
            payload["marketSlugs"] = slugs
        return {
            "event": "subscribe_market_prices",
            "namespace": LIMITLESS_WS_NAMESPACE,
            "payload": payload,
        }

    def _fetch_active_markets(self, *, limit: int) -> List[Mapping[str, Any]]:
        params = {
            "page": 1,
            "limit": max(1, min(int(limit or 50), 100)),
            "sortBy": str(self.config.get("limitless_sort_by") or "volume"),
        }
        trade_type = str(self.config.get("limitless_trade_type") or "").strip()
        if trade_type:
            params["tradeType"] = trade_type
        data = self.runtime.get_json(self._url("/markets/active"), params=params)
        markets = data.get("data") if isinstance(data, Mapping) else []
        return [market for market in markets if isinstance(market, Mapping)] if isinstance(markets, list) else []

    def _get_market(self, slug_or_id: str) -> Mapping[str, Any]:
        ref = str(slug_or_id or "").strip()
        if not ref:
            raise MarketConfigurationError("Limitless market slug cannot be empty.")
        data = self._get(f"/markets/{ref}")
        if isinstance(data, Mapping):
            market = data.get("market")
            if isinstance(market, Mapping):
                return market
            if "slug" in data or "title" in data:
                return data
        raise MarketConfigurationError(f"Limitless market {ref!r} was not found.")

    def _get(self, path: str) -> Any:
        return self.runtime.get_json(self._url(path))

    def _url(self, path: str) -> str:
        clean_path = "/" + str(path or "").strip("/")
        return f"{self.api_base_url}{clean_path}"

    def _build_delegated_order_payload(self, order: PaperOrderRequest, *, dry_run: bool) -> Dict[str, Any]:
        slug, outcome = self._split_contract_id(order.contract_id)
        side = str(order.side or "").upper()
        forbidden_wire_fields = {"token_id", "tokenId", "maker_amount", "makerAmount"}
        supplied_wire_fields = sorted(forbidden_wire_fields.intersection(order.metadata))
        if supplied_wire_fields:
            raise MarketConfigurationError(
                "Limitless wire identity and amount are derived from the reviewed order; remove metadata fields: "
                + ", ".join(supplied_wire_fields)
            )
        order_type = str(order.metadata.get("order_type") or self.config.get("limitless_order_type") or "GTC").upper()
        if order_type not in {"GTC", "FAK", "FOK"}:
            raise MarketConfigurationError("Limitless order_type must be GTC, FAK, or FOK.")
        if order_type == "FOK" and order.limit_price is not None:
            raise MarketConfigurationError(
                "Limitless FOK is a market-style order and must not discard a reviewed limit price. "
                "Use GTC/FAK to enforce the limit."
            )
        if order_type in {"GTC", "FAK"} and order.limit_price is None:
            raise MarketConfigurationError(
                "Limitless GTC/FAK are limit orders and require a reviewed limit price; use FOK only for an "
                "explicit market-style request."
            )

        args: Dict[str, Any] = {
            "tokenId": self._token_id_for_outcome(slug, outcome),
            "side": side,
        }
        if order_type == "FOK":
            args["makerAmount"] = float(order.size)
        else:
            args["price"] = self._limit_probability(order.limit_price)
            args["size"] = float(order.size)
            if order_type == "GTC" and "post_only" in order.metadata:
                args["postOnly"] = bool(order.metadata["post_only"])

        payload: Dict[str, Any] = {
            "marketSlug": slug,
            "orderType": order_type,
            "onBehalfOf": str(
                order.metadata.get("on_behalf_of")
                or self.config.get("limitless_on_behalf_of")
                or self._required_on_behalf_of(dry_run=dry_run)
            ),
            "args": args,
        }
        if dry_run:
            payload["dryRun"] = True
        return payload

    def _token_id_for_outcome(self, slug: str, outcome: str) -> str:
        market = self._get_market(slug)
        tokens = market.get("tokens")
        if isinstance(tokens, Mapping):
            value = tokens.get(outcome.lower()) or tokens.get(outcome.upper())
            if value:
                return str(value)
        position_ids = market.get("positionIds") or market.get("position_ids")
        if isinstance(position_ids, list) and len(position_ids) >= 2:
            return str(position_ids[0 if outcome == "YES" else 1])
        raise MarketConfigurationError(f"Limitless market {slug!r} did not include token IDs for {outcome}.")

    def _required_on_behalf_of(self, *, dry_run: bool) -> str:
        credential = self.resolve_credential(
            "limitless_on_behalf_of",
            ("LIMITLESS_ON_BEHALF_OF",),
            required=not dry_run,
            label="LIMITLESS_ON_BEHALF_OF",
        )
        if credential:
            return credential.value
        return "dry-run-profile"

    def _hmac_headers(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        token_id = self.resolve_credential(
            "limitless_token_id",
            ("LIMITLESS_TOKEN_ID", "LMTS_API_KEY"),
            required=True,
            label="LIMITLESS_TOKEN_ID",
        )
        token_secret = self.resolve_credential(
            "limitless_token_secret",
            ("LIMITLESS_TOKEN_SECRET",),
            required=True,
            label="LIMITLESS_TOKEN_SECRET",
        )
        timestamp = datetime.now(timezone.utc).isoformat()
        request_path = self._request_path(path)
        message = f"{timestamp}\n{method.upper()}\n{request_path}\n{body}"
        try:
            secret = base64.b64decode(token_secret.value)
        except Exception as exc:
            raise MarketConfigurationError("Limitless token secret must be base64-encoded.") from exc
        signature = base64.b64encode(hmac.new(secret, message.encode("utf-8"), hashlib.sha256).digest()).decode(
            "utf-8"
        )
        return {
            "lmts-api-key": token_id.value,
            "lmts-timestamp": timestamp,
            "lmts-signature": signature,
        }

    def _signed_request(self, method: str, path: str, *, body: str = "", content_type: bool = False) -> Any:
        request_method = str(method or "").strip().upper()
        if request_method not in {"POST", "DELETE"}:
            raise MarketConfigurationError("Limitless signed mutation method is not supported.")
        headers = self._hmac_headers(request_method, path, body)
        headers.update({"Accept": "application/json", "User-Agent": self.runtime.user_agent})
        if content_type:
            headers["Content-Type"] = "application/json"
        return self.runtime.request_json(
            request_method,
            self._url(path),
            data=body,
            headers=headers,
        )

    def _post_signed_json(self, path: str, payload: Mapping[str, Any], *, body: Optional[str] = None) -> Any:
        request_body = body if body is not None else self._canonical_json(payload)
        return self._signed_request("POST", path, body=request_body, content_type=True)

    def _get_account_json(self, path: str, *, on_behalf_of: Optional[str] = None) -> Any:
        """Issue a documented HMAC-authenticated GET for account data."""

        delegated_profile = self._delegated_profile(on_behalf_of)
        request_path = "/" + str(path or "").strip("/")
        headers = self._hmac_headers("GET", request_path)
        if delegated_profile:
            headers["x-on-behalf-of"] = delegated_profile
        headers.update({"Accept": "application/json", "User-Agent": self.runtime.user_agent})
        return self.runtime.request_json(
            "GET",
            self._url(request_path),
            headers=headers,
        )

    def _delegated_profile(self, value: Optional[str]) -> Optional[str]:
        raw = value if value is not None else self.config.get("limitless_on_behalf_of")
        if raw in (None, ""):
            return None
        profile = str(raw).strip()
        if not profile or len(profile) > 128 or not re.fullmatch(r"[A-Za-z0-9._:-]+", profile):
            raise MarketConfigurationError("Limitless on_behalf_of profile must be a safe profile identifier.")
        return profile

    @staticmethod
    def _validated_market_slug(value: str) -> str:
        slug = str(value or "").strip()
        if not slug or len(slug) > 200 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._~-]*", slug):
            raise MarketConfigurationError("Limitless market slug must be a safe URL path segment.")
        return slug

    @staticmethod
    def _order_management_id(value: Any) -> str:
        normalized = str(value or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,199}", normalized):
            raise MarketConfigurationError("Limitless order_id must be a short path-safe identifier.")
        return normalized

    @classmethod
    def _order_management_ids(cls, value: Any) -> List[str]:
        if not isinstance(value, (list, tuple)) or not value:
            raise MarketConfigurationError("Limitless batch cancellation requires a non-empty order id list.")
        if len(value) > LIMITLESS_ORDER_MANAGEMENT_MAX_BATCH:
            raise MarketConfigurationError(
                "Limitless batch cancellation accepts at most "
                f"{LIMITLESS_ORDER_MANAGEMENT_MAX_BATCH} order ids."
            )
        order_ids = [cls._order_management_id(item) for item in value]
        if len(set(order_ids)) != len(order_ids):
            raise MarketConfigurationError("Limitless batch cancellation order ids must be unique.")
        return order_ids

    def _request_path(self, path_or_url: str) -> str:
        parsed = urlparse(path_or_url if "://" in path_or_url else self._url(path_or_url))
        return parsed.path + (f"?{parsed.query}" if parsed.query else "")

    def _event_from_market(self, market: Mapping[str, Any]) -> MarketEvent:
        slug = self._market_slug(market)
        return MarketEvent(
            market_id=self.market_id,
            event_id=slug,
            title=str(market.get("title") or market.get("name") or slug),
            url=self._market_url(market),
            status=self._status_from_market(market),
            raw=dict(market),
        )

    def _contracts_from_market(self, market: Mapping[str, Any]) -> List[MarketContract]:
        slug = self._market_slug(market)
        if not slug:
            return []
        title = str(market.get("title") or market.get("name") or slug)
        status = self._status_from_market(market)
        return [
            MarketContract(
                market_id=self.market_id,
                contract_id=self._contract_id(slug, "YES"),
                event_id=slug,
                title=f"{title} - Yes",
                outcome="Yes",
                url=self._market_url(market),
                status=status,
                raw={"market": dict(market), "outcome": "YES"},
            ),
            MarketContract(
                market_id=self.market_id,
                contract_id=self._contract_id(slug, "NO"),
                event_id=slug,
                title=f"{title} - No",
                outcome="No",
                url=self._market_url(market),
                status=status,
                raw={"market": dict(market), "outcome": "NO"},
            ),
        ]

    def _validate_order(self, order: PaperOrderRequest) -> None:
        self.ensure_order_market(order)
        self._split_contract_id(order.contract_id)
        side = str(order.side or "").upper()
        if side not in {"BUY", "SELL"}:
            raise MarketConfigurationError("Limitless order side must be BUY or SELL.")
        if not self._is_positive_number(order.size):
            raise MarketConfigurationError("Limitless order size must be positive.")
        if order.limit_price is not None:
            self._limit_probability(order.limit_price)

    @staticmethod
    def _market_matches_query(market: Mapping[str, Any], query: str) -> bool:
        values = [
            market.get("id"),
            market.get("slug"),
            market.get("title"),
            market.get("description"),
            market.get("tradeType"),
            " ".join(str(tag) for tag in market.get("tags") or []),
            " ".join(str(category) for category in market.get("categories") or []),
        ]
        return query in " ".join(str(value or "") for value in values).lower()

    @staticmethod
    def _status_from_market(market: Mapping[str, Any]) -> str:
        if market.get("expired") is True:
            return "expired"
        status = str(market.get("status") or "").strip().lower()
        if status in {"funded", "open", "active"}:
            return "active"
        return status

    @staticmethod
    def _market_url(market: Mapping[str, Any]) -> str:
        raw = str(market.get("url") or "").strip()
        if raw:
            return raw
        slug = LimitlessAdapter._market_slug(market)
        return f"https://limitless.exchange/markets/{slug}" if slug else "https://limitless.exchange"

    @staticmethod
    def _market_slug(market: Mapping[str, Any]) -> str:
        return str(market.get("slug") or market.get("marketSlug") or market.get("id") or "").strip()

    @staticmethod
    def _split_contract_id(contract_id: str) -> Tuple[str, str]:
        raw = str(contract_id or "").strip()
        if not raw:
            raise MarketConfigurationError("Limitless order requires a contract id.")
        if ":" in raw:
            slug, outcome = raw.rsplit(":", 1)
        else:
            slug, outcome = raw, "YES"
        slug = slug.strip()
        outcome = outcome.strip().upper()
        if not slug:
            raise MarketConfigurationError("Limitless contract id must include a market slug.")
        if outcome not in {"YES", "NO"}:
            raise MarketConfigurationError("Limitless contract outcome must be YES or NO.")
        return slug, outcome

    @staticmethod
    def _contract_id(slug: str, outcome: str) -> str:
        return f"{slug}:{outcome.upper()}"

    @staticmethod
    def _price_from_market(market: Mapping[str, Any], outcome: str) -> Optional[float]:
        prices = market.get("prices")
        if isinstance(prices, list) and len(prices) >= 2:
            return LimitlessAdapter._safe_probability(prices[0 if outcome == "YES" else 1])
        return None

    @staticmethod
    def _value_at(data: Any, *keys: str) -> Any:
        if not isinstance(data, Mapping):
            return []
        for key in keys:
            value = data.get(key)
            if value is not None:
                return value
        orderbook = data.get("orderbook") or data.get("book")
        if isinstance(orderbook, Mapping):
            for key in keys:
                value = orderbook.get(key)
                if value is not None:
                    return value
        return []

    @staticmethod
    def _levels(raw_levels: Any, *, descending: bool = False) -> List[OrderBookLevel]:
        levels: List[OrderBookLevel] = []
        if not isinstance(raw_levels, list):
            return levels
        for raw in raw_levels:
            price: Any
            size: Any
            if isinstance(raw, (list, tuple)) and len(raw) >= 2:
                price, size = raw[0], raw[1]
            elif isinstance(raw, Mapping):
                price = raw.get("price") or raw.get("p")
                size = raw.get("size") or raw.get("quantity") or raw.get("q")
            else:
                continue
            parsed_price = LimitlessAdapter._safe_probability(price)
            try:
                parsed_size = float(size)
            except (TypeError, ValueError):
                continue
            if parsed_price is None or not LimitlessAdapter._is_positive_number(parsed_size):
                continue
            levels.append(OrderBookLevel(price=parsed_price, size=parsed_size))
        levels.sort(key=lambda level: level.price, reverse=descending)
        return levels

    @staticmethod
    def _opposite_bids_from_yes_asks(levels: List[OrderBookLevel]) -> List[OrderBookLevel]:
        bids = [
            OrderBookLevel(price=round(1.0 - level.price, 10), size=level.size)
            for level in levels
            if 0.0 <= 1.0 - level.price <= 1.0
        ]
        bids.sort(key=lambda level: level.price, reverse=True)
        return bids

    @staticmethod
    def _opposite_asks_from_yes_bids(levels: List[OrderBookLevel]) -> List[OrderBookLevel]:
        asks = [
            OrderBookLevel(price=round(1.0 - level.price, 10), size=level.size)
            for level in levels
            if 0.0 <= 1.0 - level.price <= 1.0
        ]
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
        if number > 1.0:
            if number <= 100.0:
                number = number / 100.0
            else:
                return None
        if number < 0.0 or number > 1.0:
            return None
        return number

    @staticmethod
    def _history_timestamp_seconds(value: Any, label: str) -> Optional[float]:
        if value is None:
            return None
        try:
            timestamp = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"Limitless {label} must be a finite Unix timestamp.") from exc
        if not math.isfinite(timestamp) or timestamp < 0:
            raise MarketConfigurationError(f"Limitless {label} must be a finite Unix timestamp.")
        if timestamp >= 100_000_000_000:
            timestamp /= 1000.0
        return timestamp

    @staticmethod
    def _event_timestamp_seconds(value: Any) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            try:
                timestamp = float(value)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(timestamp) or timestamp < 0:
                return None
            return timestamp / 1000.0 if timestamp >= 100_000_000_000 else timestamp
        raw = str(value).strip()
        if not raw:
            return None
        try:
            timestamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            try:
                return LimitlessAdapter._history_timestamp_seconds(raw, "createdAt")
            except MarketConfigurationError:
                return None
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp.timestamp()

    @staticmethod
    def _limitless_trade_size(value: Any) -> Optional[float]:
        try:
            size = float(value) / 1_000_000.0
        except (TypeError, ValueError):
            return None
        return size if math.isfinite(size) and size > 0 else None

    @staticmethod
    def _limit_probability(value: Any) -> float:
        probability = LimitlessAdapter._safe_probability(value)
        if probability is None or probability <= 0.0 or probability >= 1.0:
            raise MarketConfigurationError("Limitless limit price must be between 0 and 1.")
        return probability

    @staticmethod
    def _is_positive_number(value: Any) -> bool:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False
        return math.isfinite(number) and number > 0

    @staticmethod
    def _canonical_json(data: Mapping[str, Any]) -> str:
        return json.dumps(data, separators=(",", ":"), sort_keys=True)
