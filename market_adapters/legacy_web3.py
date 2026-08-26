from __future__ import annotations

import json
import math
import re
from urllib.parse import urlsplit
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

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


DEFAULT_ZEITGEIST_INDEXER_URL = "https://processor.bsr.zeitgeist.pm/graphql"

AUGUR_REFERENCES = (
    "https://github.com/AugurProject/augur",
    "https://github.com/protofire/augur-v2-subgraph",
)
OMEN_REFERENCES = (
    "https://github.com/protofire/omen-exchange",
    "https://github.com/protofire/omen-subgraph",
    "https://omendotag.gitbook.io/omen",
)
GNOSIS_REFERENCES = (
    "https://docs.gnosis.io/",
    "https://omen.eth.limo",
    "https://github.com/protofire/omen-subgraph",
)
ZEITGEIST_REFERENCES = (
    "https://docs.zeitgeist.pm/docs/build/sdk/v2/fetch-markets",
    "https://docs.zeitgeist.pm/docs/build/sdk/v2/indexer",
    "https://docs.zeitgeist.pm/docs/build/sdk/v2/calculating-current-prediction",
    "https://github.com/zeitgeistpm/zeitgeist/tree/main/zrml/hybrid-router",
    "https://github.com/zeitgeistpm/zeitgeist/releases",
)
REALITY_ETH_REFERENCES = (
    "https://reality.eth.limo/app/docs/html/contracts.html",
    "https://github.com/RealityETH/reality-eth-monorepo/tree/main/packages/graph",
    "https://raw.githubusercontent.com/RealityETH/reality-eth-monorepo/master/packages/graph/schema.graphql",
)


