from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import websocket

from scripts.verify_mobile_web_smoke import (
    MOBILE_TARGETS,
    BrowserRenderTimeoutError,
    BrowserStartupError,
    _cdp_call,
    browser_mobile_check,
)


class FakeWebSocket:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.sent: list[dict[str, object]] = []
        self.timeouts: list[float] = []

    def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    def settimeout(self, timeout: float) -> None:
        self.timeouts.append(timeout)

    def recv(self) -> str:
        if not self.responses:
            raise websocket.WebSocketTimeoutException("no response")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return str(response)


class MobileWebSmokeCdpTests(unittest.TestCase):
    def test_cdp_call_retries_transient_recv_timeout(self) -> None:
        ws = FakeWebSocket(
            [
                websocket.WebSocketTimeoutException("slow"),
                json.dumps({"method": "Runtime.consoleAPICalled"}),
                json.dumps({"id": 7, "result": {}}),
            ]
        )

        response = _cdp_call(
            ws,
            command_id=7,
            method="Page.enable",
            params=None,
            overall_deadline=time.monotonic() + 5,
            target_name="android-16",
            headless_arg="--headless=new",
        )

        self.assertEqual(response["id"], 7)
        self.assertEqual(ws.sent[0]["method"], "Page.enable")
        self.assertGreaterEqual(len(ws.timeouts), 2)

    def test_cdp_call_timeout_is_browser_startup_error(self) -> None:
        ws = FakeWebSocket([])

        with self.assertRaisesRegex(BrowserStartupError, "timed out waiting for CDP response to Page.enable"):
            _cdp_call(
                ws,
                command_id=1,
                method="Page.enable",
                params=None,
                overall_deadline=time.monotonic() - 1,
                target_name="android-16",
                headless_arg="--headless=new",
            )


class MobileWebSmokeRetryTests(unittest.TestCase):
    @patch("scripts.verify_mobile_web_smoke._browser_mobile_check_once")
    def test_render_timeout_retries_same_headless_mode(self, check_once: object) -> None:
        check_once.side_effect = [BrowserRenderTimeoutError("render timed out"), {"title": "MarketSentinel"}]

        with tempfile.TemporaryDirectory() as tmpdir:
            result = browser_mobile_check(
                "browser",
                "http://127.0.0.1/",
                Path(tmpdir) / "profile",
                "android-15",
                MOBILE_TARGETS["android-15"],
                startup_timeout_seconds=10,
                render_timeout_seconds=20,
            )

        self.assertEqual(result, {"title": "MarketSentinel"})
        self.assertEqual(check_once.call_count, 2)
        self.assertEqual(check_once.call_args_list[0].kwargs["headless_arg"], "--headless=new")
        self.assertEqual(check_once.call_args_list[1].kwargs["headless_arg"], "--headless=new")

    @patch("scripts.verify_mobile_web_smoke._browser_mobile_check_once")
    def test_render_timeout_exhausts_all_modes_and_attempts(self, check_once: object) -> None:
        check_once.side_effect = BrowserRenderTimeoutError("render timed out")

        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(SystemExit, "Mobile browser smoke exhausted all attempts"):
                browser_mobile_check(
                    "browser",
                    "http://127.0.0.1/",
                    Path(tmpdir) / "profile",
                    "android-15",
                    MOBILE_TARGETS["android-15"],
                    startup_timeout_seconds=10,
                    render_timeout_seconds=20,
                )

        self.assertEqual(check_once.call_count, 4)


if __name__ == "__main__":
    unittest.main()
