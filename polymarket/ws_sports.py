from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Dict, Optional

from websocket import (
    WebSocketConnectionClosedException,
    WebSocketTimeoutException,
    create_connection,
)

from core.request_control import cancellation_scope

from .constants import SPORTS_WSS_BASE
from .http_client import PolymarketValidationError
from .ws_transport import (
    WEBSOCKET_CONNECT_TIMEOUT_SECONDS,
    WEBSOCKET_INITIAL_BACKOFF_SECONDS,
    WEBSOCKET_IO_TIMEOUT_SECONDS,
    WEBSOCKET_MAX_BACKOFF_SECONDS,
    WEBSOCKET_STABLE_CONNECTION_SECONDS,
    WebSocketConnectionAttemptError,
    close_websocket,
    open_websocket_connection,
)


SportsEventHandler = Callable[[Dict[str, Any]], None]


def _validated_websocket_url(
    value: str,
    *,
    base_url: bool = False,
    resolve_addresses: bool = False,
) -> str:
    from market_adapters.errors import MarketConfigurationError
    from market_adapters.outbound import validate_outbound_url

    try:
        return validate_outbound_url(
            value,
            setting_key="Polymarket sports WebSocket URL",
            kind="websocket",
            base_url=base_url,
            resolve_addresses=resolve_addresses,
        )
    except MarketConfigurationError as exc:
        raise PolymarketValidationError(str(exc)) from exc


def sports_ws_url(url_base: str = SPORTS_WSS_BASE) -> str:
    base = _validated_websocket_url(url_base.rstrip("/"), base_url=True)
    return _validated_websocket_url(f"{base.rstrip('/')}/ws")


class SportsWSClient:
    """Synchronous-loop sports WebSocket client for live score events."""

    def __init__(
        self,
        on_event: SportsEventHandler,
        *,
        verbose: bool = False,
        url_base: str = SPORTS_WSS_BASE,
    ) -> None:
        self._on_event = on_event
        self._verbose = verbose
        self._url = sports_ws_url(url_base)

        self._lifecycle_lock = threading.RLock()
        self._generation = 0
        self._stop = threading.Event()
        self._ws: Optional[Any] = None
        self._ws_generation: Optional[int] = None
        self._connected = threading.Event()
        self._connected_generation: Optional[int] = None
        self._thread = threading.Thread(daemon=True)
        self.last_error = ""
        self.last_connected_at: Optional[float] = None

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._thread.is_alive():
                return
            self._generation += 1
            generation = self._generation
            stop_event = threading.Event()
            self._stop = stop_event
            self._connected.clear()
            self._connected_generation = None
            self._thread = threading.Thread(
                target=self._run,
                args=(generation, stop_event),
                daemon=True,
            )
            self._thread.start()

    def stop(self, *, join_timeout: float = 2.0) -> None:
        with self._lifecycle_lock:
            stop_event = self._stop
            stop_event.set()
            self._connected.clear()
            self._connected_generation = None
            connection = self._ws
            thread = self._thread
        close_websocket(connection)
        if thread.is_alive() and threading.current_thread() is not thread:
            thread.join(timeout=max(0.0, float(join_timeout)))

    @property
    def is_running(self) -> bool:
        with self._lifecycle_lock:
            return self._thread.is_alive() and not self._stop.is_set()

    @property
    def is_connected(self) -> bool:
        with self._lifecycle_lock:
            return (
                self._connected.is_set()
                and self._connected_generation == self._generation
                and not self._stop.is_set()
            )

    def _run(self, generation: int, stop_event: threading.Event) -> None:
        backoff = WEBSOCKET_INITIAL_BACKOFF_SECONDS
        while not stop_event.is_set():
            connected_seconds = 0.0
            try:
                connected_seconds = self._connect_once(generation, stop_event)
            except WebSocketConnectionAttemptError as exc:
                connected_seconds = exc.connected_seconds
                if not stop_event.is_set():
                    self.last_error = type(exc.cause).__name__
                    if self._verbose:
                        print("[ws-sports] error:", repr(exc.cause))
            except Exception as exc:
                if not stop_event.is_set():
                    self.last_error = type(exc).__name__
                    if self._verbose:
                        print("[ws-sports] error:", repr(exc))
            if stop_event.is_set():
                break
            if connected_seconds >= WEBSOCKET_STABLE_CONNECTION_SECONDS:
                backoff = WEBSOCKET_INITIAL_BACKOFF_SECONDS
            stop_event.wait(backoff)
            backoff = min(backoff * 2, WEBSOCKET_MAX_BACKOFF_SECONDS)

    def _connect_once(
        self,
        generation: Optional[int] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> float:
        if generation is None:
            generation = self._generation
        if stop_event is None:
            stop_event = self._stop
        connection: Optional[Any] = None
        connected_at: Optional[float] = None
        try:
            url = _validated_websocket_url(self._url)
            with cancellation_scope(stop_event.is_set):
                connection = open_websocket_connection(
                    url,
                    connection_factory=create_connection,
                    timeout=WEBSOCKET_CONNECT_TIMEOUT_SECONDS,
                    read_timeout=WEBSOCKET_IO_TIMEOUT_SECONDS,
                )
            connected_at = time.monotonic()
            if not self._register_connection(generation, stop_event, connection):
                return 0.0

            if not self._mark_connected(generation, stop_event, connection):
                return max(0.0, time.monotonic() - connected_at)
            self.last_error = ""
            self.last_connected_at = time.time()
            while not stop_event.is_set():
                try:
                    message = connection.recv()
                except WebSocketTimeoutException:
                    continue
                except WebSocketConnectionClosedException:
                    break
                if message in (None, "", b""):
                    break
                self._handle_message(connection, message)
            return max(0.0, time.monotonic() - connected_at)
        except Exception as exc:
            connected_seconds = (
                max(0.0, time.monotonic() - connected_at)
                if connected_at is not None
                else 0.0
            )
            raise WebSocketConnectionAttemptError(
                exc,
                connected_seconds=connected_seconds,
            ) from exc
        finally:
            if connection is not None:
                self._clear_connection(generation, connection)
                close_websocket(connection)

    def _handle_message(self, connection: Any, message: Any) -> None:
        if isinstance(message, bytes):
            message = message.decode("utf-8", errors="replace")
        if message == "ping":
            connection.send("pong")
            return
        try:
            data = json.loads(message)
            if isinstance(data, dict):
                self._on_event(data)
        except Exception:
            if self._verbose:
                print("[ws-sports] non-json:", str(message)[:200])

    def _register_connection(
        self,
        generation: int,
        stop_event: threading.Event,
        connection: Any,
    ) -> bool:
        with self._lifecycle_lock:
            if (
                generation != self._generation
                or stop_event is not self._stop
                or stop_event.is_set()
            ):
                return False
            self._ws = connection
            self._ws_generation = generation
            return True

    def _mark_connected(
        self,
        generation: int,
        stop_event: threading.Event,
        connection: Any,
    ) -> bool:
        with self._lifecycle_lock:
            if (
                generation == self._generation
                and stop_event is self._stop
                and not stop_event.is_set()
                and self._ws is connection
                and self._ws_generation == generation
            ):
                self._connected_generation = generation
                self._connected.set()
                return True
            return False

    def _clear_connection(self, generation: int, connection: Any) -> None:
        with self._lifecycle_lock:
            if self._ws is connection and self._ws_generation == generation:
                self._ws = None
                self._ws_generation = None
            if self._connected_generation == generation:
                self._connected_generation = None
                self._connected.clear()
