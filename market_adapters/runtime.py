from __future__ import annotations

import json
import inspect
import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional

import requests

from .errors import MarketConfigurationError, MarketHTTPError
from .outbound import (
    OutboundEndpointPolicy,
    validate_outbound_url,
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
        with self._lock:
            now = self._clock()
            delay = max(0.0, self._next_allowed_at - now)
            if delay:
                self._sleeper(delay)
                now = self._clock()
            self._next_allowed_at = max(now, self._next_allowed_at) + self.min_interval_seconds
            return delay


class _ValidatingSession(requests.Session):
    """Requests session that also covers adapters using ``runtime.session`` directly."""

    def __init__(self, policy: OutboundEndpointPolicy) -> None:
        super().__init__()
        self._outbound_policy = policy

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
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
        self.session = session or _ValidatingSession(self.outbound_policy)
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
