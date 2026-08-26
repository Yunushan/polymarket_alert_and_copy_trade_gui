from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from stat import S_IFDIR, S_IFREG, S_IMODE, S_ISDIR, S_ISREG
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.deployment_identity import (
    SHA256_HEX,
    frontend_tree_sha256,
    git_top_level_matches,
    safe_git_command,
    safe_git_environment,
)

if __package__:
    from scripts.restore_state_backup import (
        DEFAULT_MAX_ARCHIVE_BYTES,
        DEFAULT_MAX_MEMBERS,
        DEFAULT_MAX_UNCOMPRESSED_BYTES,
        catalog_verified_backups,
    )
    from scripts.verify_service_health import check_health
else:  # Supports the documented `python /path/to/scripts/verify_production_deployment.py` invocation.
    from restore_state_backup import (
        DEFAULT_MAX_ARCHIVE_BYTES,
        DEFAULT_MAX_MEMBERS,
        DEFAULT_MAX_UNCOMPRESSED_BYTES,
        catalog_verified_backups,
    )
    from verify_service_health import check_health

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 uses the locked tomli dependency.
    import tomli as tomllib


CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]
REQUIRED_UNITS = (
    "market-sentinel-web.service",
    "market-sentinel-health.timer",
    "market-sentinel-backup.timer",
)
REQUIRED_PROXY_HEADER_VALUES = {
    "strict-transport-security": ("max-age=31536000", "includesubdomains"),
    "content-security-policy": (
        "default-src 'self'",
        "base-uri 'self'",
        "object-src 'none'",
        "frame-ancestors 'none'",
        "form-action 'self'",
        "connect-src 'self'",
        "img-src 'self' data:",
        "script-src 'self'",
        "style-src 'self'",
    ),
    "x-content-type-options": ("nosniff",),
    "x-frame-options": ("deny",),
    "referrer-policy": ("no-referrer",),
    "permissions-policy": ("camera=()", "geolocation=()", "microphone=()", "payment=()", "usb=()"),
    "cross-origin-opener-policy": ("same-origin",),
    "cross-origin-resource-policy": ("same-origin",),
}
BACKUP_MAX_AGE_SECONDS = 26 * 60 * 60
BACKUP_MAX_FUTURE_SKEW_SECONDS = 5 * 60
DEFAULT_BACKUP_DIRECTORY = Path("/var/lib/market-sentinel-backups")
DEFAULT_STATE_DIRECTORY = Path("/var/lib/market-sentinel")
DEFAULT_SERVICE_ENVIRONMENT_PATH = Path("/etc/market-sentinel/market-sentinel.env")
DURABLE_STATE_PATHS = {
    "POLYMARKET_ANALYTICS_CACHE_PATH": DEFAULT_STATE_DIRECTORY / "polymarket_analytics_cache.json",
    "POLYMARKET_LIVE_VALIDATION_REPORTS_PATH": (
        DEFAULT_STATE_DIRECTORY / "polymarket_live_validation_reports.json"
    ),
    "POLYMARKET_LIVE_VALIDATION_DECISIONS_PATH": (
        DEFAULT_STATE_DIRECTORY / "polymarket_live_validation_decisions.json"
    ),
    "POLYMARKET_LIVE_VALIDATION_PROMOTION_PROPOSAL_SNAPSHOTS_PATH": (
        DEFAULT_STATE_DIRECTORY / "polymarket_live_validation_promotion_proposal_snapshots.json"
    ),
}
EVIDENCE_SCHEMA_VERSION = 1
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
REQUIRED_PRIVATE_PATHS = (
    (DEFAULT_SERVICE_ENVIRONMENT_PATH, S_IFREG, True),
    (DEFAULT_STATE_DIRECTORY, S_IFDIR, False),
)
PUBLIC_PROXY_AUTH_PROBES = (
    ("GET", ""),
    ("GET", "api/health"),
    ("GET", "api/state"),
    ("GET", "metrics"),
    ("PATCH", "api/config"),
)


def _run_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "LC_ALL": "C", "TZ": "UTC"}
    return subprocess.run(args, capture_output=True, text=True, check=False, timeout=15, env=environment)


