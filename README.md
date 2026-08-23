<p align="center">
  <img src="assets/marketsentinel.svg" alt="MarketSentinel logo" width="112" />
</p>

# MarketSentinel

A local multi-market prediction-market command center for:
- **Price alerts** (token price triggers)
- **Wallet / username tracking** (monitor on-chain activity via Data API)
- **Copy-trading (optional)** (mirror trades with safety limits)

> ⚠️ Disclaimer  
> This is a developer MVP. It is **not financial advice** and it can lose money.  
> Only use each market in ways that comply with that market's terms and your local laws/regulations.
> The currently implemented Polymarket trading path performs a **geoblock check** and will refuse to trade if blocked.

## Features (what works today)

### 1) Price triggers
- Polymarket alerts subscribe to the CLOB WebSocket market channel
- Polymarket adapter-backed alert refresh reads CLOB last-trade, midpoint, best bid, and best ask state
- Other implemented adapters can load events/contracts from the selected market and poll official price endpoints for adapter-backed alerts
- Alerts support **last trade / last value**, midpoint, best bid, and best ask sources where the selected adapter exposes them
- The React Alerts tab can create, edit, enable/disable, delete, and refresh market-scoped alerts against the local Python API
- The React Alerts tab shows adapter-backed status plus current last/midpoint/bid/ask state for each alert

### 2) Username / wallet address tracker
- Paste a **0x wallet/proxyWallet** OR search a **Polymarket username/pseudonym**
- Polls the Data API `/activity` endpoint and alerts on new `TRADE` entries
- The React Wallets tab can add, edit, enable/disable, delete, and manually poll tracked wallets
- The React Wallets tab shows recent activity cached by the local API session, including simulation copy previews

### 3) Polymarket user analytics
- Search public Polymarket profiles by username/pseudonym and return proxy wallets for tracking or copy setup
- Load public leaderboard rows and rank them by PnL USD, volume USD, or computed ROI %
- The default ROI view returns the top 100 rows from 500 scanned public leaderboard rows; returned rows, scanned rows, and MDD scan rows have no local 1,000,000-row cap and accept `all`, `unlimited`, `0`, or `-1` for explicit no-cap scans
- An unlimited run reports an explicit completion reason: `end_of_results`, `repeated_page`, `scan_limit_reached`, or `cancelled`. Only `end_of_results` means the selected public leaderboard pagination ended; none of those outcomes proves that the endpoint contains every Polymarket account ever created, inactive, hidden, or omitted by upstream ranking rules.
- Min/max filters are available for PnL USD, volume USD, and ROI %
- MDD USD/% v2 can be computed from a public-data historical equity curve: closed-position realized PnL, public activity/trade capital basis, and the current open-position snapshot
- MDD v2 supports min/max filters, MDD sorting, pagination controls for closed positions/activity/trades/open positions, and an optional `equity_base_usd` override
- Optional `mdd_mode=mark_replay` replays trade-derived token inventory against public CLOB batch price history for deeper sampled unrealized drawdown checks
- Mark replay is capped to 20 asset ids per request, reports missing/clipped/unreconstructable rows, and falls back to MDD v2 when replay cannot be built
- Optional accounting snapshot reconciliation parses the public ZIP of CSVs, uses max equity as the strongest available MDD percentage base, and reports position/cash-flow gaps
- Optional audit caching stores bounded per-wallet MDD artifacts locally, reports retention/health metadata, supports targeted purge controls, and exposes JSON/CSV export links without rerunning expensive public API calls
- Leaderboard and MDD payloads report Polymarket rate-limit/backoff metadata instead of hiding upstream 429 failures as generic errors
- MDD payloads include assumptions and limitations because the public Data API does not expose a complete deposit/withdrawal ledger or historical unrealized mark replay
- The desktop Polymarket Analytics tab embeds top-ROI leaderboard search, uncapped returned/scanned row controls, optional MDD filters, result metrics, table review, and CSV export without opening the web UI
- The React Analytics tab also exposes user search, direct wallet MDD lookup/export, cached audit details, cache management, leaderboard sorting, and filters through the local Python API

### Polymarket official API coverage
- Official Polymarket docs checked on 2026-05-28: Gamma, Data, CLOB, Bridge, Relayer, and WebSocket surfaces are represented by local wrapper modules
- `polymarket.gamma` covers events, markets, tags, related tags, series, comments, sports metadata, teams, public search, and public profiles
- `polymarket.data_api` covers activity, positions, closed positions, trades, total value, traded markets, leaderboard, market positions, holders, open interest, live volume, accounting snapshot download, and builder analytics
- `polymarket.analytics_cache` stores bounded local MDD audit artifacts, lists health/retention metadata, purges selected or expired artifacts, and formats JSON/CSV exports for cached public analytics payloads
- `polymarket.clob_rest` covers public orderbook/pricing, price history, market parameters, CLOB market lists, rebates, public rewards, and builder trades
- `polymarket.trader` and `polymarket.clob_auth` cover guarded authenticated order placement, account order/fill recovery, fixed-endpoint order lookup/cancel flows, trades, order scoring, heartbeat, and authenticated rewards
- `polymarket.bridge` covers supported assets, deposit addresses, quotes, status, and withdrawal-address creation
- `polymarket.relayer` covers guarded relayer submit/query, nonce, relay payload, deployment check, recent transactions, and API key listing
- `polymarket.ws_market`, `polymarket.ws_user`, and `polymarket.ws_sports` cover market, authenticated user, and sports WebSocket channels
- `polymarket.endpoints` and `polymarket.http_client` centralize official endpoint metadata, auth tiers, documented batch caps, retry/rate-limit handling, typed Polymarket errors, and response normalization helpers used by the wrappers
- `polymarket.auth_readiness` and `GET /api/polymarket/clob-readiness` report redacted CLOB v2 readiness for private key, signature type, funder/deposit wallet, L1 headers, and L2 read-only REST headers without deriving credentials or placing orders
- `polymarket.credential_runbook` and `scripts/verify_polymarket_credentials.py` build a local no-network credential runbook with redacted environment inventory, exact operator commands, and no funded-action path
- `GET /api/polymarket/live-validation` reports the local Polymarket live-validation stage gates for public probes, credential readiness, authenticated reads, user WebSocket checks, bridge checks, and funded order/cancel status without running funded actions from the GUI/API
- `polymarket.live_reports` and `GET/POST/DELETE /api/polymarket/live-validation/reports` persist redacted local live-validation snapshots, import/export CLI JSON reports, open stored reports by key, and compare the latest two stage-gate summaries without exposing funded execution in the GUI/API
- `polymarket.mdd` builds historical MDD v2 payloads from public Data API closed positions, current positions, activity, and trades; it reports USD/% drawdown, capital-basis source, pagination limits, cache boundaries, assumptions, and limitations
- `polymarket.mdd` also exposes an opt-in CLOB mark-replay mode using `/batch-prices-history`; the default API mode remains fast MDD v2 to avoid heavy price-history calls during normal scans
- `polymarket.accounting` parses `/v1/accounting/snapshot` ZIP CSVs and can reconcile MDD payloads against equity, positions, deposits, withdrawals, and cash-flow gaps when explicitly requested
- The GUI exposes the high-level workflows used by this app; the broader official API surface is available to backend code and summarized through `GET /api/polymarket/coverage`
- Full live end-to-end validation of authenticated trading, user WebSocket, relayer, and funded wallet flows still requires real credentials, eligible region/KYC status, funded wallets, and explicit live-mode opt-in

Polymarket coverage is intentionally reported by verification tier, not as a single "implemented" flag:

| Tier | Meaning |
| --- | --- |
| `wrapper_available` | Local Python request helper exists for the documented surface. |
| `app_workflow_available` | Tkinter/API/React exposes a user workflow for that surface. |
| `offline_tested` | Unit tests cover request construction, parsing, and guardrails. |
| `public_live_verified` | Safe non-credentialed live probe passed from this machine. |
| `credential_live_verified` | Real credentialed read/stream verified. Currently blocked without credentials. |
| `funded_live_verified` | Funded order/cancel or fund-movement flow verified. Currently blocked without explicit credentials and live-action approval. |

Current truthful status: Gamma/Data/CLOB/Bridge probes are implemented and must be
re-run from the target network before claiming `public_live_verified`; the latest
local readiness audit could not reach the official endpoints because their TLS
connections were reset. Endpoint contracts are hardened offline against
documented paths, auth tiers, and batch caps; CLOB authentication readiness and
the credential runbook are validated locally with redacted payloads;
authenticated CLOB, user WebSocket, Relayer, Bridge address/fund movement, and
funded order/cancel verification remain blocked until credentials and explicit
live parameters are supplied.

Stored live-validation reports include a promotion guard before they can support production verification claims:

| Promotion tier | Required evidence |
| --- | --- |
| `credential_live_verified` | An actual `ok` non-destructive authenticated CLOB L2 order-list read, relayer authenticated read, or authenticated user WebSocket connection in `authenticated_read_checks`. A stage-gate boolean or credential runbook is not enough. |
| `funded_live_verified` | An `ok` funded order/cancel result with `live_action=true`, an order id, placed/cancel/post-cancel audit sections, and `post_cancel_verified=true`. Dry-run transcripts and `ready_to_execute` reports do not promote this tier. |

Reports with local-only modes such as GUI readiness snapshots, credential runbooks, or browser smoke fixtures are always blocked from promotion even if they contain simulated successful fields.

Imported live-validation reports are schema-checked before storage. Accepted modes are `strict_cli`, `local_readiness_only`, `credential_runbook_no_funded_actions`, `browser_smoke`, and `browser_smoke_seed`. Live-stage reports must include an object `stage_gates`; credential runbook reports must include `env_inventory`, `readiness`, `funded_execution_exposed=false`, and no network-call mode. Malformed `POST /api/polymarket/live-validation/reports` imports return HTTP 400 with `live_validation_report_schema_error` and structured `schema_validation` errors/warnings instead of writing a bad report. Stored reports also include a stable SHA-256 redacted payload hash plus source-file provenance when available. Duplicate imports skip storage by default while recording a duplicate audit event; operators can explicitly set `allow_duplicate=true` through the API/UI or `--allow-duplicate` in the replay CLI to preserve a second full audit entry. The React Live Safety report import panel shows the accepted-mode reference plus schema diagnostics from the last import/store/open action, and opened/exported reports preserve the same metadata. See `docs/POLYMARKET_LIVE_REPORT_SCHEMA.md` for the accepted shapes and deterministic valid/invalid fixture reports.

