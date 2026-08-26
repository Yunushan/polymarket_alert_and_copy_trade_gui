"""Official BetMGM Sports API adapter.

BetMGM's partner Sports API publishes a stable read-only fixture, market,
option, and price schema.  It is an odds feed rather than an exchange CLOB:
the adapter therefore exposes discovery, event contracts, implied prices,
alerts, and local paper orders only.  The API publishes no order endpoint, so
live betting and account/copy operations remain fail-closed.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .base import MarketAdapter
from .catalog import get_market_metadata
from .errors import MarketConfigurationError, MarketHTTPError, UnsupportedFeatureError
from .runtime import AdapterRuntime
from .types import MarketContract, MarketEvent, PaperOrderRequest, PaperOrderResult, PriceSnapshot


DEFAULT_BETMGM_API_BASE_URL = "https://sportsapi.wv.betmgm.com"
BETMGM_REFERENCES = (
    "https://sportsapi.wv.betmgm.com/offer/swagger/sportsapi/swagger.json",
    "https://sportsapi.pa.betmgm.com/",
    "https://sportsapi.nj.betmgm.com/articles/getinvolved.html",
    "https://sportsapi.nj.betmgm.com/restapi/termsofuse.html",
)


class BetMGMAdapter(MarketAdapter):
    """Partner Sports API adapter with read-only and paper-order support."""

    metadata = get_market_metadata("betmgm")

    def _create_runtime(self) -> AdapterRuntime:
        interval = self.config.get("min_request_interval_seconds", 1.0)
        return AdapterRuntime(
            self.market_id,
            self.config,
            min_request_interval_seconds=float(interval or 0.0),
        )

    @property
    def api_base_url(self) -> str:
        configured = self.config.get("betmgm_api_base_url") or self.config.get("api_base_url")
        return str(configured or DEFAULT_BETMGM_API_BASE_URL).rstrip("/")

    @property
    def sport_id(self) -> int:
        value = self.config.get("betmgm_sport_id", 4)
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("BetMGM sport id must be an integer.") from exc
        if result <= 0:
            raise MarketConfigurationError("BetMGM sport id must be positive.")
        return result

    @property
    def country(self) -> str:
        value = str(self.config.get("betmgm_country") or "at").strip().lower()
        if len(value) != 2 or not value.isalpha():
            raise MarketConfigurationError("BetMGM country must be a two-letter ISO code.")
        return value

    @property
    def language(self) -> str:
        return str(self.config.get("betmgm_language") or "en").strip() or "en"

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        access_id = self.resolve_credential(
            "betmgm_access_id",
            ("BETMGM_ACCESS_ID", "BWIN_ACCESS_ID"),
            label="BetMGM AccessId",
        )
        access_token = self.resolve_credential(
            "betmgm_access_id_token",
            ("BETMGM_ACCESS_ID_TOKEN", "BWIN_ACCESS_ID_TOKEN"),
            label="BetMGM AccessIdToken",
        )
        health.update(
            {
                "api_base_url": self.api_base_url,
                "sport_id": self.sport_id,
                "country": self.country,
                "language": self.language,
                "references": list(BETMGM_REFERENCES),
                "credential_sources": [
                    {"name": credential.name, "source": credential.source}
                    for credential in (access_id, access_token)
                    if credential is not None
                ],
                "credentials_configured": bool(access_id and access_token),
                "partner_access_required": True,
                "live_trading_supported": False,
                "orderbook_supported": False,
                "copy_trading_supported": False,
            }
        )
        return health

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = max(1, min(int(limit or 50), 100))
        payload = self._fixtures()
        rows = self._rows(payload)
        needle = str(query or "").strip().lower()
        if needle:
            rows = [row for row in rows if needle in self._search_text(row)]
        return [self._event_from_fixture(row) for row in rows[:desired]]

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        clean_event_id = self._required_id(event_id, "event")
        rows = self._rows(self._fixtures([clean_event_id], only_main_markets=False))
        fixture = next(
            (
                row
                for row in rows
                if self._ids_match(self._object_id(row), clean_event_id, row.get("id"))
            ),
            None,
        )
        if fixture is None:
            raise MarketConfigurationError(f"BetMGM fixture {clean_event_id!r} was not found.")
        return self._contracts_from_fixture(fixture)

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        fixture_id, market_id, option_id = self._split_contract_id(contract_id)
        fixture, market, option = self._find_option(fixture_id, market_id, option_id)
        probability = self._price_probability(option.get("price"))
        if probability is None:
            raise MarketHTTPError(f"BetMGM option {option_id!r} has no usable decimal or fractional odds.")
        canonical_id = self._contract_id(self._object_id(fixture), market_id, option_id)
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=canonical_id,
            last=probability,
            source="betmgm_sports_api_implied_probability",
            raw={"fixture": dict(fixture), "market": dict(market), "option": dict(option)},
        )

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        self._validate_order(order)
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=str(order.contract_id).strip(),
            accepted=True,
            message=(
                f"DRY RUN: would place BetMGM {str(order.side).upper()} "
                f"for {float(order.size):.4f} stake"
                + (f" at implied probability {float(order.limit_price):.4f}" if order.limit_price is not None else "")
            ),
            raw={"official_api_is_read_only": True, "partner_access_required": True},
        )

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        raise UnsupportedFeatureError(
            self.market_id,
            "live_trading",
            "BetMGM's documented Sports API publishes offer data only; it has no supported order endpoint.",
        )

    def copy_trade_from_activity(self, activity: Mapping[str, Any]) -> PaperOrderResult:
        raise UnsupportedFeatureError(
            self.market_id,
            "copy_trading",
            "BetMGM's documented Sports API publishes no account activity feed for copy trading.",
        )

    def _fixtures(
        self,
        fixture_ids: Optional[List[str]] = None,
        *,
        only_main_markets: Optional[bool] = None,
    ) -> Any:
        params: Dict[str, Any] = {
            "language": self.language,
            "marketsFilterCriteria": str(self.config.get("betmgm_markets_filter") or "Visible"),
        }
        if only_main_markets is None:
            only_main_markets = self.config_bool("betmgm_only_main_markets", True)
        params["onlyMainMarkets"] = bool(only_main_markets)
        if fixture_ids:
            params["fixtureIds"] = fixture_ids
        return self._get(f"/offer/api/{self.sport_id}/{self.country}/fixtures", params=params)

    def _get(self, path: str, *, params: Optional[Mapping[str, Any]] = None) -> Any:
        return self.runtime.get_json(
            f"{self.api_base_url}/{str(path or '').strip('/')}",
            params=params,
            headers=self._headers(required=True),
        )

    def _headers(self, *, required: bool) -> Dict[str, str]:
        access_id = self.resolve_credential(
            "betmgm_access_id",
            ("BETMGM_ACCESS_ID", "BWIN_ACCESS_ID"),
            required=required,
            label="BetMGM AccessId",
        )
        access_token = self.resolve_credential(
            "betmgm_access_id_token",
            ("BETMGM_ACCESS_ID_TOKEN", "BWIN_ACCESS_ID_TOKEN"),
            required=required,
            label="BetMGM AccessIdToken",
        )
        if access_id is None or access_token is None:
            raise MarketConfigurationError("BetMGM reads require partner AccessId and AccessIdToken credentials.")
        return {"Bwin-AccessId": access_id.value, "Bwin-AccessIdToken": access_token.value}

    def _find_option(
        self, fixture_id: str, market_id: str, option_id: str
    ) -> Tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
        rows = self._rows(self._fixtures([fixture_id], only_main_markets=False))
        for fixture in rows:
            if not self._ids_match(self._object_id(fixture), fixture_id, fixture.get("id")):
                continue
            for market in self._mapping_rows(fixture.get("markets")):
                current_market_id = self._object_id(market)
                if current_market_id != market_id:
                    continue
                for option in self._mapping_rows(market.get("options")):
                    if self._object_id(option) == option_id:
                        return fixture, market, option
        raise MarketConfigurationError(
            f"BetMGM contract {fixture_id}|{market_id}|{option_id} was not found in the official fixture response."
        )

    def _contracts_from_fixture(self, fixture: Mapping[str, Any]) -> List[MarketContract]:
        fixture_id = self._object_id(fixture)
        event_id = fixture_id
        fixture_url = str(fixture.get("url") or "").strip()
        contracts: List[MarketContract] = []
        for market in self._mapping_rows(fixture.get("markets")):
            market_id = self._object_id(market)
            if not market_id:
                continue
            market_title = self._name(market.get("name")) or market_id
            status = str(market.get("spStatus") or ("open" if market.get("isOpenForBetting") else "closed")).lower()
            for option in self._mapping_rows(market.get("options")):
                option_id = self._object_id(option)
                if not option_id:
                    continue
                outcome = self._name(option.get("name")) or option_id
                contracts.append(
                    MarketContract(
                        market_id=self.market_id,
                        contract_id=self._contract_id(fixture_id, market_id, option_id),
                        event_id=event_id,
                        title=f"{market_title} - {outcome}",
                        outcome=outcome,
                        url=fixture_url,
                        status=status,
                        raw={"fixture": dict(fixture), "market": dict(market), "option": dict(option)},
                    )
                )
        return contracts

    def _event_from_fixture(self, fixture: Mapping[str, Any]) -> MarketEvent:
        event_id = self._object_id(fixture)
        return MarketEvent(
            market_id=self.market_id,
            event_id=event_id,
            title=self._name(fixture.get("name")) or event_id,
            url=str(fixture.get("url") or "").strip(),
            status=str(fixture.get("state") or "").strip().lower(),
            raw=dict(fixture),
        )

    def _validate_order(self, order: PaperOrderRequest) -> None:
        self.ensure_order_market(order)
        self._split_contract_id(order.contract_id)
        if str(order.side or "").upper() not in {"BUY", "BACK"}:
            raise MarketConfigurationError("BetMGM paper order side must be BUY or BACK.")
        try:
            size = float(order.size)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("BetMGM paper order stake must be numeric.") from exc
        if not math.isfinite(size) or size <= 0:
            raise MarketConfigurationError("BetMGM paper order stake must be positive and finite.")
        if order.limit_price is not None:
            try:
                limit = float(order.limit_price)
            except (TypeError, ValueError) as exc:
                raise MarketConfigurationError("BetMGM paper order limit probability must be numeric.") from exc
            if not math.isfinite(limit) or not 0.0 < limit <= 1.0:
                raise MarketConfigurationError("BetMGM paper order limit probability must be in (0, 1].")

    @staticmethod
    def _rows(payload: Any) -> List[Mapping[str, Any]]:
        if isinstance(payload, Mapping):
            rows = payload.get("items")
            return [row for row in rows or [] if isinstance(row, Mapping)] if isinstance(rows, list) else []
        return [row for row in payload if isinstance(row, Mapping)] if isinstance(payload, list) else []

    @staticmethod
    def _mapping_rows(value: Any) -> List[Mapping[str, Any]]:
        return [row for row in value or [] if isinstance(row, Mapping)] if isinstance(value, list) else []

    @classmethod
    def _object_id(cls, payload: Mapping[str, Any]) -> str:
        value = payload.get("id")
        if isinstance(value, Mapping):
            return str(value.get("full") or value.get("entityId") or "").strip()
        return str(value or "").strip()

    @classmethod
    def _ids_match(cls, canonical: str, requested: str, raw_id: Any) -> bool:
        if canonical == requested:
            return True
        if isinstance(raw_id, Mapping):
            return requested in {str(raw_id.get("full") or ""), str(raw_id.get("entityId") or "")}
        return str(raw_id or "") == requested

    @staticmethod
    def _name(value: Any) -> str:
        if isinstance(value, Mapping):
            return str(value.get("text") or value.get("shortText") or "").strip()
        return str(value or "").strip()

    @classmethod
    def _search_text(cls, payload: Mapping[str, Any]) -> str:
        return " ".join((cls._object_id(payload), cls._name(payload.get("name")), str(payload.get("state") or ""))).lower()

    @staticmethod
    def _price_probability(price: Any) -> Optional[float]:
        if not isinstance(price, Mapping):
            return None
        odds = price.get("odds")
        try:
            decimal = float(odds)
        except (TypeError, ValueError):
            decimal = float("nan")
        if math.isfinite(decimal) and decimal > 1.0:
            return 1.0 / decimal
        fraction = price.get("fraction")
        if isinstance(fraction, Mapping):
            try:
                numerator = float(fraction.get("numerator"))
                denominator = float(fraction.get("denominator"))
            except (TypeError, ValueError):
                return None
            if math.isfinite(numerator) and math.isfinite(denominator) and numerator >= 0 and denominator > 0:
                return 1.0 / (1.0 + numerator / denominator)
        us_odds = price.get("usOdds")
        try:
            american = float(us_odds)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(american) or american == 0:
            return None
        return 100.0 / (american + 100.0) if american > 0 else -american / (-american + 100.0)

    @staticmethod
    def _required_id(value: Any, label: str) -> str:
        clean = str(value or "").strip()
        if not clean or "|" in clean:
            raise MarketConfigurationError(f"BetMGM {label} id cannot be empty or contain '|'.")
        return clean

    @staticmethod
    def _contract_id(fixture_id: str, market_id: str, option_id: str) -> str:
        return f"{fixture_id}|{market_id}|{option_id}"

    @staticmethod
    def _split_contract_id(contract_id: str) -> Tuple[str, str, str]:
        parts = [part.strip() for part in str(contract_id or "").split("|")]
        if len(parts) != 3 or any(not part for part in parts):
            raise MarketConfigurationError("BetMGM contract id must be FIXTURE_ID|MARKET_ID|OPTION_ID.")
        return parts[0], parts[1], parts[2]
