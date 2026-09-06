from __future__ import annotations

import contextlib
import io
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import verify_browser_workflows as workflows


class BrowserWorkflowRunnerTests(unittest.TestCase):
    def test_missing_prerequisites_fail_before_server_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(workflows, "ROOT", Path(temporary)):
            with patch.object(workflows, "ReactGuiServer") as server, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    workflows.main(["--node", "node"])
                self.assertEqual(raised.exception.code, 2)
                server.assert_not_called()

    def _exercise_runner(self, outcome: str) -> None:
        old_connect = socket.socket.connect
        prior_path = os.environ.get("POLYMARKET_ANALYTICS_CACHE_PATH")
        captured = {}
        servers = []
        real_server = workflows.ReactGuiServer
        real_run = subprocess.run

        def create_server(*args, **kwargs):
            snapshot = kwargs["frontend_dir"]
            original = workflows.ROOT / "frontend" / "dist"
            self.assertNotEqual(snapshot, original)
            self.assertEqual((snapshot / "index.html").read_text(encoding="utf-8"), "original build")
            (original / "index.html").write_text("replacement build", encoding="utf-8")
            self.assertEqual((snapshot / "index.html").read_text(encoding="utf-8"), "original build")
            server = real_server(*args, **kwargs)
            servers.append(server)
            return server

        def run(command, **kwargs):
            if command[0] != "node":
                return real_run(command, **kwargs)
            captured.update(kwargs)
            self.assertEqual(command[-1], "--project=desktop-dark")
            self.assertTrue(kwargs["env"]["MARKET_SENTINEL_BROWSER_TEST_ORIGIN"].startswith("http://127.0.0.1:"))
            if outcome == "egress":
                with socket.socket() as probe, self.assertRaises(PermissionError):
                    probe.connect_ex(("192.0.2.1", 443))
            if outcome == "timeout":
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            return subprocess.CompletedProcess(command, 7 if outcome == "failure" else 0)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in ("frontend/dist/index.html", "frontend/node_modules/@playwright/test/cli.js"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("original build", encoding="utf-8")
            with (
                patch.object(workflows, "ROOT", root),
                patch("web_api._RESOURCE_ROOT", root),
                patch.object(workflows, "ReactGuiServer", side_effect=create_server),
                patch.object(workflows.subprocess, "run", side_effect=run),
            ):
                if outcome == "timeout":
                    with self.assertRaises(subprocess.TimeoutExpired):
                        workflows.main(["--node", "node", "--project=desktop-dark"])
                elif outcome == "egress":
                    with self.assertRaisesRegex(RuntimeError, "attempted 1 outbound"):
                        workflows.main(["--node", "node", "--project=desktop-dark"])
                else:
                    self.assertEqual(workflows.main(["--node", "node", "--project=desktop-dark"]), 7)
        self.assertEqual(servers[0].fileno(), -1)
        self.assertFalse(Path(captured["env"]["POLYMARKET_ANALYTICS_CACHE_PATH"]).parent.exists())
        self.assertIs(socket.socket.connect, old_connect)
        self.assertEqual(os.environ.get("POLYMARKET_ANALYTICS_CACHE_PATH"), prior_path)

    def test_failure_code_propagates_and_resources_are_released(self) -> None:
        self._exercise_runner("failure")

    def test_timeout_releases_server_and_temporary_state(self) -> None:
        self._exercise_runner("timeout")

    def test_attempted_backend_egress_fails_even_if_browser_process_returns_success(self) -> None:
        self._exercise_runner("egress")


if __name__ == "__main__":
    unittest.main()
