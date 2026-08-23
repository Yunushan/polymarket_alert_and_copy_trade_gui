"""Official Matchbook Betting Exchange adapter.

Matchbook exposes event/market JSON endpoints and a session-token based offer
API.  The adapter keeps the exchange's decimal odds at the transport boundary
and exposes the application's canonical probability/order-book model.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .base import MarketAdapter
from .catalog import get_market_metadata
from .errors import MarketConfigurationError, MarketHTTPError, UnsupportedFeatureError
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


DEFAULT_MATCHBOOK_API_BASE_URL = "https://api.matchbook.com/edge/rest"
DEFAULT_MATCHBOOK_LOGIN_BASE_URL = "https://api.matchbook.com/bpapi/rest"
MATCHBOOK_ORDER_MANAGEMENT_OPERATIONS = (
    "cancel_offer",
    "cancel_offers",
    "cancel_all_offers",
    "edit_offer",
    "edit_offers",
)
MATCHBOOK_ORDER_MANAGEMENT_CONFIRMATION = "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
MATCHBOOK_GLOBAL_CANCEL_CONFIRMATION = "CANCEL ALL MATCHBOOK OFFERS"
MATCHBOOK_ORDER_MANAGEMENT_MAX_BATCH = 100
MATCHBOOK_ORDER_MANAGEMENT_REFERENCES = (
    "https://developers.matchbook.com/reference/cancel-offer-v2",
    "https://developers.matchbook.com/reference/cancel-offers-v2",
    "https://developers.matchbook.com/reference/edit-offer-v2",
    "https://developers.matchbook.com/reference/edit-offers-v2",
)
MATCHBOOK_REFERENCES = (
    "https://developers.matchbook.com/",
    "https://developers.matchbook.com/reference/get-events",
    "https://developers.matchbook.com/reference/get-event",
    "https://developers.matchbook.com/reference/get-markets",
    "https://developers.matchbook.com/reference/get-market",
    "https://developers.matchbook.com/reference/login",
    "https://developers.matchbook.com/reference/submit-offers-v2",
    "https://developers.matchbook.com/reference/get-aggregated-matched-bets",
    "https://developers.matchbook.com/reference/get-settled-bets-v2",
    "https://developers.matchbook.com/reference/get-current-bets-v2",
    "https://developers.matchbook.com/reference/get-offers-v2",
    "https://developers.matchbook.com/reference/get-current-offers-v2",
    *MATCHBOOK_ORDER_MANAGEMENT_REFERENCES,
    "https://developers.matchbook.com/reference/get-new-wallet-balance",
    "https://developers.matchbook.com/reference/get-account",
)


class MatchbookAdapter(MarketAdapter):
    """Matchbook REST adapter with guarded session-authenticated offers."""

    live_order_sides = ("BUY", "SELL", "BACK", "LAY")
    order_management_operations = MATCHBOOK_ORDER_MANAGEMENT_OPERATIONS
    metadata = get_market_metadata("matchbook")
    account_recovery_operations = (
        "settled_bets",
        "current_bets",
        "current_offers",
        "balance",
        "account",
    )

    def __init__(self, config: Optional[Mapping[str, Any]] = None, **kwargs: Any) -> None:
        super().__init__(config, **kwargs)
        self._session_token: Optional[str] = None

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        credential_sources = []
        for credential in (
            self.resolve_credential(
                "matchbook_session_token",
                ("MATCHBOOK_SESSION_TOKEN",),
                label="MATCHBOOK_SESSION_TOKEN",
            ),
            self.resolve_credential("matchbook_username", ("MATCHBOOK_USERNAME",), label="MATCHBOOK_USERNAME"),
            self.resolve_credential("matchbook_password", ("MATCHBOOK_PASSWORD",), label="MATCHBOOK_PASSWORD"),
        ):
            if credential:
                credential_sources.append({"name": credential.name, "source": credential.source})
        health.update(
            {
                "api_base_url": self.api_base_url,
                "login_base_url": self.login_base_url,
                "references": list(MATCHBOOK_REFERENCES),
                "credential_sources": credential_sources,
                "live_trading_supported": True,
                "live_trading_enabled": self.config_bool("live_trading_enabled", False),
                "session_token_required_for_live": True,
                "account_recovery_operations": list(self.account_recovery_operations),
                "order_management_operations": list(self.order_management_operations),
                "order_management_enabled": self.config_bool("matchbook_order_management_enabled", False),
                "authenticated_account_endpoints": [
                    "/reports/v2/bets/settled",
                    "/reports/v2/bets/current",
                    "/v2/offers",
                    "/account/balance",
                    "/account",
                ],
                "authenticated_order_management_endpoints": [
                    "/v2/offers/{offer_id}",
                    "/v2/offers",
                ],
            }
        )
        return health

    @property
    def api_base_url(self) -> str:
        configured = self.config.get("matchbook_api_base_url") or self.config.get("api_base_url")
        return str(configured or DEFAULT_MATCHBOOK_API_BASE_URL).rstrip("/")

    @property
    def login_base_url(self) -> str:
        configured = self.config.get("matchbook_login_base_url")
        return str(configured or DEFAULT_MATCHBOOK_LOGIN_BASE_URL).rstrip("/")

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = max(1, min(int(limit or 50), 100))
        states = str(self.config.get("matchbook_event_states") or "open,suspended")
        params: Dict[str, Any] = {
            "per-page": desired,
            "states": states,
            "include-prices": True,
            "price-depth": max(1, min(int(self.config.get("matchbook_price_depth") or 3), 10)),
            "odds-type": "DECIMAL",
            "exchange-type": "back-lay",
        }
        payload = self._public_get("/events", params=params)
        events = self._list_from_payload(payload, "events", "data")
        needle = str(query or "").strip().lower()
        if needle:
            events = [event for event in events if needle in self._search_text(event)]
        return [self._event_from_payload(event) for event in events[:desired]]

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        clean_event_id = self._required_id(event_id, "event")
        params = {
            "per-page": 100,
            "states": "open,suspended",
            "include-prices": True,
            "price-depth": max(1, min(int(self.config.get("matchbook_price_depth") or 3), 10)),
            "odds-type": "DECIMAL",
            "exchange-type": "back-lay",
        }
        payload = self._public_get(f"/events/{clean_event_id}/markets", params=params)
        markets = self._list_from_payload(payload, "markets", "data")
        contracts: List[MarketContract] = []
        for market in markets:
            contracts.extend(self._contracts_from_market(market, event_id=clean_event_id))
        return contracts

    def get_orderbook(self, contract_id: str) -> OrderBookSnapshot:
        self.ensure_capability("orderbook_reading")
        event_id, market_id, runner_id = self._split_contract_id(contract_id)
        market = self._get_market(event_id, market_id)
        runner = self._find_runner(market, runner_id)
        if runner is None:
            raise MarketConfigurationError(
                f"Matchbook runner {runner_id!r} was not found in market {market_id!r}."
            )
        bids, asks = self._runner_levels(runner)
        canonical_id = self._contract_id(event_id, market_id, runner_id)
        return OrderBookSnapshot(
            market_id=self.market_id,
            contract_id=canonical_id,
            bids=bids,
            asks=asks,
            raw={"market": dict(market), "runner": dict(runner)},
        )

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        orderbook = self.get_orderbook(contract_id)
        bid = orderbook.bids[0].price if orderbook.bids else None
        ask = orderbook.asks[0].price if orderbook.asks else None
        midpoint = (bid + ask) / 2.0 if bid is not None and ask is not None else None
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=orderbook.contract_id,
            last=midpoint or bid or ask,
            bid=bid,
            ask=ask,
            midpoint=midpoint,
            source="matchbook_decimal_odds",
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
        """Return account matched bets from Matchbook's documented feed.

        ``matched-bets/aggregated`` is the official authenticated read route
        for matched bets.  Matchbook aggregates fills at the requested odds
        mode, so each returned row is exposed as one normalized trade and the
        original response remains available in ``raw``.
        """

        self.ensure_capability("trade_history")
        event_id, market_id, runner_id = self._split_contract_id(contract_id)
        desired = self._trade_limit(limit)
        before_ts = self._history_timestamp(before, "before") if before is not None else None
        after_ts = self._history_timestamp(after, "after") if after is not None else None
        if before_ts is not None and after_ts is not None and before_ts < after_ts:
            raise MarketConfigurationError("Matchbook trade history requires before to be at or after after.")

        params: Dict[str, Any] = {
            "event-ids": event_id,
            "market-ids": market_id,
            "runner-ids": runner_id,
            "offset": 0,
            "per-page": desired,
            "aggregation-type": "average",
        }
        payload = self._request_json("GET", "/v2/matched-bets/aggregated", params=params, auth=True)
        rows = self._list_from_payload(payload, "matched-bets", "matchedBets", "bets", "data")
        trades: List[MarketTrade] = []
        for raw in rows:
            row_event = str(self._value(raw, "event-id", "eventId", "event_id") or "").strip()
            row_market = str(self._value(raw, "market-id", "marketId", "market_id") or "").strip()
            row_runner = str(self._value(raw, "runner-id", "runnerId", "runner_id") or "").strip()
            if row_event and row_event != event_id:
                continue
            if row_market and row_market != market_id:
                continue
            if row_runner and row_runner != runner_id:
                continue
            trade_id = str(
                self._value(raw, "id", "bet-id", "betId", "matched-bet-id", "matchedBetId") or ""
            ).strip()
            side = self._trade_side(self._value(raw, "side", "bet-side", "betSide"))
            odds = self._positive_float(self._value(raw, "odds", "price", "decimal-odds", "decimalOdds"))
            size = self._positive_float(
                self._value(raw, "matched-stake", "matchedStake", "matched-amount", "matchedAmount", "stake", "size", "amount")
            )
            timestamp = self._timestamp_seconds(
                self._value(raw, "matched-at", "matchedAt", "matched-date", "matchedDate", "timestamp", "created-at", "createdAt")
            )
            price = 1.0 / odds if odds is not None and odds > 1.0 else None
            if not trade_id or side is None or odds is None or size is None or price is None:
                continue
            if (after_ts is not None or before_ts is not None) and timestamp is None:
                continue
            if after_ts is not None and timestamp < after_ts:
                continue
            if before_ts is not None and timestamp > before_ts:
                continue
            trades.append(
                MarketTrade(
                    market_id=self.market_id,
                    contract_id=self._contract_id(event_id, market_id, runner_id),
                    trade_id=trade_id,
                    side=side,
                    price=price,
                    size=size,
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
        """Derive bounded OHLCV candles from authenticated matched bets."""

        self.ensure_capability("candle_history")
        event_id, market_id, runner_id = self._split_contract_id(contract_id)
        interval = self._candle_interval(resolution)
        start_ts = self._history_timestamp(from_timestamp, "from_timestamp") if from_timestamp is not None else None
        end_ts = self._history_timestamp(to_timestamp, "to_timestamp") if to_timestamp is not None else None
        if start_ts is not None and end_ts is not None and end_ts < start_ts:
            raise MarketConfigurationError("Matchbook candle history requires to_timestamp to be at or after from_timestamp.")

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

        canonical = self._contract_id(event_id, market_id, runner_id)
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
                    "source": "matchbook_authenticated_matched_bets",
                    "derived": True,
                    "resolution": str(resolution or "").strip().lower(),
                    "interval_seconds": interval,
                    "trade_ids": list(bucket["trade_ids"]),
                },
            )
            for bucket_timestamp, bucket in sorted(buckets.items())
        ]

    def list_settled_bets(
        self,
        *,
        offset: int = 0,
        limit: int = 50,
        sport_id: str = "",
        event_id: str = "",
        market_id: str = "",
        from_timestamp: Any = None,
        to_timestamp: Any = None,
        odds_type: str = "DECIMAL",
    ) -> Any:
        """Read the authenticated Matchbook settled-bets report."""

        params = self._account_bet_params(
            offset=offset,
            limit=limit,
            sport_id=sport_id,
            event_id=event_id,
            market_id=market_id,
            from_timestamp=from_timestamp,
            to_timestamp=to_timestamp,
            odds_type=odds_type,
        )
        return self._request_json("GET", "/reports/v2/bets/settled", params=params, auth=True)

    def list_current_bets(
        self,
        *,
        offset: int = 0,
        limit: int = 50,
        sport_id: str = "",
        event_id: str = "",
        market_id: str = "",
        from_timestamp: Any = None,
        to_timestamp: Any = None,
        odds_type: str = "DECIMAL",
    ) -> Any:
        """Read the authenticated Matchbook current-bets report."""

        params = self._account_bet_params(
            offset=offset,
            limit=limit,
            sport_id=sport_id,
            event_id=event_id,
            market_id=market_id,
            from_timestamp=from_timestamp,
            to_timestamp=to_timestamp,
            odds_type=odds_type,
        )
        return self._request_json("GET", "/reports/v2/bets/current", params=params, auth=True)

    def list_current_offers(
        self,
        *,
        offset: int = 0,
        limit: int = 20,
        sport_id: str = "",
        event_id: str = "",
        market_id: str = "",
        runner_id: str = "",
        side: str = "",
        status: str = "",
        interval: Any = None,
        include_edits: bool = False,
        cancellation_reason: str = "",
        aggregation_type: str = "none",
        odds_type: str = "DECIMAL",
    ) -> Any:
        """Read the authenticated current-offers surface."""

        params: Dict[str, Any] = {
            "offset": self._account_offset(offset),
            "per-page": self._account_limit(limit),
            "aggregation-type": self._account_aggregation(aggregation_type),
            "odds-type": self._account_odds_type(odds_type),
        }
        for parameter, value, label in (
            ("sport-ids", sport_id, "sport_id"),
            ("event-ids", event_id, "event_id"),
            ("market-ids", market_id, "market_id"),
            ("runner-ids", runner_id, "runner_id"),
        ):
            normalized = self._account_ids(value, label)
            if normalized:
                params[parameter] = normalized
        for parameter, value, allowed, label in (
            ("side", side, ("back", "lay", "win", "lose"), "side"),
            ("status", status, ("open", "cancelled", "edited", "flushed", "matched", "unmatched"), "status"),
        ):
            normalized = self._account_values(value, allowed, label)
            if normalized:
                params[parameter] = normalized
        if interval not in (None, ""):
            params["interval"] = self._account_interval(interval)
        if include_edits:
            params["include-edits"] = True
        normalized_reason = self._account_values(
            cancellation_reason,
            ("user_request", "heartbeat_expiry"),
            "cancellation_reason",
        )
        if normalized_reason:
            params["cancellation-reason"] = normalized_reason
        return self._request_json("GET", "/v2/offers", params=params, auth=True)

    def get_balance(self) -> Any:
        """Read the authenticated Matchbook wallet balance."""

        return self._request_json("GET", "/account/balance", auth=True)

    def get_account(self) -> Any:
        """Read the authenticated Matchbook account profile."""

        return self._request_json("GET", "/account", auth=True)

    def account_recovery(self, operation: str, **kwargs: Any) -> Any:
        normalized = str(operation or "").strip().lower()
        if normalized == "settled_bets":
            return self.list_settled_bets(
                offset=kwargs.get("offset", 0),
                limit=kwargs.get("limit", 50),
                sport_id=kwargs.get("sport_id", ""),
                event_id=kwargs.get("event_id", ""),
                market_id=kwargs.get("market_id", ""),
                from_timestamp=kwargs.get("from_timestamp"),
                to_timestamp=kwargs.get("to_timestamp"),
                odds_type=kwargs.get("odds_type", "DECIMAL"),
            )
        if normalized == "current_bets":
            return self.list_current_bets(
                offset=kwargs.get("offset", 0),
                limit=kwargs.get("limit", 50),
                sport_id=kwargs.get("sport_id", ""),
                event_id=kwargs.get("event_id", ""),
                market_id=kwargs.get("market_id", ""),
                from_timestamp=kwargs.get("from_timestamp"),
                to_timestamp=kwargs.get("to_timestamp"),
                odds_type=kwargs.get("odds_type", "DECIMAL"),
            )
        if normalized == "current_offers":
            return self.list_current_offers(
                offset=kwargs.get("offset", 0),
                limit=kwargs.get("limit", 20),
                sport_id=kwargs.get("sport_id", ""),
                event_id=kwargs.get("event_id", ""),
                market_id=kwargs.get("market_id", ""),
                runner_id=kwargs.get("runner_id", ""),
                side=kwargs.get("side", ""),
                status=kwargs.get("status", ""),
                interval=kwargs.get("interval"),
                include_edits=bool(kwargs.get("include_edits", False)),
                cancellation_reason=kwargs.get("cancellation_reason", ""),
                aggregation_type=kwargs.get("aggregation_type", "none"),
                odds_type=kwargs.get("odds_type", "DECIMAL"),
            )
        if normalized == "balance":
            return self.get_balance()
        if normalized == "account":
            return self.get_account()
        supported = ", ".join(self.account_recovery_operations)
        raise MarketConfigurationError(f"Matchbook account recovery supports only: {supported}.")

    def manage_orders(self, operation: str, **kwargs: Any) -> Dict[str, Any]:
        """Run one guarded Matchbook offer cancellation or edit mutation.

        Matchbook exposes fixed ``DELETE`` and ``PUT`` offer endpoints.  The
        adapter accepts only the documented operation allow-list and validates
        every identifier, filter, odds, and stake locally before sending a
        session-authenticated request.  No caller-provided URL or HTTP method
        is accepted.
        """

        normalized = str(operation or "").strip().lower()
        if normalized not in self.order_management_operations:
            supported = ", ".join(self.order_management_operations)
            raise MarketConfigurationError(
                f"Matchbook order-management operation must be one of: {supported}."
            )
        self.ensure_capability("live_trading")
        if not self.config_bool("matchbook_order_management_enabled", False):
            raise MarketConfigurationError(
                "Matchbook order management is disabled by adapter config. "
                "Set matchbook_order_management_enabled=true only after reviewing offer mutation risk."
            )
        self.ensure_live_trading_enabled("Matchbook order management")
        if str(kwargs.get("confirm_order_management") or "").strip() != MATCHBOOK_ORDER_MANAGEMENT_CONFIRMATION:
            raise MarketConfigurationError(
                "Matchbook order management requires exact confirmation text "
                f"{MATCHBOOK_ORDER_MANAGEMENT_CONFIRMATION}."
            )
        if bool(kwargs.get("async_request")):
            raise MarketConfigurationError("Matchbook order-management requests are synchronous.")

        request_params: Dict[str, Any] = {}
        request_body: Optional[Dict[str, Any]] = None
        if normalized == "cancel_offer":
            offer_id = self._order_management_id(kwargs.get("offer_id", kwargs.get("order_id")))
            path = f"/v2/offers/{offer_id}"
            response = self._request_json("DELETE", path, auth=True)
            request_params = {"offer_id": offer_id}
        elif normalized == "cancel_offers":
            request_params = self._cancel_offer_filters(kwargs)
            if not request_params:
                raise MarketConfigurationError(
                    "Matchbook cancel_offers requires offer_ids or at least one event_ids, market_ids, or runner_ids filter."
                )
            response = self._request_json("DELETE", "/v2/offers", params=request_params, auth=True)
        elif normalized == "cancel_all_offers":
            if str(kwargs.get("confirm_global_cancel") or "").strip() != MATCHBOOK_GLOBAL_CANCEL_CONFIRMATION:
                raise MarketConfigurationError(
                    "Matchbook global cancellation requires exact confirmation text "
                    f"{MATCHBOOK_GLOBAL_CANCEL_CONFIRMATION}."
                )
            response = self._request_json("DELETE", "/v2/offers", auth=True)
        elif normalized == "edit_offer":
            offer_id = self._order_management_id(kwargs.get("offer_id", kwargs.get("order_id")))
            request_body = self._offer_edit_payload(
                {key: value for key, value in kwargs.items() if key not in {"id", "offer_id", "offer-id", "order_id"}},
                require_id=False,
            )
            response = self._request_json("PUT", f"/v2/offers/{offer_id}", body=request_body, auth=True)
            request_params = {"offer_id": offer_id}
        else:
            offers_value = kwargs.get("offers", kwargs.get("instructions"))
            offers = self._offer_edit_list(offers_value)
            request_body = {"offers": offers}
            response = self._request_json("PUT", "/v2/offers", body=request_body, auth=True)

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
                "references": list(MATCHBOOK_ORDER_MANAGEMENT_REFERENCES),
            },
            "request": {
                "params": request_params,
                "body": request_body,
            },
            "response": response,
        }

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        self._validate_order(order)
        event_id, market_id, runner_id = self._split_contract_id(order.contract_id)
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=self._contract_id(event_id, market_id, runner_id),
            accepted=True,
            message=(
                f"DRY RUN: would place Matchbook {str(order.side).upper()} "
                f"for {float(order.size):.4f} stake"
                + (f" at probability {float(order.limit_price):.4f}" if order.limit_price is not None else "")
            ),
            raw={"event_id": event_id, "market_id": market_id, "runner_id": runner_id},
        )

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        self.ensure_capability("live_trading")
        self._validate_order(order)
        preflight = self.preflight_live_order(order)
        if order.limit_price is None:
            raise MarketConfigurationError("Matchbook live orders require a limit probability.")
        event_id, market_id, runner_id = self._split_contract_id(order.contract_id)
        probability = self._safe_probability(order.limit_price)
        if probability is None or probability <= 0:
            raise MarketConfigurationError("Matchbook live order probability must be greater than 0 and at most 1.")
        side = "lay" if str(order.side or "").upper() in {"SELL", "LAY"} else "back"
        offer: Dict[str, Any] = {
            "runner-id": int(runner_id) if runner_id.isdigit() else runner_id,
            "side": side,
            "odds": round(1.0 / probability, 8),
            "stake": str(order.size),
            "keep-in-play": bool(order.metadata.get("keep_in_play", self.config_bool("matchbook_keep_in_play", False))),
        }
        body: Dict[str, Any] = {
            "odds-type": "DECIMAL",
            "exchange-type": "back-lay",
            "offers": [offer],
        }
        response = self._request_json("POST", "/v2/offers", body=body, auth=True)
        return {
            "market_id": self.market_id,
            "contract_id": self._contract_id(event_id, market_id, runner_id),
            "live": True,
            "preflight": preflight,
            "request": body,
            "response": response,
        }

    def copy_trade_from_activity(self, activity: Mapping[str, Any]) -> PaperOrderResult:
        raise UnsupportedFeatureError(
            self.market_id,
            "copy_trading",
            "Matchbook copy trading is unsupported because the adapter does not mirror account activity.",
        )

    def _get_market(self, event_id: str, market_id: str) -> Mapping[str, Any]:
        params = {
            "include-prices": True,
            "price-depth": max(1, min(int(self.config.get("matchbook_price_depth") or 3), 10)),
            "odds-type": "DECIMAL",
            "exchange-type": "back-lay",
        }
        payload = self._public_get(f"/events/{event_id}/markets/{market_id}", params=params)
        market = self._mapping_payload(payload)
        if not market:
            raise MarketConfigurationError(f"Matchbook market {market_id!r} was not found.")
        return market

    def _public_get(self, path: str, *, params: Optional[Mapping[str, Any]] = None) -> Any:
        return self.runtime.get_json(
            self._url(self.api_base_url, path),
            params=params,
            headers={"Accept": "application/json"},
        )

    @staticmethod
    def _url(base: str, path: str) -> str:
        return f"{base.rstrip('/')}/{str(path or '').strip('/')}"

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        body: Optional[Mapping[str, Any]] = None,
        auth: bool = False,
        login: bool = False,
    ) -> Any:
        self.runtime.rate_limiter.wait()
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if auth:
            token = self._resolve_session_token(required=True)
            headers["session-token"] = token
        base = self.login_base_url if login else self.api_base_url
        try:
            response = self.runtime.session.request(
                method.upper(),
                self._url(base, path),
                params=dict(params) if params is not None else None,
                json=dict(body) if body is not None else None,
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

    def _resolve_session_token(self, *, required: bool) -> str:
        if self._session_token:
            return self._session_token
        token = self.resolve_credential(
            "matchbook_session_token",
            ("MATCHBOOK_SESSION_TOKEN",),
            required=False,
            label="MATCHBOOK_SESSION_TOKEN",
        )
        if token:
            self._session_token = token.value
            return self._session_token
        username = self.resolve_credential(
            "matchbook_username",
            ("MATCHBOOK_USERNAME",),
            required=required,
            label="MATCHBOOK_USERNAME",
        )
        password = self.resolve_credential(
            "matchbook_password",
            ("MATCHBOOK_PASSWORD",),
            required=required,
            label="MATCHBOOK_PASSWORD",
        )
        if username is None or password is None:
            raise MarketConfigurationError("Matchbook live orders require MATCHBOOK_SESSION_TOKEN or username/password.")
        mfa = self.resolve_credential("matchbook_mfa_code", ("MATCHBOOK_MFA_CODE",), label="MATCHBOOK_MFA_CODE")
        login_body: Dict[str, Any] = {"username": username.value, "password": password.value}
        if mfa:
            login_body["mfa-code"] = mfa.value
        payload = self._request_json("POST", "/security/session", body=login_body, login=True)
        mapping = self._mapping_payload(payload)
        session = self._value(mapping, "session-token", "sessionToken", "session_token", "token")
        if not session:
            raise MarketConfigurationError("Matchbook login response did not include a session token.")
        self._session_token = str(session)
        return self._session_token

    @classmethod
    def _order_management_id(cls, value: Any) -> int:
        if isinstance(value, bool):
            raise MarketConfigurationError("Matchbook offer id must be a positive integer.")
        text = str(value or "").strip()
        if not text.isdigit():
            raise MarketConfigurationError("Matchbook offer id must be a positive integer.")
        parsed = int(text)
        if parsed < 1 or parsed > 9_223_372_036_854_775_807:
            raise MarketConfigurationError("Matchbook offer id must be a positive signed int64.")
        return parsed

    @classmethod
    def _order_management_ids(cls, value: Any, *, label: str = "offer_ids") -> List[int]:
        if isinstance(value, str):
            values: Any = [part.strip() for part in value.split(",") if part.strip()]
        elif isinstance(value, (list, tuple)):
            values = list(value)
        else:
            raise MarketConfigurationError(f"Matchbook {label} must be a list or comma-separated string of ids.")
        if not values or len(values) > MATCHBOOK_ORDER_MANAGEMENT_MAX_BATCH:
            raise MarketConfigurationError(
                f"Matchbook {label} must contain between 1 and {MATCHBOOK_ORDER_MANAGEMENT_MAX_BATCH} ids."
            )
        parsed = [cls._order_management_id(item) for item in values]
        if len(set(parsed)) != len(parsed):
            raise MarketConfigurationError(f"Matchbook {label} must not contain duplicate ids.")
        return parsed

    @classmethod
    def _order_management_float(cls, value: Any, label: str, *, minimum: float = 0.0) -> float:
        if isinstance(value, bool):
            raise MarketConfigurationError(f"Matchbook {label} must be a finite number.")
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"Matchbook {label} must be a finite number.") from exc
        if not math.isfinite(parsed) or parsed <= minimum:
            comparator = "greater than" if minimum else "greater than or equal to"
            raise MarketConfigurationError(f"Matchbook {label} must be {comparator} {minimum:g}.")
        if parsed > 1_000_000_000_000:
            raise MarketConfigurationError(f"Matchbook {label} is outside the supported range.")
        return parsed

    @classmethod
    def _offer_edit_payload(cls, value: Mapping[str, Any], *, require_id: bool) -> Dict[str, Any]:
        offer_id_value = value.get("id", value.get("offer_id", value.get("offer-id")))
        payload: Dict[str, Any] = {}
        if require_id or offer_id_value not in (None, ""):
            payload["id"] = cls._order_management_id(offer_id_value)
        aliases = (
            ("current-odds", "current_odds"),
            ("new-odds", "new_odds"),
            ("current-stake", "current_stake"),
            ("new-stake", "new_stake"),
        )
        for target, alias in aliases:
            raw = value.get(target, value.get(alias))
            if raw in (None, ""):
                raise MarketConfigurationError(f"Matchbook offer edit requires {target}.")
            minimum = 1.0 if target.endswith("odds") else 0.0
            payload[target] = cls._order_management_float(raw, target, minimum=minimum)
        return payload

    @classmethod
    def _offer_edit_list(cls, value: Any) -> List[Dict[str, Any]]:
        if not isinstance(value, (list, tuple)):
            raise MarketConfigurationError("Matchbook edit_offers requires an array of offer edits.")
        if not value or len(value) > MATCHBOOK_ORDER_MANAGEMENT_MAX_BATCH:
            raise MarketConfigurationError(
                f"Matchbook edit_offers requires between 1 and {MATCHBOOK_ORDER_MANAGEMENT_MAX_BATCH} edits."
            )
        edits: List[Dict[str, Any]] = []
        seen: set[int] = set()
        for item in value:
            if not isinstance(item, Mapping):
                raise MarketConfigurationError("Matchbook each offer edit must be a JSON object.")
            payload = cls._offer_edit_payload(item, require_id=True)
            offer_id = int(payload["id"])
            if offer_id in seen:
                raise MarketConfigurationError("Matchbook edit_offers must not contain duplicate offer ids.")
            seen.add(offer_id)
            edits.append(payload)
        return edits

    @classmethod
    def _cancel_offer_filter(cls, value: Any, label: str) -> str:
        if isinstance(value, (list, tuple)):
            values = list(value)
        elif isinstance(value, str):
            values = [part.strip() for part in value.split(",") if part.strip()]
        else:
            raise MarketConfigurationError(f"Matchbook {label} must be a list or comma-separated string of ids.")
        if not values or len(values) > MATCHBOOK_ORDER_MANAGEMENT_MAX_BATCH:
            raise MarketConfigurationError(
                f"Matchbook {label} must contain between 1 and {MATCHBOOK_ORDER_MANAGEMENT_MAX_BATCH} ids."
            )
        parsed = [cls._order_management_id(item) for item in values]
        if len(set(parsed)) != len(parsed):
            raise MarketConfigurationError(f"Matchbook {label} must not contain duplicate ids.")
        return ",".join(str(item) for item in parsed)

    @classmethod
    def _cancel_offer_filters(cls, kwargs: Mapping[str, Any]) -> Dict[str, Any]:
        values: Dict[str, Any] = {}
        offer_ids = kwargs.get("offer_ids")
        if offer_ids is None:
            offer_ids = kwargs.get("orders", kwargs.get("instructions"))
        if offer_ids not in (None, "", []):
            values["offer-ids"] = cls._cancel_offer_filter(offer_ids, "offer_ids")
        for key, parameter in (
            ("event_ids", "event-ids"),
            ("market_ids", "market-ids"),
            ("runner_ids", "runner-ids"),
        ):
            raw = kwargs.get(key)
            if raw not in (None, "", []):
                if "offer-ids" in values:
                    raise MarketConfigurationError("Matchbook cancel_offers accepts offer_ids or scope filters, not both.")
                values[parameter] = cls._cancel_offer_filter(raw, key)
        return values

    def _account_bet_params(
        self,
        *,
        offset: Any,
        limit: Any,
        sport_id: Any,
        event_id: Any,
        market_id: Any,
        from_timestamp: Any,
        to_timestamp: Any,
        odds_type: Any,
    ) -> Dict[str, Any]:
        after = self._account_date(from_timestamp, "from")
        before = self._account_date(to_timestamp, "to")
        if after and before and after > before:
            raise MarketConfigurationError("Matchbook account history requires to at or after from.")
        params: Dict[str, Any] = {
            "offset": self._account_offset(offset),
            "per-page": self._account_limit(limit),
            "odds-type": self._account_odds_type(odds_type),
        }
        for parameter, value, label in (
            ("sport-ids", sport_id, "sport_id"),
            ("event-ids", event_id, "event_id"),
            ("market-ids", market_id, "market_id"),
        ):
            normalized = self._account_ids(value, label)
            if normalized:
                params[parameter] = normalized
        if after:
            params["after"] = after
        if before:
            params["before"] = before
        return params

    @staticmethod
    def _account_ids(value: Any, label: str) -> str:
        clean = str(value or "").strip()
        if not clean:
            return ""
        values = [part.strip() for part in clean.split(",")]
        if len(values) > 100 or any(not part or len(part) > 32 or not part.isdigit() for part in values):
            raise MarketConfigurationError(
                f"Matchbook {label} must be a comma-separated list of numeric ids."
            )
        return ",".join(values)

    @staticmethod
    def _account_limit(value: Any) -> int:
        if isinstance(value, bool):
            raise MarketConfigurationError("Matchbook account limit must be an integer between 1 and 1000.")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Matchbook account limit must be an integer between 1 and 1000.") from exc
        if parsed < 1 or parsed > 1000:
            raise MarketConfigurationError("Matchbook account limit must be an integer between 1 and 1000.")
        return parsed

    @staticmethod
    def _account_offset(value: Any) -> int:
        if isinstance(value, bool):
            raise MarketConfigurationError("Matchbook account offset must be an integer between 0 and 100000.")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Matchbook account offset must be an integer between 0 and 100000.") from exc
        if parsed < 0 or parsed > 100000:
            raise MarketConfigurationError("Matchbook account offset must be an integer between 0 and 100000.")
        return parsed

    @staticmethod
    def _account_odds_type(value: Any) -> str:
        normalized = str(value or "DECIMAL").strip().upper()
        if normalized not in {"DECIMAL", "US", "HK", "MALAY", "INDO", "%"}:
            raise MarketConfigurationError("Matchbook odds_type must be DECIMAL, US, HK, MALAY, INDO, or %.")
        return normalized

    @staticmethod
    def _account_values(value: Any, allowed: Tuple[str, ...], label: str) -> str:
        clean = str(value or "").strip().lower()
        if not clean:
            return ""
        values = [part.strip() for part in clean.split(",")]
        if any(part not in allowed for part in values):
            raise MarketConfigurationError(
                f"Matchbook {label} must contain only: {', '.join(allowed)}."
            )
        return ",".join(values)

    @staticmethod
    def _account_interval(value: Any) -> int:
        if isinstance(value, bool):
            raise MarketConfigurationError("Matchbook interval must be an integer between 0 and 2147483647.")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Matchbook interval must be an integer between 0 and 2147483647.") from exc
        if parsed < 0 or parsed > 2_147_483_647:
            raise MarketConfigurationError("Matchbook interval must be an integer between 0 and 2147483647.")
        return parsed

    @staticmethod
    def _account_aggregation(value: Any) -> str:
        normalized = str(value or "none").strip().lower()
        if normalized not in {"none", "summary", "average"}:
            raise MarketConfigurationError("Matchbook aggregation_type must be none, summary, or average.")
        return normalized

    @classmethod
    def _account_date(cls, value: Any, label: str) -> Optional[str]:
        if value in (None, ""):
            return None
        timestamp = cls._timestamp_seconds(value)
        if timestamp is None or not math.isfinite(timestamp) or timestamp < 0:
            raise MarketConfigurationError(f"Matchbook {label} must be a valid epoch or ISO-8601 timestamp.")
        return cls._timestamp_iso(timestamp)

    @staticmethod
    def _timestamp_iso(value: float) -> str:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    @staticmethod
    def _trade_limit(value: Any) -> int:
        if isinstance(value, bool):
            raise MarketConfigurationError("Matchbook trade limit must be an integer between 1 and 1000.")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Matchbook trade limit must be an integer between 1 and 1000.") from exc
        if parsed < 1 or parsed > 1000:
            raise MarketConfigurationError("Matchbook trade limit must be an integer between 1 and 1000.")
        return parsed

    @staticmethod
    def _history_timestamp(value: Any, label: str) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"Matchbook {label} must be a numeric epoch timestamp.") from exc
        if not math.isfinite(parsed) or parsed < 0:
            raise MarketConfigurationError(f"Matchbook {label} must be a non-negative finite epoch timestamp.")
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
                f"Matchbook candle resolution must be one of: {', '.join(intervals)}."
            ) from exc

    def _candle_trade_limit(self) -> int:
        raw_limit = self.config.get("matchbook_candle_trade_limit", 1000)
        return self._trade_limit(raw_limit)

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
        return {"BACK": "BUY", "WIN": "BUY", "BUY": "BUY", "LAY": "SELL", "LOSE": "SELL", "SELL": "SELL"}.get(
            str(value or "").strip().upper()
        )

    def _validate_order(self, order: PaperOrderRequest) -> None:
        self.ensure_order_market(order)
        self._split_contract_id(order.contract_id)
        if str(order.side or "").upper() not in self.live_order_sides:
            raise MarketConfigurationError("Matchbook order side must be BUY/SELL or BACK/LAY.")
        try:
            size = float(order.size)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Matchbook order stake must be numeric.") from exc
        if not math.isfinite(size) or size <= 0:
            raise MarketConfigurationError("Matchbook order stake must be positive and finite.")
        if order.limit_price is not None and self._safe_probability(order.limit_price) is None:
            raise MarketConfigurationError("Matchbook order limit probability must be between 0 and 1.")

    def _event_from_payload(self, event: Mapping[str, Any]) -> MarketEvent:
        event_id = self._id(event)
        return MarketEvent(
            market_id=self.market_id,
            event_id=event_id,
            title=str(self._value(event, "name", "title") or event_id),
            url=str(self._value(event, "url", "url-name", "urlName") or ""),
            status=str(self._value(event, "state", "status") or "").strip().lower(),
            raw=dict(event),
        )

    def _contracts_from_market(self, market: Mapping[str, Any], *, event_id: str) -> List[MarketContract]:
        market_id = self._id(market)
        market_name = str(self._value(market, "name", "market-name", "marketName") or market_id)
        status = str(self._value(market, "state", "status") or "").strip().lower()
        url = str(self._value(market, "url", "url-name", "urlName") or "")
        contracts: List[MarketContract] = []
        runners = market.get("runners")
        if not isinstance(runners, list):
            return contracts
        for runner in runners:
            if not isinstance(runner, Mapping):
                continue
            runner_id = self._id(runner, "runner-id", "runnerId")
            if not runner_id:
                continue
            runner_name = str(self._value(runner, "name", "runner-name", "runnerName") or runner_id)
            contracts.append(
                MarketContract(
                    market_id=self.market_id,
                    contract_id=self._contract_id(event_id, market_id, runner_id),
                    event_id=event_id,
                    title=f"{market_name} - {runner_name}",
                    outcome=runner_name,
                    url=url,
                    status=status,
                    raw={"market": dict(market), "runner": dict(runner)},
                )
            )
        return contracts

    @classmethod
    def _runner_levels(cls, runner: Mapping[str, Any]) -> Tuple[List[OrderBookLevel], List[OrderBookLevel]]:
        bids: List[OrderBookLevel] = []
        asks: List[OrderBookLevel] = []
        rows: List[Mapping[str, Any]] = []
        prices = runner.get("prices") or runner.get("offers") or []
        if isinstance(prices, list):
            rows = [item for item in prices if isinstance(item, Mapping)]
        elif isinstance(prices, Mapping):
            for side, values in prices.items():
                if isinstance(values, list):
                    rows.extend(
                        [dict(item, side=side) for item in values if isinstance(item, Mapping)]
                    )
                elif isinstance(values, Mapping):
                    rows.append(dict(values, side=side))
        for row in rows:
            side = str(cls._value(row, "side", "type") or "").strip().lower()
            odds = cls._positive_float(cls._value(row, "odds", "decimal-odds", "decimalOdds", "price"))
            amount = cls._positive_float(
                cls._value(row, "available-amount", "availableAmount", "remaining", "size", "amount")
            )
            if odds is None or odds <= 1.0 or amount is None:
                continue
            level = OrderBookLevel(price=1.0 / odds, size=amount)
            if side in {"back", "win", "buy"}:
                bids.append(level)
            elif side in {"lay", "lose", "sell"}:
                asks.append(level)
        bids.sort(key=lambda level: level.price, reverse=True)
        asks.sort(key=lambda level: level.price)
        return bids, asks

    @staticmethod
    def _find_runner(market: Mapping[str, Any], runner_id: str) -> Optional[Mapping[str, Any]]:
        runners = market.get("runners")
        if not isinstance(runners, list):
            return None
        for runner in runners:
            if isinstance(runner, Mapping) and MatchbookAdapter._id(runner, "runner-id", "runnerId") == runner_id:
                return runner
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
            data = payload.get("data")
            if isinstance(data, Mapping):
                return MatchbookAdapter._list_from_payload(data, *keys)
        return []

    @staticmethod
    def _mapping_payload(payload: Any) -> Dict[str, Any]:
        if isinstance(payload, Mapping):
            data = payload.get("data")
            if isinstance(data, Mapping):
                return dict(data)
            return dict(payload)
        return {}

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
    def _search_text(cls, payload: Mapping[str, Any]) -> str:
        values = [cls._value(payload, "id"), cls._value(payload, "name", "title"), cls._value(payload, "description"), cls._value(payload, "url-name", "urlName")]
        return " ".join(str(value or "") for value in values).lower()

    @staticmethod
    def _required_id(value: Any, label: str) -> str:
        clean = str(value or "").strip()
        if not clean:
            raise MarketConfigurationError(f"Matchbook {label} id cannot be empty.")
        return clean

    @staticmethod
    def _contract_id(event_id: str, market_id: str, runner_id: str) -> str:
        return f"{event_id}:{market_id}:{runner_id}"

    @staticmethod
    def _split_contract_id(contract_id: str) -> Tuple[str, str, str]:
        parts = [part.strip() for part in str(contract_id or "").split(":")]
        if len(parts) != 3 or any(not part for part in parts):
            raise MarketConfigurationError("Matchbook contract id must be EVENT_ID:MARKET_ID:RUNNER_ID.")
        return parts[0], parts[1], parts[2]

    @staticmethod
    def _positive_float(value: Any) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and number > 0 else None

    @staticmethod
    def _safe_probability(value: Any) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and 0.0 <= number <= 1.0 else None
