from __future__ import annotations

"""Compute a conservative, reproducible MarketSentinel readiness score.

The local checks can be run from a clean checkout. External evidence is never
inferred from configuration or self-asserted JSON. Every score-eligible hosted
control, platform, deployment, release, credentialed, and funded input is bound
to an exact clean revision and verified through a narrowly scoped workflow,
GitHub run/job state, an uploaded artifact, and exact-byte artifact attestation.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 compatibility.
    import tomli as tomllib

try:
    from scripts.release_version import normalize_release_tag, normalize_release_version
    from scripts.review_deployment_evidence import DeploymentEvidenceError, review_deployment_report
    from scripts.verify_restored_state import application_check_valid
    from scripts.trusted_readiness_evidence import (
        REPORT_TYPE as TRUSTED_EVIDENCE_REPORT_TYPE,
        WORKFLOW_CONTRACTS as TRUSTED_EVIDENCE_WORKFLOW_CONTRACTS,
        canonical_json_bytes as trusted_evidence_canonical_bytes,
        derive_platform_checks,
        validate_manifest as validate_trusted_evidence_manifest,
    )
    from scripts.verify_release_assets import publishable_assets
except ModuleNotFoundError:  # Direct execution adds scripts/, rather than the repository root, to sys.path.
    from release_version import normalize_release_tag, normalize_release_version
    from review_deployment_evidence import DeploymentEvidenceError, review_deployment_report
    from verify_restored_state import application_check_valid
    from trusted_readiness_evidence import (
        REPORT_TYPE as TRUSTED_EVIDENCE_REPORT_TYPE,
        WORKFLOW_CONTRACTS as TRUSTED_EVIDENCE_WORKFLOW_CONTRACTS,
        canonical_json_bytes as trusted_evidence_canonical_bytes,
        derive_platform_checks,
        validate_manifest as validate_trusted_evidence_manifest,
    )
    from verify_release_assets import publishable_assets

from polymarket.live_report_schema import validate_live_validation_report
from polymarket.live_reports import live_validation_report_promotion


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_LIVE_ATTEMPTS = 2
PUBLIC_LIVE_RETRY_DELAY_SECONDS = 1.0
EVIDENCE_MAX_AGE_DAYS = 30
EVIDENCE_MAX_FUTURE_SKEW_SECONDS = 5 * 60
PUBLIC_LIVE_REPORT_MAX_BYTES = 1024 * 1024
PUBLIC_LIVE_EVIDENCE_MAX_AGE_HOURS = 24
PUBLIC_LIVE_REPOSITORY = "Yunushan/market-sentinel"
PUBLIC_LIVE_WORKFLOW = ".github/workflows/ci.yml"
PUBLIC_LIVE_WORKFLOW_NAME = "CI"
PUBLIC_LIVE_JOB_NAME = "Public Polymarket live / GitHub-hosted"
PUBLIC_LIVE_TRUSTED_REF = "refs/heads/main"
PUBLIC_LIVE_REPORT_NAME = "public-polymarket-live.json"
REQUIRED_PUBLIC_LIVE_JOB_STEPS = (
    "Verify exact clean source before probe",
    "Probe reviewed public Polymarket endpoints",
    "Revalidate public-only evidence before attestation",
    "Reverify exact clean source after probe",
    "Attest exact public-live evidence file",
    "Upload public-live evidence",
)
REQUIRED_PUBLIC_LIVE_CHECKS = (
    "clob_time",
    "gamma_markets",
    "data_leaderboard",
    "bridge_supported_assets",
)
REQUIRED_PUBLIC_LIVE_SAFETY = {
    "dotenv_loaded": False,
    "credentials_present": False,
    "credential_variables_present": [],
    "authenticated_reads_attempted": False,
    "authenticated_user_websocket_attempted": False,
    "bridge_mutations_attempted": False,
    "funded_orders_attempted": False,
    "public_requests_read_only": True,
}
REQUIRED_PUBLIC_LIVE_EVIDENCE_FIELDS = frozenset(
    {
        "schema_version",
        "profile",
        "repository",
        "source_revision",
        "run_id",
        "run_attempt",
        "workflow",
        "workflow_name",
        "workflow_ref",
        "event",
        "runner_environment",
        "generated_at",
        "started_at",
        "completed_at",
    }
)
RELEASE_REPORT_MAX_BYTES = 256 * 1024
RELEASE_EVIDENCE_MAX_AGE_HOURS = 24
RELEASE_REPOSITORY = "Yunushan/market-sentinel"
RELEASE_WORKFLOW = ".github/workflows/release.yml"
RELEASE_WORKFLOW_NAME = "Release"
RELEASE_METADATA_JOB_NAME = "Release metadata"
RELEASE_PUBLISH_JOB_NAME = "Publish GitHub release"
RELEASE_TRUSTED_MAIN_REF = "refs/heads/main"
RELEASE_REPORT_NAME = "release-evidence.json"
RELEASE_REPORT_TYPE = "market-sentinel-release-evidence"
RELEASE_MAX_HISTORY = 99
DEPLOYMENT_REPORT_MAX_BYTES = 1024 * 1024
DEPLOYMENT_EVIDENCE_MAX_AGE_HOURS = 24
DEPLOYMENT_REPOSITORY = "Yunushan/market-sentinel"
DEPLOYMENT_WORKFLOW = ".github/workflows/deployment-evidence.yml"
DEPLOYMENT_WORKFLOW_NAME = "Production deployment evidence"
DEPLOYMENT_TRUSTED_MAIN_REF = "refs/heads/main"
DEPLOYMENT_REPORT_NAME = "deployment-evidence.json"
DEPLOYMENT_REPORT_TYPE = "market-sentinel-deployment-evidence"
DEPLOYMENT_PREPARE_JOB = "Prepare trusted deployment identity"
DEPLOYMENT_COLLECTOR_JOB = "Collect production deployment evidence"
DEPLOYMENT_EXTERNAL_PROBE_JOB = "Probe production externally from GitHub-hosted runner"
DEPLOYMENT_REVIEW_JOB = "Review and attest production deployment evidence"
DEPLOYMENT_COLLECTOR_LABELS = frozenset(
    {"self-hosted", "linux", "x64", "market-sentinel-production"}
)
REQUIRED_DEPLOYMENT_PREPARE_STEPS = (
    "Resolve exact release coordinates",
    "Verify release tag on protected main",
    "Download exact published release frontend asset",
    "Derive frontend digest from exact release asset",
    "Upload trusted deployment identity",
)
REQUIRED_DEPLOYMENT_COLLECTOR_STEPS = (
    "Download trusted deployment identity",
    "Collect raw production deployment evidence",
    "Upload raw production deployment evidence",
)
REQUIRED_DEPLOYMENT_EXTERNAL_PROBE_STEPS = (
    "Probe exact public deployment from GitHub-hosted runner",
    "Upload raw GitHub-hosted external probe",
)
REQUIRED_DEPLOYMENT_REVIEW_STEPS = (
    "Download trusted deployment identity",
    "Download raw production deployment evidence",
    "Download raw GitHub-hosted external probe",
    "Review raw report and bind exact release identity",
    "Attest exact deployment evidence",
    "Upload attested deployment evidence",
)
REQUIRED_RELEASE_METADATA_STEPS = (
    "Validate package version matches release tag",
    "Require release tag to resolve to workflow commit on protected main",
)
REQUIRED_RELEASE_PUBLISH_STEPS = (
    "Verify final release assets",
    "Attest release assets",
    "Reconcile and publish GitHub release",
    "Generate exact published release evidence",
    "Attest exact published release evidence",
    "Upload published release evidence",
)
REQUIRED_RELEASE_EVIDENCE_FIELDS = frozenset(
    {
        "repository",
        "source_revision",
        "run_id",
        "run_attempt",
        "workflow",
        "workflow_name",
        "workflow_ref",
        "source_ref",
        "event",
        "runner_environment",
        "job",
        "trusted_main_ref",
        "run_started_at",
        "generated_at",
        "published_at",
    }
)
REQUIRED_RELEASE_REPORT_FIELDS = frozenset(
    {"schema_version", "report_type", "release", "history", "evidence"}
)
REQUIRED_RELEASE_FIELDS = frozenset(
    {
        "id",
        "tag",
        "version",
        "target_commit",
        "draft",
        "prerelease",
        "published_at",
        "html_url",
        "assets",
    }
)
REQUIRED_RELEASE_ASSET_FIELDS = frozenset({"name", "size", "sha256"})
REQUIRED_RELEASE_HISTORY_FIELDS = frozenset(
    {"id", "tag", "target_commit", "prerelease", "published_at", "html_url"}
)

CATEGORY_WEIGHTS = {
    "architecture_scope": 18,
    "tests_correctness": 18,
    "security_safety": 17,
    "ci_cd_release": 17,
    "operations_recovery": 15,
    "platform_evidence": 10,
    "live_acceptance": 5,
}

REQUIRED_ARCHITECTURE_FILES = (
    "pyproject.toml",
    "README.md",
    "LICENSE",
    "verify.py",
    "market_sentinel_cli.py",
    "web_api.py",
    "market_adapters/catalog.py",
    "docs/GOAL_COMPLETION_AUDIT.md",
)

REQUIRED_SECURITY_FILES = (
    "SECURITY.md",
    "requirements.lock",
    "requirements-security.lock",
    ".github/workflows/ci.yml",
    ".github/workflows/governance-evidence.yml",
    ".github/workflows/release.yml",
    ".github/workflows/release-evidence-reconcile.yml",
    "scripts/trusted_readiness_evidence.py",
)

REQUIRED_CI_FILES = (
    ".github/actionlint.yaml",
    ".github/workflows/ci.yml",
    ".github/workflows/governance-evidence.yml",
    ".github/workflows/platform-evidence.yml",
    ".github/workflows/polymarket-evidence.yml",
    ".github/workflows/release.yml",
    ".github/workflows/release-evidence-reconcile.yml",
    "scripts/verify_release_provenance.py",
    "scripts/verify_release_assets.py",
    "scripts/generate_release_sbom.py",
    "scripts/generate_release_evidence.py",
    "scripts/trusted_readiness_evidence.py",
    "scripts/verify_python_dist_artifacts.py",
)

REQUIRED_OPERATIONS_FILES = (
    "docs/PRODUCTION_OPERATIONS.md",
    ".github/workflows/deployment-evidence.yml",
    "deploy/systemd/market-sentinel-web.service",
    "deploy/systemd/market-sentinel-health.service",
    "deploy/systemd/market-sentinel-backup.service",
    "scripts/verify_production_deployment.py",
    "scripts/review_deployment_evidence.py",
    "scripts/generate_deployment_evidence.py",
    "scripts/backup_state.py",
    "scripts/restore_state_backup.py",
)

REQUIRED_PLATFORM_FILES = (
    "docs/PLATFORM_SUPPORT.md",
    ".github/workflows/platform-evidence.yml",
    "scripts/trusted_readiness_evidence.py",
    "scripts/verify_platform_support.py",
    ".github/workflows/ci.yml",
)

REQUIRED_LIVE_FILES = (
    "polymarket/live_verification.py",
    "polymarket/credential_runbook.py",
    ".github/workflows/polymarket-evidence.yml",
    "scripts/trusted_readiness_evidence.py",
    "scripts/verify_polymarket_live.py",
    "docs/GOAL_COMPLETION_AUDIT.md",
)


def _paths_exist(relative_paths: tuple[str, ...]) -> bool:
    return all((ROOT / path).is_file() for path in relative_paths)


def _contains(path: str, fragments: tuple[str, ...]) -> bool:
    candidate = ROOT / path
    if not candidate.is_file():
        return False
    text = candidate.read_text(encoding="utf-8").casefold()
    return all(fragment.casefold() in text for fragment in fragments)


def _category(
    name: str,
    earned: int,
    basis: str,
    missing: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "earned": earned,
        "possible": CATEGORY_WEIGHTS[name],
        "basis": basis,
        "missing": list(missing or []),
    }


def _run_local_gates(full: bool) -> dict[str, Any]:
    command = [sys.executable, "-B", "verify.py", "--skip-pip-check"]
    if full:
        command.extend(("--frontend-build", "--frontend-live-smoke"))
    started = time.monotonic()
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=900 if full else 600,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "status": "fail",
            "command": command,
            "profile": "full" if full else "core",
            "duration_seconds": round(time.monotonic() - started, 3),
            "detail": type(exc).__name__,
        }
    return {
        "status": "pass" if result.returncode == 0 else "fail",
        "command": command,
        "profile": "full" if full else "core",
        "returncode": result.returncode,
        "duration_seconds": round(time.monotonic() - started, 3),
        "output": _captured_output_metadata(result.stdout, result.stderr),
    }


def _run_public_live() -> dict[str, Any]:
    command = [
        sys.executable,
        "-B",
        "scripts/verify_polymarket_live.py",
        "--skip-authenticated-read-checks",
        "--timeout",
        "15",
    ]
    last_result: dict[str, Any] = {"status": "fail", "command": command}
    for attempt in range(1, PUBLIC_LIVE_ATTEMPTS + 1):
        started = time.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix="market-sentinel-readiness-") as temporary:
                report_path = Path(temporary) / "public-live.json"
                result = subprocess.run(
                    [*command, "--report-file", str(report_path)],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=120,
                )
                result_metadata = {
                    "command": command,
                    "returncode": result.returncode,
                    "attempt": attempt,
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "output": _captured_output_metadata(result.stdout, result.stderr),
                }
                if report_path.is_file():
                    report_bytes = report_path.read_bytes()
                    report = json.loads(report_bytes)
                    public_checks = report.get("public_checks", {})
                    public_check_statuses: dict[str, str] = {}
                    if isinstance(public_checks, dict):
                        for name in REQUIRED_PUBLIC_LIVE_CHECKS:
                            check = public_checks.get(name)
                            status = check.get("status") if isinstance(check, dict) else None
                            public_check_statuses[name] = (
                                status if status in {"ok", "failed", "skipped"} else "missing_or_malformed"
                            )
                    exact_check_set = isinstance(public_checks, dict) and set(public_checks) == set(
                        REQUIRED_PUBLIC_LIVE_CHECKS
                    )
                    result_metadata["report"] = {
                        "sha256": hashlib.sha256(report_bytes).hexdigest(),
                        "public_check_statuses": public_check_statuses,
                        "observed_check_count": len(public_checks) if isinstance(public_checks, dict) else 0,
                        "exact_check_set": exact_check_set,
                    }
                    passed = (
                        result.returncode == 0
                        and report.get("ok") is True
                        and exact_check_set
                        and all(
                            isinstance(check, dict) and check.get("status") == "ok"
                            for check in public_checks.values()
                        )
                    )
                    if passed:
                        return {"status": "pass", **result_metadata}
                last_result = {"status": "fail", **result_metadata}
        except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
            last_result = {
                "status": "fail",
                "command": command,
                "detail": type(exc).__name__,
                "attempt": attempt,
                "duration_seconds": round(time.monotonic() - started, 3),
            }
        if attempt < PUBLIC_LIVE_ATTEMPTS:
            time.sleep(PUBLIC_LIVE_RETRY_DELAY_SECONDS)
    return last_result


class _DuplicateJsonKey(ValueError):
    """Raised when security-sensitive JSON contains duplicate object keys."""


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise _DuplicateJsonKey(f"duplicate JSON key: {key}")
        output[key] = value
    return output


def _reject_nonfinite_json(value: str) -> Any:
    raise ValueError(f"non-finite JSON number: {value}")


def _strict_json_bytes(raw: bytes, *, maximum_bytes: int = PUBLIC_LIVE_REPORT_MAX_BYTES) -> Any:
    """Parse bounded UTF-8 JSON while rejecting duplicates and non-finite numbers."""

    if not raw or len(raw) > maximum_bytes:
        raise ValueError("JSON input is empty or exceeds the size limit")
    text = raw.decode("utf-8")
    return json.loads(
        text,
        object_pairs_hook=_reject_duplicate_json_keys,
        parse_constant=_reject_nonfinite_json,
    )


def _recent_timestamp(
    value: Any,
    *,
    now: datetime,
    maximum_age_hours: int = PUBLIC_LIVE_EVIDENCE_MAX_AGE_HOURS,
) -> tuple[datetime | None, str]:
    if not _is_nonblank_string(value):
        return None, "must be a non-empty ISO-8601 string"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None, "must be valid ISO-8601"
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None, "must include a timezone"
    normalized = parsed.astimezone(timezone.utc)
    age = now.astimezone(timezone.utc) - normalized
    if age < -timedelta(seconds=EVIDENCE_MAX_FUTURE_SKEW_SECONDS):
        return None, "is in the future"
    if age > timedelta(hours=maximum_age_hours):
        return None, f"is older than {maximum_age_hours} hours"
    return normalized, ""


def _run_gh_json(command: list[str], *, timeout: int = 30) -> tuple[Any | None, str]:
    """Run a fixed-shape GitHub CLI query and parse its bounded JSON response."""

    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=False,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"{type(exc).__name__} while invoking GitHub CLI"
    if result.returncode != 0:
        return None, "GitHub CLI verification failed"
    stdout = result.stdout if isinstance(result.stdout, bytes) else str(result.stdout).encode("utf-8")
    try:
        payload = _strict_json_bytes(stdout)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError, _DuplicateJsonKey):
        return None, "GitHub CLI returned malformed or oversized JSON"
    return payload, ""


def _attestation_result_matches(
    item: Any,
    *,
    report_hash: str,
    revision: str,
    workflow_ref: str,
    run_id: int,
    run_attempt: int,
    now: datetime,
    subject_name: str = PUBLIC_LIVE_REPORT_NAME,
    repository: str = PUBLIC_LIVE_REPOSITORY,
    workflow_path: str = PUBLIC_LIVE_WORKFLOW,
    event: str = "workflow_dispatch",
) -> bool:
    """Enforce certificate-backed identity and exact SLSA subject/run bindings."""

    if not isinstance(item, dict) or not isinstance(item.get("attestation"), dict) or not item["attestation"]:
        return False
    verification = item.get("verificationResult")
    if not isinstance(verification, dict):
        return False
    if (
        verification.get("mediaType")
        != "application/vnd.dev.sigstore.verificationresult+json;version=0.1"
    ):
        return False
    statement = verification.get("statement")
    signature = verification.get("signature")
    if not isinstance(statement, dict) or not isinstance(signature, dict):
        return False
    if (
        statement.get("_type") != "https://in-toto.io/Statement/v1"
        or statement.get("predicateType") != "https://slsa.dev/provenance/v1"
    ):
        return False
    subjects = statement.get("subject")
    if not isinstance(subjects, list) or len(subjects) != 1 or not isinstance(subjects[0], dict):
        return False
    subject = subjects[0]
    if subject.get("name") != subject_name:
        return False
    digest = subject.get("digest")
    if not isinstance(digest, dict) or set(digest) != {"sha256"} or digest.get("sha256") != report_hash:
        return False

    certificate = signature.get("certificate")
    if not isinstance(certificate, dict):
        return False
    if workflow_ref.count("@") != 1 or repository.count("/") != 1:
        return False
    ref = workflow_ref.split("@", 1)[1]
    cert_workflow_uri = f"https://github.com/{workflow_ref}"
    repository_uri = f"https://github.com/{repository}"
    invocation_uri = f"https://github.com/{repository}/actions/runs/{run_id}/attempts/{run_attempt}"
    owner = repository.split("/", 1)[0]
    certificate_contract = {
        "subjectAlternativeName": cert_workflow_uri,
        "issuer": "https://token.actions.githubusercontent.com",
        "buildSignerURI": cert_workflow_uri,
        "buildSignerDigest": revision,
        "runnerEnvironment": "github-hosted",
        "sourceRepositoryURI": repository_uri,
        "sourceRepositoryDigest": revision,
        "sourceRepositoryRef": ref,
        "sourceRepositoryOwnerURI": f"https://github.com/{owner}",
        "buildConfigURI": cert_workflow_uri,
        "buildConfigDigest": revision,
        "buildTrigger": event,
        "runInvocationURI": invocation_uri,
        "sourceRepositoryVisibilityAtSigning": "public",
    }
    if any(certificate.get(key) != value for key, value in certificate_contract.items()):
        return False

    timestamps = verification.get("verifiedTimestamps")
    if not isinstance(timestamps, list) or not timestamps:
        return False
    for timestamp in timestamps:
        if not isinstance(timestamp, dict):
            return False
        parsed, _ = _recent_timestamp(timestamp.get("timestamp"), now=now)
        if parsed is None:
            return False

    predicate = statement.get("predicate")
    if not isinstance(predicate, dict):
        return False
    build_definition = predicate.get("buildDefinition")
    run_details = predicate.get("runDetails")
    if not isinstance(build_definition, dict) or not isinstance(run_details, dict):
        return False
    if build_definition.get("buildType") != "https://actions.github.io/buildtypes/workflow/v1":
        return False
    external = build_definition.get("externalParameters")
    internal = build_definition.get("internalParameters")
    dependencies = build_definition.get("resolvedDependencies")
    workflow = external.get("workflow") if isinstance(external, dict) else None
    github = internal.get("github") if isinstance(internal, dict) else None
    if not isinstance(workflow, dict) or not isinstance(github, dict) or not isinstance(dependencies, list):
        return False
    if workflow != {"path": workflow_path, "ref": ref, "repository": repository_uri}:
        return False
    if github.get("event_name") != event or github.get("runner_environment") != "github-hosted":
        return False
    dependency_uri = f"git+{repository_uri}@{ref}"
    matching_dependencies = [
        dependency
        for dependency in dependencies
        if isinstance(dependency, dict)
        and dependency.get("uri") == dependency_uri
        and dependency.get("digest") == {"gitCommit": revision}
    ]
    if len(matching_dependencies) != 1:
        return False
    builder = run_details.get("builder")
    metadata = run_details.get("metadata")
    if not isinstance(builder, dict) or builder.get("id") != cert_workflow_uri:
        return False
    if not isinstance(metadata, dict) or metadata.get("invocationId") != invocation_uri:
        return False
    return True


def _attested_public_live_report(
    path_value: str | None,
    *,
    expected_revision: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate a GitHub-hosted, artifact-attested, public-only live report.

    The report is useful only when its embedded identity, GitHub Actions run,
    and Sigstore-backed artifact attestation all bind to the exact clean HEAD.
    Every failure is closed and described without retaining command output.
    """

    if not _is_nonblank_string(path_value):
        return {
            "status": "not_run",
            "mode": "attested",
            "detail": "Provide --public-live-report with a fresh GitHub-attested public-only report.",
        }
    if not _COMMIT_RE.fullmatch(expected_revision or ""):
        return {
            "status": "fail",
            "mode": "attested",
            "detail": "Attested public-live evidence requires an exact clean repository revision.",
        }

    path = Path(path_value).expanduser()
    try:
        if not path.is_file():
            raise OSError("not a regular file")
        size = path.stat().st_size
        if size <= 0 or size > PUBLIC_LIVE_REPORT_MAX_BYTES:
            raise ValueError("invalid report size")
        raw = path.read_bytes()
        payload = _strict_json_bytes(raw)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError, _DuplicateJsonKey):
        return {
            "status": "fail",
            "mode": "attested",
            "detail": "Public-live report is unreadable, malformed, duplicated-key, non-finite, or oversized JSON.",
        }
    if not isinstance(payload, dict):
        return {"status": "fail", "mode": "attested", "detail": "Public-live report must be a JSON object."}

    expected_top_level = {"ok", "mode", "market_id", "public_checks", "safety", "evidence"}
    if set(payload) != expected_top_level:
        return {
            "status": "fail",
            "mode": "attested",
            "detail": "Public-live report does not match the exact reviewed top-level schema.",
        }
    if payload.get("ok") is not True or payload.get("mode") != "public_only" or payload.get("market_id") != "polymarket":
        return {
            "status": "fail",
            "mode": "attested",
            "detail": "Public-live report must be a successful public_only Polymarket report.",
        }

    public_checks = payload.get("public_checks")
    if not isinstance(public_checks, dict) or set(public_checks) != set(REQUIRED_PUBLIC_LIVE_CHECKS):
        return {
            "status": "fail",
            "mode": "attested",
            "detail": "Public-live report must contain exactly the four reviewed public checks.",
        }
    if any(not isinstance(public_checks[name], dict) or public_checks[name].get("status") != "ok" for name in REQUIRED_PUBLIC_LIVE_CHECKS):
        return {
            "status": "fail",
            "mode": "attested",
            "detail": "Every reviewed public endpoint check must have status=ok.",
        }

    safety = payload.get("safety")
    if not isinstance(safety, dict) or set(safety) != set(REQUIRED_PUBLIC_LIVE_SAFETY):
        return {
            "status": "fail",
            "mode": "attested",
            "detail": "Public-live report safety metadata does not match the public-only contract.",
        }
    if any(type(safety[key]) is not type(expected) or safety[key] != expected for key, expected in REQUIRED_PUBLIC_LIVE_SAFETY.items()):
        return {
            "status": "fail",
            "mode": "attested",
            "detail": "Public-live report attempted credentials, authenticated reads, bridge mutations, or funded actions.",
        }

    evidence = payload.get("evidence")
    if not isinstance(evidence, dict) or set(evidence) != REQUIRED_PUBLIC_LIVE_EVIDENCE_FIELDS:
        return {
            "status": "fail",
            "mode": "attested",
            "detail": "Public-live report evidence metadata does not match the exact reviewed schema.",
        }
    exact_metadata = {
        "schema_version": 1,
        "profile": "public-only",
        "repository": PUBLIC_LIVE_REPOSITORY,
        "source_revision": expected_revision,
        "workflow": PUBLIC_LIVE_WORKFLOW,
        "workflow_name": PUBLIC_LIVE_WORKFLOW_NAME,
        "event": "workflow_dispatch",
        "runner_environment": "github-hosted",
    }
    if any(type(evidence.get(key)) is not type(expected) or evidence.get(key) != expected for key, expected in exact_metadata.items()):
        return {
            "status": "fail",
            "mode": "attested",
            "detail": "Public-live report repository, revision, workflow, event, runner, or profile identity is invalid.",
        }
    run_id = evidence.get("run_id")
    run_attempt = evidence.get("run_attempt")
    if type(run_id) is not int or run_id <= 0 or type(run_attempt) is not int or run_attempt <= 0:
        return {
            "status": "fail",
            "mode": "attested",
            "detail": "Public-live report run_id and run_attempt must be positive integers.",
        }
    workflow_ref = evidence.get("workflow_ref")
    trusted_workflow_ref = (
        f"{PUBLIC_LIVE_REPOSITORY}/{PUBLIC_LIVE_WORKFLOW}@{PUBLIC_LIVE_TRUSTED_REF}"
    )
    if workflow_ref != trusted_workflow_ref:
        return {
            "status": "fail",
            "mode": "attested",
            "detail": "Public-live report workflow_ref is invalid.",
        }

    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise ValueError("public-live evidence validation clock must include a timezone")
    evidence_times: dict[str, datetime] = {}
    for field in ("started_at", "generated_at", "completed_at"):
        parsed, error = _recent_timestamp(evidence.get(field), now=current_time)
        if parsed is None:
            return {
                "status": "fail",
                "mode": "attested",
                "detail": f"Public-live evidence {field} {error}.",
            }
        evidence_times[field] = parsed
    if not (
        evidence_times["started_at"]
        <= evidence_times["completed_at"]
        <= evidence_times["generated_at"]
    ):
        return {
            "status": "fail",
            "mode": "attested",
            "detail": "Public-live evidence timestamps are not monotonically ordered.",
        }

    report_hash = hashlib.sha256(raw).hexdigest()
    with tempfile.TemporaryDirectory(prefix="market-sentinel-attestation-") as temporary:
        attestation_target = Path(temporary) / PUBLIC_LIVE_REPORT_NAME
        attestation_target.write_bytes(raw)
        attestation, attestation_error = _run_gh_json(
            [
                "gh",
                "attestation",
                "verify",
                str(attestation_target),
                "--repo",
                PUBLIC_LIVE_REPOSITORY,
                # Keep attestation lookup broad and enforce the complete
                # signer/source/run identity below from the signed result.
                # gh versions differ in which verifier-filter combinations
                # they accept; passing only the repository avoids rejecting
                # a valid Sigstore attestation before our strict matcher runs.
                "--format",
                "json",
            ],
            timeout=60,
        )
    matching_attestations = (
        [
            item
            for item in attestation
            if _attestation_result_matches(
                item,
                report_hash=report_hash,
                revision=expected_revision,
                workflow_ref=workflow_ref,
                run_id=run_id,
                run_attempt=run_attempt,
                now=current_time,
            )
        ]
        if isinstance(attestation, list)
        else []
    )
    if len(matching_attestations) != 1:
        return {
            "status": "fail",
            "mode": "attested",
            "detail": (
                "Artifact attestation was not accepted: "
                f"{attestation_error or 'expected exactly one certificate-bound matching result'}."
            ),
            "report": {"sha256": report_hash},
        }

    api_payload, api_error = _run_gh_json(
        [
            "gh",
            "api",
            "--method",
            "GET",
            f"repos/{PUBLIC_LIVE_REPOSITORY}/actions/runs/{run_id}",
        ]
    )
    if not isinstance(api_payload, dict):
        return {
            "status": "fail",
            "mode": "attested",
            "detail": f"GitHub Actions run could not be verified: {api_error or 'malformed run response'}.",
            "report": {"sha256": report_hash},
        }
    expected_run_fields = {
        "id": run_id,
        "head_sha": expected_revision,
        "name": PUBLIC_LIVE_WORKFLOW_NAME,
        "path": PUBLIC_LIVE_WORKFLOW,
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "success",
        "run_attempt": run_attempt,
        "head_branch": "main",
    }
    if any(type(api_payload.get(key)) is not type(expected) or api_payload.get(key) != expected for key, expected in expected_run_fields.items()):
        return {
            "status": "fail",
            "mode": "attested",
            "detail": "GitHub Actions run identity, revision, workflow, event, attempt, or conclusion does not match the report.",
            "report": {"sha256": report_hash},
        }
    head_repository = api_payload.get("head_repository")
    if not isinstance(head_repository, dict) or head_repository.get("full_name") != PUBLIC_LIVE_REPOSITORY:
        return {
            "status": "fail",
            "mode": "attested",
            "detail": "GitHub Actions run head repository does not match the trusted repository.",
            "report": {"sha256": report_hash},
        }
    api_times: dict[str, datetime] = {}
    for field in ("created_at", "run_started_at", "updated_at"):
        parsed, error = _recent_timestamp(api_payload.get(field), now=current_time)
        if parsed is None:
            return {
                "status": "fail",
                "mode": "attested",
                "detail": f"GitHub Actions run {field} {error}.",
                "report": {"sha256": report_hash},
            }
        api_times[field] = parsed
    if not api_times["created_at"] <= api_times["run_started_at"] <= api_times["updated_at"]:
        return {
            "status": "fail",
            "mode": "attested",
            "detail": "GitHub Actions run timestamps are not monotonically ordered.",
            "report": {"sha256": report_hash},
        }
    skew = timedelta(seconds=EVIDENCE_MAX_FUTURE_SKEW_SECONDS)
    if (
        evidence_times["started_at"] < api_times["run_started_at"] - skew
        or evidence_times["generated_at"] > api_times["updated_at"] + skew
    ):
        return {
            "status": "fail",
            "mode": "attested",
            "detail": "Public-live evidence timestamps fall outside the trusted GitHub Actions run window.",
            "report": {"sha256": report_hash},
        }

    jobs_payload, jobs_error = _run_gh_json(
        [
            "gh",
            "api",
            "--method",
            "GET",
            f"repos/{PUBLIC_LIVE_REPOSITORY}/actions/runs/{run_id}/jobs?filter=latest&per_page=100",
        ]
    )
    jobs = jobs_payload.get("jobs") if isinstance(jobs_payload, dict) else None
    jobs_total = jobs_payload.get("total_count") if isinstance(jobs_payload, dict) else None
    if (
        not isinstance(jobs, list)
        or type(jobs_total) is not int
        or jobs_total != len(jobs)
        or jobs_total > 100
    ):
        return {
            "status": "fail",
            "mode": "attested",
            "detail": f"GitHub Actions public job could not be verified: {jobs_error or 'malformed jobs response'}.",
            "report": {"sha256": report_hash},
        }
    matching_jobs = [job for job in jobs if isinstance(job, dict) and job.get("name") == PUBLIC_LIVE_JOB_NAME]
    if len(matching_jobs) != 1:
        return {
            "status": "fail",
            "mode": "attested",
            "detail": "GitHub Actions run must contain exactly one reviewed public-live job.",
            "report": {"sha256": report_hash},
        }
    public_job = matching_jobs[0]
    labels = public_job.get("labels")
    if (
        public_job.get("status") != "completed"
        or public_job.get("conclusion") != "success"
        or not isinstance(labels, list)
        or "ubuntu-24.04" not in labels
        or "self-hosted" in labels
    ):
        return {
            "status": "fail",
            "mode": "attested",
            "detail": "GitHub Actions public-live job was not a successful GitHub-hosted ubuntu-24.04 job.",
            "report": {"sha256": report_hash},
        }
    steps = public_job.get("steps")
    if not isinstance(steps, list):
        return {
            "status": "fail",
            "mode": "attested",
            "detail": "GitHub Actions public-live job is missing reviewed step results.",
            "report": {"sha256": report_hash},
        }
    step_names = [step.get("name") for step in steps if isinstance(step, dict)]
    if len(step_names) != len(steps) or len(step_names) != len(set(step_names)):
        return {
            "status": "fail",
            "mode": "attested",
            "detail": "GitHub Actions public-live job contains malformed or duplicate step names.",
            "report": {"sha256": report_hash},
        }
    required_step_results = {
        step.get("name"): (step.get("status"), step.get("conclusion"))
        for step in steps
        if isinstance(step, dict) and step.get("name") in REQUIRED_PUBLIC_LIVE_JOB_STEPS
    }
    if set(required_step_results) != set(REQUIRED_PUBLIC_LIVE_JOB_STEPS) or any(
        result != ("completed", "success") for result in required_step_results.values()
    ):
        return {
            "status": "fail",
            "mode": "attested",
            "detail": "GitHub Actions public-live job did not successfully complete every reviewed safety step.",
            "report": {"sha256": report_hash},
        }
    job_times: dict[str, datetime] = {}
    for field in ("started_at", "completed_at"):
        parsed, error = _recent_timestamp(public_job.get(field), now=current_time)
        if parsed is None:
            return {
                "status": "fail",
                "mode": "attested",
                "detail": f"GitHub Actions public-live job {field} {error}.",
                "report": {"sha256": report_hash},
            }
        job_times[field] = parsed
    if (
        job_times["started_at"] > job_times["completed_at"]
        or evidence_times["started_at"] < job_times["started_at"] - skew
        or evidence_times["generated_at"] > job_times["completed_at"] + skew
    ):
        return {
            "status": "fail",
            "mode": "attested",
            "detail": "Public-live evidence timestamps fall outside the reviewed public job window.",
            "report": {"sha256": report_hash},
        }

    return {
        "status": "pass",
        "mode": "attested",
        "detail": "Fresh public-only report, GitHub Actions run, and artifact attestation were verified.",
        "report": {
            "sha256": report_hash,
            "public_check_statuses": {
                name: public_checks[name]["status"] for name in REQUIRED_PUBLIC_LIVE_CHECKS
            },
            "exact_check_set": True,
        },
        "evidence": {
            "repository": PUBLIC_LIVE_REPOSITORY,
            "source_revision": expected_revision,
            "run_id": run_id,
            "run_attempt": run_attempt,
            "workflow": PUBLIC_LIVE_WORKFLOW,
            "event": "workflow_dispatch",
            "runner_environment": "github-hosted",
            "attestation": "verified",
            "github_run": "verified",
            "github_job": "verified",
        },
    }


