# CI/CD and Release Operations

This project uses GitHub Actions for pull-request validation, release artifact generation, security scanning, and dependency update automation.

## Workflows

### CI

Workflow: `.github/workflows/ci.yml`

Runs on pushes, pull requests, and manual dispatch.

Jobs:

- Python verification on Ubuntu, macOS `14`, macOS `15`, macOS `26`, and hosted Windows across Python `3.10`, `3.11`, `3.12`, `3.13`, and `3.14`.
- Forward Python compatibility checks through the moving latest stable `3.x` runner; this avoids prerelease runner failures while still following future stable Python releases above 3.16 as GitHub Actions publishes them.
- Enterprise Linux verification through RHEL UBI 8/9/10, a RHEL 7-era manylinux2014 ABI container, and Rocky Linux 8/9/10 containers. The UBI and Rocky containers provision Tkinter, while the Ubuntu runner provides Xvfb and mounts its display socket into desktop-enabled containers. Those lanes then run clean dependency installation, the Enterprise Linux checks, the full Tkinter widget lifecycle smoke inside the container, the Tkinter fallback smoke command, and the complete Python verifier. The manylinux2014 lane remains an explicitly non-desktop ABI compatibility check; container GUI evidence does not by itself certify a native desktop installation on that distribution.
- Windows 11 ARM hosted compatibility checks with Python `3.12` x64, matching the currently available wheel support for the project's transitive dependencies.
- An opt-in Windows 10 self-hosted job, enabled only when repository variable `ENABLE_WINDOWS_10_SELF_HOSTED=true` and a self-hosted runner labelled `windows-10` are available. `.github/actionlint.yaml` declares that intentional custom label so workflow linting remains strict for all other runner names.
- Mobile web smoke checks for Android 14/15/16 and iOS 15/16/18/26 user-agent and viewport profiles against the built React UI.
- Tkinter fallback metadata smoke with `python app.py --smoke-test`, plus a real
  Tkinter widget-tree lifecycle smoke under Ubuntu Xvfb with
  `python app.py --gui-smoke-test`. The lifecycle smoke disables network
  background workers and verifies that normal close cancels queued UI work and
  stops worker objects before destroying the Tk interpreter.
- Full project verification with `python verify.py`.
- A pinned Ruff static-analysis gate (`F` correctness, `B` bugbear, and `S608`
  dynamic-SQL rules), run by `python verify.py` before the functional test suite.
- Enforced branch-coverage floors of 65% for the full Python application and
  74% for the headless/backend surface. The verifier measures both and fails on
  regression.
- React production build with Node.js `24`.
- Python wheel and source distribution build, explicit artifact-content verification, and an installed-wheel CLI, metadata, registry, and adapter import smoke from outside the source tree. `MANIFEST.in` keeps the source archive's fixtures, config, docs, frontend source, scripts, workflows, and visual assets while excluding generated frontend/build directories.
- Short-retention artifacts for the frontend bundle and Python distributions.
- Every third-party action is pinned to a reviewed 40-character commit SHA,
  with its tracked major version retained as a comment for reviewability.
- Runtime dependencies install from hash-protected `requirements.lock`; authenticated
  CLOB deployments add `requirements-live.lock`; CI test jobs use the
  hash-protected `requirements-test.lock`; editable
  installation uses `--no-deps` so CI cannot silently resolve newer packages.

The workflow uses read-only repository permissions by default and cancels stale runs on the same ref.

Manual dispatch of the protected `main` branch also runs
`Public Polymarket live / GitHub-hosted`; feature-branch dispatches skip this
trust-sensitive job. It rejects credential-bearing environment variables, exercises only the four
reviewed public-read endpoints, validates the resulting report offline, checks
that the checkout stayed clean at the exact requested revision, and creates a
GitHub build-provenance attestation for the report before uploading it as a
short-retention artifact. The readiness scorer accepts the downloaded report
only after independently verifying its bytes, attestation identity, workflow
run, hosted Ubuntu job, successful required steps, source revision, and
freshness, and requires both workflow and source ref to be `refs/heads/main`.
This evidence establishes public endpoint reachability from that
runner at that time; it is not deployment, credentialed-account, or funded-order
evidence.

### Security

Workflow: `.github/workflows/security.yml`

Runs on pushes, pull requests, weekly schedule, and manual dispatch.

Jobs:

- Dependency review on pull requests; GitHub Dependency Graph must be enabled
  in the repository settings and high-severity dependency changes fail the job.
- A reproducible `npm ci --ignore-scripts` followed by `npm audit --omit=dev
  --audit-level=high` on every security workflow run. This fails closed for
  high-severity vulnerabilities in the production frontend dependency tree.
- Hash-locked `pip-audit` checks against `requirements.lock` and
  `requirements-live.lock` on every security workflow run. These fail closed
  when either supported Python runtime dependency graph has a known
  vulnerability.
