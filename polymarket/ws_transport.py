from __future__ import annotations

import math
from typing import Any, Callable

from websocket import (
    WebSocket,
    WebSocketProtocolException,
    continuous_frame,
    create_connection as websocket_create_connection,
    frame_buffer,
)


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
        getstatus = getattr(connection, "getstatus", None)
        if not callable(getstatus):
            raise WebSocketTransportError(
                "WebSocket transport did not expose the HTTP handshake status."
            )
        status = getstatus()
        if isinstance(status, bool):
            status = None
        try:
            status_code = int(status)
        except (TypeError, ValueError):
            status_code = 0
        if status_code != WEBSOCKET_HANDSHAKE_STATUS:
            raise WebSocketTransportError(
                f"WebSocket handshake returned HTTP {status_code or 'unknown'}; "
                "redirects are disabled."
            )

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
