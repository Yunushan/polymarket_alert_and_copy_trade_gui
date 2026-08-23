from __future__ import annotations

import argparse
import csv
import importlib
import importlib.metadata as importlib_metadata
import json
import math
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, TextIO
from urllib import error as urllib_error
from urllib import request as urllib_request

from core.storage import ConfigLoadError, DEFAULT_CONFIG_PATH, load_config, save_config
from market_adapters import build_default_registry
from polymarket.http_client import PolymarketHTTPError, PolymarketRateLimitError
from polymarket.leaderboard_state import LeaderboardStateStore
from polymarket.live_reports import (
    live_validation_coverage_promotion_proposal_markdown,
    live_validation_promotion_proposal_snapshot_diff_markdown,
    live_validation_promotion_proposal_snapshot_markdown,
    live_validation_report_decisions_markdown,
    live_validation_report_review_markdown,
)
from web_api import (
    DEFAULT_FRONTEND_DIR,
    LEADERBOARD_SORTS,
    _fetch_polymarket_leaderboard_scan_rows,
    add_wallet_watch,
    adapter_for_market,
    alert_from_payload,
    alerts_payload,
    app_state_payload,
    apply_config_patch,
    apply_copy_settings_patch,
    apply_market_patch,
    config_payload,
    copy_payload,
    copy_preview_payload,
    delete_alert,
    delete_wallet_watch,
    find_alert,
    health_payload,
    history_refill_payload,
    live_preflight_payload,
    live_safety_payload,
    markets_payload,
    paper_order_from_payload,
    paper_order_impact,
    paper_payload,
    paper_position_rows,
    paper_quote_limit_payload,
    paper_quote_payload,
    polymarket_clob_readiness_payload,
    polymarket_leaderboard_payload,
    polymarket_live_validation_payload,
    polymarket_live_validation_decision_store_payload,
    polymarket_live_validation_decisions_payload,
    polymarket_live_validation_promotion_proposal_payload,
    polymarket_live_validation_promotion_proposal_snapshot_diff_payload,
    polymarket_live_validation_promotion_proposal_snapshot_payload,
    polymarket_live_validation_promotion_proposal_snapshot_purge_payload,
    polymarket_live_validation_promotion_proposal_snapshot_store_payload,
    polymarket_live_validation_promotion_proposal_snapshots_payload,
    polymarket_live_validation_report_payload,
    polymarket_live_validation_report_purge_payload,
    polymarket_live_validation_report_review_payload,
    polymarket_live_validation_report_store_payload,
    polymarket_live_validation_reports_payload,
    polymarket_mdd_cache_health_payload,
    polymarket_mdd_cache_payload,
    polymarket_mdd_cache_purge_payload,
    polymarket_mdd_audit_params,
    polymarket_user_mdd_payload,
    polymarket_user_search_payload,
    poll_wallet_activity,
    position_refill_payload,
    refresh_alert_price,
    refresh_all_alert_prices,
    refresh_paper_marks,
    refresh_selected_paper_mark,
    require_market_enabled,
    run_server,
    serialize_market_contract,
    serialize_market_event,
    attach_polymarket_mdd_audit_cache,
    normalize_polymarket_leaderboard_row,
    submit_paper_order,
    serialize_market_candle,
    serialize_market_trade,
    serialize_orderbook,
    serialize_price_snapshot,
    update_wallet_watch,
    wallets_payload,
)


LEADERBOARD_FIELDS = [
    "rank",
    "display_name",
    "wallet",
    "pnl_usd",
    "volume_usd",
    "roi_pct",
    "trade_count",
    "mdd_usd",
    "mdd_pct",
    "mdd_method",
    "mdd_pct_basis",
    "mdd_source",
]

SORT_ALIASES = {
    "roi": "roi_pct",
    "roi_pct": "roi_pct",
    "pnl": "pnl_usd",
    "pnl_usd": "pnl_usd",
    "volume": "volume_usd",
    "vol": "volume_usd",
    "volume_usd": "volume_usd",
    "mdd": "mdd_pct",
    "mdd_pct": "mdd_pct",
    "mdd_usd": "mdd_usd",
}

DEPENDENCY_IMPORT_FALLBACKS = {
    "websocket-client": ("websocket",),
    "python-dotenv": ("dotenv",),
    "py-clob-client": ("py_clob_client",),
    "eth-account": ("eth_account",),
    "eth-abi": ("eth_abi",),
}


def _config_path(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "config", DEFAULT_CONFIG_PATH)).expanduser()


def _load_cfg(args: argparse.Namespace):
    return load_config(_config_path(args))


def _save_cfg(args: argparse.Namespace, cfg: Any) -> None:
    save_config(cfg, _config_path(args))


def _registry():
    return build_default_registry()


def _coerce_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    lowered = text.lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"none", "null"}:
        return None
    if not text:
        return ""
    try:
        return json.loads(text)
    except Exception:
        return text


def _json_arg(value: Optional[str]) -> Dict[str, Any]:
    if not value:
        return {}
    raw = value
    if raw.startswith("@"):
        raw = Path(raw[1:]).expanduser().read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise argparse.ArgumentTypeError("JSON payload must be an object.")
    return data


def _merge_kv(payload: Dict[str, Any], values: Optional[Sequence[tuple[str, str]]]) -> Dict[str, Any]:
    for key, value in values or []:
        payload[key] = _coerce_value(value)
    return payload


