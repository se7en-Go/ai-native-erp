// Pack F（外贸选品 Sourcing）专用 API 模块。
//
// 两件事，全部经同源 `bffFetch`（/api/bff → FastAPI /api/v1/*）：
//   1. 三个外贸高频渠道接入：Alibaba.com 国际站、Amazon 卖家中心、eBay；
//   2. 1688 / URL 采集搬家闭环：采集 → 草稿箱 → 一键入主档 SKU → 多平台刊登。
//
// 契约类型来自 `types.gen.ts`（由后端统一重新生成，前端不得手改），
// 字段名一字不改：`AlibabaIntlConnectRequest` / `AmazonConnectRequest` /
// `EbayConnectRequest` / `SourcingCollectRequest` / `SourcingDraftRead` /
// `SourcingPublishResult`。Mock 模式下返回本地镜像数据，零后端也能演示整条闭环。
import { bffFetch } from "@/lib/api";
import { shouldUseMockApi } from "@/lib/data-source";
import type { MessagingConnectResult } from "@/lib/api-client/live-client";
import type {
  AlibabaIntlConnectRequest,
  AmazonConnectRequest,
  EbayConnectRequest,
  SourcingCollectRequest,
  SourcingDeleteResult,
  SourcingDraftRead,
  SourcingImportResult,
  SourcingMoqTier,
  SourcingPublishRequest,
  SourcingPublishResult,
} from "@/lib/api-client/types.gen";

// ---------------------------------------------------------------------------
// 三个新增外贸渠道
// ---------------------------------------------------------------------------

/** 新增的 3 个外贸渠道键，与 `ChannelType` 的 `alibaba_email` / `amazon_sp` / `ebay` 对应。 */
export const SOURCING_CHANNEL_KEYS = ["alibaba_intl", "amazon", "ebay"] as const;

export type SourcingChannelKey = (typeof SOURCING_CHANNEL_KEYS)[number];

export function isSourcingChannelKey(value: unknown): value is SourcingChannelKey {
  return (
    typeof value === "string" &&
    (SOURCING_CHANNEL_KEYS as readonly string[]).includes(value)
  );
}

/** 三个渠道各自的外部账号 ID 字段（提交成功后回显，绝不回显任何密钥）。 */
export function sourcingExternalAccountId(
  key: SourcingChannelKey,
  values: { store_id?: string; seller_id?: string; seller_username?: string },
): string {
  switch (key) {
    case "alibaba_intl":
      return (values.store_id ?? "").trim();
    case "amazon":
      return (values.seller_id ?? "").trim();
    case "ebay":
      return (values.seller_username ?? "").trim();
  }
}

function mapSourcingConnection(raw: {
  channel_account_id?: unknown;
  external_account_id?: unknown;
  display_name?: unknown;
  visibility_scope?: unknown;
}): MessagingConnectResult {
  const scope = raw.visibility_scope;
  return {
    channel_account_id: String(raw.channel_account_id ?? ""),
    external_account_id: String(raw.external_account_id ?? ""),
    display_name: typeof raw.display_name === "string" ? raw.display_name : null,
    visibility_scope:
      scope === "personal" || scope === "private"
        ? "personal"
        : scope === "shared"
          ? "shared"
          : undefined,
  };
}

/** POST /v1/channels/alibaba-intl/connect — App Key/Secret + 询盘邮箱。 */
export async function connectAlibabaIntl(
  body: AlibabaIntlConnectRequest,
): Promise<MessagingConnectResult> {
  if (shouldUseMockApi()) {
    return mapSourcingConnection({
      channel_account_id: "mock-alibaba-intl-account",
      external_account_id: body.store_id.trim(),
      display_name: body.display_name,
      visibility_scope: body.visibility_scope ?? "shared",
    });
  }
  const raw = await bffFetch<Record<string, unknown>>("/v1/channels/alibaba-intl/connect", {
    method: "POST",
    body: JSON.stringify(body),
  });
  return mapSourcingConnection(raw);
}

