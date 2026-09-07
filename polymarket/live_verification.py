from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import re
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence

from . import clob_rest, geoblock
from .auth_readiness import (
    build_clob_auth_readiness,
    is_evm_address_like,
    parse_signature_type,
    redacted_address,
)
from .http_client import PolymarketValidationError
from .live_report_schema import CREDENTIAL_PROMOTION_CHECKS, CREDENTIAL_PROMOTION_SEMANTICS
from .constants import (
    POLYMARKET_BOUNDED_AUDIT_MUTATION_BLOCKER,
    POLYMARKET_BOUNDED_AUDIT_MUTATIONS_SUPPORTED,
    POLYMARKET_CLOB_V2_MIGRATION_URL,
)
from .trader import PolymarketTrader, TraderConfig


CONFIRM_LIVE_ORDER_CANCEL = "I_UNDERSTAND_THIS_PLACES_A_REAL_POLYMARKET_ORDER"
ABSOLUTE_MAX_VERIFY_SIZE = 5.0
ABSOLUTE_MAX_VERIFY_NOTIONAL = 1.0
DEFAULT_MAKER_PRICE_BUFFER = 0.005
MIN_MAKER_PRICE_BUFFER = 0.001
POLYMARKET_BASE_UNITS_PER_USDC = 1_000_000


@dataclass(frozen=True)
class LiveOrderCancelRequest:
    token_id: str = ""
    side: str = ""
    price: Any = None
    size: Any = None
    tif: str = "GTC"
    allow_token_ids: Sequence[str] = ()
    private_key: str = ""
    funder_address: Optional[str] = None
    signature_type: Any = 0
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    api_passphrase: Optional[str] = None
    execute: bool = False
    cancel_immediately: bool = False
    confirmation: str = ""
    max_size: Any = ABSOLUTE_MAX_VERIFY_SIZE
    max_notional: Any = ABSOLUTE_MAX_VERIFY_NOTIONAL
    maker_price_buffer: Any = DEFAULT_MAKER_PRICE_BUFFER


def load_allow_token_ids(values: Iterable[str] = (), *, file_path: Optional[str] = None) -> list[str]:
    tokens = [str(value).strip() for value in values if str(value).strip()]
    if file_path:
        path = Path(file_path)
        for line in path.read_text(encoding="utf-8").splitlines():
            clean = line.split("#", 1)[0].strip()
            if clean:
                tokens.append(clean)
    out: list[str] = []
    for token in tokens:
        if token not in out:
            out.append(token)
    return out


