from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict


COVERAGE_LEVEL_DEFINITIONS: Dict[str, str] = {
    "wrapper_available": "A local Python module exposes documented endpoint request helpers.",
    "app_workflow_available": "The Tkinter/API/React application exposes a user workflow using this surface.",
    "offline_tested": "Unit tests validate parsing, request construction, and guardrails without real credentials.",
    "public_live_verified": "A non-credentialed live probe against Polymarket passed from this machine.",
    "credential_live_verified": "A credentialed read or authenticated stream was verified with real credentials.",
    "funded_live_verified": "A funded live action, such as order/cancel or fund movement, was verified with explicit approval.",
}

COVERAGE_STATE_DEFINITIONS: Dict[str, str] = {
    "yes": "Covered for the stated category.",
    "partial": "Some endpoints or workflows are covered, but the whole category is not complete.",
    "no": "Not currently exposed or implemented at this level.",
    "blocked": "Not verified because credentials, live account eligibility, funded wallet state, or network setup is missing.",
    "not_applicable": "This verification level does not apply to the category.",
}


POLYMARKET_OFFICIAL_API_COVERAGE: Dict[str, Any] = {
    "docs_checked": "2026-05-28",
    "official_sources": [
        "https://docs.polymarket.com/",
        "https://docs.polymarket.com/api-reference/introduction",
        "https://docs.polymarket.com/llms.txt",
    ],
    "coverage_level_definitions": COVERAGE_LEVEL_DEFINITIONS,
    "coverage_state_definitions": COVERAGE_STATE_DEFINITIONS,
    "scope": "Official Polymarket Gamma, Data, CLOB, Bridge, Relayer, and WebSocket surfaces.",
    "safe_live_probe_command": "python scripts/verify_polymarket_live.py --timeout 8",
    "contract_hardening": {
        "date": "2026-05-28",
        "modules": ["polymarket.endpoints", "polymarket.http_client"],
        "features": [
            "central endpoint metadata for official Gamma, Data, CLOB, Bridge, and Relayer REST surfaces",
            "typed Polymarket HTTP, rate-limit, validation, and response errors",
            "retry handling for safe transient public reads",
            "documented batch caps for batch price history, multi-order posting, and multi-order cancellation",
            "shared response normalization helpers used by wrapper modules",
        ],
    },
    "authenticated_clob_readiness": {
        "date": "2026-05-28",
        "module": "polymarket.auth_readiness",
        "api_route": "/api/polymarket/clob-readiness",
        "features": [
            "private-key, signature-type, chain-id, host, and funder/deposit-wallet validation",
            "explicit distinction between SDK trading readiness, L1 REST headers, and L2 read-only REST headers",
            "redacted readiness reporting for adapter health and local API payloads",
            "no funded order placement or credential derivation unless explicit live validation commands are run",
        ],
    },
    "live_order_cancel_harness": {
        "date": "2026-05-28",
        "module": "polymarket.live_verification",
        "command": "python scripts/verify_polymarket_live.py --token-id <TOKEN> --side BUY --price <PRICE> --size <SIZE> --allow-token-id <TOKEN>",
        "execute_requirements": [
            "--allow-funded-order",
            "--cancel-immediately",
            "--allow-token-id or --allow-token-file",
            "--confirm-live-order-cancel I_UNDERSTAND_THIS_PLACES_A_REAL_POLYMARKET_ORDER",
            "--recovery-journal <ABSOLUTE_PRIVATE_JOURNAL_PATH>",
            "valid CLOB credentials, eligible account/geography, same-client authenticated read, sufficient balance/allowance, and post-only maker preflight",
            "durable locked recovery journal plus exact cancel and zero-fill verification",
        ],
        "hard_caps": {"max_size": 5.0, "max_notional_usdc": 1.0},
        "default_mode": "dry_run_transcript",
    },
    "live_credential_validation": {
        "date": "2026-05-28",
        "module": "scripts.verify_polymarket_live, scripts.verify_polymarket_credentials, polymarket.credential_runbook, polymarket.live_reports, polymarket.live_verification, polymarket.ws_user",
        "command": "python scripts/verify_polymarket_live.py --require-authenticated-read-ok --include-user-websocket-connect --report-file live-report.json",
        "stages": [
            "local no-network credential runbook and redacted environment inventory",
            "public non-credentialed endpoint probes",
            "redacted local CLOB credential readiness",
            "non-destructive authenticated CLOB/relayer/user-WebSocket reads",
            "dry-run order/cancel transcript with token allow-list and hard caps",
            "funded live order/cancel only after explicit flags and confirmation text",
        ],
        "default_mode": "no_funded_actions",
        "runbook_command": "python scripts/verify_polymarket_credentials.py --json --report-file polymarket-credential-runbook.json",
        "promotion_guard": {
            "module": "polymarket.live_reports.live_validation_report_promotion",
            "credential_live_verified_requires": [
                "local report remains candidate_only until trusted workflow attestation",
                "report.ok == true with exact semantic public-check inventory",
                "clean exact revision bound to github.com/yunushan/market-sentinel origin",
                "ok clob_l2_orders authenticated read",
                "ok relayer_recent_transactions authenticated read",
            ],
            "funded_live_verified_requires": [
                "local report remains candidate_only until trusted workflow attestation",
                "same-report accepted non-mutating authenticated read",
                "funded_live_order_check.status == ok",
                "funded_live_order_check.live_action == true",
                "manual_reconciliation_required == false",
                "canonical repository origin and exact clean source revision gate",
                "same-client account identity and authenticated order-list preflight",
                "geoblock blocked == false",
                "sufficient trading balance and allowance",
                "post-only GTC maker-price execution guards",
                "audit order id",
                "placed/cancel/post_cancel_order audit sections",
                "exact cancel acknowledgement and canceled state",
                "explicit zero matched size and empty associated trades",
                "resolved cancel_verified durable recovery journal",
                "audit.post_cancel_verified == true",
            ],
            "local_only_modes_blocked": ["local_readiness_only", "credential_runbook_no_funded_actions", "browser_smoke", "browser_smoke_seed"],
        },
        "report_fields": ["credential_presence", "clob_auth_readiness", "credential_runbook", "stage_gates", "verification_promotion"],
    },
    "historical_mdd_v2": {
        "date": "2026-05-28",
        "module": "polymarket.mdd",
        "api_routes": ["/api/polymarket/users/mdd", "/api/polymarket/users/leaderboard"],
        "method": "public_data_historical_equity_curve_v2",
        "features": [
            "realized-PnL equity curve from public closed positions",
            "current open-position PnL snapshot",
            "trade/activity-derived public capital basis for percentage drawdown",
            "pagination controls for closed positions, open positions, activity, and trades",
            "process-memory public-data cache boundary for leaderboard scans",
            "payload-level assumptions and limitations for cash-flow ledger and historical mark replay gaps",
        ],
    },
    "historical_mark_replay_mdd": {
        "date": "2026-05-28",
        "module": "polymarket.mdd",
        "method": "clob_price_history_inventory_mark_replay_v1",
        "default": "off",
        "official_surface": "CLOB /batch-prices-history",
        "features": [
            "optional mdd_mode=mark_replay path for user and leaderboard MDD payloads",
            "trade-derived token inventory replay from public Data API trade/activity rows",
            "historical token marks from public CLOB batch price history",
            "documented 20-token batch cap enforced through request controls",
            "partial reconstruction reporting for missing price history, clipped token ids, bad trade rows, and negative inventory",
            "v2 public Data API MDD remains the default fallback and fast mode",
        ],
    },
    "accounting_snapshot_reconciliation": {
        "date": "2026-05-28",
        "module": "polymarket.accounting",
        "official_surface": "Data /v1/accounting/snapshot",
        "default": "off",
        "features": [
            "optional parsing of accounting snapshot ZIP CSV files",
            "equity.csv max-equity extraction for stronger MDD percentage base",
            "positions.csv reconciliation against current value and realized PnL",
            "deposit, withdrawal, explicit cash-flow, and cash-flow gap reporting",
            "fallback-safe payloads when snapshot download or parsing is unavailable",
        ],
    },
    "analytics_cache_exports": {
        "date": "2026-05-28",
        "module": "polymarket.analytics_cache",
        "api_routes": ["/api/polymarket/users/mdd/export.json", "/api/polymarket/users/mdd/export.csv"],
        "default": "off",
        "features": [
            "persistent bounded local JSON cache for expensive public MDD audit artifacts",
            "stable cache keys derived from wallet and MDD request parameters",
            "per-wallet JSON and CSV exports from cached audit payloads without rerunning public API calls",
            "explicit rate-limit/backoff metadata in leaderboard and direct MDD API responses",
            "cache summary metadata exposed with leaderboard and MDD payloads when persistence is requested",
        ],
    },
    "last_safe_live_probe": {
        "date": "2026-05-28",
        "public_checks": {
            "clob_time": "ok",
            "gamma_markets": "ok",
            "data_leaderboard": "ok",
            "bridge_supported_assets": "ok",
        },
        "credentialed_checks": "blocked_missing_credentials",
        "funded_checks": "blocked_missing_credentials_and_explicit_live_parameters",
    },
    "categories": [
        {
            "name": "Gamma public discovery",
            "truthful_status": "public wrappers and partial app workflow are available; broader discovery helpers are not all surfaced in the GUI.",
            "module": "polymarket.gamma",
            "coverage_levels": {
                "wrapper_available": "yes",
                "app_workflow_available": "partial",
                "offline_tested": "partial",
                "public_live_verified": "partial",
                "credential_live_verified": "not_applicable",
                "funded_live_verified": "not_applicable",
            },
            "coverage": [
                "events list/keyset/id/slug/tags",
                "markets list/keyset/id/slug/tags",
                "public profile and public search",
                "tags, related-tag relationships, and related tags",
                "series, comments, sports metadata, sports market types, and teams",
            ],
        },
        {
            "name": "Data public analytics",
            "truthful_status": "public wrappers and analytics workflows are available; global scans and some analytics endpoints are not full app workflows.",
            "module": "polymarket.data_api",
            "coverage_levels": {
                "wrapper_available": "yes",
                "app_workflow_available": "partial",
                "offline_tested": "partial",
                "public_live_verified": "partial",
                "credential_live_verified": "not_applicable",
                "funded_live_verified": "not_applicable",
            },
            "coverage": [
                "activity, current positions, closed positions, trades, total value, total traded markets",
                "leaderboard, market positions, top holders, open interest, live event volume",
                "accounting snapshot download, builder leaderboard, builder volume time series",
                "historical MDD v2 public-data equity curve with USD/% ranking and filtering",
                "optional CLOB price-history mark replay MDD mode for trade-derived inventory",
                "optional accounting snapshot reconciliation for MDD equity base and cash-flow gaps",
            ],
        },
        {
            "name": "CLOB public market data",
            "truthful_status": "public wrappers and core price/orderbook workflows are available; broad market-parameter/rewards helpers are wrapper-level.",
            "module": "polymarket.clob_rest",
            "coverage_levels": {
                "wrapper_available": "yes",
                "app_workflow_available": "partial",
                "offline_tested": "partial",
                "public_live_verified": "partial",
                "credential_live_verified": "not_applicable",
                "funded_live_verified": "not_applicable",
            },
            "coverage": [
                "books, prices, midpoints, spreads, last trade prices",
                "price history and batch price history",
                "fee rate, tick size, CLOB market info, market-by-token, server time",
                "simplified markets, sampling markets, rebates, public rewards, builder trades",
            ],
        },
        {
            "name": "CLOB authenticated trading and account data",
            "truthful_status": "guarded wrappers, readiness validation, and live-order path exist, but credentialed reads and funded order/cancel are not live verified.",
            "module": "polymarket.trader, polymarket.clob_auth, polymarket.auth_readiness",
            "coverage_levels": {
                "wrapper_available": "partial",
                "app_workflow_available": "partial",
                "offline_tested": "partial",
                "public_live_verified": "not_applicable",
                "credential_live_verified": "blocked",
                "funded_live_verified": "blocked",
            },
            "coverage": [
                "CLOB v2 readiness validation for private key, signature type, funder/deposit wallet, L1 headers, and L2 headers",
                "local no-network credential runbook for redacted environment inventory and exact operator commands",
                "stored report promotion guard that prevents stage-gate-only, dry-run, runbook, or browser-smoke reports from claiming production credential/funded verification tiers",
                "disabled-by-default live order/cancel verification harness with dry-run transcript, token allow-list, hard caps, maker-side orderbook preflight, immediate cancel, and post-cancel verification",
                "official py-clob-client-v2 order placement, market orders, and multi-order posting",
                "guarded REST wrappers for get/cancel orders, order lists, trades, order scoring, heartbeat",
                "guarded authenticated rewards endpoints",
            ],
            "runtime_requirements": [
                "Polymarket account eligibility",
                "private key or explicit signed L2 headers",
                "geographic/KYC availability",
                "live trading opt-in",
            ],
        },
        {
            "name": "Bridge deposits and withdrawals",
            "truthful_status": "public wrappers exist and supported-assets was live verified; address creation and fund movement are not app workflows or funded verified.",
            "module": "polymarket.bridge",
            "coverage_levels": {
                "wrapper_available": "yes",
                "app_workflow_available": "no",
                "offline_tested": "partial",
                "public_live_verified": "partial",
                "credential_live_verified": "not_applicable",
                "funded_live_verified": "blocked",
            },
            "coverage": [
                "supported assets, deposit addresses, quotes, transaction status, withdrawal addresses",
            ],
        },
        {
            "name": "Relayer",
            "truthful_status": "guarded wrappers exist, but authenticated relayer reads/submits are not live verified and no app workflow exists.",
            "module": "polymarket.relayer",
            "coverage_levels": {
                "wrapper_available": "partial",
                "app_workflow_available": "no",
                "offline_tested": "partial",
                "public_live_verified": "blocked",
                "credential_live_verified": "blocked",
                "funded_live_verified": "blocked",
            },
            "coverage": [
                "submit transaction, get transaction, recent transactions, nonce, relay payload, deployed check, API keys",
            ],
            "runtime_requirements": ["Builder API headers or relayer API key headers for authenticated endpoints"],
        },
        {
            "name": "WebSocket channels",
            "truthful_status": "market/user/sports clients exist, but only market WebSocket is tied to app alerts and no live WebSocket session was verified in this audit.",
            "module": "polymarket.ws_market, polymarket.ws_user, polymarket.ws_sports",
            "coverage_levels": {
                "wrapper_available": "partial",
                "app_workflow_available": "partial",
                "offline_tested": "partial",
                "public_live_verified": "blocked",
                "credential_live_verified": "blocked",
                "funded_live_verified": "not_applicable",
            },
            "coverage": ["market channel", "authenticated user channel", "sports channel"],
            "runtime_requirements": ["user channel requires API key, secret, and passphrase"],
        },
    ],
    "validation_note": (
        "Offline unit tests validate request construction and guardrails. Full 100% live workflow validation "
        "requires real Polymarket credentials, eligible region/KYC status, funded wallets, and user-approved live mode."
    ),
}


def polymarket_official_api_coverage() -> Dict[str, Any]:
    return deepcopy(POLYMARKET_OFFICIAL_API_COVERAGE)