def _put_optional(payload: Dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        payload[key] = value


def _add_json_output_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output", "-o", default="-", help="Output path, or - for stdout.")
    parser.add_argument("--compact", action="store_true", help="Write compact JSON instead of indented JSON.")


def _write_json(payload: Mapping[str, Any], *, output: Optional[str] = "-", compact: bool = False) -> None:
    stream, should_close = _open_output(output)
    try:
        if compact:
            json.dump(payload, stream, separators=(",", ":"), sort_keys=True)
        else:
            json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")
    finally:
        if should_close:
            stream.close()


def _write_command_payload(args: argparse.Namespace, payload: Mapping[str, Any]) -> int:
    _write_json(payload, output=getattr(args, "output", "-"), compact=bool(getattr(args, "compact", False)))
    return 0


def _write_text_command(args: argparse.Namespace, value: str) -> int:
    stream, should_close = _open_output(getattr(args, "output", "-"))
    try:
        stream.write(value)
        if not value.endswith("\n"):
            stream.write("\n")
    finally:
        if should_close:
            stream.close()
    return 0


def _add_param(params: Dict[str, List[str]], key: str, value: Any) -> None:
    if value is None:
        return
    if isinstance(value, bool):
        params[key] = ["true" if value else "false"]
        return
    text = str(value).strip()
    if text:
        params[key] = [text]


def _split_key_value(raw: str) -> tuple[str, str]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError("--param values must use KEY=VALUE format.")
    key, value = raw.split("=", 1)
    key = key.strip()
    if not key:
        raise argparse.ArgumentTypeError("--param key cannot be empty.")
    return key, value.strip()


def build_polymarket_leaderboard_params(args: argparse.Namespace) -> Dict[str, List[str]]:
    params: Dict[str, List[str]] = {}
    sort = SORT_ALIASES.get(str(args.sort or "roi_pct").strip().lower(), "roi_pct")
    _add_param(params, "sort", sort)
    _add_param(params, "direction", args.direction)
    _add_param(params, "period", args.period)
    _add_param(params, "category", args.category)
    _add_param(params, "limit", args.returned)
    _add_param(params, "scan_limit", args.scanned)
    _add_param(params, "compute_mdd", args.compute_mdd)
    _add_param(params, "fast_scan", args.fast_scan)
    _add_param(params, "mdd_mode", args.mdd_mode)
    _add_param(params, "mdd_scan_limit", args.mdd_scan)
    _add_param(params, "mdd_history_limit", args.mdd_history_limit)
    _add_param(params, "mdd_activity_limit", args.mdd_activity_limit)
    _add_param(params, "mdd_trade_limit", args.mdd_trade_limit)
    _add_param(params, "mdd_open_limit", args.mdd_open_limit)
    _add_param(params, "mdd_mark_replay_token_limit", args.mdd_mark_replay_token_limit)
    _add_param(params, "mdd_mark_replay_point_limit", args.mdd_mark_replay_point_limit)
    _add_param(params, "mdd_mark_replay_interval", args.mdd_mark_replay_interval)
    _add_param(params, "mdd_mark_replay_fidelity", args.mdd_mark_replay_fidelity)
    _add_param(params, "mdd_include_accounting", args.mdd_include_accounting)
    _add_param(params, "mdd_accounting_timeout", args.mdd_accounting_timeout)
    _add_param(params, "mdd_persist_cache", args.mdd_persist_cache)
    _add_param(params, "mdd_cache_ttl_seconds", args.mdd_cache_ttl_seconds)
    _add_param(params, "equity_base_usd", args.equity_base_usd)
    _add_param(params, "scan_concurrency", args.scan_concurrency)
    _add_param(params, "scan_retry_attempts", args.scan_retry_attempts)
    _add_param(params, "scan_retry_delay_seconds", args.scan_retry_delay_seconds)
    _add_param(params, "mdd_concurrency", args.mdd_concurrency)
    _add_param(params, "mdd_stop_on_limit", args.mdd_stop_on_limit)

    for key in (
        "min_pnl_usd",
        "max_pnl_usd",
        "min_volume_usd",
        "max_volume_usd",
        "min_roi_pct",
        "max_roi_pct",
        "min_mdd_usd",
        "max_mdd_usd",
        "min_mdd_pct",
        "max_mdd_pct",
    ):
        _add_param(params, key, getattr(args, key))

    for key, value in args.param or []:
        _add_param(params, key, value)

    return params


def _row_mdd_source(row: Mapping[str, Any]) -> str:
    return str(
        row.get("mdd_accounting_status")
        or row.get("mdd_mark_replay_status")
        or row.get("mdd_method")
        or ""
    )


def _csv_rows(rows: Iterable[Mapping[str, Any]]) -> Iterable[Dict[str, Any]]:
    for row in rows:
        item = {field: row.get(field, "") for field in LEADERBOARD_FIELDS}
        item["mdd_source"] = _row_mdd_source(row)
        yield item


def _open_output(path: Optional[str]) -> tuple[TextIO, bool]:
    if not path or path == "-":
        return sys.stdout, False
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    return output.open("w", encoding="utf-8", newline=""), True


def _log_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    days, remainder = divmod(total, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, secs = divmod(remainder, 60)
    clock = f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{days}d {clock}" if days else clock


def _progress_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _format_rate(count: int, elapsed_seconds: float) -> str:
    if count <= 0 or elapsed_seconds <= 0:
        return "-"
    return f"{count / elapsed_seconds:.2f}/s"


def write_leaderboard_payload(payload: Mapping[str, Any], *, output_format: str, output: Optional[str]) -> None:
    stream, should_close = _open_output(output)
    try:
        if output_format == "json":
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            return

        writer = csv.DictWriter(stream, fieldnames=LEADERBOARD_FIELDS)
        writer.writeheader()
        writer.writerows(_csv_rows(payload.get("rows") or []))
    finally:
        if should_close:
            stream.close()


_UNLIMITED_LIMIT_TOKENS = {"0", "-1", "all", "any", "none", "unlimited", "max"}


def _cli_optional_limit(value: Any, default: int) -> Optional[int]:
    text = str(value if value is not None else "").strip().lower()
    if text in _UNLIMITED_LIMIT_TOKENS:
        return None
    return max(1, _progress_int(value, default))


def _cli_optional_float(value: Any) -> Optional[float]:
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _cli_clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(_progress_int(value, default), maximum))


def _write_streamed_leaderboard_payload(
    payload: Mapping[str, Any],
    rows: Iterable[Mapping[str, Any]],
    *,
    output_format: str,
    output: Optional[str],
) -> None:
    stream, should_close = _open_output(output)
    try:
        if output_format == "csv":
            writer = csv.DictWriter(stream, fieldnames=LEADERBOARD_FIELDS)
            writer.writeheader()
            writer.writerows(_csv_rows(rows))
            return

        stream.write('{"rows":[')
        first = True
        for row in rows:
            if not first:
                stream.write(",")
            json.dump(row, stream, separators=(",", ":"), sort_keys=True)
            first = False
        stream.write("]")
        for key, value in payload.items():
            if key == "rows":
                continue
            stream.write(",")
            json.dump(str(key), stream)
            stream.write(":")
            json.dump(value, stream, separators=(",", ":"), sort_keys=True)
        stream.write("}\n")
    finally:
        if should_close:
            stream.close()


def _disk_backed_mdd_options(args: argparse.Namespace) -> Dict[str, Any]:
    return {
        "mode": str(args.mdd_mode or "fast"),
        "closed_limit": _cli_clamp_int(args.mdd_history_limit, 500, 1, 1000),
        "open_limit": _cli_clamp_int(args.mdd_open_limit, 500, 0, 1000),
        "activity_limit": _cli_clamp_int(args.mdd_activity_limit, 1000, 0, 5000),
        "trade_limit": _cli_clamp_int(args.mdd_trade_limit, 1000, 0, 5000),
        "include_open": True,
        "equity_base_usd": _cli_optional_float(args.equity_base_usd),
        "cache_ttl_seconds": _cli_clamp_int(args.mdd_cache_ttl_seconds, 60, 0, 300),
        "mark_replay_token_limit": _cli_clamp_int(args.mdd_mark_replay_token_limit, 10, 1, 20),
        "mark_replay_point_limit": _cli_clamp_int(args.mdd_mark_replay_point_limit, 5000, 1, 10000),
        "mark_replay_interval": str(args.mdd_mark_replay_interval or "1h"),
        "mark_replay_fidelity": _cli_clamp_int(args.mdd_mark_replay_fidelity, 60, 1, 1440),
        "include_accounting_snapshot": bool(args.mdd_include_accounting),
        "accounting_timeout": _cli_clamp_int(args.mdd_accounting_timeout, 30, 1, 60),
    }


def _leaderboard_filter_values(args: argparse.Namespace) -> Dict[str, Optional[float]]:
    return {
        key: _cli_optional_float(getattr(args, key, ""))
        for key in (
            "min_pnl_usd",
            "max_pnl_usd",
            "min_volume_usd",
            "max_volume_usd",
            "min_roi_pct",
            "max_roi_pct",
            "min_mdd_usd",
            "max_mdd_usd",
            "min_mdd_pct",
            "max_mdd_pct",
        )
    }


def _run_disk_backed_polymarket_leaderboard(args: argparse.Namespace) -> int:
    if args.checkpoint:
        raise ValueError("Use either --checkpoint or --state-db. The SQLite state database is already resumable.")

    started_at = time.monotonic()
    params = build_polymarket_leaderboard_params(args)
    progress_callback = _progress_printer(not args.quiet, started_at=started_at)
    sort = str(params["sort"][0])
    direction = str(params["direction"][0]).upper()
    period = str(params["period"][0])
    category = str(params["category"][0])
    remote_sort = LEADERBOARD_SORTS[sort]
    scan_limit = _cli_optional_limit(args.scanned, 500)
    returned_limit = _cli_optional_limit(args.returned, 100)
    mdd_scan_limit = _cli_optional_limit(args.mdd_scan, 100)
    filters = _leaderboard_filter_values(args)
    mdd_requested = bool(args.compute_mdd) or sort in {"mdd_usd", "mdd_pct"} or any(
        filters[key] is not None for key in ("min_mdd_usd", "max_mdd_usd", "min_mdd_pct", "max_mdd_pct")
    )
    scan_concurrency = _cli_clamp_int(args.scan_concurrency, 6 if args.fast_scan else 1, 1, 12)
    mdd_concurrency = _cli_clamp_int(args.mdd_concurrency, 3 if args.fast_scan else 1, 1, 6)
    retry_attempts = _cli_clamp_int(args.scan_retry_attempts, 5, 1, 50)
    retry_delay = max(0.0, min(_cli_optional_float(args.scan_retry_delay_seconds) or 0.0, 3600.0))
    warnings: List[str] = []
    state_path = Path(args.state_db).expanduser()
    store = LeaderboardStateStore(state_path)
    try:
        store.prepare(
            {"remote_sort": remote_sort, "direction": direction, "period": period, "category": category},
            resume=bool(args.resume),
        )
        state = store.progress()
        if not args.quiet:
            print(
                f"[{_log_timestamp()} pid={os.getpid()} status=starting elapsed=00:00:00 phase=setup] "
                f"Starting disk-backed Polymarket scan state_db={state_path} rows={state['rows']} next_offset={state['next_offset']}.",
                file=sys.stderr,
                flush=True,
            )

        def emit(phase: str, **values: Any) -> None:
            if progress_callback is None:
                return
            scanned = int(values.get("scanned", store.progress()["rows"]))
            if phase == "mdd":
                total = int(values.get("mdd_total", 0))
                attempted = int(values.get("mdd_attempted", 0))
                percent = 50.0 + (50.0 * attempted / total) if total else 100.0
            elif scan_limit is None:
                percent = 0.0
            else:
                percent = min(50.0 if mdd_requested else 100.0, scanned / max(scan_limit, 1) * (50.0 if mdd_requested else 100.0))
            progress_callback(
                {
                    "phase": phase,
                    "percent": percent,
                    "scanned": scanned,
                    "scan_limit": scan_limit,
                    "scan_limit_unlimited": scan_limit is None,
                    "filtered": values.get("filtered", 0),
                    "mdd_attempted": values.get("mdd_attempted", 0),
                    "mdd_computed": values.get("mdd_computed", 0),
                    "mdd_total": values.get("mdd_total", 0),
                    "mdd_scan_limit": mdd_scan_limit,
                    "mdd_scan_limit_unlimited": mdd_scan_limit is None,
                    "wallet": values.get("wallet", ""),
                    "message": values.get("message", ""),
                }
            )

        if not state["scan_complete"] and (scan_limit is None or state["rows"] < scan_limit):
            def save_page(offset: int, _limit: int, page: List[Dict[str, Any]]) -> bool:
                normalized = [normalize_polymarket_leaderboard_row(row, offset + index + 1) for index, row in enumerate(page)]
                return store.record_page(offset, _limit, normalized)

            _fetch_polymarket_leaderboard_scan_rows(
                scan_limit=scan_limit,
                scan_start_offset=int(state["next_offset"]),
                initial_scanned=int(state["rows"]),
                retain_rows=False,
                remote_sort=remote_sort,
                direction=direction,
                period=period,
                category=category,
                scan_concurrency=scan_concurrency,
                scan_retry_attempts=retry_attempts,
                scan_retry_delay_seconds=retry_delay,
                is_cancelled=lambda: False,
                emit_progress=emit,
                warnings=warnings,
                page_callback=save_page,
            )

        if mdd_requested:
            candidate_count = store.candidate_count(filters)
            mdd_total = min(candidate_count, mdd_scan_limit) if mdd_scan_limit is not None else candidate_count
            mdd_options = _disk_backed_mdd_options(args)
            processed = 0
            computed = 0
            rate_limited = False

            def compute(row: Mapping[str, Any]) -> tuple[Mapping[str, Any], Optional[Dict[str, Any]], Optional[BaseException]]:
                wallet = str(row.get("wallet") or "").strip()
                if not wallet:
                    return row, None, ValueError("Leaderboard row does not contain a wallet.")
                try:
                    return row, polymarket_user_mdd_payload(wallet, **mdd_options), None
                except Exception as exc:
                    return row, None, exc

            batch: List[Mapping[str, Any]] = []
            for candidate in store.iter_mdd_candidates(filters, sort=sort, direction=direction, limit=mdd_scan_limit):
                if candidate["mdd_status"] == "done":
                    processed += 1
                    continue
                batch.append(candidate)
                if len(batch) < mdd_concurrency:
                    continue
                futures = []
                with ThreadPoolExecutor(max_workers=len(batch)) as executor:
                    futures = [executor.submit(compute, row) for row in batch]
                    results = [future.result() for future in as_completed(futures)]
                for row, mdd, exc in results:
                    processed += 1
                    if exc is None and mdd is not None:
                        attach_polymarket_mdd_audit_cache(
                            mdd,
                            polymarket_mdd_audit_params(str(row["wallet"]), mdd_options),
                            enabled=bool(args.mdd_persist_cache),
                        )
                        store.set_mdd(int(row["id"]), mdd)
                        computed += 1
                    else:
                        store.set_mdd(int(row["id"]), None, exc)
                        if len(warnings) < 100:
                            warnings.append(f"MDD unavailable for {row.get('wallet')}: {exc}")
                        rate_limited = rate_limited or isinstance(exc, PolymarketRateLimitError)
                    emit("mdd", scanned=store.progress()["rows"], filtered=candidate_count, mdd_attempted=processed, mdd_computed=computed, mdd_total=mdd_total,
                         message=f"Computing MDD {processed}/{mdd_total}.")
                batch = []
                if rate_limited:
                    warnings.append("MDD scan paused after an upstream rate-limit response; rerun with --resume.")
                    break
            if batch and not rate_limited:
                for row, mdd, exc in [compute(row) for row in batch]:
                    processed += 1
                    if exc is None and mdd is not None:
                        attach_polymarket_mdd_audit_cache(
                            mdd, polymarket_mdd_audit_params(str(row["wallet"]), mdd_options), enabled=bool(args.mdd_persist_cache)
                        )
                        store.set_mdd(int(row["id"]), mdd)
                        computed += 1
                    else:
                        store.set_mdd(int(row["id"]), None, exc)
                        if len(warnings) < 100:
                            warnings.append(f"MDD unavailable for {row.get('wallet')}: {exc}")
                    emit("mdd", scanned=store.progress()["rows"], filtered=candidate_count, mdd_attempted=processed, mdd_computed=computed, mdd_total=mdd_total,
                         message=f"Computing MDD {processed}/{mdd_total}.")

        final_state = store.progress()
        qualified = store.result_count(filters, require_mdd=mdd_requested)
        returned = min(qualified, returned_limit) if returned_limit is not None else qualified
        payload: Dict[str, Any] = {
            "counts": {"returned": returned, "filtered": qualified, "scanned": final_state["rows"], "mdd_attempted": final_state["mdd_done"] + final_state["mdd_errors"], "mdd_computed": final_state["mdd_done"]},
            "sort": sort,
            "direction": direction,
            "period": period,
            "category": category,
            "limit": returned_limit,
            "limit_unlimited": returned_limit is None,
            "scan_limit": scan_limit,
            "scan_limit_unlimited": scan_limit is None,
            "mdd_scan_limit": mdd_scan_limit,
            "mdd_scan_limit_unlimited": mdd_scan_limit is None,
            "disk_backed": True,
            "state_db": str(state_path),
            "state": final_state,
            "completion_reason": final_state["stop_reason"] or ("scan_limit_reached" if scan_limit is not None else "unknown"),
            "source_enumeration_complete": final_state["stop_reason"] == "end_of_results",
            "source_scope_note": (
                "Results cover only rows exposed by the public Polymarket leaderboard for the selected period and category; "
                "they do not establish coverage of every Polymarket account."
            ),
            "source": "polymarket_data_api_leaderboard",
            "source_sort": remote_sort,
            "ranking_scope": "computed_from_scanned_public_leaderboard_rows_with_durable_local_state",
            "mdd_available": final_state["mdd_done"] > 0,
            "warnings": warnings,
        }
        _write_streamed_leaderboard_payload(
            payload,
            store.iter_results(filters, require_mdd=mdd_requested, sort=sort, direction=direction, limit=returned_limit),
            output_format=args.format,
            output=args.output,
        )
        if not args.quiet:
            print(
                f"[{_log_timestamp()} pid={os.getpid()} status=done elapsed={_format_elapsed(time.monotonic() - started_at)} phase=done] "
                f"Done: returned={returned} filtered={qualified} scanned={final_state['rows']} mdd_computed={final_state['mdd_done']} completion={payload['completion_reason']} warnings={len(warnings)} state_db={state_path}",
                file=sys.stderr,
            )
        return 0
    finally:
        store.close()


def _load_leaderboard_checkpoint(path: Path) -> tuple[List[Dict[str, Any]], int, int, int]:
    if not path.exists():
        return [], 0, 0, 0

    pages: Dict[int, tuple[int, List[Dict[str, Any]]]] = {}
    ignored = 0
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            raw = line.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                ignored += 1
                continue
            if not isinstance(record, Mapping) or record.get("type") != "leaderboard_page":
                ignored += 1
                continue
            try:
                offset = max(0, int(record.get("offset", 0)))
                limit = max(0, int(record.get("limit", 0)))
            except (TypeError, ValueError):
                ignored += 1
                continue
            raw_rows = record.get("rows")
            if not isinstance(raw_rows, list):
                ignored += 1
                continue
            rows = [dict(row) for row in raw_rows if isinstance(row, Mapping)]
            pages[offset] = (limit, rows)

    rows: List[Dict[str, Any]] = []
    expected_offset = 0
    loaded_pages = 0
    for offset, (_limit, page_rows) in sorted(pages.items()):
        if offset < expected_offset:
            ignored += 1
            continue
        if offset > expected_offset:
            ignored += 1
            break
        rows.extend(page_rows)
        loaded_pages += 1
        expected_offset = offset + len(page_rows)

    return rows, expected_offset, loaded_pages, ignored


class _LeaderboardCheckpointWriter:
    def __init__(self, path: Path, *, fsync_every: int = 20) -> None:
        self.path = path
        self.fsync_every = max(1, int(fsync_every or 20))
        self.written = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a", encoding="utf-8")

    def record(self, offset: int, limit: int, rows: List[Dict[str, Any]]) -> None:
        json.dump(
            {
                "type": "leaderboard_page",
                "offset": int(offset),
                "limit": int(limit),
                "row_count": len(rows),
                "written_at": int(time.time()),
                "rows": rows,
            },
            self.stream,
            separators=(",", ":"),
            sort_keys=True,
        )
        self.stream.write("\n")
        self.stream.flush()
        self.written += 1
        if self.written % self.fsync_every == 0:
            os.fsync(self.stream.fileno())

    def close(self) -> None:
        if self.stream.closed:
            return
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.stream.close()


def _progress_printer(enabled: bool, *, started_at: Optional[float] = None):
    if not enabled:
        return None
    start = started_at if started_at is not None else time.monotonic()
    pid = os.getpid()

    def emit(progress: Dict[str, Any]) -> None:
        elapsed_seconds = max(0.0, time.monotonic() - start)
        phase = str(progress.get("phase") or "scan")
        scanned = _progress_int(progress.get("scanned", 0))
        scan_limit = "unlimited" if progress.get("scan_limit_unlimited") else _progress_int(progress.get("scan_limit", 0))
        mdd_attempted = _progress_int(progress.get("mdd_attempted", 0))
        mdd_total = _progress_int(progress.get("mdd_total", 0))
        percent_value = progress.get("percent")
        percent = ""
        if percent_value is not None:
            try:
                percent = f" percent={float(percent_value):.1f}%"
            except (TypeError, ValueError):
                percent = ""
        eta = ""
        if elapsed_seconds > 0:
            if phase == "mdd" and mdd_total > 0 and 0 < mdd_attempted < mdd_total:
                eta_seconds = (mdd_total - mdd_attempted) / max(mdd_attempted / elapsed_seconds, 0.000001)
                eta = f" eta={_format_elapsed(eta_seconds)}"
            elif scan_limit != "unlimited":
                scan_limit_int = _progress_int(scan_limit)
                if 0 < scanned < scan_limit_int:
                    eta_seconds = (scan_limit_int - scanned) / max(scanned / elapsed_seconds, 0.000001)
                    eta = f" eta={_format_elapsed(eta_seconds)}"
        message = str(progress.get("message") or "").strip()
        if not message:
            message = f"{phase}: scanned {scanned}/{scan_limit}; mdd {mdd_attempted}/{mdd_total}"
        prefix = (
            f"[{_log_timestamp()} pid={pid} status=running elapsed={_format_elapsed(elapsed_seconds)} "
            f"phase={phase}{percent} scanned={scanned}/{scan_limit} scan_rate={_format_rate(scanned, elapsed_seconds)} "
            f"mdd={mdd_attempted}/{mdd_total} mdd_rate={_format_rate(mdd_attempted, elapsed_seconds)}{eta}]"
        )
        print(f"{prefix} {message}", file=sys.stderr, flush=True)

    return emit


def run_polymarket_leaderboard(args: argparse.Namespace) -> int:
    if str(getattr(args, "state_db", "") or "").strip():
        if bool(getattr(args, "resume_on_failure", False)):
            max_restarts = _cli_clamp_int(getattr(args, "resume_max_restarts", 0), 0, 0, 1_000_000)
            base_delay = max(1.0, min(_cli_optional_float(getattr(args, "resume_backoff_seconds", 60)) or 60.0, 3600.0))
            restart = 0
            while True:
                try:
                    return _run_disk_backed_polymarket_leaderboard(args)
                except PolymarketHTTPError as exc:
                    restart += 1
                    if max_restarts and restart > max_restarts:
                        raise
                    delay = min(base_delay * (2 ** min(restart - 1, 6)), 3600.0)
                    if not args.quiet:
                        limit_label = "unlimited" if max_restarts == 0 else str(max_restarts)
                        print(
                            f"[{_log_timestamp()} pid={os.getpid()} status=retrying phase=resume restart={restart}/{limit_label} "
                            f"delay={delay:g}s] Transient Polymarket API failure: {exc}. Resuming durable state after backoff.",
                            file=sys.stderr,
                            flush=True,
                        )
                    args.resume = True
                    time.sleep(delay)
        return _run_disk_backed_polymarket_leaderboard(args)
    if bool(getattr(args, "resume_on_failure", False)):
        raise ValueError("--resume-on-failure requires --state-db so the next attempt has durable scan state.")

    started_at = time.monotonic()
    params = build_polymarket_leaderboard_params(args)
    progress_callback = _progress_printer(not args.quiet, started_at=started_at)
    checkpoint_path_text = str(getattr(args, "checkpoint", "") or "").strip()
    checkpoint_writer: Optional[_LeaderboardCheckpointWriter] = None
    initial_raw_rows: Optional[List[Mapping[str, Any]]] = None
    payload_kwargs: Dict[str, Any] = {"progress_callback": progress_callback}
    if not args.quiet:
        checkpoint_label = checkpoint_path_text or "-"
        print(
            f"[{_log_timestamp()} pid={os.getpid()} status=starting elapsed=00:00:00 phase=setup] "
            f"Starting Polymarket leaderboard scan output={args.output} checkpoint={checkpoint_label}.",
            file=sys.stderr,
            flush=True,
        )
    if checkpoint_path_text:
        checkpoint_path = Path(checkpoint_path_text).expanduser()
        if getattr(args, "resume", False):
            checkpoint_rows, next_offset, loaded_pages, ignored_lines = _load_leaderboard_checkpoint(checkpoint_path)
            initial_raw_rows = checkpoint_rows
            _add_param(params, "scan_start_offset", next_offset)
            if not args.quiet:
                print(
                    f"[{_log_timestamp()} pid={os.getpid()} status=running elapsed={_format_elapsed(time.monotonic() - started_at)} phase=resume] "
                    f"Resuming leaderboard scan from {checkpoint_path}: loaded {len(checkpoint_rows)} rows "
                    f"from {loaded_pages} page(s); next offset {next_offset}; ignored {ignored_lines} line(s).",
                    file=sys.stderr,
                    flush=True,
                )
        else:
            checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            checkpoint_path.write_text("", encoding="utf-8")
        checkpoint_writer = _LeaderboardCheckpointWriter(
            checkpoint_path,
            fsync_every=max(1, int(str(getattr(args, "checkpoint_fsync_every", "20") or "20"))),
        )
        payload_kwargs["initial_raw_rows"] = initial_raw_rows or []
        payload_kwargs["leaderboard_page_callback"] = checkpoint_writer.record

    try:
        payload = polymarket_leaderboard_payload(params, **payload_kwargs)
    finally:
        if checkpoint_writer is not None:
            checkpoint_writer.close()
    write_leaderboard_payload(payload, output_format=args.format, output=args.output)

    counts = payload.get("counts") or {}
    warning_count = len(payload.get("warnings") or [])
    if not args.quiet:
        print(
            "[{timestamp} pid={pid} status=done elapsed={elapsed} phase=done] "
            "Done: returned={returned} filtered={filtered} scanned={scanned} mdd_computed={mdd_computed} warnings={warnings}".format(
                timestamp=_log_timestamp(),
                pid=os.getpid(),
                elapsed=_format_elapsed(time.monotonic() - started_at),
                returned=counts.get("returned", 0),
                filtered=counts.get("filtered", 0),
                scanned=counts.get("scanned", 0),
                mdd_computed=counts.get("mdd_computed", 0),
                warnings=warning_count,
            ),
            file=sys.stderr,
        )
    return 0


def run_polymarket_leaderboard_status(args: argparse.Namespace) -> int:
    state_path = Path(args.state_db).expanduser()
    if not state_path.is_file():
        raise FileNotFoundError(f"Leaderboard state database does not exist: {state_path}")
    store = LeaderboardStateStore(state_path)
    try:
        payload = store.status()
        payload["process"] = _leaderboard_process_status(getattr(args, "pid_file", ""))
        return _write_command_payload(args, payload)
    finally:
        store.close()


def _leaderboard_process_status(pid_file_value: Any) -> Dict[str, Any]:
    text = str(pid_file_value or "").strip()
    if not text:
        return {"checked": False, "status": "not_checked"}
    path = Path(text).expanduser()
    result: Dict[str, Any] = {"checked": True, "pid_file": str(path)}
    if not path.is_file():
        result["status"] = "missing"
        return result
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        result["status"] = "invalid"
        return result
    if pid <= 0:
        result["status"] = "invalid"
        return result
    result["pid"] = pid
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            result["status"] = "running"
            return result
        error_code = ctypes.get_last_error()
        result["status"] = "not_running" if error_code == 87 else "unknown"
        return result
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        result["status"] = "not_running"
    except PermissionError:
        result["status"] = "unknown"
    except OSError:
        result["status"] = "unknown"
    else:
        result["status"] = "running"
    return result


def run_polymarket_leaderboard_export(args: argparse.Namespace) -> int:
    state_path = Path(args.state_db).expanduser()
    if not state_path.is_file():
        raise FileNotFoundError(f"Leaderboard state database does not exist: {state_path}")
    sort = SORT_ALIASES.get(str(args.sort or "roi_pct").strip().lower(), "roi_pct")
    direction = str(args.direction or "DESC").upper()
    returned_limit = _cli_optional_limit(args.returned, 100)
    filters = _leaderboard_filter_values(args)
    require_mdd = bool(args.require_mdd) or sort in {"mdd_usd", "mdd_pct"} or any(
        filters[key] is not None for key in ("min_mdd_usd", "max_mdd_usd", "min_mdd_pct", "max_mdd_pct")
    )
    store = LeaderboardStateStore(state_path)
    try:
        state = store.progress()
        qualified = store.result_count(filters, require_mdd=require_mdd)
        returned = min(qualified, returned_limit) if returned_limit is not None else qualified
        completion_reason = state["stop_reason"] or ("in_progress" if not state["scan_complete"] else "unknown")
        payload: Dict[str, Any] = {
            "counts": {
                "returned": returned,
                "filtered": qualified,
                "scanned": state["rows"],
                "mdd_computed": state["mdd_done"],
                "mdd_errors": state["mdd_errors"],
                "mdd_pending": state["mdd_pending"],
            },
            "sort": sort,
            "direction": direction,
            "limit": returned_limit,
            "limit_unlimited": returned_limit is None,
            "require_mdd": require_mdd,
            "partial": not state["scan_complete"] or (require_mdd and state["mdd_pending"] > 0),
            "completion_reason": completion_reason,
            "state": state,
            "state_db": str(state_path),
            "exported_at": int(time.time()),
            "source": "polymarket_data_api_leaderboard_durable_state",
            "ranking_scope": "computed_from_currently_saved_public_leaderboard_rows",
            "source_scope_note": (
                "This export contains only rows currently saved from the public Polymarket leaderboard for the selected period and category; "
                "it does not establish coverage of every Polymarket account."
            ),
        }
        _write_streamed_leaderboard_payload(
            payload,
            store.iter_results(filters, require_mdd=require_mdd, sort=sort, direction=direction, limit=returned_limit),
            output_format=args.format,
            output=args.output,
        )
        return 0
    finally:
        store.close()


def run_health(args: argparse.Namespace) -> int:
    return _write_command_payload(args, health_payload(_config_path(args), Path(args.frontend_dir).expanduser()))


def _nearest_existing_parent(path: Path) -> Path:
    current = path.expanduser()
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def doctor_payload(
    config_path: Path = DEFAULT_CONFIG_PATH,
    frontend_dir: Path = DEFAULT_FRONTEND_DIR,
    *,
    check_latest: bool = False,
) -> Dict[str, Any]:
    """Build a redacted, read-only operational readiness report for CLI deployments."""
    config_path = config_path.expanduser()
    frontend_dir = frontend_dir.expanduser()
    checks: List[Dict[str, Any]] = []
    cfg = None

    try:
        cfg = load_config(config_path)
        checks.append(
            {
                "name": "configuration",
                "status": "pass",
                "message": "Configuration loaded safely.",
                "path": str(config_path),
            }
        )
    except ConfigLoadError as exc:
        checks.append(
            {
                "name": "configuration",
                "status": "fail",
                "message": str(exc),
                "path": str(config_path),
            }
        )

    storage_parent = _nearest_existing_parent(config_path.parent)
    if storage_parent.exists() and os.access(storage_parent, os.W_OK):
        checks.append(
            {
                "name": "configuration_storage",
                "status": "pass",
                "message": "Configuration storage parent is writable.",
                "path": str(storage_parent),
            }
        )
    else:
        checks.append(
            {
                "name": "configuration_storage",
                "status": "fail",
                "message": "Configuration storage parent is not writable.",
                "path": str(storage_parent),
            }
        )

    dependencies = _dependency_rows(latest=check_latest)
    missing = [row["package"] for row in dependencies if row["status"] == "missing"]
    outdated = [row["package"] for row in dependencies if row["status"] == "outdated"]
    dependency_status = "fail" if missing else ("warn" if outdated else "pass")
    checks.append(
        {
            "name": "dependencies",
            "status": dependency_status,
            "message": (
                f"Missing dependencies: {', '.join(missing)}."
                if missing
                else f"Outdated dependencies: {', '.join(outdated)}."
                if outdated
                else "Required dependencies are installed."
            ),
            "checked_latest": check_latest,
            "missing": missing,
            "outdated": outdated,
        }
    )

    health = health_payload(config_path, frontend_dir)
    frontend_available = bool(health.get("frontend_build_available"))
    checks.append(
        {
            "name": "frontend_build",
            "status": "pass" if frontend_available else "warn",
            "message": "React production build is available." if frontend_available else "React production build is unavailable; the Tkinter fallback remains available.",
            "path": str(frontend_dir),
        }
    )

    if cfg is not None:
        try:
            live_safety = live_safety_payload(cfg, _registry())
            armed = live_safety.get("status") == "armed"
            checks.append(
                {
                    "name": "live_trading_safety",
                    "status": "warn" if armed else "pass",
                    "message": "Live trading is armed; confirm operational monitoring is active." if armed else "Live trading is not armed.",
                    "selected_market_id": live_safety.get("selected_market_id"),
                    "blockers": list(live_safety.get("blockers") or []),
                }
            )
        except Exception as exc:
            checks.append(
                {
                    "name": "live_trading_safety",
                    "status": "fail",
                    "message": f"Live-safety inspection failed: {exc}",
                }
            )

    counts = {status: sum(1 for check in checks if check["status"] == status) for status in ("pass", "warn", "fail")}
    status = "error" if counts["fail"] else "warning" if counts["warn"] else "ok"
    return {
        "status": status,
        "generated_at": int(time.time()),
        "config_path": str(config_path),
        "frontend_dir": str(frontend_dir),
        "checks": checks,
        "counts": counts,
        "source": "market_sentinel_cli_doctor_v1",
        "scope_note": "This report validates local configuration and runtime prerequisites only; it does not establish release approval, native-platform certification, or funded-market verification.",
    }


def run_doctor(args: argparse.Namespace) -> int:
    payload = doctor_payload(
        _config_path(args),
        Path(args.frontend_dir),
        check_latest=bool(args.check_latest),
    )
    _write_command_payload(args, payload)
    if payload["counts"]["fail"] or (args.strict and payload["counts"]["warn"]):
        return 1
    return 0


def run_state(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args)
    payload = app_state_payload(
        cfg,
        config_path=_config_path(args),
        frontend_dir=Path(args.frontend_dir).expanduser(),
        registry=_registry(),
    )
    return _write_command_payload(args, payload)


def run_config_show(args: argparse.Namespace) -> int:
    return _write_command_payload(args, config_payload(_load_cfg(args)))


def run_config_set(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args)
    payload = _json_arg(args.json)
    _put_optional(payload, "selected_market_id", args.market)
    _put_optional(payload, "theme", args.theme)
    _put_optional(payload, "ui_design", args.design)
    apply_config_patch(cfg, payload)
    _save_cfg(args, cfg)
    return _write_command_payload(args, config_payload(cfg))


def run_markets_list(args: argparse.Namespace) -> int:
    return _write_command_payload(args, markets_payload(_load_cfg(args), _registry()))


def run_market_set(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args)
    payload = _json_arg(args.json)
    _put_optional(payload, "enabled", args.enabled)
    _put_optional(payload, "live_trading_enabled", args.live_trading_enabled)
    _put_optional(payload, "live_trading_confirmed", args.live_trading_confirmed)
    _put_optional(payload, "live_trading_kill_switch", args.live_trading_kill_switch)
    _put_optional(payload, "live_trading_max_size", args.live_trading_max_size)
    _put_optional(payload, "live_trading_max_notional", args.live_trading_max_notional)
    if args.setting:
        settings = dict(payload.get("settings") or {})
        _merge_kv(settings, args.setting)
        payload["settings"] = settings
    apply_market_patch(cfg, args.market_id, payload)
    _save_cfg(args, cfg)
    return _write_command_payload(args, markets_payload(cfg, _registry()))


def _market_read_context(args: argparse.Namespace, feature: str):
    """Load an enabled market adapter for a headless read operation.

    Read commands deliberately share the same enablement and adapter
    configuration path as the web API.  This keeps the CLI from accidentally
    bypassing local market-disable or safety settings while still allowing
    every documented adapter read to be used without a GUI.
    """

    cfg = _load_cfg(args)
    market_id = str(getattr(args, "market", None) or cfg.selected_market_id or "").strip().lower()
    if not market_id:
        raise ValueError("market is required (pass --market or select one in config).")
    require_market_enabled(cfg, market_id, feature)
    registry = _registry()
    return cfg, market_id, adapter_for_market(cfg, market_id, registry)


def _cli_history_float(value: Any, label: str) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number.") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number.")
    return number


