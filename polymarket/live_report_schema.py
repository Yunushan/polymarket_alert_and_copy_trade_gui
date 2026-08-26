from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any, Dict, Mapping, Optional


LIVE_VALIDATION_REPORT_SCHEMA_VERSION = 1
EXPECTED_REPOSITORY_ORIGIN = "github.com/yunushan/market-sentinel"
CREDENTIAL_PROMOTION_CHECKS = (
    "clob_l2_orders",
    "relayer_recent_transactions",
)
CREDENTIAL_PROMOTION_SEMANTICS = {
    "clob_l2_orders": "authenticated_order_collection",
    "relayer_recent_transactions": "authenticated_collection",
}
PUBLIC_PROMOTION_CHECKS = (
    "clob_time",
    "gamma_markets",
    "data_leaderboard",
    "bridge_supported_assets",
)
ACCEPTED_LIVE_VALIDATION_REPORT_MODES = {
    "strict_cli",
    "local_readiness_only",
    "credential_runbook_no_funded_actions",
    "browser_smoke",
    "browser_smoke_seed",
}
RECOMMENDED_STAGE_GATE_FIELDS = (
    "public_live_checks",
    "credential_readiness",
    "credentialed_read_checks",
    "bridge_address_checks",
    "funded_live_order_check",
    "credentialed_read_ok",
    "safe_to_attempt_funded_order",
    "requires_explicit_live_approval",
    "next_step",
)
BOOLEAN_STAGE_GATE_FIELDS = (
    "credentialed_read_ok",
    "safe_to_attempt_funded_order",
    "requires_explicit_live_approval",
)
KNOWN_CHECK_STATUSES = {"ok", "failed", "blocked", "skipped", "dry_run", "ready_to_execute"}
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
SOURCE_PROVENANCE_FIELDS = frozenset(
    {
        "schema_version",
        "repository",
        "repository_origin",
        "source_revision",
        "initial_revision",
        "final_revision",
        "initial_clean",
        "final_clean",
        "stable",
    }
)


class LiveValidationReportSchemaError(ValueError):
    def __init__(self, validation: Mapping[str, Any]) -> None:
        self.validation = dict(validation)
        errors = self.validation.get("errors") if isinstance(self.validation.get("errors"), list) else []
        detail = "; ".join(str(item) for item in errors) or "Live validation report schema validation failed."
        super().__init__(detail)


