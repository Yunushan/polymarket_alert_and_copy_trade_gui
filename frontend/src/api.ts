import type {
  AlertForm,
  AlertRefreshResponse,
  AlertsPayload,
  AppStatePayload,
  ConfigPayload,
  CopyForm,
  CopyPayload,
  CopyPreviewForm,
  CopyPreviewPayload,
  HealthPayload,
  LivePreflightPayload,
  LiveSafetyPayload,
  MarketsPayload,
  MarketSupportPayload,
  MarketAccountOperation,
  MarketAccountPayload,
  MarketPositionIntentPayload,
  MarketPositionOperation,
  MarketOrderManagementOperation,
  MarketOrderManagementPayload,
  MarketCandlesPayload,
  MarketContractsPayload,
  MarketEventsPayload,
  MarketOrderbookPayload,
  MarketPricePayload,
  MarketTradesPayload,
  PaperFormFillPayload,
  PaperImpactPayload,
  PaperOrderForm,
  PaperOrderResponse,
  PaperPayload,
  PaperQuotePayload,
  PolymarketLeaderboardFilters,
  PolymarketLeaderboardPayload,
  PolymarketLiveValidationDecisionLedgerPayload,
  PolymarketLiveValidationDecisionStoreRequest,
  PolymarketLiveValidationPromotionProposalPayload,
  PolymarketLiveValidationPromotionProposalSnapshotPayload,
  PolymarketLiveValidationPromotionProposalSnapshotStoreRequest,
  PolymarketLiveValidationPromotionProposalSnapshotsPayload,
  PolymarketLiveValidationReportPayload,
  PolymarketLiveValidationReportReviewPayload,
  PolymarketLiveValidationReportSchemaValidation,
  PolymarketLiveValidationReportStoreRequest,
  PolymarketLiveValidationReportsPayload,
  PolymarketLiveValidationPayload,
  PolymarketMddAuditExport,
  PolymarketMddCachePayload,
  PolymarketMddCachePurgeRequest,
  PolymarketMddForm,
  PolymarketMddPayload,
  PolymarketUserSearchPayload,
  WalletForm,
  WalletPollResponse,
  WalletsPayload
} from "./types";

export interface MarketPatch {
  enabled?: boolean;
  live_trading_enabled?: boolean;
  live_trading_confirmed?: boolean;
  live_trading_kill_switch?: boolean;
  live_trading_max_size?: string | number | null;
  live_trading_max_notional?: string | number | null;
  settings?: Record<string, unknown>;
}

type ApiErrorBody = {
  error?: string | {
    code?: string;
    message?: string;
    status?: number;
    details?: unknown;
  };
};

export class ApiRequestError extends Error {
  code?: string;
  status?: number;
  details?: unknown;

  constructor(message: string, code?: string, status?: number, details?: unknown) {
    super(message);
    this.name = "ApiRequestError";
    this.code = code;
    this.status = status;
    this.details = details;
  }
}

export function apiSchemaValidation(details: unknown): PolymarketLiveValidationReportSchemaValidation | null {
  if (!details || typeof details !== "object" || !("schema_validation" in details)) {
    return null;
  }
  const value = (details as { schema_validation?: unknown }).schema_validation;
  if (!value || typeof value !== "object") {
    return null;
  }
  const validation = value as Partial<PolymarketLiveValidationReportSchemaValidation>;
  if (typeof validation.ok !== "boolean") {
    return null;
  }
  return {
    schema_version: Number(validation.schema_version ?? 1),
    ok: validation.ok,
    mode: typeof validation.mode === "string" || validation.mode === null ? validation.mode : null,
    report_type: typeof validation.report_type === "string" || validation.report_type === null ? validation.report_type : null,
    errors: Array.isArray(validation.errors) ? validation.errors.map(String) : [],
    warnings: Array.isArray(validation.warnings) ? validation.warnings.map(String) : [],
    accepted_modes: Array.isArray(validation.accepted_modes) ? validation.accepted_modes.map(String) : []
  };
}

