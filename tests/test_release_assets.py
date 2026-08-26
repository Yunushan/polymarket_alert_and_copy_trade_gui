from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.verify_release_assets import (
    expected_assets,
    publishable_assets,
    sha256,
    stale_remote_asset_ids,
    verify_release_assets,
    verify_remote_asset_inventory,
)


VERSION = "1.2.3"
TAG = "v1.2.3"


class ReleaseAssetVerificationTests(unittest.TestCase):
    def _write_assets(self, root: Path) -> None:
        names = (
            f"market_sentinel-{VERSION}-py3-none-any.whl",
            f"market_sentinel-{VERSION}.tar.gz",
            f"market-sentinel-{TAG}-frontend-dist.zip",
            f"market-sentinel-{TAG}-win-x64.zip",
            f"market-sentinel-{TAG}-win-x64.msi",
        )
        for name in names:
            (root / name).write_bytes(name.encode("utf-8"))
        sbom_name = f"market-sentinel-{VERSION}-sbom.spdx.json"
        (root / sbom_name).write_text(
            json.dumps(
                {
                    "spdxVersion": "SPDX-2.3",
                    "name": f"market-sentinel-{VERSION}-sbom",
                    "packages": [{"name": "market-sentinel", "versionInfo": VERSION}],
                }
            ),
            encoding="utf-8",
        )
        lines = []
        for path in sorted(root.iterdir()):
            if path.name == "SHA256SUMS.txt":
                continue
            lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}")
        (root / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (root / "RELEASE_NOTES.md").write_text("## MarketSentinel\n", encoding="utf-8")

    def _remote_release(
        self,
        root: Path,
        *,
        extra_names: tuple[str, ...] = (),
        missing_names: tuple[str, ...] = (),
    ) -> dict[str, object]:
        assets = []
        for asset_id, name in enumerate(
            sorted(publishable_assets(VERSION, TAG) - set(missing_names)),
            start=100,
        ):
            path = root / name
            assets.append(
                {
                    "id": asset_id,
                    "name": name,
                    "size": path.stat().st_size,
                    "state": "uploaded",
                    "digest": f"sha256:{sha256(path)}",
                }
            )
        for offset, name in enumerate(extra_names, start=900):
            assets.append(
                {
                    "id": offset,
                    "name": name,
                    "size": 5,
                    "state": "uploaded",
                    "digest": None,
                }
            )
        return {"id": 42, "tag_name": TAG, "assets": assets}

    def test_accepts_complete_release_assets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_assets(root)
            verify_release_assets(root, VERSION, TAG)

    def test_prerelease_asset_names_use_canonical_package_version_and_raw_tag(self) -> None:
        names = expected_assets("1.2.3-rc.1", "v1.2.3-rc.1")

        self.assertIn("market_sentinel-1.2.3rc1-py3-none-any.whl", names)
        self.assertIn("market_sentinel-1.2.3rc1.tar.gz", names)
        self.assertIn("market-sentinel-v1.2.3-rc.1-win-x64.msi", names)

    def test_rejects_mismatched_version_and_tag(self) -> None:
        with self.assertRaisesRegex(SystemExit, "resolves to 1.2.4"):
            expected_assets("1.2.3", "v1.2.4")

    def test_rejects_checksum_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_assets(root)
            (root / f"market-sentinel-{TAG}-win-x64.msi").write_text("changed", encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "digest mismatch"):
                verify_release_assets(root, VERSION, TAG)

    def test_rejects_incomplete_checksum_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_assets(root)
            checksum_path = root / "SHA256SUMS.txt"
            lines = checksum_path.read_text(encoding="utf-8").splitlines()
            checksum_path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "does not exactly cover"):
                verify_release_assets(root, VERSION, TAG)

    def test_rejects_unexpected_publishable_asset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_assets(root)
            (root / "unreviewed.bin").write_bytes(b"not a release asset")
            with self.assertRaisesRegex(SystemExit, "unexpected files"):
                verify_release_assets(root, VERSION, TAG)

    def test_remote_cleanup_plan_deletes_only_assets_absent_locally(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_assets(root)
            payload = self._remote_release(root, extra_names=("obsolete.zip", "../legacy.bin"))

            self.assertEqual([900, 901], stale_remote_asset_ids(payload, VERSION, TAG))

    def test_remote_cleanup_plan_requires_every_new_asset_before_deleting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_assets(root)
            payload = self._remote_release(
                root,
                extra_names=("obsolete.zip",),
                missing_names=("SHA256SUMS.txt",),
            )

            with self.assertRaisesRegex(SystemExit, "refusing stale-asset cleanup"):
                stale_remote_asset_ids(payload, VERSION, TAG)

    def test_remote_cleanup_checks_expected_asset_metadata_before_deleting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_assets(root)
            payload = self._remote_release(root, extra_names=("obsolete.zip",))
            payload["assets"][0]["digest"] = "sha256:" + ("0" * 64)

            with self.assertRaisesRegex(SystemExit, "digest mismatch"):
                stale_remote_asset_ids(payload, VERSION, TAG, asset_dir=root)

    def test_remote_cleanup_uses_complete_separately_fetched_asset_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_assets(root)
            payload = self._remote_release(root)
            complete_assets = list(payload["assets"])
            complete_assets.append(
                {
                    "id": 999,
                    "name": "stale-from-later-page.zip",
                    "size": 10,
                    "state": "uploaded",
                    "digest": None,
                }
            )
            payload["assets"] = []

            self.assertEqual(
                [999],
                stale_remote_asset_ids(payload, VERSION, TAG, complete_assets),
            )

    def test_remote_inventory_must_exactly_match_local_names_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_assets(root)
            payload = self._remote_release(root)
            verify_remote_asset_inventory(payload, root, VERSION, TAG)

            payload["assets"][0]["size"] += 1
            with self.assertRaisesRegex(SystemExit, "size mismatch"):
                verify_remote_asset_inventory(payload, root, VERSION, TAG)

    def test_remote_inventory_rejects_wrong_release_and_duplicate_assets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_assets(root)
            payload = self._remote_release(root)
            payload["tag_name"] = "v9.9.9"
            with self.assertRaisesRegex(SystemExit, "tag mismatch"):
                stale_remote_asset_ids(payload, VERSION, TAG)

            payload = self._remote_release(root)
            payload["assets"].append(dict(payload["assets"][0]))
            with self.assertRaisesRegex(SystemExit, "duplicate asset name"):
                stale_remote_asset_ids(payload, VERSION, TAG)

    def test_release_workflow_places_notes_beside_verified_assets_after_checksums(self) -> None:
        workflow = (Path(__file__).resolve().parent.parent / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        checksum = "sha256sum * > SHA256SUMS.txt"
        notes = "cat > release-assets/RELEASE_NOTES.md"
        self.assertIn(notes, workflow)
        self.assertLess(workflow.index(checksum), workflow.index(notes))
        self.assertEqual(workflow.count("--notes-file release-assets/RELEASE_NOTES.md"), 2)
        self.assertNotIn("--notes-file RELEASE_NOTES.md", workflow)