def source_identity(root: Path = PROJECT_ROOT) -> dict[str, str]:
    """Return minimal source provenance without retaining command output."""
    project_version = "unknown"
    try:
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        candidate = data.get("project", {}).get("version", "")
        if isinstance(candidate, str) and candidate.strip():
            project_version = candidate.strip()
    except (OSError, TypeError, ValueError):
        pass

    revision = ""
    revision_status = "unavailable"
    worktree_status = "unavailable"
    try:
        trusted_root = root.resolve(strict=True)
        environment = safe_git_environment()
        top_level_result = subprocess.run(
            safe_git_command(trusted_root, "rev-parse", "--show-toplevel"),
            cwd=trusted_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            env=environment,
        )
        if top_level_result.returncode == 0 and git_top_level_matches(trusted_root, top_level_result.stdout):
            before_result = subprocess.run(
                safe_git_command(trusted_root, "rev-parse", "--verify", "HEAD^{commit}"),
                cwd=trusted_root,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
                env=environment,
            )
            before = before_result.stdout.strip().lower()
            before_valid = before_result.returncode == 0 and COMMIT_SHA.fullmatch(before)
        else:
            revision_status = "invalid"
            before = ""
            before_valid = False
        if before_valid:
            status_result = subprocess.run(
                safe_git_command(trusted_root, "status", "--porcelain=v1", "--untracked-files=all"),
                cwd=trusted_root,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
                env=environment,
            )
            if status_result.returncode == 0:
                after_result = subprocess.run(
                    safe_git_command(trusted_root, "rev-parse", "--verify", "HEAD^{commit}"),
                    cwd=trusted_root,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                    env=environment,
                )
                after = after_result.stdout.strip().lower()
                if after_result.returncode == 0 and COMMIT_SHA.fullmatch(after) and before == after:
                    revision = before
                    revision_status = "ok"
                    worktree_status = "clean" if not status_result.stdout.strip() else "dirty"
                else:
                    revision_status = "invalid"
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        pass

    return {
        "project_version": project_version,
        "git_revision": revision,
        "git_revision_status": revision_status,
        "git_worktree_status": worktree_status,
    }


