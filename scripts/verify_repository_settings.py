from __future__ import annotations

import argparse
import json
import os
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

try:
    import truststore
except ImportError:  # pragma: no cover - optional for minimal standalone use
    truststore = None


API_VERSION = "2026-03-10"
DEFAULT_API_URL = "https://api.github.com"
REQUIRED_CHECKS = frozenset(
    {
        "Python package build",
        "CodeQL",
        "Dependency review",
        "Frontend dependency audit",
        "Python dependency audit",
    }
)
GITHUB_ACTIONS_APP_ID = 15368
MINIMUM_INDEPENDENT_REVIEWERS = 2
REQUIRED_RELEASE_SECRETS = frozenset(
    {
        "WINDOWS_CODE_SIGNING_CERTIFICATE_BASE64",
        "WINDOWS_CODE_SIGNING_CERTIFICATE_PASSWORD",
    }
)
JsonRequest = Callable[[str, str, float], Any]
_TRUSTSTORE_INJECTED = False


def _ensure_system_trust_store() -> None:
    """Use the host trust store when the optional locked dependency is available."""
    global _TRUSTSTORE_INJECTED
    if _TRUSTSTORE_INJECTED or truststore is None:
        return
    truststore.inject_into_ssl()
    _TRUSTSTORE_INJECTED = True


def _request_json(path: str, token: str, timeout: float, api_url: str = DEFAULT_API_URL) -> Any:
    """Read a GitHub API document without including token material in errors."""
    _ensure_system_trust_store()
    base = api_url.rstrip("/")
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "market-sentinel-repository-settings-verifier",
    }
    try:
        with urlopen(Request(f"{base}{path}", headers=headers, method="GET"), timeout=timeout) as response:
            if response.status != 200:
                raise RuntimeError(f"GitHub API {path} returned HTTP {response.status}")
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            raise RuntimeError(f"GitHub API {path} returned HTTP {exc.code}") from exc
        finally:
            exc.close()
    except (OSError, URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"GitHub API request failed for {path}: {type(exc).__name__}") from exc


def _check(name: str, passed: bool, detail: str) -> dict[str, str]:
    return {"name": name, "status": "pass" if passed else "fail", "detail": detail}


def _required_contexts(protection: dict[str, Any]) -> set[str]:
    status_checks = protection.get("required_status_checks")
    if not isinstance(status_checks, dict):
        return set()
    contexts = {str(value) for value in status_checks.get("contexts", []) if isinstance(value, str)}
    for entry in status_checks.get("checks", []):
        if isinstance(entry, dict) and isinstance(entry.get("context"), str):
            contexts.add(entry["context"])
    return contexts


def _actions_app_contexts(protection: dict[str, Any]) -> set[str]:
    status_checks = protection.get("required_status_checks")
    if not isinstance(status_checks, dict):
        return set()
    return {
        str(entry["context"])
        for entry in status_checks.get("checks", [])
        if isinstance(entry, dict)
        and isinstance(entry.get("context"), str)
        and entry.get("app_id") == GITHUB_ACTIONS_APP_ID
    }


def _reviewer_count(reviewer_rule: Any) -> int:
    if not isinstance(reviewer_rule, dict):
        return 0
    identities: set[tuple[str, int]] = set()
    for row in reviewer_rule.get("reviewers", []):
        reviewer = row.get("reviewer") if isinstance(row, dict) else None
        reviewer_type = row.get("type") if isinstance(row, dict) else None
        reviewer_id = reviewer.get("id") if isinstance(reviewer, dict) else None
        if isinstance(reviewer_type, str) and type(reviewer_id) is int and reviewer_id > 0:
            identities.add((reviewer_type, reviewer_id))
    return len(identities)


