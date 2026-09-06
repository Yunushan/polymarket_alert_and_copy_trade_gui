from __future__ import annotations

import csv
import io
import math
import re
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional

from . import data_api
from .util import normalize_wallet


MAX_ACCOUNTING_CSV_FILES = 20
MAX_ACCOUNTING_ROWS_PER_FILE = 20000
MAX_ACCOUNTING_TOTAL_ROWS = 50000
MAX_ACCOUNTING_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_ACCOUNTING_MEMBER_BYTES = 8 * 1024 * 1024
MAX_ACCOUNTING_EXPANDED_BYTES = 32 * 1024 * 1024
MAX_ACCOUNTING_ARCHIVE_MEMBERS = 1000
MAX_ACCOUNTING_COLUMNS = 64
MAX_ACCOUNTING_RECORD_CHARS = 65536


class AccountingSnapshotLimitError(ValueError):
    """The snapshot exceeds a bounded parsing resource budget."""


class _BoundedArchiveReader(io.RawIOBase):
    def __init__(self, source: Any, budget: Dict[str, int]) -> None:
        super().__init__()
        self.source = source
        self.budget = budget
        self.bytes_read = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        remaining = min(MAX_ACCOUNTING_MEMBER_BYTES - self.bytes_read, self.budget["remaining"])
        chunk = self.source.read(min(len(buffer), max(remaining + 1, 1)))
        if len(chunk) > remaining:
            raise AccountingSnapshotLimitError("Accounting CSV expanded-byte limit exceeded.")
        self.bytes_read += len(chunk)
        self.budget["remaining"] -= len(chunk)
        buffer[:len(chunk)] = chunk
        return len(chunk)


class _BoundedCsvLines:
    def __init__(self, source: Any) -> None:
        self.source = source
        self.record_chars = 0

    def __iter__(self):
        return self

    def __next__(self) -> str:
        remaining = MAX_ACCOUNTING_RECORD_CHARS - self.record_chars
        line = self.source.readline(remaining + 1)
        if not line:
            raise StopIteration
        if len(line) > remaining:
            raise AccountingSnapshotLimitError("Accounting CSV logical record is too large.")
        self.record_chars += len(line)
        return line

EQUITY_VALUE_KEYS = (
    "equity",
    "total_equity",
    "account_equity",
    "portfolio_value",
    "portfolio",
    "total_value",
    "net_liquidation",
    "net_liq",
    "balance",
    "value",
)
POSITION_CURRENT_VALUE_KEYS = (
    "current_value",
    "market_value",
    "position_value",
    "value",
    "amount",
)
POSITION_REALIZED_KEYS = ("realized_pnl", "realizedpnl", "realized_profit", "realized")
POSITION_CASH_PNL_KEYS = ("cash_pnl", "cashpnl")
POSITION_INITIAL_KEYS = ("initial_value", "total_bought", "cost_basis", "cost", "notional")
DEPOSIT_KEYS = ("deposit", "deposits", "deposit_usd", "deposits_usd", "deposit_usdc")
WITHDRAWAL_KEYS = ("withdrawal", "withdrawals", "withdrawal_usd", "withdrawals_usd", "withdrawal_usdc")
CASH_FLOW_KEYS = ("cash_flow", "cashflow", "net_cash_flow", "net_deposit", "net_deposits", "funding")