def build_live_order_cancel_plan(request: LiveOrderCancelRequest) -> Dict[str, Any]:
    blockers: list[str] = []
    warnings: list[str] = []

    token_id = str(request.token_id or "").strip()
    side = str(request.side or "").strip().upper()
    tif = str(request.tif or "GTC").strip().upper()
    allow_token_ids = [str(token).strip() for token in request.allow_token_ids if str(token).strip()]

    price, price_error = _positive_float(request.price, "price")
    size, size_error = _positive_float(request.size, "size")
    max_size, max_size_error = _positive_float(request.max_size, "max size")
    max_notional, max_notional_error = _positive_float(request.max_notional, "max notional")
    maker_price_buffer, buffer_error = _positive_float(request.maker_price_buffer, "maker price buffer")

    for error in (price_error, size_error, max_size_error, max_notional_error, buffer_error):
        if error:
            blockers.append(error)

    if not token_id:
        blockers.append("Missing --token-id.")
    if side not in {"BUY", "SELL"}:
        blockers.append("Side must be BUY or SELL.")
    if price is not None and price >= 1:
        blockers.append("Price must be less than 1.")
    if tif != "GTC":
        blockers.append("Safe order/cancel verification requires TIF=GTC so the order can rest and be canceled.")
    if max_size is not None and max_size > ABSOLUTE_MAX_VERIFY_SIZE:
        blockers.append(f"Max size cap cannot exceed hard limit {ABSOLUTE_MAX_VERIFY_SIZE:g}.")
    if max_notional is not None and max_notional > ABSOLUTE_MAX_VERIFY_NOTIONAL:
        blockers.append(f"Max notional cap cannot exceed hard limit {ABSOLUTE_MAX_VERIFY_NOTIONAL:g} USDC.")
    if maker_price_buffer is not None and maker_price_buffer < MIN_MAKER_PRICE_BUFFER:
        blockers.append(
            f"Maker price buffer cannot be below the hard floor {MIN_MAKER_PRICE_BUFFER:g}."
        )
    if price is not None and size is not None:
        notional = price * size
        if max_size is not None and size > max_size:
            blockers.append(f"Size {size:g} exceeds max size cap {max_size:g}.")
        if max_notional is not None and notional > max_notional:
            blockers.append(f"Approx notional {notional:g} exceeds max notional cap {max_notional:g} USDC.")
    else:
        notional = None
    if not allow_token_ids:
        blockers.append("Missing token allow-list. Pass --allow-token-id or --allow-token-file.")
    elif token_id and token_id not in allow_token_ids:
        blockers.append("Token id is not present in the explicit allow-list.")
    if not request.cancel_immediately:
        blockers.append("Safe live verification requires --cancel-immediately.")

    try:
        signature_type = parse_signature_type(request.signature_type)
    except PolymarketValidationError as exc:
        blockers.append(str(exc))
        signature_type = 0

    readiness = build_clob_auth_readiness(
        {
            "private_key": request.private_key,
            "funder_address": request.funder_address or "",
            "signature_type": signature_type,
        },
        environ={},
    )
    if request.execute and readiness["blockers"]:
        blockers.extend(f"Auth readiness: {item}" for item in readiness["blockers"])
    if not request.private_key:
        warnings.append("Dry-run transcript only: private key is not present.")
    if not request.execute:
        warnings.append("Default dry-run mode: pass --allow-funded-order with exact confirmation text to execute.")
    if request.execute and request.confirmation != CONFIRM_LIVE_ORDER_CANCEL:
        blockers.append(f"Missing exact --confirm-live-order-cancel {CONFIRM_LIVE_ORDER_CANCEL!r}.")
    explicit_api_credentials = (
        str(request.api_key or "").strip(),
        str(request.api_secret or "").strip(),
        str(request.api_passphrase or "").strip(),
    )
    if any(explicit_api_credentials) and not all(explicit_api_credentials):
        blockers.append("Explicit Polymarket V2 API credentials require key, secret, and passphrase together.")
    if request.execute and not POLYMARKET_BOUNDED_AUDIT_MUTATIONS_SUPPORTED:
        blockers.append(POLYMARKET_BOUNDED_AUDIT_MUTATION_BLOCKER)

    if blockers:
        status = "blocked"
    elif request.execute and POLYMARKET_BOUNDED_AUDIT_MUTATIONS_SUPPORTED:
        # The execution harness stays testable, but the production constant
        # remains false pending exact-revision V2/recovery review and operator
        # approval. With the default constant, execution
        # is therefore blocked above before any transport-capable dependency
        # is constructed or called.
        status = "ready_to_execute"
    else:
        status = "dry_run"
    return {
        "status": status,
        "live_action": False,
        "execution_supported": POLYMARKET_BOUNDED_AUDIT_MUTATIONS_SUPPORTED,
        "execution_protocol_required": "CLOB V2",
        "migration_reference": POLYMARKET_CLOB_V2_MIGRATION_URL,
        "token_id": token_id,
        "side": side,
        "price": price,
        "size": size,
        "tif": tif,
        "approx_notional": notional,
        "caps": {
            "hard_max_size": ABSOLUTE_MAX_VERIFY_SIZE,
            "hard_max_notional": ABSOLUTE_MAX_VERIFY_NOTIONAL,
            "max_size": max_size,
            "max_notional": max_notional,
            "maker_price_buffer": maker_price_buffer,
        },
        "allow_list": {
            "count": len(allow_token_ids),
            "token_allowed": bool(token_id and token_id in allow_token_ids),
        },
        "auth_readiness": readiness,
        "redacted_credentials": {
            "private_key": "***" if request.private_key else "",
            "funder_address": redacted_address(request.funder_address),
            "signature_type": signature_type,
            "explicit_api_credentials": "***" if all(explicit_api_credentials) else "",
        },
        "required_execution_flags": [
            "--allow-funded-order",
            "--cancel-immediately",
            "--allow-token-id or --allow-token-file",
            "--confirm-live-order-cancel",
        ],
        "transcript": [
            "Validate token, side, price, size, TIF, caps, and allow-list.",
            "Validate private key, signature type, funder/deposit wallet, official host, and Polygon chain id.",
            "Inspect the public orderbook without placing an order.",
            "Use only an explicit CLOB V2 build/post path after a fail-closed server-version check.",
            "Stop before mutation while funded audit support remains disabled; never use a V1-signed fallback.",
        ],
        "blockers": blockers,
        "warnings": warnings,
    }


