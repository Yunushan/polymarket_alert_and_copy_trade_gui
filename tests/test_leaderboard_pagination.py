from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import market_sentinel_cli
import web_api
from polymarket import data_api
from polymarket.leaderboard import LEADERBOARD_MAX_OFFSET, wallet_membership_fingerprint
from polymarket.leaderboard_state import LeaderboardStateStore


def page(offset: int, count: int = 50) -> list[dict]:
    return [{"proxyWallet": "0x" + format(index + 1, "040x"), "rank": index + 1, "pnl": 100, "vol": 100}
            for index in range(offset, offset + count)]


class LeaderboardPaginationTests(unittest.TestCase):
    def scan(self, **options):
        summary, warnings = {}, []
        arguments = {"scan_limit": None, "remote_sort": "PNL", "direction": "DESC", "period": "all",
                     "category": "OVERALL", "scan_concurrency": 1, "is_cancelled": lambda: False,
                     "emit_progress": lambda *args, **kwargs: None, "warnings": warnings, "scan_summary": summary}
        arguments.update(options)
        rows, cancelled = web_api._fetch_polymarket_leaderboard_scan_rows(**arguments)
        return rows, cancelled, summary, warnings

    def test_serial_and_concurrent_unlimited_scans_never_request_beyond_source_bound(self) -> None:
        for concurrency in (1, 6, 12):
            with self.subTest(concurrency=concurrency), patch.object(
                data_api, "get_leaderboard", side_effect=lambda **kw: page(kw["offset"], kw["limit"])
            ) as get:
                rows, cancelled, summary, warnings = self.scan(scan_concurrency=concurrency)
            offsets = [call.kwargs["offset"] for call in get.call_args_list]
            self.assertEqual(sorted(offsets), list(range(0, 1001, 50)))
            self.assertEqual(len(rows), 1050)
            self.assertFalse(cancelled)
            self.assertEqual(summary["completion_reason"], "upstream_offset_limit")
            self.assertFalse(summary["source_enumeration_complete"])
            self.assertTrue(warnings)

    def test_legacy_resume_offset_past_bound_does_not_make_a_network_request(self) -> None:
        with patch.object(data_api, "get_leaderboard") as get:
            _rows, _cancelled, summary, _warnings = self.scan(scan_start_offset=12335250)
        get.assert_not_called()
        self.assertEqual(summary["completion_reason"], "upstream_offset_limit")

    def test_finite_budget_and_short_page_retain_their_own_stop_reasons(self) -> None:
        with patch.object(data_api, "get_leaderboard", side_effect=lambda **kw: page(kw["offset"], kw["limit"])):
            rows, _cancelled, summary, _warnings = self.scan(scan_limit=75)
        self.assertEqual(len(rows), 75)
        self.assertEqual(summary["completion_reason"], "scan_limit_reached")
        with patch.object(data_api, "get_leaderboard", return_value=page(0, 2)):
            _rows, _cancelled, summary, _warnings = self.scan()
        self.assertEqual(summary["completion_reason"], "end_of_results")
        self.assertTrue(summary["source_enumeration_complete"])

    def test_same_wallets_with_reordered_membership_and_changed_metrics_stop(self) -> None:
        original = page(0)
        changed = [{**row, "pnl": 900, "rank": index + 51} for index, row in enumerate(reversed(original))]
        with patch.object(data_api, "get_leaderboard", side_effect=[original, changed]) as get:
            rows, _cancelled, summary, _warnings = self.scan()
        self.assertEqual(get.call_count, 2)
        self.assertEqual(len(rows), 50)
        self.assertEqual(summary["completion_reason"], "repeated_page")

    def test_partial_overlap_with_new_wallets_is_not_a_repeat(self) -> None:
        with patch.object(data_api, "get_leaderboard", side_effect=[page(0), page(25), []]):
            rows, _cancelled, summary, _warnings = self.scan()
        self.assertEqual(len(rows), 100)
        self.assertEqual(summary["completion_reason"], "end_of_results")

    def test_membership_uses_normalized_wallets_and_refuses_unknown_membership(self) -> None:
        self.assertEqual(wallet_membership_fingerprint([{"wallet": " 0xABC "}]), wallet_membership_fingerprint([{"proxyWallet": "0xabc"}]))
        self.assertEqual(wallet_membership_fingerprint([{"wallet": "0xabc"}, {"name": "unknown"}]), "")
        self.assertEqual(wallet_membership_fingerprint([]), "")

    def test_unknown_wallets_do_not_make_distinct_pages_identical(self) -> None:
        with patch.object(data_api, "get_leaderboard", side_effect=[
            [{"rank": i} for i in range(50)], [{"rank": i} for i in range(50, 100)], []
        ]):
            rows, _cancelled, summary, _warnings = self.scan()
        self.assertEqual(len(rows), 100)
        self.assertEqual(summary["completion_reason"], "end_of_results")

    def test_durable_membership_survives_resume_and_legacy_column_migration(self) -> None:
        for legacy in (False, True):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "state.sqlite3"
                with closing(LeaderboardStateStore(path)) as store:
                    store.prepare({}, resume=False)
                    store.record_page(0, 50, [web_api.normalize_polymarket_leaderboard_row(row, 1) for row in page(0)])
                    if legacy:
                        store.connection.executescript("""
                            CREATE TABLE legacy_pages (
                                page_offset INTEGER PRIMARY KEY, page_limit INTEGER NOT NULL,
                                row_count INTEGER NOT NULL, fingerprint TEXT NOT NULL DEFAULT '', saved_at INTEGER NOT NULL
                            );
                            INSERT INTO legacy_pages SELECT page_offset, page_limit, row_count, fingerprint, saved_at FROM pages;
                            DROP TABLE pages;
                            ALTER TABLE legacy_pages RENAME TO pages;
                        """)
                        store.connection.commit()
                with closing(LeaderboardStateStore(path)) as store:
                    changed = [{**row, "pnl": 999} for row in reversed(page(0))]
                    self.assertFalse(store.record_page(50, 50, [web_api.normalize_polymarket_leaderboard_row(row, 1) for row in changed]))
                    self.assertEqual(store.progress()["stop_reason"], "repeated_page")
                    self.assertEqual(store.progress()["scanned"], 50)

    def test_api_and_cli_preserve_requested_unlimited_budgets_and_report_source_limit(self) -> None:
        with patch.object(data_api, "get_leaderboard", side_effect=lambda **kw: page(kw["offset"], kw["limit"])):
            result = web_api.polymarket_leaderboard_payload({"limit": ["unlimited"], "scan_limit": ["unlimited"]})
        self.assertIsNone(result["limit"])
        self.assertIsNone(result["scan_limit"])
        self.assertEqual(result["completion_reason"], "upstream_offset_limit")
        self.assertFalse(result["source_enumeration_complete"])
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "result.json"
            args = ["polymarket-leaderboard", "--state-db", str(Path(temporary) / "state.sqlite3"),
                    "--scanned", "unlimited", "--returned", "unlimited", "--format", "json", "--output", str(output), "--quiet"]
            with patch.object(data_api, "get_leaderboard", side_effect=lambda **kw: page(kw["offset"], kw["limit"])):
                self.assertEqual(market_sentinel_cli.main(args), 0)
            result = json.loads(output.read_text())
            self.assertEqual(result["completion_reason"], "upstream_offset_limit")
            self.assertFalse(result["source_enumeration_complete"])
            self.assertIsNone(result["scan_limit"])
            self.assertEqual(result["counts"]["scanned"], 1050)
            with patch.object(data_api, "get_leaderboard") as get:
                self.assertEqual(market_sentinel_cli.main(args + ["--resume"]), 0)
            get.assert_not_called()

    def test_data_wrapper_accepts_maximum_offset_and_rejects_one_past_it(self) -> None:
        with patch.object(data_api, "_get_json", return_value=[]) as get:
            data_api.get_leaderboard(offset=LEADERBOARD_MAX_OFFSET)
            self.assertEqual(get.call_args.kwargs["params"]["offset"], LEADERBOARD_MAX_OFFSET)
            with self.assertRaises(ValueError):
                data_api.get_leaderboard(offset=LEADERBOARD_MAX_OFFSET + 1)
            self.assertEqual(get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
