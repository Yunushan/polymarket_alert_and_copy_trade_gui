from __future__ import annotations

import io
import json
import socket
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler
from unittest.mock import Mock, patch

from scripts import verify_production_deployment as deployment
from scripts.verify_service_health import check_health, open_probe, read_health_payload, read_probe_body


MAX_BODY_BYTES = 1024 * 1024
HEALTH = {"status": "ok", "api_version": "1.0.12", "readiness": {"ready": True}}
METRICS = b"\n".join(name.encode() + b" 1" for name in (
    "market_sentinel_http_requests_total",
    "market_sentinel_http_request_duration_seconds_total",
    "market_sentinel_http_requests_completed_total",
))


@contextmanager
def serve(*, status=200, body=b"", headers=None, send_length=True):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(dict(self.headers))
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            if send_length:
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/api/health", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class ProbeTransportTests(unittest.TestCase):
    def test_308_redirect_is_rejected_without_a_stdlib_308_handler(self):
        with serve(body=json.dumps(HEALTH).encode()) as (target, received):
            with serve(status=308, headers={"Location": target}) as (origin, _):
                with patch.object(HTTPRedirectHandler, "http_error_308", None, create=True):
                    with self.assertRaisesRegex(RuntimeError, "redirects are forbidden"):
                        check_health(origin, "test-only-token", 2.0)
            self.assertEqual(received, [])

    def test_body_reader_bounds_unannounced_and_understated_payloads(self):
        for headers in ({}, {"Content-Length": "10"}):
            with self.subTest(headers=headers):
                response = Mock(headers=headers, read=Mock(return_value=b"x" * 65))
                with patch("scripts.verify_service_health.MAX_PROBE_RESPONSE_BYTES", 64):
                    with self.assertRaisesRegex(RuntimeError, "response.*limit"):
                        read_probe_body(response)
                response.read.assert_called_once_with(65)

    def test_unannounced_real_body_is_bounded(self):
        with serve(body=b"x" * 1024, send_length=False) as (url, _):
            with patch("scripts.verify_service_health.MAX_PROBE_RESPONSE_BYTES", 64):
                with self.assertRaisesRegex(RuntimeError, "response.*limit"):
                    check_health(url, "", 2.0)

    def test_body_reader_rejects_invalid_or_excessive_lengths_before_reading(self):
        for value in ("invalid", "-1", str(MAX_BODY_BYTES + 1)):
            with self.subTest(value=value):
                response = Mock(headers={"Content-Length": value})
                with self.assertRaises(RuntimeError):
                    read_probe_body(response)
                response.read.assert_not_called()

    def test_body_reader_accepts_the_exact_limit(self):
        response = Mock(headers={}, read=Mock(return_value=b"x" * 64))
        with patch("scripts.verify_service_health.MAX_PROBE_RESPONSE_BYTES", 64):
            self.assertEqual(read_probe_body(response), b"x" * 64)

    def test_invalid_socket_timeouts_fail_before_opening(self):
        for value in (True, 0, -1, float("nan"), float("inf")):
            with self.subTest(value=value), patch("core.probe_transport.build_opener") as opener:
                with self.assertRaises(ValueError):
                    open_probe(None, value)
                opener.assert_not_called()

    def test_invalid_health_json_is_a_controlled_failure(self):
        for body in (b"not JSON", b"\xff", b"[" * 10000):
            with self.subTest(body=body[:10]):
                response = io.BytesIO(body)
                response.status = 200
                with self.assertRaisesRegex(RuntimeError, "invalid JSON"):
                    read_health_payload(response)

    def test_health_redirect_does_not_send_token_to_another_origin(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status), serve(body=json.dumps(HEALTH).encode()) as (target, received):
                with serve(status=status, headers={"Location": target}) as (origin, _):
                    with self.assertRaisesRegex(RuntimeError, "redirects are forbidden"):
                        check_health(origin, "test-only-token", 2.0)
                self.assertEqual(received, [])

    def test_metrics_redirect_does_not_send_token_to_another_origin(self):
        with serve(body=METRICS, headers={"Content-Type": "text/plain; version=0.0.4"}) as (target, received):
            with serve(status=302, headers={"Location": target}) as (origin, _):
                with self.assertRaisesRegex(RuntimeError, "redirects are forbidden"):
                    deployment.check_loopback_metrics(origin, "test-only-token", 2.0)
            self.assertEqual(received, [])

    def test_redirect_to_a_401_does_not_prove_original_endpoint_authentication(self):
        with serve(status=401) as (target, received):
            with serve(status=302, headers={"Location": target}) as (origin, _):
                with self.assertRaisesRegex(RuntimeError, "redirects are forbidden"):
                    deployment.check_loopback_token_auth(origin, 2.0)
            self.assertEqual(received, [])

    def test_health_body_limit_is_enforced_on_real_responses(self):
        payload = {**HEALTH, "padding": "x" * MAX_BODY_BYTES}
        with serve(body=json.dumps(payload).encode()) as (url, _):
            with self.assertRaisesRegex(RuntimeError, "response.*limit"):
                check_health(url, "", 2.0)

    def test_metrics_body_limit_is_enforced_on_real_responses(self):
        with serve(body=METRICS + b"\n#" + b"x" * MAX_BODY_BYTES,
                   headers={"Content-Type": "text/plain; version=0.0.4"}) as (url, _):
            with self.assertRaisesRegex(RuntimeError, "response.*limit"):
                deployment.check_loopback_metrics(url, "", 2.0)

    def test_direct_health_and_metrics_probes_preserve_authentication(self):
        with serve(body=json.dumps(HEALTH).encode()) as (url, received):
            self.assertEqual(check_health(url, "test-only-token", 2.0), HEALTH)
            self.assertEqual(received[0]["Authorization"], "Bearer test-only-token")
        with serve(body=METRICS, headers={"Content-Type": "text/plain; version=0.0.4"}) as (url, received):
            self.assertEqual(deployment.check_loopback_metrics(url, "test-only-token", 2.0)["status"], "pass")
            self.assertEqual(received[0]["Authorization"], "Bearer test-only-token")

    def test_public_probe_rejects_invalid_health_instead_of_attesting_it(self):
        headers = {name: "; ".join(values) for name, values in deployment.REQUIRED_PROXY_HEADER_VALUES.items()}
        headers["cache-control"] = "no-store"
        invalid = (
            {**HEALTH, "readiness": {"ready": False, "status": "degraded"}},
            {**HEALTH, "readiness": None},
            {**HEALTH, "readiness": {"ready": "true"}},
            {**HEALTH, "api_version": "unknown"},
            [],
        )
        for payload in invalid:
            with self.subTest(payload=payload):
                response = io.BytesIO(json.dumps(payload).encode())
                response.status = 200
                response.headers = headers
                unauthorized = [HTTPError("https://analytics.example.com", 401, "Unauthorized", {}, io.BytesIO())
                                for _ in deployment.PUBLIC_PROXY_AUTH_PROBES]
                with (
                    patch("scripts.verify_production_deployment.socket.getaddrinfo", return_value=[
                        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("1.1.1.1", 443)),
                    ]),
                    patch("scripts.verify_production_deployment.urlopen", side_effect=unauthorized + [response]),
                ):
                    with self.assertRaises(RuntimeError):
                        deployment.check_public_proxy("https://analytics.example.com", "user", "test-only-password",
                                                      2.0, upstream_token="test-only-token")
                self.assertTrue(response.closed)


if __name__ == "__main__":
    unittest.main()
