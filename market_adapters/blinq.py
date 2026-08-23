"""Blinq market-data adapter.

Blinq's public product page describes its leverage layer as trading Polymarket
markets.  Blinq does not publish a separate public market-data, leverage, or
wallet execution API, so this adapter intentionally exposes only the official
Polymarket public data surface for markets surfaced by Blinq.  It never
automates Blinq accounts, leverage, deposits, or private endpoints.
"""

from __future__ import annotations

from typing import Any, Dict

from .catalog import get_market_metadata
from .polymarket import PolymarketAdapter


BLINQ_REFERENCES = (
    "https://blinq.fi/",
    "https://predictions.blinq.fi/",
    "https://docs.polymarket.com/api-reference/trade/get-trades",
    "https://docs.polymarket.com/api-reference/markets/get-prices-history",
)


class BlinqAdapter(PolymarketAdapter):
    """Read-only Blinq alias over the documented Polymarket data APIs."""

    metadata = get_market_metadata("blinq")

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        health.update(
            {
                "alias_of": "polymarket",
                "underlying_market_data_provider": "Polymarket",
                "supported_public_data_scope": (
                    "Polymarket markets surfaced by Blinq, including public price history and "
                    "authenticated read-only CLOB trade history"
                ),
                "trade_history_requires_l2_auth": True,
                "references": list(BLINQ_REFERENCES),
                "blinq_leverage_api_supported": False,
                "blinq_wallet_api_supported": False,
                "live_trading_supported": False,
                "copy_trading_supported": False,
                "live_trading_enabled": False,
                "license_notice": (
                    "This adapter reads the public Polymarket market-data surface only. "
                    "Blinq leverage, deposits, private account actions, and wallet execution "
                    "are not automated here."
                ),
            }
        )
        return health
