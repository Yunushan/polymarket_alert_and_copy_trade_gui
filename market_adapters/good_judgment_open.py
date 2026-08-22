from __future__ import annotations

"""Good Judgment Open adapter over the documented Cultivate Forecasts API.

Good Judgment Open is a forecasting platform rather than an exchange.  The
adapter therefore exposes questions, answer probabilities, and irregular
forecast snapshots as point candles.  Forecast submission is a guarded,
credentialed operation; it is never enabled by default and does not pretend
that forecasts are exchange fills.
"""

from datetime import datetime, timezone
import math
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .base import MarketAdapter
from .catalog import get_market_metadata
from .errors import MarketConfigurationError, UnsupportedFeatureError
from .types import MarketCandle, MarketContract, MarketEvent, PaperOrderRequest, PaperOrderResult, PriceSnapshot


DEFAULT_GOOD_JUDGMENT_OPEN_BASE_URL = "https://www.gjopen.com"


class GoodJudgmentOpenAdapter(MarketAdapter):
    """Credentialed Cultivate Forecasts API adapter for Good Judgment Open."""

    metadata = get_market_metadata("good_judgment_open")
    live_order_sides = ("BUY",)

    def __init__(self, config: Optional[Mapping[str, Any]] = None, *, runtime=None) -> None:
        super().__init__(config, runtime=runtime)
        self._oauth_access_token = ""

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        token = self.resolve_credential(
            "good_judgment_open_api_token",
            ("GJOPEN_API_TOKEN",),
            label="GJOPEN_API_TOKEN",
        )
        email = self.resolve_credential(
            "good_judgment_open_email",
            ("GJOPEN_EMAIL",),
            label="GJOPEN_EMAIL",
        )
        password = self.resolve_credential(
            "good_judgment_open_password",
            ("GJOPEN_PASSWORD",),
            label="GJOPEN_PASSWORD",
        )
        health.update(
            {
                "api_base_url": self.api_base_url,
                "credential_sources": [
                    {"name": credential.name, "source": credential.source}
                    for credential in (token, email, password)
                    if credential
                ],
                "authentication_mode": (
                    "bearer_token"
                    if token
                    else "oauth_password"
                    if email and password
                    else "not_configured"
                ),
                "forecast_history_supported": True,
                "forecast_submission_supported": True,
                "orderbook_supported": False,
                "trading_semantics": "forecast_submission_not_exchange_execution",
                "data_access_note": (
                    "Uses the documented Cultivate Forecasts REST contract; the Good Judgment Open instance URL "
                    "and account eligibility must be validated by the operator."
                ),
                "live_trading_enabled": self.config_bool("live_trading_enabled", False),
            }
        )
        return health

    @property
    def api_base_url(self) -> str:
        configured = (
            self.config.get("good_judgment_open_base_url")
            or self.config.get("good_judgment_open_api_base_url")
            or self.config.get("api_base_url")
        )
        return str(configured or DEFAULT_GOOD_JUDGMENT_OPEN_BASE_URL).rstrip("/")

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = max(1, min(int(limit or 50), 1000))
        data = self._get(
            "/api/v1/questions",
            params={"page": 1, "status": "active", "sort": "starts_at", "per_page": desired},
        )
        questions = self._question_rows(data)
        needle = str(query or "").strip().lower()
        events: List[MarketEvent] = []
        for question in questions:
            event = self._event_from_question(question)
            if not event.event_id:
                continue
            haystack = " ".join(
                str(question.get(key) or "")
                for key in ("name", "title", "question", "text", "description", "body")
            ).lower()
            if needle and needle not in haystack:
                continue
            events.append(event)
            if len(events) >= desired:
                break
        return events

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        question_id = self._question_id(event_id)
        question = self._get_question(question_id)
        if not question:
            return []
        return self._contracts_from_question(question)

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        question_id, answer_id = self._split_contract_id(contract_id)
        question = self._get_question(question_id)
        answer = self._find_answer(question, answer_id)
        probability = self._answer_probability(answer)
        canonical = self._contract_id(question_id, answer_id)
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=canonical,
            last=probability,
            midpoint=probability,
            source="cultivate_forecasts_api",
            raw={"question": dict(question), "answer": dict(answer)},
        )

    def list_candles(
        self,
        contract_id: str,
        *,
        resolution: str = "1h",
        from_timestamp: Optional[float] = None,
        to_timestamp: Optional[float] = None,
    ) -> List[MarketCandle]:
        """Normalize documented prediction-set submissions as point candles.

        Cultivate returns irregular forecast submissions, not OHLCV bars.  The
        normalized candle keeps all four price fields equal and leaves volume
        unset; no resampling or synthetic exchange volume is claimed.
        """

        self.ensure_capability("candle_history")
        requested = str(resolution or "1h").strip().lower()
        if requested not in {"raw", "forecast", "1h", "1d"}:
            raise MarketConfigurationError(
                "Good Judgment Open forecast history accepts resolution 'raw', 'forecast', '1h', or '1d'; "
                "the upstream submissions are irregular and are not resampled."
            )
        lower = self._optional_timestamp(from_timestamp, "from_timestamp")
        upper = self._optional_timestamp(to_timestamp, "to_timestamp")
        if lower is not None and upper is not None and upper < lower:
            raise MarketConfigurationError("Good Judgment Open to_timestamp must not precede from_timestamp.")

        question_id, answer_id = self._split_contract_id(contract_id)
        data = self._get(
            "/api/v1/prediction_sets",
            params={"question_id": question_id, "page": 1, "per_page": 1000},
        )
        rows = self._prediction_set_rows(data)
        candles: List[MarketCandle] = []
        canonical = self._contract_id(question_id, answer_id)
        for prediction_set in rows:
            set_timestamp = self._timestamp(
                prediction_set.get("updated_at")
                or prediction_set.get("submitted_at")
                or prediction_set.get("created_at")
                or prediction_set.get("timestamp")
            )
            predictions = prediction_set.get("predictions") or prediction_set.get("prediction_values") or []
            if not isinstance(predictions, list):
                continue
            for prediction in predictions:
                if not isinstance(prediction, Mapping):
                    continue
                row_answer = self._answer_ref(prediction)
                if row_answer != answer_id:
                    continue
                probability = self._answer_probability(prediction)
                timestamp = self._timestamp(
                    prediction.get("updated_at")
                    or prediction.get("submitted_at")
                    or prediction.get("created_at")
                    or prediction.get("timestamp")
                )
                timestamp = timestamp if timestamp is not None else set_timestamp
                if timestamp is None:
                    continue
                if (lower is not None and timestamp < lower) or (upper is not None and timestamp > upper):
                    continue
                candles.append(
                    MarketCandle(
                        market_id=self.market_id,
                        contract_id=canonical,
                        timestamp=timestamp,
                        open=probability,
                        high=probability,
                        low=probability,
                        close=probability,
                        volume=None,
                        raw={
                            "source": "cultivate_forecasts_api",
                            "resolution_requested": requested,
                            "question_id": question_id,
                            "answer_id": answer_id,
                            "prediction_set": dict(prediction_set),
                            "prediction": dict(prediction),
                        },
                    )
                )
        candles.sort(key=lambda candle: candle.timestamp)
        return candles

    def get_orderbook(self, contract_id: str):
        del contract_id
        raise UnsupportedFeatureError(
            self.market_id,
            "orderbook_reading",
            "Good Judgment Open publishes forecasts, not a traded orderbook.",
        )

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        probability = self._validate_order(order)
        payload, endpoint = self._submission_payload(order, probability)
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=self._canonical_contract_id(order.contract_id),
            accepted=True,
            message=(
                f"DRY RUN: would submit Good Judgment Open forecast probability {probability:.4f} "
                f"for {order.contract_id}"
            ),
            filled_size=0.0,
            average_price=probability,
            raw={"endpoint": endpoint, "request": payload, "semantics": "forecast_submission"},
        )

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        self.ensure_capability("live_trading")
        probability = self._validate_order(order)
        preflight = self.preflight_live_order(order, feature_name="forecast submission")
        payload, endpoint = self._submission_payload(order, probability)
        response = self.runtime.request_json(
            "POST",
            self._url(endpoint),
            json_body=payload,
            headers=self._auth_headers(),
        )
        return {
            "market_id": self.market_id,
            "contract_id": self._canonical_contract_id(order.contract_id),
            "live": True,
            "endpoint": endpoint,
            "preflight": preflight,
            "request": payload,
            "response": response,
            "semantics": "forecast_submission",
        }

    def _get_question(self, question_id: str) -> Mapping[str, Any]:
        data = self._get(
            "/api/v1/questions",
            params={"ids": question_id, "status": "all", "per_page": 1},
        )
        rows = self._question_rows(data)
        return rows[0] if rows else {}

    def _get(self, path: str, *, params: Optional[Mapping[str, Any]] = None) -> Any:
        return self.runtime.get_json(self._url(path), params=params, headers=self._auth_headers())

    def _url(self, path: str) -> str:
        clean_path = "/" + str(path or "").lstrip("/")
        return f"{self.api_base_url}{clean_path}"

    def _auth_headers(self) -> Dict[str, str]:
        token = self.resolve_credential(
            "good_judgment_open_api_token",
            ("GJOPEN_API_TOKEN",),
            label="GJOPEN_API_TOKEN",
        )
        if token:
            return {"Authorization": f"Bearer {token.value}"}
        if self._oauth_access_token:
            return {"Authorization": f"Bearer {self._oauth_access_token}"}

        email = self.resolve_credential(
            "good_judgment_open_email",
            ("GJOPEN_EMAIL",),
            required=True,
            label="GJOPEN_EMAIL",
        )
        password = self.resolve_credential(
            "good_judgment_open_password",
            ("GJOPEN_PASSWORD",),
            required=True,
            label="GJOPEN_PASSWORD",
        )
        response = self.runtime.request_json(
            "POST",
            self._url("/oauth/token"),
            json_body={
                "grant_type": "password",
                "email": email.value,
                "password": password.value,
            },
            headers={"Content-Type": "application/json"},
        )
        if not isinstance(response, Mapping):
            raise MarketConfigurationError("Good Judgment Open OAuth response was not an object.")
        access_token = response.get("access_token") or response.get("token")
        if not access_token:
            raise MarketConfigurationError("Good Judgment Open OAuth response did not include an access token.")
        self._oauth_access_token = str(access_token)
        return {"Authorization": f"Bearer {self._oauth_access_token}"}

    @staticmethod
    def _question_rows(data: Any) -> List[Mapping[str, Any]]:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, Mapping)]
        if isinstance(data, Mapping):
            for key in ("questions", "results", "items", "data"):
                rows = data.get(key)
                if isinstance(rows, list):
                    return [item for item in rows if isinstance(item, Mapping)]
            for key in ("question",):
                item = data.get(key)
                if isinstance(item, Mapping):
                    return [item]
            if data.get("id") is not None:
                return [data]
        return []

    @staticmethod
    def _prediction_set_rows(data: Any) -> List[Mapping[str, Any]]:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, Mapping)]
        if isinstance(data, Mapping):
            for key in ("prediction_sets", "results", "items", "data"):
                rows = data.get(key)
                if isinstance(rows, list):
                    return [item for item in rows if isinstance(item, Mapping)]
            if data.get("id") is not None:
                return [data]
        return []

    def _event_from_question(self, question: Mapping[str, Any]) -> MarketEvent:
        question_id = str(question.get("id") or question.get("question_id") or "").strip()
        return MarketEvent(
            market_id=self.market_id,
            event_id=question_id,
            title=self._question_title(question),
            url=self._question_url(question),
            status=self._question_status(question),
            raw=dict(question),
        )

    def _contracts_from_question(self, question: Mapping[str, Any]) -> List[MarketContract]:
        question_id = self._question_id(question.get("id") or question.get("question_id"))
        title = self._question_title(question)
        status = self._question_status(question)
        contracts: List[MarketContract] = []
        for answer in self._answers(question):
            answer_id = self._answer_ref(answer)
            if not answer_id:
                continue
            contracts.append(
                MarketContract(
                    market_id=self.market_id,
                    contract_id=self._contract_id(question_id, answer_id),
                    event_id=question_id,
                    title=f"{title} - {self._answer_title(answer)}",
                    outcome=self._answer_title(answer),
                    url=self._question_url(question),
                    status=status,
                    raw={"question": dict(question), "answer": dict(answer)},
                )
            )
        return contracts

    @staticmethod
    def _answers(question: Mapping[str, Any]) -> List[Mapping[str, Any]]:
        for key in ("answers", "outcomes", "choices"):
            value = question.get(key)
            if isinstance(value, list):
                return [item if isinstance(item, Mapping) else {"id": item, "name": item} for item in value]
        return []

    @staticmethod
    def _question_title(question: Mapping[str, Any]) -> str:
        return str(
            question.get("name")
            or question.get("title")
            or question.get("question")
            or question.get("text")
            or question.get("description")
            or question.get("id")
            or ""
        ).strip()

    def _question_url(self, question: Mapping[str, Any]) -> str:
        raw = str(question.get("url") or question.get("web_url") or question.get("path") or "").strip()
        if raw.startswith("http://") or raw.startswith("https://"):
            return raw
        if raw:
            return f"{self.api_base_url}{raw if raw.startswith('/') else '/' + raw}"
        question_id = str(question.get("id") or question.get("question_id") or "").strip()
        return f"{self.api_base_url}/questions/{question_id}" if question_id else self.api_base_url

    @staticmethod
    def _question_status(question: Mapping[str, Any]) -> str:
        state = str(question.get("state") or question.get("status") or "active").strip().lower()
        if state in {"closed", "resolved", "complete", "completed"}:
            return "resolved" if state in {"resolved", "complete", "completed"} else "closed"
        return "open"

    @staticmethod
    def _answer_ref(answer: Mapping[str, Any]) -> str:
        return str(answer.get("answer_id") or answer.get("id") or answer.get("uuid") or "").strip()

    @staticmethod
    def _answer_title(answer: Mapping[str, Any]) -> str:
        return str(
            answer.get("name")
            or answer.get("title")
            or answer.get("text")
            or answer.get("answer")
            or answer.get("label")
            or answer.get("id")
            or ""
        ).strip()

    @classmethod
    def _find_answer(cls, question: Mapping[str, Any], answer_id: str) -> Mapping[str, Any]:
        for answer in cls._answers(question):
            if cls._answer_ref(answer) == answer_id:
                return answer
        raise MarketConfigurationError(f"Good Judgment Open question did not include answer {answer_id}.")

    @classmethod
    def _answer_probability(cls, answer: Mapping[str, Any]) -> float:
        for key in (
            "probability",
            "probability_value",
            "forecasted_probability",
            "latest_probability",
            "community_probability",
            "mean_probability",
            "value",
        ):
            value = cls._safe_probability(answer.get(key))
            if value is not None:
                return value
        raise MarketConfigurationError("Good Judgment Open answer did not include a probability between 0 and 1.")

    @classmethod
    def _safe_probability(cls, value: Any) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        if number > 1.0 and number <= 100.0:
            number /= 100.0
        return number if 0.0 <= number <= 1.0 else None

    def _validate_order(self, order: PaperOrderRequest) -> float:
        self.ensure_order_market(order)
        question_id, answer_id = self._split_contract_id(order.contract_id)
        del question_id, answer_id
        if str(order.side or "").strip().upper() != "BUY":
            raise MarketConfigurationError("Good Judgment Open forecast submission only accepts side BUY.")
        try:
            size = float(order.size)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Good Judgment Open forecast size must be numeric.") from exc
        if not math.isfinite(size) or size <= 0:
            raise MarketConfigurationError("Good Judgment Open forecast size must be positive.")
        raw_probability = order.metadata.get("forecast_probability", order.limit_price)
        try:
            probability = float(raw_probability)
        except (TypeError, ValueError):
            probability = math.nan
        if not math.isfinite(probability) or probability < 0.0 or probability > 1.0:
            raise MarketConfigurationError(
                "Good Judgment Open forecast submission requires limit_price or metadata.forecast_probability in [0, 1]."
            )
        return probability

    @classmethod
    def _submission_payload(cls, order: PaperOrderRequest, probability: float) -> Tuple[Dict[str, Any], str]:
        question_id, answer_id = cls._split_contract_id(order.contract_id)
        try:
            numeric_answer_id = int(answer_id)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(
                "Good Judgment Open submission requires the documented numeric answer_id in the contract id."
            ) from exc
        rationale = str(order.metadata.get("rationale") or "").strip()
        prediction: Dict[str, Any] = {
            "forecasted_probability": probability,
            "answer_id": numeric_answer_id,
        }
        prediction_set: Dict[str, Any] = {"predictions_attributes": [prediction]}
        if rationale:
            prediction_set["rationale"] = rationale
        return {"prediction_set": prediction_set}, f"/api/v1/questions/{question_id}/prediction_sets"

    @staticmethod
    def _question_id(value: Any) -> str:
        question_id = str(value or "").strip()
        if not question_id or "/" in question_id or "\\" in question_id or "?" in question_id or "#" in question_id:
            raise MarketConfigurationError("Good Judgment Open question id must be a simple non-empty identifier.")
        return question_id

    @staticmethod
    def _contract_id(question_id: str, answer_id: str) -> str:
        return f"{GoodJudgmentOpenAdapter._question_id(question_id)}:{GoodJudgmentOpenAdapter._question_id(answer_id)}"

    @classmethod
    def _split_contract_id(cls, contract_id: str) -> Tuple[str, str]:
        raw = str(contract_id or "").strip()
        parts = raw.split(":")
        if len(parts) != 2:
            raise MarketConfigurationError("Good Judgment Open contract id must be question_id:answer_id.")
        return cls._question_id(parts[0]), cls._question_id(parts[1])

    @classmethod
    def _canonical_contract_id(cls, contract_id: str) -> str:
        return cls._contract_id(*cls._split_contract_id(contract_id))

    @staticmethod
    def _timestamp(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            number = float(value)
            if math.isfinite(number):
                if number > 100_000_000_000:
                    number /= 1000.0
                return number
        except (TypeError, ValueError):
            pass
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()

    @classmethod
    def _optional_timestamp(cls, value: Optional[float], label: str) -> Optional[float]:
        if value is None:
            return None
        timestamp = cls._timestamp(value)
        if timestamp is None:
            raise MarketConfigurationError(f"Good Judgment Open {label} must be a finite timestamp.")
        return timestamp
