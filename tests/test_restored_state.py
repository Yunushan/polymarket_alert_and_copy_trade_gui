from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.models import AppConfig, MutationJournalEntry
from core.storage import ConfigLoadError
from scripts.backup_state import create_backup
from scripts.verify_production_deployment import DURABLE_STATE_PATHS, check_restore_drill
from scripts.verify_restored_state import (
    STATE_FILENAMES, _check_sqlite, _inventory, isolated_environment, probe_restored_application,
)

ROOT = Path(__file__).resolve().parents[1]


class RestoredStateTests(unittest.TestCase):
    def setUp(self) -> None:
        cache = ROOT / ".cache"
        cache.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="restore-test-", dir=cache)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.frontend = self.root / "frontend"
        self.frontend.mkdir()
        (self.frontend / "index.html").write_text("<!doctype html><title>Restore test</title>", encoding="utf-8")
        (self.state / "config.json").write_text(json.dumps(AppConfig().to_dict()), encoding="utf-8")

    def probe(self) -> dict:
        with patch.dict(os.environ, isolated_environment(self.state), clear=True):
            return probe_restored_application(self.state, self.frontend)

    def test_real_backup_restores_and_serves_application_without_changes(self) -> None:
        config = AppConfig()
        config.copytrading.enabled = True
        config.copytrading.live = True
        config.mutation_journal.append(MutationJournalEntry(
            key_hash="a" * 64, method="POST", path="/api/live/order",
            request_hash="b" * 64, live=True, state="pending",
        ))
        (self.state / "config.json").write_text(json.dumps(config.to_dict()), encoding="utf-8")
        cache = self.state / STATE_FILENAMES["POLYMARKET_ANALYTICS_CACHE_PATH"]
        cache.write_text('{"version":1,"entries":{}}', encoding="utf-8")
        database = sqlite3.connect(self.state / "scan.sqlite3")
        database.execute("CREATE TABLE data (value TEXT)")
        database.execute("INSERT INTO data VALUES ('preserved')")
        database.commit()
        database.close()
        before = _inventory(self.state)
        create_backup(self.state, self.root / "backups")
        result = check_restore_drill(self.root / "backups", frontend_dir=self.frontend)
        self.assertEqual(result["status"], "pass", result)
        self.assertTrue(result["application"]["health_ready"])
        self.assertTrue(result["application"]["state_readable"])
        self.assertTrue(result["application"]["mutations_blocked"])
        self.assertEqual(result["application"]["sqlite_databases_checked"], 1)
        self.assertEqual(_inventory(self.state), before)

    def test_missing_configuration_does_not_boot_empty_defaults(self) -> None:
        (self.state / "config.json").unlink()
        with self.assertRaisesRegex(ValueError, "backed-up configuration"):
            self.probe()

    def test_duplicate_keys_and_nonfinite_values_are_rejected_without_repair(self) -> None:
        for body in ('{"markets":{},"markets":{}}', '{"theme":NaN}', '{"x":1e999}', '[]'):
            with self.subTest(body=body):
                (self.state / "config.json").write_text(body, encoding="utf-8")
                before = _inventory(self.state)
                with self.assertRaises(ValueError):
                    self.probe()
                self.assertEqual(_inventory(self.state), before)

    def test_malformed_durable_journals_are_not_silently_dropped(self) -> None:
        for field in ("copy_activity_outbox", "mutation_journal"):
            for entries in ([None], [{}], {"unexpected": "shape"}):
                with self.subTest(field=field, entries=entries):
                    (self.state / "config.json").write_text(json.dumps({field: entries}), encoding="utf-8")
                    with self.assertRaises(ConfigLoadError):
                        self.probe()

    def test_invalid_copy_risk_config_cannot_be_accepted_as_recovery_evidence(self) -> None:
        for settings in ({"live": "false"}, {"copy_percentage": "invalid"}, {"scale": 2}):
            with self.subTest(settings=settings):
                (self.state / "config.json").write_text(json.dumps({"copytrading": settings}), encoding="utf-8")
                before = _inventory(self.state)
                with self.assertRaises(ConfigLoadError):
                    self.probe()
                self.assertEqual(_inventory(self.state), before)

    def test_invalid_store_schema_is_not_quarantined_or_treated_as_empty(self) -> None:
        for filename in STATE_FILENAMES.values():
            with self.subTest(filename=filename):
                target = self.state / filename
                target.write_text('{"version":999,"entries":[]}', encoding="utf-8")
                before = _inventory(self.state)
                with self.assertRaisesRegex(ValueError, "not ready"):
                    self.probe()
                self.assertEqual(_inventory(self.state), before)
                target.unlink()

    def test_isolation_removes_credentials_proxy_and_store_overrides(self) -> None:
        self.assertEqual(STATE_FILENAMES, {name: path.name for name, path in DURABLE_STATE_PATHS.items()})
        with patch.dict(os.environ, {
            "POLYMARKET_PRIVATE_KEY": "sensitive-example",
            "HTTPS_PROXY": "http://proxy.invalid",
            "PYTHONPATH": "untrusted",
            "POLYMARKET_ANALYTICS_CACHE_PATH": "production-store.json",
        }):
            environment = isolated_environment(self.state)
        self.assertNotIn("POLYMARKET_PRIVATE_KEY", environment)
        self.assertNotIn("HTTPS_PROXY", environment)
        self.assertNotIn("PYTHONPATH", environment)
        self.assertEqual(environment["POLYMARKET_ANALYTICS_CACHE_PATH"], str(self.state / STATE_FILENAMES["POLYMARKET_ANALYTICS_CACHE_PATH"]))

    def test_backend_dns_and_connections_are_blocked(self) -> None:
        from web_api import ReactGuiServer

        original = ReactGuiServer.readiness_snapshot
        for operation in ("dns", "connect", "connect_ex"):
            def malicious_readiness(server, operation=operation):
                try:
                    if operation == "dns":
                        socket.getaddrinfo("venue.invalid", 443)
                    else:
                        with socket.socket() as sock:
                            getattr(sock, operation)(("127.0.0.1", 9))
                except PermissionError:
                    pass
                return original(server)

            with self.subTest(operation=operation), patch.object(ReactGuiServer, "readiness_snapshot", malicious_readiness):
                with self.assertRaisesRegex(ValueError, "outbound"):
                    self.probe()

    def test_application_cannot_silently_change_restored_bytes(self) -> None:
        from web_api import ReactGuiServer

        original = ReactGuiServer.readiness_snapshot

        def altered_readiness(server):
            result = original(server)
            with (self.state / "config.json").open("a", encoding="utf-8") as stream:
                stream.write("\n")
            return result

        with patch.object(ReactGuiServer, "readiness_snapshot", altered_readiness):
            with self.assertRaisesRegex(ValueError, "changed the backup"):
                self.probe()

    def test_sqlite_foreign_key_violation_fails_without_writes(self) -> None:
        connection = sqlite3.connect(self.state / "broken.db")
        connection.executescript("CREATE TABLE parent(id INTEGER PRIMARY KEY); CREATE TABLE child(id INTEGER REFERENCES parent(id)); INSERT INTO child VALUES (1);")
        connection.close()
        before = _inventory(self.state)
        with self.assertRaisesRegex(ValueError, "foreign-key"):
            _check_sqlite(self.state)
        self.assertEqual(_inventory(self.state), before)

    def test_invalid_sqlite_is_not_reinitialized(self) -> None:
        for body in (b"", b"broken SQLite contents", b"SQLite format 3\0" + b"\0" * 128):
            with self.subTest(body=body):
                (self.state / "broken.sqlite").write_bytes(body)
                before = _inventory(self.state)
                with self.assertRaises((ValueError, sqlite3.DatabaseError)):
                    _check_sqlite(self.state)
                self.assertEqual(_inventory(self.state), before)

    def test_application_timeout_fails_closed(self) -> None:
        create_backup(self.state, self.root / "backups")
        with patch("scripts.verify_production_deployment.subprocess.run", side_effect=subprocess.TimeoutExpired("probe", 60)):
            result = check_restore_drill(self.root / "backups", frontend_dir=self.frontend)
        self.assertEqual(result["status"], "fail")
        self.assertIn("60-second", result["detail"])

    def test_child_failure_details_do_not_expose_state_or_credentials(self) -> None:
        create_backup(self.state, self.root / "backups")
        with patch("scripts.verify_production_deployment.subprocess.run", return_value=subprocess.CompletedProcess("probe", 1, "private-state", "secret-token")):
            result = check_restore_drill(self.root / "backups", frontend_dir=self.frontend)
        self.assertEqual(result["status"], "fail")
        self.assertNotIn("private-state", json.dumps(result))
        self.assertNotIn("secret-token", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
