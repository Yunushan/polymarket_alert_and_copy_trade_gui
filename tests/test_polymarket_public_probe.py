from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import verify_polymarket_live as live_probe


SOURCE_REVISION = "a" * 40
SOURCE_REPOSITORY = "Yunushan/market-sentinel"
SOURCE_WORKFLOW_REF = f"{SOURCE_REPOSITORY}/.github/workflows/ci.yml@refs/heads/main"


def _source_args() -> list[str]:
    return [
        "--source-repository",
        SOURCE_REPOSITORY,
        "--source-revision",
        SOURCE_REVISION,
        "--source-run-id",
        "123456",
        "--source-run-attempt",
        "2",
        "--source-workflow-ref",
        SOURCE_WORKFLOW_REF,
    ]


def _successful_public_checks() -> dict[str, dict[str, str]]:
    return {
        name: {"status": "ok", "detail": f"{name} responded.", "sample_type": "dict"}
        for name in live_probe.PUBLIC_CHECK_NAMES
    }


class PublicOnlyPolymarketProbeTests(unittest.TestCase):
    def test_public_checks_require_semantically_usable_payloads(self) -> None:
        valid_payloads = {
            "clob_time": {"time": int(time.time())},
            "gamma_markets": [{"id": "123", "question": "Will the semantic probe pass?"}],
            "data_leaderboard": [
                {"rank": "1", "proxyWallet": "0x" + "1" * 40, "pnl": 1.0, "vol": 2.0}
            ],
            "bridge_supported_assets": {
                "supportedAssets": [
                    {
                        "chainId": "1",
                        "token": {"name": "USD Coin", "symbol": "USDC", "address": "0x" + "2" * 40},
                    }
                ]
            },
        }

        def run_with(payloads: dict[str, object]) -> dict[str, dict[str, object]]:
            with (
                patch.object(live_probe.clob_rest, "get_server_time", return_value=payloads["clob_time"]),
                patch.object(live_probe.gamma, "list_markets", return_value=payloads["gamma_markets"]),
                patch.object(live_probe.data_api, "get_leaderboard", return_value=payloads["data_leaderboard"]),
                patch.object(
                    live_probe.bridge,
                    "get_supported_assets",
                    return_value=payloads["bridge_supported_assets"],
                ),
            ):
                return live_probe._public_checks(5.0)

        accepted = run_with(valid_payloads)
        self.assertTrue(all(check["status"] == "ok" for check in accepted.values()))
        self.assertTrue(all("semantic_check" in check for check in accepted.values()))

        invalid_payloads = {
            "clob_time": {},
            "gamma_markets": [],
            "data_leaderboard": [{"rank": "1", "pnl": 1.0}],
            "bridge_supported_assets": {"supportedAssets": []},
        }
        for name, invalid in invalid_payloads.items():
            with self.subTest(name=name):
                payloads = dict(valid_payloads)
                payloads[name] = invalid
                checks = run_with(payloads)
                self.assertEqual(checks[name]["status"], "failed")
                self.assertTrue(all(checks[other]["status"] == "ok" for other in checks if other != name))

    def test_public_only_mode_skips_dotenv_and_all_private_or_mutating_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "public.json"
            with (
                patch.object(live_probe, "_public_only_credential_variables", return_value=[]),
                patch.object(live_probe, "_public_checks", return_value=_successful_public_checks()) as public,
                patch.object(live_probe, "_load_env") as load_env,
                patch.object(
                    live_probe,
                    "_authenticated_read_checks",
                    side_effect=AssertionError("authenticated reads must not run"),
                ),
                patch.object(
                    live_probe,
                    "_bridge_address_checks",
                    side_effect=AssertionError("bridge mutations must not run"),
                ),
                patch.object(
                    live_probe,
                    "_funded_order_check",
                    side_effect=AssertionError("funded orders must not run"),
                ),
                patch.object(
                    live_probe,
                    "build_clob_auth_readiness",
                    side_effect=AssertionError("auth readiness must not inspect credentials"),
                ),
                patch.object(
                    live_probe,
                    "build_polymarket_credential_runbook",
                    side_effect=AssertionError("credential runbook must not be built"),
                ),
                redirect_stdout(io.StringIO()),
            ):
                exit_code = live_probe.main(
                    ["--public-only", "--report-file", str(report_path), "--timeout", "15", *_source_args()]
                )

            self.assertEqual(exit_code, 0)
            public.assert_called_once_with(15.0)
            load_env.assert_not_called()
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(
                set(report),
                {"ok", "mode", "market_id", "evidence", "safety", "public_checks"},
            )
            self.assertTrue(report["ok"])
            self.assertEqual(report["mode"], "public_only")
            self.assertEqual(report["safety"], live_probe.PUBLIC_ONLY_SAFETY)
            self.assertEqual(set(report["public_checks"]), set(live_probe.PUBLIC_CHECK_NAMES))
            self.assertNotIn("authenticated_read_checks", report)
            self.assertNotIn("bridge_address_checks", report)
            self.assertNotIn("funded_live_order_check", report)
            self.assertNotIn("credential_presence", report)

            evidence = report["evidence"]
            self.assertEqual(set(evidence), set(live_probe.PUBLIC_ONLY_EVIDENCE_FIELDS))
            self.assertEqual(evidence["schema_version"], 1)
            self.assertEqual(evidence["profile"], "public-only")
            self.assertEqual(evidence["repository"], SOURCE_REPOSITORY)
            self.assertEqual(evidence["source_revision"], SOURCE_REVISION)
            self.assertEqual(evidence["run_id"], 123456)
            self.assertEqual(evidence["run_attempt"], 2)
            self.assertEqual(evidence["workflow"], ".github/workflows/ci.yml")
            self.assertEqual(evidence["workflow_name"], "CI")
            self.assertEqual(evidence["workflow_ref"], SOURCE_WORKFLOW_REF)
            self.assertEqual(evidence["event"], "workflow_dispatch")
            self.assertEqual(evidence["runner_environment"], "github-hosted")
            for field in ("generated_at", "started_at", "completed_at"):
                self.assertIsNotNone(live_probe._parse_utc_timestamp(evidence[field]))

    def test_public_only_mode_rejects_credential_environment_before_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "public.json"
            with (
                patch.dict(os.environ, {"POLY_API_KEY": "must-not-be-used"}, clear=True),
                patch.object(live_probe, "_public_checks") as public,
                redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit) as raised:
                    live_probe.main(
                        ["--public-only", "--report-file", str(report_path), *_source_args()]
                    )

            self.assertEqual(raised.exception.code, 2)
            public.assert_not_called()
            self.assertFalse(report_path.exists())

    def test_public_only_mode_rejects_every_private_or_mutating_option(self) -> None:
        forbidden_argument_sets = (
            ("--skip-public-checks",),
            ("--skip-authenticated-read-checks",),
            ("--require-authenticated-read-ok",),
            ("--include-user-websocket-connect",),
            ("--user-ws-market", "market-1"),
            ("--include-bridge-address-creation",),
            ("--bridge-address", "0x123"),
            ("--to-chain-id", "1"),
            ("--to-token-address", "0x456"),
            ("--recipient-addr", "0x789"),
            ("--allow-funded-order",),
            ("--cancel-immediately",),
            ("--confirm-live-order-cancel", "confirmation"),
            ("--allow-token-id", "token"),
            ("--allow-token-file", "tokens.txt"),
            ("--token-id", "token"),
            ("--side", "BUY"),
            ("--price", "0.5"),
            ("--size", "1"),
            ("--tif", "GTC"),
            ("--max-verify-size", "1"),
            ("--max-verify-notional", "1"),
            ("--maker-price-buffer", "0.01"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "public.json"
            for forbidden_args in forbidden_argument_sets:
                with self.subTest(option=forbidden_args[0]):
                    with (
                        patch.object(live_probe, "_public_only_credential_variables", return_value=[]),
                        patch.object(live_probe, "_public_checks") as public,
                        redirect_stderr(io.StringIO()),
                    ):
                        with self.assertRaises(SystemExit) as raised:
                            live_probe.main(
                                [
                                    "--public-only",
                                    "--report-file",
                                    str(report_path),
                                    *_source_args(),
                                    *forbidden_args,
                                ]
                            )
                    self.assertEqual(raised.exception.code, 2)
                    public.assert_not_called()

    def test_standalone_validator_rejects_tampering_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "public.json"
            with (
                patch.object(live_probe, "_public_only_credential_variables", return_value=[]),
                patch.object(live_probe, "_public_checks", return_value=_successful_public_checks()),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    live_probe.main(["--public-only", "--report-file", str(report_path), *_source_args()]),
                    0,
                )

            validator_args = ["--validate-public-only-report", str(report_path), *_source_args()]
            with (
                patch.object(live_probe, "_public_only_credential_variables", return_value=[]),
                patch.object(
                    live_probe,
                    "_public_checks",
                    side_effect=AssertionError("standalone validation must not make network requests"),
                ),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(live_probe.main(validator_args), 0)

            tampered = json.loads(report_path.read_text(encoding="utf-8"))
            tampered["safety"]["funded_orders_attempted"] = True
            report_path.write_text(json.dumps(tampered), encoding="utf-8")
            with (
                patch.object(live_probe, "_public_only_credential_variables", return_value=[]),
                patch.object(live_probe, "_public_checks") as public,
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(live_probe.main(validator_args), 1)
            public.assert_not_called()

    def test_failed_public_endpoint_writes_non_attestable_failure_report(self) -> None:
        checks = _successful_public_checks()
        checks["gamma_markets"] = {"status": "failed", "detail": "timeout"}
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "public.json"
            with (
                patch.object(live_probe, "_public_only_credential_variables", return_value=[]),
                patch.object(live_probe, "_public_checks", return_value=checks),
                redirect_stdout(io.StringIO()),
            ):
                exit_code = live_probe.main(
                    ["--public-only", "--report-file", str(report_path), *_source_args()]
                )
            self.assertEqual(exit_code, 1)
            self.assertFalse(json.loads(report_path.read_text(encoding="utf-8"))["ok"])

            with (
                patch.object(live_probe, "_public_only_credential_variables", return_value=[]),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(
                    live_probe.main(
                        ["--validate-public-only-report", str(report_path), *_source_args()]
                    ),
                    1,
                )


if __name__ == "__main__":
    unittest.main()
