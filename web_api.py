from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hmac
import hashlib
import io
import importlib.metadata as importlib_metadata
import ipaddress
import json
import math
import mimetypes
import os
import posixpath
import re
import secrets
import signal
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from core.request_control import RequestCancelled, cancellation_scope, request_scope
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, cast
from urllib.parse import parse_qs, unquote, urlparse

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 compatibility.
    import tomli as tomllib

from core.models import (
    AppConfig,
    CopyTradeSettings,
    MarketConfig,
    MutationJournalEntry,
    PaperTradeRecord,
    PriceAlert,
    UIDesign,
    WalletWatch,
    bounded_mutation_result,
    MAX_MUTATION_RESULT_BYTES,
    MARKET_SAFETY_BOOLEAN_FIELDS,
    MARKET_SAFETY_LIMIT_FIELDS,
)
from core.config_security import assert_no_persisted_secrets, is_sensitive_display_key
from core.deployment_identity import capture_runtime_identity
from core.json_validation import loads_strict_json
from core.storage import ConfigConflictError, DEFAULT_CONFIG_PATH, load_config, save_config
from market_adapters import build_default_registry, support_matrix_entry, support_matrix_summary
from market_adapters.registry import AdapterRegistry
from market_adapters.catalog import MARKET_CATALOG, MARKET_IDS
from market_adapters.errors import MarketConfigurationError, UnsupportedFeatureError
from market_adapters.identity import activity_identity_hint, normalize_activity_identity
from market_adapters.outbound import is_outbound_endpoint_setting
from market_adapters.types import (
    MarketCapabilities,
    MarketCandle,
    MarketContract,
    MarketEvent,
    MarketMetadata,
    OrderBookSnapshot,
    PaperOrderRequest,
    PaperOrderResult,
    PriceSnapshot,
    MarketTrade,
)
from polymarket import data_api, gamma
from polymarket.analytics_cache import (
    ANALYTICS_CACHE_VERSION,
    DEFAULT_ANALYTICS_CACHE_MAX_ENTRIES,
    DEFAULT_ANALYTICS_CACHE_TTL_SECONDS,
    POLYMARKET_MDD_AUDIT_KIND,
    analytics_cache_health,
    analytics_cache_path,
    analytics_cache_summary,
    list_analytics_artifacts,
    load_analytics_artifact,
    mdd_payload_to_csv,
    purge_analytics_artifacts,
    store_analytics_artifact,
)
from polymarket.auth_readiness import build_clob_auth_readiness
from polymarket.coverage import polymarket_official_api_coverage
from polymarket.credential_runbook import build_polymarket_credential_runbook
from polymarket.http_client import PolymarketHTTPError, PolymarketRateLimitError
from polymarket.live_verification import (
    ABSOLUTE_MAX_VERIFY_NOTIONAL,
    ABSOLUTE_MAX_VERIFY_SIZE,
    CONFIRM_LIVE_ORDER_CANCEL,
    build_live_validation_stage_gates,
)
from polymarket.live_reports import (
    LIVE_VALIDATION_IDEMPOTENCY_KEY_MAX_LENGTH,
    LIVE_VALIDATION_DECISIONS_VERSION,
    LIVE_VALIDATION_PROMOTION_PROPOSAL_SNAPSHOTS_VERSION,
    LIVE_VALIDATION_REPORTS_VERSION,
    LiveValidationStoreDurabilityError,
    list_live_validation_report_decisions,
    list_live_validation_coverage_promotion_proposal_snapshots,
    load_live_validation_coverage_promotion_proposal_snapshot,
    live_validation_coverage_promotion_proposal,
    live_validation_coverage_promotion_proposal_export_filename,
    live_validation_coverage_promotion_proposal_markdown,
    live_validation_promotion_proposal_snapshot_export_filename,
    live_validation_promotion_proposal_snapshot_diff_markdown,
    live_validation_promotion_proposal_snapshot_markdown,
    live_validation_report_payload_hash,
    live_validation_report_review_bundle,
    live_validation_report_review_export_filename,
    live_validation_report_review_markdown,
    live_validation_report_decisions_markdown,
    live_validation_report_promotion_inventory,
    list_live_validation_reports,
    live_validation_decisions_path,
    live_validation_promotion_proposal_snapshots_path,
    live_validation_reports_path,
    load_live_validation_report,
    purge_live_validation_coverage_promotion_proposal_snapshots,
    purge_live_validation_reports,
    reconcile_live_validation_promotion_proposal_snapshot_idempotency,
    reconcile_live_validation_report_idempotency,
    record_live_validation_report_decision,
    store_live_validation_coverage_promotion_proposal_snapshot,
    store_live_validation_report,
)
from polymarket.live_report_schema import LiveValidationReportSchemaError, parse_live_validation_report_json
from polymarket.leaderboard import (
    LEADERBOARD_MAX_OFFSET, PNL_VOLUME_BASIS, normalize_leaderboard_category,
    performance_ratio_metadata, wallet_membership_fingerprint,
)
from polymarket.mdd import (
    DEFAULT_CACHE_TTL_SECONDS as POLYMARKET_MDD_CACHE_TTL_SECONDS,
    MDD_CALCULATION_VERSION,
    MDD_MARK_REPLAY_ASSUMPTIONS,
    MDD_MARK_REPLAY_LIMITATIONS,
    MDD_ACCOUNTING_ASSUMPTIONS,
    MDD_ACCOUNTING_LIMITATIONS,
    MDD_METHOD_MARK_REPLAY,
    MDD_METHOD_V2,
    MDD_PCT_BASIS_V2,
    MDD_V2_ASSUMPTIONS,
    MDD_V2_LIMITATIONS,
    max_drawdown,
    polymarket_user_mdd_payload_mark_replay,
    polymarket_user_mdd_payload_v2,
)
from polymarket.util import normalize_wallet
from polymarket.ws_user import build_user_subscription


PROJECT_ROOT = Path(__file__).resolve().parent
# PyInstaller's onedir layout keeps the bundled Python modules below the
# release root and places ``frontend/dist`` beside the executable.  Derive the
# release root from this module rather than from ``sys.executable`` so a
# deployment cannot turn the static-file root into attacker-controlled input.
_RESOURCE_ROOT = PROJECT_ROOT.parent if getattr(sys, "frozen", False) else PROJECT_ROOT
DEFAULT_FRONTEND_DIR = _RESOURCE_ROOT / "frontend" / "dist"
PROJECT_NAME = "market-sentinel"
HASHED_FRONTEND_ASSET_RE = re.compile(r"-[A-Za-z0-9_-]{8,}\.[^.]+$")
STATIC_FRONTEND_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MAX_JSON_BODY_BYTES = 1_000_000
MAX_HTTP_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_HTTP_WORKERS = 32
MAX_HTTP_MUTATION_WORKERS = 8
HTTP_RESERVED_READ_WORKERS = 4
HTTP_CONNECTION_TIMEOUT_SECONDS = 15.0
HTTP_OVERLOAD_RETRY_AFTER_SECONDS = 1
HTTP_MUTATION_LOCK_TIMEOUT_SECONDS = 5.0
HTTP_MUTATION_DRAIN_TIMEOUT_SECONDS = 10.0
HTTP_RESPONSE_CHUNK_BYTES = 32 * 1024
LOCAL_READINESS_CACHE_SECONDS = 5.0
MAX_READINESS_JSON_STORE_BYTES = 64 * 1024 * 1024
AUTH_FAILURE_MAX_ATTEMPTS = 10
AUTH_FAILURE_WINDOW_SECONDS = 60.0
MAX_TRACKED_AUTH_FAILURE_CLIENTS = 1_024
PYTHON_GUI_COMMAND = "python app.py"
PYTHON_GUI_SCRIPT = "run_gui.bat"
REACT_DEV_COMMAND = "run_web_gui_dev.bat"
REACT_DEV_MANUAL_COMMAND = "python web_api.py --host 127.0.0.1 --port 8765 + cd frontend && npm run dev"
REACT_BUILD_COMMAND = "cd frontend && npm install && npm run build"
REACT_PROD_COMMAND = "run_web_gui_prod.bat"
IDEMPOTENT_MUTATION_ROUTES = frozenset(
    {
        "/api/polymarket/live-validation/reports",
        "/api/polymarket/live-validation/decisions",
        "/api/polymarket/live-validation/promotion-proposal/snapshots",
    }
)
LOCAL_DURABLE_CREATE_ROUTES = frozenset(
    {
        "/api/alerts",
        "/api/wallets",
        "/api/paper/orders",
    }
)
LIVE_MUTATION_RECONCILIATION_CONFIRMATION = "I_VERIFIED_VENUE_ORDER_HISTORY"


class HttpRequestMetrics:
    """Thread-safe, bounded Prometheus metrics for the local HTTP server."""

    _METHODS = frozenset({"GET", "POST", "PATCH", "DELETE", "OPTIONS"})

    def __init__(
        self,
        max_http_workers: int = MAX_HTTP_WORKERS,
        max_mutation_workers: int = MAX_HTTP_MUTATION_WORKERS,
        reserved_read_workers: int = HTTP_RESERVED_READ_WORKERS,
    ) -> None:
        self._lock = threading.Lock()
        self._started_at = time.time()
        self._max_http_workers = max(1, int(max_http_workers))
        self._max_mutation_workers = max(1, int(max_mutation_workers))
        self._reserved_read_workers = max(0, int(reserved_read_workers))
        self._requests: Dict[Tuple[str, int], int] = {}
        self._duration_seconds_total = 0.0
        self._requests_in_flight = 0
        self._overload_rejections = 0
        self._response_too_large = 0
        self._mutations_in_flight = 0
        self._mutations_active = 0
        self._mutation_admission_rejections = 0
        self._mutation_lock_timeouts = 0

    def request_started(self) -> None:
        with self._lock:
            self._requests_in_flight += 1

    def request_finished(self) -> None:
        with self._lock:
            self._requests_in_flight = max(0, self._requests_in_flight - 1)

    def record_overload_rejection(self) -> None:
        with self._lock:
            self._overload_rejections += 1

    def record_response_too_large(self) -> None:
        with self._lock:
            self._response_too_large += 1

    def mutation_admitted(self) -> None:
        with self._lock:
            self._mutations_in_flight += 1

    def mutation_finished(self) -> None:
        with self._lock:
            self._mutations_in_flight = max(0, self._mutations_in_flight - 1)

    def mutation_started(self) -> None:
        with self._lock:
            self._mutations_active += 1

    def mutation_stopped(self) -> None:
        with self._lock:
            self._mutations_active = max(0, self._mutations_active - 1)

    def record_mutation_admission_rejection(self) -> None:
        with self._lock:
            self._mutation_admission_rejections += 1

    def record_mutation_lock_timeout(self) -> None:
        with self._lock:
            self._mutation_lock_timeouts += 1

    def snapshot(self) -> Dict[str, Any]:
        """Return a consistent bounded admission snapshot for readiness checks."""
        with self._lock:
            return {
                "started_at": self._started_at,
                "requests_in_flight": self._requests_in_flight,
                "overload_rejections_total": self._overload_rejections,
                "response_too_large_total": self._response_too_large,
                "mutations_in_flight": self._mutations_in_flight,
                "mutations_active": self._mutations_active,
                "mutation_admission_rejections_total": self._mutation_admission_rejections,
                "mutation_lock_timeouts_total": self._mutation_lock_timeouts,
                "max_http_workers": self._max_http_workers,
                "max_mutation_workers": self._max_mutation_workers,
                "reserved_read_workers": self._reserved_read_workers,
            }

    def record(self, method: str, status: int, duration_seconds: float) -> None:
        normalized_method = str(method or "OTHER").upper()
        if normalized_method not in self._METHODS:
            normalized_method = "OTHER"
        normalized_status = int(status) if 100 <= int(status) <= 599 else 500
        with self._lock:
            key = (normalized_method, normalized_status)
            self._requests[key] = self._requests.get(key, 0) + 1
            self._duration_seconds_total += max(0.0, float(duration_seconds))

    def prometheus_text(self) -> str:
        """Render aggregate metrics without unbounded user-controlled labels."""
        with self._lock:
            rows = sorted(self._requests.items())
            total = sum(self._requests.values())
            duration_seconds_total = self._duration_seconds_total
            started_at = self._started_at
            requests_in_flight = self._requests_in_flight
            overload_rejections = self._overload_rejections
            response_too_large = self._response_too_large
            mutations_in_flight = self._mutations_in_flight
            mutations_active = self._mutations_active
            mutation_admission_rejections = self._mutation_admission_rejections
            mutation_lock_timeouts = self._mutation_lock_timeouts
            max_http_workers = self._max_http_workers
            max_mutation_workers = self._max_mutation_workers
            reserved_read_workers = self._reserved_read_workers
        lines = [
            "# HELP market_sentinel_http_requests_total Completed HTTP requests by method and status.",
            "# TYPE market_sentinel_http_requests_total counter",
        ]
        for (method, status), count in rows:
            lines.append(
                f'market_sentinel_http_requests_total{{method="{method}",status="{status}"}} {count}'
            )
        lines.extend(
            [
                "# HELP market_sentinel_http_request_duration_seconds_total Total completed HTTP request duration.",
                "# TYPE market_sentinel_http_request_duration_seconds_total counter",
                f"market_sentinel_http_request_duration_seconds_total {duration_seconds_total:.6f}",
                "# HELP market_sentinel_http_requests_completed_total Total completed HTTP requests.",
                "# TYPE market_sentinel_http_requests_completed_total counter",
                f"market_sentinel_http_requests_completed_total {total}",
                "# HELP market_sentinel_http_requests_in_flight Currently admitted HTTP request workers.",
                "# TYPE market_sentinel_http_requests_in_flight gauge",
                f"market_sentinel_http_requests_in_flight {requests_in_flight}",
                "# HELP market_sentinel_http_overload_rejections_total HTTP connections rejected at the worker limit.",
                "# TYPE market_sentinel_http_overload_rejections_total counter",
                f"market_sentinel_http_overload_rejections_total {overload_rejections}",
                "# HELP market_sentinel_http_response_too_large_total Responses rejected before headers because they exceeded the byte limit.",
                "# TYPE market_sentinel_http_response_too_large_total counter",
                f"market_sentinel_http_response_too_large_total {response_too_large}",
                "# HELP market_sentinel_http_mutations_in_flight Mutation callers admitted for body parsing, lock wait, or execution.",
                "# TYPE market_sentinel_http_mutations_in_flight gauge",
                f"market_sentinel_http_mutations_in_flight {mutations_in_flight}",
                "# HELP market_sentinel_http_mutations_active Mutations currently holding the serialization lock.",
                "# TYPE market_sentinel_http_mutations_active gauge",
                f"market_sentinel_http_mutations_active {mutations_active}",
                "# HELP market_sentinel_http_mutation_admission_rejections_total Mutation requests rejected before body parsing because admission was saturated.",
                "# TYPE market_sentinel_http_mutation_admission_rejections_total counter",
                f"market_sentinel_http_mutation_admission_rejections_total {mutation_admission_rejections}",
                "# HELP market_sentinel_http_mutation_lock_timeouts_total Admitted mutations rejected after a bounded serialization-lock wait.",
                "# TYPE market_sentinel_http_mutation_lock_timeouts_total counter",
                f"market_sentinel_http_mutation_lock_timeouts_total {mutation_lock_timeouts}",
                "# HELP market_sentinel_http_worker_limit Maximum concurrently admitted HTTP connections.",
                "# TYPE market_sentinel_http_worker_limit gauge",
                f"market_sentinel_http_worker_limit {max_http_workers}",
                "# HELP market_sentinel_http_mutation_admission_limit Maximum mutation body readers, lock waiters, and executors.",
                "# TYPE market_sentinel_http_mutation_admission_limit gauge",
                f"market_sentinel_http_mutation_admission_limit {max_mutation_workers}",
                "# HELP market_sentinel_http_reserved_read_workers HTTP worker capacity unavailable to mutation admission.",
                "# TYPE market_sentinel_http_reserved_read_workers gauge",
                f"market_sentinel_http_reserved_read_workers {reserved_read_workers}",
                "# HELP market_sentinel_http_server_start_time_seconds Unix time when the HTTP server started.",
                "# TYPE market_sentinel_http_server_start_time_seconds gauge",
                f"market_sentinel_http_server_start_time_seconds {started_at:.6f}",
                "",
            ]
        )
        return "\n".join(lines)


def _safe_http_header_value(value: Any) -> str:
    """Remove line separators so untrusted data cannot create extra headers."""
    return str(value).replace("\r", "").replace("\n", "")


def _safe_attachment_filename(value: Any) -> str:
    """Return a quoted Content-Disposition filename without header delimiters."""
    filename = _safe_http_header_value(value).replace('"', "").strip()
    return filename or "download"


def static_cache_control(relative_path: Optional[str]) -> str:
    """Avoid stale SPA shells while allowing immutable content-hashed assets.

    ``relative_path`` is the already-validated route classification, rather
    than a filesystem path.  This keeps cache policy independent of platform
    path canonicalization such as macOS's ``/var`` to ``/private/var`` alias.
    """
    if relative_path is None:
        return "no-store"
    if relative_path == "index.html":
        return "no-store"
    if relative_path.startswith("assets/") and HASHED_FRONTEND_ASSET_RE.search(
        relative_path.rsplit("/", 1)[-1]
    ):
        return "public, max-age=31536000, immutable"
    return "no-cache, max-age=0, must-revalidate"


def project_version() -> str:
    """Return installed distribution metadata, with a source-checkout fallback."""
    try:
        return importlib_metadata.version(PROJECT_NAME)
    except importlib_metadata.PackageNotFoundError:
        pass
    try:
        data = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    except Exception:
        return "unknown"
    return str(data.get("project", {}).get("version") or "unknown")


def _normalize_allowed_origin(value: Any) -> str:
    """Accept only a canonical HTTP(S) browser origin without credentials or paths."""
    origin = str(value).strip().rstrip("/")
    if not origin or origin != _safe_http_header_value(origin):
        return ""
    parsed = urlparse(origin)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.params
        or parsed.query
        or parsed.fragment
        or "*" in parsed.netloc
    ):
        return ""
    canonical = f"{parsed.scheme}://{parsed.netloc}"
    return canonical if origin == canonical else ""


def configured_allowed_origins(cli_origins: Optional[Sequence[str]] = None) -> List[str]:
    """Return normalized CORS origins from explicit flags and protected service env."""
    values = [str(origin) for origin in (cli_origins or [])]
    values.extend(os.environ.get("MARKET_SENTINEL_ALLOWED_ORIGINS", "").split(","))
    origins: List[str] = []
    for value in values:
        normalized = _normalize_allowed_origin(value)
        if normalized and normalized not in origins:
            origins.append(normalized)
    return origins


API_ROUTES = {
    "GET": [
        "/metrics",
        "/api/health",
        "/api/state",
        "/api/config",
        "/api/mutations",
        "/api/markets",
        "/api/markets/support-matrix",
        "/api/markets/{market_id}/support",
        "/api/markets/{market_id}/events",
        "/api/markets/{market_id}/contracts",
        "/api/markets/{market_id}/price",
        "/api/markets/{market_id}/orderbook",
        "/api/markets/{market_id}/trades",
        "/api/markets/{market_id}/candles",
        "/api/markets/{market_id}/account/{operation}",
        "/api/alerts",
        "/api/wallets",
        "/api/copy",
        "/api/live-safety",
        "/api/paper",
        "/api/paper/history",
        "/api/paper/positions",
        "/api/polymarket/users/search",
        "/api/polymarket/users/leaderboard",
        "/api/polymarket/users/mdd",
        "/api/polymarket/users/mdd/cache",
        "/api/polymarket/users/mdd/cache/health",
        "/api/polymarket/users/mdd/export.json",
        "/api/polymarket/users/mdd/export.csv",
        "/api/polymarket/coverage",
        "/api/polymarket/clob-readiness",
        "/api/polymarket/live-validation",
        "/api/polymarket/live-validation/reports",
        "/api/polymarket/live-validation/reports/{key}",
        "/api/polymarket/live-validation/reports/{key}/export.json",
        "/api/polymarket/live-validation/reports/{key}/review.json",
        "/api/polymarket/live-validation/reports/{key}/review.md",
        "/api/polymarket/live-validation/decisions",
        "/api/polymarket/live-validation/decisions/export.json",
        "/api/polymarket/live-validation/decisions/export.md",
        "/api/polymarket/live-validation/promotion-proposal",
        "/api/polymarket/live-validation/promotion-proposal/export.json",
        "/api/polymarket/live-validation/promotion-proposal/export.md",
        "/api/polymarket/live-validation/promotion-proposal/snapshots",
        "/api/polymarket/live-validation/promotion-proposal/snapshots/{key}",
        "/api/polymarket/live-validation/promotion-proposal/snapshots/{key}/export.json",
        "/api/polymarket/live-validation/promotion-proposal/snapshots/{key}/export.md",
        "/api/polymarket/live-validation/promotion-proposal/snapshots/{key}/diff.json",
        "/api/polymarket/live-validation/promotion-proposal/snapshots/{key}/diff.md",
    ],
    "PATCH": [
        "/api/config",
        "/api/markets/{market_id}",
        "/api/wallets/{wallet_id}",
        "/api/wallets/polling",
        "/api/copy",
    ],
    "POST": [
        "/api/alerts",
        "/api/alerts/refresh",
        "/api/alerts/{alert_id}/refresh",
        "/api/wallets",
        "/api/wallets/poll",
        "/api/copy/preview",
        "/api/live-safety/preflight",
        "/api/markets/{market_id}/positions",
        "/api/markets/{market_id}/orders/{operation}",
        "/api/paper/quote",
        "/api/paper/quote-limit",
        "/api/paper/preview-impact",
        "/api/paper/orders",
        "/api/paper/history/use",
        "/api/paper/history/clear",
        "/api/paper/positions/use",
        "/api/paper/marks/refresh",
        "/api/paper/marks/refresh-selected",
        "/api/paper/marks/clear",
        "/api/paper/marks/clear-selected",
        "/api/mutations/reconcile",
        "/api/polymarket/users/mdd/cache/purge",
        "/api/polymarket/live-validation/reports",
        "/api/polymarket/live-validation/decisions",
        "/api/polymarket/live-validation/promotion-proposal/snapshots",
    ],
    "DELETE": [
        "/api/alerts/{alert_id}",
        "/api/wallets/{wallet_id}",
        "/api/polymarket/users/mdd/cache/{key}",
        "/api/polymarket/live-validation/reports/{key}",
        "/api/polymarket/live-validation/promotion-proposal/snapshots/{key}",
    ],
}
POLYMARKET_L2_HEADERS = ("POLY_ADDRESS", "POLY_API_KEY", "POLY_PASSPHRASE", "POLY_SIGNATURE", "POLY_TIMESTAMP")
POLYMARKET_RELAYER_HEADERS = ("RELAYER_API_KEY", "RELAYER_API_KEY_ADDRESS")
POLYMARKET_USER_WS_KEYS = ("POLY_API_KEY", "POLY_API_SECRET", "POLY_SECRET", "POLY_PASSPHRASE")
SENSITIVE_SETTING_POLICY_LABELS = (
    "api credentials",
    "passwords and passphrases",
    "private and signing keys",
    "session cookies and credential tokens",
    "request signatures",
)
LIVE_SETTING_KEYS = {
    "live_trading_enabled",
    "live_trading_confirmed",
    "live_trading_kill_switch",
    "live_trading_max_size",
    "live_trading_max_notional",
}
ALERT_SOURCE_OPTIONS = [
    {"id": "last_trade", "label": "Last trade"},
    {"id": "midpoint", "label": "Midpoint"},
    {"id": "best_bid", "label": "Best bid"},
    {"id": "best_ask", "label": "Best ask"},
]
ALERT_SOURCE_IDS = {str(option["id"]) for option in ALERT_SOURCE_OPTIONS}
ALERT_DIRECTIONS = {"above", "below"}


class HttpResponseTooLargeError(RuntimeError):
    """Raised before response headers when a representation exceeds the server limit."""


def _json_bytes(payload: Dict[str, Any], *, max_bytes: Optional[int] = None) -> bytes:
    limit = MAX_HTTP_RESPONSE_BYTES if max_bytes is None else max(1, int(max_bytes))
    buffer = io.BytesIO()
    encoder = json.JSONEncoder(indent=2, sort_keys=True)
    for text_chunk in encoder.iterencode(payload):
        chunk = text_chunk.encode("utf-8")
        if buffer.tell() + len(chunk) > limit:
            raise HttpResponseTooLargeError(f"HTTP response exceeds {limit} bytes.")
        buffer.write(chunk)
    return buffer.getvalue()


def _utf8_bytes(text: str, *, max_bytes: Optional[int] = None) -> bytes:
    limit = MAX_HTTP_RESPONSE_BYTES if max_bytes is None else max(1, int(max_bytes))
    value = str(text)
    buffer = io.BytesIO()
    for offset in range(0, len(value), HTTP_RESPONSE_CHUNK_BYTES):
        chunk = value[offset : offset + HTTP_RESPONSE_CHUNK_BYTES].encode("utf-8")
        if buffer.tell() + len(chunk) > limit:
            raise HttpResponseTooLargeError(f"HTTP response exceeds {limit} bytes.")
        buffer.write(chunk)
    return buffer.getvalue()


