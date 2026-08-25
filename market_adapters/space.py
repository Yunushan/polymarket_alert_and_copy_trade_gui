from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from .base import MarketAdapter
from .catalog import get_market_metadata
from .errors import MarketConfigurationError, MarketHTTPError, UnsupportedFeatureError
from .types import (
    MarketContract,
    MarketCandle,
    MarketEvent,
    MarketTrade,
    OrderBookLevel,
    OrderBookSnapshot,
    PaperOrderRequest,
    PaperOrderResult,
    PriceSnapshot,
)


DEFAULT_SPACE_API_BASE_URL = "https://api.into.space/v1"
SPACE_REFERENCES = (
    "https://docs.into.space/en/api/rest",
    "https://docs.into.space/en/build/resources",
    "https://docs.into.space/api-reference/openapi.json",
)
_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_OUTCOME_RE = re.compile(r"^[^:\r\n]{1,128}$")
_BINARY_OUTCOMES = ("YES", "NO")


class SpaceAdapter(MarketAdapter):
    """Public read/paper adapter for Space's documented REST API.

    Space documents anonymous ``/markets``, market-detail, and orderbook
    endpoints.  Its REST reference currently documents reads only and says the
    public production release will be announced separately, so wallet-signed
    live orders and account/copy workflows remain deliberately unsupported.
    """

    metadata = get_market_metadata("space")
    live_order_sides = ("BUY", "SELL")

    def __init__(self, config: Optional[Mapping[str, Any]] = None, *, runtime=None) -> None:
        super().__init__(config, runtime=runtime)
        self._market_cache: Dict[str, Mapping[str, Any]] = {}

    @property
    def api_base_url(self) -> str:
        configured = self.config.get("space_api_base_url") or self.config.get("api_base_url")
        base = str(configured or DEFAULT_SPACE_API_BASE_URL).strip().rstrip("/")
        parsed = urlsplit(base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
            raise MarketConfigurationError(
                "Space API base URL must be an absolute http(s) URL without query or fragment."
            )
        return base

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        health.update(
            {
                "api_base_url": self.api_base_url,
                "references": list(SPACE_REFERENCES),
                "public_api": True,
                "anonymous_read_access": True,
                "production_api_notice": (
                    "Space's official REST reference documents the endpoint contract but currently says the "
                    "public production release will be announced separately; verify endpoint availability before use."
                ),
                "live_trading_supported": False,
                "live_trading_enabled": False,
                "wallet_transaction_required": True,
                "copy_trading_supported": False,
            }
        )
        return health

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = max(1, min(int(limit or 50), 100))
        params: Dict[str, Any] = {
            "status": str(self.config.get("space_market_status") or "active"),
            "limit": desired,
            "offset": max(0, int(self.config.get("space_market_offset") or 0)),
        }
        category = str(self.config.get("space_market_category") or "").strip()
        if category:
            params["category"] = category
        payload = self._get("/markets", params=params)
        rows = self._rows(payload)
        needle = str(query or "").strip().lower()
        events: List[MarketEvent] = []
        for row in rows:
            market_id = self._market_id(row.get("id") or row.get("marketId") or row.get("market_id"))
            title = str(row.get("question") or row.get("title") or market_id).strip()
            search_text = " ".join(
                str(row.get(key) or "") for key in ("id", "marketId", "question", "title", "category")
            ).lower()
            if needle and needle not in search_text:
                continue
            event = MarketEvent(
                market_id=self.market_id,
                event_id=self._event_id(market_id),
                title=title,
                url=f"{self.api_base_url}/markets/{market_id}",
                status=str(row.get("status") or "active").lower(),
                raw=dict(row),
            )
            self._market_cache[market_id.lower()] = dict(row)
            events.append(event)
            if len(events) >= desired:
                break
        return events

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        market_id = self._market_id_from_event_id(event_id)
        market = self._read_market(market_id)
        title = str(market.get("question") or market.get("title") or market_id).strip()
        status = str(market.get("status") or "active").lower()
        rows = self._outcome_rows(market)
        if not rows:
            rows = [{"name": outcome} for outcome in _BINARY_OUTCOMES]
        contracts: List[MarketContract] = []
        for row in rows:
            outcome = self._outcome_name(row)
            contracts.append(
                MarketContract(
                    market_id=self.market_id,
                    contract_id=self._contract_id(market_id, outcome),
                    event_id=self._event_id(market_id),
                    title=f"{title} - {outcome}",
                    outcome=outcome,
                    url=f"{self.api_base_url}/markets/{market_id}",
                    status=status,
                    raw={"market": dict(market), "outcome": dict(row)},
                )
            )
        return contracts

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        market_id, outcome = self._split_contract_id(contract_id)
        market = self._read_market(market_id)
        price = self._price_for_outcome(market, outcome)
        if price is None:
            raise MarketHTTPError(f"Space market {market_id!r} did not expose a bounded price for {outcome!r}.")
        canonical = self._contract_id(market_id, outcome)
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=canonical,
            last=price,
            bid=price,
            ask=price,
            midpoint=price,
            source="space_rest_market_detail",
            raw={"market": dict(market), "outcome": outcome, "price": price},
        )

    def get_orderbook(self, contract_id: str) -> OrderBookSnapshot:
        self.ensure_capability("orderbook_reading")
        market_id, outcome = self._split_contract_id(contract_id)
        depth = self.config.get("space_orderbook_depth", 20)
        try:
            depth = max(1, min(int(depth), 100))
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Space orderbook depth must be an integer between 1 and 100.") from exc
        payload = self._get(
            f"/markets/{market_id}/orderbook",
            params={"outcome": outcome, "depth": depth},
        )
        book = self._mapping_payload(payload)
        canonical = self._contract_id(market_id, outcome)
        return OrderBookSnapshot(
            market_id=self.market_id,
            contract_id=canonical,
            bids=self._levels(book.get("bids"), descending=True),
            asks=self._levels(book.get("asks")),
            raw={"market_id": market_id, "outcome": outcome, "orderbook": dict(book)},
        )

    def list_trades(
        self,
        contract_id: str,
        *,
        limit: int = 50,
        before: Optional[float] = None,
        after: Optional[float] = None,
    ) -> List[MarketTrade]:
        """Read the documented public trade feed for one market outcome."""

        market_id, outcome = self._split_contract_id(contract_id)
        query: Dict[str, Any] = {
            "outcome": outcome,
            "limit": self._bounded_limit(limit, maximum=500, label="Space trade limit"),
        }
        if before is not None:
            query["before"] = self._timestamp_cursor(before, "before")
        if after is not None:
            query["after"] = self._timestamp_cursor(after, "after")
        payload = self._get(f"/markets/{market_id}/trades", params=query)
        rows = self._rows_for_key(payload, "trades")
        trades: List[MarketTrade] = []
        canonical = self._contract_id(market_id, outcome)
        for row in rows:
            trade_id = str(row.get("id") or row.get("tradeId") or "").strip()
            price = self._bounded_probability(row.get("price"), allow_zero=True)
            size = self._finite_nonnegative(row.get("quantity") if row.get("quantity") is not None else row.get("size"))
            if not trade_id or price is None or size is None or size <= 0:
                continue
            side = str(row.get("side") or "").strip().upper()
            if side not in {"BUY", "SELL"}:
                continue
            timestamp = self._optional_timestamp(row.get("timestamp"))
            trades.append(
                MarketTrade(
                    market_id=self.market_id,
                    contract_id=canonical,
                    trade_id=trade_id,
                    side=side,
                    price=price,
                    size=size,
                    timestamp=timestamp,
                    raw=dict(row),
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
        """Read the documented public OHLCV candle feed for one outcome."""

        market_id, outcome = self._split_contract_id(contract_id)
        clean_resolution = str(resolution or "").strip().lower()
        if clean_resolution not in {"1m", "5m", "15m", "1h", "4h", "1d"}:
            raise MarketConfigurationError("Space candle resolution must be one of 1m, 5m, 15m, 1h, 4h, or 1d.")
        query: Dict[str, Any] = {"outcome": outcome, "resolution": clean_resolution}
        if from_timestamp is not None:
            query["from"] = self._timestamp_cursor(from_timestamp, "from")
        if to_timestamp is not None:
            query["to"] = self._timestamp_cursor(to_timestamp, "to")
        payload = self._get(f"/markets/{market_id}/candles", params=query)
        rows = self._rows_for_key(payload, "candles")
        candles: List[MarketCandle] = []
        canonical = self._contract_id(market_id, outcome)
        for row in rows:
            timestamp = self._optional_timestamp(row.get("time") if row.get("time") is not None else row.get("timestamp"))
            values = [self._bounded_probability(row.get(key), allow_zero=True) for key in ("open", "high", "low", "close")]
            volume = self._finite_nonnegative(row.get("volume"))
            if timestamp is None or any(value is None for value in values):
                continue
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
                    raw=dict(row),
                )
            )
        return candles

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        market_id, outcome = self._validate_order(order)
        price = self._bounded_probability(order.limit_price)
        if price is None:
            price = self.get_price(self._contract_id(market_id, outcome)).last
        if price is None:
            raise MarketHTTPError(f"Space market {market_id!r} did not expose a price for paper execution.")
        canonical = self._contract_id(market_id, outcome)
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=canonical,
            accepted=True,
            message=(
                f"DRY RUN: would place Space {str(order.side).upper()} for {float(order.size):g} "
                f"{outcome} contracts at {price:.6f}"
            ),
            filled_size=0.0,
            average_price=price,
            raw={
                "dry_run": True,
                "official_api_is_read_only": True,
                "request": {
                    "market_id": market_id,
                    "outcome": outcome,
                    "side": str(order.side).upper(),
                    "size": float(order.size),
                    "limit_price": price,
                },
            },
        )

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        raise UnsupportedFeatureError(
            self.market_id,
            "live_trading",
            "Space's documented REST API does not publish a live order-submission or wallet-signing route; only public reads and dry-run orders are implemented.",
        )

    def copy_trade_from_activity(self, activity: Mapping[str, Any]) -> PaperOrderResult:
        raise UnsupportedFeatureError(
            self.market_id,
            "copy_trading",
            "Space does not publish a safe account-activity mirroring contract for copy trading.",
        )

    def _read_market(self, market_id: str) -> Dict[str, Any]:
        clean_id = self._market_id(market_id)
        cached = self._market_cache.get(clean_id.lower())
        if cached and (cached.get("yesToken") or cached.get("yes_token") or cached.get("prices")):
            return dict(cached)
        payload = self._get(f"/markets/{clean_id}")
        market = self._mapping_payload(payload)
        response_id = market.get("id") or market.get("marketId") or clean_id
        if str(response_id) != clean_id:
            raise MarketHTTPError(f"Space market detail returned unexpected id for {clean_id!r}.")
        self._market_cache[clean_id.lower()] = dict(market)
        return dict(market)

    def _validate_order(self, order: PaperOrderRequest) -> Tuple[str, str]:
        self.ensure_order_market(order)
        market_id, outcome = self._split_contract_id(order.contract_id)
        side = str(order.side or "").upper()
        if side not in self.live_order_sides:
            raise MarketConfigurationError("Space order side must be BUY or SELL.")
        try:
            size = float(order.size)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Space order size must be numeric.") from exc
        if not math.isfinite(size) or size <= 0:
            raise MarketConfigurationError("Space order size must be positive and finite.")
        if order.limit_price is not None and self._bounded_probability(order.limit_price) is None:
            raise MarketConfigurationError("Space order limit price must be greater than 0 and at most 1.")
        return market_id, outcome

    def _get(self, path: str, *, params: Optional[Mapping[str, Any]] = None) -> Any:
        segments = [segment for segment in str(path or "").split("/") if segment]
        if any(segment in {".", ".."} or not _PATH_SEGMENT_RE.fullmatch(segment) for segment in segments):
            raise MarketConfigurationError("Space API path contains an invalid segment.")
        return self.runtime.get_json(f"{self.api_base_url}/{'/'.join(segments)}", params=params, headers={})

    @classmethod
    def _market_id(cls, value: Any) -> str:
        market_id = str(value or "").strip()
        if not _PATH_SEGMENT_RE.fullmatch(market_id):
            raise MarketConfigurationError("Space market ids must contain only letters, numbers, '.', '_' or '-'.")
        return market_id

    @classmethod
    def _event_id(cls, market_id: str) -> str:
        return f"space:{cls._market_id(market_id)}"

    @classmethod
    def _market_id_from_event_id(cls, event_id: Any) -> str:
        text = str(event_id or "").strip()
        if text.lower().startswith("space:"):
            text = text.split(":", 1)[1]
        return cls._market_id(text)

    @classmethod
    def _contract_id(cls, market_id: str, outcome: str) -> str:
        return f"{cls._market_id(market_id)}:{cls._outcome_name(outcome)}"

    @classmethod
    def _split_contract_id(cls, contract_id: Any) -> Tuple[str, str]:
        text = str(contract_id or "").strip()
        parts = text.rsplit(":", 1)
        if len(parts) != 2:
            raise MarketConfigurationError("Space contract id must be '<market-id>:<outcome>'.")
        return cls._market_id(parts[0]), cls._outcome_name(parts[1])

    @staticmethod
    def _rows(payload: Any) -> List[Mapping[str, Any]]:
        if isinstance(payload, Mapping):
            value = payload.get("markets")
            if isinstance(value, list):
                return [row for row in value if isinstance(row, Mapping)]
            value = payload.get("data")
            if isinstance(value, Mapping):
                return SpaceAdapter._rows(value)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, Mapping)]
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, Mapping)]
        return []

    @staticmethod
    def _rows_for_key(payload: Any, key: str) -> List[Mapping[str, Any]]:
        if isinstance(payload, Mapping):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, Mapping)]
            value = payload.get("data")
            if isinstance(value, Mapping):
                return SpaceAdapter._rows_for_key(value, key)
        return []

    @staticmethod
    def _bounded_limit(value: Any, *, maximum: int, label: str) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"{label} must be an integer between 1 and {maximum}.") from exc
        if number < 1 or number > maximum:
            raise MarketConfigurationError(f"{label} must be an integer between 1 and {maximum}.")
        return number

    @staticmethod
    def _timestamp_cursor(value: Any, label: str) -> int:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"Space {label} timestamp must be finite and non-negative.") from exc
        if not math.isfinite(number) or number < 0:
            raise MarketConfigurationError(f"Space {label} timestamp must be finite and non-negative.")
        return int(number)

    @staticmethod
    def _optional_timestamp(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and number >= 0 else None

    @staticmethod
    def _finite_nonnegative(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and number >= 0 else None

    @staticmethod
    def _mapping_payload(payload: Any) -> Dict[str, Any]:
        if isinstance(payload, Mapping):
            value = payload.get("data")
            if isinstance(value, Mapping):
                return dict(value)
            value = payload.get("market")
            if isinstance(value, Mapping):
                return dict(value)
            return dict(payload)
        raise MarketHTTPError("Space endpoint returned a non-object JSON payload.")

    @staticmethod
    def _outcome_rows(market: Mapping[str, Any]) -> List[Mapping[str, Any]]:
        for key in ("outcomes", "options", "results"):
            value = market.get(key)
            if isinstance(value, list):
                rows: List[Mapping[str, Any]] = []
                for item in value:
                    if isinstance(item, Mapping):
                        rows.append(item)
                    elif item not in (None, ""):
                        rows.append({"name": item})
                if rows:
                    return rows
        return []

    @classmethod
    def _outcome_name(cls, value: Any) -> str:
        if isinstance(value, Mapping):
            value = value.get("name") or value.get("label") or value.get("outcome") or value.get("id")
        outcome = str(value or "").strip()
        if not _OUTCOME_RE.fullmatch(outcome):
            raise MarketConfigurationError("Space outcomes must be non-empty and cannot contain ':' or newlines.")
        return outcome.upper() if outcome.upper() in _BINARY_OUTCOMES else outcome

    @classmethod
    def _price_for_outcome(cls, market: Mapping[str, Any], outcome: str) -> Optional[float]:
        prices = market.get("prices")
        if isinstance(prices, Mapping):
            for key in (outcome, outcome.lower(), outcome.upper()):
                if key in prices:
                    value = prices[key]
                    if isinstance(value, Mapping):
                        value = value.get("price") or value.get("probability") or value.get("last")
                    price = cls._bounded_probability(value, allow_zero=True)
                    if price is not None:
                        return price
        if outcome.upper() == "YES":
            price = cls._bounded_probability(market.get("probability"), allow_zero=True)
            if price is not None:
                return price
        if outcome.upper() == "NO":
            yes = cls._bounded_probability(market.get("probability"), allow_zero=True)
            if yes is not None:
                return 1.0 - yes
        return None

    @staticmethod
    def _bounded_probability(value: Any, *, allow_zero: bool = False) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        lower = 0.0 if allow_zero else 0.0
        if not math.isfinite(number) or number < lower or number > 1.0 or (not allow_zero and number <= 0):
            return None
        return number

    @staticmethod
    def _levels(raw: Any, *, descending: bool = False) -> List[OrderBookLevel]:
        rows = raw if isinstance(raw, list) else []
        levels: List[OrderBookLevel] = []
        for item in rows:
            if not isinstance(item, Mapping):
                continue
            try:
                price = float(item.get("price"))
                size = float(item.get("quantity") if item.get("quantity") is not None else item.get("size"))
            except (TypeError, ValueError):
                continue
            if math.isfinite(price) and math.isfinite(size) and 0 <= price <= 1 and size > 0:
                levels.append(OrderBookLevel(price=price, size=size))
        levels.sort(key=lambda level: level.price, reverse=descending)
        return levels


__all__ = ["DEFAULT_SPACE_API_BASE_URL", "SPACE_REFERENCES", "SpaceAdapter"]