/** POST /v1/channels/amazon/connect — Seller ID + Marketplace ID + LWA + Refresh Token。 */
export async function connectAmazon(
  body: AmazonConnectRequest,
): Promise<MessagingConnectResult> {
  if (shouldUseMockApi()) {
    return mapSourcingConnection({
      channel_account_id: "mock-amazon-account",
      external_account_id: body.seller_id.trim(),
      display_name: body.display_name,
      visibility_scope: "shared",
    });
  }
  const raw = await bffFetch<Record<string, unknown>>("/v1/channels/amazon/connect", {
    method: "POST",
    body: JSON.stringify(body),
  });
  return mapSourcingConnection(raw);
}

/** POST /v1/channels/ebay/connect — Seller Username + Site ID + App/Cert + Refresh Token。 */
export async function connectEbay(body: EbayConnectRequest): Promise<MessagingConnectResult> {
  if (shouldUseMockApi()) {
    return mapSourcingConnection({
      channel_account_id: "mock-ebay-account",
      external_account_id: body.seller_username.trim(),
      display_name: body.display_name,
      visibility_scope: "shared",
    });
  }
  const raw = await bffFetch<Record<string, unknown>>("/v1/channels/ebay/connect", {
    method: "POST",
    body: JSON.stringify(body),
  });
  return mapSourcingConnection(raw);
}

/** 抽屉统一分发：只按渠道键选端点，密钥字段原样透传（后端信封加密）。 */
export function connectSourcingChannel(
  key: SourcingChannelKey,
  body: {
    alibaba: AlibabaIntlConnectRequest;
    amazon: AmazonConnectRequest;
    ebay: EbayConnectRequest;
  },
): Promise<MessagingConnectResult> {
  switch (key) {
    case "alibaba_intl":
      return connectAlibabaIntl(body.alibaba);
    case "amazon":
      return connectAmazon(body.amazon);
    case "ebay":
      return connectEbay(body.ebay);
  }
}

// ---------------------------------------------------------------------------
// 采集搬家：纯计算工具（Decimal 语义，避免 IEEE-754 抖动）
// ---------------------------------------------------------------------------

export const SOURCING_DEFAULT_FX_RATE = "7.20";
export const SOURCING_DEFAULT_MARGIN_PCT = "35";
export const SOURCING_DEFAULT_CATEGORY = "LED Lighting";

/** MOQ 阶梯：与后端 `_MOQ_LADDER` 一致（100 起订 0%、500 让 5%、2000 让 10%）。 */
export const SOURCING_MOQ_LADDER: ReadonlyArray<{ min_qty: number; discount: number }> = [
  { min_qty: 100, discount: 0 },
  { min_qty: 500, discount: 0.05 },
  { min_qty: 2000, discount: 0.1 },
];

/** ROUND_HALF_UP 到 2 位小数，输出定点字符串（钱永远不落地成 float）。 */
export function moneyString(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0.00";
  return (Math.round(value * 100 + Number.EPSILON) / 100).toFixed(2);
}

/** 宽松读取 `number | string | null` 的十进制输入。 */
export function decimalInput(
  raw: number | string | null | undefined,
  fallback: number,
): number {
  const value = typeof raw === "string" ? Number(raw.replace(/,/g, "").trim()) : raw;
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : fallback;
}

/** CNY 进货价 → USD 成本 → 目标毛利率反推零售价。 */
export function costAndTargetPrice(input: {
  raw_price_cny: number;
  fx_rate_usd_cny: number;
  target_margin_pct: number;
}): { cost_usd: string; target_price_usd: string } {
  const fx = input.fx_rate_usd_cny > 0 ? input.fx_rate_usd_cny : 7.2;
  const margin = Math.min(Math.max(input.target_margin_pct, 1), 90);
  const cost = input.raw_price_cny / fx;
  const target = cost / (1 - margin / 100);
  return { cost_usd: moneyString(cost), target_price_usd: moneyString(target) };
}

/** 3 档 MOQ 阶梯价（按目标零售价等比让利，与后端阶梯一致）。 */
export function moqTiersFromTargetPrice(targetPriceUsd: string): SourcingMoqTier[] {
  const base = Number(targetPriceUsd);
  return SOURCING_MOQ_LADDER.map((tier) => ({
    min_qty: tier.min_qty,
    unit_price_usd: moneyString(base * (1 - tier.discount)),
  }));
}

