from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from stat import S_IFLNK, S_IFMT, S_IFREG
from typing import Any
from urllib.parse import urlparse
from zipfile import BadZipFile, ZipFile

try:
    from scripts.release_version import normalize_release_tag, normalize_release_version
    from scripts.review_deployment_evidence import review_deployment_report, review_external_probe_report
except ModuleNotFoundError:  # Direct execution adds scripts/ to sys.path.
    from release_version import normalize_release_tag, normalize_release_version
    from review_deployment_evidence import review_deployment_report, review_external_probe_report


REPOSITORY = "Yunushan/market-sentinel"
WORKFLOW = ".github/workflows/deployment-evidence.yml"
WORKFLOW_NAME = "Production deployment evidence"
REPORT_TYPE = "market-sentinel-deployment-evidence"
IDENTITY_TYPE = "market-sentinel-deployment-release-identity"
COLLECTOR_JOB = "Collect production deployment evidence"
REVIEW_JOB = "Review and attest production deployment evidence"
EXTERNAL_PROBE_JOB = "Probe production externally from GitHub-hosted runner"
COLLECTOR_LABELS = ("self-hosted", "linux", "x64", "market-sentinel-production")
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
MAX_JSON_BYTES = 1024 * 1024
MAX_FRONTEND_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_FRONTEND_FILES = 10_000
MAX_FRONTEND_EXPANDED_BYTES = 512 * 1024 * 1024


