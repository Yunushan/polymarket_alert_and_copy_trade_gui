from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
import math
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .base import MarketAdapter
from .catalog import get_market_metadata
from .errors import MarketConfigurationError, UnsupportedFeatureError
from .types import MarketCandle, MarketContract, MarketEvent, PaperOrderRequest, PaperOrderResult, PriceSnapshot


DEFAULT_METACULUS_BASE_URL = "https://www.metaculus.com/api"
METACULUS_ACCOUNT_OPERATIONS = ("forecast_posts",)


class MetaculusAdapter(MarketAdapter):
    """Metaculus adapter using the official authenticated API.

    Metaculus is a forecasting platform rather than an exchange.  The shared
    order methods are retained as an explicit compatibility envelope: paper
    orders only build a forecast payload locally, while live orders submit the
    documented forecast request behind the normal acknowledgement and kill
    switch gates.  No exchange fill semantics are implied.
    """

    metadata = get_market_metadata("metaculus")
    live_order_sides = ("BUY",)
    account_recovery_operations = METACULUS_ACCOUNT_OPERATIONS

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        credential = self.resolve_credential(
            "metaculus_api_token",
            ("METACULUS_API_TOKEN",),
            label="METACULUS_API_TOKEN",
        )
        health.update(
            {
                "api_base_url": self.api_base_url,
                "credential_sources": (
                    [{"name": credential.name, "source": credential.source}] if credential else []
                ),
                "data_access_note": (
                    "Metaculus API data access requires authentication; Community Prediction data is access-limited."
                ),
                "trading_supported": True,
                "forecast_submission_supported": True,
                "trading_semantics": "forecast_submission_not_exchange_execution",
                "account_recovery_operations": list(self.account_recovery_operations),
                "authenticated_account_endpoints": ["/posts/?forecaster_id=..."],
            }
        )
        return health

    @property
    def api_base_url(self) -> str:
        configured = self.config.get("metaculus_api_base_url") or self.config.get("api_base_url")
        return str(configured or DEFAULT_METACULUS_BASE_URL).rstrip("/")

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = max(1, min(int(limit or 50), 100))
        params: Dict[str, Any] = {"limit": desired}
        if query:
            params["search"] = str(query)
        order_by = self.config.get("metaculus_order_by")
        if order_by:
            params["order_by"] = str(order_by)
        data = self._get("/posts/", params=params)
        posts = self._as_post_list(data)
        return [self._event_from_post(post) for post in posts[:desired]]

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        post = self._get_post(str(event_id or "").strip())
        if not post:
            return []
        post_id = str(post.get("id") or event_id).strip()
        contracts: List[MarketContract] = []
        for question in self._questions_from_post(post):
            contracts.extend(self._contracts_from_question(post_id, post, question))
        return contracts

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        post_id, question_id, outcome, choice_id = self._split_contract_id(contract_id)
        post = self._get_post(post_id)
        question = self._find_question(post, question_id)
        if question is None:
            raise MarketConfigurationError(f"Metaculus post {post_id} did not include question {question_id}.")

        if outcome == "YES":
            value = self._binary_probability(question)
        elif outcome == "NO":
            value = 1.0 - self._binary_probability(question)
        elif outcome == "CHOICE":
            if not choice_id:
                raise MarketConfigurationError("Metaculus choice contract requires a choice id.")
            value = self._choice_probability(question, choice_id)
        elif outcome == "VALUE":
            value = self._numeric_forecast(question)
        else:
            raise MarketConfigurationError("Metaculus contract outcome must be YES, NO, CHOICE, or VALUE.")

        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=self._contract_id(post_id, question_id, outcome, choice_id),
            last=value,
            midpoint=value,
            source="metaculus_api",
            raw={"post": dict(post), "question": dict(question)},
        )

    def list_candles(
        self,
        contract_id: str,
        *,
        resolution: str = "1h",
        from_timestamp: Optional[float] = None,
        to_timestamp: Optional[float] = None,
    ) -> List[MarketCandle]:
        """Return Community Prediction aggregation history as point candles.

        Metaculus is a forecasting platform rather than a traded venue.  Its
        official API exposes irregularly-timed aggregation snapshots, not
        exchange OHLCV bars.  The generic candle shape is therefore used as a
        compatibility envelope: open/high/low/close are the same forecast
        value, volume is intentionally left unset, and the original
        aggregation entry is preserved in ``raw``.  No resampling is claimed.
        """

        self.ensure_capability("price_reading")
        requested_resolution = str(resolution or "1h").strip().lower()
        if requested_resolution not in {"raw", "forecast", "1h", "1d"}:
            raise MarketConfigurationError(
                "Metaculus forecast history accepts resolution 'raw', 'forecast', '1h', or '1d'; "
                "the irregular official snapshots are not resampled."
            )
        lower = self._optional_timestamp(from_timestamp, "from_timestamp")
        upper = self._optional_timestamp(to_timestamp, "to_timestamp")
        if lower is not None and upper is not None and upper < lower:
            raise MarketConfigurationError("Metaculus forecast history to_timestamp must not precede from_timestamp.")

        post_id, question_id, outcome, choice_id = self._split_contract_id(contract_id)
        post = self._get_post(post_id)
        question = self._find_question(post, question_id)
        if question is None:
            raise MarketConfigurationError(f"Metaculus post {post_id} did not include question {question_id}.")

        aggregation_method = str(self.config.get("metaculus_aggregation_method") or "").strip().lower()
        aggregation = self._aggregation_for_history(question, aggregation_method)
        if aggregation is None:
            method_text = aggregation_method or "an accessible aggregation"
            raise MarketConfigurationError(
                f"Metaculus response did not include {method_text} history for question {question_id}. "
                "Community Prediction history is access-limited by the official API."
            )
        history = aggregation.get("history")
        if not isinstance(history, list):
            raise MarketConfigurationError(
                f"Metaculus response did not include an accessible history list for question {question_id}."
            )

        candles: List[MarketCandle] = []
        for entry in history:
            if not isinstance(entry, Mapping):
                continue
            timestamp = self._history_timestamp(entry)
            if timestamp is None or (lower is not None and timestamp < lower) or (upper is not None and timestamp > upper):
                continue
            value = self._history_value(question, outcome, choice_id, entry)
            if value is None:
                continue
            canonical_contract = self._contract_id(post_id, question_id, outcome, choice_id)
            candles.append(
                MarketCandle(
                    market_id=self.market_id,
                    contract_id=canonical_contract,
                    timestamp=timestamp,
                    open=value,
                    high=value,
                    low=value,
                    close=value,
                    volume=None,
                    raw={
                        "source": "metaculus_api",
                        "aggregation_method": aggregation_method or "recency_weighted",
                        "resolution_requested": requested_resolution,
                        "post_id": post_id,
                        "question_id": question_id,
                        "outcome": outcome,
                        "choice_id": choice_id,
                        "history_entry": dict(entry),
                    },
                )
            )
        candles.sort(key=lambda candle: candle.timestamp)
        return candles

    def account_recovery(self, operation: str, **kwargs: Any) -> Any:
        """Read posts/questions on which a Metaculus user has forecast.

        Metaculus' documented ``GET /api/posts/`` feed accepts a
        ``forecaster_id`` filter.  The endpoint is authenticated and returns
        the official post/question payload, so this operation deliberately
        preserves the upstream response rather than pretending forecasts are
        exchange fills or positions.  A forecaster id must be supplied either
        per call or as ``metaculus_forecaster_id`` in the market settings.
        """

        normalized = str(operation or "").strip().lower()
        if normalized not in self.account_recovery_operations:
            supported = ", ".join(self.account_recovery_operations)
            raise MarketConfigurationError(
                f"Metaculus account operation must be one of: {supported}."
            )

        raw_forecaster_id = kwargs.get("forecaster_id")
        if raw_forecaster_id in (None, ""):
            raw_forecaster_id = self.config.get("metaculus_forecaster_id")
        forecaster_id = self._bounded_account_int(
            raw_forecaster_id,
            "forecaster_id",
            minimum=1,
            maximum=2_147_483_647,
            required=True,
        )
        limit = self._bounded_account_int(
            kwargs.get("limit", 50), "limit", minimum=1, maximum=100, required=True
        )
        offset = self._bounded_account_int(
            kwargs.get("offset", 0), "offset", minimum=0, maximum=100_000, required=True
        )
        params: Dict[str, Any] = {
            "forecaster_id": forecaster_id,
            "limit": limit,
            "offset": offset,
        }
        for key in ("with_cp", "include_cp_history", "include_descriptions"):
            if key in kwargs and kwargs[key] is not None:
                params[key] = self._account_bool(kwargs[key], key)

        response = self._get("/posts/", params=params)
        if not isinstance(response, (Mapping, list)):
            raise MarketConfigurationError("Metaculus forecast_posts returned an invalid posts response.")
        return response

    def get_orderbook(self, contract_id: str):
        raise UnsupportedFeatureError(
            self.market_id,
            "orderbook_reading",
            "Metaculus is a forecasting platform and does not expose a trading orderbook.",
        )

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        """Validate and preview the official forecast payload without I/O."""

        self.ensure_capability("paper_trading")
        payload, endpoint, canonical, selected_probability = self._submission_payload(order)
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=canonical,
            accepted=True,
            message=(
                f"DRY RUN: would submit Metaculus forecast for {canonical}; "
                "no upstream request was sent."
            ),
            filled_size=0.0,
            average_price=selected_probability,
            raw={
                "endpoint": endpoint,
                "request": [payload],
                "semantics": "forecast_submission",
            },
        )

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        """Submit one forecast through Metaculus' documented forecast route."""

        self.ensure_capability("live_trading")
        # A zero binary probability is valid in Metaculus' [0, 1] forecast
        # domain, while the shared exchange-oriented preflight treats a
        # supplied limit of zero as invalid.  Omit it only from the safety
        # preview; the payload validator below still enforces the real range.
        safety_order = order
        if order.limit_price is not None:
            try:
                is_zero_probability = float(order.limit_price) == 0.0
            except (TypeError, ValueError):
                is_zero_probability = False
            if is_zero_probability:
                safety_order = replace(order, limit_price=None)
        preflight = self.preflight_live_order(safety_order, feature_name="forecast submission")
        payload, endpoint, canonical, _selected_probability = self._submission_payload(order)
        response = self.runtime.request_json(
            "POST",
            self._url(endpoint),
            json_body=[payload],
            headers=self._auth_headers(),
        )
        return {
            "market_id": self.market_id,
            "contract_id": canonical,
            "live": True,
            "endpoint": endpoint,
            "preflight": preflight,
            "request": [payload],
            "response": response,
            "semantics": "forecast_submission",
        }

    def _submission_payload(
        self,
        order: PaperOrderRequest,
    ) -> Tuple[Dict[str, Any], str, str, Optional[float]]:
        """Build a validated ``POST /api/questions/forecast/`` payload.

        The official API accepts a list of forecast objects.  Binary forecasts
        use ``probability_yes``; multiple-choice forecasts use a complete
        ``probability_yes_per_category`` distribution; numeric/date forecasts
        use the documented 201-point ``continuous_cdf``.  Metadata is used for
        the latter two shapes because a scalar order price cannot represent
        their full distributions.
        """

        self.ensure_order_market(order)
        if str(order.side or "").strip().upper() != "BUY":
            raise MarketConfigurationError("Metaculus forecast submission only accepts side BUY.")
        try:
            size = float(order.size)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Metaculus forecast size must be numeric.") from exc
        if not math.isfinite(size) or size <= 0:
            raise MarketConfigurationError("Metaculus forecast size must be positive.")

        post_id, question_id, outcome, choice_id = self._split_contract_id(order.contract_id)
        canonical = self._contract_id(post_id, question_id, outcome, choice_id)
        metadata = dict(order.metadata or {})
        question_ref = int(question_id) if question_id.isdigit() else question_id
        payload: Dict[str, Any] = {
            "question": question_ref,
            "source": "api",
            "probability_yes": None,
            "probability_yes_per_category": None,
            "continuous_cdf": None,
        }
        selected_probability: Optional[float] = None

        if outcome in {"YES", "NO"}:
            raw_probability = metadata.get("forecast_probability", order.limit_price)
            probability = self._forecast_probability(raw_probability)
            payload["probability_yes"] = probability if outcome == "YES" else 1.0 - probability
            selected_probability = probability
        elif outcome == "CHOICE":
            distribution = metadata.get("probability_yes_per_category")
            if distribution is None:
                distribution = metadata.get("forecast_distribution")
            if not isinstance(distribution, Mapping) or not distribution:
                raise MarketConfigurationError(
                    "Metaculus multiple-choice forecasts require metadata.probability_yes_per_category "
                    "as a complete label-to-probability mapping."
                )
            normalized: Dict[str, float] = {}
            for label, raw_value in distribution.items():
                key = str(label).strip()
                if not key:
                    raise MarketConfigurationError("Metaculus forecast distribution labels cannot be empty.")
                normalized[key] = self._forecast_probability(raw_value, label=f"probability for {key}")
            total = sum(normalized.values())
            if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-6):
                raise MarketConfigurationError(
                    f"Metaculus multiple-choice forecast probabilities must sum to 1.0 (got {total:.8f})."
                )
            payload["probability_yes_per_category"] = normalized
            selected_probability = normalized.get(choice_id or "")
        else:
            raw_cdf = metadata.get("continuous_cdf")
            if raw_cdf is None:
                raw_cdf = metadata.get("forecast_cdf")
            if not isinstance(raw_cdf, (list, tuple)) or len(raw_cdf) != 201:
                raise MarketConfigurationError(
                    "Metaculus numeric/date forecasts require metadata.continuous_cdf with exactly 201 values."
                )
            cdf: List[float] = []
            previous = -math.inf
            for index, raw_value in enumerate(raw_cdf):
                value = self._forecast_probability(raw_value, label=f"continuous_cdf[{index}]")
                if value < previous:
                    raise MarketConfigurationError("Metaculus continuous_cdf must be monotonically non-decreasing.")
                cdf.append(value)
                previous = value
            payload["continuous_cdf"] = cdf

        end_time = metadata.get("end_time") or metadata.get("forecast_end_time")
        if end_time not in (None, ""):
            if not isinstance(end_time, str) or not end_time.strip():
                raise MarketConfigurationError("Metaculus forecast end_time must be a non-empty ISO-8601 string.")
            payload["end_time"] = end_time.strip()
        return payload, "/questions/forecast/", canonical, selected_probability

    @staticmethod
    def _forecast_probability(value: Any, *, label: str = "forecast probability") -> float:
        try:
            probability = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"Metaculus {label} must be numeric in [0, 1].") from exc
        if not math.isfinite(probability) or probability < 0.0 or probability > 1.0:
            raise MarketConfigurationError(f"Metaculus {label} must be numeric in [0, 1].")
        return probability

    def _get_post(self, ref: str) -> Optional[Mapping[str, Any]]:
        if not ref:
            return None
        data = self._get(f"/posts/{ref}/")
        return data if isinstance(data, Mapping) else None

    @classmethod
    def _aggregation_for_history(
        cls,
        question: Mapping[str, Any],
        configured_method: str = "",
    ) -> Optional[Mapping[str, Any]]:
        aggregations = question.get("aggregations")
        if not isinstance(aggregations, Mapping):
            return None
        supported = {"recency_weighted", "metaculus_prediction", "community", "unweighted"}
        if configured_method and configured_method not in supported:
            raise MarketConfigurationError(
                "Metaculus metaculus_aggregation_method must be one of: "
                + ", ".join(sorted(supported))
                + "."
            )
        methods = [configured_method] if configured_method else [
            "recency_weighted",
            "metaculus_prediction",
            "community",
            "unweighted",
        ]
        for method in methods:
            aggregation = aggregations.get(method)
            if isinstance(aggregation, Mapping) and isinstance(aggregation.get("history"), list):
                return aggregation
        return None

    @classmethod
    def _history_value(
        cls,
        question: Mapping[str, Any],
        outcome: str,
        choice_id: Optional[str],
        entry: Mapping[str, Any],
    ) -> Optional[float]:
        if outcome in {"YES", "NO"}:
            probability = cls._history_binary_probability(entry)
            if probability is None:
                return None
            return 1.0 - probability if outcome == "NO" else probability
        if outcome == "CHOICE":
            if not choice_id:
                raise MarketConfigurationError("Metaculus choice contract requires a choice id.")
            return cls._history_choice_probability(question, choice_id, entry)
        return cls._history_numeric_value(entry)

    @classmethod
    def _history_binary_probability(cls, entry: Mapping[str, Any]) -> Optional[float]:
        for key in ("probability", "prob", "center", "median", "q2"):
            probability = cls._probability_from_value(entry.get(key))
            if probability is not None:
                return probability
        for key in ("centers", "forecast_values", "means"):
            values = entry.get(key)
            if isinstance(values, Mapping):
                for candidate in ("YES", "yes", "probability", "prob", "center"):
                    if candidate in values:
                        probability = cls._probability_from_value(values[candidate])
                        if probability is not None:
                            return probability
                values = list(values.values())
            if isinstance(values, list) and values:
                probability = cls._probability_from_value(values[0])
                if probability is not None:
                    return probability
        return None

    @classmethod
    def _history_choice_probability(
        cls,
        question: Mapping[str, Any],
        choice_id: str,
        entry: Mapping[str, Any],
    ) -> Optional[float]:
        for key in ("forecast_values", "choice_probabilities", "choiceProbabilities", "answerProbs"):
            values = entry.get(key)
            if isinstance(values, Mapping):
                if choice_id in values:
                    return cls._probability_from_value(values.get(choice_id))
                for raw_key, raw_value in values.items():
                    if str(raw_key) == choice_id:
                        return cls._probability_from_value(raw_value)
            elif isinstance(values, list):
                index = cls._choice_index(question, choice_id)
                if index is not None and index < len(values):
                    return cls._probability_from_value(values[index])
        return None

    @classmethod
    def _history_numeric_value(cls, entry: Mapping[str, Any]) -> Optional[float]:
        for key in ("median", "center", "q2", "mean"):
            value = cls._number_from_value(entry.get(key))
            if value is not None:
                return value
        for key in ("centers", "means"):
            values = entry.get(key)
            if isinstance(values, list) and values:
                value = cls._number_from_value(values[len(values) // 2])
                if value is not None:
                    return value
        return None

    @classmethod
    def _choice_index(cls, question: Mapping[str, Any], choice_id: str) -> Optional[int]:
        for index, (raw_id, _label) in enumerate(cls._choices_from_question(question)):
            if raw_id == choice_id:
                return index
        try:
            index = int(choice_id)
        except (TypeError, ValueError):
            return None
        return index if index >= 0 else None

    @staticmethod
    def _optional_timestamp(value: Optional[float], label: str) -> Optional[float]:
        if value is None:
            return None
        try:
            timestamp = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"Metaculus {label} must be numeric.") from exc
        if not math.isfinite(timestamp):
            raise MarketConfigurationError(f"Metaculus {label} must be finite.")
        return timestamp

    @staticmethod
    def _bounded_account_int(
        value: Any,
        label: str,
        *,
        minimum: int,
        maximum: int,
        required: bool = False,
    ) -> Optional[int]:
        if value in (None, ""):
            if required:
                raise MarketConfigurationError(f"Metaculus account {label} is required.")
            return None
        if isinstance(value, bool):
            raise MarketConfigurationError(f"Metaculus account {label} must be an integer.")
        text = str(value).strip()
        if not text or (text.startswith("+") and not text[1:].isdigit()) or (
            text.startswith("-") and not text[1:].isdigit()
        ) or (not text.lstrip("+-").isdigit()):
            raise MarketConfigurationError(f"Metaculus account {label} must be an integer.")
        parsed = int(text)
        if parsed < minimum or parsed > maximum:
            raise MarketConfigurationError(
                f"Metaculus account {label} must be between {minimum} and {maximum}."
            )
        return parsed

    @staticmethod
    def _account_bool(value: Any, label: str) -> bool:
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        raise MarketConfigurationError(f"Metaculus account {label} must be a boolean.")

    @staticmethod
    def _history_timestamp(entry: Mapping[str, Any]) -> Optional[float]:
        raw = entry.get("start_time")
        if raw is None:
            raw = entry.get("timestamp") or entry.get("time") or entry.get("end_time")
        if raw is None:
            return None
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            value = float(raw)
            return value if math.isfinite(value) else None
        text = str(raw).strip()
        if not text:
            return None
        try:
            value = float(text)
            return value if math.isfinite(value) else None
        except ValueError:
            pass
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()

    def _get(self, path: str, *, params: Optional[Mapping[str, Any]] = None) -> Any:
        return self.runtime.get_json(self._url(path), params=params, headers=self._auth_headers())

    def _url(self, path: str) -> str:
        clean_path = "/" + str(path or "").lstrip("/")
        return f"{self.api_base_url}{clean_path}"

    def _auth_headers(self) -> Dict[str, str]:
        credential = self.resolve_credential(
            "metaculus_api_token",
            ("METACULUS_API_TOKEN",),
            required=True,
            label="METACULUS_API_TOKEN",
        )
        return {"Authorization": f"Token {credential.value}"}

    def _event_from_post(self, post: Mapping[str, Any]) -> MarketEvent:
        post_id = str(post.get("id") or "").strip()
        return MarketEvent(
            market_id=self.market_id,
            event_id=post_id,
            title=self._post_title(post),
            url=self._post_url(post),
            status=self._post_status(post),
            raw=dict(post),
        )

    def _contracts_from_question(
        self,
        post_id: str,
        post: Mapping[str, Any],
        question: Mapping[str, Any],
    ) -> List[MarketContract]:
        question_id = str(question.get("id") or "").strip()
        if not question_id:
            return []
        title = self._question_title(question, fallback=self._post_title(post))
        status = self._question_status(question, fallback=self._post_status(post))
        question_type = self._question_type(question)
        if question_type == "BINARY":
            return [
                MarketContract(
                    market_id=self.market_id,
                    contract_id=self._contract_id(post_id, question_id, "YES"),
                    event_id=post_id,
                    title=f"{title} - Yes",
                    outcome="Yes",
                    url=self._post_url(post),
                    status=status,
                    raw={"post": dict(post), "question": dict(question), "outcome": "YES"},
                ),
                MarketContract(
                    market_id=self.market_id,
                    contract_id=self._contract_id(post_id, question_id, "NO"),
                    event_id=post_id,
                    title=f"{title} - No",
                    outcome="No",
                    url=self._post_url(post),
                    status=status,
                    raw={"post": dict(post), "question": dict(question), "outcome": "NO"},
                ),
            ]
        if question_type == "MULTIPLE_CHOICE":
            contracts: List[MarketContract] = []
            for choice_id, choice_label in self._choices_from_question(question):
                contracts.append(
                    MarketContract(
                        market_id=self.market_id,
                        contract_id=self._contract_id(post_id, question_id, "CHOICE", choice_id),
                        event_id=post_id,
                        title=f"{title} - {choice_label}",
                        outcome=choice_label,
                        url=self._post_url(post),
                        status=status,
                        raw={"post": dict(post), "question": dict(question), "choice_id": choice_id},
                    )
                )
            return contracts

        return [
            MarketContract(
                market_id=self.market_id,
                contract_id=self._contract_id(post_id, question_id, "VALUE"),
                event_id=post_id,
                title=title,
                outcome="Forecast value",
                url=self._post_url(post),
                status=status,
                raw={"post": dict(post), "question": dict(question), "outcome": "VALUE"},
            )
        ]

    @staticmethod
    def _as_post_list(data: Any) -> List[Mapping[str, Any]]:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, Mapping)]
        if isinstance(data, Mapping):
            posts = data.get("results") or data.get("posts") or data.get("items") or []
            if isinstance(posts, list):
                return [item for item in posts if isinstance(item, Mapping)]
        return []

    @staticmethod
    def _questions_from_post(post: Mapping[str, Any]) -> List[Mapping[str, Any]]:
        questions: List[Mapping[str, Any]] = []
        direct = post.get("question")
        if isinstance(direct, Mapping):
            questions.append(direct)
        raw_questions = post.get("questions")
        if isinstance(raw_questions, list):
            questions.extend(item for item in raw_questions if isinstance(item, Mapping))
        for group_key in ("group_of_questions", "question_group", "group"):
            group = post.get(group_key)
            if not isinstance(group, Mapping):
                continue
            for question_key in ("questions", "subquestions"):
                group_questions = group.get(question_key)
                if isinstance(group_questions, list):
                    questions.extend(item for item in group_questions if isinstance(item, Mapping))
        conditional = post.get("conditional")
        if isinstance(conditional, Mapping):
            for value in conditional.values():
                if isinstance(value, Mapping) and MetaculusAdapter._looks_like_question(value):
                    questions.append(value)

        deduped: Dict[str, Mapping[str, Any]] = {}
        for question in questions:
            question_id = str(question.get("id") or "").strip()
            if question_id:
                deduped[question_id] = question
        return list(deduped.values())

    @staticmethod
    def _find_question(post: Optional[Mapping[str, Any]], question_id: str) -> Optional[Mapping[str, Any]]:
        if not post:
            return None
        for question in MetaculusAdapter._questions_from_post(post):
            if str(question.get("id") or "").strip() == question_id:
                return question
        return None

    @staticmethod
    def _looks_like_question(value: Mapping[str, Any]) -> bool:
        return bool(value.get("id")) and any(
            key in value for key in ("type", "question_type", "forecast_type", "possibilities", "aggregations")
        )

    @staticmethod
    def _question_type(question: Mapping[str, Any]) -> str:
        raw = (
            question.get("type")
            or question.get("question_type")
            or question.get("forecast_type")
            or question.get("outcome_type")
        )
        if not raw and isinstance(question.get("possibilities"), Mapping):
            raw = question["possibilities"].get("type")
        normalized = str(raw or "").replace("-", "_").replace(" ", "_").upper()
        if normalized in {"BINARY", "BIN"}:
            return "BINARY"
        if normalized in {"MULTIPLE_CHOICE", "MULTIPLECHOICE", "CHOICE"}:
            return "MULTIPLE_CHOICE"
        if MetaculusAdapter._choices_from_question(question):
            return "MULTIPLE_CHOICE"
        return "VALUE"

    @staticmethod
    def _choices_from_question(question: Mapping[str, Any]) -> List[Tuple[str, str]]:
        raw_choices: Any = question.get("choices") or question.get("options")
        possibilities = question.get("possibilities")
        if isinstance(possibilities, Mapping):
            raw_choices = raw_choices or possibilities.get("choices") or possibilities.get("options")

        choices: List[Tuple[str, str]] = []
        if isinstance(raw_choices, Mapping):
            raw_choices = [
                {"id": key, "label": value}
                for key, value in raw_choices.items()
            ]
        if not isinstance(raw_choices, list):
            return choices
        for index, raw in enumerate(raw_choices):
            if isinstance(raw, Mapping):
                choice_id = str(raw.get("id") or raw.get("key") or index).strip()
                label = str(raw.get("label") or raw.get("name") or raw.get("text") or choice_id).strip()
            else:
                choice_id = str(index)
                label = str(raw)
            if choice_id:
                choices.append((choice_id, label or choice_id))
        return choices

    @staticmethod
    def _binary_probability(question: Mapping[str, Any]) -> float:
        probability = MetaculusAdapter._probability_from_value(question.get("community_prediction"))
        if probability is None:
            probability = MetaculusAdapter._probability_from_value(question.get("communityPrediction"))
        if probability is None:
            probability = MetaculusAdapter._probability_from_value(question.get("probability"))
        if probability is None:
            probability = MetaculusAdapter._probability_from_value(question.get("cp"))
        if probability is None:
            probability = MetaculusAdapter._probability_from_aggregation(question)
        if probability is None:
            raise MarketConfigurationError(
                "Metaculus response did not include an accessible Community Prediction for this question."
            )
        return probability

    @staticmethod
    def _choice_probability(question: Mapping[str, Any], choice_id: str) -> float:
        for raw_map in (
            question.get("choice_probabilities"),
            question.get("choiceProbabilities"),
            question.get("answerProbs"),
        ):
            if isinstance(raw_map, Mapping) and choice_id in raw_map:
                probability = MetaculusAdapter._probability_from_value(raw_map.get(choice_id))
                if probability is not None:
                    return probability
        for raw_choice_id, _label in MetaculusAdapter._choices_from_question(question):
            if raw_choice_id != choice_id:
                continue
            choices = question.get("choices") or question.get("options")
            possibilities = question.get("possibilities")
            if isinstance(possibilities, Mapping):
                choices = choices or possibilities.get("choices") or possibilities.get("options")
            if isinstance(choices, list):
                for raw in choices:
                    if isinstance(raw, Mapping) and str(raw.get("id") or raw.get("key") or "") == choice_id:
                        probability = MetaculusAdapter._probability_from_value(
                            raw.get("probability") or raw.get("community_prediction")
                        )
                        if probability is not None:
                            return probability
        aggregation = MetaculusAdapter._latest_aggregation(question)
        if aggregation:
            values = aggregation.get("forecast_values") or aggregation.get("choice_probabilities")
            if isinstance(values, Mapping) and choice_id in values:
                probability = MetaculusAdapter._probability_from_value(values.get(choice_id))
                if probability is not None:
                    return probability
        raise MarketConfigurationError(
            f"Metaculus response did not include an accessible Community Prediction for choice {choice_id}."
        )

    @staticmethod
    def _numeric_forecast(question: Mapping[str, Any]) -> float:
        for key in ("median", "community_prediction", "communityPrediction", "prediction"):
            value = MetaculusAdapter._number_from_value(question.get(key))
            if value is not None:
                return value
        aggregation = MetaculusAdapter._latest_aggregation(question)
        if aggregation:
            for key in ("center", "median", "q2"):
                value = MetaculusAdapter._number_from_value(aggregation.get(key))
                if value is not None:
                    return value
            centers = aggregation.get("centers")
            if isinstance(centers, list) and centers:
                value = MetaculusAdapter._number_from_value(centers[len(centers) // 2])
                if value is not None:
                    return value
        raise MarketConfigurationError("Metaculus response did not include an accessible numeric forecast.")

    @staticmethod
    def _probability_from_aggregation(question: Mapping[str, Any]) -> Optional[float]:
        aggregation = MetaculusAdapter._latest_aggregation(question)
        if not aggregation:
            return None
        for key in ("prob", "probability", "center", "median", "q2"):
            probability = MetaculusAdapter._probability_from_value(aggregation.get(key))
            if probability is not None:
                return probability
        for key in ("centers", "forecast_values"):
            values = aggregation.get(key)
            if isinstance(values, list) and values:
                candidate = values[-1] if len(values) == 2 else values[0]
                probability = MetaculusAdapter._probability_from_value(candidate)
                if probability is not None:
                    return probability
        return None

    @staticmethod
    def _latest_aggregation(question: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
        aggregations = question.get("aggregations")
        if not isinstance(aggregations, Mapping):
            return None
        for key in ("recency_weighted", "community", "unweighted", "metaculus_prediction"):
            aggregation = aggregations.get(key)
            if not isinstance(aggregation, Mapping):
                continue
            latest = aggregation.get("latest")
            if isinstance(latest, Mapping):
                return latest
            if isinstance(aggregation.get("history"), list) and aggregation["history"]:
                last = aggregation["history"][-1]
                if isinstance(last, Mapping):
                    return last
        return None

    @staticmethod
    def _probability_from_value(value: Any) -> Optional[float]:
        if isinstance(value, Mapping):
            for key in ("prob", "probability", "center", "median", "q2", "value"):
                probability = MetaculusAdapter._probability_from_value(value.get(key))
                if probability is not None:
                    return probability
            full = value.get("full")
            if isinstance(full, Mapping):
                return MetaculusAdapter._probability_from_value(full)
            return None
        number = MetaculusAdapter._number_from_value(value)
        if number is None or number < 0.0 or number > 1.0:
            return None
        return number

    @staticmethod
    def _number_from_value(value: Any) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    @staticmethod
    def _split_contract_id(contract_id: str) -> Tuple[str, str, str, Optional[str]]:
        raw = str(contract_id or "").strip()
        parts = raw.split(":")
        if len(parts) < 3:
            raise MarketConfigurationError("Metaculus contract id must be post_id:question_id:outcome.")
        post_id = parts[0].strip()
        question_id = parts[1].strip()
        outcome = parts[2].strip().upper()
        choice_id = parts[3].strip() if len(parts) > 3 else None
        if not post_id or not question_id:
            raise MarketConfigurationError("Metaculus contract id requires post and question ids.")
        if outcome not in {"YES", "NO", "CHOICE", "VALUE"}:
            raise MarketConfigurationError("Metaculus contract outcome must be YES, NO, CHOICE, or VALUE.")
        if outcome == "CHOICE" and not choice_id:
            raise MarketConfigurationError("Metaculus choice contract requires a choice id.")
        return post_id, question_id, outcome, choice_id

    @staticmethod
    def _contract_id(post_id: str, question_id: str, outcome: str, choice_id: Optional[str] = None) -> str:
        if outcome.upper() == "CHOICE":
            return f"{post_id}:{question_id}:CHOICE:{choice_id}"
        return f"{post_id}:{question_id}:{outcome.upper()}"

    @staticmethod
    def _post_title(post: Mapping[str, Any]) -> str:
        question = post.get("question")
        return str(
            post.get("title")
            or (question.get("title") if isinstance(question, Mapping) else "")
            or (question.get("question") if isinstance(question, Mapping) else "")
            or post.get("short_title")
            or post.get("id")
            or ""
        )

    @staticmethod
    def _question_title(question: Mapping[str, Any], *, fallback: str = "") -> str:
        return str(question.get("title") or question.get("question") or question.get("name") or fallback)

    @staticmethod
    def _post_url(post: Mapping[str, Any]) -> str:
        url = str(post.get("url") or post.get("page_url") or "")
        if url.startswith("http"):
            return url
        if url:
            return f"https://www.metaculus.com{url if url.startswith('/') else '/' + url}"
        post_id = str(post.get("id") or "").strip()
        return f"https://www.metaculus.com/questions/{post_id}/" if post_id else "https://www.metaculus.com"

    @staticmethod
    def _post_status(post: Mapping[str, Any]) -> str:
        if post.get("is_resolved") is True or post.get("resolved") is True:
            return "resolved"
        if post.get("closed") is True:
            return "closed"
        return str(post.get("status") or "open").lower()

    @staticmethod
    def _question_status(question: Mapping[str, Any], *, fallback: str = "open") -> str:
        if question.get("resolution") not in (None, "", "open"):
            return "resolved"
        if question.get("resolved") is True:
            return "resolved"
        if question.get("closed") is True:
            return "closed"
        return str(question.get("status") or fallback or "open").lower()

