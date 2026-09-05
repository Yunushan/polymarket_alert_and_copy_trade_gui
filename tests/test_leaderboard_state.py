from __future__ import annotations

import tempfile
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from polymarket.leaderboard_state import LeaderboardStateBusyError, LeaderboardStateStore, leaderboard_writer_lock_path


class LeaderboardStateStoreTests(unittest.TestCase):
    def make_store(self) -> LeaderboardStateStore:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = LeaderboardStateStore(Path(temporary.name) / "leaderboard.sqlite3")
        self.addCleanup(store.close)
        store.prepare({"period": "all"}, resume=False)
        return store

    @staticmethod
    def row(wallet: str, rank: int = 1) -> dict:
        return {"wallet": wallet, "rank": rank, "pnl_usd": float(rank), "volume_usd": 100.0, "roi_pct": float(rank)}

    def test_every_writer_requires_wal_full_sync_and_preserves_existing_data(self) -> None:
        store = self.make_store()
        store.record_page(0, 1, [self.row("0xaaa")])
        self.assertEqual(store.connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(store.connection.execute("PRAGMA synchronous").fetchone()[0], 2)
        self.assertEqual(store.connection.execute("PRAGMA fullfsync").fetchone()[0], 1)
        store.close()
        reopened = LeaderboardStateStore(store.path)
        try:
            self.assertEqual(reopened.connection.execute("PRAGMA synchronous").fetchone()[0], 2)
            self.assertEqual(reopened.connection.execute("PRAGMA fullfsync").fetchone()[0], 1)
            self.assertEqual(reopened.progress()["rows"], 1)
        finally:
            reopened.close()

    def test_durability_downgrade_fails_before_schema_writes_and_releases_lock(self) -> None:
        connect = sqlite3.connect
        for setting, requested, downgraded in (
            ("journal_mode", "WAL", "DELETE"),
            ("synchronous", "FULL", "NORMAL"),
            ("fullfsync", "ON", "OFF"),
        ):
            with self.subTest(setting=setting), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "scan.sqlite3"

                class DowngradingConnection(sqlite3.Connection):
                    requested_statement = f"PRAGMA {setting}={requested}"
                    downgraded_statement = f"PRAGMA {setting}={downgraded}"

                    def execute(self, sql, parameters=()):
                        if sql == self.requested_statement:
                            sql = self.downgraded_statement
                        return super().execute(sql, parameters)

                with patch("polymarket.leaderboard_state.sqlite3.connect", side_effect=lambda *args, **kwargs: connect(
                    *args, **kwargs, factory=DowngradingConnection,
                )):
                    with self.assertRaisesRegex(RuntimeError, f"requires SQLite {setting}={requested}"):
                        LeaderboardStateStore(path)
                with connect(path) as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0], 0)
                connection.close()
                recovered = LeaderboardStateStore(path)
                recovered.close()

    def test_sqlite_full_does_not_advance_page_or_mdd_progress(self) -> None:
        for phase in ("page", "mdd"):
            with self.subTest(phase=phase):
                store = self.make_store()
                store.record_page(0, 1, [self.row("0xaaa")])
                row = next(store.iter_mdd_candidates({}, sort="roi_pct", direction="DESC", limit=None))
                before = store.status()
                page_count = store.connection.execute("PRAGMA page_count").fetchone()[0]
                previous_limit = store.connection.execute("PRAGMA max_page_count").fetchone()[0]
                store.connection.execute(f"PRAGMA max_page_count={int(page_count)}")
                try:
                    with self.assertRaisesRegex(sqlite3.OperationalError, "database or disk is full"):
                        if phase == "page":
                            store.record_page(1, 1, [{**self.row("0xbbb"), "raw": {"payload": "x" * 1_048_576}}])
                        else:
                            store.set_mdd(row["id"], {"mdd_pct": 10.0, "limitations": ["x" * 1_048_576]})
                    self.assertFalse(store.connection.in_transaction)
                    self.assertEqual(store.status(), before)
                    self.assertEqual(store.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                finally:
                    store.connection.execute(f"PRAGMA max_page_count={int(previous_limit)}")
                store.record_page(1, 1, [self.row("0xbbb")])
                store.set_mdd(row["id"], {"mdd_pct": 10.0})
                self.assertEqual(store.progress()["rows"], 2)
                self.assertEqual(store.progress()["mdd_done"], 1)

    def test_abrupt_exit_during_page_or_mdd_transaction_preserves_committed_state(self) -> None:
        script = """
import os, sys
from polymarket.leaderboard_state import LeaderboardStateStore
store = LeaderboardStateStore(sys.argv[1])
store.prepare({'period': 'all'}, resume=False)
store.record_page(0, 1, [{'wallet': '0xaaa'}])
set_metadata = store._set_metadata
def interrupt(key, value):
    if key == 'last_updated_at' and store.connection.in_transaction:
        os._exit(18)
    set_metadata(key, value)
store._set_metadata = interrupt
if sys.argv[2] == 'page':
    store.record_page(1, 1, [{'wallet': '0xbbb'}])
else:
    row = next(store.iter_mdd_candidates({}, sort='roi_pct', direction='DESC', limit=None))
    store.set_mdd(row['id'], {'mdd_pct': 10.0})
raise AssertionError('transaction must be interrupted before commit')
"""
        for phase in ("page", "mdd"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "scan.sqlite3"
                result = subprocess.run(
                    [sys.executable, "-B", "-c", script, str(path), phase],
                    cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=20,
                )
                self.assertEqual(result.returncode, 18, result.stderr)
                recovered = LeaderboardStateStore(path)
                try:
                    recovered.prepare({"period": "all"}, resume=True)
                    progress = recovered.progress()
                    self.assertEqual(progress["rows"], 1)
                    self.assertEqual(progress["pages"], 1)
                    self.assertEqual(progress["next_offset"], 1)
                    self.assertEqual(progress["mdd_pending"], 1)
                    self.assertEqual(progress["mdd_done"], 0)
                    self.assertEqual(recovered.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                finally:
                    recovered.close()

    def test_single_writer_ownership_allows_readers_but_rejects_a_second_writer(self) -> None:
        store = self.make_store()
        store.record_page(0, 1, [self.row("0xaaa")])
        with self.assertRaises(LeaderboardStateBusyError):
            LeaderboardStateStore(store.path)
        reader = LeaderboardStateStore(store.path, read_only=True)
        try:
            self.assertEqual(reader.progress()["rows"], 1)
            with self.assertRaises(sqlite3.OperationalError):
                reader.prepare({}, resume=False)
            self.assertEqual(reader.progress()["rows"], 1)
        finally:
            reader.close()

    def test_writer_lock_is_enforced_between_processes(self) -> None:
        store = self.make_store()
        script = """
import sys
from polymarket.leaderboard_state import LeaderboardStateStore, LeaderboardStateBusyError
try:
    store = LeaderboardStateStore(sys.argv[1])
except LeaderboardStateBusyError:
    print('busy')
else:
    store.close()
    raise SystemExit('second writer unexpectedly acquired ownership')
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", script, str(store.path)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "busy")

    def test_abrupt_process_exit_releases_writer_lock_without_removing_lock_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "scan.sqlite3"
            script = """
import os, sys
from polymarket.leaderboard_state import LeaderboardStateStore
store = LeaderboardStateStore(sys.argv[1])
store.prepare({'period': 'all'}, resume=False)
store.record_page(0, 1, [{'wallet': '0xaaa'}])
os._exit(17)
"""
            result = subprocess.run(
                [sys.executable, "-B", "-c", script, str(path)],
                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(result.returncode, 17, result.stderr)
            self.assertTrue(leaderboard_writer_lock_path(path).is_file())
            recovered = LeaderboardStateStore(path)
            try:
                recovered.prepare({"period": "all"}, resume=True)
                self.assertEqual(recovered.progress()["rows"], 1)
            finally:
                recovered.close()

    def test_snapshot_keeps_counts_and_rows_stable_during_writer_commits(self) -> None:
        writer = self.make_store()
        writer.record_page(0, 1, [self.row("0xaaa")])
        reader = LeaderboardStateStore(writer.path, read_only=True)
        try:
            with reader.snapshot():
                before = reader.status()
                writer.record_page(1, 1, [self.row("0xbbb", 2)])
                after = reader.status()
                rows = list(reader.iter_results({}, require_mdd=False, sort="roi_pct", direction="DESC", limit=None))
                self.assertEqual(after, before)
                self.assertEqual(len(rows), before["rows"])
                self.assertEqual(rows[0]["wallet"], "0xaaa")
            self.assertEqual(reader.progress()["rows"], 2)
        finally:
            reader.close()

    def test_missing_read_only_database_does_not_create_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "missing" / "scan.sqlite3"
            with self.assertRaises(sqlite3.OperationalError):
                LeaderboardStateStore(path, read_only=True)
            self.assertFalse(path.parent.exists())

    def test_failed_initialization_releases_writer_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "scan.sqlite3"
            with patch.object(LeaderboardStateStore, "_create_schema", side_effect=ValueError("invalid schema")):
                with self.assertRaisesRegex(ValueError, "invalid schema"):
                    LeaderboardStateStore(path)
            store = LeaderboardStateStore(path)
            store.close()

    def test_read_only_legacy_state_fails_without_migrating_data(self) -> None:
        store = self.make_store()
        store.connection.execute("DROP INDEX rows_wallet_unique_idx")
        store.connection.commit()
        with self.assertRaisesRegex(ValueError, "requires migration"):
            LeaderboardStateStore(store.path, read_only=True)
        self.assertIsNone(store.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'rows_wallet_unique_idx'"
        ).fetchone())

    def test_failed_scan_reset_preserves_previous_rows_and_signature(self) -> None:
        store = self.make_store()
        store.record_page(0, 1, [self.row("0xaaa")])
        before = store.status()
        with patch.object(store, "_set_metadata", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                store.prepare({"period": "new"}, resume=False)
        self.assertEqual(store.status(), before)

    def test_overlapping_pages_keep_first_wallet_observation_and_raw_scan_count(self) -> None:
        store = self.make_store()
        self.assertTrue(store.record_page(0, 2, [self.row("0xAAA"), self.row("0xBBB", 2)]))
        self.assertTrue(store.record_page(2, 2, [self.row(" 0xBbB ", 3), self.row("0xCCC", 4)]))
        state = store.progress()
        self.assertEqual(state["scanned"], 4)
        self.assertEqual(state["rows"], 3)
        self.assertEqual(state["unique_wallets"], 3)
        self.assertEqual(state["duplicate_rows"], 1)
        self.assertEqual(state["next_offset"], 4)
        rows = list(store.iter_results({}, require_mdd=False, sort="roi_pct", direction="ASC", limit=None))
        self.assertEqual([row["wallet"] for row in rows], ["0xaaa", "0xbbb", "0xccc"])
        self.assertEqual(rows[1]["pnl_usd"], 2.0)

    def test_missing_wallet_rows_are_not_collapsed_into_one_user(self) -> None:
        store = self.make_store()
        store.record_page(0, 2, [self.row("", 1), self.row("", 2)])
        self.assertEqual(store.progress()["rows"], 2)
        self.assertEqual(store.progress()["unique_wallets"], 0)
        self.assertEqual(store.progress()["duplicate_rows"], 0)

    def test_legacy_wallet_deduplication_is_durable_and_preserves_page_offsets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.sqlite3"
            store = LeaderboardStateStore(path)
            store.prepare({}, resume=False)
            store.record_page(0, 1, [self.row("0xaaa")])
            store.connection.execute("DROP INDEX rows_wallet_unique_idx")
            store.connection.execute(
                "INSERT INTO rows(page_offset, page_index, rank, display_name, wallet, raw_json) VALUES (1, 0, 2, 'duplicate', ' 0xAAA ', '{}')"
            )
            store.connection.execute("INSERT INTO pages(page_offset, page_limit, row_count, fingerprint, saved_at) VALUES (1, 1, 1, 'legacy', 1)")
            store.connection.commit()
            store.close()
            for _ in range(2):
                migrated = LeaderboardStateStore(path)
                try:
                    self.assertEqual(migrated.progress()["rows"], 1)
                    self.assertEqual(migrated.progress()["scanned"], 2)
                    self.assertEqual(migrated.progress()["next_offset"], 2)
                    row = next(migrated.iter_mdd_candidates({}, sort="roi_pct", direction="DESC", limit=None))
                    self.assertEqual(row["rank"], 1)
                    self.assertEqual(row["wallet"], "0xaaa")
                finally:
                    migrated.close()

    def test_recorded_page_retry_preserves_mdd_and_rejects_changed_observation(self) -> None:
        store = self.make_store()
        page = [self.row("0xaaa")]
        store.record_page(0, 1, page)
        row = next(store.iter_mdd_candidates({}, sort="roi_pct", direction="DESC", limit=None))
        store.set_mdd(row["id"], {"mdd_pct": 10.0})
        self.assertTrue(store.record_page(0, 1, page))
        self.assertEqual(store.progress()["mdd_done"], 1)
        with self.assertRaisesRegex(ValueError, "already saved"):
            store.record_page(0, 1, [self.row("0xbbb")])
        self.assertEqual(store.progress()["rows"], 1)
        self.assertEqual(store.progress()["mdd_done"], 1)

    def test_mdd_settings_change_invalidates_only_enrichment(self) -> None:
        store = self.make_store()
        store.record_page(0, 50, [self.row("0xaaa")])
        signature = {"calculation_version": 1, "options": {"mode": "fast"}}
        self.assertEqual(store.prepare_mdd(signature), 0)
        row = next(store.iter_mdd_candidates({}, sort="roi_pct", direction="DESC", limit=None))
        store.set_mdd(row["id"], {"mdd_pct": 10.0, "mdd_method": "old"})
        self.assertEqual(store.prepare_mdd(signature), 0)
        self.assertEqual(store.prepare_mdd({**signature, "calculation_version": 2}), 1)
        self.assertEqual(store.progress()["mdd_done"], 0)
        self.assertEqual(store.progress()["mdd_pending"], 1)
        self.assertTrue(store.progress()["scan_complete"])
        row = next(store.iter_mdd_candidates({}, sort="roi_pct", direction="DESC", limit=None))
        self.assertIsNone(row["mdd_pct"])
        self.assertEqual(row["mdd_method"], "")
        self.assertEqual(row["pnl_usd"], 1.0)
        self.assertEqual(store.status()["mdd_signature"]["calculation_version"], 2)

    def test_legacy_mdd_and_errors_are_not_reused_without_a_signature(self) -> None:
        store = self.make_store()
        store.record_page(0, 50, [self.row("0xaaa"), self.row("0xbbb", 2)])
        rows = list(store.iter_mdd_candidates({}, sort="roi_pct", direction="ASC", limit=None))
        store.set_mdd(rows[0]["id"], {"mdd_pct": 10.0})
        store.set_mdd(rows[1]["id"], None, ValueError("old error"))
        self.assertEqual(store.prepare_mdd({"calculation_version": 1}), 2)
        self.assertEqual(store.progress()["mdd_errors"], 0)
        self.assertEqual(store.progress()["mdd_pending"], 2)

    def test_mdd_invalidation_rolls_back_if_signature_cannot_be_saved(self) -> None:
        store = self.make_store()
        store.record_page(0, 50, [self.row("0xaaa")])
        store.prepare_mdd({"version": 1})
        row = next(store.iter_mdd_candidates({}, sort="roi_pct", direction="DESC", limit=None))
        store.set_mdd(row["id"], {"mdd_pct": 10.0})
        with patch.object(store, "_set_metadata", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                store.prepare_mdd({"version": 2})
        self.assertEqual(store.progress()["mdd_done"], 1)
        self.assertEqual(store.status()["mdd_signature"], {"version": 1})

    def test_repeated_page_stops_a_durable_unlimited_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LeaderboardStateStore(Path(tmp) / "leaderboard.sqlite3")
            try:
                store.prepare({"remote_sort": "PNL", "direction": "DESC", "period": "all", "category": "OVERALL"}, resume=False)
                page = [
                    {
                        "rank": 1,
                        "display_name": "leader",
                        "wallet": "0x" + "1" * 40,
                        "pnl_usd": 20.0,
                        "volume_usd": 100.0,
                        "roi_pct": 20.0,
                        "trade_count": 3,
                        "raw": {"rank": 1},
                    }
                ]

                self.assertTrue(store.record_page(0, 1, page))
                self.assertFalse(store.record_page(1, 1, page))

                progress = store.progress()
                self.assertEqual(progress["rows"], 1)
                self.assertEqual(progress["pages"], 1)
                self.assertEqual(progress["mdd_pending"], 1)
                self.assertTrue(progress["scan_complete"])
                self.assertEqual(progress["stop_reason"], "repeated_page")
                self.assertTrue(progress["started_at"])
                self.assertTrue(progress["last_updated_at"])
            finally:
                store.close()

    def test_mdd_state_keeps_export_provenance_without_point_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LeaderboardStateStore(Path(tmp) / "leaderboard.sqlite3")
            try:
                store.prepare({"remote_sort": "PNL", "direction": "DESC", "period": "all", "category": "OVERALL"}, resume=False)
                store.record_page(
                    0,
                    50,
                    [
                        {
                            "rank": 1,
                            "display_name": "leader",
                            "wallet": "0x" + "1" * 40,
                            "pnl_usd": 20.0,
                            "volume_usd": 100.0,
                            "roi_pct": 20.0,
                            "trade_count": 3,
                            "raw": {"rank": 1},
                        }
                    ],
                )
                row = next(store.iter_mdd_candidates({}, sort="roi_pct", direction="DESC", limit=1))
                store.set_mdd(
                    row["id"],
                    {
                        "mdd_usd": 5.0,
                        "mdd_pct": 10.0,
                        "mdd_method": "public_data_historical_equity_curve_v2",
                        "mdd_pct_basis": "drawdown_usd / equity",
                        "points_total": 500,
                        "points": [{"timestamp": index, "value": float(index)} for index in range(500)],
                        "limitations": ["public data only"],
                    },
                )

                result = next(store.iter_results({}, require_mdd=True, sort="roi_pct", direction="DESC", limit=1))
                self.assertEqual(result["mdd_pct"], 10.0)
                self.assertEqual(result["mdd_pct_basis"], "drawdown_usd / equity")
                self.assertEqual(result["points_total"], 500)
                self.assertNotIn("points", result)
            finally:
                store.close()

    def test_query_filters_and_sorting_cannot_interpolate_sql_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LeaderboardStateStore(Path(tmp) / "leaderboard.sqlite3")
            try:
                store.prepare({"remote_sort": "PNL", "direction": "DESC", "period": "all", "category": "OVERALL"}, resume=False)
                store.record_page(
                    0,
                    1,
                    [
                        {
                            "rank": 1,
                            "display_name": "leader",
                            "wallet": "0x" + "1" * 40,
                            "pnl_usd": 20.0,
                            "volume_usd": 100.0,
                            "roi_pct": 20.0,
                            "trade_count": 3,
                            "raw": {},
                        }
                    ],
                )

                rows = list(
                    store.iter_results(
                        {"min_pnl_usd": "0; DROP TABLE rows;--"},  # type: ignore[dict-item]
                        require_mdd=False,
                        sort="roi_pct; DROP TABLE rows;--",
                        direction="ASC; DROP TABLE rows;--",
                        limit=1,
                    )
                )

                self.assertEqual(rows, [])
                self.assertEqual(store.progress()["rows"], 1)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
