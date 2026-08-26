from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import quote

from .base import MarketAdapter
from .catalog import get_market_metadata
from .errors import MarketConfigurationError, MarketHTTPError, UnsupportedFeatureError
from .runtime import AdapterRuntime
from .types import (
    MarketContract,
    MarketEvent,
    OrderBookLevel,
    OrderBookSnapshot,
    PaperOrderRequest,
    PaperOrderResult,
    PriceSnapshot,
)


DEFAULT_CRYPTO_COM_PREDICT_BASE_URL = "https://data-api.crypto.com/api/v1/predictions"
CRYPTO_COM_PREDICT_REFERENCES = (
    "https://data.crypto.com/docs",
    "https://data.crypto.com/quickstart",
)
FANATICS_MARKETS_REFERENCES = (
    "https://fanaticsmarkets.com/legal/availability",
    "https://crypto.com/en/prediction/partners",
    "https://data.crypto.com/docs",
    "https://data.crypto.com/quickstart",
)


class CryptoComPredictAdapter(MarketAdapter):
    """Crypto.com Predictions adapter for the official read-only market-data API."""

    metadata = get_market_metadata("crypto_com_predict")
    provider_label = "Crypto.com Predictions"
    market_homepage_url = "https://crypto.com/prediction/"
    price_source = "crypto_com_predictions_market_data"
    references = CRYPTO_COM_PREDICT_REFERENCES

    def _create_runtime(self) -> AdapterRuntime:
        interval = self.config.get("min_request_interval_seconds", 0.6)
        return AdapterRuntime(
            self.market_id,
            self.config,
            min_request_interval_seconds=float(interval or 0.0),
        )

    @property
    def api_base_url(self) -> str:
        configured = self.config.get("crypto_com_predict_api_base_url") or self.config.get("api_base_url")
        return str(configured or DEFAULT_CRYPTO_COM_PREDICT_BASE_URL).rstrip("/")

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        credential = self._api_key()
        health.update(
            {
                "api_base_url": self.api_base_url,
                "references": list(self.references),
                "anonymous_read_access": True,
                "anonymous_rate_limit_per_minute": 100,
                "anonymous_rate_limit_per_day": 50_000,
                "api_key_configured": credential is not None,
                "api_key_source": credential.source if credential else "anonymous",
                "license_notice": (
                    "Personal non-commercial reads are anonymous; redistribution, commercial use, "
                    "or model training requires a Crypto.com Market Data License."
                ),
                "orderbook_supported": True,
                "orderbook_depth_supported": False,
                "live_trading_supported": False,
                "copy_trading_supported": False,
            }
        )
        return health

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = max(1, min(int(limit or 50), 100))
        clean_query = str(query or "").strip()
        path = "/events/search" if clean_query else "/events"
        params: Dict[str, Any] = {"limit": desired}
        if clean_query:
            params["q"] = clean_query
        data = self._get(path, params=params)
        return [self._event_from_payload(event) for event in self._payload_list(data)]

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        clean_event_id = str(event_id or "").strip()
        if not clean_event_id:
            raise MarketConfigurationError("Crypto.com Predictions event id cannot be empty.")
        encoded = quote(clean_event_id, safe="")
        data = self._get(f"/events/{encoded}/contracts")
        return [
            self._contract_from_payload(contract, clean_event_id)
            for contract in self._payload_list(data)
            if self._contract_symbol(contract)
        ]

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        symbol = str(contract_id or "").strip()
        if not symbol:
            raise MarketConfigurationError("Crypto.com Predictions contract symbol cannot be empty.")
        data = self._get(f"/contracts/{quote(symbol, safe='')}/price")
        payload = self._payload_mapping(data)
        bid = self._safe_probability(payload.get("bid"))
        ask = self._safe_probability(payload.get("ask"))
        midpoint = self._safe_probability(payload.get("mid"))
        if midpoint is None and bid is not None and ask is not None:
            midpoint = (bid + ask) / 2.0
        last = self._safe_probability(payload.get("probability"))
        if last is None:
            last = midpoint
        if all(value is None for value in (last, bid, ask, midpoint)):
            raise MarketHTTPError(
                f"Crypto.com Predictions price response for {symbol!r} contained no usable price fields."
            )
        response_symbol = self._contract_symbol(payload) or symbol
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=response_symbol,
            last=last,
            bid=bid,
            ask=ask,
            midpoint=midpoint,
            source=self.price_source,
            raw=dict(payload),
        )

    def get_orderbook(self, contract_id: str) -> OrderBookSnapshot:
        """Return the official bid/ask quote as a one-level book.

        The market-data contract does not publish quote depth or quantities,
        so sizes are explicitly represented as unknown ``0.0`` values and the
        raw response marks this as a top-of-book-only snapshot.
        """
        self.ensure_capability("orderbook_reading")
        price = self.get_price(contract_id)
        bids = [OrderBookLevel(price=price.bid, size=0.0)] if price.bid is not None else []
        asks = [OrderBookLevel(price=price.ask, size=0.0)] if price.ask is not None else []
        return OrderBookSnapshot(
            market_id=self.market_id,
            contract_id=price.contract_id,
            bids=bids,
            asks=asks,
            raw={
                "price": dict(price.raw),
                "depth": "top_of_book_only",
                "size_available": False,
            },
        )

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        self._validate_order(order)
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=str(order.contract_id).strip(),
            accepted=True,
            message=(
                f"DRY RUN: would place {self.provider_label} {str(order.side).upper()} "
                f"for {float(order.size):.4f} contracts"
                + (f" at limit {float(order.limit_price):.4f}" if order.limit_price is not None else "")
            ),
            filled_size=0.0,
            average_price=None,
            raw={"official_api_is_read_only": True},
        )

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        raise UnsupportedFeatureError(
            self.market_id,
            "live_trading",
            f"{self.provider_label}'s official Predictions API is market-data-only and publishes no order endpoint.",
        )

    def copy_trade_from_activity(self, activity: Mapping[str, Any]) -> PaperOrderResult:
        raise UnsupportedFeatureError(
            self.market_id,
            "copy_trading",
            f"{self.provider_label}'s official Predictions API publishes no account activity mirroring endpoint.",
        )

    def _get(self, path: str, *, params: Optional[Mapping[str, Any]] = None) -> Any:
        clean_path = "/" + str(path or "").strip("/")
        return self.runtime.get_json(
            f"{self.api_base_url}{clean_path}",
            params=params,
            headers=self._headers(),
        )

    def _headers(self) -> Dict[str, str]:
        credential = self._api_key()
        return {"X-API-Key": credential.value} if credential else {}

    def _api_key(self):
        return self.resolve_credential(
            "crypto_com_predict_api_key",
            ("CRYPTO_COM_PREDICTIONS_API_KEY",),
            required=False,
            label="Crypto.com Predictions API key",
        )

    def _event_from_payload(self, payload: Mapping[str, Any]) -> MarketEvent:
        event_id = str(payload.get("id") or "").strip()
        return MarketEvent(
            market_id=self.market_id,
            event_id=event_id,
            title=str(payload.get("title") or event_id),
            url=str(payload.get("url") or self.market_homepage_url).strip(),
            status=str(payload.get("status") or "").strip().lower(),
            raw=dict(payload),
        )

    def _contract_from_payload(self, payload: Mapping[str, Any], event_id: str) -> MarketContract:
        symbol = self._contract_symbol(payload)
        title = str(payload.get("title") or symbol)
        return MarketContract(
            market_id=self.market_id,
            contract_id=symbol,
            event_id=event_id,
            title=title,
            outcome=title,
            url=str(payload.get("url") or self.market_homepage_url).strip(),
            status=str(payload.get("status") or "").strip().lower(),
            raw=dict(payload),
        )

    def _validate_order(self, order: PaperOrderRequest) -> None:
        self.ensure_order_market(order)
        if not str(order.contract_id or "").strip():
            raise MarketConfigurationError(f"{self.provider_label} paper order requires a contract symbol.")
        if str(order.side or "").upper() not in {"BUY", "SELL"}:
            raise MarketConfigurationError(f"{self.provider_label} paper order side must be BUY or SELL.")
        if not self._is_positive_number(order.size):
            raise MarketConfigurationError(f"{self.provider_label} paper order size must be positive.")
        if order.limit_price is not None:
            try:
                limit_price = float(order.limit_price)
            except (TypeError, ValueError) as exc:
                raise MarketConfigurationError(
                    f"{self.provider_label} paper order limit price must be between 0 and 1."
                ) from exc
            if not math.isfinite(limit_price) or not 0.0 <= limit_price <= 1.0:
                raise MarketConfigurationError(
                    f"{self.provider_label} paper order limit price must be between 0 and 1."
                )

    @staticmethod
    def _payload_list(data: Any) -> List[Mapping[str, Any]]:
        if not isinstance(data, Mapping):
            return []
        payload = data.get("data")
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, Mapping)]

    @staticmethod
    def _payload_mapping(data: Any) -> Mapping[str, Any]:
        if not isinstance(data, Mapping) or not isinstance(data.get("data"), Mapping):
            raise MarketHTTPError("Crypto.com Predictions response did not contain a data object.")
        return data["data"]

    @staticmethod
    def _contract_symbol(payload: Mapping[str, Any]) -> str:
        return str(payload.get("symbol") or payload.get("id") or "").strip()

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
                number /= 100.0
            else:
                return None
        return number if 0.0 <= number <= 1.0 else None

    @staticmethod
    def _is_positive_number(value: Any) -> bool:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False
        return math.isfinite(number) and number > 0


