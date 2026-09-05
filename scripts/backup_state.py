from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import stat
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator


BACKUP_PREFIX = "market-sentinel-state-"
MANIFEST_SUFFIX = ".json"
SCHEMA_VERSION = 1
SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3")
DEFAULT_MAX_MEMBERS = 10_000
DEFAULT_MAX_UNCOMPRESSED_BYTES = 1_073_741_824
DEFAULT_MAX_ARCHIVE_BYTES = 268_435_456
DEFAULT_MAX_TAR_METADATA_BYTES = 1_048_576


class _BoundedWriter:
    """Reject a compressed archive write before it crosses the configured cap."""

    def __init__(self, raw_handle: Any, max_bytes: int) -> None:
        self._raw_handle = raw_handle
        self._max_bytes = max_bytes
        self.written = 0

    def write(self, payload: bytes) -> int:
        if self.written + len(payload) > self._max_bytes:
            raise RuntimeError("backup archive compressed size exceeds restore safety limits")
        written = self._raw_handle.write(payload)
        self.written += written
        return written

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw_handle, name)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(path: Path, source: Path) -> PurePosixPath:
    relative = path.relative_to(source)
    value = PurePosixPath(relative.as_posix())
    if value.is_absolute() or ".." in value.parts or not value.parts:
        raise RuntimeError(f"unsafe backup path: {relative}")
    return value


def _iter_regular_files(source: Path) -> Iterator[tuple[Path, PurePosixPath]]:
    for root, directory_names, file_names in os.walk(source, followlinks=False):
        root_path = Path(root)
        for directory_name in directory_names:
            candidate = root_path / directory_name
            if candidate.is_symlink():
                raise RuntimeError(f"refusing to back up symbolic link: {candidate}")
        directory_names.sort()
        for file_name in sorted(file_names):
            candidate = root_path / file_name
            if candidate.is_symlink() or not candidate.is_file():
                raise RuntimeError(f"refusing to back up non-regular file: {candidate}")
            yield candidate, _safe_relative(candidate, source)


def _is_sqlite_database(path: Path) -> bool:
    return path.suffix.lower() in SQLITE_SUFFIXES


def _is_sqlite_sidecar(path: Path) -> bool:
    if path.name.startswith(".") and path.name.endswith(".writer.lock"):
        return True
    for suffix in ("-wal", "-shm", "-journal"):
        if path.name.endswith(suffix):
            return Path(path.name[: -len(suffix)]).suffix.lower() in SQLITE_SUFFIXES
    return False


def _snapshot_sqlite(source: Path, staging_path: Path) -> Path:
    staging_path.parent.mkdir(parents=True, exist_ok=True)
    source_uri = source.resolve().as_uri() + "?mode=ro"
    source_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None
    try:
        source_connection = sqlite3.connect(source_uri, uri=True, timeout=30)
        destination_connection = sqlite3.connect(staging_path)
        source_connection.backup(destination_connection)
    except sqlite3.Error as exc:
        raise RuntimeError(f"unable to create a consistent SQLite backup for {source}") from exc
    finally:
        if destination_connection is not None:
            destination_connection.close()
        if source_connection is not None:
            source_connection.close()
    return staging_path


def _snapshot_regular_file(source: Path, staging_path: Path, max_bytes: int) -> tuple[Path, int]:
    """Copy one stable regular-file view and reject a concurrent source mutation."""
    staging_path.parent.mkdir(parents=True, exist_ok=True)
    copied = 0
    try:
        with source.open("rb") as input_handle:
            before = os.fstat(input_handle.fileno())
            path_before = source.stat(follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode) or not stat.S_ISREG(path_before.st_mode):
                raise RuntimeError(f"refusing to back up non-regular file: {source}")
            if (before.st_dev, before.st_ino) != (path_before.st_dev, path_before.st_ino):
                raise RuntimeError(f"backup source changed while being opened: {source}")

            with staging_path.open("xb") as output_handle:
                while True:
                    chunk = input_handle.read(min(1024 * 1024, max_bytes - copied + 1))
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > max_bytes:
                        raise RuntimeError("backup source exceeds uncompressed restore safety limits")
                    output_handle.write(chunk)
                output_handle.flush()
                os.fsync(output_handle.fileno())

            after = os.fstat(input_handle.fileno())
            path_after = source.stat(follow_symlinks=False)
            stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
            if (
                copied != before.st_size
                or any(getattr(before, field) != getattr(after, field) for field in stable_fields)
                or any(getattr(after, field) != getattr(path_after, field) for field in stable_fields)
            ):
                raise RuntimeError(f"backup source changed while being snapshotted: {source}")
        os.chmod(staging_path, 0o600)
        return staging_path, copied
    except Exception:
        staging_path.unlink(missing_ok=True)
        raise


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".market-sentinel-", suffix=".json", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _fsync_directory(directory: Path) -> None:
    """Persist backup publication and retention changes on POSIX filesystems."""
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_locations(source: Path, destination: Path) -> tuple[Path, Path]:
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_dir():
        raise ValueError(f"backup source is not a directory: {source}")
    if destination.is_relative_to(source):
        raise ValueError("backup destination must not be inside the source directory")
    destination.mkdir(parents=True, exist_ok=True)
    if not destination.is_dir():
        raise ValueError(f"backup destination is not a directory: {destination}")
    os.chmod(destination, 0o700)
    return source, destination


