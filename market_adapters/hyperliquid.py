from __future__ import annotations

import math
import re
import time
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from .base import MarketAdapter
from .catalog import get_market_metadata
from .errors import MarketConfigurationError
from .identity import require_activity_identity
from .types import (
    MarketCandle,
    MarketContract,
    MarketEvent,
    OrderBookLevel,
    OrderBookSnapshot,
    PaperOrderRequest,
    PaperOrderResult,
    MarketTrade,
    PriceSnapshot,
)


DEFAULT_HYPERLIQUID_MAINNET_URL = "https://api.hyperliquid.xyz"
DEFAULT_HYPERLIQUID_TESTNET_URL = "https://api.hyperliquid-testnet.xyz"
HYPERLIQUID_REFERENCES = (
    "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/spot",
    "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint",
    "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/asset-ids",
    "https://hyperliquid.gitbook.io/Hyperliquid-docs/for-developers/api/exchange-endpoint",
)
OUTCOME_ID_RE = re.compile(r"^[0-9]+$")
HYPERLIQUID_CANDLE_RESOLUTIONS = (
    "1m",
    "3m",
    "5m",
    "15m",
    "30m",
    "1h",
    "2h",
    "4h",
    "8h",
    "12h",
    "1d",
    "3d",
    "1w",
    "1M",
)
HYPERLIQUID_ORDER_MANAGEMENT_OPERATIONS = (
    "cancel_order",
    "cancel_orders",
    "cancel_by_cloid",
    "modify_order",
    "batch_modify_orders",
    "schedule_cancel",
)
HYPERLIQUID_ORDER_MANAGEMENT_CONFIRMATION = "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
HYPERLIQUID_SCHEDULE_CANCEL_CONFIRMATION = "SCHEDULE HYPERLIQUID CANCEL"
HYPERLIQUID_ORDER_MANAGEMENT_MAX_BATCH = 50
HYPERLIQUID_ASSET_BASE = 100_000_000
HYPERLIQUID_CLOID_RE = re.compile(r"^0x[0-9a-fA-F]{32}$")


