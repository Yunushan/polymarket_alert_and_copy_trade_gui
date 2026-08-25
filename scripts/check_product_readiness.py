from __future__ import annotations

"""Compute a conservative, reproducible MarketSentinel readiness score.

The local checks can be run from a clean checkout. External evidence is never
inferred from CI configuration: operators must provide reviewed JSON evidence
manifests before the deployment, platform, repository-setting, credentialed,
or funded points are awarded.
"""

import argparse
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
        return {"status": "fail", "command": command, "detail": type(exc).__name__}
    return {
        "status": "pass" if result.returncode == 0 else "fail",
        "command": command,
        "returncode": result.returncode,
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
                if result.returncode == 0 and report_path.is_file():
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                    public_checks = report.get("public_checks", {})
                    passed = bool(report.get("ok")) and bool(public_checks) and all(
                        isinstance(check, dict) and check.get("status") == "ok" for check in public_checks.values()
                    )
                    if passed:
                        return {
                            "status": "pass",
                            "command": command,
                            "returncode": result.returncode,
                            "attempt": attempt,
                        }
                last_result = {
                    "status": "fail",
                    "command": command,
                    "returncode": result.returncode,
                    "attempt": attempt,
                }
        except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
            last_result = {"status": "fail", "command": command, "detail": type(exc).__name__, "attempt": attempt}
        if attempt < PUBLIC_LIVE_ATTEMPTS:
            time.sleep(PUBLIC_LIVE_RETRY_DELAY_SECONDS)
    return last_result


_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


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


def _reviewed_evidence(
    path_value: str,
    label: str,
    *,
    evidence_type: str,
    required_fields: tuple[str, ...] = (),
    expected_fields: dict[str, Any] | None = None,
    revision_field: str = "",
    expected_revision: str = "",
    now: datetime | None = None,
) -> tuple[bool, str]:
    if not path_value:
        return False, f"Provide reviewed {label} JSON evidence."
    path = Path(path_value).expanduser()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, f"{label} evidence is not readable JSON."
    if not isinstance(payload, dict) or payload.get("verified") is not True:
        return False, f"{label} evidence must contain verified=true."
    if payload.get("schema_version") != 1:
        return False, f"{label} evidence must contain schema_version=1."
    if payload.get("evidence_type") != evidence_type:
        return False, f"{label} evidence must declare evidence_type={evidence_type!r}."
    if not payload.get("reviewed_by") or not payload.get("reviewed_at"):
        return False, f"{label} evidence requires reviewed_by and reviewed_at."
    if not isinstance(payload["reviewed_by"], str) or "\n" in payload["reviewed_by"]:
        return False, f"{label} evidence reviewed_by must be a single-line string."
    if not isinstance(payload["reviewed_at"], str):
        return False, f"{label} evidence reviewed_at must be an ISO-8601 string."
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
    if not isinstance(payload.get("source"), str) or not payload["source"].strip():
        return False, f"{label} evidence requires a non-empty source."
    missing_fields = [field for field in required_fields if field not in payload or payload[field] in (None, "", [])]
    if missing_fields:
        return False, f"{label} evidence is missing required fields: {', '.join(missing_fields)}."
    for field, expected in (expected_fields or {}).items():
        if payload.get(field) != expected:
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
    if "run_id" in payload and (not isinstance(payload["run_id"], int) or payload["run_id"] <= 0):
        return False, f"{label} evidence run_id must be a positive integer."
    if "assets" in payload and (not isinstance(payload["assets"], list) or not payload["assets"]):
        return False, f"{label} evidence assets must be a non-empty list."
    if "targets" in payload and (not isinstance(payload["targets"], list) or not payload["targets"]):
        return False, f"{label} evidence targets must be a non-empty list."
    if evidence_type == "funded-polymarket" and payload.get("live_action") is not True:
        return False, "funded Polymarket evidence requires live_action=true."
    checks = payload.get("checks")
    if not isinstance(checks, list) or not checks:
        return False, f"{label} evidence requires a non-empty checks list."
    names = [check.get("name") for check in checks if isinstance(check, dict)]
    if len(names) != len(checks) or any(not isinstance(name, str) or not name.strip() for name in names):
        return False, f"{label} evidence checks require non-empty names."
    if len(set(names)) != len(names):
        return False, f"{label} evidence checks must have unique names."
    if any(not isinstance(check, dict) or check.get("status") not in {"pass", "ok"} for check in checks):
        return False, f"{label} evidence contains a failed or malformed check."
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
    local_result = _run_local_gates(args.full_local) if args.run_local else {"status": "not_run"}
    public_result = _run_public_live() if args.run_public_live else {"status": "not_run"}
    project_version = _project_version()
    repository_revision = _repository_revision()

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
    tests = _category(
        "tests_correctness",
        18 if local_pass else 0,
        "The requested local verification command passed." if local_pass else "Run the local verification command successfully.",
        [] if local_pass else ["python verify.py --skip-pip-check"],
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
    )
    release_history_ok, release_history_detail = _reviewed_evidence(
        args.release_history_evidence,
        "release history",
        evidence_type="release-history",
        required_fields=("scope", "tag", "target_commit"),
        expected_fields={"tag": f"v{project_version}"},
        revision_field="target_commit",
        expected_revision=repository_revision,
    )
    release_ok, release_detail = _reviewed_evidence(
        args.release_evidence,
        "release",
        evidence_type="release",
        required_fields=("scope", "tag", "target_commit", "assets"),
        expected_fields={"tag": f"v{project_version}"},
        revision_field="target_commit",
        expected_revision=repository_revision,
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
        expected_revision=repository_revision,
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
        expected_revision=repository_revision,
    )
    platform_evidence_ok, platform_detail = _reviewed_evidence(
        args.platform_evidence,
        "platform",
        evidence_type="platform",
        required_fields=("scope", "targets", "source_revision"),
        revision_field="source_revision",
        expected_revision=repository_revision,
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
    public_ok = public_result.get("status") == "pass"
    credentialed_ok, credentialed_detail = _reviewed_evidence(
        args.credentialed_evidence,
        "credentialed Polymarket",
        evidence_type="credentialed-polymarket",
        required_fields=("scope", "target_tier", "report_hash", "source_revision"),
        expected_fields={"target_tier": "credential_live_verified"},
        revision_field="source_revision",
        expected_revision=repository_revision,
    )
    funded_ok, funded_detail = _reviewed_evidence(
        args.funded_evidence,
        "funded Polymarket",
        evidence_type="funded-polymarket",
        required_fields=("scope", "target_tier", "report_hash", "live_action", "source_revision"),
        expected_fields={"target_tier": "funded_live_verified"},
        revision_field="source_revision",
        expected_revision=repository_revision,
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

    categories = [architecture, tests, security, ci_cd, operations, platform, live]
    score = sum(int(item["earned"]) for item in categories)
    missing = [detail for item in categories for detail in item["missing"] if detail]
    return {
        "score": score,
        "out_of": 100,
        "status": "ready" if score == 100 else "not_ready",
        "categories": categories,
        "missing": missing,
        "checks": {"local": local_result, "public_live": public_result},
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
