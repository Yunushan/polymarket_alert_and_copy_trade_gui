from __future__ import annotations

"""Compute a conservative, reproducible MarketSentinel readiness score.

The local checks can be run from a clean checkout. External evidence is never
inferred from CI configuration: operators must provide reviewed JSON evidence
manifests before the deployment, platform, repository-setting, credentialed,
or funded points are awarded. Public-live points may instead use a narrowly
scoped report whose exact bytes, workflow identity, source revision, and
GitHub-hosted execution are verified through GitHub artifact attestations.
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 compatibility.
    import tomli as tomllib


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
    ".github/workflows/release.yml",
)

REQUIRED_CI_FILES = (
    ".github/actionlint.yaml",
    ".github/workflows/ci.yml",
    ".github/workflows/release.yml",
    "scripts/verify_release_provenance.py",
    "scripts/verify_release_assets.py",
    "scripts/generate_release_sbom.py",
    "scripts/verify_python_dist_artifacts.py",
)

REQUIRED_OPERATIONS_FILES = (
    "docs/PRODUCTION_OPERATIONS.md",
    "deploy/systemd/market-sentinel-web.service",
    "deploy/systemd/market-sentinel-health.service",
    "deploy/systemd/market-sentinel-backup.service",
    "scripts/verify_production_deployment.py",
    "scripts/backup_state.py",
    "scripts/restore_state_backup.py",
)

REQUIRED_PLATFORM_FILES = (
    "docs/PLATFORM_SUPPORT.md",
    "scripts/verify_platform_support.py",
    ".github/workflows/ci.yml",
)

REQUIRED_LIVE_FILES = (
    "polymarket/live_verification.py",
    "polymarket/credential_runbook.py",
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


def _recent_timestamp(value: Any, *, now: datetime) -> tuple[datetime | None, str]:
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
    if age > timedelta(hours=PUBLIC_LIVE_EVIDENCE_MAX_AGE_HOURS):
        return None, f"is older than {PUBLIC_LIVE_EVIDENCE_MAX_AGE_HOURS} hours"
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
    if subject.get("name") != PUBLIC_LIVE_REPORT_NAME:
        return False
    digest = subject.get("digest")
    if not isinstance(digest, dict) or set(digest) != {"sha256"} or digest.get("sha256") != report_hash:
        return False

    certificate = signature.get("certificate")
    if not isinstance(certificate, dict):
        return False
    ref = workflow_ref.split("@", 1)[1]
    cert_workflow_uri = f"https://github.com/{workflow_ref}"
    repository_uri = f"https://github.com/{PUBLIC_LIVE_REPOSITORY}"
    invocation_uri = (
        f"https://github.com/{PUBLIC_LIVE_REPOSITORY}/actions/runs/{run_id}/attempts/{run_attempt}"
    )
    certificate_contract = {
        "subjectAlternativeName": cert_workflow_uri,
        "issuer": "https://token.actions.githubusercontent.com",
        "buildSignerURI": cert_workflow_uri,
        "buildSignerDigest": revision,
        "runnerEnvironment": "github-hosted",
        "sourceRepositoryURI": repository_uri,
        "sourceRepositoryDigest": revision,
        "sourceRepositoryRef": ref,
        "sourceRepositoryOwnerURI": "https://github.com/Yunushan",
        "buildConfigURI": cert_workflow_uri,
        "buildConfigDigest": revision,
        "buildTrigger": "workflow_dispatch",
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
    if workflow != {"path": PUBLIC_LIVE_WORKFLOW, "ref": ref, "repository": repository_uri}:
        return False
    if github.get("event_name") != "workflow_dispatch" or github.get("runner_environment") != "github-hosted":
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
    signer_workflow = f"{PUBLIC_LIVE_REPOSITORY}/{PUBLIC_LIVE_WORKFLOW}"
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
                "--signer-workflow",
                signer_workflow,
                "--cert-identity",
                f"https://github.com/{workflow_ref}",
                "--cert-oidc-issuer",
                "https://token.actions.githubusercontent.com",
                "--signer-digest",
                expected_revision,
                "--source-digest",
                expected_revision,
                "--source-ref",
                workflow_ref.split("@", 1)[1],
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


_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_REPOSITORY_SETTINGS_CHECKS = (
    "branch_required_status_checks",
    "branch_require_up_to_date",
    "branch_enforce_admins",
    "branch_require_pull_request",
    "branch_conversation_resolution",
    "branch_linear_history",
    "branch_force_pushes_disabled",
    "branch_deletions_disabled",
)
REQUIRED_RELEASE_ENVIRONMENT_CHECKS = (
    "release_required_reviewers",
    "release_prevent_self_review",
    "release_protected_branches",
    "release_signing_secrets",
    "release_windows_code_signing_required",
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
    "release-history": REQUIRED_RELEASE_HISTORY_CHECKS,
    "release": REQUIRED_RELEASE_CHECKS,
    "deployment": REQUIRED_DEPLOYMENT_CHECKS,
    "platform-ci": REQUIRED_PLATFORM_CI_CHECKS,
    "platform": REQUIRED_PLATFORM_CHECKS,
    "credentialed-polymarket": REQUIRED_CREDENTIALED_POLYMARKET_CHECKS,
    "funded-polymarket": REQUIRED_FUNDED_POLYMARKET_CHECKS,
}
REQUIRED_EVIDENCE_FIELDS = {
    "repository-settings": ("source",),
    "release-environment": ("source",),
    "release-history": ("source", "scope", "tag", "target_commit"),
    "release": ("source", "scope", "tag", "target_commit", "assets"),
    "deployment": ("source", "scope", "environment", "expected_version", "source_revision"),
    "platform-ci": ("source", "scope", "run_id", "source_revision"),
    "platform": ("source", "scope", "targets", "source_revision"),
    "credentialed-polymarket": ("source", "scope", "target_tier", "report_hash", "source_revision"),
    "funded-polymarket": (
        "source",
        "scope",
        "target_tier",
        "report_hash",
        "live_action",
        "source_revision",
    ),
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
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
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
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and not result.stdout.strip()


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
    return True, f"Reviewed {label} evidence accepted."


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
        help="Run the no-credentials, public-only Polymarket readiness probe.",
    )
    parser.add_argument(
        "--public-live-report",
        help="Fresh GitHub-hosted public-only report with a verifiable artifact attestation.",
    )
    parser.add_argument("--deployment-evidence", help="Reviewed JSON manifest for a real production deployment.")
    parser.add_argument("--platform-ci-evidence", help="Reviewed JSON manifest for successful hosted platform CI lanes.")
    parser.add_argument("--platform-evidence", help="Reviewed JSON manifest for full platform evidence.")
    parser.add_argument("--repository-settings-evidence", help="Reviewed JSON manifest for GitHub settings evidence.")
    parser.add_argument("--release-environment-evidence", help="Reviewed JSON manifest for protected release-environment settings.")
    parser.add_argument("--release-history-evidence", help="Reviewed JSON manifest for an existing published release lineage.")
    parser.add_argument("--release-evidence", help="Reviewed JSON manifest for a published release and artifact evidence.")
    parser.add_argument("--credentialed-evidence", help="Reviewed JSON manifest for authenticated Polymarket evidence.")
    parser.add_argument("--funded-evidence", help="Reviewed JSON manifest for an approved funded order/cancel audit.")
    parser.add_argument("--minimum-score", type=int, default=0, help="Return failure when the score is below this value.")
    parser.add_argument("--require-100", action="store_true", help="Return failure unless every point is proven.")
    parser.add_argument("--json", action="store_true", help="Print the complete report as JSON.")
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
        "status": "pass" if direct_public_ok or attested_public_ok else "fail" if args.run_public_live or args.public_live_report else "not_run",
        "award_source": (
            "direct_and_attested"
            if direct_public_ok and attested_public_ok
            else "direct"
            if direct_public_ok
            else "attested"
            if attested_public_ok
            else "none"
        ),
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
    settings_ok, settings_detail = _reviewed_evidence(
        args.repository_settings_evidence,
        "repository-settings",
        evidence_type="repository-settings",
        required_fields=("source",),
    )
    if settings_ok:
        security["earned"] += 1
        security["basis"] += " " + settings_detail
    else:
        security["missing"].append(settings_detail)

    ci_ok = _paths_exist(REQUIRED_CI_FILES)
    release_environment_ok, release_environment_detail = _reviewed_evidence(
        args.release_environment_evidence,
        "release-environment",
        evidence_type="release-environment",
        required_fields=("source",),
        required_checks=REQUIRED_RELEASE_ENVIRONMENT_CHECKS,
    )
    release_history_ok, release_history_detail = _reviewed_evidence(
        args.release_history_evidence,
        "release history",
        evidence_type="release-history",
        required_fields=("scope", "tag", "target_commit"),
        expected_fields={"tag": f"v{project_version}"},
        revision_field="target_commit",
        expected_revision=repository_revision_initial,
    )
    release_ok, release_detail = _reviewed_evidence(
        args.release_evidence,
        "release",
        evidence_type="release",
        required_fields=("scope", "tag", "target_commit", "assets"),
        expected_fields={"tag": f"v{project_version}"},
        revision_field="target_commit",
        expected_revision=repository_revision_initial,
    )
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
    deployment_ok, deployment_detail = _reviewed_evidence(
        args.deployment_evidence,
        "deployment",
        evidence_type="deployment",
        required_fields=("scope", "environment", "expected_version", "source_revision"),
        expected_fields={"expected_version": project_version},
        revision_field="source_revision",
        expected_revision=repository_revision_initial,
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
    platform_ci_ok, platform_ci_detail = _reviewed_evidence(
        args.platform_ci_evidence,
        "platform CI",
        evidence_type="platform-ci",
        required_fields=("scope", "run_id", "source_revision"),
        revision_field="source_revision",
        expected_revision=repository_revision_initial,
    )
    platform_evidence_ok, platform_detail = _reviewed_evidence(
        args.platform_evidence,
        "platform",
        evidence_type="platform",
        required_fields=("scope", "targets", "source_revision"),
        revision_field="source_revision",
        expected_revision=repository_revision_initial,
    )
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
    public_ok = direct_public_ok or attested_public_ok
    credentialed_ok, credentialed_detail = _reviewed_evidence(
        args.credentialed_evidence,
        "credentialed Polymarket",
        evidence_type="credentialed-polymarket",
        required_fields=("scope", "target_tier", "report_hash", "source_revision"),
        expected_fields={"target_tier": "credential_live_verified"},
        revision_field="source_revision",
        expected_revision=repository_revision_initial,
    )
    funded_ok, funded_detail = _reviewed_evidence(
        args.funded_evidence,
        "funded Polymarket",
        evidence_type="funded-polymarket",
        required_fields=("scope", "target_tier", "report_hash", "live_action", "source_revision"),
        expected_fields={"target_tier": "funded_live_verified"},
        revision_field="source_revision",
        expected_revision=repository_revision_initial,
    )
    live = _category(
        "live_acceptance",
        3 if live_ok and public_ok else 0,
        "Polymarket guarded live-validation tooling and public-only evidence passed."
        if live_ok and public_ok
        else "Run the public-only live probe and keep credentialed/funded stages fail-closed.",
        [] if live_ok and public_ok else ["python scripts/verify_polymarket_live.py --skip-authenticated-read-checks"],
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
                category["missing"].append(f"Reviewed {label} evidence was revoked because {stability_reason}.")

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
        },
        "scope": "Repository readiness plus explicitly supplied external evidence; not a certification.",
    }


def main() -> int:
    args = _parser().parse_args()
    report = build_report(args)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"Production readiness: {report['score']}/{report['out_of']}")
        for category in report["categories"]:
            print(f"- {category['name']}: {category['earned']}/{category['possible']}")
        if report["missing"]:
            print("Missing evidence:")
            for item in report["missing"]:
                print(f"- {item}")
    minimum = 100 if args.require_100 else max(0, args.minimum_score)
    return 0 if report["score"] >= minimum else 1


if __name__ == "__main__":
    raise SystemExit(main())
