// Track 9 client module: sourcing visual-AI jobs (去白底 / 换场景 / 图片文字翻译)
// and read-only ad spend sync (Meta / Amazon / sandbox).
//
// Every request travels through `bffFetch` (`/api/bff/v1/...`), so the session
// cookie, the 401 refresh flow and the BFF role gate behave exactly like the
// rest of the workbench — both `/catalog` and `/finance` are already on the
// operator allowlist. The browser never recomputes a metric: ROAS, TACoS and CTR
// arrive as exact decimal strings and are rendered verbatim.
//
// Mock mode (`NEXT_PUBLIC_USE_MOCK_API=true`) never reaches the network. The
// fixtures below are mutable, so a mock render / connect / sync / delete really
// moves the state the panels read.
import { bffFetch } from "@/lib/api";
import { shouldUseMockApi } from "@/lib/data-source";
import type { UserRole } from "@/lib/contracts";

// ---------------------------------------------------------------------------
// Contract types (mirrors `apps/backend/src/erp_core/**/schemas` verbatim)
// ---------------------------------------------------------------------------

export type VisualProvider = "sandbox" | "remove_bg" | "openai_image";
export type VisualOperation = "remove_bg" | "scene_replace" | "translate_overlay";
export type VisualJobStatus = "queued" | "processing" | "completed" | "failed";

export interface VisualJobCreate {
  provider: VisualProvider;
  operation: VisualOperation;
  source_image_url: string;
  variant_id?: string | null;
  target_locale?: string | null;
  prompt?: string | null;
}

/** One persisted job. `output_image_url` is a presigned URL or a `data:image/png` preview. */
export interface VisualJobRead {
  id: string;
  tenant_id: string;
  variant_id: string | null;
  provider: VisualProvider;
  operation: VisualOperation;
  source_image_url: string;
  target_locale: string | null;
  prompt: string | null;
  status: VisualJobStatus;
  output_image_url: string | null;
  output_mime_type: string | null;
  output_r2_key: string | null;
  error_message: string | null;
  created_by: string | null;
  created_at: string;
  completed_at: string | null;
}

export type AdsPlatform = "sandbox_ads" | "meta_ads" | "amazon_ads";
export type AdsAccountStatus = "active" | "paused" | "error";

export interface AdAccountConnect {
  platform: AdsPlatform;
  account_name: string;
  external_account_id: string;
  access_token: string;
  currency: string;
}

/** Public account projection: never the token, only a masked fingerprint. */
export interface AdAccountRead {
  id: string;
  tenant_id: string;
  platform: AdsPlatform;
  account_name: string;
  external_account_id: string;
  currency: string;
  status: AdsAccountStatus;
  last_synced_at: string | null;
  created_at: string;
  has_credentials: boolean;
  token_preview: string;
}

export interface AdSpendRowRead {
  id: string;
  ad_account_id: string;
  spend_date: string;
  campaign_name: string;
  sku: string | null;
  impressions: number;
  clicks: number;
  ad_spend: string;
  attributed_sales: string;
  total_store_sales: string;
  currency: string;
}

export interface AdsSpendSummaryRead {
  tenant_id: string;
  days: number;
  currency: string;
  total_ad_spend: string;
  total_attributed_sales: string;
  total_store_sales: string;
  total_impressions: number;
  total_clicks: number;
  roas: string;
  tacos_pct: string;
  ctr_pct: string;
  rows: AdSpendRowRead[];
}

export interface AdsSyncResultRead {
  account: AdAccountRead;
  rows_upserted: number;
  days: number;
  synced_at: string;
}

// ---------------------------------------------------------------------------
// Selectable option tables
// ---------------------------------------------------------------------------

export interface SelectOption<T extends string> {
  value: T;
  label_zh: string;
  label_en: string;
}

export const VISUAL_PROVIDERS: readonly SelectOption<VisualProvider>[] = [
  { value: "sandbox", label_zh: "Sandbox 沙箱预览", label_en: "Sandbox preview" },
  { value: "remove_bg", label_zh: "remove.bg 去白底", label_en: "remove.bg cut-out" },
  { value: "openai_image", label_zh: "OpenAI 场景重绘", label_en: "OpenAI re-render" },
] as const;

