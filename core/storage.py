from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from stat import S_ISREG
from typing import Any, Dict, Iterator

from .atomic_files import replace_file
from .config_security import ConfigSecurityError, assert_no_persisted_secrets
from .json_validation import loads_strict_json
from .models import AppConfig


CONFIG_PATH_ENV = "PREDICTION_MARKET_CONFIG_PATH"


class ConfigLoadError(RuntimeError):
    """Raised when an existing configuration file cannot be loaded safely."""


class ConfigConflictError(RuntimeError):
    """Raised when a stale process attempts to overwrite newer configuration."""


_STORAGE_REVISION_ATTR = "_market_sentinel_storage_revision"
_MISSING_REVISION = "missing"
_LOCK_TIMEOUT_SECONDS = 10.0
_CONFIG_THREAD_LOCKS_GUARD = threading.Lock()
_CONFIG_THREAD_LOCKS: Dict[str, tuple[threading.RLock, int]] = {}


def default_config_path() -> Path:
    configured_path = os.environ.get(CONFIG_PATH_ENV)
    if configured_path:
        return Path(configured_path).expanduser()
    return Path(__file__).resolve().parent.parent / "data" / "config.json"


DEFAULT_CONFIG_PATH = default_config_path()


def _read_config_bytes(path: Path) -> bytes | None:
    # exists() can suppress permission/I/O failures. Only a missing directory
    # entry is an uninitialized store; a failed read must never erase history.
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not S_ISREG(info.st_mode):
        raise ValueError("Configuration must be a regular file, not a symbolic link or special file.")
    return path.read_bytes()


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    try:
        raw = _read_config_bytes(path)
        if raw is None:
            cfg = AppConfig()
            setattr(cfg, _STORAGE_REVISION_ATTR, _MISSING_REVISION)
            return cfg
        data: Dict[str, Any] = loads_strict_json(raw.decode("utf-8"))
        assert_no_persisted_secrets(data)
        cfg = AppConfig.from_dict(data)
        setattr(cfg, _STORAGE_REVISION_ATTR, hashlib.sha256(raw).hexdigest())
        return cfg
    except ConfigSecurityError as exc:
        raise ConfigLoadError(
            f"Configuration file cannot be loaded safely: {path}. {exc} The file was left unchanged."
        ) from exc
    except Exception as exc:
        raise ConfigLoadError(
            f"Configuration file cannot be loaded: {path}. The file was left unchanged; restore a verified backup or reconcile the damaged state before restarting. Do not reset trading journals."
        ) from exc


def save_config(cfg: AppConfig, path: Path = DEFAULT_CONFIG_PATH) -> None:
    # The snapshot and revision update must be serialized together. Otherwise,
    # two threads saving the same object can replay an older snapshot after the
    # newer save has updated the object's revision.
    with _config_thread_lock(path):
        raw_data = cfg.to_dict()
        assert_no_persisted_secrets(raw_data)
        # Do not publish a snapshot that the strict loader will reject.
        AppConfig.from_dict(raw_data)
        data = (json.dumps(raw_data, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
        committed_revision = hashlib.sha256(data).hexdigest()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _config_file_lock(path):
            current_revision = _config_file_revision(path)
            expected_revision = getattr(cfg, _STORAGE_REVISION_ATTR, _MISSING_REVISION)
            if expected_revision != current_revision:
                raise ConfigConflictError(
                    f"Configuration changed in another process: {path}. Reload it before saving; no data was written."
                )
            fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "wb") as tmp:
                    tmp.write(data)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                replace_file(tmp_path, path)
                # Replacement is the commit point. Keep the object consistent
                # with the committed bytes even if directory durability fails.
                setattr(cfg, _STORAGE_REVISION_ATTR, committed_revision)
                _fsync_parent_directory(path)
            finally:
                if tmp_path.exists():
                    tmp_path.unlink()


@contextmanager
def _config_thread_lock(path: Path) -> Iterator[None]:
    """Serialize snapshots and commits for one config path within this process."""

    key = os.path.normcase(os.path.abspath(os.fspath(path)))
    with _CONFIG_THREAD_LOCKS_GUARD:
        entry = _CONFIG_THREAD_LOCKS.get(key)
        if entry is None:
            lock = threading.RLock()
            users = 0
        else:
            lock, users = entry
        _CONFIG_THREAD_LOCKS[key] = (lock, users + 1)
    try:
        with lock:
            yield
    finally:
        with _CONFIG_THREAD_LOCKS_GUARD:
            registered_lock, users = _CONFIG_THREAD_LOCKS[key]
            if users == 1:
                del _CONFIG_THREAD_LOCKS[key]
            else:
                _CONFIG_THREAD_LOCKS[key] = (registered_lock, users - 1)


def _config_file_revision(path: Path) -> str:
    raw = _read_config_bytes(path)
    if raw is None:
        return _MISSING_REVISION
    return hashlib.sha256(raw).hexdigest()


@contextmanager
def _config_file_lock(path: Path, timeout: float = _LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    """Hold a persistent sibling-file lock across compare-and-replace."""

    lock_path = path.with_name(f".{path.name}.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + max(0.0, float(timeout))
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size < 1:
                os.write(descriptor, b"0")
                os.fsync(descriptor)
            while not acquired:
                try:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise ConfigConflictError(
                            f"Timed out waiting for the configuration lock: {path}. No data was written."
                        ) from exc
                    time.sleep(0.05)
        else:
            import fcntl

            while not acquired:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise ConfigConflictError(
                            f"Timed out waiting for the configuration lock: {path}. No data was written."
                        ) from exc
                    time.sleep(0.05)
        yield
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _fsync_parent_directory(path: Path) -> None:
    """Persist the atomic configuration replacement on POSIX filesystems."""
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path.parent, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