def _account_balance_allowance_preflight(trader: Any, plan: Mapping[str, Any]) -> Dict[str, Any]:
    response = trader.get_trading_balance_allowance(
        token_id=str(plan["token_id"]),
        side=str(plan["side"]),
    )
    if not isinstance(response, Mapping):
        raise ValueError("balance/allowance response must be an object")
    try:
        balance = float(str(response.get("balance")))
    except (TypeError, ValueError) as exc:
        raise ValueError("balance/allowance response has an invalid balance") from exc
    allowances = response.get("allowances")
    if not math.isfinite(balance) or balance < 0 or not isinstance(allowances, Mapping):
        raise ValueError("balance/allowance response has invalid numeric fields")
    required_units = math.ceil(
        float(plan["approx_notional"] if str(plan["side"]) == "BUY" else plan["size"])
        * POLYMARKET_BASE_UNITS_PER_USDC
    )
    sufficient_allowances = 0
    for value in allowances.values():
        try:
            allowance = float(str(value))
        except (TypeError, ValueError):
            continue
        if math.isfinite(allowance) and allowance >= required_units:
            sufficient_allowances += 1
    sufficient_balance = balance >= required_units
    sufficient_allowance = sufficient_allowances > 0
    passed = sufficient_balance and sufficient_allowance
    return {
        "status": "pass" if passed else "fail",
        "detail": (
            "Trading balance and at least one allowance cover the bounded verification order."
            if passed
            else "Trading balance or allowance is insufficient for the bounded verification order."
        ),
        "asset": "collateral" if str(plan["side"]) == "BUY" else "conditional",
        "required_base_units": required_units,
        "sufficient_balance": sufficient_balance,
        "sufficient_allowance": sufficient_allowance,
        "sufficient_allowance_count": sufficient_allowances,
    }


def _same_account_authenticated_read_preflight(trader: Any) -> tuple[Dict[str, Any], str]:
    account_address = trader.get_trading_account_address()
    if not is_evm_address_like(account_address):
        raise ValueError("trading client did not expose a valid EVM account identity")
    orders = trader.get_orders()
    if not isinstance(orders, list):
        raise ValueError("authenticated order-list response must be the documented list shape")
    for order in orders:
        if not isinstance(order, Mapping) or not extract_order_id(order):
            raise ValueError("authenticated order-list response contains an invalid order record")
    records_observed = len(orders)
    return (
        {
            "status": "pass",
            "detail": "The exact trading client completed a non-mutating authenticated order-list read.",
            "same_trading_client": True,
            "account_identity_present": True,
            "sample_type": type(orders).__name__,
            "records_observed": records_observed,
        },
        account_address,
    )


def _try_recovery_write(
    writer: Callable[[Mapping[str, Any]], None],
    payload: Mapping[str, Any],
    audit: Dict[str, Any],
) -> bool:
    try:
        writer(payload)
        return True
    except Exception as exc:
        audit["recovery_journal_error"] = _safe_exception_metadata(exc)
        return False


