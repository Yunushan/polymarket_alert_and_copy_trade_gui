from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import clob_rest, data_api
from .accounting import download_and_parse_accounting_snapshot, reconcile_mdd_payload_with_accounting
from .util import normalize_wallet


MDD_METHOD_V2 = "public_data_historical_equity_curve_v2"
MDD_METHOD_MARK_REPLAY = "clob_price_history_inventory_mark_replay_v1"
# Increment when changing calculations to invalidate durable scan enrichment.
MDD_CALCULATION_VERSION = 1
MDD_PCT_BASIS_V2 = "drawdown_usd / (equity_base_usd + peak_pnl_usd)"
MAX_CLOSED_POSITIONS = 1000
MAX_OPEN_POSITIONS = 1000
MAX_ACTIVITY_EVENTS = 5000
MAX_TRADE_ROWS = 5000
MAX_MARK_REPLAY_TOKENS = 20
MAX_MARK_REPLAY_POINTS = 10000
DEFAULT_CACHE_TTL_SECONDS = 60

MDD_V2_ASSUMPTIONS = [
    "Closed positions are ordered by public Data API timestamp and contribute realizedPnl to the historical PnL curve.",
    "Current open positions are represented by one current snapshot using public currentValue and PnL fields.",
    "Activity/trade rows are used for capital and exposure basis; they do not reconstruct historical mark-to-market prices.",
    "When equity_base_usd is not supplied, percentage MDD uses the largest public capital basis found from closed positions, open positions, and trade notional.",
]
MDD_V2_LIMITATIONS = [
    "Public Data API rows do not expose a complete deposit/withdrawal ledger, so true account-equity cash flows are not independently verified.",
    "Historical unrealized valleys between trade/close timestamps require per-token price replay and exact position inventory.",
    "Unresolved/open markets are included only at the current snapshot, not at every historical timestamp.",
]
MDD_MARK_REPLAY_ASSUMPTIONS = [
    "Trade rows are replayed as token inventory using public side, size, price, asset id, and timestamp fields.",
    "CLOB price-history points mark reconstructed inventory at sampled historical timestamps.",
    "The replay uses trade cash plus marked token inventory as a PnL curve, not a full account-equity ledger.",
    "When equity_base_usd is not supplied, percentage MDD reuses the public capital basis from the v2 payload.",
]
MDD_MARK_REPLAY_LIMITATIONS = [
    "Positions opened before the fetched trade window cannot be reconstructed unless the missing trades are supplied by the public API window.",
    "Resolved/redeemed markets, split/merge conversions, fees, rewards, deposits, and withdrawals are reported as limitations unless represented in trade cash flows.",
    "CLOB price history is sampled by the requested interval/fidelity, so valleys between samples can still be missed.",
    "Only the first 20 trade-derived asset ids are replayed per request to honor the documented batch price-history cap.",
]
MDD_ACCOUNTING_ASSUMPTIONS = [
    "When requested, the public accounting snapshot ZIP is parsed for equity and position CSV rows.",
    "The strongest available MDD percentage base prefers the maximum equity value from equity.csv over public trade notional.",
    "Accounting snapshot reconciliation is additive and does not place orders or require credentials.",
]
MDD_ACCOUNTING_LIMITATIONS = [
    "Snapshot CSV schemas may change; unknown columns are ignored and reported through parser warnings where possible.",
    "Cash-flow gaps are reported when explicit deposit/withdrawal fields are unavailable or do not explain equity changes.",
    "Snapshot reconciliation is a point-in-time statement check, not proof that every historical intra-sample equity valley was captured.",
]


@dataclass(frozen=True)
class MddInputs:
    wallet: str
    closed_positions: List[Dict[str, Any]]
    open_positions: List[Dict[str, Any]]
    activity_events: List[Dict[str, Any]]
    trade_rows: List[Dict[str, Any]]
    cache_hit: bool = False


_INPUT_CACHE: Dict[Tuple[Any, ...], Tuple[float, MddInputs]] = {}
_PRICE_HISTORY_CACHE: Dict[Tuple[Any, ...], Tuple[float, Dict[str, Any]]] = {}


def clear_mdd_input_cache() -> None:
    _INPUT_CACHE.clear()
    _PRICE_HISTORY_CACHE.clear()


def _lookup(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row:
            return row[key]
    lower_map = {str(key).lower(): value for key, value in row.items()}
    for key in keys:
        lowered = key.lower()
        if lowered in lower_map:
            return lower_map[lowered]
    return None


def _safe_float(value: Any, default: Optional[float] = 0.0) -> Optional[float]:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: Optional[int] = 0) -> Optional[int]:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(int(value), maximum))


