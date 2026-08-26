from __future__ import annotations

import math
import os
import re
from typing import Any, Dict, List, Mapping, Optional

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
from polymarket import clob_rest, gamma
from polymarket import clob_auth
from polymarket.auth_readiness import build_clob_auth_readiness, parse_signature_type, validate_sdk_trading_readiness
from polymarket.geoblock import check_geoblock
from polymarket.http_client import PolymarketValidationError
from polymarket.trader import PolymarketTrader, TraderConfig


POLYMARKET_PRICE_HISTORY_INTERVALS = ("max", "all", "1m", "1w", "1d", "6h", "1h")
POLYMARKET_ACCOUNT_OPERATIONS = ("active_orders", "order_detail", "fills")
POLYMARKET_ORDER_MANAGEMENT_OPERATIONS = (
    "cancel_order",
    "cancel_orders",
    "cancel_all_orders",
    "cancel_market_orders",
)
POLYMARKET_ORDER_MANAGEMENT_CONFIRMATION = "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
POLYMARKET_ORDER_MANAGEMENT_REFERENCES = (
    "https://docs.polymarket.com/api-reference/trade/get-user-orders",
    "https://docs.polymarket.com/api-reference/trade/get-single-order-by-id",
    "https://docs.polymarket.com/api-reference/trade/cancel-single-order",
    "https://docs.polymarket.com/api-reference/trade/cancel-multiple-orders",
    "https://docs.polymarket.com/api-reference/trade/cancel-all-orders",
    "https://docs.polymarket.com/api-reference/trade/cancel-market-orders",
)


