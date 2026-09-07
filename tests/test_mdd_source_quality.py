from __future__ import annotations

import csv
import io
import json
import tempfile
import time
import unittest
from contextlib import ExitStack, closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import market_sentinel_cli as cli
import web_api
from polymarket import data_api, mdd
from polymarket.accounting import reconcile_mdd_payload_with_accounting
from polymarket.analytics_cache import mdd_payload_to_csv
from polymarket.http_client import PolymarketResponseError
from polymarket.leaderboard_state import LeaderboardStateStore


WALLET = "0x" + "1" * 40
CLOSE = {"timestamp": 100, "realizedPnl": -5, "totalBought": 100}
TRADE = {"timestamp": 100, "asset": "token", "side": "BUY", "size": 10, "price": 0.5}


class MddSourceQualityTests(unittest.TestCase):
    def inputs(self, closed=None, opened=None, trades=None, activity=None):
        return mdd.MddInputs(WALLET, [CLOSE] if closed is None else closed, opened or [], activity or [], trades or [])

    def result(self, **kwargs):
        return mdd.build_historical_mdd_payload(self.inputs(**kwargs), equity_base_usd=100)

    def assert_unknown(self, result, source, reason):
        self.assertFalse(result["mdd_available"])
        self.assertIsNone(result["mdd_usd"])
        self.assertIsNone(result["mdd_pct"])
        self.assertEqual(result["mdd_history_status"], "invalid_source_data")
        self.assertGreater(result["mdd_source_quality"]["sources"][source]["reasons"][reason], 0)
        self.assertIn(f"invalid_source_data:{source}:{reason}", result["mdd_unavailable_reasons"])
        json.dumps(result, allow_nan=False)

    def test_missing_timestamp_cannot_qualify_despite_a_valid_loss_row(self):
        self.assert_unknown(self.result(closed=[{"realizedPnl": 1000}, {"timestamp": 100, "realizedPnl": -200}]),
                            "closed_positions", "invalid_timestamp")

    def test_unparseable_pnl_cannot_turn_partial_observations_into_zero_risk(self):
        self.assert_unknown(self.result(closed=[CLOSE, {"timestamp": 200, "realizedPnl": "unparseable"}]),
                            "closed_positions", "invalid_realized_pnl")

    def test_invalid_timestamps_fail_without_truncation_or_epoch_fallback(self):
        for timestamp in (None, "", "bad", True, False, 0, -1, 1.5, "100.5", float("nan"), float("inf"),
                          10 ** 400, time.time() + 3600, {}, []):
            with self.subTest(timestamp=timestamp):
                self.assert_unknown(self.result(closed=[{**CLOSE, "timestamp": timestamp}]), "closed_positions", "invalid_timestamp")

    def test_exact_numeric_timestamp_strings_are_accepted(self):
        for timestamp in (100, 100.0, "100", "100.0"):
            with self.subTest(timestamp=timestamp):
                self.assertEqual(self.result(closed=[{**CLOSE, "timestamp": timestamp}])["mdd_pct"], 5)

    def test_invalid_pnl_and_boolean_values_are_not_coerced_to_valid_returns(self):
        for pnl in (None, "", "bad", True, float("nan"), float("inf"), float("-inf"), 10 ** 400, {}, []):
            with self.subTest(pnl=pnl):
                self.assert_unknown(self.result(closed=[{**CLOSE, "realizedPnl": pnl}]), "closed_positions", "invalid_realized_pnl")

    def test_invalid_optional_capital_is_not_hidden_by_another_base(self):
        for value in (-1, "bad", float("nan"), float("inf"), True, 10 ** 400):
            with self.subTest(value=value):
                self.assert_unknown(self.result(closed=[{**CLOSE, "totalBought": value}]), "closed_positions", "invalid_numeric_field")

    def test_malformed_open_pnl_is_not_hidden_by_a_valid_component(self):
        self.assert_unknown(self.result(opened=[{"cashPnl": "bad", "realizedPnl": 5}]), "open_positions", "invalid_numeric_field")
        self.assert_unknown(self.result(opened=[{"currentValue": 100}]), "open_positions", "invalid_open_pnl")

    def test_open_snapshot_does_not_require_a_historical_event_timestamp(self):
        result = self.result(opened=[{"cashPnl": -10, "realizedPnl": 0}])
        self.assertTrue(result["mdd_available"])
        self.assertEqual(result["mdd_pct"], 15)

    def test_nonobject_rows_cannot_be_dropped_from_a_supplied_history(self):
        for source, option in (("closed_positions", "closed"), ("open_positions", "opened"), ("trade_rows", "trades"), ("activity_events", "activity")):
            self.assert_unknown(self.result(**{option: [None]}), source, "invalid_row_shape")

    def test_data_api_rejects_wrong_envelopes_and_mixed_row_shapes(self):
        for getter in (data_api.get_closed_positions, data_api.get_positions, data_api.get_activity, data_api.get_trades):
            for raw in ({}, {"error": "unavailable"}, None, [CLOSE, None], ["invalid"]):
                with self.subTest(getter=getter.__name__, raw=raw), patch.object(data_api, "_get_json", return_value=raw):
                    with self.assertRaises(PolymarketResponseError):
                        getter(WALLET)
            with patch.object(data_api, "_get_json", return_value=[]):
                self.assertEqual(getter(WALLET), [])

    def test_opposite_signed_same_timestamp_closes_have_unknown_order(self):
        rows = [{"timestamp": 100, "realizedPnl": 1000}, {"timestamp": 100, "realizedPnl": -200}]
        for ordered in (rows, list(reversed(rows))):
            self.assert_unknown(self.result(closed=ordered), "closed_positions", "ambiguous_timestamp_order")

    def test_unambiguous_same_timestamp_gains_preserve_zero_observed_loss(self):
        self.assertEqual(self.result(closed=[{"timestamp": 100, "realizedPnl": 1}, {"timestamp": 100, "realizedPnl": 2}])["mdd_pct"], 0)

    def test_duplicate_identified_position_observations_are_not_double_counted(self):
        row = {**CLOSE, "asset": "token"}
        self.assert_unknown(self.result(closed=[row, dict(row)]), "closed_positions", "duplicate_position_observation")

    def test_trade_ambiguity_across_sources_is_not_resolved_by_source_order(self):
        self.assert_unknown(self.result(trades=[TRADE], activity=[{**TRADE, "side": "SELL", "type": "TRADE"}]),
                            "trade_rows", "ambiguous_timestamp_order")

    def test_invalid_trade_capital_and_time_prevent_fast_qualification(self):
        for row, reason in (({**TRADE, "timestamp": None}, "invalid_timestamp"), ({**TRADE, "size": -1}, "invalid_numeric_field"),
                            ({**TRADE, "size": 0}, "invalid_trade_notional"), ({**TRADE, "price": 2}, "invalid_probability_price"),
                            ({**TRADE, "side": "UNKNOWN"}, "invalid_trade_side")):
            self.assert_unknown(self.result(trades=[row]), "trade_rows", reason)

    def test_replay_does_not_request_prices_or_promote_invalid_source_data(self):
        with patch.object(mdd.clob_rest, "get_batch_price_history") as prices:
            result = mdd.build_mark_replay_mdd_payload(self.inputs(closed=[{"realizedPnl": 1000}, CLOSE], trades=[TRADE]), equity_base_usd=100)
        prices.assert_not_called()
        self.assert_unknown(result, "closed_positions", "invalid_timestamp")
        self.assertEqual(result["mark_replay"]["status"], "unavailable")
        self.assertIsNone(result["fallback_v2"]["mdd_pct"])

    def test_price_history_never_discards_an_invalid_point_to_keep_a_good_one(self):
        for invalid in (None, {}, {"t": 101, "p": "bad"}, {"t": 1.5, "p": 0.5}, {"t": 101, "p": True}):
            with patch.object(mdd.clob_rest, "get_batch_price_history", return_value={"history": {"token": [{"t": 100, "p": 0.5}, invalid]}}):
                with self.assertRaises(ValueError):
                    mdd.build_mark_replay_mdd_payload(self.inputs(trades=[TRADE]), equity_base_usd=100)

    def test_conflicting_same_timestamp_marks_fail_and_exact_duplicates_coalesce(self):
        with self.assertRaises(ValueError):
            mdd._normalize_price_history_points([{"t": 100, "p": 0.5}, {"t": 100, "p": 0.2}])
        self.assertEqual(len(mdd._normalize_price_history_points([{"t": 100, "p": 0.5}] * 2)), 1)

    def test_accounting_does_not_rebase_or_promote_invalid_history(self):
        result = reconcile_mdd_payload_with_accounting(self.result(closed=[{"realizedPnl": 1000}, CLOSE]),
                                                       {"status": "ok", "complete": True, "equity": {"base_equity_usd": 100000}})
        self.assert_unknown(result, "closed_positions", "invalid_timestamp")
        self.assertFalse(result["accounting_snapshot"]["reconciliation"]["mdd_pct_uses_accounting_base"])

    def test_actual_fetch_and_filter_exclude_invalid_source_rows(self):
        with ExitStack() as stack:
            stack.enter_context(patch.object(data_api, "get_closed_positions", return_value=[{"realizedPnl": 1000}, CLOSE]))
            for method in ("get_positions", "get_activity", "get_trades"):
                stack.enter_context(patch.object(data_api, method, return_value=[]))
            stack.enter_context(patch.object(data_api, "get_leaderboard", return_value=[{"proxyWallet": WALLET, "pnl": 100, "vol": 10}]))
            stack.enter_context(patch.object(web_api, "attach_polymarket_mdd_audit_cache", return_value={}))
            result = web_api.polymarket_leaderboard_payload({"max_mdd_pct": ["20"], "equity_base_usd": ["100"]})
        self.assertEqual(result["counts"]["returned"], 0)
        self.assertEqual(result["counts"]["mdd_qualified"], 0)
        self.assertEqual(result["counts"]["mdd_unavailable"], 1)

    def test_state_resume_and_both_csv_formats_keep_unknown_risk_and_reasons(self):
        result = self.result(closed=[{"realizedPnl": 1000}, CLOSE])
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state.db"
            with closing(LeaderboardStateStore(state)) as store:
                store.prepare({}, resume=False)
                store.record_page(0, 50, [{"wallet": WALLET, "rank": 1, "roi_pct": 10}])
                row = next(store.iter_results({}, require_mdd=False, sort="roi_pct", direction="DESC", limit=None))
                store.set_mdd(row["id"], result)
            with closing(LeaderboardStateStore(state)) as store:
                self.assertEqual(store.result_count({"max_mdd_pct": 20}, require_mdd=True), 0)
                restored = next(store.iter_results({}, require_mdd=False, sort="roi_pct", direction="DESC", limit=None))
            self.assertEqual(restored["mdd_source_quality"], result["mdd_source_quality"])
            csv_row = next(cli._csv_rows([restored]))
            self.assertEqual(json.loads(csv_row["mdd_source_quality"]), result["mdd_source_quality"])
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["leaderboard-export", "--state-db", str(state), "--require-mdd", "--format", "csv"]), 0)
            self.assertEqual(len(list(csv.DictReader(io.StringIO(output.getvalue())))), 0)
        audit = next(csv.DictReader(io.StringIO(mdd_payload_to_csv(result))))
        self.assertEqual(audit["status"], "unavailable")
        self.assertEqual(json.loads(audit["mdd_source_quality"]), result["mdd_source_quality"])


if __name__ == "__main__":
    unittest.main()