def run_market_events(args: argparse.Namespace) -> int:
    _cfg, market_id, adapter = _market_read_context(args, "event listing")
    query = str(args.query or "")
    limit = _cli_clamp_int(args.limit, 50, 1, 1000)
    events = adapter.list_events(query, limit=limit)
    return _write_command_payload(
        args,
        {
            "market_id": market_id,
            "query": query,
            "limit": limit,
            "events": [serialize_market_event(event) for event in events],
        },
    )


def run_market_contracts(args: argparse.Namespace) -> int:
    _cfg, market_id, adapter = _market_read_context(args, "contract listing")
    contracts = adapter.list_contracts(str(args.event_id))
    return _write_command_payload(
        args,
        {
            "market_id": market_id,
            "event_id": str(args.event_id),
            "contracts": [serialize_market_contract(contract) for contract in contracts],
        },
    )


def run_market_price(args: argparse.Namespace) -> int:
    _cfg, market_id, adapter = _market_read_context(args, "price reading")
    snapshot = adapter.get_price(str(args.contract))
    return _write_command_payload(
        args,
        {
            "market_id": market_id,
            "contract_id": str(args.contract),
            "price": serialize_price_snapshot(snapshot),
        },
    )


def run_market_orderbook(args: argparse.Namespace) -> int:
    _cfg, market_id, adapter = _market_read_context(args, "orderbook reading")
    orderbook = adapter.get_orderbook(str(args.contract))
    return _write_command_payload(
        args,
        {
            "market_id": market_id,
            "contract_id": str(args.contract),
            "orderbook": serialize_orderbook(orderbook),
        },
    )


def run_market_trades(args: argparse.Namespace) -> int:
    _cfg, market_id, adapter = _market_read_context(args, "trade history")
    limit = _cli_clamp_int(args.limit, 50, 1, 1000)
    before = _cli_history_float(args.before, "before")
    after = _cli_history_float(args.after, "after")
    trades = adapter.list_trades(str(args.contract), limit=limit, before=before, after=after)
    return _write_command_payload(
        args,
        {
            "market_id": market_id,
            "contract_id": str(args.contract),
            "limit": limit,
            "before": before,
            "after": after,
            "trades": [serialize_market_trade(trade) for trade in trades],
        },
    )


def run_market_candles(args: argparse.Namespace) -> int:
    _cfg, market_id, adapter = _market_read_context(args, "candle history")
    resolution = str(args.resolution or "1h").strip()
    if not resolution:
        raise ValueError("resolution cannot be empty.")
    from_timestamp = _cli_history_float(args.from_timestamp, "from")
    to_timestamp = _cli_history_float(args.to_timestamp, "to")
    candles = adapter.list_candles(
        str(args.contract),
        resolution=resolution,
        from_timestamp=from_timestamp,
        to_timestamp=to_timestamp,
    )
    return _write_command_payload(
        args,
        {
            "market_id": market_id,
            "contract_id": str(args.contract),
            "resolution": resolution,
            "from": from_timestamp,
            "to": to_timestamp,
            "candles": [serialize_market_candle(candle) for candle in candles],
        },
    )


GEMINI_ACCOUNT_OPERATIONS = (
    "active_orders",
    "order_history",
    "positions",
    "settled_positions",
    "volume_metrics",
)
KALSHI_ACCOUNT_OPERATIONS = (
    "active_orders",
    "order_history",
    "fills",
    "positions",
    "settlements",
    "balance",
    "queue_positions",
)
LIMITLESS_ACCOUNT_OPERATIONS = ("positions", "account_history", "user_orders")
XMARKET_ACCOUNT_OPERATIONS = ("positions", "user_orders", "market_orders")
SMARKETS_ACCOUNT_OPERATIONS = ("order_history", "account")
PROBABLE_ACCOUNT_OPERATIONS = ("open_orders", "order")
OPINION_ACCOUNT_OPERATIONS = ("order_history", "order_detail", "positions")
OPINION_ORDER_MANAGEMENT_OPERATIONS = (
    "cancel_order",
    "batch_cancel_orders",
    "cancel_all_orders",
)
BETFAIR_ACCOUNT_OPERATIONS = (
    "active_orders",
    "cleared_orders",
    "funds",
    "account",
    "statement",
    "currency_rates",
)
MATCHBOOK_ACCOUNT_OPERATIONS = (
    "settled_bets",
    "current_bets",
    "current_offers",
    "balance",
    "account",
)
HYPERLIQUID_ACCOUNT_OPERATIONS = (
    "active_orders",
    "order_history",
    "positions",
    "spot_balances",
    "portfolio",
    "subaccounts",
)
POLYMARKET_ACCOUNT_OPERATIONS = ("active_orders", "order_detail", "fills")
PREDICT_FUN_ACCOUNT_OPERATIONS = (
    "account",
    "active_orders",
    "order_detail",
    "account_activity",
    "positions",
    "positions_by_address",
)
IBKR_ACCOUNT_OPERATIONS = ("orders", "order_status")
MANIFOLD_ACCOUNT_OPERATIONS = ("account", "active_orders", "order_history")
PROPHET_EXCHANGE_ACCOUNT_OPERATIONS = ("balance", "transactions")
AZURO_ACCOUNT_OPERATIONS = ("bet_history",)
MYRIAD_ACCOUNT_OPERATIONS = ("account_activity", "portfolio", "market_positions")
MARKET_ACCOUNT_OPERATIONS = tuple(
    dict.fromkeys(
        GEMINI_ACCOUNT_OPERATIONS
        + KALSHI_ACCOUNT_OPERATIONS
        + LIMITLESS_ACCOUNT_OPERATIONS
        + XMARKET_ACCOUNT_OPERATIONS
        + SMARKETS_ACCOUNT_OPERATIONS
        + PROBABLE_ACCOUNT_OPERATIONS
        + OPINION_ACCOUNT_OPERATIONS
        + BETFAIR_ACCOUNT_OPERATIONS
        + MATCHBOOK_ACCOUNT_OPERATIONS
        + HYPERLIQUID_ACCOUNT_OPERATIONS
        + POLYMARKET_ACCOUNT_OPERATIONS
        + PREDICT_FUN_ACCOUNT_OPERATIONS
        + IBKR_ACCOUNT_OPERATIONS
        + MANIFOLD_ACCOUNT_OPERATIONS
        + PROPHET_EXCHANGE_ACCOUNT_OPERATIONS
        + AZURO_ACCOUNT_OPERATIONS
        + MYRIAD_ACCOUNT_OPERATIONS
    )
)