def _normalize_key(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    return normalized


def _compact_key(value: str) -> str:
    return _normalize_key(value).replace("_", "")


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None:
            return default
        text = str(value).strip().replace(",", "")
        if not text:
            return default
        if text.startswith("(") and text.endswith(")"):
            text = "-" + text[1:-1]
        number = float(text)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _row_value(row: Mapping[str, Any], keys: Iterable[str]) -> Any:
    compact = {_compact_key(key): value for key, value in row.items()}
    for key in keys:
        normalized = _normalize_key(key)
        if normalized in row:
            return row[normalized]
        value = compact.get(_compact_key(key))
        if value is not None:
            return value
    return None


def _row_float(row: Mapping[str, Any], keys: Iterable[str]) -> Optional[float]:
    return _safe_float(_row_value(row, keys), None)


def _parse_timestamp(value: Any) -> Optional[int]:
    number = _safe_float(value, None)
    if number is not None:
        timestamp = int(number)
        if timestamp > 10_000_000_000:
            timestamp //= 1000
        return timestamp
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _read_csv_rows(source: Any, max_rows: int) -> tuple[List[Dict[str, Any]], bool]:
    lines = _BoundedCsvLines(source)
    reader = csv.reader(lines, strict=True)
    headers = next(reader, [])
    if len(headers) > MAX_ACCOUNTING_COLUMNS:
        raise AccountingSnapshotLimitError("Accounting CSV has too many columns.")
    keys = [_normalize_key(header) for header in headers]
    if len(keys) != len(set(keys)):
        raise ValueError("Accounting CSV contains ambiguous duplicate column names.")
    rows: List[Dict[str, Any]] = []
    while True:
        lines.record_chars = 0
        row = next(reader, None)
        if row is None:
            return rows, False
        if not row:
            continue
        if len(row) > len(keys):
            raise ValueError("Accounting CSV row contains more fields than its header.")
        if len(rows) >= max_rows:
            return rows, True
        # Reuse normalized header strings across rows instead of duplicating them.
        rows.append(dict(zip(keys, row + [None] * (len(keys) - len(row)), strict=True)))


def _read_member_rows(
    archive: zipfile.ZipFile, member: zipfile.ZipInfo, max_rows: int, budget: Dict[str, int]
) -> tuple[List[Dict[str, Any]], bool]:
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            with archive.open(member) as source:
                reader = io.BufferedReader(_BoundedArchiveReader(source, budget))
                with io.TextIOWrapper(reader, encoding=encoding, newline="") as text:
                    return _read_csv_rows(text, max_rows)
        except UnicodeDecodeError:
            if encoding == "latin-1":
                raise
    raise ValueError("Accounting CSV could not be decoded.")


def _timestamp_from_row(row: Mapping[str, Any]) -> Optional[int]:
    return _parse_timestamp(
        _row_value(row, ("timestamp", "time", "datetime", "date", "as_of", "asof", "created_at", "updated_at"))
    )


def _summarize_equity(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    points: List[Dict[str, Any]] = []
    deposits = 0.0
    withdrawals = 0.0
    explicit_cash_flow = 0.0
    rows_with_cash_flow = 0
    for index, row in enumerate(rows):
        equity = _row_float(row, EQUITY_VALUE_KEYS)
        timestamp = _timestamp_from_row(row)
        if equity is not None:
            points.append({"timestamp": timestamp if timestamp is not None else index, "equity_usd": equity})
        deposit = _row_float(row, DEPOSIT_KEYS)
        withdrawal = _row_float(row, WITHDRAWAL_KEYS)
        cash_flow = _row_float(row, CASH_FLOW_KEYS)
        if deposit is not None:
            deposits += max(deposit, 0.0)
            rows_with_cash_flow += 1
        if withdrawal is not None:
            withdrawals += abs(withdrawal)
            rows_with_cash_flow += 1
        if cash_flow is not None:
            explicit_cash_flow += cash_flow
            rows_with_cash_flow += 1
    points.sort(key=lambda item: item["timestamp"] if item["timestamp"] is not None else 0)
    values = [float(point["equity_usd"]) for point in points]
    first = values[0] if values else None
    last = values[-1] if values else None
    observed_change = (last - first) if first is not None and last is not None else None
    net_cash_flow = explicit_cash_flow + deposits - withdrawals
    cash_flow_gap = (observed_change - net_cash_flow) if observed_change is not None and rows_with_cash_flow else None
    return {
        "rows": len(rows),
        "points": points[-50:],
        "points_total": len(points),
        "first_equity_usd": first,
        "last_equity_usd": last,
        "max_equity_usd": max(values) if values else None,
        "min_equity_usd": min(values) if values else None,
        "base_equity_usd": None,
        "base_source": "unavailable_from_point_in_time_snapshot",
        "cash_flows": {
            "deposits_usd": deposits,
            "withdrawals_usd": withdrawals,
            "explicit_cash_flow_usd": explicit_cash_flow,
            "net_cash_flow_usd": net_cash_flow if rows_with_cash_flow else None,
            "rows_with_cash_flow": rows_with_cash_flow,
            "observed_equity_change_usd": observed_change,
            "cash_flow_gap_usd": cash_flow_gap,
            "has_explicit_cash_flows": bool(rows_with_cash_flow),
        },
    }


def _summarize_positions(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    current_value = 0.0
    realized_pnl = 0.0
    cash_pnl = 0.0
    initial_value = 0.0
    current_count = 0
    realized_count = 0
    cash_count = 0
    initial_count = 0
    for row in rows:
        current = _row_float(row, POSITION_CURRENT_VALUE_KEYS)
        realized = _row_float(row, POSITION_REALIZED_KEYS)
        cash = _row_float(row, POSITION_CASH_PNL_KEYS)
        initial = _row_float(row, POSITION_INITIAL_KEYS)
        if current is not None:
            current_value += current
            current_count += 1
        if realized is not None:
            realized_pnl += realized
            realized_count += 1
        if cash is not None:
            cash_pnl += cash
            cash_count += 1
        if initial is not None:
            initial_value += max(initial, 0.0)
            initial_count += 1
    return {
        "rows": len(rows),
        "current_value_usd": current_value if current_count else None,
        "current_value_rows": current_count,
        "realized_pnl_usd": realized_pnl if realized_count else None,
        "realized_pnl_rows": realized_count,
        "cash_pnl_usd": cash_pnl if cash_count else None,
        "cash_pnl_rows": cash_count,
        "initial_value_usd": initial_value if initial_count else None,
        "initial_value_rows": initial_count,
    }


def parse_accounting_snapshot_zip(raw: bytes, *, max_rows_per_file: int = MAX_ACCOUNTING_ROWS_PER_FILE) -> Dict[str, Any]:
    if len(raw) > MAX_ACCOUNTING_ARCHIVE_BYTES:
        raise AccountingSnapshotLimitError("Accounting ZIP exceeds the compressed-byte limit.")
    if max_rows_per_file < 1:
        raise ValueError("Accounting CSV row limit must be positive.")
    row_limit = min(int(max_rows_per_file), MAX_ACCOUNTING_ROWS_PER_FILE)
    budget = {"remaining": MAX_ACCOUNTING_EXPANDED_BYTES}
    complete = True
    warnings: List[str] = []
    csv_files: List[Dict[str, Any]] = []
    equity_rows: List[Dict[str, Any]] = []
    position_rows: List[Dict[str, Any]] = []
    all_rows: List[Dict[str, Any]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
            if len(members) > MAX_ACCOUNTING_ARCHIVE_MEMBERS:
                raise AccountingSnapshotLimitError("Accounting ZIP has too many members.")
            csv_members = [member for member in members if not member.is_dir() and member.filename.lower().endswith(".csv")]
            if len(csv_members) > MAX_ACCOUNTING_CSV_FILES:
                raise AccountingSnapshotLimitError("Accounting ZIP has too many CSV files.")
            if any(member.file_size > MAX_ACCOUNTING_MEMBER_BYTES for member in csv_members):
                raise AccountingSnapshotLimitError("Accounting CSV exceeds the expanded member-byte limit.")
            if sum(member.file_size for member in csv_members) > MAX_ACCOUNTING_EXPANDED_BYTES:
                raise AccountingSnapshotLimitError("Accounting ZIP exceeds the total expanded-byte limit.")
            names = [member.filename.casefold() for member in csv_members]
            if len(names) != len(set(names)):
                raise ValueError("Accounting ZIP contains duplicate CSV member names.")
            for member in csv_members:
                name = member.filename
                rows, truncated = _read_member_rows(
                    archive, member, min(row_limit, MAX_ACCOUNTING_TOTAL_ROWS - len(all_rows)), budget
                )
                if truncated:
                    complete = False
                    warnings.append(f"CSV row budget reached for {name}; snapshot is incomplete.")
                lower_name = name.lower()
                file_info = {"name": name, "rows": len(rows), "truncated": truncated}
                csv_files.append(file_info)
                all_rows.extend(rows)
                if "equity" in lower_name:
                    equity_rows.extend(rows)
                if "position" in lower_name:
                    position_rows.extend(rows)
    except zipfile.BadZipFile:
        return {
            "status": "invalid_zip",
            "complete": False,
            "files": [],
            "equity": _summarize_equity([]),
            "positions": _summarize_positions([]),
            "warnings": ["Accounting snapshot bytes were not a valid ZIP archive."],
        }
    if not equity_rows:
        equity_rows = [row for row in all_rows if _row_float(row, EQUITY_VALUE_KEYS) is not None]
        if equity_rows:
            warnings.append("No equity-named CSV was found; equity values were inferred from available numeric columns.")
    if not position_rows:
        position_rows = [
            row
            for row in all_rows
            if _row_float(row, POSITION_CURRENT_VALUE_KEYS) is not None
            or _row_float(row, POSITION_REALIZED_KEYS) is not None
            or _row_float(row, POSITION_INITIAL_KEYS) is not None
        ]
        if position_rows:
            warnings.append("No positions-named CSV was found; position values were inferred from available numeric columns.")
    status = ("ok" if complete else "partial") if csv_files else "empty"
    return {
        "status": status,
        "complete": complete and bool(csv_files),
        "expanded_bytes_read": MAX_ACCOUNTING_EXPANDED_BYTES - budget["remaining"],
        "limits": {
            "archive_bytes": MAX_ACCOUNTING_ARCHIVE_BYTES,
            "member_bytes": MAX_ACCOUNTING_MEMBER_BYTES,
            "expanded_bytes": MAX_ACCOUNTING_EXPANDED_BYTES,
            "rows_per_file": row_limit,
            "total_rows": MAX_ACCOUNTING_TOTAL_ROWS,
            "columns": MAX_ACCOUNTING_COLUMNS,
            "record_chars": MAX_ACCOUNTING_RECORD_CHARS,
        },
        "files": csv_files,
        "equity": _summarize_equity(equity_rows),
        "positions": _summarize_positions(position_rows),
        "warnings": warnings,
    }


def download_and_parse_accounting_snapshot(wallet: str, *, timeout: float = 30.0) -> Dict[str, Any]:
    normalized_wallet = normalize_wallet(str(wallet or "").strip())
    if not normalized_wallet:
        raise ValueError("user must be a valid 0x wallet/proxyWallet address.")
    raw = data_api.download_accounting_snapshot(normalized_wallet, timeout=timeout)
    payload = parse_accounting_snapshot_zip(raw)
    payload["wallet"] = normalized_wallet
    return payload


def reconcile_mdd_payload_with_accounting(payload: Mapping[str, Any], snapshot: Mapping[str, Any]) -> Dict[str, Any]:
    result = dict(payload)
    equity = snapshot.get("equity") if isinstance(snapshot.get("equity"), Mapping) else {}
    positions = snapshot.get("positions") if isinstance(snapshot.get("positions"), Mapping) else {}
    previous_base = result.get("equity_base_usd")
    previous_pct = result.get("mdd_pct")
    # A statement balance, including its maximum, does not establish capital
    # before the observed PnL window. Later funding/profits cannot rebase losses.
    open_current_value = _safe_float(result.get("open_current_value"), None)
    snapshot_current_value = _safe_float(positions.get("current_value_usd"), None)
    current_delta = (
        snapshot_current_value - open_current_value
        if snapshot_current_value is not None and open_current_value is not None
        else None
    )
    cumulative_realized = _safe_float(result.get("cumulative_realized_pnl"), None)
    snapshot_realized = _safe_float(positions.get("realized_pnl_usd"), None)
    realized_delta = (
        snapshot_realized - cumulative_realized
        if snapshot_realized is not None and cumulative_realized is not None
        else None
    )
    cash_flows = equity.get("cash_flows") if isinstance(equity.get("cash_flows"), Mapping) else {}
    material_gaps = [
        value
        for value in (
            current_delta,
            realized_delta,
            _safe_float(cash_flows.get("cash_flow_gap_usd"), None),
        )
        if value is not None and abs(value) > 0.01
    ]
    result["accounting_snapshot"] = {
        "status": snapshot.get("status", "unknown"),
        "complete": snapshot.get("complete"),
        "files": list(snapshot.get("files", [])) if isinstance(snapshot.get("files"), list) else [],
        "warnings": list(snapshot.get("warnings", [])) if isinstance(snapshot.get("warnings"), list) else [],
        "equity": {
            "rows": equity.get("rows"),
            "first_equity_usd": equity.get("first_equity_usd"),
            "last_equity_usd": equity.get("last_equity_usd"),
            "max_equity_usd": equity.get("max_equity_usd"),
            "min_equity_usd": equity.get("min_equity_usd"),
            "base_equity_usd": None,
            "base_source": "unavailable_from_point_in_time_snapshot",
            "cash_flows": dict(cash_flows),
        },
        "positions": dict(positions),
        "reconciliation": {
            "status": "reconciled_with_gaps" if material_gaps else "reconciled",
            "scope": "point_in_time_comparison",
            "previous_equity_base_usd": previous_base,
            "previous_mdd_pct": previous_pct,
            "mdd_pct_uses_accounting_base": False,
            "percentage_recalculation_status": "not_recalculated_snapshot_is_not_historical_opening_capital",
            "open_current_value_delta_usd": current_delta,
            "realized_pnl_delta_usd": realized_delta,
            "cash_flow_gap_usd": cash_flows.get("cash_flow_gap_usd"),
            "cash_flow_gap_reported": cash_flows.get("cash_flow_gap_usd") is not None,
        },
    }
    return result
