from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

from scripts.check_product_readiness import (
    REQUIRED_RELEASE_METADATA_STEPS,
    REQUIRED_RELEASE_PUBLISH_STEPS,
    _attested_release_report,
)
from scripts.verify_release_assets import publishable_assets


VERSION = "1.0.11"
TAG = f"v{VERSION}"
REPOSITORY = "Yunushan/market-sentinel"
REVISION = "a" * 40
RUN_ID = 123456
RUN_ATTEMPT = 1
RELEASE_ID = 456
REF = f"refs/tags/{TAG}"
WORKFLOW_PATH = ".github/workflows/release.yml"
WORKFLOW_REF = f"{REPOSITORY}/{WORKFLOW_PATH}@{REF}"


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _release_report(now: datetime) -> dict[str, Any]:
    run_started = now - timedelta(minutes=9)
    published = now - timedelta(minutes=3)
    assets = [
        {
            "name": name,
            "size": index + 10,
            "sha256": hashlib.sha256(name.encode()).hexdigest(),
        }
        for index, name in enumerate(sorted(publishable_assets(VERSION, TAG)))
    ]
    url = f"https://github.com/{REPOSITORY}/releases/tag/{TAG}"
    return {
        "schema_version": 1,
        "report_type": "market-sentinel-release-evidence",
        "release": {
            "id": RELEASE_ID,
            "tag": TAG,
            "version": VERSION,
            "target_commit": REVISION,
            "draft": False,
            "prerelease": False,
            "published_at": _iso(published),
            "html_url": url,
            "assets": assets,
        },
        "history": [
            {
                "id": RELEASE_ID,
                "tag": TAG,
                "target_commit": REVISION,
                "prerelease": False,
                "published_at": _iso(published),
                "html_url": url,
            }
        ],
        "evidence": {
            "repository": REPOSITORY,
            "source_revision": REVISION,
            "run_id": RUN_ID,
            "run_attempt": RUN_ATTEMPT,
            "workflow": WORKFLOW_PATH,
            "workflow_name": "Release",
            "workflow_ref": WORKFLOW_REF,
            "source_ref": REF,
            "event": "push",
            "runner_environment": "github-hosted",
            "job": "Publish GitHub release",
            "trusted_main_ref": "refs/heads/main",
            "run_started_at": _iso(run_started),
            "generated_at": _iso(now - timedelta(minutes=2)),
            "published_at": _iso(published),
        },
    }


def _attestation(report_hash: str, now: datetime) -> list[dict[str, Any]]:
    repository_uri = f"https://github.com/{REPOSITORY}"
    workflow_uri = f"https://github.com/{WORKFLOW_REF}"
    invocation_uri = f"{repository_uri}/actions/runs/{RUN_ID}/attempts/{RUN_ATTEMPT}"
    return [
        {
            "attestation": {"bundle": "verified"},
            "verificationResult": {
                "mediaType": "application/vnd.dev.sigstore.verificationresult+json;version=0.1",
                "statement": {
                    "_type": "https://in-toto.io/Statement/v1",
                    "predicateType": "https://slsa.dev/provenance/v1",
                    "subject": [
                        {"name": "release-evidence.json", "digest": {"sha256": report_hash}}
                    ],
                    "predicate": {
                        "buildDefinition": {
                            "buildType": "https://actions.github.io/buildtypes/workflow/v1",
                            "externalParameters": {
                                "workflow": {
                                    "path": WORKFLOW_PATH,
                                    "ref": REF,
                                    "repository": repository_uri,
                                }
                            },
                            "internalParameters": {
                                "github": {
                                    "event_name": "push",
                                    "runner_environment": "github-hosted",
                                }
                            },
                            "resolvedDependencies": [
                                {
                                    "uri": f"git+{repository_uri}@{REF}",
                                    "digest": {"gitCommit": REVISION},
                                }
                            ],
                        },
                        "runDetails": {
                            "builder": {"id": workflow_uri},
                            "metadata": {"invocationId": invocation_uri},
                        },
                    },
                },
                "signature": {
                    "certificate": {
                        "subjectAlternativeName": workflow_uri,
                        "issuer": "https://token.actions.githubusercontent.com",
                        "buildSignerURI": workflow_uri,
                        "buildSignerDigest": REVISION,
                        "runnerEnvironment": "github-hosted",
                        "sourceRepositoryURI": repository_uri,
                        "sourceRepositoryDigest": REVISION,
                        "sourceRepositoryRef": REF,
                        "sourceRepositoryOwnerURI": "https://github.com/Yunushan",
                        "buildConfigURI": workflow_uri,
                        "buildConfigDigest": REVISION,
                        "buildTrigger": "push",
                        "runInvocationURI": invocation_uri,
                        "sourceRepositoryVisibilityAtSigning": "public",
                    }
                },
                "verifiedTimestamps": [{"type": "Tlog", "timestamp": _iso(now - timedelta(minutes=1))}],
            },
        }
    ]