def run_live_order_cancel_verification(
    request: LiveOrderCancelRequest,
    *,
    trader_factory: Callable[[TraderConfig], Any] = PolymarketTrader,
    orderbook_getter: Callable[[str], Mapping[str, Any]] = clob_rest.get_book,
    geoblock_checker: Callable[[], Mapping[str, Any]] = geoblock.check_geoblock,
    recovery_writer: Optional[Callable[[Mapping[str, Any]], None]] = None,
) -> Dict[str, Any]:
    plan = build_live_order_cancel_plan(request)
    if plan["status"] != "ready_to_execute":
        return plan

    if recovery_writer is None:
        plan["status"] = "blocked"
        plan["live_action"] = False
        plan["blockers"].append(
            "Funded execution requires a durable recovery journal writer before placement."
        )
        return plan

    try:
        eligibility = geoblock_checker()
    except Exception as exc:
        plan["status"] = "blocked"
        plan["live_action"] = False
        plan["blockers"].append(
            f"Geographic eligibility could not be verified ({type(exc).__name__})."
        )
        return plan
    if not isinstance(eligibility, Mapping) or eligibility.get("blocked") is not False:
        plan["status"] = "blocked"
        plan["live_action"] = False
        plan["blockers"].append("Polymarket geographic eligibility did not return blocked=false.")
        return plan
    plan["geoblock_preflight"] = {
        "status": "pass",
        "blocked": False,
        "country_present": bool(str(eligibility.get("country") or "").strip()),
        "region_present": bool(str(eligibility.get("region") or "").strip()),
    }

    book = orderbook_getter(plan["token_id"])
    best_bid, best_ask = clob_rest.best_bid_ask_from_book(dict(book))
    maker_blocker = maker_price_blocker(
        side=str(plan["side"]),
        price=float(plan["price"]),
        best_bid=best_bid,
        best_ask=best_ask,
        buffer=float(plan["caps"]["maker_price_buffer"]),
    )
    plan["orderbook_preflight"] = {"best_bid": best_bid, "best_ask": best_ask}
    if maker_blocker:
        plan["status"] = "blocked"
        plan["live_action"] = False
        plan["blockers"].append(maker_blocker)
        return plan

    trader = trader_factory(
        TraderConfig(
            private_key=request.private_key,
            funder_address=request.funder_address or None,
            signature_type=int(plan["redacted_credentials"]["signature_type"]),
            api_key=request.api_key,
            api_secret=request.api_secret,
            api_passphrase=request.api_passphrase,
            bounded_audit=True,
        )
    )
    try:
        account_read_preflight, account_address = _same_account_authenticated_read_preflight(trader)
    except Exception as exc:
        plan["status"] = "blocked"
        plan["live_action"] = False
        plan["blockers"].append(
            f"Same-account authenticated read preflight failed ({type(exc).__name__})."
        )
        return plan
    plan["account_authenticated_read_preflight"] = account_read_preflight
    try:
        account_preflight = _account_balance_allowance_preflight(trader, plan)
    except Exception as exc:
        plan["status"] = "blocked"
        plan["live_action"] = False
        plan["blockers"].append(
            f"Trading balance/allowance preflight failed ({type(exc).__name__})."
        )
        return plan
    if account_preflight["status"] != "pass":
        plan["status"] = "blocked"
        plan["live_action"] = False
        plan["blockers"].append(str(account_preflight["detail"]))
        plan["account_preflight"] = account_preflight
        return plan
    plan["account_preflight"] = account_preflight
    plan["execution_guards"] = {
        "status": "pass",
        "post_only": True,
        "time_in_force": "GTC",
        "maker_price_verified": True,
    }

    recovery_base = {
        "schema_version": 1,
        "market_id": "polymarket",
        "token_id": str(plan["token_id"]),
        "side": str(plan["side"]),
        "price": float(plan["price"]),
        "size": float(plan["size"]),
        "tif": str(plan["tif"]),
        "post_only": True,
        "account_address": account_address,
    }
    try:
        recovery_writer(
            {
                **recovery_base,
                "stage": "placement_pending",
                "order_id": "",
                "manual_reconciliation_required": True,
                "resolved": False,
            }
        )
    except Exception as exc:
        plan["status"] = "blocked"
        plan["live_action"] = False
        plan["blockers"].append(
            f"Durable recovery journal could not be written ({type(exc).__name__})."
        )
        return plan

    try:
        placed = trader.place_limit_order(
            token_id=str(plan["token_id"]),
            side=str(plan["side"]),
            price=float(plan["price"]),
            size=float(plan["size"]),
            tif=str(plan["tif"]),
            post_only=True,
        )
    except Exception as exc:
        placement_audit: Dict[str, Any] = {}
        _try_recovery_write(
            recovery_writer,
            {
                **recovery_base,
                "stage": "placement_outcome_unknown",
                "order_id": "",
                "error_type": type(exc).__name__,
                "manual_reconciliation_required": True,
                "resolved": False,
            },
            placement_audit,
        )
        plan.update(
            {
                "status": "failed",
                "live_action": True,
                "manual_reconciliation_required": True,
                "failure": (
                    "The placement outcome is unknown. Use the durable journal identity to inspect and "
                    "cancel any matching live order manually."
                ),
                "placement_error": _safe_exception_metadata(exc),
                "audit": placement_audit,
            }
        )
        return plan
    order_id = extract_order_id(placed)
    audit: Dict[str, Any] = {"placed": _placed_audit_payload(placed), "order_id": order_id}
    plan.update(
        {
            "live_action": True,
            "audit": audit,
            "manual_reconciliation_required": True,
        }
    )
    try:
        recovery_writer(
            {
                **recovery_base,
                "stage": "order_placed_reconcile_required",
                "order_id": order_id,
                "manual_reconciliation_required": True,
                "resolved": False,
            }
        )
    except Exception as exc:
        audit["recovery_journal_error"] = _safe_exception_metadata(exc)
    if not order_id:
        plan.update(
            {
                "status": "failed",
                "failure": "Order placement response did not include an order id; manual account review is required.",
            }
        )
        return plan

    try:
        cancelled = trader.cancel_order(order_id)
    except Exception as exc:
        audit["cancel_error"] = _safe_exception_metadata(exc)
        plan.update(
            {
                "status": "failed",
                "failure": (
                    "Order placement succeeded, but the cancel request failed. "
                    "Use audit.order_id to reconcile and cancel the order manually."
                ),
            }
        )
        _try_recovery_write(
            recovery_writer,
            {
                **recovery_base,
                "stage": "cancel_failed",
                "order_id": order_id,
                "error_type": type(exc).__name__,
                "manual_reconciliation_required": True,
                "resolved": False,
            },
            audit,
        )
        return plan

    audit["cancel"] = _cancel_audit_payload(cancelled, order_id)
    try:
        post_cancel = trader.get_order(order_id)
    except Exception as exc:
        audit["post_cancel_error"] = _safe_exception_metadata(exc)
        plan.update(
            {
                "status": "failed",
                "failure": (
                    "The cancel request returned, but the post-cancel order read failed. "
                    "Use audit.order_id to reconcile the order manually."
                ),
            }
        )
        _try_recovery_write(
            recovery_writer,
            {
                **recovery_base,
                "stage": "post_cancel_read_failed",
                "order_id": order_id,
                "error_type": type(exc).__name__,
                "manual_reconciliation_required": True,
                "resolved": False,
            },
            audit,
        )
        return plan

    cancel_acknowledged = cancel_response_contains(cancelled, order_id)
    explicit_cancel_state = order_state_is_cancelled(post_cancel, order_id)
    zero_fill = order_zero_fill_evidence(post_cancel, order_id)
    cancel_verified = cancel_acknowledged and explicit_cancel_state and zero_fill["verified"]
    audit.update(
        {
            "post_cancel_order": _post_cancel_audit_payload(
                post_cancel,
                order_id,
                explicit_cancel_state=explicit_cancel_state,
                zero_fill=zero_fill,
            ),
            "cancel_acknowledged_for_order": cancel_acknowledged,
            "explicit_cancel_state": explicit_cancel_state,
            "zero_fill_evidence": zero_fill,
            "post_cancel_verified": cancel_verified,
        }
    )
    plan.update(
        {
            "status": "ok" if cancel_verified else "failed",
            "manual_reconciliation_required": not cancel_verified,
        }
    )
    if not cancel_verified:
        plan["failure"] = (
            "Order cancel was submitted, but an order-id-specific acknowledgment and explicit canceled state "
            "were not both proven. Reconcile the order manually."
        )
    recovery_written = _try_recovery_write(
        recovery_writer,
        {
            **recovery_base,
            "stage": "cancel_verified" if cancel_verified else "cancel_incomplete",
            "order_id": order_id,
            "manual_reconciliation_required": not cancel_verified,
            "resolved": cancel_verified,
            "zero_fill_verified": bool(zero_fill["verified"]),
        },
        audit,
    )
    audit["recovery_journal"] = {
        "status": "resolved" if recovery_written and cancel_verified else "unresolved",
        "stage": "cancel_verified" if cancel_verified else "cancel_incomplete",
        "resolved": bool(recovery_written and cancel_verified),
    }
    if cancel_verified and not recovery_written:
        plan.update(
            {
                "status": "failed",
                "manual_reconciliation_required": True,
                "failure": (
                    "The order cancel and zero-fill state were verified, but the durable recovery journal "
                    "could not be marked resolved. Reconcile the journal manually."
                ),
            }
        )
    return plan