def _release_evidence_result(
    status: str,
    detail: str,
    *,
    report_hash: str = "",
) -> dict[str, Any]:
    result: dict[str, Any] = {"status": status, "mode": "attested", "detail": detail}
    if report_hash:
        result["report"] = {"sha256": report_hash}
    return result


def _release_timestamp(value: Any) -> tuple[str | None, datetime | None, str]:
    """Return one timezone-aware timestamp in the generator's canonical UTC form."""

    if not _is_nonblank_string(value):
        return None, None, "must be a non-empty ISO-8601 string"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None, None, "must be valid ISO-8601"
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None, None, "must include a timezone"
    normalized = parsed.astimezone(timezone.utc)
    canonical = normalized.isoformat().replace("+00:00", "Z")
    return canonical, normalized, ""


def _normalized_live_release_history(payload: Any) -> tuple[list[dict[str, Any]] | None, str]:
    """Normalize one complete, bounded GitHub releases API page."""

    if not isinstance(payload, list) or not payload:
        return None, "published release history is not a non-empty list"
    if len(payload) > RELEASE_MAX_HISTORY:
        return None, "published release history exceeds the single-page proof limit"

    history: list[dict[str, Any]] = []
    release_ids: set[int] = set()
    tags: set[str] = set()
    for index, row in enumerate(payload):
        if not isinstance(row, dict):
            return None, f"published release history entry {index} is malformed"
        draft = row.get("draft")
        if type(draft) is not bool:
            return None, f"published release history entry {index} has invalid draft state"
        if draft:
            continue
        release_id = row.get("id")
        tag = row.get("tag_name")
        target = row.get("target_commitish")
        prerelease = row.get("prerelease")
        html_url = row.get("html_url")
        if type(release_id) is not int or release_id <= 0:
            return None, f"published release history entry {index} has an invalid id"
        if not _is_nonblank_string(tag):
            return None, f"published release history entry {index} has an invalid tag"
        try:
            normalize_release_tag(tag)
        except ValueError:
            return None, f"published release history entry {index} has an unsupported tag"
        if not isinstance(target, str) or not _COMMIT_RE.fullmatch(target):
            return None, f"published release history entry {index} has an invalid target commit"
        if type(prerelease) is not bool:
            return None, f"published release history entry {index} has invalid prerelease state"
        expected_url = f"https://github.com/{RELEASE_REPOSITORY}/releases/tag/{tag}"
        if html_url != expected_url:
            return None, f"published release history entry {index} has an invalid URL"
        published_at, _, timestamp_error = _release_timestamp(row.get("published_at"))
        if published_at is None:
            return None, f"published release history entry {index} timestamp {timestamp_error}"
        if release_id in release_ids or tag in tags:
            return None, "published release history contains duplicate ids or tags"
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

    if not history:
        return None, "published release history contains no published releases"
    return sorted(history, key=lambda entry: (entry["published_at"], entry["id"], entry["tag"])), ""


