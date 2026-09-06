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
                    "actions/attest-build-provenance": (4, "4d101475d8b20a2381f78447822ac1eab6504dd8"),
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

    def test_manual_ci_public_probe_is_read_only_exact_sha_and_attested(self) -> None:
        text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        job = text.split("  public-polymarket-live:\n", 1)[1].split("  package:\n", 1)[0]

        for fragment in (
            "name: Public Polymarket live / GitHub-hosted",
            "if: github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'",
            "runs-on: ubuntu-24.04",
            "contents: read",
            "attestations: write",
            "id-token: write",
            "persist-credentials: false",
            "python -m pip install --no-cache-dir --require-hashes -r requirements-bootstrap.lock",
            "python -m pip install --no-cache-dir --require-hashes -r requirements.lock",
            "Verify exact clean source before probe",
            "Reverify exact clean source after probe",
            'test "$(git rev-parse HEAD)" = "${GITHUB_SHA}"',
            "git status --porcelain=v1 --untracked-files=all",
            'report_dir="${RUNNER_TEMP}/public-live"',
            "for probe_attempt in 1 2",
            "sleep 1",
            'if [ "${probe_succeeded}" != true ]',
            "--public-only",
            "--validate-public-only-report",
            '--source-repository "${GITHUB_REPOSITORY}"',
            '--source-revision "${GITHUB_SHA}"',
            '--source-run-id "${GITHUB_RUN_ID}"',
            '--source-run-attempt "${GITHUB_RUN_ATTEMPT}"',
            '--source-workflow-ref "${GITHUB_WORKFLOW_REF}"',
            '"error":"probe terminated before a report was written"',
            "actions/attest-build-provenance@4d101475d8b20a2381f78447822ac1eab6504dd8 # v4.2.2",
            "subject-path: ${{ runner.temp }}/public-live/public-polymarket-live.json",
            "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7",
            "name: public-polymarket-live-${{ github.sha }}-${{ github.run_id }}-${{ github.run_attempt }}",
            "if-no-files-found: error",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, job)

        for forbidden in (
            "env:",
            "secrets.",
            "requirements-live.lock",
            "python -m pip install --no-cache-dir --no-deps -e .",
            "--skip-authenticated-read-checks",
            "--require-authenticated-read-ok",
            "--include-user-websocket-connect",
            "--include-bridge-address-creation",
            "--allow-funded-order",
            "--token-id",
            "--private-key",
            "${{ github.workflow_ref }}",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, job)

        probe_index = job.index("Probe reviewed public Polymarket endpoints")
        validate_index = job.index("Revalidate public-only evidence before attestation")
        attest_index = job.index("Attest exact public-live evidence file")
        upload_index = job.index("Upload public-live evidence")
        self.assertLess(probe_index, validate_index)
        self.assertLess(validate_index, attest_index)
        self.assertLess(attest_index, upload_index)

    def test_release_workflow_publishes_checked_and_checksummed_assets(self) -> None:
        text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

        for fragment in (
            '"v*.*.*"',
            "workflow_dispatch:",
            "release-unsigned",
            "vars.REQUIRE_WINDOWS_CODE_SIGNING == 'true' && 'release' || 'release-unsigned'",
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
            "Reconcile and publish GitHub release",
            "--print-stale-remote-asset-ids",
            "--verify-remote-inventory",
            "--remote-release-json",
            "--remote-assets-json",
            "gh api --paginate --slurp",
            "release_index_json",
            "Draft release assets cannot be downloaded",
            "application/octet-stream",
            "cmp --",
            "actions/attest-build-provenance@4d101475d8b20a2381f78447822ac1eab6504dd8 # v4.2.2",
            "attestations: write",
            "id-token: write",
            "Verify Windows signing configuration",
            "REQUIRE_WINDOWS_CODE_SIGNING",
            "WINDOWS_SIGNING_REQUIRED",
            "WINDOWS_CODE_SIGNING_CERTIFICATE_BASE64",
            "WINDOWS_CODE_SIGNING_CERTIFICATE_PASSWORD",
            "X509Certificate2",
            "EphemeralKeySet",
            "certificate base64 contains internal whitespace",
            "scripts/sign_windows_release.py",
            "gh release create",
            "uploads.github.com/repos/${GITHUB_REPOSITORY}/releases/${release_id}/assets?name=${asset_name}",
            "--method POST",
            '--input "${asset_path}"',
            "--target \"${GITHUB_SHA}\"",
            "Smoke install built wheel",
            "--force-reinstall --no-deps",
            "License-Expression",
            "fetch-depth: 0",
            "scripts/verify_python_dist_artifacts.py",
            "Prepare Windows application payload",
            "Smoke test staged Windows executable",
            "Sign staged Windows executable",
            "Package Windows portable zip and MSI",
            "Sign Windows MSI package",
            "Verify signatures in final Windows artifacts",
            "Verify unsigned Windows artifacts",
            "--prepare-only",
            "--package-only",
            "requirements-live.lock",
            "release-assets/RELEASE_NOTES.md",
            "--notes-file release-assets/RELEASE_NOTES.md",
            "scripts/release_version.py normalize-tag",
            "scripts/release_version.py is-prerelease",
            "scripts/release_version.py validate-project",
            '"--prerelease=${PRERELEASE}"',
            '"--draft=${DRAFT}"',
            "runs-on: ubuntu-24.04",
            "Generate exact published release evidence",
            "scripts/generate_release_evidence.py",
            "Attest exact published release evidence",
            "subject-path: release-evidence/release-evidence.json",
            "Upload published release evidence",
            "name: release-evidence-${{ github.sha }}-${{ github.run_id }}-${{ github.run_attempt }}",
            "id: publish_release",
            'echo "release_prepared=true" >> "${GITHUB_OUTPUT}"',
            "Re-draft release after evidence failure",
            "failure() || cancelled()",
            'RELEASE_ID: ${{ steps.publish_release.outputs.release_id }}',
            'RELEASE_FINGERPRINT: ${{ steps.publish_release.outputs.release_fingerprint }}',
            'current_fingerprint="$(' ,
            "Release changed after publication; preserving the newer state instead of re-drafting it.",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, text)

        # gh api rejects combining --slurp with --jq. Keep pagination and
        # flattening as separate pipeline stages so the release job can list
        # multi-page release/asset inventories successfully.
        slurp_lines = [
            index
            for index, line in enumerate(text.splitlines())
            if "gh api --paginate --slurp" in line
        ]
        self.assertEqual(len(slurp_lines), 7)
        lines = text.splitlines()
        for index in slurp_lines:
            with self.subTest(slurp_line=index):
                command = "\n".join(lines[index : index + 3])
                self.assertIn("| jq 'flatten'", command)
                self.assertNotIn("--jq", command)
        self.assertNotIn("python -m pip install --no-cache-dir build", text)
        self.assertNotIn("cache: pip", text)
        self.assertNotIn("cache-dependency-path", text)
        self.assertNotIn("macos-latest", text)
        self.assertNotIn("windows-latest", text)
        self.assertNotIn('version=${tag_name#v}', text)
        self.assertNotIn('tag_name="${{ inputs.tag_name }}"', text)
        self.assertLess(
            text.index("Verify Windows signing configuration"),
            text.index("Download frontend bundle"),
        )
        windows_app = text.split("  windows-app:\n", 1)[1].split("  publish:\n", 1)[0]
        self.assertIn("WINDOWS_SIGNING_REQUIRED: ${{ vars.REQUIRE_WINDOWS_CODE_SIGNING == 'true' }}", windows_app)
        self.assertIn(
            "python -m pip install --no-cache-dir --require-hashes -r requirements-live.lock",
            windows_app,
        )
        self.assertNotIn(
            "python -m pip install --no-cache-dir --require-hashes -r requirements.lock",
            windows_app,
        )
        prepare_index = windows_app.index("Prepare Windows application payload")
        smoke_index = windows_app.index("Smoke test staged Windows executable")
        sign_exe_index = windows_app.index("Sign staged Windows executable")
        package_index = windows_app.index("Package Windows portable zip and MSI")
        sign_msi_index = windows_app.index("Sign Windows MSI package")
        verify_signatures_index = windows_app.index("Verify signatures in final Windows artifacts")
        verify_unsigned_index = windows_app.index("Verify unsigned Windows artifacts")
        upload_index = windows_app.index("Upload Windows release packages")
        self.assertLess(prepare_index, smoke_index)
        self.assertLess(smoke_index, sign_exe_index)
        self.assertLess(prepare_index, sign_exe_index)
        self.assertLess(sign_exe_index, package_index)
        self.assertLess(package_index, sign_msi_index)
        self.assertLess(sign_msi_index, verify_signatures_index)
        self.assertLess(verify_signatures_index, verify_unsigned_index)
        self.assertLess(verify_signatures_index, upload_index)
        self.assertLess(verify_unsigned_index, upload_index)
        self.assertIn("if: ${{ env.WINDOWS_SIGNING_REQUIRED == 'true' }}", windows_app)
        self.assertIn("if: ${{ env.WINDOWS_SIGNING_REQUIRED != 'true' }}", windows_app)
        self.assertIn("build/windows-release/market-sentinel-${{ needs.metadata.outputs.tag_name }}-win-x64/market-sentinel.exe", windows_app)
        self.assertIn("& $executable --smoke-test", windows_app)
        self.assertIn("release-assets/market-sentinel-${{ needs.metadata.outputs.tag_name }}-win-x64.msi", windows_app)
        self.assertIn('Get-ChildItem -LiteralPath $extractDirectory -Recurse -File -Filter "market-sentinel.exe"', windows_app)
        self.assertIn("verify /pa /all $embeddedExecutables[0].FullName", windows_app)
        self.assertIn("verify /pa /all $installer", windows_app)
        self.assertNotIn("Get-ChildItem release-assets -File", windows_app)
        checksum_index = text.index("sha256sum * > SHA256SUMS.txt")
        notes_index = text.index("cat > release-assets/RELEASE_NOTES.md")
        self.assertLess(checksum_index, notes_index)
        metadata = text.split("  metadata:\n", 1)[1].split("  python-compatibility:\n", 1)[0]
        self.assertIn("version=\"$(python scripts/release_version.py normalize-tag", metadata)
        self.assertIn("prerelease=\"$(python scripts/release_version.py is-prerelease", metadata)
        self.assertIn('if [ "${{ github.event_name }}" = "workflow_dispatch" ]', metadata)
        self.assertIn('[ "${requested_prerelease}" != "${prerelease}" ]', metadata)
        publish = text.split("  publish:\n", 1)[1]
        self.assertEqual(publish.count('"${release_state_flags[@]}"'), 1)
        self.assertNotIn("release_flags+=(--prerelease)", publish)
        self.assertNotIn("release_flags+=(--draft)", publish)
        self.assertNotIn("gh release delete", publish)
        self.assertNotIn('gh release edit "${TAG_NAME}" --draft=true', publish)
        self.assertIn('"repos/${GITHUB_REPOSITORY}/releases/${RELEASE_ID}"', publish)
        self.assertIn('-F draft=true > "${redrafted_release_json}"', publish)
        self.assertIn('[ "${current_fingerprint}" != "${RELEASE_FINGERPRINT}" ]', publish)
        self.assertIn('[[ ! "${asset_id}" =~ ^[1-9][0-9]*$ ]]', publish)
        self.assertIn('"repos/${GITHUB_REPOSITORY}/releases/assets/${asset_id}"', publish)
        self.assertIn('"repos/${GITHUB_REPOSITORY}/releases/${release_id}/assets?per_page=100"', publish)
        preflight_index = publish.index("release_index_json")
        draft_index = publish.index('gh api --method PATCH')
        upload_recheck_index = publish.index(
            "Release identity changed before asset upload; refusing mutation."
        )
        upload_index = publish.index(
            "uploads.github.com/repos/${GITHUB_REPOSITORY}/releases/${release_id}/assets?name=${asset_name}"
        )
        self.assertNotIn('gh release upload "${TAG_NAME}"', publish)
        cleanup_plan_index = publish.index("--print-stale-remote-asset-ids")
        cleanup_index = publish.index(
            '"repos/${GITHUB_REPOSITORY}/releases/assets/${asset_id}"',
            upload_index,
        )
        remote_verify_index = publish.index("--verify-remote-inventory")
        download_index = publish.index("Draft release assets cannot be downloaded")
        byte_compare_index = publish.index("cmp --")
        release_prepared_index = publish.index('echo "release_prepared=true"')
        final_publish_index = publish.index('"${release_state_flags[@]}"')
        evidence_index = publish.index("Generate exact published release evidence")
        evidence_attest_index = publish.index("Attest exact published release evidence")
        evidence_upload_index = publish.index("Upload published release evidence")
        evidence_cleanup_index = publish.index("Re-draft release after evidence failure")
        self.assertLess(preflight_index, draft_index)
        self.assertLess(draft_index, upload_recheck_index)
        self.assertLess(upload_recheck_index, upload_index)
        self.assertLess(upload_index, cleanup_plan_index)
        self.assertLess(cleanup_plan_index, cleanup_index)
        self.assertLess(cleanup_index, remote_verify_index)
        self.assertLess(remote_verify_index, download_index)
        self.assertLess(download_index, byte_compare_index)
        self.assertLess(byte_compare_index, release_prepared_index)
        self.assertLess(release_prepared_index, final_publish_index)
        self.assertLess(final_publish_index, evidence_index)
        self.assertLess(evidence_index, evidence_attest_index)
        self.assertLess(evidence_attest_index, evidence_upload_index)
        self.assertLess(evidence_upload_index, evidence_cleanup_index)
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

    def test_release_preflights_existing_identity_before_any_mutation(self) -> None:
        text = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        publish = text.split("  publish:\n", 1)[1]

        for fragment in (
            'release_index_json="${RUNNER_TEMP}/market-sentinel-release-index.json"',
            '"repos/${GITHUB_REPOSITORY}/releases?per_page=100"',
            'release_preflight_json="${RUNNER_TEMP}/market-sentinel-release-preflight.json"',
            'prepared_release_id="$(jq -er \'.id\' "${release_preflight_json}")"',
            ".tag_name == $tag",
            ".target_commitish == $target",
            '(.draft | type == "boolean")',
            '(.prerelease | type == "boolean")',
            "Existing published release prerelease state conflicts",
            '"repos/${GITHUB_REPOSITORY}/releases/${prepared_release_id}"',
            "Existing release changed during draft transition",
            "Release identity changed before asset upload",
            'if [ "${release_id}" != "${prepared_release_id}" ]',
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, publish)

        preflight = publish.index("release_index_json")
        validate_target = publish.index(".target_commitish == $target")
        exact_id_patch = publish.index('gh api --method PATCH')
        upload_recheck = publish.index("Release identity changed before asset upload")
        upload = publish.index(
            "uploads.github.com/repos/${GITHUB_REPOSITORY}/releases/${release_id}/assets?name=${asset_name}"
        )
        self.assertNotIn('gh release upload "${TAG_NAME}"', publish)
        self.assertLess(preflight, validate_target)
        self.assertLess(validate_target, exact_id_patch)
        self.assertLess(exact_id_patch, upload_recheck)
        self.assertLess(upload_recheck, upload)
        self.assertNotIn('if gh release view "${TAG_NAME}"', publish)

    def test_distribution_smoke_uses_the_current_catalog_count(self) -> None:
        for workflow_name in ("ci.yml", "release.yml"):
            with self.subTest(workflow=workflow_name):
                text = (ROOT / ".github" / "workflows" / workflow_name).read_text(encoding="utf-8")
                self.assertIn("from market_adapters import MARKET_IDS; print(len(MARKET_IDS))", text)
                self.assertIn("EXPECTED_MARKET_COUNT", text)
                self.assertNotIn("len(build_default_registry().list_market_ids()) == 41", text)

    def test_release_reconcile_preserves_prior_evidence_on_failed_rerun(self) -> None:
        text = (
            ROOT / ".github" / "workflows" / "release-evidence-reconcile.yml"
        ).read_text(encoding="utf-8")

        for fragment in (
            "current_release_has_valid_evidence()",
            "actions/artifacts?per_page=100",
            "^release-evidence-${HEAD_SHA}-([1-9][0-9]*)-([1-9][0-9]*)$",
            ".workflow_run.id, .workflow_run.head_sha",
            "from scripts.check_product_readiness import _attested_release_report",
            'result.get("status") == "pass"',
            "earlier evidence still validates against the current release bytes",
            "This run attempt did not publish the current release; preserving it.",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, text)

        evidence_check = text.index("if current_release_has_valid_evidence; then")
        ownership_check = text.index('if [[ "${owns_published_state}" != "true" ]]')
        draft = text.index("gh api --method PATCH")
        self.assertLess(evidence_check, ownership_check)
        self.assertLess(ownership_check, draft)
        self.assertNotIn(
            'artifact="release-evidence-${HEAD_SHA}-${RUN_ID}-${RUN_ATTEMPT}"',
            text,
        )
        self.assertNotIn('gh release edit "${tag}" --draft=true', text)
        self.assertNotIn(
            'if [[ "${publish_step_conclusion}" == "success" ]]; then\n'
            "            owns_published_state=true",
            text,
        )

        # gh api rejects combining --slurp with --jq. Keep reconciliation
        # pagination and extraction as separate pipeline stages as well.
        slurp_lines = [
            index
            for index, line in enumerate(text.splitlines())
            if "gh api --paginate --slurp" in line
        ]
        self.assertEqual(len(slurp_lines), 2)
        lines = text.splitlines()
        for index in slurp_lines:
            with self.subTest(slurp_line=index):
                command = "\n".join(lines[index : index + 3])
                self.assertIn("| jq", command)
                self.assertNotIn("--jq", command)
        self.assertIn("| jq '[.[].artifacts[]?]'", text)
        self.assertIn("| jq '[.[].jobs[]?]'", text)

    def test_release_reconcile_refuses_stale_run_or_release_identity(self) -> None:
        text = (
            ROOT / ".github" / "workflows" / "release-evidence-reconcile.yml"
        ).read_text(encoding="utf-8")

        for fragment in (
            "group: release-${{ github.event.workflow_run.head_sha }}",
            "RUN_NUMBER: ${{ github.event.workflow_run.run_number }}",
            'if [[ "${current_attempt}" != "${RUN_ATTEMPT}" ]]',
            "actions/runs/${RUN_ID}/attempts/${RUN_ATTEMPT}/jobs?per_page=100",
            'select(.name == "Reconcile and publish GitHub release")',
            "started <= published <= completed and started <= updated <= completed",
            "release timestamps must belong to this exact job window",
            'if [[ "${latest_run_attempt}" != "${RUN_ATTEMPT}" ]]',
            "owned_release_fingerprint",
            "latest_release_fingerprint",
            "Release ${release_id} changed after inspection; preserving the newer state.",
            "Release ${release_id} changed while evidence was rechecked; preserving the newer state.",
            '"repos/${GITHUB_REPOSITORY}/releases/${release_id}"',
            "-F draft=true",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, text)

        first_snapshot = text.index('owned_release_fingerprint="$(jq')
        latest_attempt = text.index('latest_run_attempt="$(gh api')
        first_recheck = text.index(
            'latest_release_fingerprint="$(jq', latest_attempt
        )
        second_evidence_check = text.rindex("if current_release_has_valid_evidence; then")
        second_recheck = text.rindex('latest_release_fingerprint="$(jq')
        draft = text.index("gh api --method PATCH")
        self.assertLess(first_snapshot, latest_attempt)
        self.assertLess(latest_attempt, first_recheck)
        self.assertLess(first_recheck, second_evidence_check)
        self.assertLess(second_evidence_check, second_recheck)
        self.assertLess(second_recheck, draft)

        release_workflow = (
            ROOT / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("group: release-${{ github.sha }}", release_workflow)

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
            "npm audit --audit-level=high",
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
                    "github/codeql-action/init": (4, "cdf488f595d80d6e07e03d4674febd5ab45fa938"),
                    "github/codeql-action/analyze": (4, "cdf488f595d80d6e07e03d4674febd5ab45fa938"),
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
            "Release reruns are fail-closed",
            "numeric asset IDs",
            "release environment",
            "branch protection",
            "Windows code-signing credentials are required",
            "docs/PRODUCTION_OPERATIONS.md",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, docs)


if __name__ == "__main__":
    unittest.main()