def run_market_account(args: argparse.Namespace) -> int:
    """Read one adapter's explicitly documented authenticated account feed."""

    _cfg, market_id, adapter = _market_read_context(args, "account recovery")
    operation = str(args.operation or "").strip().lower()
    kwargs: Dict[str, Any] = {}
    if market_id == "limitless_exchange":
        kwargs = {
            "on_behalf_of": str(getattr(args, "on_behalf_of", "") or "").strip() or None,
        }
        if operation == "user_orders":
            kwargs["market_slug"] = str(getattr(args, "market_slug", "") or "").strip()
    elif market_id == "xmarket":
        market_id_filter = str(getattr(args, "account_market_id", "") or "").strip()
        if not market_id_filter and args.contract:
            market_id_filter = str(args.contract).split(":", 1)[0].strip()
        kwargs = {
            "status": str(getattr(args, "status", "") or "").strip().lower() or None,
            "page": _cli_clamp_int(getattr(args, "page", "1"), 1, 1, 10000),
            "limit": _cli_clamp_int(getattr(args, "limit", None), 50, 1, 1000),
        }
        if operation == "market_orders":
            kwargs["market_id"] = market_id_filter
    elif market_id == "smarkets":
        kwargs = {
            "status": str(getattr(args, "status", "") or "").strip().lower(),
            "limit": _cli_clamp_int(getattr(args, "limit", None), 50, 1, 1000),
        }
    elif market_id == "probable":
        kwargs = {
            "page": _cli_clamp_int(getattr(args, "page", "1"), 1, 1, 10000),
            "limit": _cli_clamp_int(getattr(args, "limit", None), 50, 1, 50),
            "event_id": str(getattr(args, "account_event_id", "") or "").strip() or None,
            "token_ids": str(getattr(args, "token_ids", "") or "").strip() or None,
        }
        if operation == "order":
            kwargs.update(
                {
                    "order_id": str(args.order_id or "").strip(),
                    "token_id": str(getattr(args, "token_id", "") or "").strip(),
                    "client_order_id": str(getattr(args, "client_order_id", "") or "").strip() or None,
                }
            )
    elif market_id == "kalshi":
        ticker = str(args.ticker or "").strip()
        if not ticker and args.contract:
            ticker = str(args.contract).split(":", 1)[0].strip()
        subaccount = (
            _cli_clamp_int(args.subaccount, 0, 0, 63)
            if args.subaccount not in (None, "")
            else None
        )
        kwargs.update(
            {
                "ticker": ticker,
                "event_ticker": str(args.event_ticker or "").strip(),
                "limit": _cli_clamp_int(args.limit, 100, 1, 1000),
                "cursor": str(args.cursor or "").strip(),
                "min_timestamp": _cli_history_float(args.from_timestamp, "from"),
                "max_timestamp": _cli_history_float(args.to_timestamp, "to"),
                "subaccount": subaccount,
            }
        )
        if operation == "order_history":
            kwargs.update(
                {
                    "status": str(args.status or "executed").strip().lower(),
                    "historical": bool(args.historical),
                }
            )
        elif operation == "fills":
            kwargs.update(
                {
                    "order_id": str(args.order_id or "").strip(),
                    "historical": bool(args.historical),
                }
            )
        elif operation == "positions":
            kwargs["count_filter"] = str(args.count_filter or "").strip()
        elif operation == "queue_positions":
            kwargs = {
                "ticker": ticker,
                "event_ticker": str(args.event_ticker or "").strip(),
                "subaccount": subaccount,
            }
    elif market_id == "opinion_labs":
        if operation == "order_detail":
            kwargs = {"order_id": str(args.order_id or "").strip()}
        else:
            kwargs = {
                "page": _cli_clamp_int(getattr(args, "page", "1"), 1, 1, 10000),
                "limit": _cli_clamp_int(args.limit, 10, 1, 20),
                "market_id": str(getattr(args, "account_market_id", "") or "").strip(),
                "chain_id": str(getattr(args, "chain_id", "") or "").strip(),
            }
            if operation == "order_history":
                kwargs["status"] = str(args.status or "").strip()
    elif market_id == "polymarket":
        kwargs = {
            "market_id": str(getattr(args, "account_market_id", "") or "").strip(),
            "contract_id": str(args.contract or "").strip(),
            "next_cursor": str(args.cursor or "").strip(),
        }
        if operation == "order_detail":
            kwargs = {"order_id": str(args.order_id or "").strip()}
        elif operation == "fills":
            kwargs.update(
                {
                    "trade_id": str(getattr(args, "trade_id", "") or "").strip(),
                    "limit": _cli_clamp_int(args.limit, 100, 1, 500),
                    "before": _cli_history_float(getattr(args, "before", None), "before"),
                    "after": _cli_history_float(getattr(args, "after", None), "after"),
                }
            )
    elif market_id == "betfair_exchange":
        if operation == "funds":
            kwargs = {"wallet": str(getattr(args, "wallet", "") or "").strip()}
        elif operation == "account":
            kwargs = {}
        elif operation == "statement":
            kwargs = {
                "locale": str(getattr(args, "locale", "en") or "en").strip(),
                "limit": _cli_clamp_int(args.limit, 100, 1, 1000),
                "offset": _cli_clamp_int(args.offset, 0, 0, 100000),
                "include_item": not bool(getattr(args, "exclude_item", False)),
                "wallet": str(getattr(args, "wallet", "") or "").strip(),
                "from_timestamp": _cli_history_float(args.from_timestamp, "from"),
                "to_timestamp": _cli_history_float(args.to_timestamp, "to"),
            }
        elif operation == "currency_rates":
            kwargs = {"from_currency": str(getattr(args, "from_currency", "") or "").strip()}
        elif operation in {"active_orders", "cleared_orders"}:
            market_id_filter = str(getattr(args, "account_market_id", "") or "").strip()
            runner_id = str(getattr(args, "runner_id", "") or "").strip()
            if not market_id_filter and args.contract:
                parts = str(args.contract).split(":", 1)
                market_id_filter = parts[0].strip()
                if len(parts) == 2 and not runner_id:
                    runner_id = parts[1].strip()
            if operation == "active_orders":
                kwargs = {
                    "market_id": market_id_filter,
                    "contract_id": str(args.contract or "").strip(),
                    "status": str(args.status or "").strip(),
                    "order_by": str(getattr(args, "order_by", "BY_MATCH_TIME") or "BY_MATCH_TIME").strip(),
                    "sort_dir": str(getattr(args, "sort_dir", "EARLIEST_TO_LATEST") or "EARLIEST_TO_LATEST").strip(),
                    "include_item_description": bool(getattr(args, "include_item_description", False)),
                    "limit": _cli_clamp_int(args.limit, 100, 1, 1000),
                    "offset": _cli_clamp_int(args.offset, 0, 0, 100000),
                    "from_timestamp": _cli_history_float(args.from_timestamp, "from"),
                    "to_timestamp": _cli_history_float(args.to_timestamp, "to"),
                }
            else:
                kwargs = {
                    "bet_status": str(args.status or "SETTLED").strip(),
                    "market_id": market_id_filter,
                    "event_type_id": str(getattr(args, "event_type_id", "") or "").strip(),
                    "event_id": str(getattr(args, "account_event_id", "") or "").strip(),
                    "runner_id": runner_id,
                    "bet_id": str(getattr(args, "bet_id", "") or "").strip(),
                    "group_by": str(getattr(args, "group_by", "BET") or "BET").strip(),
                    "include_item_description": bool(getattr(args, "include_item_description", False)),
                    "limit": _cli_clamp_int(args.limit, 100, 1, 1000),
                    "offset": _cli_clamp_int(args.offset, 0, 0, 100000),
                    "from_timestamp": _cli_history_float(args.from_timestamp, "from"),
                    "to_timestamp": _cli_history_float(args.to_timestamp, "to"),
                }
    elif market_id == "matchbook":
        if operation in {"balance", "account"}:
            kwargs = {}
        elif operation in {"settled_bets", "current_bets"}:
            kwargs = {
                "offset": _cli_clamp_int(args.offset, 0, 0, 100000),
                "limit": _cli_clamp_int(args.limit, 50, 1, 1000),
                "sport_id": str(getattr(args, "account_sport_id", "") or "").strip(),
                "event_id": str(getattr(args, "account_event_id", "") or "").strip(),
                "market_id": str(getattr(args, "account_market_id", "") or "").strip(),
                "odds_type": str(getattr(args, "account_odds_type", "DECIMAL") or "DECIMAL").strip(),
                "from_timestamp": _cli_history_float(args.from_timestamp, "from"),
                "to_timestamp": _cli_history_float(args.to_timestamp, "to"),
            }
        elif operation == "current_offers":
            raw_interval = str(getattr(args, "account_interval", "") or "").strip()
            kwargs = {
                "offset": _cli_clamp_int(args.offset, 0, 0, 100000),
                "limit": _cli_clamp_int(args.limit, 20, 1, 1000),
                "sport_id": str(getattr(args, "account_sport_id", "") or "").strip(),
                "event_id": str(getattr(args, "account_event_id", "") or "").strip(),
                "market_id": str(getattr(args, "account_market_id", "") or "").strip(),
                "runner_id": str(getattr(args, "runner_id", "") or "").strip(),
                "side": str(getattr(args, "account_side", "") or "").strip(),
                "status": str(getattr(args, "account_offer_status", "") or "").strip(),
                "interval": _cli_clamp_int(raw_interval, 0, 0, 2147483647) if raw_interval else None,
                "include_edits": bool(getattr(args, "account_include_edits", False)),
                "cancellation_reason": str(getattr(args, "account_cancellation_reason", "") or "").strip(),
                "aggregation_type": str(getattr(args, "account_aggregation_type", "none") or "none").strip(),
                "odds_type": str(getattr(args, "account_odds_type", "DECIMAL") or "DECIMAL").strip(),
            }
    elif market_id == "hyperliquid":
        if operation in {"active_orders", "positions"}:
            kwargs["dex"] = str(getattr(args, "dex", "") or "").strip()
        elif operation == "order_history":
            kwargs["limit"] = _cli_clamp_int(args.limit, 2000, 1, 2000)
    elif market_id == "predict_fun":
        if operation == "order_detail":
            kwargs = {"order_id": str(getattr(args, "order_id", "") or "").strip()}
        elif operation == "positions_by_address":
            kwargs = {
                "address": str(getattr(args, "wallet", "") or "").strip(),
                "limit": _cli_clamp_int(getattr(args, "limit", None), 50, 1, 100),
                "cursor": str(getattr(args, "cursor", "") or "").strip(),
                "market_id": str(getattr(args, "account_market_id", "") or "").strip(),
                "is_resolved": getattr(args, "is_resolved", None),
                "sort": str(getattr(args, "sort", "") or "").strip(),
            }
        elif operation == "account":
            kwargs = {}
        else:
            kwargs = {
                "limit": _cli_clamp_int(getattr(args, "limit", None), 50, 1, 100),
                "cursor": str(getattr(args, "cursor", "") or "").strip(),
                "market_id": str(getattr(args, "account_market_id", "") or "").strip(),
                "status": str(getattr(args, "status", "") or "").strip(),
                "is_resolved": getattr(args, "is_resolved", None),
                "sort": str(getattr(args, "sort", "") or "").strip(),
            }
            if operation == "account_activity":
                kwargs["event_types"] = str(getattr(args, "event_types", "") or "").strip()
    elif market_id == "manifold":
        if operation == "account":
            kwargs = {}
        else:
            kwargs = {
                "contract_id": str(args.contract or "").strip() or None,
                "limit": _cli_clamp_int(args.limit, 50, 1, 1000),
                "before": str(getattr(args, "before", "") or "").strip() or None,
                "after": str(getattr(args, "after", "") or "").strip() or None,
                "before_time": _cli_history_float(getattr(args, "to_timestamp", None), "to"),
                "after_time": _cli_history_float(getattr(args, "from_timestamp", None), "from"),
            }
    elif market_id == "prophet_exchange":
        if operation == "balance":
            kwargs = {}
        else:
            kwargs = {
                "cursor": str(getattr(args, "cursor", "") or "").strip() or None,
                "limit": _cli_clamp_int(getattr(args, "limit", None), 10, 1, 500),
            }
    elif market_id == "azuro":
        kwargs = {
            "wallet": str(getattr(args, "wallet", "") or "").strip(),
            "limit": _cli_clamp_int(getattr(args, "limit", None), 100, 1, 1000),
            "offset": _cli_clamp_int(getattr(args, "offset", "0"), 0, 0, 1_000_000),
        }
    elif market_id == "myriad_markets":
        kwargs = {
            "wallet": str(getattr(args, "wallet", "") or "").strip(),
            "limit": _cli_clamp_int(getattr(args, "limit", None), 25, 1, 100),
        }
        if operation in {"portfolio", "market_positions"}:
            kwargs.update(
                {
                    "page": _cli_clamp_int(getattr(args, "page", None), 1, 1, 10_000),
                    "trading_model": str(getattr(args, "trading_model", "all") or "all").strip(),
                    "min_shares": str(getattr(args, "min_shares", "") or "").strip() or None,
                    "market_slug": str(getattr(args, "market_slug", "") or "").strip() or None,
                    "market_id": str(getattr(args, "account_market_id", "") or "").strip() or None,
                    "network_id": str(getattr(args, "network_id", "") or "").strip() or None,
                    "token_address": str(getattr(args, "token_address", "") or "").strip() or None,
                    "status": str(getattr(args, "status", "") or "").strip() or None,
                    "keyword": str(getattr(args, "keyword", "") or "").strip() or None,
                    "sort": str(getattr(args, "sort", "") or "").strip() or None,
                    "sort_by": str(getattr(args, "sort_by", "") or "").strip() or None,
                    "exclude_history": bool(getattr(args, "exclude_history", False)),
                    "group_by_event": bool(getattr(args, "group_by_event", False)),
                }
            )
            if operation == "market_positions":
                kwargs.update(
                    {
                        "state": str(getattr(args, "state", "") or "").strip() or None,
                        "topics": str(getattr(args, "topics", "") or "").strip() or None,
                        "market_ids": str(getattr(args, "market_ids", "") or "").strip() or None,
                    }
                )
    elif market_id in {"ibkr_forecasttrader", "forecastex", "cme_prediction_markets"}:
        kwargs = {
            "filters": str(getattr(args, "status", "") or "").strip(),
            "force": bool(getattr(args, "historical", False)),
        }
        if operation == "order_status":
            kwargs["order_id"] = str(getattr(args, "order_id", "") or "").strip()
    else:
        if operation in {"active_orders", "order_history"}:
            kwargs.update(
                {
                    "contract_id": str(args.contract or "").strip() or None,
                    "limit": _cli_clamp_int(args.limit, 50, 1, 1000),
                    "offset": _cli_clamp_int(args.offset, 0, 0, 100000),
                }
            )
        if operation == "order_history":
            kwargs.update(
                {
                    "status": str(args.status or "filled").strip().lower(),
                    "from_timestamp": _cli_history_float(args.from_timestamp, "from"),
                    "to_timestamp": _cli_history_float(args.to_timestamp, "to"),
                }
            )
        elif operation == "positions":
            kwargs.update(
                {
                    "event_ticker": str(args.event_ticker or "").strip(),
                    "limit": (
                        _cli_clamp_int(args.limit, 100, 1, 1000)
                        if args.limit not in (None, "")
                        else None
                    ),
                    "offset": _cli_clamp_int(args.offset, 0, 0, 100000),
                    "sort": str(args.sort or "").strip() or None,
                }
            )
        elif operation == "settled_positions":
            kwargs.update(
                {
                    "event_ticker": str(args.event_ticker or "").strip(),
                    "limit": _cli_clamp_int(args.limit, 1000, 1, 1000),
                    "offset": _cli_clamp_int(args.offset, 0, 0, 100000),
                    "sort": str(args.sort or "-date").strip(),
                    "search": str(args.search or "").strip(),
                    "category": str(args.category or "").strip(),
                    "with_cash_outs": bool(args.with_cash_outs),
                }
            )
        elif operation == "volume_metrics":
            kwargs.update(
                {
                    "event_ticker": str(args.event_ticker or "").strip(),
                    "start_timestamp": _cli_history_float(args.from_timestamp, "from"),
                    "end_timestamp": _cli_history_float(args.to_timestamp, "to"),
                }
            )
    data = adapter.account_recovery(operation, **kwargs)
    return _write_command_payload(
        args,
        {
            "market_id": market_id,
            "operation": operation,
            "parameters": kwargs,
            "data": data,
        },
    )


BETFAIR_ORDER_MANAGEMENT_OPERATIONS = ("cancel_orders", "update_orders", "replace_orders")
KALSHI_ORDER_MANAGEMENT_OPERATIONS = ("cancel_order", "batch_cancel_orders", "amend_order", "decrease_order")
POLYMARKET_ORDER_MANAGEMENT_OPERATIONS = (
    "cancel_order",
    "cancel_orders",
    "cancel_all_orders",
    "cancel_market_orders",
)
GEMINI_ORDER_MANAGEMENT_OPERATIONS = ("cancel_order", "batch_cancel_orders")
MATCHBOOK_ORDER_MANAGEMENT_OPERATIONS = (
    "cancel_offer",
    "cancel_offers",
    "cancel_all_offers",
    "edit_offer",
    "edit_offers",
)
MYRIAD_ORDER_MANAGEMENT_OPERATIONS = (
    "cancel_order",
    "batch_cancel_orders",
    "cancel_all_orders",
    "batch_modify_orders",
)
LIMITLESS_ORDER_MANAGEMENT_OPERATIONS = ("cancel_order", "batch_cancel_orders", "cancel_all_orders")
SMARKETS_ORDER_MANAGEMENT_OPERATIONS = ("cancel_order", "cancel_orders")
PROBABLE_ORDER_MANAGEMENT_OPERATIONS = ("cancel_order", "cancel_orders", "cancel_all_orders")
HYPERLIQUID_ORDER_MANAGEMENT_OPERATIONS = (
    "cancel_order",
    "cancel_orders",
    "cancel_by_cloid",
    "modify_order",
    "batch_modify_orders",
    "schedule_cancel",
)
PREDICT_FUN_ORDER_MANAGEMENT_OPERATIONS = ("remove_orders", "remove_orders_by_hash")
XMARKET_ORDER_MANAGEMENT_OPERATIONS = ("batch_create_orders", "batch_cancel_orders")
IBKR_ORDER_MANAGEMENT_OPERATIONS = ("cancel_order", "cancel_all_orders", "modify_order")
MANIFOLD_ORDER_MANAGEMENT_OPERATIONS = ("cancel_order",)
PROPHET_EXCHANGE_ORDER_MANAGEMENT_OPERATIONS = ("cancel_order", "cancel_orders")
MARKET_ORDER_MANAGEMENT_OPERATIONS = tuple(
    dict.fromkeys(
        BETFAIR_ORDER_MANAGEMENT_OPERATIONS
        + KALSHI_ORDER_MANAGEMENT_OPERATIONS
        + POLYMARKET_ORDER_MANAGEMENT_OPERATIONS
        + GEMINI_ORDER_MANAGEMENT_OPERATIONS
        + MATCHBOOK_ORDER_MANAGEMENT_OPERATIONS
        + MYRIAD_ORDER_MANAGEMENT_OPERATIONS
        + OPINION_ORDER_MANAGEMENT_OPERATIONS
        + LIMITLESS_ORDER_MANAGEMENT_OPERATIONS
        + SMARKETS_ORDER_MANAGEMENT_OPERATIONS
        + PROBABLE_ORDER_MANAGEMENT_OPERATIONS
        + HYPERLIQUID_ORDER_MANAGEMENT_OPERATIONS
        + PREDICT_FUN_ORDER_MANAGEMENT_OPERATIONS
        + XMARKET_ORDER_MANAGEMENT_OPERATIONS
        + IBKR_ORDER_MANAGEMENT_OPERATIONS
        + MANIFOLD_ORDER_MANAGEMENT_OPERATIONS
        + PROPHET_EXCHANGE_ORDER_MANAGEMENT_OPERATIONS
    )
)


def run_market_order_management(args: argparse.Namespace) -> int:
    """Run a documented live order-management mutation through an adapter."""

    _cfg, market_id, adapter = _market_read_context(args, "order management")
    operation = str(args.operation or "").strip().lower()
    payload = _json_arg(getattr(args, "json", None))
    exchange_market_id = str(getattr(args, "exchange_market_id", "") or "").strip()
    if exchange_market_id:
        payload["market_id"] = exchange_market_id
    polymarket_market_id = str(getattr(args, "market_id", "") or "").strip()
    if polymarket_market_id:
        payload["market_id"] = polymarket_market_id
    if market_id == "polymarket":
        _put_optional(payload, "asset_id", getattr(args, "asset_id", None))
    market_slug = str(getattr(args, "market_slug", "") or "").strip()
    if market_slug:
        payload["market_slug"] = market_slug
    instructions_value = getattr(args, "instructions", None)
    if instructions_value:
        raw = str(instructions_value)
        if raw.startswith("@"):
            raw = Path(raw[1:]).expanduser().read_text(encoding="utf-8")
        parsed = json.loads(raw)
        if not isinstance(parsed, list) and not (
            market_id == "myriad_markets" and operation in {"cancel_order", "batch_modify_orders"}
        ) and not (market_id == "hyperliquid" and isinstance(parsed, dict)):
            if not (market_id in {"ibkr_forecasttrader", "forecastex", "cme_prediction_markets"} and operation == "modify_order" and isinstance(parsed, dict)):
                raise ValueError("--instructions must contain a JSON array (or a Myriad/Hyperliquid/IBKR JSON object).")
        if operation in {"batch_create_orders", "batch_cancel_orders"} or (market_id == "polymarket" and operation == "cancel_orders") or (market_id == "prophet_exchange" and operation == "cancel_orders"):
            payload["orders"] = parsed
        elif market_id == "myriad_markets" and operation == "batch_modify_orders":
            if not isinstance(parsed, dict):
                raise ValueError("Myriad batch_modify_orders instructions must be a JSON object with cancel/place arrays.")
            payload.update(parsed)
        elif market_id == "myriad_markets" and operation == "cancel_order":
            if not isinstance(parsed, dict):
                raise ValueError("Myriad cancel_order instructions must be a JSON object with order and signature.")
            payload.update(parsed)
        elif market_id == "hyperliquid":
            if not isinstance(parsed, dict):
                raise ValueError("Hyperliquid order-management instructions must be a signed JSON object.")
            payload["signed_action"] = parsed
        elif market_id in {"ibkr_forecasttrader", "forecastex", "cme_prediction_markets"}:
            if not isinstance(parsed, dict):
                raise ValueError("IBKR modify_order instructions must be a JSON object.")
            payload["instructions"] = parsed
        else:
            payload["instructions"] = parsed
    _put_optional(payload, "customer_ref", getattr(args, "customer_ref", None))
    if getattr(args, "market_version", None) not in (None, ""):
        raw_version = str(args.market_version)
        payload["market_version"] = _coerce_value(raw_version)
    if bool(getattr(args, "async_request", False)):
        payload["async_request"] = True
    _put_optional(payload, "confirm_global_cancel", getattr(args, "confirm_global_cancel", None))
    for key, argument in (
        ("order_id", "order_id"),
        ("external_id", "external_id"),
        ("order_ids", "order_ids"),
        ("token_id", "token_id"),
        ("token_ids", "token_ids"),
        ("event_id", "event_id"),
        ("offer_id", "offer_id"),
        ("offer_ids", "offer_ids"),
        ("event_ids", "event_ids"),
        ("market_ids", "market_ids"),
        ("runner_ids", "runner_ids"),
        ("current_odds", "current_odds"),
        ("new_odds", "new_odds"),
        ("current_stake", "current_stake"),
        ("new_stake", "new_stake"),
        ("ticker", "ticker"),
        ("side", "side"),
        ("price", "price"),
        ("count", "count"),
        ("client_order_id", "client_order_id"),
        ("updated_client_order_id", "updated_client_order_id"),
        ("reduce_by", "reduce_by"),
        ("reduce_to", "reduce_to"),
        ("order_hash", "order_hash"),
        ("trader", "trader"),
        ("timestamp", "timestamp"),
        ("signature", "signature"),
        ("signature_type", "signature_type"),
        ("network_id", "network_id"),
        ("allow_partial", "allow_partial"),
        ("cancel", "cancel"),
        ("place", "place"),
        ("subaccount", "subaccount"),
        ("exchange_index", "exchange_index"),
        ("confirm_order_management", "confirm_order_management"),
        ("signed_action", "signed_action"),
        ("manual_indicator", "manual_indicator"),
        ("external_operator", "external_operator"),
    ):
        _put_optional(payload, key, getattr(args, argument, None))
    data = adapter.manage_orders(operation, **payload)
    return _write_command_payload(
        args,
        {
            "market_id": market_id,
            "operation": operation,
            "parameters": payload,
            "data": data,
        },
    )


