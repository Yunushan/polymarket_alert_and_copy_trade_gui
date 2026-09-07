from __future__ import annotations

import math
import os
from pathlib import Path
import socket
import ssl
from typing import Any, Callable
from urllib.parse import urlsplit

from websocket import (
    WebSocket,
    WebSocketProtocolException,
    continuous_frame,
    create_connection as websocket_create_connection,
    frame_buffer,
)
from websocket._socket import DEFAULT_SOCKET_OPTION
from websocket._url import get_proxy_info
from urllib3.util.ssltransport import SSLTransport

from core.request_control import RequestControl, request_scope


WEBSOCKET_HANDSHAKE_STATUS = 101
WEBSOCKET_CONNECT_TIMEOUT_SECONDS = 8.0
WEBSOCKET_IO_TIMEOUT_SECONDS = 0.25
WEBSOCKET_PING_INTERVAL_SECONDS = 10.0
WEBSOCKET_STABLE_CONNECTION_SECONDS = 5.0
WEBSOCKET_INITIAL_BACKOFF_SECONDS = 1.0
WEBSOCKET_MAX_BACKOFF_SECONDS = 30.0
WEBSOCKET_MAX_TIMEOUT_SECONDS = 60.0
WEBSOCKET_MAX_FRAME_BYTES = 1024 * 1024
WEBSOCKET_MAX_MESSAGE_BYTES = 1024 * 1024
_ORIGINAL_WEBSOCKET_CREATE_CONNECTION = websocket_create_connection


class WebSocketTransportError(RuntimeError):
    """Raised when a WebSocket handshake cannot be trusted."""


class WebSocketConnectionAttemptError(RuntimeError):
    """Carries connection age so reconnect backoff can distinguish stable runs."""

    def __init__(self, cause: Exception, *, connected_seconds: float = 0.0) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.connected_seconds = max(0.0, float(connected_seconds))


class _BoundedFrameBuffer(frame_buffer):
    """Reject an oversized frame after its header, before reading its payload."""

    def recv_length(self) -> None:
        super().recv_length()
        if self.length is not None and self.length > WEBSOCKET_MAX_FRAME_BYTES:
            raise WebSocketProtocolException(
                "WebSocket frame exceeds the "
                f"{WEBSOCKET_MAX_FRAME_BYTES}-byte receive limit."
            )


class _BoundedContinuousFrame(continuous_frame):
    """Bound the aggregate payload accumulated across continuation frames."""

    def add(self, frame: Any) -> None:
        existing_size = len(self.cont_data[1]) if self.cont_data is not None else 0
        incoming_size = len(frame.data)
        if existing_size + incoming_size > WEBSOCKET_MAX_MESSAGE_BYTES:
            raise WebSocketProtocolException(
                "WebSocket message exceeds the "
                f"{WEBSOCKET_MAX_MESSAGE_BYTES}-byte receive limit."
            )
        super().add(frame)


