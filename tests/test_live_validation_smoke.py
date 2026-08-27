from __future__ import annotations

import io
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

from scripts import verify_live_validation_report_smoke as live_smoke


class LiveValidationSmokeHttpTests(unittest.TestCase):
    def test_request_json_forwards_idempotency_key(self) -> None:
        captured = {}

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            @staticmethod
            def read() -> bytes:
                return b'{"ok":true}'

        def fake_urlopen(request, timeout):
            captured["idempotency_key"] = request.get_header("Idempotency-key")
            captured["timeout"] = timeout
            return Response()

        with patch.object(live_smoke, "urlopen", side_effect=fake_urlopen):
            status, payload = live_smoke.request_json(
                "https://example.test",
                "/durable-create",
                method="POST",
                payload={"value": 1},
                idempotency_key="browser-smoke-test-key-v1",
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True})
        self.assertEqual(captured["idempotency_key"], "browser-smoke-test-key-v1")
        self.assertEqual(captured["timeout"], 10)

    def test_request_json_closes_http_error_response(self) -> None:
        body = io.BytesIO(b'{"error":"unauthorized"}')
        error = HTTPError("https://example.test/state", 401, "Unauthorized", {}, body)

        with patch.object(live_smoke, "urlopen", side_effect=error):
            status, payload = live_smoke.request_json("https://example.test", "/state")

        self.assertEqual(status, 401)
        self.assertEqual(payload, {"error": "unauthorized"})
        self.assertTrue(body.closed)

    def test_request_raw_closes_http_error_response(self) -> None:
        body = io.BytesIO(b"forbidden")
        error = HTTPError("https://example.test/raw", 403, "Forbidden", {}, body)

        with patch.object(live_smoke, "urlopen", side_effect=error):
            status, payload = live_smoke.request_raw("https://example.test", "/raw")

        self.assertEqual(status, 403)
        self.assertEqual(payload, b"forbidden")
        self.assertTrue(body.closed)