Existing report files can be replayed offline before import. The replay CLI validates one or more JSON reports, prints schema diagnostics plus guarded credential/funded promotion summaries, and never performs network or funded actions:

```powershell
python scripts/replay_polymarket_live_reports.py live-report.json live-auth-report.json
python scripts/replay_polymarket_live_reports.py --json live-report.json
python scripts/replay_polymarket_live_reports.py --import --label-prefix replay live-auth-report.json
python scripts/replay_polymarket_live_reports.py --import --allow-duplicate live-auth-report.json
```

`--import` stores only schema-valid reports through the redacted local report store; invalid files are reported and skipped. Duplicate redacted payload hashes are skipped by default with an audit event, unless `--allow-duplicate` is supplied. See `docs/POLYMARKET_LIVE_REPORT_REPLAY.md` for options and verification behavior.

Stored reports can also be exported as operator review bundles without exposing the raw report payload:

```powershell
curl http://127.0.0.1:8765/api/polymarket/live-validation/reports/<REPORT_KEY>/review.json
curl http://127.0.0.1:8765/api/polymarket/live-validation/reports/<REPORT_KEY>/review.md
```

The bundle combines schema status, redacted payload hash/provenance, duplicate history, guarded promotion evidence/blockers, source CLI commands, and coverage-tier mapping. It is evidence for human review only and keeps `static_coverage_mutated=false`; it does not promote credentialed or funded production verification by itself. See `docs/POLYMARKET_LIVE_REPORT_REVIEW_BUNDLE.md`.

Promotion decisions are recorded in a separate no-secrets ledger. Each decision requires
the report key, redacted payload hash, target tier, `accepted`/`rejected` decision,
reviewer note, and current review-bundle hash. Payload-hash or review-hash mismatches
fail closed, and blocked credential/funded tiers cannot be accepted without qualifying
review-bundle evidence:

```powershell
python scripts/review_polymarket_live_decisions.py --report-key <REPORT_KEY> --print-review-input
python scripts/review_polymarket_live_decisions.py --export-ledger --markdown
python scripts/review_polymarket_live_decisions.py --export-proposal --markdown
```

The ledger exports at `/api/polymarket/live-validation/decisions/export.json` and
`/api/polymarket/live-validation/decisions/export.md` keep `static_coverage_mutated=false`
and do not mutate coverage by themselves. See `docs/POLYMARKET_LIVE_REPORT_DECISION_LEDGER.md`.
Accepted decisions can also be exported as a no-automerge coverage/docs promotion
proposal at `/api/polymarket/live-validation/promotion-proposal/export.json` and
`/api/polymarket/live-validation/promotion-proposal/export.md`. The proposal detects
stale payload/review-bundle hashes, keeps `static_coverage_mutated=false`, and is only
input for a later human-authored patch. The React Live Safety tab includes a read-only
Promotion Proposal Preview with target-tier filtering, review gates, accepted/stale
counts, candidate/change tables, optional no-secrets proposal snapshot archive
controls, stale snapshot warnings, and no apply action. See
`docs/POLYMARKET_LIVE_REPORT_PROMOTION_PROPOSAL.md`.

Authenticated CLOB readiness follows the official Polymarket split between L1/L2 authentication and local order signing:

| Readiness item | Current behavior |
| --- | --- |
| SDK trading readiness | Requires a 0x-prefixed private key, supported signature type, official CLOB host, Polygon chain id 137, and a funder/deposit wallet when the signature type requires one. |
| Direct L2 read readiness | Requires all explicit `POLY_ADDRESS`, `POLY_API_KEY`, `POLY_PASSPHRASE`, `POLY_SIGNATURE`, and `POLY_TIMESTAMP` headers. |
| L1 REST readiness | Reports presence of `POLY_ADDRESS`, `POLY_SIGNATURE`, `POLY_TIMESTAMP`, and `POLY_NONCE`; it does not synthesize signatures. |
| Redaction | Private keys and signed headers are never returned by readiness payloads; addresses are shortened. |
| Live action boundary | Readiness never derives API credentials, submits orders, or moves funds. Funded checks remain behind `scripts/verify_polymarket_live.py` explicit flags. |

The credential runbook is the first local step before any credentialed live validation. It performs no network calls and only inventories whether required environment variables are present:

| Runbook group | Variables |
| --- | --- |
| SDK trading credentials | `POLYMARKET_PRIVATE_KEY` or `PRIVATE_KEY`; optional `POLYMARKET_SIGNATURE_TYPE` or `SIGNATURE_TYPE`; `POLYMARKET_FUNDER_ADDRESS`, `FUNDER_ADDRESS`, or `DEPOSIT_WALLET_ADDRESS` when the signature type requires a funder/deposit wallet. |
| Direct CLOB L2 reads | `POLY_ADDRESS`, `POLY_API_KEY`, `POLY_PASSPHRASE`, `POLY_SIGNATURE`, and `POLY_TIMESTAMP`. |
| CLOB L1 REST headers | `POLY_ADDRESS`, `POLY_SIGNATURE`, `POLY_TIMESTAMP`, and `POLY_NONCE`. |
| User WebSocket | `POLY_API_KEY`, `POLY_API_SECRET` or `POLY_SECRET`, and `POLY_PASSPHRASE`. |
| Relayer | `RELAYER_API_KEY` and `RELAYER_API_KEY_ADDRESS`. |
| Builder API | `POLY_BUILDER_API_KEY`, `POLY_BUILDER_TIMESTAMP`, `POLY_BUILDER_PASSPHRASE`, and `POLY_BUILDER_SIGNATURE`. |

Use it to create a redacted inventory report:

```powershell
python scripts/verify_polymarket_credentials.py --json --report-file polymarket-credential-runbook.json
python scripts/verify_polymarket_credentials.py --require-authenticated-read-ready
```

`--require-authenticated-read-ready` exits non-zero until at least one non-destructive authenticated read/stream candidate is locally ready. The runbook output includes exact follow-up commands for public readiness, credentialed reads, user WebSocket probing, dry-run order/cancel transcripts, and the separate funded order/cancel command. The funded command still requires explicit live flags, allow-listed token id, hard caps, maker-side orderbook preflight, and the exact confirmation text; the runbook itself cannot execute it.

The funded live order/cancel verifier is also disabled by default. Running
`python scripts/verify_polymarket_live.py --token-id <TOKEN> --side BUY --price <PRICE> --size <SIZE> --allow-token-id <TOKEN>`
returns a dry-run transcript. A real order/cancel verification requires all of the following: `--allow-funded-order`,
`--cancel-immediately`, an allow-listed token id, `--confirm-live-order-cancel I_UNDERSTAND_THIS_PLACES_A_REAL_POLYMARKET_ORDER`,
valid CLOB credentials, an eligible/funded account, a GTC order, size <= 5 shares, approximate notional <= 1 USDC, and a public
orderbook check proving the requested price is maker-side before placement. The harness immediately cancels the returned order id
and then fetches the order to verify it is no longer live.

For live credential validation, use the verifier as a stage gate and keep the JSON report:

```powershell
python scripts/verify_polymarket_live.py --report-file live-report.json
python scripts/verify_polymarket_live.py --require-authenticated-read-ok --include-user-websocket-connect --report-file live-auth-report.json
python scripts/verify_polymarket_live.py --token-id <TOKEN> --side BUY --price <PRICE> --size <SIZE> --allow-token-id <TOKEN> --report-file live-dry-run-report.json
```

`--require-authenticated-read-ok` fails unless at least one non-destructive authenticated read/stream check succeeds. `--include-user-websocket-connect` opens the authenticated user WebSocket and sends the subscription payload; secrets are not returned in the report. Use `--skip-public-checks` or `--skip-authenticated-read-checks` only for local readiness/debug runs, not for a production live approval.

### 4) Copy trading (paper mode by default)
- Follows a tracked wallet’s **BUY** trades (SELL optional, guarded) for a selected market with an official wallet-activity feed (Polymarket, Opinion Labs, Manifold, Myriad, and Hyperliquid HIP-4)
- Default mode is **SIMULATION** (logs what it *would* do)
- Copy sizing is a bounded **0..100%** setting; `0%` watches without copying and `100%` mirrors full detected size before max-USDC caps
- Multiple followed wallets are supported; the conflict guard skips duplicate or opposite-side same-token copies inside the guard window
- Enable **LIVE** mode only after the selected adapter live preflight settings are explicitly acknowledged; Opinion uses an optional official CLOB SDK signing path and remains off by default
- The React Wallets & Copy tab edits simulation-first copy settings and previews guarded live-copy preflight without placing orders

### 5) Adapter-backed paper trading
- Load an implemented market, select a contract, and submit dry-run paper orders through the selected adapter
- Refresh the selected contract's quote/orderbook preview before sizing a paper or preflighted live order
- Fill the order limit from the selected contract's current quote using side-aware bid/ask selection
- Summarizes local paper exposure by market and contract from accepted paper-order history
- Refreshes paper exposure marks and unrealized P&L from adapter price feeds without placing orders
- Refreshes a selected paper exposure mark without replacing other active marks
- Clears a selected paper exposure mark without dropping other active marks
- Shows aggregate paper exposure totals, marked count, and unrealized P&L above the exposure table
- Shows whether each paper exposure mark came from bid, ask, midpoint, or last trade
- Shows local mark refresh time per exposure row and the latest mark time in the summary
- Revalues marked paper P&L from the current local exposure whenever paper history changes
- Prunes cached paper marks when a contract no longer has open local paper exposure
- Clears transient paper marks without deleting local paper-order history
- Previews how the current paper order form would change the selected contract's local paper exposure
- Reload a selected paper-exposure row into the order form as a position-sized closing order
- Reload a selected paper-history row into the order form for repeat or adjusted dry-run orders
- Uses the adapter’s own validation and dry-run payload builder, including market-specific price/odds rules
- Stores local paper-order history in `data/config.json`

### 6) Central live trading safety
- Every implemented live adapter and the Polymarket copy-trading live path run the same preflight before an order can be posted
- Preflight requires `live_trading_enabled=true` and `live_trading_confirmed=true`, honors `live_trading_kill_switch`, and blocks orders above configured size/notional caps
- Preflight returns a redacted audit payload with contract, side, size, approximate notional, metadata key names, dry-run preview text, and region/KYC/credential warnings
- The React Live Safety tab edits the selected market's live gate and displays the redacted preflight audit without placing orders
- The Paper Trading tab can run **Preview Live Preflight** for the current order form without submitting a paper or live order

