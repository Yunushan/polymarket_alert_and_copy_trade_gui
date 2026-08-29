# Required GitHub Repository Settings

These controls are configured in GitHub, not source control. An administrator
must enable them before treating a release as production-ready.

## Solo-maintainer main branch

Protect `main` with:

1. Required successful `Python package build`, `CodeQL`, `Dependency review`,
   `Frontend dependency audit`, and `Python dependency audit` checks. `Python
   package build` is the aggregate CI gate: it waits for the supported
   Python/OS matrix, enterprise Linux containers, Windows 11, React build,
   mobile-web smoke, and real Tkinter GUI lifecycle jobs.
2. No force pushes, no branch deletion, and no direct administrator bypass for
   normal releases.
3. Required conversation resolution and linear history.

The separate tag-triggered `Release` workflow performs release validation. Its
protected `release` environment must gate publishing, code signing, SBOM,
checksums, and provenance rather than being configured as a branch status check.

## Team production policy

When an independent maintainer is available, additionally require at least one
approving review, Code Owner review, and dismissal of stale approvals on new
commits. Keep `.github/CODEOWNERS` current before enabling that policy.

## Security and automation

1. Enable dependency graph, Dependabot alerts, Dependabot security updates,
   secret scanning, and push protection.
2. Enable private vulnerability reporting.
3. Review and merge or close the active Dependabot pull requests after CI.
4. In `Actions` -> `General`, allow selected actions only, permit GitHub-owned
   actions, and keep SHA pinning required. The checked-in workflows use only
   SHA-pinned `actions/*` and `github/*` actions; do not broaden this policy
   without reviewing the new action's provenance and permissions.
5. Enable non-provider secret-pattern scanning when GitHub makes that control
   available for the repository plan. It complements, but does not replace,
   secret scanning push protection and the source-level secret hygiene gate.

## Release environment

Create the `release` environment with required reviewers, no self-approval,
and deployment-branch rules limited to protected `main` tags. For production
Windows releases, store code-signing credentials there:
`WINDOWS_CODE_SIGNING_CERTIFICATE_BASE64`,
`WINDOWS_CODE_SIGNING_CERTIFICATE_PASSWORD`, and optional
`WINDOWS_CODE_SIGNING_TIMESTAMP_URL`; set
`REQUIRE_WINDOWS_CODE_SIGNING=true`. If those credentials are unavailable,
an explicitly unsigned testing/development release can set
`REQUIRE_WINDOWS_CODE_SIGNING=false`; the workflow labels its Windows assets
unsigned and the release must not be treated as production-trusted. Never add
venue credentials to GitHub Actions.

## Evidence check

Before a release, collect read-only proof that the externally configured controls
match this policy. Set `GITHUB_TOKEN` only in the calling shell to a fine-grained
token with repository `Administration: read` and `Actions: read` permissions;
the command does not print or persist the token.

```bash
python scripts/verify_repository_settings.py \
  --repository Yunushan/market-sentinel \
  --branch main
```

It validates required checks, up-to-date and administrator-enforced branch
protection, pull-request/conversation/linear-history controls, disabled force
pushes and deletions, release-environment reviewers and self-review prevention,
protected deployment branches, required Windows-signing secret names, and
`REQUIRE_WINDOWS_CODE_SIGNING=true`. It reports a nonzero exit status on any
missing control. Run it from an administrator-authorized workstation; a normal
workflow token is intentionally insufficient for this audit.
