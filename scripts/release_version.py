#!/usr/bin/env python3
"""Parse and normalize the release versions supported by MarketSentinel.

The release pipeline intentionally accepts a small, unambiguous subset of
semantic/PEP 440 versions.  Keeping this parser dependency-free lets the
metadata job validate a tag before it installs any project dependencies.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path


_BASE = r"(?P<major>0|[1-9][0-9]*)\.(?P<minor>0|[1-9][0-9]*)\.(?P<patch>0|[1-9][0-9]*)"
_VERSION_PATTERN = re.compile(
    rf"^(?P<prefix>v)?{_BASE}(?:"
    r"-(?P<long_stage>alpha|beta|rc)\.(?P<long_serial>[1-9][0-9]*)"
    r"|(?P<short_stage>a|b|rc)(?P<short_serial>[1-9][0-9]*)"
    r")?$"
)
_PROJECT_SECTION_PATTERN = re.compile(r"^\s*\[project\]\s*(?:#.*)?$")
_SECTION_PATTERN = re.compile(r"^\s*\[\[?[^]]+\]\]?\s*(?:#.*)?$")
_PROJECT_VERSION_PATTERN = re.compile(
    r"^\s*version\s*=\s*(?P<quote>['\"])(?P<version>[^'\"]+)(?P=quote)\s*(?:#.*)?$"
)


@dataclass(frozen=True)
class ReleaseVersion:
    """A validated release version in canonical PEP 440 form."""

    major: int
    minor: int
    patch: int
    stage: str | None = None
    serial: int | None = None

    @property
    def normalized(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        if self.stage is None:
            return base
        return f"{base}{self.stage}{self.serial}"

    @property
    def is_prerelease(self) -> bool:
        return self.stage is not None


def parse_release_version(value: str, *, require_tag_prefix: bool = False) -> ReleaseVersion:
    """Parse one supported stable, alpha, beta, or release-candidate version.

    Accepted prerelease spellings are the human-oriented tag forms
    ``v1.2.3-alpha.1``, ``v1.2.3-beta.1``, and ``v1.2.3-rc.1`` plus their
    canonical PEP 440 forms ``1.2.3a1``, ``1.2.3b1``, and ``1.2.3rc1`` (with
    an optional ``v`` when parsing a tag).
    """

    match = _VERSION_PATTERN.fullmatch(value)
    if match is None or (require_tag_prefix and match.group("prefix") != "v"):
        expected = (
            "v1.2.3, v1.2.3-alpha.1, v1.2.3-beta.1, or v1.2.3-rc.1"
            if require_tag_prefix
            else "1.2.3, 1.2.3a1, 1.2.3b1, or 1.2.3rc1"
        )
        raise ValueError(f"unsupported release version {value!r}; expected {expected}")

    long_stage = match.group("long_stage")
    short_stage = match.group("short_stage")
    stage_alias = long_stage or short_stage
    stage = {"alpha": "a", "a": "a", "beta": "b", "b": "b", "rc": "rc"}.get(stage_alias)
    serial_text = match.group("long_serial") or match.group("short_serial")
    serial = int(serial_text) if serial_text is not None else None

    return ReleaseVersion(
        major=int(match.group("major")),
        minor=int(match.group("minor")),
        patch=int(match.group("patch")),
        stage=stage,
        serial=serial,
    )


def normalize_release_tag(tag: str) -> str:
    """Return the canonical version represented by a required ``v`` tag."""

    return normalize_release_version(tag, require_tag_prefix=True)


def normalize_release_version(value: str, *, require_tag_prefix: bool = False) -> str:
    """Return the canonical form of one supported release version."""

    return parse_release_version(value, require_tag_prefix=require_tag_prefix).normalized


def read_project_version(pyproject_path: Path) -> str:
    """Read the simple ``project.version`` string without third-party TOML code."""

    in_project = False
    versions: list[str] = []
    try:
        lines = pyproject_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"could not read {pyproject_path}: {exc}") from exc

    for line in lines:
        if _PROJECT_SECTION_PATTERN.fullmatch(line):
            in_project = True
            continue
        if in_project and _SECTION_PATTERN.fullmatch(line):
            in_project = False
            continue
        if in_project and (match := _PROJECT_VERSION_PATTERN.fullmatch(line)):
            versions.append(match.group("version"))

    if len(versions) != 1:
        raise ValueError(
            f"expected exactly one simple project.version string in {pyproject_path}; found {len(versions)}"
        )
    return versions[0]


def validate_project_version(tag: str, pyproject_path: Path) -> str:
    """Require a canonical project version to match the normalized release tag."""

    expected = normalize_release_tag(tag)
    actual = read_project_version(pyproject_path)
    try:
        parsed_actual = parse_release_version(actual)
    except ValueError as exc:
        raise ValueError(f"pyproject.toml project.version is invalid: {exc}") from exc
    if actual != parsed_actual.normalized:
        raise ValueError(
            "pyproject.toml project.version must use canonical form "
            f"{parsed_actual.normalized!r}; got {actual!r}"
        )
    if actual != expected:
        raise ValueError(
            f"pyproject.toml project.version is {actual}, but release tag {tag} resolves to {expected}. "
            "Update [project].version before publishing this release."
        )
    return actual


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    normalize = subparsers.add_parser("normalize-tag", help="Print a tag's canonical package version.")
    normalize.add_argument("tag")

    prerelease = subparsers.add_parser(
        "is-prerelease",
        help="Print true when a tag is an alpha, beta, or release candidate.",
    )
    prerelease.add_argument("tag")

    validate = subparsers.add_parser(
        "validate-project",
        help="Validate pyproject.toml's version against a release tag.",
    )
    validate.add_argument("--tag", required=True)
    validate.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    try:
        if args.command == "normalize-tag":
            print(normalize_release_tag(args.tag))
        elif args.command == "is-prerelease":
            parsed = parse_release_version(args.tag, require_tag_prefix=True)
            print("true" if parsed.is_prerelease else "false")
        else:
            print(validate_project_version(args.tag, args.pyproject))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
