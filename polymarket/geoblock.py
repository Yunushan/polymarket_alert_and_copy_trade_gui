from __future__ import annotations

from typing import Any, Dict

from .endpoints import PolymarketEndpoint
from .http_client import RetryPolicy, request_json


_GEOBLOCK_ENDPOINT = PolymarketEndpoint(
    service="geoblock",
    method="GET",
    path="/api/geoblock",
    base_url="https://polymarket.com",
)
_NO_RETRY = RetryPolicy(max_attempts=1)


def check_geoblock(timeout: float = 10.0) -> Dict[str, Any]:
    """
    Polymarket geoblock endpoint:
    GET https://polymarket.com/api/geoblock
    Returns {blocked:boolean, ip:string, country:string, region:string}
    """
    data = request_json(_GEOBLOCK_ENDPOINT, timeout=timeout, retry_policy=_NO_RETRY)
    return data if isinstance(data, dict) else {}
