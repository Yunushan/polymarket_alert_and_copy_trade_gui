from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit


COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
SOURCE_REVISION_ENV = "MARKET_SENTINEL_SOURCE_REVISION"
FRONTEND_SHA256_ENV = "MARKET_SENTINEL_FRONTEND_SHA256"


def canonical_https_origin(value: str) -> str:
    """Normalize origin syntax consistently; this does not authorize DNS targets."""
    if not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("origin must be an origin-only HTTPS URL without control characters")
    value = value.strip()
    if any(char.isspace() for char in value) or "?" in value or "#" in value or "\\" in value:
        raise ValueError("origin must be an origin-only HTTPS URL")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.path not in {"", "/"}
            or parsed.username is not None or parsed.password is not None):
        raise ValueError("origin must be an origin-only HTTPS URL")
    port = parsed.port
    if port == 0 or parsed.netloc.endswith(":"):
        raise ValueError("origin port must be between 1 and 65535")
    host = parsed.hostname
    if "%" in host:
        raise ValueError("origin hostname must not contain escapes or an IPv6 scope")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if parsed.netloc.startswith("["):
            raise ValueError("origin IP literal is unsupported") from None
        host = host.encode("idna").decode("ascii").lower()
        if host.endswith("."):
            host = host[:-1]
        if len(host) > 253 or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in host.split(".")
        ):
            raise ValueError("origin hostname is invalid") from None
    else:
        host = f"[{address.compressed}]" if address.version == 6 else str(address)
    suffix = f":{port}" if port is not None and port != 443 else ""
    return f"https://{host}{suffix}"


def safe_git_command(root: Path, *arguments: str) -> list[str]:
    """Build a Git command that trusts only the explicitly resolved deployment root.

    ``git status`` consults ``core.fsmonitor`` and can execute a helper named in
    repository-local configuration.  Deployment verification may run with
    elevated privileges, so disable that extension explicitly for every
    provenance read.
    """
    trusted_root = root.resolve()
    return [
        "git",
        "-c",
        f"safe.directory={trusted_root}",
        "-c",
        "core.fsmonitor=false",
        *arguments,
    ]


def safe_git_environment(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a deterministic Git environment without inherited repository overrides."""
    values = os.environ if environment is None else environment
    scrubbed = {
        str(name): str(value)
        for name, value in values.items()
        if not str(name).upper().startswith("GIT_")
    }
    scrubbed.update({"LC_ALL": "C", "TZ": "UTC"})
    return scrubbed


def git_top_level_matches(root: Path, candidate: str) -> bool:
    """Return whether Git's reported top level is the requested resolved root."""
    try:
        trusted_root = root.resolve(strict=True)
        reported = Path(candidate.strip())
        if not reported.is_absolute():
            return False
        reported_root = reported.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    return os.path.normcase(str(reported_root)) == os.path.normcase(str(trusted_root))


def git_source_revision(root: Path) -> str:
    """Return the checked-out commit without retaining Git diagnostics."""
    try:
        trusted_root = root.resolve(strict=True)
        environment = safe_git_environment()
        top_level = subprocess.run(
            safe_git_command(trusted_root, "rev-parse", "--show-toplevel"),
            cwd=trusted_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            env=environment,
        )
        if top_level.returncode != 0 or not git_top_level_matches(trusted_root, top_level.stdout):
            return ""
        result = subprocess.run(
            safe_git_command(trusted_root, "rev-parse", "--verify", "HEAD^{commit}"),
            cwd=trusted_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            env=environment,
        )
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        return ""
    candidate = result.stdout.strip().lower()
    return candidate if result.returncode == 0 and COMMIT_SHA.fullmatch(candidate) else ""


def _hash_field(digest: Any, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def frontend_tree_sha256(frontend_dir: Path) -> str:
    """Hash every regular frontend asset by relative path and content."""
    try:
        root = frontend_dir.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"frontend directory is unavailable: {frontend_dir}") from exc
    if not root.is_dir():
        raise ValueError(f"frontend path is not a directory: {root}")

    files: list[Path] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"frontend tree contains a symbolic link: {path}")
        if path.is_file():
            files.append(path)
        elif not path.is_dir():
            raise ValueError(f"frontend tree contains a non-regular entry: {path}")

    digest = hashlib.sha256(b"market-sentinel-frontend-tree-v1\0")
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        file_digest = hashlib.sha256()
        size = 0
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                size += len(chunk)
                file_digest.update(chunk)
        _hash_field(digest, relative)
        _hash_field(digest, str(size).encode("ascii"))
        _hash_field(digest, file_digest.digest())
    return digest.hexdigest()


def capture_runtime_identity(
    source_root: Path,
    frontend_dir: Path,
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Capture immutable process-start source and frontend fingerprints."""
    values = os.environ if environment is None else environment
    configured_revision = str(values.get(SOURCE_REVISION_ENV, "")).strip().lower()
    if configured_revision and not COMMIT_SHA.fullmatch(configured_revision):
        raise ValueError(f"{SOURCE_REVISION_ENV} must be a lowercase 40-character Git commit")
    checkout_revision = git_source_revision(source_root)
    if configured_revision and checkout_revision and configured_revision != checkout_revision:
        raise ValueError(
            f"{SOURCE_REVISION_ENV} does not match the checked-out commit "
            f"({configured_revision} != {checkout_revision})"
        )
    source_revision = configured_revision or checkout_revision

    frontend_sha256 = frontend_tree_sha256(frontend_dir) if frontend_dir.is_dir() else ""
    configured_frontend_sha256 = str(values.get(FRONTEND_SHA256_ENV, "")).strip().lower()
    if configured_frontend_sha256 and not SHA256_HEX.fullmatch(configured_frontend_sha256):
        raise ValueError(f"{FRONTEND_SHA256_ENV} must be a lowercase SHA-256 digest")
    if configured_frontend_sha256 and frontend_sha256 != configured_frontend_sha256:
        raise ValueError(
            f"{FRONTEND_SHA256_ENV} does not match the served frontend tree "
            f"({configured_frontend_sha256} != {frontend_sha256 or 'unavailable'})"
        )
    return {
        "source_revision": source_revision,
        "frontend_sha256": frontend_sha256,
    }