class DeploymentEvidenceGenerationError(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DeploymentEvidenceGenerationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise DeploymentEvidenceGenerationError(f"non-finite JSON number: {value}")


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise DeploymentEvidenceGenerationError(f"not a regular JSON file: {path}")
    raw = path.read_bytes()
    if not raw or len(raw) > MAX_JSON_BYTES:
        raise DeploymentEvidenceGenerationError("JSON input is empty or oversized")
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeploymentEvidenceGenerationError("JSON input is malformed") from exc
    if not isinstance(payload, dict):
        raise DeploymentEvidenceGenerationError("JSON input must be an object")
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_field(digest: Any, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def frontend_archive_sha256(path: Path) -> str:
    """Derive the runtime frontend-tree digest directly from the exact release ZIP."""

    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FRONTEND_ARCHIVE_BYTES:
        raise DeploymentEvidenceGenerationError("frontend release asset is unavailable or oversized")
    entries: dict[str, tuple[int, bytes]] = {}
    expanded = 0
    try:
        with ZipFile(path) as archive:
            for info in archive.infolist():
                raw_name = info.filename.replace("\\", "/")
                name = PurePosixPath(raw_name)
                if (
                    info.is_dir()
                    or not raw_name
                    or name.is_absolute()
                    or ".." in name.parts
                    or raw_name != name.as_posix()
                ):
                    if info.is_dir() and raw_name and ".." not in name.parts and not name.is_absolute():
                        continue
                    raise DeploymentEvidenceGenerationError("frontend release asset has an unsafe member")
                mode_type = S_IFMT((info.external_attr >> 16) & 0xFFFF)
                if mode_type in {S_IFLNK} or mode_type not in {0, S_IFREG}:
                    raise DeploymentEvidenceGenerationError("frontend release asset has a non-regular member")
                if raw_name in entries or len(entries) >= MAX_FRONTEND_FILES:
                    raise DeploymentEvidenceGenerationError("frontend release asset has duplicate or excessive members")
                if info.file_size < 0 or expanded + info.file_size > MAX_FRONTEND_EXPANDED_BYTES:
                    raise DeploymentEvidenceGenerationError("frontend release asset expands beyond the safety limit")
                body = archive.read(info)
                if len(body) != info.file_size:
                    raise DeploymentEvidenceGenerationError("frontend release asset member size changed")
                expanded += len(body)
                entries[raw_name] = (len(body), hashlib.sha256(body).digest())
    except (BadZipFile, OSError, RuntimeError) as exc:
        raise DeploymentEvidenceGenerationError("frontend release asset is not a valid ZIP") from exc
    if "index.html" not in entries:
        raise DeploymentEvidenceGenerationError("frontend release asset is missing index.html")
    digest = hashlib.sha256(b"market-sentinel-frontend-tree-v1\0")
    for name in sorted(entries):
        size, file_digest = entries[name]
        _hash_field(digest, name.encode("utf-8"))
        _hash_field(digest, str(size).encode("ascii"))
        _hash_field(digest, file_digest)
    return digest.hexdigest()


def _canonical_origin(value: str) -> str:
    parsed = urlparse(value.strip())
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise DeploymentEvidenceGenerationError("production origin must be an origin-only HTTPS URL")
    hostname = parsed.hostname.lower()
    if hostname in {"localhost", "example.com", "analytics.example.com"} or hostname.endswith(".example.com"):
        raise DeploymentEvidenceGenerationError("production origin must not use a placeholder or localhost")
    port = f":{parsed.port}" if parsed.port and parsed.port != 443 else ""
    return f"https://{hostname}{port}"


def generate_release_identity(
    release_payload: dict[str, Any],
    frontend_asset: Path,
    *,
    tag: str,
    version: str,
    revision: str,
) -> dict[str, Any]:
    version = normalize_release_version(version)
    if normalize_release_tag(tag) != version or not COMMIT_SHA.fullmatch(revision):
        raise DeploymentEvidenceGenerationError("release coordinates are invalid")
    release_id = release_payload.get("id")
    expected_url = f"https://github.com/{REPOSITORY}/releases/tag/{tag}"
    if (
        type(release_id) is not int
        or release_id <= 0
        or release_payload.get("tag_name") != tag
        or release_payload.get("target_commitish") != revision
        or release_payload.get("draft") is not False
        or release_payload.get("prerelease") is not False
        or release_payload.get("html_url") != expected_url
        or not isinstance(release_payload.get("published_at"), str)
    ):
        raise DeploymentEvidenceGenerationError("release metadata does not match the exact stable release")
    asset_name = f"market-sentinel-{tag}-frontend-dist.zip"
    assets = release_payload.get("assets")
    matches = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == asset_name] if isinstance(assets, list) else []
    if len(matches) != 1:
        raise DeploymentEvidenceGenerationError("release has no unique frontend asset")
    asset = matches[0]
    local_digest = _sha256(frontend_asset)
    if (
        type(asset.get("id")) is not int
        or asset["id"] <= 0
        or asset.get("state") != "uploaded"
        or asset.get("size") != frontend_asset.stat().st_size
        or asset.get("digest") != f"sha256:{local_digest}"
        or not SHA256_HEX.fullmatch(local_digest)
    ):
        raise DeploymentEvidenceGenerationError("downloaded frontend asset does not match GitHub metadata")
    return {
        "schema_version": 1,
        "identity_type": IDENTITY_TYPE,
        "repository": REPOSITORY,
        "release": {
            "id": release_id,
            "tag": tag,
            "version": version,
            "target_commit": revision,
            "published_at": release_payload["published_at"],
            "html_url": expected_url,
            "asset": {
                "id": asset["id"],
                "name": asset_name,
                "size": asset["size"],
                "sha256": local_digest,
            },
        },
        "frontend_sha256": frontend_archive_sha256(frontend_asset),
    }


def generate_evidence(
    raw_report_path: Path,
    external_probe_path: Path,
    identity_path: Path,
    *,
    public_origin: str,
    run_id: int,
    run_attempt: int,
    workflow_ref: str,
    source_ref: str,
    artifact_name: str,
) -> dict[str, Any]:
    identity = _read_json(identity_path)
    if identity.get("identity_type") != IDENTITY_TYPE or identity.get("repository") != REPOSITORY:
        raise DeploymentEvidenceGenerationError("trusted release identity is invalid")
    release = identity.get("release")
    if not isinstance(release, dict):
        raise DeploymentEvidenceGenerationError("trusted release identity is missing release metadata")
    canonical_origin = _canonical_origin(public_origin)
    raw_report = _read_json(raw_report_path)
    collection = raw_report.get("collection")
    expected_nonce = f"{release.get('target_commit') or ''}:{run_id}:{run_attempt}"
    if (
        not isinstance(collection, dict)
        or collection.get("public_origin") != canonical_origin
        or collection.get("run_id") != run_id
        or collection.get("run_attempt") != run_attempt
        or collection.get("nonce") != expected_nonce
    ):
        raise DeploymentEvidenceGenerationError("raw deployment report is not bound to the exact production origin")
    revision = str(release.get("target_commit") or "")
    version = str(release.get("version") or "")
    review = review_deployment_report(
        raw_report_path,
        expected_version=version,
        expected_revision=revision,
        expected_run_id=run_id,
        expected_run_attempt=run_attempt,
        expected_nonce=expected_nonce,
    )
    external_probe = review_external_probe_report(
        external_probe_path,
        expected_version=version,
        expected_revision=revision,
        expected_frontend_sha256=str(identity.get("frontend_sha256") or ""),
        expected_origin=canonical_origin,
        expected_run_id=run_id,
        expected_run_attempt=run_attempt,
        expected_nonce=expected_nonce,
    )
    if review.get("frontend_sha256") != identity.get("frontend_sha256"):
        raise DeploymentEvidenceGenerationError("deployment frontend does not match the exact release asset")
    if type(run_id) is not int or run_id <= 0 or type(run_attempt) is not int or run_attempt <= 0:
        raise DeploymentEvidenceGenerationError("workflow run identity is invalid")
    expected_workflow_ref = f"{REPOSITORY}/{WORKFLOW}@refs/heads/main"
    if workflow_ref != expected_workflow_ref or source_ref != "refs/heads/main":
        raise DeploymentEvidenceGenerationError("deployment evidence must originate from protected main")
    if artifact_name != f"deployment-evidence-{revision}-{run_id}-{run_attempt}":
        raise DeploymentEvidenceGenerationError("deployment artifact name is not run-unique")
    return {
        "schema_version": 1,
        "report_type": REPORT_TYPE,
        "deployment": {
            "environment": "production",
            "public_origin": canonical_origin,
            "collected_at": review["collected_at"],
            "raw_report_sha256": review["raw_report_sha256"],
            "workflow_nonce": expected_nonce,
            "check_count": review["check_count"],
            "frontend_sha256": review["frontend_sha256"],
            "deployment_provider": review["deployment_provider"],
            "host_identity_sha256": review["host_identity_sha256"],
            "restore_drill": review["restore_drill"],
            "rollback_drill": review["rollback_drill"],
            "external_probe": {
                "probed_at": external_probe["probed_at"],
                "raw_report_sha256": external_probe["report_sha256"],
                "runner_environment": "github-hosted",
                "api_version": external_probe["api_version"],
                "source_revision": external_probe["source_revision"],
                "frontend_sha256": external_probe["frontend_sha256"],
                "unauthenticated_probes": external_probe["unauthenticated_probes"],
            },
            "release": release,
        },
        "evidence": {
            "repository": REPOSITORY,
            "source_revision": revision,
            "run_id": run_id,
            "run_attempt": run_attempt,
            "workflow": WORKFLOW,
            "workflow_name": WORKFLOW_NAME,
            "workflow_ref": workflow_ref,
            "source_ref": source_ref,
            "event": "workflow_dispatch",
            "runner_environment": "github-hosted",
            "collector_job": COLLECTOR_JOB,
            "external_probe_job": EXTERNAL_PROBE_JOB,
            "review_job": REVIEW_JOB,
            "collector_labels": list(COLLECTOR_LABELS),
            "artifact_name": artifact_name,
        },
    }


def _write_canonical(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate canonical GitHub-attestable deployment evidence.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    identity = subparsers.add_parser("identity")
    identity.add_argument("--release-json", type=Path, required=True)
    identity.add_argument("--frontend-asset", type=Path, required=True)
    identity.add_argument("--tag", required=True)
    identity.add_argument("--version", required=True)
    identity.add_argument("--revision", required=True)
    identity.add_argument("--output", type=Path, required=True)
    evidence = subparsers.add_parser("evidence")
    evidence.add_argument("--raw-report", type=Path, required=True)
    evidence.add_argument("--external-probe-report", type=Path, required=True)
    evidence.add_argument("--identity", type=Path, required=True)
    evidence.add_argument("--public-origin", required=True)
    evidence.add_argument("--run-id", type=int, required=True)
    evidence.add_argument("--run-attempt", type=int, required=True)
    evidence.add_argument("--workflow-ref", required=True)
    evidence.add_argument("--source-ref", required=True)
    evidence.add_argument("--artifact-name", required=True)
    evidence.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "identity":
            payload = generate_release_identity(
                _read_json(args.release_json),
                args.frontend_asset,
                tag=args.tag,
                version=args.version,
                revision=args.revision.lower(),
            )
        else:
            payload = generate_evidence(
                args.raw_report,
                args.external_probe_report,
                args.identity,
                public_origin=args.public_origin,
                run_id=args.run_id,
                run_attempt=args.run_attempt,
                workflow_ref=args.workflow_ref,
                source_ref=args.source_ref,
                artifact_name=args.artifact_name,
            )
        _write_canonical(args.output, payload)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