### 7) Market safety and credential diagnostics
- The Markets tab shows the selected adapter's health, enabled capabilities, configured credential environment variables, and detected credential sources without secret values
- The Markets and Live Safety tabs persist selected-market enablement, live enablement, live acknowledgement, kill switch, max size, and max notional settings
- Adapter-backed market search, alerts, paper actions, quote previews, and live preflight previews require the selected market to be enabled in local config

### 8) Kalshi adapter support
- Lists Kalshi events/contracts through official REST market-data endpoints
- Reads binary orderbooks and derives YES/NO best bid/ask prices
- Reads public trade history and normalized OHLCV candlesticks for each YES/NO outcome
- Reads signed account orders, fills, positions, settlements, balance, and queue positions through the documented portfolio endpoints; order/fill history can use the documented historical feeds. The CLI and `/api/markets/kalshi/account/{operation}` route expose only this explicit operation allow-list, validate query bounds, and never accept arbitrary authenticated paths. Guarded V2 `cancel_order`, `batch_cancel_orders`, `amend_order`, and `decrease_order` mutations use fixed signed paths and require a separate `kalshi_order_management_enabled` opt-in plus exact operator confirmation.
- Supports dry-run/paper orders; live orders are opt-in and require signed API credentials

### 8a) Predict.fun adapter support
- Lists markets and outcomes through Predict.fun's documented REST API, reads orderbooks and derived YES/NO prices, normalizes the public order-match feed as BUY/SELL trades, and normalizes the official point-based market timeseries feed as flat OHLC candles without fabricating volume.
- Exposes authenticated account, active-order, order-detail, activity, and position recovery through the CLI and `/api/markets/predict_fun/account/{operation}`; wallet-scoped positions validate a 20-byte address and private reads require `PREDICT_FUN_JWT`.
- Supports guarded signed order submission and opt-in relay-only removal by order id or hash. Removal requires `predict_fun_order_management_enabled`, the shared live-safety gates, JWT credentials, and exact operator confirmation; Predict.fun documents that removal does not invalidate orders on-chain.

### 8) Manifold adapter support
- Searches and lists Manifold markets through the official API
- Reads binary and multiple-choice probabilities for alerts
- Reads authenticated `/v0/me` account details plus `/v0/bets` active open-limit and historical bets with bounded contract, cursor, and timestamp filters
- Supports paper orders locally, guarded MANA betting, and one-bet open-limit cancellation through documented API-key auth; cancellation is disabled by default and requires the shared live-safety gates, a separate opt-in, and exact operator confirmation

### 9) Metaculus adapter support
- Lists authenticated Metaculus posts/questions through the official API
- Reads accessible binary, multiple-choice, and numeric forecast values for alerts
- Reads accessible Community Prediction aggregation history through `list_candles`; official irregular snapshots are normalized as point candles (no fabricated OHLCV or resampling)
- Does not expose trading controls because Metaculus is a forecasting platform, not a cash market

### 10) Good Judgment Open adapter support
- Lists Good Judgment Open questions and answer contracts through the documented Cultivate Forecasts REST API
- Reads answer probabilities and authenticated prediction-set history as irregular point candles without fabricated OHLCV
- Supports local forecast previews and guarded OAuth/Bearer forecast submission; this is a forecast update, not exchange execution
- Requires an operator-validated Good Judgment Open/Cultivate instance URL and account credentials; live submission remains disabled by default

### 11) Legacy Web3 protocol adapter support
- Lists Augur v2 markets/outcomes through a configured documented subgraph endpoint
- Reads Omen AMM marginal prices and Zeitgeist indexer asset prices for alerts
- Supports dry-run paper orders where reliable price data exists; Zeitgeist also exposes a guarded externally signed HybridRouter transaction boundary, off by default and without in-app signing or settlement

### 11) Additional official adapter support
- Reads Gemini Prediction Markets events/contracts, documented contract orderbooks, implied prices, irregular price-history points, and authenticated account recovery (active/history orders, current/settled positions, and event volume metrics) through official endpoints; history is exposed as flat candles without fabricated OHLCV resampling. The headless CLI exposes these private reads as `markets account <operation>` and the web API exposes `/api/markets/{market_id}/account/{operation}` for Gemini, Kalshi, Limitless, Opinion, and Hyperliquid; all use an explicit operation allow-list and never accept arbitrary authenticated paths. Gemini, Betfair, Kalshi, Limitless, Polymarket, Probable, and Hyperliquid expose separate guarded order-management CLI/API/React surfaces using only their documented fixed mutation endpoints; Gemini supports single and batch cancellation with a local 20-order cap, Limitless supports single, batch, and market-scoped cancellation, Probable composes fixed signed cancellation paths, and Hyperliquid forwards only complete externally signed cancel/modify/scheduled-cancel envelopes.
- Reads Myriad, Opinion, Predict.fun, XO, Betfair, and Limitless market data through their documented APIs; Betfair matched account orders are normalized as probability-priced trades, Myriad order-book matches are normalized as public trades, while Limitless historical YES prices are normalized as flat candles with complementary NO prices and finalized public market-event fills are normalized as trades. Limitless also exposes HMAC-authenticated portfolio positions, account history, and market-specific user orders as raw lossless payloads when approved token credentials and an optional delegated profile are configured. Xmarket exposes authenticated positions, user orders, market-scoped orders, and guarded fixed-path batch create/cancel mutations through its documented API-key endpoints.
- Opinion also has an optional official CLOB SDK path for guarded BNB-chain limit/market orders; all live trading stays off by default and requires explicit opt-in plus documented credentials or pre-signed order payloads

## Install & Run

Requires **Python >=3.10** with no artificial upper cap. Python **3.10** through **3.14** are required stable CI lanes today, and the moving latest stable **3.x** runner is included in CI/release checks so future stable Python releases are covered automatically when GitHub Actions publishes them.

### 1) Create a venv (recommended)
```bash
python -m venv .venv
source .venv/bin/activate  # (macOS/Linux)
# .venv\Scripts\activate  # (Windows)
```

### 2) Install deps
```bash
pip install --require-hashes -r requirements.lock
pip install --no-deps -e .
```

`requirements.lock` is the reviewed, hash-protected runtime dependency set.
For authenticated Polymarket CLOB signing and trading, install
`requirements-live.lock` after the runtime lock. For local verification, install
`requirements-test.lock` instead; it includes the live SDK plus `pytest` and
`coverage`. Distribution builds also need `requirements-build.lock`. Regenerate
the locks only as part of an intentional dependency update with
`python -m piptools compile --generate-hashes --strip-extras --output-file requirements.lock pyproject.toml`,
`python -m piptools compile --generate-hashes --strip-extras --output-file requirements-live.lock requirements-live.txt`,
`python -m piptools compile --generate-hashes --strip-extras --output-file requirements-test.lock requirements-test.txt`,
and `python -m piptools compile --generate-hashes --strip-extras --output-file requirements-build.lock requirements-build.txt`.
Compile the runtime and test locks with Python 3.10 so their conditional
`tomli` dependency remains represented for the minimum supported interpreter.

### 3) (Optional) set up LIVE trading credentials
Copy `.env.example` to `.env` and fill values:
```bash
cp .env.example .env
```

Configuration examples:
- `data/config.example.json` lists every supported market id, default enablement, and per-market settings.
- `data/config.json` is local state and is intentionally gitignored.
- Keep credentials in `.env` or your shell environment; config examples only reference env var names.

Platform support is tracked in `docs/PLATFORM_SUPPORT.md`. Windows, Ubuntu Linux, and macOS are CI-tested source platforms; Windows also has EXE/MSI release packages. BSD, Solaris, Android, and iOS are not marked fully supported until dedicated runners, packaging, and platform-specific smoke tests exist.

### 4) Start the GUI
```bash
python app.py
```

On Windows you can also double-click `run_gui.bat`. It uses `.venv` when it is healthy and falls back to the Python launcher.

The Tkinter app keeps the classic interface available and adds selectable UI designs from the top command bar:
- `Classic` preserves the older compact desktop styling.
- `Aurora 2026` is the default modern light/dark command-center design.
- `Graphite 2026` is a denser modern design with stronger contrast.
- `Sentinel 2027` is a flatter, roomier redesign with borderless panels, modern tabs, and higher-DPI spacing.

The Windows app and release packages include the same ICO/PNG icon assets for the title bar, taskbar, portable zip, and MSI install layout.

### 5) Optional React/TypeScript GUI
The existing Tkinter app remains available through `run_gui.bat` or `python app.py`. The React GUI is a parallel local interface backed by a stdlib Python API; it does not replace the Python GUI.

Windows launch scripts:
- `run_web_gui.bat` is the smart launcher. It starts the production React build when `frontend/dist/index.html` exists, starts the Vite dev server when `frontend/node_modules` exists, or prints the exact setup commands plus the Tkinter fallback.
- `run_web_gui_dev.bat` starts `web_api.py` on `127.0.0.1:8765`, sets `VITE_API_BASE_URL`, and runs `npm run dev` from `frontend`.
- `build_web_gui.bat` installs frontend dependencies when needed and runs `npm run build`.
- `run_web_gui_prod.bat` serves the built React app from `frontend/dist` through `web_api.py` at `http://127.0.0.1:8765`.

Manual development startup:
```bash
python web_api.py --host 127.0.0.1 --port 8765
cd frontend
npm install
npm run dev
```

Then open `http://127.0.0.1:5173`.

Manual production startup:
```bash
cd frontend
npm install
npm run build
cd ..
python web_api.py --host 127.0.0.1 --port 8765
```

Then open `http://127.0.0.1:8765`.

Headless CLI support for Linux, Windows, and servers:
```bash
python -m market_sentinel_cli polymarket-leaderboard \
  --sort roi_pct --direction DESC \
  --returned unlimited --scanned unlimited \
  --compute-mdd --fast-scan --mdd-scan unlimited --max-mdd-pct 20 \
  --scan-retry-attempts 10 --scan-retry-delay 60 \
  --state-db data/polymarket-best-roi-mdd20.sqlite3 --resume \
  --resume-on-failure --resume-backoff-seconds 60 \
  --format csv --output data/polymarket-best-roi-mdd20.csv
```

After package installation, the same command is available as `market-sentinel ...`. The CLI uses the same shared `data/config.json` file as the desktop and web UIs, and every command accepts `--config path/to/config.json` for isolated Linux/Windows automation.

