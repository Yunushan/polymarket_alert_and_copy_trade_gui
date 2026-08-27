from __future__ import annotations

import unittest
from unittest.mock import patch

import scripts.verify_repository_settings as repository_settings
from scripts.verify_repository_settings import (
    GITHUB_ACTIONS_APP_ID,
    REQUIRED_CHECKS,
    REQUIRED_PRODUCTION_SECRETS,
    REQUIRED_RELEASE_ENVIRONMENT_CHECKS,
    check_branch_protection,
    check_production_environment,
    check_release_environment,
    check_release_variable,
    collect_checks,
)


def _passing_protection() -> dict:
    return {
        "required_status_checks": {
            "strict": True,
            "contexts": sorted(REQUIRED_CHECKS),
            "checks": [
                {"context": context, "app_id": GITHUB_ACTIONS_APP_ID}
                for context in sorted(REQUIRED_CHECKS)
            ],
        },
        "enforce_admins": {"enabled": True},
        "required_pull_request_reviews": {
            "required_approving_review_count": 1,
            "dismiss_stale_reviews": True,
            "require_last_push_approval": True,
        },
        "required_conversation_resolution": {"enabled": True},
        "required_linear_history": {"enabled": True},
        "allow_force_pushes": {"enabled": False},
        "allow_deletions": {"enabled": False},
    }


def _passing_environment() -> dict:
    return {
        "protection_rules": [
            {
                "type": "required_reviewers",
                "prevent_self_review": True,
                "reviewers": [
                    {"type": "User", "reviewer": {"id": 1}},
                    {"type": "User", "reviewer": {"id": 2}},
                ],
            }
        ],
        "deployment_branch_policy": {"protected_branches": True},
    }


class RepositorySettingsTests(unittest.TestCase):
    def test_release_environment_check_contract_matches_readiness_scorer(self) -> None:
        from scripts.check_product_readiness import REQUIRED_RELEASE_ENVIRONMENT_CHECKS as SCORER_CHECKS

        generated = {
            check["name"]
            for check in [
                *check_release_environment(
                    _passing_environment(),
                    ["WINDOWS_CODE_SIGNING_CERTIFICATE_BASE64", "WINDOWS_CODE_SIGNING_CERTIFICATE_PASSWORD"],
                ),
                check_release_variable({"value": "true"}),
                *check_production_environment(_passing_environment(), REQUIRED_PRODUCTION_SECRETS),
            ]
        }
        self.assertEqual(tuple(REQUIRED_RELEASE_ENVIRONMENT_CHECKS), tuple(SCORER_CHECKS))
        self.assertEqual(generated, set(REQUIRED_RELEASE_ENVIRONMENT_CHECKS))

    def test_request_transport_uses_system_trust_store_when_available(self) -> None:
        previous = repository_settings._TRUSTSTORE_INJECTED
        try:
            repository_settings._TRUSTSTORE_INJECTED = False
            with patch.object(repository_settings, "truststore") as truststore:
                repository_settings._ensure_system_trust_store()
                truststore.inject_into_ssl.assert_called_once_with()
                self.assertTrue(repository_settings._TRUSTSTORE_INJECTED)
        finally:
            repository_settings._TRUSTSTORE_INJECTED = previous

    def test_branch_protection_requires_all_documented_controls(self) -> None:
        checks = check_branch_protection(_passing_protection())
        self.assertTrue(all(check["status"] == "pass" for check in checks))

        weak = _passing_protection()
        weak["required_status_checks"] = {"strict": False, "contexts": ["CodeQL"]}
        weak["required_pull_request_reviews"] = {
            "required_approving_review_count": 0,
            "dismiss_stale_reviews": False,
            "require_last_push_approval": False,
        }
        weak["allow_force_pushes"] = {"enabled": True}
        names = {check["name"] for check in check_branch_protection(weak) if check["status"] == "fail"}
        self.assertIn("branch_required_status_checks", names)
        self.assertIn("branch_status_checks_bound_to_actions_app", names)
        self.assertIn("branch_require_up_to_date", names)
        self.assertIn("branch_force_pushes_disabled", names)
        self.assertIn("branch_minimum_approvals", names)
        self.assertIn("branch_dismiss_stale_reviews", names)
        self.assertIn("branch_require_last_push_approval", names)

    def test_release_environment_requires_reviewers_branches_and_signing_secrets(self) -> None:
        secret_names = ["WINDOWS_CODE_SIGNING_CERTIFICATE_BASE64", "WINDOWS_CODE_SIGNING_CERTIFICATE_PASSWORD"]
        checks = check_release_environment(_passing_environment(), secret_names)
        self.assertTrue(all(check["status"] == "pass" for check in checks))
        self.assertEqual(check_release_variable({"value": "true"})["status"], "pass")

        weak = {"protection_rules": [], "deployment_branch_policy": {"protected_branches": False}}
        failures = {check["name"] for check in check_release_environment(weak, []) if check["status"] == "fail"}
        self.assertEqual(
            failures,
            {
                "release_required_reviewers",
                "release_independent_reviewers",
                "release_prevent_self_review",
                "release_protected_branches",
                "release_signing_secrets",
            },
        )
        self.assertEqual(check_release_variable({"value": "false"})["status"], "fail")

    def test_collection_uses_documented_read_only_api_endpoints(self) -> None:
        requested: list[str] = []
        documents = {
            "/repos/acme/market-sentinel/branches/main/protection": _passing_protection(),
            "/repos/acme/market-sentinel/environments/release": _passing_environment(),
            "/repos/acme/market-sentinel/environments/release/secrets?per_page=100": {
                "secrets": [
                    {"name": "WINDOWS_CODE_SIGNING_CERTIFICATE_BASE64"},
                    {"name": "WINDOWS_CODE_SIGNING_CERTIFICATE_PASSWORD"},
                ]
            },
            "/repos/acme/market-sentinel/environments/production": _passing_environment(),
            "/repos/acme/market-sentinel/environments/production/secrets?per_page=100": {
                "secrets": [{"name": name} for name in sorted(REQUIRED_PRODUCTION_SECRETS)]
            },
            "/repos/acme/market-sentinel/actions/variables/REQUIRE_WINDOWS_CODE_SIGNING": {"value": "true"},
        }

        def request(path: str, token: str, timeout: float):
            requested.append(path)
            self.assertEqual(token, "not-a-real-token")
            self.assertEqual(timeout, 5.0)
            return documents[path]

        checks = collect_checks("acme/market-sentinel", "main", "not-a-real-token", 5.0, request)
        self.assertEqual(requested, list(documents))
        self.assertTrue(all(check["status"] == "pass" for check in checks))


if __name__ == "__main__":
    unittest.main()
