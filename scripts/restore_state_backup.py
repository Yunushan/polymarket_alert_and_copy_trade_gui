from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import stat
import tarfile
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterator

if __package__:
    from scripts.backup_state import (
        BACKUP_PREFIX,
        DEFAULT_MAX_ARCHIVE_BYTES,
        DEFAULT_MAX_MEMBERS,
        DEFAULT_MAX_TAR_METADATA_BYTES,
        DEFAULT_MAX_UNCOMPRESSED_BYTES,
        MANIFEST_SUFFIX,
        SCHEMA_VERSION,
    )
else:  # Supports the documented `python /path/to/scripts/restore_state_backup.py` invocation.
    from backup_state import (
        BACKUP_PREFIX,
        DEFAULT_MAX_ARCHIVE_BYTES,
        DEFAULT_MAX_MEMBERS,
        DEFAULT_MAX_TAR_METADATA_BYTES,
        DEFAULT_MAX_UNCOMPRESSED_BYTES,
        MANIFEST_SUFFIX,
        SCHEMA_VERSION,
    )


WINDOWS_RESERVED_NAMES = {
    "AUX",
    "CON",
    "CONIN$",
    "CONOUT$",
    "NUL",
    "PRN",
    *(f"COM{suffix}" for suffix in (*range(1, 10), "¹", "²", "³")),
    *(f"LPT{suffix}" for suffix in (*range(1, 10), "¹", "²", "³")),
}
WINDOWS_INVALID_CHARACTERS = frozenset('<>:"|?*')
TAR_BLOCK_BYTES = 512
TAR_RECORD_BYTES = 10_240
TAR_EXTENSION_TYPES = frozenset({b"x", b"g", b"L", b"K"})
TAR_SUPPORTED_TYPES = frozenset({b"\0", b"0", b"5", *TAR_EXTENSION_TYPES})


@dataclass(frozen=True)
class VerifiedBackup:
    archive_path: Path
    created_at: datetime
    manifest: dict[str, Any]


@dataclass(frozen=True)
class BackupCatalog:
    verified: tuple[VerifiedBackup, ...]
    orphan_archives: tuple[str, ...]
    orphan_manifests: tuple[str, ...]
    invalid_pairs: tuple[str, ...]


def _mkdir_private(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _create_restore_destination(path: Path) -> Path:
    """Atomically create a new private destination below an existing parent."""
    requested = Path(path)
    if not requested.name:
        raise ValueError("restore destination must name a new directory")
    try:
        parent = requested.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"restore destination parent must already exist: {requested.parent}") from exc
    if not parent.is_dir():
        raise ValueError(f"restore destination parent is not a directory: {parent}")

    destination = parent / requested.name
    if os.path.lexists(destination):
        raise ValueError(f"restore destination must not already exist: {destination}")
    try:
        destination.mkdir(mode=0o700)
    except FileExistsError as exc:
        # Preserve the fail-closed result if another process creates or swaps
        # the final path between the existence check and mkdir.
        raise ValueError(f"restore destination must not already exist: {destination}") from exc
    os.chmod(destination, 0o700)
    return destination


def _safe_member_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    windows_path = PureWindowsPath(name)
    if (
        "\\" in name
        or path.is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or ".." in path.parts
        or ".." in windows_path.parts
        or not path.parts
    ):
        raise RuntimeError(f"backup archive contains unsafe member path: {name}")
    for component in path.parts:
        reserved_stem = component.split(".", 1)[0].upper()
        if (
            component.endswith((" ", "."))
            or reserved_stem in WINDOWS_RESERVED_NAMES
            or any(character in WINDOWS_INVALID_CHARACTERS or ord(character) < 32 for character in component)
        ):
            raise RuntimeError(f"backup archive contains unsafe Windows member path: {name}")
    return path


def _confined_target(destination: Path, relative_path: PurePosixPath) -> Path:
    """Return a restore target only when native path parsing keeps it below destination."""
    target = destination.joinpath(*relative_path.parts)
    resolved_target = target.resolve(strict=False)
    try:
        resolved_target.relative_to(destination)
    except ValueError as exc:
        raise RuntimeError(f"backup archive member escapes restore destination: {relative_path}") from exc
    return target


