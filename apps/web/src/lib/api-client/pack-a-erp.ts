// Pack A live adapters: catalog authoring (single create + CSV bulk import),
// export documents (Commercial Invoice / Packing List PDFs) and WMS
// replenishment (safety-stock alerts, one-click PO, warehouse onboarding).
//
// Every request goes through `bffFetch` from `@/lib/api`, so the browser only
// ever talks to the same-origin `/api/bff/*` shell (session cookie + 401 refresh
// handled once). Zero SQL / ORM: the typed contract lives in `types.gen.ts`.
//
// Live-client purity: `live-client.ts`, `data-source.ts`, `sdk.gen.ts` and
// `types.gen.ts` stay untouched — Pack A owns this file alone.
import { bffFetch } from "@/lib/api";
import type {
  CatalogCsvImportRequest,
  CatalogCsvImportResponse,
  ChannelAnalyticsOverviewRead,
  ChannelDailyMetricRead,
  ChannelDailyMetricUpsert,
  InventorySyncDispatchSummary,
  InventorySyncJobRead,
  LowStockCreatePoRequest,
  LowStockItemRead,
  LowStockListRead,
  PaginatedResponseInventorySyncJobRead,
  ProductCreate,
  ProductRead,
  PurchaseOrderRead,
  SkuVelocityResponse,
  VariantCreate,
  VariantRead,
  WarehouseCreateRequest,
  WmsWarehouseRead,
} from "@/lib/api-client/types.gen";
import { shouldUseMockApi } from "@/lib/data-source";

// ---------------------------------------------------------------------------
// Payload construction (pure, unit-testable)
// ---------------------------------------------------------------------------

/**
 * Everything the `+ 新建商品 / SKU` drawer collects.
 *
 * Money and packing decimals stay **strings** end-to-end: the API accepts
 * `number | string` for `retail_price_usd` / `cost_price_base`, and shipping a
 * decimal string avoids any IEEE-754 rounding on the way to `NUMERIC` columns.
 */
export interface CatalogSkuDraft {
  spu_code: string;
  product_name: string;
  category: string;
  hs_code?: string | null;
  sku: string;
  retail_price_usd: string;
  /** Optional: leave empty to let the backend keep a zero base cost. */
  cost_price_base?: string | null;
  lead_time_days: number;
  /** Units per export carton — drives the per-unit gram weight. */
  carton_qty: number;
  gross_weight_kg: string;
  net_weight_kg: string;
  cbm_per_carton: string;
  safety_stock_qty: number;
}

const DECIMAL_PATTERN = /^\d+(\.\d+)?$/;

/** Canonical decimal string, or `null` when the cell is blank / not a decimal. */
function decimalString(raw: string | null | undefined): string | null {
  const trimmed = (raw ?? "").trim();
  if (!trimmed) return null;
  if (!DECIMAL_PATTERN.test(trimmed)) return null;
  return trimmed;
}

/** Exact `kg -> grams` conversion, tolerating operands such as `"12.500"`. */
function kilogramsToGrams(kg: string): number {
  const trimmed = kg.trim();
  if (!DECIMAL_PATTERN.test(trimmed)) return 0;
  const [whole, fraction = ""] = trimmed.split(".");
  const padded = `${fraction}000`.slice(0, 3);
  return Number(whole) * 1000 + Number(padded);
}

/** Per-unit gram weight from the carton net weight, mirroring the CSV service. */
export function weightGramsFromPacking(draft: {
  carton_qty: number;
  net_weight_kg: string;
}): number {
  const cartonQty = Number.isFinite(draft.carton_qty) && draft.carton_qty > 0
    ? Math.trunc(draft.carton_qty)
    : 1;
  const netGrams = kilogramsToGrams(draft.net_weight_kg);
  if (netGrams <= 0) return 0;
  return Math.round(netGrams / cartonQty);
}

/**
 * Export-packing attributes (`carton_qty`, weights per carton, CBM, safety
 * stock). The CSV import service stores exactly these keys on the variant, so a
 * drawer-created SKU is indistinguishable from a CSV-imported one.
 */
