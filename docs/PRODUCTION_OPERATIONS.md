# Production Operations

MarketSentinel is a local desktop/CLI application with a guarded optional web
interface. This guide covers a single-operator Linux deployment for analytics,
alerts, paper trading, and explicitly approved live-trading workflows. It does
not make a funded strategy autonomous or remove exchange eligibility, KYC, or
regional restrictions.

## Deployment boundary

- Keep `web_api.py` bound to `127.0.0.1`; do not publish port `8765` in a
  firewall, Docker mapping, or cloud security group.
- Serve browser access through a TLS reverse proxy with authentication. The
  provided Caddy example supplies Basic Auth, TLS, a restrictive browser
  content-security policy, cross-origin and permissions headers, and the
  upstream API token.
- The application also emits a conservative browser-security baseline on every
  local response (including errors and CORS preflights): CSP, anti-framing,
  no-sniff, no-referrer, restricted browser permissions, and opener isolation.
  Caddy remains responsible for public HTTPS-only HSTS, resource isolation, and
  removing the `Server` header at the internet-facing boundary.
- The threaded loopback server gives each connection 15 seconds to make
  progress, admits at most 32 concurrent request workers, rejects overload with
  `503` and `Retry-After`, and does not wait on stalled daemon workers during
  shutdown. It rejects ambiguous or incomplete request framing and caps every
  JSON, text, and static response at 16 MiB before sending response headers.
  The systemd unit retains its 30-second stop deadline as a final process-level
  safeguard.
- A broken pipe, reset or abort while writing a response is recorded once as
  `outcome=client_disconnected`, with local log/metric status `499` and the
  attempted HTTP status in `response_status`. No 499 or replacement 500 response
  is sent to the closed connection. The request still releases its worker and
  mutation admission; a committed mutation is not undone. Retry supported
  durable creates with the same idempotency key to reconcile uncertain delivery.
  Upstream connection failures remain backend errors, not client disconnects.
- When an API token is configured, the server permits ten failed token attempts
  per client per minute, then returns `429` with `Retry-After`. A valid token
  immediately clears that client record. This is a backstop for the proxy's
  authentication controls, not a replacement for Caddy Basic Auth or firewall
  policy.
- Run under the dedicated `market-sentinel` user. Use `/var/lib/market-sentinel`
  for state and a root-owned `/etc/market-sentinel/market-sentinel.env` for
  credentials and tokens.
- Never put credential values in `config.json`. Config load, save, the HTTP API,
  and the CLI reject persisted passwords, private keys, cookies, bearer tokens,
  and venue API secrets. Persist only validated environment-variable names or
  protected credential-file paths, then supply the values through the
  root-owned service environment or secret manager.
- Configuration writes use an advisory sibling lock, an on-disk revision
  check, `fsync`, and atomic replacement. A process that loaded an older
  revision fails closed instead of overwriting a newer writer; the web API
  returns `409 config_conflict`. Reload state before retrying. Keep the lock
  file on the same local filesystem as `config.json`, and do not run multiple
  active instances against network filesystems with unreliable advisory locks.
- Desktop settings, market selection, wallet/follow changes, and manual
  alert/history edits are persisted as detached field replacements before
  publication to the shared runtime configuration. Failed pre-commit saves do
  not install their settings or report successful actions. The config root and
  unchanged journal objects retain their identities for background workers.
- Configuration replacement is the commit point. `ConfigCommitError` means the
  replacement completed but subsequent synchronization or cleanup failed; the
  saved revision remains attached to the candidate. Do not treat that error as
  proof that nothing was written, or blindly restore the previous snapshot.
- After a desktop persistence failure or stale-writer conflict, configuration
  writes, alert evaluation and copy activity are paused for that process. A
  post-commit candidate remains reflected in memory, but execution stays paused;
  committed copy checkpoints/dispatch intent are not rolled back in memory.
  Fix permissions, capacity or writer conflicts, inspect the durable settings
  and any ambiguous order journal, then restart to reload verified state. Do
  not reset journals or assume restarting by itself resolves the underlying
  error. A later unrelated desktop action cannot save a previously rejected
  setting or automatically clear the pause.
- Live-validation report, decision, and promotion-snapshot stores use the same
  single-writer/atomic-replace discipline. Existing malformed JSON is preserved
  and rejected rather than silently replaced. Restore a reviewed backup before
  resuming evidence collection after a store-read error.
- Atomic publication of config, analytics cache, live evidence and text/CSV
  exports retries Windows access/sharing/lock replacement errors (5, 32, 33)
  at most ten times, with less than 1.5 seconds of total retry sleep. A held
  reader or file scanner can temporarily deny replacement. Persistent denial
  is still reported, and the previous file is not deleted to force success;
  resolve access/ownership before retrying. Other errors are not retried.
