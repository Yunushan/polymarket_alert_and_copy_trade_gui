from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.atomic_files import atomic_text_writer, atomic_write_text, fsync_parent_directory


class AtomicFileTests(unittest.TestCase):
    def test_stream_failure_or_interrupt_preserves_previous_file(self) -> None:
        for failure in (OSError("disk full"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "export.csv"
                target.write_text("previous\n", encoding="utf-8")
                with self.assertRaises(type(failure)):
                    with atomic_text_writer(target, newline="") as stream:
                        stream.write("partial\n")
                        raise failure
                self.assertEqual(target.read_text(), "previous\n")
                self.assertFalse(list(target.parent.glob(f".{target.name}.*.tmp")))

    def test_atomic_stream_syncs_then_publishes_complete_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "export.csv"
            with patch("core.atomic_files.os.fsync", wraps=os.fsync) as sync:
                with atomic_text_writer(target, newline="") as stream:
                    stream.write("header\r\n")
                    self.assertFalse(target.exists())
                    stream.write("value\r\n")
            self.assertTrue(sync.called)
            self.assertEqual(target.read_bytes(), b"header\r\nvalue\r\n")

    def test_failed_replace_preserves_previous_file_and_removes_temporary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "export.json"
            target.write_text("previous", encoding="utf-8")
            with patch("core.atomic_files.os.replace", side_effect=PermissionError("busy")):
                with self.assertRaises(PermissionError):
                    atomic_write_text(target, "new")
            self.assertEqual(target.read_text(), "previous")
            self.assertFalse(list(target.parent.glob(f".{target.name}.*.tmp")))

    def test_atomic_write_uses_an_exclusive_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "evidence.json"
            predictable_temporary = target.with_name(f"{target.name}.tmp")
            predictable_temporary.write_text("keep", encoding="utf-8")

            self.assertEqual(atomic_write_text(target, "{\"status\": \"ok\"}\n"), target)

            self.assertEqual(target.read_text(encoding="utf-8"), "{\"status\": \"ok\"}\n")
            self.assertEqual(predictable_temporary.read_text(encoding="utf-8"), "keep")
            self.assertFalse(list(target.parent.glob(f".{target.name}.*.tmp")))

    def test_parent_directory_is_synced_on_posix(self) -> None:
        path = Path("output") / "report.json"
        with (
            patch("core.atomic_files.os.name", "posix"),
            patch("core.atomic_files.os.open", return_value=42) as open_directory,
            patch("core.atomic_files.os.fsync") as sync,
            patch("core.atomic_files.os.close") as close,
        ):
            fsync_parent_directory(path)

        open_directory.assert_called_once_with(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        sync.assert_called_once_with(42)
        close.assert_called_once_with(42)
