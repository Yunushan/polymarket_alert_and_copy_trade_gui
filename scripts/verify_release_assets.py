from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

try:
    from scripts.release_version import normalize_release_tag, normalize_release_version
except ModuleNotFoundError:  # Direct execution adds scripts/, rather than the repository root, to sys.path.
    from release_version import normalize_release_tag, normalize_release_version


CHECKSUM_LINE = re.compile(r"^([0-9a-f]{64})  ([^/\\]+)$")
CONTROL_ASSETS = frozenset({"SHA256SUMS.txt", "RELEASE_NOTES.md"})


def _release_coordinates(version: str, tag: str) -> tuple[str, str]:
    try:
        canonical_version = normalize_release_version(str(version))
        tag_version = normalize_release_tag(str(tag))
    except ValueError as exc:
        raise SystemExit(f"Invalid release coordinates: {exc}") from exc
    if canonical_version != tag_version:
        raise SystemExit(
            f"Release version {version!r} resolves to {canonical_version}, "
            f"but tag {tag!r} resolves to {tag_version}."
        )
    return canonical_version, str(tag)


def expected_assets(version: str, tag: str) -> set[str]:
    version, tag = _release_coordinates(version, tag)
    return {
        f"market_sentinel-{version}-py3-none-any.whl",
        f"market_sentinel-{version}.tar.gz",
        f"market-sentinel-{tag}-frontend-dist.zip",
        f"market-sentinel-{tag}-win-x64.zip",
        f"market-sentinel-{tag}-win-x64.msi",
        f"market-sentinel-{version}-sbom.spdx.json",
    }


def publishable_assets(version: str, tag: str) -> set[str]:
    """Return the exact file-name inventory published on the GitHub release."""

    return expected_assets(version, tag) | set(CONTROL_ASSETS)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_checksums(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise SystemExit(f"Missing checksum manifest: {path.name}")
    checksums: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = CHECKSUM_LINE.fullmatch(line)
        if not match:
            raise SystemExit(f"Malformed SHA256SUMS entry: {line!r}")
        digest, name = match.groups()
        if name in checksums:
            raise SystemExit(f"Duplicate SHA256SUMS entry: {name}")
        checksums[name] = digest
    return checksums


def verify_sbom(path: Path, version: str) -> None:
    try:
        version = normalize_release_version(str(version))
    except ValueError as exc:
        raise SystemExit(f"Invalid SBOM release version {version!r}: {exc}") from exc
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"Invalid SPDX SBOM {path.name}: {error}") from error
    if payload.get("spdxVersion") != "SPDX-2.3":
        raise SystemExit(f"SBOM {path.name} does not declare SPDX-2.3.")
    if payload.get("name") != f"market-sentinel-{version}-sbom":
        raise SystemExit(f"SBOM {path.name} does not match release version {version}.")
    packages = payload.get("packages")
    if not isinstance(packages, list) or not any(
        package.get("name") == "market-sentinel" and package.get("versionInfo") == version
        for package in packages
        if isinstance(package, dict)
    ):
        raise SystemExit(f"SBOM {path.name} is missing the market-sentinel {version} package entry.")


def _remote_asset_inventory(
    payload: Any,
    tag: str,
    assets_payload: Any | None = None,
) -> dict[str, dict[str, Any]]:
    """Validate an exact-tag GitHub release response and index its assets by name."""

    if not isinstance(payload, dict):
        raise SystemExit("Remote release metadata must be a JSON object.")
    if payload.get("tag_name") != tag:
        raise SystemExit(
            f"Remote release tag mismatch: expected {tag!r}, got {payload.get('tag_name')!r}."
        )
    release_id = payload.get("id")
    if isinstance(release_id, bool) or not isinstance(release_id, int) or release_id <= 0:
        raise SystemExit("Remote release metadata has an invalid release id.")
    assets = payload.get("assets") if assets_payload is None else assets_payload
    if not isinstance(assets, list):
        raise SystemExit("Remote release metadata does not contain an asset list.")

    inventory: dict[str, dict[str, Any]] = {}
    asset_ids: set[int] = set()
    for position, asset in enumerate(assets):
        if not isinstance(asset, dict):
            raise SystemExit(f"Remote release asset {position} is not a JSON object.")
        name = asset.get("name")
        asset_id = asset.get("id")
        if not isinstance(name, str) or not name:
            raise SystemExit(f"Remote release asset {position} has an invalid name.")
        if name in inventory:
            raise SystemExit(f"Remote release has duplicate asset name: {name}")
        if isinstance(asset_id, bool) or not isinstance(asset_id, int) or asset_id <= 0:
            raise SystemExit(f"Remote release asset {name!r} has an invalid asset id.")
        if asset_id in asset_ids:
            raise SystemExit(f"Remote release has duplicate asset id: {asset_id}")
        inventory[name] = asset
        asset_ids.add(asset_id)
    return inventory


def stale_remote_asset_ids(
    payload: Any,
    version: str,
    tag: str,
    assets_payload: Any | None = None,
    *,
    asset_dir: Path | None = None,
) -> list[int]:
    """Plan narrowly scoped stale-asset deletion after all local assets were uploaded."""

    version, tag = _release_coordinates(version, tag)
    local_names = publishable_assets(version, tag)
    inventory = _remote_asset_inventory(payload, tag, assets_payload)
    missing = sorted(local_names - set(inventory))
    if missing:
        raise SystemExit(
            "Remote release is missing newly uploaded assets; refusing stale-asset cleanup: "
            + ", ".join(missing)
        )
    if asset_dir is not None:
        _verify_remote_asset_metadata(inventory, asset_dir, local_names)
    return sorted(
        (int(asset["id"]) for name, asset in inventory.items() if name not in local_names)
    )