def build_live_validation_stage_gates(report: Mapping[str, Any]) -> Dict[str, Any]:
    public_status = _section_status(report.get("public_checks"))
    authenticated_status = _section_status(report.get("authenticated_read_checks"))
    bridge_status = _section_status(report.get("bridge_address_checks"))
    readiness = report.get("clob_auth_readiness") if isinstance(report.get("clob_auth_readiness"), Mapping) else {}
    funded = report.get("funded_live_order_check") if isinstance(report.get("funded_live_order_check"), Mapping) else {}
    funded_status = str(funded.get("status") or "unknown")
    accepted_read_checks = accepted_credential_read_checks(report.get("authenticated_read_checks"))
    credentialed_read_ok = bool(accepted_read_checks)
    credential_readiness_ok = bool(readiness.get("ok"))
    safe_to_attempt_funded_order = (
        POLYMARKET_BOUNDED_AUDIT_MUTATIONS_SUPPORTED
        and
        public_status == "ok"
        and credential_readiness_ok
        and credentialed_read_ok
        and funded_status == "ready_to_execute"
        and bool(funded.get("live_action"))
    )
    return {
        "public_live_checks": public_status,
        "credential_readiness": "ok" if credential_readiness_ok else "blocked",
        "credentialed_read_checks": authenticated_status,
        "accepted_credential_read_checks": accepted_read_checks,
        "bridge_address_checks": bridge_status,
        "funded_live_order_check": funded_status,
        "credentialed_read_ok": credentialed_read_ok,
        "safe_to_attempt_funded_order": safe_to_attempt_funded_order,
        "requires_explicit_live_approval": True,
        "next_step": _next_live_validation_step(
            public_status=public_status,
            credential_readiness_ok=credential_readiness_ok,
            credentialed_read_ok=credentialed_read_ok,
            funded_status=funded_status,
        ),
    }