def _read_json_body(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    transfer_encoding = str(handler.headers.get("Transfer-Encoding") or "").strip()
    if transfer_encoding:
        raise ValueError("Transfer-Encoding is not supported for JSON request bodies; send Content-Length.")
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError as exc:
        raise ValueError("Content-Length must be an integer.") from exc
    if length < 0:
        raise ValueError("Content-Length cannot be negative.")
    if length > MAX_JSON_BODY_BYTES:
        raise ValueError("JSON request body is too large.")
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    if len(raw) != length:
        raise ValueError("JSON request body is incomplete.")
    if not raw:
        return {}
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("JSON request body must be UTF-8.") from exc
    data = loads_strict_json(text)
    if not isinstance(data, dict):
        raise ValueError("JSON request body must be an object.")
    return data


def _discard_available_request_body(handler: BaseHTTPRequestHandler) -> None:
    """Best-effort drain of a small already-sent body before an early close.

    Closing a Windows socket with unread request bytes can reset the connection
    and hide the structured overload response. Never wait long enough for a
    slow sender to occupy reserved read capacity.
    """
    try:
        length = int(handler.headers.get("Content-Length") or "0")
    except (TypeError, ValueError):
        return
    if length <= 0 or length > MAX_JSON_BODY_BYTES:
        return
    connection = handler.connection
    previous_timeout = connection.gettimeout()
    remaining = length
    deadline = time.monotonic() + 0.05
    try:
        connection.settimeout(0.05)
        while remaining > 0 and time.monotonic() < deadline:
            chunk = handler.rfile.read1(min(remaining, HTTP_RESPONSE_CHUNK_BYTES))
            if not chunk:
                break
            remaining -= len(chunk)
    except (OSError, ValueError):
        pass
    finally:
        try:
            connection.settimeout(previous_timeout)
        except OSError:
            pass


def _validated_idempotency_key(headers: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
    """Resolve one visible-ASCII idempotency key without exposing it in responses."""
    header_key = str(headers.get("Idempotency-Key") or "")
    raw_body_key = payload.get("idempotency_key")
    if raw_body_key is not None and not isinstance(raw_body_key, str):
        raise ValueError("idempotency_key must be a string.")
    body_key = str(raw_body_key or "")
    for candidate in (header_key, body_key):
        if candidate and (
            len(candidate) > LIVE_VALIDATION_IDEMPOTENCY_KEY_MAX_LENGTH
            or any(ord(char) < 33 or ord(char) > 126 for char in candidate)
        ):
            raise ValueError(
                "Idempotency keys must contain 1-"
                f"{LIVE_VALIDATION_IDEMPOTENCY_KEY_MAX_LENGTH} visible ASCII characters."
            )
    if header_key and body_key and not hmac.compare_digest(header_key, body_key):
        raise ValueError("Idempotency-Key header and idempotency_key body field must match.")
    key = header_key or body_key
    if not key:
        return ""
    return key


def _is_live_order_management_route(method: str, path: str) -> bool:
    parts = str(path or "").strip("/").split("/")
    return (
        str(method or "").strip().upper() == "POST"
        and len(parts) == 5
        and parts[:2] == ["api", "markets"]
        and parts[3] == "orders"
        and bool(parts[2])
        and bool(parts[4])
    )


def _is_general_durable_mutation(method: str, path: str) -> bool:
    return (
        str(method or "").strip().upper() == "POST"
        and (path in LOCAL_DURABLE_CREATE_ROUTES or _is_live_order_management_route(method, path))
    )


def _mutation_key_hash(idempotency_key: str) -> str:
    return hashlib.sha256(str(idempotency_key).encode("utf-8")).hexdigest()


def _mutation_request_hash(payload: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            dict(payload),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("Mutation request must contain canonical JSON values.") from exc
    return hashlib.sha256(encoded).hexdigest()


def _mutation_journal_entry(
    cfg: AppConfig,
    idempotency_key: str,
) -> Optional[MutationJournalEntry]:
    key_hash = _mutation_key_hash(idempotency_key)
    return next((entry for entry in cfg.mutation_journal if entry.key_hash == key_hash), None)


def _new_mutation_journal_entry(
    idempotency_key: str,
    method: str,
    path: str,
    payload: Mapping[str, Any],
    *,
    live: bool,
) -> MutationJournalEntry:
    return MutationJournalEntry(
        key_hash=_mutation_key_hash(idempotency_key),
        method=str(method or "").strip().upper(),
        path=str(path or "").strip(),
        request_hash=_mutation_request_hash(payload),
        live=bool(live),
    )


def _assert_mutation_request_matches(
    entry: MutationJournalEntry,
    method: str,
    path: str,
    payload: Mapping[str, Any],
) -> None:
    request_hash = _mutation_request_hash(payload)
    if not (
        hmac.compare_digest(entry.method, str(method or "").strip().upper())
        and hmac.compare_digest(entry.path, str(path or "").strip())
        and hmac.compare_digest(entry.request_hash, request_hash)
    ):
        raise IdempotencyConflictError(
            "The Idempotency-Key was already used for a different route or request body."
        )


def _stored_mutation_result(
    payload: Mapping[str, Any],
    *,
    preserve_shape: bool = False,
    max_bytes: Optional[int] = None,
) -> Dict[str, Any]:
    sanitized = sanitize_audit_value(dict(payload))
    stored = bounded_mutation_result(
        sanitized,
        preserve_shape=preserve_shape,
        max_bytes=max_bytes if max_bytes is not None else MAX_MUTATION_RESULT_BYTES,
    )
    try:
        assert_no_persisted_secrets(stored)
    except ValueError:
        encoded = json.dumps(
            stored,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return {
            "ok": True,
            "mutation_result": {
                "stored": "receipt_only",
                "reason": "sensitive_fields_were_redacted",
                "sha256": hashlib.sha256(encoded).hexdigest(),
            },
        }
    return stored


def _client_mutation_result(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Keep the original mutation response shape, trimming only huge bodies."""

    candidate = dict(payload)
    try:
        _json_bytes(candidate)
    except (HttpResponseTooLargeError, TypeError, ValueError):
        return bounded_mutation_result(
            sanitize_audit_value(candidate),
            preserve_shape=True,
            max_bytes=MAX_HTTP_RESPONSE_BYTES,
        )
    return candidate


def mutation_journal_payload(cfg: AppConfig) -> Dict[str, Any]:
    entries = sorted(
        cfg.mutation_journal,
        key=lambda item: (item.updated_at, item.created_at, item.id),
        reverse=True,
    )
    return {
        "entries": [
            {
                "id": entry.id,
                "method": entry.method,
                "path": entry.path,
                "live": entry.live,
                "state": entry.state,
                "response_status": entry.response_status or None,
                "outcome_code": entry.outcome_code,
                "outcome_message": entry.outcome_message,
                "created_at": entry.created_at,
                "updated_at": entry.updated_at,
                "replay_authorized_at": entry.replay_authorized_at or None,
            }
            for entry in entries
        ],
        "counts": {
            "total": len(entries),
            "unresolved": sum(1 for entry in entries if entry.state in {"pending", "ambiguous"}),
        },
    }


class IdempotencyConflictError(RuntimeError):
    """Raised when one client key is reused for a different mutation."""


def api_error_payload(
    status: int,
    code: str,
    message: str,
    details: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    error: Dict[str, Any] = {
        "code": code,
        "message": message,
        "status": int(status),
    }
    if details:
        error["details"] = sanitize_audit_value(dict(details))
    return {"ok": False, "error": error}


def _paper_record_signed_size(record: PaperTradeRecord) -> float:
    size = float(record.filled_size or record.size or 0.0)
    side = str(record.side or "").upper()
    if side in {"SELL", "LAY"}:
        return -size
    if side in {"BUY", "BACK"}:
        return size
    return 0.0


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    number = _safe_float(value, None)
    if number is None:
        return default
    return int(number)


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(_safe_int(value, default), maximum))


UNLIMITED_LIMIT_TOKENS = {"0", "-1", "all", "any", "none", "unlimited", "max"}


def _parse_optional_limit(value: Any, default: int, *, minimum: int = 1) -> Optional[int]:
    text = str(value if value is not None else "").strip().lower()
    if text in UNLIMITED_LIMIT_TOKENS:
        return None
    return max(minimum, _safe_int(value, default))


def _limit_label(value: Optional[int]) -> str:
    return "unlimited" if value is None else str(value)


def _limit_slice(rows: List[Dict[str, Any]], limit: Optional[int]) -> List[Dict[str, Any]]:
    if limit is None:
        return list(rows)
    return rows[:limit]


def _query_value(params: Mapping[str, List[str]], key: str, default: str = "") -> str:
    values = params.get(key)
    if not values:
        return default
    return str(values[0] if values[0] is not None else default).strip()


def _query_float(params: Mapping[str, List[str]], key: str) -> Optional[float]:
    if key not in params:
        return None
    return _safe_float(_query_value(params, key), None)


def _query_bool(params: Mapping[str, List[str]], key: str, default: bool = False) -> bool:
    if key not in params:
        return default
    value = _query_value(params, key).lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def activity_key(item: Mapping[str, Any]) -> str:
    tx = str(item.get("transactionHash") or item.get("transaction_hash") or "").strip().lower()
    if tx:
        return f"tx:{tx}"
    activity_id = str(item.get("activityId") or item.get("activity_id") or "").strip().lower()
    if activity_id:
        return f"activity-id:{activity_id}"
    fields = ("timestamp", "proxyWallet", "asset", "side", "price", "size", "slug", "outcome")
    return "activity:" + "|".join(str(item.get(key) or "").strip().lower() for key in fields)


def _alert_market_id(alert: PriceAlert) -> str:
    return str(getattr(alert, "market_id", "polymarket") or "polymarket").strip().lower()


def _alert_price_state_key(market_id: str, contract_id: str) -> Tuple[str, str]:
    return str(market_id or "polymarket").strip().lower(), str(contract_id or "").strip()


def price_snapshot_values(snapshot: PriceSnapshot) -> Dict[str, Optional[float]]:
    midpoint = snapshot.midpoint
    if midpoint is None and snapshot.bid is not None and snapshot.ask is not None:
        midpoint = (float(snapshot.bid) + float(snapshot.ask)) / 2.0
    return {
        "last_trade": _safe_float(snapshot.last, None),
        "midpoint": _safe_float(midpoint, None),
        "best_bid": _safe_float(snapshot.bid, None),
        "best_ask": _safe_float(snapshot.ask, None),
    }


def _alert_values(
    alert: PriceAlert,
    price_state: Optional[Mapping[Tuple[str, str], Mapping[str, Any]]] = None,
) -> Dict[str, Optional[float]]:
    key = _alert_price_state_key(_alert_market_id(alert), alert.token_id)
    raw_values = dict((price_state or {}).get(key, {}))
    return {source: _safe_float(raw_values.get(source), None) for source in ALERT_SOURCE_IDS}


def _alert_current_value(
    alert: PriceAlert,
    price_state: Optional[Mapping[Tuple[str, str], Mapping[str, Any]]] = None,
) -> Optional[float]:
    values = _alert_values(alert, price_state)
    value = values.get(str(alert.source))
    if value is not None:
        return value
    return _safe_float(alert.last_value, None)


def _alert_condition(alert: PriceAlert, value: float) -> bool:
    return value >= float(alert.threshold) if alert.direction == "above" else value <= float(alert.threshold)


def _paper_order_signed_size(order: PaperOrderRequest) -> float:
    side = str(order.side or "").upper()
    size = float(order.size or 0.0)
    if side in {"SELL", "LAY"}:
        return -size
    if side in {"BUY", "BACK"}:
        return size
    return 0.0


def _paper_order_effect(current_net: float, signed_size: float, projected_net: float) -> str:
    if signed_size == 0:
        return "unchanged"
    if current_net == 0:
        return "opens position"
    if projected_net == 0:
        return "closes position"
    if (current_net > 0 > projected_net) or (current_net < 0 < projected_net):
        return "flips position"
    if (current_net > 0 and signed_size > 0) or (current_net < 0 and signed_size < 0):
        return "adds to position"
    return "reduces position"


def _paper_position_mark_price(snapshot: PriceSnapshot, net_size: float) -> Tuple[float, str]:
    midpoint = snapshot.midpoint
    if midpoint is None and snapshot.bid is not None and snapshot.ask is not None:
        midpoint = (float(snapshot.bid) + float(snapshot.ask)) / 2.0
    values = {
        "best_bid": snapshot.bid,
        "best_ask": snapshot.ask,
        "midpoint": midpoint,
        "last_trade": snapshot.last,
    }
    ordered_sources = (
        (("best_bid", "bid"), ("midpoint", "midpoint"), ("last_trade", "last"), ("best_ask", "ask"))
        if net_size >= 0
        else (("best_ask", "ask"), ("midpoint", "midpoint"), ("last_trade", "last"), ("best_bid", "bid"))
    )
    for key, label in ordered_sources:
        value = values.get(key)
        if value is not None:
            return float(value), label
    raise ValueError("No mark price is available for this position.")


def _paper_position_unrealized(row: Dict[str, Any], mark: Mapping[str, Any]) -> Optional[float]:
    mark_price = _safe_float(mark.get("mark_price"), None)
    notional = row.get("notional")
    if mark_price is None or notional is None:
        return None
    return float(row["net_size"]) * mark_price - float(notional)


def paper_position_rows(records: List[PaperTradeRecord]) -> List[Dict[str, Any]]:
    grouped: Dict[tuple[str, str], Dict[str, Any]] = {}
    for record in records:
        if not record.accepted:
            continue
        signed_size = _paper_record_signed_size(record)
        if signed_size == 0:
            continue
        price = record.average_price if record.average_price is not None else record.limit_price
        key = (record.market_id, record.contract_id)
        row = grouped.setdefault(
            key,
            {
                "market_id": record.market_id,
                "contract_id": record.contract_id,
                "net_size": 0.0,
                "notional": 0.0,
                "priced_size": 0.0,
                "trades": 0,
            },
        )
        row["net_size"] += signed_size
        row["trades"] += 1
        if price is not None:
            row["notional"] += signed_size * float(price)
            row["priced_size"] += abs(signed_size)

    rows: List[Dict[str, Any]] = []
    for row in grouped.values():
        priced_size = float(row.pop("priced_size"))
        notional = float(row["notional"])
        net_size = float(row["net_size"])
        row["average_price"] = abs(notional) / abs(net_size) if priced_size > 0 and net_size != 0 else None
        row["notional"] = notional if priced_size > 0 else None
        rows.append(row)
    return sorted(rows, key=lambda item: (str(item["market_id"]), str(item["contract_id"])))


def _paper_marks_for_rows(
    marks: Mapping[Tuple[str, str], Dict[str, Any]],
    rows: List[Dict[str, Any]],
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    active = {(str(row["market_id"]), str(row["contract_id"])) for row in rows}
    return {key: value for key, value in marks.items() if isinstance(key, tuple) and len(key) == 2 and key in active}


def paper_position_summary(
    rows: List[Dict[str, Any]],
    marks: Optional[Mapping[Tuple[str, str], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    marks = marks or {}
    priced_rows = [row for row in rows if row.get("notional") is not None]
    unrealized_values = []
    mark_sources: Dict[str, int] = {}
    last_marked_at: Optional[float] = None
    marked_count = 0
    for row in rows:
        mark = marks.get((str(row["market_id"]), str(row["contract_id"])), {})
        if _safe_float(mark.get("mark_price"), None) is not None:
            marked_count += 1
        unrealized = _paper_position_unrealized(row, mark)
        if unrealized is not None:
            unrealized_values.append(float(unrealized))
        source = str(mark.get("source") or "")
        if source:
            mark_sources[source] = mark_sources.get(source, 0) + 1
        marked_at = _safe_float(mark.get("marked_at"), None)
        if marked_at is not None:
            last_marked_at = marked_at if last_marked_at is None else max(last_marked_at, marked_at)
    return {
        "positions": len(rows),
        "gross_size": sum(abs(float(row["net_size"])) for row in rows),
        "entry_notional": sum(abs(float(row["notional"])) for row in priced_rows),
        "net_notional": sum(float(row["notional"]) for row in priced_rows),
        "marked": marked_count,
        "unrealized": sum(unrealized_values) if unrealized_values else None,
        "mark_sources": mark_sources,
        "last_marked_at": last_marked_at,
    }


def bool_from_setting(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def optional_positive_float(raw: Any, label: str) -> Optional[float]:
    if raw in (None, ""):
        return None
    value = raw
    if isinstance(raw, str):
        value = raw.strip()
        if value == "":
            return None
    if type(value) not in (int, float, str):
        raise ValueError(f"{label} must be blank or a positive finite number.")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be blank or a positive number.") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label} must be blank or a positive number.")
    return float(number)


def sanitize_settings(settings: Mapping[str, Any]) -> Dict[str, Any]:
    sanitized: Dict[str, Any] = {}
    for key, value in settings.items():
        if is_sensitive_display_key(key):
            sanitized[str(key)] = "***" if value not in (None, "") else ""
        else:
            sanitized[str(key)] = sanitize_audit_value(value, str(key))
    return sanitized


def sanitize_audit_value(value: Any, key: str = "") -> Any:
    if is_sensitive_display_key(key):
        return "***" if value not in (None, "") else ""
    if isinstance(value, Mapping):
        return {str(child_key): sanitize_audit_value(child_value, str(child_key)) for child_key, child_value in value.items()}
    if isinstance(value, list):
        return [sanitize_audit_value(item, key) for item in value]
    if isinstance(value, tuple):
        return [sanitize_audit_value(item, key) for item in value]
    return value


def enabled_capabilities(capabilities: MarketCapabilities) -> List[str]:
    return [key for key, value in capabilities.to_dict().items() if value]


def market_live_status(capabilities: MarketCapabilities, settings: Mapping[str, Any]) -> str:
    if not capabilities.live_trading:
        return "no"
    if not bool_from_setting(settings.get("live_trading_enabled"), False):
        return "guarded/off"
    if bool_from_setting(settings.get("live_trading_kill_switch"), False) or bool_from_setting(
        settings.get("live_trading_paused"), False
    ):
        return "kill-switch"
    if not (
        bool_from_setting(settings.get("live_trading_confirmed"), False)
        or bool_from_setting(settings.get("live_trading_acknowledged"), False)
    ):
        return "needs-confirmation"
    return "armed"


def market_status_text(meta: MarketMetadata, enabled: bool, health: Dict[str, Any], settings: Mapping[str, Any]) -> str:
    message = str(health.get("message") or "").strip()
    if health.get("verified_blocker"):
        return f"{meta.display_name}: verified blocked. {message}"

    capabilities = meta.capabilities
    read_supported = (
        capabilities.market_discovery
        or capabilities.event_listing
        or capabilities.price_reading
        or capabilities.orderbook_reading
    )
    return (
        f"{meta.display_name}: adapter loaded. "
        f"Config {'enabled' if enabled else 'disabled'}; "
        f"Alerts {'yes' if capabilities.alerts else 'no'}; "
        f"read-only {'yes' if read_supported else 'no'}; "
        f"paper {'yes' if capabilities.paper_trading else 'no'}; "
        f"live {market_live_status(capabilities, settings)}; "
        f"copy {'yes' if capabilities.copy_trading else 'no'}."
    )


def market_safety_payload(settings: Mapping[str, Any], enabled: bool) -> Dict[str, Any]:
    return {
        "enabled": bool(enabled),
        "live_trading_enabled": bool_from_setting(settings.get("live_trading_enabled"), False),
        "live_trading_confirmed": bool_from_setting(settings.get("live_trading_confirmed"), False)
        or bool_from_setting(settings.get("live_trading_acknowledged"), False),
        "live_trading_kill_switch": bool_from_setting(settings.get("live_trading_kill_switch"), False)
        or bool_from_setting(settings.get("live_trading_paused"), False),
        "live_trading_max_size": settings.get("live_trading_max_size") if settings.get("live_trading_max_size") not in (None, "") else None,
        "live_trading_max_notional": settings.get("live_trading_max_notional")
        if settings.get("live_trading_max_notional") not in (None, "")
        else None,
    }


def market_health_payload(meta: MarketMetadata, cfg: AppConfig, registry: Optional[AdapterRegistry] = None) -> Dict[str, Any]:
    market_cfg = cfg.markets.get(meta.market_id)
    settings = dict(market_cfg.settings) if market_cfg else {}
    registry = registry or build_default_registry()
    adapter = None
    adapter_error = ""
    try:
        adapter = registry.create(meta.market_id, settings)
        health = adapter.health_check()
    except Exception as exc:
        adapter_error = f"Adapter health check failed: {type(exc).__name__}."
        health = {
            "market_id": meta.market_id,
            "ok": False,
            "message": "Adapter health check failed.",
            "error_type": type(exc).__name__,
            "adapter": "",
            "capabilities": meta.capabilities.to_dict(),
        }
    blocker = None
    if health.get("verified_blocker"):
        blocker = {
            "reason": health.get("message") or "Verified upstream blocker.",
            "references": health.get("references") or [],
            "last_reviewed": health.get("last_reviewed") or "",
        }
    support = support_matrix_entry(meta, adapter, blocker=blocker, adapter_error=adapter_error)
    credential_sources = health.get("credential_sources") if isinstance(health.get("credential_sources"), list) else []
    credential_env_vars = [str(value) for value in settings.get("credential_env_vars") or []]
    return {
        "market_id": meta.market_id,
        "display_name": meta.display_name,
        "enabled": bool(market_cfg and market_cfg.enabled),
        "default_enabled": bool(meta.default_enabled),
        "homepage_url": meta.homepage_url,
        "description": meta.description,
        "capabilities": meta.capabilities.to_dict(),
        "enabled_capabilities": enabled_capabilities(meta.capabilities),
        "settings": sanitize_settings(settings),
        "safety": market_safety_payload(settings, bool(market_cfg and market_cfg.enabled)),
        "health": health,
        "support": support,
        "status_text": market_status_text(meta, bool(market_cfg and market_cfg.enabled), health, settings),
        "credential_env_vars": credential_env_vars,
        "credential_sources": credential_sources,
        "credential_summary": ", ".join(
            f"{str(item.get('name') or 'credential')} from {str(item.get('source') or 'configured')}"
            for item in credential_sources
            if isinstance(item, dict)
        )
        or "none detected",
    }


def markets_payload(cfg: AppConfig, registry: Optional[AdapterRegistry] = None) -> Dict[str, Any]:
    registry = registry or build_default_registry()
    markets = [market_health_payload(meta, cfg, registry) for meta in MARKET_CATALOG]
    support_matrix = [market["support"] for market in markets]
    support_summary = support_matrix_summary(support_matrix)
    return {
        "selected_market_id": cfg.selected_market_id,
        "markets": markets,
        "support_matrix": support_matrix,
        "support_summary": support_summary,
        "counts": {
            "total": len(markets),
            "enabled": sum(1 for market in markets if market["enabled"]),
            "implemented": sum(1 for market in markets if any(market["capabilities"].values())),
            "verified_blocked": sum(1 for item in support_matrix if item["implementation_status"] == "verified_blocked"),
            "guarded_markets": sum(
                1
                for item in support_matrix
                if any(operation["status"] == "guarded" for operation in item["operations"].values())
            ),
        },
    }


def market_support_payload(
    cfg: AppConfig,
    registry: Optional[AdapterRegistry] = None,
    market_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the canonical support matrix, optionally narrowed to one market."""

    payload = markets_payload(cfg, registry)
    normalized = str(market_id or "").strip().lower()
    if not normalized:
        return {
            "selected_market_id": payload["selected_market_id"],
            "markets": payload["support_matrix"],
            "support_summary": payload["support_summary"],
            "counts": payload["counts"],
        }
    rows = [row for row in payload["support_matrix"] if row["market_id"] == normalized]
    if not rows:
        raise ValueError(f"Unknown market id: {normalized}")
    return {
        "selected_market_id": normalized,
        "market": rows[0],
        "markets": rows,
        "support_summary": payload["support_summary"],
        "counts": payload["counts"],
    }


def live_safety_payload(
    cfg: AppConfig,
    registry: Optional[AdapterRegistry] = None,
    market_id: Optional[str] = None,
) -> Dict[str, Any]:
    registry = registry or build_default_registry()
    normalized = str(market_id or cfg.selected_market_id or "").strip().lower()
    meta = next((item for item in MARKET_CATALOG if item.market_id == normalized), None)
    if meta is None:
        raise ValueError(f"Unknown market id: {normalized}")

    market_cfg = cfg.markets.get(normalized)
    settings = dict(market_cfg.settings) if market_cfg else {}
    enabled = bool(market_cfg and market_cfg.enabled)
    safety = market_safety_payload(settings, enabled)
    status = market_live_status(meta.capabilities, settings)
    blockers: List[str] = []
    if not enabled:
        blockers.append("market disabled")
    if not meta.capabilities.live_trading:
        blockers.append("live trading unsupported")
    if meta.capabilities.live_trading and not safety["live_trading_enabled"]:
        blockers.append("live trading disabled")
    if safety["live_trading_kill_switch"]:
        blockers.append("kill switch active")
    if meta.capabilities.live_trading and safety["live_trading_enabled"] and not safety["live_trading_confirmed"]:
        blockers.append("acknowledgement required")

    if enabled and status == "armed":
        tone = "good"
    elif blockers:
        tone = "warn"
    else:
        tone = "neutral"

    return {
        "selected_market_id": normalized,
        "market": market_health_payload(meta, cfg, registry),
        "status": status,
        "tone": tone,
        "blockers": blockers,
        "can_preflight": bool(enabled and meta.capabilities.live_trading),
        "controls": safety,
        "redaction": {
            "sensitive_key_policy": list(SENSITIVE_SETTING_POLICY_LABELS),
            "audit_payloads_redacted": True,
            "persistent_credentials_allowed": False,
        },
    }


def format_live_preflight(preflight: Mapping[str, Any]) -> str:
    preview = str(preflight.get("dry_run_preview") or "Live order preflight passed.")
    exposure = preflight.get("approx_notional")
    max_notional = preflight.get("max_notional")
    warnings = preflight.get("warnings") if isinstance(preflight.get("warnings"), list) else []

    parts = [f"Preflight OK: {preview}"]
    if isinstance(exposure, (int, float)):
        parts.append(f"max_exposure~{float(exposure):g}")
    if isinstance(max_notional, (int, float)):
        parts.append(f"max_notional={float(max_notional):g}")
    if warnings:
        parts.append("warnings=" + ",".join(str(item) for item in warnings))
    return "; ".join(parts)


def live_order_audit_payload(order: PaperOrderRequest) -> Dict[str, Any]:
    return {
        "market_id": order.market_id,
        "contract_id": order.contract_id,
        "side": order.side,
        "size": order.size,
        "limit_price": order.limit_price,
        # This initial audit record is produced before an adapter is selected.
        # Use the shared fail-closed upper bound; successful preflight replaces
        # it with the venue-specific exposure model in ``preflight``.
        "approx_notional": order.size * max(1.0, float(order.limit_price or 0.0)),
        "exposure_model": "full_size_upper_bound",
        "metadata_keys": sorted(str(key) for key in order.metadata.keys()),
    }


def live_preflight_payload(cfg: AppConfig, registry: AdapterRegistry, payload: Mapping[str, Any]) -> Dict[str, Any]:
    order = paper_order_from_payload(payload)
    response: Dict[str, Any] = {
        "ok": False,
        "blocked": True,
        "order": live_order_audit_payload(order),
        "preflight": None,
        "message": "",
        "live_safety": live_safety_payload(cfg, registry, order.market_id),
    }
    try:
        require_market_enabled(cfg, order.market_id, "live preflight preview")
        adapter = adapter_for_market(cfg, order.market_id, registry)
        if not adapter.capabilities.live_trading:
            raise UnsupportedFeatureError(
                adapter.market_id,
                "live_trading",
                f"{adapter.display_name} does not support live trading in this app.",
            )
        preflight = sanitize_audit_value(adapter.preflight_live_order(order, feature_name="live preflight preview"))
    except Exception as exc:
        response["message"] = f"Live preflight blocked: {exc}"
        response["error"] = str(exc)
        response["live_safety"] = live_safety_payload(cfg, registry, order.market_id)
        return response

    response.update(
        {
            "ok": True,
            "blocked": False,
            "preflight": preflight,
            "message": format_live_preflight(preflight),
            "live_safety": live_safety_payload(cfg, registry, order.market_id),
        }
    )
    return response


def paper_payload(
    cfg: AppConfig,
    marks: Optional[Mapping[Tuple[str, str], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    positions = paper_position_rows(cfg.paper_trades)
    active_marks = _paper_marks_for_rows(marks or {}, positions)
    marked_positions = []
    for row in positions:
        key = (str(row["market_id"]), str(row["contract_id"]))
        mark = active_marks.get(key, {})
        unrealized = _paper_position_unrealized(row, mark)
        marked_positions.append(
            {
                **row,
                "mark_price": _safe_float(mark.get("mark_price"), None),
                "mark_source": str(mark.get("source") or ""),
                "marked_at": _safe_float(mark.get("marked_at"), None),
                "unrealized": unrealized,
            }
        )
    history = [record.to_dict() for record in cfg.paper_trades]
    return {
        "summary": paper_position_summary(positions, active_marks),
        "positions": marked_positions,
        "history": history,
        "counts": {
            "history": len(history),
            "accepted": sum(1 for record in cfg.paper_trades if record.accepted),
            "rejected": sum(1 for record in cfg.paper_trades if not record.accepted),
        },
    }


def config_payload(cfg: AppConfig) -> Dict[str, Any]:
    return {
        "selected_market_id": cfg.selected_market_id,
        "theme": cfg.theme,
        "ui_design": getattr(cfg, "ui_design", "aurora_2026"),
        "alerts": [alert.to_dict() for alert in cfg.alerts],
        "wallets": [wallet.to_dict() for wallet in cfg.wallets],
        "copytrading": cfg.copytrading.to_dict(),
    }


def _directory_write_probe(path: Path) -> tuple[bool, str]:
    """Prove create-and-unlink access without modifying a durable state file."""
    parent = path.parent
    if not parent.exists():
        return False, "parent_missing"
    if not parent.is_dir():
        return False, "parent_not_directory"
    descriptor = -1
    probe_name = ""
    try:
        descriptor, probe_name = tempfile.mkstemp(prefix=".market-sentinel-readiness-", dir=parent)
        os.close(descriptor)
        descriptor = -1
        Path(probe_name).unlink()
        probe_name = ""
        return True, "create_unlink_succeeded"
    except OSError as exc:
        return False, f"create_unlink_failed:{type(exc).__name__}"
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if probe_name:
            try:
                Path(probe_name).unlink()
            except OSError:
                pass


def _json_store_readiness(
    name: str,
    path: Path,
    *,
    config_store: bool = False,
    collection_key: str = "",
    expected_version: Optional[int] = None,
) -> Dict[str, Any]:
    """Inspect one local JSON store without changing its durable contents."""
    target = path.expanduser()
    result: Dict[str, Any] = {
        "name": name,
        "path": str(target),
        "exists": False,
        "readable": False,
        "writable": False,
        "schema_valid": False,
        "ready": False,
        "size_bytes": 0,
        "last_modified_at": None,
        "age_seconds": None,
    }
    writable, write_probe = _directory_write_probe(target)
    result["writable"] = writable
    result["write_probe"] = write_probe
    try:
        if target.is_symlink():
            result["read_status"] = "target_symlink_rejected"
            return result
        if not target.exists():
            # All stores have safe in-memory empty defaults. A missing file is
            # ready only when its parent has proven atomic-publication access.
            result["readable"] = True
            result["schema_valid"] = True
            result["read_status"] = "uninitialized_default_available"
            result["ready"] = writable
            return result
        if not target.is_file():
            result["read_status"] = "target_not_regular_file"
            return result
        stat_result = target.stat()
        result["exists"] = True
        result["size_bytes"] = int(stat_result.st_size)
        result["last_modified_at"] = float(stat_result.st_mtime)
        result["age_seconds"] = max(0.0, time.time() - float(stat_result.st_mtime))
        if stat_result.st_size > MAX_READINESS_JSON_STORE_BYTES:
            result["read_status"] = "store_exceeds_readiness_size_limit"
            return result
        if config_store:
            load_config(target)
        else:
            with target.open("rb") as handle:
                raw = handle.read(MAX_READINESS_JSON_STORE_BYTES + 1)
            if len(raw) > MAX_READINESS_JSON_STORE_BYTES:
                result["read_status"] = "store_exceeds_readiness_size_limit"
                return result
            parsed = json.loads(raw.decode("utf-8"))
            if not isinstance(parsed, Mapping):
                result["read_status"] = "json_root_not_object"
                return result
            if expected_version is not None:
                version = parsed.get("version")
                if version is None:
                    result["read_status"] = "schema_version_missing"
                    return result
                if isinstance(version, bool) or not isinstance(version, int) or version != expected_version:
                    result["read_status"] = "schema_version_mismatch"
                    return result
            if collection_key:
                collection = parsed.get(collection_key)
                if collection is None:
                    result["read_status"] = f"collection_missing:{collection_key}"
                    return result
                if not isinstance(collection, Mapping):
                    result["read_status"] = f"collection_not_object:{collection_key}"
                    return result
        result["readable"] = True
        result["schema_valid"] = True
        result["read_status"] = "validated"
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, RuntimeError) as exc:
        result["read_status"] = f"validation_failed:{type(exc).__name__}"
    result["ready"] = bool(result["readable"] and result["writable"])
    return result


def _backup_freshness_signal() -> Dict[str, Any]:
    """Report bounded backup metadata; integrity remains an offline verifier job."""
    configured = str(os.environ.get("MARKET_SENTINEL_BACKUP_DIRECTORY") or "").strip()
    if not configured:
        return {
            "configured": False,
            "status": "unknown",
            "authoritative": False,
            "message": "Set MARKET_SENTINEL_BACKUP_DIRECTORY to expose bounded backup-age metadata.",
        }
    directory = Path(configured).expanduser()
    signal: Dict[str, Any] = {
        "configured": True,
        "directory": str(directory),
        "status": "missing",
        "authoritative": False,
        "integrity_verified": False,
        "manifest_pairs_seen": 0,
        "scan_truncated": False,
        "latest_manifest_modified_at": None,
        "latest_manifest_age_seconds": None,
        "message": "Metadata only; use verify_production_deployment.py for cryptographic backup verification.",
    }
    try:
        if directory.is_symlink() or not directory.is_dir():
            return signal
        newest: Optional[float] = None
        seen = 0
        with os.scandir(directory) as entries:
            for entry in entries:
                seen += 1
                if seen > 1_000:
                    signal["scan_truncated"] = True
                    break
                if not entry.name.endswith(".manifest.json") or entry.is_symlink():
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                archive_name = entry.name[: -len(".manifest.json")]
                archive = directory / archive_name
                if archive.is_symlink() or not archive.is_file():
                    continue
                signal["manifest_pairs_seen"] += 1
                modified_at = float(entry.stat(follow_symlinks=False).st_mtime)
                newest = modified_at if newest is None else max(newest, modified_at)
        if newest is not None:
            age = max(0.0, time.time() - newest)
            signal["latest_manifest_modified_at"] = newest
            signal["latest_manifest_age_seconds"] = age
            signal["status"] = "recent_metadata" if age <= 36 * 60 * 60 else "stale_metadata"
    except OSError as exc:
        signal["status"] = f"inspection_failed:{type(exc).__name__}"
    return signal


def local_resource_readiness(config_path: Path, frontend_dir: Path) -> Dict[str, Any]:
    """Probe local production dependencies without making any network calls."""
    stores = [
        _json_store_readiness("configuration", config_path, config_store=True),
        _json_store_readiness(
            "analytics_cache",
            analytics_cache_path(),
            collection_key="entries",
            expected_version=ANALYTICS_CACHE_VERSION,
        ),
        _json_store_readiness(
            "live_validation_reports",
            live_validation_reports_path(),
            collection_key="reports",
            expected_version=LIVE_VALIDATION_REPORTS_VERSION,
        ),
        _json_store_readiness(
            "live_validation_decisions",
            live_validation_decisions_path(),
            collection_key="decisions",
            expected_version=LIVE_VALIDATION_DECISIONS_VERSION,
        ),
        _json_store_readiness(
            "live_validation_promotion_snapshots",
            live_validation_promotion_proposal_snapshots_path(),
            collection_key="snapshots",
            expected_version=LIVE_VALIDATION_PROMOTION_PROPOSAL_SNAPSHOTS_VERSION,
        ),
    ]
    frontend_index = frontend_dir / "index.html"
    frontend_ready = False
    frontend_status = "missing"
    try:
        frontend_ready = bool(not frontend_index.is_symlink() and frontend_index.is_file())
        frontend_status = "available" if frontend_ready else "missing"
    except OSError as exc:
        frontend_status = f"inspection_failed:{type(exc).__name__}"
    return {
        "storage": {
            "ready": all(bool(store["ready"]) for store in stores),
            "stores": stores,
        },
        "frontend": {
            "ready": frontend_ready,
            "status": frontend_status,
            "index_path": str(frontend_index),
        },
        "backup": _backup_freshness_signal(),
        "network_checks_performed": False,
    }


def runtime_readiness_payload(
    resources: Mapping[str, Any],
    admission: Mapping[str, Any],
    wallet_polling: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Combine cached local probes with live admission and freshness state."""
    max_workers = max(1, int(admission.get("max_http_workers") or MAX_HTTP_WORKERS))
    mutation_capacity = max(1, int(admission.get("max_mutation_workers") or 1))
    requests_in_flight = max(0, int(admission.get("requests_in_flight") or 0))
    mutations_in_flight = max(0, int(admission.get("mutations_in_flight") or 0))
    reserved = max(0, int(admission.get("reserved_read_workers") or 0))
    admission_payload = dict(admission)
    admission_payload.update(
        {
            "worker_saturated": requests_in_flight >= max_workers,
            "mutation_saturated": mutations_in_flight >= mutation_capacity,
            "ready": (
                reserved >= 1
                and not bool(admission.get("draining", False))
                and requests_in_flight < max_workers
                and mutations_in_flight < mutation_capacity
                and int(admission.get("mutations_active") or 0) <= 1
            ),
        }
    )
    polling = dict(wallet_polling or {})
    interval = max(2.0, float(polling.get("poll_interval_seconds") or 10.0))
    last_polled_at = polling.get("last_polled_at")
    last_polled = float(last_polled_at) if isinstance(last_polled_at, (int, float)) else None
    poll_age = max(0.0, time.time() - last_polled) if last_polled is not None else None
    polling_freshness = {
        "last_polled_at": last_polled,
        "age_seconds": poll_age,
        "stale_after_seconds": max(60.0, interval * 3),
        "status": (
            "not_started"
            if poll_age is None
            else "fresh"
            if poll_age <= max(60.0, interval * 3)
            else "stale"
        ),
        "required_for_service_readiness": False,
    }
    storage = resources.get("storage") if isinstance(resources.get("storage"), Mapping) else {}
    frontend = resources.get("frontend") if isinstance(resources.get("frontend"), Mapping) else {}
    ready = bool(storage.get("ready") and frontend.get("ready") and admission_payload["ready"])
    return {
        "ready": ready,
        "status": "ready" if ready else "degraded",
        "storage": dict(storage),
        "frontend": dict(frontend),
        "admission": admission_payload,
        "freshness": {
            "wallet_polling": polling_freshness,
            "backup": dict(resources.get("backup") or {}),
        },
        "network_checks_performed": False,
    }


def health_payload(
    config_path: Path = DEFAULT_CONFIG_PATH,
    frontend_dir: Path = DEFAULT_FRONTEND_DIR,
    runtime_identity: Optional[Mapping[str, str]] = None,
    max_http_workers: int = MAX_HTTP_WORKERS,
    runtime_readiness: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    frontend_index = frontend_dir / "index.html"
    identity = runtime_identity or {}
    if runtime_readiness is None:
        reserved = min(HTTP_RESERVED_READ_WORKERS, max(1, int(max_http_workers) // 4))
        mutation_capacity = max(1, min(MAX_HTTP_MUTATION_WORKERS, int(max_http_workers) - reserved))
        runtime_readiness = runtime_readiness_payload(
            local_resource_readiness(config_path, frontend_dir),
            {
                "max_http_workers": max(1, int(max_http_workers)),
                "max_mutation_workers": mutation_capacity,
                "reserved_read_workers": max(0, int(max_http_workers) - mutation_capacity),
                "requests_in_flight": 0,
                "mutations_in_flight": 0,
                "mutations_active": 0,
            },
        )
    return {
        "status": "ok",
        "ready": bool(runtime_readiness.get("ready")),
        "readiness": dict(runtime_readiness),
        "api_version": project_version(),
        "runtime_source_revision": str(identity.get("source_revision") or ""),
        "runtime_frontend_sha256": str(identity.get("frontend_sha256") or ""),
        "mode": "parallel",
        "python_gui_available": True,
        "python_gui_command": PYTHON_GUI_COMMAND,
        "python_gui_script": PYTHON_GUI_SCRIPT,
        "tkinter_fallback": f"{PYTHON_GUI_SCRIPT} or {PYTHON_GUI_COMMAND}",
        "react_gui": "parallel",
        "react_dev_command": REACT_DEV_COMMAND,
        "react_dev_manual_command": REACT_DEV_MANUAL_COMMAND,
        "react_build_command": REACT_BUILD_COMMAND,
        "react_prod_command": REACT_PROD_COMMAND,
        "config_path": str(config_path),
        "frontend_dist": str(frontend_dir),
        "frontend_build_available": frontend_index.exists(),
        "observability": {
            "metrics_endpoint": "/metrics",
            "metrics_format": "prometheus",
            "request_logging": "structured_json",
            "metrics_access": "same server authorization as the API",
            "max_http_workers": max(1, int(max_http_workers)),
            "max_mutation_workers": int(
                (runtime_readiness.get("admission") or {}).get("max_mutation_workers") or 1
            ),
            "reserved_read_workers": int(
                (runtime_readiness.get("admission") or {}).get("reserved_read_workers") or 0
            ),
            "mutation_lock_timeout_seconds": HTTP_MUTATION_LOCK_TIMEOUT_SECONDS,
            "max_http_response_bytes": MAX_HTTP_RESPONSE_BYTES,
            "connection_timeout_seconds": HTTP_CONNECTION_TIMEOUT_SECONDS,
        },
        "routes": API_ROUTES,
    }


def app_state_payload(
    cfg: AppConfig,
    config_path: Path = DEFAULT_CONFIG_PATH,
    frontend_dir: Path = DEFAULT_FRONTEND_DIR,
    paper_marks: Optional[Mapping[Tuple[str, str], Dict[str, Any]]] = None,
    registry: Optional[AdapterRegistry] = None,
    alert_price_state: Optional[Mapping[Tuple[str, str], Mapping[str, Any]]] = None,
    wallet_polling: Optional[Mapping[str, Any]] = None,
    recent_wallet_activity: Optional[List[Dict[str, Any]]] = None,
    runtime_identity: Optional[Mapping[str, str]] = None,
    max_http_workers: int = MAX_HTTP_WORKERS,
    runtime_readiness: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    registry = registry or build_default_registry()
    return {
        "health": health_payload(
            config_path,
            frontend_dir,
            runtime_identity,
            max_http_workers,
            runtime_readiness,
        ),
        "config": config_payload(cfg),
        "markets": markets_payload(cfg, registry),
        "alerts": alerts_payload(cfg, registry, alert_price_state),
        "wallets": wallets_payload(cfg, wallet_polling, recent_wallet_activity),
        "copy": copy_payload(cfg, registry),
        "live_safety": live_safety_payload(cfg, registry),
        "polymarket_live_validation": polymarket_live_validation_payload(cfg),
        "polymarket_live_validation_reports": polymarket_live_validation_reports_payload(),
        "paper": paper_payload(cfg, paper_marks),
    }


def apply_config_patch(cfg: AppConfig, payload: Dict[str, Any]) -> AppConfig:
    if "selected_market_id" in payload:
        selected_market_id = str(payload["selected_market_id"] or "").strip().lower()
        if selected_market_id not in MARKET_IDS:
            raise ValueError(f"Unknown market id: {selected_market_id}")
        cfg.selected_market_id = selected_market_id
    if "theme" in payload:
        theme = str(payload["theme"] or "").strip().lower()
        if theme not in {"light", "dark"}:
            raise ValueError("theme must be light or dark.")
        cfg.theme = "dark" if theme == "dark" else "light"
    if "ui_design" in payload:
        ui_design = str(payload["ui_design"] or "").strip().lower().replace("-", "_").replace(" ", "_")
        if ui_design not in {"classic", "aurora_2026", "graphite_2026", "sentinel_2027"}:
            raise ValueError("ui_design must be classic, aurora_2026, graphite_2026, or sentinel_2027.")
        cfg.ui_design = cast(UIDesign, ui_design)
    return cfg


def _blocked_outbound_setting_keys(value: Any) -> List[str]:
    blocked: set[str] = set()
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            if is_outbound_endpoint_setting(key):
                blocked.add(key)
            blocked.update(_blocked_outbound_setting_keys(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            blocked.update(_blocked_outbound_setting_keys(child))
    return sorted(blocked)


def apply_market_patch(
    cfg: AppConfig,
    market_id: str,
    payload: Dict[str, Any],
    *,
    allow_outbound_endpoint_changes: bool = False,
) -> AppConfig:
    normalized = str(market_id or "").strip().lower()
    if normalized not in cfg.markets:
        raise ValueError(f"Unknown market id: {normalized}")
    market_cfg = cfg.markets[normalized]
    control_fields = (*MARKET_SAFETY_BOOLEAN_FIELDS, *MARKET_SAFETY_LIMIT_FIELDS)
    top_settings = {key: payload[key] for key in control_fields if key in payload}
    top = MarketConfig.from_dict(normalized, {
        "enabled": payload.get("enabled", market_cfg.enabled), "settings": top_settings,
    })
    raw_settings = payload.get("settings", {})
    if "settings" in payload:
        if not isinstance(raw_settings, dict):
            raise ValueError("settings must be an object.")
        assert_no_persisted_secrets(raw_settings)
        blocked_outbound = _blocked_outbound_setting_keys(raw_settings)
        if blocked_outbound and not allow_outbound_endpoint_changes:
            raise ValueError(
                "Outbound endpoint and custom-host policy settings cannot be changed through the HTTP API; "
                "edit trusted local configuration and restart: "
                + ", ".join(blocked_outbound)
            )
    nested = MarketConfig.validated_settings(raw_settings)
    for key in top_settings.keys() & raw_settings.keys():
        if top.settings.get(key) != nested.get(key):
            raise ValueError(f"Top-level and nested market setting '{key}' disagree.")
    settings = dict(market_cfg.settings)
    for original, validated in ((top_settings, top.settings), (raw_settings, nested)):
        settings.update(validated)
        for key in MARKET_SAFETY_LIMIT_FIELDS:
            if key in original and key not in validated:
                settings.pop(key, None)
    updated = MarketConfig.from_dict(normalized, {"enabled": top.enabled, "settings": settings})
    market_cfg.enabled = updated.enabled
    market_cfg.settings = updated.settings
    return cfg


def require_market_enabled(cfg: AppConfig, market_id: str, feature: str) -> None:
    market_cfg = cfg.markets.get(market_id)
    if not market_cfg or not market_cfg.enabled:
        raise ValueError(f"{market_id} is disabled in local market config. Enable it before using {feature}.")


def adapter_for_market(cfg: AppConfig, market_id: str, registry: AdapterRegistry):
    market_cfg = cfg.markets.get(market_id)
    settings = market_cfg.settings if market_cfg else {}
    return registry.create(market_id, settings)


def find_alert(cfg: AppConfig, alert_id: str) -> PriceAlert:
    normalized = str(alert_id or "").strip()
    for alert in cfg.alerts:
        if alert.id == normalized:
            return alert
    raise ValueError(f"Unknown alert id: {normalized}")


def alert_status(
    cfg: AppConfig,
    registry: AdapterRegistry,
    alert: PriceAlert,
    price_state: Optional[Mapping[Tuple[str, str], Mapping[str, Any]]] = None,
) -> Dict[str, str]:
    market_id = _alert_market_id(alert)
    market_cfg = cfg.markets.get(market_id)
    if not alert.enabled:
        return {"label": "triggered/disabled" if alert.triggered else "disabled", "tone": "neutral"}
    if not market_cfg or not market_cfg.enabled:
        return {"label": "market disabled", "tone": "warn"}
    try:
        adapter = adapter_for_market(cfg, market_id, registry)
    except Exception as exc:
        return {"label": f"adapter unavailable: {exc}", "tone": "warn"}
    if not adapter.capabilities.alerts:
        return {"label": "alerts unsupported", "tone": "warn"}
    if not adapter.capabilities.price_reading:
        return {"label": "price feed unavailable", "tone": "warn"}
    if alert.triggered:
        return {"label": "triggered", "tone": "warn"}
    if _alert_current_value(alert, price_state) is None:
        return {"label": f"waiting for {alert.source}", "tone": "neutral"}
    return {"label": "armed", "tone": "good"}


def alert_payload(
    cfg: AppConfig,
    registry: AdapterRegistry,
    alert: PriceAlert,
    price_state: Optional[Mapping[Tuple[str, str], Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    market_id = _alert_market_id(alert)
    values = _alert_values(alert, price_state)
    return {
        **alert.to_dict(),
        "market_id": market_id,
        "contract_id": alert.token_id,
        "values": values,
        "current_value": _alert_current_value(alert, price_state),
        "status": alert_status(cfg, registry, alert, price_state),
    }


def alerts_payload(
    cfg: AppConfig,
    registry: Optional[AdapterRegistry] = None,
    price_state: Optional[Mapping[Tuple[str, str], Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    registry = registry or build_default_registry()
    alerts = [alert_payload(cfg, registry, alert, price_state) for alert in cfg.alerts]
    return {
        "alerts": alerts,
        "source_options": ALERT_SOURCE_OPTIONS,
        "counts": {
            "total": len(alerts),
            "enabled": sum(1 for alert in cfg.alerts if alert.enabled),
            "triggered": sum(1 for alert in cfg.alerts if alert.triggered),
        },
    }


def alert_from_payload(
    cfg: AppConfig,
    registry: AdapterRegistry,
    payload: Mapping[str, Any],
    existing: Optional[PriceAlert] = None,
) -> PriceAlert:
    market_id = str(payload.get("market_id") or (existing.market_id if existing else "") or "").strip().lower()
    token_id = str(
        payload.get("contract_id")
        or payload.get("token_id")
        or (existing.token_id if existing else "")
        or ""
    ).strip()
    label = str(payload.get("label") if "label" in payload else (existing.label if existing else "")).strip()
    direction = str(payload.get("direction") or (existing.direction if existing else "above")).strip().lower()
    source = str(payload.get("source") or (existing.source if existing else "last_trade")).strip().lower()
    threshold = _safe_float(payload.get("threshold") if "threshold" in payload else (existing.threshold if existing else None), None)

    if not market_id:
        raise ValueError("market_id is required.")
    if market_id not in MARKET_IDS:
        raise ValueError(f"Unknown market id: {market_id}")
    if not token_id:
        raise ValueError("contract_id is required.")
    if direction not in ALERT_DIRECTIONS:
        raise ValueError("direction must be above or below.")
    if source not in ALERT_SOURCE_IDS:
        raise ValueError("source must be one of: last_trade, midpoint, best_bid, best_ask.")
    if threshold is None or threshold < 0 or threshold > 1:
        raise ValueError("threshold must be a number between 0 and 1.")

    once = bool_from_setting(payload.get("once"), existing.once if existing else True)
    enabled = bool_from_setting(payload.get("enabled"), existing.enabled if existing else True)
    label = label or f"Alert {token_id[:8]}"
    watched_change_requested = existing is None or any(
        key in payload for key in ("market_id", "contract_id", "token_id", "direction", "threshold", "source")
    )
    if existing is None or enabled or watched_change_requested:
        require_market_enabled(cfg, market_id, "price alerts")
        adapter = adapter_for_market(cfg, market_id, registry)
        if not adapter.capabilities.alerts:
            raise UnsupportedFeatureError(market_id, "alerts", f"{adapter.display_name} does not support price alerts.")
        if not adapter.capabilities.price_reading:
            raise UnsupportedFeatureError(market_id, "price_reading", f"{adapter.display_name} has no price feed for alerts.")

    if existing is None:
        return PriceAlert(
            token_id=token_id,
            label=label,
            direction=direction,  # type: ignore[arg-type]
            threshold=float(threshold),
            source=source,  # type: ignore[arg-type]
            once=once,
            enabled=enabled,
            market_id=market_id,
        )

    watched_before = (existing.market_id, existing.token_id, existing.direction, existing.threshold, existing.source)
    existing.market_id = market_id
    existing.token_id = token_id
    existing.label = label
    existing.direction = direction  # type: ignore[assignment]
    existing.threshold = float(threshold)
    existing.source = source  # type: ignore[assignment]
    existing.once = once
    existing.enabled = enabled
    watched_after = (existing.market_id, existing.token_id, existing.direction, existing.threshold, existing.source)
    if watched_after != watched_before:
        existing.last_value = None
        existing.triggered = False
    return existing


def evaluate_alerts_for_contract(
    cfg: AppConfig,
    market_id: str,
    contract_id: str,
    price_state: Mapping[Tuple[str, str], Mapping[str, Any]],
) -> List[str]:
    normalized_market, normalized_contract = _alert_price_state_key(market_id, contract_id)
    values = dict(price_state.get((normalized_market, normalized_contract), {}))
    messages: List[str] = []
    for alert in cfg.alerts:
        if not alert.enabled:
            continue
        if _alert_market_id(alert) != normalized_market or alert.token_id != normalized_contract:
            continue
        value = _safe_float(values.get(str(alert.source)), None)
        if value is None:
            continue
        previous = alert.last_value
        alert.last_value = float(value)
        condition_now = _alert_condition(alert, float(value))
        condition_previous = None
        if previous is not None:
            condition_previous = _alert_condition(alert, float(previous))
        crossed = condition_now and (condition_previous is False or condition_previous is None)
        if crossed and not alert.triggered:
            alert.triggered = True
            messages.append(
                f"{normalized_market}:{alert.label} {alert.source}={float(value):g} crossed {alert.direction} {float(alert.threshold):g}"
            )
            if alert.once:
                alert.enabled = False
        if not alert.once and alert.triggered and not condition_now:
            alert.triggered = False
    return messages


def refresh_alert_price(
    cfg: AppConfig,
    registry: AdapterRegistry,
    alert: PriceAlert,
    price_state: Dict[Tuple[str, str], Dict[str, Any]],
) -> Dict[str, Any]:
    market_id = _alert_market_id(alert)
    require_market_enabled(cfg, market_id, "alert price refresh")
    adapter = adapter_for_market(cfg, market_id, registry)
    if not adapter.capabilities.alerts:
        raise UnsupportedFeatureError(market_id, "alerts", f"{adapter.display_name} does not support price alerts.")
    if not adapter.capabilities.price_reading:
        raise UnsupportedFeatureError(market_id, "price_reading", f"{adapter.display_name} has no price feed for alerts.")
    snapshot = adapter.get_price(alert.token_id)
    key = _alert_price_state_key(market_id, alert.token_id)
    price_state[key] = price_snapshot_values(snapshot)
    messages = evaluate_alerts_for_contract(cfg, market_id, alert.token_id, price_state)
    return {
        "market_id": key[0],
        "contract_id": key[1],
        "values": price_state[key],
        "messages": messages,
        "source": snapshot.source,
    }


def refresh_all_alert_prices(
    cfg: AppConfig,
    registry: AdapterRegistry,
    price_state: Dict[Tuple[str, str], Dict[str, Any]],
) -> Dict[str, Any]:
    refreshed: List[Dict[str, Any]] = []
    problems: List[str] = []
    seen: set[Tuple[str, str]] = set()
    for alert in cfg.alerts:
        if not alert.enabled:
            continue
        key = _alert_price_state_key(_alert_market_id(alert), alert.token_id)
        if key in seen:
            continue
        seen.add(key)
        try:
            refreshed.append(refresh_alert_price(cfg, registry, alert, price_state))
        except Exception as exc:
            problems.append(f"{key[0]}:{key[1]}: {exc}")
    return {"refreshed": refreshed, "problems": problems}


def delete_alert(cfg: AppConfig, alert_id: str) -> PriceAlert:
    alert = find_alert(cfg, alert_id)
    cfg.alerts = [item for item in cfg.alerts if item.id != alert.id]
    return alert


def require_polymarket_selected(cfg: AppConfig, feature: str) -> None:
    if str(cfg.selected_market_id or "").strip().lower() != "polymarket":
        raise ValueError(f"{feature} is only available when the selected market is polymarket.")
    require_market_enabled(cfg, "polymarket", feature)


def require_selected_market(cfg: AppConfig, feature: str) -> str:
    """Require an enabled selected market and return its normalized id.

    Wallet tracking and simulation-first copy workflows are market-scoped now;
    Polymarket analytics remains intentionally protected by
    ``require_polymarket_selected`` above.
    """

    market_id = str(cfg.selected_market_id or "").strip().lower()
    if not market_id:
        raise ValueError(f"{feature} requires a selected market.")
    require_market_enabled(cfg, market_id, feature)
    return market_id


def wallet_payload(wallet: WalletWatch) -> Dict[str, Any]:
    return {
        **wallet.to_dict(),
        "seen_count": len(wallet.seen_activity_keys or []),
    }


def wallet_activity_payload(wallet: WalletWatch, item: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "id": activity_key(item),
        "wallet_id": wallet.id,
        "wallet": wallet.wallet,
        "display_name": wallet.display_name,
        "timestamp": int(item.get("timestamp") or 0),
        "transaction_hash": str(item.get("transactionHash") or item.get("transaction_hash") or ""),
        "proxy_wallet": str(item.get("proxyWallet") or ""),
        "side": str(item.get("side") or ""),
        "asset": str(item.get("asset") or ""),
        "price": _safe_float(item.get("price"), None),
        "size": _safe_float(item.get("size"), None),
        "slug": str(item.get("slug") or ""),
        "outcome": str(item.get("outcome") or ""),
        "pseudonym": str(item.get("pseudonym") or item.get("name") or ""),
        "raw": dict(item),
    }


def wallets_payload(
    cfg: AppConfig,
    polling: Optional[Mapping[str, Any]] = None,
    recent_activity: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    enabled_wallets = [wallet for wallet in cfg.wallets if wallet.enabled]
    return {
        "wallets": [wallet_payload(wallet) for wallet in cfg.wallets],
        "counts": {
            "total": len(cfg.wallets),
            "enabled": len(enabled_wallets),
        },
        "polling": {
            "mode": "manual",
            "poll_interval_seconds": float((polling or {}).get("poll_interval_seconds") or 10.0),
            "last_polled_at": _safe_float((polling or {}).get("last_polled_at"), None),
            "last_message": str((polling or {}).get("last_message") or "Not polled yet."),
        },
        "recent_activity": list(recent_activity or []),
    }


LEADERBOARD_SORTS = {
    "roi_pct": "PNL",
    "pnl_usd": "PNL",
    "volume_usd": "VOL",
    "mdd_usd": "PNL",
    "mdd_pct": "PNL",
}
POLYMARKET_LEADERBOARD_PAGE_SIZE = 50


def _leaderboard_lookup(row: Mapping[str, Any], *keys: str) -> Any:
    sources: List[Mapping[str, Any]] = [row]
    for nested_key in ("user", "profile", "trader"):
        nested = row.get(nested_key)
        if isinstance(nested, Mapping):
            sources.append(nested)
    for source in sources:
        for key in keys:
            if key in source and source.get(key) not in (None, ""):
                return source.get(key)
    return None


def _leaderboard_display_name(row: Mapping[str, Any], wallet: str) -> str:
    name = _leaderboard_lookup(
        row,
        "pseudonym",
        "displayName",
        "display_name",
        "username",
        "name",
    )
    if name:
        return str(name)
    return wallet or "-"


def normalize_polymarket_leaderboard_row(raw: Mapping[str, Any], fallback_rank: int) -> Dict[str, Any]:
    wallet = str(
        _leaderboard_lookup(raw, "proxyWallet", "proxy_wallet", "wallet", "address", "userAddress") or ""
    )
    pnl = _safe_float(
        _leaderboard_lookup(raw, "pnl", "pnlUsd", "pnl_usd", "profit", "profitLoss", "realizedPnl", "realizedPnlUsd"),
        None,
    )
    volume = _safe_float(
        _leaderboard_lookup(raw, "volume", "volumeUsd", "volume_usd", "vol", "totalVolume", "totalVolumeUsd"),
        None,
    )
    roi = (float(pnl) / float(volume) * 100.0) if pnl is not None and volume and volume > 0 else None
    mdd_usd = _safe_float(
        _leaderboard_lookup(raw, "mdd", "mddUsd", "mdd_usd", "maxDrawdown", "max_drawdown", "maxDrawdownUsd"),
        None,
    )
    mdd_pct = _safe_float(
        _leaderboard_lookup(raw, "mddPct", "mdd_pct", "maxDrawdownPct", "max_drawdown_pct"),
        None,
    )
    rank = _safe_int(_leaderboard_lookup(raw, "rank", "position"), fallback_rank)
    display_public = _leaderboard_lookup(raw, "displayUsernamePublic", "display_username_public")
    return {
        "rank": rank or fallback_rank,
        "wallet": wallet,
        "display_name": _leaderboard_display_name(raw, wallet),
        "profile_image": str(_leaderboard_lookup(raw, "profileImage", "profile_image", "avatar") or ""),
        "display_username_public": bool(display_public) if display_public is not None else True,
        "pnl_usd": pnl,
        "volume_usd": volume,
        "roi_pct": roi,
        **performance_ratio_metadata(roi),
        "trade_count": _safe_int(_leaderboard_lookup(raw, "trades", "tradeCount", "trade_count", "totalTrades"), 0),
        "mdd_usd": mdd_usd,
        "mdd_pct": mdd_pct,
        "mdd_available": mdd_usd is not None or mdd_pct is not None,
        "raw": dict(raw),
    }


def _position_total_pnl(row: Mapping[str, Any]) -> Optional[float]:
    total = _safe_float(_leaderboard_lookup(row, "totalPnl", "total_pnl"), None)
    if total is not None:
        return total
    values = [
        _safe_float(_leaderboard_lookup(row, "cashPnl", "cash_pnl"), None),
        _safe_float(_leaderboard_lookup(row, "realizedPnl", "realized_pnl"), None),
    ]
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def _fetch_user_positions_all(wallet: str, limit: int = 500) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    offset = 0
    clean_limit = max(0, min(int(limit), 1000))
    while len(rows) < clean_limit:
        page_limit = min(500, clean_limit - len(rows))
        page = data_api.get_positions(wallet, limit=page_limit, offset=offset)
        if not page:
            break
        rows.extend(page)
        if len(page) < page_limit:
            break
        offset += len(page)
    return rows


def _fetch_user_closed_positions_all(wallet: str, limit: int = 500) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    offset = 0
    clean_limit = max(0, min(int(limit), 1000))
    while len(rows) < clean_limit:
        page_limit = min(50, clean_limit - len(rows))
        page = data_api.get_closed_positions(
            wallet,
            limit=page_limit,
            offset=offset,
            sort_by="TIMESTAMP",
            sort_direction="ASC",
        )
        if not page:
            break
        rows.extend(page)
        if len(page) < page_limit:
            break
        offset += len(page)
    return rows


def _max_drawdown(points: List[Dict[str, Any]], equity_base_usd: Optional[float]) -> Dict[str, Any]:
    return max_drawdown(points, equity_base_usd)


def polymarket_user_mdd_payload(
    wallet: str,
    *,
    mode: str = "fast",
    closed_limit: int = 500,
    open_limit: int = 500,
    activity_limit: int = 1000,
    trade_limit: int = 1000,
    include_open: bool = True,
    equity_base_usd: Optional[float] = None,
    max_points: int = 50,
    cache_ttl_seconds: int = 0,
    mark_replay_token_limit: int = 10,
    mark_replay_point_limit: int = 5000,
    mark_replay_interval: Optional[str] = "1h",
    mark_replay_fidelity: Optional[int] = 60,
    mark_replay_start_ts: Optional[int] = None,
    mark_replay_end_ts: Optional[int] = None,
    include_accounting_snapshot: bool = False,
    accounting_timeout: float = 30.0,
) -> Dict[str, Any]:
    clean_mode = str(mode or "fast").strip().lower()
    if clean_mode in {"mark_replay", "mark-replay", "clob_mark_replay", "price_history"}:
        return polymarket_user_mdd_payload_mark_replay(
            wallet,
            closed_limit=closed_limit,
            open_limit=open_limit,
            activity_limit=activity_limit,
            trade_limit=trade_limit,
            include_open=include_open,
            equity_base_usd=equity_base_usd,
            max_points=max_points,
            cache_ttl_seconds=cache_ttl_seconds,
            mark_replay_token_limit=mark_replay_token_limit,
            mark_replay_point_limit=mark_replay_point_limit,
            mark_replay_interval=mark_replay_interval,
            mark_replay_fidelity=mark_replay_fidelity,
            mark_replay_start_ts=mark_replay_start_ts,
            mark_replay_end_ts=mark_replay_end_ts,
            include_accounting_snapshot=include_accounting_snapshot,
            accounting_timeout=accounting_timeout,
        )
    return polymarket_user_mdd_payload_v2(
        wallet,
        closed_limit=closed_limit,
        open_limit=open_limit,
        activity_limit=activity_limit,
        trade_limit=trade_limit,
        include_open=include_open,
        equity_base_usd=equity_base_usd,
        max_points=max_points,
        cache_ttl_seconds=cache_ttl_seconds,
        include_accounting_snapshot=include_accounting_snapshot,
        accounting_timeout=accounting_timeout,
    )


def polymarket_rate_limit_status(exc: Optional[BaseException] = None) -> Dict[str, Any]:
    if exc is None:
        return {"limited": False, "backoff_status": "not_limited", "events": []}
    event: Dict[str, Any] = {
        "message": str(exc),
        "retry_after_seconds": None,
    }
    if isinstance(exc, PolymarketHTTPError):
        event.update(
            {
                "service": exc.service,
                "method": exc.method,
                "status_code": exc.status_code,
                "url": exc.url,
            }
        )
    return {"limited": True, "backoff_status": "retry_later", "events": [event]}


def polymarket_mdd_audit_params(wallet: str, options: Mapping[str, Any]) -> Dict[str, Any]:
    params = dict(options)
    params["wallet"] = normalize_wallet(wallet)
    params["artifact"] = POLYMARKET_MDD_AUDIT_KIND
    params["calculation_version"] = MDD_CALCULATION_VERSION
    return params


def attach_polymarket_mdd_audit_cache(
    payload: Dict[str, Any],
    params: Mapping[str, Any],
    *,
    enabled: bool,
) -> Dict[str, Any]:
    payload["rate_limit"] = polymarket_rate_limit_status()
    if not enabled:
        metadata = analytics_cache_summary(enabled=False)
        metadata.update({"stored": False, "key": None, "kind": POLYMARKET_MDD_AUDIT_KIND})
        payload["audit_cache"] = metadata
        return metadata

    artifact_payload = dict(payload)
    artifact_payload.pop("audit_cache", None)
    artifact_payload.pop("rate_limit", None)
    try:
        metadata = store_analytics_artifact(
            POLYMARKET_MDD_AUDIT_KIND,
            params,
            artifact_payload,
            ttl_seconds=DEFAULT_ANALYTICS_CACHE_TTL_SECONDS,
            max_entries=DEFAULT_ANALYTICS_CACHE_MAX_ENTRIES,
        )
    except Exception as exc:
        metadata = analytics_cache_summary(enabled=True)
        metadata.update({"stored": False, "key": None, "kind": POLYMARKET_MDD_AUDIT_KIND, "error": str(exc)})
        warnings = payload.setdefault("warnings", [])
        if isinstance(warnings, list):
            warnings.append(f"MDD audit cache write failed: {exc}")
    payload["audit_cache"] = metadata
    return metadata


def polymarket_mdd_export_payload(cache_key: str) -> Dict[str, Any]:
    loaded = load_analytics_artifact(cache_key, kind=POLYMARKET_MDD_AUDIT_KIND, allow_expired=True)
    if loaded is None:
        raise ValueError("Unknown MDD audit cache key.")
    payload, metadata = loaded
    metadata["calculation_current"] = payload.get("calculation_version") == MDD_CALCULATION_VERSION
    payload["calculation_current"] = metadata["calculation_current"]
    return {"cache": metadata, "payload": payload, "export": {"format": "json", "source": POLYMARKET_MDD_AUDIT_KIND}}


def polymarket_mdd_export_csv(cache_key: str) -> Dict[str, Any]:
    export = polymarket_mdd_export_payload(cache_key)
    payload = export["payload"]
    wallet = str(payload.get("wallet") or "wallet").replace("/", "_").replace("\\", "_")
    key = str(export["cache"].get("key") or "audit")
    return {
        "cache": export["cache"],
        "filename": f"polymarket-mdd-{wallet}-{key[:8]}.csv",
        "csv": mdd_payload_to_csv(payload),
    }


def polymarket_mdd_cache_payload(*, include_expired: bool = True) -> Dict[str, Any]:
    return list_analytics_artifacts(
        kind=POLYMARKET_MDD_AUDIT_KIND,
        include_expired=include_expired,
        include_payload=False,
    )


def polymarket_mdd_cache_health_payload() -> Dict[str, Any]:
    return {
        "source": POLYMARKET_MDD_AUDIT_KIND,
        "cache": analytics_cache_health(kind=POLYMARKET_MDD_AUDIT_KIND),
    }


def polymarket_mdd_cache_purge_payload(payload: Mapping[str, Any]) -> Dict[str, Any]:
    raw_keys = payload.get("keys")
    keys: List[str] = []
    if isinstance(raw_keys, list):
        keys.extend(str(key or "").strip() for key in raw_keys if str(key or "").strip())
    single_key = str(payload.get("key") or "").strip()
    if single_key:
        keys.append(single_key)
    result = purge_analytics_artifacts(
        keys=keys,
        kind=POLYMARKET_MDD_AUDIT_KIND,
        expired_only=bool(payload.get("expired_only")),
        all_entries=bool(payload.get("all") or payload.get("all_entries")),
    )
    result["source"] = POLYMARKET_MDD_AUDIT_KIND
    return result


def polymarket_user_search_payload(query: str, limit: int = 10) -> Dict[str, Any]:
    clean_query = str(query or "").strip()
    clean_limit = _clamp_int(limit, 10, 1, 50)
    if not clean_query:
        return {"query": clean_query, "profiles": [], "counts": {"profiles": 0}, "source": "gamma_public_search"}
    profiles = gamma.search_profiles(clean_query, limit=clean_limit)
    return {
        "query": clean_query,
        "profiles": [
            {
                "pseudonym": profile.pseudonym,
                "proxy_wallet": profile.proxy_wallet,
                "profile_image": profile.profile_image,
                "display_username_public": profile.display_username_public,
            }
            for profile in profiles
        ],
        "counts": {"profiles": len(profiles)},
        "source": "gamma_public_search",
    }


def polymarket_clob_readiness_payload(cfg: AppConfig) -> Dict[str, Any]:
    market_cfg = cfg.markets.get("polymarket")
    settings = dict(market_cfg.settings) if market_cfg else {}
    enabled = bool(market_cfg and market_cfg.enabled)
    return {
        "market_id": "polymarket",
        "selected": str(cfg.selected_market_id or "").strip().lower() == "polymarket",
        "enabled": enabled,
        "live_safety": market_safety_payload(settings, enabled),
        "readiness": build_clob_auth_readiness(settings),
    }


def _validation_item(status: str, detail: str, **extra: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"status": status, "detail": detail}
    payload.update(extra)
    return payload


def _env_presence(names: Tuple[str, ...]) -> Dict[str, bool]:
    return {name: bool(os.getenv(name)) for name in names}


def _all_env_present(names: Tuple[str, ...]) -> bool:
    return all(os.getenv(name) for name in names)


def polymarket_live_validation_payload(cfg: AppConfig) -> Dict[str, Any]:
    market_cfg = cfg.markets.get("polymarket")
    settings = dict(market_cfg.settings) if market_cfg else {}
    readiness = build_clob_auth_readiness(settings)
    direct_l2_ready = bool(readiness.get("direct_l2_read_ready"))
    sdk_ready = bool(readiness.get("sdk_trading_ready"))
    relayer_ready = _all_env_present(POLYMARKET_RELAYER_HEADERS)
    user_ws_auth = {
        "apiKey": os.getenv("POLY_API_KEY", ""),
        "secret": os.getenv("POLY_API_SECRET") or os.getenv("POLY_SECRET", ""),
        "passphrase": os.getenv("POLY_PASSPHRASE", ""),
    }
    try:
        build_user_subscription(user_ws_auth)
        user_ws_payload = _validation_item(
            "skipped",
            "User WebSocket auth payload can be built; GUI/API does not open the authenticated stream.",
            next_step="Run scripts/verify_polymarket_live.py --include-user-websocket-connect for a real stream probe.",
        )
    except ValueError as exc:
        user_ws_payload = _validation_item("blocked", str(exc))

    report: Dict[str, Any] = {
        "generated_at": time.time(),
        "market_id": "polymarket",
        "mode": "local_readiness_only",
        "selected": str(cfg.selected_market_id or "").strip().lower() == "polymarket",
        "enabled": bool(market_cfg and market_cfg.enabled),
        "credential_presence": {
            "clob_l2_headers": _env_presence(POLYMARKET_L2_HEADERS),
            "py_clob_client": _env_presence(
                (
                    "POLYMARKET_PRIVATE_KEY",
                    "PRIVATE_KEY",
                    "POLYMARKET_FUNDER_ADDRESS",
                    "FUNDER_ADDRESS",
                    "POLYMARKET_SIGNATURE_TYPE",
                    "SIGNATURE_TYPE",
                )
            ),
            "relayer_headers": _env_presence(POLYMARKET_RELAYER_HEADERS),
            "user_ws": _env_presence(POLYMARKET_USER_WS_KEYS),
        },
        "clob_auth_readiness": readiness,
        "credential_runbook": build_polymarket_credential_runbook(settings),
        "public_checks": {
            "clob_time": _validation_item("skipped", "GUI/API report does not run public network probes."),
            "gamma_markets": _validation_item("skipped", "GUI/API report does not run public network probes."),
            "data_leaderboard": _validation_item("skipped", "GUI/API report does not run public network probes."),
            "bridge_supported_assets": _validation_item("skipped", "GUI/API report does not run public network probes."),
        },
        "authenticated_read_checks": {
            "clob_l2_orders": _validation_item(
                "skipped" if direct_l2_ready else "blocked",
                "Explicit L2 headers are present; run the CLI for a non-destructive order-list read."
                if direct_l2_ready
                else "Missing explicit L2 headers for CLOB order-list reads.",
                missing=readiness.get("l2_headers", {}).get("missing", []),
            ),
            "py_clob_client_credentials": _validation_item(
                "skipped" if sdk_ready else "blocked",
                "SDK trading credentials are locally ready; GUI/API does not derive API credentials."
                if sdk_ready
                else "SDK trading credentials are not locally ready.",
                blockers=readiness.get("blockers", []),
            ),
            "relayer_recent_transactions": _validation_item(
                "skipped" if relayer_ready else "blocked",
                "Relayer headers are present; run the CLI for a non-destructive recent-transactions read."
                if relayer_ready
                else "Missing relayer API key headers.",
                missing=[name for name, present in _env_presence(POLYMARKET_RELAYER_HEADERS).items() if not present],
            ),
            "user_websocket_auth_payload": user_ws_payload,
            "user_websocket_connect": _validation_item(
                "skipped",
                "GUI/API does not open authenticated WebSocket sessions.",
                next_step="Run scripts/verify_polymarket_live.py --include-user-websocket-connect.",
            ),
        },
        "bridge_address_checks": {
            "deposit_address_creation": _validation_item(
                "blocked",
                "Not exposed in GUI/API. Use the CLI with explicit bridge address flags if this check is required.",
            ),
            "withdrawal_address_creation": _validation_item(
                "blocked",
                "Not exposed in GUI/API. Use the CLI with explicit withdrawal arguments if this check is required.",
            ),
        },
        "funded_live_order_check": _validation_item(
            "blocked",
            "Funded order/cancel verification is not exposed in the GUI/API.",
            live_action=False,
        ),
        "live_order_cancel_harness": {
            "default_mode": "dry_run_transcript",
            "execute_flag": "--allow-funded-order",
            "confirmation_required": CONFIRM_LIVE_ORDER_CANCEL,
            "hard_max_size": ABSOLUTE_MAX_VERIFY_SIZE,
            "hard_max_notional": ABSOLUTE_MAX_VERIFY_NOTIONAL,
        },
        "operator_commands": {
            "public_and_readiness": "python scripts/verify_polymarket_live.py --report-file live-report.json",
            "credentialed_read": "python scripts/verify_polymarket_live.py --require-authenticated-read-ok --include-user-websocket-connect --report-file live-auth-report.json",
            "dry_run_order_cancel": "python scripts/verify_polymarket_live.py --token-id <TOKEN> --side BUY --price <PRICE> --size <SIZE> --allow-token-id <TOKEN> --report-file live-dry-run-report.json",
        },
        "funded_execution_exposed": False,
        "notes": [
            "This endpoint is a local readiness view only.",
            "Credentialed reads, user WebSocket connections, and funded order/cancel checks remain CLI-only.",
            "No funded action may run without credentials, safe parameters, and explicit user approval.",
        ],
    }
    report["stage_gates"] = build_live_validation_stage_gates(report)
    return sanitize_audit_value(report)


def polymarket_live_validation_reports_payload(*, include_payload: bool = False) -> Dict[str, Any]:
    return list_live_validation_reports(include_payload=include_payload)


def polymarket_live_validation_report_payload(key: str) -> Optional[Dict[str, Any]]:
    entry = load_live_validation_report(key)
    if entry is None:
        return None
    return {
        "source": "polymarket_live_validation_report",
        "entry": entry,
        "decisions": list_live_validation_report_decisions(report_key=key).get("entries", []),
        "export": {
            "format": "json",
            "filename": polymarket_live_validation_report_export_filename(key),
        },
    }


def polymarket_live_validation_report_export_filename(key: str) -> str:
    clean_key = "".join(char for char in str(key or "") if char.isalnum() or char in {"-", "_"})[:80]
    return f"polymarket-live-validation-{clean_key or 'report'}.json"


def polymarket_live_validation_report_review_payload(key: str) -> Optional[Dict[str, Any]]:
    bundle = live_validation_report_review_bundle(key)
    if bundle is None:
        return None
    return {
        "source": "polymarket_live_validation_report_review_bundle",
        "bundle": bundle,
        "export": {
            "json_filename": live_validation_report_review_export_filename(key, "json"),
            "markdown_filename": live_validation_report_review_export_filename(key, "md"),
        },
    }


def polymarket_live_validation_decisions_payload(params: Optional[Mapping[str, List[str]]] = None) -> Dict[str, Any]:
    report_key = _query_value(params or {}, "report_key", "")
    return list_live_validation_report_decisions(report_key=report_key or "")


def polymarket_live_validation_promotion_proposal_payload(
    params: Optional[Mapping[str, List[str]]] = None,
) -> Dict[str, Any]:
    target_tier = _query_value(params or {}, "target_tier", "")
    return live_validation_coverage_promotion_proposal(target_tier=target_tier or "")


def polymarket_live_validation_promotion_proposal_snapshots_payload() -> Dict[str, Any]:
    return list_live_validation_coverage_promotion_proposal_snapshots()


def polymarket_live_validation_promotion_proposal_snapshot_payload(key: str) -> Optional[Dict[str, Any]]:
    return load_live_validation_coverage_promotion_proposal_snapshot(key)


def polymarket_live_validation_promotion_proposal_snapshot_diff_payload(key: str) -> Optional[Dict[str, Any]]:
    snapshot = polymarket_live_validation_promotion_proposal_snapshot_payload(key)
    diff = snapshot.get("diff") if isinstance(snapshot, Mapping) else None
    return dict(diff) if isinstance(diff, Mapping) else None


def polymarket_live_validation_promotion_proposal_snapshot_store_payload(
    payload: Mapping[str, Any],
    *,
    idempotency_key: str = "",
) -> Dict[str, Any]:
    target_tier = str(payload.get("target_tier") or "").strip()
    label = str(payload.get("label") or "").strip()
    source = str(payload.get("source") or "react_preview").strip() or "react_preview"
    idempotency_request = {
        "target_tier": target_tier,
        "label": label,
        "source": source,
    }
    replayed = (
        reconcile_live_validation_promotion_proposal_snapshot_idempotency(
            idempotency_key=idempotency_key,
            idempotency_request=idempotency_request,
        )
        if idempotency_key
        else None
    )
    if replayed is not None:
        inventory = polymarket_live_validation_promotion_proposal_snapshots_payload()
        inventory.update(
            {
                "stored": replayed,
                "message": f"Replayed promotion proposal snapshot {replayed.get('key')}.",
            }
        )
        return inventory
    proposal = live_validation_coverage_promotion_proposal(target_tier=target_tier)
    stored = store_live_validation_coverage_promotion_proposal_snapshot(
        proposal=proposal,
        target_tier=target_tier,
        label=label,
        source=source,
        idempotency_key=idempotency_key,
        idempotency_request=idempotency_request,
    )
    inventory = polymarket_live_validation_promotion_proposal_snapshots_payload()
    inventory.update(
        {
            "stored": stored,
            "message": (
                f"Replayed promotion proposal snapshot {stored.get('key')}."
                if stored.get("idempotent_replay")
                else f"Stored promotion proposal snapshot {stored.get('key')}."
            ),
        }
    )
    return inventory


def polymarket_live_validation_promotion_proposal_snapshot_purge_payload(payload: Mapping[str, Any]) -> Dict[str, Any]:
    keys: List[str] = []
    if payload.get("key"):
        keys.append(str(payload.get("key")))
    raw_keys = payload.get("keys")
    if isinstance(raw_keys, list):
        keys.extend(str(key) for key in raw_keys)
    return purge_live_validation_coverage_promotion_proposal_snapshots(
        keys=keys,
        all_entries=bool(payload.get("all") or payload.get("all_entries")),
    )


def polymarket_live_validation_decision_store_payload(
    payload: Mapping[str, Any],
    *,
    idempotency_key: str = "",
) -> Dict[str, Any]:
    normalized_request = {
        "report_key": str(payload.get("report_key") or "").strip(),
        "payload_hash": str(payload.get("payload_hash") or "").strip(),
        "target_tier": str(payload.get("target_tier") or "").strip(),
        "decision": str(payload.get("decision") or "").strip().lower(),
        "reviewer_note": str(payload.get("reviewer_note") or "").strip(),
        "review_bundle_hash": str(payload.get("review_bundle_hash") or "").strip(),
        "reviewer": str(payload.get("reviewer") or "").strip() or "operator",
    }
    stored = record_live_validation_report_decision(
        report_key=normalized_request["report_key"],
        payload_hash=normalized_request["payload_hash"],
        target_tier=normalized_request["target_tier"],
        decision=normalized_request["decision"],
        reviewer_note=normalized_request["reviewer_note"],
        review_bundle_hash=normalized_request["review_bundle_hash"],
        reviewer=normalized_request["reviewer"],
        idempotency_key=idempotency_key,
        idempotency_request=normalized_request,
    )
    ledger = polymarket_live_validation_decisions_payload()
    ledger.update(
        {
            "stored": stored,
            "message": (
                f"Replayed {stored.get('decision')} decision for {stored.get('target_tier')} "
                f"on report {stored.get('report_key')}."
                if stored.get("idempotent_replay")
                else f"Recorded {stored.get('decision')} decision for {stored.get('target_tier')} "
                f"on report {stored.get('report_key')}."
            ),
        }
    )
    return ledger


def polymarket_live_validation_report_store_payload(
    cfg: AppConfig,
    payload: Mapping[str, Any],
    *,
    idempotency_key: str = "",
) -> Dict[str, Any]:
    label = str(payload.get("label") or "").strip()
    source = str(payload.get("source") or "").strip()
    source_file = payload.get("source_file") if str(payload.get("source_file") or "").strip() else None
    allow_duplicate = bool(payload.get("allow_duplicate"))
    skip_duplicate = bool(payload.get("skip_duplicate", True)) and not allow_duplicate
    report: Optional[Mapping[str, Any]]
    request_mode: str

    if "report_json" in payload and str(payload.get("report_json") or "").strip():
        report = parse_live_validation_report_json(str(payload.get("report_json") or ""))
        source = source or "cli_import"
        label = label or "CLI import"
        request_mode = "report_json"
    elif isinstance(payload.get("report"), Mapping):
        report = payload["report"]  # type: ignore[assignment]
        source = source or "cli_import"
        label = label or "Imported report"
        request_mode = "report"
    else:
        report = None
        source = source or "gui_snapshot"
        label = label or "GUI readiness snapshot"
        request_mode = "generated"

    idempotency_request: Dict[str, Any] = {
        "mode": request_mode,
        "source": source,
        "label": label,
        "source_file": str(Path(str(source_file))) if source_file is not None else "",
        "duplicate_policy": "allow" if allow_duplicate or not skip_duplicate else "skip",
    }
    if request_mode != "generated":
        assert report is not None
        idempotency_request["payload_hash"] = live_validation_report_payload_hash(report)

    replayed = (
        reconcile_live_validation_report_idempotency(
            idempotency_key=idempotency_key,
            idempotency_request=idempotency_request,
        )
        if idempotency_key
        else None
    )
    if replayed is not None:
        inventory = polymarket_live_validation_reports_payload()
        inventory.update(
            {
                "stored": replayed,
                "message": f"Replayed live validation report {replayed.get('key')}.",
            }
        )
        return inventory
    if report is None:
        report = polymarket_live_validation_payload(cfg)

    stored = store_live_validation_report(
        report,
        source=source,
        label=label,
        source_file=source_file,
        allow_duplicate=allow_duplicate,
        skip_duplicate=skip_duplicate,
        idempotency_key=idempotency_key,
        idempotency_request=idempotency_request,
    )
    inventory = polymarket_live_validation_reports_payload()
    if stored.get("idempotent_replay"):
        message = f"Replayed live validation report {stored.get('key')}."
    elif stored.get("duplicate") and not stored.get("stored"):
        message = f"Skipped duplicate live validation report {stored.get('duplicate_key') or stored.get('key')}."
    elif stored.get("duplicate"):
        message = f"Stored duplicate live validation report {stored.get('key')}."
    else:
        message = f"Stored live validation report {stored.get('key')}."
    inventory.update(
        {
            "stored": stored,
            "message": message,
        }
    )
    return inventory


def polymarket_live_validation_report_purge_payload(payload: Mapping[str, Any]) -> Dict[str, Any]:
    keys: List[str] = []
    if payload.get("key"):
        keys.append(str(payload.get("key")))
    raw_keys = payload.get("keys")
    if isinstance(raw_keys, list):
        keys.extend(str(key) for key in raw_keys)
    return purge_live_validation_reports(keys=keys, all_entries=bool(payload.get("all") or payload.get("all_entries")))


def _number_in_range(value: Optional[float], minimum: Optional[float], maximum: Optional[float]) -> bool:
    if value is None:
        return minimum is None and maximum is None
    if minimum is not None and value < minimum:
        return False
    if maximum is not None and value > maximum:
        return False
    return True


def _sort_polymarket_leaderboard_rows(rows: List[Dict[str, Any]], sort: str, direction: str) -> None:
    reverse = direction != "ASC"
    missing_numeric = float("-inf") if reverse else float("inf")
    if sort == "roi_pct":
        rows.sort(key=lambda row: row["roi_pct"] if row["roi_pct"] is not None else missing_numeric, reverse=reverse)
    elif sort == "volume_usd":
        rows.sort(key=lambda row: row["volume_usd"] if row["volume_usd"] is not None else missing_numeric, reverse=reverse)
    elif sort == "mdd_usd":
        rows.sort(key=lambda row: row["mdd_usd"] if row["mdd_usd"] is not None else missing_numeric, reverse=reverse)
    elif sort == "mdd_pct":
        rows.sort(key=lambda row: row["mdd_pct"] if row["mdd_pct"] is not None else missing_numeric, reverse=reverse)
    else:
        rows.sort(key=lambda row: row["pnl_usd"] if row["pnl_usd"] is not None else missing_numeric, reverse=reverse)


def _fetch_polymarket_leaderboard_scan_rows(
    *,
    scan_limit: Optional[int],
    scan_start_offset: int = 0,
    initial_rows: Optional[List[Dict[str, Any]]] = None,
    initial_scanned: Optional[int] = None,
    retain_rows: bool = True,
    remote_sort: str,
    direction: str,
    period: str,
    category: str,
    scan_concurrency: int,
    scan_retry_attempts: int = 1,
    scan_retry_delay_seconds: float = 0.0,
    is_cancelled: Callable[[], bool],
    emit_progress: Callable[..., None],
    warnings: List[str],
    page_callback: Optional[Callable[[int, int, List[Dict[str, Any]]], Optional[bool]]] = None,
    scan_summary: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], bool]:
    raw_rows: List[Dict[str, Any]] = [dict(row) for row in (initial_rows or [])]
    scanned_count = max(len(raw_rows), int(initial_scanned or 0))
    if not retain_rows:
        raw_rows = []
    offset = max(0, int(scan_start_offset or 0))
    if scanned_count and offset <= 0:
        offset = scanned_count
    cancelled = False
    concurrency = max(1, int(scan_concurrency or 1))
    retry_attempts = max(1, int(scan_retry_attempts or 1))
    retry_delay = max(0.0, float(scan_retry_delay_seconds or 0.0))
    seen_page_fingerprints: set[str] = set()
    seen_wallet_fingerprints: set[str] = set()
    completion_reason = "scan_limit_reached" if scan_limit is not None else "upstream_exhausted"

    def fetch_page(page_offset: int, page_limit: int) -> List[Dict[str, Any]]:
        with cancellation_scope(is_cancelled):
            return fetch_page_with_cancellation(page_offset, page_limit)

    def fetch_page_with_cancellation(page_offset: int, page_limit: int) -> List[Dict[str, Any]]:
        for attempt in range(1, retry_attempts + 1):
            try:
                return data_api.get_leaderboard(
                    limit=page_limit,
                    offset=page_offset,
                    sort_by=remote_sort,
                    sort_direction=direction,
                    period=period,
                    category=category,
                )
            except RequestCancelled:
                raise
            except Exception as exc:
                if attempt >= retry_attempts:
                    raise
                warning = (
                    f"Leaderboard page offset {page_offset} failed attempt {attempt}/{retry_attempts}: "
                    f"{exc}; retrying in {retry_delay:g}s."
                )
                warnings.append(warning)
                emit_progress(
                    "leaderboard",
                    scanned=scanned_count,
                    message=warning,
                )
                if retry_delay:
                    with request_scope(retry_delay + 1) as control:
                        control.sleep(retry_delay)
        return []

    emit_progress(
        "leaderboard",
        scanned=scanned_count,
        message=f"Scanning leaderboard rows {scanned_count}/{_limit_label(scan_limit)} from offset {offset}.",
    )
    while scan_limit is None or scanned_count < scan_limit:
        if offset > LEADERBOARD_MAX_OFFSET:
            completion_reason = "upstream_offset_limit"
            warning = f"Leaderboard scan reached the documented upstream offset limit ({LEADERBOARD_MAX_OFFSET}); not all Polymarket accounts can be enumerated."
            warnings.append(warning)
            emit_progress("leaderboard", scanned=scanned_count, message=warning)
            break
        if is_cancelled():
            cancelled = True
            completion_reason = "cancelled"
            warnings.append("Leaderboard scan cancelled by user.")
            break

        remaining = None if scan_limit is None else scan_limit - scanned_count
        batch_specs: List[Tuple[int, int]] = []
        page_count = concurrency if remaining is None else min(concurrency, max(1, (remaining + POLYMARKET_LEADERBOARD_PAGE_SIZE - 1) // POLYMARKET_LEADERBOARD_PAGE_SIZE))
        for _ in range(page_count):
            if offset > LEADERBOARD_MAX_OFFSET:
                break
            if remaining is not None and remaining <= 0:
                break
            page_limit = POLYMARKET_LEADERBOARD_PAGE_SIZE if remaining is None else min(POLYMARKET_LEADERBOARD_PAGE_SIZE, remaining)
            batch_specs.append((offset, page_limit))
            offset += page_limit
            if remaining is not None:
                remaining -= page_limit
        if not batch_specs:
            break

        pages_by_offset: Dict[int, List[Dict[str, Any]]] = {}
        if len(batch_specs) == 1:
            page_offset, page_limit = batch_specs[0]
            try:
                pages_by_offset[page_offset] = fetch_page(page_offset, page_limit)
            except RequestCancelled:
                cancelled = True
        else:
            with ThreadPoolExecutor(max_workers=len(batch_specs)) as executor:
                futures = {
                    executor.submit(fetch_page, page_offset, page_limit): (page_offset, page_limit)
                    for page_offset, page_limit in batch_specs
                }
                for future in as_completed(futures):
                    page_offset, _page_limit = futures[future]
                    try:
                        pages_by_offset[page_offset] = future.result()
                    except RequestCancelled:
                        cancelled = True
                        continue
                    completed = scanned_count + sum(len(page) for page in pages_by_offset.values())
                    progress_scanned = completed if scan_limit is None else min(completed, scan_limit)
                    emit_progress(
                        "leaderboard",
                        scanned=progress_scanned,
                        message=f"Scanning leaderboard rows {progress_scanned}/{_limit_label(scan_limit)}.",
                    )

        stop_after_batch = False
        for page_offset, page_limit in batch_specs:
            if page_offset not in pages_by_offset:
                break  # Never checkpoint beyond a cancelled pagination gap.
            page = pages_by_offset.get(page_offset) or []
            if page_callback is not None:
                try:
                    accepted = page_callback(page_offset, page_limit, page)
                except Exception as exc:
                    warnings.append(f"Leaderboard checkpoint callback failed at offset {page_offset}: {exc}")
                    raise
                if accepted is False:
                    completion_reason = "repeated_page"
                    warning = f"Leaderboard scan stopped at offset {page_offset}: upstream returned a page already stored at an earlier offset."
                    warnings.append(warning)
                    emit_progress("leaderboard", scanned=scanned_count, message=warning)
                    stop_after_batch = True
                    break
            if not page:
                completion_reason = "end_of_results"
                stop_after_batch = True
                break
            page_fingerprint = hashlib.sha256(
                json.dumps(page, default=str, separators=(",", ":"), sort_keys=True).encode("utf-8")
            ).hexdigest()
            wallet_fingerprint = wallet_membership_fingerprint(page)
            if page_fingerprint in seen_page_fingerprints or (wallet_fingerprint and wallet_fingerprint in seen_wallet_fingerprints):
                completion_reason = "repeated_page"
                warning = f"Leaderboard scan stopped at offset {page_offset}: upstream repeated a previously returned page or wallet membership."
                warnings.append(warning)
                emit_progress("leaderboard", scanned=scanned_count, message=warning)
                stop_after_batch = True
                break
            seen_page_fingerprints.add(page_fingerprint)
            if wallet_fingerprint:
                seen_wallet_fingerprints.add(wallet_fingerprint)
            scanned_count += len(page)
            if retain_rows:
                raw_rows.extend(page)
            if len(page) < page_limit:
                completion_reason = "end_of_results"
                stop_after_batch = True
                break

        progress_scanned = scanned_count if scan_limit is None else min(scanned_count, scan_limit)
        emit_progress(
            "leaderboard",
            scanned=progress_scanned,
            message=f"Scanning leaderboard rows {progress_scanned}/{_limit_label(scan_limit)}.",
        )
        if cancelled or is_cancelled():
            cancelled = True
            completion_reason = "cancelled"
            warnings.append("Leaderboard scan cancelled by user.")
            break
        if stop_after_batch:
            break

    if scan_summary is not None:
        scan_summary.update(
            {
                "completion_reason": completion_reason,
                "source_enumeration_complete": completion_reason == "end_of_results",
                "source_max_offset": LEADERBOARD_MAX_OFFSET,
            }
        )
    return _limit_slice(raw_rows, scan_limit), cancelled


def polymarket_leaderboard_payload(
    params: Optional[Mapping[str, List[str]]] = None,
    *,
    cancel_check: Optional[Callable[[], bool]] = None,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    initial_raw_rows: Optional[List[Mapping[str, Any]]] = None,
    leaderboard_page_callback: Optional[Callable[[int, int, List[Dict[str, Any]]], Optional[bool]]] = None,
) -> Dict[str, Any]:
    query = params or {}
    sort = _query_value(query, "sort", "roi_pct").lower()
    if sort not in LEADERBOARD_SORTS:
        sort = "roi_pct"
    direction = _query_value(query, "direction", "DESC").upper()
    if direction not in {"ASC", "DESC"}:
        direction = "DESC"
    period = _query_value(query, "period", "all") or "all"
    category = normalize_leaderboard_category(_query_value(query, "category", "OVERALL"))
    limit = _parse_optional_limit(_query_value(query, "limit", "100"), 100)
    default_scan = 500 if sort == "roi_pct" else max(100, limit or 100)
    scan_limit = _parse_optional_limit(_query_value(query, "scan_limit", str(default_scan)), default_scan)
    if limit is not None and scan_limit is not None:
        scan_limit = max(scan_limit, limit)
    mdd_history_limit = _clamp_int(_query_value(query, "mdd_history_limit", "500"), 500, 1, 1000)
    mdd_activity_limit = _clamp_int(_query_value(query, "mdd_activity_limit", "1000"), 1000, 0, 5000)
    mdd_trade_limit = _clamp_int(_query_value(query, "mdd_trade_limit", "1000"), 1000, 0, 5000)
    mdd_open_limit = _clamp_int(_query_value(query, "mdd_open_limit", "500"), 500, 0, 1000)
    default_mdd_scan = 100 if scan_limit is None else min(scan_limit, 100)
    mdd_scan_limit = _parse_optional_limit(_query_value(query, "mdd_scan_limit", str(default_mdd_scan)), default_mdd_scan)
    if scan_limit is not None and mdd_scan_limit is not None:
        mdd_scan_limit = min(mdd_scan_limit, scan_limit)
    raw_mdd_mode = _query_value(query, "mdd_mode", "fast").lower()
    mdd_mode = "mark_replay" if raw_mdd_mode in {"mark_replay", "mark-replay", "clob_mark_replay", "price_history"} else "fast"
    mdd_mark_replay_token_limit = _clamp_int(_query_value(query, "mdd_mark_replay_token_limit", "10"), 10, 1, 20)
    mdd_mark_replay_point_limit = _clamp_int(_query_value(query, "mdd_mark_replay_point_limit", "5000"), 5000, 1, 10000)
    mdd_mark_replay_fidelity = _clamp_int(_query_value(query, "mdd_mark_replay_fidelity", "60"), 60, 1, 1440)
    mdd_mark_replay_interval = _query_value(query, "mdd_mark_replay_interval", "1h") or "1h"
    if mdd_mark_replay_interval not in {"max", "all", "1m", "1w", "1d", "6h", "1h"}:
        mdd_mark_replay_interval = "1h"
    mdd_mark_replay_start_ts = _query_float(query, "mdd_mark_replay_start_ts")
    mdd_mark_replay_end_ts = _query_float(query, "mdd_mark_replay_end_ts")
    mdd_include_accounting = _query_bool(query, "mdd_include_accounting", False)
    mdd_accounting_timeout = _clamp_int(_query_value(query, "mdd_accounting_timeout", "30"), 30, 1, 60)
    mdd_persist_cache = _query_bool(query, "mdd_persist_cache", False)
    mdd_cache_ttl_seconds = _clamp_int(
        _query_value(query, "mdd_cache_ttl_seconds", str(POLYMARKET_MDD_CACHE_TTL_SECONDS)),
        POLYMARKET_MDD_CACHE_TTL_SECONDS,
        0,
        300,
    )
    compute_mdd = _query_bool(query, "compute_mdd", False)
    equity_base_usd = _query_float(query, "equity_base_usd")
    fast_scan = _query_bool(query, "fast_scan", False)
    scan_concurrency_default = 6 if fast_scan else 1
    mdd_concurrency_default = 3 if fast_scan else 1
    scan_start_offset = max(0, _safe_int(_query_value(query, "scan_start_offset", "0"), 0))
    scan_retry_attempts = _clamp_int(
        _query_value(query, "scan_retry_attempts", "1"),
        1,
        1,
        50,
    )
    scan_retry_delay_seconds = max(
        0.0,
        min(float(_safe_float(_query_value(query, "scan_retry_delay_seconds", "0"), 0.0) or 0.0), 3600.0),
    )
    scan_concurrency = _clamp_int(
        _query_value(query, "scan_concurrency", str(scan_concurrency_default)),
        scan_concurrency_default,
        1,
        12,
    )
    mdd_concurrency = _clamp_int(
        _query_value(query, "mdd_concurrency", str(mdd_concurrency_default)),
        mdd_concurrency_default,
        1,
        6,
    )

    min_pnl = _query_float(query, "min_pnl_usd")
    max_pnl = _query_float(query, "max_pnl_usd")
    min_volume = _query_float(query, "min_volume_usd")
    max_volume = _query_float(query, "max_volume_usd")
    min_roi = _query_float(query, "min_roi_pct")
    max_roi = _query_float(query, "max_roi_pct")
    min_mdd_usd = _query_float(query, "min_mdd_usd")
    max_mdd_usd = _query_float(query, "max_mdd_usd")
    min_mdd_pct = _query_float(query, "min_mdd_pct")
    max_mdd_pct = _query_float(query, "max_mdd_pct")
    mdd_requested = compute_mdd or mdd_mode == "mark_replay" or sort in {"mdd_usd", "mdd_pct"} or any(
        value is not None for value in (min_mdd_usd, max_mdd_usd, min_mdd_pct, max_mdd_pct)
    )
    mdd_stop_on_limit = limit is not None and _query_bool(
        query,
        "mdd_stop_on_limit",
        fast_scan and sort == "roi_pct" and direction == "DESC" and any(
            value is not None for value in (min_mdd_usd, max_mdd_usd, min_mdd_pct, max_mdd_pct)
        ),
    )

    cancelled = False
    warnings: List[str] = []

    def is_cancelled() -> bool:
        if cancel_check is None:
            return False
        try:
            return bool(cancel_check())
        except Exception:
            return True

    def emit_progress(
        phase: str,
        *,
        scanned: int,
        filtered: int = 0,
        mdd_attempted: int = 0,
        mdd_computed: int = 0,
        mdd_total: Optional[int] = None,
        wallet: str = "",
        message: str = "",
        percent: Optional[float] = None,
    ) -> None:
        if progress_callback is None:
            return
        if percent is None:
            if phase == "leaderboard":
                if scan_limit is None:
                    percent = 0.0
                else:
                    scan_fraction = min(scanned / max(scan_limit, 1), 1.0)
                    percent = scan_fraction * (50.0 if mdd_requested else 100.0)
            elif phase == "mdd":
                total = max(int(mdd_total or 0), 1)
                percent = 50.0 + (min(mdd_attempted / total, 1.0) * 50.0)
            else:
                percent = 100.0
        try:
            progress_callback(
                {
                    "phase": phase,
                    "percent": max(0.0, min(float(percent), 100.0)),
                    "scanned": scanned,
                    "scan_limit": scan_limit,
                    "scan_limit_unlimited": scan_limit is None,
                    "filtered": filtered,
                    "mdd_attempted": mdd_attempted,
                    "mdd_computed": mdd_computed,
                    "mdd_total": mdd_total if mdd_total is not None else (mdd_scan_limit if mdd_requested and mdd_scan_limit is not None else 0),
                    "mdd_scan_limit": mdd_scan_limit,
                    "mdd_scan_limit_unlimited": mdd_scan_limit is None,
                    "wallet": wallet,
                    "message": message,
                }
            )
        except Exception:
            pass

    remote_sort = LEADERBOARD_SORTS[sort]
    checkpoint_rows = [dict(row) for row in (initial_raw_rows or [])]
    scan_summary: Dict[str, Any] = {}
    raw_rows, leaderboard_cancelled = _fetch_polymarket_leaderboard_scan_rows(
        scan_limit=scan_limit,
        scan_start_offset=scan_start_offset,
        initial_rows=checkpoint_rows,
        remote_sort=remote_sort,
        direction=direction,
        period=period,
        category=category,
        scan_concurrency=scan_concurrency,
        scan_retry_attempts=scan_retry_attempts,
        scan_retry_delay_seconds=scan_retry_delay_seconds,
        is_cancelled=is_cancelled,
        emit_progress=emit_progress,
        warnings=warnings,
        page_callback=leaderboard_page_callback,
        scan_summary=scan_summary,
    )
    cancelled = cancelled or leaderboard_cancelled

    rate_limit_events: List[Dict[str, Any]] = []
    rows = [normalize_polymarket_leaderboard_row(row, index + 1) for index, row in enumerate(raw_rows)]
    seen_wallets: set[str] = set()
    duplicate_rows = 0
    prefiltered: List[Dict[str, Any]] = []
    for row in rows:
        wallet = str(row.get("wallet") or "").strip().lower()
        if wallet:
            if wallet in seen_wallets:
                duplicate_rows += 1
                continue
            seen_wallets.add(wallet)
        if not _number_in_range(row["pnl_usd"], min_pnl, max_pnl):
            continue
        if not _number_in_range(row["volume_usd"], min_volume, max_volume):
            continue
        if not _number_in_range(row["roi_pct"], min_roi, max_roi):
            continue
        prefiltered.append(row)

    computed_mdd = 0
    attempted_mdd = 0
    qualified_mdd = 0
    unavailable_mdd = 0
    history_limited_mdd = 0
    if mdd_requested:
        mdd_candidate_rows = list(prefiltered)
        if sort not in {"mdd_usd", "mdd_pct"}:
            _sort_polymarket_leaderboard_rows(mdd_candidate_rows, sort, direction)
        mdd_targets = _limit_slice(mdd_candidate_rows, mdd_scan_limit)
        mdd_total = len(mdd_targets)
        return_limit_label = _limit_label(limit)

        def build_mdd_options() -> Dict[str, Any]:
            return {
                "mode": mdd_mode,
                "closed_limit": mdd_history_limit,
                "open_limit": mdd_open_limit,
                "activity_limit": mdd_activity_limit,
                "trade_limit": mdd_trade_limit,
                "include_open": True,
                "equity_base_usd": equity_base_usd,
                "cache_ttl_seconds": mdd_cache_ttl_seconds,
                "mark_replay_token_limit": mdd_mark_replay_token_limit,
                "mark_replay_point_limit": mdd_mark_replay_point_limit,
                "mark_replay_interval": mdd_mark_replay_interval,
                "mark_replay_fidelity": mdd_mark_replay_fidelity,
                "mark_replay_start_ts": int(mdd_mark_replay_start_ts) if mdd_mark_replay_start_ts is not None else None,
                "mark_replay_end_ts": int(mdd_mark_replay_end_ts) if mdd_mark_replay_end_ts is not None else None,
                "include_accounting_snapshot": mdd_include_accounting,
                "accounting_timeout": mdd_accounting_timeout,
            }

        def compute_mdd_for_row(row: Dict[str, Any]) -> Tuple[Dict[str, Any], str, Optional[Dict[str, Any]], Dict[str, Any], Optional[BaseException]]:
            wallet = normalize_wallet(row.get("wallet") or "")
            if not wallet:
                return row, "", None, {}, None
            options = build_mdd_options()
            try:
                with cancellation_scope(is_cancelled):
                    return row, wallet, polymarket_user_mdd_payload(wallet, **options), options, None
            except RequestCancelled:
                raise
            except Exception as exc:
                return row, wallet, None, options, exc

        def apply_mdd_result(
            row: Dict[str, Any],
            wallet: str,
            mdd: Optional[Dict[str, Any]],
            mdd_options: Dict[str, Any],
            exc: Optional[BaseException],
        ) -> bool:
            nonlocal attempted_mdd, computed_mdd, qualified_mdd, unavailable_mdd, history_limited_mdd
            attempted_mdd += 1
            if not wallet:
                emit_progress(
                    "mdd",
                    scanned=len(rows),
                    filtered=len(prefiltered),
                    mdd_attempted=attempted_mdd,
                    mdd_computed=computed_mdd,
                    mdd_total=mdd_total,
                    message=f"Computing MDD {attempted_mdd}/{mdd_total}.",
                )
                return False
            if isinstance(exc, PolymarketRateLimitError):
                status = polymarket_rate_limit_status(exc)
                rate_limit_events.extend(status["events"])
                warnings.append(f"MDD rate-limited for {wallet}; retry after the upstream backoff window.")
                return True
            if exc is not None:
                warnings.append(f"MDD unavailable for {wallet}: {exc}")
                emit_progress(
                    "mdd",
                    scanned=len(rows),
                    filtered=len(prefiltered),
                    mdd_attempted=attempted_mdd,
                    mdd_computed=computed_mdd,
                    mdd_total=mdd_total,
                    wallet=wallet,
                    message=f"Computing MDD {attempted_mdd}/{mdd_total}.",
                )
                return False
            if mdd is None:
                return False

            audit_cache = attach_polymarket_mdd_audit_cache(
                mdd,
                polymarket_mdd_audit_params(wallet, mdd_options),
                enabled=mdd_persist_cache,
            )
            row.update(
                {
                    "mdd_usd": mdd["mdd_usd"],
                    "mdd_pct": mdd["mdd_pct"],
                    "mdd_available": mdd.get("mdd_available", True),
                    "mdd_scope": mdd.get("mdd_scope", "observed_public_pnl"),
                    "mdd_account_equity_verified": False,
                    "mdd_history_status": mdd.get("mdd_history_status", "unknown"),
                    "mdd_history_coverage": mdd.get("mdd_history_coverage", {}),
                    "mdd_source_quality": mdd.get("mdd_source_quality", {}),
                    "position_capital_basis": mdd.get("position_capital_basis", {}),
                    "mdd_unavailable_reasons": mdd.get("mdd_unavailable_reasons", []),
                    "mdd_calculation_version": mdd.get("calculation_version"),
                    "mdd_pct_peak_value": mdd.get("pct_peak_value"),
                    "mdd_pct_trough_value": mdd.get("pct_trough_value"),
                    "mdd_pct_peak_timestamp": mdd.get("pct_peak_timestamp"),
                    "mdd_pct_trough_timestamp": mdd.get("pct_trough_timestamp"),
                    "mdd_method": mdd["mdd_method"],
                    "mdd_pct_basis": mdd["mdd_pct_basis"],
                    "mdd_points": len(mdd["points"]),
                    "mdd_closed_positions": mdd["closed_positions"],
                    "mdd_open_positions": mdd["open_positions"],
                    "mdd_activity_events": mdd.get("activity_events", 0),
                    "mdd_trade_events": mdd.get("trade_events", 0),
                    "mdd_equity_base_usd": mdd["equity_base_usd"],
                    "mdd_equity_base_source": mdd.get("equity_base_source"),
                    "mdd_public_capital_basis_usd": mdd.get("public_capital_basis_usd"),
                    "mdd_peak_value": mdd["peak_value"],
                    "mdd_trough_value": mdd["trough_value"],
                    "mdd_peak_timestamp": mdd["peak_timestamp"],
                    "mdd_trough_timestamp": mdd["trough_timestamp"],
                    "mdd_mark_replay_status": (mdd.get("mark_replay") or {}).get("status"),
                    "mdd_mark_replay_tokens": (mdd.get("mark_replay") or {}).get("token_count"),
                    "mdd_accounting_status": (mdd.get("accounting_snapshot") or {}).get("status"),
                    "mdd_accounting_equity_base_usd": ((mdd.get("accounting_snapshot") or {}).get("equity") or {}).get("base_equity_usd"),
                    "mdd_accounting_cash_flow_gap_usd": (
                        (((mdd.get("accounting_snapshot") or {}).get("equity") or {}).get("cash_flows") or {}).get("cash_flow_gap_usd")
                    ),
                    "mdd_audit_cache_key": audit_cache.get("key"),
                    "mdd_audit_cache_stored": audit_cache.get("stored", False),
                }
            )
            computed_mdd += 1
            unavailable_mdd += int(not row["mdd_available"])
            history_limited_mdd += int(row["mdd_history_status"] == "limit_reached")
            if row["mdd_available"] and _number_in_range(row["mdd_usd"], min_mdd_usd, max_mdd_usd) and _number_in_range(row["mdd_pct"], min_mdd_pct, max_mdd_pct):
                qualified_mdd += 1
            emit_progress(
                "mdd",
                scanned=len(rows),
                filtered=len(prefiltered),
                mdd_attempted=attempted_mdd,
                mdd_computed=computed_mdd,
                mdd_total=mdd_total,
                wallet=wallet,
                message=f"Computing MDD {attempted_mdd}/{mdd_total}; matched {qualified_mdd}/{return_limit_label}.",
            )
            return False

        emit_progress(
            "mdd",
            scanned=len(rows),
            filtered=len(prefiltered),
            mdd_attempted=0,
            mdd_computed=0,
            mdd_total=mdd_total,
            message=f"Computing MDD 0/{mdd_total}.",
            percent=50.0 if mdd_total else 100.0,
        )
        mdd_index = 0
        rate_limited = False
        while mdd_index < len(mdd_targets):
            if is_cancelled():
                cancelled = True
                warnings.append("MDD scan cancelled by user.")
                break
            if mdd_stop_on_limit and limit is not None and qualified_mdd >= limit:
                warnings.append(f"MDD scan stopped after finding {qualified_mdd} qualifying row(s) within the scanned ROI candidates.")
                break
            batch = mdd_targets[mdd_index : mdd_index + max(1, mdd_concurrency)]
            emit_progress(
                "mdd",
                scanned=len(rows),
                filtered=len(prefiltered),
                mdd_attempted=attempted_mdd,
                mdd_computed=computed_mdd,
                mdd_total=mdd_total,
                message=f"Computing MDD {attempted_mdd + 1}/{mdd_total}.",
            )
            if len(batch) == 1:
                try:
                    rate_limited = apply_mdd_result(*compute_mdd_for_row(batch[0]))
                except RequestCancelled:
                    cancelled = True
            else:
                with ThreadPoolExecutor(max_workers=len(batch)) as executor:
                    for future in as_completed([executor.submit(compute_mdd_for_row, row) for row in batch]):
                        try:
                            if apply_mdd_result(*future.result()):
                                rate_limited = True
                        except RequestCancelled:
                            cancelled = True
                if rate_limited:
                    break
            mdd_index += len(batch)
            if cancelled or is_cancelled():
                cancelled = True
                warnings.append("MDD scan cancelled by user.")
                break
            if rate_limited:
                break

    if unavailable_mdd:
        warnings.append(f"Excluded {unavailable_mdd} unavailable MDD result(s); {history_limited_mdd} reached a history limit.")
    filtered: List[Dict[str, Any]] = []
    for row in prefiltered:
        if mdd_requested:
            if not row["mdd_available"]:
                continue
            if not _number_in_range(row["mdd_usd"], min_mdd_usd, max_mdd_usd):
                continue
            if not _number_in_range(row["mdd_pct"], min_mdd_pct, max_mdd_pct):
                continue
        filtered.append(row)

    _sort_polymarket_leaderboard_rows(filtered, sort, direction)

    mdd_values_available = any(row["mdd_available"] for row in filtered)
    result_rows = _limit_slice(filtered, limit)
    return {
        "rows": result_rows,
        "counts": {
            "returned": len(result_rows),
            "filtered": len(filtered),
            "scanned": len(rows),
            "unique_wallets": len(seen_wallets),
            "duplicate_rows": duplicate_rows,
            "mdd_attempted": attempted_mdd,
            "mdd_computed": computed_mdd,
            "mdd_qualified": qualified_mdd,
            "mdd_unavailable": unavailable_mdd,
            "mdd_history_limited": history_limited_mdd,
        },
        "sort": sort,
        "direction": direction,
        "period": period,
        "category": category,
        "limit": limit,
        "limit_unlimited": limit is None,
        "scan_limit": scan_limit,
        "scan_limit_unlimited": scan_limit is None,
        "mdd_scan_limit": mdd_scan_limit,
        "mdd_scan_limit_unlimited": mdd_scan_limit is None,
        "mdd_history_limit": mdd_history_limit,
        "mdd_activity_limit": mdd_activity_limit,
        "mdd_trade_limit": mdd_trade_limit,
        "mdd_open_limit": mdd_open_limit,
        "mdd_mode": mdd_mode,
        "mdd_mark_replay_token_limit": mdd_mark_replay_token_limit,
        "mdd_mark_replay_point_limit": mdd_mark_replay_point_limit,
        "mdd_mark_replay_interval": mdd_mark_replay_interval,
        "mdd_mark_replay_fidelity": mdd_mark_replay_fidelity,
        "mdd_include_accounting": mdd_include_accounting,
        "mdd_accounting_timeout": mdd_accounting_timeout,
        "mdd_persist_cache": mdd_persist_cache,
        "mdd_cache_ttl_seconds": mdd_cache_ttl_seconds,
        "fast_scan": fast_scan,
        "scan_concurrency": scan_concurrency,
        "scan_start_offset": scan_start_offset,
        "scan_retry_attempts": scan_retry_attempts,
        "scan_retry_delay_seconds": scan_retry_delay_seconds,
        "initial_checkpoint_rows": len(checkpoint_rows),
        "mdd_concurrency": mdd_concurrency,
        "mdd_stop_on_limit": mdd_stop_on_limit,
        "analytics_cache": analytics_cache_summary(enabled=mdd_persist_cache),
        "rate_limit": {
            "limited": bool(rate_limit_events),
            "backoff_status": "retry_later" if rate_limit_events else "not_limited",
            "events": rate_limit_events,
        },
        "source": "polymarket_data_api_leaderboard",
        "cancelled": cancelled,
        "source_sort": remote_sort,
        "roi_pct_basis": PNL_VOLUME_BASIS,
        "wallet_observation_policy": "first_observation_per_normalized_wallet",
        "ranking_scope": "computed_from_scanned_public_leaderboard_rows_with_optional_public_data_mdd_v2",
        "completion_reason": str(scan_summary.get("completion_reason") or "unknown"),
        "source_enumeration_complete": bool(scan_summary.get("source_enumeration_complete")),
        "source_max_offset": LEADERBOARD_MAX_OFFSET,
        "source_scope_note": (
            "Results cover only rows exposed by the public Polymarket leaderboard for the selected period and category; "
            "they do not establish coverage of every Polymarket account."
        ),
        "search_strategy": (
            "fast_roi_candidates_then_adaptive_mdd_filter"
            if fast_scan and mdd_requested and sort == "roi_pct"
            else "scanned_public_leaderboard_rows"
        ),
        "mdd_available": mdd_values_available,
        "mdd_method": MDD_METHOD_MARK_REPLAY if mdd_mode == "mark_replay" else MDD_METHOD_V2,
        "mdd_pct_basis": MDD_PCT_BASIS_V2,
        "mdd_note": (
            "Observed MDD uses sampled CLOB prices and fetched inventory, not verified account equity. Incomplete replay or capped history is excluded; fast fallback is diagnostic only."
            if mdd_mode == "mark_replay"
            else "MDD v2 uses public closed-position realized PnL, public trade/activity capital basis, and the current open-position snapshot; complete account-equity MDD still requires cash-flow ledger and historical mark replay."
        ),
        "mdd_assumptions": list(MDD_MARK_REPLAY_ASSUMPTIONS if mdd_mode == "mark_replay" else MDD_V2_ASSUMPTIONS)
        + (list(MDD_ACCOUNTING_ASSUMPTIONS) if mdd_include_accounting else []),
        "mdd_limitations": list(MDD_MARK_REPLAY_LIMITATIONS if mdd_mode == "mark_replay" else MDD_V2_LIMITATIONS)
        + (list(MDD_ACCOUNTING_LIMITATIONS) if mdd_include_accounting else []),
        "warnings": warnings,
    }


def find_wallet(cfg: AppConfig, wallet_id: str) -> WalletWatch:
    normalized = str(wallet_id or "").strip()
    for wallet in cfg.wallets:
        if wallet.id == normalized:
            return wallet
    raise ValueError(f"Unknown wallet id: {normalized}")


def wallet_from_payload(
    payload: Mapping[str, Any],
    existing: Optional[WalletWatch] = None,
    *,
    market_id: str = "polymarket",
) -> WalletWatch:
    raw_wallet = str(payload.get("wallet") if "wallet" in payload else (existing.wallet if existing else "")).strip()
    wallet = normalize_activity_identity(market_id, raw_wallet)
    if not wallet:
        if str(market_id or "").strip().lower() == "manifold":
            raise ValueError("wallet must use the safe manifold:<username> activity identity format.")
        raise ValueError("wallet must be a valid 0x wallet/proxyWallet address.")
    display_name = str(
        payload.get("display_name") if "display_name" in payload else (existing.display_name if existing else "")
    ).strip()
    enabled = bool_from_setting(payload.get("enabled"), existing.enabled if existing else True)
    only_market_slug = str(
        payload.get("only_market_slug")
        if "only_market_slug" in payload
        else (existing.only_market_slug if existing else "")
    ).strip()
    if existing is None:
        return WalletWatch(wallet=wallet, display_name=display_name or f"{wallet[:10]}...", enabled=enabled, only_market_slug=only_market_slug)
    existing.wallet = wallet
    existing.display_name = display_name
    existing.enabled = enabled
    existing.only_market_slug = only_market_slug
    return existing


def add_wallet_watch(cfg: AppConfig, payload: Mapping[str, Any]) -> WalletWatch:
    market_id = require_selected_market(cfg, "Wallet tracking")
    wallet = wallet_from_payload(payload, market_id=market_id)
    if any(item.wallet == wallet.wallet for item in cfg.wallets):
        raise ValueError("This wallet is already being tracked.")
    cfg.wallets.append(wallet)
    return wallet


def update_wallet_watch(cfg: AppConfig, wallet_id: str, payload: Mapping[str, Any]) -> WalletWatch:
    market_id = require_selected_market(cfg, "Wallet tracking")
    wallet = find_wallet(cfg, wallet_id)
    wallet_from_payload(payload, existing=wallet, market_id=market_id)
    duplicates = [item for item in cfg.wallets if item.wallet == wallet.wallet and item.id != wallet.id]
    if duplicates:
        raise ValueError("This wallet is already being tracked.")
    return wallet


def delete_wallet_watch(cfg: AppConfig, wallet_id: str) -> WalletWatch:
    wallet = find_wallet(cfg, wallet_id)
    cfg.wallets = [item for item in cfg.wallets if item.id != wallet.id]
    return wallet


def copy_payload(
    cfg: AppConfig,
    registry: Optional[AdapterRegistry] = None,
) -> Dict[str, Any]:
    registry = registry or build_default_registry()
    market_id = str(cfg.selected_market_id or "").strip().lower() or "polymarket"
    market_cfg = cfg.markets.get(market_id)
    settings = market_cfg.settings if market_cfg else {}
    status = "simulation"
    if not cfg.copytrading.enabled:
        status = "disabled"
    elif cfg.copytrading.live:
        status = "live requested"
    live_gate = {
        "market_enabled": bool(market_cfg.enabled) if market_cfg else False,
        "live_trading_enabled": bool_from_setting(settings.get("live_trading_enabled"), False),
        "live_trading_confirmed": bool_from_setting(settings.get("live_trading_confirmed"), False)
        or bool_from_setting(settings.get("live_trading_acknowledged"), False),
        "live_trading_kill_switch": bool_from_setting(settings.get("live_trading_kill_switch"), False)
        or bool_from_setting(settings.get("live_trading_paused"), False),
        "max_size": _safe_float(settings.get("live_trading_max_size"), None),
        "max_notional": _safe_float(settings.get("live_trading_max_notional"), None),
    }
    try:
        adapter = adapter_for_market(cfg, market_id, registry)
        capability = adapter.capabilities.copy_trading
        adapter_name = adapter.display_name
    except Exception:
        capability = False
        adapter_name = market_id
    followed_wallets = cfg.copytrading.normalized_follow_wallets()
    tracked_wallets = {wallet.wallet for wallet in cfg.wallets}
    return {
        "settings": cfg.copytrading.to_dict(),
        "wallet_choices": [wallet.wallet for wallet in cfg.wallets],
        "follow_wallet_tracked": any(wallet in tracked_wallets for wallet in followed_wallets),
        "follow_wallets_tracked": sum(1 for wallet in followed_wallets if wallet in tracked_wallets),
        "follow_wallets_untracked": [wallet for wallet in followed_wallets if wallet not in tracked_wallets],
        "status": status,
        "simulation_first": not cfg.copytrading.live,
        "copy_trading_supported": bool(capability),
        "adapter": adapter_name,
        "market_id": market_id,
        "activity_identity_hint": activity_identity_hint(market_id),
        "live_gate": live_gate,
    }


def _wallets_from_copy_payload(
    payload: Mapping[str, Any],
    existing: CopyTradeSettings,
    *,
    market_id: str = "polymarket",
) -> List[str]:
    raw_values: List[Any] = []
    if "follow_wallets" in payload:
        raw = payload.get("follow_wallets")
        if isinstance(raw, list):
            raw_values.extend(raw)
        elif isinstance(raw, str):
            raw_values.extend(raw.replace(";", ",").split(","))
        else:
            raise ValueError("follow_wallets must be a list or comma-separated string.")
    elif "follow_wallet" in payload:
        raw_values.append(payload.get("follow_wallet"))
    else:
        raw_values.extend(existing.normalized_follow_wallets())

    wallets: List[str] = []
    for raw in raw_values:
        if not isinstance(raw, str):
            raise ValueError("Copy follow identities must be strings.")
        raw_wallet = raw.strip()
        if not raw_wallet:
            continue
        normalized = normalize_activity_identity(market_id, raw_wallet)
        if not normalized:
            if str(market_id or "").strip().lower() == "manifold":
                raise ValueError("follow_wallets must contain only safe manifold:<username> activity identities.")
            raise ValueError("follow_wallets must contain only valid activity identities (0x or Solana wallets).")
        if normalized not in wallets:
            wallets.append(normalized)
    if "follow_wallets" in payload and "follow_wallet" in payload:
        single = _wallets_from_copy_payload({"follow_wallet": payload["follow_wallet"]}, existing, market_id=market_id)
        if single != wallets[:1]:
            raise ValueError("follow_wallet and follow_wallets disagree.")
    return wallets


def copy_settings_from_payload(
    payload: Mapping[str, Any],
    existing: CopyTradeSettings,
    *,
    market_id: str = "polymarket",
) -> CopyTradeSettings:
    follow_wallets = _wallets_from_copy_payload(payload, existing, market_id=market_id)
    data = existing.to_dict()
    data.pop("copy_percentage")
    for key in (
        "enabled", "live", "scale", "max_usdc_per_trade", "slippage",
        "allow_sells", "conflict_guard", "conflict_window_seconds",
    ):
        if key in payload:
            data[key] = payload[key]
    percentages = [payload[key] for key in ("copy_percentage", "scale_percent", "percentage") if key in payload]
    if percentages:
        scales = [CopyTradeSettings.from_dict({"copy_percentage": value}).scale for value in percentages]
        if any(not math.isclose(scales[0], scale, rel_tol=0, abs_tol=1e-12) for scale in scales[1:]):
            raise ValueError("Copy percentage aliases disagree.")
        data["copy_percentage"] = percentages[0]
        if "scale" not in payload:
            data.pop("scale")
    data["follow_wallet"] = follow_wallets[0] if follow_wallets else ""
    data["follow_wallets"] = follow_wallets
    return CopyTradeSettings.from_dict(data)


def apply_copy_settings_patch(cfg: AppConfig, payload: Mapping[str, Any]) -> CopyTradeSettings:
    market_id = require_selected_market(cfg, "Copy trading settings")
    cfg.copytrading = copy_settings_from_payload(payload, cfg.copytrading, market_id=market_id)
    return cfg.copytrading


def copy_trade_guard_key(activity: Mapping[str, Any]) -> str:
    token_id = str(activity.get("asset") or "").strip().lower()
    market_slug = str(activity.get("slug") or "").strip().lower()
    outcome = str(activity.get("outcome") or "").strip().lower()
    return "|".join(part for part in (token_id, market_slug, outcome) if part)


def copy_trade_conflict_reason(
    settings: CopyTradeSettings,
    activity: Mapping[str, Any],
    conflict_state: Optional[Dict[str, Dict[str, Any]]],
) -> Optional[str]:
    if conflict_state is None or not settings.conflict_guard:
        return None
    key = copy_trade_guard_key(activity)
    if not key:
        return None
    side = str(activity.get("side") or "").strip().upper()
    wallet = str(activity.get("proxyWallet") or "").strip().lower()
    timestamp = int(_safe_float(activity.get("timestamp"), time.time()) or time.time())
    window = max(0, int(settings.conflict_window_seconds or 0))
    if window:
        stale = [
            state_key
            for state_key, state in conflict_state.items()
            if timestamp - int(state.get("timestamp") or timestamp) > window
        ]
        for state_key in stale:
            conflict_state.pop(state_key, None)
    existing = conflict_state.get(key)
    if existing:
        previous_side = str(existing.get("side") or "").upper()
        previous_wallet = str(existing.get("wallet") or "")
        if previous_wallet == wallet:
            conflict_state[key] = {
                "side": side,
                "wallet": wallet,
                "activity_key": activity_key(activity),
                "timestamp": timestamp,
            }
            return None
        if previous_side and previous_side != side:
            return f"conflict guard blocked opposite-side copy for the same token from {previous_wallet}"
        return f"conflict guard skipped duplicate same-token copy already accepted from {previous_wallet}"
    conflict_state[key] = {
        "side": side,
        "wallet": wallet,
        "activity_key": activity_key(activity),
        "timestamp": timestamp,
    }
    return None


def copy_trade_preview_from_activity(
    cfg: AppConfig,
    registry: AdapterRegistry,
    activity: Mapping[str, Any],
    conflict_state: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    market_id = require_selected_market(cfg, "Copy trading preview")
    settings = cfg.copytrading
    if not settings.enabled:
        return {"status": "skipped", "reason": "copy trading disabled"}
    adapter = adapter_for_market(cfg, market_id, registry)
    if not adapter.capabilities.copy_trading:
        return {
            "status": "skipped",
            "reason": f"{adapter.display_name} does not expose an official account-activity copy feed",
        }
    followed_wallets = settings.normalized_follow_wallets()
    if not followed_wallets:
        return {"status": "skipped", "reason": "follow wallet is not set"}
    activity_identity = normalize_activity_identity(market_id, activity.get("proxyWallet"))
    if not activity_identity or activity_identity not in followed_wallets:
        return {"status": "skipped", "reason": "activity wallet does not match follow wallet"}
    side = str(activity.get("side") or "").strip().upper()
    if side not in {"BUY", "SELL"}:
        return {"status": "skipped", "reason": "activity side is not BUY or SELL"}
    if side == "SELL" and not settings.allow_sells:
        return {"status": "skipped", "reason": "SELL copying disabled"}
    token_id = str(activity.get("asset") or activity.get("contract_id") or "").strip()
    if not token_id:
        return {"status": "skipped", "reason": "activity has no asset token"}
    raw_size = _safe_float(activity.get("size"), 0.0) or 0.0
    raw_price = _safe_float(activity.get("price"), None)
    size = max(0.0, raw_size * float(settings.scale))
    best_bid = best_ask = None
    is_azuro = market_id == "azuro"
    if not is_azuro:
        try:
            orderbook = adapter.get_orderbook(token_id)
            best_bid = orderbook.bids[0].price if orderbook.bids else None
            best_ask = orderbook.asks[0].price if orderbook.asks else None
        except Exception:
            pass
    slippage = max(0.0, min(float(settings.slippage), 1.0))
    if is_azuro:
        raw_odds = _safe_float(activity.get("odds"), None)
        if raw_odds is None and raw_price is not None and raw_price > 0:
            raw_odds = 1.0 / raw_price
        if raw_odds is None or raw_odds <= 0:
            return {"status": "skipped", "reason": "Azuro activity has no valid decimal odds"}
        # Azuro copy previews use decimal minimum odds; slippage lowers the
        # accepted odds floor instead of adding to a probability.
        limit_price = max(1e-12, float(raw_odds) * (1.0 - slippage))
    elif side == "BUY":
        reference_price = best_ask if best_ask is not None else raw_price
        limit_price = min(1.0, float(reference_price if reference_price is not None else 0.99) + slippage)
    else:
        reference_price = best_bid if best_bid is not None else raw_price
        limit_price = max(0.0, float(reference_price if reference_price is not None else 0.01) - slippage)
    max_usdc = max(0.01, float(settings.max_usdc_per_trade))
    capped = False
    # Manifold and Myriad BUY activity sizes are collateral budgets, while
    # SELL activity sizes are shares. Do not divide a BUY budget by
    # probability a second time.
    buy_budget_activity = market_id in {"azuro", "manifold", "myriad_markets"} and side == "BUY"
    if buy_budget_activity:
        if size > max_usdc:
            size = max_usdc
            capped = True
    elif limit_price > 0:
        max_shares = max_usdc / limit_price
        if size > max_shares:
            size = max_shares
            capped = True
    if size <= 0:
        return {"status": "skipped", "reason": "computed copy size is zero"}
    conflict_reason = copy_trade_conflict_reason(settings, activity, conflict_state)
    if conflict_reason:
        return {"status": "skipped", "reason": conflict_reason, "conflict_guard": True}
    order_metadata: Dict[str, Any] = {
        "source": "copy_trading",
        "tif": "FOK",
        "activity_key": activity_key(activity),
    }
    if market_id in {"manifold", "myriad_markets"} and side == "SELL":
        order_metadata["shares"] = activity.get("shares") or size
    order = PaperOrderRequest(
        market_id=market_id,
        contract_id=token_id,
        side=side,
        size=size,
        limit_price=limit_price,
        metadata=order_metadata,
    )
    approx_notional = (
        order.size
        if buy_budget_activity
        else order.size * max(1.0, float(order.limit_price or 0.0))
    )
    result: Dict[str, Any] = {
        "status": "live_preflight" if settings.live else "simulation",
        "live": bool(settings.live),
        "would_place_order": bool(settings.live),
        "order": {
            "market_id": order.market_id,
            "contract_id": order.contract_id,
            "side": order.side,
            "size": order.size,
            "limit_price": order.limit_price,
            "approx_notional": approx_notional,
            "exposure_model": "full_size_upper_bound",
        },
        "pricing": {
            "raw_price": raw_price,
            "raw_odds": _safe_float(activity.get("odds"), None),
            "best_bid": best_bid,
            "best_ask": best_ask,
            "slippage": slippage,
            "capped_by_max_usdc": capped,
        },
        "conflict_guard": bool(settings.conflict_guard),
    }
    if settings.live:
        try:
            result["preflight"] = adapter.preflight_live_order(order, feature_name="live copy trading")
            result["blocked"] = False
            result["message"] = "Live copy preflight passed. No order was placed."
        except Exception as exc:
            result["blocked"] = True
            result["message"] = f"Live copy preflight blocked: {exc}"
    else:
        result["blocked"] = False
        result["message"] = (
            f"Simulation: would {side} token={token_id[:10]}... size={size:.4f} "
            f"limit={limit_price:.4f}; no order placed."
        )
    return result


def copy_preview_payload(cfg: AppConfig, registry: AdapterRegistry, payload: Mapping[str, Any]) -> Dict[str, Any]:
    require_selected_market(cfg, "Copy trading preview")
    default_wallets = cfg.copytrading.normalized_follow_wallets()
    activity = {
        "proxyWallet": payload.get("proxyWallet") or payload.get("proxy_wallet") or (default_wallets[0] if default_wallets else ""),
        "asset": payload.get("asset") or payload.get("contract_id") or payload.get("token_id") or "",
        "side": payload.get("side") or "BUY",
        "size": payload.get("size") if "size" in payload else 0,
        "price": payload.get("price") if "price" in payload else None,
        "timestamp": int(time.time()),
        "slug": payload.get("slug") or "",
        "outcome": payload.get("outcome") or "",
    }
    preview = copy_trade_preview_from_activity(cfg, registry, activity)
    return {"preview": preview, "copy": copy_payload(cfg, registry)}


def poll_wallet_activity(
    cfg: AppConfig,
    registry: AdapterRegistry,
    recent_activity: List[Dict[str, Any]],
    *,
    limit: int = 25,
) -> Dict[str, Any]:
    market_id = require_selected_market(cfg, "Wallet polling")
    adapter = adapter_for_market(cfg, market_id, registry)
    activity_loader = getattr(adapter, "list_activity", None)
    if not callable(activity_loader) and market_id != "polymarket":
        raise ValueError(
            f"{adapter.display_name} does not expose an official wallet activity feed for tracking."
        )
    emitted: List[Dict[str, Any]] = []
    problems: List[str] = []
    copy_conflicts: Dict[str, Dict[str, Any]] = {}
    for wallet in list(cfg.wallets):
        if not wallet.enabled:
            continue
        try:
            if callable(activity_loader):
                items = activity_loader(wallet.wallet, limit=limit)
            else:
                items = data_api.get_activity(wallet.wallet, limit=limit, types=["TRADE"])
        except Exception as exc:
            problems.append(f"{wallet.wallet}: {exc}")
            continue
        seen_keys = set(wallet.seen_activity_keys or [])
        new_items: List[Tuple[str, Mapping[str, Any]]] = []
        for item in reversed(items or []):
            if not isinstance(item, Mapping):
                continue
            key = activity_key(item)
            if key in seen_keys:
                continue
            timestamp = int(item.get("timestamp") or 0)
            tx = str(item.get("transactionHash") or item.get("transaction_hash") or "")
            if timestamp > (wallet.last_seen_ts or 0):
                new_items.append((key, item))
                seen_keys.add(key)
            elif timestamp == (wallet.last_seen_ts or 0) and (not tx or tx != (wallet.last_seen_tx or "")):
                new_items.append((key, item))
                seen_keys.add(key)
        for key, item in new_items:
            if wallet.only_market_slug and str(item.get("slug") or "") != wallet.only_market_slug:
                continue
            wallet.last_seen_ts = max(wallet.last_seen_ts or 0, int(item.get("timestamp") or 0))
            wallet.last_seen_tx = str(item.get("transactionHash") or item.get("transaction_hash") or wallet.last_seen_tx or "")
            wallet.seen_activity_keys.append(key)
            if len(wallet.seen_activity_keys) > 200:
                wallet.seen_activity_keys = wallet.seen_activity_keys[-200:]
            activity = wallet_activity_payload(wallet, item)
            activity["copy_preview"] = copy_trade_preview_from_activity(cfg, registry, item, copy_conflicts)
            emitted.insert(0, activity)
            recent_activity.insert(0, activity)
    del recent_activity[100:]
    return {"activity": emitted, "problems": problems, "polled_wallets": sum(1 for wallet in cfg.wallets if wallet.enabled)}


def paper_order_from_payload(payload: Mapping[str, Any]) -> PaperOrderRequest:
    market_id = str(payload.get("market_id") or "").strip().lower()
    contract_id = str(payload.get("contract_id") or "").strip()
    side = str(payload.get("side") or "").strip().upper()
    if not market_id:
        raise ValueError("market_id is required.")
    if not contract_id:
        raise ValueError("contract_id is required.")
    if side not in {"BUY", "SELL", "BACK", "LAY"}:
        raise ValueError("side must be BUY, SELL, BACK, or LAY.")
    size = optional_positive_float(payload.get("size"), "Order size")
    if size is None:
        raise ValueError("Order size is required.")
    limit_price = optional_positive_float(payload.get("limit_price"), "Limit price")
    return PaperOrderRequest(
        market_id=market_id,
        contract_id=contract_id,
        side=side,
        size=size,
        limit_price=limit_price,
        metadata=dict(payload.get("metadata") or {}),
    )


def paper_order_impact(records: List[PaperTradeRecord], order: PaperOrderRequest) -> Dict[str, Any]:
    current_row = next(
        (
            row
            for row in paper_position_rows(records)
            if row["market_id"] == order.market_id and row["contract_id"] == order.contract_id
        ),
        None,
    )
    current_net = float(current_row["net_size"]) if current_row else 0.0
    current_notional = current_row.get("notional") if current_row else None
    signed_size = _paper_order_signed_size(order)
    projected_net = current_net + signed_size
    order_notional = signed_size * float(order.limit_price) if order.limit_price is not None else None
    projected_notional = (
        float(current_notional) + float(order_notional)
        if current_notional is not None and order_notional is not None
        else None
    )
    projected_average = (
        abs(projected_notional) / abs(projected_net)
        if projected_notional is not None and projected_net != 0
        else None
    )
    return {
        "market_id": order.market_id,
        "contract_id": order.contract_id,
        "side": order.side,
        "size": order.size,
        "limit_price": order.limit_price,
        "current_net": current_net,
        "signed_size": signed_size,
        "projected_net": projected_net,
        "effect": _paper_order_effect(current_net, signed_size, projected_net),
        "order_notional": order_notional,
        "projected_notional": projected_notional,
        "projected_average": projected_average,
    }


def format_paper_order_impact(impact: Mapping[str, Any]) -> str:
    parts = [
        f"Impact: {impact['market_id']}:{impact['contract_id']}",
        f"{impact['side']} size={float(impact['size']):g}",
        f"current_net={float(impact['current_net']):.4f}",
        f"order_net={float(impact['signed_size']):.4f}",
        f"projected_net={float(impact['projected_net']):.4f}",
        f"effect={impact['effect']}",
    ]
    if impact.get("order_notional") is None:
        parts.append("limit blank")
    else:
        parts.append(f"order_notional={float(impact['order_notional']):.4f}")
    if impact.get("projected_notional") is not None:
        parts.append(f"projected_notional={float(impact['projected_notional']):.4f}")
    if impact.get("projected_average") is not None:
        parts.append(f"projected_avg={float(impact['projected_average']):.4f}")
    return "; ".join(parts)


def serialize_market_event(event: MarketEvent) -> Dict[str, Any]:
    """Return the stable, non-raw event schema shared by API and clients."""

    return {
        "market_id": event.market_id,
        "event_id": event.event_id,
        "title": event.title,
        "url": event.url,
        "status": event.status,
    }


def serialize_market_contract(contract: MarketContract) -> Dict[str, Any]:
    """Return the stable, non-raw contract schema shared by API and clients."""

    return {
        "market_id": contract.market_id,
        "contract_id": contract.contract_id,
        "event_id": contract.event_id,
        "title": contract.title,
        "outcome": contract.outcome,
        "url": contract.url,
        "status": contract.status,
    }


def serialize_price_snapshot(snapshot: Optional[PriceSnapshot]) -> Optional[Dict[str, Any]]:
    if snapshot is None:
        return None
    midpoint = snapshot.midpoint
    if midpoint is None and snapshot.bid is not None and snapshot.ask is not None:
        midpoint = (float(snapshot.bid) + float(snapshot.ask)) / 2.0
    return {
        "market_id": snapshot.market_id,
        "contract_id": snapshot.contract_id,
        "last": snapshot.last,
        "bid": snapshot.bid,
        "ask": snapshot.ask,
        "midpoint": midpoint,
        "source": snapshot.source,
    }


def serialize_orderbook(orderbook: Optional[OrderBookSnapshot]) -> Optional[Dict[str, Any]]:
    if orderbook is None:
        return None
    return {
        "market_id": orderbook.market_id,
        "contract_id": orderbook.contract_id,
        "bids": [{"price": level.price, "size": level.size} for level in orderbook.bids],
        "asks": [{"price": level.price, "size": level.size} for level in orderbook.asks],
        "best_bid": orderbook.bids[0].price if orderbook.bids else None,
        "best_ask": orderbook.asks[0].price if orderbook.asks else None,
    }


def serialize_market_trade(trade: MarketTrade) -> Dict[str, Any]:
    return {
        "market_id": trade.market_id,
        "contract_id": trade.contract_id,
        "trade_id": trade.trade_id,
        "side": trade.side,
        "price": trade.price,
        "size": trade.size,
        "timestamp": trade.timestamp,
    }


def serialize_market_candle(candle: MarketCandle) -> Dict[str, Any]:
    return {
        "market_id": candle.market_id,
        "contract_id": candle.contract_id,
        "timestamp": candle.timestamp,
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
        "volume": candle.volume,
    }


def market_events_payload(
    cfg: AppConfig,
    registry: AdapterRegistry,
    market_id: str,
    query_params: Mapping[str, List[str]],
) -> Dict[str, Any]:
    normalized_market_id = str(market_id or "").strip().lower()
    require_market_enabled(cfg, normalized_market_id, "event listing")
    adapter = adapter_for_market(cfg, normalized_market_id, registry)
    query = _query_value(query_params, "query", "")
    limit = _clamp_int(_query_value(query_params, "limit", "50"), 50, 1, 1000)
    events = adapter.list_events(query, limit=limit)
    return {
        "market_id": normalized_market_id,
        "query": query,
        "limit": limit,
        "events": [serialize_market_event(event) for event in events],
    }


def market_contracts_payload(
    cfg: AppConfig,
    registry: AdapterRegistry,
    market_id: str,
    query_params: Mapping[str, List[str]],
) -> Dict[str, Any]:
    normalized_market_id = str(market_id or "").strip().lower()
    event_id = _query_value(query_params, "event_id")
    if not event_id:
        raise ValueError("event_id is required.")
    require_market_enabled(cfg, normalized_market_id, "contract listing")
    adapter = adapter_for_market(cfg, normalized_market_id, registry)
    contracts = adapter.list_contracts(event_id)
    return {
        "market_id": normalized_market_id,
        "event_id": event_id,
        "contracts": [serialize_market_contract(contract) for contract in contracts],
    }


def market_price_payload(
    cfg: AppConfig,
    registry: AdapterRegistry,
    market_id: str,
    query_params: Mapping[str, List[str]],
) -> Dict[str, Any]:
    normalized_market_id = str(market_id or "").strip().lower()
    contract_id = _query_value(query_params, "contract_id")
    if not contract_id:
        raise ValueError("contract_id is required.")
    require_market_enabled(cfg, normalized_market_id, "price reading")
    adapter = adapter_for_market(cfg, normalized_market_id, registry)
    return {
        "market_id": normalized_market_id,
        "contract_id": contract_id,
        "price": serialize_price_snapshot(adapter.get_price(contract_id)),
    }


def market_orderbook_payload(
    cfg: AppConfig,
    registry: AdapterRegistry,
    market_id: str,
    query_params: Mapping[str, List[str]],
) -> Dict[str, Any]:
    normalized_market_id = str(market_id or "").strip().lower()
    contract_id = _query_value(query_params, "contract_id")
    if not contract_id:
        raise ValueError("contract_id is required.")
    require_market_enabled(cfg, normalized_market_id, "orderbook reading")
    adapter = adapter_for_market(cfg, normalized_market_id, registry)
    return {
        "market_id": normalized_market_id,
        "contract_id": contract_id,
        "orderbook": serialize_orderbook(adapter.get_orderbook(contract_id)),
    }


def market_trades_payload(
    cfg: AppConfig,
    registry: AdapterRegistry,
    market_id: str,
    query_params: Mapping[str, List[str]],
) -> Dict[str, Any]:
    normalized_market_id = str(market_id or "").strip().lower()
    contract_id = _query_value(query_params, "contract_id")
    if not contract_id:
        raise ValueError("contract_id is required.")
    require_market_enabled(cfg, normalized_market_id, "trade history")
    adapter = adapter_for_market(cfg, normalized_market_id, registry)
    limit = _clamp_int(_query_value(query_params, "limit", "50"), 50, 1, 500)
    before = _query_float(query_params, "before")
    after = _query_float(query_params, "after")
    trades = adapter.list_trades(contract_id, limit=limit, before=before, after=after)
    return {
        "market_id": normalized_market_id,
        "contract_id": contract_id,
        "limit": limit,
        "before": before,
        "after": after,
        "trades": [serialize_market_trade(trade) for trade in trades],
    }


def market_candles_payload(
    cfg: AppConfig,
    registry: AdapterRegistry,
    market_id: str,
    query_params: Mapping[str, List[str]],
) -> Dict[str, Any]:
    normalized_market_id = str(market_id or "").strip().lower()
    contract_id = _query_value(query_params, "contract_id")
    if not contract_id:
        raise ValueError("contract_id is required.")
    require_market_enabled(cfg, normalized_market_id, "candle history")
    adapter = adapter_for_market(cfg, normalized_market_id, registry)
    resolution = _query_value(query_params, "resolution", "1h")
    candles = adapter.list_candles(
        contract_id,
        resolution=resolution,
        from_timestamp=_query_float(query_params, "from"),
        to_timestamp=_query_float(query_params, "to"),
    )
    return {
        "market_id": normalized_market_id,
        "contract_id": contract_id,
        "resolution": resolution,
        "from": _query_float(query_params, "from"),
        "to": _query_float(query_params, "to"),
        "candles": [serialize_market_candle(candle) for candle in candles],
    }


def market_account_payload(
    cfg: AppConfig,
    registry: AdapterRegistry,
    market_id: str,
    operation: str,
    query_params: Mapping[str, List[str]],
) -> Dict[str, Any]:
    """Read one explicitly documented authenticated account operation.

    Account recovery is not treated as public market-data history.  The
    adapter must publish an operation allow-list; arbitrary authenticated
    paths are never accepted by this route.
    """

    normalized_market_id = str(market_id or "").strip().lower()
    normalized_operation = str(operation or "").strip().lower()
    require_market_enabled(cfg, normalized_market_id, "account recovery")
    adapter = adapter_for_market(cfg, normalized_market_id, registry)
    supported = tuple(str(value).strip().lower() for value in getattr(adapter, "account_recovery_operations", ()))
    if normalized_operation not in supported:
        raise UnsupportedFeatureError(
            normalized_market_id,
            "account_recovery",
            f"{normalized_market_id} does not support account operation {normalized_operation or '<empty>'}. "
            f"Supported operations: {', '.join(supported) or 'none'}.",
        )

    kwargs: Dict[str, Any] = {}
    if normalized_market_id == "limitless_exchange":
        kwargs = {"on_behalf_of": _query_value(query_params, "on_behalf_of") or None}
        if normalized_operation == "user_orders":
            raw_market_slug = _query_value(query_params, "market_slug")
            if not raw_market_slug:
                raw_market_slug = _query_value(query_params, "contract_id").split(":", 1)[0]
            kwargs["market_slug"] = raw_market_slug
    elif normalized_market_id == "xmarket":
        raw_contract = _query_value(query_params, "contract_id")
        market_id_filter = _query_value(query_params, "market_id")
        if not market_id_filter and raw_contract:
            market_id_filter = raw_contract.split(":", 1)[0].strip()
        kwargs = {
            "status": _query_value(query_params, "status") or None,
            "page": _clamp_int(_query_value(query_params, "page", "1"), 1, 1, 10000),
            "limit": _clamp_int(_query_value(query_params, "limit", "50"), 50, 1, 1000),
        }
        if normalized_operation == "market_orders":
            kwargs["market_id"] = market_id_filter
    elif normalized_market_id == "smarkets":
        kwargs = {
            "status": _query_value(query_params, "status"),
            "limit": _clamp_int(_query_value(query_params, "limit", "50"), 50, 1, 1000),
        }
    elif normalized_market_id == "probable":
        kwargs = {
            "page": _clamp_int(_query_value(query_params, "page", "1"), 1, 1, 10000),
            "limit": _clamp_int(_query_value(query_params, "limit", "50"), 50, 1, 50),
            "event_id": _query_value(query_params, "event_id"),
            "token_ids": query_params.get("token_ids") or query_params.get("token_id") or [],
        }
        if normalized_operation == "order":
            kwargs = {
                "order_id": _query_value(query_params, "order_id"),
                "token_id": _query_value(query_params, "token_id"),
                "client_order_id": _query_value(query_params, "client_order_id"),
            }
    elif normalized_market_id == "kalshi":
        raw_contract = _query_value(query_params, "contract_id")
        ticker = _query_value(query_params, "ticker") or raw_contract.split(":", 1)[0]
        raw_subaccount = _query_value(query_params, "subaccount")
        subaccount = _clamp_int(raw_subaccount, 0, 0, 63) if raw_subaccount else None
        kwargs.update(
            {
                "ticker": ticker,
                "event_ticker": _query_value(query_params, "event_ticker"),
                "limit": _clamp_int(_query_value(query_params, "limit", "100"), 100, 1, 1000),
                "cursor": _query_value(query_params, "cursor"),
                "min_timestamp": _query_float(query_params, "from"),
                "max_timestamp": _query_float(query_params, "to"),
                "subaccount": subaccount,
            }
        )
        if normalized_operation == "order_history":
            kwargs.update(
                {
                    "status": _query_value(query_params, "status", "executed").lower(),
                    "historical": _query_bool(query_params, "historical", False),
                }
            )
        elif normalized_operation == "fills":
            kwargs.update(
                {
                    "order_id": _query_value(query_params, "order_id"),
                    "historical": _query_bool(query_params, "historical", False),
                }
            )
        elif normalized_operation == "positions":
            kwargs["count_filter"] = _query_value(query_params, "count_filter")
        elif normalized_operation == "queue_positions":
            kwargs = {
                "ticker": ticker,
                "event_ticker": _query_value(query_params, "event_ticker"),
                "subaccount": subaccount,
            }
        elif normalized_operation == "balance":
            kwargs = {"subaccount": subaccount}
    elif normalized_market_id == "polymarket":
        raw_contract = _query_value(query_params, "contract_id")
        kwargs = {
            "market_id": _query_value(query_params, "market_id"),
            "contract_id": raw_contract,
            "next_cursor": _query_value(query_params, "cursor"),
        }
        if normalized_operation == "order_detail":
            kwargs = {"order_id": _query_value(query_params, "order_id")}
        elif normalized_operation == "fills":
            kwargs.update(
                {
                    "trade_id": _query_value(query_params, "trade_id"),
                    "before": _query_float(query_params, "before")
                    if _query_value(query_params, "before") is not None
                    else _query_float(query_params, "to"),
                    "after": _query_float(query_params, "after")
                    if _query_value(query_params, "after") is not None
                    else _query_float(query_params, "from"),
                    "limit": _clamp_int(_query_value(query_params, "limit", "100"), 100, 1, 500),
                }
            )
    elif normalized_market_id == "hyperliquid":
        if normalized_operation in {"active_orders", "positions"}:
            kwargs["dex"] = _query_value(query_params, "dex") or ""
        elif normalized_operation == "order_history":
            kwargs["limit"] = _clamp_int(_query_value(query_params, "limit", "2000"), 2000, 1, 2000)
    elif normalized_market_id == "dflow":
        kwargs = {
            "wallet": _query_value(query_params, "wallet") or _query_value(query_params, "address"),
            "limit": _clamp_int(_query_value(query_params, "limit", "25"), 25, 1, 250),
            "cursor": _query_value(query_params, "cursor"),
            "ticker": _query_value(query_params, "ticker") or _query_value(query_params, "market_id"),
            "mint": _query_value(query_params, "mint") or _query_value(query_params, "token_id"),
        }
    elif normalized_market_id == "predict_fun":
        if normalized_operation == "account":
            kwargs = {}
        elif normalized_operation == "order_detail":
            kwargs = {"order_id": _query_value(query_params, "order_id")}
        elif normalized_operation == "positions_by_address":
            kwargs = {
                "address": _query_value(query_params, "wallet") or _query_value(query_params, "address"),
                "limit": _clamp_int(_query_value(query_params, "limit", "50"), 50, 1, 100),
                "cursor": _query_value(query_params, "cursor"),
                "market_id": _query_value(query_params, "market_id"),
                "is_resolved": _query_value(query_params, "is_resolved"),
                "sort": _query_value(query_params, "sort"),
            }
        else:
            kwargs = {
                "limit": _clamp_int(_query_value(query_params, "limit", "50"), 50, 1, 100),
                "cursor": _query_value(query_params, "cursor"),
                "market_id": _query_value(query_params, "market_id"),
                "status": _query_value(query_params, "status"),
                "is_resolved": _query_value(query_params, "is_resolved"),
                "sort": _query_value(query_params, "sort"),
            }
            if normalized_operation == "account_activity":
                kwargs["event_types"] = _query_value(query_params, "event_types")
    elif normalized_market_id == "manifold":
        if normalized_operation == "account":
            kwargs = {}
        else:
            kwargs = {
                "contract_id": _query_value(query_params, "contract_id") or None,
                "limit": _clamp_int(_query_value(query_params, "limit", "50"), 50, 1, 1000),
                "before": _query_value(query_params, "before") or None,
                "after": _query_value(query_params, "after") or None,
                "before_time": _query_float(query_params, "before_time")
                if _query_value(query_params, "before_time")
                else _query_float(query_params, "to"),
                "after_time": _query_float(query_params, "after_time")
                if _query_value(query_params, "after_time")
                else _query_float(query_params, "from"),
            }
    elif normalized_market_id == "prophet_exchange":
        if normalized_operation == "balance":
            kwargs = {}
        elif normalized_operation == "order_detail":
            kwargs = {"order_id": _query_value(query_params, "order_id")}
        elif normalized_operation == "order_history":
            kwargs = {
                "cursor": _query_value(query_params, "cursor") or _query_value(query_params, "next_cursor"),
                "limit": _clamp_int(_query_value(query_params, "limit", "100"), 100, 1, 100),
                "market_id": _query_value(query_params, "market_id"),
                "event_id": _query_value(query_params, "event_id"),
                "matching_status": _query_value(query_params, "matching_status"),
                "status": _query_value(query_params, "status"),
                "from": _query_value(query_params, "from"),
                "to": _query_value(query_params, "to"),
            }
        elif normalized_operation == "trades":
            kwargs = {
                "cursor": _query_value(query_params, "cursor") or _query_value(query_params, "next_cursor"),
                "limit": _clamp_int(_query_value(query_params, "limit", "100"), 100, 1, 100),
                "from": _query_value(query_params, "from"),
                "to": _query_value(query_params, "to"),
            }
        else:
            raw_cursor = _query_value(query_params, "cursor") or _query_value(query_params, "next")
            kwargs = {
                "cursor": raw_cursor or None,
                "limit": _clamp_int(_query_value(query_params, "limit", "10"), 10, 1, 500),
            }
    elif normalized_market_id == "azuro":
        kwargs = {
            "wallet": _query_value(query_params, "wallet") or _query_value(query_params, "address"),
            "limit": _clamp_int(_query_value(query_params, "limit", "100"), 100, 1, 1000),
            "offset": _clamp_int(_query_value(query_params, "offset", "0"), 0, 0, 1_000_000),
        }
    elif normalized_market_id == "thales_market":
        raw_market_id = _query_value(query_params, "market_id")
        raw_contract_id = _query_value(query_params, "contract_id")
        if not raw_market_id and raw_contract_id:
            raw_market_id = raw_contract_id.split(":", 1)[0].strip()
        kwargs = {
            "wallet": _query_value(query_params, "wallet") or _query_value(query_params, "address"),
            "limit": _clamp_int(_query_value(query_params, "limit", "100"), 100, 1, 1000),
            "market_id": raw_market_id or None,
            "from_timestamp": _query_float(query_params, "from"),
            "to_timestamp": _query_float(query_params, "to"),
        }
        if raw_contract_id:
            kwargs["contract_id"] = raw_contract_id
    elif normalized_market_id in {"metadao", "omen", "gnosis_prediction_markets"}:
        kwargs = {
            "wallet": _query_value(query_params, "wallet") or _query_value(query_params, "address"),
            "limit": _clamp_int(_query_value(query_params, "limit", "25"), 25, 1, 100),
        }
    elif normalized_market_id == "metaculus":
        kwargs = {
            "forecaster_id": _query_value(query_params, "forecaster_id") or None,
            "limit": _clamp_int(_query_value(query_params, "limit", "50"), 50, 1, 100),
            "offset": _clamp_int(_query_value(query_params, "offset", "0"), 0, 0, 100_000),
            "with_cp": _query_bool(query_params, "with_cp", False),
            "include_cp_history": _query_bool(query_params, "include_cp_history", False),
            "include_descriptions": _query_bool(query_params, "include_descriptions", False),
        }
    elif normalized_market_id == "good_judgment_open":
        kwargs = {
            "page": _clamp_int(_query_value(query_params, "page", "0"), 0, 0, 100_000),
            "membership_id": _query_value(query_params, "membership_id") or None,
            "question_id": _query_value(query_params, "question_id") or None,
            "filter": _query_value(query_params, "filter") or None,
            "created_before": _query_value(query_params, "created_before") or None,
            "created_after": _query_value(query_params, "created_after") or None,
            "updated_before": _query_value(query_params, "updated_before") or None,
            "updated_after": _query_value(query_params, "updated_after") or None,
            "score_type": _query_value(query_params, "score_type") or None,
            "scoreable_id": _query_value(query_params, "scoreable_id") or None,
            "predictor_type": _query_value(query_params, "predictor_type") or None,
            "include_daily_scores": _query_bool(query_params, "include_daily_scores", False),
        }
    elif normalized_market_id == "myriad_markets":
        kwargs = {
            "wallet": _query_value(query_params, "wallet") or _query_value(query_params, "address"),
            "limit": _clamp_int(_query_value(query_params, "limit", "25"), 25, 1, 100),
        }
        if normalized_operation in {"portfolio", "market_positions"}:
            kwargs.update(
                {
                    "page": _clamp_int(_query_value(query_params, "page", "1"), 1, 1, 10_000),
                    "trading_model": _query_value(query_params, "trading_model", "all"),
                    "min_shares": _query_value(query_params, "min_shares"),
                    "market_slug": _query_value(query_params, "market_slug"),
                    "market_id": _query_value(query_params, "market_id"),
                    "network_id": _query_value(query_params, "network_id"),
                    "token_address": _query_value(query_params, "token_address"),
                    "status": _query_value(query_params, "status"),
                    "keyword": _query_value(query_params, "keyword"),
                    "sort": _query_value(query_params, "sort"),
                    "sort_by": _query_value(query_params, "sort_by"),
                    "exclude_history": _query_bool(query_params, "exclude_history", False),
                    "group_by_event": _query_bool(query_params, "group_by_event", False),
                }
            )
            if normalized_operation == "market_positions":
                kwargs.update(
                    {
                        "state": _query_value(query_params, "state"),
                        "topics": _query_value(query_params, "topics"),
                        "market_ids": _query_value(query_params, "market_ids"),
                    }
                )
    elif normalized_market_id == "xo_market":
        if normalized_operation in {"account", "positions", "orders"}:
            kwargs = {}
        elif normalized_operation in {"settlement", "settlement_history"}:
            raw_contract = _query_value(query_params, "contract_id")
            market_id_filter = _query_value(query_params, "market_id")
            if not market_id_filter and raw_contract:
                market_id_filter = raw_contract.split(":", 1)[0].strip()
            kwargs = {"market_id": market_id_filter}
            if normalized_operation == "settlement_history":
                kwargs.update(
                    {
                        "limit": _clamp_int(_query_value(query_params, "limit", "50"), 50, 1, 1000),
                        "cursor": _query_value(query_params, "cursor") or None,
                    }
                )
        elif normalized_operation in {"trades", "audit_logs"}:
            kwargs = {
                "limit": _clamp_int(_query_value(query_params, "limit", "100"), 100, 1, 1000),
                "market_id": _query_value(query_params, "market_id") or None,
                "outcome_id": _query_value(query_params, "outcome_id") or None,
                "start_time": _query_value(query_params, "start_time") or _query_value(query_params, "from"),
                "end_time": _query_value(query_params, "end_time") or _query_value(query_params, "to"),
            }
            if normalized_operation == "audit_logs":
                kwargs["event_type"] = _query_value(query_params, "event_type") or None
    elif normalized_market_id == "sx_bet":
        if normalized_operation == "balance":
            kwargs = {}
        elif normalized_operation == "active_orders":
            kwargs = {
                "market_hash": _query_value(query_params, "market_hash") or _query_value(query_params, "market_id"),
                "event_id": _query_value(query_params, "event_id"),
                "per_page": _clamp_int(_query_value(query_params, "limit", "50"), 50, 1, 100),
                "next_key": _query_value(query_params, "cursor"),
            }
        elif normalized_operation == "order_detail":
            kwargs = {"order_id": _query_value(query_params, "order_id")}
        elif normalized_operation == "order_by_client_id":
            kwargs = {"client_order_id": _query_value(query_params, "client_order_id")}
        elif normalized_operation == "order_history":
            kwargs = {
                "market_hash": _query_value(query_params, "market_hash") or _query_value(query_params, "market_id"),
                "status": _query_value(query_params, "status"),
                "start_date": _query_value(query_params, "start_date"),
                "end_date": _query_value(query_params, "end_date"),
                "sort_asc": _query_bool(query_params, "sort_asc", True),
                "per_page": _clamp_int(_query_value(query_params, "limit", "50"), 50, 1, 100),
                "next_key": _query_value(query_params, "cursor"),
            }
        elif normalized_operation == "fills":
            kwargs = {
                "trade_id": _query_value(query_params, "trade_id"),
                "order_id": _query_value(query_params, "order_id"),
                "start_date": _query_value(query_params, "start_date"),
                "end_date": _query_value(query_params, "end_date"),
                "sort_asc": _query_bool(query_params, "sort_asc", True),
                "per_page": _clamp_int(_query_value(query_params, "limit", "50"), 50, 1, 100),
                "next_key": _query_value(query_params, "cursor"),
            }
        elif normalized_operation == "positions":
            kwargs = {
                "status": _query_value(query_params, "status"),
                "event_id": _query_value(query_params, "event_id"),
                "sort_asc": _query_bool(query_params, "sort_asc", False),
                "per_page": _clamp_int(_query_value(query_params, "limit", "50"), 50, 1, 100),
                "next_key": _query_value(query_params, "cursor"),
            }
    elif normalized_market_id in {"ibkr_forecasttrader", "forecastex", "cme_prediction_markets"}:
        kwargs = {
            "filters": _query_value(query_params, "status") or "",
            "force": _query_bool(query_params, "force", False),
        }
        if normalized_operation == "order_status":
            kwargs["order_id"] = _query_value(query_params, "order_id")
    elif normalized_market_id == "opinion_labs":
        if normalized_operation == "order_detail":
            kwargs = {"order_id": _query_value(query_params, "order_id")}
        else:
            kwargs = {
                "page": _clamp_int(_query_value(query_params, "page", "1"), 1, 1, 10000),
                "limit": _clamp_int(_query_value(query_params, "limit", "10"), 10, 1, 20),
                "market_id": _query_value(query_params, "market_id"),
                "chain_id": _query_value(query_params, "chain_id"),
            }
            if normalized_operation == "order_history":
                kwargs["status"] = _query_value(query_params, "status")
    elif normalized_market_id == "matchbook":
        if normalized_operation in {"balance", "account"}:
            kwargs = {}
        elif normalized_operation in {"settled_bets", "current_bets"}:
            kwargs = {
                "offset": _clamp_int(_query_value(query_params, "offset", "0"), 0, 0, 100000),
                "limit": _clamp_int(_query_value(query_params, "limit", "50"), 50, 1, 1000),
                "sport_id": _query_value(query_params, "sport_id"),
                "event_id": _query_value(query_params, "event_id"),
                "market_id": _query_value(query_params, "market_id"),
                "odds_type": _query_value(query_params, "odds_type", "DECIMAL"),
                "from_timestamp": _query_float(query_params, "from"),
                "to_timestamp": _query_float(query_params, "to"),
            }
        elif normalized_operation == "current_offers":
            raw_interval = _query_value(query_params, "interval")
            kwargs = {
                "offset": _clamp_int(_query_value(query_params, "offset", "0"), 0, 0, 100000),
                "limit": _clamp_int(_query_value(query_params, "limit", "20"), 20, 1, 1000),
                "sport_id": _query_value(query_params, "sport_id"),
                "event_id": _query_value(query_params, "event_id"),
                "market_id": _query_value(query_params, "market_id"),
                "runner_id": _query_value(query_params, "runner_id"),
                "side": _query_value(query_params, "side"),
                "status": _query_value(query_params, "offer_status"),
                "interval": _clamp_int(raw_interval, 0, 0, 2147483647) if raw_interval else None,
                "include_edits": _query_bool(query_params, "include_edits", False),
                "cancellation_reason": _query_value(query_params, "cancellation_reason"),
                "aggregation_type": _query_value(query_params, "aggregation_type", "none"),
                "odds_type": _query_value(query_params, "odds_type", "DECIMAL"),
            }
    elif normalized_market_id == "betfair_exchange":
        if normalized_operation in {"funds", "account"}:
            if normalized_operation == "funds":
                kwargs = {"wallet": _query_value(query_params, "wallet")}
        elif normalized_operation == "statement":
            kwargs = {
                "locale": _query_value(query_params, "locale", "en"),
                "limit": _clamp_int(_query_value(query_params, "limit", "100"), 100, 1, 1000),
                "offset": _clamp_int(_query_value(query_params, "offset", "0"), 0, 0, 100000),
                "include_item": not _query_bool(query_params, "exclude_item", False),
                "wallet": _query_value(query_params, "wallet"),
                "from_timestamp": _query_float(query_params, "from"),
                "to_timestamp": _query_float(query_params, "to"),
            }
        elif normalized_operation == "currency_rates":
            kwargs = {"from_currency": _query_value(query_params, "from_currency")}
        elif normalized_operation in {"active_orders", "cleared_orders"}:
            market_id_filter = _query_value(query_params, "market_id")
            runner_id = _query_value(query_params, "runner_id")
            raw_contract = _query_value(query_params, "contract_id")
            if not market_id_filter and raw_contract:
                parts = raw_contract.split(":", 1)
                market_id_filter = parts[0].strip()
                if len(parts) == 2 and not runner_id:
                    runner_id = parts[1].strip()
            if normalized_operation == "active_orders":
                kwargs = {
                    "market_id": market_id_filter,
                    "contract_id": raw_contract,
                    "status": _query_value(query_params, "status"),
                    "order_by": _query_value(query_params, "order_by", "BY_MATCH_TIME"),
                    "sort_dir": _query_value(query_params, "sort_dir", "EARLIEST_TO_LATEST"),
                    "include_item_description": _query_bool(query_params, "include_item_description", False),
                    "limit": _clamp_int(_query_value(query_params, "limit", "100"), 100, 1, 1000),
                    "offset": _clamp_int(_query_value(query_params, "offset", "0"), 0, 0, 100000),
                    "from_timestamp": _query_float(query_params, "from"),
                    "to_timestamp": _query_float(query_params, "to"),
                }
            else:
                kwargs = {
                    "bet_status": _query_value(query_params, "status", "SETTLED"),
                    "market_id": market_id_filter,
                    "event_type_id": _query_value(query_params, "event_type_id"),
                    "event_id": _query_value(query_params, "event_id"),
                    "runner_id": runner_id,
                    "bet_id": _query_value(query_params, "bet_id"),
                    "group_by": _query_value(query_params, "group_by", "BET"),
                    "include_item_description": _query_bool(query_params, "include_item_description", False),
                    "limit": _clamp_int(_query_value(query_params, "limit", "100"), 100, 1, 1000),
                    "offset": _clamp_int(_query_value(query_params, "offset", "0"), 0, 0, 100000),
                    "from_timestamp": _query_float(query_params, "from"),
                    "to_timestamp": _query_float(query_params, "to"),
                }
    elif normalized_operation in {"active_orders", "order_history"}:
        kwargs.update(
            {
                "contract_id": _query_value(query_params, "contract_id") or None,
                "limit": _clamp_int(_query_value(query_params, "limit", "50"), 50, 1, 1000),
                "offset": _clamp_int(_query_value(query_params, "offset", "0"), 0, 0, 100000),
            }
        )
    if normalized_market_id not in {"kalshi", "limitless_exchange", "opinion_labs", "xmarket", "smarkets", "manifold", "sx_bet"} and normalized_operation == "order_history":
        kwargs.update(
            {
                "status": _query_value(query_params, "status", "filled").lower(),
                "from_timestamp": _query_float(query_params, "from"),
                "to_timestamp": _query_float(query_params, "to"),
            }
        )
    elif normalized_market_id not in {"kalshi", "limitless_exchange", "opinion_labs", "xmarket", "sx_bet"} and normalized_operation == "positions":
        raw_limit = _query_value(query_params, "limit")
        kwargs.update(
            {
                "event_ticker": _query_value(query_params, "event_ticker"),
                "limit": _clamp_int(raw_limit, 100, 1, 1000) if raw_limit else None,
                "offset": _clamp_int(_query_value(query_params, "offset", "0"), 0, 0, 100000),
                "sort": _query_value(query_params, "sort") or None,
            }
        )
    elif normalized_market_id not in {"kalshi", "limitless_exchange", "opinion_labs"} and normalized_operation == "settled_positions":
        kwargs.update(
            {
                "event_ticker": _query_value(query_params, "event_ticker"),
                "limit": _clamp_int(_query_value(query_params, "limit", "1000"), 1000, 1, 1000),
                "offset": _clamp_int(_query_value(query_params, "offset", "0"), 0, 0, 100000),
                "sort": _query_value(query_params, "sort", "-date"),
                "search": _query_value(query_params, "search"),
                "category": _query_value(query_params, "category"),
                "with_cash_outs": _query_bool(query_params, "with_cash_outs", False),
            }
        )
    elif normalized_market_id not in {"kalshi", "limitless_exchange", "opinion_labs"} and normalized_operation == "volume_metrics":
        kwargs.update(
            {
                "event_ticker": _query_value(query_params, "event_ticker"),
                "start_timestamp": _query_float(query_params, "from"),
                "end_timestamp": _query_float(query_params, "to"),
            }
        )

    data = adapter.account_recovery(normalized_operation, **kwargs)
    return {
        "market_id": normalized_market_id,
        "operation": normalized_operation,
        "parameters": kwargs,
        "data": data,
    }


def market_position_intent_payload(
    cfg: AppConfig,
    registry: AdapterRegistry,
    market_id: str,
    payload: Mapping[str, Any],
) -> Dict[str, Any]:
    """Request an unsigned, reviewable position transaction from a venue."""

    normalized_market_id = str(market_id or "").strip().lower()
    require_market_enabled(cfg, normalized_market_id, "position transaction intent")
    adapter = adapter_for_market(cfg, normalized_market_id, registry)
    operation = str(payload.get("operation") or "").strip().lower()
    supported = tuple(str(value).strip().lower() for value in getattr(adapter, "position_intent_operations", ()))
    if operation not in supported:
        raise UnsupportedFeatureError(
            normalized_market_id,
            "position_intent",
            f"{normalized_market_id} does not support position operation {operation or '<empty>'}. "
            f"Supported operations: {', '.join(supported) or 'none'}.",
        )
    kwargs: Dict[str, Any] = {}
    for key in ("market_id", "marketId", "amount", "network_id", "networkId", "event_id", "eventId", "outcome_index", "outcomeIndex"):
        if key in payload:
            kwargs[key] = payload.get(key)
    data = adapter.position_intent(operation, **kwargs)
    return {
        "market_id": normalized_market_id,
        "operation": operation,
        "parameters": kwargs,
        "data": data,
    }


def market_order_management_payload(
    cfg: AppConfig,
    registry: AdapterRegistry,
    market_id: str,
    operation: str,
    payload: Mapping[str, Any],
) -> Dict[str, Any]:
    """Run one explicitly allow-listed live order-management mutation."""

    normalized_market_id = str(market_id or "").strip().lower()
    normalized_operation = str(operation or "").strip().lower()
    require_market_enabled(cfg, normalized_market_id, "order management")
    adapter = adapter_for_market(cfg, normalized_market_id, registry)
    supported = tuple(str(value).strip().lower() for value in getattr(adapter, "order_management_operations", ()))
    if normalized_operation not in supported:
        raise UnsupportedFeatureError(
            normalized_market_id,
            "order_management",
            f"{normalized_market_id} does not support order-management operation "
            f"{normalized_operation or '<empty>'}. Supported operations: {', '.join(supported) or 'none'}.",
        )
    if not isinstance(payload, Mapping):
        raise ValueError("Order-management payload must be a JSON object.")
    if (
        normalized_market_id == "kalshi"
        and normalized_operation == "decrease_order"
        and payload.get("reduce_by") not in (None, "")
    ):
        raise ValueError(
            "Kalshi reduce_by is disabled through the web API because stale quantities can over-reduce a live order; use an explicit reduce_to value."
        )
    kwargs: Dict[str, Any] = {
        "market_id": str(payload.get("market_id") or payload.get("exchange_market_id") or "").strip(),
        "instructions": payload.get("instructions"),
        "customer_ref": str(payload.get("customer_ref") or payload.get("customerRef") or "").strip(),
        "market_version": payload.get("market_version", payload.get("marketVersion")),
        "async_request": bool_from_setting(payload.get("async_request", payload.get("async")), False),
        "confirm_global_cancel": str(payload.get("confirm_global_cancel") or "").strip(),
    }
    market_slug = str(payload.get("market_slug") or payload.get("marketSlug") or "").strip()
    if market_slug:
        kwargs["market_slug"] = market_slug
    if normalized_market_id == "polymarket":
        kwargs.update(
            {
                "contract_id": str(payload.get("contract_id") or "").strip(),
                "asset_id": str(payload.get("asset_id") or "").strip(),
            }
        )
    for key in (
        "order_id",
        "external_id",
        "order_ids",
        "token_id",
        "token_ids",
        "event_id",
        "offer_id",
        "offer_ids",
        "event_ids",
        "market_ids",
        "runner_ids",
        "current_odds",
        "new_odds",
        "current_stake",
        "new_stake",
        "ticker",
        "side",
        "price",
        "count",
        "client_order_id",
        "updated_client_order_id",
        "reduce_by",
        "reduce_to",
        "order_hash",
        "order_hashes",
        "trader",
        "timestamp",
        "nonce",
        "signature",
        "signature_type",
        "network_id",
        "allow_partial",
        "cancel",
        "place",
        "subaccount",
        "exchange_index",
        "orders",
        "signed_action",
        "signed_cancel",
        "confirm_order_management",
        "manual_indicator",
        "external_operator",
    ):
        if key in payload:
            kwargs[key] = payload.get(key)
    data = adapter.manage_orders(normalized_operation, **kwargs)
    return {
        "market_id": normalized_market_id,
        "operation": normalized_operation,
        "parameters": kwargs,
        "data": data,
    }


def paper_quote_payload(cfg: AppConfig, registry: AdapterRegistry, payload: Mapping[str, Any]) -> Dict[str, Any]:
    market_id = str(payload.get("market_id") or "").strip().lower()
    contract_id = str(payload.get("contract_id") or "").strip()
    if not market_id:
        raise ValueError("market_id is required.")
    if not contract_id:
        raise ValueError("contract_id is required.")
    require_market_enabled(cfg, market_id, "paper quote")
    adapter = adapter_for_market(cfg, market_id, registry)
    if not (adapter.capabilities.price_reading or adapter.capabilities.orderbook_reading):
        raise UnsupportedFeatureError(market_id, "price_reading", f"{adapter.display_name} does not support quote previews.")
    snapshot = adapter.get_price(contract_id) if adapter.capabilities.price_reading else None
    orderbook = adapter.get_orderbook(contract_id) if adapter.capabilities.orderbook_reading else None
    price = serialize_price_snapshot(snapshot)
    book = serialize_orderbook(orderbook)
    best_bid = (book or {}).get("best_bid")
    best_ask = (book or {}).get("best_ask")
    if price:
        best_bid = best_bid if best_bid is not None else price.get("bid")
        best_ask = best_ask if best_ask is not None else price.get("ask")
    return {
        "market_id": market_id,
        "contract_id": contract_id,
        "display_name": adapter.display_name,
        "price": price,
        "orderbook": book,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "suggested_limits": {
            "BUY": best_ask,
            "BACK": best_ask,
            "SELL": best_bid,
            "LAY": best_bid,
        },
        "message": f"Quote: {adapter.display_name} {contract_id}",
    }


def paper_quote_limit_payload(cfg: AppConfig, registry: AdapterRegistry, payload: Mapping[str, Any]) -> Dict[str, Any]:
    side = str(payload.get("side") or "").strip().upper()
    if side not in {"BUY", "SELL", "BACK", "LAY"}:
        raise ValueError("side must be BUY, SELL, BACK, or LAY.")
    quote = paper_quote_payload(cfg, registry, payload)
    source = "best_ask" if side in {"BUY", "BACK"} else "best_bid"
    limit_price = quote["best_ask"] if source == "best_ask" else quote["best_bid"]
    if limit_price is None:
        raise ValueError(f"No {source} is available for {side}.")
    return {
        "market_id": quote["market_id"],
        "contract_id": quote["contract_id"],
        "side": side,
        "limit_price": float(limit_price),
        "source": source,
        "quote": quote,
        "message": f"Loaded {source} limit {float(limit_price):g} for {side}.",
    }


def record_paper_trade(cfg: AppConfig, order: PaperOrderRequest, result: PaperOrderResult) -> PaperTradeRecord:
    record = PaperTradeRecord(
        market_id=order.market_id,
        contract_id=order.contract_id,
        side=order.side,
        size=order.size,
        limit_price=order.limit_price,
        accepted=result.accepted,
        message=result.message,
        filled_size=result.filled_size,
        average_price=result.average_price,
        raw={"request": order.metadata, "result": result.raw},
    )
    cfg.paper_trades.insert(0, record)
    if len(cfg.paper_trades) > 200:
        cfg.paper_trades = cfg.paper_trades[:200]
    return record


def submit_paper_order(cfg: AppConfig, registry: AdapterRegistry, payload: Mapping[str, Any]) -> Dict[str, Any]:
    order = paper_order_from_payload(payload)
    require_market_enabled(cfg, order.market_id, "paper trading")
    adapter = adapter_for_market(cfg, order.market_id, registry)
    if not adapter.capabilities.paper_trading:
        raise UnsupportedFeatureError(order.market_id, "paper_trading")
    result = adapter.place_paper_order(order)
    record = record_paper_trade(cfg, order, result)
    return {
        "order": {
            "market_id": order.market_id,
            "contract_id": order.contract_id,
            "side": order.side,
            "size": order.size,
            "limit_price": order.limit_price,
        },
        "result": {
            "market_id": result.market_id,
            "contract_id": result.contract_id,
            "accepted": result.accepted,
            "message": result.message,
            "filled_size": result.filled_size,
            "average_price": result.average_price,
        },
        "record": record.to_dict(),
    }


def history_refill_payload(cfg: AppConfig, record_id: str) -> Dict[str, Any]:
    for record in cfg.paper_trades:
        if record.id == record_id:
            return {
                "market_id": record.market_id,
                "contract_id": record.contract_id,
                "side": record.side,
                "size": record.size,
                "limit_price": record.limit_price,
                "message": f"Loaded paper history order: {record.market_id}:{record.contract_id} {record.side} size={record.size:g}",
            }
    raise ValueError("Paper history record was not found.")


def position_close_side(records: List[PaperTradeRecord], market_id: str, contract_id: str, net_size: float) -> str:
    matching_sides = [
        str(record.side or "").upper()
        for record in records
        if record.accepted and record.market_id == market_id and record.contract_id == contract_id
    ]
    uses_back_lay = any(side in {"BACK", "LAY"} for side in matching_sides)
    uses_buy_sell = any(side in {"BUY", "SELL"} for side in matching_sides)
    if uses_back_lay and not uses_buy_sell:
        return "LAY" if net_size > 0 else "BACK"
    return "SELL" if net_size > 0 else "BUY"


def position_refill_payload(cfg: AppConfig, market_id: str, contract_id: str) -> Dict[str, Any]:
    normalized = str(market_id or "").strip().lower()
    contract = str(contract_id or "").strip()
    row = next(
        (
            item
            for item in paper_position_rows(cfg.paper_trades)
            if item["market_id"] == normalized and item["contract_id"] == contract
        ),
        None,
    )
    if not row:
        raise ValueError("Paper position was not found.")
    net_size = float(row["net_size"])
    side = position_close_side(cfg.paper_trades, normalized, contract, net_size)
    return {
        "market_id": normalized,
        "contract_id": contract,
        "side": side,
        "size": abs(net_size),
        "limit_price": None,
        "message": f"Loaded paper position: {normalized}:{contract} {side} size={abs(net_size):g}; limit cleared.",
    }


def refresh_paper_marks(
    cfg: AppConfig,
    registry: AdapterRegistry,
    rows: List[Dict[str, Any]],
    existing_marks: Optional[Mapping[Tuple[str, str], Dict[str, Any]]] = None,
) -> Tuple[Dict[Tuple[str, str], Dict[str, Any]], List[str]]:
    marks: Dict[Tuple[str, str], Dict[str, Any]] = _paper_marks_for_rows(existing_marks or {}, rows)
    problems: List[str] = []
    marked_at = int(time.time())
    for row in rows:
        market_id = str(row["market_id"])
        contract_id = str(row["contract_id"])
        if not cfg.markets.get(market_id) or not cfg.markets[market_id].enabled:
            problems.append(f"{market_id}: disabled")
            continue
        try:
            adapter = adapter_for_market(cfg, market_id, registry)
            if not adapter.capabilities.price_reading:
                problems.append(f"{adapter.display_name}: no price feed")
                continue
            snapshot = adapter.get_price(contract_id)
            mark_price, source = _paper_position_mark_price(snapshot, float(row["net_size"]))
            marks[(market_id, contract_id)] = {
                "mark_price": mark_price,
                "source": source,
                "marked_at": marked_at,
            }
        except Exception as exc:
            problems.append(f"{market_id}:{contract_id}: {exc}")
    return _paper_marks_for_rows(marks, rows), problems


def refresh_selected_paper_mark(
    cfg: AppConfig,
    registry: AdapterRegistry,
    market_id: str,
    contract_id: str,
    marks: Mapping[Tuple[str, str], Dict[str, Any]],
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    normalized = str(market_id or "").strip().lower()
    contract = str(contract_id or "").strip()
    rows = paper_position_rows(cfg.paper_trades)
    row = next((item for item in rows if item["market_id"] == normalized and item["contract_id"] == contract), None)
    if not row:
        raise ValueError("Paper position was not found.")
    require_market_enabled(cfg, normalized, "paper mark refresh")
    adapter = adapter_for_market(cfg, normalized, registry)
    if not adapter.capabilities.price_reading:
        raise UnsupportedFeatureError(normalized, "price_reading", f"{adapter.display_name} does not support paper mark refresh.")
    snapshot = adapter.get_price(contract)
    mark_price, source = _paper_position_mark_price(snapshot, float(row["net_size"]))
    updated = _paper_marks_for_rows(marks, rows)
    updated[(normalized, contract)] = {
        "mark_price": mark_price,
        "source": source,
        "marked_at": int(time.time()),
    }
    return updated


class AuthFailureLimiter:
    """Bound repeated invalid API-token attempts without retaining unbounded client state."""

    def __init__(
        self,
        *,
        max_attempts: int = AUTH_FAILURE_MAX_ATTEMPTS,
        window_seconds: float = AUTH_FAILURE_WINDOW_SECONDS,
        max_clients: int = MAX_TRACKED_AUTH_FAILURE_CLIENTS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_attempts = max(1, int(max_attempts))
        self.window_seconds = max(1.0, float(window_seconds))
        self.max_clients = max(1, int(max_clients))
        self.clock = clock
        self._attempts: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    def record_failure(self, client_id: str) -> Optional[int]:
        """Record one failed attempt and return the required retry delay, if blocked."""
        now = self.clock()
        key = str(client_id or "unknown")[:256]
        with self._lock:
            attempts = [
                timestamp
                for timestamp in self._attempts.get(key, [])
                if now - timestamp < self.window_seconds
            ]
            if len(attempts) >= self.max_attempts:
                self._attempts[key] = attempts
                remaining = self.window_seconds - (now - attempts[0])
                return max(1, int(remaining + 0.999))
            if key not in self._attempts and len(self._attempts) >= self.max_clients:
                oldest_key = min(self._attempts, key=lambda item: self._attempts[item][-1] if self._attempts[item] else now)
                self._attempts.pop(oldest_key, None)
            attempts.append(now)
            self._attempts[key] = attempts
        return None

    def clear(self, client_id: str) -> None:
        with self._lock:
            self._attempts.pop(str(client_id or "unknown")[:256], None)


class ReactGuiServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False
    request_queue_size = 32

    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler_class: type[BaseHTTPRequestHandler],
        *,
        config_path: Path = DEFAULT_CONFIG_PATH,
        frontend_dir: Path = DEFAULT_FRONTEND_DIR,
        adapter_registry: Optional[AdapterRegistry] = None,
        api_token: str = "",
        allowed_origins: Optional[Sequence[str]] = None,
        max_http_workers: int = MAX_HTTP_WORKERS,
        max_mutation_workers: Optional[int] = None,
        mutation_lock_timeout_seconds: float = HTTP_MUTATION_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        bind_host = str(server_address[0]).strip()
        is_loopback = is_loopback_host(bind_host)
        token = str(api_token or "").strip()
        worker_limit = int(max_http_workers)
        if worker_limit < 1:
            raise ValueError("max_http_workers must be at least 1.")
        reserved_target = min(HTTP_RESERVED_READ_WORKERS, max(1, worker_limit // 4))
        default_mutation_limit = max(1, min(MAX_HTTP_MUTATION_WORKERS, worker_limit - reserved_target))
        mutation_limit = default_mutation_limit if max_mutation_workers is None else int(max_mutation_workers)
        if mutation_limit < 1:
            raise ValueError("max_mutation_workers must be at least 1.")
        if worker_limit > 1 and mutation_limit >= worker_limit:
            raise ValueError("max_mutation_workers must reserve at least one HTTP worker for reads.")
        if worker_limit == 1 and mutation_limit != 1:
            raise ValueError("A one-worker server supports exactly one mutation worker and cannot reserve read capacity.")
        mutation_lock_timeout = float(mutation_lock_timeout_seconds)
        if mutation_lock_timeout <= 0:
            raise ValueError("mutation_lock_timeout_seconds must be positive.")
        if not is_loopback and not token:
            raise ValueError("A non-loopback React GUI bind requires a non-empty API token.")
        trusted_frontend_dir = _resolve_trusted_frontend_dir(frontend_dir)
        if trusted_frontend_dir is None:
            raise ValueError(
                "The frontend directory must resolve beneath the deployment resource root. "
                f"Allowed root: {_RESOURCE_ROOT}"
            )
        runtime_identity = capture_runtime_identity(PROJECT_ROOT, trusted_frontend_dir)
        super().__init__(server_address, request_handler_class)
        self.bind_host = bind_host
        self.is_loopback = is_loopback
        self.api_token = token
        self.config_path = config_path
        self.frontend_dir = trusted_frontend_dir
        self.runtime_identity = runtime_identity
        # Static files are a deployment-time input. Build the immutable catalog
        # before serving requests so URL parsing never performs filesystem work.
        self.static_files = ReactGuiHandler._static_file_catalog(self.frontend_dir)
        self.adapter_registry = adapter_registry or build_default_registry()
        default_origins = {
            f"http://{self.bind_host}:{self.server_address[1]}",
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "http://127.0.0.1:4173",
            "http://localhost:4173",
        }
        self.allowed_origins = {
            normalized
            for origin in (allowed_origins or default_origins)
            if (normalized := _normalize_allowed_origin(origin))
        }
        self.paper_position_marks: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.alert_price_state: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.wallet_recent_activity: List[Dict[str, Any]] = []
        self.wallet_polling: Dict[str, Any] = {
            "poll_interval_seconds": 10.0,
            "last_polled_at": None,
            "last_message": "Not polled yet.",
        }
        self.http_metrics = HttpRequestMetrics(worker_limit, mutation_limit, max(0, worker_limit - mutation_limit))
        self.auth_failure_limiter = AuthFailureLimiter()
        self.max_http_workers = worker_limit
        self.max_mutation_workers = mutation_limit
        self.reserved_read_workers = max(0, worker_limit - mutation_limit)
        self.mutation_lock_timeout_seconds = mutation_lock_timeout
        self._worker_slots = threading.BoundedSemaphore(worker_limit)
        self._mutation_slots = threading.BoundedSemaphore(mutation_limit)
        self.mutation_lock = threading.RLock()
        self._draining = threading.Event()
        self._drain_condition = threading.Condition()
        self._admitted_mutations = 0
        self._readiness_cache_lock = threading.Lock()
        self._readiness_cache_at = 0.0
        self._readiness_cache: Dict[str, Any] = {}

    def try_admit_mutation(self) -> bool:
        """Bound mutation body readers and lock waiters below the HTTP worker pool."""
        if self._draining.is_set():
            return False
        if not self._mutation_slots.acquire(blocking=False):
            self.http_metrics.record_mutation_admission_rejection()
            return False
        with self._drain_condition:
            if self._draining.is_set():
                self._mutation_slots.release()
                return False
            self._admitted_mutations += 1
        self.http_metrics.mutation_admitted()
        return True

    def finish_mutation_admission(self) -> None:
        self.http_metrics.mutation_finished()
        with self._drain_condition:
            self._admitted_mutations = max(0, self._admitted_mutations - 1)
            if self._admitted_mutations == 0:
                self._drain_condition.notify_all()
        self._mutation_slots.release()

    @property
    def is_draining(self) -> bool:
        return self._draining.is_set()

    def begin_drain(self) -> bool:
        """Stop admitting mutations and report whether this call began the drain."""

        with self._drain_condition:
            already_draining = self._draining.is_set()
            self._draining.set()
            return not already_draining

    def wait_for_mutation_drain(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        with self._drain_condition:
            while self._admitted_mutations:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._drain_condition.wait(timeout=remaining)
            return True

    def readiness_snapshot(self) -> Dict[str, Any]:
        """Return cached local probes combined with current admission counters."""
        now = time.monotonic()
        with self._readiness_cache_lock:
            if not self._readiness_cache or now - self._readiness_cache_at >= LOCAL_READINESS_CACHE_SECONDS:
                self._readiness_cache = local_resource_readiness(self.config_path, self.frontend_dir)
                self._readiness_cache_at = now
            resources = dict(self._readiness_cache)
        metrics = self.http_metrics.snapshot()
        return runtime_readiness_payload(
            resources,
            {
                **metrics,
                "max_http_workers": self.max_http_workers,
                "max_mutation_workers": self.max_mutation_workers,
                "reserved_read_workers": self.reserved_read_workers,
                "mutation_lock_timeout_seconds": self.mutation_lock_timeout_seconds,
                "draining": self.is_draining,
            },
            self.wallet_polling,
        )

    def process_request(self, request: Any, client_address: Any) -> None:
        """Admit at most ``max_http_workers`` connections before creating threads."""
        if not self._worker_slots.acquire(blocking=False):
            self.http_metrics.record_overload_rejection()
            self._reject_overloaded_request(request)
            return
        self.http_metrics.request_started()
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.http_metrics.request_finished()
            self._worker_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.http_metrics.request_finished()
            self._worker_slots.release()

    def _reject_overloaded_request(self, request: Any) -> None:
        request_id = secrets.token_hex(12)
        data = json.dumps(
            api_error_payload(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "server_overloaded",
                "The HTTP server has reached its active request limit; retry shortly.",
                {"retry_after_seconds": HTTP_OVERLOAD_RETRY_AFTER_SECONDS},
            ),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        response = (
            b"HTTP/1.1 503 Service Unavailable\r\n"
            b"Content-Type: application/json; charset=utf-8\r\n"
            b"Cache-Control: no-store\r\n"
            b"Connection: close\r\n"
            b"Retry-After: "
            + str(HTTP_OVERLOAD_RETRY_AFTER_SECONDS).encode("ascii")
            + b"\r\nX-Content-Type-Options: nosniff\r\n"
            + b"X-Frame-Options: DENY\r\n"
            + b"X-Request-ID: "
            + request_id.encode("ascii")
            + b"\r\nContent-Length: "
            + str(len(data)).encode("ascii")
            + b"\r\n\r\n"
            + data
        )
        try:
            request.settimeout(float(HTTP_OVERLOAD_RETRY_AFTER_SECONDS))
            request.sendall(response)
        except OSError:
            pass
        finally:
            self.shutdown_request(request)


class HttpClientDisconnected(ConnectionError):
    """The response stream, not an upstream transport, lost its client."""


class _ClientResponseWriter:
    """Identify peer disconnects at the only stream that writes to the client."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._disconnected = False

    def _call(self, operation: Callable[..., Any], *args: Any) -> Any:
        if self._disconnected:
            raise HttpClientDisconnected("The HTTP client has disconnected.")
        try:
            return operation(*args)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as exc:
            self._disconnected = True
            raise HttpClientDisconnected("The HTTP client has disconnected.") from exc

    def write(self, data: Any) -> Any:
        return self._call(self._stream.write, data)

    def flush(self) -> None:
        self._call(self._stream.flush)

    @property
    def closed(self) -> bool:
        return self._stream.closed

    def close(self) -> None:
        self._stream.close()


class ReactGuiHandler(BaseHTTPRequestHandler):
    server_version = "PredictionMarketReactGui/0.1"
    _security_headers = (
        (
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
            "form-action 'self'; connect-src 'self'; img-src 'self' data:; script-src 'self'; style-src 'self'",
        ),
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "no-referrer"),
        ("Permissions-Policy", "camera=(), geolocation=(), microphone=(), payment=(), usb=()"),
        ("Cross-Origin-Opener-Policy", "same-origin"),
    )

    def setup(self) -> None:
        """Bound each client connection so incomplete requests cannot pin worker threads."""
        super().setup()
        self.wfile = _ClientResponseWriter(self.wfile)
        self.connection.settimeout(HTTP_CONNECTION_TIMEOUT_SECONDS)

    def version_string(self) -> str:
        """Do not disclose the Python runtime version in HTTP responses."""
        return "MarketSentinel"

    def end_headers(self) -> None:
        """Apply the browser baseline to every success, error, and preflight response."""
        for name, value in self._security_headers:
            self.send_header(name, value)
        super().end_headers()

    def handle_one_request(self) -> None:
        """Emit one safe structured log event and one aggregate metric per request."""
        started_at = time.monotonic()
        self._request_id = secrets.token_hex(12)
        self._response_status: Optional[int] = None
        client_disconnected = False
        try:
            super().handle_one_request()
        except HttpClientDisconnected:
            client_disconnected = True
            self.close_connection = True
        finally:
            method = str(getattr(self, "command", "") or "").upper()
            if method:
                status = self._response_status if self._response_status is not None else HTTPStatus.INTERNAL_SERVER_ERROR
                if client_disconnected:
                    # 499 is local accounting only; no second response is sent.
                    status = 499
                duration_seconds = time.monotonic() - started_at
                self.app_server.http_metrics.record(method, int(status), duration_seconds)
                raw_path = str(getattr(self, "path", "") or "")
                path = urlparse(raw_path).path[:512] or "/"
                event = {
                    "event": "http_request",
                    "request_id": self._request_id,
                    "method": method,
                    "path": path,
                    "status": int(status),
                    "duration_ms": round(max(0.0, duration_seconds) * 1000, 3),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                if client_disconnected:
                    event.update(outcome="client_disconnected", response_status=self._response_status)
                print(json.dumps(event, sort_keys=True, separators=(",", ":")), flush=True)

    def send_response(self, code: int, message: Optional[str] = None) -> None:
        self._response_status = int(code)
        super().send_response(code, message)
        request_id = getattr(self, "_request_id", "")
        if request_id:
            self.send_header("X-Request-ID", request_id)

    def do_OPTIONS(self) -> None:
        if not self._origin_is_allowed():
            self._send_error(HTTPStatus.FORBIDDEN, "cors_origin_forbidden", "Request origin is not allowed.")
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        if not self._require_authorized_request():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/metrics":
            self._send_text(
                HTTPStatus.OK,
                self.app_server.http_metrics.prometheus_text(),
                content_type="text/plain; version=0.0.4; charset=utf-8",
            )
            return
        if parsed.path.startswith("/api/"):
            self._handle_api_get(parsed.path, parsed.query)
            return
        self._serve_static(parsed.path)

    def do_PATCH(self) -> None:
        if not self._require_authorized_request():
            return
        self._handle_admitted_mutation("PATCH")

    def do_POST(self) -> None:
        if not self._require_authorized_request():
            return
        self._handle_admitted_mutation("POST")

    def do_DELETE(self) -> None:
        if not self._require_authorized_request():
            return
        self._handle_admitted_mutation("DELETE")

    def log_message(self, fmt: str, *args: Any) -> None:
        # handle_one_request emits a structured, query-string-free event instead.
        return None

    @property
    def app_server(self) -> ReactGuiServer:
        return self.server  # type: ignore[return-value]

    def _load_config(self) -> AppConfig:
        return load_config(self.app_server.config_path)

    def _save_config(self, cfg: AppConfig) -> None:
        save_config(cfg, self.app_server.config_path)

    def _origin_is_allowed(self) -> bool:
        origin = str(self.headers.get("Origin") or "").strip().rstrip("/")
        return not origin or origin in self.app_server.allowed_origins

    def _require_authorized_request(self) -> bool:
        if not self._origin_is_allowed():
            self._send_error(HTTPStatus.FORBIDDEN, "cors_origin_forbidden", "Request origin is not allowed.")
            return False
        expected = self.app_server.api_token
        if not expected:
            return True
        presented = str(self.headers.get("X-Market-Sentinel-Token") or "").strip()
        authorization = str(self.headers.get("Authorization") or "").strip()
        if authorization.lower().startswith("bearer "):
            presented = authorization[7:].strip()
        client_id = str(self.client_address[0] or "unknown")
        if hmac.compare_digest(presented, expected):
            self.app_server.auth_failure_limiter.clear(client_id)
            return True
        retry_after = self.app_server.auth_failure_limiter.record_failure(client_id)
        if retry_after is not None:
            self._send_error(
                HTTPStatus.TOO_MANY_REQUESTS,
                "api_token_rate_limited",
                "Too many invalid API token attempts; retry later.",
                {"retry_after_seconds": retry_after},
                retry_after_seconds=retry_after,
            )
            return False
        self._send_error(HTTPStatus.UNAUTHORIZED, "api_token_required", "A valid API token is required for this server.")
        return False

    def _handle_api_get(self, path: str, query: str = "") -> None:
        try:
            query_params = parse_qs(query, keep_blank_values=True)
            if path == "/api/health":
                readiness = self.app_server.readiness_snapshot()
                self._send_json(
                    HTTPStatus.OK,
                    health_payload(
                        self.app_server.config_path,
                        self.app_server.frontend_dir,
                        self.app_server.runtime_identity,
                        self.app_server.max_http_workers,
                        readiness,
                    ),
                )
                return
            cfg = self._load_config()
            if path == "/api/state":
                self._send_json(
                    HTTPStatus.OK,
                    app_state_payload(
                        cfg,
                        self.app_server.config_path,
                        self.app_server.frontend_dir,
                        self.app_server.paper_position_marks,
                        self.app_server.adapter_registry,
                        self.app_server.alert_price_state,
                        self.app_server.wallet_polling,
                        self.app_server.wallet_recent_activity,
                        self.app_server.runtime_identity,
                        self.app_server.max_http_workers,
                        self.app_server.readiness_snapshot(),
                    ),
                )
                return
            if path == "/api/config":
                self._send_json(HTTPStatus.OK, config_payload(cfg))
                return
            if path == "/api/mutations":
                self._send_json(HTTPStatus.OK, mutation_journal_payload(cfg))
                return
            if path == "/api/markets":
                self._send_json(HTTPStatus.OK, markets_payload(cfg, self.app_server.adapter_registry))
                return
            if path == "/api/markets/support-matrix":
                self._send_json(HTTPStatus.OK, market_support_payload(cfg, self.app_server.adapter_registry))
                return
            market_route = path.strip("/").split("/")
            if len(market_route) == 4 and market_route[:2] == ["api", "markets"]:
                market_id = unquote(market_route[2])
                if market_route[3] == "support":
                    self._send_json(
                        HTTPStatus.OK,
                        market_support_payload(cfg, self.app_server.adapter_registry, market_id),
                    )
                    return
                if market_route[3] == "events":
                    self._send_json(
                        HTTPStatus.OK,
                        market_events_payload(cfg, self.app_server.adapter_registry, market_id, query_params),
                    )
                    return
                if market_route[3] == "contracts":
                    self._send_json(
                        HTTPStatus.OK,
                        market_contracts_payload(cfg, self.app_server.adapter_registry, market_id, query_params),
                    )
                    return
                if market_route[3] == "price":
                    self._send_json(
                        HTTPStatus.OK,
                        market_price_payload(cfg, self.app_server.adapter_registry, market_id, query_params),
                    )
                    return
                if market_route[3] == "orderbook":
                    self._send_json(
                        HTTPStatus.OK,
                        market_orderbook_payload(cfg, self.app_server.adapter_registry, market_id, query_params),
                    )
                    return
                if market_route[3] == "trades":
                    self._send_json(
                        HTTPStatus.OK,
                        market_trades_payload(cfg, self.app_server.adapter_registry, market_id, query_params),
                    )
                    return
                if market_route[3] == "candles":
                    self._send_json(
                        HTTPStatus.OK,
                        market_candles_payload(cfg, self.app_server.adapter_registry, market_id, query_params),
                    )
                    return
            if len(market_route) == 5 and market_route[:2] == ["api", "markets"] and market_route[3] == "account":
                market_id = unquote(market_route[2])
                operation = unquote(market_route[4])
                self._send_json(
                    HTTPStatus.OK,
                    market_account_payload(
                        cfg,
                        self.app_server.adapter_registry,
                        market_id,
                        operation,
                        query_params,
                    ),
                )
                return
            if path == "/api/alerts":
                self._send_json(HTTPStatus.OK, alerts_payload(cfg, self.app_server.adapter_registry, self.app_server.alert_price_state))
                return
            if path == "/api/wallets":
                self._send_json(HTTPStatus.OK, wallets_payload(cfg, self.app_server.wallet_polling, self.app_server.wallet_recent_activity))
                return
            if path == "/api/copy":
                self._send_json(HTTPStatus.OK, copy_payload(cfg, self.app_server.adapter_registry))
                return
            if path == "/api/live-safety":
                self._send_json(HTTPStatus.OK, live_safety_payload(cfg, self.app_server.adapter_registry))
                return
            if path == "/api/paper":
                self._send_json(HTTPStatus.OK, paper_payload(cfg, self.app_server.paper_position_marks))
                return
            if path == "/api/paper/history":
                self._send_json(HTTPStatus.OK, {"history": paper_payload(cfg, self.app_server.paper_position_marks)["history"]})
                return
            if path == "/api/paper/positions":
                paper = paper_payload(cfg, self.app_server.paper_position_marks)
                self._send_json(HTTPStatus.OK, {"summary": paper["summary"], "positions": paper["positions"]})
                return
            if path == "/api/polymarket/users/search":
                self._send_json(
                    HTTPStatus.OK,
                    polymarket_user_search_payload(
                        _query_value(query_params, "q"),
                        _clamp_int(_query_value(query_params, "limit", "10"), 10, 1, 50),
                    ),
                )
                return
            if path == "/api/polymarket/users/leaderboard":
                self._send_json(HTTPStatus.OK, polymarket_leaderboard_payload(query_params))
                return
            if path == "/api/polymarket/users/mdd/cache/health":
                self._send_json(HTTPStatus.OK, polymarket_mdd_cache_health_payload())
                return
            if path == "/api/polymarket/users/mdd/cache":
                self._send_json(
                    HTTPStatus.OK,
                    polymarket_mdd_cache_payload(include_expired=_query_bool(query_params, "include_expired", True)),
                )
                return
            if path == "/api/polymarket/users/mdd/export.json":
                self._send_json(HTTPStatus.OK, polymarket_mdd_export_payload(_query_value(query_params, "key")))
                return
            if path == "/api/polymarket/users/mdd/export.csv":
                export = polymarket_mdd_export_csv(_query_value(query_params, "key"))
                self._send_text(
                    HTTPStatus.OK,
                    export["csv"],
                    content_type="text/csv; charset=utf-8",
                    filename=export["filename"],
                )
                return
            if path == "/api/polymarket/users/mdd":
                wallet = _query_value(query_params, "user") or _query_value(query_params, "wallet")
                mdd_options = {
                    "mode": _query_value(query_params, "mode", _query_value(query_params, "mdd_mode", "fast")),
                    "closed_limit": _clamp_int(_query_value(query_params, "closed_limit", "500"), 500, 1, 1000),
                    "open_limit": _clamp_int(_query_value(query_params, "open_limit", "500"), 500, 0, 1000),
                    "activity_limit": _clamp_int(_query_value(query_params, "activity_limit", "1000"), 1000, 0, 5000),
                    "trade_limit": _clamp_int(_query_value(query_params, "trade_limit", "1000"), 1000, 0, 5000),
                    "include_open": _query_bool(query_params, "include_open", True),
                    "equity_base_usd": _query_float(query_params, "equity_base_usd"),
                    "max_points": _clamp_int(_query_value(query_params, "max_points", "50"), 50, 1, 1000),
                    "cache_ttl_seconds": _clamp_int(_query_value(query_params, "cache_ttl_seconds", "0"), 0, 0, 300),
                    "mark_replay_token_limit": _clamp_int(_query_value(query_params, "mark_replay_token_limit", "10"), 10, 1, 20),
                    "mark_replay_point_limit": _clamp_int(_query_value(query_params, "mark_replay_point_limit", "5000"), 5000, 1, 10000),
                    "mark_replay_interval": _query_value(query_params, "mark_replay_interval", "1h") or "1h",
                    "mark_replay_fidelity": _clamp_int(_query_value(query_params, "mark_replay_fidelity", "60"), 60, 1, 1440),
                    "mark_replay_start_ts": _safe_int(_query_value(query_params, "mark_replay_start_ts"), None)
                    if _query_value(query_params, "mark_replay_start_ts")
                    else None,
                    "mark_replay_end_ts": _safe_int(_query_value(query_params, "mark_replay_end_ts"), None)
                    if _query_value(query_params, "mark_replay_end_ts")
                    else None,
                    "include_accounting_snapshot": _query_bool(query_params, "include_accounting_snapshot", False),
                    "accounting_timeout": float(_clamp_int(_query_value(query_params, "accounting_timeout", "30"), 30, 1, 60)),
                }
                payload = polymarket_user_mdd_payload(wallet, **mdd_options)
                attach_polymarket_mdd_audit_cache(
                    payload,
                    polymarket_mdd_audit_params(wallet, mdd_options),
                    enabled=_query_bool(query_params, "persist_cache", _query_bool(query_params, "mdd_persist_cache", False)),
                )
                self._send_json(HTTPStatus.OK, payload)
                return
            if path == "/api/polymarket/coverage":
                coverage = polymarket_official_api_coverage()
                coverage["stored_live_validation_report_promotion"] = live_validation_report_promotion_inventory()
                self._send_json(HTTPStatus.OK, coverage)
                return
            if path == "/api/polymarket/clob-readiness":
                self._send_json(HTTPStatus.OK, polymarket_clob_readiness_payload(cfg))
                return
            if path == "/api/polymarket/live-validation/reports":
                self._send_json(
                    HTTPStatus.OK,
                    polymarket_live_validation_reports_payload(
                        include_payload=_query_bool(query_params, "include_payload", False)
                    ),
                )
                return
            if path == "/api/polymarket/live-validation/decisions":
                self._send_json(HTTPStatus.OK, polymarket_live_validation_decisions_payload(query_params))
                return
            if path == "/api/polymarket/live-validation/decisions/export.json":
                ledger = polymarket_live_validation_decisions_payload(query_params)
                self._send_text(
                    HTTPStatus.OK,
                    json.dumps(ledger, indent=2, sort_keys=True),
                    content_type="application/json; charset=utf-8",
                    filename="polymarket-live-validation-decision-ledger.json",
                )
                return
            if path == "/api/polymarket/live-validation/decisions/export.md":
                ledger = polymarket_live_validation_decisions_payload(query_params)
                self._send_text(
                    HTTPStatus.OK,
                    live_validation_report_decisions_markdown(ledger),
                    content_type="text/markdown; charset=utf-8",
                    filename="polymarket-live-validation-decision-ledger.md",
                )
                return
            if path == "/api/polymarket/live-validation/promotion-proposal":
                self._send_json(HTTPStatus.OK, polymarket_live_validation_promotion_proposal_payload(query_params))
                return
            if path == "/api/polymarket/live-validation/promotion-proposal/export.json":
                proposal = polymarket_live_validation_promotion_proposal_payload(query_params)
                self._send_text(
                    HTTPStatus.OK,
                    json.dumps(proposal, indent=2, sort_keys=True),
                    content_type="application/json; charset=utf-8",
                    filename=live_validation_coverage_promotion_proposal_export_filename("json"),
                )
                return
            if path == "/api/polymarket/live-validation/promotion-proposal/export.md":
                proposal = polymarket_live_validation_promotion_proposal_payload(query_params)
                self._send_text(
                    HTTPStatus.OK,
                    live_validation_coverage_promotion_proposal_markdown(proposal),
                    content_type="text/markdown; charset=utf-8",
                    filename=live_validation_coverage_promotion_proposal_export_filename("md"),
                )
                return
            if path == "/api/polymarket/live-validation/promotion-proposal/snapshots":
                self._send_json(HTTPStatus.OK, polymarket_live_validation_promotion_proposal_snapshots_payload())
                return
            if path.startswith("/api/polymarket/live-validation/promotion-proposal/snapshots/"):
                suffix = path[len("/api/polymarket/live-validation/promotion-proposal/snapshots/") :]
                diff_json = suffix.endswith("/diff.json")
                diff_markdown = suffix.endswith("/diff.md")
                export_json = suffix.endswith("/export.json")
                export_markdown = suffix.endswith("/export.md")
                if diff_json:
                    raw_key = suffix[: -len("/diff.json")]
                elif diff_markdown:
                    raw_key = suffix[: -len("/diff.md")]
                elif export_json:
                    raw_key = suffix[: -len("/export.json")]
                elif export_markdown:
                    raw_key = suffix[: -len("/export.md")]
                else:
                    raw_key = suffix
                snapshot_key = unquote(raw_key.strip("/"))
                if not snapshot_key or "/" in snapshot_key:
                    self._send_error(HTTPStatus.NOT_FOUND, "not_found", "Unknown promotion proposal snapshot.")
                    return
                snapshot = polymarket_live_validation_promotion_proposal_snapshot_payload(snapshot_key)
                if snapshot is None:
                    self._send_error(HTTPStatus.NOT_FOUND, "not_found", "Unknown promotion proposal snapshot.")
                    return
                if diff_json:
                    self._send_text(
                        HTTPStatus.OK,
                        json.dumps(snapshot.get("diff") or {}, indent=2, sort_keys=True),
                        content_type="application/json; charset=utf-8",
                        filename=live_validation_promotion_proposal_snapshot_export_filename(snapshot_key, "diff.json"),
                    )
                elif diff_markdown:
                    self._send_text(
                        HTTPStatus.OK,
                        live_validation_promotion_proposal_snapshot_diff_markdown(snapshot.get("diff") or {}),
                        content_type="text/markdown; charset=utf-8",
                        filename=live_validation_promotion_proposal_snapshot_export_filename(snapshot_key, "diff.md"),
                    )
                elif export_json:
                    self._send_text(
                        HTTPStatus.OK,
                        json.dumps(snapshot, indent=2, sort_keys=True),
                        content_type="application/json; charset=utf-8",
                        filename=live_validation_promotion_proposal_snapshot_export_filename(snapshot_key, "json"),
                    )
                elif export_markdown:
                    self._send_text(
                        HTTPStatus.OK,
                        live_validation_promotion_proposal_snapshot_markdown(snapshot),
                        content_type="text/markdown; charset=utf-8",
                        filename=live_validation_promotion_proposal_snapshot_export_filename(snapshot_key, "md"),
                    )
                else:
                    self._send_json(HTTPStatus.OK, snapshot)
                return
            if path.startswith("/api/polymarket/live-validation/reports/"):
                suffix = path[len("/api/polymarket/live-validation/reports/") :]
                export_json = suffix.endswith("/export.json")
                review_json = suffix.endswith("/review.json")
                review_markdown = suffix.endswith("/review.md")
                if export_json:
                    raw_key = suffix[: -len("/export.json")]
                elif review_json:
                    raw_key = suffix[: -len("/review.json")]
                elif review_markdown:
                    raw_key = suffix[: -len("/review.md")]
                else:
                    raw_key = suffix
                report_key = unquote(raw_key.strip("/"))
                if not report_key or "/" in report_key:
                    self._send_error(HTTPStatus.NOT_FOUND, "not_found", "Unknown live validation report.")
                    return
                if review_json or review_markdown:
                    review = polymarket_live_validation_report_review_payload(report_key)
                    if review is None:
                        self._send_error(HTTPStatus.NOT_FOUND, "not_found", "Unknown live validation report.")
                        return
                    if review_json:
                        self._send_text(
                            HTTPStatus.OK,
                            json.dumps(review, indent=2, sort_keys=True),
                            content_type="application/json; charset=utf-8",
                            filename=str(review["export"]["json_filename"]),
                        )
                    else:
                        self._send_text(
                            HTTPStatus.OK,
                            live_validation_report_review_markdown(review["bundle"]),
                            content_type="text/markdown; charset=utf-8",
                            filename=str(review["export"]["markdown_filename"]),
                        )
                    return
                report = polymarket_live_validation_report_payload(report_key)
                if report is None:
                    self._send_error(HTTPStatus.NOT_FOUND, "not_found", "Unknown live validation report.")
                    return
                if export_json:
                    self._send_text(
                        HTTPStatus.OK,
                        json.dumps(report, indent=2, sort_keys=True),
                        content_type="application/json; charset=utf-8",
                        filename=str(report["export"]["filename"]),
                    )
                else:
                    self._send_json(HTTPStatus.OK, report)
                return
            if path == "/api/polymarket/live-validation":
                self._send_json(HTTPStatus.OK, polymarket_live_validation_payload(cfg))
                return
            self._send_error(HTTPStatus.NOT_FOUND, "not_found", "Unknown API route.")
        except PolymarketRateLimitError as exc:
            self._send_error(
                HTTPStatus.TOO_MANY_REQUESTS,
                "polymarket_rate_limited",
                "Polymarket API rate limit was reached; retry after the upstream backoff window.",
                {"rate_limit": polymarket_rate_limit_status(exc)},
            )
        except LiveValidationReportSchemaError as exc:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "live_validation_report_schema_error",
                "Live validation report failed schema validation.",
                {"schema_validation": exc.validation},
            )
        except ValueError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, "validation_error", str(exc))
        except HttpClientDisconnected:
            raise
        except Exception as exc:
            print(f"[web-gui] internal error while handling GET {path}: {type(exc).__name__}")
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", "Internal server error.")

    def _handle_admitted_mutation(self, method: str) -> None:
        """Bound body parsing and lock wait without weakening mutation serialization."""
        if not self.app_server.try_admit_mutation():
            _discard_available_request_body(self)
            draining = self.app_server.is_draining
            self._send_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "server_draining" if draining else "mutation_admission_saturated",
                (
                    "The server is draining and is not accepting new mutations."
                    if draining
                    else "The mutation admission limit is saturated; read and health capacity remains reserved."
                ),
                {"retry_after_seconds": HTTP_OVERLOAD_RETRY_AFTER_SECONDS},
                retry_after_seconds=HTTP_OVERLOAD_RETRY_AFTER_SECONDS,
            )
            return
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            if not path.startswith("/api/"):
                self._send_error(HTTPStatus.NOT_FOUND, "not_found", "Unknown route.")
                return
            try:
                payload = _read_json_body(self)
                idempotency_key = _validated_idempotency_key(self.headers, payload)
                specialized_idempotency = method == "POST" and path in IDEMPOTENT_MUTATION_ROUTES
                durable_mutation = _is_general_durable_mutation(method, path)
                if specialized_idempotency and not idempotency_key:
                    raise ValueError("Idempotency-Key is required for this durable create route.")
                if durable_mutation and not str(self.headers.get("Idempotency-Key") or ""):
                    raise ValueError("Idempotency-Key header is required for this durable mutation route.")
                if idempotency_key and not (specialized_idempotency or durable_mutation):
                    raise ValueError("Idempotency-Key is not supported for this mutation route.")
            except json.JSONDecodeError:
                self._send_error(HTTPStatus.BAD_REQUEST, "invalid_json", "Invalid JSON request body.")
                return
            except ValueError as exc:
                self._send_error(HTTPStatus.BAD_REQUEST, "validation_error", str(exc))
                return

            acquired = self.app_server.mutation_lock.acquire(
                timeout=self.app_server.mutation_lock_timeout_seconds
            )
            if not acquired:
                self.app_server.http_metrics.record_mutation_lock_timeout()
                self._send_error(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "mutation_busy",
                    "Another mutation is still running; retry rather than waiting indefinitely.",
                    {"retry_after_seconds": HTTP_OVERLOAD_RETRY_AFTER_SECONDS},
                    retry_after_seconds=HTTP_OVERLOAD_RETRY_AFTER_SECONDS,
                )
                return
            self.app_server.http_metrics.mutation_started()
            try:
                self._handle_mutation(method, path, payload, idempotency_key)
            finally:
                self.app_server.http_metrics.mutation_stopped()
                self.app_server.mutation_lock.release()
        finally:
            self.app_server.finish_mutation_admission()

    def _prepare_general_idempotent_mutation(
        self,
        cfg: AppConfig,
        method: str,
        path: str,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> tuple[Optional[MutationJournalEntry], bool]:
        """Resolve replay/conflict state and persist the live dispatch barrier."""

        live = _is_live_order_management_route(method, path)
        route_parts = str(path or "").strip("/").split("/")
        if (
            live
            and len(route_parts) == 5
            and unquote(route_parts[2]).strip().lower() == "kalshi"
            and unquote(route_parts[4]).strip().lower() == "decrease_order"
            and payload.get("reduce_by") not in (None, "")
        ):
            raise ValueError(
                "Kalshi reduce_by is disabled through the web API because stale quantities can over-reduce a live order; use an explicit reduce_to value."
            )
        if live and len(route_parts) == 5:
            market_id = unquote(route_parts[2]).strip().lower()
            operation = unquote(route_parts[4]).strip().lower()
            require_market_enabled(cfg, market_id, "order management")
            adapter = adapter_for_market(cfg, market_id, self.app_server.adapter_registry)
            supported = tuple(
                str(value).strip().lower()
                for value in getattr(adapter, "order_management_operations", ())
            )
            if operation not in supported:
                raise UnsupportedFeatureError(
                    market_id,
                    "order_management",
                    f"{market_id} does not support order-management operation "
                    f"{operation or '<empty>'}. Supported operations: {', '.join(supported) or 'none'}.",
                )
        entry = _mutation_journal_entry(cfg, idempotency_key)
        if entry is not None:
            _assert_mutation_request_matches(entry, method, path, payload)
            if entry.state == "completed":
                self._send_json(entry.response_status or HTTPStatus.OK, dict(entry.response))
                return None, True
            if entry.state == "rejected":
                if (
                    entry.outcome_code == "pre_dispatch_validation_failed"
                    and entry.response_status
                    and isinstance(entry.response, dict)
                ):
                    self._send_json(entry.response_status, dict(entry.response))
                    return None, True
                self._send_error(
                    HTTPStatus.CONFLICT,
                    "mutation_rejected_after_reconciliation",
                    "The reconciled mutation was discarded and cannot be retried with this Idempotency-Key.",
                    {"mutation_id": entry.id, "state": entry.state},
                )
                return None, True
            if live and entry.state == "retryable" and entry.replay_authorized_at:
                entry.state = "pending"
                entry.updated_at = int(time.time())
                entry.replay_authorized_at = 0
                entry.outcome_code = "manual_retry_started"
                entry.outcome_message = "The operator-authorized retry is in progress."
                self._save_config(cfg)
                return entry, False
            self._send_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "live_mutation_reconciliation_required" if entry.live else "mutation_reconciliation_required",
                (
                    "The previous live dispatch outcome is unresolved; inspect venue history and reconcile it before retrying."
                    if entry.live
                    else "The previous durable mutation outcome is unresolved and cannot be run again automatically."
                ),
                {
                    "mutation_id": entry.id,
                    "state": entry.state,
                    "reconciliation_route": "/api/mutations/reconcile" if entry.live else None,
                },
                retry_after_seconds=HTTP_OVERLOAD_RETRY_AFTER_SECONDS,
            )
            return None, True

        entry = _new_mutation_journal_entry(
            idempotency_key,
            method,
            path,
            payload,
            live=live,
        )
        if live:
            cfg.append_mutation_journal(entry)
            self._save_config(cfg)
        return entry, False

    def _commit_local_idempotent_mutation(
        self,
        cfg: AppConfig,
        entry: MutationJournalEntry,
        response: Mapping[str, Any],
        *,
        status: int = HTTPStatus.OK,
        preserve_response_shape: bool = False,
        client_response: Optional[Mapping[str, Any]] = None,
    ) -> None:
        stored = _stored_mutation_result(response, preserve_shape=preserve_response_shape)
        entry.state = "completed"
        entry.response_status = int(status)
        entry.response = stored
        entry.outcome_code = "committed"
        entry.outcome_message = "The local mutation and replay result were committed atomically."
        entry.updated_at = int(time.time())
        cfg.append_mutation_journal(entry)
        self._save_config(cfg)
        self._send_json(
            status,
            _client_mutation_result(client_response) if client_response is not None else stored,
        )

    def _execute_live_order_management(
        self,
        cfg: AppConfig,
        entry: MutationJournalEntry,
        market_id: str,
        operation: str,
        payload: Mapping[str, Any],
    ) -> None:
        """Execute once behind a durable pending barrier; never auto-retry ambiguity."""

        try:
            result = market_order_management_payload(
                cfg,
                self.app_server.adapter_registry,
                market_id,
                operation,
                payload,
            )
        except (MarketConfigurationError, UnsupportedFeatureError) as exc:
            # The supported live adapters use these explicit exception types
            # for validation that completes before their transport call.  Keep
            # generic exceptions (including a plain ValueError from a client or
            # response parser) ambiguous: once adapter code has been entered,
            # the caller cannot prove that the venue was not reached.
            rejection = api_error_payload(
                HTTPStatus.BAD_REQUEST,
                "validation_error",
                str(exc),
                {"mutation_id": entry.id, "state": "rejected"},
            )
            entry.state = "rejected"
            entry.response_status = HTTPStatus.BAD_REQUEST
            entry.response = rejection
            entry.outcome_code = "pre_dispatch_validation_failed"
            entry.outcome_message = "Local validation failed before the live adapter was dispatched."
            entry.updated_at = int(time.time())
            try:
                self._save_config(cfg)
            except Exception as durability_exc:
                # The durable pending barrier may still be on disk.  Keep the
                # response fail-closed until that disposition can be saved.
                print(
                    "[web-gui] pre-dispatch rejection could not be durably updated: "
                    f"{type(durability_exc).__name__}"
                )
                self._send_error(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "live_mutation_durability_uncertain",
                    "Local validation failed, but its replay disposition was not durably committed. Reconcile before retrying.",
                    {
                        "mutation_id": entry.id,
                        "state": "pending",
                        "reconciliation_route": "/api/mutations/reconcile",
                    },
                    retry_after_seconds=HTTP_OVERLOAD_RETRY_AFTER_SECONDS,
                )
                return
            self._send_json(HTTPStatus.BAD_REQUEST, rejection)
            return
        except Exception as exc:
            entry.state = "ambiguous"
            entry.response_status = HTTPStatus.SERVICE_UNAVAILABLE
            entry.response = {}
            entry.outcome_code = "live_dispatch_outcome_unknown"
            entry.outcome_message = (
                "The live adapter returned without a provable non-dispatch outcome; venue reconciliation is required."
            )
            entry.updated_at = int(time.time())
            try:
                self._save_config(cfg)
            except Exception as durability_exc:
                print(
                    "[web-gui] live mutation ambiguity could not be durably updated: "
                    f"{type(durability_exc).__name__}"
                )
            print(
                "[web-gui] live order-management outcome requires reconciliation: "
                f"{type(exc).__name__}"
            )
            self._send_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "live_mutation_outcome_ambiguous",
                "The live mutation may have reached the venue. It will not be retried automatically; reconcile venue history first.",
                {
                    "mutation_id": entry.id,
                    "state": "ambiguous",
                    "reconciliation_route": "/api/mutations/reconcile",
                },
                retry_after_seconds=HTTP_OVERLOAD_RETRY_AFTER_SECONDS,
            )
            return

        stored = _stored_mutation_result(result)
        entry.state = "completed"
        entry.response_status = HTTPStatus.OK
        entry.response = stored
        entry.outcome_code = "live_dispatch_completed"
        entry.outcome_message = "The live mutation result was durably recorded."
        entry.updated_at = int(time.time())
        try:
            self._save_config(cfg)
        except Exception as exc:
            # The on-disk pending barrier remains fail-closed.  Do not expose
            # success when its replay disposition was not durably committed.
            print(f"[web-gui] live mutation result durability is uncertain: {type(exc).__name__}")
            self._send_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "live_mutation_durability_uncertain",
                "The venue returned a result, but its replay record was not durably committed. Reconcile before retrying.",
                {
                    "mutation_id": entry.id,
                    "state": "pending",
                    "reconciliation_route": "/api/mutations/reconcile",
                },
                retry_after_seconds=HTTP_OVERLOAD_RETRY_AFTER_SECONDS,
            )
            return
        self._send_json(HTTPStatus.OK, stored)

    def _handle_mutation(
        self,
        method: str,
        path: str,
        payload: Dict[str, Any],
        idempotency_key: str = "",
    ) -> None:
        if not path.startswith("/api/"):
            self._send_error(HTTPStatus.NOT_FOUND, "not_found", "Unknown route.")
            return

        try:
            cfg = self._load_config()
            journal_entry: Optional[MutationJournalEntry] = None
            if _is_general_durable_mutation(method, path):
                journal_entry, handled = self._prepare_general_idempotent_mutation(
                    cfg,
                    method,
                    path,
                    payload,
                    idempotency_key,
                )
                if handled:
                    return
                if journal_entry is None:
                    raise RuntimeError("Durable mutation journal preparation failed.")
            if method == "POST":
                order_route = path.strip("/").split("/")
                if len(order_route) == 4 and order_route[:2] == ["api", "markets"] and order_route[3] == "positions":
                    self._send_json(
                        HTTPStatus.OK,
                        market_position_intent_payload(
                            cfg,
                            self.app_server.adapter_registry,
                            unquote(order_route[2]),
                            payload,
                        ),
                    )
                    return
                if len(order_route) == 5 and order_route[:2] == ["api", "markets"] and order_route[3] == "orders":
                    self._execute_live_order_management(
                        cfg,
                        journal_entry,
                        unquote(order_route[2]),
                        unquote(order_route[4]),
                        payload,
                    )
                    return
            if method == "PATCH" and path == "/api/config":
                apply_config_patch(cfg, payload)
                self._save_config(cfg)
                self._send_json(HTTPStatus.OK, config_payload(cfg))
                return
            if method == "PATCH" and path.startswith("/api/markets/"):
                market_id = path.rsplit("/", 1)[-1]
                apply_market_patch(cfg, market_id, payload)
                self._save_config(cfg)
                self._send_json(HTTPStatus.OK, markets_payload(cfg, self.app_server.adapter_registry))
                return
            if method == "POST" and path == "/api/mutations/reconcile":
                if str(payload.get("confirm_reconciliation") or "").strip() != LIVE_MUTATION_RECONCILIATION_CONFIRMATION:
                    raise ValueError(
                        "Live mutation reconciliation requires exact confirmation text "
                        f"{LIVE_MUTATION_RECONCILIATION_CONFIRMATION}."
                    )
                raw_response = payload.get("response")
                if raw_response is not None and not isinstance(raw_response, dict):
                    raise ValueError("response must be a JSON object when supplied.")
                entry = cfg.reconcile_ambiguous_mutation(
                    str(payload.get("mutation_id") or ""),
                    str(payload.get("resolution") or ""),
                    _stored_mutation_result(raw_response) if isinstance(raw_response, dict) else None,
                )
                self._save_config(cfg)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "reconciled": {
                            "id": entry.id,
                            "state": entry.state,
                            "outcome_code": entry.outcome_code,
                            "updated_at": entry.updated_at,
                        },
                        **mutation_journal_payload(cfg),
                    },
                )
                return
            if method == "POST" and path == "/api/polymarket/users/mdd/cache/purge":
                self._send_json(HTTPStatus.OK, polymarket_mdd_cache_purge_payload(payload))
                return
            if method == "DELETE" and path.startswith("/api/polymarket/users/mdd/cache/"):
                cache_key = unquote(path.rsplit("/", 1)[-1])
                self._send_json(HTTPStatus.OK, polymarket_mdd_cache_purge_payload({"key": cache_key}))
                return
            if method == "POST" and path == "/api/polymarket/live-validation/reports":
                self._send_json(
                    HTTPStatus.OK,
                    polymarket_live_validation_report_store_payload(
                        cfg,
                        payload,
                        idempotency_key=idempotency_key,
                    ),
                )
                return
            if method == "POST" and path == "/api/polymarket/live-validation/decisions":
                self._send_json(
                    HTTPStatus.OK,
                    polymarket_live_validation_decision_store_payload(
                        payload,
                        idempotency_key=idempotency_key,
                    ),
                )
                return
            if method == "POST" and path == "/api/polymarket/live-validation/promotion-proposal/snapshots":
                self._send_json(
                    HTTPStatus.OK,
                    polymarket_live_validation_promotion_proposal_snapshot_store_payload(
                        payload,
                        idempotency_key=idempotency_key,
                    ),
                )
                return
            if method == "DELETE" and path.startswith("/api/polymarket/live-validation/promotion-proposal/snapshots/"):
                snapshot_key = unquote(path.rsplit("/", 1)[-1])
                self._send_json(
                    HTTPStatus.OK,
                    polymarket_live_validation_promotion_proposal_snapshot_purge_payload({"key": snapshot_key}),
                )
                return
            if method == "DELETE" and path.startswith("/api/polymarket/live-validation/reports/"):
                report_key = unquote(path.rsplit("/", 1)[-1])
                self._send_json(HTTPStatus.OK, polymarket_live_validation_report_purge_payload({"key": report_key}))
                return
            if method == "POST" and path == "/api/alerts":
                alert = alert_from_payload(cfg, self.app_server.adapter_registry, payload)
                cfg.alerts.append(alert)
                response = alerts_payload(
                    cfg,
                    self.app_server.adapter_registry,
                    self.app_server.alert_price_state,
                )
                self._commit_local_idempotent_mutation(cfg, journal_entry, response)
                return
            if method == "POST" and path == "/api/alerts/refresh":
                result = refresh_all_alert_prices(cfg, self.app_server.adapter_registry, self.app_server.alert_price_state)
                self._save_config(cfg)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "alerts": alerts_payload(cfg, self.app_server.adapter_registry, self.app_server.alert_price_state),
                        "message": f"Refreshed {len(result['refreshed'])} alert price source(s).",
                        **result,
                    },
                )
                return
            if method == "POST" and path.startswith("/api/alerts/") and path.endswith("/refresh"):
                alert_id = path.strip("/").split("/")[-2]
                alert = find_alert(cfg, alert_id)
                result = refresh_alert_price(
                    cfg,
                    self.app_server.adapter_registry,
                    alert,
                    self.app_server.alert_price_state,
                )
                self._save_config(cfg)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "alerts": alerts_payload(cfg, self.app_server.adapter_registry, self.app_server.alert_price_state),
                        "message": f"Refreshed {alert.label}: {alert.source}.",
                        "refreshed": [result],
                        "problems": [],
                    },
                )
                return
            if method == "PATCH" and path.startswith("/api/alerts/"):
                alert_id = path.rsplit("/", 1)[-1]
                alert = find_alert(cfg, alert_id)
                previous_key = _alert_price_state_key(_alert_market_id(alert), alert.token_id)
                alert_from_payload(cfg, self.app_server.adapter_registry, payload, existing=alert)
                next_key = _alert_price_state_key(_alert_market_id(alert), alert.token_id)
                if next_key != previous_key:
                    self.app_server.alert_price_state.pop(previous_key, None)
                self._save_config(cfg)
                self._send_json(
                    HTTPStatus.OK,
                    alerts_payload(cfg, self.app_server.adapter_registry, self.app_server.alert_price_state),
                )
                return
            if method == "DELETE" and path.startswith("/api/alerts/"):
                alert_id = path.rsplit("/", 1)[-1]
                alert = delete_alert(cfg, alert_id)
                if not any(_alert_price_state_key(_alert_market_id(item), item.token_id) == _alert_price_state_key(_alert_market_id(alert), alert.token_id) for item in cfg.alerts):
                    self.app_server.alert_price_state.pop(_alert_price_state_key(_alert_market_id(alert), alert.token_id), None)
                self._save_config(cfg)
                self._send_json(
                    HTTPStatus.OK,
                    alerts_payload(cfg, self.app_server.adapter_registry, self.app_server.alert_price_state),
                )
                return
            if method == "POST" and path == "/api/wallets":
                add_wallet_watch(cfg, payload)
                response = wallets_payload(
                    cfg,
                    self.app_server.wallet_polling,
                    self.app_server.wallet_recent_activity,
                )
                self._commit_local_idempotent_mutation(cfg, journal_entry, response)
                return
            if method == "PATCH" and path == "/api/wallets/polling":
                interval = optional_positive_float(payload.get("poll_interval_seconds"), "Poll interval")
                if interval is not None:
                    self.app_server.wallet_polling["poll_interval_seconds"] = max(2.0, float(interval))
                self.app_server.wallet_polling["last_message"] = "Polling settings updated."
                self._send_json(
                    HTTPStatus.OK,
                    wallets_payload(cfg, self.app_server.wallet_polling, self.app_server.wallet_recent_activity),
                )
                return
            if method == "POST" and path == "/api/wallets/poll":
                limit = int(_safe_float(payload.get("limit"), 25) or 25)
                result = poll_wallet_activity(
                    cfg,
                    self.app_server.adapter_registry,
                    self.app_server.wallet_recent_activity,
                    limit=max(1, min(limit, 100)),
                )
                self.app_server.wallet_polling["last_polled_at"] = time.time()
                self.app_server.wallet_polling["last_message"] = (
                    f"Polled {result['polled_wallets']} wallet(s); {len(result['activity'])} new activity item(s)."
                )
                self._save_config(cfg)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "wallets": wallets_payload(cfg, self.app_server.wallet_polling, self.app_server.wallet_recent_activity),
                        "copy": copy_payload(cfg, self.app_server.adapter_registry),
                        "message": self.app_server.wallet_polling["last_message"],
                        **result,
                    },
                )
                return
            if method == "PATCH" and path.startswith("/api/wallets/"):
                wallet_id = path.rsplit("/", 1)[-1]
                update_wallet_watch(cfg, wallet_id, payload)
                self._save_config(cfg)
                self._send_json(
                    HTTPStatus.OK,
                    wallets_payload(cfg, self.app_server.wallet_polling, self.app_server.wallet_recent_activity),
                )
                return
            if method == "DELETE" and path.startswith("/api/wallets/"):
                wallet_id = path.rsplit("/", 1)[-1]
                wallet = delete_wallet_watch(cfg, wallet_id)
                self.app_server.wallet_recent_activity = [
                    item for item in self.app_server.wallet_recent_activity if item.get("wallet_id") != wallet.id
                ]
                self._save_config(cfg)
                self._send_json(
                    HTTPStatus.OK,
                    wallets_payload(cfg, self.app_server.wallet_polling, self.app_server.wallet_recent_activity),
                )
                return
            if method == "PATCH" and path == "/api/copy":
                apply_copy_settings_patch(cfg, payload)
                self._save_config(cfg)
                self._send_json(HTTPStatus.OK, copy_payload(cfg, self.app_server.adapter_registry))
                return
            if method == "POST" and path == "/api/copy/preview":
                self._send_json(HTTPStatus.OK, copy_preview_payload(cfg, self.app_server.adapter_registry, payload))
                return
            if method == "POST" and path == "/api/live-safety/preflight":
                self._send_json(HTTPStatus.OK, live_preflight_payload(cfg, self.app_server.adapter_registry, payload))
                return
            if method == "POST" and path == "/api/paper/quote":
                self._send_json(HTTPStatus.OK, paper_quote_payload(cfg, self.app_server.adapter_registry, payload))
                return
            if method == "POST" and path == "/api/paper/quote-limit":
                self._send_json(HTTPStatus.OK, paper_quote_limit_payload(cfg, self.app_server.adapter_registry, payload))
                return
            if method == "POST" and path == "/api/paper/preview-impact":
                order = paper_order_from_payload(payload)
                impact = paper_order_impact(cfg.paper_trades, order)
                self._send_json(HTTPStatus.OK, {"impact": impact, "message": format_paper_order_impact(impact)})
                return
            if method == "POST" and path == "/api/paper/orders":
                result = submit_paper_order(cfg, self.app_server.adapter_registry, payload)
                self.app_server.paper_position_marks = _paper_marks_for_rows(
                    self.app_server.paper_position_marks,
                    paper_position_rows(cfg.paper_trades),
                )
                response = {
                    **result,
                    "paper": paper_payload(cfg, self.app_server.paper_position_marks),
                }
                self._commit_local_idempotent_mutation(
                    cfg,
                    journal_entry,
                    response,
                    preserve_response_shape=True,
                    client_response=response,
                )
                return
            if method == "POST" and path == "/api/paper/history/use":
                self._send_json(HTTPStatus.OK, history_refill_payload(cfg, str(payload.get("record_id") or "")))
                return
            if method == "POST" and path == "/api/paper/positions/use":
                self._send_json(
                    HTTPStatus.OK,
                    position_refill_payload(
                        cfg,
                        str(payload.get("market_id") or ""),
                        str(payload.get("contract_id") or ""),
                    ),
                )
                return
            if method == "POST" and path == "/api/paper/marks/refresh":
                rows = paper_position_rows(cfg.paper_trades)
                self.app_server.paper_position_marks, problems = refresh_paper_marks(
                    cfg,
                    self.app_server.adapter_registry,
                    rows,
                    self.app_server.paper_position_marks,
                )
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "paper": paper_payload(cfg, self.app_server.paper_position_marks),
                        "problems": problems,
                        "message": f"Marked {len(self.app_server.paper_position_marks)}/{len(rows)} paper positions.",
                    },
                )
                return
            if method == "POST" and path == "/api/paper/marks/refresh-selected":
                self.app_server.paper_position_marks = refresh_selected_paper_mark(
                    cfg,
                    self.app_server.adapter_registry,
                    str(payload.get("market_id") or ""),
                    str(payload.get("contract_id") or ""),
                    self.app_server.paper_position_marks,
                )
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "paper": paper_payload(cfg, self.app_server.paper_position_marks),
                        "message": "Selected paper exposure mark refreshed.",
                    },
                )
                return
            if method == "POST" and path == "/api/paper/marks/clear":
                self.app_server.paper_position_marks = {}
                self._send_json(
                    HTTPStatus.OK,
                    {"paper": paper_payload(cfg, self.app_server.paper_position_marks), "message": "Paper exposure marks cleared."},
                )
                return
            if method == "POST" and path == "/api/paper/marks/clear-selected":
                key = (str(payload.get("market_id") or "").strip().lower(), str(payload.get("contract_id") or "").strip())
                marks = _paper_marks_for_rows(self.app_server.paper_position_marks, paper_position_rows(cfg.paper_trades))
                marks.pop(key, None)
                self.app_server.paper_position_marks = marks
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "paper": paper_payload(cfg, self.app_server.paper_position_marks),
                        "message": f"Selected paper exposure mark cleared: {key[0]}:{key[1]}",
                    },
                )
                return
            if method == "POST" and path == "/api/paper/history/clear":
                cfg.paper_trades = []
                self.app_server.paper_position_marks = {}
                self._save_config(cfg)
                self._send_json(HTTPStatus.OK, paper_payload(cfg, self.app_server.paper_position_marks))
                return
        except json.JSONDecodeError:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_json", "Invalid JSON request body.")
            return
        except UnsupportedFeatureError as exc:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "unsupported_feature",
                str(exc),
                {"market_id": exc.market_id, "feature": exc.feature},
            )
            return
        except LiveValidationReportSchemaError as exc:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "live_validation_report_schema_error",
                "Live validation report failed schema validation.",
                {"schema_validation": exc.validation},
            )
            return
        except ConfigConflictError:
            self._send_error(
                HTTPStatus.CONFLICT,
                "config_conflict",
                "Configuration changed in another process. Reload state and retry the mutation.",
            )
            return
        except IdempotencyConflictError as exc:
            self._send_error(
                HTTPStatus.CONFLICT,
                "idempotency_conflict",
                str(exc),
            )
            return
        except LiveValidationStoreDurabilityError:
            self._send_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "live_validation_store_durability_uncertain",
                "The evidence record may already be committed but durable flush confirmation failed. "
                "Retry this exact request with the same Idempotency-Key.",
                {
                    "committed": True,
                    "retryable": True,
                    "retry_after_seconds": HTTP_OVERLOAD_RETRY_AFTER_SECONDS,
                },
                retry_after_seconds=HTTP_OVERLOAD_RETRY_AFTER_SECONDS,
            )
            return
        except ValueError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, "validation_error", str(exc))
            return
        except HttpClientDisconnected:
            raise
        except Exception as exc:
            print(f"[web-gui] internal error while handling {method} {path}: {type(exc).__name__}")
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", "Internal server error.")
            return

        self._send_error(HTTPStatus.NOT_FOUND, "not_found", "Unknown API route.")

    def _serve_static(self, raw_path: str) -> None:
        static_files = self.app_server.static_files
        index = self._resolve_static_path(static_files, "/")
        if index is None or not index.is_file():
            self._send_error(
                HTTPStatus.NOT_FOUND,
                "react_build_missing",
                "React build is missing.",
                {
                    "hint": "Build the React app, use the Vite dev server, or start the Tkinter fallback.",
                    "build_command": REACT_BUILD_COMMAND,
                    "dev_command": REACT_DEV_COMMAND,
                    "prod_command": REACT_PROD_COMMAND,
                    "tkinter_fallback": f"{PYTHON_GUI_SCRIPT} or {PYTHON_GUI_COMMAND}",
                },
            )
            return

        target = self._resolve_static_path(static_files, raw_path)
        if target is None or not target.exists() or not target.is_file():
            target = index
            relative_path = "index.html"
        elif target.parent.name == "assets":
            relative_path = f"assets/{target.name}"
        else:
            relative_path = target.name

        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if target.stat().st_size > MAX_HTTP_RESPONSE_BYTES:
            self._send_response_too_large()
            return
        data = target.read_bytes()
        if len(data) > MAX_HTTP_RESPONSE_BYTES:
            self._send_response_too_large()
            return
        self.send_response(HTTPStatus.OK)
        self._send_cors_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", static_cache_control(relative_path))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self._write_response_body(data)

    def _resolve_static_path(self, static_files: Mapping[str, Path], raw_path: str) -> Optional[Path]:
        path = unquote(raw_path.split("?", 1)[0])
        # A backslash is a path separator on Windows but not POSIX. Reject it on
        # every platform instead of allowing platform-dependent interpretation.
        if "\\" in path:
            return None
        if path in {"", "/"}:
            normalized = "index.html"
        else:
            normalized = posixpath.normpath(path.lstrip("/"))
        if normalized.startswith("../") or normalized == "..":
            return None
        parts = normalized.split("/")
        if len(parts) == 1:
            relative_path = parts[0]
        elif len(parts) == 2 and parts[0] == "assets":
            relative_path = f"assets/{parts[1]}"
        else:
            return None
        if not STATIC_FRONTEND_FILENAME_RE.fullmatch(parts[-1]):
            return None

        # Look up a URL key in a catalog built only from the trusted frontend
        # directory.  No request value is used to construct a filesystem path.
        return static_files.get(relative_path)

    @staticmethod
    def _static_file_catalog(frontend_dir: Optional[Path] = None) -> Dict[str, Path]:
        """Return supported static files beneath a trusted deployment root."""
        try:
            configured = frontend_dir if frontend_dir is not None else DEFAULT_FRONTEND_DIR
            normalized_root = os.path.normcase(
                os.path.realpath(os.path.expanduser(os.fspath(configured)))
            )
            normalized_default = os.path.normcase(
                os.path.realpath(os.path.expanduser(os.fspath(DEFAULT_FRONTEND_DIR)))
            )
            normalized_allowed = os.path.normcase(os.path.realpath(os.fspath(_RESOURCE_ROOT)))
            allowed_prefix = normalized_allowed.rstrip(os.sep) + os.sep
            if normalized_root != normalized_default and not normalized_root.startswith(allowed_prefix):
                return {}
            # The canonical root is normalized and constrained immediately
            # above, before any filesystem lookup.
            root = Path(normalized_root)
        except (OSError, RuntimeError, ValueError, TypeError):
            return {}
        if not root.is_dir():
            return {}

        catalog: Dict[str, Path] = {}
        try:
            index_target = (root / "index.html").resolve()
            index_target.relative_to(root)
            if index_target.is_file():
                catalog["index.html"] = index_target
        except (OSError, RuntimeError, ValueError):
            pass

        try:
            root_entries = tuple(root.iterdir())
        except OSError:
            return catalog
        for candidate in root_entries:
            if candidate.name != "index.html" and STATIC_FRONTEND_FILENAME_RE.fullmatch(candidate.name):
                try:
                    target = candidate.resolve()
                    target.relative_to(root)
                    if target.is_file():
                        catalog[candidate.name] = target
                except (OSError, RuntimeError, ValueError):
                    continue

        assets_dir = root / "assets"
        try:
            asset_entries = (
                tuple(assets_dir.iterdir()) if assets_dir.is_dir() else ()
            )
        except OSError:
            return catalog
        for candidate in asset_entries:
            if STATIC_FRONTEND_FILENAME_RE.fullmatch(candidate.name):
                try:
                    target = candidate.resolve()
                    target.relative_to(root)
                    if target.is_file():
                        catalog[f"assets/{candidate.name}"] = target
                except (OSError, RuntimeError, ValueError):
                    continue
        return catalog

    def _send_json(self, status: int, payload: Dict[str, Any], *, retry_after_seconds: Optional[int] = None) -> None:
        try:
            data = _json_bytes(payload)
        except HttpResponseTooLargeError:
            self._send_response_too_large()
            return
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if retry_after_seconds is not None:
            self.send_header("Retry-After", str(max(1, int(retry_after_seconds))))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self._write_response_body(data)
        self.close_connection = True

    def _send_text(self, status: int, text: str, *, content_type: str, filename: Optional[str] = None) -> None:
        try:
            data = _utf8_bytes(text)
        except HttpResponseTooLargeError:
            self._send_response_too_large()
            return
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        if filename:
            safe_name = _safe_attachment_filename(filename)
            self.send_header("Content-Disposition", f'attachment; filename="{safe_name}"')
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self._write_response_body(data)
        self.close_connection = True

    def _send_response_too_large(self) -> None:
        self.app_server.http_metrics.record_response_too_large()
        data = json.dumps(
            api_error_payload(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "response_too_large",
                "The server refused to send a response larger than its configured byte limit.",
                {"max_response_bytes": MAX_HTTP_RESPONSE_BYTES},
            ),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self._write_response_body(data)
        self.close_connection = True

    def _write_response_body(self, data: bytes) -> None:
        """Write large responses in bounded chunks and verify every byte is sent.

        Some Windows loopback/socket combinations can return from a single
        large buffered write after only 128 KiB while the handler proceeds to
        close the connection.  Chunking keeps the HTTP ``Content-Length``
        contract intact for the support matrix, bundled assets, and exports.
        """

        view = memoryview(data)
        while view:
            chunk = view[:HTTP_RESPONSE_CHUNK_BYTES]
            written = self.wfile.write(chunk)
            if written is None:
                written = len(chunk)
            if written <= 0:
                raise ConnectionError("HTTP response writer made no progress.")
            view = view[written:]
        self.wfile.flush()

    def _send_error(
        self,
        status: int,
        code: str,
        message: str,
        details: Optional[Mapping[str, Any]] = None,
        *,
        retry_after_seconds: Optional[int] = None,
    ) -> None:
        self._send_json(
            status,
            api_error_payload(status, code, message, details),
            retry_after_seconds=retry_after_seconds,
        )

    def _send_cors_headers(self) -> None:
        origin = str(self.headers.get("Origin") or "").strip().rstrip("/")
        if origin and origin in self.app_server.allowed_origins:
            self.send_header("Access-Control-Allow-Origin", _safe_http_header_value(origin))
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, PATCH, POST, DELETE, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization, X-Market-Sentinel-Token, Idempotency-Key",
        )
        self.send_header("Access-Control-Expose-Headers", "X-Request-ID")


