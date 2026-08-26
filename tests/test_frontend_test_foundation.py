import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FrontendTestFoundationTests(unittest.TestCase):
    def test_frontend_test_runner_is_part_of_strict_build_verification(self) -> None:
        package = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
        verify_source = (ROOT / "verify.py").read_text(encoding="utf-8")

        self.assertEqual(package["scripts"]["test"], "node scripts/run-tests.mjs")
        self.assertEqual(package["scripts"]["prebuild"], "npm run test")
        self.assertIn('("dev", "test", "prebuild", "build", "preview")', verify_source)
        self.assertIn('[npm_command(), "run", "build"]', verify_source)

    def test_test_harness_uses_locked_runtime_and_leaves_no_compiled_artifact(self) -> None:
        runner = (ROOT / "frontend" / "scripts" / "run-tests.mjs").read_text(encoding="utf-8")
        package = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
        lock = json.loads((ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8"))

        self.assertIn('node_modules", "typescript", "bin", "tsc"', runner)
        self.assertIn('rmSync(outputRoot, { force: true, recursive: true })', runner)
        self.assertEqual(package["devDependencies"], lock["packages"][""]["devDependencies"])
        self.assertEqual(package["dependencies"], lock["packages"][""]["dependencies"])

    def test_live_preflight_component_and_api_risk_paths_have_behavioral_tests(self) -> None:
        app = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
        component = (ROOT / "frontend" / "src" / "live-preflight-audit.tsx").read_text(encoding="utf-8")
        api_tests = (ROOT / "frontend" / "tests" / "api.test.mjs").read_text(encoding="utf-8")
        component_tests = (ROOT / "frontend" / "tests" / "live-preflight-audit.test.mjs").read_text(encoding="utf-8")

        self.assertIn('import { LivePreflightAudit } from "./live-preflight-audit";', app)
        self.assertIn("payload.blocked || !payload.ok", component)
        self.assertIn("Idempotency-Key", api_tests)
        self.assertIn("must be a JSON object", api_tests)
        self.assertIn('data-preflight-result="blocked"', component_tests)
        self.assertIn('role="alert"', component_tests)


if __name__ == "__main__":
    unittest.main()
