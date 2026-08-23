from __future__ import annotations

import unittest
from pathlib import Path

from scripts.verify_dependency_lock import lock_issues


ROOT = Path(__file__).resolve().parent.parent


class DependencyLockTests(unittest.TestCase):
    def test_repository_lock_covers_direct_dependencies_with_hashes(self) -> None:
        project = ROOT / "pyproject.toml"
        lock = ROOT / "requirements.lock"
        self.assertTrue(lock.exists())
        dependencies = [
            "exceptiongroup>=1.0.2; python_version < '3.11'",
            "requests>=2.31.0",
            "truststore>=0.10.0",
            "websocket-client>=1.7.0",
            "tomli>=2.0.0; python_version < '3.11'",
        ]
        self.assertEqual([], lock_issues(lock.read_text(encoding="utf-8"), dependencies), project.read_text(encoding="utf-8"))

    def test_runtime_lock_excludes_test_tooling(self) -> None:
        lock = (ROOT / "requirements.lock").read_text(encoding="utf-8")
        self.assertNotIn("pytest==", lock)
        self.assertNotIn("coverage==", lock)

    def test_runtime_derived_locks_include_python_310_requirements_with_hashes(self) -> None:
        for name in ("requirements.lock", "requirements-live.lock", "requirements-test.lock"):
            with self.subTest(name=name):
                lock = (ROOT / name).read_text(encoding="utf-8")
                self.assertIn('exceptiongroup==1.3.1 ; python_version < "3.11"', lock)
                self.assertIn("--hash=sha256:8b412432c6055b0b7d14c310000ae93352ed6754f70fa8f7c34141f91c4e3219", lock)
                self.assertIn("--hash=sha256:a7a39a3bd276781e98394987d3a5701d0c4edffb633bb7a5144577f82c773598", lock)
                self.assertIn('tomli==2.2.1 ; python_version < "3.11"', lock)
                self.assertIn("--hash=sha256:cb55c73c5f4408779d0cf3eef9f762b9c9f147a77de7b258bef0a5628adc85cc", lock)

    def test_build_lock_includes_hash_pinned_distribution_build_toolchain(self) -> None:
        source = (ROOT / "requirements-build.txt").read_text(encoding="utf-8")
        lock = (ROOT / "requirements-build.lock").read_text(encoding="utf-8")
        requirements = [line.strip() for line in source.splitlines() if line.strip() and not line.startswith("#")]
        self.assertGreaterEqual(len(requirements), 2)
        for requirement in requirements:
            self.assertIn(requirement, lock)
        self.assertIn("pyproject-hooks==1.2.0", lock)

    def test_test_lock_covers_runtime_and_test_dependencies(self) -> None:
        lock = (ROOT / "requirements-test.lock").read_text(encoding="utf-8")
        self.assertEqual(
            [],
            lock_issues(
                lock,
                ["requests>=2.31.0", "py-clob-client>=0.34.0", "pytest>=8.0", "coverage[toml]>=7.6", "ruff==0.16.1"],
            ),
        )

    def test_live_lock_covers_authenticated_clob_sdk_dependencies(self) -> None:
        lock = (ROOT / "requirements-live.lock").read_text(encoding="utf-8")
        self.assertEqual([], lock_issues(lock, ["requests>=2.31.0", "py-clob-client>=0.34.0"]))

    def test_security_audit_lock_is_hash_protected(self) -> None:
        source = (ROOT / "requirements-security.txt").read_text(encoding="utf-8")
        lock = (ROOT / "requirements-security.lock").read_text(encoding="utf-8")
        self.assertEqual("pip-audit==2.10.1\n", source)
        self.assertEqual([], lock_issues(lock, ["pip-audit==2.10.1"]))

    def test_bootstrap_pip_lock_is_hash_protected(self) -> None:
        source = (ROOT / "requirements-bootstrap.txt").read_text(encoding="utf-8")
        lock = (ROOT / "requirements-bootstrap.lock").read_text(encoding="utf-8")
        self.assertEqual("pip==26.2.1\n", source)
        self.assertEqual([], lock_issues(lock, ["pip==26.2.1"]))

    def test_standalone_lock_verifier_covers_security_audit_lock(self) -> None:
        verifier = (ROOT / "scripts" / "verify_dependency_lock.py").read_text(encoding="utf-8")
        self.assertIn("SECURITY_LOCK_PATH", verifier)
        self.assertIn("SECURITY_REQUIREMENTS_PATH", verifier)
        self.assertIn("BOOTSTRAP_LOCK_PATH", verifier)
        self.assertIn("BOOTSTRAP_REQUIREMENTS_PATH", verifier)

    def test_lock_validation_rejects_unhashed_or_missing_direct_dependency(self) -> None:
        lock = "requests==2.0.0\n"
        issues = lock_issues(lock, ["requests>=2", "truststore>=1"])
        self.assertIn("requests is not hash protected", issues)
        self.assertIn("direct dependency truststore is missing from requirements.lock", issues)


if __name__ == "__main__":
    unittest.main()
