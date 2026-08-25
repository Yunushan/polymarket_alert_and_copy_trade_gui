from __future__ import annotations

import unittest
from pathlib import Path

from verify import workflow_action_pin_issues


ROOT = Path(__file__).resolve().parent.parent


class CiCdWorkflowTests(unittest.TestCase):
    def test_ci_workflow_covers_python_frontend_and_artifacts(self) -> None:
        text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

        for fragment in (
            "permissions:",
            "contents: read",
            "concurrency:",
            "cancel-in-progress: true",
            "PIP_NO_CACHE_DIR",
            "ubuntu-latest",
            "ubuntu-24.04",
            "macos-14",
            "macos-15",
            "macos-26",
            "windows-2025-vs2026",
            '"3.10"',
            '"3.11"',
            '"3.12"',
            '"3.13"',
            '"3.14"',
            '"3.x"',
            "Future Python",
            'node-version: "24"',
            "python app.py --smoke-test",
            "Tkinter GUI lifecycle / Ubuntu",
            "xvfb-run --auto-servernum python app.py --gui-smoke-test",
            "PREDICTION_MARKET_CONFIG_PATH",
            "python verify.py",
            "python -m pip install --no-cache-dir --require-hashes -r requirements-bootstrap.lock",
            "python -m pip install --no-cache-dir --require-hashes -r requirements-test.lock",
            "python -m pip install --no-cache-dir --require-hashes -r requirements-build.lock",
            "python -m pip install --no-cache-dir --no-deps -e .",
            "scripts/ci_enterprise_linux_smoke.py",
            "RHEL 8 UBI / Python 3.12",
            "RHEL 9 UBI / Python 3.12",
            "RHEL 10 UBI / Python 3.12 minimal",
            "RHEL 7 ABI / manylinux2014 Python 3.10",
            "Rocky Linux 8 / Python 3.12",
            "Rocky Linux 9 / Python 3.12",
            "Rocky Linux 10 / Python 3.12",
            "registry.access.redhat.com/ubi8/python-312:latest",
            "registry.access.redhat.com/ubi9/python-312:latest",
            "registry.access.redhat.com/ubi10/python-312-minimal:latest",
            "quay.io/pypa/manylinux2014_x86_64:latest",
            "rockylinux/rockylinux:8",
            "rockylinux/rockylinux:9",
            "rockylinux/rockylinux:10",
            "Windows 11 ARM runner / Python 3.12 x64",
            "windows-11-arm",
            'architecture: "x64"',
            "Windows 10 self-hosted / Python 3.12",
            "ENABLE_WINDOWS_10_SELF_HOSTED",
            "windows-10",
            "Mobile web smoke",
            "scripts/verify_mobile_web_smoke.py",
            "android-14",
            "android-15",
            "android-16",
            "ios-15",
            "ios-16",
            "ios-18",
            "ios-26",
            "docker run --rm",
            "npm run build",
            "npm install --no-audit --no-fund",
            "python -m build",
            "python -m build --no-isolation",
            "Smoke install built wheel",
            "--force-reinstall --no-deps",
            "License-Expression",
            "fetch-depth: 0",
            "scripts/verify_python_dist_artifacts.py",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, text)
        self.assertNotIn("cache: pip", text)
        self.assertNotIn("cache-dependency-path", text)
        self.assertNotIn("macos-latest", text)
        self.assertNotIn("windows-latest", text)
        self.assertNotIn("python -m pip install --no-cache-dir build", text)
        enterprise_linux = text.split("  enterprise-linux:\n", 1)[1].split("  windows-11:\n", 1)[0]
        self.assertIn('desktop_validation: "true"', enterprise_linux)
        self.assertIn('desktop_validation: "false"', enterprise_linux)
        self.assertIn("python3.12-tkinter", enterprise_linux)
        self.assertIn("sudo apt-get install --yes xvfb", enterprise_linux)
        self.assertIn("Xvfb :99 -screen 0 1280x1024x24 -nolisten tcp", enterprise_linux)
        self.assertIn("-v /tmp/.X11-unix:/tmp/.X11-unix", enterprise_linux)
        self.assertIn("CI_DESKTOP_VALIDATION", enterprise_linux)
        self.assertEqual(
            enterprise_linux.count(
                "bootstrap: dnf -y upgrade --refresh && dnf -y install "
                "python3.12 python3.12-pip python3.12-tkinter git"
            ),
            2,
        )
        self.assertIn(
            "bootstrap: dnf -y upgrade --refresh && dnf -y install --allowerasing --nobest "
            "python3.12 python3.12-pip python3.12-tkinter git",
            enterprise_linux,
        )
        self.assertIn("git config --global --add safe.directory /workspace", enterprise_linux)
        self.assertIn('if [ -z "${DISPLAY:-}" ]; then', enterprise_linux)
        self.assertIn("DISPLAY is required for desktop validation but is not configured.", enterprise_linux)
        self.assertIn('"$PYTHON_BIN" app.py --gui-smoke-test', enterprise_linux)
        self.assertIn('"$PYTHON_BIN" app.py --smoke-test', enterprise_linux)
        self.assertIn('"$PYTHON_BIN" verify.py', enterprise_linux)
        self.assertIn("ABI-only container", enterprise_linux)
        self.assertEqual(
            [],
            workflow_action_pin_issues(
                text,
                {
                    "actions/checkout": (7, "3d3c42e5aac5ba805825da76410c181273ba90b1"),
                    "actions/setup-python": (7, "5fda3b95a4ea91299a34e894583c3862153e4b97"),
                    "actions/setup-node": (7, "820762786026740c76f36085b0efc47a31fe5020"),
                    "actions/upload-artifact": (7, "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"),
                    "actions/download-artifact": (8, "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"),
                },
            ),
        )

        package = text.split("  package:\n", 1)[1]
        self.assertIn("- tkinter-gui-lifecycle", package)
        self.assertIn("- windows-10-self-hosted", package)
        self.assertIn("TKINTER_GUI_RESULT: ${{ needs.tkinter-gui-lifecycle.result }}", package)
        self.assertIn('"tkinter-gui-lifecycle:${TKINTER_GUI_RESULT}"', package)
        self.assertIn("WINDOWS_10_RESULT: ${{ needs.windows-10-self-hosted.result }}", package)
        self.assertIn("WINDOWS_10_ENABLED: ${{ vars.ENABLE_WINDOWS_10_SELF_HOSTED }}", package)
        self.assertIn('if [ "${WINDOWS_10_ENABLED}" = "true" ]', package)
        self.assertIn('"Required opt-in CI job windows-10-self-hosted finished with ${WINDOWS_10_RESULT}."', package)

    def test_release_workflow_publishes_checked_and_checksummed_assets(self) -> None:
        text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

        for fragment in (
            '"v*.*.*"',
            "workflow_dispatch:",
            "environment: release",
            "contents: write",
            "python app.py --smoke-test",
            "xvfb-run --auto-servernum python app.py --gui-smoke-test",
            "PREDICTION_MARKET_CONFIG_PATH",
            "python verify.py",
            "PIP_NO_CACHE_DIR",
            "python -m pip install --no-cache-dir --require-hashes -r requirements-bootstrap.lock",
            "python -m pip install --no-cache-dir --require-hashes -r requirements-test.lock",
            "python -m pip install --no-cache-dir --no-deps -e .",
            "python -m build",
            "python -m build --no-isolation",
            "Validate package version matches release tag",
            "Require release tag to resolve to workflow commit on protected main",
            "GITHUB_TOKEN: ${{ github.token }}",
            "http.https://github.com/.extraheader",
            "scripts/verify_release_provenance.py",
            '--tag "${RELEASE_TAG}"',
            '--commit "${GITHUB_SHA}"',
            '--main-ref "origin/main"',
            "Python compatibility",
            '"3.x"',
            "npm run build",
            "npm ci --ignore-scripts",
            "npm install --ignore-scripts --no-audit --no-fund",
            "Audit frontend dependencies used for packaging",
            "npm audit --audit-level=high",
            "Build Windows EXE and MSI",
            "macos-14",
            "macos-15",
            "macos-26",
            "windows-2025-vs2026",
            "requirements-build.lock",
            "requirements-bootstrap.lock",
            "requirements-security.lock",
            "requirements.lock",
            "requirements-test.lock",
            "python -m pip install --no-cache-dir --require-hashes -r requirements-build.lock",
            "Audit locked Python dependencies used for packaging",
            "pip_audit --requirement requirements.lock --progress-spinner off",
            "pip_audit --requirement requirements-live.lock --progress-spinner off",
            "pip_audit --requirement requirements-test.lock --progress-spinner off",
            "pip_audit --requirement requirements-build.lock --progress-spinner off",
            "pip_audit --requirement requirements-bootstrap.lock --progress-spinner off",
            "pip_audit --requirement requirements-security.lock --progress-spinner off",
            "pyproject.toml",
            "dotnet tool install --global wix --version 6.0.2",
            'Expected WiX Toolset 6.0.2',
            "scripts/build_windows_release.py",
            "windows-dist",
            "Windows x64 MSI installer",
            'node-version: "24"',
            "sha256sum * > SHA256SUMS.txt",
            "Generate SPDX SBOM",
            "scripts/generate_release_sbom.py",
            "Verify final release assets",
            "scripts/verify_release_assets.py",
            "actions/attest-build-provenance@4d101475d8b20a2381f78447822ac1eab6504dd8 # v4.2.2",
            "attestations: write",
            "id-token: write",
            "Verify protected Windows signing configuration",
            "REQUIRE_WINDOWS_CODE_SIGNING",
            "WINDOWS_CODE_SIGNING_CERTIFICATE_BASE64",
            "WINDOWS_CODE_SIGNING_CERTIFICATE_PASSWORD",
            "X509Certificate2",
            "EphemeralKeySet",
            "certificate base64 contains internal whitespace",
            "scripts/sign_windows_release.py",
            "gh release create",
            "gh release upload",
            "--target \"${GITHUB_SHA}\"",
            "Smoke install built wheel",
            "--force-reinstall --no-deps",
            "License-Expression",
            "fetch-depth: 0",
            "scripts/verify_python_dist_artifacts.py",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, text)
        self.assertNotIn("python -m pip install --no-cache-dir build", text)
        self.assertNotIn("cache: pip", text)
        self.assertNotIn("cache-dependency-path", text)
        self.assertNotIn("macos-latest", text)
        self.assertNotIn("windows-latest", text)
        self.assertLess(
            text.index("Verify protected Windows signing configuration"),
            text.index("Download frontend bundle"),
        )
        self.assertEqual(
            [],
            workflow_action_pin_issues(
                text,
                {
                    "actions/checkout": (7, "3d3c42e5aac5ba805825da76410c181273ba90b1"),
                    "actions/setup-python": (7, "5fda3b95a4ea91299a34e894583c3862153e4b97"),
                    "actions/setup-node": (7, "820762786026740c76f36085b0efc47a31fe5020"),
                    "actions/upload-artifact": (7, "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"),
                    "actions/download-artifact": (8, "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"),
                    "actions/attest-build-provenance": (4, "4d101475d8b20a2381f78447822ac1eab6504dd8"),
                },
            ),
        )

    def test_distribution_smoke_uses_the_current_catalog_count(self) -> None:
        for workflow_name in ("ci.yml", "release.yml"):
            with self.subTest(workflow=workflow_name):
                text = (ROOT / ".github" / "workflows" / workflow_name).read_text(encoding="utf-8")
                self.assertIn("from market_adapters import MARKET_IDS; print(len(MARKET_IDS))", text)
                self.assertIn("EXPECTED_MARKET_COUNT", text)
                self.assertNotIn("len(build_default_registry().list_market_ids()) == 41", text)

    def test_windows_packaging_lock_is_hash_protected(self) -> None:
        source = (ROOT / "requirements-build.txt").read_text(encoding="utf-8")
        text = (ROOT / "requirements-build.lock").read_text(encoding="utf-8")
        requirements = [line.strip() for line in source.splitlines() if line.strip() and not line.startswith("#")]
        self.assertGreaterEqual(len(requirements), 2)
        for requirement in requirements:
            self.assertIn(requirement, text)
        self.assertIn("--hash=sha256:", text)

    def test_security_and_dependabot_automation_are_configured(self) -> None:
        security = (ROOT / ".github" / "workflows" / "security.yml").read_text(encoding="utf-8")
        dependabot = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")

        for fragment in (
            "actions/dependency-review-action",
            "security-events: write",
            "fail-on-severity: high",
            "Frontend dependency audit",
            "npm ci --ignore-scripts",
            "npm audit --omit=dev --audit-level=high",
            "Audit all locked Python dependency graphs",
            "name: Python dependency audit",
            "requirements-bootstrap.lock",
            "requirements-security.lock",
            "pip_audit --requirement requirements.lock --progress-spinner off",
            "pip_audit --requirement requirements-live.lock --progress-spinner off",
            "pip_audit --requirement requirements-test.lock --progress-spinner off",
            "pip_audit --requirement requirements-build.lock --progress-spinner off",
            "pip_audit --requirement requirements-bootstrap.lock --progress-spinner off",
            "pip_audit --requirement requirements-security.lock --progress-spinner off",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, security)

        self.assertEqual(
            [],
            workflow_action_pin_issues(
                security,
                {
                    "actions/checkout": (7, "3d3c42e5aac5ba805825da76410c181273ba90b1"),
                    "actions/setup-python": (7, "5fda3b95a4ea91299a34e894583c3862153e4b97"),
                    "actions/setup-node": (7, "820762786026740c76f36085b0efc47a31fe5020"),
                    "actions/dependency-review-action": (5, "a1d282b36b6f3519aa1f3fc636f609c47dddb294"),
                    "github/codeql-action/init": (4, "db488ddef3bf6cb639b32c2e9a7c0a7ea8271d28"),
                    "github/codeql-action/analyze": (4, "db488ddef3bf6cb639b32c2e9a7c0a7ea8271d28"),
                },
            ),
        )

        for fragment in (
            "package-ecosystem: github-actions",
            "package-ecosystem: pip",
            "package-ecosystem: npm",
            "directory: /frontend",
            "timezone: Europe/Istanbul",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, dependabot)
        self.assertNotIn("labels:", dependabot)

    def test_repository_settings_policy_has_a_read_only_evidence_command(self) -> None:
        text = (ROOT / "docs" / "REPOSITORY_SETTINGS.md").read_text(encoding="utf-8")
        for fragment in (
            "scripts/verify_repository_settings.py",
            "Administration: read",
            "Actions: read",
            "REQUIRE_WINDOWS_CODE_SIGNING=true",
            "nonzero exit status",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, text)

    def test_action_policy_accepts_reviewed_pins_and_rejects_drift(self) -> None:
        expected = {
            "actions/checkout": (7, "3d3c42e5aac5ba805825da76410c181273ba90b1"),
            "actions/setup-node": (7, "820762786026740c76f36085b0efc47a31fe5020"),
        }
        reviewed = """
        - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7
        - uses: actions/setup-node@820762786026740c76f36085b0efc47a31fe5020 # v7
        """
        drifted = """
        - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7
        - uses: actions/setup-node@820762786026740c76f36085b0efc47a31fe5020 # v6
        """

        self.assertEqual([], workflow_action_pin_issues(reviewed, expected))
        self.assertEqual(
            ["actions/setup-node requires # v7; found # v6"],
            workflow_action_pin_issues(drifted, expected),
        )

    def test_actionlint_knows_the_intentional_windows_10_runner_label(self) -> None:
        text = (ROOT / ".github" / "actionlint.yaml").read_text(encoding="utf-8")

        self.assertIn("self-hosted-runner:", text)
        self.assertIn("windows-10", text)

    def test_ci_cd_docs_describe_release_operations(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        docs = (ROOT / "docs" / "CI_CD.md").read_text(encoding="utf-8")

        for fragment in (
            "## CI/CD and Releases",
            "ci.yml",
            "security.yml",
            "release.yml",
            "docs/CI_CD.md",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, readme)

        for fragment in (
            "Release Process",
            "python verify.py --frontend-build",
            "Node.js `24`",
            "dependency graph",
            "pyproject.toml",
            "Windows Release Packages",
            "WiX Toolset `6.0.2`",
            "Windows x64 MSI installer",
            "docs/PLATFORM_SUPPORT.md",
            "git tag v0.1.0",
            "SHA256SUMS.txt",
            "release environment",
            "branch protection",
            "Windows code-signing credentials are required",
            "docs/PRODUCTION_OPERATIONS.md",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, docs)


if __name__ == "__main__":
    unittest.main()
