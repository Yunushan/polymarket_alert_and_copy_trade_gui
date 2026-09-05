from __future__ import annotations

import errno
import hashlib
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from functools import wraps
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional

from core.atomic_files import replace_file
from .live_report_schema import (
    CREDENTIAL_PROMOTION_CHECKS,
    CREDENTIAL_PROMOTION_SEMANTICS,
    EXPECTED_REPOSITORY_ORIGIN,
    PUBLIC_PROMOTION_CHECKS,
    compact_schema_validation,
    ensure_live_validation_report_valid,
    validate_live_validation_report,
)
from .live_verification import (
    cancel_response_contains,
    extract_order_id,
    order_state_is_cancelled,
    order_zero_fill_evidence,
)
from .constants import POLYMARKET_LIVE_MUTATION_BLOCKER, POLYMARKET_LIVE_MUTATIONS_SUPPORTED


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LIVE_VALIDATION_REPORTS_PATH = PROJECT_ROOT / "data" / "polymarket_live_validation_reports.json"
DEFAULT_LIVE_VALIDATION_DECISIONS_PATH = PROJECT_ROOT / "data" / "polymarket_live_validation_decisions.json"
DEFAULT_LIVE_VALIDATION_PROMOTION_PROPOSAL_SNAPSHOTS_PATH = (
    PROJECT_ROOT / "data" / "polymarket_live_validation_promotion_proposal_snapshots.json"
)
DEFAULT_LIVE_VALIDATION_REPORTS_MAX_ENTRIES = 100
DEFAULT_LIVE_VALIDATION_PROMOTION_PROPOSAL_SNAPSHOTS_MAX_ENTRIES = 50
LIVE_VALIDATION_REPORTS_VERSION = 1
LIVE_VALIDATION_REPORT_REVIEW_BUNDLE_VERSION = 1
LIVE_VALIDATION_DECISIONS_VERSION = 1
LIVE_VALIDATION_PROMOTION_PROPOSAL_VERSION = 1
LIVE_VALIDATION_PROMOTION_PROPOSAL_SNAPSHOTS_VERSION = 1
POLYMARKET_LIVE_VALIDATION_REPORT_KIND = "polymarket_live_validation_report"
POLYMARKET_LIVE_VALIDATION_REPORT_REVIEW_KIND = "polymarket_live_validation_report_review_bundle"
POLYMARKET_LIVE_VALIDATION_DECISION_KIND = "polymarket_live_validation_promotion_decision"
POLYMARKET_LIVE_VALIDATION_PROMOTION_PROPOSAL_KIND = "polymarket_live_validation_coverage_promotion_proposal"
POLYMARKET_LIVE_VALIDATION_PROMOTION_PROPOSAL_SNAPSHOT_KIND = (
    "polymarket_live_validation_coverage_promotion_proposal_snapshot"
)
LIVE_VALIDATION_DECISION_TARGET_TIERS = ("public_live_verified", "credential_live_verified", "funded_live_verified")
LIVE_VALIDATION_DECISIONS = ("accepted", "rejected")
LOCAL_ONLY_REPORT_MODES = {
    "local_readiness_only",
    "credential_runbook_no_funded_actions",
    "browser_smoke",
    "browser_smoke_seed",
}

SENSITIVE_REPORT_KEY_FRAGMENTS = (
    "api_key",
    "apikey",
    "secret",
    "signature",
    "passphrase",
    "private",
    "password",
    "cookie",
    "session",
    "bearer",
)

LIVE_VALIDATION_STORE_LOCK_TIMEOUT_SECONDS = 30.0
LIVE_VALIDATION_STORE_LOCK_POLL_SECONDS = 0.05
LIVE_VALIDATION_IDEMPOTENCY_KEY_MAX_LENGTH = 128
LIVE_VALIDATION_IDEMPOTENCY_BINDINGS_MAX_ENTRIES = 256
_MISSING_STORE_REVISION = "missing"
_STORE_REVISION_ATTR = "_live_validation_store_revision"


class LiveValidationStoreError(RuntimeError):
    """Base error for a live-evidence store that cannot be used safely."""

    def __init__(self, path: Path | str, reason: str) -> None:
        self.path = Path(path)
        self.reason = str(reason or "store error")
        super().__init__(
            f"Live validation evidence store could not be used safely ({self.reason}): {self.path}. "
            "The existing file was left unchanged."
        )


class LiveValidationStoreReadError(LiveValidationStoreError):
    """Raised when existing live-evidence JSON is unreadable or malformed."""


class LiveValidationStoreLockError(LiveValidationStoreError):
    """Raised when exclusive mutation access to a live-evidence store fails."""


class LiveValidationStoreConflictError(LiveValidationStoreError):
    """Raised when a stale snapshot would overwrite a newer store revision."""


class LiveValidationStoreIntegrityError(LiveValidationStoreError):
    """Raised when persisted store invariants are malformed or ambiguous."""


class LiveValidationStoreDurabilityError(LiveValidationStoreError):
    """Raised when replacement committed but directory durability is uncertain."""

    def __init__(self, path: Path | str, content_hash: str) -> None:
        self.path = Path(path)
        self.reason = "replacement committed but parent-directory fsync failed"
        self.committed = True
        self.content_hash = str(content_hash or "")
        self.operation_key = ""
        self.idempotency_key_hash = ""
        RuntimeError.__init__(
            self,
            f"Live validation evidence was replaced but directory durability is uncertain: {self.path}. "
            "Do not create a new operation; retry with the same idempotency key so the committed record can be reconciled.",
        )


class _RevisionedLiveValidationStore(dict[str, Any]):
    """A store snapshot carrying non-persisted compare-and-swap provenance."""

    def __init__(self, *args: Any, revision: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        setattr(self, _STORE_REVISION_ATTR, revision)


_STORE_THREAD_LOCKS_GUARD = threading.Lock()
_STORE_THREAD_LOCKS: Dict[str, threading.RLock] = {}
_STORE_THREAD_STATE = threading.local()


def live_validation_reports_path(path: Optional[Path | str] = None) -> Path:
    if path is not None:
        return Path(path)
    configured = os.environ.get("POLYMARKET_LIVE_VALIDATION_REPORTS_PATH")
    if configured:
        return Path(configured)
    return DEFAULT_LIVE_VALIDATION_REPORTS_PATH


def live_validation_decisions_path(path: Optional[Path | str] = None) -> Path:
    if path is not None:
        return Path(path)
    configured = os.environ.get("POLYMARKET_LIVE_VALIDATION_DECISIONS_PATH")
    if configured:
        return Path(configured)
    return DEFAULT_LIVE_VALIDATION_DECISIONS_PATH


def live_validation_promotion_proposal_snapshots_path(path: Optional[Path | str] = None) -> Path:
    if path is not None:
        return Path(path)
    configured = os.environ.get("POLYMARKET_LIVE_VALIDATION_PROMOTION_PROPOSAL_SNAPSHOTS_PATH")
    if configured:
        return Path(configured)
    return DEFAULT_LIVE_VALIDATION_PROMOTION_PROPOSAL_SNAPSHOTS_PATH


def _store_lock_key(target: Path) -> str:
    return os.path.normcase(str(target.expanduser().resolve(strict=False)))


def _store_thread_lock(target: Path) -> threading.RLock:
    key = _store_lock_key(target)
    with _STORE_THREAD_LOCKS_GUARD:
        lock = _STORE_THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _STORE_THREAD_LOCKS[key] = lock
    return lock


def _store_lock_path(target: Path) -> Path:
    return target.with_name(f".{target.name}.lock")


def _lock_is_contended(error: OSError) -> bool:
    return error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK} or getattr(error, "winerror", None) in {
        33,
        36,
        158,
    }


def _try_lock_file(handle: Any) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: Any) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _initialize_lock_file(handle: Any, target: Path) -> None:
    try:
        if os.name == "posix":
            os.fchmod(handle.fileno(), 0o600)
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        raise LiveValidationStoreLockError(target, "lock file initialization failed") from None


