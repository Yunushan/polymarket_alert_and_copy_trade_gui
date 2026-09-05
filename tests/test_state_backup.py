from __future__ import annotations

import gzip
import io
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import backup_state
from scripts.backup_state import BACKUP_PREFIX, _fsync_directory, create_backup
from scripts.restore_state_backup import (
    DEFAULT_MAX_TAR_METADATA_BYTES,
    _safe_member_path,
    _max_tar_stream_bytes,
    catalog_verified_backups,
    restore_backup,
    verify_backup,
)


class StateBackupTests(unittest.TestCase):
    def test_backup_directory_is_synced_on_posix(self) -> None:
        directory = Path("backups")
        with (
            patch("scripts.backup_state.os.name", "posix"),
            patch("scripts.backup_state.os.open", return_value=42) as open_directory,
            patch("scripts.backup_state.os.fsync") as sync,
            patch("scripts.backup_state.os.close") as close,
        ):
            _fsync_directory(directory)

        open_directory.assert_called_once()
        sync.assert_called_once_with(42)
        close.assert_called_once_with(42)

    def test_restore_utility_runs_when_invoked_as_a_script_path(self) -> None:
        script = Path(__file__).resolve().parent.parent / "scripts" / "restore_state_backup.py"
        result = subprocess.run([sys.executable, str(script), "--help"], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Verify or restore", result.stdout)
        self.assertIn("--max-archive-bytes", result.stdout)

    def test_backup_utility_exposes_the_same_creation_limits(self) -> None:
        script = Path(__file__).resolve().parent.parent / "scripts" / "backup_state.py"
        result = subprocess.run([sys.executable, str(script), "--help"], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--max-members", result.stdout)
        self.assertIn("--max-bytes", result.stdout)
        self.assertIn("--max-archive-bytes", result.stdout)
        self.assertIn("--sqlite-timeout", result.stdout)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "state"
            destination = root / "backups"
            source.mkdir()
            source.joinpath("config.json").write_text("{}", encoding="utf-8")
            created = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--source",
                    str(source),
                    "--destination",
                    str(destination),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            payload = json.loads(created.stdout)
            self.assertEqual(verify_backup(destination / payload["archive"])["file_count"], 1)

    def test_backup_verification_and_restore_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "state"
            destination = root / "backups"
            restored = root / "restored"
            (source / "nested").mkdir(parents=True)
            (source / "config.json").write_text('{"theme": "dark"}', encoding="utf-8")
            (source / "nested" / "paper.jsonl").write_text('{"id": 1}\n', encoding="utf-8")

            manifest = create_backup(source, destination, retain=2)
            archive = destination / str(manifest["archive"])
            self.assertTrue(archive.is_file())
            self.assertTrue(archive.with_name(archive.name + ".json").is_file())
            verified = verify_backup(archive)
            self.assertEqual(verified["file_count"], 2)
            self.assertEqual(verified["verified_archive_bytes"], archive.stat().st_size)
            restore_backup(archive, restored)

            self.assertEqual((restored / "config.json").read_text(encoding="utf-8"), '{"theme": "dark"}')
            self.assertEqual((restored / "nested" / "paper.jsonl").read_text(encoding="utf-8"), '{"id": 1}\n')

    def test_backup_creation_enforces_restore_limits_without_publishing_a_pair(self) -> None:
        cases = (
            ("members", {"a.txt": b"a", "b.txt": b"b"}, {"max_members": 1}, "member"),
            ("payload", {"large.bin": b"x" * 2048}, {"max_bytes": 1024}, "uncompressed"),
            ("archive", {"data.txt": b"content"}, {"max_archive_bytes": 32}, "compressed"),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for case_name, files, limits, error_fragment in cases:
                with self.subTest(case=case_name):
                    source = root / f"{case_name}-state"
                    destination = root / f"{case_name}-backups"
                    source.mkdir()
                    for name, payload in files.items():
                        source.joinpath(name).write_bytes(payload)
                    with self.assertRaisesRegex(RuntimeError, error_fragment):
                        create_backup(source, destination, **limits)
                    self.assertEqual(list(destination.glob(f"{BACKUP_PREFIX}*")), [])

    def test_backup_verifies_the_staged_pair_before_manifest_last_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "state"
            destination = root / "backups"
            source.mkdir()
            source.joinpath("config.json").write_text("{}", encoding="utf-8")

            def reject_staged_pair(archive: Path, **_limits: int) -> None:
                self.assertTrue(archive.is_file())
                self.assertTrue(archive.with_name(archive.name + ".json").is_file())
                self.assertEqual(list(destination.glob(f"{BACKUP_PREFIX}*")), [])
                raise RuntimeError("staged verification rejected")

            with (
                patch("scripts.restore_state_backup.verify_backup", side_effect=reject_staged_pair) as verifier,
                self.assertRaisesRegex(RuntimeError, "staged verification rejected"),
            ):
                create_backup(source, destination)

            verifier.assert_called_once()
            self.assertEqual(list(destination.glob(f"{BACKUP_PREFIX}*")), [])

    def test_backup_tars_the_stable_snapshot_when_source_changes_after_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "state"
            destination = root / "backups"
            restored = root / "restored"
            source.mkdir()
            source_file = source / "config.json"
            source_file.write_bytes(b"before")
            snapshot = backup_state._snapshot_regular_file

            def snapshot_then_mutate(path: Path, staging_path: Path, max_bytes: int) -> tuple[Path, int]:
                result = snapshot(path, staging_path, max_bytes)
                path.write_bytes(b"mutated-after-stable-copy")
                return result

            with patch("scripts.backup_state._snapshot_regular_file", side_effect=snapshot_then_mutate):
                archive = destination / str(create_backup(source, destination)["archive"])

            restore_backup(archive, restored)
            self.assertEqual(restored.joinpath("config.json").read_bytes(), b"before")
            self.assertEqual(source_file.read_bytes(), b"mutated-after-stable-copy")

    def test_regular_file_snapshot_rejects_a_detected_mid_copy_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "config.json"
            staging = root / "staging" / "config.json"
            source.write_bytes(b"stable-length")
            before = source.stat()
            after = SimpleNamespace(
                st_dev=before.st_dev,
                st_ino=before.st_ino,
                st_mode=before.st_mode,
                st_size=before.st_size,
                st_mtime_ns=before.st_mtime_ns + 1,
            )

            with (
                patch("scripts.backup_state.os.fstat", side_effect=[before, after]),
                self.assertRaisesRegex(RuntimeError, "changed while being snapshotted"),
            ):
                backup_state._snapshot_regular_file(source, staging, 1024)

            self.assertFalse(staging.exists())

    def test_bounded_decompression_rejects_a_gzip_bomb_before_tarfile_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            archive = root / "oversized.tar.gz"
            max_members = 1
            max_bytes = 1024
            raw_limit = _max_tar_stream_bytes(max_members, max_bytes)
            archive.write_bytes(gzip.compress(b"\0" * (raw_limit + 1)))
            archive.with_name(archive.name + ".json").write_text(
                json.dumps(
                    {
                        "archive": archive.name,
                        "created_at": "2026-01-01T00:00:00Z",
                        "file_count": 0,
                        "schema_version": 1,
                        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                        "source_name": "state",
                        "uncompressed_bytes": 0,
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch("scripts.restore_state_backup.tarfile.open", side_effect=AssertionError("must preflight")),
                self.assertRaisesRegex(RuntimeError, "tar stream exceeds"),
            ):
                verify_backup(archive, max_members=max_members, max_bytes=max_bytes)

    def test_oversized_pax_metadata_is_rejected_before_tarfile_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            archive = root / "oversized-pax.tar.gz"
            metadata_size = DEFAULT_MAX_TAR_METADATA_BYTES + 1
            extension = tarfile.TarInfo("PaxHeader")
            extension.type = tarfile.XHDTYPE
            extension.size = metadata_size
            raw_tar = (
                extension.tobuf(format=tarfile.USTAR_FORMAT)
                + (b"a" * metadata_size)
                + (b"\0" * (-metadata_size % 512))
                + (b"\0" * 1024)
            )
            archive.write_bytes(gzip.compress(raw_tar))
            archive.with_name(archive.name + ".json").write_text(
                json.dumps(
                    {
                        "archive": archive.name,
                        "created_at": "2026-01-01T00:00:00Z",
                        "file_count": 0,
                        "schema_version": 1,
                        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                        "source_name": "state",
                        "uncompressed_bytes": 0,
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch("scripts.restore_state_backup.tarfile.open", side_effect=AssertionError("must preflight")),
                self.assertRaisesRegex(RuntimeError, "tar metadata exceeds"),
            ):
                verify_backup(archive)

    def test_verification_and_restore_never_materialize_all_archive_members(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "state"
            destination = root / "backups"
            restored = root / "restored"
            source.mkdir()
            source.joinpath("config.json").write_text("{}", encoding="utf-8")
            archive = destination / str(create_backup(source, destination)["archive"])

            with patch.object(
                tarfile.TarFile,
                "getmembers",
                side_effect=AssertionError("restore must stream archive members"),
            ) as getmembers:
                self.assertEqual(verify_backup(archive)["file_count"], 1)
                restore_backup(archive, restored)

            getmembers.assert_not_called()
            self.assertEqual((restored / "config.json").read_text(encoding="utf-8"), "{}")

    def test_verification_rejects_the_member_after_the_configured_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            archive = root / "too-many-members.tar.gz"
            with tarfile.open(archive, "w:gz") as handle:
                for index in range(3):
                    payload = str(index).encode("ascii")
                    member = tarfile.TarInfo(f"member-{index}.txt")
                    member.size = len(payload)
                    handle.addfile(member, io.BytesIO(payload))
            archive.with_name(archive.name + ".json").write_text(
                json.dumps(
                    {
                        "archive": archive.name,
                        "created_at": "2026-01-01T00:00:00Z",
                        "file_count": 3,
                        "schema_version": 1,
                        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                        "source_name": "state",
                        "uncompressed_bytes": 3,
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "exceeds restore safety limits"):
                verify_backup(archive, max_members=2)

    def test_verification_rejects_archive_during_snapshot_copy_after_compressed_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "state"
            destination = root / "backups"
            source.mkdir()
            source.joinpath("config.json").write_text("{}", encoding="utf-8")
            archive = destination / str(create_backup(source, destination)["archive"])
            compressed_bytes = archive.stat().st_size

            with self.assertRaisesRegex(RuntimeError, "compressed size exceeds restore safety limits"):
                verify_backup(archive, max_archive_bytes=compressed_bytes - 1)

            self.assertEqual(
                verify_backup(archive, max_archive_bytes=compressed_bytes)["verified_archive_bytes"],
                compressed_bytes,
            )

    def test_backup_retention_prunes_old_archive_and_checksum_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "state"
            destination = root / "backups"
            source.mkdir()
            source.joinpath("config.json").write_text("{}", encoding="utf-8")
            first_archive = destination / str(create_backup(source, destination, retain=1)["archive"])
            source.joinpath("config.json").write_text('{"updated": true}', encoding="utf-8")
            second_archive = destination / str(create_backup(source, destination, retain=1)["archive"])
            self.assertFalse(first_archive.exists())
            self.assertTrue(second_archive.exists())
            with second_archive.open("ab") as handle:
                handle.write(b"tampered")
            with self.assertRaisesRegex(RuntimeError, "checksum"):
                verify_backup(second_archive)

    def test_retention_counts_only_verified_pairs_and_preserves_orphans(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            source = root / "state"
            destination = root / "backups"
            source.mkdir()
            source.joinpath("config.json").write_text("{}", encoding="utf-8")
            backup_times = (
                datetime(2026, 1, 1, tzinfo=timezone.utc),
                datetime(2026, 1, 2, tzinfo=timezone.utc),
                datetime(2026, 1, 3, tzinfo=timezone.utc),
            )
            with patch("scripts.backup_state._utc_now", side_effect=backup_times):
                first = destination / str(create_backup(source, destination, retain=2)["archive"])

                orphan_archive = destination / "market-sentinel-state-20990101T000000Z-orphan00.tar.gz"
                orphan_archive.write_bytes(b"publication interrupted before manifest")
                orphan_manifest = destination / "market-sentinel-state-20990101T000001Z-orphan01.tar.gz.json"
                orphan_manifest.write_text("{}", encoding="utf-8")

                invalid_archive = destination / "market-sentinel-state-20990101T000002Z-invalid0.tar.gz"
                invalid_archive.write_bytes(b"not a tar archive")
                invalid_archive.with_name(invalid_archive.name + ".json").write_text(
                    json.dumps(
                        {
                            "archive": invalid_archive.name,
                            "created_at": "2099-01-01T00:00:02Z",
                            "file_count": 0,
                            "schema_version": 1,
                            "sha256": hashlib.sha256(invalid_archive.read_bytes()).hexdigest(),
                            "source_name": "state",
                            "uncompressed_bytes": 0,
                        }
                    ),
                    encoding="utf-8",
                )

                second = destination / str(create_backup(source, destination, retain=2)["archive"])
                self.assertTrue(first.exists())
                self.assertTrue(second.exists())
                self.assertTrue(orphan_archive.exists())
                self.assertTrue(orphan_manifest.exists())
                self.assertTrue(invalid_archive.exists())

                third_result = create_backup(source, destination, retain=2)
                third = destination / str(third_result["archive"])

            self.assertFalse(first.exists())
            self.assertEqual(third_result["pruned"], [first.name])
            self.assertTrue(second.exists())
            self.assertTrue(third.exists())
            self.assertTrue(orphan_archive.exists())
            self.assertTrue(orphan_manifest.exists())
            self.assertTrue(invalid_archive.exists())

            catalog = catalog_verified_backups(destination)
            self.assertEqual([item.archive_path for item in catalog.verified], [third, second])
            self.assertEqual(catalog.orphan_archives, (orphan_archive.name,))
            self.assertEqual(catalog.orphan_manifests, (orphan_manifest.name,))
            self.assertEqual(catalog.invalid_pairs, (invalid_archive.name,))

    def test_sqlite_size_limit_rejects_before_creating_staged_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "large.sqlite3"
            staging = root / "staged.sqlite3"
            connection = sqlite3.connect(source)
            try:
                connection.execute("CREATE TABLE data (value BLOB)")
                connection.execute("INSERT INTO data VALUES (zeroblob(1048576))")
                connection.commit()
            finally:
                connection.close()
            with self.assertRaisesRegex(RuntimeError, "uncompressed restore safety limits"):
                backup_state._snapshot_sqlite(source, staging, 4096)
            self.assertFalse(staging.exists())

    def test_sqlite_snapshot_does_not_grow_when_live_writer_commits_after_preflight(self) -> None:
        connect = sqlite3.connect
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "live.sqlite3"
            staging = root / "staged.sqlite3"
            writer = connect(source)
            try:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("CREATE TABLE data (value BLOB)")
                writer.execute("INSERT INTO data VALUES ('original')")
                writer.commit()
                size_limit = writer.execute("PRAGMA page_size").fetchone()[0] * writer.execute("PRAGMA page_count").fetchone()[0]

                class GrowingConnection(sqlite3.Connection):
                    def backup(self, target, **kwargs):
                        writer.execute("INSERT INTO data VALUES (zeroblob(1048576))")
                        writer.commit()
                        return super().backup(target, **kwargs)

                with patch("scripts.backup_state.sqlite3.connect", side_effect=lambda *args, **kwargs: connect(
                    *args, **kwargs, factory=GrowingConnection,
                )):
                    backup_state._snapshot_sqlite(source, staging, size_limit)
                self.assertLessEqual(staging.stat().st_size, size_limit)
                reader = connect(staging)
                try:
                    self.assertEqual(reader.execute("SELECT value FROM data").fetchall(), [("original",)])
                    self.assertEqual(reader.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                finally:
                    reader.close()
                self.assertEqual(writer.execute("SELECT COUNT(*) FROM data").fetchone()[0], 2)
            finally:
                writer.close()

    def test_sqlite_copy_deadline_preserves_previous_backup_and_does_not_publish(self) -> None:
        connect = sqlite3.connect
        now = [0.0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "state"
            source.mkdir()
            database = source / "large.sqlite3"
            destination = root / "backups"
            connection = connect(database)
            try:
                connection.execute("CREATE TABLE data (value BLOB)")
                connection.execute("INSERT INTO data VALUES (zeroblob(1048576))")
                connection.commit()
            finally:
                connection.close()
            previous = create_backup(source, destination, retain=1)
            existing_files = set(destination.iterdir())
            chunks = []

            class SlowConnection(sqlite3.Connection):
                def backup(self, target, **kwargs):
                    progress = kwargs["progress"]

                    def slow_progress(status, remaining, total):
                        chunks.append((remaining, total))
                        now[0] = 2.0
                        progress(status, remaining, total)

                    kwargs["progress"] = slow_progress
                    return super().backup(target, **kwargs)

            with (
                patch("scripts.backup_state.sqlite3.connect", side_effect=lambda *args, **kwargs: connect(
                    *args, **kwargs, factory=SlowConnection,
                )),
                patch("scripts.backup_state.time.monotonic", side_effect=lambda: now[0]),
                self.assertRaisesRegex(RuntimeError, "SQLite backup exceeded"),
            ):
                create_backup(source, destination, retain=1, sqlite_timeout_seconds=1)
            self.assertEqual(len(chunks), 1)
            self.assertGreater(chunks[0][0], 0, "deadline must stop the real copy before all pages are copied")
            self.assertEqual(set(destination.iterdir()), existing_files)
            self.assertEqual(verify_backup(destination / previous["archive"])["file_count"], 1)

    def test_sqlite_lock_wait_obeys_budget_and_releases_connections(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "locked.sqlite3"
            staging = Path(temporary) / "staged.sqlite3"
            writer = sqlite3.connect(source)
            try:
                writer.execute("CREATE TABLE data (value TEXT)")
                writer.commit()
                writer.execute("BEGIN EXCLUSIVE")
                before = time.monotonic()
                with self.assertRaisesRegex(RuntimeError, "unable to create a consistent SQLite backup"):
                    backup_state._snapshot_sqlite(source, staging, 65536, timeout_seconds=0.1)
                self.assertLess(time.monotonic() - before, 2)
                self.assertFalse(staging.exists())
                writer.rollback()
                backup_state._snapshot_sqlite(source, staging, 65536, timeout_seconds=1)
            finally:
                writer.close()

    def test_invalid_sqlite_timeout_is_rejected_before_backup_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "backups"
            for timeout in (0, -1, float("nan"), float("inf")):
                with self.subTest(timeout=timeout), self.assertRaisesRegex(ValueError, "finite and positive"):
                    create_backup(root, destination, sqlite_timeout_seconds=timeout)
            self.assertFalse(destination.exists())

    def test_backup_uses_a_consistent_sqlite_snapshot_without_transaction_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "state"
            destination = root / "backups"
            restored = root / "restored"
            source.mkdir()
            database = source / "leaderboard.sqlite3"
            connection = sqlite3.connect(database)
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE rows (value TEXT NOT NULL)")
                connection.execute("INSERT INTO rows(value) VALUES ('durable')")
                connection.commit()
                source.joinpath("interrupted.sqlite3-journal").write_bytes(b"rollback journal")
                archive = destination / str(create_backup(source, destination)["archive"])
            finally:
                connection.close()

            with tarfile.open(archive, "r:gz") as handle:
                self.assertEqual(handle.getnames(), ["leaderboard.sqlite3"])
            restore_backup(archive, restored)
            restored_connection = sqlite3.connect(restored / "leaderboard.sqlite3")
            try:
                self.assertEqual(restored_connection.execute("SELECT value FROM rows").fetchone()[0], "durable")
            finally:
                restored_connection.close()

    def test_backup_of_active_leaderboard_omits_writer_lock_and_restores_rows(self) -> None:
        from polymarket.leaderboard_state import LeaderboardStateStore

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "state"
            destination = root / "backups"
            writer = LeaderboardStateStore(source / "scan.sqlite3")
            try:
                writer.prepare({}, resume=False)
                writer.record_page(0, 1, [{"wallet": "0xaaa"}])
                archive = destination / str(create_backup(source, destination)["archive"])
                with tarfile.open(archive, "r:gz") as handle:
                    self.assertEqual(handle.getnames(), ["scan.sqlite3"])
                restore_backup(archive, root / "restored")
                recovered = LeaderboardStateStore(root / "restored" / "scan.sqlite3", read_only=True)
                try:
                    self.assertEqual(recovered.progress()["rows"], 1)
                finally:
                    recovered.close()
            finally:
                writer.close()

    def test_restore_rejects_unsafe_archive_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            archive = root / "unsafe.tar.gz"
            with tarfile.open(archive, "w:gz") as handle:
                member = tarfile.TarInfo("../outside.txt")
                payload = b"unsafe"
                member.size = len(payload)
                handle.addfile(member, io.BytesIO(payload))
            archive.with_name(archive.name + ".json").write_text(
                json.dumps(
                    {
                        "archive": archive.name,
                        "created_at": "2026-01-01T00:00:00Z",
                        "file_count": 1,
                        "schema_version": 1,
                        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                        "source_name": "state",
                        "uncompressed_bytes": 6,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "unsafe member path"):
                verify_backup(archive)

    def test_restore_rejects_windows_separator_drive_and_unc_paths(self) -> None:
        for member_name in (
            r"..\outside.txt",
            r"C:\outside.txt",
            r"\\server\share\outside.txt",
        ):
            with self.subTest(member_name=member_name):
                with self.assertRaisesRegex(RuntimeError, "unsafe member path"):
                    _safe_member_path(member_name)

    def test_restore_rejects_windows_devices_streams_and_aliasing_names(self) -> None:
        for member_name in (
            "config.json:evil",
            "CON",
            "dir/nul.txt",
            "AUX.json",
            "COM1",
            "nested/Lpt9.log",
            "trailing.",
            "trailing ",
            "wild?.json",
        ):
            with self.subTest(member_name=member_name):
                with self.assertRaisesRegex(RuntimeError, "unsafe Windows member path"):
                    _safe_member_path(member_name)

    def test_restore_does_not_write_a_backslash_traversal_outside_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            archive = root / "unsafe-windows.tar.gz"
            payload = b"unsafe"
            with tarfile.open(archive, "w:gz") as handle:
                member = tarfile.TarInfo(r"..\escaped.txt")
                member.size = len(payload)
                handle.addfile(member, io.BytesIO(payload))
            archive.with_name(archive.name + ".json").write_text(
                json.dumps(
                    {
                        "archive": archive.name,
                        "created_at": "2026-01-01T00:00:00Z",
                        "file_count": 1,
                        "schema_version": 1,
                        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                        "source_name": "state",
                        "uncompressed_bytes": len(payload),
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "unsafe member path"):
                restore_backup(archive, root / "restore")
            self.assertFalse((root / "escaped.txt").exists())

    def test_restore_rejects_every_preexisting_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "state"
            destination = root / "backups"
            source.mkdir()
            source.joinpath("config.json").write_text("{}", encoding="utf-8")
            archive = destination / str(create_backup(source, destination)["archive"])

            empty_directory = root / "empty-restore-target"
            empty_directory.mkdir()
            existing_file = root / "file-restore-target"
            existing_file.write_text("keep", encoding="utf-8")
            nonempty_directory = root / "nonempty-restore-target"
            nonempty_directory.mkdir()
            nonempty_directory.joinpath("existing.txt").write_text("keep", encoding="utf-8")

            for restore_target in (empty_directory, existing_file, nonempty_directory):
                with self.subTest(restore_target=restore_target):
                    with self.assertRaisesRegex(ValueError, "must not already exist"):
                        restore_backup(archive, restore_target)

    def test_restore_requires_an_existing_parent_and_creates_private_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "state"
            backup_directory = root / "backups"
            source.mkdir()
            source.joinpath("config.json").write_text("{}", encoding="utf-8")
            archive = backup_directory / str(create_backup(source, backup_directory)["archive"])

            with self.assertRaisesRegex(ValueError, "parent must already exist"):
                restore_backup(archive, root / "missing-parent" / "restore-target")

            restore_target = root / "restore-target"
            restore_backup(archive, restore_target)
            if os.name == "posix":
                self.assertEqual(restore_target.stat().st_mode & 0o777, 0o700)
            self.assertEqual(restore_target.joinpath("config.json").read_text(encoding="utf-8"), "{}")


if __name__ == "__main__":
    unittest.main()
