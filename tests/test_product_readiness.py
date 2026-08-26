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
        self.assertEqual(
            set(result["report"]["public_check_statuses"]),
            {"clob_time", "gamma_markets", "data_leaderboard", "bridge_supported_assets"},
        )
        self.assertIn("output", result)
        sleep.assert_called_once()

    def test_public_live_probe_requires_exact_check_keys(self) -> None:
        required = ("clob_time", "gamma_markets", "data_leaderboard", "bridge_supported_assets")

        for names in (required[:-1], (*required, "unreviewed_endpoint")):
            with self.subTest(names=names):

                def run_probe(
                    command: list[str],
                    check_names: tuple[str, ...] = names,
                    **kwargs: object,
                ) -> subprocess.CompletedProcess[str]:
                    report_path = Path(command[command.index("--report-file") + 1])
                    report_path.write_text(
                        json.dumps(
                            {
                                "ok": True,
                                "public_checks": {name: {"status": "ok"} for name in check_names},
                            }
                        ),
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(command, 0, "", "")

                with (
                    patch("scripts.check_product_readiness.subprocess.run", side_effect=run_probe),
                    patch("scripts.check_product_readiness.time.sleep"),
                ):
                    from scripts.check_product_readiness import _run_public_live

                    result = _run_public_live()

                self.assertEqual(result["status"], "fail")
                self.assertEqual(result["attempt"], 2)
                self.assertNotIn("unreviewed_endpoint", json.dumps(result))

    def test_gate_output_metadata_does_not_retain_output_contents(self) -> None:
        completed = subprocess.CompletedProcess(
            [sys.executable, "verify.py"],
            0,
            "stdout-secret-value\nsecond line\n",
            "stderr-secret-value\n",
        )
        with patch("scripts.check_product_readiness.subprocess.run", return_value=completed):
            from scripts.check_product_readiness import _run_local_gates

            result = _run_local_gates(False)

        serialized = json.dumps(result)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["output"]["stdout"]["lines"], 2)
        self.assertEqual(result["output"]["stderr"]["lines"], 1)
        self.assertNotIn("stdout-secret-value", serialized)
        self.assertNotIn("stderr-secret-value", serialized)

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
        from scripts.check_product_readiness import (
            REQUIRED_PLATFORM_CI_CHECKS,
            REQUIRED_RELEASE_ENVIRONMENT_CHECKS,
        )

        manifest = {
            "verified": True,
            "schema_version": 1,
            "reviewed_by": "test-reviewer",
            "reviewed_at": reviewed_at_now(),
            "checks": [{"name": name, "status": "pass"} for name in REQUIRED_PLATFORM_CI_CHECKS],
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
                        "checks": [
                            {"name": name, "status": "pass"} for name in REQUIRED_RELEASE_ENVIRONMENT_CHECKS
                        ],
                    }
                ),
                encoding="utf-8",
            )
            from scripts.check_product_readiness import _parser, build_report

            args = _parser().parse_args(
                [
                    "--no-run-local",
                    "--platform-ci-evidence",
                    str(platform_path),
                    "--release-environment-evidence",
                    str(release_environment_path),
                ]
            )
            with (
                patch("scripts.check_product_readiness._repository_is_clean", return_value=True),
                patch("scripts.check_product_readiness._repository_revision", return_value=revision),
            ):
                report = build_report(args)

        platform = next(item for item in report["categories"] if item["name"] == "platform_evidence")
        ci_cd = next(item for item in report["categories"] if item["name"] == "ci_cd_release")
        self.assertEqual(platform["earned"], 8)
        self.assertEqual(ci_cd["earned"], 15)

    def test_release_environment_evidence_requires_exact_security_checks(self) -> None:
        from scripts.check_product_readiness import REQUIRED_RELEASE_ENVIRONMENT_CHECKS, _reviewed_evidence

        base = {
            "verified": True,
            "schema_version": 1,
            "evidence_type": "release-environment",
            "reviewed_by": "test-reviewer",
            "reviewed_at": reviewed_at_now(),
            "source": "test",
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "release-environment.json"
            path.write_text(
                json.dumps({**base, "checks": [{"name": "release_environment_exists", "status": "pass"}]}),
                encoding="utf-8",
            )
            incomplete, detail = _reviewed_evidence(
                str(path),
                "release-environment",
                evidence_type="release-environment",
                required_checks=REQUIRED_RELEASE_ENVIRONMENT_CHECKS,
            )

            path.write_text(
                json.dumps(
                    {
                        **base,
                        "checks": [
                            {"name": name, "status": "pass"} for name in REQUIRED_RELEASE_ENVIRONMENT_CHECKS
                        ],
                    }
                ),
                encoding="utf-8",
            )
            complete, complete_detail = _reviewed_evidence(
                str(path),
                "release-environment",
                evidence_type="release-environment",
                required_checks=REQUIRED_RELEASE_ENVIRONMENT_CHECKS,
            )

            path.write_text(
                json.dumps(
                    {
                        **base,
                        "checks": [
                            *[
                                {"name": name, "status": "pass"}
                                for name in REQUIRED_RELEASE_ENVIRONMENT_CHECKS
                            ],
                            {"name": "renamed_or_unreviewed_check", "status": "pass"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            unknown, unknown_detail = _reviewed_evidence(
                str(path),
                "release-environment",
                evidence_type="release-environment",
                required_checks=REQUIRED_RELEASE_ENVIRONMENT_CHECKS,
            )

        self.assertFalse(incomplete)
        self.assertIn("missing required checks", detail)
        self.assertTrue(complete, complete_detail)
        self.assertFalse(unknown)
        self.assertIn("unknown checks", unknown_detail)

    def test_every_evidence_type_has_an_exact_check_contract(self) -> None:
        from scripts.check_product_readiness import (
            REQUIRED_EVIDENCE_CHECKS,
            REQUIRED_EVIDENCE_FIELDS,
            _reviewed_evidence,
        )

        values = {
            "source": "test source",
            "scope": "test scope",
            "environment": "production",
            "expected_version": "1.0.11",
            "source_revision": "a" * 40,
            "tag": "v1.0.11",
            "target_commit": "a" * 40,
            "assets": ["artifact.whl"],
            "run_id": 1,
            "targets": ["Windows"],
            "target_tier": "test-tier",
            "report_hash": "b" * 64,
            "live_action": True,
        }
        self.assertEqual(set(REQUIRED_EVIDENCE_CHECKS), set(REQUIRED_EVIDENCE_FIELDS))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "evidence.json"
            for evidence_type, required_checks in REQUIRED_EVIDENCE_CHECKS.items():
                with self.subTest(evidence_type=evidence_type):
                    self.assertTrue(required_checks)
                    self.assertEqual(len(required_checks), len(set(required_checks)))
                    payload = {
                        "verified": True,
                        "schema_version": 1,
                        "evidence_type": evidence_type,
                        "reviewed_by": "test-reviewer",
                        "reviewed_at": reviewed_at_now(),
                        "checks": [{"name": name, "status": "pass"} for name in required_checks],
                        **{field: values[field] for field in REQUIRED_EVIDENCE_FIELDS[evidence_type]},
                    }
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    accepted, detail = _reviewed_evidence(
                        str(path),
                        evidence_type,
                        evidence_type=evidence_type,
                    )
                    self.assertTrue(accepted, detail)

                    payload["checks"] = payload["checks"][:-1]
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    incomplete, incomplete_detail = _reviewed_evidence(
                        str(path),
                        evidence_type,
                        evidence_type=evidence_type,
                    )
                    self.assertFalse(incomplete)
                    self.assertIn("missing required checks", incomplete_detail)

    def test_evidence_scalar_types_are_not_coerced(self) -> None:
        from scripts.check_product_readiness import REQUIRED_REPOSITORY_SETTINGS_CHECKS, _reviewed_evidence

        base = {
            "verified": True,
            "schema_version": 1,
            "evidence_type": "repository-settings",
            "reviewed_by": "test-reviewer",
            "reviewed_at": reviewed_at_now(),
            "source": "test source",
            "checks": [{"name": name, "status": "pass"} for name in REQUIRED_REPOSITORY_SETTINGS_CHECKS],
        }
        invalid_variants = (
            {"schema_version": True},
            {"reviewed_by": "   "},
            {"reviewed_by": "reviewer\rsecond-line"},
            {"source": []},
            {"source": " \t "},
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "repository-settings.json"
            for invalid in invalid_variants:
                with self.subTest(invalid=invalid):
                    path.write_text(json.dumps({**base, **invalid}), encoding="utf-8")
                    accepted, detail = _reviewed_evidence(
                        str(path),
                        "repository settings",
                        evidence_type="repository-settings",
                    )
                    self.assertFalse(accepted, detail)

    def test_boolean_run_id_and_non_string_target_are_rejected(self) -> None:
        from scripts.check_product_readiness import (
            REQUIRED_PLATFORM_CHECKS,
            REQUIRED_PLATFORM_CI_CHECKS,
            _reviewed_evidence,
        )

        base = {
            "verified": True,
            "schema_version": 1,
            "reviewed_by": "test-reviewer",
            "reviewed_at": reviewed_at_now(),
            "source": "test source",
            "scope": "hosted",
            "source_revision": "a" * 40,
        }
        payloads = (
            {
                **base,
                "evidence_type": "platform-ci",
                "run_id": True,
                "checks": [{"name": name, "status": "pass"} for name in REQUIRED_PLATFORM_CI_CHECKS],
            },
            {
                **base,
                "evidence_type": "platform",
                "targets": ["Windows", 11],
                "checks": [{"name": name, "status": "pass"} for name in REQUIRED_PLATFORM_CHECKS],
            },
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "evidence.json"
            for payload in payloads:
                with self.subTest(evidence_type=payload["evidence_type"]):
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    accepted, detail = _reviewed_evidence(
                        str(path),
                        str(payload["evidence_type"]),
                        evidence_type=str(payload["evidence_type"]),
                    )
                    self.assertFalse(accepted, detail)

    def test_current_repository_settings_manifest_remains_accepted(self) -> None:
        from scripts.check_product_readiness import _reviewed_evidence

        accepted, detail = _reviewed_evidence(
            str(ROOT / "evidence" / "repository-settings.json"),
            "repository settings",
            evidence_type="repository-settings",
            now=datetime(2026, 8, 26, tzinfo=timezone.utc),
        )

        self.assertTrue(accepted, detail)

    def test_checked_in_platform_ci_manifest_is_rejected_for_an_old_revision(self) -> None:
        from scripts.check_product_readiness import _reviewed_evidence

        revision = repository_revision()
        accepted, detail = _reviewed_evidence(
            str(ROOT / "evidence" / "platform-ci.json"),
            "platform CI",
            evidence_type="platform-ci",
            revision_field="source_revision",
            expected_revision=revision,
            now=datetime(2026, 8, 26, tzinfo=timezone.utc),
        )

        self.assertFalse(accepted)
        self.assertIn("must match the current repository revision", detail)

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
        from scripts.check_product_readiness import REQUIRED_PLATFORM_CHECKS, REQUIRED_PLATFORM_CI_CHECKS

        revision = repository_revision()
        manifest = {
            "verified": True,
            "schema_version": 1,
            "reviewed_by": "test-reviewer",
            "reviewed_at": reviewed_at_now(),
            "source": "test",
            "scope": "hosted-ci",
            "source_revision": revision,
        }
        with tempfile.TemporaryDirectory() as temporary:
            platform_ci_path = Path(temporary) / "platform-ci.json"
            platform_path = Path(temporary) / "platform.json"
            platform_ci_path.write_text(
                json.dumps(
                    {
                        **manifest,
                        "evidence_type": "platform-ci",
                        "run_id": 1,
                        "checks": [
                            {"name": name, "status": "pass"} for name in REQUIRED_PLATFORM_CI_CHECKS
                        ],
                    }
                ),
                encoding="utf-8",
            )
            platform_path.write_text(
                json.dumps(
                    {
                        **manifest,
                        "evidence_type": "platform",
                        "targets": ["Windows"],
                        "checks": [{"name": name, "status": "pass"} for name in REQUIRED_PLATFORM_CHECKS],
                    }
                ),
                encoding="utf-8",
            )
            from scripts.check_product_readiness import _parser, build_report

            args = _parser().parse_args(
                [
                    "--no-run-local",
                    "--platform-ci-evidence",
                    str(platform_ci_path),
                    "--platform-evidence",
                    str(platform_path),
                ]
            )
            with (
                patch("scripts.check_product_readiness._repository_is_clean", return_value=True),
                patch("scripts.check_product_readiness._repository_revision", return_value=revision),
            ):
                report = build_report(args)

        platform = next(item for item in report["categories"] if item["name"] == "platform_evidence")
        self.assertEqual(platform["earned"], 10)

    def test_core_local_profile_cannot_award_the_final_readiness_point(self) -> None:
        from scripts.check_product_readiness import _parser, build_report

        core_args = _parser().parse_args([])
        full_args = _parser().parse_args(["--full-local"])
        with (
            patch("scripts.check_product_readiness._run_local_gates", return_value={"status": "pass"}),
            patch("scripts.check_product_readiness._repository_is_clean", return_value=False),
        ):
            core_report = build_report(core_args)
            full_report = build_report(full_args)

        core_tests = next(item for item in core_report["categories"] if item["name"] == "tests_correctness")
        full_tests = next(item for item in full_report["categories"] if item["name"] == "tests_correctness")
        self.assertEqual(core_tests["earned"], 17)
        self.assertEqual(full_tests["earned"], 18)
        self.assertTrue(any("--full-local" in item for item in core_tests["missing"]))

    def test_revision_bound_points_are_revoked_when_head_changes_during_scoring(self) -> None:
        from scripts.check_product_readiness import REQUIRED_PLATFORM_CI_CHECKS, _parser, build_report

        initial_revision = "a" * 40
        final_revision = "b" * 40
        manifest = {
            "verified": True,
            "schema_version": 1,
            "evidence_type": "platform-ci",
            "reviewed_by": "test-reviewer",
            "reviewed_at": reviewed_at_now(),
            "source": "test source",
            "scope": "hosted-ci",
            "run_id": 1,
            "source_revision": initial_revision,
            "checks": [{"name": name, "status": "pass"} for name in REQUIRED_PLATFORM_CI_CHECKS],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "platform-ci.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            args = _parser().parse_args(["--no-run-local", "--platform-ci-evidence", str(path)])
            with (
                patch("scripts.check_product_readiness._repository_is_clean", side_effect=(True, True)),
                patch(
                    "scripts.check_product_readiness._repository_revision",
                    side_effect=(initial_revision, final_revision),
                ),
            ):
                report = build_report(args)

        platform = next(item for item in report["categories"] if item["name"] == "platform_evidence")
        self.assertEqual(platform["earned"], 5)
        self.assertEqual(report["checks"]["repository"]["status"], "fail")
        self.assertEqual(report["checks"]["repository"]["initial_revision"], initial_revision)
        self.assertEqual(report["checks"]["repository"]["final_revision"], final_revision)
        self.assertTrue(any("was revoked" in item for item in platform["missing"]))

    def test_dirty_worktree_cannot_receive_revision_bound_evidence_points(self) -> None:
        from scripts.check_product_readiness import _parser, build_report

        revision = repository_revision()
        manifest = {
            "verified": True,
            "schema_version": 1,
            "evidence_type": "platform-ci",
            "reviewed_by": "test-reviewer",
            "reviewed_at": reviewed_at_now(),
            "source": "test",
            "scope": "hosted-ci",
            "run_id": 1,
            "source_revision": revision,
            "checks": [{"name": "hosted_matrix", "status": "pass"}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "platform-ci.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            args = _parser().parse_args(["--no-run-local", "--platform-ci-evidence", str(path)])
            with patch("scripts.check_product_readiness._repository_is_clean", return_value=False):
                report = build_report(args)

        platform = next(item for item in report["categories"] if item["name"] == "platform_evidence")
        self.assertEqual(platform["earned"], 5)
        self.assertEqual(report["checks"]["repository"]["status"], "fail")
        self.assertTrue(any("revision is unavailable" in item for item in platform["missing"]))

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
