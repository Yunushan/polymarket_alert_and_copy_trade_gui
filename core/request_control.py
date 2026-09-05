from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import math
import socket
import threading
import time
from typing import Any, Callable, Iterator, Optional

import requests


POLL_SECONDS = 0.05
MAX_DNS_LOOKUPS = 16
_dns_slots = threading.BoundedSemaphore(MAX_DNS_LOOKUPS)
_current: ContextVar[Optional["RequestControl"]] = ContextVar("http_request_control", default=None)
_cancel_check: ContextVar[Optional[Callable[[], bool]]] = ContextVar("http_cancel_check", default=None)


class RequestCancelled(Exception):
    """A read operation was cancelled; it must not be retried or cached as data."""


class RequestDeadlineExceeded(requests.Timeout):
    """The overall request deadline expired, independently of socket progress."""


class RequestControl:
    def __init__(self, timeout: float, *, cancel_check: Optional[Callable[[], bool]] = None) -> None:
        timeout = float(timeout)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("HTTP overall timeout must be finite and greater than zero.")
        self.deadline = time.monotonic() + timeout
        self.cancel_check = cancel_check if cancel_check is not None else _cancel_check.get()
        self._lock = threading.RLock()
        self._finished = threading.Event()
        self._closed = False
        self._reason: Optional[str] = None
        self._sockets: dict[socket.socket, socket.socket] = {}
        self._watcher: Optional[threading.Thread] = None

    @contextmanager
    def activate(self) -> Iterator["RequestControl"]:
        token = _current.set(self)
        try:
            self.check()
            yield self
        finally:
            _current.reset(token)

    def _abort(self, reason: str) -> None:
        with self._lock:
            if self._closed or self._reason is not None:
                return
            self._reason = reason
            self._finished.set()
            # Duplicate handles refer to the same connection even while TLS
            # replaces the original socket object. Never call SSL APIs here.
            for guard, original in self._sockets.items():
                # Windows select() must also be woken through the original
                # handle. The duplicate protects the connection during TLS
                # socket-object replacement and is independently owned here.
                for target in (original, guard):
                    try:
                        socket.socket.shutdown(target, socket.SHUT_RDWR)
                    except OSError:
                        pass
                # On Windows shutdown/close alone may leave select() waiting
                # while makefile() owns an I/O reference. Detach invalidates
                # the socket object before closing its handle, preventing a
                # later wrapper cleanup from closing a reused descriptor.
                descriptor = original.detach()
                if descriptor >= 0:
                    socket.close(descriptor)
                guard.close()
            self._sockets.clear()

    def check(self) -> None:
        with self._lock:
            if self._closed and self._reason is None:
                return
        if self.cancel_check is not None:
            try:
                cancelled = bool(self.cancel_check())
            except Exception:
                cancelled = True
            if cancelled:
                self._abort("cancelled")
        if time.monotonic() >= self.deadline:
            self._abort("deadline")
        with self._lock:
            reason = self._reason
        if reason == "cancelled":
            raise RequestCancelled("HTTP request cancelled.")
        if reason == "deadline":
            raise RequestDeadlineExceeded("HTTP overall request deadline exceeded.")

    def remaining(self) -> float:
        self.check()
        return max(0.000001, self.deadline - time.monotonic())

    def _watch(self) -> None:
        while not self._finished.wait(min(POLL_SECONDS, max(0.0, self.deadline - time.monotonic()))):
            try:
                self.check()
            except (RequestCancelled, RequestDeadlineExceeded):
                return

    def _start_watcher(self) -> None:
        with self._lock:
            if self._watcher is None and not self._finished.is_set():
                self._watcher = threading.Thread(target=self._watch, name="http-deadline", daemon=True)
                try:
                    self._watcher.start()
                except BaseException:
                    self._watcher = None
                    raise

    def watch_socket(self, sock: socket.socket) -> socket.socket:
        self.check()
        guard = socket.socket(fileno=socket.dup(sock.fileno()))
        with self._lock:
            if self._finished.is_set():
                guard.close()
                self.check()
                raise RuntimeError("Cannot attach a socket to a closed request.")
            self._sockets[guard] = sock
        try:
            self._start_watcher()
        except BaseException:
            self.unwatch_socket(guard)
            raise
        return guard

    def unwatch_socket(self, guard: socket.socket) -> None:
        # Serialize disarming with timeout shutdown before the pool can hand
        # this connection to a different request.
        with self._lock:
            if guard in self._sockets:
                del self._sockets[guard]
                guard.close()

    def sleep(self, delay: float) -> None:
        self.check()
        self._start_watcher()
        self._finished.wait(max(0.0, float(delay)))
        self.check()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._finished.set()
            for guard in self._sockets:
                guard.close()
            self._sockets.clear()
            watcher = self._watcher
        if watcher is not None and watcher is not threading.current_thread():
            watcher.join()