def check_source_revision(
    expected_revision: str,
    source: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Require deployment evidence to match the intended release commit."""
    expected = expected_revision.strip().lower()
    if not COMMIT_SHA.fullmatch(expected):
        return {
            "name": "source_revision",
            "status": "fail",
            "detail": "--expected-source-revision must be a lowercase 40-character Git commit",
        }
    identity = source if source is not None else source_identity()
    revision_status = identity.get("git_revision_status", "unavailable").strip().lower()
    if revision_status != "ok":
        return {
            "name": "source_revision",
            "status": "fail",
            "detail": f"deployed Git revision identity is {revision_status or 'unavailable'}",
        }
    actual = identity.get("git_revision", "").strip().lower()
    if actual != expected:
        return {
            "name": "source_revision",
            "status": "fail",
            "detail": f"deployed Git revision is {actual or 'unavailable'}, expected {expected}",
        }
    worktree_status = identity.get("git_worktree_status", "unavailable").strip().lower()
    if worktree_status != "clean":
        return {
            "name": "source_revision",
            "status": "fail",
            "detail": (
                "deployed source checkout is not clean "
                f"(git_worktree_status={worktree_status or 'unavailable'}); "
                "tracked, staged, and untracked source changes are not release evidence"
            ),
        }
    return {
        "name": "source_revision",
        "status": "pass",
        "detail": f"deployed Git revision matches {expected} and the checkout is clean",
    }


def _systemd_timestamp_seconds(value: str) -> float:
    normalized = value.strip()
    for pattern in ("%a %Y-%m-%d %H:%M:%S UTC", "%a %Y-%m-%d %H:%M:%S.%f UTC"):
        try:
            return datetime.strptime(normalized, pattern).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    raise ValueError(f"invalid systemd UTC timestamp: {normalized or 'missing'}")


def check_filesystem_permissions(
    stat_reader: Callable[[Path], object] = lambda path: path.lstat(),
    *,
    backup_directory: Path = DEFAULT_BACKUP_DIRECTORY,
) -> list[dict[str, Any]]:
    required_paths = (
        *REQUIRED_PRIVATE_PATHS,
        (Path(backup_directory), S_IFDIR, False),
    )
    return [
        _check_private_path(path, expected_type, require_root_owner, stat_reader)
        for path, expected_type, require_root_owner in required_paths
    ]


def _systemd_property(runner: CommandRunner, unit: str, property_name: str) -> str:
    result = runner(["systemctl", "show", unit, f"--property={property_name}", "--value"])
    if result.returncode != 0:
        raise RuntimeError(f"systemd could not read {property_name} for {unit}")
    value = result.stdout.strip()
    if not value:
        raise RuntimeError(f"systemd returned an empty {property_name} for {unit}")
    return value


def _contains_path_token(value: str, path: Path) -> bool:
    path_text = re.escape(path.as_posix())
    return re.search(rf"(?<![A-Za-z0-9_./-]){path_text}(?![A-Za-z0-9_./-])", value) is not None


def _read_process_environment(pid: int) -> bytes:
    return Path(f"/proc/{pid}/environ").read_bytes()


def check_durable_state_wiring(
    runner: CommandRunner = _run_command,
    process_environment_reader: Callable[[int], bytes] = _read_process_environment,
) -> dict[str, Any]:
    """Prove the running service stores every durable artifact inside the backed-up state root."""
    base = {
        "name": "durable_state_wiring",
        "state_directory": DEFAULT_STATE_DIRECTORY.as_posix(),
        "backup_source": DEFAULT_STATE_DIRECTORY.as_posix(),
        "durable_store_count": len(DURABLE_STATE_PATHS),
    }
    try:
        environment_files = _systemd_property(runner, "market-sentinel-web.service", "EnvironmentFiles")
        if not _contains_path_token(environment_files, DEFAULT_SERVICE_ENVIRONMENT_PATH):
            raise RuntimeError("the web service does not load the protected service environment file")
        required_environment_pattern = re.compile(
            rf"(?<![A-Za-z0-9_./-]){re.escape(DEFAULT_SERVICE_ENVIRONMENT_PATH.as_posix())}"
            r"\s+\(ignore_errors=no\)(?!\S)"
        )
        if required_environment_pattern.search(environment_files) is None:
            raise RuntimeError("the web service environment file is optional instead of fail-fast")

        writable_paths = _systemd_property(runner, "market-sentinel-web.service", "ReadWritePaths")
        if not _contains_path_token(writable_paths, DEFAULT_STATE_DIRECTORY):
            raise RuntimeError("the durable state directory is not writable in the web service sandbox")

        backup_command = _systemd_property(runner, "market-sentinel-backup.service", "ExecStart")
        backup_source_pattern = re.compile(
            rf"(?:^|[\s;])--source(?:=|\s+)[\"']?{re.escape(DEFAULT_STATE_DIRECTORY.as_posix())}"
            rf"[\"']?(?=$|[\s;}}])"
        )
        if backup_source_pattern.search(backup_command) is None:
            raise RuntimeError("the backup service does not capture the durable state directory")

        raw_pid = _systemd_property(runner, "market-sentinel-web.service", "MainPID")
        try:
            pid = int(raw_pid)
        except ValueError as exc:
            raise RuntimeError("the web service MainPID is invalid") from exc
        if pid <= 0:
            raise RuntimeError("the web service is not running")

        raw_environment = process_environment_reader(pid)
        if len(raw_environment) > 1024 * 1024:
            raise RuntimeError("the web service environment exceeds the verifier safety limit")
        observed: dict[str, str] = {}
        required_names = {name.encode("ascii"): name for name in DURABLE_STATE_PATHS}
        for entry in raw_environment.split(b"\0"):
            key, separator, value = entry.partition(b"=")
            name = required_names.get(key)
            if not separator or name is None:
                continue
            if name in observed:
                raise RuntimeError(f"the running service has duplicate {name} entries")
            try:
                observed[name] = value.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RuntimeError(f"the running service has a non-UTF-8 {name} value") from exc

        for name, expected_path in DURABLE_STATE_PATHS.items():
            actual = observed.get(name)
            if actual is None:
                raise RuntimeError(f"the running service is missing {name}")
            if actual != expected_path.as_posix():
                raise RuntimeError(f"the running service has an unsafe {name} value")
            if not expected_path.is_relative_to(DEFAULT_STATE_DIRECTORY):
                raise RuntimeError(f"the expected {name} path is outside the durable state directory")
    except (OSError, RuntimeError, ValueError) as exc:
        return {**base, "status": "fail", "detail": str(exc)}

    return {
        **base,
        "status": "pass",
        "detail": (
            "running service paths are exact, sandbox-writable, and beneath the effective backup source"
        ),
    }


def _check_private_path(
    path: Path,
    expected_type: int,
    require_root_owner: bool,
    stat_reader: Callable[[Path], object],
) -> dict[str, Any]:
    try:
        metadata = stat_reader(path)
        mode = int(metadata.st_mode)
        owner = int(metadata.st_uid)
        valid_type = S_ISREG(mode) if expected_type == S_IFREG else S_ISDIR(mode)
        private = S_IMODE(mode) & 0o077 == 0
        owner_valid = not require_root_owner or owner == 0
        passed = valid_type and private and owner_valid
        detail = f"mode={S_IMODE(mode):04o}; uid={owner}; expected={'file' if expected_type == S_IFREG else 'directory'}"
    except OSError as exc:
        passed = False
        detail = str(exc)
    return {
        "name": f"filesystem_private_{path.name}",
        "status": "pass" if passed else "fail",
        "detail": detail,
    }


def check_evidence_output_directory(
    output_path: Path,
    stat_reader: Callable[[Path], object] = lambda path: path.stat(),
) -> dict[str, Any]:
    """Require output evidence to live in a private, root-owned existing directory."""
    parent = output_path.parent
    symlinked_component = next((path for path in (parent, *parent.parents) if path.is_symlink()), None)
    if symlinked_component is not None:
        return {
            "name": f"filesystem_private_{parent.name}",
            "status": "fail",
            "detail": f"refusing symbolic-link evidence path component: {symlinked_component}",
        }
    return _check_private_path(parent, S_IFDIR, True, stat_reader)


def check_systemd(
    runner: CommandRunner = _run_command,
    clock: Callable[[], float] = time.time,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for unit in REQUIRED_UNITS:
        for command in ("is-active", "is-enabled"):
            result = runner(["systemctl", command, unit])
            checks.append(
                {
                    "name": f"systemd_{command}_{unit}",
                    "status": "pass" if result.returncode == 0 else "fail",
                    "detail": (result.stdout or result.stderr).strip(),
                }
            )
    completion = runner(
        [
            "systemctl",
            "show",
            "market-sentinel-backup.service",
            "--property=Result",
            "--property=ExecMainStatus",
            "--property=ExecMainExitTimestamp",
            "--value",
        ]
    )
    values = [value.strip() for value in completion.stdout.splitlines()]
    result, exit_status, completed_at = (values + ["", "", ""])[:3]
    try:
        backup_age_seconds = clock() - _systemd_timestamp_seconds(completed_at)
    except ValueError:
        backup_age_seconds = float("inf")
    completed = (
        completion.returncode == 0
        and result == "success"
        and exit_status == "0"
        and completed_at not in {"", "n/a"}
        and backup_age_seconds >= -BACKUP_MAX_FUTURE_SKEW_SECONDS
        and backup_age_seconds <= BACKUP_MAX_AGE_SECONDS
    )
    checks.append(
        {
            "name": "systemd_recent_success_market-sentinel-backup.service",
            "status": "pass" if completed else "fail",
            "detail": (
                f"result={result or 'unknown'}; exit_status={exit_status or 'unknown'}; "
                f"completed_at={completed_at or 'unknown'}; backup_age_seconds={backup_age_seconds:.0f}; "
                f"max_age_seconds={BACKUP_MAX_AGE_SECONDS}; max_future_skew_seconds={BACKUP_MAX_FUTURE_SKEW_SECONDS}"
            ),
        }
    )
    return checks


def check_backup_evidence(
    backup_directory: Path,
    *,
    clock: Callable[[], float] = time.time,
    stat_reader: Callable[[Path], object] = lambda path: path.lstat(),
) -> dict[str, Any]:
    """Require a recent, bounded, cryptographically verified state backup."""
    backup_directory = Path(backup_directory)
    base = {
        "name": "verified_recent_state_backup",
        "directory": str(backup_directory),
    }
    if not backup_directory.is_absolute():
        return {
            **base,
            "status": "fail",
            "detail": "trusted backup directory must be an absolute path",
        }
    symlinked_component = next(
        (path for path in (backup_directory, *backup_directory.parents) if path.is_symlink()),
        None,
    )
    if symlinked_component is not None:
        return {
            **base,
            "status": "fail",
            "detail": f"refusing symbolic-link backup path component: {symlinked_component}",
        }
    directory_check = _check_private_path(backup_directory, S_IFDIR, False, stat_reader)
    if directory_check["status"] != "pass":
        return {
            **base,
            "status": "fail",
            "detail": f"backup directory is not a trusted private directory: {directory_check['detail']}",
        }

    try:
        catalog = catalog_verified_backups(
            backup_directory,
            max_members=DEFAULT_MAX_MEMBERS,
            max_bytes=DEFAULT_MAX_UNCOMPRESSED_BYTES,
            max_archive_bytes=DEFAULT_MAX_ARCHIVE_BYTES,
        )
    except (OSError, RuntimeError, ValueError, tarfile.TarError) as exc:
        return {**base, "status": "fail", "detail": str(exc)}

    observed_at = clock()
    for backup in catalog.verified:
        age_seconds = observed_at - backup.created_at.timestamp()
        if -BACKUP_MAX_FUTURE_SKEW_SECONDS <= age_seconds <= BACKUP_MAX_AGE_SECONDS:
            manifest = backup.manifest
            return {
                **base,
                "status": "pass",
                "archive": backup.archive_path.name,
                "created_at": backup.created_at.isoformat().replace("+00:00", "Z"),
                "backup_age_seconds": round(age_seconds),
                "sha256": manifest["sha256"],
                "file_count": manifest["file_count"],
                "verified_archive_bytes": manifest["verified_archive_bytes"],
                "verified_tar_bytes": manifest["verified_tar_bytes"],
                "verified_bytes": manifest["verified_bytes"],
                "verified_pairs": len(catalog.verified),
                "invalid_pairs": len(catalog.invalid_pairs),
                "orphan_archives": len(catalog.orphan_archives),
                "orphan_manifests": len(catalog.orphan_manifests),
                "detail": "archive checksum, manifest, member paths, types, counts, and size bounds verified",
            }

    return {
        **base,
        "status": "fail",
        "verified_pairs": len(catalog.verified),
        "invalid_pairs": len(catalog.invalid_pairs),
        "orphan_archives": len(catalog.orphan_archives),
        "orphan_manifests": len(catalog.orphan_manifests),
        "detail": (
            "no cryptographically verified backup pair has a creation timestamp within "
            f"{BACKUP_MAX_AGE_SECONDS} seconds and no more than "
            f"{BACKUP_MAX_FUTURE_SKEW_SECONDS} seconds in the future"
        ),
    }


def _require_runtime_fingerprints(
    payload: dict[str, Any],
    expected_source_revision: str,
    expected_frontend_sha256: str,
) -> tuple[str, str]:
    runtime_revision = str(payload.get("runtime_source_revision") or "").strip().lower()
    runtime_frontend_sha256 = str(payload.get("runtime_frontend_sha256") or "").strip().lower()
    if expected_source_revision and runtime_revision != expected_source_revision:
        raise RuntimeError(
            "health endpoint reported process-start source revision "
            f"{runtime_revision or 'unavailable'}, expected {expected_source_revision}; restart the service"
        )
    if expected_frontend_sha256 and runtime_frontend_sha256 != expected_frontend_sha256:
        raise RuntimeError(
            "health endpoint reported process-start frontend digest "
            f"{runtime_frontend_sha256 or 'unavailable'}, expected {expected_frontend_sha256}; restart the service"
        )
    return runtime_revision, runtime_frontend_sha256


def check_loopback(
    url: str,
    token: str,
    timeout: float,
    expected_version: str = "",
    expected_source_revision: str = "",
    expected_frontend_sha256: str = "",
    frontend_dir: Path = PROJECT_ROOT / "frontend" / "dist",
) -> dict[str, Any]:
    payload = check_health(url, token, timeout)
    version = str(payload["api_version"])
    if expected_version and version != expected_version:
        raise RuntimeError(f"health endpoint reported version {version}, expected {expected_version}")
    runtime_revision, runtime_frontend = _require_runtime_fingerprints(
        payload,
        expected_source_revision,
        expected_frontend_sha256,
    )
    disk_frontend = ""
    if expected_frontend_sha256:
        disk_frontend = frontend_tree_sha256(frontend_dir)
        if disk_frontend != expected_frontend_sha256:
            raise RuntimeError(
                f"served frontend tree digest is {disk_frontend}, expected {expected_frontend_sha256}"
            )
    return {
        "name": "loopback_health",
        "status": "pass",
        "api_version": version,
        "runtime_source_revision": runtime_revision,
        "runtime_frontend_sha256": runtime_frontend,
        "disk_frontend_sha256": disk_frontend,
    }


def check_loopback_metrics(url: str, token: str, timeout: float) -> dict[str, Any]:
    """Verify the authenticated Prometheus endpoint without persisting metric values."""
    headers = {"Accept": "text/plain; version=0.0.4"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urlopen(Request(url, headers=headers, method="GET"), timeout=timeout) as response:
        content_type = str(response.headers.get("Content-Type") or "").lower()
        body = response.read().decode("utf-8")
    if response.status != 200:
        raise RuntimeError(f"loopback metrics endpoint returned HTTP {response.status}")
    if not content_type.startswith("text/plain; version=0.0.4"):
        raise RuntimeError("loopback metrics endpoint did not return Prometheus text format")
    required = (
        "market_sentinel_http_requests_total",
        "market_sentinel_http_request_duration_seconds_total",
        "market_sentinel_http_requests_completed_total",
    )
    missing = [name for name in required if name not in body]
    if missing:
        raise RuntimeError("loopback metrics endpoint is missing required metrics: " + ", ".join(missing))
    return {"name": "loopback_metrics", "status": "pass", "format": "prometheus"}


def _require_unauthorized(request: Request, timeout: float, label: str) -> None:
    try:
        with urlopen(request, timeout=timeout) as response:
            status = response.status
    except HTTPError as exc:
        try:
            if exc.code != 401:
                raise RuntimeError(f"unauthenticated {label} returned HTTP {exc.code}, expected 401") from exc
        finally:
            exc.close()
        return
    raise RuntimeError(f"unauthenticated {label} was accepted with HTTP {status}")


def check_loopback_token_auth(url: str, timeout: float) -> dict[str, Any]:
    """Prove that the upstream API rejects a tokenless loopback request."""
    _require_unauthorized(
        Request(url, headers={"Accept": "application/json"}, method="GET"),
        timeout,
        "loopback API request",
    )
    return {"name": "loopback_token_auth", "status": "pass"}


def check_public_proxy(
    url: str,
    username: str,
    password: str,
    timeout: float,
    expected_version: str = "",
    upstream_token: str = "",
    expected_source_revision: str = "",
    expected_frontend_sha256: str = "",
) -> dict[str, Any]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("public URL must be an absolute https URL")
    if not username or not password:
        raise ValueError("public proxy verification requires non-empty Basic Auth credentials")
    if not upstream_token.strip():
        raise ValueError("public proxy verification requires a non-empty upstream API token")

    base_url = url.rstrip("/") + "/"
    for method, relative_url in PUBLIC_PROXY_AUTH_PROBES:
        probe_url = urljoin(base_url, relative_url)
        headers = {"Accept": "application/json"}
        body = None
        if method == "PATCH":
            headers["Content-Type"] = "application/json"
            body = b"{"
        _require_unauthorized(
            Request(probe_url, data=body, headers=headers, method=method),
            timeout,
            f"public proxy {method} {urlparse(probe_url).path or '/'}",
        )

    health_url = urljoin(base_url, "api/health")

    headers = {"Accept": "application/json"}
    encoded = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    headers["Authorization"] = f"Basic {encoded}"
    with urlopen(Request(health_url, headers=headers, method="GET"), timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
        response_headers = {str(name).lower(): str(value) for name, value in response.headers.items()}
        missing = [name for name in REQUIRED_PROXY_HEADER_VALUES if name not in response_headers]
        if response.status != 200 or payload.get("status") != "ok":
            raise RuntimeError("public proxy health endpoint did not report status=ok")
        if expected_version and str(payload.get("api_version", "")) != expected_version:
            raise RuntimeError(
                f"public proxy reported version {payload.get('api_version')}, expected {expected_version}"
            )
        _require_runtime_fingerprints(payload, expected_source_revision, expected_frontend_sha256)
        if response_headers.get("cache-control") != "no-store":
            raise RuntimeError("public proxy health endpoint is missing Cache-Control: no-store")
        if missing:
            raise RuntimeError("public proxy is missing security headers: " + ", ".join(missing))
        weak_headers = [
            name
            for name, expected_values in REQUIRED_PROXY_HEADER_VALUES.items()
            if any(value not in response_headers[name].lower() for value in expected_values)
        ]
        if weak_headers:
            raise RuntimeError("public proxy has incomplete security-header policy: " + ", ".join(weak_headers))
        if response_headers.get("server"):
            raise RuntimeError("public proxy exposes a Server header")
    return {
        "name": "public_https_proxy",
        "status": "pass",
        "api_version": payload.get("api_version"),
        "unauthenticated_probes": len(PUBLIC_PROXY_AUTH_PROBES),
    }


def write_evidence(path: Path, payload: dict[str, Any]) -> None:
    """Atomically persist redacted deployment evidence in a pre-validated directory."""
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            if os.name == "posix":
                os.fchmod(handle.fileno(), 0o600)
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_parent_directory(path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _fsync_parent_directory(path: Path) -> None:
    """Persist the directory entry created by the atomic replacement on POSIX hosts."""
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path.parent, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def build_evidence(
    checks: list[dict[str, Any]],
    *,
    collected_at: datetime | None = None,
    source: dict[str, str] | None = None,
) -> dict[str, Any]:
    timestamp = collected_at or datetime.now(timezone.utc)
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "collected_at": timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": source if source is not None else source_identity(),
        "status": "ok" if all(check["status"] == "pass" for check in checks) else "failed",
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect read-only MarketSentinel production deployment evidence.")
    parser.add_argument("--loopback-url", default="http://127.0.0.1:8765/api/health")
    parser.add_argument("--loopback-metrics-url", default="http://127.0.0.1:8765/metrics")
    parser.add_argument("--token", default=os.environ.get("MARKET_SENTINEL_API_TOKEN", ""))
    parser.add_argument("--expected-version", default="")
    parser.add_argument(
        "--expected-source-revision",
        default="",
        help="Required lowercase 40-character Git commit for the deployed release source.",
    )
    parser.add_argument(
        "--expected-frontend-sha256",
        default="",
        help="Required trusted SHA-256 fingerprint of the reviewed frontend tree.",
    )
    parser.add_argument(
        "--frontend-dir",
        type=Path,
        default=PROJECT_ROOT / "frontend" / "dist",
        help="Served frontend directory to hash independently of the running process.",
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument(
        "--skip-systemd",
        action="store_true",
        help=(
            "Skip Linux systemd, filesystem-ownership, and backup-archive checks for an isolated loopback smoke test."
        ),
    )
    parser.add_argument(
        "--backup-directory",
        type=Path,
        default=DEFAULT_BACKUP_DIRECTORY,
        help="Trusted private directory containing state backup archive/manifest pairs.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional path for an atomically written, mode-0600 JSON evidence record.",
    )
    parser.add_argument("--public-url", default="")
    parser.add_argument("--public-basic-user", default=os.environ.get("MARKET_SENTINEL_PUBLIC_BASIC_USER", ""))
    parser.add_argument(
        "--public-basic-password-env",
        default="MARKET_SENTINEL_PUBLIC_BASIC_PASSWORD",
        help="Environment variable containing the required public Basic Auth password when --public-url is set.",
    )
    args = parser.parse_args()

    checks: list[dict[str, Any]] = []
    evidence_source = source_identity()
    expected_version = args.expected_version.strip()
    expected_source_revision = args.expected_source_revision.strip().lower()
    expected_frontend_sha256 = args.expected_frontend_sha256.strip().lower()
    missing_identity = False
    if not expected_version:
        missing_identity = True
        checks.append(
            {
                "name": "expected_version",
                "status": "fail",
                "detail": "--expected-version is required to prove the deployed release identity",
            }
        )
    if not expected_source_revision:
        missing_identity = True
        checks.append(
            {
                "name": "expected_source_revision",
                "status": "fail",
                "detail": "--expected-source-revision is required to prove the deployed source identity",
            }
        )
    if not expected_frontend_sha256:
        missing_identity = True
        checks.append(
            {
                "name": "expected_frontend_sha256",
                "status": "fail",
                "detail": "--expected-frontend-sha256 is required to prove the served frontend identity",
            }
        )
    elif not SHA256_HEX.fullmatch(expected_frontend_sha256):
        missing_identity = True
        checks.append(
            {
                "name": "expected_frontend_sha256",
                "status": "fail",
                "detail": "--expected-frontend-sha256 must be a lowercase 64-character SHA-256 digest",
            }
        )
    if not missing_identity:
        try:
            checks.append(check_source_revision(expected_source_revision, evidence_source))
            if not args.skip_systemd:
                checks.extend(check_systemd())
                checks.extend(check_filesystem_permissions(backup_directory=args.backup_directory))
                checks.append(check_durable_state_wiring())
                checks.append(check_backup_evidence(args.backup_directory))
            if args.public_url and not args.token.strip():
                raise ValueError("public proxy verification requires a non-empty upstream API token")
            checks.append(
                check_loopback(
                    args.loopback_url,
                    args.token,
                    args.timeout,
                    expected_version,
                    expected_source_revision,
                    expected_frontend_sha256,
                    args.frontend_dir,
                )
            )
            checks.append(check_loopback_metrics(args.loopback_metrics_url, args.token, args.timeout))
            if args.public_url:
                password = os.environ.get(args.public_basic_password_env, "")
                checks.append(check_loopback_token_auth(args.loopback_url, args.timeout))
                checks.append(
                    check_public_proxy(
                        args.public_url,
                        args.public_basic_user,
                        password,
                        args.timeout,
                        expected_version,
                        args.token,
                        expected_source_revision,
                        expected_frontend_sha256,
                    )
                )
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
            checks.append({"name": "deployment_verifier", "status": "fail", "detail": str(exc)})

    # Re-read source provenance after all host/network probes. Evidence must
    # never report success for a revision different from the one actually
    # recorded in the artifact.
    evidence_source = source_identity()
    if not missing_identity:
        final_source_check = check_source_revision(expected_source_revision, evidence_source)
        final_source_check["name"] = "source_revision_final"
        checks.append(final_source_check)
    evidence = build_evidence(checks, source=evidence_source)
    if args.output:
        output_directory = check_evidence_output_directory(args.output)
        checks.append(output_directory)
        evidence_source = source_identity()
        if not missing_identity:
            pre_write_source_check = check_source_revision(expected_source_revision, evidence_source)
            pre_write_source_check["name"] = "source_revision_pre_write"
            checks.append(pre_write_source_check)
        evidence = build_evidence(checks, source=evidence_source)
        if output_directory["status"] == "pass":
            try:
                write_evidence(args.output, evidence)
            except OSError as exc:
                checks.append({"name": "evidence_output", "status": "fail", "detail": str(exc)})
                evidence = build_evidence(checks, source=evidence_source)
    print(json.dumps(evidence, sort_keys=True))
    return 0 if evidence["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
