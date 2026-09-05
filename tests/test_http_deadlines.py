from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import socket
import threading
import time
import unittest
from unittest.mock import patch

import requests

from core.request_control import (
    RequestCancelled,
    RequestControl,
    RequestDeadlineExceeded,
    cancellation_scope,
    current_request,
    request_scope,
    resolve_with_deadline,
)
from market_adapters.errors import MarketHTTPError
from market_adapters.runtime import AdapterRuntime, RateLimiter, create_managed_http_session
from polymarket.endpoints import PolymarketEndpoint
from polymarket.http_client import PolymarketHTTPError, RetryPolicy, request_json


@contextmanager
def slow_server(*, protocol="HTTP/1.1", connection_close=False):
    stop = threading.Event()
    started = threading.Event()
    requests_seen = []
    handlers = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = protocol

        def do_GET(self):
            handlers.append(threading.current_thread())
            requests_seen.append((self.path, self.client_address[1]))
            body = b'{"ok":true}'
            try:
                if self.path == "/headers":
                    self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                    self.wfile.flush()
                    started.set()
                    while not stop.wait(0.03):
                        self.wfile.write(b"a")
                        self.wfile.flush()
                    return
                if self.path == "/retry":
                    self.send_response(503)
                    self.send_header("Retry-After", "10")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    started.set()
                    return
                self.send_response(400 if self.path == "/error" else 200)
                self.send_header("Content-Length", str(len(body) if self.path in {"/fast", "/slow-success"} else 10000))
                if connection_close:
                    self.send_header("Connection", "close")
                self.end_headers()
                started.set()
                if self.path == "/fast":
                    self.wfile.write(body)
                    self.wfile.flush()
                    return
                if self.path == "/slow-success":
                    for byte in body:
                        if stop.wait(0.03):
                            break
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                    return
                while not stop.wait(0.03):
                    self.wfile.write(b"a")
                    self.wfile.flush()
            except (ConnectionError, OSError):
                self.close_connection = True

        def do_POST(self):
            self.do_GET()

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        with patch.dict(os.environ, {
            "MARKET_SENTINEL_OUTBOUND_PRIVATE_ORIGINS": origin,
            "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1",
        }):
            try:
                yield origin, started, requests_seen
            finally:
                stop.set()
                server.shutdown()
                thread.join(timeout=5)
                for handler in set(handlers):
                    handler.join(timeout=1)


