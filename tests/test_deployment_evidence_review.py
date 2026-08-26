from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.review_deployment_evidence import (
    DeploymentEvidenceError,
    required_check_names,
    review_deployment_report,
)


REVISION = "a" * 40
FRONTEND_SHA256 = "b" * 64
VERSION = "1.0.11"
NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


def _report() -> dict[str, object]:
    checks: list[dict[str, object]] = [
        {"name": name, "status": "pass", "detail": "verified"}
        for name in sorted(required_check_names())
    ]
    indexed = {str(check["name"]): check for check in checks}
    indexed["loopback_health"].update(
        {
            "api_version": VERSION,
            "runtime_source_revision": REVISION,
            "runtime_frontend_sha256": FRONTEND_SHA256,
            "disk_frontend_sha256": FRONTEND_SHA256,
        }
    )
    indexed["public_https_proxy"].update(
        {
            "api_version": VERSION,
            "runtime_source_revision": REVISION,
            "runtime_frontend_sha256": FRONTEND_SHA256,
            "unauthenticated_probes": 5,
        }
    )
    indexed["deployment_host_identity"].update(
        {"deployment_provider": "bare-metal", "host_identity_sha256": "d" * 64}
    )
    indexed["durable_state_wiring"].update(
        {
            "durable_store_count": 4,
            "state_directory": "/var/lib/market-sentinel",
            "backup_source": "/var/lib/market-sentinel",
        }
    )
    indexed["verified_recent_state_backup"].update(
        {
            "created_at": "2026-08-26T11:00:00Z",
            "archive": "market-sentinel-state-20260826T110000Z.tar.gz",
            "backup_age_seconds": 1800,
            "sha256": "c" * 64,
            "file_count": 3,
            "verified_bytes": 42,
            "verified_pairs": 1,
            "invalid_pairs": 0,
            "orphan_archives": 0,
            "orphan_manifests": 0,
        }
    )
    indexed["verified_restore_drill"].update(
        {
            "mode": "isolated_full_restore",
            "archive": "market-sentinel-state-20260826T110000Z.tar.gz",
            "backup_created_at": "2026-08-26T11:00:00Z",
            "backup_sha256": "c" * 64,
            "restored_file_count": 3,
            "restored_bytes": 42,
            "completed_at": "2026-08-26T11:25:00Z",
        }
    )
    indexed["verified_production_rollback_drill"].update(
        {
            "drill_id": "00000000-0000-4000-8000-000000000001",
            "report_sha256": "f" * 64,
            "completed_at": "2026-08-26T10:30:00Z",
            "rollback_revision": "e" * 40,
            "final_revision": REVISION,
            "step_count": 5,
        }
    )
    return {
        "schema_version": 1,
        "collected_at": "2026-08-26T11:30:00Z",
        "source": {
            "project_version": VERSION,
            "git_revision": REVISION,
            "git_revision_status": "ok",
            "git_worktree_status": "clean",
        },
        "collection": {
            "mode": "production",
            "systemd_requested": True,
            "public_proxy_requested": True,
            "public_origin": "https://markets.example.net",
            "expected_version": VERSION,
            "expected_source_revision": REVISION,
            "expected_frontend_sha256": FRONTEND_SHA256,
            "deployment_provider": "bare-metal",
            "host_identity_sha256": "d" * 64,
            "restore_drill_requested": True,
            "rollback_drill_requested": True,
            "run_id": 0,
            "run_attempt": 0,
            "nonce": "",
        },
        "status": "ok",
        "checks": checks,
    }


