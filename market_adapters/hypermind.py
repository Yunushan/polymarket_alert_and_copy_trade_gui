from __future__ import annotations

import csv
import io
import json
import math
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from .base import MarketAdapter
from .catalog import get_market_metadata
from .errors import MarketConfigurationError, MarketHTTPError, UnsupportedFeatureError
from .types import MarketCandle, MarketContract, MarketEvent, MarketTrade, PaperOrderRequest, PaperOrderResult, PriceSnapshot


DEFAULT_HYPERMIND_PRICES_URL = "https://predict.hypermind.com/hypermind/media/data/All-ifps-all-prices-260714.csv"
DEFAULT_HYPERMIND_OUTCOMES_URL = "https://predict.hypermind.com/hypermind/media/data/Winning-outcomes-all-260714.txt"
HYPERMIND_REFERENCES = (
    "https://www.hypermind.com/prediction-market/forecasting-accuracy",
    DEFAULT_HYPERMIND_PRICES_URL,
    DEFAULT_HYPERMIND_OUTCOMES_URL,
)
_ALLOWED_HOSTS = {"predict.hypermind.com"}
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\-]{0,127}$")
_OUTCOME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\- ]{0,127}$")
_PRICE_FIELDS = ("timestamp", "ifpid", "outcome", "price", "qty")


