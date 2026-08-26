from __future__ import annotations

import json
import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional

import requests

from .errors import MarketConfigurationError, MarketHTTPError


DEFAULT_USER_AGENT = "market-sentinel/1.0"
DEFAULT_TIMEOUT_SECONDS = 10.0
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
    ) -> None:
        self.market_id = str(market_id or "").strip().lower()
        self.config: Dict[str, Any] = dict(config or {})
        self.session = session or requests.Session()
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

    def describe(self) -> Dict[str, Any]:
        return {
            "market_id": self.market_id,
            "user_agent": self.user_agent,
            "timeout_seconds": self.timeout_seconds,
            "min_request_interval_seconds": self.rate_limiter.min_interval_seconds,
        }

    def request_json(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Any = None,
        headers: Optional[Mapping[str, str]] = None,
        max_response_bytes: Optional[int] = None,
    ) -> Any:
        if (
            max_response_bytes is not None
            and (
                isinstance(max_response_bytes, bool)
                or not isinstance(max_response_bytes, int)
                or max_response_bytes < 1
            )
        ):
            raise MarketConfigurationError("HTTP JSON response byte cap must be a positive integer.")
        self.rate_limiter.wait()
        request_headers = {"Accept": "application/json", "User-Agent": self.user_agent}
        request_headers.update(dict(headers or {}))
        request_options: Dict[str, Any] = {
            "params": dict(params or {}),
            "json": json_body,
            "headers": request_headers,
            "timeout": self.timeout_seconds,
        }
        if max_response_bytes is not None:
            request_options["stream"] = True
        try:
            response = self.session.request(
                method.upper(),
                url,
                **request_options,
            )
        except requests.RequestException as exc:
            raise MarketHTTPError(f"{self.market_id} HTTP request failed: {exc}") from exc

        status = int(getattr(response, "status_code", 0) or 0)
        if max_response_bytes is None:
            if status >= 400:
                text = str(getattr(response, "text", "") or "")
                raise MarketHTTPError(f"{self.market_id} HTTP {status}: {text[:200]}")

            try:
                return response.json()
            except ValueError:
                text = str(getattr(response, "text", "") or "")
                try:
                    return json.loads(text)
                except json.JSONDecodeError as exc:
                    raise MarketHTTPError(f"{self.market_id} response was not valid JSON.") from exc

        try:
            if status >= 400:
                raise MarketHTTPError(f"{self.market_id} HTTP {status}.")
            content_length = str(getattr(response, "headers", {}).get("Content-Length") or "").strip()
            if content_length:
                try:
                    declared_length = int(content_length)
                except ValueError:
                    declared_length = -1
                if declared_length > max_response_bytes:
                    raise MarketHTTPError(
                        f"{self.market_id} response exceeded the configured JSON byte cap."
                    )

            body = bytearray()
            try:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    body.extend(chunk)
                    if len(body) > max_response_bytes:
                        raise MarketHTTPError(
                            f"{self.market_id} response exceeded the configured JSON byte cap."
                        )
            except requests.RequestException as exc:
                raise MarketHTTPError(f"{self.market_id} HTTP response failed: {exc}") from exc
            try:
                return json.loads(body)
            except (ValueError, RecursionError) as exc:
                raise MarketHTTPError(f"{self.market_id} response was not valid JSON.") from exc
        finally:
            response.close()

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
