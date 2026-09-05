# Production Readiness

`python scripts/check_product_readiness.py` is the repository's conservative
readiness score. It reports a score out of 100 and separates repeatable local
proof from evidence that can only be collected on a real host, account, or
GitHub repository.

## Current Independent Assessment

The latest independent review scored `b7b9b21` plus the managed-WebSocket
candidate **75/100 on 2026-09-05**, not approved for unattended funded production.
That candidate is now committed as `b548461`; its [CI](https://github.com/Yunushan/market-sentinel/actions/runs/33985401422)
passed 48 jobs with two optional skips, and its
[Security](https://github.com/Yunushan/market-sentinel/actions/runs/33985401417)
passed. Those results do not validate subsequent local changes. The candidate
remains on open PR #79, not protected main or a published release. Earlier
independent assessments and the 83-point formal local checklist below are
historical results with different scope, not competing current approvals.
Missing external acceptance cannot be supplied by a score.

The next recovery candidate closes a reproduced false-positive restore check:
a checksummed backup containing malformed configuration previously passed the
inventory-only drill. Collection now boots the restored application in an
isolated read-only child, checks health/state and SQLite integrity, and rejects
changed bytes or backend egress. Review requires this exact-runtime application
evidence. Local regression results do not establish actual VPS recovery or raise
the independent score by themselves.

The accepted browser-workflow baseline applies the saved React theme,
preserves view navigation through reload/back/forward, names the previously
unnamed controls, announces alert/wallet results and errors, and clears an edit
form after its wallet is deleted. Semantic palette variables and responsive form
columns prevent light-only surfaces and cramped alert selectors. The committed
Playwright acceptance gate uses isolated temporary backend state with outbound
connections forbidden, covers desktop/mobile widths in both themes, and retains
screenshots/traces in CI. See [Browser Acceptance](BROWSER_ACCEPTANCE.md) for
scope, commands and limitations. All 16 local Edge acceptance cases passed,
as did the 12 existing frontend tests and 24 focused runner/workflow/metadata
tests. The full Windows verifier ran 1,405 cases with the same three errors,
one failed assertion and nine skips originating in local TLS trust rejection;
it is not a full local pass. Source/wheel artifact verification and the npm
audit (including development dependencies) passed. Exact-commit hosted CI now
also passed the 16 browser cases and package build. This supports one point over
the preceding 74/100 assessment, not acceptance of financial, real-host or
funded-account behavior.

The response-disconnect follow-up identifies broken pipes, resets and aborts at
the client output stream. It does not relabel upstream errors as disconnects or
retry writes to a closed client. Local request logs/metrics use 499 with the
attempted response status preserved; durable creates retain their commit and
idempotent replay behavior. This follow-up requires its own exact-commit tests
and CI before receiving additional credit.

Local follow-up verification passed 10 focused Windows disconnect tests, all 41
Linux HTTP/deadline/TLS tests and all 16 browser cases. The browser run observed
four orderly disconnect records and no backend tracebacks. The full Windows
verifier ran 1,415 cases in 68.867 seconds with the same three TLS errors, one
TLS-related failed assertion and nine skips; it remains a failed full local run.

The local Windows TLS failure was diagnosed without bypassing verification:
the peer certificate issuer is `Avast Web/Mail Shield Untrusted Root`, not the
fixture's ephemeral CA. The certificate-chain rejection is therefore expected
for the intercepted connection. Keep the original DNS/SNI and negative trust
tests intact. Run them in a clean Windows VM or runner without HTTPS interception;
do not trust the interceptor's untrusted root, use `verify=False`, disable
hostname checks, or skip the cases to manufacture a full local pass.

The next managed-WebSocket candidate addresses a reproduced scope-exit cleanup
failure: cancellation after disarming the socket guard could leave a connection
open despite connect() raising. Cleanup now surrounds the complete connection
scope. Direct connections pin validated DNS addresses, preserve verified TLS/SNI,
reject non-101 upgrades, and share the setup deadline across DNS/TCP/TLS/HTTP.
The channel clients no longer resolve DNS before that deadline and their stop
events cancel pending direct setup. A Windows unconnected-socket guard fix also
preserves socket family/type/protocol and releases a failed duplicate handle.
Focused regressions use real socket pairs, loopback upgrades, ephemeral-CA TLS,
slow peers and actual worker stop events. Memory-BIO TLS through urllib3 keeps
the guarded raw socket owned throughout the handshake while retaining native
certificate verification. The real TLS session test covers partial-frame
timeouts, fragmented messages, ping/pong, binary traffic and an orderly close
after the setup deadline. Custom SSL contexts and CA/client-certificate options
are retained. urllib3 is now an explicit runtime dependency, with the existing
hash-locked version unchanged. Configured proxies are preserved but remain
outside the direct pinning/active-cancellation guarantee. This candidate needs
its own exact-commit hosted checks, not transferred release, deployment,
financial-history or funded acceptance evidence.

Candidate verification passed 23 Linux WebSocket tests, ran 170 Linux Polymarket
tests with one platform-specific skip, and passed 20 focused Windows metadata
and dependency-lock tests. Wheel/source artifact validation and Ruff passed.
The final Windows full verifier ran 1,437 cases in 70.257 seconds with five
errors, one failed assertion and nine skips, all six failure records originating
in the local TLS fixture's certificate-trust rejection. This is not a full local
pass, and no certificate or hostname-verification bypass has been introduced.

## Audit History: 2026-09-05

The committed transport follow-up `981a2e0` passed exact-commit hosted
[CI](https://github.com/Yunushan/market-sentinel/actions/runs/33975571723)
(48 successful jobs, two optional skips) and
[Security](https://github.com/Yunushan/market-sentinel/actions/runs/33975571816).
This includes macOS deadline and real SIGTERM regressions after retaining POSIX
reader descriptors until normal cleanup. It does not verify subsequent edits.

The next storage candidate requires WAL/FULL synchronization and requests
macOS fullfsync before scan writes. Its tests exercise actual SQLite-full errors and
abrupt exits inside page/MDD transactions, retaining committed progress. SQLite
backups now preflight a pinned snapshot before copying and check a configurable
per-database budget between bounded copy steps. These are storage-contract
improvements, not evidence of host power-loss behavior or a 100/100 promotion.
The focused storage/backup/CLI/deployment group passed 172 tests on Linux and
ran 172 on Windows with five platform-specific skips. The full Windows verifier
ran 1,400 tests with nine skips, three errors and one failed assertion, all four
failures originating in the local TLS fixture's certificate-trust rejection.
Ruff, dependency/workflow checks, frontend build and browser smoke passed.
This is not a full local pass; exact-commit hosted verification is still required.

A subsequent review of the working tree (committed transport baseline
`cc13dbe`, plus uncommitted deadline/cancellation changes) scored
**70/100**, not a release approval. It reproduced two concrete candidate bugs:
HTTP/1.0 and `Connection: close` responses lost their cancellation guard before
body consumption, and rate-limiter lock acquisition ignored the deadline. The
slow-TLS regression was still blocked at a 12-second diagnostic cutoff despite
a 0.5-second budget. This result must not be replaced by the historical formal
83/100 checklist or the earlier committed candidate's passing CI.

The deadline candidate now preserves guards through nonpersistent responses,
bounds rate-limiter lock waits, and keeps one monotonic budget across managed
request retries. Cancellation propagates through concurrent page/MDD work and
CSV/JSON file publication. A real Linux SIGTERM regression preserves committed
pages, leaves interrupted MDD pending, keeps the previous export, and exits 143.
Failed watchdog startup releases its owned socket handle. No live support gate
has been enabled. Candidate tests are evidence for these specific fixes, not
financial, deployed-host, signed-release or funded-account acceptance. Exact
candidate CI and Security must be revalidated before awarding new credit.

An earlier clean-commit review of `b068de1` scored **73/100**, with the
**83/100 formal local checklist** and passing exact-commit CI/Security. It
credited the cache correction and hosted checks but reproduced stale DNS
address retention in shared connection pools. This is still an assessment of
an unmerged branch, not approval of a production release or funded execution.

The following transport hardening now keys pools by both their normal TLS/origin
configuration and their validated destination addresses, and routes direct
Polymarket HTTP through the shared managed session. New regressions cover DNS
changes and rebinding, TLS hostname/certificate verification, redirect rejection
before body consumption, response/session cleanup, and mutation retry limits.
On this Windows audit machine, Avast replaces the ephemeral local TLS server's
certificate with an untrusted interception certificate. The client rejects it;
the Windows candidate run has two errors among 1,364 cases (seven skipped).
Independent Ubuntu WSL execution passes all 12 HTTP transport tests, including
positive TLS, untrusted/wrong-host certificates and DNS rotation. Broad WSL
discovery passes 1,232 cases but has three module-import errors because Tkinter
is absent, plus two skips; it is not a full-suite pass. The Linux CLI starts
without Tkinter. Verification has not been disabled and TLS tests are not skipped.
Do not carry the baseline local pass or external evidence forward to this
candidate merely because the earlier commit passed.

An earlier independent review assessed the preceding working tree at **70/100**,
with a passing local gate and **83/100 formal checklist**. It reproduced a
cache read-error defect: valid data could be quarantined and replaced by an
empty active cache after a temporary read failure. That defect is now fixed
and regression-tested, without automatically awarding more readiness points.
An earlier 68/100 review had a failing local gate (49/100 formal checklist)
before replay-test and Windows replacement integration. None of these results
is production certification or acceptance of protected main or published artifacts.

The baseline scan/cache fixes at `b068de1` passed
`python -B verify.py --frontend-build --frontend-live-smoke`: **1,346 tests ran,
7 skipped on Windows**, with 73% overall and 76% backend combined
statement/branch coverage. Overall statement coverage is 78% and branch coverage
is 60%; the combined percentage is not the percentage of branches exercised. Verified
changes include:

- MDD resume binds completed enrichment to normalized calculation options and
  algorithm version. Changed settings or legacy results without calculation
  metadata invalidate only MDD, retaining fetched pages; matching calculations
  survive filter changes.
- Web/API and SQLite scan results keep the first observation per normalized
  wallet before filtering/MDD. Raw scanned rows and unique wallets are counted
  separately; legacy database migration preserves pagination progress.
- Page retries preserve existing MDD, and conflicting page rewrites fail closed.
- Progress logging no longer queries aggregate database counts per MDD result.
- Regression tests cover mode/equity/history/accounting changes, rollback,
  legacy migration, overlapping wallets and resumed finite scan budgets.
- An OS-held writer lock prevents concurrent scan resets/enrichment and is
  released after abrupt process exit; separate-process tests verify ownership.
- Status/export use read-only SQLite snapshots. Export metadata and rows stay
  consistent while the scan writer commits additional pages.
- Interrupted CSV/JSON file exports preserve the previous file via atomic
  replacement. Database/journal/lock paths cannot be used as export targets.
- Active-scan backup/restore tests omit lock/journal sidecars and restore the
  committed database. Failed scan resets roll back rather than erasing state.
- Dollar and percentage MDD have independent maxima/provenance, include the
  initial observed-window loss, and return unknown risk for empty PnL histories.
  A deterministic all-prior-points reference checks 150 random curves at three
  equity bases. API/CLI tests reject the reproduced 25%-drawdown wallet.
- Calculation version 5 invalidates old durable enrichment. Accounting rebasing
  uses all observed drawdown episodes even when displayed points are truncated;
  legacy audit exports remain available but explicitly marked non-current.
- Process MDD input/history caches use thread-safe TTL/LRU eviction, entry and
  serialized-data byte budgets, oversized-entry rejection and copy isolation.
- Serial/concurrent scans stop at the documented public leaderboard offset
  boundary without changing unlimited settings. Stable wallet membership detects
  repeats despite changed ranks/PnL/order and survives SQLite resume/migration.
- Replay computes all fetched trade/mark events before retaining bounded output;
  missing inventory/history cannot qualify through a diagnostic fast fallback.
- Accounting imports bound compressed/expanded bytes, member/file counts, rows,
  columns and record length. Partial snapshots cannot rebase percentage risk.
- PnL/volume replaces investment-ROI wording in controls; legacy `roi_pct` keys
  remain compatible, with explicit formula metadata in API/CSV/JSON results.
- Per-source history limits/windows persist through caches, SQLite and exports.
  Reaching a source cap yields unknown top-level risk, including after accounting
  reconciliation or resume. Exhausted public windows remain account-unverified.
- Frontend coverage tables expose source counts, limits and observed timestamps;
  component tests cover capped, legacy, malformed-timestamp and invalid-source
  diagnostic states, including escaped labels and rejected-row counts.
- Required PnL/time fields, optional financial values, duplicate position
  observations and ambiguous same-timestamp ordering are validated before risk
  is calculated. Invalid source history returns unknown MDD and cannot qualify
  via API/CLI/desktop exports, accounting rebasing or resumed SQLite results.
- Invalid source history stops mark replay before price requests; malformed or
  conflicting price points cannot be silently dropped to keep a valid subset.
- Atomic config/cache/evidence/export publication retries only specific Windows
  replacement errors, at most ten attempts with under 1.5 seconds total sleep.
  Persistent denial still raises and preserves the prior file. Tests exercise
  both injected failures and a real Windows reader without delete sharing.
- Desktop CSV export now preserves its previous complete output if a later row
  cannot be serialized, and retains unknown-risk diagnostics as JSON columns.
- Cache reads distinguish missing files from I/O failures and malformed bytes.
  Read-denied stores are not quarantined, overwritten, or reported as empty.
  Genuine corruption is preserved before recovery; failed quarantine or sync
  is reported. API/CLI regression tests verify explicit errors and recovery
  without losing artifacts or overwriting previous exports.

The frontend has 12 focused tests. Current synthetic capped/invalid-history
browser checks passed in headless Edge at 1440x1000 and 390x844; those checks do
not establish physical mobile-device or full cross-browser acceptance. The
concurrent Windows report-writer test also passed ten consecutive reruns after
the bounded retry change; this is evidence of improvement, not a claim that
every possible filesystem failure is eliminated.

A synthetic offline SQLite check ingested 100,000 rows containing 10,000
duplicates in 2.324 seconds on the audit machine and verified 90,000 distinct
wallets and next offset 100,000. This does not measure network/MDD throughput,
eight-million-wallet performance, production-host resource use, or durability
under machine failure.

The previous committed candidate review was **73/100**. It credited the tested direct
transport improvements but reproduced a separate overall-deadline gap: a
loopback server sending one byte every 0.1 seconds completed an 11-byte response
in 1.094 seconds despite a 0.25-second socket timeout. Requests' inactivity
timeout is not a wall-clock deadline. HTTP body reads/retries and scan batch
waiting still require end-to-end deadline and cancellation handling; byte caps
alone do not bound elapsed time.

Remaining acceptance work includes financial metric/history completeness,
independent reference datasets and full-account source coverage; a successful
current-version release and installation of its actual
artifacts; production-host restore/rollback and sustained-load exercises;
broader UI/native browser acceptance; and explicitly authorized credentialed
and funded venue validation. Live mutation gates remain disabled. No readiness
points should be awarded for external acceptance until its matching evidence
exists and is verified.

## Running the Scorer

Run the complete local verification profile:

```bash
python scripts/check_product_readiness.py \
  --full-local \
  --json
```

`--full-local` runs `verify.py --skip-pip-check --frontend-build
--frontend-live-smoke`. A prior clean baseline produced an **83/100 formal local
checklist** result. Rerun the command for a new candidate; that historical pass
does not replace the locally failing Windows TLS run described above. It is not
the deployment-oriented production score and does
not prove an exact deployed revision, release, external platform, or live venue.

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

## Score Model (Formal Scorer)

| Area | Points | Local checklist earned | Remaining proof required |
| --- | ---: | ---: | --- |
| Architecture and scope | 18 | 18 | None beyond the repository contract |
| Tests and correctness | 18 | 18 | No additional formal points; independent financial correctness and user-workflow acceptance remain required |
| Security and safety | 17 | 16 | Dispatch the implemented governance collector on a clean protected-main revision with the required admin-read token and supply its exact attested artifact |
| CI/CD and release | 17 | 14 | Exact live release-environment evidence plus a fresh GitHub-attested release report |
| Operations and recovery | 15 | 12 | Run and supply cryptographically attested real-host deployment evidence for the final current-version release; none was supplied to this local assessment |
| Platform evidence | 10 | 5 | Dispatch the implemented platform collector against a successful exact-revision CI run and supply both exact attested artifacts |
| Live acceptance | 5 | 0 | Reachable public endpoints plus the implemented credentialed/funded collectors' exact attested artifacts; funded execution also requires deliberate promotion of the offline-tested V2 mutation gate |

The recorded full local audit of baseline `b068de1` on 2026-09-05 was
**83/100 (not ready)** when no external
evidence manifests are supplied. Local verification covers the adapter catalog
(68 markets, 57 implemented and 11 explicitly blocked), 344 offline fixture
files, documentation, workflows, secret hygiene, frontend build/browser smoke,
and packaging checks. That verifier ran 1,346 tests (1,339 passed and 7
intentionally skipped on Windows), achieved 73% all-source and 76% backend
combined statement/branch coverage, satisfied the 65% overall and 74% backend
combined coverage floors, and passed Ruff. Counts are reported
from this run rather than carried forward from an older artifact.
The canonical capability snapshot is 55 price/paper adapters, 33 orderbook
adapters, 35 trade-history adapters, 39 candle-history adapters, and 23 guarded
live adapters; 34 adapters explicitly do not support live trading and 11 are
verified-blocked. Polymarket is in the unsupported-live group: its V2 wrapper is
offline-tested, but exact credentialed/funded acceptance and deliberate support
promotion are still pending.
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

## Historical Independent Assessment

This earlier deployment-oriented assessment was **73/100 - not ready**. It discounts
repository-design points that the formal scorer awards before an exact clean
revision, signed release, real production host, recovery exercise, and live
venue behavior are proven. This candidate includes uncommitted direct-transport
fixes; its baseline CI pass cannot stand in for exact-candidate hosted checks.
The table includes the separately reproduced overall-deadline gap and must be
reassessed against the final revision and its acceptance evidence.

| Area | Independent score | Maximum | Main deduction |
| --- | ---: | ---: | --- |
| Architecture and correctness | 16 | 18 | Financial capital/history definitions and full-account discovery still need independent validation |
| Tests and validation | 16 | 18 | The reviewed snapshot passed local verification; workflow and real-host acceptance were incomplete |
| Security and safeguards | 15 | 17 | Direct pinning and TLS tests improved; proxy/WebSocket egress and actual production controls remain unverified |
| CI/CD and release | 10 | 17 | Baseline CI is green; current fixes need exact-revision CI and a published, install-tested current-version release |
| Operations and recovery | 10 | 15 | Overall response deadlines/cancellation and actual-host restore, rollback, sustained load and alert delivery remain unproven |
| Platform evidence | 6 | 10 | Hosted matrix passes for the pushed head, but current native packages and real client-browser acceptance are incomplete |
| Live acceptance | 0 | 5 | Exact-revision authenticated/funded acceptance is absent; Polymarket mutations remain disabled |
| **Total** | **73** | **100** | **Reviewed candidate; unattended real-money production is not approved** |

Hand-authored schema-v1 repository-settings, release-environment, platform-CI,
platform, credentialed, and funded manifests are diagnostic inputs only. They
never award points. Schema-v2 governance, platform, and Polymarket collectors
now provide a structural path to the full formal 100, but every external point
remains withheld until a clean protected-main revision produces the exact
successful hosted workflow/job, artifact, and exact-byte attestation required
for that evidence type. This is separate from the independent assessment above. A
published stable release also remains unscored without fresh exact attested
release evidence. The independent release-evidence reconciliation workflow
re-drafts such a release after its Release run completes, including failed or
cancelled runs. A GitHub-wide outage can delay that compensating action, so the
scorer continues to fail closed rather than assuming cleanup occurred.

The repository also contains historical manifests under `evidence/`; they are
inputs, not automatically valid current proof. Revision-bound evidence must be
recollected for every final commit; it does not transfer merely because an old
manifest or workflow run exists.

The local pass is not a claim that every production failure mode has been
eliminated. The independent production assessment remains below the checklist because
several formal categories award repository design points before real deployment
behavior is proven. The hardening tree now pins
durable stores below `/var/lib/market-sentinel`, verifies backup coverage,
serializes analytics and live-evidence updates with revision checks, isolates
web admission capacity, exposes readiness separately from liveness, requires
durable idempotency journals for externally visible web mutations, drains new
mutations during bounded shutdown, bounds WebSocket payloads, and
persists copy-activity dispatch intent before a live venue call.

The remaining risks are evidence and deployment boundaries. Every candidate
revision must pass exact-revision CI and security before its external points are
re-awarded; source version `1.0.12` has no published, installed release proof and the
previous release attempt failed its Windows-signing policy; no real host has
proved deployment, restore, backup age, monitoring, or rollback; no current
platform matrix evidence is bound to this tree; and public, credentialed, and
funded live acceptance remain at zero. Polymarket live orders, cancellations,
relayer submissions, and funded verification are additionally disabled until a
durable external recovery journal and exact credentialed/funded acceptance make
the reviewed V2-only implementation safe to promote.
The architecture is still a monolithic,
single-node application. Direct HTTP connections in the shared adapter runtime
and Polymarket HTTP client pin validated destination addresses, but
proxy/WebSocket paths do not establish that same uniform boundary. The
production host must enforce allow-listed connect-time egress with a firewall
or controlled forward proxy. A venue copy dispatch has no end-to-end venue idempotency token:
ambiguous outcomes deliberately require manual venue-history reconciliation,
and the in-memory conflict cache is not a global durable deduplication system.
The historical governance snapshot required one approving review, dismissed stale
approvals, required approval after the last push, enforced the rules for
administrators, and did not require signed commits. Revalidate current settings
before using that snapshot. Signed-commit enforcement
and independent review evidence remain separate controls before external
evidence can support a 100/100 decision.

The scorer never treats a workflow matrix as proof that a runner completed.
It also does not promote Polymarket credentialed or funded tiers from a local
runbook, browser smoke test, or dry-run transcript.

The direct `--run-public-live` probe is diagnostic only and never awards points.
The three public-live points require a fresh report downloaded from the manual
`CI` workflow dispatched on protected `main` and supplied with
`--public-live-report`. Feature-branch runs are deliberately ineligible. The hosted path is
fail-closed: the scorer validates the report's exact public-only schema,
four endpoint results, absence of credentials or mutating actions, GitHub
Actions run and job outcome, exact `refs/heads/main` source revision, and GitHub artifact
attestation. The attestation must have been produced by this repository's
`.github/workflows/ci.yml` on a GitHub-hosted runner and the report must be no
more than 24 hours old. A passing hosted report proves reachability from that
GitHub runner at that time; it does not prove production-host egress,
credentialed access, account eligibility, or funded trading safety.

## External Evidence Manifests

Some external points require a JSON manifest supplied with the corresponding
option. Every reviewed manifest must contain `schema_version: 1`, the exact
`evidence_type` for the scorer option that consumes it, a non-empty `source`,
`verified: true`, non-empty `reviewed_by` and ISO-8601 `reviewed_at` values,
and a non-empty `checks` array with unique names whose entries all have
`status` equal to `pass` or `ok`. Tier-specific fields are also validated so a
deployment, platform, or Polymarket report cannot be relabeled to
award another category. `reviewed_at` must include a timezone, be no more than
30 days old, and not be more than five minutes in the future. Revision-bound
evidence must identify the exact current `git rev-parse HEAD`. The scorer rejects all
revision-bound evidence while the worktree has tracked or untracked changes,
because a CI run or release for `HEAD` cannot prove uncommitted files.

| Scorer option | Required evidence kind | Required identity fields |
| --- | --- | --- |
| `--repository-settings-evidence` | `repository-settings` | `source` |
| `--deployment-evidence` + `--deployment-origin` | Canonical deployment report from the protected-main production workflow | Exact release/tag/SHA, production origin, release frontend asset digest, production collector labels, hosted review, successful jobs/steps, unique fresh artifact, attestation, and protected-main ancestry must all verify; raw reports remain diagnostic-only |
| `--platform-ci-evidence` | `platform-ci` | `scope`, `run_id`, `source_revision=current HEAD` |
| `--platform-evidence` | `platform` | `scope`, `targets`, `source_revision=current HEAD` |
| `--release-environment-evidence` | `release-environment` | `source` |
| `--release-history-evidence` | Cryptographically attested `release-evidence.json` | Exact current tag/version/SHA, complete published history, successful trusted release run |
| `--release-evidence` | Cryptographically attested `release-evidence.json` | Exact eight asset names/sizes/SHA-256 values, current live release/tag, successful trusted release run |
| `--public-live-report` | Cryptographically attested public-only report | `evidence.repository=Yunushan/market-sentinel`, `source_revision=current HEAD`, successful workflow run/job, exact four public checks, no credentialed or mutating actions |
| `--credentialed-evidence` | Diagnostic-only strict Polymarket live report | Schema, clean source, cumulative public/read evidence, and promotion gates are reviewed; no points are awarded without trusted attestation |
| `--funded-evidence` | Diagnostic-only strict Polymarket funded audit | Same-account read, source gate, geoblock, balance/allowance, post-only, zero-fill, cancel, and resolved-journal evidence are reviewed; no points are awarded without trusted attestation |

Release-environment evidence must include passing checks named exactly
`release_required_reviewers`, `release_prevent_self_review`,
`release_protected_branches`, `release_signing_secrets`, and
`release_windows_code_signing_required`. Missing, unknown, or renamed checks
fail closed. The repository currently includes
`evidence/release-environment.json`, a historical snapshot that intentionally
does not prove signing-secret presence; the scorer therefore rejects it for
the environment point, and the release workflow remains fail-closed until the
two signing secrets are configured and freshly verified.

The two release options intentionally retain their existing CLI names, but
they no longer accept manually asserted `verified=true` release manifests.
Download the distinct `release-evidence-<sha>-<run>-<attempt>` artifact from a
successful stable `Release` workflow and pass its `release-evidence.json` file
to one or both options. The scorer verifies the exact file bytes and Sigstore
attestation, exact repository/current SHA/tag/version, workflow ref and event,
run attempt, hosted Ubuntu publish job, ordered successful release/evidence
steps, unexpired evidence artifact, protected-main ancestry, current live tag
and release, complete asset names/sizes/SHA-256 values, complete published
release history, every bounded historical tag's exact target and protected-main
ancestry, and a workflow collection/attestation no older than 24 hours. The
release's original `published_at` may be older; freshness is bound to the
canonical report's `generated_at`, GitHub run, job, artifact, and attestation.
Missing, stale,
mutated, paginated, manually authored, self-hosted, or otherwise ambiguous
evidence fails closed. Pass the same downloaded report to both options when it
must prove both release-history and exact-release points.

Deployment points are available only from the canonical report produced by the
protected-main production workflow and independently verified against its
GitHub-hosted review job, artifact, attestation, release asset, origin, and
current protected-main ancestry. A handwritten wrapper or raw collector report
still awards zero. Schema-v1 credentialed and funded inputs remain
diagnostic-only. The implemented protected-main Polymarket workflow can produce
score-eligible schema-v2 artifacts, but its credentialed tier requires real
secrets and eligible-account reads, and its funded tier intentionally fails
closed until the offline-tested V2 mutation support is deliberately promoted
and a bounded post-only order/immediate-cancel audit is explicitly approved.
The scorer has no remaining structural ceiling below 100. The historical
83-point result lacked external evidence; every new candidate must revalidate
local checks and supply its own qualifying external evidence.

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
  --public-live-report /path/to/public-polymarket-live.json \
  --platform-ci-evidence evidence/platform-ci.json \
  --platform-evidence evidence/platform.json \
  --repository-settings-evidence evidence/repository-settings.json \
  --release-environment-evidence evidence/release-environment.json \
  --release-history-evidence /path/to/release-evidence.json \
  --release-evidence /path/to/release-evidence.json
```

You may also pass raw schema-v1 `--deployment-evidence`,
`--credentialed-evidence`, or `--funded-evidence` to obtain fail-closed
diagnostics. Score credit requires the corresponding canonical schema-v2
artifact. Do not add `--require-100` until every exact hosted collector and real
external check has succeeded; it is expected to fail today.

Do not put venue credentials, private keys, cookies, or raw request logs in an
evidence manifest. Use the deployment and Polymarket runbooks to produce
redacted results, then review the source revision and check results before
passing a manifest to the scorer.
