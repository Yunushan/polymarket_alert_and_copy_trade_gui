import { useEffect, useMemo, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import {
  Activity,
  BarChart3,
  Bell,
  BellRing,
  Copy,
  Database,
  Download,
  Edit3,
  Eye,
  Power,
  Radio,
  RefreshCw,
  Save,
  Search,
  Settings,
  ShieldCheck,
  SlidersHorizontal,
  Trophy,
  Trash2,
  Upload,
  Wallet,
  XCircle
} from "lucide-react";
import {
  ApiRequestError,
  apiSchemaValidation,
  createAlert,
  createWallet,
  clearPaperMarks,
  clearSelectedPaperMark,
  clearPaperHistory,
  deleteAlert,
  deletePolymarketLiveValidationReport,
  deletePolymarketLiveValidationPromotionProposalSnapshot,
  deleteWallet,
  fillPaperQuoteLimit,
  fetchLiveSafety,
  fetchMarketAccount,
  fetchMarketCandles,
  fetchMarketContracts,
  fetchMarketEvents,
  fetchMarketOrderbook,
  fetchMarketPrice,
  fetchMarketTrades,
  manageMarketOrders,
  fetchPolymarketLeaderboard,
  fetchPolymarketLiveValidationDecisions,
  fetchPolymarketLiveValidation,
  fetchPolymarketLiveValidationPromotionProposal,
  fetchPolymarketLiveValidationPromotionProposalSnapshot,
  fetchPolymarketLiveValidationPromotionProposalSnapshots,
  fetchPolymarketLiveValidationReport,
  fetchPolymarketLiveValidationReportReview,
  fetchPolymarketLiveValidationReports,
  fetchPolymarketMdd,
  fetchPolymarketMddAudit,
  fetchPolymarketMddCache,
  fetchState,
  polymarketLiveValidationReportExportUrl,
  polymarketLiveValidationDecisionLedgerJsonUrl,
  polymarketLiveValidationDecisionLedgerMarkdownUrl,
  polymarketLiveValidationPromotionProposalJsonUrl,
  polymarketLiveValidationPromotionProposalMarkdownUrl,
  polymarketLiveValidationPromotionProposalSnapshotJsonUrl,
  polymarketLiveValidationPromotionProposalSnapshotDiffJsonUrl,
  polymarketLiveValidationPromotionProposalSnapshotDiffMarkdownUrl,
  polymarketLiveValidationPromotionProposalSnapshotMarkdownUrl,
  polymarketLiveValidationReportReviewJsonUrl,
  polymarketLiveValidationReportReviewMarkdownUrl,
  polymarketMddExportUrl,
  pollWallets,
  previewLivePreflight,
  previewPaperImpact,
  previewCopyTrade,
  purgePolymarketMddCache,
  refreshAlert,
  refreshAlerts,
  refreshPaperMarks,
  refreshPaperQuote,
  refreshSelectedPaperMark,
  searchPolymarketUsers,
  submitPaperOrder,
  storePolymarketLiveValidationReport,
  storePolymarketLiveValidationDecision,
  storePolymarketLiveValidationPromotionProposalSnapshot,
  updateAlert,
  updateCopySettings,
  updateConfig,
  updateMarket,
  updateWallet,
  updateWalletPolling,
  usePaperHistory,
  usePaperPosition
} from "./api";
import type { MarketPatch } from "./api";
import type {
  AlertForm,
  AlertsPayload,
  ConfigPayload,
  CopyForm,
  CopyPayload,
  CopyPreviewForm,
  CopyTradePreview,
  HealthPayload,
  LivePreflightPayload,
  LiveSafetyPayload,
  Market,
  MarketCandlesPayload,
  MarketAccountOperation,
  MarketAccountPayload,
  MarketContractsPayload,
  MarketEventsPayload,
  MarketOrderbookPayload,
  MarketOrderManagementOperation,
  MarketOrderManagementPayload,
  MarketPricePayload,
  MarketTradesPayload,
  MarketsPayload,
  PaperOrderForm,
  PaperPayload,
  PolymarketLeaderboardFilters,
  PolymarketLeaderboardPayload,
  PolymarketLeaderboardSort,
  PolymarketLiveValidationDecisionLedgerPayload,
  PolymarketLiveValidationPromotionProposalPayload,
  PolymarketLiveValidationPromotionProposalSnapshotPayload,
  PolymarketLiveValidationPromotionProposalSnapshotsPayload,
  PolymarketLiveValidationReportPayload,
  PolymarketLiveValidationReportSchemaValidation,
  PolymarketLiveValidationReportsPayload,
  PolymarketLiveValidationPayload,
  PolymarketMddAuditExport,
  PolymarketMddCachePayload,
  PolymarketMddCachePurgeRequest,
  PolymarketMddForm,
  PolymarketMddPayload,
  PolymarketUserSearchPayload,
  PriceAlert,
  Theme,
  UiDesign,
  WalletActivity,
  WalletForm,
  WalletsPayload,
  WalletWatch
} from "./types";
import "./styles.css";

type Tab = "overview" | "markets" | "analytics" | "live" | "alerts" | "wallets" | "paper" | "settings";
const TABS: Tab[] = ["overview", "markets", "analytics", "live", "alerts", "wallets", "paper", "settings"];

interface LiveValidationDecisionForm {
  target_tier: string;
  decision: "accepted" | "rejected";
  reviewer: string;
  reviewer_note: string;
}

interface MarketReadForm {
  query: string;
  event_id: string;
  contract_id: string;
  account_ticker: string;
  account_order_id: string;
  account_trade_id: string;
  account_cursor: string;
  account_offset: string;
  account_wallet: string;
  account_event_types: string;
  account_is_resolved: string;
  account_dex: string;
  account_market_id: string;
  account_chain_id: string;
  account_status: string;
  account_event_type_id: string;
  account_event_id: string;
  account_runner_id: string;
  account_bet_id: string;
  account_group_by: string;
  account_include_item_description: boolean;
  account_sport_id: string;
  account_side: string;
  account_offer_status: string;
  account_aggregation_type: string;
  account_odds_type: string;
  account_interval: string;
  account_cancellation_reason: string;
  account_include_edits: boolean;
  account_subaccount: string;
  account_count_filter: string;
  account_market_slug: string;
  account_page: string;
  account_trading_model: string;
  account_min_shares: string;
  account_network_id: string;
  account_token_address: string;
  account_keyword: string;
  account_sort: string;
  account_sort_by: string;
  account_state: string;
  account_topics: string;
  account_market_ids: string;
  account_exclude_history: boolean;
  account_group_by_event: boolean;
  account_on_behalf_of: string;
  account_historical: boolean;
  resolution: string;
  from: string;
  to: string;
  account_operation: MarketAccountOperation;
  order_management_operation: MarketOrderManagementOperation;
  order_management_market_id: string;
  order_management_market_slug: string;
  order_management_instructions: string;
  order_management_customer_ref: string;
  order_management_market_version: string;
  order_management_async: boolean;
  order_management_confirmation: string;
  order_management_order_id: string;
  order_management_external_id: string;
  order_management_offer_ids: string;
  order_management_event_ids: string;
  order_management_market_ids: string;
  order_management_runner_ids: string;
  order_management_current_odds: string;
  order_management_new_odds: string;
  order_management_current_stake: string;
  order_management_new_stake: string;
  order_management_order_hash: string;
  order_management_trader: string;
  order_management_timestamp: string;
  order_management_signature: string;
  order_management_signature_type: string;
  order_management_network_id: string;
  order_management_allow_partial: boolean;
  order_management_ticker: string;
  order_management_side: string;
  order_management_price: string;
  order_management_count: string;
  order_management_client_order_id: string;
  order_management_updated_client_order_id: string;
  order_management_reduce_by: string;
  order_management_reduce_to: string;
  order_management_subaccount: string;
  order_management_exchange_index: string;
  order_management_operator_confirmation: string;
}

interface MarketReadState {
  events: MarketEventsPayload | null;
  contracts: MarketContractsPayload | null;
  price: MarketPricePayload | null;
  orderbook: MarketOrderbookPayload | null;
  trades: MarketTradesPayload | null;
  candles: MarketCandlesPayload | null;
  account: MarketAccountPayload | null;
  order_management: MarketOrderManagementPayload | null;
}

function isTab(value: string | null): value is Tab {
  return TABS.includes(value as Tab);
}

function initialTabFromUrl(): Tab {
  const value = new URLSearchParams(window.location.search).get("tab");
  return isTab(value) ? value : "overview";
}

function formatNumber(value: number | null | undefined, digits = 4): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "-";
  }
  return value.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits
  });
}

function formatUsd(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "-";
  }
  return value.toLocaleString(undefined, {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  });
}

function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "-";
  }
  return `${value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}%`;
}

function formatTime(seconds: number): string {
  if (!seconds) {
    return "-";
  }
  return new Date(seconds * 1000).toLocaleString();
}

function formatUnknownTime(value: unknown): string {
  const numeric = typeof value === "number" ? value : Number(value);
  return Number.isFinite(numeric) && numeric > 0 ? formatTime(numeric) : "-";
}

function formatUnknownNumber(value: unknown, digits = 4): string {
  const numeric = typeof value === "number" ? value : Number(value);
  return formatNumber(numeric, digits);
}

function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "-";
  }
  if (value < 1024) {
    return `${value} B`;
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toLocaleString(undefined, { maximumFractionDigits: 1 })} KB`;
  }
  return `${(value / (1024 * 1024)).toLocaleString(undefined, { maximumFractionDigits: 1 })} MB`;
}

function formatHash(value: string | null | undefined): string {
  const clean = String(value ?? "").trim();
  if (!clean) {
    return "-";
  }
  return clean.length > 16 ? `${clean.slice(0, 12)}...${clean.slice(-6)}` : clean;
}

function proposalValue(row: Record<string, unknown> | undefined, key: string): string {
  const value = row?.[key];
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function formatAuditValue(value: unknown): string {
  if (Array.isArray(value)) {
    return value.length ? value.map((item) => String(item)).join(", ") : "-";
  }
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  if (typeof value === "object") {
    return JSON.stringify(value);
  }
  return String(value);
}

function enabledCapabilities(market: Market): string[] {
  return Object.entries(market.capabilities)
    .filter(([, enabled]) => enabled)
    .map(([name]) => name);
}

function credentialRequirementText(market: Market): string {
  if (market.health?.credential_requirement === "live_trading_only") {
    return "credentials: live only";
  }
  return market.capabilities.credentials_required ? "credentials required" : "no credential flag";
}

function StatusPill({ children, tone = "neutral" }: { children: ReactNode; tone?: "good" | "warn" | "neutral" }) {
  return <span className={`status-pill ${tone}`}>{children}</span>;
}

function Metric({ label, value, tone }: { label: string; value: string | number; tone?: "good" | "warn" | "neutral" }) {
  return (
    <div className={`metric ${tone ?? ""}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function validationTone(status: string | undefined): "good" | "warn" | "neutral" {
  if (status === "ok" || status === "passed" || status === "partial") {
    return "good";
  }
  if (status === "blocked" || status === "failed") {
    return "warn";
  }
  return "neutral";
}

function schemaValidationTone(validation: PolymarketLiveValidationReportSchemaValidation | null | undefined): "good" | "warn" | "neutral" {
  if (!validation) {
    return "neutral";
  }
  if (!validation.ok || validation.errors.length) {
    return "warn";
  }
  return validation.warnings.length ? "warn" : "good";
}

function schemaValidationLabel(validation: PolymarketLiveValidationReportSchemaValidation | null | undefined): string {
  if (!validation) {
    return "schema: unknown";
  }
  if (!validation.ok || validation.errors.length) {
    return `schema: rejected (${validation.errors.length})`;
  }
  if (validation.warnings.length) {
    return `schema: accepted with ${validation.warnings.length} warning(s)`;
  }
  return "schema: accepted";
}

function emptyAlertForm(marketId = "polymarket"): AlertForm {
  return {
    market_id: marketId,
    contract_id: "",
    label: "",
    direction: "above",
    threshold: "",
    source: "last_trade",
    once: true,
    enabled: true
  };
}

function emptyMarketReadForm(): MarketReadForm {
  return {
    query: "",
    event_id: "",
    contract_id: "",
    account_ticker: "",
    account_order_id: "",
    account_trade_id: "",
    account_cursor: "",
    account_offset: "",
    account_wallet: "",
    account_event_types: "",
    account_is_resolved: "",
    account_dex: "",
    account_market_id: "",
    account_chain_id: "",
    account_status: "",
    account_event_type_id: "",
    account_event_id: "",
    account_runner_id: "",
    account_bet_id: "",
    account_group_by: "BET",
    account_include_item_description: false,
    account_sport_id: "",
    account_side: "",
    account_offer_status: "",
    account_aggregation_type: "none",
    account_odds_type: "DECIMAL",
    account_interval: "",
    account_cancellation_reason: "",
    account_include_edits: false,
    account_subaccount: "",
    account_count_filter: "",
    account_market_slug: "",
    account_page: "1",
    account_trading_model: "all",
    account_min_shares: "",
    account_network_id: "",
    account_token_address: "",
    account_keyword: "",
    account_sort: "",
    account_sort_by: "",
    account_state: "",
    account_topics: "",
    account_market_ids: "",
    account_exclude_history: false,
    account_group_by_event: false,
    account_on_behalf_of: "",
    account_historical: false,
    resolution: "1h",
    from: "",
    to: "",
    account_operation: "active_orders",
    order_management_operation: "cancel_orders",
    order_management_market_id: "",
    order_management_market_slug: "",
    order_management_instructions: "[{\"bet_id\": \"bet-id\", \"size_reduction\": 1}]",
    order_management_customer_ref: "",
    order_management_market_version: "",
    order_management_async: false,
    order_management_confirmation: "",
    order_management_order_id: "",
    order_management_external_id: "",
    order_management_offer_ids: "",
    order_management_event_ids: "",
    order_management_market_ids: "",
    order_management_runner_ids: "",
    order_management_current_odds: "",
    order_management_new_odds: "",
    order_management_current_stake: "",
    order_management_new_stake: "",
    order_management_order_hash: "",
    order_management_trader: "",
    order_management_timestamp: "",
    order_management_signature: "",
    order_management_signature_type: "",
    order_management_network_id: "",
    order_management_allow_partial: true,
    order_management_ticker: "",
    order_management_side: "bid",
    order_management_price: "",
    order_management_count: "",
    order_management_client_order_id: "",
    order_management_updated_client_order_id: "",
    order_management_reduce_by: "",
    order_management_reduce_to: "",
    order_management_subaccount: "",
    order_management_exchange_index: "",
    order_management_operator_confirmation: ""
  };
}

function emptyMarketReadState(): MarketReadState {
  return {
    events: null,
    contracts: null,
    price: null,
    orderbook: null,
    trades: null,
    candles: null,
    account: null,
    order_management: null
  };
}

function alertToForm(alert: PriceAlert): AlertForm {
  return {
    market_id: alert.market_id,
    contract_id: alert.contract_id || alert.token_id,
    label: alert.label,
    direction: alert.direction,
    threshold: String(alert.threshold),
    source: alert.source,
    once: alert.once,
    enabled: alert.enabled
  };
}

function emptyWalletForm(): WalletForm {
  return {
    wallet: "",
    display_name: "",
    enabled: true,
    only_market_slug: ""
  };
}

function walletToForm(wallet: WalletWatch): WalletForm {
  return {
    wallet: wallet.wallet,
    display_name: wallet.display_name,
    enabled: wallet.enabled,
    only_market_slug: wallet.only_market_slug
  };
}

function copyToForm(copy: CopyPayload | null): CopyForm {
  const settings = copy?.settings;
  return {
    enabled: settings?.enabled ?? false,
    live: settings?.live ?? false,
    follow_wallets: (settings?.follow_wallets?.length ? settings.follow_wallets : settings?.follow_wallet ? [settings.follow_wallet] : []).join(", "),
    copy_percentage: String(settings?.copy_percentage ?? (settings?.scale ?? 1) * 100),
    max_usdc_per_trade: String(settings?.max_usdc_per_trade ?? 25),
    slippage: String(settings?.slippage ?? 0.02),
    allow_sells: settings?.allow_sells ?? false,
    conflict_guard: settings?.conflict_guard ?? true
  };
}

function emptyCopyPreviewForm(followWallet = ""): CopyPreviewForm {
  return {
    proxyWallet: followWallet,
    asset: "",
    side: "BUY",
    size: "",
    price: "",
    slug: "",
    outcome: ""
  };
}

function defaultLeaderboardFilters(): PolymarketLeaderboardFilters {
  return {
    sort: "roi_pct",
    direction: "DESC",
    limit: "100",
    scan_limit: "500",
    compute_mdd: false,
    mdd_scan_limit: "100",
    mdd_history_limit: "500",
    mdd_activity_limit: "1000",
    mdd_trade_limit: "1000",
    mdd_open_limit: "500",
    mdd_mode: "fast",
    mdd_mark_replay_token_limit: "10",
    mdd_mark_replay_point_limit: "5000",
    mdd_mark_replay_interval: "1h",
    mdd_mark_replay_fidelity: "60",
    mdd_include_accounting: false,
    mdd_accounting_timeout: "30",
    mdd_persist_cache: false,
    mdd_cache_ttl_seconds: "60",
    equity_base_usd: "",
    min_pnl_usd: "",
    max_pnl_usd: "",
    min_volume_usd: "",
    max_volume_usd: "",
    min_roi_pct: "",
    max_roi_pct: "",
    min_mdd_usd: "",
    max_mdd_usd: "",
    min_mdd_pct: "",
    max_mdd_pct: ""
  };
}

function defaultMddForm(): PolymarketMddForm {
  return {
    wallet: "",
    mode: "fast",
    closed_limit: "500",
    activity_limit: "1000",
    trade_limit: "1000",
    open_limit: "500",
    max_points: "100",
    equity_base_usd: "",
    mark_replay_token_limit: "10",
    mark_replay_interval: "1h",
    mark_replay_fidelity: "60",
    include_accounting_snapshot: false,
    persist_cache: true
  };
}

