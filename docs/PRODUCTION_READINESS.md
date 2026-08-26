# Production Readiness

`python scripts/check_product_readiness.py` is the repository's conservative
readiness score. It reports a score out of 100 and separates repeatable local
proof from evidence that can only be collected on a real host, account, or
GitHub repository.

## Current Check

Run the complete local verification profile:

```bash
python scripts/check_product_readiness.py \
  --full-local \
  --json
```

`--full-local` runs `verify.py --skip-pip-check --frontend-build
--frontend-live-smoke`. This local-only command is the authoritative 83-point
ceiling used for the current audit.

Run the no-credentials public Polymarket probe separately when external network
evidence is in scope:

```bash
python scripts/check_product_readiness.py \
  --full-local \
  --run-public-live \
  --json
```

The public probe never derives credentials, opens the authenticated user
stream, places orders, or performs funded actions.

The readiness scorer retries the complete public-only probe once after a
transient subprocess, transport, or malformed-report failure. Each individual
HTTP request remains bounded by the shared client timeout/retry policy; a
repeated probe failure still fails the score.

Use `--no-run-local` when inspecting the repository shape only. A skipped
check does not receive local test or security points.

## Score Model

| Area | Points | Current verified | Remaining proof required |
| --- | ---: | ---: | --- |
| Architecture and scope | 18 | 18 | None beyond the repository contract |
| Tests and correctness | 18 | 18 | None |
| Security and safety | 17 | 16 | Reviewed repository-settings JSON evidence |
| CI/CD and release | 17 | 14 | Reviewed release-environment, release-history, and release JSON evidence |
| Operations and recovery | 15 | 12 | Reviewed real-host deployment evidence for `v1.0.11` |
| Platform evidence | 10 | 5 | Reviewed platform CI and platform JSON evidence |
| Live acceptance | 5 | 0 | Reachable public endpoints, credentialed read evidence, and approved funded audit |

The latest local audit on 2026-08-26 is **83/100 (not ready)** when no external
evidence manifests are supplied. Local verification covers the adapter catalog
(68 markets, 57 implemented and 11 explicitly blocked), 344 offline fixture
files, documentation, workflows, secret hygiene, frontend build/browser smoke,
and packaging checks. The current verifier passed 1,078 tests (7 intentionally
skipped on Windows), achieved 72% overall and 75% backend branch coverage,
satisfied the enforced coverage floors, and passed Ruff. Counts are reported
from this run rather than carried forward from an older artifact.
Metaculus now supports fixture-backed local forecast
previews and guarded official forecast submission for binary, multiple-choice,
and numeric/date question shapes; the web form forwards validated metadata JSON.
Large HTTP responses are written in bounded chunks so the support matrix and
exports cannot truncate on Windows loopback sockets. The BetMGM partner Sports
API remains a fixture-backed read-only/paper adapter; live and copy trading
remain explicitly unsupported because no official order/account surface is
available. The public-only Polymarket probe was rerun, but all four public
checks ended in connection resets from the audit host, so it supplies no
passing live evidence. This result does not by itself prove that the public
APIs or adapters are defective; it proves only that this run could not
establish reachability. Credentialed and funded checks were not attempted and
remain unawarded. No live evidence is invented to compensate for that gap. The
score therefore reflects repeatable repository proof plus explicitly supplied
evidence, not a production certification.

The repository also contains historical manifests under `evidence/`; they are
inputs, not automatically valid current proof. This audit deliberately supplied
none of them, so the local-only score remains **83/100**. When the current,
reviewed repository-settings manifest is supplied, it is accepted and the
evidence-backed score is **84/100**. Stale or
revision/tag-mismatched manifests are rejected, and all revision-bound evidence
must be recollected for the exact final commit. Reaching 100 requires fresh
proof for the current `v1.0.11` commit: repository settings (+1), release
environment/history/release (+3), deployment (+3), platform CI/targets (+5),
public probe (+3), credentialed read (+1), and approved funded audit (+1). No
points are awarded merely because an old manifest file exists.

The local pass is not a claim that every production failure mode has been
eliminated. The scorer's **84/100** result is the repository's formal
evidence score; an independent risk-adjusted review of the same tree is
**80/100 (no-go)** because several base categories award repository design
points before real deployment behavior is proven. The hardening tree now pins
durable stores below `/var/lib/market-sentinel`, verifies backup coverage,
serializes analytics and live-evidence updates with revision checks, isolates
web admission capacity, exposes readiness separately from liveness, requires
idempotency keys for durable web mutations, bounds WebSocket payloads, and
persists copy-activity dispatch intent before a live venue call.

The remaining risks are evidence and deployment boundaries rather than known
repository P0/P1 defects: this worktree is uncommitted and has not passed CI as
an exact revision; version `1.0.11` has no published, signed release and the
previous release attempt failed its Windows-signing policy; no real host has
proved deployment, restore, backup age, monitoring, or rollback; no current
platform matrix evidence is bound to this tree; and public, credentialed, and
funded live acceptance remain at zero. The architecture is still a monolithic,
single-node application. DNS validation also cannot eliminate the
validation-to-connect race in the current HTTP/WebSocket libraries, so the
production host must enforce allow-listed egress with a firewall or controlled
forward proxy. A venue copy dispatch has no end-to-end venue idempotency token:
ambiguous outcomes deliberately require manual venue-history reconciliation,
and the in-memory conflict cache is not a global durable deduplication system.
Branch protection currently requires no approving review and does not require
signed commits. These boundaries must be resolved or explicitly mitigated
before external evidence can support a 100/100 decision.