const vitePorts = new Set(["5173", "4173"]);
const defaultApiBase = vitePorts.has(window.location.port) ? "http://127.0.0.1:8765" : "";
const apiBase = (import.meta.env?.VITE_API_BASE_URL ?? defaultApiBase).replace(/\/$/, "");

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${apiBase}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers ?? {})
    }
  });
  const payload = (await response.json()) as T & ApiErrorBody;
  if (!response.ok) {
    const error = payload.error;
    const message = typeof error === "string" ? error : error?.message;
    const code = typeof error === "object" ? error?.code : undefined;
    const status = typeof error === "object" ? error?.status ?? response.status : response.status;
    const details = typeof error === "object" ? error?.details : undefined;
    throw new ApiRequestError(`${code ? `${code}: ` : ""}${message ?? `Request failed: ${response.status}`}`, code, status, details);
  }
  return payload;
}

const pendingIdempotencyKeys = new Map<string, string>();

function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== "object") {
    return JSON.stringify(value) ?? "null";
  }
  if (Array.isArray(value)) {
    return `[${value.map((item) => canonicalJson(item)).join(",")}]`;
  }
  const record = value as Record<string, unknown>;
  const members = Object.keys(record)
    .filter((key) => record[key] !== undefined)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`);
  return `{${members.join(",")}}`;
}

function newIdempotencyKey(): string {
  if (typeof globalThis.crypto?.randomUUID !== "function") {
    throw new Error("This browser cannot safely generate an idempotency key.");
  }
  return `market-sentinel-${globalThis.crypto.randomUUID()}`;
}

async function requestIdempotentMutation<T>(path: string, payload: object): Promise<T> {
  const requestIdentity = `${path}:${canonicalJson(payload)}`;
  let idempotencyKey = pendingIdempotencyKeys.get(requestIdentity);
  if (!idempotencyKey) {
    idempotencyKey = newIdempotencyKey();
    pendingIdempotencyKeys.set(requestIdentity, idempotencyKey);
  }
  try {
    const response = await request<T>(path, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify(payload)
    });
    pendingIdempotencyKeys.delete(requestIdentity);
    return response;
  } catch (error) {
    // A non-retryable 4xx response is terminal. Network/parse failures, 429,
    // and all 5xx responses are ambiguous, so retain the key for reconciliation.
    if (
      error instanceof ApiRequestError &&
      error.status !== undefined &&
      error.status < 500 &&
      error.status !== 429
    ) {
      pendingIdempotencyKeys.delete(requestIdentity);
    }
    throw error;
  }
}

export function fetchHealth(): Promise<HealthPayload> {
  return request<HealthPayload>("/api/health");
}

export function fetchState(): Promise<AppStatePayload> {
  return request<AppStatePayload>("/api/state");
}

export function fetchConfig(): Promise<ConfigPayload> {
  return request<ConfigPayload>("/api/config");
}

export function fetchMarkets(): Promise<MarketsPayload> {
  return request<MarketsPayload>("/api/markets");
}

function serializePaperOrderForm(form: PaperOrderForm): Record<string, unknown> {
  const { metadata_json, ...payload } = form;
  const trimmed = metadata_json.trim();
  if (!trimmed) {
    return payload;
  }
  let metadata: unknown;
  try {
    metadata = JSON.parse(trimmed);
  } catch (error) {
    throw new Error(`Order metadata must be valid JSON: ${error instanceof Error ? error.message : String(error)}`);
  }
  if (!metadata || typeof metadata !== "object" || Array.isArray(metadata)) {
    throw new Error("Order metadata must be a JSON object.");
  }
  return { ...payload, metadata };
}

export function fetchMarketSupport(marketId = ""): Promise<MarketSupportPayload> {
  const path = marketId.trim()
    ? `/api/markets/${encodeURIComponent(marketId.trim())}/support`
    : "/api/markets/support-matrix";
  return request<MarketSupportPayload>(path);
}

function marketReadQuery(values: Record<string, string | number | boolean | undefined>): string {
  const params = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => {
    if (value !== undefined && String(value).trim() !== "") {
      params.set(key, String(value));
    }
  });
  const query = params.toString();
  return query ? `?${query}` : "";
}

export function fetchMarketEvents(marketId: string, query = "", limit = 50): Promise<MarketEventsPayload> {
  return request<MarketEventsPayload>(
    `/api/markets/${encodeURIComponent(marketId)}/events${marketReadQuery({ query, limit })}`
  );
}

export function fetchMarketContracts(marketId: string, eventId: string): Promise<MarketContractsPayload> {
  return request<MarketContractsPayload>(
    `/api/markets/${encodeURIComponent(marketId)}/contracts${marketReadQuery({ event_id: eventId })}`
  );
}

export function fetchMarketPrice(marketId: string, contractId: string): Promise<MarketPricePayload> {
  return request<MarketPricePayload>(
    `/api/markets/${encodeURIComponent(marketId)}/price${marketReadQuery({ contract_id: contractId })}`
  );
}

export function fetchMarketOrderbook(marketId: string, contractId: string): Promise<MarketOrderbookPayload> {
  return request<MarketOrderbookPayload>(
    `/api/markets/${encodeURIComponent(marketId)}/orderbook${marketReadQuery({ contract_id: contractId })}`
  );
}

export function fetchMarketTrades(
  marketId: string,
  contractId: string,
  limit = 50,
  before = "",
  after = ""
): Promise<MarketTradesPayload> {
  return request<MarketTradesPayload>(
    `/api/markets/${encodeURIComponent(marketId)}/trades${marketReadQuery({ contract_id: contractId, limit, before, after })}`
  );
}

export function fetchMarketCandles(
  marketId: string,
  contractId: string,
  resolution = "1h",
  from = "",
  to = ""
): Promise<MarketCandlesPayload> {
  return request<MarketCandlesPayload>(
    `/api/markets/${encodeURIComponent(marketId)}/candles${marketReadQuery({ contract_id: contractId, resolution, from, to })}`
  );
}

export function fetchMarketAccount(
  marketId: string,
  operation: MarketAccountOperation,
  values: Record<string, string | number | boolean | undefined> = {}
): Promise<MarketAccountPayload> {
  return request<MarketAccountPayload>(
    `/api/markets/${encodeURIComponent(marketId)}/account/${encodeURIComponent(operation)}${marketReadQuery(values)}`
  );
}

export function requestMarketPositionIntent(
  marketId: string,
  operation: MarketPositionOperation,
  payload: Record<string, unknown>
): Promise<MarketPositionIntentPayload> {
  return request<MarketPositionIntentPayload>(
    `/api/markets/${encodeURIComponent(marketId)}/positions`,
    { method: "POST", body: JSON.stringify({ operation, ...payload }) }
  );
}

export function manageMarketOrders(
  marketId: string,
  operation: MarketOrderManagementOperation,
  payload: Record<string, unknown>
): Promise<MarketOrderManagementPayload> {
  return requestIdempotentMutation<MarketOrderManagementPayload>(
    `/api/markets/${encodeURIComponent(marketId)}/orders/${encodeURIComponent(operation)}`,
    payload
  );
}

export function fetchAlerts(): Promise<AlertsPayload> {
  return request<AlertsPayload>("/api/alerts");
}

export function fetchPaper(): Promise<PaperPayload> {
  return request<PaperPayload>("/api/paper");
}

export function fetchWallets(): Promise<WalletsPayload> {
  return request<WalletsPayload>("/api/wallets");
}

export function fetchCopy(): Promise<CopyPayload> {
  return request<CopyPayload>("/api/copy");
}

export function fetchLiveSafety(): Promise<LiveSafetyPayload> {
  return request<LiveSafetyPayload>("/api/live-safety");
}

export function fetchPolymarketLiveValidation(): Promise<PolymarketLiveValidationPayload> {
  return request<PolymarketLiveValidationPayload>("/api/polymarket/live-validation");
}

export function fetchPolymarketLiveValidationReports(includePayload = false): Promise<PolymarketLiveValidationReportsPayload> {
  const params = new URLSearchParams({ include_payload: String(includePayload) });
  return request<PolymarketLiveValidationReportsPayload>(`/api/polymarket/live-validation/reports?${params.toString()}`);
}

export function fetchPolymarketLiveValidationReport(key: string): Promise<PolymarketLiveValidationReportPayload> {
  return request<PolymarketLiveValidationReportPayload>(`/api/polymarket/live-validation/reports/${encodeURIComponent(key)}`);
}

export function fetchPolymarketLiveValidationReportReview(key: string): Promise<PolymarketLiveValidationReportReviewPayload> {
  return request<PolymarketLiveValidationReportReviewPayload>(
    `/api/polymarket/live-validation/reports/${encodeURIComponent(key)}/review.json`
  );
}

export function storePolymarketLiveValidationReport(
  payload: PolymarketLiveValidationReportStoreRequest
): Promise<PolymarketLiveValidationReportsPayload> {
  return requestIdempotentMutation<PolymarketLiveValidationReportsPayload>(
    "/api/polymarket/live-validation/reports",
    payload
  );
}

export function fetchPolymarketLiveValidationDecisions(reportKey = ""): Promise<PolymarketLiveValidationDecisionLedgerPayload> {
  const params = new URLSearchParams();
  if (reportKey) {
    params.set("report_key", reportKey);
  }
  const query = params.toString();
  return request<PolymarketLiveValidationDecisionLedgerPayload>(
    `/api/polymarket/live-validation/decisions${query ? `?${query}` : ""}`
  );
}

export function fetchPolymarketLiveValidationPromotionProposal(
  targetTier = ""
): Promise<PolymarketLiveValidationPromotionProposalPayload> {
  const params = new URLSearchParams();
  if (targetTier) {
    params.set("target_tier", targetTier);
  }
  const query = params.toString();
  return request<PolymarketLiveValidationPromotionProposalPayload>(
    `/api/polymarket/live-validation/promotion-proposal${query ? `?${query}` : ""}`
  );
}

export function fetchPolymarketLiveValidationPromotionProposalSnapshots(): Promise<PolymarketLiveValidationPromotionProposalSnapshotsPayload> {
  return request<PolymarketLiveValidationPromotionProposalSnapshotsPayload>(
    "/api/polymarket/live-validation/promotion-proposal/snapshots"
  );
}

export function fetchPolymarketLiveValidationPromotionProposalSnapshot(
  key: string
): Promise<PolymarketLiveValidationPromotionProposalSnapshotPayload> {
  return request<PolymarketLiveValidationPromotionProposalSnapshotPayload>(
    `/api/polymarket/live-validation/promotion-proposal/snapshots/${encodeURIComponent(key)}`
  );
}

export function storePolymarketLiveValidationPromotionProposalSnapshot(
  payload: PolymarketLiveValidationPromotionProposalSnapshotStoreRequest
): Promise<PolymarketLiveValidationPromotionProposalSnapshotsPayload> {
  return requestIdempotentMutation<PolymarketLiveValidationPromotionProposalSnapshotsPayload>(
    "/api/polymarket/live-validation/promotion-proposal/snapshots",
    payload
  );
}

export function storePolymarketLiveValidationDecision(
  payload: PolymarketLiveValidationDecisionStoreRequest
): Promise<PolymarketLiveValidationDecisionLedgerPayload> {
  return requestIdempotentMutation<PolymarketLiveValidationDecisionLedgerPayload>(
    "/api/polymarket/live-validation/decisions",
    payload
  );
}

export function deletePolymarketLiveValidationReport(key: string): Promise<PolymarketLiveValidationReportsPayload> {
  return request<PolymarketLiveValidationReportsPayload>(`/api/polymarket/live-validation/reports/${encodeURIComponent(key)}`, {
    method: "DELETE"
  });
}

export function deletePolymarketLiveValidationPromotionProposalSnapshot(
  key: string
): Promise<PolymarketLiveValidationPromotionProposalSnapshotsPayload> {
  return request<PolymarketLiveValidationPromotionProposalSnapshotsPayload>(
    `/api/polymarket/live-validation/promotion-proposal/snapshots/${encodeURIComponent(key)}`,
    {
      method: "DELETE"
    }
  );
}

export function polymarketLiveValidationReportExportUrl(key: string): string {
  return `${apiBase}/api/polymarket/live-validation/reports/${encodeURIComponent(key)}/export.json`;
}

export function polymarketLiveValidationReportReviewJsonUrl(key: string): string {
  return `${apiBase}/api/polymarket/live-validation/reports/${encodeURIComponent(key)}/review.json`;
}

export function polymarketLiveValidationReportReviewMarkdownUrl(key: string): string {
  return `${apiBase}/api/polymarket/live-validation/reports/${encodeURIComponent(key)}/review.md`;
}

export function polymarketLiveValidationDecisionLedgerJsonUrl(): string {
  return `${apiBase}/api/polymarket/live-validation/decisions/export.json`;
}

export function polymarketLiveValidationDecisionLedgerMarkdownUrl(): string {
  return `${apiBase}/api/polymarket/live-validation/decisions/export.md`;
}

export function polymarketLiveValidationPromotionProposalJsonUrl(targetTier = ""): string {
  const params = new URLSearchParams();
  if (targetTier) {
    params.set("target_tier", targetTier);
  }
  const query = params.toString();
  return `${apiBase}/api/polymarket/live-validation/promotion-proposal/export.json${query ? `?${query}` : ""}`;
}

export function polymarketLiveValidationPromotionProposalMarkdownUrl(targetTier = ""): string {
  const params = new URLSearchParams();
  if (targetTier) {
    params.set("target_tier", targetTier);
  }
  const query = params.toString();
  return `${apiBase}/api/polymarket/live-validation/promotion-proposal/export.md${query ? `?${query}` : ""}`;
}

export function polymarketLiveValidationPromotionProposalSnapshotJsonUrl(key: string): string {
  return `${apiBase}/api/polymarket/live-validation/promotion-proposal/snapshots/${encodeURIComponent(key)}/export.json`;
}

export function polymarketLiveValidationPromotionProposalSnapshotMarkdownUrl(key: string): string {
  return `${apiBase}/api/polymarket/live-validation/promotion-proposal/snapshots/${encodeURIComponent(key)}/export.md`;
}

export function polymarketLiveValidationPromotionProposalSnapshotDiffJsonUrl(key: string): string {
  return `${apiBase}/api/polymarket/live-validation/promotion-proposal/snapshots/${encodeURIComponent(key)}/diff.json`;
}

export function polymarketLiveValidationPromotionProposalSnapshotDiffMarkdownUrl(key: string): string {
  return `${apiBase}/api/polymarket/live-validation/promotion-proposal/snapshots/${encodeURIComponent(key)}/diff.md`;
}

export function updateMarket(marketId: string, patch: MarketPatch): Promise<MarketsPayload> {
  return request<MarketsPayload>(`/api/markets/${encodeURIComponent(marketId)}`, {
    method: "PATCH",
    body: JSON.stringify(patch)
  });
}

export function updateConfig(payload: Partial<Pick<ConfigPayload, "selected_market_id" | "theme" | "ui_design">>): Promise<ConfigPayload> {
  return request<ConfigPayload>("/api/config", {
    method: "PATCH",
    body: JSON.stringify(payload)
  });
}

export function createAlert(form: AlertForm): Promise<AlertsPayload> {
  return requestIdempotentMutation<AlertsPayload>("/api/alerts", form);
}

export function updateAlert(alertId: string, form: Partial<AlertForm>): Promise<AlertsPayload> {
  return request<AlertsPayload>(`/api/alerts/${encodeURIComponent(alertId)}`, {
    method: "PATCH",
    body: JSON.stringify(form)
  });
}

export function deleteAlert(alertId: string): Promise<AlertsPayload> {
  return request<AlertsPayload>(`/api/alerts/${encodeURIComponent(alertId)}`, {
    method: "DELETE"
  });
}

export function refreshAlerts(): Promise<AlertRefreshResponse> {
  return request<AlertRefreshResponse>("/api/alerts/refresh", {
    method: "POST",
    body: JSON.stringify({})
  });
}

export function refreshAlert(alertId: string): Promise<AlertRefreshResponse> {
  return request<AlertRefreshResponse>(`/api/alerts/${encodeURIComponent(alertId)}/refresh`, {
    method: "POST",
    body: JSON.stringify({})
  });
}

export function createWallet(form: WalletForm): Promise<WalletsPayload> {
  return requestIdempotentMutation<WalletsPayload>("/api/wallets", form);
}

export function updateWallet(walletId: string, form: Partial<WalletForm>): Promise<WalletsPayload> {
  return request<WalletsPayload>(`/api/wallets/${encodeURIComponent(walletId)}`, {
    method: "PATCH",
    body: JSON.stringify(form)
  });
}

export function deleteWallet(walletId: string): Promise<WalletsPayload> {
  return request<WalletsPayload>(`/api/wallets/${encodeURIComponent(walletId)}`, {
    method: "DELETE"
  });
}

export function updateWalletPolling(pollIntervalSeconds: string | number): Promise<WalletsPayload> {
  return request<WalletsPayload>("/api/wallets/polling", {
    method: "PATCH",
    body: JSON.stringify({ poll_interval_seconds: pollIntervalSeconds })
  });
}

export function pollWallets(limit = 25): Promise<WalletPollResponse> {
  return request<WalletPollResponse>("/api/wallets/poll", {
    method: "POST",
    body: JSON.stringify({ limit })
  });
}

export function searchPolymarketUsers(query: string, limit = 10): Promise<PolymarketUserSearchPayload> {
  const params = new URLSearchParams({ q: query, limit: String(limit) });
  return request<PolymarketUserSearchPayload>(`/api/polymarket/users/search?${params.toString()}`);
}

export function fetchPolymarketLeaderboard(filters: PolymarketLeaderboardFilters): Promise<PolymarketLeaderboardPayload> {
  const params = new URLSearchParams();
  Object.entries(filters).forEach(([key, value]) => {
    if (value !== "") {
      params.set(key, String(value));
    }
  });
  return request<PolymarketLeaderboardPayload>(`/api/polymarket/users/leaderboard?${params.toString()}`);
}

export function fetchPolymarketMdd(form: PolymarketMddForm): Promise<PolymarketMddPayload> {
  const params = new URLSearchParams();
  Object.entries(form).forEach(([key, value]) => {
    if (value !== "") {
      params.set(key, String(value));
    }
  });
  return request<PolymarketMddPayload>(`/api/polymarket/users/mdd?${params.toString()}`);
}

export function fetchPolymarketMddAudit(key: string): Promise<PolymarketMddAuditExport> {
  const params = new URLSearchParams({ key });
  return request<PolymarketMddAuditExport>(`/api/polymarket/users/mdd/export.json?${params.toString()}`);
}

export function fetchPolymarketMddCache(includeExpired = true): Promise<PolymarketMddCachePayload> {
  const params = new URLSearchParams({ include_expired: String(includeExpired) });
  return request<PolymarketMddCachePayload>(`/api/polymarket/users/mdd/cache?${params.toString()}`);
}

export function fetchPolymarketMddCacheHealth(): Promise<{ source: string; cache: PolymarketMddCachePayload["cache"] }> {
  return request<{ source: string; cache: PolymarketMddCachePayload["cache"] }>("/api/polymarket/users/mdd/cache/health");
}

export function purgePolymarketMddCache(payload: PolymarketMddCachePurgeRequest): Promise<PolymarketMddCachePayload> {
  return request<PolymarketMddCachePayload>("/api/polymarket/users/mdd/cache/purge", {
    method: "POST",
    body: JSON.stringify(payload)
  });
}

export function polymarketMddExportUrl(key: string, format: "json" | "csv"): string {
  const params = new URLSearchParams({ key });
  return `${apiBase}/api/polymarket/users/mdd/export.${format}?${params.toString()}`;
}

export function updateCopySettings(form: CopyForm): Promise<CopyPayload> {
  return request<CopyPayload>("/api/copy", {
    method: "PATCH",
    body: JSON.stringify(form)
  });
}

export function previewCopyTrade(form: CopyPreviewForm): Promise<CopyPreviewPayload> {
  return request<CopyPreviewPayload>("/api/copy/preview", {
    method: "POST",
    body: JSON.stringify(form)
  });
}

export function previewLivePreflight(form: PaperOrderForm): Promise<LivePreflightPayload> {
  return request<LivePreflightPayload>("/api/live-safety/preflight", {
    method: "POST",
    body: JSON.stringify(serializePaperOrderForm(form))
  });
}

export function clearPaperHistory(): Promise<PaperPayload> {
  return request<PaperPayload>("/api/paper/history/clear", {
    method: "POST",
    body: JSON.stringify({})
  });
}

export function refreshPaperQuote(form: PaperOrderForm): Promise<PaperQuotePayload> {
  return request<PaperQuotePayload>("/api/paper/quote", {
    method: "POST",
    body: JSON.stringify(serializePaperOrderForm(form))
  });
}

export function fillPaperQuoteLimit(form: PaperOrderForm): Promise<{ limit_price: number; message: string }> {
  return request<{ limit_price: number; message: string }>("/api/paper/quote-limit", {
    method: "POST",
    body: JSON.stringify(serializePaperOrderForm(form))
  });
}

export function previewPaperImpact(form: PaperOrderForm): Promise<PaperImpactPayload> {
  return request<PaperImpactPayload>("/api/paper/preview-impact", {
    method: "POST",
    body: JSON.stringify(serializePaperOrderForm(form))
  });
}

export function submitPaperOrder(form: PaperOrderForm): Promise<PaperOrderResponse> {
  return requestIdempotentMutation<PaperOrderResponse>(
    "/api/paper/orders",
    serializePaperOrderForm(form)
  );
}

export function usePaperHistory(recordId: string): Promise<PaperFormFillPayload> {
  return request<PaperFormFillPayload>("/api/paper/history/use", {
    method: "POST",
    body: JSON.stringify({ record_id: recordId })
  });
}

export function usePaperPosition(marketId: string, contractId: string): Promise<PaperFormFillPayload> {
  return request<PaperFormFillPayload>("/api/paper/positions/use", {
    method: "POST",
    body: JSON.stringify({ market_id: marketId, contract_id: contractId })
  });
}

export function refreshPaperMarks(): Promise<{ paper: PaperPayload; message: string; problems: string[] }> {
  return request<{ paper: PaperPayload; message: string; problems: string[] }>("/api/paper/marks/refresh", {
    method: "POST",
    body: JSON.stringify({})
  });
}

export function refreshSelectedPaperMark(marketId: string, contractId: string): Promise<{ paper: PaperPayload; message: string }> {
  return request<{ paper: PaperPayload; message: string }>("/api/paper/marks/refresh-selected", {
    method: "POST",
    body: JSON.stringify({ market_id: marketId, contract_id: contractId })
  });
}

export function clearPaperMarks(): Promise<{ paper: PaperPayload; message: string }> {
  return request<{ paper: PaperPayload; message: string }>("/api/paper/marks/clear", {
    method: "POST",
    body: JSON.stringify({})
  });
}

export function clearSelectedPaperMark(marketId: string, contractId: string): Promise<{ paper: PaperPayload; message: string }> {
  return request<{ paper: PaperPayload; message: string }>("/api/paper/marks/clear-selected", {
    method: "POST",
    body: JSON.stringify({ market_id: marketId, contract_id: contractId })
  });
}
