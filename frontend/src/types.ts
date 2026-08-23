export type Theme = "light" | "dark";
export type UiDesign = "classic" | "aurora_2026" | "graphite_2026" | "sentinel_2027";

export interface MarketCapabilities {
  market_discovery: boolean;
  event_listing: boolean;
  price_reading: boolean;
  orderbook_reading: boolean;
  trade_history: boolean;
  candle_history: boolean;
  alerts: boolean;
  paper_trading: boolean;
  live_trading: boolean;
  copy_trading: boolean;
  api_required: boolean;
  credentials_required: boolean;
  kyc_required: boolean;
  region_limited: boolean;
}

export interface Market {
  market_id: string;
  display_name: string;
  enabled: boolean;
  default_enabled: boolean;
  homepage_url: string;
  description: string;
  capabilities: MarketCapabilities;
  enabled_capabilities: string[];
  settings: Record<string, unknown>;
  safety: {
    enabled: boolean;
    live_trading_enabled: boolean;
    live_trading_confirmed: boolean;
    live_trading_kill_switch: boolean;
    live_trading_max_size: number | string | null;
    live_trading_max_notional: number | string | null;
  };
  health: {
    market_id: string;
    ok: boolean;
    message: string;
    adapter: string;
    capabilities: MarketCapabilities;
    runtime?: Record<string, unknown>;
    verified_blocker?: boolean;
    credential_requirement?: string;
    account_recovery_operations?: string[];
    authenticated_account_endpoints?: string[];
    order_management_operations?: string[];
    order_management_enabled?: boolean;
    order_management_endpoints?: string[];
  };
  status_text: string;
  credential_env_vars: string[];
  credential_sources: Array<{
    name: string;
    source: string;
  }>;
  credential_summary: string;
}

export interface MarketsPayload {
  selected_market_id: string;
  markets: Market[];
  counts: {
    total: number;
    enabled: number;
    implemented: number;
  };
}

export interface MarketEvent {
  market_id: string;
  event_id: string;
  title: string;
  url: string;
  status: string;
}

export interface MarketContract {
  market_id: string;
  contract_id: string;
  event_id: string;
  title: string;
  outcome: string;
  url: string;
  status: string;
}

export interface MarketPriceSnapshot {
  market_id: string;
  contract_id: string;
  last: number | null;
  bid: number | null;
  ask: number | null;
  midpoint: number | null;
  source: string;
}

export interface MarketOrderbook {
  market_id: string;
  contract_id: string;
  bids: Array<{ price: number; size: number }>;
  asks: Array<{ price: number; size: number }>;
  best_bid: number | null;
  best_ask: number | null;
}

export interface MarketTrade {
  market_id: string;
  contract_id: string;
  trade_id: string;
  side: string;
  price: number;
  size: number;
  timestamp: number | null;
}

export interface MarketCandle {
  market_id: string;
  contract_id: string;
  timestamp: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number | null;
}

export interface MarketEventsPayload {
  market_id: string;
  query: string;
  limit: number;
  events: MarketEvent[];
}

export interface MarketContractsPayload {
  market_id: string;
  event_id: string;
  contracts: MarketContract[];
}

export interface MarketPricePayload {
  market_id: string;
  contract_id: string;
  price: MarketPriceSnapshot | null;
}

export interface MarketOrderbookPayload {
  market_id: string;
  contract_id: string;
  orderbook: MarketOrderbook | null;
}

export interface MarketTradesPayload {
  market_id: string;
  contract_id: string;
  limit: number;
  before: number | null;
  after: number | null;
  trades: MarketTrade[];
}

export interface MarketCandlesPayload {
  market_id: string;
  contract_id: string;
  resolution: string;
  from: number | null;
  to: number | null;
  candles: MarketCandle[];
}

export type MarketAccountOperation =
  | "orders"
  | "order_status"
  | "active_orders"
  | "account"
  | "account_activity"
  | "order_history"
  | "order_detail"
  | "fills"
  | "positions"
  | "settlements"
  | "balance"
  | "queue_positions"
  | "settled_positions"
  | "volume_metrics"
  | "account_history"
  | "user_orders"
  | "market_orders"
  | "cleared_orders"
  | "settled_bets"
  | "current_bets"
  | "current_offers"
  | "portfolio"
  | "subaccounts"
  | "spot_balances"
  | "positions_by_address"
  | "transactions";

export interface MarketAccountPayload {
  market_id: string;
  operation: MarketAccountOperation;
  parameters: Record<string, unknown>;
  data: unknown;
}

