from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

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


DEFAULT_BETFAIR_RPC_URL = "https://api.betfair.com/exchange/betting/json-rpc/v1"
DEFAULT_BETFAIR_ACCOUNT_RPC_URL = "https://api.betfair.com/exchange/account/json-rpc/v1"
BETFAIR_REFERENCES = (
    "https://developer.betfair.com/",
    "https://support.developer.betfair.com/hc/en-us/categories/360000245252-Exchange-API",
    "https://support.developer.betfair.com/hc/en-us/articles/115003864651-How-do-I-get-started",
    "https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687504/listCurrentOrders",
    "https://support.developer.betfair.com/hc/en-us/articles/360016170431-How-do-I-place-bets-on-handicap-markets",
    "https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687679/listClearedOrders+-+Roll-up+Fields+Available",
    "https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687725/Accounts+API",
    "https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2699900/getAccountDetails",
    "https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687491/cancelOrders",
    "https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687485/updateOrders",
    "https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687487/replaceOrders",
)


class BetfairExchangeAdapter(MarketAdapter):
    """Betfair Exchange adapter using the official Exchange API JSON-RPC."""

    live_order_sides = ("BUY", "SELL", "BACK", "LAY")
    live_order_exposure_model = "exchange_stake_or_lay_liability"
    metadata = get_market_metadata("betfair_exchange")
    account_recovery_operations = (
        "active_orders",
        "cleared_orders",
        "funds",
        "account",
        "statement",
        "currency_rates",
    )
    # Persistence updates and replacements can increase exposure without the
    # original order preflight context. Only cancellation is fail-safe here.
    order_management_operations = ("cancel_orders",)

    def live_order_exposure(
        self,
        order: PaperOrderRequest,
        *,
        size: float,
        limit_price: Optional[float],
    ) -> float:
        if str(order.side or "").upper() not in {"SELL", "LAY"}:
            return size
        if limit_price is None or limit_price > 1:
            raise MarketConfigurationError("Betfair LAY preflight requires a probability greater than 0 and at most 1.")
        return size * ((1.0 / limit_price) - 1.0)

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        app_key = self.resolve_credential("betfair_app_key", ("BETFAIR_APP_KEY",), label="BETFAIR_APP_KEY")
        session = self.resolve_credential(
            "betfair_session_token",
            ("BETFAIR_SESSION_TOKEN",),
            label="BETFAIR_SESSION_TOKEN",
        )
        credential_sources = []
        for credential in (app_key, session):
            if credential:
                credential_sources.append({"name": credential.name, "source": credential.source})
        health.update(
            {
                "api_base_url": self.api_base_url,
                "account_api_base_url": self.account_api_base_url,
                "references": list(BETFAIR_REFERENCES),
                "credential_sources": credential_sources,
                "live_trading_supported": True,
                "live_trading_enabled": self.config_bool("live_trading_enabled", False),
                "account_recovery_operations": list(self.account_recovery_operations),
                "order_management_operations": list(self.order_management_operations),
                "order_management_enabled": self.config_bool("betfair_order_management_enabled", False),
                "authenticated_account_endpoints": [
                    "listCurrentOrders",
                    "listClearedOrders",
                    "getAccountFunds",
                    "getAccountDetails",
                    "getAccountStatement",
                    "listCurrencyRates",
                ],
                "order_management_endpoints": ["cancelOrders"],
            }
        )
        return health

    @property
    def api_base_url(self) -> str:
        configured = self.config.get("betfair_api_base_url") or self.config.get("api_base_url")
        return str(configured or DEFAULT_BETFAIR_RPC_URL).rstrip("/")

    @property
    def account_api_base_url(self) -> str:
        configured = self.config.get("betfair_account_api_base_url")
        return str(configured or DEFAULT_BETFAIR_ACCOUNT_RPC_URL).rstrip("/")

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = max(1, min(int(limit or 50), 100))
        market_filter: Dict[str, Any] = {}
        text_query = str(query or self.config.get("betfair_text_query") or "").strip()
        if text_query:
            market_filter["textQuery"] = text_query
        event_type_ids = self.config.get("betfair_event_type_ids")
        if event_type_ids:
            market_filter["eventTypeIds"] = list(event_type_ids) if isinstance(event_type_ids, list) else [str(event_type_ids)]
        result = self._rpc(
            "SportsAPING/v1.0/listMarketCatalogue",
            {
                "filter": market_filter,
                "marketProjection": ["EVENT", "RUNNER_DESCRIPTION", "MARKET_START_TIME"],
                "sort": "FIRST_TO_START",
                "maxResults": str(desired),
            },
        )
        markets = [item for item in result if isinstance(item, Mapping)] if isinstance(result, list) else []
        return [self._event_from_market(market) for market in markets]

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        market = self._get_market_catalogue(event_id)
        return self._contracts_from_market(market)

    def get_orderbook(self, contract_id: str) -> OrderBookSnapshot:
        self.ensure_capability("orderbook_reading")
        market_id, selection_id = self._split_contract_id(contract_id)
        result = self._rpc(
            "SportsAPING/v1.0/listMarketBook",
            {
                "marketIds": [market_id],
                "priceProjection": {"priceData": ["EX_BEST_OFFERS"], "virtualise": True},
            },
        )
        books = [item for item in result if isinstance(item, Mapping)] if isinstance(result, list) else []
        if not books:
            raise MarketConfigurationError(f"Betfair market {market_id!r} book was not found.")
        runner = self._find_runner(books[0], selection_id)
        if not runner:
            raise MarketConfigurationError(f"Betfair runner {selection_id!r} was not found in market {market_id!r}.")
        ex = runner.get("ex") if isinstance(runner.get("ex"), Mapping) else {}
        bids = self._levels_from_decimal_odds(ex.get("availableToBack"), descending=True)
        asks = self._levels_from_decimal_odds(ex.get("availableToLay"))
        return OrderBookSnapshot(
            market_id=self.market_id,
            contract_id=self._contract_id(market_id, selection_id),
            bids=bids,
            asks=asks,
            raw={"market_book": dict(books[0]), "runner": dict(runner)},
        )

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        market_id, selection_id = self._split_contract_id(contract_id)
        orderbook = self.get_orderbook(self._contract_id(market_id, selection_id))
        bid = orderbook.bids[0].price if orderbook.bids else None
        ask = orderbook.asks[0].price if orderbook.asks else None
        midpoint = (bid + ask) / 2.0 if bid is not None and ask is not None else None
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=self._contract_id(market_id, selection_id),
            last=midpoint,
            bid=bid,
            ask=ask,
            midpoint=midpoint,
            source="betfair_exchange_best_offers",
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
        """Return matched account executions from Betfair's order feed.

        Betfair's documented ``listCurrentOrders`` operation can order by
        match time and, with ``orderProjection=ALL``, returns fully and
        partially matched orders.  The adapter exposes those matched order
        summaries as normalized trades; the raw Betfair order remains
        attached for callers that need per-price match detail.
        """

        self.ensure_capability("trade_history")
        market_id, selection_id = self._split_contract_id(contract_id)
        desired = self._trade_limit(limit)
        before_ts = self._history_timestamp(before, "before") if before is not None else None
        after_ts = self._history_timestamp(after, "after") if after is not None else None
        if before_ts is not None and after_ts is not None and before_ts < after_ts:
            raise MarketConfigurationError("Betfair trade history requires before to be at or after after.")

        params: Dict[str, Any] = {
            "marketIds": [market_id],
            "orderProjection": "ALL",
            "orderBy": "BY_MATCH_TIME",
            "sortDir": "EARLIEST_TO_LATEST",
            "fromRecord": 0,
            "recordCount": desired,
            "includeItemDescription": False,
        }
        if after_ts is not None or before_ts is not None:
            date_range: Dict[str, str] = {}
            if after_ts is not None:
                date_range["from"] = self._timestamp_iso(after_ts)
            if before_ts is not None:
                date_range["to"] = self._timestamp_iso(before_ts)
            params["dateRange"] = date_range

        payload = self._rpc("SportsAPING/v1.0/listCurrentOrders", params)
        rows = payload.get("currentOrders") if isinstance(payload, Mapping) else []
        if not isinstance(rows, list):
            return []

        trades: List[MarketTrade] = []
        for raw in rows:
            if not isinstance(raw, Mapping):
                continue
            row_market_id = str(raw.get("marketId") or "").strip()
            row_selection_id = str(raw.get("selectionId") or "").strip()
            if row_market_id and row_market_id != market_id:
                continue
            if row_selection_id and row_selection_id != selection_id:
                continue
            trade_id = str(raw.get("betId") or "").strip()
            side = self._trade_side(raw.get("side"))
            matched_size = self._positive_number(raw.get("sizeMatched"))
            decimal_price = self._positive_number(raw.get("averagePriceMatched"))
            price = self._probability_from_decimal_odds(decimal_price)
            timestamp = self._timestamp_seconds(
                raw.get("matchedDate") or raw.get("lastMatchedDate") or raw.get("placedDate")
            )
            if not trade_id or side is None or matched_size is None or price is None or timestamp is None:
                continue
            if after_ts is not None and timestamp < after_ts:
                continue
            if before_ts is not None and timestamp > before_ts:
                continue
            trades.append(
                MarketTrade(
                    market_id=self.market_id,
                    contract_id=self._contract_id(market_id, selection_id),
                    trade_id=trade_id,
                    side=side,
                    price=price,
                    size=matched_size,
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
        """Derive bounded OHLCV candles from matched Betfair executions."""

        self.ensure_capability("candle_history")
        market_id, selection_id = self._split_contract_id(contract_id)
        interval = self._candle_interval(resolution)
        start_ts = self._history_timestamp(from_timestamp, "from_timestamp") if from_timestamp is not None else None
        end_ts = self._history_timestamp(to_timestamp, "to_timestamp") if to_timestamp is not None else None
        if start_ts is not None and end_ts is not None and end_ts < start_ts:
            raise MarketConfigurationError("Betfair candle history requires to_timestamp to be at or after from_timestamp.")

        trades = self.list_trades(
            contract_id,
            limit=self._candle_trade_limit(),
            before=end_ts,
            after=start_ts,
        )
        buckets: Dict[int, Dict[str, Any]] = {}
        for trade in trades:
            if trade.timestamp is None or trade.timestamp < 0:
                continue
            bucket_timestamp = int(float(trade.timestamp) // interval * interval)
            bucket = buckets.setdefault(
                bucket_timestamp,
                {"open": trade.price, "high": trade.price, "low": trade.price, "close": trade.price, "volume": 0.0, "trade_ids": []},
            )
            bucket["high"] = max(float(bucket["high"]), trade.price)
            bucket["low"] = min(float(bucket["low"]), trade.price)
            bucket["close"] = trade.price
            bucket["volume"] += max(0.0, float(trade.size))
            bucket["trade_ids"].append(trade.trade_id)

        canonical = self._contract_id(market_id, selection_id)
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
                    "source": "betfair_matched_account_orders",
                    "derived": True,
                    "resolution": str(resolution or "").strip().lower(),
                    "interval_seconds": interval,
                    "trade_ids": list(bucket["trade_ids"]),
                },
            )
            for bucket_timestamp, bucket in sorted(buckets.items())
        ]

    def list_cleared_orders(
        self,
        *,
        bet_status: str = "SETTLED",
        market_id: str = "",
        event_type_id: str = "",
        event_id: str = "",
        runner_id: str = "",
        bet_id: str = "",
        group_by: str = "BET",
        include_item_description: bool = False,
        limit: int = 100,
        offset: int = 0,
        from_timestamp: Optional[float] = None,
        to_timestamp: Optional[float] = None,
    ) -> Any:
        """Read settled/cleared Betfair orders through the official RPC feed.

        ``listClearedOrders`` is the documented settlement surface for settled,
        voided, lapsed, and cancelled bets.  The adapter returns the upstream
        roll-up payload losslessly while validating all caller-controlled
        filters and date bounds before they reach the authenticated endpoint.
        """

        status = str(bet_status or "SETTLED").strip().upper()
        if status not in {"SETTLED", "VOIDED", "LAPSED", "CANCELLED"}:
            raise MarketConfigurationError(
                "Betfair cleared-order status must be SETTLED, VOIDED, LAPSED, or CANCELLED."
            )
        rollup = str(group_by or "BET").strip().upper()
        if rollup not in {"EVENT_TYPE", "EVENT", "MARKET", "RUNNER", "BET"}:
            raise MarketConfigurationError(
                "Betfair cleared-order group_by must be EVENT_TYPE, EVENT, MARKET, RUNNER, or BET."
            )
        count = self._account_limit(limit)
        start = self._account_offset(offset)
        after_ts = self._history_timestamp(from_timestamp, "from") if from_timestamp is not None else None
        before_ts = self._history_timestamp(to_timestamp, "to") if to_timestamp is not None else None
        if after_ts is not None and before_ts is not None and before_ts < after_ts:
            raise MarketConfigurationError("Betfair cleared-order history requires to at or after from.")

        params: Dict[str, Any] = {
            "betStatus": status,
            "groupBy": rollup,
            "includeItemDescription": bool(include_item_description),
            "fromRecord": start,
            "recordCount": count,
        }
        for parameter, value, label in (
            ("eventTypeIds", event_type_id, "event_type_id"),
            ("eventIds", event_id, "event_id"),
            ("marketIds", market_id, "market_id"),
            ("runnerIds", runner_id, "runner_id"),
            ("betIds", bet_id, "bet_id"),
        ):
            normalized = self._account_id(value, label)
            if normalized:
                params[parameter] = [normalized]
        if after_ts is not None or before_ts is not None:
            date_range: Dict[str, str] = {}
            if after_ts is not None:
                date_range["from"] = self._timestamp_iso(after_ts)
            if before_ts is not None:
                date_range["to"] = self._timestamp_iso(before_ts)
            params["settledDateRange"] = date_range
        return self._rpc("SportsAPING/v1.0/listClearedOrders", params)

    def list_current_orders(
        self,
        *,
        market_id: str = "",
        contract_id: str = "",
        status: str = "",
        order_by: str = "BY_MATCH_TIME",
        sort_dir: str = "EARLIEST_TO_LATEST",
        include_item_description: bool = False,
        limit: int = 100,
        offset: int = 0,
        from_timestamp: Optional[float] = None,
        to_timestamp: Optional[float] = None,
    ) -> Any:
        """Read current Betfair orders through the documented betting API.

        The upstream ``listCurrentOrders`` operation returns both executable
        and matched orders.  Market/runner/status filters are validated and
        applied locally so the raw response remains lossless while callers
        cannot inject arbitrary RPC parameters.
        """

        market_filter = self._account_id(market_id, "market_id")
        selection_filter = ""
        if contract_id:
            contract_market, selection_filter = self._split_contract_id(contract_id)
            if market_filter and market_filter != contract_market:
                raise MarketConfigurationError("Betfair current-order market_id and contract_id do not match.")
            market_filter = contract_market
        normalized_status = str(status or "").strip().upper()
        if normalized_status and normalized_status not in {
            "EXECUTABLE",
            "EXECUTION_COMPLETE",
            "EXECUTION_FAILED",
            "EXPIRED",
            "CANCELLED",
            "LAPSED",
        }:
            raise MarketConfigurationError(
                "Betfair current-order status must be EXECUTABLE, EXECUTION_COMPLETE, EXECUTION_FAILED, "
                "EXPIRED, CANCELLED, or LAPSED."
            )
        normalized_order_by = str(order_by or "BY_MATCH_TIME").strip().upper()
        if normalized_order_by not in {"BY_BET", "BY_MARKET", "BY_MATCH_TIME", "BY_PLACE_TIME"}:
            raise MarketConfigurationError(
                "Betfair current-order order_by must be BY_BET, BY_MARKET, BY_MATCH_TIME, or BY_PLACE_TIME."
            )
        normalized_sort_dir = str(sort_dir or "EARLIEST_TO_LATEST").strip().upper()
        if normalized_sort_dir not in {"EARLIEST_TO_LATEST", "LATEST_TO_EARLIEST"}:
            raise MarketConfigurationError(
                "Betfair current-order sort_dir must be EARLIEST_TO_LATEST or LATEST_TO_EARLIEST."
            )
        count = self._account_limit(limit)
        start = self._account_offset(offset)
        after_ts = self._history_timestamp(from_timestamp, "from") if from_timestamp is not None else None
        before_ts = self._history_timestamp(to_timestamp, "to") if to_timestamp is not None else None
        if after_ts is not None and before_ts is not None and before_ts < after_ts:
            raise MarketConfigurationError("Betfair current-order history requires to at or after from.")
        params: Dict[str, Any] = {
            "orderProjection": "ALL",
            "orderBy": normalized_order_by,
            "sortDir": normalized_sort_dir,
            "includeItemDescription": bool(include_item_description),
            "fromRecord": start,
            "recordCount": count,
        }
        if market_filter:
            params["marketIds"] = [market_filter]
        if after_ts is not None or before_ts is not None:
            date_range: Dict[str, str] = {}
            if after_ts is not None:
                date_range["from"] = self._timestamp_iso(after_ts)
            if before_ts is not None:
                date_range["to"] = self._timestamp_iso(before_ts)
            params["dateRange"] = date_range
        payload = self._rpc("SportsAPING/v1.0/listCurrentOrders", params)
        if not isinstance(payload, Mapping):
            return payload
        rows = payload.get("currentOrders")
        if not isinstance(rows, list):
            return payload
        filtered: List[Dict[str, Any]] = []
        for raw in rows:
            if not isinstance(raw, Mapping):
                continue
            if market_filter and str(raw.get("marketId") or "").strip() != market_filter:
                continue
            if selection_filter and str(raw.get("selectionId") or "").strip() != selection_filter:
                continue
            if normalized_status and str(raw.get("status") or "").strip().upper() != normalized_status:
                continue
            filtered.append(dict(raw))
        return {**dict(payload), "currentOrders": filtered}

    def get_account_funds(self, *, wallet: str = "") -> Any:
        """Read available-to-bet funds through Betfair's documented account API."""

        normalized_wallet = self._account_wallet(wallet)
        params = {"wallet": normalized_wallet} if normalized_wallet else {}
        return self._account_rpc("AccountAPING/v1.0/getAccountFunds", params)

    def get_account_details(self) -> Any:
        """Read account profile/discount/points details through Betfair's account API."""

        return self._account_rpc("AccountAPING/v1.0/getAccountDetails", {})

    def get_account_statement(
        self,
        *,
        locale: str = "en",
        limit: int = 100,
        offset: int = 0,
        include_item: bool = True,
        wallet: str = "",
        from_timestamp: Optional[float] = None,
        to_timestamp: Optional[float] = None,
    ) -> Any:
        """Read the documented account money-movement statement.

        The response is deliberately returned losslessly: callers can inspect
        the venue's balance, item, and transfer fields without this adapter
        inventing a normalized accounting model. Date bounds are converted to
        Betfair's ISO ``itemDateRange`` shape and all paging/enum-like values
        are validated locally before the authenticated request.
        """

        normalized_locale = self._account_locale(locale)
        count = self._account_limit(limit)
        start = self._account_offset(offset)
        after_ts = self._history_timestamp(from_timestamp, "from") if from_timestamp is not None else None
        before_ts = self._history_timestamp(to_timestamp, "to") if to_timestamp is not None else None
        if after_ts is not None and before_ts is not None and before_ts < after_ts:
            raise MarketConfigurationError("Betfair account statement requires to at or after from.")
        normalized_wallet = self._account_wallet(wallet)
        params: Dict[str, Any] = {
            "locale": normalized_locale,
            "fromRecord": start,
            "recordCount": count,
            "includeItem": bool(include_item),
        }
        if normalized_wallet:
            params["wallet"] = normalized_wallet
        if after_ts is not None or before_ts is not None:
            date_range: Dict[str, str] = {}
            if after_ts is not None:
                date_range["from"] = self._timestamp_iso(after_ts)
            if before_ts is not None:
                date_range["to"] = self._timestamp_iso(before_ts)
            params["itemDateRange"] = date_range
        return self._account_rpc("AccountAPING/v1.0/getAccountStatement", params)

    def list_currency_rates(self, *, from_currency: str) -> Any:
        """Read Betfair's documented currency conversion rates."""

        normalized_currency = str(from_currency or "").strip().upper()
        if len(normalized_currency) != 3 or not normalized_currency.isalpha():
            raise MarketConfigurationError("Betfair from_currency must be a three-letter ISO currency code.")
        return self._account_rpc(
            "AccountAPING/v1.0/listCurrencyRates",
            {"fromCurrency": normalized_currency},
        )

    def account_recovery(self, operation: str, **kwargs: Any) -> Any:
        normalized = str(operation or "").strip().lower()
        if normalized == "active_orders":
            return self.list_current_orders(
                market_id=kwargs.get("market_id", ""),
                contract_id=kwargs.get("contract_id", ""),
                status=kwargs.get("status", ""),
                order_by=kwargs.get("order_by", "BY_MATCH_TIME"),
                sort_dir=kwargs.get("sort_dir", "EARLIEST_TO_LATEST"),
                include_item_description=bool(kwargs.get("include_item_description", False)),
                limit=kwargs.get("limit", 100),
                offset=kwargs.get("offset", 0),
                from_timestamp=kwargs.get("from_timestamp"),
                to_timestamp=kwargs.get("to_timestamp"),
            )
        if normalized == "cleared_orders":
            return self.list_cleared_orders(
                bet_status=kwargs.get("bet_status", "SETTLED"),
                market_id=kwargs.get("market_id", ""),
                event_type_id=kwargs.get("event_type_id", ""),
                event_id=kwargs.get("event_id", ""),
                runner_id=kwargs.get("runner_id", ""),
                bet_id=kwargs.get("bet_id", ""),
                group_by=kwargs.get("group_by", "BET"),
                include_item_description=bool(kwargs.get("include_item_description", False)),
                limit=kwargs.get("limit", 100),
                offset=kwargs.get("offset", 0),
                from_timestamp=kwargs.get("from_timestamp"),
                to_timestamp=kwargs.get("to_timestamp"),
            )
        if normalized == "funds":
            return self.get_account_funds(wallet=kwargs.get("wallet", ""))
        if normalized == "account":
            return self.get_account_details()
        if normalized == "statement":
            return self.get_account_statement(
                locale=kwargs.get("locale", "en"),
                limit=kwargs.get("limit", 100),
                offset=kwargs.get("offset", 0),
                include_item=bool(kwargs.get("include_item", True)),
                wallet=kwargs.get("wallet", ""),
                from_timestamp=kwargs.get("from_timestamp"),
                to_timestamp=kwargs.get("to_timestamp"),
            )
        if normalized == "currency_rates":
            return self.list_currency_rates(from_currency=kwargs.get("from_currency", ""))
        supported = ", ".join(self.account_recovery_operations)
        raise MarketConfigurationError(f"Betfair account recovery supports only: {supported}.")

    def manage_orders(
        self,
        operation: str,
        *,
        market_id: str = "",
        instructions: Any = None,
        customer_ref: str = "",
        market_version: Any = None,
        async_request: bool = False,
        confirm_global_cancel: str = "",
    ) -> Any:
        """Run one documented Betfair order-management mutation.

        The Exchange API treats these as live mutations, so they are never
        exposed through the read-only account route.  They require both the
        shared live-safety gate and the Betfair-specific
        ``betfair_order_management_enabled`` opt-in.  Requests are normalized
        to the documented camelCase JSON-RPC schema after validating every
        caller-controlled field.
        """

        normalized = str(operation or "").strip().lower()
        if normalized not in self.order_management_operations:
            supported = ", ".join(self.order_management_operations)
            raise MarketConfigurationError(f"Betfair order management supports only: {supported}.")
        self.ensure_capability("live_trading")
        if not self.config_bool("betfair_order_management_enabled", False):
            raise MarketConfigurationError(
                "Betfair order management is disabled by adapter config. "
                "Set betfair_order_management_enabled=true only after reviewing cancellation risk."
            )
        self.ensure_live_trading_enabled("Betfair order management")

        normalized_market_id = self._account_id(market_id, "market_id")
        normalized_instructions = self._order_management_instructions(normalized, instructions)
        if normalized in {"update_orders", "replace_orders"} and not normalized_market_id:
            raise MarketConfigurationError(f"Betfair {normalized} requires a market_id.")
        if normalized in {"update_orders", "replace_orders"} and not normalized_instructions:
            raise MarketConfigurationError(f"Betfair {normalized} requires at least one instruction.")
        if normalized == "cancel_orders" and not normalized_market_id and normalized_instructions:
            raise MarketConfigurationError("Betfair cancel_orders requires market_id when instructions are supplied.")
        if normalized == "cancel_orders" and not normalized_market_id:
            if str(confirm_global_cancel or "").strip() != "CANCEL ALL BETS":
                raise MarketConfigurationError(
                    "Global Betfair cancellation requires confirm_global_cancel='CANCEL ALL BETS'."
                )

        normalized_customer_ref = self._customer_ref(customer_ref)
        params: Dict[str, Any] = {}
        if normalized_market_id:
            params["marketId"] = normalized_market_id
        if normalized_instructions:
            params["instructions"] = normalized_instructions
        if normalized_customer_ref:
            params["customerRef"] = normalized_customer_ref
        if normalized == "replace_orders":
            if market_version not in (None, ""):
                params["marketVersion"] = self._market_version(market_version)
            if bool(async_request):
                params["async"] = True
        elif bool(async_request):
            raise MarketConfigurationError(f"Betfair {normalized} does not support async=true.")

        preflight = {
            "market_id": normalized_market_id or None,
            "operation": normalized,
            "instruction_count": len(normalized_instructions),
            "customer_ref": normalized_customer_ref or None,
            "global_cancel": normalized == "cancel_orders" and not normalized_market_id,
            "live_trading_enabled": True,
            "confirmed": True,
            "kill_switch": False,
            "order_management_enabled": True,
        }
        endpoint = {
            "cancel_orders": "SportsAPING/v1.0/cancelOrders",
            "update_orders": "SportsAPING/v1.0/updateOrders",
            "replace_orders": "SportsAPING/v1.0/replaceOrders",
        }[normalized]
        result = self._rpc(endpoint, params)
        return {
            "market_id": self.market_id,
            "operation": normalized,
            "live": True,
            "preflight": preflight,
            "request": params,
            "response": result,
        }

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        self._validate_order(order)
        market_id, selection_id = self._split_contract_id(order.contract_id)
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=self._contract_id(market_id, selection_id),
            accepted=True,
            message=(
                f"DRY RUN: would place Betfair {order.side.upper()} "
                f"for {order.size:.4f} stake"
                + (f" at implied probability {order.limit_price:.3f}" if order.limit_price is not None else "")
            ),
            average_price=order.limit_price,
            raw={"market_id": market_id, "selection_id": selection_id},
        )

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        self.ensure_capability("live_trading")
        self._validate_order(order)
        preflight = self.preflight_live_order(order)
        if order.limit_price is None:
            raise MarketConfigurationError("Betfair live orders require a limit probability.")
        market_id, selection_id = self._split_contract_id(order.contract_id)
        params = {
            "marketId": market_id,
            "instructions": [self._place_instruction(order, selection_id=selection_id)],
        }
        customer_ref = order.metadata.get("customer_ref") or order.metadata.get("customerRef")
        if customer_ref:
            params["customerRef"] = str(customer_ref)
        result = self._rpc("SportsAPING/v1.0/placeOrders", params)
        return {
            "market_id": self.market_id,
            "contract_id": order.contract_id,
            "live": True,
            "preflight": preflight,
            "request": params,
            "response": result,
        }

    def copy_trade_from_activity(self, activity: Mapping[str, Any]) -> PaperOrderResult:
        """Build a local copy preview from a matched Betfair order.

        Betfair's documented ``listCurrentOrders`` response includes the bet
        id, market/selection identity, BACK/LAY direction, average matched
        decimal odds, and matched size.  The preview converts those fields to
        the shared BUY/SELL probability model and never calls ``placeOrders``.
        """

        self.ensure_capability("copy_trading")
        contract_id = str(activity.get("asset") or activity.get("contract_id") or "").strip()
        market_id = str(activity.get("market_id") or activity.get("marketId") or "").strip()
        selection_id = str(activity.get("selection_id") or activity.get("selectionId") or "").strip()
        if contract_id:
            parsed_market, parsed_selection = self._split_contract_id(contract_id)
            if market_id and parsed_market != market_id:
                raise MarketConfigurationError("Betfair activity market_id does not match contract_id.")
            if selection_id and parsed_selection != selection_id:
                raise MarketConfigurationError("Betfair activity selection_id does not match contract_id.")
            market_id, selection_id = parsed_market, parsed_selection
        elif market_id and selection_id:
            contract_id = self._contract_id(market_id, selection_id)
        else:
            raise MarketConfigurationError("Betfair activity requires contract_id or market_id plus selection_id.")

        side = self._trade_side(
            activity.get("side") or activity.get("bet_side") or activity.get("betSide")
        )
        if side not in {"BUY", "SELL"}:
            raise MarketConfigurationError("Betfair activity side must be BACK/LAY or BUY/SELL.")
        size = self._positive_number(
            activity.get("sizeMatched")
            if activity.get("sizeMatched") not in (None, "")
            else activity.get("matched_size")
            if activity.get("matched_size") not in (None, "")
            else activity.get("size")
        )
        if size is None:
            raise MarketConfigurationError("Betfair activity matched size must be positive and finite.")
        raw_probability = activity.get("probability")
        if raw_probability in (None, ""):
            decimal_odds = activity.get("averagePriceMatched")
            if decimal_odds in (None, ""):
                decimal_odds = activity.get("average_price_matched")
            if decimal_odds in (None, ""):
                decimal_odds = activity.get("odds")
            raw_probability = self._probability_from_decimal_odds(decimal_odds)
        else:
            raw_probability = self._safe_probability(raw_probability)
        if raw_probability is None or raw_probability <= 0.0:
            raise MarketConfigurationError("Betfair activity price/odds must map to a probability in (0, 1].")
        trade_id = str(
            activity.get("betId")
            or activity.get("bet_id")
            or activity.get("trade_id")
            or activity.get("tradeId")
            or activity.get("id")
            or ""
        ).strip()
        if not trade_id:
            raise MarketConfigurationError("Betfair activity requires a documented bet id.")
        preview = self.place_paper_order(
            PaperOrderRequest(
                market_id=self.market_id,
                contract_id=contract_id,
                side=side,
                size=size,
                limit_price=raw_probability,
                metadata={"activity": dict(activity), "source": "betfair_authenticated_current_orders"},
            )
        )
        preview.raw["source"] = "betfair_authenticated_current_orders"
        preview.raw["activity"] = dict(activity)
        return preview

    def _get_market_catalogue(self, market_id: str) -> Mapping[str, Any]:
        clean = str(market_id or "").strip()
        if not clean:
            raise MarketConfigurationError("Betfair market id cannot be empty.")
        result = self._rpc(
            "SportsAPING/v1.0/listMarketCatalogue",
            {
                "filter": {"marketIds": [clean]},
                "marketProjection": ["EVENT", "RUNNER_DESCRIPTION", "MARKET_START_TIME"],
                "maxResults": "1",
            },
        )
        markets = [item for item in result if isinstance(item, Mapping)] if isinstance(result, list) else []
        if not markets:
            raise MarketConfigurationError(f"Betfair market {clean!r} was not found.")
        return markets[0]

    def _rpc(self, method: str, params: Mapping[str, Any]) -> Any:
        return self._request_rpc(self.api_base_url, method, params)

    def _account_rpc(self, method: str, params: Mapping[str, Any]) -> Any:
        return self._request_rpc(self.account_api_base_url, method, params)

    def _request_rpc(self, base_url: str, method: str, params: Mapping[str, Any]) -> Any:
        headers = self._headers(required=True)
        body = {"jsonrpc": "2.0", "method": method, "params": dict(params), "id": 1}
        payload = self.runtime.request_json(
            "POST",
            base_url,
            json_body=body,
            headers=headers,
        )
        if isinstance(payload, Mapping) and payload.get("error"):
            raise MarketHTTPError(f"{self.market_id} RPC error: {payload['error']}")
        return payload.get("result") if isinstance(payload, Mapping) else payload

    def _headers(self, *, required: bool = False) -> Dict[str, str]:
        app_key = self.resolve_credential(
            "betfair_app_key",
            ("BETFAIR_APP_KEY",),
            required=required,
            label="BETFAIR_APP_KEY",
        )
        session = self.resolve_credential(
            "betfair_session_token",
            ("BETFAIR_SESSION_TOKEN",),
            required=required,
            label="BETFAIR_SESSION_TOKEN",
        )
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if app_key:
            headers["X-Application"] = app_key.value
        if session:
            headers["X-Authentication"] = session.value
        return headers

    def _event_from_market(self, market: Mapping[str, Any]) -> MarketEvent:
        market_id = self._market_id(market)
        event = market.get("event") if isinstance(market.get("event"), Mapping) else {}
        name = str(market.get("marketName") or event.get("name") or market_id)
        return MarketEvent(
            market_id=self.market_id,
            event_id=market_id,
            title=name,
            url=f"https://www.betfair.com/exchange/plus/market/{market_id}",
            status=str(market.get("status") or "").strip().lower(),
            raw=dict(market),
        )

    def _contracts_from_market(self, market: Mapping[str, Any]) -> List[MarketContract]:
        market_id = self._market_id(market)
        market_name = str(market.get("marketName") or market_id)
        runners = market.get("runners")
        contracts: List[MarketContract] = []
        if isinstance(runners, list):
            for runner in runners:
                if not isinstance(runner, Mapping):
                    continue
                selection_id = str(runner.get("selectionId") or "").strip()
                if not selection_id:
                    continue
                runner_name = str(runner.get("runnerName") or selection_id)
                contracts.append(
                    MarketContract(
                        market_id=self.market_id,
                        contract_id=self._contract_id(market_id, selection_id),
                        event_id=market_id,
                        title=f"{market_name} - {runner_name}",
                        outcome=runner_name,
                        url=f"https://www.betfair.com/exchange/plus/market/{market_id}",
                        status=str(market.get("status") or "").strip().lower(),
                        raw={"market": dict(market), "runner": dict(runner)},
                    )
                )
        return contracts

    def _validate_order(self, order: PaperOrderRequest) -> None:
        self.ensure_order_market(order)
        self._split_contract_id(order.contract_id)
        if str(order.side or "").upper() not in {"BUY", "SELL", "BACK", "LAY"}:
            raise MarketConfigurationError("Betfair paper order side must be BUY/SELL or BACK/LAY.")
        if not self._is_positive_number(order.size):
            raise MarketConfigurationError("Betfair paper order stake must be positive.")
        if order.limit_price is not None and self._safe_probability(order.limit_price) is None:
            raise MarketConfigurationError("Betfair paper order limit probability must be between 0 and 1.")

    @staticmethod
    def _account_id(value: Any, label: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if len(text) > 128 or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-" for char in text):
            raise MarketConfigurationError(f"Betfair {label} must be a short identifier.")
        return text

    @staticmethod
    def _customer_ref(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if len(text) > 32 or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-" for char in text):
            raise MarketConfigurationError("Betfair customer_ref must be at most 32 safe identifier characters.")
        return text

    @classmethod
    def _order_management_instructions(cls, operation: str, value: Any) -> List[Dict[str, Any]]:
        if value in (None, ""):
            return []
        if not isinstance(value, list):
            raise MarketConfigurationError("Betfair order-management instructions must be a JSON array.")
        if len(value) > 60:
            raise MarketConfigurationError("Betfair order-management requests support at most 60 instructions.")
        normalized: List[Dict[str, Any]] = []
        for index, raw in enumerate(value):
            if not isinstance(raw, Mapping):
                raise MarketConfigurationError(f"Betfair instruction {index + 1} must be an object.")
            bet_id = cls._account_id(raw.get("bet_id") or raw.get("betId"), f"instruction_{index + 1}_bet_id")
            if not bet_id:
                raise MarketConfigurationError(f"Betfair instruction {index + 1} requires bet_id.")
            item: Dict[str, Any] = {"betId": bet_id}
            if operation == "cancel_orders":
                reduction = raw.get("size_reduction", raw.get("sizeReduction"))
                if reduction not in (None, ""):
                    try:
                        parsed = float(reduction)
                    except (TypeError, ValueError) as exc:
                        raise MarketConfigurationError(
                            f"Betfair instruction {index + 1} size_reduction must be numeric."
                        ) from exc
                    if not math.isfinite(parsed) or parsed <= 0:
                        raise MarketConfigurationError(
                            f"Betfair instruction {index + 1} size_reduction must be positive and finite."
                        )
                    item["sizeReduction"] = parsed
            elif operation == "update_orders":
                persistence = str(
                    raw.get("new_persistence_type", raw.get("newPersistenceType", "")) or ""
                ).strip().upper()
                if persistence not in {"LAPSE", "PERSIST", "MARKET_ON_CLOSE"}:
                    raise MarketConfigurationError(
                        f"Betfair instruction {index + 1} new_persistence_type must be LAPSE, PERSIST, or MARKET_ON_CLOSE."
                    )
                item["newPersistenceType"] = persistence
            else:
                price = raw.get("new_price", raw.get("newPrice"))
                try:
                    parsed_price = float(price)
                except (TypeError, ValueError) as exc:
                    raise MarketConfigurationError(
                        f"Betfair instruction {index + 1} new_price must be numeric."
                    ) from exc
                if not math.isfinite(parsed_price) or parsed_price < 1.01 or parsed_price > 1000:
                    raise MarketConfigurationError(
                        f"Betfair instruction {index + 1} new_price must be between 1.01 and 1000."
                    )
                item["newPrice"] = parsed_price
            normalized.append(item)
        return normalized

    @staticmethod
    def _market_version(value: Any) -> Dict[str, int]:
        raw = value.get("version") if isinstance(value, Mapping) else value
        try:
            version = int(raw)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Betfair market_version must contain a positive integer version.") from exc
        if version < 1:
            raise MarketConfigurationError("Betfair market_version must contain a positive integer version.")
        return {"version": version}

    @staticmethod
    def _account_locale(value: Any) -> str:
        locale = str(value or "en").strip().lower()
        if not locale or len(locale) > 16 or any(char not in "abcdefghijklmnopqrstuvwxyz-_" for char in locale):
            raise MarketConfigurationError("Betfair locale must contain only letters, hyphens, or underscores.")
        return locale

    @staticmethod
    def _account_wallet(value: Any) -> str:
        wallet = str(value or "").strip().upper()
        if wallet and (
            len(wallet) > 32
            or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ_" for char in wallet)
        ):
            raise MarketConfigurationError("Betfair wallet must contain only letters and underscores.")
        return wallet

    @staticmethod
    def _account_limit(value: Any) -> int:
        try:
            limit = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Betfair cleared-order limit must be an integer between 1 and 1000.") from exc
        if limit < 1 or limit > 1000:
            raise MarketConfigurationError("Betfair cleared-order limit must be between 1 and 1000.")
        return limit

    @staticmethod
    def _account_offset(value: Any) -> int:
        try:
            offset = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Betfair cleared-order offset must be an integer between 0 and 100000.") from exc
        if offset < 0 or offset > 100000:
            raise MarketConfigurationError("Betfair cleared-order offset must be between 0 and 100000.")
        return offset

    @staticmethod
    def _trade_limit(value: Any) -> int:
        if isinstance(value, bool):
            raise MarketConfigurationError("Betfair trade limit must be an integer between 1 and 1000.")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Betfair trade limit must be an integer between 1 and 1000.") from exc
        if parsed < 1 or parsed > 1000:
            raise MarketConfigurationError("Betfair trade limit must be an integer between 1 and 1000.")
        return parsed

    @staticmethod
    def _history_timestamp(value: Any, label: str) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"Betfair {label} must be a numeric epoch timestamp.") from exc
        if not math.isfinite(parsed) or parsed < 0:
            raise MarketConfigurationError(f"Betfair {label} must be a non-negative finite epoch timestamp.")
        return parsed / 1000.0 if parsed > 10_000_000_000 else parsed

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
            raise MarketConfigurationError(
                f"Betfair candle resolution must be one of: {', '.join(intervals)}."
            ) from exc

    def _candle_trade_limit(self) -> int:
        raw_limit = self.config.get("betfair_candle_trade_limit", 1000)
        return self._trade_limit(raw_limit)

    @staticmethod
    def _timestamp_iso(value: float) -> str:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    @classmethod
    def _timestamp_seconds(cls, value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None:
            if not math.isfinite(parsed):
                return None
            return parsed / 1000.0 if parsed > 10_000_000_000 else parsed
        try:
            text = str(value).strip().replace("Z", "+00:00")
            parsed_dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed_dt.tzinfo is None:
            parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
        return parsed_dt.timestamp()

    @staticmethod
    def _trade_side(value: Any) -> Optional[str]:
        return {"BACK": "BUY", "BUY": "BUY", "LAY": "SELL", "SELL": "SELL"}.get(
            str(value or "").strip().upper()
        )

    def _place_instruction(self, order: PaperOrderRequest, *, selection_id: str) -> Dict[str, Any]:
        probability = self._safe_probability(order.limit_price)
        if probability is None or probability <= 0:
            raise MarketConfigurationError("Betfair live order limit probability must be greater than 0 and at most 1.")
        side = str(order.side or "").upper()
        betfair_side = "LAY" if side in {"SELL", "LAY"} else "BACK"
        instruction: Dict[str, Any] = {
            "selectionId": int(selection_id) if selection_id.isdigit() else selection_id,
            "side": betfair_side,
            "orderType": "LIMIT",
            "limitOrder": {
                "size": str(order.size),
                "price": str(round(1.0 / probability, 4)),
                "persistenceType": str(
                    order.metadata.get("persistence_type")
                    or order.metadata.get("persistenceType")
                    or self.config.get("betfair_persistence_type")
                    or "LAPSE"
                ),
            },
        }
        handicap = order.metadata.get("handicap")
        if handicap not in (None, ""):
            instruction["handicap"] = str(handicap)
        return instruction

    @staticmethod
    def _market_id(market: Mapping[str, Any]) -> str:
        return str(market.get("marketId") or "").strip()

    @staticmethod
    def _contract_id(market_id: str, selection_id: str) -> str:
        return f"{market_id}:{selection_id}"

    @staticmethod
    def _split_contract_id(contract_id: str) -> Tuple[str, str]:
        parts = [part.strip() for part in str(contract_id or "").split(":")]
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise MarketConfigurationError("Betfair contract id must be MARKET_ID:SELECTION_ID.")
        return parts[0], parts[1]

    @staticmethod
    def _find_runner(market_book: Mapping[str, Any], selection_id: str) -> Optional[Mapping[str, Any]]:
        runners = market_book.get("runners")
        if not isinstance(runners, list):
            return None
        for runner in runners:
            if isinstance(runner, Mapping) and str(runner.get("selectionId") or "").strip() == str(selection_id):
                return runner
        return None

    @staticmethod
    def _levels_from_decimal_odds(raw: Any, *, descending: bool = False) -> List[OrderBookLevel]:
        levels: List[OrderBookLevel] = []
        if not isinstance(raw, list):
            return levels
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            probability = BetfairExchangeAdapter._probability_from_decimal_odds(item.get("price"))
            try:
                size = float(item.get("size"))
            except (TypeError, ValueError):
                continue
            if probability is not None and BetfairExchangeAdapter._is_positive_number(size):
                levels.append(OrderBookLevel(price=probability, size=size))
        levels.sort(key=lambda level: level.price, reverse=descending)
        return levels

    @staticmethod
    def _probability_from_decimal_odds(value: Any) -> Optional[float]:
        try:
            odds = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(odds) or odds <= 1.0:
            return None
        return 1.0 / odds

    @staticmethod
    def _positive_number(value: Any) -> Optional[float]:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) and parsed > 0 else None

    @staticmethod
    def _safe_probability(value: Any) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        return number if 0.0 <= number <= 1.0 else None

    @staticmethod
    def _is_positive_number(value: Any) -> bool:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False
        return math.isfinite(number) and number > 0
