from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from scripts.verify_production_deployment import (
        BACKUP_MAX_AGE_SECONDS,
        BACKUP_MAX_FUTURE_SKEW_SECONDS,
        DURABLE_STATE_PATHS,
        EVIDENCE_SCHEMA_VERSION,
        EXTERNAL_PROBE_REPORT_TYPE,
        PUBLIC_PROXY_AUTH_PROBES,
        PROVIDER_SLUG,
        ROLLBACK_DRILL_STEPS,
        REQUIRED_UNITS,
    )
    from scripts.verify_restored_state import application_check_valid
except ModuleNotFoundError:  # Direct execution adds scripts/ to sys.path.
    from verify_production_deployment import (  # type: ignore[no-redef]
        BACKUP_MAX_AGE_SECONDS,
        BACKUP_MAX_FUTURE_SKEW_SECONDS,
        DURABLE_STATE_PATHS,
        EVIDENCE_SCHEMA_VERSION,
        EXTERNAL_PROBE_REPORT_TYPE,
        PUBLIC_PROXY_AUTH_PROBES,
        PROVIDER_SLUG,
        ROLLBACK_DRILL_STEPS,
        REQUIRED_UNITS,
    )
    from verify_restored_state import application_check_valid

from core.deployment_identity import canonical_https_origin


MAX_REPORT_BYTES = 1024 * 1024
MAX_REPORT_AGE_SECONDS = 24 * 60 * 60
MAX_REPORT_FUTURE_SKEW_SECONDS = 5 * 60
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class DeploymentEvidenceError(ValueError):
    """Raised when a deployment report cannot prove the production gate."""


