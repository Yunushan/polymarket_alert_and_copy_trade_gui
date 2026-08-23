from __future__ import annotations

from typing import Any, Dict

from .catalog import get_market_metadata
from .kalshi import KalshiAdapter


DEFAULT_COINBASE_PREDICTION_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
COINBASE_PREDICTION_REFERENCES = (
    "https://www.coinbase.com/blog/system-update-the-future-of-prediction-markets",
    "https://help.coinbase.com/en/coinbase/trading-and-funding/prediction-markets/intro",
    "https://help.coinbase.com/en/coinbase/trading-and-funding/prediction-markets/payouts",
    "https://docs.kalshi.com/getting_started/quick_start_market_data",
)


class CoinbasePredictionMarketsAdapter(KalshiAdapter):
    """Read-only Coinbase prediction-market alias over Coinbase's Kalshi venue.

    Coinbase documents that prediction-market flow is supplied by Kalshi and
    directs users to the corresponding Kalshi market for contract outcomes.
    Coinbase-specific account/order endpoints are not published, so this
    adapter intentionally exposes only the documented public Kalshi market
    data surface plus local paper orders.
    """

    metadata = get_market_metadata("coinbase_prediction_markets")
    # Coinbase documents the Kalshi venue only for public distribution data;
    # Coinbase account/order endpoints are not exposed through this alias.
    account_recovery_operations = ()
    order_management_operations = ()

    @property
    def api_base_url(self) -> str:
        configured = (
            self.config.get("coinbase_prediction_markets_api_base_url")
            or self.config.get("kalshi_api_base_url")
            or self.config.get("api_base_url")
        )
        return str(configured or DEFAULT_COINBASE_PREDICTION_BASE_URL).rstrip("/")

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        health.update(
            {
                "alias_of": "kalshi",
                "provider": "Coinbase Financial Markets / Kalshi",
                "references": list(COINBASE_PREDICTION_REFERENCES),
                "coinbase_order_api_supported": False,
                "live_trading_supported": False,
                "copy_trading_supported": False,
                "market_data_scope": "public Kalshi venue data; Coinbase account execution is not exposed",
            }
        )
        return health