def is_loopback_host(host: str) -> bool:
    normalized = str(host or "").strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _resolve_trusted_frontend_dir(frontend_dir: Path) -> Optional[Path]:
    """Normalize a deployment frontend path and keep it inside the bundle root."""
    try:
        # Normalize before constructing a filesystem path and compare the
        # canonical strings with the trusted deployment root. This follows the
        # CodeQL containment pattern and also handles macOS /var symlinks.
        configured_root = os.path.realpath(os.path.expanduser(os.fspath(frontend_dir)))
        default_root = os.path.realpath(os.path.expanduser(os.fspath(DEFAULT_FRONTEND_DIR)))
        # The module-level default is a deployment resource, not request data.
        # Keep this fast path so packaged builds and test-configured defaults
        # do not need to widen the trusted-root boundary.
        if os.path.normcase(configured_root) == os.path.normcase(default_root):
            return Path(configured_root)
        allowed_root = os.path.realpath(os.fspath(_RESOURCE_ROOT))
        normalized_root = os.path.normcase(configured_root)
        normalized_allowed_root = os.path.normcase(allowed_root)
        if os.path.commonpath((normalized_root, normalized_allowed_root)) != normalized_allowed_root:
            return None
        # The commonpath check above proves this is beneath the trusted root.
        return Path(configured_root)
    except (OSError, RuntimeError, ValueError, TypeError):
        return None