export const VISUAL_OPERATIONS: readonly SelectOption<VisualOperation>[] = [
  { value: "remove_bg", label_zh: "去白底 remove_bg", label_en: "Remove background" },
  { value: "scene_replace", label_zh: "换场景 scene_replace", label_en: "Replace scene" },
  {
    value: "translate_overlay",
    label_zh: "图片文字翻译 translate_overlay",
    label_en: "Translate overlay",
  },
] as const;

export const ADS_PLATFORMS: readonly SelectOption<AdsPlatform>[] = [
  { value: "sandbox_ads", label_zh: "Sandbox 沙箱广告", label_en: "Sandbox ads" },
  { value: "meta_ads", label_zh: "Meta Ads", label_en: "Meta Ads" },
  { value: "amazon_ads", label_zh: "Amazon Ads", label_en: "Amazon Ads" },
] as const;

export const VISUAL_JOB_STATUS_LABELS: Record<VisualJobStatus, { zh: string; en: string }> = {
  queued: { zh: "排队中", en: "Queued" },
  processing: { zh: "处理中", en: "Processing" },
  completed: { zh: "已完成", en: "Completed" },
  failed: { zh: "失败", en: "Failed" },
};

/** Ads spend is a finance number: `AGENT` never sees it (matches `ADS_READ_ROLES`). */
export function canViewAdsSpend(role: UserRole | null | undefined): boolean {
  return role === "owner" || role === "admin" || role === "manager" || role === "finance";
}

/** Wiring credentials / triggering a pull needs `ADS_WRITE_ROLES`. */
export function canManageAdsAccounts(role: UserRole | null | undefined): boolean {
  return role === "owner" || role === "admin" || role === "manager";
}

/** Rendering product imagery is an operator action: `FINANCE` may only look. */
export function canWriteVisualJobs(role: UserRole | null | undefined): boolean {
  return role === "owner" || role === "admin" || role === "manager" || role === "agent";
}

// ---------------------------------------------------------------------------
// Render helpers — decimal strings only, never float arithmetic
// ---------------------------------------------------------------------------

/** `"2.50"` -> `"2.50x"` (ROAS is a multiple, not money). */
export function formatMultiple(value: string | null | undefined): string {
  if (value === null || value === undefined || value.trim() === "") return "0.00x";
  return `${value}x`;
}

/** `"4.00"` -> `"4.00%"`. */
export function formatPct(value: string | null | undefined): string {
  if (value === null || value === undefined || value.trim() === "") return "0.00%";
  return `${value}%`;
}

/** Money with the account currency prefix: `("120.50", "USD")` -> `"USD 120.50"`. */
export function formatAdsMoney(
  value: string | null | undefined,
  currency: string | null | undefined,
): string {
  const ccy = (currency ?? "USD").toUpperCase();
  const amount = value === null || value === undefined || value.trim() === "" ? "0.00" : value;
  return `${ccy} ${amount}`;
}