class FanaticsMarketsAdapter(CryptoComPredictAdapter):
    """Read-only Fanatics Markets alias backed by the official CDNA data API.

    Fanatics documents its market product as an intermediary/partner experience
    whose event contracts are listed and priced on Crypto.com Derivatives North
    America (CDNA).  No Fanatics-specific public order endpoint is published,
    so this adapter intentionally exposes only discovery, prices, alerts, and
    local dry-run orders.
    """

    metadata = get_market_metadata("fanatics_markets")
    provider_label = "Fanatics Markets/CDNA"
    market_homepage_url = "https://fanaticsmarkets.com"
    price_source = "cdna_predictions_market_data"
    references = FANATICS_MARKETS_REFERENCES

    @property
    def api_base_url(self) -> str:
        configured = (
            self.config.get("fanatics_markets_api_base_url")
            or self.config.get("crypto_com_predict_api_base_url")
            or self.config.get("api_base_url")
        )
        return str(configured or DEFAULT_CRYPTO_COM_PREDICT_BASE_URL).rstrip("/")

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        health.update(
            {
                "alias_of": "crypto_com_predict",
                "underlying_market_data_provider": "Crypto.com Derivatives North America (CDNA)",
                "intermediary": "Fanatics Markets / Paragon Global Markets",
                "fanatics_order_api_supported": False,
                "live_trading_supported": False,
                "copy_trading_supported": False,
                "license_notice": (
                    "CDNA Predictions API anonymous reads are for personal non-commercial use; "
                    "commercial redistribution or model-training use requires a Market Data License. "
                    "Fanatics account trading is not automated here."
                ),
            }
        )
        return health

    def _api_key(self):
        return self.resolve_credential(
            "fanatics_markets_api_key",
            ("FANATICS_MARKETS_API_KEY", "CRYPTO_COM_PREDICTIONS_API_KEY"),
            required=False,
            label="Fanatics Markets/CDNA Predictions API key",
        )