- Cache read failures are reported without treating an unreadable store as
  missing or corrupt. Restore access and retry; do not delete a valid cache
  to clear a read error. Malformed UTF-8/JSON or invalid entry containers are
  quarantined with their original bytes before an empty cache can be created.
  Quarantine rename or directory-sync failures are also reported, not hidden
  as successful recovery. Inspect the active and `.corrupt-*` files before
  retrying after such a failure.
- Adapter egress defaults to reviewed HTTPS/WSS endpoints, refuses redirects,
  and rejects private, loopback, link-local, reserved, or mixed-public/private
  DNS destinations. If a reviewed local integration genuinely needs a private
  origin, list its exact scheme, host, and port in
  `MARKET_SENTINEL_OUTBOUND_PRIVATE_ORIGINS`; never use wildcards, CIDRs, or a
  public deployment's browser/API settings endpoint to change it.
- Managed direct Polymarket WebSocket connections pin the current validated
  DNS addresses, retain the origin hostname for TLS/SNI and HTTP Host, and
  require a verified TLS connection and a completed 101 upgrade. DNS, TCP,
  TLS and HTTP upgrade share one connection deadline. Worker stop events
  cancel pending direct connections; subscriptions are not sent before upgrade.
  Successful connections disarm their setup deadline so a later timeout cannot
  close an established stream. Configured proxies remain honored and require
  their own connect-time egress and deadline enforcement.
- Managed Polymarket WebSocket connections reject any frame or complete
  fragmented message larger than 1 MiB. The frame-length check runs before
  `websocket-client` reads the declared payload, and the continuation check
  runs before fragments are concatenated. A deliberately injected custom
  WebSocket connection factory is outside that managed transport boundary and
  must provide an equivalent receive limit.
- Do not enable funded trading or live copy execution in a service until the
  evidence gates in `README.md` and `polymarket/live_verification.py` pass.
- Copy activity is checkpointed durably before any live handler is allowed to
  run, providing at-most-once crash behavior. If checkpoint persistence fails,
  execution is skipped. Review the error and reload/restart the operator process
  after resolving a configuration conflict; do not replay an uncertain funded
  action automatically.
- Polymarket price alerts retain a bounded REST polling path when a WebSocket is
  disconnected. WebSocket workers are restartable and stop with bounded joins;
  alert freshness and worker health still require monitoring through the
  application state and service logs.

### Required connect-time egress enforcement

URL validation resolves a configured hostname immediately before a managed
HTTP or WebSocket request and rejects every non-global answer unless the exact
origin is explicitly allowed. The shared adapter runtime pins those addresses
into direct HTTP connections while preserving the origin hostname for TLS.
The Polymarket HTTP client uses the same managed direct transport, with one
owned session spanning each bounded retry/body-read operation. Pools include
the validated address set in their identity, so a DNS change cannot reuse a
pool pinned to retired addresses. Redirect responses are closed before
Requests can eagerly consume their bodies while preparing a next request.
Managed direct WebSockets likewise resolve inside their connection deadline
and connect only to that validated address set, without a second hostname lookup.
Configured proxy and injected SDK/factory paths still need their own connect-time
enforcement; do not infer uniform pinning across all transports from direct
HTTP/WebSocket coverage.
TLS hostname verification, disabled redirects, and immutable endpoint settings
reduce exposure but do not remove every validation-to-connect race.

A production host must therefore enforce the destination again at connect time.
Use a service/cgroup-aware egress firewall or a forward proxy that meets all of
these requirements:

- deny loopback, RFC1918/unique-local, link-local, carrier-grade NAT,
  documentation, benchmark, multicast, unspecified, reserved, and cloud
  metadata destinations for service-originated outbound connections;
- resolve each requested hostname through a trusted resolver, reject the whole
  request if any answer is non-global, and connect to one of those already
  approved addresses without a second DNS lookup;
- preserve the original hostname for HTTP `Host`, TLS SNI, and certificate
  verification; permit only the required TCP ports (normally 443); and keep
  redirects disabled or repeat the complete policy for every redirect target;
- express an intentional private integration as both an exact
  `MARKET_SENTINEL_OUTBOUND_PRIVATE_ORIGINS` entry and an equally narrow network
  exception. The environment variable alone is not a firewall rule.

Because the web process also accepts legitimate loopback traffic from Caddy and
the health probe, a broad host rule that blocks loopback in both directions is
incorrect. Scope the outbound rule by service cgroup, process owner, proxy, or
connection direction. Before approving deployment evidence, exercise the rule
with controlled hostnames that return public, mixed public/private, and rebound
private answers; all but the stable public case must fail without reaching the
destination.

### HTTP deadlines and scan cancellation

