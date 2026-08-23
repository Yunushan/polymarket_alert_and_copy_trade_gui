from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .base import MarketAdapter
from .catalog import get_market_metadata
from .errors import MarketConfigurationError, MarketHTTPError, UnsupportedFeatureError
from .types import (
    MarketContract,
    MarketEvent,
    PaperOrderRequest,
    PaperOrderResult,
    PriceSnapshot,
)


DEFAULT_AZURO_BASE_URL = "https://api.onchainfeed.org/api/v1/public"
DEFAULT_AZURO_WS_URL = "wss://streams.onchainfeed.org/v1/streams/feed"
DEFAULT_AZURO_ENVIRONMENT = "PolygonUSDT"
DEFAULT_AZURO_CHAIN_ID = 137
AZURO_ODDS_SCALE = 10**12
AZURO_ACCOUNT_OPERATIONS = ("bet_history",)
AZURO_BET_HISTORY_PAGE_MAX = 1000
AZURO_BETTOR_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
AZURO_GRAPH_ENDPOINTS = {
    "PolygonUSDT": "https://thegraph.onchainfeed.org/subgraphs/name/azuro-protocol/azuro-api-polygon-v3",
    "PolygonAmoyUSDT": "https://thegraph.onchainfeed.org/subgraphs/name/azuro-protocol/azuro-api-polygon-amoy-dev-v3",
    "GnosisXDAI": "https://thegraph.onchainfeed.org/subgraphs/name/azuro-protocol/azuro-api-gnosis-v3",
    "GnosisDevXDAI": "https://thegraph.onchainfeed.org/subgraphs/name/azuro-protocol/azuro-api-gnosis-dev-v3",
    "BaseWETH": "https://thegraph.onchainfeed.org/subgraphs/name/azuro-protocol/azuro-api-base-v3",
    "BaseSepoliaWETH": "https://thegraph.onchainfeed.org/subgraphs/name/azuro-protocol/azuro-api-base-sepolia-dev-v3",
    "ChilizWCHZ": "https://thegraph.onchainfeed.org/subgraphs/name/azuro-protocol/azuro-api-chiliz-v3",
    "ChilizSpicyWCHZ": "https://thegraph.onchainfeed.org/subgraphs/name/azuro-protocol/azuro-api-chiliz-spicy-dev-v3",
}
AZURO_BET_HISTORY_QUERY = """
query UserBetHistory($bettor: String!, $first: Int!, $skip: Int!) {
  v3Bets(
    first: $first
    skip: $skip
    where: { bettor: $bettor }
    orderBy: createdBlockTimestamp
    orderDirection: desc
    subgraphError: allow
  ) {
    id
    betId
    type
    amount
    odds
    potentialPayout
    payout
    status
    result
    createdBlockTimestamp
    createdTxHash
    selections {
      odds
      result
      conditionKind
      outcome {
        outcomeId
        condition {
          conditionId
        }
      }
    }
    _gamesIds
  }
  liveBets(
    first: $first
    skip: $skip
    where: { actor_starts_with_nocase: $bettor }
    orderBy: createdAt
    orderDirection: desc
    subgraphError: allow
  ) {
    id
    betId
    status
    amount
    odds
    createdAt
    potentialPayout
    payout
    result
    selections {
      odds
      result
      outcome {
        id
        outcomeId
        condition {
          id
          conditionId
          status
          gameId
        }
      }
    }
    isRedeemed
    isRedeemable
    txHash
    core {
      address
      liquidityPool {
        address
        tokenSymbol
      }
    }
  }
}
"""
AZURO_REFERENCES = (
    "https://gem.azuro.org/hub/apps/APIs",
    "https://gem.azuro.org/hub/apps/APIs/backend",
    "https://gem.azuro.org/hub/apps/APIs/backend/betting",
    "https://gem.azuro.org/hub/apps/APIs/websocket",
    "https://gem.azuro.org/hub/apps/toolkit/feed/getGamesByFilters",
    "https://gem.azuro.org/hub/apps/toolkit/feed/getConditionsByGameIds",
    "https://gem.azuro.org/hub/apps/sdk/overview",
    "https://gem.azuro.org/hub/apps/APIs/graph",
    "https://gem.azuro.org/hub/apps/APIs/backend/betting",
    "https://gem.azuro.org/hub/apps/guides/advanced/live/get-bets-history",
)