def _release_job_steps_are_trusted(job: dict[str, Any], required: tuple[str, ...]) -> bool:
    steps = job.get("steps")
    if not isinstance(steps, list):
        return False
    names = [step.get("name") for step in steps if isinstance(step, dict)]
    if len(names) != len(steps) or len(names) != len(set(names)):
        return False
    indexes: list[int] = []
    for name in required:
        try:
            index = names.index(name)
        except ValueError:
            return False
        step = steps[index]
        if step.get("status") != "completed" or step.get("conclusion") != "success":
            return False
        indexes.append(index)
    return indexes == sorted(indexes)


def _resolve_release_tag_commit(tag: str) -> tuple[str | None, str]:
    """Resolve a lightweight or bounded annotated Git tag through the live API."""

    reference, error = _run_gh_json(
        [
            "gh",
            "api",
            "--method",
            "GET",
            f"repos/{RELEASE_REPOSITORY}/git/ref/tags/{tag}",
        ]
    )
    object_payload = reference.get("object") if isinstance(reference, dict) else None
    if not isinstance(object_payload, dict):
        return None, error or "malformed Git tag reference"
    seen: set[str] = set()
    for _ in range(5):
        object_type = object_payload.get("type")
        object_sha = object_payload.get("sha")
        if not isinstance(object_sha, str) or not _COMMIT_RE.fullmatch(object_sha):
            return None, "Git tag object has an invalid SHA"
        if object_sha in seen:
            return None, "Git tag object chain contains a cycle"
        seen.add(object_sha)
        if object_type == "commit":
            return object_sha, ""
        if object_type != "tag":
            return None, "Git tag does not resolve to a commit"
        annotated, annotated_error = _run_gh_json(
            [
                "gh",
                "api",
                "--method",
                "GET",
                f"repos/{RELEASE_REPOSITORY}/git/tags/{object_sha}",
            ]
        )
        object_payload = annotated.get("object") if isinstance(annotated, dict) else None
        if not isinstance(object_payload, dict):
            return None, annotated_error or "malformed annotated Git tag"
    return None, "Git tag object chain exceeds the supported depth"


