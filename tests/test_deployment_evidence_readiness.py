from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from scripts.check_product_readiness import (
    DEPLOYMENT_COLLECTOR_JOB,
    DEPLOYMENT_EXTERNAL_PROBE_JOB,
    DEPLOYMENT_PREPARE_JOB,
    DEPLOYMENT_REVIEW_JOB,
    REQUIRED_DEPLOYMENT_COLLECTOR_STEPS,
    REQUIRED_DEPLOYMENT_EXTERNAL_PROBE_STEPS,
    REQUIRED_DEPLOYMENT_PREPARE_STEPS,
    REQUIRED_DEPLOYMENT_REVIEW_STEPS,
    _attested_deployment_report,
)


REVISION = "a" * 40
VERSION = "1.0.11"
RUN_ID = 741
NOW = datetime(2026, 8, 26, 12, tzinfo=timezone.utc)
REPOSITORY = "Yunushan/market-sentinel"
ORIGIN = "https://markets.example.net"


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _report() -> dict[str, Any]:
    tag = f"v{VERSION}"
    return {
        "schema_version": 1,
        "report_type": "market-sentinel-deployment-evidence",
        "deployment": {
            "environment": "production",
            "public_origin": ORIGIN,
            "collected_at": _iso(NOW - timedelta(minutes=4)),
            "raw_report_sha256": "b" * 64,
            "workflow_nonce": f"{REVISION}:{RUN_ID}:1",
            "check_count": 31,
            "frontend_sha256": "c" * 64,
            "deployment_provider": "bare-metal",
            "host_identity_sha256": "f" * 64,
            "restore_drill": {
                "completed_at": _iso(NOW - timedelta(minutes=5)),
                "backup_sha256": "a" * 64,
                "restored_file_count": 2,
                "restored_bytes": 8,
            },
            "rollback_drill": {
                "drill_id": "00000000-0000-4000-8000-000000000001",
                "report_sha256": "b" * 64,
                "completed_at": _iso(NOW - timedelta(minutes=30)),
                "rollback_revision": "f" * 40,
                "final_revision": REVISION,
                "step_count": 5,
            },
            "external_probe": {
                "probed_at": _iso(NOW - timedelta(minutes=3)),
                "raw_report_sha256": "e" * 64,
                "runner_environment": "github-hosted",
                "api_version": VERSION,
                "source_revision": REVISION,
                "frontend_sha256": "c" * 64,
                "unauthenticated_probes": 5,
            },
            "release": {
                "id": 81,
                "tag": tag,
                "version": VERSION,
                "target_commit": REVISION,
                "published_at": _iso(NOW - timedelta(hours=2)),
                "html_url": f"https://github.com/{REPOSITORY}/releases/tag/{tag}",
                "asset": {"id": 82, "name": f"market-sentinel-{tag}-frontend-dist.zip", "size": 99, "sha256": "d" * 64},
            },
        },
        "evidence": {
            "repository": REPOSITORY,
            "source_revision": REVISION,
            "run_id": RUN_ID,
            "run_attempt": 1,
            "workflow": ".github/workflows/deployment-evidence.yml",
            "workflow_name": "Production deployment evidence",
            "workflow_ref": f"{REPOSITORY}/.github/workflows/deployment-evidence.yml@refs/heads/main",
            "source_ref": "refs/heads/main",
            "event": "workflow_dispatch",
            "runner_environment": "github-hosted",
            "collector_job": DEPLOYMENT_COLLECTOR_JOB,
            "external_probe_job": DEPLOYMENT_EXTERNAL_PROBE_JOB,
            "review_job": DEPLOYMENT_REVIEW_JOB,
            "collector_labels": ["linux", "market-sentinel-production", "self-hosted", "x64"],
            "artifact_name": f"deployment-evidence-{REVISION}-{RUN_ID}-1",
        },
    }


def _job(name: str, labels: list[str], steps: tuple[str, ...]) -> dict[str, Any]:
    return {
        "name": name, "labels": labels, "head_sha": REVISION, "run_attempt": 1,
        "status": "completed", "conclusion": "success",
        "steps": [{"name": step, "status": "completed", "conclusion": "success"} for step in steps],
    }