class BoundedWebSocket(WebSocket):
    """``websocket-client`` connection with pre-allocation receive limits.

    websocket-client 1.9 has no public maximum-message option. Its exported
    frame parser reads the declared payload length before calling
    ``recv_strict(length)``, so replacing that parser is the earliest point at
    which an oversized peer-controlled allocation can be rejected.
    """

    def __init__(
        self,
        *args: Any,
        fire_cont_frame: bool = False,
        skip_utf8_validation: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            *args,
            fire_cont_frame=fire_cont_frame,
            skip_utf8_validation=skip_utf8_validation,
            **kwargs,
        )
        self.frame_buffer = _BoundedFrameBuffer(
            self._recv,
            skip_utf8_validation,
        )
        self.cont_frame = _BoundedContinuousFrame(
            fire_cont_frame,
            skip_utf8_validation,
        )

    def connect(self, url: str, **options: Any) -> None:
        from market_adapters.outbound import validate_outbound_url_with_addresses

        if options.get("socket") is not None:
            raise WebSocketTransportError("Managed WebSockets cannot use an unvalidated caller socket.")
        if self.sock is not None:
            raise WebSocketTransportError("Close the existing managed WebSocket before reconnecting.")
        default_timeout = self.sock_opt.timeout if self.sock_opt.timeout is not None else WEBSOCKET_CONNECT_TIMEOUT_SECONDS
        timeout = bounded_websocket_timeout(options.get("timeout", default_timeout))
        options["redirect_limit"] = 0
        sock = None
        try:
            with request_scope(timeout) as control:
                url, addresses = validate_outbound_url_with_addresses(
                    url, kind="websocket", setting_key="Managed WebSocket URL",
                )
                parsed = urlsplit(url)
                hostname = str(parsed.hostname)
                secure = parsed.scheme == "wss"
                _require_verified_tls(self.sock_opt.sslopt, hostname)
                proxy_host, _proxy_port, _proxy_auth = get_proxy_info(
                    hostname, secure,
                    proxy_host=options.get("http_proxy_host"),
                    proxy_port=options.get("http_proxy_port", 0),
                    proxy_auth=options.get("http_proxy_auth"),
                    no_proxy=options.get("http_no_proxy"),
                    proxy_type=options.get("proxy_type", "http"),
                )
                if proxy_host:
                    # The configured proxy owns its target connection. Preserve
                    # that route; its egress/deadline enforcement is external.
                    super().connect(url, **{**options, "timeout": control.remaining()})
                else:
                    sock, guard = _open_pinned_socket(
                        addresses, parsed.port or (443 if secure else 80), control, self.sock_opt.sockopt,
                    )
                    try:
                        if secure:
                            sock.settimeout(control.remaining())
                            sock = _wrap_tls_socket(sock, self.sock_opt.sslopt, hostname)
                        sock.settimeout(control.remaining())
                        super().connect(url, **{**options, "socket": sock, "timeout": control.remaining()})
                    finally:
                        control.unwatch_socket(guard)
                _require_websocket_handshake(self)
        except BaseException:
            # Scope-exit cancellation can happen after the socket guard is
            # disarmed. Keep ownership until the entire scope exits successfully.
            self.connected = False
            for owned_socket in (sock, self.sock):
                if owned_socket is not None:
                    try:
                        owned_socket.close()
                    except Exception:
                        pass
            self.sock = None
            self.handshake_response = None
            raise


class _WebSocketTLS(SSLTransport):
    def shutdown(self, how: int) -> None:
        self.socket.shutdown(how)


def _wrap_tls_socket(sock: socket.socket, options: dict[str, Any], hostname: str) -> SSLTransport:
    # Memory-BIO TLS keeps the guarded raw socket as the I/O owner, including
    # during handshake. No detached Windows handle can outlive cancellation.
    context = options.get("context")
    if context is None:
        context = ssl.SSLContext(options.get("ssl_version", ssl.PROTOCOL_TLS_CLIENT))
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        cafile, capath = options.get("ca_certs"), options.get("ca_cert_path")
        bundle = os.environ.get("WEBSOCKET_CLIENT_CA_BUNDLE")
        if bundle:
            if not cafile and Path(bundle).is_file():
                cafile = bundle
            elif not capath and Path(bundle).is_dir():
                capath = bundle
        if cafile or capath:
            context.load_verify_locations(cafile=cafile, capath=capath)
        else:
            context.load_default_certs(ssl.Purpose.SERVER_AUTH)
        if options.get("certfile"):
            context.load_cert_chain(options["certfile"], options.get("keyfile"), options.get("password"))
        if "cert_chain" in options:
            chain = options["cert_chain"]
            if not isinstance(chain, (tuple, list)) or len(chain) != 3:
                raise WebSocketTransportError("TLS cert_chain must contain certfile, keyfile and password.")
            context.load_cert_chain(*chain)
        if "ciphers" in options:
            context.set_ciphers(options["ciphers"])
        if "ecdh_curve" in options:
            context.set_ecdh_curve(options["ecdh_curve"])
        if "SSLKEYLOGFILE" in os.environ:
            context.keylog_filename = os.environ["SSLKEYLOGFILE"]
    return _WebSocketTLS(
        sock, context, server_hostname=hostname,
        suppress_ragged_eofs=options.get("suppress_ragged_eofs", True),
    )


