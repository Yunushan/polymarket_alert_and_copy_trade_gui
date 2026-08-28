from __future__ import annotations

import io
import json
import os
import subprocess
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


def _successful_public_checks() -> dict[str, dict[str, object]]:
    semantics = {
        "clob_time": "current_unix_time",
        "gamma_markets": "market_identity",
        "data_leaderboard": "leaderboard_identity",
        "bridge_supported_assets": "supported_asset_identity",
    }
    return {
        name: {
            "status": "ok",
            "detail": f"{name} responded.",
            "sample_type": "dict",
            "semantic_check": semantics[name],
        }
        for name in live_probe.PUBLIC_CHECK_NAMES
    }


def _successful_authenticated_reads() -> dict[str, dict[str, object]]:
    return {
        "clob_l2_orders": {
            "status": "ok",
            "detail": "orders read",
            "sample_type": "list",
            "semantic_check": "authenticated_order_collection",
            "records_observed": 0,
        },
        "py_clob_client_credentials": {"status": "ok", "detail": "credentials derived"},
    }


def _clean_source_state(revision: str = SOURCE_REVISION) -> dict[str, object]:
    return {
        "revision": revision,
        "clean": True,
        "git_available": True,
        "repository_origin": "github.com/yunushan/market-sentinel",
    }