def run_live_safety_show(args: argparse.Namespace) -> int:
    return _write_command_payload(args, live_safety_payload(_load_cfg(args), _registry(), args.market))


def _order_payload(args: argparse.Namespace) -> Dict[str, Any]:
    payload = _json_arg(getattr(args, "json", None))
    market_id = getattr(args, "market", None)
    if not market_id:
        market_id = _load_cfg(args).selected_market_id
    _put_optional(payload, "market_id", market_id)
    _put_optional(payload, "contract_id", getattr(args, "contract", None))
    _put_optional(payload, "side", getattr(args, "side", None))
    _put_optional(payload, "size", getattr(args, "size", None))
    _put_optional(payload, "limit_price", getattr(args, "limit_price", None))
    if getattr(args, "metadata", None):
        metadata = dict(payload.get("metadata") or {})
        _merge_kv(metadata, args.metadata)
        payload["metadata"] = metadata
    return payload


def run_live_safety_preflight(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args)
    return _write_command_payload(args, live_preflight_payload(cfg, _registry(), _order_payload(args)))


def _alert_payload_from_args(args: argparse.Namespace, *, default_market: bool = False) -> Dict[str, Any]:
    payload = _json_arg(getattr(args, "json", None))
    market = args.market
    if not market and default_market:
        market = _load_cfg(args).selected_market_id
    _put_optional(payload, "market_id", market)
    _put_optional(payload, "contract_id", args.contract)
    _put_optional(payload, "label", args.label)
    _put_optional(payload, "direction", args.direction)
    _put_optional(payload, "threshold", args.threshold)
    _put_optional(payload, "source", args.source)
    _put_optional(payload, "once", args.once)
    _put_optional(payload, "enabled", args.enabled)
    return payload


def run_alerts_list(args: argparse.Namespace) -> int:
    return _write_command_payload(args, alerts_payload(_load_cfg(args), _registry()))


def run_alert_add(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args)
    registry = _registry()
    alert = alert_from_payload(cfg, registry, _alert_payload_from_args(args, default_market=True))
    cfg.alerts.append(alert)
    _save_cfg(args, cfg)
    return _write_command_payload(args, alerts_payload(cfg, registry))


def run_alert_update(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args)
    registry = _registry()
    alert = find_alert(cfg, args.alert_id)
    alert_from_payload(cfg, registry, _alert_payload_from_args(args), existing=alert)
    _save_cfg(args, cfg)
    return _write_command_payload(args, alerts_payload(cfg, registry))


def run_alert_delete(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args)
    deleted = delete_alert(cfg, args.alert_id)
    _save_cfg(args, cfg)
    return _write_command_payload(args, {"deleted": deleted.to_dict(), **alerts_payload(cfg, _registry())})


def run_alert_refresh(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args)
    registry = _registry()
    price_state: Dict[Any, Dict[str, Any]] = {}
    if args.alert_id:
        result = refresh_alert_price(cfg, registry, find_alert(cfg, args.alert_id), price_state)
        payload = {"refreshed": [result], "problems": [], "alerts": alerts_payload(cfg, registry, price_state)}
    else:
        result = refresh_all_alert_prices(cfg, registry, price_state)
        payload = {**result, "alerts": alerts_payload(cfg, registry, price_state)}
    _save_cfg(args, cfg)
    return _write_command_payload(args, payload)


def run_wallets_list(args: argparse.Namespace) -> int:
    return _write_command_payload(args, wallets_payload(_load_cfg(args)))


def _wallet_payload_from_args(args: argparse.Namespace) -> Dict[str, Any]:
    payload = _json_arg(getattr(args, "json", None))
    _put_optional(payload, "wallet", getattr(args, "wallet", None))
    _put_optional(payload, "display_name", getattr(args, "display_name", None))
    _put_optional(payload, "enabled", getattr(args, "enabled", None))
    _put_optional(payload, "only_market_slug", getattr(args, "only_market_slug", None))
    return payload


def run_wallet_add(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args)
    add_wallet_watch(cfg, _wallet_payload_from_args(args))
    _save_cfg(args, cfg)
    return _write_command_payload(args, wallets_payload(cfg))


def run_wallet_update(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args)
    update_wallet_watch(cfg, args.wallet_id, _wallet_payload_from_args(args))
    _save_cfg(args, cfg)
    return _write_command_payload(args, wallets_payload(cfg))


def run_wallet_delete(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args)
    deleted = delete_wallet_watch(cfg, args.wallet_id)
    _save_cfg(args, cfg)
    return _write_command_payload(args, {"deleted": deleted.to_dict(), **wallets_payload(cfg)})


def run_wallet_poll(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args)
    recent_activity: List[Dict[str, Any]] = []
    result = poll_wallet_activity(cfg, _registry(), recent_activity, limit=max(1, min(int(args.limit), 100)))
    _save_cfg(args, cfg)
    payload = {
        **result,
        "wallets": wallets_payload(
            cfg,
            {
                "poll_interval_seconds": 10.0,
                "last_polled_at": time.time(),
                "last_message": f"Polled {result['polled_wallets']} wallet(s); {len(result['activity'])} new activity item(s).",
            },
            recent_activity,
        ),
        "copy": copy_payload(cfg, _registry()),
    }
    return _write_command_payload(args, payload)


def _wallet_poll_once(args: argparse.Namespace, recent_activity: List[Dict[str, Any]]) -> Dict[str, Any]:
    cfg = _load_cfg(args)
    registry = _registry()
    result = poll_wallet_activity(cfg, registry, recent_activity, limit=max(1, min(int(args.limit), 100)))
    _save_cfg(args, cfg)
    return {
        **result,
        "wallets": wallets_payload(
            cfg,
            {
                "poll_interval_seconds": float(args.interval),
                "last_polled_at": time.time(),
                "last_message": f"Polled {result['polled_wallets']} wallet(s); {len(result['activity'])} new activity item(s).",
            },
            recent_activity,
        ),
        "copy": copy_payload(cfg, registry),
    }