def _require_verified_tls(options: dict[str, Any], hostname: str) -> None:
    context = options.get("context")
    if (
        options.get("cert_reqs", ssl.CERT_REQUIRED) != ssl.CERT_REQUIRED
        or options.get("check_hostname", True) is not True
        or options.get("server_hostname", hostname) != hostname
        or options.get("do_handshake_on_connect", True) is not True
        or (context is not None and (context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname))
    ):
        raise WebSocketTransportError("Managed WebSockets require verified TLS and the origin hostname.")


def _open_pinned_socket(addresses: Any, port: int, control: RequestControl, socket_options: Any) -> tuple[Any, Any]:
    last_error: OSError | None = None
    for address in addresses:
        control.check()
        sock = socket.socket(socket.AF_INET6 if address.version == 6 else socket.AF_INET, socket.SOCK_STREAM)
        guard = None
        try:
            sock.settimeout(control.remaining())
            for option in [*DEFAULT_SOCKET_OPTION, *socket_options]:
                sock.setsockopt(*option)
            guard = control.watch_socket(sock)
            # Numeric addresses never pass through getaddrinfo a second time.
            sock.connect((str(address), port))
            return sock, guard
        except BaseException as exc:
            if guard is not None:
                control.unwatch_socket(guard)
            sock.close()
            control.check()
            if not isinstance(exc, OSError):
                raise
            last_error = exc
    if last_error is not None:
        raise last_error
    raise WebSocketTransportError("Managed WebSocket DNS validation returned no addresses.")


def bounded_websocket_timeout(value: Any) -> float:
    if isinstance(value, bool):
        raise WebSocketTransportError("WebSocket timeout must be a positive finite number.")
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise WebSocketTransportError(
            "WebSocket timeout must be a positive finite number."
        ) from exc
    if not math.isfinite(timeout) or timeout <= 0 or timeout > WEBSOCKET_MAX_TIMEOUT_SECONDS:
        raise WebSocketTransportError(
            f"WebSocket timeout must be between 0 and {WEBSOCKET_MAX_TIMEOUT_SECONDS:g} seconds."
        )
    return timeout


def close_websocket(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _require_websocket_handshake(connection: Any) -> None:
    getstatus = getattr(connection, "getstatus", None)
    if not callable(getstatus):
        raise WebSocketTransportError("WebSocket transport did not expose the HTTP handshake status.")
    status = getstatus()
    if isinstance(status, bool):
        status = None
    try:
        status_code = int(status)
    except (TypeError, ValueError):
        status_code = 0
    if status_code != WEBSOCKET_HANDSHAKE_STATUS:
        raise WebSocketTransportError(
            f"WebSocket handshake returned HTTP {status_code or 'unknown'}; redirects are disabled."
        )


def open_websocket_connection(
    url: str,
    *,
    connection_factory: Callable[..., Any],
    timeout: Any = WEBSOCKET_CONNECT_TIMEOUT_SECONDS,
    read_timeout: Any | None = WEBSOCKET_IO_TIMEOUT_SECONDS,
) -> Any:
    """Open one non-redirecting WebSocket and require a completed 101 handshake.

    ``websocket-client`` 1.9 exposes ``redirect_limit`` only on its lower-level
    connection API. It also returns a connection object for a redirect when
    the limit is zero, so status verification must happen before callers send
    subscriptions or credentials.
    """

    connect_timeout = bounded_websocket_timeout(timeout)
    connection_options: dict[str, Any] = {
        "timeout": connect_timeout,
        "redirect_limit": 0,
    }
    if connection_factory is _ORIGINAL_WEBSOCKET_CREATE_CONNECTION:
        connection_options["class_"] = BoundedWebSocket
    connection = connection_factory(url, **connection_options)
    try:
        _require_websocket_handshake(connection)
        if read_timeout is not None:
            io_timeout = bounded_websocket_timeout(read_timeout)
            settimeout = getattr(connection, "settimeout", None)
            if not callable(settimeout):
                raise WebSocketTransportError(
                    "WebSocket transport did not expose a bounded read timeout."
                )
            settimeout(io_timeout)
        return connection
    except Exception:
        close_websocket(connection)
        raise