export default function App() {
  const [tab, setTab] = useState<Tab>(initialTabFromUrl);
  const [health, setHealth] = useState<HealthPayload | null>(null);
  const [config, setConfig] = useState<ConfigPayload | null>(null);
  const [markets, setMarkets] = useState<MarketsPayload | null>(null);
  const [alerts, setAlerts] = useState<AlertsPayload | null>(null);
  const [alertForm, setAlertForm] = useState<AlertForm>({
    market_id: "polymarket",
    contract_id: "",
    label: "",
    direction: "above",
    threshold: "",
    source: "last_trade",
    once: true,
    enabled: true
  });
  const [editingAlertId, setEditingAlertId] = useState<string | null>(null);
  const [alertMessage, setAlertMessage] = useState("");
  const [wallets, setWallets] = useState<WalletsPayload | null>(null);
  const [walletForm, setWalletForm] = useState<WalletForm>(emptyWalletForm());
  const [editingWalletId, setEditingWalletId] = useState<string | null>(null);
  const [walletMessage, setWalletMessage] = useState("");
  const [copyState, setCopyState] = useState<CopyPayload | null>(null);
  const [copyForm, setCopyForm] = useState<CopyForm>(copyToForm(null));
  const [copyPreviewForm, setCopyPreviewForm] = useState<CopyPreviewForm>(emptyCopyPreviewForm());
  const [copyPreview, setCopyPreview] = useState<CopyTradePreview | null>(null);
  const [liveSafety, setLiveSafety] = useState<LiveSafetyPayload | null>(null);
  const [liveValidation, setLiveValidation] = useState<PolymarketLiveValidationPayload | null>(null);
  const [liveValidationReportDetail, setLiveValidationReportDetail] = useState<PolymarketLiveValidationReportPayload | null>(null);
  const [liveValidationReports, setLiveValidationReports] = useState<PolymarketLiveValidationReportsPayload | null>(null);
  const [liveValidationDecisions, setLiveValidationDecisions] = useState<PolymarketLiveValidationDecisionLedgerPayload | null>(null);
  const [liveValidationPromotionProposal, setLiveValidationPromotionProposal] =
    useState<PolymarketLiveValidationPromotionProposalPayload | null>(null);
  const [liveValidationPromotionProposalTargetTier, setLiveValidationPromotionProposalTargetTier] = useState("");
  const [liveValidationPromotionProposalSnapshots, setLiveValidationPromotionProposalSnapshots] =
    useState<PolymarketLiveValidationPromotionProposalSnapshotsPayload | null>(null);
  const [liveValidationPromotionProposalSnapshotDetail, setLiveValidationPromotionProposalSnapshotDetail] =
    useState<PolymarketLiveValidationPromotionProposalSnapshotPayload | null>(null);
  const [liveValidationDecisionForm, setLiveValidationDecisionForm] = useState<LiveValidationDecisionForm>({
    target_tier: "credential_live_verified",
    decision: "rejected",
    reviewer: "operator",
    reviewer_note: ""
  });
  const [liveValidationImport, setLiveValidationImport] = useState("");
  const [liveValidationAllowDuplicate, setLiveValidationAllowDuplicate] = useState(false);
  const [liveValidationReportMessage, setLiveValidationReportMessage] = useState("");
  const [liveValidationReportSchemaValidation, setLiveValidationReportSchemaValidation] =
    useState<PolymarketLiveValidationReportSchemaValidation | null>(null);
  const [liveValidationReportBusyKey, setLiveValidationReportBusyKey] = useState<string | null>(null);
  const [livePreflight, setLivePreflight] = useState<LivePreflightPayload | null>(null);
  const [liveMessage, setLiveMessage] = useState("");
  const [paper, setPaper] = useState<PaperPayload | null>(null);
  const [paperForm, setPaperForm] = useState<PaperOrderForm>({
    market_id: "polymarket",
    contract_id: "",
    side: "BUY",
    size: "",
    limit_price: ""
  });
  const [paperMessage, setPaperMessage] = useState("");
  const [marketQuery, setMarketQuery] = useState("");
  const [marketReadForm, setMarketReadForm] = useState<MarketReadForm>(emptyMarketReadForm());
  const [marketRead, setMarketRead] = useState<MarketReadState>(emptyMarketReadState());
  const [marketReadBusy, setMarketReadBusy] = useState<string | null>(null);
  const [marketReadMessage, setMarketReadMessage] = useState("");
  const [analyticsLoading, setAnalyticsLoading] = useState(false);
  const [analyticsMessage, setAnalyticsMessage] = useState("");
  const [userSearchQuery, setUserSearchQuery] = useState("");
  const [userSearch, setUserSearch] = useState<PolymarketUserSearchPayload | null>(null);
  const [leaderboardFilters, setLeaderboardFilters] = useState<PolymarketLeaderboardFilters>(defaultLeaderboardFilters());
  const [leaderboard, setLeaderboard] = useState<PolymarketLeaderboardPayload | null>(null);
  const [mddForm, setMddForm] = useState<PolymarketMddForm>(defaultMddForm());
  const [walletMdd, setWalletMdd] = useState<PolymarketMddPayload | null>(null);
  const [mddAuditDetail, setMddAuditDetail] = useState<PolymarketMddAuditExport | null>(null);
  const [mddCache, setMddCache] = useState<PolymarketMddCachePayload | null>(null);
  const [mddCacheBusyKey, setMddCacheBusyKey] = useState<string | null>(null);
  const [busyMarket, setBusyMarket] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  async function loadAll() {
    setLoading(true);
    setError(null);
    try {
      const state = await fetchState();
      setHealth(state.health);
      setConfig(state.config);
      setMarkets(state.markets);
      setAlerts(state.alerts);
      setWallets(state.wallets);
      setCopyState(state.copy);
      setCopyForm(copyToForm(state.copy));
      setCopyPreviewForm((current) => ({ ...current, proxyWallet: current.proxyWallet || state.copy.settings.follow_wallet }));
      setLiveSafety(state.live_safety);
      setLiveValidation(state.polymarket_live_validation);
      setLiveValidationReports(state.polymarket_live_validation_reports);
      setLiveValidationReportSchemaValidation(state.polymarket_live_validation_reports.entries[0]?.schema_validation ?? null);
      setPaper(state.paper);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void loadAll();
  }, []);

  useEffect(() => {
    if (config?.selected_market_id) {
      setPaperForm((current) => ({ ...current, market_id: current.market_id || config.selected_market_id }));
      setAlertForm((current) => ({ ...current, market_id: current.market_id || config.selected_market_id }));
    }
  }, [config?.selected_market_id]);

  const filteredMarkets = useMemo(() => {
    const all = markets?.markets ?? [];
    const query = marketQuery.trim().toLowerCase();
    if (!query) {
      return all;
    }
    return all.filter((market) => {
      const capabilityText = enabledCapabilities(market).join(" ");
      return `${market.market_id} ${market.display_name} ${market.description} ${capabilityText}`
        .toLowerCase()
        .includes(query);
    });
  }, [marketQuery, markets]);

  const selectedMarket = useMemo(() => {
    if (!markets || !config) {
      return null;
    }
    return markets.markets.find((market) => market.market_id === config.selected_market_id) ?? null;
  }, [config, markets]);

  async function handleMarketToggle(market: Market) {
    setBusyMarket(market.market_id);
    setError(null);
    try {
      const payload = await updateMarket(market.market_id, { enabled: !market.enabled });
      setMarkets(payload);
      if (market.market_id === config?.selected_market_id) {
        setLiveSafety(await fetchLiveSafety());
      }
      if (market.market_id === "polymarket") {
        setLiveValidation(await fetchPolymarketLiveValidation());
      }
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusyMarket(null);
    }
  }

  async function handleMarketSettingsSave(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedMarket) {
      return;
    }
    const form = new FormData(event.currentTarget);
    const patch: MarketPatch = {
      enabled: form.get("enabled") === "on",
      live_trading_enabled: form.get("live_trading_enabled") === "on",
      live_trading_confirmed: form.get("live_trading_confirmed") === "on",
      live_trading_kill_switch: form.get("live_trading_kill_switch") === "on",
      live_trading_max_size: String(form.get("live_trading_max_size") ?? "").trim(),
      live_trading_max_notional: String(form.get("live_trading_max_notional") ?? "").trim()
    };
    if (selectedMarket.market_id === "betfair_exchange") {
      patch.settings = {
        betfair_order_management_enabled: form.get("betfair_order_management_enabled") === "on"
      };
    } else if (selectedMarket.market_id === "kalshi") {
      patch.settings = {
        kalshi_order_management_enabled: form.get("kalshi_order_management_enabled") === "on"
      };
    } else if (selectedMarket.market_id === "polymarket") {
      patch.settings = {
        polymarket_order_management_enabled: form.get("polymarket_order_management_enabled") === "on"
      };
    } else if (selectedMarket.market_id === "gemini_titan") {
      patch.settings = {
        gemini_order_management_enabled: form.get("gemini_order_management_enabled") === "on"
      };
    } else if (selectedMarket.market_id === "matchbook") {
      patch.settings = {
        matchbook_order_management_enabled: form.get("matchbook_order_management_enabled") === "on"
      };
    } else if (selectedMarket.market_id === "myriad_markets") {
      patch.settings = {
        myriad_order_management_enabled: form.get("myriad_order_management_enabled") === "on"
      };
    } else if (selectedMarket.market_id === "opinion_labs") {
      patch.settings = {
        opinion_order_management_enabled: form.get("opinion_order_management_enabled") === "on"
      };
    } else if (selectedMarket.market_id === "limitless_exchange") {
      patch.settings = {
        limitless_order_management_enabled: form.get("limitless_order_management_enabled") === "on"
      };
    } else if (selectedMarket.market_id === "smarkets") {
      patch.settings = {
        smarkets_order_management_enabled: form.get("smarkets_order_management_enabled") === "on"
      };
    } else if (selectedMarket.market_id === "probable") {
      patch.settings = {
        probable_order_management_enabled: form.get("probable_order_management_enabled") === "on"
      };
    } else if (selectedMarket.market_id === "hyperliquid") {
      patch.settings = {
        hyperliquid_order_management_enabled: form.get("hyperliquid_order_management_enabled") === "on"
      };
    } else if (selectedMarket.market_id === "predict_fun") {
      patch.settings = {
        predict_fun_order_management_enabled: form.get("predict_fun_order_management_enabled") === "on"
      };
    } else if (selectedMarket.market_id === "xmarket") {
      patch.settings = {
        xmarket_order_management_enabled: form.get("xmarket_order_management_enabled") === "on"
      };
    } else if (["ibkr_forecasttrader", "forecastex", "cme_prediction_markets"].includes(selectedMarket.market_id)) {
      patch.settings = {
        ibkr_order_management_enabled: form.get("ibkr_order_management_enabled") === "on"
      };
    } else if (selectedMarket.market_id === "manifold") {
      patch.settings = {
        manifold_order_management_enabled: form.get("manifold_order_management_enabled") === "on"
      };
    } else if (selectedMarket.market_id === "prophet_exchange") {
      patch.settings = {
        prophet_exchange_order_management_enabled: form.get("prophet_exchange_order_management_enabled") === "on"
      };
    }
    setBusyMarket(selectedMarket.market_id);
    setError(null);
    try {
      const payload = await updateMarket(selectedMarket.market_id, patch);
      setMarkets(payload);
      setLiveSafety(await fetchLiveSafety());
      if (selectedMarket.market_id === "polymarket") {
        setLiveValidation(await fetchPolymarketLiveValidation());
      }
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusyMarket(null);
    }
  }

  async function handleSelectedMarketChange(marketId: string) {
    setError(null);
    try {
      const payload = await updateConfig({ selected_market_id: marketId });
      setConfig(payload);
      setPaperForm((current) => ({ ...current, market_id: marketId }));
      setAlertForm((current) => ({ ...current, market_id: marketId }));
      setMarketRead(emptyMarketReadState());
      const nextMarket = markets?.markets.find((market) => market.market_id === marketId);
      const accountOperations = nextMarket?.health.account_recovery_operations ?? [];
      const orderOperations = nextMarket?.health.order_management_operations ?? [];
      setMarketReadForm({
        ...emptyMarketReadForm(),
        account_operation: accountOperations[0] as MarketAccountOperation ?? "active_orders",
        order_management_operation: (orderOperations[0] as MarketOrderManagementOperation) ?? "cancel_orders",
      });
      setMarketReadMessage("");
      setLivePreflight(null);
      setLiveMessage("");
      setLiveSafety(await fetchLiveSafety());
      setLiveValidation(await fetchPolymarketLiveValidation());
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }

  async function handleMarketRead(action: "events" | "contracts" | "price" | "orderbook" | "trades" | "candles" | "account") {
    const marketId = selectedMarket?.market_id;
    if (!marketId) {
      setError("Select an enabled market before reading market data.");
      return;
    }
    const form = marketReadForm;
    if (action !== "events" && action !== "contracts" && action !== "account" && !form.contract_id.trim()) {
      setError("Enter a contract id before reading quote or history data.");
      return;
    }
    if (action === "contracts" && !form.event_id.trim()) {
      setError("Enter an event id before loading contracts.");
      return;
    }
    if (action === "candles" && !form.resolution.trim()) {
      setError("Enter a candle resolution before loading history.");
      return;
    }
    setMarketReadBusy(action);
    setMarketReadMessage("");
    setError(null);
    try {
      if (action === "events") {
        const payload = await fetchMarketEvents(marketId, form.query);
        setMarketRead((current) => ({ ...current, events: payload }));
        setMarketReadMessage(`${payload.events.length} event(s) loaded.`);
      } else if (action === "contracts") {
        const payload = await fetchMarketContracts(marketId, form.event_id.trim());
        setMarketRead((current) => ({ ...current, contracts: payload }));
        setMarketReadMessage(`${payload.contracts.length} contract(s) loaded.`);
      } else if (action === "price") {
        const payload = await fetchMarketPrice(marketId, form.contract_id.trim());
        setMarketRead((current) => ({ ...current, price: payload }));
        setMarketReadMessage("Price snapshot loaded.");
      } else if (action === "orderbook") {
        const payload = await fetchMarketOrderbook(marketId, form.contract_id.trim());
        setMarketRead((current) => ({ ...current, orderbook: payload }));
        setMarketReadMessage("Orderbook loaded.");
      } else if (action === "trades") {
        const payload = await fetchMarketTrades(
          marketId,
          form.contract_id.trim(),
          50,
          form.to.trim(),
          form.from.trim()
        );
        setMarketRead((current) => ({ ...current, trades: payload }));
        setMarketReadMessage(`${payload.trades.length} trade(s) loaded.`);
      } else {
        if (action === "candles") {
          const payload = await fetchMarketCandles(
            marketId,
            form.contract_id.trim(),
            form.resolution.trim(),
            form.from.trim(),
            form.to.trim()
          );
          setMarketRead((current) => ({ ...current, candles: payload }));
          setMarketReadMessage(`${payload.candles.length} candle(s) loaded.`);
        } else {
          const payload = await fetchMarketAccount(marketId, form.account_operation, {
            contract_id: form.contract_id.trim() || undefined,
            ticker: form.account_ticker.trim() || undefined,
            order_id: form.account_order_id.trim() || undefined,
            wallet: form.account_wallet.trim() || undefined,
            address: form.account_wallet.trim() || undefined,
            token_id: marketId === "probable" ? (form.contract_id.trim().split(":").pop() || undefined) : undefined,
            token_ids: marketId === "probable" ? (form.contract_id.trim().split(":").pop() || undefined) : undefined,
            trade_id: form.account_trade_id.trim() || undefined,
            cursor: form.account_cursor.trim() || undefined,
            offset: form.account_offset.trim() || undefined,
            dex: form.account_dex.trim() || undefined,
            market_id: form.account_market_id.trim() || undefined,
            chain_id: form.account_chain_id.trim() || undefined,
            status: form.account_status.trim() || undefined,
            event_types: form.account_event_types.trim() || undefined,
            is_resolved: form.account_is_resolved.trim() || undefined,
            event_type_id: form.account_event_type_id.trim() || undefined,
            event_id: form.account_event_id.trim() || undefined,
            runner_id: form.account_runner_id.trim() || undefined,
            bet_id: form.account_bet_id.trim() || undefined,
            group_by: form.account_group_by.trim() || undefined,
            include_item_description: form.account_include_item_description,
            sport_id: form.account_sport_id.trim() || undefined,
            side: form.account_side.trim() || undefined,
            offer_status: form.account_offer_status.trim() || undefined,
            aggregation_type: form.account_aggregation_type.trim() || undefined,
            odds_type: form.account_odds_type.trim() || undefined,
            interval: form.account_interval.trim() || undefined,
            cancellation_reason: form.account_cancellation_reason.trim() || undefined,
            include_edits: form.account_include_edits,
            subaccount: form.account_subaccount.trim() || undefined,
            count_filter: form.account_count_filter.trim() || undefined,
            market_slug: form.account_market_slug.trim() || undefined,
            page: form.account_page.trim() || undefined,
            trading_model: form.account_trading_model.trim() || undefined,
            min_shares: form.account_min_shares.trim() || undefined,
            network_id: form.account_network_id.trim() || undefined,
            token_address: form.account_token_address.trim() || undefined,
            keyword: form.account_keyword.trim() || undefined,
            sort: form.account_sort.trim() || undefined,
            sort_by: form.account_sort_by.trim() || undefined,
            state: form.account_state.trim() || undefined,
            topics: form.account_topics.trim() || undefined,
            market_ids: form.account_market_ids.trim() || undefined,
            exclude_history: form.account_exclude_history,
            group_by_event: form.account_group_by_event,
            on_behalf_of: form.account_on_behalf_of.trim() || undefined,
            historical: form.account_historical,
            event_ticker: form.event_id.trim() || undefined,
            from: form.from.trim() || undefined,
            to: form.to.trim() || undefined,
            limit: marketId === "hyperliquid" ? 2000 : marketId === "opinion_labs" ? 20 : marketId === "betfair_exchange" ? 100 : marketId === "matchbook" && form.account_operation === "current_offers" ? 20 : 50,
            ...(marketId === "opinion_labs" ? { page: 1 } : {})
          });
          setMarketRead((current) => ({ ...current, account: payload }));
          setMarketReadMessage(`${payload.operation.replaceAll("_", " ")} loaded.`);
        }
      }
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setMarketReadBusy(null);
    }
  }

  async function handleMarketOrderManagement() {
    const marketId = selectedMarket?.market_id;
    if (!marketId) {
      setError("Select an enabled market before managing orders.");
      return;
    }
    const supported = selectedMarket.health.order_management_operations ?? [];
    const operation = marketReadForm.order_management_operation;
    if (!supported.includes(operation)) {
      setError(`${selectedMarket.display_name} does not advertise ${operation.replaceAll("_", " ")}.`);
      return;
    }
    const isKalshi = marketId === "kalshi";
    const isPolymarket = marketId === "polymarket";
    const isGemini = marketId === "gemini_titan";
    const isMatchbook = marketId === "matchbook";
    const isMyriad = marketId === "myriad_markets";
    const isOpinion = marketId === "opinion_labs";
    const isLimitless = marketId === "limitless_exchange";
    const isSmarkets = marketId === "smarkets";
    const isProbable = marketId === "probable";
    const isHyperliquid = marketId === "hyperliquid";
    const isPredictFun = marketId === "predict_fun";
    const isXmarket = marketId === "xmarket";
    const isIbkr = ["ibkr_forecasttrader", "forecastex", "cme_prediction_markets"].includes(marketId);
    const isManifold = marketId === "manifold";
    const isProphet = marketId === "prophet_exchange";
    let instructions: unknown = [];
    const needsInstructions =
      isHyperliquid ||
      isPredictFun ||
      isXmarket ||
      (isIbkr && operation === "modify_order") ||
      (isProphet && operation === "cancel_orders") ||
      (!isPolymarket && !isGemini && !isMatchbook && !isMyriad && !isOpinion && !isLimitless && !isSmarkets && !isProbable && !isPredictFun && !isXmarket && !isIbkr && !isManifold && !isProphet) ||
      (operation === "cancel_orders" && !isSmarkets && !isProbable && !isProphet) ||
      (isProbable && operation === "cancel_orders") ||
      operation === "batch_cancel_orders" ||
      (isMatchbook && (operation === "cancel_offers" || operation === "edit_offers")) ||
      (isMyriad && (operation === "cancel_order" || operation === "batch_modify_orders"));
    if (needsInstructions) {
      try {
        const defaultJson = (isMyriad && (operation === "cancel_order" || operation === "batch_modify_orders")) || isHyperliquid ? "{}" : "[]";
        instructions = JSON.parse(marketReadForm.order_management_instructions || defaultJson);
      } catch (exc) {
        setError(exc instanceof Error ? `Instructions JSON is invalid: ${exc.message}` : "Instructions JSON is invalid.");
        return;
      }
      const allowsObject = (isMyriad && (operation === "cancel_order" || operation === "batch_modify_orders")) || isHyperliquid || (isIbkr && operation === "modify_order");
      if (!Array.isArray(instructions) && !(allowsObject && instructions && typeof instructions === "object")) {
        setError(allowsObject ? "Instructions must be a JSON array or object." : "Instructions must be a JSON array.");
        return;
      }
    }
    const myriadInstructionObject = instructions && !Array.isArray(instructions) && typeof instructions === "object"
      ? instructions as Record<string, unknown>
      : null;
    const hyperliquidInstructionObject = isHyperliquid && instructions && !Array.isArray(instructions) && typeof instructions === "object"
      ? instructions as Record<string, unknown>
      : null;
    if (isHyperliquid && !hyperliquidInstructionObject) {
      setError("Hyperliquid order management requires a signed exchange action JSON object.");
      return;
    }
    if (isHyperliquid && operation === "schedule_cancel" &&
      marketReadForm.order_management_confirmation.trim() !== "SCHEDULE HYPERLIQUID CANCEL") {
      setError("Hyperliquid schedule_cancel requires exact confirmation: SCHEDULE HYPERLIQUID CANCEL.");
      return;
    }
    if (isPolymarket && operation === "cancel_order" && !marketReadForm.order_management_order_id.trim()) {
      setError("A Polymarket order id is required for cancel_order.");
      return;
    }
    if (isPolymarket && operation === "cancel_orders" && (!Array.isArray(instructions) || (instructions as unknown[]).length === 0)) {
      setError("Polymarket cancel_orders requires at least one order hash.");
      return;
    }
    if (isPolymarket && operation === "cancel_market_orders" && (!marketReadForm.order_management_market_id.trim() || !marketReadForm.contract_id.trim())) {
      setError("Polymarket cancel_market_orders requires a condition id and token id.");
      return;
    }
    if (isGemini && operation === "cancel_order" && !marketReadForm.order_management_order_id.trim()) {
      setError("A Gemini order id is required for cancel_order.");
      return;
    }
    if (isGemini && operation === "batch_cancel_orders" && !(instructions as unknown[]).length) {
      setError("Gemini batch cancellation requires at least one numeric order id.");
      return;
    }
    if (isMatchbook && operation === "cancel_offer" && !marketReadForm.order_management_order_id.trim()) {
      setError("A Matchbook offer id is required for cancel_offer.");
      return;
    }
    if (isMatchbook && operation === "cancel_offers" && !(instructions as unknown[]).length &&
      !marketReadForm.order_management_offer_ids.trim() &&
      !marketReadForm.order_management_event_ids.trim() && !marketReadForm.order_management_market_ids.trim() &&
      !marketReadForm.order_management_runner_ids.trim()) {
      setError("Matchbook cancel_offers requires offer ids, offer ids JSON, or a scope filter.");
      return;
    }
    if (isMatchbook && operation === "cancel_all_offers" &&
      marketReadForm.order_management_confirmation.trim() !== "CANCEL ALL MATCHBOOK OFFERS") {
      setError("Global Matchbook cancellation requires exact confirmation: CANCEL ALL MATCHBOOK OFFERS.");
      return;
    }
    if (isMatchbook && operation === "edit_offer" && !marketReadForm.order_management_order_id.trim()) {
      setError("A Matchbook offer id is required for edit_offer.");
      return;
    }
    if (isMatchbook && operation === "edit_offers" && !(instructions as unknown[]).length) {
      setError("Matchbook edit_offers requires an array of offer edit objects.");
      return;
    }
    if (isMyriad && operation === "cancel_order" && (!(marketReadForm.order_management_order_hash || marketReadForm.order_management_order_id).trim() || !myriadInstructionObject?.order || !myriadInstructionObject?.signature)) {
      setError("Myriad cancel_order requires an order hash/client id and a signed order object with signature.");
      return;
    }
    if (isMyriad && operation === "batch_cancel_orders" && !(instructions as unknown[]).length) {
      setError("Myriad batch cancellation requires at least one signed order entry.");
      return;
    }
    if (isMyriad && operation === "cancel_all_orders" &&
      (marketReadForm.order_management_confirmation.trim() !== "CANCEL ALL MYRIAD ORDERS" ||
        !marketReadForm.order_management_trader.trim() || !marketReadForm.order_management_timestamp.trim() ||
        !marketReadForm.order_management_signature.trim())) {
      setError("Myriad global cancellation requires trader, timestamp, signature, and exact confirmation: CANCEL ALL MYRIAD ORDERS.");
      return;
    }
    if (isMyriad && operation === "batch_modify_orders" &&
      (!myriadInstructionObject || (!Array.isArray(myriadInstructionObject.cancel) && !Array.isArray(myriadInstructionObject.place)))) {
      setError("Myriad batch_modify_orders requires a JSON object with cancel and/or place arrays.");
      return;
    }
    if (isOpinion && operation === "cancel_order" && !marketReadForm.order_management_order_id.trim()) {
      setError("An Opinion order id is required for cancel_order.");
      return;
    }
    if (isOpinion && operation === "batch_cancel_orders" && !(instructions as unknown[]).length) {
      setError("Opinion batch cancellation requires at least one order id.");
      return;
    }
    if (isOpinion && operation === "cancel_all_orders" &&
      marketReadForm.order_management_confirmation.trim() !== "CANCEL ALL OPINION ORDERS") {
      setError("Global Opinion cancellation requires exact confirmation: CANCEL ALL OPINION ORDERS.");
      return;
    }
    if (isLimitless && operation === "cancel_order" && !marketReadForm.order_management_order_id.trim()) {
      setError("A Limitless order id is required for cancel_order.");
      return;
    }
    if (isLimitless && operation === "batch_cancel_orders" &&
      (!Array.isArray(instructions) || !(instructions as unknown[]).length)) {
      setError("Limitless batch cancellation requires at least one order id.");
      return;
    }
    if (isLimitless && operation === "cancel_all_orders" &&
      (!marketReadForm.order_management_market_slug.trim() ||
        marketReadForm.order_management_confirmation.trim() !== "CANCEL ALL LIMITLESS ORDERS")) {
      setError("Limitless cancellation requires a market slug and exact confirmation: CANCEL ALL LIMITLESS ORDERS.");
      return;
    }
    if (isSmarkets && operation === "cancel_order" && !marketReadForm.order_management_order_id.trim()) {
      setError("A Smarkets order id is required for cancel_order.");
      return;
    }
    if (isSmarkets && operation === "cancel_orders" && !marketReadForm.order_management_market_id.trim()) {
      setError("A Smarkets market id is required for market-scoped cancel_orders.");
      return;
    }
    if (isProbable && operation === "cancel_order" &&
      (!marketReadForm.order_management_order_id.trim() || !marketReadForm.order_management_market_id.trim())) {
      setError("Probable cancel_order requires an order id and token id.");
      return;
    }
    if (isProbable && operation === "cancel_orders" &&
      (!Array.isArray(instructions) || !(instructions as unknown[]).length || !marketReadForm.order_management_market_id.trim())) {
      setError("Probable cancel_orders requires a token id and a JSON array of order ids.");
      return;
    }
    if (isProbable && operation === "cancel_all_orders" &&
      marketReadForm.order_management_confirmation.trim() !== "CANCEL ALL PROBABLE ORDERS") {
      setError("Global Probable cancellation requires exact confirmation: CANCEL ALL PROBABLE ORDERS.");
      return;
    }
    if (isPredictFun && (!Array.isArray(instructions) || !(instructions as unknown[]).length)) {
      setError("Predict.fun removal requires a non-empty JSON array of order ids or hashes.");
      return;
    }
    if (isPredictFun && marketReadForm.order_management_operator_confirmation.trim() !== "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS") {
      setError("Predict.fun removal requires exact confirmation: I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS.");
      return;
    }
    if (isXmarket && !(instructions as unknown[]).length) {
      setError("Xmarket batch order operations require a non-empty JSON array.");
      return;
    }
    if (isXmarket && marketReadForm.order_management_operator_confirmation.trim() !== "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS") {
      setError("Xmarket order management requires exact confirmation: I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS.");
      return;
    }
    if (isManifold && operation === "cancel_order" && !marketReadForm.order_management_order_id.trim()) {
      setError("A Manifold bet id is required for cancel_order.");
      return;
    }
    if (isManifold && marketReadForm.order_management_operator_confirmation.trim() !== "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS") {
      setError("Manifold order management requires exact confirmation: I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS.");
      return;
    }
    if (isProphet && operation === "cancel_order" &&
      (!marketReadForm.order_management_order_id.trim() || !marketReadForm.order_management_external_id.trim())) {
      setError("Prophet Exchange cancel_order requires both the returned order id and the original external id.");
      return;
    }
    if (isProphet && operation === "cancel_orders" &&
      (!Array.isArray(instructions) || !(instructions as unknown[]).length)) {
      setError("Prophet Exchange cancel_orders requires a non-empty JSON array of order_id/external_id entries.");
      return;
    }
    if (isProphet && marketReadForm.order_management_operator_confirmation.trim() !== "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS") {
      setError("Prophet Exchange order management requires exact confirmation: I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS.");
      return;
    }
    if (isIbkr && operation === "cancel_order" && !marketReadForm.order_management_order_id.trim()) {
      setError("An IBKR order id is required for cancel_order.");
      return;
    }
    if (isIbkr && operation === "cancel_all_orders" && marketReadForm.order_management_confirmation.trim() !== "CANCEL ALL IBKR EVENT ORDERS") {
      setError("Global IBKR cancellation requires exact confirmation: CANCEL ALL IBKR EVENT ORDERS.");
      return;
    }
    if (isIbkr && operation === "modify_order" && (!instructions || Array.isArray(instructions) || typeof instructions !== "object")) {
      setError("IBKR modify_order requires one JSON order object.");
      return;
    }
    if (isIbkr && marketReadForm.order_management_operator_confirmation.trim() !== "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS") {
      setError("IBKR order management requires exact confirmation: I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS.");
      return;
    }
    if (!isKalshi && !isPolymarket && !isGemini && !isMatchbook && !isMyriad && !isOpinion && !isLimitless && !isHyperliquid && !isXmarket && !isIbkr && !isProphet && (operation === "update_orders" || operation === "replace_orders") && !marketReadForm.order_management_market_id.trim()) {
      setError("A Betfair exchange market id is required for update and replace operations.");
      return;
    }
    if (isKalshi && operation === "batch_cancel_orders" && !(instructions as unknown[]).length) {
      setError("Kalshi batch cancellation requires at least one order object.");
      return;
    }
    if (isKalshi && operation !== "batch_cancel_orders" && !marketReadForm.order_management_order_id.trim()) {
      setError("A Kalshi order id is required for this mutation.");
      return;
    }
    if (isKalshi && operation === "amend_order" && !marketReadForm.order_management_ticker.trim()) {
      setError("A Kalshi ticker is required for amend_order.");
      return;
    }
    const warning = !isKalshi && !isPolymarket && !isGemini && !isMatchbook && !isMyriad && !isOpinion && !isLimitless && !isSmarkets && !isProbable && !isHyperliquid && !isPredictFun && !isXmarket && !isIbkr && !isManifold && !isProphet && operation === "cancel_orders" && !marketReadForm.order_management_market_id.trim()
      ? "This submits a GLOBAL Betfair cancellation for the account."
      : `This submits a live ${isKalshi ? "Kalshi" : isPolymarket ? "Polymarket" : isGemini ? "Gemini" : isMatchbook ? "Matchbook" : isMyriad ? "Myriad" : isOpinion ? "Opinion" : isLimitless ? "Limitless" : isSmarkets ? "Smarkets" : isProbable ? "Probable" : isHyperliquid ? "Hyperliquid" : isPredictFun ? "Predict.fun relay-only" : isXmarket ? "Xmarket" : isIbkr ? "IBKR event-contract" : isManifold ? "Manifold" : isProphet ? "Prophet Exchange" : "Betfair"} ${operation.replaceAll("_", " ")} request.`;
    if (!window.confirm(`${warning} Continue only if the live-safety gates and request details are intentional.`)) {
      return;
    }
    const payload: Record<string, unknown> = isKalshi
      ? { instructions }
      : isPolymarket
        ? {
            market_id: marketReadForm.order_management_market_id.trim() || undefined,
            asset_id: marketReadForm.contract_id.trim() || undefined,
            order_id: marketReadForm.order_management_order_id.trim() || undefined,
            orders: operation === "cancel_orders" ? instructions : undefined,
            confirm_order_management: marketReadForm.order_management_operator_confirmation.trim() || undefined
          }
      : isGemini
        ? {
            order_id: marketReadForm.order_management_order_id.trim() || undefined,
            orders: operation === "batch_cancel_orders" ? instructions : undefined,
            confirm_order_management: marketReadForm.order_management_operator_confirmation.trim() || undefined
          }
      : isMyriad
        ? {
            order_hash: (marketReadForm.order_management_order_hash || marketReadForm.order_management_order_id).trim() || undefined,
            order_id: marketReadForm.order_management_order_id.trim() || undefined,
            instructions: operation === "cancel_order" ? myriadInstructionObject : undefined,
            orders: operation === "batch_cancel_orders" ? instructions : undefined,
            modify: operation === "batch_modify_orders" ? myriadInstructionObject : undefined,
            market_id: marketReadForm.order_management_market_id.trim() || undefined,
            trader: marketReadForm.order_management_trader.trim() || undefined,
            timestamp: marketReadForm.order_management_timestamp.trim() || undefined,
            signature: marketReadForm.order_management_signature.trim() || (myriadInstructionObject?.signature as string | undefined),
            signature_type: marketReadForm.order_management_signature_type.trim() || undefined,
            network_id: marketReadForm.order_management_network_id.trim() || undefined,
            allow_partial: marketReadForm.order_management_allow_partial,
            confirm_order_management: marketReadForm.order_management_operator_confirmation.trim() || undefined,
            confirm_global_cancel: marketReadForm.order_management_confirmation.trim() || undefined
          }
      : isOpinion
        ? {
            market_id: marketReadForm.order_management_market_id.trim() || undefined,
            order_id: marketReadForm.order_management_order_id.trim() || undefined,
            orders: operation === "batch_cancel_orders" ? instructions : undefined,
            side: marketReadForm.order_management_side.trim() || undefined,
            confirm_order_management: marketReadForm.order_management_operator_confirmation.trim() || undefined,
            confirm_global_cancel: marketReadForm.order_management_confirmation.trim() || undefined
          }
      : isLimitless
        ? {
            market_slug: marketReadForm.order_management_market_slug.trim() || undefined,
            order_id: marketReadForm.order_management_order_id.trim() || undefined,
            orders: operation === "batch_cancel_orders" ? instructions : undefined,
            confirm_order_management: marketReadForm.order_management_operator_confirmation.trim() || undefined,
            confirm_global_cancel: marketReadForm.order_management_confirmation.trim() || undefined
          }
      : isSmarkets
        ? {
            market_id: marketReadForm.order_management_market_id.trim() || undefined,
            order_id: marketReadForm.order_management_order_id.trim() || undefined,
            confirm_order_management: marketReadForm.order_management_operator_confirmation.trim() || undefined
          }
      : isProbable
        ? {
            order_id: marketReadForm.order_management_order_id.trim() || undefined,
            token_id: marketReadForm.order_management_market_id.trim() || undefined,
            order_ids: operation === "cancel_orders" ? instructions : undefined,
            confirm_order_management: marketReadForm.order_management_operator_confirmation.trim() || undefined,
            confirm_global_cancel: marketReadForm.order_management_confirmation.trim() || undefined
          }
      : isHyperliquid
        ? {
            signed_action: hyperliquidInstructionObject,
            confirm_order_management: marketReadForm.order_management_operator_confirmation.trim() || undefined,
            confirm_global_cancel: marketReadForm.order_management_confirmation.trim() || undefined
          }
      : isPredictFun
        ? {
            orders: instructions,
            confirm_order_management: marketReadForm.order_management_operator_confirmation.trim() || undefined
          }
      : isXmarket
        ? {
            orders: instructions,
            confirm_order_management: marketReadForm.order_management_operator_confirmation.trim() || undefined
          }
      : isManifold
        ? {
            order_id: marketReadForm.order_management_order_id.trim() || undefined,
            confirm_order_management: marketReadForm.order_management_operator_confirmation.trim() || undefined
          }
      : isProphet
        ? {
            order_id: marketReadForm.order_management_order_id.trim() || undefined,
            external_id: marketReadForm.order_management_external_id.trim() || undefined,
            orders: operation === "cancel_orders" ? instructions : undefined,
            confirm_order_management: marketReadForm.order_management_operator_confirmation.trim() || undefined
          }
      : isIbkr
        ? {
            order_id: marketReadForm.order_management_order_id.trim() || undefined,
            instructions: operation === "modify_order" ? instructions : undefined,
            confirm_order_management: marketReadForm.order_management_operator_confirmation.trim() || undefined,
            confirm_global_cancel: marketReadForm.order_management_confirmation.trim() || undefined
          }
      : isMatchbook
        ? {
            order_id: marketReadForm.order_management_order_id.trim() || undefined,
            offer_id: marketReadForm.order_management_order_id.trim() || undefined,
            offer_ids: marketReadForm.order_management_offer_ids.trim() || undefined,
            orders: operation === "cancel_offers" && (instructions as unknown[]).length ? instructions : undefined,
            instructions: operation === "edit_offers" ? instructions : undefined,
            event_ids: marketReadForm.order_management_event_ids.trim() || undefined,
            market_ids: marketReadForm.order_management_market_ids.trim() || undefined,
            runner_ids: marketReadForm.order_management_runner_ids.trim() || undefined,
            current_odds: marketReadForm.order_management_current_odds.trim() || undefined,
            new_odds: marketReadForm.order_management_new_odds.trim() || undefined,
            current_stake: marketReadForm.order_management_current_stake.trim() || undefined,
            new_stake: marketReadForm.order_management_new_stake.trim() || undefined,
            confirm_order_management: marketReadForm.order_management_operator_confirmation.trim() || undefined,
            confirm_global_cancel: marketReadForm.order_management_confirmation.trim() || undefined
          }
      : {
          market_id: marketReadForm.order_management_market_id.trim() || undefined,
          instructions,
          customer_ref: marketReadForm.order_management_customer_ref.trim() || undefined,
          confirm_global_cancel: marketReadForm.order_management_confirmation.trim() || undefined
        };
    if (!isKalshi && !isPolymarket && !isGemini && !isMatchbook && !isMyriad && !isOpinion && !isLimitless && !isSmarkets && !isProbable && !isHyperliquid && !isXmarket && !isIbkr && !isManifold && !isProphet && marketReadForm.order_management_market_version.trim()) {
      const version = Number(marketReadForm.order_management_market_version.trim());
      if (!Number.isInteger(version) || version < 1) {
        setError("Market version must be a positive integer.");
        return;
      }
      payload.market_version = version;
    }
    if (!isKalshi && !isPolymarket && !isGemini && !isMatchbook && !isMyriad && !isOpinion && !isLimitless && !isSmarkets && !isProbable && !isHyperliquid && !isXmarket && !isIbkr && !isManifold && !isProphet && marketReadForm.order_management_async) {
      payload.async_request = true;
    }
    if (isKalshi) {
      if (operation === "batch_cancel_orders") {
        payload.orders = instructions;
      }
      const optionalText: Array<[string, string]> = [
        ["order_id", marketReadForm.order_management_order_id],
        ["ticker", marketReadForm.order_management_ticker],
        ["side", marketReadForm.order_management_side],
        ["price", marketReadForm.order_management_price],
        ["count", marketReadForm.order_management_count],
        ["client_order_id", marketReadForm.order_management_client_order_id],
        ["updated_client_order_id", marketReadForm.order_management_updated_client_order_id],
        ["reduce_by", marketReadForm.order_management_reduce_by],
        ["reduce_to", marketReadForm.order_management_reduce_to],
        ["confirm_order_management", marketReadForm.order_management_operator_confirmation]
      ];
      optionalText.forEach(([key, value]) => {
        if (value.trim()) {
          payload[key] = value.trim();
        }
      });
      if (marketReadForm.order_management_subaccount.trim()) {
        payload.subaccount = Number(marketReadForm.order_management_subaccount.trim());
      }
      if (marketReadForm.order_management_exchange_index.trim()) {
        payload.exchange_index = Number(marketReadForm.order_management_exchange_index.trim());
      }
    }
    setMarketReadBusy("order_management");
    setMarketReadMessage("");
    setError(null);
    try {
      const result = await manageMarketOrders(marketId, operation, payload);
      setMarketRead((current) => ({ ...current, order_management: result }));
      setMarketReadMessage(`${operation.replaceAll("_", " ")} request accepted by the API.`);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setMarketReadBusy(null);
    }
  }

  async function handleThemeChange(theme: Theme) {
    setError(null);
    try {
      const payload = await updateConfig({ theme });
      setConfig(payload);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }

  async function handleUiDesignChange(uiDesign: UiDesign) {
    setError(null);
    try {
      const payload = await updateConfig({ ui_design: uiDesign });
      setConfig(payload);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }

  async function handleUserSearch(event?: FormEvent<HTMLFormElement>) {
    event?.preventDefault();
    setError(null);
    setAnalyticsMessage("");
    setAnalyticsLoading(true);
    try {
      const payload = await searchPolymarketUsers(userSearchQuery, 10);
      setUserSearch(payload);
      setAnalyticsMessage(payload.counts.profiles ? `Found ${payload.counts.profiles} profile(s).` : "No matching profiles found.");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setAnalyticsLoading(false);
    }
  }

  async function handleLeaderboardRefresh(event?: FormEvent<HTMLFormElement>) {
    event?.preventDefault();
    setError(null);
    setAnalyticsMessage("");
    setAnalyticsLoading(true);
    try {
      const payload = await fetchPolymarketLeaderboard(leaderboardFilters);
      setLeaderboard(payload);
      const warning = payload.warnings.length ? ` ${payload.warnings[0]}` : "";
      const cache = payload.analytics_cache.enabled ? ` Audit cache entries: ${payload.analytics_cache.entries}.` : "";
      const completion = ` Scan ended: ${payload.completion_reason.replaceAll("_", " ")}. ${payload.source_scope_note}`;
      setAnalyticsMessage(
        `Loaded ${payload.counts.returned} trader row(s) from ${payload.counts.scanned} scanned rows; computed MDD for ${payload.counts.mdd_computed}.${cache}${completion}${warning}`
      );
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setAnalyticsLoading(false);
    }
  }

  async function handleMddLookup(event?: FormEvent<HTMLFormElement>) {
    event?.preventDefault();
    setError(null);
    setAnalyticsMessage("");
    setAnalyticsLoading(true);
    try {
      const payload = await fetchPolymarketMdd(mddForm);
      setWalletMdd(payload);
      if (payload.audit_cache?.key) {
        setMddAuditDetail({
          cache: payload.audit_cache,
          payload,
          export: { format: "json", source: "direct_wallet_mdd" }
        });
      }
      const cache = payload.audit_cache?.key ? ` Audit cache key ${payload.audit_cache.key.slice(0, 8)}.` : "";
      setAnalyticsMessage(`Computed wallet MDD ${formatUsd(payload.mdd_usd)} / ${formatPercent(payload.mdd_pct)}.${cache}`);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setAnalyticsLoading(false);
    }
  }

  async function handleAuditDetailLoad(cacheKey: string) {
    setError(null);
    setAnalyticsMessage("");
    setAnalyticsLoading(true);
    try {
      const payload = await fetchPolymarketMddAudit(cacheKey);
      setMddAuditDetail(payload);
      setAnalyticsMessage(`Loaded cached MDD audit ${cacheKey.slice(0, 8)}.`);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setAnalyticsLoading(false);
    }
  }

  async function handleMddCacheRefresh() {
    setError(null);
    setAnalyticsMessage("");
    setAnalyticsLoading(true);
    try {
      const payload = await fetchPolymarketMddCache(true);
      setMddCache(payload);
      setAnalyticsMessage(
        `MDD audit cache has ${payload.counts.entries} artifact(s), ${payload.counts.expired_entries} expired.`
      );
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setAnalyticsLoading(false);
    }
  }

  async function handleMddCachePurge(request: PolymarketMddCachePurgeRequest) {
    if (request.all && !window.confirm("Clear all cached Polymarket MDD audit artifacts?")) {
      return;
    }
    setError(null);
    setAnalyticsMessage("");
    setMddCacheBusyKey(request.key ?? (request.expired_only ? "__expired__" : "__all__"));
    try {
      const payload = await purgePolymarketMddCache(request);
      setMddCache(payload);
      if (payload.deleted_keys?.includes(mddAuditDetail?.cache?.key ?? "")) {
        setMddAuditDetail(null);
      }
      setAnalyticsMessage(payload.message ?? `Purged ${payload.deleted ?? 0} MDD audit artifact(s).`);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setMddCacheBusyKey(null);
    }
  }

  async function handleAlertSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setAlertMessage("");
    try {
      const payload = editingAlertId ? await updateAlert(editingAlertId, alertForm) : await createAlert(alertForm);
      setAlerts(payload);
      setAlertMessage(editingAlertId ? "Alert updated." : "Alert added.");
      setEditingAlertId(null);
      setAlertForm(emptyAlertForm(alertForm.market_id));
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }

  async function handleAlertAction(action: string, alert?: PriceAlert) {
    setError(null);
    setAlertMessage("");
    try {
      if (action === "edit" && alert) {
        setEditingAlertId(alert.id);
        setAlertForm(alertToForm(alert));
        setAlertMessage(`Editing ${alert.label}.`);
      } else if (action === "cancel-edit") {
        setEditingAlertId(null);
        setAlertForm(emptyAlertForm(config?.selected_market_id ?? "polymarket"));
      } else if (action === "toggle" && alert) {
        const payload = await updateAlert(alert.id, { enabled: !alert.enabled });
        setAlerts(payload);
        setAlertMessage(`${alert.label} ${alert.enabled ? "disabled" : "enabled"}.`);
      } else if (action === "refresh" && alert) {
        const payload = await refreshAlert(alert.id);
        setAlerts(payload.alerts);
        setAlertMessage(payload.message);
      } else if (action === "refresh-all") {
        const payload = await refreshAlerts();
        setAlerts(payload.alerts);
        setAlertMessage(payload.problems.length ? `${payload.message} ${payload.problems.length} problem(s).` : payload.message);
      } else if (action === "delete" && alert) {
        if (!window.confirm(`Delete alert "${alert.label}"?`)) {
          return;
        }
        const payload = await deleteAlert(alert.id);
        setAlerts(payload);
        setAlertMessage(`Deleted ${alert.label}.`);
        if (editingAlertId === alert.id) {
          setEditingAlertId(null);
          setAlertForm(emptyAlertForm(config?.selected_market_id ?? "polymarket"));
        }
      }
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }

  async function handleWalletSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setWalletMessage("");
    try {
      const payload = editingWalletId ? await updateWallet(editingWalletId, walletForm) : await createWallet(walletForm);
      setWallets(payload);
      setWalletMessage(editingWalletId ? "Wallet watch updated." : "Wallet watch added.");
      setEditingWalletId(null);
      setWalletForm(emptyWalletForm());
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }

  async function handleWalletAction(action: string, wallet?: WalletWatch) {
    setError(null);
    setWalletMessage("");
    try {
      if (action === "edit" && wallet) {
        setEditingWalletId(wallet.id);
        setWalletForm(walletToForm(wallet));
        setWalletMessage(`Editing ${wallet.display_name || wallet.wallet}.`);
      } else if (action === "cancel-edit") {
        setEditingWalletId(null);
        setWalletForm(emptyWalletForm());
      } else if (action === "toggle" && wallet) {
        const payload = await updateWallet(wallet.id, { wallet: wallet.wallet, enabled: !wallet.enabled });
        setWallets(payload);
        setWalletMessage(`${wallet.display_name || wallet.wallet} ${wallet.enabled ? "disabled" : "enabled"}.`);
      } else if (action === "delete" && wallet) {
        if (!window.confirm(`Delete wallet watch "${wallet.display_name || wallet.wallet}"?`)) {
          return;
        }
        const payload = await deleteWallet(wallet.id);
        setWallets(payload);
        setWalletMessage("Wallet watch deleted.");
      } else if (action === "poll") {
        const payload = await pollWallets();
        setWallets(payload.wallets);
        setCopyState(payload.copy);
        setCopyForm(copyToForm(payload.copy));
        setWalletMessage(payload.problems.length ? `${payload.message} ${payload.problems.length} problem(s).` : payload.message);
      } else if (action === "save-polling") {
        const payload = await updateWalletPolling(wallets?.polling.poll_interval_seconds ?? 10);
        setWallets(payload);
        setWalletMessage(payload.polling.last_message);
      }
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }

  async function handleCopySubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setWalletMessage("");
    try {
      const payload = await updateCopySettings(copyForm);
      setCopyState(payload);
      setCopyForm(copyToForm(payload));
      setCopyPreviewForm((current) => ({ ...current, proxyWallet: current.proxyWallet || payload.settings.follow_wallet }));
      setWalletMessage("Copy settings saved.");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }

  async function handleCopyPreview() {
    setError(null);
    setWalletMessage("");
    try {
      const payload = await previewCopyTrade(copyPreviewForm);
      setCopyState(payload.copy);
      setCopyForm(copyToForm(payload.copy));
      setCopyPreview(payload.preview);
      setWalletMessage(payload.preview.message ?? payload.preview.reason ?? "Copy preview updated.");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }

  async function handleClearHistory() {
    if (!window.confirm("Clear all local paper-order history?")) {
      return;
    }
    setError(null);
    try {
      const payload = await clearPaperHistory();
      setPaper(payload);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }

  async function handleLivePreflight() {
    setError(null);
    setLiveMessage("");
    try {
      const payload = await previewLivePreflight(paperForm);
      setLivePreflight(payload);
      setLiveSafety(payload.live_safety);
      setLiveMessage(payload.message);
      setPaperMessage(payload.message);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }

  async function handleLiveValidationRefresh() {
    setError(null);
    try {
      setLiveValidation(await fetchPolymarketLiveValidation());
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }

  async function handleLiveValidationReportsRefresh() {
    setError(null);
    setLiveValidationReportMessage("");
    try {
      const payload = await fetchPolymarketLiveValidationReports();
      setLiveValidationReports(payload);
      setLiveValidationReportSchemaValidation(payload.entries[0]?.schema_validation ?? null);
      if (liveValidationPromotionProposal) {
        setLiveValidationPromotionProposal(
          await fetchPolymarketLiveValidationPromotionProposal(liveValidationPromotionProposalTargetTier)
        );
      }
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }

  async function handleLiveValidationPromotionProposalRefresh(
    targetTier = liveValidationPromotionProposalTargetTier,
    announce = true
  ) {
    setError(null);
    setLiveValidationReportMessage("");
    setLiveValidationReportBusyKey("promotion-proposal");
    try {
      const proposal = await fetchPolymarketLiveValidationPromotionProposal(targetTier);
      setLiveValidationPromotionProposal(proposal);
      if (announce) {
        setLiveValidationReportMessage(
          `Loaded promotion proposal: ${proposal.counts.accepted_candidates} accepted, ${proposal.counts.stale_decisions} stale.`
        );
      }
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setLiveValidationReportBusyKey(null);
    }
  }

  async function handleLiveValidationPromotionProposalTargetChange(value: string) {
    setLiveValidationPromotionProposalTargetTier(value);
    await handleLiveValidationPromotionProposalRefresh(value, true);
  }

  async function handleLiveValidationPromotionProposalSnapshotsRefresh(announce = true) {
    setError(null);
    if (announce) {
      setLiveValidationReportMessage("");
    }
    setLiveValidationReportBusyKey("promotion-proposal-snapshots");
    try {
      const snapshots = await fetchPolymarketLiveValidationPromotionProposalSnapshots();
      setLiveValidationPromotionProposalSnapshots(snapshots);
      if (announce) {
        setLiveValidationReportMessage(
          `Loaded ${snapshots.counts.entries} proposal snapshot(s), ${snapshots.counts.stale} stale.`
        );
      }
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setLiveValidationReportBusyKey(null);
    }
  }

  async function handleLiveValidationPromotionProposalSnapshotStore() {
    setError(null);
    setLiveValidationReportMessage("");
    setLiveValidationReportBusyKey("promotion-proposal-snapshot-store");
    try {
      const snapshots = await storePolymarketLiveValidationPromotionProposalSnapshot({
        target_tier: liveValidationPromotionProposalTargetTier,
        source: "react_preview"
      });
      setLiveValidationPromotionProposalSnapshots(snapshots);
      const storedKey = snapshots.stored?.key ?? "";
      if (storedKey) {
        setLiveValidationPromotionProposalSnapshotDetail(
          await fetchPolymarketLiveValidationPromotionProposalSnapshot(storedKey)
        );
      }
      setLiveValidationReportMessage(snapshots.message ?? "Stored promotion proposal snapshot.");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setLiveValidationReportBusyKey(null);
    }
  }

  async function handleLiveValidationPromotionProposalSnapshotOpen(key: string) {
    if (!key) {
      return;
    }
    setError(null);
    setLiveValidationReportMessage("");
    setLiveValidationReportBusyKey(key);
    try {
      const snapshot = await fetchPolymarketLiveValidationPromotionProposalSnapshot(key);
      setLiveValidationPromotionProposalSnapshotDetail(snapshot);
      setLiveValidationReportMessage(`Opened proposal snapshot ${key}.`);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setLiveValidationReportBusyKey(null);
    }
  }

  async function handleLiveValidationPromotionProposalSnapshotDelete(key: string) {
    if (!key || !window.confirm("Delete this promotion proposal snapshot?")) {
      return;
    }
    setError(null);
    setLiveValidationReportMessage("");
    setLiveValidationReportBusyKey(key);
    try {
      const snapshots = await deletePolymarketLiveValidationPromotionProposalSnapshot(key);
      setLiveValidationPromotionProposalSnapshots(snapshots);
      if (liveValidationPromotionProposalSnapshotDetail?.entry.key === key) {
        setLiveValidationPromotionProposalSnapshotDetail(null);
      }
      setLiveValidationReportMessage(snapshots.message ?? "Deleted promotion proposal snapshot.");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setLiveValidationReportBusyKey(null);
    }
  }

  async function handleLiveValidationReportOpen(key: string) {
    if (!key) {
      return;
    }
    setError(null);
    setLiveValidationReportMessage("");
    setLiveValidationReportBusyKey(key);
    try {
      const payload = await fetchPolymarketLiveValidationReport(key);
      setLiveValidationReportDetail(payload);
      const ledger = await fetchPolymarketLiveValidationDecisions(key);
      setLiveValidationDecisions(ledger);
      setLiveValidationReportSchemaValidation(payload.entry.schema_validation ?? null);
      setLiveValidationReportMessage(`Opened report ${key}.`);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setLiveValidationReportBusyKey(null);
    }
  }

  async function handleLiveValidationSnapshotStore() {
    setError(null);
    setLiveValidationReportMessage("");
    setLiveValidationReportBusyKey("store-current");
    try {
      const payload = await storePolymarketLiveValidationReport({ source: "gui_snapshot", label: "GUI readiness snapshot" });
      setLiveValidationReports(payload);
      setLiveValidationReportSchemaValidation(payload.stored?.schema_validation ?? null);
      setLiveValidationReportMessage(payload.message ?? "Stored GUI readiness snapshot.");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setLiveValidationReportBusyKey(null);
    }
  }

  async function handleLiveValidationReportImport() {
    const reportJson = liveValidationImport.trim();
    if (!reportJson) {
      setLiveValidationReportMessage("Paste a CLI JSON report before importing.");
      return;
    }
    setError(null);
    setLiveValidationReportMessage("");
    setLiveValidationReportBusyKey("import-json");
    try {
      const payload = await storePolymarketLiveValidationReport({
        source: "cli_import",
        label: "CLI import",
        report_json: reportJson,
        allow_duplicate: liveValidationAllowDuplicate,
        skip_duplicate: !liveValidationAllowDuplicate
      });
      setLiveValidationReports(payload);
      setLiveValidationReportSchemaValidation(payload.stored?.schema_validation ?? null);
      setLiveValidationImport("");
      setLiveValidationReportMessage(payload.message ?? "Imported CLI validation report.");
    } catch (exc) {
      if (exc instanceof ApiRequestError) {
        const validation = apiSchemaValidation(exc.details);
        if (validation) {
          setLiveValidationReportSchemaValidation(validation);
          setLiveValidationReportMessage(exc.message);
          return;
        }
      }
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setLiveValidationReportBusyKey(null);
    }
  }

  async function handleLiveValidationReportDelete(key: string) {
    if (!key || !window.confirm("Delete this stored live validation report?")) {
      return;
    }
    setError(null);
    setLiveValidationReportMessage("");
    setLiveValidationReportBusyKey(key);
    try {
      const payload = await deletePolymarketLiveValidationReport(key);
      setLiveValidationReports(payload);
      if (liveValidationReportDetail?.entry.key === key) {
        setLiveValidationReportDetail(null);
      }
      setLiveValidationReportSchemaValidation(payload.entries[0]?.schema_validation ?? null);
      setLiveValidationReportMessage(payload.message ?? "Deleted live validation report.");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setLiveValidationReportBusyKey(null);
    }
  }

  async function handleLiveValidationDecisionSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const reportKey = liveValidationReportDetail?.entry.key ?? "";
    if (!reportKey) {
      setLiveValidationReportMessage("Open a report before recording a promotion decision.");
      return;
    }
    if (!liveValidationDecisionForm.reviewer_note.trim()) {
      setLiveValidationReportMessage("Reviewer note is required for a promotion decision.");
      return;
    }
    setError(null);
    setLiveValidationReportMessage("");
    setLiveValidationReportBusyKey("decision-ledger");
    try {
      const review = await fetchPolymarketLiveValidationReportReview(reportKey);
      const payloadHash = review.bundle.report.payload_hash ?? liveValidationReportDetail?.entry.payload_hash ?? "";
      const reviewBundleHash = String(review.bundle.review_bundle_hash ?? "");
      const ledger = await storePolymarketLiveValidationDecision({
        report_key: reportKey,
        payload_hash: payloadHash,
        target_tier: liveValidationDecisionForm.target_tier,
        decision: liveValidationDecisionForm.decision,
        reviewer: liveValidationDecisionForm.reviewer,
        reviewer_note: liveValidationDecisionForm.reviewer_note,
        review_bundle_hash: reviewBundleHash
      });
      setLiveValidationDecisions(ledger);
      setLiveValidationPromotionProposal(
        await fetchPolymarketLiveValidationPromotionProposal(liveValidationPromotionProposalTargetTier)
      );
      setLiveValidationDecisionForm((current) => ({ ...current, reviewer_note: "" }));
      setLiveValidationReportMessage(ledger.message ?? "Recorded promotion decision.");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setLiveValidationReportBusyKey(null);
    }
  }

  async function handlePaperAction(action: string, target?: { market_id: string; contract_id: string; id?: string }) {
    setError(null);
    setPaperMessage("");
    try {
      if (action === "quote") {
        const quote = await refreshPaperQuote(paperForm);
        setPaperMessage(quote.message);
      } else if (action === "quote-limit") {
        const result = await fillPaperQuoteLimit(paperForm);
        setPaperForm((current) => ({ ...current, limit_price: String(result.limit_price) }));
        setPaperMessage(result.message);
      } else if (action === "impact") {
        const result = await previewPaperImpact(paperForm);
        setPaperMessage(result.message);
      } else if (action === "live-preflight") {
        await handleLivePreflight();
      } else if (action === "submit") {
        const result = await submitPaperOrder(paperForm);
        setPaper(result.paper);
        setPaperMessage(result.result.message);
      } else if (action === "refresh-marks") {
        const result = await refreshPaperMarks();
        setPaper(result.paper);
        setPaperMessage(result.message);
      } else if (action === "clear-marks") {
        const result = await clearPaperMarks();
        setPaper(result.paper);
        setPaperMessage(result.message);
      } else if (action === "refresh-selected-mark" && target) {
        const result = await refreshSelectedPaperMark(target.market_id, target.contract_id);
        setPaper(result.paper);
        setPaperMessage(result.message);
      } else if (action === "clear-selected-mark" && target) {
        const result = await clearSelectedPaperMark(target.market_id, target.contract_id);
        setPaper(result.paper);
        setPaperMessage(result.message);
      } else if (action === "use-position" && target) {
        const result = await usePaperPosition(target.market_id, target.contract_id);
        setPaperForm({
          market_id: result.market_id,
          contract_id: result.contract_id,
          side: result.side,
          size: String(result.size),
          limit_price: ""
        });
        setPaperMessage(result.message);
      } else if (action === "use-history" && target?.id) {
        const result = await usePaperHistory(target.id);
        setPaperForm({
          market_id: result.market_id,
          contract_id: result.contract_id,
          side: result.side,
          size: String(result.size),
          limit_price: result.limit_price === null ? "" : String(result.limit_price)
        });
        setPaperMessage(result.message);
      }
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }

  return (
    <main className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <img src="/marketsentinel.png" alt="MarketSentinel logo" />
          <div>
            <strong>MarketSentinel</strong>
            <span>Local command center</span>
          </div>
        </div>
        <nav className="nav-list" aria-label="Primary">
          <button className={tab === "overview" ? "active" : ""} onClick={() => setTab("overview")}>
            <BarChart3 size={18} /> Overview
          </button>
          <button className={tab === "markets" ? "active" : ""} onClick={() => setTab("markets")}>
            <SlidersHorizontal size={18} /> Markets
          </button>
          <button className={tab === "analytics" ? "active" : ""} onClick={() => setTab("analytics")}>
            <Trophy size={18} /> Analytics
          </button>
          <button className={tab === "live" ? "active" : ""} onClick={() => setTab("live")}>
            <ShieldCheck size={18} /> Live Safety
          </button>
          <button className={tab === "alerts" ? "active" : ""} onClick={() => setTab("alerts")}>
            <Bell size={18} /> Alerts
          </button>
          <button className={tab === "wallets" ? "active" : ""} onClick={() => setTab("wallets")}>
            <Wallet size={18} /> Wallets
          </button>
          <button className={tab === "paper" ? "active" : ""} onClick={() => setTab("paper")}>
            <Activity size={18} /> Paper
          </button>
          <button className={tab === "settings" ? "active" : ""} onClick={() => setTab("settings")}>
            <Settings size={18} /> Settings
          </button>
        </nav>
      </aside>

      <section className="workspace">
        <header className="topbar">
          <div>
            <h1>{tabLabel(tab)}</h1>
            <div className="status-row">
              <StatusPill tone={health?.status === "ok" ? "good" : "warn"}>
                API {health?.status ?? "loading"}
              </StatusPill>
              <StatusPill tone={health?.python_gui_available ? "good" : "neutral"}>
                Python GUI {health?.python_gui_available ? "available" : "unknown"}
              </StatusPill>
              {selectedMarket ? <StatusPill>{selectedMarket.display_name}</StatusPill> : null}
            </div>
          </div>
          <button className="icon-button" onClick={() => void loadAll()} disabled={loading} title="Refresh data">
            <RefreshCw size={18} /> Refresh
          </button>
        </header>

        {error ? <div className="error-banner">{error}</div> : null}

        {tab === "overview" ? (
          <OverviewView alerts={alerts} config={config} copy={copyState} health={health} markets={markets} paper={paper} wallets={wallets} loading={loading} />
        ) : null}
        {tab === "markets" ? (
          <MarketsView
            busyMarket={busyMarket}
            filteredMarkets={filteredMarkets}
            marketQuery={marketQuery}
            marketRead={marketRead}
            marketReadBusy={marketReadBusy}
            marketReadForm={marketReadForm}
            marketReadMessage={marketReadMessage}
            markets={markets}
            onMarketOrderManagement={() => void handleMarketOrderManagement()}
            onQueryChange={setMarketQuery}
            onMarketRead={(action) => void handleMarketRead(action)}
            onMarketReadFormChange={(patch) => setMarketReadForm((current) => ({ ...current, ...patch }))}
            onSelectedMarketChange={(marketId) => void handleSelectedMarketChange(marketId)}
            onSettingsSave={(event) => void handleMarketSettingsSave(event)}
            onToggle={(market) => void handleMarketToggle(market)}
            selectedMarket={selectedMarket}
            selectedMarketId={config?.selected_market_id ?? ""}
          />
        ) : null}
        {tab === "analytics" ? (
          <PolymarketAnalyticsView
            filters={leaderboardFilters}
            loading={analyticsLoading}
            message={analyticsMessage}
            mddAuditDetail={mddAuditDetail}
            mddCache={mddCache}
            mddCacheBusyKey={mddCacheBusyKey}
            mddForm={mddForm}
            mddPayload={walletMdd}
            onAuditDetailLoad={(cacheKey) => void handleAuditDetailLoad(cacheKey)}
            onMddCachePurge={(request) => void handleMddCachePurge(request)}
            onMddCacheRefresh={() => void handleMddCacheRefresh()}
            onFiltersChange={setLeaderboardFilters}
            onLeaderboardRefresh={(event) => void handleLeaderboardRefresh(event)}
            onMddFormChange={setMddForm}
            onMddLookup={(event) => void handleMddLookup(event)}
            onUserSearch={(event) => void handleUserSearch(event)}
            onUserSearchQueryChange={setUserSearchQuery}
            searchQuery={userSearchQuery}
            searchResults={userSearch}
            leaderboard={leaderboard}
          />
        ) : null}
        {tab === "live" ? (
          <LiveSafetyView
            busyMarket={busyMarket}
            form={paperForm}
            livePreflight={livePreflight}
            liveSafety={liveSafety}
            liveValidation={liveValidation}
            liveValidationAllowDuplicate={liveValidationAllowDuplicate}
            liveValidationDecisionForm={liveValidationDecisionForm}
            liveValidationDecisions={liveValidationDecisions}
            liveValidationImport={liveValidationImport}
            liveValidationPromotionProposal={liveValidationPromotionProposal}
            liveValidationPromotionProposalSnapshotDetail={liveValidationPromotionProposalSnapshotDetail}
            liveValidationPromotionProposalSnapshots={liveValidationPromotionProposalSnapshots}
            liveValidationPromotionProposalTargetTier={liveValidationPromotionProposalTargetTier}
            liveValidationReportDetail={liveValidationReportDetail}
            liveValidationReportBusyKey={liveValidationReportBusyKey}
            liveValidationReportMessage={liveValidationReportMessage}
            liveValidationReportSchemaValidation={liveValidationReportSchemaValidation}
            liveValidationReports={liveValidationReports}
            markets={markets}
            message={liveMessage}
            onFormChange={setPaperForm}
            onPreview={() => void handleLivePreflight()}
            onValidationImport={() => void handleLiveValidationReportImport()}
            onValidationAllowDuplicateChange={setLiveValidationAllowDuplicate}
            onValidationDecisionChange={setLiveValidationDecisionForm}
            onValidationDecisionSubmit={(event) => void handleLiveValidationDecisionSubmit(event)}
            onValidationImportChange={setLiveValidationImport}
            onValidationProposalRefresh={() => void handleLiveValidationPromotionProposalRefresh()}
            onValidationProposalSnapshotDelete={(key) => void handleLiveValidationPromotionProposalSnapshotDelete(key)}
            onValidationProposalSnapshotOpen={(key) => void handleLiveValidationPromotionProposalSnapshotOpen(key)}
            onValidationProposalSnapshotsRefresh={() => void handleLiveValidationPromotionProposalSnapshotsRefresh()}
            onValidationProposalSnapshotStore={() => void handleLiveValidationPromotionProposalSnapshotStore()}
            onValidationProposalTargetChange={(value) => void handleLiveValidationPromotionProposalTargetChange(value)}
            onValidationReportDelete={(key) => void handleLiveValidationReportDelete(key)}
            onValidationReportOpen={(key) => void handleLiveValidationReportOpen(key)}
            onValidationReportsRefresh={() => void handleLiveValidationReportsRefresh()}
            onValidationRefresh={() => void handleLiveValidationRefresh()}
            onValidationSnapshotStore={() => void handleLiveValidationSnapshotStore()}
            onSelectedMarketChange={(marketId) => void handleSelectedMarketChange(marketId)}
            onSettingsSave={(event) => void handleMarketSettingsSave(event)}
            selectedMarket={selectedMarket}
            selectedMarketId={config?.selected_market_id ?? ""}
          />
        ) : null}
        {tab === "paper" ? (
          <PaperView
            form={paperForm}
            markets={markets}
            message={paperMessage}
            onAction={(action, target) => void handlePaperAction(action, target)}
            onClearHistory={() => void handleClearHistory()}
            onFormChange={setPaperForm}
            paper={paper}
          />
        ) : null}
        {tab === "alerts" ? (
          <AlertsView
            alerts={alerts}
            editingAlertId={editingAlertId}
            form={alertForm}
            markets={markets}
            message={alertMessage}
            onAction={(action, alert) => void handleAlertAction(action, alert)}
            onFormChange={setAlertForm}
            onSubmit={(event) => void handleAlertSubmit(event)}
          />
        ) : null}
        {tab === "wallets" ? (
          <WalletsCopyView
            copy={copyState}
            copyForm={copyForm}
            copyPreview={copyPreview}
            copyPreviewForm={copyPreviewForm}
            editingWalletId={editingWalletId}
            form={walletForm}
            message={walletMessage}
            onCopyFormChange={setCopyForm}
            onCopyPreview={() => void handleCopyPreview()}
            onCopyPreviewFormChange={setCopyPreviewForm}
            onCopySubmit={(event) => void handleCopySubmit(event)}
            onPollingIntervalChange={(value) =>
              setWallets((current) =>
                current
                  ? { ...current, polling: { ...current.polling, poll_interval_seconds: value } }
                  : current
              )
            }
            onWalletAction={(action, wallet) => void handleWalletAction(action, wallet)}
            onWalletFormChange={setWalletForm}
            onWalletSubmit={(event) => void handleWalletSubmit(event)}
            wallets={wallets}
          />
        ) : null}
        {tab === "settings" ? (
          <SettingsView
            config={config}
            health={health}
            markets={markets}
            onSelectedMarketChange={(marketId) => void handleSelectedMarketChange(marketId)}
            onThemeChange={(theme) => void handleThemeChange(theme)}
            onUiDesignChange={(uiDesign) => void handleUiDesignChange(uiDesign)}
          />
        ) : null}
      </section>
    </main>
  );
}

function tabLabel(tab: Tab): string {
  if (tab === "markets") {
    return "Market Operations";
  }
  if (tab === "analytics") {
    return "Polymarket Analytics";
  }
  if (tab === "live") {
    return "Live Safety";
  }
  if (tab === "alerts") {
    return "Alerts";
  }
  if (tab === "wallets") {
    return "Wallets & Copy";
  }
  if (tab === "paper") {
    return "Paper Trading";
  }
  if (tab === "settings") {
    return "Settings";
  }
  return "Overview";
}

function OverviewView({
  alerts,
  config,
  copy,
  health,
  markets,
  paper,
  wallets,
  loading
}: {
  alerts: AlertsPayload | null;
  config: ConfigPayload | null;
  copy: CopyPayload | null;
  health: HealthPayload | null;
  markets: MarketsPayload | null;
  paper: PaperPayload | null;
  wallets: WalletsPayload | null;
  loading: boolean;
}) {
  return (
    <div className="content-grid">
      <section className="panel span-2">
        <div className="panel-title">
          <ShieldCheck size={18} />
          <h2>Runtime</h2>
        </div>
        <div className="metrics-grid">
          <Metric label="API" value={health?.status ?? (loading ? "loading" : "offline")} tone={health?.status === "ok" ? "good" : "warn"} />
          <Metric label="Selected" value={config?.selected_market_id ?? "-"} />
          <Metric label="Theme" value={config?.theme ?? "-"} />
          <Metric label="Design" value={config?.ui_design ?? "-"} />
          <Metric label="API version" value={health?.api_version ?? "-"} />
          <Metric label="Mode" value={health?.mode ?? "parallel"} />
        </div>
      </section>

      <section className="panel">
        <div className="panel-title">
          <SlidersHorizontal size={18} />
          <h2>Markets</h2>
        </div>
        <div className="metrics-grid two">
          <Metric label="Total" value={markets?.counts.total ?? 0} />
          <Metric label="Enabled" value={markets?.counts.enabled ?? 0} tone="good" />
          <Metric label="Implemented" value={markets?.counts.implemented ?? 0} />
        </div>
      </section>

      <section className="panel">
        <div className="panel-title">
          <Activity size={18} />
          <h2>Paper</h2>
        </div>
        <div className="metrics-grid two">
          <Metric label="Positions" value={paper?.summary.positions ?? 0} />
          <Metric label="History" value={paper?.counts.history ?? 0} />
          <Metric label="Accepted" value={paper?.counts.accepted ?? 0} tone="good" />
          <Metric label="Rejected" value={paper?.counts.rejected ?? 0} tone={paper?.counts.rejected ? "warn" : undefined} />
        </div>
      </section>

      <section className="panel">
        <div className="panel-title">
          <BellRing size={18} />
          <h2>Alerts</h2>
        </div>
        <div className="metrics-grid two">
          <Metric label="Total" value={alerts?.counts.total ?? 0} />
          <Metric label="Enabled" value={alerts?.counts.enabled ?? 0} tone="good" />
          <Metric label="Triggered" value={alerts?.counts.triggered ?? 0} tone={alerts?.counts.triggered ? "warn" : undefined} />
        </div>
      </section>

      <section className="panel">
        <div className="panel-title">
          <Wallet size={18} />
          <h2>Wallets</h2>
        </div>
        <div className="metrics-grid two">
          <Metric label="Tracked" value={wallets?.counts.total ?? 0} />
          <Metric label="Enabled" value={wallets?.counts.enabled ?? 0} tone="good" />
          <Metric label="Recent" value={wallets?.recent_activity.length ?? 0} />
          <Metric label="Copy" value={copy?.status ?? "disabled"} tone={copy?.settings.enabled ? "good" : undefined} />
        </div>
      </section>
    </div>
  );
}

function MarketsView({
  busyMarket,
  filteredMarkets,
  marketQuery,
  marketRead,
  marketReadBusy,
  marketReadForm,
  marketReadMessage,
  markets,
  onMarketOrderManagement,
  onMarketRead,
  onMarketReadFormChange,
  onQueryChange,
  onSelectedMarketChange,
  onSettingsSave,
  onToggle,
  selectedMarket,
  selectedMarketId
}: {
  busyMarket: string | null;
  filteredMarkets: Market[];
  marketQuery: string;
  marketRead: MarketReadState;
  marketReadBusy: string | null;
  marketReadForm: MarketReadForm;
  marketReadMessage: string;
  markets: MarketsPayload | null;
  onMarketOrderManagement: () => void;
  onMarketRead: (action: "events" | "contracts" | "price" | "orderbook" | "trades" | "candles" | "account") => void;
  onMarketReadFormChange: (patch: Partial<MarketReadForm>) => void;
  onQueryChange: (value: string) => void;
  onSelectedMarketChange: (marketId: string) => void;
  onSettingsSave: (event: FormEvent<HTMLFormElement>) => void;
  onToggle: (market: Market) => void;
  selectedMarket: Market | null;
  selectedMarketId: string;
}) {
  return (
    <section className="panel full">
      <div className="toolbar">
        <label className="search-box">
          <Search size={17} />
          <input
            value={marketQuery}
            onChange={(event) => onQueryChange(event.target.value)}
            placeholder="Search markets, ids, capabilities"
          />
        </label>
        <select value={selectedMarketId} onChange={(event) => onSelectedMarketChange(event.target.value)}>
          {(markets?.markets ?? []).map((market) => (
            <option key={market.market_id} value={market.market_id}>
              {market.display_name}
            </option>
          ))}
        </select>
      </div>
      {selectedMarket ? (
        <form className="market-detail" key={selectedMarket.market_id} onSubmit={onSettingsSave}>
          <div className="market-detail-main">
            <div>
              <h2>{selectedMarket.display_name}</h2>
              <p>{selectedMarket.status_text}</p>
            </div>
            <StatusPill tone={selectedMarket.health.ok ? "good" : "warn"}>
              {selectedMarket.health.ok ? "health ok" : "health issue"}
            </StatusPill>
          </div>
          <div className="diagnostic-grid">
            <div>
              <span>Adapter</span>
              <strong>{selectedMarket.health.adapter || "-"}</strong>
            </div>
            <div>
              <span>Message</span>
              <strong>{selectedMarket.health.message || "-"}</strong>
            </div>
            <div>
              <span>Credential sources</span>
              <strong>{selectedMarket.credential_summary}</strong>
            </div>
            <div>
              <span>Credential env vars</span>
              <strong>{selectedMarket.credential_env_vars.length ? selectedMarket.credential_env_vars.join(", ") : "none listed"}</strong>
            </div>
          </div>
          <div className="safety-grid">
            <label className="check-row">
              <input name="enabled" type="checkbox" defaultChecked={selectedMarket.enabled} />
              <span>Market enabled</span>
            </label>
            <label className="check-row">
              <input name="live_trading_enabled" type="checkbox" defaultChecked={selectedMarket.safety.live_trading_enabled} />
              <span>Live enabled</span>
            </label>
            <label className="check-row">
              <input name="live_trading_confirmed" type="checkbox" defaultChecked={selectedMarket.safety.live_trading_confirmed} />
              <span>Live acknowledged</span>
            </label>
            <label className="check-row">
              <input name="live_trading_kill_switch" type="checkbox" defaultChecked={selectedMarket.safety.live_trading_kill_switch} />
              <span>Kill switch</span>
            </label>
            {selectedMarket.market_id === "betfair_exchange" ? (
              <label className="check-row">
                <input
                  name="betfair_order_management_enabled"
                  type="checkbox"
                  defaultChecked={selectedMarket.health.order_management_enabled === true}
                />
                <span>Enable Betfair order management</span>
              </label>
            ) : selectedMarket.market_id === "kalshi" ? (
              <label className="check-row">
                <input
                  name="kalshi_order_management_enabled"
                  type="checkbox"
                  defaultChecked={selectedMarket.health.order_management_enabled === true}
                />
                <span>Enable Kalshi order management</span>
              </label>
            ) : selectedMarket.market_id === "polymarket" ? (
              <label className="check-row">
                <input
                  name="polymarket_order_management_enabled"
                  type="checkbox"
                  defaultChecked={selectedMarket.health.order_management_enabled === true}
                />
                <span>Enable Polymarket order management</span>
              </label>
            ) : selectedMarket.market_id === "gemini_titan" ? (
              <label className="check-row">
                <input
                  name="gemini_order_management_enabled"
                  type="checkbox"
                  defaultChecked={selectedMarket.health.order_management_enabled === true}
                />
                <span>Enable Gemini order management</span>
              </label>
            ) : selectedMarket.market_id === "matchbook" ? (
              <label className="check-row">
                <input
                  name="matchbook_order_management_enabled"
                  type="checkbox"
                  defaultChecked={selectedMarket.health.order_management_enabled === true}
                />
                <span>Enable Matchbook order management</span>
              </label>
            ) : selectedMarket.market_id === "myriad_markets" ? (
              <label className="check-row">
                <input
                  name="myriad_order_management_enabled"
                  type="checkbox"
                  defaultChecked={selectedMarket.health.order_management_enabled === true}
                />
                <span>Enable Myriad order management</span>
              </label>
            ) : selectedMarket.market_id === "opinion_labs" ? (
              <label className="check-row">
                <input
                  name="opinion_order_management_enabled"
                  type="checkbox"
                  defaultChecked={selectedMarket.health.order_management_enabled === true}
                />
                <span>Enable Opinion order management</span>
              </label>
            ) : selectedMarket.market_id === "limitless_exchange" ? (
              <label className="check-row">
                <input
                  name="limitless_order_management_enabled"
                  type="checkbox"
                  defaultChecked={selectedMarket.health.order_management_enabled === true}
                />
                <span>Enable Limitless order management</span>
              </label>
            ) : selectedMarket.market_id === "smarkets" ? (
              <label className="check-row">
                <input
                  name="smarkets_order_management_enabled"
                  type="checkbox"
                  defaultChecked={selectedMarket.health.order_management_enabled === true}
                />
                <span>Enable Smarkets order management</span>
              </label>
            ) : selectedMarket.market_id === "probable" ? (
              <label className="check-row">
                <input
                  name="probable_order_management_enabled"
                  type="checkbox"
                  defaultChecked={selectedMarket.health.order_management_enabled === true}
                />
                <span>Enable Probable order management</span>
              </label>
            ) : selectedMarket.market_id === "hyperliquid" ? (
              <label className="check-row">
                <input
                  name="hyperliquid_order_management_enabled"
                  type="checkbox"
                  defaultChecked={selectedMarket.health.order_management_enabled === true}
                />
                <span>Enable Hyperliquid order management</span>
              </label>
            ) : selectedMarket.market_id === "predict_fun" ? (
              <label className="check-row">
                <input
                  name="predict_fun_order_management_enabled"
                  type="checkbox"
                  defaultChecked={selectedMarket.health.order_management_enabled === true}
                />
                <span>Enable Predict.fun relay removal</span>
              </label>
            ) : selectedMarket.market_id === "xmarket" ? (
              <label className="check-row">
                <input
                  name="xmarket_order_management_enabled"
                  type="checkbox"
                  defaultChecked={selectedMarket.health.order_management_enabled === true}
                />
                <span>Enable Xmarket batch order management</span>
              </label>
            ) : ["ibkr_forecasttrader", "forecastex", "cme_prediction_markets"].includes(selectedMarket.market_id) ? (
              <label className="check-row">
                <input
                  name="ibkr_order_management_enabled"
                  type="checkbox"
                  defaultChecked={selectedMarket.health.order_management_enabled === true}
                />
                <span>Enable IBKR order management</span>
              </label>
            ) : selectedMarket.market_id === "manifold" ? (
              <label className="check-row">
                <input
                  name="manifold_order_management_enabled"
                  type="checkbox"
                  defaultChecked={selectedMarket.health.order_management_enabled === true}
                />
                <span>Enable Manifold order management</span>
              </label>
            ) : selectedMarket.market_id === "prophet_exchange" ? (
              <label className="check-row">
                <input
                  name="prophet_exchange_order_management_enabled"
                  type="checkbox"
                  defaultChecked={selectedMarket.health.order_management_enabled === true}
                />
                <span>Enable Prophet Exchange order management</span>
              </label>
            ) : null}
            <label>
              <span>Max size</span>
              <input name="live_trading_max_size" defaultValue={selectedMarket.safety.live_trading_max_size ?? ""} />
            </label>
            <label>
              <span>Max notional</span>
              <input name="live_trading_max_notional" defaultValue={selectedMarket.safety.live_trading_max_notional ?? ""} />
            </label>
            <button className="icon-button" type="submit" disabled={busyMarket === selectedMarket.market_id}>
              <Settings size={17} /> Save Market Settings
            </button>
          </div>
        </form>
      ) : null}
      {selectedMarket ? (
        <div className="market-detail">
          <div className="market-detail-main">
            <div>
              <h3>Market data</h3>
              <p>Use the enabled adapter's official discovery, quote, orderbook, trade, candle, and credentialed account feeds.</p>
            </div>
            <StatusPill tone={marketReadBusy ? "neutral" : marketReadMessage ? "good" : "neutral"}>
              {marketReadBusy ? `loading ${marketReadBusy}` : marketReadMessage || "ready"}
            </StatusPill>
          </div>
          <div className="safety-grid">
            <label>
              <span>Event search</span>
              <input
                value={marketReadForm.query}
                onChange={(event) => onMarketReadFormChange({ query: event.target.value })}
                placeholder="Optional search"
              />
            </label>
            <label>
              <span>Event id</span>
              <input
                value={marketReadForm.event_id}
                onChange={(event) => onMarketReadFormChange({ event_id: event.target.value })}
                placeholder="For contracts"
              />
            </label>
            <label>
              <span>Contract id</span>
              <input
                value={marketReadForm.contract_id}
                onChange={(event) => onMarketReadFormChange({ contract_id: event.target.value })}
                placeholder="For quotes/history"
              />
            </label>
            <label>
              <span>Account ticker</span>
              <input
                value={marketReadForm.account_ticker}
                onChange={(event) => onMarketReadFormChange({ account_ticker: event.target.value })}
                placeholder="Kalshi ticker"
              />
            </label>
            <label>
              <span>Account order id</span>
              <input
                value={marketReadForm.account_order_id}
                onChange={(event) => onMarketReadFormChange({ account_order_id: event.target.value })}
                placeholder="Optional"
              />
            </label>
            <label>
              <span>Polymarket trade id</span>
              <input
                value={marketReadForm.account_trade_id}
                onChange={(event) => onMarketReadFormChange({ account_trade_id: event.target.value })}
                placeholder="Optional fill id"
              />
            </label>
            <label>
              <span>Account cursor</span>
              <input
                value={marketReadForm.account_cursor}
                onChange={(event) => onMarketReadFormChange({ account_cursor: event.target.value })}
                placeholder="Next page cursor"
              />
            </label>
            <label>
              <span>Account offset</span>
              <input
                value={marketReadForm.account_offset}
                onChange={(event) => onMarketReadFormChange({ account_offset: event.target.value })}
                placeholder="Optional numeric offset"
              />
            </label>
            <label>
              <span>Account wallet / address</span>
              <input
                value={marketReadForm.account_wallet}
                onChange={(event) => onMarketReadFormChange({ account_wallet: event.target.value })}
                placeholder="Predict.fun 0x address"
              />
            </label>
            <label>
              <span>Perp DEX</span>
              <input
                value={marketReadForm.account_dex}
                onChange={(event) => onMarketReadFormChange({ account_dex: event.target.value })}
                placeholder="Optional Hyperliquid DEX"
              />
            </label>
            <label>
              <span>Account market id</span>
              <input
                value={marketReadForm.account_market_id}
                onChange={(event) => onMarketReadFormChange({ account_market_id: event.target.value })}
                placeholder="Opinion / Betfair / Xmarket market id"
              />
            </label>
            <label>
              <span>Opinion chain id</span>
              <input
                value={marketReadForm.account_chain_id}
                onChange={(event) => onMarketReadFormChange({ account_chain_id: event.target.value })}
                placeholder="Optional numeric id"
              />
            </label>
            <label>
              <span>Account status</span>
              <input
                value={marketReadForm.account_status}
                onChange={(event) => onMarketReadFormChange({ account_status: event.target.value })}
                placeholder="Opinion 1,2,3,4,5 / Betfair SETTLED"
              />
            </label>
            <label>
              <span>Account activity event types</span>
              <input
                value={marketReadForm.account_event_types}
                onChange={(event) => onMarketReadFormChange({ account_event_types: event.target.value })}
                placeholder="ORDER_MATCHED,ORDER_CREATED"
              />
            </label>
            <label>
              <span>Predict.fun resolved filter</span>
              <input
                value={marketReadForm.account_is_resolved}
                onChange={(event) => onMarketReadFormChange({ account_is_resolved: event.target.value })}
                placeholder="true or false"
              />
            </label>
            <label>
              <span>Betfair event type id</span>
              <input
                value={marketReadForm.account_event_type_id}
                onChange={(event) => onMarketReadFormChange({ account_event_type_id: event.target.value })}
                placeholder="Optional"
              />
            </label>
            <label>
              <span>Betfair event id</span>
              <input
                value={marketReadForm.account_event_id}
                onChange={(event) => onMarketReadFormChange({ account_event_id: event.target.value })}
                placeholder="Optional"
              />
            </label>
            <label>
              <span>Betfair runner id</span>
              <input
                value={marketReadForm.account_runner_id}
                onChange={(event) => onMarketReadFormChange({ account_runner_id: event.target.value })}
                placeholder="Optional"
              />
            </label>
            <label>
              <span>Betfair bet id</span>
              <input
                value={marketReadForm.account_bet_id}
                onChange={(event) => onMarketReadFormChange({ account_bet_id: event.target.value })}
                placeholder="Optional"
              />
            </label>
            <label>
              <span>Betfair group by</span>
              <input
                value={marketReadForm.account_group_by}
                onChange={(event) => onMarketReadFormChange({ account_group_by: event.target.value })}
                placeholder="BET, MARKET, RUNNER"
              />
            </label>
            <label className="check-row">
              <input
                type="checkbox"
                checked={marketReadForm.account_include_item_description}
                onChange={(event) => onMarketReadFormChange({ account_include_item_description: event.target.checked })}
              />
              <span>Include Betfair item descriptions</span>
            </label>
            <label>
              <span>Matchbook sport id</span>
              <input
                value={marketReadForm.account_sport_id}
                onChange={(event) => onMarketReadFormChange({ account_sport_id: event.target.value })}
                placeholder="Optional numeric id(s)"
              />
            </label>
            <label>
              <span>Matchbook offer side</span>
              <input
                value={marketReadForm.account_side}
                onChange={(event) => onMarketReadFormChange({ account_side: event.target.value })}
                placeholder="back, lay, win, lose"
              />
            </label>
            <label>
              <span>Matchbook offer status</span>
              <input
                value={marketReadForm.account_offer_status}
                onChange={(event) => onMarketReadFormChange({ account_offer_status: event.target.value })}
                placeholder="open, matched, unmatched"
              />
            </label>
            <label>
              <span>Matchbook aggregation</span>
              <input
                value={marketReadForm.account_aggregation_type}
                onChange={(event) => onMarketReadFormChange({ account_aggregation_type: event.target.value })}
                placeholder="none, summary, average"
              />
            </label>
            <label>
              <span>Matchbook odds type</span>
              <input
                value={marketReadForm.account_odds_type}
                onChange={(event) => onMarketReadFormChange({ account_odds_type: event.target.value })}
                placeholder="DECIMAL, US, HK, MALAY, INDO, %"
              />
            </label>
            <label>
              <span>Matchbook interval</span>
              <input
                value={marketReadForm.account_interval}
                onChange={(event) => onMarketReadFormChange({ account_interval: event.target.value })}
                placeholder="Seconds (optional)"
              />
            </label>
            <label>
              <span>Cancellation reason</span>
              <input
                value={marketReadForm.account_cancellation_reason}
                onChange={(event) => onMarketReadFormChange({ account_cancellation_reason: event.target.value })}
                placeholder="user_request or heartbeat_expiry"
              />
            </label>
            <label className="check-row">
              <input
                type="checkbox"
                checked={marketReadForm.account_include_edits}
                onChange={(event) => onMarketReadFormChange({ account_include_edits: event.target.checked })}
              />
              <span>Include Matchbook offer edits</span>
            </label>
            <label>
              <span>Subaccount</span>
              <input
                value={marketReadForm.account_subaccount}
                onChange={(event) => onMarketReadFormChange({ account_subaccount: event.target.value })}
                placeholder="Kalshi 0–63"
              />
            </label>
            <label>
              <span>Position count filter</span>
              <input
                value={marketReadForm.account_count_filter}
                onChange={(event) => onMarketReadFormChange({ account_count_filter: event.target.value })}
                placeholder="position,total_traded"
              />
            </label>
            <label>
              <span>Limitless market slug</span>
              <input
                value={marketReadForm.account_market_slug}
                onChange={(event) => onMarketReadFormChange({ account_market_slug: event.target.value })}
                placeholder="For Limitless user orders"
              />
            </label>
            {selectedMarket.market_id === "myriad_markets" ? (
              <>
                <label>
                  <span>Myriad account page</span>
                  <input
                    value={marketReadForm.account_page}
                    onChange={(event) => onMarketReadFormChange({ account_page: event.target.value })}
                    placeholder="1–10000"
                  />
                </label>
                <label>
                  <span>Myriad trading model</span>
                  <input
                    value={marketReadForm.account_trading_model}
                    onChange={(event) => onMarketReadFormChange({ account_trading_model: event.target.value })}
                    placeholder="amm, ob, all"
                  />
                </label>
                <label>
                  <span>Myriad minimum shares</span>
                  <input
                    value={marketReadForm.account_min_shares}
                    onChange={(event) => onMarketReadFormChange({ account_min_shares: event.target.value })}
                    placeholder="Optional non-negative number"
                  />
                </label>
                <label>
                  <span>Myriad network id</span>
                  <input
                    value={marketReadForm.account_network_id}
                    onChange={(event) => onMarketReadFormChange({ account_network_id: event.target.value })}
                    placeholder="Optional chain id"
                  />
                </label>
                <label>
                  <span>Myriad token address</span>
                  <input
                    value={marketReadForm.account_token_address}
                    onChange={(event) => onMarketReadFormChange({ account_token_address: event.target.value })}
                    placeholder="0x…"
                  />
                </label>
                <label>
                  <span>Myriad keyword</span>
                  <input
                    value={marketReadForm.account_keyword}
                    onChange={(event) => onMarketReadFormChange({ account_keyword: event.target.value })}
                    placeholder="Optional search"
                  />
                </label>
                <label>
                  <span>Myriad sort</span>
                  <input
                    value={marketReadForm.account_sort}
                    onChange={(event) => onMarketReadFormChange({ account_sort: event.target.value })}
                    placeholder="asc or desc"
                  />
                </label>
                <label>
                  <span>Myriad sort by</span>
                  <input
                    value={marketReadForm.account_sort_by}
                    onChange={(event) => onMarketReadFormChange({ account_sort_by: event.target.value })}
                    placeholder="Documented portfolio field"
                  />
                </label>
                <label>
                  <span>Myriad position state</span>
                  <input
                    value={marketReadForm.account_state}
                    onChange={(event) => onMarketReadFormChange({ account_state: event.target.value })}
                    placeholder="For market_positions"
                  />
                </label>
                <label>
                  <span>Myriad topics</span>
                  <input
                    value={marketReadForm.account_topics}
                    onChange={(event) => onMarketReadFormChange({ account_topics: event.target.value })}
                    placeholder="Comma-separated"
                  />
                </label>
                <label>
                  <span>Myriad market ids</span>
                  <input
                    value={marketReadForm.account_market_ids}
                    onChange={(event) => onMarketReadFormChange({ account_market_ids: event.target.value })}
                    placeholder="Comma-separated"
                  />
                </label>
                <label className="check-row">
                  <input
                    type="checkbox"
                    checked={marketReadForm.account_exclude_history}
                    onChange={(event) => onMarketReadFormChange({ account_exclude_history: event.target.checked })}
                  />
                  <span>Exclude Myriad portfolio history</span>
                </label>
                <label className="check-row">
                  <input
                    type="checkbox"
                    checked={marketReadForm.account_group_by_event}
                    onChange={(event) => onMarketReadFormChange({ account_group_by_event: event.target.checked })}
                  />
                  <span>Group Myriad portfolio by event</span>
                </label>
              </>
            ) : null}
            <label>
              <span>Delegated profile</span>
              <input
                value={marketReadForm.account_on_behalf_of}
                onChange={(event) => onMarketReadFormChange({ account_on_behalf_of: event.target.value })}
                placeholder="Optional x-on-behalf-of"
              />
            </label>
            <label>
              <span>Resolution</span>
              <input
                value={marketReadForm.resolution}
                onChange={(event) => onMarketReadFormChange({ resolution: event.target.value })}
                placeholder="1h"
              />
            </label>
            <label>
              <span>From (Unix)</span>
              <input
                value={marketReadForm.from}
                onChange={(event) => onMarketReadFormChange({ from: event.target.value })}
                placeholder="Optional"
              />
            </label>
            <label>
              <span>To (Unix)</span>
              <input
                value={marketReadForm.to}
                onChange={(event) => onMarketReadFormChange({ to: event.target.value })}
                placeholder="Optional"
              />
            </label>
            {selectedMarket.health.account_recovery_operations?.length ? (
              <label>
                <span>Account read</span>
                <select
                  value={marketReadForm.account_operation}
                  onChange={(event) => onMarketReadFormChange({ account_operation: event.target.value as MarketAccountOperation })}
                >
                  {selectedMarket.health.account_recovery_operations.map((operation) => (
                    <option key={operation} value={operation}>{operation.replaceAll("_", " ")}</option>
                  ))}
                </select>
              </label>
            ) : null}
            {selectedMarket.health.account_recovery_operations?.length ? (
              <label className="check-row">
                <input
                  type="checkbox"
                  checked={marketReadForm.account_historical}
                  onChange={(event) => onMarketReadFormChange({ account_historical: event.target.checked })}
                />
                <span>Use historical account endpoint</span>
              </label>
            ) : null}
          </div>
          <div className="button-row compact">
            {(["events", "contracts", "price", "orderbook", "trades", "candles"] as const).map((action) => (
              <button
                className="secondary-button"
                key={action}
                type="button"
                disabled={marketReadBusy !== null}
                onClick={() => onMarketRead(action)}
              >
                {action === "events" ? "Discover events" : action === "contracts" ? "Load contracts" : `Read ${action}`}
              </button>
            ))}
            {selectedMarket.health.account_recovery_operations?.length ? (
              <button
                className="secondary-button"
                type="button"
                disabled={marketReadBusy !== null}
                onClick={() => onMarketRead("account")}
              >
                Read account
              </button>
            ) : null}
          </div>
          {selectedMarket.health.order_management_operations?.length ? (
            <div className="order-management-panel">
              <div className="market-detail-main">
                <div>
                  <h4>Guarded live order management</h4>
                  <p>
                    {selectedMarket.market_id === "kalshi"
                      ? "Kalshi mutations are disabled by default and require the shared live-safety gates, Kalshi-specific opt-in, and exact operator confirmation."
                      : selectedMarket.market_id === "polymarket"
                        ? "Polymarket CLOB cancellations are disabled by default and require the shared live-safety gates, explicit L2 headers, a separate opt-in, and exact operator confirmation."
                          : selectedMarket.market_id === "gemini_titan"
                            ? "Gemini cancellations are disabled by default and require the shared live-safety gates, Gemini-specific opt-in, authenticated Trader credentials, and exact operator confirmation."
                            : selectedMarket.market_id === "matchbook"
                              ? "Matchbook offer cancellation and edits are disabled by default and require the shared live-safety gates, Matchbook-specific opt-in, a session, and exact operator confirmation."
                              : selectedMarket.market_id === "myriad_markets"
                                ? "Myriad signed-order cancellation and replacement are disabled by default and require HMAC/JWT account authentication, the shared live-safety gates, a separate opt-in, and exact operator confirmation."
                              : selectedMarket.market_id === "opinion_labs"
                                ? "Opinion CLOB cancellations are disabled by default and require the official SDK, wallet credentials, shared live-safety gates, a separate opt-in, and exact operator confirmation."
                              : selectedMarket.market_id === "limitless_exchange"
                                ? "Limitless cancellations are disabled by default and require HMAC token credentials, shared live-safety gates, a separate opt-in, and exact operator/global confirmation."
                              : selectedMarket.market_id === "smarkets"
                                ? "Smarkets cancellations are disabled by default and require an approved session token, shared live-safety gates, a separate opt-in, and exact operator confirmation."
                              : selectedMarket.market_id === "probable"
                                ? "Probable cancellations are disabled by default and require HMAC L2 credentials, shared live-safety gates, a separate opt-in, and exact operator/global confirmation."
                              : selectedMarket.market_id === "predict_fun"
                                ? "Predict.fun relay removal is disabled by default and requires the shared live-safety gates, a separate opt-in, JWT credentials, and exact operator confirmation; it does not invalidate orders on-chain."
                              : selectedMarket.market_id === "hyperliquid"
                                ? "Hyperliquid signed cancellation, modification, and scheduled-cancel actions are disabled by default and require an externally signed exchange envelope, shared live-safety gates, a separate opt-in, and exact operator confirmation."
                              : selectedMarket.market_id === "xmarket"
                                ? "Xmarket batch creation and cancellation are disabled by default and require the API key, shared live-safety gates, a separate opt-in, and exact operator confirmation."
                              : ["ibkr_forecasttrader", "forecastex", "cme_prediction_markets"].includes(selectedMarket.market_id)
                                ? "IBKR event-contract cancellation and modification are disabled by default and require an authorized session, account entitlements, the shared live-safety gates, a separate opt-in, and exact operator confirmation."
                              : selectedMarket.market_id === "manifold"
                                ? "Manifold limit-bet cancellation is disabled by default and requires MANIFOLD_API_KEY, the shared live-safety gates, a separate opt-in, and exact operator confirmation. Only an unfilled limit bet can be cancelled."
                              : selectedMarket.market_id === "prophet_exchange"
                                ? "ProphetX cancellation is disabled by default and requires approved Trading API credentials, the shared live-safety gates, a separate opt-in, and exact operator confirmation."
                              : "Betfair mutations are disabled by default and require the shared live-safety gates plus the Betfair-specific opt-in. The UI never sends a request without explicit confirmation."}
                  </p>
                </div>
                <StatusPill tone={selectedMarket.health.order_management_enabled ? "warn" : "neutral"}>
                  {selectedMarket.health.order_management_enabled ? "opt-in enabled" : "opt-in disabled"}
                </StatusPill>
              </div>
              <div className="safety-grid">
                <label>
                  <span>Operation</span>
                  <select
                    value={marketReadForm.order_management_operation}
                    onChange={(event) => onMarketReadFormChange({ order_management_operation: event.target.value as MarketOrderManagementOperation })}
                  >
                    {selectedMarket.health.order_management_operations.map((operation) => (
                      <option key={operation} value={operation}>{operation.replaceAll("_", " ")}</option>
                    ))}
                  </select>
                </label>
                {selectedMarket.market_id === "kalshi" ? (
                  <>
                    <label>
                      <span>Order id</span>
                      <input
                        value={marketReadForm.order_management_order_id}
                        onChange={(event) => onMarketReadFormChange({ order_management_order_id: event.target.value })}
                        placeholder="Required except batch cancel"
                      />
                    </label>
                    <label>
                      <span>Kalshi ticker</span>
                      <input
                        value={marketReadForm.order_management_ticker}
                        onChange={(event) => onMarketReadFormChange({ order_management_ticker: event.target.value })}
                        placeholder="For amend_order"
                      />
                    </label>
                    <label>
                      <span>Side</span>
                      <select
                        value={marketReadForm.order_management_side}
                        onChange={(event) => onMarketReadFormChange({ order_management_side: event.target.value })}
                      >
                        <option value="bid">bid</option>
                        <option value="ask">ask</option>
                      </select>
                    </label>
                    <label>
                      <span>Price</span>
                      <input
                        value={marketReadForm.order_management_price}
                        onChange={(event) => onMarketReadFormChange({ order_management_price: event.target.value })}
                        placeholder="0.01–0.99"
                      />
                    </label>
                    <label>
                      <span>Count</span>
                      <input
                        value={marketReadForm.order_management_count}
                        onChange={(event) => onMarketReadFormChange({ order_management_count: event.target.value })}
                        placeholder="Amend total count"
                      />
                    </label>
                    <label>
                      <span>Subaccount</span>
                      <input
                        value={marketReadForm.order_management_subaccount}
                        onChange={(event) => onMarketReadFormChange({ order_management_subaccount: event.target.value })}
                        placeholder="0–63"
                      />
                    </label>
                    <label>
                      <span>Exchange index</span>
                      <input
                        value={marketReadForm.order_management_exchange_index}
                        onChange={(event) => onMarketReadFormChange({ order_management_exchange_index: event.target.value })}
                        placeholder="0 only"
                      />
                    </label>
                    <label>
                      <span>Reduce by</span>
                      <input
                        value={marketReadForm.order_management_reduce_by}
                        onChange={(event) => onMarketReadFormChange({ order_management_reduce_by: event.target.value })}
                        placeholder="Decrease only"
                      />
                    </label>
                    <label>
                      <span>Reduce to</span>
                      <input
                        value={marketReadForm.order_management_reduce_to}
                        onChange={(event) => onMarketReadFormChange({ order_management_reduce_to: event.target.value })}
                        placeholder="Decrease only"
                      />
                    </label>
                    <label>
                      <span>Original client id</span>
                      <input
                        value={marketReadForm.order_management_client_order_id}
                        onChange={(event) => onMarketReadFormChange({ order_management_client_order_id: event.target.value })}
                        placeholder="Optional"
                      />
                    </label>
                    <label>
                      <span>Updated client id</span>
                      <input
                        value={marketReadForm.order_management_updated_client_order_id}
                        onChange={(event) => onMarketReadFormChange({ order_management_updated_client_order_id: event.target.value })}
                        placeholder="Optional amend id"
                      />
                    </label>
                    <label className="wide-field">
                      <span>Batch orders JSON</span>
                      <textarea
                        value={marketReadForm.order_management_instructions}
                        onChange={(event) => onMarketReadFormChange({ order_management_instructions: event.target.value })}
                        rows={3}
                        spellCheck={false}
                        placeholder='[{"order_id":"order-id","subaccount":0}]'
                      />
                    </label>
                    <label className="wide-field">
                      <span>Operator confirmation</span>
                      <input
                        value={marketReadForm.order_management_operator_confirmation}
                        onChange={(event) => onMarketReadFormChange({ order_management_operator_confirmation: event.target.value })}
                        placeholder="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
                      />
                    </label>
                  </>
                ) : selectedMarket.market_id === "polymarket" ? (
                  <>
                    <label>
                      <span>Order id</span>
                      <input
                        value={marketReadForm.order_management_order_id}
                        onChange={(event) => onMarketReadFormChange({ order_management_order_id: event.target.value })}
                        placeholder="0x… 32-byte order hash"
                      />
                    </label>
                    <label>
                      <span>Condition id</span>
                      <input
                        value={marketReadForm.order_management_market_id}
                        onChange={(event) => onMarketReadFormChange({ order_management_market_id: event.target.value })}
                        placeholder="Required for market cancel"
                      />
                    </label>
                    <label>
                      <span>Token id</span>
                      <input
                        value={marketReadForm.contract_id}
                        onChange={(event) => onMarketReadFormChange({ contract_id: event.target.value })}
                        placeholder="Required for market cancel"
                      />
                    </label>
                    <label className="wide-field">
                      <span>Order hashes JSON (cancel_orders)</span>
                      <textarea
                        value={marketReadForm.order_management_instructions}
                        onChange={(event) => onMarketReadFormChange({ order_management_instructions: event.target.value })}
                        rows={3}
                        spellCheck={false}
                        placeholder='["0x…"]'
                      />
                    </label>
                    <label className="wide-field">
                      <span>Operator confirmation</span>
                      <input
                        value={marketReadForm.order_management_operator_confirmation}
                        onChange={(event) => onMarketReadFormChange({ order_management_operator_confirmation: event.target.value })}
                        placeholder="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
                      />
                    </label>
                  </>
                ) : selectedMarket.market_id === "gemini_titan" ? (
                  <>
                    <label>
                      <span>Order id</span>
                      <input
                        value={marketReadForm.order_management_order_id}
                        onChange={(event) => onMarketReadFormChange({ order_management_order_id: event.target.value })}
                        placeholder="Positive numeric Gemini order id"
                      />
                    </label>
                    <label className="wide-field">
                      <span>Order ids JSON (batch_cancel_orders)</span>
                      <textarea
                        value={marketReadForm.order_management_instructions}
                        onChange={(event) => onMarketReadFormChange({ order_management_instructions: event.target.value })}
                        rows={3}
                        spellCheck={false}
                        placeholder='[106817811, 106817812]'
                      />
                    </label>
                    <label className="wide-field">
                      <span>Operator confirmation</span>
                      <input
                        value={marketReadForm.order_management_operator_confirmation}
                        onChange={(event) => onMarketReadFormChange({ order_management_operator_confirmation: event.target.value })}
                        placeholder="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
                      />
                    </label>
                  </>
                ) : selectedMarket.market_id === "myriad_markets" ? (
                  <>
                    <label>
                      <span>Order hash / client id</span>
                      <input
                        value={marketReadForm.order_management_order_hash || marketReadForm.order_management_order_id}
                        onChange={(event) => onMarketReadFormChange({ order_management_order_hash: event.target.value, order_management_order_id: event.target.value })}
                        placeholder="0x + 64 hex chars or safe client id"
                      />
                    </label>
                    <label>
                      <span>Network id</span>
                      <input
                        value={marketReadForm.order_management_network_id}
                        onChange={(event) => onMarketReadFormChange({ order_management_network_id: event.target.value })}
                        placeholder="56"
                      />
                    </label>
                    <label>
                      <span>Trader wallet (cancel-all)</span>
                      <input
                        value={marketReadForm.order_management_trader}
                        onChange={(event) => onMarketReadFormChange({ order_management_trader: event.target.value })}
                        placeholder="0x…"
                      />
                    </label>
                    <label>
                      <span>Cancel-all timestamp</span>
                      <input
                        value={marketReadForm.order_management_timestamp}
                        onChange={(event) => onMarketReadFormChange({ order_management_timestamp: event.target.value })}
                        placeholder="Unix seconds"
                      />
                    </label>
                    <label>
                      <span>Cancel-all market id</span>
                      <input
                        value={marketReadForm.order_management_market_id}
                        onChange={(event) => onMarketReadFormChange({ order_management_market_id: event.target.value })}
                        placeholder="Blank cancels all markets"
                      />
                    </label>
                    <label>
                      <span>Signature type</span>
                      <select
                        value={marketReadForm.order_management_signature_type}
                        onChange={(event) => onMarketReadFormChange({ order_management_signature_type: event.target.value })}
                      >
                        <option value="">Default (0 / EOA)</option>
                        <option value="0">0 (EOA)</option>
                        <option value="3">3 (SCW)</option>
                      </select>
                    </label>
                    <label className="wide-field">
                      <span>Signed order payload JSON</span>
                      <textarea
                        value={marketReadForm.order_management_instructions}
                        onChange={(event) => onMarketReadFormChange({ order_management_instructions: event.target.value })}
                        rows={5}
                        spellCheck={false}
                        placeholder='{"order":{"trader":"0x…","marketId":"42","outcomeId":0,"side":0,"amount":"1000000000000000000","price":"500000000000000000","minFillAmount":"0","nonce":"1","expiration":"0"},"signature":"0x…"}'
                      />
                    </label>
                    <label>
                      <span>Cancel-all signature</span>
                      <input
                        value={marketReadForm.order_management_signature}
                        onChange={(event) => onMarketReadFormChange({ order_management_signature: event.target.value })}
                        placeholder="0x…"
                      />
                    </label>
                    <label className="check-row">
                      <input
                        type="checkbox"
                        checked={marketReadForm.order_management_allow_partial}
                        onChange={(event) => onMarketReadFormChange({ order_management_allow_partial: event.target.checked })}
                      />
                      <span>Allow partial batch results</span>
                    </label>
                    <label>
                      <span>Global cancellation confirmation</span>
                      <input
                        value={marketReadForm.order_management_confirmation}
                        onChange={(event) => onMarketReadFormChange({ order_management_confirmation: event.target.value })}
                        placeholder="CANCEL ALL MYRIAD ORDERS"
                      />
                    </label>
                    <label className="wide-field">
                      <span>Operator confirmation</span>
                      <input
                        value={marketReadForm.order_management_operator_confirmation}
                        onChange={(event) => onMarketReadFormChange({ order_management_operator_confirmation: event.target.value })}
                        placeholder="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
                      />
                    </label>
                  </>
                ) : selectedMarket.market_id === "opinion_labs" ? (
                  <>
                    <label>
                      <span>Order id</span>
                      <input
                        value={marketReadForm.order_management_order_id}
                        onChange={(event) => onMarketReadFormChange({ order_management_order_id: event.target.value })}
                        placeholder="Safe Opinion order id"
                      />
                    </label>
                    <label>
                      <span>Market id (optional cancel-all filter)</span>
                      <input
                        value={marketReadForm.order_management_market_id}
                        onChange={(event) => onMarketReadFormChange({ order_management_market_id: event.target.value })}
                        placeholder="Positive numeric id"
                      />
                    </label>
                    <label>
                      <span>Side (optional cancel-all filter)</span>
                      <select
                        value={marketReadForm.order_management_side}
                        onChange={(event) => onMarketReadFormChange({ order_management_side: event.target.value })}
                      >
                        <option value="">All sides</option>
                        <option value="BUY">BUY</option>
                        <option value="SELL">SELL</option>
                      </select>
                    </label>
                    <label className="wide-field">
                      <span>Order ids JSON (batch_cancel_orders)</span>
                      <textarea
                        value={marketReadForm.order_management_instructions}
                        onChange={(event) => onMarketReadFormChange({ order_management_instructions: event.target.value })}
                        rows={3}
                        spellCheck={false}
                        placeholder='["order-1", "order-2"]'
                      />
                    </label>
                    <label>
                      <span>Global cancellation confirmation</span>
                      <input
                        value={marketReadForm.order_management_confirmation}
                        onChange={(event) => onMarketReadFormChange({ order_management_confirmation: event.target.value })}
                        placeholder="CANCEL ALL OPINION ORDERS"
                      />
                    </label>
                    <label className="wide-field">
                      <span>Operator confirmation</span>
                      <input
                        value={marketReadForm.order_management_operator_confirmation}
                        onChange={(event) => onMarketReadFormChange({ order_management_operator_confirmation: event.target.value })}
                        placeholder="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
                      />
                    </label>
                  </>
                ) : selectedMarket.market_id === "limitless_exchange" ? (
                  <>
                    <label>
                      <span>Order id</span>
                      <input
                        value={marketReadForm.order_management_order_id}
                        onChange={(event) => onMarketReadFormChange({ order_management_order_id: event.target.value })}
                        placeholder="Safe Limitless order id"
                      />
                    </label>
                    <label>
                      <span>Market slug (cancel all)</span>
                      <input
                        value={marketReadForm.order_management_market_slug}
                        onChange={(event) => onMarketReadFormChange({ order_management_market_slug: event.target.value })}
                        placeholder="market-slug"
                      />
                    </label>
                    <label className="wide-field">
                      <span>Order ids JSON (batch_cancel_orders)</span>
                      <textarea
                        value={marketReadForm.order_management_instructions}
                        onChange={(event) => onMarketReadFormChange({ order_management_instructions: event.target.value })}
                        rows={3}
                        spellCheck={false}
                        placeholder='["order-1", "order-2"]'
                      />
                    </label>
                    <label>
                      <span>Global cancellation confirmation</span>
                      <input
                        value={marketReadForm.order_management_confirmation}
                        onChange={(event) => onMarketReadFormChange({ order_management_confirmation: event.target.value })}
                        placeholder="CANCEL ALL LIMITLESS ORDERS"
                      />
                    </label>
                    <label className="wide-field">
                      <span>Operator confirmation</span>
                      <input
                        value={marketReadForm.order_management_operator_confirmation}
                        onChange={(event) => onMarketReadFormChange({ order_management_operator_confirmation: event.target.value })}
                        placeholder="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
                      />
                    </label>
                  </>
                ) : selectedMarket.market_id === "smarkets" ? (
                  <>
                    <label>
                      <span>Order id</span>
                      <input
                        value={marketReadForm.order_management_order_id}
                        onChange={(event) => onMarketReadFormChange({ order_management_order_id: event.target.value })}
                        placeholder="Required for cancel_order"
                      />
                    </label>
                    <label>
                      <span>Market id</span>
                      <input
                        value={marketReadForm.order_management_market_id}
                        onChange={(event) => onMarketReadFormChange({ order_management_market_id: event.target.value })}
                        placeholder="Required for cancel_orders"
                      />
                    </label>
                    <label className="wide-field">
                      <span>Operator confirmation</span>
                      <input
                        value={marketReadForm.order_management_operator_confirmation}
                        onChange={(event) => onMarketReadFormChange({ order_management_operator_confirmation: event.target.value })}
                        placeholder="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
                      />
                    </label>
                  </>
                ) : selectedMarket.market_id === "probable" ? (
                  <>
                    <label>
                      <span>Order id</span>
                      <input
                        value={marketReadForm.order_management_order_id}
                        onChange={(event) => onMarketReadFormChange({ order_management_order_id: event.target.value })}
                        placeholder="Required for cancel_order"
                      />
                    </label>
                    <label>
                      <span>Token id</span>
                      <input
                        value={marketReadForm.order_management_market_id}
                        onChange={(event) => onMarketReadFormChange({ order_management_market_id: event.target.value })}
                        placeholder="Required for cancellation"
                      />
                    </label>
                    <label className="wide-field">
                      <span>Order ids JSON (cancel_orders)</span>
                      <textarea
                        value={marketReadForm.order_management_instructions}
                        onChange={(event) => onMarketReadFormChange({ order_management_instructions: event.target.value })}
                        rows={3}
                        spellCheck={false}
                        placeholder='["123", "124"]'
                      />
                    </label>
                    <label>
                      <span>Global cancellation confirmation</span>
                      <input
                        value={marketReadForm.order_management_confirmation}
                        onChange={(event) => onMarketReadFormChange({ order_management_confirmation: event.target.value })}
                        placeholder="CANCEL ALL PROBABLE ORDERS"
                      />
                    </label>
                    <label className="wide-field">
                      <span>Operator confirmation</span>
                      <input
                        value={marketReadForm.order_management_operator_confirmation}
                        onChange={(event) => onMarketReadFormChange({ order_management_operator_confirmation: event.target.value })}
                        placeholder="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
                      />
                    </label>
                  </>
                ) : selectedMarket.market_id === "hyperliquid" ? (
                  <>
                    <label className="wide-field">
                      <span>Signed exchange action JSON</span>
                      <textarea
                        value={marketReadForm.order_management_instructions}
                        onChange={(event) => onMarketReadFormChange({ order_management_instructions: event.target.value })}
                        rows={8}
                        spellCheck={false}
                        placeholder='{"action":{"type":"cancel","cancels":[{"a":100000000,"o":123456789}]},"nonce":1700000000000,"signature":"0x…"}'
                      />
                    </label>
                    {marketReadForm.order_management_operation === "schedule_cancel" ? (
                      <label>
                        <span>Schedule-cancel confirmation</span>
                        <input
                          value={marketReadForm.order_management_confirmation}
                          onChange={(event) => onMarketReadFormChange({ order_management_confirmation: event.target.value })}
                          placeholder="SCHEDULE HYPERLIQUID CANCEL"
                        />
                      </label>
                    ) : null}
                    <label className="wide-field">
                      <span>Operator confirmation</span>
                      <input
                        value={marketReadForm.order_management_operator_confirmation}
                        onChange={(event) => onMarketReadFormChange({ order_management_operator_confirmation: event.target.value })}
                        placeholder="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
                      />
                    </label>
                  </>
                ) : selectedMarket.market_id === "predict_fun" ? (
                  <>
                    <label className="wide-field">
                      <span>{marketReadForm.order_management_operation === "remove_orders_by_hash" ? "Order hashes JSON" : "Order ids JSON"}</span>
                      <textarea
                        value={marketReadForm.order_management_instructions}
                        onChange={(event) => onMarketReadFormChange({ order_management_instructions: event.target.value })}
                        rows={3}
                        spellCheck={false}
                        placeholder={marketReadForm.order_management_operation === "remove_orders_by_hash" ? '["0x…"]' : '["order-id-1", "order-id-2"]'}
                      />
                    </label>
                    <p className="form-help">Predict.fun removal is relay-only; it does not invalidate orders on-chain.</p>
                    <label className="wide-field">
                      <span>Operator confirmation</span>
                      <input
                        value={marketReadForm.order_management_operator_confirmation}
                        onChange={(event) => onMarketReadFormChange({ order_management_operator_confirmation: event.target.value })}
                        placeholder="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
                      />
                    </label>
                  </>
                ) : selectedMarket.market_id === "xmarket" ? (
                  <>
                    <label className="wide-field">
                      <span>{marketReadForm.order_management_operation === "batch_create_orders" ? "Orders JSON (batch_create_orders)" : "Order ids JSON (batch_cancel_orders)"}</span>
                      <textarea
                        value={marketReadForm.order_management_instructions}
                        onChange={(event) => onMarketReadFormChange({ order_management_instructions: event.target.value })}
                        rows={5}
                        spellCheck={false}
                        placeholder={marketReadForm.order_management_operation === "batch_create_orders"
                          ? '[{"outcomeId":"outcome-uuid","side":"buy","type":"limit","price":0.65,"quantity":100}]'
                          : '["order-id-1", "order-id-2"]'}
                      />
                    </label>
                    <p className="form-help">Xmarket batch mutations use fixed authenticated API endpoints; live order management remains opt-in and synchronous.</p>
                    <label className="wide-field">
                      <span>Operator confirmation</span>
                      <input
                        value={marketReadForm.order_management_operator_confirmation}
                        onChange={(event) => onMarketReadFormChange({ order_management_operator_confirmation: event.target.value })}
                        placeholder="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
                      />
                    </label>
                  </>
                ) : ["ibkr_forecasttrader", "forecastex", "cme_prediction_markets"].includes(selectedMarket.market_id) ? (
                  <>
                    <label>
                      <span>IBKR order id</span>
                      <input
                        value={marketReadForm.order_management_order_id}
                        onChange={(event) => onMarketReadFormChange({ order_management_order_id: event.target.value })}
                        placeholder="Positive numeric order id"
                      />
                    </label>
                    <label className="wide-field">
                      <span>Modified order JSON (modify_order)</span>
                      <textarea
                        value={marketReadForm.order_management_instructions}
                        onChange={(event) => onMarketReadFormChange({ order_management_instructions: event.target.value })}
                        rows={6}
                        spellCheck={false}
                        placeholder='{"conid":721095497,"orderType":"LMT","side":"BUY","tif":"DAY","quantity":5,"price":0.51}'
                      />
                    </label>
                    <p className="form-help">CME mutations additionally require manualIndicator and extOperator in the JSON or configured IBKR settings. Global cancellation uses the documented -1 order id.</p>
                    <label>
                      <span>Global cancellation confirmation</span>
                      <input
                        value={marketReadForm.order_management_confirmation}
                        onChange={(event) => onMarketReadFormChange({ order_management_confirmation: event.target.value })}
                        placeholder="CANCEL ALL IBKR EVENT ORDERS"
                      />
                    </label>
                    <label className="wide-field">
                      <span>Operator confirmation</span>
                      <input
                        value={marketReadForm.order_management_operator_confirmation}
                        onChange={(event) => onMarketReadFormChange({ order_management_operator_confirmation: event.target.value })}
                        placeholder="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
                      />
                    </label>
                  </>
                ) : selectedMarket.market_id === "manifold" ? (
                  <>
                    <label>
                      <span>Manifold bet id</span>
                      <input
                        value={marketReadForm.order_management_order_id}
                        onChange={(event) => onMarketReadFormChange({ order_management_order_id: event.target.value })}
                        placeholder="Open limit bet id"
                      />
                    </label>
                    <p className="form-help">This calls the documented POST /v0/bet/{"{id}"}/cancel endpoint. It cannot cancel a filled or already-cancelled bet.</p>
                    <label className="wide-field">
                      <span>Operator confirmation</span>
                      <input
                        value={marketReadForm.order_management_operator_confirmation}
                        onChange={(event) => onMarketReadFormChange({ order_management_operator_confirmation: event.target.value })}
                        placeholder="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
                      />
                    </label>
                  </>
                ) : selectedMarket.market_id === "prophet_exchange" ? (
                  <>
                    <label>
                      <span>Returned order id</span>
                      <input
                        value={marketReadForm.order_management_order_id}
                        onChange={(event) => onMarketReadFormChange({ order_management_order_id: event.target.value })}
                        placeholder="order-1"
                      />
                    </label>
                    <label>
                      <span>Original external id</span>
                      <input
                        value={marketReadForm.order_management_external_id}
                        onChange={(event) => onMarketReadFormChange({ order_management_external_id: event.target.value })}
                        placeholder="external-1"
                      />
                    </label>
                    {marketReadForm.order_management_operation === "cancel_orders" ? (
                      <label className="wide-field">
                        <span>Cancel entries JSON</span>
                        <textarea
                          value={marketReadForm.order_management_instructions}
                          onChange={(event) => onMarketReadFormChange({ order_management_instructions: event.target.value })}
                          rows={5}
                          spellCheck={false}
                          placeholder='[{"order_id":"order-1","external_id":"external-1"}]'
                        />
                      </label>
                    ) : null}
                    <p className="form-help">ProphetX cancellation uses fixed POST /mm/cancel_order or /mm/cancel_multiple_orders paths. Each entry must include the exact external_id returned with the original submission.</p>
                    <label className="wide-field">
                      <span>Operator confirmation</span>
                      <input
                        value={marketReadForm.order_management_operator_confirmation}
                        onChange={(event) => onMarketReadFormChange({ order_management_operator_confirmation: event.target.value })}
                        placeholder="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
                      />
                    </label>
                  </>
                ) : selectedMarket.market_id === "matchbook" ? (
                  <>
                    <label>
                      <span>Offer id</span>
                      <input
                        value={marketReadForm.order_management_order_id}
                        onChange={(event) => onMarketReadFormChange({ order_management_order_id: event.target.value })}
                        placeholder="Positive numeric id"
                      />
                    </label>
                    <label>
                      <span>Offer ids (cancel_offers)</span>
                      <input
                        value={marketReadForm.order_management_offer_ids}
                        onChange={(event) => onMarketReadFormChange({ order_management_offer_ids: event.target.value })}
                        placeholder="404,405"
                      />
                    </label>
                    <label>
                      <span>Event ids filter</span>
                      <input
                        value={marketReadForm.order_management_event_ids}
                        onChange={(event) => onMarketReadFormChange({ order_management_event_ids: event.target.value })}
                        placeholder="101"
                      />
                    </label>
                    <label>
                      <span>Market ids filter</span>
                      <input
                        value={marketReadForm.order_management_market_ids}
                        onChange={(event) => onMarketReadFormChange({ order_management_market_ids: event.target.value })}
                        placeholder="202"
                      />
                    </label>
                    <label>
                      <span>Runner ids filter</span>
                      <input
                        value={marketReadForm.order_management_runner_ids}
                        onChange={(event) => onMarketReadFormChange({ order_management_runner_ids: event.target.value })}
                        placeholder="303"
                      />
                    </label>
                    <label>
                      <span>Current odds (edit)</span>
                      <input
                        value={marketReadForm.order_management_current_odds}
                        onChange={(event) => onMarketReadFormChange({ order_management_current_odds: event.target.value })}
                        placeholder="1.50"
                      />
                    </label>
                    <label>
                      <span>New odds (edit)</span>
                      <input
                        value={marketReadForm.order_management_new_odds}
                        onChange={(event) => onMarketReadFormChange({ order_management_new_odds: event.target.value })}
                        placeholder="2.00"
                      />
                    </label>
                    <label>
                      <span>Current stake (edit)</span>
                      <input
                        value={marketReadForm.order_management_current_stake}
                        onChange={(event) => onMarketReadFormChange({ order_management_current_stake: event.target.value })}
                        placeholder="5"
                      />
                    </label>
                    <label>
                      <span>New stake (edit)</span>
                      <input
                        value={marketReadForm.order_management_new_stake}
                        onChange={(event) => onMarketReadFormChange({ order_management_new_stake: event.target.value })}
                        placeholder="6"
                      />
                    </label>
                    <label className="wide-field">
                      <span>Offer edits JSON (edit_offers)</span>
                      <textarea
                        value={marketReadForm.order_management_instructions}
                        onChange={(event) => onMarketReadFormChange({ order_management_instructions: event.target.value })}
                        rows={3}
                        spellCheck={false}
                        placeholder='[{"id":404,"current-odds":1.5,"new-odds":2,"current-stake":5,"new-stake":6}]'
                      />
                    </label>
                    <label>
                      <span>Global cancellation confirmation</span>
                      <input
                        value={marketReadForm.order_management_confirmation}
                        onChange={(event) => onMarketReadFormChange({ order_management_confirmation: event.target.value })}
                        placeholder="CANCEL ALL MATCHBOOK OFFERS"
                      />
                    </label>
                    <label className="wide-field">
                      <span>Operator confirmation</span>
                      <input
                        value={marketReadForm.order_management_operator_confirmation}
                        onChange={(event) => onMarketReadFormChange({ order_management_operator_confirmation: event.target.value })}
                        placeholder="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
                      />
                    </label>
                  </>
                ) : (
                  <>
                    <label>
                      <span>Exchange market id</span>
                      <input
                        value={marketReadForm.order_management_market_id}
                        onChange={(event) => onMarketReadFormChange({ order_management_market_id: event.target.value })}
                        placeholder="1.234 (blank = global cancel only)"
                      />
                    </label>
                    <label>
                      <span>Customer reference</span>
                      <input
                        value={marketReadForm.order_management_customer_ref}
                        onChange={(event) => onMarketReadFormChange({ order_management_customer_ref: event.target.value })}
                        placeholder="Optional, max 32 safe chars"
                      />
                    </label>
                    <label>
                      <span>Market version (replace)</span>
                      <input
                        value={marketReadForm.order_management_market_version}
                        onChange={(event) => onMarketReadFormChange({ order_management_market_version: event.target.value })}
                        placeholder="Optional positive integer"
                      />
                    </label>
                    <label className="wide-field">
                      <span>Instructions JSON (max 60)</span>
                      <textarea
                        value={marketReadForm.order_management_instructions}
                        onChange={(event) => onMarketReadFormChange({ order_management_instructions: event.target.value })}
                        rows={3}
                        spellCheck={false}
                      />
                    </label>
                    <label>
                      <span>Global-cancel confirmation</span>
                      <input
                        value={marketReadForm.order_management_confirmation}
                        onChange={(event) => onMarketReadFormChange({ order_management_confirmation: event.target.value })}
                        placeholder="Type CANCEL ALL BETS"
                      />
                    </label>
                    <label className="check-row">
                      <input
                        type="checkbox"
                        checked={marketReadForm.order_management_async}
                        onChange={(event) => onMarketReadFormChange({ order_management_async: event.target.checked })}
                      />
                      <span>Async replace request</span>
                    </label>
                  </>
                )}
              </div>
              <div className="button-row compact">
                <button
                  className="danger-button"
                  type="button"
                  disabled={marketReadBusy !== null}
                  onClick={onMarketOrderManagement}
                >
                  Submit guarded live mutation
                </button>
              </div>
            </div>
          ) : null}
          {marketRead.events ? (
            <div className="table-wrap">
              <h4>Events ({marketRead.events.events.length})</h4>
              <table>
                <thead><tr><th>Event</th><th>Title</th><th>Status</th><th /></tr></thead>
                <tbody>
                  {marketRead.events.events.map((event) => (
                    <tr key={event.event_id}>
                      <td>{event.event_id}</td><td>{event.title}</td><td>{event.status || "-"}</td>
                      <td><button className="secondary-button" type="button" onClick={() => onMarketReadFormChange({ event_id: event.event_id })}>Use</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
          {marketRead.contracts ? (
            <div className="table-wrap">
              <h4>Contracts ({marketRead.contracts.contracts.length})</h4>
              <table>
                <thead><tr><th>Contract</th><th>Title</th><th>Outcome</th><th>Status</th><th /></tr></thead>
                <tbody>
                  {marketRead.contracts.contracts.map((contract) => (
                    <tr key={contract.contract_id}>
                      <td>{contract.contract_id}</td><td>{contract.title}</td><td>{contract.outcome || "-"}</td><td>{contract.status || "-"}</td>
                      <td><button className="secondary-button" type="button" onClick={() => onMarketReadFormChange({ contract_id: contract.contract_id })}>Use</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
          {marketRead.price ? (
            <div className="diagnostic-grid">
              <div><span>Last</span><strong>{formatNumber(marketRead.price.price?.last)}</strong></div>
              <div><span>Bid</span><strong>{formatNumber(marketRead.price.price?.bid)}</strong></div>
              <div><span>Ask</span><strong>{formatNumber(marketRead.price.price?.ask)}</strong></div>
              <div><span>Midpoint</span><strong>{formatNumber(marketRead.price.price?.midpoint)}</strong></div>
            </div>
          ) : null}
          {marketRead.orderbook?.orderbook ? (
            <div className="table-wrap">
              <h4>Orderbook ({marketRead.orderbook.orderbook.contract_id})</h4>
              <div className="diagnostic-grid">
                <div><span>Best bid</span><strong>{formatNumber(marketRead.orderbook.orderbook.best_bid)}</strong></div>
                <div><span>Best ask</span><strong>{formatNumber(marketRead.orderbook.orderbook.best_ask)}</strong></div>
              </div>
              <table>
                <thead><tr><th>Side</th><th>Price</th><th>Size</th></tr></thead>
                <tbody>
                  {[...marketRead.orderbook.orderbook.bids.map((level) => ({ ...level, side: "bid" })), ...marketRead.orderbook.orderbook.asks.map((level) => ({ ...level, side: "ask" }))].map((level, index) => (
                    <tr key={`${level.side}-${level.price}-${index}`}><td>{level.side}</td><td>{formatNumber(level.price)}</td><td>{formatNumber(level.size)}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
          {marketRead.trades ? (
            <div className="table-wrap">
              <h4>Trades ({marketRead.trades.trades.length})</h4>
              <table>
                <thead><tr><th>Time</th><th>Side</th><th>Price</th><th>Size</th><th>Trade</th></tr></thead>
                <tbody>{marketRead.trades.trades.map((trade) => (
                  <tr key={trade.trade_id}><td>{formatUnknownTime(trade.timestamp)}</td><td>{trade.side}</td><td>{formatNumber(trade.price)}</td><td>{formatNumber(trade.size)}</td><td>{trade.trade_id}</td></tr>
                ))}</tbody>
              </table>
            </div>
          ) : null}
          {marketRead.candles ? (
            <div className="table-wrap">
              <h4>Candles ({marketRead.candles.candles.length})</h4>
              <table>
                <thead><tr><th>Time</th><th>Open</th><th>High</th><th>Low</th><th>Close</th><th>Volume</th></tr></thead>
                <tbody>{marketRead.candles.candles.map((candle) => (
                  <tr key={candle.timestamp}><td>{formatUnknownTime(candle.timestamp)}</td><td>{formatNumber(candle.open)}</td><td>{formatNumber(candle.high)}</td><td>{formatNumber(candle.low)}</td><td>{formatNumber(candle.close)}</td><td>{formatNumber(candle.volume)}</td></tr>
                ))}</tbody>
              </table>
            </div>
          ) : null}
          {marketRead.account ? (
            <div className="table-wrap">
              <h4>Account: {marketRead.account.operation.replaceAll("_", " ")}</h4>
              <pre className="account-json">{JSON.stringify(marketRead.account.data, null, 2)}</pre>
            </div>
          ) : null}
          {marketRead.order_management ? (
            <div className="table-wrap">
              <h4>Order-management response: {marketRead.order_management.operation.replaceAll("_", " ")}</h4>
              <pre className="account-json">{JSON.stringify(marketRead.order_management.data, null, 2)}</pre>
            </div>
          ) : null}
        </div>
      ) : null}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Enabled</th>
              <th>Market</th>
              <th>Capabilities</th>
              <th>Access</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {filteredMarkets.map((market) => {
              const capabilities = enabledCapabilities(market);
              const implemented = capabilities.length > 0;
              return (
                <tr key={market.market_id}>
                  <td>
                    <button
                      className={`switch ${market.enabled ? "on" : ""}`}
                      disabled={busyMarket === market.market_id}
                      onClick={() => onToggle(market)}
                      title={market.enabled ? "Disable market" : "Enable market"}
                    >
                      <span />
                    </button>
                  </td>
                  <td>
                    <strong>{market.display_name}</strong>
                    <small>{market.market_id}</small>
                  </td>
                  <td>
                    <div className="chip-row">
                      {capabilities.slice(0, 5).map((capability) => (
                        <span className="chip" key={capability}>
                          {capability.replace(/_/g, " ")}
                        </span>
                      ))}
                      {capabilities.length > 5 ? <span className="chip muted">+{capabilities.length - 5}</span> : null}
                      {!implemented ? <span className="chip muted">blocked</span> : null}
                    </div>
                  </td>
                  <td>
                    <div className="stacked">
                      <span>{market.capabilities.api_required ? "API required" : "No API flag"}</span>
                      <small>{credentialRequirementText(market)}</small>
                    </div>
                  </td>
                  <td>
                    <StatusPill tone={market.enabled ? "good" : implemented ? "neutral" : "warn"}>
                      {market.enabled ? "enabled" : implemented ? "disabled" : "blocked"}
                    </StatusPill>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function PolymarketAnalyticsView({
  filters,
  loading,
  message,
  mddAuditDetail,
  mddCache,
  mddCacheBusyKey,
  mddForm,
  mddPayload,
  onAuditDetailLoad,
  onMddCachePurge,
  onMddCacheRefresh,
  onFiltersChange,
  onLeaderboardRefresh,
  onMddFormChange,
  onMddLookup,
  onUserSearch,
  onUserSearchQueryChange,
  searchQuery,
  searchResults,
  leaderboard
}: {
  filters: PolymarketLeaderboardFilters;
  loading: boolean;
  message: string;
  mddAuditDetail: PolymarketMddAuditExport | null;
  mddCache: PolymarketMddCachePayload | null;
  mddCacheBusyKey: string | null;
  mddForm: PolymarketMddForm;
  mddPayload: PolymarketMddPayload | null;
  onAuditDetailLoad: (cacheKey: string) => void;
  onMddCachePurge: (request: PolymarketMddCachePurgeRequest) => void;
  onMddCacheRefresh: () => void;
  onFiltersChange: (filters: PolymarketLeaderboardFilters) => void;
  onLeaderboardRefresh: (event: FormEvent<HTMLFormElement>) => void;
  onMddFormChange: (form: PolymarketMddForm) => void;
  onMddLookup: (event: FormEvent<HTMLFormElement>) => void;
  onUserSearch: (event: FormEvent<HTMLFormElement>) => void;
  onUserSearchQueryChange: (value: string) => void;
  searchQuery: string;
  searchResults: PolymarketUserSearchPayload | null;
  leaderboard: PolymarketLeaderboardPayload | null;
}) {
  const updateFilter = <K extends keyof PolymarketLeaderboardFilters>(field: K, value: PolymarketLeaderboardFilters[K]) => {
    onFiltersChange({ ...filters, [field]: value });
  };
  const updateMddForm = <K extends keyof PolymarketMddForm>(field: K, value: PolymarketMddForm[K]) => {
    onMddFormChange({ ...mddForm, [field]: value });
  };
  const auditPayload = mddAuditDetail?.payload ?? null;
  const auditCacheKey = mddAuditDetail?.cache?.key ?? auditPayload?.audit_cache?.key ?? null;
  const auditPoints = auditPayload?.points?.slice(0, 12) ?? [];
  const walletMddCacheKey = mddPayload?.audit_cache?.key ?? null;
  const cacheEntries = mddCache?.entries ?? [];
  const cacheStatus = mddCache?.cache ?? null;

  return (
    <div className="content-grid">
      <section className="panel span-2">
        <div className="panel-title">
          <Search size={18} />
          <h2>User Search</h2>
        </div>
        <form className="analytics-form user-search-form" onSubmit={onUserSearch}>
          <label>
            <span>Search</span>
            <input value={searchQuery} onChange={(event) => onUserSearchQueryChange(event.target.value)} placeholder="Profile, name, wallet" />
          </label>
          <button className="icon-button" type="submit" disabled={loading || !searchQuery.trim()}>
            <Search size={17} /> Search
          </button>
        </form>
        {searchResults ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Profile</th>
                  <th>Wallet</th>
                  <th>Public</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {searchResults.profiles.map((profile) => (
                  <tr key={profile.proxy_wallet}>
                    <td>
                      <strong>{profile.pseudonym || "-"}</strong>
                      <small>{profile.profile_image || "-"}</small>
                    </td>
                    <td>{profile.proxy_wallet}</td>
                    <td>
                      <StatusPill tone={profile.display_username_public ? "good" : "neutral"}>
                        {profile.display_username_public ? "yes" : "hidden"}
                      </StatusPill>
                    </td>
                    <td>
                      <button className="icon-button compact" type="button" onClick={() => updateMddForm("wallet", profile.proxy_wallet)}>
                        <Wallet size={14} /> Use
                      </button>
                    </td>
                  </tr>
                ))}
                {!searchResults.profiles.length ? (
                  <tr>
                    <td colSpan={4} className="empty-cell">
                      No matching profiles.
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>

      <section className="panel span-2">
        <div className="panel-title">
          <Wallet size={18} />
          <h2>Direct Wallet MDD</h2>
        </div>
        <form className="analytics-form direct-mdd-form" onSubmit={onMddLookup}>
          <label>
            <span>Wallet</span>
            <input value={mddForm.wallet} onChange={(event) => updateMddForm("wallet", event.target.value)} placeholder="0x..." />
          </label>
          <label>
            <span>MDD mode</span>
            <select value={mddForm.mode} onChange={(event) => updateMddForm("mode", event.target.value as PolymarketMddForm["mode"])}>
              <option value="fast">Fast public curve</option>
              <option value="mark_replay">CLOB mark replay</option>
            </select>
          </label>
          <label>
            <span>Closed</span>
            <input inputMode="numeric" min="1" max="1000" type="number" value={mddForm.closed_limit} onChange={(event) => updateMddForm("closed_limit", event.target.value)} />
          </label>
          <label>
            <span>Activity</span>
            <input inputMode="numeric" min="0" max="5000" type="number" value={mddForm.activity_limit} onChange={(event) => updateMddForm("activity_limit", event.target.value)} />
          </label>
          <label>
            <span>Trades</span>
            <input inputMode="numeric" min="0" max="5000" type="number" value={mddForm.trade_limit} onChange={(event) => updateMddForm("trade_limit", event.target.value)} />
          </label>
          <label>
            <span>Open</span>
            <input inputMode="numeric" min="0" max="1000" type="number" value={mddForm.open_limit} onChange={(event) => updateMddForm("open_limit", event.target.value)} />
          </label>
          <label>
            <span>Points</span>
            <input inputMode="numeric" min="1" max="1000" type="number" value={mddForm.max_points} onChange={(event) => updateMddForm("max_points", event.target.value)} />
          </label>
          <label>
            <span>Equity base</span>
            <input inputMode="decimal" value={mddForm.equity_base_usd} onChange={(event) => updateMddForm("equity_base_usd", event.target.value)} />
          </label>
          <label>
            <span>Replay tokens</span>
            <input
              inputMode="numeric"
              min="1"
              max="20"
              type="number"
              value={mddForm.mark_replay_token_limit}
              onChange={(event) => updateMddForm("mark_replay_token_limit", event.target.value)}
            />
          </label>
          <label>
            <span>Replay interval</span>
            <select value={mddForm.mark_replay_interval} onChange={(event) => updateMddForm("mark_replay_interval", event.target.value)}>
              <option value="1h">1h</option>
              <option value="6h">6h</option>
              <option value="1d">1d</option>
              <option value="1w">1w</option>
              <option value="all">All</option>
              <option value="max">Max</option>
            </select>
          </label>
          <label>
            <span>Replay fidelity</span>
            <input
              inputMode="numeric"
              min="1"
              max="1440"
              type="number"
              value={mddForm.mark_replay_fidelity}
              onChange={(event) => updateMddForm("mark_replay_fidelity", event.target.value)}
            />
          </label>
          <label className="check-row">
            <input
              type="checkbox"
              checked={mddForm.include_accounting_snapshot}
              onChange={(event) => updateMddForm("include_accounting_snapshot", event.target.checked)}
            />
            <span>Accounting base</span>
          </label>
          <label className="check-row">
            <input type="checkbox" checked={mddForm.persist_cache} onChange={(event) => updateMddForm("persist_cache", event.target.checked)} />
            <span>Audit cache</span>
          </label>
          <button className="icon-button" type="submit" disabled={loading || !mddForm.wallet.trim()}>
            <RefreshCw size={17} /> Compute
          </button>
        </form>
        {mddPayload ? (
          <>
            <div className="metrics-grid four">
              <Metric label="MDD USD" value={formatUsd(mddPayload.mdd_usd)} tone={mddPayload.mdd_available ? "warn" : "neutral"} />
              <Metric label="MDD %" value={formatPercent(mddPayload.mdd_pct)} />
              <Metric label="Peak" value={formatUsd(mddPayload.peak_value)} />
              <Metric label="Trough" value={formatUsd(mddPayload.trough_value)} />
            </div>
            <div className="audit-summary">
              <div>
                <span>Method</span>
                <strong>{mddPayload.mdd_method}</strong>
              </div>
              <div>
                <span>Equity base</span>
                <strong>{formatUsd(mddPayload.equity_base_usd)}</strong>
              </div>
              <div>
                <span>Positions</span>
                <strong>
                  {formatAuditValue([`closed ${mddPayload.closed_positions ?? 0}`, `open ${mddPayload.open_positions ?? 0}`])}
                </strong>
              </div>
              <div>
                <span>Cache</span>
                <strong>{walletMddCacheKey || "not stored"}</strong>
              </div>
            </div>
            {mddPayload.rate_limit?.limited ? <div className="info-banner warn">Polymarket rate limit reached; retry after the upstream backoff window.</div> : null}
            {walletMddCacheKey ? (
              <div className="audit-actions panel-actions">
                <button className="icon-button compact" type="button" onClick={() => onAuditDetailLoad(walletMddCacheKey)}>
                  <Database size={14} /> Detail
                </button>
                <a className="icon-button compact" href={polymarketMddExportUrl(walletMddCacheKey, "json")}>
                  <Download size={14} /> JSON
                </a>
                <a className="icon-button compact" href={polymarketMddExportUrl(walletMddCacheKey, "csv")}>
                  <Download size={14} /> CSV
                </a>
              </div>
            ) : null}
          </>
        ) : null}
      </section>

      <section className="panel span-2">
        <div className="panel-header">
          <div className="panel-title">
            <Database size={18} />
            <h2>MDD Audit Cache</h2>
          </div>
          <StatusPill tone={cacheStatus?.exists ? "good" : "neutral"}>{cacheStatus?.exists ? "on disk" : "empty"}</StatusPill>
        </div>
        <div className="metrics-grid four">
          <Metric label="Entries" value={mddCache?.counts.entries ?? cacheStatus?.entries ?? 0} />
          <Metric label="Active" value={mddCache?.counts.active_entries ?? cacheStatus?.active_entries ?? 0} tone="good" />
          <Metric label="Expired" value={mddCache?.counts.expired_entries ?? cacheStatus?.expired_entries ?? 0} tone={(mddCache?.counts.expired_entries ?? 0) ? "warn" : "neutral"} />
          <Metric label="Size" value={formatBytes(cacheStatus?.size_bytes)} />
        </div>
        <div className="audit-summary cache-health">
          <div>
            <span>Path</span>
            <strong>{cacheStatus?.path ?? "-"}</strong>
          </div>
          <div>
            <span>TTL</span>
            <strong>{cacheStatus?.ttl_seconds ? `${cacheStatus.ttl_seconds}s` : "-"}</strong>
          </div>
          <div>
            <span>Max entries</span>
            <strong>{cacheStatus?.max_entries ?? "-"}</strong>
          </div>
          <div>
            <span>Newest</span>
            <strong>{formatUnknownTime(cacheStatus?.newest_stored_at)}</strong>
          </div>
        </div>
        <div className="audit-actions panel-actions cache-actions">
          <button className="icon-button compact" type="button" onClick={onMddCacheRefresh} disabled={loading}>
            <RefreshCw size={14} /> Refresh
          </button>
          <button
            className="icon-button compact"
            type="button"
            onClick={() => onMddCachePurge({ expired_only: true })}
            disabled={mddCacheBusyKey === "__expired__"}
          >
            <Trash2 size={14} /> Purge expired
          </button>
          <button
            className="icon-button compact danger"
            type="button"
            onClick={() => onMddCachePurge({ all: true })}
            disabled={!cacheEntries.length || mddCacheBusyKey === "__all__"}
          >
            <Trash2 size={14} /> Clear all
          </button>
        </div>
        <div className="table-wrap audit-points cache-table">
          <table>
            <thead>
              <tr>
                <th>Stored</th>
                <th>Wallet</th>
                <th className="numeric">MDD USD</th>
                <th className="numeric">MDD %</th>
                <th>Retention</th>
                <th>Status</th>
                <th>Key</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {cacheEntries.map((entry) => {
                const cacheKey = entry.key ?? "";
                return (
                  <tr key={cacheKey || `${entry.wallet}-${entry.stored_at}`}>
                    <td>{formatUnknownTime(entry.stored_at)}</td>
                    <td>{entry.wallet || "-"}</td>
                    <td className="numeric">{formatUsd(entry.mdd_usd)}</td>
                    <td className="numeric">{formatPercent(entry.mdd_pct)}</td>
                    <td>
                      <strong>{entry.ttl_remaining_seconds === null || entry.ttl_remaining_seconds === undefined ? "-" : `${entry.ttl_remaining_seconds}s`}</strong>
                      <small>age {entry.age_seconds === null || entry.age_seconds === undefined ? "-" : `${entry.age_seconds}s`}</small>
                    </td>
                    <td>
                      <StatusPill tone={entry.expired ? "warn" : "good"}>{entry.expired ? "expired" : "active"}</StatusPill>
                    </td>
                    <td className="cache-key">{cacheKey || "-"}</td>
                    <td>
                      {cacheKey ? (
                        <span className="audit-actions">
                          <button className="icon-button compact" type="button" onClick={() => onAuditDetailLoad(cacheKey)}>
                            <Database size={14} /> Detail
                          </button>
                          <a className="icon-button compact" href={polymarketMddExportUrl(cacheKey, "json")}>
                            <Download size={14} /> JSON
                          </a>
                          <a className="icon-button compact" href={polymarketMddExportUrl(cacheKey, "csv")}>
                            <Download size={14} /> CSV
                          </a>
                          <button
                            className="icon-button compact danger"
                            type="button"
                            onClick={() => onMddCachePurge({ key: cacheKey })}
                            disabled={mddCacheBusyKey === cacheKey}
                          >
                            <Trash2 size={14} /> Delete
                          </button>
                        </span>
                      ) : (
                        "-"
                      )}
                    </td>
                  </tr>
                );
              })}
              {!cacheEntries.length ? (
                <tr>
                  <td colSpan={8} className="empty-cell">
                    No cached MDD audit artifacts.
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </section>

      <section className="panel span-2">
        <div className="panel-title">
          <Trophy size={18} />
          <h2>Leaderboard</h2>
        </div>
        <form className="analytics-form leaderboard-form" onSubmit={onLeaderboardRefresh}>
          <label>
            <span>Sort</span>
            <select value={filters.sort} onChange={(event) => updateFilter("sort", event.target.value as PolymarketLeaderboardSort)}>
              <option value="roi_pct">ROI %</option>
              <option value="pnl_usd">PnL USD</option>
              <option value="volume_usd">Volume USD</option>
              <option value="mdd_pct">MDD %</option>
              <option value="mdd_usd">MDD USD</option>
            </select>
          </label>
          <label>
            <span>Direction</span>
            <select value={filters.direction} onChange={(event) => updateFilter("direction", event.target.value as PolymarketLeaderboardFilters["direction"])}>
              <option value="DESC">High to low</option>
              <option value="ASC">Low to high</option>
            </select>
          </label>
          <label>
            <span>Returned</span>
            <input
              inputMode="text"
              type="text"
              value={filters.limit}
              onChange={(event) => updateFilter("limit", event.target.value)}
            />
          </label>
          <label>
            <span>Scanned</span>
            <input
              inputMode="text"
              type="text"
              value={filters.scan_limit}
              onChange={(event) => updateFilter("scan_limit", event.target.value)}
            />
          </label>
          <label className="check-row">
            <input
              type="checkbox"
              checked={filters.compute_mdd}
              onChange={(event) => updateFilter("compute_mdd", event.target.checked)}
            />
            <span>Compute MDD</span>
          </label>
          <label>
            <span>MDD mode</span>
            <select value={filters.mdd_mode} onChange={(event) => updateFilter("mdd_mode", event.target.value as PolymarketLeaderboardFilters["mdd_mode"])}>
              <option value="fast">Fast public curve</option>
              <option value="mark_replay">CLOB mark replay</option>
            </select>
          </label>
          <label>
            <span>MDD scan</span>
            <input
              inputMode="text"
              type="text"
              value={filters.mdd_scan_limit}
              onChange={(event) => updateFilter("mdd_scan_limit", event.target.value)}
            />
          </label>
          <label>
            <span>MDD history</span>
            <input
              inputMode="numeric"
              min="1"
              max="1000"
              type="number"
              value={filters.mdd_history_limit}
              onChange={(event) => updateFilter("mdd_history_limit", event.target.value)}
            />
          </label>
          <label>
            <span>MDD activity</span>
            <input
              inputMode="numeric"
              min="0"
              max="5000"
              type="number"
              value={filters.mdd_activity_limit}
              onChange={(event) => updateFilter("mdd_activity_limit", event.target.value)}
            />
          </label>
          <label>
            <span>MDD trades</span>
            <input
              inputMode="numeric"
              min="0"
              max="5000"
              type="number"
              value={filters.mdd_trade_limit}
              onChange={(event) => updateFilter("mdd_trade_limit", event.target.value)}
            />
          </label>
          <label>
            <span>MDD open</span>
            <input
              inputMode="numeric"
              min="0"
              max="1000"
              type="number"
              value={filters.mdd_open_limit}
              onChange={(event) => updateFilter("mdd_open_limit", event.target.value)}
            />
          </label>
          <label>
            <span>Replay tokens</span>
            <input
              inputMode="numeric"
              min="1"
              max="20"
              type="number"
              value={filters.mdd_mark_replay_token_limit}
              onChange={(event) => updateFilter("mdd_mark_replay_token_limit", event.target.value)}
            />
          </label>
          <label>
            <span>Replay fidelity</span>
            <input
              inputMode="numeric"
              min="1"
              max="1440"
              type="number"
              value={filters.mdd_mark_replay_fidelity}
              onChange={(event) => updateFilter("mdd_mark_replay_fidelity", event.target.value)}
            />
          </label>
          <label>
            <span>Replay interval</span>
            <select value={filters.mdd_mark_replay_interval} onChange={(event) => updateFilter("mdd_mark_replay_interval", event.target.value)}>
              <option value="1h">1h</option>
              <option value="6h">6h</option>
              <option value="1d">1d</option>
              <option value="1w">1w</option>
              <option value="all">All</option>
              <option value="max">Max</option>
            </select>
          </label>
          <label className="check-row">
            <input
              type="checkbox"
              checked={filters.mdd_include_accounting}
              onChange={(event) => updateFilter("mdd_include_accounting", event.target.checked)}
            />
            <span>Accounting base</span>
          </label>
          <label className="check-row">
            <input
              type="checkbox"
              checked={filters.mdd_persist_cache}
              onChange={(event) => updateFilter("mdd_persist_cache", event.target.checked)}
            />
            <span>Audit cache</span>
          </label>
          <label>
            <span>Equity base</span>
            <input inputMode="decimal" value={filters.equity_base_usd} onChange={(event) => updateFilter("equity_base_usd", event.target.value)} />
          </label>
          <label>
            <span>Min PnL</span>
            <input inputMode="decimal" value={filters.min_pnl_usd} onChange={(event) => updateFilter("min_pnl_usd", event.target.value)} />
          </label>
          <label>
            <span>Max PnL</span>
            <input inputMode="decimal" value={filters.max_pnl_usd} onChange={(event) => updateFilter("max_pnl_usd", event.target.value)} />
          </label>
          <label>
            <span>Min Volume</span>
            <input inputMode="decimal" value={filters.min_volume_usd} onChange={(event) => updateFilter("min_volume_usd", event.target.value)} />
          </label>
          <label>
            <span>Max Volume</span>
            <input inputMode="decimal" value={filters.max_volume_usd} onChange={(event) => updateFilter("max_volume_usd", event.target.value)} />
          </label>
          <label>
            <span>Min ROI %</span>
            <input inputMode="decimal" value={filters.min_roi_pct} onChange={(event) => updateFilter("min_roi_pct", event.target.value)} />
          </label>
          <label>
            <span>Max ROI %</span>
            <input inputMode="decimal" value={filters.max_roi_pct} onChange={(event) => updateFilter("max_roi_pct", event.target.value)} />
          </label>
          <label>
            <span>Min MDD USD</span>
            <input inputMode="decimal" value={filters.min_mdd_usd} onChange={(event) => updateFilter("min_mdd_usd", event.target.value)} />
          </label>
          <label>
            <span>Max MDD USD</span>
            <input inputMode="decimal" value={filters.max_mdd_usd} onChange={(event) => updateFilter("max_mdd_usd", event.target.value)} />
          </label>
          <label>
            <span>Min MDD %</span>
            <input inputMode="decimal" value={filters.min_mdd_pct} onChange={(event) => updateFilter("min_mdd_pct", event.target.value)} />
          </label>
          <label>
            <span>Max MDD %</span>
            <input inputMode="decimal" value={filters.max_mdd_pct} onChange={(event) => updateFilter("max_mdd_pct", event.target.value)} />
          </label>
          <button className="icon-button" type="submit" disabled={loading}>
            <RefreshCw size={17} /> Load
          </button>
        </form>

        {message ? <div className={`info-banner ${leaderboard?.warnings.length ? "warn" : ""}`}>{message}</div> : null}
        {leaderboard ? (
          <>
            <div className="metrics-grid four">
              <Metric label="Returned" value={leaderboard.counts.returned} tone="good" />
              <Metric label="Filtered" value={leaderboard.counts.filtered} />
              <Metric label="Scanned" value={leaderboard.counts.scanned} />
              <Metric label="MDD computed" value={leaderboard.counts.mdd_computed} />
            </div>
            <div className={`info-banner ${leaderboard.source_enumeration_complete ? "" : "warn"}`}>
              Scan ended: {leaderboard.completion_reason.replaceAll("_", " ")}. {leaderboard.source_scope_note}
            </div>
            <div className={`info-banner ${leaderboard.mdd_available ? "" : "warn"}`}>{leaderboard.mdd_note}</div>
            {leaderboard.rate_limit.limited ? <div className="info-banner warn">Polymarket rate limit reached; retry after the upstream backoff window.</div> : null}
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Rank</th>
                    <th>User</th>
                    <th className="numeric">PnL</th>
                    <th className="numeric">Volume</th>
                    <th className="numeric">ROI</th>
                    <th className="numeric">Trades</th>
                    <th className="numeric">MDD USD</th>
                    <th className="numeric">MDD %</th>
                    <th>MDD source</th>
                    <th>Audit</th>
                  </tr>
                </thead>
                <tbody>
                  {leaderboard.rows.map((row) => (
                    <tr key={`${row.rank}-${row.wallet || row.display_name}`}>
                      <td>{row.rank}</td>
                      <td>
                        <strong>{row.display_name}</strong>
                        <small>{row.wallet || "-"}</small>
                      </td>
                      <td className="numeric">{formatUsd(row.pnl_usd)}</td>
                      <td className="numeric">{formatUsd(row.volume_usd)}</td>
                      <td className="numeric">{formatPercent(row.roi_pct)}</td>
                      <td className="numeric">{row.trade_count || "-"}</td>
                      <td className="numeric">{row.mdd_available ? formatUsd(row.mdd_usd) : "-"}</td>
                      <td className="numeric">{row.mdd_available ? formatPercent(row.mdd_pct) : "-"}</td>
                      <td>
                        {row.mdd_available
                          ? row.mdd_accounting_status ?? row.mdd_mark_replay_status ?? (row.mdd_method?.includes("mark") ? "mark replay" : "fast")
                          : "-"}
                      </td>
                      <td>
                        {row.mdd_audit_cache_key ? (
                          <span className="audit-actions">
                            <button className="icon-button compact" type="button" onClick={() => onAuditDetailLoad(row.mdd_audit_cache_key ?? "")}>
                              <Database size={14} /> Detail
                            </button>
                            <a className="icon-button compact" href={polymarketMddExportUrl(row.mdd_audit_cache_key, "json")}>
                              <Download size={14} /> JSON
                            </a>
                            <a className="icon-button compact" href={polymarketMddExportUrl(row.mdd_audit_cache_key, "csv")}>
                              <Download size={14} /> CSV
                            </a>
                          </span>
                        ) : (
                          "-"
                        )}
                      </td>
                    </tr>
                  ))}
                  {!leaderboard.rows.length ? (
                    <tr>
                      <td colSpan={10} className="empty-cell">
                        No leaderboard rows matched the filters.
                      </td>
                    </tr>
                  ) : null}
                </tbody>
              </table>
            </div>
          </>
        ) : null}
      </section>

      {auditPayload ? (
        <section className="panel span-2">
          <div className="panel-header">
            <div className="panel-title">
              <Database size={18} />
              <h2>MDD Audit Detail</h2>
            </div>
            {auditCacheKey ? <StatusPill tone="good">cached</StatusPill> : <StatusPill>direct</StatusPill>}
          </div>
          <div className="metrics-grid four">
            <Metric label="MDD USD" value={formatUsd(auditPayload.mdd_usd)} tone={auditPayload.mdd_available ? "warn" : "neutral"} />
            <Metric label="MDD %" value={formatPercent(auditPayload.mdd_pct)} />
            <Metric label="Equity base" value={formatUsd(auditPayload.equity_base_usd)} />
            <Metric label="Points" value={auditPayload.points_total ?? auditPayload.points?.length ?? 0} />
          </div>
          <div className="audit-summary">
            <div>
              <span>Wallet</span>
              <strong>{auditPayload.wallet || "-"}</strong>
            </div>
            <div>
              <span>Method</span>
              <strong>{auditPayload.mdd_method}</strong>
            </div>
            <div>
              <span>Peak</span>
              <strong>
                {formatUsd(auditPayload.peak_value)} at {formatUnknownTime(auditPayload.peak_timestamp)}
              </strong>
            </div>
            <div>
              <span>Trough</span>
              <strong>
                {formatUsd(auditPayload.trough_value)} at {formatUnknownTime(auditPayload.trough_timestamp)}
              </strong>
            </div>
            <div>
              <span>Closed/open</span>
              <strong>
                {formatAuditValue([`closed ${auditPayload.closed_positions ?? 0}`, `open ${auditPayload.open_positions ?? 0}`])}
              </strong>
            </div>
            <div>
              <span>Activity/trades</span>
              <strong>
                {formatAuditValue([`activity ${auditPayload.activity_events ?? 0}`, `trades ${auditPayload.trade_events ?? 0}`])}
              </strong>
            </div>
            <div>
              <span>Cache key</span>
              <strong>{auditCacheKey || "-"}</strong>
            </div>
            <div>
              <span>Cache path</span>
              <strong>{mddAuditDetail?.cache?.path || auditPayload.audit_cache?.path || "-"}</strong>
            </div>
          </div>
          {auditCacheKey ? (
            <div className="audit-actions panel-actions">
              <a className="icon-button compact" href={polymarketMddExportUrl(auditCacheKey, "json")}>
                <Download size={14} /> JSON
              </a>
              <a className="icon-button compact" href={polymarketMddExportUrl(auditCacheKey, "csv")}>
                <Download size={14} /> CSV
              </a>
            </div>
          ) : null}
          <div className="table-wrap audit-points">
            <table>
              <thead>
                <tr>
                  <th>Time</th>
                  <th className="numeric">Value</th>
                  <th>Source</th>
                </tr>
              </thead>
              <tbody>
                {auditPoints.map((point, index) => (
                  <tr key={`${point.timestamp ?? index}-${index}`}>
                    <td>{formatUnknownTime(point.timestamp)}</td>
                    <td className="numeric">{formatUnknownNumber(point.value, 4)}</td>
                    <td>{point.source ?? point.kind ?? "-"}</td>
                  </tr>
                ))}
                {!auditPoints.length ? (
                  <tr>
                    <td colSpan={3} className="empty-cell">
                      No audit points in this payload.
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
          <div className="audit-detail-grid">
            <div>
              <span>Assumptions</span>
              <strong>{formatAuditValue(auditPayload.assumptions)}</strong>
            </div>
            <div>
              <span>Limitations</span>
              <strong>{formatAuditValue(auditPayload.limitations)}</strong>
            </div>
          </div>
        </section>
      ) : null}
    </div>
  );
}

function LiveSafetyView({
  busyMarket,
  form,
  livePreflight,
  liveSafety,
  liveValidation,
  liveValidationAllowDuplicate,
  liveValidationDecisionForm,
  liveValidationDecisions,
  liveValidationImport,
  liveValidationPromotionProposal,
  liveValidationPromotionProposalSnapshotDetail,
  liveValidationPromotionProposalSnapshots,
  liveValidationPromotionProposalTargetTier,
  liveValidationReportDetail,
  liveValidationReportBusyKey,
  liveValidationReportMessage,
  liveValidationReportSchemaValidation,
  liveValidationReports,
  markets,
  message,
  onFormChange,
  onPreview,
  onValidationImport,
  onValidationAllowDuplicateChange,
  onValidationDecisionChange,
  onValidationDecisionSubmit,
  onValidationImportChange,
  onValidationProposalRefresh,
  onValidationProposalSnapshotDelete,
  onValidationProposalSnapshotOpen,
  onValidationProposalSnapshotsRefresh,
  onValidationProposalSnapshotStore,
  onValidationProposalTargetChange,
  onValidationReportDelete,
  onValidationReportOpen,
  onValidationReportsRefresh,
  onValidationRefresh,
  onValidationSnapshotStore,
  onSelectedMarketChange,
  onSettingsSave,
  selectedMarket,
  selectedMarketId
}: {
  busyMarket: string | null;
  form: PaperOrderForm;
  livePreflight: LivePreflightPayload | null;
  liveSafety: LiveSafetyPayload | null;
  liveValidation: PolymarketLiveValidationPayload | null;
  liveValidationAllowDuplicate: boolean;
  liveValidationDecisionForm: LiveValidationDecisionForm;
  liveValidationDecisions: PolymarketLiveValidationDecisionLedgerPayload | null;
  liveValidationImport: string;
  liveValidationPromotionProposal: PolymarketLiveValidationPromotionProposalPayload | null;
  liveValidationPromotionProposalSnapshotDetail: PolymarketLiveValidationPromotionProposalSnapshotPayload | null;
  liveValidationPromotionProposalSnapshots: PolymarketLiveValidationPromotionProposalSnapshotsPayload | null;
  liveValidationPromotionProposalTargetTier: string;
  liveValidationReportDetail: PolymarketLiveValidationReportPayload | null;
  liveValidationReportBusyKey: string | null;
  liveValidationReportMessage: string;
  liveValidationReportSchemaValidation: PolymarketLiveValidationReportSchemaValidation | null;
  liveValidationReports: PolymarketLiveValidationReportsPayload | null;
  markets: MarketsPayload | null;
  message: string;
  onFormChange: (form: PaperOrderForm) => void;
  onPreview: () => void;
  onValidationImport: () => void;
  onValidationAllowDuplicateChange: (value: boolean) => void;
  onValidationDecisionChange: (value: LiveValidationDecisionForm) => void;
  onValidationDecisionSubmit: (event: FormEvent<HTMLFormElement>) => void;
  onValidationImportChange: (value: string) => void;
  onValidationProposalRefresh: () => void;
  onValidationProposalSnapshotDelete: (key: string) => void;
  onValidationProposalSnapshotOpen: (key: string) => void;
  onValidationProposalSnapshotsRefresh: () => void;
  onValidationProposalSnapshotStore: () => void;
  onValidationProposalTargetChange: (value: string) => void;
  onValidationReportDelete: (key: string) => void;
  onValidationReportOpen: (key: string) => void;
  onValidationReportsRefresh: () => void;
  onValidationRefresh: () => void;
  onValidationSnapshotStore: () => void;
  onSelectedMarketChange: (marketId: string) => void;
  onSettingsSave: (event: FormEvent<HTMLFormElement>) => void;
  selectedMarket: Market | null;
  selectedMarketId: string;
}) {
  const updateField = (field: keyof PaperOrderForm, value: string) => onFormChange({ ...form, [field]: value });
  const blockers = liveSafety?.blockers ?? [];

  return (
    <div className="content-grid">
      <section className="panel full">
        <div className="toolbar">
          <div className="panel-title">
            <ShieldCheck size={18} />
            <h2>Gate Controls</h2>
          </div>
          <select value={selectedMarketId} onChange={(event) => onSelectedMarketChange(event.target.value)}>
            {(markets?.markets ?? []).map((market) => (
              <option key={market.market_id} value={market.market_id}>
                {market.display_name}
              </option>
            ))}
          </select>
        </div>

        {selectedMarket ? (
          <form className="market-detail" key={`live-${selectedMarket.market_id}`} onSubmit={onSettingsSave}>
            <div className="market-detail-main">
              <div>
                <h2>{selectedMarket.display_name}</h2>
                <p>{selectedMarket.status_text}</p>
              </div>
              <StatusPill tone={liveSafety?.tone ?? "neutral"}>{liveSafety?.status ?? "unknown"}</StatusPill>
            </div>
            <div className="metrics-grid">
              <Metric label="Market" value={selectedMarket.enabled ? "enabled" : "disabled"} tone={selectedMarket.enabled ? "good" : "warn"} />
              <Metric label="Live" value={selectedMarket.safety.live_trading_enabled ? "enabled" : "off"} tone={selectedMarket.safety.live_trading_enabled ? "good" : undefined} />
              <Metric label="Max size" value={String(selectedMarket.safety.live_trading_max_size ?? "-")} />
              <Metric label="Max notional" value={String(selectedMarket.safety.live_trading_max_notional ?? "-")} />
            </div>
            {blockers.length ? (
              <div className="chip-row">
                {blockers.map((blocker) => (
                  <span className="chip muted" key={blocker}>
                    {blocker}
                  </span>
                ))}
              </div>
            ) : null}
            <div className="safety-grid">
              <label className="check-row">
                <input name="enabled" type="checkbox" defaultChecked={selectedMarket.enabled} />
                <span>Market enabled</span>
              </label>
              <label className="check-row">
                <input name="live_trading_enabled" type="checkbox" defaultChecked={selectedMarket.safety.live_trading_enabled} />
                <span>Live enabled</span>
              </label>
              <label className="check-row">
                <input name="live_trading_confirmed" type="checkbox" defaultChecked={selectedMarket.safety.live_trading_confirmed} />
                <span>Live acknowledged</span>
              </label>
              <label className="check-row">
                <input name="live_trading_kill_switch" type="checkbox" defaultChecked={selectedMarket.safety.live_trading_kill_switch} />
                <span>Kill switch</span>
              </label>
              <label>
                <span>Max size</span>
                <input name="live_trading_max_size" defaultValue={selectedMarket.safety.live_trading_max_size ?? ""} />
              </label>
              <label>
                <span>Max notional</span>
                <input name="live_trading_max_notional" defaultValue={selectedMarket.safety.live_trading_max_notional ?? ""} />
              </label>
              <button className="icon-button" type="submit" disabled={busyMarket === selectedMarket.market_id}>
                <Save size={17} /> Save Gate
              </button>
            </div>
          </form>
        ) : null}
      </section>

      <PolymarketLiveValidationPanel
        allowDuplicate={liveValidationAllowDuplicate}
        busyKey={liveValidationReportBusyKey}
        decisionForm={liveValidationDecisionForm}
        decisions={liveValidationDecisions}
        importText={liveValidationImport}
        message={liveValidationReportMessage}
        onDeleteReport={onValidationReportDelete}
        onImport={onValidationImport}
        onAllowDuplicateChange={onValidationAllowDuplicateChange}
        onDecisionChange={onValidationDecisionChange}
        onDecisionSubmit={onValidationDecisionSubmit}
        onImportTextChange={onValidationImportChange}
        onProposalRefresh={onValidationProposalRefresh}
        onProposalSnapshotDelete={onValidationProposalSnapshotDelete}
        onProposalSnapshotOpen={onValidationProposalSnapshotOpen}
        onProposalSnapshotsRefresh={onValidationProposalSnapshotsRefresh}
        onProposalSnapshotStore={onValidationProposalSnapshotStore}
        onProposalTargetChange={onValidationProposalTargetChange}
        onOpenReport={onValidationReportOpen}
        onRefresh={onValidationRefresh}
        onReportsRefresh={onValidationReportsRefresh}
        onStoreSnapshot={onValidationSnapshotStore}
        payload={liveValidation}
        proposal={liveValidationPromotionProposal}
        proposalSnapshotDetail={liveValidationPromotionProposalSnapshotDetail}
        proposalSnapshots={liveValidationPromotionProposalSnapshots}
        proposalTargetTier={liveValidationPromotionProposalTargetTier}
        reportDetail={liveValidationReportDetail}
        schemaValidation={liveValidationReportSchemaValidation}
        reports={liveValidationReports}
      />

      <section className="panel full">
        <div className="panel-title">
          <Database size={18} />
          <h2>Preflight Audit</h2>
        </div>
        <div className="paper-form">
          <label>
            <span>Market</span>
            <select
              value={form.market_id}
              onChange={(event) => {
                updateField("market_id", event.target.value);
                onSelectedMarketChange(event.target.value);
              }}
            >
              {(markets?.markets ?? []).map((market) => (
                <option key={market.market_id} value={market.market_id}>
                  {market.display_name}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>Contract</span>
            <input value={form.contract_id} onChange={(event) => updateField("contract_id", event.target.value)} />
          </label>
          <label>
            <span>Side</span>
            <select value={form.side} onChange={(event) => updateField("side", event.target.value)}>
              <option value="BUY">BUY</option>
              <option value="SELL">SELL</option>
              <option value="BACK">BACK</option>
              <option value="LAY">LAY</option>
            </select>
          </label>
          <label>
            <span>Size</span>
            <input value={form.size} onChange={(event) => updateField("size", event.target.value)} />
          </label>
          <label>
            <span>Limit</span>
            <input value={form.limit_price} onChange={(event) => updateField("limit_price", event.target.value)} />
          </label>
        </div>
        <div className="button-row">
          <button className="icon-button" onClick={onPreview}>
            <ShieldCheck size={17} /> Run Preflight
          </button>
        </div>
        {message ? <div className={`info-banner ${livePreflight?.blocked ? "warn" : ""}`}>{message}</div> : null}
        {livePreflight ? <LivePreflightAudit payload={livePreflight} /> : null}
      </section>
    </div>
  );
}

function PolymarketLiveValidationPanel({
  allowDuplicate,
  busyKey,
  decisionForm,
  decisions,
  importText,
  message,
  onAllowDuplicateChange,
  onDecisionChange,
  onDecisionSubmit,
  onDeleteReport,
  onImport,
  onImportTextChange,
  onProposalRefresh,
  onProposalSnapshotDelete,
  onProposalSnapshotOpen,
  onProposalSnapshotsRefresh,
  onProposalSnapshotStore,
  onProposalTargetChange,
  onOpenReport,
  payload,
  proposal,
  proposalSnapshotDetail,
  proposalSnapshots,
  proposalTargetTier,
  reportDetail,
  schemaValidation,
  reports,
  onRefresh,
  onReportsRefresh,
  onStoreSnapshot
}: {
  allowDuplicate: boolean;
  busyKey: string | null;
  decisionForm: LiveValidationDecisionForm;
  decisions: PolymarketLiveValidationDecisionLedgerPayload | null;
  importText: string;
  message: string;
  onAllowDuplicateChange: (value: boolean) => void;
  onDecisionChange: (value: LiveValidationDecisionForm) => void;
  onDecisionSubmit: (event: FormEvent<HTMLFormElement>) => void;
  onDeleteReport: (key: string) => void;
  onImport: () => void;
  onImportTextChange: (value: string) => void;
  onProposalRefresh: () => void;
  onProposalSnapshotDelete: (key: string) => void;
  onProposalSnapshotOpen: (key: string) => void;
  onProposalSnapshotsRefresh: () => void;
  onProposalSnapshotStore: () => void;
  onProposalTargetChange: (value: string) => void;
  onOpenReport: (key: string) => void;
  payload: PolymarketLiveValidationPayload | null;
  proposal: PolymarketLiveValidationPromotionProposalPayload | null;
  proposalSnapshotDetail: PolymarketLiveValidationPromotionProposalSnapshotPayload | null;
  proposalSnapshots: PolymarketLiveValidationPromotionProposalSnapshotsPayload | null;
  proposalTargetTier: string;
  reportDetail: PolymarketLiveValidationReportPayload | null;
  schemaValidation: PolymarketLiveValidationReportSchemaValidation | null;
  reports: PolymarketLiveValidationReportsPayload | null;
  onRefresh: () => void;
  onReportsRefresh: () => void;
  onStoreSnapshot: () => void;
}) {
  const gates = payload?.stage_gates;
  const authChecks = Object.entries(payload?.authenticated_read_checks ?? {});
  const publicChecks = Object.entries(payload?.public_checks ?? {});
  const commands = Object.entries(payload?.operator_commands ?? {});
  const reportEntries = reports?.entries ?? [];
  const comparison = reports?.comparison ?? null;
  const importSchemaValidation = schemaValidation ?? reports?.stored?.schema_validation ?? reportEntries[0]?.schema_validation ?? null;
  return (
    <section className="panel full">
      <div className="toolbar">
        <div className="panel-title">
          <Radio size={18} />
          <h2>Polymarket Live Validation</h2>
        </div>
        <div className="button-row compact">
          <button className="icon-button" onClick={onRefresh}>
            <RefreshCw size={17} /> Refresh
          </button>
          <button className="icon-button" onClick={onStoreSnapshot} disabled={busyKey === "store-current"}>
            <Save size={17} /> Store Snapshot
          </button>
        </div>
      </div>
      {payload ? (
        <>
          <div className="metrics-grid">
            <Metric label="Report" value={formatTime(payload.generated_at)} />
            <Metric label="Mode" value={payload.mode} />
            <Metric
              label="Credential readiness"
              value={gates?.credential_readiness ?? "unknown"}
              tone={validationTone(gates?.credential_readiness)}
            />
            <Metric
              label="Credentialed reads"
              value={gates?.credentialed_read_checks ?? "unknown"}
              tone={validationTone(gates?.credentialed_read_checks)}
            />
            <Metric
              label="Funded exposed"
              value={payload.funded_execution_exposed ? "yes" : "no"}
              tone={payload.funded_execution_exposed ? "warn" : "good"}
            />
            <Metric
              label="Funded gate"
              value={gates?.funded_live_order_check ?? "unknown"}
              tone={validationTone(gates?.funded_live_order_check)}
            />
          </div>
          {gates?.next_step ? <div className="info-banner warn">{gates.next_step}</div> : null}
          <div className="diagnostic-grid">
            <div>
              <span>Public probes</span>
              <strong>{gates?.public_live_checks ?? "unknown"}</strong>
            </div>
            <div>
              <span>Bridge checks</span>
              <strong>{gates?.bridge_address_checks ?? "unknown"}</strong>
            </div>
            <div>
              <span>Authenticated read OK</span>
              <strong>{gates?.credentialed_read_ok ? "yes" : "no"}</strong>
            </div>
            <div>
              <span>Safe funded attempt</span>
              <strong>{gates?.safe_to_attempt_funded_order ? "yes" : "no"}</strong>
            </div>
          </div>
          <div className="split-grid">
            <div className="audit-box">
              <h3>Authenticated Checks</h3>
              <div className="chip-row">
                {authChecks.map(([name, item]) => (
                  <span className={`chip ${item.status === "blocked" || item.status === "failed" ? "muted" : ""}`} key={name}>
                    {name}: {item.status}
                  </span>
                ))}
              </div>
            </div>
            <div className="audit-box">
              <h3>Public Checks</h3>
              <div className="chip-row">
                {publicChecks.map(([name, item]) => (
                  <span className={`chip ${item.status === "blocked" || item.status === "failed" ? "muted" : ""}`} key={name}>
                    {name}: {item.status}
                  </span>
                ))}
              </div>
            </div>
          </div>
          <div className="audit-box">
            <h3>CLI Audit Commands</h3>
            <div className="diagnostic-grid">
              {commands.map(([name, command]) => (
                <div key={name}>
                  <span>{name.replaceAll("_", " ")}</span>
                  <strong>{command}</strong>
                </div>
              ))}
            </div>
          </div>
          <div className="audit-box">
            <div className="toolbar">
              <div className="panel-title">
                <Database size={18} />
                <h2>Validation Reports</h2>
              </div>
              <button className="icon-button" onClick={onReportsRefresh}>
                <RefreshCw size={17} /> Refresh Reports
              </button>
            </div>
            <div className="diagnostic-grid cache-health">
              <div>
                <span>Store</span>
                <strong>{reports?.cache.path ?? "-"}</strong>
              </div>
              <div>
                <span>Reports</span>
                <strong>{reports?.counts.entries ?? 0}</strong>
              </div>
              <div>
                <span>Payload hashes</span>
                <strong>{reports?.counts.payload_hashes ?? 0}</strong>
              </div>
              <div>
                <span>Duplicate skips</span>
                <strong>{reports?.counts.duplicate_imports ?? 0}</strong>
              </div>
              <div>
                <span>Newest</span>
                <strong>{formatUnknownTime(reports?.cache.newest_stored_at)}</strong>
              </div>
              <div>
                <span>Size</span>
                <strong>{formatBytes(reports?.cache.size_bytes ?? 0)}</strong>
              </div>
            </div>
            {message ? (
              <div className={`info-banner ${importSchemaValidation && !importSchemaValidation.ok ? "warn" : ""}`}>{message}</div>
            ) : null}
            <LiveReportSchemaReference validation={importSchemaValidation} />
            <LiveReportSchemaDiagnostics title="Schema Diagnostics" validation={importSchemaValidation} />
            <div className="report-import-form">
              <label>
                <span>CLI JSON report import</span>
                <textarea
                  value={importText}
                  onChange={(event) => onImportTextChange(event.target.value)}
                  placeholder='{"stage_gates": {...}}'
                  rows={4}
                />
              </label>
              <label className="check-row">
                <input
                  type="checkbox"
                  checked={allowDuplicate}
                  onChange={(event) => onAllowDuplicateChange(event.target.checked)}
                />
                <span>Allow duplicate import</span>
              </label>
              <button className="icon-button" onClick={onImport} disabled={busyKey === "import-json"}>
                <Upload size={17} /> Import JSON
              </button>
            </div>
            <div className="toolbar compact ledger-toolbar">
              <div className="panel-title">
                <Database size={16} />
                <h3>Promotion Decision Ledger</h3>
              </div>
              <div className="button-row compact">
                <a className="icon-button compact" href={polymarketLiveValidationDecisionLedgerJsonUrl()} download>
                  <Download size={14} /> Ledger JSON
                </a>
                <a className="icon-button compact" href={polymarketLiveValidationDecisionLedgerMarkdownUrl()} download>
                  <Download size={14} /> Ledger Markdown
                </a>
              </div>
            </div>
            <PromotionProposalPreview
              busyKey={busyKey}
              proposal={proposal}
              proposalSnapshotDetail={proposalSnapshotDetail}
              proposalSnapshots={proposalSnapshots}
              targetTier={proposalTargetTier}
              onRefresh={onProposalRefresh}
              onSnapshotDelete={onProposalSnapshotDelete}
              onSnapshotOpen={onProposalSnapshotOpen}
              onSnapshotsRefresh={onProposalSnapshotsRefresh}
              onSnapshotStore={onProposalSnapshotStore}
              onTargetTierChange={onProposalTargetChange}
            />
            {comparison ? (
              <div className="audit-box">
                <h3>Latest vs Previous</h3>
                {comparison.changed ? (
                  <div className="comparison-list">
                    {comparison.changes.map((change) => (
                      <div key={change.field}>
                        <span>{change.field.replaceAll("_", " ")}</span>
                        <strong>
                          {formatAuditValue(change.previous)} {"->"} {formatAuditValue(change.latest)}
                        </strong>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="empty-state compact">No stage-gate changes between the latest two reports.</div>
                )}
              </div>
            ) : null}
            {reportDetail ? (
              <div className="audit-box">
                <h3>Opened Report</h3>
                <div className="diagnostic-grid">
                  <div>
                    <span>Key</span>
                    <strong>{reportDetail.entry.key ?? "-"}</strong>
                  </div>
                  <div>
                    <span>Source</span>
                    <strong>{reportDetail.entry.source}</strong>
                  </div>
                  <div>
                    <span>Generated</span>
                    <strong>{formatUnknownTime(reportDetail.entry.summary?.generated_at)}</strong>
                  </div>
                  <div>
                    <span>Payload</span>
                    <strong>{formatBytes(reportDetail.entry.payload_bytes ?? 0)}</strong>
                  </div>
                  <div>
                    <span>Payload hash</span>
                    <strong title={reportDetail.entry.payload_hash ?? ""}>{formatHash(reportDetail.entry.payload_hash)}</strong>
                  </div>
                  <div>
                    <span>Source file</span>
                    <strong>{reportDetail.entry.provenance?.source_file_name ?? "-"}</strong>
                  </div>
                  <div>
                    <span>Duplicate of</span>
                    <strong>{formatHash(reportDetail.entry.duplicate_of ?? null)}</strong>
                  </div>
                  <div>
                    <span>Credential readiness</span>
                    <strong>{reportDetail.entry.summary?.credential_readiness ?? "unknown"}</strong>
                  </div>
                  <div>
                    <span>Credentialed reads</span>
                    <strong>{reportDetail.entry.summary?.credentialed_read_checks ?? "unknown"}</strong>
                  </div>
                  <div>
                    <span>Bridge</span>
                    <strong>{reportDetail.entry.summary?.bridge_address_checks ?? "unknown"}</strong>
                  </div>
                  <div>
                    <span>Funded gate</span>
                    <strong>{reportDetail.entry.summary?.funded_live_order_check ?? "unknown"}</strong>
                  </div>
                  <div>
                    <span>Credential tier</span>
                    <strong>{reportDetail.entry.summary?.credential_live_verified ?? "blocked"}</strong>
                  </div>
                  <div>
                    <span>Funded tier</span>
                    <strong>{reportDetail.entry.summary?.funded_live_verified ?? "blocked"}</strong>
                  </div>
                </div>
                <LiveReportSchemaDiagnostics
                  title="Opened Report Schema Diagnostics"
                  validation={reportDetail.entry.schema_validation ?? null}
                />
                {reportDetail.entry.summary?.verification_promotion?.blocked_reasons?.length ? (
                  <div className="info-banner warn">
                    {reportDetail.entry.summary.verification_promotion.blocked_reasons.join(" ")}
                  </div>
                ) : null}
                <div className="button-row">
                  <a
                    className="icon-button"
                    href={reportDetail.entry.key ? polymarketLiveValidationReportExportUrl(reportDetail.entry.key) : "#"}
                    download={reportDetail.export.filename}
                  >
                    <Download size={17} /> Download JSON
                  </a>
                  <a
                    className="icon-button"
                    href={reportDetail.entry.key ? polymarketLiveValidationReportReviewJsonUrl(reportDetail.entry.key) : "#"}
                    download
                  >
                    <Download size={17} /> Review JSON
                  </a>
                  <a
                    className="icon-button"
                    href={reportDetail.entry.key ? polymarketLiveValidationReportReviewMarkdownUrl(reportDetail.entry.key) : "#"}
                    download
                  >
                    <Download size={17} /> Review Markdown
                  </a>
                </div>
                <form className="decision-form" onSubmit={onDecisionSubmit}>
                  <div className="panel-header compact">
                    <h3>Promotion Decision Ledger</h3>
                    <StatusPill tone="neutral">{decisions?.counts.entries ?? reportDetail.decisions?.length ?? 0} decisions</StatusPill>
                  </div>
                  <div className="decision-grid">
                    <label>
                      <span>Target tier</span>
                      <select
                        value={decisionForm.target_tier}
                        onChange={(event) => onDecisionChange({ ...decisionForm, target_tier: event.target.value })}
                      >
                        <option value="credential_live_verified">credential_live_verified</option>
                        <option value="funded_live_verified">funded_live_verified</option>
                        <option value="public_live_verified">public_live_verified</option>
                      </select>
                    </label>
                    <label>
                      <span>Decision</span>
                      <select
                        value={decisionForm.decision}
                        onChange={(event) =>
                          onDecisionChange({ ...decisionForm, decision: event.target.value as "accepted" | "rejected" })
                        }
                      >
                        <option value="rejected">rejected</option>
                        <option value="accepted">accepted</option>
                      </select>
                    </label>
                    <label>
                      <span>Reviewer</span>
                      <input
                        value={decisionForm.reviewer}
                        onChange={(event) => onDecisionChange({ ...decisionForm, reviewer: event.target.value })}
                      />
                    </label>
                    <label>
                      <span>Reviewer note</span>
                      <input
                        value={decisionForm.reviewer_note}
                        onChange={(event) => onDecisionChange({ ...decisionForm, reviewer_note: event.target.value })}
                        placeholder="Required decision rationale"
                      />
                    </label>
                  </div>
                  <div className="button-row">
                    <button className="icon-button" type="submit" disabled={busyKey === "decision-ledger"}>
                      <Save size={17} /> Record Decision
                    </button>
                    <a className="icon-button" href={polymarketLiveValidationDecisionLedgerJsonUrl()} download>
                      <Download size={17} /> Ledger JSON
                    </a>
                    <a className="icon-button" href={polymarketLiveValidationDecisionLedgerMarkdownUrl()} download>
                      <Download size={17} /> Ledger Markdown
                    </a>
                    <a className="icon-button" href={polymarketLiveValidationPromotionProposalJsonUrl()} download>
                      <Download size={17} /> Proposal JSON
                    </a>
                    <a className="icon-button" href={polymarketLiveValidationPromotionProposalMarkdownUrl()} download>
                      <Download size={17} /> Proposal Markdown
                    </a>
                  </div>
                  {(decisions?.entries ?? reportDetail.decisions ?? []).length ? (
                    <div className="decision-list">
                      {(decisions?.entries ?? reportDetail.decisions ?? []).slice(0, 5).map((item) => (
                        <div className="decision-row" key={item.key ?? `${item.target_tier}-${item.created_at}`}>
                          <strong>
                            {item.decision} {item.target_tier}
                          </strong>
                          <span>
                            {item.reviewer} | {formatUnknownTime(item.created_at)} | hash {formatHash(item.review_bundle_hash)}
                          </span>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="empty-state compact">No promotion decisions recorded for this report.</div>
                  )}
                </form>
              </div>
            ) : null}
            <div className="report-list">
              {reportEntries.length ? (
                reportEntries.map((entry) => (
                  <div className="report-row" key={entry.key ?? `${entry.source}-${entry.stored_at}`}>
                    <div>
                      <strong>{entry.label || entry.source}</strong>
                      <span>
                        {entry.source} | stored {formatUnknownTime(entry.stored_at)} | generated{" "}
                        {formatUnknownTime(entry.summary?.generated_at)}
                      </span>
                    </div>
                    <div className="chip-row">
                      <span className={`chip ${entry.summary?.credential_readiness === "passed" ? "" : "muted"}`}>
                        credentials: {entry.summary?.credential_readiness ?? "unknown"}
                      </span>
                      <span className={`chip ${entry.summary?.credentialed_read_checks === "passed" ? "" : "muted"}`}>
                        reads: {entry.summary?.credentialed_read_checks ?? "unknown"}
                      </span>
                      <span className={`chip ${entry.summary?.funded_live_order_check === "passed" ? "" : "muted"}`}>
                        funded: {entry.summary?.funded_live_order_check ?? "unknown"}
                      </span>
                      <span className={`chip ${entry.summary?.credential_live_verified === "yes" ? "" : "muted"}`}>
                        credential tier: {entry.summary?.credential_live_verified ?? "blocked"}
                      </span>
                      <span className={`chip ${entry.summary?.funded_live_verified === "yes" ? "" : "muted"}`}>
                        funded tier: {entry.summary?.funded_live_verified ?? "blocked"}
                      </span>
                      <span className={`chip ${schemaValidationTone(entry.schema_validation) === "good" ? "" : "muted"}`}>
                        {schemaValidationLabel(entry.schema_validation)}
                      </span>
                      <span className="chip muted" title={entry.payload_hash ?? ""}>
                        hash: {formatHash(entry.payload_hash)}
                      </span>
                      {entry.provenance?.source_file_name ? (
                        <span className="chip muted">source file: {entry.provenance.source_file_name}</span>
                      ) : null}
                      {entry.duplicate ? (
                        <span className="chip muted">
                          duplicate{entry.duplicate_import_count ? ` skips: ${entry.duplicate_import_count}` : ""}
                        </span>
                      ) : null}
                    </div>
                    <div className="row-actions">
                      <button
                        className="icon-button compact"
                        onClick={() => entry.key && onOpenReport(entry.key)}
                        disabled={!entry.key || busyKey === entry.key}
                      >
                        <Eye size={14} /> Open
                      </button>
                      {entry.key ? (
                        <a className="icon-button compact" href={polymarketLiveValidationReportExportUrl(entry.key)} download>
                          <Download size={14} /> JSON
                        </a>
                      ) : null}
                      {entry.key ? (
                        <a className="icon-button compact" href={polymarketLiveValidationReportReviewMarkdownUrl(entry.key)} download>
                          <Download size={14} /> Review
                        </a>
                      ) : null}
                      <button
                        className="icon-button compact danger"
                        onClick={() => entry.key && onDeleteReport(entry.key)}
                        disabled={!entry.key || busyKey === entry.key}
                      >
                        <Trash2 size={14} /> Delete
                      </button>
                    </div>
                  </div>
                ))
              ) : (
                <div className="empty-state compact">No stored validation reports yet.</div>
              )}
            </div>
          </div>
        </>
      ) : (
        <div className="empty-state">No live validation report loaded.</div>
      )}
    </section>
  );
}

function PromotionProposalPreview({
  busyKey,
  proposal,
  proposalSnapshotDetail,
  proposalSnapshots,
  targetTier,
  onRefresh,
  onSnapshotDelete,
  onSnapshotOpen,
  onSnapshotsRefresh,
  onSnapshotStore,
  onTargetTierChange
}: {
  busyKey: string | null;
  proposal: PolymarketLiveValidationPromotionProposalPayload | null;
  proposalSnapshotDetail: PolymarketLiveValidationPromotionProposalSnapshotPayload | null;
  proposalSnapshots: PolymarketLiveValidationPromotionProposalSnapshotsPayload | null;
  targetTier: string;
  onRefresh: () => void;
  onSnapshotDelete: (key: string) => void;
  onSnapshotOpen: (key: string) => void;
  onSnapshotsRefresh: () => void;
  onSnapshotStore: () => void;
  onTargetTierChange: (value: string) => void;
}) {
  const gates = proposal?.review_gates ?? [];
  const accepted = proposal?.accepted_decisions ?? [];
  const stale = proposal?.stale_decisions ?? [];
  const ignored = proposal?.ignored_decisions ?? [];
  const changes = proposal?.proposed_changes ?? [];
  return (
    <div className="proposal-preview audit-box">
      <div className="panel-header compact">
        <div>
          <h3>Promotion Proposal Preview</h3>
          <p>Read-only manual patch proposal from accepted ledger decisions.</p>
        </div>
        <div className="button-row compact">
          <select value={targetTier} onChange={(event) => onTargetTierChange(event.target.value)}>
            <option value="">all target tiers</option>
            <option value="credential_live_verified">credential_live_verified</option>
            <option value="funded_live_verified">funded_live_verified</option>
            <option value="public_live_verified">public_live_verified</option>
          </select>
          <button className="icon-button compact" onClick={onRefresh} disabled={busyKey === "promotion-proposal"}>
            <RefreshCw size={14} /> Refresh Proposal
          </button>
          <button
            className="icon-button compact"
            onClick={onSnapshotStore}
            disabled={busyKey === "promotion-proposal-snapshot-store"}
          >
            <Save size={14} /> Save Snapshot
          </button>
          <button
            className="icon-button compact"
            onClick={onSnapshotsRefresh}
            disabled={busyKey === "promotion-proposal-snapshots"}
          >
            <RefreshCw size={14} /> Refresh Archive
          </button>
          <a className="icon-button compact" href={polymarketLiveValidationPromotionProposalJsonUrl(targetTier)} download>
            <Download size={14} /> Proposal JSON
          </a>
          <a className="icon-button compact" href={polymarketLiveValidationPromotionProposalMarkdownUrl(targetTier)} download>
            <Download size={14} /> Proposal Markdown
          </a>
        </div>
      </div>
      <div className="info-banner warn">
        Manual review required. This preview has no apply action, keeps automerge disabled, and does not mutate static coverage.
      </div>
      <div className="metrics-grid proposal-metrics">
        <Metric label="Accepted" value={proposal?.counts.accepted_candidates ?? 0} tone={proposal?.counts.accepted_candidates ? "good" : "neutral"} />
        <Metric label="Stale" value={proposal?.counts.stale_decisions ?? 0} tone={proposal?.counts.stale_decisions ? "warn" : "good"} />
        <Metric label="Ignored" value={proposal?.counts.ignored_decisions ?? 0} />
        <Metric label="Changes" value={proposal?.counts.proposed_changes ?? 0} />
        <Metric label="Automerge" value={proposal?.automerge_enabled ? "enabled" : "disabled"} tone={proposal?.automerge_enabled ? "warn" : "good"} />
        <Metric label="Static mutation" value={proposal?.static_coverage_mutated ? "yes" : "no"} tone={proposal?.static_coverage_mutated ? "warn" : "good"} />
      </div>
      {proposal ? (
        <div className="proposal-sections">
          <div>
            <h4>Review Gates</h4>
            <div className="proposal-gates">
              {gates.length ? (
                gates.map((gate, index) => (
                  <div className="proposal-gate-row" key={`${proposalValue(gate, "gate")}-${index}`}>
                    <strong>{proposalValue(gate, "gate")}</strong>
                    <span>{proposalValue(gate, "status")}</span>
                    <p>{proposalValue(gate, "description")}</p>
                  </div>
                ))
              ) : (
                <div className="empty-state compact">No review gates returned.</div>
              )}
            </div>
          </div>
          <ProposalRows
            title="Accepted Candidates"
            rows={accepted}
            emptyText="No accepted decisions are ready for a proposal."
            columns={[
              ["target_tier", "Tier"],
              ["reviewer", "Reviewer"],
              ["current_review_bundle_hash", "Current hash"],
              ["proposal_effect", "Effect"]
            ]}
          />
          <ProposalRows
            title="Stale Decisions"
            rows={stale}
            emptyText="No stale decisions detected."
            columns={[
              ["target_tier", "Tier"],
              ["stale_reasons", "Reasons"],
              ["expected_review_bundle_hash", "Expected hash"],
              ["current_review_bundle_hash", "Current hash"]
            ]}
          />
          <ProposalRows
            title="Proposed Manual Changes"
            rows={changes}
            emptyText="No manual changes proposed."
            columns={[
              ["path", "File"],
              ["target_tier", "Tier"],
              ["action", "Action"],
              ["evidence", "Evidence"]
            ]}
          />
          {ignored.length ? (
            <ProposalRows
              title="Ignored Decisions"
              rows={ignored}
              emptyText="No ignored decisions."
              columns={[
                ["target_tier", "Tier"],
                ["decision", "Decision"],
                ["reason", "Reason"],
                ["report_key", "Report"]
              ]}
            />
          ) : null}
          <ProposalSnapshotArchive
            busyKey={busyKey}
            detail={proposalSnapshotDetail}
            snapshots={proposalSnapshots}
            onDelete={onSnapshotDelete}
            onOpen={onSnapshotOpen}
          />
        </div>
      ) : (
        <>
          <div className="empty-state compact">Refresh Proposal loads the current no-automerge preview.</div>
          <ProposalSnapshotArchive
            busyKey={busyKey}
            detail={proposalSnapshotDetail}
            snapshots={proposalSnapshots}
            onDelete={onSnapshotDelete}
            onOpen={onSnapshotOpen}
          />
        </>
      )}
    </div>
  );
}

function ProposalSnapshotArchive({
  busyKey,
  detail,
  snapshots,
  onDelete,
  onOpen
}: {
  busyKey: string | null;
  detail: PolymarketLiveValidationPromotionProposalSnapshotPayload | null;
  snapshots: PolymarketLiveValidationPromotionProposalSnapshotsPayload | null;
  onDelete: (key: string) => void;
  onOpen: (key: string) => void;
}) {
  const rows = snapshots?.entries ?? [];
  return (
    <div className="proposal-snapshot-archive">
      <div className="panel-header compact">
        <div>
          <h4>Proposal Snapshot Archive</h4>
          <p>Stored no-secrets proposal evidence for later review.</p>
        </div>
        <div className="chip-row">
          <span className="chip">snapshots: {snapshots?.counts.entries ?? 0}</span>
          <span className={`chip ${(snapshots?.counts.stale ?? 0) ? "" : "muted"}`}>stale: {snapshots?.counts.stale ?? 0}</span>
        </div>
      </div>
      <div className="proposal-table-wrap">
        <table className="proposal-table snapshot-table">
          <thead>
            <tr>
              <th>Stored</th>
              <th>Status</th>
              <th>Label</th>
              <th>Hash</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {rows.length ? (
              rows.slice(0, 8).map((row) => (
                <tr key={row.key ?? `${row.proposal_hash}-${row.stored_at}`}>
                  <td>{formatUnknownTime(row.stored_at)}</td>
                  <td>
                    <StatusPill tone={row.stale ? "warn" : "good"}>{row.snapshot_status}</StatusPill>
                  </td>
                  <td title={row.label}>{row.label || "-"}</td>
                  <td title={row.proposal_hash}>{formatHash(row.proposal_hash)}</td>
                  <td>
                    <div className="row-actions">
                      <button
                        className="icon-button compact"
                        onClick={() => row.key && onOpen(row.key)}
                        disabled={!row.key || busyKey === row.key}
                      >
                        <Eye size={14} /> Open
                      </button>
                      {row.key ? (
                        <a className="icon-button compact" href={polymarketLiveValidationPromotionProposalSnapshotJsonUrl(row.key)} download>
                          <Download size={14} /> JSON
                        </a>
                      ) : null}
                      {row.key ? (
                        <a className="icon-button compact" href={polymarketLiveValidationPromotionProposalSnapshotMarkdownUrl(row.key)} download>
                          <Download size={14} /> Markdown
                        </a>
                      ) : null}
                      <button
                        className="icon-button compact danger"
                        onClick={() => row.key && onDelete(row.key)}
                        disabled={!row.key || busyKey === row.key}
                      >
                        <Trash2 size={14} /> Delete
                      </button>
                    </div>
                  </td>
                </tr>
              ))
            ) : (
              <tr>
                <td colSpan={5} className="empty-cell">
                  No proposal snapshots stored yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {detail ? (
        <>
          <div className={`info-banner ${detail.entry.stale ? "warn" : ""}`}>
            Opened snapshot {formatHash(detail.entry.key)} is {detail.entry.snapshot_status}; reasons:{" "}
            {detail.entry.stale_reasons.length ? detail.entry.stale_reasons.join(", ") : "none"}.
          </div>
          <ProposalSnapshotDiffReview detail={detail} />
        </>
      ) : null}
    </div>
  );
}

function ProposalSnapshotDiffReview({ detail }: { detail: PolymarketLiveValidationPromotionProposalSnapshotPayload }) {
  const diff = detail.diff;
  const countRows = Object.entries(diff.counts).filter(([, value]) => value.delta !== 0);
  const acceptedChanges = diff.accepted_decisions.added.length + diff.accepted_decisions.removed.length;
  const staleChanges = diff.stale_decisions.added.length + diff.stale_decisions.removed.length;
  const snapshotKey = detail.entry.key ?? "";
  return (
    <div className="proposal-snapshot-diff audit-box">
      <div className="panel-header compact">
        <div>
          <h4>Current-vs-Snapshot Diff</h4>
          <p>Read-only change summary. It does not apply a coverage or documentation change.</p>
        </div>
        {snapshotKey ? (
          <div className="button-row compact">
            <a className="icon-button compact" href={polymarketLiveValidationPromotionProposalSnapshotDiffJsonUrl(snapshotKey)} download>
              <Download size={14} /> Diff JSON
            </a>
            <a className="icon-button compact" href={polymarketLiveValidationPromotionProposalSnapshotDiffMarkdownUrl(snapshotKey)} download>
              <Download size={14} /> Diff Markdown
            </a>
          </div>
        ) : null}
      </div>
      <div className="chip-row">
        <span className={`chip ${diff.changed ? "" : "muted"}`}>changed: {diff.changed ? "yes" : "no"}</span>
        <span className={`chip ${diff.proposal_hash.snapshot_integrity_valid ? "muted" : ""}`}>integrity: {diff.proposal_hash.snapshot_integrity_valid ? "valid" : "mismatch"}</span>
        <span className="chip">accepted changes: {acceptedChanges}</span>
        <span className="chip">stale changes: {staleChanges}</span>
        <span className="chip">file changes: {diff.proposed_files.added.length + diff.proposed_files.removed.length}</span>
        <span className="chip">gate changes: {diff.review_gates.added.length + diff.review_gates.removed.length + diff.review_gates.changed.length}</span>
      </div>
      <p className="muted-text">Categories: {diff.change_categories.length ? diff.change_categories.join(", ") : "none"}</p>
      <div className="proposal-table-wrap">
        <table className="proposal-table snapshot-table">
          <thead>
            <tr><th>Count</th><th>Snapshot</th><th>Current</th><th>Delta</th></tr>
          </thead>
          <tbody>
            {(countRows.length ? countRows : Object.entries(diff.counts)).map(([key, value]) => (
              <tr key={key}><td>{key}</td><td>{value.snapshot}</td><td>{value.current}</td><td>{value.delta > 0 ? `+${value.delta}` : value.delta}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="info-banner compact">
        Files added/removed: {diff.proposed_files.added.join(", ") || "none"} / {diff.proposed_files.removed.join(", ") || "none"}. Review-gate changes: {diff.review_gates.changed.length} changed, {diff.review_gates.added.length} added, {diff.review_gates.removed.length} removed.
      </div>
    </div>
  );
}

function ProposalRows({
  title,
  rows,
  columns,
  emptyText
}: {
  title: string;
  rows: Array<Record<string, unknown>>;
  columns: Array<[string, string]>;
  emptyText: string;
}) {
  return (
    <div>
      <h4>{title}</h4>
      <div className="proposal-table-wrap">
        <table className="proposal-table">
          <thead>
            <tr>
              {columns.map(([, label]) => (
                <th key={label}>{label}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.length ? (
              rows.slice(0, 8).map((row, index) => (
                <tr key={`${proposalValue(row, "decision_key")}-${proposalValue(row, "path")}-${index}`}>
                  {columns.map(([key]) => (
                    <td key={key} title={proposalValue(row, key)}>
                      {key.includes("hash") ? formatHash(proposalValue(row, key)) : proposalValue(row, key)}
                    </td>
                  ))}
                </tr>
              ))
            ) : (
              <tr>
                <td colSpan={columns.length} className="empty-cell">
                  {emptyText}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function LiveReportSchemaReference({
  validation
}: {
  validation: PolymarketLiveValidationReportSchemaValidation | null;
}) {
  const modes =
    validation?.accepted_modes.length
      ? validation.accepted_modes
      : ["strict_cli", "local_readiness_only", "credential_runbook_no_funded_actions", "browser_smoke", "browser_smoke_seed"];
  return (
    <div className="schema-reference">
      <div>
        <span>Accepted modes</span>
        <strong>{modes.join(", ")}</strong>
      </div>
      <div>
        <span>Live-stage required</span>
        <strong>mode + stage_gates object</strong>
      </div>
      <div>
        <span>Runbook required</span>
        <strong>env_inventory + readiness + funded_execution_exposed=false + network_calls=none</strong>
      </div>
    </div>
  );
}

function LiveReportSchemaDiagnostics({
  title,
  validation
}: {
  title: string;
  validation: PolymarketLiveValidationReportSchemaValidation | null;
}) {
  if (!validation) {
    return null;
  }
  const problems = validation.errors.length || validation.warnings.length;
  return (
    <div className="schema-diagnostics">
      <div className="panel-header compact">
        <h3>{title}</h3>
        <StatusPill tone={schemaValidationTone(validation)}>{schemaValidationLabel(validation)}</StatusPill>
      </div>
      <div className="diagnostic-grid compact">
        <div>
          <span>Schema version</span>
          <strong>{validation.schema_version}</strong>
        </div>
        <div>
          <span>Mode</span>
          <strong>{validation.mode ?? "missing"}</strong>
        </div>
        <div>
          <span>Report type</span>
          <strong>{validation.report_type ?? "unknown"}</strong>
        </div>
        <div>
          <span>Accepted modes</span>
          <strong>{validation.accepted_modes.join(", ")}</strong>
        </div>
      </div>
      {problems ? (
        <div className="schema-message-grid">
          {validation.errors.length ? (
            <div className="schema-message-list error-list">
              <span>Schema errors</span>
              <ul>
                {validation.errors.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </div>
          ) : null}
          {validation.warnings.length ? (
            <div className="schema-message-list warning-list">
              <span>Schema warnings</span>
              <ul>
                {validation.warnings.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </div>
          ) : null}
        </div>
      ) : (
        <div className="empty-state compact">No schema errors or warnings.</div>
      )}
    </div>
  );
}

function LivePreflightAudit({ payload }: { payload: LivePreflightPayload }) {
  const preflight = payload.preflight;
  return (
    <div className="audit-box">
      <div className="diagnostic-grid">
        <div>
          <span>Result</span>
          <strong>{payload.blocked ? "blocked" : "passed"}</strong>
        </div>
        <div>
          <span>Market</span>
          <strong>{payload.order.market_id}</strong>
        </div>
        <div>
          <span>Contract</span>
          <strong>{payload.order.contract_id}</strong>
        </div>
        <div>
          <span>Order</span>
          <strong>
            {payload.order.side} {formatNumber(payload.order.size, 4)} @ {formatNumber(payload.order.limit_price, 4)}
          </strong>
        </div>
        <div>
          <span>Notional</span>
          <strong>{formatNumber(payload.order.approx_notional, 4)}</strong>
        </div>
        <div>
          <span>Gate</span>
          <strong>{payload.live_safety.status}</strong>
        </div>
        <div>
          <span>Metadata keys</span>
          <strong>{payload.order.metadata_keys.length ? payload.order.metadata_keys.join(", ") : "-"}</strong>
        </div>
        <div>
          <span>Redaction</span>
          <strong>{payload.live_safety.redaction.audit_payloads_redacted ? "enabled" : "unknown"}</strong>
        </div>
      </div>
      {preflight ? (
        <div className="diagnostic-grid">
          <div>
            <span>Adapter</span>
            <strong>{formatAuditValue(preflight.display_name)}</strong>
          </div>
          <div>
            <span>Feature</span>
            <strong>{formatAuditValue(preflight.feature)}</strong>
          </div>
          <div>
            <span>Max size</span>
            <strong>{formatAuditValue(preflight.max_size)}</strong>
          </div>
          <div>
            <span>Max notional</span>
            <strong>{formatAuditValue(preflight.max_notional)}</strong>
          </div>
          <div>
            <span>Warnings</span>
            <strong>{formatAuditValue(preflight.warnings)}</strong>
          </div>
          <div>
            <span>Credentials</span>
            <strong>{formatAuditValue(preflight.requires_credentials)}</strong>
          </div>
          <div>
            <span>KYC</span>
            <strong>{formatAuditValue(preflight.requires_kyc)}</strong>
          </div>
          <div>
            <span>Region limited</span>
            <strong>{formatAuditValue(preflight.region_limited)}</strong>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function AlertsView({
  alerts,
  editingAlertId,
  form,
  markets,
  message,
  onAction,
  onFormChange,
  onSubmit
}: {
  alerts: AlertsPayload | null;
  editingAlertId: string | null;
  form: AlertForm;
  markets: MarketsPayload | null;
  message: string;
  onAction: (action: string, alert?: PriceAlert) => void;
  onFormChange: (form: AlertForm) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
}) {
  const alertMarkets = (markets?.markets ?? []).filter((market) => market.capabilities.alerts);
  const sourceOptions = alerts?.source_options ?? [
    { id: "last_trade" as const, label: "Last trade" },
    { id: "midpoint" as const, label: "Midpoint" },
    { id: "best_bid" as const, label: "Best bid" },
    { id: "best_ask" as const, label: "Best ask" }
  ];
  return (
    <section className="panel full">
      <form className="alert-form" onSubmit={onSubmit}>
        <label>
          <span>Market</span>
          <select value={form.market_id} onChange={(event) => onFormChange({ ...form, market_id: event.target.value })}>
            {alertMarkets.map((market) => (
              <option key={market.market_id} value={market.market_id}>
                {market.display_name}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Contract/token ID</span>
          <input
            value={form.contract_id}
            onChange={(event) => onFormChange({ ...form, contract_id: event.target.value })}
            placeholder="Contract or token id"
          />
        </label>
        <label>
          <span>Label</span>
          <input value={form.label} onChange={(event) => onFormChange({ ...form, label: event.target.value })} placeholder="Alert label" />
        </label>
        <label>
          <span>Direction</span>
          <select value={form.direction} onChange={(event) => onFormChange({ ...form, direction: event.target.value as AlertForm["direction"] })}>
            <option value="above">Above</option>
            <option value="below">Below</option>
          </select>
        </label>
        <label>
          <span>Source</span>
          <select value={form.source} onChange={(event) => onFormChange({ ...form, source: event.target.value as AlertForm["source"] })}>
            {sourceOptions.map((source) => (
              <option key={source.id} value={source.id}>
                {source.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Threshold</span>
          <input
            inputMode="decimal"
            value={form.threshold}
            onChange={(event) => onFormChange({ ...form, threshold: event.target.value })}
            placeholder="0.50"
          />
        </label>
        <label className="check-row">
          <input type="checkbox" checked={form.once} onChange={(event) => onFormChange({ ...form, once: event.target.checked })} />
          <span>Trigger once</span>
        </label>
        <label className="check-row">
          <input type="checkbox" checked={form.enabled} onChange={(event) => onFormChange({ ...form, enabled: event.target.checked })} />
          <span>Enabled</span>
        </label>
        <div className="button-row">
          <button className="icon-button" type="submit">
            <Save size={17} /> {editingAlertId ? "Save Alert" : "Add Alert"}
          </button>
          {editingAlertId ? (
            <button className="secondary-button" type="button" onClick={() => onAction("cancel-edit")}>
              <XCircle size={17} /> Cancel
            </button>
          ) : null}
          <button className="secondary-button" type="button" onClick={() => onAction("refresh-all")} disabled={!alerts?.alerts.length}>
            <RefreshCw size={17} /> Refresh Prices
          </button>
        </div>
      </form>
      {message ? <div className="info-banner">{message}</div> : null}
      <div className="metrics-grid four">
        <Metric label="Alerts" value={alerts?.counts.total ?? 0} />
        <Metric label="Enabled" value={alerts?.counts.enabled ?? 0} tone="good" />
        <Metric label="Triggered" value={alerts?.counts.triggered ?? 0} tone={alerts?.counts.triggered ? "warn" : undefined} />
        <Metric label="Alert markets" value={alertMarkets.length} />
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Status</th>
              <th>Alert</th>
              <th>Trigger</th>
              <th>Current Price State</th>
              <th>Created</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {(alerts?.alerts ?? []).map((alert) => (
              <tr key={alert.id}>
                <td>
                  <StatusPill tone={alert.status.tone}>{alert.status.label}</StatusPill>
                </td>
                <td>
                  <strong>{alert.label}</strong>
                  <small>
                    {alert.market_id}:{alert.contract_id}
                  </small>
                </td>
                <td>
                  <div className="stacked">
                    <span>
                      {alert.source.replace(/_/g, " ")} {alert.direction} {formatNumber(alert.threshold, 4)}
                    </span>
                    <small>{alert.once ? "once" : "repeat"} / {alert.enabled ? "enabled" : "disabled"}</small>
                  </div>
                </td>
                <td>
                  <div className="stacked">
                    <span>{formatNumber(alert.current_value, 4)}</span>
                    <small>
                      last {formatNumber(alert.values.last_trade, 4)} / mid {formatNumber(alert.values.midpoint, 4)} / bid{" "}
                      {formatNumber(alert.values.best_bid, 4)} / ask {formatNumber(alert.values.best_ask, 4)}
                    </small>
                  </div>
                </td>
                <td>{formatTime(alert.created_at)}</td>
                <td>
                  <div className="row-actions">
                    <button className="secondary-button" onClick={() => onAction("refresh", alert)} title="Refresh current price">
                      <RefreshCw size={16} />
                    </button>
                    <button className="secondary-button" onClick={() => onAction("edit", alert)} title="Edit alert">
                      <Edit3 size={16} />
                    </button>
                    <button className="secondary-button" onClick={() => onAction("toggle", alert)} title={alert.enabled ? "Disable alert" : "Enable alert"}>
                      <Power size={16} />
                    </button>
                    <button className="danger-button" onClick={() => onAction("delete", alert)} title="Delete alert">
                      <Trash2 size={16} />
                    </button>
                  </div>
                </td>
              </tr>
            ))}
            {!alerts?.alerts.length ? (
              <tr>
                <td colSpan={6}>
                  <span className="muted-text">No price alerts configured.</span>
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function WalletsCopyView({
  copy,
  copyForm,
  copyPreview,
  copyPreviewForm,
  editingWalletId,
  form,
  message,
  onCopyFormChange,
  onCopyPreview,
  onCopyPreviewFormChange,
  onCopySubmit,
  onPollingIntervalChange,
  onWalletAction,
  onWalletFormChange,
  onWalletSubmit,
  wallets
}: {
  copy: CopyPayload | null;
  copyForm: CopyForm;
  copyPreview: CopyTradePreview | null;
  copyPreviewForm: CopyPreviewForm;
  editingWalletId: string | null;
  form: WalletForm;
  message: string;
  onCopyFormChange: (form: CopyForm) => void;
  onCopyPreview: () => void;
  onCopyPreviewFormChange: (form: CopyPreviewForm) => void;
  onCopySubmit: (event: FormEvent<HTMLFormElement>) => void;
  onPollingIntervalChange: (value: number) => void;
  onWalletAction: (action: string, wallet?: WalletWatch) => void;
  onWalletFormChange: (form: WalletForm) => void;
  onWalletSubmit: (event: FormEvent<HTMLFormElement>) => void;
  wallets: WalletsPayload | null;
}) {
  const identityHint = copy?.activity_identity_hint ?? "0x wallet address";
  return (
    <div className="content-grid">
      <section className="panel span-2">
        <div className="panel-title">
          <Wallet size={18} />
          <h2>Wallet Tracking</h2>
        </div>
        <form className="wallet-form" onSubmit={onWalletSubmit}>
          <label>
            <span>Activity identity</span>
            <input value={form.wallet} onChange={(event) => onWalletFormChange({ ...form, wallet: event.target.value })} placeholder={identityHint} />
          </label>
          <label>
            <span>Name</span>
            <input
              value={form.display_name}
              onChange={(event) => onWalletFormChange({ ...form, display_name: event.target.value })}
              placeholder="Display name"
            />
          </label>
          <label>
            <span>Market slug filter</span>
            <input
              value={form.only_market_slug}
              onChange={(event) => onWalletFormChange({ ...form, only_market_slug: event.target.value })}
              placeholder="Optional"
            />
          </label>
          <label className="check-row">
            <input type="checkbox" checked={form.enabled} onChange={(event) => onWalletFormChange({ ...form, enabled: event.target.checked })} />
            <span>Enabled</span>
          </label>
          <div className="button-row">
            <button className="icon-button" type="submit">
              <Save size={17} /> {editingWalletId ? "Save Wallet" : "Add Wallet"}
            </button>
            {editingWalletId ? (
              <button className="secondary-button" type="button" onClick={() => onWalletAction("cancel-edit")}>
                <XCircle size={17} /> Cancel
              </button>
            ) : null}
          </div>
        </form>
        <div className="toolbar">
          <div className="metrics-grid two compact-metrics">
            <Metric label="Tracked" value={wallets?.counts.total ?? 0} />
            <Metric label="Enabled" value={wallets?.counts.enabled ?? 0} tone="good" />
          </div>
          <div className="button-row compact">
            <label className="inline-field">
              <span>Interval</span>
              <input
                inputMode="numeric"
                value={wallets?.polling.poll_interval_seconds ?? 10}
                onChange={(event) => onPollingIntervalChange(Number(event.target.value) || 10)}
              />
            </label>
            <button className="secondary-button" onClick={() => onWalletAction("save-polling")}>
              <Save size={16} /> Save
            </button>
            <button className="icon-button" onClick={() => onWalletAction("poll")} disabled={!wallets?.counts.enabled}>
              <Radio size={17} /> Poll Now
            </button>
          </div>
        </div>
        {message ? <div className="info-banner">{message}</div> : null}
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Status</th>
                <th>Wallet</th>
                <th>Last Seen</th>
                <th>Filter</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {(wallets?.wallets ?? []).map((wallet) => (
                <tr key={wallet.id}>
                  <td>
                    <StatusPill tone={wallet.enabled ? "good" : "neutral"}>{wallet.enabled ? "enabled" : "disabled"}</StatusPill>
                  </td>
                  <td>
                    <strong>{wallet.display_name || wallet.wallet}</strong>
                    <small>{wallet.wallet}</small>
                  </td>
                  <td>
                    <div className="stacked">
                      <span>{formatTime(wallet.last_seen_ts)}</span>
                      <small>{wallet.seen_count} seen</small>
                    </div>
                  </td>
                  <td>{wallet.only_market_slug || "-"}</td>
                  <td>
                    <div className="row-actions">
                      <button className="secondary-button" onClick={() => onWalletAction("edit", wallet)} title="Edit wallet">
                        <Edit3 size={16} />
                      </button>
                      <button className="secondary-button" onClick={() => onWalletAction("toggle", wallet)} title={wallet.enabled ? "Disable wallet" : "Enable wallet"}>
                        <Power size={16} />
                      </button>
                      <button className="danger-button" onClick={() => onWalletAction("delete", wallet)} title="Delete wallet">
                        <Trash2 size={16} />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
              {!wallets?.wallets.length ? (
                <tr>
                  <td colSpan={5}>
                    <span className="muted-text">No wallet watches configured.</span>
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </section>

      <section className="panel">
        <div className="panel-title">
          <Copy size={18} />
          <h2>Copy Settings</h2>
        </div>
        <form className="copy-form" onSubmit={onCopySubmit}>
          <label className="check-row">
            <input type="checkbox" checked={copyForm.enabled} onChange={(event) => onCopyFormChange({ ...copyForm, enabled: event.target.checked })} />
            <span>Enabled</span>
          </label>
          <label className="check-row">
            <input type="checkbox" checked={copyForm.live} onChange={(event) => onCopyFormChange({ ...copyForm, live: event.target.checked })} />
            <span>Live mode</span>
          </label>
          <label>
            <span>Follow identities</span>
            <input
              list="wallet-choices"
              value={copyForm.follow_wallets}
              onChange={(event) => onCopyFormChange({ ...copyForm, follow_wallets: event.target.value })}
              placeholder={`${identityHint}, ${identityHint}`}
            />
            <datalist id="wallet-choices">
              {(copy?.wallet_choices ?? []).map((wallet) => (
                <option key={wallet} value={wallet} />
              ))}
            </datalist>
          </label>
          <label>
            <span>Copy %</span>
            <input
              type="number"
              inputMode="decimal"
              min="0"
              max="100"
              step="0.01"
              value={copyForm.copy_percentage}
              onChange={(event) => onCopyFormChange({ ...copyForm, copy_percentage: event.target.value })}
            />
          </label>
          <label>
            <span>Max USDC</span>
            <input
              inputMode="decimal"
              value={copyForm.max_usdc_per_trade}
              onChange={(event) => onCopyFormChange({ ...copyForm, max_usdc_per_trade: event.target.value })}
            />
          </label>
          <label>
            <span>Slippage</span>
            <input inputMode="decimal" value={copyForm.slippage} onChange={(event) => onCopyFormChange({ ...copyForm, slippage: event.target.value })} />
          </label>
          <label className="check-row">
            <input
              type="checkbox"
              checked={copyForm.allow_sells}
              onChange={(event) => onCopyFormChange({ ...copyForm, allow_sells: event.target.checked })}
            />
            <span>Allow sells</span>
          </label>
          <label className="check-row">
            <input
              type="checkbox"
              checked={copyForm.conflict_guard}
              onChange={(event) => onCopyFormChange({ ...copyForm, conflict_guard: event.target.checked })}
            />
            <span>Conflict guard</span>
          </label>
          <button className="icon-button" type="submit">
            <Save size={17} /> Save Copy Settings
          </button>
        </form>
        <div className="metrics-grid two">
          <Metric label="Mode" value={copy?.status ?? "disabled"} tone={copy?.settings.enabled ? "good" : undefined} />
          <Metric label="Supported" value={copy?.copy_trading_supported ? "yes" : "no"} tone={copy?.copy_trading_supported ? "good" : "warn"} />
          <Metric label="Tracked follows" value={copy?.follow_wallets_tracked ?? 0} tone={copy?.follow_wallet_tracked ? "good" : undefined} />
          <Metric label="Kill switch" value={copy?.live_gate.live_trading_kill_switch ? "on" : "off"} tone={copy?.live_gate.live_trading_kill_switch ? "warn" : undefined} />
        </div>
      </section>

      <section className="panel">
        <div className="panel-title">
          <ShieldCheck size={18} />
          <h2>Live Preflight</h2>
        </div>
        <div className="copy-form">
          <label>
            <span>{copy?.market_id === "manifold" ? "Activity identity" : "Proxy wallet"}</span>
            <input value={copyPreviewForm.proxyWallet} onChange={(event) => onCopyPreviewFormChange({ ...copyPreviewForm, proxyWallet: event.target.value })} />
          </label>
          <label>
            <span>Token</span>
            <input value={copyPreviewForm.asset} onChange={(event) => onCopyPreviewFormChange({ ...copyPreviewForm, asset: event.target.value })} />
          </label>
          <label>
            <span>Side</span>
            <select value={copyPreviewForm.side} onChange={(event) => onCopyPreviewFormChange({ ...copyPreviewForm, side: event.target.value })}>
              <option value="BUY">BUY</option>
              <option value="SELL">SELL</option>
            </select>
          </label>
          <label>
            <span>Size</span>
            <input inputMode="decimal" value={copyPreviewForm.size} onChange={(event) => onCopyPreviewFormChange({ ...copyPreviewForm, size: event.target.value })} />
          </label>
          <label>
            <span>Price</span>
            <input inputMode="decimal" value={copyPreviewForm.price} onChange={(event) => onCopyPreviewFormChange({ ...copyPreviewForm, price: event.target.value })} />
          </label>
          <button className="icon-button" type="button" onClick={onCopyPreview}>
            <ShieldCheck size={17} /> Preview
          </button>
        </div>
        <div className="diagnostic-grid">
          <div>
            <span>Live enabled</span>
            <strong>{copy?.live_gate.live_trading_enabled ? "yes" : "no"}</strong>
          </div>
          <div>
            <span>Confirmed</span>
            <strong>{copy?.live_gate.live_trading_confirmed ? "yes" : "no"}</strong>
          </div>
          <div>
            <span>Max size</span>
            <strong>{formatNumber(copy?.live_gate.max_size, 4)}</strong>
          </div>
          <div>
            <span>Max notional</span>
            <strong>{formatNumber(copy?.live_gate.max_notional, 4)}</strong>
          </div>
        </div>
        {copyPreview ? (
          <div className={`info-banner ${copyPreview.blocked ? "warn" : ""}`}>
            <strong>{copyPreview.status}</strong>
            <span>{copyPreview.message ?? copyPreview.reason ?? ""}</span>
            {copyPreview.order ? (
              <small>
                {copyPreview.order.side} {formatNumber(copyPreview.order.size, 4)} @ {formatNumber(copyPreview.order.limit_price, 4)} notional{" "}
                {formatNumber(copyPreview.order.approx_notional, 4)}
              </small>
            ) : null}
          </div>
        ) : null}
      </section>

      <section className="panel span-2">
        <div className="panel-title">
          <Activity size={18} />
          <h2>Recent Activity</h2>
        </div>
        <ActivityTable activity={wallets?.recent_activity ?? []} />
      </section>
    </div>
  );
}

function ActivityTable({ activity }: { activity: WalletActivity[] }) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Time</th>
            <th>Wallet</th>
            <th>Trade</th>
            <th>Market</th>
            <th>Copy Preview</th>
          </tr>
        </thead>
        <tbody>
          {activity.map((item) => (
            <tr key={item.id}>
              <td>{formatTime(item.timestamp)}</td>
              <td>
                <strong>{item.display_name || item.wallet}</strong>
                <small>{item.transaction_hash || item.wallet}</small>
              </td>
              <td>
                <div className="stacked">
                  <span>
                    {item.side} {item.outcome || item.asset}
                  </span>
                  <small>
                    price {formatNumber(item.price, 4)} / size {formatNumber(item.size, 4)}
                  </small>
                </div>
              </td>
              <td>{item.slug || "-"}</td>
              <td>{item.copy_preview?.message ?? item.copy_preview?.reason ?? item.copy_preview?.status ?? "-"}</td>
            </tr>
          ))}
          {!activity.length ? (
            <tr>
              <td colSpan={5}>
                <span className="muted-text">No recent wallet activity in this API session.</span>
              </td>
            </tr>
          ) : null}
        </tbody>
      </table>
    </div>
  );
}

function PaperView({
  form,
  markets,
  message,
  onAction,
  onClearHistory,
  onFormChange,
  paper
}: {
  form: PaperOrderForm;
  markets: MarketsPayload | null;
  message: string;
  onAction: (action: string, target?: { market_id: string; contract_id: string; id?: string }) => void;
  onClearHistory: () => void;
  onFormChange: (form: PaperOrderForm) => void;
  paper: PaperPayload | null;
}) {
  const updateField = (field: keyof PaperOrderForm, value: string) => onFormChange({ ...form, [field]: value });
  return (
    <div className="content-grid">
      <section className="panel full">
        <div className="panel-title">
          <Activity size={18} />
          <h2>Order Form</h2>
        </div>
        <div className="paper-form">
          <label>
            <span>Market</span>
            <select value={form.market_id} onChange={(event) => updateField("market_id", event.target.value)}>
              {(markets?.markets ?? []).map((market) => (
                <option key={market.market_id} value={market.market_id}>
                  {market.display_name}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>Contract</span>
            <input value={form.contract_id} onChange={(event) => updateField("contract_id", event.target.value)} />
          </label>
          <label>
            <span>Side</span>
            <select value={form.side} onChange={(event) => updateField("side", event.target.value)}>
              <option value="BUY">BUY</option>
              <option value="SELL">SELL</option>
              <option value="BACK">BACK</option>
              <option value="LAY">LAY</option>
            </select>
          </label>
          <label>
            <span>Size</span>
            <input value={form.size} onChange={(event) => updateField("size", event.target.value)} />
          </label>
          <label>
            <span>Limit</span>
            <input value={form.limit_price} onChange={(event) => updateField("limit_price", event.target.value)} />
          </label>
        </div>
        <div className="button-row">
          <button className="icon-button" onClick={() => onAction("quote")}>
            <RefreshCw size={17} /> Refresh Quote
          </button>
          <button className="icon-button" onClick={() => onAction("quote-limit")}>
            <SlidersHorizontal size={17} /> Use Quote Limit
          </button>
          <button className="icon-button" onClick={() => onAction("impact")}>
            <BarChart3 size={17} /> Preview Impact
          </button>
          <button className="icon-button" onClick={() => onAction("live-preflight")}>
            <ShieldCheck size={17} /> Live Preflight
          </button>
          <button className="icon-button" onClick={() => onAction("submit")}>
            <Database size={17} /> Submit Paper Order
          </button>
        </div>
        {message ? <div className="info-banner">{message}</div> : null}
      </section>

      <section className="panel span-2">
        <div className="panel-title">
          <BarChart3 size={18} />
          <h2>Exposure</h2>
        </div>
        <div className="metrics-grid">
          <Metric label="Positions" value={paper?.summary.positions ?? 0} />
          <Metric label="Gross size" value={formatNumber(paper?.summary.gross_size, 4)} />
          <Metric label="Entry notional" value={formatNumber(paper?.summary.entry_notional, 4)} />
          <Metric label="Net notional" value={formatNumber(paper?.summary.net_notional, 4)} />
          <Metric label="Marked" value={`${paper?.summary.marked ?? 0}/${paper?.summary.positions ?? 0}`} />
          <Metric label="Unrealized" value={formatNumber(paper?.summary.unrealized, 4)} />
        </div>
      </section>

      <section className="panel full">
        <div className="panel-header">
          <div className="panel-title">
            <Activity size={18} />
            <h2>Open Positions</h2>
          </div>
          <div className="button-row compact">
            <button className="icon-button" onClick={() => onAction("refresh-marks")}>
              <RefreshCw size={17} /> Refresh Marks
            </button>
            <button className="icon-button" onClick={() => onAction("clear-marks")}>
              <Trash2 size={17} /> Clear Marks
            </button>
          </div>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Market</th>
                <th>Contract</th>
                <th className="numeric">Net size</th>
                <th className="numeric">Avg price</th>
                <th className="numeric">Notional</th>
                <th className="numeric">Mark</th>
                <th>Source</th>
                <th className="numeric">Unrealized</th>
                <th className="numeric">Trades</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {(paper?.positions ?? []).map((position) => (
                <tr key={`${position.market_id}:${position.contract_id}`}>
                  <td>{position.market_id}</td>
                  <td>{position.contract_id}</td>
                  <td className="numeric">{formatNumber(position.net_size)}</td>
                  <td className="numeric">{formatNumber(position.average_price)}</td>
                  <td className="numeric">{formatNumber(position.notional)}</td>
                  <td className="numeric">{formatNumber(position.mark_price)}</td>
                  <td>{position.mark_source || "-"}</td>
                  <td className="numeric">{formatNumber(position.unrealized)}</td>
                  <td className="numeric">{position.trades}</td>
                  <td>
                    <div className="row-actions">
                      <button onClick={() => onAction("use-position", position)}>Use</button>
                      <button onClick={() => onAction("refresh-selected-mark", position)}>Mark</button>
                      <button onClick={() => onAction("clear-selected-mark", position)}>Clear</button>
                    </div>
                  </td>
                </tr>
              ))}
              {!paper?.positions.length ? (
                <tr>
                  <td colSpan={10} className="empty-cell">
                    No open paper exposure.
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </section>

      <section className="panel full">
        <div className="panel-header">
          <div className="panel-title">
            <Database size={18} />
            <h2>History</h2>
          </div>
          <button className="danger-button" onClick={onClearHistory} disabled={!paper?.history.length}>
            <Trash2 size={17} /> Clear History
          </button>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Market</th>
                <th>Contract</th>
                <th>Side</th>
                <th className="numeric">Size</th>
                <th className="numeric">Limit</th>
                <th>Accepted</th>
                <th>Message</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {(paper?.history ?? []).map((trade) => (
                <tr key={trade.id}>
                  <td>{formatTime(trade.created_at)}</td>
                  <td>{trade.market_id}</td>
                  <td>{trade.contract_id}</td>
                  <td>{trade.side}</td>
                  <td className="numeric">{formatNumber(trade.size)}</td>
                  <td className="numeric">{formatNumber(trade.limit_price)}</td>
                  <td>
                    <StatusPill tone={trade.accepted ? "good" : "warn"}>{trade.accepted ? "yes" : "no"}</StatusPill>
                  </td>
                  <td>{trade.message}</td>
                  <td>
                    <div className="row-actions">
                      <button onClick={() => onAction("use-history", { ...trade, market_id: trade.market_id, contract_id: trade.contract_id })}>
                        Use
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
              {!paper?.history.length ? (
                <tr>
                  <td colSpan={9} className="empty-cell">
                    No paper-order history.
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}

function SettingsView({
  config,
  health,
  markets,
  onSelectedMarketChange,
  onThemeChange,
  onUiDesignChange
}: {
  config: ConfigPayload | null;
  health: HealthPayload | null;
  markets: MarketsPayload | null;
  onSelectedMarketChange: (marketId: string) => void;
  onThemeChange: (theme: Theme) => void;
  onUiDesignChange: (uiDesign: UiDesign) => void;
}) {
  return (
    <section className="panel full">
      <div className="settings-grid">
        <label>
          <span>Selected market</span>
          <select value={config?.selected_market_id ?? ""} onChange={(event) => onSelectedMarketChange(event.target.value)}>
            {(markets?.markets ?? []).map((market) => (
              <option key={market.market_id} value={market.market_id}>
                {market.display_name}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Theme</span>
          <select value={config?.theme ?? "light"} onChange={(event) => onThemeChange(event.target.value as Theme)}>
            <option value="light">Light</option>
            <option value="dark">Dark</option>
          </select>
        </label>
        <label>
          <span>Tkinter design</span>
          <select value={config?.ui_design ?? "aurora_2026"} onChange={(event) => onUiDesignChange(event.target.value as UiDesign)}>
            <option value="classic">Classic</option>
            <option value="aurora_2026">Aurora 2026</option>
            <option value="graphite_2026">Graphite 2026</option>
            <option value="sentinel_2027">Sentinel 2027</option>
          </select>
        </label>
        <label>
          <span>Config path</span>
          <input value={health?.config_path ?? ""} readOnly />
        </label>
        <label>
          <span>React build path</span>
          <input value={health?.frontend_dist ?? ""} readOnly />
        </label>
        <label>
          <span>React build available</span>
          <input value={health?.frontend_build_available ? "Yes" : "No"} readOnly />
        </label>
        <label>
          <span>Python GUI command</span>
          <input value={health?.python_gui_command ?? ""} readOnly />
        </label>
        <label>
          <span>Tkinter fallback</span>
          <input value={health?.tkinter_fallback ?? ""} readOnly />
        </label>
        <label>
          <span>React dev command</span>
          <input value={health?.react_dev_command ?? ""} readOnly />
        </label>
        <label>
          <span>React build command</span>
          <input value={health?.react_build_command ?? ""} readOnly />
        </label>
        <label>
          <span>React prod command</span>
          <input value={health?.react_prod_command ?? ""} readOnly />
        </label>
      </div>
    </section>
  );
}
