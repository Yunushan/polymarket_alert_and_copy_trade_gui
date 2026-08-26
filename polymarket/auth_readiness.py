from __future__ import annotations

import os
import re
from typing import Any, Dict, Mapping, Optional

from .clob_auth import REQUIRED_L2_HEADERS
from .constants import CLOB_API
from .http_client import PolymarketValidationError


POLYGON_CHAIN_ID = 137
PRIVATE_KEY_KEYS = ("private_key", "polymarket_private_key")
PRIVATE_KEY_ENV_VARS = ("PRIVATE_KEY", "POLYMARKET_PRIVATE_KEY")
FUNDER_KEYS = ("funder_address", "polymarket_funder_address", "deposit_wallet_address")
FUNDER_ENV_VARS = ("FUNDER_ADDRESS", "POLYMARKET_FUNDER_ADDRESS", "DEPOSIT_WALLET_ADDRESS")
SIGNATURE_TYPE_KEYS = ("signature_type", "polymarket_signature_type")
SIGNATURE_TYPE_ENV_VARS = ("SIGNATURE_TYPE", "POLYMARKET_SIGNATURE_TYPE")
L1_HEADER_NAMES = ("POLY_ADDRESS", "POLY_SIGNATURE", "POLY_TIMESTAMP", "POLY_NONCE")
HEX_PRIVATE_KEY_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

SIGNATURE_TYPE_INFO: Dict[int, Dict[str, Any]] = {
    0: {
        "name": "EOA",
        "description": "Standard Ethereum wallet. Funder is the EOA address and needs POL for gas.",
        "requires_funder": False,
    },
    1: {
        "name": "POLY_PROXY",
        "description": "Existing Polymarket proxy wallet flow.",
        "requires_funder": True,
    },
    2: {
        "name": "GNOSIS_SAFE",
        "description": "Existing Gnosis Safe wallet flow.",
        "requires_funder": True,
    },
    3: {
        "name": "POLY_1271",
        "description": "Deposit-wallet flow for new API users.",
        "requires_funder": True,
    },
}


