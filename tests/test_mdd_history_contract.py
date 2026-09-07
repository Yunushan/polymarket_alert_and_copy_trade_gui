from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from contextlib import ExitStack, closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import app
import market_sentinel_cli as cli
import web_api
from polymarket import mdd
from polymarket.accounting import reconcile_mdd_payload_with_accounting
from polymarket.leaderboard_state import LeaderboardStateStore


WALLET = "0x" + "1" * 40
CLOSE = {"timestamp": 100, "realizedPnl": -5, "totalBought": 100}
TRADE = {"timestamp": 100, "asset": "token", "side": "BUY", "size": 10, "price": 0.5}
ROWS = {"closed_positions": [CLOSE], "open_positions": [{"timestamp": 200, "cashPnl": 0}],
        "activity_events": [dict(TRADE, type="TRADE")], "trade_rows": [TRADE]}
LIMITS = {"closed_positions": "closed_limit", "open_positions": "open_limit",
          "activity_events": "activity_limit", "trade_rows": "trade_limit"}


class MddHistoryContractTests(unittest.TestCase):
    def setUp(self):
        mdd.clear_mdd_input_cache()
        self.addCleanup(mdd.clear_mdd_input_cache)

    def fetch(self, **options):
        with ExitStack() as stack:
            for name, rows in ROWS.items():
                stack.enter_context(patch.object(mdd, "_fetch_" + name, return_value=rows))
            return mdd.fetch_mdd_inputs(WALLET, **{
                **{option: 2 for option in LIMITS.values()}, **options,
            })

    def test_each_full_source_budget_invalidates_risk_without_erasing_diagnostics(self):
        for source, option in LIMITS.items():
            with self.subTest(source=source):
                payload = mdd.build_historical_mdd_payload(self.fetch(**{option: 1}), equity_base_usd=100)
                self.assertFalse(payload["mdd_available"])
                self.assertIsNone(payload["mdd_pct"])
                self.assertEqual(payload["observed_drawdown"]["mdd_pct"], 5)
                self.assertEqual(payload["mdd_history_capped_sources"], [source])
                self.assertEqual(payload["mdd_history_coverage"][source]["status"], "limit_reached")

    def test_short_source_windows_are_not_verified_account_history(self):
        payload = mdd.build_historical_mdd_payload(self.fetch(), equity_base_usd=100)
        self.assertTrue(payload["mdd_available"])
        self.assertEqual(payload["mdd_pct"], 5)
        self.assertEqual(payload["mdd_scope"], "observed_public_pnl")
        self.assertFalse(payload["mdd_account_equity_verified"])
        self.assertEqual(payload["mdd_history_status"], "source_window_exhausted")
        self.assertEqual(payload["mdd_history_coverage"]["closed_positions"]["first_timestamp"], 100)

    def test_no_source_fetch_invents_complete_account_history(self):
        inputs = mdd.MddInputs(WALLET, [CLOSE], [], [], [])
        payload = mdd.build_historical_mdd_payload(inputs, equity_base_usd=100)
        self.assertEqual(payload["mdd_history_status"], "supplied_rows_unverified")
        self.assertFalse(payload["mdd_account_equity_verified"])

    def test_excluded_open_positions_are_reported(self):
        payload = mdd.build_historical_mdd_payload(self.fetch(include_open=False), equity_base_usd=100)
        self.assertEqual(payload["mdd_history_status"], "sources_excluded")
        self.assertEqual(payload["mdd_history_excluded_sources"], ["open_positions"])
        self.assertEqual(payload["mdd_history_coverage"]["open_positions"]["returned"], 0)

    def test_coverage_survives_cache_and_returned_mutation_is_isolated(self):
        inputs = self.fetch(closed_limit=1, cache_ttl_seconds=60)
        inputs.history_coverage["closed_positions"]["status"] = "corrupted"
        with patch.object(mdd, "_fetch_closed_positions") as fetch:
            restored = mdd.fetch_mdd_inputs(WALLET, closed_limit=1, open_limit=2, activity_limit=2, trade_limit=2, cache_ttl_seconds=60)
        fetch.assert_not_called()
        self.assertTrue(restored.cache_hit)
        self.assertEqual(restored.history_coverage["closed_positions"]["status"], "limit_reached")

    def test_actual_pagination_cap_and_short_page_are_distinguished(self):
        with patch.object(mdd.data_api, "get_closed_positions", return_value=[CLOSE]), patch.object(
            mdd.data_api, "get_positions", return_value=[]
        ), patch.object(mdd.data_api, "get_activity", return_value=[]), patch.object(mdd.data_api, "get_trades", return_value=[]):
            capped = mdd.fetch_mdd_inputs(WALLET, closed_limit=1)
            exhausted = mdd.fetch_mdd_inputs(WALLET, closed_limit=2)
        self.assertEqual(capped.history_coverage["closed_positions"]["status"], "limit_reached")
        self.assertEqual(exhausted.history_coverage["closed_positions"]["status"], "end_of_results")

    def test_accounting_cannot_promote_a_capped_result(self):
        payload = mdd.build_historical_mdd_payload(self.fetch(closed_limit=1), equity_base_usd=100)
        result = reconcile_mdd_payload_with_accounting(payload, {
            "status": "ok", "complete": True, "equity": {"base_equity_usd": 10000}, "positions": {},
        })
        self.assertFalse(result["mdd_available"])
        self.assertIsNone(result["mdd_pct"])
        self.assertFalse(result["accounting_snapshot"]["reconciliation"]["mdd_pct_uses_accounting_base"])

    def test_successful_mark_replay_does_not_promote_capped_inputs(self):
        with patch.object(mdd.clob_rest, "get_batch_price_history", return_value={"history": {"token": [{"t": 101, "p": 0.5}]}}):
            result = mdd.build_mark_replay_mdd_payload(self.fetch(trade_limit=1), equity_base_usd=100)
        self.assertFalse(result["mdd_available"])
        self.assertIsNone(result["mdd_pct"])
        self.assertEqual(result["mark_replay"]["status"], "partial")
        self.assertIn("history_limit_reached:trade_rows", result["mark_replay"]["incomplete_reasons"])

    def test_api_filter_excludes_capped_result_and_reports_reason_count(self):
        with patch.object(web_api.data_api, "get_leaderboard", return_value=[{"proxyWallet": WALLET, "pnl": 10, "vol": 100}]), patch.object(
            mdd, "fetch_mdd_inputs", return_value=self.fetch(closed_limit=1)
        ), patch.object(web_api, "attach_polymarket_mdd_audit_cache", return_value={}):
            result = web_api.polymarket_leaderboard_payload({"max_mdd_pct": ["20"], "equity_base_usd": ["100"]})
        self.assertEqual(result["counts"]["returned"], 0)
        self.assertEqual(result["counts"]["mdd_history_limited"], 1)
        self.assertEqual(result["counts"]["mdd_qualified"], 0)
        self.assertTrue(any("reached a history limit" in warning for warning in result["warnings"]))

    def test_sqlite_round_trip_and_csv_preserve_provenance(self):
        with tempfile.TemporaryDirectory() as temporary, closing(LeaderboardStateStore(Path(temporary) / "state.db")) as store:
            store.prepare({}, resume=False)
            row = web_api.normalize_polymarket_leaderboard_row({"proxyWallet": WALLET, "pnl": 10, "vol": 100}, 1)
            store.record_page(0, 1, [row])
            saved = next(store.iter_results({}, require_mdd=False, sort="roi_pct", direction="DESC", limit=None))
            store.set_mdd(saved["id"], mdd.build_historical_mdd_payload(self.fetch(closed_limit=1), equity_base_usd=100))
            self.assertEqual(store.result_count({"max_mdd_pct": 20}, require_mdd=True), 0)
            self.assertEqual(store.status()["mdd_available"], 0)
            self.assertEqual(store.status()["mdd_unavailable"], 1)
            exported = next(store.iter_results({}, require_mdd=False, sort="roi_pct", direction="DESC", limit=None))
            self.assertEqual(exported["pnl_volume_pct"], 10)
            self.assertIn("not return on invested capital", exported["roi_pct_basis"])
            self.assertEqual(exported["mdd_history_status"], "limit_reached")
            output = Path(temporary) / "result.csv"
            cli._write_streamed_leaderboard_payload({}, [exported], output_format="csv", output=str(output))
            with output.open(newline="") as stream:
                csv_row = next(csv.DictReader(stream))
            self.assertEqual(json.loads(csv_row["mdd_history_coverage"])["closed_positions"]["limit"], 1)
            self.assertEqual(csv_row["mdd_pct"], "")

    def test_cli_resume_keeps_unknown_risk_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "result.json"
            args = ["polymarket-leaderboard", "--state-db", str(Path(temporary) / "state.db"),
                    "--max-mdd-pct", "20", "--format", "json", "--output", str(output), "--quiet"]
            with patch.object(web_api.data_api, "get_leaderboard", return_value=[{"proxyWallet": WALLET, "pnl": 10, "vol": 100}]), patch.object(
                mdd, "fetch_mdd_inputs", return_value=self.fetch(closed_limit=1)
            ), patch.object(cli, "attach_polymarket_mdd_audit_cache", return_value={}), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(args), 0)
            with patch.object(cli, "polymarket_user_mdd_payload") as compute, patch.object(web_api.data_api, "get_leaderboard") as fetch:
                self.assertEqual(cli.main(args + ["--resume"]), 0)
            compute.assert_not_called()
            fetch.assert_not_called()
            result = json.loads(output.read_text())
            self.assertEqual(result["counts"]["returned"], 0)
            self.assertFalse(result["mdd_available"])

    def test_legacy_sort_keys_remain_compatible_with_new_metric_labels(self):
        self.assertEqual(app.App._leaderboard_sort_value("PnL/volume %"), "roi_pct")
        self.assertEqual(app.App._leaderboard_sort_value("ROI %"), "roi_pct")
        self.assertEqual(app.App._leaderboard_sort_value("Obs. MDD %"), "mdd_pct")
        for command in ("polymarket-leaderboard", "polymarket-leaderboard-export"):
            with redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as exited:
                cli.main([command, "--help"])
            self.assertEqual(exited.exception.code, 0)
            self.assertIn("not investment ROI", output.getvalue())


if __name__ == "__main__":
    unittest.main()