def _successful_gh(
    report: dict[str, Any],
    now: datetime,
    *,
    corrupt_asset_digest: bool = False,
) -> Callable[..., tuple[Any | None, str]]:
    run_started = now - timedelta(minutes=9)
    published = datetime.fromisoformat(
        report["release"]["published_at"].replace("Z", "+00:00")
    )
    publish_started = now - timedelta(minutes=5)
    publish_completed = now - timedelta(minutes=1)
    report_assets = {asset["name"]: asset for asset in report["release"]["assets"]}

    def query(command: list[str], **_: object) -> tuple[Any | None, str]:
        route = command[-1]
        if command[:3] == ["gh", "attestation", "verify"]:
            report_hash = hashlib.sha256(Path(command[3]).read_bytes()).hexdigest()
            return _attestation(report_hash, now), ""
        if route.endswith(f"/actions/runs/{RUN_ID}"):
            return {
                "id": RUN_ID,
                "head_sha": REVISION,
                "name": "Release",
                "path": WORKFLOW_PATH,
                "event": "push",
                "status": "completed",
                "conclusion": "success",
                "run_attempt": RUN_ATTEMPT,
                "head_branch": TAG,
                "head_repository": {"full_name": REPOSITORY},
                "created_at": _iso(now - timedelta(minutes=10)),
                "run_started_at": _iso(run_started),
                "updated_at": _iso(now - timedelta(seconds=30)),
            }, ""
        if "/attempts/" in route and "/jobs?" in route:
            common = {
                "status": "completed",
                "conclusion": "success",
                "head_sha": REVISION,
                "run_attempt": RUN_ATTEMPT,
                "started_at": _iso(run_started),
                "completed_at": _iso(publish_completed),
            }
            return {
                "total_count": 2,
                "jobs": [
                    {
                        **common,
                        "name": "Release metadata",
                        "labels": ["ubuntu-latest"],
                        "steps": [
                            {"name": name, "status": "completed", "conclusion": "success"}
                            for name in REQUIRED_RELEASE_METADATA_STEPS
                        ],
                    },
                    {
                        **common,
                        "name": "Publish GitHub release",
                        "labels": ["ubuntu-24.04"],
                        "started_at": _iso(publish_started),
                        "steps": [
                            {"name": name, "status": "completed", "conclusion": "success"}
                            for name in REQUIRED_RELEASE_PUBLISH_STEPS
                        ],
                    },
                ],
            }, ""
        if route.endswith(f"/actions/runs/{RUN_ID}/artifacts?per_page=100"):
            return {
                "total_count": 1,
                "artifacts": [
                    {
                        "id": 789,
                        "name": f"release-evidence-{REVISION}-{RUN_ID}-{RUN_ATTEMPT}",
                        "size_in_bytes": 2048,
                        "expired": False,
                        "created_at": _iso(now - timedelta(minutes=2)),
                        "updated_at": _iso(now - timedelta(minutes=1)),
                        "workflow_run": {"id": RUN_ID, "head_sha": REVISION},
                    }
                ],
            }, ""
        if route.endswith(f"/releases/tags/{TAG}"):
            return {
                "id": RELEASE_ID,
                "tag_name": TAG,
                "target_commitish": REVISION,
                "draft": False,
                "prerelease": False,
                "published_at": _iso(published),
                "html_url": f"https://github.com/{REPOSITORY}/releases/tag/{TAG}",
            }, ""
        if route.endswith(f"/releases/{RELEASE_ID}/assets?per_page=100"):
            assets = [
                {
                    "id": index,
                    "name": name,
                    "state": "uploaded",
                    "size": asset["size"],
                    "digest": f"sha256:{asset['sha256']}",
                }
                for index, (name, asset) in enumerate(sorted(report_assets.items()), start=1)
            ]
            if corrupt_asset_digest:
                assets[0]["digest"] = "sha256:" + "0" * 64
            return assets, ""
        if route.endswith("/releases?per_page=100"):
            return [
                {
                    "id": RELEASE_ID,
                    "tag_name": TAG,
                    "target_commitish": REVISION,
                    "draft": False,
                    "prerelease": False,
                    "published_at": _iso(published),
                    "html_url": f"https://github.com/{REPOSITORY}/releases/tag/{TAG}",
                }
            ], ""
        if route.endswith(f"/git/ref/tags/{TAG}"):
            return {"object": {"type": "commit", "sha": REVISION}}, ""
        if route.endswith("/branches/main"):
            return {"name": "main", "protected": True, "commit": {"sha": REVISION}}, ""
        if route.endswith(f"/compare/{REVISION}...main"):
            return {
                "status": "identical",
                "base_commit": {"sha": REVISION},
                "merge_base_commit": {"sha": REVISION},
            }, ""
        raise AssertionError(f"unexpected GitHub query: {command}")

    return query


