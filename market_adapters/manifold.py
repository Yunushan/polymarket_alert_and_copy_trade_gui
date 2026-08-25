from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .base import MarketAdapter
from .catalog import get_market_metadata
from .errors import MarketConfigurationError, MarketHTTPError, UnsupportedFeatureError
from .identity import require_activity_identity
from .types import (
    MarketCandle,
    MarketContract,
    MarketEvent,
    MarketTrade,
    PaperOrderRequest,
    PaperOrderResult,
    PriceSnapshot,
)


DEFAULT_MANIFOLD_BASE_URL = "https://api.manifold.markets/v0"
MANIFOLD_ACCOUNT_RECOVERY_OPERATIONS = ("account", "active_orders", "order_history")
MANIFOLD_ORDER_MANAGEMENT_OPERATIONS = ("cancel_order",)
MANIFOLD_ORDER_MANAGEMENT_CONFIRMATION = "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
MANIFOLD_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")


class ManifoldAdapter(MarketAdapter):
    """Manifold adapter using the documented public REST API."""

    metadata = get_market_metadata("manifold")
    account_recovery_operations = MANIFOLD_ACCOUNT_RECOVERY_OPERATIONS
    order_management_operations = MANIFOLD_ORDER_MANAGEMENT_OPERATIONS

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        credential = self.resolve_credential("manifold_api_key", ("MANIFOLD_API_KEY",), label="MANIFOLD_API_KEY")
        health.update(
            {
                "api_base_url": self.api_base_url,
                "live_trading_enabled": self.config_bool("live_trading_enabled", False),
                "credential_sources": (
                    [{"name": credential.name, "source": credential.source}] if credential else []
                ),
                "orderbook_supported": False,
                "activity_feed_supported": True,
                "copy_trading_supported": bool(self.capabilities.copy_trading),
                "account_recovery_operations": list(self.account_recovery_operations),
                "order_management_operations": list(self.order_management_operations),
                "order_management_enabled": self.config_bool("manifold_order_management_enabled", False),
                "authenticated_account_endpoints": ["GET /v0/me", "GET /v0/bets"],
                "order_management_endpoints": ["POST /v0/bet/{id}/cancel"],
            }
        )
        return health

    @property
    def api_base_url(self) -> str:
        configured = self.config.get("manifold_api_base_url") or self.config.get("api_base_url")
        return str(configured or DEFAULT_MANIFOLD_BASE_URL).rstrip("/")

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = max(1, min(int(limit or 50), 1000))
        params = {
            "term": str(query or ""),
            "sort": str(self.config.get("manifold_sort") or "most-popular"),
            "filter": str(self.config.get("manifold_market_filter") or "open"),
            "contractType": str(self.config.get("manifold_contract_type") or "ALL"),
            "limit": desired,
        }
        markets = self._as_market_list(self._get("/search-markets", params=params))
        return [self._event_from_market(market) for market in markets[:desired]]

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        market = self._get_market(str(event_id or "").strip())
        if not market:
            return []
        return self._contracts_from_market(market)

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        market_id, outcome, answer_id = self._split_contract_id(contract_id)
        data = self._get(f"/market/{market_id}/prob")
        price = self._price_from_probability_payload(data, outcome, answer_id)
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=self._contract_id(market_id, outcome, answer_id),
            last=price,
            midpoint=price,
            source="manifold_probability",
            raw=data if isinstance(data, dict) else {},
        )

    def list_activity(self, identity: str, *, limit: int = 25) -> List[Dict[str, Any]]:
        """Return normalized public Manifold bets for ``manifold:<username>``.

        The public ``/v0/bets`` endpoint contains executed and pending limit
        bets together. Only filled, non-cancelled, non-redemption bets are
        exposed to the copy workflow; malformed rows are ignored rather than
        turned into an unsafe order intent.
        """

        self.ensure_capability("copy_trading")
        normalized = require_activity_identity(self.market_id, identity)
        username = normalized.split(":", 1)[1]
        desired = max(1, min(int(limit or 25), 1000))
        payload = self._get("/bets", params={"username": username, "limit": desired})
        rows = self._activity_rows(payload)
        activities: List[Dict[str, Any]] = []
        for bet in rows:
            if bet.get("isRedemption") is True or bet.get("isCancelled") is True:
                continue
            if bet.get("isFilled") is False:
                continue
            try:
                activities.append(self._activity_from_bet(normalized, bet))
            except MarketConfigurationError:
                continue
        return activities

    def list_trades(
        self,
        contract_id: str,
        *,
        limit: int = 50,
        before: Optional[float] = None,
        after: Optional[float] = None,
    ) -> List[MarketTrade]:
        """Return normalized public Manifold fills for one contract.

        Manifold's documented ``GET /v0/bets`` endpoint returns executed bets
        (and open limit bets) rather than a separate trade resource.  A bet
        may contain multiple ``fills``; each fill is emitted as its own
        normalized trade so partial limit-order executions are not collapsed
        into one misleading event.  ``before`` and ``after`` are interpreted
        as Unix timestamps by the shared history API and sent as the
        endpoint's documented millisecond ``beforeTime``/``afterTime``
        filters.
        """

        market_id, outcome, answer_id = self._split_contract_id(contract_id)
        params: Dict[str, Any] = {
            "contractId": market_id,
            "limit": self._history_limit(limit),
        }
        if before is not None:
            params["beforeTime"] = self._history_timestamp_millis(before, "before")
        if after is not None:
            params["afterTime"] = self._history_timestamp_millis(after, "after")

        payload = self._get("/bets", params=params)
        rows = self._activity_rows(payload)
        canonical = self._contract_id(market_id, outcome, answer_id)
        trades: List[MarketTrade] = []
        for bet in rows:
            if bet.get("isCancelled") is True or bet.get("isRedemption") is True:
                continue
            row_market_id = str(bet.get("contractId") or bet.get("contract_id") or "").strip()
            if row_market_id != market_id:
                continue
            row_answer_id = str(bet.get("answerId") or bet.get("answer_id") or "").strip() or None
            if row_answer_id != answer_id:
                continue
            if answer_id is None and str(bet.get("outcome") or "").strip().upper() != outcome:
                continue

            raw_fills = bet.get("fills")
            fills = [fill for fill in raw_fills if isinstance(fill, Mapping)] if isinstance(raw_fills, list) else []
            if not fills:
                # Normal bets have no fills array; their amount/shares pair
                # represents one executed fill.  Do not treat an unfilled
                # limit order as a trade.
                if bet.get("isFilled") is False:
                    continue
                fills = [bet]

            bet_id = str(bet.get("id") or bet.get("betId") or "").strip()
            if not bet_id:
                continue
            bet_amount = self._finite_number(bet.get("amount"))
            for index, fill in enumerate(fills):
                amount = self._finite_number(fill.get("amount"))
                shares = self._finite_number(fill.get("shares"))
                if amount is None and fill is bet:
                    amount = bet_amount
                if shares is None and fill is bet:
                    shares = self._finite_number(bet.get("shares"))
                if shares is None or not self._is_positive_number(abs(shares)):
                    continue

                sign_source = bet_amount if bet_amount not in (None, 0.0) else amount
                side = "SELL" if sign_source is not None and sign_source < 0 else "BUY"
                price = self._safe_probability(fill.get("price"))
                if price is None and amount is not None and shares != 0:
                    price = self._safe_probability(abs(amount) / abs(shares))
                if price is None:
                    price = self._safe_probability(bet.get("limitProb"))
                if price is None:
                    price = self._safe_probability(bet.get("probAfter") if side == "BUY" else bet.get("probBefore"))
                if price is None:
                    continue

                fill_id = bet_id if len(fills) == 1 else f"{bet_id}:{index}"
                timestamp = self._timestamp_seconds(
                    fill.get("timestamp")
                    or fill.get("createdTime")
                    or bet.get("createdTime")
                    or bet.get("createdAt")
                )
                raw = dict(bet)
                if fill is not bet:
                    raw["fill"] = dict(fill)
                trades.append(
                    MarketTrade(
                        market_id=self.market_id,
                        contract_id=canonical,
                        trade_id=fill_id,
                        side=side,
                        price=price,
                        size=abs(shares),
                        timestamp=timestamp,
                        raw=raw,
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
        """Derive bounded OHLCV candles from Manifold's documented fills.

        Manifold exposes executed bets/fills rather than an OHLCV endpoint.
        The aggregation is therefore explicitly derived, uses the bounded
        public ``/v0/bets`` history page, and retains source fill ids for
        auditability rather than claiming native candle support.
        """

        self.ensure_capability("candle_history")
        interval = self._candle_interval(resolution)
        start_ts = self._candle_timestamp(from_timestamp, "from_timestamp") if from_timestamp is not None else None
        end_ts = self._candle_timestamp(to_timestamp, "to_timestamp") if to_timestamp is not None else None
        if start_ts is not None and end_ts is not None and end_ts < start_ts:
            raise MarketConfigurationError("Manifold candle history requires to_timestamp to be at or after from_timestamp.")

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
            if start_ts is not None and trade.timestamp < start_ts:
                continue
            if end_ts is not None and trade.timestamp > end_ts:
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

        canonical = self._canonical_contract_id(contract_id)
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
                    "source": "manifold_public_bet_fills",
                    "derived": True,
                    "resolution": str(resolution or "").strip().lower(),
                    "interval_seconds": interval,
                    "trade_ids": list(bucket["trade_ids"]),
                },
            )
            for bucket_timestamp, bucket in sorted(buckets.items())
        ]

    def copy_trade_from_activity(self, activity: Mapping[str, Any]) -> PaperOrderResult:
        """Build a guarded paper order from a normalized Manifold bet."""

        self.ensure_capability("copy_trading")
        contract_id = str(activity.get("asset") or activity.get("contract_id") or "").strip()
        if not contract_id:
            raise MarketConfigurationError("Manifold activity has no contract id.")
        side = str(activity.get("side") or "").strip().upper()
        if side not in {"BUY", "SELL"}:
            raise MarketConfigurationError("Manifold activity side must be BUY or SELL.")
        size = self._required_positive_number(activity.get("size"), "Manifold activity size")
        raw_price = activity.get("price")
        reference_price = None if raw_price in (None, "") else self._safe_probability(raw_price)
        if raw_price not in (None, "") and reference_price is None:
            raise MarketConfigurationError("Manifold activity reference probability must be between 0 and 1.")
        metadata: Dict[str, Any] = {"activity": dict(activity), "source": "manifold_bet_feed"}
        if side == "SELL":
            metadata["shares"] = activity.get("shares") or size
            # Manifold's sell endpoint cannot enforce a limit. Retain the
            # observed fill only as provenance so the paper preview never
            # presents it as a wire-enforced limit.
            if reference_price is not None:
                metadata["reference_price"] = reference_price
        return self.place_paper_order(
            PaperOrderRequest(
                market_id=self.market_id,
                contract_id=contract_id,
                side=side,
                size=size,
                limit_price=None if side == "SELL" else reference_price,
                metadata=metadata,
            )
        )

    def get_orderbook(self, contract_id: str):
        self.ensure_capability("orderbook_reading")
        raise UnsupportedFeatureError(
            self.market_id,
            "orderbook_reading",
            "Manifold exposes documented probabilities and bet history, not a CLOB orderbook endpoint.",
        )

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        self._validate_order(order)
        payload, endpoint = self._build_order_payload(order, dry_run=True)
        raw: Dict[str, Any] = {"endpoint": endpoint, "request": payload}
        if "reference_price" in order.metadata:
            raw["reference_price"] = order.metadata["reference_price"]
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=self._canonical_contract_id(order.contract_id),
            accepted=True,
            message=(
                f"DRY RUN: would place Manifold {order.side.upper()} "
                f"for {order.size:.4f} MANA-equivalent"
                + (f" at limit {order.limit_price:.2f}" if order.limit_price is not None else "")
            ),
            filled_size=0.0,
            average_price=None,
            raw=raw,
        )

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        self.ensure_capability("live_trading")
        self._validate_order(order)
        preflight = self.preflight_live_order(order)
        payload, endpoint = self._build_order_payload(order, dry_run=False)
        if order.side.upper() == "BUY" and self._split_contract_id(order.contract_id)[2]:
            raise MarketConfigurationError(
                "Manifold live BUY for one multiple-choice answer is not implemented because the documented "
                "/v0/multi-bet endpoint requires multiple answer IDs."
            )
        headers = self._auth_headers()
        response = self.runtime.request_json(
            "POST",
            self._url(endpoint),
            json_body=payload,
            headers=headers,
        )
        return {
            "market_id": self.market_id,
            "contract_id": self._canonical_contract_id(order.contract_id),
            "live": True,
            "endpoint": endpoint,
            "preflight": preflight,
            "request": payload,
            "response": response,
        }

    def account_recovery(self, operation: str, **kwargs: Any) -> Any:
        """Read Manifold's documented authenticated account and bet feeds."""

        normalized = str(operation or "").strip().lower()
        if normalized not in self.account_recovery_operations:
            raise MarketConfigurationError(
                "Manifold account operation must be one of: "
                + ", ".join(self.account_recovery_operations)
                + "."
            )

        headers = self._auth_headers()
        account = self._get("/me", headers=headers)
        if normalized == "account":
            return account
        if not isinstance(account, Mapping):
            raise MarketConfigurationError("Manifold /me response was not an object.")

        user_id = self._safe_id(account.get("id") or account.get("userId"), "account id")
        username = str(account.get("username") or "").strip()
        if not user_id and not username:
            raise MarketConfigurationError("Manifold /me response omitted both id and username.")

        params: Dict[str, Any] = {
            "limit": self._history_limit(kwargs.get("limit", 50)),
        }
        if user_id:
            params["userId"] = user_id
        elif username:
            params["username"] = self._safe_id(username, "username")

        contract_id = str(kwargs.get("contract_id") or "").strip()
        if contract_id:
            params["contractId"] = self._safe_id(contract_id.split(":", 1)[0], "contract id")

        for key in ("before", "after"):
            value = str(kwargs.get(key) or "").strip()
            if value:
                params[key] = self._safe_id(value, f"{key} cursor")

        before_time = kwargs.get("before_time")
        after_time = kwargs.get("after_time")
        if before_time not in (None, ""):
            params["beforeTime"] = self._history_timestamp_millis(before_time, "before")
        if after_time not in (None, ""):
            params["afterTime"] = self._history_timestamp_millis(after_time, "after")
        if "beforeTime" in params and "afterTime" in params and params["beforeTime"] < params["afterTime"]:
            raise MarketConfigurationError("Manifold account history before_time must not precede after_time.")
        if normalized == "active_orders":
            params["kinds"] = "open-limit"

        response = self._get("/bets", params=params, headers=headers)
        return {
            "account": {"id": user_id, "username": username or None},
            "operation": normalized,
            "parameters": params,
            "response": response,
        }

    def manage_orders(self, operation: str, **kwargs: Any) -> Dict[str, Any]:
        """Cancel one open Manifold limit bet through the documented endpoint."""

        normalized = str(operation or "").strip().lower()
        if normalized not in self.order_management_operations:
            raise MarketConfigurationError(
                "Manifold order-management operation must be one of: "
                + ", ".join(self.order_management_operations)
                + "."
            )
        self.ensure_capability("live_trading")
        if not self.config_bool("manifold_order_management_enabled", False):
            raise MarketConfigurationError(
                "Manifold order management is disabled by adapter config. "
                "Set manifold_order_management_enabled=true only after reviewing live-order risk controls."
            )
        self.ensure_live_trading_enabled("Manifold order management")
        if str(kwargs.get("confirm_order_management") or "").strip() != MANIFOLD_ORDER_MANAGEMENT_CONFIRMATION:
            raise MarketConfigurationError(
                "Manifold order management requires exact confirmation text "
                f"{MANIFOLD_ORDER_MANAGEMENT_CONFIRMATION}."
            )
        if bool(kwargs.get("async_request")):
            raise MarketConfigurationError("Manifold order cancellation does not support async_request.")

        bet_id = self._safe_id(kwargs.get("order_id"), "bet id")
        if not bet_id:
            raise MarketConfigurationError("Manifold cancel_order requires order_id.")
        response = self.runtime.request_json(
            "POST",
            self._url(f"/bet/{bet_id}/cancel"),
            headers=self._auth_headers(),
        )
        return {
            "market_id": self.market_id,
            "operation": normalized,
            "order_id": bet_id,
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
                "endpoint": "POST /v0/bet/{id}/cancel",
            },
            "response": response,
        }

    def _get_market(self, ref: str) -> Optional[Mapping[str, Any]]:
        if not ref:
            return None
        market_id = self._split_contract_id(ref)[0] if ":" in ref else ref
        try:
            data = self._get(f"/market/{market_id}")
        except MarketHTTPError:
            data = self._get(f"/slug/{market_id}")
        return data if isinstance(data, Mapping) else None

    def _get(
        self,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Any:
        request_kwargs: Dict[str, Any] = {"params": params}
        if headers is not None:
            request_kwargs["headers"] = headers
        return self.runtime.get_json(self._url(path), **request_kwargs)

    @staticmethod
    def _activity_rows(data: Any) -> List[Mapping[str, Any]]:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, Mapping)]
        if isinstance(data, Mapping):
            for key in ("bets", "data", "results"):
                value = data.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, Mapping)]
        return []

    def _activity_from_bet(self, identity: str, bet: Mapping[str, Any]) -> Dict[str, Any]:
        market_id = str(bet.get("contractId") or bet.get("contract_id") or "").strip()
        if not market_id:
            raise MarketConfigurationError("Manifold bet response omitted contractId.")
        answer_id = str(bet.get("answerId") or bet.get("answer_id") or "").strip() or None
        if answer_id:
            contract_id = self._contract_id(market_id, "ANSWER", answer_id)
            outcome = str(bet.get("outcome") or answer_id).strip()
        else:
            outcome = str(bet.get("outcome") or "").strip().upper()
            if outcome not in {"YES", "NO"}:
                raise MarketConfigurationError("Manifold binary bet outcome must be YES or NO.")
            contract_id = self._contract_id(market_id, outcome)

        amount = self._finite_number(bet.get("amount"))
        shares = self._finite_number(bet.get("shares"))
        if amount is None and shares is None:
            raise MarketConfigurationError("Manifold bet response omitted amount and shares.")
        side = "SELL" if ((amount is not None and amount < 0) or (amount is None and shares is not None and shares < 0)) else "BUY"
        if side == "BUY":
            size = abs(amount if amount is not None else shares or 0.0)
        else:
            size = abs(shares if shares is not None else amount or 0.0)
        if not self._is_positive_number(size):
            raise MarketConfigurationError("Manifold bet size must be positive.")

        limit_probability = self._safe_probability(bet.get("limitProb"))
        if limit_probability is None:
            limit_probability = self._safe_probability(
                bet.get("probAfter") if side == "BUY" else bet.get("probBefore")
            )
        bet_id = str(bet.get("id") or bet.get("betId") or "").strip()
        if not bet_id:
            raise MarketConfigurationError("Manifold bet response omitted a stable id.")
        timestamp = self._timestamp_seconds(bet.get("createdTime") or bet.get("createdAt") or bet.get("timestamp"))
        username = identity.split(":", 1)[1]
        return {
            "type": "TRADE",
            "proxyWallet": identity,
            "wallet": identity,
            "asset": contract_id,
            "contract_id": contract_id,
            "marketId": market_id,
            "side": side,
            "size": size,
            "amount": abs(amount) if amount is not None else None,
            "shares": abs(shares) if shares is not None else None,
            "price": limit_probability,
            "timestamp": timestamp,
            "transactionHash": f"manifold-bet:{bet_id}",
            "activity_id": bet_id,
            "slug": str(bet.get("contractSlug") or bet.get("slug") or market_id),
            "outcome": str(bet.get("outcome") or outcome),
            "pseudonym": str(bet.get("userUsername") or bet.get("username") or username),
            "raw": dict(bet),
        }

    @staticmethod
    def _history_limit(value: Any) -> int:
        try:
            limit = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Manifold trade limit must be an integer.") from exc
        return max(1, min(limit, 1000))

    @staticmethod
    def _history_timestamp_millis(value: Any, label: str) -> int:
        number = ManifoldAdapter._finite_number(value)
        if number is None or number <= 0:
            raise MarketConfigurationError(f"Manifold trade {label} timestamp must be positive.")
        if number < 100_000_000_000:
            number *= 1000.0
        return int(number)

    @staticmethod
    def _candle_timestamp(value: Any, label: str) -> float:
        number = ManifoldAdapter._finite_number(value)
        if number is None or number < 0:
            raise MarketConfigurationError(f"Manifold {label} timestamp must be a finite non-negative epoch second.")
        return number

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
                f"Manifold candle resolution must be one of: {', '.join(intervals)}."
            ) from exc

    def _candle_trade_limit(self) -> int:
        raw_limit = self.config.get("manifold_candle_trade_limit", 1000)
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Manifold candle trade limit must be an integer between 1 and 1000.") from exc
        if limit < 1 or limit > 1000:
            raise MarketConfigurationError("Manifold candle trade limit must be between 1 and 1000.")
        return limit

    def _url(self, path: str) -> str:
        clean_path = "/" + str(path or "").strip("/")
        return f"{self.api_base_url}{clean_path}"

    def _auth_headers(self) -> Dict[str, str]:
        credential = self.resolve_credential(
            "manifold_api_key",
            ("MANIFOLD_API_KEY",),
            required=True,
            label="MANIFOLD_API_KEY",
        )
        return {"Authorization": f"Key {credential.value}", "Content-Type": "application/json"}

    @staticmethod
    def _safe_id(value: Any, label: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if not MANIFOLD_ID_PATTERN.fullmatch(text):
            raise MarketConfigurationError(f"Manifold {label} is invalid or contains path separators.")
        return text

    def _event_from_market(self, market: Mapping[str, Any]) -> MarketEvent:
        event_id = str(market.get("id") or "").strip()
        return MarketEvent(
            market_id=self.market_id,
            event_id=event_id,
            title=str(market.get("question") or event_id),
            url=str(market.get("url") or ""),
            status=self._status_from_market(market),
            raw=dict(market),
        )

    def _contracts_from_market(self, market: Mapping[str, Any]) -> List[MarketContract]:
        market_id = str(market.get("id") or "").strip()
        if not market_id:
            return []
        outcome_type = str(market.get("outcomeType") or "").upper()
        question = str(market.get("question") or market_id)
        status = self._status_from_market(market)
        if outcome_type == "BINARY":
            return [
                MarketContract(
                    market_id=self.market_id,
                    contract_id=self._contract_id(market_id, "YES"),
                    event_id=market_id,
                    title=f"{question} - Yes",
                    outcome="Yes",
                    url=str(market.get("url") or ""),
                    status=status,
                    raw={"market": dict(market), "outcome": "YES"},
                ),
                MarketContract(
                    market_id=self.market_id,
                    contract_id=self._contract_id(market_id, "NO"),
                    event_id=market_id,
                    title=f"{question} - No",
                    outcome="No",
                    url=str(market.get("url") or ""),
                    status=status,
                    raw={"market": dict(market), "outcome": "NO"},
                ),
            ]

        answers = market.get("answers") or []
        contracts: List[MarketContract] = []
        if isinstance(answers, list):
            for answer in answers:
                if not isinstance(answer, Mapping):
                    continue
                answer_id = str(answer.get("id") or "").strip()
                if not answer_id:
                    continue
                answer_text = str(answer.get("text") or answer.get("name") or answer_id)
                contracts.append(
                    MarketContract(
                        market_id=self.market_id,
                        contract_id=self._contract_id(market_id, "ANSWER", answer_id),
                        event_id=market_id,
                        title=f"{question} - {answer_text}",
                        outcome=answer_text,
                        url=str(market.get("url") or ""),
                        status=status,
                        raw={"market": dict(market), "answer": dict(answer)},
                    )
                )
        return contracts

    def _build_order_payload(self, order: PaperOrderRequest, *, dry_run: bool) -> Tuple[Dict[str, Any], str]:
        market_id, outcome, answer_id = self._split_contract_id(order.contract_id)
        side = str(order.side or "").upper()
        if side == "SELL":
            if order.limit_price is not None:
                raise MarketConfigurationError(
                    "Manifold SELL does not support a wire-enforced limit price; remove the limit or do not submit."
                )
            # The transmitted share count must be the exact value that passed
            # the shared size/exposure caps. A metadata override would let the
            # wire order diverge from the reviewed request.
            payload: Dict[str, Any] = {"shares": float(order.size)}
            if outcome in {"YES", "NO"}:
                payload["outcome"] = outcome
            if answer_id:
                payload["answerId"] = answer_id
                payload.setdefault("outcome", "YES")
            return payload, f"/market/{market_id}/sell"

        payload = {
            "amount": float(order.size),
            "contractId": market_id,
            "outcome": "YES" if answer_id else outcome,
            "dryRun": dry_run,
        }
        if answer_id:
            payload["answerId"] = answer_id
        if order.limit_price is not None:
            payload["limitProb"] = self._limit_probability(order.limit_price)
        for key in ("expiresAt", "expiresMillisAfter"):
            if key in order.metadata:
                payload[key] = order.metadata[key]
        return payload, "/bet"

    def _validate_order(self, order: PaperOrderRequest) -> None:
        self.ensure_order_market(order)
        self._split_contract_id(order.contract_id)
        side = str(order.side or "").upper()
        if side not in {"BUY", "SELL"}:
            raise MarketConfigurationError("Manifold order side must be BUY or SELL.")
        if not self._is_positive_number(order.size):
            raise MarketConfigurationError("Manifold order size must be positive.")
        if order.limit_price is not None:
            self._limit_probability(order.limit_price)

    @staticmethod
    def _as_market_list(data: Any) -> List[Mapping[str, Any]]:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, Mapping)]
        if isinstance(data, Mapping):
            markets = data.get("markets") or data.get("results") or data.get("contracts") or []
            if isinstance(markets, list):
                return [item for item in markets if isinstance(item, Mapping)]
        return []

    @staticmethod
    def _status_from_market(market: Mapping[str, Any]) -> str:
        if market.get("isResolved") is True:
            return "resolved"
        close_time = market.get("closeTime")
        if close_time is not None:
            try:
                if float(close_time) <= 0:
                    return "open"
            except (TypeError, ValueError):
                pass
        return "open"

    @staticmethod
    def _price_from_probability_payload(data: Any, outcome: str, answer_id: Optional[str]) -> float:
        if not isinstance(data, Mapping):
            raise MarketConfigurationError("Manifold probability response was not an object.")
        if answer_id:
            answer_probs = data.get("answerProbs")
            if not isinstance(answer_probs, Mapping) or answer_id not in answer_probs:
                raise MarketConfigurationError(f"Manifold probability response did not include answer {answer_id}.")
            probability = ManifoldAdapter._safe_probability(answer_probs.get(answer_id))
        else:
            probability = ManifoldAdapter._safe_probability(data.get("prob"))
            if outcome == "NO" and probability is not None:
                probability = 1.0 - probability
        if probability is None:
            raise MarketConfigurationError("Manifold probability must be between 0 and 1.")
        return probability

    @staticmethod
    def _split_contract_id(contract_id: str) -> Tuple[str, str, Optional[str]]:
        raw = str(contract_id or "").strip()
        if not raw:
            raise MarketConfigurationError("Manifold order requires a contract id.")
        parts = raw.split(":")
        market_id = parts[0].strip()
        if not market_id:
            raise MarketConfigurationError("Manifold order requires a market id.")
        if len(parts) == 1:
            return market_id, "YES", None
        outcome = parts[1].strip().upper()
        if outcome == "ANSWER":
            if len(parts) < 3 or not parts[2].strip():
                raise MarketConfigurationError("Manifold answer contract requires an answer id.")
            return market_id, "ANSWER", parts[2].strip()
        if outcome not in {"YES", "NO"}:
            raise MarketConfigurationError("Manifold binary contract outcome must be YES or NO.")
        return market_id, outcome, None

    @staticmethod
    def _contract_id(market_id: str, outcome: str, answer_id: Optional[str] = None) -> str:
        if outcome.upper() == "ANSWER":
            return f"{market_id}:ANSWER:{answer_id}"
        return f"{market_id}:{outcome.upper()}"

    @staticmethod
    def _canonical_contract_id(contract_id: str) -> str:
        return ManifoldAdapter._contract_id(*ManifoldAdapter._split_contract_id(contract_id))

    @staticmethod
    def _safe_probability(value: Any) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number) or number < 0.0 or number > 1.0:
            return None
        return number

    @staticmethod
    def _finite_number(value: Any) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    @staticmethod
    def _required_positive_number(value: Any, label: str) -> float:
        number = ManifoldAdapter._finite_number(value)
        if number is None or number <= 0:
            raise MarketConfigurationError(f"{label} must be positive.")
        return number

    @staticmethod
    def _timestamp_seconds(value: Any) -> int:
        number = ManifoldAdapter._finite_number(value)
        if number is None or number <= 0:
            return 0
        if number > 100_000_000_000:
            number /= 1000.0
        return int(number)

    @staticmethod
    def _limit_probability(value: Any) -> float:
        probability = ManifoldAdapter._safe_probability(value)
        if probability is None or probability < 0.01 or probability > 0.99:
            raise MarketConfigurationError("Manifold limit price must be between 0.01 and 0.99.")
        rounded = round(probability, 2)
        if abs(probability - rounded) > 1e-9:
            raise MarketConfigurationError("Manifold limit price must use whole percentage points, e.g. 0.42.")
        return rounded

    @staticmethod
    def _is_positive_number(value: Any) -> bool:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False
        return math.isfinite(number) and number > 0
