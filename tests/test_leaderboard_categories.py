from __future__ import annotations

import ast
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

import market_sentinel_cli as cli
import web_api
from polymarket import data_api
from polymarket.leaderboard import LEADERBOARD_CATEGORIES, normalize_leaderboard_category
from polymarket.leaderboard_state import LeaderboardStateStore


ROOT = Path(__file__).resolve().parents[1]
RAW = {"proxyWallet": "0x" + "b" * 40, "pnl": 10, "vol": 100}


class LeaderboardCategoryTests(unittest.TestCase):
    def run_cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            result = cli.main(list(args))
        return result, stdout.getvalue(), stderr.getvalue()

    def scan_args(self, *args):
        return ("polymarket-leaderboard", "--scanned", "1", "--returned", "1", "--format", "json", "--quiet", *args)

    def test_all_documented_categories_are_normalized_and_sent_unchanged(self):
        expected = ("OVERALL", "POLITICS", "SPORTS", "ESPORTS", "CRYPTO", "CULTURE", "MENTIONS",
                    "WEATHER", "ECONOMICS", "TECH", "FINANCE")
        self.assertEqual(LEADERBOARD_CATEGORIES, expected)
        for category in expected:
            for value in (category, " " + category.lower() + " "):
                with self.subTest(category=value), patch("polymarket.data_api._get_json", return_value=[]) as fetch:
                    data_api.get_leaderboard(category=value)
                self.assertEqual(fetch.call_args.kwargs["params"]["category"], category)
        for value in (None, "", "   "):
            self.assertEqual(normalize_leaderboard_category(value), "OVERALL")

    def test_invalid_categories_fail_before_fetch_even_with_initial_rows(self):
        for value in ("ESPORT", "ALL", "SPORTS,CRYPTO", "not-a-category"):
            with self.subTest(category=value), patch("polymarket.data_api._get_json") as fetch:
                with self.assertRaisesRegex(ValueError, "Unsupported leaderboard category"):
                    data_api.get_leaderboard(category=value)
                with self.assertRaisesRegex(ValueError, "Unsupported leaderboard category"):
                    web_api.polymarket_leaderboard_payload({"category": [value]}, initial_raw_rows=[RAW])
                fetch.assert_not_called()
        for value in (1, False, [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "must be a string"):
                normalize_leaderboard_category(value)

    def test_api_reports_the_category_it_sent_upstream(self):
        for category in LEADERBOARD_CATEGORIES:
            with self.subTest(category=category), patch("polymarket.data_api._get_json", return_value=[RAW]) as fetch:
                result = web_api.polymarket_leaderboard_payload({
                    "category": [" " + category.lower() + " "], "scan_limit": ["1"], "limit": ["1"],
                })
            self.assertEqual(result["category"], category)
            self.assertEqual(result["counts"]["returned"], 1)
            self.assertEqual(fetch.call_args.kwargs["params"]["category"], category)

    def test_cli_memory_sqlite_and_checkpoint_paths_keep_the_category(self):
        for mode in (None, "--state-db", "--checkpoint"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                state = Path(directory) / "scan"
                extra = (mode, str(state)) if mode else ()
                with patch("polymarket.data_api._get_json", return_value=[RAW]) as fetch:
                    result, stdout, stderr = self.run_cli(*self.scan_args("--category", " esports ", *extra))
                self.assertEqual(result, 0, stderr)
                self.assertEqual(json.loads(stdout)["category"], "ESPORTS")
                self.assertEqual(fetch.call_args.kwargs["params"]["category"], "ESPORTS")
                if mode:
                    with patch("polymarket.data_api._get_json", return_value=[]) as fetch:
                        result, stdout, stderr = self.run_cli(*self.scan_args("--category", "ESPORTS", *extra, "--resume"))
                    self.assertEqual(result, 0, stderr)
                    self.assertEqual(json.loads(stdout)["category"], "ESPORTS")
                    fetch.assert_not_called()

    def test_invalid_cli_category_preserves_existing_output_and_saved_state(self):
        for mode in (None, "--state-db", "--checkpoint"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "out.csv"
                state = Path(directory) / "state"
                output.write_bytes(b"previous report")
                state.write_bytes(b"previous state")
                extra = (mode, str(state)) if mode else ()
                with patch("polymarket.data_api._get_json") as fetch:
                    result, stdout, stderr = self.run_cli(*self.scan_args(
                        "--category", "ESPORT", "--output", str(output), *extra,
                    ))
                self.assertEqual(result, 1)
                self.assertIn("Unsupported leaderboard category", stderr)
                self.assertEqual(stdout, "")
                fetch.assert_not_called()
                self.assertEqual(output.read_bytes(), b"previous report")
                self.assertEqual(state.read_bytes(), b"previous state")

    def test_legacy_esports_sqlite_cannot_resume_or_export_mislabeled_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.db"
            output = Path(directory) / "report.json"
            output.write_bytes(b"previous report")
            legacy = {"remote_sort": "PNL", "direction": "DESC", "period": "all", "category": "ESPORTS"}
            with closing(LeaderboardStateStore(state)) as store:
                store.prepare(legacy, resume=False)
                store.record_page(0, 1, [web_api.normalize_polymarket_leaderboard_row(RAW, 1)])
            commands = (
                self.scan_args("--category", "ESPORTS", "--state-db", str(state), "--resume"),
                ("polymarket-leaderboard-export", "--state-db", str(state)),
            )
            for command in commands:
                with self.subTest(command=command), patch("polymarket.data_api._get_json") as fetch:
                    result, stdout, stderr = self.run_cli(*command, "--output", str(output))
                self.assertEqual(result, 1)
                self.assertTrue("different leaderboard scan settings" in stderr or "Legacy ESPORTS" in stderr)
                self.assertEqual(stdout, "")
                fetch.assert_not_called()
                self.assertEqual(output.read_bytes(), b"previous report")
                with closing(LeaderboardStateStore(state, read_only=True)) as store:
                    self.assertEqual(store.status()["signature"], legacy)
                    self.assertEqual(store.progress()["rows"], 1)

    def test_current_esports_sqlite_exports_verified_scan_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.db"
            with patch("polymarket.data_api._get_json", return_value=[RAW]):
                result, _, stderr = self.run_cli(*self.scan_args("--category", "ESPORTS", "--state-db", str(state)))
            self.assertEqual(result, 0, stderr)
            result, stdout, stderr = self.run_cli("polymarket-leaderboard-export", "--state-db", str(state), "--format", "json")
            self.assertEqual(result, 0, stderr)
            self.assertEqual(json.loads(stdout)["scan_signature"]["category"], "ESPORTS")
            self.assertEqual(json.loads(stdout)["scan_signature"]["category_contract_version"], 1)

    def test_checkpoint_rejects_changed_source_settings_without_modifying_files(self):
        for changed in (("--category", "SPORTS"), ("--period", "day"), ("--direction", "ASC"), ("--sort", "volume")):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                state, output = Path(directory) / "scan.jsonl", Path(directory) / "result.json"
                args = self.scan_args("--category", "ESPORTS", "--checkpoint", str(state), "--output", str(output))
                with patch("polymarket.data_api._get_json", return_value=[RAW]):
                    result, _, stderr = self.run_cli(*args)
                self.assertEqual(result, 0, stderr)
                before_state, before_output = state.read_bytes(), output.read_bytes()
                with patch("polymarket.data_api._get_json") as fetch:
                    result, _, stderr = self.run_cli(*args, "--resume", *changed)
                self.assertEqual(result, 1)
                self.assertIn("different leaderboard scan settings", stderr)
                fetch.assert_not_called()
                self.assertEqual(state.read_bytes(), before_state)
                self.assertEqual(output.read_bytes(), before_output)

    def test_checkpoint_without_identity_cannot_be_relabelled_during_resume(self):
        for content in ("", "not-json\n", json.dumps({"type": "leaderboard_page", "offset": 0, "limit": 1, "rows": [RAW]}) + "\n"):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                state = Path(directory) / "scan.jsonl"
                state.write_text(content, encoding="utf-8")
                before = state.read_bytes()
                with patch("polymarket.data_api._get_json") as fetch:
                    result, _, stderr = self.run_cli(*self.scan_args("--checkpoint", str(state), "--resume", "--category", "ESPORTS"))
                self.assertEqual(result, 1)
                self.assertIn("scan identity", stderr)
                fetch.assert_not_called()
                self.assertEqual(state.read_bytes(), before)

    def test_frontend_and_desktop_share_the_documented_category_options(self):
        source = (ROOT / "frontend" / "src" / "types.ts").read_text(encoding="utf-8")
        declaration = source.split("export const POLYMARKET_LEADERBOARD_CATEGORIES = ", 1)[1].split(" as const;", 1)[0]
        self.assertEqual(json.loads(declaration), list(LEADERBOARD_CATEGORIES))
        desktop = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
        selectors = [node for node in ast.walk(desktop) if isinstance(node, ast.Tuple) and len(node.elts) == 6
                     and isinstance(node.elts[0], ast.Constant) and node.elts[0].value == "Category"]
        self.assertEqual(len(selectors), 1)
        self.assertIsInstance(selectors[0].elts[2], ast.Name)
        self.assertEqual(selectors[0].elts[2].id, "LEADERBOARD_CATEGORIES")

    def test_checkpoint_resume_separates_an_interrupted_tail_from_new_pages(self):
        args = cli.build_parser().parse_args(self.scan_args("--category", "ESPORTS"))
        signature = cli._leaderboard_scan_signature(cli.build_polymarket_leaderboard_params(args))
        header = json.dumps({"type": "leaderboard_scan", "version": 1, "signature": signature})
        page = json.dumps({"type": "leaderboard_page", "offset": 0, "limit": 1, "rows": [RAW]})
        for tail in (page, page + '\n{"type":"leaderboard_page",'):
            with self.subTest(tail=tail), tempfile.TemporaryDirectory() as directory:
                checkpoint = Path(directory) / "scan.jsonl"
                checkpoint.write_text(header + "\n" + tail, encoding="utf-8")
                loaded, offset, _, _ = cli._load_leaderboard_checkpoint(checkpoint, signature=signature)
                self.assertEqual(offset, 1)
                self.assertEqual(loaded, [RAW])
                with closing(cli._LeaderboardCheckpointWriter(checkpoint)) as writer:
                    writer.record(1, 1, [{**RAW, "proxyWallet": "0x" + "c" * 40}])
                loaded, offset, _, _ = cli._load_leaderboard_checkpoint(checkpoint, signature=signature)
                self.assertEqual(offset, 2)
                self.assertEqual(len(loaded), 2)

    def test_checkpoint_cannot_mix_multiple_scan_headers(self):
        args = cli.build_parser().parse_args(self.scan_args())
        signature = cli._leaderboard_scan_signature(cli.build_polymarket_leaderboard_params(args))
        header = json.dumps({"type": "leaderboard_scan", "version": 1, "signature": signature})
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "scan.jsonl"
            checkpoint.write_text(header + "\n" + header + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "multiple scan identities"):
                cli._load_leaderboard_checkpoint(checkpoint, signature=signature)


if __name__ == "__main__":
    unittest.main()
