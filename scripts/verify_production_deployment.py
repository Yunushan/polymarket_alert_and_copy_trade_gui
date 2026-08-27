from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from stat import S_IFDIR, S_IFREG, S_IMODE, S_ISDIR, S_ISREG
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

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
        restore_backup,
    )
    from scripts.verify_service_health import check_health
else:  # Supports the documented `python /path/to/scripts/verify_production_deployment.py` invocation.
    from restore_state_backup import (
        DEFAULT_MAX_ARCHIVE_BYTES,
        DEFAULT_MAX_MEMBERS,
        DEFAULT_MAX_UNCOMPRESSED_BYTES,
        catalog_verified_backups,
        restore_backup,
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
ROLLBACK_DRILL_MAX_AGE_SECONDS = 24 * 60 * 60
ROLLBACK_DRILL_MAX_FUTURE_SKEW_SECONDS = 5 * 60
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
PROVIDER_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
EXTERNAL_PROBE_REPORT_TYPE = "market-sentinel-external-deployment-probe"
ROLLBACK_DRILL_REPORT_TYPE = "market-sentinel-production-rollback-drill"
ROLLBACK_DRILL_STEPS = (
    "current_release_healthy",
    "rollback_release_activated",
    "rollback_release_healthy",
    "current_release_reactivated",
    "current_release_healthy_after_reactivation",
)
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


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise RuntimeError("public proxy redirects are forbidden")


def _public_open(request: Request, timeout: float):
    if urlopen.__class__.__module__ == "unittest.mock":
        return urlopen(request, timeout=timeout)
    return build_opener(_RejectRedirects()).open(request, timeout=timeout)


def _validated_public_origin(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("public URL must be an absolute https origin-only URL")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address is not None and (not address.is_global or address.is_loopback or address.is_link_local or address.is_private):
        raise ValueError("public URL must not use a private, loopback, or link-local address")
    if address is None and not parsed.hostname.endswith(".example.com"):
        try:
            resolved = {
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
            }
        except OSError as exc:
            raise ValueError("public URL hostname could not be resolved safely") from exc
        if not resolved or any(not item.is_global for item in resolved):
            raise ValueError("public URL resolves to a private, loopback, or link-local address")
    port = f":{parsed.port}" if parsed.port and parsed.port != 443 else ""
    return f"https://{parsed.hostname.lower()}{port}"


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
    result = _check_private_path(parent, S_IFDIR, True, stat_reader)
    # Keep the evidence contract independent of the operator-selected output
    # directory name.  A stable check identifier lets the offline reviewer
    # require an exact, duplicate-free check inventory.
    result["name"] = "evidence_output_directory"
    return result


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


def _utc_datetime(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty UTC timestamp")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{label} is not a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def check_deployment_host_identity(
    provider: str,
    expected_host_identity_sha256: str,
    identity_file: Path = Path("/etc/machine-id"),
) -> dict[str, Any]:
    """Bind a self-hosted report to a protected provider label and machine identity."""

    normalized_provider = provider.strip().lower()
    expected_digest = expected_host_identity_sha256.strip().lower()
    base = {
        "name": "deployment_host_identity",
        "deployment_provider": normalized_provider,
        "host_identity_sha256": expected_digest,
    }
    try:
        identity_file = Path(identity_file)
        if not PROVIDER_SLUG.fullmatch(normalized_provider):
            raise ValueError("deployment provider must be a lowercase provider slug")
        if not SHA256_HEX.fullmatch(expected_digest):
            raise ValueError("expected host identity must be a lowercase SHA-256 digest")
        if not identity_file.is_absolute():
            raise ValueError("host identity file must use an absolute path")
        if any(path.is_symlink() for path in (identity_file, *identity_file.parents)):
            raise ValueError("host identity path must not contain symbolic links")
        metadata = identity_file.lstat()
        if not S_ISREG(metadata.st_mode) or S_IMODE(metadata.st_mode) & 0o022:
            raise ValueError("host identity file must be a non-writable regular file")
        if os.name == "posix" and getattr(metadata, "st_uid", -1) != 0:
            raise ValueError("host identity file must be root-owned")
        raw_identity = identity_file.read_bytes()
        if not raw_identity or len(raw_identity) > 4096:
            raise ValueError("host identity file is empty or oversized")
        actual_digest = hashlib.sha256(raw_identity.strip()).hexdigest()
        if actual_digest != expected_digest:
            raise ValueError("production host identity does not match the protected expected digest")
    except (OSError, RuntimeError, ValueError) as exc:
        return {**base, "status": "fail", "detail": str(exc)}
    return {
        **base,
        "status": "pass",
        "detail": "provider and root-owned machine identity match protected deployment configuration",
    }


def check_restore_drill(
    backup_directory: Path,
    *,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Actually restore the newest valid backup into a private isolated directory."""

    base = {"name": "verified_restore_drill", "mode": "isolated_full_restore"}
    try:
        catalog = catalog_verified_backups(
            Path(backup_directory),
            max_members=DEFAULT_MAX_MEMBERS,
            max_bytes=DEFAULT_MAX_UNCOMPRESSED_BYTES,
            max_archive_bytes=DEFAULT_MAX_ARCHIVE_BYTES,
        )
        observed_at = clock()
        backup = next(
            (
                item
                for item in catalog.verified
                if -BACKUP_MAX_FUTURE_SKEW_SECONDS
                <= observed_at - item.created_at.timestamp()
                <= BACKUP_MAX_AGE_SECONDS
            ),
            None,
        )
        if backup is None:
            raise RuntimeError("no recent verified backup is available for a restore drill")
        with tempfile.TemporaryDirectory(prefix="market-sentinel-restore-drill-") as temporary:
            restored = Path(temporary) / "restored-state"
            manifest = restore_backup(
                backup.archive_path,
                restored,
                max_members=DEFAULT_MAX_MEMBERS,
                max_bytes=DEFAULT_MAX_UNCOMPRESSED_BYTES,
                max_archive_bytes=DEFAULT_MAX_ARCHIVE_BYTES,
            )
            restored_files = 0
            restored_bytes = 0
            for candidate in restored.rglob("*"):
                if candidate.is_symlink():
                    raise RuntimeError("restore drill produced a symbolic link")
                if candidate.is_file():
                    restored_files += 1
                    restored_bytes += candidate.stat().st_size
            if (
                restored_files != manifest.get("file_count")
                or restored_bytes != manifest.get("verified_bytes")
            ):
                raise RuntimeError("restored file inventory does not match the verified backup manifest")
        completed_at = datetime.fromtimestamp(clock(), timezone.utc).isoformat().replace("+00:00", "Z")
    except (OSError, RuntimeError, ValueError, tarfile.TarError) as exc:
        return {**base, "status": "fail", "detail": str(exc)}
    return {
        **base,
        "status": "pass",
        "archive": backup.archive_path.name,
        "backup_created_at": backup.created_at.isoformat().replace("+00:00", "Z"),
        "backup_sha256": manifest["sha256"],
        "restored_file_count": restored_files,
        "restored_bytes": restored_bytes,
        "completed_at": completed_at,
        "detail": "the complete verified backup was restored and inventoried in an isolated private directory",
    }


def _read_strict_json_object(path: Path, *, maximum_bytes: int) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    if not raw or len(raw) > maximum_bytes:
        raise ValueError("JSON report is empty or oversized")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number: {value}")

    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("JSON report is malformed") from exc
    if not isinstance(payload, dict):
        raise ValueError("JSON report must be an object")
    return payload, hashlib.sha256(raw).hexdigest()


def check_rollback_drill(
    report_path: Path,
    *,
    expected_version: str,
    expected_current_revision: str,
    expected_frontend_sha256: str,
    deployment_provider: str,
    host_identity_sha256: str,
    public_origin: str,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Validate a recent root-owned journal from a completed production rollback drill."""

    base = {"name": "verified_production_rollback_drill"}
    try:
        report_path = Path(report_path)
        if not report_path.is_absolute():
            raise ValueError("rollback drill report must use an absolute path")
        if any(path.is_symlink() for path in (report_path, *report_path.parents)):
            raise ValueError("rollback drill report path must not contain symbolic links")
        metadata = report_path.lstat()
        if not S_ISREG(metadata.st_mode) or S_IMODE(metadata.st_mode) & 0o077:
            raise ValueError("rollback drill report must be a private regular file")
        if os.name == "posix" and getattr(metadata, "st_uid", -1) != 0:
            raise ValueError("rollback drill report must be root-owned")
        report, report_sha256 = _read_strict_json_object(report_path, maximum_bytes=64 * 1024)
        expected_fields = {
            "schema_version",
            "report_type",
            "drill_id",
            "started_at",
            "completed_at",
            "deployment_provider",
            "host_identity_sha256",
            "public_origin",
            "current_revision",
            "rollback_revision",
            "final_revision",
            "status",
            "steps",
        }
        if set(report) != expected_fields:
            raise ValueError("rollback drill report fields are not exact")
        drill_id = str(report.get("drill_id") or "")
        if str(uuid.UUID(drill_id)) != drill_id:
            raise ValueError("rollback drill id must be a canonical UUID")
        current_revision = expected_current_revision.strip().lower()
        rollback_revision = str(report.get("rollback_revision") or "").strip().lower()
        if (
            report.get("schema_version") != 1
            or report.get("report_type") != ROLLBACK_DRILL_REPORT_TYPE
            or report.get("status") != "ok"
            or report.get("deployment_provider") != deployment_provider.strip().lower()
            or report.get("host_identity_sha256") != host_identity_sha256.strip().lower()
            or report.get("public_origin") != _validated_public_origin(public_origin)
            or report.get("current_revision") != current_revision
            or report.get("final_revision") != current_revision
            or not COMMIT_SHA.fullmatch(rollback_revision)
            or rollback_revision == current_revision
        ):
            raise ValueError("rollback drill identity does not match this deployment")
        started_at = _utc_datetime(report.get("started_at"), "rollback started_at")
        completed_at = _utc_datetime(report.get("completed_at"), "rollback completed_at")
        observed_at = datetime.fromtimestamp(clock(), timezone.utc)
        age_seconds = (observed_at - completed_at).total_seconds()
        if (
            completed_at < started_at
            or (completed_at - started_at).total_seconds() > 60 * 60
            or age_seconds < -ROLLBACK_DRILL_MAX_FUTURE_SKEW_SECONDS
            or age_seconds > ROLLBACK_DRILL_MAX_AGE_SECONDS
        ):
            raise ValueError("rollback drill timing is stale, future-dated, reversed, or unbounded")
        steps = report.get("steps")
        if not isinstance(steps, list) or len(steps) != len(ROLLBACK_DRILL_STEPS):
            raise ValueError("rollback drill must contain the exact ordered step inventory")
        expected_revisions = (
            current_revision,
            rollback_revision,
            rollback_revision,
            current_revision,
            current_revision,
        )
        previous_time = started_at
        for position, (step, expected_name, expected_revision) in enumerate(
            zip(steps, ROLLBACK_DRILL_STEPS, expected_revisions, strict=True)
        ):
            if not isinstance(step, dict) or set(step) != {
                "name",
                "status",
                "revision",
                "observed_at",
                "api_version",
                "runtime_source_revision",
                "runtime_frontend_sha256",
            }:
                raise ValueError(f"rollback drill step {position} fields are not exact")
            step_time = _utc_datetime(step.get("observed_at"), f"rollback step {position} observed_at")
            if (
                step.get("name") != expected_name
                or step.get("status") != "pass"
                or step.get("revision") != expected_revision
                or step_time < previous_time
                or step_time > completed_at
            ):
                raise ValueError(f"rollback drill step {position} is invalid")
            is_current_health = expected_name in {
                "current_release_healthy",
                "current_release_healthy_after_reactivation",
            }
            is_rollback_health = expected_name == "rollback_release_healthy"
            if is_current_health:
                if (
                    step.get("api_version") != expected_version
                    or step.get("runtime_source_revision") != current_revision
                    or step.get("runtime_frontend_sha256") != expected_frontend_sha256
                ):
                    raise ValueError("current-release health proof in rollback drill is invalid")
            elif is_rollback_health:
                if (
                    not isinstance(step.get("api_version"), str)
                    or not step["api_version"].strip()
                    or step.get("runtime_source_revision") != rollback_revision
                    or not isinstance(step.get("runtime_frontend_sha256"), str)
                    or not SHA256_HEX.fullmatch(step["runtime_frontend_sha256"])
                ):
                    raise ValueError("rollback-release health proof in rollback drill is invalid")
            elif any(step.get(field) != "" for field in (
                "api_version",
                "runtime_source_revision",
                "runtime_frontend_sha256",
            )):
                raise ValueError("activation-only rollback steps must not claim health fingerprints")
            previous_time = step_time
    except (OSError, RuntimeError, ValueError) as exc:
        return {**base, "status": "fail", "detail": str(exc)}
    return {
        **base,
        "status": "pass",
        "drill_id": drill_id,
        "report_sha256": report_sha256,
        "completed_at": completed_at.isoformat().replace("+00:00", "Z"),
        "rollback_revision": rollback_revision,
        "final_revision": current_revision,
        "step_count": len(steps),
        "detail": "rollback release activation, health, and reactivation of the current release were journaled",
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


def _require_unauthorized(request: Request, timeout: float, label: str, *, opener=None) -> None:
    opener = opener or urlopen
    try:
        with opener(request, timeout=timeout) as response:
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
    *,
    require_upstream_token: bool = True,
) -> dict[str, Any]:
    origin = _validated_public_origin(url)
    if not username or not password:
        raise ValueError("public proxy verification requires non-empty Basic Auth credentials")
    if require_upstream_token and not upstream_token.strip():
        raise ValueError("public proxy verification requires a non-empty upstream API token")

    base_url = origin + "/"
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
            opener=_public_open,
        )

    health_url = urljoin(base_url, "api/health")

    headers = {"Accept": "application/json"}
    encoded = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    headers["Authorization"] = f"Basic {encoded}"
    with _public_open(Request(health_url, headers=headers, method="GET"), timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
        response_headers = {str(name).lower(): str(value) for name, value in response.headers.items()}
        missing = [name for name in REQUIRED_PROXY_HEADER_VALUES if name not in response_headers]
        if response.status != 200 or payload.get("status") != "ok":
            raise RuntimeError("public proxy health endpoint did not report status=ok")
        if expected_version and str(payload.get("api_version", "")) != expected_version:
            raise RuntimeError(
                f"public proxy reported version {payload.get('api_version')}, expected {expected_version}"
            )
        runtime_revision, runtime_frontend_sha256 = _require_runtime_fingerprints(
            payload,
            expected_source_revision,
            expected_frontend_sha256,
        )
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
        "runtime_source_revision": runtime_revision,
        "runtime_frontend_sha256": runtime_frontend_sha256,
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
    collection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    timestamp = collected_at or datetime.now(timezone.utc)
    payload = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "collected_at": timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": source if source is not None else source_identity(),
        "status": "ok" if all(check["status"] == "pass" for check in checks) else "failed",
        "checks": checks,
    }
    if collection is not None:
        payload["collection"] = collection
    return payload


def build_external_public_probe_evidence(
    *,
    public_origin: str,
    username: str,
    password: str,
    expected_version: str,
    expected_source_revision: str,
    expected_frontend_sha256: str,
    run_id: int,
    run_attempt: int,
    nonce: str,
    timeout: float,
    probed_at: datetime | None = None,
) -> dict[str, Any]:
    """Probe the public deployment from a separately attested GitHub-hosted job."""

    origin = _validated_public_origin(public_origin)
    revision = expected_source_revision.strip().lower()
    frontend_sha256 = expected_frontend_sha256.strip().lower()
    if not expected_version.strip() or not COMMIT_SHA.fullmatch(revision):
        raise ValueError("external probe requires an exact release version and source revision")
    if not SHA256_HEX.fullmatch(frontend_sha256):
        raise ValueError("external probe requires an exact frontend SHA-256")
    if run_id <= 0 or run_attempt <= 0 or nonce != f"{revision}:{run_id}:{run_attempt}":
        raise ValueError("external probe workflow identity is invalid")
    check = check_public_proxy(
        origin,
        username,
        password,
        timeout,
        expected_version.strip(),
        "",
        revision,
        frontend_sha256,
        require_upstream_token=False,
    )
    timestamp = (probed_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return {
        "schema_version": 1,
        "report_type": EXTERNAL_PROBE_REPORT_TYPE,
        "probed_at": timestamp.isoformat().replace("+00:00", "Z"),
        "status": "ok",
        "source_revision": revision,
        "collection": {
            "mode": "github_hosted_external_public_probe",
            "public_origin": origin,
            "expected_version": expected_version.strip(),
            "expected_source_revision": revision,
            "expected_frontend_sha256": frontend_sha256,
            "run_id": run_id,
            "run_attempt": run_attempt,
            "nonce": nonce,
            "runner_environment": "github-hosted",
        },
        "checks": [check],
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
    parser.add_argument("--deployment-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--evidence-run-id", type=int, default=0)
    parser.add_argument("--evidence-run-attempt", type=int, default=0)
    parser.add_argument("--evidence-nonce", default="")
    parser.add_argument(
        "--external-public-probe-only",
        action="store_true",
        help="Run only the public HTTPS probe for a separately attested GitHub-hosted job.",
    )
    parser.add_argument("--deployment-provider", default="")
    parser.add_argument("--expected-host-id-sha256", default="")
    parser.add_argument("--host-identity-file", type=Path, default=Path("/etc/machine-id"))
    parser.add_argument(
        "--rollback-drill-report",
        type=Path,
        default=Path("/var/lib/market-sentinel-rollback-drills/latest.json"),
    )
    parser.add_argument("--public-basic-user", default=os.environ.get("MARKET_SENTINEL_PUBLIC_BASIC_USER", ""))
    parser.add_argument(
        "--public-basic-password-env",
        default="MARKET_SENTINEL_PUBLIC_BASIC_PASSWORD",
        help="Environment variable containing the required public Basic Auth password when --public-url is set.",
    )
    args = parser.parse_args()

    if args.external_public_probe_only:
        try:
            if args.output is None:
                raise ValueError("external public probe requires --output")
            password = os.environ.get(args.public_basic_password_env, "")
            evidence = build_external_public_probe_evidence(
                public_origin=args.public_url,
                username=args.public_basic_user,
                password=password,
                expected_version=args.expected_version,
                expected_source_revision=args.expected_source_revision,
                expected_frontend_sha256=args.expected_frontend_sha256,
                run_id=args.evidence_run_id,
                run_attempt=args.evidence_run_attempt,
                nonce=args.evidence_nonce,
                timeout=args.timeout,
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            write_evidence(args.output, evidence)
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            print(json.dumps({"status": "failed", "detail": str(exc)}, sort_keys=True))
            return 1
        print(json.dumps(evidence, sort_keys=True))
        return 0

    checks: list[dict[str, Any]] = []
    evidence_source = source_identity(args.deployment_root)
    expected_version = args.expected_version.strip()
    expected_source_revision = args.expected_source_revision.strip().lower()
    expected_frontend_sha256 = args.expected_frontend_sha256.strip().lower()
    try:
        public_origin = _validated_public_origin(args.public_url) if args.public_url else ""
    except ValueError:
        public_origin = ""
    collection = {
        "mode": "production" if not args.skip_systemd and bool(args.public_url) else "local_smoke",
        "systemd_requested": not args.skip_systemd,
        "public_proxy_requested": bool(args.public_url),
        "public_origin": public_origin,
        "expected_version": expected_version,
        "expected_source_revision": expected_source_revision,
        "expected_frontend_sha256": expected_frontend_sha256,
        "deployment_provider": args.deployment_provider.strip().lower(),
        "host_identity_sha256": args.expected_host_id_sha256.strip().lower(),
        "restore_drill_requested": not args.skip_systemd and bool(public_origin),
        "rollback_drill_requested": not args.skip_systemd and bool(public_origin),
        "run_id": args.evidence_run_id,
        "run_attempt": args.evidence_run_attempt,
        "nonce": args.evidence_nonce,
    }
    missing_identity = False
    if args.public_url and not public_origin:
        missing_identity = True
        checks.append({"name": "public_origin", "status": "fail", "detail": "--public-url must be a canonical public HTTPS origin"})
    if not args.skip_systemd and (args.evidence_run_id <= 0 or args.evidence_run_attempt <= 0 or not args.evidence_nonce.strip()):
        missing_identity = True
        checks.append({"name": "workflow_nonce", "status": "fail", "detail": "production collection requires run id, attempt, and nonce"})
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
                if public_origin:
                    checks.append(
                        check_deployment_host_identity(
                            args.deployment_provider,
                            args.expected_host_id_sha256,
                            args.host_identity_file,
                        )
                    )
                    checks.append(check_restore_drill(args.backup_directory))
                    checks.append(
                        check_rollback_drill(
                            args.rollback_drill_report,
                            expected_version=expected_version,
                            expected_current_revision=expected_source_revision,
                            expected_frontend_sha256=expected_frontend_sha256,
                            deployment_provider=args.deployment_provider,
                            host_identity_sha256=args.expected_host_id_sha256,
                            public_origin=public_origin,
                        )
                    )
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
            if public_origin:
                password = os.environ.get(args.public_basic_password_env, "")
                checks.append(check_loopback_token_auth(args.loopback_url, args.timeout))
                checks.append(
                    check_public_proxy(
                        public_origin,
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
    evidence_source = source_identity(args.deployment_root)
    if not missing_identity:
        final_source_check = check_source_revision(expected_source_revision, evidence_source)
        final_source_check["name"] = "source_revision_final"
        checks.append(final_source_check)
    evidence = build_evidence(checks, source=evidence_source, collection=collection)
    if args.output:
        output_directory = check_evidence_output_directory(args.output)
        checks.append(output_directory)
        evidence_source = source_identity(args.deployment_root)
        if not missing_identity:
            pre_write_source_check = check_source_revision(expected_source_revision, evidence_source)
            pre_write_source_check["name"] = "source_revision_pre_write"
            checks.append(pre_write_source_check)
        evidence = build_evidence(checks, source=evidence_source, collection=collection)
        if output_directory["status"] == "pass":
            try:
                write_evidence(args.output, evidence)
            except OSError as exc:
                checks.append({"name": "evidence_output", "status": "fail", "detail": str(exc)})
                evidence = build_evidence(checks, source=evidence_source, collection=collection)
    print(json.dumps(evidence, sort_keys=True))
    return 0 if evidence["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
