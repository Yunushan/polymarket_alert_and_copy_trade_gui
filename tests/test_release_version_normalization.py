from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.release_version import (
    normalize_release_tag,
    normalize_release_version,
    parse_release_version,
    read_project_version,
    validate_project_version,
)


class ReleaseVersionNormalizationTests(unittest.TestCase):
    def test_stable_and_prerelease_tags_normalize_to_canonical_versions(self) -> None:
        cases = {
            "v0.0.0": "0.0.0",
            "v1.2.3": "1.2.3",
            "v1.2.3-alpha.1": "1.2.3a1",
            "v1.2.3-beta.20": "1.2.3b20",
            "v1.2.3-rc.59": "1.2.3rc59",
            "v1.2.3a1": "1.2.3a1",
            "v1.2.3b2": "1.2.3b2",
            "v1.2.3rc3": "1.2.3rc3",
        }

        for tag, expected in cases.items():
            with self.subTest(tag=tag):
                self.assertEqual(normalize_release_tag(tag), expected)

    def test_parser_reports_prerelease_from_validated_structure(self) -> None:
        self.assertFalse(parse_release_version("v1.2.3", require_tag_prefix=True).is_prerelease)
        self.assertTrue(parse_release_version("v1.2.3-rc.1", require_tag_prefix=True).is_prerelease)
        self.assertEqual(normalize_release_version("1.2.3-rc.1"), "1.2.3rc1")

    def test_invalid_or_ambiguous_versions_are_rejected(self) -> None:
        invalid = (
            "1.2.3",
            "v01.2.3",
            "v1.02.3",
            "v1.2.03",
            "v1.2.3-rc.01",
            "v1.2.3-rc",
            "v1.2.3-preview.1",
            "v1.2.3-alpha1",
            "v1.2.3-rc-1",
            "v1.2.3.post1",
            "v1.2.3+build.1",
            " v1.2.3",
        )

        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    normalize_release_tag(value)

    def test_project_version_validation_requires_canonical_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pyproject = Path(directory) / "pyproject.toml"
            pyproject.write_text(
                '[build-system]\nrequires = []\n\n[project]\nname = "example"\nversion = "1.2.3rc1"\n',
                encoding="utf-8",
            )

            self.assertEqual(read_project_version(pyproject), "1.2.3rc1")
            self.assertEqual(validate_project_version("v1.2.3-rc.1", pyproject), "1.2.3rc1")
            with self.assertRaisesRegex(ValueError, "resolves to 1.2.4"):
                validate_project_version("v1.2.4", pyproject)

            pyproject.write_text('[project]\nversion = "1.2.3-rc.1"\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must use canonical form"):
                validate_project_version("v1.2.3-rc.1", pyproject)

    def test_project_version_reader_rejects_missing_or_duplicate_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pyproject = Path(directory) / "pyproject.toml"
            for text in (
                '[project]\nname = "example"\n',
                '[project]\nversion = "1.2.3"\nversion = "1.2.4"\n',
            ):
                with self.subTest(text=text):
                    pyproject.write_text(text, encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "exactly one"):
                        read_project_version(pyproject)


if __name__ == "__main__":
    unittest.main()
