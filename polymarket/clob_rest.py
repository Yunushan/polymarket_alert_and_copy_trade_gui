from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .endpoints import CLOB_ENDPOINTS
from .http_client import (
    PolymarketError,
    as_dict,
    build_batch,
    comma_join,
    compact_params,
    optional_price,
    request_json,
)


def _get_json(endpoint_name: str, *, path: Optional[str] = None, params: Optional[Mapping[str, Any]] = None, timeout: float = 10.0) -> Any:
    return request_json(CLOB_ENDPOINTS[endpoint_name], path=path, params=params, timeout=timeout)


def _post_json(endpoint_name: str, payload: Any, *, timeout: float = 10.0) -> Any:
    return request_json(CLOB_ENDPOINTS[endpoint_name], payload=payload, timeout=timeout)


def get_book(token_id: str, timeout: float = 10.0) -> Dict[str, Any]:
    return as_dict(_get_json("book", params={"token_id": token_id}, timeout=timeout), endpoint_name="clob.book")


def get_books(token_ids: Iterable[str], timeout: float = 10.0) -> List[Dict[str, Any]]:
    payload = [{"token_id": str(token_id)} for token_id in build_batch(token_ids, max_items=None, name="clob.books")]
    data = _post_json("books", payload, timeout=timeout)
    return data if isinstance(data, list) else []


