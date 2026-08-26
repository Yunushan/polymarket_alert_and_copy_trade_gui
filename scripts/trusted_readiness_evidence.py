from __future__ import annotations

"""Build and validate narrowly scoped, attestable production-readiness evidence.

This module deliberately separates semantic evidence validation from online
GitHub provenance verification.  A manifest built here is still not
score-eligible until ``check_product_readiness.py`` verifies its exact-byte
artifact attestation, workflow run, hosted job, artifact, and source revision.
"""

import argparse
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = 2
EVIDENCE_METADATA_SCHEMA_VERSION = 1
REPORT_TYPE = "market-sentinel-trusted-readiness-evidence"
MAX_EVIDENCE_BYTES = 1024 * 1024
MAX_FUTURE_SKEW_SECONDS = 5 * 60
MAX_AGE_HOURS = 24
REPOSITORY = "Yunushan/market-sentinel"
TRUSTED_REF = "refs/heads/main"

REPOSITORY_SETTINGS_CHECKS = (
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
RELEASE_ENVIRONMENT_CHECKS = (
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
PLATFORM_CI_CHECKS = (
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
PLATFORM_CHECKS = (
    "windows_hosted_python_and_smoke",
    "windows_11_arm_python_and_smoke",
    "ubuntu_python_tkinter_and_verifier",
    "macos_14_15_26_python_and_verifier",
)
CREDENTIALED_POLYMARKET_CHECKS = (
    "report_integrity",
    "source_revision",
    "public_live_checks",
    "credentialed_read_checks",
    "credential_live_verified",
)
FUNDED_POLYMARKET_CHECKS = (
    "report_integrity",
    "source_revision",
    "public_live_checks",
    "credentialed_read_checks",
    "funded_order_cancel",
    "post_cancel_verified",
    "funded_live_verified",
)

REQUIRED_CHECKS: dict[str, tuple[str, ...]] = {
    "repository-settings": REPOSITORY_SETTINGS_CHECKS,
    "release-environment": RELEASE_ENVIRONMENT_CHECKS,
    "platform-ci": PLATFORM_CI_CHECKS,
    "platform": PLATFORM_CHECKS,
    "credentialed-polymarket": CREDENTIALED_POLYMARKET_CHECKS,
    "funded-polymarket": FUNDED_POLYMARKET_CHECKS,
}

WORKFLOW_CONTRACTS: dict[str, dict[str, Any]] = {
    "repository-settings": {
        "workflow": ".github/workflows/governance-evidence.yml",
        "workflow_name": "Governance evidence",
        "job": "Collect and attest governance evidence",
        "subject_name": "repository-settings-evidence.json",
        "required_steps": (
            "Verify exact clean source before collection",
            "Collect live repository and release-environment controls",
            "Generate exact governance evidence",
            "Review governance evidence before attestation",
            "Reverify exact clean source after collection",
            "Attest repository-settings evidence",
            "Attest release-environment evidence",
            "Upload repository-settings evidence",
            "Upload release-environment evidence",
        ),
    },
    "release-environment": {
        "workflow": ".github/workflows/governance-evidence.yml",
        "workflow_name": "Governance evidence",
        "job": "Collect and attest governance evidence",
        "subject_name": "release-environment-evidence.json",
        "required_steps": (
            "Verify exact clean source before collection",
            "Collect live repository and release-environment controls",
            "Generate exact governance evidence",
            "Review governance evidence before attestation",
            "Reverify exact clean source after collection",
            "Attest repository-settings evidence",
            "Attest release-environment evidence",
            "Upload repository-settings evidence",
            "Upload release-environment evidence",
        ),
    },
    "platform-ci": {
        "workflow": ".github/workflows/platform-evidence.yml",
        "workflow_name": "Platform evidence",
        "job": "Review and attest platform evidence",
        "subject_name": "platform-ci-evidence.json",
        "required_steps": (
            "Verify exact clean source before review",
            "Download exact CI run metadata",
            "Generate exact platform evidence",
            "Review platform evidence before attestation",
            "Reverify exact clean source after review",
            "Attest platform-CI evidence",
            "Attest platform evidence",
            "Upload platform-CI evidence",
            "Upload platform evidence",
        ),
    },
    "platform": {
        "workflow": ".github/workflows/platform-evidence.yml",
        "workflow_name": "Platform evidence",
        "job": "Review and attest platform evidence",
        "subject_name": "platform-evidence.json",
        "required_steps": (
            "Verify exact clean source before review",
            "Download exact CI run metadata",
            "Generate exact platform evidence",
            "Review platform evidence before attestation",
            "Reverify exact clean source after review",
            "Attest platform-CI evidence",
            "Attest platform evidence",
            "Upload platform-CI evidence",
            "Upload platform evidence",
        ),
    },
    "credentialed-polymarket": {
        "workflow": ".github/workflows/polymarket-evidence.yml",
        "workflow_name": "Polymarket acceptance evidence",
        "job": "Credentialed Polymarket evidence",
        "subject_name": "credentialed-polymarket-evidence.json",
        "required_steps": (
            "Verify credentialed evidence mode",
            "Verify exact clean source before probe",
            "Run credentialed public and authenticated reads",
            "Generate exact credentialed evidence",
            "Review credentialed evidence before attestation",
            "Reverify exact clean source after probe",
            "Attest credentialed evidence",
            "Upload credentialed evidence",
        ),
    },
    "funded-polymarket": {
        "workflow": ".github/workflows/polymarket-evidence.yml",
        "workflow_name": "Polymarket acceptance evidence",
        "job": "Funded Polymarket order/cancel evidence",
        "subject_name": "funded-polymarket-evidence.json",
        "required_steps": (
            "Verify funded execution approval",
            "Verify exact clean source before funded action",
            "Run bounded funded order and immediate cancel",
            "Generate exact funded evidence",
            "Review funded evidence before attestation",
            "Reverify exact clean source after funded action",
            "Attest funded evidence",
            "Upload funded evidence",
        ),
    },
}

_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_COMMON_FIELDS = frozenset(
    {
        "schema_version",
        "report_type",
        "evidence_type",
        "verified",
        "source",
        "scope",
        "source_revision",
        "checks",
        "evidence",
    }
)
_TYPE_FIELDS = {
    "repository-settings": _COMMON_FIELDS,
    "release-environment": _COMMON_FIELDS,
    "platform-ci": _COMMON_FIELDS | {"source_run_id"},
    "platform": _COMMON_FIELDS | {"source_run_id", "targets"},
    "credentialed-polymarket": _COMMON_FIELDS
    | {"target_tier", "report_sha256", "live_action", "live_report"},
    "funded-polymarket": _COMMON_FIELDS
    | {"target_tier", "report_sha256", "live_action", "live_report"},
}
_EVIDENCE_FIELDS = frozenset(
    {
        "schema_version",
        "repository",
        "source_revision",
        "run_id",
        "run_attempt",
        "workflow",
        "workflow_name",
        "workflow_ref",
        "event",
        "runner_environment",
        "job",
        "artifact_name",
        "generated_at",
    }
)
_EXACT_SCOPES = {
    "repository-settings": "Live protected-main repository controls",
    "release-environment": "Live protected release/production environment controls and signing prerequisites",
    "platform-ci": "Successful exact-revision hosted CI compatibility lanes",
    "platform": "Exact-revision hosted desktop platform evidence",
    "credentialed-polymarket": "Exact-revision credentialed Polymarket read acceptance",
    "funded-polymarket": "Exact-revision bounded funded Polymarket order/cancel acceptance",
}
_PLATFORM_TARGETS = ("Windows", "Windows 11", "Ubuntu Linux", "macOS")

_PYTHON_VERSIONS = ("3.10", "3.11", "3.12", "3.13", "3.14")
_PYTHON_OSES = ("ubuntu-latest", "macos-14", "macos-15", "macos-26", "windows-2025-vs2026")
_STABLE_PYTHON_JOBS = tuple(
    f"Python {version} / {operating_system}"
    for operating_system in _PYTHON_OSES
    for version in _PYTHON_VERSIONS
)
_FUTURE_PYTHON_JOBS = tuple(f"Future Python 3.x / {operating_system}" for operating_system in _PYTHON_OSES)
_RHEL_JOBS = (
    "RHEL 8 UBI / Python 3.12",
    "RHEL 9 UBI / Python 3.12",
    "RHEL 10 UBI / Python 3.12 minimal",
    "RHEL 7 ABI / manylinux2014 Python 3.10",
)
_ROCKY_JOBS = (
    "Rocky Linux 8 / Python 3.12",
    "Rocky Linux 9 / Python 3.12",
    "Rocky Linux 10 / Python 3.12",
)
_MOBILE_JOBS = tuple(
    f"Mobile web smoke / {target}"
    for target in ("android-14", "android-15", "android-16", "ios-15", "ios-16", "ios-18", "ios-26")
)


class TrustedEvidenceError(ValueError):
    """Raised when untrusted input cannot produce a trusted evidence candidate."""


class _DuplicateJsonKey(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise _DuplicateJsonKey(f"duplicate JSON key: {key}")
        output[key] = value
    return output


def _reject_nonfinite(value: str) -> Any:
    raise ValueError(f"non-finite JSON number: {value}")


def strict_json_bytes(raw: bytes, *, maximum_bytes: int = MAX_EVIDENCE_BYTES) -> Any:
    if not raw or len(raw) > maximum_bytes:
        raise ValueError("JSON input is empty or exceeds the size limit")
    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_nonfinite,
    )


def load_strict_json(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ValueError("evidence input must be a regular non-symbolic-link file")
    return strict_json_bytes(path.read_bytes())


def canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def artifact_name(evidence_type: str, revision: str, run_id: int, run_attempt: int) -> str:
    return f"{evidence_type}-evidence-{revision}-{run_id}-{run_attempt}"


def _utc_timestamp(value: datetime | None = None) -> str:
    current = (value or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return current.isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any, *, now: datetime, errors: list[str]) -> datetime | None:
    if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value:
        errors.append("evidence.generated_at must be a non-empty single-line ISO-8601 timestamp")
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        errors.append("evidence.generated_at must be valid ISO-8601")
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        errors.append("evidence.generated_at must include a timezone")
        return None
    normalized = parsed.astimezone(timezone.utc)
    age = now.astimezone(timezone.utc) - normalized
    if age < -timedelta(seconds=MAX_FUTURE_SKEW_SECONDS):
        errors.append("evidence.generated_at is in the future")
    if age > timedelta(hours=MAX_AGE_HOURS):
        errors.append(f"evidence.generated_at is older than {MAX_AGE_HOURS} hours")
    return normalized


def _require_revision(value: str) -> str:
    candidate = str(value or "").strip().lower()
    if not _COMMIT_RE.fullmatch(candidate):
        raise TrustedEvidenceError("source revision must be a lowercase 40-character commit SHA")
    return candidate


def _require_positive_integer(value: Any, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise TrustedEvidenceError(f"{label} must be a positive integer")
    return value


def _evidence_metadata(
    evidence_type: str,
    *,
    repository: str,
    source_revision: str,
    run_id: int,
    run_attempt: int,
    workflow_ref: str,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    if repository != REPOSITORY or not _REPOSITORY_RE.fullmatch(repository):
        raise TrustedEvidenceError(f"repository must equal {REPOSITORY}")
    revision = _require_revision(source_revision)
    run = _require_positive_integer(run_id, "run_id")
    attempt = _require_positive_integer(run_attempt, "run_attempt")
    contract = WORKFLOW_CONTRACTS[evidence_type]
    expected_ref = f"{REPOSITORY}/{contract['workflow']}@{TRUSTED_REF}"
    if workflow_ref != expected_ref:
        raise TrustedEvidenceError(f"workflow_ref must equal {expected_ref}")
    return {
        "schema_version": EVIDENCE_METADATA_SCHEMA_VERSION,
        "repository": REPOSITORY,
        "source_revision": revision,
        "run_id": run,
        "run_attempt": attempt,
        "workflow": contract["workflow"],
        "workflow_name": contract["workflow_name"],
        "workflow_ref": expected_ref,
        "event": "workflow_dispatch",
        "runner_environment": "github-hosted",
        "job": contract["job"],
        "artifact_name": artifact_name(evidence_type, revision, run, attempt),
        "generated_at": _utc_timestamp(generated_at),
    }


def _base_manifest(
    evidence_type: str,
    *,
    source: str,
    source_revision: str,
    checks: Iterable[str],
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "report_type": REPORT_TYPE,
        "evidence_type": evidence_type,
        "verified": True,
        "source": source,
        "scope": _EXACT_SCOPES[evidence_type],
        "source_revision": source_revision,
        "checks": [{"name": name, "status": "pass"} for name in checks],
        "evidence": dict(metadata),
    }


def _validated_raw_governance(payload: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, Mapping) or set(payload) != {"repository", "branch", "status", "checks"}:
        raise TrustedEvidenceError("governance collector output does not match the exact schema")
    if payload.get("repository") != REPOSITORY or payload.get("branch") != "main" or payload.get("status") != "ok":
        raise TrustedEvidenceError("governance collector identity or status is invalid")
    checks = payload.get("checks")
    if not isinstance(checks, list):
        raise TrustedEvidenceError("governance collector checks must be an array")
    indexed: dict[str, dict[str, Any]] = {}
    allowed = set(REPOSITORY_SETTINGS_CHECKS) | set(RELEASE_ENVIRONMENT_CHECKS)
    for item in checks:
        if not isinstance(item, Mapping) or set(item) != {"name", "status", "detail"}:
            raise TrustedEvidenceError("governance collector check does not match the exact schema")
        name = item.get("name")
        if not isinstance(name, str) or name not in allowed or name in indexed:
            raise TrustedEvidenceError("governance collector contains an unknown or duplicate check")
        if item.get("status") != "pass" or not isinstance(item.get("detail"), str):
            raise TrustedEvidenceError(f"governance collector check {name} did not pass")
        indexed[name] = dict(item)
    if set(indexed) != allowed:
        raise TrustedEvidenceError("governance collector is missing required checks")
    return indexed


def build_governance_manifests(
    raw_payload: Any,
    *,
    repository: str,
    source_revision: str,
    run_id: int,
    run_attempt: int,
    workflow_ref: str,
    generated_at: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    _validated_raw_governance(raw_payload)
    revision = _require_revision(source_revision)
    output: dict[str, dict[str, Any]] = {}
    sources = {
        "repository-settings": f"https://api.github.com/repos/{REPOSITORY}/branches/main/protection",
        "release-environment": f"https://api.github.com/repos/{REPOSITORY}/environments",
    }
    for evidence_type in ("repository-settings", "release-environment"):
        metadata = _evidence_metadata(
            evidence_type,
            repository=repository,
            source_revision=revision,
            run_id=run_id,
            run_attempt=run_attempt,
            workflow_ref=workflow_ref,
            generated_at=generated_at,
        )
        output[evidence_type] = _base_manifest(
            evidence_type,
            source=sources[evidence_type],
            source_revision=revision,
            checks=REQUIRED_CHECKS[evidence_type],
            metadata=metadata,
        )
    return output


def _required_job_names() -> dict[str, tuple[str, ...]]:
    def python_jobs(operating_system: str) -> tuple[str, ...]:
        return tuple(f"Python {version} / {operating_system}" for version in _PYTHON_VERSIONS) + (
            f"Future Python 3.x / {operating_system}",
        )

    return {
        "aggregate_python_package_build": ("Python package build",),
        "python_ubuntu_matrix": python_jobs("ubuntu-latest"),
        "python_macos_14_15_26_matrix": tuple(
            name for name in (*_STABLE_PYTHON_JOBS, *_FUTURE_PYTHON_JOBS) if "/ macos-" in name
        ),
        "python_windows_2025_vs2026_matrix": python_jobs("windows-2025-vs2026"),
        "rhel_ubi_8_9_10_and_rhel_7_abi": _RHEL_JOBS,
        "rocky_linux_8_9_10": _ROCKY_JOBS,
        "windows_11_arm": ("Windows 11 ARM runner / Python 3.12 x64",),
        "react_build": ("React build",),
        "mobile_web_smoke_android_and_ios": _MOBILE_JOBS,
        "tkinter_gui_lifecycle": ("Tkinter GUI lifecycle / Ubuntu",),
    }


def derive_platform_checks(
    run_payload: Any,
    jobs_payload: Any,
    *,
    expected_revision: str,
    source_run_id: int,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    revision = _require_revision(expected_revision)
    source_run = _require_positive_integer(source_run_id, "source_run_id")
    if not isinstance(run_payload, Mapping):
        raise TrustedEvidenceError("CI run response must be an object")
    exact_run = {
        "id": source_run,
        "head_sha": revision,
        "name": "CI",
        "path": ".github/workflows/ci.yml",
        "status": "completed",
        "conclusion": "success",
        "head_branch": "main",
    }
    if any(type(run_payload.get(key)) is not type(value) or run_payload.get(key) != value for key, value in exact_run.items()):
        raise TrustedEvidenceError("CI run identity, revision, branch, or conclusion is invalid")
    if run_payload.get("event") not in {"push", "workflow_dispatch"}:
        raise TrustedEvidenceError("CI evidence must come from a push or workflow_dispatch on protected main")
    head_repository = run_payload.get("head_repository")
    if not isinstance(head_repository, Mapping) or head_repository.get("full_name") != REPOSITORY:
        raise TrustedEvidenceError("CI run head repository is invalid")
    if not isinstance(jobs_payload, Mapping):
        raise TrustedEvidenceError("CI jobs response must be an object")
    jobs = jobs_payload.get("jobs")
    total = jobs_payload.get("total_count")
    if not isinstance(jobs, list) or type(total) is not int or total != len(jobs) or total > 100:
        raise TrustedEvidenceError("CI jobs response is malformed, incomplete, or paginated")
    indexed: dict[str, Mapping[str, Any]] = {}
    for job in jobs:
        if not isinstance(job, Mapping) or not isinstance(job.get("name"), str):
            raise TrustedEvidenceError("CI jobs response contains a malformed job")
        name = str(job["name"])
        if name in indexed:
            raise TrustedEvidenceError(f"CI jobs response contains duplicate job {name}")
        indexed[name] = job

    groups = _required_job_names()
    for check_name, names in groups.items():
        for name in names:
            job = indexed.get(name)
            if job is None:
                raise TrustedEvidenceError(f"CI evidence for {check_name} is missing job {name}")
            labels = job.get("labels")
            if (
                job.get("status") != "completed"
                or job.get("conclusion") != "success"
                or not isinstance(labels, list)
                or any(not isinstance(label, str) for label in labels)
                or "self-hosted" in labels
            ):
                raise TrustedEvidenceError(f"CI evidence job {name} was not successful on a hosted runner")

    platform_groups = {
        "windows_hosted_python_and_smoke": groups["python_windows_2025_vs2026_matrix"],
        "windows_11_arm_python_and_smoke": groups["windows_11_arm"],
        "ubuntu_python_tkinter_and_verifier": groups["python_ubuntu_matrix"] + groups["tkinter_gui_lifecycle"],
        "macos_14_15_26_python_and_verifier": groups["python_macos_14_15_26_matrix"],
    }
    for check_name, names in platform_groups.items():
        if any(name not in indexed for name in names):
            raise TrustedEvidenceError(f"platform evidence for {check_name} is incomplete")
    return PLATFORM_CI_CHECKS, PLATFORM_CHECKS


def build_platform_manifests(
    run_payload: Any,
    jobs_payload: Any,
    *,
    repository: str,
    source_revision: str,
    source_run_id: int,
    run_id: int,
    run_attempt: int,
    workflow_ref: str,
    generated_at: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    revision = _require_revision(source_revision)
    ci_checks, platform_checks = derive_platform_checks(
        run_payload,
        jobs_payload,
        expected_revision=revision,
        source_run_id=source_run_id,
    )
    source = f"https://github.com/{REPOSITORY}/actions/runs/{source_run_id}"
    output: dict[str, dict[str, Any]] = {}
    for evidence_type, checks in (("platform-ci", ci_checks), ("platform", platform_checks)):
        metadata = _evidence_metadata(
            evidence_type,
            repository=repository,
            source_revision=revision,
            run_id=run_id,
            run_attempt=run_attempt,
            workflow_ref=workflow_ref,
            generated_at=generated_at,
        )
        manifest = _base_manifest(
            evidence_type,
            source=source,
            source_revision=revision,
            checks=checks,
            metadata=metadata,
        )
        manifest["source_run_id"] = source_run_id
        if evidence_type == "platform":
            manifest["targets"] = list(_PLATFORM_TARGETS)
        output[evidence_type] = manifest
    return output


def build_live_manifest(
    report: Any,
    *,
    tier: str,
    repository: str,
    source_revision: str,
    run_id: int,
    run_attempt: int,
    workflow_ref: str,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    from polymarket.live_report_schema import validate_live_validation_report
    from polymarket.live_reports import live_validation_report_promotion

    if tier not in {"credentialed", "funded"}:
        raise TrustedEvidenceError("tier must be credentialed or funded")
    if not isinstance(report, Mapping):
        raise TrustedEvidenceError("live report must be an object")
    revision = _require_revision(source_revision)
    validation = validate_live_validation_report(report)
    if validation.get("ok") is not True:
        raise TrustedEvidenceError("live report failed schema validation")
    provenance = report.get("source_provenance")
    if (
        not isinstance(provenance, Mapping)
        or provenance.get("stable") is not True
        or provenance.get("source_revision") != revision
    ):
        raise TrustedEvidenceError("live report is not bound to the exact clean source revision")
    promotion = live_validation_report_promotion(report)
    field = "can_promote_credential_live_verified" if tier == "credentialed" else "can_promote_funded_live_verified"
    if promotion.get(field) is not True:
        reasons = promotion.get("blocked_reasons")
        detail = "; ".join(str(item) for item in reasons) if isinstance(reasons, list) else "promotion failed"
        raise TrustedEvidenceError(f"live report is not {tier} promotion-eligible: {detail}")
    funded_check = report.get("funded_live_order_check")
    funded_live_action = isinstance(funded_check, Mapping) and funded_check.get("live_action") is True
    if tier == "funded" and not funded_live_action:
        raise TrustedEvidenceError("funded evidence requires a real live_action=true order/cancel audit")

    evidence_type = f"{tier}-polymarket"
    metadata = _evidence_metadata(
        evidence_type,
        repository=repository,
        source_revision=revision,
        run_id=run_id,
        run_attempt=run_attempt,
        workflow_ref=workflow_ref,
        generated_at=generated_at,
    )
    manifest = _base_manifest(
        evidence_type,
        source=f"https://github.com/{REPOSITORY}/actions/runs/{run_id}",
        source_revision=revision,
        checks=REQUIRED_CHECKS[evidence_type],
        metadata=metadata,
    )
    canonical_report = canonical_json_bytes(dict(report))
    manifest.update(
        {
            "target_tier": f"{tier}_live_verified",
            "report_sha256": hashlib.sha256(canonical_report).hexdigest(),
            "live_action": tier == "funded",
            "live_report": dict(report),
        }
    )
    return manifest


def _expected_source(payload: Mapping[str, Any], evidence_type: str) -> str:
    if evidence_type == "repository-settings":
        return f"https://api.github.com/repos/{REPOSITORY}/branches/main/protection"
    if evidence_type == "release-environment":
        return f"https://api.github.com/repos/{REPOSITORY}/environments"
    if evidence_type in {"platform-ci", "platform"}:
        return f"https://github.com/{REPOSITORY}/actions/runs/{payload.get('source_run_id')}"
    evidence = payload.get("evidence")
    run_id = evidence.get("run_id") if isinstance(evidence, Mapping) else None
    return f"https://github.com/{REPOSITORY}/actions/runs/{run_id}"


def validate_manifest(
    payload: Any,
    *,
    expected_evidence_type: str,
    expected_revision: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    if expected_evidence_type not in WORKFLOW_CONTRACTS:
        return {"ok": False, "errors": ["evidence type has no trusted workflow contract"]}
    revision = str(expected_revision or "").strip().lower()
    if not _COMMIT_RE.fullmatch(revision):
        return {"ok": False, "errors": ["expected revision must be a lowercase 40-character commit SHA"]}
    if not isinstance(payload, Mapping):
        return {"ok": False, "errors": ["trusted evidence must be a JSON object"]}
    expected_fields = _TYPE_FIELDS[expected_evidence_type]
    if set(payload) != expected_fields:
        errors.append("trusted evidence does not match the exact top-level field contract")
    exact = {
        "schema_version": SCHEMA_VERSION,
        "report_type": REPORT_TYPE,
        "evidence_type": expected_evidence_type,
        "verified": True,
        "scope": _EXACT_SCOPES[expected_evidence_type],
        "source_revision": revision,
    }
    if any(type(payload.get(key)) is not type(value) or payload.get(key) != value for key, value in exact.items()):
        errors.append("trusted evidence type, schema, scope, verification, or revision is invalid")
    if payload.get("source") != _expected_source(payload, expected_evidence_type):
        errors.append("trusted evidence source URL is invalid")

    checks = payload.get("checks")
    required_checks = REQUIRED_CHECKS[expected_evidence_type]
    if not isinstance(checks, list) or len(checks) != len(required_checks):
        errors.append("trusted evidence checks do not match the exact contract")
    else:
        observed_names: list[str] = []
        for check in checks:
            if not isinstance(check, Mapping) or set(check) != {"name", "status"} or check.get("status") != "pass":
                errors.append("trusted evidence contains a malformed or failed check")
                break
            observed_names.append(str(check.get("name") or ""))
        if tuple(observed_names) != required_checks:
            errors.append("trusted evidence check order or inventory is invalid")

    evidence = payload.get("evidence")
    contract = WORKFLOW_CONTRACTS[expected_evidence_type]
    if not isinstance(evidence, Mapping) or set(evidence) != _EVIDENCE_FIELDS:
        errors.append("trusted evidence metadata does not match the exact contract")
        evidence = {}
    run_id = evidence.get("run_id")
    run_attempt = evidence.get("run_attempt")
    exact_metadata = {
        "schema_version": EVIDENCE_METADATA_SCHEMA_VERSION,
        "repository": REPOSITORY,
        "source_revision": revision,
        "workflow": contract["workflow"],
        "workflow_name": contract["workflow_name"],
        "workflow_ref": f"{REPOSITORY}/{contract['workflow']}@{TRUSTED_REF}",
        "event": "workflow_dispatch",
        "runner_environment": "github-hosted",
        "job": contract["job"],
    }
    if any(type(evidence.get(key)) is not type(value) or evidence.get(key) != value for key, value in exact_metadata.items()):
        errors.append("trusted evidence repository, workflow, event, runner, job, or revision metadata is invalid")
    if type(run_id) is not int or run_id <= 0 or type(run_attempt) is not int or run_attempt <= 0:
        errors.append("trusted evidence run_id and run_attempt must be positive integers")
    elif evidence.get("artifact_name") != artifact_name(expected_evidence_type, revision, run_id, run_attempt):
        errors.append("trusted evidence artifact name is invalid")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("trusted evidence validation clock must include a timezone")
    generated_at = _parse_timestamp(evidence.get("generated_at"), now=current, errors=errors)

    source_run_id: int | None = None
    if expected_evidence_type in {"platform-ci", "platform"}:
        candidate = payload.get("source_run_id")
        if type(candidate) is not int or candidate <= 0:
            errors.append("platform evidence source_run_id must be a positive integer")
        else:
            source_run_id = candidate
    if expected_evidence_type == "platform" and payload.get("targets") != list(_PLATFORM_TARGETS):
        errors.append("platform evidence targets do not match the exact supported target contract")

    if expected_evidence_type in {"credentialed-polymarket", "funded-polymarket"}:
        tier = "credentialed" if expected_evidence_type == "credentialed-polymarket" else "funded"
        if payload.get("target_tier") != f"{tier}_live_verified":
            errors.append("live evidence target tier is invalid")
        if type(payload.get("live_action")) is not bool or payload.get("live_action") is not (tier == "funded"):
            errors.append("live evidence live_action does not match its tier")
        report = payload.get("live_report")
        if not isinstance(report, Mapping):
            errors.append("live evidence report must be an object")
        else:
            report_hash = hashlib.sha256(canonical_json_bytes(dict(report))).hexdigest()
            if not _HASH_RE.fullmatch(str(payload.get("report_sha256") or "")) or payload.get("report_sha256") != report_hash:
                errors.append("live evidence report hash is invalid")
            try:
                rebuilt = build_live_manifest(
                    report,
                    tier=tier,
                    repository=REPOSITORY,
                    source_revision=revision,
                    run_id=run_id if type(run_id) is int else 0,
                    run_attempt=run_attempt if type(run_attempt) is int else 0,
                    workflow_ref=f"{REPOSITORY}/{contract['workflow']}@{TRUSTED_REF}",
                    generated_at=generated_at,
                )
            except TrustedEvidenceError as exc:
                errors.append(str(exc))
            else:
                for field in ("checks", "target_tier", "report_sha256", "live_action"):
                    if payload.get(field) != rebuilt.get(field):
                        errors.append(f"live evidence derived field {field} is invalid")

    return {
        "ok": not errors,
        "errors": errors,
        "evidence_type": expected_evidence_type,
        "source_revision": revision,
        "run_id": run_id if type(run_id) is int else 0,
        "run_attempt": run_attempt if type(run_attempt) is int else 0,
        "source_run_id": source_run_id or 0,
        "generated_at": generated_at,
        "artifact_name": evidence.get("artifact_name") if isinstance(evidence, Mapping) else "",
        "subject_name": contract["subject_name"],
        "workflow": contract["workflow"],
        "workflow_name": contract["workflow_name"],
        "workflow_ref": exact_metadata["workflow_ref"],
        "job": contract["job"],
        "required_steps": contract["required_steps"],
    }


def write_manifest(path: Path, payload: Mapping[str, Any]) -> None:
    if not path.parent.is_dir() or path.is_symlink():
        raise ValueError("evidence output parent must exist and output must not be a symbolic link")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            if os.name == "posix":
                os.fchmod(handle.fileno(), 0o600)
            handle.write(canonical_json_bytes(payload))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _common_builder_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repository", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--run-attempt", required=True, type=int)
    parser.add_argument("--workflow-ref", required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate or validate exact trusted readiness evidence candidates.")
    commands = parser.add_subparsers(dest="command", required=True)

    governance = commands.add_parser("governance")
    _common_builder_arguments(governance)
    governance.add_argument("--input", required=True, type=Path)
    governance.add_argument("--output-directory", required=True, type=Path)

    platform = commands.add_parser("platform")
    _common_builder_arguments(platform)
    platform.add_argument("--run-input", required=True, type=Path)
    platform.add_argument("--jobs-input", required=True, type=Path)
    platform.add_argument("--source-run-id", required=True, type=int)
    platform.add_argument("--output-directory", required=True, type=Path)

    live = commands.add_parser("live")
    _common_builder_arguments(live)
    live.add_argument("--input", required=True, type=Path)
    live.add_argument("--tier", required=True, choices=("credentialed", "funded"))
    live.add_argument("--output", required=True, type=Path)

    validate = commands.add_parser("validate")
    validate.add_argument("--input", required=True, type=Path)
    validate.add_argument("--evidence-type", required=True, choices=tuple(WORKFLOW_CONTRACTS))
    validate.add_argument("--expected-revision", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "governance":
            manifests = build_governance_manifests(
                load_strict_json(args.input),
                repository=args.repository,
                source_revision=args.source_revision,
                run_id=args.run_id,
                run_attempt=args.run_attempt,
                workflow_ref=args.workflow_ref,
            )
            for evidence_type, payload in manifests.items():
                write_manifest(
                    args.output_directory / WORKFLOW_CONTRACTS[evidence_type]["subject_name"],
                    payload,
                )
            result = {"ok": True, "evidence_types": sorted(manifests)}
        elif args.command == "platform":
            manifests = build_platform_manifests(
                load_strict_json(args.run_input),
                load_strict_json(args.jobs_input),
                repository=args.repository,
                source_revision=args.source_revision,
                source_run_id=args.source_run_id,
                run_id=args.run_id,
                run_attempt=args.run_attempt,
                workflow_ref=args.workflow_ref,
            )
            for evidence_type, payload in manifests.items():
                write_manifest(
                    args.output_directory / WORKFLOW_CONTRACTS[evidence_type]["subject_name"],
                    payload,
                )
            result = {"ok": True, "evidence_types": sorted(manifests)}
        elif args.command == "live":
            evidence_type = f"{args.tier}-polymarket"
            payload = build_live_manifest(
                load_strict_json(args.input),
                tier=args.tier,
                repository=args.repository,
                source_revision=args.source_revision,
                run_id=args.run_id,
                run_attempt=args.run_attempt,
                workflow_ref=args.workflow_ref,
            )
            write_manifest(args.output, payload)
            result = {"ok": True, "evidence_types": [evidence_type]}
        else:
            payload = load_strict_json(args.input)
            validation = validate_manifest(
                payload,
                expected_evidence_type=args.evidence_type,
                expected_revision=args.expected_revision,
            )
            if not validation["ok"]:
                raise TrustedEvidenceError("; ".join(validation["errors"]))
            if args.input.read_bytes() != canonical_json_bytes(payload):
                raise TrustedEvidenceError("trusted evidence file is not canonical JSON")
            result = {"ok": True, "evidence_types": [args.evidence_type]}
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError, _DuplicateJsonKey, TrustedEvidenceError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