The scorer never treats a workflow matrix as proof that a runner completed.
It also does not promote Polymarket credentialed or funded tiers from a local
runbook, browser smoke test, or dry-run transcript.

## External Evidence Manifests

External points require a JSON manifest supplied with the corresponding
option. Every manifest must contain `schema_version: 1`, the exact
`evidence_type` for the scorer option that consumes it, a non-empty `source`,
`verified: true`, non-empty `reviewed_by` and ISO-8601 `reviewed_at` values,
and a non-empty `checks` array with unique names whose entries all have
`status` equal to `pass` or `ok`. Tier-specific fields are also validated so a
deployment, release, platform, or Polymarket report cannot be relabeled to
award another category. `reviewed_at` must include a timezone, be no more than
30 days old, and not be more than five minutes in the future. Revision-bound
evidence must identify the exact current `git rev-parse HEAD`; release evidence
must also identify the current project tag (`v1.0.11`). The scorer rejects all
revision-bound evidence while the worktree has tracked or untracked changes,
because a CI run or release for `HEAD` cannot prove uncommitted files.

| Scorer option | Required `evidence_type` | Required identity fields |
| --- | --- | --- |
| `--repository-settings-evidence` | `repository-settings` | `source` |
| `--deployment-evidence` | `deployment` | `scope`, `environment`, `expected_version=v1.0.11`, `source_revision=current HEAD` |
| `--platform-ci-evidence` | `platform-ci` | `scope`, `run_id`, `source_revision=current HEAD` |
| `--platform-evidence` | `platform` | `scope`, `targets`, `source_revision=current HEAD` |
| `--release-environment-evidence` | `release-environment` | `source` |
| `--release-history-evidence` | `release-history` | `scope`, `tag=v1.0.11`, `target_commit=current HEAD` |
| `--release-evidence` | `release` | `scope`, `tag=v1.0.11`, `target_commit=current HEAD`, `assets` |
| `--credentialed-evidence` | `credentialed-polymarket` | `scope`, `target_tier=credential_live_verified`, `report_hash`, `source_revision=current HEAD` |
| `--funded-evidence` | `funded-polymarket` | `scope`, `target_tier=funded_live_verified`, `report_hash`, `live_action=true`, `source_revision=current HEAD` |

Release-environment evidence must include passing checks named exactly
`release_required_reviewers`, `release_prevent_self_review`,
`release_protected_branches`, `release_signing_secrets`, and
`release_windows_code_signing_required`. Missing, unknown, or renamed checks
fail closed. The repository currently includes
`evidence/release-environment.json`, a historical snapshot that intentionally
does not prove signing-secret presence; the scorer therefore rejects it for
the environment point, and the release workflow remains fail-closed until the
two signing secrets are configured and freshly verified.

```json
{
  "schema_version": 1,
  "evidence_type": "platform-ci",
  "verified": true,
  "reviewed_by": "operator-or-reviewer",
  "reviewed_at": "2026-08-03T18:00:00Z",
  "source": "GitHub Actions run URL or redacted host report",
  "scope": "hosted-ci",
  "run_id": 123,
  "source_revision": "0123456789abcdef0123456789abcdef01234567",
  "checks": [
    {"name": "aggregate_python_package_build", "status": "pass"},
    {"name": "python_ubuntu_matrix", "status": "pass"},
    {"name": "python_macos_14_15_26_matrix", "status": "pass"},
    {"name": "python_windows_2025_vs2026_matrix", "status": "pass"},
    {"name": "rhel_ubi_8_9_10_and_rhel_7_abi", "status": "pass"},
    {"name": "rocky_linux_8_9_10", "status": "pass"},
    {"name": "windows_11_arm", "status": "pass"},
    {"name": "react_build", "status": "pass"},
    {"name": "mobile_web_smoke_android_and_ios", "status": "pass"},
    {"name": "tkinter_gui_lifecycle", "status": "pass"}
  ]
}
```

The example SHA is a placeholder. Replace every placeholder and ensure each
`source_revision` or `target_commit` equals the exact checkout being scored.

Example after the evidence has actually been collected and reviewed:

```bash
python scripts/check_product_readiness.py \
  --full-local \
  --run-public-live \
  --deployment-evidence evidence/deployment.json \
  --platform-ci-evidence evidence/platform-ci.json \
  --platform-evidence evidence/platform.json \
  --repository-settings-evidence evidence/repository-settings.json \
  --release-environment-evidence evidence/release-environment.json \
  --release-history-evidence evidence/release-history.json \
  --release-evidence evidence/release.json \
  --credentialed-evidence evidence/polymarket-credentialed.json \
  --funded-evidence evidence/polymarket-funded.json \
  --require-100
```

Do not put venue credentials, private keys, cookies, or raw request logs in an
evidence manifest. Use the deployment and Polymarket runbooks to produce
redacted results, then review the source revision and check results before
passing a manifest to the scorer.