class PublicOnlyPolymarketProbeTests(unittest.TestCase):
    def test_authenticated_reads_stay_available_without_legacy_sdk_derivation(self) -> None:
        credentials = {
            "POLY_API_KEY": "api-key",
            "POLY_API_SECRET": "api-secret",
            "POLY_PASSPHRASE": "passphrase",
            "POLYMARKET_PRIVATE_KEY": "0x" + "1" * 64,
        }
        with (
            patch.dict(os.environ, credentials, clear=True),
            patch.object(live_probe, "PolymarketTrader") as trader_cls,
        ):
            trader_cls.return_value.get_orders.return_value = []
            checks = live_probe._authenticated_read_checks(2.0)

        self.assertEqual(checks["clob_l2_orders"]["status"], "ok")
        self.assertEqual(checks["clob_l2_orders"]["semantic_check"], "authenticated_order_collection")
        self.assertEqual(checks["py_clob_client_credentials"]["status"], "ok")
        trader_cls.return_value.get_orders.assert_called_once_with(only_first_page=True)
        config = trader_cls.call_args.args[0]
        self.assertTrue(config.authenticated_sdk_reads)
        self.assertFalse(config.allow_api_key_derivation)
        self.assertFalse(config.allow_api_key_creation)
        self.assertEqual(config.api_secret, "api-secret")

    def test_strict_source_provenance_requires_the_canonical_origin(self) -> None:
        self.assertEqual(
            live_probe._canonical_repository_origin("https://github.com/Yunushan/market-sentinel.git"),
            "github.com/yunushan/market-sentinel",
        )
        self.assertEqual(
            live_probe._canonical_repository_origin("git@github.com:Yunushan/market-sentinel.git"),
            "github.com/yunushan/market-sentinel",
        )
        self.assertEqual(live_probe._canonical_repository_origin("https://github.com/example/fork.git"), "")

        wrong_origin = _clean_source_state()
        wrong_origin["repository_origin"] = ""
        provenance = live_probe._strict_source_provenance(wrong_origin, wrong_origin)
        gate = live_probe._funded_source_revision_gate(wrong_origin, wrong_origin)

        self.assertFalse(provenance["stable"])
        self.assertEqual(provenance["repository_origin"], "")
        self.assertEqual(gate["status"], "fail")
        self.assertEqual(gate["repository_origin"], "")

    def test_readonly_git_command_scopes_safe_directory_to_checkout(self) -> None:
        completed = subprocess.CompletedProcess(
            ["git"],
            0,
            f"{live_probe.ROOT.resolve()}\n",
            "",
        )
        with patch.object(live_probe.subprocess, "run", return_value=completed) as run:
            live_probe._run_git_readonly("rev-parse", "--show-toplevel")

        command = run.call_args.args[0]
        self.assertIn(f"safe.directory={live_probe.ROOT.resolve()}", command)
        self.assertEqual(run.call_args.kwargs["env"]["GIT_CONFIG_GLOBAL"], os.devnull)

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
            ("--recovery-journal", str((Path.cwd() / "recovery.json").resolve())),
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

    def test_funded_cli_blocks_before_mutation_transport_while_v2_is_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "blocked-funded.json"
            with (
                patch.object(
                    live_probe,
                    "_repository_source_state",
                    side_effect=[_clean_source_state(), _clean_source_state(), _clean_source_state()],
                ),
                patch.object(
                    live_probe,
                    "_enforce_windows_recovery_journal_privacy",
                    side_effect=AssertionError("unsupported mutations must not prepare a recovery journal"),
                ) as journal_acl,
                patch.object(live_probe, "_load_env"),
                patch.object(live_probe, "_public_checks", return_value=_successful_public_checks()),
                patch.object(
                    live_probe,
                    "_authenticated_read_checks",
                    return_value=_successful_authenticated_reads(),
                ),
                patch.object(
                    live_probe,
                    "_bridge_address_checks",
                    return_value={"deposit_address_creation": {"status": "blocked"}},
                ),
                patch.object(live_probe, "build_clob_auth_readiness", return_value={"ok": True}),
                patch.object(live_probe, "build_polymarket_credential_runbook", return_value={}),
                patch.object(
                    live_probe,
                    "run_live_order_cancel_verification",
                    side_effect=AssertionError("unsupported mutations must not reach the execution harness"),
                ) as verifier,
                redirect_stdout(io.StringIO()),
            ):
                exit_code = live_probe.main(
                    ["--allow-funded-order", "--report-file", str(report_path)]
                )

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 1)
            self.assertEqual(report["funded_live_order_check"]["status"], "blocked")
            self.assertFalse(report["funded_live_order_check"]["live_action"])
            self.assertFalse(report["funded_live_order_check"]["execution_supported"])
            self.assertIn("CLOB V2", report["funded_live_order_check"]["detail"])
            journal_acl.assert_not_called()
            verifier.assert_not_called()

    @patch.object(live_probe, "POLYMARKET_BOUNDED_AUDIT_MUTATIONS_SUPPORTED", True)
    def test_funded_cli_requires_a_distinct_absolute_recovery_journal(self) -> None:
        invalid_argv = (
            ["--allow-funded-order"],
            ["--allow-funded-order", "--recovery-journal", "relative-recovery.json"],
            ["--recovery-journal", str((Path.cwd() / "recovery.json").resolve())],
        )
        for argv in invalid_argv:
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    live_probe.main(argv)
            self.assertEqual(raised.exception.code, 2)

        with tempfile.TemporaryDirectory() as tmp:
            report_path = (Path(tmp) / "same.json").resolve()
            with (
                patch.object(live_probe, "_enforce_windows_recovery_journal_privacy"),
                redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit) as raised:
                    live_probe.main(
                        [
                            "--allow-funded-order",
                            "--recovery-journal",
                            str(report_path),
                            "--report-file",
                            str(report_path),
                        ]
                    )
            self.assertEqual(raised.exception.code, 2)

    @unittest.skipUnless(os.name == "nt", "Windows-specific funded-journal ACL gate")
    @patch.object(live_probe, "POLYMARKET_BOUNDED_AUDIT_MUTATIONS_SUPPORTED", True)
    def test_windows_funded_cli_fails_closed_without_owner_only_acl_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recovery_path = (Path(tmp) / "recovery.json").resolve()
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as raised:
                    live_probe.main(
                        [
                            "--allow-funded-order",
                            "--recovery-journal",
                            str(recovery_path),
                        ]
                    )
            self.assertEqual(raised.exception.code, 2)
            self.assertIn("cannot verify an owner-only directory ACL", stderr.getvalue())

    def test_credential_only_cli_does_not_require_a_funded_journal_acl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "credential.json"
            with (
                patch.object(
                    live_probe,
                    "_repository_source_state",
                    side_effect=[_clean_source_state(), _clean_source_state()],
                ),
                patch.object(
                    live_probe,
                    "_enforce_windows_recovery_journal_privacy",
                    side_effect=AssertionError("credential-only mode must not inspect funded-journal ACLs"),
                ),
                patch.object(live_probe, "_load_env"),
                patch.object(live_probe, "_public_checks", return_value=_successful_public_checks()),
                patch.object(
                    live_probe,
                    "_authenticated_read_checks",
                    return_value=_successful_authenticated_reads(),
                ),
                patch.object(
                    live_probe,
                    "_bridge_address_checks",
                    return_value={"deposit_address_creation": {"status": "blocked"}},
                ),
                patch.object(live_probe, "build_clob_auth_readiness", return_value={"ok": True}),
                patch.object(live_probe, "build_polymarket_credential_runbook", return_value={}),
                patch.object(
                    live_probe,
                    "_funded_order_check",
                    return_value={"status": "blocked", "live_action": False},
                ),
                redirect_stdout(io.StringIO()),
            ):
                exit_code = live_probe.main(
                    [
                        "--require-authenticated-read-ok",
                        "--report-file",
                        str(report_path),
                    ]
                )

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 0)
            self.assertTrue(report["ok"])
            self.assertTrue(report["stage_gates"]["credentialed_read_ok"])
            self.assertTrue(report["source_provenance"]["stable"])

    @patch.object(live_probe, "POLYMARKET_BOUNDED_AUDIT_MUTATIONS_SUPPORTED", True)
    def test_strict_cli_blocks_funded_harness_when_only_credential_derivation_succeeds(self) -> None:
        authenticated_checks = {
            "py_clob_client_credentials": {"status": "ok", "detail": "credentials derived"},
            "clob_l2_orders": {"status": "blocked"},
            "relayer_recent_transactions": {"status": "blocked"},
            "user_websocket_connect": {"status": "skipped"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "strict.json"
            recovery_path = Path(tmp) / "recovery.json"
            with (
                patch.object(
                    live_probe,
                    "_repository_source_state",
                    side_effect=[_clean_source_state(), _clean_source_state(), _clean_source_state()],
                ),
                patch.object(live_probe, "_enforce_windows_recovery_journal_privacy"),
                patch.object(live_probe, "_load_env"),
                patch.object(live_probe, "_public_checks", return_value=_successful_public_checks()),
                patch.object(live_probe, "_authenticated_read_checks", return_value=authenticated_checks),
                patch.object(
                    live_probe,
                    "_bridge_address_checks",
                    return_value={"deposit_address_creation": {"status": "blocked"}},
                ),
                patch.object(live_probe, "build_clob_auth_readiness", return_value={"ok": True}),
                patch.object(live_probe, "build_polymarket_credential_runbook", return_value={}),
                patch.object(
                    live_probe,
                    "_funded_order_check",
                    side_effect=AssertionError("funded harness must not run without an accepted read"),
                ) as funded,
                redirect_stdout(io.StringIO()),
            ):
                exit_code = live_probe.main(
                    [
                        "--allow-funded-order",
                        "--require-authenticated-read-ok",
                        "--token-id",
                        "token-1",
                        "--side",
                        "BUY",
                        "--price",
                        "0.01",
                        "--size",
                        "1",
                        "--cancel-immediately",
                        "--allow-token-id",
                        "token-1",
                        "--confirm-live-order-cancel",
                        live_probe.CONFIRM_LIVE_ORDER_CANCEL,
                        "--recovery-journal",
                        str(recovery_path.resolve()),
                        "--report-file",
                        str(report_path),
                    ]
                )

            funded.assert_not_called()
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 1)
            self.assertFalse(report["stage_gates"]["credentialed_read_ok"])
            self.assertEqual(report["stage_gates"]["accepted_credential_read_checks"], [])
            self.assertEqual(report["funded_live_order_check"]["status"], "blocked")
            self.assertFalse(report["funded_live_order_check"]["live_action"])
            self.assertTrue(report["source_provenance"]["stable"])
            self.assertEqual(report["source_provenance"]["source_revision"], SOURCE_REVISION)

    @patch.object(live_probe, "POLYMARKET_BOUNDED_AUDIT_MUTATIONS_SUPPORTED", True)
    def test_strict_cli_allows_mocked_funded_harness_only_after_accepted_read_and_clean_source(self) -> None:
        authenticated_checks = _successful_authenticated_reads()
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "strict.json"
            recovery_path = Path(tmp) / "recovery.json"
            with (
                patch.object(
                    live_probe,
                    "_repository_source_state",
                    side_effect=[_clean_source_state(), _clean_source_state(), _clean_source_state()],
                ),
                patch.object(live_probe, "_enforce_windows_recovery_journal_privacy"),
                patch.object(live_probe, "_load_env"),
                patch.object(live_probe, "_public_checks", return_value=_successful_public_checks()),
                patch.object(live_probe, "_authenticated_read_checks", return_value=authenticated_checks),
                patch.object(
                    live_probe,
                    "_bridge_address_checks",
                    return_value={"deposit_address_creation": {"status": "blocked"}},
                ),
                patch.object(live_probe, "build_clob_auth_readiness", return_value={"ok": True}),
                patch.object(live_probe, "build_polymarket_credential_runbook", return_value={}),
                patch.object(
                    live_probe,
                    "_funded_order_check",
                    return_value={
                        "status": "ok",
                        "live_action": True,
                        "manual_reconciliation_required": False,
                    },
                ) as funded,
                redirect_stdout(io.StringIO()),
            ):
                exit_code = live_probe.main(
                    [
                        "--allow-funded-order",
                        "--token-id",
                        "token-1",
                        "--side",
                        "BUY",
                        "--price",
                        "0.01",
                        "--size",
                        "1",
                        "--cancel-immediately",
                        "--allow-token-id",
                        "token-1",
                        "--confirm-live-order-cancel",
                        live_probe.CONFIRM_LIVE_ORDER_CANCEL,
                        "--recovery-journal",
                        str(recovery_path.resolve()),
                        "--report-file",
                        str(report_path),
                    ]
                )

            funded.assert_called_once()
            self.assertEqual(funded.call_args.kwargs, {"source_revision": SOURCE_REVISION})
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 0)
            self.assertEqual(report["stage_gates"]["accepted_credential_read_checks"], ["clob_l2_orders"])
            self.assertTrue(report["stage_gates"]["credentialed_read_ok"])
            self.assertTrue(report["source_provenance"]["stable"])

    @patch.object(live_probe, "POLYMARKET_BOUNDED_AUDIT_MUTATIONS_SUPPORTED", True)
    def test_strict_cli_fails_when_source_changes_after_funded_execution(self) -> None:
        final_state = {
            "revision": "b" * 40,
            "clean": True,
            "git_available": True,
            "repository_origin": "github.com/yunushan/market-sentinel",
        }
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "strict.json"
            recovery_path = Path(tmp) / "recovery.json"
            with (
                patch.object(
                    live_probe,
                    "_repository_source_state",
                    side_effect=[_clean_source_state(), _clean_source_state(), final_state],
                ),
                patch.object(live_probe, "_enforce_windows_recovery_journal_privacy"),
                patch.object(live_probe, "_load_env"),
                patch.object(live_probe, "_public_checks", return_value=_successful_public_checks()),
                patch.object(
                    live_probe,
                    "_authenticated_read_checks",
                    return_value=_successful_authenticated_reads(),
                ),
                patch.object(
                    live_probe,
                    "_bridge_address_checks",
                    return_value={"deposit_address_creation": {"status": "blocked"}},
                ),
                patch.object(live_probe, "build_clob_auth_readiness", return_value={"ok": True}),
                patch.object(live_probe, "build_polymarket_credential_runbook", return_value={}),
                patch.object(
                    live_probe,
                    "_funded_order_check",
                    return_value={
                        "status": "ok",
                        "live_action": True,
                        "manual_reconciliation_required": False,
                    },
                ) as funded,
                redirect_stdout(io.StringIO()),
            ):
                exit_code = live_probe.main(
                    [
                        "--allow-funded-order",
                        "--token-id",
                        "token-1",
                        "--side",
                        "BUY",
                        "--price",
                        "0.01",
                        "--size",
                        "1",
                        "--cancel-immediately",
                        "--allow-token-id",
                        "token-1",
                        "--confirm-live-order-cancel",
                        live_probe.CONFIRM_LIVE_ORDER_CANCEL,
                        "--recovery-journal",
                        str(recovery_path.resolve()),
                        "--report-file",
                        str(report_path),
                    ]
                )

            funded.assert_called_once()
            self.assertEqual(funded.call_args.kwargs, {"source_revision": SOURCE_REVISION})
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 1)
            self.assertFalse(report["ok"])
            self.assertEqual(report["funded_live_order_check"]["status"], "ok")
            self.assertEqual(report["funded_live_order_check"]["source_revision_gate"]["status"], "pass")
            self.assertFalse(report["source_provenance"]["stable"])
            self.assertEqual(report["stage_gates"]["exact_source_revision"], "failed")

    @patch.object(live_probe, "POLYMARKET_BOUNDED_AUDIT_MUTATIONS_SUPPORTED", True)
    def test_strict_cli_blocks_funded_harness_from_dirty_source(self) -> None:
        dirty_state = {
            "revision": SOURCE_REVISION,
            "clean": False,
            "git_available": True,
            "repository_origin": "github.com/yunushan/market-sentinel",
        }
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "strict.json"
            recovery_path = Path(tmp) / "recovery.json"
            with (
                patch.object(
                    live_probe,
                    "_repository_source_state",
                    side_effect=[dirty_state, dirty_state, dirty_state],
                ),
                patch.object(live_probe, "_enforce_windows_recovery_journal_privacy"),
                patch.object(live_probe, "_load_env"),
                patch.object(live_probe, "_public_checks", return_value=_successful_public_checks()),
                patch.object(
                    live_probe,
                    "_authenticated_read_checks",
                    return_value=_successful_authenticated_reads(),
                ),
                patch.object(
                    live_probe,
                    "_bridge_address_checks",
                    return_value={"deposit_address_creation": {"status": "blocked"}},
                ),
                patch.object(live_probe, "build_clob_auth_readiness", return_value={"ok": True}),
                patch.object(live_probe, "build_polymarket_credential_runbook", return_value={}),
                patch.object(
                    live_probe,
                    "_funded_order_check",
                    side_effect=AssertionError("funded harness must not run from dirty source"),
                ) as funded,
                redirect_stdout(io.StringIO()),
            ):
                exit_code = live_probe.main(
                    [
                        "--allow-funded-order",
                        "--token-id",
                        "token-1",
                        "--side",
                        "BUY",
                        "--price",
                        "0.01",
                        "--size",
                        "1",
                        "--cancel-immediately",
                        "--allow-token-id",
                        "token-1",
                        "--confirm-live-order-cancel",
                        live_probe.CONFIRM_LIVE_ORDER_CANCEL,
                        "--recovery-journal",
                        str(recovery_path.resolve()),
                        "--report-file",
                        str(report_path),
                    ]
                )

            funded.assert_not_called()
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 1)
            self.assertFalse(report["source_provenance"]["stable"])
            self.assertEqual(report["source_provenance"]["source_revision"], "")
            self.assertEqual(report["funded_live_order_check"]["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
