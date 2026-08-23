"""Canonical support-state reporting for every catalog market.

Capability flags describe what an adapter has deliberately implemented.  They
do not, by themselves, explain whether an operation is locally runnable,
safety-gated, or blocked by the upstream venue.  This module turns that
contract into a stable, JSON-serialisable matrix shared by the web API and
headless CLI.

The matrix is intentionally descriptive.  It never turns an unsupported or
blocked operation into a successful stub, and it never treats a live/copy
capability as permission to send an order without the adapter's safety gates.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from .base import MarketAdapter
from .capability_audit import capability_contract_issues
from .types import MarketMetadata


SUPPORT_OPERATIONS = (
    "market_discovery",
    "event_listing",
    "price_reading",
    "orderbook_reading",
    "trade_history",
    "candle_history",
    "alerts",
    "paper_trading",
    "live_trading",
    "copy_trading",
)


_CAPABILITY_LABELS = {
    "market_discovery": "market discovery",
    "event_listing": "event and contract listing",
    "price_reading": "price reading",
    "orderbook_reading": "orderbook reading",
    "trade_history": "trade history",
    "candle_history": "candle history",
    "alerts": "price alerts",
    "paper_trading": "paper trading",
    "live_trading": "live trading",
    "copy_trading": "copy trading",
}


def _blocker_payload(blocker: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if not blocker:
        return None
    payload: Dict[str, Any] = {
        "reason": str(blocker.get("reason") or "Verified upstream blocker."),
        "references": [str(value) for value in blocker.get("references") or [] if str(value).strip()],
    }
    if blocker.get("last_reviewed"):
        payload["last_reviewed"] = str(blocker["last_reviewed"])
    return payload


def _state(
    status: str,
    reason: str,
    *,
    advertised: bool = False,
    guarded: bool = False,
) -> Dict[str, Any]:
    return {
        "status": status,
        "advertised": bool(advertised),
        "guarded": bool(guarded),
        "reason": reason,
    }


def _operation_state(
    capability: str,
    advertised: bool,
    *,
    blocker: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    label = _CAPABILITY_LABELS[capability]
    if blocker:
        return _state(
            "blocked",
            str(blocker.get("reason") or f"{label.title()} is blocked by the verified upstream review."),
            advertised=advertised,
        )
    if not advertised:
        return _state(
            "unsupported",
            f"{label.title()} is not advertised by this adapter and fails closed.",
        )
    if capability == "live_trading":
        return _state(
            "guarded",
            "Implemented behind adapter credentials, venue eligibility, funding, live-safety acknowledgement, and kill-switch gates.",
            advertised=True,
            guarded=True,
        )
    if capability == "copy_trading":
        return _state(
            "guarded",
            "Implemented only for the adapter's documented activity feed and remains configuration/safety gated.",
            advertised=True,
            guarded=True,
        )
    if capability == "paper_trading":
        return _state(
            "supported",
            "Local dry-run orders are supported; no upstream order is sent.",
            advertised=True,
        )
    return _state("supported", f"{label.title()} is implemented by the adapter.", advertised=True)


def support_matrix_entry(
    metadata: MarketMetadata,
    adapter: Optional[MarketAdapter] = None,
    *,
    blocker: Optional[Mapping[str, Any]] = None,
    adapter_error: str = "",
) -> Dict[str, Any]:
    """Return one stable support-matrix row for a catalog entry.

    ``metadata`` is authoritative for advertised capabilities.  This matters
    for verified-blocked adapters because their fail-closed stub intentionally
    clears runtime capabilities while the catalog still records the reviewed
    product scope.
    """

    blocker_payload = _blocker_payload(blocker)
    capabilities = metadata.capabilities.to_dict()
    operations = {
        capability: _operation_state(
            capability,
            bool(capabilities.get(capability, False)),
            blocker=blocker_payload,
        )
        for capability in SUPPORT_OPERATIONS
    }

    account_operations = tuple(getattr(adapter, "account_recovery_operations", ()) or ()) if adapter else ()
    order_operations = tuple(getattr(adapter, "order_management_operations", ()) or ()) if adapter else ()

    if blocker_payload:
        account_state = _state("blocked", blocker_payload["reason"])
        order_state = _state("blocked", blocker_payload["reason"])
    elif account_operations:
        account_state = _state(
            "guarded",
            "Credentialed account reads are implemented; account eligibility and credentials remain external gates.",
            advertised=True,
            guarded=True,
        )
    else:
        account_state = _state("unsupported", "No documented authenticated account-read operations are declared.")

    if blocker_payload:
        order_state = _state("blocked", blocker_payload["reason"])
    elif order_operations:
        order_state = _state(
            "guarded",
            "Mutating order management is implemented behind explicit opt-in, confirmation, credentials, and live-safety gates.",
            advertised=True,
            guarded=True,
        )
    else:
        order_state = _state("unsupported", "No documented authenticated order-management operations are declared.")

    if adapter_error:
        implementation_status = "unavailable"
        implementation_reason = adapter_error
    elif blocker_payload:
        implementation_status = "verified_blocked"
        implementation_reason = blocker_payload["reason"]
    elif adapter is None:
        implementation_status = "unavailable"
        implementation_reason = "Adapter instance could not be created for this catalog row."
    else:
        implementation_status = "implemented"
        implementation_reason = "Adapter registered and support contract loaded."

    statuses = [item["status"] for item in operations.values()] + [account_state["status"], order_state["status"]]
    counts = {status: statuses.count(status) for status in ("supported", "guarded", "unsupported", "blocked")}
    audit_issues = list(capability_contract_issues(adapter)) if adapter is not None else []

    return {
        "market_id": metadata.market_id,
        "display_name": metadata.display_name,
        "implementation_status": implementation_status,
        "implementation_reason": implementation_reason,
        "adapter": type(adapter).__name__ if adapter is not None else "",
        "operations": operations,
        "account_recovery": {
            **account_state,
            "operations": [str(value) for value in account_operations],
        },
        "order_management": {
            **order_state,
            "operations": [str(value) for value in order_operations],
        },
        "requirements": {
            "api_required": bool(capabilities.get("api_required")),
            "credentials_required": bool(capabilities.get("credentials_required")),
            "kyc_required": bool(capabilities.get("kyc_required")),
            "region_limited": bool(capabilities.get("region_limited")),
        },
        "blocker": blocker_payload,
        "audit": {
            "capability_contract_issues": audit_issues,
            "ok": not audit_issues and implementation_status != "unavailable",
        },
        "counts": counts,
    }


__all__ = ["SUPPORT_OPERATIONS", "support_matrix_entry"]
