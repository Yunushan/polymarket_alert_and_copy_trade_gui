from __future__ import annotations

import os
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

from core.request_control import RequestCancelled, RequestDeadlineExceeded, cancellation_scope
from scripts import verify_production_deployment as deployment
from scripts.verify_service_health import open_probe, read_probe_body
from test_polymarket_http_transport import local_tls_server, resolver_for


@contextmanager
def probe_server(mode="normal", status=200):
    stop = threading.Event()
    received = []

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                if mode == "tls":
                    stop.wait(3)
                    return
                self.request.settimeout(1)
                request = b""
                while b"\r\n\r\n" not in request and len(request) < 65536:
                    chunk = self.request.recv(1024)
                    if not chunk:
                        return
                    request += chunk
                received.append(request)
                self.request.sendall(f"HTTP/1.1 {status} Response\r\n".encode())
                if mode == "headers":
                    self.request.sendall(b"X-Delay: ")
                    for _ in range(200):
                        if stop.wait(0.01):
                            return
                        self.request.sendall(b"x")
                    self.request.sendall(b"\r\n")
                body = b"x" * 200 if mode == "body" else b"{}"
                self.request.sendall(f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode())
                if mode == "body":
                    for byte in body:
                        if stop.wait(0.01):
                            return
                        self.request.sendall(bytes([byte]))
                else:
                    self.request.sendall(body)
            except OSError:
                pass

    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield server.server_address[1], received
        finally:
            stop.set()
            server.shutdown()
            thread.join(timeout=3)


class ProbeDeadlineTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {
            "HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": "",
            "http_proxy": "", "https_proxy": "", "all_proxy": "", "NO_PROXY": "*", "no_proxy": "*",
        })
        environment.start()
        self.addCleanup(environment.stop)

    def test_slow_headers_and_body_cannot_restart_the_overall_budget(self):
        for mode in ("headers", "body"):
            with self.subTest(mode=mode), probe_server(mode) as (port, _):
                before = time.monotonic()
                with self.assertRaises(RequestDeadlineExceeded):
                    with open_probe(Request(f"http://127.0.0.1:{port}"), 0.15) as response:
                        read_probe_body(response)
                self.assertLess(time.monotonic() - before, 1.5)

    def test_cancellation_interrupts_a_slow_response(self):
        cancelled = threading.Event()
        timer = threading.Timer(0.15, cancelled.set)
        with probe_server("body") as (port, _):
            timer.start()
            try:
                with cancellation_scope(cancelled.is_set), self.assertRaises(RequestCancelled):
                    with open_probe(Request(f"http://127.0.0.1:{port}"), 5) as response:
                        read_probe_body(response)
            finally:
                timer.cancel()
                timer.join(timeout=1)

    def test_stalled_dns_times_out_without_a_late_connection(self):
        release = threading.Event()
        done = threading.Event()
        with probe_server() as (port, received):
            def resolve(*_args, **_kwargs):
                try:
                    release.wait(2)
                    return resolver_for("127.0.0.1")("probe.example.test", port, type=socket.SOCK_STREAM)
                finally:
                    done.set()

            try:
                before = time.monotonic()
                with patch("socket.getaddrinfo", side_effect=resolve):
                    with self.assertRaises(RequestDeadlineExceeded):
                        with open_probe(Request(f"http://probe.example.test:{port}"), 0.15) as response:
                            read_probe_body(response)
                self.assertLess(time.monotonic() - before, 1.5)
            finally:
                release.set()
                self.assertTrue(done.wait(1))
            self.assertEqual(received, [])

    def test_stalled_tls_handshake_obeys_the_same_budget(self):
        with probe_server("tls") as (port, _):
            before = time.monotonic()
            with self.assertRaises(RequestDeadlineExceeded):
                with open_probe(Request(f"https://127.0.0.1:{port}"), 0.15):
                    self.fail("stalled TLS cannot return a response")
            self.assertLess(time.monotonic() - before, 1.5)

    def test_public_probe_rejects_private_or_mixed_dns_before_connecting(self):
        for addresses in (("127.0.0.1",), ("1.1.1.1", "10.0.0.1"), ("224.0.0.1",)):
            with self.subTest(addresses=addresses):
                with patch("socket.getaddrinfo", side_effect=resolver_for(*addresses)), patch("socket.socket.connect") as connect:
                    with self.assertRaisesRegex(RuntimeError, "public addresses"):
                        with open_probe(Request("https://venue.example.test"), 1, public_only=True):
                            self.fail("private resolution cannot be opened")
                    connect.assert_not_called()

    def test_every_public_deployment_probe_revalidates_rebound_dns(self):
        original_open = deployment.urlopen
        for attack_at in range(len(deployment.PUBLIC_PROXY_AUTH_PROBES) + 1):
            with self.subTest(attack_at=attack_at):
                requests = []

                def open_until_rebound(request, timeout, _requests=requests, _attack_at=attack_at, **kwargs):
                    _requests.append((request, kwargs))
                    if len(_requests) <= _attack_at:
                        raise HTTPError(request.full_url, 401, "Unauthorized", {}, None)
                    return original_open(request, timeout, **kwargs)

                public = resolver_for("1.1.1.1")("venue.example.test", 443, type=socket.SOCK_STREAM)
                private = resolver_for("127.0.0.1")("venue.example.test", 443, type=socket.SOCK_STREAM)
                with (
                    patch("socket.getaddrinfo", side_effect=[public, private]),
                    patch("scripts.verify_production_deployment.urlopen", side_effect=open_until_rebound),
                    patch("socket.socket.connect", side_effect=AssertionError("private connection attempted")) as connect,
                ):
                    with self.assertRaisesRegex(RuntimeError, "public addresses"):
                        deployment.check_public_proxy("https://venue.example.test", "operator", "test-only", 1,
                                                      upstream_token="test-only-api-token")
                connect.assert_not_called()
                self.assertEqual(len(requests), attack_at + 1)
                self.assertTrue(all(options.get("public_only") is True for _, options in requests))
                if attack_at == len(deployment.PUBLIC_PROXY_AUTH_PROBES):
                    self.assertTrue(requests[-1][0].get_header("Authorization").startswith("Basic "))

    def test_public_preflight_rejects_nonpublic_literals_and_dns(self):
        for address in ("127.0.0.1", "10.0.0.1", "100.64.0.1", "169.254.169.254", "224.0.0.1",
                        "192.0.2.1", "240.0.0.1", "::1", "ff02::1"):
            literal = f"[{address}]" if ":" in address else address
            with self.subTest(address=address), self.assertRaises(ValueError):
                deployment._validated_public_origin(f"https://{literal}")
            with self.subTest(dns=address), patch("socket.getaddrinfo", side_effect=resolver_for(address)):
                with self.assertRaises(ValueError):
                    deployment._validated_public_origin("https://venue.example.test")

    def test_public_preflight_dns_uses_the_callers_deadline(self):
        release = threading.Event()
        done = threading.Event()

        def resolve(*_args, **_kwargs):
            try:
                release.wait(2)
                return resolver_for("1.1.1.1")("venue.example.test", 443, type=socket.SOCK_STREAM)
            finally:
                done.set()

        try:
            before = time.monotonic()
            with (
                patch("socket.getaddrinfo", side_effect=resolve),
                patch("scripts.verify_production_deployment.urlopen") as opener,
            ):
                with self.assertRaises(RequestDeadlineExceeded):
                    deployment.check_public_proxy("https://venue.example.test", "operator", "test-only", 0.15,
                                                  upstream_token="test-only-api-token")
                opener.assert_not_called()
            self.assertLess(time.monotonic() - before, 1.5)
        finally:
            release.set()
            self.assertTrue(done.wait(1))

    def test_probe_ignores_inherited_proxies_and_uses_one_dns_answer(self):
        with probe_server() as (port, received):
            with (
                patch.dict(os.environ, {"http_proxy": "http://127.0.0.2:9", "HTTP_PROXY": "http://127.0.0.2:9",
                                        "no_proxy": "", "NO_PROXY": ""}),
                patch("socket.getaddrinfo", side_effect=resolver_for("127.0.0.1")) as resolver,
            ):
                with open_probe(Request(f"http://probe.example.test:{port}", headers={"Authorization": "Bearer test-only"}), 2) as response:
                    self.assertEqual(read_probe_body(response), b"{}")
            self.assertEqual(resolver.call_count, 1)
            self.assertEqual(resolver.call_args.args[0], "probe.example.test")
            self.assertIn(f"Host: probe.example.test:{port}".encode(), received[0])
            self.assertIn(b"Authorization: Bearer test-only", received[0])

    def test_pinned_tls_keeps_hostname_validation_and_sni(self):
        with tempfile.TemporaryDirectory() as directory, local_tls_server(directory) as (port, ca_file, observed):
            context = ssl.create_default_context(cafile=str(ca_file))
            with (
                patch("core.probe_transport.ssl.create_default_context", return_value=context),
                patch("socket.getaddrinfo", side_effect=resolver_for("127.0.0.1")) as resolver,
            ):
                with open_probe(Request(f"https://venue.example.test:{port}/data"), 2) as response:
                    self.assertEqual(read_probe_body(response), b'{"ok":true}')
            self.assertEqual(resolver.call_count, 1)
            self.assertEqual(observed["hosts"], [f"venue.example.test:{port}"])
            self.assertEqual(observed["sni"], ["venue.example.test"])

    def test_pinned_tls_rejects_untrusted_ca_and_wrong_hostname(self):
        for wrong_host in (False, True):
            with self.subTest(wrong_host=wrong_host), tempfile.TemporaryDirectory() as directory:
                with local_tls_server(directory) as (port, ca_file, observed):
                    context = ssl.create_default_context(cafile=str(ca_file) if wrong_host else None)
                    host = "wrong.example.test" if wrong_host else "venue.example.test"
                    with (
                        patch("core.probe_transport.ssl.create_default_context", return_value=context),
                        patch("socket.getaddrinfo", side_effect=resolver_for("127.0.0.1")),
                    ):
                        with self.assertRaises(URLError) as raised:
                            open_probe(Request(f"https://{host}:{port}/data"), 2)
                    self.assertIsInstance(raised.exception.reason, ssl.SSLCertVerificationError)
                    self.assertEqual(observed["paths"], [])

    def test_refused_address_falls_back_within_one_deadline(self):
        connect = socket.socket.connect

        def refuse_first(sock, endpoint):
            # Inject a refusal rather than relying on OS loopback retransmission timing.
            if endpoint[0] == "127.0.0.2":
                raise ConnectionRefusedError("test-only refusal")
            return connect(sock, endpoint)

        with (
            probe_server() as (port, received),
            patch("socket.getaddrinfo", side_effect=resolver_for("127.0.0.2", "127.0.0.1")) as resolver,
            patch("socket.socket.connect", new=refuse_first),
        ):
            with open_probe(Request(f"http://probe.example.test:{port}"), 2) as response:
                self.assertEqual(read_probe_body(response), b"{}")
            self.assertTrue(response._control._closed)
            self.assertEqual(response._control._sockets, {})
            response.close()
        self.assertEqual(resolver.call_count, 1)
        self.assertEqual(len(received), 1)

    def test_error_responses_release_the_deadline_controller(self):
        from core.request_control import RequestControl

        controls = []

        def create_control(timeout):
            control = RequestControl(timeout)
            controls.append(control)
            return control

        with probe_server(status=401) as (port, _), patch(
            "core.probe_transport.RequestControl", side_effect=create_control
        ):
            with self.assertRaises(HTTPError) as raised:
                open_probe(Request(f"http://127.0.0.1:{port}"), 2)
        self.assertEqual(raised.exception.code, 401)
        self.assertTrue(raised.exception.fp.closed)
        self.assertTrue(controls[0]._closed)
        self.assertEqual(controls[0]._sockets, {})

    def test_early_os_timeouts_are_reported_as_deadlines(self):
        with patch("core.probe_transport.build_opener") as build:
            build.return_value.open.side_effect = URLError(TimeoutError("test-only early socket timeout"))
            with self.assertRaises(RequestDeadlineExceeded):
                open_probe(Request("https://venue.example.test"), 2)
        with probe_server() as (port, _):
            with open_probe(Request(f"http://127.0.0.1:{port}"), 2) as response:
                with patch.object(response._response, "read", side_effect=TimeoutError("test-only read timeout")):
                    with self.assertRaises(RequestDeadlineExceeded):
                        response.read()

    def test_probe_controller_is_available_without_site_packages(self):
        result = subprocess.run(
            [sys.executable, "-S", "-c", "from core.request_control import RequestControl; from core.probe_transport import open_probe; c=RequestControl(1); c.close()"],
            cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
