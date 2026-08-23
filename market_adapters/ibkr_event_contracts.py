"""Interactive Brokers event-contract adapters.

IBKR models ForecastEx event contracts as options and CME event contracts as
futures options.  The official Client Portal Web API exposes the discovery,
market-data, and order routes used here.  The adapter deliberately requires an
already-authorized brokerage session; it never handles IBKR passwords or
creates a session on behalf of a user.
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .base import MarketAdapter
from .catalog import get_market_metadata
from .errors import MarketConfigurationError
from .types import (
    MarketCapabilities,
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


DEFAULT_IBKR_API_BASE_URL = "https://api.ibkr.com/v1/api"
IBKR_REFERENCES = (
    "https://www.interactivebrokers.com/campus/ibkr-api-page/event-contracts/",
    "https://www.interactivebrokers.com/docs/web-api/api-reference/trading/trading-market-data/get-md-history",
    "https://www.interactivebrokers.com/docs/web-api/v1/endpoints/order-monitoring/trades",
    "https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/",
    "https://www.interactivebrokers.com/campus/ibkr-api-page/web-api-trading/",
)

IBKR_EVENT_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=True,
    trade_history=True,
    alerts=True,
    paper_trading=True,
    live_trading=True,
    # The documented account-trades feed contains complete execution
    # identity, event conid, side, price, size, and timestamp fields.  Copy
    # previews remain simulation-first and never submit a live order.
    copy_trading=True,
    api_required=True,
    credentials_required=True,
    kyc_required=True,
    region_limited=True,
)

IBKR_CANDLE_RESOLUTIONS = (
    "1min",
    "2min",
    "3min",
    "5min",
    "10min",
    "15min",
    "30min",
    "1h",
    "2h",
    "3h",
    "4h",
    "8h",
    "1d",
    "1w",
    "1m",
)

IBKR_ACCOUNT_OPERATIONS = ("orders", "order_status")
IBKR_ORDER_MANAGEMENT_OPERATIONS = ("cancel_order", "cancel_all_orders", "modify_order")
IBKR_ORDER_MANAGEMENT_CONFIRMATION = "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
IBKR_GLOBAL_CANCEL_CONFIRMATION = "CANCEL ALL IBKR EVENT ORDERS"
IBKR_ORDER_ID_PATTERN = re.compile(r"[0-9]{1,20}")
IBKR_MODIFY_FIELDS = {
    "conid",
    "orderType",
    "price",
    "side",
    "tif",
    "quantity",
    "outsideRth",
    "manualIndicator",
    "extOperator",
}


class IBKREventContractsAdapter(MarketAdapter):
    """Shared implementation for IBKR ForecastEx and CME event contracts."""

    metadata = get_market_metadata("ibkr_forecasttrader")
    venue = "FORECASTX"
    security_type = "OPT"
    _forecastx = True
    event_url_base = "https://forecasttrader.interactivebrokers.com/eventtrader/#/markets"
    account_recovery_operations = IBKR_ACCOUNT_OPERATIONS
    order_management_operations = IBKR_ORDER_MANAGEMENT_OPERATIONS

    def __init__(self, config: Optional[Mapping[str, Any]] = None, *, runtime=None) -> None:
        super().__init__(config, runtime=runtime)
        self._underlier_cache: Dict[str, Mapping[str, Any]] = {}
        self._accounts_ready = False

    @property
    def api_base_url(self) -> str:
        configured = self.config.get("ibkr_api_base_url") or self.config.get("api_base_url")
        return str(configured or DEFAULT_IBKR_API_BASE_URL).rstrip("/")

    @property
    def mode_name(self) -> str:
        return "ForecastEx" if self._forecastx else "CME event contracts"

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        session_cookie = self.resolve_credential(
            "ibkr_session_cookie", ("IBKR_SESSION_COOKIE",), label="IBKR_SESSION_COOKIE"
        )
        access_token = self.resolve_credential(
            "ibkr_access_token", ("IBKR_ACCESS_TOKEN",), label="IBKR_ACCESS_TOKEN"
        )
        account_id = self.resolve_credential("ibkr_account_id", ("IBKR_ACCOUNT_ID",), label="IBKR_ACCOUNT_ID")
        health.update(
            {
                "api_base_url": self.api_base_url,
                "venue": self.venue,
                "security_type": self.security_type,
                "mode": self.mode_name,
                "references": list(IBKR_REFERENCES),
                "credential_sources": [
                    {"name": item.name, "source": item.source}
                    for item in (session_cookie, access_token, account_id)
                    if item is not None
                ],
                "live_trading_enabled": self.config_bool("live_trading_enabled", False),
                "live_submission_enabled": self.config_bool("ibkr_submit_live_orders", False),
                "account_recovery_operations": list(self.account_recovery_operations),
                "order_management_operations": list(self.order_management_operations),
                "order_management_enabled": self.config_bool("ibkr_order_management_enabled", False),
                "authenticated_order_endpoints": [
                    "GET /iserver/account/orders",
                    "GET /iserver/account/:accountId/order/status/:orderId",
                    "POST /iserver/account/:accountId/order/:orderId",
                    "DELETE /iserver/account/:accountId/order/:orderId",
                ],
            }
        )
        return health

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = max(1, min(int(limit or 50), 500))
        needle = str(query or "").strip().lower()
        if self._forecastx:
            payload = self._get("/trsrv/event/category-tree")
            events = []
            for market in self._category_markets(payload):
                symbol = self._symbol(market)
                title = str(market.get("name") or market.get("label") or symbol).strip()
                if not symbol or (needle and needle not in f"{symbol} {title}".lower()):
                    continue
                events.append(
                    MarketEvent(
                        market_id=self.market_id,
                        event_id=self._event_id(symbol),
                        title=title,
                        url=str(market.get("url") or self.event_url_base),
                        status=str(market.get("status") or "active").strip().lower(),
                        raw=dict(market),
                    )
                )
        else:
            products = self._configured_products(query)
            events = []
            for symbol in products:
                try:
                    underlier = self._underlier(symbol)
                except MarketConfigurationError:
                    if query:
                        raise
                    continue
                title = str(
                    underlier.get("companyName")
                    or underlier.get("description")
                    or underlier.get("symbol")
                    or symbol
                ).strip()
                events.append(
                    MarketEvent(
                        market_id=self.market_id,
                        event_id=self._event_id(symbol),
                        title=title,
                        url="https://www.cmegroup.com/markets/event-contracts.html",
                        status="active",
                        raw=dict(underlier),
                    )
                )
        events.sort(key=lambda item: item.title.lower())
        return events[:desired]

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        symbol, requested_month = self._split_event_id(event_id)
        underlier = self._underlier(symbol)
        conid = self._underlier_conid(underlier)
        months = [requested_month] if requested_month else self._months(underlier)
        if not months:
            configured = self._configured_months()
            months = configured
        if not months:
            raise MarketConfigurationError(
                f"{self.mode_name} event {symbol!r} did not provide contract months; "
                "set ibkr_contract_month or ibkr_contract_months."
            )

        records: List[Mapping[str, Any]] = []
        max_months = self._positive_int_config("ibkr_max_contract_months", default=12)
        for month in months[:max_months]:
            params = {
                "conid": conid,
                "exchange": self.venue,
                "sectype": self.security_type,
                "month": month,
            }
            if self._forecastx:
                strikes = self._get("/iserver/secdef/strikes", params=params)
                strike_values = self._strike_values(strikes)
                max_strikes = self._positive_int_config("ibkr_max_strikes_per_month", default=100)
                for strike in strike_values[:max_strikes]:
                    info_params = dict(params)
                    info_params["strike"] = strike
                    info = self._get("/iserver/secdef/info", params=info_params)
                    records.extend(self._records(info))
            else:
                info = self._get("/iserver/secdef/info", params=params)
                records.extend(self._records(info))

        contracts: List[MarketContract] = []
        seen: set[str] = set()
        for record in records:
            contract = self._contract_from_record(record, event_id=self._event_id(symbol))
            if contract is None or contract.contract_id in seen:
                continue
            seen.add(contract.contract_id)
            contracts.append(contract)
        return contracts

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        canonical, conid = self._parse_contract_id(contract_id)
        snapshot = self._snapshot(conid)
        bid = self._number(snapshot.get("84"))
        ask = self._number(snapshot.get("86"))
        last = self._number(snapshot.get("31"))
        midpoint = (bid + ask) / 2.0 if bid is not None and ask is not None else last
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=canonical,
            last=last,
            bid=bid,
            ask=ask,
            midpoint=midpoint,
            source="ibkr_event_contract_snapshot",
            raw=dict(snapshot),
        )

    def get_orderbook(self, contract_id: str) -> OrderBookSnapshot:
        self.ensure_capability("orderbook_reading")
        canonical, conid = self._parse_contract_id(contract_id)
        snapshot = self._snapshot(conid)
        bid = self._number(snapshot.get("84"))
        ask = self._number(snapshot.get("86"))
        bid_size = self._number(snapshot.get("88"))
        ask_size = self._number(snapshot.get("85"))
        bids = [OrderBookLevel(bid, bid_size or 0.0)] if bid is not None else []
        asks = [OrderBookLevel(ask, ask_size or 0.0)] if ask is not None else []
        return OrderBookSnapshot(
            market_id=self.market_id,
            contract_id=canonical,
            bids=bids,
            asks=asks,
            raw=dict(snapshot),
        )

    def list_candles(
        self,
        contract_id: str,
        *,
        resolution: str = "1h",
        from_timestamp: Optional[float] = None,
        to_timestamp: Optional[float] = None,
    ) -> List[MarketCandle]:
        """Return historical Last Trade OHLC bars for an IBKR event contract.

        IBKR's documented ``/iserver/marketdata/history`` endpoint anchors a
        period at ``startTime`` and, with ``direction=-1``, walks backwards.
        The normalized ``to_timestamp`` therefore becomes that anchor and the
        requested range is converted to the smallest supported period.  The
        response is filtered locally so rounded-up periods cannot leak bars
        outside the caller's requested range.
        """

        self.ensure_capability("price_reading")
        canonical, conid = self._parse_contract_id(contract_id)
        clean_resolution = str(resolution or "").strip()
        if clean_resolution not in IBKR_CANDLE_RESOLUTIONS:
            allowed = ", ".join(IBKR_CANDLE_RESOLUTIONS)
            raise MarketConfigurationError(f"{self.mode_name} candle resolution must be one of: {allowed}.")

        end_ts = self._history_timestamp(to_timestamp, "to_timestamp") if to_timestamp is not None else int(time.time())
        default_lookback = self._candle_lookback_seconds(clean_resolution)
        start_ts = (
            self._history_timestamp(from_timestamp, "from_timestamp")
            if from_timestamp is not None
            else max(0, end_ts - default_lookback)
        )
        if end_ts <= start_ts:
            raise MarketConfigurationError(f"{self.mode_name} candle history requires to_timestamp greater than from_timestamp.")

        params: Dict[str, Any] = {
            "conid": conid,
            "period": self._period_for_seconds(end_ts - start_ts),
            "bar": clean_resolution,
            "startTime": datetime.fromtimestamp(end_ts, tz=timezone.utc).strftime("%Y%m%d-%H:%M:%S"),
            "direction": -1,
            "source": "Last",
            "outsideRth": self.config_bool("ibkr_history_outside_rth", False),
        }
        payload = self._get("/iserver/marketdata/history", params=params)
        rows = payload.get("data") if isinstance(payload, Mapping) else []
        if not isinstance(rows, list):
            return []

        candles: List[MarketCandle] = []
        for raw in rows:
            if not isinstance(raw, Mapping):
                continue
            timestamp = self._timestamp_seconds(raw.get("t"))
            values = tuple(self._number(raw.get(key)) for key in ("o", "h", "l", "c"))
            volume = self._number(raw.get("v"))
            if timestamp is None or timestamp < start_ts or timestamp > end_ts or any(value is None for value in values):
                continue
            if volume is not None and volume < 0:
                volume = None
            candles.append(
                MarketCandle(
                    market_id=self.market_id,
                    contract_id=canonical,
                    timestamp=timestamp,
                    open=float(values[0]),
                    high=float(values[1]),
                    low=float(values[2]),
                    close=float(values[3]),
                    volume=volume,
                    raw=dict(raw),
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
        """Return recent executions from IBKR's documented account trade feed.

        ``/iserver/account/trades`` returns the currently selected account's
        executions for the current day and six previous days.  It has no
        contract or timestamp query parameters, so this adapter filters the
        response locally after validating the requested conid and bounds.
        """

        self.ensure_capability("trade_history")
        canonical, conid = self._parse_contract_id(contract_id)
        desired = self._trade_limit(limit)
        before_ts = self._history_timestamp(before, "before") if before is not None else None
        after_ts = self._history_timestamp(after, "after") if after is not None else None
        if before_ts is not None and after_ts is not None and before_ts < after_ts:
            raise MarketConfigurationError(f"{self.mode_name} trade history before must not precede after.")

        payload = self._get("/iserver/account/trades")
        if isinstance(payload, list):
            rows = payload
        elif isinstance(payload, Mapping) and isinstance(payload.get("trades"), list):
            rows = payload["trades"]
        elif isinstance(payload, Mapping) and isinstance(payload.get("data"), list):
            rows = payload["data"]
        else:
            rows = []

        trades: List[MarketTrade] = []
        for raw in rows:
            if not isinstance(raw, Mapping):
                continue
            row_conid = raw.get("conid") or raw.get("conidEx")
            try:
                if row_conid is None or int(str(row_conid).split(";")[0]) != conid:
                    continue
            except (TypeError, ValueError):
                continue
            trade_id = str(raw.get("execution_id") or raw.get("executionId") or "").strip()
            side = self._trade_side(raw.get("side"))
            price = self._number(raw.get("price"))
            size = self._number(raw.get("size") or raw.get("quantity"))
            timestamp = self._trade_timestamp(raw)
            if not trade_id or side is None or price is None or size is None or size <= 0 or timestamp is None:
                continue
            if after_ts is not None and timestamp < after_ts:
                continue
            if before_ts is not None and timestamp > before_ts:
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
            if len(trades) >= desired:
                break
        return trades

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        self._validate_order(order)
        canonical, conid = self._parse_contract_id(order.contract_id)
        request = self._order_payload(order, conid=conid)
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=canonical,
            accepted=True,
            message=f"DRY RUN: would submit {self.mode_name} {order.side.upper()} order for {order.size:g} contracts",
            average_price=order.limit_price,
            raw={"request": request, "venue": self.venue},
        )

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        self.ensure_capability("live_trading")
        self._validate_order(order)
        preflight = self.preflight_live_order(order, feature_name=f"{self.mode_name} live trading")
        if not self.config_bool("ibkr_submit_live_orders", False):
            raise MarketConfigurationError(
                f"{self.mode_name} live orders require ibkr_submit_live_orders=true after reviewing the IBKR session/order controls."
            )
        if order.limit_price is None:
            raise MarketConfigurationError(f"{self.mode_name} live orders require a limit price.")
        account = self.resolve_credential("ibkr_account_id", ("IBKR_ACCOUNT_ID",), required=True, label="IBKR_ACCOUNT_ID")
        canonical, conid = self._parse_contract_id(order.contract_id)
        self._ensure_accounts()
        payload = {"orders": [self._order_payload(order, conid=conid, include_manual_fields=True)]}
        response = self._post(f"/iserver/account/{account.value}/orders", payload)
        return {
            "market_id": self.market_id,
            "contract_id": canonical,
            "account_id": account.redacted,
            "venue": self.venue,
            "live": True,
            "preflight": preflight,
            "request": payload,
            "response": response,
            "confirmation_required": self._requires_confirmation(response),
        }

    def account_recovery(self, operation: str, **kwargs: Any) -> Dict[str, Any]:
        """Read the documented IBKR open-order and order-status surfaces."""

        normalized = str(operation or "").strip().lower()
        if normalized not in self.account_recovery_operations:
            supported = ", ".join(self.account_recovery_operations)
            raise MarketConfigurationError(f"{self.mode_name} account operation must be one of: {supported}.")
        account = self.resolve_credential("ibkr_account_id", ("IBKR_ACCOUNT_ID",), required=True, label="IBKR_ACCOUNT_ID")
        self._ensure_accounts()
        if normalized == "orders":
            filters = str(kwargs.get("filters") or "").strip()
            if filters and not re.fullmatch(r"[A-Za-z0-9_, ]{1,120}", filters):
                raise MarketConfigurationError("IBKR order filters must contain only documented filter names.")
            params: Dict[str, Any] = {"accountId": account.value}
            if filters:
                params["filters"] = filters
            if "force" in kwargs and kwargs.get("force") is not None:
                params["force"] = bool(kwargs.get("force"))
            response = self._get("/iserver/account/orders", params=params)
        else:
            order_id = self._order_id(kwargs.get("order_id"))
            response = self._get(f"/iserver/account/{account.value}/order/status/{order_id}")
        return {
            "market_id": self.market_id,
            "operation": normalized,
            "account_id": account.redacted,
            "response": response,
        }

    def manage_orders(self, operation: str, **kwargs: Any) -> Dict[str, Any]:
        """Run fixed-path, explicitly confirmed IBKR order mutations."""

        normalized = str(operation or "").strip().lower()
        if normalized not in self.order_management_operations:
            supported = ", ".join(self.order_management_operations)
            raise MarketConfigurationError(f"{self.mode_name} order-management operation must be one of: {supported}.")
        self.ensure_capability("live_trading")
        if not self.config_bool("ibkr_order_management_enabled", False):
            raise MarketConfigurationError(
                f"{self.mode_name} order management is disabled by adapter config. "
                "Set ibkr_order_management_enabled=true only after reviewing live-order risk."
            )
        self.ensure_live_trading_enabled(f"{self.mode_name} order management")
        if str(kwargs.get("confirm_order_management") or "").strip() != IBKR_ORDER_MANAGEMENT_CONFIRMATION:
            raise MarketConfigurationError(
                "IBKR order management requires exact confirmation text "
                f"{IBKR_ORDER_MANAGEMENT_CONFIRMATION}."
            )
        if bool(kwargs.get("async_request")):
            raise MarketConfigurationError(f"{self.mode_name} order-management requests are synchronous.")

        account = self.resolve_credential("ibkr_account_id", ("IBKR_ACCOUNT_ID",), required=True, label="IBKR_ACCOUNT_ID")
        self._ensure_accounts()
        order_id = "-1" if normalized == "cancel_all_orders" else self._order_id(kwargs.get("order_id"))
        if normalized == "cancel_all_orders":
            if str(kwargs.get("confirm_global_cancel") or "").strip() != IBKR_GLOBAL_CANCEL_CONFIRMATION:
                raise MarketConfigurationError(
                    "IBKR global cancellation requires exact confirmation "
                    f"{IBKR_GLOBAL_CANCEL_CONFIRMATION}."
                )

        params = self._manual_order_params(kwargs)
        path = f"/iserver/account/{account.value}/order/{order_id}"
        if normalized == "modify_order":
            request = self._modify_order_payload(kwargs.get("instructions"), kwargs)
            response = self.runtime.request_json("POST", self._url(path), params=params, json_body=request, headers=self._auth_headers())
        else:
            request = None
            response = self.runtime.request_json("DELETE", self._url(path), params=params, headers=self._auth_headers())
        return {
            "market_id": self.market_id,
            "operation": normalized,
            "account_id": account.redacted,
            "order_id": order_id,
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
                "manual_indicator": params.get("manualIndicator"),
                "external_operator_supplied": "extOperator" in params,
            },
            "request": request,
            "response": response,
        }

    def copy_trade_from_activity(self, activity: Mapping[str, Any]) -> PaperOrderResult:
        """Build a local copy preview from an authenticated IBKR execution.

        IBKR's documented ``/iserver/account/trades`` feed provides the
        execution id, event-contract conid, B/S side, execution price, and
        filled size.  The preview validates those fields and routes only to
        the local paper-order path; it never calls an order endpoint.
        """

        self.ensure_capability("copy_trading")
        contract_id = str(activity.get("asset") or activity.get("contract_id") or "").strip()
        raw_conid = activity.get("conid") or activity.get("conidEx")
        if contract_id:
            canonical, conid = self._parse_contract_id(contract_id)
            if raw_conid not in (None, ""):
                try:
                    supplied = int(str(raw_conid).split(";")[0])
                except (TypeError, ValueError) as exc:
                    raise MarketConfigurationError("IBKR activity conid must be numeric.") from exc
                if supplied != conid:
                    raise MarketConfigurationError("IBKR activity conid does not match contract_id.")
        else:
            if raw_conid in (None, ""):
                raise MarketConfigurationError("IBKR activity requires contract_id or conid.")
            canonical = self._contract_id(raw_conid)
            conid = int(canonical.split(":", 1)[1])

        side = self._trade_side(activity.get("side"))
        if side not in {"BUY", "SELL"}:
            raise MarketConfigurationError("IBKR activity side must be B/S or BUY/SELL.")
        price = self._finite_positive(activity.get("price"), "IBKR activity price")
        if price > 1.0:
            raise MarketConfigurationError("IBKR event-contract activity price must be between 0 and 1.")
        size = self._finite_positive(activity.get("size") or activity.get("quantity"), "IBKR activity size")
        if not float(size).is_integer():
            raise MarketConfigurationError("IBKR event-contract activity size must be a whole number.")
        trade_id = str(
            activity.get("execution_id")
            or activity.get("executionId")
            or activity.get("trade_id")
            or activity.get("tradeId")
            or activity.get("id")
            or ""
        ).strip()
        if not trade_id:
            raise MarketConfigurationError("IBKR activity requires a documented execution id.")

        preview = self.place_paper_order(
            PaperOrderRequest(
                market_id=self.market_id,
                contract_id=canonical,
                side=side,
                size=size,
                limit_price=price,
                metadata={"activity": dict(activity), "source": "ibkr_authenticated_account_trades", "conid": conid},
            )
        )
        preview.raw["source"] = "ibkr_authenticated_account_trades"
        preview.raw["activity"] = dict(activity)
        preview.raw["execution_id"] = trade_id
        return preview

    def _get(self, path: str, *, params: Optional[Mapping[str, Any]] = None) -> Any:
        return self.runtime.get_json(self._url(path), params=params, headers=self._auth_headers())

    def _post(self, path: str, payload: Mapping[str, Any]) -> Any:
        return self.runtime.request_json("POST", self._url(path), json_body=payload, headers=self._auth_headers())

    def _ensure_accounts(self) -> None:
        if not self._accounts_ready:
            self._get("/iserver/accounts")
            self._accounts_ready = True

    def _auth_headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        session = self.resolve_credential(
            "ibkr_session_cookie", ("IBKR_SESSION_COOKIE",), required=False, label="IBKR_SESSION_COOKIE"
        )
        token = self.resolve_credential(
            "ibkr_access_token", ("IBKR_ACCESS_TOKEN",), required=False, label="IBKR_ACCESS_TOKEN"
        )
        if session:
            cookie = session.value.strip()
            headers["Cookie"] = cookie if "=" in cookie else f"api={cookie}"
        if token:
            headers["Authorization"] = f"Bearer {token.value}"
        if not headers:
            raise MarketConfigurationError(
                f"{self.mode_name} requires an authorized IBKR Web API session; set IBKR_SESSION_COOKIE or IBKR_ACCESS_TOKEN."
            )
        return headers

    def _snapshot(self, conid: int) -> Mapping[str, Any]:
        if not self._accounts_ready and self.config_bool("ibkr_accounts_preflight", True):
            self._ensure_accounts()
        params = {"conids": str(conid), "fields": "31,84,85,86,88,7059"}
        payload = self._get("/iserver/marketdata/snapshot", params=params)
        snapshot = self._first_record(payload)
        if not any(key in snapshot for key in ("31", "84", "86")):
            payload = self._get("/iserver/marketdata/snapshot", params=params)
            snapshot = self._first_record(payload)
        return snapshot

    def _underlier(self, symbol: str) -> Mapping[str, Any]:
        normalized = str(symbol or "").strip().upper()
        if not normalized:
            raise MarketConfigurationError("IBKR event product symbol cannot be empty.")
        cached = self._underlier_cache.get(normalized)
        if cached is not None:
            return cached
        params: Dict[str, Any] = {"symbol": normalized}
        if not self._forecastx:
            params["secType"] = "IND"
        payload = self._get("/iserver/secdef/search", params=params)
        records = self._records(payload)
        if not records:
            raise MarketConfigurationError(f"IBKR event product {normalized!r} was not found.")
        selected = self._select_underlier(records, normalized)
        self._underlier_cache[normalized] = selected
        return selected

    def _select_underlier(self, records: Sequence[Mapping[str, Any]], symbol: str) -> Mapping[str, Any]:
        for record in records:
            sections = record.get("sections")
            section_types = {str(item.get("secType") or "").upper() for item in sections if isinstance(item, Mapping)} if isinstance(sections, list) else set()
            text = f"{record.get('symbol', '')} {record.get('description', '')}".upper()
            if str(record.get("symbol") or "").upper() == symbol and (not self._forecastx and "EC" in section_types):
                return record
            if self._forecastx and str(record.get("symbol") or "").upper() == symbol:
                return record
            if symbol in text and (self._forecastx or "EC" in section_types):
                return record
        return records[0]

    def _contract_from_record(self, record: Mapping[str, Any], *, event_id: str) -> Optional[MarketContract]:
        conid = record.get("conid") or record.get("conidEx")
        if conid in (None, ""):
            return None
        trading_class = str(record.get("tradingClass") or "").upper()
        if not self._forecastx and trading_class and not trading_class.startswith(f"EC{event_id.split(':')[-1].upper()}"):
            return None
        right = str(record.get("right") or "").upper()
        if right not in {"C", "P"}:
            return None
        outcome = "YES" if right == "C" else "NO"
        canonical = self._contract_id(conid)
        description = str(record.get("desc2") or record.get("localSymbol") or record.get("symbol") or canonical)
        return MarketContract(
            market_id=self.market_id,
            contract_id=canonical,
            event_id=event_id,
            title=f"{description} ({outcome})",
            outcome=outcome,
            url=self.event_url_base if self._forecastx else "https://www.cmegroup.com/markets/event-contracts.html",
            status="active",
            raw=dict(record),
        )

    def _order_payload(
        self,
        order: PaperOrderRequest,
        *,
        conid: int,
        include_manual_fields: bool = False,
    ) -> Dict[str, Any]:
        if order.limit_price is None:
            raise MarketConfigurationError(f"{self.mode_name} orders require a limit price.")
        payload: Dict[str, Any] = {
            "conid": conid,
            "orderType": str(order.metadata.get("order_type") or "LMT").upper(),
            "price": float(order.limit_price),
            "side": str(order.side or "").upper(),
            "tif": str(order.metadata.get("tif") or "DAY").upper(),
            "quantity": float(order.size),
        }
        if order.metadata.get("outside_rth") is not None:
            payload["outsideRth"] = bool(order.metadata["outside_rth"])
        if include_manual_fields:
            payload.update(self._manual_order_params(order.metadata))
        return payload

    def _manual_order_params(self, values: Mapping[str, Any]) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        manual = values.get("manual_indicator")
        if manual is None:
            manual = values.get("manualIndicator")
        operator = values.get("external_operator")
        if operator in (None, ""):
            operator = values.get("extOperator")
        if self.venue == "CME":
            if manual is None:
                manual = self.config.get("ibkr_manual_indicator")
            if operator in (None, ""):
                operator = self.config.get("ibkr_external_operator")
            if manual is None:
                raise MarketConfigurationError(
                    "CME event-contract order mutations require explicit ibkr_manual_indicator (true/false)."
                )
            if operator in (None, ""):
                raise MarketConfigurationError(
                    "CME event-contract order mutations require ibkr_external_operator."
                )
        if manual is not None:
            if isinstance(manual, str):
                normalized = manual.strip().lower()
                if normalized not in {"true", "false", "1", "0"}:
                    raise MarketConfigurationError("IBKR manual_indicator must be true or false.")
                manual = normalized in {"true", "1"}
            elif not isinstance(manual, bool):
                raise MarketConfigurationError("IBKR manual_indicator must be true or false.")
            params["manualIndicator"] = manual
        if operator not in (None, ""):
            normalized_operator = str(operator).strip()
            if not re.fullmatch(r"[A-Za-z0-9_.@:+-]{1,80}", normalized_operator):
                raise MarketConfigurationError("IBKR external_operator contains unsupported characters.")
            params["extOperator"] = normalized_operator
        return params

    @classmethod
    def _order_id(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if not IBKR_ORDER_ID_PATTERN.fullmatch(normalized):
            raise MarketConfigurationError("IBKR order id must be a positive numeric identifier.")
        return normalized

    def _modify_order_payload(self, value: Any, context: Mapping[str, Any]) -> Dict[str, Any]:
        if not isinstance(value, Mapping):
            raise MarketConfigurationError("IBKR modify_order instructions must be one JSON order object.")
        unknown = sorted(str(key) for key in value if str(key) not in IBKR_MODIFY_FIELDS)
        if unknown:
            raise MarketConfigurationError("IBKR modify_order contains unsupported fields: " + ", ".join(unknown))
        required = {"conid", "orderType", "side", "tif", "quantity", "price"}
        missing = sorted(key for key in required if key not in value)
        if missing:
            raise MarketConfigurationError("IBKR modify_order is missing required fields: " + ", ".join(missing))
        try:
            conid = int(str(value["conid"]).strip())
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("IBKR modify_order conid must be numeric.") from exc
        if conid <= 0:
            raise MarketConfigurationError("IBKR modify_order conid must be positive.")
        order_type = str(value["orderType"] or "").strip().upper()
        if order_type != "LMT":
            raise MarketConfigurationError("IBKR event-contract modify_order supports only LMT orders.")
        side = str(value["side"] or "").strip().upper()
        allowed_sides = {"BUY"} if self._forecastx else {"BUY", "SELL"}
        if side not in allowed_sides:
            raise MarketConfigurationError(
                f"{self.mode_name} modify_order side must be one of: {', '.join(sorted(allowed_sides))}."
            )
        tif = str(value["tif"] or "").strip().upper()
        if tif not in {"DAY", "GTC"}:
            raise MarketConfigurationError("IBKR event-contract modify_order tif must be DAY or GTC.")
        quantity = self._finite_positive(value["quantity"], "quantity")
        if not float(quantity).is_integer():
            raise MarketConfigurationError("IBKR event-contract quantity must be a whole number.")
        price = self._finite_positive(value.get("price"), "price")
        if price > 1.0:
            raise MarketConfigurationError("IBKR event-contract modify_order price must be between 0 and 1.")
        request: Dict[str, Any] = {
            "conid": conid,
            "orderType": order_type,
            "side": side,
            "tif": tif,
            "quantity": quantity,
            "price": price,
        }
        if "outsideRth" in value:
            request["outsideRth"] = bool(value["outsideRth"])
        request.update(self._manual_order_params({**value, **context}))
        return request

    def _validate_order(self, order: PaperOrderRequest) -> None:
        self.ensure_order_market(order)
        self._parse_contract_id(order.contract_id)
        side = str(order.side or "").upper()
        allowed = {"BUY"} if self._forecastx else {"BUY", "SELL"}
        if side not in allowed:
            raise MarketConfigurationError(f"{self.mode_name} order side must be one of: {', '.join(sorted(allowed))}.")
        size = self._finite_positive(order.size, "order size")
        if not float(size).is_integer():
            # Fractional quantities are not valid event-contract orders.
            raise MarketConfigurationError(f"{self.mode_name} event-contract quantity must be a whole number.")
        if order.limit_price is None:
            raise MarketConfigurationError(f"{self.mode_name} orders require a limit price.")
        price = self._finite_positive(order.limit_price, "limit price")
        if price > 1.0:
            raise MarketConfigurationError(f"{self.mode_name} event-contract price must be between 0 and 1.")

    def _configured_products(self, query: str) -> List[str]:
        configured = self.config.get("ibkr_event_products")
        if configured in (None, ""):
            configured = self.config.get("ibkr_product_codes")
        if isinstance(configured, str):
            values = [part.strip().upper() for part in configured.replace(";", ",").split(",") if part.strip()]
        elif isinstance(configured, Iterable):
            values = [str(part).strip().upper() for part in configured if str(part).strip()]
        else:
            values = []
        needle = str(query or "").strip().upper()
        if needle:
            return [needle] if needle not in values else [needle]
        return list(dict.fromkeys(values))

    def _configured_months(self) -> List[str]:
        value = self.config.get("ibkr_contract_months") or self.config.get("ibkr_contract_month")
        if isinstance(value, str):
            return [part.strip().upper() for part in value.replace(";", ",").split(",") if part.strip()]
        if isinstance(value, Iterable):
            return [str(part).strip().upper() for part in value if str(part).strip()]
        return []

    @staticmethod
    def _category_markets(payload: Any) -> List[Mapping[str, Any]]:
        if isinstance(payload, Mapping) and isinstance(payload.get("data"), Mapping):
            payload = payload["data"]
        values = payload.values() if isinstance(payload, Mapping) else payload if isinstance(payload, list) else []
        markets: List[Mapping[str, Any]] = []
        for category in values:
            if not isinstance(category, Mapping):
                continue
            raw = category.get("markets")
            if isinstance(raw, list):
                markets.extend(item for item in raw if isinstance(item, Mapping))
        return markets

    def _months(self, record: Mapping[str, Any]) -> List[str]:
        months: List[str] = []
        sections = record.get("sections")
        if isinstance(sections, list):
            for section in sections:
                if not isinstance(section, Mapping):
                    continue
                raw = section.get("months") or section.get("month") or section.get("opt")
                if isinstance(raw, str):
                    months.extend(part.strip().upper() for part in raw.split(";") if part.strip())
        return list(dict.fromkeys(months))

    @staticmethod
    def _strike_values(payload: Any) -> List[float]:
        values: List[float] = []
        if isinstance(payload, Mapping):
            for key in ("call", "put", "strikes"):
                raw = payload.get(key)
                if isinstance(raw, list):
                    for item in raw:
                        try:
                            number = float(item)
                        except (TypeError, ValueError):
                            continue
                        if number not in values:
                            values.append(number)
        return values

    @staticmethod
    def _records(payload: Any) -> List[Mapping[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, Mapping)]
        if isinstance(payload, Mapping):
            for key in ("data", "results", "contracts", "instruments"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, Mapping)]
        return []

    @staticmethod
    def _first_record(payload: Any) -> Mapping[str, Any]:
        records = IBKREventContractsAdapter._records(payload)
        if records:
            return records[0]
        return dict(payload) if isinstance(payload, Mapping) else {}

    @staticmethod
    def _symbol(record: Mapping[str, Any]) -> str:
        return str(record.get("symbol") or record.get("productCode") or record.get("ticker") or "").strip().upper()

    @staticmethod
    def _underlier_conid(record: Mapping[str, Any]) -> int:
        raw = record.get("conid") or record.get("conidEx")
        try:
            return int(str(raw).split(";")[0])
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("IBKR event underlier did not include a numeric conid.") from exc

    @staticmethod
    def _event_id(symbol: str) -> str:
        return f"IBKR:{str(symbol or '').strip().upper()}"

    @staticmethod
    def _split_event_id(event_id: str) -> Tuple[str, Optional[str]]:
        raw = str(event_id or "").strip()
        if not raw:
            raise MarketConfigurationError("IBKR event id cannot be empty.")
        parts = [part.strip().upper() for part in raw.split(":") if part.strip()]
        if parts and parts[0] == "IBKR":
            parts = parts[1:]
        if not parts:
            raise MarketConfigurationError("IBKR event id must include a product symbol.")
        return parts[0], parts[1] if len(parts) > 1 else None

    @staticmethod
    def _contract_id(conid: Any) -> str:
        try:
            return f"IBKR:{int(str(conid).split(';')[0])}"
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"IBKR contract id is not numeric: {conid!r}") from exc

    @staticmethod
    def _parse_contract_id(contract_id: str) -> Tuple[str, int]:
        raw = str(contract_id or "").strip()
        parts = raw.split(":")
        value = parts[-1].strip()
        if not value.isdigit():
            raise MarketConfigurationError("IBKR contract id must be IBKR:<numeric conid>.")
        return IBKREventContractsAdapter._contract_id(value), int(value)

    @staticmethod
    def _number(value: Any) -> Optional[float]:
        if value in (None, "", "-"):
            return None
        try:
            return float(str(value).replace(",", ""))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _history_timestamp(value: Any, label: str) -> int:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"{label} must be a numeric epoch timestamp.") from exc
        if not math.isfinite(number) or number < 0:
            raise MarketConfigurationError(f"{label} must be a non-negative finite epoch timestamp.")
        return int(number)

    @staticmethod
    def _timestamp_seconds(value: Any) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number) or number <= 0:
            return None
        if number >= 100_000_000_000:
            number /= 1000.0
        return number

    @staticmethod
    def _trade_limit(value: Any) -> int:
        if isinstance(value, bool):
            raise MarketConfigurationError("IBKR trade limit must be an integer between 1 and 500.")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("IBKR trade limit must be an integer between 1 and 500.") from exc
        if parsed < 1 or parsed > 500:
            raise MarketConfigurationError("IBKR trade limit must be an integer between 1 and 500.")
        return parsed

    @staticmethod
    def _trade_side(value: Any) -> Optional[str]:
        return {"B": "BUY", "BUY": "BUY", "S": "SELL", "SELL": "SELL"}.get(str(value or "").strip().upper())

    @classmethod
    def _trade_timestamp(cls, row: Mapping[str, Any]) -> Optional[float]:
        timestamp = cls._timestamp_seconds(row.get("trade_time_r") or row.get("tradeTimeR"))
        if timestamp is not None:
            return timestamp
        text = str(row.get("trade_time") or row.get("tradeTime") or "").strip()
        if not text:
            return None
        for fmt in ("%Y%m%d-%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                parsed = datetime.strptime(text, fmt)
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        return None

    @staticmethod
    def _candle_lookback_seconds(resolution: str) -> int:
        if resolution.endswith("min"):
            return max(3600, int(resolution[:-3]) * 60 * 100)
        if resolution.endswith("h"):
            return max(86400, int(resolution[:-1]) * 3600 * 100)
        if resolution.endswith("d"):
            return max(86400, int(resolution[:-1]) * 86400 * 30)
        if resolution.endswith("w"):
            return int(resolution[:-1]) * 604800 * 20
        return 30 * 86400

    @staticmethod
    def _period_for_seconds(seconds: int) -> str:
        if seconds <= 0:
            raise MarketConfigurationError("IBKR candle history period must be positive.")
        units = (
            (60, "min", 30),
            (3600, "h", 8),
            (86400, "d", 1000),
            (604800, "w", 792),
            (2_592_000, "m", 182),
            (31_536_000, "y", 15),
        )
        for size, suffix, maximum in units:
            count = int(math.ceil(seconds / size))
            if count <= maximum:
                return f"{max(1, count)}{suffix}"
        raise MarketConfigurationError("IBKR candle history range exceeds the documented 15-year maximum.")

    @staticmethod
    def _finite_positive(value: Any, label: str) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"{label} must be numeric.") from exc
        if number <= 0 or number != number or number in {float("inf"), float("-inf")}:
            raise MarketConfigurationError(f"{label} must be positive and finite.")
        return number

    def _url(self, path: str) -> str:
        return f"{self.api_base_url}/{'/'.join(part for part in str(path or '').split('/') if part)}"

    def _positive_int_config(self, key: str, *, default: int) -> int:
        raw = self.config.get(key, default)
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"{self.market_id} config {key} must be an integer.") from exc
        if value <= 0:
            raise MarketConfigurationError(f"{self.market_id} config {key} must be greater than zero.")
        return value

    @staticmethod
    def _requires_confirmation(response: Any) -> bool:
        if not isinstance(response, Mapping):
            return False
        return bool(response.get("warning") or response.get("messageIds") or response.get("confirmations"))


class IBKRForecastTraderAdapter(IBKREventContractsAdapter):
    """IBKR ForecastTrader view over ForecastEx event contracts."""

    metadata = get_market_metadata("ibkr_forecasttrader")
    venue = "FORECASTX"
    security_type = "OPT"
    _forecastx = True


class ForecastExAdapter(IBKREventContractsAdapter):
    """ForecastEx event contracts routed through the official IBKR Web API."""

    metadata = get_market_metadata("forecastex")
    venue = "FORECASTX"
    security_type = "OPT"
    _forecastx = True
    event_url_base = "https://forecastex.com/markets/"


class CMEPredictionMarketsAdapter(IBKREventContractsAdapter):
    """CME event contracts routed through the official IBKR Web API."""

    metadata = get_market_metadata("cme_prediction_markets")
    venue = "CME"
    security_type = "FOP"
    _forecastx = False
    event_url_base = "https://www.cmegroup.com/markets/event-contracts.html"
