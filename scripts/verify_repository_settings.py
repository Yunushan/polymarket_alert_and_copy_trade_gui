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


def check_branch_protection(protection: dict[str, Any], required_checks: Iterable[str] = REQUIRED_CHECKS) -> list[dict[str, str]]:
    """Validate the branch protection controls required by the checked-in policy."""
    status_checks = protection.get("required_status_checks")
    strict = isinstance(status_checks, dict) and status_checks.get("strict") is True
    contexts = _required_contexts(protection)
    missing_contexts = sorted(set(required_checks) - contexts)
    enforce_admins = isinstance(protection.get("enforce_admins"), dict) and protection["enforce_admins"].get("enabled") is True
    pull_requests = protection.get("required_pull_request_reviews") is not None
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
        _check("branch_require_up_to_date", strict, "required_status_checks.strict must be true"),
        _check("branch_enforce_admins", enforce_admins, "administrator bypass must be disabled"),
        _check("branch_require_pull_request", pull_requests, "required_pull_request_reviews must be configured"),
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
    branch_policy = environment.get("deployment_branch_policy")
    protected_branches = isinstance(branch_policy, dict) and branch_policy.get("protected_branches") is True
    required_secret_names = set(REQUIRED_RELEASE_SECRETS)
    missing_secrets = sorted(required_secret_names - {str(name) for name in secret_names})
    return [
        _check("release_required_reviewers", reviewer_rule is not None, "release environment must require reviewer approval"),
        _check("release_prevent_self_review", self_review_disabled, "release environment must prevent self approval"),
        _check("release_protected_branches", protected_branches, "release environment must restrict deployment to protected branches"),
        _check("release_signing_secrets", not missing_secrets, "missing=" + ",".join(missing_secrets) if missing_secrets else "required signing secrets present"),
    ]


def check_release_variable(variable: dict[str, Any]) -> dict[str, str]:
    return _check(
        "release_windows_code_signing_required",
        variable.get("value") == "true",
        "REQUIRE_WINDOWS_CODE_SIGNING must equal true",
    )


REQUIRED_RELEASE_ENVIRONMENT_CHECKS = (
    "release_required_reviewers",
    "release_prevent_self_review",
    "release_protected_branches",
    "release_signing_secrets",
    "release_windows_code_signing_required",
)


def collect_checks(repository: str, branch: str, token: str, timeout: float, request_json: JsonRequest) -> list[dict[str, str]]:
    owner, name = repository.split("/", 1)
    prefix = f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
    protection = request_json(f"{prefix}/branches/{quote(branch, safe='')}/protection", token, timeout)
    environment = request_json(f"{prefix}/environments/release", token, timeout)
    secrets = request_json(f"{prefix}/environments/release/secrets?per_page=100", token, timeout)
    variable = request_json(f"{prefix}/actions/variables/REQUIRE_WINDOWS_CODE_SIGNING", token, timeout)
    if not all(isinstance(value, dict) for value in (protection, environment, secrets, variable)):
        raise RuntimeError("GitHub API returned an unexpected document shape")
    secret_rows = secrets.get("secrets", [])
    secret_names = [row.get("name") for row in secret_rows if isinstance(row, dict) and isinstance(row.get("name"), str)]
    return [
        *check_branch_protection(protection),
        *check_release_environment(environment, secret_names),
        check_release_variable(variable),
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
