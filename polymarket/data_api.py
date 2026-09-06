from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .endpoints import DATA_ENDPOINTS
from .http_client import PolymarketResponseError, comma_join, request_bytes, request_json
from .leaderboard import LEADERBOARD_MAX_OFFSET, normalize_leaderboard_category


def _get_json(endpoint_name: str, *, params: Optional[Mapping[str, Any]] = None, timeout: float = 15.0) -> Any:
    return request_json(DATA_ENDPOINTS[endpoint_name], params=params, timeout=timeout)


def _list_payload(data: Any, keys: List[str]) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _history_payload(data: Any, endpoint: str) -> List[Dict[str, Any]]:
    if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
        raise PolymarketResponseError(f"{endpoint} must return an array of history objects; completeness is unknown.")
    return data


def get_activity(
    user: str,
    *,
    limit: int = 50,
    offset: int = 0,
    types: Optional[List[str]] = None,
    side: Optional[str] = None,
    market: Optional[List[str]] = None,  # condition IDs
    start: Optional[int] = None,
    end: Optional[int] = None,
    sort_by: str = "TIMESTAMP",
    sort_direction: str = "DESC",
    timeout: float = 15.0,
) -> List[Dict[str, Any]]:
    """
    Data API: /activity
    Returns trades and activity history for a specified wallet.
    """
    clean_sort = str(sort_by or "TIMESTAMP").strip().upper()
    if clean_sort not in {"TIMESTAMP", "TOKENS", "CASH"}:
        clean_sort = "TIMESTAMP"
    clean_direction = str(sort_direction or "DESC").strip().upper()
    if clean_direction not in {"ASC", "DESC"}:
        clean_direction = "DESC"
    params: Dict[str, Any] = {
        "user": user,
        "limit": max(0, min(int(limit), 500)),
        "offset": max(0, min(int(offset), 10000)),
        "sortDirection": clean_direction,
        "sortBy": clean_sort,
    }
    if types:
        # The docs use enum list; requests will encode repeated params if list passed.
        params["type"] = types
    if side:
        params["side"] = side
    if market:
        params["market"] = market
    if start is not None:
        params["start"] = int(start)
    if end is not None:
        params["end"] = int(end)

    data = _get_json("activity", params=params, timeout=timeout)
    return _history_payload(data, "activity")


def get_positions(
    user: str,
    *,
    limit: int = 100,
    offset: int = 0,
    size_threshold: float = 1.0,
    include_archived: bool = False,
    timeout: float = 15.0,
) -> List[Dict[str, Any]]:
    if (isinstance(size_threshold, bool) or not isinstance(size_threshold, (int, float))
            or not math.isfinite(size_threshold) or size_threshold < 0):
        raise ValueError("Position size threshold must be finite and nonnegative.")
    if not isinstance(include_archived, bool):
        raise ValueError("Include archived positions must be a boolean.")
    params = {
        "user": user,
        "limit": max(0, min(int(limit), 500)),
        "offset": max(0, min(int(offset), 10000)),
        "sizeThreshold": size_threshold,
        "includeArchived": str(include_archived).lower(),
    }
    data = _get_json("positions", params=params, timeout=timeout)
    return _history_payload(data, "positions")


def get_closed_positions(
    user: str,
    *,
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "TIMESTAMP",
    sort_direction: str = "ASC",
    timeout: float = 15.0,
) -> List[Dict[str, Any]]:
    clean_sort = str(sort_by or "TIMESTAMP").strip().upper()
    if clean_sort not in {"REALIZEDPNL", "TITLE", "PRICE", "AVGPRICE", "TIMESTAMP"}:
        clean_sort = "TIMESTAMP"
    clean_direction = str(sort_direction or "ASC").strip().upper()
    if clean_direction not in {"ASC", "DESC"}:
        clean_direction = "ASC"
    params = {
        "user": user,
        "limit": max(0, min(int(limit), 50)),
        "offset": max(0, min(int(offset), 100000)),
        "sortBy": clean_sort,
        "sortDirection": clean_direction,
    }
    data = _get_json("closed_positions", params=params, timeout=timeout)
    return _history_payload(data, "closed_positions")


def get_trades(
    user: str,
    *,
    limit: int = 100,
    offset: int = 0,
    timeout: float = 15.0,
) -> List[Dict[str, Any]]:
    params = {
        "user": user,
        "limit": max(0, min(int(limit), 500)),
        "offset": max(0, min(int(offset), 10000)),
    }
    data = _get_json("trades", params=params, timeout=timeout)
    return _history_payload(data, "trades")