def _position_total_pnl(row: Mapping[str, Any]) -> Optional[float]:
    total = _safe_float(_lookup(row, "totalPnl", "total_pnl"), None)
    if total is not None:
        return total
    values = [
        _safe_float(_lookup(row, "cashPnl", "cash_pnl"), None),
        _safe_float(_lookup(row, "realizedPnl", "realized_pnl"), None),
    ]
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def _position_capital(row: Mapping[str, Any]) -> float:
    value = _safe_float(
        _lookup(row, "totalBought", "total_bought", "initialValue", "initial_value", "currentValue", "current_value"),
        0.0,
    )
    return max(float(value or 0.0), 0.0)


def _trade_notional(row: Mapping[str, Any]) -> float:
    explicit = _safe_float(_lookup(row, "usdcSize", "usdc_size", "notional", "value", "cash"), None)
    if explicit is not None:
        return abs(float(explicit))
    size = _safe_float(_lookup(row, "size", "tokens"), None)
    price = _safe_float(_lookup(row, "price", "avgPrice", "avg_price"), None)
    if size is None or price is None:
        return 0.0
    return abs(float(size) * float(price))


def _trade_side(row: Mapping[str, Any]) -> str:
    return str(_lookup(row, "side") or "").strip().upper()


def _trade_token_id(row: Mapping[str, Any]) -> str:
    return str(_lookup(row, "asset", "token", "tokenId", "token_id", "assetId", "asset_id", "market") or "").strip()


def _trade_size(row: Mapping[str, Any]) -> Optional[float]:
    value = _safe_float(_lookup(row, "size", "tokens", "amount"), None)
    return abs(float(value)) if value is not None else None


def _trade_price(row: Mapping[str, Any]) -> Optional[float]:
    value = _safe_float(_lookup(row, "price", "avgPrice", "avg_price"), None)
    return float(value) if value is not None else None


def _trade_key(row: Mapping[str, Any]) -> Tuple[Any, ...]:
    return (
        _lookup(row, "transactionHash", "transaction_hash", "txHash", "hash"),
        _trade_token_id(row),
        _safe_int(_lookup(row, "timestamp"), 0),
        _trade_side(row),
        _trade_size(row),
        _trade_price(row),
    )


