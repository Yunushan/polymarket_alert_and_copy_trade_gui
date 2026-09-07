from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import os
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from websocket import ABNF, WebSocketTimeoutException, frame_buffer

from core.request_control import RequestCancelled, RequestControl, RequestDeadlineExceeded, cancellation_scope
from market_adapters.errors import MarketConfigurationError
from market_adapters.outbound import OutboundEndpointPolicy
from polymarket import ws_market, ws_sports, ws_transport, ws_user
from test_polymarket_http_transport import local_tls_server, resolver_for


def upgrade(handler):
    key = handler.headers["Sec-WebSocket-Key"]
    digest = hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode(), usedforsecurity=False).digest()
    handler.send_response(101)
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", base64.b64encode(digest).decode())
    handler.end_headers()


def send_ready(handler):
    upgrade(handler)
    handler.wfile.write(ABNF(1, 0, 0, 0, ABNF.OPCODE_TEXT, 0, b"ready").format())


@contextmanager
def plain_server(handle_get):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib handler API
            handle_get(self)

        def log_message(self, *_args):
            return

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield server.server_port
        finally:
            server.shutdown()
            thread.join(timeout=5)


class ManagedWebSocketConnectionTests(unittest.TestCase):
    def setUp(self):
        environment = {key: "" for key in (
            "http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY",
            "no_proxy", "NO_PROXY", "WEBSOCKET_CLIENT_CA_BUNDLE",
        )}
        self.environment = patch.dict(os.environ, environment)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    @contextmanager
    def policy(self, origin, *, addresses=("127.0.0.1",), private=True, resolver=None):
        resolver = resolver or Mock(side_effect=resolver_for(*addresses))
        policy = OutboundEndpointPolicy(
            private_origins=frozenset({origin}) if private else frozenset(), resolver=resolver,
        )
        with patch.object(OutboundEndpointPolicy, "from_environment", return_value=policy):
            yield resolver

    @staticmethod
    def handshake(connection, _url, **options):
        connection.sock = options["socket"]
        connection.connected = True
        connection.handshake_response = SimpleNamespace(status=101)

    def test_scope_exit_cancellation_and_deadline_close_direct_and_proxy_connections(self):
        for proxy in (False, True):
            for reason in ("cancelled", "deadline"):
                with self.subTest(proxy=proxy, reason=reason):
                    left, right = socket.socketpair()
                    connection = ws_transport.BoundedWebSocket()
                    state = {"cancelled": False}
                    original_unwatch = RequestControl.unwatch_socket

                    def expire(control, reason=reason, state=state):
                        if reason == "cancelled":
                            state["cancelled"] = True
                        else:
                            control.deadline = time.monotonic() - 1

                    def unwatch(control, guard, original_unwatch=original_unwatch, expire=expire):
                        original_unwatch(control, guard)
                        expire(control)

                    def pinned(_addresses, _port, control, _options, left=left):
                        return left, control.watch_socket(left)

                    def handshake(conn, url, left=left, proxy=proxy, expire=expire, **options):
                        self.handshake(conn, url, socket=left)
                        if proxy:
                            from core.request_control import current_request

                            expire(current_request())

                    try:
                        with (
                            self.policy("ws://127.0.0.1"),
                            cancellation_scope(lambda state=state: state["cancelled"]),
                            patch.object(ws_transport, "get_proxy_info", return_value=("proxy.test" if proxy else None, 80, None)),
                            patch.object(ws_transport, "_open_pinned_socket", side_effect=pinned),
                            patch.object(ws_transport.WebSocket, "connect", handshake),
                            patch.object(RequestControl, "unwatch_socket", unwatch),
                        ):
                            expected = RequestCancelled if reason == "cancelled" else RequestDeadlineExceeded
                            with self.assertRaises(expected):
                                connection.connect("ws://127.0.0.1/ws", timeout=2)
                        self.assertFalse(connection.connected)
                        self.assertIsNone(connection.sock)
                        self.assertIsNone(connection.handshake_response)
                        self.assertEqual(left.fileno(), -1)
                    finally:
                        connection.shutdown()
                        left.close()
                        right.close()

    def test_private_mixed_and_rebound_dns_fail_before_opening_socket(self):
        origin = "wss://venue.example.test"
        for addresses in (("127.0.0.1",), ("93.184.216.34", "169.254.169.254")):
            with self.subTest(addresses=addresses), self.policy(origin, addresses=addresses, private=False):
                with patch.object(ws_transport, "_open_pinned_socket") as opened:
                    with self.assertRaises(MarketConfigurationError):
                        ws_transport.BoundedWebSocket().connect(origin, timeout=1)
                    opened.assert_not_called()

        resolver = Mock(side_effect=[resolver_for("93.184.216.34")("host", 443, type=socket.SOCK_STREAM),
                                     resolver_for("127.0.0.1")("host", 443, type=socket.SOCK_STREAM)])
        left, right = socket.socketpair()
        connection = ws_transport.BoundedWebSocket()

        def pinned(addresses, port, control, _options):
            self.assertEqual([str(address) for address in addresses], ["93.184.216.34"])
            self.assertEqual(port, 443)
            return left, control.watch_socket(left)

        try:
            with (
                self.policy(origin, private=False, resolver=resolver),
                patch.object(ws_transport, "_open_pinned_socket", side_effect=pinned) as opened,
                patch.object(ws_transport, "_wrap_tls_socket", side_effect=lambda sock, *_args: sock),
                patch.object(ws_transport.WebSocket, "connect", self.handshake),
            ):
                connection.connect(origin, timeout=1)
                connection.shutdown()
                with self.assertRaises(MarketConfigurationError):
                    connection.connect(origin, timeout=1)
                self.assertEqual(resolver.call_count, 2)
                self.assertEqual(opened.call_count, 1)
        finally:
            connection.shutdown()
            left.close()
            right.close()

    def test_numeric_address_fallback_preserves_socket_options_and_closes_failed_attempt(self):
        first, second = Mock(), Mock()
        first.connect.side_effect = ConnectionRefusedError("refused")
        control = Mock(spec=RequestControl)
        control.remaining.return_value = 0.25
        with patch.object(ws_transport.socket, "socket", side_effect=[first, second]) as create:
            result, guard = ws_transport._open_pinned_socket(
                (ipaddress.ip_address("93.184.216.34"), ipaddress.ip_address("2606:4700::1111")),
                443, control, [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)],
            )
        self.assertIs(result, second)
        self.assertEqual(create.call_args_list[0].args, (socket.AF_INET, socket.SOCK_STREAM))
        self.assertEqual(create.call_args_list[1].args, (socket.AF_INET6, socket.SOCK_STREAM))
        first.connect.assert_called_once_with(("93.184.216.34", 443))
        second.connect.assert_called_once_with(("2606:4700::1111", 443))
        first.close.assert_called_once()
        second.close.assert_not_called()
        second.settimeout.assert_called_once_with(0.25)
        second.setsockopt.assert_any_call(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        control.unwatch_socket.assert_called_once_with(guard)

    def test_invalid_tls_options_and_caller_socket_are_rejected(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        for options in (
            {"cert_reqs": ssl.CERT_NONE}, {"check_hostname": False},
            {"server_hostname": "other.test"}, {"do_handshake_on_connect": False}, {"context": context},
        ):
            with self.subTest(options=list(options)), self.policy("wss://venue.example.test"):
                with patch.object(ws_transport, "_open_pinned_socket") as opened:
                    with self.assertRaisesRegex(ws_transport.WebSocketTransportError, "verified TLS"):
                        ws_transport.BoundedWebSocket(sslopt=options).connect("wss://venue.example.test", timeout=1)
                    opened.assert_not_called()
        with self.assertRaisesRegex(ws_transport.WebSocketTransportError, "caller socket"):
            ws_transport.BoundedWebSocket().connect("wss://venue.example.test", socket=object(), timeout=1)

    def test_cleanup_failure_does_not_mask_handshake_error(self):
        sock = Mock()
        sock.close.side_effect = OSError("close failed")
        connection = ws_transport.BoundedWebSocket()
        with (
            self.policy("wss://venue.example.test"),
            patch.object(ws_transport, "_open_pinned_socket", return_value=(sock, object())),
            patch.object(ws_transport, "_wrap_tls_socket", side_effect=lambda value, *_args: value),
            patch.object(RequestControl, "watch_socket", return_value=object()),
            patch.object(ws_transport.WebSocket, "connect", side_effect=ValueError("original handshake error")),
        ):
            with self.assertRaisesRegex(ValueError, "original handshake error"):
                connection.connect("wss://venue.example.test", timeout=1)
        self.assertFalse(connection.connected)
        self.assertIsNone(connection.sock)
        self.assertTrue(sock.close.called)

    def test_tls_setup_failure_closes_original_socket_and_releases_guard(self):
        left, right = socket.socketpair()
        controls = []

        def pinned(_addresses, _port, control, _options):
            controls.append(control)
            return left, control.watch_socket(left)

        try:
            with (
                self.policy("wss://venue.example.test"),
                patch.object(ws_transport, "_open_pinned_socket", side_effect=pinned),
                patch.object(ws_transport, "_wrap_tls_socket", side_effect=RuntimeError("TLS setup failed")),
            ):
                with self.assertRaisesRegex(RuntimeError, "TLS setup failed"):
                    ws_transport.BoundedWebSocket().connect("wss://venue.example.test", timeout=1)
            self.assertEqual(left.fileno(), -1)
            self.assertFalse(controls[0]._sockets)
            self.assertFalse(controls[0]._watcher.is_alive())
        finally:
            for sock in (left, right):
                sock.close()

    def test_environment_proxy_is_preserved_and_no_proxy_selects_direct_path(self):
        for bypass in (False, True):
            with self.subTest(bypass=bypass):
                left, right = socket.socketpair()
                connection = ws_transport.BoundedWebSocket()
                calls = []

                def handshake(conn, url, calls=calls, left=left, **options):
                    calls.append(options)
                    self.handshake(conn, url, socket=left)

                def pinned(_addresses, _port, control, _options, left=left):
                    return left, control.watch_socket(left)

                try:
                    with (
                        self.policy("wss://venue.example.test"),
                        patch.dict(os.environ, {"https_proxy": "http://proxy.example.test:8080", "no_proxy": "*" if bypass else ""}),
                        patch.object(ws_transport, "_open_pinned_socket", side_effect=pinned) as opened,
                        patch.object(ws_transport, "_wrap_tls_socket", side_effect=lambda sock, *_args: sock),
                        patch.object(ws_transport.WebSocket, "connect", handshake),
                    ):
                        connection.connect("wss://venue.example.test", timeout=1, redirect_limit=7)
                    self.assertEqual(opened.call_count, int(bypass))
                    self.assertEqual("socket" in calls[0], bypass)
                    self.assertEqual(calls[0]["redirect_limit"], 0)
                    self.assertGreater(calls[0]["timeout"], 0)
                    self.assertLessEqual(calls[0]["timeout"], 1)
                finally:
                    connection.shutdown()
                    left.close()
                    right.close()

    def test_real_tls_preserves_hostname_sni_trust_bundle_and_single_dns_lookup(self):
        with tempfile.TemporaryDirectory() as directory, local_tls_server(directory, send_ready) as (port, ca, observed):
            origin = f"wss://venue.example.test:{port}"
            connection = None
            try:
                with (
                    self.policy(origin) as resolver,
                    patch.dict(os.environ, {"WEBSOCKET_CLIENT_CA_BUNDLE": str(ca)}),
                    patch.object(socket, "getaddrinfo", side_effect=AssertionError("unvalidated second DNS lookup")),
                ):
                    connection = ws_transport.open_websocket_connection(
                        origin + "/ws/market", connection_factory=ws_transport.websocket_create_connection,
                        timeout=2, read_timeout=1,
                    )
                    self.assertEqual(connection.recv(), "ready")
                self.assertIsInstance(connection, ws_transport.BoundedWebSocket)
                self.assertEqual(resolver.call_count, 1)
                self.assertEqual(observed["hosts"], [f"venue.example.test:{port}"])
                self.assertEqual(observed["sni"], ["venue.example.test"])
                self.assertEqual(observed["paths"], ["/ws/market"])
            finally:
                if connection is not None:
                    connection.shutdown()

    def test_real_tls_rejects_untrusted_and_wrong_hostname_before_subscription(self):
        for trusted, hostname in ((False, "venue.example.test"), (True, "wrong.example.test")):
            with self.subTest(trusted=trusted), tempfile.TemporaryDirectory() as directory:
                with local_tls_server(directory, send_ready) as (port, ca, observed):
                    origin = f"wss://{hostname}:{port}"
                    connection = ws_transport.BoundedWebSocket(sslopt={"ca_certs": str(ca)} if trusted else {})
                    with self.policy(origin):
                        with self.assertRaises(ssl.SSLError):
                            connection.connect(origin + "/ws/user", timeout=2)
                    self.assertFalse(connection.connected)
                    self.assertIsNone(connection.sock)
                    self.assertEqual(observed["hosts"], [])

    def test_real_tls_session_survives_partial_frames_and_setup_deadline(self):
        partial, resume, finished = threading.Event(), threading.Event(), threading.Event()
        received, errors = [], []
        binary = bytes(range(256)) * 256

        def handler(request):
            try:
                request.connection.settimeout(5)
                upgrade(request)
                frames = frame_buffer(request.connection.recv, skip_utf8_validation=False)
                subscription = frames.recv_frame()
                received.append((subscription.opcode, subscription.data))
                fragment = ABNF(0, 0, 0, 0, ABNF.OPCODE_TEXT, 0, b"hel").format()
                request.connection.sendall(fragment[:3])
                partial.set()
                if not resume.wait(5):
                    raise TimeoutError("client did not resume the partial frame")
                request.connection.sendall(
                    fragment[3:]
                    + ABNF(1, 0, 0, 0, ABNF.OPCODE_PING, 0, b"pulse").format()
                    + ABNF(1, 0, 0, 0, ABNF.OPCODE_CONT, 0, b"lo").format()
                )
                for _ in range(2):
                    frame = frames.recv_frame()
                    received.append((frame.opcode, frame.data))
                    if frame.opcode == ABNF.OPCODE_BINARY:
                        request.connection.sendall(ABNF(1, 0, 0, 0, frame.opcode, 0, frame.data).format())
                close = frames.recv_frame()
                received.append((close.opcode, close.data))
                request.connection.sendall(ABNF(1, 0, 0, 0, close.opcode, 0, close.data).format())
            except Exception as exc:
                errors.append(exc)
            finally:
                finished.set()

        with tempfile.TemporaryDirectory() as directory, local_tls_server(directory, handler) as (port, ca, observed):
            origin = f"wss://venue.example.test:{port}"
            context = ssl.create_default_context(cafile=str(ca))
            connection = ws_transport.BoundedWebSocket(sslopt={"context": context})
            try:
                with self.policy(origin):
                    connection.connect(origin + "/ws/user", timeout=2)
                connection.settimeout(0.05)
                connection.send("test-subscription")
                self.assertTrue(partial.wait(2))
                with self.assertRaises(WebSocketTimeoutException):
                    connection.recv()
                self.assertTrue(connection.connected)
                time.sleep(2.1)
                connection.settimeout(2)
                resume.set()
                self.assertEqual(connection.recv(), "hello")
                connection.send_binary(binary)
                self.assertEqual(connection.recv(), binary)
                connection.close(timeout=1)
                self.assertTrue(finished.wait(2))
                self.assertFalse(connection.connected)
                self.assertIsNone(connection.sock)
                self.assertEqual(errors, [])
                self.assertEqual(received, [
                    (ABNF.OPCODE_TEXT, b"test-subscription"),
                    (ABNF.OPCODE_PONG, b"pulse"),
                    (ABNF.OPCODE_BINARY, binary),
                    (ABNF.OPCODE_CLOSE, b"\x03\xe8"),
                ])
                self.assertEqual(observed["sni"], ["venue.example.test"])
            finally:
                resume.set()
                connection.shutdown()

    def test_tls_options_preserve_ca_client_certificate_and_context_configuration(self):
        context, sock = Mock(), Mock()
        options = {
            "ca_certs": "explicit-ca.pem", "ca_cert_path": "ca-directory",
            "certfile": "client.pem", "keyfile": "client.key", "password": "test-password",
            "cert_chain": ("alternate.pem", "alternate.key", "alternate-password"),
            "ciphers": "ECDHE+AESGCM", "ecdh_curve": "prime256v1", "suppress_ragged_eofs": False,
        }
        with (
            patch.object(ws_transport.ssl, "SSLContext", return_value=context) as create_context,
            patch.object(ws_transport, "_WebSocketTLS") as wrap,
            patch.dict(os.environ, {"SSLKEYLOGFILE": "test-keylog.log"}),
        ):
            self.assertIs(ws_transport._wrap_tls_socket(sock, options, "venue.example.test"), wrap.return_value)
        create_context.assert_called_once_with(ssl.PROTOCOL_TLS_CLIENT)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertIs(context.check_hostname, True)
        context.load_verify_locations.assert_called_once_with(cafile="explicit-ca.pem", capath="ca-directory")
        context.load_default_certs.assert_not_called()
        self.assertEqual([call.args for call in context.load_cert_chain.call_args_list], [
            ("client.pem", "client.key", "test-password"),
            ("alternate.pem", "alternate.key", "alternate-password"),
        ])
        context.set_ciphers.assert_called_once_with("ECDHE+AESGCM")
        context.set_ecdh_curve.assert_called_once_with("prime256v1")
        self.assertEqual(context.keylog_filename, "test-keylog.log")
        wrap.assert_called_once_with(sock, context, server_hostname="venue.example.test", suppress_ragged_eofs=False)

        with patch.object(ws_transport.ssl, "SSLContext") as create_context, patch.object(ws_transport, "_WebSocketTLS") as wrap:
            ws_transport._wrap_tls_socket(sock, {"context": context}, "venue.example.test")
        create_context.assert_not_called()
        wrap.assert_called_once_with(sock, context, server_hostname="venue.example.test", suppress_ragged_eofs=True)

    def test_tls_ca_environment_fallback_and_invalid_chain_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            ca = Path(directory) / "ca.pem"
            ca.write_text("mock CA loading only", encoding="ascii")
            for bundle, cafile, capath in (("", None, None), (str(ca), str(ca), None), (directory, None, directory)):
                with self.subTest(bundle=bundle):
                    context = Mock()
                    with (
                        patch.object(ws_transport.ssl, "SSLContext", return_value=context),
                        patch.object(ws_transport, "_WebSocketTLS"),
                        patch.dict(os.environ, {"WEBSOCKET_CLIENT_CA_BUNDLE": bundle}),
                    ):
                        ws_transport._wrap_tls_socket(Mock(), {}, "venue.example.test")
                    if cafile or capath:
                        context.load_verify_locations.assert_called_once_with(cafile=cafile, capath=capath)
                        context.load_default_certs.assert_not_called()
                    else:
                        context.load_default_certs.assert_called_once_with(ssl.Purpose.SERVER_AUTH)
                        context.load_verify_locations.assert_not_called()
        for chain in ("client.pem", (), ("client.pem", "client.key")):
            with self.subTest(chain=chain):
                with (
                    patch.object(ws_transport.ssl, "SSLContext"),
                    patch.object(ws_transport, "_WebSocketTLS") as wrap,
                ):
                    with self.assertRaisesRegex(ws_transport.WebSocketTransportError, "cert_chain"):
                        ws_transport._wrap_tls_socket(Mock(), {"cert_chain": chain}, "venue.example.test")
                    wrap.assert_not_called()

    def test_success_disarms_connection_deadline_without_closing_long_lived_socket(self):
        release = threading.Event()

        def handler(request):
            upgrade(request)
            release.wait(3)
            request.wfile.write(ABNF(1, 0, 0, 0, ABNF.OPCODE_TEXT, 0, b"ready").format())

        with plain_server(handler) as port:
            origin = f"ws://127.0.0.1:{port}"
            connection = None
            try:
                with self.policy(origin):
                    connection = ws_transport.open_websocket_connection(
                        origin, connection_factory=ws_transport.websocket_create_connection, timeout=0.25, read_timeout=1,
                    )
                time.sleep(0.35)
                release.set()
                self.assertEqual(connection.recv(), "ready")
                self.assertTrue(connection.connected)
            finally:
                release.set()
                if connection is not None:
                    connection.shutdown()

    def test_slow_http_handshake_cannot_reset_overall_deadline(self):
        finished = threading.Event()

        def handler(request):
            try:
                for byte in b"HTTP/1.1 101 Switching Protocols\r\n":
                    request.connection.sendall(bytes([byte]))
                    if finished.wait(0.05):
                        break
            except OSError:
                pass

        with plain_server(handler) as port:
            connection = ws_transport.BoundedWebSocket()
            try:
                with self.policy(f"ws://127.0.0.1:{port}"):
                    started = time.monotonic()
                    with self.assertRaises(RequestDeadlineExceeded):
                        connection.connect(f"ws://127.0.0.1:{port}/ws", timeout=0.2)
                    self.assertLess(time.monotonic() - started, 1)
                self.assertIsNone(connection.sock)
                self.assertFalse(connection.connected)
            finally:
                finished.set()
                connection.shutdown()

    def test_probe_dns_obeys_deadline_before_any_socket_or_auth_send(self):
        release = threading.Event()

        def resolver(*_args, **_kwargs):
            release.wait(3)
            return resolver_for("127.0.0.1")("host", 443, type=socket.SOCK_STREAM)

        try:
            with self.policy("wss://venue.example.test", resolver=resolver):
                with patch.object(ws_transport, "_open_pinned_socket") as opened:
                    started = time.monotonic()
                    with self.assertRaises(RequestDeadlineExceeded):
                        ws_user.probe_user_websocket(
                            {"apiKey": "key", "secret": "not-sent", "passphrase": "pass"},
                            url_base="wss://venue.example.test", timeout=0.15,
                        )
                    self.assertLess(time.monotonic() - started, 0.8)
                    opened.assert_not_called()
        finally:
            release.set()

    def test_stalled_tls_handshake_obeys_deadline_and_cancellation(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel), socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                listener.settimeout(3)
                entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()

                def serve(listener=listener, entered=entered, release=release):
                    peer, _address = listener.accept()
                    with peer:
                        peer.settimeout(2)
                        peer.recv(4096)
                        entered.set()
                        release.wait(3)

                def stop(entered=entered, cancelled=cancelled):
                    if entered.wait(2):
                        cancelled.set()

                server_thread = threading.Thread(target=serve)
                cancel_thread = threading.Thread(target=stop) if cancel else None
                connection = ws_transport.BoundedWebSocket()
                server_thread.start()
                if cancel_thread:
                    cancel_thread.start()
                try:
                    origin = f"wss://venue.example.test:{listener.getsockname()[1]}"
                    with self.policy(origin), cancellation_scope(cancelled.is_set):
                        started = time.monotonic()
                        expected = RequestCancelled if cancel else RequestDeadlineExceeded
                        with self.assertRaises(expected):
                            connection.connect(origin, timeout=2 if cancel else 0.2)
                        self.assertLess(time.monotonic() - started, 1)
                    self.assertTrue(entered.is_set())
                    self.assertFalse(connection.connected)
                    self.assertIsNone(connection.sock)
                finally:
                    release.set()
                    connection.shutdown()
                    server_thread.join(timeout=4)
                    if cancel_thread:
                        cancel_thread.join(timeout=4)
                self.assertFalse(server_thread.is_alive())

    def test_all_channel_stops_cancel_pending_managed_dns(self):
        factories = (
            lambda: ws_market.MarketWSClient([], lambda _event: None, url_base="wss://venue.example.test"),
            lambda: ws_user.UserWSClient({"apiKey": "key", "secret": "not-sent", "passphrase": "pass"}, [], lambda _event: None, url_base="wss://venue.example.test"),
            lambda: ws_sports.SportsWSClient(lambda _event: None, url_base="wss://venue.example.test"),
        )
        for factory in factories:
            entered, release = threading.Event(), threading.Event()

            def resolver(*_args, entered=entered, release=release, **_kwargs):
                entered.set()
                release.wait(3)
                return resolver_for("127.0.0.1")("host", 443, type=socket.SOCK_STREAM)

            client = None
            try:
                with self.policy("wss://venue.example.test", resolver=resolver):
                    client = factory()
                    with patch.object(ws_transport, "_open_pinned_socket") as opened:
                        client.start()
                        self.assertTrue(entered.wait(2))
                        started = time.monotonic()
                        client.stop(join_timeout=1)
                        self.assertFalse(client._thread.is_alive())
                        self.assertLess(time.monotonic() - started, 0.8)
                        opened.assert_not_called()
            finally:
                release.set()
                if client is not None:
                    client.stop()

    def test_redirect_is_not_followed_or_given_user_credentials(self):
        received, finished = [], threading.Event()

        def handler(request):
            request.send_response(302)
            request.send_header("Location", "ws://127.0.0.1:9/not-forwarded")
            request.end_headers()
            request.connection.settimeout(1)
            try:
                received.append(request.connection.recv(1))
            finally:
                finished.set()

        with plain_server(handler) as port, self.policy(f"ws://127.0.0.1:{port}"):
            with self.assertRaisesRegex(ws_transport.WebSocketTransportError, "HTTP 302"):
                ws_user.probe_user_websocket(
                    {"apiKey": "key", "secret": "not-sent", "passphrase": "pass"},
                    url_base=f"ws://127.0.0.1:{port}", timeout=1,
                )
            self.assertTrue(finished.wait(2))
            self.assertEqual(received, [b""])

    def test_all_channel_stops_interrupt_a_pending_http_handshake(self):
        factories = (
            lambda base: ws_market.MarketWSClient([], lambda _event: None, url_base=base),
            lambda base: ws_user.UserWSClient({"apiKey": "key", "secret": "not-sent", "passphrase": "pass"}, [], lambda _event: None, url_base=base),
            lambda base: ws_sports.SportsWSClient(lambda _event: None, url_base=base),
        )
        for factory in factories:
            entered, closed = threading.Event(), threading.Event()
            received = []

            def handler(request, entered=entered, closed=closed, received=received):
                entered.set()
                request.connection.settimeout(3)
                try:
                    received.append(request.connection.recv(1))
                except ConnectionResetError:
                    received.append(b"")
                finally:
                    closed.set()

            with plain_server(handler) as port, self.policy(f"ws://127.0.0.1:{port}"):
                client = factory(f"ws://127.0.0.1:{port}")
                try:
                    client.start()
                    self.assertTrue(entered.wait(2))
                    started = time.monotonic()
                    client.stop(join_timeout=1)
                    self.assertFalse(client._thread.is_alive())
                    self.assertLess(time.monotonic() - started, 0.8)
                    self.assertTrue(closed.wait(1))
                    self.assertEqual(received, [b""])
                    self.assertFalse(client.is_connected)
                finally:
                    client.stop()


if __name__ == "__main__":
    unittest.main()
