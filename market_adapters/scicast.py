from __future__ import annotations

import datetime as _dt
import math
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from .base import MarketAdapter
from .catalog import get_market_metadata
from .errors import MarketConfigurationError, MarketHTTPError, UnsupportedFeatureError
from .types import MarketCandle, MarketContract, MarketEvent, MarketTrade, PaperOrderRequest, PaperOrderResult, PriceSnapshot


DEFAULT_SCICAST_BASE_URL = "https://datamart.scicast.org"
SCICAST_REFERENCES = (
    "https://scicast.wordpress.com/wp-content/uploads/2014/10/scicast_datamart_guide_v1-21.pdf",
    "https://scicast.org/blog_subdomain/download/complete-scicast-data-package/",
    "https://scicast.org/blog_subdomain/mode_grid/",
)
_SCICAST_QUERY_TYPES = ("comment", "person", "person/leaderboard", "question", "question_history", "trade_history")
_SCICAST_HOSTS = {"datamart.scicast.org"}
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class SciCastAdapter(MarketAdapter):
    """Archive-only adapter for SciCast's documented historical Data Mart.

    SciCast documented a credentialed JSON Data Mart with question,
    question-history, and trade-history queries, and later published a complete
    historical package after the service shut down.  This adapter keeps those
    documented reads and local paper previews available without pretending the
    retired service is a live execution venue.  It never scrapes the old web
    UI, submits orders, or copies account activity.
    """

    metadata = get_market_metadata("scicast")
    live_order_sides = ("BUY", "SELL")

    def __init__(self, config: Optional[Mapping[str, Any]] = None, *, runtime=None) -> None:
        super().__init__(config, runtime=runtime)
        self._question_cache: Dict[str, Dict[str, Any]] = {}

    @property
    def api_base_url(self) -> str:
        configured = self.config.get("scicast_api_base_url") or self.config.get("api_base_url")
        base = str(configured or DEFAULT_SCICAST_BASE_URL).strip().rstrip("/")
        parsed = urlsplit(base)
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise MarketConfigurationError(
                "SciCast Data Mart base URL must be an absolute HTTPS origin without a path, query, or fragment."
            )
        if parsed.hostname not in _SCICAST_HOSTS and not self.config_bool("scicast_allow_custom_base_url", False):
            raise MarketConfigurationError(
                "SciCast Data Mart base URL must use datamart.scicast.org unless "
                "scicast_allow_custom_base_url is explicitly enabled for a reviewed archive mirror or test."
            )
        return base

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        credential = self.resolve_credential("scicast_api_key", ("SCICAST_API_KEY",), label="SCICAST_API_KEY")
        health.update(
            {
                "api_base_url": self.api_base_url,
                "references": list(SCICAST_REFERENCES),
                "credential_sources": [{"name": credential.name, "source": credential.source}] if credential else [],
                "public_api": False,
                "official_archive": True,
                "archive_only": True,
                "query_types": list(_SCICAST_QUERY_TYPES),
                "dynamic_discovery": True,
                "orderbook_supported": False,
                "live_trading_supported": False,
                "live_trading_enabled": False,
                "copy_trading_supported": False,
                "service_status": "retired historical Data Mart; use only with an approved archive credential/mirror",
            }
        )
        return health

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = self._limit(limit, "SciCast event limit", maximum=100)
        rows = self._query_rows("question", params={"limit": desired})
        needle = str(query or "").strip().lower()
        events: List[MarketEvent] = []
        for row in rows:
            question = self._question_mapping(row)
            question_id = self._question_id(question)
            if not question_id:
                continue
            self._question_cache[question_id] = dict(question)
            title = self._question_title(question, question_id)
            search_text = " ".join(
                str(question.get(key) or "") for key in ("question_id", "id", "name", "question", "question_text", "title", "category")
            ).lower()
            if needle and needle not in search_text:
                continue
            events.append(
                MarketEvent(
                    market_id=self.market_id,
                    event_id=self._event_id(question_id),
                    title=title,
                    url="https://scicast.org",
                    status=self._status(question),
                    raw={"question": dict(question), "archive_only": True},
                )
            )
            if len(events) >= desired:
                break
        return events

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        question_id = self._question_from_event_id(event_id)
        question = self._question_cache.get(question_id) or self._find_question(question_id)
        if question is None:
            return []
        title = self._question_title(question, question_id)
        status = self._status(question)
        choices = self._choices(question)
        contracts: List[MarketContract] = []
        if choices:
            for index, label in enumerate(choices):
                outcome = "YES" if len(choices) == 2 and index == 0 and label.lower() in {"yes", "true"} else "NO" if len(choices) == 2 and index == 1 and label.lower() in {"no", "false"} else f"CHOICE:{index}"
                contract_id = self._contract_id(question_id, outcome)
                contracts.append(
                    MarketContract(
                        market_id=self.market_id,
                        contract_id=contract_id,
                        event_id=self._event_id(question_id),
                        title=f"{title} - {label}",
                        outcome=label,
                        url="https://scicast.org",
                        status=status,
                        raw={"question": dict(question), "choice_index": index, "choice": label},
                    )
                )
        else:
            contract_id = self._contract_id(question_id, "VALUE")
            contracts.append(
                MarketContract(
                    market_id=self.market_id,
                    contract_id=contract_id,
                    event_id=self._event_id(question_id),
                    title=title,
                    outcome="VALUE",
                    url="https://scicast.org",
                    status=status,
                    raw={"question": dict(question), "numeric": True},
                )
            )
        return contracts

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        question_id, outcome = self._split_contract_id(contract_id)
        rows = self._history_rows(question_id)
        value = self._latest_value(rows, outcome)
        if value is None:
            raise MarketHTTPError(f"SciCast history contains no bounded value for {question_id}:{outcome}.")
        canonical = self._contract_id(question_id, outcome)
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=canonical,
            last=value,
            bid=None,
            ask=None,
            midpoint=value,
            source="scicast_datamart_history",
            raw={"question_id": question_id, "outcome": outcome, "history_rows": len(rows), "archive_only": True},
        )

    def get_orderbook(self, contract_id: str):
        raise UnsupportedFeatureError(
            self.market_id,
            "orderbook_reading",
            "SciCast's documented Data Mart exposes historical snapshots, not a supported orderbook feed.",
        )

    def list_candles(
        self,
        contract_id: str,
        *,
        resolution: str = "raw",
        from_timestamp: Optional[float] = None,
        to_timestamp: Optional[float] = None,
    ) -> List[MarketCandle]:
        self.ensure_capability("price_reading")
        question_id, outcome = self._split_contract_id(contract_id)
        clean_resolution = str(resolution or "").strip().lower()
        if clean_resolution not in {"raw", "forecast", "1h", "hour", "1d", "day", "daily"}:
            raise MarketConfigurationError("SciCast history resolution must be raw, forecast, 1h, or 1d; no resampling is fabricated.")
        start = self._timestamp_bound(from_timestamp, "from_timestamp") if from_timestamp is not None else None
        end = self._timestamp_bound(to_timestamp, "to_timestamp") if to_timestamp is not None else None
        if start is not None and end is not None and start > end:
            raise MarketConfigurationError("SciCast history requires from_timestamp <= to_timestamp.")
        canonical = self._contract_id(question_id, outcome)
        candles: List[MarketCandle] = []
        for row in self._history_rows(question_id):
            timestamp = self._row_timestamp(row)
            value = self._value_from_row(row, outcome)
            if timestamp is None or value is None:
                continue
            if start is not None and timestamp < start:
                continue
            if end is not None and timestamp > end:
                continue
            candles.append(
                MarketCandle(
                    market_id=self.market_id,
                    contract_id=canonical,
                    timestamp=timestamp,
                    open=value,
                    high=value,
                    low=value,
                    close=value,
                    volume=None,
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
        question_id, outcome = self._split_contract_id(contract_id)
        desired = self._limit(limit, "SciCast trade limit", maximum=500)
        before_ts = self._timestamp_bound(before, "before") if before is not None else None
        after_ts = self._timestamp_bound(after, "after") if after is not None else None
        if before_ts is not None and after_ts is not None and before_ts < after_ts:
            raise MarketConfigurationError("SciCast trade history requires before >= after.")
        rows = self._query_rows("trade_history", params={"question_id": question_id, "limit": desired})
        canonical = self._contract_id(question_id, outcome)
        desired_index = self._outcome_index(outcome)
        trades: List[MarketTrade] = []
        for row in rows:
            row_question = self._question_id(row) or str(row.get("question_id") or row.get("questionId") or "").strip()
            if row_question and row_question != question_id:
                continue
            index = self._row_choice_index(row)
            if desired_index is not None and index is not None and index != desired_index:
                continue
            timestamp = self._row_timestamp(row)
            if timestamp is None or (before_ts is not None and timestamp > before_ts) or (after_ts is not None and timestamp < after_ts):
                continue
            price = self._trade_price(row, index)
            size = self._trade_size(row, index)
            side = self._trade_side(row, index)
            trade_id = str(row.get("trade_id") or row.get("tradeId") or row.get("id") or "").strip()
            if not trade_id or price is None or size is None or size <= 0 or side not in {"BUY", "SELL"}:
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
                    raw=dict(row),
                )
            )
            if len(trades) >= desired:
                break
        return trades

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        self.ensure_order_market(order)
        question_id, outcome = self._split_contract_id(order.contract_id)
        side = str(order.side or "").strip().upper()
        if side not in self.live_order_sides:
            raise MarketConfigurationError("SciCast paper order side must be BUY or SELL.")
        size = self._positive_number(order.size, "SciCast paper order size")
        price = self._bounded_probability(order.limit_price)
        if order.limit_price not in (None, "") and price is None:
            raise MarketConfigurationError("SciCast paper order limit_price must be between 0 and 1.")
        if price is None:
            price = self.get_price(self._contract_id(question_id, outcome)).last
        if price is None:
            raise MarketHTTPError(f"SciCast archive contains no price for {question_id}:{outcome}.")
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=self._contract_id(question_id, outcome),
            accepted=True,
            message=(f"DRY RUN: would place SciCast archive {side} for {size:g} {outcome} at {price:.4f}; "
                     "SciCast is a retired historical data service, not a live order venue"),
            average_price=price,
            raw={"dry_run": True, "archive_only": True, "official_api_is_read_only": True},
        )

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        raise UnsupportedFeatureError(
            self.market_id,
            "live_trading",
            "SciCast shut down after its historical data package; no live order API is supported.",
        )

    def copy_trade_from_activity(self, activity: Mapping[str, Any]) -> PaperOrderResult:
        raise UnsupportedFeatureError(
            self.market_id,
            "copy_trading",
            "SciCast does not expose a supported current account-activity feed for copy trading.",
        )

    def _query_rows(self, query_type: str, *, params: Optional[Mapping[str, Any]] = None) -> List[Dict[str, Any]]:
        payload = self._query(query_type, params=params)
        rows: Any = payload
        if isinstance(payload, Mapping):
            for key in ("results", "data", "questions", "history", "trades", "items", "objects"):
                candidate = payload.get(key)
                if isinstance(candidate, list):
                    rows = candidate
                    break
        if not isinstance(rows, list):
            return []
        return [dict(row) for row in rows if isinstance(row, Mapping)]

    def _query(self, query_type: str, *, params: Optional[Mapping[str, Any]] = None) -> Any:
        clean_type = str(query_type or "").strip().strip("/")
        if clean_type not in _SCICAST_QUERY_TYPES:
            raise MarketConfigurationError(f"Unsupported SciCast Data Mart query type: {query_type!r}.")
        credential = self.resolve_credential(
            "scicast_api_key", ("SCICAST_API_KEY",), required=True, label="SCICAST_API_KEY"
        )
        query_params: Dict[str, Any] = {"format": "json", "api_key": credential.value}
        query_params.update(dict(params or {}))
        return self.runtime.get_json(f"{self.api_base_url}/{clean_type}/", params=query_params, headers={})

    def _find_question(self, question_id: str) -> Optional[Dict[str, Any]]:
        for row in self._query_rows("question", params={"question_id": question_id}):
            question = self._question_mapping(row)
            if self._question_id(question) == question_id:
                self._question_cache[question_id] = dict(question)
                return dict(question)
        return None

    def _history_rows(self, question_id: str) -> List[Dict[str, Any]]:
        return self._query_rows("question_history", params={"question_id": question_id})

    @staticmethod
    def _question_mapping(row: Mapping[str, Any]) -> Dict[str, Any]:
        nested = row.get("question")
        return dict(nested) if isinstance(nested, Mapping) else dict(row)

    @staticmethod
    def _question_id(row: Mapping[str, Any]) -> str:
        value = row.get("question_id") or row.get("questionId") or row.get("id")
        clean = str(value or "").strip()
        return clean if _ID_RE.fullmatch(clean) else ""

    @staticmethod
    def _question_title(row: Mapping[str, Any], fallback: str) -> str:
        for key in ("name", "question", "question_text", "title", "text"):
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return fallback

    @staticmethod
    def _status(row: Mapping[str, Any]) -> str:
        raw = str(row.get("status") or "").strip().lower()
        if raw in {"resolved", "closed", "locked", "open", "active"}:
            return raw
        return "archived"

    @staticmethod
    def _choices(row: Mapping[str, Any]) -> List[str]:
        raw = row.get("choices") or row.get("choice_list") or row.get("options")
        if isinstance(raw, Mapping):
            raw = list(raw.values())
        if not isinstance(raw, (list, tuple)):
            question_type = str(row.get("type") or row.get("question_type") or "").lower()
            return ["Yes", "No"] if question_type in {"binary", "bool", "boolean"} else []
        result: List[str] = []
        for value in raw:
            if isinstance(value, Mapping):
                value = value.get("name") or value.get("label") or value.get("text") or value.get("value")
            clean = str(value or "").strip()
            if clean:
                result.append(clean)
        return result

    def _event_id(self, question_id: str) -> str:
        return f"question:{question_id}"

    def _question_from_event_id(self, event_id: str) -> str:
        value = str(event_id or "").strip()
        if not value.startswith("question:"):
            raise MarketConfigurationError("SciCast event id must use the question:<question_id> format.")
        question_id = value.split(":", 1)[1]
        if not _ID_RE.fullmatch(question_id):
            raise MarketConfigurationError("SciCast event id contains an unsafe question id.")
        return question_id

    @staticmethod
    def _contract_id(question_id: str, outcome: str) -> str:
        return f"{question_id}:{outcome}"

    def _split_contract_id(self, contract_id: str) -> Tuple[str, str]:
        value = str(contract_id or "").strip()
        parts = value.split(":")
        if len(parts) not in {2, 3} or not _ID_RE.fullmatch(parts[0]):
            raise MarketConfigurationError("SciCast contract id must use question_id:YES, :NO, :CHOICE:index, or :VALUE.")
        outcome = ":".join(parts[1:])
        if outcome not in {"YES", "NO", "VALUE"} and not re.fullmatch(r"CHOICE:[0-9]{1,4}", outcome):
            raise MarketConfigurationError("SciCast contract id contains an unsupported outcome.")
        return parts[0], outcome

    @staticmethod
    def _outcome_index(outcome: str) -> Optional[int]:
        if outcome == "YES":
            return 0
        if outcome == "NO":
            return 1
        if outcome.startswith("CHOICE:"):
            try:
                return int(outcome.split(":", 1)[1])
            except ValueError:
                return None
        return None

    def _value_from_row(self, row: Mapping[str, Any], outcome: str) -> Optional[float]:
        index = self._outcome_index(outcome)
        values = None
        for key in ("probabilities", "probability_array", "probabilityArray", "marginals", "values", "probability"):
            if key in row:
                values = row.get(key)
                break
        if isinstance(values, Mapping):
            if outcome in values:
                return self._bounded_probability(values.get(outcome))
            if index is not None:
                for key in (str(index), index):
                    if key in values:
                        return self._bounded_probability(values.get(key))
            values = list(values.values())
        if isinstance(values, (list, tuple)):
            if index is None or index >= len(values):
                return None
            value = self._bounded_probability(values[index])
            return value
        if outcome == "NO" and values is not None:
            yes = self._bounded_probability(values)
            return None if yes is None else 1.0 - yes
        if outcome == "VALUE":
            for key in ("value", "median", "prediction", "forecast"):
                value = self._number(row.get(key))
                if value is not None:
                    return value
        return self._bounded_probability(values)

    def _latest_value(self, rows: Sequence[Mapping[str, Any]], outcome: str) -> Optional[float]:
        ranked = sorted(rows, key=lambda row: self._row_timestamp(row) or float("-inf"))
        for row in reversed(ranked):
            value = self._value_from_row(row, outcome)
            if value is not None:
                return value
        return None

    @staticmethod
    def _row_choice_index(row: Mapping[str, Any]) -> Optional[int]:
        raw = row.get("choice_index")
        if raw is None:
            raw = row.get("choiceIndex") or row.get("option_index") or row.get("choice")
        if isinstance(raw, Mapping):
            raw = raw.get("index") or raw.get("id")
        try:
            index = int(raw)
        except (TypeError, ValueError):
            return None
        return index if index >= 0 else None

    def _trade_price(self, row: Mapping[str, Any], index: Optional[int]) -> Optional[float]:
        for key in ("price", "probability", "new_probability", "newProbability"):
            value = self._bounded_probability(row.get(key))
            if value is not None:
                return value
        for key in ("new_value_list", "newValueList", "probabilities", "probability_array"):
            values = row.get(key)
            if isinstance(values, (list, tuple)) and index is not None and index < len(values):
                value = self._bounded_probability(values[index])
                if value is not None:
                    return value
        return None

    @staticmethod
    def _trade_size(row: Mapping[str, Any], index: Optional[int]) -> Optional[float]:
        for key in ("size", "shares", "quantity", "amount"):
            value = SciCastAdapter._number(row.get(key))
            if value is not None and value > 0:
                return value
        values = row.get("assets_per_option") or row.get("assetsPerOption")
        if isinstance(values, (list, tuple)) and index is not None and index < len(values):
            value = SciCastAdapter._number(values[index])
            if value is not None:
                return abs(value)
        return None

    @staticmethod
    def _trade_side(row: Mapping[str, Any], index: Optional[int]) -> str:
        side = str(row.get("side") or row.get("trade_side") or row.get("action") or "").strip().upper()
        if side in {"BUY", "SELL"}:
            return side
        values = row.get("assets_per_option") or row.get("assetsPerOption")
        if isinstance(values, (list, tuple)) and index is not None and index < len(values):
            try:
                return "BUY" if float(values[index]) > 0 else "SELL"
            except (TypeError, ValueError):
                pass
        return ""

    @staticmethod
    def _row_timestamp(row: Mapping[str, Any]) -> Optional[float]:
        for key in ("sampled_at", "traded_at", "timestamp", "time", "date", "created_at"):
            value = row.get(key)
            parsed = SciCastAdapter._timestamp(value)
            if parsed is not None:
                return parsed
        return None

    @staticmethod
    def _timestamp(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            number = float(value)
            return number if math.isfinite(number) else None
        text = str(value).strip()
        try:
            number = float(text)
            return number if math.isfinite(number) else None
        except ValueError:
            pass
        try:
            parsed = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            for pattern in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
                try:
                    parsed = _dt.datetime.strptime(text, pattern)
                    break
                except ValueError:
                    parsed = None
            if parsed is None:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_dt.timezone.utc)
        return parsed.timestamp()

    @staticmethod
    def _timestamp_bound(value: Any, label: str) -> float:
        parsed = SciCastAdapter._timestamp(value)
        if parsed is None:
            raise MarketConfigurationError(f"SciCast {label} timestamp must be finite numeric or ISO date-time.")
        return parsed

    @staticmethod
    def _number(value: Any) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    @staticmethod
    def _bounded_probability(value: Any) -> Optional[float]:
        number = SciCastAdapter._number(value)
        if number is None or number < 0 or number > 1:
            return None
        return number

    @staticmethod
    def _positive_number(value: Any, label: str) -> float:
        number = SciCastAdapter._number(value)
        if number is None or number <= 0:
            raise MarketConfigurationError(f"{label} must be positive and finite.")
        return number

    @staticmethod
    def _limit(value: Any, label: str, *, maximum: int) -> int:
        try:
            desired = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"{label} must be an integer between 1 and {maximum}.") from exc
        if desired < 1 or desired > maximum:
            raise MarketConfigurationError(f"{label} must be between 1 and {maximum}.")
        return desired


__all__ = ["DEFAULT_SCICAST_BASE_URL", "SCICAST_REFERENCES", "SciCastAdapter"]
