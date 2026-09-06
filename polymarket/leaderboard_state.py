from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Dict, Iterator, Mapping, Optional

from .leaderboard import LEADERBOARD_MAX_OFFSET, performance_ratio_metadata, wallet_membership_fingerprint


_SORT_COLUMNS = {
    "roi_pct": "roi_pct",
    "pnl_usd": "pnl_usd",
    "volume_usd": "volume_usd",
    "mdd_pct": "mdd_pct",
    "mdd_usd": "mdd_usd",
}


class LeaderboardStateBusyError(RuntimeError):
    """Another scan owns the state database's writer lock."""


def leaderboard_writer_lock_path(path: Path) -> Path:
    target = path.expanduser().resolve()
    return target.with_name(f".{target.name}.writer.lock")


def _acquire_writer_lock(path: Path) -> BinaryIO:
    lock_path = leaderboard_writer_lock_path(path)
    if lock_path.is_symlink():
        raise ValueError(f"Leaderboard writer lock must not be a symbolic link: {lock_path}")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    handle = os.fdopen(descriptor, "r+b")
    try:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise LeaderboardStateBusyError(
                f"Cannot acquire the leaderboard writer lock for {path}. "
                "Another scan may be running; use status/export or wait for it to stop."
            ) from exc
        return handle
    except BaseException:
        handle.close()
        raise


