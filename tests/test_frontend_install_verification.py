import json
import tempfile
import unittest
from pathlib import Path

import verify


class FrontendInstallVerificationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.package = {"dependencies": {"react": "^19.0.0"}}
        self.lock = {"packages": {"": self.package, "node_modules/react": {"version": "19.2.8"},
                                 "node_modules/platform-optional": {"version": "1.0.0", "optional": True}}}
        self.write_manifest()
        self.install("react", "19.2.8")

    def write_manifest(self):
        (self.root / "package.json").write_text(json.dumps(self.package), encoding="utf-8")
        (self.root / "package-lock.json").write_text(json.dumps(self.lock), encoding="utf-8")

    def install(self, name, version):
        path = self.root / "node_modules" / name / "package.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": version}), encoding="utf-8")

    def test_matching_graph_allows_missing_platform_optional_package(self):
        self.assertEqual(verify.frontend_install_issues(self.root), [])

    def test_stale_direct_package_fails_even_within_declared_semver_range(self):
        self.install("react", "19.2.6")
        self.assertIn("expected 19.2.8, installed 19.2.6", verify.frontend_install_issues(self.root)[0])

    def test_stale_transitive_package_fails(self):
        self.lock["packages"]["node_modules/transitive"] = {"version": "2.0.0"}
        self.install("transitive", "1.0.0")
        self.write_manifest()
        self.assertIn("node_modules/transitive", verify.frontend_install_issues(self.root)[0])

    def test_missing_required_package_fails(self):
        (self.root / "node_modules/react/package.json").unlink()
        self.assertIn("installed missing", verify.frontend_install_issues(self.root)[0])

    def test_stale_installed_optional_package_fails(self):
        self.install("platform-optional", "0.9.0")
        self.assertIn("platform-optional", verify.frontend_install_issues(self.root)[0])

    def test_root_dependency_drift_fails(self):
        (self.root / "package.json").write_text('{"dependencies":{"react":"^20.0.0"}}', encoding="utf-8")
        self.assertIn("differs from package-lock", verify.frontend_install_issues(self.root)[0])

    def test_invalid_installed_json_fails(self):
        (self.root / "node_modules/react/package.json").write_text("{invalid", encoding="utf-8")
        self.assertIn("installed missing", verify.frontend_install_issues(self.root)[0])

    def test_lockfile_cannot_read_outside_node_modules(self):
        self.lock["packages"]["../outside"] = {"version": "1"}
        self.write_manifest()
        self.assertIn("Unverifiable locked package", verify.frontend_install_issues(self.root)[0])


if __name__ == "__main__":
    unittest.main()
