from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
import requests
from requests.adapters import HTTPAdapter
import urllib3.util.connection

from core.request_control import RequestCancelled, RequestDeadlineExceeded, cancellation_scope
from market_adapters.errors import MarketConfigurationError
from market_adapters.outbound import OutboundEndpointPolicy
from market_adapters.runtime import create_managed_http_session
from polymarket.endpoints import PolymarketEndpoint
from polymarket.http_client import (
    PolymarketHTTPError,
    PolymarketResponseError,
    PolymarketValidationError,
    RetryPolicy,
    request_bytes,
    request_json,
)


def resolver_for(*addresses):
    def resolve(_host, port, *, type):
        return [
            (socket.AF_INET, type, socket.IPPROTO_TCP, "", (address, port))
            for address in addresses
        ]
    return resolve


def response_for(status=200, body=b'{"ok":true}', headers=None):
    response = requests.Response()
    response.status_code = status
    response.url = "https://venue.example.test/data"
    response.request = requests.Request("GET", response.url).prepare()
    response.headers.update(headers or {})
    response._content = body
    response._content_consumed = True
    response.close = Mock(wraps=response.close)
    response.close_spy = response.close
    return response


@contextmanager
def local_tls_server(directory, handle_get=None):
    # Trust only an ephemeral CA in the test session. The leaf has a DNS SAN
    # but no IP SAN, so success proves hostname verification survives pinning.
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "venue.example.test")])
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"Test CA {x509.random_serial_number():x}")])
    now = datetime.now(timezone.utc)
    ca_certificate = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(True, False, False, False, False, True, True, False, False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(True, False, True, False, False, False, False, False, False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("venue.example.test")]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    ca_path = Path(directory) / "ca.pem"
    ca_path.write_bytes(ca_certificate.public_bytes(serialization.Encoding.PEM))
    certificate_path = Path(directory) / "server.pem"
    key_path = Path(directory) / "server.key"
    certificate_path.write_bytes(
        certificate.public_bytes(serialization.Encoding.PEM) + ca_certificate.public_bytes(serialization.Encoding.PEM)
    )
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ))
    observed = {"hosts": [], "sni": [], "paths": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib handler API
            observed["hosts"].append(self.headers.get("Host"))
            observed["paths"].append(self.path)
            if handle_get is not None:
                handle_get(self)
                return
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    # Application bootstrap may inject truststore's client verifier. Only the
    # fixture server uses its underlying stdlib server context; clients retain
    # the application's normal certificate and hostname verification.
    context = getattr(context, "_ctx", context)
    context.load_cert_chain(certificate_path, key_path)
    context.set_servername_callback(lambda _socket, server_name, _context: observed["sni"].append(server_name))
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield server.server_port, ca_path, observed
        finally:
            server.shutdown()
            thread.join(timeout=5)


class PolymarketHTTPTransportTests(unittest.TestCase):
    endpoint = PolymarketEndpoint("test", "GET", "/data", "https://venue.example.test")
    retry = RetryPolicy(max_attempts=2, backoff_seconds=0, max_sleep_seconds=0)

    @contextmanager
    def managed_transport(self, policy=None, certificate=None):
        sessions = []

        def create_session():
            session = create_managed_http_session(
                outbound_policy=policy or OutboundEndpointPolicy(resolver=resolver_for("93.184.216.34"))
            )
            session.trust_env = False
            if certificate is not None:
                session.verify = str(certificate)
            session.close = Mock(wraps=session.close)
            sessions.append(session)
            return session

        with patch("market_adapters.runtime.create_managed_http_session", side_effect=create_session):
            try:
                yield sessions
            finally:
                for session in sessions:
                    session.close.assert_called_once_with()
                    for adapter in session.adapters.values():
                        self.assertEqual(len(adapter.poolmanager.pools), 0)

    def test_real_tls_connection_uses_numeric_address_and_preserves_host_and_sni(self) -> None:
        with tempfile.TemporaryDirectory() as directory, local_tls_server(directory) as (port, certificate, observed):
            origin = f"https://venue.example.test:{port}"
            policy = OutboundEndpointPolicy(
                private_origins=frozenset({origin}), resolver=resolver_for("127.0.0.1")
            )
            original_resolver = socket.getaddrinfo

            def numeric_dns_only(host, *args, **kwargs):
                self.assertNotEqual(host, "venue.example.test", "socket must not repeat the hostname lookup")
                return original_resolver(host, *args, **kwargs)

            with (
                self.managed_transport(policy, certificate) as sessions,
                patch("socket.getaddrinfo", side_effect=numeric_dns_only),
                patch.object(urllib3.util.connection, "create_connection", wraps=urllib3.util.connection.create_connection) as connect,
            ):
                endpoint = PolymarketEndpoint("test", "GET", "/data", origin)
                self.assertEqual(request_json(endpoint, params={"page": 1}, timeout=2), {"ok": True})
                self.assertEqual(len(sessions), 1)
            self.assertEqual([call.args[0][0] for call in connect.call_args_list], ["127.0.0.1"])
            self.assertEqual(observed["hosts"], [f"venue.example.test:{port}"])
            self.assertEqual(observed["sni"], ["venue.example.test"])
            self.assertEqual(observed["paths"], ["/data?page=1"])

    def test_tls_rejects_untrusted_certificate_and_wrong_hostname(self) -> None:
        with tempfile.TemporaryDirectory() as directory, local_tls_server(directory) as (port, certificate, observed):
            for host, trusted_certificate in (("venue.example.test", None), ("wrong.example.test", certificate)):
                with self.subTest(host=host):
                    origin = f"https://{host}:{port}"
                    policy = OutboundEndpointPolicy(
                        private_origins=frozenset({origin}), resolver=resolver_for("127.0.0.1")
                    )
                    with self.managed_transport(policy, trusted_certificate), self.assertRaises(PolymarketHTTPError) as raised:
                        request_json(
                            PolymarketEndpoint("test", "GET", "/data", origin),
                            timeout=2, retry_policy=RetryPolicy(max_attempts=1),
                        )
                    self.assertIsInstance(raised.exception.__cause__, requests.exceptions.SSLError)
            self.assertEqual(observed["hosts"], [])

    def test_real_tls_slow_body_obeys_deadline_and_cancellation(self) -> None:
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                stop = threading.Event()
                cancelled = threading.Event()

                def handle(handler, cancel=cancel, cancelled=cancelled, stop=stop):
                    handler.send_response(200)
                    handler.send_header("Content-Length", "10000")
                    handler.end_headers()
                    if cancel:
                        cancelled.set()
                    try:
                        while not stop.wait(0.03):
                            handler.wfile.write(b"a")
                            handler.wfile.flush()
                    except OSError:
                        pass
                    handler.close_connection = True

                with tempfile.TemporaryDirectory() as directory, local_tls_server(directory, handle) as (port, certificate, _):
                    origin = f"https://venue.example.test:{port}"
                    policy = OutboundEndpointPolicy(private_origins=frozenset({origin}), resolver=resolver_for("127.0.0.1"))
                    try:
                        with self.managed_transport(policy, certificate), cancellation_scope(cancelled.is_set):
                            before = time.monotonic()
                            with self.assertRaises(RequestCancelled if cancel else PolymarketHTTPError) as raised:
                                request_json(PolymarketEndpoint("test", "GET", "/data", origin), timeout=2 if cancel else 0.5)
                            self.assertLess(time.monotonic() - before, 1.5)
                            if not cancel:
                                self.assertIsInstance(raised.exception.__cause__, RequestDeadlineExceeded)
                    finally:
                        stop.set()

    def test_retry_connects_to_newly_validated_dns_address(self) -> None:
        with tempfile.TemporaryDirectory() as directory, local_tls_server(directory) as (port, certificate, observed):
            calls = []

            def changing_dns(host, dns_port, *, type):
                calls.append(host)
                address = "127.0.0.2" if len(calls) <= 2 else "127.0.0.1"
                return resolver_for(address)(host, dns_port, type=type)

            origin = f"https://venue.example.test:{port}"
            policy = OutboundEndpointPolicy(private_origins=frozenset({origin}), resolver=changing_dns)
            real_connect = urllib3.util.connection.create_connection

            def refuse_retired_address(address, *args, **kwargs):
                if address[0] == "127.0.0.2":
                    raise ConnectionRefusedError("retired test address")
                return real_connect(address, *args, **kwargs)

            with (
                self.managed_transport(policy, certificate) as sessions,
                patch.object(urllib3.util.connection, "create_connection", side_effect=refuse_retired_address) as connect,
            ):
                result = request_json(
                    PolymarketEndpoint("test", "GET", "/data", origin), timeout=1, retry_policy=self.retry
                )
                self.assertEqual(result, {"ok": True})
                self.assertEqual(len(sessions), 1)
            self.assertEqual([call.args[0][0] for call in connect.call_args_list], ["127.0.0.2", "127.0.0.1"])
            self.assertEqual(len(calls), 4)
            self.assertEqual(len(observed["hosts"]), 1)

    def test_private_mixed_and_rebound_dns_fail_before_transport(self) -> None:
        for answer_sets in (
            [("127.0.0.1",)],
            [("93.184.216.34", "169.254.169.254")],
            [("93.184.216.34",), ("127.0.0.1",)],
        ):
            with self.subTest(answers=answer_sets):
                remaining = iter(answer_sets)
                policy = OutboundEndpointPolicy(
                    resolver=lambda host, port, *, type, remaining=remaining: resolver_for(*next(remaining))(host, port, type=type)
                )
                with self.managed_transport(policy), patch.object(HTTPAdapter, "send") as send:
                    with self.assertRaisesRegex(PolymarketValidationError, "non-public"):
                        request_json(self.endpoint, retry_policy=self.retry)
                    send.assert_not_called()

    def test_private_literal_and_invalid_factory_policy_fail_closed(self) -> None:
        with self.managed_transport(), patch.object(HTTPAdapter, "send") as send:
            with self.assertRaises(PolymarketValidationError):
                request_json(PolymarketEndpoint("test", "GET", "/", "https://127.0.0.1"))
            send.assert_not_called()
        with patch("market_adapters.runtime.create_managed_http_session", side_effect=MarketConfigurationError("policy")):
            with self.assertRaisesRegex(PolymarketValidationError, "policy"):
                request_json(self.endpoint)

    def test_redirect_is_closed_and_not_followed_or_retried(self) -> None:
        response = response_for(302, headers={"Location": "https://127.0.0.1/private"})
        response.iter_content = Mock(side_effect=AssertionError("redirect body must not be consumed"))
        with self.managed_transport(), patch.object(HTTPAdapter, "send", return_value=response) as send:
            with self.assertRaisesRegex(PolymarketHTTPError, "redirects are disabled") as raised:
                request_json(self.endpoint, retry_policy=self.retry)
            self.assertEqual(raised.exception.status_code, 302)
            send.assert_called_once()
        response.close.assert_called_once_with()

    def test_dns_failures_stop_initial_request_and_retry(self) -> None:
        for fail_on_attempt in (1, 3):
            with self.subTest(fail_on_attempt=fail_on_attempt):
                calls = []

                def resolve(host, port, *, type, fail_on_attempt=fail_on_attempt, calls=calls):
                    calls.append(host)
                    if len(calls) >= fail_on_attempt:
                        raise socket.gaierror("resolver unavailable")
                    return resolver_for("93.184.216.34")(host, port, type=type)

                response = response_for(503, headers={"Retry-After": "0"})
                with (
                    self.managed_transport(OutboundEndpointPolicy(resolver=resolve)),
                    patch.object(HTTPAdapter, "send", return_value=response) as send,
                ):
                    with self.assertRaisesRegex(PolymarketValidationError, "resolved"):
                        request_json(self.endpoint, retry_policy=self.retry)
                    self.assertEqual(send.call_count, 0 if fail_on_attempt == 1 else 1)
                self.assertEqual(response.close_spy.call_count, 0 if fail_on_attempt == 1 else 1)

    def test_retry_response_and_session_lifetimes(self) -> None:
        first = response_for(503, headers={"Retry-After": "0"})
        second = response_for()
        with self.managed_transport() as sessions, patch.object(HTTPAdapter, "send", side_effect=[first, second]) as send:
            original_iter = second.iter_content

            def consume(*args, **kwargs):
                self.assertFalse(sessions[0].close.called)
                self.assertTrue(first.close_spy.called)
                yield from original_iter(*args, **kwargs)

            second.iter_content = consume
            self.assertEqual(request_json(self.endpoint, retry_policy=self.retry), {"ok": True})
            self.assertEqual(send.call_count, 2)
            self.assertEqual(len(sessions), 1)
        first.close_spy.assert_called_once_with()
        second.close_spy.assert_called_once_with()

    def test_stream_failure_retries_only_safe_methods(self) -> None:
        for method, expected_calls in (("GET", 2), ("POST", 1)):
            with self.subTest(method=method):
                failed = response_for()
                failed.iter_content = Mock(side_effect=requests.exceptions.ChunkedEncodingError("interrupted"))
                recovered = response_for()
                endpoint = PolymarketEndpoint("test", method, "/data", self.endpoint.base_url)
                with self.managed_transport(), patch.object(HTTPAdapter, "send", side_effect=[failed, recovered]) as send:
                    if method == "GET":
                        self.assertEqual(request_json(endpoint, retry_policy=self.retry), {"ok": True})
                    else:
                        with self.assertRaisesRegex(PolymarketHTTPError, "reading the response"):
                            request_json(endpoint, payload={"value": 1}, retry_policy=self.retry)
                    self.assertEqual(send.call_count, expected_calls)
                failed.close_spy.assert_called_once_with()

    def test_post_never_retries_http_or_connection_failures(self) -> None:
        endpoint = PolymarketEndpoint("test", "POST", "/data", self.endpoint.base_url)
        for outcome in (response_for(503), requests.ConnectionError("interrupted")):
            with self.subTest(outcome=type(outcome).__name__):
                with self.managed_transport(), patch.object(HTTPAdapter, "send", side_effect=[outcome]) as send:
                    with self.assertRaises(PolymarketHTTPError):
                        request_json(endpoint, payload={"value": 1}, retry_policy=self.retry)
                    send.assert_called_once()
                if isinstance(outcome, requests.Response):
                    outcome.close_spy.assert_called_once_with()

    def test_response_limits_errors_and_cancellation_close_owned_resources(self) -> None:
        for name in ("declared", "streamed", "http_error", "bad_json", "cancelled", "bytes"):
            with self.subTest(name=name):
                response = response_for()
                expected = PolymarketHTTPError
                if name == "declared":
                    response.headers["Content-Length"] = "33"
                elif name == "streamed":
                    response._content = b"x" * 33
                elif name == "http_error":
                    response.status_code = 400
                elif name == "bad_json":
                    response._content = b"not-json"
                    expected = PolymarketResponseError
                elif name == "cancelled":
                    response.iter_content = Mock(side_effect=KeyboardInterrupt())
                    expected = KeyboardInterrupt
                with (
                    self.managed_transport(),
                    patch.object(HTTPAdapter, "send", return_value=response) as send,
                    patch("polymarket.http_client.MAX_RESPONSE_BYTES", 32),
                ):
                    if name == "bytes":
                        self.assertEqual(request_bytes(self.endpoint), b'{"ok":true}')
                    else:
                        with self.assertRaises(expected):
                            request_json(self.endpoint, retry_policy=self.retry)
                    send.assert_called_once()
                response.close_spy.assert_called_once_with()

    def test_injected_request_contract_keeps_validation_without_creating_a_session(self) -> None:
        response = response_for()
        with (
            patch("polymarket.http_client.requests.request", return_value=response) as send,
            patch("market_adapters.runtime.create_managed_http_session") as factory,
        ):
            self.assertEqual(request_json(self.endpoint), {"ok": True})
            self.assertFalse(send.call_args.kwargs["allow_redirects"])
            self.assertTrue(send.call_args.kwargs["stream"])
            with self.assertRaises(PolymarketValidationError):
                request_json(PolymarketEndpoint("test", "GET", "/", "https://127.0.0.1"))
            send.assert_called_once()
            factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