export type MarketOrderManagementOperation =
  | "cancel_orders"
  | "cancel_all_orders"
  | "cancel_market_orders"
  | "update_orders"
  | "replace_orders"
  | "cancel_order"
  | "batch_cancel_orders"
  | "cancel_by_cloid"
  | "modify_order"
  | "amend_order"
  | "decrease_order"
  | "cancel_offer"
  | "cancel_offers"
  | "cancel_all_offers"
  | "edit_offer"
  | "edit_offers"
  | "batch_modify_orders"
  | "schedule_cancel"
  | "remove_orders"
  | "remove_orders_by_hash"
  | "batch_create_orders";

export interface MarketOrderManagementPayload {
  market_id: string;
  operation: MarketOrderManagementOperation;
  parameters: Record<string, unknown>;
  data: unknown;
}

export type AlertDirection = "above" | "below";
export type AlertSource = "last_trade" | "midpoint" | "best_bid" | "best_ask";

export interface AlertSourceOption {
  id: AlertSource;
  label: string;
}

export interface AlertPriceValues {
  last_trade: number | null;
  midpoint: number | null;
  best_bid: number | null;
  best_ask: number | null;
}

export interface PriceAlert {
  id: string;
  created_at: number;
  market_id: string;
  token_id: string;
  contract_id: string;
  label: string;
  direction: AlertDirection;
  threshold: number;
  source: AlertSource;
  once: boolean;
  enabled: boolean;
  last_value: number | null;
  triggered: boolean;
  values: AlertPriceValues;
  current_value: number | null;
  status: {
    label: string;
    tone: "good" | "warn" | "neutral";
  };
}

export interface AlertsPayload {
  alerts: PriceAlert[];
  source_options: AlertSourceOption[];
  counts: {
    total: number;
    enabled: number;
    triggered: number;
  };
}

export interface AlertForm {
  market_id: string;
  contract_id: string;
  label: string;
  direction: AlertDirection;
  threshold: string;
  source: AlertSource;
  once: boolean;
  enabled: boolean;
}

export interface AlertRefreshResponse {
  alerts: AlertsPayload;
  message: string;
  refreshed: Array<{
    market_id: string;
    contract_id: string;
    values: AlertPriceValues;
    messages: string[];
    source: string;
  }>;
  problems: string[];
}

export interface WalletWatch {
  id: string;
  wallet: string;
  display_name: string;
  enabled: boolean;
  last_seen_ts: number;
  last_seen_tx: string;
  seen_activity_keys: string[];
  seen_count: number;
  only_market_slug: string;
}

export interface CopyTradePreview {
  status: string;
  reason?: string;
  live?: boolean;
  would_place_order?: boolean;
  blocked?: boolean;
  message?: string;
  order?: {
    market_id: string;
    contract_id: string;
    side: string;
    size: number;
    limit_price: number | null;
    approx_notional: number;
  };
  pricing?: {
    raw_price: number | null;
    best_bid: number | null;
    best_ask: number | null;
    slippage: number;
    capped_by_max_usdc: boolean;
  };
  preflight?: Record<string, unknown>;
}

export interface WalletActivity {
  id: string;
  wallet_id: string;
  wallet: string;
  display_name: string;
  timestamp: number;
  transaction_hash: string;
  proxy_wallet: string;
  side: string;
  asset: string;
  price: number | null;
  size: number | null;
  slug: string;
  outcome: string;
  pseudonym: string;
  raw: Record<string, unknown>;
  copy_preview?: CopyTradePreview;
}

export interface WalletsPayload {
  wallets: WalletWatch[];
  counts: {
    total: number;
    enabled: number;
  };
  polling: {
    mode: string;
    poll_interval_seconds: number;
    last_polled_at: number | null;
    last_message: string;
  };
  recent_activity: WalletActivity[];
}

export interface WalletForm {
  wallet: string;
  display_name: string;
  enabled: boolean;
  only_market_slug: string;
}

export interface WalletPollResponse {
  wallets: WalletsPayload;
  copy: CopyPayload;
  message: string;
  activity: WalletActivity[];
  problems: string[];
  polled_wallets: number;
}

export interface PolymarketUserProfile {
  pseudonym: string;
  proxy_wallet: string;
  profile_image: string;
  display_username_public: boolean;
}

export interface PolymarketUserSearchPayload {
  query: string;
  profiles: PolymarketUserProfile[];
  counts: {
    profiles: number;
  };
  source: string;
}

