"""FanDuel Predicts market-data adapter.

FanDuel's official product documentation identifies two underlying contract
venues: CME Group and Crypto.com's OG Prediction Markets/CDNA.  FanDuel does
not publish a separate public automation API, so this adapter intentionally
covers the documented OG/CDNA market-data surface only.  CME-listed contracts
remain available through :class:`CMEPredictionMarketsAdapter`.
"""

from __future__ import annotations

from typing import Any, Dict

from .catalog import get_market_metadata
from .crypto_com_predict import (
    CRYPTO_COM_PREDICT_REFERENCES,
    DEFAULT_CRYPTO_COM_PREDICT_BASE_URL,
    CryptoComPredictAdapter,
)


FANDUEL_PREDICTS_REFERENCES = (
    "https://www.fanduel.com/predicts",
    "https://www.fanduel.com/predicts-riskdisclosures",
    "https://www.fanduel.com/about/news/fanduel-predicts-to-expand-event-contract-offering-through-partnership-with-crypto-com-and-og-prediction-markets",
    *CRYPTO_COM_PREDICT_REFERENCES,
)


class FanDuelPredictsAdapter(CryptoComPredictAdapter):
    """Read-only FanDuel Predicts alias over the official OG/CDNA API.

    FanDuel's published announcement says its contracts are listed by both
    CME Group and Crypto.com's derivatives exchanges.  The public data API
    used here is the documented Crypto.com Predictions API; it does not
    automate FanDuel accounts, private app endpoints, or CME access.
    """

    metadata = get_market_metadata("fanduel_predicts")
    provider_label = "FanDuel Predicts/OG"
    market_homepage_url = "https://www.fanduel.com/predicts"
    price_source = "og_prediction_markets_market_data"
    references = FANDUEL_PREDICTS_REFERENCES

    @property
    def api_base_url(self) -> str:
        configured = (
            self.config.get("fanduel_predicts_api_base_url")
            or self.config.get("crypto_com_predict_api_base_url")
            or self.config.get("api_base_url")
        )
        return str(configured or DEFAULT_CRYPTO_COM_PREDICT_BASE_URL).rstrip("/")

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        health.update(
            {
                "alias_of": "crypto_com_predict",
                "underlying_market_data_provider": "Crypto.com OG Prediction Markets / CDNA",
                "fanduel_order_api_supported": False,
                "cme_contracts_api_supported": False,
                "supported_public_data_scope": "FanDuel-listed OG/CDNA contracts; CME-listed contracts use cme_prediction_markets",
                "live_trading_supported": False,
                "copy_trading_supported": False,
                "license_notice": (
                    "OG/CDNA Predictions API anonymous reads are for personal non-commercial use; "
                    "commercial redistribution or model-training use requires a Market Data License. "
                    "FanDuel account trading is not automated here."
                ),
            }
        )
        return health

    def _api_key(self):
        return self.resolve_credential(
            "fanduel_predicts_api_key",
            ("FANDUEL_PREDICTS_API_KEY", "CRYPTO_COM_PREDICTIONS_API_KEY"),
            required=False,
            label="FanDuel Predicts/OG Predictions API key",
        )
