from __future__ import annotations

import base64
import math
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlparse

from .base import MarketAdapter
from .catalog import get_market_metadata
from .errors import MarketConfigurationError, MarketHTTPError
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


DEFAULT_KALSHI_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
KALSHI_ORDER_PATH = "/portfolio/events/orders"
KALSHI_ORDER_MANAGEMENT_CONFIRMATION = "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
KALSHI_ORDER_MANAGEMENT_MAX_BATCH = 50
KALSHI_ORDER_MANAGEMENT_REFERENCES = (
    "https://docs.kalshi.com/api-reference/orders/cancel-order-v2",
    "https://docs.kalshi.com/api-reference/orders/batch-cancel-orders-v2",
    "https://docs.kalshi.com/api-reference/orders/decrease-order-v2",
)
KALSHI_ACCOUNT_REFERENCES = (
    "https://docs.kalshi.com/api-reference/portfolio/get-fills",
    "https://docs.kalshi.com/getting_started/order_direction",
)


class KalshiAdapter(MarketAdapter):
    """Kalshi adapter using the documented REST API surface."""

    metadata = get_market_metadata("kalshi")
    # Private, signed portfolio reads are kept separate from public history.
    # The explicit allow-list is also consumed by the shared CLI/API account
    # route so arbitrary authenticated paths can never be requested.
    account_recovery_operations = (
        "active_orders",
        "order_history",
        "fills",
        "positions",
        "settlements",
        "balance",
        "queue_positions",
    )
    order_management_operations = (
        "cancel_order",
        "batch_cancel_orders",
        "decrease_order",
    )

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        credential_sources = []
        for config_key, env_vars, label in (
            ("kalshi_api_key_id", ("KALSHI_API_KEY_ID",), "KALSHI_API_KEY_ID"),
            ("kalshi_private_key_path", ("KALSHI_PRIVATE_KEY_PATH",), "KALSHI_PRIVATE_KEY_PATH"),
            ("kalshi_private_key_pem", ("KALSHI_PRIVATE_KEY_PEM",), "KALSHI_PRIVATE_KEY_PEM"),
        ):
            credential = self.resolve_credential(config_key, env_vars, label=label)
            if credential:
                credential_sources.append({"name": credential.name, "source": credential.source})
        health.update(
            {
                "api_base_url": self.api_base_url,
                "live_trading_enabled": self.config_bool("live_trading_enabled", False),
                "credential_sources": credential_sources,
                "account_recovery_operations": list(self.account_recovery_operations),
                "account_recovery_endpoints": list(KALSHI_ACCOUNT_REFERENCES),
                "order_management_operations": list(self.order_management_operations),
                "order_management_enabled": self.config_bool("kalshi_order_management_enabled", False),
                "order_management_endpoints": list(KALSHI_ORDER_MANAGEMENT_REFERENCES),
            }
        )
        return health

    @property
    def api_base_url(self) -> str:
        configured = self.config.get("kalshi_api_base_url") or self.config.get("api_base_url")
        return str(configured or DEFAULT_KALSHI_BASE_URL).rstrip("/")

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = max(1, min(int(limit or 50), 100))
        markets = self._fetch_markets(limit=max(desired * 3, desired))
        q = str(query or "").strip().lower()
        if q:
            markets = [market for market in markets if self._market_matches_query(market, q)]

        grouped: Dict[str, List[Mapping[str, Any]]] = {}
        for market in markets:
            event_id = self._event_id_for_market(market)
            if event_id:
                grouped.setdefault(event_id, []).append(market)

        events: List[MarketEvent] = []
        for event_id, event_markets in grouped.items():
            first = event_markets[0]
            events.append(
                MarketEvent(
                    market_id=self.market_id,
                    event_id=event_id,
                    title=self._event_title(first),
                    url=self._market_url(first),
                    status=self._event_status(event_markets),
                    raw={"markets": list(event_markets)},
                )
            )
            if len(events) >= desired:
                break
        return events

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        ref = str(event_id or "").strip().upper()
        if not ref:
            return []

        markets = self._fetch_markets(event_ticker=ref, limit=1000)
        if not markets:
            market = self._get_market(ref)
            markets = [market] if market else []

        contracts: List[MarketContract] = []
        for market in markets:
            contracts.extend(self._contracts_from_market(market))
        return contracts

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        orderbook = self.get_orderbook(contract_id)
        bid = orderbook.bids[0].price if orderbook.bids else None
        ask = orderbook.asks[0].price if orderbook.asks else None
        midpoint = (bid + ask) / 2.0 if bid is not None and ask is not None else None
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=contract_id,
            bid=bid,
            ask=ask,
            midpoint=midpoint,
            source="kalshi_orderbook",
            raw=orderbook.raw,
        )

    def get_orderbook(self, contract_id: str) -> OrderBookSnapshot:
        self.ensure_capability("orderbook_reading")
        ticker, outcome = self._split_contract_id(contract_id)
        payload = self._get(f"/markets/{ticker}/orderbook")
        book = self._orderbook_payload(payload)

        yes_bids = self._levels(
            book.get("yes_dollars")
            or book.get("yes")
            or book.get("yes_bids")
            or book.get("yesBid")
            or [],
            descending=True,
        )
        no_bids = self._levels(
            book.get("no_dollars")
            or book.get("no")
            or book.get("no_bids")
            or book.get("noBid")
            or [],
            descending=True,
        )

        if outcome == "yes":
            bids = yes_bids
            asks = self._asks_from_opposite_bids(no_bids)
        else:
            bids = no_bids
            asks = self._asks_from_opposite_bids(yes_bids)

        return OrderBookSnapshot(
            market_id=self.market_id,
            contract_id=self._contract_id(ticker, outcome),
            bids=bids,
            asks=asks,
            raw=payload if isinstance(payload, dict) else {},
        )

    def list_trades(
        self,
        contract_id: str,
        *,
        limit: int = 50,
        before: Optional[float] = None,
        after: Optional[float] = None,
    ) -> List[MarketTrade]:
        """Return normalized public Kalshi trades for one binary outcome.

        Kalshi's documented ``GET /markets/trades`` feed is public and can be
        filtered by ticker and timestamp.  Its ``taker_outcome_side`` field is
        an outcome (YES/NO), rather than a buy/sell direction, so that value is
        retained in the normalized ``side`` field and the raw row remains
        available for callers that need the exchange-specific semantics.
        """

        ticker, outcome = self._split_contract_id(contract_id)
        params: Dict[str, Any] = {
            "ticker": ticker,
            "limit": self._history_limit(limit),
        }
        if after is not None:
            params["min_ts"] = self._history_timestamp(after, "after")
        if before is not None:
            params["max_ts"] = self._history_timestamp(before, "before")
        payload = self._get("/markets/trades", params=params)
        rows = payload.get("trades") if isinstance(payload, Mapping) else []
        if not isinstance(rows, list):
            return []

        trades: List[MarketTrade] = []
        for raw in rows:
            if not isinstance(raw, Mapping):
                continue
            row_ticker = str(raw.get("ticker") or "").strip().upper()
            if row_ticker and row_ticker != ticker:
                continue
            row_outcome = str(raw.get("taker_outcome_side") or raw.get("taker_side") or "").strip().lower()
            if row_outcome and row_outcome not in {"yes", "no"}:
                continue
            if row_outcome and row_outcome != outcome:
                continue
            price = self._trade_price(raw, outcome)
            size = self._positive_number(raw.get("count_fp") or raw.get("count") or raw.get("quantity"))
            trade_id = str(raw.get("trade_id") or raw.get("tradeId") or "").strip()
            if price is None or size is None or not trade_id:
                continue
            timestamp = self._timestamp_seconds(raw.get("created_time") or raw.get("created_ts") or raw.get("timestamp"))
            trades.append(
                MarketTrade(
                    market_id=self.market_id,
                    contract_id=self._contract_id(ticker, outcome),
                    trade_id=trade_id,
                    side=(row_outcome or outcome).upper(),
                    price=price,
                    size=size,
                    timestamp=timestamp,
                    raw=dict(raw),
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
        """Return normalized Kalshi OHLCV history for one outcome.

        The official single-market endpoint requires a series ticker and a
        bounded time range.  The adapter obtains the series from the public
        market record, maps the common resolutions to Kalshi's documented
        1/60/1440-minute intervals, and complements YES prices for NO candles.
        """

        ticker, outcome = self._split_contract_id(contract_id)
        interval = self._candle_interval(resolution)
        market = self._get_market(ticker)
        series_ticker = str((market or {}).get("series_ticker") or self.config.get("kalshi_series_ticker") or "").strip().upper()
        if not series_ticker:
            raise MarketConfigurationError(
                f"Kalshi market {ticker} did not provide a series_ticker required for candlestick history."
            )

        end_ts = self._history_timestamp(to_timestamp, "to_timestamp") if to_timestamp is not None else int(time.time())
        default_lookback = max(interval * 100, 3600)
        start_ts = (
            self._history_timestamp(from_timestamp, "from_timestamp")
            if from_timestamp is not None
            else max(0, end_ts - default_lookback)
        )
        if end_ts <= start_ts:
            raise MarketConfigurationError("Kalshi candle history requires to_timestamp greater than from_timestamp.")

        path = f"/series/{series_ticker}/markets/{ticker}/candlesticks"
        payload = self._get(
            path,
            params={
                "start_ts": start_ts,
                "end_ts": end_ts,
                "period_interval": interval,
                "include_latest_before_start": False,
            },
        )
        rows = payload.get("candlesticks") if isinstance(payload, Mapping) else []
        if not isinstance(rows, list):
            return []

        candles: List[MarketCandle] = []
        for raw in rows:
            if not isinstance(raw, Mapping):
                continue
            timestamp = self._timestamp_seconds(raw.get("end_period_ts") or raw.get("end_period"))
            values = self._candle_values(raw, outcome)
            if timestamp is None or values is None:
                continue
            volume = self._nonnegative_number(raw.get("volume_fp") or raw.get("volume"))
            candles.append(
                MarketCandle(
                    market_id=self.market_id,
                    contract_id=self._contract_id(ticker, outcome),
                    timestamp=timestamp,
                    open=values[0],
                    high=values[1],
                    low=values[2],
                    close=values[3],
                    volume=volume,
                    raw=dict(raw),
                )
            )
        return candles

    def get_account_orders(
        self,
        *,
        status: str = "resting",
        ticker: str = "",
        event_ticker: str = "",
        limit: int = 100,
        cursor: str = "",
        min_timestamp: Optional[float] = None,
        max_timestamp: Optional[float] = None,
        subaccount: Optional[int] = None,
        historical: bool = False,
    ) -> Any:
        """Read signed active or historical Kalshi orders."""

        params = self._account_common_params(
            ticker=ticker,
            event_ticker=event_ticker,
            limit=limit,
            cursor=cursor,
            min_timestamp=min_timestamp,
            max_timestamp=max_timestamp,
            subaccount=subaccount,
        )
        normalized_status = self._account_status(status)
        if normalized_status:
            params["status"] = normalized_status
        path = "/historical/orders" if historical else "/portfolio/orders"
        return self._authenticated_get(path, params=params)

    def get_account_fills(
        self,
        *,
        ticker: str = "",
        order_id: str = "",
        limit: int = 100,
        cursor: str = "",
        min_timestamp: Optional[float] = None,
        max_timestamp: Optional[float] = None,
        subaccount: Optional[int] = None,
        historical: bool = False,
    ) -> Any:
        """Read signed member fills, including the optional historical feed."""

        params = self._account_common_params(
            ticker=ticker,
            limit=limit,
            cursor=cursor,
            min_timestamp=min_timestamp,
            max_timestamp=max_timestamp,
            subaccount=subaccount,
        )
        normalized_order_id = self._account_text(order_id, "order_id", max_length=128)
        if normalized_order_id:
            params["order_id"] = normalized_order_id
        path = "/historical/fills" if historical else "/portfolio/fills"
        return self._authenticated_get(path, params=params)

    def get_account_positions(
        self,
        *,
        ticker: str = "",
        event_ticker: str = "",
        count_filter: str = "",
        limit: int = 100,
        cursor: str = "",
        subaccount: Optional[int] = None,
    ) -> Any:
        """Read signed unsettled market/event positions."""

        params = self._account_common_params(ticker=ticker, limit=limit, cursor=cursor, subaccount=subaccount)
        normalized_event = self._account_text(event_ticker, "event_ticker", max_length=128)
        if normalized_event:
            params["event_ticker"] = normalized_event
        normalized_filter = self._account_text(count_filter, "count_filter", max_length=64)
        if normalized_filter:
            values = {part.strip() for part in normalized_filter.split(",") if part.strip()}
            if not values or not values.issubset({"position", "total_traded"}):
                raise MarketConfigurationError("Kalshi count_filter must contain only position,total_traded.")
            params["count_filter"] = ",".join(value for value in ("position", "total_traded") if value in values)
        return self._authenticated_get("/portfolio/positions", params=params)

    def get_account_settlements(
        self,
        *,
        ticker: str = "",
        event_ticker: str = "",
        limit: int = 100,
        cursor: str = "",
        min_timestamp: Optional[float] = None,
        max_timestamp: Optional[float] = None,
        subaccount: Optional[int] = None,
    ) -> Any:
        """Read signed settled-position history."""

        params = self._account_common_params(
            ticker=ticker,
            event_ticker=event_ticker,
            limit=limit,
            cursor=cursor,
            min_timestamp=min_timestamp,
            max_timestamp=max_timestamp,
            subaccount=subaccount,
        )
        return self._authenticated_get("/portfolio/settlements", params=params)

    def get_account_balance(self, *, subaccount: Optional[int] = None) -> Any:
        """Read the signed account balance without exposing credentials."""

        params: Dict[str, Any] = {}
        if subaccount is not None:
            params["subaccount"] = self._account_subaccount(subaccount)
        return self._authenticated_get("/portfolio/balance", params=params)

    def get_queue_positions(
        self,
        *,
        ticker: str = "",
        event_ticker: str = "",
        subaccount: Optional[int] = None,
    ) -> Any:
        """Read queue positions for the account's resting orders."""

        normalized_ticker = self._account_text(ticker, "ticker", max_length=128)
        normalized_event = self._account_text(event_ticker, "event_ticker", max_length=128)
        if not normalized_ticker and not normalized_event:
            raise MarketConfigurationError("Kalshi queue positions require ticker or event_ticker.")
        params: Dict[str, Any] = {}
        if normalized_ticker:
            params["market_tickers"] = normalized_ticker
        if normalized_event:
            params["event_ticker"] = normalized_event
        if subaccount is not None:
            params["subaccount"] = self._account_subaccount(subaccount)
        return self._authenticated_get("/portfolio/orders/queue_positions", params=params)

    def account_recovery(self, operation: str, **kwargs: Any) -> Any:
        """Dispatch one validated, documented Kalshi portfolio read."""

        normalized = str(operation or "").strip().lower()
        common = {
            "ticker": kwargs.get("ticker", ""),
            "event_ticker": kwargs.get("event_ticker", ""),
            "limit": kwargs.get("limit", 100),
            "cursor": kwargs.get("cursor", ""),
            "min_timestamp": kwargs.get("min_timestamp"),
            "max_timestamp": kwargs.get("max_timestamp"),
            "subaccount": kwargs.get("subaccount"),
        }
        if normalized == "active_orders":
            return self.get_account_orders(status="resting", **common, historical=False)
        if normalized == "order_history":
            return self.get_account_orders(
                status=kwargs.get("status", "executed"),
                **common,
                historical=bool(kwargs.get("historical", False)),
            )
        if normalized == "fills":
            fills_common = {key: value for key, value in common.items() if key != "event_ticker"}
            return self.get_account_fills(
                **fills_common,
                order_id=kwargs.get("order_id", ""),
                historical=bool(kwargs.get("historical", False)),
            )
        if normalized == "positions":
            positions_common = {
                key: value
                for key, value in common.items()
                if key in {"ticker", "event_ticker", "limit", "cursor", "subaccount"}
            }
            return self.get_account_positions(
                count_filter=kwargs.get("count_filter", ""),
                **positions_common,
            )
        if normalized == "settlements":
            return self.get_account_settlements(**common)
        if normalized == "balance":
            return self.get_account_balance(subaccount=kwargs.get("subaccount"))
        if normalized == "queue_positions":
            return self.get_queue_positions(
                ticker=kwargs.get("ticker", ""),
                event_ticker=kwargs.get("event_ticker", ""),
                subaccount=kwargs.get("subaccount"),
            )
        supported = ", ".join(self.account_recovery_operations)
        raise MarketConfigurationError(f"Kalshi account recovery operation must be one of: {supported}.")

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        self._validate_order(order)
        ticker, outcome = self._split_contract_id(order.contract_id)
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=self._contract_id(ticker, outcome),
            accepted=True,
            message=(
                f"DRY RUN: would place Kalshi {order.side.upper()} order for "
                f"{order.size:.4f} {outcome.upper()} contracts"
                + (f" at limit {order.limit_price:.4f}" if order.limit_price is not None else "")
            ),
            filled_size=0.0,
            average_price=order.limit_price,
            raw={"request": dict(order.metadata), "ticker": ticker, "outcome": outcome},
        )

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        self.ensure_capability("live_trading")
        self._validate_order(order)
        preflight = self.preflight_live_order(order)
        if order.limit_price is None:
            raise MarketConfigurationError("Kalshi live trading requires a limit price.")

        payload = self._build_live_order_payload(order)
        headers = self._auth_headers("POST", KALSHI_ORDER_PATH)
        headers["Content-Type"] = "application/json"
        response = self.runtime.request_json(
            "POST",
            self._url(KALSHI_ORDER_PATH),
            json_body=payload,
            headers=headers,
        )
        return {
            "market_id": self.market_id,
            "contract_id": order.contract_id,
            "live": True,
            "preflight": preflight,
            "request": payload,
            "response": response,
        }

    def copy_trade_from_activity(self, activity: Mapping[str, Any]) -> PaperOrderResult:
        """Build a local copy preview from an authenticated Kalshi fill.

        Kalshi's documented portfolio fills expose the ticker, outcome side,
        bid/ask book side, fixed-point price, count, and fill identity.  The
        adapter maps bid/ask to BUY/SELL for the selected YES/NO contract and
        only creates a local paper preview; it never submits a live order.
        """

        self.ensure_capability("copy_trading")
        contract_id = str(activity.get("asset") or activity.get("contract_id") or "").strip()
        ticker = str(activity.get("ticker") or activity.get("market_id") or activity.get("marketId") or "").strip()
        outcome = str(
            activity.get("outcome_side")
            or activity.get("outcome")
            or activity.get("position")
            or ""
        ).strip().lower()
        if contract_id:
            parsed_ticker, parsed_outcome = self._split_contract_id(contract_id)
            if ticker and parsed_ticker != ticker.upper():
                raise MarketConfigurationError("Kalshi activity ticker does not match contract_id.")
            if outcome and parsed_outcome != outcome:
                raise MarketConfigurationError("Kalshi activity outcome does not match contract_id.")
            ticker, outcome = parsed_ticker, parsed_outcome
        else:
            if not ticker or outcome not in {"yes", "no"}:
                raise MarketConfigurationError("Kalshi activity requires contract_id or ticker plus outcome_side.")
            ticker = ticker.upper()
            contract_id = self._contract_id(ticker, outcome)

        raw_book_side = str(activity.get("book_side") or activity.get("bookSide") or "").strip().lower()
        if raw_book_side in {"bid", "buy"}:
            side = "BUY"
        elif raw_book_side in {"ask", "sell"}:
            side = "SELL"
        else:
            side_value = str(activity.get("trade_side") or activity.get("direction") or activity.get("action") or "").strip().upper()
            if side_value in {"BUY", "SELL"}:
                side = side_value
            else:
                raise MarketConfigurationError("Kalshi activity requires documented bid/ask direction.")

        price = self._trade_price(activity, outcome)
        if price is None or price <= 0.0 or price >= 1.0:
            raise MarketConfigurationError("Kalshi activity price must be between 0 and 1.")
        size = self._positive_number(
            activity.get("count_fp")
            or activity.get("fill_count_fp")
            or activity.get("count")
            or activity.get("quantity")
            or activity.get("size")
        )
        if size is None:
            raise MarketConfigurationError("Kalshi activity count must be positive and finite.")
        trade_id = str(
            activity.get("fill_id")
            or activity.get("fillId")
            or activity.get("trade_id")
            or activity.get("tradeId")
            or activity.get("id")
            or ""
        ).strip()
        if not trade_id:
            raise MarketConfigurationError("Kalshi activity requires a documented fill id.")
        preview = self.place_paper_order(
            PaperOrderRequest(
                market_id=self.market_id,
                contract_id=contract_id,
                side=side,
                size=size,
                limit_price=price,
                metadata={"activity": dict(activity), "source": "kalshi_authenticated_portfolio_fills"},
            )
        )
        preview.raw["source"] = "kalshi_authenticated_portfolio_fills"
        preview.raw["activity"] = dict(activity)
        return preview

    def manage_orders(self, operation: str, **kwargs: Any) -> Dict[str, Any]:
        """Run a guarded Kalshi V2 order-management mutation.

        Kalshi's event-market mutations use signed REST requests.  The
        adapter signs only the fixed, documented paths below; callers cannot
        provide an arbitrary URL or method.  A separate opt-in and exact
        confirmation are required in addition to the shared live-trading
        acknowledgement so account recovery and order placement settings
        cannot accidentally arm destructive mutations.
        """

        normalized = str(operation or "").strip().lower()
        if normalized not in self.order_management_operations:
            supported = ", ".join(self.order_management_operations)
            raise MarketConfigurationError(
                f"Kalshi order-management operation must be one of: {supported}."
            )
        self.ensure_capability("live_trading")
        if not self.config_bool("kalshi_order_management_enabled", False):
            raise MarketConfigurationError(
                "Kalshi order management is disabled by adapter config. "
                "Set kalshi_order_management_enabled=true only after reviewing live-order controls."
            )
        self.ensure_live_trading_enabled("order management")
        if str(kwargs.get("confirm_order_management") or "").strip() != KALSHI_ORDER_MANAGEMENT_CONFIRMATION:
            raise MarketConfigurationError(
                "Kalshi order management requires exact confirmation text "
                f"{KALSHI_ORDER_MANAGEMENT_CONFIRMATION}."
            )
        if bool(kwargs.get("async_request")):
            raise MarketConfigurationError("Kalshi order-management requests are synchronous.")

        default_subaccount = self._order_management_subaccount(kwargs.get("subaccount"))
        default_exchange_index = self._order_management_exchange_index(kwargs.get("exchange_index"))
        request_body: Optional[Dict[str, Any]] = None
        request_params: Dict[str, Any] = {}

        if normalized == "cancel_order":
            order_id = self._order_management_id(kwargs.get("order_id"), label="order_id")
            path = f"/portfolio/events/orders/{order_id}"
            if default_subaccount is not None:
                request_params["subaccount"] = default_subaccount
            if default_exchange_index is not None:
                request_params["exchange_index"] = default_exchange_index
            response = self._authenticated_request("DELETE", path, params=request_params)
        elif normalized == "batch_cancel_orders":
            orders = self._batch_cancel_payload(
                kwargs.get("orders"),
                default_subaccount=default_subaccount,
                default_exchange_index=default_exchange_index,
            )
            request_body = {"orders": orders}
            response = self._authenticated_request(
                "DELETE", "/portfolio/events/orders/batched", json_body=request_body
            )
        else:
            order_id = self._order_management_id(kwargs.get("order_id"), label="order_id")
            reduce_by = self._order_management_reduction(kwargs.get("reduce_by"), label="reduce_by")
            reduce_to = self._order_management_reduction(kwargs.get("reduce_to"), label="reduce_to", allow_zero=True)
            if (reduce_by is None) == (reduce_to is None):
                raise MarketConfigurationError("Kalshi decrease_order requires exactly one of reduce_by or reduce_to.")
            request_body = {
                ("reduce_by" if reduce_by is not None else "reduce_to"): self._fixed_decimal(
                    reduce_by if reduce_by is not None else reduce_to
                )
            }
            if default_exchange_index is not None:
                request_body["exchange_index"] = default_exchange_index
            if default_subaccount is not None:
                request_params["subaccount"] = default_subaccount
            response = self._authenticated_request(
                "POST", f"/portfolio/events/orders/{order_id}/decrease", params=request_params, json_body=request_body
            )

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
                "references": list(KALSHI_ORDER_MANAGEMENT_REFERENCES),
            },
            "request": {"params": request_params, "body": request_body},
            "response": response,
        }

    def _fetch_markets(
        self,
        *,
        event_ticker: Optional[str] = None,
        limit: int = 100,
    ) -> List[Mapping[str, Any]]:
        params: Dict[str, Any] = {
            "limit": max(1, min(int(limit or 100), 1000)),
        }
        status = str(self.config.get("kalshi_market_status") or self.config.get("market_status") or "open").strip()
        if status:
            params["status"] = status
        if event_ticker:
            params["event_ticker"] = event_ticker
        data = self._get("/markets", params=params)
        markets = data.get("markets") if isinstance(data, Mapping) else []
        return [market for market in markets if isinstance(market, Mapping)] if isinstance(markets, list) else []

    def _get_market(self, ticker: str) -> Optional[Mapping[str, Any]]:
        data = self._get(f"/markets/{ticker}")
        if isinstance(data, Mapping):
            market = data.get("market")
            if isinstance(market, Mapping):
                return market
            if "ticker" in data:
                return data
        return None

    def _get(self, path: str, *, params: Optional[Mapping[str, Any]] = None) -> Any:
        return self.runtime.get_json(self._url(path), params=params)

    def _url(self, path: str) -> str:
        clean_path = "/" + str(path or "").strip("/")
        return f"{self.api_base_url}{clean_path}"

    def _request_path(self, path: str) -> str:
        return urlparse(self._url(path)).path

    @staticmethod
    def _history_limit(value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Kalshi history limit must be an integer between 1 and 1000.") from exc
        if parsed < 1 or parsed > 1000:
            raise MarketConfigurationError("Kalshi history limit must be between 1 and 1000.")
        return parsed

    @staticmethod
    def _history_timestamp(value: Any, label: str) -> int:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"Kalshi {label} timestamp must be numeric.") from exc
        if not math.isfinite(parsed) or parsed < 0:
            raise MarketConfigurationError(f"Kalshi {label} timestamp must be a finite non-negative number.")
        return int(parsed)

    @staticmethod
    def _candle_interval(resolution: str) -> int:
        normalized = str(resolution or "").strip().lower()
        intervals = {"1m": 1, "1h": 60, "1d": 1440, "60m": 60, "1440m": 1440}
        try:
            return intervals[normalized]
        except KeyError as exc:
            raise MarketConfigurationError("Kalshi candle resolution must be 1m, 1h, or 1d.") from exc

    @classmethod
    def _trade_price(cls, raw: Mapping[str, Any], outcome: str) -> Optional[float]:
        keys = (
            ("yes_price_dollars", "yes_price") if outcome == "yes" else ("no_price_dollars", "no_price")
        ) + ("price_dollars", "price")
        for key in keys:
            value = cls._safe_probability(raw.get(key))
            if value is not None:
                return value
        return None

    @classmethod
    def _candle_values(cls, raw: Mapping[str, Any], outcome: str) -> Optional[Tuple[float, float, float, float]]:
        price = raw.get("price")
        if not isinstance(price, Mapping):
            price = raw.get("yes_bid") if outcome == "yes" else raw.get("no_bid")
        if not isinstance(price, Mapping):
            return None
        values: Dict[str, float] = {}
        for name in ("open", "high", "low", "close"):
            value = cls._safe_probability(price.get(f"{name}_dollars") or price.get(name))
            if value is None:
                return None
            values[name] = value
        if outcome == "no":
            return (
                round(1.0 - values["open"], 10),
                round(1.0 - values["low"], 10),
                round(1.0 - values["high"], 10),
                round(1.0 - values["close"], 10),
            )
        return (values["open"], values["high"], values["low"], values["close"])

    @staticmethod
    def _timestamp_seconds(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)):
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(parsed):
                return None
            return parsed / 1000.0 if parsed > 10_000_000_000 else parsed
        text = str(value).strip()
        try:
            parsed = float(text)
            return parsed / 1000.0 if parsed > 10_000_000_000 else parsed
        except ValueError:
            pass
        try:
            parsed_dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed_dt.tzinfo is None:
                parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
            return parsed_dt.timestamp()
        except ValueError:
            return None

    @staticmethod
    def _positive_number(value: Any) -> Optional[float]:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) and parsed > 0 else None

    @staticmethod
    def _nonnegative_number(value: Any) -> Optional[float]:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) and parsed >= 0 else None

    def _contracts_from_market(self, market: Mapping[str, Any]) -> List[MarketContract]:
        ticker = str(market.get("ticker") or "").strip().upper()
        if not ticker:
            return []
        event_id = self._event_id_for_market(market) or ticker
        title = str(market.get("title") or market.get("subtitle") or ticker)
        status = self._status_from_market(market)
        return [
            MarketContract(
                market_id=self.market_id,
                contract_id=self._contract_id(ticker, "yes"),
                event_id=event_id,
                title=f"{title} - Yes",
                outcome="Yes",
                url=self._market_url(market),
                status=status,
                raw={"market": dict(market), "outcome": "yes"},
            ),
            MarketContract(
                market_id=self.market_id,
                contract_id=self._contract_id(ticker, "no"),
                event_id=event_id,
                title=f"{title} - No",
                outcome="No",
                url=self._market_url(market),
                status=status,
                raw={"market": dict(market), "outcome": "no"},
            ),
        ]

    def _build_live_order_payload(self, order: PaperOrderRequest) -> Dict[str, Any]:
        ticker, outcome = self._split_contract_id(order.contract_id)
        if order.limit_price is None:
            raise MarketConfigurationError("Kalshi live trading requires a limit price.")
        side, yes_side_price = self._yes_side_order(order.side, outcome, order.limit_price)
        time_in_force = str(order.metadata.get("time_in_force") or self.config.get("kalshi_time_in_force") or "fill_or_kill")
        if time_in_force not in {"fill_or_kill", "good_till_canceled", "immediate_or_cancel"}:
            raise MarketConfigurationError("Kalshi time_in_force must be fill_or_kill, good_till_canceled, or immediate_or_cancel.")
        self_trade_prevention = str(
            order.metadata.get("self_trade_prevention_type")
            or self.config.get("kalshi_self_trade_prevention_type")
            or "taker_at_cross"
        )
        if self_trade_prevention not in {"taker_at_cross", "maker"}:
            raise MarketConfigurationError("Kalshi self_trade_prevention_type must be taker_at_cross or maker.")

        payload: Dict[str, Any] = {
            "ticker": ticker,
            "client_order_id": str(order.metadata.get("client_order_id") or f"pmacg-{uuid.uuid4().hex}"),
            "side": side,
            "count": self._fixed_decimal(order.size),
            "price": self._fixed_decimal(yes_side_price, places=4),
            "time_in_force": time_in_force,
            "self_trade_prevention_type": self_trade_prevention,
        }
        for key in ("expiration_time", "post_only", "cancel_order_on_pause", "reduce_only", "subaccount", "order_group_id"):
            if key in order.metadata:
                payload[key] = order.metadata[key]
        if "exchange_index" in order.metadata:
            payload["exchange_index"] = order.metadata["exchange_index"]
        return payload

    @staticmethod
    def _account_text(value: Any, label: str, *, max_length: int = 128) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if len(text) > max_length or any(not char.isprintable() for char in text):
            raise MarketConfigurationError(f"Kalshi {label} must be printable and at most {max_length} characters.")
        return text

    @staticmethod
    def _account_limit(value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Kalshi account limit must be an integer between 1 and 1000.") from exc
        if parsed < 1 or parsed > 1000:
            raise MarketConfigurationError("Kalshi account limit must be between 1 and 1000.")
        return parsed

    @classmethod
    def _account_common_params(
        cls,
        *,
        ticker: str = "",
        event_ticker: str = "",
        limit: int = 100,
        cursor: str = "",
        min_timestamp: Optional[float] = None,
        max_timestamp: Optional[float] = None,
        subaccount: Optional[int] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": cls._account_limit(limit)}
        normalized_ticker = cls._account_text(ticker, "ticker")
        normalized_event = cls._account_text(event_ticker, "event_ticker")
        normalized_cursor = cls._account_text(cursor, "cursor", max_length=2048)
        if normalized_ticker:
            params["ticker"] = normalized_ticker
        if normalized_event:
            params["event_ticker"] = normalized_event
        if normalized_cursor:
            params["cursor"] = normalized_cursor
        if min_timestamp is not None:
            params["min_ts"] = cls._history_timestamp(min_timestamp, "min_timestamp")
        if max_timestamp is not None:
            params["max_ts"] = cls._history_timestamp(max_timestamp, "max_timestamp")
        if "min_ts" in params and "max_ts" in params and params["min_ts"] > params["max_ts"]:
            raise MarketConfigurationError("Kalshi account max_timestamp must not precede min_timestamp.")
        if subaccount is not None:
            params["subaccount"] = cls._account_subaccount(subaccount)
        return params

    @staticmethod
    def _account_status(value: Any) -> str:
        status = str(value or "").strip().lower()
        if not status:
            return ""
        if status not in {"resting", "canceled", "executed"}:
            raise MarketConfigurationError("Kalshi order status must be resting, canceled, or executed.")
        return status

    @staticmethod
    def _account_subaccount(value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Kalshi subaccount must be an integer between 0 and 63.") from exc
        if parsed < 0 or parsed > 63:
            raise MarketConfigurationError("Kalshi subaccount must be between 0 and 63.")
        return parsed

    @classmethod
    def _order_management_id(cls, value: Any, *, label: str) -> str:
        text = str(value or "").strip()
        if not text or len(text) > 128 or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.:" for char in text):
            raise MarketConfigurationError(f"Kalshi {label} must be a safe non-empty identifier (max 128 characters).")
        return text

    @staticmethod
    def _order_management_reduction(value: Any, *, label: str, allow_zero: bool = False) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"Kalshi {label} must be numeric.") from exc
        if not math.isfinite(parsed) or (parsed < 0 if allow_zero else parsed <= 0):
            qualifier = "non-negative" if allow_zero else "positive"
            raise MarketConfigurationError(f"Kalshi {label} must be finite and {qualifier}.")
        return parsed

    @classmethod
    def _order_management_subaccount(cls, value: Any) -> Optional[int]:
        if value in (None, ""):
            return None
        return cls._account_subaccount(value)

    @staticmethod
    def _order_management_exchange_index(value: Any) -> Optional[int]:
        if value in (None, ""):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Kalshi exchange_index must be integer 0.") from exc
        if parsed != 0:
            raise MarketConfigurationError("Kalshi exchange_index must be 0; other shards are not supported by the API.")
        return parsed

    @classmethod
    def _batch_cancel_payload(
        cls,
        value: Any,
        *,
        default_subaccount: Optional[int],
        default_exchange_index: Optional[int],
    ) -> List[Dict[str, Any]]:
        if not isinstance(value, list) or not value:
            raise MarketConfigurationError("Kalshi batch_cancel_orders requires a non-empty orders array.")
        if len(value) > KALSHI_ORDER_MANAGEMENT_MAX_BATCH:
            raise MarketConfigurationError(
                f"Kalshi batch_cancel_orders supports at most {KALSHI_ORDER_MANAGEMENT_MAX_BATCH} orders per request."
            )
        normalized: List[Dict[str, Any]] = []
        for index, item in enumerate(value):
            if not isinstance(item, Mapping):
                raise MarketConfigurationError(f"Kalshi cancellation item {index} must be an object.")
            order_id = cls._order_management_id(item.get("order_id"), label=f"orders[{index}].order_id")
            subaccount = cls._order_management_subaccount(item.get("subaccount", default_subaccount))
            exchange_index = cls._order_management_exchange_index(
                item.get("exchange_index", default_exchange_index)
            )
            entry: Dict[str, Any] = {"order_id": order_id}
            if subaccount is not None:
                entry["subaccount"] = subaccount
            if exchange_index is not None:
                entry["exchange_index"] = exchange_index
            normalized.append(entry)
        return normalized

    def _authenticated_get(self, path: str, *, params: Optional[Mapping[str, Any]] = None) -> Any:
        """Perform one signed GET while retaining query parameters separately.

        Kalshi signs the canonical request path (not an arbitrary caller
        supplied URL).  Query values are passed through ``requests`` only
        after the operation and each value have been validated above.
        """

        headers = self._auth_headers("GET", path)
        headers.update({"Accept": "application/json", "User-Agent": self.runtime.user_agent})
        self.runtime.rate_limiter.wait()
        try:
            response = self.runtime.session.request(
                "GET",
                self._url(path),
                params=dict(params or {}),
                headers=headers,
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

    def _authenticated_request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        """Perform one signed mutation against a fixed Kalshi API path."""

        normalized_method = str(method or "").strip().upper()
        if normalized_method not in {"POST", "DELETE"}:
            raise MarketConfigurationError("Kalshi order-management method is not supported.")
        headers = self._auth_headers(normalized_method, path)
        headers.update({"Accept": "application/json", "User-Agent": self.runtime.user_agent})
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        self.runtime.rate_limiter.wait()
        try:
            response = self.runtime.session.request(
                normalized_method,
                self._url(path),
                params=dict(params or {}),
                json=dict(json_body) if json_body is not None else None,
                headers=headers,
                timeout=self.runtime.timeout_seconds,
            )
        except Exception as exc:
            raise MarketHTTPError(f"{self.market_id} HTTP request failed: {exc}") from exc
        status = int(getattr(response, "status_code", 0) or 0)
        if status >= 400:
            raise MarketHTTPError(f"{self.market_id} HTTP {status}: {str(getattr(response, 'text', ''))[:200]}")
        if status == 204:
            return {}
        try:
            return response.json()
        except ValueError:
            return {}

    def _auth_headers(self, method: str, path: str) -> Dict[str, str]:
        api_key = self.resolve_credential(
            "kalshi_api_key_id",
            ("KALSHI_API_KEY_ID",),
            required=True,
            label="KALSHI_API_KEY_ID",
        )
        private_key_bytes = self._load_private_key_bytes()
        timestamp_ms = str(int(time.time() * 1000))
        request_path = self._request_path(path)
        message = f"{timestamp_ms}{method.upper()}{request_path}"
        signature = self._sign_pss(private_key_bytes, message)
        return {
            "KALSHI-ACCESS-KEY": api_key.value,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": signature,
        }

    def _load_private_key_bytes(self) -> bytes:
        pem = self.resolve_credential(
            "kalshi_private_key_pem",
            ("KALSHI_PRIVATE_KEY_PEM",),
            label="KALSHI_PRIVATE_KEY_PEM",
        )
        if pem:
            return pem.value.encode("utf-8")

        path_credential = self.resolve_credential(
            "kalshi_private_key_path",
            ("KALSHI_PRIVATE_KEY_PATH",),
            required=True,
            label="KALSHI_PRIVATE_KEY_PATH",
        )
        path = Path(path_credential.value).expanduser()
        try:
            return path.read_bytes()
        except OSError as exc:
            raise MarketConfigurationError(f"Kalshi private key file could not be read: {path}") from exc

    def _sign_pss(self, private_key_bytes: bytes, message: str) -> str:
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
        except Exception as exc:
            raise MarketConfigurationError(
                "Kalshi live trading requires the cryptography package. Install project dependencies with pip install -r requirements.txt."
            ) from exc

        password_credential = self.resolve_credential(
            "kalshi_private_key_password",
            ("KALSHI_PRIVATE_KEY_PASSWORD",),
            label="KALSHI_PRIVATE_KEY_PASSWORD",
        )
        password = password_credential.value.encode("utf-8") if password_credential else None
        try:
            private_key = serialization.load_pem_private_key(private_key_bytes, password=password)
            signature = private_key.sign(
                message.encode("utf-8"),
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                hashes.SHA256(),
            )
        except Exception as exc:
            raise MarketConfigurationError("Kalshi private key could not sign the request.") from exc
        return base64.b64encode(signature).decode("ascii")

    def _validate_order(self, order: PaperOrderRequest) -> None:
        self.ensure_order_market(order)
        self._split_contract_id(order.contract_id)
        side = str(order.side or "").upper()
        if side not in {"BUY", "SELL"}:
            raise MarketConfigurationError("Kalshi order side must be BUY or SELL.")
        if not self._is_positive_number(order.size):
            raise MarketConfigurationError("Kalshi order size must be positive.")
        if order.limit_price is not None and self._safe_probability(order.limit_price) is None:
            raise MarketConfigurationError("Kalshi limit price must be between 0 and 1.")

    @staticmethod
    def _orderbook_payload(payload: Any) -> Mapping[str, Any]:
        if not isinstance(payload, Mapping):
            return {}
        for key in ("orderbook_fp", "orderbook", "book"):
            value = payload.get(key)
            if isinstance(value, Mapping):
                return value
        return payload

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
                price = raw.get("price") or raw.get("price_dollars") or raw.get("yes_price") or raw.get("no_price")
                size = raw.get("size") or raw.get("count") or raw.get("quantity") or raw.get("count_fp")
            else:
                continue
            parsed_price = KalshiAdapter._safe_probability(price)
            try:
                parsed_size = float(size)
            except (TypeError, ValueError):
                continue
            if parsed_price is None or not KalshiAdapter._is_positive_number(parsed_size):
                continue
            levels.append(OrderBookLevel(price=parsed_price, size=parsed_size))
        levels.sort(key=lambda level: level.price, reverse=descending)
        return levels

    @staticmethod
    def _asks_from_opposite_bids(levels: List[OrderBookLevel]) -> List[OrderBookLevel]:
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
            if isinstance(value, str) and "." in value:
                return None
            number = number / 100.0
        if number < 0.0 or number > 1.0:
            return None
        return number

    @staticmethod
    def _is_positive_number(value: Any) -> bool:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False
        return math.isfinite(number) and number > 0

    @staticmethod
    def _split_contract_id(contract_id: str) -> Tuple[str, str]:
        raw = str(contract_id or "").strip()
        if not raw:
            raise MarketConfigurationError("Kalshi order requires a contract id.")
        if ":" in raw:
            ticker, outcome = raw.rsplit(":", 1)
        else:
            ticker, outcome = raw, "yes"
        ticker = ticker.strip().upper()
        outcome = outcome.strip().lower()
        if not ticker:
            raise MarketConfigurationError("Kalshi order requires a market ticker.")
        if outcome not in {"yes", "no"}:
            raise MarketConfigurationError("Kalshi contract outcome must be YES or NO.")
        return ticker, outcome

    @staticmethod
    def _contract_id(ticker: str, outcome: str) -> str:
        return f"{ticker.upper()}:{outcome.upper()}"

    @staticmethod
    def _yes_side_order(order_side: str, outcome: str, limit_price: float) -> Tuple[str, float]:
        side = str(order_side or "").upper()
        price = float(limit_price)
        if outcome == "yes":
            return ("bid", price) if side == "BUY" else ("ask", price)
        yes_side_price = 1.0 - price
        return ("ask", yes_side_price) if side == "BUY" else ("bid", yes_side_price)

    @staticmethod
    def _fixed_decimal(value: Any, *, places: int = 2) -> str:
        return f"{float(value):.{places}f}"

    @staticmethod
    def _event_id_for_market(market: Mapping[str, Any]) -> str:
        return str(market.get("event_ticker") or market.get("ticker") or "").strip().upper()

    @staticmethod
    def _event_title(market: Mapping[str, Any]) -> str:
        return str(
            market.get("event_title")
            or market.get("title")
            or market.get("subtitle")
            or market.get("event_ticker")
            or market.get("ticker")
            or ""
        )

    @staticmethod
    def _event_status(markets: List[Mapping[str, Any]]) -> str:
        statuses = [KalshiAdapter._status_from_market(market) for market in markets]
        if any(status in {"open", "active"} for status in statuses):
            return "active"
        return statuses[0] if statuses else ""

    @staticmethod
    def _status_from_market(market: Mapping[str, Any]) -> str:
        status = str(market.get("status") or "").strip().lower()
        return "active" if status == "open" else status

    @staticmethod
    def _market_matches_query(market: Mapping[str, Any], query: str) -> bool:
        haystack = " ".join(
            str(market.get(key) or "")
            for key in (
                "ticker",
                "event_ticker",
                "series_ticker",
                "title",
                "subtitle",
                "yes_sub_title",
                "no_sub_title",
            )
        ).lower()
        return query in haystack

    @staticmethod
    def _market_url(market: Mapping[str, Any]) -> str:
        ticker = str(market.get("ticker") or "").strip()
        return f"https://kalshi.com/markets/{ticker}" if ticker else "https://kalshi.com"