def run_wallet_watch(args: argparse.Namespace) -> int:
    recent_activity: List[Dict[str, Any]] = []
    stream, should_close = _open_output(args.output)
    iterations = None if args.iterations in (None, "") else max(1, int(args.iterations))
    interval = max(1.0, float(args.interval))
    completed = 0
    try:
        while iterations is None or completed < iterations:
            payload = _wallet_poll_once(args, recent_activity)
            if args.compact:
                json.dump(payload, stream, separators=(",", ":"), sort_keys=True)
            else:
                json.dump(payload, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            completed += 1
            if iterations is not None and completed >= iterations:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        return 130
    finally:
        if should_close:
            stream.close()
    return 0


def run_copy_show(args: argparse.Namespace) -> int:
    return _write_command_payload(args, copy_payload(_load_cfg(args), _registry()))


def run_copy_set(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args)
    payload = _json_arg(args.json)
    _put_optional(payload, "enabled", args.enabled)
    _put_optional(payload, "live", args.live)
    _put_optional(payload, "follow_wallet", args.follow_wallet)
    _put_optional(payload, "follow_wallets", args.follow_wallets)
    _put_optional(payload, "copy_percentage", args.copy_percentage)
    _put_optional(payload, "scale", args.scale)
    _put_optional(payload, "max_usdc_per_trade", args.max_usdc_per_trade)
    _put_optional(payload, "slippage", args.slippage)
    _put_optional(payload, "allow_sells", args.allow_sells)
    _put_optional(payload, "conflict_guard", args.conflict_guard)
    _put_optional(payload, "conflict_window_seconds", args.conflict_window_seconds)
    apply_copy_settings_patch(cfg, payload)
    _save_cfg(args, cfg)
    return _write_command_payload(args, copy_payload(cfg, _registry()))


def run_copy_preview(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args)
    payload = _json_arg(args.json)
    _put_optional(payload, "proxyWallet", args.proxy_wallet)
    _put_optional(payload, "asset", args.asset or args.token_id)
    _put_optional(payload, "side", args.side)
    _put_optional(payload, "size", args.size)
    _put_optional(payload, "price", args.price)
    _put_optional(payload, "slug", args.slug)
    _put_optional(payload, "outcome", args.outcome)
    return _write_command_payload(args, copy_preview_payload(cfg, _registry(), payload))


def _paper_marks_path(args: argparse.Namespace) -> Path:
    configured = str(getattr(args, "marks_file", "") or "").strip()
    if configured:
        return Path(configured).expanduser()
    config_path = _config_path(args)
    return config_path.with_name(f"{config_path.stem}.paper-marks.json")


def _load_paper_marks(path: Path) -> Dict[tuple[str, str], Dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = payload.get("marks") if isinstance(payload, Mapping) else None
    if not isinstance(entries, list):
        return {}
    marks: Dict[tuple[str, str], Dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        market_id = str(entry.get("market_id") or "").strip().lower()
        contract_id = str(entry.get("contract_id") or "").strip()
        try:
            mark_price = float(entry.get("mark_price"))
            marked_at = int(entry.get("marked_at"))
        except (TypeError, ValueError):
            continue
        if market_id and contract_id:
            marks[(market_id, contract_id)] = {
                "mark_price": mark_price,
                "source": str(entry.get("source") or "unknown"),
                "marked_at": marked_at,
            }
    return marks


def _active_paper_marks(cfg: Any, marks: Mapping[tuple[str, str], Dict[str, Any]]) -> Dict[tuple[str, str], Dict[str, Any]]:
    active = {(str(row["market_id"]), str(row["contract_id"])) for row in paper_position_rows(cfg.paper_trades)}
    return {key: dict(value) for key, value in marks.items() if key in active}


def _fsync_parent_directory(path: Path) -> None:
    """Persist a completed atomic rename on POSIX filesystems."""
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path.parent, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _save_paper_marks(path: Path, marks: Mapping[tuple[str, str], Mapping[str, Any]]) -> None:
    payload = {
        "version": 1,
        "marks": [
            {
                "market_id": market_id,
                "contract_id": contract_id,
                "mark_price": value.get("mark_price"),
                "source": value.get("source"),
                "marked_at": value.get("marked_at"),
            }
            for (market_id, contract_id), value in sorted(marks.items())
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_path, path)
        _fsync_parent_directory(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def run_paper_show(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args)
    marks = _active_paper_marks(cfg, _load_paper_marks(_paper_marks_path(args)))
    return _write_command_payload(args, paper_payload(cfg, marks))


def run_paper_quote(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args)
    return _write_command_payload(args, paper_quote_payload(cfg, _registry(), _order_payload(args)))


def run_paper_quote_limit(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args)
    return _write_command_payload(args, paper_quote_limit_payload(cfg, _registry(), _order_payload(args)))


def run_paper_impact(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args)
    order = paper_order_from_payload(_order_payload(args))
    impact = paper_order_impact(cfg.paper_trades, order)
    return _write_command_payload(args, {"impact": impact})


def run_paper_order(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args)
    result = submit_paper_order(cfg, _registry(), _order_payload(args))
    _save_cfg(args, cfg)
    marks = _active_paper_marks(cfg, _load_paper_marks(_paper_marks_path(args)))
    _save_paper_marks(_paper_marks_path(args), marks)
    return _write_command_payload(args, {**result, "paper": paper_payload(cfg, marks)})


def run_paper_use_history(args: argparse.Namespace) -> int:
    return _write_command_payload(args, history_refill_payload(_load_cfg(args), args.record_id))


def run_paper_use_position(args: argparse.Namespace) -> int:
    return _write_command_payload(args, position_refill_payload(_load_cfg(args), args.market, args.contract))


def run_paper_clear_history(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args)
    cfg.paper_trades = []
    _save_cfg(args, cfg)
    _save_paper_marks(_paper_marks_path(args), {})
    return _write_command_payload(args, paper_payload(cfg, {}))


def run_paper_marks_refresh(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args)
    rows = paper_position_rows(cfg.paper_trades)
    path = _paper_marks_path(args)
    marks, problems = refresh_paper_marks(cfg, _registry(), rows, _active_paper_marks(cfg, _load_paper_marks(path)))
    _save_paper_marks(path, marks)
    return _write_command_payload(
        args,
        {"paper": paper_payload(cfg, marks), "problems": problems, "message": f"Marked {len(marks)}/{len(rows)} paper positions."},
    )


def run_paper_marks_refresh_selected(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args)
    path = _paper_marks_path(args)
    marks = refresh_selected_paper_mark(
        cfg,
        _registry(),
        args.market,
        args.contract,
        _active_paper_marks(cfg, _load_paper_marks(path)),
    )
    _save_paper_marks(path, marks)
    return _write_command_payload(args, {"paper": paper_payload(cfg, marks), "message": "Selected paper exposure mark refreshed."})


def run_paper_marks_clear(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args)
    _save_paper_marks(_paper_marks_path(args), {})
    return _write_command_payload(args, {"paper": paper_payload(cfg, {}), "message": "Paper exposure marks cleared."})


def run_paper_marks_clear_selected(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args)
    path = _paper_marks_path(args)
    marks = _active_paper_marks(cfg, _load_paper_marks(path))
    marks.pop((str(args.market).strip().lower(), str(args.contract).strip()), None)
    _save_paper_marks(path, marks)
    return _write_command_payload(
        args,
        {"paper": paper_payload(cfg, marks), "message": f"Selected paper exposure mark cleared: {args.market}:{args.contract}"},
    )


def _project_root() -> Path:
    return Path(__file__).resolve().parent


def _parse_requirement_entry(raw: str) -> Optional[Dict[str, str]]:
    line = raw.strip()
    if not line or line.startswith("#"):
        return None
    if "#" in line:
        line = line.split("#", 1)[0].strip()
    if not line:
        return None
    try:
        from packaging.requirements import Requirement

        requirement = Requirement(line)
        if requirement.marker is not None and not requirement.marker.evaluate():
            return None
        extras = f"[{','.join(sorted(requirement.extras))}]" if requirement.extras else ""
        return {"name": requirement.name, "display": f"{requirement.name}{extras}", "spec": str(requirement.specifier)}
    except Exception:
        if ";" in line:
            line = line.split(";", 1)[0].strip()
    name = line
    spec = ""
    for marker in ("==", ">=", "<=", "~=", "!=", ">", "<"):
        if marker in line:
            name, spec = line.split(marker, 1)
            spec = marker + spec
            break
    return {"name": name.strip(), "display": name.strip(), "spec": spec.strip()} if name.strip() else None


def _load_requirements() -> List[Dict[str, str]]:
    root = _project_root()
    requirements = root / "requirements.txt"
    raw_entries: List[str] = []
    if requirements.exists():
        raw_entries = requirements.read_text(encoding="utf-8").splitlines()
    else:
        pyproject = root / "pyproject.toml"
        if pyproject.exists():
            try:
                try:
                    import tomllib
                except ModuleNotFoundError:
                    import tomli as tomllib  # type: ignore
                data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
                raw_entries = [str(item) for item in data.get("project", {}).get("dependencies", [])]
            except Exception:
                raw_entries = []
    return [parsed for raw in raw_entries if (parsed := _parse_requirement_entry(raw))]


def _installed_version(package: str) -> str:
    try:
        return importlib_metadata.version(package)
    except importlib_metadata.PackageNotFoundError:
        pass
    for module_name in DEPENDENCY_IMPORT_FALLBACKS.get(package, (package.replace("-", "_"),)):
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue
        version = str(getattr(module, "__version__", "") or getattr(module, "version", "") or "").strip()
        return version or "installed"
    return ""


def _fetch_latest_version(package: str) -> str:
    request = urllib_request.Request(
        f"https://pypi.org/pypi/{package}/json",
        headers={"User-Agent": "MarketSentinel CLI"},
    )
    try:
        with urllib_request.urlopen(request, timeout=10) as response:
            if response.status != 200:
                return ""
            data = json.loads(response.read().decode("utf-8"))
            return str(data.get("info", {}).get("version") or "").strip()
    except (urllib_error.URLError, urllib_error.HTTPError, TimeoutError):
        return ""


def _is_up_to_date(installed: str, latest: str) -> bool:
    try:
        from packaging.version import Version

        return Version(installed) >= Version(latest)
    except Exception:
        return installed == latest


def _dependency_rows(*, latest: bool = False) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for req in _load_requirements():
        installed = _installed_version(req["name"])
        latest_version = _fetch_latest_version(req["name"]) if latest else ""
        if not installed:
            status = "missing"
        elif installed == "installed":
            status = "installed"
        elif latest_version:
            status = "ok" if _is_up_to_date(installed, latest_version) else "outdated"
        else:
            status = "ok"
        rows.append(
            {
                "package": req["display"],
                "required": req["spec"],
                "installed": installed or "not installed",
                "latest": latest_version or "-",
                "status": status,
            }
        )
    return rows


def run_dependencies(args: argparse.Namespace) -> int:
    rows = _dependency_rows(latest=bool(args.latest))
    return _write_command_payload(
        args,
        {
            "checked_latest": bool(args.latest),
            "dependencies": rows,
            "counts": {
                "total": len(rows),
                "missing": sum(1 for row in rows if row["status"] == "missing"),
                "outdated": sum(1 for row in rows if row["status"] == "outdated"),
            },
        },
    )


def run_polymarket_user_search(args: argparse.Namespace) -> int:
    return _write_command_payload(args, polymarket_user_search_payload(args.query, limit=int(args.limit)))


def run_polymarket_user_mdd(args: argparse.Namespace) -> int:
    payload = polymarket_user_mdd_payload(
        args.wallet,
        mode=args.mode,
        closed_limit=int(args.closed_limit),
        open_limit=int(args.open_limit),
        activity_limit=int(args.activity_limit),
        trade_limit=int(args.trade_limit),
        include_open=bool(args.include_open),
        equity_base_usd=None if args.equity_base_usd in (None, "") else float(args.equity_base_usd),
        max_points=int(args.max_points),
        cache_ttl_seconds=int(args.cache_ttl_seconds),
        mark_replay_token_limit=int(args.mark_replay_token_limit),
        mark_replay_point_limit=int(args.mark_replay_point_limit),
        mark_replay_interval=args.mark_replay_interval,
        mark_replay_fidelity=int(args.mark_replay_fidelity),
        include_accounting_snapshot=bool(args.include_accounting),
        accounting_timeout=float(args.accounting_timeout),
    )
    return _write_command_payload(args, payload)


def run_polymarket_readiness(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args)
    payload = {
        "clob_readiness": polymarket_clob_readiness_payload(cfg),
        "live_validation": polymarket_live_validation_payload(cfg),
    }
    return _write_command_payload(args, payload)


def run_polymarket_mdd_cache_list(args: argparse.Namespace) -> int:
    return _write_command_payload(args, polymarket_mdd_cache_payload(include_expired=bool(args.include_expired)))


def run_polymarket_mdd_cache_health(args: argparse.Namespace) -> int:
    return _write_command_payload(args, polymarket_mdd_cache_health_payload())


def run_polymarket_mdd_cache_purge(args: argparse.Namespace) -> int:
    payload: Dict[str, Any] = {"key": args.key or "", "expired_only": args.expired_only, "all": args.all}
    return _write_command_payload(args, polymarket_mdd_cache_purge_payload(payload))


def _require_live_validation_report(key: str) -> Dict[str, Any]:
    payload = polymarket_live_validation_report_payload(key)
    if payload is None:
        raise ValueError(f"Unknown live validation report: {key}")
    return payload


def _require_live_validation_review(key: str) -> Dict[str, Any]:
    payload = polymarket_live_validation_report_review_payload(key)
    if payload is None:
        raise ValueError(f"Unknown live validation report: {key}")
    return payload


def _write_live_validation_format(args: argparse.Namespace, payload: Mapping[str, Any], markdown: str) -> int:
    if args.format == "markdown":
        return _write_text_command(args, markdown)
    return _write_command_payload(args, payload)


def run_polymarket_live_reports_list(args: argparse.Namespace) -> int:
    return _write_command_payload(args, polymarket_live_validation_reports_payload(include_payload=bool(args.include_payload)))


def run_polymarket_live_reports_open(args: argparse.Namespace) -> int:
    return _write_command_payload(args, _require_live_validation_report(args.key))


def run_polymarket_live_reports_store(args: argparse.Namespace) -> int:
    payload: Dict[str, Any] = {
        "label": args.label or "",
        "source": args.source or "",
        "allow_duplicate": bool(args.allow_duplicate),
    }
    if args.report_file:
        path = Path(args.report_file).expanduser()
        payload["report_json"] = path.read_text(encoding="utf-8")
        payload["source_file"] = str(path)
    return _write_command_payload(args, polymarket_live_validation_report_store_payload(_load_cfg(args), payload))


def run_polymarket_live_reports_delete(args: argparse.Namespace) -> int:
    return _write_command_payload(args, polymarket_live_validation_report_purge_payload({"key": args.key}))


def run_polymarket_live_reports_export(args: argparse.Namespace) -> int:
    return _write_command_payload(args, _require_live_validation_report(args.key))


def run_polymarket_live_reports_review(args: argparse.Namespace) -> int:
    payload = _require_live_validation_review(args.key)
    return _write_live_validation_format(args, payload, live_validation_report_review_markdown(payload["bundle"]))


def run_polymarket_live_decisions_list(args: argparse.Namespace) -> int:
    params = {"report_key": [args.report_key]} if args.report_key else None
    return _write_command_payload(args, polymarket_live_validation_decisions_payload(params))


def run_polymarket_live_decisions_record(args: argparse.Namespace) -> int:
    payload = {
        "report_key": args.report_key,
        "payload_hash": args.payload_hash,
        "target_tier": args.target_tier,
        "decision": args.decision,
        "reviewer_note": args.reviewer_note,
        "review_bundle_hash": args.review_bundle_hash,
        "reviewer": args.reviewer or "",
    }
    return _write_command_payload(args, polymarket_live_validation_decision_store_payload(payload))


def run_polymarket_live_decisions_export(args: argparse.Namespace) -> int:
    params = {"report_key": [args.report_key]} if args.report_key else None
    payload = polymarket_live_validation_decisions_payload(params)
    return _write_live_validation_format(args, payload, live_validation_report_decisions_markdown(payload))


def _live_validation_proposal(args: argparse.Namespace) -> Dict[str, Any]:
    params = {"target_tier": [args.target_tier]} if getattr(args, "target_tier", "") else None
    return polymarket_live_validation_promotion_proposal_payload(params)


def run_polymarket_live_proposal_show(args: argparse.Namespace) -> int:
    return _write_command_payload(args, _live_validation_proposal(args))


def run_polymarket_live_proposal_export(args: argparse.Namespace) -> int:
    payload = _live_validation_proposal(args)
    return _write_live_validation_format(args, payload, live_validation_coverage_promotion_proposal_markdown(payload))


def _require_live_validation_snapshot(key: str) -> Dict[str, Any]:
    payload = polymarket_live_validation_promotion_proposal_snapshot_payload(key)
    if payload is None:
        raise ValueError(f"Unknown promotion proposal snapshot: {key}")
    return payload


def run_polymarket_live_snapshots_list(args: argparse.Namespace) -> int:
    return _write_command_payload(args, polymarket_live_validation_promotion_proposal_snapshots_payload())


def run_polymarket_live_snapshots_store(args: argparse.Namespace) -> int:
    payload = {"target_tier": args.target_tier or "", "label": args.label or "", "source": args.source or "cli"}
    return _write_command_payload(args, polymarket_live_validation_promotion_proposal_snapshot_store_payload(payload))


def run_polymarket_live_snapshots_open(args: argparse.Namespace) -> int:
    return _write_command_payload(args, _require_live_validation_snapshot(args.key))


def run_polymarket_live_snapshots_diff(args: argparse.Namespace) -> int:
    payload = polymarket_live_validation_promotion_proposal_snapshot_diff_payload(args.key)
    if payload is None:
        raise ValueError(f"Unknown promotion proposal snapshot: {args.key}")
    return _write_live_validation_format(args, payload, live_validation_promotion_proposal_snapshot_diff_markdown(payload))


def run_polymarket_live_snapshots_delete(args: argparse.Namespace) -> int:
    return _write_command_payload(args, polymarket_live_validation_promotion_proposal_snapshot_purge_payload({"key": args.key}))


def run_polymarket_live_snapshots_export(args: argparse.Namespace) -> int:
    payload = _require_live_validation_snapshot(args.key)
    return _write_live_validation_format(args, payload, live_validation_promotion_proposal_snapshot_markdown(payload))


def run_serve(args: argparse.Namespace) -> int:
    run_server(
        args.host,
        int(args.port),
        _config_path(args),
        frontend_dir=Path(args.frontend_dir).expanduser(),
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="market-sentinel", description="MarketSentinel headless utilities.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Config JSON path. Defaults to data/config.json or PREDICTION_MARKET_CONFIG_PATH.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    leaderboard = subparsers.add_parser(
        "polymarket-leaderboard",
        aliases=["leaderboard", "polymarket-analytics"],
        parents=[common],
        help="Run the Polymarket ROI/PnL/volume/MDD leaderboard scan without a GUI.",
    )
    leaderboard.add_argument("--sort", default="roi_pct", help="roi_pct, pnl_usd, volume_usd, mdd_pct, or mdd_usd.")
    leaderboard.add_argument("--direction", default="DESC", choices=["ASC", "DESC"])
    leaderboard.add_argument("--period", default="all")
    leaderboard.add_argument("--category", default="OVERALL")
    leaderboard.add_argument("--returned", "--limit", default="100", help="Rows to return; use unlimited, all, 0, or -1 for no local cap.")
    leaderboard.add_argument("--scanned", "--scan-limit", default="500", help="Rows to scan; use unlimited, all, 0, or -1 to scan until the API is exhausted.")
    leaderboard.add_argument("--compute-mdd", action="store_true")
    leaderboard.add_argument("--fast-scan", action="store_true")
    leaderboard.add_argument("--mdd-mode", default="fast", choices=["fast", "mark_replay"])
    leaderboard.add_argument("--mdd-scan", "--mdd-scan-limit", default="100", help="Candidate rows to compute MDD for; use unlimited, all, 0, or -1 for all candidates.")
    leaderboard.add_argument("--mdd-history-limit", default="500")
    leaderboard.add_argument("--mdd-activity-limit", default="1000")
    leaderboard.add_argument("--mdd-trade-limit", default="1000")
    leaderboard.add_argument("--mdd-open-limit", default="500")
    leaderboard.add_argument("--mdd-mark-replay-token-limit", default="10")
    leaderboard.add_argument("--mdd-mark-replay-point-limit", default="5000")
    leaderboard.add_argument("--mdd-mark-replay-interval", default="1h")
    leaderboard.add_argument("--mdd-mark-replay-fidelity", default="60")
    leaderboard.add_argument("--mdd-include-accounting", action="store_true")
    leaderboard.add_argument("--mdd-accounting-timeout", default="30")
    leaderboard.add_argument("--mdd-persist-cache", action="store_true")
    leaderboard.add_argument("--mdd-cache-ttl-seconds", default="60")
    leaderboard.add_argument("--equity-base-usd", default="")
    leaderboard.add_argument("--scan-concurrency", default="")
    leaderboard.add_argument("--scan-retry-attempts", default="5", help="Retry each leaderboard page this many times before failing.")
    leaderboard.add_argument("--scan-retry-delay-seconds", "--scan-retry-delay", default="30", help="Seconds to wait between leaderboard page retry attempts.")
    leaderboard.add_argument(
        "--state-db",
        default="",
        help="SQLite state database for durable, resumable large scans. Do not combine with --checkpoint.",
    )
    leaderboard.add_argument("--checkpoint", default="", help="Append fetched leaderboard pages to this JSONL checkpoint file.")
    leaderboard.add_argument("--resume", action="store_true", help="Resume --checkpoint or --state-db from its saved scan state.")
    leaderboard.add_argument(
        "--resume-on-failure",
        action="store_true",
        help="For --state-db scans, retry transient Polymarket API failures and resume the saved state automatically.",
    )
    leaderboard.add_argument(
        "--resume-max-restarts",
        default="0",
        help="Maximum automatic resume attempts; 0 means retry until interrupted. Requires --resume-on-failure.",
    )
    leaderboard.add_argument(
        "--resume-backoff-seconds",
        default="60",
        help="Initial automatic resume delay; doubles per failure up to one hour. Requires --resume-on-failure.",
    )
    leaderboard.add_argument("--checkpoint-fsync-every", type=int, default=20, help="Flush checkpoint data to disk every N pages.")
    leaderboard.add_argument("--mdd-concurrency", default="")
    leaderboard.add_argument("--mdd-stop-on-limit", action="store_true", default=None)
    leaderboard.add_argument("--min-pnl-usd", default="")
    leaderboard.add_argument("--max-pnl-usd", default="")
    leaderboard.add_argument("--min-volume-usd", default="")
    leaderboard.add_argument("--max-volume-usd", default="")
    leaderboard.add_argument("--min-roi-pct", default="")
    leaderboard.add_argument("--max-roi-pct", default="")
    leaderboard.add_argument("--min-mdd-usd", default="")
    leaderboard.add_argument("--max-mdd-usd", default="")
    leaderboard.add_argument("--min-mdd-pct", default="")
    leaderboard.add_argument("--max-mdd-pct", default="")
    leaderboard.add_argument("--param", action="append", type=_split_key_value, default=[], help="Raw API query override in KEY=VALUE form. Can be passed more than once.")
    leaderboard.add_argument("--format", choices=["csv", "json"], default="csv")
    leaderboard.add_argument("--output", "-o", default="-", help="Output file path, or - for stdout.")
    leaderboard.add_argument("--quiet", action="store_true", help="Suppress progress and summary messages on stderr.")
    leaderboard.set_defaults(func=run_polymarket_leaderboard)

    leaderboard_status = subparsers.add_parser(
        "polymarket-leaderboard-status",
        aliases=["leaderboard-status"],
        help="Read durable Polymarket leaderboard scan status without starting or changing a scan.",
    )
    leaderboard_status.add_argument("--state-db", required=True, help="Existing SQLite state database created by polymarket-leaderboard.")
    leaderboard_status.add_argument("--pid-file", default="", help="Optional PID file, such as one written by `echo $! > polymarket-scan.pid`.")
    _add_json_output_args(leaderboard_status)
    leaderboard_status.set_defaults(func=run_polymarket_leaderboard_status)

    leaderboard_export = subparsers.add_parser(
        "polymarket-leaderboard-export",
        aliases=["leaderboard-export"],
        help="Export current durable leaderboard rows without starting, resuming, or changing a scan.",
    )
    leaderboard_export.add_argument("--state-db", required=True, help="Existing SQLite state database created by polymarket-leaderboard.")
    leaderboard_export.add_argument("--sort", default="roi_pct", help="roi_pct, pnl_usd, volume_usd, mdd_pct, or mdd_usd.")
    leaderboard_export.add_argument("--direction", default="DESC", choices=["ASC", "DESC"])
    leaderboard_export.add_argument("--returned", "--limit", default="unlimited", help="Rows to export; use unlimited, all, 0, or -1 for no local cap.")
    leaderboard_export.add_argument("--require-mdd", action="store_true", help="Export only rows with completed MDD calculations.")
    leaderboard_export.add_argument("--min-pnl-usd", default="")
    leaderboard_export.add_argument("--max-pnl-usd", default="")
    leaderboard_export.add_argument("--min-volume-usd", default="")
    leaderboard_export.add_argument("--max-volume-usd", default="")
    leaderboard_export.add_argument("--min-roi-pct", default="")
    leaderboard_export.add_argument("--max-roi-pct", default="")
    leaderboard_export.add_argument("--min-mdd-usd", default="")
    leaderboard_export.add_argument("--max-mdd-usd", default="")
    leaderboard_export.add_argument("--min-mdd-pct", default="")
    leaderboard_export.add_argument("--max-mdd-pct", default="")
    leaderboard_export.add_argument("--format", choices=["csv", "json"], default="csv")
    leaderboard_export.add_argument("--output", "-o", default="-", help="Output file path, or - for stdout.")
    leaderboard_export.set_defaults(func=run_polymarket_leaderboard_export)

    health = subparsers.add_parser("health", parents=[common], help="Print API/app health and route metadata.")
    health.add_argument("--frontend-dir", type=Path, default=DEFAULT_FRONTEND_DIR)
    _add_json_output_args(health)
    health.set_defaults(func=run_health)

    doctor = subparsers.add_parser("doctor", parents=[common], help="Run read-only local configuration and runtime readiness checks.")
    doctor.add_argument("--frontend-dir", type=Path, default=DEFAULT_FRONTEND_DIR)
    doctor.add_argument("--check-latest", action="store_true", help="Also check installed dependency versions against PyPI.")
    doctor.add_argument("--strict", action="store_true", help="Exit non-zero when warnings are present, such as an armed live-trading configuration.")
    _add_json_output_args(doctor)
    doctor.set_defaults(func=run_doctor)

    state = subparsers.add_parser("state", parents=[common], help="Print the full headless app state.")
    state.add_argument("--frontend-dir", type=Path, default=DEFAULT_FRONTEND_DIR)
    _add_json_output_args(state)
    state.set_defaults(func=run_state)

    config = subparsers.add_parser("config", parents=[common], help="Show or update global app config.")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_show = config_sub.add_parser("show", parents=[common], help="Show selected market, theme, design, wallets, and copy settings.")
    _add_json_output_args(config_show)
    config_show.set_defaults(func=run_config_show)
    config_set = config_sub.add_parser("set", parents=[common], help="Update selected market, theme, or design.")
    config_set.add_argument("--market", dest="market", default=None)
    config_set.add_argument("--theme", choices=["light", "dark"], default=None)
    config_set.add_argument("--design", choices=["classic", "aurora_2026", "graphite_2026", "sentinel_2027"], default=None)
    config_set.add_argument("--json", default=None, help="Inline JSON object or @file to merge before explicit flags.")
    _add_json_output_args(config_set)
    config_set.set_defaults(func=run_config_set)

    markets = subparsers.add_parser("markets", parents=[common], help="List or update market enablement and safety settings.")
    markets_sub = markets.add_subparsers(dest="markets_command", required=True)
    markets_list = markets_sub.add_parser("list", parents=[common], help="List configured markets and capabilities.")
    _add_json_output_args(markets_list)
    markets_list.set_defaults(func=run_markets_list)
    market_set = markets_sub.add_parser("set", parents=[common], help="Patch one market config.")
    market_set.add_argument("market_id")
    market_set.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=None)
    market_set.add_argument("--live-trading-enabled", action=argparse.BooleanOptionalAction, default=None)
    market_set.add_argument("--live-trading-confirmed", action=argparse.BooleanOptionalAction, default=None)
    market_set.add_argument("--live-trading-kill-switch", action=argparse.BooleanOptionalAction, default=None)
    market_set.add_argument("--live-trading-max-size", default=None)
    market_set.add_argument("--live-trading-max-notional", default=None)
    market_set.add_argument("--setting", action="append", type=_split_key_value, default=[], help="Raw market setting KEY=VALUE.")
    market_set.add_argument("--json", default=None, help="Inline JSON object or @file to merge before explicit flags.")
    _add_json_output_args(market_set)
    market_set.set_defaults(func=run_market_set)

    market_events = markets_sub.add_parser(
        "events",
        parents=[common],
        help="List events/markets from an enabled adapter using its official discovery feed.",
    )
    market_events.add_argument("--market", default=None, help="Market id; defaults to the selected config market.")
    market_events.add_argument("--query", default="", help="Optional adapter-supported search text.")
    market_events.add_argument("--limit", default="50", help="Maximum events to return (1-1000).")
    _add_json_output_args(market_events)
    market_events.set_defaults(func=run_market_events)

    market_contracts = markets_sub.add_parser(
        "contracts",
        parents=[common],
        help="List contracts/outcomes for an event from an enabled adapter.",
    )
    market_contracts.add_argument("event_id")
    market_contracts.add_argument("--market", default=None, help="Market id; defaults to the selected config market.")
    _add_json_output_args(market_contracts)
    market_contracts.set_defaults(func=run_market_contracts)

    market_price = markets_sub.add_parser(
        "price",
        parents=[common],
        help="Read one normalized contract price from an enabled adapter.",
    )
    market_price.add_argument("contract")
    market_price.add_argument("--market", default=None, help="Market id; defaults to the selected config market.")
    _add_json_output_args(market_price)
    market_price.set_defaults(func=run_market_price)

    market_orderbook = markets_sub.add_parser(
        "orderbook",
        parents=[common],
        help="Read one normalized contract orderbook from an enabled adapter.",
    )
    market_orderbook.add_argument("contract")
    market_orderbook.add_argument("--market", default=None, help="Market id; defaults to the selected config market.")
    _add_json_output_args(market_orderbook)
    market_orderbook.set_defaults(func=run_market_orderbook)

    market_trades = markets_sub.add_parser(
        "trades",
        parents=[common],
        help="Read normalized trade history from an enabled adapter when officially documented.",
    )
    market_trades.add_argument("contract")
    market_trades.add_argument("--market", default=None, help="Market id; defaults to the selected config market.")
    market_trades.add_argument("--limit", default="50", help="Maximum trades to return (1-1000).")
    market_trades.add_argument("--before", default=None, help="Optional Unix timestamp bound.")
    market_trades.add_argument("--after", default=None, help="Optional Unix timestamp bound.")
    _add_json_output_args(market_trades)
    market_trades.set_defaults(func=run_market_trades)

    market_candles = markets_sub.add_parser(
        "candles",
        parents=[common],
        help="Read normalized candle history from an enabled adapter when officially documented.",
    )
    market_candles.add_argument("contract")
    market_candles.add_argument("--market", default=None, help="Market id; defaults to the selected config market.")
    market_candles.add_argument("--resolution", default="1h")
    market_candles.add_argument("--from", dest="from_timestamp", default=None, help="Optional Unix timestamp bound.")
    market_candles.add_argument("--to", dest="to_timestamp", default=None, help="Optional Unix timestamp bound.")
    _add_json_output_args(market_candles)
    market_candles.set_defaults(func=run_market_candles)

    market_account = markets_sub.add_parser(
        "account",
        parents=[common],
        help="Read an explicitly documented authenticated account feed (including Polymarket CLOB and Predict.fun account surfaces).",
    )
    market_account.add_argument("operation", choices=MARKET_ACCOUNT_OPERATIONS)
    market_account.add_argument("--market", default=None, help="Market id; defaults to the selected config market.")
    market_account.add_argument("--contract", default=None, help="Optional canonical contract id for order feeds.")
    market_account.add_argument("--ticker", default=None, help="Kalshi market ticker for account reads.")
    market_account.add_argument("--market-slug", default=None, help="Limitless market slug for user_orders.")
    market_account.add_argument("--on-behalf-of", default=None, help="Optional Limitless delegated profile.")
    market_account.add_argument("--order-id", default=None, help="Optional order id for order-detail/fill reads.")
    market_account.add_argument("--token-id", default=None, help="Probable token id for authenticated order reads.")
    market_account.add_argument("--token-ids", default=None, help="Probable comma-separated token ids for open-order reads.")
    market_account.add_argument("--client-order-id", default=None, help="Optional Probable client order id.")
    market_account.add_argument("--trade-id", default=None, help="Optional Polymarket trade id for fill reads.")
    market_account.add_argument("--page", default="1", help="Opinion account page (1-10000).")
    market_account.add_argument("--account-market-id", default="", help="Opinion numeric market filter.")
    market_account.add_argument("--trading-model", default="all", help="Myriad account model: amm, ob, or all.")
    market_account.add_argument("--min-shares", default="", help="Myriad minimum position shares.")
    market_account.add_argument("--network-id", default="", help="Myriad account network id.")
    market_account.add_argument("--token-address", default="", help="Myriad account token address.")
    market_account.add_argument("--keyword", default="", help="Myriad account search keyword.")
    market_account.add_argument("--sort-by", default="", help="Myriad portfolio sort field.")
    market_account.add_argument("--exclude-history", action="store_true", help="Myriad portfolio: exclude historical positions.")
    market_account.add_argument("--group-by-event", action="store_true", help="Myriad portfolio: group positions by event.")
    market_account.add_argument("--state", default="", help="Myriad market-position state filter.")
    market_account.add_argument("--topics", default="", help="Myriad market-position comma-separated topics.")
    market_account.add_argument("--market-ids", default="", help="Myriad market-position comma-separated market ids.")
    market_account.add_argument("--event-types", default="", help="Predict.fun comma-separated account activity event types.")
    market_account.add_argument("--chain-id", default="", help="Opinion numeric chain filter.")
    market_account.add_argument("--event-type-id", default="", help="Betfair event type id filter.")
    market_account.add_argument("--account-event-id", default="", help="Betfair event id filter.")
    market_account.add_argument("--account-sport-id", default="", help="Matchbook sport id filter.")
    market_account.add_argument("--account-side", default="", help="Matchbook offer side filter (back/lay/win/lose).")
    market_account.add_argument("--account-offer-status", default="", help="Matchbook offer status filter.")
    market_account.add_argument("--account-aggregation-type", default="none", help="Matchbook offer aggregation (none/summary/average).")
    market_account.add_argument("--account-odds-type", default="DECIMAL", help="Matchbook odds type (DECIMAL/US/HK/MALAY/INDO/percent).")
    market_account.add_argument("--account-interval", default="", help="Matchbook offer interval in seconds.")
    market_account.add_argument("--account-cancellation-reason", default="", help="Matchbook cancellation reason.")
    market_account.add_argument("--account-include-edits", action="store_true", help="Include Matchbook offer edits.")
    market_account.add_argument("--runner-id", default="", help="Betfair runner id filter.")
    market_account.add_argument("--bet-id", default="", help="Betfair bet id filter.")
    market_account.add_argument("--group-by", default="BET", help="Betfair cleared-order roll-up.")
    market_account.add_argument("--include-item-description", action="store_true")
    market_account.add_argument("--wallet", default="", help="Betfair account wallet, Predict.fun address, or Myriad activity wallet.")
    market_account.add_argument("--locale", default="en", help="Betfair account-statement locale.")
    market_account.add_argument("--exclude-item", action="store_true", help="Exclude item details from Betfair statements.")
    market_account.add_argument("--from-currency", default="", help="Betfair source currency for currency_rates.")
    market_account.add_argument("--order-by", default="BY_MATCH_TIME", help="Betfair current-order sort field.")
    market_account.add_argument("--sort-dir", default="EARLIEST_TO_LATEST", help="Betfair current-order sort direction.")
    market_account.add_argument("--event-ticker", default=None, help="Optional event ticker for position/volume feeds.")
    market_account.add_argument("--status", default="", help="Documented account order status (venue-specific).")
    market_account.add_argument("--limit", default=None, help="Optional page size (operation-specific).")
    market_account.add_argument("--offset", default="0", help="Optional page offset.")
    market_account.add_argument("--cursor", default="", help="Cursor returned by a previous account read.")
    market_account.add_argument("--subaccount", default=None, help="Optional Kalshi subaccount number (0-63).")
    market_account.add_argument("--count-filter", default="", help="Kalshi positions filter: position,total_traded.")
    market_account.add_argument("--historical", action="store_true", help="Use the venue's documented historical endpoint.")
    market_account.add_argument("--sort", default=None, help="Documented position sort value.")
    market_account.add_argument("--is-resolved", default=None, help="Predict.fun positions filter: true or false.")
    market_account.add_argument("--search", default="", help="Settled-position search text.")
    market_account.add_argument("--category", default="", help="Settled-position category.")
    market_account.add_argument("--with-cash-outs", action="store_true")
    market_account.add_argument("--dex", default="", help="Optional Hyperliquid perpetual DEX name.")
    market_account.add_argument("--from", dest="from_timestamp", default=None, help="Optional Unix timestamp bound.")
    market_account.add_argument("--to", dest="to_timestamp", default=None, help="Optional Unix timestamp bound.")
    market_account.add_argument("--before", default=None, help="Optional Polymarket fill timestamp upper bound.")
    market_account.add_argument("--after", default=None, help="Optional Polymarket fill timestamp lower bound.")
    _add_json_output_args(market_account)
    market_account.set_defaults(func=run_market_account)

    market_orders = markets_sub.add_parser(
        "manage-orders",
        parents=[common],
        help="Run a guarded documented live order-management mutation (Betfair, Gemini, Hyperliquid, IBKR event contracts, Kalshi, Limitless, Matchbook, Myriad, Opinion, Polymarket, Prophet Exchange, Predict.fun, Probable, Smarkets, or Xmarket).",
    )
    market_orders.add_argument("operation", choices=MARKET_ORDER_MANAGEMENT_OPERATIONS)
    market_orders.add_argument("--market", default=None, help="Market id; defaults to the selected config market.")
    market_orders.add_argument("--exchange-market-id", default="", help="Betfair exchange market id for the mutation.")
    market_orders.add_argument("--market-id", default="", help="Polymarket condition id for cancel_market_orders.")
    market_orders.add_argument("--market-slug", default="", help="Limitless market slug for cancel_all_orders.")
    market_orders.add_argument("--asset-id", default="", help="Polymarket token id for cancel_market_orders.")
    market_orders.add_argument(
        "--instructions",
        default=None,
        help="JSON array of venue instructions/order ids, or a signed Hyperliquid/Myriad JSON object, or @path to a JSON file.",
    )
    market_orders.add_argument("--customer-ref", default=None, help="Optional Betfair de-duplication reference (max 32 chars).")
    market_orders.add_argument("--market-version", default=None, help="Optional replaceOrders market version integer or JSON object.")
    market_orders.add_argument("--async-request", action="store_true", help="Request asynchronous replaceOrders processing.")
    market_orders.add_argument(
        "--confirm-global-cancel",
        default=None,
        help="Exact global-cancel text is required (venue-specific; e.g. CANCEL ALL BETS, CANCEL ALL LIMITLESS ORDERS, CANCEL ALL MATCHBOOK OFFERS, or CANCEL ALL OPINION ORDERS).",
    )
    market_orders.add_argument("--order-id", default=None, help="Venue order identifier for single-order mutations.")
    market_orders.add_argument("--external-id", default=None, help="Prophet Exchange external id paired with the returned order id.")
    market_orders.add_argument("--order-ids", default=None, help="Probable comma-separated order ids for batch cancellation.")
    market_orders.add_argument("--token-id", default=None, help="Probable token id for order cancellation.")
    market_orders.add_argument("--token-ids", default=None, help="Probable comma-separated token ids for scoped reads.")
    market_orders.add_argument("--event-id", default=None, help="Probable event id for cancel-all scoping.")
    market_orders.add_argument("--offer-id", default=None, help="Matchbook offer identifier for single-offer mutations.")
    market_orders.add_argument("--offer-ids", default=None, help="Matchbook comma-separated offer ids for cancel_offers.")
    market_orders.add_argument("--event-ids", default=None, help="Matchbook comma-separated event ids for scoped cancellation.")
    market_orders.add_argument("--market-ids", default=None, help="Matchbook comma-separated market ids for scoped cancellation.")
    market_orders.add_argument("--runner-ids", default=None, help="Matchbook comma-separated runner ids for scoped cancellation.")
    market_orders.add_argument("--current-odds", default=None, help="Matchbook edit_offer current decimal odds.")
    market_orders.add_argument("--new-odds", default=None, help="Matchbook edit_offer replacement decimal odds.")
    market_orders.add_argument("--current-stake", default=None, help="Matchbook edit_offer current remaining stake.")
    market_orders.add_argument("--new-stake", default=None, help="Matchbook edit_offer replacement remaining stake.")
    market_orders.add_argument("--order-hash", default=None, help="Myriad order hash or safe client order id for cancel_order.")
    market_orders.add_argument("--trader", default=None, help="Myriad trader wallet for cancel_all_orders.")
    market_orders.add_argument("--timestamp", default=None, help="Myriad cancel-all EIP-712 timestamp.")
    market_orders.add_argument("--signature", default=None, help="Myriad EIP-712 order/cancel signature.")
    market_orders.add_argument("--signature-type", default=None, help="Myriad signature type: 0 (EOA) or 3 (SCW).")
    market_orders.add_argument("--network-id", default=None, help="Myriad network id for signed order requests.")
    market_orders.add_argument(
        "--allow-partial",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Myriad batch mutation behavior when individual entries fail (default true).",
    )
    market_orders.add_argument("--trade-id", default=None, help="Polymarket trade id for account fills reads.")
    market_orders.add_argument("--ticker", default=None, help="Kalshi market ticker for amend_order.")
    market_orders.add_argument("--side", choices=["bid", "ask", "BUY", "SELL"], default=None, help="Kalshi bid/ask or Opinion BUY/SELL filter.")
    market_orders.add_argument("--price", default=None, help="Kalshi V2 amend price in probability dollars.")
    market_orders.add_argument("--count", default=None, help="Kalshi V2 amend total contract count.")
    market_orders.add_argument("--client-order-id", default=None, help="Optional Kalshi original client order id.")
    market_orders.add_argument("--updated-client-order-id", default=None, help="Optional Kalshi amended client order id.")
    market_orders.add_argument("--reduce-by", default=None, help="Kalshi decrease amount.")
    market_orders.add_argument("--reduce-to", default=None, help="Kalshi target remaining count; use exactly one reduction flag.")
    market_orders.add_argument("--subaccount", default=None, help="Kalshi subaccount number (0-63).")
    market_orders.add_argument("--exchange-index", default=None, help="Kalshi exchange shard; only 0 is supported.")
    market_orders.add_argument("--manual-indicator", default=None, help="IBKR CME manualIndicator value (true/false).")
    market_orders.add_argument("--external-operator", default=None, help="IBKR CME extOperator identifier.")
    market_orders.add_argument(
        "--confirm-order-management",
        default=None,
        help="Exact text I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS is required for live mutations.",
    )
    market_orders.add_argument("--json", default=None, help="Inline JSON object or @file to merge before explicit flags.")
    _add_json_output_args(market_orders)
    market_orders.set_defaults(func=run_market_order_management)

    live = subparsers.add_parser("live-safety", parents=[common], help="Inspect live safety gates or run a no-order preflight.")
    live_sub = live.add_subparsers(dest="live_command", required=True)
    live_show = live_sub.add_parser("show", parents=[common], help="Show live safety gates.")
    live_show.add_argument("--market", default=None)
    _add_json_output_args(live_show)
    live_show.set_defaults(func=run_live_safety_show)
    live_preflight = live_sub.add_parser("preflight", parents=[common], help="Validate a live order without placing it.")
    live_preflight.add_argument("--market", default=None)
    live_preflight.add_argument("--contract", required=True)
    live_preflight.add_argument("--side", required=True, choices=["BUY", "SELL", "BACK", "LAY"])
    live_preflight.add_argument("--size", required=True)
    live_preflight.add_argument("--limit-price", default=None)
    live_preflight.add_argument("--metadata", action="append", type=_split_key_value, default=[])
    live_preflight.add_argument("--json", default=None)
    _add_json_output_args(live_preflight)
    live_preflight.set_defaults(func=run_live_safety_preflight)

    alerts = subparsers.add_parser("alerts", parents=[common], help="Manage price alerts.")
    alerts_sub = alerts.add_subparsers(dest="alerts_command", required=True)
    alerts_list = alerts_sub.add_parser("list", parents=[common], help="List alerts.")
    _add_json_output_args(alerts_list)
    alerts_list.set_defaults(func=run_alerts_list)
    alert_add = alerts_sub.add_parser("add", parents=[common], help="Add a price alert.")
    alert_add.add_argument("--market", default=None)
    alert_add.add_argument("--contract", required=True)
    alert_add.add_argument("--label", default=None)
    alert_add.add_argument("--direction", choices=["above", "below"], required=True)
    alert_add.add_argument("--threshold", required=True)
    alert_add.add_argument("--source", choices=["last_trade", "midpoint", "best_bid", "best_ask"], default="last_trade")
    alert_add.add_argument("--once", action=argparse.BooleanOptionalAction, default=None)
    alert_add.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=None)
    alert_add.add_argument("--json", default=None)
    _add_json_output_args(alert_add)
    alert_add.set_defaults(func=run_alert_add)
    alert_update = alerts_sub.add_parser("update", parents=[common], help="Update an alert.")
    alert_update.add_argument("alert_id")
    alert_update.add_argument("--market", default=None)
    alert_update.add_argument("--contract", default=None)
    alert_update.add_argument("--label", default=None)
    alert_update.add_argument("--direction", choices=["above", "below"], default=None)
    alert_update.add_argument("--threshold", default=None)
    alert_update.add_argument("--source", choices=["last_trade", "midpoint", "best_bid", "best_ask"], default=None)
    alert_update.add_argument("--once", action=argparse.BooleanOptionalAction, default=None)
    alert_update.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=None)
    alert_update.add_argument("--json", default=None)
    _add_json_output_args(alert_update)
    alert_update.set_defaults(func=run_alert_update)
    alert_delete = alerts_sub.add_parser("delete", parents=[common], help="Delete an alert.")
    alert_delete.add_argument("alert_id")
    _add_json_output_args(alert_delete)
    alert_delete.set_defaults(func=run_alert_delete)
    alert_refresh = alerts_sub.add_parser("refresh", parents=[common], help="Refresh all alerts or one alert id.")
    alert_refresh.add_argument("alert_id", nargs="?")
    _add_json_output_args(alert_refresh)
    alert_refresh.set_defaults(func=run_alert_refresh)

    wallets = subparsers.add_parser("wallets", parents=[common], help="Manage Polymarket wallet tracking.")
    wallets_sub = wallets.add_subparsers(dest="wallets_command", required=True)
    wallets_list = wallets_sub.add_parser("list", parents=[common], help="List tracked wallets.")
    _add_json_output_args(wallets_list)
    wallets_list.set_defaults(func=run_wallets_list)
    wallet_add = wallets_sub.add_parser("add", parents=[common], help="Track a wallet.")
    wallet_add.add_argument("--wallet", required=True)
    wallet_add.add_argument("--display-name", default=None)
    wallet_add.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=None)
    wallet_add.add_argument("--only-market-slug", default=None)
    wallet_add.add_argument("--json", default=None)
    _add_json_output_args(wallet_add)
    wallet_add.set_defaults(func=run_wallet_add)
    wallet_update = wallets_sub.add_parser("update", parents=[common], help="Update a tracked wallet.")
    wallet_update.add_argument("wallet_id")
    wallet_update.add_argument("--wallet", default=None)
    wallet_update.add_argument("--display-name", default=None)
    wallet_update.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=None)
    wallet_update.add_argument("--only-market-slug", default=None)
    wallet_update.add_argument("--json", default=None)
    _add_json_output_args(wallet_update)
    wallet_update.set_defaults(func=run_wallet_update)
    wallet_delete = wallets_sub.add_parser("delete", parents=[common], help="Delete a tracked wallet.")
    wallet_delete.add_argument("wallet_id")
    _add_json_output_args(wallet_delete)
    wallet_delete.set_defaults(func=run_wallet_delete)
    wallet_poll = wallets_sub.add_parser("poll", parents=[common], help="Poll tracked wallets once and run copy previews.")
    wallet_poll.add_argument("--limit", type=int, default=25)
    _add_json_output_args(wallet_poll)
    wallet_poll.set_defaults(func=run_wallet_poll)
    wallet_watch = wallets_sub.add_parser("watch", parents=[common], help="Continuously poll tracked wallets from CLI until Ctrl+C.")
    wallet_watch.add_argument("--limit", type=int, default=25)
    wallet_watch.add_argument("--interval", default="10")
    wallet_watch.add_argument("--iterations", default=None, help="Optional number of polls for batch/smoke runs.")
    _add_json_output_args(wallet_watch)
    wallet_watch.set_defaults(func=run_wallet_watch)

    copy = subparsers.add_parser("copy", parents=[common], help="Show, update, or preview guarded copy trading.")
    copy_sub = copy.add_subparsers(dest="copy_command", required=True)
    copy_show = copy_sub.add_parser("show", parents=[common], help="Show copy trading settings.")
    _add_json_output_args(copy_show)
    copy_show.set_defaults(func=run_copy_show)
    copy_set = copy_sub.add_parser("set", parents=[common], help="Patch copy trading settings.")
    copy_set.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=None)
    copy_set.add_argument("--live", action=argparse.BooleanOptionalAction, default=None)
    copy_set.add_argument("--follow-wallet", default=None)
    copy_set.add_argument("--follow-wallets", default=None, help="Comma-separated wallet list.")
    copy_set.add_argument("--copy-percentage", default=None)
    copy_set.add_argument("--scale", default=None)
    copy_set.add_argument("--max-usdc-per-trade", default=None)
    copy_set.add_argument("--slippage", default=None)
    copy_set.add_argument("--allow-sells", action=argparse.BooleanOptionalAction, default=None)
    copy_set.add_argument("--conflict-guard", action=argparse.BooleanOptionalAction, default=None)
    copy_set.add_argument("--conflict-window-seconds", default=None)
    copy_set.add_argument("--json", default=None)
    _add_json_output_args(copy_set)
    copy_set.set_defaults(func=run_copy_set)
    copy_preview = copy_sub.add_parser("preview", parents=[common], help="Preview a copy-trading activity without placing an order.")
    copy_preview.add_argument("--proxy-wallet", default=None)
    copy_preview.add_argument("--asset", default=None)
    copy_preview.add_argument("--token-id", default=None)
    copy_preview.add_argument("--side", choices=["BUY", "SELL"], default="BUY")
    copy_preview.add_argument("--size", default="0")
    copy_preview.add_argument("--price", default=None)
    copy_preview.add_argument("--slug", default=None)
    copy_preview.add_argument("--outcome", default=None)
    copy_preview.add_argument("--json", default=None)
    _add_json_output_args(copy_preview)
    copy_preview.set_defaults(func=run_copy_preview)

    paper = subparsers.add_parser("paper", parents=[common], help="Paper trading state, quotes, impact, and orders.")
    paper_sub = paper.add_subparsers(dest="paper_command", required=True)
    paper_show = paper_sub.add_parser("show", parents=[common], help="Show paper history and positions.")
    paper_show.add_argument("--marks-file", default="", help="Persistent CLI paper-mark sidecar; defaults beside --config.")
    _add_json_output_args(paper_show)
    paper_show.set_defaults(func=run_paper_show)
    paper_quote = paper_sub.add_parser("quote", parents=[common], help="Fetch a quote/orderbook for a contract.")
    paper_quote.add_argument("--market", default=None)
    paper_quote.add_argument("--contract", required=True)
    paper_quote.add_argument("--json", default=None)
    _add_json_output_args(paper_quote)
    paper_quote.set_defaults(func=run_paper_quote)
    paper_quote_limit = paper_sub.add_parser("quote-limit", parents=[common], help="Fetch the side-aware best bid/ask limit.")
    paper_quote_limit.add_argument("--market", default=None)
    paper_quote_limit.add_argument("--contract", required=True)
    paper_quote_limit.add_argument("--side", required=True, choices=["BUY", "SELL", "BACK", "LAY"])
    paper_quote_limit.add_argument("--json", default=None)
    _add_json_output_args(paper_quote_limit)
    paper_quote_limit.set_defaults(func=run_paper_quote_limit)
    paper_impact = paper_sub.add_parser("impact", parents=[common], help="Preview position impact without recording an order.")
    paper_impact.add_argument("--market", default=None)
    paper_impact.add_argument("--contract", required=True)
    paper_impact.add_argument("--side", required=True, choices=["BUY", "SELL", "BACK", "LAY"])
    paper_impact.add_argument("--size", required=True)
    paper_impact.add_argument("--limit-price", default=None)
    paper_impact.add_argument("--metadata", action="append", type=_split_key_value, default=[])
    paper_impact.add_argument("--json", default=None)
    _add_json_output_args(paper_impact)
    paper_impact.set_defaults(func=run_paper_impact)
    paper_order = paper_sub.add_parser("order", parents=[common], help="Submit a guarded paper order.")
    paper_order.add_argument("--market", default=None)
    paper_order.add_argument("--contract", required=True)
    paper_order.add_argument("--side", required=True, choices=["BUY", "SELL", "BACK", "LAY"])
    paper_order.add_argument("--size", required=True)
    paper_order.add_argument("--limit-price", default=None)
    paper_order.add_argument("--metadata", action="append", type=_split_key_value, default=[])
    paper_order.add_argument("--json", default=None)
    paper_order.add_argument("--marks-file", default="", help="Persistent CLI paper-mark sidecar; defaults beside --config.")
    _add_json_output_args(paper_order)
    paper_order.set_defaults(func=run_paper_order)
    paper_history = paper_sub.add_parser("use-history", parents=[common], help="Return an order form payload from a paper history record.")
    paper_history.add_argument("record_id")
    _add_json_output_args(paper_history)
    paper_history.set_defaults(func=run_paper_use_history)
    paper_position = paper_sub.add_parser("use-position", parents=[common], help="Return a close-order payload from a paper position.")
    paper_position.add_argument("--market", required=True)
    paper_position.add_argument("--contract", required=True)
    _add_json_output_args(paper_position)
    paper_position.set_defaults(func=run_paper_use_position)
    paper_clear = paper_sub.add_parser("clear-history", parents=[common], help="Clear paper history.")
    paper_clear.add_argument("--marks-file", default="", help="Persistent CLI paper-mark sidecar; defaults beside --config.")
    _add_json_output_args(paper_clear)
    paper_clear.set_defaults(func=run_paper_clear_history)
    paper_marks = paper_sub.add_parser("marks", parents=[common], help="Refresh or clear durable CLI paper-position marks.")
    paper_marks_sub = paper_marks.add_subparsers(dest="paper_marks_command", required=True)
    paper_marks_refresh = paper_marks_sub.add_parser("refresh", parents=[common], help="Refresh marks for all open paper positions.")
    paper_marks_refresh.add_argument("--marks-file", default="", help="Persistent CLI paper-mark sidecar; defaults beside --config.")
    _add_json_output_args(paper_marks_refresh)
    paper_marks_refresh.set_defaults(func=run_paper_marks_refresh)
    paper_marks_selected = paper_marks_sub.add_parser("refresh-selected", parents=[common], help="Refresh one open paper-position mark.")
    paper_marks_selected.add_argument("--market", required=True)
    paper_marks_selected.add_argument("--contract", required=True)
    paper_marks_selected.add_argument("--marks-file", default="", help="Persistent CLI paper-mark sidecar; defaults beside --config.")
    _add_json_output_args(paper_marks_selected)
    paper_marks_selected.set_defaults(func=run_paper_marks_refresh_selected)
    paper_marks_clear = paper_marks_sub.add_parser("clear", parents=[common], help="Clear all durable CLI paper-position marks.")
    paper_marks_clear.add_argument("--marks-file", default="", help="Persistent CLI paper-mark sidecar; defaults beside --config.")
    _add_json_output_args(paper_marks_clear)
    paper_marks_clear.set_defaults(func=run_paper_marks_clear)
    paper_marks_clear_selected = paper_marks_sub.add_parser("clear-selected", parents=[common], help="Clear one durable CLI paper-position mark.")
    paper_marks_clear_selected.add_argument("--market", required=True)
    paper_marks_clear_selected.add_argument("--contract", required=True)
    paper_marks_clear_selected.add_argument("--marks-file", default="", help="Persistent CLI paper-mark sidecar; defaults beside --config.")
    _add_json_output_args(paper_marks_clear_selected)
    paper_marks_clear_selected.set_defaults(func=run_paper_marks_clear_selected)

    deps = subparsers.add_parser("dependencies", parents=[common], aliases=["deps"], help="Check local dependency install status.")
    deps.add_argument("--latest", action="store_true", help="Also query PyPI for latest versions.")
    _add_json_output_args(deps)
    deps.set_defaults(func=run_dependencies)

    search = subparsers.add_parser("polymarket-user-search", parents=[common], help="Search public Polymarket profiles.")
    search.add_argument("--query", "-q", required=True)
    search.add_argument("--limit", type=int, default=10)
    _add_json_output_args(search)
    search.set_defaults(func=run_polymarket_user_search)

    user_mdd = subparsers.add_parser("polymarket-user-mdd", parents=[common], help="Compute one Polymarket wallet MDD payload.")
    user_mdd.add_argument("--wallet", required=True)
    user_mdd.add_argument("--mode", default="fast", choices=["fast", "mark_replay"])
    user_mdd.add_argument("--closed-limit", default="500")
    user_mdd.add_argument("--open-limit", default="500")
    user_mdd.add_argument("--activity-limit", default="1000")
    user_mdd.add_argument("--trade-limit", default="1000")
    user_mdd.add_argument("--include-open", action=argparse.BooleanOptionalAction, default=True)
    user_mdd.add_argument("--equity-base-usd", default=None)
    user_mdd.add_argument("--max-points", default="50")
    user_mdd.add_argument("--cache-ttl-seconds", default="0")
    user_mdd.add_argument("--mark-replay-token-limit", default="10")
    user_mdd.add_argument("--mark-replay-point-limit", default="5000")
    user_mdd.add_argument("--mark-replay-interval", default="1h")
    user_mdd.add_argument("--mark-replay-fidelity", default="60")
    user_mdd.add_argument("--include-accounting", action="store_true")
    user_mdd.add_argument("--accounting-timeout", default="30")
    _add_json_output_args(user_mdd)
    user_mdd.set_defaults(func=run_polymarket_user_mdd)

    readiness = subparsers.add_parser("polymarket-readiness", parents=[common], help="Show Polymarket CLOB/live-validation readiness.")
    _add_json_output_args(readiness)
    readiness.set_defaults(func=run_polymarket_readiness)

    live_reports = subparsers.add_parser(
        "polymarket-live-reports",
        parents=[common],
        help="Manage local redacted Polymarket live-validation reports without placing orders.",
    )
    live_reports_sub = live_reports.add_subparsers(dest="live_reports_command", required=True)
    live_reports_list = live_reports_sub.add_parser("list", parents=[common], help="List stored redacted live-validation reports.")
    live_reports_list.add_argument("--include-payload", action="store_true")
    _add_json_output_args(live_reports_list)
    live_reports_list.set_defaults(func=run_polymarket_live_reports_list)
    live_reports_open = live_reports_sub.add_parser("open", parents=[common], help="Open one stored live-validation report.")
    live_reports_open.add_argument("key")
    _add_json_output_args(live_reports_open)
    live_reports_open.set_defaults(func=run_polymarket_live_reports_open)
    live_reports_store = live_reports_sub.add_parser(
        "store",
        aliases=["import", "snapshot"],
        parents=[common],
        help="Store a local readiness snapshot or import a redacted report JSON file.",
    )
    live_reports_store.add_argument("--report-file", default="", help="Existing report JSON to validate and import.")
    live_reports_store.add_argument("--label", default="")
    live_reports_store.add_argument("--source", default="")
    live_reports_store.add_argument("--allow-duplicate", action="store_true")
    _add_json_output_args(live_reports_store)
    live_reports_store.set_defaults(func=run_polymarket_live_reports_store)
    live_reports_delete = live_reports_sub.add_parser("delete", parents=[common], help="Delete one stored live-validation report.")
    live_reports_delete.add_argument("key")
    _add_json_output_args(live_reports_delete)
    live_reports_delete.set_defaults(func=run_polymarket_live_reports_delete)
    live_reports_export = live_reports_sub.add_parser("export", parents=[common], help="Export one stored report as JSON.")
    live_reports_export.add_argument("key")
    _add_json_output_args(live_reports_export)
    live_reports_export.set_defaults(func=run_polymarket_live_reports_export)
    live_reports_review = live_reports_sub.add_parser("review", parents=[common], help="Export a redacted report review bundle.")
    live_reports_review.add_argument("key")
    live_reports_review.add_argument("--format", choices=["json", "markdown"], default="json")
    _add_json_output_args(live_reports_review)
    live_reports_review.set_defaults(func=run_polymarket_live_reports_review)

    live_decisions = subparsers.add_parser(
        "polymarket-live-decisions",
        parents=[common],
        help="Inspect or record local live-validation promotion decisions without automatic promotion.",
    )
    live_decisions_sub = live_decisions.add_subparsers(dest="live_decisions_command", required=True)
    live_decisions_list = live_decisions_sub.add_parser("list", parents=[common])
    live_decisions_list.add_argument("--report-key", default="")
    _add_json_output_args(live_decisions_list)
    live_decisions_list.set_defaults(func=run_polymarket_live_decisions_list)
    live_decisions_record = live_decisions_sub.add_parser("record", parents=[common], help="Record a guarded human review decision.")
    live_decisions_record.add_argument("--report-key", required=True)
    live_decisions_record.add_argument("--payload-hash", required=True)
    live_decisions_record.add_argument("--target-tier", required=True)
    live_decisions_record.add_argument("--decision", required=True, choices=["accepted", "rejected"])
    live_decisions_record.add_argument("--reviewer-note", required=True)
    live_decisions_record.add_argument("--review-bundle-hash", required=True)
    live_decisions_record.add_argument("--reviewer", default="")
    _add_json_output_args(live_decisions_record)
    live_decisions_record.set_defaults(func=run_polymarket_live_decisions_record)
    live_decisions_export = live_decisions_sub.add_parser("export", parents=[common], help="Export the decision ledger.")
    live_decisions_export.add_argument("--report-key", default="")
    live_decisions_export.add_argument("--format", choices=["json", "markdown"], default="json")
    _add_json_output_args(live_decisions_export)
    live_decisions_export.set_defaults(func=run_polymarket_live_decisions_export)

    live_proposal = subparsers.add_parser(
        "polymarket-promotion-proposal",
        parents=[common],
        help="Inspect local read-only Polymarket coverage promotion proposals and snapshots.",
    )
    live_proposal_sub = live_proposal.add_subparsers(dest="live_proposal_command", required=True)
    live_proposal_show = live_proposal_sub.add_parser("show", parents=[common])
    live_proposal_show.add_argument("--target-tier", default="")
    _add_json_output_args(live_proposal_show)
    live_proposal_show.set_defaults(func=run_polymarket_live_proposal_show)
    live_proposal_export = live_proposal_sub.add_parser("export", parents=[common])
    live_proposal_export.add_argument("--target-tier", default="")
    live_proposal_export.add_argument("--format", choices=["json", "markdown"], default="json")
    _add_json_output_args(live_proposal_export)
    live_proposal_export.set_defaults(func=run_polymarket_live_proposal_export)
    live_snapshots = live_proposal_sub.add_parser("snapshots", parents=[common], help="Manage read-only promotion-proposal snapshots.")
    live_snapshots_sub = live_snapshots.add_subparsers(dest="live_snapshots_command", required=True)
    live_snapshots_list = live_snapshots_sub.add_parser("list", parents=[common])
    _add_json_output_args(live_snapshots_list)
    live_snapshots_list.set_defaults(func=run_polymarket_live_snapshots_list)
    live_snapshots_store = live_snapshots_sub.add_parser("store", parents=[common])
    live_snapshots_store.add_argument("--target-tier", default="")
    live_snapshots_store.add_argument("--label", default="")
    live_snapshots_store.add_argument("--source", default="cli")
    _add_json_output_args(live_snapshots_store)
    live_snapshots_store.set_defaults(func=run_polymarket_live_snapshots_store)
    live_snapshots_open = live_snapshots_sub.add_parser("open", parents=[common])
    live_snapshots_open.add_argument("key")
    _add_json_output_args(live_snapshots_open)
    live_snapshots_open.set_defaults(func=run_polymarket_live_snapshots_open)
    live_snapshots_diff = live_snapshots_sub.add_parser("diff", parents=[common])
    live_snapshots_diff.add_argument("key")
    live_snapshots_diff.add_argument("--format", choices=["json", "markdown"], default="json")
    _add_json_output_args(live_snapshots_diff)
    live_snapshots_diff.set_defaults(func=run_polymarket_live_snapshots_diff)
    live_snapshots_delete = live_snapshots_sub.add_parser("delete", parents=[common])
    live_snapshots_delete.add_argument("key")
    _add_json_output_args(live_snapshots_delete)
    live_snapshots_delete.set_defaults(func=run_polymarket_live_snapshots_delete)
    live_snapshots_export = live_snapshots_sub.add_parser("export", parents=[common])
    live_snapshots_export.add_argument("key")
    live_snapshots_export.add_argument("--format", choices=["json", "markdown"], default="json")
    _add_json_output_args(live_snapshots_export)
    live_snapshots_export.set_defaults(func=run_polymarket_live_snapshots_export)

    mdd_cache = subparsers.add_parser("polymarket-mdd-cache", parents=[common], help="Inspect or purge cached Polymarket MDD audits.")
    mdd_cache_sub = mdd_cache.add_subparsers(dest="mdd_cache_command", required=True)
    mdd_cache_list = mdd_cache_sub.add_parser("list", parents=[common])
    mdd_cache_list.add_argument("--include-expired", action=argparse.BooleanOptionalAction, default=True)
    _add_json_output_args(mdd_cache_list)
    mdd_cache_list.set_defaults(func=run_polymarket_mdd_cache_list)
    mdd_cache_health = mdd_cache_sub.add_parser("health", parents=[common])
    _add_json_output_args(mdd_cache_health)
    mdd_cache_health.set_defaults(func=run_polymarket_mdd_cache_health)
    mdd_cache_purge = mdd_cache_sub.add_parser("purge", parents=[common])
    mdd_cache_purge.add_argument("--key", default="")
    mdd_cache_purge.add_argument("--expired-only", action="store_true")
    mdd_cache_purge.add_argument("--all", action="store_true")
    _add_json_output_args(mdd_cache_purge)
    mdd_cache_purge.set_defaults(func=run_polymarket_mdd_cache_purge)

    serve = subparsers.add_parser("serve", parents=[common], help="Run the local HTTP API/web GUI server from CLI.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--frontend-dir", type=Path, default=DEFAULT_FRONTEND_DIR)
    serve.set_defaults(func=run_serve)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        if os.environ.get("MARKET_SENTINEL_CLI_DEBUG"):
            raise
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