def _attested_release_report(
    path_value: str | None,
    *,
    expected_revision: str,
    expected_version: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate exact report bytes plus current release, workflow, and tag state."""

    if not _is_nonblank_string(path_value):
        return _release_evidence_result(
            "not_run",
            "Attested release evidence is required: provide a fresh GitHub-attested release-evidence.json report.",
        )
    if not _COMMIT_RE.fullmatch(expected_revision or ""):
        return _release_evidence_result(
            "fail",
            "Attested release evidence requires an exact clean repository revision.",
        )
    try:
        canonical_version = normalize_release_version(expected_version)
    except ValueError:
        return _release_evidence_result(
            "fail",
            "Attested release evidence requires a supported canonical project version.",
        )
    if canonical_version != expected_version:
        return _release_evidence_result(
            "fail",
            "Attested release evidence requires the canonical project version.",
        )

    path = Path(path_value).expanduser()
    try:
        if not path.is_file():
            raise OSError("not a regular file")
        raw = path.read_bytes()
        payload = _strict_json_bytes(raw, maximum_bytes=RELEASE_REPORT_MAX_BYTES)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError, _DuplicateJsonKey):
        return _release_evidence_result(
            "fail",
            "Release report is unreadable, malformed, duplicated-key, non-finite, or oversized JSON.",
        )
    report_hash = hashlib.sha256(raw).hexdigest()
    if not isinstance(payload, dict) or set(payload) != REQUIRED_RELEASE_REPORT_FIELDS:
        return _release_evidence_result(
            "fail",
            "Release report does not match the exact reviewed top-level schema.",
            report_hash=report_hash,
        )
    canonical_bytes = (
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    if raw != canonical_bytes:
        return _release_evidence_result(
            "fail",
            "Release report bytes are not in the deterministic canonical form.",
            report_hash=report_hash,
        )
    if payload.get("schema_version") != 1 or payload.get("report_type") != RELEASE_REPORT_TYPE:
        return _release_evidence_result(
            "fail",
            "Release report schema version or type is invalid.",
            report_hash=report_hash,
        )

    release = payload.get("release")
    if not isinstance(release, dict) or set(release) != REQUIRED_RELEASE_FIELDS:
        return _release_evidence_result(
            "fail",
            "Release report release metadata does not match the exact reviewed schema.",
            report_hash=report_hash,
        )
    tag = f"v{canonical_version}"
    release_id = release.get("id")
    exact_release_fields = {
        "tag": tag,
        "version": canonical_version,
        "target_commit": expected_revision,
        "draft": False,
        "prerelease": False,
        "html_url": f"https://github.com/{RELEASE_REPOSITORY}/releases/tag/{tag}",
    }
    if type(release_id) is not int or release_id <= 0 or any(
        type(release.get(key)) is not type(expected) or release.get(key) != expected
        for key, expected in exact_release_fields.items()
    ):
        return _release_evidence_result(
            "fail",
            "Release report tag, version, target, state, URL, or id is invalid.",
            report_hash=report_hash,
        )
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise ValueError("release evidence validation clock must include a timezone")
    skew = timedelta(seconds=EVIDENCE_MAX_FUTURE_SKEW_SECONDS)
    published_at, published_time, published_error = _release_timestamp(release.get("published_at"))
    if published_at != release.get("published_at") or published_time is None:
        return _release_evidence_result(
            "fail",
            f"Release report published_at {published_error or 'is not canonical UTC'}.",
            report_hash=report_hash,
        )
    if published_time > current_time.astimezone(timezone.utc) + skew:
        return _release_evidence_result(
            "fail",
            "Release report published_at is in the future.",
            report_hash=report_hash,
        )

    assets = release.get("assets")
    expected_names = publishable_assets(canonical_version, tag)
    if not isinstance(assets, list) or len(assets) != len(expected_names):
        return _release_evidence_result(
            "fail",
            "Release report does not contain the complete publishable asset inventory.",
            report_hash=report_hash,
        )
    asset_map: dict[str, dict[str, Any]] = {}
    for asset in assets:
        if not isinstance(asset, dict) or set(asset) != REQUIRED_RELEASE_ASSET_FIELDS:
            return _release_evidence_result(
                "fail",
                "Release report contains malformed asset metadata.",
                report_hash=report_hash,
            )
        name = asset.get("name")
        size = asset.get("size")
        digest = asset.get("sha256")
        if (
            not isinstance(name, str)
            or name not in expected_names
            or name in asset_map
            or type(size) is not int
            or size <= 0
            or not isinstance(digest, str)
            or not _HASH_RE.fullmatch(digest)
        ):
            return _release_evidence_result(
                "fail",
                "Release report asset name, size, or SHA-256 is invalid.",
                report_hash=report_hash,
            )
        asset_map[name] = asset
    if set(asset_map) != expected_names or [asset["name"] for asset in assets] != sorted(expected_names):
        return _release_evidence_result(
            "fail",
            "Release report asset names must be exact, unique, and sorted.",
            report_hash=report_hash,
        )

    history = payload.get("history")
    if not isinstance(history, list) or not history or len(history) > RELEASE_MAX_HISTORY:
        return _release_evidence_result(
            "fail",
            "Release report history is empty, malformed, or exceeds the proof limit.",
            report_hash=report_hash,
        )
    history_ids: set[int] = set()
    history_tags: set[str] = set()
    for index, entry in enumerate(history):
        if not isinstance(entry, dict) or set(entry) != REQUIRED_RELEASE_HISTORY_FIELDS:
            return _release_evidence_result(
                "fail",
                f"Release report history entry {index} is malformed.",
                report_hash=report_hash,
            )
        entry_id = entry.get("id")
        entry_tag = entry.get("tag")
        entry_target = entry.get("target_commit")
        entry_prerelease = entry.get("prerelease")
        if type(entry_id) is not int or entry_id <= 0 or not _is_nonblank_string(entry_tag):
            return _release_evidence_result(
                "fail",
                f"Release report history entry {index} has an invalid id or tag.",
                report_hash=report_hash,
            )
        try:
            normalize_release_tag(entry_tag)
        except ValueError:
            return _release_evidence_result(
                "fail",
                f"Release report history entry {index} has an unsupported tag.",
                report_hash=report_hash,
            )
        entry_url = f"https://github.com/{RELEASE_REPOSITORY}/releases/tag/{entry_tag}"
        entry_published, _, entry_timestamp_error = _release_timestamp(entry.get("published_at"))
        if (
            not isinstance(entry_target, str)
            or not _COMMIT_RE.fullmatch(entry_target)
            or type(entry_prerelease) is not bool
            or entry.get("html_url") != entry_url
            or entry_published != entry.get("published_at")
        ):
            return _release_evidence_result(
                "fail",
                f"Release report history entry {index} target, state, URL, or timestamp "
                f"{entry_timestamp_error or 'is invalid'}.",
                report_hash=report_hash,
            )
        if entry_id in history_ids or entry_tag in history_tags:
            return _release_evidence_result(
                "fail",
                "Release report history contains duplicate ids or tags.",
                report_hash=report_hash,
            )
        history_ids.add(entry_id)
        history_tags.add(entry_tag)
    sorted_history = sorted(
        history,
        key=lambda entry: (entry["published_at"], entry["id"], entry["tag"]),
    )
    if history != sorted_history:
        return _release_evidence_result(
            "fail",
            "Release report history is not in deterministic chronological order.",
            report_hash=report_hash,
        )
    current_history = [entry for entry in history if entry["id"] == release_id]
    if (
        len(current_history) != 1
        or history[-1]["id"] != release_id
        or current_history[0]
        != {
            "id": release_id,
            "tag": tag,
            "target_commit": expected_revision,
            "prerelease": False,
            "published_at": published_at,
            "html_url": release["html_url"],
        }
    ):
        return _release_evidence_result(
            "fail",
            "Release report history does not end in the exact current stable release.",
            report_hash=report_hash,
        )

    evidence = payload.get("evidence")
    if not isinstance(evidence, dict) or set(evidence) != REQUIRED_RELEASE_EVIDENCE_FIELDS:
        return _release_evidence_result(
            "fail",
            "Release report evidence identity does not match the exact reviewed schema.",
            report_hash=report_hash,
        )
    run_id = evidence.get("run_id")
    run_attempt = evidence.get("run_attempt")
    event = evidence.get("event")
    source_ref = evidence.get("source_ref")
    if type(run_id) is not int or run_id <= 0 or type(run_attempt) is not int or run_attempt <= 0:
        return _release_evidence_result(
            "fail",
            "Release report run id and attempt must be positive integers.",
            report_hash=report_hash,
        )
    allowed_event_ref = (
        event == "push" and source_ref == f"refs/tags/{tag}"
    ) or (
        event == "workflow_dispatch" and source_ref in {f"refs/tags/{tag}", RELEASE_TRUSTED_MAIN_REF}
    )
    workflow_ref = f"{RELEASE_REPOSITORY}/{RELEASE_WORKFLOW}@{source_ref}"
    exact_evidence_fields = {
        "repository": RELEASE_REPOSITORY,
        "source_revision": expected_revision,
        "workflow": RELEASE_WORKFLOW,
        "workflow_name": RELEASE_WORKFLOW_NAME,
        "workflow_ref": workflow_ref,
        "runner_environment": "github-hosted",
        "job": RELEASE_PUBLISH_JOB_NAME,
        "trusted_main_ref": RELEASE_TRUSTED_MAIN_REF,
        "published_at": published_at,
    }
    if not allowed_event_ref or any(
        type(evidence.get(key)) is not type(expected) or evidence.get(key) != expected
        for key, expected in exact_evidence_fields.items()
    ):
        return _release_evidence_result(
            "fail",
            "Release report repository, revision, workflow, ref, event, runner, job, or tag binding is invalid.",
            report_hash=report_hash,
        )
    run_started_at, run_started_time, run_started_error = _release_timestamp(
        evidence.get("run_started_at")
    )
    generated_at, generated_time, generated_error = _release_timestamp(
        evidence.get("generated_at")
    )
    recent_run_started, recent_run_error = _recent_timestamp(
        run_started_at,
        now=current_time,
        maximum_age_hours=RELEASE_EVIDENCE_MAX_AGE_HOURS,
    )
    recent_generated, recent_generated_error = _recent_timestamp(
        generated_at,
        now=current_time,
        maximum_age_hours=RELEASE_EVIDENCE_MAX_AGE_HOURS,
    )
    if (
        run_started_at != evidence.get("run_started_at")
        or run_started_time is None
        or recent_run_started is None
    ):
        return _release_evidence_result(
            "fail",
            f"Release report run_started_at "
            f"{run_started_error or recent_run_error or 'is not canonical UTC'}.",
            report_hash=report_hash,
        )
    if (
        generated_at != evidence.get("generated_at")
        or generated_time is None
        or recent_generated is None
    ):
        return _release_evidence_result(
            "fail",
            f"Release report generated_at "
            f"{generated_error or recent_generated_error or 'is not canonical UTC'}.",
            report_hash=report_hash,
        )
    if generated_time < run_started_time - skew or generated_time < published_time - skew:
        return _release_evidence_result(
            "fail",
            "Release evidence generation predates the workflow run or published release.",
            report_hash=report_hash,
        )

    with tempfile.TemporaryDirectory(prefix="market-sentinel-release-attestation-") as temporary:
        attestation_target = Path(temporary) / RELEASE_REPORT_NAME
        attestation_target.write_bytes(raw)
        attestation, attestation_error = _run_gh_json(
            [
                "gh",
                "attestation",
                "verify",
                str(attestation_target),
                "--repo",
                RELEASE_REPOSITORY,
                # The signed result is checked exhaustively by
                # _attestation_result_matches below. Keep lookup filters to
                # the repository because gh verifier-filter combinations are
                # not compatible across supported CLI versions.
                "--format",
                "json",
            ],
            timeout=60,
        )
    matching_attestations = (
        [
            item
            for item in attestation
            if _attestation_result_matches(
                item,
                report_hash=report_hash,
                revision=expected_revision,
                workflow_ref=workflow_ref,
                run_id=run_id,
                run_attempt=run_attempt,
                now=current_time,
                subject_name=RELEASE_REPORT_NAME,
                repository=RELEASE_REPOSITORY,
                workflow_path=RELEASE_WORKFLOW,
                event=event,
            )
        ]
        if isinstance(attestation, list)
        else []
    )
    if len(matching_attestations) != 1:
        return _release_evidence_result(
            "fail",
            "Release artifact attestation was not accepted: "
            f"{attestation_error or 'expected exactly one certificate-bound matching result'}.",
            report_hash=report_hash,
        )

    run_payload, run_error = _run_gh_json(
        [
            "gh",
            "api",
            "--method",
            "GET",
            f"repos/{RELEASE_REPOSITORY}/actions/runs/{run_id}",
        ]
    )
    expected_head_branch = tag if source_ref == f"refs/tags/{tag}" else "main"
    expected_run_fields = {
        "id": run_id,
        "head_sha": expected_revision,
        "name": RELEASE_WORKFLOW_NAME,
        "path": RELEASE_WORKFLOW,
        "event": event,
        "status": "completed",
        "conclusion": "success",
        "run_attempt": run_attempt,
        "head_branch": expected_head_branch,
    }
    if not isinstance(run_payload, dict) or any(
        type(run_payload.get(key)) is not type(expected) or run_payload.get(key) != expected
        for key, expected in expected_run_fields.items()
    ):
        return _release_evidence_result(
            "fail",
            "GitHub release run identity, revision, workflow, ref, attempt, or success does not match: "
            f"{run_error or 'invalid run response'}.",
            report_hash=report_hash,
        )
    head_repository = run_payload.get("head_repository")
    if not isinstance(head_repository, dict) or head_repository.get("full_name") != RELEASE_REPOSITORY:
        return _release_evidence_result(
            "fail",
            "GitHub release run head repository does not match the trusted repository.",
            report_hash=report_hash,
        )
    run_times: dict[str, datetime] = {}
    for field in ("created_at", "run_started_at", "updated_at"):
        parsed, timestamp_error = _recent_timestamp(
            run_payload.get(field),
            now=current_time,
            maximum_age_hours=RELEASE_EVIDENCE_MAX_AGE_HOURS,
        )
        if parsed is None:
            return _release_evidence_result(
                "fail",
                f"GitHub release run {field} {timestamp_error}.",
                report_hash=report_hash,
            )
        run_times[field] = parsed
    if not run_times["created_at"] <= run_times["run_started_at"] <= run_times["updated_at"]:
        return _release_evidence_result(
            "fail",
            "GitHub release run timestamps are not monotonically ordered.",
            report_hash=report_hash,
        )
    if (
        run_started_time != run_times["run_started_at"]
        or generated_time < run_times["run_started_at"] - skew
        or generated_time > run_times["updated_at"] + skew
    ):
        return _release_evidence_result(
            "fail",
            "Release evidence collection falls outside the exact GitHub release run window.",
            report_hash=report_hash,
        )

    jobs_payload, jobs_error = _run_gh_json(
        [
            "gh",
            "api",
            "--method",
            "GET",
            f"repos/{RELEASE_REPOSITORY}/actions/runs/{run_id}/attempts/{run_attempt}/jobs?per_page=100",
        ]
    )
    jobs = jobs_payload.get("jobs") if isinstance(jobs_payload, dict) else None
    jobs_total = jobs_payload.get("total_count") if isinstance(jobs_payload, dict) else None
    if (
        not isinstance(jobs, list)
        or type(jobs_total) is not int
        or jobs_total != len(jobs)
        or jobs_total > 100
    ):
        return _release_evidence_result(
            "fail",
            "GitHub release jobs could not be completely verified: "
            f"{jobs_error or 'malformed or paginated jobs response'}.",
            report_hash=report_hash,
        )
    metadata_jobs = [
        job for job in jobs if isinstance(job, dict) and job.get("name") == RELEASE_METADATA_JOB_NAME
    ]
    publish_jobs = [
        job for job in jobs if isinstance(job, dict) and job.get("name") == RELEASE_PUBLISH_JOB_NAME
    ]
    if len(metadata_jobs) != 1 or len(publish_jobs) != 1:
        return _release_evidence_result(
            "fail",
            "GitHub release run must contain exactly one metadata job and one publish job.",
            report_hash=report_hash,
        )
    metadata_job = metadata_jobs[0]
    publish_job = publish_jobs[0]
    for job in (metadata_job, publish_job):
        if (
            job.get("status") != "completed"
            or job.get("conclusion") != "success"
            or job.get("head_sha") != expected_revision
            or job.get("run_attempt") != run_attempt
        ):
            return _release_evidence_result(
                "fail",
                "GitHub release metadata or publish job identity and success are invalid.",
                report_hash=report_hash,
            )
    labels = publish_job.get("labels")
    if (
        not isinstance(labels, list)
        or "ubuntu-24.04" not in labels
        or "self-hosted" in labels
        or not _release_job_steps_are_trusted(metadata_job, REQUIRED_RELEASE_METADATA_STEPS)
        or not _release_job_steps_are_trusted(publish_job, REQUIRED_RELEASE_PUBLISH_STEPS)
    ):
        return _release_evidence_result(
            "fail",
            "GitHub release jobs did not use the trusted runner or complete the ordered safety steps.",
            report_hash=report_hash,
        )
    publish_times: dict[str, datetime] = {}
    for field in ("started_at", "completed_at"):
        parsed, timestamp_error = _recent_timestamp(
            publish_job.get(field),
            now=current_time,
            maximum_age_hours=RELEASE_EVIDENCE_MAX_AGE_HOURS,
        )
        if parsed is None:
            return _release_evidence_result(
                "fail",
                f"GitHub release publish job {field} {timestamp_error}.",
                report_hash=report_hash,
            )
        publish_times[field] = parsed
    if (
        publish_times["started_at"] > publish_times["completed_at"]
        or generated_time < publish_times["started_at"] - skew
        or generated_time > publish_times["completed_at"] + skew
    ):
        return _release_evidence_result(
            "fail",
            "Release evidence collection falls outside the trusted publish job window.",
            report_hash=report_hash,
        )

    artifacts_payload, artifacts_error = _run_gh_json(
        [
            "gh",
            "api",
            "--method",
            "GET",
            f"repos/{RELEASE_REPOSITORY}/actions/runs/{run_id}/artifacts?per_page=100",
        ]
    )
    artifacts = artifacts_payload.get("artifacts") if isinstance(artifacts_payload, dict) else None
    artifacts_total = artifacts_payload.get("total_count") if isinstance(artifacts_payload, dict) else None
    if (
        not isinstance(artifacts, list)
        or type(artifacts_total) is not int
        or artifacts_total != len(artifacts)
        or artifacts_total > 100
    ):
        return _release_evidence_result(
            "fail",
            "GitHub release artifacts could not be completely verified: "
            f"{artifacts_error or 'malformed or paginated artifacts response'}.",
            report_hash=report_hash,
        )
    artifact_name = f"release-evidence-{expected_revision}-{run_id}-{run_attempt}"
    matching_artifacts = [
        artifact
        for artifact in artifacts
        if isinstance(artifact, dict) and artifact.get("name") == artifact_name
    ]
    if len(matching_artifacts) != 1:
        return _release_evidence_result(
            "fail",
            "GitHub release run does not contain exactly one distinct evidence artifact.",
            report_hash=report_hash,
        )
    evidence_artifact = matching_artifacts[0]
    artifact_run = evidence_artifact.get("workflow_run")
    if (
        type(evidence_artifact.get("id")) is not int
        or evidence_artifact["id"] <= 0
        or type(evidence_artifact.get("size_in_bytes")) is not int
        or evidence_artifact["size_in_bytes"] <= 0
        or evidence_artifact.get("expired") is not False
        or not isinstance(artifact_run, dict)
        or artifact_run.get("id") != run_id
        or artifact_run.get("head_sha") != expected_revision
    ):
        return _release_evidence_result(
            "fail",
            "GitHub release evidence artifact is expired or bound to the wrong run or revision.",
            report_hash=report_hash,
        )
    artifact_times: dict[str, datetime] = {}
    for field in ("created_at", "updated_at"):
        parsed, timestamp_error = _recent_timestamp(
            evidence_artifact.get(field),
            now=current_time,
            maximum_age_hours=RELEASE_EVIDENCE_MAX_AGE_HOURS,
        )
        if parsed is None:
            return _release_evidence_result(
                "fail",
                f"GitHub release evidence artifact {field} {timestamp_error}.",
                report_hash=report_hash,
            )
        artifact_times[field] = parsed
    if (
        artifact_times["created_at"] > artifact_times["updated_at"]
        or artifact_times["created_at"] < generated_time - skew
        or artifact_times["updated_at"] > run_times["updated_at"] + skew
    ):
        return _release_evidence_result(
            "fail",
            "GitHub release evidence artifact timestamps fall outside the trusted run.",
            report_hash=report_hash,
        )

    live_release, live_release_error = _run_gh_json(
        [
            "gh",
            "api",
            "--method",
            "GET",
            f"repos/{RELEASE_REPOSITORY}/releases/tags/{tag}",
        ]
    )
    expected_live_release = {
        "id": release_id,
        "tag_name": tag,
        "target_commitish": expected_revision,
        "draft": False,
        "prerelease": False,
        "published_at": published_at,
        "html_url": release["html_url"],
    }
    if not isinstance(live_release, dict) or any(
        type(live_release.get(key)) is not type(expected) or live_release.get(key) != expected
        for key, expected in expected_live_release.items()
    ):
        return _release_evidence_result(
            "fail",
            "Current GitHub release metadata no longer matches the attested report: "
            f"{live_release_error or 'invalid release response'}.",
            report_hash=report_hash,
        )
    live_assets, live_assets_error = _run_gh_json(
        [
            "gh",
            "api",
            "--method",
            "GET",
            f"repos/{RELEASE_REPOSITORY}/releases/{release_id}/assets?per_page=100",
        ]
    )
    if not isinstance(live_assets, list) or len(live_assets) != len(expected_names):
        return _release_evidence_result(
            "fail",
            "Current GitHub release asset inventory is incomplete or paginated: "
            f"{live_assets_error or 'invalid asset response'}.",
            report_hash=report_hash,
        )
    live_asset_map: dict[str, dict[str, Any]] = {}
    for asset in live_assets:
        name = asset.get("name") if isinstance(asset, dict) else None
        if not isinstance(name, str) or name in live_asset_map:
            return _release_evidence_result(
                "fail",
                "Current GitHub release asset names are malformed or duplicated.",
                report_hash=report_hash,
            )
        live_asset_map[name] = asset
    if set(live_asset_map) != expected_names:
        return _release_evidence_result(
            "fail",
            "Current GitHub release asset names do not match the exact publishable set.",
            report_hash=report_hash,
        )
    for name in sorted(expected_names):
        report_asset = asset_map[name]
        live_asset = live_asset_map[name]
        if (
            type(live_asset.get("id")) is not int
            or live_asset["id"] <= 0
            or live_asset.get("state") != "uploaded"
            or live_asset.get("size") != report_asset["size"]
            or live_asset.get("digest") != f"sha256:{report_asset['sha256']}"
        ):
            return _release_evidence_result(
                "fail",
                f"Current GitHub release asset size or SHA-256 does not match: {name}.",
                report_hash=report_hash,
            )

    live_history_payload, live_history_error = _run_gh_json(
        [
            "gh",
            "api",
            "--method",
            "GET",
            "--paginate",
            "--slurp",
            "--jq",
            "flatten",
            f"repos/{RELEASE_REPOSITORY}/releases?per_page=100",
        ]
    )
    normalized_history, history_error = _normalized_live_release_history(live_history_payload)
    if normalized_history is None or normalized_history != history:
        return _release_evidence_result(
            "fail",
            "Current published release history does not exactly match the attested report: "
            f"{live_history_error or history_error or 'history mismatch'}.",
            report_hash=report_hash,
        )

    branch, branch_error = _run_gh_json(
        [
            "gh",
            "api",
            "--method",
            "GET",
            f"repos/{RELEASE_REPOSITORY}/branches/main",
        ]
    )
    branch_commit = branch.get("commit") if isinstance(branch, dict) else None
    if (
        not isinstance(branch, dict)
        or branch.get("name") != "main"
        or branch.get("protected") is not True
        or not isinstance(branch_commit, dict)
        or not isinstance(branch_commit.get("sha"), str)
        or not _COMMIT_RE.fullmatch(branch_commit["sha"])
    ):
        return _release_evidence_result(
            "fail",
            "The live main branch is not an exact protected branch: "
            f"{branch_error or 'invalid branch response'}.",
            report_hash=report_hash,
        )
    for entry in history:
        history_tag = entry["tag"]
        history_target = entry["target_commit"]
        resolved_tag, tag_error = _resolve_release_tag_commit(history_tag)
        if resolved_tag != history_target:
            return _release_evidence_result(
                "fail",
                f"Published release tag {history_tag} does not resolve to its recorded target: "
                f"{tag_error or 'tag moved or was deleted'}.",
                report_hash=report_hash,
            )
        comparison, comparison_error = _run_gh_json(
            [
                "gh",
                "api",
                "--method",
                "GET",
                f"repos/{RELEASE_REPOSITORY}/compare/{history_target}...main",
            ]
        )
        base_commit = comparison.get("base_commit") if isinstance(comparison, dict) else None
        merge_base = comparison.get("merge_base_commit") if isinstance(comparison, dict) else None
        if (
            not isinstance(comparison, dict)
            or comparison.get("status") not in {"identical", "ahead"}
            or not isinstance(base_commit, dict)
            or base_commit.get("sha") != history_target
            or not isinstance(merge_base, dict)
            or merge_base.get("sha") != history_target
        ):
            return _release_evidence_result(
                "fail",
                f"Published release tag {history_tag} target is not proven to be on protected main "
                f"ancestry: {comparison_error or 'invalid comparison response'}.",
                report_hash=report_hash,
            )

    return {
        "status": "pass",
        "mode": "attested",
        "detail": (
            "Fresh canonical release report, exact attestation, successful protected-main workflow, "
            "artifact, release inventory, and every bounded history tag and main ancestor were verified."
        ),
        "report": {
            "sha256": report_hash,
            "tag": tag,
            "release_id": release_id,
            "asset_count": len(asset_map),
            "history_count": len(history),
        },
        "evidence": {
            "repository": RELEASE_REPOSITORY,
            "source_revision": expected_revision,
            "run_id": run_id,
            "run_attempt": run_attempt,
            "workflow": RELEASE_WORKFLOW,
            "event": event,
            "runner_environment": "github-hosted",
            "attestation": "verified",
            "github_run": "verified",
            "github_jobs": "verified",
            "github_artifact": "verified",
            "github_release": "verified",
            "git_tag": "verified",
            "history_tags": "verified",
            "protected_main": "verified",
        },
    }


_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_REPOSITORY_SETTINGS_CHECKS = (
    "branch_required_status_checks",
    "branch_status_checks_bound_to_actions_app",
    "branch_require_up_to_date",
    "branch_enforce_admins",
    "branch_require_pull_request",
    "branch_minimum_approvals",
    "branch_dismiss_stale_reviews",
    "branch_require_last_push_approval",
    "branch_conversation_resolution",
    "branch_linear_history",
    "branch_force_pushes_disabled",
    "branch_deletions_disabled",
)
REQUIRED_RELEASE_ENVIRONMENT_CHECKS = (
    "release_required_reviewers",
    "release_independent_reviewers",
    "release_prevent_self_review",
    "release_protected_branches",
    "release_signing_secrets",
    "release_windows_code_signing_required",
    "production_required_reviewers",
    "production_independent_reviewers",
    "production_prevent_self_review",
    "production_protected_branches",
    "production_secrets",
)
REQUIRED_RELEASE_HISTORY_CHECKS = (
    "published_release_exists",
    "release_is_not_draft_or_prerelease",
    "release_lineage_complete",
    "release_assets_include_checksums_and_python_artifacts",
)
REQUIRED_RELEASE_CHECKS = (
    "published_release_exists",
    "release_is_not_draft_or_prerelease",
    "target_commit_matches",
    "release_assets_complete",
    "checksums_verified",
    "python_artifacts_verified",
    "sbom_verified",
    "provenance_verified",
)
REQUIRED_DEPLOYMENT_CHECKS = (
    "source_revision",
    "source_revision_final",
    "systemd_service_state",
    "filesystem_permissions",
    "verified_recent_state_backup",
    "deployment_host_identity",
    "verified_restore_drill",
    "verified_production_rollback_drill",
    "loopback_health",
    "loopback_metrics",
    "loopback_token_auth",
    "public_https_proxy",
)
REQUIRED_PLATFORM_CI_CHECKS = (
    "aggregate_python_package_build",
    "python_ubuntu_matrix",
    "python_macos_14_15_26_matrix",
    "python_windows_2025_vs2026_matrix",
    "rhel_ubi_8_9_10_and_rhel_7_abi",
    "rocky_linux_8_9_10",
    "windows_11_arm",
    "react_build",
    "mobile_web_smoke_android_and_ios",
    "tkinter_gui_lifecycle",
)
REQUIRED_PLATFORM_CHECKS = (
    "windows_hosted_python_and_smoke",
    "windows_11_arm_python_and_smoke",
    "ubuntu_python_tkinter_and_verifier",
    "macos_14_15_26_python_and_verifier",
)
REQUIRED_CREDENTIALED_POLYMARKET_CHECKS = (
    "report_integrity",
    "source_revision",
    "public_live_checks",
    "credentialed_read_checks",
    "credential_live_verified",
)
REQUIRED_FUNDED_POLYMARKET_CHECKS = (
    "report_integrity",
    "source_revision",
    "public_live_checks",
    "credentialed_read_checks",
    "funded_order_cancel",
    "post_cancel_verified",
    "funded_live_verified",
)
REQUIRED_EVIDENCE_CHECKS = {
    "repository-settings": REQUIRED_REPOSITORY_SETTINGS_CHECKS,
    "release-environment": REQUIRED_RELEASE_ENVIRONMENT_CHECKS,
    "platform-ci": REQUIRED_PLATFORM_CI_CHECKS,
    "platform": REQUIRED_PLATFORM_CHECKS,
}
REQUIRED_EVIDENCE_FIELDS = {
    "repository-settings": ("source",),
    "release-environment": ("source",),
    "platform-ci": ("source", "scope", "run_id", "source_revision"),
    "platform": ("source", "scope", "targets", "source_revision"),
}
_STRING_FIELDS = frozenset(
    {
        "source",
        "scope",
        "environment",
        "expected_version",
        "source_revision",
        "tag",
        "target_commit",
        "target_tier",
        "report_hash",
    }
)


def _captured_output_metadata(stdout: str, stderr: str) -> dict[str, Any]:
    """Describe captured process output without retaining its potentially sensitive contents."""

    def describe(value: str) -> dict[str, Any]:
        text = value if isinstance(value, str) else ""
        encoded = text.encode("utf-8")
        return {
            "bytes": len(encoded),
            "lines": len(text.splitlines()),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }

    return {"stdout": describe(stdout), "stderr": describe(stderr)}


def _is_nonblank_string(value: Any, *, single_line: bool = True) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and (not single_line or ("\n" not in value and "\r" not in value))
    )


def _required_field_is_valid(field: str, value: Any) -> bool:
    if field in _STRING_FIELDS:
        return _is_nonblank_string(value)
    if field == "run_id":
        return type(value) is int and value > 0
    if field in {"assets", "targets"}:
        return (
            isinstance(value, list)
            and bool(value)
            and all(_is_nonblank_string(item) for item in value)
            and len(value) == len(set(value))
        )
    if field == "live_action":
        return type(value) is bool and value is True
    return False


def _project_version() -> str:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(metadata.get("project", {}).get("version") or "").strip()


def _repository_revision() -> str:
    """Return the exact checkout revision used by revision-bound evidence."""

    try:
        if not _repository_root_is_exact():
            return ""
        result = subprocess.run(
            _trusted_git_command("rev-parse", "--verify", "HEAD^{commit}"),
            cwd=ROOT,
            env=_trusted_git_environment(),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    revision = result.stdout.strip().lower()
    return revision if result.returncode == 0 and _COMMIT_RE.fullmatch(revision) else ""


def _repository_is_clean() -> bool:
    """Return whether evidence can be bound to the exact committed checkout."""

    try:
        if not _repository_root_is_exact():
            return False
        result = subprocess.run(
            _trusted_git_command("status", "--porcelain=v1", "--untracked-files=all"),
            cwd=ROOT,
            env=_trusted_git_environment(),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and not result.stdout.strip()


def _trusted_git_environment() -> dict[str, str]:
    """Return a non-interactive Git environment without caller-controlled Git state."""

    allowed = {
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
        "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA",
    }
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    return environment


def _trusted_git_command(*arguments: str) -> list[str]:
    return [
        "git",
        "-c", f"safe.directory={ROOT.resolve()}",
        "-c", "core.fsmonitor=false",
        "-c", "core.hooksPath=",
        *arguments,
    ]


def _repository_root_is_exact() -> bool:
    try:
        result = subprocess.run(
            _trusted_git_command("rev-parse", "--show-toplevel"),
            cwd=ROOT,
            env=_trusted_git_environment(),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        reported = Path(result.stdout.strip()).resolve()
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and os.path.normcase(str(reported)) == os.path.normcase(str(ROOT.resolve()))


def _reviewed_evidence(
    path_value: str,
    label: str,
    *,
    evidence_type: str,
    required_fields: tuple[str, ...] = (),
    required_checks: tuple[str, ...] = (),
    expected_fields: dict[str, Any] | None = None,
    revision_field: str = "",
    expected_revision: str = "",
    now: datetime | None = None,
    score_eligible: bool = True,
) -> tuple[bool, str]:
    if not _is_nonblank_string(path_value):
        return False, f"Provide reviewed {label} JSON evidence."
    path = Path(path_value).expanduser()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, f"{label} evidence is not readable JSON."
    if not isinstance(payload, dict) or payload.get("verified") is not True:
        return False, f"{label} evidence must contain verified=true."
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        return False, f"{label} evidence must contain schema_version=1."
    if payload.get("evidence_type") != evidence_type:
        return False, f"{label} evidence must declare evidence_type={evidence_type!r}."
    contract_fields = REQUIRED_EVIDENCE_FIELDS.get(evidence_type)
    contract_checks = REQUIRED_EVIDENCE_CHECKS.get(evidence_type)
    if contract_fields is None or contract_checks is None:
        return False, f"{label} evidence_type has no validation contract."
    if required_checks and set(required_checks) != set(contract_checks):
        return False, f"{label} evidence validator check contract is inconsistent."
    if "reviewed_by" not in payload or "reviewed_at" not in payload:
        return False, f"{label} evidence requires reviewed_by and reviewed_at."
    if not _is_nonblank_string(payload["reviewed_by"]):
        return False, f"{label} evidence reviewed_by must be a non-empty single-line string."
    if not _is_nonblank_string(payload["reviewed_at"]):
        return False, f"{label} evidence reviewed_at must be a non-empty ISO-8601 string."
    try:
        reviewed_at = datetime.fromisoformat(payload["reviewed_at"].replace("Z", "+00:00"))
    except ValueError:
        return False, f"{label} evidence reviewed_at must be valid ISO-8601."
    if reviewed_at.tzinfo is None or reviewed_at.utcoffset() is None:
        return False, f"{label} evidence reviewed_at must include a timezone."
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise ValueError("evidence validation clock must include a timezone")
    age = current_time.astimezone(timezone.utc) - reviewed_at.astimezone(timezone.utc)
    if age < -timedelta(seconds=EVIDENCE_MAX_FUTURE_SKEW_SECONDS):
        return False, f"{label} evidence reviewed_at is in the future."
    if age > timedelta(days=EVIDENCE_MAX_AGE_DAYS):
        return False, f"{label} evidence is stale; review it again within {EVIDENCE_MAX_AGE_DAYS} days."
    if not _is_nonblank_string(payload.get("source")):
        return False, f"{label} evidence requires a non-empty source."
    all_required_fields = tuple(dict.fromkeys((*contract_fields, *required_fields)))
    missing_fields = [field for field in all_required_fields if field not in payload]
    if missing_fields:
        return False, f"{label} evidence is missing required fields: {', '.join(missing_fields)}."
    invalid_fields = [
        field for field in all_required_fields if not _required_field_is_valid(field, payload[field])
    ]
    if invalid_fields:
        return False, f"{label} evidence has invalid required fields: {', '.join(invalid_fields)}."
    for field in _STRING_FIELDS:
        if field in payload and not _is_nonblank_string(payload[field]):
            return False, f"{label} evidence field {field} must be a non-empty single-line string."
    for field, expected in (expected_fields or {}).items():
        actual = payload.get(field)
        if type(actual) is not type(expected) or actual != expected:
            return False, f"{label} evidence field {field} must equal {expected!r}."
    for field in ("source_revision", "target_commit"):
        if field in payload and (not isinstance(payload[field], str) or not _COMMIT_RE.fullmatch(payload[field])):
            return False, f"{label} evidence field {field} must be a 40-character commit SHA."
    if revision_field:
        if not expected_revision:
            return False, f"{label} evidence cannot be validated because the repository revision is unavailable."
        if payload.get(revision_field) != expected_revision:
            return False, (
                f"{label} evidence field {revision_field} must match the current repository revision "
                f"{expected_revision}."
            )
    if "report_hash" in payload and (
        not isinstance(payload["report_hash"], str) or not _HASH_RE.fullmatch(payload["report_hash"])
    ):
        return False, f"{label} evidence report_hash must be a 64-character SHA-256 value."
    if "run_id" in payload and (type(payload["run_id"]) is not int or payload["run_id"] <= 0):
        return False, f"{label} evidence run_id must be a positive integer."
    for field in ("assets", "targets"):
        if field in payload and not _required_field_is_valid(field, payload[field]):
            return False, f"{label} evidence {field} must be a non-empty list of unique non-empty strings."
    if "live_action" in payload and type(payload["live_action"]) is not bool:
        return False, f"{label} evidence live_action must be a boolean."
    if evidence_type == "funded-polymarket" and payload.get("live_action") is not True:
        return False, "funded Polymarket evidence requires live_action=true."
    checks = payload.get("checks")
    if not isinstance(checks, list) or not checks:
        return False, f"{label} evidence requires a non-empty checks list."
    names = [check.get("name") for check in checks if isinstance(check, dict)]
    if len(names) != len(checks) or any(not _is_nonblank_string(name) for name in names):
        return False, f"{label} evidence checks require non-empty names."
    if len(set(names)) != len(names):
        return False, f"{label} evidence checks must have unique names."
    if any(not isinstance(check, dict) or check.get("status") not in {"pass", "ok"} for check in checks):
        return False, f"{label} evidence contains a failed or malformed check."
    missing_checks = [name for name in contract_checks if name not in names]
    if missing_checks:
        return False, f"{label} evidence is missing required checks: {', '.join(missing_checks)}."
    unknown_checks = [name for name in names if name not in contract_checks]
    if unknown_checks:
        return False, f"{label} evidence contains unknown checks: {', '.join(unknown_checks)}."
    if not score_eligible:
        return False, (
            f"Reviewed {label} manifest is diagnostic-only; score credit requires exact live API "
            "evidence produced by a trusted hosted workflow and bound by artifact attestation."
        )
    return True, f"Reviewed {label} evidence passed its diagnostic contract."


def _trusted_evidence_result(
    status: str,
    detail: str,
    *,
    evidence_type: str,
    report_hash: str = "",
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": status,
        "mode": "attested",
        "evidence_type": evidence_type,
        "detail": detail,
    }
    if report_hash:
        result["report"] = {"sha256": report_hash}
    return result


def _attested_trusted_evidence(
    path_value: str | None,
    label: str,
    *,
    evidence_type: str,
    expected_revision: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify exact-byte hosted evidence before allowing it to affect the score."""

    if evidence_type not in TRUSTED_EVIDENCE_WORKFLOW_CONTRACTS:
        return _trusted_evidence_result(
            "fail",
            f"{label} has no trusted hosted workflow contract.",
            evidence_type=evidence_type,
        )
    if not _is_nonblank_string(path_value):
        return _trusted_evidence_result(
            "not_run",
            f"Provide fresh GitHub-attested {label} evidence.",
            evidence_type=evidence_type,
        )
    path = Path(path_value).expanduser()
    try:
        if path.is_symlink() or not path.is_file():
            raise OSError("not a regular non-symbolic-link file")
        raw = path.read_bytes()
        payload = _strict_json_bytes(raw, maximum_bytes=PUBLIC_LIVE_REPORT_MAX_BYTES)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError, _DuplicateJsonKey):
        return _trusted_evidence_result(
            "fail",
            f"{label} evidence is unreadable, malformed, duplicated-key, non-finite, symbolic-link, or oversized JSON.",
            evidence_type=evidence_type,
        )
    if not isinstance(payload, dict):
        return _trusted_evidence_result(
            "fail",
            f"{label} evidence must be a JSON object.",
            evidence_type=evidence_type,
        )
    if payload.get("report_type") != TRUSTED_EVIDENCE_REPORT_TYPE:
        if payload.get("evidence_type") != evidence_type:
            return _trusted_evidence_result(
                "diagnostic",
                (
                    f"Reviewed {label} manifest is diagnostic-only. It must declare "
                    f"evidence_type={evidence_type!r}. Score credit requires the schema-v2 trusted hosted report."
                ),
                evidence_type=evidence_type,
            )
        if not _COMMIT_RE.fullmatch(expected_revision or ""):
            return _trusted_evidence_result(
                "fail",
                f"Attested {label} evidence cannot be scored because the repository revision is unavailable; "
                "an exact clean repository revision is required.",
                evidence_type=evidence_type,
            )
        return _trusted_evidence_result(
            "diagnostic",
            (
                f"Reviewed {label} manifest is diagnostic-only. Score credit requires the schema-v2 "
                "trusted hosted report, exact GitHub run, uploaded artifact, and exact-byte attestation."
            ),
            evidence_type=evidence_type,
        )
    if not _COMMIT_RE.fullmatch(expected_revision or ""):
        return _trusted_evidence_result(
            "fail",
            f"Attested {label} evidence cannot be scored because the repository revision is unavailable; "
            "an exact clean repository revision is required.",
            evidence_type=evidence_type,
        )

    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise ValueError("trusted evidence validation clock must include a timezone")
    validation = validate_trusted_evidence_manifest(
        payload,
        expected_evidence_type=evidence_type,
        expected_revision=expected_revision,
        now=current_time,
    )
    report_hash = hashlib.sha256(raw).hexdigest()
    if validation.get("ok") is not True:
        errors = validation.get("errors")
        detail = "; ".join(str(item) for item in errors) if isinstance(errors, list) else "schema validation failed"
        return _trusted_evidence_result(
            "fail",
            f"{label} evidence failed the exact semantic contract: {detail}.",
            evidence_type=evidence_type,
            report_hash=report_hash,
        )
    if raw != trusted_evidence_canonical_bytes(payload):
        return _trusted_evidence_result(
            "fail",
            f"{label} evidence is not canonical JSON.",
            evidence_type=evidence_type,
            report_hash=report_hash,
        )

    contract = TRUSTED_EVIDENCE_WORKFLOW_CONTRACTS[evidence_type]
    run_id = int(validation["run_id"])
    run_attempt = int(validation["run_attempt"])
    workflow_ref = str(validation["workflow_ref"])
    subject_name = str(validation["subject_name"])
    with tempfile.TemporaryDirectory(prefix="market-sentinel-trusted-evidence-") as temporary:
        attestation_target = Path(temporary) / subject_name
        attestation_target.write_bytes(raw)
        attestation, attestation_error = _run_gh_json(
            [
                "gh",
                "attestation",
                "verify",
                str(attestation_target),
                "--repo",
                PUBLIC_LIVE_REPOSITORY,
                # Policy is enforced from the verified signed result by
                # _attestation_result_matches. Avoid gh's incompatible
                # verifier-filter combinations so current CLI versions can
                # actually retrieve valid attestations.
                "--format",
                "json",
            ],
            timeout=60,
        )
    matching_attestations = (
        [
            item
            for item in attestation
            if _attestation_result_matches(
                item,
                report_hash=report_hash,
                revision=expected_revision,
                workflow_ref=workflow_ref,
                run_id=run_id,
                run_attempt=run_attempt,
                now=current_time,
                subject_name=subject_name,
                repository=PUBLIC_LIVE_REPOSITORY,
                workflow_path=str(contract["workflow"]),
                event="workflow_dispatch",
            )
        ]
        if isinstance(attestation, list)
        else []
    )
    if len(matching_attestations) != 1:
        return _trusted_evidence_result(
            "fail",
            (
                f"{label} artifact attestation was not accepted: "
                f"{attestation_error or 'expected exactly one certificate-bound matching result'}."
            ),
            evidence_type=evidence_type,
            report_hash=report_hash,
        )

    run_payload, run_error = _run_gh_json(
        ["gh", "api", "--method", "GET", f"repos/{PUBLIC_LIVE_REPOSITORY}/actions/runs/{run_id}"]
    )
    if not isinstance(run_payload, dict):
        return _trusted_evidence_result(
            "fail",
            f"{label} workflow run could not be verified: {run_error or 'malformed run response'}.",
            evidence_type=evidence_type,
            report_hash=report_hash,
        )
    expected_run = {
        "id": run_id,
        "head_sha": expected_revision,
        "name": contract["workflow_name"],
        "path": contract["workflow"],
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "success",
        "run_attempt": run_attempt,
        "head_branch": "main",
    }
    if any(type(run_payload.get(key)) is not type(value) or run_payload.get(key) != value for key, value in expected_run.items()):
        return _trusted_evidence_result(
            "fail",
            f"{label} workflow run identity, revision, event, attempt, branch, or conclusion is invalid.",
            evidence_type=evidence_type,
            report_hash=report_hash,
        )
    head_repository = run_payload.get("head_repository")
    if not isinstance(head_repository, dict) or head_repository.get("full_name") != PUBLIC_LIVE_REPOSITORY:
        return _trusted_evidence_result(
            "fail",
            f"{label} workflow run head repository is invalid.",
            evidence_type=evidence_type,
            report_hash=report_hash,
        )
    run_times: dict[str, datetime] = {}
    for field in ("created_at", "run_started_at", "updated_at"):
        parsed, error = _recent_timestamp(run_payload.get(field), now=current_time)
        if parsed is None:
            return _trusted_evidence_result(
                "fail",
                f"{label} workflow run {field} {error}.",
                evidence_type=evidence_type,
                report_hash=report_hash,
            )
        run_times[field] = parsed
    if not run_times["created_at"] <= run_times["run_started_at"] <= run_times["updated_at"]:
        return _trusted_evidence_result(
            "fail",
            f"{label} workflow run timestamps are not monotonically ordered.",
            evidence_type=evidence_type,
            report_hash=report_hash,
        )
    generated_at = validation.get("generated_at")
    skew = timedelta(seconds=EVIDENCE_MAX_FUTURE_SKEW_SECONDS)
    if (
        not isinstance(generated_at, datetime)
        or generated_at < run_times["run_started_at"] - skew
        or generated_at > run_times["updated_at"] + skew
    ):
        return _trusted_evidence_result(
            "fail",
            f"{label} generated_at falls outside the trusted workflow run window.",
            evidence_type=evidence_type,
            report_hash=report_hash,
        )

    jobs_payload, jobs_error = _run_gh_json(
        [
            "gh",
            "api",
            "--method",
            "GET",
            f"repos/{PUBLIC_LIVE_REPOSITORY}/actions/runs/{run_id}/jobs?filter=latest&per_page=100",
        ]
    )
    jobs = jobs_payload.get("jobs") if isinstance(jobs_payload, dict) else None
    jobs_total = jobs_payload.get("total_count") if isinstance(jobs_payload, dict) else None
    if not isinstance(jobs, list) or type(jobs_total) is not int or jobs_total != len(jobs) or jobs_total > 100:
        return _trusted_evidence_result(
            "fail",
            f"{label} hosted review job could not be verified: {jobs_error or 'malformed jobs response'}.",
            evidence_type=evidence_type,
            report_hash=report_hash,
        )
    matching_jobs = [job for job in jobs if isinstance(job, dict) and job.get("name") == contract["job"]]
    if len(matching_jobs) != 1:
        return _trusted_evidence_result(
            "fail",
            f"{label} workflow must contain exactly one trusted hosted review job.",
            evidence_type=evidence_type,
            report_hash=report_hash,
        )
    job = matching_jobs[0]
    labels = job.get("labels")
    if (
        job.get("status") != "completed"
        or job.get("conclusion") != "success"
        or not isinstance(labels, list)
        or "ubuntu-24.04" not in labels
        or "self-hosted" in labels
    ):
        return _trusted_evidence_result(
            "fail",
            f"{label} review was not a successful GitHub-hosted ubuntu-24.04 job.",
            evidence_type=evidence_type,
            report_hash=report_hash,
        )
    steps = job.get("steps")
    if not isinstance(steps, list):
        return _trusted_evidence_result(
            "fail",
            f"{label} hosted review job is missing step results.",
            evidence_type=evidence_type,
            report_hash=report_hash,
        )
    step_names = [step.get("name") for step in steps if isinstance(step, dict)]
    if len(step_names) != len(steps) or len(step_names) != len(set(step_names)):
        return _trusted_evidence_result(
            "fail",
            f"{label} hosted review job has malformed or duplicate step names.",
            evidence_type=evidence_type,
            report_hash=report_hash,
        )
    required_steps = tuple(contract["required_steps"])
    required_results = {
        step.get("name"): (step.get("status"), step.get("conclusion"))
        for step in steps
        if isinstance(step, dict) and step.get("name") in required_steps
    }
    if set(required_results) != set(required_steps) or any(
        result != ("completed", "success") for result in required_results.values()
    ):
        return _trusted_evidence_result(
            "fail",
            f"{label} hosted review job did not complete every required collection, review, and attestation step.",
            evidence_type=evidence_type,
            report_hash=report_hash,
        )
    job_times: dict[str, datetime] = {}
    for field in ("started_at", "completed_at"):
        parsed, error = _recent_timestamp(job.get(field), now=current_time)
        if parsed is None:
            return _trusted_evidence_result(
                "fail",
                f"{label} hosted review job {field} {error}.",
                evidence_type=evidence_type,
                report_hash=report_hash,
            )
        job_times[field] = parsed
    if (
        job_times["started_at"] > job_times["completed_at"]
        or generated_at < job_times["started_at"] - skew
        or generated_at > job_times["completed_at"] + skew
    ):
        return _trusted_evidence_result(
            "fail",
            f"{label} generated_at falls outside the trusted hosted review job window.",
            evidence_type=evidence_type,
            report_hash=report_hash,
        )

    artifacts_payload, artifacts_error = _run_gh_json(
        [
            "gh",
            "api",
            "--method",
            "GET",
            f"repos/{PUBLIC_LIVE_REPOSITORY}/actions/runs/{run_id}/artifacts?per_page=100",
        ]
    )
    artifacts = artifacts_payload.get("artifacts") if isinstance(artifacts_payload, dict) else None
    artifacts_total = artifacts_payload.get("total_count") if isinstance(artifacts_payload, dict) else None
    if not isinstance(artifacts, list) or type(artifacts_total) is not int or artifacts_total != len(artifacts) or artifacts_total > 100:
        return _trusted_evidence_result(
            "fail",
            f"{label} artifact inventory is incomplete: {artifacts_error or 'malformed artifacts response'}.",
            evidence_type=evidence_type,
            report_hash=report_hash,
        )
    expected_artifact = str(validation["artifact_name"])
    matching_artifacts = [
        artifact for artifact in artifacts if isinstance(artifact, dict) and artifact.get("name") == expected_artifact
    ]
    if len(matching_artifacts) != 1:
        return _trusted_evidence_result(
            "fail",
            f"{label} workflow does not contain exactly one distinct evidence artifact.",
            evidence_type=evidence_type,
            report_hash=report_hash,
        )
    artifact = matching_artifacts[0]
    artifact_run = artifact.get("workflow_run")
    if (
        type(artifact.get("id")) is not int
        or artifact["id"] <= 0
        or type(artifact.get("size_in_bytes")) is not int
        or artifact["size_in_bytes"] <= 0
        or artifact.get("expired") is not False
        or not isinstance(artifact_run, dict)
        or artifact_run.get("id") != run_id
        or artifact_run.get("head_sha") != expected_revision
    ):
        return _trusted_evidence_result(
            "fail",
            f"{label} artifact is expired, empty, or bound to the wrong run or revision.",
            evidence_type=evidence_type,
            report_hash=report_hash,
        )
    for field in ("created_at", "updated_at"):
        parsed, error = _recent_timestamp(artifact.get(field), now=current_time)
        if parsed is None or parsed < job_times["started_at"] - skew or parsed > run_times["updated_at"] + skew:
            return _trusted_evidence_result(
                "fail",
                f"{label} artifact {field} {error or 'falls outside the trusted run window'}.",
                evidence_type=evidence_type,
                report_hash=report_hash,
            )

    if evidence_type in {"platform-ci", "platform"}:
        source_run_id = int(validation["source_run_id"])
        source_run_payload, source_run_error = _run_gh_json(
            ["gh", "api", "--method", "GET", f"repos/{PUBLIC_LIVE_REPOSITORY}/actions/runs/{source_run_id}"]
        )
        source_jobs_payload, source_jobs_error = _run_gh_json(
            [
                "gh",
                "api",
                "--method",
                "GET",
                f"repos/{PUBLIC_LIVE_REPOSITORY}/actions/runs/{source_run_id}/jobs?filter=latest&per_page=100",
            ]
        )
        try:
            derived_ci, derived_platform = derive_platform_checks(
                source_run_payload,
                source_jobs_payload,
                expected_revision=expected_revision,
                source_run_id=source_run_id,
            )
        except ValueError as exc:
            return _trusted_evidence_result(
                "fail",
                (
                    f"{label} referenced CI run could not be independently verified: "
                    f"{source_run_error or source_jobs_error or str(exc)}."
                ),
                evidence_type=evidence_type,
                report_hash=report_hash,
            )
        expected_checks = derived_ci if evidence_type == "platform-ci" else derived_platform
        observed_checks = tuple(
            str(check.get("name") or "") for check in payload.get("checks", []) if isinstance(check, dict)
        )
        if observed_checks != expected_checks:
            return _trusted_evidence_result(
                "fail",
                f"{label} checks do not match the independently verified exact CI run.",
                evidence_type=evidence_type,
                report_hash=report_hash,
            )

    return {
        "status": "pass",
        "mode": "attested",
        "evidence_type": evidence_type,
        "detail": (
            f"Fresh canonical {label} evidence, exact hosted workflow/job, uploaded artifact, "
            "and exact-byte artifact attestation were verified."
        ),
        "report": {"sha256": report_hash},
        "evidence": {
            "repository": PUBLIC_LIVE_REPOSITORY,
            "source_revision": expected_revision,
            "run_id": run_id,
            "run_attempt": run_attempt,
            "workflow": contract["workflow"],
            "runner_environment": "github-hosted",
            "attestation": "verified",
            "github_run": "verified",
            "github_job": "verified",
            "github_artifact": "verified",
        },
    }


