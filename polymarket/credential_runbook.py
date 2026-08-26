from __future__ import annotations

import os
import time
from typing import Any, Dict, Mapping, Optional, Sequence

from .auth_readiness import (
    FUNDER_ENV_VARS,
    L1_HEADER_NAMES,
    PRIVATE_KEY_ENV_VARS,
    SIGNATURE_TYPE_ENV_VARS,
    build_clob_auth_readiness,
    redacted_address,
)
from .clob_auth import REQUIRED_L2_HEADERS
from .live_verification import CONFIRM_LIVE_ORDER_CANCEL
from .ws_user import build_user_subscription


RELAYER_HEADERS = ("RELAYER_API_KEY", "RELAYER_API_KEY_ADDRESS")
BUILDER_HEADERS = (
    "POLY_BUILDER_API_KEY",
    "POLY_BUILDER_TIMESTAMP",
    "POLY_BUILDER_PASSPHRASE",
    "POLY_BUILDER_SIGNATURE",
)
USER_WS_KEY = ("POLY_API_KEY",)
USER_WS_SECRET = ("POLY_API_SECRET", "POLY_SECRET")
USER_WS_PASSPHRASE = ("POLY_PASSPHRASE",)
SIGNED_HEADER_NAMES = {"POLY_SIGNATURE", "POLY_TIMESTAMP", "POLY_NONCE", "POLY_BUILDER_SIGNATURE", "POLY_BUILDER_TIMESTAMP"}


