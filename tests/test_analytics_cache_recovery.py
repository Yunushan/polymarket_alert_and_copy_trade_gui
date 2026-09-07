from __future__ import annotations

import json
import tempfile
import unittest
from functools import partial
from pathlib import Path
from unittest.mock import patch

from polymarket.analytics_cache import (
    analytics_cache_health,
    analytics_cache_summary,
    list_analytics_artifacts,
    load_analytics_artifact,
    load_analytics_cache,
    purge_analytics_artifacts,
    save_analytics_cache,
    store_analytics_artifact,
)


class AnalyticsCacheRecoveryTests(unittest.TestCase):
    def test_read_failures_preserve_active_cache_for_read_and_mutation_paths(self) -> None:
        for failure in (PermissionError("temporarily unreadable"), OSError("I/O failure"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "cache.json"
                metadata = store_analytics_artifact("audit", {"wallet": "existing"}, {"value": 5}, path=target)
                snapshot = load_analytics_cache(target)
                original_read = Path.read_bytes
                previous_bytes = original_read(target)
                operations = (
                    partial(load_analytics_cache, target),
                    partial(analytics_cache_summary, target),
                    partial(analytics_cache_health, target),
                    partial(list_analytics_artifacts, path=target),
                    partial(load_analytics_artifact, metadata["key"], path=target),
                    partial(store_analytics_artifact, "audit", {"wallet": "new"}, {"value": 9}, path=target),
                    partial(purge_analytics_artifacts, all_entries=True, path=target),
                    partial(save_analytics_cache, snapshot, target),
                )

                def deny_read(path: Path, *, denied=target, error=failure, reader=original_read) -> bytes:
                    if path == denied:
                        raise error
                    return reader(path)

                for operation in operations:
                    with (
                        self.subTest(operation=operation.func.__name__),
                        patch.object(Path, "read_bytes", deny_read),
                        patch("polymarket.analytics_cache.replace_file") as replace,
                        self.assertRaises(type(failure)),
                    ):
                        operation()
                    replace.assert_not_called()
                    self.assertEqual(original_read(target), previous_bytes)
                    self.assertFalse(list(target.parent.glob("cache.json.corrupt-*")))

                loaded = load_analytics_artifact(metadata["key"], path=target)
                self.assertIsNotNone(loaded)
                self.assertEqual(loaded[0], {"value": 5})
                new = store_analytics_artifact("audit", {"wallet": "new"}, {"value": 9}, path=target)
                self.assertEqual(set(load_analytics_cache(target)["entries"]), {metadata["key"], new["key"]})

    def test_failed_exists_probe_cannot_turn_an_unreadable_cache_into_a_missing_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "cache.json"
            snapshot = load_analytics_cache(target)
            save_analytics_cache(snapshot, target)
            previous_bytes = target.read_bytes()
            for operation in (partial(load_analytics_cache, target), partial(save_analytics_cache, snapshot, target)):
                with (
                    self.subTest(operation=operation.func.__name__),
                    patch.object(Path, "exists", return_value=False),
                    patch.object(Path, "read_bytes", side_effect=PermissionError("denied")),
                    self.assertRaises(PermissionError),
                ):
                    operation()
            self.assertEqual(target.read_bytes(), previous_bytes)

    def test_missing_cache_can_be_created_without_quarantine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "cache.json"
            snapshot = load_analytics_cache(target)
            self.assertEqual(snapshot["entries"], {})
            self.assertFalse(target.exists())
            snapshot["entries"]["one"] = {"kind": "test"}
            save_analytics_cache(snapshot, target)
            self.assertEqual(load_analytics_cache(target)["entries"], snapshot["entries"])
            self.assertFalse(list(target.parent.glob("cache.json.corrupt-*")))

    def test_malformed_bytes_and_invalid_containers_are_preserved_before_recovery(self) -> None:
        for encoded in (b"{broken", b"\xff", b"[]", b"null", b'{"entries": []}', b'{"entries": null}'):
            with self.subTest(encoded=encoded), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "cache.json"
                target.write_bytes(encoded)
                snapshot = load_analytics_cache(target)
                self.assertEqual(snapshot["entries"], {})
                self.assertFalse(target.exists())
                backups = list(target.parent.glob("cache.json.corrupt-*"))
                self.assertEqual(len(backups), 1)
                self.assertEqual(backups[0].read_bytes(), encoded)
                save_analytics_cache(snapshot, target)
                self.assertEqual(json.loads(target.read_bytes())["entries"], {})
                self.assertEqual(backups[0].read_bytes(), encoded)

    def test_failed_quarantine_never_returns_an_empty_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "cache.json"
            previous_bytes = b"{broken"
            target.write_bytes(previous_bytes)
            for operation in (
                partial(load_analytics_cache, target),
                partial(store_analytics_artifact, "audit", {}, {}, path=target),
                partial(purge_analytics_artifacts, all_entries=True, path=target),
            ):
                with (
                    self.subTest(operation=operation.func.__name__),
                    patch("polymarket.analytics_cache.replace_file", side_effect=PermissionError("rename denied")),
                    self.assertRaises(PermissionError),
                ):
                    operation()
                self.assertEqual(target.read_bytes(), previous_bytes)
                self.assertFalse(list(target.parent.glob("cache.json.corrupt-*")))

    def test_quarantine_directory_sync_failure_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "cache.json"
            previous_bytes = b"{broken"
            target.write_bytes(previous_bytes)
            with (
                patch("polymarket.analytics_cache._fsync_parent_directory", side_effect=OSError("sync failed")),
                self.assertRaisesRegex(OSError, "sync failed"),
            ):
                load_analytics_cache(target)
            backups = list(target.parent.glob("cache.json.corrupt-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), previous_bytes)

    def test_unexpected_decoder_failures_do_not_quarantine_valid_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "cache.json"
            target.write_text('{"entries": {}}', encoding="utf-8")
            previous_bytes = target.read_bytes()
            with (
                patch("polymarket.analytics_cache.json.loads", side_effect=MemoryError("allocation failed")),
                self.assertRaises(MemoryError),
            ):
                load_analytics_cache(target)
            self.assertEqual(target.read_bytes(), previous_bytes)
            self.assertFalse(list(target.parent.glob("cache.json.corrupt-*")))

    def test_legacy_cache_without_entries_is_not_rewritten_during_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "cache.json"
            target.write_text('{"version": 1}', encoding="utf-8")
            previous_bytes = target.read_bytes()
            self.assertEqual(load_analytics_cache(target)["entries"], {})
            self.assertEqual(target.read_bytes(), previous_bytes)
            self.assertFalse(list(target.parent.glob("cache.json.corrupt-*")))