def _gh(*, labels: list[str] | None = None, duplicate_artifact: bool = False, stale_artifact: bool = False, failed_step: bool = False):
    def query(command: list[str], **_: object):
        route = command[-1]
        if command[:3] == ["gh", "attestation", "verify"]:
            return [{"trusted": True}], ""
        if route.endswith(f"/actions/runs/{RUN_ID}"):
            return {"id": RUN_ID, "head_sha": REVISION, "head_branch": "main", "event": "workflow_dispatch", "name": "Production deployment evidence", "path": ".github/workflows/deployment-evidence.yml", "status": "completed", "conclusion": "success", "run_attempt": 1, "head_repository": {"full_name": REPOSITORY}, "created_at": _iso(NOW - timedelta(minutes=10)), "run_started_at": _iso(NOW - timedelta(minutes=9)), "updated_at": _iso(NOW - timedelta(minutes=1))}, ""
        if "/jobs?" in route:
            jobs = [
                _job(DEPLOYMENT_PREPARE_JOB, ["ubuntu-24.04"], REQUIRED_DEPLOYMENT_PREPARE_STEPS),
                _job(DEPLOYMENT_COLLECTOR_JOB, labels or ["self-hosted", "linux", "x64", "market-sentinel-production"], REQUIRED_DEPLOYMENT_COLLECTOR_STEPS),
                _job(DEPLOYMENT_EXTERNAL_PROBE_JOB, ["ubuntu-24.04"], REQUIRED_DEPLOYMENT_EXTERNAL_PROBE_STEPS),
                _job(DEPLOYMENT_REVIEW_JOB, ["ubuntu-24.04"], REQUIRED_DEPLOYMENT_REVIEW_STEPS),
            ]
            if failed_step:
                jobs[3]["steps"][2]["conclusion"] = "failure"
            return {"total_count": 4, "jobs": jobs}, ""
        if "/artifacts?" in route:
            item = {"id": 90, "name": f"deployment-evidence-{REVISION}-{RUN_ID}-1", "expired": False, "created_at": _iso(NOW - timedelta(days=2) if stale_artifact else NOW - timedelta(minutes=2)), "updated_at": _iso(NOW - timedelta(minutes=1)), "workflow_run": {"id": RUN_ID, "head_sha": REVISION}}
            return {"total_count": 2 if duplicate_artifact else 1, "artifacts": [item, dict(item)] if duplicate_artifact else [item]}, ""
        if "/releases/tags/" in route:
            tag = f"v{VERSION}"
            return {"id": 81, "tag_name": tag, "target_commitish": REVISION, "draft": False, "prerelease": False, "published_at": _iso(NOW - timedelta(hours=2)), "html_url": f"https://github.com/{REPOSITORY}/releases/tag/{tag}", "assets": [{"id": 82, "name": f"market-sentinel-{tag}-frontend-dist.zip", "state": "uploaded", "size": 99, "digest": f"sha256:{'d' * 64}"}]}, ""
        if route.endswith("/branches/main"):
            return {"protected": True, "commit": {"sha": "e" * 40}}, ""
        if "/compare/" in route:
            return {"status": "ahead", "base_commit": {"sha": REVISION}, "merge_base_commit": {"sha": REVISION}}, ""
        raise AssertionError(command)
    return query


class DeploymentEvidenceReadinessTests(unittest.TestCase):
    def _validate(self, report: dict[str, Any], *, attestation: bool = True, **gh_options: object) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "deployment-evidence.json")
            path.write_bytes((json.dumps(report, indent=2, sort_keys=True) + "\n").encode())
            with patch("scripts.check_product_readiness._run_gh_json", side_effect=_gh(**gh_options)), patch("scripts.check_product_readiness._attestation_result_matches", return_value=attestation), patch("scripts.check_product_readiness._resolve_release_tag_commit", return_value=(REVISION, "")):
                return _attested_deployment_report(str(path), expected_revision=REVISION, expected_version=VERSION, expected_origin=ORIGIN, now=NOW)

    def test_accepts_exact_attested_production_deployment(self) -> None:
        self.assertEqual(self._validate(_report())["status"], "pass")

    def test_rejects_origin_mutation(self) -> None:
        report = _report(); report["deployment"]["public_origin"] = "https://other.example.net"
        self.assertEqual(self._validate(report)["status"], "fail")

    def test_rejects_collector_label_mutation(self) -> None:
        self.assertEqual(self._validate(_report(), labels=["self-hosted", "linux", "x64"])["status"], "fail")

    def test_rejects_duplicate_or_stale_artifact(self) -> None:
        self.assertEqual(self._validate(_report(), duplicate_artifact=True)["status"], "fail")
        self.assertEqual(self._validate(_report(), stale_artifact=True)["status"], "fail")

    def test_rejects_frontend_release_asset_mutation(self) -> None:
        report = _report(); report["deployment"]["release"]["asset"]["sha256"] = "e" * 64
        self.assertEqual(self._validate(report)["status"], "fail")

    def test_rejects_missing_attestation_or_failed_review_step(self) -> None:
        self.assertEqual(self._validate(_report(), attestation=False)["status"], "fail")
        self.assertEqual(self._validate(_report(), failed_step=True)["status"], "fail")


if __name__ == "__main__":
    unittest.main()
