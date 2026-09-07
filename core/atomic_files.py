from __future__ import annotations

import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TextIO


def replace_file(source: Path, target: Path) -> None:
    """Atomically replace a file, tolerating briefly held Windows file handles."""
    for attempt in range(10):
        try:
            os.replace(source, target)
            return
        except PermissionError as error:
            # Windows can report access denied instead of sharing violation
            # while a reader or scanner holds the destination without delete
            # sharing. Permanent denial still fails after this bounded retry.
            if getattr(error, "winerror", None) not in {5, 32, 33} or attempt == 9:
                raise
            time.sleep(min(0.025 * (2 ** attempt), 0.2))


def fsync_parent_directory(path: Path) -> None:
    """Persist a completed atomic replacement on POSIX filesystems."""
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path.parent, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def atomic_text_writer(path: Path, *, newline: str | None = None) -> Iterator[TextIO]:
    """Stream text to a temporary file; publish only after successful completion."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline=newline) as handle:
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
        replace_file(temporary, path)
        fsync_parent_directory(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def atomic_write_text(path: Path, content: str) -> Path:
    """Write text through an exclusive temporary file and atomically publish it."""
    with atomic_text_writer(path) as handle:
        handle.write(content)
    return path