- CodeQL analysis for Python and JavaScript/TypeScript.

The CodeQL job is the only job with `security-events: write`; all other jobs use least-privilege read permissions unless they need more. Dependency review runs with the pull-request permissions required by GitHub's action and fails on high-severity dependency changes.

### Release

Workflow: `.github/workflows/release.yml`

Runs on tags matching `v*.*.*` and manual dispatch.

Release jobs:

- Validate release tag shape.
- Validate that `pyproject.toml` project version matches the release tag.
- Verify Python, Tkinter fallback, and project checks across the supported release range, including macOS `14`, macOS `15`, macOS `26`, and forward compatibility through future stable `3.x` releases when those interpreters are available.
- Build Python wheel/source distribution and smoke-install the exact wheel before upload.
- Build React production assets.
- Package `frontend/dist` as a zip file.
- Build a Windows x64 PyInstaller executable package.
- Package the Windows executable as a portable zip and MSI installer.
- Generate `SHA256SUMS.txt` and an SPDX 2.3 software bill of materials.
- Create GitHub build-provenance attestations for every release asset.
- Publish or update a GitHub Release using the built-in `GITHUB_TOKEN`.
- After a stable release is remotely inventoried, downloaded, and byte-compared,
  generate, attest, and upload a distinct canonical `release-evidence.json`
  artifact bound to the exact repository, commit, tag, run, attempt, release
  history, and asset names/sizes/SHA-256 values.

The publish job targets the `release` environment. Treat this as the release environment for production publishing, and configure protection rules for it in GitHub if releases should require manual approval.

Release reruns are fail-closed. An updatable existing release is returned to draft state before its assets change; a new release starts as a draft. After the verified local files are uploaded, the workflow enumerates every remote asset page, removes only numeric asset IDs belonging to that exact release whose names are absent locally, checks the exact remote names and metadata, downloads every asset, and compares the bytes with the attested local files. The requested draft or published state is applied only after those checks pass. If immutable releases are enabled, GitHub rejects modification of a published release instead of allowing a partial rerun.

For a non-draft, non-prerelease publication, the pinned `ubuntu-24.04`
publish job then queries the final release, asset inventory, complete bounded
release history, and its own run identity. `scripts/generate_release_evidence.py`
fails unless they match the exact verified local bytes and trusted workflow
coordinates. Only then does the workflow attest and upload
`release-evidence.json` as
`release-evidence-<sha>-<run-id>-<attempt>`. This evidence file is not itself a
release asset, avoiding a checksum/inventory cycle. See
`docs/PRODUCTION_READINESS.md` for the independent live-state checks performed
before the readiness scorer awards either release point.

If generation, attestation, or upload of that post-publication evidence fails,
the workflow runs a compensating cleanup that returns the prepared release to
draft state and verifies the transition. If GitHub refuses that transition,
the cleanup also fails loudly and the release requires immediate operator
reconciliation. The report records a fresh `generated_at` collection time
independently of the release's original `published_at`, so an unchanged current
release can be re-evaluated without pretending it was newly published.

See `docs/PLATFORM_SUPPORT.md` for the platform support tiers and the gates required before any additional OS or mobile platform is advertised as fully supported.

The normal verifier runs `python scripts/verify_platform_support.py` to ensure platform claims remain documented and honest. `python scripts/verify_platform_support.py --require-full` is the strict 100% platform certification gate; it must fail until every requested desktop, Unix, BSD/Solaris, Android, and iOS target has real repeatable test evidence.

## Release Process

1. Make sure local verification passes:

   ```bash
   python app.py --smoke-test
   python app.py --gui-smoke-test # Requires a local display server.
   python -m pytest -q
   python verify.py
   python scripts/verify_dependency_lock.py
   ```

2. Make sure `[project].version` in `pyproject.toml` matches the release tag you plan to publish. `python verify.py` rejects reusing a tag that points to an older commit and requires an untagged version to be newer than the latest repository release tag. CI/release checkouts use full history (`fetch-depth: 0`), and local shallow clones must fetch complete history and tags before verification.

3. Build frontend dependencies in an environment where npm can complete:

   ```bash
   cd frontend
   npm install
   npm run build
   cd ..
   python verify.py --frontend-build
   ```

4. Create and push a semver tag:

   ```bash
   git tag v0.1.0
   git push origin v0.1.0
   ```

5. Watch the Release workflow. A successful run publishes:

   - Python wheel
   - Python source distribution
   - React production frontend zip
   - Windows x64 portable zip with the bundled `.exe`, React assets, launchers, and example config
   - Windows x64 MSI installer
   - SPDX JSON SBOM
   - SHA256 checksums
   - GitHub build-provenance attestations