def parse_live_validation_report_json(text: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        validation = _validation_result(
            mode=None,
            errors=[f"report_json must be valid JSON: {exc.msg}"],
            warnings=[],
            report_type="invalid_json",
        )
        raise LiveValidationReportSchemaError(validation) from exc
    if not isinstance(parsed, Mapping):
        validation = _validation_result(
            mode=None,
            errors=["report_json must decode to an object."],
            warnings=[],
            report_type="invalid_shape",
        )
        raise LiveValidationReportSchemaError(validation)
    return dict(parsed)


def validate_live_validation_report(report: Mapping[str, Any]) -> Dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(report, Mapping):
        return _validation_result(
            mode=None,
            errors=["Live validation report must be an object."],
            warnings=[],
            report_type="invalid_shape",
        )
    if not report:
        errors.append("Live validation report must not be empty.")

    mode = _clean_string(report.get("mode"))
    if not mode:
        errors.append("Live validation report requires a non-empty string mode.")
    elif mode not in ACCEPTED_LIVE_VALIDATION_REPORT_MODES:
        errors.append(
            "Live validation report mode must be one of: "
            + ", ".join(sorted(ACCEPTED_LIVE_VALIDATION_REPORT_MODES))
            + "."
        )

    generated_at = report.get("generated_at")
    if generated_at is None:
        warnings.append("generated_at is missing; report chronology will rely on storage time.")
    elif not _is_number(generated_at):
        errors.append("generated_at must be numeric when present.")

    report_type = _report_type_for_mode(mode)
    if mode == "credential_runbook_no_funded_actions":
        _validate_runbook_report(report, errors, warnings)
    else:
        _validate_live_stage_report(report, errors, warnings)

    return _validation_result(
        mode=mode or None,
        errors=errors,
        warnings=warnings,
        report_type=report_type,
    )


def ensure_live_validation_report_valid(report: Mapping[str, Any]) -> Dict[str, Any]:
    validation = validate_live_validation_report(report)
    if not validation["ok"]:
        raise LiveValidationReportSchemaError(validation)
    return validation


def compact_schema_validation(validation: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "schema_version": int(validation.get("schema_version") or LIVE_VALIDATION_REPORT_SCHEMA_VERSION),
        "ok": bool(validation.get("ok")),
        "mode": validation.get("mode"),
        "report_type": validation.get("report_type"),
        "errors": list(validation.get("errors") or []),
        "warnings": list(validation.get("warnings") or []),
        "accepted_modes": list(validation.get("accepted_modes") or sorted(ACCEPTED_LIVE_VALIDATION_REPORT_MODES)),
    }


def _validate_live_stage_report(report: Mapping[str, Any], errors: list[str], warnings: list[str]) -> None:
    _validate_source_provenance(report.get("source_provenance"), errors, warnings)
    stage_gates = report.get("stage_gates")
    if not isinstance(stage_gates, Mapping):
        errors.append("stage_gates must be an object for live-validation reports.")
        return
    for field in RECOMMENDED_STAGE_GATE_FIELDS:
        if field not in stage_gates:
            warnings.append(f"stage_gates.{field} is missing.")
    for field in BOOLEAN_STAGE_GATE_FIELDS:
        if field in stage_gates and not isinstance(stage_gates.get(field), bool):
            errors.append(f"stage_gates.{field} must be boolean when present.")

    _validate_check_section(report, "public_checks", warnings)
    _validate_check_section(report, "authenticated_read_checks", warnings)
    _validate_check_section(report, "bridge_address_checks", warnings)
    funded = report.get("funded_live_order_check")
    if funded is None:
        warnings.append("funded_live_order_check is missing.")
    elif not isinstance(funded, Mapping):
        errors.append("funded_live_order_check must be an object when present.")
    elif "status" not in funded:
        warnings.append("funded_live_order_check.status is missing.")
    elif str(funded.get("status")) not in KNOWN_CHECK_STATUSES:
        warnings.append(f"funded_live_order_check.status has an unrecognized value: {funded.get('status')!r}.")


def _validate_source_provenance(value: Any, errors: list[str], warnings: list[str]) -> None:
    if value is None:
        warnings.append("source_provenance is missing; this report cannot prove an exact clean source revision.")
        return
    if not isinstance(value, Mapping):
        errors.append("source_provenance must be an object when present.")
        return
    if set(value) != SOURCE_PROVENANCE_FIELDS:
        errors.append("source_provenance does not match the exact reviewed field contract.")
        return
    if value.get("schema_version") != 1 or value.get("repository") != "market-sentinel":
        errors.append("source_provenance schema_version or repository is invalid.")
    repository_origin = value.get("repository_origin")
    if not isinstance(repository_origin, str) or repository_origin not in {"", EXPECTED_REPOSITORY_ORIGIN}:
        errors.append("source_provenance.repository_origin is not the canonical reviewed GitHub origin.")
    for field in ("initial_clean", "final_clean", "stable"):
        if type(value.get(field)) is not bool:
            errors.append(f"source_provenance.{field} must be boolean.")
    for field in ("initial_revision", "final_revision"):
        revision = value.get(field)
        if not isinstance(revision, str) or (revision and not _COMMIT_RE.fullmatch(revision)):
            errors.append(f"source_provenance.{field} must be empty or a 40-character lowercase commit SHA.")
    source_revision = value.get("source_revision")
    if not isinstance(source_revision, str) or (source_revision and not _COMMIT_RE.fullmatch(source_revision)):
        errors.append("source_provenance.source_revision must be empty or a 40-character lowercase commit SHA.")
    if value.get("stable") is True:
        revisions_match = (
            bool(source_revision)
            and source_revision == value.get("initial_revision") == value.get("final_revision")
        )
        if (
            value.get("initial_clean") is not True
            or value.get("final_clean") is not True
            or repository_origin != EXPECTED_REPOSITORY_ORIGIN
            or not revisions_match
        ):
            errors.append(
                "stable source_provenance requires the canonical repository origin and matching clean "
                "initial/final/source revisions."
            )
    elif source_revision:
        errors.append("unstable source_provenance must not claim source_revision.")


def _validate_runbook_report(report: Mapping[str, Any], errors: list[str], warnings: list[str]) -> None:
    if not isinstance(report.get("env_inventory"), Mapping):
        errors.append("credential runbook reports require env_inventory.")
    if not isinstance(report.get("readiness"), Mapping):
        errors.append("credential runbook reports require readiness.")
    if report.get("funded_execution_exposed") is not False:
        errors.append("credential runbook reports must set funded_execution_exposed=false.")
    if report.get("network_calls") not in (None, "none"):
        errors.append("credential runbook reports must not perform network calls.")
    if "stage_gates" in report:
        warnings.append("credential runbook reports do not need stage_gates; promotion remains blocked.")


def _validate_check_section(report: Mapping[str, Any], key: str, warnings: list[str]) -> None:
    section = report.get(key)
    if section is None:
        warnings.append(f"{key} is missing.")
        return
    if not isinstance(section, Mapping):
        warnings.append(f"{key} should be an object.")
        return
    for check_name, item in section.items():
        if not isinstance(item, Mapping):
            warnings.append(f"{key}.{check_name} should be an object.")
            continue
        if "status" not in item:
            warnings.append(f"{key}.{check_name}.status is missing.")
        elif str(item.get("status")) not in KNOWN_CHECK_STATUSES:
            warnings.append(f"{key}.{check_name}.status has an unrecognized value: {item.get('status')!r}.")


def _validation_result(
    *,
    mode: Optional[str],
    errors: list[str],
    warnings: list[str],
    report_type: str,
) -> Dict[str, Any]:
    return {
        "schema_version": LIVE_VALIDATION_REPORT_SCHEMA_VERSION,
        "ok": not errors,
        "mode": mode,
        "report_type": report_type,
        "errors": deepcopy(errors),
        "warnings": deepcopy(warnings),
        "accepted_modes": sorted(ACCEPTED_LIVE_VALIDATION_REPORT_MODES),
    }


def _report_type_for_mode(mode: str) -> str:
    if mode == "credential_runbook_no_funded_actions":
        return "credential_runbook"
    if mode in {"browser_smoke", "browser_smoke_seed"}:
        return "browser_smoke"
    if mode == "local_readiness_only":
        return "gui_readiness"
    if mode == "strict_cli":
        return "cli_live_validation"
    return "unknown"


def _clean_string(value: Any) -> str:
    return str(value or "").strip()


def _is_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    return isinstance(value, (int, float))
