from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent


def reviewed_at_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def repository_revision() -> str:
    from scripts.check_product_readiness import _repository_revision

    revision = _repository_revision()
    if len(revision) != 40:
        raise AssertionError("tests require a Git checkout with a resolvable HEAD")
    return revision


class ProductReadinessTests(unittest.TestCase):
    def test_public_live_probe_retries_transient_failure(self) -> None:
        calls = 0

        def run_probe(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            nonlocal calls
            calls += 1
            report_path = Path(command[command.index("--report-file") + 1])
            if calls == 2:
                report_path.write_text(
                    json.dumps(
                        {
                            "ok": True,
                            "public_checks": {
                                name: {"status": "ok"}
                                for name in ("clob_time", "gamma_markets", "data_leaderboard", "bridge_supported_assets")
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(command, 0, "", "")
            return subprocess.CompletedProcess(command, 1, "", "transient endpoint failure")

        with (
            patch("scripts.check_product_readiness.subprocess.run", side_effect=run_probe),
            patch("scripts.check_product_readiness.time.sleep") as sleep,
        ):
            from scripts.check_product_readiness import _run_public_live

            result = _run_public_live()

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["attempt"], 2)
        self.assertEqual(calls, 2)
        sleep.assert_called_once()

    def test_public_live_probe_fails_after_retries(self) -> None:
        calls = 0

        def run_probe(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            nonlocal calls
            calls += 1
            return subprocess.CompletedProcess(command, 1, "", "transient endpoint failure")

        with (
            patch("scripts.check_product_readiness.subprocess.run", side_effect=run_probe),
            patch("scripts.check_product_readiness.time.sleep") as sleep,
        ):
            from scripts.check_product_readiness import _run_public_live

            result = _run_public_live()

        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["attempt"], 2)
        self.assertEqual(calls, 2)
        sleep.assert_called_once()

    def test_readiness_scorer_reports_conservative_static_score(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/check_product_readiness.py", "--no-run-local", "--json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["out_of"], 100)
        self.assertLess(report["score"], 100)
        self.assertEqual(report["status"], "not_ready")
        self.assertEqual({item["name"] for item in report["categories"]}, {
            "architecture_scope",
            "tests_correctness",
            "security_safety",
            "ci_cd_release",
            "operations_recovery",
            "platform_evidence",
            "live_acceptance",
        })

    def test_readiness_document_defines_external_evidence_boundary(self) -> None:
        text = (ROOT / "docs" / "PRODUCTION_READINESS.md").read_text(encoding="utf-8")
        for fragment in (
            "repository's conservative",
            "External Evidence Manifests",
            "credentialed",
            "funded",
            "Do not put venue credentials",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, text)

    def test_unreviewed_external_evidence_does_not_award_points(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "deployment.json"
            path.write_text(
                json.dumps(
                    {
                        "verified": True,
                        "schema_version": 1,
                        "evidence_type": "deployment",
                        "source": "test",
                        "checks": [{"name": "health", "status": "pass"}],
                    }
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/check_product_readiness.py",
                    "--no-run-local",
                    "--deployment-evidence",
                    str(path),
                    "--json",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        operations = next(item for item in report["categories"] if item["name"] == "operations_recovery")
        self.assertEqual(operations["earned"], 12)
        self.assertTrue(any("reviewed_by" in item for item in operations["missing"]))

    def test_reviewed_partial_external_evidence_awards_only_its_scoped_points(self) -> None:
        manifest = {
            "verified": True,
            "schema_version": 1,
            "reviewed_by": "test-reviewer",
            "reviewed_at": reviewed_at_now(),
            "checks": [{"name": "check", "status": "pass"}],
        }
        revision = repository_revision()
        with tempfile.TemporaryDirectory() as temporary:
            platform_path = Path(temporary) / "platform-ci.json"
            release_environment_path = Path(temporary) / "release-environment.json"
            platform_path.write_text(
                json.dumps(
                    {
                        **manifest,
                        "evidence_type": "platform-ci",
                        "source": "test",
                        "scope": "hosted-ci",
                        "run_id": 1,
                        "source_revision": revision,
                    }
                ),
                encoding="utf-8",
            )
            release_environment_path.write_text(
                json.dumps(
                    {
                        **manifest,
                        "evidence_type": "release-environment",
                        "source": "test",
                    }
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/check_product_readiness.py",
                    "--no-run-local",
                    "--platform-ci-evidence",
                    str(platform_path),
                    "--release-environment-evidence",
                    str(release_environment_path),
                    "--json",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        platform = next(item for item in report["categories"] if item["name"] == "platform_evidence")
        ci_cd = next(item for item in report["categories"] if item["name"] == "ci_cd_release")
        self.assertEqual(platform["earned"], 8)
        self.assertEqual(ci_cd["earned"], 15)

    def test_stale_reviewed_evidence_is_rejected(self) -> None:
        from scripts.check_product_readiness import EVIDENCE_MAX_AGE_DAYS, _reviewed_evidence

        now = datetime.now(timezone.utc)
        revision = repository_revision()
        payload = {
            "verified": True,
            "schema_version": 1,
            "evidence_type": "platform-ci",
            "reviewed_by": "test-reviewer",
            "reviewed_at": (now - timedelta(days=EVIDENCE_MAX_AGE_DAYS + 1)).isoformat(),
            "source": "test",
            "scope": "hosted-ci",
            "run_id": 1,
            "source_revision": revision,
            "checks": [{"name": "check", "status": "pass"}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "platform-ci.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            accepted, detail = _reviewed_evidence(
                str(path),
                "platform CI",
                evidence_type="platform-ci",
                required_fields=("scope", "run_id", "source_revision"),
                revision_field="source_revision",
                expected_revision=revision,
                now=now,
            )

        self.assertFalse(accepted)
        self.assertIn("stale", detail)

    def test_revision_bound_evidence_for_another_commit_is_rejected(self) -> None:
        from scripts.check_product_readiness import _reviewed_evidence

        revision = repository_revision()
        other_revision = "0" * 40 if revision != "0" * 40 else "1" * 40
        payload = {
            "verified": True,
            "schema_version": 1,
            "evidence_type": "platform-ci",
            "reviewed_by": "test-reviewer",
            "reviewed_at": reviewed_at_now(),
            "source": "test",
            "scope": "hosted-ci",
            "run_id": 1,
            "source_revision": other_revision,
            "checks": [{"name": "check", "status": "pass"}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "platform-ci.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            accepted, detail = _reviewed_evidence(
                str(path),
                "platform CI",
                evidence_type="platform-ci",
                required_fields=("scope", "run_id", "source_revision"),
                revision_field="source_revision",
                expected_revision=revision,
            )

        self.assertFalse(accepted)
        self.assertIn("must match the current repository revision", detail)

    def test_current_revision_platform_evidence_awards_only_platform_points(self) -> None:
        revision = repository_revision()
        manifest = {
            "verified": True,
            "schema_version": 1,
            "reviewed_by": "test-reviewer",
            "reviewed_at": reviewed_at_now(),
            "source": "test",
            "scope": "hosted-ci",
            "source_revision": revision,
            "checks": [{"name": "check", "status": "pass"}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            platform_ci_path = Path(temporary) / "platform-ci.json"
            platform_path = Path(temporary) / "platform.json"
            platform_ci_path.write_text(
                json.dumps({**manifest, "evidence_type": "platform-ci", "run_id": 1}),
                encoding="utf-8",
            )
            platform_path.write_text(
                json.dumps({**manifest, "evidence_type": "platform", "targets": ["Windows"]}),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/check_product_readiness.py",
                    "--no-run-local",
                    "--platform-ci-evidence",
                    str(platform_ci_path),
                    "--platform-evidence",
                    str(platform_path),
                    "--json",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        platform = next(item for item in report["categories"] if item["name"] == "platform_evidence")
        self.assertEqual(platform["earned"], 10)

    def test_release_history_must_match_current_version_and_revision(self) -> None:
        revision = repository_revision()
        manifest = {
            "verified": True,
            "schema_version": 1,
            "evidence_type": "release-history",
            "reviewed_by": "test-reviewer",
            "reviewed_at": reviewed_at_now(),
            "source": "test",
            "scope": "published-release",
            "tag": "v0.0.0",
            "target_commit": revision,
            "checks": [{"name": "check", "status": "pass"}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "release-history.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/check_product_readiness.py",
                    "--no-run-local",
                    "--release-history-evidence",
                    str(path),
                    "--json",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        ci_cd = next(item for item in report["categories"] if item["name"] == "ci_cd_release")
        self.assertEqual(ci_cd["earned"], 14)
        self.assertTrue(any("field tag" in item for item in ci_cd["missing"]))

    def test_mislabeled_evidence_cannot_award_a_different_tier(self) -> None:
        manifest = {
            "verified": True,
            "schema_version": 1,
            "evidence_type": "release-environment",
            "reviewed_by": "test-reviewer",
            "reviewed_at": reviewed_at_now(),
            "source": "test",
            "checks": [{"name": "check", "status": "pass"}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "mislabeled.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/check_product_readiness.py",
                    "--no-run-local",
                    "--platform-ci-evidence",
                    str(path),
                    "--json",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        platform = next(item for item in report["categories"] if item["name"] == "platform_evidence")
        self.assertEqual(platform["earned"], 5)
        self.assertTrue(any("evidence_type=\'platform-ci\'" in item for item in platform["missing"]))


if __name__ == "__main__":
    unittest.main()
