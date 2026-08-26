"""Nadex/CDNA read-only prediction-market adapter.

Nadex and Crypto.com Derivatives North America (CDNA) are the same regulated
exchange group for the prediction-event surface.  Nadex does not publish a
public order API for this application, so this adapter deliberately exposes
only the documented Crypto.com Predictions market-data contract and local
paper orders.  It does not imply support for Nadex knock-outs, DCM/FIX depth,
account automation, or live execution.
"""

from __future__ import annotations

from typing import Any, Dict

from .catalog import get_market_metadata
from .crypto_com_predict import (
    DEFAULT_CRYPTO_COM_PREDICT_BASE_URL,
    CryptoComPredictAdapter,
)


NADEX_REFERENCES = (
    "https://www.nadex.com/product-market/",
    "https://www.nadex.com/rules/",
    "https://www.nadex.com/learning/what-are-event-contracts-and-how-do-they-work/",
    "https://data-api.crypto.com/docs",
    "https://data.crypto.com/quickstart",
)


class NadexAdapter(CryptoComPredictAdapter):
    """Read-only Nadex/CDNA alias over the official Predictions API."""

    metadata = get_market_metadata("nadex")
    provider_label = "Nadex/CDNA Predictions"
    market_homepage_url = "https://www.nadex.com/product-market/"
    price_source = "nadex_cdna_predictions_market_data"
    references = NADEX_REFERENCES

    @property
    def api_base_url(self) -> str:
        configured = (
            self.config.get("nadex_api_base_url")
            or self.config.get("crypto_com_predict_api_base_url")
            or self.config.get("api_base_url")
        )
        return str(configured or DEFAULT_CRYPTO_COM_PREDICT_BASE_URL).rstrip("/")

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        health.update(
            {
                "alias_of": "crypto_com_predict",
                "underlying_market_data_provider": "Crypto.com Derivatives North America (CDNA/Nadex)",
                "supported_public_data_scope": "CDNA/Nadex prediction-event market data only",
                "nadex_order_api_supported": False,
                "dcm_fix_api_supported": False,
                "knockout_contracts_supported": False,
                "live_trading_supported": False,
                "copy_trading_supported": False,
                "license_notice": (
                    "Anonymous CDNA Predictions API reads are for personal non-commercial use; commercial "
                    "redistribution or model-training use requires a Market Data License. Nadex account "
                    "trading is not automated here."
                ),
            }
        )
        return health

    def _api_key(self):
        return self.resolve_credential(
            "nadex_api_key",
            ("NADEX_PREDICTIONS_API_KEY", "CRYPTO_COM_PREDICTIONS_API_KEY"),
            required=False,
            label="Nadex/CDNA Predictions API key",
        )
