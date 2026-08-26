from __future__ import annotations

"""Generate the exact, bounded report attested after a GitHub release is published."""

import argparse
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from scripts.release_version import normalize_release_tag, normalize_release_version
    from scripts.verify_release_assets import publishable_assets, verify_release_assets, verify_remote_asset_inventory
except ModuleNotFoundError:  # Direct execution adds scripts/, rather than the repository root, to sys.path.
    from release_version import normalize_release_tag, normalize_release_version
    from verify_release_assets import publishable_assets, verify_release_assets, verify_remote_asset_inventory


SCHEMA_VERSION = 1
REPORT_TYPE = "market-sentinel-release-evidence"
REPORT_NAME = "release-evidence.json"
REPORT_MAX_BYTES = 256 * 1024
INPUT_MAX_BYTES = 4 * 1024 * 1024
MAX_RELEASE_HISTORY = 99
WORKFLOW_PATH = ".github/workflows/release.yml"
WORKFLOW_NAME = "Release"
PUBLISH_JOB_NAME = "Publish GitHub release"
TRUSTED_MAIN_REF = "refs/heads/main"
COMMIT_LENGTH = 40


class DuplicateJsonKey(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise DuplicateJsonKey(f"duplicate JSON key: {key}")
        output[key] = value
    return output


def _reject_nonfinite(value: str) -> Any:
    raise ValueError(f"non-finite JSON number: {value}")


def read_json(path: Path, label: str) -> Any:
    try:
        if not path.is_file():
            raise OSError("not a regular file")
        raw = path.read_bytes()
        if not raw or len(raw) > INPUT_MAX_BYTES:
            raise ValueError("empty or oversized JSON")
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError, DuplicateJsonKey) as exc:
        raise SystemExit(f"Invalid {label} {path}: {type(exc).__name__}") from exc


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value:
        raise SystemExit(f"{label} must be a non-empty single-line string.")
    return value


def _required_positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SystemExit(f"{label} must be a positive integer.")
    return value


def _required_commit(value: Any, label: str) -> str:
    candidate = _required_string(value, label).lower()
    if len(candidate) != COMMIT_LENGTH or any(character not in "0123456789abcdef" for character in candidate):
        raise SystemExit(f"{label} must be a full lowercase commit SHA.")
    return candidate


