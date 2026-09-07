from __future__ import annotations

import unittest
import csv
import io
import json
import tempfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import market_sentinel_cli as cli
import web_api
from polymarket import data_api, mdd
from polymarket.analytics_cache import mdd_payload_to_csv
from polymarket.leaderboard_state import LeaderboardStateStore


WALLET = "0x" + "c" * 40


class PositionCapitalBasisTests(unittest.TestCase):
    def payload(self, *, closed=(), opened=()):
        return mdd.build_historical_mdd_payload(mdd.MddInputs(WALLET, list(closed), list(opened), [], []))

    def test_closed_share_quantity_is_priced_before_becoming_dollars(self):
        result = self.payload(closed=[{"timestamp": 100, "realizedPnl": -10, "totalBought": 100, "avgPrice": 0.25}])
        self.assertEqual(result["closed_capital_basis_usd"], 25)
        self.assertEqual(result["mdd_pct"], 40)
        self.assertFalse(result["mdd_account_equity_verified"])

    def test_quantity_or_market_value_alone_does_not_establish_cost(self):
        for cost in ({"totalBought": 100}, {"currentValue": 1000}, {"avgPrice": 0.5}):
            with self.subTest(cost=cost):
                result = self.payload(closed=[{"timestamp": 100, "realizedPnl": -10, **cost}])
                self.assertIsNone(result["equity_base_usd"])
                self.assertIsNone(result["mdd_pct"])
                self.assertEqual(result["mdd_usd"], 10)
                self.assertEqual(result["position_capital_basis"]["closed_unknown_rows"], 1)

    def test_open_position_uses_remaining_gross_cost_without_adding_fees_twice(self):
        result = self.payload(opened=[{
            "cashPnl": -5, "totalBought": 1000, "size": 80, "avgPrice": 0.25,
            "initialValue": 20, "grossInitialValue": 21, "entryFeesUsdc": 1, "currentValue": 15,
        }])
        self.assertEqual(result["open_capital_basis_usd"], 21)
        self.assertAlmostEqual(result["mdd_pct"], 5 / 21 * 100)
        self.assertEqual(result["position_capital_basis"]["sources"], {"gross_initial_value": 1})
        self.assertFalse(result["position_capital_basis"]["account_fees_verified"])

    def test_open_position_falls_back_to_reported_cost_and_observed_fees(self):
        for cost in ({"initialValue": 20}, {"size": 80, "avgPrice": 0.25}):
            with self.subTest(cost=cost):
                result = self.payload(opened=[{"cashPnl": -5, "totalBought": 1000, "entryFeesUsdc": 1, **cost}])
                self.assertEqual(result["open_capital_basis_usd"], 21)

    def test_missing_entry_fee_is_not_reported_as_a_known_zero_fee(self):
        result = self.payload(opened=[{"cashPnl": -5, "initialValue": 20}])
        zero = self.payload(opened=[{"cashPnl": -5, "initialValue": 20, "entryFeesUsdc": 0}])
        self.assertEqual(result["open_capital_basis_usd"], 20)
        self.assertNotEqual(result["position_capital_basis"]["sources"], zero["position_capital_basis"]["sources"])

    def test_zero_gross_cost_does_not_fall_through_to_quantity_or_market_value(self):
        result = self.payload(opened=[{"cashPnl": -1, "grossInitialValue": 0, "totalBought": 100, "currentValue": 50}])
        self.assertIsNone(result["equity_base_usd"])
        self.assertIsNone(result["mdd_pct"])

    def test_fee_fields_are_validated_even_with_explicit_user_capital(self):
        for field in ("grossInitialValue", "entryFeesUsdc", "gross_initial_value", "entry_fees_usdc"):
            for value in (True, "bad", -1, float("nan"), float("inf")):
                with self.subTest(field=field, value=value):
                    inputs = mdd.MddInputs(WALLET, [], [{"cashPnl": -5, field: value}], [], [])
                    result = mdd.build_historical_mdd_payload(inputs, equity_base_usd=100)
                    self.assertFalse(result["mdd_available"])
                    self.assertEqual(result["mdd_history_status"], "invalid_source_data")

    def test_contradictory_fee_breakdown_cannot_establish_risk(self):
        for cost in (
            {"grossInitialValue": 1, "entryFeesUsdc": 2},
            {"grossInitialValue": 21, "initialValue": 20, "entryFeesUsdc": 2},
            {"grossInitialValue": 19, "initialValue": 20},
        ):
            with self.subTest(cost=cost):
                result = self.payload(opened=[{"cashPnl": -5, **cost}])
                self.assertFalse(result["mdd_available"])

    def test_six_decimal_rounding_does_not_reject_a_consistent_breakdown(self):
        result = self.payload(opened=[{
            "cashPnl": -0.1, "grossInitialValue": 0.333334,
            "initialValue": 0.333333, "entryFeesUsdc": 0.000002,
        }])
        self.assertTrue(result["mdd_available"])
        self.assertEqual(result["open_capital_basis_usd"], 0.333334)

    def test_api_risk_filter_excludes_share_count_denominator_false_positive(self):
        inputs = mdd.MddInputs(WALLET, [
            {"timestamp": 100, "realizedPnl": -10, "totalBought": 100, "avgPrice": 0.25},
        ], [], [], [])
        with patch.object(data_api, "get_leaderboard", return_value=[{"proxyWallet": WALLET, "pnl": 10, "vol": 100}]), patch.object(
            mdd, "fetch_mdd_inputs", return_value=inputs
        ), patch("web_api.attach_polymarket_mdd_audit_cache", return_value={}):
            result = web_api.polymarket_leaderboard_payload({"max_mdd_pct": ["20"]})
            permitted = web_api.polymarket_leaderboard_payload({"max_mdd_pct": ["50"]})
        self.assertEqual(result["counts"]["mdd_computed"], 1)
        self.assertEqual(result["counts"]["returned"], 0)
        self.assertEqual(permitted["rows"][0]["position_capital_basis"]["unit"], "USD")

    def test_corrected_basis_invalidates_version_six_results(self):
        self.assertGreater(mdd.MDD_CALCULATION_VERSION, 6)

    def test_snake_case_cost_aliases_and_numeric_strings_are_supported(self):
        result = self.payload(opened=[{
            "cash_pnl": "-5", "initial_value": "20", "gross_initial_value": "21", "entry_fees_usdc": "1",
        }])
        self.assertEqual(result["open_capital_basis_usd"], 21)

    def test_derived_cost_overflow_cannot_establish_risk(self):
        result = self.payload(opened=[{"cashPnl": -5, "initialValue": 1e308, "entryFeesUsdc": 1e308}])
        self.assertFalse(result["mdd_available"])

    def test_cost_provenance_survives_reopened_scan_and_csv_exports(self):
        payload = self.payload(closed=[{"timestamp": 100, "realizedPnl": -10, "totalBought": 100, "avgPrice": 0.25}])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scan.sqlite3"
            with closing(LeaderboardStateStore(path)) as store:
                store.prepare({"period": "all"}, resume=False)
                store.record_page(0, 1, [{"wallet": WALLET, "rank": 1, "pnl_usd": 10, "volume_usd": 100, "roi_pct": 10}])
                store.prepare_mdd({"calculation_version": mdd.MDD_CALCULATION_VERSION})
                candidate = next(store.iter_mdd_candidates({}, sort="roi_pct", direction="DESC", limit=None))
                store.set_mdd(candidate["id"], payload)
            with closing(LeaderboardStateStore(path)) as store:
                row = next(store.iter_results({}, sort="roi_pct", direction="DESC", limit=None, require_mdd=True))
                self.assertEqual(row["position_capital_basis"], payload["position_capital_basis"])
                exported = next(cli._csv_rows([row]))
                self.assertEqual(json.loads(exported["position_capital_basis"]), payload["position_capital_basis"])
                store.prepare_mdd({"calculation_version": 6})
                store.set_mdd(candidate["id"], {"calculation_version": 6, "mdd_pct": 10, "mdd_usd": 10})
                self.assertEqual(store.prepare_mdd({"calculation_version": mdd.MDD_CALCULATION_VERSION}), 1)
                self.assertEqual(store.progress()["rows"], 1)
        summary = next(csv.DictReader(io.StringIO(mdd_payload_to_csv(payload))))
        self.assertEqual(json.loads(summary["position_capital_basis"]), payload["position_capital_basis"])


