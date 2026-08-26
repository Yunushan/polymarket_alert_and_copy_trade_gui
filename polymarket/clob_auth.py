from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Optional

from .endpoints import CLOB_ENDPOINTS
from .http_client import request_json
from .constants import POLYMARKET_LIVE_MUTATION_BLOCKER


REQUIRED_L2_HEADERS = ("POLY_ADDRESS", "POLY_API_KEY", "POLY_PASSPHRASE", "POLY_SIGNATURE", "POLY_TIMESTAMP")


def _live_mutation_unsupported() -> None:
    raise RuntimeError(POLYMARKET_LIVE_MUTATION_BLOCKER)


def _l2_headers(headers: Mapping[str, str]) -> Dict[str, str]:
    clean = {str(key): str(value) for key, value in headers.items() if value}
    missing = [name for name in REQUIRED_L2_HEADERS if name not in clean]
    if missing:
        raise ValueError(f"Polymarket CLOB L2 request headers missing: {', '.join(missing)}")
    return clean


def _request_json(
    endpoint_name: str,
    *,
    headers: Mapping[str, str],
    params: Optional[Mapping[str, Any]] = None,
    payload: Optional[Any] = None,
    timeout: float = 15.0,
) -> Any:
    endpoint = CLOB_ENDPOINTS[endpoint_name]
    return request_json(
        endpoint,
        params=params,
        payload=payload,
        headers=_l2_headers(headers),
        timeout=timeout,
    )


def post_order(order_payload: Mapping[str, Any], headers: Mapping[str, str], *, timeout: float = 15.0) -> Dict[str, Any]:
    _live_mutation_unsupported()


def post_orders(order_payloads: Iterable[Mapping[str, Any]], headers: Mapping[str, str], *, timeout: float = 15.0) -> Any:
    _live_mutation_unsupported()


def cancel_order(order_id: str, headers: Mapping[str, str], *, timeout: float = 15.0) -> Dict[str, Any]:
    _live_mutation_unsupported()


def get_order(order_id: str, headers: Mapping[str, str], *, timeout: float = 15.0) -> Dict[str, Any]:
    data = request_json(
        CLOB_ENDPOINTS["get_order"],
        path=f"/order/{order_id}",
        headers=_l2_headers(headers),
        timeout=timeout,
    )
    return data if isinstance(data, dict) else {}


def get_orders(
    headers: Mapping[str, str],
    *,
    order_id: Optional[str] = None,
    market: Optional[str] = None,
    asset_id: Optional[str] = None,
    next_cursor: Optional[str] = None,
    timeout: float = 15.0,
) -> Dict[str, Any]:
    data = _request_json(
        "get_orders",
        params={"id": order_id, "market": market, "asset_id": asset_id, "next_cursor": next_cursor},
        headers=headers,
        timeout=timeout,
    )
    return data if isinstance(data, dict) else {}


def cancel_orders(order_ids: Iterable[str], headers: Mapping[str, str], *, timeout: float = 15.0) -> Dict[str, Any]:
    _live_mutation_unsupported()


def cancel_all_orders(headers: Mapping[str, str], *, timeout: float = 15.0) -> Dict[str, Any]:
    _live_mutation_unsupported()


def cancel_market_orders(
    market: str,
    asset_id: str,
    headers: Mapping[str, str],
    *,
    timeout: float = 15.0,
) -> Dict[str, Any]:
    _live_mutation_unsupported()


def get_trades(headers: Mapping[str, str], *, timeout: float = 15.0, **filters: Any) -> Dict[str, Any]:
    data = _request_json("trades", params=filters, headers=headers, timeout=timeout)
    return data if isinstance(data, dict) else {}


def get_order_scoring_status(order_id: str, headers: Mapping[str, str], *, timeout: float = 15.0) -> Dict[str, Any]:
    data = _request_json("order_scoring", params={"order_id": order_id}, headers=headers, timeout=timeout)
    return data if isinstance(data, dict) else {}


def send_heartbeat(headers: Mapping[str, str], *, timeout: float = 15.0) -> Dict[str, Any]:
    _live_mutation_unsupported()


def get_user_rewards(headers: Mapping[str, str], *, timeout: float = 15.0, **params: Any) -> Dict[str, Any]:
    data = _request_json("user_rewards", params=params, headers=headers, timeout=timeout)
    return data if isinstance(data, dict) else {}


def get_user_reward_total(headers: Mapping[str, str], *, timeout: float = 15.0, **params: Any) -> Any:
    return _request_json("user_reward_total", params=params, headers=headers, timeout=timeout)


def get_user_reward_percentages(headers: Mapping[str, str], *, timeout: float = 15.0, **params: Any) -> Dict[str, Any]:
    data = _request_json("user_reward_percentages", params=params, headers=headers, timeout=timeout)
    return data if isinstance(data, dict) else {}


def get_user_reward_markets(headers: Mapping[str, str], *, timeout: float = 15.0, **params: Any) -> Dict[str, Any]:
    data = _request_json("user_reward_markets", params=params, headers=headers, timeout=timeout)
    return data if isinstance(data, dict) else {}