def _fetch_closed_positions(wallet: str, limit: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    offset = 0
    clean_limit = _clamp(limit, 0, MAX_CLOSED_POSITIONS)
    while len(rows) < clean_limit:
        page_limit = min(50, clean_limit - len(rows))
        page = data_api.get_closed_positions(
            wallet,
            limit=page_limit,
            offset=offset,
            sort_by="TIMESTAMP",
            sort_direction="ASC",
        )
        if not page:
            break
        rows.extend(page)
        if len(page) < page_limit:
            break
        offset += len(page)
    return rows


def _fetch_open_positions(wallet: str, limit: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    offset = 0
    clean_limit = _clamp(limit, 0, MAX_OPEN_POSITIONS)
    while len(rows) < clean_limit:
        page_limit = min(500, clean_limit - len(rows))
        page = data_api.get_positions(wallet, limit=page_limit, offset=offset)
        if not page:
            break
        rows.extend(page)
        if len(page) < page_limit:
            break
        offset += len(page)
    return rows


def _fetch_activity_events(wallet: str, limit: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    offset = 0
    clean_limit = _clamp(limit, 0, MAX_ACTIVITY_EVENTS)
    while len(rows) < clean_limit:
        page_limit = min(500, clean_limit - len(rows))
        page = data_api.get_activity(
            wallet,
            limit=page_limit,
            offset=offset,
            types=["TRADE", "SPLIT", "MERGE", "REDEEM", "REWARD", "CONVERSION", "MAKER_REBATE", "REFERRAL_REWARD"],
            sort_by="TIMESTAMP",
            sort_direction="ASC",
        )
        if not page:
            break
        rows.extend(page)
        if len(page) < page_limit:
            break
        offset += len(page)
    return rows


def _fetch_trade_rows(wallet: str, limit: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    offset = 0
    clean_limit = _clamp(limit, 0, MAX_TRADE_ROWS)
    while len(rows) < clean_limit:
        page_limit = min(500, clean_limit - len(rows))
        page = data_api.get_trades(wallet, limit=page_limit, offset=offset)
        if not page:
            break
        rows.extend(page)
        if len(page) < page_limit:
            break
        offset += len(page)
    return rows


def fetch_mdd_inputs(
    wallet: str,
    *,
    closed_limit: int = 500,
    open_limit: int = 500,
    activity_limit: int = 1000,
    trade_limit: int = 1000,
    include_open: bool = True,
    cache_ttl_seconds: int = 0,
) -> MddInputs:
    normalized_wallet = normalize_wallet(str(wallet or "").strip())
    if not normalized_wallet:
        raise ValueError("user must be a valid 0x wallet/proxyWallet address.")

    clean_closed_limit = _clamp(closed_limit, 0, MAX_CLOSED_POSITIONS)
    clean_open_limit = _clamp(open_limit, 0, MAX_OPEN_POSITIONS) if include_open else 0
    clean_activity_limit = _clamp(activity_limit, 0, MAX_ACTIVITY_EVENTS)
    clean_trade_limit = _clamp(trade_limit, 0, MAX_TRADE_ROWS)
    cache_key = (normalized_wallet, clean_closed_limit, clean_open_limit, clean_activity_limit, clean_trade_limit)
    ttl = max(int(cache_ttl_seconds or 0), 0)
    now = time.time()
    if ttl > 0:
        cached = _INPUT_CACHE.get(cache_key)
        if cached and now - cached[0] <= ttl:
            old = cached[1]
            return MddInputs(
                wallet=old.wallet,
                closed_positions=list(old.closed_positions),
                open_positions=list(old.open_positions),
                activity_events=list(old.activity_events),
                trade_rows=list(old.trade_rows),
                cache_hit=True,
            )

    inputs = MddInputs(
        wallet=normalized_wallet,
        closed_positions=_fetch_closed_positions(normalized_wallet, clean_closed_limit),
        open_positions=_fetch_open_positions(normalized_wallet, clean_open_limit) if include_open else [],
        activity_events=_fetch_activity_events(normalized_wallet, clean_activity_limit),
        trade_rows=_fetch_trade_rows(normalized_wallet, clean_trade_limit),
    )
    if ttl > 0:
        _INPUT_CACHE[cache_key] = (now, inputs)
    return inputs


def max_drawdown(points: Sequence[Mapping[str, Any]], equity_base_usd: Optional[float]) -> Dict[str, Any]:
    if not points:
        return {
            "mdd_usd": 0.0,
            "mdd_pct": 0.0 if equity_base_usd and equity_base_usd > 0 else None,
            "peak_value": 0.0,
            "trough_value": 0.0,
            "peak_timestamp": None,
            "trough_timestamp": None,
        }
    peak_value = float(points[0]["value"])
    peak_ts = points[0].get("timestamp")
    trough_value = peak_value
    trough_ts = peak_ts
    max_dd = 0.0
    max_dd_pct: Optional[float] = 0.0 if equity_base_usd and equity_base_usd > 0 else None
    max_peak = peak_value
    max_peak_ts = peak_ts
    for point in points:
        value = float(point["value"])
        timestamp = point.get("timestamp")
        if value > peak_value:
            peak_value = value
            peak_ts = timestamp
        drawdown = max(0.0, peak_value - value)
        denominator = (float(equity_base_usd) + peak_value) if equity_base_usd and equity_base_usd > 0 else None
        drawdown_pct = (drawdown / denominator * 100.0) if denominator and denominator > 0 else None
        if drawdown > max_dd:
            max_dd = drawdown
            max_dd_pct = drawdown_pct
            max_peak = peak_value
            max_peak_ts = peak_ts
            trough_value = value
            trough_ts = timestamp
    return {
        "mdd_usd": max_dd,
        "mdd_pct": max_dd_pct,
        "peak_value": max_peak,
        "trough_value": trough_value,
        "peak_timestamp": max_peak_ts,
        "trough_timestamp": trough_ts,
    }


def _canonical_trade_events(activity_events: Sequence[Mapping[str, Any]], trade_rows: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    events: List[Mapping[str, Any]] = []
    seen = set()
    for row in list(activity_events) + list(trade_rows):
        row_type = str(_lookup(row, "type") or "TRADE").strip().upper()
        if row_type and row_type != "TRADE":
            continue
        key = _trade_key(row)
        if key in seen:
            continue
        seen.add(key)
        events.append(row)

    events.sort(key=lambda item: _safe_int(_lookup(item, "timestamp"), 0) or 0)
    return events


def _trade_capital_stats(activity_events: Sequence[Mapping[str, Any]], trade_rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    events = _canonical_trade_events(activity_events, trade_rows)
    buy_notional = 0.0
    sell_notional = 0.0
    unknown_notional = 0.0
    gross_notional = 0.0
    net_cash_flow = 0.0
    deployed = 0.0
    max_deployed = 0.0
    timeline: List[Dict[str, Any]] = []
    for row in events:
        notional = _trade_notional(row)
        if notional <= 0:
            continue
        side = _trade_side(row)
        gross_notional += notional
        if side == "BUY":
            buy_notional += notional
            net_cash_flow -= notional
            deployed += notional
        elif side == "SELL":
            sell_notional += notional
            net_cash_flow += notional
            deployed = max(0.0, deployed - notional)
        else:
            unknown_notional += notional
        max_deployed = max(max_deployed, deployed)
        timeline.append(
            {
                "timestamp": _safe_int(_lookup(row, "timestamp"), 0),
                "side": side or None,
                "notional_usd": notional,
                "deployed_notional_usd": deployed,
            }
        )

    return {
        "events": len(events),
        "buy_notional_usd": buy_notional,
        "sell_notional_usd": sell_notional,
        "unknown_notional_usd": unknown_notional,
        "gross_notional_usd": gross_notional,
        "net_cash_flow_usd": net_cash_flow,
        "max_deployed_notional_usd": max_deployed,
        "timeline": timeline[-50:],
    }


def build_historical_mdd_payload(
    inputs: MddInputs,
    *,
    equity_base_usd: Optional[float] = None,
    max_points: int = 50,
) -> Dict[str, Any]:
    points: List[Dict[str, Any]] = []
    cumulative_realized = 0.0
    closed_capital = 0.0
    for row in sorted(inputs.closed_positions, key=lambda item: _safe_float(_lookup(item, "timestamp"), 0.0) or 0.0):
        realized = _safe_float(_lookup(row, "realizedPnl", "realized_pnl"), 0.0) or 0.0
        cumulative_realized += float(realized)
        closed_capital += _position_capital(row)
        points.append(
            {
                "timestamp": _safe_int(_lookup(row, "timestamp"), 0),
                "value": cumulative_realized,
                "kind": "closed_position",
                "realized_pnl": cumulative_realized,
                "realized_delta": float(realized),
                "capital_basis_usd": closed_capital,
            }
        )

    open_pnl = 0.0
    open_current_value = 0.0
    open_capital = 0.0
    for row in inputs.open_positions:
        pnl = _position_total_pnl(row)
        if pnl is not None:
            open_pnl += float(pnl)
        open_current_value += _safe_float(_lookup(row, "currentValue", "current_value"), 0.0) or 0.0
        open_capital += _position_capital(row)
    if inputs.open_positions:
        points.append(
            {
                "timestamp": int(time.time()),
                "value": cumulative_realized + open_pnl,
                "kind": "current_open_snapshot",
                "realized_pnl": cumulative_realized,
                "open_pnl": open_pnl,
                "open_current_value": open_current_value,
                "capital_basis_usd": closed_capital + open_capital,
            }
        )

    trade_stats = _trade_capital_stats(inputs.activity_events, inputs.trade_rows)
    public_capital_basis = max(
        closed_capital + open_capital,
        float(trade_stats["max_deployed_notional_usd"]),
        float(trade_stats["buy_notional_usd"]),
    )
    if equity_base_usd is not None:
        base = max(float(equity_base_usd), 0.0)
        base_source = "user_supplied"
    else:
        base = public_capital_basis
        base_source = "max_public_capital_basis_from_positions_and_trade_activity"

    drawdown = max_drawdown(points, base if base > 0 else None)
    clean_max_points = _clamp(max_points, 1, 1000)
    return {
        "wallet": inputs.wallet,
        "version": 2,
        "mdd_usd": drawdown["mdd_usd"],
        "mdd_pct": drawdown["mdd_pct"],
        "mdd_available": True,
        "mdd_method": MDD_METHOD_V2,
        "mdd_pct_basis": MDD_PCT_BASIS_V2,
        "equity_base_usd": base if base > 0 else None,
        "equity_base_source": base_source,
        "public_capital_basis_usd": public_capital_basis if public_capital_basis > 0 else None,
        "peak_value": drawdown["peak_value"],
        "trough_value": drawdown["trough_value"],
        "peak_timestamp": drawdown["peak_timestamp"],
        "trough_timestamp": drawdown["trough_timestamp"],
        "closed_positions": len(inputs.closed_positions),
        "open_positions": len(inputs.open_positions),
        "activity_events": len(inputs.activity_events),
        "trade_events": len(inputs.trade_rows),
        "trade_capital": {key: value for key, value in trade_stats.items() if key != "timeline"},
        "trade_capital_timeline": trade_stats["timeline"],
        "closed_capital_basis_usd": closed_capital,
        "open_capital_basis_usd": open_capital,
        "cumulative_realized_pnl": cumulative_realized,
        "open_pnl": open_pnl,
        "open_current_value": open_current_value,
        "points": points[-clean_max_points:],
        "points_total": len(points),
        "assumptions": list(MDD_V2_ASSUMPTIONS),
        "limitations": list(MDD_V2_LIMITATIONS),
        "cache": {
            "input_cache": "process_memory_public_data_only",
            "hit": inputs.cache_hit,
            "ttl_seconds": DEFAULT_CACHE_TTL_SECONDS,
        },
    }


def _normalize_price_history_points(raw: Any) -> List[Dict[str, float]]:
    if isinstance(raw, Mapping):
        raw = raw.get("history") or raw.get("prices") or raw.get("data") or raw.get("points") or []
    if not isinstance(raw, list):
        return []
    points: List[Dict[str, float]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        timestamp = _safe_int(_lookup(item, "t", "timestamp", "ts", "time"), None)
        price = _safe_float(_lookup(item, "p", "price", "value"), None)
        if timestamp is None or price is None:
            continue
        points.append({"timestamp": int(timestamp), "price": float(price)})
    points.sort(key=lambda item: item["timestamp"])
    return points


def _extract_token_history(payload: Mapping[str, Any], token_id: str) -> List[Dict[str, float]]:
    history = payload.get("history")
    if isinstance(history, Mapping):
        direct = history.get(token_id)
        if direct is None:
            direct = history.get(str(token_id))
        return _normalize_price_history_points(direct)
    if isinstance(history, list):
        direct_points = _normalize_price_history_points(history)
        if direct_points:
            return direct_points
        for item in history:
            if not isinstance(item, Mapping):
                continue
            item_token = str(_lookup(item, "market", "asset", "asset_id", "token_id", "tokenId") or "").strip()
            if item_token == token_id:
                return _normalize_price_history_points(item)
    for key in ("data", "results", "markets"):
        rows = payload.get(key)
        if not isinstance(rows, list):
            continue
        for item in rows:
            if not isinstance(item, Mapping):
                continue
            item_token = str(_lookup(item, "market", "asset", "asset_id", "token_id", "tokenId") or "").strip()
            if item_token == token_id:
                return _normalize_price_history_points(item)
    return []


def _batch_price_history_cached(
    token_ids: Sequence[str],
    *,
    start_ts: Optional[int],
    end_ts: Optional[int],
    interval: Optional[str],
    fidelity: Optional[int],
    cache_ttl_seconds: int,
) -> Tuple[Dict[str, Any], bool]:
    cache_key = (
        tuple(token_ids),
        int(start_ts) if start_ts is not None else None,
        int(end_ts) if end_ts is not None else None,
        str(interval or ""),
        int(fidelity) if fidelity is not None else None,
    )
    now = time.time()
    ttl = max(int(cache_ttl_seconds or 0), 0)
    if ttl > 0:
        cached = _PRICE_HISTORY_CACHE.get(cache_key)
        if cached and now - cached[0] <= ttl:
            return dict(cached[1]), True
    payload = clob_rest.get_batch_price_history(
        token_ids,
        start_ts=start_ts,
        end_ts=end_ts,
        interval=interval,
        fidelity=fidelity,
    )
    if ttl > 0:
        _PRICE_HISTORY_CACHE[cache_key] = (now, dict(payload))
    return payload, False


def _sample_timeline(events: List[Tuple[int, int, str, Any]], limit: int) -> Tuple[List[Tuple[int, int, str, Any]], bool]:
    clean_limit = _clamp(limit, 1, MAX_MARK_REPLAY_POINTS)
    if len(events) <= clean_limit:
        return events, False
    step = max(int(math.ceil(len(events) / clean_limit)), 1)
    sampled = [event for index, event in enumerate(events) if index % step == 0]
    if sampled[-1] != events[-1]:
        sampled = sampled[: max(clean_limit - 1, 0)] + [events[-1]]
    return sampled[:clean_limit], True


def _build_mark_replay_points(
    trade_events: Sequence[Mapping[str, Any]],
    histories_by_token: Mapping[str, Sequence[Mapping[str, float]]],
    *,
    point_limit: int,
) -> Dict[str, Any]:
    timeline: List[Tuple[int, int, str, Any]] = []
    for row in trade_events:
        timestamp = _safe_int(_lookup(row, "timestamp"), None)
        if timestamp is not None:
            timeline.append((int(timestamp), 0, "trade", row))
    for token_id, points in histories_by_token.items():
        for point in points:
            timestamp = _safe_int(point.get("timestamp"), None)
            price = _safe_float(point.get("price"), None)
            if timestamp is not None and price is not None:
                timeline.append((int(timestamp), 1, "mark", {"token_id": token_id, "price": float(price)}))
    timeline.sort(key=lambda item: (item[0], item[1]))
    timeline, truncated = _sample_timeline(timeline, point_limit)

    cash = 0.0
    quantities: Dict[str, float] = {}
    last_prices: Dict[str, float] = {}
    points: List[Dict[str, Any]] = []
    trades_without_token = 0
    trades_without_size_or_price = 0
    negative_inventory_events = 0
    for timestamp, _order, kind, payload in timeline:
        if kind == "trade":
            row = payload
            token_id = _trade_token_id(row)
            side = _trade_side(row)
            size = _trade_size(row)
            price = _trade_price(row)
            if not token_id:
                trades_without_token += 1
                continue
            if size is None or price is None:
                trades_without_size_or_price += 1
                continue
            notional = size * price
            last_prices[token_id] = price
            current_qty = quantities.get(token_id, 0.0)
            if side == "BUY":
                quantities[token_id] = current_qty + size
                cash -= notional
            elif side == "SELL":
                quantities[token_id] = current_qty - size
                cash += notional
                if quantities[token_id] < -1e-9:
                    negative_inventory_events += 1
            else:
                trades_without_size_or_price += 1
                continue
        else:
            token_id = str(payload["token_id"])
            last_prices[token_id] = float(payload["price"])

        marked_value = 0.0
        missing_mark_tokens: List[str] = []
        active_inventory = 0
        for token_id, quantity in quantities.items():
            if abs(quantity) <= 1e-9:
                continue
            active_inventory += 1
            price = last_prices.get(token_id)
            if price is None:
                missing_mark_tokens.append(token_id)
                continue
            marked_value += quantity * price
        value = cash + marked_value
        points.append(
            {
                "timestamp": timestamp,
                "value": value,
                "kind": "historical_mark_replay",
                "cash_flow_usd": cash,
                "marked_inventory_value_usd": marked_value,
                "active_inventory": active_inventory,
                "missing_mark_tokens": missing_mark_tokens,
            }
        )

    final_inventory = {
        token_id: quantity
        for token_id, quantity in quantities.items()
        if abs(quantity) > 1e-9
    }
    return {
        "points": points,
        "timeline_truncated": truncated,
        "trades_without_token": trades_without_token,
        "trades_without_size_or_price": trades_without_size_or_price,
        "negative_inventory_events": negative_inventory_events,
        "final_inventory": final_inventory,
    }


def _base_payload_summary(payload: Mapping[str, Any]) -> Dict[str, Any]:
    keys = [
        "mdd_usd",
        "mdd_pct",
        "mdd_method",
        "equity_base_usd",
        "equity_base_source",
        "public_capital_basis_usd",
        "peak_value",
        "trough_value",
        "peak_timestamp",
        "trough_timestamp",
        "closed_positions",
        "open_positions",
        "activity_events",
        "trade_events",
        "cumulative_realized_pnl",
        "open_current_value",
    ]
    return {key: payload.get(key) for key in keys}


def _apply_accounting_snapshot_if_requested(
    payload: Dict[str, Any],
    wallet: str,
    *,
    include_accounting_snapshot: bool = False,
    accounting_timeout: float = 30.0,
) -> Dict[str, Any]:
    if not include_accounting_snapshot:
        return payload
    try:
        snapshot = download_and_parse_accounting_snapshot(wallet, timeout=accounting_timeout)
    except Exception as exc:
        result = dict(payload)
        result["accounting_snapshot"] = {
            "status": "unavailable",
            "reason": f"Accounting snapshot download or parse failed: {type(exc).__name__}",
            "reconciliation": {
                "status": "unavailable",
                "mdd_pct_uses_accounting_base": False,
            },
        }
        return result
    result = reconcile_mdd_payload_with_accounting(payload, snapshot)
    result["assumptions"] = list(result.get("assumptions", [])) + list(MDD_ACCOUNTING_ASSUMPTIONS)
    result["limitations"] = list(result.get("limitations", [])) + list(MDD_ACCOUNTING_LIMITATIONS)
    return result


def build_mark_replay_mdd_payload(
    inputs: MddInputs,
    *,
    equity_base_usd: Optional[float] = None,
    max_points: int = 50,
    mark_replay_token_limit: int = 10,
    mark_replay_point_limit: int = 5000,
    mark_replay_interval: Optional[str] = "1h",
    mark_replay_fidelity: Optional[int] = 60,
    mark_replay_start_ts: Optional[int] = None,
    mark_replay_end_ts: Optional[int] = None,
    cache_ttl_seconds: int = 0,
) -> Dict[str, Any]:
    base = build_historical_mdd_payload(inputs, equity_base_usd=equity_base_usd, max_points=max_points)
    fallback_summary = _base_payload_summary(base)
    trade_events = _canonical_trade_events(inputs.activity_events, inputs.trade_rows)
    token_ids: List[str] = []
    clipped_token_ids: List[str] = []
    trades_without_token = 0
    for row in trade_events:
        token_id = _trade_token_id(row)
        if not token_id:
            trades_without_token += 1
            continue
        if token_id in token_ids:
            continue
        if len(token_ids) < _clamp(mark_replay_token_limit, 1, MAX_MARK_REPLAY_TOKENS):
            token_ids.append(token_id)
        else:
            clipped_token_ids.append(token_id)
    replay_trade_events = [row for row in trade_events if _trade_token_id(row) in set(token_ids)]
    timestamps = [
        _safe_int(_lookup(row, "timestamp"), None)
        for row in replay_trade_events
    ]
    timestamps = [int(value) for value in timestamps if value is not None]
    if not token_ids or not timestamps:
        base.update(
            {
                "requested_mdd_method": MDD_METHOD_MARK_REPLAY,
                "mark_replay": {
                    "status": "unavailable",
                    "reason": "No trade-derived token inventory with timestamps was available from the public Data API window.",
                    "token_count": len(token_ids),
                    "trades_without_token": trades_without_token,
                    "clipped_token_ids": clipped_token_ids,
                },
                "fallback_v2": fallback_summary,
            }
        )
        return base

    start_ts = int(mark_replay_start_ts if mark_replay_start_ts is not None else min(timestamps))
    end_ts = int(mark_replay_end_ts if mark_replay_end_ts is not None else max(max(timestamps), int(time.time())))
    if end_ts <= start_ts:
        end_ts = start_ts + 60
    interval = str(mark_replay_interval or "").strip() or None
    if interval not in {None, "max", "all", "1m", "1w", "1d", "6h", "1h"}:
        interval = "1h"
    fidelity = _clamp(mark_replay_fidelity, 1, 1440) if mark_replay_fidelity is not None else None

    try:
        price_payload, price_cache_hit = _batch_price_history_cached(
            token_ids,
            start_ts=start_ts,
            end_ts=end_ts,
            interval=interval,
            fidelity=fidelity,
            cache_ttl_seconds=cache_ttl_seconds,
        )
    except Exception as exc:
        base.update(
            {
                "requested_mdd_method": MDD_METHOD_MARK_REPLAY,
                "mark_replay": {
                    "status": "unavailable",
                    "reason": f"CLOB price-history request failed: {type(exc).__name__}",
                    "token_ids": token_ids,
                    "clipped_token_ids": clipped_token_ids,
                },
                "fallback_v2": fallback_summary,
            }
        )
        return base
    histories_by_token = {
        token_id: _extract_token_history(price_payload, token_id)
        for token_id in token_ids
    }
    missing_history_tokens = [token_id for token_id, points in histories_by_token.items() if not points]
    replay = _build_mark_replay_points(
        replay_trade_events,
        histories_by_token,
        point_limit=mark_replay_point_limit,
    )
    replay_points = replay["points"]
    if not replay_points:
        base.update(
            {
                "requested_mdd_method": MDD_METHOD_MARK_REPLAY,
                "mark_replay": {
                    "status": "unavailable",
                    "reason": "CLOB price-history replay produced no markable points.",
                    "token_ids": token_ids,
                    "missing_history_tokens": missing_history_tokens,
                    "clipped_token_ids": clipped_token_ids,
                    "price_history_cache_hit": price_cache_hit,
                },
                "fallback_v2": fallback_summary,
            }
        )
        return base

    equity_base = base.get("equity_base_usd")
    drawdown = max_drawdown(replay_points, float(equity_base) if equity_base else None)
    clean_max_points = _clamp(max_points, 1, 1000)
    base.update(
        {
            "version": 3,
            "mdd_usd": drawdown["mdd_usd"],
            "mdd_pct": drawdown["mdd_pct"],
            "mdd_method": MDD_METHOD_MARK_REPLAY,
            "mdd_pct_basis": MDD_PCT_BASIS_V2,
            "peak_value": drawdown["peak_value"],
            "trough_value": drawdown["trough_value"],
            "peak_timestamp": drawdown["peak_timestamp"],
            "trough_timestamp": drawdown["trough_timestamp"],
            "points": replay_points[-clean_max_points:],
            "points_total": len(replay_points),
            "assumptions": list(MDD_MARK_REPLAY_ASSUMPTIONS),
            "limitations": list(MDD_MARK_REPLAY_LIMITATIONS),
            "mark_replay": {
                "status": "ok" if not missing_history_tokens and not clipped_token_ids else "partial",
                "token_ids": token_ids,
                "token_count": len(token_ids),
                "clipped_token_ids": clipped_token_ids,
                "missing_history_tokens": missing_history_tokens,
                "trade_events_replayed": len(replay_trade_events),
                "price_history_points": sum(len(points) for points in histories_by_token.values()),
                "start_ts": start_ts,
                "end_ts": end_ts,
                "interval": interval,
                "fidelity": fidelity,
                "batch_size": len(token_ids),
                "batch_cap": MAX_MARK_REPLAY_TOKENS,
                "price_history_cache_hit": price_cache_hit,
                "timeline_truncated": replay["timeline_truncated"],
                "trades_without_token": trades_without_token + replay["trades_without_token"],
                "trades_without_size_or_price": replay["trades_without_size_or_price"],
                "negative_inventory_events": replay["negative_inventory_events"],
                "final_inventory": replay["final_inventory"],
            },
            "fallback_v2": fallback_summary,
        }
    )
    return base


def polymarket_user_mdd_payload_v2(
    wallet: str,
    *,
    closed_limit: int = 500,
    open_limit: int = 500,
    activity_limit: int = 1000,
    trade_limit: int = 1000,
    include_open: bool = True,
    equity_base_usd: Optional[float] = None,
    max_points: int = 50,
    cache_ttl_seconds: int = 0,
    include_accounting_snapshot: bool = False,
    accounting_timeout: float = 30.0,
) -> Dict[str, Any]:
    inputs = fetch_mdd_inputs(
        wallet,
        closed_limit=closed_limit,
        open_limit=open_limit,
        activity_limit=activity_limit,
        trade_limit=trade_limit,
        include_open=include_open,
        cache_ttl_seconds=cache_ttl_seconds,
    )
    payload = build_historical_mdd_payload(inputs, equity_base_usd=equity_base_usd, max_points=max_points)
    payload["input_limits"] = {
        "closed_limit": _clamp(closed_limit, 0, MAX_CLOSED_POSITIONS),
        "open_limit": _clamp(open_limit, 0, MAX_OPEN_POSITIONS) if include_open else 0,
        "activity_limit": _clamp(activity_limit, 0, MAX_ACTIVITY_EVENTS),
        "trade_limit": _clamp(trade_limit, 0, MAX_TRADE_ROWS),
        "max_points": _clamp(max_points, 1, 1000),
    }
    payload["cache"]["ttl_seconds"] = max(int(cache_ttl_seconds or 0), 0)
    return _apply_accounting_snapshot_if_requested(
        payload,
        inputs.wallet,
        include_accounting_snapshot=include_accounting_snapshot,
        accounting_timeout=accounting_timeout,
    )


def polymarket_user_mdd_payload_mark_replay(
    wallet: str,
    *,
    closed_limit: int = 500,
    open_limit: int = 500,
    activity_limit: int = 1000,
    trade_limit: int = 1000,
    include_open: bool = True,
    equity_base_usd: Optional[float] = None,
    max_points: int = 50,
    cache_ttl_seconds: int = 0,
    mark_replay_token_limit: int = 10,
    mark_replay_point_limit: int = 5000,
    mark_replay_interval: Optional[str] = "1h",
    mark_replay_fidelity: Optional[int] = 60,
    mark_replay_start_ts: Optional[int] = None,
    mark_replay_end_ts: Optional[int] = None,
    include_accounting_snapshot: bool = False,
    accounting_timeout: float = 30.0,
) -> Dict[str, Any]:
    inputs = fetch_mdd_inputs(
        wallet,
        closed_limit=closed_limit,
        open_limit=open_limit,
        activity_limit=activity_limit,
        trade_limit=trade_limit,
        include_open=include_open,
        cache_ttl_seconds=cache_ttl_seconds,
    )
    payload = build_mark_replay_mdd_payload(
        inputs,
        equity_base_usd=equity_base_usd,
        max_points=max_points,
        mark_replay_token_limit=mark_replay_token_limit,
        mark_replay_point_limit=mark_replay_point_limit,
        mark_replay_interval=mark_replay_interval,
        mark_replay_fidelity=mark_replay_fidelity,
        mark_replay_start_ts=mark_replay_start_ts,
        mark_replay_end_ts=mark_replay_end_ts,
        cache_ttl_seconds=cache_ttl_seconds,
    )
    payload["input_limits"] = {
        "closed_limit": _clamp(closed_limit, 0, MAX_CLOSED_POSITIONS),
        "open_limit": _clamp(open_limit, 0, MAX_OPEN_POSITIONS) if include_open else 0,
        "activity_limit": _clamp(activity_limit, 0, MAX_ACTIVITY_EVENTS),
        "trade_limit": _clamp(trade_limit, 0, MAX_TRADE_ROWS),
        "max_points": _clamp(max_points, 1, 1000),
        "mark_replay_token_limit": _clamp(mark_replay_token_limit, 1, MAX_MARK_REPLAY_TOKENS),
        "mark_replay_point_limit": _clamp(mark_replay_point_limit, 1, MAX_MARK_REPLAY_POINTS),
    }
    payload["cache"]["ttl_seconds"] = max(int(cache_ttl_seconds or 0), 0)
    return _apply_accounting_snapshot_if_requested(
        payload,
        inputs.wallet,
        include_accounting_snapshot=include_accounting_snapshot,
        accounting_timeout=accounting_timeout,
    )