Managed HTTP requests use a monotonic budget shared by DNS admission,
rate-limiter waits, response headers/body reads, and internal retry backoff.
The configured `timeout` is the budget for one HTTP operation, including its
internal retries; it is not the duration limit for an entire unlimited scan.
Response byte limits remain independent. Socket ownership lasts through
HTTP/1.0 and `Connection: close` bodies, and a completed request disarms its
pooled socket before another request can borrow it.

Leaderboard CLI runs handle SIGINT/SIGTERM by requesting cancellation. During
page/MDD work, cancelled requests are not retried or recorded as valid empty
history. Committed contiguous SQLite pages and completed MDD remain resumable;
an interrupted fetch or calculation does not overwrite the previous export.
Exit status is 130 for SIGINT and 143 for SIGTERM. Resume the same state database
with `--resume`, or inspect it with `polymarket-leaderboard-status`. Windows
service termination and forced process kills are not equivalent to POSIX
SIGTERM; abrupt-exit durability remains a separate contract.

An OS DNS lookup cannot be forcibly interrupted by Python. At most 16 daemon
DNS helpers may remain outstanding; expired lookups cannot initiate managed
direct HTTP/WebSocket work.
Cancellation callbacks must be fast, side-effect-free and thread-safe. Raw
connect/TLS setup can take up to the remaining transport timeout to unwind;
body reads and retry/rate-limiter waits are actively interrupted. Custom
injected transports, SOCKS transports and venue SDKs do not acquire these
guarantees merely by accepting a timeout argument. Configured WebSocket proxies
retain the library's connection route and do not inherit direct socket guards;
proxy deadline/egress enforcement and host acceptance above remain required.

### Durable state boundary