@contextmanager
def _cross_process_store_lock(
    target: Path,
    *,
    timeout_seconds: float = LIVE_VALIDATION_STORE_LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    lock_path = _store_lock_path(target)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
    except OSError:
        raise LiveValidationStoreLockError(target, "lock file unavailable") from None

    try:
        _initialize_lock_file(handle, target)

        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        while True:
            try:
                _try_lock_file(handle)
                break
            except OSError as error:
                if not _lock_is_contended(error):
                    raise LiveValidationStoreLockError(target, "lock acquisition failed") from None
                if time.monotonic() >= deadline:
                    raise LiveValidationStoreLockError(target, "lock acquisition timed out") from None
                time.sleep(LIVE_VALIDATION_STORE_LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            try:
                _unlock_file(handle)
            except OSError:
                # Closing the descriptor releases an advisory lock even if an
                # explicit unlock races with process teardown.
                pass
    finally:
        handle.close()


@contextmanager
def _store_mutation_lock(target: Path) -> Iterator[None]:
    key = _store_lock_key(target)
    thread_lock = _store_thread_lock(target)
    if not thread_lock.acquire(timeout=LIVE_VALIDATION_STORE_LOCK_TIMEOUT_SECONDS):
        raise LiveValidationStoreLockError(target, "in-process lock acquisition timed out")

    depths = getattr(_STORE_THREAD_STATE, "depths", None)
    if depths is None:
        depths = {}
        _STORE_THREAD_STATE.depths = depths
    depth = int(depths.get(key, 0))
    depths[key] = depth + 1
    try:
        if depth:
            yield
        else:
            with _cross_process_store_lock(target):
                yield
    finally:
        remaining = int(depths.get(key, 1)) - 1
        if remaining > 0:
            depths[key] = remaining
        else:
            depths.pop(key, None)
        thread_lock.release()


def _locked_store_mutation(path_resolver: Any, *, path_parameter: str, positional_index: Optional[int] = None) -> Any:
    def decorate(function: Any) -> Any:
        @wraps(function)
        def locked(*args: Any, **kwargs: Any) -> Any:
            configured_path = kwargs.get(path_parameter)
            if configured_path is None and positional_index is not None and len(args) > positional_index:
                configured_path = args[positional_index]
            target = path_resolver(configured_path)
            with _store_mutation_lock(target):
                return function(*args, **kwargs)

        return locked

    return decorate


def _read_store_json(target: Path, *, collection_key: str) -> Dict[str, Any]:
    try:
        encoded = target.read_bytes()
        payload = encoded.decode("utf-8")
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError):
        raise LiveValidationStoreReadError(target, "existing file is unreadable") from None

    try:
        raw = json.loads(payload)
    except (json.JSONDecodeError, RecursionError):
        raise LiveValidationStoreReadError(target, "existing file contains invalid JSON") from None
    if not isinstance(raw, dict):
        raise LiveValidationStoreReadError(target, "existing JSON root is not an object")
    if not isinstance(raw.get(collection_key), dict):
        raise LiveValidationStoreReadError(target, f"existing JSON field '{collection_key}' is not an object")
    return _RevisionedLiveValidationStore(raw, revision=hashlib.sha256(encoded).hexdigest())


def _validate_store_for_write(store: Mapping[str, Any], *, collection_key: str) -> None:
    if not isinstance(store, Mapping) or not isinstance(store.get(collection_key), Mapping):
        raise ValueError(f"Live validation evidence store must contain an object field named '{collection_key}'.")


def _idempotency_key_hash(kind: str, value: str) -> str:
    clean = str(value or "")
    if not clean:
        return ""
    if len(clean) > LIVE_VALIDATION_IDEMPOTENCY_KEY_MAX_LENGTH or any(
        ord(char) < 33 or ord(char) > 126 for char in clean
    ):
        raise ValueError(
            f"idempotency_key must contain 1-{LIVE_VALIDATION_IDEMPOTENCY_KEY_MAX_LENGTH} visible ASCII characters."
        )
    return hashlib.sha256(f"{kind}:idempotency:{clean}".encode("utf-8")).hexdigest()


def _operation_request_hash(kind: str, payload: Mapping[str, Any]) -> str:
    body = json.dumps(_jsonable(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{kind}:request:{body}".encode("utf-8")).hexdigest()


def _bound_operation_request_hash(
    kind: str,
    default_request: Mapping[str, Any],
    idempotency_request: Optional[Mapping[str, Any]],
    *,
    stable_context: Optional[Mapping[str, Any]] = None,
) -> str:
    if idempotency_request is not None and not isinstance(idempotency_request, Mapping):
        raise ValueError("idempotency_request must be an object when provided.")
    binding_mode = "caller" if idempotency_request is not None else "derived"
    request = (
        {
            "context": _jsonable(stable_context or {}),
            "caller_request": _jsonable(idempotency_request),
        }
        if idempotency_request is not None
        else _jsonable(default_request)
    )
    return _operation_request_hash(
        kind,
        {
            "binding_mode": binding_mode,
            "request": _jsonable(request),
        },
    )


def _find_idempotent_entry(
    entries: Mapping[str, Any],
    idempotency_hash: str,
    *,
    target: Path,
) -> Optional[Dict[str, Any]]:
    if not idempotency_hash:
        return None
    matches: List[Dict[str, Any]] = []
    for entry in entries.values():
        if not isinstance(entry, dict):
            continue
        known_hashes = entry.get("idempotency_key_hashes")
        request_hashes = entry.get("idempotency_requests")
        if entry.get("idempotency_key_hash") == idempotency_hash or (
            isinstance(known_hashes, list) and idempotency_hash in known_hashes
        ) or (
            isinstance(request_hashes, Mapping) and idempotency_hash in request_hashes
        ):
            matches.append(entry)
    if len(matches) > 1:
        raise LiveValidationStoreIntegrityError(
            target,
            "idempotency binding is ambiguous across multiple records",
        )
    return matches[0] if matches else None


def _validate_idempotency_request(
    entry: Mapping[str, Any],
    *,
    idempotency_hash: str,
    request_hash: str,
    target: Path,
) -> None:
    if not idempotency_hash:
        return
    requests = entry.get("idempotency_requests")
    if requests is not None and not isinstance(requests, Mapping):
        raise LiveValidationStoreIntegrityError(target, "idempotency request bindings are malformed")
    known_hashes = entry.get("idempotency_key_hashes")
    if known_hashes is not None and not isinstance(known_hashes, list):
        raise LiveValidationStoreIntegrityError(target, "idempotency key bindings are malformed")
    mapped_request_hash = requests.get(idempotency_hash) if isinstance(requests, Mapping) else None
    primary_request_hash = (
        entry.get("idempotency_request_hash") if entry.get("idempotency_key_hash") == idempotency_hash else None
    )
    if mapped_request_hash is not None and not str(mapped_request_hash):
        raise LiveValidationStoreIntegrityError(target, "idempotency request binding is empty")
    if primary_request_hash is not None and not str(primary_request_hash):
        raise LiveValidationStoreIntegrityError(target, "primary idempotency request binding is empty")
    if mapped_request_hash and primary_request_hash and str(mapped_request_hash) != str(primary_request_hash):
        raise LiveValidationStoreIntegrityError(target, "idempotency request bindings contradict each other")
    known_request_hash = mapped_request_hash or primary_request_hash
    if not known_request_hash:
        raise ValueError(
            "idempotency_key matches an older record without request binding; use a new key for this operation."
        )
    if str(known_request_hash) != request_hash:
        raise ValueError("idempotency_key was already used for a different operation request.")


def _register_idempotency_request(
    entry: Dict[str, Any],
    *,
    idempotency_hash: str,
    request_hash: str,
    target: Path,
) -> None:
    if not idempotency_hash:
        return
    requests = entry.get("idempotency_requests")
    known_hashes = entry.get("idempotency_key_hashes")
    if requests is not None and not isinstance(requests, dict):
        raise LiveValidationStoreIntegrityError(target, "idempotency request bindings are malformed")
    if known_hashes is not None and not isinstance(known_hashes, list):
        raise LiveValidationStoreIntegrityError(target, "idempotency key bindings are malformed")
    requests = requests if isinstance(requests, dict) else {}
    known_hashes = known_hashes if isinstance(known_hashes, list) else []
    if (
        idempotency_hash not in requests
        and len(requests) >= LIVE_VALIDATION_IDEMPOTENCY_BINDINGS_MAX_ENTRIES
    ):
        raise LiveValidationStoreError(
            target,
            "idempotency binding capacity is exhausted for this retained record",
        )
    if idempotency_hash in requests and not str(requests[idempotency_hash]):
        raise LiveValidationStoreIntegrityError(target, "idempotency request binding is empty")
    if idempotency_hash in requests and str(requests[idempotency_hash]) != request_hash:
        raise ValueError("idempotency_key was already used for a different operation request.")
    primary_key_hash = str(entry.get("idempotency_key_hash") or "")
    primary_request_hash = str(entry.get("idempotency_request_hash") or "")
    if primary_key_hash == idempotency_hash and primary_request_hash and primary_request_hash != request_hash:
        raise LiveValidationStoreIntegrityError(target, "idempotency request bindings contradict each other")
    if primary_key_hash == idempotency_hash and not primary_request_hash:
        raise LiveValidationStoreIntegrityError(target, "primary idempotency request binding is empty")

    entry["idempotency_requests"] = requests
    entry["idempotency_key_hashes"] = known_hashes
    requests[idempotency_hash] = request_hash
    if idempotency_hash not in known_hashes:
        known_hashes.append(idempotency_hash)
    if not entry.get("idempotency_key_hash"):
        entry["idempotency_key_hash"] = idempotency_hash
        entry["idempotency_request_hash"] = request_hash


def _attach_uncertain_operation(
    error: LiveValidationStoreDurabilityError,
    *,
    operation_key: str,
    idempotency_hash: str,
) -> None:
    error.operation_key = str(operation_key or "")
    error.idempotency_key_hash = str(idempotency_hash or "")


def _save_store_operation(
    writer: Any,
    store: Mapping[str, Any],
    target: Path,
    *,
    operation_key: str,
    idempotency_hash: str,
) -> Path:
    try:
        return writer(store, target)
    except LiveValidationStoreDurabilityError as error:
        _attach_uncertain_operation(
            error,
            operation_key=operation_key,
            idempotency_hash=idempotency_hash,
        )
        raise


def _reconcile_committed_store_operation(
    store: Mapping[str, Any],
    target: Path,
    *,
    operation_key: str,
    idempotency_hash: str,
) -> None:
    expected_revision = getattr(store, _STORE_REVISION_ATTR, _MISSING_STORE_REVISION)
    current_revision = _store_file_revision(target)
    if expected_revision != current_revision:
        raise LiveValidationStoreConflictError(
            target,
            "store changed while reconciling an idempotent operation; retry after reloading",
        )
    try:
        _fsync_parent_directory(target)
    except OSError as error:
        durability_error = LiveValidationStoreDurabilityError(target, current_revision)
        _attach_uncertain_operation(
            durability_error,
            operation_key=operation_key,
            idempotency_hash=idempotency_hash,
        )
        raise durability_error from error


def load_live_validation_reports(path: Optional[Path | str] = None) -> Dict[str, Any]:
    target = live_validation_reports_path(path)
    try:
        raw = _read_store_json(target, collection_key="reports")
    except FileNotFoundError:
        return _empty_store()
    raw.setdefault("version", LIVE_VALIDATION_REPORTS_VERSION)
    raw.setdefault("created_at", _now())
    raw.setdefault("updated_at", raw.get("created_at", _now()))
    raw.setdefault("max_entries", DEFAULT_LIVE_VALIDATION_REPORTS_MAX_ENTRIES)
    return raw


def load_live_validation_decisions(path: Optional[Path | str] = None) -> Dict[str, Any]:
    target = live_validation_decisions_path(path)
    try:
        raw = _read_store_json(target, collection_key="decisions")
    except FileNotFoundError:
        return _empty_decision_store()
    raw.setdefault("version", LIVE_VALIDATION_DECISIONS_VERSION)
    raw.setdefault("created_at", _now())
    raw.setdefault("updated_at", raw.get("created_at", _now()))
    return raw


def load_live_validation_promotion_proposal_snapshots(path: Optional[Path | str] = None) -> Dict[str, Any]:
    target = live_validation_promotion_proposal_snapshots_path(path)
    try:
        raw = _read_store_json(target, collection_key="snapshots")
    except FileNotFoundError:
        return _empty_promotion_proposal_snapshot_store()
    raw.setdefault("version", LIVE_VALIDATION_PROMOTION_PROPOSAL_SNAPSHOTS_VERSION)
    raw.setdefault("created_at", _now())
    raw.setdefault("updated_at", raw.get("created_at", _now()))
    raw.setdefault("max_entries", DEFAULT_LIVE_VALIDATION_PROMOTION_PROPOSAL_SNAPSHOTS_MAX_ENTRIES)
    return raw


def _fsync_parent_directory(path: Path) -> None:
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path.parent, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _store_file_revision(target: Path) -> str:
    if not target.exists():
        return _MISSING_STORE_REVISION
    try:
        return hashlib.sha256(target.read_bytes()).hexdigest()
    except OSError:
        raise LiveValidationStoreReadError(target, "existing file is unreadable") from None


def _write_store_atomic(target: Path, store: Mapping[str, Any]) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    serialized = (json.dumps(_jsonable(store), indent=2, sort_keys=True) + "\n").encode("utf-8")
    content_hash = hashlib.sha256(serialized).hexdigest()
    current_revision = _store_file_revision(target)
    expected_revision = getattr(store, _STORE_REVISION_ATTR, _MISSING_STORE_REVISION)
    if expected_revision != current_revision:
        if current_revision == content_hash:
            if isinstance(store, _RevisionedLiveValidationStore):
                setattr(store, _STORE_REVISION_ATTR, content_hash)
            try:
                _fsync_parent_directory(target)
            except OSError as error:
                raise LiveValidationStoreDurabilityError(target, content_hash) from error
            return target
        raise LiveValidationStoreConflictError(
            target,
            "store changed in another writer; reload before saving to avoid overwriting newer evidence",
        )

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            if os.name == "posix":
                os.fchmod(handle.fileno(), 0o600)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        replace_file(temporary, target)
        if isinstance(store, _RevisionedLiveValidationStore):
            setattr(store, _STORE_REVISION_ATTR, content_hash)
        try:
            _fsync_parent_directory(target)
        except OSError as error:
            raise LiveValidationStoreDurabilityError(target, content_hash) from error
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return target


@_locked_store_mutation(live_validation_reports_path, path_parameter="path", positional_index=1)
def save_live_validation_reports(store: Mapping[str, Any], path: Optional[Path | str] = None) -> Path:
    target = live_validation_reports_path(path)
    if target.exists():
        load_live_validation_reports(target)
    _validate_store_for_write(store, collection_key="reports")
    return _write_store_atomic(target, store)


@_locked_store_mutation(live_validation_decisions_path, path_parameter="path", positional_index=1)
def save_live_validation_decisions(store: Mapping[str, Any], path: Optional[Path | str] = None) -> Path:
    target = live_validation_decisions_path(path)
    if target.exists():
        load_live_validation_decisions(target)
    _validate_store_for_write(store, collection_key="decisions")
    return _write_store_atomic(target, store)


@_locked_store_mutation(
    live_validation_promotion_proposal_snapshots_path,
    path_parameter="path",
    positional_index=1,
)
def save_live_validation_promotion_proposal_snapshots(
    store: Mapping[str, Any],
    path: Optional[Path | str] = None,
) -> Path:
    target = live_validation_promotion_proposal_snapshots_path(path)
    if target.exists():
        load_live_validation_promotion_proposal_snapshots(target)
    _validate_store_for_write(store, collection_key="snapshots")
    return _write_store_atomic(target, store)


def redact_live_validation_report(report: Mapping[str, Any]) -> Dict[str, Any]:
    redacted = _redact_value(report)
    if not isinstance(redacted, dict):
        return {}
    return redacted


def live_validation_report_payload_hash(report: Mapping[str, Any]) -> str:
    clean_report = redact_live_validation_report(report)
    body = json.dumps(_jsonable(clean_report), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{POLYMARKET_LIVE_VALIDATION_REPORT_KIND}:payload:{body}".encode("utf-8")).hexdigest()


def find_live_validation_report_duplicate(
    report_or_hash: Mapping[str, Any] | str,
    *,
    path: Optional[Path | str] = None,
    exclude_key: str = "",
) -> Optional[Dict[str, Any]]:
    payload_hash = (
        str(report_or_hash).strip()
        if isinstance(report_or_hash, str)
        else live_validation_report_payload_hash(report_or_hash)
    )
    if not payload_hash:
        return None
    target = live_validation_reports_path(path)
    store = load_live_validation_reports(target)
    reports = store.get("reports") if isinstance(store.get("reports"), Mapping) else {}
    return _find_duplicate_live_validation_report(payload_hash, reports, target, exclude_key=exclude_key)


def _replay_live_validation_report_idempotency(
    *,
    store: Mapping[str, Any],
    reports: Mapping[str, Any],
    target: Path,
    idempotency_hash: str,
    request_hash: str,
    max_entries: int,
) -> Optional[Dict[str, Any]]:
    idempotent_entry = _find_idempotent_entry(reports, idempotency_hash, target=target)
    if idempotent_entry is None:
        return None
    _validate_idempotency_request(
        idempotent_entry,
        idempotency_hash=idempotency_hash,
        request_hash=request_hash,
        target=target,
    )
    _reconcile_committed_store_operation(
        store,
        target,
        operation_key=str(idempotent_entry.get("key") or ""),
        idempotency_hash=idempotency_hash,
    )
    metadata = live_validation_report_metadata(idempotent_entry, target)
    existing_payload = idempotent_entry.get("payload")
    if isinstance(existing_payload, Mapping):
        metadata["summary"] = live_validation_report_summary(existing_payload)
    outcomes = idempotent_entry.get("idempotency_outcomes")
    outcome = outcomes.get(idempotency_hash) if isinstance(outcomes, Mapping) else None
    if isinstance(outcome, Mapping):
        metadata.update(_jsonable(outcome))
    metadata.update(
        {
            "stored": False,
            "idempotent_replay": True,
            "entries": len(reports),
            "max_entries": int(store.get("max_entries") or max_entries),
        }
    )
    return metadata


@_locked_store_mutation(live_validation_reports_path, path_parameter="path")
def reconcile_live_validation_report_idempotency(
    *,
    idempotency_key: str,
    idempotency_request: Mapping[str, Any],
    path: Optional[Path | str] = None,
    max_entries: int = DEFAULT_LIVE_VALIDATION_REPORTS_MAX_ENTRIES,
) -> Optional[Dict[str, Any]]:
    """Reconcile a caller-bound report retry before regenerating its payload."""
    idempotency_hash = _idempotency_key_hash(POLYMARKET_LIVE_VALIDATION_REPORT_KIND, idempotency_key)
    if not idempotency_hash:
        return None
    request_hash = _bound_operation_request_hash(
        POLYMARKET_LIVE_VALIDATION_REPORT_KIND,
        {},
        idempotency_request,
        stable_context={
            "store_path": str(live_validation_reports_path(path)),
            "max_entries": int(max_entries),
        },
    )
    target = live_validation_reports_path(path)
    store = load_live_validation_reports(target)
    reports = store.get("reports")
    if not isinstance(reports, Mapping):
        raise LiveValidationStoreIntegrityError(target, "reports collection is malformed")
    return _replay_live_validation_report_idempotency(
        store=store,
        reports=reports,
        target=target,
        idempotency_hash=idempotency_hash,
        request_hash=request_hash,
        max_entries=max_entries,
    )


@_locked_store_mutation(live_validation_reports_path, path_parameter="path")
def store_live_validation_report(
    report: Mapping[str, Any],
    *,
    source: str = "gui_snapshot",
    label: str = "",
    path: Optional[Path | str] = None,
    max_entries: int = DEFAULT_LIVE_VALIDATION_REPORTS_MAX_ENTRIES,
    source_file: Optional[Path | str] = None,
    allow_duplicate: bool = False,
    skip_duplicate: bool = True,
    idempotency_key: str = "",
    idempotency_request: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    if not isinstance(report, Mapping):
        raise ValueError("Live validation report must be an object.")
    schema_validation = ensure_live_validation_report_valid(report)
    clean_report = redact_live_validation_report(report)
    payload_hash = live_validation_report_payload_hash(clean_report)
    clean_source = str(source or "gui_snapshot").strip() or "gui_snapshot"
    clean_label = str(label or "").strip()
    clean_source_file = str(Path(str(source_file))) if source_file is not None and str(source_file).strip() else ""
    effective_allow_duplicate = bool(allow_duplicate or not skip_duplicate)
    idempotency_hash = _idempotency_key_hash(POLYMARKET_LIVE_VALIDATION_REPORT_KIND, idempotency_key)
    request_hash = _bound_operation_request_hash(
        POLYMARKET_LIVE_VALIDATION_REPORT_KIND,
        {
            "payload_hash": payload_hash,
            "source": clean_source,
            "label": clean_label,
            "source_file": clean_source_file,
            "duplicate_policy": "allow" if effective_allow_duplicate else "skip",
            "max_entries": int(max_entries),
        },
        idempotency_request,
        stable_context={
            "store_path": str(live_validation_reports_path(path)),
            "max_entries": int(max_entries),
        },
    )
    target = live_validation_reports_path(path)
    store = load_live_validation_reports(target)
    reports = store.setdefault("reports", {})
    if not isinstance(reports, dict):
        reports = {}
        store["reports"] = reports
    idempotent_replay = _replay_live_validation_report_idempotency(
        store=store,
        reports=reports,
        target=target,
        idempotency_hash=idempotency_hash,
        request_hash=request_hash,
        max_entries=max_entries,
    )
    if idempotent_replay is not None:
        return idempotent_replay
    now = _now()
    stored_at_ns = time.time_ns()
    duplicate = _find_duplicate_live_validation_report(payload_hash, reports, target)
    summary = live_validation_report_summary(clean_report)
    schema_validation_metadata = compact_schema_validation(schema_validation)
    if duplicate and not effective_allow_duplicate:
        duplicate_key = str(duplicate.get("key") or "")
        duplicate_entry = reports.get(duplicate_key) if isinstance(reports.get(duplicate_key), Mapping) else None
        audit_event = _duplicate_import_event(
            source=clean_source,
            label=clean_label,
            source_file=source_file,
            payload_hash=payload_hash,
            duplicate_of=duplicate_key,
            attempted_at=now,
            attempted_at_ns=stored_at_ns,
        )
        if isinstance(duplicate_entry, dict):
            _register_idempotency_request(
                duplicate_entry,
                idempotency_hash=idempotency_hash,
                request_hash=request_hash,
                target=target,
            )
            if idempotency_hash:
                outcomes = duplicate_entry.get("idempotency_outcomes")
                if outcomes is not None and not isinstance(outcomes, dict):
                    raise ValueError("Stored idempotency outcomes are malformed; no evidence was changed.")
                outcomes = outcomes if isinstance(outcomes, dict) else {}
                outcomes[idempotency_hash] = {
                    "duplicate": True,
                    "duplicate_key": duplicate_key,
                    "duplicate_of": duplicate_key,
                    "duplicate_policy": "skip",
                }
                duplicate_entry["idempotency_outcomes"] = outcomes
            _append_duplicate_import(duplicate_entry, audit_event)
            duplicate_entry["updated_at"] = now
            store["updated_at"] = now
            store["max_entries"] = int(max_entries)
            _save_store_operation(
                save_live_validation_reports,
                store,
                target,
                operation_key=duplicate_key,
                idempotency_hash=idempotency_hash,
            )
            duplicate = live_validation_report_metadata(duplicate_entry, target)
            payload = duplicate_entry.get("payload")
            if isinstance(payload, Mapping):
                duplicate["summary"] = live_validation_report_summary(payload)
        metadata = dict(duplicate)
        metadata.update(
            {
                "stored": False,
                "duplicate": True,
                "duplicate_key": duplicate_key,
                "duplicate_of": duplicate_key,
                "duplicate_policy": "skip",
                "duplicate_audit_event": audit_event,
                "entries": len(reports),
                "max_entries": int(max_entries),
                "summary": summary,
                "schema_validation": schema_validation_metadata,
                "payload_hash": payload_hash,
                "idempotent_replay": False,
                "provenance": _live_validation_report_provenance(
                    payload_hash=payload_hash,
                    source_file=source_file,
                    duplicate_policy="skip",
                    duplicate_of=duplicate_key,
                ),
            }
        )
        return metadata

    key = make_live_validation_report_key(
        clean_report,
        source=clean_source,
        label=clean_label,
        stored_at=now,
        stored_at_ns=stored_at_ns,
    )
    while key in reports:
        stored_at_ns += 1
        key = make_live_validation_report_key(
            clean_report,
            source=clean_source,
            label=clean_label,
            stored_at=now,
            stored_at_ns=stored_at_ns,
        )
    duplicate_key = str(duplicate.get("key") or "") if duplicate else ""
    entry = {
        "key": key,
        "kind": POLYMARKET_LIVE_VALIDATION_REPORT_KIND,
        "source": clean_source,
        "label": clean_label,
        "stored_at": now,
        "stored_at_ns": stored_at_ns,
        "payload_hash": payload_hash,
        "provenance": _live_validation_report_provenance(
            payload_hash=payload_hash,
            source_file=source_file,
            duplicate_policy="allow" if duplicate_key else "unique",
            duplicate_of=duplicate_key or None,
        ),
        "duplicate_of": duplicate_key or None,
        "payload": clean_report,
        "summary": summary,
        "schema_validation": schema_validation_metadata,
        "idempotency_key_hash": idempotency_hash or None,
        "idempotency_request_hash": request_hash if idempotency_hash else None,
        "idempotency_key_hashes": [idempotency_hash] if idempotency_hash else [],
        "idempotency_requests": {idempotency_hash: request_hash} if idempotency_hash else {},
        "idempotency_outcomes": {
            idempotency_hash: {
                "duplicate": bool(duplicate_key),
                "duplicate_key": duplicate_key or None,
                "duplicate_of": duplicate_key or None,
                "duplicate_policy": "allow" if duplicate_key else "unique",
            }
        }
        if idempotency_hash
        else {},
    }
    reports[key] = entry
    _prune_reports(reports, max_entries=max_entries)
    store["updated_at"] = now
    store["max_entries"] = int(max_entries)
    _save_store_operation(
        save_live_validation_reports,
        store,
        target,
        operation_key=key,
        idempotency_hash=idempotency_hash,
    )
    metadata = live_validation_report_metadata(entry, target)
    metadata.update(
        {
            "stored": True,
            "duplicate": bool(duplicate_key),
            "duplicate_key": duplicate_key or None,
            "duplicate_of": duplicate_key or None,
            "duplicate_policy": "allow" if duplicate_key else "unique",
            "entries": len(reports),
            "max_entries": int(max_entries),
            "summary": summary,
            "schema_validation": schema_validation_metadata,
            "idempotent_replay": False,
        }
    )
    return metadata


def list_live_validation_reports(
    *,
    include_payload: bool = False,
    path: Optional[Path | str] = None,
) -> Dict[str, Any]:
    target = live_validation_reports_path(path)
    store = load_live_validation_reports(target)
    rows: List[Dict[str, Any]] = []
    for entry in (store.get("reports") or {}).values():
        if not isinstance(entry, Mapping):
            continue
        row = live_validation_report_metadata(entry, target)
        payload = entry.get("payload")
        if isinstance(payload, Mapping):
            row["summary"] = live_validation_report_summary(payload)
            row["payload_bytes"] = len(json.dumps(_jsonable(payload), sort_keys=True, separators=(",", ":")).encode("utf-8"))
            if include_payload:
                row["payload"] = deepcopy(payload)
        rows.append(row)
    rows.sort(key=lambda item: (float(item.get("stored_at") or 0), float(item.get("stored_at_ns") or 0)), reverse=True)
    hash_counts: Dict[str, int] = {}
    for row in rows:
        payload_hash = str(row.get("payload_hash") or "").strip()
        if payload_hash:
            hash_counts[payload_hash] = hash_counts.get(payload_hash, 0) + 1
    duplicate_imports = 0
    for row in rows:
        payload_hash = str(row.get("payload_hash") or "").strip()
        duplicate_payload_count = hash_counts.get(payload_hash, 0) if payload_hash else 0
        if duplicate_payload_count > 1:
            row["duplicate_payload_count"] = duplicate_payload_count
        duplicate_import_count = int(row.get("duplicate_import_count") or 0)
        duplicate_imports += duplicate_import_count
        row["duplicate"] = bool(row.get("duplicate_of") or duplicate_payload_count > 1 or duplicate_import_count)
    return {
        "source": "polymarket_live_validation_reports",
        "cache": live_validation_reports_health(target),
        "entries": rows,
        "counts": {
            "entries": len(rows),
            "payload_hashes": len(hash_counts),
            "duplicate_payloads": sum(1 for count in hash_counts.values() if count > 1),
            "duplicate_imports": duplicate_imports,
        },
        "comparison": compare_live_validation_report_entries(rows[0], rows[1]) if len(rows) >= 2 else None,
    }


def load_live_validation_report(
    key: str,
    *,
    path: Optional[Path | str] = None,
) -> Optional[Dict[str, Any]]:
    clean_key = str(key or "").strip()
    if not clean_key:
        return None
    target = live_validation_reports_path(path)
    store = load_live_validation_reports(target)
    entry = (store.get("reports") or {}).get(clean_key)
    if not isinstance(entry, Mapping):
        return None
    payload = entry.get("payload")
    if not isinstance(payload, Mapping):
        return None
    metadata = live_validation_report_metadata(entry, target)
    metadata["summary"] = live_validation_report_summary(payload)
    metadata["payload"] = deepcopy(payload)
    return metadata


@_locked_store_mutation(live_validation_reports_path, path_parameter="path")
def purge_live_validation_reports(
    *,
    keys: Optional[Iterable[str]] = None,
    all_entries: bool = False,
    path: Optional[Path | str] = None,
) -> Dict[str, Any]:
    requested_keys = [str(key or "").strip() for key in (keys or []) if str(key or "").strip()]
    if not requested_keys and not all_entries:
        raise ValueError("Report purge requires a key or all=true.")

    target = live_validation_reports_path(path)
    store = load_live_validation_reports(target)
    reports = store.setdefault("reports", {})
    if not isinstance(reports, dict):
        reports = {}
        store["reports"] = reports

    deleted_keys: List[str] = []
    missing_keys: List[str] = []
    if all_entries:
        deleted_keys = [str(key) for key in reports]
        reports.clear()
    else:
        for key in requested_keys:
            if key in reports:
                reports.pop(key, None)
                deleted_keys.append(key)
            else:
                missing_keys.append(key)

    store["updated_at"] = _now()
    save_live_validation_reports(store, target)
    inventory = list_live_validation_reports(path=target)
    inventory.update(
        {
            "deleted": len(deleted_keys),
            "deleted_keys": deleted_keys,
            "missing_keys": missing_keys,
            "requested": len(requested_keys),
            "message": f"Deleted {len(deleted_keys)} live validation report(s).",
        }
    )
    return inventory


def live_validation_reports_health(path: Optional[Path | str] = None) -> Dict[str, Any]:
    target = live_validation_reports_path(path)
    store = load_live_validation_reports(target)
    reports = store.get("reports") or {}
    entries = reports if isinstance(reports, Mapping) else {}
    newest_stored_at: Optional[int] = None
    oldest_stored_at: Optional[int] = None
    payload_hash_counts: Dict[str, int] = {}
    duplicate_imports = 0
    for entry in entries.values():
        if not isinstance(entry, Mapping):
            continue
        payload_hash = _entry_payload_hash(entry)
        if payload_hash:
            payload_hash_counts[payload_hash] = payload_hash_counts.get(payload_hash, 0) + 1
        duplicate_imports += _entry_duplicate_import_count(entry)
        stored_at = _safe_int(entry.get("stored_at"))
        if stored_at is None:
            continue
        newest_stored_at = stored_at if newest_stored_at is None else max(newest_stored_at, stored_at)
        oldest_stored_at = stored_at if oldest_stored_at is None else min(oldest_stored_at, stored_at)
    return {
        "path": str(target),
        "exists": target.exists(),
        "entries": len(entries),
        "max_entries": int(store.get("max_entries") or DEFAULT_LIVE_VALIDATION_REPORTS_MAX_ENTRIES),
        "size_bytes": target.stat().st_size if target.exists() else 0,
        "version": int(store.get("version") or LIVE_VALIDATION_REPORTS_VERSION),
        "created_at": store.get("created_at"),
        "updated_at": store.get("updated_at"),
        "newest_stored_at": newest_stored_at,
        "oldest_stored_at": oldest_stored_at,
        "payload_hashes": len(payload_hash_counts),
        "duplicate_payloads": sum(1 for count in payload_hash_counts.values() if count > 1),
        "duplicate_imports": duplicate_imports,
    }


def live_validation_report_promotion_inventory(path: Optional[Path | str] = None) -> Dict[str, Any]:
    listing = list_live_validation_reports(path=path)
    credential_candidates: List[Dict[str, Any]] = []
    funded_candidates: List[Dict[str, Any]] = []
    blocked_entries: List[Dict[str, Any]] = []
    for entry in listing.get("entries") or []:
        if not isinstance(entry, Mapping):
            continue
        summary = entry.get("summary") if isinstance(entry.get("summary"), Mapping) else {}
        promotion = (
            summary.get("verification_promotion")
            if isinstance(summary.get("verification_promotion"), Mapping)
            else {}
        )
        if summary.get("can_promote_credential_live_verified"):
            credential_candidates.append(_promotion_candidate(entry, promotion))
        if summary.get("can_promote_funded_live_verified"):
            funded_candidates.append(_promotion_candidate(entry, promotion))
        if not summary.get("can_promote_credential_live_verified") or not summary.get("can_promote_funded_live_verified"):
            blocked_entries.append(
                {
                    "key": entry.get("key"),
                    "label": entry.get("label"),
                    "source": entry.get("source"),
                    "credential_live_verified": summary.get("credential_live_verified"),
                    "funded_live_verified": summary.get("funded_live_verified"),
                    "blocked_reasons": promotion.get("blocked_reasons", []),
                }
            )
    return {
        "source": "stored_live_validation_reports",
        "static_coverage_mutated": False,
        "credential_live_verified": "candidate_only" if credential_candidates else "blocked",
        "funded_live_verified": "candidate_only" if funded_candidates else "blocked",
        "attested_workflow_verified": False,
        "evidence_trust": "local_unattested_candidate",
        "credential_candidates": credential_candidates,
        "funded_candidates": funded_candidates,
        "blocked_entries": blocked_entries[:10],
        "counts": {
            "reports": int((listing.get("counts") or {}).get("entries") or 0),
            "credential_candidates": len(credential_candidates),
            "funded_candidates": len(funded_candidates),
            "blocked_entries": len(blocked_entries),
        },
        "note": (
            "Stored reports are local unattested evidence candidates only. They never establish a verified "
            "credentialed or funded tier; production verification requires a trusted workflow and exact-byte attestation."
        ),
    }


def live_validation_report_review_bundle(
    key: str,
    *,
    path: Optional[Path | str] = None,
) -> Optional[Dict[str, Any]]:
    clean_key = str(key or "").strip()
    if not clean_key:
        return None
    target = live_validation_reports_path(path)
    store = load_live_validation_reports(target)
    entry = (store.get("reports") or {}).get(clean_key)
    if not isinstance(entry, Mapping):
        return None
    payload = entry.get("payload")
    if not isinstance(payload, Mapping):
        return None

    metadata = live_validation_report_metadata(entry, target)
    summary = live_validation_report_summary(payload)
    promotion = (
        summary.get("verification_promotion")
        if isinstance(summary.get("verification_promotion"), Mapping)
        else live_validation_report_promotion(payload)
    )
    schema_validation = metadata.get("schema_validation")
    if not isinstance(schema_validation, Mapping):
        schema_validation = compact_schema_validation(validate_live_validation_report(payload))

    bundle = {
        "source": POLYMARKET_LIVE_VALIDATION_REPORT_REVIEW_KIND,
        "kind": POLYMARKET_LIVE_VALIDATION_REPORT_REVIEW_KIND,
        "bundle_version": LIVE_VALIDATION_REPORT_REVIEW_BUNDLE_VERSION,
        "generated_at": _now(),
        "funded_execution_exposed": False,
        "static_coverage_mutated": False,
        "report": {
            "key": metadata.get("key"),
            "kind": metadata.get("kind"),
            "label": metadata.get("label"),
            "source": metadata.get("source"),
            "stored_at": metadata.get("stored_at"),
            "stored_at_ns": metadata.get("stored_at_ns"),
            "payload_bytes": metadata.get("payload_bytes"),
            "payload_hash": metadata.get("payload_hash"),
            "provenance": metadata.get("provenance", {}),
            "summary": _review_summary(summary),
        },
        "schema_validation": compact_schema_validation(schema_validation),
        "duplicate_history": _review_duplicate_history(entry, metadata),
        "promotion_review": _review_promotion(payload, promotion),
        "operator_commands": _review_operator_commands(payload),
        "coverage_tier_mapping": _review_coverage_tier_mapping(summary, promotion),
        "review_notes": [
            "This review bundle is sanitized operator evidence only.",
            "It does not include the raw live-validation payload.",
            "It does not mutate static coverage tiers or enable credentialed/funded production claims by itself.",
        ],
    }
    bundle["review_bundle_hash"] = live_validation_report_review_bundle_hash(bundle)
    return bundle


def live_validation_report_review_bundle_hash(bundle: Mapping[str, Any]) -> str:
    canonical = _jsonable(bundle)
    if isinstance(canonical, dict):
        canonical.pop("generated_at", None)
        canonical.pop("review_bundle_hash", None)
    body = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(
        f"{POLYMARKET_LIVE_VALIDATION_REPORT_REVIEW_KIND}:bundle:{body}".encode("utf-8")
    ).hexdigest()


def live_validation_report_review_markdown(bundle: Mapping[str, Any]) -> str:
    report = bundle.get("report") if isinstance(bundle.get("report"), Mapping) else {}
    schema = bundle.get("schema_validation") if isinstance(bundle.get("schema_validation"), Mapping) else {}
    duplicate_history = bundle.get("duplicate_history") if isinstance(bundle.get("duplicate_history"), Mapping) else {}
    promotion = bundle.get("promotion_review") if isinstance(bundle.get("promotion_review"), Mapping) else {}
    coverage = bundle.get("coverage_tier_mapping") if isinstance(bundle.get("coverage_tier_mapping"), Mapping) else {}
    commands = bundle.get("operator_commands") if isinstance(bundle.get("operator_commands"), Mapping) else {}
    blockers = promotion.get("blocked_reasons") if isinstance(promotion.get("blocked_reasons"), list) else []
    evidence = promotion.get("evidence") if isinstance(promotion.get("evidence"), Mapping) else {}

    lines = [
        "# Polymarket Live Validation Review Bundle",
        "",
        f"- Bundle version: {bundle.get('bundle_version')}",
        f"- Generated at: {bundle.get('generated_at')}",
        f"- Review bundle hash: {bundle.get('review_bundle_hash') or '-'}",
        f"- Static coverage mutated: {str(bool(bundle.get('static_coverage_mutated'))).lower()}",
        f"- Funded execution exposed: {str(bool(bundle.get('funded_execution_exposed'))).lower()}",
        "",
        "## Report",
        "",
        f"- Key: {report.get('key') or '-'}",
        f"- Label: {report.get('label') or '-'}",
        f"- Source: {report.get('source') or '-'}",
        f"- Stored at: {report.get('stored_at') or '-'}",
        f"- Payload hash: {report.get('payload_hash') or '-'}",
        f"- Payload bytes: {report.get('payload_bytes') or '-'}",
        "",
        "## Schema",
        "",
        f"- Accepted: {str(bool(schema.get('ok'))).lower()}",
        f"- Mode: {schema.get('mode') or '-'}",
        f"- Report type: {schema.get('report_type') or '-'}",
        f"- Errors: {len(schema.get('errors') or [])}",
        f"- Warnings: {len(schema.get('warnings') or [])}",
        "",
        "## Promotion Review",
        "",
        f"- Credential live verified: {promotion.get('credential_live_verified') or 'blocked'}",
        f"- Funded live verified: {promotion.get('funded_live_verified') or 'blocked'}",
        f"- Can promote credential tier: {str(bool(promotion.get('can_promote_credential_live_verified'))).lower()}",
        f"- Can promote funded tier: {str(bool(promotion.get('can_promote_funded_live_verified'))).lower()}",
        "",
        "### Evidence",
        "",
        f"- Credential evidence rows: {len(evidence.get('credential') or [])}",
        f"- Funded evidence rows: {len(evidence.get('funded') or [])}",
        "",
        "### Blockers",
        "",
    ]
    if blockers:
        lines.extend(f"- {item}" for item in blockers)
    else:
        lines.append("- None")

    lines.extend(
        [
            "",
            "## Duplicate History",
            "",
            f"- Duplicate of: {duplicate_history.get('duplicate_of') or '-'}",
            f"- Duplicate import count: {duplicate_history.get('duplicate_import_count') or 0}",
            f"- Retained duplicate events: {len(duplicate_history.get('duplicate_imports') or [])}",
            "",
            "## Coverage Tier Mapping",
            "",
            "| Tier | Static states | Report status | Review effect |",
            "| --- | --- | --- | --- |",
        ]
    )
    levels = coverage.get("levels") if isinstance(coverage.get("levels"), Mapping) else {}
    for tier in ("public_live_verified", "credential_live_verified", "funded_live_verified"):
        item = levels.get(tier) if isinstance(levels.get(tier), Mapping) else {}
        lines.append(
            "| {tier} | {states} | {status} | {effect} |".format(
                tier=tier,
                states=_markdown_cell(item.get("static_category_states") or {}),
                status=_markdown_cell(item.get("report_status") or "-"),
                effect=_markdown_cell(item.get("review_effect") or "-"),
            )
        )

    lines.extend(["", "## Operator Commands", ""])
    if commands:
        for name, command in commands.items():
            lines.extend([f"### {name}", "", "```powershell", str(command), "```", ""])
    else:
        lines.append("- None")

    lines.extend(["", "## Review Notes", ""])
    for note in bundle.get("review_notes") or []:
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


def live_validation_report_review_export_filename(key: str, extension: str) -> str:
    clean_key = "".join(char for char in str(key or "") if char.isalnum() or char in {"-", "_"})[:80]
    clean_extension = str(extension or "json").strip().lstrip(".") or "json"
    return f"polymarket-live-validation-review-{clean_key or 'report'}.{clean_extension}"


@_locked_store_mutation(live_validation_decisions_path, path_parameter="decision_path")
def record_live_validation_report_decision(
    *,
    report_key: str,
    payload_hash: str,
    target_tier: str,
    decision: str,
    reviewer_note: str,
    review_bundle_hash: str,
    reviewer: str = "",
    report_store_path: Optional[Path | str] = None,
    decision_path: Optional[Path | str] = None,
    idempotency_key: str = "",
    idempotency_request: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    clean_report_key = str(report_key or "").strip()
    clean_payload_hash = str(payload_hash or "").strip()
    clean_target_tier = str(target_tier or "").strip()
    clean_decision = str(decision or "").strip().lower()
    clean_note = str(reviewer_note or "").strip()
    clean_review_hash = str(review_bundle_hash or "").strip()
    clean_reviewer = str(reviewer or "").strip() or "operator"

    missing = []
    if not clean_report_key:
        missing.append("report_key")
    if not clean_payload_hash:
        missing.append("payload_hash")
    if not clean_target_tier:
        missing.append("target_tier")
    if not clean_decision:
        missing.append("decision")
    if not clean_note:
        missing.append("reviewer_note")
    if not clean_review_hash:
        missing.append("review_bundle_hash")
    if missing:
        raise ValueError("Decision requires: " + ", ".join(missing) + ".")
    if clean_target_tier not in LIVE_VALIDATION_DECISION_TARGET_TIERS:
        raise ValueError(
            "target_tier must be one of: " + ", ".join(LIVE_VALIDATION_DECISION_TARGET_TIERS) + "."
        )
    if clean_decision not in LIVE_VALIDATION_DECISIONS:
        raise ValueError("decision must be one of: " + ", ".join(LIVE_VALIDATION_DECISIONS) + ".")

    idempotency_hash = _idempotency_key_hash(POLYMARKET_LIVE_VALIDATION_DECISION_KIND, idempotency_key)
    request_hash = _bound_operation_request_hash(
        POLYMARKET_LIVE_VALIDATION_DECISION_KIND,
        {
            "report_key": clean_report_key,
            "payload_hash": clean_payload_hash,
            "target_tier": clean_target_tier,
            "decision": clean_decision,
            "reviewer_note": clean_note,
            "review_bundle_hash": clean_review_hash,
            "reviewer": clean_reviewer,
            "report_store_path": str(live_validation_reports_path(report_store_path)),
        },
        idempotency_request,
        stable_context={
            "decision_path": str(live_validation_decisions_path(decision_path)),
            "report_store_path": str(live_validation_reports_path(report_store_path)),
        },
    )
    target = live_validation_decisions_path(decision_path)
    store = load_live_validation_decisions(target)
    decisions = store.setdefault("decisions", {})
    if not isinstance(decisions, dict):
        decisions = {}
        store["decisions"] = decisions
    idempotent_entry = _find_idempotent_entry(decisions, idempotency_hash, target=target)
    if idempotent_entry is not None:
        _validate_idempotency_request(
            idempotent_entry,
            idempotency_hash=idempotency_hash,
            request_hash=request_hash,
            target=target,
        )
        _reconcile_committed_store_operation(
            store,
            target,
            operation_key=str(idempotent_entry.get("key") or ""),
            idempotency_hash=idempotency_hash,
        )
        metadata = live_validation_report_decision_metadata(idempotent_entry, target)
        metadata.update({"stored": False, "idempotent_replay": True, "entries": len(decisions)})
        return metadata

    bundle = live_validation_report_review_bundle(clean_report_key, path=report_store_path)
    if bundle is None:
        raise ValueError("Unknown live validation report for decision.")
    bundle_payload_hash = str(((bundle.get("report") or {}).get("payload_hash") if isinstance(bundle.get("report"), Mapping) else "") or "")
    if clean_payload_hash != bundle_payload_hash:
        raise ValueError("payload_hash mismatch: decision does not match the stored report.")
    computed_review_hash = live_validation_report_review_bundle_hash(bundle)
    if clean_review_hash != computed_review_hash:
        raise ValueError("review_bundle_hash mismatch: review evidence changed or was tampered with.")

    coverage_levels = (
        (bundle.get("coverage_tier_mapping") or {}).get("levels")
        if isinstance(bundle.get("coverage_tier_mapping"), Mapping)
        else {}
    )
    target_mapping = coverage_levels.get(clean_target_tier) if isinstance(coverage_levels, Mapping) else {}
    can_promote = bool(target_mapping.get("can_promote_from_report")) if isinstance(target_mapping, Mapping) else False
    if clean_decision == "accepted" and clean_target_tier != "public_live_verified" and not can_promote:
        raise ValueError(f"Cannot accept {clean_target_tier}: review bundle marks the tier as blocked.")

    now = _now()
    created_at_ns = time.time_ns()
    decision_key = make_live_validation_decision_key(
        report_key=clean_report_key,
        payload_hash=clean_payload_hash,
        target_tier=clean_target_tier,
        decision=clean_decision,
        reviewer_note=clean_note,
        review_bundle_hash=clean_review_hash,
        created_at=now,
        created_at_ns=created_at_ns,
    )
    while decision_key in decisions:
        created_at_ns += 1
        decision_key = make_live_validation_decision_key(
            report_key=clean_report_key,
            payload_hash=clean_payload_hash,
            target_tier=clean_target_tier,
            decision=clean_decision,
            reviewer_note=clean_note,
            review_bundle_hash=clean_review_hash,
            created_at=now,
            created_at_ns=created_at_ns,
        )
    record = {
        "key": decision_key,
        "kind": POLYMARKET_LIVE_VALIDATION_DECISION_KIND,
        "created_at": now,
        "created_at_ns": created_at_ns,
        "report_key": clean_report_key,
        "payload_hash": clean_payload_hash,
        "target_tier": clean_target_tier,
        "decision": clean_decision,
        "reviewer": clean_reviewer,
        "reviewer_note": clean_note,
        "review_bundle_hash": clean_review_hash,
        "review_bundle_hash_verified": True,
        "static_coverage_mutated": False,
        "funded_execution_exposed": False,
        "promotion_effect": "ledger_only_no_static_coverage_mutation",
        "report_label": (bundle.get("report") or {}).get("label") if isinstance(bundle.get("report"), Mapping) else "",
        "report_source": (bundle.get("report") or {}).get("source") if isinstance(bundle.get("report"), Mapping) else "",
        "coverage_tier_decision": _jsonable(target_mapping) if isinstance(target_mapping, Mapping) else {},
        "promotion_review": _jsonable(bundle.get("promotion_review") or {}),
        "idempotency_key_hash": idempotency_hash or None,
        "idempotency_request_hash": request_hash if idempotency_hash else None,
        "idempotency_key_hashes": [idempotency_hash] if idempotency_hash else [],
        "idempotency_requests": {idempotency_hash: request_hash} if idempotency_hash else {},
    }

    decisions[record["key"]] = record
    store["updated_at"] = now
    _save_store_operation(
        save_live_validation_decisions,
        store,
        target,
        operation_key=str(record["key"]),
        idempotency_hash=idempotency_hash,
    )
    metadata = live_validation_report_decision_metadata(record, target)
    metadata.update({"stored": True, "idempotent_replay": False, "entries": len(decisions)})
    return metadata


def list_live_validation_report_decisions(
    *,
    report_key: str = "",
    path: Optional[Path | str] = None,
) -> Dict[str, Any]:
    target = live_validation_decisions_path(path)
    store = load_live_validation_decisions(target)
    rows = []
    for entry in (store.get("decisions") or {}).values():
        if not isinstance(entry, Mapping):
            continue
        if report_key and entry.get("report_key") != report_key:
            continue
        rows.append(live_validation_report_decision_metadata(entry, target))
    rows.sort(key=lambda item: (float(item.get("created_at") or 0), float(item.get("created_at_ns") or 0)), reverse=True)
    counts_by_decision: Dict[str, int] = {}
    counts_by_tier: Dict[str, int] = {}
    for row in rows:
        counts_by_decision[str(row.get("decision") or "unknown")] = counts_by_decision.get(str(row.get("decision") or "unknown"), 0) + 1
        counts_by_tier[str(row.get("target_tier") or "unknown")] = counts_by_tier.get(str(row.get("target_tier") or "unknown"), 0) + 1
    return {
        "source": "polymarket_live_validation_decision_ledger",
        "kind": POLYMARKET_LIVE_VALIDATION_DECISION_KIND,
        "cache": live_validation_report_decisions_health(target),
        "entries": rows,
        "counts": {
            "entries": len(rows),
            "accepted": counts_by_decision.get("accepted", 0),
            "rejected": counts_by_decision.get("rejected", 0),
            "by_decision": counts_by_decision,
            "by_tier": counts_by_tier,
        },
        "static_coverage_mutated": False,
        "funded_execution_exposed": False,
    }


def live_validation_report_decisions_health(path: Optional[Path | str] = None) -> Dict[str, Any]:
    target = live_validation_decisions_path(path)
    store = load_live_validation_decisions(target)
    decisions = store.get("decisions") if isinstance(store.get("decisions"), Mapping) else {}
    return {
        "path": str(target),
        "exists": target.exists(),
        "entries": len(decisions),
        "size_bytes": target.stat().st_size if target.exists() else 0,
        "version": int(store.get("version") or LIVE_VALIDATION_DECISIONS_VERSION),
        "created_at": store.get("created_at"),
        "updated_at": store.get("updated_at"),
    }


def live_validation_report_decision_metadata(entry: Mapping[str, Any], path: Path) -> Dict[str, Any]:
    return {
        "key": entry.get("key"),
        "kind": entry.get("kind") or POLYMARKET_LIVE_VALIDATION_DECISION_KIND,
        "created_at": _safe_int(entry.get("created_at")),
        "created_at_ns": _safe_int(entry.get("created_at_ns")),
        "report_key": entry.get("report_key"),
        "payload_hash": entry.get("payload_hash"),
        "target_tier": entry.get("target_tier"),
        "decision": entry.get("decision"),
        "reviewer": entry.get("reviewer") or "operator",
        "reviewer_note": entry.get("reviewer_note") or "",
        "review_bundle_hash": entry.get("review_bundle_hash"),
        "review_bundle_hash_verified": bool(entry.get("review_bundle_hash_verified")),
        "static_coverage_mutated": bool(entry.get("static_coverage_mutated")),
        "funded_execution_exposed": bool(entry.get("funded_execution_exposed")),
        "promotion_effect": entry.get("promotion_effect") or "ledger_only_no_static_coverage_mutation",
        "report_label": entry.get("report_label") or "",
        "report_source": entry.get("report_source") or "",
        "coverage_tier_decision": _jsonable(entry.get("coverage_tier_decision") or {}),
        "path": str(path),
    }


def live_validation_report_decisions_markdown(payload: Mapping[str, Any]) -> str:
    rows = payload.get("entries") if isinstance(payload.get("entries"), list) else []
    lines = [
        "# Polymarket Live Validation Promotion Decision Ledger",
        "",
        f"- Static coverage mutated: {str(bool(payload.get('static_coverage_mutated'))).lower()}",
        f"- Funded execution exposed: {str(bool(payload.get('funded_execution_exposed'))).lower()}",
        f"- Entries: {len(rows)}",
        "",
        "| Created | Report key | Tier | Decision | Reviewer | Payload hash | Review bundle hash | Note |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        lines.append(
            "| {created} | {report_key} | {tier} | {decision} | {reviewer} | {payload_hash} | {bundle_hash} | {note} |".format(
                created=_markdown_cell(row.get("created_at") or "-"),
                report_key=_markdown_cell(row.get("report_key") or "-"),
                tier=_markdown_cell(row.get("target_tier") or "-"),
                decision=_markdown_cell(row.get("decision") or "-"),
                reviewer=_markdown_cell(row.get("reviewer") or "-"),
                payload_hash=_markdown_cell(row.get("payload_hash") or "-"),
                bundle_hash=_markdown_cell(row.get("review_bundle_hash") or "-"),
                note=_markdown_cell(row.get("reviewer_note") or "-"),
            )
        )
    lines.extend(
        [
            "",
            "This ledger records operator decisions only. It does not mutate static coverage tiers.",
            "",
        ]
    )
    return "\n".join(lines)


def live_validation_coverage_promotion_proposal(
    *,
    report_store_path: Optional[Path | str] = None,
    decision_path: Optional[Path | str] = None,
    target_tier: str = "",
) -> Dict[str, Any]:
    clean_target = str(target_tier or "").strip()
    if clean_target and clean_target not in LIVE_VALIDATION_DECISION_TARGET_TIERS:
        raise ValueError(
            "target_tier must be one of: " + ", ".join(LIVE_VALIDATION_DECISION_TARGET_TIERS) + "."
        )

    report_target = live_validation_reports_path(report_store_path)
    decision_target = live_validation_decisions_path(decision_path)
    ledger = list_live_validation_report_decisions(path=decision_target)
    accepted_decisions: List[Dict[str, Any]] = []
    stale_decisions: List[Dict[str, Any]] = []
    ignored_decisions: List[Dict[str, Any]] = []
    proposed_changes: List[Dict[str, Any]] = []

    for entry in ledger.get("entries") or []:
        if not isinstance(entry, Mapping):
            continue
        if clean_target and entry.get("target_tier") != clean_target:
            continue
        if str(entry.get("decision") or "").lower() != "accepted":
            ignored_decisions.append(
                {
                    "decision_key": entry.get("key"),
                    "report_key": entry.get("report_key"),
                    "target_tier": entry.get("target_tier"),
                    "decision": entry.get("decision"),
                    "reason": "decision_not_accepted",
                }
            )
            continue

        candidate = _live_validation_promotion_proposal_candidate(entry, report_target)
        if candidate.get("stale"):
            stale_decisions.append(candidate)
        else:
            accepted_decisions.append(candidate)
            proposed_changes.extend(_live_validation_promotion_proposal_changes(candidate))

    proposal = {
        "source": POLYMARKET_LIVE_VALIDATION_PROMOTION_PROPOSAL_KIND,
        "kind": POLYMARKET_LIVE_VALIDATION_PROMOTION_PROPOSAL_KIND,
        "proposal_version": LIVE_VALIDATION_PROMOTION_PROPOSAL_VERSION,
        "generated_at": _now(),
        "target_tier_filter": clean_target or None,
        "human_review_required": True,
        "automerge_enabled": False,
        "apply_by_default": False,
        "static_coverage_mutated": False,
        "funded_execution_exposed": False,
        "report_store": {
            "path": str(report_target),
            "exists": report_target.exists(),
        },
        "decision_ledger": {
            "path": str(decision_target),
            "cache": ledger.get("cache", {}),
        },
        "review_gates": _live_validation_promotion_proposal_gates(),
        "accepted_decisions": accepted_decisions,
        "stale_decisions": stale_decisions,
        "ignored_decisions": ignored_decisions[:25],
        "proposed_changes": proposed_changes,
        "patch_proposal": {
            "format": "manual_review_patch_proposal",
            "automerge_enabled": False,
            "apply_by_default": False,
            "static_mutation_allowed": False,
            "files": sorted({str(item.get("path") or "") for item in proposed_changes if item.get("path")}),
            "instructions": [
                "Review every accepted decision and current review-bundle hash before editing coverage/docs.",
                "Do not apply this proposal automatically; it is an export for human-authored code/docs changes.",
                "Run focused tests plus the full verifier after any manual coverage/docs patch.",
            ],
        },
        "counts": {
            "ledger_entries": int((ledger.get("counts") or {}).get("entries") or 0),
            "accepted_candidates": len(accepted_decisions),
            "stale_decisions": len(stale_decisions),
            "ignored_decisions": len(ignored_decisions),
            "proposed_changes": len(proposed_changes),
        },
        "notes": [
            "This proposal is derived from accepted ledger decisions only.",
            "It does not mutate static coverage, README, GOAL, or docs by itself.",
            "Stale decisions must be re-reviewed before any coverage promotion is considered.",
        ],
    }
    proposal["proposal_hash"] = live_validation_coverage_promotion_proposal_hash(proposal)
    return proposal


def live_validation_coverage_promotion_proposal_hash(proposal: Mapping[str, Any]) -> str:
    canonical = _jsonable(proposal)
    if isinstance(canonical, dict):
        canonical.pop("generated_at", None)
        canonical.pop("proposal_hash", None)
    body = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(
        f"{POLYMARKET_LIVE_VALIDATION_PROMOTION_PROPOSAL_KIND}:proposal:{body}".encode("utf-8")
    ).hexdigest()


def live_validation_coverage_promotion_proposal_markdown(proposal: Mapping[str, Any]) -> str:
    counts = proposal.get("counts") if isinstance(proposal.get("counts"), Mapping) else {}
    gates = proposal.get("review_gates") if isinstance(proposal.get("review_gates"), list) else []
    accepted = proposal.get("accepted_decisions") if isinstance(proposal.get("accepted_decisions"), list) else []
    stale = proposal.get("stale_decisions") if isinstance(proposal.get("stale_decisions"), list) else []
    changes = proposal.get("proposed_changes") if isinstance(proposal.get("proposed_changes"), list) else []
    patch = proposal.get("patch_proposal") if isinstance(proposal.get("patch_proposal"), Mapping) else {}

    lines = [
        "# Polymarket Live Validation Coverage Promotion Proposal",
        "",
        f"- Proposal version: {proposal.get('proposal_version')}",
        f"- Generated at: {proposal.get('generated_at')}",
        f"- Proposal hash: {proposal.get('proposal_hash') or '-'}",
        f"- Human review required: {str(bool(proposal.get('human_review_required'))).lower()}",
        f"- Automerge enabled: {str(bool(proposal.get('automerge_enabled'))).lower()}",
        f"- Apply by default: {str(bool(proposal.get('apply_by_default'))).lower()}",
        f"- Static coverage mutated: {str(bool(proposal.get('static_coverage_mutated'))).lower()}",
        f"- Funded execution exposed: {str(bool(proposal.get('funded_execution_exposed'))).lower()}",
        "",
        "## Counts",
        "",
        f"- Ledger entries: {counts.get('ledger_entries') or 0}",
        f"- Accepted candidates: {counts.get('accepted_candidates') or 0}",
        f"- Stale decisions: {counts.get('stale_decisions') or 0}",
        f"- Ignored decisions: {counts.get('ignored_decisions') or 0}",
        f"- Proposed changes: {counts.get('proposed_changes') or 0}",
        "",
        "## Review Gates",
        "",
    ]
    for gate in gates:
        if isinstance(gate, Mapping):
            lines.append(
                "- {gate}: {status} - {description}".format(
                    gate=_markdown_cell(gate.get("gate") or "-"),
                    status=_markdown_cell(gate.get("status") or "-"),
                    description=_markdown_cell(gate.get("description") or "-"),
                )
            )
    if not gates:
        lines.append("- None")

    lines.extend(
        [
            "",
            "## Accepted Decisions",
            "",
            "| Decision key | Report key | Tier | Reviewer | Current hash | Proposed effect |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in accepted:
        if not isinstance(row, Mapping):
            continue
        lines.append(
            "| {decision_key} | {report_key} | {tier} | {reviewer} | {hash} | {effect} |".format(
                decision_key=_markdown_cell(row.get("decision_key") or "-"),
                report_key=_markdown_cell(row.get("report_key") or "-"),
                tier=_markdown_cell(row.get("target_tier") or "-"),
                reviewer=_markdown_cell(row.get("reviewer") or "-"),
                hash=_markdown_cell(row.get("current_review_bundle_hash") or "-"),
                effect=_markdown_cell(row.get("proposal_effect") or "-"),
            )
        )
    if not accepted:
        lines.append("| - | - | - | - | - | - |")

    lines.extend(
        [
            "",
            "## Stale Decisions",
            "",
            "| Decision key | Report key | Tier | Reasons |",
            "| --- | --- | --- | --- |",
        ]
    )
    for row in stale:
        if not isinstance(row, Mapping):
            continue
        lines.append(
            "| {decision_key} | {report_key} | {tier} | {reasons} |".format(
                decision_key=_markdown_cell(row.get("decision_key") or "-"),
                report_key=_markdown_cell(row.get("report_key") or "-"),
                tier=_markdown_cell(row.get("target_tier") or "-"),
                reasons=_markdown_cell(", ".join(str(item) for item in row.get("stale_reasons") or []) or "-"),
            )
        )
    if not stale:
        lines.append("| - | - | - | - |")

    lines.extend(
        [
            "",
            "## Proposed Manual Changes",
            "",
            "| File | Target tier | Action | Evidence |",
            "| --- | --- | --- | --- |",
        ]
    )
    for change in changes:
        if not isinstance(change, Mapping):
            continue
        lines.append(
            "| {path} | {tier} | {action} | {evidence} |".format(
                path=_markdown_cell(change.get("path") or "-"),
                tier=_markdown_cell(change.get("target_tier") or "-"),
                action=_markdown_cell(change.get("action") or "-"),
                evidence=_markdown_cell(change.get("evidence") or "-"),
            )
        )
    if not changes:
        lines.append("| - | - | - | - |")

    lines.extend(["", "## Patch Proposal", ""])
    for instruction in patch.get("instructions") or []:
        lines.append(f"- {instruction}")
    lines.extend(
        [
            "",
            "This export is a proposal only. It has `automerge_enabled=false` and does not mutate static coverage.",
            "",
        ]
    )
    return "\n".join(lines)


def live_validation_coverage_promotion_proposal_export_filename(extension: str) -> str:
    clean_extension = str(extension or "json").strip().lstrip(".") or "json"
    return f"polymarket-live-validation-promotion-proposal.{clean_extension}"


def _replay_live_validation_promotion_snapshot_idempotency(
    *,
    store: Mapping[str, Any],
    snapshots: Mapping[str, Any],
    target: Path,
    idempotency_hash: str,
    request_hash: str,
    report_store_path: Optional[Path | str],
    decision_path: Optional[Path | str],
) -> Optional[Dict[str, Any]]:
    idempotent_entry = _find_idempotent_entry(snapshots, idempotency_hash, target=target)
    if idempotent_entry is None:
        return None
    _validate_idempotency_request(
        idempotent_entry,
        idempotency_hash=idempotency_hash,
        request_hash=request_hash,
        target=target,
    )
    _reconcile_committed_store_operation(
        store,
        target,
        operation_key=str(idempotent_entry.get("key") or ""),
        idempotency_hash=idempotency_hash,
    )
    metadata = live_validation_promotion_proposal_snapshot_metadata(
        idempotent_entry,
        target,
        report_store_path=report_store_path,
        decision_path=decision_path,
    )
    metadata.update({"stored": False, "idempotent_replay": True, "entries": len(snapshots)})
    return metadata


@_locked_store_mutation(live_validation_promotion_proposal_snapshots_path, path_parameter="path")
def reconcile_live_validation_promotion_proposal_snapshot_idempotency(
    *,
    idempotency_key: str,
    idempotency_request: Mapping[str, Any],
    report_store_path: Optional[Path | str] = None,
    decision_path: Optional[Path | str] = None,
    path: Optional[Path | str] = None,
    max_entries: int = DEFAULT_LIVE_VALIDATION_PROMOTION_PROPOSAL_SNAPSHOTS_MAX_ENTRIES,
) -> Optional[Dict[str, Any]]:
    """Reconcile a caller-bound snapshot retry before regenerating its proposal."""
    idempotency_hash = _idempotency_key_hash(
        POLYMARKET_LIVE_VALIDATION_PROMOTION_PROPOSAL_SNAPSHOT_KIND,
        idempotency_key,
    )
    if not idempotency_hash:
        return None
    request_hash = _bound_operation_request_hash(
        POLYMARKET_LIVE_VALIDATION_PROMOTION_PROPOSAL_SNAPSHOT_KIND,
        {},
        idempotency_request,
        stable_context={
            "snapshot_path": str(live_validation_promotion_proposal_snapshots_path(path)),
            "report_store_path": str(live_validation_reports_path(report_store_path)),
            "decision_path": str(live_validation_decisions_path(decision_path)),
            "max_entries": int(max_entries),
        },
    )
    target = live_validation_promotion_proposal_snapshots_path(path)
    store = load_live_validation_promotion_proposal_snapshots(target)
    snapshots = store.get("snapshots")
    if not isinstance(snapshots, Mapping):
        raise LiveValidationStoreIntegrityError(target, "snapshots collection is malformed")
    return _replay_live_validation_promotion_snapshot_idempotency(
        store=store,
        snapshots=snapshots,
        target=target,
        idempotency_hash=idempotency_hash,
        request_hash=request_hash,
        report_store_path=report_store_path,
        decision_path=decision_path,
    )


@_locked_store_mutation(live_validation_promotion_proposal_snapshots_path, path_parameter="path")
def store_live_validation_coverage_promotion_proposal_snapshot(
    *,
    proposal: Optional[Mapping[str, Any]] = None,
    report_store_path: Optional[Path | str] = None,
    decision_path: Optional[Path | str] = None,
    target_tier: str = "",
    path: Optional[Path | str] = None,
    source: str = "react_preview",
    label: str = "",
    max_entries: int = DEFAULT_LIVE_VALIDATION_PROMOTION_PROPOSAL_SNAPSHOTS_MAX_ENTRIES,
    idempotency_key: str = "",
    idempotency_request: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    clean_target = str(target_tier or "").strip()
    clean_source = str(source or "react_preview").strip() or "react_preview"
    proposal_payload = (
        _jsonable(proposal)
        if isinstance(proposal, Mapping)
        else live_validation_coverage_promotion_proposal(
            report_store_path=report_store_path,
            decision_path=decision_path,
            target_tier=clean_target,
        )
    )
    if not isinstance(proposal_payload, dict):
        raise ValueError("Promotion proposal snapshot requires a proposal object.")
    if proposal_payload.get("kind") != POLYMARKET_LIVE_VALIDATION_PROMOTION_PROPOSAL_KIND:
        raise ValueError("Promotion proposal snapshot requires a coverage promotion proposal payload.")
    expected_hash = live_validation_coverage_promotion_proposal_hash(proposal_payload)
    supplied_hash = str(proposal_payload.get("proposal_hash") or "").strip()
    if supplied_hash and supplied_hash != expected_hash:
        raise ValueError("proposal_hash mismatch: snapshot proposal does not match its canonical hash.")
    proposal_payload["proposal_hash"] = expected_hash

    effective_target = str(proposal_payload.get("target_tier_filter") or clean_target or "").strip()
    clean_label = str(label or "").strip() or _proposal_snapshot_default_label(proposal_payload)
    idempotency_hash = _idempotency_key_hash(
        POLYMARKET_LIVE_VALIDATION_PROMOTION_PROPOSAL_SNAPSHOT_KIND,
        idempotency_key,
    )
    request_hash = _bound_operation_request_hash(
        POLYMARKET_LIVE_VALIDATION_PROMOTION_PROPOSAL_SNAPSHOT_KIND,
        {
            "proposal_hash": expected_hash,
            "target_tier": effective_target,
            "source": clean_source,
            "label": clean_label,
            "report_store_path": str(live_validation_reports_path(report_store_path)),
            "decision_path": str(live_validation_decisions_path(decision_path)),
            "max_entries": int(max_entries),
        },
        idempotency_request,
        stable_context={
            "snapshot_path": str(live_validation_promotion_proposal_snapshots_path(path)),
            "report_store_path": str(live_validation_reports_path(report_store_path)),
            "decision_path": str(live_validation_decisions_path(decision_path)),
            "max_entries": int(max_entries),
        },
    )
    target = live_validation_promotion_proposal_snapshots_path(path)
    store = load_live_validation_promotion_proposal_snapshots(target)
    snapshots = store.setdefault("snapshots", {})
    if not isinstance(snapshots, dict):
        snapshots = {}
        store["snapshots"] = snapshots
    idempotent_replay = _replay_live_validation_promotion_snapshot_idempotency(
        store=store,
        snapshots=snapshots,
        target=target,
        idempotency_hash=idempotency_hash,
        request_hash=request_hash,
        report_store_path=report_store_path,
        decision_path=decision_path,
    )
    if idempotent_replay is not None:
        return idempotent_replay

    now = _now()
    stored_at_ns = time.time_ns()
    key = make_live_validation_promotion_proposal_snapshot_key(
        proposal_hash=expected_hash,
        target_tier=effective_target,
        stored_at=now,
        stored_at_ns=stored_at_ns,
    )
    while key in snapshots:
        stored_at_ns += 1
        key = make_live_validation_promotion_proposal_snapshot_key(
            proposal_hash=expected_hash,
            target_tier=effective_target,
            stored_at=now,
            stored_at_ns=stored_at_ns,
        )
    record = {
        "key": key,
        "kind": POLYMARKET_LIVE_VALIDATION_PROMOTION_PROPOSAL_SNAPSHOT_KIND,
        "stored_at": now,
        "stored_at_ns": stored_at_ns,
        "source": clean_source,
        "label": clean_label,
        "proposal_hash": expected_hash,
        "proposal_generated_at": proposal_payload.get("generated_at"),
        "proposal_version": proposal_payload.get("proposal_version"),
        "target_tier_filter": proposal_payload.get("target_tier_filter"),
        "counts": _jsonable(proposal_payload.get("counts") or {}),
        "human_review_required": bool(proposal_payload.get("human_review_required")),
        "automerge_enabled": bool(proposal_payload.get("automerge_enabled")),
        "apply_by_default": bool(proposal_payload.get("apply_by_default")),
        "static_coverage_mutated": bool(proposal_payload.get("static_coverage_mutated")),
        "funded_execution_exposed": bool(proposal_payload.get("funded_execution_exposed")),
        "provenance": {
            "source": clean_source,
            "report_store_path": str(live_validation_reports_path(report_store_path)),
            "decision_path": str(live_validation_decisions_path(decision_path)),
            "snapshot_path": str(target),
            "proposal_hash": expected_hash,
        },
        "proposal": proposal_payload,
        "idempotency_key_hash": idempotency_hash or None,
        "idempotency_request_hash": request_hash if idempotency_hash else None,
        "idempotency_key_hashes": [idempotency_hash] if idempotency_hash else [],
        "idempotency_requests": {idempotency_hash: request_hash} if idempotency_hash else {},
    }
    snapshots[key] = record
    _prune_promotion_proposal_snapshots(snapshots, max_entries=max_entries)
    store["updated_at"] = now
    store["max_entries"] = int(max_entries)
    _save_store_operation(
        save_live_validation_promotion_proposal_snapshots,
        store,
        target,
        operation_key=key,
        idempotency_hash=idempotency_hash,
    )
    metadata = live_validation_promotion_proposal_snapshot_metadata(
        record,
        target,
        report_store_path=report_store_path,
        decision_path=decision_path,
    )
    metadata.update({"stored": True, "idempotent_replay": False, "entries": len(snapshots)})
    return metadata


def list_live_validation_coverage_promotion_proposal_snapshots(
    *,
    path: Optional[Path | str] = None,
    report_store_path: Optional[Path | str] = None,
    decision_path: Optional[Path | str] = None,
) -> Dict[str, Any]:
    target = live_validation_promotion_proposal_snapshots_path(path)
    store = load_live_validation_promotion_proposal_snapshots(target)
    rows: List[Dict[str, Any]] = []
    current_hashes: Dict[str, str] = {}
    for entry in (store.get("snapshots") or {}).values():
        if not isinstance(entry, Mapping):
            continue
        rows.append(
            live_validation_promotion_proposal_snapshot_metadata(
                entry,
                target,
                report_store_path=report_store_path,
                decision_path=decision_path,
                current_hashes=current_hashes,
            )
        )
    rows.sort(key=lambda item: (float(item.get("stored_at") or 0), float(item.get("stored_at_ns") or 0)), reverse=True)
    stale = sum(1 for row in rows if row.get("snapshot_status") == "stale")
    current = sum(1 for row in rows if row.get("snapshot_status") == "current")
    return {
        "source": "polymarket_live_validation_promotion_proposal_snapshots",
        "kind": POLYMARKET_LIVE_VALIDATION_PROMOTION_PROPOSAL_SNAPSHOT_KIND,
        "cache": live_validation_promotion_proposal_snapshots_health(target),
        "entries": rows,
        "counts": {
            "entries": len(rows),
            "current": current,
            "stale": stale,
        },
        "static_coverage_mutated": False,
        "funded_execution_exposed": False,
    }


def load_live_validation_coverage_promotion_proposal_snapshot(
    key: str,
    *,
    path: Optional[Path | str] = None,
    report_store_path: Optional[Path | str] = None,
    decision_path: Optional[Path | str] = None,
) -> Optional[Dict[str, Any]]:
    clean_key = str(key or "").strip()
    if not clean_key:
        return None
    target = live_validation_promotion_proposal_snapshots_path(path)
    store = load_live_validation_promotion_proposal_snapshots(target)
    entry = (store.get("snapshots") or {}).get(clean_key)
    if not isinstance(entry, Mapping):
        return None
    metadata = live_validation_promotion_proposal_snapshot_metadata(
        entry,
        target,
        report_store_path=report_store_path,
        decision_path=decision_path,
    )
    proposal = _jsonable(entry.get("proposal") or {})
    diff = live_validation_promotion_proposal_snapshot_diff(
        entry,
        report_store_path=report_store_path,
        decision_path=decision_path,
    )
    return {
        "source": "polymarket_live_validation_promotion_proposal_snapshot",
        "kind": POLYMARKET_LIVE_VALIDATION_PROMOTION_PROPOSAL_SNAPSHOT_KIND,
        "entry": metadata,
        "proposal": proposal,
        "diff": diff,
        "export": {
            "json_filename": live_validation_promotion_proposal_snapshot_export_filename(clean_key, "json"),
            "markdown_filename": live_validation_promotion_proposal_snapshot_export_filename(clean_key, "md"),
        },
        "static_coverage_mutated": False,
        "funded_execution_exposed": False,
    }


@_locked_store_mutation(live_validation_promotion_proposal_snapshots_path, path_parameter="path")
def purge_live_validation_coverage_promotion_proposal_snapshots(
    *,
    keys: Optional[Iterable[str]] = None,
    all_entries: bool = False,
    path: Optional[Path | str] = None,
) -> Dict[str, Any]:
    requested_keys = [str(key or "").strip() for key in (keys or []) if str(key or "").strip()]
    if not requested_keys and not all_entries:
        raise ValueError("Promotion proposal snapshot purge requires a key or all=true.")
    target = live_validation_promotion_proposal_snapshots_path(path)
    store = load_live_validation_promotion_proposal_snapshots(target)
    snapshots = store.setdefault("snapshots", {})
    if not isinstance(snapshots, dict):
        snapshots = {}
        store["snapshots"] = snapshots

    deleted_keys: List[str] = []
    missing_keys: List[str] = []
    if all_entries:
        deleted_keys = [str(key) for key in snapshots]
        snapshots.clear()
    else:
        for key in requested_keys:
            if key in snapshots:
                snapshots.pop(key, None)
                deleted_keys.append(key)
            else:
                missing_keys.append(key)
    store["updated_at"] = _now()
    save_live_validation_promotion_proposal_snapshots(store, target)
    inventory = list_live_validation_coverage_promotion_proposal_snapshots(path=target)
    inventory.update(
        {
            "deleted": len(deleted_keys),
            "deleted_keys": deleted_keys,
            "missing_keys": missing_keys,
            "requested": len(requested_keys),
            "message": f"Deleted {len(deleted_keys)} promotion proposal snapshot(s).",
        }
    )
    return inventory


def live_validation_promotion_proposal_snapshots_health(path: Optional[Path | str] = None) -> Dict[str, Any]:
    target = live_validation_promotion_proposal_snapshots_path(path)
    store = load_live_validation_promotion_proposal_snapshots(target)
    snapshots = store.get("snapshots") if isinstance(store.get("snapshots"), Mapping) else {}
    return {
        "path": str(target),
        "exists": target.exists(),
        "entries": len(snapshots),
        "max_entries": int(store.get("max_entries") or DEFAULT_LIVE_VALIDATION_PROMOTION_PROPOSAL_SNAPSHOTS_MAX_ENTRIES),
        "size_bytes": target.stat().st_size if target.exists() else 0,
        "version": int(store.get("version") or LIVE_VALIDATION_PROMOTION_PROPOSAL_SNAPSHOTS_VERSION),
        "created_at": store.get("created_at"),
        "updated_at": store.get("updated_at"),
    }


def live_validation_promotion_proposal_snapshot_metadata(
    entry: Mapping[str, Any],
    path: Path,
    *,
    report_store_path: Optional[Path | str] = None,
    decision_path: Optional[Path | str] = None,
    current_hashes: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    target_tier = str(entry.get("target_tier_filter") or "")
    current_hash = _current_promotion_proposal_hash(
        target_tier=target_tier,
        report_store_path=report_store_path,
        decision_path=decision_path,
        cache=current_hashes,
    )
    proposal_hash = str(entry.get("proposal_hash") or "")
    stale_reasons: List[str] = []
    if current_hash and proposal_hash and current_hash != proposal_hash:
        stale_reasons.append("proposal_hash_mismatch")
    if bool(entry.get("automerge_enabled")):
        stale_reasons.append("snapshot_has_automerge_enabled")
    if bool(entry.get("static_coverage_mutated")):
        stale_reasons.append("snapshot_claims_static_coverage_mutation")
    stored_at = _safe_int(entry.get("stored_at"))
    metadata = {
        "key": entry.get("key"),
        "kind": entry.get("kind") or POLYMARKET_LIVE_VALIDATION_PROMOTION_PROPOSAL_SNAPSHOT_KIND,
        "stored_at": stored_at,
        "stored_at_ns": _safe_int(entry.get("stored_at_ns")),
        "age_seconds": None if stored_at is None else max(0, int(_now() - stored_at)),
        "source": entry.get("source") or "react_preview",
        "label": entry.get("label") or "",
        "proposal_hash": proposal_hash,
        "current_proposal_hash": current_hash,
        "proposal_generated_at": _safe_int(entry.get("proposal_generated_at")),
        "proposal_version": entry.get("proposal_version"),
        "target_tier_filter": entry.get("target_tier_filter"),
        "counts": _jsonable(entry.get("counts") or {}),
        "human_review_required": bool(entry.get("human_review_required")),
        "automerge_enabled": bool(entry.get("automerge_enabled")),
        "apply_by_default": bool(entry.get("apply_by_default")),
        "static_coverage_mutated": bool(entry.get("static_coverage_mutated")),
        "funded_execution_exposed": bool(entry.get("funded_execution_exposed")),
        "snapshot_status": "stale" if stale_reasons else "current",
        "stale": bool(stale_reasons),
        "stale_reasons": stale_reasons,
        "path": str(path),
        "provenance": _jsonable(entry.get("provenance") or {}),
    }
    return metadata


def live_validation_promotion_proposal_snapshot_diff(
    entry: Mapping[str, Any],
    *,
    report_store_path: Optional[Path | str] = None,
    decision_path: Optional[Path | str] = None,
) -> Dict[str, Any]:
    """Return a compact no-secrets diff between a stored snapshot and its current proposal."""
    snapshot = _jsonable(entry.get("proposal") or {})
    if not isinstance(snapshot, Mapping):
        snapshot = {}
    target_tier = str(snapshot.get("target_tier_filter") or entry.get("target_tier_filter") or "").strip()
    current = live_validation_coverage_promotion_proposal(
        report_store_path=report_store_path,
        decision_path=decision_path,
        target_tier=target_tier,
    )
    stored_hash = str(entry.get("proposal_hash") or snapshot.get("proposal_hash") or "")
    snapshot_hash = live_validation_coverage_promotion_proposal_hash(snapshot) if snapshot else ""
    current_hash = str(current.get("proposal_hash") or "")
    counts = _promotion_proposal_count_diff(snapshot, current)
    accepted = _promotion_proposal_row_diff(snapshot, current, "accepted_decisions")
    stale = _promotion_proposal_row_diff(snapshot, current, "stale_decisions")
    files = _promotion_proposal_file_diff(snapshot, current)
    gates = _promotion_proposal_gate_diff(snapshot, current)

    categories: List[str] = []
    if stored_hash != current_hash:
        categories.append("proposal_hash")
    if stored_hash and snapshot_hash and stored_hash != snapshot_hash:
        categories.append("snapshot_integrity")
    if any(item.get("delta") for item in counts.values()):
        categories.append("counts")
    if accepted["added"] or accepted["removed"]:
        categories.append("accepted_decisions")
    if stale["added"] or stale["removed"]:
        categories.append("stale_decisions")
    if files["added"] or files["removed"]:
        categories.append("proposed_files")
    if gates["added"] or gates["removed"] or gates["changed"]:
        categories.append("review_gates")

    return {
        "source": "polymarket_live_validation_promotion_proposal_snapshot_diff",
        "kind": "polymarket_live_validation_coverage_promotion_proposal_snapshot_diff",
        "snapshot_key": str(entry.get("key") or ""),
        "target_tier_filter": target_tier or None,
        "proposal_hash": {
            "snapshot": stored_hash,
            "snapshot_canonical": snapshot_hash,
            "current": current_hash,
            "changed": stored_hash != current_hash,
            "snapshot_integrity_valid": not stored_hash or stored_hash == snapshot_hash,
        },
        "counts": counts,
        "accepted_decisions": accepted,
        "stale_decisions": stale,
        "proposed_files": files,
        "review_gates": gates,
        "changed": bool(categories),
        "change_categories": categories,
        "static_coverage_mutated": False,
        "funded_execution_exposed": False,
        "notes": [
            "The diff is a read-only comparison of stored and current no-secrets proposal summaries.",
            "It does not apply coverage or documentation changes and does not alter promotion decisions.",
        ],
    }


def _promotion_proposal_count_diff(snapshot: Mapping[str, Any], current: Mapping[str, Any]) -> Dict[str, Dict[str, int]]:
    before = snapshot.get("counts") if isinstance(snapshot.get("counts"), Mapping) else {}
    after = current.get("counts") if isinstance(current.get("counts"), Mapping) else {}
    fields = sorted({str(key) for key in before} | {str(key) for key in after})
    return {
        field: {
            "snapshot": _safe_int(before.get(field)) or 0,
            "current": _safe_int(after.get(field)) or 0,
            "delta": (_safe_int(after.get(field)) or 0) - (_safe_int(before.get(field)) or 0),
        }
        for field in fields
    }


def _promotion_proposal_row_diff(
    snapshot: Mapping[str, Any],
    current: Mapping[str, Any],
    field: str,
) -> Dict[str, Any]:
    before = _promotion_proposal_row_map(snapshot.get(field))
    after = _promotion_proposal_row_map(current.get(field))
    added = [after[key] for key in sorted(set(after) - set(before))]
    removed = [before[key] for key in sorted(set(before) - set(after))]
    return {"snapshot": len(before), "current": len(after), "added": added, "removed": removed, "unchanged": len(set(before) & set(after))}


def _promotion_proposal_row_map(rows: Any) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    if not isinstance(rows, list):
        return result
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            continue
        decision_key = str(row.get("decision_key") or row.get("key") or "").strip()
        report_key = str(row.get("report_key") or "").strip()
        tier = str(row.get("target_tier") or "").strip()
        identity = decision_key or f"{report_key}:{tier}:{index}"
        result[identity] = {
            "decision_key": decision_key or None,
            "report_key": report_key or None,
            "target_tier": tier or None,
        }
    return result


def _promotion_proposal_file_diff(snapshot: Mapping[str, Any], current: Mapping[str, Any]) -> Dict[str, Any]:
    before = set(_promotion_proposal_files(snapshot))
    after = set(_promotion_proposal_files(current))
    return {
        "snapshot": sorted(before),
        "current": sorted(after),
        "added": sorted(after - before),
        "removed": sorted(before - after),
        "unchanged": sorted(before & after),
    }


def _promotion_proposal_files(proposal: Mapping[str, Any]) -> List[str]:
    patch = proposal.get("patch_proposal") if isinstance(proposal.get("patch_proposal"), Mapping) else {}
    files = patch.get("files") if isinstance(patch.get("files"), list) else []
    return [str(path) for path in files if str(path or "").strip()]


def _promotion_proposal_gate_diff(snapshot: Mapping[str, Any], current: Mapping[str, Any]) -> Dict[str, Any]:
    before = _promotion_proposal_gate_map(snapshot.get("review_gates"))
    after = _promotion_proposal_gate_map(current.get("review_gates"))
    shared = sorted(set(before) & set(after))
    changed = [
        {"gate": key, "snapshot": before[key], "current": after[key]}
        for key in shared
        if before[key] != after[key]
    ]
    return {
        "snapshot": len(before),
        "current": len(after),
        "added": [after[key] for key in sorted(set(after) - set(before))],
        "removed": [before[key] for key in sorted(set(before) - set(after))],
        "changed": changed,
        "unchanged": len(shared) - len(changed),
    }


def _promotion_proposal_gate_map(gates: Any) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    if not isinstance(gates, list):
        return result
    for index, gate in enumerate(gates):
        if not isinstance(gate, Mapping):
            continue
        name = str(gate.get("gate") or f"gate-{index}").strip()
        result[name] = {
            "gate": name,
            "status": str(gate.get("status") or ""),
            "description": str(gate.get("description") or ""),
        }
    return result


def live_validation_promotion_proposal_snapshot_markdown(payload: Mapping[str, Any]) -> str:
    entry = payload.get("entry") if isinstance(payload.get("entry"), Mapping) else {}
    proposal = payload.get("proposal") if isinstance(payload.get("proposal"), Mapping) else {}
    diff = payload.get("diff") if isinstance(payload.get("diff"), Mapping) else {}
    lines = [
        "# Polymarket Live Validation Promotion Proposal Snapshot",
        "",
        f"- Snapshot key: {entry.get('key') or '-'}",
        f"- Label: {entry.get('label') or '-'}",
        f"- Stored at: {entry.get('stored_at') or '-'}",
        f"- Snapshot status: {entry.get('snapshot_status') or '-'}",
        f"- Stale reasons: {', '.join(str(item) for item in entry.get('stale_reasons') or []) or '-'}",
        f"- Proposal hash: {entry.get('proposal_hash') or '-'}",
        f"- Current proposal hash: {entry.get('current_proposal_hash') or '-'}",
        f"- Static coverage mutated: {str(bool(entry.get('static_coverage_mutated'))).lower()}",
        f"- Funded execution exposed: {str(bool(entry.get('funded_execution_exposed'))).lower()}",
        "",
        "## Snapshot Proposal",
        "",
        live_validation_coverage_promotion_proposal_markdown(proposal) if proposal else "- Missing proposal payload",
        "",
    ]
    if diff:
        hash_diff = diff.get("proposal_hash") if isinstance(diff.get("proposal_hash"), Mapping) else {}
        count_diff = diff.get("counts") if isinstance(diff.get("counts"), Mapping) else {}
        files = diff.get("proposed_files") if isinstance(diff.get("proposed_files"), Mapping) else {}
        gates = diff.get("review_gates") if isinstance(diff.get("review_gates"), Mapping) else {}
        lines.extend(
            [
                "## Current-vs-Snapshot Diff",
                "",
                f"- Changed: {str(bool(diff.get('changed'))).lower()}",
                f"- Change categories: {', '.join(str(item) for item in diff.get('change_categories') or []) or '-'}",
                f"- Snapshot hash: {hash_diff.get('snapshot') or '-'}",
                f"- Current hash: {hash_diff.get('current') or '-'}",
                f"- Snapshot integrity valid: {str(bool(hash_diff.get('snapshot_integrity_valid'))).lower()}",
                "",
                "### Count Changes",
                "",
                "| Count | Snapshot | Current | Delta |",
                "| --- | --- | --- | --- |",
            ]
        )
        for key in sorted(count_diff):
            value = count_diff.get(key) if isinstance(count_diff.get(key), Mapping) else {}
            lines.append(
                f"| {_markdown_cell(key)} | {_markdown_cell(value.get('snapshot') or 0)} | "
                f"{_markdown_cell(value.get('current') or 0)} | {_markdown_cell(value.get('delta') or 0)} |"
            )
        if not count_diff:
            lines.append("| - | 0 | 0 | 0 |")
        lines.extend(
            [
                "",
                "### Decision, File, and Gate Changes",
                "",
                f"- Accepted decisions added/removed: {len(diff.get('accepted_decisions', {}).get('added', [])) if isinstance(diff.get('accepted_decisions'), Mapping) else 0}/"
                f"{len(diff.get('accepted_decisions', {}).get('removed', [])) if isinstance(diff.get('accepted_decisions'), Mapping) else 0}",
                f"- Stale decisions added/removed: {len(diff.get('stale_decisions', {}).get('added', [])) if isinstance(diff.get('stale_decisions'), Mapping) else 0}/"
                f"{len(diff.get('stale_decisions', {}).get('removed', [])) if isinstance(diff.get('stale_decisions'), Mapping) else 0}",
                f"- Proposed files added/removed: {', '.join(str(item) for item in files.get('added') or []) or '-'}/"
                f"{', '.join(str(item) for item in files.get('removed') or []) or '-'}",
                f"- Review gates added/removed/changed: {len(gates.get('added') or [])}/{len(gates.get('removed') or [])}/{len(gates.get('changed') or [])}",
                "",
            ]
        )
    return "\n".join(lines)


def live_validation_promotion_proposal_snapshot_diff_markdown(payload: Mapping[str, Any]) -> str:
    return live_validation_promotion_proposal_snapshot_markdown(
        {"entry": {"key": payload.get("snapshot_key") or "", "snapshot_status": "diff"}, "diff": payload}
    )


def live_validation_promotion_proposal_snapshot_export_filename(key: str, extension: str) -> str:
    clean_key = "".join(char for char in str(key or "") if char.isalnum() or char in {"-", "_"})[:80]
    clean_extension = str(extension or "json").strip().lstrip(".") or "json"
    return f"polymarket-live-validation-promotion-proposal-snapshot-{clean_key or 'snapshot'}.{clean_extension}"


def make_live_validation_decision_key(
    *,
    report_key: str,
    payload_hash: str,
    target_tier: str,
    decision: str,
    reviewer_note: str,
    review_bundle_hash: str,
    created_at: int,
    created_at_ns: int,
) -> str:
    body = json.dumps(
        {
            "report_key": report_key,
            "payload_hash": payload_hash,
            "target_tier": target_tier,
            "decision": decision,
            "reviewer_note": reviewer_note,
            "review_bundle_hash": review_bundle_hash,
            "created_at": int(created_at),
            "created_at_ns": int(created_at_ns),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(f"{POLYMARKET_LIVE_VALIDATION_DECISION_KIND}:{body}".encode("utf-8")).hexdigest()[:32]


def make_live_validation_promotion_proposal_snapshot_key(
    *,
    proposal_hash: str,
    target_tier: str,
    stored_at: int,
    stored_at_ns: int,
) -> str:
    body = json.dumps(
        {
            "proposal_hash": proposal_hash,
            "target_tier": target_tier,
            "stored_at": int(stored_at),
            "stored_at_ns": int(stored_at_ns),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(
        f"{POLYMARKET_LIVE_VALIDATION_PROMOTION_PROPOSAL_SNAPSHOT_KIND}:{body}".encode("utf-8")
    ).hexdigest()[:32]


def make_live_validation_report_key(
    report: Mapping[str, Any],
    *,
    source: str,
    label: str,
    stored_at: int,
    stored_at_ns: int = 0,
) -> str:
    body = json.dumps(
        {
            "report": _jsonable(report),
            "source": str(source or ""),
            "label": str(label or ""),
            "stored_at": int(stored_at),
            "stored_at_ns": int(stored_at_ns or 0),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(f"{POLYMARKET_LIVE_VALIDATION_REPORT_KIND}:{body}".encode("utf-8")).hexdigest()[:32]


def live_validation_report_metadata(entry: Mapping[str, Any], path: Path) -> Dict[str, Any]:
    stored_at = _safe_int(entry.get("stored_at"))
    stored_at_ns = _safe_int(entry.get("stored_at_ns"))
    payload = entry.get("payload")
    payload_bytes = None
    if isinstance(payload, Mapping):
        payload_bytes = len(json.dumps(_jsonable(payload), sort_keys=True, separators=(",", ":")).encode("utf-8"))
    payload_hash = _entry_payload_hash(entry)
    duplicate_import_count = _entry_duplicate_import_count(entry)
    provenance = _entry_provenance(entry, payload_hash)
    metadata = {
        "key": entry.get("key"),
        "kind": entry.get("kind") or POLYMARKET_LIVE_VALIDATION_REPORT_KIND,
        "source": entry.get("source") or "unknown",
        "label": entry.get("label") or "",
        "stored_at": stored_at,
        "stored_at_ns": stored_at_ns,
        "age_seconds": None if stored_at is None else max(0, int(_now() - stored_at)),
        "path": str(path),
        "payload_bytes": payload_bytes,
        "payload_hash": payload_hash,
        "provenance": provenance,
        "duplicate_of": entry.get("duplicate_of") or provenance.get("duplicate_of") or None,
        "duplicate_import_count": duplicate_import_count,
    }
    if isinstance(entry.get("last_duplicate_import"), Mapping):
        metadata["last_duplicate_import"] = _jsonable(entry["last_duplicate_import"])
    schema_validation = entry.get("schema_validation")
    if isinstance(schema_validation, Mapping):
        metadata["schema_validation"] = compact_schema_validation(schema_validation)
    elif isinstance(payload, Mapping):
        metadata["schema_validation"] = compact_schema_validation(validate_live_validation_report(payload))
    return metadata


def live_validation_report_summary(report: Mapping[str, Any]) -> Dict[str, Any]:
    stage_gates = report.get("stage_gates") if isinstance(report.get("stage_gates"), Mapping) else {}
    readiness = report.get("clob_auth_readiness") if isinstance(report.get("clob_auth_readiness"), Mapping) else {}
    funded_check = report.get("funded_live_order_check") if isinstance(report.get("funded_live_order_check"), Mapping) else {}
    promotion = live_validation_report_promotion(report)
    return {
        "generated_at": _safe_float(report.get("generated_at")),
        "market_id": report.get("market_id"),
        "mode": report.get("mode"),
        "source_provenance": _jsonable(report.get("source_provenance") or {}),
        "selected": bool(report.get("selected")),
        "enabled": bool(report.get("enabled")),
        "public_live_checks": stage_gates.get("public_live_checks"),
        "credential_readiness": stage_gates.get("credential_readiness"),
        "credentialed_read_checks": stage_gates.get("credentialed_read_checks"),
        "bridge_address_checks": stage_gates.get("bridge_address_checks"),
        "funded_live_order_check": stage_gates.get("funded_live_order_check") or funded_check.get("status"),
        "credentialed_read_ok": bool(stage_gates.get("credentialed_read_ok")),
        "safe_to_attempt_funded_order": bool(stage_gates.get("safe_to_attempt_funded_order")),
        "requires_explicit_live_approval": bool(stage_gates.get("requires_explicit_live_approval")),
        "next_step": stage_gates.get("next_step"),
        "funded_execution_exposed": bool(report.get("funded_execution_exposed")),
        "direct_l2_read_ready": bool(readiness.get("direct_l2_read_ready")),
        "sdk_trading_ready": bool(readiness.get("sdk_trading_ready")),
        "verification_promotion": promotion,
        "credential_live_verified": promotion["credential_live_verified"],
        "funded_live_verified": promotion["funded_live_verified"],
        "can_promote_credential_live_verified": bool(promotion["can_promote_credential_live_verified"]),
        "can_promote_funded_live_verified": bool(promotion["can_promote_funded_live_verified"]),
    }


def live_validation_report_promotion(report: Mapping[str, Any]) -> Dict[str, Any]:
    mode = str(report.get("mode") or "").strip()
    local_only = mode in LOCAL_ONLY_REPORT_MODES
    source_provenance_verified = _stable_source_provenance(report.get("source_provenance"))
    authenticated_checks = (
        report.get("authenticated_read_checks")
        if isinstance(report.get("authenticated_read_checks"), Mapping)
        else {}
    )
    funded_check = (
        report.get("funded_live_order_check")
        if isinstance(report.get("funded_live_order_check"), Mapping)
        else {}
    )
    stage_gates = report.get("stage_gates") if isinstance(report.get("stage_gates"), Mapping) else {}
    public_checks = report.get("public_checks") if isinstance(report.get("public_checks"), Mapping) else {}
    public_evidence = _public_promotion_evidence(public_checks)
    public_verified = bool(report.get("ok") is True and len(public_evidence) == len(PUBLIC_PROMOTION_CHECKS))
    credential_evidence = _credential_promotion_evidence(authenticated_checks)
    source_revision = ""
    if isinstance(report.get("source_provenance"), Mapping):
        source_revision = str(report["source_provenance"].get("source_revision") or "")
    funded_evidence = _funded_promotion_evidence(
        funded_check,
        expected_source_revision=source_revision,
    )
    blockers: List[str] = []

    if local_only:
        blockers.append(f"Report mode {mode!r} is local-only and cannot promote production verification tiers.")
    if not public_verified:
        blockers.append(
            "Credential and funded promotion require report.ok=true plus the exact successful semantic public-check inventory."
        )
    if mode == "strict_cli" and not source_provenance_verified:
        blockers.append(
            "Strict CLI promotion requires matching clean initial/final source revisions in source_provenance."
        )
    if stage_gates.get("credentialed_read_ok") and not credential_evidence:
        blockers.append("Stage gates claim credentialed_read_ok, but no accepted authenticated-read evidence is present.")
    if stage_gates.get("funded_live_order_check") in {"ok", "passed"} and not funded_evidence:
        blockers.append("Stage gates claim funded verification, but no funded order/cancel audit evidence is present.")

    can_promote_credential = (
        public_verified
        and bool(credential_evidence)
        and not local_only
        and source_provenance_verified
    )
    # A funded mutation is not independently sufficient evidence of an
    # authenticated account path.  Promotion requires a successful accepted
    # non-mutating read from the same report/run as the order/cancel audit.
    can_promote_funded = (
        bool(funded_evidence)
        and bool(credential_evidence)
        and public_verified
        and not local_only
        and source_provenance_verified
    )
    if not credential_evidence:
        blockers.append(
            "Credential live verification requires an ok CLOB L2 order-list or relayer authenticated read."
        )
    if not funded_evidence:
        blockers.append(
            "Funded live verification requires an exact-source, same-account, post-only order/cancel result with "
            "geoblock, balance/allowance, zero-fill, and resolved recovery-journal evidence."
        )
    if not credential_evidence:
        blockers.append(
            "Funded live verification also requires accepted non-mutating authenticated-read evidence "
            "in the same report."
        )
    if not POLYMARKET_LIVE_MUTATIONS_SUPPORTED:
        blockers.append(POLYMARKET_LIVE_MUTATION_BLOCKER)

    return {
        "credential_live_verified": "candidate_only" if can_promote_credential else "blocked",
        "funded_live_verified": "candidate_only" if can_promote_funded else "blocked",
        "can_promote_credential_live_verified": can_promote_credential,
        "can_promote_funded_live_verified": can_promote_funded,
        "attested_workflow_verified": False,
        "evidence_trust": "local_unattested_candidate",
        "requires_attested_workflow": True,
        "candidate_limitations": [
            "Local report bytes are not trusted workflow evidence and cannot establish a verified production tier."
        ],
        "source_provenance_verified": source_provenance_verified,
        "public_checks_verified": public_verified,
        "public_evidence": public_evidence,
        "credential_evidence": credential_evidence,
        "funded_evidence": funded_evidence,
        "blocked_reasons": _dedupe(blockers),
        "accepted_credential_checks": list(CREDENTIAL_PROMOTION_CHECKS),
        "accepted_public_checks": list(PUBLIC_PROMOTION_CHECKS),
        "accepted_funded_audit_fields": [
            "status",
            "live_action",
            "manual_reconciliation_required",
            "audit.order_id",
            "audit.cancel",
            "audit.post_cancel_order",
            "audit.post_cancel_verified",
            "audit.zero_fill_evidence",
            "audit.recovery_journal",
            "account_authenticated_read_preflight",
            "account_preflight",
            "execution_guards",
            "geoblock_preflight",
            "source_revision_gate",
        ],
    }


def _stable_source_provenance(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    source_revision = str(value.get("source_revision") or "")
    return bool(
        value.get("schema_version") == 1
        and value.get("repository") == "market-sentinel"
        and value.get("repository_origin") == EXPECTED_REPOSITORY_ORIGIN
        and value.get("stable") is True
        and value.get("initial_clean") is True
        and value.get("final_clean") is True
        and len(source_revision) == 40
        and all(character in "0123456789abcdef" for character in source_revision)
        and source_revision == value.get("initial_revision") == value.get("final_revision")
    )


def _credential_promotion_evidence(authenticated_checks: Mapping[str, Any]) -> List[Dict[str, Any]]:
    evidence: List[Dict[str, Any]] = []
    for name in CREDENTIAL_PROMOTION_CHECKS:
        item = authenticated_checks.get(name)
        if not isinstance(item, Mapping) or item.get("status") != "ok":
            continue
        records_observed = item.get("records_observed")
        if not (
            item.get("semantic_check") == CREDENTIAL_PROMOTION_SEMANTICS[name]
            and isinstance(records_observed, int)
            and not isinstance(records_observed, bool)
            and records_observed >= 0
        ):
            continue
        evidence.append(
            {
                "check": name,
                "status": "ok",
                "detail": item.get("detail") or "",
                "sample_type": item.get("sample_type") or "",
                "semantic_check": CREDENTIAL_PROMOTION_SEMANTICS[name],
                "records_observed": records_observed,
            }
        )
    return evidence


def _public_promotion_evidence(public_checks: Mapping[str, Any]) -> List[Dict[str, Any]]:
    expected_semantics = {
        "clob_time": "current_unix_time",
        "gamma_markets": "market_identity",
        "data_leaderboard": "leaderboard_identity",
        "bridge_supported_assets": "supported_asset_identity",
    }
    if set(public_checks) != set(PUBLIC_PROMOTION_CHECKS):
        return []
    evidence: List[Dict[str, Any]] = []
    for name in PUBLIC_PROMOTION_CHECKS:
        item = public_checks.get(name)
        if not isinstance(item, Mapping):
            return []
        if item.get("status") != "ok" or item.get("semantic_check") != expected_semantics[name]:
            return []
        evidence.append(
            {
                "check": name,
                "status": "ok",
                "semantic_check": expected_semantics[name],
            }
        )
    return evidence


def _funded_promotion_evidence(
    funded_check: Mapping[str, Any],
    *,
    expected_source_revision: str,
) -> List[Dict[str, Any]]:
    # A historical/local report cannot establish a supported CLOB V2 mutation
    # while the repository intentionally exposes no V2 order client.
    if not POLYMARKET_LIVE_MUTATIONS_SUPPORTED:
        return []
    audit = funded_check.get("audit") if isinstance(funded_check.get("audit"), Mapping) else {}
    account_read = (
        funded_check.get("account_authenticated_read_preflight")
        if isinstance(funded_check.get("account_authenticated_read_preflight"), Mapping)
        else {}
    )
    account = funded_check.get("account_preflight") if isinstance(funded_check.get("account_preflight"), Mapping) else {}
    guards = funded_check.get("execution_guards") if isinstance(funded_check.get("execution_guards"), Mapping) else {}
    geoblock = funded_check.get("geoblock_preflight") if isinstance(funded_check.get("geoblock_preflight"), Mapping) else {}
    source_gate = (
        funded_check.get("source_revision_gate")
        if isinstance(funded_check.get("source_revision_gate"), Mapping)
        else {}
    )
    recovery = audit.get("recovery_journal") if isinstance(audit.get("recovery_journal"), Mapping) else {}
    order_id = str(audit.get("order_id") or "").strip()
    post_cancel_verified = bool(audit.get("post_cancel_verified"))
    if funded_check.get("status") != "ok" or funded_check.get("live_action") is not True:
        return []
    if funded_check.get("manual_reconciliation_required") is not False:
        return []
    if not order_id or not post_cancel_verified:
        return []
    if not (
        account_read.get("status") == "pass"
        and account_read.get("same_trading_client") is True
        and account_read.get("account_identity_present") is True
    ):
        return []
    if not (
        account.get("status") == "pass"
        and account.get("sufficient_balance") is True
        and account.get("sufficient_allowance") is True
    ):
        return []
    if not (
        guards.get("status") == "pass"
        and guards.get("post_only") is True
        and guards.get("time_in_force") == "GTC"
        and guards.get("maker_price_verified") is True
    ):
        return []
    if not (geoblock.get("status") == "pass" and geoblock.get("blocked") is False):
        return []
    if not (
        source_gate.get("status") == "pass"
        and source_gate.get("clean") is True
        and source_gate.get("matches_initial_revision") is True
        and source_gate.get("repository_origin") == EXPECTED_REPOSITORY_ORIGIN
        and bool(expected_source_revision)
        and source_gate.get("source_revision") == expected_source_revision
    ):
        return []
    if not (
        recovery.get("status") == "resolved"
        and recovery.get("stage") == "cancel_verified"
        and recovery.get("resolved") is True
    ):
        return []
    required_sections = ("placed", "cancel", "post_cancel_order")
    if not all(isinstance(audit.get(section), Mapping) for section in required_sections):
        return []
    if extract_order_id(audit["placed"]) != order_id:
        return []
    if not cancel_response_contains(audit["cancel"], order_id):
        return []
    if not order_state_is_cancelled(audit["post_cancel_order"], order_id):
        return []
    if not order_zero_fill_evidence(audit["post_cancel_order"], order_id)["verified"]:
        return []
    return [
        {
            "check": "funded_order_cancel",
            "status": "ok",
            "order_id_present": True,
            "post_cancel_verified": True,
            "post_only_verified": True,
            "same_account_read_verified": True,
            "geoblock_verified": True,
            "balance_allowance_verified": True,
            "zero_fill_verified": True,
            "recovery_journal_resolved": True,
            "source_revision_verified": True,
            "audit_sections": list(required_sections),
        }
    ]


def _live_validation_promotion_proposal_gates() -> List[Dict[str, Any]]:
    return [
        {
            "gate": "human_review",
            "status": "required",
            "description": "A human reviewer must compare the proposal, decision ledger, and current review bundle.",
        },
        {
            "gate": "no_automerge",
            "status": "required",
            "description": "The proposal export must not be merged or applied automatically.",
        },
        {
            "gate": "stale_decision_check",
            "status": "required",
            "description": "Any payload or review-bundle hash mismatch blocks promotion until the decision is re-recorded.",
        },
        {
            "gate": "credential_or_funded_evidence",
            "status": "required",
            "description": "Credential/funded tiers require current accepted evidence from the sanitized review bundle.",
        },
        {
            "gate": "eligibility_and_live_risk",
            "status": "required",
            "description": "Credential, region/KYC, account, wallet funding, and explicit live-action approval remain external prerequisites.",
        },
        {
            "gate": "tests_and_docs",
            "status": "required",
            "description": "Any manual coverage/docs patch must update tests/docs and pass the project verifier.",
        },
    ]


def _live_validation_promotion_proposal_candidate(entry: Mapping[str, Any], report_path: Path) -> Dict[str, Any]:
    report_key = str(entry.get("report_key") or "").strip()
    target_tier = str(entry.get("target_tier") or "").strip()
    expected_payload_hash = str(entry.get("payload_hash") or "").strip()
    expected_review_hash = str(entry.get("review_bundle_hash") or "").strip()
    stale_reasons: List[str] = []
    bundle = live_validation_report_review_bundle(report_key, path=report_path)
    current_payload_hash = ""
    current_review_hash = ""
    target_mapping: Dict[str, Any] = {}
    promotion_review: Dict[str, Any] = {}
    report_label = entry.get("report_label") or ""
    report_source = entry.get("report_source") or ""

    if bundle is None:
        stale_reasons.append("missing_report")
    else:
        report = bundle.get("report") if isinstance(bundle.get("report"), Mapping) else {}
        coverage = bundle.get("coverage_tier_mapping") if isinstance(bundle.get("coverage_tier_mapping"), Mapping) else {}
        levels = coverage.get("levels") if isinstance(coverage.get("levels"), Mapping) else {}
        current_payload_hash = str(report.get("payload_hash") or "").strip()
        current_review_hash = live_validation_report_review_bundle_hash(bundle)
        report_label = report.get("label") or report_label
        report_source = report.get("source") or report_source
        if expected_payload_hash != current_payload_hash:
            stale_reasons.append("payload_hash_mismatch")
        if expected_review_hash != current_review_hash:
            stale_reasons.append("review_bundle_hash_mismatch")
        if isinstance(levels.get(target_tier), Mapping):
            target_mapping = _jsonable(levels.get(target_tier) or {})
        promotion = bundle.get("promotion_review") if isinstance(bundle.get("promotion_review"), Mapping) else {}
        promotion_review = _jsonable(promotion)
        can_promote = bool(target_mapping.get("can_promote_from_report"))
        if target_tier != "public_live_verified" and not can_promote:
            stale_reasons.append("tier_no_longer_promotable")

    proposal_effect = (
        "manual_coverage_docs_patch_candidate"
        if not stale_reasons
        else "blocked_until_decision_is_revalidated"
    )
    return {
        "decision_key": entry.get("key"),
        "report_key": report_key,
        "report_label": report_label,
        "report_source": report_source,
        "target_tier": target_tier,
        "created_at": entry.get("created_at"),
        "created_at_ns": entry.get("created_at_ns"),
        "reviewer": entry.get("reviewer") or "operator",
        "reviewer_note": entry.get("reviewer_note") or "",
        "expected_payload_hash": expected_payload_hash,
        "current_payload_hash": current_payload_hash,
        "expected_review_bundle_hash": expected_review_hash,
        "current_review_bundle_hash": current_review_hash,
        "review_bundle_hash_verified": bool(entry.get("review_bundle_hash_verified")),
        "stale": bool(stale_reasons),
        "stale_reasons": stale_reasons,
        "proposal_effect": proposal_effect,
        "static_coverage_mutated": False,
        "funded_execution_exposed": False,
        "coverage_tier_decision": _jsonable(entry.get("coverage_tier_decision") or {}),
        "current_coverage_tier_mapping": target_mapping,
        "current_promotion_review": promotion_review,
    }


def _live_validation_promotion_proposal_changes(candidate: Mapping[str, Any]) -> List[Dict[str, Any]]:
    target_tier = str(candidate.get("target_tier") or "").strip()
    evidence_hash = str(candidate.get("current_review_bundle_hash") or candidate.get("expected_review_bundle_hash") or "")
    report_key = str(candidate.get("report_key") or "")
    decision_key = str(candidate.get("decision_key") or "")
    evidence = f"report={report_key}; decision={decision_key}; review_bundle_hash={evidence_hash}"
    descriptions = (
        (
            "polymarket/coverage.py",
            "manual_static_coverage_review",
            "Review static Polymarket coverage tier states for categories supported by this accepted evidence.",
        ),
        (
            "README.md",
            "manual_capability_matrix_review",
            "Update the Polymarket capability matrix and live-validation notes only after human approval.",
        ),
        (
            "GOAL.md",
            "manual_goal_status_review",
            "Record the exact reviewed promotion scope without claiming broader production support.",
        ),
        (
            "docs/POLYMARKET_LIVE_REPORT_DECISION_LEDGER.md",
            "manual_evidence_doc_review",
            "Reference the accepted decision and proposal hash in operator-facing evidence documentation.",
        ),
    )
    return [
        {
            "path": path,
            "action": action,
            "target_tier": target_tier,
            "description": description,
            "evidence": evidence,
            "automerge_enabled": False,
            "apply_by_default": False,
            "static_coverage_mutated": False,
        }
        for path, action, description in descriptions
    ]


def _proposal_snapshot_default_label(proposal: Mapping[str, Any]) -> str:
    target = str(proposal.get("target_tier_filter") or "all tiers")
    counts = proposal.get("counts") if isinstance(proposal.get("counts"), Mapping) else {}
    accepted = int(counts.get("accepted_candidates") or 0)
    stale = int(counts.get("stale_decisions") or 0)
    return f"{target} proposal: {accepted} accepted, {stale} stale"


def _current_promotion_proposal_hash(
    *,
    target_tier: str,
    report_store_path: Optional[Path | str],
    decision_path: Optional[Path | str],
    cache: Optional[Dict[str, str]] = None,
) -> str:
    cache_key = str(target_tier or "")
    if cache is not None and cache_key in cache:
        return cache[cache_key]
    try:
        proposal = live_validation_coverage_promotion_proposal(
            report_store_path=report_store_path,
            decision_path=decision_path,
            target_tier=cache_key,
        )
        current_hash = str(proposal.get("proposal_hash") or live_validation_coverage_promotion_proposal_hash(proposal))
    except Exception:
        current_hash = ""
    if cache is not None:
        cache[cache_key] = current_hash
    return current_hash


def _promotion_candidate(entry: Mapping[str, Any], promotion: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "key": entry.get("key"),
        "label": entry.get("label"),
        "source": entry.get("source"),
        "stored_at": entry.get("stored_at"),
        "credential_evidence": promotion.get("credential_evidence", []),
        "funded_evidence": promotion.get("funded_evidence", []),
    }


def _review_summary(summary: Mapping[str, Any]) -> Dict[str, Any]:
    keys = (
        "generated_at",
        "market_id",
        "mode",
        "public_live_checks",
        "credential_readiness",
        "credentialed_read_checks",
        "bridge_address_checks",
        "funded_live_order_check",
        "credentialed_read_ok",
        "safe_to_attempt_funded_order",
        "requires_explicit_live_approval",
        "next_step",
        "funded_execution_exposed",
        "credential_live_verified",
        "funded_live_verified",
        "can_promote_credential_live_verified",
        "can_promote_funded_live_verified",
    )
    return {key: _jsonable(summary.get(key)) for key in keys}


def _review_duplicate_history(entry: Mapping[str, Any], metadata: Mapping[str, Any]) -> Dict[str, Any]:
    duplicate_imports = _entry_duplicate_imports(entry)
    return {
        "duplicate_of": metadata.get("duplicate_of"),
        "duplicate_import_count": _entry_duplicate_import_count(entry),
        "last_duplicate_import": metadata.get("last_duplicate_import"),
        "duplicate_imports": duplicate_imports,
        "duplicate_policy": (metadata.get("provenance") or {}).get("duplicate_policy")
        if isinstance(metadata.get("provenance"), Mapping)
        else None,
    }


def _review_promotion(report: Mapping[str, Any], promotion: Mapping[str, Any]) -> Dict[str, Any]:
    stage_gates = report.get("stage_gates") if isinstance(report.get("stage_gates"), Mapping) else {}
    return {
        "credential_live_verified": promotion.get("credential_live_verified", "blocked"),
        "funded_live_verified": promotion.get("funded_live_verified", "blocked"),
        "can_promote_credential_live_verified": bool(promotion.get("can_promote_credential_live_verified")),
        "can_promote_funded_live_verified": bool(promotion.get("can_promote_funded_live_verified")),
        "stage_gate_claims": {
            "credentialed_read_ok": bool(stage_gates.get("credentialed_read_ok")),
            "safe_to_attempt_funded_order": bool(stage_gates.get("safe_to_attempt_funded_order")),
            "funded_live_order_check": stage_gates.get("funded_live_order_check"),
        },
        "evidence": {
            "credential": _jsonable(promotion.get("credential_evidence", [])),
            "funded": _jsonable(promotion.get("funded_evidence", [])),
        },
        "blocked_reasons": _jsonable(promotion.get("blocked_reasons", [])),
        "accepted_credential_checks": _jsonable(promotion.get("accepted_credential_checks", [])),
        "accepted_funded_audit_fields": _jsonable(promotion.get("accepted_funded_audit_fields", [])),
    }


def _review_operator_commands(report: Mapping[str, Any]) -> Dict[str, str]:
    commands: Dict[str, str] = {}
    for source_key, _prefix in (
        ("operator_commands", ""),
        ("commands", ""),
    ):
        source = report.get(source_key)
        if isinstance(source, Mapping):
            for name, command in source.items():
                clean_name = str(name or "").strip()
                clean_command = str(command or "").strip()
                if clean_name and clean_command:
                    commands[clean_name] = clean_command
    credential_runbook = report.get("credential_runbook")
    if isinstance(credential_runbook, Mapping):
        runbook_commands = credential_runbook.get("operator_commands")
        if isinstance(runbook_commands, Mapping):
            for name, command in runbook_commands.items():
                clean_name = str(name or "").strip()
                clean_command = str(command or "").strip()
                if clean_name and clean_command:
                    commands[f"credential_runbook.{clean_name}"] = clean_command
    return commands


def _review_coverage_tier_mapping(summary: Mapping[str, Any], promotion: Mapping[str, Any]) -> Dict[str, Any]:
    from .coverage import polymarket_official_api_coverage

    coverage = polymarket_official_api_coverage()
    static_snapshot = _coverage_level_state_snapshot(coverage)
    credential_can_promote = bool(promotion.get("can_promote_credential_live_verified"))
    funded_can_promote = bool(promotion.get("can_promote_funded_live_verified"))
    return {
        "static_coverage_mutated": False,
        "coverage_docs_checked": coverage.get("docs_checked"),
        "review_scope": "stored_live_validation_report_evidence_only",
        "levels": {
            "public_live_verified": {
                "static_category_states": static_snapshot.get("public_live_verified", {}),
                "report_status": summary.get("public_live_checks") or "unknown",
                "review_effect": "informational_only",
                "can_promote_from_report": False,
            },
            "credential_live_verified": {
                "static_category_states": static_snapshot.get("credential_live_verified", {}),
                "report_status": promotion.get("credential_live_verified", "blocked"),
                "review_effect": "candidate_evidence_only" if credential_can_promote else "blocked",
                "can_promote_from_report": credential_can_promote,
                "required_evidence": [
                    "ok clob_l2_orders authenticated read",
                    "ok relayer_recent_transactions authenticated read",
                ],
            },
            "funded_live_verified": {
                "static_category_states": static_snapshot.get("funded_live_verified", {}),
                "report_status": promotion.get("funded_live_verified", "blocked"),
                "review_effect": "candidate_evidence_only" if funded_can_promote else "blocked",
                "can_promote_from_report": funded_can_promote,
                "required_evidence": [
                    "same-report accepted non-mutating authenticated read",
                    "funded_live_order_check.status == ok",
                    "funded_live_order_check.live_action == true",
                    "audit order id",
                    "placed/cancel/post_cancel_order audit sections",
                    "audit.post_cancel_verified == true",
                ],
            },
        },
        "note": (
            "The bundle maps report evidence to coverage tiers for operator review only; "
            "static Polymarket coverage remains unchanged."
        ),
    }


def _coverage_level_state_snapshot(coverage: Mapping[str, Any]) -> Dict[str, Dict[str, int]]:
    out: Dict[str, Dict[str, int]] = {
        "public_live_verified": {},
        "credential_live_verified": {},
        "funded_live_verified": {},
    }
    for item in coverage.get("categories") or []:
        if not isinstance(item, Mapping):
            continue
        levels = item.get("coverage_levels") if isinstance(item.get("coverage_levels"), Mapping) else {}
        for level in out:
            state = str(levels.get(level) or "unknown")
            out[level][state] = out[level].get(state, 0) + 1
    return out


def _entry_duplicate_imports(entry: Mapping[str, Any]) -> List[Dict[str, Any]]:
    imports = entry.get("duplicate_imports")
    if not isinstance(imports, list):
        return []
    return [_jsonable(item) for item in imports if isinstance(item, Mapping)]


def _markdown_cell(value: Any) -> str:
    text = json.dumps(_jsonable(value), sort_keys=True) if isinstance(value, Mapping) else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def _find_duplicate_live_validation_report(
    payload_hash: str,
    reports: Mapping[str, Any],
    path: Path,
    *,
    exclude_key: str = "",
) -> Optional[Dict[str, Any]]:
    clean_hash = str(payload_hash or "").strip()
    clean_exclude = str(exclude_key or "").strip()
    if not clean_hash:
        return None
    matches: List[Mapping[str, Any]] = []
    for key, entry in reports.items():
        if clean_exclude and str(key) == clean_exclude:
            continue
        if not isinstance(entry, Mapping):
            continue
        if _entry_payload_hash(entry) == clean_hash:
            matches.append(entry)
    if not matches:
        return None
    matches.sort(
        key=lambda item: (
            float(item.get("stored_at") or 0),
            float(item.get("stored_at_ns") or 0),
        ),
        reverse=True,
    )
    duplicate = live_validation_report_metadata(matches[0], path)
    payload = matches[0].get("payload")
    if isinstance(payload, Mapping):
        duplicate["summary"] = live_validation_report_summary(payload)
    duplicate["duplicate"] = True
    return duplicate


def _entry_payload_hash(entry: Mapping[str, Any]) -> str:
    stored_hash = str(entry.get("payload_hash") or "").strip()
    if stored_hash:
        return stored_hash
    provenance = entry.get("provenance") if isinstance(entry.get("provenance"), Mapping) else {}
    provenance_hash = str(provenance.get("payload_hash") or provenance.get("redacted_payload_hash") or "").strip()
    if provenance_hash:
        return provenance_hash
    payload = entry.get("payload")
    if isinstance(payload, Mapping):
        return live_validation_report_payload_hash(payload)
    return ""


def _entry_provenance(entry: Mapping[str, Any], payload_hash: str) -> Dict[str, Any]:
    raw = entry.get("provenance")
    provenance = dict(raw) if isinstance(raw, Mapping) else {}
    if payload_hash:
        provenance.setdefault("payload_hash", payload_hash)
        provenance.setdefault("redacted_payload_hash", payload_hash)
    return _jsonable(provenance)


def _live_validation_report_provenance(
    *,
    payload_hash: str,
    source_file: Optional[Path | str] = None,
    duplicate_policy: str,
    duplicate_of: Optional[str] = None,
) -> Dict[str, Any]:
    provenance: Dict[str, Any] = {
        "payload_hash": payload_hash,
        "redacted_payload_hash": payload_hash,
        "duplicate_policy": str(duplicate_policy or "").strip() or "unique",
    }
    if source_file is not None and str(source_file).strip():
        source_path = Path(str(source_file))
        provenance["source_file"] = str(source_path)
        provenance["source_file_name"] = source_path.name
    if duplicate_of:
        provenance["duplicate_of"] = str(duplicate_of)
    return provenance


def _duplicate_import_event(
    *,
    source: str,
    label: str,
    source_file: Optional[Path | str],
    payload_hash: str,
    duplicate_of: str,
    attempted_at: int,
    attempted_at_ns: int,
) -> Dict[str, Any]:
    event = {
        "attempted_at": int(attempted_at),
        "attempted_at_ns": int(attempted_at_ns),
        "source": str(source or "").strip(),
        "label": str(label or "").strip(),
        "payload_hash": payload_hash,
        "duplicate_policy": "skip",
        "duplicate_of": duplicate_of,
        "stored": False,
    }
    if source_file is not None and str(source_file).strip():
        source_path = Path(str(source_file))
        event["source_file"] = str(source_path)
        event["source_file_name"] = source_path.name
    return event


def _append_duplicate_import(entry: Dict[str, Any], event: Mapping[str, Any]) -> None:
    previous_count = _entry_duplicate_import_count(entry)
    imports = entry.get("duplicate_imports")
    if not isinstance(imports, list):
        imports = []
    imports.append(_jsonable(event))
    entry["duplicate_imports"] = imports[-50:]
    entry["duplicate_import_count"] = previous_count + 1
    entry["last_duplicate_import"] = _jsonable(event)


def _entry_duplicate_import_count(entry: Mapping[str, Any]) -> int:
    stored_count = _safe_int(entry.get("duplicate_import_count"))
    if stored_count is not None:
        return max(0, stored_count)
    imports = entry.get("duplicate_imports")
    return len(imports) if isinstance(imports, list) else 0


def _dedupe(values: Iterable[str]) -> List[str]:
    out: List[str] = []
    for value in values:
        clean = str(value or "").strip()
        if clean and clean not in out:
            out.append(clean)
    return out


def compare_live_validation_report_entries(latest: Mapping[str, Any], previous: Mapping[str, Any]) -> Dict[str, Any]:
    latest_summary = latest.get("summary") if isinstance(latest.get("summary"), Mapping) else {}
    previous_summary = previous.get("summary") if isinstance(previous.get("summary"), Mapping) else {}
    fields = (
        "public_live_checks",
        "credential_readiness",
        "credentialed_read_checks",
        "bridge_address_checks",
        "funded_live_order_check",
        "credentialed_read_ok",
        "safe_to_attempt_funded_order",
        "credential_live_verified",
        "funded_live_verified",
        "next_step",
    )
    changes = []
    for field in fields:
        latest_value = latest_summary.get(field)
        previous_value = previous_summary.get(field)
        if latest_value != previous_value:
            changes.append({"field": field, "previous": previous_value, "latest": latest_value})
    return {
        "latest_key": latest.get("key"),
        "previous_key": previous.get("key"),
        "changed": bool(changes),
        "changes": changes,
    }


def _empty_store() -> Dict[str, Any]:
    now = _now()
    return _RevisionedLiveValidationStore(
        {
            "version": LIVE_VALIDATION_REPORTS_VERSION,
            "created_at": now,
            "updated_at": now,
            "max_entries": DEFAULT_LIVE_VALIDATION_REPORTS_MAX_ENTRIES,
            "reports": {},
        },
        revision=_MISSING_STORE_REVISION,
    )


def _empty_decision_store() -> Dict[str, Any]:
    now = _now()
    return _RevisionedLiveValidationStore(
        {
            "version": LIVE_VALIDATION_DECISIONS_VERSION,
            "created_at": now,
            "updated_at": now,
            "decisions": {},
        },
        revision=_MISSING_STORE_REVISION,
    )


def _empty_promotion_proposal_snapshot_store() -> Dict[str, Any]:
    now = _now()
    return _RevisionedLiveValidationStore(
        {
            "version": LIVE_VALIDATION_PROMOTION_PROPOSAL_SNAPSHOTS_VERSION,
            "created_at": now,
            "updated_at": now,
            "max_entries": DEFAULT_LIVE_VALIDATION_PROMOTION_PROPOSAL_SNAPSHOTS_MAX_ENTRIES,
            "snapshots": {},
        },
        revision=_MISSING_STORE_REVISION,
    )


def _now() -> int:
    return int(time.time())


def _redact_value(value: Any, key: str = "") -> Any:
    normalized = str(key or "").strip().lower()
    sensitive_key = any(fragment in normalized for fragment in SENSITIVE_REPORT_KEY_FRAGMENTS)
    if sensitive_key and not isinstance(value, (bool, int, float)) and value not in (None, ""):
        return "***"
    if isinstance(value, Mapping):
        return {str(child_key): _redact_value(child_value, str(child_key)) for child_key, child_value in value.items()}
    if isinstance(value, list):
        return [_redact_value(item, key) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item, key) for item in value]
    return _jsonable(value)


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
    except TypeError:
        if isinstance(value, Mapping):
            return {str(key): _jsonable(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [_jsonable(item) for item in value]
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return deepcopy(value)


def _prune_reports(reports: Dict[str, Any], *, max_entries: int) -> None:
    for key, entry in list(reports.items()):
        if not isinstance(entry, Mapping):
            reports.pop(key, None)
    while len(reports) > max(1, int(max_entries)):
        oldest_key = min(
            reports,
            key=lambda item: (
                float((reports[item] or {}).get("stored_at") or 0),
                float((reports[item] or {}).get("stored_at_ns") or 0),
            ),
        )
        reports.pop(oldest_key, None)


def _prune_promotion_proposal_snapshots(snapshots: Dict[str, Any], *, max_entries: int) -> None:
    for key, entry in list(snapshots.items()):
        if not isinstance(entry, Mapping):
            snapshots.pop(key, None)
    while len(snapshots) > max(1, int(max_entries)):
        oldest_key = min(
            snapshots,
            key=lambda item: (
                float((snapshots[item] or {}).get("stored_at") or 0),
                float((snapshots[item] or {}).get("stored_at_ns") or 0),
            ),
        )
        snapshots.pop(oldest_key, None)


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