def best_bid_ask_from_book(book: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    # Docs/examples sometimes use bids/asks; elsewhere buys/sells.
    bids = book.get("bids") or book.get("buys") or []
    asks = book.get("asks") or book.get("sells") or []
    bid_prices = _finite_book_prices(bids)
    ask_prices = _finite_book_prices(asks)
    # The current API documents descending bids and ascending asks, but choose
    # the extrema defensively so a malformed/reordered response cannot weaken a
    # maker-price guard.
    best_bid = max(bid_prices, default=None)
    best_ask = min(ask_prices, default=None)
    return best_bid, best_ask


def _finite_book_prices(levels: Any) -> List[float]:
    if not isinstance(levels, list):
        return []
    prices: List[float] = []
    for level in levels:
        if not isinstance(level, Mapping):
            continue
        try:
            price = float(level.get("price"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(price) and 0 < price < 1:
            prices.append(price)
    return prices


def get_midpoint(token_id: str, timeout: float = 10.0) -> Optional[float]:
    try:
        data = _get_json("midpoint", params={"token_id": token_id}, timeout=timeout)
    except PolymarketError:
        return None
    return optional_price(data, ("mid", "midpoint", "price"))


def get_midpoints(token_ids: Iterable[str], timeout: float = 10.0) -> Dict[str, Any]:
    return as_dict(_get_json("midpoints", params={"token_ids": comma_join(token_ids)}, timeout=timeout), endpoint_name="clob.midpoints")


def get_midpoints_body(token_ids: Iterable[str], timeout: float = 10.0) -> Dict[str, Any]:
    payload = [{"token_id": str(token_id)} for token_id in build_batch(token_ids, max_items=None, name="clob.midpoints")]
    data = _post_json("midpoints_body", payload, timeout=timeout)
    return data if isinstance(data, dict) else {}


def get_price(token_id: str, side: str, timeout: float = 10.0) -> Optional[float]:
    data = _get_json("price", params={"token_id": token_id, "side": side.upper()}, timeout=timeout)
    return optional_price(data, ("price",))


def get_prices(token_ids: Iterable[str], sides: Iterable[str], timeout: float = 10.0) -> Dict[str, Any]:
    return as_dict(
        _get_json(
            "prices",
            params={"token_ids": comma_join(token_ids), "sides": comma_join(side.upper() for side in sides)},
            timeout=timeout,
        ),
        endpoint_name="clob.prices",
    )


def get_prices_body(requests_payload: Iterable[Mapping[str, Any]], timeout: float = 10.0) -> Dict[str, Any]:
    payload = [
        {"token_id": str(item["token_id"]), "side": str(item.get("side", "")).upper()}
        for item in requests_payload
        if item.get("token_id")
    ]
    build_batch(payload, max_items=None, name="clob.prices")
    data = _post_json("prices_body", payload, timeout=timeout)
    return data if isinstance(data, dict) else {}


def get_spread(token_id: str, timeout: float = 10.0) -> Optional[float]:
    data = _get_json("spread", params={"token_id": token_id}, timeout=timeout)
    return optional_price(data, ("spread", "price"))


def get_spreads(token_ids: Iterable[str], timeout: float = 10.0) -> Dict[str, Any]:
    payload = [{"token_id": str(token_id)} for token_id in build_batch(token_ids, max_items=None, name="clob.spreads")]
    data = _post_json("spreads", payload, timeout=timeout)
    return data if isinstance(data, dict) else {}


def get_last_trade_price(token_id: str, timeout: float = 10.0) -> Optional[float]:
    try:
        data = _get_json("last_trade_price", params={"token_id": token_id}, timeout=timeout)
    except PolymarketError:
        return None
    return optional_price(data, ("last_trade_price", "lastTradePrice", "last", "price"))


def get_last_trade_prices(token_ids: Iterable[str], timeout: float = 10.0) -> List[Dict[str, Any]]:
    data = _get_json("last_trade_prices", params={"token_ids": comma_join(token_ids)}, timeout=timeout)
    return data if isinstance(data, list) else []


def get_last_trade_prices_body(token_ids: Iterable[str], timeout: float = 10.0) -> List[Dict[str, Any]]:
    payload = [{"token_id": str(token_id)} for token_id in build_batch(token_ids, max_items=None, name="clob.last-trades-prices")]
    data = _post_json("last_trade_prices_body", payload, timeout=timeout)
    return data if isinstance(data, list) else []


def get_price_history(
    market: str,
    *,
    start_ts: Optional[float] = None,
    end_ts: Optional[float] = None,
    interval: Optional[str] = None,
    fidelity: Optional[int] = None,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    return as_dict(
        _get_json(
        "prices_history",
        params={
            "market": market,
            "startTs": start_ts,
            "endTs": end_ts,
            "interval": interval,
            "fidelity": fidelity,
        },
        timeout=timeout,
        ),
        endpoint_name="clob.prices-history",
    )


def get_batch_price_history(
    markets: Iterable[str],
    *,
    start_ts: Optional[float] = None,
    end_ts: Optional[float] = None,
    interval: Optional[str] = None,
    fidelity: Optional[int] = None,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    cleaned_markets = [str(market) for market in build_batch(markets, max_items=CLOB_ENDPOINTS["batch_prices_history"].max_items, name="clob.batch-prices-history")]
    payload = {
        "markets": cleaned_markets,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "interval": interval,
        "fidelity": fidelity,
    }
    data = _post_json("batch_prices_history", compact_params(payload), timeout=timeout)
    return data if isinstance(data, dict) else {}


def get_fee_rate(token_id: str, timeout: float = 10.0) -> Dict[str, Any]:
    return as_dict(_get_json("fee_rate", params={"token_id": token_id}, timeout=timeout), endpoint_name="clob.fee-rate")


def get_fee_rate_by_token(token_id: str, timeout: float = 10.0) -> Dict[str, Any]:
    return as_dict(_get_json("fee_rate_token", path=f"/fee-rate/{token_id}", timeout=timeout), endpoint_name="clob.fee-rate-token")


def get_tick_size(token_id: str, timeout: float = 10.0) -> Dict[str, Any]:
    return as_dict(_get_json("tick_size", params={"token_id": token_id}, timeout=timeout), endpoint_name="clob.tick-size")


def get_tick_size_by_token(token_id: str, timeout: float = 10.0) -> Dict[str, Any]:
    return as_dict(_get_json("tick_size_token", path=f"/tick-size/{token_id}", timeout=timeout), endpoint_name="clob.tick-size-token")


def get_clob_market_info(condition_id: str, timeout: float = 10.0) -> Dict[str, Any]:
    return as_dict(_get_json("clob_market", path=f"/clob-markets/{condition_id}", timeout=timeout), endpoint_name="clob.clob-market")


def get_market_by_token(token_id: str, timeout: float = 10.0) -> Dict[str, Any]:
    return as_dict(_get_json("market_by_token", path=f"/markets-by-token/{token_id}", timeout=timeout), endpoint_name="clob.market-by-token")


def get_server_time(timeout: float = 10.0) -> Dict[str, Any]:
    data = _get_json("time", timeout=timeout)
    if isinstance(data, dict):
        return data
    return {"time": data}


def list_simplified_markets(next_cursor: Optional[str] = None, timeout: float = 10.0) -> Dict[str, Any]:
    return as_dict(_get_json("simplified_markets", params={"next_cursor": next_cursor}, timeout=timeout), endpoint_name="clob.simplified-markets")


def list_sampling_markets(next_cursor: Optional[str] = None, timeout: float = 10.0) -> Dict[str, Any]:
    return as_dict(_get_json("sampling_markets", params={"next_cursor": next_cursor}, timeout=timeout), endpoint_name="clob.sampling-markets")


def list_sampling_simplified_markets(next_cursor: Optional[str] = None, timeout: float = 10.0) -> Dict[str, Any]:
    return as_dict(_get_json("sampling_simplified_markets", params={"next_cursor": next_cursor}, timeout=timeout), endpoint_name="clob.sampling-simplified-markets")


def get_current_rebated_fees(date: str, maker_address: str, timeout: float = 10.0) -> List[Dict[str, Any]]:
    data = _get_json("rebates_current", params={"date": date, "maker_address": maker_address}, timeout=timeout)
    return data if isinstance(data, list) else []


def get_current_rewards_config(
    *, sponsored: bool = False, next_cursor: Optional[str] = None, timeout: float = 10.0
) -> Dict[str, Any]:
    return as_dict(
        _get_json(
        "rewards_current",
        params={"sponsored": str(bool(sponsored)).lower(), "next_cursor": next_cursor},
        timeout=timeout,
        ),
        endpoint_name="clob.rewards-current",
    )


def get_raw_rewards_for_market(condition_id: str, timeout: float = 10.0) -> Dict[str, Any]:
    return as_dict(_get_json("rewards_market", path=f"/rewards/markets/{condition_id}", timeout=timeout), endpoint_name="clob.rewards-market")


def get_rewards_markets(next_cursor: Optional[str] = None, timeout: float = 10.0) -> Dict[str, Any]:
    return as_dict(_get_json("rewards_markets", params={"next_cursor": next_cursor}, timeout=timeout), endpoint_name="clob.rewards-markets")


def get_builder_trades(builder_code: str, timeout: float = 10.0, **filters: Any) -> Dict[str, Any]:
    params: Dict[str, Any] = {"builder_code": builder_code}
    params.update(filters)
    data = _get_json("builder_trades", params=params, timeout=timeout)
    return data if isinstance(data, dict) else {}
