from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict, Mapping, Optional, Sequence

from .base import MarketAdapter
from .errors import UnsupportedFeatureError
from .types import MarketCapabilities, MarketMetadata, PaperOrderRequest


class StubMarketAdapter(MarketAdapter):
    """Graceful placeholder for markets without an implemented adapter yet."""

    def __init__(
        self,
        metadata: MarketMetadata,
        config: Optional[Mapping[str, Any]] = None,
        reason: str = "",
    ) -> None:
        super().__init__(config=config)
        self.metadata = replace(metadata, capabilities=MarketCapabilities())
        self.runtime = self._create_runtime()
        self.reason = reason or (
            f"{self.display_name} is listed in the market catalog, but an official adapter "
            "has not been implemented yet."
        )

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        health.update({"ok": False, "message": self.reason, "stub": True})
        return health

    def ensure_capability(self, capability: str) -> None:
        raise UnsupportedFeatureError(self.market_id, capability, self.unsupported_message(capability))

    def unsupported_message(self, capability: str) -> str:
        return (
            f"{self.display_name} adapter does not currently support {capability}. "
            f"{self.reason}"
        )

    def list_events(self, query: str = "", limit: int = 50):
        self.ensure_capability("event_listing")

    def list_contracts(self, event_id: str):
        self.ensure_capability("event_listing")

    def get_price(self, contract_id: str):
        self.ensure_capability("price_reading")

    def get_orderbook(self, contract_id: str):
        self.ensure_capability("orderbook_reading")

    def list_trades(
        self,
        contract_id: str,
        *,
        limit: int = 50,
        before: Optional[float] = None,
        after: Optional[float] = None,
    ):
        """Reject history reads with the same market-specific blocker context.

        History was added after the original stub surface.  Keeping the
        operation explicit prevents verified-blocked markets from falling
        through to the generic base error and makes every adapter operation
        fail closed consistently.
        """

        self.ensure_capability("trade_history")

    def list_candles(
        self,
        contract_id: str,
        *,
        resolution: str = "1h",
        from_timestamp: Optional[float] = None,
        to_timestamp: Optional[float] = None,
    ):
        """Reject candle reads with the verified blocker explanation."""

        self.ensure_capability("candle_history")

    def place_paper_order(self, order: PaperOrderRequest):
        self.ensure_capability("paper_trading")

    def place_live_order(self, order: PaperOrderRequest):
        self.ensure_capability("live_trading")

    def copy_trade_from_activity(self, activity: Mapping[str, Any]):
        self.ensure_capability("copy_trading")


class VerifiedBlockedAdapter(StubMarketAdapter):
    """Stub adapter for markets whose official support was checked and blocked."""

    def __init__(
        self,
        metadata: MarketMetadata,
        config: Optional[Mapping[str, Any]] = None,
        reason: str = "",
        *,
        references: Sequence[str] = (),
        last_reviewed: str = "",
    ) -> None:
        super().__init__(metadata=metadata, config=config, reason=reason)
        self.references = tuple(str(ref) for ref in references if str(ref).strip())
        self.last_reviewed = str(last_reviewed or "")

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        health.update(
            {
                "verified_blocker": True,
                "last_reviewed": self.last_reviewed,
                "references": list(self.references),
            }
        )
        return health

    def unsupported_message(self, capability: str) -> str:
        return (
            f"{self.display_name} adapter is verified blocked for {capability}. "
            f"{self.reason}"
        )


def create_stub_adapter(
    metadata: MarketMetadata,
    config: Optional[Mapping[str, Any]] = None,
    reason: str = "",
) -> StubMarketAdapter:
    return StubMarketAdapter(metadata=metadata, config=config, reason=reason)


def create_verified_blocked_adapter(
    metadata: MarketMetadata,
    config: Optional[Mapping[str, Any]] = None,
    reason: str = "",
    *,
    references: Sequence[str] = (),
    last_reviewed: str = "",
) -> VerifiedBlockedAdapter:
    return VerifiedBlockedAdapter(
        metadata=metadata,
        config=config,
        reason=reason,
        references=references,
        last_reviewed=last_reviewed,
    )
