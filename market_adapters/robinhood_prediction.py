"""Robinhood prediction-market distribution aliases.

Robinhood's public product documentation identifies KalshiEX as the venue
behind its Prediction Markets hub.  Robinhood does not publish a separate
public order or account-activity API, so these adapters deliberately expose
the documented public Kalshi market-data surface and local paper orders only.
"""

from __future__ import annotations

from typing import Any, Dict

from .catalog import get_market_metadata
from .kalshi import DEFAULT_KALSHI_BASE_URL, KalshiAdapter


ROBINHOOD_PREDICTION_REFERENCES = (
    "https://robinhood.com/us/en/prediction-markets",
    "https://robinhood.com/us/en/newsroom/robinhood-prediction-markets-hub/",
    "https://docs.kalshi.com/getting_started/quick_start_market_data",
)


class _RobinhoodKalshiDistributionAlias(KalshiAdapter):
    """Common read-only Kalshi distribution alias behavior."""

    provider_name = "Robinhood Derivatives / KalshiEX"
    api_config_key = ""
    # The alias is intentionally read-only.  Inheriting Kalshi's private
    # portfolio methods must not make Robinhood account paths appear supported.
    account_recovery_operations = ()
    order_management_operations = ()

    @property
    def api_base_url(self) -> str:
        configured = (
            self.config.get(self.api_config_key)
            or self.config.get("kalshi_api_base_url")
            or self.config.get("api_base_url")
        )
        return str(configured or DEFAULT_KALSHI_BASE_URL).rstrip("/")

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        health.update(
            {
                "alias_of": "kalshi",
                "provider": self.provider_name,
                "references": list(ROBINHOOD_PREDICTION_REFERENCES),
                "robinhood_order_api_supported": False,
                "distribution_order_api_supported": False,
                "live_trading_supported": False,
                "copy_trading_supported": False,
                "supported_public_data_scope": (
                    "public Kalshi venue data distributed through Robinhood; "
                    "Robinhood account execution and activity are not exposed"
                ),
            }
        )
        return health


class RobinhoodPredictionMarketsAdapter(_RobinhoodKalshiDistributionAlias):
    """Read-only Robinhood Prediction Markets alias over Kalshi."""

    metadata = get_market_metadata("robinhood_prediction_markets")
    api_config_key = "robinhood_prediction_markets_api_base_url"


class KalshiViaRobinhoodAdapter(_RobinhoodKalshiDistributionAlias):
    """Explicit Kalshi-through-Robinhood distribution alias."""

    metadata = get_market_metadata("kalshi_via_robinhood")
    api_config_key = "kalshi_via_robinhood_api_base_url"
