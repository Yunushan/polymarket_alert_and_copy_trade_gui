from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from app import tkinter_smoke_payload
from core.models import AppConfig
from market_adapters import MARKET_IDS
from market_adapters.registry import build_default_registry
from scripts import verify_live_validation_report_smoke as live_smoke
from web_api import app_state_payload


ROOT = Path(__file__).resolve().parent.parent


class FinalParityTests(unittest.TestCase):
    def test_react_account_operation_union_covers_registry_allow_list(self) -> None:
        """Prevent a newly exposed adapter account operation from becoming UI-only dynamic data."""

        source = (ROOT / "frontend" / "src" / "types.ts").read_text(encoding="utf-8")
        block = source.split("export type MarketAccountOperation =", 1)[1].split(
            "export interface MarketAccountPayload", 1
        )[0]
        declared = set(re.findall(r'^\s*\|\s*"([^"\\]+)"', block, flags=re.MULTILINE))
        registry = build_default_registry()
        expected = {
            str(operation).strip().lower()
            for metadata in registry.list_metadata()
            for operation in getattr(registry.create(metadata.market_id), "account_recovery_operations", ())
        }
        self.assertTrue(expected <= declared, f"Frontend account operation union is missing: {sorted(expected - declared)}")

    def test_tkinter_smoke_payload_proves_fallback_gui_contract(self) -> None:
        payload = tkinter_smoke_payload()

        self.assertTrue(payload["ok"])
        self.assertTrue(payload["tkinter_base"])
        self.assertEqual(payload["app_class"], "App")
        self.assertEqual(payload["window_title"], "MarketSentinel")
        self.assertEqual(payload["fallback_command"], "python app.py")
        self.assertEqual(payload["market_count"], len(MARKET_IDS))
        self.assertEqual(payload["choice_count"], len(MARKET_IDS))
        self.assertTrue(payload["all_markets_configured"])
        self.assertIn("Classic", payload["ui_designs"])
        self.assertIn("Aurora 2026", payload["ui_designs"])
        self.assertIn("Graphite 2026", payload["ui_designs"])
        self.assertIn("Sentinel 2027", payload["ui_designs"])
        self.assertIn("Polymarket Analytics", payload["desktop_tabs"])
        self.assertTrue(payload["icon_available"])

    def test_react_state_payload_covers_final_workflow_surfaces(self) -> None:
        payload = app_state_payload(AppConfig())

        self.assertIn("health", payload)
        self.assertIn("config", payload)
        self.assertIn("markets", payload)
        self.assertIn("alerts", payload)
        self.assertIn("wallets", payload)
        self.assertIn("copy", payload)
        self.assertIn("live_safety", payload)
        self.assertIn("polymarket_live_validation", payload)
        self.assertIn("polymarket_live_validation_reports", payload)
        self.assertIn("paper", payload)
        self.assertEqual(payload["health"]["tkinter_fallback"], "run_gui.bat or python app.py")
        self.assertIn("/api/alerts", payload["health"]["routes"]["GET"])
        self.assertIn("/api/wallets", payload["health"]["routes"]["GET"])
        self.assertIn("/api/paper", payload["health"]["routes"]["GET"])
        self.assertIn("/api/live-safety/preflight", payload["health"]["routes"]["POST"])

    def test_frontend_package_supports_strict_build_verification(self) -> None:
        package = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))

        self.assertEqual(package["scripts"]["dev"], "vite --host 127.0.0.1 --port 5173")
        self.assertEqual(
            package["scripts"]["build"],
            "tsc -p tsconfig.json --noEmit && tsc -p tsconfig.node.json --noEmit && vite build",
        )
        self.assertEqual(package["scripts"]["preview"], "vite preview --host 127.0.0.1 --port 4173")
        self.assertIn("react", package["dependencies"])
        self.assertIn("typescript", package["devDependencies"])
        self.assertIn("vite", package["devDependencies"])

    def test_status_pill_accepts_formatted_jsx_children(self) -> None:
        source = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")

        self.assertIn("import type { FormEvent, ReactNode } from \"react\";", source)
        self.assertIn("{ children: ReactNode; tone?: \"good\" | \"warn\" | \"neutral\" }", source)

    def test_react_sx_bet_order_management_is_wired_through_settings_and_form(self) -> None:
        source = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
        type_source = (ROOT / "frontend" / "src" / "types.ts").read_text(encoding="utf-8")

        self.assertIn("sx_bet_order_management_enabled", source)
        self.assertIn('selectedMarket.market_id === "sx_bet"', source)
        self.assertIn("cancel_event_orders", source)
        self.assertIn("SX Bet v3 uses fixed DELETE /orders-v3", source)
        self.assertIn("I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS", source)
        self.assertIn("account_market_hash", source)
        self.assertIn("account_client_order_id", source)
        self.assertIn("account_start_date", source)
        self.assertIn("account_sort_asc", source)
        self.assertIn("account_outcome_id", source)
        self.assertIn("account_locale", source)
        self.assertIn("account_exclude_item", source)
        self.assertIn("account_from_currency", source)
        self.assertIn("account_order_by", source)
        self.assertIn("account_sort_dir", source)
        self.assertIn("account_search", source)
        self.assertIn("account_with_cash_outs", source)
        self.assertIn("account_before", source)
        self.assertIn("account_after", source)
        self.assertIn("account_before_time", source)
        self.assertIn("account_after_time", source)
        self.assertIn("force: form.account_historical", source)
        self.assertIn("next: form.account_cursor", source)
        self.assertIn('"order_by_client_id"', type_source)

    def test_react_analytics_source_exposes_direct_mdd_lookup_and_cached_detail(self) -> None:
        app_source = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
        api_source = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
        type_source = (ROOT / "frontend" / "src" / "types.ts").read_text(encoding="utf-8")

        self.assertIn("Direct Wallet MDD", app_source)
        self.assertIn("MDD Audit Detail", app_source)
        self.assertIn("onAuditDetailLoad", app_source)
        self.assertIn("fetchPolymarketMddAudit", api_source)
        self.assertIn("PolymarketMddAuditExport", type_source)

    def test_react_analytics_source_exposes_mdd_cache_management(self) -> None:
        app_source = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
        api_source = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
        type_source = (ROOT / "frontend" / "src" / "types.ts").read_text(encoding="utf-8")

        self.assertIn("MDD Audit Cache", app_source)
        self.assertIn("onMddCachePurge", app_source)
        self.assertIn("fetchPolymarketMddCache", api_source)
        self.assertIn("purgePolymarketMddCache", api_source)
        self.assertIn("PolymarketMddCachePayload", type_source)

    def test_react_tabs_can_be_opened_for_browser_smoke_routes(self) -> None:
        source = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")

        self.assertIn("function initialTabFromUrl()", source)
        self.assertIn('new URLSearchParams(window.location.search).get("tab")', source)
        self.assertIn("useState<Tab>(initialTabFromUrl)", source)

    def test_react_live_safety_exposes_polymarket_live_validation_report(self) -> None:
        app_source = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
        api_source = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
        type_source = (ROOT / "frontend" / "src" / "types.ts").read_text(encoding="utf-8")

        self.assertIn("Polymarket Live Validation", app_source)
        self.assertIn("Validation Reports", app_source)
        self.assertIn("Schema Diagnostics", app_source)
        self.assertIn("Accepted modes", app_source)
        self.assertIn("Allow duplicate import", app_source)
        self.assertIn("Payload hash", app_source)
        self.assertIn("schemaValidationLabel", app_source)
        self.assertIn("Store Snapshot", app_source)
        self.assertIn("Opened Report", app_source)
        self.assertIn("Download JSON", app_source)
        self.assertIn("Review JSON", app_source)
        self.assertIn("Review Markdown", app_source)
        self.assertIn("stage_gates", app_source)
        self.assertIn("fetchPolymarketLiveValidation", api_source)
        self.assertIn("fetchPolymarketLiveValidationReport", api_source)
        self.assertIn("storePolymarketLiveValidationReport", api_source)
        self.assertIn("deletePolymarketLiveValidationReport", api_source)
        self.assertIn("ApiRequestError", api_source)
        self.assertIn("apiSchemaValidation", api_source)
        self.assertIn("polymarketLiveValidationReportExportUrl", api_source)
        self.assertIn("polymarketLiveValidationReportReviewJsonUrl", api_source)
        self.assertIn("polymarketLiveValidationReportReviewMarkdownUrl", api_source)
        self.assertIn("/api/polymarket/live-validation", api_source)
        self.assertIn("/export.json", api_source)
        self.assertIn("/review.json", api_source)
        self.assertIn("/review.md", api_source)
        self.assertIn("PolymarketLiveValidationPayload", type_source)
        self.assertIn("PolymarketLiveValidationReportSchemaValidation", type_source)
        self.assertIn("schema_validation", type_source)
        self.assertIn("PolymarketLiveValidationReportPayload", type_source)
        self.assertIn("PolymarketLiveValidationReportsPayload", type_source)
        self.assertIn("PolymarketLiveValidationReportReviewBundle", type_source)
        self.assertIn("payload_hash", type_source)
        self.assertIn("allow_duplicate", type_source)

    def test_windows_package_supports_tkinter_and_react_modes(self) -> None:
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        build_source = (ROOT / "scripts" / "build_windows_release.py").read_text(encoding="utf-8")

        self.assertIn("--web-gui", app_source)
        self.assertIn("UI_DESIGN_LABELS", app_source)
        self.assertIn("SetCurrentProcessExplicitAppUserModelID", app_source)
        self.assertIn('APP_ID = "market-sentinel"', app_source)
        self.assertIn('APP_TITLE = "MarketSentinel"', app_source)
        self.assertIn('background=[("active", tab_hover_bg), ("selected", tab_bg)]', app_source)
        self.assertIn("iconbitmap(default=str(icon_path))", app_source)
        self.assertIn("run_server(parsed.host, parsed.port, parsed.config)", app_source)
        self.assertIn("PyInstaller", build_source)
        self.assertIn('copy_file(ROOT / "assets" / "marketsentinel.ico"', build_source)
        self.assertIn('copy_file(ROOT / "marketsentinel.png"', build_source)
        self.assertIn("start_tkinter_gui.bat", build_source)
        self.assertIn("start_web_gui.bat", build_source)
        self.assertIn("PREDICTION_MARKET_CONFIG_PATH", build_source)
        self.assertIn("--config \"%PREDICTION_MARKET_CONFIG_PATH%\"", build_source)
        self.assertIn("StartMenuTkinterShortcut", build_source)
        self.assertIn("Target=\"[INSTALLFOLDER]start_tkinter_gui.bat\"", build_source)
        self.assertIn("wix, \"build\"", build_source)

    def test_readme_documents_final_parity_commands(self) -> None:
        text = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("python app.py --smoke-test", text)
        self.assertIn("python app.py --gui-smoke-test", text)
        self.assertIn("python verify.py --frontend-build", text)
        self.assertIn("npm run build", text)
        self.assertIn("run_gui.bat", text)

    def test_polymarket_leaderboard_unlimited_scan_controls_are_visible(self) -> None:
        web_api_source = (ROOT / "web_api.py").read_text(encoding="utf-8")
        app_source = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        cli_source = (ROOT / "market_sentinel_cli.py").read_text(encoding="utf-8")
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn("UNLIMITED_LIMIT_TOKENS", web_api_source)
        self.assertIn("POLYMARKET_LEADERBOARD_PAGE_SIZE = 50", web_api_source)
        self.assertIn('type="text"', app_source)
        self.assertIn("no local 1,000,000-row cap", readme)
        self.assertIn("source_scope_note", web_api_source)
        self.assertIn("source_enumeration_complete", web_api_source)
        self.assertIn("Scan ended:", app_source)
        self.assertIn("completion reason", readme)
        self.assertIn("polymarket-leaderboard", cli_source)
        self.assertIn("polymarket-leaderboard-status", cli_source)
        self.assertIn("polymarket-leaderboard-status", readme)
        self.assertIn("polymarket-leaderboard-export", cli_source)
        self.assertIn("polymarket-leaderboard-export", readme)
        self.assertIn('market-sentinel = "market_sentinel_cli:main"', pyproject)

    def test_verify_frontend_build_uses_windows_npm_command(self) -> None:
        source = (ROOT / "verify.py").read_text(encoding="utf-8")

        self.assertIn('"npm.cmd" if sys.platform == "win32" else "npm"', source)
        self.assertIn("[npm_command(), \"run\", \"build\"]", source)

    def test_live_validation_report_browser_smoke_is_reusable_and_safe(self) -> None:
        verify_source = (ROOT / "verify.py").read_text(encoding="utf-8")
        smoke_source = (ROOT / "scripts" / "verify_live_validation_report_smoke.py").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("--frontend-live-smoke", verify_source)
        self.assertIn("verify_live_validation_report_smoke.py", verify_source)
        self.assertIn("POLYMARKET_LIVE_VALIDATION_REPORTS_PATH", smoke_source)
        self.assertIn("TemporaryDirectory", smoke_source)
        self.assertIn("ignore_cleanup_errors=True", smoke_source)
        self.assertIn("BrowserStartupError", smoke_source)
        self.assertIn("BrowserRenderError", smoke_source)
        self.assertIn("--disable-extensions", smoke_source)
        self.assertIn("--headless", smoke_source)
        self.assertIn("browser_smoke", smoke_source)
        self.assertIn("funded_execution_exposed", smoke_source)
        self.assertIn("/api/polymarket/live-validation/reports", smoke_source)
        self.assertIn("/export.json", smoke_source)
        self.assertIn("secretPresent", smoke_source)
        self.assertIn("duplicate_import", smoke_source)
        self.assertIn("decision_ledger", smoke_source)
        self.assertIn("transient_shell", smoke_source)
        self.assertIn('"Failed to fetch"', smoke_source)
        self.assertIn("wait_for_state_api", smoke_source)
        self.assertIn("python verify.py --frontend-build --frontend-live-smoke", readme)

    def test_live_validation_browser_smoke_retries_a_transient_state_timeout(self) -> None:
        with patch.object(
            live_smoke,
            "request_json",
            side_effect=[TimeoutError("temporary startup timeout"), (200, {"ready": True})],
        ) as request_json:
            self.assertEqual(
                live_smoke.wait_for_state_api(
                    "http://127.0.0.1:1",
                    timeout_seconds=1,
                    retry_interval_seconds=0,
                ),
                {"ready": True},
            )
        self.assertEqual(request_json.call_count, 2)

    def test_polymarket_credential_runbook_is_documented_and_no_funded_actions(self) -> None:
        verify_source = (ROOT / "verify.py").read_text(encoding="utf-8")
        script_source = (ROOT / "scripts" / "verify_polymarket_credentials.py").read_text(encoding="utf-8")
        runbook_source = (ROOT / "polymarket" / "credential_runbook.py").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        docs = (ROOT / "docs" / "POLYMARKET_CREDENTIAL_RUNBOOK.md").read_text(encoding="utf-8")

        self.assertIn("run_polymarket_credential_runbook_check", verify_source)
        self.assertIn("verify_polymarket_credentials.py", verify_source)
        self.assertIn("build_polymarket_credential_runbook", script_source)
        self.assertIn("credential_runbook_no_funded_actions", runbook_source)
        self.assertIn("funded_execution_exposed", runbook_source)
        self.assertIn("network_calls", runbook_source)
        self.assertIn("POLY_API_SECRET", runbook_source)
        self.assertIn("RELAYER_API_KEY", runbook_source)
        self.assertIn("CONFIRM_LIVE_ORDER_CANCEL", runbook_source)
        self.assertIn("python scripts/verify_polymarket_credentials.py --json", readme)
        self.assertIn("--require-authenticated-read-ready", readme)
        self.assertIn("Funded order/cancel verification is separate", docs)

    def test_polymarket_live_report_promotion_guard_is_visible(self) -> None:
        live_reports_source = (ROOT / "polymarket" / "live_reports.py").read_text(encoding="utf-8")
        app_source = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
        type_source = (ROOT / "frontend" / "src" / "types.ts").read_text(encoding="utf-8")
        web_api_source = (ROOT / "web_api.py").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("live_validation_report_promotion", live_reports_source)
        self.assertIn("CREDENTIAL_PROMOTION_CHECKS", live_reports_source)
        self.assertIn("credential_live_verified", live_reports_source)
        self.assertIn("funded_live_verified", live_reports_source)
        self.assertIn("post_cancel_verified", live_reports_source)
        self.assertIn("live_validation_report_promotion_inventory", live_reports_source)
        self.assertIn("stored_live_validation_report_promotion", web_api_source)
        self.assertIn("Stage gates claim credentialed_read_ok", live_reports_source)
        self.assertIn("Credential tier", app_source)
        self.assertIn("Funded tier", app_source)
        self.assertIn("verification_promotion", type_source)
        self.assertIn("promotion guard", readme.lower())

    def test_polymarket_live_report_schema_is_documented_and_verified(self) -> None:
        schema_source = (ROOT / "polymarket" / "live_report_schema.py").read_text(encoding="utf-8")
        live_reports_source = (ROOT / "polymarket" / "live_reports.py").read_text(encoding="utf-8")
        web_api_source = (ROOT / "web_api.py").read_text(encoding="utf-8")
        verify_source = (ROOT / "verify.py").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        docs = (ROOT / "docs" / "POLYMARKET_LIVE_REPORT_SCHEMA.md").read_text(encoding="utf-8")
        fixture_root = ROOT / "tests" / "fixtures" / "polymarket" / "live_reports"

        self.assertIn("ACCEPTED_LIVE_VALIDATION_REPORT_MODES", schema_source)
        self.assertIn("credential_runbook_no_funded_actions", schema_source)
        self.assertIn("browser_smoke_seed", schema_source)
        self.assertIn("ensure_live_validation_report_valid", live_reports_source)
        self.assertIn("schema_validation", live_reports_source)
        self.assertIn("payload_hash", live_reports_source)
        self.assertIn("duplicate_imports", live_reports_source)
        self.assertIn("live_validation_report_review_bundle", live_reports_source)
        self.assertIn("live_validation_report_review_markdown", live_reports_source)
        self.assertIn("LiveValidationReportSchemaError", web_api_source)
        self.assertIn("polymarket_live_validation_report_review_payload", web_api_source)
        self.assertIn("/review.json", web_api_source)
        self.assertIn("/review.md", web_api_source)
        self.assertIn("live_validation_report_schema_error", web_api_source)
        self.assertIn("run_polymarket_live_report_schema_check", verify_source)
        self.assertIn("INVALID_REPORT", (ROOT / "scripts" / "verify_live_validation_report_smoke.py").read_text(encoding="utf-8"))
        self.assertIn("invalid_import_schema_error", (ROOT / "scripts" / "verify_live_validation_report_smoke.py").read_text(encoding="utf-8"))
        self.assertIn("POLYMARKET_LIVE_REPORT_SCHEMA.md", readme)
        self.assertIn("HTTP 400", docs)
        for name in (
            "valid_credentialed_read.json",
            "valid_funded_audit.json",
            "valid_dry_run.json",
            "valid_runbook.json",
            "valid_browser_smoke.json",
            "invalid_missing_mode.json",
            "invalid_bad_stage_gates.json",
        ):
            self.assertTrue((fixture_root / name).exists(), name)

    def test_polymarket_live_report_replay_cli_is_documented_and_verified(self) -> None:
        replay_source = (ROOT / "polymarket" / "live_report_replay.py").read_text(encoding="utf-8")
        script_source = (ROOT / "scripts" / "replay_polymarket_live_reports.py").read_text(encoding="utf-8")
        verify_source = (ROOT / "verify.py").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        docs = (ROOT / "docs" / "POLYMARKET_LIVE_REPORT_REPLAY.md").read_text(encoding="utf-8")

        self.assertIn("replay_live_validation_report_paths", replay_source)
        self.assertIn("store_live_validation_report", replay_source)
        self.assertIn("live_validation_report_summary", replay_source)
        self.assertIn("funded_execution_exposed", replay_source)
        self.assertIn("--import", script_source)
        self.assertIn("--allow-duplicate", script_source)
        self.assertIn("--skip-duplicates", script_source)
        self.assertIn("--fail-on-warning", script_source)
        self.assertIn("never performs funded actions", script_source)
        self.assertIn("find_live_validation_report_duplicate", replay_source)
        self.assertIn("source_file", replay_source)
        self.assertIn("run_polymarket_live_report_replay_check", verify_source)
        self.assertIn("replay_polymarket_live_reports.py", readme)
        self.assertIn("--allow-duplicate", readme)
        self.assertIn("dry-run", docs.lower())
        self.assertIn("Invalid reports are never stored", docs)
        self.assertIn("payload hash", docs.lower())
        self.assertIn("duplicate", docs.lower())

    def test_polymarket_live_report_review_bundle_is_documented_and_verified(self) -> None:
        live_reports_source = (ROOT / "polymarket" / "live_reports.py").read_text(encoding="utf-8")
        web_api_source = (ROOT / "web_api.py").read_text(encoding="utf-8")
        verify_source = (ROOT / "verify.py").read_text(encoding="utf-8")
        app_source = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
        api_source = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
        type_source = (ROOT / "frontend" / "src" / "types.ts").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        docs = (ROOT / "docs" / "POLYMARKET_LIVE_REPORT_REVIEW_BUNDLE.md").read_text(encoding="utf-8")

        self.assertIn("POLYMARKET_LIVE_VALIDATION_REPORT_REVIEW_KIND", live_reports_source)
        self.assertIn("live_validation_report_review_bundle", live_reports_source)
        self.assertIn("live_validation_report_review_markdown", live_reports_source)
        self.assertIn("static_coverage_mutated", live_reports_source)
        self.assertIn("operator_commands", live_reports_source)
        self.assertIn("coverage_tier_mapping", live_reports_source)
        self.assertIn("polymarket_live_validation_report_review_payload", web_api_source)
        self.assertIn("/review.json", web_api_source)
        self.assertIn("/review.md", web_api_source)
        self.assertIn("run_polymarket_live_report_review_bundle_check", verify_source)
        self.assertIn("Review JSON", app_source)
        self.assertIn("Review Markdown", app_source)
        self.assertIn("Promotion Decision Ledger", app_source)
        self.assertIn("polymarketLiveValidationReportReviewJsonUrl", api_source)
        self.assertIn("PolymarketLiveValidationReportReviewBundle", type_source)
        self.assertIn("POLYMARKET_LIVE_REPORT_REVIEW_BUNDLE.md", readme)
        self.assertIn("raw report payload is not included", docs.lower())
        self.assertIn("static_coverage_mutated", docs)

    def test_polymarket_live_report_decision_ledger_is_documented_and_verified(self) -> None:
        live_reports_source = (ROOT / "polymarket" / "live_reports.py").read_text(encoding="utf-8")
        web_api_source = (ROOT / "web_api.py").read_text(encoding="utf-8")
        verify_source = (ROOT / "verify.py").read_text(encoding="utf-8")
        app_source = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
        api_source = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
        type_source = (ROOT / "frontend" / "src" / "types.ts").read_text(encoding="utf-8")
        script_source = (ROOT / "scripts" / "review_polymarket_live_decisions.py").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        docs = (ROOT / "docs" / "POLYMARKET_LIVE_REPORT_DECISION_LEDGER.md").read_text(encoding="utf-8")

        self.assertIn("POLYMARKET_LIVE_VALIDATION_DECISION_KIND", live_reports_source)
        self.assertIn("record_live_validation_report_decision", live_reports_source)
        self.assertIn("review_bundle_hash mismatch", live_reports_source)
        self.assertIn("payload_hash mismatch", live_reports_source)
        self.assertIn("live_validation_report_decisions_markdown", live_reports_source)
        self.assertIn("polymarket_live_validation_decision_store_payload", web_api_source)
        self.assertIn("/api/polymarket/live-validation/decisions", web_api_source)
        self.assertIn("run_polymarket_live_report_decision_ledger_check", verify_source)
        self.assertIn("Promotion Decision Ledger", app_source)
        self.assertIn("Record Decision", app_source)
        self.assertIn("fetchPolymarketLiveValidationDecisions", api_source)
        self.assertIn("storePolymarketLiveValidationDecision", api_source)
        self.assertIn("PolymarketLiveValidationDecisionLedgerPayload", type_source)
        self.assertIn("--print-review-input", script_source)
        self.assertIn("--export-ledger", script_source)
        self.assertIn("POLYMARKET_LIVE_REPORT_DECISION_LEDGER.md", readme)
        self.assertIn("review-bundle hash", docs.lower())
        self.assertIn("static_coverage_mutated", docs)

    def test_polymarket_live_report_promotion_proposal_is_documented_and_verified(self) -> None:
        live_reports_source = (ROOT / "polymarket" / "live_reports.py").read_text(encoding="utf-8")
        web_api_source = (ROOT / "web_api.py").read_text(encoding="utf-8")
        verify_source = (ROOT / "verify.py").read_text(encoding="utf-8")
        app_source = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
        api_source = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
        type_source = (ROOT / "frontend" / "src" / "types.ts").read_text(encoding="utf-8")
        script_source = (ROOT / "scripts" / "review_polymarket_live_decisions.py").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        docs = (ROOT / "docs" / "POLYMARKET_LIVE_REPORT_PROMOTION_PROPOSAL.md").read_text(encoding="utf-8")

        self.assertIn("POLYMARKET_LIVE_VALIDATION_PROMOTION_PROPOSAL_KIND", live_reports_source)
        self.assertIn("live_validation_coverage_promotion_proposal", live_reports_source)
        self.assertIn("stale_decisions", live_reports_source)
        self.assertIn("automerge_enabled", live_reports_source)
        self.assertIn("polymarket_live_validation_promotion_proposal_payload", web_api_source)
        self.assertIn("/api/polymarket/live-validation/promotion-proposal", web_api_source)
        self.assertIn("run_polymarket_live_report_promotion_proposal_check", verify_source)
        self.assertIn("Proposal JSON", app_source)
        self.assertIn("Proposal Markdown", app_source)
        self.assertIn("polymarketLiveValidationPromotionProposalJsonUrl", api_source)
        self.assertIn("PolymarketLiveValidationPromotionProposalPayload", type_source)
        self.assertIn("--export-proposal", script_source)
        self.assertIn("POLYMARKET_LIVE_REPORT_PROMOTION_PROPOSAL.md", readme)
        self.assertIn("automerge_enabled=false", docs)
        self.assertIn("stale", docs.lower())

    def test_polymarket_live_report_promotion_proposal_preview_is_documented_and_smoked(self) -> None:
        app_source = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
        api_source = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
        style_source = (ROOT / "frontend" / "src" / "styles.css").read_text(encoding="utf-8")
        smoke_source = (ROOT / "scripts" / "verify_live_validation_report_smoke.py").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        docs = (ROOT / "docs" / "POLYMARKET_LIVE_REPORT_PROMOTION_PROPOSAL.md").read_text(encoding="utf-8")
        goal = (ROOT / "GOAL.md").read_text(encoding="utf-8")

        self.assertIn("PromotionProposalPreview", app_source)
        self.assertIn("Promotion Proposal Preview", app_source)
        self.assertIn("Refresh Proposal", app_source)
        self.assertIn("target_tier", app_source)
        self.assertIn("Accepted Candidates", app_source)
        self.assertIn("Stale Decisions", app_source)
        self.assertIn("Proposed Manual Changes", app_source)
        self.assertIn("no apply", docs)
        self.assertIn("polymarketLiveValidationPromotionProposalJsonUrl(targetTier", api_source)
        self.assertIn(".proposal-preview", style_source)
        self.assertIn("Promotion Proposal Preview", smoke_source)
        self.assertIn("Refresh Proposal", smoke_source)
        self.assertIn("Promotion Proposal Preview", readme)
        self.assertIn("Article 66", goal)

    def test_polymarket_live_report_promotion_proposal_snapshot_archive_is_documented_and_verified(self) -> None:
        live_reports_source = (ROOT / "polymarket" / "live_reports.py").read_text(encoding="utf-8")
        web_api_source = (ROOT / "web_api.py").read_text(encoding="utf-8")
        verify_source = (ROOT / "verify.py").read_text(encoding="utf-8")
        app_source = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
        api_source = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
        type_source = (ROOT / "frontend" / "src" / "types.ts").read_text(encoding="utf-8")
        smoke_source = (ROOT / "scripts" / "verify_live_validation_report_smoke.py").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        docs = (ROOT / "docs" / "POLYMARKET_LIVE_REPORT_PROMOTION_PROPOSAL.md").read_text(encoding="utf-8")
        goal = (ROOT / "GOAL.md").read_text(encoding="utf-8")

        self.assertIn("POLYMARKET_LIVE_VALIDATION_PROMOTION_PROPOSAL_SNAPSHOT_KIND", live_reports_source)
        self.assertIn("store_live_validation_coverage_promotion_proposal_snapshot", live_reports_source)
        self.assertIn("list_live_validation_coverage_promotion_proposal_snapshots", live_reports_source)
        self.assertIn("proposal_hash_mismatch", live_reports_source)
        self.assertIn("polymarket_live_validation_promotion_proposal_snapshot_store_payload", web_api_source)
        self.assertIn("/api/polymarket/live-validation/promotion-proposal/snapshots", web_api_source)
        self.assertIn("run_polymarket_live_report_promotion_proposal_snapshot_check", verify_source)
        self.assertIn("Proposal Snapshot Archive", app_source)
        self.assertIn("Save Snapshot", app_source)
        self.assertIn("Refresh Archive", app_source)
        self.assertIn("deletePolymarketLiveValidationPromotionProposalSnapshot", api_source)
        self.assertIn("PolymarketLiveValidationPromotionProposalSnapshotsPayload", type_source)
        self.assertIn("Proposal Snapshot Archive", smoke_source)
        self.assertIn("proposal snapshot", readme.lower())
        self.assertIn("Snapshot Archive", docs)
        self.assertIn("Article 67", goal)

    def test_polymarket_live_report_promotion_proposal_snapshot_diff_is_documented_and_verified(self) -> None:
        live_reports_source = (ROOT / "polymarket" / "live_reports.py").read_text(encoding="utf-8")
        web_api_source = (ROOT / "web_api.py").read_text(encoding="utf-8")
        verify_source = (ROOT / "verify.py").read_text(encoding="utf-8")
        app_source = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
        api_source = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
        type_source = (ROOT / "frontend" / "src" / "types.ts").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        docs = (ROOT / "docs" / "POLYMARKET_LIVE_REPORT_PROMOTION_PROPOSAL.md").read_text(encoding="utf-8")
        goal = (ROOT / "GOAL.md").read_text(encoding="utf-8")

        self.assertIn("live_validation_promotion_proposal_snapshot_diff", live_reports_source)
        self.assertIn("_promotion_proposal_count_diff", live_reports_source)
        self.assertIn("Current-vs-Snapshot Diff", live_reports_source)
        self.assertIn("/diff.json", web_api_source)
        self.assertIn("polymarket_live_validation_promotion_proposal_snapshot_diff_payload", web_api_source)
        self.assertIn("ProposalSnapshotDiffReview", app_source)
        self.assertIn("Diff Markdown", app_source)
        self.assertIn("polymarketLiveValidationPromotionProposalSnapshotDiffJsonUrl", api_source)
        self.assertIn("PolymarketLiveValidationPromotionProposalSnapshotDiff", type_source)
        self.assertIn("live_validation_promotion_proposal_snapshot_diff_markdown", verify_source)
        self.assertIn("current-versus-snapshot diff", readme.lower())
        self.assertIn("current-vs-snapshot diff", docs.lower())
        self.assertIn("Article 68", goal)


if __name__ == "__main__":
    unittest.main()
