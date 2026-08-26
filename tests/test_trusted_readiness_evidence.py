from __future__ import annotations

import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from scripts.trusted_readiness_evidence import (
    CREDENTIALED_POLYMARKET_CHECKS,
    FUNDED_POLYMARKET_CHECKS,
    PLATFORM_CHECKS,
    PLATFORM_CI_CHECKS,
    RELEASE_ENVIRONMENT_CHECKS,
    REPOSITORY,
    REPOSITORY_SETTINGS_CHECKS,
    TRUSTED_REF,
    WORKFLOW_CONTRACTS,
    TrustedEvidenceError,
    _required_job_names,
    build_governance_manifests,
    build_live_manifest,
    build_platform_manifests,
    canonical_json_bytes,
    main as trusted_evidence_main,
    validate_manifest,
    write_manifest,
)


ROOT = Path(__file__).resolve().parent.parent
REVISION = "a" * 40
SOURCE_RUN_ID = 600
EVIDENCE_RUN_ID = 700
RUN_ATTEMPT = 1
ORDER_ID = "0x" + "1" * 64


def governance_payload() -> dict[str, object]:
    checks = [
        {"name": name, "status": "pass", "detail": "live API control verified"}
        for name in (*REPOSITORY_SETTINGS_CHECKS, *RELEASE_ENVIRONMENT_CHECKS)
    ]
    return {"repository": REPOSITORY, "branch": "main", "status": "ok", "checks": checks}