def _section_status(section: Any) -> str:
    if not isinstance(section, Mapping) or not section:
        return "skipped"
    statuses = [str(item.get("status") or "unknown") for item in section.values() if isinstance(item, Mapping)]
    if not statuses:
        return "skipped"
    if any(status == "failed" for status in statuses):
        return "failed"
    ok_count = statuses.count("ok")
    blocked_count = statuses.count("blocked")
    skipped_count = statuses.count("skipped")
    if ok_count and not blocked_count and not skipped_count:
        return "ok"
    if ok_count:
        return "partial"
    if blocked_count:
        return "blocked"
    if skipped_count == len(statuses):
        return "skipped"
    return "unknown"


def accepted_credential_read_checks(section: Any) -> list[str]:
    """Return successful non-mutating reads accepted by the promotion contract."""

    if not isinstance(section, Mapping):
        return []
    return [
        name
        for name in CREDENTIAL_PROMOTION_CHECKS
        if _credential_read_evidence_valid(name, section.get(name))
    ]


def _credential_read_evidence_valid(name: str, value: Any) -> bool:
    if not isinstance(value, Mapping) or value.get("status") != "ok":
        return False
    records_observed = value.get("records_observed")
    return bool(
        value.get("semantic_check") == CREDENTIAL_PROMOTION_SEMANTICS.get(name)
        and isinstance(records_observed, int)
        and not isinstance(records_observed, bool)
        and records_observed >= 0
    )