def current_request() -> Optional[RequestControl]:
    return _current.get()


@contextmanager
def cancellation_scope(check: Optional[Callable[[], bool]]) -> Iterator[None]:
    """The callback must be fast, side-effect-free and safe on a watcher thread."""
    token = _cancel_check.set(check)
    try:
        yield
    finally:
        _cancel_check.reset(token)


@contextmanager
def request_scope(timeout: float) -> Iterator[RequestControl]:
    existing = current_request()
    control = existing or RequestControl(timeout)
    try:
        with control.activate():
            try:
                yield control
            except Exception:
                control.check()
                raise
            control.check()
    finally:
        if existing is None:
            control.close()


def resolve_with_deadline(resolver: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    control = current_request()
    if control is None:
        return resolver(*args, **kwargs)
    control.check()
    while not _dns_slots.acquire(timeout=min(POLL_SECONDS, control.remaining())):
        control.check()
    done = threading.Event()
    result: list[Any] = []

    def resolve() -> None:
        # This worker owns only DNS. After timeout it cannot open a connection,
        # retain the request control or consume an unbounded executor queue.
        try:
            result.append((True, resolver(*args, **kwargs)))
        except BaseException as exc:
            result.append((False, exc))
        finally:
            _dns_slots.release()
            done.set()

    worker = threading.Thread(target=resolve, name="http-dns", daemon=True)
    try:
        control.check()
        worker.start()
    except BaseException:
        _dns_slots.release()
        raise
    while not done.wait(min(POLL_SECONDS, control.remaining())):
        control.check()
    control.check()
    success, value = result[0]
    if not success:
        raise value
    return value


def _attach_response(response: Any, control: RequestControl, *, owned: bool) -> Any:
    if getattr(response, "_market_sentinel_request_control", None) is control:
        if owned:
            response._market_sentinel_owns_request_control = True
            if getattr(response, "_content_consumed", False) is True:
                control.close()
        return response
    response._market_sentinel_request_control = control
    response._market_sentinel_owns_request_control = owned
    original_close = getattr(response, "close", None)
    original_iterator = getattr(response, "iter_content", None)

    def close() -> None:
        try:
            if callable(original_close):
                original_close()
        finally:
            if response._market_sentinel_owns_request_control:
                control.close()

    def iter_content(*args: Any, **kwargs: Any):
        try:
            control.check()
            for chunk in original_iterator(*args, **kwargs):
                control.check()
                yield chunk
            control.check()
        except BaseException:
            control.check()
            raise

    response.close = close
    if callable(original_iterator):
        response.iter_content = iter_content
    if owned and getattr(response, "_content_consumed", False) is True:
        control.close()
    return response


def controlled_response(_timeout: float, call: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Keep an owned streaming deadline alive until response.close()."""
    existing = current_request()
    control = existing or RequestControl(_timeout)
    response = None
    try:
        with control.activate():
            response = call(*args, **kwargs)
            control.check()
            return _attach_response(response, control, owned=existing is None)
    except BaseException:
        try:
            if response is not None:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
            control.check()
        finally:
            if existing is None:
                control.close()
        raise