Leaderboard scan writers require SQLite WAL with `synchronous=FULL`. Each
page/MDD transaction requests a storage sync before reporting success, rather
than deferring it until a checkpoint. `fullfsync=ON` additionally requests
macOS F_FULLFSYNC where supported. Every writer connection reapplies and checks
these settings before schema migration or scan writes; an unavailable setting
fails closed. Read-only status/export connections do not change database mode.
This can reduce write throughput compared with the previous NORMAL setting.
Use a local filesystem with working locks and reliable storage sync; SQLite
settings and process-crash tests cannot certify a VPS provider's power-loss
behavior. Keep backups and perform real-host recovery drills. See the
[SQLite sync contract](https://www.sqlite.org/pragma.html#pragma_synchronous).

The bundled production environment pins every non-configuration durable store
below `/var/lib/market-sentinel`:

- `POLYMARKET_ANALYTICS_CACHE_PATH` writes
  `polymarket_analytics_cache.json`.
- `POLYMARKET_LIVE_VALIDATION_REPORTS_PATH` writes
  `polymarket_live_validation_reports.json`.
- `POLYMARKET_LIVE_VALIDATION_DECISIONS_PATH` writes
  `polymarket_live_validation_decisions.json`.
- `POLYMARKET_LIVE_VALIDATION_PROMOTION_PROPOSAL_SNAPSHOTS_PATH` writes
  `polymarket_live_validation_promotion_proposal_snapshots.json`.

These assignments are required, not optional deployment suggestions. The web
unit requires `/etc/market-sentinel/market-sentinel.env` and runs an exact-value
preflight for all four variables before `doctor` or the server starts. This
keeps writes inside the unit's sole `ReadWritePaths` state root while
`ProtectSystem=strict` makes the release checkout read-only. The backup unit
recursively captures that same state root, so every existing regular store file
is included in the next successful snapshot. Do not relocate one store without
also reviewing the sandbox, backup source, restore procedure, and production
deployment verifier; the verifier inspects the running process environment and
fails if its effective paths or backup source differ from this boundary.

### Durable-mutation idempotency window

Configuration loading rejects duplicate JSON keys, non-finite numbers,
malformed record collections and market/safety-setting containers. Existing
journals above the supported capacity are rejected unchanged, not trimmed on
startup: sorting away an older pending live operation would erase its replay
protection. Normal append-time retention still removes completed/rejected
records while preserving unresolved ones. Valid older configurations may omit
the newer journal collections. Saving validates the snapshot before publication
so malformed or non-finite state cannot replace a valid configuration.
An inaccessible, unreadable, symlinked or special-file configuration is not an
empty store. Both loading and the save-time revision guard distinguish a truly
missing directory entry from an inspection/read error. This avoids relying on
[`Path.exists()`](https://docs.python.org/3/library/pathlib.html#pathlib.Path.exists),
which can return false for inaccessible files.

Operational flags in stored configuration must be JSON booleans (`true` or
`false`), not quoted strings, numbers or nulls. Copy percentage/scale, slippage,
positive per-trade caps and integer conflict windows are validated on load and
before save. Valid finite legacy numeric strings remain accepted, but invalid
values are not clamped, truncated or replaced with a 100% copy allocation.
When both percentage and scale are present, they must agree within serialization
rounding. Existing invalid settings need explicit operator correction.

The HTTP API and CLI validate the original mutation values with the same model
rules before changing settings. Send actual JSON booleans, not `"true"` or
`"false"`; ordinary CLI `--enabled`/`--no-live` flags remain supported. Integer
conflict windows must not contain fractional values. If both percentage and
scale (or multiple percentage aliases) are supplied, they must agree; likewise
top-level and nested market safety controls must not contradict each other.
An empty wallet string or empty wallet list intentionally clears the follows;
nulls and non-string members are invalid. If both follow aliases are supplied,
the single identity must match the first normalized list identity.

Shared market safety flags and positive caps are also checked when loading and
saving configuration. A blank string or null explicitly unsets an optional
market cap; a supplied nonblank cap must be positive and finite. Numeric
booleans, NaN and infinity are rejected. Desktop copy settings are validated
before being installed in memory. HTTP/CLI JSON objects, including `--json
@file` and structured `--setting` values, reject duplicate keys and non-finite
JSON numbers rather than silently taking the last value. Invalid settings must
be corrected explicitly; the application does not rewrite or reset damaged
configuration to make it load.

Mutation journals require stable record IDs, SHA-256 key/request hashes,
mutation method/path and an explicit boolean live classification. Copy outboxes
require stable record, market, watch and activity identities. Duplicate record
IDs, duplicate journal client keys or duplicate outbox signal identities fail
closed. Malformed replay metadata and completed records without a response
status are rejected. Missing/unknown dispatch states remain ambiguous, never
automatically pending. Valid legacy configurations can omit journal collections,
but incomplete records inside a present journal are not reconstructed with new
identities. Restore acceptance uses these same configuration rules.

On a configuration-integrity error, preserve the original bytes and reconcile
against a verified backup and venue history. Do not replace the configuration
with empty defaults or delete its trading journals to get the process started.

The live-report, decision, and promotion-snapshot create routes use an opaque
`Idempotency-Key` (1-128 visible ASCII characters, with no whitespace). Only a
SHA-256-derived binding is persisted. A retry with the same key and canonical
request reconciles a committed file replacement, including a prior uncertain
parent-directory sync, without adding a second record or duplicate-import audit.
Reusing a key for different inputs fails closed.

Idempotency retention follows the evidence record: report and promotion
snapshot keys remain protected while their records remain inside the configured
bounded store, while decision keys remain with the decision ledger. After an
operator explicitly purges a record or bounded retention prunes it, that old key
is no longer reserved and clients must not reuse it. Duplicate-import bindings
on one retained report are capped at 256 to keep authenticated retry metadata
bounded. Browser clients retain one generated key across transport-uncertain
retries and discard it only after a terminal HTTP response or success.

## Install on RHEL/Rocky/Ubuntu

```bash
sudo useradd --system --home /var/lib/market-sentinel --shell /sbin/nologin market-sentinel
sudo install -d -o market-sentinel -g market-sentinel -m 0700 /var/lib/market-sentinel
sudo install -d -o root -g market-sentinel -m 0750 /etc/market-sentinel
sudo install -m 0600 deploy/systemd/market-sentinel.env.example /etc/market-sentinel/market-sentinel.env
sudo install -m 0644 deploy/systemd/market-sentinel.conf /etc/tmpfiles.d/market-sentinel.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/market-sentinel.conf

sudo mkdir -p /opt/market-sentinel
sudo chown "$USER" /opt/market-sentinel
RELEASE_VERSION="<RELEASE_VERSION>"
git clone https://github.com/Yunushan/market-sentinel.git /opt/market-sentinel
cd /opt/market-sentinel
git fetch --tags --force origin
git switch --detach "v${RELEASE_VERSION}"
EXPECTED_SOURCE_REVISION="$(git rev-parse --verify HEAD^{commit})"
test -z "$(git status --porcelain=v1 --untracked-files=all)"

# Install the exact locked frontend tree before the strict verifier builds it.
npm --prefix frontend ci --ignore-scripts --no-audit --no-fund

# Validate the checked-out source with the test dependency set before deployment.
python3 -m venv .verify-venv
.verify-venv/bin/python -m pip install --require-hashes -r requirements-bootstrap.lock
.verify-venv/bin/python -m pip install --require-hashes -r requirements-test.lock
.verify-venv/bin/python -m pip install --no-deps .
.verify-venv/bin/python verify.py --frontend-build --frontend-live-smoke
rm -rf .verify-venv

# Install the lean runtime dependency set used by the systemd service.
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-bootstrap.lock
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m pip install --no-deps .
```

An authenticated Polymarket CLOB SDK is intentionally excluded from the
baseline runtime. Install it only for an explicitly approved signed-trading
workflow:

```bash
.venv/bin/python -m pip install --require-hashes -r requirements-live.lock
```

The strict verifier above built the React frontend. Capture its reviewed
fingerprint before starting the service:

```bash
cd /opt/market-sentinel

# Capture this from the reviewed, clean release build before the service starts.
EXPECTED_FRONTEND_SHA256="$(.venv/bin/python -c 'from pathlib import Path; from core.deployment_identity import frontend_tree_sha256; print(frontend_tree_sha256(Path("frontend/dist")))')"
printf '%s\n' "${EXPECTED_FRONTEND_SHA256}" | sudo tee /etc/market-sentinel/frontend-dist.sha256 >/dev/null
sudo chmod 0600 /etc/market-sentinel/frontend-dist.sha256
```

Treat `/etc/market-sentinel/frontend-dist.sha256` as deployment evidence, not
as a value to regenerate immediately before verification. It must be captured
from the reviewed clean release build before the service starts and kept under
root ownership. Rebuilding or changing `frontend/dist` requires recording a new
reviewed fingerprint and restarting the service.

Install the systemd unit and validate it:

```bash
sudo install -m 0644 deploy/systemd/market-sentinel-web.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/market-sentinel-health.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/market-sentinel-health.timer /etc/systemd/system/
sudo install -m 0644 deploy/systemd/market-sentinel-backup.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/market-sentinel-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now market-sentinel-web
sudo systemctl enable --now market-sentinel-health.timer
sudo systemctl enable --now market-sentinel-backup.timer
sudo systemctl start market-sentinel-backup.service
sudo systemctl status market-sentinel-web
sudo systemctl status market-sentinel-health.timer
sudo systemctl status market-sentinel-backup.timer
sudo journalctl -u market-sentinel-web -f
/opt/market-sentinel/.venv/bin/python /opt/market-sentinel/scripts/verify_service_health.py
/opt/market-sentinel/.venv/bin/market-sentinel doctor --strict --config /var/lib/market-sentinel/config.json --frontend-dir /opt/market-sentinel/frontend/dist
```

The CLI `serve --frontend-dir` option is supported for deployment-relative
builds and is validated before the socket is opened. The resolved directory
must remain beneath the release/resource root, so a typo or an unsafe path
fails closed instead of changing the HTTP static-file root.
The static catalog is built once from that canonical root; each candidate is
resolved and checked relative to the root before it is read, so request URLs
never construct filesystem paths.

The web and health units use strict systemd sandboxes, private device and
hostname/clock namespaces, restricted network address families, and a root-owned
environment file. Missing that file or changing a required durable path prevents
the web service from starting. After those path assertions, the web unit has a
strict read-only `doctor` preflight and a startup health check; the timer runs a
separate loopback health check every minute. Both units limit start failures to
five attempts in five minutes, and the health unit times out after 30 seconds. A
start-limit hit is an operator action item rather than a signal to retry
continuously; inspect
`journalctl -u market-sentinel-web` or `journalctl -u market-sentinel-health`
and use `systemctl reset-failed` only after correcting the cause. Review
`systemd-analyze security market-sentinel-web` and
`systemd-analyze security market-sentinel-health` after installation and tighten
any setting that does not prevent normal operation on the chosen distribution.
The web unit manages `/var/lib/market-sentinel` with `StateDirectory` and mode
`0700`, so a normal service start does not depend on a pre-existing writable
state directory. The initial install command remains useful for inspecting
ownership before the first start.

The backup timer runs a local, network-isolated state backup each day with a
14-pair retention limit. It writes archives and SHA-256 manifests only to
`/var/lib/market-sentinel-backups`, owned by the service account and separate
from the live state directory. Place `/var/lib` on encrypted storage or change
the backup destination to an encrypted mounted volume before using this in
production. The archive and then its manifest are each published atomically,
with their directory changes synced on POSIX filesystems. Retention counts only
cryptographically verified, restorable archive/manifest pairs. An orphan left
by an interrupted publication and an invalid pair are preserved for operator
inspection, do not consume a retention slot, and cannot evict a valid backup.
SQLite state databases are captured with SQLite's online backup API instead of
copying WAL, shared-memory, or rollback-journal sidecar files. A read transaction
pins each database snapshot before its page-count/size preflight, so concurrent
WAL commits cannot expand the copy beyond its accepted payload budget. Oversized
databases are rejected before a staged database is created. `--sqlite-timeout`
sets the per-database lock/copy budget (30 seconds by default). Copying checks
that budget after each 64-page step; this is not an interrupt for a blocked
kernel/filesystem call. The systemd unit's five-minute process timeout remains
the final service-level bound. Failed or timed-out copies publish no new pair
and do not prune prior valid backups. Other regular
files are copied to a private stable snapshot and rejected if they change while
being copied. Creation enforces the same member, payload, compressed-archive,
and bounded tar-overhead limits as verification; it verifies the staged pair
before publishing the archive and then the manifest. The archive intentionally excludes
`/etc/market-sentinel` and its credentials; protect and back up that root-owned
configuration through the host's secret-management and configuration process.

## TLS and browser access

Install Caddy from its official package repository, copy
`deploy/caddy/Caddyfile.example` to `/etc/caddy/Caddyfile`, and replace the
example hostname. Set these protected Caddy environment values:

```bash
MARKET_SENTINEL_API_TOKEN="$(openssl rand -hex 32)"
MARKET_SENTINEL_CADDY_PASSWORD_HASH="$(caddy hash-password --plaintext 'replace-this-password')"
MARKET_SENTINEL_ALLOWED_ORIGINS="https://analytics.example.com"
```

Use the same `MARKET_SENTINEL_API_TOKEN` in
`/etc/market-sentinel/market-sentinel.env`. Configure DNS and permit only ports
80/443 to Caddy. Keep 8765 private. Test the public hostname, the TLS renewal
path, and authenticated browser flow before enabling any live feature. Set
`MARKET_SENTINEL_ALLOWED_ORIGINS` in that protected environment file to the exact
public Caddy origin; it must match the replaced Caddy hostname, omit any path,
and must not use a wildcard. Multiple separately trusted origins are
comma-separated.

## Deployment evidence

After a deployment, collect a read-only verification record from the VPS. It
checks the systemd web service and health timer, validates the loopback health
endpoint, authenticated Prometheus metrics endpoint, and release version, and, when given a public URL, proves that an
unauthenticated request receives `401` before validating the authenticated HTTPS
proxy response, cache policy, the required browser-security header directives,
and removal of the public `Server` header.
It also verifies the root-owned, private service environment file and private
state/backup directories used by the bundled systemd units.
It extracts only the four non-secret durable-path values from the running web
process environment for evidence, proves they are the exact paths beneath the
sandbox-writable state directory, and checks the effective backup command
captures that directory. A
missing variable, a release-tree path, an unbacked path, or a stale installed
unit makes the evidence fail rather than silently accepting partial backups.
It also requires a successful backup service completion and independently opens
at least one archive/manifest pair from the trusted private backup directory,
verifies its SHA-256 digest and bounded archive structure, and requires its
manifest timestamp to be within the last 26 hours. Enable the timer and run the
service once before collecting deployment evidence. The bundled directory is
the safe default; use `--backup-directory` only when the systemd backup
destination was intentionally changed to another absolute, private,
service-owned path with no symbolic-link components.
`--expected-version` is required: it prevents a healthy but stale deployment
from being accepted as release evidence.
`--expected-source-revision` is also required: it prevents a healthy service
from being accepted when the checkout does not match the intended release
commit. Resolve it from the trusted release tag before running the verifier.
`--expected-frontend-sha256` binds both the running process and the files served
from disk to the fingerprint captured from the reviewed frontend build. The
verifier does not derive this expected value from the mutable live tree.
It does not place orders, contact market APIs, or enable any live feature.

```bash
export MARKET_SENTINEL_PUBLIC_BASIC_USER="operator"
export MARKET_SENTINEL_PUBLIC_BASIC_PASSWORD="the-existing-caddy-password"
export MARKET_SENTINEL_API_TOKEN="the-existing-market-sentinel-api-token"
RELEASE_VERSION="<RELEASE_VERSION>"
EXPECTED_SOURCE_REVISION="$(git -C /opt/market-sentinel rev-parse --verify "v${RELEASE_VERSION}^{commit}")"
EXPECTED_FRONTEND_SHA256="$(sudo cat /etc/market-sentinel/frontend-dist.sha256)"

sudo --preserve-env=MARKET_SENTINEL_PUBLIC_BASIC_USER,MARKET_SENTINEL_PUBLIC_BASIC_PASSWORD,MARKET_SENTINEL_API_TOKEN \
  /opt/market-sentinel/.venv/bin/python /opt/market-sentinel/scripts/verify_production_deployment.py \
  --expected-version "${RELEASE_VERSION}" \
  --expected-source-revision "${EXPECTED_SOURCE_REVISION}" \
  --expected-frontend-sha256 "${EXPECTED_FRONTEND_SHA256}" \
  --frontend-dir /opt/market-sentinel/frontend/dist \
  --backup-directory /var/lib/market-sentinel-backups \
  --public-url https://analytics.example.com \
  --output /var/lib/market-sentinel-deployment-evidence/deployment-evidence-<RELEASE_VERSION>.json
```

Keep the password and API token only in the environment. Do not pass either
secret on the command line. The API token must exactly match the token in the
root-owned service environment file; it lets the verifier prove both that the
loopback API rejects tokenless requests and that Caddy injects the configured
token only after successful Basic Auth.
The generated JSON contains a schema version, UTC collection timestamp, and source version/revision status but no credentials; `--output` requires an existing,
private root-owned parent directory, writes atomically with mode `0600`, and
syncs the replacement directory entry on POSIX so a service account cannot
replace the release-change record. Repeat
the verification after every restore drill. The command
uses `sudo` because it verifies the root-owned service environment file; it
preserves only the two explicitly named Basic Auth variables and the API token
needed for the public proxy and loopback authentication checks. For a
loopback-only staging host, omit `--public-url`; the script will still validate
the local service and timer, but retain all three expected identity arguments for the
deployed release.

Production collection also restores the newest verified backup into a private
temporary directory and boots an isolated read-only application from that copy.
The backed-up `config.json` must exist and load without dropping durable journal
records. Known JSON stores must pass the application's readiness checks, and
restored SQLite files must pass integrity and foreign-key checks. The probe
requires successful health and state responses, tests that all mutation methods
are rejected, and compares file hashes before and after startup. Its subprocess
has a 60-second budget, no inherited credentials or proxies, redirected durable
store paths, and backend socket/DNS connections denied. It never promotes the
restored files over running production state.

The resulting restore evidence includes the application version, source and
frontend fingerprints. Inventory-only restore reports are no longer accepted by
the reviewer or readiness scorer. This isolated probe is not evidence of
off-host backup recovery, service-account permissions, full business-workflow
recovery, measured RPO/RTO, or an actual systemd failover; those operational drills
still need to be performed on the deployment host.

Review the raw collector output directly; do not translate its results into a
hand-written readiness manifest. The reviewer recomputes the raw file digest,
requires a fresh production-mode collection with the exact systemd and public
proxy inventory, rechecks the clean source/runtime/frontend identities, and
rejects missing, duplicate, failed, or unknown checks:

```bash
/opt/market-sentinel/.venv/bin/python /opt/market-sentinel/scripts/review_deployment_evidence.py \
  /var/lib/market-sentinel-deployment-evidence/deployment-evidence-<RELEASE_VERSION>.json \
  --expected-version "${RELEASE_VERSION}" \
  --expected-revision "${EXPECTED_SOURCE_REVISION}" \
  --json
```

The review is deliberately ineligible when the collector used
`--skip-systemd`, omitted `--public-url`, was re-reviewed after its freshness
window, or reported a backup that is no longer recent. Preserve the original
raw bytes for later attestation; changing whitespace also changes the bound
SHA-256 digest.

For score-eligible evidence, manually run the protected-main **Production
deployment evidence** workflow with the exact stable release tag and production
HTTPS origin. Its production-labeled self-hosted collector verifies the live
host; a separate GitHub-hosted job reviews the raw bytes, binds the exact
release SHA and frontend ZIP digest, and attests canonical
`deployment-evidence.json`. Download that final artifact and pass it with the
identical `--deployment-origin`. Raw reports, handwritten wrappers, and reviewer
summaries remain diagnostic-only. Do not use staging, generic self-hosted
runners, or placeholder origins for this workflow.
Configure `MARKET_SENTINEL_PRODUCTION_ORIGIN` as a protected `production`
environment variable. The workflow rejects an input that is not byte-for-byte
equal to that canonical public origin, rejects private or non-global resolution,
and completes this check before the collector job can access credentials. The
collector executes the verifier from the protected-main checkout with system
Python and passes `/opt/market-sentinel` only as the inspected deployment root;
it never executes a mutable verifier from the deployed checkout. Authenticated
public probes do not follow redirects. Raw evidence is nonce-bound to the exact
workflow SHA, run ID, and run attempt.

For a non-Linux or isolated local loopback smoke test only, add
`--skip-systemd`. This intentionally skips Linux systemd and filesystem
ownership checks while retaining versioned health and metrics validation; it is
not production-host evidence.

## Monitoring and recovery

- Health: `market-sentinel-health.timer` polls `GET /api/health` through
  loopback every minute using `scripts/verify_service_health.py`. Ship failures
  of `market-sentinel-health.service` from journald to the selected monitoring
  system and alert after two consecutive failed executions.
- Startup readiness: run `market-sentinel doctor --strict` against the service
  configuration and production frontend before each deployment and after each
  restore. It fails on corrupt configuration, unwritable storage, or missing
  dependencies, and also treats an armed live-trading configuration as a
  strict-mode failure for operator review.
- Logs: ship `journalctl -u market-sentinel-web` to the selected log system and
  alert on restart loops, authentication failures, failed safety preflights,
  and API rate-limit errors. Every completed HTTP request is emitted as one
  JSON log record with `timestamp`, `request_id`, `method`, path (without its
  query string), status, and duration. Use `request_id` when correlating an
  operator report with the reverse-proxy and service logs; it is also returned
  in the `X-Request-ID` response header.
- Metrics: the authenticated `/metrics` endpoint exposes bounded Prometheus
  counters for completed HTTP requests and request duration, plus current
  in-flight requests, overload rejections, and oversized-response rejections.
  It deliberately never uses request paths, wallets, query values, or
  credentials as labels.
  Caddy Basic Auth and the upstream API token protect this endpoint in the
  supplied deployment. Scrape it through the public proxy or a trusted
  loopback collector, and alert on sustained `5xx` responses, elevated request
  duration, sustained worker saturation or overload rejection, oversized
  responses, and an unexpected loss of request traffic.
- Backups: back up `/var/lib/market-sentinel` daily with encryption and tested
  retention. `market-sentinel-backup.timer` performs an integrity-manifested
  daily archive with 14 retained, cryptographically verified archive/manifest
  pairs. Orphaned and invalid entries remain visible for operator investigation
  but do not displace a restorable pair. The directory contains local
  configuration, paper records, the analytics cache, and redacted
  live-validation reports, decisions, and promotion snapshots. Do not back up
  `.env` files to shared or unencrypted storage.
- Restore drill: quarterly, select an archive from
  `/var/lib/market-sentinel-backups`, verify it, then restore it only into a
  brand-new path on an isolated host. The destination itself must not exist;
  create and permission its trusted parent in advance:

  ```bash
  /opt/market-sentinel/.venv/bin/python /opt/market-sentinel/scripts/restore_state_backup.py \
    --archive /var/lib/market-sentinel-backups/<archive>.tar.gz
  /opt/market-sentinel/.venv/bin/python /opt/market-sentinel/scripts/restore_state_backup.py \
    --archive /var/lib/market-sentinel-backups/<archive>.tar.gz \
    --destination /var/lib/market-sentinel-restore-drill
  ```

  The restore command rejects checksum mismatches, unsafe archive paths,
  compressed archives larger than 256 MiB, expanded archives larger than
  the 1 GiB file-payload limit plus strictly bounded tar headers, padding, and
  extension metadata, archives with more than 10,000 members, oversized
  cumulative PAX/GNU metadata, and every pre-existing
  destination (including an empty directory or file). The compressed and
  expanded limits can be lowered for a drill with `--max-archive-bytes` and
  `--max-bytes`; do not raise them without reviewing the expected backup size.
  The tool resolves an existing parent and atomically creates the final restore
  directory with private permissions, reducing final-component symlink races.
  Start the service loopback-only from the restored state, run the health
  check, and confirm no live trading is enabled by restored configuration.
- Configuration recovery: an existing malformed `config.json` now fails closed
  and is never silently replaced with defaults. Stop the service and use the
  restore command above to extract the most recent verified backup into a
  brand-new private sibling directory; the restore destination is a directory,
  never the `config.json` path itself. Run `market-sentinel doctor --config
  <new-directory>/config.json` and review that live trading remains disabled or
  intentionally configured. Preserve the malformed file in a root-only
  incident directory, install the verified recovered file through a temporary
  `0600` path with the service account's ownership, and atomically rename that
  temporary file over `/var/lib/market-sentinel/config.json`. Run the loopback
  health checks before restarting public access. Do not delete the damaged file
  until the restored configuration has been verified.

## Incident response

1. Set each affected market's `live_trading_kill_switch=true`, stop the
   service, and revoke exposed API credentials at the venue.
2. Preserve systemd logs and redacted live-validation reports; do not copy raw
   secrets into tickets or chat.
3. Rotate the reverse-proxy API token and operator password; validate service
   health before restoring read-only operation.
4. Create a GitHub private security advisory for product vulnerabilities.
5. For funded incidents, reconcile venue orders, fills, balances, and local
   audit output before considering any live re-enable request.

## Release acceptance

Before deploying a new release, verify its GitHub Actions run, checksum file,
SPDX SBOM, and build-provenance attestation. The release workflow rejects a tag
unless its target commit is already reachable from protected `main`; do not
publish from an unmerged feature branch. Confirm the release tag matches
`pyproject.toml`, install `requirements.lock`, and perform a staged loopback
deployment before public proxy cutover. Install `requirements-live.lock` only
where authenticated CLOB signing is explicitly approved.

### Funded production acceptance

Polymarket funded production acceptance is currently unavailable because live
mutations are blocked pending the reviewed CLOB V2 client/signing migration.
After that migration, acceptance would still require a current credentialed-read
report and a deliberately approved, capped order/cancel report with
post-cancel verification. Dry-run, browser-smoke, readiness-only, and legacy
V1 reports are not substitutes.
