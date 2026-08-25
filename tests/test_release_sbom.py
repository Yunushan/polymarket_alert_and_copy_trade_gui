from __future__ import annotations

import unittest
from unittest.mock import patch
from pathlib import Path

from scripts.generate_release_sbom import build_sbom, created_at, locked_python_packages


ROOT = Path(__file__).resolve().parent.parent


class ReleaseSbomTests(unittest.TestCase):
    def test_created_at_uses_utc_timestamp_compatibly(self) -> None:
        with patch.dict("os.environ", {"SOURCE_DATE_EPOCH": "0"}, clear=False):
            self.assertEqual(created_at(), "1970-01-01T00:00:00Z")

    def test_sbom_contains_project_and_locked_dependencies(self) -> None:
        version = next(
            line.split('"', 2)[1]
            for line in (ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines()
            if line.startswith("version = ")
        )
        payload = build_sbom(version)
        self.assertEqual(payload["spdxVersion"], "SPDX-2.3")
        self.assertEqual(payload["packages"][0]["name"], "market-sentinel")
        names = {package["name"] for package in payload["packages"]}
        self.assertIn("requests", names)
        self.assertIn("py-clob-client", names)
        self.assertIn("opinion-clob-sdk", names)
        self.assertIn("react", names)
        self.assertGreater(len(payload["relationships"]), 2)

        package_coordinates = {
            (package["name"], package["versionInfo"])
            for package in payload["packages"]
            if package["name"] != "market-sentinel"
        }
        self.assertTrue(
            set(locked_python_packages(ROOT / "requirements-live.lock"))
            <= package_coordinates
        )
        package_ids = [package["SPDXID"] for package in payload["packages"]]
        self.assertEqual(len(package_ids), len(set(package_ids)))


if __name__ == "__main__":
    unittest.main()