def required_check_names() -> frozenset[str]:
    systemd = {
        f"systemd_{command}_{unit}"
        for unit in REQUIRED_UNITS
        for command in ("is-active", "is-enabled")
    }
    return frozenset(
        {
            "source_revision",
            *systemd,
            "systemd_recent_success_market-sentinel-backup.service",
            "filesystem_private_market-sentinel.env",
            "filesystem_private_market-sentinel",
            "filesystem_private_market-sentinel-backups",
            "durable_state_wiring",
            "verified_recent_state_backup",
            "deployment_host_identity",
            "verified_restore_drill",
            "verified_production_rollback_drill",
            "loopback_health",
            "loopback_metrics",
            "loopback_token_auth",
            "public_https_proxy",
            "source_revision_final",
            "evidence_output_directory",
            "source_revision_pre_write",
        }
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DeploymentEvidenceError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise DeploymentEvidenceError(f"non-finite JSON number: {value}")


def _read_report(path: Path) -> tuple[dict[str, Any], str]:
    if path.is_symlink() or not path.is_file():
        raise DeploymentEvidenceError("deployment evidence must be a regular, non-symbolic-link file")
    size = path.stat().st_size
    if size <= 0 or size > MAX_REPORT_BYTES:
        raise DeploymentEvidenceError(
            f"deployment evidence size must be between 1 and {MAX_REPORT_BYTES} bytes"
        )
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DeploymentEvidenceError("deployment evidence must be UTF-8 JSON") from exc
    try:
        payload = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except json.JSONDecodeError as exc:
        raise DeploymentEvidenceError(f"invalid deployment evidence JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise DeploymentEvidenceError("deployment evidence must be a JSON object")
    return payload, hashlib.sha256(raw).hexdigest()


def _utc_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise DeploymentEvidenceError(f"{label} must be a non-empty UTC timestamp")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise DeploymentEvidenceError(f"{label} is not a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DeploymentEvidenceError(f"{label} must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _require_exact_keys(payload: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(payload)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        details: list[str] = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unknown:
            details.append("unknown=" + ",".join(unknown))
        raise DeploymentEvidenceError(f"{label} fields are not exact ({'; '.join(details)})")


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DeploymentEvidenceError(f"{label} must be a positive integer")
    return value


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DeploymentEvidenceError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise DeploymentEvidenceError(f"{label} must be a finite number")
    return result


def _canonical_origin(value: str) -> str:
    try:
        return canonical_https_origin(value)
    except ValueError as exc:
        raise DeploymentEvidenceError(str(exc)) from exc


def review_external_probe_report(
    path: Path,
    *,
    expected_version: str,
    expected_revision: str,
    expected_frontend_sha256: str,
    expected_origin: str,
    expected_run_id: int,
    expected_run_attempt: int,
    expected_nonce: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate the independently collected GitHub-hosted public deployment probe."""

    report, report_sha256 = _read_report(Path(path))
    _require_exact_keys(
        report,
        {"schema_version", "report_type", "probed_at", "status", "source_revision", "collection", "checks"},
        "external deployment probe",
    )
    revision = expected_revision.strip().lower()
    frontend_sha256 = expected_frontend_sha256.strip().lower()
    origin = _canonical_origin(expected_origin)
    if (
        report.get("schema_version") != 1
        or report.get("report_type") != EXTERNAL_PROBE_REPORT_TYPE
        or report.get("status") != "ok"
        or report.get("source_revision") != revision
        or not COMMIT_SHA.fullmatch(revision)
        or not SHA256_HEX.fullmatch(frontend_sha256)
    ):
        raise DeploymentEvidenceError("external deployment probe identity is invalid")
    observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    probed_at = _utc_timestamp(report.get("probed_at"), "external probed_at")
    age_seconds = (observed_at - probed_at).total_seconds()
    if age_seconds < -MAX_REPORT_FUTURE_SKEW_SECONDS or age_seconds > MAX_REPORT_AGE_SECONDS:
        raise DeploymentEvidenceError("external deployment probe is stale or future-dated")
    collection = report.get("collection")
    if not isinstance(collection, dict):
        raise DeploymentEvidenceError("external deployment probe collection must be an object")
    _require_exact_keys(
        collection,
        {
            "mode",
            "public_origin",
            "expected_version",
            "expected_source_revision",
            "expected_frontend_sha256",
            "run_id",
            "run_attempt",
            "nonce",
            "runner_environment",
        },
        "external deployment probe collection",
    )
    expected_collection = {
        "mode": "github_hosted_external_public_probe",
        "public_origin": origin,
        "expected_version": expected_version,
        "expected_source_revision": revision,
        "expected_frontend_sha256": frontend_sha256,
        "run_id": expected_run_id,
        "run_attempt": expected_run_attempt,
        "nonce": expected_nonce,
        "runner_environment": "github-hosted",
    }
    if any(collection.get(key) != value for key, value in expected_collection.items()):
        raise DeploymentEvidenceError("external deployment probe is not bound to the exact workflow run")
    checks = report.get("checks")
    if not isinstance(checks, list) or len(checks) != 1 or not isinstance(checks[0], dict):
        raise DeploymentEvidenceError("external deployment probe must contain exactly one public check")
    check = checks[0]
    if (
        set(check) != {
            "name",
            "status",
            "api_version",
            "runtime_source_revision",
            "runtime_frontend_sha256",
            "unauthenticated_probes",
        }
        or check.get("name") != "public_https_proxy"
        or check.get("status") != "pass"
        or check.get("api_version") != expected_version
        or check.get("runtime_source_revision") != revision
        or check.get("runtime_frontend_sha256") != frontend_sha256
        or check.get("unauthenticated_probes") != len(PUBLIC_PROXY_AUTH_PROBES)
    ):
        raise DeploymentEvidenceError("external public proxy result is incomplete or misbound")
    return {
        "status": "ok",
        "probed_at": report["probed_at"],
        "report_sha256": report_sha256,
        "public_origin": origin,
        "api_version": expected_version,
        "source_revision": revision,
        "frontend_sha256": frontend_sha256,
        "unauthenticated_probes": len(PUBLIC_PROXY_AUTH_PROBES),
    }


def review_deployment_report(
    path: Path,
    *,
    expected_version: str,
    expected_revision: str,
    expected_run_id: int | None = None,
    expected_run_attempt: int | None = None,
    expected_nonce: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate a raw real-host collector report without trusting a wrapper manifest."""

    expected_version = expected_version.strip()
    expected_revision = expected_revision.strip().lower()
    if not expected_version or "\n" in expected_version or "\r" in expected_version:
        raise DeploymentEvidenceError("expected version must be a non-empty single-line value")
    if not COMMIT_SHA.fullmatch(expected_revision):
        raise DeploymentEvidenceError("expected revision must be a lowercase 40-character Git SHA")

    report, report_sha256 = _read_report(Path(path))
    _require_exact_keys(
        report,
        {"schema_version", "collected_at", "source", "status", "checks", "collection"},
        "deployment evidence",
    )
    if report["schema_version"] != EVIDENCE_SCHEMA_VERSION or isinstance(report["schema_version"], bool):
        raise DeploymentEvidenceError(
            f"deployment evidence schema_version must equal {EVIDENCE_SCHEMA_VERSION}"
        )
    if report["status"] != "ok":
        raise DeploymentEvidenceError("deployment evidence status must equal ok")

    observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    collected_at = _utc_timestamp(report["collected_at"], "collected_at")
    age_seconds = (observed_at - collected_at).total_seconds()
    if age_seconds < -MAX_REPORT_FUTURE_SKEW_SECONDS or age_seconds > MAX_REPORT_AGE_SECONDS:
        raise DeploymentEvidenceError(
            "deployment evidence is stale or implausibly future-dated "
            f"(age_seconds={age_seconds:.0f})"
        )

    source = report["source"]
    if not isinstance(source, dict):
        raise DeploymentEvidenceError("source must be an object")
    _require_exact_keys(
        source,
        {"project_version", "git_revision", "git_revision_status", "git_worktree_status"},
        "source",
    )
    if source["project_version"] != expected_version:
        raise DeploymentEvidenceError("source project_version does not match the expected version")
    if source["git_revision"] != expected_revision:
        raise DeploymentEvidenceError("source git_revision does not match the expected revision")
    if source["git_revision_status"] != "ok" or source["git_worktree_status"] != "clean":
        raise DeploymentEvidenceError("source revision must be available and the deployed checkout clean")

    collection = report["collection"]
    if not isinstance(collection, dict):
        raise DeploymentEvidenceError("collection must be an object")
    _require_exact_keys(
        collection,
        {
            "mode",
            "systemd_requested",
            "public_proxy_requested",
            "public_origin",
            "expected_version",
            "expected_source_revision",
            "expected_frontend_sha256",
            "deployment_provider",
            "host_identity_sha256",
            "restore_drill_requested",
            "rollback_drill_requested",
            "run_id",
            "run_attempt",
            "nonce",
        },
        "collection",
    )
    if collection["mode"] != "production":
        raise DeploymentEvidenceError("collection mode must equal production")
    if collection["systemd_requested"] is not True:
        raise DeploymentEvidenceError("production evidence must request systemd checks")
    if collection["public_proxy_requested"] is not True:
        raise DeploymentEvidenceError("production evidence must request the public HTTPS proxy checks")
    if collection["restore_drill_requested"] is not True:
        raise DeploymentEvidenceError("production evidence must request an actual restore drill")
    if collection["rollback_drill_requested"] is not True:
        raise DeploymentEvidenceError("production evidence must request a production rollback drill")
    if not isinstance(collection["public_origin"], str) or not collection["public_origin"].startswith("https://"):
        raise DeploymentEvidenceError("production evidence must bind the exact public HTTPS origin")
    if collection["expected_version"] != expected_version:
        raise DeploymentEvidenceError("collection expected_version does not match")
    if collection["expected_source_revision"] != expected_revision:
        raise DeploymentEvidenceError("collection expected_source_revision does not match")
    if expected_run_id is not None and collection["run_id"] != expected_run_id:
        raise DeploymentEvidenceError("collection workflow run id does not match")
    if expected_run_attempt is not None and collection["run_attempt"] != expected_run_attempt:
        raise DeploymentEvidenceError("collection workflow run attempt does not match")
    if expected_nonce is not None and collection["nonce"] != expected_nonce:
        raise DeploymentEvidenceError("collection workflow nonce does not match")
    frontend_sha256 = collection["expected_frontend_sha256"]
    if not isinstance(frontend_sha256, str) or not SHA256_HEX.fullmatch(frontend_sha256):
        raise DeploymentEvidenceError("collection expected_frontend_sha256 must be a lowercase SHA-256")
    deployment_provider = collection["deployment_provider"]
    host_identity_sha256 = collection["host_identity_sha256"]
    if not isinstance(deployment_provider, str) or not PROVIDER_SLUG.fullmatch(deployment_provider):
        raise DeploymentEvidenceError("collection deployment_provider must be a lowercase provider slug")
    if not isinstance(host_identity_sha256, str) or not SHA256_HEX.fullmatch(host_identity_sha256):
        raise DeploymentEvidenceError("collection host_identity_sha256 must be a lowercase SHA-256")

    checks = report["checks"]
    if not isinstance(checks, list):
        raise DeploymentEvidenceError("checks must be a list")
    indexed: dict[str, dict[str, Any]] = {}
    for position, check in enumerate(checks):
        if not isinstance(check, dict):
            raise DeploymentEvidenceError(f"check {position} must be an object")
        name = check.get("name")
        if not isinstance(name, str) or not name:
            raise DeploymentEvidenceError(f"check {position} has an invalid name")
        if name in indexed:
            raise DeploymentEvidenceError(f"duplicate deployment check: {name}")
        if check.get("status") != "pass":
            raise DeploymentEvidenceError(f"deployment check did not pass: {name}")
        indexed[name] = check
    required = required_check_names()
    actual = set(indexed)
    if actual != set(required):
        missing = sorted(set(required) - actual)
        unknown = sorted(actual - set(required))
        details: list[str] = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unknown:
            details.append("unknown=" + ",".join(unknown))
        raise DeploymentEvidenceError(
            "deployment check inventory is not exact (" + "; ".join(details) + ")"
        )

    loopback = indexed["loopback_health"]
    if loopback.get("api_version") != expected_version:
        raise DeploymentEvidenceError("loopback health version does not match")
    if loopback.get("runtime_source_revision") != expected_revision:
        raise DeploymentEvidenceError("loopback runtime revision does not match")
    if loopback.get("runtime_frontend_sha256") != frontend_sha256:
        raise DeploymentEvidenceError("loopback runtime frontend digest does not match")
    if loopback.get("disk_frontend_sha256") != frontend_sha256:
        raise DeploymentEvidenceError("on-disk frontend digest does not match")

    public_proxy = indexed["public_https_proxy"]
    if public_proxy.get("api_version") != expected_version:
        raise DeploymentEvidenceError("public proxy version does not match")
    if public_proxy.get("unauthenticated_probes") != len(PUBLIC_PROXY_AUTH_PROBES):
        raise DeploymentEvidenceError("public proxy did not run the complete unauthenticated probe set")
    if public_proxy.get("runtime_source_revision") != expected_revision:
        raise DeploymentEvidenceError("public proxy runtime revision does not match")
    if public_proxy.get("runtime_frontend_sha256") != frontend_sha256:
        raise DeploymentEvidenceError("public proxy runtime frontend digest does not match")

    host_identity = indexed["deployment_host_identity"]
    if (
        host_identity.get("deployment_provider") != deployment_provider
        or host_identity.get("host_identity_sha256") != host_identity_sha256
    ):
        raise DeploymentEvidenceError("deployment host identity is not bound to protected configuration")

    durable = indexed["durable_state_wiring"]
    if durable.get("durable_store_count") != len(DURABLE_STATE_PATHS):
        raise DeploymentEvidenceError("durable-state check does not cover every required store")
    if durable.get("state_directory") != "/var/lib/market-sentinel":
        raise DeploymentEvidenceError("durable-state directory is not the production path")
    if durable.get("backup_source") != "/var/lib/market-sentinel":
        raise DeploymentEvidenceError("backup source does not match the production state path")

    backup = indexed["verified_recent_state_backup"]
    backup_created_at = _utc_timestamp(backup.get("created_at"), "backup created_at")
    backup_age_seconds = (observed_at - backup_created_at).total_seconds()
    if backup_age_seconds < -BACKUP_MAX_FUTURE_SKEW_SECONDS or backup_age_seconds > BACKUP_MAX_AGE_SECONDS:
        raise DeploymentEvidenceError("the verified backup is stale or future-dated at review time")
    if not isinstance(backup.get("sha256"), str) or not SHA256_HEX.fullmatch(backup["sha256"]):
        raise DeploymentEvidenceError("verified backup SHA-256 is invalid")
    _positive_int(backup.get("file_count"), "verified backup file_count")
    _positive_int(backup.get("verified_pairs"), "verified backup verified_pairs")
    if backup.get("invalid_pairs") != 0 or backup.get("orphan_archives") != 0 or backup.get("orphan_manifests") != 0:
        raise DeploymentEvidenceError("backup catalog contains invalid or orphaned artifacts")
    collected_backup_age_seconds = (collected_at - backup_created_at).total_seconds()
    if abs(
        _finite_number(backup.get("backup_age_seconds"), "backup_age_seconds")
        - collected_backup_age_seconds
    ) > 5:
        raise DeploymentEvidenceError("reported backup age is inconsistent with its timestamp")

    restore = indexed["verified_restore_drill"]
    restore_completed_at = _utc_timestamp(restore.get("completed_at"), "restore completed_at")
    if (
        restore.get("mode") != "isolated_full_restore"
        or not application_check_valid(
            restore.get("application"), version=expected_version,
            revision=expected_revision, frontend_sha256=frontend_sha256,
        )
        or restore.get("archive") != backup.get("archive")
        or restore.get("backup_created_at") != backup.get("created_at")
        or restore.get("backup_sha256") != backup.get("sha256")
        or restore.get("restored_file_count") != backup.get("file_count")
        or restore.get("restored_bytes") != backup.get("verified_bytes")
        or restore_completed_at < backup_created_at
        or restore_completed_at > observed_at + timedelta(seconds=MAX_REPORT_FUTURE_SKEW_SECONDS)
    ):
        raise DeploymentEvidenceError("restore drill is not bound to the complete reviewed backup")

    rollback = indexed["verified_production_rollback_drill"]
    rollback_completed_at = _utc_timestamp(rollback.get("completed_at"), "rollback completed_at")
    rollback_revision = rollback.get("rollback_revision")
    if (
        not isinstance(rollback.get("drill_id"), str)
        or not rollback["drill_id"].strip()
        or not isinstance(rollback.get("report_sha256"), str)
        or not SHA256_HEX.fullmatch(rollback["report_sha256"])
        or not isinstance(rollback_revision, str)
        or not COMMIT_SHA.fullmatch(rollback_revision)
        or rollback_revision == expected_revision
        or rollback.get("final_revision") != expected_revision
        or rollback.get("step_count") != len(ROLLBACK_DRILL_STEPS)
        or rollback_completed_at > observed_at + timedelta(seconds=MAX_REPORT_FUTURE_SKEW_SECONDS)
        or (observed_at - rollback_completed_at).total_seconds() > MAX_REPORT_AGE_SECONDS
    ):
        raise DeploymentEvidenceError("production rollback drill identity or freshness is invalid")

    return {
        "schema_version": 1,
        "evidence_type": "reviewed-raw-deployment",
        "status": "ok",
        "environment": "production",
        "collected_at": report["collected_at"],
        "reviewed_at": observed_at.isoformat().replace("+00:00", "Z"),
        "source_revision": expected_revision,
        "expected_version": expected_version,
        "frontend_sha256": frontend_sha256,
        "raw_report_sha256": report_sha256,
        "check_count": len(indexed),
        "deployment_provider": deployment_provider,
        "host_identity_sha256": host_identity_sha256,
        "restore_drill": {
            "completed_at": restore["completed_at"],
            "backup_sha256": restore["backup_sha256"],
            "restored_file_count": restore["restored_file_count"],
            "restored_bytes": restore["restored_bytes"],
            "application": restore["application"],
        },
        "rollback_drill": {
            "drill_id": rollback["drill_id"],
            "report_sha256": rollback["report_sha256"],
            "completed_at": rollback["completed_at"],
            "rollback_revision": rollback_revision,
            "final_revision": expected_revision,
            "step_count": rollback["step_count"],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Review a raw MarketSentinel real-host deployment report without trusting a wrapper manifest."
    )
    parser.add_argument("report", type=Path)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        result = review_deployment_report(
            args.report,
            expected_version=args.expected_version,
            expected_revision=args.expected_revision,
        )
    except (OSError, DeploymentEvidenceError) as exc:
        failure = {"status": "failed", "detail": str(exc)}
        if args.json:
            print(json.dumps(failure, sort_keys=True))
        else:
            print(f"[fail] {exc}")
        return 1
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(
            "[ok] production deployment evidence "
            f"({result['source_revision']}, raw_sha256={result['raw_report_sha256']})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
