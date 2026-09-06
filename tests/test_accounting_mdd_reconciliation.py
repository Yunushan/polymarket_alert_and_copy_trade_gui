from __future__ import annotations

import io
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from contextlib import closing

import market_sentinel_cli
import web_api
from polymarket.accounting import parse_accounting_snapshot_zip, reconcile_mdd_payload_with_accounting
from polymarket.leaderboard_state import LeaderboardStateStore
from polymarket.mdd import MDD_CALCULATION_VERSION, MddInputs, build_historical_mdd_payload, polymarket_user_mdd_payload_mark_replay


WALLET = "0x" + "b" * 40


def snapshot(csv_text="timestamp,equity,deposit\n0,100,0\n1,70,0\n2,10070,10000\n"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("equity.csv", csv_text)
    return parse_accounting_snapshot_zip(buffer.getvalue())


def inputs(loss=-30, *, capital=True):
    row = {"timestamp": 1, "realizedPnl": loss}
    if capital:
        row["totalBought"] = 100
    return MddInputs(WALLET, [row], [], [], [])


class AccountingMddReconciliationTests(unittest.TestCase):
    def assert_unchanged_drawdown(self, payload, statement):
        reconciled = reconcile_mdd_payload_with_accounting(payload, statement)
        for key, value in payload.items():
            self.assertEqual(reconciled[key], value, key)
        self.assertFalse(reconciled["accounting_snapshot"]["reconciliation"]["mdd_pct_uses_accounting_base"])
        return reconciled

    def test_snapshot_equity_is_not_declared_as_historical_opening_capital(self):
        equity = snapshot()["equity"]
        self.assertEqual(equity["first_equity_usd"], 100)
        self.assertEqual(equity["last_equity_usd"], 10070)
        self.assertEqual(equity["max_equity_usd"], 10070)
        self.assertIsNone(equity["base_equity_usd"])
        self.assertEqual(equity["base_source"], "unavailable_from_point_in_time_snapshot")

    def test_later_deposits_profits_and_withdrawals_cannot_rebase_history(self):
        statements = (
            "timestamp,equity,deposit\n0,100,0\n1,70,0\n2,10070,10000\n",
            "timestamp,equity\n0,100\n1,70\n2,10070\n",
            "timestamp,equity,withdrawal\n0,1000,0\n1,700,0\n2,70,630\n",
            "timestamp,equity\n9999999999,10070\n",
            "equity\n10070\n",
        )
        for csv_text in statements:
            for base in (100, None):
                with self.subTest(csv=csv_text, base=base):
                    payload = build_historical_mdd_payload(inputs(), equity_base_usd=base)
                    self.assertEqual(payload["mdd_pct"], 30)
                    self.assert_unchanged_drawdown(payload, snapshot(csv_text))

    def test_snapshot_cannot_supply_missing_historical_capital(self):
        payload = build_historical_mdd_payload(inputs(capital=False))
        self.assertIsNone(payload["mdd_pct"])
        self.assert_unchanged_drawdown(payload, snapshot())

    def test_legacy_max_equity_field_cannot_reenable_rebasing(self):
        payload = build_historical_mdd_payload(inputs(), equity_base_usd=100)
        for statement in (
            {"equity": {"base_equity_usd": 10070}},
            {"status": "ok", "complete": True, "equity": {"base_equity_usd": 10070}},
        ):
            with self.subTest(statement=statement):
                self.assert_unchanged_drawdown(payload, statement)

    def test_independent_percentage_peak_and_trimmed_history_are_preserved(self):
        data = MddInputs(WALLET, [
            {"timestamp": i + 1, "realizedPnl": delta}
            for i, delta in enumerate((100, -50, 850, -100))
        ], [], [], [])
        payload = build_historical_mdd_payload(data, equity_base_usd=100, max_points=1)
        self.assertEqual(payload["mdd_pct"], 25)
        self.assertEqual(payload["pct_peak_timestamp"], 1)
        self.assert_unchanged_drawdown(payload, snapshot())

    def test_current_position_comparisons_remain_available(self):
        payload = build_historical_mdd_payload(inputs(), equity_base_usd=100)
        statement = snapshot()
        statement["positions"] = {"current_value_usd": 40, "realized_pnl_usd": -25}
        result = self.assert_unchanged_drawdown(payload, statement)
        reconciliation = result["accounting_snapshot"]["reconciliation"]
        self.assertEqual(reconciliation["open_current_value_delta_usd"], 40)
        self.assertEqual(reconciliation["realized_pnl_delta_usd"], 5)
        self.assertEqual(reconciliation["scope"], "point_in_time_comparison")

    def test_mark_replay_preserves_the_original_capital_base(self):
        data = MddInputs(WALLET, [], [], [], [
            {"side": "BUY", "timestamp": 1, "size": 100, "price": 1, "asset": "token"}
        ])
        with patch("polymarket.mdd.clob_rest.get_batch_price_history", return_value={
            "history": {"token": [{"t": 1, "p": 1}, {"t": 2, "p": 0.7}]}
        }), patch("polymarket.mdd.download_and_parse_accounting_snapshot", return_value=snapshot()), patch(
            "polymarket.mdd.fetch_mdd_inputs", return_value=data
        ):
            result = polymarket_user_mdd_payload_mark_replay(WALLET, equity_base_usd=100, include_accounting_snapshot=True)
        self.assertEqual(result["mdd_pct"], 30)
        self.assertEqual(result["equity_base_usd"], 100)
        self.assertEqual(result["equity_base_source"], "user_supplied")

    def test_api_filter_rejects_the_historical_30_percent_loss(self):
        for base in (None, 100):
            query = {"compute_mdd": ["true"], "max_mdd_pct": ["20"], "mdd_include_accounting": ["true"]}
            if base is not None:
                query["equity_base_usd"] = [str(base)]
            with self.subTest(base=base), patch("web_api.data_api.get_leaderboard", return_value=[
                {"proxyWallet": WALLET, "pnl": 100, "vol": 100}
            ]), patch("polymarket.mdd.fetch_mdd_inputs", return_value=inputs()), patch(
                "polymarket.mdd.download_and_parse_accounting_snapshot", return_value=snapshot()
            ), patch("web_api.attach_polymarket_mdd_audit_cache", return_value={}):
                result = web_api.polymarket_leaderboard_payload(query)
            self.assertEqual(result["counts"]["returned"], 0)
            self.assertEqual(result["counts"]["mdd_computed"], 1)

    def test_accounting_does_not_disqualify_an_unchanged_valid_observed_result(self):
        with patch("web_api.data_api.get_leaderboard", return_value=[
            {"proxyWallet": WALLET, "pnl": 100, "vol": 100}
        ]), patch("polymarket.mdd.fetch_mdd_inputs", return_value=inputs(-5)), patch(
            "polymarket.mdd.download_and_parse_accounting_snapshot", return_value=snapshot()
        ), patch("web_api.attach_polymarket_mdd_audit_cache", return_value={}):
            result = web_api.polymarket_leaderboard_payload({
                "compute_mdd": ["true"], "max_mdd_pct": ["20"], "mdd_include_accounting": ["true"],
                "equity_base_usd": ["100"],
            })
        self.assertEqual(result["counts"]["returned"], 1)
        self.assertEqual(result["rows"][0]["mdd_pct"], 5)
        self.assertFalse(result["rows"][0]["mdd_account_equity_verified"])

    def test_cli_resume_invalidates_version_five_accounting_enrichment(self):
        self.assertGreater(MDD_CALCULATION_VERSION, 5)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"
            args = ["polymarket-leaderboard", "--state-db", str(Path(directory) / "scan.sqlite3"),
                    "--scanned", "unlimited", "--returned", "unlimited", "--compute-mdd",
                    "--mdd-scan", "unlimited", "--max-mdd-pct", "20", "--equity-base-usd", "100",
                    "--mdd-include-accounting", "--format", "json", "--output", str(output), "--quiet"]
            legacy = {**build_historical_mdd_payload(inputs(), equity_base_usd=100),
                      "mdd_pct": 30 / 10070 * 100, "equity_base_usd": 10070, "calculation_version": 5}
            with patch("web_api.data_api.get_leaderboard", return_value=[
                {"proxyWallet": WALLET, "pnl": 100, "vol": 100}
            ]), patch("market_sentinel_cli.MDD_CALCULATION_VERSION", 5), patch(
                "market_sentinel_cli.polymarket_user_mdd_payload", return_value=legacy
            ), patch("market_sentinel_cli.attach_polymarket_mdd_audit_cache", return_value={}):
                self.assertEqual(market_sentinel_cli.main(args), 0)
            self.assertEqual(json.loads(output.read_text())["counts"]["returned"], 1)
            for export_filter in (["--require-mdd"], ["--max-mdd-pct", "20"], ["--sort", "mdd_pct"]):
                for output_format in ("json", "csv"):
                    before = output.read_bytes()
                    with self.subTest(export_filter=export_filter, format=output_format), patch("sys.stderr", io.StringIO()) as errors:
                        self.assertEqual(market_sentinel_cli.main([
                            "leaderboard-export", "--state-db", str(Path(directory) / "scan.sqlite3"),
                            "--format", output_format, "--output", str(output), *export_filter,
                        ]), 1)
                    self.assertIn("Resume the scan", errors.getvalue())
                    self.assertEqual(output.read_bytes(), before)
            for output_format in ("json", "csv"):
                self.assertEqual(market_sentinel_cli.main([
                    "leaderboard-export", "--state-db", str(Path(directory) / "scan.sqlite3"),
                    "--format", output_format, "--output", str(output),
                ]), 0)
                if output_format == "json":
                    historical = json.loads(output.read_text())
                    self.assertFalse(historical["mdd_calculation_current"])
                    self.assertTrue(historical["warnings"])
                else:
                    historical = next(csv.DictReader(io.StringIO(output.read_text())))
                    self.assertEqual(historical["mdd_calculation_version"], "5")
                    self.assertEqual(historical["mdd_calculation_current"], "False")
            with patch("web_api.data_api.get_leaderboard") as leaderboard, patch(
                "polymarket.mdd.fetch_mdd_inputs", return_value=inputs()
            ) as history, patch("polymarket.mdd.download_and_parse_accounting_snapshot", return_value=snapshot()), patch(
                "market_sentinel_cli.attach_polymarket_mdd_audit_cache", return_value={}
            ):
                self.assertEqual(market_sentinel_cli.main(args + ["--resume"]), 0)
            leaderboard.assert_not_called()
            history.assert_called_once()
            result = json.loads(output.read_text())
            self.assertEqual(result["counts"]["returned"], 0)
            self.assertEqual(result["mdd_signature"]["calculation_version"], MDD_CALCULATION_VERSION)

    def test_csv_rows_identify_current_and_unknown_versions(self):
        for version, current in ((None, False), (5, False), (MDD_CALCULATION_VERSION, True)):
            for key in ("calculation_version", "mdd_calculation_version"):
                with self.subTest(version=version, key=key):
                    row = next(market_sentinel_cli._csv_rows([{key: version, "mdd_pct": 5}]))
                    self.assertEqual(row["mdd_calculation_version"], version)
                    self.assertEqual(row["mdd_calculation_current"], current)

    def test_risk_export_rejects_an_unversioned_saved_calculation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scan.sqlite3"
            with closing(LeaderboardStateStore(path)) as store:
                store.prepare({}, resume=False)
                store.record_page(0, 50, [{"wallet": WALLET}])
                row = next(store.iter_mdd_candidates({}, sort="roi_pct", direction="DESC", limit=1))
                store.set_mdd(row["id"], {"mdd_pct": 5})
            with patch("sys.stderr", io.StringIO()) as errors, patch("sys.stdout", io.StringIO()) as output:
                self.assertEqual(market_sentinel_cli.main([
                    "leaderboard-export", "--state-db", str(path), "--max-mdd-pct", "20", "--format", "csv",
                ]), 1)
            self.assertIn("unversioned", errors.getvalue())
            self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