Common full-app CLI commands:
```bash
market-sentinel health
market-sentinel doctor --strict
market-sentinel state
market-sentinel config set --theme dark --design sentinel_2027
market-sentinel markets list
market-sentinel markets set polymarket --enabled --live-trading-max-size 5
market-sentinel markets events --market kalshi --query election --limit 25
market-sentinel markets contracts --market kalshi EVENT_TICKER
market-sentinel markets price --market kalshi EVENT_TICKER:YES
market-sentinel markets orderbook --market kalshi EVENT_TICKER:YES
market-sentinel markets trades --market manifold MARKET_ID:YES --limit 100
market-sentinel markets candles --market myriad_markets MARKET_ID:YES --resolution 24h
market-sentinel markets account account_activity --market myriad_markets --wallet 0x... --limit 25
market-sentinel markets account active_orders --market polymarket --account-market-id CONDITION_ID --contract TOKEN_ID
market-sentinel markets account fills --market polymarket --contract TOKEN_ID --after UNIX --before UNIX
market-sentinel markets manage-orders cancel_order --market polymarket --order-id 0x... --json '{"confirm_order_management":"I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"}'
market-sentinel live-safety show --market polymarket
market-sentinel live-safety preflight --market polymarket --contract TOKEN --side BUY --size 1 --limit-price 0.50
market-sentinel alerts list
market-sentinel alerts add --market polymarket --contract TOKEN --direction above --threshold 0.65
market-sentinel wallets add --wallet 0x...
market-sentinel wallets poll --limit 25
market-sentinel wallets watch --interval 10
market-sentinel copy set --enabled --follow-wallet 0x... --copy-percentage 25 --max-usdc-per-trade 10 --no-live
market-sentinel copy preview --proxy-wallet 0x... --token-id TOKEN --side BUY --size 5 --price 0.42
market-sentinel paper show
market-sentinel paper quote --market polymarket --contract TOKEN
market-sentinel paper impact --market polymarket --contract TOKEN --side BUY --size 3 --limit-price 0.42
market-sentinel paper order --market polymarket --contract TOKEN --side BUY --size 3 --limit-price 0.42
market-sentinel dependencies
market-sentinel polymarket-user-search --query trader
market-sentinel polymarket-user-mdd --wallet 0x... --mode fast
market-sentinel polymarket-leaderboard-status --state-db data/polymarket-best-roi-mdd20.sqlite3 --pid-file polymarket-scan.pid
market-sentinel polymarket-leaderboard-export --state-db data/polymarket-best-roi-mdd20.sqlite3 --require-mdd --max-mdd-pct 20 --format csv --output data/polymarket-current-mdd20.csv
market-sentinel polymarket-readiness
market-sentinel polymarket-live-reports list
market-sentinel polymarket-live-reports import --report-file live-auth-report.json --label "authenticated read"
market-sentinel polymarket-live-reports review REPORT_KEY --format markdown --output review.md
market-sentinel polymarket-live-decisions list
market-sentinel polymarket-promotion-proposal snapshots list
market-sentinel paper marks refresh
market-sentinel paper marks clear-selected --market polymarket --contract TOKEN_ID
market-sentinel polymarket-mdd-cache list
market-sentinel serve --host 127.0.0.1 --port 8765 --frontend-dir frontend/dist
```

Commands that mutate config or paper state write through the same atomic config storage as the GUI. Most commands return JSON to stdout and support `--output file.json` plus `--compact`; `polymarket-leaderboard` can emit CSV or JSON. The read-only `markets events`, `contracts`, `price`, `orderbook`, `trades`, and `candles` commands use the same enabled-market settings and adapter configuration as the web API, normalize records to the shared schemas, and fail closed with a market-specific unsupported error when an official feed is not available. `trades` accepts optional Unix `--before`/`--after` bounds; `candles` accepts `--resolution`, `--from`, and `--to` bounds. `doctor` is read-only: it checks configuration integrity and storage access, installed dependencies, React build availability, and the selected market's live-safety state without printing secrets. Use `doctor --strict` in service deployment automation to fail on warnings such as an armed live-trading configuration; it always fails on corrupt configuration, unwritable storage, or missing dependencies. `paper marks` persists CLI-only computed marks in an atomic sidecar beside the selected config (or `--marks-file`) so refresh, show, and clear work across separate CLI processes; the file and its parent directory are synced on POSIX after replacement, it contains no credentials, and it is ignored by Git. `polymarket-live-reports`, `polymarket-live-decisions`, and `polymarket-promotion-proposal` expose the same local redacted report/review/decision/proposal artifacts as Live Safety; they never derive credentials, perform network actions, or place orders, and Markdown is available only for existing review exports. Unlimited scans run until the public leaderboard API returns no more rows, a repeated full page is detected, a rate limit stops the run, or the process is cancelled; use finite `--scanned` and `--mdd-scan` values for normal interactive jobs. For long VPS scans, use `--state-db path.sqlite3 --resume` plus retry flags: every fetched page, normalized row, and completed MDD audit is committed to SQLite, so a transient SSL/API failure only loses the current page batch and a later invocation resumes from the durable state. Add `--resume-on-failure` to keep a `nohup` scan alive after transient Polymarket HTTP/SSL failures; it resumes SQLite state after exponential backoff, with `--resume-max-restarts 0` meaning retry until interrupted. Run `market-sentinel polymarket-leaderboard-status --state-db path.sqlite3 --pid-file polymarket-scan.pid` at any time for a read-only JSON status with rows/pages, MDD done/error/pending counts, next offset, timestamps, stop reason, saved scan signature, and optional PID-file liveness. Use `market-sentinel polymarket-leaderboard-export --state-db path.sqlite3 --require-mdd --max-mdd-pct 20 --format csv --output current.csv` to write a sorted partial result snapshot without rerunning API or MDD calls; its JSON output identifies whether the export is still partial. A repeated page is recorded as `stop_reason=repeated_page`; it is an upstream pagination boundary, not proof that every Polymarket account exists in the public leaderboard. The CSV/JSON output is streamed from SQLite rather than rebuilding all rows in RAM. `--checkpoint` remains available as a lightweight JSONL checkpoint for shorter scans, but cannot be combined with `--state-db`. Progress logs on stderr include timestamp, PID, running status, elapsed time, phase, percent, scan rate, MDD rate, and ETA when a finite limit is known.
The `serve --frontend-dir` value is normalized and must remain beneath the
deployment resource root; this keeps the custom static build option useful
without allowing an arbitrary path to become an HTTP file root. `health`,
`state`, and `doctor` accept the same safe deployment-relative directory.

Keep long-scan state, exports, progress logs, PID files, and the default MDD
analytics cache under `data/`. The repository ignores those generated
operational artifacts (`data/*.sqlite*`, `data/*.jsonl`, `data/*.csv`,
`data/*.log`, `data/*.pid`, and the analytics cache) so they cannot be added to
a commit accidentally.

For a strict public-data ROI/MDD screen, `--max-mdd-pct 20` filters to successful public-data MDD calculations at or below 20%. Fast MDD is a public historical-equity approximation, not independently verified account-equity MDD: public deposits/withdrawals, unresolved historical marks, fees, and records outside the selected fetch windows can change the true result. Use `--mdd-mode mark_replay --mdd-include-accounting` for deeper sampled reconciliation, inspect the exported `mdd_method`, `mdd_pct_basis`, `mdd_source`, and warnings, and treat results as candidates for manual due diligence.

