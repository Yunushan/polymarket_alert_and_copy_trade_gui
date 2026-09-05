from __future__ import annotations

import json
import csv
import io
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import market_sentinel_cli
import web_api
from polymarket.accounting import reconcile_mdd_payload_with_accounting
from polymarket.analytics_cache import mdd_payload_to_csv
from polymarket.drawdown import max_drawdown, percentage_drawdown
from polymarket.mdd import MDD_CALCULATION_VERSION, MddInputs, build_historical_mdd_payload, build_mark_replay_mdd_payload


WALLET = "0x" + "b" * 40


def inputs(deltas: list[float]) -> MddInputs:
    return MddInputs(
        wallet=WALLET,
        closed_positions=[{"timestamp": index + 1, "realizedPnl": delta} for index, delta in enumerate(deltas)],
        open_positions=[], activity_events=[], trade_rows=[],
    )


class DrawdownCorrectnessTests(unittest.TestCase):
    def test_percentage_and_dollar_maxima_have_independent_provenance(self) -> None:
        result = build_historical_mdd_payload(inputs([1000, -500, 8500, -1000]), equity_base_usd=1000)
        self.assertEqual(result["mdd_usd"], 1000)
        self.assertEqual(result["mdd_pct"], 25)
        self.assertEqual((result["peak_value"], result["trough_value"]), (9000, 8000))
        self.assertEqual((result["peak_timestamp"], result["trough_timestamp"]), (3, 4))
        self.assertEqual((result["pct_peak_value"], result["pct_trough_value"]), (1000, 500))
        self.assertEqual((result["pct_peak_timestamp"], result["pct_trough_timestamp"]), (1, 2))
        self.assertEqual(result["pct_drawdown_usd"], 500)
        self.assertEqual(result["calculation_version"], MDD_CALCULATION_VERSION)

    def test_initial_realized_loss_starts_at_zero_pnl(self) -> None:
        result = build_historical_mdd_payload(inputs([-30]), equity_base_usd=100)
        self.assertEqual(result["mdd_usd"], 30)
        self.assertEqual(result["mdd_pct"], 30)
        self.assertEqual(result["peak_value"], 0)
        self.assertIsNone(result["peak_timestamp"])
        self.assertEqual(result["trough_timestamp"], 1)
        self.assertEqual(result["drawdown_baseline"], "zero_pnl_at_start_of_observed_window")

    def test_initial_open_loss_is_included(self) -> None:
        data = inputs([])
        data.open_positions.append({"totalPnl": -30})
        result = build_historical_mdd_payload(data, equity_base_usd=100)
        self.assertEqual(result["mdd_pct"], 30)

    def test_empty_history_is_unknown_not_zero_risk(self) -> None:
        result = build_historical_mdd_payload(inputs([]), equity_base_usd=100)
        self.assertIsNone(result["mdd_pct"])
        self.assertIsNone(result["mdd_usd"])
        self.assertFalse(result["mdd_available"])

    def test_rows_without_any_pnl_do_not_establish_zero_risk(self) -> None:
        data = inputs([])
        data.closed_positions.append({"timestamp": 1})
        data.open_positions.append({"currentValue": 100})
        result = build_historical_mdd_payload(data, equity_base_usd=100)
        self.assertFalse(result["mdd_available"])
        self.assertIsNone(result["mdd_pct"])

    def test_audit_csv_preserves_percentage_provenance_and_version(self) -> None:
        payload = build_historical_mdd_payload(inputs([1000, -500, 8500, -1000]), equity_base_usd=1000)
        row = next(csv.DictReader(io.StringIO(mdd_payload_to_csv(payload))))
        self.assertEqual(row["mdd_pct"], "25.0")
        self.assertEqual(row["pct_peak_timestamp"], "1")
        self.assertEqual(row["calculation_version"], str(MDD_CALCULATION_VERSION))

    def test_legacy_audit_export_is_marked_as_old_without_erasing_evidence(self) -> None:
        legacy = {"mdd_pct": 10}
        with patch("web_api.load_analytics_artifact", return_value=(dict(legacy), {"key": "old"})):
            result = web_api.polymarket_mdd_export_payload("old")
        self.assertFalse(result["cache"]["calculation_current"])
        self.assertFalse(result["payload"]["calculation_current"])
        self.assertEqual(result["payload"]["mdd_pct"], 10)

    def test_no_positive_capital_preserves_dollar_drawdown_only(self) -> None:
        for base in (None, 0, -1):
            with self.subTest(base=base):
                result = max_drawdown([{"value": -30}], base)
                self.assertEqual(result["mdd_usd"], 30)
                self.assertIsNone(result["mdd_pct"])
                self.assertIsNone(result["pct_peak_value"])

    def test_observed_no_loss_is_distinct_from_missing_observations(self) -> None:
        result = max_drawdown([{"value": 0}, {"value": 10}], 100)
        self.assertTrue(result["mdd_available"])
        self.assertEqual(result["mdd_pct"], 0)
        self.assertEqual(result["drawdown_episodes"], [])

    def test_nonfinite_values_fail_closed(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    max_drawdown([{"value": value}], 100)
                with self.assertRaises(ValueError):
                    max_drawdown([{"value": 0}], value)

    def test_random_paths_match_independent_all_prior_points_reference(self) -> None:
        rng = random.Random(8675309)
        for _ in range(150):
            values = [rng.randint(-2000, 10000) for _ in range(rng.randint(1, 35))]
            points = [{"timestamp": index, "value": value} for index, value in enumerate(values)]
            for base in (10, 1000, 10000):
                # An O(n^2) reference includes the known constructed zero-PnL baseline.
                pairs = [(max([0] + values[:i + 1]), value) for i, value in enumerate(values)]
                expected_usd = max(max(0, prior - value) for prior, value in pairs)
                expected_pct = max(max(0, prior - value) / (base + prior) * 100 for prior, value in pairs if prior >= 0)
                actual = max_drawdown(points, base)
                self.assertAlmostEqual(actual["mdd_usd"], expected_usd)
                self.assertAlmostEqual(actual["mdd_pct"], expected_pct)
                rebased = percentage_drawdown(max_drawdown(points, 1)["drawdown_episodes"], base)
                self.assertAlmostEqual(rebased["mdd_pct"], expected_pct)

    def test_equal_dollar_losses_can_have_different_percentage_losses(self) -> None:
        result = max_drawdown([{"value": value} for value in (100, 50, 900, 850)], 100)
        self.assertEqual(result["mdd_usd"], 50)
        self.assertEqual(result["mdd_pct"], 25)
        self.assertEqual(len(result["drawdown_episodes"]), 2)

    def test_accounting_rebase_can_change_winning_percentage_episode(self) -> None:
        result = build_historical_mdd_payload(inputs([100, -50, 850, -100]), equity_base_usd=100, max_points=1)
        self.assertEqual(result["mdd_pct"], 25)
        self.assertEqual(len(result["points"]), 1)
        reconciled = reconcile_mdd_payload_with_accounting(result, {"equity": {"base_equity_usd": 10000}})
        self.assertAlmostEqual(reconciled["mdd_pct"], 100 / 10900 * 100)
        self.assertEqual(reconciled["pct_peak_timestamp"], 3)
        self.assertEqual(reconciled["mdd_usd"], 100)
        self.assertTrue(reconciled["accounting_snapshot"]["reconciliation"]["mdd_pct_uses_accounting_base"])

    def test_accounting_does_not_rebase_an_incomplete_legacy_summary(self) -> None:
        legacy = {"mdd_usd": 100, "mdd_pct": 25, "peak_value": 900, "equity_base_usd": 100,
                  "points": [{"value": 800}], "points_total": 4}
        result = reconcile_mdd_payload_with_accounting(legacy, {"equity": {"base_equity_usd": 10000}})
        self.assertEqual(result["mdd_pct"], 25)
        self.assertEqual(result["equity_base_usd"], 100)
        self.assertFalse(result["accounting_snapshot"]["reconciliation"]["mdd_pct_uses_accounting_base"])

    def test_mark_replay_uses_independent_percentage_maximum(self) -> None:
        data = inputs([])
        data.trade_rows.append({"side": "BUY", "timestamp": 1, "size": 10000, "price": 0.1, "asset": "token"})
        history = {"history": {"token": [{"t": i + 2, "p": p} for i, p in enumerate((0.2, 0.15, 1, 0.9))]}}
        with patch("polymarket.mdd.clob_rest.get_batch_price_history", return_value=history):
            result = build_mark_replay_mdd_payload(data, equity_base_usd=1000)
        self.assertEqual(result["mdd_pct"], 25)
        self.assertEqual(result["mdd_usd"], 1000)
        self.assertTrue(result["mdd_available"])

    def test_web_compatibility_helper_uses_shared_math(self) -> None:
        self.assertEqual(web_api._max_drawdown([{"value": -30}], 100)["mdd_pct"], 30)

    def test_api_risk_filter_rejects_25_percent_wallet(self) -> None:
        with patch("web_api.data_api.get_leaderboard", return_value=[{"proxyWallet": WALLET, "pnl": 8000, "vol": 1000}]), patch(
            "polymarket.mdd.fetch_mdd_inputs", return_value=inputs([1000, -500, 8500, -1000])
        ), patch("web_api.attach_polymarket_mdd_audit_cache", return_value={}):
            result = web_api.polymarket_leaderboard_payload({
                "compute_mdd": ["true"], "max_mdd_pct": ["20"], "equity_base_usd": ["1000"],
            })
        self.assertEqual(result["counts"]["returned"], 0)
        self.assertEqual(result["counts"]["mdd_computed"], 1)

    def test_cli_resume_invalidates_old_calculation_version_and_rejects_wallet(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "result.json"
            args = ["polymarket-leaderboard", "--state-db", str(Path(temporary) / "state.sqlite3"),
                    "--scanned", "unlimited", "--returned", "unlimited", "--compute-mdd",
                    "--mdd-scan", "unlimited", "--max-mdd-pct", "20", "--equity-base-usd", "1000",
                    "--format", "json", "--output", str(output), "--quiet"]
            with patch("web_api.data_api.get_leaderboard", return_value=[{"proxyWallet": WALLET, "pnl": 8000, "vol": 1000}]), patch(
                "market_sentinel_cli.MDD_CALCULATION_VERSION", 1
            ), patch("market_sentinel_cli.polymarket_user_mdd_payload", return_value={"mdd_pct": 10}), patch(
                "market_sentinel_cli.attach_polymarket_mdd_audit_cache", return_value={}
            ):
                self.assertEqual(market_sentinel_cli.main(args), 0)
            self.assertEqual(json.loads(output.read_text())["counts"]["returned"], 1)
            with patch("web_api.data_api.get_leaderboard") as fetch, patch(
                "polymarket.mdd.fetch_mdd_inputs", return_value=inputs([1000, -500, 8500, -1000])
            ) as get_inputs, patch("market_sentinel_cli.attach_polymarket_mdd_audit_cache", return_value={}):
                self.assertEqual(market_sentinel_cli.main(args + ["--resume"]), 0)
            fetch.assert_not_called()
            get_inputs.assert_called_once()
            result = json.loads(output.read_text())
            self.assertEqual(result["counts"]["returned"], 0)
            self.assertEqual(result["mdd_signature"]["calculation_version"], MDD_CALCULATION_VERSION)


if __name__ == "__main__":
    unittest.main()