function asciiWords(raw: string): string {
  return raw
    .replace(/[^\x20-\x7e]/g, " ")
    .replace(/[^A-Za-z0-9 +.-]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

/** 把中文标题/链接转成可用的英文 SEO 标题（Mock 版的「AI 优化」）。 */
export function optimizedEnglishTitle(
  raw: string,
  category: string = SOURCING_DEFAULT_CATEGORY,
): string {
  const words = asciiWords(raw);
  if (words.length >= 10) {
    return `${words} | OEM Factory Direct Wholesale`.slice(0, 190);
  }
  return `${category} OEM Wholesale Supplier - Factory Direct Export Pricing`.slice(0, 190);
}

/** 5 条海外卖点（Mock 版改写；线上由后端确定性模板生成）。 */
export function sourcingBulletPoints(moqTiers: SourcingMoqTier[]): string[] {
  const firstTier = moqTiers[0]?.unit_price_usd ?? "0.00";
  return [
    `Factory-direct pricing from ${firstTier} USD/unit at 100 pcs MOQ.`,
    "CE / RoHS compliant build with 50,000-hour rated lifetime.",
    "OEM logo, packaging and voltage customisation supported.",
    "7-day sample lead time, 18-day bulk production lead time.",
    "FOB Fuzhou with DDP door-to-door shipping available.",
  ];
}

function suggestedSku(raw: string, platform: string): string {
  const words = asciiWords(raw)
    .toUpperCase()
    .replace(/[^A-Z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 24);
  if (words) return `SRC-${words}`;
  let digest = 0;
  for (let index = 0; index < raw.length; index += 1) {
    digest = (digest * 31 + raw.charCodeAt(index)) % 46656;
  }
  return `SRC-${platform.toUpperCase().replace(/[^A-Z0-9]/g, "")}-${digest
    .toString(36)
    .padStart(3, "0")
    .toUpperCase()}`;
}

/**
 * 纯函数：把一次采集请求算成一条草稿（与后端 `plan_sourcing_draft` 同构）。
 * Mock 与 UI 预览都复用它，所以采集箱里的价格永远可复算。
 */
export function planSourcingDraft(
  request: SourcingCollectRequest,
  options: { id?: string; now?: string } = {},
): SourcingDraftRead {
  const now = options.now ?? new Date().toISOString();
  const platform = request.source_platform ?? "1688";
  const rawTitle = (request.raw_title ?? request.source_url ?? "").trim();
  const rawText = (request.raw_text ?? "").trim();
  const category = (request.category ?? "").trim() || SOURCING_DEFAULT_CATEGORY;
  const priceCny = decimalInput(request.raw_price_cny, 18.6);
  const fx = decimalInput(request.fx_rate_usd_cny, Number(SOURCING_DEFAULT_FX_RATE));
  const margin = decimalInput(request.target_margin_pct, Number(SOURCING_DEFAULT_MARGIN_PCT));
  const { cost_usd, target_price_usd } = costAndTargetPrice({
    raw_price_cny: priceCny,
    fx_rate_usd_cny: fx,
    target_margin_pct: margin,
  });
  const moq_tiers = moqTiersFromTargetPrice(target_price_usd);
  const optimized_title_en = optimizedEnglishTitle(rawTitle || rawText, category);

  return {
    id: options.id ?? "mock-sourcing-draft-new",
    tenant_id: "mock-tenant",
    source_platform: platform,
    source_url: request.source_url ?? null,
    raw_title: rawTitle || optimized_title_en,
    raw_price_cny: moneyString(priceCny),
    fx_rate_usd_cny: moneyString(fx),
    target_margin_pct: moneyString(margin),
    cost_usd,
    target_price_usd,
    optimized_title_en,
    bullet_points: sourcingBulletPoints(moq_tiers),
    suggested_sku: suggestedSku(rawTitle || rawText || category, platform),
    suggested_category: category,
    suggested_hs_code: request.hs_code ?? "94054200",
    moq_tiers,
    images: request.images ?? [],
    attributes: {
      source_platform: platform,
      incoterm: "FOB Fuzhou",
      lead_time_days: 18,
      sample_days: 7,
      carton_qty: 50,
      raw_text_excerpt: rawText.slice(0, 240),
    },
    status: "draft",
    imported_product_id: null,
    imported_variant_id: null,
    publish_history: [],
    created_at: now,
    updated_at: now,
  };
}

// ---------------------------------------------------------------------------
// 采集箱（Mock 数据 + 状态）
// ---------------------------------------------------------------------------

function seedDraft(
  overrides: Partial<SourcingDraftRead> & { id: string },
  extra: Partial<SourcingCollectRequest> = {},
): SourcingDraftRead {
  const base = planSourcingDraft(
    {
      source_platform: "1688",
      source_url: "https://detail.1688.com/offer/680001234567.html",
      raw_title: "COB LED Strip 24V 5m 4000K 高显指灯带",
      raw_price_cny: "18.60",
      fx_rate_usd_cny: SOURCING_DEFAULT_FX_RATE,
      target_margin_pct: SOURCING_DEFAULT_MARGIN_PCT,
      category: "LED Strip Lighting",
      hs_code: "94054200",
      ...extra,
    },
    { id: overrides.id, now: "2026-02-11T02:20:00.000Z" },
  );
  return { ...base, ...overrides };
}

/** 采集箱演示数据：草稿 / 已入主档 / 已刊登 三种状态各一条。 */
export const MOCK_SOURCING_DRAFTS: SourcingDraftRead[] = [
  seedDraft({ id: "src-draft-1" }),
  seedDraft({
    id: "src-draft-2",
    source_platform: "url",
    source_url: "https://www.alibaba.com/product-detail/outdoor-flood-light",
    raw_title: "Outdoor flood light 200W IP66 太阳能投光灯",
    suggested_category: "LED Flood Lighting",
    optimized_title_en:
      "Outdoor Flood Light 200W IP66 Solar Powered | OEM Factory Direct Wholesale",
    suggested_sku: "SRC-OUTDOOR-FLOOD-LIGHT-200W",
    status: "imported",
    imported_product_id: "mock-product-1",
    imported_variant_id: "mock-variant-1",
  }),
  seedDraft({
    id: "src-draft-3",
    source_platform: "manual",
    source_url: null,
    raw_title: "庭院灯 智能 遥控 防水",
    suggested_category: "Garden Lighting",
    optimized_title_en: "Smart Remote Control Garden Light IP65 | Factory Direct Export",
    suggested_sku: "SRC-SMART-GARDEN-LIGHT",
    status: "published",
    imported_product_id: "mock-product-2",
    imported_variant_id: "mock-variant-2",
    publish_history: [
      {
        channel_type: "shopify",
        external_sku: "SHOPIFY-SRC-SMART-GARDEN-LIGHT",
        price_usd: "42.80",
        published_at: "2026-02-09T06:10:00.000Z",
      },
    ],
  }, { raw_text: "支持遥控调光，铝材灯体，IP65 防水，出口欧美。" }),
];

let mockDrafts: SourcingDraftRead[] = MOCK_SOURCING_DRAFTS.map((draft) => ({ ...draft }));

/** Mock 模式的采集箱当前快照（只读，供测试断言）。 */
export function mockSourcingDrafts(): SourcingDraftRead[] {
  return mockDrafts.map((draft) => ({ ...draft }));
}

// ---------------------------------------------------------------------------
// 采集搬家：BFF 端点
// ---------------------------------------------------------------------------

/** POST /v1/catalog/sourcing/collect — 采集 + AI 优化，落一条草稿。 */
export async function collectSourcingDraft(
  body: SourcingCollectRequest,
): Promise<SourcingDraftRead> {
  if (shouldUseMockApi()) {
    const created = planSourcingDraft(body, {
      id: `src-draft-mock-${mockDrafts.length + 1}`,
    });
    mockDrafts = [created, ...mockDrafts];
    return created;
  }
  return bffFetch<SourcingDraftRead>("/v1/catalog/sourcing/collect", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** GET /v1/catalog/sourcing/drafts — 采集箱草稿列表（新到旧）。 */
export async function fetchSourcingDrafts(): Promise<SourcingDraftRead[]> {
  if (shouldUseMockApi()) return mockSourcingDrafts();
  return bffFetch<SourcingDraftRead[]>("/v1/catalog/sourcing/drafts", { method: "GET" });
}

/** DELETE /v1/catalog/sourcing/drafts/{draft_id} — 移出采集箱。 */
export async function deleteSourcingDraft(draftId: string): Promise<SourcingDeleteResult> {
  if (shouldUseMockApi()) {
    mockDrafts = mockDrafts.filter((draft) => draft.id !== draftId);
    return { deleted: true, draft_id: draftId };
  }
  return bffFetch<SourcingDeleteResult>(
    `/v1/catalog/sourcing/drafts/${encodeURIComponent(draftId)}`,
    { method: "DELETE" },
  );
}

/** POST /v1/catalog/sourcing/drafts/{draft_id}/import-to-catalog — 一键入主档 SKU。 */
export async function importSourcingDraftToCatalog(
  draftId: string,
): Promise<SourcingImportResult> {
  if (shouldUseMockApi()) {
    const draft = mockDrafts.find((item) => item.id === draftId) ?? mockDrafts[0];
    const imported: SourcingDraftRead = {
      ...draft,
      status: "imported",
      imported_product_id: `mock-product-${draft.id}`,
      imported_variant_id: `mock-variant-${draft.id}`,
      updated_at: new Date().toISOString(),
    };
    mockDrafts = mockDrafts.map((item) => (item.id === draft.id ? imported : item));
    return {
      created: true,
      draft: imported,
      product_id: imported.imported_product_id ?? "",
      variant_id: imported.imported_variant_id ?? "",
      sku: imported.suggested_sku,
      retail_price_usd: imported.target_price_usd,
    };
  }
  return bffFetch<SourcingImportResult>(
    `/v1/catalog/sourcing/drafts/${encodeURIComponent(draftId)}/import-to-catalog`,
    { method: "POST" },
  );
}

/** POST /v1/catalog/sourcing/drafts/{draft_id}/publish — 多平台一键刊登。 */
export async function publishSourcingDraft(
  draftId: string,
  body: SourcingPublishRequest,
): Promise<SourcingPublishResult[]> {
  if (shouldUseMockApi()) {
    const draft = mockDrafts.find((item) => item.id === draftId) ?? mockDrafts[0];
    const multiplier = decimalInput(body.price_multiplier, 1);
    const ratio = decimalInput(body.stock_sync_ratio, 1);
    const publishedAt = new Date().toISOString();
    const results: SourcingPublishResult[] = body.channel_account_ids.map(
      (accountId, index) => ({
        channel_account_id: accountId,
        channel_type: mockStoreChannelType(accountId),
        external_sku: `${draft.suggested_sku}-${String(index + 1).padStart(2, "0")}`,
        external_product_id: `ext-${draft.id}-${index + 1}`,
        price_usd: moneyString(Number(draft.target_price_usd) * multiplier),
        stock_sync_ratio: moneyString(ratio),
        mapping_id: `mock-mapping-${draft.id}-${index + 1}`,
        published_at: publishedAt,
      }),
    );
    const published: SourcingDraftRead = {
      ...draft,
      status: "published",
      publish_history: [
        ...(draft.publish_history ?? []),
        ...results.map((result) => ({
          channel_type: result.channel_type,
          external_sku: result.external_sku,
          price_usd: result.price_usd,
          published_at: result.published_at,
        })),
      ],
      updated_at: publishedAt,
    };
    mockDrafts = mockDrafts.map((item) => (item.id === draft.id ? published : item));
    return results;
  }
  return bffFetch<SourcingPublishResult[]>(
    `/v1/catalog/sourcing/drafts/${encodeURIComponent(draftId)}/publish`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

// ---------------------------------------------------------------------------
// 刊登目标店铺展示
// ---------------------------------------------------------------------------

/** 可刊登的渠道类型（与后端 `_PUBLISHABLE_CHANNEL_TYPES` 对齐）。 */
export const SOURCING_PUBLISHABLE_CHANNEL_TYPES = [
  "shopify",
  "amazon_sp",
  "ebay",
  "alibaba_email",
  "tiktok_shop",
  "woocommerce",
  "shopline",
  "shoplazza",
] as const;

const STORE_LABELS: Record<string, string> = {
  shopify: "Shopify",
  amazon_sp: "Amazon",
  ebay: "eBay",
  alibaba_email: "Alibaba.com",
  tiktok_shop: "TikTok Shop",
  woocommerce: "WooCommerce",
  shopline: "SHOPLINE",
  shoplazza: "Shoplazza",
  web_livechat: "Web chat",
  email: "Email",
  telegram: "Telegram",
  whatsapp_cloud: "WhatsApp",
  whatsapp_coexistence: "WhatsApp",
  messenger: "Messenger",
  instagram_dm: "Instagram",
  made_in_china_email: "Made-in-China",
};

/** 渠道类型 → 展板文案；未知类型回退成大写原值，绝不留空。 */
export function sourcingStoreLabel(channelType: string): string {
  return STORE_LABELS[channelType] ?? channelType.replace(/_/g, " ").toUpperCase();
}

/** 演示店铺账号：mock 模式且租户尚未接入任何店铺时用于补齐刊登目标。 */
export const MOCK_SOURCING_STORE_ACCOUNTS: ReadonlyArray<{
  id: string;
  channel_type: string;
  display_name: string;
}> = [
  { id: "ch-shopify-qzled", channel_type: "shopify", display_name: "qzled.myshopify.com" },
  { id: "ch-amazon-qzled", channel_type: "amazon_sp", display_name: "QZ LED · Amazon US" },
  { id: "ch-ebay-qzled", channel_type: "ebay", display_name: "qzled_official · eBay US" },
  { id: "ch-alibaba-qzled", channel_type: "alibaba_email", display_name: "QZ LED · Alibaba 国际站" },
  { id: "ch-tiktok-qzled", channel_type: "tiktok_shop", display_name: "QZ LED · TikTok Shop" },
];

function mockStoreChannelType(accountId: string): string {
  return (
    MOCK_SOURCING_STORE_ACCOUNTS.find((store) => store.id === accountId)?.channel_type ??
    "shopify"
  );
}

/** 刊登目标店铺：优先真实已接入店铺，mock 模式无店铺时补 5 个演示店铺。 */
export function sourcingStoreAccounts(
  accounts: ReadonlyArray<{ id: string; channel_type: string; display_name: string }>,
): Array<{ id: string; channel_type: string; display_name: string }> {
  const publishable = accounts
    .filter((account) =>
      (SOURCING_PUBLISHABLE_CHANNEL_TYPES as readonly string[]).includes(account.channel_type),
    )
    .map((account) => ({
      id: account.id,
      channel_type: account.channel_type,
      display_name: account.display_name,
    }));
  if (publishable.length > 0) return publishable;
  return shouldUseMockApi() ? MOCK_SOURCING_STORE_ACCOUNTS.map((store) => ({ ...store })) : [];
}

/** 草稿状态徽标：草稿 / 已入主档 / 已刊登。 */
export function sourcingStatusCopy(status: string): { zh: string; en: string; tone: string } {
  switch (status) {
    case "imported":
      return {
        zh: "已入主档",
        en: "Imported",
        tone: "border-sky-200 bg-sky-50 text-sky-800 dark:border-sky-800/70 dark:bg-sky-950/40 dark:text-sky-300",
      };
    case "published":
      return {
        zh: "已刊登",
        en: "Published",
        tone: "border-emerald-200 bg-emerald-50 text-emerald-800 dark:border-emerald-800/70 dark:bg-emerald-950/40 dark:text-emerald-300",
      };
    default:
      return {
        zh: "草稿",
        en: "Draft",
        tone: "border-slate-200 bg-slate-100 text-slate-700 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300",
      };
  }
}