def platform_api_payloads(*, now: datetime | None = None) -> tuple[dict[str, object], dict[str, object]]:
    current = now or datetime.now(timezone.utc)
    run = {
        "id": SOURCE_RUN_ID,
        "head_sha": REVISION,
        "name": "CI",
        "path": ".github/workflows/ci.yml",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "run_attempt": 1,
        "head_branch": "main",
        "head_repository": {"full_name": REPOSITORY},
        "created_at": (current - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "run_started_at": (current - timedelta(minutes=59)).isoformat().replace("+00:00", "Z"),
        "updated_at": (current - timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
    }
    names = tuple(dict.fromkeys(name for group in _required_job_names().values() for name in group))
    jobs = {
        "total_count": len(names),
        "jobs": [
            {
                "name": name,
                "status": "completed",
                "conclusion": "success",
                "labels": ["ubuntu-24.04"],
            }
            for name in names
        ],
    }
    return run, jobs


def clean_source_provenance(revision: str = REVISION) -> dict[str, object]:
    return {
        "schema_version": 1,
        "repository": "market-sentinel",
        "repository_origin": "github.com/yunushan/market-sentinel",
        "source_revision": revision,
        "initial_revision": revision,
        "final_revision": revision,
        "initial_clean": True,
        "final_clean": True,
        "stable": True,
    }


def public_checks() -> dict[str, dict[str, object]]:
    return {
        "clob_time": {"status": "ok", "semantic_check": "current_unix_time"},
        "gamma_markets": {"status": "ok", "semantic_check": "market_identity"},
        "data_leaderboard": {"status": "ok", "semantic_check": "leaderboard_identity"},
        "bridge_supported_assets": {"status": "ok", "semantic_check": "supported_asset_identity"},
    }


def authenticated_reads() -> dict[str, dict[str, object]]:
    return {
        "clob_l2_orders": {
            "status": "ok",
            "detail": "Authenticated CLOB order list responded.",
            "sample_type": "list",
            "semantic_check": "authenticated_order_collection",
            "records_observed": 0,
        }
    }


def credentialed_report() -> dict[str, object]:
    return {
        "ok": True,
        "generated_at": 1.0,
        "mode": "strict_cli",
        "market_id": "polymarket",
        "source_provenance": clean_source_provenance(),
        "public_checks": public_checks(),
        "authenticated_read_checks": authenticated_reads(),
        "funded_live_order_check": {"status": "blocked", "live_action": False},
        "stage_gates": {
            "credentialed_read_ok": True,
            "safe_to_attempt_funded_order": False,
            "requires_explicit_live_approval": True,
        },
    }


def funded_report() -> dict[str, object]:
    report = credentialed_report()
    report["funded_live_order_check"] = {
        "status": "ok",
        "live_action": True,
        "manual_reconciliation_required": False,
        "account_authenticated_read_preflight": {
            "status": "pass",
            "same_trading_client": True,
            "account_identity_present": True,
            "sample_type": "list",
            "records_observed": 0,
        },
        "account_preflight": {
            "status": "pass",
            "sufficient_balance": True,
            "sufficient_allowance": True,
        },
        "execution_guards": {
            "status": "pass",
            "post_only": True,
            "time_in_force": "GTC",
            "maker_price_verified": True,
        },
        "geoblock_preflight": {"status": "pass", "blocked": False},
        "source_revision_gate": {
            "status": "pass",
            "clean": True,
            "matches_initial_revision": True,
            "source_revision": REVISION,
            "repository_origin": "github.com/yunushan/market-sentinel",
        },
        "audit": {
            "order_id": ORDER_ID,
            "placed": {"orderID": ORDER_ID, "status": "live"},
            "cancel": {"canceled": [ORDER_ID]},
            "post_cancel_order": {
                "id": ORDER_ID,
                "status": "ORDER_STATUS_CANCELED",
                "size_matched": "0",
                "associate_trades": [],
            },
            "zero_fill_evidence": {
                "verified": True,
                "order_identity_matches": True,
                "size_matched_zero": True,
                "associated_trades_empty": True,
            },
            "recovery_journal": {"status": "resolved", "stage": "cancel_verified", "resolved": True},
            "post_cancel_verified": True,
        },
    }
    report["stage_gates"] = {
        "credentialed_read_ok": True,
        "safe_to_attempt_funded_order": True,
        "requires_explicit_live_approval": True,
        "funded_live_order_check": "ok",
    }
    return report


def workflow_ref(evidence_type: str) -> str:
    return f"{REPOSITORY}/{WORKFLOW_CONTRACTS[evidence_type]['workflow']}@{TRUSTED_REF}"


def all_manifests(now: datetime) -> tuple[dict[str, dict[str, object]], dict[str, object], dict[str, object]]:
    generated = now - timedelta(minutes=2)
    governance = build_governance_manifests(
        governance_payload(),
        repository=REPOSITORY,
        source_revision=REVISION,
        run_id=EVIDENCE_RUN_ID,
        run_attempt=RUN_ATTEMPT,
        workflow_ref=workflow_ref("repository-settings"),
        generated_at=generated,
    )
    source_run, source_jobs = platform_api_payloads(now=now)
    platform = build_platform_manifests(
        source_run,
        source_jobs,
        repository=REPOSITORY,
        source_revision=REVISION,
        source_run_id=SOURCE_RUN_ID,
        run_id=EVIDENCE_RUN_ID,
        run_attempt=RUN_ATTEMPT,
        workflow_ref=workflow_ref("platform-ci"),
        generated_at=generated,
    )
    credentialed = build_live_manifest(
        credentialed_report(),
        tier="credentialed",
        repository=REPOSITORY,
        source_revision=REVISION,
        run_id=EVIDENCE_RUN_ID,
        run_attempt=RUN_ATTEMPT,
        workflow_ref=workflow_ref("credentialed-polymarket"),
        generated_at=generated,
    )
    with patch("polymarket.live_reports.POLYMARKET_LIVE_MUTATIONS_SUPPORTED", True):
        funded = build_live_manifest(
            funded_report(),
            tier="funded",
            repository=REPOSITORY,
            source_revision=REVISION,
            run_id=EVIDENCE_RUN_ID,
            run_attempt=RUN_ATTEMPT,
            workflow_ref=workflow_ref("funded-polymarket"),
            generated_at=generated,
        )
    return {**governance, **platform}, credentialed, funded


def hosted_api_side_effect(
    evidence_type: str,
    manifest: dict[str, object],
    *,
    now: datetime,
):
    contract = WORKFLOW_CONTRACTS[evidence_type]
    source_run, source_jobs = platform_api_payloads(now=now)

    def query(command: list[str], *, timeout: int = 30):
        del timeout
        if command[:3] == ["gh", "attestation", "verify"]:
            return [{"attestation": "checked by separately tested matcher"}], ""
        endpoint = command[-1]
        if endpoint.endswith(f"/actions/runs/{EVIDENCE_RUN_ID}"):
            return {
                "id": EVIDENCE_RUN_ID,
                "head_sha": REVISION,
                "name": contract["workflow_name"],
                "path": contract["workflow"],
                "event": "workflow_dispatch",
                "status": "completed",
                "conclusion": "success",
                "run_attempt": RUN_ATTEMPT,
                "head_branch": "main",
                "head_repository": {"full_name": REPOSITORY},
                "created_at": (now - timedelta(minutes=6)).isoformat().replace("+00:00", "Z"),
                "run_started_at": (now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
                "updated_at": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
            }, ""
        if endpoint.endswith(f"/actions/runs/{EVIDENCE_RUN_ID}/jobs?filter=latest&per_page=100"):
            return {
                "total_count": 1,
                "jobs": [
                    {
                        "name": contract["job"],
                        "status": "completed",
                        "conclusion": "success",
                        "labels": ["ubuntu-24.04"],
                        "started_at": (now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
                        "completed_at": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
                        "steps": [
                            {"name": name, "status": "completed", "conclusion": "success"}
                            for name in contract["required_steps"]
                        ],
                    }
                ],
            }, ""
        if endpoint.endswith(f"/actions/runs/{EVIDENCE_RUN_ID}/artifacts?per_page=100"):
            evidence = manifest["evidence"]
            assert isinstance(evidence, dict)
            return {
                "total_count": 1,
                "artifacts": [
                    {
                        "id": 800,
                        "name": evidence["artifact_name"],
                        "size_in_bytes": len(canonical_json_bytes(manifest)),
                        "expired": False,
                        "created_at": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
                        "updated_at": (now - timedelta(seconds=30)).isoformat().replace("+00:00", "Z"),
                        "workflow_run": {"id": EVIDENCE_RUN_ID, "head_sha": REVISION},
                    }
                ],
            }, ""
        if endpoint.endswith(f"/actions/runs/{SOURCE_RUN_ID}"):
            return source_run, ""
        if endpoint.endswith(f"/actions/runs/{SOURCE_RUN_ID}/jobs?filter=latest&per_page=100"):
            return source_jobs, ""
        raise AssertionError(f"unexpected GitHub query: {command}")

    return query


class TrustedReadinessEvidenceTests(unittest.TestCase):
    def test_generator_scorer_and_live_collectors_share_exact_check_contracts(self) -> None:
        from scripts.check_product_readiness import (
            REQUIRED_CREDENTIALED_POLYMARKET_CHECKS,
            REQUIRED_FUNDED_POLYMARKET_CHECKS,
            REQUIRED_PLATFORM_CHECKS,
            REQUIRED_PLATFORM_CI_CHECKS,
            REQUIRED_RELEASE_ENVIRONMENT_CHECKS,
            REQUIRED_REPOSITORY_SETTINGS_CHECKS,
        )
        from scripts.verify_repository_settings import (
            REQUIRED_RELEASE_ENVIRONMENT_CHECKS as COLLECTOR_RELEASE_CHECKS,
        )

        self.assertEqual(REQUIRED_REPOSITORY_SETTINGS_CHECKS, REPOSITORY_SETTINGS_CHECKS)
        self.assertEqual(REQUIRED_RELEASE_ENVIRONMENT_CHECKS, RELEASE_ENVIRONMENT_CHECKS)
        self.assertEqual(COLLECTOR_RELEASE_CHECKS, RELEASE_ENVIRONMENT_CHECKS)
        self.assertEqual(REQUIRED_PLATFORM_CI_CHECKS, PLATFORM_CI_CHECKS)
        self.assertEqual(tuple(_required_job_names()), PLATFORM_CI_CHECKS)
        self.assertEqual(REQUIRED_PLATFORM_CHECKS, PLATFORM_CHECKS)
        self.assertEqual(REQUIRED_CREDENTIALED_POLYMARKET_CHECKS, CREDENTIALED_POLYMARKET_CHECKS)
        self.assertEqual(REQUIRED_FUNDED_POLYMARKET_CHECKS, FUNDED_POLYMARKET_CHECKS)

    def test_governance_builder_requires_every_live_control(self) -> None:
        now = datetime.now(timezone.utc)
        manifests = build_governance_manifests(
            governance_payload(),
            repository=REPOSITORY,
            source_revision=REVISION,
            run_id=EVIDENCE_RUN_ID,
            run_attempt=RUN_ATTEMPT,
            workflow_ref=workflow_ref("repository-settings"),
            generated_at=now,
        )
        self.assertEqual(set(manifests), {"repository-settings", "release-environment"})
        for evidence_type, manifest in manifests.items():
            validation = validate_manifest(
                manifest,
                expected_evidence_type=evidence_type,
                expected_revision=REVISION,
                now=now,
            )
            self.assertTrue(validation["ok"], validation["errors"])

        incomplete = governance_payload()
        incomplete["checks"] = list(incomplete["checks"])[:-1]  # type: ignore[arg-type]
        with self.assertRaisesRegex(TrustedEvidenceError, "missing required checks"):
            build_governance_manifests(
                incomplete,
                repository=REPOSITORY,
                source_revision=REVISION,
                run_id=EVIDENCE_RUN_ID,
                run_attempt=RUN_ATTEMPT,
                workflow_ref=workflow_ref("repository-settings"),
            )

    def test_governance_cli_generates_canonical_files_that_validate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            raw = directory / "raw.json"
            output = directory / "out"
            output.mkdir()
            raw.write_text(json.dumps(governance_payload()), encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                generated = trusted_evidence_main(
                    [
                        "governance",
                        "--input",
                        str(raw),
                        "--repository",
                        REPOSITORY,
                        "--source-revision",
                        REVISION,
                        "--run-id",
                        str(EVIDENCE_RUN_ID),
                        "--run-attempt",
                        str(RUN_ATTEMPT),
                        "--workflow-ref",
                        workflow_ref("repository-settings"),
                        "--output-directory",
                        str(output),
                    ]
                )
            self.assertEqual(generated, 0)
            for evidence_type in ("repository-settings", "release-environment"):
                path = output / str(WORKFLOW_CONTRACTS[evidence_type]["subject_name"])
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(path.read_bytes(), canonical_json_bytes(payload))
                with redirect_stdout(io.StringIO()):
                    validated = trusted_evidence_main(
                        [
                            "validate",
                            "--input",
                            str(path),
                            "--evidence-type",
                            evidence_type,
                            "--expected-revision",
                            REVISION,
                        ]
                    )
                self.assertEqual(validated, 0)

    def test_platform_builder_rejects_missing_or_self_hosted_required_job(self) -> None:
        now = datetime.now(timezone.utc)
        source_run, source_jobs = platform_api_payloads(now=now)
        manifests = build_platform_manifests(
            source_run,
            source_jobs,
            repository=REPOSITORY,
            source_revision=REVISION,
            source_run_id=SOURCE_RUN_ID,
            run_id=EVIDENCE_RUN_ID,
            run_attempt=RUN_ATTEMPT,
            workflow_ref=workflow_ref("platform-ci"),
            generated_at=now,
        )
        self.assertEqual(set(manifests), {"platform-ci", "platform"})
        for evidence_type, manifest in manifests.items():
            self.assertTrue(
                validate_manifest(
                    manifest,
                    expected_evidence_type=evidence_type,
                    expected_revision=REVISION,
                    now=now,
                )["ok"]
            )

        for mutation in ("missing", "self-hosted"):
            with self.subTest(mutation=mutation):
                rejected_jobs = deepcopy(source_jobs)
                jobs = rejected_jobs["jobs"]
                assert isinstance(jobs, list)
                if mutation == "missing":
                    jobs.pop()
                    rejected_jobs["total_count"] = len(jobs)
                else:
                    assert isinstance(jobs[0], dict)
                    jobs[0]["labels"] = ["self-hosted"]
                with self.assertRaises(TrustedEvidenceError):
                    build_platform_manifests(
                        source_run,
                        rejected_jobs,
                        repository=REPOSITORY,
                        source_revision=REVISION,
                        source_run_id=SOURCE_RUN_ID,
                        run_id=EVIDENCE_RUN_ID,
                        run_attempt=RUN_ATTEMPT,
                        workflow_ref=workflow_ref("platform-ci"),
                    )

    def test_live_builder_recomputes_credentialed_and_funded_promotion(self) -> None:
        now = datetime.now(timezone.utc)
        credentialed = build_live_manifest(
            credentialed_report(),
            tier="credentialed",
            repository=REPOSITORY,
            source_revision=REVISION,
            run_id=EVIDENCE_RUN_ID,
            run_attempt=RUN_ATTEMPT,
            workflow_ref=workflow_ref("credentialed-polymarket"),
            generated_at=now,
        )
        self.assertTrue(
            validate_manifest(
                credentialed,
                expected_evidence_type="credentialed-polymarket",
                expected_revision=REVISION,
                now=now,
            )["ok"]
        )

        with patch("polymarket.live_reports.POLYMARKET_LIVE_MUTATIONS_SUPPORTED", True):
            funded = build_live_manifest(
                funded_report(),
                tier="funded",
                repository=REPOSITORY,
                source_revision=REVISION,
                run_id=EVIDENCE_RUN_ID,
                run_attempt=RUN_ATTEMPT,
                workflow_ref=workflow_ref("funded-polymarket"),
                generated_at=now,
            )
            self.assertTrue(
                validate_manifest(
                    funded,
                    expected_evidence_type="funded-polymarket",
                    expected_revision=REVISION,
                    now=now,
                )["ok"]
            )

        tampered = deepcopy(credentialed)
        tampered["live_report"]["authenticated_read_checks"] = {}  # type: ignore[index]
        validation = validate_manifest(
            tampered,
            expected_evidence_type="credentialed-polymarket",
            expected_revision=REVISION,
            now=now,
        )
        self.assertFalse(validation["ok"])

    def test_every_formerly_diagnostic_type_can_pass_exact_hosted_verification(self) -> None:
        from scripts.check_product_readiness import _attested_trusted_evidence

        now = datetime.now(timezone.utc)
        non_live, credentialed, funded = all_manifests(now)
        manifests = {**non_live, "credentialed-polymarket": credentialed, "funded-polymarket": funded}
        with tempfile.TemporaryDirectory() as temporary:
            for evidence_type, manifest in manifests.items():
                with self.subTest(evidence_type=evidence_type):
                    path = Path(temporary) / str(WORKFLOW_CONTRACTS[evidence_type]["subject_name"])
                    write_manifest(path, manifest)
                    api = hosted_api_side_effect(evidence_type, manifest, now=now)
                    patches = [
                        patch("scripts.check_product_readiness._run_gh_json", side_effect=api),
                        patch("scripts.check_product_readiness._attestation_result_matches", return_value=True),
                    ]
                    if evidence_type == "funded-polymarket":
                        patches.append(patch("polymarket.live_reports.POLYMARKET_LIVE_MUTATIONS_SUPPORTED", True))
                    with patches[0], patches[1]:
                        if len(patches) == 3:
                            with patches[2]:
                                result = _attested_trusted_evidence(
                                    str(path),
                                    evidence_type,
                                    evidence_type=evidence_type,
                                    expected_revision=REVISION,
                                    now=now,
                                )
                        else:
                            result = _attested_trusted_evidence(
                                str(path),
                                evidence_type,
                                evidence_type=evidence_type,
                                expected_revision=REVISION,
                                now=now,
                            )
                    self.assertEqual(result["status"], "pass", result)

    def test_manual_manifest_and_tampered_attested_manifest_remain_fail_closed(self) -> None:
        from scripts.check_product_readiness import _attested_trusted_evidence

        now = datetime.now(timezone.utc)
        manual = {
            "schema_version": 1,
            "verified": True,
            "evidence_type": "repository-settings",
            "reviewed_by": "self",
            "reviewed_at": now.isoformat(),
            "source": "manual",
            "checks": [{"name": name, "status": "pass"} for name in REPOSITORY_SETTINGS_CHECKS],
        }
        manifests = build_governance_manifests(
            governance_payload(),
            repository=REPOSITORY,
            source_revision=REVISION,
            run_id=EVIDENCE_RUN_ID,
            run_attempt=RUN_ATTEMPT,
            workflow_ref=workflow_ref("repository-settings"),
            generated_at=now,
        )
        tampered = deepcopy(manifests["repository-settings"])
        tampered["checks"][0]["status"] = "fail"  # type: ignore[index]

        with tempfile.TemporaryDirectory() as temporary:
            manual_path = Path(temporary) / "manual.json"
            manual_path.write_text(json.dumps(manual), encoding="utf-8")
            manual_result = _attested_trusted_evidence(
                str(manual_path),
                "repository settings",
                evidence_type="repository-settings",
                expected_revision=REVISION,
                now=now,
            )
            self.assertEqual(manual_result["status"], "diagnostic")

            tampered_path = Path(temporary) / "tampered.json"
            write_manifest(tampered_path, tampered)
            tampered_result = _attested_trusted_evidence(
                str(tampered_path),
                "repository settings",
                evidence_type="repository-settings",
                expected_revision=REVISION,
                now=now,
            )
            self.assertEqual(tampered_result["status"], "fail")
            self.assertIn("semantic contract", tampered_result["detail"])

    def test_attested_evidence_rejects_absent_stale_cross_revision_and_replayed_artifacts(self) -> None:
        from scripts.check_product_readiness import _attested_trusted_evidence

        now = datetime.now(timezone.utc)
        missing = _attested_trusted_evidence(
            None,
            "repository settings",
            evidence_type="repository-settings",
            expected_revision=REVISION,
            now=now,
        )
        self.assertEqual(missing["status"], "not_run")

        stale = build_governance_manifests(
            governance_payload(),
            repository=REPOSITORY,
            source_revision=REVISION,
            run_id=EVIDENCE_RUN_ID,
            run_attempt=RUN_ATTEMPT,
            workflow_ref=workflow_ref("repository-settings"),
            generated_at=now - timedelta(hours=25),
        )["repository-settings"]
        current = build_governance_manifests(
            governance_payload(),
            repository=REPOSITORY,
            source_revision=REVISION,
            run_id=EVIDENCE_RUN_ID,
            run_attempt=RUN_ATTEMPT,
            workflow_ref=workflow_ref("repository-settings"),
            generated_at=now - timedelta(minutes=2),
        )["repository-settings"]

        with tempfile.TemporaryDirectory() as temporary:
            stale_path = Path(temporary) / "stale.json"
            write_manifest(stale_path, stale)
            stale_result = _attested_trusted_evidence(
                str(stale_path),
                "repository settings",
                evidence_type="repository-settings",
                expected_revision=REVISION,
                now=now,
            )
            self.assertEqual(stale_result["status"], "fail")
            self.assertIn("older than 24 hours", stale_result["detail"])

            current_path = Path(temporary) / "current.json"
            write_manifest(current_path, current)
            cross_revision = _attested_trusted_evidence(
                str(current_path),
                "repository settings",
                evidence_type="repository-settings",
                expected_revision="b" * 40,
                now=now,
            )
            self.assertEqual(cross_revision["status"], "fail")
            self.assertIn("semantic contract", cross_revision["detail"])

            normal_api = hosted_api_side_effect("repository-settings", current, now=now)

            def replayed_artifact(command: list[str], *, timeout: int = 30):
                payload, error = normal_api(command, timeout=timeout)
                if command[-1].endswith(f"/actions/runs/{EVIDENCE_RUN_ID}/artifacts?per_page=100"):
                    assert isinstance(payload, dict)
                    payload = deepcopy(payload)
                    artifacts = payload["artifacts"]
                    assert isinstance(artifacts, list)
                    artifacts.append(deepcopy(artifacts[0]))
                    payload["total_count"] = 2
                return payload, error

            with (
                patch("scripts.check_product_readiness._run_gh_json", side_effect=replayed_artifact),
                patch("scripts.check_product_readiness._attestation_result_matches", return_value=True),
            ):
                replayed = _attested_trusted_evidence(
                    str(current_path),
                    "repository settings",
                    evidence_type="repository-settings",
                    expected_revision=REVISION,
                    now=now,
                )
            self.assertEqual(replayed["status"], "fail")
            self.assertIn("exactly one distinct evidence artifact", replayed["detail"])

    def test_hosted_job_must_complete_every_review_and_attestation_step(self) -> None:
        from scripts.check_product_readiness import _attested_trusted_evidence

        now = datetime.now(timezone.utc)
        manifest = build_governance_manifests(
            governance_payload(),
            repository=REPOSITORY,
            source_revision=REVISION,
            run_id=EVIDENCE_RUN_ID,
            run_attempt=RUN_ATTEMPT,
            workflow_ref=workflow_ref("repository-settings"),
            generated_at=now - timedelta(minutes=2),
        )["repository-settings"]
        normal_api = hosted_api_side_effect("repository-settings", manifest, now=now)

        def missing_review_step(command: list[str], *, timeout: int = 30):
            payload, error = normal_api(command, timeout=timeout)
            if command[-1].endswith(f"/actions/runs/{EVIDENCE_RUN_ID}/jobs?filter=latest&per_page=100"):
                assert isinstance(payload, dict)
                payload = deepcopy(payload)
                jobs = payload["jobs"]
                assert isinstance(jobs, list) and isinstance(jobs[0], dict)
                jobs[0]["steps"] = list(jobs[0]["steps"])[:-1]
            return payload, error

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "repository-settings-evidence.json"
            write_manifest(path, manifest)
            with (
                patch("scripts.check_product_readiness._run_gh_json", side_effect=missing_review_step),
                patch("scripts.check_product_readiness._attestation_result_matches", return_value=True),
            ):
                result = _attested_trusted_evidence(
                    str(path),
                    "repository settings",
                    evidence_type="repository-settings",
                    expected_revision=REVISION,
                    now=now,
                )
        self.assertEqual(result["status"], "fail")
        self.assertIn("every required collection, review, and attestation step", result["detail"])

    def test_scorer_has_no_formal_nine_point_ceiling_when_all_evidence_passes(self) -> None:
        from scripts.check_product_readiness import _parser, build_report

        args = _parser().parse_args(
            [
                "--full-local",
                "--run-public-live",
                "--repository-settings-evidence",
                "repository.json",
                "--release-environment-evidence",
                "release-environment.json",
                "--release-history-evidence",
                "release.json",
                "--release-evidence",
                "release.json",
                "--deployment-evidence",
                "deployment.json",
                "--deployment-origin",
                "https://markets.example.net",
                "--platform-ci-evidence",
                "platform-ci.json",
                "--platform-evidence",
                "platform.json",
                "--credentialed-evidence",
                "credentialed.json",
                "--funded-evidence",
                "funded.json",
            ]
        )
        attested = {"status": "pass", "detail": "attested"}
        with (
            patch("scripts.check_product_readiness._repository_is_clean", return_value=True),
            patch("scripts.check_product_readiness._repository_revision", return_value=REVISION),
            patch("scripts.check_product_readiness._run_local_gates", return_value={"status": "pass"}),
            patch("scripts.check_product_readiness._run_public_live", return_value={"status": "pass"}),
            patch("scripts.check_product_readiness._attested_public_live_report", return_value=attested),
            patch("scripts.check_product_readiness._attested_trusted_evidence", return_value=attested),
            patch("scripts.check_product_readiness._attested_release_report", return_value=attested),
            patch(
                "scripts.check_product_readiness._deployment_evidence",
                return_value=(True, "attested", attested),
            ),
            patch(
                "scripts.check_product_readiness._live_evidence",
                return_value=(True, "attested", attested),
            ),
        ):
            report = build_report(args)

        self.assertEqual(report["score"], 100)
        self.assertEqual(report["status"], "ready")
        self.assertTrue(all(category["earned"] == category["possible"] for category in report["categories"]))

    def test_evidence_workflows_are_manual_main_only_and_fail_closed(self) -> None:
        paths = (
            ROOT / ".github" / "workflows" / "governance-evidence.yml",
            ROOT / ".github" / "workflows" / "platform-evidence.yml",
            ROOT / ".github" / "workflows" / "polymarket-evidence.yml",
        )
        for path in paths:
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn("workflow_dispatch:", text)
                self.assertIn("github.ref == 'refs/heads/main'", text)
                self.assertNotIn("pull_request:", text)
                self.assertNotIn("\n  push:", text)
                self.assertIn("persist-credentials: false", text)
                self.assertIn("actions/attest-build-provenance@4d101475", text)
                self.assertIn("actions/upload-artifact@043fb46", text)
        funded = paths[-1].read_text(encoding="utf-8")
        self.assertIn("I_UNDERSTAND_THIS_PLACES_A_REAL_POLYMARKET_ORDER", funded)
        self.assertIn("--cancel-immediately", funded)
        self.assertIn("--recovery-journal", funded)
        self.assertIn("environment: production", funded)
        self.assertEqual(funded.count("requirements-live.lock"), 2)
        self.assertNotIn("-r requirements.lock", funded)


if __name__ == "__main__":
    unittest.main()