export type PolymarketLeaderboardSort = "roi_pct" | "pnl_usd" | "volume_usd" | "mdd_usd" | "mdd_pct";
export type PolymarketMddMode = "fast" | "mark_replay";

export interface PolymarketLeaderboardFilters {
  sort: PolymarketLeaderboardSort;
  direction: "ASC" | "DESC";
  limit: string;
  scan_limit: string;
  compute_mdd: boolean;
  mdd_scan_limit: string;
  mdd_history_limit: string;
  mdd_activity_limit: string;
  mdd_trade_limit: string;
  mdd_open_limit: string;
  mdd_mode: PolymarketMddMode;
  mdd_mark_replay_token_limit: string;
  mdd_mark_replay_point_limit: string;
  mdd_mark_replay_interval: string;
  mdd_mark_replay_fidelity: string;
  mdd_include_accounting: boolean;
  mdd_accounting_timeout: string;
  mdd_persist_cache: boolean;
  mdd_cache_ttl_seconds: string;
  equity_base_usd: string;
  min_pnl_usd: string;
  max_pnl_usd: string;
  min_volume_usd: string;
  max_volume_usd: string;
  min_roi_pct: string;
  max_roi_pct: string;
  min_mdd_usd: string;
  max_mdd_usd: string;
  min_mdd_pct: string;
  max_mdd_pct: string;
}

export interface PolymarketLeaderboardRow {
  rank: number;
  wallet: string;
  display_name: string;
  profile_image: string;
  display_username_public: boolean;
  pnl_usd: number | null;
  volume_usd: number | null;
  roi_pct: number | null;
  trade_count: number;
  mdd_usd: number | null;
  mdd_pct: number | null;
  mdd_available: boolean;
  mdd_method?: string;
  mdd_pct_basis?: string;
  mdd_points?: number;
  mdd_closed_positions?: number;
  mdd_open_positions?: number;
  mdd_activity_events?: number;
  mdd_trade_events?: number;
  mdd_equity_base_usd?: number | null;
  mdd_equity_base_source?: string;
  mdd_public_capital_basis_usd?: number | null;
  mdd_peak_value?: number | null;
  mdd_trough_value?: number | null;
  mdd_peak_timestamp?: number | null;
  mdd_trough_timestamp?: number | null;
  mdd_mark_replay_status?: string | null;
  mdd_mark_replay_tokens?: number | null;
  mdd_accounting_status?: string | null;
  mdd_accounting_equity_base_usd?: number | null;
  mdd_accounting_cash_flow_gap_usd?: number | null;
  mdd_audit_cache_key?: string | null;
  mdd_audit_cache_stored?: boolean;
  raw: Record<string, unknown>;
}

export interface PolymarketRateLimitStatus {
  limited: boolean;
  backoff_status: string;
  events: Array<Record<string, unknown>>;
}

export interface PolymarketAnalyticsCacheStatus {
  enabled: boolean;
  path: string;
  exists: boolean;
  entries: number;
  max_entries: number;
  ttl_seconds: number;
  size_bytes: number;
}

export interface PolymarketMddCacheMetadata {
  key?: string | null;
  kind?: string | null;
  stored?: boolean;
  enabled?: boolean;
  hit?: boolean;
  expired?: boolean;
  path?: string;
  stored_at?: number | null;
  expires_at?: number | null;
  entries?: number;
  max_entries?: number;
  ttl_seconds?: number;
  size_bytes?: number;
  exists?: boolean;
  error?: string;
}

export interface PolymarketMddPoint {
  timestamp?: number | string | null;
  value?: number | string | null;
  source?: string | null;
  kind?: string | null;
  [key: string]: unknown;
}