class HyperliquidAdapter(MarketAdapter):
    """Official Hyperliquid HIP-4 outcome-market adapter.

    HIP-4 exposes outcome metadata through the public ``info`` endpoint and
    represents each binary side as a synthetic spot coin ``#<encoding>`` where
    ``encoding = 10 * outcome + side``.  The adapter maps the documented
    metadata and ``l2Book`` responses to the shared model.  Live submission is
    accepted only when the caller supplies a complete externally signed
    HyperCore exchange payload; this class never handles private keys.
    """

    metadata = get_market_metadata("hyperliquid")
    live_order_sides = ("BUY", "SELL")
    # Private account reads are restricted to the documented Info endpoint
    # request types below.  The shared CLI/API/React surfaces consume this
    # allow-list so arbitrary authenticated payloads cannot be requested.
    account_recovery_operations = (
        "active_orders",
        "order_history",
        "positions",
        "spot_balances",
        "portfolio",
        "subaccounts",
    )
    order_management_operations = HYPERLIQUID_ORDER_MANAGEMENT_OPERATIONS

    def __init__(self, config: Optional[Mapping[str, Any]] = None, *, runtime=None) -> None:
        super().__init__(config, runtime=runtime)
        self._outcome_cache: Dict[str, Dict[str, Any]] = {}
        self._question_cache: Dict[str, Dict[str, Any]] = {}

    @property
    def api_base_url(self) -> str:
        configured = self.config.get("hyperliquid_api_base_url") or self.config.get("api_base_url")
        network = str(self.config.get("hyperliquid_network") or "mainnet").strip().lower()
        default = DEFAULT_HYPERLIQUID_TESTNET_URL if network == "testnet" else DEFAULT_HYPERLIQUID_MAINNET_URL
        base = str(configured or default).strip().rstrip("/")
        parsed = urlsplit(base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
            raise MarketConfigurationError("Hyperliquid API base URL must be an absolute http(s) URL without query or fragment.")
        if network not in {"mainnet", "testnet"}:
            raise MarketConfigurationError("Hyperliquid network must be 'mainnet' or 'testnet'.")
        return base

    @property
    def network(self) -> str:
        value = str(self.config.get("hyperliquid_network") or "mainnet").strip().lower()
        if value not in {"mainnet", "testnet"}:
            raise MarketConfigurationError("Hyperliquid network must be 'mainnet' or 'testnet'.")
        return value

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        health.update(
            {
                "api_base_url": self.api_base_url,
                "network": self.network,
                "references": list(HYPERLIQUID_REFERENCES),
                "public_api": True,
                "live_trading_supported": True,
                "live_trading_enabled": self.config_bool("live_trading_enabled", False),
                "external_signature_required": True,
                "activity_feed_supported": True,
                "copy_trading_supported": bool(self.capabilities.copy_trading),
                "account_recovery_operations": list(self.account_recovery_operations),
                "order_management_operations": list(self.order_management_operations),
                "authenticated_account_endpoints": [
                    "openOrders",
                    "historicalOrders",
                    "clearinghouseState",
                    "spotClearinghouseState",
                    "portfolio",
                    "subAccounts",
                ],
                "order_management_enabled": self.config_bool("hyperliquid_order_management_enabled", False),
                "authenticated_order_management_endpoints": [
                    "POST /exchange action.cancel/cancelByCloid",
                    "POST /exchange action.modify/batchModify",
                    "POST /exchange action.scheduleCancel",
                ],
            }
        )
        return health

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = max(1, min(int(limit or 50), 1000))
        payload = self._info({"type": "outcomeMeta"})
        outcomes = payload.get("outcomes") if isinstance(payload, Mapping) else None
        questions = payload.get("questions") if isinstance(payload, Mapping) else None
        if not isinstance(outcomes, list):
            raise MarketConfigurationError("Hyperliquid outcomeMeta returned no outcomes array.")
        self._outcome_cache = {}
        self._question_cache = {}
        rows: List[Tuple[str, Mapping[str, Any]]] = []
        for row in outcomes:
            if isinstance(row, Mapping):
                outcome_id = self._outcome_id(row.get("outcome"))
                self._outcome_cache[outcome_id] = dict(row)
                rows.append((f"outcome:{outcome_id}", row))
        if isinstance(questions, list):
            for row in questions:
                if isinstance(row, Mapping) and row.get("question") is not None:
                    question_id = self._outcome_id(row.get("question"))
                    self._question_cache[question_id] = dict(row)
                    rows.append((f"question:{question_id}", row))

        needle = str(query or "").strip().lower()
        events: List[MarketEvent] = []
        for event_id, row in rows:
            text = self._search_text(row)
            if needle and needle not in text:
                continue
            events.append(self._event_from_row(event_id, row))
            if len(events) >= desired:
                break
        return events

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        kind, item_id = self._split_event_id(event_id)
        if kind == "outcome":
            row = self._outcome_cache.get(item_id)
            if not row:
                row = self._load_outcomes().get(item_id, {})
            if not row:
                raise MarketConfigurationError(f"Hyperliquid outcome {item_id!r} was not found.")
            side_specs = row.get("sideSpecs")
            if not isinstance(side_specs, list) or not side_specs:
                raise MarketConfigurationError(f"Hyperliquid outcome {item_id!r} did not return side specifications.")
            return [self._contract_from_side(item_id, index, side, row) for index, side in enumerate(side_specs[:2])]

        question = self._question_cache.get(item_id)
        if not question:
            self._load_outcomes()
            question = self._question_cache.get(item_id, {})
        if not question:
            raise MarketConfigurationError(f"Hyperliquid question {item_id!r} was not found.")
        named = question.get("namedOutcomes")
        if not isinstance(named, list):
            named = []
        contracts: List[MarketContract] = []
        for index, outcome_id in enumerate(named):
            outcome_key = self._outcome_id(outcome_id)
            contracts.append(
                MarketContract(
                    market_id=self.market_id,
                    contract_id=f"question:{item_id}:{outcome_key}",
                    event_id=f"question:{item_id}",
                    title=f"{self._title(question, item_id)} - {outcome_key}",
                    outcome=outcome_key,
                    url=f"{self.api_base_url}/trade",
                    status="open",
                    raw={"question": dict(question), "outcome_id": outcome_key, "outcome_index": index},
                )
            )
        return contracts

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        outcome_id, side = self._split_contract_id(contract_id)
        book = self.get_orderbook(contract_id)
        bid = book.bids[0].price if book.bids else None
        ask = book.asks[0].price if book.asks else None
        midpoint = (bid + ask) / 2.0 if bid is not None and ask is not None else bid or ask
        if midpoint is None:
            mids = self._info({"type": "allMids"})
            coin = self._coin(outcome_id, side)
            midpoint = self._probability(mids.get(coin) if isinstance(mids, Mapping) else None)
        if midpoint is None:
            raise MarketConfigurationError(f"Hyperliquid outcome {outcome_id}:{side} has no available price.")
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=self._contract_id(outcome_id, side),
            last=midpoint,
            bid=bid,
            ask=ask,
            midpoint=midpoint,
            source="hyperliquid_hip4_l2book",
            raw={"orderbook": book.raw, "outcome": outcome_id, "side": side},
        )

    def get_orderbook(self, contract_id: str) -> OrderBookSnapshot:
        self.ensure_capability("orderbook_reading")
        outcome_id, side = self._split_contract_id(contract_id)
        payload = self._info({"type": "l2Book", "coin": self._coin(outcome_id, side)})
        levels = payload.get("levels") if isinstance(payload, Mapping) else None
        if not isinstance(levels, list) or len(levels) < 2:
            raise MarketConfigurationError("Hyperliquid l2Book returned an invalid levels payload.")
        bids = self._levels(levels[0])
        asks = self._levels(levels[1])
        return OrderBookSnapshot(
            market_id=self.market_id,
            contract_id=self._contract_id(outcome_id, side),
            bids=bids,
            asks=asks,
            raw=dict(payload) if isinstance(payload, Mapping) else {"payload": payload},
        )

    def list_candles(
        self,
        contract_id: str,
        *,
        resolution: str = "1h",
        from_timestamp: Optional[float] = None,
        to_timestamp: Optional[float] = None,
    ) -> List[MarketCandle]:
        """Return normalized HIP-4 OHLCV candles from Hyperliquid's public feed.

        Hyperliquid documents ``candleSnapshot`` as a public ``info`` request,
        with epoch-millisecond bounds and a maximum of the most recent 5,000
        candles.  The synthetic ``#<encoding>`` coin maps directly from the
        canonical outcome contract id used by this adapter.
        """

        outcome_id, side = self._split_contract_id(contract_id)
        clean_resolution = str(resolution or "").strip()
        if clean_resolution not in HYPERLIQUID_CANDLE_RESOLUTIONS:
            allowed = ", ".join(HYPERLIQUID_CANDLE_RESOLUTIONS)
            raise MarketConfigurationError(f"Hyperliquid candle resolution must be one of: {allowed}.")

        end_ms = self._timestamp_millis(to_timestamp, "to_timestamp") if to_timestamp is not None else int(time.time() * 1000)
        default_lookback = self._candle_lookback_millis(clean_resolution)
        start_ms = (
            self._timestamp_millis(from_timestamp, "from_timestamp")
            if from_timestamp is not None
            else max(0, end_ms - default_lookback)
        )
        if end_ms <= start_ms:
            raise MarketConfigurationError("Hyperliquid candle history requires to_timestamp greater than from_timestamp.")

        payload = self._info(
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": self._coin(outcome_id, side),
                    "interval": clean_resolution,
                    "startTime": start_ms,
                    "endTime": end_ms,
                },
            }
        )
        if not isinstance(payload, list):
            return []

        canonical = self._contract_id(outcome_id, side)
        candles: List[MarketCandle] = []
        for raw in payload:
            if not isinstance(raw, Mapping):
                continue
            timestamp = self._timestamp_seconds(raw.get("t"))
            values = tuple(self._candle_probability(raw.get(key)) for key in ("o", "h", "l", "c"))
            volume = self._nonnegative_number(raw.get("v"))
            if timestamp <= 0 or any(value is None for value in values):
                continue
            candles.append(
                MarketCandle(
                    market_id=self.market_id,
                    contract_id=canonical,
                    timestamp=float(timestamp),
                    open=float(values[0]),
                    high=float(values[1]),
                    low=float(values[2]),
                    close=float(values[3]),
                    volume=volume,
                    raw=dict(raw),
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
        """Return normalized HIP-4 fills for a configured wallet.

        Hyperliquid's documented ``userFills`` and ``userFillsByTime`` info
        requests are account-scoped.  The feed includes perpetual and spot
        activity as well, so only the requested synthetic HIP-4 coin is
        admitted into the shared trade-history model.  Wallet identity is
        explicit configuration rather than an HTTP-controlled path value.
        """

        self.ensure_capability("trade_history")
        outcome_id, side_index = self._split_contract_id(contract_id)
        canonical_contract = self._contract_id(outcome_id, side_index)
        wallet_credential = self.resolve_credential(
            "hyperliquid_trade_wallet",
            ("HYPERLIQUID_TRADE_WALLET", "HYPERLIQUID_ACTIVITY_WALLET"),
            required=True,
            label="HYPERLIQUID_TRADE_WALLET",
        )
        wallet = require_activity_identity(self.market_id, wallet_credential.value)
        desired = self._trade_limit(limit)
        before_ts = self._history_timestamp(before, "before") if before is not None else None
        after_ts = self._history_timestamp(after, "after") if after is not None else None
        if before_ts is not None and after_ts is not None and before_ts < after_ts:
            raise MarketConfigurationError("Hyperliquid trade history requires before to be at or after after.")

        if before_ts is None and after_ts is None:
            request: Dict[str, Any] = {
                "type": "userFills",
                "user": wallet,
                "aggregateByTime": True,
            }
        else:
            request = {
                "type": "userFillsByTime",
                "user": wallet,
                "startTime": int(after_ts * 1000) if after_ts is not None else 0,
                "aggregateByTime": True,
            }
            if before_ts is not None:
                request["endTime"] = int(before_ts * 1000)

        payload = self._info(request)
        if not isinstance(payload, list):
            raise MarketConfigurationError("Hyperliquid user fills returned an invalid payload.")

        trades: List[MarketTrade] = []
        for fill in payload:
            if not isinstance(fill, Mapping):
                continue
            try:
                activity = self._activity_from_fill(wallet, fill)
            except MarketConfigurationError:
                continue
            if activity.get("contract_id") != canonical_contract:
                continue
            timestamp = float(activity.get("timestamp") or 0)
            price = activity.get("price")
            if timestamp <= 0 or price is None:
                continue
            if after_ts is not None and timestamp < after_ts:
                continue
            if before_ts is not None and timestamp > before_ts:
                continue
            trades.append(
                MarketTrade(
                    market_id=self.market_id,
                    contract_id=canonical_contract,
                    trade_id=str(activity["activity_id"]),
                    side=str(activity["side"]),
                    price=float(price),
                    size=float(activity["size"]),
                    timestamp=timestamp,
                    raw=dict(fill),
                )
            )
            if len(trades) >= desired:
                break
        return trades

    def list_active_orders(self, *, dex: str = "") -> Any:
        """Read the configured wallet's documented open-order feed.

        Hyperliquid's ``openOrders`` request is account-scoped and returns
        both perpetual and spot orders.  The response is deliberately kept
        lossless; callers that need HIP-4-only rows can filter by the
        synthetic ``#<encoding>`` coin using the same contract mapping as
        normalized trade history.
        """

        wallet = self._account_wallet()
        request: Dict[str, Any] = {"type": "openOrders", "user": wallet}
        normalized_dex = self._account_dex(dex)
        if normalized_dex:
            request["dex"] = normalized_dex
        payload = self._info(request)
        if not isinstance(payload, list):
            raise MarketConfigurationError("Hyperliquid openOrders returned an invalid payload.")
        return payload

    def list_order_history(self, *, limit: int = 2000) -> Any:
        """Read the documented historical-order feed for the configured wallet."""

        wallet = self._account_wallet()
        desired = self._account_limit(limit)
        payload = self._info({"type": "historicalOrders", "user": wallet})
        if not isinstance(payload, list):
            raise MarketConfigurationError("Hyperliquid historicalOrders returned an invalid payload.")
        return payload[:desired]

    def get_positions(self, *, dex: str = "") -> Any:
        """Read the documented perpetual clearinghouse state."""

        wallet = self._account_wallet()
        request: Dict[str, Any] = {"type": "clearinghouseState", "user": wallet}
        normalized_dex = self._account_dex(dex)
        if normalized_dex:
            request["dex"] = normalized_dex
        payload = self._info(request)
        if not isinstance(payload, Mapping):
            raise MarketConfigurationError("Hyperliquid clearinghouseState returned an invalid payload.")
        return dict(payload)

    def get_spot_balances(self) -> Any:
        """Read the documented spot clearinghouse balances for the wallet."""

        wallet = self._account_wallet()
        payload = self._info({"type": "spotClearinghouseState", "user": wallet})
        if not isinstance(payload, Mapping):
            raise MarketConfigurationError("Hyperliquid spotClearinghouseState returned an invalid payload.")
        return dict(payload)

    def get_portfolio(self) -> Any:
        """Read the documented account portfolio performance payload."""

        wallet = self._account_wallet()
        payload = self._info({"type": "portfolio", "user": wallet})
        if not isinstance(payload, list):
            raise MarketConfigurationError("Hyperliquid portfolio returned an invalid payload.")
        return payload

    def list_subaccounts(self) -> Any:
        """Read the documented sub-account list for the configured master wallet."""

        wallet = self._account_wallet()
        payload = self._info({"type": "subAccounts", "user": wallet})
        if not isinstance(payload, list):
            raise MarketConfigurationError("Hyperliquid subAccounts returned an invalid payload.")
        return payload

    def account_recovery(self, operation: str, **kwargs: Any) -> Any:
        """Dispatch one validated, documented Hyperliquid account read."""

        normalized = str(operation or "").strip().lower()
        if normalized == "active_orders":
            return self.list_active_orders(dex=kwargs.get("dex", ""))
        if normalized == "order_history":
            return self.list_order_history(limit=kwargs.get("limit", 2000))
        if normalized == "positions":
            return self.get_positions(dex=kwargs.get("dex", ""))
        if normalized == "spot_balances":
            return self.get_spot_balances()
        if normalized == "portfolio":
            return self.get_portfolio()
        if normalized == "subaccounts":
            return self.list_subaccounts()
        supported = ", ".join(self.account_recovery_operations)
        raise MarketConfigurationError(f"Hyperliquid account recovery operation must be one of: {supported}.")

    def manage_orders(self, operation: str, **kwargs: Any) -> Dict[str, Any]:
        """Forward a reviewed, externally signed Hyperliquid order action.

        Hyperliquid exposes cancellation and modification through the fixed
        ``POST /exchange`` route.  The app never signs these actions and never
        accepts an arbitrary exchange action: callers must provide the complete
        signed envelope and the operation-specific action type is validated
        locally before the request is sent.
        """

        normalized = str(operation or "").strip().lower()
        if normalized not in self.order_management_operations:
            supported = ", ".join(self.order_management_operations)
            raise MarketConfigurationError(
                f"Hyperliquid order-management operation must be one of: {supported}."
            )
        self.ensure_capability("live_trading")
        if not self.config_bool("hyperliquid_order_management_enabled", False):
            raise MarketConfigurationError(
                "Hyperliquid order management is disabled by adapter config. "
                "Set hyperliquid_order_management_enabled=true only after reviewing signed-action risk controls."
            )
        self.ensure_live_trading_enabled("Hyperliquid order management")
        if str(kwargs.get("confirm_order_management") or "").strip() != HYPERLIQUID_ORDER_MANAGEMENT_CONFIRMATION:
            raise MarketConfigurationError(
                "Hyperliquid order management requires exact confirmation text "
                f"{HYPERLIQUID_ORDER_MANAGEMENT_CONFIRMATION}."
            )
        if normalized == "schedule_cancel" and str(kwargs.get("confirm_global_cancel") or "").strip() != HYPERLIQUID_SCHEDULE_CANCEL_CONFIRMATION:
            raise MarketConfigurationError(
                "Hyperliquid schedule_cancel requires exact confirmation text "
                f"{HYPERLIQUID_SCHEDULE_CANCEL_CONFIRMATION}."
            )

        signed_action = kwargs.get("signed_action")
        if signed_action is None and isinstance(kwargs.get("instructions"), Mapping):
            signed_action = kwargs.get("instructions")
        envelope = self._validate_management_envelope(signed_action, normalized)
        response = self.runtime.request_json(
            "POST",
            f"{self.api_base_url}/exchange",
            json_body=envelope,
            headers={"Content-Type": "application/json"},
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
                "requires_external_signature": True,
                "references": list(HYPERLIQUID_REFERENCES),
            },
            "request": envelope,
            "response": response,
        }

    def list_activity(self, wallet_address: str, *, limit: int = 25) -> List[Dict[str, Any]]:
        """Return normalized public HIP-4 fills for a wallet.

        Hyperliquid's documented ``userFills`` response contains both perpetual
        and spot fills. HIP-4 outcome assets are the synthetic ``#<encoding>``
        coins, where the encoding is ``10 * outcome_id + side``. Only those
        rows are exposed to the copy workflow so ordinary perp/spot activity
        cannot be misinterpreted as a prediction-market order.
        """

        self.ensure_capability("copy_trading")
        wallet = require_activity_identity(self.market_id, wallet_address)
        desired = max(1, min(int(limit or 25), 100))
        payload = self._info({"type": "userFills", "user": wallet, "aggregateByTime": True})
        if not isinstance(payload, list):
            raise MarketConfigurationError("Hyperliquid userFills returned an invalid payload.")

        activities: List[Dict[str, Any]] = []
        for fill in payload:
            if not isinstance(fill, Mapping):
                continue
            try:
                activity = self._activity_from_fill(wallet, fill)
            except MarketConfigurationError:
                continue
            activities.append(activity)
            if len(activities) >= desired:
                break
        return activities

    def copy_trade_from_activity(self, activity: Mapping[str, Any]) -> PaperOrderResult:
        """Build a simulation-first paper order from a normalized HIP-4 fill."""

        self.ensure_capability("copy_trading")
        contract_id = str(activity.get("asset") or activity.get("contract_id") or "").strip()
        if not contract_id:
            raise MarketConfigurationError("Hyperliquid activity has no contract id.")
        outcome_id, side_index = self._split_contract_id(contract_id)
        canonical_contract = self._contract_id(outcome_id, side_index)
        order_side = str(activity.get("side") or "").strip().upper()
        if order_side not in self.live_order_sides:
            raise MarketConfigurationError("Hyperliquid activity side must be BUY or SELL.")
        size = self._required_positive_number(activity.get("size"), "Hyperliquid activity size")
        raw_price = activity.get("price")
        limit_price = None if raw_price in (None, "") else self._probability(raw_price)
        if raw_price not in (None, "") and limit_price is None:
            raise MarketConfigurationError("Hyperliquid activity price must be between 0 and 1.")
        return self.place_paper_order(
            PaperOrderRequest(
                market_id=self.market_id,
                contract_id=canonical_contract,
                side=order_side,
                size=size,
                limit_price=limit_price,
                metadata={"activity": dict(activity), "source": "hyperliquid_user_fills"},
            )
        )

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        outcome_id, side = self._validate_order(order)
        price = self._probability(order.limit_price)
        if price is None:
            price = self.get_price(self._contract_id(outcome_id, side)).last
        action = self._order_action(outcome_id, side, order.side, order.size, price)
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=self._contract_id(outcome_id, side),
            accepted=True,
            message=(
                f"DRY RUN: would place Hyperliquid {str(order.side).upper()} for {float(order.size):.6f} outcome units"
                + (f" at {float(price):.6f}" if price is not None else "")
            ),
            filled_size=0.0,
            average_price=price,
            raw={"dry_run": True, "action": action},
        )

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        outcome_id, side = self._validate_order(order)
        audit = self.preflight_live_order(order, feature_name="Hyperliquid signed order submission")
        signed = order.metadata.get("signed_action")
        if not isinstance(signed, Mapping):
            raise MarketConfigurationError(
                "Hyperliquid live orders require metadata.signed_action containing the complete externally signed exchange payload."
            )
        payload = dict(signed)
        self._validate_signed_payload(payload, outcome_id, side, order)
        response = self.runtime.request_json(
            "POST",
            f"{self.api_base_url}/exchange",
            json_body=payload,
            headers={"Content-Type": "application/json"},
        )
        return {
            "live": True,
            "market_id": self.market_id,
            "contract_id": self._contract_id(outcome_id, side),
            "audit": audit,
            "response": response,
        }

    def _info(self, body: Mapping[str, Any]) -> Any:
        return self.runtime.request_json(
            "POST",
            f"{self.api_base_url}/info",
            json_body=dict(body),
            headers={"Content-Type": "application/json"},
        )

    def _load_outcomes(self) -> Dict[str, Dict[str, Any]]:
        payload = self._info({"type": "outcomeMeta"})
        rows = payload.get("outcomes") if isinstance(payload, Mapping) else None
        if not isinstance(rows, list):
            raise MarketConfigurationError("Hyperliquid outcomeMeta returned no outcomes array.")
        self._outcome_cache = {}
        self._question_cache = {}
        for row in rows:
            if isinstance(row, Mapping):
                self._outcome_cache[self._outcome_id(row.get("outcome"))] = dict(row)
        questions = payload.get("questions") if isinstance(payload, Mapping) else None
        if isinstance(questions, list):
            for row in questions:
                if isinstance(row, Mapping) and row.get("question") is not None:
                    self._question_cache[self._outcome_id(row.get("question"))] = dict(row)
        return self._outcome_cache

    def _event_from_row(self, event_id: str, row: Mapping[str, Any]) -> MarketEvent:
        item_id = event_id.split(":", 1)[1]
        return MarketEvent(
            market_id=self.market_id,
            event_id=event_id,
            title=self._title(row, item_id),
            url=f"{self.api_base_url}/trade",
            status="open",
            raw=dict(row),
        )

    def _contract_from_side(self, outcome_id: str, side: int, spec: Any, row: Mapping[str, Any]) -> MarketContract:
        name = str(spec.get("name") if isinstance(spec, Mapping) else spec or ("Yes" if side == 0 else "No"))
        return MarketContract(
            market_id=self.market_id,
            contract_id=self._contract_id(outcome_id, side),
            event_id=f"outcome:{outcome_id}",
            title=f"{self._title(row, outcome_id)} - {name}",
            outcome=name,
            url=f"{self.api_base_url}/trade",
            status="open",
            raw={"outcome": dict(row), "side": side, "coin": self._coin(outcome_id, side)},
        )

    def _validate_order(self, order: PaperOrderRequest) -> Tuple[str, int]:
        self.ensure_order_market(order)
        outcome_id, side = self._split_contract_id(order.contract_id)
        if str(order.side or "").upper() not in self.live_order_sides:
            raise MarketConfigurationError("Hyperliquid order side must be BUY or SELL.")
        try:
            size = float(order.size)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Hyperliquid order size must be numeric.") from exc
        if not math.isfinite(size) or size <= 0:
            raise MarketConfigurationError("Hyperliquid order size must be positive and finite.")
        if order.limit_price is not None and self._probability(order.limit_price) is None:
            raise MarketConfigurationError("Hyperliquid order limit price must be between 0 and 1.")
        return outcome_id, side

    def _activity_from_fill(self, wallet: str, fill: Mapping[str, Any]) -> Dict[str, Any]:
        coin = str(fill.get("coin") or "").strip()
        match = re.fullmatch(r"#([0-9]+)", coin)
        if not match:
            raise MarketConfigurationError("Hyperliquid fill is not a HIP-4 outcome asset.")
        encoding = int(match.group(1))
        outcome_id, side_index = divmod(encoding, 10)
        if side_index not in {0, 1}:
            raise MarketConfigurationError("Hyperliquid HIP-4 fill has an unknown outcome side.")

        raw_side = str(fill.get("side") or "").strip().upper()
        if raw_side in {"B", "BUY"}:
            order_side = "BUY"
        elif raw_side in {"A", "S", "SELL"}:
            order_side = "SELL"
        else:
            raise MarketConfigurationError("Hyperliquid fill has an unknown trade side.")

        size = self._required_positive_number(fill.get("sz"), "Hyperliquid fill size")
        price = self._probability(fill.get("px"))
        fill_id = str(fill.get("hash") or fill.get("tid") or fill.get("oid") or "").strip()
        if not fill_id:
            raise MarketConfigurationError("Hyperliquid fill omitted a stable identifier.")
        timestamp = self._timestamp_seconds(fill.get("time") or fill.get("timestamp"))
        contract_id = self._contract_id(str(outcome_id), side_index)
        outcome = "YES" if side_index == 0 else "NO"
        return {
            "type": "TRADE",
            "proxyWallet": wallet,
            "wallet": wallet,
            "asset": contract_id,
            "contract_id": contract_id,
            "marketId": str(outcome_id),
            "side": order_side,
            "size": size,
            "shares": size,
            "price": price,
            "timestamp": timestamp,
            "transactionHash": f"hyperliquid-fill:{fill_id}",
            "activity_id": fill_id,
            "slug": f"outcome:{outcome_id}",
            "outcome": outcome,
            "raw": dict(fill),
        }

    def _account_wallet(self) -> str:
        credential = self.resolve_credential(
            "hyperliquid_account_wallet",
            ("HYPERLIQUID_ACCOUNT_WALLET", "HYPERLIQUID_TRADE_WALLET", "HYPERLIQUID_ACTIVITY_WALLET"),
            required=True,
            label="HYPERLIQUID_ACCOUNT_WALLET",
        )
        return require_activity_identity(self.market_id, credential.value)

    @staticmethod
    def _account_dex(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if len(text) > 64 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}", text):
            raise MarketConfigurationError("Hyperliquid account dex must be a short alphanumeric name.")
        return text

    @staticmethod
    def _account_limit(value: Any) -> int:
        try:
            limit = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Hyperliquid account limit must be an integer between 1 and 2000.") from exc
        if limit < 1 or limit > 2000:
            raise MarketConfigurationError("Hyperliquid account limit must be between 1 and 2000.")
        return limit

    def _validate_signed_payload(
        self, payload: Mapping[str, Any], outcome_id: str, side: int, order: PaperOrderRequest
    ) -> None:
        action = payload.get("action")
        if not isinstance(action, Mapping) or action.get("type") != "order":
            raise MarketConfigurationError("Hyperliquid signed_action.action.type must be 'order'.")
        orders = action.get("orders")
        if not isinstance(orders, list) or not orders or not isinstance(orders[0], Mapping):
            raise MarketConfigurationError("Hyperliquid signed_action.action.orders must contain an order.")
        wire = orders[0]
        expected_asset = self._asset_id(outcome_id, side)
        if int(wire.get("a", -1)) != expected_asset:
            raise MarketConfigurationError("Hyperliquid signed order asset does not match the selected outcome side.")
        expected_buy = str(order.side).upper() == "BUY"
        if bool(wire.get("b")) != expected_buy:
            raise MarketConfigurationError("Hyperliquid signed order side does not match the selected order side.")
        try:
            signed_size = float(wire.get("s"))
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Hyperliquid signed order size must be numeric.") from exc
        if not math.isclose(signed_size, float(order.size), rel_tol=0.0, abs_tol=1e-9):
            raise MarketConfigurationError("Hyperliquid signed order size does not match the requested size.")
        self._validate_signature(payload.get("signature"))
        if payload.get("nonce") in (None, ""):
            raise MarketConfigurationError("Hyperliquid signed_action must include a signature and nonce.")

    def _validate_management_envelope(self, payload: Any, operation: str) -> Dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise MarketConfigurationError(
                "Hyperliquid order management requires signed_action with the complete external signature envelope."
            )
        envelope = dict(payload)
        action = envelope.get("action")
        if not isinstance(action, Mapping):
            raise MarketConfigurationError("Hyperliquid signed_action.action must be an object.")
        self._validate_signature(envelope.get("signature"))
        self._positive_integer(envelope.get("nonce"), "Hyperliquid signed-action nonce")
        if envelope.get("expiresAfter") not in (None, ""):
            self._positive_integer(
                envelope.get("expiresAfter"), "Hyperliquid signed-action expiresAfter"
            )
        if envelope.get("vaultAddress") not in (None, ""):
            self._wallet_address(envelope.get("vaultAddress"), "vaultAddress")

        action_type = str(action.get("type") or "").strip()
        expected = {
            "cancel_order": "cancel",
            "cancel_orders": "cancel",
            "cancel_by_cloid": "cancelByCloid",
            "modify_order": "modify",
            "batch_modify_orders": "batchModify",
            "schedule_cancel": "scheduleCancel",
        }[operation]
        if action_type != expected:
            raise MarketConfigurationError(
                f"Hyperliquid {operation} requires signed_action.action.type={expected!r}."
            )

        if action_type == "cancel":
            cancels = self._validate_cancel_entries(action.get("cancels"), by_cloid=False)
            if operation == "cancel_order" and len(cancels) != 1:
                raise MarketConfigurationError("Hyperliquid cancel_order requires exactly one cancel entry.")
            if operation == "cancel_orders" and not cancels:
                raise MarketConfigurationError("Hyperliquid cancel_orders requires at least one cancel entry.")
        elif action_type == "cancelByCloid":
            cancels = self._validate_cancel_entries(action.get("cancels"), by_cloid=True)
            if not cancels:
                raise MarketConfigurationError("Hyperliquid cancel_by_cloid requires at least one cancel entry.")
        elif action_type == "modify":
            if not isinstance(action.get("order"), Mapping):
                raise MarketConfigurationError("Hyperliquid modify_order requires a signed order object.")
            self._order_reference(action.get("oid"), "modify oid")
            self._validate_modify_order(action.get("order"))
        elif action_type == "batchModify":
            modifies = action.get("modifies")
            if not isinstance(modifies, list) or not modifies:
                raise MarketConfigurationError("Hyperliquid batch_modify_orders requires a non-empty modifies array.")
            if len(modifies) > HYPERLIQUID_ORDER_MANAGEMENT_MAX_BATCH:
                raise MarketConfigurationError(
                    f"Hyperliquid batch_modify_orders is capped at {HYPERLIQUID_ORDER_MANAGEMENT_MAX_BATCH} orders."
                )
            for entry in modifies:
                self._validate_modify_entry(entry)
        else:
            schedule_time = action.get("time")
            if schedule_time not in (None, ""):
                schedule_time = self._positive_integer(schedule_time, "Hyperliquid schedule cancel time")
                if schedule_time < int(time.time() * 1000) + 5_000:
                    raise MarketConfigurationError(
                        "Hyperliquid schedule cancel time must be at least five seconds in the future."
                    )
        return envelope

    @staticmethod
    def _validate_signature(value: Any) -> None:
        if not isinstance(value, Mapping):
            raise MarketConfigurationError(
                "Hyperliquid signed_action.signature must be an object containing r, s, and v."
            )
        for component in ("r", "s"):
            raw = value.get(component)
            if not isinstance(raw, str) or not re.fullmatch(r"0x[0-9a-fA-F]+", raw):
                raise MarketConfigurationError(
                    f"Hyperliquid signed_action.signature.{component} must be a hexadecimal string."
                )
        recovery = value.get("v")
        if isinstance(recovery, bool) or not isinstance(recovery, int) or recovery < 0 or recovery > 255:
            raise MarketConfigurationError("Hyperliquid signed_action.signature.v must be an integer byte.")

    def _validate_cancel_entries(self, value: Any, *, by_cloid: bool) -> List[Dict[str, Any]]:
        if not isinstance(value, list) or not value:
            raise MarketConfigurationError("Hyperliquid cancellation requires a non-empty cancels array.")
        if len(value) > HYPERLIQUID_ORDER_MANAGEMENT_MAX_BATCH:
            raise MarketConfigurationError(
                f"Hyperliquid cancellation is capped at {HYPERLIQUID_ORDER_MANAGEMENT_MAX_BATCH} orders."
            )
        entries: List[Dict[str, Any]] = []
        seen = set()
        for raw in value:
            if not isinstance(raw, Mapping):
                raise MarketConfigurationError("Hyperliquid cancellation entries must be objects.")
            asset = self._hip4_asset(raw.get("asset", raw.get("a")))
            if by_cloid:
                reference = self._cloid(raw.get("cloid"), "cancel cloid")
                key = (asset, reference)
                entry = {"asset": asset, "cloid": reference}
            else:
                reference = self._positive_integer(raw.get("oid", raw.get("o")), "cancel oid")
                key = (asset, reference)
                entry = {"a": asset, "o": reference}
            if key in seen:
                raise MarketConfigurationError("Hyperliquid cancellation entries must be unique.")
            seen.add(key)
            entries.append(entry)
        return entries

    def _validate_modify_entry(self, value: Any) -> Dict[str, Any]:
        if not isinstance(value, Mapping):
            raise MarketConfigurationError("Hyperliquid modify entries must be objects.")
        oid = self._order_reference(value.get("oid"), "modify oid")
        order = self._validate_modify_order(value.get("order"))
        return {"oid": oid, "order": order}

    def _validate_modify_order(self, value: Any) -> Dict[str, Any]:
        if not isinstance(value, Mapping):
            raise MarketConfigurationError("Hyperliquid modify order must be an object.")
        asset = self._hip4_asset(value.get("a"))
        if not isinstance(value.get("b"), bool) or not isinstance(value.get("r"), bool):
            raise MarketConfigurationError("Hyperliquid modify order b and r fields must be booleans.")
        price = self._probability(value.get("p"))
        if price is None:
            raise MarketConfigurationError("Hyperliquid modify order price must be between 0 and 1.")
        size = self._required_positive_number(value.get("s"), "Hyperliquid modify order size")
        order_type = value.get("t")
        if not isinstance(order_type, Mapping):
            raise MarketConfigurationError("Hyperliquid modify order type must be a limit or trigger object.")
        normalized_type: Dict[str, Any]
        if isinstance(order_type.get("limit"), Mapping):
            tif = str(order_type["limit"].get("tif") or "").strip()
            if tif not in {"Alo", "Ioc", "Gtc"}:
                raise MarketConfigurationError("Hyperliquid modify limit tif must be Alo, Ioc, or Gtc.")
            normalized_type = {"limit": {"tif": tif}}
        elif isinstance(order_type.get("trigger"), Mapping):
            trigger = order_type["trigger"]
            if not isinstance(trigger.get("isMarket"), bool):
                raise MarketConfigurationError("Hyperliquid trigger isMarket must be boolean.")
            trigger_price = self._probability(trigger.get("triggerPx"))
            if trigger_price is None:
                raise MarketConfigurationError("Hyperliquid trigger price must be between 0 and 1.")
            tpsl = str(trigger.get("tpsl") or "").strip().lower()
            if tpsl not in {"tp", "sl"}:
                raise MarketConfigurationError("Hyperliquid trigger tpsl must be tp or sl.")
            normalized_type = {
                "trigger": {"isMarket": bool(trigger["isMarket"]), "triggerPx": str(trigger_price), "tpsl": tpsl}
            }
        else:
            raise MarketConfigurationError("Hyperliquid modify order type must contain limit or trigger.")
        normalized: Dict[str, Any] = {
            "a": asset,
            "b": bool(value["b"]),
            "p": str(price),
            "s": str(size),
            "r": bool(value["r"]),
            "t": normalized_type,
        }
        if value.get("c") not in (None, ""):
            normalized["c"] = self._cloid(value.get("c"), "modify cloid")
        return normalized

    @staticmethod
    def _positive_integer(value: Any, label: str) -> int:
        if isinstance(value, bool):
            raise MarketConfigurationError(f"{label} must be a positive integer.")
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"{label} must be a positive integer.") from exc
        if number <= 0 or number > 2**63 - 1 or str(value).strip() != str(number):
            raise MarketConfigurationError(f"{label} must be a positive integer.")
        return number

    @classmethod
    def _order_reference(cls, value: Any, label: str) -> Any:
        text = str(value or "").strip()
        if HYPERLIQUID_CLOID_RE.fullmatch(text):
            return text
        return cls._positive_integer(text, label)

    @classmethod
    def _cloid(cls, value: Any, label: str) -> str:
        text = str(value or "").strip()
        if not HYPERLIQUID_CLOID_RE.fullmatch(text):
            raise MarketConfigurationError(f"{label} must be a 128-bit hexadecimal cloid with a 0x prefix.")
        return text

    @classmethod
    def _hip4_asset(cls, value: Any) -> int:
        asset = cls._positive_integer(value, "Hyperliquid HIP-4 asset")
        encoded = asset - HYPERLIQUID_ASSET_BASE
        if encoded < 0 or encoded % 10 not in {0, 1}:
            raise MarketConfigurationError("Hyperliquid order asset must be a HIP-4 synthetic outcome asset.")
        return asset

    @staticmethod
    def _wallet_address(value: Any, label: str) -> str:
        text = str(value or "").strip()
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", text):
            raise MarketConfigurationError(f"Hyperliquid {label} must be a 20-byte hexadecimal address.")
        return text

    @classmethod
    def _order_action(cls, outcome_id: str, side: int, order_side: Any, size: Any, price: Optional[float]) -> Dict[str, Any]:
        if price is None:
            raise MarketConfigurationError("Hyperliquid paper orders require a price when no quote is available.")
        return {
            "type": "order",
            "orders": [
                {
                    "a": cls._asset_id(outcome_id, side),
                    "b": str(order_side).upper() == "BUY",
                    "p": f"{float(price):.8f}",
                    "s": f"{float(size):.8f}",
                    "r": False,
                    "t": {"limit": {"tif": "Gtc"}},
                }
            ],
            "grouping": "na",
        }

    @staticmethod
    def _levels(rows: Any) -> List[OrderBookLevel]:
        if not isinstance(rows, list):
            return []
        levels: List[OrderBookLevel] = []
        for row in rows[:20]:
            if not isinstance(row, Mapping):
                continue
            try:
                price = float(row.get("px"))
                size = float(row.get("sz"))
            except (TypeError, ValueError):
                continue
            if math.isfinite(price) and price > 0 and math.isfinite(size) and size > 0:
                levels.append(OrderBookLevel(price=price, size=size))
        return levels

    @staticmethod
    def _title(row: Mapping[str, Any], item_id: str) -> str:
        name = str(row.get("name") or row.get("marketName") or item_id).strip()
        description = str(row.get("description") or "").strip()
        specs = HyperliquidAdapter._description_specs(description)
        details = " / ".join(str(specs[key]) for key in ("underlying", "targetPrice", "expiry") if key in specs)
        return f"{name} ({details})" if details else name

    @staticmethod
    def _description_specs(value: str) -> Dict[str, str]:
        result: Dict[str, str] = {}
        for part in str(value or "").split("|"):
            key, separator, raw = part.partition(":")
            if separator and key and raw:
                result[key.strip()] = raw.strip()
        return result

    @staticmethod
    def _search_text(row: Mapping[str, Any]) -> str:
        return " ".join(str(row.get(key) or "") for key in ("name", "description", "marketName", "underlying")).lower()

    @staticmethod
    def _outcome_id(value: Any) -> str:
        text = str(value or "").strip()
        if not OUTCOME_ID_RE.fullmatch(text):
            raise MarketConfigurationError("Hyperliquid outcome IDs must be non-negative decimal integers.")
        return text

    @classmethod
    def _split_event_id(cls, event_id: Any) -> Tuple[str, str]:
        text = str(event_id or "").strip()
        parts = text.split(":", 1)
        if len(parts) != 2 or parts[0] not in {"outcome", "question"}:
            raise MarketConfigurationError("Hyperliquid event id must be 'outcome:<id>' or 'question:<id>'.")
        return parts[0], cls._outcome_id(parts[1])

    @classmethod
    def _split_contract_id(cls, contract_id: Any) -> Tuple[str, int]:
        text = str(contract_id or "").strip()
        parts = text.split(":")
        if len(parts) != 3 or parts[0] != "outcome" or not parts[2].isdigit() or int(parts[2]) not in {0, 1}:
            raise MarketConfigurationError("Hyperliquid contract id must be 'outcome:<id>:0' or ':1'.")
        return cls._outcome_id(parts[1]), int(parts[2])

    @staticmethod
    def _encoding(outcome_id: str, side: int) -> int:
        return 10 * int(outcome_id) + int(side)

    @classmethod
    def _coin(cls, outcome_id: str, side: int) -> str:
        return f"#{cls._encoding(outcome_id, side)}"

    @classmethod
    def _asset_id(cls, outcome_id: str, side: int) -> int:
        return 100_000_000 + cls._encoding(outcome_id, side)

    @staticmethod
    def _contract_id(outcome_id: str, side: int) -> str:
        return f"outcome:{outcome_id}:{int(side)}"

    @staticmethod
    def _probability(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and 0 < number < 1 else None

    @staticmethod
    def _required_positive_number(value: Any, label: str) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"{label} must be numeric.") from exc
        if not math.isfinite(number) or number <= 0:
            raise MarketConfigurationError(f"{label} must be positive and finite.")
        return number

    @staticmethod
    def _timestamp_seconds(value: Any) -> int:
        try:
            timestamp = int(float(value or 0))
        except (TypeError, ValueError):
            return 0
        return timestamp // 1000 if timestamp > 10_000_000_000 else timestamp

    @staticmethod
    def _history_timestamp(value: Any, label: str) -> float:
        try:
            timestamp = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"Hyperliquid trade history {label} must be a finite Unix timestamp.") from exc
        if not math.isfinite(timestamp) or timestamp < 0:
            raise MarketConfigurationError(f"Hyperliquid trade history {label} must be a non-negative Unix timestamp.")
        return timestamp / 1000.0 if timestamp > 10_000_000_000 else timestamp

    @staticmethod
    def _trade_limit(value: Any) -> int:
        try:
            limit = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Hyperliquid trade history limit must be an integer between 1 and 1000.") from exc
        if limit < 1 or limit > 1000:
            raise MarketConfigurationError("Hyperliquid trade history limit must be between 1 and 1000.")
        return limit

    @staticmethod
    def _timestamp_millis(value: Any, label: str) -> int:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"Hyperliquid {label} must be a finite Unix timestamp.") from exc
        if not math.isfinite(number) or number < 0:
            raise MarketConfigurationError(f"Hyperliquid {label} must be a non-negative Unix timestamp.")
        return int(number if number > 10_000_000_000 else number * 1000)

    @staticmethod
    def _candle_lookback_millis(resolution: str) -> int:
        unit = resolution[-1]
        try:
            count = int(resolution[:-1])
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"Hyperliquid candle resolution is invalid: {resolution!r}.") from exc
        if unit == "m":
            minutes = count
        elif unit == "h":
            minutes = count * 60
        elif unit == "d":
            minutes = count * 24 * 60
        elif unit == "w":
            minutes = count * 7 * 24 * 60
        elif unit == "M":
            minutes = count * 30 * 24 * 60
        else:
            raise MarketConfigurationError(f"Hyperliquid candle resolution is invalid: {resolution!r}.")
        return max(minutes, 1) * 60_000 * 100

    @staticmethod
    def _candle_probability(value: Any) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and 0.0 <= number <= 1.0 else None

    @staticmethod
    def _nonnegative_number(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and number >= 0 else None
