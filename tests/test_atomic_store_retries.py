from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from core import storage
from core.storage import ConfigCommitError, load_config, save_config
from polymarket.analytics_cache import load_analytics_cache, save_analytics_cache
from polymarket.live_reports import (
    load_live_validation_decisions,
    load_live_validation_promotion_proposal_snapshots,
    load_live_validation_reports,
    save_live_validation_decisions,
    save_live_validation_promotion_proposal_snapshots,
    save_live_validation_reports,
)


STORES = (
    ("config", None, load_config, save_config),
    ("cache", "entries", load_analytics_cache, save_analytics_cache),
    ("reports", "reports", load_live_validation_reports, save_live_validation_reports),
    ("decisions", "decisions", load_live_validation_decisions, save_live_validation_decisions),
    ("snapshots", "snapshots", load_live_validation_promotion_proposal_snapshots, save_live_validation_promotion_proposal_snapshots),
)


class AtomicStoreRetryTests(unittest.TestCase):
    def test_config_commit_error_distinguishes_post_replace_lock_cleanup(self):
        original_lock = storage._config_file_lock

        @contextmanager
        def failed_release(path):
            with original_lock(path):
                yield
            raise OSError("private lock-release detail")

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "config.json"
            cfg = load_config(target)
            with patch("core.storage._config_file_lock", failed_release):
                with self.assertRaises(ConfigCommitError) as raised:
                    save_config(cfg, target)
            self.assertNotIn("private lock-release detail", str(raised.exception))
            self.assertEqual(str(raised.exception.__cause__), "private lock-release detail")
            self.assertEqual(cfg.to_dict(), load_config(target).to_dict())
            cfg.theme = "dark"
            save_config(cfg, target)
            self.assertEqual(load_config(target).theme, "dark")

    def test_precommit_failure_does_not_claim_a_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "config.json"
            cfg = load_config(target)
            save_config(cfg, target)
            before = target.read_bytes()
            cfg.theme = "dark"
            with patch("core.storage.replace_file", side_effect=OSError("disk full")):
                with self.assertRaises(OSError) as raised:
                    save_config(cfg, target)
            self.assertNotIsInstance(raised.exception, ConfigCommitError)
            self.assertEqual(target.read_bytes(), before)

    def change(self, snapshot, collection):
        if collection:
            snapshot[collection]["new"] = {"status": "ok"}
        else:
            snapshot.theme = "dark"

    def assert_committed(self, target, collection):
        payload = json.loads(target.read_text(encoding="utf-8"))
        if collection:
            self.assertEqual(payload[collection]["new"], {"status": "ok"})
        else:
            self.assertEqual(payload["theme"], "dark")
        self.assertFalse(list(target.parent.glob(f".{target.name}.*.tmp")))

    def test_all_durable_stores_retry_transient_denial_without_losing_the_previous_revision(self):
        original_replace = os.replace
        for name, collection, loader, writer in STORES:
            with self.subTest(store=name), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / f"{name}.json"
                writer(loader(target), target)
                previous = target.read_bytes()
                snapshot = loader(target)
                self.change(snapshot, collection)
                attempts = []

                def blocked_then_released(source, destination, *, target=target, previous=previous, attempts=attempts):
                    self.assertEqual(target.read_bytes(), previous)
                    self.assertEqual(destination, target)
                    json.loads(Path(source).read_bytes())
                    attempts.append(source)
                    if len(attempts) < 3:
                        error = PermissionError("temporary file contention")
                        error.winerror = 5
                        raise error
                    original_replace(source, destination)

                with patch("core.atomic_files.os.replace", side_effect=blocked_then_released), patch("core.atomic_files.time.sleep"):
                    writer(snapshot, target)
                self.assertEqual(len(attempts), 3)
                self.assertEqual(len(set(attempts)), 1)
                self.assert_committed(target, collection)

    def test_permanent_denial_preserves_data_and_allows_the_same_snapshot_after_recovery(self):
        for name, collection, loader, writer in STORES:
            with self.subTest(store=name), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / f"{name}.json"
                writer(loader(target), target)
                previous = target.read_bytes()
                snapshot = loader(target)
                self.change(snapshot, collection)
                error = PermissionError("permanent denial")
                error.winerror = 32
                with patch("core.atomic_files.os.replace", side_effect=error) as replace, patch("core.atomic_files.time.sleep"):
                    with self.assertRaises(PermissionError):
                        writer(snapshot, target)
                self.assertEqual(replace.call_count, 10)
                self.assertEqual(target.read_bytes(), previous)
                self.assertFalse(list(target.parent.glob(f".{target.name}.*.tmp")))
                writer(snapshot, target)
                self.assert_committed(target, collection)
