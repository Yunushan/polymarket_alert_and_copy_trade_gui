from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.generate_deployment_evidence import (
    DeploymentEvidenceGenerationError,
    generate_evidence,
)


REVISION = "a" * 40
ORIGIN = "https://markets.example.net"


class DeploymentEvidenceGenerationTests(unittest.TestCase):
    def test_binds_reviewed_raw_bytes_to_exact_origin_and_release_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw.json"
            external = root / "external.json"
            identity = root / "identity.json"
            raw.write_text(json.dumps({"collection": {"public_origin": ORIGIN, "run_id": 7, "run_attempt": 1, "nonce": f"{REVISION}:7:1"}}), encoding="utf-8")
            identity.write_text(json.dumps({
                "identity_type": "market-sentinel-deployment-release-identity",
                "repository": "Yunushan/market-sentinel",
                "frontend_sha256": "b" * 64,
                "release": {"target_commit": REVISION, "version": "1.0.11"},
            }), encoding="utf-8")
            external.write_text("{}", encoding="utf-8")
            review = {
                "frontend_sha256": "b" * 64,
                "collected_at": "2026-08-26T11:00:00Z",
                "raw_report_sha256": "c" * 64,
                "check_count": 34,
                "deployment_provider": "bare-metal",
                "host_identity_sha256": "d" * 64,
                "restore_drill": {"completed_at": "2026-08-26T11:01:00Z", "backup_sha256": "e" * 64, "restored_file_count": 2, "restored_bytes": 5},
                "rollback_drill": {"drill_id": "00000000-0000-4000-8000-000000000001", "report_sha256": "a" * 64, "completed_at": "2026-08-26T10:00:00Z", "rollback_revision": "f" * 40, "final_revision": REVISION, "step_count": 5},
            }
            external_review = {"probed_at": "2026-08-26T11:02:00Z", "report_sha256": "f" * 64, "api_version": "1.0.11", "source_revision": REVISION, "frontend_sha256": "b" * 64, "unauthenticated_probes": 5}
            with (
                patch("scripts.generate_deployment_evidence.review_deployment_report", return_value=review),
                patch("scripts.generate_deployment_evidence.review_external_probe_report", return_value=external_review),
            ):
                report = generate_evidence(raw, external, identity, public_origin=ORIGIN, run_id=7, run_attempt=1, workflow_ref="Yunushan/market-sentinel/.github/workflows/deployment-evidence.yml@refs/heads/main", source_ref="refs/heads/main", artifact_name=f"deployment-evidence-{REVISION}-7-1")
            self.assertEqual(report["deployment"]["public_origin"], ORIGIN)
            self.assertEqual(report["deployment"]["frontend_sha256"], "b" * 64)
            self.assertEqual(report["deployment"]["external_probe"]["runner_environment"], "github-hosted")

    def test_rejects_origin_or_artifact_name_mutation_before_attestation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw.json"
            external = root / "external.json"
            identity = root / "identity.json"
            raw.write_text(json.dumps({"collection": {"public_origin": "https://other.example.net", "run_id": 7, "run_attempt": 1, "nonce": f"{REVISION}:7:1"}}), encoding="utf-8")
            identity.write_text(json.dumps({"identity_type": "market-sentinel-deployment-release-identity", "repository": "Yunushan/market-sentinel", "frontend_sha256": "b" * 64, "release": {"target_commit": REVISION, "version": "1.0.11"}}), encoding="utf-8")
            with self.assertRaises(DeploymentEvidenceGenerationError):
                generate_evidence(raw, external, identity, public_origin=ORIGIN, run_id=7, run_attempt=1, workflow_ref="Yunushan/market-sentinel/.github/workflows/deployment-evidence.yml@refs/heads/main", source_ref="refs/heads/main", artifact_name="wrong")

    def test_rejects_collector_nonce_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw.json"
            external = root / "external.json"
            identity = root / "identity.json"
            raw.write_text(json.dumps({"collection": {"public_origin": ORIGIN, "run_id": 7, "run_attempt": 1, "nonce": "tampered"}}), encoding="utf-8")
            identity.write_text(json.dumps({"identity_type": "market-sentinel-deployment-release-identity", "repository": "Yunushan/market-sentinel", "frontend_sha256": "b" * 64, "release": {"target_commit": REVISION, "version": "1.0.11"}}), encoding="utf-8")
            with self.assertRaises(DeploymentEvidenceGenerationError):
                generate_evidence(raw, external, identity, public_origin=ORIGIN, run_id=7, run_attempt=1, workflow_ref="Yunushan/market-sentinel/.github/workflows/deployment-evidence.yml@refs/heads/main", source_ref="refs/heads/main", artifact_name=f"deployment-evidence-{REVISION}-7-1")

    def test_workflow_executes_protected_checkout_verifier_not_deployed_script(self) -> None:
        workflow = Path(".github/workflows/deployment-evidence.yml").read_text(encoding="utf-8")
        self.assertIn('"${GITHUB_WORKSPACE}/scripts/verify_production_deployment.py"', workflow)
        self.assertIn("--deployment-root /opt/market-sentinel", workflow)
        self.assertIn("--external-public-probe-only", workflow)
        self.assertIn("runs-on: ubuntu-24.04", workflow)
        self.assertNotIn("/opt/market-sentinel/scripts/verify_production_deployment.py", workflow)


if __name__ == "__main__":
    unittest.main()
