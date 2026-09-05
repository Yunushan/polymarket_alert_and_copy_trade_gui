from __future__ import annotations

import json
import inspect
import math
import os
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional

import requests
import urllib3
from requests.adapters import HTTPAdapter
from requests.models import PreparedRequest
from requests.utils import select_proxy
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

from core.request_control import controlled_response, current_request, resolve_with_deadline
from .errors import MarketConfigurationError, MarketHTTPError
from .outbound import (
    OutboundEndpointPolicy,
    validate_outbound_url,
    validate_outbound_url_with_addresses,
)


DEFAULT_USER_AGENT = "market-sentinel/1.0"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
HARD_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
ERROR_RESPONSE_PREVIEW_BYTES = 4096
REDACTED = "***"


@dataclass(frozen=True)
class ResolvedCredential:
    name: str
    value: str
    source: str

    @property
    def redacted(self) -> str:
        return REDACTED if self.value else ""


class RateLimiter:
    """Small synchronous rate limiter for adapter HTTP calls."""

    def __init__(
        self,
        min_interval_seconds: float = 0.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_interval_seconds = max(0.0, float(min_interval_seconds or 0.0))
        self._clock = clock
        self._sleeper = sleeper
        self._lock = threading.Lock()
        self._next_allowed_at = 0.0

    def wait(self) -> float:
        if self.min_interval_seconds <= 0:
            return 0.0
        control = current_request()
        if control is None:
            self._lock.acquire()
        else:
            while not self._lock.acquire(timeout=min(0.05, control.remaining())):
                control.check()
        try:
            if control is not None:
                control.check()
            now = self._clock()
            delay = max(0.0, self._next_allowed_at - now)
            if delay:
                if control is None:
                    self._sleeper(delay)
                else:
                    control.sleep(delay)
                now = self._clock()
            self._next_allowed_at = max(now, self._next_allowed_at) + self.min_interval_seconds
            return delay
        finally:
            self._lock.release()


class _PinnedConnectionMixin:
    """Connect to addresses validated for the request while retaining TLS SNI."""

    def __init__(self, *args: Any, pinned_addresses: Iterable[str] = (), **kwargs: Any) -> None:
        self._market_sentinel_pinned_addresses = tuple(str(address) for address in pinned_addresses)
        self._request_control = None
        self._request_socket_guard = None
        super().__init__(*args, **kwargs)

    def bind_request_control(self, control) -> None:
        self.release_request_control()
        self._request_control = control
        if control is not None and self.sock is not None:
            self._request_socket_guard = control.watch_socket(self.sock)

    def release_request_control(self) -> None:
        if self._request_control is not None and self._request_socket_guard is not None:
            self._request_control.unwatch_socket(self._request_socket_guard)
        self._request_control = None
        self._request_socket_guard = None

    def close(self) -> None:
        # http.client closes the connection before returning a non-keepalive
        # response, whose makefile still owns the socket. The request must
        # retain that guard until the response body is consumed or closed.
        self._request_control = None
        self._request_socket_guard = None
        super().close()

    def connect(self) -> None:
        super().connect()
        control = self._request_control
        if control is not None:
            if self._request_socket_guard is not None:
                control.unwatch_socket(self._request_socket_guard)
            self._request_socket_guard = control.watch_socket(self.sock)

    def _new_conn(self):
        addresses = self._market_sentinel_pinned_addresses
        control = self._request_control
        if control is not None and not addresses:
            records = resolve_with_deadline(socket.getaddrinfo, self._dns_host, self.port, type=socket.SOCK_STREAM)
            addresses = tuple(dict.fromkeys(str(record[4][0]) for record in records))
        if not addresses:
            return super()._new_conn()

        # urllib3's ``host`` property reads ``_dns_host``. Restore it before
        # connect() performs TLS verification and before HTTP headers are sent;
        # only the raw socket connection uses the validated numeric address.
        original_dns_host = getattr(self, "_dns_host", getattr(self, "host", ""))
        original_timeout = self.timeout
        last_error: Optional[BaseException] = None
        try:
            for address in addresses:
                self._dns_host = address
                if control is not None:
                    self.timeout = min(float(original_timeout), control.remaining()) if original_timeout is not None else control.remaining()
                try:
                    sock = super()._new_conn()
                    if control is not None:
                        try:
                            # TCP connection time must not restart the TLS
                            # handshake's remaining timeout budget.
                            sock.settimeout(min(float(self.timeout), control.remaining()))
                            self._request_socket_guard = control.watch_socket(sock)
                        except BaseException:
                            sock.close()
                            raise
                    return sock
                except Exception as exc:  # pragma: no cover - urllib3 version-specific errors
                    if control is not None:
                        control.check()
                    last_error = exc
        finally:
            self._dns_host = original_dns_host
            self.timeout = original_timeout
        if last_error is not None:
            raise last_error
        return super()._new_conn()


class _PinnedHTTPConnection(_PinnedConnectionMixin, urllib3.connection.HTTPConnection):
    pass


class _PinnedHTTPSConnection(_PinnedConnectionMixin, urllib3.connection.HTTPSConnection):
    pass


class _RequestControlPoolMixin:
    def _get_conn(self, timeout=None):
        connection = super()._get_conn(timeout=timeout)
        try:
            connection.bind_request_control(current_request())
        except BaseException:
            connection.close()
            super()._put_conn(None)
            raise
        return connection

    def _put_conn(self, connection) -> None:
        if connection is not None:
            connection.release_request_control()
        super()._put_conn(connection)


class _PinnedHTTPConnectionPool(_RequestControlPoolMixin, HTTPConnectionPool):
    ConnectionCls = _PinnedHTTPConnection

    def __init__(self, host: str, port: Optional[int] = None, *, pinned_addresses: Iterable[str] = (), **kwargs: Any) -> None:
        self._market_sentinel_pinned_addresses = tuple(str(address) for address in pinned_addresses)
        kwargs["pinned_addresses"] = self._market_sentinel_pinned_addresses
        super().__init__(host, port, **kwargs)


class _PinnedHTTPSConnectionPool(_RequestControlPoolMixin, HTTPSConnectionPool):
    ConnectionCls = _PinnedHTTPSConnection

    def __init__(self, host: str, port: Optional[int] = None, *, pinned_addresses: Iterable[str] = (), **kwargs: Any) -> None:
        self._market_sentinel_pinned_addresses = tuple(str(address) for address in pinned_addresses)
        kwargs["pinned_addresses"] = self._market_sentinel_pinned_addresses
        super().__init__(host, port, **kwargs)


class _PinnedPoolManager(urllib3.PoolManager):
    """Isolate pools by both urllib3's TLS/origin key and validated addresses."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.pool_classes_by_scheme = dict(self.pool_classes_by_scheme)
        self.pool_classes_by_scheme.update(
            http=_PinnedHTTPConnectionPool,
            https=_PinnedHTTPSConnectionPool,
        )

    def connection_from_context(self, request_context: dict[str, Any]):
        context = dict(request_context)
        pinned_addresses = tuple(sorted({str(address) for address in context.pop("pinned_addresses", ())}))
        pool_key = self.key_fn_by_scheme[context["scheme"].lower()](context)
        context["pinned_addresses"] = pinned_addresses
        # Retain every standard TLS key field without passing our metadata to
        # urllib3's version-specific PoolKey constructor. Its bounded LRU now
        # evicts obsolete address sets instead of reusing a stale pinned pool.
        return self.connection_from_pool_key((pool_key, pinned_addresses), request_context=context)


class _PinnedHTTPAdapter(HTTPAdapter):
    """HTTP adapter that pins direct sockets to the session's last validation."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._market_sentinel_active_pins = threading.local()

    def init_poolmanager(self, connections: int, maxsize: int, block: bool = False, **pool_kwargs: Any) -> None:
        self.poolmanager = _PinnedPoolManager(
            num_pools=connections,
            maxsize=maxsize,
            block=block,
            **pool_kwargs,
        )

    def send(self, request: PreparedRequest, **kwargs: Any):
        control = current_request()
        if control is not None:
            timeout = kwargs.get("timeout")
            remaining = control.remaining()
            if isinstance(timeout, tuple):
                kwargs["timeout"] = tuple(min(float(value), remaining) if value is not None else remaining for value in timeout)
            elif timeout is None or isinstance(timeout, (float, int)):
                kwargs["timeout"] = min(float(timeout), remaining) if timeout is not None else remaining
        pins = tuple(getattr(request, "_market_sentinel_pinned_addresses", ()) or ())
        self._market_sentinel_active_pins.addresses = pins
        try:
            response = super().send(request, **kwargs)
            # Requests prepares response.next even with allow_redirects=False,
            # which can eagerly read an unbounded redirect body. Reject it
            # before it reaches Session.send's redirect preparation.
            if 300 <= response.status_code < 400:
                response.close()
                raise requests.TooManyRedirects("Outbound redirects are disabled.", response=response)
            return response
        finally:
            try:
                del self._market_sentinel_active_pins.addresses
            except AttributeError:
                pass

    def proxy_manager_for(self, proxy: str, **proxy_kwargs: Any):
        manager = super().proxy_manager_for(proxy, **proxy_kwargs)
        if not proxy.lower().startswith("socks"):
            manager.pool_classes_by_scheme = dict(manager.pool_classes_by_scheme)
            manager.pool_classes_by_scheme.update(http=_PinnedHTTPConnectionPool, https=_PinnedHTTPSConnectionPool)
        return manager

    def _direct_pinned_connection(self, url: str, pins: Iterable[str]):
        return self.poolmanager.connection_from_url(
            url,
            pool_kwargs={"pinned_addresses": tuple(pins)},
        )

    def get_connection(self, url: str, proxies: Optional[Mapping[str, str]] = None):
        # Requests <2.32 does not pass the PreparedRequest to a connection
        # hook.  ``send`` keeps the pins in thread-local state for that path.
        if select_proxy(url, proxies):
            return super().get_connection(url, proxies)
        pins = tuple(getattr(self._market_sentinel_active_pins, "addresses", ()) or ())
        if pins:
            return self._direct_pinned_connection(url, pins)
        return super().get_connection(url, proxies)

    def get_connection_with_tls_context(
        self,
        request: PreparedRequest,
        verify: Any,
        proxies: Optional[Mapping[str, str]] = None,
        cert: Any = None,
    ):
        # A configured proxy owns the target connection; retain Requests'
        # normal proxy handling while the origin is still validated by the
        # session before this hook runs.
        if select_proxy(request.url, proxies):
            return super().get_connection_with_tls_context(request, verify, proxies, cert)
        pins = tuple(getattr(request, "_market_sentinel_pinned_addresses", ()) or ())
        if not pins:
            return super().get_connection_with_tls_context(request, verify, proxies, cert)
        try:
            host_params, pool_kwargs = self.build_connection_pool_key_attributes(request, verify, cert)
        except ValueError as exc:
            raise requests.exceptions.InvalidURL(exc, request=request) from exc
        pool_kwargs = dict(pool_kwargs)
        pool_kwargs["pinned_addresses"] = pins
        return self.poolmanager.connection_from_host(**host_params, pool_kwargs=pool_kwargs)


class _ValidatingSession(requests.Session):
    """Requests session that also covers adapters using ``runtime.session`` directly."""

    def __init__(self, policy: OutboundEndpointPolicy) -> None:
        super().__init__()
        self._outbound_policy = policy
        self.mount("http://", _PinnedHTTPAdapter())
        self.mount("https://", _PinnedHTTPAdapter())

    def send(self, request: PreparedRequest, **kwargs: Any) -> requests.Response:
        timeout = kwargs.get("timeout", DEFAULT_TIMEOUT_SECONDS)
        if isinstance(timeout, tuple):
            timeout = sum(float(value) for value in timeout if value is not None) or DEFAULT_TIMEOUT_SECONDS
        return controlled_response(DEFAULT_TIMEOUT_SECONDS if timeout is None else timeout, self._send, request, **kwargs)

    def _send(self, request: PreparedRequest, **kwargs: Any) -> requests.Response:
        safe_url, addresses = validate_outbound_url_with_addresses(
            request.url,
            setting_key="outbound_url",
            policy=self._outbound_policy,
            resolve_addresses=True,
        )
        request.url = safe_url
        request._market_sentinel_pinned_addresses = tuple(str(address) for address in addresses)  # type: ignore[attr-defined]
        try:
            return super().send(request, **kwargs)
        finally:
            try:
                del request._market_sentinel_pinned_addresses  # type: ignore[attr-defined]
            except AttributeError:
                pass

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        timeout = kwargs.get("timeout", DEFAULT_TIMEOUT_SECONDS)
        if isinstance(timeout, tuple):
            timeout = sum(float(value) for value in timeout if value is not None) or DEFAULT_TIMEOUT_SECONDS
        return controlled_response(DEFAULT_TIMEOUT_SECONDS if timeout is None else timeout, self._request, method, url, **kwargs)

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        already_validated = bool(kwargs.pop("_market_sentinel_url_validated", False))
        if not already_validated:
            url = validate_outbound_url(
                url,
                setting_key="outbound_url",
                policy=self._outbound_policy,
                resolve_addresses=True,
            )
        kwargs["allow_redirects"] = False
        response = super().request(method, url, **kwargs)
        status = int(getattr(response, "status_code", 0) or 0)
        if 300 <= status < 400:
            response.close()
            raise requests.TooManyRedirects("Outbound redirects are disabled.", response=response)
        return response


def create_managed_http_session(*, outbound_policy: Optional[OutboundEndpointPolicy] = None) -> requests.Session:
    """Create an owned session with validation, direct socket pins and no redirects."""
    return _ValidatingSession(outbound_policy or OutboundEndpointPolicy.from_environment())


class AdapterRuntime:
    """Shared runtime helpers for market adapters.

    This keeps new adapters consistent around HTTP defaults, credential lookup,
    fixture loading, and local safety gates.
    """

    def __init__(
        self,
        market_id: str,
        config: Optional[Mapping[str, Any]] = None,
        *,
        session: Optional[requests.Session] = None,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout_seconds: Optional[float] = None,
        min_request_interval_seconds: Optional[float] = None,
        outbound_policy: Optional[OutboundEndpointPolicy] = None,
        resolve_outbound_addresses: Optional[bool] = None,
    ) -> None:
        self.market_id = str(market_id or "").strip().lower()
        self.config: Dict[str, Any] = dict(config or {})
        self.outbound_policy = outbound_policy or OutboundEndpointPolicy.from_environment()
        self.session = session or create_managed_http_session(outbound_policy=self.outbound_policy)
        self._resolve_outbound_addresses_override = resolve_outbound_addresses
        self.user_agent = str(self.config.get("user_agent") or user_agent)
        self.timeout_seconds = float(
            timeout_seconds
            if timeout_seconds is not None
            else self.config.get("http_timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
        )
        interval = (
            min_request_interval_seconds
            if min_request_interval_seconds is not None
            else self.config.get("min_request_interval_seconds", 0.0)
        )
        self.rate_limiter = RateLimiter(float(interval or 0.0))
        self.max_response_bytes = self._positive_byte_cap(
            self.config.get("http_max_response_bytes", DEFAULT_MAX_RESPONSE_BYTES),
            label="HTTP JSON response byte cap",
        )

    def describe(self) -> Dict[str, Any]:
        return {
            "market_id": self.market_id,
            "user_agent": self.user_agent,
            "timeout_seconds": self.timeout_seconds,
            "min_request_interval_seconds": self.rate_limiter.min_interval_seconds,
            "max_response_bytes": self.max_response_bytes,
        }

    def validate_endpoint(
        self,
        value: Any,
        *,
        setting_key: str,
        kind: str = "http",
        base_url: bool = False,
        resolve_addresses: Optional[bool] = None,
    ) -> str:
        return validate_outbound_url(
            value,
            setting_key=setting_key,
            kind=kind,
            base_url=base_url,
            policy=self.outbound_policy,
            resolve_addresses=(
                self._resolve_addresses_for_request()
                if resolve_addresses is None
                else bool(resolve_addresses)
            ),
        )

    @staticmethod
    def _positive_byte_cap(value: Any, *, label: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise MarketConfigurationError(f"{label} must be a positive integer.")
        if value > HARD_MAX_RESPONSE_BYTES:
            raise MarketConfigurationError(
                f"{label} cannot exceed {HARD_MAX_RESPONSE_BYTES} bytes."
            )
        return value

    def _managed_transport_is_intact(self) -> bool:
        if not isinstance(self.session, _ValidatingSession):
            return False
        request = getattr(self.session, "request", None)
        return getattr(request, "__self__", None) is self.session and getattr(
            request, "__func__", None
        ) is _ValidatingSession.request

    def _resolve_addresses_for_request(self) -> bool:
        if self._resolve_outbound_addresses_override is not None:
            return bool(self._resolve_outbound_addresses_override)
        # Test transports are often injected or installed on a runtime after
        # construction. They still receive syntax and literal-IP validation,
        # but must not trigger real DNS for reserved fixture domains.
        return self._managed_transport_is_intact()

    def _transport_accepts_keyword(self, keyword: str) -> bool:
        try:
            parameters = inspect.signature(self.session.request).parameters.values()
        except (TypeError, ValueError):
            return True
        return any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD or parameter.name == keyword
            for parameter in parameters
        )

    @staticmethod
    def _close_response(response: Any) -> None:
        close = getattr(response, "close", None)
        if callable(close):
            close()

    @staticmethod
    def _iter_response_content(
        response: Any,
        *,
        chunk_size: int,
        prefer_injected_json: bool = False,
    ):
        iterator = getattr(response, "iter_content", None)
        if callable(iterator):
            yield from iterator(chunk_size=chunk_size)
            return
        content = getattr(response, "content", None)
        if (
            content is None
            and prefer_injected_json
            and not isinstance(response, requests.Response)
        ):
            parser = getattr(response, "json", None)
            if callable(parser):
                try:
                    content = json.dumps(parser(), separators=(",", ":")).encode("utf-8")
                except (TypeError, ValueError, RecursionError):
                    content = None
        if content is None:
            content = str(getattr(response, "text", "") or "").encode("utf-8")
        elif isinstance(content, str):
            content = content.encode("utf-8")
        yield bytes(content)

    def request_response(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Any = None,
        data: Any = None,
        headers: Optional[Mapping[str, str]] = None,
        max_response_bytes: Optional[int] = None,
        error_context: Optional[str] = None,
    ) -> requests.Response:
        """Return a bounded response whose deadline lasts until close()."""
        try:
            return controlled_response(
                self.timeout_seconds, self._request_response, method, url,
                params=params, json_body=json_body, data=data, headers=headers,
                max_response_bytes=max_response_bytes, error_context=error_context,
            )
        except requests.RequestException as exc:
            raise MarketHTTPError(f"{error_context or self.market_id} HTTP request failed: {exc}") from exc

    def _request_response(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Any = None,
        data: Any = None,
        headers: Optional[Mapping[str, str]] = None,
        max_response_bytes: Optional[int] = None,
        error_context: Optional[str] = None,
    ) -> requests.Response:
        """Open one validated, non-redirecting, streamed HTTP response.

        Callers own the returned response and must close it. JSON callers should
        use :meth:`request_json`, which consumes it through the byte cap.
        """

        cap = (
            self.max_response_bytes
            if max_response_bytes is None
            else self._positive_byte_cap(max_response_bytes, label="HTTP response byte cap")
        )
        if json_body is not None and data is not None:
            raise MarketConfigurationError("HTTP request cannot contain both JSON and raw bodies.")
        subject = str(error_context or self.market_id).strip() or "market"
        safe_url = validate_outbound_url(
            url,
            setting_key=f"{self.market_id or 'market'} outbound URL",
            policy=self.outbound_policy,
            resolve_addresses=self._resolve_addresses_for_request(),
        )
        self.rate_limiter.wait()
        request_headers = {"Accept": "application/json", "User-Agent": self.user_agent}
        request_headers.update(dict(headers or {}))
        request_options: Dict[str, Any] = {
            "headers": request_headers,
            "timeout": self.timeout_seconds,
        }
        if params is not None:
            request_options["params"] = dict(params)
        if data is not None:
            request_options["data"] = data
        elif json_body is not None:
            request_options["json"] = json_body
        if self._transport_accepts_keyword("stream"):
            request_options["stream"] = True
        if self._transport_accepts_keyword("allow_redirects"):
            request_options["allow_redirects"] = False
        if self._managed_transport_is_intact():
            request_options["_market_sentinel_url_validated"] = True
        try:
            response = self.session.request(method.upper(), safe_url, **request_options)
        except requests.RequestException as exc:
            request_label = (
                f"{subject} request failed"
                if error_context
                else f"{subject} HTTP request failed"
            )
            raise MarketHTTPError(f"{request_label}: {exc}") from exc

        status = int(getattr(response, "status_code", 0) or 0)
        if 300 <= status < 400:
            self._close_response(response)
            raise MarketHTTPError(f"{subject} HTTP redirects are disabled.")
        if status <= 0 or status >= 400:
            try:
                preview = self._read_error_preview(response)
            finally:
                self._close_response(response)
            suffix = f": {preview}" if preview else "."
            raise MarketHTTPError(f"{subject} HTTP {status}{suffix}")

        content_length = str(getattr(response, "headers", {}).get("Content-Length") or "").strip()
        if content_length:
            try:
                declared_length = int(content_length)
            except ValueError:
                declared_length = -1
            if declared_length > cap:
                self._close_response(response)
                raise MarketHTTPError(
                    f"{subject} response exceeded the configured byte cap."
                )
        return response

    @staticmethod
    def _read_error_preview(response: requests.Response) -> str:
        body = bytearray()
        try:
            for chunk in AdapterRuntime._iter_response_content(response, chunk_size=1024):
                if not chunk:
                    continue
                remaining = ERROR_RESPONSE_PREVIEW_BYTES - len(body)
                if remaining <= 0:
                    break
                body.extend(chunk[:remaining])
                if len(body) >= ERROR_RESPONSE_PREVIEW_BYTES:
                    break
        except requests.RequestException:
            return ""
        return bytes(body).decode("utf-8", errors="replace").strip()[:200]

    def request_bytes(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Any = None,
        data: Any = None,
        headers: Optional[Mapping[str, str]] = None,
        max_response_bytes: Optional[int] = None,
        error_context: Optional[str] = None,
        _prefer_injected_json: bool = False,
    ) -> bytes:
        cap = (
            self.max_response_bytes
            if max_response_bytes is None
            else self._positive_byte_cap(
                max_response_bytes,
                label="HTTP response byte cap",
            )
        )
        response = self.request_response(
            method,
            url,
            params=params,
            json_body=json_body,
            data=data,
            headers=headers,
            max_response_bytes=cap,
            error_context=error_context,
        )
        try:
            body = bytearray()
            try:
                for chunk in self._iter_response_content(
                    response,
                    chunk_size=64 * 1024,
                    prefer_injected_json=_prefer_injected_json,
                ):
                    if not chunk:
                        continue
                    body.extend(chunk)
                    if len(body) > cap:
                        raise MarketHTTPError(
                            f"{str(error_context or self.market_id).strip() or 'market'} "
                            "response exceeded the configured byte cap."
                        )
            except requests.RequestException as exc:
                raise MarketHTTPError(f"{self.market_id} HTTP response failed: {exc}") from exc
            return bytes(body)
        finally:
            self._close_response(response)

    def request_text(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        data: Any = None,
        headers: Optional[Mapping[str, str]] = None,
        max_response_bytes: Optional[int] = None,
        encoding: str = "utf-8",
        error_context: Optional[str] = None,
    ) -> str:
        body = self.request_bytes(
            method,
            url,
            params=params,
            data=data,
            headers=headers,
            max_response_bytes=max_response_bytes,
            error_context=error_context,
        )
        try:
            return body.decode(encoding, errors="replace")
        except LookupError as exc:
            raise MarketConfigurationError("HTTP text response encoding is invalid.") from exc

    def request_json(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Any = None,
        data: Any = None,
        headers: Optional[Mapping[str, str]] = None,
        max_response_bytes: Optional[int] = None,
        allow_empty: bool = False,
    ) -> Any:
        cap = (
            self.max_response_bytes
            if max_response_bytes is None
            else self._positive_byte_cap(
                max_response_bytes,
                label="HTTP JSON response byte cap",
            )
        )
        body = self.request_bytes(
            method,
            url,
            params=params,
            json_body=json_body,
            data=data,
            headers=headers,
            max_response_bytes=cap,
            _prefer_injected_json=True,
        )
        if not body and allow_empty:
            return None
        try:
            return json.loads(body)
        except (ValueError, RecursionError) as exc:
            raise MarketHTTPError(f"{self.market_id} response was not valid JSON.") from exc

    def get_json(
        self,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        max_response_bytes: Optional[int] = None,
    ) -> Any:
        options: Dict[str, Any] = {"params": params, "headers": headers}
        if max_response_bytes is not None:
            options["max_response_bytes"] = max_response_bytes
        return self.request_json("GET", url, **options)

    def resolve_credential(
        self,
        config_key: str,
        env_vars: Iterable[str] = (),
        *,
        required: bool = False,
        label: str = "",
    ) -> Optional[ResolvedCredential]:
        display = label or config_key
        raw = self.config.get(config_key)
        if raw not in (None, ""):
            return ResolvedCredential(name=display, value=str(raw), source=f"config:{config_key}")

        for env_var in env_vars:
            value = os.getenv(env_var)
            if value:
                return ResolvedCredential(name=display, value=value, source=f"env:{env_var}")

        if required:
            names = ", ".join([config_key, *env_vars])
            raise MarketConfigurationError(f"Missing required credential for {self.market_id}: {names}")
        return None

    def config_bool(self, key: str, default: bool = False) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def config_float(self, key: str, default: Optional[float] = None) -> Optional[float]:
        value = self.config.get(key, default)
        if value in (None, ""):
            return default
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"{self.market_id} config {key} must be numeric.") from exc
        if not math.isfinite(number):
            raise MarketConfigurationError(f"{self.market_id} config {key} must be finite.")
        return number


def load_json_fixture(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_market_fixture(
    market_id: str,
    fixture_name: str,
    *,
    fixture_root: Optional[Path] = None,
) -> Any:
    root = fixture_root or Path(__file__).resolve().parent.parent / "tests" / "fixtures"
    name = fixture_name if fixture_name.endswith(".json") else f"{fixture_name}.json"
    return load_json_fixture(root / market_id / name)