Useful local API endpoints:
- Predict.fun account operations are available at `GET /api/markets/predict_fun/account/{operation}` for `account`, `active_orders`, `order_detail`, `account_activity`, `positions`, and validated wallet-scoped `positions_by_address`; relay-only removal is available at `POST /api/markets/predict_fun/orders/{operation}` for `remove_orders` and `remove_orders_by_hash`, with JWT credentials, opt-in safety gates, and explicit non-on-chain-cancellation reporting.
- `GET /api/state` returns the initial React GUI snapshot: health, config, markets, alerts, wallets, copy, live safety, and paper state.
- `GET /api/health` returns API version, route metadata, React dev/build/prod commands, build availability, and confirms the Tkinter fallback remains `run_gui.bat` or `python app.py`.
- `PATCH /api/config` updates shared local config fields such as selected market, theme, and Tkinter UI design.
- `GET /api/markets` returns market capabilities, health, status text, credential source diagnostics without secret values, and live-safety settings.
- `GET /api/markets/{market_id}/events?query=...&limit=50` and `GET /api/markets/{market_id}/contracts?event_id=...` expose normalized official discovery feeds for enabled adapters.
- `GET /api/markets/{market_id}/price?contract_id=...` and `GET /api/markets/{market_id}/orderbook?contract_id=...` expose normalized quote/orderbook reads for enabled adapters.
- `GET /api/markets/{market_id}/trades?contract_id=...` and `GET /api/markets/{market_id}/candles?contract_id=...&resolution=1h` expose normalized history for adapters that document those feeds (currently Kalshi and its read-only distribution aliases, Hyperliquid HIP-4 wallet fills/candles, Manifold trades, Myriad trades/candles, Context V2 activity trades and binary price history, Opinion authenticated filled trades and price history, Gemini authenticated filled account trades and price history, Probable activity trades and price history, Polymarket price history, IBKR event-contract executions/candles, Betfair matched orders, Matchbook matched bets, SX Bet public trades, Space, Predict.fun public order matches plus timeseries candles, Iowa Electronic Markets archive candles, and SciCast Data Mart archive snapshots/trades; Myriad maps common resolutions to its official 24h/7d/30d/all price-chart buckets without resampling; IBKR execution history covers the current day and six previous days from the selected authorized account; Context V2, Gemini, Opinion, Polymarket, Probable, Predict.fun, and SciCast one-value-per-timestamp feeds are represented as flat OHLC points without fabricated volume; authenticated/account-scoped history requires the adapter's documented credentials and identity settings; Polymarket trade history still requires explicit operator-supplied CLOB L2 headers; IEM and SciCast history are archive-only); unsupported adapters fail closed with a structured error.
- `GET /api/markets/{market_id}/account/{operation}` exposes explicitly allow-listed authenticated recovery reads. Kalshi operations are `active_orders`, `order_history`, `fills`, `positions`, `settlements`, `balance`, and `queue_positions`; Gemini operations remain the documented order/position/volume reads; Limitless operations are `positions`, `account_history`, and `user_orders` (the latter requires a validated `market_slug`); Xmarket operations are `positions`, `user_orders`, and `market_orders` with bounded status/page/page-size filters and a validated market id; Opinion operations are `order_history`, `order_detail`, and wallet-scoped `positions` with bounded page/limit, numeric market/chain filters, status allow-listing, and a validated account wallet; Betfair exposes `active_orders`, `cleared_orders`, `funds`, `account`, `statement`, and `currency_rates` through the documented Exchange/Accounts JSON-RPC feeds with bounded status/order-by/sort/id/date filters, validated locale/currency values, and a validated wallet; Matchbook exposes authenticated `settled_bets`, `current_bets`, `current_offers`, `balance`, and `account` reads with bounded numeric-id, date, odds, side/status, interval, cancellation, and aggregation filters; Hyperliquid operations are `active_orders`, `order_history`, `positions`, `spot_balances`, `portfolio`, and `subaccounts` and use the configured `HYPERLIQUID_ACCOUNT_WALLET` (falling back to the trade/activity wallet) with an optional validated `dex` name; Polymarket exposes L2-authenticated `active_orders`, `order_detail`, and `fills` reads with validated order hashes, condition/token filters, cursors, and time bounds; Probable exposes L2-authenticated `open_orders` and `order` reads through fixed chain-scoped CLOB paths with signed query parameters; IBKR ForecastTrader, ForecastEx, and CME event contracts expose `orders` and `order_status` through the documented Client Portal account paths with an authorized session and account id; Manifold exposes `account`, `active_orders`, and `order_history` through authenticated `/v0/me` and `/v0/bets` calls, including documented `kinds=open-limit` and bounded cursor/time filters; Prophet Exchange exposes `balance` and bounded `transactions` reads through the documented `/v4/mm/get_balance` and `/v4/mm/get_transactions` wallet endpoints. Limitless accepts an optional validated `on_behalf_of` profile for delegated reads. Kalshi order/fill history accepts `historical=true` for the documented historical endpoints. Credentials, account eligibility, delegation scope, and any live trading remain operator-controlled external gates.
- `POST /api/markets/{market_id}/orders/{operation}` exposes only explicitly allow-listed Betfair (`cancel_orders`, `update_orders`, `replace_orders`), Kalshi (`cancel_order`, `batch_cancel_orders`, `amend_order`, `decrease_order`), Limitless (`cancel_order`, `batch_cancel_orders`, `cancel_all_orders`), Polymarket (`cancel_order`, `cancel_orders`, `cancel_all_orders`, `cancel_market_orders`), Probable (`cancel_order`, `cancel_orders`, `cancel_all_orders`), Hyperliquid (`cancel_order`, `cancel_orders`, `cancel_by_cloid`, `modify_order`, `batch_modify_orders`, `schedule_cancel`), Xmarket (`batch_create_orders`, `batch_cancel_orders`), IBKR event contracts (`cancel_order`, `cancel_all_orders`, `modify_order`), Manifold (`cancel_order`), and Prophet Exchange (`cancel_order`, `cancel_orders`) mutations. Requests require the venue-specific order-management opt-in, shared live-safety gates, bounded documented payloads, and explicit operator confirmation; Limitless uses fixed HMAC-signed paths and requires an exact market-scoped global-cancel confirmation, Polymarket mutations use fixed CLOB endpoints and L2 headers, Probable batch/global cancellation composes the documented fixed single-order DELETE path with bounded identities and exact global confirmation, Hyperliquid accepts only a complete externally signed `POST /exchange` envelope with HIP-4 asset validation and an exact scheduled-cancel confirmation, Xmarket uses fixed API-key-authenticated batch endpoints with bounded order schemas, IBKR uses fixed account-order paths with numeric order ids plus CME `manualIndicator`/`extOperator` compliance fields where required, Manifold uses the documented `POST /v0/bet/{id}/cancel` endpoint for one validated open-limit bet, and Prophet Exchange uses fixed Trading API cancel paths requiring the original `external_id` plus returned `order_id`. The route is POST-only and never accepts an arbitrary upstream method or path.
- `PATCH /api/markets/{market_id}` toggles a market and persists live-safety settings such as enablement, acknowledgement, kill switch, max size, and max notional.
- `GET /api/alerts` returns alert rows enriched with adapter-backed status and current in-memory price state.
- `POST /api/alerts` creates a market-scoped price alert after validating the selected adapter supports alerts.
- `PATCH /api/alerts/{alert_id}` edits alert fields or toggles alert enablement.
- `DELETE /api/alerts/{alert_id}` deletes an alert from local config.
- `POST /api/alerts/refresh` refreshes current prices for enabled alerts through adapter price feeds.
- `POST /api/alerts/{alert_id}/refresh` refreshes the selected alert's current price state.
- `GET /api/wallets` returns wallet watches, manual polling status, and recent wallet activity cached by the API session.
- `POST /api/wallets` creates a Polymarket wallet watch for a valid `0x` proxy wallet.
- `PATCH /api/wallets/{wallet_id}` edits wallet display name, enablement, or market-slug filter.
- `DELETE /api/wallets/{wallet_id}` deletes a wallet watch from local config.
- `POST /api/wallets/poll` polls enabled wallet watches once through the Polymarket Data API and updates dedupe state.
- `GET /api/polymarket/users/search?q=...` searches public Polymarket profiles and returns proxy-wallet candidates.
- `GET /api/polymarket/users/leaderboard` returns public leaderboard rows ranked by PnL USD, volume USD, computed ROI %, MDD USD, or MDD %, with min/max filters for PnL, volume, ROI, and MDD. `limit`, `scan_limit`, and `mdd_scan_limit` accept finite integers or explicit no-cap values (`all`, `unlimited`, `0`, `-1`) with no local 1,000,000-row cap; smaller values should be selected for normal interactive use. MDD scans accept `mdd_mode`, `mdd_history_limit`, `mdd_activity_limit`, `mdd_trade_limit`, `mdd_open_limit`, `mdd_mark_replay_token_limit`, `mdd_mark_replay_interval`, `mdd_mark_replay_fidelity`, `mdd_include_accounting`, `mdd_persist_cache`, and `mdd_cache_ttl_seconds`; payloads include `analytics_cache` and `rate_limit` metadata.
- `GET /api/polymarket/users/mdd?user=0x...` computes one wallet's MDD USD/% v2 from public closed positions, activity/trade capital basis, and the current open-position snapshot. It accepts `mode=fast` by default or `mode=mark_replay` for CLOB price-history inventory replay, plus `include_accounting_snapshot=true` for accounting ZIP reconciliation, `persist_cache=true`, `closed_limit`, `activity_limit`, `trade_limit`, `open_limit`, `include_open`, `max_points`, `equity_base_usd`, `mark_replay_token_limit`, `mark_replay_interval`, `mark_replay_fidelity`, and `cache_ttl_seconds`.
- `GET /api/polymarket/users/mdd/cache` lists cached MDD audit artifacts with wallet, MDD, age, TTL, expiry, size, and cache path metadata.
- `GET /api/polymarket/users/mdd/cache/health` returns cache path, size, entry counts, active/expired counts, TTL, and retention bounds for MDD audit artifacts.
- `POST /api/polymarket/users/mdd/cache/purge` purges selected keys, expired artifacts, or all MDD audit artifacts from the local analytics cache.
- `DELETE /api/polymarket/users/mdd/cache/{key}` purges one cached MDD audit artifact by cache key.
- `GET /api/polymarket/users/mdd/export.json?key=...` and `GET /api/polymarket/users/mdd/export.csv?key=...` return cached per-wallet MDD audit artifacts created by `persist_cache=true` or `mdd_persist_cache=true`.
- `GET /api/polymarket/coverage` returns the official Polymarket API coverage manifest and live-validation requirements.
- `GET /api/polymarket/live-validation` returns the current local Polymarket live-validation stage-gate report for the React Live Safety view.
- `GET /api/polymarket/live-validation/reports` lists stored redacted live-validation report snapshots and the latest-vs-previous stage-gate comparison.
- `GET /api/polymarket/live-validation/reports/{key}` opens one stored redacted live-validation report with metadata and payload.
- `GET /api/polymarket/live-validation/reports/{key}/export.json` downloads one stored redacted live-validation report as a JSON audit file.
- `GET /api/polymarket/live-validation/reports/{key}/review.json` downloads one sanitized promotion review bundle.
- `GET /api/polymarket/live-validation/reports/{key}/review.md` downloads the same review bundle as Markdown.
- `GET /api/polymarket/live-validation/decisions` lists the no-secrets promotion decision ledger.
- `GET /api/polymarket/live-validation/decisions/export.json` downloads the decision ledger as JSON.
- `GET /api/polymarket/live-validation/decisions/export.md` downloads the decision ledger as Markdown.
- `GET /api/polymarket/live-validation/promotion-proposal` builds a no-automerge coverage/docs proposal from accepted decisions.
- `GET /api/polymarket/live-validation/promotion-proposal/export.json` downloads the proposal as JSON.
- `GET /api/polymarket/live-validation/promotion-proposal/export.md` downloads the proposal as Markdown.
- `GET /api/polymarket/live-validation/promotion-proposal/snapshots` lists stored no-secrets proposal snapshots.
- `POST /api/polymarket/live-validation/promotion-proposal/snapshots` stores the current proposal as a bounded local snapshot.
- `GET /api/polymarket/live-validation/promotion-proposal/snapshots/{key}` opens one proposal snapshot with current-hash staleness metadata.
- `GET /api/polymarket/live-validation/promotion-proposal/snapshots/{key}/export.json` downloads one proposal snapshot as JSON.
- `GET /api/polymarket/live-validation/promotion-proposal/snapshots/{key}/export.md` downloads one proposal snapshot as Markdown.
- `GET /api/polymarket/live-validation/promotion-proposal/snapshots/{key}/diff.json` and `/diff.md` provide a no-secrets current-versus-snapshot diff summary for hashes, counts, decisions, proposed files, and review gates.
- `DELETE /api/polymarket/live-validation/promotion-proposal/snapshots/{key}` deletes one proposal snapshot.
- `POST /api/polymarket/live-validation/reports` stores the current GUI readiness snapshot or imports a CLI JSON report from `report_json`.
- `POST /api/polymarket/live-validation/decisions` records a review-bundle decision after validating report key, payload hash, target tier, decision, reviewer note, and review-bundle hash.
- `DELETE /api/polymarket/live-validation/reports/{key}` deletes one stored live-validation report snapshot.
- `GET /api/copy` returns copy-trading settings, tracked-wallet status, and live gate state.
- `PATCH /api/copy` updates simulation-first copy settings, including multiple followed wallets, bounded copy percentage (`0..100`), and conflict-guard settings.
- `POST /api/copy/preview` previews copy-trade sizing and guarded live preflight without placing orders.
- `GET /api/live-safety` returns selected-market live gate state, blockers, and redaction metadata.
- `POST /api/live-safety/preflight` runs the shared live-order preflight for the current order form and returns a redacted pass/block audit without placing orders.
- `POST /api/paper/quote` returns the selected contract quote and orderbook snapshot for the paper order form.
- `POST /api/paper/quote-limit` fills a side-aware paper limit price from the selected contract's quote.
- `POST /api/paper/preview-impact` previews local exposure impact before recording a paper order.
- `POST /api/paper/orders` submits an adapter-backed paper order and stores the local history record.
- `POST /api/paper/history/use` reloads a paper-history row into the order form.
- `POST /api/paper/history/clear` clears local paper-order history.
- `POST /api/paper/positions/use` reloads an exposure row into a close-sized order form.
- `POST /api/paper/marks/refresh` refreshes current marks and unrealized P&L for all open paper exposure.
- `POST /api/paper/marks/refresh-selected` refreshes only the selected exposure mark.
- `POST /api/paper/marks/clear` clears transient paper marks without deleting history.
- `POST /api/paper/marks/clear-selected` clears only the selected exposure mark.

API hardening:
- Error responses use `{ "ok": false, "error": { "code": "...", "message": "...", "status": 400 } }`.
- JSON mutation bodies must be objects and are rejected when malformed or larger than 1 MB.
- Internal server errors return a generic message rather than raw exception text.
- Credential-like keys in settings, diagnostics, and error details are redacted recursively.

## Market Capability Matrix

Thales Market now includes a guarded externally signed AMM transaction boundary. Live submission remains off by default and requires a reviewed network, configured AMM target, calldata/method, selected market/position, collateral approval, wallet signature, and settlement handling.

This matrix describes current application adapter support. Verified-blocked markets appear in the GUI and config, but their market-specific operations intentionally return clear unsupported-feature messages until official access, entitlements, or documented automation terms make support safe to add. Verified-blocked rows were checked against currently available official docs/pages. Robinhood Prediction Markets and Kalshi via Robinhood are now read-only aliases over Kalshi's official public market-data API; DraftKings Predictions is a read-only alias over the official Crypto.com/CDNA data surface. None of these aliases automate private brokerage/app accounts or live/copy execution. Space now uses its documented public REST contract for reads, orderbooks, public trades, candles, alerts, and paper orders; the official docs still say public production release timing will be announced separately. Hedgehog Markets now uses the official HPL Parimutuel/Eclipse `MarketV1` account contract for on-chain discovery, pooled prices, alerts, and dry-run `DepositV1` intents; CLOB depth, wallet-signed live execution, and copy trading remain unsupported. Frenzy Finance now uses the official Base `BetIntent`/`BetSettled` contract for explicitly configured price-range grid specs, settlement history, alerts, and dry-run EIP-712 intent previews; public live quotes/oracle acknowledgements, orderbooks, wallet signing, live execution, and copy trading remain unsupported.

Article 35 re-audited verified-blocked markets on 2026-05-26 and did not promote any blocked market. A 2026-07-15 follow-up promoted Crypto.com Predict/CDNA after Crypto.com published its official Predictions Market Data API. Context V2 was promoted after its current v2 API documentation was validated for market discovery, prices, orderbooks, externally signed orders, market activity trades, and binary price history; live use still requires a current API key, wallet setup, chain eligibility, and explicit safety gates. Context activity keeps the upstream outcome label (YES/NO) rather than guessing a BUY/SELL direction, and price history is normalized as flat OHLC points without fabricated volume. Smarkets was promoted after its current v3 REST contract was validated for event/market/contract discovery, quote orderbooks, and session-authenticated orders; API application approval, written data-use permission, account eligibility, and funded execution remain external gates. Thales Market is now implemented for its documented public AMM REST contract, including grouped/ungrouped market discovery, positional prices, buy-quote fallback, and paper orders; wallet signing, collateral, settlement, and live transactions remain disabled. MetaDAO is now implemented for its documented public Futarchy DEX `/api/tickers` contract, including DAO/token-pair discovery, bid/ask/last-price reads, and paper orders; depth, wallet signing, settlement, and live execution remain disabled. Seer is now implemented for its documented public `markets-search`/`get-market` API, including chain-qualified market discovery, outcome prices, alerts, and paper orders; depth, wallet signing, settlement, and live execution remain disabled. Hyperliquid is now implemented for the official HIP-4 `outcomeMeta` and `l2Book` contracts, including outcome discovery, encoded sides, prices, orderbooks, paper orders, and guarded externally signed exchange payloads. Trueo is now implemented for its official Base `TruthMarketManager`/`TruthMarket` contracts and documented Uniswap pools, including on-chain discovery, immutable market fields, AMM prices, alerts, paper orders, and guarded externally signed transactions; CLOB depth and copy trading remain unsupported. Zeitgeist SDK / Markets is now implemented as an explicit alias over the same documented Subsquid market/asset GraphQL contract, with separate configuration and fixture coverage. Zeitgeist Prediction Pools is now implemented as a pool-scoped alias over the documented market/pool/asset GraphQL schema, requiring a valid pool identifier before quotes or paper orders; pool settlement, wallet execution, and CLOB depth remain unsupported. Reality.eth Markets is now implemented as a read-only alias over the official Reality.eth question subgraph, with question discovery, response-option listing, lifecycle status, and alert-compatible metadata; prices, orderbooks, paper orders, and trading remain unsupported because Reality.eth is an oracle/question protocol. Drift BET is now implemented for the official public Data API BET prediction-record contract, with explicit configured-symbol inventory, bounded YES/NO price derivation, alerts, and dry-run orders; the Data API has no stable market-list route, while binary DLOB depth, wallet-signed live orders, settlement, and copy trading remain disabled. IBKR ForecastTrader, ForecastEx, and CME event contracts are now implemented through the official Client Portal Web API event-contract discovery, conid, snapshot, and order routes; an authorized brokerage session and account/data entitlements remain external gates. Xmarket was added as an implemented adapter after its documented API was mapped with offline fixtures. Probable was promoted after validating its official market/CLOB API contract and adding fixture-backed adapter coverage; live orders still require an externally signed order and explicit credentials. Matchbook was promoted after validating its current official event/market, session, and offer contracts with offline fixtures; live offers still require a Matchbook account session and explicit safety gates. DFlow was promoted after validating its official Metadata/Trade API, nested event markets, outcome mints, orderbook, and wallet-signed Solana transaction flow with offline fixtures; live submission still requires API credentials, a Proof-eligible wallet, a signed transaction, and a configured Solana RPC. Fanatics Markets was subsequently promoted as a read-only alias over CDNA's official Predictions API after Fanatics documented that its intermediary product lists and prices event contracts on CDNA; Fanatics-specific orderbook, live, and copy APIs remain unsupported. FanDuel Predicts is now represented by a fixture-backed read-only alias over the official Crypto.com OG/CDNA Predictions API for the FanDuel-listed OG portion; CME-listed contracts remain covered by the CME/IBKR adapter, while FanDuel account execution and copy APIs remain unsupported. Coinbase Prediction Markets was subsequently promoted as a read-only alias over the official Kalshi venue after Coinbase documented that prediction-market flow comes from Kalshi and links users to Kalshi market outcomes; Coinbase-specific live and copy APIs remain unsupported. Robinhood Prediction Markets and Kalshi via Robinhood are now represented as fixture-backed read-only Kalshi aliases, and DraftKings Predictions is represented as a fixture-backed read-only CDNA alias; private brokerage/app endpoints, live execution, and copy trading remain unsupported. The catalog includes the requested platform names, including separate rows for distribution and protocol variants whose access contracts differ.

Hedgehog Markets was promoted on 2026-08-17 after validating the official HPL Parimutuel/Eclipse `MarketV1` custom-Borsh account layout and public Eclipse JSON-RPC contract. The adapter supports on-chain discovery, pooled outcome probabilities, alerts, dry-run `DepositV1` intents, and a guarded submission boundary for externally signed `DepositV1` transactions; CLOB depth, settlement, and copy trading remain unsupported, while live submission stays off by default. Manifold wallet tracking now uses the official public `/v0/bets?username=...` feed with a safe `manifold:<username>` identity and simulation-first copy intents; the same documented `/v0/bets?contractId=...` feed now provides normalized per-fill trade history with timestamp filters. Live MANA betting and CLOB orderbooks remain separately guarded or unsupported. Myriad wallet tracking now uses the official public `GET /users/:address/events` feed, normalizes buy/sell collateral-versus-share semantics, and emits simulation-first copy intents; live order submission remains guarded behind signed orders.

Frenzy Finance was promoted on 2026-08-17 after validating the official Base/Base Sepolia contract deployments, `BetSettled` log shape, and fixture-backed `BetIntent` preview path. The adapter supports configured grid discovery, historical settlement reads, alerts, and dry-run EIP-712 intents; the active quote/oracle acknowledgement, wallet signing, orderbook, live execution, and copy-trading paths remain fail-closed.

Nadex is represented by a fixture-backed read-only alias over the official Crypto.com Predictions API used for the CDNA/Nadex prediction-event data surface. Nadex account trading, DCM/FIX depth, knock-out products, and copy trading remain unsupported until a documented public automation contract is available.

Blinq is represented by a fixture-backed read-only alias over the official Polymarket data surface because Blinq's product page explicitly says it trades Polymarket markets. Blinq leverage, deposits, private account actions, live wallet execution, and copy trading remain unsupported until Blinq publishes a separate documented automation contract.

| Market | Adapter | Alerts | Read-only data | Paper trading | Live trading | Copy trading | API required | Credentials required | Region/KYC limitation |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Polymarket (`polymarket`) | Implemented | Yes | Yes | Yes | Guarded, off by default | Yes, dry-run default | Yes | Live trading only | Trading may be region/KYC limited |
| Kalshi (`kalshi`) | Implemented | Yes | Yes | Yes | Guarded, off by default | No | Required | Exchange account/API keys | Region/KYC limited |
| PredictIt (`predictit`) | Implemented | Yes | Yes | Yes | No | No | Required | No | Region/account limited |
| Robinhood Prediction Markets (`robinhood_prediction_markets`) | Implemented | Yes | Yes (including trades/candles) | Yes | No | No | Required | No | Region/KYC limited |
| Fanatics Markets (`fanatics_markets`) | Implemented | Yes | Yes | Yes | No | No | Required | Optional API key | Region/KYC limited |
| DraftKings Predictions (`draftkings_predictions`) | Implemented | Yes | Yes | Yes | No | No | Required | Optional API key | Region/KYC limited |
| Interactive Brokers ForecastTrader / IBKR Prediction Markets (`ibkr_forecasttrader`) | Implemented | Yes | Yes (executions/OHLC/account order reads) | Yes | Guarded, off by default (cancel/modify also guarded) | No | Required | IBKR account required | Region/KYC limited |
| ForecastEx (`forecastex`) | Implemented | Yes | Yes (executions/OHLC/account order reads) | Yes | Guarded, off by default (cancel/modify also guarded) | No | Required | IBKR account required | Region/KYC limited |
| CME Group Prediction Markets (`cme_prediction_markets`) | Implemented | Yes | Yes (executions/OHLC/account order reads) | Yes | Guarded, off by default (cancel/modify also guarded) | No | Required | IBKR account required | Region/KYC limited |
| Nadex (`nadex`) | Implemented | Yes | Yes | Yes | No | No | Required | Optional API key | Region/KYC limited |
| Crypto.com Predict / CDNA (`crypto_com_predict`) | Implemented | Yes | Yes | Yes | No | No | Required | Optional API key | Not KYC limited |
| Hyperliquid (`hyperliquid`) | Implemented | Yes | Yes (HIP-4 wallet fills/candles) | Yes | Guarded, off by default; signed cancel/cancel-by-cloid/modify/batch-modify/schedule-cancel also guarded | Yes (HIP-4 wallet fills; simulation-first) | Required | No API key for reads; externally signed wallet payload required for live orders | Jurisdiction varies |
| Myriad Markets (`myriad_markets`) | Implemented | Yes | Yes (trades/candles/account activity) | Yes | Guarded, off by default; signed cancel/batch-cancel/cancel-all/batch-modify also guarded | Yes, simulation-first | Required | API credentials required | Jurisdiction varies |
| Context V2 (`context_v2`) | Implemented | Yes | Yes (activity trades/price history) | Yes | Guarded, off by default | No | Required | API credentials required | Region/KYC limited |
| Frenzy Finance (`frenzy_finance`) | Implemented | Yes | Yes | Yes | No (oracle/wallet gate) | No | Required | No API key; wallet/collateral required only for future live chain flow | Jurisdiction varies |
| XO Market (`xo_market`) | Implemented | Yes | Yes | Yes | Guarded, off by default | No | Required | API credentials required | Region/KYC limited |
| Manifold Markets (`manifold`) | Implemented | Yes | Yes (probabilities/trades/account reads) | Yes | Guarded, off by default (bet placement/cancel) | Yes, simulation-first | Required | Optional API key | Not KYC limited |
| Metaculus (`metaculus`) | Implemented | Yes | Yes (forecast snapshots) | No | No | No | Required | Account/API token required | Not trading/KYC limited |
| SciCast (`scicast`) | Implemented | Yes | Yes (archive snapshots/trades) | Yes (local dry-run) | No | No | Required | API key required | Not trading/KYC limited |
| Good Judgment Open (`good_judgment_open`) | Implemented | Yes | Yes (forecast probabilities/history) | Yes (local preview) | Guarded, off by default | No | Required | Account/API token required | Region/account limited |
| Hypermind (`hypermind`) | Verified blocked | No | No | No | No | No | Required | Program access required | Program access limited |
| Iowa Electronic Markets (`iowa_electronic_markets`) | Implemented | Yes | Yes | Yes | No | No | Required | Not required | Not trading/KYC limited |
| INFER / INFER-pub (`infer`) | Verified blocked | No | No | No | No | No | Required | Account/export access required | Not trading/KYC limited |
| Fact Machine (`fact_machine`) | Verified blocked | No | No | No | No | No | Required | Wallet/personhood required | Identity/jurisdiction limited |
| Opinion Labs (`opinion_labs`) | Implemented | Yes | Yes (price history, authenticated filled trades/account reads) | Yes | Guarded, off by default (orders and cancel/batch/global cancellation) | Yes, simulation only | Required | API credentials required | Jurisdiction varies |
| Gemini Titan / Gemini Predictions (`gemini_titan`) | Implemented | Yes | Yes (filled account trades/price-history points) | Yes | Guarded, off by default (orders and single/batch cancellation) | No | Required | Live trading only | Region/KYC limited |
| Augur (`augur`) | Implemented | No | Yes | No | No | No | Required | Subgraph endpoint required | Jurisdiction varies |
| BetMGM (`betmgm`) | Implemented | Yes | Yes | Yes | No | No | Required | Account/API token required | Region/KYC limited |
| PrizePicks (`prizepicks`) | Verified blocked | No | No | No | No | No | Required | Account required | Region/KYC limited |
| Underdog Sports (`underdog_sports`) | Verified blocked | No | No | No | No | No | Required | Account required | Region/KYC limited |
| Drift BET (`drift_bet`) | Implemented | Yes | Yes | Yes | No | No | Required | No API key; wallet/collateral required only for future live chain flow | Jurisdiction varies |
| Thales Market (`thales_market`) | Implemented | Yes | Yes | Yes | Guarded, off by default | No | Required | No API key; externally signed wallet transaction required for live orders | Jurisdiction varies |
| Hedgehog Markets (`hedgehog_markets`) | Implemented | Yes | Yes | Yes | Guarded, off by default | No | Required | No API key; externally signed wallet transaction required for live orders | Jurisdiction varies |
| Omen (`omen`) | Implemented | Yes | Yes | Yes | Guarded, off by default | No | Required | Subgraph endpoint required | Jurisdiction varies |
| Zeitgeist (`zeitgeist`) | Implemented | Yes | Yes | Yes | Guarded, off by default | No | Required | No API key for reads; externally signed wallet payload required for live orders | Jurisdiction varies |
| Zeitgeist SDK / Markets (`zeitgeist_sdk_markets`) | Implemented | Yes | Yes | Yes | Guarded, off by default | No | Required | No API key for reads; externally signed wallet payload required for live orders | Jurisdiction varies |
| Azuro (`azuro`) | Implemented | Yes (bettor bet history) | Yes | Yes | Guarded, off by default | No | Required | Live signed orders only | Jurisdiction varies |
| SX Bet / SX Network (`sx_bet`) | Implemented | Yes | Yes | Yes (public trades) | Guarded, off by default | No | Required | Live/WebSocket only | Jurisdiction varies |
| Limitless Exchange (`limitless_exchange`) | Implemented | Yes | Yes | Yes | Guarded place/cancel/batch/market-cancel, off by default | No | Required | Account/API token required | Jurisdiction varies |
| Predict.fun (`predict_fun`) | Implemented | Yes (public matches/timeseries) | Yes (account/orders/activity/positions) | Yes | Guarded place/relay-remove, off by default | No | Required | API key; JWT for private reads/removal | Jurisdiction varies |
| Smarkets (`smarkets`) | Implemented | Yes | Yes (authenticated orders/account) | Yes | Guarded place/cancel, off by default | No | Required | Exchange account/API keys | Region/KYC limited |
| Betfair Exchange (`betfair_exchange`) | Implemented | Yes | Yes (current/cleared account orders, funds, account details, statements, currency rates) | Yes | Guarded place/cancel/update/replace, off by default | No | Required | Exchange account/API keys | Region/KYC limited |
| Probo (`probo`) | Verified blocked | No | No | No | No | No | Required | Account required | Region limited |
| Coinbase Prediction Markets (`coinbase_prediction_markets`) | Implemented | Yes | Yes | Yes | No | No | Required | No | Region/KYC limited |
| Probable (`probable`) | Implemented | Yes | Yes (activity/price history; authenticated open-order reads) | Yes | Guarded, off by default | No | Required | API credentials required | Jurisdiction varies |
| Kalshi via Robinhood (`kalshi_via_robinhood`) | Implemented | Yes | Yes (including trades/candles) | Yes | No | No | Required | No | Region/KYC limited |
| FanDuel Predicts (`fanduel_predicts`) | Implemented | Yes | Yes | Yes | No | No | Required | Account required for trading | Region/KYC limited |
| Seer (`seer`) | Implemented | Yes | Yes | Yes | Guarded, off by default | No | Required | No API key; externally signed wallet transaction required for live orders | Jurisdiction varies |
| DFlow (`dflow`) | Implemented | Yes | Yes | Yes | Guarded, off by default | No | Required | Wallet required for trading | Region limited |
| Space (`space`) | Implemented | Yes | Yes (including public trades/candles) | Yes | No | No | Required | No API key; wallet/settlement required only for future live chain flow | Jurisdiction varies |
| Xmarket (`xmarket`) | Implemented | Yes (positions/orders) | Yes | Yes | Guarded, off by default (single and batch create; batch cancel) | No | Required | API credentials required | Jurisdiction varies |
| Trueo (`trueo`) | Implemented | Yes | Yes | Yes | Guarded, off by default | No | Required | No API key; externally signed wallet transaction required for live orders | Jurisdiction varies |
| PRDT Finance (`prdt_finance`) | Implemented | Yes | Yes | Yes | No | No | Required | No API key; wallet required only for future live chain flow | Jurisdiction varies |
| SynStation (`synstation`) | Verified blocked | No | No | No | No | No | Required | API credentials required | Jurisdiction varies |
| Gnosis Prediction Markets (`gnosis_prediction_markets`) | Implemented | Yes | Yes | Yes | Guarded, off by default | No | Required | Subgraph endpoint required | Jurisdiction varies |
| MetaDAO (`metadao`) | Implemented | Yes | Yes | Yes | Guarded, off by default | No | Required | No API key; externally signed wallet transaction required for live orders | Jurisdiction varies |
| Levr Bet (`levr_bet`) | Verified blocked | No | No | No | No | No | Required | Account required | Jurisdiction varies |
| Dexsport (`dexsport`) | Verified blocked | No | No | No | No | No | Required | Wallet required for trading | Jurisdiction varies |
| Lamas Finance (`lamas_finance`) | Implemented | Yes | Yes | Yes | Guarded, off by default | No | Required | Solana RPC; externally signed wallet transaction required for live orders | Devnet example; production deployment must be reviewed |
| Zetarium World (`zetarium_world`) | Implemented | Yes | Yes | Yes | Guarded, off by default | No | Required | No API key; externally signed wallet transaction required for live orders | Jurisdiction varies |
| Blinq (`blinq`) | Implemented | Yes | Yes | Yes | No | No | Required | No | Region limited |
| Zeitgeist Prediction Pools (`zeitgeist_prediction_pools`) | Implemented | Yes | Yes | Yes | Guarded, off by default | No | Required | No API key for reads; externally signed wallet payload required for live orders | Jurisdiction varies |
| Reality.eth Markets (`reality_eth_markets`) | Implemented | Yes | Yes | No | No | No | Required | Subgraph endpoint required | Identity/jurisdiction limited |
| SportsTrade (`sportstrade`) | Verified blocked | No | No | No | No | No | Required | Account required | Region/KYC limited |
| Prophet Exchange (`prophet_exchange`) | Implemented | Yes | Yes | Yes | Guarded, off by default | No | Required | API credentials required | Region/KYC limited |
| Sporttrade Prediction / Exchange Products (`sporttrade_products`) | Verified blocked | No | No | No | No | No | Required | Account required | Region/KYC limited |
| Matchbook (`matchbook`) | Implemented | Yes | Yes | Yes (matched, settled/current bets) | Guarded, off by default; cancel/edit offer mutations are separately opt-in | No | Required | Exchange account/API keys | Region/KYC limited |
| Meta Arena (`meta_arena`) | Verified blocked | No | No | No | No | No | Required | Account required | Jurisdiction varies |

## Verification

Project-level checks:
```bash
python verify.py
```

This runs:
- Python version check (`>=3.10`, with no artificial upper cap)
- dependency import checks
- `pip check`
- `compileall`
- adapter catalog, config example, README matrix, and blocker documentation checks
- offline fixture JSON checks
- GUI market selector integration checks
- Windows launch UX checks
- Tkinter fallback smoke checks
- frontend build readiness checks; the build is skipped unless `frontend/node_modules` exists
- optional Live Safety report-history browser smoke checks when `--frontend-live-smoke` is supplied
- offline unit tests for config/storage, API wrapper parsing, alert crossing, copy-trade percentage sizing, and wallet activity de-duplication
- enforced branch-coverage floors of 65% across the full Python application and 74% across the headless/backend surface; `python verify.py` fails when either floor regresses

Install `requirements-test.lock` before running the pytest suite:
```bash
python -m pip install --require-hashes -r requirements-test.lock
python -m pytest
```

Tkinter fallback smoke check:
```bash
python app.py --smoke-test
```

Real Tkinter lifecycle smoke check (requires a display server; CI uses Xvfb):
```bash
python app.py --gui-smoke-test
```

This constructs the full desktop widget tree, verifies the tab contract, and
closes it without starting network background workers.

React frontend checks:
```bash
cd frontend
npm install
npm run build
```

Strict final frontend verification:
```bash
python verify.py --frontend-build
```

Strict frontend verification plus Live Safety report-history browser smoke:
```bash
python verify.py --frontend-build --frontend-live-smoke
```

The browser smoke uses temporary config/report files, seeds a redacted local Polymarket validation report, checks the built React Live Safety route with a local Chromium/Edge headless browser, and exercises stored report open/export routes without credentials or funded actions. If a browser is not auto-detected, set `PREDICTION_MARKET_BROWSER_PATH` or run the direct script with `--browser-path`.

If `frontend/node_modules` is missing, the normal verifier records frontend build readiness and skips the build. The strict command above fails until `npm install` or `build_web_gui.bat` has completed successfully.

## CI/CD and Releases

For a conservative, reproducible production-readiness score, run:

```bash
python scripts/check_product_readiness.py \
  --full-local \
  --run-public-live \
  --json
```

The scorer keeps repository verification separate from real deployment,
platform, GitHub-settings, credentialed Polymarket, and funded-operation
evidence. See [`docs/PRODUCTION_READINESS.md`](docs/PRODUCTION_READINESS.md)
for the 100-point model and reviewed evidence-manifest contract. A green CI
matrix is not treated as proof that an external runner or production host has
completed its required checks.

GitHub Actions workflows live under `.github/workflows`:
- `ci.yml` runs Python verification across Ubuntu, macOS `14`/`15`/`26`, and hosted Windows with Python `3.10` through `3.14`, runs a moving latest stable `3.x` compatibility lane for future Python releases, constructs and closes the real Tkinter widget tree under Ubuntu Xvfb, smoke checks RHEL UBI 8/9/10, a RHEL 7-era manylinux2014 ABI container, Rocky Linux 8/9/10, hosted Windows 11 ARM with Python `3.12` x64 dependency wheels, mobile web profiles for Android 14/15/16 and iOS 15/16/18/26, includes an opt-in self-hosted Windows 10 job gated by `ENABLE_WINDOWS_10_SELF_HOSTED=true`, builds the React frontend with Node.js `24`, and builds Python distributions.
- `security.yml` runs CodeQL analysis and requires dependency review on pull requests once the repository dependency graph is enabled.
- `release.yml` publishes tagged releases (`v*.*.*`) with Python package artifacts, a zipped React production bundle, Windows x64 portable/installer packages, SHA256 checksums, an SPDX SBOM, and GitHub build-provenance attestations. Local verification rejects reusing an existing release tag from a newer commit and requires an untagged project version to be newer than the latest tag.

Dependabot is configured in `.github/dependabot.yml` for GitHub Actions, Python, and frontend npm dependency updates.

See `docs/CI_CD.md` for the release process, recommended branch protection, release environment setup, and strict frontend build verification. See `docs/PRODUCTION_OPERATIONS.md` for hardened Linux deployment and `docs/REPOSITORY_SETTINGS.md` for required GitHub controls.

In-app checks:
- **About -> Check versions** compares installed dependency versions with PyPI.
- **Copy Trading -> Check Geoblock** verifies whether live trading should be blocked for the current location.
- **Markets** displays selected-adapter health and edits live safety gates without touching credentials.
- Disabled markets remain visible but adapter-backed actions are blocked until enabled in **Markets** or **Live Safety**.
- **Live Safety** displays selected-market live gate status, blockers, max caps, acknowledgement, redacted preflight audits, the Polymarket live-validation stage-gate report, and local redacted report history/import/open/export/compare controls.
- **Alerts** creates, edits, toggles, deletes, and refreshes market-scoped price alerts.
- **Alerts** exposes last trade, midpoint, best bid, and best ask source selection for each alert.
- **Alerts -> Refresh Prices** polls adapter-backed current price state and updates trigger status without placing orders.
- **Wallets & Copy** manages tracked wallets for markets with an official activity feed (Polymarket, Opinion Labs, Manifold, Myriad, and Hyperliquid HIP-4); Polymarket also supports username/profile search, while Manifold requires `manifold:<username>` and the other feeds require a 0x wallet address.
- **Wallets & Copy** shows recent wallet activity with the copy-trading simulation or skip reason for each item.
- **Wallets & Copy** edits followed wallets, copy percentage, max USDC, slippage, live mode, SELL-copy permission, and the same-token conflict guard.
- **Wallets & Copy -> Preview** runs the live-copy preflight gate for a sample activity and does not place an order.
- **Analytics** searches public Polymarket profiles and loads leaderboard rows by ROI %, PnL USD, volume USD, MDD USD, or MDD %.
- **Analytics** computes MDD USD/% on demand from closed-position realized PnL plus current open-position PnL.
- **Analytics** can run the same leaderboard/MDD scanner headlessly through `python -m market_sentinel_cli polymarket-leaderboard`, including unlimited returned/scanned/MDD-scan settings for Linux batch jobs.
- **Analytics** can compute a single wallet's MDD directly, use a profile-search wallet as input, and inspect cached audit detail without rerunning the public API calls.
- **Analytics** can persist MDD audit artifacts, inspect cache health/retention, purge selected/expired/all cache entries, and download artifacts as JSON or CSV.
- **Paper Trading -> Refresh Quote** previews the selected contract's current adapter quote/orderbook without placing an order.
- **Paper Trading -> Use Quote Limit** fills the limit field from best ask for BUY/BACK and best bid for SELL/LAY where available.
- **Paper Trading** keeps a local paper exposure summary above the order-history table.
- **Paper Trading -> Refresh Marks** marks paper exposure against current adapter prices and shows unrealized P&L.
- **Paper Trading -> Refresh Selected Mark** marks only the selected exposure row and preserves other active marks.
- **Paper Trading -> Clear Selected Mark** clears only the selected exposure row's mark and preserves other active marks.
- **Paper Trading** totals gross size, entry notional, marked rows, and aggregate unrealized P&L above the exposure table.
- **Paper Trading** shows mark source per exposure row and aggregates mark-source counts in the summary.
- **Paper Trading** shows mark time per exposure row and the latest mark time in the summary.
- **Paper Trading** recomputes marked P&L from the latest local paper exposure after new or cleared history.
- **Paper Trading** drops cached marks for contracts that no longer appear in the local exposure table.
- **Paper Trading -> Clear Marks** clears mark price/source/time/P&L while preserving paper history.
- **Paper Trading -> Preview Impact** shows current net, order net, projected net, effect, and projected notional before recording a paper order.
- **Paper Trading -> Use Position** loads the selected exposure row into a close-sized order form and clears the limit for a fresh quote.
- **Paper Trading -> Use History Order** reloads the selected paper-history row into the order form without placing an order.
- **Paper Trading -> Preview Live Preflight** validates a proposed order against the live safety gate without posting it.
- Form validation guards wallet addresses, alert thresholds, and copy-trading numeric settings.
- Copy trading defaults to simulation mode and performs a geoblock check plus the shared adapter live preflight before live orders.

## Notes
- The app stores local settings and paper-order history in `data/config.json` (gitignored user-specific state) using atomic replace writes.
- If you enable LIVE trading, do **not** store keys in plaintext beyond what you accept as your risk.
  Prefer env vars, a password manager, or OS keychain tooling.

## Project structure
- `app.py` – GUI + orchestration
- `polymarket/` – API wrappers + websocket client + trading wrapper
- `core/` – config models + storage helpers
- `data/` – local config and cache

## License
MarketSentinel is licensed under the BSD Zero Clause License (`0BSD`). See [LICENSE](LICENSE).
