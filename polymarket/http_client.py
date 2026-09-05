from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import time
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, TypeVar

import requests

from core.request_control import current_request, request_scope
from .endpoints import PolymarketEndpoint


TRANSIENT_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
SAFE_RETRY_METHODS = {"GET", "HEAD", "OPTIONS"}


class PolymarketError(RuntimeError):
    """Base exception for Polymarket API wrapper failures."""


class PolymarketValidationError(PolymarketError, ValueError):
    """Raised before sending requests when local input violates documented contracts."""


class PolymarketHTTPError(PolymarketError):
    def __init__(
        self,
        message: str,
        *,
        service: str,
        method: str,
        url: str,
        status_code: Optional[int] = None,
        response_body: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.service = service
        self.method = method
        self.url = url
        self.status_code = status_code
        self.response_body = response_body


class PolymarketRateLimitError(PolymarketHTTPError):
    """Raised after retry handling cannot recover from a 429 response."""


class PolymarketResponseError(PolymarketError):
    """Raised when an endpoint returns malformed or unexpected JSON."""


class _ResponseTooLargeError(RuntimeError):
    pass


T = TypeVar("T")
DEFAULT_USER_AGENT = "market-sentinel/1.0"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
ERROR_RESPONSE_PREVIEW_BYTES = 4 * 1024
_ORIGINAL_REQUEST = requests.request


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 2
    backoff_seconds: float = 0.25
    max_sleep_seconds: float = 2.0

    def attempts_for(self, method: str) -> int:
        method = str(method).upper()
        if method in SAFE_RETRY_METHODS:
            return max(1, int(self.max_attempts))
        return 1


DEFAULT_RETRY_POLICY = RetryPolicy()


def compact_params(params: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in params.items() if value is not None}


def comma_join(values: Optional[Iterable[str]]) -> Optional[str]:
    if values is None:
        return None
    joined = ",".join(str(value) for value in values if str(value))
    return joined or None


def build_batch(items: Iterable[Any], *, max_items: Optional[int], name: str) -> List[Any]:
    cleaned = [item for item in items if item is not None and str(item)]
    if not cleaned:
        raise PolymarketValidationError(f"{name} requires at least one item.")
    if max_items is not None and len(cleaned) > max_items:
        raise PolymarketValidationError(f"{name} accepts at most {max_items} items; got {len(cleaned)}.")
    return cleaned


def endpoint_url(endpoint: PolymarketEndpoint, path: Optional[str] = None) -> str:
    return f"{endpoint.base_url}{path or endpoint.path}"


def request_json(
    endpoint: PolymarketEndpoint,
    *,
    path: Optional[str] = None,
    params: Optional[Mapping[str, Any]] = None,
    payload: Optional[Any] = None,
    headers: Optional[Mapping[str, str]] = None,
    timeout: float = 15.0,
    retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
) -> Any:
    body = _request(
        endpoint,
        path=path,
        params=params,
        payload=payload,
        headers=headers,
        timeout=timeout,
        retry_policy=retry_policy,
        json_fallback=True,
    )
    try:
        return json.loads(body)
    except (ValueError, RecursionError) as exc:
        raise PolymarketResponseError(
            f"{endpoint.service} {endpoint.method} {path or endpoint.path} returned non-JSON response."
        ) from exc


def request_bytes(
    endpoint: PolymarketEndpoint,
    *,
    path: Optional[str] = None,
    params: Optional[Mapping[str, Any]] = None,
    headers: Optional[Mapping[str, str]] = None,
    timeout: float = 30.0,
    retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
) -> bytes:
    return _request(
        endpoint,
        path=path,
        params=params,
        headers=headers,
        timeout=timeout,
        retry_policy=retry_policy,
    )


def as_dict(data: Any, *, endpoint_name: str) -> Dict[str, Any]:
    if isinstance(data, dict):
        return data
    raise PolymarketResponseError(f"{endpoint_name} expected an object response, got {type(data).__name__}.")


def as_list(data: Any, *, endpoint_name: str) -> List[Any]:
    if isinstance(data, list):
        return data
    raise PolymarketResponseError(f"{endpoint_name} expected an array response, got {type(data).__name__}.")


def as_list_of_dicts(data: Any, *, endpoint_name: str, wrapper_keys: Sequence[str] = ()) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in wrapper_keys:
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    raise PolymarketResponseError(f"{endpoint_name} expected an array response, got {type(data).__name__}.")


def optional_price(data: Any, keys: Sequence[str]) -> Optional[float]:
    try:
        if isinstance(data, dict):
            for key in keys:
                if key in data:
                    return float(data[key])
        if isinstance(data, (int, float, str)):
            return float(data)
    except Exception:
        return None
    return None


def _request(
    endpoint: PolymarketEndpoint,
    *,
    path: Optional[str] = None,
    params: Optional[Mapping[str, Any]] = None,
    payload: Optional[Any] = None,
    headers: Optional[Mapping[str, str]] = None,
    timeout: float,
    retry_policy: RetryPolicy,
    json_fallback: bool = False,
) -> bytes:
    try:
        with request_scope(timeout), _request_transport() as transport:
            return _request_with_transport(
                endpoint,
                transport=transport,
                path=path,
                params=params,
                payload=payload,
                headers=headers,
                timeout=timeout,
                retry_policy=retry_policy,
                json_fallback=json_fallback,
            )
    except requests.Timeout as exc:
        raise PolymarketHTTPError(
            f"{endpoint.service} {endpoint.method} request deadline exceeded.",
            service=endpoint.service, method=endpoint.method, url=endpoint_url(endpoint, path),
        ) from exc


@contextmanager
def _request_transport() -> Iterator[Callable[..., Any]]:
    # Lazy imports avoid the adapter registry's import cycle. One owned session
    # spans retries and streamed body reads, and is closed on every exit path.
    from market_adapters.errors import MarketConfigurationError
    from market_adapters.runtime import create_managed_http_session

    try:
        if requests.request is not _ORIGINAL_REQUEST:
            # Preserve the injected offline transport contract; endpoint syntax
            # and literal-address validation still apply in this path.
            yield requests.request
        else:
            with create_managed_http_session() as session:
                yield session.request
    except MarketConfigurationError as exc:
        raise PolymarketValidationError(str(exc)) from exc


def _request_with_transport(
    endpoint: PolymarketEndpoint,
    *,
    transport: Callable[..., Any],
    path: Optional[str],
    params: Optional[Mapping[str, Any]],
    payload: Optional[Any],
    headers: Optional[Mapping[str, str]],
    timeout: float,
    retry_policy: RetryPolicy,
    json_fallback: bool,
) -> bytes:
    method = endpoint.method.upper()
    url = _validated_endpoint_url(endpoint, path)
    request_headers = dict(headers or {})
    request_headers.setdefault("User-Agent", DEFAULT_USER_AGENT)
    request_headers.setdefault("Accept", "application/json")
    if payload is not None:
        request_headers.setdefault("Content-Type", "application/json")
    attempts = retry_policy.attempts_for(method)
    last_exc: Optional[BaseException] = None

    for attempt in range(1, attempts + 1):
        control = current_request()
        if control is not None:
            control.check()
        try:
            response = transport(
                method,
                url,
                params=compact_params(params or {}),
                json=payload,
                headers=request_headers or None,
                timeout=timeout,
                allow_redirects=False,
                stream=True,
            )
        except requests.TooManyRedirects as exc:
            # The managed session already closed the redirect response. Never
            # retry or follow it, including when it came from a safe GET.
            raise PolymarketHTTPError(
                f"{endpoint.service} {method} {url} returned a redirect, but redirects are disabled.",
                service=endpoint.service,
                method=method,
                url=url,
                status_code=exc.response.status_code if exc.response is not None else None,
            ) from exc
        except requests.RequestException as exc:
            if control is not None:
                control.check()
            last_exc = exc
            if attempt < attempts:
                _sleep_before_retry(attempt, retry_policy)
                continue
            raise PolymarketHTTPError(
                f"{endpoint.service} {method} {url} failed: {exc}",
                service=endpoint.service,
                method=method,
                url=url,
            ) from exc

        status_code = int(getattr(response, "status_code", 0) or 0)
        if 300 <= status_code < 400:
            try:
                preview = _response_preview(response)
            finally:
                _close_response(response)
            raise PolymarketHTTPError(
                f"{endpoint.service} {method} {url} returned a redirect, but redirects are disabled.",
                service=endpoint.service,
                method=method,
                url=url,
                status_code=status_code,
                response_body=preview,
            )

        if status_code >= 400 and status_code in TRANSIENT_STATUS_CODES and attempt < attempts:
            _close_response(response)
            _sleep_before_retry(attempt, retry_policy, response=response)
            continue

        if status_code >= 400:
            try:
                preview = _response_preview(response)
            finally:
                _close_response(response)
            error_cls = PolymarketRateLimitError if status_code == 429 else PolymarketHTTPError
            raise error_cls(
                f"{endpoint.service} {method} {url} returned HTTP {status_code}.",
                service=endpoint.service,
                method=method,
                url=url,
                status_code=status_code,
                response_body=preview,
            )

        read_error: Optional[requests.RequestException] = None
        try:
            content_length = _content_length(response)
            if content_length is not None and content_length > MAX_RESPONSE_BYTES:
                raise PolymarketHTTPError(
                    f"{endpoint.service} {method} {url} exceeded the {MAX_RESPONSE_BYTES}-byte response limit.",
                    service=endpoint.service,
                    method=method,
                    url=url,
                    status_code=status_code,
                )
            body = _read_response_body(
                response,
                max_bytes=MAX_RESPONSE_BYTES,
                json_fallback=json_fallback,
            )
        except _ResponseTooLargeError as exc:
            raise PolymarketHTTPError(
                f"{endpoint.service} {method} {url} exceeded the {MAX_RESPONSE_BYTES}-byte response limit.",
                service=endpoint.service,
                method=method,
                url=url,
                status_code=status_code,
            ) from exc
        except requests.RequestException as exc:
            read_error = exc
        finally:
            _close_response(response)

        if read_error is not None:
            if control is not None:
                control.check()
            last_exc = read_error
            if attempt < attempts:
                _sleep_before_retry(attempt, retry_policy)
                continue
            raise PolymarketHTTPError(
                f"{endpoint.service} {method} {url} failed while reading the response: {read_error}",
                service=endpoint.service,
                method=method,
                url=url,
                status_code=status_code,
            ) from read_error
        return body

    if last_exc is not None:
        raise PolymarketHTTPError(
            f"{endpoint.service} {method} {url} failed: {last_exc}",
            service=endpoint.service,
            method=method,
            url=url,
        ) from last_exc
    raise PolymarketHTTPError(f"{endpoint.service} {method} {url} failed.", service=endpoint.service, method=method, url=url)


def _sleep_before_retry(attempt: int, retry_policy: RetryPolicy, *, response: Optional[requests.Response] = None) -> None:
    retry_after = _retry_after_seconds(response)
    delay = retry_after if retry_after is not None else retry_policy.backoff_seconds * (2 ** max(0, attempt - 1))
    delay = min(max(0.0, delay), retry_policy.max_sleep_seconds)
    control = current_request()
    if control is None:
        time.sleep(delay)
    else:
        control.sleep(delay)


def _retry_after_seconds(response: Optional[requests.Response]) -> Optional[float]:
    if response is None:
        return None
    value = response.headers.get("Retry-After") if hasattr(response, "headers") else None
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _validated_endpoint_url(endpoint: PolymarketEndpoint, path: Optional[str]) -> str:
    # Import lazily to avoid the market_adapters package's adapter registry
    # importing this Polymarket client while it is still being initialized.
    from market_adapters.errors import MarketConfigurationError
    from market_adapters.outbound import validate_outbound_url

    url = endpoint_url(endpoint, path)
    try:
        return validate_outbound_url(
            url,
            setting_key=f"Polymarket {endpoint.service} endpoint",
            # Managed sessions validate DNS again for each attempt and carry
            # that address set through to the socket, including after prepare.
            resolve_addresses=False,
        )
    except MarketConfigurationError as exc:
        raise PolymarketValidationError(str(exc)) from exc


def _close_response(response: Any) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()


def _content_length(response: Any) -> Optional[int]:
    headers = getattr(response, "headers", {})
    value = str(headers.get("Content-Length") or "").strip() if hasattr(headers, "get") else ""
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _iter_response_content(response: Any, *, chunk_size: int, json_fallback: bool = False):
    iterator = getattr(response, "iter_content", None)
    if callable(iterator):
        yield from iterator(chunk_size=chunk_size)
        return

    content = getattr(response, "content", None)
    if content not in (None, b"", ""):
        yield content.encode("utf-8") if isinstance(content, str) else bytes(content)
        return

    text = getattr(response, "text", None)
    if text not in (None, ""):
        yield str(text).encode("utf-8")
        return

    if json_fallback:
        loader = getattr(response, "json", None)
        if callable(loader):
            try:
                yield json.dumps(loader()).encode("utf-8")
            except (TypeError, ValueError):
                return


def _read_response_body(
    response: Any,
    *,
    max_bytes: int,
    json_fallback: bool = False,
    truncate: bool = False,
) -> bytes:
    body = bytearray()
    for chunk in _iter_response_content(response, chunk_size=64 * 1024, json_fallback=json_fallback):
        if not chunk:
            continue
        raw = chunk.encode("utf-8") if isinstance(chunk, str) else bytes(chunk)
        remaining = max_bytes - len(body)
        if truncate:
            if remaining <= 0:
                break
            body.extend(raw[:remaining])
            if len(body) >= max_bytes:
                break
            continue
        if len(raw) > remaining:
            raise _ResponseTooLargeError
        body.extend(raw)
    return bytes(body)


def _response_preview(response: requests.Response) -> str:
    try:
        body = _read_response_body(
            response,
            max_bytes=ERROR_RESPONSE_PREVIEW_BYTES,
            json_fallback=True,
            truncate=True,
        )
    except Exception:
        return ""
    return body.decode("utf-8", errors="replace").strip()