def has_accepted_credential_read(section: Any) -> bool:
    return bool(accepted_credential_read_checks(section))


def _next_live_validation_step(
    *,
    public_status: str,
    credential_readiness_ok: bool,
    credentialed_read_ok: bool,
    funded_status: str,
) -> str:
    if public_status == "failed":
        return "Fix public Polymarket connectivity before using credentials or live-order checks."
    if not credential_readiness_ok:
        return "Provide valid local CLOB trading credentials or explicit signed L2 headers, then rerun readiness."
    if not credentialed_read_ok:
        return "Run at least one non-destructive authenticated read check before any funded verification."
    if not POLYMARKET_BOUNDED_AUDIT_MUTATIONS_SUPPORTED:
        return "Keep the bounded CLOB V2 funded audit disabled until durable recovery is wired and reviewed."
    if funded_status in {"blocked", "skipped"}:
        return "Keep Polymarket mutation paths disabled until the CLOB V2 client/signing migration is reviewed."
    if funded_status == "dry_run":
        return "Review the dry-run transcript; real order/cancel still requires explicit funded flags and confirmation."
    if funded_status == "ready_to_execute":
        return "All local gates are ready; execute only if the operator explicitly approves the funded live check."
    if funded_status == "ok":
        return "Funded order/cancel verification completed and post-cancel verification passed."
    return "Review the report before taking any live action."


def maker_price_blocker(
    *,
    side: str,
    price: float,
    best_bid: Optional[float],
    best_ask: Optional[float],
    buffer: float = DEFAULT_MAKER_PRICE_BUFFER,
) -> str:
    side = str(side or "").upper()
    if side == "BUY":
        if best_ask is None:
            return "Cannot prove BUY order is maker-side because best ask is unavailable."
        if price >= best_ask - buffer:
            return f"BUY price {price:g} is too close to/takes best ask {best_ask:g}; lower price or increase safety buffer."
    elif side == "SELL":
        if best_bid is None:
            return "Cannot prove SELL order is maker-side because best bid is unavailable."
        if price <= best_bid + buffer:
            return f"SELL price {price:g} is too close to/takes best bid {best_bid:g}; raise price or increase safety buffer."
    else:
        return "Side must be BUY or SELL."
    return ""


def extract_order_id(payload: Any) -> str:
    if not isinstance(payload, Mapping):
        return ""
    for key in ("orderID", "orderId", "order_id", "id"):
        value = payload.get(key)
        if value:
            candidate = str(value).strip()
            # Current docs show 20-byte hex identifiers while production order
            # hashes may be 32 bytes. Admit only those two canonical forms.
            if re.fullmatch(r"0x(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", candidate):
                return "0x" + candidate[2:].lower()
    return ""


def cancel_response_contains(payload: Any, order_id: str) -> bool:
    if not isinstance(payload, Mapping):
        return False
    canceled = payload.get("canceled")
    return bool(
        order_id
        and isinstance(canceled, list)
        and str(order_id) in {str(item) for item in canceled}
    )