class HTTPDeadlineTests(unittest.TestCase):
    def tearDown(self):
        self.assertIsNone(current_request())
        self.assertFalse([thread for thread in threading.enumerate() if thread.name == "http-deadline"])

    def test_slow_headers_body_and_error_preview_obey_total_deadline(self):
        for path in ("/headers", "/body", "/error"):
            with self.subTest(path=path), slow_server() as (origin, _started, seen):
                before = time.monotonic()
                with self.assertRaises(PolymarketHTTPError) as raised:
                    request_json(PolymarketEndpoint("test", "GET", path, origin), timeout=0.25)
                self.assertIsInstance(raised.exception.__cause__, RequestDeadlineExceeded)
                self.assertLess(time.monotonic() - before, 1.0)
                self.assertEqual(len(seen), 1, "deadline must not restart a request")

    def test_runtime_has_the_same_body_deadline(self):
        with slow_server() as (origin, _started, seen):
            runtime = AdapterRuntime("test", timeout_seconds=0.25)
            try:
                before = time.monotonic()
                with self.assertRaises(MarketHTTPError):
                    runtime.request_json("GET", origin + "/body")
                self.assertLess(time.monotonic() - before, 1.0)
                self.assertEqual(len(seen), 1)
            finally:
                runtime.session.close()

    def test_nonpersistent_response_retains_deadline_and_cancellation(self):
        for protocol, connection_close in (("HTTP/1.0", False), ("HTTP/1.1", True)):
            for cancel in (False, True):
                with self.subTest(protocol=protocol, close=connection_close, cancel=cancel), slow_server(
                    protocol=protocol, connection_close=connection_close
                ) as (origin, started, _seen):
                    cancelled = threading.Event()

                    def cancel_when_started(started=started, cancelled=cancelled):
                        if started.wait(2):
                            cancelled.set()

                    worker = threading.Thread(target=cancel_when_started)
                    if cancel:
                        worker.start()
                    try:
                        before = time.monotonic()
                        with cancellation_scope(cancelled.is_set), self.assertRaises(
                            RequestCancelled if cancel else PolymarketHTTPError
                        ):
                            request_json(PolymarketEndpoint("test", "GET", "/body", origin), timeout=2 if cancel else 0.2)
                        self.assertLess(time.monotonic() - before, 1)
                    finally:
                        if cancel:
                            worker.join(timeout=3)

    def test_nonpersistent_response_close_releases_its_detached_guard(self):
        with slow_server(connection_close=True) as (origin, _started, _seen), create_managed_http_session() as session:
            response = session.get(origin + "/body", stream=True, timeout=2)
            control = response._market_sentinel_request_control
            self.assertTrue(control._sockets, "a connection-closing body still needs a guard")
            response.close()
            self.assertFalse(control._sockets)
            self.assertTrue(control._closed)

    def test_rate_limiter_lock_wait_obeys_deadline_and_cancellation(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                limiter = RateLimiter(0.1)
                limiter._lock.acquire()
                cancelled = threading.Event()
                release = threading.Timer(1.5, limiter._lock.release)
                canceller = threading.Timer(0.1, cancelled.set)
                release.start()
                if cancel:
                    canceller.start()
                try:
                    before = time.monotonic()
                    with cancellation_scope(cancelled.is_set), self.assertRaises(
                        RequestCancelled if cancel else RequestDeadlineExceeded
                    ), request_scope(3 if cancel else 0.1):
                        limiter.wait()
                    self.assertLess(time.monotonic() - before, 0.75)
                    self.assertEqual(limiter._next_allowed_at, 0)
                finally:
                    release.cancel()
                    canceller.cancel()
                    release.join()
                    if cancel:
                        canceller.join()
                    if limiter._lock.locked():
                        limiter._lock.release()

    def test_post_timeout_never_retries_a_potential_mutation(self):
        with slow_server() as (origin, _started, seen):
            with self.assertRaises(PolymarketHTTPError):
                request_json(
                    PolymarketEndpoint("test", "POST", "/body", origin), timeout=0.25,
                    retry_policy=RetryPolicy(max_attempts=10),
                )
            self.assertEqual(len(seen), 1)

    def test_retry_after_uses_the_same_budget(self):
        with slow_server() as (origin, _started, seen):
            before = time.monotonic()
            with self.assertRaises(PolymarketHTTPError) as raised:
                request_json(
                    PolymarketEndpoint("test", "GET", "/retry", origin), timeout=0.25,
                    retry_policy=RetryPolicy(max_attempts=3, max_sleep_seconds=10),
                )
            self.assertIsInstance(raised.exception.__cause__, RequestDeadlineExceeded)
            self.assertLess(time.monotonic() - before, 1.0)
            self.assertEqual(len(seen), 1)

    def test_cancellation_interrupts_active_body_and_retry_sleep(self):
        for path in ("/headers", "/body", "/retry"):
            with self.subTest(path=path), slow_server() as (origin, started, seen):
                cancelled = threading.Event()

                def cancel(started=started, cancelled=cancelled):
                    if started.wait(2):
                        cancelled.set()

                canceller = threading.Thread(target=cancel)
                canceller.start()
                try:
                    before = time.monotonic()
                    with cancellation_scope(cancelled.is_set), self.assertRaises(RequestCancelled):
                        request_json(
                            PolymarketEndpoint("test", "GET", path, origin), timeout=5,
                            retry_policy=RetryPolicy(max_attempts=3, max_sleep_seconds=10),
                        )
                    self.assertLess(time.monotonic() - before, 1.0)
                    self.assertEqual(len(seen), 1)
                finally:
                    canceller.join(timeout=3)

    def test_old_response_deadline_cannot_abort_reborrowed_connection(self):
        with slow_server() as (origin, _started, seen), create_managed_http_session() as session:
            first = session.get(origin + "/fast", stream=True, timeout=0.15)
            try:
                self.assertEqual(first.content, b'{"ok":true}')
                with session.get(origin + "/slow-success", timeout=2) as second:
                    self.assertEqual(second.json(), {"ok": True})
                self.assertEqual(len({port for _path, port in seen}), 1, "exercise actual connection reuse")
            finally:
                first.close()

    def test_closing_an_unconsumed_response_disarms_deadline(self):
        with slow_server() as (origin, _started, _seen), create_managed_http_session() as session:
            response = session.get(origin + "/body", stream=True, timeout=0.25)
            response.close()
            self.assertFalse(response._market_sentinel_request_control._sockets)

    def test_prepared_send_and_nonstream_response_are_controlled(self):
        with slow_server() as (origin, _started, _seen), create_managed_http_session() as session:
            with session.send(requests.Request("GET", origin + "/fast").prepare(), timeout=1) as response:
                self.assertTrue(response._market_sentinel_request_control._closed)
                self.assertEqual(response.json(), {"ok": True})
            with self.assertRaises(RequestDeadlineExceeded):
                session.send(requests.Request("GET", origin + "/headers").prepare(), timeout=0.25)

    def test_dns_timeout_has_bounded_helpers_and_no_http_continuation(self):
        release = threading.Event()
        entered = []
        finished = []
        slots = threading.BoundedSemaphore(2)

        def resolver():
            entered.append(threading.current_thread())
            try:
                release.wait(5)
                return ["127.0.0.1"]
            finally:
                finished.append(True)

        with patch("core.request_control._dns_slots", slots):
            try:
                for _ in range(4):
                    before = time.monotonic()
                    with self.assertRaises(RequestDeadlineExceeded), request_scope(0.08):
                        resolve_with_deadline(resolver)
                        self.fail("late DNS result must not continue the request")
                    self.assertLess(time.monotonic() - before, 0.5)
                self.assertEqual(len(entered), 2)
            finally:
                release.set()
                for worker in entered:
                    worker.join(timeout=2)
            self.assertEqual(len(finished), 2)
            self.assertTrue(slots.acquire(blocking=False))
            self.assertTrue(slots.acquire(blocking=False))

    def test_cancellation_during_dns_returns_before_lookup_finishes(self):
        release = threading.Event()
        cancelled = threading.Event()
        workers = []

        def resolver():
            workers.append(threading.current_thread())
            cancelled.set()
            release.wait(3)
            return []

        try:
            with cancellation_scope(cancelled.is_set), self.assertRaises(RequestCancelled), request_scope(2):
                resolve_with_deadline(resolver)
            self.assertFalse(release.is_set())
        finally:
            release.set()
            for worker in workers:
                worker.join(timeout=2)

    def test_dns_start_error_releases_admission_slot(self):
        slots = threading.BoundedSemaphore(1)
        with patch("core.request_control._dns_slots", slots), request_scope(1):
            with patch.object(threading.Thread, "start", side_effect=RuntimeError("cannot start")):
                with self.assertRaisesRegex(RuntimeError, "cannot start"):
                    resolve_with_deadline(lambda: [])
            self.assertTrue(slots.acquire(blocking=False))

    def test_watchdog_start_error_releases_guard_without_closing_caller_socket(self):
        left, right = socket.socketpair()
        control = RequestControl(1)
        try:
            with patch.object(threading.Thread, "start", side_effect=RuntimeError("cannot start")):
                with self.assertRaisesRegex(RuntimeError, "cannot start"):
                    control.watch_socket(left)
            self.assertFalse(control._sockets)
            self.assertIsNone(control._watcher)
            self.assertGreaterEqual(left.fileno(), 0)
        finally:
            control.close()
            left.close()
            right.close()

    def test_invalid_timeout_and_failed_cancellation_check_fail_closed(self):
        for value in (0, -1, float("inf"), float("nan")):
            with self.subTest(timeout=value), self.assertRaises(ValueError):
                RequestControl(value)
        with cancellation_scope(lambda: 1 / 0), self.assertRaises(RequestCancelled), request_scope(1):
            self.fail("failed cancellation source must not dispatch")

    def test_guard_shutdown_interrupts_recv_and_release_is_idempotent(self):
        left, right = socket.socketpair()
        control = RequestControl(0.1)
        try:
            left.settimeout(2)
            guard = control.watch_socket(left)
            before = time.monotonic()
            try:
                self.assertEqual(left.recv(1), b"")
            except OSError:
                pass  # Windows can report local shutdown instead of EOF.
            self.assertLess(time.monotonic() - before, 0.75)
            with self.assertRaises(RequestDeadlineExceeded):
                control.check()
            control.unwatch_socket(guard)
            control.unwatch_socket(guard)
        finally:
            control.close()
            control.close()
            left.close()
            right.close()


if __name__ == "__main__":
    unittest.main()
