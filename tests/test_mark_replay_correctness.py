from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import market_sentinel_cli
import web_api
from polymarket.accounting import reconcile_mdd_payload_with_accounting
from polymarket.drawdown import max_drawdown
from polymarket.leaderboard_state import LeaderboardStateStore
from polymarket.mdd import MddInputs, _build_mark_replay_points, build_mark_replay_mdd_payload


WALLET = "0x" + "a" * 40


def trade(timestamp, side, price, size=100, token="token"):
    return {"timestamp": timestamp, "asset": token, "side": side, "price": price, "size": size}


class MarkReplayCorrectnessTests(unittest.TestCase):
    def setUp(self):
        self.inputs = MddInputs(WALLET, [], [], [], [trade(2, "BUY", 1), trade(4, "SELL", 0.5)])
        self.history = {"history": {"token": [
            {"t": timestamp, "p": 1} for timestamp in range(1, 6003) if timestamp not in (2, 4)
        ]}}

    def build(self, inputs=None, history=None, **kwargs):
        with patch("polymarket.mdd.clob_rest.get_batch_price_history", return_value=self.history if history is None else history):
            return build_mark_replay_mdd_payload(
                inputs or self.inputs, equity_base_usd=100,
                mark_replay_start_ts=1, mark_replay_end_ts=6002, **kwargs,
            )

    def test_point_limits_never_drop_trades_or_drawdown(self):
        for limit in (1, 2, 3, 5000, 10000):
            with self.subTest(limit=limit):
                result = self.build(mark_replay_point_limit=limit)
                self.assertEqual(result["mdd_pct"], 50)
                self.assertEqual(result["mdd_usd"], 50)
                self.assertEqual(result["points_total"], 6002)
                self.assertLessEqual(len(result["points"]), min(limit, 50))
                self.assertEqual(result["mark_replay"]["trade_events_replayed"], 2)
                self.assertEqual(result["mark_replay"]["status"], "ok")
                self.assertFalse(result["mark_replay"]["timeline_truncated"])
                self.assertTrue(result["mark_replay"]["display_points_truncated"])

    def test_mark_valley_remains_in_calculation_when_not_retained(self):
        data = MddInputs(WALLET, [], [], [], [trade(1, "BUY", 1)])
        history = {"history": {"token": [{"t": 2, "p": 0.2}, {"t": 3, "p": 1}]}}
        result = self.build(inputs=data, history=history, mark_replay_point_limit=1, max_points=1)
        self.assertEqual(result["mdd_pct"], 80)
        self.assertEqual(result["points"][0]["value"], 0)
        self.assertEqual(result["pct_trough_timestamp"], 2)
        reconciled = reconcile_mdd_payload_with_accounting(result, {"equity": {"base_equity_usd": 200}})
        self.assertEqual(reconciled["mdd_pct"], 80)

    def test_drawdown_accepts_one_pass_and_empty_iterators(self):
        self.assertEqual(max_drawdown(iter([{"value": -20}]), 100)["mdd_pct"], 20)
        empty = max_drawdown(iter(()), 100)
        self.assertFalse(empty["mdd_available"])
        self.assertIsNone(empty["mdd_pct"])

    def test_inventory_cash_and_same_timestamp_order_are_preserved(self):
        result = _build_mark_replay_points(
            [trade(1, "BUY", 0.5, 20), trade(3, "SELL", 0.8, 5)],
            {"token": [{"timestamp": 1, "price": 0.4}, {"timestamp": 2, "price": 0.2}]},
            point_limit=1, equity_base_usd=10,
        )
        self.assertEqual(result["trade_events_replayed"], 2)
        self.assertEqual(result["final_inventory"], {"token": 15})
        self.assertEqual(result["points"][0]["cash_flow_usd"], -6)
        self.assertEqual(result["points"][0]["marked_inventory_value_usd"], 12)
        self.assertEqual(result["drawdown"]["mdd_pct"], 60)

    def test_negative_inventory_returns_unknown_not_zero_risk(self):
        data = MddInputs(WALLET, [], [], [], [trade(1, "SELL", 0.5)])
        result = self.build(inputs=data)
        self.assertEqual(result["mark_replay"]["status"], "partial")
        self.assertIn("negative_inventory_events", result["mark_replay"]["incomplete_reasons"])
        self.assertFalse(result["mdd_available"])
        self.assertIsNone(result["mdd_pct"])
        self.assertIn("observed_drawdown", result["mark_replay"])

    def test_invalid_trade_sources_stop_before_replay_with_diagnostics(self):
        invalid_rows = [
            {**trade(3, "BUY", 0.5), "timestamp": None},
            trade(3, "INVALID", 0.5), trade(3, "BUY", None),
            trade(3, "BUY", 2), trade(3, "BUY", 0.5, size=0),
        ]
        for invalid in invalid_rows:
            with self.subTest(invalid=invalid):
                data = MddInputs(WALLET, [], [], [], [trade(2, "BUY", 1), invalid])
                with patch("polymarket.mdd.clob_rest.get_batch_price_history") as fetch_prices:
                    result = build_mark_replay_mdd_payload(data, equity_base_usd=100)
                fetch_prices.assert_not_called()
                self.assertFalse(result["mdd_available"])
                self.assertIsNone(result["mdd_pct"])
                self.assertIsNone(result["mdd_usd"])
                self.assertEqual(result["mark_replay"]["trade_events_replayed"], 0)
                self.assertEqual(result["mark_replay"]["status"], "unavailable")
                source = result["mdd_source_quality"]["sources"]["trade_rows"]
                self.assertEqual(source["rows"], 2)
                self.assertEqual(source["invalid_rows"], 1)
                self.assertTrue(source["reasons"])
                self.assertTrue(result["mark_replay"]["incomplete_reasons"])
                self.assertIsNone(result["fallback_v2"]["mdd_pct"])

    def test_missing_token_allows_diagnostic_replay_but_not_risk_qualification(self):
        data = MddInputs(WALLET, [], [], [], [trade(2, "BUY", 1), trade(3, "BUY", 0.5, token="")])
        result = self.build(inputs=data)
        self.assertFalse(result["mdd_available"])
        self.assertIsNone(result["mdd_pct"])
        self.assertEqual(result["mark_replay"]["trade_events_replayed"], 1)
        self.assertEqual(result["mark_replay"]["status"], "partial")
        self.assertTrue(result["mark_replay"]["incomplete_reasons"])

    def test_missing_history_and_clipped_tokens_are_not_qualified_risk(self):
        data = MddInputs(WALLET, [], [], [], [trade(1, "BUY", 0.5), trade(2, "BUY", 0.5, token="second")])
        for kwargs in ({"history": {}}, {"mark_replay_token_limit": 1}):
            with self.subTest(kwargs=kwargs):
                result = self.build(inputs=data, **kwargs)
                self.assertFalse(result["mdd_available"])
                self.assertIsNone(result["mdd_pct"])
                self.assertEqual(result["mark_replay"]["status"], "partial")

    def test_unavailable_replay_does_not_silently_qualify_fast_fallback(self):
        data = MddInputs(WALLET, [{"timestamp": 1, "realizedPnl": 1}], [], [], [])
        result = self.build(inputs=data)
        self.assertEqual(result["mark_replay"]["status"], "unavailable")
        self.assertEqual(result["fallback_v2"]["mdd_pct"], 0)
        self.assertFalse(result["mdd_available"])
        self.assertIsNone(result["mdd_pct"])

    def test_history_transport_failure_preserves_diagnostics_not_risk(self):
        with patch("polymarket.mdd.clob_rest.get_batch_price_history", side_effect=OSError("offline")):
            result = build_mark_replay_mdd_payload(self.inputs, equity_base_usd=100)
        self.assertEqual(result["mark_replay"]["status"], "unavailable")
        self.assertFalse(result["mdd_available"])

    def test_accounting_cannot_promote_incomplete_replay(self):
        data = MddInputs(WALLET, [], [], [], [trade(1, "SELL", 0.5)])
        result = self.build(inputs=data, max_points=1000)
        result = reconcile_mdd_payload_with_accounting(result, {"equity": {"base_equity_usd": 200}})
        self.assertFalse(result["mdd_available"])
        self.assertIsNone(result["mdd_pct"])
        self.assertFalse(result["accounting_snapshot"]["reconciliation"]["mdd_pct_uses_accounting_base"])

    def test_invalid_mark_prices_fail_closed(self):
        for price in (float("nan"), float("inf"), -1, 2):
            with self.subTest(price=price), self.assertRaises(ValueError):
                self.build(history={"history": {"token": [{"t": 3, "p": price}]}})

    def test_api_maximum_mdd_rejects_default_limit_replay_loss(self):
        with patch("web_api.data_api.get_leaderboard", return_value=[{"proxyWallet": WALLET, "pnl": 10, "vol": 100}]), patch(
            "polymarket.mdd.fetch_mdd_inputs", return_value=self.inputs
        ), patch("polymarket.mdd.clob_rest.get_batch_price_history", return_value=self.history), patch(
            "web_api.attach_polymarket_mdd_audit_cache", return_value={}
        ):
            result = web_api.polymarket_leaderboard_payload({
                "mdd_mode": ["mark_replay"], "max_mdd_pct": ["20"], "equity_base_usd": ["100"],
            })
        self.assertEqual(result["counts"]["returned"], 0)
        self.assertEqual(result["counts"]["mdd_computed"], 1)

    def test_cli_filter_and_resume_preserve_correct_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state.sqlite3"
            output = Path(temporary) / "result.json"
            args = ["polymarket-leaderboard", "--state-db", str(state), "--scanned", "unlimited",
                    "--returned", "unlimited", "--mdd-mode", "mark_replay", "--max-mdd-pct", "20",
                    "--equity-base-usd", "100", "--format", "json", "--output", str(output), "--quiet"]
            with patch("web_api.data_api.get_leaderboard", return_value=[{"proxyWallet": WALLET, "pnl": 10, "vol": 100}]), patch(
                "polymarket.mdd.fetch_mdd_inputs", return_value=self.inputs
            ), patch("polymarket.mdd.clob_rest.get_batch_price_history", return_value=self.history), patch(
                "market_sentinel_cli.attach_polymarket_mdd_audit_cache", return_value={}
            ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(market_sentinel_cli.main(args), 0)
            self.assertEqual(json.loads(output.read_text())["counts"]["returned"], 0)
            with patch("web_api.data_api.get_leaderboard") as page, patch("market_sentinel_cli.polymarket_user_mdd_payload") as mdd:
                self.assertEqual(market_sentinel_cli.main(args + ["--resume"]), 0)
            page.assert_not_called()
            mdd.assert_not_called()

    def test_durable_state_does_not_qualify_explicit_unknown_risk(self):
        with tempfile.TemporaryDirectory() as temporary:
            with closing(LeaderboardStateStore(Path(temporary) / "state.sqlite3")) as store:
                store.prepare({}, resume=False)
                store.record_page(0, 1, [{"wallet": WALLET, "rank": 1, "roi_pct": 10}])
                row = next(store.iter_results({}, require_mdd=False, sort="roi_pct", direction="DESC", limit=None))
                store.set_mdd(row["id"], {"mdd_available": False, "mdd_pct": 0, "mdd_usd": 0,
                    "mark_replay": {"status": "partial", "incomplete_reasons": ["negative_inventory_events"]}})
                self.assertEqual(store.result_count({}, require_mdd=True), 0)
                row = next(store.iter_results({}, require_mdd=False, sort="roi_pct", direction="DESC", limit=None))
                self.assertFalse(row["mdd_available"])
                self.assertIsNone(row["mdd_pct"])
                self.assertEqual(row["mark_replay"]["incomplete_reasons"], ["negative_inventory_events"])


if __name__ == "__main__":
    unittest.main()
