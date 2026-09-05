from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Set

from websocket import (
    WebSocketConnectionClosedException,
    WebSocketTimeoutException,
    create_connection,
)

from core.request_control import cancellation_scope

from .constants import CLOB_WSS_BASE
from .http_client import PolymarketValidationError
from .ws_transport import (
    WEBSOCKET_CONNECT_TIMEOUT_SECONDS,
    WEBSOCKET_INITIAL_BACKOFF_SECONDS,
    WEBSOCKET_IO_TIMEOUT_SECONDS,
    WEBSOCKET_MAX_BACKOFF_SECONDS,
    WEBSOCKET_PING_INTERVAL_SECONDS,
    WEBSOCKET_STABLE_CONNECTION_SECONDS,
    WebSocketConnectionAttemptError,
    close_websocket,
    open_websocket_connection,
)


UserEventHandler = Callable[[Dict[str, Any]], None]


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
            setting_key="Polymarket user WebSocket URL",
            kind="websocket",
            base_url=base_url,
            resolve_addresses=resolve_addresses,
        )
    except MarketConfigurationError as exc:
        raise PolymarketValidationError(str(exc)) from exc


def build_user_subscription(
    auth: Mapping[str, str],
    markets: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    required = ("apiKey", "secret", "passphrase")
    missing = [key for key in required if not auth.get(key)]
    if missing:
        raise ValueError(f"Polymarket user WebSocket auth is missing: {', '.join(missing)}")
    msg: Dict[str, Any] = {
        "auth": {key: str(auth[key]) for key in required},
        "type": "user",
    }
    market_ids = [str(market) for market in (markets or []) if str(market)]
    if market_ids:
        msg["markets"] = market_ids
    return msg


def user_ws_url(url_base: str = CLOB_WSS_BASE) -> str:
    base = _validated_websocket_url(url_base.rstrip("/"), base_url=True)
    return _validated_websocket_url(f"{base.rstrip('/')}/ws/user")


def probe_user_websocket(
    auth: Mapping[str, str],
    markets: Optional[Iterable[str]] = None,
    *,
    timeout: float = 8.0,
    url_base: str = CLOB_WSS_BASE,
    connection_factory: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    subscription = build_user_subscription(auth, markets)
    factory = connection_factory or create_connection
    url = user_ws_url(url_base)
    connection = open_websocket_connection(
        url,
        connection_factory=factory,
        timeout=timeout,
        read_timeout=timeout,
    )
    try:
        connection.send(json.dumps(subscription))
        try:
            connection.send("PING")
            message = connection.recv()
        except Exception:
            message = ""
        return {
            "connected": True,
            "subscription_sent": True,
            "received_message": bool(message),
            "message_sample_type": type(message).__name__ if message else "",
        }
    finally:
        close_websocket(connection)


class UserWSClient:
    """Authenticated synchronous-loop user-channel WebSocket client."""

    def __init__(
        self,
        auth: Mapping[str, str],
        markets: Iterable[str],
        on_event: UserEventHandler,
        *,
        verbose: bool = False,
        url_base: str = CLOB_WSS_BASE,
    ) -> None:
        self._auth = dict(auth)
        self._markets: Set[str] = {str(market) for market in markets if market}
        self._state_lock = threading.RLock()
        self._on_event = on_event
        self._verbose = verbose
        self._url = user_ws_url(url_base)

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

    def subscribe(self, markets: Iterable[str]) -> None:
        normalized = {str(market) for market in markets if market}
        if not normalized:
            return
        with self._state_lock:
            self._markets.update(normalized)

    def unsubscribe(self, markets: Iterable[str]) -> None:
        normalized = {str(market) for market in markets if market}
        if not normalized:
            return
        with self._state_lock:
            self._markets.difference_update(normalized)

    def _market_snapshot(self) -> Set[str]:
        with self._state_lock:
            return set(self._markets)

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
                        print("[ws-user] error:", repr(exc.cause))
            except Exception as exc:
                if not stop_event.is_set():
                    self.last_error = type(exc).__name__
                    if self._verbose:
                        print("[ws-user] error:", repr(exc))
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

            sent_markets = self._market_snapshot()
            connection.send(
                json.dumps(build_user_subscription(self._auth, sorted(sent_markets)))
            )
            if not self._mark_connected(generation, stop_event, connection):
                return max(0.0, time.monotonic() - connected_at)
            self.last_error = ""
            self.last_connected_at = time.time()

            next_ping_at = time.monotonic()
            while not stop_event.is_set():
                sent_markets = self._sync_subscriptions(connection, sent_markets)
                now = time.monotonic()
                if now >= next_ping_at:
                    connection.send("PING")
                    next_ping_at = now + WEBSOCKET_PING_INTERVAL_SECONDS
                try:
                    message = connection.recv()
                except WebSocketTimeoutException:
                    continue
                except WebSocketConnectionClosedException:
                    break
                if message in (None, "", b""):
                    break
                self._handle_message(message)
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

    def _sync_subscriptions(self, connection: Any, sent_markets: Set[str]) -> Set[str]:
        current_markets = self._market_snapshot()
        removed = sorted(sent_markets - current_markets)
        if removed:
            connection.send(
                json.dumps({"markets": removed, "operation": "unsubscribe"})
            )
            sent_markets.difference_update(removed)

        added = sorted(current_markets - sent_markets)
        if added:
            connection.send(json.dumps({"markets": added, "operation": "subscribe"}))
            sent_markets.update(added)
        return sent_markets

    def _handle_message(self, message: Any) -> None:
        if isinstance(message, bytes):
            message = message.decode("utf-8", errors="replace")
        if message in {"PONG", "PING"}:
            return
        try:
            data = json.loads(message)
            if isinstance(data, dict):
                self._on_event(data)
        except Exception:
            if self._verbose:
                print("[ws-user] non-json:", str(message)[:200])

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
