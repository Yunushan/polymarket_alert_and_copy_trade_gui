"""DraftKings Predictions market-data alias.

DraftKings documents its Predictions catalog as a distribution experience for
CDNA event contracts, alongside CME-listed contracts.  No public DraftKings
account/order API is documented, so this adapter routes only the published
Crypto.com/CDNA market-data API and keeps live and copy trading disabled.
"""

from __future__ import annotations

from typing import Any, Dict

from .catalog import get_market_metadata
from .crypto_com_predict import (
    CRYPTO_COM_PREDICT_REFERENCES,
    DEFAULT_CRYPTO_COM_PREDICT_BASE_URL,
    CryptoComPredictAdapter,
)


DRAFTKINGS_PREDICTIONS_REFERENCES = (
    "https://predictions.draftkings.com/en",
    "https://ir.aboutdraftkings.com/files/doc_news/2026/DraftKings-Expands-Prediction-Markets-Catalog-in-Deal-With-Crypto-com.pdf",
    *CRYPTO_COM_PREDICT_REFERENCES,
)


class DraftKingsPredictionsAdapter(CryptoComPredictAdapter):
    """Read-only DraftKings Predictions alias over official CDNA data."""

    metadata = get_market_metadata("draftkings_predictions")
    provider_label = "DraftKings Predictions/CDNA"
    market_homepage_url = "https://predictions.draftkings.com/en"
    price_source = "cdna_prediction_markets_market_data"
    references = DRAFTKINGS_PREDICTIONS_REFERENCES

    @property
    def api_base_url(self) -> str:
        configured = (
            self.config.get("draftkings_predictions_api_base_url")
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
                "draftkings_order_api_supported": False,
                "cme_contracts_api_supported": False,
                "supported_public_data_scope": (
                    "DraftKings-listed CDNA event contracts; CME-listed contracts use "
                    "cme_prediction_markets"
                ),
                "live_trading_supported": False,
                "copy_trading_supported": False,
                "license_notice": (
                    "Anonymous reads use the official CDNA market-data surface; redistribution or "
                    "commercial use requires the applicable Market Data License. DraftKings account "
                    "execution is not automated."
                ),
            }
        )
        return health

    def _api_key(self):
        return self.resolve_credential(
            "draftkings_predictions_api_key",
            ("DRAFTKINGS_PREDICTIONS_API_KEY", "CRYPTO_COM_PREDICTIONS_API_KEY"),
            required=False,
            label="DraftKings Predictions/CDNA API key",
        )