export interface PolymarketMddPayload {
  wallet?: string | null;
  mdd_usd: number | null;
  mdd_pct: number | null;
  mdd_available: boolean;
  mdd_method: string;
  mdd_pct_basis: string;
  points: PolymarketMddPoint[];
  points_total?: number;
  closed_positions?: number;
  open_positions?: number;
  activity_events?: number;
  trade_events?: number;
  equity_base_usd?: number | null;
  equity_base_source?: string | null;
  public_capital_basis_usd?: number | null;
  peak_value?: number | null;
  trough_value?: number | null;
  peak_timestamp?: number | null;
  trough_timestamp?: number | null;
  assumptions?: string[];
  limitations?: string[];
  warnings?: string[];
  audit_cache?: PolymarketMddCacheMetadata;
  rate_limit?: PolymarketRateLimitStatus;
  accounting_snapshot?: Record<string, unknown>;
  mark_replay?: Record<string, unknown>;
  fallback_v2?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface PolymarketMddForm {
  wallet: string;
  mode: PolymarketMddMode;
  closed_limit: string;
  activity_limit: string;
  trade_limit: string;
  open_limit: string;
  max_points: string;
  equity_base_usd: string;
  mark_replay_token_limit: string;
  mark_replay_interval: string;
  mark_replay_fidelity: string;
  include_accounting_snapshot: boolean;
  persist_cache: boolean;
}

export interface PolymarketMddAuditExport {
  cache: PolymarketMddCacheMetadata;
  payload: PolymarketMddPayload;
  export: {
    format: string;
    source: string;
  };
}

export interface PolymarketMddCacheEntry extends PolymarketMddCacheMetadata {
  params?: Record<string, unknown>;
  wallet?: string | null;
  mdd_method?: string | null;
  mdd_available?: boolean;
  mdd_usd?: number | null;
  mdd_pct?: number | null;
  equity_base_usd?: number | null;
  peak_value?: number | null;
  trough_value?: number | null;
  peak_timestamp?: number | null;
  trough_timestamp?: number | null;
  points_total?: number;
  payload_bytes?: number;
  ttl_remaining_seconds?: number | null;
  age_seconds?: number | null;
}

export interface PolymarketMddCachePayload {
  source: string;
  cache: PolymarketAnalyticsCacheStatus & {
    active_entries?: number;
    expired_entries?: number;
    newest_stored_at?: number | null;
    oldest_stored_at?: number | null;
    created_at?: number | null;
    updated_at?: number | null;
    kind?: string | null;
    kinds?: string[];
    version?: number;
  };
  entries: PolymarketMddCacheEntry[];
  counts: {
    entries: number;
    active_entries: number;
    expired_entries: number;
  };
  deleted?: number;
  deleted_keys?: string[];
  missing_keys?: string[];
  requested?: number;
  message?: string;
}

export interface PolymarketMddCachePurgeRequest {
  key?: string;
  keys?: string[];
  expired_only?: boolean;
  all?: boolean;
}

export interface PolymarketLeaderboardPayload {
  rows: PolymarketLeaderboardRow[];
  counts: {
    returned: number;
    filtered: number;
    scanned: number;
    mdd_computed: number;
  };
  sort: PolymarketLeaderboardSort;
  direction: "ASC" | "DESC";
  period: string;
  category: string;
  limit: number | null;
  limit_unlimited?: boolean;
  scan_limit: number | null;
  scan_limit_unlimited?: boolean;
  mdd_scan_limit: number | null;
  mdd_scan_limit_unlimited?: boolean;
  mdd_history_limit: number;
  mdd_activity_limit: number;
  mdd_trade_limit: number;
  mdd_open_limit: number;
  mdd_mode: PolymarketMddMode;
  mdd_mark_replay_token_limit: number;
  mdd_mark_replay_point_limit: number;
  mdd_mark_replay_interval: string;
  mdd_mark_replay_fidelity: number;
  mdd_include_accounting: boolean;
  mdd_accounting_timeout: number;
  mdd_persist_cache: boolean;
  mdd_cache_ttl_seconds: number;
  analytics_cache: PolymarketAnalyticsCacheStatus;
  rate_limit: PolymarketRateLimitStatus;
  source: string;
  source_sort: string;
  ranking_scope: string;
  completion_reason: string;
  source_enumeration_complete: boolean;
  source_scope_note: string;
  mdd_available: boolean;
  mdd_method: string;
  mdd_pct_basis: string;
  mdd_note: string;
  mdd_assumptions: string[];
  mdd_limitations: string[];
  warnings: string[];
}

export interface CopySettings {
  enabled: boolean;
  live: boolean;
  follow_wallet: string;
  follow_wallets: string[];
  scale: number;
  copy_percentage: number;
  max_usdc_per_trade: number;
  slippage: number;
  allow_sells: boolean;
  conflict_guard: boolean;
  conflict_window_seconds: number;
}

export interface CopyPayload {
  settings: CopySettings;
  wallet_choices: string[];
  follow_wallet_tracked: boolean;
  follow_wallets_tracked: number;
  follow_wallets_untracked: string[];
  status: string;
  simulation_first: boolean;
  copy_trading_supported: boolean;
  adapter: string;
  market_id?: string;
  activity_identity_hint?: string;
  live_gate: {
    market_enabled: boolean;
    live_trading_enabled: boolean;
    live_trading_confirmed: boolean;
    live_trading_kill_switch: boolean;
    max_size: number | null;
    max_notional: number | null;
  };
}

export interface CopyForm {
  enabled: boolean;
  live: boolean;
  follow_wallets: string;
  copy_percentage: string;
  max_usdc_per_trade: string;
  slippage: string;
  allow_sells: boolean;
  conflict_guard: boolean;
}

export interface CopyPreviewForm {
  proxyWallet: string;
  asset: string;
  side: string;
  size: string;
  price: string;
  slug: string;
  outcome: string;
}

export interface CopyPreviewPayload {
  preview: CopyTradePreview;
  copy: CopyPayload;
}

export interface LiveSafetyPayload {
  selected_market_id: string;
  market: Market;
  status: string;
  tone: "good" | "warn" | "neutral";
  blockers: string[];
  can_preflight: boolean;
  controls: Market["safety"];
  redaction: {
    sensitive_key_fragments: string[];
    audit_payloads_redacted: boolean;
  };
}

export interface LiveOrderAudit {
  market_id: string;
  contract_id: string;
  side: string;
  size: number;
  limit_price: number | null;
  approx_notional: number;
  metadata_keys: string[];
}

export interface LivePreflightPayload {
  ok: boolean;
  blocked: boolean;
  order: LiveOrderAudit;
  preflight: Record<string, unknown> | null;
  message: string;
  error?: string;
  live_safety: LiveSafetyPayload;
}

export interface PolymarketValidationItem {
  status: string;
  detail: string;
  missing?: string[];
  blockers?: string[];
  next_step?: string;
  live_action?: boolean;
  [key: string]: unknown;
}

export interface PolymarketLiveValidationPayload {
  generated_at: number;
  market_id: string;
  mode: string;
  selected: boolean;
  enabled: boolean;
  credential_presence: Record<string, Record<string, boolean>>;
  clob_auth_readiness: Record<string, unknown>;
  public_checks: Record<string, PolymarketValidationItem>;
  authenticated_read_checks: Record<string, PolymarketValidationItem>;
  bridge_address_checks: Record<string, PolymarketValidationItem>;
  funded_live_order_check: PolymarketValidationItem;
  live_order_cancel_harness: Record<string, unknown>;
  operator_commands: Record<string, string>;
  funded_execution_exposed: boolean;
  notes: string[];
  stage_gates: {
    public_live_checks: string;
    credential_readiness: string;
    credentialed_read_checks: string;
    bridge_address_checks: string;
    funded_live_order_check: string;
    credentialed_read_ok: boolean;
    safe_to_attempt_funded_order: boolean;
    requires_explicit_live_approval: boolean;
    next_step: string;
    [key: string]: unknown;
  };
}

export interface PolymarketLiveValidationReportSummary {
  generated_at?: number | null;
  market_id?: string | null;
  mode?: string | null;
  selected?: boolean;
  enabled?: boolean;
  public_live_checks?: string | null;
  credential_readiness?: string | null;
  credentialed_read_checks?: string | null;
  bridge_address_checks?: string | null;
  funded_live_order_check?: string | null;
  credentialed_read_ok?: boolean;
  safe_to_attempt_funded_order?: boolean;
  requires_explicit_live_approval?: boolean;
  next_step?: string | null;
  funded_execution_exposed?: boolean;
  direct_l2_read_ready?: boolean;
  sdk_trading_ready?: boolean;
  verification_promotion?: {
    credential_live_verified?: string;
    funded_live_verified?: string;
    can_promote_credential_live_verified?: boolean;
    can_promote_funded_live_verified?: boolean;
    credential_evidence?: Array<Record<string, unknown>>;
    funded_evidence?: Array<Record<string, unknown>>;
    blocked_reasons?: string[];
    accepted_credential_checks?: string[];
    accepted_funded_audit_fields?: string[];
    [key: string]: unknown;
  };
  credential_live_verified?: string;
  funded_live_verified?: string;
  can_promote_credential_live_verified?: boolean;
  can_promote_funded_live_verified?: boolean;
  [key: string]: unknown;
}

export interface PolymarketLiveValidationReportSchemaValidation {
  schema_version: number;
  ok: boolean;
  mode?: string | null;
  report_type?: string | null;
  errors: string[];
  warnings: string[];
  accepted_modes: string[];
}

export interface PolymarketLiveValidationReportEntry {
  key?: string | null;
  kind?: string | null;
  source: string;
  label: string;
  stored_at?: number | null;
  stored_at_ns?: number | null;
  age_seconds?: number | null;
  path?: string;
  payload_bytes?: number | null;
  payload_hash?: string | null;
  provenance?: {
    payload_hash?: string | null;
    redacted_payload_hash?: string | null;
    source_file?: string | null;
    source_file_name?: string | null;
    duplicate_policy?: string | null;
    duplicate_of?: string | null;
    [key: string]: unknown;
  };
  duplicate?: boolean;
  duplicate_key?: string | null;
  duplicate_of?: string | null;
  duplicate_policy?: string | null;
  duplicate_payload_count?: number;
  duplicate_import_count?: number;
  last_duplicate_import?: Record<string, unknown>;
  schema_validation?: PolymarketLiveValidationReportSchemaValidation;
  summary?: PolymarketLiveValidationReportSummary;
  payload?: PolymarketLiveValidationPayload | Record<string, unknown>;
}

export interface PolymarketLiveValidationReportsPayload {
  source: string;
  cache: {
    path: string;
    exists: boolean;
    entries: number;
    max_entries: number;
    size_bytes: number;
    version: number;
    created_at?: number | null;
    updated_at?: number | null;
    newest_stored_at?: number | null;
    oldest_stored_at?: number | null;
    payload_hashes?: number;
    duplicate_payloads?: number;
    duplicate_imports?: number;
  };
  entries: PolymarketLiveValidationReportEntry[];
  counts: {
    entries: number;
    payload_hashes?: number;
    duplicate_payloads?: number;
    duplicate_imports?: number;
  };
  comparison?: {
    latest_key?: string | null;
    previous_key?: string | null;
    changed: boolean;
    changes: Array<{
      field: string;
      previous: unknown;
      latest: unknown;
    }>;
  } | null;
  stored?: PolymarketLiveValidationReportEntry & {
    stored?: boolean;
    entries?: number;
    max_entries?: number;
    duplicate?: boolean;
    duplicate_key?: string | null;
    duplicate_audit_event?: Record<string, unknown>;
  };
  deleted?: number;
  deleted_keys?: string[];
  missing_keys?: string[];
  requested?: number;
  message?: string;
}

export interface PolymarketLiveValidationReportPayload {
  source: string;
  entry: PolymarketLiveValidationReportEntry & {
    payload: PolymarketLiveValidationPayload | Record<string, unknown>;
  };
  decisions?: PolymarketLiveValidationDecisionEntry[];
  export: {
    format: string;
    filename: string;
  };
}

export interface PolymarketLiveValidationReportReviewBundle {
  source: string;
  kind: string;
  bundle_version: number;
  review_bundle_hash?: string;
  generated_at: number;
  funded_execution_exposed: boolean;
  static_coverage_mutated: boolean;
  report: {
    key?: string | null;
    label?: string | null;
    source?: string | null;
    stored_at?: number | null;
    payload_hash?: string | null;
    provenance?: Record<string, unknown>;
    summary?: Record<string, unknown>;
  };
  schema_validation: PolymarketLiveValidationReportSchemaValidation;
  duplicate_history: Record<string, unknown>;
  promotion_review: Record<string, unknown>;
  operator_commands: Record<string, string>;
  coverage_tier_mapping: Record<string, unknown>;
  review_notes: string[];
}

export interface PolymarketLiveValidationReportReviewPayload {
  source: string;
  bundle: PolymarketLiveValidationReportReviewBundle;
  export: {
    json_filename: string;
    markdown_filename: string;
  };
}

export interface PolymarketLiveValidationDecisionEntry {
  key?: string | null;
  kind?: string | null;
  created_at?: number | null;
  created_at_ns?: number | null;
  report_key: string;
  payload_hash: string;
  target_tier: string;
  decision: "accepted" | "rejected" | string;
  reviewer: string;
  reviewer_note: string;
  review_bundle_hash: string;
  review_bundle_hash_verified: boolean;
  static_coverage_mutated: boolean;
  funded_execution_exposed: boolean;
  promotion_effect: string;
  report_label?: string;
  report_source?: string;
  coverage_tier_decision?: Record<string, unknown>;
}

export interface PolymarketLiveValidationDecisionLedgerPayload {
  source: string;
  kind: string;
  cache: {
    path: string;
    exists: boolean;
    entries: number;
    size_bytes: number;
    version: number;
    created_at?: number | null;
    updated_at?: number | null;
  };
  entries: PolymarketLiveValidationDecisionEntry[];
  counts: {
    entries: number;
    accepted: number;
    rejected: number;
    by_decision: Record<string, number>;
    by_tier: Record<string, number>;
  };
  static_coverage_mutated: boolean;
  funded_execution_exposed: boolean;
  stored?: PolymarketLiveValidationDecisionEntry & {
    stored?: boolean;
    entries?: number;
  };
  message?: string;
}

export interface PolymarketLiveValidationDecisionStoreRequest {
  report_key: string;
  payload_hash: string;
  target_tier: string;
  decision: "accepted" | "rejected";
  reviewer_note: string;
  review_bundle_hash: string;
  reviewer?: string;
}

export interface PolymarketLiveValidationPromotionProposalPayload {
  source: string;
  kind: string;
  proposal_version: number;
  generated_at: number;
  proposal_hash?: string;
  target_tier_filter?: string | null;
  human_review_required: boolean;
  automerge_enabled: boolean;
  apply_by_default: boolean;
  static_coverage_mutated: boolean;
  funded_execution_exposed: boolean;
  review_gates: Array<Record<string, unknown>>;
  accepted_decisions: Array<Record<string, unknown>>;
  stale_decisions: Array<Record<string, unknown>>;
  ignored_decisions: Array<Record<string, unknown>>;
  proposed_changes: Array<Record<string, unknown>>;
  patch_proposal: Record<string, unknown>;
  counts: {
    ledger_entries: number;
    accepted_candidates: number;
    stale_decisions: number;
    ignored_decisions: number;
    proposed_changes: number;
  };
  notes: string[];
}

export interface PolymarketLiveValidationPromotionProposalSnapshotEntry {
  key?: string | null;
  kind?: string | null;
  stored_at?: number | null;
  stored_at_ns?: number | null;
  age_seconds?: number | null;
  source: string;
  label: string;
  proposal_hash: string;
  current_proposal_hash?: string;
  proposal_generated_at?: number | null;
  proposal_version?: number | null;
  target_tier_filter?: string | null;
  counts: Record<string, unknown>;
  human_review_required: boolean;
  automerge_enabled: boolean;
  apply_by_default: boolean;
  static_coverage_mutated: boolean;
  funded_execution_exposed: boolean;
  snapshot_status: "current" | "stale" | string;
  stale: boolean;
  stale_reasons: string[];
  path?: string;
  provenance?: Record<string, unknown>;
}

export interface PolymarketLiveValidationPromotionProposalSnapshotsPayload {
  source: string;
  kind: string;
  cache: {
    path: string;
    exists: boolean;
    entries: number;
    max_entries: number;
    size_bytes: number;
    version: number;
    created_at?: number | null;
    updated_at?: number | null;
  };
  entries: PolymarketLiveValidationPromotionProposalSnapshotEntry[];
  counts: {
    entries: number;
    current: number;
    stale: number;
  };
  static_coverage_mutated: boolean;
  funded_execution_exposed: boolean;
  stored?: PolymarketLiveValidationPromotionProposalSnapshotEntry & {
    stored?: boolean;
    entries?: number;
  };
  deleted?: number;
  deleted_keys?: string[];
  missing_keys?: string[];
  requested?: number;
  message?: string;
}

export interface PolymarketLiveValidationPromotionProposalSnapshotPayload {
  source: string;
  kind: string;
  entry: PolymarketLiveValidationPromotionProposalSnapshotEntry;
  proposal: PolymarketLiveValidationPromotionProposalPayload | Record<string, unknown>;
  diff: PolymarketLiveValidationPromotionProposalSnapshotDiff;
  export: {
    json_filename: string;
    markdown_filename: string;
  };
  static_coverage_mutated: boolean;
  funded_execution_exposed: boolean;
}

export interface PolymarketLiveValidationPromotionProposalSnapshotDiff {
  source: string;
  kind: string;
  snapshot_key: string;
  target_tier_filter?: string | null;
  proposal_hash: {
    snapshot: string;
    snapshot_canonical: string;
    current: string;
    changed: boolean;
    snapshot_integrity_valid: boolean;
  };
  counts: Record<string, { snapshot: number; current: number; delta: number }>;
  accepted_decisions: { snapshot: number; current: number; added: Array<Record<string, unknown>>; removed: Array<Record<string, unknown>>; unchanged: number };
  stale_decisions: { snapshot: number; current: number; added: Array<Record<string, unknown>>; removed: Array<Record<string, unknown>>; unchanged: number };
  proposed_files: { snapshot: string[]; current: string[]; added: string[]; removed: string[]; unchanged: string[] };
  review_gates: {
    snapshot: number;
    current: number;
    added: Array<Record<string, unknown>>;
    removed: Array<Record<string, unknown>>;
    changed: Array<Record<string, unknown>>;
    unchanged: number;
  };
  changed: boolean;
  change_categories: string[];
  static_coverage_mutated: boolean;
  funded_execution_exposed: boolean;
  notes: string[];
}

export interface PolymarketLiveValidationPromotionProposalSnapshotStoreRequest {
  target_tier?: string;
  label?: string;
  source?: string;
}

export interface PolymarketLiveValidationReportStoreRequest {
  label?: string;
  source?: string;
  source_file?: string;
  allow_duplicate?: boolean;
  skip_duplicate?: boolean;
  report_json?: string;
  report?: Record<string, unknown>;
}

export interface ConfigPayload {
  selected_market_id: string;
  theme: Theme;
  ui_design: UiDesign;
  alerts: unknown[];
  wallets: unknown[];
  copytrading: {
    enabled: boolean;
    live: boolean;
    follow_wallet: string;
    follow_wallets: string[];
    scale: number;
    copy_percentage: number;
    max_usdc_per_trade: number;
    slippage: number;
    allow_sells: boolean;
    conflict_guard: boolean;
    conflict_window_seconds: number;
  };
}

export interface HealthPayload {
  status: string;
  api_version: string;
  mode: string;
  python_gui_available: boolean;
  python_gui_command: string;
  python_gui_script: string;
  tkinter_fallback: string;
  react_gui: string;
  react_dev_command: string;
  react_dev_manual_command: string;
  react_build_command: string;
  react_prod_command: string;
  config_path: string;
  frontend_dist: string;
  frontend_build_available: boolean;
  routes: Record<string, string[]>;
}

export interface PaperPosition {
  market_id: string;
  contract_id: string;
  net_size: number;
  average_price: number | null;
  notional: number | null;
  trades: number;
  mark_price: number | null;
  mark_source: string;
  marked_at: number | null;
  unrealized: number | null;
}

export interface PaperTrade {
  id: string;
  created_at: number;
  market_id: string;
  contract_id: string;
  side: string;
  size: number;
  limit_price: number | null;
  accepted: boolean;
  message: string;
  filled_size: number;
  average_price: number | null;
  raw: Record<string, unknown>;
}

export interface PaperPayload {
  summary: {
    positions: number;
    gross_size: number;
    entry_notional: number;
    net_notional: number;
    marked: number;
    unrealized: number | null;
    mark_sources: Record<string, number>;
    last_marked_at: number | null;
  };
  positions: PaperPosition[];
  history: PaperTrade[];
  counts: {
    history: number;
    accepted: number;
    rejected: number;
  };
}

export interface PaperOrderForm {
  market_id: string;
  contract_id: string;
  side: string;
  size: string;
  limit_price: string;
}

export interface PaperQuotePayload {
  market_id: string;
  contract_id: string;
  display_name: string;
  best_bid: number | null;
  best_ask: number | null;
  suggested_limits: Record<string, number | null>;
  message: string;
  price: {
    last: number | null;
    bid: number | null;
    ask: number | null;
    midpoint: number | null;
    source: string;
  } | null;
  orderbook: {
    best_bid: number | null;
    best_ask: number | null;
    bids: Array<{ price: number; size: number }>;
    asks: Array<{ price: number; size: number }>;
  } | null;
}

export interface PaperImpactPayload {
  impact: Record<string, number | string | null>;
  message: string;
}

export interface PaperOrderResponse {
  record: PaperTrade;
  result: {
    accepted: boolean;
    message: string;
    filled_size: number;
    average_price: number | null;
  };
  paper: PaperPayload;
}

export interface PaperFormFillPayload {
  market_id: string;
  contract_id: string;
  side: string;
  size: number;
  limit_price: number | null;
  message: string;
}

export interface AppStatePayload {
  health: HealthPayload;
  config: ConfigPayload;
  markets: MarketsPayload;
  alerts: AlertsPayload;
  wallets: WalletsPayload;
  copy: CopyPayload;
  live_safety: LiveSafetyPayload;
  polymarket_live_validation: PolymarketLiveValidationPayload;
  polymarket_live_validation_reports: PolymarketLiveValidationReportsPayload;
  paper: PaperPayload;
}
