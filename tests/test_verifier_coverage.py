from __future__ import annotations

import unittest
from unittest.mock import patch
import subprocess
import sys

import verify
from market_adapters import MARKET_IDS, VERIFIED_BLOCKERS


class VerifierCoverageTests(unittest.TestCase):
    def test_static_analysis_policy_includes_dynamic_sql_detection(self) -> None:
        data = verify.tomllib.loads((verify.ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertIn("S608", data["tool"]["ruff"]["lint"]["select"])

    def test_static_analysis_gate_uses_the_pinned_ruff_module(self) -> None:
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch("verify.subprocess.run", return_value=result) as run:
            verify.run_static_analysis()

        run.assert_called_once_with(
            [sys.executable, "-m", "ruff", "check", "."],
            cwd=verify.ROOT,
            capture_output=True,
            text=True,
        )

    def test_static_analysis_gate_reports_findings(self) -> None:
        result = subprocess.CompletedProcess(args=[], returncode=1, stdout="F401 unused import", stderr="")
        with patch("verify.subprocess.run", return_value=result):
            with self.assertRaisesRegex(SystemExit, "(?s)Ruff static analysis failed:.*F401"):
                verify.run_static_analysis()

    def test_branch_coverage_policy_has_overall_and_backend_floors(self) -> None:
        self.assertGreaterEqual(verify.MIN_TOTAL_BRANCH_COVERAGE, 65.0)
        self.assertGreaterEqual(verify.MIN_BACKEND_BRANCH_COVERAGE, 74.0)
        self.assertIn("web_api.py", verify.BACKEND_COVERAGE_INCLUDE)
        self.assertIn("market_sentinel_cli.py", verify.BACKEND_COVERAGE_INCLUDE)
        self.assertEqual(verify.RESOURCE_WARNING_POLICY, "error::ResourceWarning")

    def test_implemented_adapter_fixture_mapping_matches_the_catalog(self) -> None:
        implemented = set(MARKET_IDS) - set(VERIFIED_BLOCKERS)
        self.assertEqual(set(verify.IMPLEMENTED_ADAPTER_FIXTURE_TESTS), implemented)
        verify.run_adapter_fixture_coverage_check()

    def test_implemented_adapter_fixture_check_rejects_an_incomplete_mapping(self) -> None:
        broken = dict(verify.IMPLEMENTED_ADAPTER_FIXTURE_TESTS)
        broken.pop("polymarket")

        with patch.object(verify, "IMPLEMENTED_ADAPTER_FIXTURE_TESTS", broken):
            with self.assertRaises(SystemExit) as ctx:
                verify.run_adapter_fixture_coverage_check()

        self.assertIn("missing mappings: polymarket", str(ctx.exception))

    def test_support_matrix_snapshot_matches_goal_document(self) -> None:
        verify.run_support_matrix_snapshot_check()