class DeploymentEvidenceReviewTests(unittest.TestCase):
    def _review(self, report: dict[str, object], *, now: datetime = NOW) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deployment.json"
            path.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
            return review_deployment_report(
                path,
                expected_version=VERSION,
                expected_revision=REVISION,
                now=now,
            )

    def _review_text(self, text: str) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deployment.json"
            path.write_text(text, encoding="utf-8")
            return review_deployment_report(
                path,
                expected_version=VERSION,
                expected_revision=REVISION,
                now=NOW,
            )

    def test_accepts_complete_fresh_production_report(self) -> None:
        result = self._review(_report())

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["environment"], "production")
        self.assertEqual(result["source_revision"], REVISION)
        self.assertEqual(result["check_count"], len(required_check_names()))
        self.assertEqual(len(str(result["raw_report_sha256"])), 64)

    def test_rejects_local_smoke_or_missing_public_proxy(self) -> None:
        for field, value in (("mode", "local_smoke"), ("systemd_requested", False), ("public_proxy_requested", False)):
            with self.subTest(field=field):
                report = _report()
                collection = report["collection"]
                assert isinstance(collection, dict)
                collection[field] = value
                with self.assertRaises(DeploymentEvidenceError):
                    self._review(report)

    def test_rejects_stale_report_even_if_reviewed_now(self) -> None:
        report = _report()
        report["collected_at"] = "2026-08-24T11:30:00Z"

        with self.assertRaisesRegex(DeploymentEvidenceError, "stale"):
            self._review(report)

    def test_rejects_source_or_runtime_identity_tampering(self) -> None:
        mutations = (
            ("source", "git_revision", "d" * 40),
            ("collection", "expected_source_revision", "d" * 40),
        )
        for section, field, value in mutations:
            with self.subTest(section=section, field=field):
                report = _report()
                payload = report[section]
                assert isinstance(payload, dict)
                payload[field] = value
                with self.assertRaises(DeploymentEvidenceError):
                    self._review(report)

        report = _report()
        checks = report["checks"]
        assert isinstance(checks, list)
        loopback = next(check for check in checks if check["name"] == "loopback_health")
        loopback["runtime_frontend_sha256"] = "d" * 64
        with self.assertRaisesRegex(DeploymentEvidenceError, "frontend"):
            self._review(report)

    def test_rejects_missing_unknown_duplicate_or_failed_checks(self) -> None:
        base = _report()
        checks = base["checks"]
        assert isinstance(checks, list)

        missing = copy.deepcopy(base)
        missing_checks = missing["checks"]
        assert isinstance(missing_checks, list)
        missing_checks.pop()

        unknown = copy.deepcopy(base)
        unknown_checks = unknown["checks"]
        assert isinstance(unknown_checks, list)
        unknown_checks.append({"name": "self_asserted_extra", "status": "pass"})

        duplicate = copy.deepcopy(base)
        duplicate_checks = duplicate["checks"]
        assert isinstance(duplicate_checks, list)
        duplicate_checks.append(copy.deepcopy(duplicate_checks[0]))

        failed = copy.deepcopy(base)
        failed_checks = failed["checks"]
        assert isinstance(failed_checks, list)
        failed_checks[0]["status"] = "fail"

        for label, report in (("missing", missing), ("unknown", unknown), ("duplicate", duplicate), ("failed", failed)):
            with self.subTest(label=label), self.assertRaises(DeploymentEvidenceError):
                self._review(report)

    def test_rejects_stale_or_inconsistent_backup(self) -> None:
        report = _report()
        checks = report["checks"]
        assert isinstance(checks, list)
        backup = next(check for check in checks if check["name"] == "verified_recent_state_backup")
        backup["created_at"] = (NOW - timedelta(days=2)).isoformat().replace("+00:00", "Z")

        with self.assertRaisesRegex(DeploymentEvidenceError, "backup"):
            self._review(report)

    def test_rejects_handwritten_wrapper_manifest(self) -> None:
        wrapper = {
            "schema_version": 1,
            "verified": True,
            "reviewed_at": "2026-08-26T12:00:00Z",
            "source_revision": REVISION,
            "checks": [{"name": name, "status": "pass"} for name in sorted(required_check_names())],
        }

        with self.assertRaises(DeploymentEvidenceError):
            self._review(wrapper)

    def test_rejects_duplicate_keys_and_nonfinite_numbers(self) -> None:
        valid = json.dumps(_report(), sort_keys=True)
        duplicate = valid.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1', 1)
        nonfinite = valid.replace('"schema_version": 1', '"schema_version": NaN', 1)

        for label, text in (("duplicate", duplicate), ("nonfinite", nonfinite)):
            with self.subTest(label=label), self.assertRaises(DeploymentEvidenceError):
                self._review_text(text)


if __name__ == "__main__":
    unittest.main()
