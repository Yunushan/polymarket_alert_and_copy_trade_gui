from __future__ import annotations

from contextlib import redirect_stdout
from http.client import HTTPConnection
import io
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from core.models import AppConfig
from core.storage import load_config, save_config
from web_api import (
    HTTP_RESPONSE_CHUNK_BYTES,
    HttpClientDisconnected,
    ReactGuiHandler,
    ReactGuiServer,
    _ClientResponseWriter,
)


class MemoryClient:
    def __init__(self, request: bytes, failure_at: int = 0, error: Exception | None = None) -> None:
        self.reader = io.BytesIO(request)
        self.sent: list[bytes] = []
        self.attempts = 0
        self.failure_at = failure_at
        self.error = error or ConnectionResetError("client closed")

    def makefile(self, *_args):
        return self.reader

    def settimeout(self, _timeout):
        pass

    def sendall(self, data):
        self.attempts += 1
        if self.attempts == self.failure_at:
            raise self.error
        self.sent.append(bytes(data))


class HttpClientDisconnectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.config_path = root / "config.json"
        save_config(AppConfig(), self.config_path)
        frontend = root / "dist"
        frontend.mkdir()
        (frontend / "index.html").write_text("<html>local fixture</html>", encoding="utf-8")
        with patch("web_api._RESOURCE_ROOT", root):
            self.server = ReactGuiServer(
                ("127.0.0.1", 0), ReactGuiHandler,
                config_path=self.config_path, frontend_dir=frontend,
            )
        self.addCleanup(self.server.server_close)

    def request(self, path="/api/health", *, method="GET", payload=None, failure_at=0, error=None):
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        request = (
            f"{method} {path} HTTP/1.1\r\nHost: localhost\r\n"
            f"Content-Length: {len(body)}\r\nIdempotency-Key: disconnect-test\r\n\r\n"
        ).encode("ascii") + body
        client = MemoryClient(request, failure_at, error)
        output = io.StringIO()
        with redirect_stdout(output):
            ReactGuiHandler(client, ("127.0.0.1", 12345), self.server)
        logs = output.getvalue()
        events = [json.loads(line) for line in logs.splitlines() if line.startswith("{")]
        self.assertEqual(len(events), 1)
        self.assertTrue(client.reader.closed)
        return client, logs, events[0]

    def test_response_header_and_body_disconnects_stop_without_sending_a_second_response(self) -> None:
        for failure_at in (1, 2):
            for error_type in (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                with self.subTest(failure_at=failure_at, error=error_type.__name__):
                    client, logs, event = self.request(failure_at=failure_at, error=error_type("closed"))
                    self.assertEqual(client.attempts, failure_at)
                    self.assertNotIn("internal error", logs)
                    self.assertEqual(event["status"], 499)
                    self.assertEqual(event["response_status"], 200)
                    self.assertEqual(event["outcome"], "client_disconnected")
        self.assertIn('method="GET",status="499"} 6', self.server.http_metrics.prometheus_text())

    def test_static_and_stdlib_error_responses_use_the_same_disconnect_boundary(self) -> None:
        for path, method, response_status in (("/", "GET", 200), ("/", "PUT", 501)):
            with self.subTest(path=path, method=method):
                client, logs, event = self.request(path, method=method, failure_at=2)
                self.assertEqual(client.attempts, 2)
                self.assertNotIn("internal error", logs)
                self.assertEqual(event["status"], 499)
                self.assertEqual(event["response_status"], response_status)

    def test_partial_chunked_response_stops_at_the_failed_write(self) -> None:
        with patch("web_api.app_state_payload", return_value={"value": "x" * (3 * HTTP_RESPONSE_CHUNK_BYTES)}):
            client, logs, event = self.request("/api/state", failure_at=3)
        self.assertEqual(client.attempts, 3)
        self.assertEqual(len(client.sent), 2)
        self.assertEqual(len(client.sent[1]), HTTP_RESPONSE_CHUNK_BYTES)
        self.assertNotIn("internal error", logs)
        self.assertEqual(event["status"], 499)

    def test_flush_disconnect_is_classified_once_and_the_stream_can_be_closed(self) -> None:
        stream = io.BytesIO()
        writer = _ClientResponseWriter(stream)
        self.assertEqual(writer.write(b"partial"), 7)
        with patch.object(stream, "flush", side_effect=BrokenPipeError("closed")) as flush:
            for _attempt in range(2):
                with self.assertRaises(HttpClientDisconnected):
                    writer.flush()
            with self.assertRaises(HttpClientDisconnected):
                writer.write(b"must not be written")
            flush.assert_called_once_with()
        self.assertEqual(stream.getvalue(), b"partial")
        writer.close()
        self.assertTrue(writer.closed)

    def test_closed_response_socket_releases_worker_and_next_health_request_succeeds(self) -> None:
        class CloseOnceHandler(ReactGuiHandler):
            def _write_response_body(self, data):
                if self.headers.get("X-Test-Close"):
                    self.connection.shutdown(socket.SHUT_RDWR)
                super()._write_response_body(data)

        self.server.RequestHandlerClass = CloseOnceHandler
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        output = io.StringIO()
        with redirect_stdout(output):
            thread.start()
            try:
                with socket.create_connection(self.server.server_address, timeout=3) as client:
                    client.sendall(b"GET /api/health HTTP/1.1\r\nHost: localhost\r\nX-Test-Close: yes\r\n\r\n")
                    try:
                        while client.recv(65536):
                            pass
                    except ConnectionResetError:
                        pass
                deadline = time.monotonic() + 3
                while 'status="499"} 1' not in self.server.http_metrics.prometheus_text():
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.01)
                while self.server.http_metrics.snapshot()["requests_in_flight"]:
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.01)
                connection = HTTPConnection(*self.server.server_address, timeout=3)
                try:
                    connection.request("GET", "/api/health")
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertTrue(response.read())
                finally:
                    connection.close()
            finally:
                self.server.shutdown()
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
        self.assertNotIn("internal error", output.getvalue())

    def test_disconnect_after_commit_preserves_idempotent_mutation_and_releases_admission(self) -> None:
        payload = {"wallet": "0x" + "a" * 40, "display_name": "Local test"}
        client, logs, event = self.request(
            "/api/wallets", method="POST", payload=payload, failure_at=2,
        )
        self.assertEqual(client.attempts, 2)
        self.assertNotIn("internal error", logs)
        self.assertEqual(event["status"], 499)
        committed = self.config_path.read_bytes()
        self.assertEqual(len(load_config(self.config_path).wallets), 1)
        snapshot = self.server.http_metrics.snapshot()
        self.assertEqual(snapshot["mutations_in_flight"], 0)
        self.assertEqual(snapshot["mutations_active"], 0)
        self.assertTrue(self.server.wait_for_mutation_drain(0))
        _client, _logs, retry = self.request("/api/wallets", method="POST", payload=payload)
        self.assertEqual(retry["status"], 200)
        self.assertEqual(self.config_path.read_bytes(), committed)

    def test_upstream_connection_reset_is_still_an_internal_error(self) -> None:
        with patch("web_api.app_state_payload", side_effect=ConnectionResetError("upstream reset")):
            client, logs, event = self.request("/api/state")
        self.assertIn("internal error", logs)
        self.assertEqual(event["status"], 500)
        self.assertNotIn("outcome", event)
        self.assertIn(b"internal_error", b"".join(client.sent))

    def test_mutation_backend_connection_reset_is_not_a_client_disconnect(self) -> None:
        with patch("web_api.add_wallet_watch", side_effect=ConnectionResetError("backend reset")):
            _client, logs, event = self.request(
                "/api/wallets", method="POST", payload={"wallet": "0x" + "a" * 40},
            )
        self.assertIn("internal error", logs)
        self.assertEqual(event["status"], 500)
        self.assertNotIn("outcome", event)
        self.assertEqual(load_config(self.config_path).wallets, [])

    def test_disconnect_while_sending_a_backend_error_does_not_erase_that_error(self) -> None:
        with patch("web_api.app_state_payload", side_effect=RuntimeError("backend failure")):
            client, logs, event = self.request("/api/state", failure_at=2)
        self.assertIn("internal error", logs)
        self.assertEqual(client.attempts, 2)
        self.assertEqual(event["status"], 499)
        self.assertEqual(event["response_status"], 500)

    def test_unrelated_output_error_is_not_silenced_as_a_disconnect(self) -> None:
        _client, logs, event = self.request(failure_at=1, error=OSError("unexpected output failure"))
        self.assertIn("internal error", logs)
        self.assertEqual(event["status"], 500)
        self.assertNotIn("outcome", event)


if __name__ == "__main__":
    unittest.main()