def build_polymarket_credential_runbook(
    settings: Optional[Mapping[str, Any]] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    env = environ if environ is not None else os.environ
    settings = dict(settings or {})
    clob_readiness = build_clob_auth_readiness(settings, environ=env)

    direct_l2_ready = bool(clob_readiness.get("direct_l2_read_ready"))
    l1_ready = bool(clob_readiness.get("l1_rest_api_key_ready"))
    sdk_ready = bool(clob_readiness.get("sdk_trading_ready"))
    relayer_ready = _all_present(RELAYER_HEADERS, env)
    builder_ready = _all_present(BUILDER_HEADERS, env)
    user_ws_status = _user_ws_status(env)
    user_ws_ready = user_ws_status["status"] == "ok"
    non_destructive_auth_ready = bool(direct_l2_ready or relayer_ready or user_ws_ready)

    runbook: Dict[str, Any] = {
        "generated_at": time.time(),
        "mode": "credential_runbook_no_funded_actions",
        "network_calls": "none",
        "funded_execution_exposed": False,
        "safe_to_attempt_funded_order": False,
        "env_inventory": {
            "sdk_trading_credentials": _sdk_group(clob_readiness, env),
            "direct_l2_read_headers": _all_of_group(
                "direct_l2_read_headers",
                "Direct CLOB L2 read headers",
                "Required for non-destructive authenticated CLOB reads such as order-list checks.",
                REQUIRED_L2_HEADERS,
                env,
                ready=direct_l2_ready,
                blocked_detail="Missing one or more explicit CLOB L2 headers.",
                ok_detail="All explicit CLOB L2 headers are present.",
            ),
            "l1_rest_headers": _all_of_group(
                "l1_rest_headers",
                "CLOB L1 REST headers",
                "Required for explicit L1-authenticated CLOB REST calls; this runbook never synthesizes signatures.",
                L1_HEADER_NAMES,
                env,
                ready=l1_ready,
                blocked_detail="Missing one or more CLOB L1 REST headers.",
                ok_detail="All CLOB L1 REST headers are present.",
            ),
            "user_websocket_auth": _user_ws_group(user_ws_status, env),
            "relayer_headers": _all_of_group(
                "relayer_headers",
                "Relayer API headers",
                "Required for non-destructive relayer recent-transaction/API-key reads.",
                RELAYER_HEADERS,
                env,
                ready=relayer_ready,
                blocked_detail="Missing relayer API key headers.",
                ok_detail="All relayer API key headers are present.",
            ),
            "builder_headers": _all_of_group(
                "builder_headers",
                "Builder API headers",
                "Required only for builder-specific authenticated endpoints; not needed for the default credentialed read gate.",
                BUILDER_HEADERS,
                env,
                ready=builder_ready,
                blocked_detail="Missing builder API headers.",
                ok_detail="All builder API headers are present.",
            ),
        },
        "readiness": {
            "sdk_trading_credentials": _status_item(
                "ok" if sdk_ready else "blocked",
                "SDK trading credentials are locally well-formed."
                if sdk_ready
                else "SDK trading credentials are not locally ready.",
                blockers=clob_readiness.get("blockers", []),
                warnings=clob_readiness.get("warnings", []),
            ),
            "direct_l2_read_headers": _status_item(
                "ok" if direct_l2_ready else "blocked",
                "Ready for a non-destructive CLOB L2 order-list read."
                if direct_l2_ready
                else "Missing explicit CLOB L2 headers for authenticated read checks.",
                missing=clob_readiness.get("l2_headers", {}).get("missing", []),
            ),
            "l1_rest_headers": _status_item(
                "ok" if l1_ready else "blocked",
                "Ready for explicit L1 REST-authenticated calls."
                if l1_ready
                else "Missing explicit CLOB L1 REST headers.",
                missing=clob_readiness.get("l1_headers", {}).get("missing", []),
            ),
            "user_websocket_auth_payload": user_ws_status,
            "relayer_headers": _status_item(
                "ok" if relayer_ready else "blocked",
                "Ready for a non-destructive relayer authenticated read."
                if relayer_ready
                else "Missing relayer API key headers.",
                missing=_missing(RELAYER_HEADERS, env),
            ),
            "builder_headers": _status_item(
                "ok" if builder_ready else "blocked",
                "Builder API headers are present."
                if builder_ready
                else "Builder API headers are not complete.",
                missing=_missing(BUILDER_HEADERS, env),
            ),
            "non_destructive_auth_ready": non_destructive_auth_ready,
            "credentialed_read_candidates": _credentialed_read_candidates(
                direct_l2_ready=direct_l2_ready,
                user_ws_ready=user_ws_ready,
                relayer_ready=relayer_ready,
            ),
            "clob_auth_readiness": clob_readiness,
        },
        "operator_commands": _operator_commands(),
        "safety_boundaries": [
            "This runbook performs no network calls.",
            "This runbook never derives API credentials.",
            "This runbook never signs, places, cancels, or submits funded actions.",
            "Use data/config.json for non-secret app settings only; keep credentials in .env, shell environment, OS keychain tooling, or approved secret files.",
            "Funded verification remains blocked until explicit live flags, token allow-list, hard caps, maker-side orderbook preflight, and exact confirmation text are supplied.",
        ],
        "next_steps": _next_steps(
            direct_l2_ready=direct_l2_ready,
            user_ws_ready=user_ws_ready,
            relayer_ready=relayer_ready,
            non_destructive_auth_ready=non_destructive_auth_ready,
            sdk_ready=sdk_ready,
        ),
    }
    return runbook


def _sdk_group(clob_readiness: Mapping[str, Any], env: Mapping[str, str]) -> Dict[str, Any]:
    signature_type = clob_readiness.get("signature_type") if isinstance(clob_readiness.get("signature_type"), Mapping) else {}
    requires_funder = bool(signature_type.get("requires_funder"))
    requirements = [
        _requirement(
            "private_key",
            "one_of",
            PRIVATE_KEY_ENV_VARS,
            env,
            purpose="Required for py-clob-client-v2 credential derivation and local order signing.",
        ),
        _requirement(
            "signature_type",
            "optional_default_0",
            SIGNATURE_TYPE_ENV_VARS,
            env,
            purpose="Optional signature type; defaults to EOA/0 when omitted.",
            ready=True,
        ),
        _requirement(
            "funder_or_deposit_wallet",
            "conditional_one_of" if requires_funder else "optional",
            FUNDER_ENV_VARS,
            env,
            purpose="Required for POLY_PROXY, GNOSIS_SAFE, and POLY_1271 signature flows.",
            ready=not requires_funder or _any_present(FUNDER_ENV_VARS, env),
        ),
    ]
    return {
        "id": "sdk_trading_credentials",
        "label": "SDK trading credentials",
        "purpose": "Local py-clob-client-v2 readiness for authenticated CLOB workflows.",
        "status": "ok" if clob_readiness.get("sdk_trading_ready") else "blocked",
        "requirements": requirements,
        "blockers": list(clob_readiness.get("blockers") or []),
        "warnings": list(clob_readiness.get("warnings") or []),
    }


def _all_of_group(
    group_id: str,
    label: str,
    purpose: str,
    names: Sequence[str],
    env: Mapping[str, str],
    *,
    ready: bool,
    blocked_detail: str,
    ok_detail: str,
) -> Dict[str, Any]:
    return {
        "id": group_id,
        "label": label,
        "purpose": purpose,
        "status": "ok" if ready else "blocked",
        "detail": ok_detail if ready else blocked_detail,
        "requirements": [_requirement(group_id, "all_of", names, env, purpose=purpose, ready=ready)],
    }


def _user_ws_group(status: Mapping[str, Any], env: Mapping[str, str]) -> Dict[str, Any]:
    return {
        "id": "user_websocket_auth",
        "label": "User WebSocket authentication",
        "purpose": "Required for the authenticated user WebSocket subscription check.",
        "status": status.get("status", "blocked"),
        "detail": status.get("detail", ""),
        "requirements": [
            _requirement("api_key", "all_of", USER_WS_KEY, env, purpose="User WebSocket API key."),
            _requirement("api_secret", "one_of", USER_WS_SECRET, env, purpose="User WebSocket API secret."),
            _requirement("passphrase", "all_of", USER_WS_PASSPHRASE, env, purpose="User WebSocket passphrase."),
        ],
    }


def _requirement(
    requirement_id: str,
    mode: str,
    names: Sequence[str],
    env: Mapping[str, str],
    *,
    purpose: str,
    ready: Optional[bool] = None,
) -> Dict[str, Any]:
    variables = [_variable_entry(name, env) for name in names]
    present = [item["name"] for item in variables if item["present"]]
    if ready is None:
        ready = bool(present) if mode in {"one_of", "conditional_one_of"} else len(present) == len(names)
    optional_modes = {"one_of", "conditional_one_of", "optional", "optional_default_0"}
    missing = [] if ready and mode in optional_modes else [item["name"] for item in variables if not item["present"]]
    return {
        "id": requirement_id,
        "mode": mode,
        "purpose": purpose,
        "ready": bool(ready),
        "present": present,
        "missing": missing,
        "variables": variables,
    }


def _variable_entry(name: str, env: Mapping[str, str]) -> Dict[str, Any]:
    value = env.get(name, "")
    present = bool(value)
    return {
        "name": name,
        "present": present,
        "source": f"env:{name}" if present else "",
        "classification": _classification(name),
        "redacted": _redact_env_value(name, value) if present else "",
    }


def _classification(name: str) -> str:
    if name in SIGNATURE_TYPE_ENV_VARS:
        return "setting"
    normalized = name.lower()
    if "address" in normalized or name in {"POLY_ADDRESS"}:
        return "address"
    if name in SIGNED_HEADER_NAMES:
        return "signed_header"
    if "key" in normalized or "secret" in normalized or "passphrase" in normalized or "signature" in normalized:
        return "secret"
    if "timestamp" in normalized or "nonce" in normalized:
        return "signed_header"
    return "setting"


def _redact_env_value(name: str, value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    if _classification(name) == "address":
        return redacted_address(text)
    if name in SIGNATURE_TYPE_ENV_VARS:
        return text
    return "***"


def _user_ws_status(env: Mapping[str, str]) -> Dict[str, Any]:
    auth = {
        "apiKey": env.get("POLY_API_KEY", ""),
        "secret": env.get("POLY_API_SECRET") or env.get("POLY_SECRET", ""),
        "passphrase": env.get("POLY_PASSPHRASE", ""),
    }
    try:
        build_user_subscription(auth)
    except ValueError as exc:
        return _status_item("blocked", str(exc), missing=_user_ws_missing(env))
    return _status_item("ok", "User WebSocket auth payload can be built.")


def _user_ws_missing(env: Mapping[str, str]) -> list[str]:
    missing = []
    if not env.get("POLY_API_KEY"):
        missing.append("POLY_API_KEY")
    if not (env.get("POLY_API_SECRET") or env.get("POLY_SECRET")):
        missing.append("POLY_API_SECRET or POLY_SECRET")
    if not env.get("POLY_PASSPHRASE"):
        missing.append("POLY_PASSPHRASE")
    return missing


def _credentialed_read_candidates(*, direct_l2_ready: bool, user_ws_ready: bool, relayer_ready: bool) -> list[str]:
    candidates = []
    if direct_l2_ready:
        candidates.append("clob_l2_orders")
    if relayer_ready:
        candidates.append("relayer_recent_transactions")
    return candidates


def _operator_commands() -> Dict[str, str]:
    return {
        "credential_inventory": "python scripts/verify_polymarket_credentials.py --json --report-file polymarket-credential-runbook.json",
        "public_readiness_report": "python scripts/verify_polymarket_live.py --report-file live-report.json",
        "credentialed_read_no_funded_actions": "python scripts/verify_polymarket_live.py --require-authenticated-read-ok --include-user-websocket-connect --report-file live-auth-report.json",
        "credentialed_read_without_websocket": "python scripts/verify_polymarket_live.py --require-authenticated-read-ok --report-file live-auth-report.json",
        "dry_run_order_cancel_no_funded_actions": "python scripts/verify_polymarket_live.py --token-id <TOKEN> --side BUY --price <PRICE> --size <SIZE> --allow-token-id <TOKEN> --report-file live-dry-run-report.json",
        "funded_order_cancel_requires_approval": (
            "python scripts/verify_polymarket_live.py --token-id <TOKEN> --side BUY --price <PRICE> --size <SIZE> "
            "--allow-token-id <TOKEN> --cancel-immediately --allow-funded-order "
            "--recovery-journal <ABSOLUTE_PRIVATE_JOURNAL_PATH> "
            f"--confirm-live-order-cancel {CONFIRM_LIVE_ORDER_CANCEL} --report-file live-funded-report.json"
        ),
    }


def _next_steps(
    *,
    direct_l2_ready: bool,
    user_ws_ready: bool,
    relayer_ready: bool,
    non_destructive_auth_ready: bool,
    sdk_ready: bool,
) -> list[str]:
    steps = []
    if not direct_l2_ready:
        steps.append("Add explicit POLY_* L2 headers before running CLOB order-list authenticated reads.")
    if not user_ws_ready:
        steps.append("Add POLY_API_KEY, POLY_API_SECRET or POLY_SECRET, and POLY_PASSPHRASE before probing the user WebSocket.")
    if not relayer_ready:
        steps.append("Add RELAYER_API_KEY and RELAYER_API_KEY_ADDRESS only if relayer authenticated reads are required.")
    if not sdk_ready:
        steps.append("Fix private key, signature type, and funder/deposit-wallet readiness before any dry-run order/cancel transcript can become executable.")
    if not non_destructive_auth_ready:
        steps.append("Do not attempt funded verification; first make at least one non-destructive authenticated read or stream check ready.")
    else:
        steps.append("Run the credentialed-read CLI command and save the JSON report for GUI import/audit history.")
    steps.append("Keep funded verification behind explicit live-action approval, token allow-list, hard caps, and exact confirmation text.")
    return steps


def _status_item(status: str, detail: str, **extra: Any) -> Dict[str, Any]:
    item: Dict[str, Any] = {"status": status, "detail": detail}
    item.update(extra)
    return item


def _all_present(names: Sequence[str], env: Mapping[str, str]) -> bool:
    return all(bool(env.get(name)) for name in names)


def _any_present(names: Sequence[str], env: Mapping[str, str]) -> bool:
    return any(bool(env.get(name)) for name in names)


def _missing(names: Sequence[str], env: Mapping[str, str]) -> list[str]:
    return [name for name in names if not env.get(name)]
