from __future__ import annotations

import argparse
from contextlib import redirect_stderr
import io
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest.mock import patch

from core.request_control import RequestCancelled
import market_sentinel_cli as cli
from polymarket.endpoints import PolymarketEndpoint
from polymarket.http_client import request_json
from test_http_deadlines import slow_server
import web_api


class ScanCancellationTests(unittest.TestCase):
    def test_cancellation_during_csv_and_json_exports_preserves_previous_file(self):
        for streamed in (False, True):
            for output_format in ("csv", "json"):
                with self.subTest(streamed=streamed, format=output_format), tempfile.TemporaryDirectory() as directory:
                    output = Path(directory) / "previous.csv"
                    output.write_text("previous complete export\n", encoding="utf-8")
                    calls = []

                    def cancelled(calls=calls):
                        calls.append(True)
                        return len(calls) >= 4

                    rows = [{"wallet": f"0x{index:040x}"} for index in range(5)]
                    with self.assertRaises(RequestCancelled):
                        if streamed:
                            cli._write_streamed_leaderboard_payload(
                                {"counts": {"returned": 5}}, iter(rows), output_format=output_format,
                                output=str(output), cancel_check=cancelled,
                            )
                        else:
                            cli.write_leaderboard_payload(
                                {"rows": rows}, output_format=output_format, output=str(output), cancel_check=cancelled,
                            )
                    self.assertEqual(output.read_text(encoding="utf-8"), "previous complete export\n")

    @unittest.skipUnless(os.name == "posix", "POSIX SIGTERM delivery semantics")
    def test_real_cli_sigterm_preserves_durable_pages_pending_mdd_and_previous_export(self):
        child_code = textwrap.dedent("""
            import sys
            import market_sentinel_cli as cli
            import web_api
            from polymarket.endpoints import PolymarketEndpoint
            from polymarket.http_client import request_json

            phase, origin, database, output = sys.argv[1:]
            rows = [{'proxyWallet': f'0x{index:040x}', 'pnl': 1, 'vol': 10} for index in range(1, 51)]

            def slow_request(*args, **kwargs):
                return request_json(PolymarketEndpoint('test', 'GET', '/body', origin), timeout=30)

            def page(**kwargs):
                return rows if kwargs['offset'] == 0 else slow_request()

            web_api.data_api.get_leaderboard = page
            cli.polymarket_user_mdd_payload = slow_request
            arguments = ['polymarket-leaderboard', '--state-db', database, '--output', output,
                         '--format', 'csv', '--returned', 'unlimited', '--scan-concurrency', '1',
                         '--scan-retry-attempts', '1', '--scanned', '100' if phase == 'pages' else '50']
            if phase == 'mdd':
                arguments += ['--resume', '--compute-mdd', '--mdd-scan', 'unlimited', '--mdd-concurrency', '2']
            raise SystemExit(cli.main(arguments))
        """)
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            output = Path(directory) / "previous.csv"
            output.write_text("previous complete export\n", encoding="utf-8")
            for phase in ("pages", "mdd"):
                with self.subTest(phase=phase), slow_server() as (origin, started, _seen):
                    process = subprocess.Popen(
                        [sys.executable, "-B", "-c", child_code, phase, origin, str(database), str(output)],
                        cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True,
                    )
                    try:
                        self.assertTrue(started.wait(10), "CLI must enter the real slow HTTP body before SIGTERM")
                        before = time.monotonic()
                        process.send_signal(signal.SIGTERM)
                        _stdout, stderr = process.communicate(timeout=5)
                        self.assertEqual(process.returncode, 143, stderr)
                        self.assertLess(time.monotonic() - before, 2)
                        self.assertIn("status=cancelled", stderr)
                    finally:
                        if process.poll() is None:
                            process.kill()
                        process.communicate(timeout=5)
                    self.assertEqual(output.read_text(encoding="utf-8"), "previous complete export\n")
                    with sqlite3.connect(database) as connection:
                        self.assertEqual(connection.execute("SELECT page_offset FROM pages").fetchall(), [(0,)])
                        self.assertEqual(connection.execute("SELECT COUNT(*) FROM rows").fetchone()[0], 50)
                        self.assertEqual(connection.execute("SELECT COUNT(*) FROM rows WHERE mdd_status = 'pending' AND mdd_attempts = 0").fetchone()[0], 50)

    def test_cancelled_parallel_page_preserves_only_contiguous_checkpoint(self):
        with slow_server() as (origin, started, _seen):
            cancelled = threading.Event()
            first = [{"proxyWallet": f"0x{index:040x}", "pnl": 1, "vol": 10} for index in range(1, 51)]
            stored = []
            summary = {}

            def page(**kwargs):
                if kwargs["offset"] == 0:
                    return first
                return request_json(PolymarketEndpoint("test", "GET", "/body", origin), timeout=5)

            def cancel():
                if started.wait(2):
                    cancelled.set()

            worker = threading.Thread(target=cancel)
            worker.start()
            try:
                before = time.monotonic()
                with patch("web_api.data_api.get_leaderboard", side_effect=page):
                    rows, was_cancelled = web_api._fetch_polymarket_leaderboard_scan_rows(
                        scan_limit=100, remote_sort="PNL", direction="DESC", period="all", category="OVERALL",
                        scan_concurrency=2, is_cancelled=cancelled.is_set, emit_progress=lambda *_a, **_k: None,
                        warnings=[], scan_summary=summary,
                        page_callback=lambda offset, _limit, _rows: stored.append(offset),
                    )
                self.assertLess(time.monotonic() - before, 1.5)
                self.assertTrue(was_cancelled)
                self.assertEqual(rows, first)
                self.assertEqual(stored, [0])
                self.assertEqual(summary["completion_reason"], "cancelled")
                self.assertFalse(summary["source_enumeration_complete"])
            finally:
                worker.join(timeout=3)

    def test_cancelled_mdd_workers_do_not_publish_cache_results(self):
        with slow_server() as (origin, started, _seen):
            cancelled = threading.Event()
            rows = [{"proxyWallet": f"0x{index:040x}", "pnl": 1, "vol": 10} for index in (1, 2)]

            def compute(*_args, **_kwargs):
                return request_json(PolymarketEndpoint("test", "GET", "/body", origin), timeout=5)

            def cancel():
                if started.wait(2):
                    cancelled.set()

            worker = threading.Thread(target=cancel)
            worker.start()
            try:
                before = time.monotonic()
                with (
                    patch("web_api.data_api.get_leaderboard", return_value=rows),
                    patch("web_api.polymarket_user_mdd_payload", side_effect=compute),
                    patch("web_api.attach_polymarket_mdd_audit_cache") as cache,
                ):
                    result = web_api.polymarket_leaderboard_payload(
                        {"limit": ["2"], "scan_limit": ["2"], "compute_mdd": ["true"],
                         "mdd_scan_limit": ["2"], "mdd_concurrency": ["2"]},
                        cancel_check=cancelled.is_set,
                    )
                self.assertLess(time.monotonic() - before, 1.5)
                self.assertTrue(result["cancelled"])
                self.assertEqual(result["counts"]["mdd_computed"], 0)
                cache.assert_not_called()
            finally:
                worker.join(timeout=3)

    def test_page_retry_delay_is_cancellable(self):
        cancelled = threading.Event()

        def progress(_phase, **values):
            if "retrying" in values.get("message", ""):
                cancelled.set()

        before = time.monotonic()
        with patch("web_api.data_api.get_leaderboard", side_effect=RuntimeError("upstream unavailable")) as fetch:
            rows, was_cancelled = web_api._fetch_polymarket_leaderboard_scan_rows(
                scan_limit=10, remote_sort="PNL", direction="DESC", period="all", category="OVERALL",
                scan_concurrency=1, scan_retry_attempts=10, scan_retry_delay_seconds=60,
                is_cancelled=cancelled.is_set, emit_progress=progress, warnings=[],
            )
        self.assertLess(time.monotonic() - before, 1)
        self.assertTrue(was_cancelled)
        self.assertEqual(rows, [])
        fetch.assert_called_once()

    def test_cli_signals_cancel_and_restore_the_prior_handlers(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signum=signum):
                original = signal.getsignal(signum)

                def run(_args, *, cancel_check, signum=signum):
                    handler = signal.getsignal(signum)
                    handler(signum, None)
                    self.assertTrue(cancel_check())
                    raise RequestCancelled("interrupted")

                output = io.StringIO()
                with patch("market_sentinel_cli._run_polymarket_leaderboard", side_effect=run), redirect_stderr(output):
                    code = cli.run_polymarket_leaderboard(argparse.Namespace())
                self.assertEqual(code, 128 + int(signum))
                self.assertIs(signal.getsignal(signum), original)
                self.assertIn("status=cancelled", output.getvalue())

    def test_cli_cancellation_preserves_previous_output(self):
        cancelled = threading.Event()
        args = argparse.Namespace(state_db="", resume_on_failure=False, quiet=True, checkpoint="", output="existing.csv")

        def scan(*_args, **kwargs):
            cancelled.set()
            self.assertTrue(kwargs["cancel_check"]())
            return {"cancelled": True, "rows": [], "counts": {}}

        with (
            patch("market_sentinel_cli.build_polymarket_leaderboard_params", return_value={}),
            patch("market_sentinel_cli.polymarket_leaderboard_payload", side_effect=scan),
            patch("market_sentinel_cli.write_leaderboard_payload") as write,
            self.assertRaises(RequestCancelled),
        ):
            cli._run_polymarket_leaderboard(args, cancel_check=cancelled.is_set)
        write.assert_not_called()

    def test_failed_cancel_source_fails_closed_before_page_dispatch(self):
        with patch("web_api.data_api.get_leaderboard") as fetch:
            result = web_api.polymarket_leaderboard_payload({}, cancel_check=lambda: 1 / 0)
        self.assertTrue(result["cancelled"])
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
