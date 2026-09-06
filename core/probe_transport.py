from __future__ import annotations

import http.client
import ipaddress
import math
import socket
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPHandler, HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener

from core.request_control import RequestControl, RequestDeadlineExceeded, resolve_with_deadline


class _RejectRedirects(HTTPRedirectHandler):
    # Python 3.10 lacks the stdlib 308 dispatch alias.
    http_error_308 = HTTPRedirectHandler.http_error_302

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise RuntimeError("health and deployment probe redirects are forbidden")


def is_public_probe_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return address.is_global and not address.is_multicast and not address.is_reserved


def _check_probe_timeout(control: RequestControl, exc: BaseException) -> None:
    control.check()
    reason = exc.reason if isinstance(exc, URLError) else exc
    # OS socket timeout rounding can expire just before the monotonic watchdog.
    if isinstance(reason, TimeoutError):
        raise RequestDeadlineExceeded("Probe network deadline exceeded.") from exc


class _ProbeConnectionMixin:
    def __init__(self, *args, control: RequestControl, public_only: bool, **kwargs):
        self._control = control
        self._public_only = public_only
        self._guard = None
        super().__init__(*args, **kwargs)
        self._create_connection = self._connect_exact

    def _connect_exact(self, address, timeout=None, source_address=None):
        control = self._control
        records = resolve_with_deadline(socket.getaddrinfo, *address, type=socket.SOCK_STREAM)
        if not records or any(record[0] not in (socket.AF_INET, socket.AF_INET6) for record in records):
            raise RuntimeError("probe DNS returned no usable addresses")
        if self._public_only and any(not is_public_probe_address(record[4][0]) for record in records):
            raise RuntimeError("public probe must resolve exclusively to public addresses")
        last_error = None
        for family, kind, protocol, _name, endpoint in records:
            sock = socket.socket(family, kind, protocol)
            guard = None
            try:
                sock.settimeout(control.remaining())
                guard = control.watch_socket(sock)
                if source_address is not None:
                    sock.bind(source_address)
                sock.connect(endpoint)
                control.check()
                sock.settimeout(control.remaining())
                self._guard = guard
                return sock
            except BaseException as exc:
                if guard is not None:
                    control.unwatch_socket(guard)
                sock.close()
                control.check()
                if not isinstance(exc, OSError):
                    raise
                last_error = exc
        raise last_error

    def connect(self):
        super().connect()
        self.sock.settimeout(self._control.remaining())
        if self._guard is not None:
            self._control.unwatch_socket(self._guard)
        self._guard = self._control.watch_socket(self.sock)
        # Keep the guard until response close, even if http.client closes its
        # socket object while the response's buffered reader still owns it.


class _ProbeHTTPConnection(_ProbeConnectionMixin, http.client.HTTPConnection):
    pass


class _ProbeHTTPSConnection(_ProbeConnectionMixin, http.client.HTTPSConnection):
    pass


class _ProbeHTTPHandler(HTTPHandler):
    def __init__(self, control: RequestControl, public_only: bool):
        super().__init__()
        self.control = control
        self.public_only = public_only

    def http_open(self, request):
        return self.do_open(_ProbeHTTPConnection, request, control=self.control, public_only=self.public_only)


class _ProbeHTTPSHandler(HTTPSHandler):
    def __init__(self, control: RequestControl, public_only: bool):
        super().__init__(context=ssl.create_default_context())
        self.control = control
        self.public_only = public_only

    def https_open(self, request):
        return self.do_open(_ProbeHTTPSConnection, request, context=self._context,
                            control=self.control, public_only=self.public_only)


class _ProbeResponse:
    def __init__(self, response, control: RequestControl):
        self._response = response
        self._control = control
        self._closed = False

    def __getattr__(self, name):
        return getattr(self._response, name)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        try:
            self._control.check()
        finally:
            self.close()

    def read(self, size=-1):
        try:
            self._control.check()
            body = self._response.read(size)
            self._control.check()
            return body
        except BaseException as exc:
            _check_probe_timeout(self._control, exc)
            raise

    def close(self):
        if not self._closed:
            self._closed = True
            try:
                self._response.close()
            finally:
                self._control.close()


def open_probe(request: Request, timeout: float, *, public_only: bool = False):
    """Open one direct, non-redirecting request with an owned network deadline."""
    if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("probe timeout must be finite and greater than zero")
    parsed = urlsplit(request.full_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("probe URL must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None or request.has_proxy():
        raise ValueError("probe URLs must not contain userinfo or explicit proxies")
    if public_only and parsed.scheme != "https":
        raise ValueError("public probes require HTTPS")
    control = RequestControl(timeout)
    response = None
    try:
        with control.activate():
            opener = build_opener(ProxyHandler({}), _RejectRedirects(),
                                  _ProbeHTTPHandler(control, public_only), _ProbeHTTPSHandler(control, public_only))
            response = opener.open(request, timeout=control.remaining())
            control.check()
        return _ProbeResponse(response, control)
    except BaseException as exc:
        try:
            if response is not None:
                response.close()
            if isinstance(exc, HTTPError):
                exc.close()
            _check_probe_timeout(control, exc)
        finally:
            control.close()
        raise