Manual releases can also be started from the GitHub Actions UI with
`workflow_dispatch`, but select the existing release tag as the workflow ref.
The workflow rejects a supplied tag that is missing, resolves to a different
commit, or is not reachable from protected `main`.

## Dependency Automation

Config: `.github/dependabot.yml`

Dependabot opens grouped weekly pull requests for:

- GitHub Actions versions
- Python requirements
- Frontend npm dependencies

The bootstrap installer plus runtime, live SDK, test, build, and security-audit
locks are regenerated with `pip-compile --allow-unsafe --generate-hashes` only
during an intentional dependency update. Every Python workflow first installs
the hash-locked `requirements-bootstrap.lock`; CI test jobs then install
`requirements-test.lock`, while package build jobs install
`requirements.lock` plus `requirements-build.lock`. The security workflow and
the release package gate install `requirements-security.lock` and audit every
bootstrap, runtime, live, test, build, and security lock before publishing.
This keeps all Python dependency graphs independently reviewable and hash
protected.
The release frontend build installs with lifecycle scripts disabled and runs
`npm audit --audit-level=high` over its full build dependency tree before
creating the published bundle.
The Windows packaging-only PyInstaller dependency graph is likewise installed
from hash-protected `requirements-build.lock`.

## Windows Release Packages

Windows artifacts are produced by `scripts/build_windows_release.py` on the `windows-2025-vs2026` GitHub Actions runner. The release workflow pins WiX Toolset `6.0.2` for MSI packaging so the build is deterministic and does not silently accept newer WiX EULA prompts in CI.

The portable zip contains:

- `market-sentinel.exe`
- `start_tkinter_gui.bat`
- `start_web_gui.bat`
- bundled app icons in `assets\`
- bundled `frontend/dist` React assets
- `README.md`, `README_WINDOWS.txt`, `LICENSE`, `.env.example`, and `data/config.example.json`

The MSI installs the same payload under Program Files, creates Start Menu shortcuts for the Tkinter and React launchers, and supports normal Windows uninstall/upgrade behavior through MSI product metadata. Set `REQUIRE_WINDOWS_CODE_SIGNING=true` for production releases; the `release` environment must then provide `WINDOWS_CODE_SIGNING_CERTIFICATE_BASE64` and `WINDOWS_CODE_SIGNING_CERTIFICATE_PASSWORD`. Before downloading build inputs or running WiX/PyInstaller, the release job verifies that the secret is a password-protected PFX with a private key and that the timestamp endpoint is HTTPS. `scripts/sign_windows_release.py` signs and verifies every EXE/MSI using an RFC 3161 timestamp URL; certificates are decoded only into a temporary file on the Windows runner. If `REQUIRE_WINDOWS_CODE_SIGNING` is explicitly false or absent, the workflow still builds and verifies the portable ZIP/MSI contents, but publishes them as intentionally unsigned testing/development artifacts and labels that status in the release notes. Unsigned Windows artifacts are not production-trusted.

The Windows launchers use `data/config.json` when the package folder is writable, which keeps the portable zip self-contained. If the app is installed under a protected folder such as Program Files, the launchers set `PREDICTION_MARKET_CONFIG_PATH` to `%APPDATA%\market-sentinel\data\config.json` so normal users can save settings without administrator privileges.

## Required Repository Settings

Recommended GitHub settings:

- Require the `Python package build`, `Python dependency audit`, `CodeQL`, `Dependency review`, and `Frontend dependency audit` checks before merging. The package gate aggregates the Python/OS matrix, React build, mobile-web smoke, enterprise Linux checks, Windows 11, and real Tkinter GUI lifecycle job. When the `ENABLE_WINDOWS_10_SELF_HOSTED=true` repository variable is enabled, it also requires the Windows 10 self-hosted job to succeed; when the variable is absent, that optional job is intentionally skipped and does not block the aggregate gate.
- Enable GitHub dependency graph; the dependency review job fails closed without it.
- Keep GitHub Actions workflow permissions as read-only by default.
- Create a protected `release` environment if production releases should require approval.
- Enable Dependabot alerts, secret scanning, push protection, and private vulnerability reporting.
- Use branch protection on `main` or `master`.

The release workflow uses the built-in `GITHUB_TOKEN` with `contents: write`,
`attestations: write`, and `id-token: write` only on the protected publish job.
Windows code-signing credentials are required before distributing a production
installer publicly. Set `REQUIRE_WINDOWS_CODE_SIGNING=true` and the protected
code-signing secrets in the `release` environment. For an explicitly unsigned
testing/development release, set the variable to `false`; the workflow labels
the resulting Windows artifacts as unsigned. See `docs/REPOSITORY_SETTINGS.md` and
`docs/PRODUCTION_OPERATIONS.md` for the mandatory repository and deployment controls.