def _live_evidence(
    path_value: str | None,
    *,
    target_tier: str,
    expected_revision: str,
) -> tuple[bool, str, dict[str, Any]]:
    evidence_type = (
        "credentialed-polymarket"
        if target_tier == "credential_live_verified"
        else "funded-polymarket"
    )
    label = "credentialed Polymarket" if evidence_type == "credentialed-polymarket" else "funded Polymarket"
    if _is_nonblank_string(path_value):
        try:
            candidate = _strict_json_bytes(
                Path(str(path_value)).expanduser().read_bytes(),
                maximum_bytes=PUBLIC_LIVE_REPORT_MAX_BYTES,
            )
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError, _DuplicateJsonKey):
            candidate = None
        if isinstance(candidate, dict) and candidate.get("report_type") == TRUSTED_EVIDENCE_REPORT_TYPE:
            result = _attested_trusted_evidence(
                path_value,
                label,
                evidence_type=evidence_type,
                expected_revision=expected_revision,
            )
            return result.get("status") == "pass", str(result.get("detail") or "Live evidence failed."), result
    diagnostic_ok, diagnostic_detail = _diagnostic_live_evidence(
        path_value,
        target_tier=target_tier,
        expected_revision=expected_revision,
    )
    return diagnostic_ok, diagnostic_detail, {
        "status": "diagnostic" if _is_nonblank_string(path_value) else "not_run",
        "mode": "raw",
        "evidence_type": evidence_type,
        "detail": diagnostic_detail,
    }


