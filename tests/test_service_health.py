from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from scripts.verify_service_health import check_health


class _Response:
    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class ServiceHealthTests(unittest.TestCase):
    def test_check_health_accepts_a_versioned_ok_response(self) -> None:
        response = _Response(200, {"status": "ok", "api_version": "1.0.10"})
        with patch("scripts.verify_service_health.urlopen", return_value=response):
            payload = check_health("http://127.0.0.1:8765/api/health", "", 1.0)

        self.assertEqual(payload["api_version"], "1.0.10")

    def test_check_health_rejects_missing_or_unknown_version(self) -> None:
        for version in (None, "", "unknown"):
            with self.subTest(version=version):
                response = _Response(200, {"status": "ok", "api_version": version})
                with patch("scripts.verify_service_health.urlopen", return_value=response):
                    with self.assertRaisesRegex(RuntimeError, "usable api_version"):
                        check_health("http://127.0.0.1:8765/api/health", "", 1.0)

    def test_check_health_rejects_a_versioned_but_degraded_service(self) -> None:
        response = _Response(
            200,
            {
                "status": "ok",
                "api_version": "1.0.11",
                "readiness": {"ready": False, "status": "degraded"},
            },
        )
        with patch("scripts.verify_service_health.urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "readiness=degraded"):
                check_health("http://127.0.0.1:8765/api/health", "", 1.0)

    def test_check_health_accepts_explicit_ready_state(self) -> None:
        response = _Response(
            200,
            {
                "status": "ok",
                "api_version": "1.0.11",
                "readiness": {"ready": True, "status": "ready"},
            },
        )
        with patch("scripts.verify_service_health.urlopen", return_value=response):
            payload = check_health("http://127.0.0.1:8765/api/health", "", 1.0)

        self.assertTrue(payload["readiness"]["ready"])


if __name__ == "__main__":
    unittest.main()
