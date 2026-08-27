from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from .endpoints import RELAYER_ENDPOINTS
from .http_client import request_json
from .constants import POLYMARKET_LIVE_MUTATION_BLOCKER


AUTH_HEADER_SETS = (
    ("POLY_BUILDER_API_KEY", "POLY_BUILDER_TIMESTAMP", "POLY_BUILDER_PASSPHRASE", "POLY_BUILDER_SIGNATURE"),
    ("RELAYER_API_KEY", "RELAYER_API_KEY_ADDRESS"),
)


def _auth_headers(headers: Optional[Mapping[str, str]]) -> Dict[str, str]:
    clean = {str(key): str(value) for key, value in (headers or {}).items() if value}
    if any(all(name in clean for name in header_set) for header_set in AUTH_HEADER_SETS):
        return clean
    raise ValueError(
        "Relayer authenticated request requires explicit Builder API headers "
        "or RELAYER_API_KEY plus RELAYER_API_KEY_ADDRESS."
    )


def _get_json(
    endpoint_name: str,
    *,
    params: Optional[Mapping[str, Any]] = None,
    headers: Optional[Mapping[str, str]] = None,
    auth_required: bool = False,
    timeout: float = 15.0,
) -> Any:
    request_headers = _auth_headers(headers) if auth_required else dict(headers or {})
    return request_json(RELAYER_ENDPOINTS[endpoint_name], params=params, headers=request_headers or None, timeout=timeout)


def _post_json(
    endpoint_name: str,
    payload: Mapping[str, Any],
    *,
    headers: Optional[Mapping[str, str]] = None,
    auth_required: bool = False,
    timeout: float = 15.0,
) -> Any:
    request_headers = _auth_headers(headers) if auth_required else dict(headers or {})
    return request_json(RELAYER_ENDPOINTS[endpoint_name], payload=dict(payload), headers=request_headers or None, timeout=timeout)


def submit_transaction(payload: Mapping[str, Any], headers: Mapping[str, str], *, timeout: float = 15.0) -> Dict[str, Any]:
    raise RuntimeError(POLYMARKET_LIVE_MUTATION_BLOCKER)


def get_transaction(transaction_id: str, *, timeout: float = 15.0) -> Any:
    return _get_json("transaction", params={"id": transaction_id}, timeout=timeout)


def get_recent_transactions(headers: Mapping[str, str], *, timeout: float = 15.0) -> List[Dict[str, Any]]:
    data = _get_json("transactions", headers=headers, auth_required=True, timeout=timeout)
    return data if isinstance(data, list) else []


def get_current_nonce(address: str, nonce_type: str, *, timeout: float = 15.0) -> Dict[str, Any]:
    data = _get_json("nonce", params={"address": address, "type": nonce_type.upper()}, timeout=timeout)
    return data if isinstance(data, dict) else {}


def get_relay_payload(address: str, nonce_type: str, *, timeout: float = 15.0) -> Dict[str, Any]:
    data = _get_json("relay_payload", params={"address": address, "type": nonce_type.upper()}, timeout=timeout)
    return data if isinstance(data, dict) else {}


def is_wallet_deployed(address: str, wallet_type: str = "SAFE", *, timeout: float = 15.0) -> Dict[str, Any]:
    data = _get_json("deployed", params={"address": address, "type": wallet_type.upper()}, timeout=timeout)
    return data if isinstance(data, dict) else {}


def get_all_api_keys(headers: Mapping[str, str], *, timeout: float = 15.0) -> List[Dict[str, Any]]:
    data = _get_json("api_keys", headers=headers, auth_required=True, timeout=timeout)
    return data if isinstance(data, list) else []