def _timestamp(value: Any, label: str) -> tuple[str, datetime]:
    text = _required_string(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SystemExit(f"{label} must be valid ISO-8601.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SystemExit(f"{label} must include a timezone.")
    normalized = parsed.astimezone(timezone.utc)
    return normalized.isoformat().replace("+00:00", "Z"), normalized


def _expected_head_branch(source_ref: str) -> str:
    for prefix in ("refs/tags/", "refs/heads/"):
        if source_ref.startswith(prefix) and len(source_ref) > len(prefix):
            return source_ref[len(prefix) :]
    raise SystemExit("Release source ref must be a concrete branch or tag ref.")


def _normalized_history(payload: Any, *, current_release_id: int) -> list[dict[str, Any]]:
    if not isinstance(payload, list) or not payload:
        raise SystemExit("Published release history must be a non-empty JSON list.")
    if len(payload) > MAX_RELEASE_HISTORY:
        raise SystemExit(
            f"Published release history exceeds the single-page proof limit of {MAX_RELEASE_HISTORY}; "
            "use a paginated, attested evidence implementation before continuing."
        )

    history: list[dict[str, Any]] = []
    release_ids: set[int] = set()
    tags: set[str] = set()
    for index, row in enumerate(payload):
        if not isinstance(row, dict):
            raise SystemExit(f"Published release history entry {index} is not an object.")
        if row.get("draft") is True:
            continue
        release_id = _required_positive_int(row.get("id"), f"release history entry {index} id")
        tag = _required_string(row.get("tag_name"), f"release history entry {index} tag")
        target = _required_commit(row.get("target_commitish"), f"release history entry {index} target")
        published_at, _ = _timestamp(row.get("published_at"), f"release history entry {index} published_at")
        prerelease = row.get("prerelease")
        if type(prerelease) is not bool:
            raise SystemExit(f"release history entry {index} prerelease must be a boolean.")
        html_url = _required_string(row.get("html_url"), f"release history entry {index} html_url")
        if release_id in release_ids or tag in tags:
            raise SystemExit("Published release history contains duplicate release ids or tags.")
        release_ids.add(release_id)
        tags.add(tag)
        history.append(
            {
                "id": release_id,
                "tag": tag,
                "target_commit": target,
                "prerelease": prerelease,
                "published_at": published_at,
                "html_url": html_url,
            }
        )

    if current_release_id not in release_ids:
        raise SystemExit("Published release history does not contain the current release.")
    return sorted(history, key=lambda entry: (entry["published_at"], entry["id"], entry["tag"]))


def build_release_evidence(
    *,
    asset_dir: Path,
    release_payload: Any,
    assets_payload: Any,
    history_payload: Any,
    run_payload: Any,
    version: str,
    tag: str,
    repository: str,
    revision: str,
    run_id: int,
    run_attempt: int,
    workflow_ref: str,
    source_ref: str,
    event: str,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Validate the post-publication state and return its canonical evidence report."""

    try:
        canonical_version = normalize_release_version(version)
        tag_version = normalize_release_tag(tag)
    except ValueError as exc:
        raise SystemExit(f"Invalid release coordinates: {exc}") from exc
    if canonical_version != version or tag_version != canonical_version:
        raise SystemExit("Release version and tag must use matching canonical forms.")
    repository = _required_string(repository, "repository")
    if repository.count("/") != 1:
        raise SystemExit("repository must use OWNER/REPOSITORY form.")
    revision = _required_commit(revision, "revision")
    run_id = _required_positive_int(run_id, "run_id")
    run_attempt = _required_positive_int(run_attempt, "run_attempt")
    source_ref = _required_string(source_ref, "source_ref")
    expected_head_branch = _expected_head_branch(source_ref)
    if event not in {"push", "workflow_dispatch"}:
        raise SystemExit("Release evidence supports only tag-push or manual release events.")
    if event == "push" and source_ref != f"refs/tags/{tag}":
        raise SystemExit("A tag-push release must run from the exact release tag ref.")
    if event == "workflow_dispatch" and source_ref not in {f"refs/tags/{tag}", TRUSTED_MAIN_REF}:
        raise SystemExit("A manual release must run from the exact release tag or protected main ref.")
    expected_workflow_ref = f"{repository}/{WORKFLOW_PATH}@{source_ref}"
    if workflow_ref != expected_workflow_ref:
        raise SystemExit("workflow_ref does not identify the exact trusted release workflow and source ref.")

    asset_dir = asset_dir.resolve()
    if not asset_dir.is_dir():
        raise SystemExit(f"Release asset directory does not exist: {asset_dir}")
    verify_release_assets(asset_dir, version, tag)
    verify_remote_asset_inventory(release_payload, asset_dir, version, tag, assets_payload)

    if not isinstance(release_payload, dict):
        raise SystemExit("Remote release metadata must be an object.")
    release_id = _required_positive_int(release_payload.get("id"), "release id")
    if release_payload.get("tag_name") != tag:
        raise SystemExit("Published release tag does not match the evidence tag.")
    if _required_commit(release_payload.get("target_commitish"), "release target") != revision:
        raise SystemExit("Published release target does not match the exact source revision.")
    if release_payload.get("draft") is not False or release_payload.get("prerelease") is not False:
        raise SystemExit("Production release evidence requires a published, non-prerelease release.")
    published_at, published_time = _timestamp(release_payload.get("published_at"), "release published_at")
    html_url = _required_string(release_payload.get("html_url"), "release html_url")
    expected_html_url = f"https://github.com/{repository}/releases/tag/{tag}"
    if html_url != expected_html_url:
        raise SystemExit("Published release URL does not match the trusted repository and tag.")

    if not isinstance(assets_payload, list):
        raise SystemExit("Remote release asset inventory must be a list.")
    remote_assets: dict[str, dict[str, Any]] = {}
    for index, asset in enumerate(assets_payload):
        if not isinstance(asset, dict):
            raise SystemExit(f"Remote release asset {index} is not an object.")
        name = _required_string(asset.get("name"), f"remote release asset {index} name")
        if name in remote_assets:
            raise SystemExit("Remote release asset inventory contains duplicate names.")
        remote_assets[name] = asset

    expected_names = publishable_assets(version, tag)
    if set(remote_assets) != expected_names:
        raise SystemExit("Remote release asset inventory does not match the exact publishable set.")
    report_assets: list[dict[str, Any]] = []
    for name in sorted(expected_names):
        local_path = asset_dir / name
        size = local_path.stat().st_size
        digest = sha256(local_path)
        remote = remote_assets[name]
        if remote.get("size") != size or remote.get("digest") != f"sha256:{digest}":
            raise SystemExit(f"Remote release asset metadata does not bind exact size and SHA-256: {name}")
        report_assets.append({"name": name, "size": size, "sha256": digest})

    history = _normalized_history(history_payload, current_release_id=release_id)

    if not isinstance(run_payload, dict):
        raise SystemExit("GitHub Actions run metadata must be an object.")
    expected_run = {
        "id": run_id,
        "head_sha": revision,
        "name": WORKFLOW_NAME,
        "path": WORKFLOW_PATH,
        "event": event,
        "run_attempt": run_attempt,
        "head_branch": expected_head_branch,
    }
    if any(type(run_payload.get(key)) is not type(value) or run_payload.get(key) != value for key, value in expected_run.items()):
        raise SystemExit("GitHub Actions run metadata does not match the release evidence identity.")
    if run_payload.get("status") != "in_progress" or run_payload.get("conclusion") is not None:
        raise SystemExit("Release evidence must be generated by the still-running trusted release job.")
    head_repository = run_payload.get("head_repository")
    if not isinstance(head_repository, dict) or head_repository.get("full_name") != repository:
        raise SystemExit("GitHub Actions run head repository does not match the trusted repository.")
    run_started_at, run_started_time = _timestamp(run_payload.get("run_started_at"), "run started_at")
    generated_at, generated_time = _timestamp(
        generated_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "evidence generated_at",
    )
    skew = timedelta(minutes=5)
    if generated_time < run_started_time - skew:
        raise SystemExit("Release evidence generation predates the trusted workflow run.")
    if generated_time < published_time - skew:
        raise SystemExit("Release evidence generation predates the published release.")

    return {
        "schema_version": SCHEMA_VERSION,
        "report_type": REPORT_TYPE,
        "release": {
            "id": release_id,
            "tag": tag,
            "version": version,
            "target_commit": revision,
            "draft": False,
            "prerelease": False,
            "published_at": published_at,
            "html_url": html_url,
            "assets": report_assets,
        },
        "history": history,
        "evidence": {
            "repository": repository,
            "source_revision": revision,
            "run_id": run_id,
            "run_attempt": run_attempt,
            "workflow": WORKFLOW_PATH,
            "workflow_name": WORKFLOW_NAME,
            "workflow_ref": workflow_ref,
            "source_ref": source_ref,
            "event": event,
            "runner_environment": "github-hosted",
            "job": PUBLISH_JOB_NAME,
            "trusted_main_ref": TRUSTED_MAIN_REF,
            "run_started_at": run_started_at,
            "generated_at": generated_at,
            "published_at": published_at,
        },
    }


def canonical_report_bytes(payload: dict[str, Any]) -> bytes:
    raw = (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    if not raw or len(raw) > REPORT_MAX_BYTES:
        raise SystemExit("Generated release evidence report exceeds its fixed size limit.")
    return raw


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-dir", type=Path, required=True)
    parser.add_argument("--release-json", type=Path, required=True)
    parser.add_argument("--assets-json", type=Path, required=True)
    parser.add_argument("--history-json", type=Path, required=True)
    parser.add_argument("--run-json", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--run-attempt", type=int, required=True)
    parser.add_argument("--workflow-ref", required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--event", required=True)
    parser.add_argument(
        "--generated-at",
        help="Optional explicit ISO-8601 collection time; defaults to the current UTC time.",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    report = build_release_evidence(
        asset_dir=args.asset_dir,
        release_payload=read_json(args.release_json, "remote release metadata"),
        assets_payload=read_json(args.assets_json, "remote release assets"),
        history_payload=read_json(args.history_json, "published release history"),
        run_payload=read_json(args.run_json, "GitHub Actions run metadata"),
        version=args.version,
        tag=args.tag,
        repository=args.repository,
        revision=args.revision,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        workflow_ref=args.workflow_ref,
        source_ref=args.source_ref,
        event=args.event,
        generated_at=args.generated_at,
    )
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    raw = canonical_report_bytes(report)
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_bytes(raw)
    temporary.replace(output)
    print(f"[ok] Published release evidence ({output.name}, sha256={hashlib.sha256(raw).hexdigest()})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