def _verify_remote_asset_metadata(
    inventory: dict[str, dict[str, Any]],
    asset_dir: Path,
    local_names: set[str],
) -> None:
    for name in sorted(local_names):
        local_path = asset_dir / name
        asset = inventory[name]
        size = asset.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size != local_path.stat().st_size:
            raise SystemExit(f"Remote release asset size mismatch: {name}")
        state = asset.get("state")
        if state is not None and state != "uploaded":
            raise SystemExit(f"Remote release asset is not uploaded: {name}")
        digest = asset.get("digest")
        if digest is not None and digest != f"sha256:{sha256(local_path)}":
            raise SystemExit(f"Remote release asset digest mismatch: {name}")


def verify_remote_asset_inventory(
    payload: Any,
    asset_dir: Path,
    version: str,
    tag: str,
    assets_payload: Any | None = None,
) -> None:
    """Require the remote asset names and metadata to match the verified local set."""

    version, tag = _release_coordinates(version, tag)
    local_names = publishable_assets(version, tag)
    inventory = _remote_asset_inventory(payload, tag, assets_payload)
    remote_names = set(inventory)
    if remote_names != local_names:
        missing = sorted(local_names - remote_names)
        unexpected = sorted(remote_names - local_names)
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected: {', '.join(unexpected)}")
        raise SystemExit("Remote release asset inventory mismatch (" + "; ".join(details) + ").")

    _verify_remote_asset_metadata(inventory, asset_dir, local_names)


def read_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"Invalid {label} {path}: {error}") from error


def verify_release_assets(asset_dir: Path, version: str, tag: str) -> None:
    version, tag = _release_coordinates(version, tag)
    expected = expected_assets(version, tag)
    actual = {path.name for path in asset_dir.iterdir() if path.is_file()}
    allowed = expected | set(CONTROL_ASSETS)
    missing = sorted(expected - actual)
    if missing:
        raise SystemExit(f"Release assets are missing: {', '.join(missing)}")
    unexpected = sorted(actual - allowed)
    if unexpected:
        raise SystemExit(f"Release assets contain unexpected files: {', '.join(unexpected)}")
    release_notes = asset_dir / "RELEASE_NOTES.md"
    if not release_notes.is_file() or not release_notes.read_text(encoding="utf-8").strip():
        raise SystemExit("Release notes are missing or empty.")

    checksums = read_checksums(asset_dir / "SHA256SUMS.txt")
    checksum_names = set(checksums)
    if checksum_names != expected:
        missing_checksums = sorted(expected - checksum_names)
        unexpected_checksums = sorted(checksum_names - expected)
        details = []
        if missing_checksums:
            details.append(f"missing: {', '.join(missing_checksums)}")
        if unexpected_checksums:
            details.append(f"unexpected: {', '.join(unexpected_checksums)}")
        raise SystemExit("SHA256SUMS does not exactly cover release assets (" + "; ".join(details) + ").")

    for name in sorted(expected):
        path = asset_dir / name
        if path.stat().st_size == 0:
            raise SystemExit(f"Release asset is empty: {name}")
        if checksums[name] != sha256(path):
            raise SystemExit(f"SHA256SUMS digest mismatch: {name}")

    verify_sbom(asset_dir / f"market-sentinel-{version}-sbom.spdx.json", version)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify final MarketSentinel release assets and checksums.")
    parser.add_argument("--asset-dir", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument(
        "--remote-release-json",
        type=Path,
        help="Exact-tag GitHub release API response used for remote reconciliation.",
    )
    parser.add_argument(
        "--remote-assets-json",
        type=Path,
        help="Complete paginated GitHub release-assets API response used for reconciliation.",
    )
    remote_mode = parser.add_mutually_exclusive_group()
    remote_mode.add_argument(
        "--print-stale-remote-asset-ids",
        action="store_true",
        help="Print numeric IDs for remote assets absent from the verified local set.",
    )
    remote_mode.add_argument(
        "--verify-remote-inventory",
        action="store_true",
        help="Require remote names, sizes, states, and available digests to match locally.",
    )
    args = parser.parse_args()
    asset_dir = args.asset_dir.resolve()
    if not asset_dir.is_dir():
        raise SystemExit(f"Release asset directory does not exist: {asset_dir}")
    verify_release_assets(asset_dir, str(args.version), str(args.tag))
    remote_requested = args.print_stale_remote_asset_ids or args.verify_remote_inventory
    if remote_requested and args.remote_release_json is None:
        raise SystemExit("--remote-release-json is required for remote release reconciliation.")
    if remote_requested and args.remote_assets_json is None:
        raise SystemExit("--remote-assets-json is required for remote release reconciliation.")
    if args.remote_release_json is not None and not remote_requested:
        raise SystemExit(
            "Choose --print-stale-remote-asset-ids or --verify-remote-inventory with "
            "--remote-release-json."
        )
    if (args.remote_release_json is None) != (args.remote_assets_json is None):
        raise SystemExit("--remote-release-json and --remote-assets-json must be provided together.")
    if args.remote_release_json is not None:
        remote_payload = read_json(args.remote_release_json, "remote release metadata")
        remote_assets = read_json(args.remote_assets_json, "remote release asset inventory")
        if args.print_stale_remote_asset_ids:
            for asset_id in stale_remote_asset_ids(
                remote_payload,
                str(args.version),
                str(args.tag),
                remote_assets,
                asset_dir=asset_dir,
            ):
                print(asset_id)
            return 0
        else:
            verify_remote_asset_inventory(
                remote_payload,
                asset_dir,
                str(args.version),
                str(args.tag),
                remote_assets,
            )
    print(f"[ok] Release assets ({asset_dir})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
