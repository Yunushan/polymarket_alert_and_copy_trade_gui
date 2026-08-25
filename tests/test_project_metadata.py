from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 compatibility.
    import tomli as tomllib


ROOT = Path(__file__).resolve().parent.parent
PROJECT_NAME = "market-sentinel"
APP_TITLE = "MarketSentinel"


class ProjectMetadataTests(unittest.TestCase):
    def test_repository_hygiene_policy(self) -> None:
        attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")

        self.assertIn("* text=auto eol=lf", attributes)
        self.assertIn("*.bat text eol=crlf", attributes)
        self.assertIn("*.cmd text eol=crlf", attributes)

        if not (ROOT / ".git").exists():
            self.skipTest("Git metadata is unavailable in this source tree")

        try:
            tracked_result = subprocess.run(
                ["git", "ls-files", "-z"],
                cwd=ROOT,
                check=True,
                capture_output=True,
            )
            eol_result = subprocess.run(
                ["git", "ls-files", "--eol"],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
        except FileNotFoundError:
            self.skipTest("Git executable is unavailable")

        tracked = tracked_result.stdout.decode("utf-8", errors="surrogateescape").split("\0")
        generated = sorted(
            path
            for path in tracked
            if "/__pycache__/" in f"/{path}" or path.endswith((".pyc", ".pyo", ".pyd"))
        )
        non_normalized = sorted(
            line for line in eol_result.stdout.splitlines() if line.startswith(("i/crlf", "i/mixed"))
        )

        self.assertEqual(generated, [], f"generated Python artifacts are tracked: {generated}")
        self.assertEqual(non_normalized, [], f"non-normalized Git blobs are tracked: {non_normalized}")

    def test_project_name_uses_dashes_not_underscores(self) -> None:
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        frontend_package = (ROOT / "frontend" / "package.json").read_text(encoding="utf-8")
        name = data["project"]["name"]

        self.assertEqual(data["build-system"]["requires"], ["setuptools>=77"])
        self.assertEqual(name, PROJECT_NAME)
        self.assertNotIn("_", name)
        self.assertEqual(data["project"]["requires-python"], ">=3.10")
        self.assertEqual(data["project"]["license"], "0BSD")
        self.assertEqual(data["project"]["license-files"], ["LICENSE"])
        self.assertIn("Programming Language :: Python :: 3.15", data["project"]["classifiers"])
        self.assertIn("Programming Language :: Python :: 3.16", data["project"]["classifiers"])
        self.assertIn('"name": "market-sentinel-react-gui"', frontend_package)

    def test_license_file_uses_bsd_zero_clause_text(self) -> None:
        text = (ROOT / "LICENSE").read_text(encoding="utf-8")

        self.assertTrue(text.startswith("BSD Zero Clause License\n"))
        self.assertIn("Permission to use, copy, modify, and/or distribute this software", text)
        self.assertIn('THE SOFTWARE IS PROVIDED "AS IS"', text)

    def test_user_facing_project_title_uses_marketsentinel_brand(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        app = (ROOT / "app.py").read_text(encoding="utf-8")

        self.assertIn(f"# {APP_TITLE}", readme)
        self.assertIn('assets/marketsentinel.svg', readme)
        self.assertTrue((ROOT / "assets" / "marketsentinel.svg").exists())
        self.assertTrue((ROOT / "assets" / "marketsentinel.ico").exists())
        self.assertTrue((ROOT / "assets" / "icons" / "marketsentinel-32.png").exists())
        self.assertTrue((ROOT / "assets" / "icons" / "marketsentinel-24.png").exists())
        self.assertTrue((ROOT / "marketsentinel.png").exists())
        self.assertTrue((ROOT / "frontend" / "public" / "marketsentinel.png").exists())
        self.assertIn(f'APP_TITLE = "{APP_TITLE}"', app)
        self.assertIn(f'APP_ID = "{PROJECT_NAME}"', app)
        self.assertIn('APP_USER_AGENT = f"{APP_ID}/1.0"', app)
        self.assertIn('headers={"User-Agent": APP_USER_AGENT}', app)

    def test_old_polymarket_project_branding_is_not_used(self) -> None:
        files = [
            ROOT / "README.md",
            ROOT / "app.py",
            ROOT / "pyproject.toml",
            ROOT / "GOAL.md",
        ]
        forbidden = (
            "prediction-market-alert-and-copy-trade-gui",
            "polymarket-alert-and-copy-trade-gui",
            "polymarket-sentinel-gui",
            "Polymarket Sentinel GUI",
            "PolymarketSentinelGUI",
        )

        for path in files:
            text = path.read_text(encoding="utf-8")
            for value in forbidden:
                with self.subTest(path=path.name, value=value):
                    self.assertNotIn(value, text)

    def test_source_distribution_manifest_keeps_verification_inputs(self) -> None:
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")

        for fragment in (
            "recursive-include .github",
            "recursive-include assets",
            "recursive-include data",
            "recursive-include docs",
            "recursive-include frontend",
            "recursive-include scripts",
            "recursive-include tests",
            "prune frontend/dist",
            "prune frontend/node_modules",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, manifest)


if __name__ == "__main__":
    unittest.main()
