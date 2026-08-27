"""Blinq market-data adapter.

Blinq's public product page describes its leverage layer as trading Polymarket
markets.  Blinq does not publish a separate public market-data, leverage, or
wallet execution API, so this adapter intentionally exposes only the official
Polymarket public data surface for markets surfaced by Blinq.  It never
automates Blinq accounts, leverage, deposits, or private endpoints.
"""

from __future__ import annotations

from typing import Any, Dict

from .base import MarketAdapter
from .catalog import get_market_metadata
from .errors import UnsupportedFeatureError
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
    # Polymarket's inherited private account and mutation methods must not
    # leak into this read-only distribution alias or the UI/API surface.
    account_recovery_operations = ()
    order_management_operations = ()

    def health_check(self) -> Dict[str, Any]:
        # Start from the generic health payload rather than Polymarket's
        # private-wallet readiness report.  Blinq supports only the underlying
        # market-data contract, with optional L2 headers for GET /trades.
        health = MarketAdapter.health_check(self)
        health.update(
            {
                "alias_of": "polymarket",
                "underlying_market_data_provider": "Polymarket",
                "supported_public_data_scope": (
                    "Polymarket markets surfaced by Blinq, including public price history and "
                    "authenticated read-only CLOB trade history"
                ),
                "trade_history_requires_l2_auth": True,
                "credential_requirement": "optional_readonly_l2_trade_history_only",
                "authenticated_readonly_market_data_endpoints": ["GET /trades"],
                "account_recovery_operations": [],
                "order_management_operations": [],
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

    def account_recovery(self, operation: str, **kwargs: Any) -> Any:
        """Reject Polymarket private-account reads inherited by the alias."""

        del operation, kwargs
        raise UnsupportedFeatureError(
            self.market_id,
            "account_recovery",
            (
                "Blinq private account recovery is not supported by this read-only adapter. "
                "Optional Polymarket L2 credentials are used only for CLOB trade-history reads."
            ),
        )

    def place_live_order(self, order: Any) -> Dict[str, Any]:
        """Reject the inherited Polymarket mutation boundary for this alias."""

        del order
        raise UnsupportedFeatureError(
            self.market_id,
            "live_trading",
            "Blinq leverage and wallet execution are not supported by this read-only adapter.",
        )

    def get_account_orders(
        self,
        *,
        market_id: str = "",
        contract_id: str = "",
        next_cursor: str = "",
    ) -> Dict[str, Any]:
        """Reject direct access to inherited private Polymarket orders."""

        return self.account_recovery(
            "active_orders",
            market_id=market_id,
            contract_id=contract_id,
            next_cursor=next_cursor,
        )

    def get_account_order(self, order_id: str) -> Dict[str, Any]:
        """Reject direct access to an inherited private Polymarket order."""

        return self.account_recovery("order_detail", order_id=order_id)

    def get_account_fills(
        self,
        *,
        trade_id: str = "",
        market_id: str = "",
        contract_id: str = "",
        before: Any = None,
        after: Any = None,
        next_cursor: str = "",
        limit: Any = 100,
    ) -> Dict[str, Any]:
        """Reject direct access to inherited private Polymarket fills."""

        return self.account_recovery(
            "fills",
            trade_id=trade_id,
            market_id=market_id,
            contract_id=contract_id,
            before=before,
            after=after,
            next_cursor=next_cursor,
            limit=limit,
        )