class LeaderboardStateStore:
    """Durable local state for large leaderboard scans and MDD enrichment."""

    def __init__(self, path: Path | str, *, read_only: bool = False) -> None:
        self.path = Path(path).expanduser().resolve()
        self._writer_lock: Optional[BinaryIO] = None
        self.read_only = read_only
        try:
            if read_only:
                self.connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
                self.connection.execute("PRAGMA query_only=ON")
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._writer_lock = _acquire_writer_lock(self.path)
                self.connection = sqlite3.connect(self.path)
            self.connection.row_factory = sqlite3.Row
            if read_only:
                index = self.connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'rows_wallet_unique_idx'"
                ).fetchone()
                if index is None:
                    raise ValueError("Legacy leaderboard state requires migration; resume the scan once before status/export.")
            else:
                self._create_schema()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        try:
            connection = getattr(self, "connection", None)
            if connection is not None:
                connection.close()
        finally:
            if self._writer_lock is not None:
                # The OS releases ownership even after an ungraceful process exit.
                self._writer_lock.close()
                self._writer_lock = None

    @contextmanager
    def snapshot(self) -> Iterator[None]:
        """Keep counts, provenance and streamed rows on one SQLite read snapshot."""
        started = not self.connection.in_transaction
        if started:
            self.connection.execute("BEGIN")
        try:
            yield
        finally:
            if started:
                self.connection.rollback()

    def _create_schema(self) -> None:
        # A successful page/MDD commit must request a storage sync, not wait
        # until a later WAL checkpoint. Verify settings before any migration.
        for setting, value, expected in (
            ("journal_mode", "WAL", "wal"),
            ("synchronous", "FULL", 2),
            ("fullfsync", "ON", 1),
        ):
            self.connection.execute(f"PRAGMA {setting}={value}")
            row = self.connection.execute(f"PRAGMA {setting}").fetchone()
            if row is None or row[0] != expected:
                raise RuntimeError(f"Leaderboard state requires SQLite {setting}={value}; refusing to write with weaker durability.")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pages (
                page_offset INTEGER PRIMARY KEY,
                page_limit INTEGER NOT NULL,
                row_count INTEGER NOT NULL,
                fingerprint TEXT NOT NULL DEFAULT '',
                wallet_fingerprint TEXT NOT NULL DEFAULT '',
                saved_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rows (
                id INTEGER PRIMARY KEY,
                page_offset INTEGER NOT NULL,
                page_index INTEGER NOT NULL,
                rank INTEGER,
                display_name TEXT NOT NULL,
                wallet TEXT NOT NULL,
                pnl_usd REAL,
                volume_usd REAL,
                roi_pct REAL,
                trade_count INTEGER,
                raw_json TEXT NOT NULL,
                mdd_status TEXT NOT NULL DEFAULT 'pending',
                mdd_attempts INTEGER NOT NULL DEFAULT 0,
                mdd_usd REAL,
                mdd_pct REAL,
                mdd_method TEXT,
                mdd_source TEXT,
                mdd_json TEXT,
                mdd_error TEXT,
                UNIQUE(page_offset, page_index)
            );
            CREATE INDEX IF NOT EXISTS rows_roi_idx ON rows(roi_pct);
            CREATE INDEX IF NOT EXISTS rows_pnl_idx ON rows(pnl_usd);
            CREATE INDEX IF NOT EXISTS rows_volume_idx ON rows(volume_usd);
            CREATE INDEX IF NOT EXISTS rows_mdd_pct_idx ON rows(mdd_pct);
            CREATE INDEX IF NOT EXISTS rows_mdd_status_idx ON rows(mdd_status);
            """
        )
        page_columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(pages)")
        }
        if "fingerprint" not in page_columns:
            self.connection.execute("ALTER TABLE pages ADD COLUMN fingerprint TEXT NOT NULL DEFAULT ''")
        migrate_memberships = "wallet_fingerprint" not in page_columns
        if migrate_memberships:
            self.connection.execute("ALTER TABLE pages ADD COLUMN wallet_fingerprint TEXT NOT NULL DEFAULT ''")
        self.connection.execute("CREATE INDEX IF NOT EXISTS pages_fingerprint_idx ON pages(fingerprint)")
        self.connection.execute("CREATE INDEX IF NOT EXISTS pages_wallet_fingerprint_idx ON pages(wallet_fingerprint)")
        wallet_index = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'rows_wallet_unique_idx'"
        ).fetchone()
        if wallet_index is None:
            # Migrate old scans atomically, keeping the earliest observed row per wallet.
            with self.connection:
                self.connection.execute("UPDATE rows SET wallet = LOWER(TRIM(wallet))")
                self.connection.execute(
                    """
                    DELETE FROM rows WHERE id IN (
                        SELECT id FROM (
                            SELECT id, ROW_NUMBER() OVER (
                                PARTITION BY wallet ORDER BY page_offset, page_index, id
                            ) AS occurrence FROM rows WHERE wallet != ''
                        ) WHERE occurrence > 1
                    )
                    """
                )
                self.connection.execute(
                    "CREATE UNIQUE INDEX rows_wallet_unique_idx ON rows(wallet) WHERE wallet != ''"
                )
        if migrate_memberships:
            # Only reconstruct complete retained pages within the documented source window.
            for page in self.connection.execute("SELECT page_offset, row_count FROM pages WHERE page_offset <= ?", (LEADERBOARD_MAX_OFFSET,)):
                rows = [dict(row) for row in self.connection.execute("SELECT wallet FROM rows WHERE page_offset = ?", (page["page_offset"],))]
                if len(rows) == page["row_count"]:
                    self.connection.execute("UPDATE pages SET wallet_fingerprint = ? WHERE page_offset = ?", (
                        wallet_membership_fingerprint(rows), page["page_offset"],
                    ))
        self.connection.commit()

    def prepare(self, signature: Mapping[str, Any], *, resume: bool) -> None:
        serialized = json.dumps(dict(signature), sort_keys=True, separators=(",", ":"))
        existing = self._metadata("signature")
        now = str(int(time.time()))
        with self.connection:
            if resume:
                if existing and existing != serialized:
                    raise ValueError("State database was created with different leaderboard scan settings.")
                if not existing:
                    self._set_metadata("signature", serialized)
                if not self._metadata("started_at"):
                    self._set_metadata("started_at", now)
                self._set_metadata("last_updated_at", now)
            else:
                self.connection.execute("DELETE FROM pages")
                self.connection.execute("DELETE FROM rows")
                self.connection.execute("DELETE FROM metadata")
                self._set_metadata("signature", serialized)
                self._set_metadata("scan_complete", "0")
                self._set_metadata("started_at", now)
                self._set_metadata("last_updated_at", now)

    def prepare_mdd(self, signature: Mapping[str, Any]) -> int:
        """Invalidate enrichment, not fetched pages, when calculation inputs change."""
        serialized = json.dumps(dict(signature), sort_keys=True, separators=(",", ":"), allow_nan=False)
        if self._metadata("mdd_signature") == serialized:
            return 0
        with self.connection:
            invalidated = self.connection.execute(
                """
                UPDATE rows SET mdd_status = 'pending', mdd_attempts = 0,
                    mdd_usd = NULL, mdd_pct = NULL, mdd_method = NULL,
                    mdd_source = NULL, mdd_json = NULL, mdd_error = NULL
                WHERE mdd_status != 'pending' OR mdd_json IS NOT NULL
                """
            ).rowcount
            self._set_metadata("mdd_signature", serialized)
            self._set_metadata("last_updated_at", str(int(time.time())))
        return invalidated

    def _metadata(self, key: str) -> str:
        row = self.connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row is not None else ""

    def _set_metadata(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT INTO metadata(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def progress(self) -> Dict[str, Any]:
        with self.snapshot():
            return self._snapshot_progress()

    def _snapshot_progress(self) -> Dict[str, Any]:
        row_count = int(self.connection.execute("SELECT COUNT(*) AS count FROM rows").fetchone()["count"])
        wallet_count = int(self.connection.execute("SELECT COUNT(*) FROM rows WHERE wallet != ''").fetchone()[0])
        page_stats = self.connection.execute(
            "SELECT COUNT(*) AS count, COALESCE(SUM(row_count), 0) AS scanned, "
            "MIN(saved_at) AS started_at, MAX(saved_at) AS updated_at FROM pages"
        ).fetchone()
        page_count = int(page_stats["count"])
        scanned_count = int(page_stats["scanned"])
        done = int(
            self.connection.execute("SELECT COUNT(*) AS count FROM rows WHERE mdd_status = 'done'").fetchone()["count"]
        )
        available = int(self.connection.execute(
            "SELECT COUNT(*) FROM rows WHERE mdd_status = 'done' AND (mdd_usd IS NOT NULL OR mdd_pct IS NOT NULL)"
        ).fetchone()[0])
        failed = int(
            self.connection.execute("SELECT COUNT(*) AS count FROM rows WHERE mdd_status = 'error'").fetchone()["count"]
        )
        last_page = self.connection.execute(
            "SELECT page_offset, page_limit, row_count FROM pages ORDER BY page_offset DESC LIMIT 1"
        ).fetchone()
        next_offset = 0
        if last_page is not None:
            next_offset = int(last_page["page_offset"]) + int(last_page["row_count"])
        page_started_at = str(page_stats["started_at"] or "")
        page_updated_at = str(page_stats["updated_at"] or "")
        started_at = self._metadata("started_at") or page_started_at
        last_updated_at = self._metadata("last_updated_at") or page_updated_at
        return {
            "rows": row_count,
            "scanned": scanned_count,
            "unique_wallets": wallet_count,
            "duplicate_rows": max(0, scanned_count - row_count),
            "pages": page_count,
            "mdd_done": done,
            "mdd_available": available,
            "mdd_unavailable": done - available,
            "mdd_errors": failed,
            "mdd_pending": max(0, row_count - done - failed),
            "next_offset": next_offset,
            "scan_complete": self._metadata("scan_complete") == "1",
            "stop_reason": self._metadata("stop_reason"),
            "started_at": started_at,
            "last_updated_at": last_updated_at,
        }

    def status(self) -> Dict[str, Any]:
        with self.snapshot():
            return self._snapshot_status()

    def _snapshot_status(self) -> Dict[str, Any]:
        signature_text = self._metadata("signature")
        try:
            signature = json.loads(signature_text) if signature_text else {}
        except json.JSONDecodeError:
            signature = {"invalid": True}
        progress = self.progress()
        mdd_signature_text = self._metadata("mdd_signature")
        mdd_signature = json.loads(mdd_signature_text) if mdd_signature_text else None
        return {
            "state_db": str(self.path),
            "database_bytes": self.path.stat().st_size if self.path.exists() else 0,
            "signature": signature if isinstance(signature, Mapping) else {},
            "mdd_signature": mdd_signature,
            **progress,
        }

    def record_page(self, offset: int, limit: int, rows: list[Mapping[str, Any]]) -> bool:
        clean_offset = max(0, int(offset))
        clean_limit = max(1, int(limit))
        fingerprint = self._page_fingerprint(rows)
        wallet_fingerprint = wallet_membership_fingerprint(rows)
        with self.connection:
            saved_page = self.connection.execute(
                "SELECT fingerprint FROM pages WHERE page_offset = ?", (clean_offset,)
            ).fetchone()
            if saved_page is not None:
                if saved_page["fingerprint"] != fingerprint:
                    raise ValueError("Cannot overwrite an already saved leaderboard page with different observations.")
                return True
            duplicate = self.connection.execute(
                "SELECT page_offset FROM pages WHERE (fingerprint = ? OR (? != '' AND wallet_fingerprint = ?)) AND page_offset != ? LIMIT 1",
                (fingerprint, wallet_fingerprint, wallet_fingerprint, clean_offset),
            ).fetchone()
            if rows and duplicate is not None:
                self._set_metadata("scan_complete", "1")
                self._set_metadata("stop_reason", "repeated_page")
                self._set_metadata("stop_offset", str(clean_offset))
                self._set_metadata("repeated_page_offset", str(int(duplicate["page_offset"])))
                self._set_metadata("last_updated_at", str(int(time.time())))
                return False
            self.connection.execute(
                "INSERT OR REPLACE INTO pages(page_offset, page_limit, row_count, fingerprint, wallet_fingerprint, saved_at) VALUES (?, ?, ?, ?, ?, ?)",
                (clean_offset, clean_limit, len(rows), fingerprint, wallet_fingerprint, int(time.time())),
            )
            self.connection.executemany(
                """
                INSERT INTO rows(page_offset, page_index, rank, display_name, wallet, pnl_usd, volume_usd, roi_pct, trade_count, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(wallet) WHERE wallet != '' DO NOTHING
                """,
                [
                    (
                        clean_offset,
                        index,
                        row.get("rank"),
                        str(row.get("display_name") or "-"),
                        str(row.get("wallet") or "").strip().lower(),
                        row.get("pnl_usd"),
                        row.get("volume_usd"),
                        row.get("roi_pct"),
                        row.get("trade_count"),
                        json.dumps(dict(row.get("raw") or {}), separators=(",", ":"), sort_keys=True),
                    )
                    for index, row in enumerate(rows)
                ],
            )
            if len(rows) < clean_limit:
                self._set_metadata("scan_complete", "1")
                self._set_metadata("stop_reason", "end_of_results")
            else:
                self._set_metadata("stop_reason", "")
            self._set_metadata("last_updated_at", str(int(time.time())))
        return True

    def stop_at_upstream_limit(self) -> None:
        with self.connection:
            self._set_metadata("scan_complete", "1")
            self._set_metadata("stop_reason", "upstream_offset_limit")
            self._set_metadata("last_updated_at", str(int(time.time())))

    @staticmethod
    def _page_fingerprint(rows: list[Mapping[str, Any]]) -> str:
        canonical = json.dumps(list(rows), default=str, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def candidate_count(self, filters: Mapping[str, Optional[float]]) -> int:
        where, values = self._where(filters, require_mdd=False)
        # `where` is built only from the fixed column tuple in `_where`; values remain bound parameters.
        row = self.connection.execute(f"SELECT COUNT(*) AS count FROM rows {where}", values).fetchone()  # noqa: S608
        return int(row["count"])

    def iter_mdd_candidates(
        self,
        filters: Mapping[str, Optional[float]],
        *,
        sort: str,
        direction: str,
        limit: Optional[int],
    ) -> Iterator[Dict[str, Any]]:
        where, values = self._where(filters, require_mdd=False)
        order = self._order_clause(sort, direction, candidate=True)
        # `where` and `order` are both generated from fixed allowlists; user input is never interpolated.
        query = f"SELECT * FROM rows {where} {order}"  # noqa: S608
        if limit is not None:
            query += " LIMIT ?"
            values.append(int(limit))
        for row in self.connection.execute(query, values):
            yield self._decode_row(row)

    def set_mdd(self, row_id: int, payload: Optional[Mapping[str, Any]], error: Optional[BaseException] = None) -> None:
        if payload is None:
            with self.connection:
                self.connection.execute(
                    "UPDATE rows SET mdd_status = 'error', mdd_attempts = mdd_attempts + 1, mdd_error = ? WHERE id = ?",
                    (str(error or "MDD unavailable")[:512], int(row_id)),
                )
                self._set_metadata("last_updated_at", str(int(time.time())))
            return

        summary = self._mdd_summary(payload)
        if summary.get("mdd_available") is False:
            summary.update(mdd_usd=None, mdd_pct=None)
        mark_replay = summary.get("mark_replay") or {}
        accounting = summary.get("accounting_snapshot") or {}
        source = str(
            (accounting.get("status") if isinstance(accounting, Mapping) else "")
            or (mark_replay.get("status") if isinstance(mark_replay, Mapping) else "")
            or summary.get("mdd_method")
            or ""
        )
        with self.connection:
            self.connection.execute(
                """
                UPDATE rows
                SET mdd_status = 'done', mdd_attempts = mdd_attempts + 1, mdd_usd = ?, mdd_pct = ?,
                    mdd_method = ?, mdd_source = ?, mdd_json = ?, mdd_error = NULL
                WHERE id = ?
                """,
                (
                    summary.get("mdd_usd"),
                    summary.get("mdd_pct"),
                    summary.get("mdd_method"),
                    source,
                    json.dumps(summary, separators=(",", ":"), sort_keys=True),
                    int(row_id),
                ),
            )
            self._set_metadata("last_updated_at", str(int(time.time())))

    @staticmethod
    def _mdd_summary(payload: Mapping[str, Any]) -> Dict[str, Any]:
        """Keep result/provenance fields required for resume and export, not full point history."""
        keys = (
            "version",
            "calculation_version",
            "mdd_usd",
            "mdd_pct",
            "mdd_available",
            "mdd_method",
            "mdd_pct_basis",
            "mdd_scope",
            "mdd_account_equity_verified",
            "mdd_history_status",
            "mdd_history_coverage",
            "mdd_history_capped_sources",
            "mdd_history_excluded_sources",
            "mdd_source_quality",
            "mdd_unavailable_reasons",
            "equity_base_usd",
            "equity_base_source",
            "public_capital_basis_usd",
            "position_capital_basis",
            "peak_value",
            "trough_value",
            "peak_timestamp",
            "trough_timestamp",
            "pct_drawdown_usd",
            "pct_peak_value",
            "pct_trough_value",
            "pct_peak_timestamp",
            "pct_trough_timestamp",
            "drawdown_baseline",
            "points_total",
            "data_counts",
            "assumptions",
            "limitations",
        )
        summary = {key: payload.get(key) for key in keys if key in payload}
        for key in ("mark_replay", "accounting_snapshot"):
            value = payload.get(key)
            if isinstance(value, Mapping):
                summary[key] = {
                    item_key: value.get(item_key)
                    for item_key in (
                        "status", "source", "available", "warning_count", "warnings", "limitations",
                        "incomplete_reasons", "trade_events_replayed", "trades_without_timestamp",
                        "trades_without_size_or_price", "negative_inventory_events", "timeline_truncated",
                        "display_points_truncated", "complete",
                    )
                    if item_key in value
                }
        return summary

    def result_count(self, filters: Mapping[str, Optional[float]], *, require_mdd: bool) -> int:
        where, values = self._where(filters, require_mdd=require_mdd)
        # `where` is built only from the fixed column tuple in `_where`; values remain bound parameters.
        row = self.connection.execute(f"SELECT COUNT(*) AS count FROM rows {where}", values).fetchone()  # noqa: S608
        return int(row["count"])

    def iter_results(
        self,
        filters: Mapping[str, Optional[float]],
        *,
        require_mdd: bool,
        sort: str,
        direction: str,
        limit: Optional[int],
    ) -> Iterator[Dict[str, Any]]:
        where, values = self._where(filters, require_mdd=require_mdd)
        # `_where` and `_order_clause` produce fixed SQL fragments; user values are passed separately.
        query = f"SELECT * FROM rows {where} {self._order_clause(sort, direction)}"  # noqa: S608
        if limit is not None:
            query += " LIMIT ?"
            values.append(int(limit))
        for row in self.connection.execute(query, values):
            yield self._decode_row(row)

    def _where(self, filters: Mapping[str, Optional[float]], *, require_mdd: bool) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        for column, minimum_key, maximum_key in (
            ("pnl_usd", "min_pnl_usd", "max_pnl_usd"),
            ("volume_usd", "min_volume_usd", "max_volume_usd"),
            ("roi_pct", "min_roi_pct", "max_roi_pct"),
        ):
            minimum = filters.get(minimum_key)
            maximum = filters.get(maximum_key)
            if minimum is not None:
                clauses.append(f"{column} >= ?")
                values.append(minimum)
            if maximum is not None:
                clauses.append(f"{column} <= ?")
                values.append(maximum)
        if require_mdd:
            clauses.append("mdd_status = 'done'")
            clauses.append("(mdd_usd IS NOT NULL OR mdd_pct IS NOT NULL)")
            for column, minimum_key, maximum_key in (
                ("mdd_usd", "min_mdd_usd", "max_mdd_usd"),
                ("mdd_pct", "min_mdd_pct", "max_mdd_pct"),
            ):
                minimum = filters.get(minimum_key)
                maximum = filters.get(maximum_key)
                if minimum is not None:
                    clauses.append(f"{column} >= ?")
                    values.append(minimum)
                if maximum is not None:
                    clauses.append(f"{column} <= ?")
                    values.append(maximum)
        return ("WHERE " + " AND ".join(clauses)) if clauses else "", values

    @staticmethod
    def _order_clause(sort: str, direction: str, *, candidate: bool = False) -> str:
        column = _SORT_COLUMNS.get(sort, "roi_pct")
        if candidate and column in {"mdd_pct", "mdd_usd"}:
            return "ORDER BY rank ASC, id ASC"
        clean_direction = "ASC" if str(direction).upper() == "ASC" else "DESC"
        return f"ORDER BY ({column} IS NULL) ASC, {column} {clean_direction}, id ASC"

    @staticmethod
    def _decode_row(row: sqlite3.Row) -> Dict[str, Any]:
        result = {
            "id": int(row["id"]),
            "rank": row["rank"],
            "display_name": row["display_name"],
            "wallet": row["wallet"],
            "pnl_usd": row["pnl_usd"],
            "volume_usd": row["volume_usd"],
            "roi_pct": row["roi_pct"],
            **performance_ratio_metadata(row["roi_pct"]),
            "trade_count": row["trade_count"],
            "mdd_usd": row["mdd_usd"],
            "mdd_pct": row["mdd_pct"],
            "mdd_available": row["mdd_status"] == "done",
            "mdd_method": row["mdd_method"] or "",
            "mdd_source": row["mdd_source"] or "",
            "mdd_status": row["mdd_status"],
            "mdd_error": row["mdd_error"] or "",
            "raw": json.loads(str(row["raw_json"] or "{}")),
        }
        if row["mdd_json"]:
            try:
                result.update(json.loads(str(row["mdd_json"])))
            except json.JSONDecodeError:
                pass
        result["id"] = int(row["id"])
        return result