class PositionQueryScopeTests(unittest.TestCase):
    def test_mdd_requests_dust_and_archived_active_positions_on_every_page(self):
        row = {"cashPnl": -0.1}
        with patch.object(data_api, "get_positions", side_effect=[[row] * 500, [row]]) as fetch:
            self.assertEqual(len(mdd._fetch_open_positions(WALLET, 1000)), 501)
        self.assertEqual([call.kwargs["offset"] for call in fetch.call_args_list], [0, 500])
        for call in fetch.call_args_list:
            self.assertEqual(call.kwargs["size_threshold"], 0)
            self.assertIs(call.kwargs["include_archived"], True)

    def test_position_wrapper_preserves_explicit_zero_and_archived_flag(self):
        with patch.object(data_api, "_get_json", return_value=[]) as get:
            data_api.get_positions(WALLET, size_threshold=0, include_archived=True)
        self.assertEqual(get.call_args.kwargs["params"]["sizeThreshold"], 0)
        self.assertEqual(get.call_args.kwargs["params"]["includeArchived"], "true")

    def test_general_position_wrapper_retains_documented_default_filters(self):
        with patch.object(data_api, "_get_json", return_value=[]) as get:
            data_api.get_positions(WALLET)
        self.assertEqual(get.call_args.kwargs["params"]["sizeThreshold"], 1)
        self.assertEqual(get.call_args.kwargs["params"]["includeArchived"], "false")

    def test_position_wrapper_rejects_invalid_filters_before_network(self):
        for options in ({"size_threshold": -1}, {"size_threshold": float("nan")},
                        {"size_threshold": True}, {"include_archived": "false"}):
            with self.subTest(options=options), patch.object(data_api, "_get_json") as get:
                with self.assertRaises(ValueError):
                    data_api.get_positions(WALLET, **options)
                get.assert_not_called()

    def test_mdd_coverage_discloses_query_scope_without_verifying_account_history(self):
        with patch.object(data_api, "get_positions", return_value=[{"cashPnl": -1, "initialValue": 10}]), patch.object(
            data_api, "get_closed_positions", return_value=[]
        ), patch.object(data_api, "get_activity", return_value=[]), patch.object(data_api, "get_trades", return_value=[]):
            result = mdd.build_historical_mdd_payload(mdd.fetch_mdd_inputs(WALLET))
        self.assertEqual(result["mdd_history_coverage"]["open_positions"]["query_filters"], {
            "sizeThreshold": 0, "includeArchived": True,
        })
        self.assertFalse(result["mdd_account_equity_verified"])


if __name__ == "__main__":
    unittest.main()