export function packingAttributesFromDraft(draft: CatalogSkuDraft): Record<string, string | number> {
  const cartonQty = Number.isFinite(draft.carton_qty) && draft.carton_qty > 0
    ? Math.trunc(draft.carton_qty)
    : 1;
  const safetyStock = Number.isFinite(draft.safety_stock_qty) && draft.safety_stock_qty > 0
    ? Math.trunc(draft.safety_stock_qty)
    : 0;
  return {
    carton_qty: cartonQty,
    gross_weight_kg: decimalString(draft.gross_weight_kg) ?? "0",
    net_weight_kg: decimalString(draft.net_weight_kg) ?? "0",
    cbm_per_carton: decimalString(draft.cbm_per_carton) ?? "0",
    safety_stock_qty: safetyStock,
  };
}

/** `ProductCreate` body. The product name rides in `i18n_specs.en.title`. */
export function productPayloadFromDraft(draft: CatalogSkuDraft): ProductCreate {
  const payload: ProductCreate = {
    spu_code: draft.spu_code.trim(),
    category: draft.category.trim(),
    // `i18n_specs.en.title` is the column the CI/PL renderer and the low-stock
    // board read the human product name from.
    i18n_specs: { en: { title: draft.product_name.trim() } },
    origin_country: "CN",
    certifications: [],
    ai_faq_facts: {},
  };
  const hsCode = (draft.hs_code ?? "").trim();
  if (hsCode) payload.hs_code = hsCode;
  return payload;
}

/** `VariantCreate` body without `product_id` (filled in after the SPU lands). */
export function variantPayloadFromDraft(
  draft: CatalogSkuDraft
): Omit<VariantCreate, "product_id"> {
  const cost = decimalString(draft.cost_price_base ?? null);
  return {
    sku: draft.sku.trim(),
    attributes: packingAttributesFromDraft(draft),
    cost_price_base: cost ?? "0",
    base_currency: "CNY",
    retail_price_usd: decimalString(draft.retail_price_usd) ?? "0",
    b2b_moq_tiers: [],
    weight_grams: weightGramsFromPacking(draft),
    carton_dims_cm: null,
    lead_time_days: Number.isFinite(draft.lead_time_days) ? Math.trunc(draft.lead_time_days) : 7,
  };
}

// ---------------------------------------------------------------------------
// Catalog: create product + variant
// ---------------------------------------------------------------------------

export interface CreatedCatalogSku {
  product: ProductRead;
  variant: VariantRead;
}

/**
 * Two-step authoring: POST the SPU, then POST the SKU against the new
 * `product_id`. The backend rejects a variant whose product is unknown, so the
 * calls are strictly sequential (never `Promise.all`).
 */
export async function createCatalogProductAndVariant(
  productPayload: ProductCreate,
  variantPayload: Omit<VariantCreate, "product_id">
): Promise<CreatedCatalogSku> {
  if (shouldUseMockApi()) {
    return mockCreateCatalogProductAndVariant(productPayload, variantPayload);
  }
  const product = await bffFetch<ProductRead>("/v1/catalog/products", {
    method: "POST",
    body: JSON.stringify(productPayload),
  });
  const variant = await bffFetch<VariantRead>("/v1/catalog/variants", {
    method: "POST",
    body: JSON.stringify({ ...variantPayload, product_id: product.id }),
  });
  return { product, variant };
}

// ---------------------------------------------------------------------------
// Catalog: bulk CSV import
// ---------------------------------------------------------------------------

