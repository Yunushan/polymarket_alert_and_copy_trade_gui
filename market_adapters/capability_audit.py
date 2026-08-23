"""Static contract checks for advertised market-adapter capabilities.

The catalog is intentionally honest: a capability flag is a promise that the
adapter exposes a concrete operation, not merely that the base class has a
method with the same name.  This module keeps that promise machine-checkable
for every catalog row, including the verified-blocked rows.
"""

from __future__ import annotations

from typing import Mapping

from .base import MarketAdapter


# ``market_discovery`` is represented by the catalog metadata itself.  The
# operational discovery surface is ``event_listing`` (events plus contracts).
# Every other entry maps directly to one or more public adapter operations.
CORE_CAPABILITY_METHODS: Mapping[str, tuple[str, ...]] = {
    "event_listing": ("list_events", "list_contracts"),
    "price_reading": ("get_price",),
    "orderbook_reading": ("get_orderbook",),
    "trade_history": ("list_trades",),
    "candle_history": ("list_candles",),
    "paper_trading": ("place_paper_order",),
    "live_trading": ("place_live_order",),
    "copy_trading": ("copy_trade_from_activity",),
}


def capability_contract_issues(adapter: MarketAdapter) -> tuple[str, ...]:
    """Return contradictions between an adapter's flags and its class methods.

    Comparing methods on the class (rather than calling live endpoints) keeps
    this check deterministic and credential-free.  Inherited concrete methods
    from another official adapter are valid; only methods that still resolve
    to :class:`MarketAdapter`'s fail-closed implementation are rejected.
    """

    capabilities = adapter.capabilities.to_dict()
    issues: list[str] = []

    if capabilities.get("market_discovery") and not capabilities.get("event_listing"):
        issues.append("market_discovery requires event_listing")

    for capability, method_names in CORE_CAPABILITY_METHODS.items():
        if not capabilities.get(capability, False):
            continue
        missing = [
            method_name
            for method_name in method_names
            if getattr(type(adapter), method_name, None) is getattr(MarketAdapter, method_name)
        ]
        if missing:
            issues.append(f"{capability} inherits unsupported operation(s): {', '.join(missing)}")

    return tuple(issues)