class PolymarketAdapter(MarketAdapter):
    metadata = get_market_metadata("polymarket")
    account_recovery_operations = POLYMARKET_ACCOUNT_OPERATIONS
    order_management_operations = POLYMARKET_ORDER_MANAGEMENT_OPERATIONS

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        credential_sources = []
        for config_key, env_vars in (
            ("private_key", ("PRIVATE_KEY", "POLYMARKET_PRIVATE_KEY")),
            ("funder_address", ("FUNDER_ADDRESS", "POLYMARKET_FUNDER_ADDRESS", "DEPOSIT_WALLET_ADDRESS")),
            ("signature_type", ("SIGNATURE_TYPE", "POLYMARKET_SIGNATURE_TYPE")),
        ):
            credential = self.resolve_credential(config_key, env_vars, label=env_vars[0])
            if credential:
                credential_sources.append({"name": credential.name, "source": credential.source})
        readiness = build_clob_auth_readiness(self.config)
        health.update(
            {
                "live_trading_enabled": self.config_bool("live_trading_enabled", False),
                "credential_sources": credential_sources,
                "credential_requirement": "live_trading_only",
                "geoblock_required_for_live": True,
                "clob_auth_readiness": readiness,
                "account_recovery_operations": list(self.account_recovery_operations),
                "order_management_operations": list(self.order_management_operations),
                "order_management_enabled": self.config_bool("polymarket_order_management_enabled", False),
                "authenticated_account_endpoints": ["GET /data/orders", "GET /order/{orderID}", "GET /trades"],
                "order_management_endpoints": [
                    "DELETE /order",
                    "DELETE /orders",
                    "DELETE /cancel-all",
                    "DELETE /cancel-market-orders",
                ],
            }
        )
        return health

    def search_profiles(self, query: str, limit: int = 10) -> List[gamma.ProfileResult]:
        return gamma.search_profiles(query, limit=limit)

    def get_market_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        return gamma.get_market_by_slug(slug)

    def get_market_by_id(self, market_id: str) -> Optional[Dict[str, Any]]:
        return gamma.get_market_by_id(market_id)

    def get_event_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        return gamma.get_event_by_slug(slug)

    def get_event_by_id(self, event_id: str) -> Optional[Dict[str, Any]]:
        return gamma.get_event_by_id(event_id)

    def parse_market_outcomes(self, market: Dict[str, Any]) -> List[gamma.MarketOutcome]:
        return gamma.parse_market_outcomes(market)

    def check_geoblock(self) -> Dict[str, Any]:
        return check_geoblock()

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        query = str(query or "").strip()
        if not query:
            return []
        limit = max(1, min(int(limit or 50), 100))

        data = gamma.public_search(query, search_profiles=False, search_tags=False, limit_per_type=limit)
        if not isinstance(data, Mapping):
            return []
        events = data.get("events") or []
        markets = data.get("markets") or []
        if not isinstance(events, list):
            events = []
        if not isinstance(markets, list):
            markets = []
        out: List[MarketEvent] = []

        for raw in events:
            if not isinstance(raw, Mapping):
                continue
            event_id = str(raw.get("id") or raw.get("slug") or "")
            title = str(raw.get("title") or raw.get("question") or raw.get("slug") or event_id)
            if event_id:
                out.append(
                    MarketEvent(
                        market_id=self.market_id,
                        event_id=event_id,
                        title=title,
                        url=str(raw.get("url") or ""),
                        status=self._status_from_raw(raw),
                        raw=raw,
                    )
                )

        for raw in markets:
            if not isinstance(raw, Mapping):
                continue
            market_id = str(raw.get("id") or raw.get("conditionId") or raw.get("slug") or "")
            title = str(raw.get("question") or raw.get("title") or raw.get("slug") or market_id)
            if market_id:
                out.append(
                    MarketEvent(
                        market_id=self.market_id,
                        event_id=market_id,
                        title=title,
                        url=str(raw.get("url") or ""),
                        status=self._status_from_raw(raw),
                        raw=raw,
                    )
                )

        return out[:limit]

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        ref = str(event_id or "").strip()
        if not ref:
            return []

        raw_event = self.get_event_by_id(ref) if ref.isdigit() else self.get_event_by_slug(ref)
        if isinstance(raw_event, Mapping):
            contracts: List[MarketContract] = []
            markets = raw_event.get("markets") or []
            if isinstance(markets, list):
                for market in markets:
                    if isinstance(market, Mapping):
                        contracts.extend(self._contracts_from_market(market))
            return contracts

        raw_market = self.get_market_by_id(ref) if ref.isdigit() else self.get_market_by_slug(ref)
        return self._contracts_from_market(raw_market) if isinstance(raw_market, Mapping) else []

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        orderbook = self.get_orderbook(contract_id)
        try:
            midpoint = self._safe_probability(clob_rest.get_midpoint(contract_id))
        except Exception:
            midpoint = None
        try:
            last_trade = self._safe_probability(clob_rest.get_last_trade_price(contract_id))
        except Exception:
            last_trade = None
        if midpoint is None and orderbook.bids and orderbook.asks:
            midpoint = (orderbook.bids[0].price + orderbook.asks[0].price) / 2.0
        raw = dict(orderbook.raw)
        raw["last_trade"] = last_trade
        raw["midpoint"] = midpoint
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=contract_id,
            last=last_trade,
            bid=orderbook.bids[0].price if orderbook.bids else None,
            ask=orderbook.asks[0].price if orderbook.asks else None,
            midpoint=midpoint,
            source="polymarket_clob",
            raw=raw,
        )

    def get_orderbook(self, contract_id: str) -> OrderBookSnapshot:
        self.ensure_capability("orderbook_reading")
        book = clob_rest.get_book(contract_id)
        if not isinstance(book, Mapping):
            book = {}
        return OrderBookSnapshot(
            market_id=self.market_id,
            contract_id=contract_id,
            bids=self._levels(book.get("bids") or book.get("buys") or [], descending=True),
            asks=self._levels(book.get("asks") or book.get("sells") or []),
            raw=book,
        )

    def list_trades(
        self,
        contract_id: str,
        *,
        limit: int = 50,
        before: Optional[float] = None,
        after: Optional[float] = None,
    ) -> List[MarketTrade]:
        """Return authenticated public CLOB trades for a token.

        Polymarket's documented CLOB trade feed is L2-authenticated even
        though it is read-only.  The adapter accepts explicit signed L2
        headers from the operator and never derives or persists them.  Missing
        headers fail closed instead of silently falling back to an unscoped or
        private endpoint.
        """

        token_id = str(contract_id or "").strip()
        if not token_id:
            raise MarketConfigurationError("Polymarket trade history requires a contract id.")
        try:
            desired = int(limit)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Polymarket trade history limit must be an integer between 1 and 500.") from exc
        if desired < 1 or desired > 500:
            raise MarketConfigurationError("Polymarket trade history limit must be between 1 and 500.")

        params: Dict[str, Any] = {"asset_id": token_id, "limit": desired}
        if before is not None:
            params["before"] = self._history_timestamp(before, "before")
        if after is not None:
            params["after"] = self._history_timestamp(after, "after")
        try:
            payload = clob_auth.get_trades(self._l2_read_headers(), **params)
        except (ValueError, KeyError) as exc:
            raise MarketConfigurationError(f"Polymarket authenticated trade history is not ready: {exc}") from exc
        rows = payload.get("data") if isinstance(payload, Mapping) else []
        if not isinstance(rows, list):
            return []

        trades: List[MarketTrade] = []
        for raw in rows:
            if not isinstance(raw, Mapping):
                continue
            asset_id = str(raw.get("asset_id") or raw.get("assetId") or token_id).strip()
            if asset_id and asset_id != token_id:
                continue
            side = str(raw.get("side") or "").strip().upper()
            if side not in {"BUY", "SELL"}:
                continue
            price = self._finite_probability(raw.get("price"))
            size = self._fixed_trade_size(raw.get("size"))
            trade_id = str(raw.get("id") or raw.get("trade_id") or "").strip()
            if price is None or size is None or not trade_id:
                continue
            trades.append(
                MarketTrade(
                    market_id=self.market_id,
                    contract_id=token_id,
                    trade_id=trade_id,
                    side=side,
                    price=price,
                    size=size,
                    timestamp=self._history_timestamp_value(
                        raw.get("match_time") or raw.get("matchTime") or raw.get("last_update")
                    ),
                    raw=dict(raw),
                )
            )
        return trades

    def get_account_orders(
        self,
        *,
        market_id: str = "",
        contract_id: str = "",
        next_cursor: str = "",
    ) -> Dict[str, Any]:
        """Read the authenticated user's open CLOB orders.

        Polymarket documents ``GET /data/orders`` as a paginated L2-authenticated
        account endpoint.  Filters are passed only as query parameters; callers
        cannot select an arbitrary path or endpoint.
        """

        normalized_market = self._account_filter(market_id, "market_id")
        normalized_asset = self._account_filter(contract_id, "contract_id")
        cursor = self._account_cursor(next_cursor)
        try:
            return clob_auth.get_orders(
                self._l2_read_headers(),
                market=normalized_market or None,
                asset_id=normalized_asset or None,
                next_cursor=cursor or None,
            )
        except (ValueError, KeyError) as exc:
            raise MarketConfigurationError(f"Polymarket authenticated order recovery is not ready: {exc}") from exc

    def get_account_order(self, order_id: str) -> Dict[str, Any]:
        """Read one authenticated order by its documented order hash."""

        normalized_order_id = self._order_management_id(order_id, label="order_id")
        try:
            return clob_auth.get_order(normalized_order_id, self._l2_read_headers())
        except (ValueError, KeyError) as exc:
            raise MarketConfigurationError(f"Polymarket authenticated order detail is not ready: {exc}") from exc

    def get_account_fills(
        self,
        *,
        trade_id: str = "",
        market_id: str = "",
        contract_id: str = "",
        before: Any = None,
        after: Any = None,
        next_cursor: str = "",
        limit: Any = 100,
    ) -> Dict[str, Any]:
        """Read the authenticated user's CLOB fills/trades feed."""

        normalized_trade_id = self._account_filter(trade_id, "trade_id")
        normalized_market = self._account_filter(market_id, "market_id")
        normalized_asset = self._account_filter(contract_id, "contract_id")
        normalized_limit = self._account_limit(limit)
        params: Dict[str, Any] = {
            "id": normalized_trade_id or None,
            "market": normalized_market or None,
            "asset_id": normalized_asset or None,
            "before": self._account_timestamp(before, "before") if before not in (None, "") else None,
            "after": self._account_timestamp(after, "after") if after not in (None, "") else None,
            "next_cursor": self._account_cursor(next_cursor) or None,
            "limit": normalized_limit,
        }
        if params["before"] is not None and params["after"] is not None and params["before"] < params["after"]:
            raise MarketConfigurationError("Polymarket fills require before greater than or equal to after.")
        try:
            return clob_auth.get_trades(self._l2_read_headers(), **params)
        except (ValueError, KeyError) as exc:
            raise MarketConfigurationError(f"Polymarket authenticated fills are not ready: {exc}") from exc

    def account_recovery(self, operation: str, **kwargs: Any) -> Any:
        """Dispatch one validated, documented Polymarket account read."""

        normalized = str(operation or "").strip().lower()
        if normalized == "active_orders":
            return self.get_account_orders(
                market_id=kwargs.get("market_id", ""),
                contract_id=kwargs.get("contract_id", ""),
                next_cursor=kwargs.get("next_cursor", ""),
            )
        if normalized == "order_detail":
            return self.get_account_order(kwargs.get("order_id", ""))
        if normalized == "fills":
            return self.get_account_fills(
                trade_id=kwargs.get("trade_id", ""),
                market_id=kwargs.get("market_id", ""),
                contract_id=kwargs.get("contract_id", ""),
                before=kwargs.get("before"),
                after=kwargs.get("after"),
                next_cursor=kwargs.get("next_cursor", ""),
                limit=kwargs.get("limit", 100),
            )
        supported = ", ".join(self.account_recovery_operations)
        raise MarketConfigurationError(f"Polymarket account recovery supports only: {supported}.")

    def manage_orders(self, operation: str, **kwargs: Any) -> Dict[str, Any]:
        """Run one guarded Polymarket CLOB order-management mutation.

        Every mutation uses a fixed documented endpoint, explicit L2 headers,
        the shared live-safety gates, a separate adapter opt-in, and exact
        operator confirmation.  No caller-provided URL, method, or headers are
        accepted.
        """

        normalized = str(operation or "").strip().lower()
        if normalized not in self.order_management_operations:
            supported = ", ".join(self.order_management_operations)
            raise MarketConfigurationError(f"Polymarket order management supports only: {supported}.")
        self.ensure_capability("live_trading")
        if not self.config_bool("polymarket_order_management_enabled", False):
            raise MarketConfigurationError(
                "Polymarket order management is disabled by adapter config. "
                "Set polymarket_order_management_enabled=true only after reviewing cancellation risk."
            )
        self.ensure_live_trading_enabled("Polymarket order management")
        if str(kwargs.get("confirm_order_management") or "").strip() != POLYMARKET_ORDER_MANAGEMENT_CONFIRMATION:
            raise MarketConfigurationError(
                "Polymarket order management requires exact confirmation text "
                f"{POLYMARKET_ORDER_MANAGEMENT_CONFIRMATION}."
            )

        headers = self._l2_read_headers()
        request: Dict[str, Any] = {}
        if normalized == "cancel_order":
            order_id = self._order_management_id(kwargs.get("order_id"), label="order_id")
            request = {"orderID": order_id}
            response = clob_auth.cancel_order(order_id, headers)
        elif normalized == "cancel_orders":
            order_ids = self._order_management_ids(kwargs.get("orders", kwargs.get("instructions")))
            request = {"orders": order_ids}
            response = clob_auth.cancel_orders(order_ids, headers)
        elif normalized == "cancel_all_orders":
            response = clob_auth.cancel_all_orders(headers)
        else:
            market_id = self._account_filter(kwargs.get("market_id"), "market_id")
            asset_id = self._account_filter(
                kwargs.get("asset_id") or kwargs.get("contract_id"), "asset_id"
            )
            if not market_id or not asset_id:
                raise MarketConfigurationError(
                    "Polymarket cancel_market_orders requires both market_id and asset_id."
                )
            request = {"market": market_id, "asset_id": asset_id}
            response = clob_auth.cancel_market_orders(market_id, asset_id, headers)

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
                "references": list(POLYMARKET_ORDER_MANAGEMENT_REFERENCES),
            },
            "request": request,
            "response": response,
        }

    def list_candles(
        self,
        contract_id: str,
        *,
        resolution: str = "1h",
        from_timestamp: Optional[float] = None,
        to_timestamp: Optional[float] = None,
    ) -> List[MarketCandle]:
        """Return the documented public CLOB price history for one token.

        Polymarket's ``prices-history`` response is a price point stream rather
        than OHLCV bars.  The normalized candle model repeats each point across
        OHLC and leaves volume unset; the original point is retained in ``raw``.
        """

        self.ensure_capability("price_reading")
        token_id = str(contract_id or "").strip()
        if not token_id:
            raise MarketConfigurationError("Polymarket price history requires a contract id.")
        interval = str(resolution or "").strip().lower()
        if interval not in POLYMARKET_PRICE_HISTORY_INTERVALS:
            allowed = ", ".join(POLYMARKET_PRICE_HISTORY_INTERVALS)
            raise MarketConfigurationError(f"Polymarket price-history interval must be one of: {allowed}.")

        start_ts = self._history_timestamp(from_timestamp, "from_timestamp") if from_timestamp is not None else None
        end_ts = self._history_timestamp(to_timestamp, "to_timestamp") if to_timestamp is not None else None
        if start_ts is not None and end_ts is not None and end_ts <= start_ts:
            raise MarketConfigurationError(
                "Polymarket price history requires to_timestamp greater than from_timestamp."
            )

        payload = clob_rest.get_price_history(
            token_id,
            start_ts=start_ts,
            end_ts=end_ts,
            interval=interval,
        )
        history = payload.get("history") if isinstance(payload, Mapping) else []
        if not isinstance(history, list):
            return []

        candles: List[MarketCandle] = []
        for row in history:
            if not isinstance(row, Mapping):
                continue
            timestamp = self._history_timestamp_value(row.get("t"))
            price = self._finite_probability(row.get("p"))
            if timestamp is None or price is None:
                continue
            candles.append(
                MarketCandle(
                    market_id=self.market_id,
                    contract_id=token_id,
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
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=order.contract_id,
            accepted=True,
            message=(
                f"DRY RUN: would place {order.side.upper()} order for "
                f"{order.size:.4f} shares"
                + (f" at limit {order.limit_price:.4f}" if order.limit_price is not None else "")
            ),
            filled_size=0.0,
            average_price=None,
            raw={"request": order.metadata},
        )

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        self.ensure_capability("live_trading")
        self._validate_order(order)
        preflight = self.preflight_live_order(order)
        if order.limit_price is None:
            raise MarketConfigurationError("Polymarket live trading requires a limit price.")

        geo = self.check_geoblock()
        if geo.get("blocked") is True:
            raise MarketConfigurationError("Polymarket geoblock check blocked live trading.")

        private_key = self.resolve_credential(
            "private_key",
            ("PRIVATE_KEY", "POLYMARKET_PRIVATE_KEY"),
            required=True,
            label="PRIVATE_KEY",
        )

        funder_credential = self.resolve_credential(
            "funder_address",
            ("FUNDER_ADDRESS", "POLYMARKET_FUNDER_ADDRESS", "DEPOSIT_WALLET_ADDRESS"),
            label="FUNDER_ADDRESS",
        )
        funder = funder_credential.value.strip() if funder_credential else None
        try:
            signature_type = parse_signature_type(
                self.config.get("signature_type")
                or self.config.get("polymarket_signature_type")
                or os.getenv("SIGNATURE_TYPE")
                or os.getenv("POLYMARKET_SIGNATURE_TYPE")
                or "0"
            )
        except PolymarketValidationError as exc:
            raise MarketConfigurationError(str(exc)) from exc
        try:
            validate_sdk_trading_readiness(
                private_key=private_key.value,
                signature_type=signature_type,
                funder_address=funder,
            )
        except PolymarketValidationError as exc:
            raise MarketConfigurationError(str(exc)) from exc
        try:
            trader = PolymarketTrader(
                TraderConfig(
                    private_key=private_key.value,
                    funder_address=funder,
                    signature_type=signature_type,
                )
            )
        except PolymarketValidationError as exc:
            raise MarketConfigurationError(str(exc)) from exc
        response = trader.place_limit_order(
            token_id=order.contract_id,
            side=order.side,
            price=order.limit_price,
            size=order.size,
            tif=str(order.metadata.get("tif") or "FOK"),
        )
        return {
            "market_id": self.market_id,
            "contract_id": order.contract_id,
            "live": True,
            "preflight": preflight,
            "response": response,
        }

    def copy_trade_from_activity(self, activity: Mapping[str, Any]) -> PaperOrderResult:
        self.ensure_capability("copy_trading")
        token_id = str(activity.get("asset") or "")
        side = str(activity.get("side") or "").upper()
        try:
            size = float(activity.get("size") or 0.0)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Polymarket activity size must be numeric.") from exc
        price = activity.get("price")
        try:
            limit_price = float(price) if price is not None else None
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Polymarket activity price must be numeric when present.") from exc
        order = PaperOrderRequest(
            market_id=self.market_id,
            contract_id=token_id,
            side=side,
            size=size,
            limit_price=limit_price,
            metadata={"activity": dict(activity)},
        )
        return self.place_paper_order(order)

    def _contracts_from_market(self, market: Mapping[str, Any]) -> List[MarketContract]:
        market_ref = str(market.get("id") or market.get("conditionId") or market.get("slug") or "")
        market_title = str(market.get("question") or market.get("title") or market.get("slug") or market_ref)
        status = self._status_from_raw(market)
        contracts: List[MarketContract] = []
        try:
            outcomes = self.parse_market_outcomes(dict(market))
        except Exception:
            outcomes = []
        for outcome in outcomes:
            if not outcome.token_id:
                continue
            contracts.append(
                MarketContract(
                    market_id=self.market_id,
                    contract_id=outcome.token_id,
                    event_id=market_ref,
                    title=market_title,
                    outcome=outcome.outcome,
                    url=str(market.get("url") or ""),
                    status=status,
                    raw={"market": market, "outcome": outcome},
                )
            )
        return contracts

    def _validate_order(self, order: PaperOrderRequest) -> None:
        self.ensure_order_market(order)
        if not str(order.contract_id or "").strip():
            raise MarketConfigurationError("Polymarket order requires a contract id.")
        side = str(order.side or "").upper()
        if side not in ("BUY", "SELL"):
            raise MarketConfigurationError("Polymarket order side must be BUY or SELL.")
        if not self._is_positive_number(order.size):
            raise MarketConfigurationError("Polymarket order size must be positive.")
        if order.limit_price is not None and self._safe_probability(order.limit_price) is None:
            raise MarketConfigurationError("Polymarket limit price must be between 0 and 1.")

    def _l2_read_headers(self) -> Dict[str, str]:
        configured = self.config.get("polymarket_l2_headers")
        source = configured if isinstance(configured, Mapping) else {}
        headers: Dict[str, str] = {}
        for name in clob_auth.REQUIRED_L2_HEADERS:
            value = source.get(name) or source.get(name.lower())
            if value in (None, ""):
                credential = self.resolve_credential(name.lower(), (name,), label=name)
                value = credential.value if credential else None
            if value not in (None, ""):
                headers[name] = str(value)
        missing = [name for name in clob_auth.REQUIRED_L2_HEADERS if name not in headers]
        if missing:
            raise MarketConfigurationError(
                "Polymarket authenticated trade history requires explicit L2 headers: " + ", ".join(missing)
            )
        return headers

    @staticmethod
    def _account_filter(value: Any, label: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if len(text) > 256 or any(not char.isprintable() for char in text):
            raise MarketConfigurationError(f"Polymarket {label} must be printable and at most 256 characters.")
        return text

    @staticmethod
    def _account_cursor(value: Any) -> str:
        text = str(value or "").strip()
        if len(text) > 512 or any(not char.isprintable() for char in text):
            raise MarketConfigurationError("Polymarket account cursor must be printable and at most 512 characters.")
        return text

    @staticmethod
    def _account_limit(value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Polymarket account limit must be an integer between 1 and 500.") from exc
        if parsed < 1 or parsed > 500:
            raise MarketConfigurationError("Polymarket account limit must be between 1 and 500.")
        return parsed

    @staticmethod
    def _account_timestamp(value: Any, label: str) -> int:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"Polymarket {label} timestamp must be numeric.") from exc
        if not math.isfinite(parsed) or parsed < 0:
            raise MarketConfigurationError(f"Polymarket {label} timestamp must be a finite non-negative number.")
        return int(parsed)

    @staticmethod
    def _order_management_id(value: Any, *, label: str) -> str:
        text = str(value or "").strip()
        # Polymarket's API examples use shortened hashes while live order ids
        # are commonly 32-byte values.  Keep the path-safe hexadecimal shape
        # without rejecting documented/test identifiers solely on length.
        if not re.fullmatch(r"0x[0-9a-fA-F]{40,128}", text) or len(text[2:]) % 2:
            raise MarketConfigurationError(f"Polymarket {label} must be a 0x-prefixed hexadecimal order hash.")
        return text

    @classmethod
    def _order_management_ids(cls, value: Any) -> List[str]:
        if not isinstance(value, (list, tuple)):
            raise MarketConfigurationError("Polymarket cancel_orders requires a JSON array of order hashes.")
        if not value or len(value) > 3000:
            raise MarketConfigurationError("Polymarket cancel_orders requires between 1 and 3000 order hashes.")
        normalized: List[str] = []
        seen = set()
        for item in value:
            order_id = cls._order_management_id(item, label="order_id")
            if order_id not in seen:
                normalized.append(order_id)
                seen.add(order_id)
        return normalized

    @staticmethod
    def _history_timestamp(value: Any, label: str) -> int:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"Polymarket {label} timestamp must be numeric.") from exc
        if not math.isfinite(parsed) or parsed < 0:
            raise MarketConfigurationError(f"Polymarket {label} timestamp must be a finite non-negative number.")
        return int(parsed)

    @classmethod
    def _history_timestamp_value(cls, value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) and parsed >= 0 else None

    @staticmethod
    def _finite_probability(value: Any) -> Optional[float]:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) and 0.0 <= parsed <= 1.0 else None

    @staticmethod
    def _fixed_trade_size(value: Any) -> Optional[float]:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(parsed) or parsed <= 0:
            return None
        # The documented CLOB trade schema uses fixed-math values with six
        # decimal places for size.  Preserve ordinary decimal fixtures too.
        return parsed / 1_000_000.0 if parsed >= 1_000_000 else parsed

    @staticmethod
    def _levels(raw_levels: Any, *, descending: bool = False) -> List[OrderBookLevel]:
        levels: List[OrderBookLevel] = []
        if not isinstance(raw_levels, list):
            return levels
        for raw in raw_levels:
            if not isinstance(raw, Mapping):
                continue
            try:
                price = PolymarketAdapter._safe_probability(raw.get("price"))
                size = float(raw.get("size") or 0.0)
            except (KeyError, TypeError, ValueError):
                continue
            if price is None or not PolymarketAdapter._is_positive_number(size):
                continue
            levels.append(OrderBookLevel(price=price, size=size))
        levels.sort(key=lambda level: level.price, reverse=descending)
        return levels

    @staticmethod
    def _status_from_raw(raw: Mapping[str, Any]) -> str:
        if raw.get("closed") is True:
            return "closed"
        if raw.get("active") is True:
            return "active"
        return str(raw.get("status") or "")

    @staticmethod
    def _safe_probability(value: Any) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number) or number < 0 or number > 1:
            return None
        return number

    @staticmethod
    def _is_positive_number(value: Any) -> bool:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False
        return math.isfinite(number) and number > 0