def run_server(
    host: str,
    port: int,
    config_path: Path,
    *,
    api_token: str = "",
    allow_remote: bool = False,
    allowed_origins: Optional[Sequence[str]] = None,
    frontend_dir: Path = DEFAULT_FRONTEND_DIR,
) -> None:
    frontend_dir = _resolve_trusted_frontend_dir(frontend_dir)
    if frontend_dir is None:
        raise ValueError(
            "The frontend directory must resolve beneath the deployment resource root. "
            f"Allowed root: {_RESOURCE_ROOT}"
        )
    if not is_loopback_host(host) and not allow_remote:
        raise ValueError(
            "Refusing a non-loopback bind without --allow-remote. Keep the default loopback bind and use a TLS reverse proxy."
        )
    # Fail before listening so a corrupt state file cannot silently reset trading settings.
    load_config(config_path)
    server = ReactGuiServer(
        (host, port),
        ReactGuiHandler,
        config_path=config_path,
        frontend_dir=frontend_dir,
        api_token=api_token,
        allowed_origins=allowed_origins,
    )
    print(f"React GUI API listening on http://{host}:{port}")
    if (frontend_dir / "index.html").exists():
        print("Serving built React GUI from the configured frontend directory")
    else:
        print("React build not found in the configured frontend directory")
        print(f"Build it with `{REACT_BUILD_COMMAND}`, or run `{REACT_DEV_COMMAND}` for Vite.")
    print(f"Tkinter GUI is unchanged: run `{PYTHON_GUI_SCRIPT}` or `{PYTHON_GUI_COMMAND}`.")
    previous_signal_handlers: Dict[int, Any] = {}

    def begin_signal_drain(signum: int, _frame: Any) -> None:
        if server.begin_drain():
            print(f"\nSignal {signum} received; draining mutations before shutdown.")
            threading.Thread(target=server.shutdown, name="market-sentinel-shutdown", daemon=True).start()

    if threading.current_thread() is threading.main_thread():
        for signal_name in ("SIGTERM", "SIGINT"):
            signal_number = getattr(signal, signal_name, None)
            if signal_number is None:
                continue
            previous_signal_handlers[int(signal_number)] = signal.getsignal(signal_number)
            signal.signal(signal_number, begin_signal_drain)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping React GUI API.")
    finally:
        server.begin_drain()
        if not server.wait_for_mutation_drain(HTTP_MUTATION_DRAIN_TIMEOUT_SECONDS):
            print(
                "[web-gui] mutation drain deadline expired; unresolved live requests remain protected by durable pending journal entries."
            )
        server.server_close()
        for signal_number, previous_handler in previous_signal_handlers.items():
            signal.signal(signal_number, previous_handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the optional local API for the React/TypeScript GUI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument(
        "--frontend-dir",
        type=Path,
        default=DEFAULT_FRONTEND_DIR,
        help="Built React frontend directory. Must remain beneath the deployment resource root.",
    )
    parser.add_argument(
        "--api-token",
        default=os.environ.get("MARKET_SENTINEL_API_TOKEN", ""),
        help="Required for non-loopback binds. Defaults to MARKET_SENTINEL_API_TOKEN.",
    )
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="Acknowledge a non-loopback bind. Requires --api-token or MARKET_SENTINEL_API_TOKEN.",
    )
    parser.add_argument(
        "--allow-origin",
        action="append",
        default=None,
        help="Additional browser Origin allowed for CORS. Repeat as needed; wildcard origins are never accepted.",
    )
    args = parser.parse_args()
    run_server(
        args.host,
        args.port,
        args.config,
        api_token=args.api_token,
        allow_remote=args.allow_remote,
        allowed_origins=configured_allowed_origins(args.allow_origin),
        frontend_dir=args.frontend_dir,
    )


if __name__ == "__main__":
    main()
