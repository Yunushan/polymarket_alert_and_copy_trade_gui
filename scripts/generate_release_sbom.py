from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 compatibility.
    import tomli as tomllib


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.atomic_files import atomic_write_text

try:
    from scripts.release_version import normalize_release_version
except ModuleNotFoundError:  # Direct execution adds scripts/, rather than the repository root, to sys.path.
    from release_version import normalize_release_version

LOCK_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;]+)")


def spdx_id(name: str, version: str) -> str:
    digest = hashlib.sha256(f"{name}@{version}".encode("utf-8")).hexdigest()[:16]
    return f"SPDXRef-Package-{digest}"


def created_at() -> str:
    source_date_epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if source_date_epoch and source_date_epoch.isdigit():
        return datetime.fromtimestamp(int(source_date_epoch), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def locked_python_packages(lock_path: Path) -> list[tuple[str, str]]:
    packages: dict[str, str] = {}
    for line in lock_path.read_text(encoding="utf-8").splitlines():
        match = LOCK_RE.match(line)
        if match:
            packages[match.group(1).lower()] = match.group(2)
    return sorted(packages.items())


def locked_node_packages(lock_path: Path) -> list[tuple[str, str]]:
    data = json.loads(lock_path.read_text(encoding="utf-8"))
    packages: dict[str, str] = {}
    for path, metadata in dict(data.get("packages") or {}).items():
        if not path.startswith("node_modules/") or not isinstance(metadata, dict):
            continue
        name = path.removeprefix("node_modules/")
        version = str(metadata.get("version") or "").strip()
        if name and version:
            packages[name] = version
    return sorted(packages.items())


def package_entry(name: str, version: str, *, license_expression: str = "NOASSERTION") -> dict[str, Any]:
    return {
        "SPDXID": spdx_id(name, version),
        "name": name,
        "versionInfo": version,
        "downloadLocation": "NOASSERTION",
        "filesAnalyzed": False,
        "licenseConcluded": "NOASSERTION",
        "licenseDeclared": license_expression,
        "copyrightText": "NOASSERTION",
    }


def build_sbom(version: str) -> dict[str, Any]:
    try:
        version = normalize_release_version(str(version))
    except ValueError as exc:
        raise SystemExit(f"Unsupported SBOM release version {version!r}: {exc}") from exc
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    project_name = str(project["name"])
    project_version = str(project["version"])
    try:
        canonical_project_version = normalize_release_version(project_version)
    except ValueError as exc:
        raise SystemExit(f"pyproject.toml has an unsupported release version {project_version!r}: {exc}") from exc
    if project_version != canonical_project_version:
        raise SystemExit(
            f"pyproject.toml project.version must use canonical form {canonical_project_version!r}; "
            f"got {project_version!r}."
        )
    if project_version != version:
        raise SystemExit(f"Requested SBOM version {version} does not match pyproject.toml ({project['version']}).")
    root_package = package_entry(project_name, version, license_expression="0BSD")
    # The Windows release installs requirements-live.lock before PyInstaller,
    # so that lock—not the lean server lock—is the shipped Python superset.
    # Deduplicate an identical Python/Node name+version coordinate to avoid
    # emitting duplicate SPDX package IDs and relationships.
    dependency_coordinates = sorted(
        set(locked_python_packages(ROOT / "requirements-live.lock"))
        | set(locked_node_packages(ROOT / "frontend" / "package-lock.json"))
    )
    dependencies = [
        package_entry(name, package_version)
        for name, package_version in dependency_coordinates
    ]
    relationships = [
        {
            "spdxElementId": "SPDXRef-DOCUMENT",
            "relationshipType": "DESCRIBES",
            "relatedSpdxElement": root_package["SPDXID"],
        }
    ]
    relationships.extend(
        {
            "spdxElementId": root_package["SPDXID"],
            "relationshipType": "DEPENDS_ON",
            "relatedSpdxElement": dependency["SPDXID"],
        }
        for dependency in dependencies
    )
    namespace_hash = hashlib.sha256(f"{project_name}:{version}".encode("utf-8")).hexdigest()
    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"{project_name}-{version}-sbom",
        "documentNamespace": f"https://github.com/Yunushan/market-sentinel/sbom/{namespace_hash}",
        "creationInfo": {"created": created_at(), "creators": ["Tool: market-sentinel generate_release_sbom.py"]},
        "packages": [root_package, *dependencies],
        "relationships": relationships,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate an SPDX JSON SBOM from the committed dependency locks.")
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = build_sbom(str(args.version))
    atomic_write_text(args.output, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(f"[ok] SBOM ({args.output})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