def check_branch_protection(protection: dict[str, Any], required_checks: Iterable[str] = REQUIRED_CHECKS) -> list[dict[str, str]]:
    """Validate the branch protection controls required by the checked-in policy."""
    status_checks = protection.get("required_status_checks")
    strict = isinstance(status_checks, dict) and status_checks.get("strict") is True
    contexts = _required_contexts(protection)
    missing_contexts = sorted(set(required_checks) - contexts)
    missing_actions_bindings = sorted(set(required_checks) - _actions_app_contexts(protection))
    enforce_admins = isinstance(protection.get("enforce_admins"), dict) and protection["enforce_admins"].get("enabled") is True
    pull_request_rule = protection.get("required_pull_request_reviews")
    pull_requests = isinstance(pull_request_rule, dict)
    approvals = pull_request_rule.get("required_approving_review_count") if isinstance(pull_request_rule, dict) else None
    minimum_approvals = type(approvals) is int and approvals >= 1
    dismiss_stale = isinstance(pull_request_rule, dict) and pull_request_rule.get("dismiss_stale_reviews") is True
    last_push_approval = (
        isinstance(pull_request_rule, dict) and pull_request_rule.get("require_last_push_approval") is True
    )
    conversation = isinstance(protection.get("required_conversation_resolution"), dict) and protection[
        "required_conversation_resolution"
    ].get("enabled") is True
    linear_history = isinstance(protection.get("required_linear_history"), dict) and protection["required_linear_history"].get(
        "enabled"
    ) is True
    force_pushes = isinstance(protection.get("allow_force_pushes"), dict) and protection["allow_force_pushes"].get("enabled") is True
    deletions = isinstance(protection.get("allow_deletions"), dict) and protection["allow_deletions"].get("enabled") is True
    return [
        _check("branch_required_status_checks", not missing_contexts, "missing=" + ",".join(missing_contexts) if missing_contexts else "all required checks configured"),
        _check(
            "branch_status_checks_bound_to_actions_app",
            not missing_actions_bindings,
            "missing=" + ",".join(missing_actions_bindings)
            if missing_actions_bindings
            else f"all required checks are bound to GitHub Actions app_id={GITHUB_ACTIONS_APP_ID}",
        ),
        _check("branch_require_up_to_date", strict, "required_status_checks.strict must be true"),
        _check("branch_enforce_admins", enforce_admins, "administrator bypass must be disabled"),
        _check("branch_require_pull_request", pull_requests, "required_pull_request_reviews must be configured"),
        _check("branch_minimum_approvals", minimum_approvals, "at least one approving review must be required"),
        _check("branch_dismiss_stale_reviews", dismiss_stale, "stale approvals must be dismissed after new commits"),
        _check(
            "branch_require_last_push_approval",
            last_push_approval,
            "the most recent push must be approved by someone other than its author",
        ),
        _check("branch_conversation_resolution", conversation, "required conversation resolution must be enabled"),
        _check("branch_linear_history", linear_history, "required linear history must be enabled"),
        _check("branch_force_pushes_disabled", not force_pushes, "force pushes must be disabled"),
        _check("branch_deletions_disabled", not deletions, "branch deletion must be disabled"),
    ]


def check_release_environment(environment: dict[str, Any], secret_names: Iterable[str]) -> list[dict[str, str]]:
    """Validate release approvals, branch restrictions, signing configuration, and secret presence."""
    rules = environment.get("protection_rules")
    rules = rules if isinstance(rules, list) else []
    reviewer_rule = next((rule for rule in rules if isinstance(rule, dict) and rule.get("type") == "required_reviewers"), None)
    self_review_disabled = isinstance(reviewer_rule, dict) and reviewer_rule.get("prevent_self_review") is True
    reviewer_count = _reviewer_count(reviewer_rule)
    branch_policy = environment.get("deployment_branch_policy")
    protected_branches = isinstance(branch_policy, dict) and branch_policy.get("protected_branches") is True
    required_secret_names = set(REQUIRED_RELEASE_SECRETS)
    missing_secrets = sorted(required_secret_names - {str(name) for name in secret_names})
    return [
        _check("release_required_reviewers", reviewer_rule is not None, "release environment must require reviewer approval"),
        _check(
            "release_independent_reviewers",
            reviewer_count >= MINIMUM_INDEPENDENT_REVIEWERS,
            f"release environment needs at least {MINIMUM_INDEPENDENT_REVIEWERS} distinct eligible reviewers; found {reviewer_count}",
        ),
        _check("release_prevent_self_review", self_review_disabled, "release environment must prevent self approval"),
        _check("release_protected_branches", protected_branches, "release environment must restrict deployment to protected branches"),
        _check("release_signing_secrets", not missing_secrets, "missing=" + ",".join(missing_secrets) if missing_secrets else "required signing secrets present"),
    ]