def build_clob_auth_readiness(
    settings: Optional[Mapping[str, Any]] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    settings = dict(settings or {})
    env = environ if environ is not None else os.environ
    private_key = _first_config_or_env(settings, PRIVATE_KEY_KEYS, env, PRIVATE_KEY_ENV_VARS)
    funder = _first_config_or_env(settings, FUNDER_KEYS, env, FUNDER_ENV_VARS)
    signature_type_raw = _first_config_or_env(settings, SIGNATURE_TYPE_KEYS, env, SIGNATURE_TYPE_ENV_VARS)
    signature_type_value, signature_type_error = _parse_signature_type(signature_type_raw["value"])
    signature_info = SIGNATURE_TYPE_INFO.get(signature_type_value, {}) if signature_type_error is None else {}

    blockers = []
    warnings = []
    if not private_key["present"]:
        blockers.append("Missing private key for L1 API credential derivation and local order signing.")
    elif not is_private_key_like(private_key["value"]):
        blockers.append("Private key must be a 0x-prefixed 32-byte hex string.")

    if signature_type_error:
        blockers.append(signature_type_error)
    elif signature_type_value not in SIGNATURE_TYPE_INFO:
        blockers.append(f"Unsupported Polymarket signature type: {signature_type_value}.")

    if signature_info.get("requires_funder") and not funder["present"]:
        blockers.append(f"{signature_info['name']} signature type requires an explicit funder/deposit wallet address.")
    if funder["present"] and not is_evm_address_like(funder["value"]):
        blockers.append("Funder/deposit wallet address must be a 0x-prefixed EVM address.")
    if signature_type_value == 0 and not funder["present"]:
        warnings.append("EOA signature type selected without explicit funder; py-clob-client-v2 will use the signing wallet flow.")
    if signature_type_value == 3:
        warnings.append("POLY_1271 is the deposit-wallet flow; verify the funder matches the Polymarket deposit wallet.")

    l2_headers = _header_presence(env, REQUIRED_L2_HEADERS)
    direct_l2_read_ready = all(item["present"] for item in l2_headers.values())
    if not direct_l2_read_ready:
        warnings.append("Direct authenticated REST read checks need all five POLY_* L2 headers.")

    l1_headers = _header_presence(env, L1_HEADER_NAMES)
    l1_rest_ready = all(item["present"] for item in l1_headers.values())

    return {
        "ok": not blockers,
        "sdk_trading_ready": not blockers,
        "can_derive_or_create_api_key": private_key["present"] and is_private_key_like(private_key["value"]),
        "can_sign_orders": private_key["present"] and is_private_key_like(private_key["value"]),
        "direct_l2_read_ready": direct_l2_read_ready,
        "l1_rest_api_key_ready": l1_rest_ready,
        "host": CLOB_API,
        "chain_id": POLYGON_CHAIN_ID,
        "private_key": _redacted_secret_presence(private_key),
        "funder_address": _redacted_address_presence(funder),
        "signature_type": {
            "value": signature_type_value,
            "name": signature_info.get("name", "UNKNOWN"),
            "requires_funder": bool(signature_info.get("requires_funder", False)),
            "description": signature_info.get("description", ""),
            "source": signature_type_raw["source"] if signature_type_raw["present"] else "default:0",
        },
        "l2_headers": {
            "required": list(REQUIRED_L2_HEADERS),
            "missing": [name for name, item in l2_headers.items() if not item["present"]],
            "present": {name: item["present"] for name, item in l2_headers.items()},
            "sources": {name: item["source"] for name, item in l2_headers.items() if item["present"]},
        },
        "l1_headers": {
            "required": list(L1_HEADER_NAMES),
            "missing": [name for name, item in l1_headers.items() if not item["present"]],
            "present": {name: item["present"] for name, item in l1_headers.items()},
            "sources": {name: item["source"] for name, item in l1_headers.items() if item["present"]},
        },
        "blockers": blockers,
        "warnings": warnings,
        "docs": {
            "authentication": "https://docs.polymarket.com/api-reference/authentication",
            "clients_and_sdks": "https://docs.polymarket.com/api-reference/clients-and-sdks",
        },
    }


def validate_sdk_trading_readiness(
    *,
    private_key: str,
    signature_type: int,
    funder_address: Optional[str],
    chain_id: int = POLYGON_CHAIN_ID,
    host: str = CLOB_API,
) -> Dict[str, Any]:
    if int(chain_id) != POLYGON_CHAIN_ID:
        raise PolymarketValidationError(f"Polymarket CLOB trading expects Polygon chain id {POLYGON_CHAIN_ID}.")
    if str(host).rstrip("/") != CLOB_API:
        raise PolymarketValidationError("Polymarket CLOB trading host must be the official CLOB API.")
    settings = {
        "private_key": private_key,
        "signature_type": signature_type,
        "funder_address": funder_address or "",
    }
    report = build_clob_auth_readiness(settings, environ={})
    if report["blockers"]:
        raise PolymarketValidationError("; ".join(report["blockers"]))
    return report


def parse_signature_type(value: Any) -> int:
    parsed, error = _parse_signature_type(value)
    if error is not None:
        raise PolymarketValidationError(error)
    if parsed not in SIGNATURE_TYPE_INFO:
        raise PolymarketValidationError(f"Unsupported Polymarket signature type: {parsed}.")
    return int(parsed)


def is_private_key_like(value: Any) -> bool:
    return bool(HEX_PRIVATE_KEY_RE.match(str(value or "").strip()))


def is_evm_address_like(value: Any) -> bool:
    return bool(EVM_ADDRESS_RE.match(str(value or "").strip()))


def redacted_address(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 12:
        return "***"
    return f"{text[:6]}...{text[-4:]}"


def _first_config_or_env(
    settings: Mapping[str, Any],
    keys: tuple[str, ...],
    env: Mapping[str, str],
    env_vars: tuple[str, ...],
) -> Dict[str, Any]:
    for key in keys:
        value = settings.get(key)
        if value not in (None, ""):
            return {"present": True, "value": str(value), "source": f"config:{key}"}
    for env_var in env_vars:
        value = env.get(env_var)
        if value:
            return {"present": True, "value": str(value), "source": f"env:{env_var}"}
    return {"present": False, "value": "", "source": ""}


def _parse_signature_type(value: Any) -> tuple[int, Optional[str]]:
    raw = "0" if value in (None, "") else str(value).strip()
    try:
        return int(raw), None
    except (TypeError, ValueError):
        return 0, "Polymarket SIGNATURE_TYPE must be an integer."


def _header_presence(env: Mapping[str, str], names: tuple[str, ...]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for name in names:
        value = env.get(name)
        out[name] = {"present": bool(value), "source": f"env:{name}" if value else ""}
    return out


def _redacted_secret_presence(item: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "present": bool(item.get("present")),
        "source": str(item.get("source") or ""),
        "redacted": "***" if item.get("present") else "",
    }


def _redacted_address_presence(item: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "present": bool(item.get("present")),
        "source": str(item.get("source") or ""),
        "redacted": redacted_address(item.get("value")) if item.get("present") else "",
    }