def _load_manifest(archive_path: Path) -> dict[str, Any]:
    manifest_path = archive_path.with_name(archive_path.name + ".json")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"unable to read backup manifest: {manifest_path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("backup manifest has an unsupported schema version")
    if payload.get("archive") != archive_path.name:
        raise RuntimeError("backup manifest does not match the selected archive")
    expected_sha256 = payload.get("sha256")
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        raise RuntimeError("backup manifest has no usable SHA-256 checksum")
    return payload


def _manifest_created_at(payload: dict[str, Any]) -> datetime:
    value = payload.get("created_at")
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("backup manifest has no usable creation timestamp")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        created_at = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise RuntimeError("backup manifest has no usable creation timestamp") from exc
    if created_at.tzinfo is None:
        raise RuntimeError("backup manifest creation timestamp must include a timezone")
    return created_at.astimezone(timezone.utc)


def _max_tar_stream_bytes(max_members: int, max_bytes: int) -> int:
    """Bound payload, per-member headers/padding, extension records, and EOF padding."""
    max_extension_headers = max_members + 16
    per_record_overhead = TAR_BLOCK_BYTES + TAR_BLOCK_BYTES - 1
    return (
        max_bytes
        + (max_members * per_record_overhead)
        + (max_extension_headers * per_record_overhead)
        + DEFAULT_MAX_TAR_METADATA_BYTES
        + TAR_RECORD_BYTES
    )


def _decompress_gzip_bounded(compressed: Any, expanded: Any, max_tar_bytes: int) -> int:
    compressed.seek(0)
    expanded_bytes = 0
    try:
        with gzip.GzipFile(fileobj=compressed, mode="rb") as gzip_stream:
            while True:
                read_size = min(1024 * 1024, max_tar_bytes - expanded_bytes + 1)
                chunk = gzip_stream.read1(read_size)
                if not chunk:
                    break
                expanded_bytes += len(chunk)
                if expanded_bytes > max_tar_bytes:
                    raise RuntimeError("backup tar stream exceeds restore safety limits")
                expanded.write(chunk)
    except (EOFError, OSError) as exc:
        raise RuntimeError("backup archive has an invalid or incomplete gzip stream") from exc
    expanded.flush()
    expanded.seek(0)
    return expanded_bytes


def _parse_tar_size(field: bytes) -> int:
    if not field or field[0] & 0x80:
        raise RuntimeError("backup archive contains an unsupported tar size encoding")
    value = field.rstrip(b"\0 ").lstrip(b" ")
    if not value:
        return 0
    if any(character < ord("0") or character > ord("7") for character in value):
        raise RuntimeError("backup archive contains an invalid tar size")
    return int(value, 8)