def _diagnostic_deployment_evidence(
    path_value: str | None,
    *,
    expected_version: str,
    expected_revision: str,
) -> tuple[bool, str]:
    """Review raw host output, but never treat unauthenticated bytes as score evidence."""

    if not _is_nonblank_string(path_value):
        return False, "Provide GitHub-attested production deployment evidence."
    if not _COMMIT_RE.fullmatch(expected_revision or ""):
        return False, "Deployment evidence requires an exact clean repository revision."
    try:
        review = review_deployment_report(
            Path(path_value).expanduser(),
            expected_version=expected_version,
            expected_revision=expected_revision,
        )
    except (OSError, DeploymentEvidenceError) as exc:
        return False, f"Raw deployment evidence failed strict semantic review: {exc}"
    return False, (
        "Raw deployment evidence passed semantic review "
        f"(sha256={review['raw_report_sha256']}) but is not score-eligible without a trusted "
        "GitHub workflow and exact-byte artifact attestation."
    )


def _deployment_result(status: str, detail: str, *, report_hash: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {"status": status, "mode": "attested", "detail": detail}
    if report_hash:
        result["report"] = {"sha256": report_hash}
    return result


def _canonical_deployment_origin(value: Any) -> str:
    if not _is_nonblank_string(value):
        return ""
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
        return ""
    hostname = parsed.hostname.lower()
    if hostname in {"localhost", "example.com", "analytics.example.com"} or hostname.endswith(".example.com"):
        return ""
    try:
        port = parsed.port
    except ValueError:
        return ""
    suffix = f":{port}" if port and port != 443 else ""
    return f"https://{hostname}{suffix}"


def _attested_deployment_report(
    path_value: str | None,
    *,
    expected_revision: str,
    expected_version: str,
    expected_origin: str | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate a canonical hosted-review envelope and its live GitHub provenance."""

    if not _is_nonblank_string(path_value):
        return _deployment_result("not_run", "Provide fresh GitHub-attested deployment evidence.")
    if not _COMMIT_RE.fullmatch(expected_revision or ""):
        return _deployment_result("fail", "Attested deployment evidence requires an exact clean revision.")
    canonical_origin = _canonical_deployment_origin(expected_origin)
    if not canonical_origin:
        return _deployment_result(
            "fail", "Attested deployment evidence requires the exact non-placeholder production origin."
        )
    path = Path(path_value).expanduser()
    try:
        if path.is_symlink() or not path.is_file():
            raise OSError("not a regular file")
        raw = path.read_bytes()
        payload = _strict_json_bytes(raw, maximum_bytes=DEPLOYMENT_REPORT_MAX_BYTES)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError, _DuplicateJsonKey):
        return _deployment_result("fail", "Deployment evidence is unreadable or malformed strict JSON.")
    report_hash = hashlib.sha256(raw).hexdigest()
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "report_type",
        "deployment",
        "evidence",
    }:
        return _deployment_result(
            "fail", "Deployment evidence does not match the exact attested schema.", report_hash=report_hash
        )
    canonical_bytes = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if raw != canonical_bytes:
        return _deployment_result(
            "fail", "Deployment evidence is not canonical JSON.", report_hash=report_hash
        )
    if payload.get("schema_version") != 1 or payload.get("report_type") != DEPLOYMENT_REPORT_TYPE:
        return _deployment_result(
            "fail", "Deployment evidence type or schema is invalid.", report_hash=report_hash
        )

    deployment = payload.get("deployment")
    evidence = payload.get("evidence")
    if not isinstance(deployment, dict) or set(deployment) != {
        "environment",
        "public_origin",
        "collected_at",
        "raw_report_sha256",
        "workflow_nonce",
        "check_count",
        "frontend_sha256",
        "deployment_provider",
        "host_identity_sha256",
        "restore_drill",
        "rollback_drill",
        "external_probe",
        "release",
    }:
        return _deployment_result(
            "fail", "Deployment metadata does not match the exact schema.", report_hash=report_hash
        )
    if not isinstance(evidence, dict) or set(evidence) != {
        "repository",
        "source_revision",
        "run_id",
        "run_attempt",
        "workflow",
        "workflow_name",
        "workflow_ref",
        "source_ref",
        "event",
        "runner_environment",
        "collector_job",
        "external_probe_job",
        "review_job",
        "collector_labels",
        "artifact_name",
    }:
        return _deployment_result(
            "fail", "Deployment workflow identity does not match the exact schema.", report_hash=report_hash
        )
    run_id = evidence.get("run_id")
    run_attempt = evidence.get("run_attempt")
    workflow_ref = f"{DEPLOYMENT_REPOSITORY}/{DEPLOYMENT_WORKFLOW}@{DEPLOYMENT_TRUSTED_MAIN_REF}"
    artifact_name = f"deployment-evidence-{expected_revision}-{run_id}-{run_attempt}"
    exact_evidence = {
        "repository": DEPLOYMENT_REPOSITORY,
        "source_revision": expected_revision,
        "workflow": DEPLOYMENT_WORKFLOW,
        "workflow_name": DEPLOYMENT_WORKFLOW_NAME,
        "workflow_ref": workflow_ref,
        "source_ref": DEPLOYMENT_TRUSTED_MAIN_REF,
        "event": "workflow_dispatch",
        "runner_environment": "github-hosted",
        "collector_job": DEPLOYMENT_COLLECTOR_JOB,
        "external_probe_job": DEPLOYMENT_EXTERNAL_PROBE_JOB,
        "review_job": DEPLOYMENT_REVIEW_JOB,
        "collector_labels": sorted(DEPLOYMENT_COLLECTOR_LABELS),
        "artifact_name": artifact_name,
    }
    if (
        type(run_id) is not int
        or run_id <= 0
        or type(run_attempt) is not int
        or run_attempt <= 0
        or any(evidence.get(key) != value for key, value in exact_evidence.items())
    ):
        return _deployment_result(
            "fail", "Deployment run, workflow, labels, or artifact binding is invalid.", report_hash=report_hash
        )

    current_time = now or datetime.now(timezone.utc)
    collected_at, collected_error = _recent_timestamp(
        deployment.get("collected_at"),
        now=current_time,
        maximum_age_hours=DEPLOYMENT_EVIDENCE_MAX_AGE_HOURS,
    )
    if collected_at is None:
        return _deployment_result(
            "fail", f"Deployment collection timestamp {collected_error}.", report_hash=report_hash
        )
    if (
        deployment.get("environment") != "production"
        or deployment.get("public_origin") != canonical_origin
        or not isinstance(deployment.get("raw_report_sha256"), str)
        or not _HASH_RE.fullmatch(deployment["raw_report_sha256"])
        or deployment.get("workflow_nonce") != f"{expected_revision}:{run_id}:{run_attempt}"
        or type(deployment.get("check_count")) is not int
        or deployment["check_count"] <= 0
        or not isinstance(deployment.get("frontend_sha256"), str)
        or not _HASH_RE.fullmatch(deployment["frontend_sha256"])
        or not _is_nonblank_string(deployment.get("deployment_provider"))
        or not isinstance(deployment.get("host_identity_sha256"), str)
        or not _HASH_RE.fullmatch(deployment["host_identity_sha256"])
    ):
        return _deployment_result(
            "fail", "Deployment environment, origin, raw digest, or review summary is invalid.", report_hash=report_hash
        )

    restore_drill = deployment.get("restore_drill")
    rollback_drill = deployment.get("rollback_drill")
    external_probe = deployment.get("external_probe")
    if (
        not isinstance(restore_drill, dict)
        or set(restore_drill) != {"completed_at", "backup_sha256", "restored_file_count", "restored_bytes", "application"}
        or not application_check_valid(
            restore_drill.get("application"), version=expected_version,
            revision=expected_revision, frontend_sha256=deployment.get("frontend_sha256"),
        )
        or not isinstance(restore_drill.get("backup_sha256"), str)
        or not _HASH_RE.fullmatch(restore_drill["backup_sha256"])
        or type(restore_drill.get("restored_file_count")) is not int
        or restore_drill["restored_file_count"] <= 0
        or type(restore_drill.get("restored_bytes")) is not int
        or restore_drill["restored_bytes"] < 0
        or not isinstance(rollback_drill, dict)
        or set(rollback_drill) != {"drill_id", "report_sha256", "completed_at", "rollback_revision", "final_revision", "step_count"}
        or not _is_nonblank_string(rollback_drill.get("drill_id"))
        or not isinstance(rollback_drill.get("report_sha256"), str)
        or not _HASH_RE.fullmatch(rollback_drill["report_sha256"])
        or not isinstance(rollback_drill.get("rollback_revision"), str)
        or not _COMMIT_RE.fullmatch(rollback_drill["rollback_revision"])
        or rollback_drill["rollback_revision"] == expected_revision
        or rollback_drill.get("final_revision") != expected_revision
        or rollback_drill.get("step_count") != 5
        or not isinstance(external_probe, dict)
        or set(external_probe) != {
            "probed_at",
            "raw_report_sha256",
            "runner_environment",
            "api_version",
            "source_revision",
            "frontend_sha256",
            "unauthenticated_probes",
        }
        or not isinstance(external_probe.get("raw_report_sha256"), str)
        or not _HASH_RE.fullmatch(external_probe["raw_report_sha256"])
        or external_probe.get("runner_environment") != "github-hosted"
        or external_probe.get("api_version") != expected_version
        or external_probe.get("source_revision") != expected_revision
        or external_probe.get("frontend_sha256") != deployment.get("frontend_sha256")
        or external_probe.get("unauthenticated_probes") != 5
    ):
        return _deployment_result(
            "fail",
            "Deployment restore, rollback, or independent external-probe proof is invalid.",
            report_hash=report_hash,
        )
    drill_times: dict[str, datetime] = {}
    for label, value in (
        ("restore drill", restore_drill.get("completed_at")),
        ("rollback drill", rollback_drill.get("completed_at")),
        ("external probe", external_probe.get("probed_at")),
    ):
        parsed, timestamp_error = _recent_timestamp(
            value,
            now=current_time,
            maximum_age_hours=DEPLOYMENT_EVIDENCE_MAX_AGE_HOURS,
        )
        if parsed is None:
            return _deployment_result(
                "fail",
                f"Deployment {label} timestamp {timestamp_error}.",
                report_hash=report_hash,
            )
        drill_times[label] = parsed

    release = deployment.get("release")
    if not isinstance(release, dict) or set(release) != {
        "id",
        "tag",
        "version",
        "target_commit",
        "published_at",
        "html_url",
        "asset",
    }:
        return _deployment_result(
            "fail", "Deployment release identity does not match the exact schema.", report_hash=report_hash
        )
    tag = f"v{expected_version}"
    release_id = release.get("id")
    expected_release_url = f"https://github.com/{DEPLOYMENT_REPOSITORY}/releases/tag/{tag}"
    asset = release.get("asset")
    expected_asset_name = f"market-sentinel-{tag}-frontend-dist.zip"
    if (
        type(release_id) is not int
        or release_id <= 0
        or release.get("tag") != tag
        or release.get("version") != expected_version
        or release.get("target_commit") != expected_revision
        or release.get("html_url") != expected_release_url
        or not _is_nonblank_string(release.get("published_at"))
        or not isinstance(asset, dict)
        or set(asset) != {"id", "name", "size", "sha256"}
        or type(asset.get("id")) is not int
        or asset["id"] <= 0
        or asset.get("name") != expected_asset_name
        or type(asset.get("size")) is not int
        or asset["size"] <= 0
        or not isinstance(asset.get("sha256"), str)
        or not _HASH_RE.fullmatch(asset["sha256"])
    ):
        return _deployment_result(
            "fail", "Deployment is not bound to the exact stable release and frontend asset.", report_hash=report_hash
        )

    attestation, attestation_error = _run_gh_json(
        [
            "gh",
            "attestation",
            "verify",
            str(path),
            "--repo",
            DEPLOYMENT_REPOSITORY,
            "--source-repo",
            DEPLOYMENT_REPOSITORY,
            "--source-digest",
            expected_revision,
            "--source-ref",
            DEPLOYMENT_TRUSTED_MAIN_REF,
            "--predicate-type",
            "https://slsa.dev/provenance/v1",
            "--digest-alg",
            "sha256",
            "--deny-self-hosted-runners",
            "--format",
            "json",
        ],
        timeout=60,
    )
    matching_attestations = [
        item
        for item in attestation
        if _attestation_result_matches(
            item,
            report_hash=report_hash,
            revision=expected_revision,
            workflow_ref=workflow_ref,
            run_id=run_id,
            run_attempt=run_attempt,
            now=current_time,
            subject_name=DEPLOYMENT_REPORT_NAME,
            repository=DEPLOYMENT_REPOSITORY,
            workflow_path=DEPLOYMENT_WORKFLOW,
        )
    ] if isinstance(attestation, list) else []
    if len(matching_attestations) != 1:
        return _deployment_result(
            "fail",
            f"Deployment attestation was not accepted: {attestation_error or 'no unique matching result'}.",
            report_hash=report_hash,
        )

    run_payload, run_error = _run_gh_json(
        ["gh", "api", "--method", "GET", f"repos/{DEPLOYMENT_REPOSITORY}/actions/runs/{run_id}"]
    )
    expected_run = {
        "id": run_id,
        "head_sha": expected_revision,
        "head_branch": "main",
        "event": "workflow_dispatch",
        "name": DEPLOYMENT_WORKFLOW_NAME,
        "path": DEPLOYMENT_WORKFLOW,
        "status": "completed",
        "conclusion": "success",
        "run_attempt": run_attempt,
    }
    if not isinstance(run_payload, dict) or any(run_payload.get(key) != value for key, value in expected_run.items()):
        return _deployment_result(
            "fail", f"Deployment workflow run is invalid: {run_error or 'identity mismatch'}.", report_hash=report_hash
        )
    head_repository = run_payload.get("head_repository")
    if not isinstance(head_repository, dict) or head_repository.get("full_name") != DEPLOYMENT_REPOSITORY:
        return _deployment_result("fail", "Deployment run repository is invalid.", report_hash=report_hash)
    run_times: dict[str, datetime] = {}
    for field in ("created_at", "run_started_at", "updated_at"):
        parsed, timestamp_error = _recent_timestamp(
            run_payload.get(field), now=current_time, maximum_age_hours=DEPLOYMENT_EVIDENCE_MAX_AGE_HOURS
        )
        if parsed is None:
            return _deployment_result(
                "fail", f"Deployment run {field} {timestamp_error}.", report_hash=report_hash
            )
        run_times[field] = parsed
    skew = timedelta(seconds=EVIDENCE_MAX_FUTURE_SKEW_SECONDS)
    if (
        not run_times["created_at"] <= run_times["run_started_at"] <= run_times["updated_at"]
        or collected_at < run_times["run_started_at"] - skew
        or collected_at > run_times["updated_at"] + skew
        or drill_times["restore drill"] < run_times["run_started_at"] - skew
        or drill_times["restore drill"] > run_times["updated_at"] + skew
        or drill_times["external probe"] < run_times["run_started_at"] - skew
        or drill_times["external probe"] > run_times["updated_at"] + skew
        or drill_times["rollback drill"] > collected_at + skew
    ):
        return _deployment_result("fail", "Deployment timestamps fall outside the workflow run.", report_hash=report_hash)

    jobs_payload, jobs_error = _run_gh_json(
        [
            "gh",
            "api",
            "--method",
            "GET",
            f"repos/{DEPLOYMENT_REPOSITORY}/actions/runs/{run_id}/jobs?filter=latest&per_page=100",
        ]
    )
    jobs = jobs_payload.get("jobs") if isinstance(jobs_payload, dict) else None
    jobs_total = jobs_payload.get("total_count") if isinstance(jobs_payload, dict) else None
    if not isinstance(jobs, list) or jobs_total != len(jobs) or jobs_total != 4:
        return _deployment_result(
            "fail", f"Deployment jobs are incomplete: {jobs_error or 'unexpected inventory'}.", report_hash=report_hash
        )
    by_name = {job.get("name"): job for job in jobs if isinstance(job, dict)}
    if set(by_name) != {
        DEPLOYMENT_PREPARE_JOB,
        DEPLOYMENT_COLLECTOR_JOB,
        DEPLOYMENT_EXTERNAL_PROBE_JOB,
        DEPLOYMENT_REVIEW_JOB,
    }:
        return _deployment_result("fail", "Deployment job names are not exact.", report_hash=report_hash)
    for job in by_name.values():
        if (
            job.get("status") != "completed"
            or job.get("conclusion") != "success"
            or job.get("head_sha") != expected_revision
            or job.get("run_attempt") != run_attempt
        ):
            return _deployment_result("fail", "A deployment evidence job did not succeed.", report_hash=report_hash)
    prepare_job = by_name[DEPLOYMENT_PREPARE_JOB]
    collector_job = by_name[DEPLOYMENT_COLLECTOR_JOB]
    external_probe_job = by_name[DEPLOYMENT_EXTERNAL_PROBE_JOB]
    review_job = by_name[DEPLOYMENT_REVIEW_JOB]
    if (
        "ubuntu-24.04" not in set(prepare_job.get("labels") or [])
        or "self-hosted" in set(prepare_job.get("labels") or [])
        or "ubuntu-24.04" not in set(external_probe_job.get("labels") or [])
        or "self-hosted" in set(external_probe_job.get("labels") or [])
        or "ubuntu-24.04" not in set(review_job.get("labels") or [])
        or "self-hosted" in set(review_job.get("labels") or [])
        or set(collector_job.get("labels") or []) != DEPLOYMENT_COLLECTOR_LABELS
        or not _release_job_steps_are_trusted(prepare_job, REQUIRED_DEPLOYMENT_PREPARE_STEPS)
        or not _release_job_steps_are_trusted(collector_job, REQUIRED_DEPLOYMENT_COLLECTOR_STEPS)
        or not _release_job_steps_are_trusted(
            external_probe_job,
            REQUIRED_DEPLOYMENT_EXTERNAL_PROBE_STEPS,
        )
        or not _release_job_steps_are_trusted(review_job, REQUIRED_DEPLOYMENT_REVIEW_STEPS)
    ):
        return _deployment_result(
            "fail", "Deployment runners, collector labels, or ordered safety steps are invalid.", report_hash=report_hash
        )

    artifacts_payload, artifacts_error = _run_gh_json(
        ["gh", "api", "--method", "GET", f"repos/{DEPLOYMENT_REPOSITORY}/actions/runs/{run_id}/artifacts?per_page=100"]
    )
    artifacts = artifacts_payload.get("artifacts") if isinstance(artifacts_payload, dict) else None
    artifacts_total = artifacts_payload.get("total_count") if isinstance(artifacts_payload, dict) else None
    if not isinstance(artifacts, list) or artifacts_total != len(artifacts) or artifacts_total > 100:
        return _deployment_result(
            "fail", f"Deployment artifacts are incomplete: {artifacts_error or 'invalid inventory'}.", report_hash=report_hash
        )
    matching_artifacts = [item for item in artifacts if isinstance(item, dict) and item.get("name") == artifact_name]
    if len(matching_artifacts) != 1:
        return _deployment_result("fail", "Deployment evidence artifact is not unique.", report_hash=report_hash)
    evidence_artifact = matching_artifacts[0]
    artifact_run = evidence_artifact.get("workflow_run")
    if (
        type(evidence_artifact.get("id")) is not int
        or evidence_artifact["id"] <= 0
        or evidence_artifact.get("expired") is not False
        or not isinstance(artifact_run, dict)
        or artifact_run.get("id") != run_id
        or artifact_run.get("head_sha") != expected_revision
    ):
        return _deployment_result("fail", "Deployment evidence artifact is expired or misbound.", report_hash=report_hash)
    for field in ("created_at", "updated_at"):
        artifact_time, artifact_time_error = _recent_timestamp(
            evidence_artifact.get(field),
            now=current_time,
            maximum_age_hours=DEPLOYMENT_EVIDENCE_MAX_AGE_HOURS,
        )
        if artifact_time is None or artifact_time < run_times["created_at"] - skew or artifact_time > current_time + skew:
            return _deployment_result(
                "fail",
                f"Deployment evidence artifact {field} {artifact_time_error or 'falls outside the trusted run window'}.",
                report_hash=report_hash,
            )

    live_release, release_error = _run_gh_json(
        ["gh", "api", "--method", "GET", f"repos/{DEPLOYMENT_REPOSITORY}/releases/tags/{tag}"]
    )
    live_assets = live_release.get("assets") if isinstance(live_release, dict) else None
    matching_assets = [item for item in live_assets if isinstance(item, dict) and item.get("name") == expected_asset_name] if isinstance(live_assets, list) else []
    if (
        not isinstance(live_release, dict)
        or live_release.get("id") != release_id
        or live_release.get("tag_name") != tag
        or live_release.get("target_commitish") != expected_revision
        or live_release.get("draft") is not False
        or live_release.get("prerelease") is not False
        or live_release.get("published_at") != release.get("published_at")
        or live_release.get("html_url") != expected_release_url
        or len(matching_assets) != 1
        or matching_assets[0].get("id") != asset.get("id")
        or matching_assets[0].get("state") != "uploaded"
        or matching_assets[0].get("size") != asset.get("size")
        or matching_assets[0].get("digest") != f"sha256:{asset.get('sha256')}"
    ):
        return _deployment_result(
            "fail", f"Current stable release no longer matches deployment evidence: {release_error or 'mismatch'}.", report_hash=report_hash
        )

    resolved_tag, tag_error = _resolve_release_tag_commit(tag)
    if resolved_tag != expected_revision:
        return _deployment_result(
            "fail", f"Deployment release tag is not immutable: {tag_error or 'mismatch'}.", report_hash=report_hash
        )
    branch, branch_error = _run_gh_json(
        ["gh", "api", "--method", "GET", f"repos/{DEPLOYMENT_REPOSITORY}/branches/main"]
    )
    branch_commit = branch.get("commit") if isinstance(branch, dict) else None
    if (
        not isinstance(branch, dict)
        or branch.get("protected") is not True
        or not isinstance(branch_commit, dict)
        or not _COMMIT_RE.fullmatch(str(branch_commit.get("sha") or ""))
    ):
        return _deployment_result(
            "fail", f"Deployment main branch is not protected: {branch_error or 'invalid'}.", report_hash=report_hash
        )
    comparison, comparison_error = _run_gh_json(
        ["gh", "api", "--method", "GET", f"repos/{DEPLOYMENT_REPOSITORY}/compare/{expected_revision}...main"]
    )
    base_commit = comparison.get("base_commit") if isinstance(comparison, dict) else None
    merge_base = comparison.get("merge_base_commit") if isinstance(comparison, dict) else None
    if (
        not isinstance(comparison, dict)
        or comparison.get("status") not in {"identical", "ahead"}
        or not isinstance(base_commit, dict)
        or base_commit.get("sha") != expected_revision
        or not isinstance(merge_base, dict)
        or merge_base.get("sha") != expected_revision
    ):
        return _deployment_result(
            "fail", f"Deployment revision is not on protected main: {comparison_error or 'invalid'}.", report_hash=report_hash
        )
    return {
        "status": "pass",
        "mode": "attested",
        "detail": (
            "Fresh canonical deployment evidence, exact release asset/frontend identity, production host lane, "
            "host-bound restore/rollback drills, independent GitHub-hosted public probing, hosted review, "
            "attestation, artifact, protected-main ancestry, and live release were verified."
        ),
        "report": {
            "sha256": report_hash,
            "raw_report_sha256": deployment["raw_report_sha256"],
            "external_probe_sha256": external_probe["raw_report_sha256"],
            "public_origin": canonical_origin,
            "release_tag": tag,
            "frontend_sha256": deployment["frontend_sha256"],
            "run_id": run_id,
        },
    }


def _deployment_evidence(
    path_value: str | None,
    *,
    expected_version: str,
    expected_revision: str,
    expected_origin: str | None,
) -> tuple[bool, str, dict[str, Any]]:
    """Use attested envelopes for points and preserve raw reports as diagnostics."""

    if _is_nonblank_string(path_value):
        path = Path(path_value).expanduser()
        try:
            raw = path.read_bytes() if path.is_file() and not path.is_symlink() else b""
            payload = _strict_json_bytes(raw, maximum_bytes=DEPLOYMENT_REPORT_MAX_BYTES)
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError, _DuplicateJsonKey):
            payload = None
        if isinstance(payload, dict) and payload.get("report_type") == DEPLOYMENT_REPORT_TYPE:
            result = _attested_deployment_report(
                path_value,
                expected_revision=expected_revision,
                expected_version=expected_version,
                expected_origin=expected_origin,
            )
            return result.get("status") == "pass", str(result.get("detail") or "Deployment evidence failed."), result
    diagnostic_ok, diagnostic_detail = _diagnostic_deployment_evidence(
        path_value,
        expected_version=expected_version,
        expected_revision=expected_revision,
    )
    return diagnostic_ok, diagnostic_detail, {
        "status": "diagnostic" if _is_nonblank_string(path_value) else "not_run",
        "mode": "raw",
        "detail": diagnostic_detail,
    }


def _diagnostic_live_evidence(
    path_value: str | None,
    *,
    target_tier: str,
    expected_revision: str,
) -> tuple[bool, str]:
    """Recompute live promotion locally while withholding points from unattested files."""

    label = "credentialed" if target_tier == "credential_live_verified" else "funded"
    if not _is_nonblank_string(path_value):
        return False, f"Provide GitHub-attested {label} Polymarket evidence."
    if not _COMMIT_RE.fullmatch(expected_revision or ""):
        return False, f"{label.capitalize()} Polymarket evidence requires an exact clean repository revision."
    path = Path(path_value).expanduser()
    try:
        if path.is_symlink() or not path.is_file():
            raise OSError("not a regular non-symbolic-link file")
        raw = path.read_bytes()
        payload = _strict_json_bytes(raw, maximum_bytes=PUBLIC_LIVE_REPORT_MAX_BYTES)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError, _DuplicateJsonKey) as exc:
        return False, f"{label.capitalize()} Polymarket report is not strict JSON: {type(exc).__name__}."
    if not isinstance(payload, dict):
        return False, f"{label.capitalize()} Polymarket report must be a JSON object."
    validation = validate_live_validation_report(payload)
    if validation.get("ok") is not True:
        return False, f"{label.capitalize()} Polymarket report failed schema validation."
    provenance = payload.get("source_provenance")
    if not isinstance(provenance, dict) or provenance.get("source_revision") != expected_revision:
        return False, f"{label.capitalize()} Polymarket report is not bound to the current source revision."
    promotion = live_validation_report_promotion(payload)
    promotion_field = (
        "can_promote_credential_live_verified"
        if target_tier == "credential_live_verified"
        else "can_promote_funded_live_verified"
    )
    if promotion.get(promotion_field) is not True:
        reasons = promotion.get("blocked_reasons")
        detail = "; ".join(str(item) for item in reasons) if isinstance(reasons, list) else "promotion failed"
        return False, f"{label.capitalize()} Polymarket report is not promotion-eligible: {detail}."
    return False, (
        f"{label.capitalize()} Polymarket report passed local promotion checks "
        f"(sha256={hashlib.sha256(raw).hexdigest()}) but is not score-eligible without a trusted "
        "GitHub workflow and exact-byte artifact attestation."
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compute a conservative MarketSentinel production-readiness score.")
    parser.add_argument(
        "--run-local",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run verify.py locally before awarding test and security points (default: enabled).",
    )
    parser.add_argument(
        "--full-local",
        action="store_true",
        help="Include frontend build and browser smoke in the local verification command.",
    )
    parser.add_argument(
        "--run-public-live",
        action="store_true",
        help=(
            "Run the no-credentials, public-only Polymarket probe for diagnostics. "
            "Only a GitHub-hosted, exact-revision, attested report earns readiness points."
        ),
    )
    parser.add_argument(
        "--public-live-report",
        help="Fresh GitHub-hosted public-only report with a verifiable artifact attestation.",
    )
    parser.add_argument(
        "--deployment-evidence",
        help="GitHub-attested deployment-evidence.json; raw production-host reports remain diagnostic-only.",
    )
    parser.add_argument(
        "--deployment-origin",
        help="Exact public HTTPS production origin bound into attested deployment evidence.",
    )
    parser.add_argument("--platform-ci-evidence", help="Reviewed JSON manifest for successful hosted platform CI lanes.")
    parser.add_argument("--platform-evidence", help="Reviewed JSON manifest for full platform evidence.")
    parser.add_argument("--repository-settings-evidence", help="Reviewed JSON manifest for GitHub settings evidence.")
    parser.add_argument("--release-environment-evidence", help="Reviewed JSON manifest for protected release-environment settings.")
    parser.add_argument(
        "--release-history-evidence",
        help="Fresh GitHub-attested release-evidence.json report for published release lineage.",
    )
    parser.add_argument(
        "--release-evidence",
        help="Fresh GitHub-attested release-evidence.json report for exact release assets.",
    )
    parser.add_argument(
        "--credentialed-evidence",
        help="Fresh trusted-workflow, exact-byte-attested credentialed Polymarket evidence.",
    )
    parser.add_argument(
        "--funded-evidence",
        help="Fresh trusted-workflow, exact-byte-attested funded order/cancel evidence.",
    )
    parser.add_argument("--minimum-score", type=int, default=0, help="Return failure when the score is below this value.")
    parser.add_argument("--require-100", action="store_true", help="Return failure unless every point is proven.")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print a redacted readiness report as JSON; evidence and probe payloads are omitted.",
    )
    return parser


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    repository_clean_initial = _repository_is_clean()
    repository_revision_initial = _repository_revision() if repository_clean_initial else ""
    local_result = (
        _run_local_gates(args.full_local)
        if args.run_local
        else {"status": "not_run", "profile": "full" if args.full_local else "core"}
    )
    direct_public_result = _run_public_live() if args.run_public_live else {"status": "not_run", "mode": "direct"}
    attested_public_result = _attested_public_live_report(
        args.public_live_report,
        expected_revision=repository_revision_initial,
    )
    direct_public_ok = direct_public_result.get("status") == "pass"
    attested_public_ok = attested_public_result.get("status") == "pass"
    public_result = {
        "status": "pass" if attested_public_ok else "fail" if args.public_live_report else "not_run",
        "award_source": "attested" if attested_public_ok else "none",
        "diagnostic_status": "pass" if direct_public_ok else direct_public_result.get("status", "not_run"),
        "direct": direct_public_result,
        "attested": attested_public_result,
    }
    project_version = _project_version()

    architecture_ok = _paths_exist(REQUIRED_ARCHITECTURE_FILES) and _contains(
        "README.md", ("capability", "Polymarket", "credential_live_verified")
    )
    architecture = _category(
        "architecture_scope",
        18 if architecture_ok else 0,
        "Required application modules, adapter catalog, capability documentation, and evidence audit are present."
        if architecture_ok
        else "Required architecture or capability documentation is missing.",
        [] if architecture_ok else [path for path in REQUIRED_ARCHITECTURE_FILES if not (ROOT / path).is_file()],
    )

    local_pass = local_result.get("status") == "pass"
    full_local_pass = local_pass and args.full_local
    tests = _category(
        "tests_correctness",
        18 if full_local_pass else 17 if local_pass else 0,
        (
            "The full local verification profile, frontend build, and browser smoke passed."
            if full_local_pass
            else "The core local verification profile passed; full browser/build coverage was not requested."
            if local_pass
            else "Run the local verification command successfully."
        ),
        (
            []
            if full_local_pass
            else ["Run the scorer with --full-local before claiming a 100/100 ready result."]
            if local_pass
            else ["python verify.py --skip-pip-check"]
        ),
    )
    security_ok = _paths_exist(REQUIRED_SECURITY_FILES) and local_pass
    security = _category(
        "security_safety",
        16 if security_ok else 0,
        "Security metadata, locked dependencies, guarded workflows, and secret-hygiene verification are present."
        if security_ok
        else "Security files or local security verification are incomplete.",
        [] if security_ok else ["SECURITY.md, lock files, CI workflow, and a passing local verifier"],
    )
    settings_result = _attested_trusted_evidence(
        args.repository_settings_evidence,
        "repository-settings",
        evidence_type="repository-settings",
        expected_revision=repository_revision_initial,
    )
    settings_ok = settings_result.get("status") == "pass"
    settings_detail = str(settings_result.get("detail") or "Repository-settings evidence failed.")
    if settings_ok:
        security["earned"] += 1
        security["basis"] += " " + settings_detail
    else:
        security["missing"].append(settings_detail)

    ci_ok = _paths_exist(REQUIRED_CI_FILES)
    release_environment_result = _attested_trusted_evidence(
        args.release_environment_evidence,
        "release-environment",
        evidence_type="release-environment",
        expected_revision=repository_revision_initial,
    )
    release_environment_ok = release_environment_result.get("status") == "pass"
    release_environment_detail = str(
        release_environment_result.get("detail") or "Release-environment evidence failed."
    )
    release_history_result = _attested_release_report(
        args.release_history_evidence,
        expected_revision=repository_revision_initial,
        expected_version=project_version,
    )
    release_result = (
        release_history_result
        if _is_nonblank_string(args.release_evidence)
        and args.release_evidence == args.release_history_evidence
        else _attested_release_report(
            args.release_evidence,
            expected_revision=repository_revision_initial,
            expected_version=project_version,
        )
    )
    release_history_ok = release_history_result.get("status") == "pass"
    release_history_detail = str(release_history_result.get("detail") or "Release history evidence failed.")
    release_ok = release_result.get("status") == "pass"
    release_detail = str(release_result.get("detail") or "Release evidence failed.")
    ci_cd = _category(
        "ci_cd_release",
        14 if ci_ok else 0,
        "CI, release workflow, provenance, checksum, SBOM, and distribution checks are present."
        if ci_ok
        else "Required CI/CD or release verification files are missing.",
        [] if ci_ok else [path for path in REQUIRED_CI_FILES if not (ROOT / path).is_file()],
    )
    if release_environment_ok:
        ci_cd["earned"] += 1
        ci_cd["basis"] += " " + release_environment_detail
    else:
        ci_cd["missing"].append(release_environment_detail)
    if release_history_ok:
        ci_cd["earned"] += 1
        ci_cd["basis"] += " " + release_history_detail
    else:
        ci_cd["missing"].append(release_history_detail)
    if release_ok:
        ci_cd["earned"] += 1
        ci_cd["basis"] += " " + release_detail
    else:
        ci_cd["missing"].append(release_detail)

    operations_ok = _paths_exist(REQUIRED_OPERATIONS_FILES)
    deployment_ok, deployment_detail, deployment_result = _deployment_evidence(
        args.deployment_evidence,
        expected_version=project_version,
        expected_revision=repository_revision_initial,
        expected_origin=getattr(args, "deployment_origin", None),
    )
    operations = _category(
        "operations_recovery",
        12 if operations_ok else 0,
        "Systemd, backup/restore, health, and production deployment verification artifacts are present."
        if operations_ok
        else "Required operations and recovery artifacts are missing.",
        [] if operations_ok else [path for path in REQUIRED_OPERATIONS_FILES if not (ROOT / path).is_file()],
    )
    if deployment_ok:
        operations["earned"] += 3
        operations["basis"] += " " + deployment_detail
    else:
        operations["missing"].append(deployment_detail)

    platform_ok = _paths_exist(REQUIRED_PLATFORM_FILES)
    platform_ci_result = _attested_trusted_evidence(
        args.platform_ci_evidence,
        "platform CI",
        evidence_type="platform-ci",
        expected_revision=repository_revision_initial,
    )
    platform_ci_ok = platform_ci_result.get("status") == "pass"
    platform_ci_detail = str(platform_ci_result.get("detail") or "Platform CI evidence failed.")
    platform_evidence_result = _attested_trusted_evidence(
        args.platform_evidence,
        "platform",
        evidence_type="platform",
        expected_revision=repository_revision_initial,
    )
    platform_evidence_ok = platform_evidence_result.get("status") == "pass"
    platform_detail = str(platform_evidence_result.get("detail") or "Platform evidence failed.")
    platform = _category(
        "platform_evidence",
        5 if platform_ok else 0,
        "Hosted compatibility lanes and an explicit non-overclaiming platform matrix are present."
        if platform_ok
        else "Platform matrix or verification tooling is missing.",
        [] if platform_ok else [path for path in REQUIRED_PLATFORM_FILES if not (ROOT / path).is_file()],
    )
    if platform_ci_ok:
        platform["earned"] += 3
        platform["basis"] += " " + platform_ci_detail
    else:
        platform["missing"].append(platform_ci_detail)
    if platform_evidence_ok:
        platform["earned"] += 2
        platform["basis"] += " " + platform_detail
    else:
        platform["missing"].append(platform_detail)

    live_ok = _paths_exist(REQUIRED_LIVE_FILES)
    # A local process, DNS resolver, proxy, or trust store is not an evidence
    # authority. Keep the direct probe useful for diagnosis, but award points
    # only to the exact-revision GitHub-hosted artifact and attestation path.
    public_ok = attested_public_ok
    credentialed_ok, credentialed_detail, credentialed_result = _live_evidence(
        args.credentialed_evidence,
        target_tier="credential_live_verified",
        expected_revision=repository_revision_initial,
    )
    funded_ok, funded_detail, funded_result = _live_evidence(
        args.funded_evidence,
        target_tier="funded_live_verified",
        expected_revision=repository_revision_initial,
    )
    live = _category(
        "live_acceptance",
        3 if live_ok and public_ok else 0,
        "Polymarket guarded live-validation tooling and public-only evidence passed."
        if live_ok and public_ok
        else "Collect GitHub-attested public-only evidence and keep credentialed/funded stages fail-closed.",
        []
        if live_ok and public_ok
        else ["Run .github/workflows/polymarket-evidence.yml public-only evidence on the exact protected-main revision."],
    )
    if credentialed_ok:
        live["earned"] += 1
        live["basis"] += " " + credentialed_detail
    else:
        live["missing"].append(credentialed_detail)
    if funded_ok:
        live["earned"] += 1
        live["basis"] += " " + funded_detail
    else:
        live["missing"].append(funded_detail)

    repository_clean_final = _repository_is_clean()
    repository_revision_final = _repository_revision() if repository_clean_final else ""
    repository_stable = (
        repository_clean_initial
        and repository_clean_final
        and bool(repository_revision_initial)
        and repository_revision_initial == repository_revision_final
    )
    if not repository_clean_initial:
        repository_detail = "The initial worktree check was not clean."
    elif not repository_revision_initial:
        repository_detail = "The initial repository revision was unavailable."
    elif not repository_clean_final:
        repository_detail = "The worktree changed or became dirty while readiness evidence was evaluated."
    elif not repository_revision_final:
        repository_detail = "The final repository revision was unavailable."
    elif repository_revision_initial != repository_revision_final:
        repository_detail = "The repository revision changed while readiness evidence was evaluated."
    else:
        repository_detail = "The worktree stayed clean at the same revision throughout evidence evaluation."

    if not repository_stable:
        stability_reason = "the final clean/revision check did not match the initial repository identity"
        revision_bound_awards = (
            (settings_ok, security, 1, "repository settings"),
            (release_environment_ok, ci_cd, 1, "release environment"),
            (release_history_ok, ci_cd, 1, "release history"),
            (release_ok, ci_cd, 1, "release"),
            (deployment_ok, operations, 3, "deployment"),
            (platform_ci_ok, platform, 3, "platform CI"),
            (platform_evidence_ok, platform, 2, "platform"),
            (public_ok, live, 3, "public Polymarket"),
            (credentialed_ok, live, 1, "credentialed Polymarket"),
            (funded_ok, live, 1, "funded Polymarket"),
        )
        for was_accepted, category, points, label in revision_bound_awards:
            if was_accepted:
                category["earned"] -= points
                category["missing"].append(f"Accepted {label} evidence was revoked because {stability_reason}.")

    categories = [architecture, tests, security, ci_cd, operations, platform, live]
    score = sum(int(item["earned"]) for item in categories)
    missing = [detail for item in categories for detail in item["missing"] if detail]
    return {
        "score": score,
        "out_of": 100,
        "status": "ready" if score == 100 else "not_ready",
        "categories": categories,
        "missing": missing,
        "checks": {
            "repository": {
                "status": "pass" if repository_stable else "fail",
                "initial_clean": repository_clean_initial,
                "final_clean": repository_clean_final,
                "initial_revision": repository_revision_initial,
                "final_revision": repository_revision_final,
                "revision": repository_revision_final if repository_stable else "",
                "detail": repository_detail,
            },
            "local": local_result,
            "public_live": public_result,
            "release_evidence": {
                "history": release_history_result,
                "release": release_result,
            },
            "deployment_evidence": deployment_result,
            "repository_settings_evidence": settings_result,
            "release_environment_evidence": release_environment_result,
            "platform_ci_evidence": platform_ci_result,
            "platform_evidence": platform_evidence_result,
            "credentialed_evidence": credentialed_result,
            "funded_evidence": funded_result,
        },
        "scope": "Repository readiness plus explicitly supplied external evidence; not a certification.",
    }


_PUBLIC_REPORT_MISSING_MESSAGES = {
    "architecture_scope": "Required architecture or capability documentation is missing.",
    "tests_correctness": "Run the full local verification profile before claiming a ready result.",
    "security_safety": "Fresh trusted repository-settings evidence is required.",
    "ci_cd_release": "Attested release evidence is required for the current release.",
    "operations_recovery": "Attested production deployment evidence is required.",
    "platform_evidence": "Hosted platform CI and platform evidence are required; evidence_type='platform-ci' must be declared.",
    "live_acceptance": "Attested public, credentialed, and funded live evidence is required.",
}


def _safe_status(value: Any) -> str:
    """Return a fixed, non-sensitive status suitable for command-line output."""

    if value == "pass":
        return "pass"
    if value == "fail":
        return "fail"
    return "not_run"


def _safe_report_for_output(report: dict[str, Any]) -> dict[str, Any]:
    """Build a redacted CLI report without serializing evidence or probe payloads.

    The internal report contains paths, workflow responses, hashes, and details
    derived from user-supplied evidence files.  None of those values belong in
    logs.  Keep the public shape useful while exposing only fixed labels,
    bounded score integers, and coarse statuses.
    """

    raw_categories = report.get("categories")
    category_by_name: dict[str, dict[str, Any]] = {}
    if isinstance(raw_categories, list):
        for item in raw_categories:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if name in CATEGORY_WEIGHTS and name not in category_by_name:
                category_by_name[name] = item

    categories: list[dict[str, Any]] = []
    for name, possible in CATEGORY_WEIGHTS.items():
        item = category_by_name.get(name)
        earned_value = item.get("earned") if isinstance(item, dict) else None
        earned = earned_value if type(earned_value) is int else 0
        earned = max(0, min(earned, possible))
        missing = [_PUBLIC_REPORT_MISSING_MESSAGES[name]] if earned < possible else []
        categories.append(
            {
                "name": name,
                "earned": earned,
                "possible": possible,
                "missing": missing,
            }
        )

    score = sum(item["earned"] for item in categories)
    raw_checks = report.get("checks")
    safe_checks: dict[str, dict[str, Any]] = {}
    if isinstance(raw_checks, dict):
        for name in (
            "local",
            "public_live",
            "release_evidence",
            "deployment_evidence",
            "repository_settings_evidence",
            "release_environment_evidence",
            "platform_ci_evidence",
            "platform_evidence",
            "credentialed_evidence",
            "funded_evidence",
        ):
            check = raw_checks.get(name)
            if isinstance(check, dict):
                safe_checks[name] = {"status": _safe_status(check.get("status"))}
        repository = raw_checks.get("repository")
        if isinstance(repository, dict):
            safe_checks["repository"] = {
                "status": _safe_status(repository.get("status")),
                "initial_clean": repository.get("initial_clean") is True,
                "final_clean": repository.get("final_clean") is True,
                "revision_stable": (
                    repository.get("initial_clean") is True
                    and repository.get("final_clean") is True
                    and bool(repository.get("initial_revision"))
                    and repository.get("initial_revision") == repository.get("final_revision")
                ),
            }

    missing = [detail for category in categories for detail in category["missing"]]
    return {
        "score": score,
        "out_of": 100,
        "status": "ready" if score == 100 else "not_ready",
        "categories": categories,
        "missing": missing,
        "checks": safe_checks,
        "scope": "Repository readiness plus explicitly supplied external evidence; not a certification.",
    }


def main() -> int:
    args = _parser().parse_args()
    report = build_report(args)
    public_report = _safe_report_for_output(report)
    if args.json:
        print(json.dumps(public_report, indent=2, sort_keys=True))
    else:
        print(f"Production readiness: {public_report['score']}/{public_report['out_of']}")
        for category in public_report["categories"]:
            print(f"- {category['name']}: {category['earned']}/{category['possible']}")
        if public_report["missing"]:
            print("Missing evidence:")
            for item in public_report["missing"]:
                print(f"- {item}")
    minimum = 100 if args.require_100 else max(0, args.minimum_score)
    return 0 if report["score"] >= minimum else 1


if __name__ == "__main__":
    raise SystemExit(main())
