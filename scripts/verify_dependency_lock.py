from __future__ import annotations

import re
import sys
from pathlib import Path

from packaging.requirements import Requirement

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 compatibility.
    import tomli as tomllib


ROOT = Path(__file__).resolve().parent.parent
LOCK_PATH = ROOT / "requirements.lock"
LIVE_LOCK_PATH = ROOT / "requirements-live.lock"
TEST_LOCK_PATH = ROOT / "requirements-test.lock"
BUILD_LOCK_PATH = ROOT / "requirements-build.lock"
SECURITY_LOCK_PATH = ROOT / "requirements-security.lock"
BOOTSTRAP_LOCK_PATH = ROOT / "requirements-bootstrap.lock"
REQUIREMENTS_PATH = ROOT / "requirements.txt"
LIVE_REQUIREMENTS_PATH = ROOT / "requirements-live.txt"
TEST_REQUIREMENTS_PATH = ROOT / "requirements-test.txt"
BUILD_REQUIREMENTS_PATH = ROOT / "requirements-build.txt"
SECURITY_REQUIREMENTS_PATH = ROOT / "requirements-security.txt"
BOOTSTRAP_REQUIREMENTS_PATH = ROOT / "requirements-bootstrap.txt"
PROJECT_PATH = ROOT / "pyproject.toml"
LOCKED_REQUIREMENT_RE = re.compile(
    r"^([A-Za-z0-9_.-]+)==([^\s;\\]+)(?:\s*;\s*[^\\]+)?\s*\\?$"
)
HASH_RE = re.compile(r"^\s*--hash=sha256:[0-9a-f]{64}$")


def canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def lock_issues(lock_text: str, project_dependencies: list[str]) -> list[str]:
    locked: dict[str, int] = {}
    locked_versions: dict[str, list[str]] = {}
    active_name: str | None = None
    active_has_hash = False
    issues: list[str] = []

    def finish_active() -> None:
        nonlocal active_name, active_has_hash
        if active_name and not active_has_hash:
            issues.append(f"{active_name} is not hash protected")
        active_name = None
        active_has_hash = False

    for line in lock_text.splitlines():
        match = LOCKED_REQUIREMENT_RE.match(line)
        if match:
            finish_active()
            active_name = canonical_name(match.group(1))
            locked[active_name] = locked.get(active_name, 0) + 1
            locked_versions.setdefault(active_name, []).append(match.group(2))
            continue
        if active_name and HASH_RE.match(line):
            active_has_hash = True
            continue
        if active_name and line and not line.startswith((" ", "#")):
            finish_active()
    finish_active()

    for dependency in project_dependencies:
        requirement = Requirement(dependency)
        requirement_name = canonical_name(requirement.name)
        versions = locked_versions.get(requirement_name, [])
        if not versions:
            issues.append(f"direct dependency {requirement.name} is missing from requirements.lock")
            continue
        for version in versions:
            if requirement.specifier and not requirement.specifier.contains(version, prereleases=True):
                issues.append(
                    f"locked {requirement.name}=={version} does not satisfy source requirement {dependency}"
                )
    duplicate_names = sorted(name for name, count in locked.items() if count > 1 and name != "tomli")
    if duplicate_names:
        issues.append("duplicate locked packages: " + ", ".join(duplicate_names))
    return issues


def _requirement_lines(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "-r"))
    ]


def main() -> int:
    project = tomllib.loads(PROJECT_PATH.read_text(encoding="utf-8"))
    optional = project.get("project", {}).get("optional-dependencies", {})
    runtime_dependencies = list(project.get("project", {}).get("dependencies", []))
    runtime_source_requirements = _requirement_lines(REQUIREMENTS_PATH)
    live_source_requirements = _requirement_lines(LIVE_REQUIREMENTS_PATH)
    test_source_requirements = _requirement_lines(TEST_REQUIREMENTS_PATH)
    runtime_constraints = [*runtime_dependencies, *runtime_source_requirements]
    live_dependencies = [*runtime_dependencies, *optional.get("live", [])]
    checks = (
        (LOCK_PATH, runtime_constraints),
        (
            LIVE_LOCK_PATH,
            [*live_dependencies, *runtime_source_requirements, *live_source_requirements],
        ),
        (
            TEST_LOCK_PATH,
            [
                *live_dependencies,
                *optional.get("test", []),
                *runtime_source_requirements,
                *live_source_requirements,
                *test_source_requirements,
            ],
        ),
        (BUILD_LOCK_PATH, _requirement_lines(BUILD_REQUIREMENTS_PATH)),
        (SECURITY_LOCK_PATH, _requirement_lines(SECURITY_REQUIREMENTS_PATH)),
        (BOOTSTRAP_LOCK_PATH, _requirement_lines(BOOTSTRAP_REQUIREMENTS_PATH)),
    )
    issues: list[str] = []
    for lock_path, dependencies in checks:
        if not lock_path.exists():
            issues.append(f"{lock_path.name} is missing")
            continue
        issues.extend(
            f"{lock_path.name}: {issue}"
            for issue in lock_issues(lock_path.read_text(encoding="utf-8"), dependencies)
        )
    if issues:
        print("Dependency lock validation failed:", file=sys.stderr)
        for issue in issues:
            print(f"- {issue}", file=sys.stderr)
        return 1
    print("[ok] dependency lock")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
