from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.generate_release_evidence import build_release_evidence, canonical_report_bytes
from scripts.verify_release_assets import expected_assets, publishable_assets


VERSION = "1.0.11"
TAG = f"v{VERSION}"
REPOSITORY = "Yunushan/market-sentinel"
REVISION = "a" * 40
RUN_ID = 123456
RUN_ATTEMPT = 1


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_release_assets(directory: Path) -> None:
    for name in sorted(expected_assets(VERSION, TAG)):
        path = directory / name
        if name.endswith("-sbom.spdx.json"):
            path.write_text(
                json.dumps(
                    {
                        "spdxVersion": "SPDX-2.3",
                        "name": f"market-sentinel-{VERSION}-sbom",
                        "packages": [{"name": "market-sentinel", "versionInfo": VERSION}],
                    }
                ),
                encoding="utf-8",
            )
        else:
            path.write_bytes(f"exact bytes for {name}\n".encode())
    checksums = "".join(
        f"{_sha256(directory / name)}  {name}\n" for name in sorted(expected_assets(VERSION, TAG))
    )
    (directory / "SHA256SUMS.txt").write_text(checksums, encoding="utf-8")
    (directory / "RELEASE_NOTES.md").write_text("Verified release notes.\n", encoding="utf-8")


def _inputs(directory: Path) -> dict[str, object]:
    current = datetime.now(timezone.utc).replace(microsecond=0)
    started = current - timedelta(minutes=2)
    published = current - timedelta(minutes=1)
    generated = current - timedelta(seconds=30)
    release = {
        "id": 456,
        "tag_name": TAG,
        "target_commitish": REVISION,
        "draft": False,
        "prerelease": False,
        "published_at": published.isoformat().replace("+00:00", "Z"),
        "html_url": f"https://github.com/{REPOSITORY}/releases/tag/{TAG}",
    }
    assets = [
        {
            "id": index,
            "name": name,
            "size": (directory / name).stat().st_size,
            "state": "uploaded",
            "digest": f"sha256:{_sha256(directory / name)}",
        }
        for index, name in enumerate(sorted(publishable_assets(VERSION, TAG)), start=1)
    ]
    history = [{**release}]
    run = {
        "id": RUN_ID,
        "head_sha": REVISION,
        "name": "Release",
        "path": ".github/workflows/release.yml",
        "event": "push",
        "status": "in_progress",
        "conclusion": None,
        "run_attempt": RUN_ATTEMPT,
        "head_branch": TAG,
        "head_repository": {"full_name": REPOSITORY},
        "run_started_at": started.isoformat().replace("+00:00", "Z"),
    }
    return {
        "asset_dir": directory,
        "release_payload": release,
        "assets_payload": assets,
        "history_payload": history,
        "run_payload": run,
        "version": VERSION,
        "tag": TAG,
        "repository": REPOSITORY,
        "revision": REVISION,
        "run_id": RUN_ID,
        "run_attempt": RUN_ATTEMPT,
        "workflow_ref": f"{REPOSITORY}/.github/workflows/release.yml@refs/tags/{TAG}",
        "source_ref": f"refs/tags/{TAG}",
        "event": "push",
        "generated_at": generated.isoformat().replace("+00:00", "Z"),
    }


class ReleaseEvidenceTests(unittest.TestCase):
    def test_generator_binds_deterministic_report_to_exact_remote_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _write_release_assets(directory)
            report = build_release_evidence(**_inputs(directory))

        self.assertEqual(report["report_type"], "market-sentinel-release-evidence")
        self.assertEqual(report["release"]["target_commit"], REVISION)
        self.assertEqual(
            [asset["name"] for asset in report["release"]["assets"]],
            sorted(publishable_assets(VERSION, TAG)),
        )
        canonical = canonical_report_bytes(report)
        self.assertEqual(json.loads(canonical), report)
        self.assertTrue(canonical.startswith(b'{\n  "evidence":'))
        self.assertTrue(canonical.endswith(b"\n"))

    def test_generator_fails_closed_on_remote_digest_or_workflow_ref_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _write_release_assets(directory)
            inputs = _inputs(directory)
            inputs["assets_payload"][0]["digest"] = "sha256:" + "0" * 64
            with self.assertRaises(SystemExit):
                build_release_evidence(**inputs)

            inputs = _inputs(directory)
            inputs["workflow_ref"] = (
                f"{REPOSITORY}/.github/workflows/release.yml@refs/heads/unprotected"
            )
            with self.assertRaises(SystemExit):
                build_release_evidence(**inputs)

    def test_generator_refreshes_evidence_for_an_older_unchanged_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _write_release_assets(directory)
            inputs = _inputs(directory)
            old_publication = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=30)
            old_timestamp = old_publication.isoformat().replace("+00:00", "Z")
            inputs["release_payload"]["published_at"] = old_timestamp
            inputs["history_payload"][0]["published_at"] = old_timestamp

            report = build_release_evidence(**inputs)

        self.assertEqual(report["release"]["published_at"], old_timestamp)
        self.assertEqual(report["evidence"]["generated_at"], inputs["generated_at"])

    def test_generator_rejects_collection_before_the_workflow_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _write_release_assets(directory)
            inputs = _inputs(directory)
            inputs["generated_at"] = (
                datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=1)
            ).isoformat().replace("+00:00", "Z")

            with self.assertRaisesRegex(SystemExit, "generation predates"):
                build_release_evidence(**inputs)


if __name__ == "__main__":
    unittest.main()
