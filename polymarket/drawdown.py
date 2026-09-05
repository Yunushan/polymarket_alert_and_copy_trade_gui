from __future__ import annotations

import math
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence


def _finite(value: Any) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Drawdown inputs must be finite numbers.")
    return number


def percentage_drawdown(
    episodes: Sequence[Mapping[str, Any]], equity_base_usd: Optional[float]
) -> Dict[str, Any]:
    base = _finite(equity_base_usd) if equity_base_usd is not None else None
    result: Dict[str, Any] = {
        "mdd_pct": 0.0 if base and base > 0 else None,
        "pct_drawdown_usd": None,
        "pct_peak_value": None,
        "pct_trough_value": None,
        "pct_peak_timestamp": None,
        "pct_trough_timestamp": None,
    }
    for episode in episodes:
        peak = _finite(episode["peak_value"])
        trough = _finite(episode["trough_value"])
        if peak < 0 or trough > peak:
            raise ValueError("Drawdown episodes must start at a nonnegative PnL peak.")
        loss = _finite(peak - trough)
        if base is None or base <= 0:
            continue
        denominator = _finite(base + peak)
        pct = _finite(loss / denominator * 100.0)
        if pct > result["mdd_pct"]:
            result.update(
                mdd_pct=pct,
                pct_drawdown_usd=loss,
                pct_peak_value=peak,
                pct_trough_value=trough,
                pct_peak_timestamp=episode.get("peak_timestamp"),
                pct_trough_timestamp=episode.get("trough_timestamp"),
            )
    return result


def max_drawdown(
    points: Iterable[Mapping[str, Any]], equity_base_usd: Optional[float]
) -> Dict[str, Any]:
    """Measure sampled PnL losses from a zero-PnL observed-window baseline.

    Dollar and percentage maxima may belong to different episodes. Retaining
    the lowest trough for each running peak also permits exact percentage
    recalculation when the equity base changes, without the full point history.
    This baseline does not establish pre-window inventory or account cash flows.
    """
    peak = 0.0
    peak_ts = None
    episodes: list[Dict[str, Any]] = []
    active: Optional[Dict[str, Any]] = None
    observed = False
    for point in points:
        observed = True
        value = _finite(point["value"])
        timestamp = point.get("timestamp")
        if value > peak:
            peak, peak_ts, active = value, timestamp, None
        elif value < peak:
            if active is None:
                active = {
                    "peak_value": peak,
                    "trough_value": value,
                    "peak_timestamp": peak_ts,
                    "trough_timestamp": timestamp,
                }
                episodes.append(active)
            elif value < active["trough_value"]:
                active.update(trough_value=value, trough_timestamp=timestamp)

    percentage = percentage_drawdown(episodes, equity_base_usd)
    maximum = max(episodes, key=lambda item: item["peak_value"] - item["trough_value"], default=None)
    return {
        "mdd_usd": _finite(maximum["peak_value"] - maximum["trough_value"]) if maximum else (0.0 if observed else None),
        **percentage,
        "mdd_pct": percentage["mdd_pct"] if observed else None,
        "mdd_available": observed,
        "peak_value": maximum["peak_value"] if maximum else None,
        "trough_value": maximum["trough_value"] if maximum else None,
        "peak_timestamp": maximum["peak_timestamp"] if maximum else None,
        "trough_timestamp": maximum["trough_timestamp"] if maximum else None,
        "drawdown_episodes": episodes,
        "drawdown_baseline": "zero_pnl_at_start_of_observed_window",
    }