class HypermindAdapter(MarketAdapter):
    """Archive-only adapter for Hypermind's documented historical exports.

    Hypermind's July 2026 forecasting report links a trade-level CSV and a
    winning-outcomes text export.  Those files provide historical prices,
    quantities, contracts, and resolution metadata, but do not describe a
    supported orderbook or execution API.  The adapter therefore never
    scrapes the site, submits orders, or infers BUY/SELL direction from a
    trade row that does not publish it.
    """

    metadata = get_market_metadata("hypermind")
    live_order_sides = ("BUY", "SELL")

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        health.update(
            {
                "references": list(HYPERMIND_REFERENCES),
                "public_api": False,
                "official_export": True,
                "archive_only": True,
                "dynamic_discovery": True,
                "configured_prices_url": self.prices_url,
                "configured_outcomes_url": self.outcomes_url,
                "orderbook_supported": False,
                "live_trading_supported": False,
                "live_trading_enabled": False,
                "copy_trading_supported": False,
                "price_scale": "source percentages 0-100 normalized to 0-1",
                "data_semantics": "historical play-money trade-level prices; trade direction is not published",
            }
        )
        return health

    @property
    def prices_url(self) -> str:
        return self._validated_url(
            self.config.get("hypermind_prices_url") or DEFAULT_HYPERMIND_PRICES_URL,
            "prices",
        )

    @property
    def outcomes_url(self) -> str:
        return self._validated_url(
            self.config.get("hypermind_outcomes_url") or DEFAULT_HYPERMIND_OUTCOMES_URL,
            "outcomes",
        )

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = self._limit(limit, "Hypermind event limit", maximum=1000)
        outcomes = self._load_outcomes()
        inventory: Dict[str, Dict[str, Any]] = {}
        for row in self._iter_price_rows():
            item = inventory.setdefault(row["market_id"], {"outcomes": set(), "first_timestamp": row["timestamp"]})
            item["outcomes"].add(row["outcome"])
            item["first_timestamp"] = min(item["first_timestamp"], row["timestamp"])
        needle = str(query or "").strip().lower()
        events: List[MarketEvent] = []
        for market_id in sorted(inventory):
            title = f"Hypermind market {market_id}"
            if needle and needle not in f"{market_id} {title}".lower():
                continue
            events.append(
                MarketEvent(
                    market_id=self.market_id,
                    event_id=self._event_id(market_id),
                    title=title,
                    url=self._report_url,
                    status="resolved" if market_id in outcomes else "archived",
                    raw={
                        "archive_only": True,
                        "outcome_count": len(inventory[market_id]["outcomes"]),
                        "first_timestamp": inventory[market_id]["first_timestamp"],
                        "resolved_outcomes": list(outcomes.get(market_id, ())),
                    },
                )
            )
            if len(events) >= desired:
                break
        return events

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        market_id = self._market_from_event_id(event_id)
        outcome_map = self._load_outcomes()
        outcomes = set(outcome_map.get(market_id, ()))
        for row in self._iter_price_rows():
            if row["market_id"] == market_id:
                outcomes.add(row["outcome"])
        resolved = set(outcome_map.get(market_id, ()))
        return [
            MarketContract(
                market_id=self.market_id,
                contract_id=self._contract_id(market_id, outcome),
                event_id=self._event_id(market_id),
                title=f"Hypermind {market_id} - {outcome}",
                outcome=outcome,
                url=self.prices_url,
                status="resolved" if outcome in resolved else "archived",
                raw={"archive_only": True, "source": "hypermind_trade_export"},
            )
            for outcome in sorted(outcomes)
        ]

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        market_id, outcome = self._split_contract_id(contract_id)
        latest: Optional[Dict[str, Any]] = None
        for row in self._iter_price_rows():
            if row["market_id"] != market_id or row["outcome"] != outcome:
                continue
            if latest is None or row["timestamp"] >= latest["timestamp"]:
                latest = row
        if latest is None:
            raise MarketHTTPError(f"Hypermind export contains no price for {market_id}:{outcome}.")
        canonical = self._contract_id(market_id, outcome)
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=canonical,
            last=latest["price"],
            bid=None,
            ask=None,
            midpoint=latest["price"],
            source="hypermind_trade_export",
            raw={"archive_only": True, "row": dict(latest["raw"])},
        )

    def get_orderbook(self, contract_id: str):
        raise UnsupportedFeatureError(
            self.market_id,
            "orderbook_reading",
            "Hypermind publishes historical exports but no supported public orderbook API.",
        )

    def list_trades(
        self,
        contract_id: str,
        *,
        limit: int = 50,
        before: Optional[float] = None,
        after: Optional[float] = None,
    ) -> List[MarketTrade]:
        self.ensure_capability("trade_history")
        market_id, outcome = self._split_contract_id(contract_id)
        desired = self._limit(limit, "Hypermind trade limit", maximum=500)
        lower = self._timestamp_bound(after, "after") if after is not None else None
        upper = self._timestamp_bound(before, "before") if before is not None else None
        if lower is not None and upper is not None and lower > upper:
            raise MarketConfigurationError("Hypermind trade history requires after <= before.")
        rows = [
            row
            for row in self._iter_price_rows()
            if row["market_id"] == market_id
            and row["outcome"] == outcome
            and (lower is None or row["timestamp"] >= lower)
            and (upper is None or row["timestamp"] <= upper)
        ]
        rows.sort(key=lambda row: row["timestamp"], reverse=True)
        canonical = self._contract_id(market_id, outcome)
        return [
            MarketTrade(
                market_id=self.market_id,
                contract_id=canonical,
                trade_id=f"hypermind:{market_id}:{outcome}:{int(row['timestamp'] * 1000)}:{index}",
                side="TRADE",
                price=row["price"],
                size=row["quantity"],
                timestamp=row["timestamp"],
                raw={"archive_only": True, "source": "hypermind_trade_export", "row": dict(row["raw"])},
            )
            for index, row in enumerate(rows[:desired])
        ]

    def list_candles(
        self,
        contract_id: str,
        *,
        resolution: str = "raw",
        from_timestamp: Optional[float] = None,
        to_timestamp: Optional[float] = None,
    ) -> List[MarketCandle]:
        self.ensure_capability("candle_history")
        requested = str(resolution or "raw").strip().lower()
        if requested not in {"raw", "trade", "1h", "hour", "1d", "day", "daily"}:
            raise MarketConfigurationError(
                "Hypermind history accepts raw, trade, 1h, or 1d; the official export is not resampled."
            )
        market_id, outcome = self._split_contract_id(contract_id)
        lower = self._timestamp_bound(from_timestamp, "from_timestamp") if from_timestamp is not None else None
        upper = self._timestamp_bound(to_timestamp, "to_timestamp") if to_timestamp is not None else None
        if lower is not None and upper is not None and lower > upper:
            raise MarketConfigurationError("Hypermind history requires from_timestamp <= to_timestamp.")
        rows = [
            row
            for row in self._iter_price_rows()
            if row["market_id"] == market_id
            and row["outcome"] == outcome
            and (lower is None or row["timestamp"] >= lower)
            and (upper is None or row["timestamp"] <= upper)
        ]
        rows.sort(key=lambda row: row["timestamp"])
        canonical = self._contract_id(market_id, outcome)
        return [
            MarketCandle(
                market_id=self.market_id,
                contract_id=canonical,
                timestamp=row["timestamp"],
                open=row["price"],
                high=row["price"],
                low=row["price"],
                close=row["price"],
                volume=row["quantity"],
                raw={
                    "archive_only": True,
                    "derived": True,
                    "source": "hypermind_trade_export",
                    "row": dict(row["raw"]),
                },
            )
            for row in rows
        ]

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        self.ensure_order_market(order)
        market_id, outcome = self._split_contract_id(order.contract_id)
        side = str(order.side or "").strip().upper()
        if side not in self.live_order_sides:
            raise MarketConfigurationError("Hypermind paper order side must be BUY or SELL.")
        size = self._positive_number(order.size, "Hypermind paper order size")
        price = self._bounded_probability(order.limit_price)
        if order.limit_price not in (None, "") and price is None:
            raise MarketConfigurationError("Hypermind paper order limit_price must be between 0 and 1.")
        if price is None:
            price = self.get_price(self._contract_id(market_id, outcome)).last
        if price is None:
            raise MarketHTTPError(f"Hypermind export contains no price for {market_id}:{outcome}.")
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=self._contract_id(market_id, outcome),
            accepted=True,
            message=(
                f"DRY RUN: would place Hypermind archive {side} for {size:g} {outcome} at {price:.4f}; "
                "the official export is historical and read-only"
            ),
            filled_size=0.0,
            average_price=price,
            raw={"dry_run": True, "archive_only": True, "official_api_is_read_only": True},
        )

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        raise UnsupportedFeatureError(
            self.market_id,
            "live_trading",
            "Hypermind's documented exports are read-only and do not support live order submission.",
        )

    def copy_trade_from_activity(self, activity: Mapping[str, Any]) -> PaperOrderResult:
        raise UnsupportedFeatureError(
            self.market_id,
            "copy_trading",
            "Hypermind's historical export does not publish a supported account-activity feed for copy trading.",
        )

    @property
    def _report_url(self) -> str:
        return HYPERMIND_REFERENCES[0]

    def _iter_price_rows(self) -> Iterable[Dict[str, Any]]:
        text = self._get_text(self.prices_url, "price export")
        reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
        fields = tuple(str(field or "").strip().lower() for field in (reader.fieldnames or ()))
        if fields != _PRICE_FIELDS:
            raise MarketHTTPError(
                "Hypermind price export header must be timestamp,ifpid,outcome,price,qty."
            )
        count = 0
        maximum = self._limit(self.config.get("hypermind_max_rows", 1_500_000), "Hypermind row limit", maximum=5_000_000)
        for raw in reader:
            count += 1
            if count > maximum:
                raise MarketHTTPError(f"Hypermind price export exceeds configured safety limit of {maximum} rows.")
            try:
                market_id = self._safe_id(raw.get("ifpid"), "Hypermind market id")
                outcome = self._safe_outcome(raw.get("outcome"))
                timestamp = self._parse_timestamp(raw.get("timestamp"))
                source_price = float(str(raw.get("price") or "").strip())
                quantity = float(str(raw.get("qty") or "").strip())
            except (TypeError, ValueError, OverflowError, MarketConfigurationError):
                continue
            if not math.isfinite(source_price) or not 0 <= source_price <= 100:
                continue
            if not math.isfinite(quantity) or quantity <= 0:
                continue
            yield {
                "market_id": market_id,
                "outcome": outcome,
                "timestamp": timestamp,
                "price": source_price / 100.0,
                "quantity": quantity,
                "raw": {
                    "timestamp": raw.get("timestamp"),
                    "ifpid": raw.get("ifpid"),
                    "outcome": raw.get("outcome"),
                    "price": raw.get("price"),
                    "qty": raw.get("qty"),
                },
            }

    def _load_outcomes(self) -> Dict[str, Tuple[str, ...]]:
        text = self._get_text(self.outcomes_url, "outcomes export")
        result: Dict[str, Tuple[str, ...]] = {}
        for line_number, line in enumerate(str(text or "").splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            market_raw, separator, payload = stripped.partition(" ")
            if not separator:
                raise MarketHTTPError(f"Hypermind outcomes export line {line_number} is malformed.")
            try:
                market_id = self._safe_id(market_raw, "Hypermind market id")
                values = json.loads(payload.strip())
            except (json.JSONDecodeError, MarketConfigurationError) as exc:
                raise MarketHTTPError(f"Hypermind outcomes export line {line_number} is malformed.") from exc
            if not isinstance(values, list) or not values:
                raise MarketHTTPError(f"Hypermind outcomes export line {line_number} must contain a non-empty list.")
            normalized = tuple(sorted({self._safe_outcome(value) for value in values}))
            result[market_id] = normalized
        return result

    def _get_text(self, url: str, label: str) -> str:
        return self.runtime.request_text(
            "GET",
            url,
            headers={"Accept": "text/csv,text/plain", "User-Agent": "market-sentinel/1.0"},
            error_context=f"Hypermind {label}",
        )

    def _validated_url(self, value: Any, label: str) -> str:
        url = str(value or "").strip()
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
            raise MarketConfigurationError(f"Hypermind {label} URL must be an absolute HTTPS URL without query or fragment.")
        if parsed.hostname not in _ALLOWED_HOSTS and not self.config_bool("hypermind_allow_custom_data_host", False):
            raise MarketConfigurationError(
                "Hypermind export URLs must use predict.hypermind.com unless "
                "hypermind_allow_custom_data_host is explicitly enabled for a reviewed mirror or test."
            )
        return url

    @staticmethod
    def _event_id(market_id: str) -> str:
        return f"archive:{market_id}"

    @classmethod
    def _market_from_event_id(cls, event_id: str) -> str:
        value = str(event_id or "").strip()
        if not value.startswith("archive:"):
            raise MarketConfigurationError("Hypermind event id must use the archive:<market_id> format.")
        return cls._safe_id(value.split(":", 1)[1], "Hypermind market id")

    @staticmethod
    def _contract_id(market_id: str, outcome: str) -> str:
        return f"{market_id}:{outcome}"

    @classmethod
    def _split_contract_id(cls, contract_id: str) -> Tuple[str, str]:
        value = str(contract_id or "").strip()
        if value.count(":") != 1:
            raise MarketConfigurationError("Hypermind contract id must use the market_id:outcome format.")
        market_id, outcome = value.split(":", 1)
        return cls._safe_id(market_id, "Hypermind market id"), cls._safe_outcome(outcome)

    @staticmethod
    def _safe_id(value: Any, label: str) -> str:
        clean = str(value or "").strip().strip('"')
        if not _ID_RE.fullmatch(clean):
            raise MarketConfigurationError(f"{label} must be a safe non-empty identifier.")
        return clean

    @staticmethod
    def _safe_outcome(value: Any) -> str:
        clean = str(value or "").strip().strip('"')
        if not _OUTCOME_RE.fullmatch(clean):
            raise MarketConfigurationError("Hypermind outcomes must be safe non-empty labels.")
        return clean

    @staticmethod
    def _parse_timestamp(value: Any) -> float:
        raw = str(value or "").strip()
        if not raw:
            raise ValueError("empty timestamp")
        try:
            numeric = float(raw)
            if math.isfinite(numeric):
                return numeric
        except (TypeError, ValueError):
            pass
        normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()

    @staticmethod
    def _limit(value: Any, label: str, *, maximum: int) -> int:
        try:
            desired = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"{label} must be an integer between 1 and {maximum}.") from exc
        if desired < 1 or desired > maximum:
            raise MarketConfigurationError(f"{label} must be between 1 and {maximum}.")
        return desired

    @staticmethod
    def _timestamp_bound(value: Any, label: str) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"Hypermind {label} timestamp must be numeric.") from exc
        if not math.isfinite(number):
            raise MarketConfigurationError(f"Hypermind {label} timestamp must be finite.")
        return number

    @staticmethod
    def _positive_number(value: Any, label: str) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"{label} must be numeric.") from exc
        if not math.isfinite(number) or number <= 0:
            raise MarketConfigurationError(f"{label} must be positive and finite.")
        return number

    @staticmethod
    def _bounded_probability(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number) or number < 0 or number > 1:
            return None
        return number
