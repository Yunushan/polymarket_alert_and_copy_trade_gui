from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
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


def raw_production_deployment_report(
    revision: str,
    version: str,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Build semantically valid but deliberately unattested collector output."""

    from scripts.review_deployment_evidence import required_check_names
    from scripts.verify_production_deployment import DURABLE_STATE_PATHS, PUBLIC_PROXY_AUTH_PROBES

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    collected_at = current - timedelta(minutes=2)
    backup_created_at = collected_at - timedelta(minutes=30)
    restore_completed_at = collected_at - timedelta(minutes=1)
    rollback_completed_at = collected_at - timedelta(seconds=30)
    frontend_sha256 = "b" * 64
    backup_sha256 = "c" * 64
    host_identity_sha256 = "d" * 64
    rollback_revision = "e" * 40
    backup_archive = "market-sentinel-state-test.tar.gz"
    checks: list[dict[str, object]] = [
        {"name": name, "status": "pass", "detail": "self-asserted test fixture"}
        for name in sorted(required_check_names())
    ]
    indexed = {str(check["name"]): check for check in checks}
    indexed["loopback_health"].update(
        {
            "api_version": version,
            "runtime_source_revision": revision,
            "runtime_frontend_sha256": frontend_sha256,
            "disk_frontend_sha256": frontend_sha256,
        }
    )
    indexed["public_https_proxy"].update(
        {
            "api_version": version,
            "unauthenticated_probes": len(PUBLIC_PROXY_AUTH_PROBES),
            "runtime_source_revision": revision,
            "runtime_frontend_sha256": frontend_sha256,
        }
    )
    indexed["deployment_host_identity"].update(
        {
            "deployment_provider": "test-provider",
            "host_identity_sha256": host_identity_sha256,
        }
    )
    indexed["durable_state_wiring"].update(
        {
            "durable_store_count": len(DURABLE_STATE_PATHS),
            "state_directory": "/var/lib/market-sentinel",
            "backup_source": "/var/lib/market-sentinel",
        }
    )
    indexed["verified_recent_state_backup"].update(
        {
            "created_at": backup_created_at.isoformat().replace("+00:00", "Z"),
            "backup_age_seconds": 30 * 60,
            "archive": backup_archive,
            "sha256": backup_sha256,
            "file_count": 1,
            "verified_bytes": 128,
            "verified_pairs": 1,
            "invalid_pairs": 0,
            "orphan_archives": 0,
            "orphan_manifests": 0,
        }
    )
    indexed["verified_restore_drill"].update(
        {
            "mode": "isolated_full_restore",
            "archive": backup_archive,
            "backup_created_at": backup_created_at.isoformat().replace("+00:00", "Z"),
            "backup_sha256": backup_sha256,
            "restored_file_count": 1,
            "restored_bytes": 128,
            "completed_at": restore_completed_at.isoformat().replace("+00:00", "Z"),
        }
    )
    indexed["verified_production_rollback_drill"].update(
        {
            "drill_id": "00000000-0000-4000-8000-000000000001",
            "report_sha256": "f" * 64,
            "completed_at": rollback_completed_at.isoformat().replace("+00:00", "Z"),
            "rollback_revision": rollback_revision,
            "final_revision": revision,
            "step_count": 5,
        }
    )
    return {
        "schema_version": 1,
        "collected_at": collected_at.isoformat().replace("+00:00", "Z"),
        "source": {
            "project_version": version,
            "git_revision": revision,
            "git_revision_status": "ok",
            "git_worktree_status": "clean",
        },
        "status": "ok",
        "checks": checks,
        "collection": {
            "mode": "production",
            "systemd_requested": True,
            "public_proxy_requested": True,
            "public_origin": "https://markets.example.net",
            "expected_version": version,
            "expected_source_revision": revision,
            "expected_frontend_sha256": frontend_sha256,
            "deployment_provider": "test-provider",
            "host_identity_sha256": host_identity_sha256,
            "restore_drill_requested": True,
            "rollback_drill_requested": True,
            "run_id": 0,
            "run_attempt": 0,
            "nonce": "",
        },
    }


def score_deployment_evidence(path: Path, revision: str) -> dict[str, Any]:
    from scripts.check_product_readiness import _parser, build_report

    args = _parser().parse_args(["--no-run-local", "--deployment-evidence", str(path)])
    with (
        patch("scripts.check_product_readiness._repository_is_clean", return_value=True),
        patch("scripts.check_product_readiness._repository_revision", return_value=revision),
    ):
        return build_report(args)


def attested_public_live_payload(revision: str, *, now: datetime | None = None) -> dict[str, object]:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    started = current - timedelta(minutes=4)
    completed = current - timedelta(minutes=3)
    generated = current - timedelta(minutes=2)
    return {
        "ok": True,
        "mode": "public_only",
        "market_id": "polymarket",
        "public_checks": {
            name: {"status": "ok", "detail": "read-only endpoint responded"}
            for name in ("clob_time", "gamma_markets", "data_leaderboard", "bridge_supported_assets")
        },
        "safety": {
            "dotenv_loaded": False,
            "credentials_present": False,
            "credential_variables_present": [],
            "authenticated_reads_attempted": False,
            "authenticated_user_websocket_attempted": False,
            "bridge_mutations_attempted": False,
            "funded_orders_attempted": False,
            "public_requests_read_only": True,
        },
        "evidence": {
            "schema_version": 1,
            "profile": "public-only",
            "repository": "Yunushan/market-sentinel",
            "source_revision": revision,
            "run_id": 123456,
            "run_attempt": 1,
            "workflow": ".github/workflows/ci.yml",
            "workflow_name": "CI",
            "workflow_ref": "Yunushan/market-sentinel/.github/workflows/ci.yml@refs/heads/main",
            "event": "workflow_dispatch",
            "runner_environment": "github-hosted",
            "generated_at": generated.isoformat().replace("+00:00", "Z"),
            "started_at": started.isoformat().replace("+00:00", "Z"),
            "completed_at": completed.isoformat().replace("+00:00", "Z"),
        },
    }


def successful_public_live_gh_run(
    revision: str,
    *,
    now: datetime | None = None,
    run_overrides: dict[str, object] | None = None,
    jobs_overrides: dict[str, object] | None = None,
    attestation_mutator: Callable[[list[dict[str, Any]]], None] | None = None,
) -> Callable[..., subprocess.CompletedProcess[bytes]]:
    current = now or datetime.now(timezone.utc)

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if command[:3] == ["gh", "attestation", "verify"]:
            import hashlib

            report_hash = hashlib.sha256(Path(command[3]).read_bytes()).hexdigest()
            workflow_ref = "refs/heads/main"
            workflow_uri = (
                "https://github.com/Yunushan/market-sentinel/.github/workflows/ci.yml@" + workflow_ref
            )
            repository_uri = "https://github.com/Yunushan/market-sentinel"
            invocation_uri = "https://github.com/Yunushan/market-sentinel/actions/runs/123456/attempts/1"
            attestation = [
                {
                    "attestation": {"bundle": "verified"},
                    "verificationResult": {
                        "mediaType": "application/vnd.dev.sigstore.verificationresult+json;version=0.1",
                        "statement": {
                            "_type": "https://in-toto.io/Statement/v1",
                            "predicateType": "https://slsa.dev/provenance/v1",
                            "subject": [
                                {
                                    "name": "public-polymarket-live.json",
                                    "digest": {"sha256": report_hash},
                                }
                            ],
                            "predicate": {
                                "buildDefinition": {
                                    "buildType": "https://actions.github.io/buildtypes/workflow/v1",
                                    "externalParameters": {
                                        "workflow": {
                                            "path": ".github/workflows/ci.yml",
                                            "ref": workflow_ref,
                                            "repository": repository_uri,
                                        }
                                    },
                                    "internalParameters": {
                                        "github": {
                                            "event_name": "workflow_dispatch",
                                            "runner_environment": "github-hosted",
                                        }
                                    },
                                    "resolvedDependencies": [
                                        {
                                            "uri": f"git+{repository_uri}@{workflow_ref}",
                                            "digest": {"gitCommit": revision},
                                        }
                                    ],
                                },
                                "runDetails": {
                                    "builder": {"id": workflow_uri},
                                    "metadata": {"invocationId": invocation_uri},
                                },
                            },
                        },
                        "signature": {
                            "certificate": {
                                "subjectAlternativeName": workflow_uri,
                                "issuer": "https://token.actions.githubusercontent.com",
                                "buildSignerURI": workflow_uri,
                                "buildSignerDigest": revision,
                                "runnerEnvironment": "github-hosted",
                                "sourceRepositoryURI": repository_uri,
                                "sourceRepositoryDigest": revision,
                                "sourceRepositoryRef": workflow_ref,
                                "sourceRepositoryOwnerURI": "https://github.com/Yunushan",
                                "buildConfigURI": workflow_uri,
                                "buildConfigDigest": revision,
                                "buildTrigger": "workflow_dispatch",
                                "runInvocationURI": invocation_uri,
                                "sourceRepositoryVisibilityAtSigning": "public",
                            }
                        },
                        "verifiedTimestamps": [
                            {
                                "type": "Tlog",
                                "timestamp": (current - timedelta(minutes=1)).isoformat().replace(
                                    "+00:00", "Z"
                                ),
                            }
                        ],
                    },
                }
            ]
            if attestation_mutator is not None:
                attestation_mutator(attestation)
            return subprocess.CompletedProcess(command, 0, json.dumps(attestation).encode("utf-8"), b"")
        if command[:2] == ["gh", "api"] and "/jobs?" in command[-1]:
            jobs_payload = {
                "total_count": 1,
                "jobs": [
                    {
                        "name": "Public Polymarket live / GitHub-hosted",
                        "status": "completed",
                        "conclusion": "success",
                        "labels": ["ubuntu-24.04"],
                        "started_at": (current - timedelta(minutes=4)).isoformat().replace("+00:00", "Z"),
                        "completed_at": (current - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
                        "steps": [
                            {"name": name, "status": "completed", "conclusion": "success"}
                            for name in (
                                "Verify exact clean source before probe",
                                "Probe reviewed public Polymarket endpoints",
                                "Revalidate public-only evidence before attestation",
                                "Reverify exact clean source after probe",
                                "Attest exact public-live evidence file",
                                "Upload public-live evidence",
                            )
                        ],
                    }
                ],
                **(jobs_overrides or {}),
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(jobs_payload).encode("utf-8"), b"")
        if command[:2] == ["gh", "api"]:
            payload = {
                "id": 123456,
                "head_sha": revision,
                "name": "CI",
                "path": ".github/workflows/ci.yml",
                "event": "workflow_dispatch",
                "status": "completed",
                "conclusion": "success",
                "run_attempt": 1,
                "head_branch": "main",
                "created_at": (current - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
                "run_started_at": (current - timedelta(minutes=4)).isoformat().replace("+00:00", "Z"),
                "updated_at": (current - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
                "head_repository": {"full_name": "Yunushan/market-sentinel"},
                **(run_overrides or {}),
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(payload).encode("utf-8"), b"")
        raise AssertionError(f"unexpected command: {command}")

    return run


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

    def test_attested_public_live_report_accepts_exact_fresh_github_evidence(self) -> None:
        from scripts.check_product_readiness import _attested_public_live_report

        revision = repository_revision()
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "public-live.json"
            path.write_text(json.dumps(attested_public_live_payload(revision, now=now)), encoding="utf-8")
            with patch(
                "scripts.check_product_readiness.subprocess.run",
                side_effect=successful_public_live_gh_run(revision, now=now),
            ) as run:
                result = _attested_public_live_report(str(path), expected_revision=revision, now=now)

        self.assertEqual(result["status"], "pass", result)
        self.assertEqual(result["evidence"]["run_id"], 123456)
        self.assertEqual(result["evidence"]["github_job"], "verified")
        commands = [call.args[0] for call in run.call_args_list]
        attestation_command = commands[0]
        self.assertEqual(attestation_command[:3], ["gh", "attestation", "verify"])
        self.assertIn("--deny-self-hosted-runners", attestation_command)
        self.assertEqual(attestation_command[attestation_command.index("--digest-alg") + 1], "sha256")
        self.assertEqual(
            attestation_command[attestation_command.index("--predicate-type") + 1],
            "https://slsa.dev/provenance/v1",
        )
        self.assertEqual(
            attestation_command[attestation_command.index("--signer-digest") + 1],
            revision,
        )
        self.assertEqual(
            attestation_command[attestation_command.index("--source-digest") + 1],
            revision,
        )
        self.assertEqual(
            attestation_command[attestation_command.index("--signer-workflow") + 1],
            "Yunushan/market-sentinel/.github/workflows/ci.yml",
        )
        self.assertTrue(any(command[-1].endswith("/actions/runs/123456") for command in commands))
        self.assertTrue(any("/actions/runs/123456/jobs?" in command[-1] for command in commands))

    def test_attested_public_live_report_rejects_forged_or_unsafe_content_before_gh(self) -> None:
        from scripts.check_product_readiness import _attested_public_live_report

        revision = repository_revision()
        now = datetime.now(timezone.utc)
        variants: list[tuple[str, dict[str, object]]] = []
        forged = attested_public_live_payload(revision, now=now)
        forged["evidence"]["source_revision"] = "f" * 40  # type: ignore[index]
        variants.append(("forged revision", forged))
        unsafe = attested_public_live_payload(revision, now=now)
        unsafe["safety"]["credentials_present"] = True  # type: ignore[index]
        variants.append(("credential present", unsafe))
        missing_check = attested_public_live_payload(revision, now=now)
        del missing_check["public_checks"]["clob_time"]  # type: ignore[index]
        variants.append(("missing public check", missing_check))
        injected_ref = attested_public_live_payload(revision, now=now)
        injected_ref["evidence"]["workflow_ref"] = (  # type: ignore[index]
            "Yunushan/market-sentinel/.github/workflows/ci.yml@refs/heads/main'$(touch injected)'"
        )
        variants.append(("shell metacharacters in ref", injected_ref))

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "public-live.json"
            for label, payload in variants:
                with self.subTest(label=label):
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with patch("scripts.check_product_readiness.subprocess.run") as run:
                        result = _attested_public_live_report(str(path), expected_revision=revision, now=now)
                    self.assertEqual(result["status"], "fail")
                    run.assert_not_called()

    def test_attested_public_live_report_rejects_duplicate_keys_and_nan(self) -> None:
        from scripts.check_product_readiness import _attested_public_live_report

        revision = repository_revision()
        malformed_values = (
            '{"ok":true,"ok":true}',
            '{"ok":NaN}',
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "public-live.json"
            for value in malformed_values:
                with self.subTest(value=value):
                    path.write_text(value, encoding="utf-8")
                    with patch("scripts.check_product_readiness.subprocess.run") as run:
                        result = _attested_public_live_report(str(path), expected_revision=revision)
                    self.assertEqual(result["status"], "fail")
                    self.assertIn("malformed", result["detail"])
                    run.assert_not_called()

    def test_attested_public_live_report_fails_closed_when_attestation_fails(self) -> None:
        from scripts.check_product_readiness import _attested_public_live_report

        revision = repository_revision()
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "public-live.json"
            path.write_text(json.dumps(attested_public_live_payload(revision, now=now)), encoding="utf-8")

            def fail_attestation(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
                self.assertEqual(command[:3], ["gh", "attestation", "verify"])
                return subprocess.CompletedProcess(command, 1, b"", b"not trusted")

            with patch("scripts.check_product_readiness.subprocess.run", side_effect=fail_attestation) as run:
                result = _attested_public_live_report(str(path), expected_revision=revision, now=now)

        self.assertEqual(result["status"], "fail")
        self.assertIn("attestation", result["detail"].casefold())
        self.assertEqual(run.call_count, 1)

    def test_attested_public_live_report_rejects_github_run_mismatch(self) -> None:
        from scripts.check_product_readiness import _attested_public_live_report

        revision = repository_revision()
        now = datetime.now(timezone.utc)
        mismatches = (
            {"head_sha": "f" * 40},
            {"event": "push"},
            {"conclusion": "failure"},
            {"run_attempt": 2},
            {"path": ".github/workflows/release.yml"},
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "public-live.json"
            path.write_text(json.dumps(attested_public_live_payload(revision, now=now)), encoding="utf-8")
            for mismatch in mismatches:
                with self.subTest(mismatch=mismatch):
                    with patch(
                        "scripts.check_product_readiness.subprocess.run",
                        side_effect=successful_public_live_gh_run(
                            revision,
                            now=now,
                            run_overrides=mismatch,
                        ),
                    ):
                        result = _attested_public_live_report(str(path), expected_revision=revision, now=now)
                    self.assertEqual(result["status"], "fail")
                    self.assertIn("run identity", result["detail"])

    def test_attested_public_live_report_rejects_paginated_or_inconsistent_jobs(self) -> None:
        from scripts.check_product_readiness import _attested_public_live_report

        revision = repository_revision()
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "public-live.json"
            path.write_text(json.dumps(attested_public_live_payload(revision, now=now)), encoding="utf-8")
            for total_count in (2, 101):
                with self.subTest(total_count=total_count):
                    with patch(
                        "scripts.check_product_readiness.subprocess.run",
                        side_effect=successful_public_live_gh_run(
                            revision,
                            now=now,
                            jobs_overrides={"total_count": total_count},
                        ),
                    ):
                        result = _attested_public_live_report(str(path), expected_revision=revision, now=now)
                    self.assertEqual(result["status"], "fail")
                    self.assertIn("public job", result["detail"])

    def test_attested_public_live_report_rejects_immutable_binding_mutations(self) -> None:
        from scripts.check_product_readiness import _attested_public_live_report

        revision = repository_revision()
        now = datetime.now(timezone.utc)
        mutations: tuple[tuple[str, tuple[str | int, ...], object], ...] = (
            ("subject name", (0, "verificationResult", "statement", "subject", 0, "name"), "other.json"),
            ("subject digest", (0, "verificationResult", "statement", "subject", 0, "digest", "sha256"), "0" * 64),
            ("predicate type", (0, "verificationResult", "statement", "predicateType"), "https://example.invalid"),
            ("certificate SAN", (0, "verificationResult", "signature", "certificate", "subjectAlternativeName"), "https://example.invalid"),
            ("signer digest", (0, "verificationResult", "signature", "certificate", "buildSignerDigest"), "f" * 40),
            ("source ref", (0, "verificationResult", "signature", "certificate", "sourceRepositoryRef"), "refs/heads/other"),
            ("runner", (0, "verificationResult", "signature", "certificate", "runnerEnvironment"), "self-hosted"),
            ("config digest", (0, "verificationResult", "signature", "certificate", "buildConfigDigest"), "f" * 40),
            ("trigger", (0, "verificationResult", "signature", "certificate", "buildTrigger"), "push"),
            ("invocation", (0, "verificationResult", "signature", "certificate", "runInvocationURI"), "https://example.invalid"),
            ("timestamps", (0, "verificationResult", "verifiedTimestamps"), []),
        )

        def set_path(value: list[dict[str, Any]], path: tuple[str | int, ...], replacement: object) -> None:
            target: Any = value
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = replacement

        with tempfile.TemporaryDirectory() as temporary:
            report_path = Path(temporary) / "public-live.json"
            report_path.write_text(
                json.dumps(attested_public_live_payload(revision, now=now)),
                encoding="utf-8",
            )
            for label, mutation_path, replacement in mutations:
                with self.subTest(label=label):
                    def mutate(
                        attestation: list[dict[str, Any]],
                        path: tuple[str | int, ...] = mutation_path,
                        changed: object = replacement,
                    ) -> None:
                        set_path(attestation, path, deepcopy(changed))

                    with patch(
                        "scripts.check_product_readiness.subprocess.run",
                        side_effect=successful_public_live_gh_run(
                            revision,
                            now=now,
                            attestation_mutator=mutate,
                        ),
                    ):
                        result = _attested_public_live_report(
                            str(report_path),
                            expected_revision=revision,
                            now=now,
                        )
                    self.assertEqual(result["status"], "fail")
                    self.assertIn("attestation", result["detail"].casefold())

    def test_direct_public_probe_is_diagnostic_when_attested_evidence_passes(self) -> None:
        from scripts.check_product_readiness import _parser, build_report

        revision = repository_revision()
        args = _parser().parse_args(
            ["--no-run-local", "--run-public-live", "--public-live-report", "attested.json"]
        )
        with (
            patch("scripts.check_product_readiness._repository_is_clean", return_value=True),
            patch("scripts.check_product_readiness._repository_revision", return_value=revision),
            patch("scripts.check_product_readiness._run_public_live", return_value={"status": "pass"}),
            patch(
                "scripts.check_product_readiness._attested_public_live_report",
                return_value={"status": "pass"},
            ),
        ):
            report = build_report(args)

        live = next(item for item in report["categories"] if item["name"] == "live_acceptance")
        self.assertEqual(live["earned"], 3)
        self.assertEqual(report["checks"]["public_live"]["award_source"], "attested")
        self.assertEqual(report["checks"]["public_live"]["diagnostic_status"], "pass")

    def test_attested_public_live_points_are_revoked_when_repository_changes(self) -> None:
        from scripts.check_product_readiness import _parser, build_report

        initial_revision = "a" * 40
        final_revision = "b" * 40
        args = _parser().parse_args(["--no-run-local", "--public-live-report", "attested.json"])
        with (
            patch("scripts.check_product_readiness._repository_is_clean", side_effect=(True, True)),
            patch(
                "scripts.check_product_readiness._repository_revision",
                side_effect=(initial_revision, final_revision),
            ),
            patch(
                "scripts.check_product_readiness._attested_public_live_report",
                return_value={"status": "pass"},
            ),
        ):
            report = build_report(args)

        live = next(item for item in report["categories"] if item["name"] == "live_acceptance")
        self.assertEqual(live["earned"], 0)
        self.assertTrue(any("public Polymarket" in item and "revoked" in item for item in live["missing"]))

    def test_direct_public_live_probe_never_earns_points(self) -> None:
        from scripts.check_product_readiness import _parser, build_report

        revision = "a" * 40
        args = _parser().parse_args(["--no-run-local", "--run-public-live"])
        with (
            patch("scripts.check_product_readiness._repository_is_clean", side_effect=(True, True)),
            patch("scripts.check_product_readiness._repository_revision", return_value=revision),
            patch("scripts.check_product_readiness._run_public_live", return_value={"status": "pass"}),
        ):
            report = build_report(args)

        live = next(item for item in report["categories"] if item["name"] == "live_acceptance")
        self.assertEqual(live["earned"], 0)
        self.assertEqual(report["checks"]["public_live"]["award_source"], "none")
        self.assertEqual(report["checks"]["public_live"]["diagnostic_status"], "pass")

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

    def test_complete_legacy_deployment_wrapper_cannot_award_points(self) -> None:
        from scripts.check_product_readiness import REQUIRED_DEPLOYMENT_CHECKS, _project_version

        revision = "a" * 40
        wrapper = {
            "verified": True,
            "schema_version": 1,
            "evidence_type": "deployment",
            "reviewed_by": "self-asserted-reviewer",
            "reviewed_at": reviewed_at_now(),
            "source": "self-asserted test wrapper",
            "scope": "production-host",
            "environment": "production",
            "expected_version": _project_version(),
            "source_revision": revision,
            "checks": [
                {"name": name, "status": "pass"} for name in REQUIRED_DEPLOYMENT_CHECKS
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "deployment.json"
            path.write_text(json.dumps(wrapper), encoding="utf-8")
            report = score_deployment_evidence(path, revision)

        operations = next(item for item in report["categories"] if item["name"] == "operations_recovery")
        self.assertEqual(operations["earned"], 12)
        self.assertTrue(any("strict semantic review" in item for item in operations["missing"]))

    def test_handwritten_valid_raw_deployment_report_remains_diagnostic_only(self) -> None:
        from scripts.check_product_readiness import _project_version
        from scripts.review_deployment_evidence import review_deployment_report

        revision = "a" * 40
        version = _project_version()
        raw = raw_production_deployment_report(revision, version)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "raw-deployment.json"
            path.write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")
            semantic_review = review_deployment_report(
                path,
                expected_version=version,
                expected_revision=revision,
            )
            report = score_deployment_evidence(path, revision)

        self.assertEqual(semantic_review["status"], "ok")
        operations = next(item for item in report["categories"] if item["name"] == "operations_recovery")
        self.assertEqual(operations["earned"], 12)
        self.assertTrue(any("passed semantic review" in item for item in operations["missing"]))
        self.assertTrue(any("not score-eligible" in item for item in operations["missing"]))

    def test_deployment_reviewer_summary_cannot_award_points(self) -> None:
        from scripts.check_product_readiness import _project_version
        from scripts.review_deployment_evidence import review_deployment_report

        revision = "a" * 40
        version = _project_version()
        raw = raw_production_deployment_report(revision, version)
        with tempfile.TemporaryDirectory() as temporary:
            raw_path = Path(temporary) / "raw-deployment.json"
            summary_path = Path(temporary) / "review-summary.json"
            raw_path.write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")
            summary = review_deployment_report(
                raw_path,
                expected_version=version,
                expected_revision=revision,
            )
            summary_path.write_text(json.dumps(summary, sort_keys=True), encoding="utf-8")
            report = score_deployment_evidence(summary_path, revision)

        self.assertEqual(summary["status"], "ok")
        operations = next(item for item in report["categories"] if item["name"] == "operations_recovery")
        self.assertEqual(operations["earned"], 12)
        self.assertTrue(any("strict semantic review" in item for item in operations["missing"]))

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
        self.assertEqual(platform["earned"], 5)
        self.assertEqual(ci_cd["earned"], 14)
        self.assertTrue(any("diagnostic-only" in item for item in platform["missing"]))
        self.assertTrue(any("diagnostic-only" in item for item in ci_cd["missing"]))

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

    def test_legacy_repository_settings_manifest_is_rejected_after_policy_hardening(self) -> None:
        from scripts.check_product_readiness import _reviewed_evidence

        accepted, detail = _reviewed_evidence(
            str(ROOT / "evidence" / "repository-settings.json"),
            "repository settings",
            evidence_type="repository-settings",
            now=datetime(2026, 8, 26, tzinfo=timezone.utc),
        )

        self.assertFalse(accepted, detail)
        self.assertIn("missing required checks", detail)

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
        self.assertEqual(platform["earned"], 5)
        self.assertTrue(any("diagnostic-only" in item for item in platform["missing"]))

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
        self.assertTrue(any("diagnostic-only" in item for item in platform["missing"]))

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

    def test_manual_release_history_manifest_cannot_award_attested_points(self) -> None:
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
        self.assertTrue(any("Attested release evidence" in item for item in ci_cd["missing"]))

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


    def test_repository_git_probe_scrubs_overrides_and_disables_execution_hooks(self) -> None:
        from scripts.check_product_readiness import ROOT as SCORER_ROOT, _repository_revision

        completed = (
            subprocess.CompletedProcess([], 0, stdout=str(SCORER_ROOT.resolve()) + "\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="a" * 40 + "\n", stderr=""),
        )
        with (
            patch.dict(
                "os.environ",
                {"GIT_DIR": "attacker", "GIT_WORK_TREE": "attacker", "GIT_ASKPASS": "attacker"},
                clear=False,
            ),
            patch("scripts.check_product_readiness.subprocess.run", side_effect=completed) as run,
        ):
            self.assertEqual(_repository_revision(), "a" * 40)

        for call in run.call_args_list:
            environment = call.kwargs["env"]
            self.assertNotIn("GIT_DIR", environment)
            self.assertNotIn("GIT_WORK_TREE", environment)
            self.assertNotIn("GIT_ASKPASS", environment)
            command = call.args[0]
            self.assertIn("core.fsmonitor=false", command)
            self.assertIn("core.hooksPath=", command)

    def test_repository_git_probe_rejects_a_different_top_level(self) -> None:
        from scripts.check_product_readiness import _repository_revision

        forged = subprocess.CompletedProcess([], 0, stdout=str(ROOT.parent) + "\n", stderr="")
        with patch("scripts.check_product_readiness.subprocess.run", return_value=forged):
            self.assertEqual(_repository_revision(), "")


if __name__ == "__main__":
    unittest.main()