/** Thousands separator for integer counts, without touching any money value. */
export function formatCount(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "0";
  return String(Math.trunc(value)).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

export function providerLabel(provider: VisualProvider, locale: "zh" | "en"): string {
  return VISUAL_PROVIDERS.find((row) => row.value === provider)?.[`label_${locale}`] ?? provider;
}

export function operationLabel(operation: VisualOperation, locale: "zh" | "en"): string {
  return (
    VISUAL_OPERATIONS.find((row) => row.value === operation)?.[`label_${locale}`] ?? operation
  );
}

export function platformLabel(platform: AdsPlatform, locale: "zh" | "en"): string {
  return ADS_PLATFORMS.find((row) => row.value === platform)?.[`label_${locale}`] ?? platform;
}

// ---------------------------------------------------------------------------
// Defensive normalisation: a proxy returning a non-contract body must not crash
// ---------------------------------------------------------------------------

function asArray<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

function asText(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function asDecimalText(value: unknown, fallback = "0.00"): string {
  if (typeof value === "string" && value.length > 0) return value;
  if (typeof value === "number" && Number.isFinite(value)) return value.toFixed(2);
  return fallback;
}

function asNumber(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

export function normalizeVisualJob(raw: unknown): VisualJobRead {
  const job = (raw ?? {}) as Partial<VisualJobRead>;
  return {
    id: asText(job.id),
    tenant_id: asText(job.tenant_id),
    variant_id: job.variant_id ?? null,
    provider: (job.provider ?? "sandbox") as VisualProvider,
    operation: (job.operation ?? "remove_bg") as VisualOperation,
    source_image_url: asText(job.source_image_url),
    target_locale: job.target_locale ?? null,
    prompt: job.prompt ?? null,
    status: (job.status ?? "completed") as VisualJobStatus,
    output_image_url: job.output_image_url ?? null,
    output_mime_type: job.output_mime_type ?? null,
    output_r2_key: job.output_r2_key ?? null,
    error_message: job.error_message ?? null,
    created_by: job.created_by ?? null,
    created_at: asText(job.created_at),
    completed_at: job.completed_at ?? null,
  };
}

export function normalizeAdAccount(raw: unknown): AdAccountRead {
  const account = (raw ?? {}) as Partial<AdAccountRead>;
  return {
    id: asText(account.id),
    tenant_id: asText(account.tenant_id),
    platform: (account.platform ?? "sandbox_ads") as AdsPlatform,
    account_name: asText(account.account_name),
    external_account_id: asText(account.external_account_id),
    currency: asText(account.currency, "USD"),
    status: (account.status ?? "active") as AdsAccountStatus,
    last_synced_at: account.last_synced_at ?? null,
    created_at: asText(account.created_at),
    has_credentials: Boolean(account.has_credentials),
    token_preview: asText(account.token_preview, "\u2022\u2022\u2022\u2022"),
  };
}

export function normalizeSpendRow(raw: unknown): AdSpendRowRead {
  const row = (raw ?? {}) as Partial<AdSpendRowRead>;
  return {
    id: asText(row.id),
    ad_account_id: asText(row.ad_account_id),
    spend_date: asText(row.spend_date),
    campaign_name: asText(row.campaign_name),
    sku: row.sku ?? null,
    impressions: asNumber(row.impressions),
    clicks: asNumber(row.clicks),
    ad_spend: asDecimalText(row.ad_spend),
    attributed_sales: asDecimalText(row.attributed_sales),
    total_store_sales: asDecimalText(row.total_store_sales),
    currency: asText(row.currency, "USD"),
  };
}

export function emptySpendSummary(days = 30): AdsSpendSummaryRead {
  return {
    tenant_id: "",
    days,
    currency: "USD",
    total_ad_spend: "0.00",
    total_attributed_sales: "0.00",
    total_store_sales: "0.00",
    total_impressions: 0,
    total_clicks: 0,
    roas: "0.00",
    tacos_pct: "0.00",
    ctr_pct: "0.00",
    rows: [],
  };
}

export function normalizeSpendSummary(raw: unknown, days = 30): AdsSpendSummaryRead {
  const fallback = emptySpendSummary(days);
  const summary = (raw ?? {}) as Partial<AdsSpendSummaryRead>;
  return {
    ...fallback,
    tenant_id: asText(summary.tenant_id),
    days: asNumber(summary.days, days),
    currency: asText(summary.currency, "USD"),
    total_ad_spend: asDecimalText(summary.total_ad_spend),
    total_attributed_sales: asDecimalText(summary.total_attributed_sales),
    total_store_sales: asDecimalText(summary.total_store_sales),
    total_impressions: asNumber(summary.total_impressions),
    total_clicks: asNumber(summary.total_clicks),
    roas: asDecimalText(summary.roas),
    tacos_pct: asDecimalText(summary.tacos_pct),
    ctr_pct: asDecimalText(summary.ctr_pct),
    rows: asArray<AdSpendRowRead>(summary.rows).map(normalizeSpendRow),
  };
}

// ---------------------------------------------------------------------------
// Mock fixtures (mutable, so mock-mode mutations are visible)
// ---------------------------------------------------------------------------

/** 1x1-ish deterministic PNG preview used by the mock renderer. */
export const MOCK_PREVIEW_PNG =
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/AF+7kGvAAAAAElFTkSuQmCC";

const INITIAL_MOCK_VISUAL_JOBS: readonly VisualJobRead[] = [
  {
    id: "vj-mock-1",
    tenant_id: "t1",
    variant_id: null,
    provider: "sandbox",
    operation: "remove_bg",
    source_image_url: "https://cdn.example.com/products/led-panel-600.png",
    target_locale: null,
    prompt: "clean white background",
    status: "completed",
    output_image_url: MOCK_PREVIEW_PNG,
    output_mime_type: "image/png",
    output_r2_key: null,
    error_message: null,
    created_by: "u-owner",
    created_at: "2026-11-27T02:10:00Z",
    completed_at: "2026-11-27T02:10:01Z",
  },
];

const INITIAL_MOCK_ADS_ACCOUNTS: readonly AdAccountRead[] = [
  {
    id: "ad-mock-1",
    tenant_id: "t1",
    platform: "sandbox_ads",
    account_name: "QZ LED 沙箱广告账户",
    external_account_id: "act-sandbox-1",
    currency: "USD",
    status: "active",
    last_synced_at: "2026-11-27T01:00:00Z",
    created_at: "2026-11-20T01:00:00Z",
    has_credentials: true,
    token_preview: "\u2022\u2022\u2022\u20229f13",
  },
];

const MOCK_SPEND_ROWS: readonly AdSpendRowRead[] = [
  {
    id: "row-mock-1",
    ad_account_id: "ad-mock-1",
    spend_date: "2026-11-26",
    campaign_name: "QZ-LED Search 搜索广告",
    sku: "LED-PANEL-600",
    impressions: 1240,
    clicks: 65,
    ad_spend: "45.00",
    attributed_sales: "94.50",
    total_store_sales: "283.50",
    currency: "USD",
  },
  {
    id: "row-mock-2",
    ad_account_id: "ad-mock-1",
    spend_date: "2026-11-26",
    campaign_name: "QZ-LED Retarget 再营销",
    sku: "LED-TUBE-1200",
    impressions: 1450,
    clicks: 76,
    ad_spend: "54.00",
    attributed_sales: "118.80",
    total_store_sales: "0.00",
    currency: "USD",
  },
  {
    id: "row-mock-3",
    ad_account_id: "ad-mock-1",
    spend_date: "2026-11-25",
    campaign_name: "QZ-LED Search 搜索广告",
    sku: "LED-PANEL-600",
    impressions: 1203,
    clicks: 63,
    ad_spend: "46.37",
    attributed_sales: "98.30",
    total_store_sales: "294.90",
    currency: "USD",
  },
];

let mockVisualJobs: VisualJobRead[] = INITIAL_MOCK_VISUAL_JOBS.map((job) => ({ ...job }));
let mockAdsAccounts: AdAccountRead[] = INITIAL_MOCK_ADS_ACCOUNTS.map((account) => ({
  ...account,
}));
let mockVisualJobSeq = INITIAL_MOCK_VISUAL_JOBS.length + 1;
let mockAdsAccountSeq = INITIAL_MOCK_ADS_ACCOUNTS.length + 1;

/** Restore the fixtures to their documented starting state (tests call this in `beforeEach`). */
export function resetTrack9Mocks(): void {
  mockVisualJobs = INITIAL_MOCK_VISUAL_JOBS.map((job) => ({ ...job }));
  mockAdsAccounts = INITIAL_MOCK_ADS_ACCOUNTS.map((account) => ({ ...account }));
  mockVisualJobSeq = INITIAL_MOCK_VISUAL_JOBS.length + 1;
  mockAdsAccountSeq = INITIAL_MOCK_ADS_ACCOUNTS.length + 1;
}

// ---------------------------------------------------------------------------
// Live + mock dispatchers
// ---------------------------------------------------------------------------

export async function fetchVisualJobs(): Promise<VisualJobRead[]> {
  if (shouldUseMockApi()) return mockVisualJobs.map((job) => ({ ...job }));
  const body = await bffFetch<unknown>("/v1/catalog/visual-jobs");
  return asArray<VisualJobRead>(body).map(normalizeVisualJob);
}

export async function createVisualJob(payload: VisualJobCreate): Promise<VisualJobRead> {
  if (shouldUseMockApi()) {
    const job: VisualJobRead = {
      id: `vj-mock-${mockVisualJobSeq}`,
      tenant_id: "t1",
      variant_id: payload.variant_id ?? null,
      provider: payload.provider,
      operation: payload.operation,
      source_image_url: payload.source_image_url,
      target_locale: payload.target_locale ?? null,
      prompt: payload.prompt ?? null,
      // A sandbox render always succeeds offline; a vendor provider without a key
      // fails closed exactly like the backend, so the mock teaches the real UX.
      status: payload.provider === "sandbox" ? "completed" : "failed",
      output_image_url: payload.provider === "sandbox" ? MOCK_PREVIEW_PNG : null,
      output_mime_type: payload.provider === "sandbox" ? "image/png" : null,
      output_r2_key: null,
      error_message:
        payload.provider === "sandbox"
          ? null
          : `${
              payload.provider === "remove_bg" ? "REMOVE_BG_API_KEY" : "OPENAI_API_KEY"
            } is not configured in this environment.`,
      created_by: "u-owner",
      created_at: new Date().toISOString(),
      completed_at: new Date().toISOString(),
    };
    mockVisualJobSeq += 1;
    mockVisualJobs = [job, ...mockVisualJobs];
    return { ...job };
  }
  const body = await bffFetch<unknown>("/v1/catalog/visual-jobs", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  return normalizeVisualJob(body);
}

export async function deleteVisualJob(jobId: string): Promise<void> {
  if (shouldUseMockApi()) {
    mockVisualJobs = mockVisualJobs.filter((job) => job.id !== jobId);
    return;
  }
  await bffFetch<void>(`/v1/catalog/visual-jobs/${jobId}`, { method: "DELETE" });
}

export async function fetchAdAccounts(): Promise<AdAccountRead[]> {
  if (shouldUseMockApi()) return mockAdsAccounts.map((account) => ({ ...account }));
  const body = await bffFetch<unknown>("/v1/finance/ads/accounts");
  return asArray<AdAccountRead>(body).map(normalizeAdAccount);
}

export async function connectAdAccount(payload: AdAccountConnect): Promise<AdAccountRead> {
  if (shouldUseMockApi()) {
    const account: AdAccountRead = {
      id: `ad-mock-${mockAdsAccountSeq}`,
      tenant_id: "t1",
      platform: payload.platform,
      account_name: payload.account_name,
      external_account_id: payload.external_account_id,
      currency: payload.currency.toUpperCase(),
      status: "active",
      last_synced_at: null,
      created_at: new Date().toISOString(),
      has_credentials: payload.access_token.trim().length > 0,
      // The mock never stores or echoes the token either.
      token_preview: "\u2022\u2022\u2022\u2022mock",
    };
    mockAdsAccountSeq += 1;
    mockAdsAccounts = [...mockAdsAccounts, account];
    return { ...account };
  }
  const body = await bffFetch<unknown>("/v1/finance/ads/accounts", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  return normalizeAdAccount(body);
}

export async function deleteAdAccount(accountId: string): Promise<void> {
  if (shouldUseMockApi()) {
    mockAdsAccounts = mockAdsAccounts.filter((account) => account.id !== accountId);
    return;
  }
  await bffFetch<void>(`/v1/finance/ads/accounts/${accountId}`, { method: "DELETE" });
}

export async function syncAdAccount(
  accountId: string,
  days = 7,
): Promise<AdsSyncResultRead> {
  if (shouldUseMockApi()) {
    const account = mockAdsAccounts.find((row) => row.id === accountId);
    if (!account) throw new Error("Ad account not found");
    const syncedAt = new Date().toISOString();
    account.last_synced_at = syncedAt;
    return {
      account: { ...account },
      rows_upserted: MOCK_SPEND_ROWS.length,
      days,
      synced_at: syncedAt,
    };
  }
  const body = await bffFetch<unknown>(
    `/v1/finance/ads/accounts/${accountId}/sync?days=${days}`,
    { method: "POST" },
  );
  const result = (body ?? {}) as Partial<AdsSyncResultRead>;
  return {
    account: normalizeAdAccount(result.account),
    rows_upserted: asNumber(result.rows_upserted),
    days: asNumber(result.days, days),
    synced_at: asText(result.synced_at),
  };
}

export async function fetchAdsSpendSummary(days = 30): Promise<AdsSpendSummaryRead> {
  if (shouldUseMockApi()) {
    const rows = MOCK_SPEND_ROWS.map((row) => ({ ...row }));
    return {
      tenant_id: "t1",
      days,
      currency: "USD",
      total_ad_spend: "145.37",
      total_attributed_sales: "311.60",
      total_store_sales: "578.40",
      total_impressions: 3893,
      total_clicks: 204,
      roas: "2.14",
      tacos_pct: "25.13",
      ctr_pct: "5.24",
      rows,
    };
  }
  const body = await bffFetch<unknown>(`/v1/finance/ads/spend-summary?days=${days}`);
  return normalizeSpendSummary(body, days);
}