class AttestedReleaseReadinessTests(unittest.TestCase):
    def test_accepts_exact_attested_release_and_rejects_live_asset_mutation(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        report = _release_report(now)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "release-evidence.json"
            path.write_bytes(
                (json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
            )
            with patch(
                "scripts.check_product_readiness._run_gh_json",
                side_effect=_successful_gh(report, now),
            ):
                accepted = _attested_release_report(
                    str(path),
                    expected_revision=REVISION,
                    expected_version=VERSION,
                    now=now,
                )
            with patch(
                "scripts.check_product_readiness._run_gh_json",
                side_effect=_successful_gh(report, now, corrupt_asset_digest=True),
            ):
                rejected = _attested_release_report(
                    str(path),
                    expected_revision=REVISION,
                    expected_version=VERSION,
                    now=now,
                )

        self.assertEqual(accepted["status"], "pass", accepted)
        self.assertEqual(accepted["report"]["asset_count"], 8)
        self.assertEqual(rejected["status"], "fail")
        self.assertIn("SHA-256", rejected["detail"])

    def test_accepts_fresh_evidence_for_an_older_unchanged_release(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        report = _release_report(now)
        old_published = _iso(now - timedelta(days=30))
        report["release"]["published_at"] = old_published
        report["history"][0]["published_at"] = old_published
        report["evidence"]["published_at"] = old_published
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "release-evidence.json"
            path.write_bytes(
                (json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
            )
            with patch(
                "scripts.check_product_readiness._run_gh_json",
                side_effect=_successful_gh(report, now),
            ):
                result = _attested_release_report(
                    str(path),
                    expected_revision=REVISION,
                    expected_version=VERSION,
                    now=now,
                )

        self.assertEqual(result["status"], "pass", result)

    def test_rejects_stale_evidence_collection_even_for_a_current_release(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        report = _release_report(now)
        report["evidence"]["generated_at"] = _iso(now - timedelta(hours=25))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "release-evidence.json"
            path.write_bytes(
                (json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
            )
            with patch("scripts.check_product_readiness._run_gh_json") as run:
                result = _attested_release_report(
                    str(path),
                    expected_revision=REVISION,
                    expected_version=VERSION,
                    now=now,
                )

        self.assertEqual(result["status"], "fail")
        self.assertIn("generated_at", result["detail"])
        run.assert_not_called()

    def test_rejects_a_moved_historical_release_tag(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        report = _release_report(now)
        historical_tag = "v1.0.10"
        historical_target = "b" * 40
        historical_entry = {
            "id": RELEASE_ID - 1,
            "tag": historical_tag,
            "target_commit": historical_target,
            "prerelease": False,
            "published_at": _iso(now - timedelta(days=30)),
            "html_url": f"https://github.com/{REPOSITORY}/releases/tag/{historical_tag}",
        }
        report["history"].insert(0, historical_entry)
        base_query = _successful_gh(report, now)

        def query(command: list[str], **kwargs: object) -> tuple[Any | None, str]:
            route = command[-1]
            if route.endswith("/releases?per_page=100"):
                return [
                    {
                        "id": entry["id"],
                        "tag_name": entry["tag"],
                        "target_commitish": entry["target_commit"],
                        "draft": False,
                        "prerelease": entry["prerelease"],
                        "published_at": entry["published_at"],
                        "html_url": entry["html_url"],
                    }
                    for entry in report["history"]
                ], ""
            if route.endswith(f"/git/ref/tags/{historical_tag}"):
                return {"object": {"type": "commit", "sha": "c" * 40}}, ""
            return base_query(command, **kwargs)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "release-evidence.json"
            path.write_bytes(
                (json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
            )
            with patch(
                "scripts.check_product_readiness._run_gh_json",
                side_effect=query,
            ):
                result = _attested_release_report(
                    str(path),
                    expected_revision=REVISION,
                    expected_version=VERSION,
                    now=now,
                )

        self.assertEqual(result["status"], "fail")
        self.assertIn(historical_tag, result["detail"])
        self.assertIn("recorded target", result["detail"])

    def test_rejects_a_historical_release_target_outside_protected_main(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        report = _release_report(now)
        historical_tag = "v1.0.10"
        historical_target = "b" * 40
        report["history"].insert(
            0,
            {
                "id": RELEASE_ID - 1,
                "tag": historical_tag,
                "target_commit": historical_target,
                "prerelease": False,
                "published_at": _iso(now - timedelta(days=30)),
                "html_url": f"https://github.com/{REPOSITORY}/releases/tag/{historical_tag}",
            },
        )
        base_query = _successful_gh(report, now)

        def query(command: list[str], **kwargs: object) -> tuple[Any | None, str]:
            route = command[-1]
            if route.endswith("/releases?per_page=100"):
                return [
                    {
                        "id": entry["id"],
                        "tag_name": entry["tag"],
                        "target_commitish": entry["target_commit"],
                        "draft": False,
                        "prerelease": entry["prerelease"],
                        "published_at": entry["published_at"],
                        "html_url": entry["html_url"],
                    }
                    for entry in report["history"]
                ], ""
            if route.endswith(f"/git/ref/tags/{historical_tag}"):
                return {"object": {"type": "commit", "sha": historical_target}}, ""
            if route.endswith(f"/compare/{historical_target}...main"):
                return {
                    "status": "diverged",
                    "base_commit": {"sha": historical_target},
                    "merge_base_commit": {"sha": "c" * 40},
                }, ""
            return base_query(command, **kwargs)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "release-evidence.json"
            path.write_bytes(
                (json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
            )
            with patch(
                "scripts.check_product_readiness._run_gh_json",
                side_effect=query,
            ):
                result = _attested_release_report(
                    str(path),
                    expected_revision=REVISION,
                    expected_version=VERSION,
                    now=now,
                )

        self.assertEqual(result["status"], "fail")
        self.assertIn(historical_tag, result["detail"])
        self.assertIn("protected main ancestry", result["detail"])


if __name__ == "__main__":
    unittest.main()