def order_state_is_cancelled(payload: Any, order_id: str = "") -> bool:
    if not isinstance(payload, Mapping):
        return False
    if order_id and extract_order_id(payload) != str(order_id):
        return False
    status = str(payload.get("status") or payload.get("orderStatus") or "").strip().upper()
    normalized = status.replace("-", "_").replace(" ", "_")
    return normalized in {
        "CANCELED",
        "CANCELLED",
        "ORDER_STATUS_CANCELED",
        "ORDER_STATUS_CANCELLED",
    }


def order_zero_fill_evidence(payload: Any, order_id: str = "") -> Dict[str, bool]:
    """Require explicit zero matched size and an empty associated-trade list."""

    result = {
        "verified": False,
        "order_identity_matches": False,
        "size_matched_zero": False,
        "associated_trades_empty": False,
    }
    if not isinstance(payload, Mapping):
        return result
    identity_matches = bool(order_id and extract_order_id(payload) == str(order_id))
    size_values = [payload[key] for key in ("size_matched", "sizeMatched") if key in payload]
    trade_values = [
        payload[key]
        for key in ("associate_trades", "associated_trades", "associateTrades")
        if key in payload
    ]
    if len(size_values) != 1 or len(trade_values) != 1:
        return {**result, "order_identity_matches": identity_matches}
    try:
        matched = float(str(size_values[0]))
    except (TypeError, ValueError):
        return {**result, "order_identity_matches": identity_matches}
    size_zero = math.isfinite(matched) and matched == 0
    trades_empty = isinstance(trade_values[0], list) and not trade_values[0]
    return {
        "verified": identity_matches and size_zero and trades_empty,
        "order_identity_matches": identity_matches,
        "size_matched_zero": size_zero,
        "associated_trades_empty": trades_empty,
    }


def _safe_exception_metadata(exc: Exception) -> Dict[str, str]:
    """Describe a venue failure without copying potentially secret-bearing text."""

    return {"type": type(exc).__name__}


def _placed_audit_payload(payload: Any) -> Dict[str, Any]:
    order_id = extract_order_id(payload)
    return {
        "orderID": order_id,
        "order_id_present": bool(order_id),
        "response_received": isinstance(payload, Mapping),
    }


def _cancel_audit_payload(payload: Any, order_id: str) -> Dict[str, Any]:
    acknowledged = cancel_response_contains(payload, order_id)
    return {
        "canceled": [order_id] if acknowledged else [],
        "order_acknowledged": acknowledged,
        "response_received": isinstance(payload, Mapping),
    }


def _post_cancel_audit_payload(
    payload: Any,
    order_id: str,
    *,
    explicit_cancel_state: bool,
    zero_fill: Mapping[str, bool],
) -> Dict[str, Any]:
    observed_order_id = extract_order_id(payload)
    return {
        "id": observed_order_id,
        "status": "ORDER_STATUS_CANCELED" if explicit_cancel_state else "UNVERIFIED",
        "size_matched": "0" if zero_fill.get("size_matched_zero") is True else "NONZERO_OR_UNVERIFIED",
        "associate_trades": [] if zero_fill.get("associated_trades_empty") is True else [{"present": True}],
        "order_identity_matches": bool(observed_order_id and observed_order_id == order_id),
        "response_received": isinstance(payload, Mapping),
    }


def _positive_float(value: Any, label: str) -> tuple[Optional[float], str]:
    number, error = _finite_float(value, label)
    if error:
        return None, error
    if number is None or number <= 0:
        return None, f"{label.capitalize()} must be greater than 0."
    return number, ""


def _non_negative_float(value: Any, label: str) -> tuple[Optional[float], str]:
    number, error = _finite_float(value, label)
    if error:
        return None, error
    if number is None or number < 0:
        return None, f"{label.capitalize()} must be greater than or equal to 0."
    return number, ""


def _finite_float(value: Any, label: str) -> tuple[Optional[float], str]:
    if value in (None, ""):
        return None, f"Missing {label}."
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None, f"{label.capitalize()} must be numeric."
    if not math.isfinite(number):
        return None, f"{label.capitalize()} must be finite."
    return number, ""
