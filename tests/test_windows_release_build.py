from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from scripts.build_windows_release import (
    APP_NAME,
    build_pyinstaller,
    extract_frontend_archive,
    main,
    make_portable_zip,
    msi_product_version,
    validate_staged_package,
    windows_config_bootstrap,
)


class WindowsReleaseBuildTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows batch launcher integration")
    def test_config_bootstrap_uses_portable_path_when_writable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "portable app"
            data = package / "data"
            data.mkdir(parents=True)
            config = data / "config.json"
            config.write_bytes(b'{"existing": "portable"}\n')

            selected = self._run_config_bootstrap(package, root / "roaming data")

            self.assertEqual(selected, str(config))
            self.assertEqual(config.read_bytes(), b'{"existing": "portable"}\n')
            self.assertFalse((data / ".write-test").exists())
            self.assertFalse((root / "roaming data").exists())

    @unittest.skipUnless(os.name == "nt", "Windows batch launcher integration")
    def test_config_bootstrap_seeds_appdata_when_portable_probe_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "installed app"
            data = package / "data"
            data.mkdir(parents=True)
            example = b'{"example": "appdata defaults"}\n'
            (data / "config.example.json").write_bytes(example)
            # A directory makes the probe fail without changing filesystem ACLs.
            (data / ".write-test").mkdir()
            roaming = root / "roaming data"
            config = roaming / APP_NAME / "data" / "config.json"

            selected = self._run_config_bootstrap(package, roaming)

            self.assertEqual(selected, str(config))
            self.assertTrue(config.is_file(), "The fallback must seed the selected AppData configuration.")
            self.assertEqual(config.read_bytes(), example)
            self.assertFalse((data / "config.json").exists())

    @unittest.skipUnless(os.name == "nt", "Windows batch launcher integration")
    def test_config_bootstrap_preserves_existing_appdata_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "installed app"
            data = package / "data"
            data.mkdir(parents=True)
            (data / "config.example.json").write_bytes(b'{"example": "new defaults"}\n')
            (data / ".write-test").mkdir()
            roaming = root / "roaming data"
            config = roaming / APP_NAME / "data" / "config.json"
            config.parent.mkdir(parents=True)
            saved = b'{"existing": "user settings and journals"}\n'
            config.write_bytes(saved)

            selected = self._run_config_bootstrap(package, roaming)

            self.assertEqual(selected, str(config))
            self.assertEqual(config.read_bytes(), saved)
            self.assertFalse((data / "config.json").exists(), "Do not copy defaults to the rejected portable path.")

    def _run_config_bootstrap(self, package: Path, roaming: Path) -> str:
        launcher = package / "bootstrap probe.cmd"
        launcher.write_text(
            "@echo off\nsetlocal\n" + windows_config_bootstrap() + "\necho CONFIG=%PREDICTION_MARKET_CONFIG_PATH%\n",
            encoding="utf-8",
        )
        environment = dict(os.environ)
        environment["APPDATA"] = str(roaming)
        result = subprocess.run(
            [environment.get("COMSPEC", "cmd.exe"), "/d", "/v:off", "/c", str(launcher)],
            cwd=package.parent,
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        paths = [line.removeprefix("CONFIG=") for line in result.stdout.splitlines() if line.startswith("CONFIG=")]
        self.assertEqual(len(paths), 1, result.stdout + result.stderr)
        return paths[0]

    def test_msi_product_versions_are_monotonic_across_release_stages(self) -> None:
        versions = (
            "1.2.2",
            "1.2.3a1",
            "1.2.3a20",
            "1.2.3b1",
            "1.2.3b20",
            "1.2.3rc1",
            "1.2.3rc59",
            "1.2.3",
            "1.2.4a1",
        )
        mapped = [msi_product_version(version) for version in versions]

        self.assertEqual(
            mapped,
            [
                "1.2.299",
                "1.2.300",
                "1.2.319",
                "1.2.320",
                "1.2.339",
                "1.2.340",
                "1.2.398",
                "1.2.399",
                "1.2.400",
            ],
        )
        self.assertEqual(mapped, sorted(mapped, key=lambda value: tuple(map(int, value.split(".")))))

    def test_msi_product_version_accepts_human_prerelease_spelling(self) -> None:
        self.assertEqual(msi_product_version("1.2.3-alpha.1"), "1.2.300")
        self.assertEqual(msi_product_version("1.2.3-beta.1"), "1.2.320")
        self.assertEqual(msi_product_version("1.2.3-rc.1"), "1.2.340")

    def test_msi_product_version_enforces_windows_and_stage_bounds(self) -> None:
        invalid = {
            "256.1.1": "major and minor",
            "1.256.1": "major and minor",
            "1.2.3a21": "between 1 and 20",
            "1.2.3b21": "between 1 and 20",
            "1.2.3rc60": "between 1 and 59",
            "1.2.655a1": "complete prerelease-to-final build bucket",
            "1.2.655rc1": "complete prerelease-to-final build bucket",
            "1.2.3-dev.1": "Unsupported MSI release version",
        }

        for version, error in invalid.items():
            with self.subTest(version=version):
                with self.assertRaisesRegex(SystemExit, error):
                    msi_product_version(version)

    def test_pyinstaller_collects_both_optional_live_sdk_packages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work_dir = root / "work"
            package_dir = root / "package"

            def fake_run(command: list[str], **_kwargs: object) -> None:
                built_app = work_dir / "pyinstaller-dist" / APP_NAME
                built_app.mkdir(parents=True)
                (built_app / f"{APP_NAME}.exe").write_bytes(b"frozen-app")

            with patch("scripts.build_windows_release.run", side_effect=fake_run) as runner:
                build_pyinstaller(work_dir, package_dir)

            command = runner.call_args.args[0]
            self.assertIn(["--collect-all", "py_clob_client_v2"], [command[index : index + 2] for index in range(len(command) - 1)])
            self.assertIn(["--collect-all", "opinion_clob_sdk"], [command[index : index + 2] for index in range(len(command) - 1)])
            self.assertEqual((package_dir / f"{APP_NAME}.exe").read_bytes(), b"frozen-app")

    def test_package_only_validation_rejects_missing_or_stale_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package_dir = Path(directory) / "package"
            package_dir.mkdir()
            with self.assertRaisesRegex(SystemExit, "executable is missing"):
                validate_staged_package(package_dir, "1.2.3")

            (package_dir / f"{APP_NAME}.exe").write_bytes(b"signed-app")
            (package_dir / "VERSION.txt").write_text(f"{APP_NAME} 1.2.2\n", encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "does not match"):
                validate_staged_package(package_dir, "1.2.3")

    def test_portable_zip_preserves_the_signed_staged_executable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_dir = root / "package"
            output_dir = root / "output"
            package_dir.mkdir()
            output_dir.mkdir()
            signed_bytes = b"signed-authenticode-executable"
            (package_dir / f"{APP_NAME}.exe").write_bytes(signed_bytes)

            archive_path = make_portable_zip(package_dir, output_dir, "v1.2.3")

            with ZipFile(archive_path) as archive:
                self.assertEqual(
                    archive.read(f"{APP_NAME}-v1.2.3-win-x64/{APP_NAME}.exe"),
                    signed_bytes,
                )

    def test_package_only_main_does_not_clean_or_rebuild_signed_stage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work_dir = root / "work"
            output_dir = root / "output"
            package_dir = work_dir / f"{APP_NAME}-v1.2.3-win-x64"
            package_dir.mkdir(parents=True)
            executable = package_dir / f"{APP_NAME}.exe"
            executable.write_bytes(b"signed-authenticode-executable")
            (package_dir / "VERSION.txt").write_text(f"{APP_NAME} 1.2.3\n", encoding="utf-8")
            args = SimpleNamespace(
                version="1.2.3",
                tag="v1.2.3",
                frontend_zip=None,
                output_dir=output_dir,
                work_dir=work_dir,
                skip_msi=False,
                prepare_only=False,
                package_only=True,
            )

            with (
                patch("scripts.build_windows_release.os.name", "nt"),
                patch("scripts.build_windows_release.parse_args", return_value=args),
                patch("scripts.build_windows_release.clean_dir") as cleaner,
                patch("scripts.build_windows_release.build_pyinstaller") as pyinstaller,
                patch(
                    "scripts.build_windows_release.make_portable_zip",
                    return_value=output_dir / "portable.zip",
                ) as portable,
                patch("scripts.build_windows_release.build_msi", return_value=output_dir / "installer.msi") as msi,
            ):
                self.assertEqual(main(), 0)

            cleaner.assert_not_called()
            pyinstaller.assert_not_called()
            portable.assert_called_once_with(package_dir.resolve(), output_dir.resolve(), "v1.2.3")
            msi.assert_called_once_with(package_dir.resolve(), output_dir.resolve(), work_dir.resolve(), "v1.2.3", "1.2.3")
            self.assertEqual(executable.read_bytes(), b"signed-authenticode-executable")

    def test_extract_frontend_archive_extracts_normal_release_assets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "frontend.zip"
            destination = root / "dist"
            destination.mkdir()
            with ZipFile(archive_path, "w", ZIP_DEFLATED) as archive:
                archive.writestr("index.html", "<html></html>")
                archive.writestr("assets/app.js", "console.log('ok')")

            extract_frontend_archive(archive_path, destination)

            self.assertEqual((destination / "index.html").read_text(encoding="utf-8"), "<html></html>")
            self.assertEqual((destination / "assets" / "app.js").read_text(encoding="utf-8"), "console.log('ok')")

    def test_extract_frontend_archive_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "unsafe.zip"
            destination = root / "dist"
            destination.mkdir()
            with ZipFile(archive_path, "w", ZIP_DEFLATED) as archive:
                archive.writestr("../outside.txt", "unsafe")

            with self.assertRaisesRegex(ValueError, "unsafe member path"):
                extract_frontend_archive(archive_path, destination)

            self.assertFalse((root / "outside.txt").exists())

    def test_extract_frontend_archive_rejects_symbolic_links(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "symlink.zip"
            destination = root / "dist"
            destination.mkdir()
            link = ZipInfo("assets/link")
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            with ZipFile(archive_path, "w", ZIP_DEFLATED) as archive:
                archive.writestr(link, "../../outside.txt")

            with self.assertRaisesRegex(ValueError, "symbolic-link"):
                extract_frontend_archive(archive_path, destination)