def get_leaderboard(
    *,
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "PNL",
    sort_direction: str = "DESC",
    period: str = "all",
    category: str = "OVERALL",
    timeout: float = 15.0,
) -> List[Dict[str, Any]]:
    """
    Data API: /v1/leaderboard
    Returns public trader leaderboard rows.
    """
    clean_offset = max(0, int(offset))
    if clean_offset > LEADERBOARD_MAX_OFFSET:
        raise ValueError(f"Public leaderboard offset must not exceed {LEADERBOARD_MAX_OFFSET}; this endpoint cannot enumerate all accounts.")
    clean_sort = str(sort_by or "PNL").strip().upper()
    if clean_sort not in {"PNL", "VOL"}:
        clean_sort = "PNL"
    clean_direction = str(sort_direction or "DESC").strip().upper()
    if clean_direction not in {"ASC", "DESC"}:
        clean_direction = "DESC"
    clean_period = str(period or "ALL").strip().upper()
    if clean_period not in {"DAY", "WEEK", "MONTH", "ALL"}:
        clean_period = "ALL"
    clean_category = normalize_leaderboard_category(category)
    params = {
        "limit": max(1, min(int(limit), 50)),
        "offset": clean_offset,
        "orderBy": clean_sort,
        "sortDirection": clean_direction,
        "timePeriod": clean_period,
        "category": clean_category,
    }
    return _list_payload(_get_json("leaderboard", params=params, timeout=timeout), ["data", "leaderboard", "users", "results"])


def get_total_value(
    user: str,
    *,
    market: Optional[Iterable[str]] = None,
    timeout: float = 15.0,
) -> List[Dict[str, Any]]:
    data = _get_json("value", params={"user": user, "market": comma_join(market)}, timeout=timeout)
    return data if isinstance(data, list) else []


def get_total_markets_traded(user: str, *, timeout: float = 15.0) -> Dict[str, Any]:
    data = _get_json("traded", params={"user": user}, timeout=timeout)
    return data if isinstance(data, dict) else {}


def get_market_positions(
    market: str,
    *,
    user: Optional[str] = None,
    status: str = "ALL",
    sort_by: str = "TOTAL_PNL",
    sort_direction: str = "DESC",
    limit: int = 50,
    offset: int = 0,
    timeout: float = 15.0,
) -> List[Dict[str, Any]]:
    data = _get_json(
        "market_positions",
        params={
            "market": market,
            "user": user,
            "status": status,
            "sortBy": sort_by,
            "sortDirection": sort_direction,
            "limit": max(0, min(int(limit), 500)),
            "offset": max(0, min(int(offset), 10000)),
        },
        timeout=timeout,
    )
    return data if isinstance(data, list) else []


def get_top_holders(
    markets: Iterable[str],
    *,
    limit: int = 20,
    min_balance: int = 1,
    timeout: float = 15.0,
) -> List[Dict[str, Any]]:
    data = _get_json(
        "holders",
        params={
            "market": comma_join(markets),
            "limit": max(0, min(int(limit), 20)),
            "minBalance": max(0, min(int(min_balance), 999999)),
        },
        timeout=timeout,
    )
    return data if isinstance(data, list) else []


def get_open_interest(markets: Optional[Iterable[str]] = None, *, timeout: float = 15.0) -> List[Dict[str, Any]]:
    data = _get_json("oi", params={"market": comma_join(markets)}, timeout=timeout)
    return data if isinstance(data, list) else []


def get_live_volume(event_id: int, *, timeout: float = 15.0) -> List[Dict[str, Any]]:
    data = _get_json("live_volume", params={"id": int(event_id)}, timeout=timeout)
    return data if isinstance(data, list) else []


def download_accounting_snapshot(user: str, *, timeout: float = 30.0) -> bytes:
    return request_bytes(DATA_ENDPOINTS["accounting_snapshot"], params={"user": user}, timeout=timeout)


def get_builder_leaderboard(
    *,
    time_period: str = "DAY",
    limit: int = 25,
    offset: int = 0,
    timeout: float = 15.0,
) -> List[Dict[str, Any]]:
    data = _get_json(
        "builder_leaderboard",
        params={
            "timePeriod": str(time_period or "DAY").upper(),
            "limit": max(0, min(int(limit), 50)),
            "offset": max(0, min(int(offset), 1000)),
        },
        timeout=timeout,
    )
    return data if isinstance(data, list) else []


def get_builder_volume(*, time_period: str = "DAY", timeout: float = 15.0) -> List[Dict[str, Any]]:
    data = _get_json("builder_volume", params={"timePeriod": str(time_period or "DAY").upper()}, timeout=timeout)
    return data if isinstance(data, list) else []