class _GraphQLAdapter(MarketAdapter):
    graphql_config_key = ""
    graphql_env_vars: Sequence[str] = ()
    default_graphql_url = ""

    @property
    def graphql_url(self) -> str:
        url, _source = self._graphql_url_with_source(required=True)
        return url

    def _graphql_url_with_source(self, *, required: bool = False) -> Tuple[str, str]:
        credential = self.resolve_credential(
            self.graphql_config_key,
            self.graphql_env_vars,
            required=False,
            label=self.graphql_config_key.upper(),
        )
        if credential and credential.value.strip():
            return credential.value.strip().rstrip("/"), credential.source
        if self.default_graphql_url:
            return self.default_graphql_url.rstrip("/"), "default"
        if required:
            names = ", ".join([self.graphql_config_key, *self.graphql_env_vars])
            raise MarketConfigurationError(f"{self.display_name} requires a configured GraphQL endpoint: {names}.")
        return "", "missing"

    def _graphql(self, query: str, variables: Optional[Mapping[str, Any]] = None) -> Mapping[str, Any]:
        payload = self.runtime.request_json(
            "POST",
            self.graphql_url,
            json_body={"query": query, "variables": dict(variables or {})},
            headers={"Content-Type": "application/json"},
        )
        if not isinstance(payload, Mapping):
            raise MarketHTTPError(f"{self.market_id} GraphQL response was not a JSON object.")
        errors = payload.get("errors")
        if errors:
            raise MarketHTTPError(f"{self.market_id} GraphQL errors: {errors}")
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise MarketHTTPError(f"{self.market_id} GraphQL response did not include data.")
        return data

    @staticmethod
    def _is_positive_number(value: Any) -> bool:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False
        return math.isfinite(number) and number > 0

    @staticmethod
    def _probability(value: Any, *, allow_zero: bool = True) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Probability must be numeric.") from exc
        if not math.isfinite(number):
            raise MarketConfigurationError("Probability must be finite.")
        if number > 1.0 and number <= 100.0:
            number = number / 100.0
        lower_ok = number >= 0.0 if allow_zero else number > 0.0
        if not lower_ok or number > 1.0:
            raise MarketConfigurationError("Probability must be between 0 and 1.")
        return number

    @staticmethod
    def _optional_probability(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            return _GraphQLAdapter._probability(value)
        except MarketConfigurationError:
            return None


class AugurAdapter(_GraphQLAdapter):
    """Read-only Augur v2 protocol adapter backed by the documented subgraph schema."""

    metadata = get_market_metadata("augur")
    graphql_config_key = "augur_subgraph_url"
    graphql_env_vars = ("AUGUR_SUBGRAPH_URL",)

    MARKETS_QUERY = """
    query AugurMarkets($first: Int!) {
      markets(first: $first, orderBy: timestamp, orderDirection: desc) {
        id
        description
        longDescription
        categories
        status
        marketType
        endTimestamp
        timestamp
        numOutcomes
        outcomes {
          id
          value
          payoutNumerator
          isFinalNumerator
        }
      }
    }
    """

    MARKET_QUERY = """
    query AugurMarket($id: ID!) {
      market(id: $id) {
        id
        description
        longDescription
        categories
        status
        marketType
        endTimestamp
        timestamp
        numOutcomes
        outcomes {
          id
          value
          payoutNumerator
          isFinalNumerator
        }
      }
    }
    """

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        url, source = self._graphql_url_with_source(required=False)
        health.update(
            {
                "graphql_url_configured": bool(url),
                "graphql_url_source": source,
                "references": list(AUGUR_REFERENCES),
                "price_reading_supported": False,
                "live_trading_enabled": False,
            }
        )
        return health

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = max(1, min(int(limit or 50), 100))
        data = self._graphql(self.MARKETS_QUERY, {"first": desired})
        markets = self._markets_from_payload(data)
        q = str(query or "").strip().lower()
        if q:
            markets = [market for market in markets if self._market_matches_query(market, q)]
        return [self._event_from_market(market) for market in markets[:desired]]

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        market_id = str(event_id or "").strip()
        if not market_id:
            return []
        market = self._fetch_market(market_id)
        return self._contracts_from_market(market)

    def get_price(self, contract_id: str) -> PriceSnapshot:
        raise UnsupportedFeatureError(
            self.market_id,
            "price_reading",
            "Augur v2 subgraph market entities expose lifecycle/outcome data, but not a maintained live price or orderbook feed.",
        )

    def get_orderbook(self, contract_id: str):
        raise UnsupportedFeatureError(
            self.market_id,
            "orderbook_reading",
            "Augur orderbook/trading data is not exposed through a maintained official Python-compatible API in this adapter.",
        )

    def place_paper_order(self, order: PaperOrderRequest):
        raise UnsupportedFeatureError(
            self.market_id,
            "paper_trading",
            "Augur paper trading is disabled because the implemented adapter is read-only market discovery/listing.",
        )

    def place_live_order(self, order: PaperOrderRequest):
        raise UnsupportedFeatureError(
            self.market_id,
            "live_trading",
            "Augur live trading requires explicit wallet-signed protocol transactions and is not implemented.",
        )

    def copy_trade_from_activity(self, activity: Mapping[str, Any]):
        raise UnsupportedFeatureError(
            self.market_id,
            "copy_trading",
            "Augur copy trading is unsupported because no maintained official account activity mirroring API is used.",
        )

    def _fetch_market(self, market_id: str) -> Mapping[str, Any]:
        data = self._graphql(self.MARKET_QUERY, {"id": market_id})
        market = data.get("market")
        if isinstance(market, Mapping):
            return market
        raise MarketConfigurationError(f"Augur market {market_id!r} was not found.")

    def _event_from_market(self, market: Mapping[str, Any]) -> MarketEvent:
        market_id = self._market_id(market)
        return MarketEvent(
            market_id=self.market_id,
            event_id=market_id,
            title=str(market.get("description") or market.get("longDescription") or market_id),
            url="https://augur.net",
            status=str(market.get("status") or "").strip().lower(),
            raw=dict(market),
        )

    def _contracts_from_market(self, market: Mapping[str, Any]) -> List[MarketContract]:
        market_id = self._market_id(market)
        title = str(market.get("description") or market_id)
        status = str(market.get("status") or "").strip().lower()
        contracts: List[MarketContract] = []
        for index, outcome in enumerate(self._outcomes(market)):
            outcome_id = str(outcome.get("id") or index)
            outcome_name = str(outcome.get("value") or f"Outcome {index}")
            contracts.append(
                MarketContract(
                    market_id=self.market_id,
                    contract_id=self._contract_id(market_id, outcome_id),
                    event_id=market_id,
                    title=f"{title} - {outcome_name}",
                    outcome=outcome_name,
                    url="https://augur.net",
                    status=status,
                    raw={"market": dict(market), "outcome": dict(outcome), "outcome_index": index},
                )
            )
        return contracts

    @staticmethod
    def _markets_from_payload(data: Mapping[str, Any]) -> List[Mapping[str, Any]]:
        markets = data.get("markets")
        return [market for market in markets if isinstance(market, Mapping)] if isinstance(markets, list) else []

    @staticmethod
    def _outcomes(market: Mapping[str, Any]) -> List[Mapping[str, Any]]:
        outcomes = market.get("outcomes")
        return [outcome for outcome in outcomes if isinstance(outcome, Mapping)] if isinstance(outcomes, list) else []

    @staticmethod
    def _market_matches_query(market: Mapping[str, Any], query: str) -> bool:
        values = [
            market.get("id"),
            market.get("description"),
            market.get("longDescription"),
            market.get("marketType"),
            " ".join(str(category) for category in market.get("categories") or []),
            " ".join(str(outcome.get("value") or "") for outcome in AugurAdapter._outcomes(market)),
        ]
        return query in " ".join(str(value or "") for value in values).lower()

    @staticmethod
    def _market_id(market: Mapping[str, Any]) -> str:
        return str(market.get("id") or "").strip()

    @staticmethod
    def _contract_id(market_id: str, outcome_id: str) -> str:
        return f"{market_id}:{outcome_id}"


class RealityEthMarketsAdapter(_GraphQLAdapter):
    """Read-only Reality.eth question adapter backed by the official subgraph schema.

    Reality.eth is an oracle/question protocol, not a traded CLOB.  This adapter
    deliberately exposes only question discovery, response-option listing, and
    alert-compatible lifecycle metadata; prices, orders, and wallet execution
    remain unsupported.
    """

    metadata = get_market_metadata("reality_eth_markets")
    graphql_config_key = "reality_eth_subgraph_url"
    graphql_env_vars = ("REALITY_ETH_SUBGRAPH_URL", "REALITYETH_SUBGRAPH_URL")

    QUESTIONS_QUERY = """
    query RealityQuestions($first: Int!) {
      questions(first: $first, orderBy: createdTimestamp, orderDirection: desc) {
        id
        questionId
        contract
        createdBlock
        createdTimestamp
        updatedBlock
        updatedTimestamp
        data
        qJsonStr
        qTitle
        qCategory
        qDescription
        qLang
        qType
        user
        arbitrator
        openingTimestamp
        timeout
        bounty
        currentAnswer
        currentAnswerBond
        currentAnswerTimestamp
        minBond
        lastBond
        cumulativeBonds
        isPendingArbitration
        arbitrationOccurred
        answerFinalizedTimestamp
        currentScheduledFinalizationTimestamp
        outcomes {
          id
          answer
        }
      }
    }
    """

    QUESTION_QUERY = """
    query RealityQuestion($id: ID!) {
      question(id: $id) {
        id
        questionId
        contract
        createdBlock
        createdTimestamp
        updatedBlock
        updatedTimestamp
        data
        qJsonStr
        qTitle
        qCategory
        qDescription
        qLang
        qType
        user
        arbitrator
        openingTimestamp
        timeout
        bounty
        currentAnswer
        currentAnswerBond
        currentAnswerTimestamp
        minBond
        lastBond
        cumulativeBonds
        isPendingArbitration
        arbitrationOccurred
        answerFinalizedTimestamp
        currentScheduledFinalizationTimestamp
        outcomes {
          id
          answer
        }
      }
    }
    """

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        url, source = self._graphql_url_with_source(required=False)
        health.update(
            {
                "graphql_url_configured": bool(url),
                "graphql_url_source": source,
                "references": list(REALITY_ETH_REFERENCES),
                "question_schema_supported": True,
                "price_reading_supported": False,
                "orderbook_supported": False,
                "paper_trading_supported": False,
                "live_trading_enabled": False,
            }
        )
        return health

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = max(1, min(int(limit or 50), 100))
        data = self._graphql(self.QUESTIONS_QUERY, {"first": desired})
        questions = self._questions_from_payload(data)
        q = str(query or "").strip().lower()
        if q:
            questions = [question for question in questions if self._question_matches_query(question, q)]
        return [self._event_from_question(question) for question in questions[:desired]]

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        question = self._fetch_question(event_id)
        question_id = self._question_id(question)
        title = self._title(question)
        status = self._status(question)
        contracts = []
        for index, outcome in enumerate(self._outcomes(question)):
            outcome_id = str(outcome.get("id") or index)
            outcome_name = str(outcome.get("answer") or f"Answer {index + 1}")
            contracts.append(
                MarketContract(
                    market_id=self.market_id,
                    contract_id=self._contract_id(question_id, outcome_id),
                    event_id=str(question.get("id") or question_id),
                    title=f"{title} - {outcome_name}",
                    outcome=outcome_name,
                    url="https://reality.eth.limo",
                    status=status,
                    raw={"question": dict(question), "outcome": dict(outcome), "outcome_index": index},
                )
            )
        return contracts

    def get_price(self, contract_id: str) -> PriceSnapshot:
        raise UnsupportedFeatureError(
            self.market_id,
            "price_reading",
            "Reality.eth publishes oracle answers and question lifecycle data, not tradable contract prices.",
        )

    def get_orderbook(self, contract_id: str):
        raise UnsupportedFeatureError(
            self.market_id,
            "orderbook_reading",
            "Reality.eth is a question/oracle protocol and has no CLOB orderbook.",
        )

    def place_paper_order(self, order: PaperOrderRequest):
        raise UnsupportedFeatureError(
            self.market_id,
            "paper_trading",
            "Reality.eth question responses are not tradeable paper orders.",
        )

    def place_live_order(self, order: PaperOrderRequest):
        raise UnsupportedFeatureError(
            self.market_id,
            "live_trading",
            "Reality.eth answer submission requires explicit wallet-signed protocol transactions and is not trading.",
        )

    def copy_trade_from_activity(self, activity: Mapping[str, Any]):
        raise UnsupportedFeatureError(
            self.market_id,
            "copy_trading",
            "Reality.eth has no official account-activity mirroring model for copy trading.",
        )

    def _fetch_question(self, question_id: str) -> Mapping[str, Any]:
        normalized = str(question_id or "").strip()
        if not normalized:
            raise MarketConfigurationError("Reality.eth question id cannot be empty.")
        data = self._graphql(self.QUESTION_QUERY, {"id": normalized})
        question = data.get("question")
        if isinstance(question, Mapping):
            return question
        raise MarketConfigurationError(f"Reality.eth question {normalized!r} was not found.")

    def _event_from_question(self, question: Mapping[str, Any]) -> MarketEvent:
        event_id = str(question.get("id") or self._question_id(question))
        return MarketEvent(
            market_id=self.market_id,
            event_id=event_id,
            title=self._title(question),
            url="https://reality.eth.limo",
            status=self._status(question),
            raw=dict(question),
        )

    @staticmethod
    def _questions_from_payload(data: Mapping[str, Any]) -> List[Mapping[str, Any]]:
        questions = data.get("questions")
        return [question for question in questions if isinstance(question, Mapping)] if isinstance(questions, list) else []

    @staticmethod
    def _question_id(question: Mapping[str, Any]) -> str:
        return str(question.get("questionId") or question.get("id") or "").strip()

    @staticmethod
    def _title(question: Mapping[str, Any]) -> str:
        title = str(question.get("qTitle") or question.get("qDescription") or "").strip()
        if title:
            return title
        raw_json = question.get("qJsonStr")
        if raw_json:
            try:
                parsed = json.loads(str(raw_json))
            except (TypeError, ValueError):
                parsed = {}
            if isinstance(parsed, Mapping) and str(parsed.get("title") or "").strip():
                return str(parsed["title"]).strip()
        return RealityEthMarketsAdapter._question_id(question) or "Reality.eth question"

    @staticmethod
    def _status(question: Mapping[str, Any]) -> str:
        if question.get("answerFinalizedTimestamp") not in (None, "", 0, "0"):
            return "finalized"
        if bool(question.get("isPendingArbitration")):
            return "pending_arbitration"
        return "open"

    @staticmethod
    def _outcomes(question: Mapping[str, Any]) -> List[Mapping[str, Any]]:
        outcomes = question.get("outcomes")
        normalized = [outcome for outcome in outcomes if isinstance(outcome, Mapping)] if isinstance(outcomes, list) else []
        if normalized:
            return normalized
        q_type = str(question.get("qType") or "").lower()
        if q_type in {"bool", "boolean"}:
            return [{"id": "yes", "answer": "Yes"}, {"id": "no", "answer": "No"}]
        raw_json = question.get("qJsonStr")
        if raw_json:
            try:
                parsed = json.loads(str(raw_json))
            except (TypeError, ValueError):
                parsed = {}
            values = parsed.get("outcomes") if isinstance(parsed, Mapping) else None
            if isinstance(values, list) and values:
                return [{"id": str(index), "answer": str(value)} for index, value in enumerate(values)]
        return [{"id": "answer", "answer": "Answer"}]

    @classmethod
    def _question_matches_query(cls, question: Mapping[str, Any], query: str) -> bool:
        values = [
            question.get("id"),
            question.get("questionId"),
            question.get("qTitle"),
            question.get("qDescription"),
            question.get("qCategory"),
            question.get("qType"),
            cls._title(question),
            " ".join(str(outcome.get("answer") or "") for outcome in cls._outcomes(question)),
        ]
        return query in " ".join(str(value or "") for value in values).lower()

    @staticmethod
    def _contract_id(question_id: str, outcome_id: str) -> str:
        return f"{question_id}:{outcome_id}"


class OmenAdapter(_GraphQLAdapter):
    """Omen AMM adapter using the documented FixedProductMarketMaker schema.

    Reads and paper orders use the official subgraph.  Live orders are limited
    to forwarding an operator-reviewed, externally signed FPMM transaction to
    an explicitly configured EVM RPC; this adapter never signs, approves
    collateral, or settles a position.
    """

    metadata = get_market_metadata("omen")
    graphql_config_key = "omen_subgraph_url"
    graphql_env_vars = ("OMEN_SUBGRAPH_URL",)
    account_recovery_operations = ("activity",)

    FPMMS_QUERY = """
    query OmenMarkets($first: Int!) {
      fixedProductMarketMakers(first: $first, orderBy: creationTimestamp, orderDirection: desc) {
        id
        title
        category
        outcomes
        outcomeTokenMarginalPrices
        outcomeTokenAmounts
        outcomeSlotCount
        openingTimestamp
        resolutionTimestamp
        currentAnswer
        answerFinalizedTimestamp
        scaledLiquidityMeasure
        scaledRunningDailyVolume
        collateralToken
        curatedByDxDao
        question {
          id
          title
          category
          outcomes
          openingTimestamp
        }
        condition {
          id
          resolutionTimestamp
          payouts
        }
      }
    }
    """

    FPMM_QUERY = """
    query OmenMarket($id: ID!) {
      fixedProductMarketMaker(id: $id) {
        id
        title
        category
        outcomes
        outcomeTokenMarginalPrices
        outcomeTokenAmounts
        outcomeSlotCount
        openingTimestamp
        resolutionTimestamp
        currentAnswer
        answerFinalizedTimestamp
        scaledLiquidityMeasure
        scaledRunningDailyVolume
        collateralToken
        curatedByDxDao
        question {
          id
          title
          category
          outcomes
          openingTimestamp
        }
        condition {
          id
          resolutionTimestamp
          payouts
        }
      }
    }
    """

    FPMM_TRADES_QUERY = """
    query OmenTrades(
      $first: Int!
      $fpmm: String!
      $outcomeIndex: BigInt!
      $after: BigInt!
      $before: BigInt!
    ) {
      fpmmTrades(
        first: $first
        orderBy: creationTimestamp
        orderDirection: desc
        where: {
          fpmm: $fpmm
          outcomeIndex: $outcomeIndex
          creationTimestamp_gte: $after
          creationTimestamp_lte: $before
        }
      ) {
        id
        fpmm { id }
        title
        collateralToken
        outcomeTokenMarginalPrice
        oldOutcomeTokenMarginalPrice
        type
        creationTimestamp
        collateralAmount
        collateralAmountUSD
        feeAmount
        outcomeIndex
        outcomeTokensTraded
        transactionHash
        creator { id }
      }
    }
    """

    FPMM_ACTIVITY_QUERY = """
    query OmenActivity(
      $first: Int!
      $creator: String!
      $after: BigInt!
      $before: BigInt!
    ) {
      fpmmTrades(
        first: $first
        orderBy: creationTimestamp
        orderDirection: desc
        where: {
          creator: $creator
          creationTimestamp_gte: $after
          creationTimestamp_lte: $before
        }
      ) {
        id
        fpmm { id }
        title
        collateralToken
        outcomeTokenMarginalPrice
        oldOutcomeTokenMarginalPrice
        type
        creationTimestamp
        collateralAmount
        collateralAmountUSD
        feeAmount
        outcomeIndex
        outcomeTokensTraded
        transactionHash
        creator { id }
      }
    }
    """

    TOKEN_SCALE_QUERY = """
    query OmenTokenScale($id: ID!) {
      token(id: $id) {
        id
        scale
      }
    }
    """

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        url, source = self._graphql_url_with_source(required=False)
        health.update(
            {
                "graphql_url_configured": bool(url),
                "graphql_url_source": source,
                "references": list(OMEN_REFERENCES),
                "orderbook_supported": False,
                "copy_trading_supported": bool(self.capabilities.copy_trading),
                "copy_trading_source": "omen_fpmm_creator_trades",
                "copy_trading_coverage": "bounded wallet-filtered public creator rows; simulation-first only",
                "account_recovery_operations": list(self.account_recovery_operations),
                "public_account_endpoints": [
                    "POST <subgraph> fpmmTrades(creator=...)",
                ],
                "live_trading_supported": bool(self.capabilities.live_trading),
                "live_trading_enabled": bool(self.capabilities.live_trading)
                and self.config_bool("live_trading_enabled", False),
                "signed_transaction_submission_enabled": bool(self.capabilities.live_trading)
                and self.config_bool(self._submit_config_key, False),
                "rpc_configured": bool(self._configured_rpc_url),
            }
        )
        return health

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = max(1, min(int(limit or 50), 100))
        data = self._graphql(self.FPMMS_QUERY, {"first": desired})
        markets = self._fpmms_from_payload(data)
        q = str(query or "").strip().lower()
        if q:
            markets = [market for market in markets if self._market_matches_query(market, q)]
        return [self._event_from_fpmm(market) for market in markets[:desired]]

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        fpmm = self._fetch_fpmm(event_id)
        return self._contracts_from_fpmm(fpmm)

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        fpmm_id, outcome_index = self._split_contract_id(contract_id)
        fpmm = self._fetch_fpmm(fpmm_id)
        prices = self._marginal_prices(fpmm)
        if outcome_index >= len(prices) or prices[outcome_index] is None:
            raise MarketConfigurationError(f"Omen price for {contract_id!r} was not available from the subgraph.")
        price = prices[outcome_index]
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=self._contract_id(fpmm_id, outcome_index),
            last=price,
            midpoint=price,
            source="omen_subgraph_outcomeTokenMarginalPrices",
            raw={"fpmm": dict(fpmm), "outcome_index": outcome_index},
        )

    def get_orderbook(self, contract_id: str):
        raise UnsupportedFeatureError(
            self.market_id,
            "orderbook_reading",
            "Omen uses FixedProductMarketMaker AMM pools and the documented subgraph exposes marginal prices, not CLOB depth.",
        )

    def list_trades(
        self,
        contract_id: str,
        *,
        limit: int = 50,
        before: Optional[float] = None,
        after: Optional[float] = None,
    ) -> List[MarketTrade]:
        """Return public Omen FPMM buys and sells from the official subgraph."""

        self.ensure_capability("trade_history")
        fpmm_id, outcome_index = self._split_contract_id(contract_id)
        desired = self._history_limit(limit)
        after_ts = self._history_timestamp(after, "after") if after is not None else 0
        before_ts = (
            self._history_timestamp(before, "before") if before is not None else 253_402_300_799
        )
        if before_ts < after_ts:
            raise MarketConfigurationError("Omen trade history requires before to be at or after after.")

        data = self._graphql(
            self.FPMM_TRADES_QUERY,
            {
                "first": desired,
                "fpmm": fpmm_id,
                "outcomeIndex": str(outcome_index),
                "after": str(after_ts),
                "before": str(before_ts),
            },
        )
        rows = data.get("fpmmTrades")
        if not isinstance(rows, list):
            return []

        canonical = self._contract_id(fpmm_id, outcome_index)
        trades: List[MarketTrade] = []
        token_scales: Dict[str, int] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            row_fpmm = row.get("fpmm")
            if isinstance(row_fpmm, Mapping):
                row_fpmm = row_fpmm.get("id")
            if str(row_fpmm or "").strip().casefold() != fpmm_id.casefold():
                continue
            try:
                row_outcome = int(row.get("outcomeIndex"))
            except (TypeError, ValueError):
                continue
            if row_outcome != outcome_index:
                continue
            side = str(row.get("type") or "").strip().upper()
            if side not in {"BUY", "SELL"}:
                continue
            trade_id = str(row.get("id") or "").strip()
            timestamp = self._optional_history_timestamp(row.get("creationTimestamp"))
            collateral_token = self._collateral_token(row.get("collateralToken"))
            if collateral_token is None:
                continue
            scale = token_scales.get(collateral_token)
            if scale is None:
                scale = self._token_scale(collateral_token)
                token_scales[collateral_token] = scale
            price, size = self._trade_price_and_size(row, token_scale=scale)
            if not trade_id or timestamp is None or price is None or size is None:
                continue
            if timestamp < after_ts or timestamp > before_ts:
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
                    raw={"source": "omen_fpmm_trades", **dict(row)},
                )
            )

        trades.sort(key=lambda trade: float(trade.timestamp or 0), reverse=True)
        return trades[:desired]

    def list_activity(self, wallet_address: str, *, limit: int = 25) -> List[Dict[str, Any]]:
        """Return bounded public FPMM trades created by an EVM wallet.

        The official Omen subgraph exposes ``FPMMTrade.creator`` as an
        ``Account`` relation.  The query is wallet-filtered server-side,
        bounded to a finite timestamp range, and re-checked locally before a
        row becomes eligible for a copy preview.  This is intentionally a
        public activity preview, not a claim of complete account history.
        """

        self.ensure_capability("copy_trading")
        identity = require_activity_identity(self.market_id, wallet_address)
        desired = self._activity_limit(limit)
        data = self._graphql(
            self.FPMM_ACTIVITY_QUERY,
            {
                "first": desired,
                "creator": identity,
                "after": "0",
                "before": "253402300799",
            },
        )
        rows = data.get("fpmmTrades")
        if not isinstance(rows, list):
            return []

        activities: List[Dict[str, Any]] = []
        token_scales: Dict[str, int] = {}
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            creator = row.get("creator")
            if isinstance(creator, Mapping):
                creator = creator.get("id")
            try:
                row_identity = require_activity_identity(self.market_id, creator)
            except MarketConfigurationError:
                continue
            if row_identity != identity:
                continue
            trade_id = str(row.get("id") or "").strip()
            if not trade_id or trade_id in seen:
                continue
            fpmm = row.get("fpmm")
            if isinstance(fpmm, Mapping):
                fpmm = fpmm.get("id")
            fpmm_id = str(fpmm or "").strip()
            if not fpmm_id:
                continue
            try:
                outcome_index = int(row.get("outcomeIndex"))
            except (TypeError, ValueError):
                continue
            if outcome_index < 0:
                continue
            side = str(row.get("type") or "").strip().upper()
            if side not in self.live_order_sides:
                continue
            timestamp = self._optional_history_timestamp(row.get("creationTimestamp"))
            collateral_token = self._collateral_token(row.get("collateralToken"))
            if timestamp is None or collateral_token is None:
                continue
            scale = token_scales.get(collateral_token)
            if scale is None:
                scale = self._token_scale(collateral_token)
                token_scales[collateral_token] = scale
            price, size = self._trade_price_and_size(row, token_scale=scale)
            if price is None or size is None:
                continue
            contract_id = self._contract_id(fpmm_id, outcome_index)
            seen.add(trade_id)
            activities.append(
                {
                    "activityId": f"{self.market_id}:{trade_id}",
                    "proxyWallet": identity,
                    "asset": contract_id,
                    "contract_id": contract_id,
                    "market_id": self.market_id,
                    "side": side,
                    "size": size,
                    "price": price,
                    "timestamp": int(timestamp),
                    "transactionHash": str(row.get("transactionHash") or "").strip(),
                    "source": "omen_fpmm_creator_trades",
                    "creator": identity,
                    "trade_id": trade_id,
                    "raw": dict(row),
                }
            )

        activities.sort(
            key=lambda row: (int(row.get("timestamp") or 0), str(row.get("activityId") or "")),
            reverse=True,
        )
        return activities[:desired]

    def account_recovery(self, operation: str, **kwargs: Any) -> Dict[str, Any]:
        """Read the bounded public FPMM creator-activity feed.

        Omen and the Gnosis alias expose the same official ``FPMMTrade``
        schema.  This operation is deliberately framed as public activity,
        not a claim of complete authenticated account history.
        """

        normalized = str(operation or "").strip().lower()
        if normalized not in self.account_recovery_operations:
            raise MarketConfigurationError(
                f"{self.display_name} account operation must be one of: "
                + ", ".join(self.account_recovery_operations)
                + "."
            )
        self.ensure_capability("copy_trading")
        identity = require_activity_identity(
            self.market_id,
            kwargs.get("wallet") or kwargs.get("address"),
        )
        desired = self._activity_limit(kwargs.get("limit", 25))
        activities = self.list_activity(identity, limit=desired)
        return {
            "source": "omen_fpmm_creator_trades",
            "endpoint": "fpmmTrades(creator=...)",
            "wallet": identity,
            "limit": desired,
            "coverage": "bounded_public_creator_rows",
            "activity": activities,
            "raw": activities,
        }

    def list_candles(
        self,
        contract_id: str,
        *,
        resolution: str = "1h",
        from_timestamp: Optional[float] = None,
        to_timestamp: Optional[float] = None,
    ) -> List[MarketCandle]:
        """Derive bounded OHLCV candles from the official FPMM trade tape."""

        self.ensure_capability("candle_history")
        interval = self._candle_interval(resolution)
        start_ts = (
            self._history_timestamp(from_timestamp, "from_timestamp")
            if from_timestamp is not None
            else None
        )
        end_ts = (
            self._history_timestamp(to_timestamp, "to_timestamp")
            if to_timestamp is not None
            else None
        )
        if start_ts is not None and end_ts is not None and end_ts < start_ts:
            raise MarketConfigurationError(
                "Omen candle history requires to_timestamp to be at or after from_timestamp."
            )

        trades = self.list_trades(
            contract_id,
            limit=self._candle_trade_limit(),
            before=end_ts,
            after=start_ts,
        )
        timestamp_prices: Dict[float, float] = {}
        for trade in trades:
            if trade.timestamp is None:
                continue
            timestamp = float(trade.timestamp)
            prior_price = timestamp_prices.get(timestamp)
            if prior_price is not None and not math.isclose(
                prior_price,
                trade.price,
                rel_tol=0.0,
                abs_tol=1e-15,
            ):
                raise MarketConfigurationError(
                    "Omen cannot derive chronological OHLC from same-second trades with different prices; "
                    "the official subgraph does not expose a block/log ordering key."
                )
            timestamp_prices[timestamp] = trade.price
        buckets: Dict[int, Dict[str, Any]] = {}
        for trade in sorted(trades, key=lambda item: float(item.timestamp or 0)):
            if trade.timestamp is None:
                continue
            bucket_timestamp = int(float(trade.timestamp) // interval * interval)
            bucket = buckets.setdefault(
                bucket_timestamp,
                {
                    "open": trade.price,
                    "high": trade.price,
                    "low": trade.price,
                    "close": trade.price,
                    "volume": 0.0,
                    "trade_ids": [],
                },
            )
            bucket["high"] = max(float(bucket["high"]), trade.price)
            bucket["low"] = min(float(bucket["low"]), trade.price)
            bucket["close"] = trade.price
            bucket["volume"] += trade.size
            bucket["trade_ids"].append(trade.trade_id)

        fpmm_id, outcome_index = self._split_contract_id(contract_id)
        canonical = self._contract_id(fpmm_id, outcome_index)
        normalized_resolution = str(resolution or "").strip().lower()
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
                    "source": "omen_fpmm_trades",
                    "derived": True,
                    "resolution": normalized_resolution,
                    "interval_seconds": interval,
                    "trade_ids": list(bucket["trade_ids"]),
                },
            )
            for bucket_timestamp, bucket in sorted(buckets.items())
        ]

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        self._validate_order(order)
        fpmm_id, outcome_index = self._split_contract_id(order.contract_id)
        average = order.limit_price if order.limit_price is not None else self.get_price(order.contract_id).last
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=self._contract_id(fpmm_id, outcome_index),
            accepted=True,
            message=(
                f"DRY RUN: would record Omen AMM {order.side.upper()} "
                f"for {order.size:.4f} outcome shares"
                + (f" at max probability {float(average):.4f}" if average is not None else "")
            ),
            filled_size=0.0,
            average_price=average,
            raw={"fpmm": fpmm_id, "outcome_index": outcome_index, "dry_run": True},
        )

    def place_live_order(self, order: PaperOrderRequest):
        self.ensure_capability("live_trading")
        self._validate_order(order)
        audit = self.preflight_live_order(order, feature_name="Omen live trading")
        if not self.config_bool(self._submit_config_key, False):
            raise MarketConfigurationError(
                f"{self.display_name} live trading requires {self._submit_config_key}=true after reviewing the signed transaction."
            )
        rpc_url = self._configured_rpc_url
        if not rpc_url:
            raise MarketConfigurationError(
                f"{self.display_name} live orders require {self._rpc_config_key} or evm_rpc_url for transaction submission."
            )
        fpmm_id, outcome_index = self._split_contract_id(order.contract_id)
        signed = str(
            order.metadata.get("signed_transaction") or order.metadata.get("signedTransaction") or ""
        ).strip()
        self._validate_signed_transaction(signed)
        metadata = dict(order.metadata or {})
        target = str(
            metadata.get("transaction_to")
            or metadata.get("to")
            or metadata.get("fpmm_address")
            or ""
        ).strip()
        if not target or target.casefold() != fpmm_id.casefold():
            raise MarketConfigurationError("Omen signed transaction metadata targets a different FPMM market.")
        method = str(metadata.get("method") or "").strip()
        allowed_methods = {"buy", "buyWithHint", "sell", "sellFor"}
        if method not in allowed_methods:
            raise MarketConfigurationError(
                "Omen live orders require reviewed buy/buyWithHint/sell/sellFor method metadata."
            )
        side = str(order.side or "").upper()
        if (side == "BUY" and method not in {"buy", "buyWithHint"}) or (
            side == "SELL" and method not in {"sell", "sellFor"}
        ):
            raise MarketConfigurationError("Omen signed transaction method does not match the requested order side.")
        try:
            reviewed_outcome = int(metadata.get("outcome_index"))
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Omen live orders require reviewed outcome_index metadata.") from exc
        if reviewed_outcome != outcome_index:
            raise MarketConfigurationError("Omen signed transaction metadata targets a different outcome.")
        data = str(metadata.get("data") or metadata.get("calldata") or "").strip()
        if data.startswith("0x"):
            data = data[2:]
        if not data or len(data) < 8 or len(data) % 2 or not re.fullmatch(r"[0-9a-fA-F]+", data):
            raise MarketConfigurationError("Omen live orders require reviewed hexadecimal transaction calldata.")
        response = self._evm_rpc(
            rpc_url,
            "eth_sendRawTransaction",
            [signed],
        )
        if not isinstance(response, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", response):
            raise MarketHTTPError("Omen RPC did not return a valid transaction hash.")
        return {
            "market_id": self.market_id,
            "contract_id": self._contract_id(fpmm_id, outcome_index),
            "live": True,
            "preflight": audit,
            "submission": "evm_rpc_eth_sendRawTransaction",
            "tx_hash": response,
            "fpmm_address": target,
            "method": method,
            "outcome_index": outcome_index,
        }

    def copy_trade_from_activity(self, activity: Mapping[str, Any]):
        self.ensure_capability("copy_trading")
        identity = require_activity_identity(
            self.market_id,
            activity.get("proxyWallet") or activity.get("proxy_wallet") or activity.get("wallet"),
        )
        raw = activity.get("raw") if isinstance(activity.get("raw"), Mapping) else {}
        creator = activity.get("creator") or raw.get("creator")
        if isinstance(creator, Mapping):
            creator = creator.get("id")
        if not creator:
            raise MarketConfigurationError("Omen activity must include its creator identity.")
        creator_identity = require_activity_identity(self.market_id, creator)
        if creator_identity != identity:
            raise MarketConfigurationError("Omen activity creator does not match proxyWallet.")
        contract_id = str(activity.get("asset") or activity.get("contract_id") or "").strip()
        if not contract_id:
            raise MarketConfigurationError("Omen activity has no contract id.")
        fpmm_id, outcome_index = self._split_contract_id(contract_id)
        canonical = self._contract_id(fpmm_id, outcome_index)
        side = str(activity.get("side") or "").strip().upper()
        if side not in self.live_order_sides:
            raise MarketConfigurationError("Omen activity side must be BUY or SELL.")
        size = self._strict_positive_number(activity.get("size"), "activity size")
        price = self._probability(activity.get("price"), allow_zero=False)
        activity_market = str(activity.get("market_id") or activity.get("marketId") or "").strip()
        if activity_market and activity_market != self.market_id:
            raise MarketConfigurationError("Omen activity market id does not match the selected adapter.")
        return self.place_paper_order(
            PaperOrderRequest(
                market_id=self.market_id,
                contract_id=canonical,
                side=side,
                size=size,
                limit_price=price,
                metadata={
                    "activity": dict(activity),
                    "source": "omen_public_creator_trades",
                    "activity_identity": identity,
                },
            )
        )

    def _fetch_fpmm(self, fpmm_id: str) -> Mapping[str, Any]:
        market_id = str(fpmm_id or "").strip()
        if not market_id:
            raise MarketConfigurationError("Omen market id cannot be empty.")
        data = self._graphql(self.FPMM_QUERY, {"id": market_id})
        fpmm = data.get("fixedProductMarketMaker")
        if isinstance(fpmm, Mapping):
            return fpmm
        raise MarketConfigurationError(f"Omen market {market_id!r} was not found.")

    @property
    def _rpc_config_key(self) -> str:
        return "gnosis_rpc_url" if self.market_id == "gnosis_prediction_markets" else "omen_rpc_url"

    @property
    def _submit_config_key(self) -> str:
        return (
            "gnosis_submit_signed_transactions"
            if self.market_id == "gnosis_prediction_markets"
            else "omen_submit_signed_transactions"
        )

    @property
    def _configured_rpc_url(self) -> str:
        value = (
            self.config.get(self._rpc_config_key)
            or self.config.get("evm_rpc_url")
            or self.config.get("web3_rpc_url")
        )
        text = str(value or "").strip().rstrip("/")
        if not text:
            return ""
        parsed = urlsplit(text)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
            raise MarketConfigurationError(
                f"{self.display_name} RPC URL must be an absolute http(s) URL without query or fragment."
            )
        return text

    @staticmethod
    def _validate_signed_transaction(value: str) -> None:
        if not value or not re.fullmatch(r"0x[0-9a-fA-F]+", value) or len(value) % 2:
            raise MarketConfigurationError(
                "Omen live orders require an externally signed raw EVM transaction in metadata['signed_transaction']."
            )
        if len(value) > 2_000_002 or len(value) < 130:
            raise MarketConfigurationError("Omen signed transaction has an invalid size.")

    def _evm_rpc(self, url: str, method: str, params: List[Any]) -> Any:
        payload = self.runtime.request_json(
            "POST",
            url,
            json_body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            headers={"Content-Type": "application/json"},
        )
        if not isinstance(payload, Mapping):
            raise MarketHTTPError(f"{self.display_name} RPC response was not a JSON object.")
        if payload.get("error"):
            raise MarketHTTPError(f"{self.display_name} RPC error.")
        return payload.get("result")

    def _event_from_fpmm(self, fpmm: Mapping[str, Any]) -> MarketEvent:
        fpmm_id = self._fpmm_id(fpmm)
        return MarketEvent(
            market_id=self.market_id,
            event_id=fpmm_id,
            title=self._title(fpmm),
            url=f"https://omen.eth.limo/#/{fpmm_id}" if fpmm_id else "https://omen.eth.limo",
            status=self._status(fpmm),
            raw=dict(fpmm),
        )

    def _contracts_from_fpmm(self, fpmm: Mapping[str, Any]) -> List[MarketContract]:
        fpmm_id = self._fpmm_id(fpmm)
        title = self._title(fpmm)
        prices = self._marginal_prices(fpmm)
        contracts: List[MarketContract] = []
        for index, outcome in enumerate(self._outcome_names(fpmm)):
            contracts.append(
                MarketContract(
                    market_id=self.market_id,
                    contract_id=self._contract_id(fpmm_id, index),
                    event_id=fpmm_id,
                    title=f"{title} - {outcome}",
                    outcome=outcome,
                    url=f"https://omen.eth.limo/#/{fpmm_id}" if fpmm_id else "https://omen.eth.limo",
                    status=self._status(fpmm),
                    raw={
                        "fpmm": dict(fpmm),
                        "outcome_index": index,
                        "marginal_price": prices[index] if index < len(prices) else None,
                    },
                )
            )
        return contracts

    def _validate_order(self, order: PaperOrderRequest) -> None:
        self.ensure_order_market(order)
        self._split_contract_id(order.contract_id)
        side = str(order.side or "").upper()
        if side not in {"BUY", "SELL"}:
            raise MarketConfigurationError("Omen paper order side must be BUY or SELL.")
        if not self._is_positive_number(order.size):
            raise MarketConfigurationError("Omen paper order size must be positive.")
        if order.limit_price is not None:
            self._probability(order.limit_price, allow_zero=False)

    @staticmethod
    def _fpmms_from_payload(data: Mapping[str, Any]) -> List[Mapping[str, Any]]:
        markets = data.get("fixedProductMarketMakers")
        return [market for market in markets if isinstance(market, Mapping)] if isinstance(markets, list) else []

    @staticmethod
    def _market_matches_query(fpmm: Mapping[str, Any], query: str) -> bool:
        values = [
            fpmm.get("id"),
            fpmm.get("title"),
            fpmm.get("category"),
            " ".join(OmenAdapter._outcome_names(fpmm)),
        ]
        question = fpmm.get("question")
        if isinstance(question, Mapping):
            values.extend([question.get("title"), question.get("category")])
        return query in " ".join(str(value or "") for value in values).lower()

    @staticmethod
    def _fpmm_id(fpmm: Mapping[str, Any]) -> str:
        return str(fpmm.get("id") or "").strip()

    @staticmethod
    def _title(fpmm: Mapping[str, Any]) -> str:
        question = fpmm.get("question")
        if isinstance(question, Mapping) and question.get("title"):
            return str(question["title"])
        return str(fpmm.get("title") or OmenAdapter._fpmm_id(fpmm))

    @staticmethod
    def _status(fpmm: Mapping[str, Any]) -> str:
        if fpmm.get("resolutionTimestamp") or (
            isinstance(fpmm.get("condition"), Mapping) and fpmm["condition"].get("resolutionTimestamp")
        ):
            return "resolved"
        if fpmm.get("currentAnswer") or fpmm.get("answerFinalizedTimestamp"):
            return "answering"
        return "active"

    @staticmethod
    def _outcome_names(fpmm: Mapping[str, Any]) -> List[str]:
        outcomes: Any = fpmm.get("outcomes")
        question = fpmm.get("question")
        if not isinstance(outcomes, list) and isinstance(question, Mapping):
            outcomes = question.get("outcomes")
        if isinstance(outcomes, list) and outcomes:
            return [str(outcome) for outcome in outcomes]
        try:
            count = int(fpmm.get("outcomeSlotCount") or 0)
        except (TypeError, ValueError):
            count = 0
        if count == 2:
            return ["Yes", "No"]
        return [f"Outcome {index}" for index in range(max(count, 0))]

    @staticmethod
    def _marginal_prices(fpmm: Mapping[str, Any]) -> List[Optional[float]]:
        raw = fpmm.get("outcomeTokenMarginalPrices")
        if not isinstance(raw, list):
            return []
        return [OmenAdapter._optional_probability(value) for value in raw]

    @staticmethod
    def _history_limit(value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Omen trade limit must be an integer between 1 and 1000.") from exc
        if parsed < 1 or parsed > 1000:
            raise MarketConfigurationError("Omen trade limit must be between 1 and 1000.")
        return parsed

    @staticmethod
    def _activity_limit(value: Any) -> int:
        if isinstance(value, bool):
            raise MarketConfigurationError("Omen activity limit must be an integer.")
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise MarketConfigurationError("Omen activity limit must be an integer.") from exc
        if parsed < 1 or parsed > 100:
            raise MarketConfigurationError("Omen activity limit must be between 1 and 100.")
        return parsed

    @staticmethod
    def _strict_positive_number(value: Any, label: str) -> float:
        if isinstance(value, bool):
            raise MarketConfigurationError(f"Omen {label} must be positive and finite.")
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise MarketConfigurationError(f"Omen {label} must be positive and finite.") from exc
        if not math.isfinite(parsed) or parsed <= 0:
            raise MarketConfigurationError(f"Omen {label} must be positive and finite.")
        return parsed

    @staticmethod
    def _history_timestamp(value: Any, label: str) -> int:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"Omen {label} timestamp must be numeric epoch seconds.") from exc
        if not math.isfinite(parsed) or parsed < 0:
            raise MarketConfigurationError(
                f"Omen {label} timestamp must be a finite non-negative epoch second."
            )
        return int(parsed)

    @staticmethod
    def _optional_history_timestamp(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) and parsed >= 0 else None

    @staticmethod
    def _candle_interval(resolution: str) -> int:
        intervals = {
            "1m": 60,
            "5m": 300,
            "15m": 900,
            "30m": 1800,
            "1h": 3600,
            "4h": 14_400,
            "1d": 86_400,
            "1w": 604_800,
        }
        normalized = str(resolution or "").strip().lower()
        try:
            return intervals[normalized]
        except KeyError as exc:
            raise MarketConfigurationError(
                f"Omen candle resolution must be one of: {', '.join(intervals)}."
            ) from exc

    def _candle_trade_limit(self) -> int:
        key = (
            "gnosis_candle_trade_limit"
            if self.market_id == "gnosis_prediction_markets"
            else "omen_candle_trade_limit"
        )
        return self._history_limit(self.config.get(key, 1000))

    @staticmethod
    def _collateral_token(value: Any) -> Optional[str]:
        token = str(value or "").strip().lower()
        if not re.fullmatch(r"0x[0-9a-f]{40}", token):
            return None
        return token

    def _token_scale(self, collateral_token: str) -> int:
        cache = getattr(self, "_omen_token_scale_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._omen_token_scale_cache = cache
        cached = cache.get(collateral_token)
        if isinstance(cached, int):
            return cached

        data = self._graphql(self.TOKEN_SCALE_QUERY, {"id": collateral_token})
        token = data.get("token")
        if not isinstance(token, Mapping):
            raise MarketConfigurationError(
                f"{self.display_name} token scale was not indexed for collateral {collateral_token}."
            )
        returned_id = str(token.get("id") or "").strip().lower()
        try:
            scale = int(token.get("scale"))
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(
                f"{self.display_name} token scale for collateral {collateral_token} was invalid."
            ) from exc
        if returned_id != collateral_token or scale <= 0 or scale > 10**30:
            raise MarketConfigurationError(
                f"{self.display_name} token scale for collateral {collateral_token} was invalid."
            )
        cache[collateral_token] = scale
        return scale

    def _trade_price_and_size(
        self,
        row: Mapping[str, Any],
        *,
        token_scale: int,
    ) -> Tuple[Optional[float], Optional[float]]:
        try:
            collateral = int(row.get("collateralAmount"))
            outcome_tokens = int(row.get("outcomeTokensTraded"))
        except (TypeError, ValueError):
            return None, None
        if (
            collateral <= 0
            or outcome_tokens <= 0
            or collateral.bit_length() > 256
            or outcome_tokens.bit_length() > 256
        ):
            return None, None
        price = collateral / outcome_tokens
        if not math.isfinite(price) or price <= 0 or price > 1:
            return None, None
        size = outcome_tokens / float(token_scale)
        if not math.isfinite(size) or size <= 0:
            return None, None
        return price, size

    @staticmethod
    def _split_contract_id(contract_id: str) -> Tuple[str, int]:
        raw = str(contract_id or "").strip()
        if ":" not in raw:
            raise MarketConfigurationError("Omen contract id must be FPMM_ID:OUTCOME_INDEX.")
        market_id, outcome = raw.rsplit(":", 1)
        try:
            index = int(outcome)
        except ValueError as exc:
            raise MarketConfigurationError("Omen outcome index must be an integer.") from exc
        if not market_id.strip() or index < 0:
            raise MarketConfigurationError("Omen contract id must include a market id and non-negative outcome index.")
        return market_id.strip(), index

    @staticmethod
    def _contract_id(fpmm_id: str, outcome_index: int) -> str:
        return f"{fpmm_id}:{int(outcome_index)}"


class GnosisPredictionMarketsAdapter(OmenAdapter):
    """Gnosis prediction-market alias over the official Omen FPMM indexer.

    Gnosis' currently supported prediction-market surface is Omen/Presagio;
    the market entities and lifecycle are the same FixedProductMarketMaker
    schema.  Keeping a separate adapter identity preserves market-scoped
    configuration and diagnostics without pretending there is a second CLOB.
    """

    metadata = get_market_metadata("gnosis_prediction_markets")
    graphql_config_key = "gnosis_subgraph_url"
    graphql_env_vars = ("GNOSIS_SUBGRAPH_URL", "OMEN_SUBGRAPH_URL")

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        url, source = self._graphql_url_with_source(required=False)
        health.update(
            {
                "graphql_url_configured": bool(url),
                "graphql_url_source": source,
                "references": list(GNOSIS_REFERENCES),
                "alias_of": "omen",
                "live_trading_enabled": False,
            }
        )
        return health


class ZeitgeistAdapter(_GraphQLAdapter):
    """Zeitgeist adapter using indexer reads and guarded signed HybridRouter extrinsics.

    The adapter never creates keys, signs calls, approves collateral, or settles
    positions.  Live forwarding is limited to an operator-reviewed, externally
    signed ``HybridRouter.buy``/``sell`` extrinsic whose review metadata matches
    the selected market/outcome and the current runtime spec version.
    """

    metadata = get_market_metadata("zeitgeist")
    graphql_config_key = "zeitgeist_indexer_url"
    graphql_env_vars = ("ZEITGEIST_INDEXER_URL",)
    default_graphql_url = DEFAULT_ZEITGEIST_INDEXER_URL

    MARKETS_QUERY = """
    query ZeitgeistMarkets($limit: Int!, $offset: Int!) {
      markets(limit: $limit, offset: $offset) {
        id
        marketId
        question
        description
        slug
        status
        resolvedOutcome
        outcomeAssets
        marketType {
          categorical
          scalar
        }
        categories {
          ticker
          name
          color
        }
        pool {
          id
          poolId
          poolStatus
          baseAsset
          volume
          ztgQty
          weights {
            assetId
            len
          }
        }
      }
    }
    """

    MARKET_QUERY = """
    query ZeitgeistMarket($marketId: Int!) {
      markets(limit: 1, where: { marketId_eq: $marketId }) {
        id
        marketId
        question
        description
        slug
        status
        resolvedOutcome
        outcomeAssets
        marketType {
          categorical
          scalar
        }
        categories {
          ticker
          name
          color
        }
        pool {
          id
          poolId
          poolStatus
          baseAsset
          volume
          ztgQty
          weights {
            assetId
            len
          }
        }
      }
    }
    """

    ASSET_QUERY = """
    query ZeitgeistAsset($assetId: String!) {
      assets(limit: 1, where: { assetId_eq: $assetId }) {
        id
        assetId
        poolId
        price
        amountInPool
      }
    }
    """

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        url, source = self._graphql_url_with_source(required=False)
        rpc_url, rpc_source = self._configured_rpc_url()
        health.update(
            {
                "indexer_url_configured": bool(url),
                "indexer_url_source": source,
                "references": list(ZEITGEIST_REFERENCES),
                "orderbook_supported": False,
                "live_trading_supported": bool(self.capabilities.live_trading),
                "live_trading_enabled": bool(self.capabilities.live_trading)
                and self.config_bool("live_trading_enabled", False),
                "signed_extrinsic_submission_enabled": bool(self.capabilities.live_trading)
                and self.config_bool("zeitgeist_submit_signed_extrinsics", False),
                "rpc_configured": bool(rpc_url),
                "rpc_url_source": rpc_source,
                "hybrid_router_pallet": "HybridRouter",
                "hybrid_router_calls": ["buy", "sell"],
                "wallet_signing_required": True,
                "settlement_supported": False,
            }
        )
        return health

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = max(1, min(int(limit or 50), 100))
        data = self._graphql(self.MARKETS_QUERY, {"limit": desired, "offset": 0})
        markets = self._markets_from_payload(data)
        status_filter = str(self.config.get("zeitgeist_market_status") or "").strip().lower()
        if status_filter:
            markets = [market for market in markets if str(market.get("status") or "").lower() == status_filter]
        q = str(query or "").strip().lower()
        if q:
            markets = [market for market in markets if self._market_matches_query(market, q)]
        return [self._event_from_market(market) for market in markets[:desired]]

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        market = self._fetch_market(event_id)
        return self._contracts_from_market(market)

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        market_id, outcome_index = self._split_contract_id(contract_id)
        market = self._fetch_market(market_id)
        asset_id = self._asset_id_for_outcome(market, outcome_index)
        asset = self._fetch_asset(asset_id)
        price = self._optional_probability(asset.get("price"))
        if price is None:
            raise MarketConfigurationError(f"Zeitgeist asset price for {asset_id!r} was not available from the indexer.")
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=self._contract_id(str(self._market_id(market)), outcome_index),
            last=price,
            midpoint=price,
            source="zeitgeist_indexer_asset_price",
            raw={"market": dict(market), "asset": dict(asset), "outcome_index": outcome_index},
        )

    def get_orderbook(self, contract_id: str):
        raise UnsupportedFeatureError(
            self.market_id,
            "orderbook_reading",
            "Zeitgeist indexer support currently exposes market, pool, and asset prices, not CLOB depth.",
        )

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        self._validate_order(order)
        market_id, outcome_index = self._split_contract_id(order.contract_id)
        average = order.limit_price if order.limit_price is not None else self.get_price(order.contract_id).last
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=self._contract_id(market_id, outcome_index),
            accepted=True,
            message=(
                f"DRY RUN: would simulate Zeitgeist {order.side.upper()} "
                f"for {order.size:.4f} outcome shares"
                + (f" at probability {float(average):.4f}" if average is not None else "")
            ),
            filled_size=0.0,
            average_price=average,
            raw={"market_id": market_id, "outcome_index": outcome_index, "dry_run": True},
        )

    def place_live_order(self, order: PaperOrderRequest):
        audit = self.preflight_live_order(order)
        self._validate_order(order)
        if not self.config_bool("zeitgeist_submit_signed_extrinsics", False):
            raise MarketConfigurationError(
                "Zeitgeist live trading requires zeitgeist_submit_signed_extrinsics=true after reviewing the signed HybridRouter extrinsic."
            )
        rpc_url, _source = self._configured_rpc_url()
        if not rpc_url:
            raise MarketConfigurationError(
                "Zeitgeist live orders require zeitgeist_rpc_url or substrate_rpc_url for transaction submission."
            )

        market_id, outcome_index = self._split_contract_id(order.contract_id)
        market = self._fetch_market(market_id)
        asset_id = self._asset_id_for_outcome(market, outcome_index)
        review = self._validate_reviewed_hybrid_router_order(order, market, asset_id, outcome_index)

        runtime_version = self._substrate_rpc(rpc_url, "state_getRuntimeVersion", [])
        if not isinstance(runtime_version, Mapping):
            raise MarketHTTPError("Zeitgeist runtime version response was not an object.")
        current_spec_version = self._positive_integer(runtime_version.get("specVersion"), "runtime spec version")
        if current_spec_version != review["runtime_spec_version"]:
            raise MarketConfigurationError(
                "Zeitgeist signed extrinsic review runtime_spec_version does not match the connected chain."
            )

        result = self._substrate_rpc(rpc_url, "author_submitExtrinsic", [review["signed_extrinsic"]])
        if not isinstance(result, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", result):
            raise MarketHTTPError("Zeitgeist author_submitExtrinsic returned an invalid extrinsic hash.")
        return {
            "live": True,
            "market_id": self.market_id,
            "contract_id": order.contract_id,
            "side": order.side,
            "submission": "substrate_rpc_author_submitExtrinsic",
            "extrinsic_hash": result,
            "pallet": "HybridRouter",
            "call": review["call"],
            "market_numeric_id": review["market_id"],
            "outcome_index": review["outcome_index"],
            "strategy": review["strategy"],
            "runtime_spec_version": review["runtime_spec_version"],
            "audit": audit,
        }

    def copy_trade_from_activity(self, activity: Mapping[str, Any]):
        raise UnsupportedFeatureError(
            self.market_id,
            "copy_trading",
            "Zeitgeist copy trading is unsupported because this adapter has no official account activity mirroring API.",
        )

    def _fetch_market(self, market_id: Any) -> Mapping[str, Any]:
        try:
            parsed_market_id = int(str(market_id).strip())
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Zeitgeist market id must be an integer.") from exc
        data = self._graphql(self.MARKET_QUERY, {"marketId": parsed_market_id})
        markets = self._markets_from_payload(data)
        if markets:
            return markets[0]
        raise MarketConfigurationError(f"Zeitgeist market {parsed_market_id!r} was not found.")

    def _fetch_asset(self, asset_id: str) -> Mapping[str, Any]:
        data = self._graphql(self.ASSET_QUERY, {"assetId": asset_id})
        assets = data.get("assets")
        if isinstance(assets, list) and assets and isinstance(assets[0], Mapping):
            return assets[0]
        raise MarketConfigurationError(f"Zeitgeist asset {asset_id!r} was not found.")

    def _event_from_market(self, market: Mapping[str, Any]) -> MarketEvent:
        market_id = str(self._market_id(market))
        return MarketEvent(
            market_id=self.market_id,
            event_id=market_id,
            title=str(market.get("question") or market.get("description") or market_id),
            url=self._market_url(market),
            status=str(market.get("status") or "").strip().lower(),
            raw=dict(market),
        )

    def _contracts_from_market(self, market: Mapping[str, Any]) -> List[MarketContract]:
        market_id = str(self._market_id(market))
        title = str(market.get("question") or market_id)
        status = str(market.get("status") or "").strip().lower()
        assets = self._outcome_assets(market)
        categories = self._categories(market)
        contracts: List[MarketContract] = []
        for index, asset_id in enumerate(assets):
            outcome = self._category_name(categories, index) or asset_id
            contracts.append(
                MarketContract(
                    market_id=self.market_id,
                    contract_id=self._contract_id(market_id, index),
                    event_id=market_id,
                    title=f"{title} - {outcome}",
                    outcome=outcome,
                    url=self._market_url(market),
                    status=status,
                    raw={"market": dict(market), "asset_id": asset_id, "outcome_index": index},
                )
            )
        return contracts

    def _validate_order(self, order: PaperOrderRequest) -> None:
        self.ensure_order_market(order)
        self._split_contract_id(order.contract_id)
        side = str(order.side or "").upper()
        if side not in {"BUY", "SELL"}:
            raise MarketConfigurationError("Zeitgeist paper order side must be BUY or SELL.")
        if not self._is_positive_number(order.size):
            raise MarketConfigurationError("Zeitgeist paper order size must be positive.")
        if order.limit_price is not None:
            self._probability(order.limit_price, allow_zero=False)

    def _validate_reviewed_hybrid_router_order(
        self,
        order: PaperOrderRequest,
        market: Mapping[str, Any],
        asset_id: str,
        outcome_index: int,
    ) -> Dict[str, Any]:
        metadata = order.metadata if isinstance(order.metadata, Mapping) else {}
        pallet = str(metadata.get("pallet") or "").strip()
        if pallet != "HybridRouter":
            raise MarketConfigurationError(
                "Zeitgeist live orders require reviewed metadata pallet='HybridRouter'."
            )
        call = str(metadata.get("call") or metadata.get("method") or "").strip().lower()
        expected_call = str(order.side or "").strip().lower()
        if call not in {"buy", "sell"} or call != expected_call:
            raise MarketConfigurationError(
                "Zeitgeist HybridRouter call must be buy for BUY orders or sell for SELL orders."
            )

        reviewed_market_id = self._positive_integer(metadata.get("market_id"), "reviewed market id", allow_zero=True)
        parsed_market_id = self._positive_integer(self._market_id(market), "market id", allow_zero=True)
        if reviewed_market_id != parsed_market_id:
            raise MarketConfigurationError("Zeitgeist reviewed market_id does not match the selected contract.")

        reviewed_outcome_index = self._positive_integer(
            metadata.get("outcome_index"), "reviewed outcome index", allow_zero=True
        )
        if reviewed_outcome_index != outcome_index:
            raise MarketConfigurationError("Zeitgeist reviewed outcome_index does not match the selected contract.")
        reviewed_asset = str(metadata.get("asset") or metadata.get("asset_id") or "").strip()
        if reviewed_asset != asset_id:
            raise MarketConfigurationError("Zeitgeist reviewed asset does not match the selected outcome asset.")

        expected_asset_count = len(self._outcome_assets(market))
        asset_count = self._positive_integer(metadata.get("asset_count"), "asset_count")
        if asset_count != expected_asset_count or asset_count > 8:
            raise MarketConfigurationError(
                "Zeitgeist HybridRouter asset_count must match the market outcome count and be at most 8."
            )
        amount_in = self._positive_integer(metadata.get("amount_in"), "amount_in")
        price_key = "max_price" if call == "buy" else "min_price"
        price_limit = self._positive_integer(metadata.get("price_limit", metadata.get(price_key)), price_key)
        strategy = str(metadata.get("strategy") or "").strip()
        if strategy not in {"ImmediateOrCancel", "LimitOrder"}:
            raise MarketConfigurationError(
                "Zeitgeist HybridRouter strategy must be ImmediateOrCancel or LimitOrder."
            )
        orders = metadata.get("orders", [])
        if not isinstance(orders, list) or len(orders) > 64:
            raise MarketConfigurationError("Zeitgeist HybridRouter orders must be a list of at most 64 order ids.")
        parsed_orders = [self._positive_integer(value, "order id", allow_zero=True) for value in orders]
        if parsed_orders != sorted(parsed_orders):
            raise MarketConfigurationError("Zeitgeist HybridRouter order ids must be sorted for deterministic routing.")

        runtime_spec_version = self._positive_integer(metadata.get("runtime_spec_version"), "runtime spec version")
        signed_extrinsic = self._validate_signed_extrinsic(metadata.get("signed_extrinsic"))
        self._validate_live_market_metadata(market, metadata)
        return {
            "market_id": reviewed_market_id,
            "outcome_index": reviewed_outcome_index,
            "asset_count": asset_count,
            "asset": reviewed_asset,
            "amount_in": amount_in,
            "price_limit": price_limit,
            "orders": parsed_orders,
            "strategy": strategy,
            "call": call,
            "runtime_spec_version": runtime_spec_version,
            "signed_extrinsic": signed_extrinsic,
        }

    def _validate_live_market_metadata(self, market: Mapping[str, Any], metadata: Mapping[str, Any]) -> None:
        """Hook for aliases that require additional reviewed market metadata."""

    @staticmethod
    def _positive_integer(value: Any, label: str, *, allow_zero: bool = False) -> int:
        if isinstance(value, bool) or value is None:
            raise MarketConfigurationError(f"Zeitgeist {label} must be an integer.")
        text = str(value).strip()
        if not re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)", text):
            raise MarketConfigurationError(f"Zeitgeist {label} must be an integer.")
        number = int(text, 16) if text.lower().startswith("0x") else int(text, 10)
        if number < 0 or (number == 0 and not allow_zero):
            raise MarketConfigurationError(f"Zeitgeist {label} must be positive.")
        return number

    @staticmethod
    def _validate_signed_extrinsic(value: Any) -> str:
        if not isinstance(value, str) or value != value.strip():
            raise MarketConfigurationError("Zeitgeist signed_extrinsic must be canonical 0x-prefixed hex.")
        signed = value.strip()
        if not re.fullmatch(r"0x[0-9a-fA-F]+", signed) or len(signed[2:]) % 2:
            raise MarketConfigurationError("Zeitgeist signed_extrinsic must be canonical 0x-prefixed hex.")
        byte_length = (len(signed) - 2) // 2
        if byte_length < 32 or byte_length > 1_000_000:
            raise MarketConfigurationError("Zeitgeist signed_extrinsic must be between 32 bytes and 1 MB.")
        return signed

    def _configured_rpc_url(self) -> Tuple[str, str]:
        configured = self.config.get("substrate_rpc_url")
        if configured and str(configured).strip():
            return str(configured).strip().rstrip("/"), "config:substrate_rpc_url"
        credential = self.resolve_credential(
            "zeitgeist_rpc_url",
            ("ZEITGEIST_RPC_URL", "SUBSTRATE_RPC_URL"),
            required=False,
            label="ZEITGEIST_RPC_URL",
        )
        if credential and credential.value.strip():
            return credential.value.strip().rstrip("/"), credential.source
        return "", "missing"

    def _substrate_rpc(self, url: str, method: str, params: Sequence[Any]) -> Any:
        payload = self.runtime.request_json(
            "POST",
            url,
            json_body={"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)},
            headers={"Content-Type": "application/json"},
        )
        if not isinstance(payload, Mapping):
            raise MarketHTTPError("Zeitgeist RPC response was not a JSON object.")
        if payload.get("error"):
            raise MarketHTTPError(f"Zeitgeist RPC {method} failed: {payload['error']}")
        if "result" not in payload:
            raise MarketHTTPError(f"Zeitgeist RPC {method} response did not include a result.")
        return payload["result"]

    @staticmethod
    def _markets_from_payload(data: Mapping[str, Any]) -> List[Mapping[str, Any]]:
        markets = data.get("markets")
        return [market for market in markets if isinstance(market, Mapping)] if isinstance(markets, list) else []

    @staticmethod
    def _market_matches_query(market: Mapping[str, Any], query: str) -> bool:
        values = [
            market.get("id"),
            market.get("marketId"),
            market.get("question"),
            market.get("description"),
            market.get("slug"),
            market.get("status"),
            " ".join(ZeitgeistAdapter._outcome_assets(market)),
            " ".join(
                str(category.get("name") or category.get("ticker") or "")
                for category in ZeitgeistAdapter._categories(market)
            ),
        ]
        return query in " ".join(str(value or "") for value in values).lower()

    @staticmethod
    def _market_id(market: Mapping[str, Any]) -> Any:
        return market.get("marketId") if market.get("marketId") is not None else market.get("id")

    @staticmethod
    def _outcome_assets(market: Mapping[str, Any]) -> List[str]:
        assets = market.get("outcomeAssets")
        return [str(asset) for asset in assets if asset is not None] if isinstance(assets, list) else []

    @staticmethod
    def _categories(market: Mapping[str, Any]) -> List[Mapping[str, Any]]:
        categories = market.get("categories")
        return [category for category in categories if isinstance(category, Mapping)] if isinstance(categories, list) else []

    @staticmethod
    def _category_name(categories: List[Mapping[str, Any]], index: int) -> str:
        if index >= len(categories):
            return ""
        category = categories[index]
        return str(category.get("name") or category.get("ticker") or "").strip()

    @staticmethod
    def _asset_id_for_outcome(market: Mapping[str, Any], outcome_index: int) -> str:
        assets = ZeitgeistAdapter._outcome_assets(market)
        if outcome_index >= len(assets):
            raise MarketConfigurationError("Zeitgeist outcome index is outside this market's asset list.")
        return assets[outcome_index]

    @staticmethod
    def _market_url(market: Mapping[str, Any]) -> str:
        market_id = ZeitgeistAdapter._market_id(market)
        slug = str(market.get("slug") or "").strip()
        if slug:
            return f"https://app.zeitgeist.pm/markets/{slug}"
        return f"https://app.zeitgeist.pm/markets/{market_id}" if market_id is not None else "https://zeitgeist.pm"

    @staticmethod
    def _split_contract_id(contract_id: str) -> Tuple[str, int]:
        raw = str(contract_id or "").strip()
        if ":" not in raw:
            raise MarketConfigurationError("Zeitgeist contract id must be MARKET_ID:OUTCOME_INDEX.")
        market_id, outcome = raw.rsplit(":", 1)
        try:
            index = int(outcome)
        except ValueError as exc:
            raise MarketConfigurationError("Zeitgeist outcome index must be an integer.") from exc
        if not market_id.strip() or index < 0:
            raise MarketConfigurationError(
                "Zeitgeist contract id must include a market id and non-negative outcome index."
            )
        return market_id.strip(), index

    @staticmethod
    def _contract_id(market_id: str, outcome_index: int) -> str:
        return f"{market_id}:{int(outcome_index)}"


class ZeitgeistSdkMarketsAdapter(ZeitgeistAdapter):
    """Zeitgeist SDK/Markets alias using the same documented indexer contract.

    The SDK's market-fetching surface is the same Subsquid GraphQL schema used
    by the primary Zeitgeist adapter, but it is exposed as a separate catalog
    target so configuration and health diagnostics remain explicit.
    """

    metadata = get_market_metadata("zeitgeist_sdk_markets")
    graphql_config_key = "zeitgeist_sdk_indexer_url"
    graphql_env_vars = ("ZEITGEIST_SDK_INDEXER_URL", "ZEITGEIST_INDEXER_URL")


class ZeitgeistPredictionPoolsAdapter(ZeitgeistAdapter):
    """Pool-scoped Zeitgeist adapter using the documented market/pool indexer shape.

    Zeitgeist exposes pool metadata alongside each market and asset price.  This
    adapter makes that pool contract explicit: discovery and paper quotes are
    accepted only for markets with a valid pool identifier, while wallet
    settlement and orderbook depth remain intentionally unsupported.
    """

    metadata = get_market_metadata("zeitgeist_prediction_pools")
    graphql_config_key = "zeitgeist_pools_indexer_url"
    graphql_env_vars = ("ZEITGEIST_POOLS_INDEXER_URL", "ZEITGEIST_INDEXER_URL")
    default_graphql_url = DEFAULT_ZEITGEIST_INDEXER_URL

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        health.update(
            {
                "alias_of": "zeitgeist",
                "pool_schema_supported": True,
                "pool_accounting_supported": False,
                "pool_settlement_supported": False,
            }
        )
        return health

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        desired = max(1, min(int(limit or 50), 100))
        events = super().list_events(query, 100)
        pooled = [event for event in events if isinstance(event.raw.get("pool"), Mapping)]
        return pooled[:desired]

    def _fetch_market(self, market_id: Any) -> Mapping[str, Any]:
        market = super()._fetch_market(market_id)
        pool = market.get("pool")
        if not isinstance(pool, Mapping) or pool.get("poolId") is None:
            raise MarketConfigurationError(
                f"Zeitgeist prediction-pool market {market_id!r} did not include a valid pool identifier."
            )
        return market

    def _validate_live_market_metadata(self, market: Mapping[str, Any], metadata: Mapping[str, Any]) -> None:
        pool = market.get("pool")
        if not isinstance(pool, Mapping) or pool.get("poolId") is None:
            raise MarketConfigurationError("Zeitgeist prediction-pool live orders require a valid pool identifier.")
        reviewed_pool_id = self._positive_integer(metadata.get("pool_id"), "reviewed pool id", allow_zero=True)
        pool_id = self._positive_integer(pool.get("poolId"), "pool id", allow_zero=True)
        if reviewed_pool_id != pool_id:
            raise MarketConfigurationError("Zeitgeist reviewed pool_id does not match the selected pool.")