class AzuroAdapter(MarketAdapter):
    """Azuro adapter using documented V3 backend/feed APIs."""

    live_order_sides = ("BUY",)
    metadata = get_market_metadata("azuro")
    account_recovery_operations = AZURO_ACCOUNT_OPERATIONS

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        bettor = self.resolve_credential("azuro_bettor_address", ("AZURO_BETTOR_ADDRESS",), label="AZURO_BETTOR_ADDRESS")
        affiliate = self.resolve_credential(
            "azuro_affiliate_address",
            ("AZURO_AFFILIATE_ADDRESS",),
            label="AZURO_AFFILIATE_ADDRESS",
        )
        credential_sources = []
        for credential in (bettor, affiliate):
            if credential:
                credential_sources.append({"name": credential.name, "source": credential.source})
        health.update(
            {
                "api_base_url": self.api_base_url,
                "websocket_url": self.websocket_url,
                "environment": self.environment,
                "references": list(AZURO_REFERENCES),
                "orderbook_supported": False,
                "graph_api_url": self.graph_api_url,
                "account_recovery_operations": list(self.account_recovery_operations),
                "authenticated_account_endpoints": ["POST Azuro client subgraph GraphQL v3Bets/liveBets"],
                "live_trading_enabled": self.config_bool("live_trading_enabled", False),
                "credential_sources": credential_sources,
            }
        )
        return health

    @property
    def api_base_url(self) -> str:
        configured = self.config.get("azuro_api_base_url") or self.config.get("api_base_url")
        return str(configured or DEFAULT_AZURO_BASE_URL).rstrip("/")

    @property
    def websocket_url(self) -> str:
        configured = self.config.get("azuro_ws_url") or self.config.get("websocket_url")
        return str(configured or DEFAULT_AZURO_WS_URL).rstrip("/")

    @property
    def environment(self) -> str:
        return str(self.config.get("azuro_environment") or self.config.get("environment") or DEFAULT_AZURO_ENVIRONMENT)

    @property
    def chain_id(self) -> int:
        return int(self.config.get("azuro_chain_id") or self.config.get("chain_id") or DEFAULT_AZURO_CHAIN_ID)

    @property
    def graph_api_url(self) -> str:
        configured = self.config.get("azuro_graph_api_url") or self.config.get("graph_api_url")
        if configured not in (None, ""):
            value = str(configured).strip().rstrip("/")
            if not re.fullmatch(r"https?://[^\s]+", value):
                raise MarketConfigurationError("Azuro graph_api_url must be an absolute HTTP(S) URL.")
            return value
        return AZURO_GRAPH_ENDPOINTS.get(self.environment, AZURO_GRAPH_ENDPOINTS["PolygonUSDT"])

    def account_recovery(self, operation: str, **kwargs: Any) -> Dict[str, Any]:
        """Read the documented Azuro V3 bettor and live-bet history feeds."""

        normalized = str(operation or "").strip().lower()
        if normalized not in self.account_recovery_operations:
            raise MarketConfigurationError(
                "Azuro account operation must be one of: " + ", ".join(self.account_recovery_operations) + "."
            )
        wallet = str(kwargs.get("wallet") or kwargs.get("bettor") or "").strip()
        if not wallet:
            credential = self.resolve_credential(
                "azuro_bettor_address",
                ("AZURO_BETTOR_ADDRESS",),
                required=True,
                label="AZURO_BETTOR_ADDRESS",
            )
            wallet = credential.value
        if not AZURO_BETTOR_ADDRESS_RE.fullmatch(wallet):
            raise MarketConfigurationError("Azuro bettor wallet must be a canonical 0x-prefixed 40-hex address.")
        limit = self._bounded_bet_history_int(kwargs.get("limit", 100), "limit", default=100)
        offset = self._bounded_bet_history_int(kwargs.get("offset", kwargs.get("skip", 0)), "offset", default=0)
        payload = self.runtime.request_json(
            "POST",
            self.graph_api_url,
            json_body={
                "query": AZURO_BET_HISTORY_QUERY,
                "variables": {"bettor": wallet, "first": limit, "skip": offset},
            },
            headers={"Content-Type": "application/json"},
        )
        if not isinstance(payload, Mapping):
            raise MarketHTTPError("azuro GraphQL response was not an object.")
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            message = "; ".join(str(item.get("message") or item) for item in errors[:3] if isinstance(item, Mapping))
            raise MarketHTTPError(f"azuro GraphQL query failed: {message or 'unknown error'}")
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise MarketHTTPError("azuro GraphQL response did not contain a data object.")
        return {
            "source": "azuro_client_subgraph",
            "graph_api_url": self.graph_api_url,
            "bettor": wallet,
            "limit": limit,
            "offset": offset,
            "v3_bets": data.get("v3Bets") if isinstance(data.get("v3Bets"), list) else [],
            "live_bets": data.get("liveBets") if isinstance(data.get("liveBets"), list) else [],
            "data": dict(data),
        }

    @staticmethod
    def _bounded_bet_history_int(value: Any, label: str, *, default: int) -> int:
        if value in (None, ""):
            return default
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"Azuro bet history {label} must be an integer.") from exc
        maximum = AZURO_BET_HISTORY_PAGE_MAX if label == "limit" else 1_000_000
        minimum = 1 if label == "limit" else 0
        if parsed < minimum or parsed > maximum:
            raise MarketConfigurationError(
                f"Azuro bet history {label} must be between {minimum} and {maximum}."
            )
        return parsed

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = max(1, min(int(limit or 50), 100))
        games = self._fetch_games(limit=desired, query=query)
        q = str(query or "").strip().lower()
        if q:
            games = [game for game in games if self._game_matches_query(game, q)]
        return [self._event_from_game(game) for game in games[:desired]]

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        game_id = str(event_id or "").strip()
        if not game_id:
            return []
        game = self._get_game(game_id)
        conditions = self._fetch_conditions([game_id])
        contracts: List[MarketContract] = []
        for condition in conditions:
            if self._condition_game_id(condition) != game_id:
                continue
            contracts.extend(self._contracts_from_condition(game, condition))
        return contracts

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        game_id, condition_id, outcome_id = self._split_contract_id(contract_id)
        conditions = self._fetch_conditions([game_id])
        condition, outcome = self._find_condition_outcome(conditions, condition_id, outcome_id)
        decimal_odds = self._decimal_odds(outcome)
        probability = 1.0 / decimal_odds if decimal_odds and decimal_odds > 0 else None
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=self._contract_id(game_id, condition_id, outcome_id),
            last=probability,
            midpoint=probability,
            source="azuro_current_odds",
            raw={
                "condition": dict(condition),
                "outcome": dict(outcome),
                "decimal_odds": decimal_odds,
                "environment": self.environment,
            },
        )

    def get_orderbook(self, contract_id: str):
        raise UnsupportedFeatureError(
            self.market_id,
            "orderbook_reading",
            "Azuro uses a liquidity-pool/vAMM odds model and does not expose a CLOB orderbook endpoint.",
        )

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        self._validate_order(order)
        game_id, condition_id, outcome_id = self._split_contract_id(order.contract_id)
        odds = order.limit_price if order.limit_price is not None else self.get_price(order.contract_id).raw.get("decimal_odds")
        calculation_payload = self._bet_calculation_payload(condition_id, outcome_id)
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=self._contract_id(game_id, condition_id, outcome_id),
            accepted=True,
            message=(
                f"DRY RUN: would prepare Azuro bet calculation for {order.size:.4f} stake"
                + (f" at minimum decimal odds {float(odds):.4f}" if odds is not None else "")
            ),
            filled_size=0.0,
            average_price=None,
            raw={
                "calculation_endpoint": self._url("/bet/calculation"),
                "calculation_request": calculation_payload,
                "paper_stake": float(order.size),
                "min_odds": self._min_odds_units(odds) if odds is not None else None,
            },
        )

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        self.ensure_capability("live_trading")
        self._validate_order(order)
        preflight = self.preflight_live_order(order)
        payload, endpoint = self._live_order_payload(order)
        response = self.runtime.request_json(
            "POST",
            self._url(endpoint),
            json_body=payload,
            headers={"Content-Type": "application/json"},
        )
        return {
            "market_id": self.market_id,
            "contract_id": self._contract_id(*self._split_contract_id(order.contract_id)),
            "live": True,
            "endpoint": endpoint,
            "preflight": preflight,
            "request": payload,
            "response": response,
        }

    def copy_trade_from_activity(self, activity: Mapping[str, Any]) -> PaperOrderResult:
        raise UnsupportedFeatureError(
            self.market_id,
            "copy_trading",
            "Azuro copy trading is unsupported because this adapter has no official account activity mirroring model.",
        )

    def websocket_connection_info(
        self,
        *,
        game_ids: Optional[List[str]] = None,
        condition_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        messages = self.websocket_subscriptions(
            environment=self.environment,
            game_ids=game_ids,
            condition_ids=condition_ids,
        )
        return {
            "url": self.websocket_url,
            "environment": self.environment,
            "subscriptions": messages,
            "events": ["GameUpdated", "ConditionUpdated"],
        }

    @staticmethod
    def websocket_subscriptions(
        *,
        environment: str,
        game_ids: Optional[List[str]] = None,
        condition_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        games = [str(game_id).strip() for game_id in (game_ids or []) if str(game_id).strip()]
        conditions = [str(condition_id).strip() for condition_id in (condition_ids or []) if str(condition_id).strip()]
        if not games and not conditions:
            raise MarketConfigurationError("Azuro WebSocket subscription requires game ids or condition ids.")
        messages: List[Dict[str, Any]] = []
        if games:
            messages.append({"event": "SubscribeGames", "data": {"gameIds": games, "environment": environment}})
        if conditions:
            messages.append(
                {"event": "SubscribeConditions", "data": {"conditionIds": conditions, "environment": environment}}
            )
        return messages

    def _fetch_games(self, *, limit: int, query: str = "") -> List[Mapping[str, Any]]:
        if str(query or "").strip():
            payload = {
                "environment": self.environment,
                "query": str(query or "").strip(),
                "page": 1,
                "perPage": max(1, min(int(limit or 50), 100)),
            }
            data = self.runtime.request_json("POST", self._url("/market-manager/search-games"), json_body=payload)
        else:
            payload = {
                "environment": self.environment,
                "state": str(self.config.get("azuro_game_state") or "Prematch"),
                "page": 1,
                "perPage": max(1, min(int(limit or 50), 100)),
                "orderBy": str(self.config.get("azuro_order_by") or "StartsAt"),
                "orderDir": str(self.config.get("azuro_order_dir") or "Asc"),
            }
            sport_slug = str(self.config.get("azuro_sport_slug") or "").strip()
            league_slug = str(self.config.get("azuro_league_slug") or "").strip()
            if sport_slug:
                payload["sportSlug"] = sport_slug
            if league_slug:
                payload["leagueSlug"] = league_slug
            data = self.runtime.request_json("POST", self._url("/market-manager/games-by-filters"), json_body=payload)
        return self._games_from_payload(data)

    def _get_game(self, game_id: str) -> Mapping[str, Any]:
        data = self.runtime.request_json(
            "POST",
            self._url("/market-manager/games-by-ids"),
            json_body={"environment": self.environment, "gameIds": [game_id]},
        )
        games = self._games_from_payload(data)
        for game in games:
            if self._game_id(game) == game_id:
                return game
        raise MarketConfigurationError(f"Azuro game {game_id!r} was not found.")

    def _fetch_conditions(self, game_ids: List[str]) -> List[Mapping[str, Any]]:
        data = self.runtime.request_json(
            "POST",
            self._url("/market-manager/conditions-by-game-ids"),
            json_body={"environment": self.environment, "gameIds": game_ids},
        )
        return self._conditions_from_payload(data)

    def _url(self, path: str) -> str:
        clean_path = "/" + str(path or "").strip("/")
        return f"{self.api_base_url}{clean_path}"

    def _event_from_game(self, game: Mapping[str, Any]) -> MarketEvent:
        game_id = self._game_id(game)
        return MarketEvent(
            market_id=self.market_id,
            event_id=game_id,
            title=str(game.get("title") or game.get("slug") or game_id),
            url=self._game_url(game),
            status=self._game_state(game),
            raw=dict(game),
        )

    def _contracts_from_condition(
        self,
        game: Mapping[str, Any],
        condition: Mapping[str, Any],
    ) -> List[MarketContract]:
        game_id = self._game_id(game) or self._condition_game_id(condition)
        condition_id = self._condition_id(condition)
        if not game_id or not condition_id:
            return []
        market_name = str(condition.get("marketName") or condition.get("title") or condition.get("name") or "Market")
        status = self._condition_state(condition)
        contracts: List[MarketContract] = []
        for outcome in self._outcomes(condition):
            outcome_id = self._outcome_id(outcome)
            if not outcome_id:
                continue
            selection_name = str(outcome.get("selectionName") or outcome.get("title") or outcome.get("name") or outcome_id)
            contracts.append(
                MarketContract(
                    market_id=self.market_id,
                    contract_id=self._contract_id(game_id, condition_id, outcome_id),
                    event_id=game_id,
                    title=f"{str(game.get('title') or game_id)} - {market_name} - {selection_name}",
                    outcome=selection_name,
                    url=self._game_url(game),
                    status=status,
                    raw={"game": dict(game), "condition": dict(condition), "outcome": dict(outcome)},
                )
            )
        return contracts

    def _live_order_payload(self, order: PaperOrderRequest) -> Tuple[Dict[str, Any], str]:
        order_type = str(order.metadata.get("order_type") or "ordinar").lower()
        if order_type not in {"ordinar", "combo"}:
            raise MarketConfigurationError("Azuro order_type must be ordinar or combo.")
        bettor = str(
            order.metadata.get("bettor")
            or self.config.get("azuro_bettor_address")
            or self._required_env_address("AZURO_BETTOR_ADDRESS")
        )
        bet_owner = str(order.metadata.get("bet_owner") or bettor)
        client_bet_data = order.metadata.get("client_bet_data")
        signature = order.metadata.get("bettor_signature")
        if not isinstance(client_bet_data, Mapping) or not signature:
            raise MarketConfigurationError(
                "Azuro live trading requires pre-signed client_bet_data and bettor_signature from a user wallet."
            )
        payload = {
            "environment": str(order.metadata.get("environment") or self.environment),
            "bettor": bettor,
            "betOwner": bet_owner,
            "clientBetData": dict(client_bet_data),
            "bettorSignature": str(signature),
        }
        return payload, f"/bet/orders/{order_type}"

    def _required_env_address(self, env_var: str) -> str:
        credential = self.resolve_credential(
            env_var.lower(),
            (env_var,),
            required=True,
            label=env_var,
        )
        return credential.value

    def _bet_calculation_payload(self, condition_id: str, outcome_id: str) -> Dict[str, Any]:
        return {
            "environment": self.environment,
            "bets": [{"conditionId": condition_id, "outcomeId": self._int_or_string(outcome_id)}],
        }

    def _validate_order(self, order: PaperOrderRequest) -> None:
        self.ensure_order_market(order)
        self._split_contract_id(order.contract_id)
        side = str(order.side or "").upper()
        if side != "BUY":
            raise MarketConfigurationError("Azuro orders must use side BUY because bets select an outcome.")
        if not self._is_positive_number(order.size):
            raise MarketConfigurationError("Azuro order size must be positive.")
        if order.limit_price is not None and not self._is_positive_number(order.limit_price):
            raise MarketConfigurationError("Azuro limit price must be positive decimal odds.")

    @staticmethod
    def _games_from_payload(payload: Any) -> List[Mapping[str, Any]]:
        if not isinstance(payload, Mapping):
            return []
        candidates = payload.get("games")
        if candidates is None and isinstance(payload.get("data"), Mapping):
            candidates = payload["data"].get("games")
        if isinstance(candidates, list):
            return [game for game in candidates if isinstance(game, Mapping)]
        return []

    @staticmethod
    def _conditions_from_payload(payload: Any) -> List[Mapping[str, Any]]:
        if not isinstance(payload, Mapping):
            return []
        candidates = payload.get("conditions")
        if candidates is None and isinstance(payload.get("data"), Mapping):
            candidates = payload["data"].get("conditions")
        conditions: List[Mapping[str, Any]] = []
        if isinstance(candidates, list):
            conditions.extend(condition for condition in candidates if isinstance(condition, Mapping))
        games = payload.get("games")
        if games is None and isinstance(payload.get("data"), Mapping):
            games = payload["data"].get("games")
        if isinstance(games, list):
            for game in games:
                if not isinstance(game, Mapping):
                    continue
                for condition in game.get("conditions") or []:
                    if isinstance(condition, Mapping):
                        condition_data = dict(condition)
                        condition_data.setdefault("gameId", AzuroAdapter._game_id(game))
                        conditions.append(condition_data)
        return conditions

    @staticmethod
    def _find_condition_outcome(
        conditions: List[Mapping[str, Any]],
        condition_id: str,
        outcome_id: str,
    ) -> Tuple[Mapping[str, Any], Mapping[str, Any]]:
        for condition in conditions:
            if AzuroAdapter._condition_id(condition) != condition_id:
                continue
            for outcome in AzuroAdapter._outcomes(condition):
                if AzuroAdapter._outcome_id(outcome) == outcome_id:
                    return condition, outcome
        raise MarketConfigurationError(f"Azuro outcome {condition_id}:{outcome_id} was not found.")

    @staticmethod
    def _game_matches_query(game: Mapping[str, Any], query: str) -> bool:
        participants = game.get("participants") or []
        participant_names = " ".join(str(item.get("name") or "") for item in participants if isinstance(item, Mapping))
        values = [
            game.get("id"),
            game.get("gameId"),
            game.get("slug"),
            game.get("title"),
            game.get("sport", {}).get("name") if isinstance(game.get("sport"), Mapping) else "",
            game.get("league", {}).get("name") if isinstance(game.get("league"), Mapping) else "",
            participant_names,
        ]
        return query in " ".join(str(value or "") for value in values).lower()

    @staticmethod
    def _game_url(game: Mapping[str, Any]) -> str:
        slug = str(game.get("slug") or "").strip()
        return f"https://azuro.org/{slug}" if slug else "https://azuro.org"

    @staticmethod
    def _game_id(game: Mapping[str, Any]) -> str:
        return str(game.get("id") or game.get("gameId") or "").strip()

    @staticmethod
    def _game_state(game: Mapping[str, Any]) -> str:
        return str(game.get("state") or "").strip().lower()

    @staticmethod
    def _condition_id(condition: Mapping[str, Any]) -> str:
        return str(condition.get("conditionId") or condition.get("id") or "").strip()

    @staticmethod
    def _condition_game_id(condition: Mapping[str, Any]) -> str:
        return str(condition.get("gameId") or condition.get("game", {}).get("id") if isinstance(condition.get("game"), Mapping) else condition.get("gameId") or "").strip()

    @staticmethod
    def _condition_state(condition: Mapping[str, Any]) -> str:
        return str(condition.get("state") or "").strip().lower()

    @staticmethod
    def _outcomes(condition: Mapping[str, Any]) -> List[Mapping[str, Any]]:
        outcomes = condition.get("outcomes")
        if isinstance(outcomes, list):
            return [outcome for outcome in outcomes if isinstance(outcome, Mapping)]
        return []

    @staticmethod
    def _outcome_id(outcome: Mapping[str, Any]) -> str:
        return str(outcome.get("outcomeId") or outcome.get("id") or "").strip()

    @staticmethod
    def _decimal_odds(outcome: Mapping[str, Any]) -> Optional[float]:
        for key in ("currentOdds", "odds", "price"):
            value = outcome.get(key)
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number) and number > 0:
                return number
        return None

    @staticmethod
    def _split_contract_id(contract_id: str) -> Tuple[str, str, str]:
        raw = str(contract_id or "").strip()
        parts = [part.strip() for part in raw.split(":")]
        if len(parts) != 3 or not all(parts):
            raise MarketConfigurationError("Azuro contract id must be GAME_ID:CONDITION_ID:OUTCOME_ID.")
        return parts[0], parts[1], parts[2]

    @staticmethod
    def _contract_id(game_id: str, condition_id: str, outcome_id: str) -> str:
        return f"{game_id}:{condition_id}:{outcome_id}"

    @staticmethod
    def _min_odds_units(value: Any) -> str:
        try:
            odds = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Azuro decimal odds must be numeric.") from exc
        if not math.isfinite(odds) or odds <= 0:
            raise MarketConfigurationError("Azuro decimal odds must be positive.")
        return str(int(round(odds * AZURO_ODDS_SCALE)))

    @staticmethod
    def _int_or_string(value: str) -> Any:
        try:
            return int(value)
        except (TypeError, ValueError):
            return value

    @staticmethod
    def _is_positive_number(value: Any) -> bool:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False
        return math.isfinite(number) and number > 0