def _preflight_tar_headers(expanded: Any, expanded_bytes: int, max_members: int) -> None:
    """Reject dangerous extension sizes before handing the stream to tarfile."""
    expanded.seek(0)
    offset = 0
    zero_headers = 0
    member_count = 0
    extension_count = 0
    extension_bytes = 0
    max_extension_headers = max_members + 16
    while offset < expanded_bytes:
        header = expanded.read(TAR_BLOCK_BYTES)
        if len(header) != TAR_BLOCK_BYTES:
            raise RuntimeError("backup archive has a truncated tar header")
        offset += TAR_BLOCK_BYTES
        if header == b"\0" * TAR_BLOCK_BYTES:
            zero_headers += 1
            if zero_headers < 2:
                continue
            while offset < expanded_bytes:
                chunk = expanded.read(min(1024 * 1024, expanded_bytes - offset))
                if not chunk or any(chunk):
                    raise RuntimeError("backup archive contains data after the tar end marker")
                offset += len(chunk)
            expanded.seek(0)
            return
        if zero_headers:
            raise RuntimeError("backup archive contains an incomplete tar end marker")

        type_flag = header[156:157]
        if type_flag not in TAR_SUPPORTED_TYPES:
            raise RuntimeError("backup archive contains an unsupported member type")
        member_size = _parse_tar_size(header[124:136])
        if type_flag in TAR_EXTENSION_TYPES:
            extension_count += 1
            extension_bytes += member_size
            if extension_count > max_extension_headers or extension_bytes > DEFAULT_MAX_TAR_METADATA_BYTES:
                raise RuntimeError("backup archive tar metadata exceeds restore safety limits")
        else:
            member_count += 1
            if member_count > max_members:
                raise RuntimeError("backup archive exceeds restore safety limits")

        padded_size = ((member_size + TAR_BLOCK_BYTES - 1) // TAR_BLOCK_BYTES) * TAR_BLOCK_BYTES
        if offset + padded_size > expanded_bytes:
            raise RuntimeError("backup archive member exceeds the bounded tar stream")
        expanded.seek(padded_size, os.SEEK_CUR)
        offset += padded_size

    raise RuntimeError("backup archive has no complete tar end marker")


@contextmanager
def _verified_archive(
    archive_path: Path,
    *,
    max_members: int,
    max_bytes: int,
    max_archive_bytes: int,
) -> Iterator[tuple[dict[str, Any], tarfile.TarFile, tuple[tarfile.TarInfo, ...]]]:
    """Yield a verified private snapshot so path swaps cannot alter extraction."""
    archive_path = archive_path.resolve()
    if not archive_path.is_file():
        raise ValueError(f"backup archive does not exist: {archive_path}")
    if max_members < 1 or max_bytes < 1 or max_archive_bytes < 1:
        raise ValueError("max-members, max-bytes, and max-archive-bytes must be positive")
    manifest = _load_manifest(archive_path)
    digest = hashlib.sha256()
    with (
        archive_path.open("rb") as source,
        tempfile.TemporaryFile() as compressed_snapshot,
        tempfile.TemporaryFile() as expanded_snapshot,
    ):
        copied_bytes = 0
        while True:
            # Read at most one byte beyond the configured compressed limit so
            # an archive that grows while being copied still fails closed.
            read_size = min(1024 * 1024, max_archive_bytes - copied_bytes + 1)
            chunk = source.read(read_size)
            if not chunk:
                break
            copied_bytes += len(chunk)
            if copied_bytes > max_archive_bytes:
                raise RuntimeError("backup archive compressed size exceeds restore safety limits")
            digest.update(chunk)
            compressed_snapshot.write(chunk)
        if digest.hexdigest() != manifest["sha256"]:
            raise RuntimeError("backup archive checksum does not match its manifest")

        expanded_bytes = _decompress_gzip_bounded(
            compressed_snapshot,
            expanded_snapshot,
            _max_tar_stream_bytes(max_members, max_bytes),
        )
        _preflight_tar_headers(expanded_snapshot, expanded_bytes, max_members)
        expanded_snapshot.seek(0)
        try:
            with tarfile.open(fileobj=expanded_snapshot, mode="r:") as archive:
                members: list[tarfile.TarInfo] = []
                total_size = 0
                for member_count, member in enumerate(archive, start=1):
                    if member_count > max_members:
                        raise RuntimeError("backup archive exceeds restore safety limits")
                    _safe_member_path(member.name)
                    if not (member.isfile() or member.isdir()):
                        raise RuntimeError(f"backup archive contains unsupported member type: {member.name}")
                    total_size += member.size
                    if total_size > max_bytes:
                        raise RuntimeError("backup archive exceeds restore safety limits")
                    members.append(member)
                if manifest.get("file_count") != len(members):
                    raise RuntimeError("backup manifest file count does not match archive contents")
                if manifest.get("uncompressed_bytes") != total_size:
                    raise RuntimeError("backup manifest byte count does not match archive contents")
                yield {
                    **manifest,
                    "verified_archive_bytes": copied_bytes,
                    "verified_tar_bytes": expanded_bytes,
                    "verified_bytes": total_size,
                }, archive, tuple(members)
        except tarfile.TarError as exc:
            raise RuntimeError(f"backup archive cannot be read: {archive_path}") from exc


def verify_backup(
    archive_path: Path,
    max_members: int = DEFAULT_MAX_MEMBERS,
    max_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
    max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
) -> dict[str, Any]:
    with _verified_archive(
        archive_path,
        max_members=max_members,
        max_bytes=max_bytes,
        max_archive_bytes=max_archive_bytes,
    ) as (
        manifest,
        _archive,
        _members,
    ):
        return manifest


def catalog_verified_backups(
    directory: Path,
    max_members: int = DEFAULT_MAX_MEMBERS,
    max_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
    max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
) -> BackupCatalog:
    """Inspect complete backup pairs without following backup-entry symlinks."""
    requested_directory = Path(directory)
    try:
        directory_metadata = requested_directory.lstat()
    except OSError as exc:
        raise ValueError(f"backup directory cannot be inspected: {requested_directory}") from exc
    if requested_directory.is_symlink() or not stat.S_ISDIR(directory_metadata.st_mode):
        raise ValueError(f"backup directory must be a real directory: {requested_directory}")
    directory = requested_directory.resolve(strict=True)

    archives: dict[str, Path] = {}
    manifests: dict[str, Path] = {}
    try:
        entries = tuple(directory.iterdir())
    except OSError as exc:
        raise ValueError(f"backup directory cannot be inspected: {directory}") from exc
    for entry in entries:
        name = entry.name
        if name.startswith(BACKUP_PREFIX) and name.endswith(".tar.gz"):
            archives[name] = entry
        elif name.startswith(BACKUP_PREFIX) and name.endswith(f".tar.gz{MANIFEST_SUFFIX}"):
            manifests[name[: -len(MANIFEST_SUFFIX)]] = entry

    archive_names = set(archives)
    manifest_names = set(manifests)
    orphan_archives = tuple(sorted(archive_names - manifest_names))
    orphan_manifests = tuple(
        sorted(manifests[name].name for name in manifest_names - archive_names)
    )
    invalid_pairs: list[str] = []
    verified: list[VerifiedBackup] = []
    for archive_name in sorted(archive_names & manifest_names):
        archive_path = archives[archive_name]
        manifest_path = manifests[archive_name]
        try:
            archive_metadata = archive_path.lstat()
            manifest_metadata = manifest_path.lstat()
            if (
                archive_path.is_symlink()
                or manifest_path.is_symlink()
                or not stat.S_ISREG(archive_metadata.st_mode)
                or not stat.S_ISREG(manifest_metadata.st_mode)
            ):
                raise RuntimeError("backup pair entries must be regular files")
            payload = verify_backup(
                archive_path,
                max_members=max_members,
                max_bytes=max_bytes,
                max_archive_bytes=max_archive_bytes,
            )
            verified.append(
                VerifiedBackup(
                    archive_path=archive_path,
                    created_at=_manifest_created_at(payload),
                    manifest=payload,
                )
            )
        except (OSError, RuntimeError, ValueError, tarfile.TarError):
            invalid_pairs.append(archive_name)

    verified.sort(key=lambda item: (item.created_at, item.archive_path.name), reverse=True)
    return BackupCatalog(
        verified=tuple(verified),
        orphan_archives=orphan_archives,
        orphan_manifests=orphan_manifests,
        invalid_pairs=tuple(sorted(invalid_pairs)),
    )


def restore_backup(
    archive_path: Path,
    destination: Path,
    max_members: int = DEFAULT_MAX_MEMBERS,
    max_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
    max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
) -> dict[str, Any]:
    with _verified_archive(
        archive_path,
        max_members=max_members,
        max_bytes=max_bytes,
        max_archive_bytes=max_archive_bytes,
    ) as (
        manifest,
        archive,
        members,
    ):
        destination = _create_restore_destination(destination)

        for member in members:
            relative_path = _safe_member_path(member.name)
            target = _confined_target(destination, relative_path)
            if member.isdir():
                _mkdir_private(target)
                continue
            _mkdir_private(target.parent)
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError(f"backup archive member has no file content: {member.name}")
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(target, member.mode & 0o600 or 0o600)
        return manifest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify or restore a MarketSentinel state backup into a new private directory."
    )
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--max-members", type=int, default=DEFAULT_MAX_MEMBERS)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_UNCOMPRESSED_BYTES)
    parser.add_argument("--max-archive-bytes", type=int, default=DEFAULT_MAX_ARCHIVE_BYTES)
    args = parser.parse_args()
    try:
        if args.destination is None:
            result = verify_backup(
                args.archive,
                args.max_members,
                args.max_bytes,
                args.max_archive_bytes,
            )
        else:
            result = restore_backup(
                args.archive,
                args.destination,
                args.max_members,
                args.max_bytes,
                args.max_archive_bytes,
            )
    except (OSError, RuntimeError, ValueError, tarfile.TarError) as exc:
        raise SystemExit(f"State backup verification or restore failed: {exc}") from exc
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