/** POST `/v1/catalog/variants/import-csv` — case-insensitive header bulk upsert. */
export function importCatalogCsv(csvText: string): Promise<CatalogCsvImportResponse> {
  if (shouldUseMockApi()) {
    return Promise.resolve(mockImportCatalogCsv(csvText));
  }
  const body: CatalogCsvImportRequest = { csv_text: csvText };
  return bffFetch<CatalogCsvImportResponse>("/v1/catalog/variants/import-csv", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** Mandatory CSV headers, mirroring `_CSV_REQUIRED_COLUMNS` on the backend. */
export const CATALOG_CSV_REQUIRED_COLUMNS = [
  "spu_code",
  "product_name",
  "sku",
  "retail_price_usd",
] as const;

/**
 * One-click sample sheet. Documented headers, export-grade packing columns and
 * a real LED strip fixture so the operator can eyeball the shape before upload.
 */
export const CATALOG_CSV_SAMPLE = [
  "spu_code,product_name,category,hs_code,sku,retail_price_usd,cost_price_base,lead_time_days,carton_qty,gross_weight_kg,net_weight_kg,cbm_per_carton,safety_stock_qty,moq_100_price,moq_500_price",
  "LED-COB-24V,COB LED Strip 24V 5m 4000K,LED Strip Lighting,94054200,LED-COB-24V-5M-4000K,29.90,45.50,7,50,11.500,10.000,0.032,120,18.50,14.20",
  "LED-COB-24V,COB LED Strip 24V 10m 6000K,LED Strip Lighting,94054200,LED-COB-24V-10M-6000K,54.90,88.00,10,25,13.800,12.000,0.041,60,34.00,27.50",
  "LED-PANEL-60,LED Panel Light 60x60 40W 5000K,LED Panel Lighting,94051100,LED-PANEL-60-40W-5000K,49.90,68.00,12,10,18.400,16.500,0.068,80,32.00,26.50",
].join("\n");

// ---------------------------------------------------------------------------
// Orders: export document links (rendered server-side, opened in a new tab)
// ---------------------------------------------------------------------------

export type ExportDocumentLanguage = "en" | "zh";

/** Same-origin BFF link to the live-rendered Commercial Invoice PDF. */
export function commercialInvoicePdfUrl(
  orderId: string,
  language: ExportDocumentLanguage = "en"
): string {
  return `/api/bff/v1/orders/${encodeURIComponent(orderId)}/documents/commercial-invoice.pdf?language=${language}`;
}

/** Same-origin BFF link to the live-rendered Packing List PDF. */
export function packingListPdfUrl(
  orderId: string,
  language: ExportDocumentLanguage = "en"
): string {
  return `/api/bff/v1/orders/${encodeURIComponent(orderId)}/documents/packing-list.pdf?language=${language}`;
}

// ---------------------------------------------------------------------------
// WMS: safety-stock alerts, one-click replenishment, warehouse onboarding
// ---------------------------------------------------------------------------

/** GET `/v1/wms/low-stock` — every SKU/warehouse pair under its safety stock. */
export function fetchLowStockAlerts(): Promise<LowStockListRead> {
  if (shouldUseMockApi()) {
    return Promise.resolve({ items: mockLowStock.map((item) => ({ ...item })), total: mockLowStock.length });
  }
  return bffFetch<LowStockListRead>("/v1/wms/low-stock");
}

/** POST `/v1/wms/low-stock/create-po` — single-line draft purchase order. */
export function createPoFromLowStock(payload: LowStockCreatePoRequest): Promise<PurchaseOrderRead> {
  if (shouldUseMockApi()) {
    return Promise.resolve(mockPurchaseOrderFromLowStock(payload));
  }
  return bffFetch<PurchaseOrderRead>("/v1/wms/low-stock/create-po", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

/** POST `/v1/wms/warehouses` — owner/admin/manager only (backend enforced too). */
export function createWmsWarehouse(payload: WarehouseCreateRequest): Promise<WmsWarehouseRead> {
  if (shouldUseMockApi()) {
    return Promise.resolve({
      id: `wh-${payload.code.toLowerCase()}`,
      code: payload.code,
      name: payload.name,
      country_code: (payload.country_code ?? "CN").toUpperCase(),
      city: payload.city ?? "",
    });
  }
  return bffFetch<WmsWarehouseRead>("/v1/wms/warehouses", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

// ---------------------------------------------------------------------------
// Phase 2: Multi-platform real-time inventory sync
// ---------------------------------------------------------------------------

/** GET `/v1/orders/inventory/sync-logs` — paginated channel inventory sync jobs. */
export function listInventorySyncLogs(options?: {
  variantId?: string;
  status?: string;
  limit?: number;
  offset?: number;
}): Promise<PaginatedResponseInventorySyncJobRead> {
  const limit = options?.limit ?? 50;
  const offset = options?.offset ?? 0;
  if (shouldUseMockApi()) {
    let filtered = mockSyncJobs;
    if (options?.variantId) {
      filtered = filtered.filter((j) => j.variant_id === options.variantId);
    }
    if (options?.status) {
      filtered = filtered.filter((j) => j.status === options.status);
    }
    const items = filtered.slice(offset, offset + limit).map((j) => ({ ...j }));
    return Promise.resolve({
      items,
      total: filtered.length,
      limit,
      offset,
      has_more: offset + items.length < filtered.length,
    });
  }
  const params = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
  });
  if (options?.variantId) params.set("variant_id", options.variantId);
  if (options?.status) params.set("status", options.status);
  return bffFetch<PaginatedResponseInventorySyncJobRead>(
    `/v1/orders/inventory/sync-logs?${params.toString()}`
  );
}

/** POST `/v1/orders/inventory/sync-now` — push sellable qty to all mapped stores now. */
export function triggerInventorySyncNow(options?: {
  variantId?: string;
  channelAccountId?: string;
}): Promise<InventorySyncDispatchSummary> {
  if (shouldUseMockApi()) {
    const items = mockSyncJobs.map((j) => ({
      ...j,
      status: "succeeded",
      synced_qty: j.requested_qty,
      error_code: null,
      error_detail: null,
    }));
    return Promise.resolve({
      enqueued: items.length,
      dispatched: items.length,
      succeeded: items.length,
      failed: 0,
      dead_letter: 0,
      jobs: items,
    });
  }
  return bffFetch<InventorySyncDispatchSummary>("/v1/orders/inventory/sync-now", {
    method: "POST",
    body: JSON.stringify({
      variant_id: options?.variantId ?? null,
      channel_account_id: options?.channelAccountId ?? null,
    }),
  });
}

/** POST `/v1/orders/inventory/sync-jobs/{jobId}/retry` — retry one failed/dead-letter job. */
export function retryInventorySyncJob(jobId: string): Promise<InventorySyncJobRead> {
  if (shouldUseMockApi()) {
    const found = mockSyncJobs.find((j) => j.id === jobId) ?? mockSyncJobs[0];
    return Promise.resolve({
      ...found,
      status: "succeeded",
      synced_qty: found.requested_qty,
      error_code: null,
      error_detail: null,
    });
  }
  return bffFetch<InventorySyncJobRead>(
    `/v1/orders/inventory/sync-jobs/${encodeURIComponent(jobId)}/retry`,
    { method: "POST" }
  );
}

// ---------------------------------------------------------------------------
// Phase 3: Multi-platform sales, margin, ROAS & SKU velocity analytics
// ---------------------------------------------------------------------------

export type AnalyticsWindow = "7d" | "30d" | "90d";

/** GET `/v1/finance/channel-analytics/overview?window=...` */
export function fetchChannelAnalyticsOverview(
  window: AnalyticsWindow = "30d"
): Promise<ChannelAnalyticsOverviewRead> {
  if (shouldUseMockApi()) {
    return Promise.resolve({ ...mockChannelAnalyticsOverview, window });
  }
  return bffFetch<ChannelAnalyticsOverviewRead>(
    `/v1/finance/channel-analytics/overview?window=${encodeURIComponent(window)}`
  );
}

/** GET `/v1/finance/channel-analytics/sku-velocity?window=...&limit=...` */
export function fetchSkuVelocity(
  window: AnalyticsWindow = "30d",
  limit = 50
): Promise<SkuVelocityResponse> {
  if (shouldUseMockApi()) {
    return Promise.resolve({ ...mockSkuVelocity, window });
  }
  return bffFetch<SkuVelocityResponse>(
    `/v1/finance/channel-analytics/sku-velocity?window=${encodeURIComponent(window)}&limit=${limit}`
  );
}

/** POST `/v1/finance/channel-analytics/daily-metrics` */
export function upsertChannelDailyMetric(
  payload: ChannelDailyMetricUpsert
): Promise<ChannelDailyMetricRead> {
  if (shouldUseMockApi()) {
    return Promise.resolve({
      id: mockId("metric"),
      tenant_id: "22222222-2222-2222-2222-222222222222",
      channel_account_id: payload.channel_account_id,
      variant_id: payload.variant_id ?? null,
      metric_date: payload.metric_date,
      impressions: payload.impressions ?? 0,
      clicks: payload.clicks ?? 0,
      ad_spend_base: String(payload.ad_spend_base ?? "0.00"),
      external_sessions: payload.external_sessions ?? 0,
      source: payload.source ?? "manual",
      created_at: MOCK_NOW,
      updated_at: MOCK_NOW,
    });
  }
  return bffFetch<ChannelDailyMetricRead>("/v1/finance/channel-analytics/daily-metrics", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

// ---------------------------------------------------------------------------
// Mock fallbacks (only with NEXT_PUBLIC_USE_MOCK_API=true)
// ---------------------------------------------------------------------------

const MOCK_NOW = "2026-01-05T09:15:00.000Z";

const mockSyncJobs: InventorySyncJobRead[] = [
  {
    id: "sync-job-shopify-1",
    tenant_id: "22222222-2222-2222-2222-222222222222",
    variant_id: "var-1",
    mapping_id: "map-shopify-1",
    channel_account_id: "ca-shopify-1",
    channel_type: "shopify",
    channel_label: "Shopify US Sandbox",
    internal_sku: "LED-COB-24V-5M",
    external_sku: "SHOP-LED-COB-5M",
    requested_qty: 28,
    synced_qty: 28,
    status: "succeeded",
    attempts: 1,
    next_retry_at: MOCK_NOW,
    error_code: null,
    error_detail: null,
    trigger_source: "order_reserve",
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
  },
  {
    id: "sync-job-tiktok-1",
    tenant_id: "22222222-2222-2222-2222-222222222222",
    variant_id: "var-1",
    mapping_id: "map-tiktok-1",
    channel_account_id: "ca-tiktok-1",
    channel_type: "tiktok_shop",
    channel_label: "TikTok Shop US",
    internal_sku: "LED-COB-24V-5M",
    external_sku: "TT-LED-COB-5M",
    requested_qty: 28,
    synced_qty: null,
    status: "dead_letter",
    attempts: 3,
    next_retry_at: MOCK_NOW,
    error_code: "HTTP_429",
    error_detail: "TikTok rate limit exceeded during burst push",
    trigger_source: "order_reserve",
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
  },
];

/** Two real safety-stock breaches so the replenishment flow is demoable offline. */
const mockLowStock: LowStockItemRead[] = [
  {
    variant_id: "var-1",
    sku: "LED-COB-24V-5M",
    product_name: "COB LED Strip 24V 5m 4000K",
    warehouse_id: "33333333-3333-3333-3333-333333333333",
    warehouse_name: "Quanzhou Main Warehouse",
    on_hand_qty: 40,
    reserved_qty: 12,
    available_qty: 28,
    safety_stock_qty: 120,
    suggested_reorder_qty: 212,
    sourcing_url: "https://detail.1688.com/offer/777000111222.html",
  },
  {
    variant_id: "var-2",
    sku: "LED-PANEL-60-40W",
    product_name: "LED Panel Light 60x60 40W 5000K",
    warehouse_id: "33333333-3333-3333-3333-333333333333",
    warehouse_name: "Quanzhou Main Warehouse",
    on_hand_qty: 55,
    reserved_qty: 5,
    available_qty: 50,
    safety_stock_qty: 80,
    suggested_reorder_qty: 110,
    sourcing_url: null,
  },
];

const mockChannelAnalyticsOverview: ChannelAnalyticsOverviewRead = {
  window: "30d",
  base_currency: "CNY",
  totals: {
    channel_key: "all",
    channel_account_id: null,
    channel_type: "all",
    channel_label: "All Channels",
    order_count: 18,
    units_sold: 142,
    gmv_base: "46800.00",
    refunded_amount_base: "920.00",
    refund_rate_pct: "1.97",
    cogs_base: "21500.00",
    platform_commission_base: "3120.00",
    ad_spend_base: "4800.00",
    net_margin_base: "16460.00",
    margin_pct: "35.17",
    roas: "9.75",
  },
  channels: [
    {
      channel_key: "ca-shopify-1",
      channel_account_id: "ca-shopify-1",
      channel_type: "shopify",
      channel_label: "Shopify US Sandbox",
      order_count: 10,
      units_sold: 76,
      gmv_base: "26400.00",
      refunded_amount_base: "420.00",
      refund_rate_pct: "1.59",
      cogs_base: "11800.00",
      platform_commission_base: "1320.00",
      ad_spend_base: "2400.00",
      net_margin_base: "10460.00",
      margin_pct: "39.62",
      roas: "11.00",
    },
    {
      channel_key: "ca-tiktok-1",
      channel_account_id: "ca-tiktok-1",
      channel_type: "tiktok_shop",
      channel_label: "TikTok Shop US",
      order_count: 8,
      units_sold: 66,
      gmv_base: "20400.00",
      refunded_amount_base: "500.00",
      refund_rate_pct: "2.45",
      cogs_base: "9700.00",
      platform_commission_base: "1800.00",
      ad_spend_base: "2400.00",
      net_margin_base: "6000.00",
      margin_pct: "29.41",
      roas: "8.50",
    },
  ],
  daily_trend: [
    {
      metric_date: "2026-11-22",
      order_count: 9,
      units_sold: 70,
      gmv_base: "23400.00",
      net_margin_base: "8200.00",
      ad_spend_base: "2400.00",
    },
    {
      metric_date: "2026-11-23",
      order_count: 9,
      units_sold: 72,
      gmv_base: "23400.00",
      net_margin_base: "8260.00",
      ad_spend_base: "2400.00",
    },
  ],
};

const mockSkuVelocity: SkuVelocityResponse = {
  window: "30d",
  base_currency: "CNY",
  total: 2,
  items: [
    {
      variant_id: "var-1",
      product_id: "prod-1",
      sku: "LED-COB-24V-5M",
      product_name: "COB LED Strip 24V 5m 4000K",
      total_units_sold: 90,
      avg_daily_units: "3.00",
      available_qty: 28,
      days_of_cover: "9.3",
      lead_time_days: 12,
      stockout_risk: "critical",
      recommended_reorder_qty: 50,
      cost_price_base: "45.50",
      sourcing_url: "https://detail.1688.com/offer/777000111222.html",
      sourcing_draft_id: "draft-1",
      channel_breakdown: [
        {
          channel_type: "shopify",
          channel_label: "Shopify US Sandbox",
          units_sold: 52,
          revenue_base: "15600.00",
        },
        {
          channel_type: "tiktok_shop",
          channel_label: "TikTok Shop US",
          units_sold: 38,
          revenue_base: "11400.00",
        },
      ],
    },
    {
      variant_id: "var-2",
      product_id: "prod-2",
      sku: "LED-PANEL-60-40W",
      product_name: "LED Panel Light 60x60 40W 5000K",
      total_units_sold: 30,
      avg_daily_units: "1.00",
      available_qty: 50,
      days_of_cover: "50.0",
      lead_time_days: 7,
      stockout_risk: "healthy",
      recommended_reorder_qty: 0,
      cost_price_base: "68.00",
      sourcing_url: null,
      sourcing_draft_id: null,
      channel_breakdown: [
        {
          channel_type: "shopify",
          channel_label: "Shopify US Sandbox",
          units_sold: 30,
          revenue_base: "10800.00",
        },
      ],
    },
  ],
};

function mockId(prefix: string): string {
  return `${prefix}-${Date.now().toString(36)}`;
}

function mockCreateCatalogProductAndVariant(
  productPayload: ProductCreate,
  variantPayload: Omit<VariantCreate, "product_id">
): CreatedCatalogSku {
  const product: ProductRead = {
    id: mockId("prod"),
    tenant_id: "22222222-2222-2222-2222-222222222222",
    spu_code: productPayload.spu_code,
    category: productPayload.category,
    hs_code: productPayload.hs_code ?? null,
    origin_country: productPayload.origin_country ?? "CN",
    i18n_specs: productPayload.i18n_specs ?? {},
    certifications: productPayload.certifications ?? [],
    ai_faq_facts: productPayload.ai_faq_facts ?? {},
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
  };
  const variant: VariantRead = {
    id: mockId("var"),
    tenant_id: product.tenant_id,
    product_id: product.id,
    sku: variantPayload.sku,
    attributes: variantPayload.attributes ?? {},
    base_currency: variantPayload.base_currency ?? "CNY",
    min_margin_floor_pct: String(variantPayload.min_margin_floor_pct ?? "15.00"),
    retail_price_usd: String(variantPayload.retail_price_usd),
    b2b_moq_tiers: [],
    weight_grams: variantPayload.weight_grams,
    carton_dims_cm: variantPayload.carton_dims_cm ?? null,
    lead_time_days: variantPayload.lead_time_days ?? 7,
    cost_price_base: String(variantPayload.cost_price_base ?? "0"),
    created_at: MOCK_NOW,
  };
  return { product, variant };
}

/** Minimal RFC-4180-ish splitter: honours double-quoted cells with `""` escapes. */
function splitCsvLine(line: string): string[] {
  const cells: string[] = [];
  let current = "";
  let quoted = false;
  for (let index = 0; index < line.length; index += 1) {
    const char = line[index];
    if (quoted) {
      if (char === '"' && line[index + 1] === '"') {
        current += '"';
        index += 1;
      } else if (char === '"') {
        quoted = false;
      } else {
        current += char;
      }
      continue;
    }
    if (char === '"') {
      quoted = true;
    } else if (char === ",") {
      cells.push(current);
      current = "";
    } else {
      current += char;
    }
  }
  cells.push(current);
  return cells.map((cell) => cell.trim());
}

/**
 * Offline mirror of the server-side CSV upsert: identical mandatory columns,
 * identical summary counters, per-row errors instead of a hard failure.
 */
function mockImportCatalogCsv(csvText: string): CatalogCsvImportResponse {
  const body = csvText.replace(/^\ufeff/, "").trim();
  if (!body) {
    return { created_products: 0, created_variants: 0, updated_variants: 0, errors: ["CSV 内容为空。"] };
  }
  const lines = body.split(/\r?\n/).filter((line) => line.trim().length > 0);
  const header = splitCsvLine(lines[0]).map((cell) => cell.toLowerCase());
  const missing = CATALOG_CSV_REQUIRED_COLUMNS.filter((column) => !header.includes(column));
  if (missing.length > 0) {
    return {
      created_products: 0,
      created_variants: 0,
      updated_variants: 0,
      errors: [`CSV 表头缺少必填列：${missing.join(", ")}。`],
    };
  }

  const columnIndex = (name: string): number => header.indexOf(name);
  const spuCodes = new Set<string>();
  const skus = new Set<string>();
  const errors: string[] = [];

  lines.slice(1).forEach((line, offset) => {
    const cells = splitCsvLine(line);
    if (cells.every((cell) => cell.length === 0)) return;
    const cellOf = (name: string): string => cells[columnIndex(name)] ?? "";
    const rowNumber = offset + 2;
    const emptyColumns = CATALOG_CSV_REQUIRED_COLUMNS.filter((column) => !cellOf(column));
    if (emptyColumns.length > 0) {
      errors.push(`第 ${rowNumber} 行缺少必填列：${emptyColumns.join(", ")}。`);
      return;
    }
    if (!DECIMAL_PATTERN.test(cellOf("retail_price_usd"))) {
      errors.push(`第 ${rowNumber} 行零售价不是合法金额。`);
      return;
    }
    spuCodes.add(cellOf("spu_code"));
    skus.add(cellOf("sku"));
  });

  return {
    created_products: spuCodes.size,
    created_variants: skus.size,
    updated_variants: 0,
    errors,
  };
}

function mockPurchaseOrderFromLowStock(payload: LowStockCreatePoRequest): PurchaseOrderRead {
  const quantity = Number(payload.quantity);
  const unitCost = Number(payload.unit_cost);
  const total = Number.isFinite(quantity * unitCost) ? (quantity * unitCost).toFixed(2) : "0.00";
  const item = mockLowStock.find((row) => row.variant_id === payload.variant_id);
  return {
    id: mockId("po"),
    po_no: `PO-MOCK-${Date.now().toString().slice(-6)}`,
    supplier_id: payload.supplier_id,
    warehouse_id: item?.warehouse_id ?? "33333333-3333-3333-3333-333333333333",
    currency: payload.currency ?? "USD",
    total_amount: total,
    status: "draft",
    expected_arrival_date: null,
    notes: payload.notes ?? null,
    created_by: "11111111-1111-1111-1111-111111111111",
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
    items: [
      {
        id: mockId("poi"),
        variant_id: payload.variant_id,
        sku: item?.sku ?? "",
        qty_ordered: quantity,
        qty_received: 0,
        unit_cost: String(payload.unit_cost),
      },
    ],
  };
}