def _prune_backups(
    destination: Path,
    retain: int,
    *,
    max_members: int = DEFAULT_MAX_MEMBERS,
    max_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
    max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
) -> list[str]:
    if __package__:
        from scripts.restore_state_backup import catalog_verified_backups
    else:  # Supports the documented direct script invocation.
        from restore_state_backup import catalog_verified_backups

    catalog = catalog_verified_backups(
        destination,
        max_members=max_members,
        max_bytes=max_bytes,
        max_archive_bytes=max_archive_bytes,
    )
    removed: list[str] = []
    for backup in catalog.verified[retain:]:
        path = backup.archive_path
        path.unlink()
        path.with_name(path.name + MANIFEST_SUFFIX).unlink(missing_ok=True)
        removed.append(path.name)
    if removed:
        _fsync_directory(destination)
    return removed


def create_backup(
    source: Path,
    destination: Path,
    retain: int = 14,
    *,
    max_members: int = DEFAULT_MAX_MEMBERS,
    max_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
    max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
) -> dict[str, Any]:
    if retain < 1 or max_members < 1 or max_bytes < 1 or max_archive_bytes < 1:
        raise ValueError("retain, max-members, max-bytes, and max-archive-bytes must be positive")
    source, destination = _validate_locations(source, destination)
    now = _utc_now()
    archive_name = f"{BACKUP_PREFIX}{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}.tar.gz"
    archive_path = destination / archive_name
    manifest_path = archive_path.with_name(archive_path.name + MANIFEST_SUFFIX)
    file_count = 0
    uncompressed_bytes = 0
    with tempfile.TemporaryDirectory(prefix=".market-sentinel-publish-", dir=destination) as staging_name:
        staging_root = Path(staging_name)
        os.chmod(staging_root, 0o700)
        snapshots_root = staging_root / "snapshots"
        snapshots_root.mkdir(mode=0o700)
        staged_archive = staging_root / archive_name
        staged_manifest = staged_archive.with_name(staged_archive.name + MANIFEST_SUFFIX)

        with staged_archive.open("xb") as raw_handle:
            bounded_handle = _BoundedWriter(raw_handle, max_archive_bytes)
            with tarfile.open(fileobj=bounded_handle, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
                for file_path, relative_path in _iter_regular_files(source):
                    if _is_sqlite_sidecar(file_path):
                        continue
                    if file_count >= max_members:
                        raise RuntimeError("backup source exceeds member restore safety limits")

                    snapshot_path = snapshots_root.joinpath(*relative_path.parts)
                    remaining_bytes = max_bytes - uncompressed_bytes
                    if _is_sqlite_database(file_path):
                        archive_source = _snapshot_sqlite(file_path, snapshot_path)
                        snapshot_bytes = archive_source.stat().st_size
                        if snapshot_bytes > remaining_bytes:
                            raise RuntimeError("backup source exceeds uncompressed restore safety limits")
                    else:
                        archive_source, snapshot_bytes = _snapshot_regular_file(
                            file_path,
                            snapshot_path,
                            remaining_bytes,
                        )

                    file_count += 1
                    uncompressed_bytes += snapshot_bytes
                    with archive_source.open("rb") as input_handle:
                        member = archive.gettarinfo(str(archive_source), arcname=str(relative_path))
                        archive.addfile(member, input_handle)
            raw_handle.flush()
            os.fsync(raw_handle.fileno())

        os.chmod(staged_archive, 0o600)
        if staged_archive.stat().st_size > max_archive_bytes:
            raise RuntimeError("backup archive compressed size exceeds restore safety limits")
        manifest = {
            "archive": archive_name,
            "created_at": now.isoformat().replace("+00:00", "Z"),
            "file_count": file_count,
            "schema_version": SCHEMA_VERSION,
            "sha256": _sha256(staged_archive),
            "source_name": source.name,
            "uncompressed_bytes": uncompressed_bytes,
        }
        _write_json_atomic(staged_manifest, manifest)

        if __package__:
            from scripts.restore_state_backup import verify_backup
        else:  # Supports the documented direct script invocation.
            from restore_state_backup import verify_backup

        verify_backup(
            staged_archive,
            max_members=max_members,
            max_bytes=max_bytes,
            max_archive_bytes=max_archive_bytes,
        )

        if os.path.lexists(archive_path) or os.path.lexists(manifest_path):
            raise RuntimeError(f"refusing to replace an existing backup pair: {archive_name}")
        os.replace(staged_archive, archive_path)
        _fsync_directory(destination)
        os.replace(staged_manifest, manifest_path)
        _fsync_directory(destination)

    manifest["pruned"] = _prune_backups(
        destination,
        retain,
        max_members=max_members,
        max_bytes=max_bytes,
        max_archive_bytes=max_archive_bytes,
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Create an integrity-manifested MarketSentinel state backup with atomic publication.")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--retain", type=int, default=14)
    parser.add_argument("--max-members", type=int, default=DEFAULT_MAX_MEMBERS)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_UNCOMPRESSED_BYTES)
    parser.add_argument("--max-archive-bytes", type=int, default=DEFAULT_MAX_ARCHIVE_BYTES)
    args = parser.parse_args()
    try:
        result = create_backup(
            args.source,
            args.destination,
            args.retain,
            max_members=args.max_members,
            max_bytes=args.max_bytes,
            max_archive_bytes=args.max_archive_bytes,
        )
    except (OSError, RuntimeError, ValueError, tarfile.TarError) as exc:
        raise SystemExit(f"State backup failed: {exc}") from exc
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
