from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core.atomic_files import atomic_text_writer, atomic_write_text, fsync_parent_directory, replace_file


class AtomicFileTests(unittest.TestCase):
    def test_transient_windows_replace_errors_retry_the_same_atomic_operation(self) -> None:
        for code in (5, 32, 33):
            with self.subTest(winerror=code), tempfile.TemporaryDirectory() as directory:
                source, target = Path(directory) / "new", Path(directory) / "old"
                source.write_text("next")
                target.write_text("previous")
                error = PermissionError("Windows file contention")
                error.winerror = code
                original_replace = os.replace
                attempts = []

                def replace_once_unblocked(left, right, *, target=target, source=source,
                                           attempts=attempts, error=error, original_replace=original_replace):
                    self.assertEqual(target.read_text(), "previous")
                    self.assertEqual(source.read_text(), "next")
                    attempts.append((left, right))
                    if len(attempts) < 3:
                        raise error
                    original_replace(left, right)

                with patch("core.atomic_files.os.replace", side_effect=replace_once_unblocked), patch("core.atomic_files.time.sleep") as sleep:
                    replace_file(source, target)
                self.assertEqual(attempts, [(source, target)] * 3)
                self.assertEqual(sleep.call_count, 2)
                self.assertEqual(target.read_text(), "next")
                self.assertFalse(source.exists())

    def test_permanent_windows_denial_is_bounded_and_preserves_both_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "new", Path(directory) / "old"
            source.write_text("next")
            target.write_text("previous")
            error = PermissionError("permanent denial")
            error.winerror = 5
            with patch("core.atomic_files.os.replace", side_effect=error) as replace, patch("core.atomic_files.time.sleep") as sleep:
                with self.assertRaises(PermissionError) as raised:
                    replace_file(source, target)
            self.assertIs(raised.exception, error)
            self.assertEqual(replace.call_count, 10)
            self.assertEqual(sleep.call_count, 9)
            self.assertLess(sum(call.args[0] for call in sleep.call_args_list), 1.5)
            self.assertEqual(target.read_text(), "previous")
            self.assertEqual(source.read_text(), "next")

    def test_other_replace_errors_and_interruptions_are_not_retried(self) -> None:
        for error in (PermissionError("denied"), FileNotFoundError("missing"), OSError("disk failure"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__), patch("core.atomic_files.os.replace", side_effect=error) as replace, patch("core.atomic_files.time.sleep") as sleep:
                with self.assertRaises(type(error)):
                    replace_file(Path("source"), Path("target"))
                replace.assert_called_once()
                sleep.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows file sharing contract")
    def test_real_windows_reader_without_delete_sharing_can_release_before_retry(self) -> None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                               wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        create_file.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "export.json"
            target.write_text("previous")
            handle = create_file(str(target), 0x80000000, 3, None, 3, 0x80, None)
            self.assertNotEqual(handle, ctypes.c_void_p(-1).value, ctypes.get_last_error())
            original_sleep = time.sleep

            def release_reader(_delay):
                nonlocal handle
                self.assertEqual(target.read_text(), "previous")
                if handle is not None:
                    self.assertTrue(close_handle(handle))
                    handle = None
                original_sleep(_delay)

            try:
                with patch("core.atomic_files.time.sleep", side_effect=release_reader) as sleep:
                    atomic_write_text(target, "next")
                self.assertGreaterEqual(sleep.call_count, 1)
                self.assertEqual(target.read_text(), "next")
                self.assertFalse(list(target.parent.glob(f".{target.name}.*.tmp")))
            finally:
                if handle is not None:
                    close_handle(handle)

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