def check_production_environment(environment: dict[str, Any], secret_names: Iterable[str]) -> list[dict[str, str]]:
    """Validate the independent approval and secret inventory used by production evidence lanes."""
    rules = environment.get("protection_rules")
    rules = rules if isinstance(rules, list) else []
    reviewer_rule = next(
        (rule for rule in rules if isinstance(rule, dict) and rule.get("type") == "required_reviewers"),
        None,
    )
    reviewer_count = _reviewer_count(reviewer_rule)
    branch_policy = environment.get("deployment_branch_policy")
    protected_branches = isinstance(branch_policy, dict) and branch_policy.get("protected_branches") is True
    missing_secrets = sorted(REQUIRED_PRODUCTION_SECRETS - {str(name) for name in secret_names})
    return [
        _check("production_required_reviewers", reviewer_rule is not None, "production environment must require reviewer approval"),
        _check(
            "production_independent_reviewers",
            reviewer_count >= MINIMUM_INDEPENDENT_REVIEWERS,
            f"production environment needs at least {MINIMUM_INDEPENDENT_REVIEWERS} distinct eligible reviewers; found {reviewer_count}",
        ),
        _check(
            "production_prevent_self_review",
            isinstance(reviewer_rule, dict) and reviewer_rule.get("prevent_self_review") is True,
            "production environment must prevent self approval",
        ),
        _check(
            "production_protected_branches",
            protected_branches,
            "production environment must restrict deployment to protected branches",
        ),
        _check(
            "production_secrets",
            not missing_secrets,
            "missing=" + ",".join(missing_secrets) if missing_secrets else "required production secrets present",
        ),
    ]


def check_release_variable(variable: dict[str, Any]) -> dict[str, str]:
    return _check(
        "release_windows_code_signing_required",
        variable.get("value") == "true",
        "REQUIRE_WINDOWS_CODE_SIGNING must equal true",
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
REQUIRED_PRODUCTION_SECRETS = frozenset(
    {
        "POLY_ADDRESS",
        "POLY_API_KEY",
        "POLY_API_SECRET",
        "POLY_PASSPHRASE",
        "POLYMARKET_PRIVATE_KEY",
        "POLYMARKET_RECOVERY_STORE_URL",
        "POLYMARKET_RECOVERY_STORE_TOKEN",
        "POLYMARKET_RECOVERY_ENCRYPTION_KEY_BASE64",
    }
)


def collect_checks(repository: str, branch: str, token: str, timeout: float, request_json: JsonRequest) -> list[dict[str, str]]:
    owner, name = repository.split("/", 1)
    prefix = f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
    protection = request_json(f"{prefix}/branches/{quote(branch, safe='')}/protection", token, timeout)
    environment = request_json(f"{prefix}/environments/release", token, timeout)
    secrets = request_json(f"{prefix}/environments/release/secrets?per_page=100", token, timeout)
    production_environment = request_json(f"{prefix}/environments/production", token, timeout)
    production_secrets = request_json(f"{prefix}/environments/production/secrets?per_page=100", token, timeout)
    variable = request_json(f"{prefix}/actions/variables/REQUIRE_WINDOWS_CODE_SIGNING", token, timeout)
    if not all(
        isinstance(value, dict)
        for value in (protection, environment, secrets, production_environment, production_secrets, variable)
    ):
        raise RuntimeError("GitHub API returned an unexpected document shape")
    secret_rows = secrets.get("secrets", [])
    secret_names = [row.get("name") for row in secret_rows if isinstance(row, dict) and isinstance(row.get("name"), str)]
    production_secret_rows = production_secrets.get("secrets", [])
    production_secret_names = [
        row.get("name")
        for row in production_secret_rows
        if isinstance(row, dict) and isinstance(row.get("name"), str)
    ]
    return [
        *check_branch_protection(protection),
        *check_release_environment(environment, secret_names),
        check_release_variable(variable),
        *check_production_environment(production_environment, production_secret_names),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect read-only GitHub production-governance evidence for MarketSentinel.")
    parser.add_argument("--repository", required=True, help="GitHub repository in OWNER/REPOSITORY form.")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--token-env", default="GITHUB_TOKEN", help="Environment variable holding an administration-read token.")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help="GitHub API base URL; intended for GitHub Enterprise Server.")
    args = parser.parse_args()
    repository = args.repository.strip()
    if repository.count("/") != 1 or any(not value.strip() for value in repository.split("/", 1)):
        raise SystemExit("--repository must use OWNER/REPOSITORY form")
    token = os.environ.get(args.token_env, "").strip()
    if not token:
        raise SystemExit(f"{args.token_env} must contain a GitHub token with Administration read and Actions read access")
    try:
        checks = collect_checks(
            repository,
            args.branch.strip() or "main",
            token,
            max(1.0, args.timeout),
            lambda path, request_token, request_timeout: _request_json(path, request_token, request_timeout, args.api_url),
        )
    except RuntimeError as exc:
        checks = [{"name": "repository_governance", "status": "fail", "detail": str(exc)}]
    payload = {"repository": repository, "branch": args.branch.strip() or "main", "status": "ok" if all(check["status"] == "pass" for check in checks) else "failed", "checks": checks}
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
