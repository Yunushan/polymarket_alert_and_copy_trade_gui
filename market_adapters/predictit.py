from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .base import MarketAdapter
from .catalog import get_market_metadata
from .errors import MarketConfigurationError, MarketHTTPError, UnsupportedFeatureError
from .types import (
    MarketContract,
    MarketEvent,
    OrderBookLevel,
    OrderBookSnapshot,
    PaperOrderRequest,
    PaperOrderResult,
    PriceSnapshot,
)


DEFAULT_PREDICTIT_BASE_URL = "https://www.predictit.org/api/marketdata"
PREDICTIT_REFERENCES = (
    "https://predictit.freshdesk.com/support/solutions/articles/12000001878-does-predictit-make-market-data-available-via-an-api-",
    "https://www.predictit.org/api/marketdata/all",
    "https://www.predictit.org",
)


class PredictItAdapter(MarketAdapter):
    """PredictIt adapter using the documented public read-only market data API."""

    metadata = get_market_metadata("predictit")

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        health.update(
            {
                "api_base_url": self.api_base_url,
                "references": list(PREDICTIT_REFERENCES),
                "orderbook_supported": True,
                "orderbook_depth_supported": False,
                "live_trading_supported": False,
                "live_trading_enabled": self.config_bool("live_trading_enabled", False),
            }
        )
        return health

    @property
    def api_base_url(self) -> str:
        configured = self.config.get("predictit_api_base_url") or self.config.get("api_base_url")
        return str(configured or DEFAULT_PREDICTIT_BASE_URL).rstrip("/")

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = max(1, min(int(limit or 50), 1000))
        markets = self._fetch_all_markets()
        q = str(query or "").strip().lower()
        if q:
            markets = [market for market in markets if self._market_matches_query(market, q)]
        return [self._event_from_market(market) for market in markets[:desired]]

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        market = self._get_market(str(event_id or "").strip())
        if not market:
            return []
        return self._contracts_from_market(market)

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        market_id, predictit_contract_id, outcome = self._split_contract_id(contract_id)
        market = self._get_market(market_id)
        contract = self._find_contract(market, predictit_contract_id)
        if not contract:
            raise MarketConfigurationError(
                f"PredictIt contract {predictit_contract_id!r} was not found in market {market_id!r}."
            )
        bid, ask, last = self._prices_from_contract(contract, outcome)
        midpoint = (bid + ask) / 2.0 if bid is not None and ask is not None else None
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=self._contract_id(market_id, predictit_contract_id, outcome),
            last=last,
            bid=bid,
            ask=ask,
            midpoint=midpoint,
            source="predictit_marketdata",
            raw={"market": dict(market), "contract": dict(contract), "outcome": outcome},
        )

    def get_orderbook(self, contract_id: str) -> OrderBookSnapshot:
        """Return the documented one-level quote snapshot.

        PredictIt's public feed publishes best buy/sell costs but no depth or
        quantities.  The canonical orderbook therefore exposes each available
        quote with an explicit unknown size (``0.0``) and preserves the raw
        contract payload.  Full depth remains intentionally unsupported.
        """
        self.ensure_capability("orderbook_reading")
        market_id, predictit_contract_id, outcome = self._split_contract_id(contract_id)
        market = self._get_market(market_id)
        contract = self._find_contract(market, predictit_contract_id)
        if not contract:
            raise MarketConfigurationError(
                f"PredictIt contract {predictit_contract_id!r} was not found in market {market_id!r}."
            )
        bid, ask, _ = self._prices_from_contract(contract, outcome)
        bids = [OrderBookLevel(price=bid, size=0.0)] if bid is not None else []
        asks = [OrderBookLevel(price=ask, size=0.0)] if ask is not None else []
        return OrderBookSnapshot(
            market_id=self.market_id,
            contract_id=self._contract_id(market_id, predictit_contract_id, outcome),
            bids=bids,
            asks=asks,
            raw={
                "market": dict(market),
                "contract": dict(contract),
                "outcome": outcome,
                "depth": "top_of_book_only",
                "size_available": False,
            },
        )

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        self._validate_order(order)
        market_id, contract_id, outcome = self._split_contract_id(order.contract_id)
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=self._contract_id(market_id, contract_id, outcome),
            accepted=True,
            message=(
                f"DRY RUN: would place PredictIt {order.side.upper()} "
                f"for {order.size:.4f} {outcome} shares"
                + (f" at limit {order.limit_price:.2f}" if order.limit_price is not None else "")
            ),
            filled_size=0.0,
            average_price=None,
            raw={"market_id": market_id, "predictit_contract_id": contract_id, "outcome": outcome},
        )

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        raise UnsupportedFeatureError(
            self.market_id,
            "live_trading",
            "PredictIt does not publish an official automated trading API for this adapter; live trading is unsupported.",
        )

    def copy_trade_from_activity(self, activity: Mapping[str, Any]) -> PaperOrderResult:
        raise UnsupportedFeatureError(
            self.market_id,
            "copy_trading",
            "PredictIt does not expose an official account activity mirroring API for copy trading.",
        )

    def _fetch_all_markets(self) -> List[Mapping[str, Any]]:
        return self._markets_from_payload(self._get("/all"))

    def _get_market(self, market_id: str) -> Mapping[str, Any]:
        clean_market_id = str(market_id or "").strip()
        if not clean_market_id:
            raise MarketConfigurationError("PredictIt market id cannot be empty.")
        try:
            data = self._get(f"/markets/{clean_market_id}")
        except MarketHTTPError:
            for market in self._fetch_all_markets():
                if self._market_id(market) == clean_market_id:
                    return market
            raise
        market = self._market_from_payload(data, clean_market_id)
        if not market:
            raise MarketConfigurationError(f"PredictIt market {clean_market_id!r} was not found.")
        return market

    def _get(self, path: str) -> Any:
        return self.runtime.get_json(self._url(path))

    def _url(self, path: str) -> str:
        clean_path = "/" + str(path or "").strip("/")
        return f"{self.api_base_url}{clean_path}"

    def _event_from_market(self, market: Mapping[str, Any]) -> MarketEvent:
        market_id = self._market_id(market)
        return MarketEvent(
            market_id=self.market_id,
            event_id=market_id,
            title=str(market.get("name") or market.get("shortName") or market_id),
            url=self._market_url(market),
            status=self._status_from_mapping(market),
            raw=dict(market),
        )

    def _contracts_from_market(self, market: Mapping[str, Any]) -> List[MarketContract]:
        market_id = self._market_id(market)
        if not market_id:
            return []
        market_title = str(market.get("name") or market.get("shortName") or market_id)
        status = self._status_from_mapping(market)
        contracts: List[MarketContract] = []
        for contract in self._contract_list(market):
            predictit_contract_id = self._contract_payload_id(contract)
            if not predictit_contract_id:
                continue
            contract_title = str(contract.get("name") or contract.get("shortName") or predictit_contract_id)
            for outcome in ("YES", "NO"):
                contracts.append(
                    MarketContract(
                        market_id=self.market_id,
                        contract_id=self._contract_id(market_id, predictit_contract_id, outcome),
                        event_id=market_id,
                        title=f"{market_title} - {contract_title} - {outcome.title()}",
                        outcome=outcome.title(),
                        url=self._market_url(market),
                        status=self._status_from_mapping(contract) or status,
                        raw={"market": dict(market), "contract": dict(contract), "outcome": outcome},
                    )
                )
        return contracts

    def _validate_order(self, order: PaperOrderRequest) -> None:
        self.ensure_order_market(order)
        self._split_contract_id(order.contract_id)
        side = str(order.side or "").upper()
        if side not in {"BUY", "SELL"}:
            raise MarketConfigurationError("PredictIt paper order side must be BUY or SELL.")
        if not self._is_positive_number(order.size):
            raise MarketConfigurationError("PredictIt paper order size must be positive.")
        if order.limit_price is not None and self._safe_probability(order.limit_price) is None:
            raise MarketConfigurationError("PredictIt paper order limit price must be between 0 and 1.")

    @staticmethod
    def _markets_from_payload(data: Any) -> List[Mapping[str, Any]]:
        if isinstance(data, Mapping):
            markets = data.get("markets")
            if isinstance(markets, list):
                return [market for market in markets if isinstance(market, Mapping)]
            if "contracts" in data:
                return [data]
        return []

    @staticmethod
    def _market_from_payload(data: Any, market_id: str) -> Optional[Mapping[str, Any]]:
        if not isinstance(data, Mapping):
            return None
        market = data.get("market")
        if isinstance(market, Mapping):
            return market
        markets = data.get("markets")
        if isinstance(markets, list):
            for item in markets:
                if isinstance(item, Mapping) and PredictItAdapter._market_id(item) == market_id:
                    return item
            if len(markets) == 1 and isinstance(markets[0], Mapping):
                return markets[0]
        if "contracts" in data:
            return data
        return None

    @staticmethod
    def _find_contract(market: Mapping[str, Any], contract_id: str) -> Optional[Mapping[str, Any]]:
        for contract in PredictItAdapter._contract_list(market):
            if PredictItAdapter._contract_payload_id(contract) == contract_id:
                return contract
        return None

    @staticmethod
    def _contract_list(market: Mapping[str, Any]) -> List[Mapping[str, Any]]:
        contracts = market.get("contracts")
        if isinstance(contracts, list):
            return [contract for contract in contracts if isinstance(contract, Mapping)]
        return []

    @staticmethod
    def _prices_from_contract(contract: Mapping[str, Any], outcome: str) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        outcome = outcome.upper()
        last_yes = PredictItAdapter._safe_probability(contract.get("lastTradePrice"))
        if last_yes is None:
            last_yes = PredictItAdapter._safe_probability(contract.get("lastClosePrice"))
        if outcome == "YES":
            return (
                PredictItAdapter._safe_probability(contract.get("bestSellYesCost")),
                PredictItAdapter._safe_probability(contract.get("bestBuyYesCost")),
                last_yes,
            )
        last_no = 1.0 - last_yes if last_yes is not None else None
        return (
            PredictItAdapter._safe_probability(contract.get("bestSellNoCost")),
            PredictItAdapter._safe_probability(contract.get("bestBuyNoCost")),
            last_no,
        )

    @staticmethod
    def _market_matches_query(market: Mapping[str, Any], query: str) -> bool:
        values = [
            market.get("id"),
            market.get("name"),
            market.get("shortName"),
            market.get("ticker"),
        ]
        for contract in PredictItAdapter._contract_list(market):
            values.extend([contract.get("id"), contract.get("name"), contract.get("shortName"), contract.get("ticker")])
        return query in " ".join(str(value or "") for value in values).lower()

    @staticmethod
    def _status_from_mapping(data: Mapping[str, Any]) -> str:
        status = str(data.get("status") or "").strip().lower()
        if status == "open":
            return "active"
        return status

    @staticmethod
    def _market_url(market: Mapping[str, Any]) -> str:
        raw = str(market.get("url") or "").strip()
        if raw:
            return raw
        market_id = PredictItAdapter._market_id(market)
        return f"https://www.predictit.org/markets/detail/{market_id}" if market_id else "https://www.predictit.org"

    @staticmethod
    def _split_contract_id(contract_id: str) -> Tuple[str, str, str]:
        raw = str(contract_id or "").strip()
        if not raw:
            raise MarketConfigurationError("PredictIt order requires a contract id.")
        parts = [part.strip() for part in raw.split(":")]
        if len(parts) == 2:
            market_id, predictit_contract_id = parts
            outcome = "YES"
        elif len(parts) == 3:
            market_id, predictit_contract_id, outcome = parts
            outcome = outcome.upper()
        else:
            raise MarketConfigurationError("PredictIt contract id must be MARKET_ID:CONTRACT_ID[:YES|NO].")
        if not market_id or not predictit_contract_id:
            raise MarketConfigurationError("PredictIt contract id must include market and contract ids.")
        if outcome not in {"YES", "NO"}:
            raise MarketConfigurationError("PredictIt contract outcome must be YES or NO.")
        return market_id, predictit_contract_id, outcome

    @staticmethod
    def _contract_id(market_id: str, contract_id: str, outcome: str) -> str:
        return f"{market_id}:{contract_id}:{outcome.upper()}"

    @staticmethod
    def _market_id(market: Mapping[str, Any]) -> str:
        return str(market.get("id") or market.get("marketId") or "").strip()

    @staticmethod
    def _contract_payload_id(contract: Mapping[str, Any]) -> str:
        return str(contract.get("id") or contract.get("contractId") or "").strip()

    @staticmethod
    def _safe_probability(value: Any) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        if number > 1.0:
            if number <= 100.0:
                number = number / 100.0
            else:
                return None
        if number < 0.0 or number > 1.0:
            return None
        return number

    @staticmethod
    def _is_positive_number(value: Any) -> bool:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False
        return math.isfinite(number) and number > 0
