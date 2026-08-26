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
    if capabilities.get("alerts") and not capabilities.get("price_reading"):
        issues.append("alerts require price_reading for the shared price-trigger engine")

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


def account_surface_issues(
    adapter: MarketAdapter,
    *,
    cli_account_operations: frozenset[str] = frozenset(),
    cli_order_operations: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    """Check that declared authenticated operations have concrete surfaces.

    Account and order-management operations are intentionally separate from
    public capability flags.  They still must have an adapter implementation,
    and when a CLI operation registry is supplied the operation must be
    selectable there so CLI/API/React cannot drift apart.
    """

    issues: list[str] = []
    if adapter.account_recovery_operations:
        if type(adapter).account_recovery is MarketAdapter.account_recovery:
            issues.append("account recovery operations are declared but account_recovery is not implemented")
        missing = sorted(set(adapter.account_recovery_operations) - cli_account_operations)
        if missing:
            issues.append("CLI account operation(s) missing: " + ", ".join(missing))
    if adapter.order_management_operations:
        if type(adapter).manage_orders is MarketAdapter.manage_orders:
            issues.append("order-management operations are declared but manage_orders is not implemented")
        missing = sorted(set(adapter.order_management_operations) - cli_order_operations)
        if missing:
            issues.append("CLI order operation(s) missing: " + ", ".join(missing))
    return tuple(issues)
