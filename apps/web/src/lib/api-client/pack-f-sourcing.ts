// Pack F（外贸选品 Sourcing）专用 API 模块。
//
// 三件事，全部经同源 `bffFetch`（/api/bff → FastAPI /api/v1/*）：
//   1. 三个外贸高频渠道接入：Alibaba.com 国际站、Amazon 卖家中心、eBay；
//   2. 8 个全球化电商平台接入：Shopee / Lazada / Temu / SHEIN / AliExpress /
//      Ozon / Walmart / Mercado Libre（与后端 `ChannelType` 一一对应）；
//   3. 1688 / URL 采集搬家闭环：采集 → 草稿箱 → 一键入主档 SKU → 多平台刊登。
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
  AliExpressConnectRequest,
  AmazonConnectRequest,
  CollectorTokenCreateRequest,
  CollectorTokenCreateResponse,
  CollectorTokenRead,
  EbayConnectRequest,
  LazadaConnectRequest,
  MediaAssetRead,
  MediaUrlIngestRequest,
  MercadoLibreConnectRequest,
  OzonConnectRequest,
  SheinConnectRequest,
  ShopeeConnectRequest,
  SourcingAiRewriteRequest,
  SourcingAiRewriteResponse,
  SourcingCollectRequest,
  SourcingCopyRewriteOutput,
  SourcingDeleteResult,
  SourcingDraftRead,
  SourcingDraftUpdateRequest,
  SourcingImportResult,
  SourcingMoqTier,
  SourcingPublishRequest,
  SourcingPublishResult,
  SourcingVariantSpecOutput,
  TemuConnectRequest,
  WalmartConnectRequest,
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
// 8 个全球化电商平台
//
// 与后端 `ChannelType` 的 `shopee` / `lazada` / `temu` / `shein` /
// `aliexpress` / `ozon` / `walmart` / `mercado_libre` 一一对应；
// 上面的 `SOURCING_CHANNEL_KEYS`（alibaba_intl / amazon / ebay）保持不变。
// ---------------------------------------------------------------------------

/** 8 个全球化电商平台键，顺序与后端 `ChannelType` 声明顺序一致。 */
export const GLOBAL_MARKETPLACE_CHANNEL_KEYS = [
  "shopee",
  "lazada",
  "temu",
  "shein",
  "aliexpress",
  "ozon",
  "walmart",
  "mercado_libre",
] as const;

export type GlobalMarketplaceChannelKey = (typeof GLOBAL_MARKETPLACE_CHANNEL_KEYS)[number];

export function isGlobalMarketplaceChannelKey(
  value: unknown,
): value is GlobalMarketplaceChannelKey {
  return (
    typeof value === "string" &&
    (GLOBAL_MARKETPLACE_CHANNEL_KEYS as readonly string[]).includes(value)
  );
}

/** 每个平台「外部账号 ID」的表单字段（提交成功后回显，绝不回显任何密钥）。 */
export function globalMarketplaceExternalAccountId(
  key: GlobalMarketplaceChannelKey,
  values: {
    shop_id?: string;
    seller_id?: string;
    mall_id?: string;
    client_id?: string;
  },
): string {
  switch (key) {
    case "shopee":
      return (values.shop_id ?? "").trim();
    case "temu":
      return (values.mall_id ?? "").trim();
    case "ozon":
      return (values.client_id ?? "").trim();
    case "lazada":
    case "shein":
    case "aliexpress":
    case "walmart":
    case "mercado_libre":
      return (values.seller_id ?? "").trim();
  }
}

/** POST /v1/channels/shopee/connect — Shop ID + Partner ID/Key + Access Token。 */
export async function connectShopee(
  body: ShopeeConnectRequest,
): Promise<MessagingConnectResult> {
  if (shouldUseMockApi()) {
    return mapSourcingConnection({
      channel_account_id: "mock-shopee-account",
      external_account_id: body.shop_id.trim(),
      display_name: body.display_name,
      visibility_scope: "shared",
    });
  }
  const raw = await bffFetch<Record<string, unknown>>("/v1/channels/shopee/connect", {
    method: "POST",
    body: JSON.stringify(body),
  });
  return mapSourcingConnection(raw);
}

/** POST /v1/channels/lazada/connect — Seller ID + App Key/Secret + Access Token。 */
export async function connectLazada(
  body: LazadaConnectRequest,
): Promise<MessagingConnectResult> {
  if (shouldUseMockApi()) {
    return mapSourcingConnection({
      channel_account_id: "mock-lazada-account",
      external_account_id: body.seller_id.trim(),
      display_name: body.display_name,
      visibility_scope: "shared",
    });
  }
  const raw = await bffFetch<Record<string, unknown>>("/v1/channels/lazada/connect", {
    method: "POST",
    body: JSON.stringify(body),
  });
  return mapSourcingConnection(raw);
}

/** POST /v1/channels/temu/connect — Mall ID + App Key/Secret + Access Token。 */
export async function connectTemu(body: TemuConnectRequest): Promise<MessagingConnectResult> {
  if (shouldUseMockApi()) {
    return mapSourcingConnection({
      channel_account_id: "mock-temu-account",
      external_account_id: body.mall_id.trim(),
      display_name: body.display_name,
      visibility_scope: "shared",
    });
  }
  const raw = await bffFetch<Record<string, unknown>>("/v1/channels/temu/connect", {
    method: "POST",
    body: JSON.stringify(body),
  });
  return mapSourcingConnection(raw);
}

/** POST /v1/channels/shein/connect — Seller ID + Open Key ID / Secret Key。 */
export async function connectShein(body: SheinConnectRequest): Promise<MessagingConnectResult> {
  if (shouldUseMockApi()) {
    return mapSourcingConnection({
      channel_account_id: "mock-shein-account",
      external_account_id: body.seller_id.trim(),
      display_name: body.display_name,
      visibility_scope: "shared",
    });
  }
  const raw = await bffFetch<Record<string, unknown>>("/v1/channels/shein/connect", {
    method: "POST",
    body: JSON.stringify(body),
  });
  return mapSourcingConnection(raw);
}

/** POST /v1/channels/aliexpress/connect — Seller ID + App Key/Secret + Access Token。 */
export async function connectAliExpress(
  body: AliExpressConnectRequest,
): Promise<MessagingConnectResult> {
  if (shouldUseMockApi()) {
    return mapSourcingConnection({
      channel_account_id: "mock-aliexpress-account",
      external_account_id: body.seller_id.trim(),
      display_name: body.display_name,
      visibility_scope: "shared",
    });
  }
  const raw = await bffFetch<Record<string, unknown>>("/v1/channels/aliexpress/connect", {
    method: "POST",
    body: JSON.stringify(body),
  });
  return mapSourcingConnection(raw);
}

/** POST /v1/channels/ozon/connect — Client ID + API Key（仓库 ID 归后端 config）。 */
export async function connectOzon(body: OzonConnectRequest): Promise<MessagingConnectResult> {
  if (shouldUseMockApi()) {
    return mapSourcingConnection({
      channel_account_id: "mock-ozon-account",
      external_account_id: body.client_id.trim(),
      display_name: body.display_name,
      visibility_scope: "shared",
    });
  }
  const raw = await bffFetch<Record<string, unknown>>("/v1/channels/ozon/connect", {
    method: "POST",
    body: JSON.stringify(body),
  });
  return mapSourcingConnection(raw);
}

/** POST /v1/channels/walmart/connect — Seller ID + Client ID/Secret（Access Token 可选）。 */
export async function connectWalmart(
  body: WalmartConnectRequest,
): Promise<MessagingConnectResult> {
  if (shouldUseMockApi()) {
    return mapSourcingConnection({
      channel_account_id: "mock-walmart-account",
      external_account_id: body.seller_id.trim(),
      display_name: body.display_name,
      visibility_scope: "shared",
    });
  }
  const raw = await bffFetch<Record<string, unknown>>("/v1/channels/walmart/connect", {
    method: "POST",
    body: JSON.stringify(body),
  });
  return mapSourcingConnection(raw);
}

/** POST /v1/channels/mercado-libre/connect — Seller ID + Site ID + Access Token。 */
export async function connectMercadoLibre(
  body: MercadoLibreConnectRequest,
): Promise<MessagingConnectResult> {
  if (shouldUseMockApi()) {
    return mapSourcingConnection({
      channel_account_id: "mock-mercado-libre-account",
      external_account_id: body.seller_id.trim(),
      display_name: body.display_name,
      visibility_scope: "shared",
    });
  }
  const raw = await bffFetch<Record<string, unknown>>("/v1/channels/mercado-libre/connect", {
    method: "POST",
    body: JSON.stringify(body),
  });
  return mapSourcingConnection(raw);
}

/** 8 个平台的请求体映射（抽屉统一分发用）。 */
export type GlobalMarketplaceConnectRequests = {
  shopee: ShopeeConnectRequest;
  lazada: LazadaConnectRequest;
  temu: TemuConnectRequest;
  shein: SheinConnectRequest;
  aliexpress: AliExpressConnectRequest;
  ozon: OzonConnectRequest;
  walmart: WalmartConnectRequest;
  mercado_libre: MercadoLibreConnectRequest;
};

/** 抽屉/页面统一分发：8 个平台各自打自己的 `/connect` 端点。 */
export function connectGlobalMarketplaceChannel(
  key: GlobalMarketplaceChannelKey,
  body: GlobalMarketplaceConnectRequests,
): Promise<MessagingConnectResult> {
  switch (key) {
    case "shopee":
      return connectShopee(body.shopee);
    case "lazada":
      return connectLazada(body.lazada);
    case "temu":
      return connectTemu(body.temu);
    case "shein":
      return connectShein(body.shein);
    case "aliexpress":
      return connectAliExpress(body.aliexpress);
    case "ozon":
      return connectOzon(body.ozon);
    case "walmart":
      return connectWalmart(body.walmart);
    case "mercado_libre":
      return connectMercadoLibre(body.mercado_libre);
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
  const priceCny = decimalInput(
    request.raw_price_cny ?? request.variants_spec?.[0]?.cost_price_cny,
    18.6,
  );
  const fx = decimalInput(request.fx_rate_usd_cny, Number(SOURCING_DEFAULT_FX_RATE));
  const margin = decimalInput(request.target_margin_pct, Number(SOURCING_DEFAULT_MARGIN_PCT));
  const { cost_usd, target_price_usd } = costAndTargetPrice({
    raw_price_cny: priceCny,
    fx_rate_usd_cny: fx,
    target_margin_pct: margin,
  });
  const moq_tiers = moqTiersFromTargetPrice(target_price_usd);
  const optimized_title_en = optimizedEnglishTitle(rawTitle || rawText, category);
  const variants_spec: SourcingVariantSpecOutput[] = (request.variants_spec ?? []).map((v) => {
    const vCost = decimalInput(v.cost_price_cny, priceCny);
    const computedUsd = v.retail_price_usd
      ? moneyString(decimalInput(v.retail_price_usd, Number(target_price_usd)))
      : costAndTargetPrice({
          raw_price_cny: vCost,
          fx_rate_usd_cny: fx,
          target_margin_pct: margin,
        }).target_price_usd;
    return {
      sku_suffix: v.sku_suffix.trim().toUpperCase(),
      option_values: v.option_values ?? {},
      cost_price_cny: moneyString(vCost),
      retail_price_usd: computedUsd,
      image_url: v.image_url ?? null,
      initial_stock: v.initial_stock ?? 100,
    };
  });

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
    options_spec: request.options_spec ?? [],
    variants_spec,
    attributes: {
      source_platform: platform,
      incoterm: "FOB Fuzhou",
      lead_time_days: 18,
      sample_days: 7,
      carton_qty: 50,
      raw_text_excerpt: rawText.slice(0, 240),
    },
    // 多语种文案deck与平台类目属性：新草稿一开始就是空对象（不再是 undefined），
    // 这样 `Object.entries(draft.localized_copies ?? {})` 与
    // `draft.marketplace_category_attrs[channel]` 在 UI 侧永远可安全展开。
    localized_copies: {},
    marketplace_category_attrs: {},
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

/** POST /v1/catalog/sourcing/drafts/{draft_id}/publish — 多平台一键刊登。
 *
 * `push_to_marketplace: true` 走真实平台 API（Shopify Admin GraphQL / Amazon SP-API / eBay Sell API / Alibaba IOP）；
 * 缺省 `false` 只写本地 SKU 映射，与既有「不夸大」口径一致。
 */
export async function publishSourcingDraft(
  draftId: string,
  body: SourcingPublishRequest,
): Promise<SourcingPublishResult[]> {
  const pushToMarketplace = body.push_to_marketplace === true;
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
        // 只有显式要求实时推送时，Mock 才声称平台已确认上线。
        listing_state: pushToMarketplace ? "live_published" : "mapping_ready",
        external_listing_confirmed: pushToMarketplace,
        publish_mode: pushToMarketplace ? "live_marketplace_api" : "local_sku_mapping",
      }),
    );
    const published: SourcingDraftRead = {
      ...draft,
      status: "published",
      // 本地 `status` 保留 `published`（既有徽标断言依赖「已刊登」子串）；
      // 只有真实推送成功才把 `listing_state` 提升为 `live_published`。
      listing_state: pushToMarketplace ? "live_published" : draft.listing_state,
      attributes:
        typeof body.initial_stock_quantity === "number"
          ? { ...draft.attributes, initial_stock_quantity: body.initial_stock_quantity }
          : draft.attributes,
      publish_history: [
        ...(draft.publish_history ?? []),
        ...results.map((result) => ({
          channel_type: result.channel_type,
          external_sku: result.external_sku,
          price_usd: result.price_usd,
          published_at: result.published_at,
          publish_mode: result.publish_mode,
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

/** POST /v1/catalog/products/{product_id}/publish — 主档商品直接多平台一键刊登。 */
export async function publishMasterProduct(
  productId: string,
  body: SourcingPublishRequest,
): Promise<SourcingPublishResult[]> {
  const pushToMarketplace = body.push_to_marketplace === true;
  if (shouldUseMockApi()) {
    const publishedAt = new Date().toISOString();
    return body.channel_account_ids.map((accountId, index) => ({
      channel_account_id: accountId,
      channel_type: mockStoreChannelType(accountId),
      external_sku: `MASTER-${index + 1}`,
      external_product_id: pushToMarketplace
        ? `ext-master-${productId}-${index + 1}`
        : `pending-listing:MASTER-${index + 1}`,
      price_usd: "19.90",
      stock_sync_ratio: "1.00",
      mapping_id: `mock-master-map-${index + 1}`,
      published_at: publishedAt,
      listing_state: pushToMarketplace ? "live_published" : "mapping_ready",
      external_listing_confirmed: pushToMarketplace,
      publish_mode: pushToMarketplace ? "live_marketplace_api" : "local_sku_mapping",
    }));
  }
  return bffFetch<SourcingPublishResult[]>(
    `/v1/catalog/products/${encodeURIComponent(productId)}/publish`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

/** POST /v1/catalog/media/upload — 本地图片直传统一 MinIO/R2 图床。 */
export async function uploadCatalogMedia(
  file: File,
  opts: {
    productId?: string;
    variantId?: string;
    sourcingDraftId?: string;
    altText?: string;
    sortOrder?: number;
  } = {},
): Promise<MediaAssetRead> {
  if (shouldUseMockApi()) {
    return {
      id: `mock-media-${Date.now()}`,
      tenant_id: "mock-tenant",
      product_id: opts.productId ?? null,
      variant_id: opts.variantId ?? null,
      sourcing_draft_id: opts.sourcingDraftId ?? null,
      storage_key: `mock/${file.name}`,
      source_url: null,
      sha256: "0".repeat(64),
      mime_type: file.type || "image/png",
      byte_size: file.size || 128,
      sort_order: opts.sortOrder ?? 0,
      alt_text: opts.altText ?? file.name,
      content_path: "/api/bff/v1/catalog/media/mock/content",
      presigned_url: null,
      created_at: new Date().toISOString(),
    };
  }
  const form = new FormData();
  form.append("file", file);
  if (opts.productId) form.append("product_id", opts.productId);
  if (opts.variantId) form.append("variant_id", opts.variantId);
  if (opts.sourcingDraftId) form.append("sourcing_draft_id", opts.sourcingDraftId);
  if (opts.altText) form.append("alt_text", opts.altText);
  if (opts.sortOrder !== undefined) form.append("sort_order", String(opts.sortOrder));

  const res = await fetch("/api/bff/v1/catalog/media/upload", {
    method: "POST",
    body: form,
    credentials: "include",
  });
  if (!res.ok) {
    const errText = await res.text();
    throw new Error(errText || `Upload failed with HTTP ${res.status}`);
  }
  return (await res.json()) as MediaAssetRead;
}

/** POST /v1/catalog/media/ingest-urls — 将公网图片外链转存至自有 MinIO/R2 图床。 */
export async function ingestCatalogMediaUrls(
  body: MediaUrlIngestRequest,
): Promise<MediaAssetRead[]> {
  if (shouldUseMockApi()) {
    return body.urls.map((url, idx) => ({
      id: `mock-url-media-${idx + 1}`,
      tenant_id: "mock-tenant",
      product_id: body.product_id ?? null,
      variant_id: body.variant_id ?? null,
      sourcing_draft_id: body.sourcing_draft_id ?? null,
      storage_key: `mock/url-${idx + 1}.jpg`,
      source_url: url,
      sha256: "1".repeat(64),
      mime_type: "image/jpeg",
      byte_size: 2048,
      sort_order: idx,
      alt_text: body.alt_text ?? "",
      content_path: "/api/bff/v1/catalog/media/mock/content",
      presigned_url: url,
      created_at: new Date().toISOString(),
    }));
  }
  return bffFetch<MediaAssetRead[]>("/v1/catalog/media/ingest-urls", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** GET /v1/catalog/media — 列出商品或采集草稿已挂载的图片资产。 */
export async function listCatalogMedia(
  opts: { productId?: string; sourcingDraftId?: string } = {},
): Promise<MediaAssetRead[]> {
  if (shouldUseMockApi()) return [];
  const params = new URLSearchParams();
  if (opts.productId) params.set("product_id", opts.productId);
  if (opts.sourcingDraftId) params.set("sourcing_draft_id", opts.sourcingDraftId);
  const qs = params.toString();
  return bffFetch<MediaAssetRead[]>(`/v1/catalog/media${qs ? `?${qs}` : ""}`, {
    method: "GET",
  });
}

// ---------------------------------------------------------------------------
// 平台订单回拉（Amazon SP-API / eBay Sell API / Alibaba IOP）
// ---------------------------------------------------------------------------

/** 订单回拉统计：扫到 / 找回 / 更新 / 失败，全部由后端确定性 SQL 返回。 */
export interface MarketplaceOrderSyncResult {
  channel_account_id: string;
  channel_type: string;
  scanned: number;
  recovered: number;
  updated: number;
  failed: number;
}

/** POST /v1/channels/marketplace/{channel_account_id}/sync-orders?lookback_hours=N。
 *
 * 走平台官方订单接口回拉最近 N 小时的订单，缺失的直接落 ERP，已存在的幂等更新。
 * Mock 模式返回一组固定统计，零后端也能点亮「拉单」按钮。
 */
export async function triggerMarketplaceOrderSync(
  channelAccountId: string,
  lookbackHours = 24,
): Promise<MarketplaceOrderSyncResult> {
  const hours =
    Number.isFinite(lookbackHours) && lookbackHours > 0 ? Math.trunc(lookbackHours) : 24;
  if (shouldUseMockApi()) {
    return {
      channel_account_id: channelAccountId,
      channel_type: "amazon_sp",
      scanned: 2,
      recovered: 1,
      updated: 1,
      failed: 0,
    };
  }
  return bffFetch<MarketplaceOrderSyncResult>(
    `/v1/channels/marketplace/${encodeURIComponent(channelAccountId)}/sync-orders?lookback_hours=${hours}`,
    { method: "POST" },
  );
}

// ---------------------------------------------------------------------------
// 刊登目标店铺展示
// ---------------------------------------------------------------------------

/** 可刊登的渠道类型（与后端 `_PUBLISHABLE_CHANNEL_TYPES` 对齐，共 16 个平台）。 */
export const SOURCING_PUBLISHABLE_CHANNEL_TYPES = [
  "shopify",
  "amazon_sp",
  "ebay",
  "alibaba_email",
  "tiktok_shop",
  "woocommerce",
  "shopline",
  "shoplazza",
  "shopee",
  "lazada",
  "temu",
  "shein",
  "aliexpress",
  "ozon",
  "walmart",
  "mercado_libre",
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
  shopee: "Shopee",
  lazada: "Lazada",
  temu: "Temu",
  shein: "SHEIN",
  aliexpress: "AliExpress",
  ozon: "Ozon",
  walmart: "Walmart",
  mercado_libre: "Mercado Libre",
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

/**
 * 演示店铺账号：mock 模式且租户尚未接入任何店铺时用于补齐刊登目标。
 *
 * 前 5 条顺序被 `pack-f-sourcing-channels-ui.test.tsx` 的演示回退断言锁住，
 * 新增的 8 个全球化平台放在 `MOCK_GLOBAL_MARKETPLACE_STORE_ACCOUNTS`，
 * 需要 13 个演示店铺时用 `MOCK_ALL_SOURCING_STORE_ACCOUNTS` /
 * `sourcingStoreAccountsWithGlobalMarketplaces()`，不影响既有 5 店铺 mock。
 */
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

/** 8 个全球化电商平台的演示店铺（刊登目标）。 */
export const MOCK_GLOBAL_MARKETPLACE_STORE_ACCOUNTS: ReadonlyArray<{
  id: string;
  channel_type: string;
  display_name: string;
}> = [
  { id: "ch-shopee-qzled", channel_type: "shopee", display_name: "QZ LED · Shopee SG" },
  { id: "ch-lazada-qzled", channel_type: "lazada", display_name: "QZ LED · Lazada SG" },
  { id: "ch-temu-qzled", channel_type: "temu", display_name: "QZ LED · Temu US" },
  { id: "ch-shein-qzled", channel_type: "shein", display_name: "QZ LED · SHEIN US" },
  { id: "ch-aliexpress-qzled", channel_type: "aliexpress", display_name: "QZ LED · AliExpress" },
  { id: "ch-ozon-qzled", channel_type: "ozon", display_name: "QZ LED · Ozon RU" },
  { id: "ch-walmart-qzled", channel_type: "walmart", display_name: "QZ LED · Walmart US" },
  {
    id: "ch-mercado-libre-qzled",
    channel_type: "mercado_libre",
    display_name: "QZ LED · Mercado Libre MX",
  },
];

/** 全部 13 个演示店铺：5 个既有店铺 + 8 个全球化平台（顺序稳定）。 */
export const MOCK_ALL_SOURCING_STORE_ACCOUNTS: ReadonlyArray<{
  id: string;
  channel_type: string;
  display_name: string;
}> = [...MOCK_SOURCING_STORE_ACCOUNTS, ...MOCK_GLOBAL_MARKETPLACE_STORE_ACCOUNTS];

function mockStoreChannelType(accountId: string): string {
  return (
    MOCK_ALL_SOURCING_STORE_ACCOUNTS.find((store) => store.id === accountId)?.channel_type ??
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

/**
 * 同 `sourcingStoreAccounts`，但 mock 回退补齐全部 13 个演示店铺
 * （含 8 个全球化电商平台），供已升级到 16 平台刊登口的页面使用。
 */
export function sourcingStoreAccountsWithGlobalMarketplaces(
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
  return shouldUseMockApi() ? MOCK_ALL_SOURCING_STORE_ACCOUNTS.map((store) => ({ ...store })) : [];
}

/** 草稿状态徽标：草稿 / 已入主档 / 本地映射就绪 / 已刊登（本地记录）/ 已上线刊登。
 *
 * 诚实原则：Mock 与「一键刊登」只写入本地 SKU 映射（`publish_history`），
 * 并不能证明平台侧已经上线，所以本地状态用「映射就绪（待平台确认）」而不是「已刊登」。
 * 真正由平台回执确认后才用 `live_published`。
 *
 * 注意：`published` 的 zh 必须保留「已刊登」子串，
 * 既有 `pack-f-sourcing-channels-ui.test.tsx` 断言使用了 `toContain("已刊登")`。
 */
export function sourcingStatusCopy(status: string): { zh: string; en: string; tone: string } {
  switch (status) {
    case "imported":
      return {
        zh: "已入主档",
        en: "Imported",
        tone: "border-sky-200 bg-sky-50 text-sky-800 dark:border-sky-800/70 dark:bg-sky-950/40 dark:text-sky-300",
      };
    case "mapping_ready":
      return {
        zh: "映射就绪（待平台确认）",
        en: "Mapping ready",
        tone: "border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-700/70 dark:bg-amber-950/40 dark:text-amber-200",
      };
    case "published":
      return {
        zh: "已刊登（本地记录）",
        en: "Published (local)",
        tone: "border-emerald-300 bg-emerald-50 text-emerald-800 dark:border-emerald-700/70 dark:bg-emerald-950/40 dark:text-emerald-300",
      };
    case "live_published":
      return {
        zh: "已上线刊登",
        en: "Live published",
        tone: "border-emerald-300 bg-emerald-100 text-emerald-900 ring-1 ring-emerald-200 dark:border-emerald-700/70 dark:bg-emerald-950/60 dark:text-emerald-200 dark:ring-emerald-800/60",
      };
    default:
      return {
        zh: "草稿",
        en: "Draft",
        tone: "border-slate-200 bg-slate-100 text-slate-700 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300",
      };
  }
}

// ---------------------------------------------------------------------------
// Phase 4: Multi-Source Browser Collector Extension Tokens
// ---------------------------------------------------------------------------

let mockCollectorTokens: CollectorTokenRead[] = [
  {
    id: "col-tok-mock-1",
    name: "Chrome 1688/淘宝选品插件",
    token_prefix: "erp_col_demo1234",
    scopes: ["sourcing:collect"],
    last_used_at: null,
    expires_at: null,
    revoked_at: null,
    created_at: "2026-11-23T08:00:00Z",
  },
];

export async function fetchCollectorTokens(): Promise<CollectorTokenRead[]> {
  if (shouldUseMockApi()) {
    return mockCollectorTokens.map((item) => ({ ...item }));
  }
  return bffFetch<CollectorTokenRead[]>("/v1/catalog/sourcing/collector-tokens", {
    method: "GET",
  });
}

export async function createCollectorToken(
  body: CollectorTokenCreateRequest,
): Promise<CollectorTokenCreateResponse> {
  if (shouldUseMockApi()) {
    const suffix = String(mockCollectorTokens.length + 1).padStart(2, "0");
    const plain = `erp_col_mock_secret_${suffix}_${Math.random().toString(36).slice(2, 10)}`;
    const token: CollectorTokenRead = {
      id: `col-tok-mock-${suffix}`,
      name: body.name.trim() || `Extension ${suffix}`,
      token_prefix: plain.slice(0, 16),
      scopes: ["sourcing:collect"],
      last_used_at: null,
      expires_at: null,
      revoked_at: null,
      created_at: new Date().toISOString(),
    };
    mockCollectorTokens = [token, ...mockCollectorTokens];
    return { token, plain_token: plain };
  }
  return bffFetch<CollectorTokenCreateResponse>("/v1/catalog/sourcing/collector-tokens", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function revokeCollectorToken(tokenId: string): Promise<CollectorTokenRead> {
  if (shouldUseMockApi()) {
    const now = new Date().toISOString();
    let updated: CollectorTokenRead | undefined;
    mockCollectorTokens = mockCollectorTokens.map((item) => {
      if (item.id !== tokenId) return item;
      updated = { ...item, revoked_at: item.revoked_at ?? now };
      return updated;
    });
    return (
      updated ?? {
        id: tokenId,
        name: "Revoked",
        token_prefix: "erp_col_mock",
        scopes: ["sourcing:collect"],
        last_used_at: null,
        expires_at: null,
        revoked_at: now,
        created_at: now,
      }
    );
  }
  return bffFetch<CollectorTokenRead>(
    `/v1/catalog/sourcing/collector-tokens/${encodeURIComponent(tokenId)}`,
    { method: "DELETE" },
  );
}

// ---------------------------------------------------------------------------
// AI 多语种改写 & 草稿编辑（对齐后端 `erp_core/catalog/sourcing_copy_protocol.py`）
//
// 后端把「平台 → 默认语言」和「12 种受支持语言」写死在
// `DEFAULT_LANGUAGE_BY_CHANNEL` / `SUPPORTED_REWRITE_LANGUAGES` 里，前端这份常量
// 是同一个契约的镜像 —— 换语言、换平台都不需要再问后端。
// 改写结果永远带回 `rewrite_mode`（`llm` 或 `deterministic_fallback`），
// UI 必须照实展示，绝不能在回退时声称调用过 LLM。
// ---------------------------------------------------------------------------

/** 下拉选项：`value` 是契约里的枚举值，`zh` / `en` 是双语（en 尽量用母语自称）。 */
export interface RewriteSelectOption {
  value: string;
  zh: string;
  en: string;
}

/** 12 种受支持语言，顺序与后端 `SUPPORTED_REWRITE_LANGUAGES` 一致。 */
export const REWRITE_LANGUAGE_OPTIONS: ReadonlyArray<RewriteSelectOption> = [
  { value: "en", zh: "英语", en: "English" },
  { value: "zh", zh: "中文", en: "中文" },
  { value: "es", zh: "西班牙语", en: "Español" },
  { value: "pt", zh: "葡萄牙语", en: "Português" },
  { value: "ru", zh: "俄语", en: "Русский" },
  { value: "th", zh: "泰语", en: "ไทย" },
  { value: "vi", zh: "越南语", en: "Tiếng Việt" },
  { value: "id", zh: "印尼语", en: "Bahasa Indonesia" },
  { value: "ms", zh: "马来语", en: "Bahasa Melayu" },
  { value: "fr", zh: "法语", en: "Français" },
  { value: "de", zh: "德语", en: "Deutsch" },
  { value: "ja", zh: "日语", en: "日本語" },
];

/** 4 种语气档位，与后端 `SourcingRewriteTone` 一致。 */
export const REWRITE_TONE_OPTIONS: ReadonlyArray<RewriteSelectOption> = [
  { value: "conversion", zh: "转化优先", en: "Conversion" },
  { value: "seo", zh: "搜索优化", en: "SEO" },
  { value: "b2b_wholesale", zh: "B2B 批发报价", en: "B2B wholesale" },
  { value: "social_viral", zh: "社媒爆款", en: "Social viral" },
];

/** 16 个刊登平台各自的默认目标语言（东南亚 / 俄罗斯 / 拉美本地化）。 */
export const DEFAULT_REWRITE_LANGUAGE_BY_CHANNEL: Readonly<
  Record<(typeof SOURCING_PUBLISHABLE_CHANNEL_TYPES)[number], string>
> = {
  shopify: "en",
  amazon_sp: "en",
  ebay: "en",
  walmart: "en",
  woocommerce: "en",
  shopline: "en",
  shoplazza: "en",
  alibaba_email: "en",
  tiktok_shop: "en",
  temu: "en",
  shein: "en",
  aliexpress: "en",
  shopee: "th",
  lazada: "ms",
  ozon: "ru",
  mercado_libre: "es",
};

export function rewriteLanguageOption(code: string): RewriteSelectOption | undefined {
  const normalized = code.trim().toLowerCase();
  return REWRITE_LANGUAGE_OPTIONS.find((option) => option.value === normalized);
}

export function rewriteToneOption(tone: string): RewriteSelectOption | undefined {
  const normalized = tone.trim().toLowerCase();
  return REWRITE_TONE_OPTIONS.find((option) => option.value === normalized);
}

/** 平台 → 默认语言；未知平台回退 `en`（绝不抛错，UI 永远能选中一项）。 */
export function defaultRewriteLanguageForChannel(channel: string): string {
  const normalized = channel.trim().toLowerCase();
  return (
    DEFAULT_REWRITE_LANGUAGE_BY_CHANNEL[
      normalized as keyof typeof DEFAULT_REWRITE_LANGUAGE_BY_CHANNEL
    ] ?? "en"
  );
}

/** 语言小标签（`th` → `TH`），用于 `shopee:th · Shopee (TH)` 这类芯片文案。 */
export function rewriteLanguageShortLabel(code: string): string {
  return code.trim().toUpperCase() || "EN";
}

const MOCK_REWRITE_BULLET_FALLBACK: ReadonlyArray<string> = [
  "Factory-direct supply with export-grade QC before shipment.",
  "OEM logo, packaging and plug-type customisation supported.",
  "Volume ladder pricing quoted per MOQ tier.",
  "7-day sample lead time, 18-day bulk production lead time.",
  "FOB Fuzhou with DDP door-to-door shipping available.",
];

/**
 * Mock 模式的「确定性本地规则」改写：真的按平台 / 语言 / 语气生成一版文案，
 * 但永远诚实地标 `deterministic_fallback` + `0 credits`。
 */
function mockDeterministicRewrite(
  draft: SourcingDraftRead,
  body: SourcingAiRewriteRequest,
): SourcingCopyRewriteOutput {
  const channel = (body.target_channel ?? "shopify").trim().toLowerCase() || "shopify";
  const language =
    (body.target_language ?? defaultRewriteLanguageForChannel(channel)).trim().toLowerCase() ||
    "en";
  const tone = body.tone ?? "conversion";
  const languageLabel = rewriteLanguageOption(language)?.en ?? rewriteLanguageShortLabel(language);
  const title = `${draft.optimized_title_en} | ${sourcingStoreLabel(channel)} ${languageLabel}`.slice(
    0,
    190,
  );
  const bullets = [...draft.bullet_points];
  while (bullets.length < 5) {
    bullets.push(MOCK_REWRITE_BULLET_FALLBACK[bullets.length % MOCK_REWRITE_BULLET_FALLBACK.length]);
  }
  const bullet_points = bullets.slice(0, 5) as [
    string,
    string,
    string,
    string,
    string,
  ];
  return {
    title,
    bullet_points,
    description: `${title}. ${bullet_points.join(" ")}`,
    target_channel: channel,
    target_language: language,
    tone,
    model_used: "deterministic-rule-v1",
    rewrite_mode: "deterministic_fallback",
    credits_consumed: "0.0000",
    fallback_reason: "mock_demo_mode",
    seo_keywords: [draft.suggested_category, sourcingStoreLabel(channel), language],
    generated_at: new Date().toISOString(),
  };
}

/**
 * POST /v1/catalog/sourcing/drafts/{draft_id}/ai-rewrite
 *
 * 把一个草稿改写成「某个平台 + 某种语言 + 某种语气」的刊登文案，并把结果写进
 * `localized_copies["<channel>:<language>"]`。`apply_to_draft` 为真时同时覆盖
 * 草稿的英文标题与 5 条卖点。
 */
export async function aiRewriteSourcingDraft(
  draftId: string,
  body: SourcingAiRewriteRequest,
): Promise<SourcingAiRewriteResponse> {
  if (shouldUseMockApi()) {
    const draft = mockDrafts.find((item) => item.id === draftId);
    if (!draft) {
      throw new Error(`Sourcing draft ${draftId} is not in the mock sourcing box.`);
    }
    const rewrite = mockDeterministicRewrite(draft, body);
    const copyKey = `${rewrite.target_channel}:${rewrite.target_language}`;
    const applyToDraft = body.apply_to_draft !== false;
    const updated: SourcingDraftRead = {
      ...draft,
      optimized_title_en: applyToDraft ? rewrite.title : draft.optimized_title_en,
      bullet_points: applyToDraft ? [...rewrite.bullet_points] : draft.bullet_points,
      localized_copies: {
        ...(draft.localized_copies ?? {}),
        [copyKey]: { ...rewrite, optimized_description: rewrite.description },
      },
      marketplace_category_attrs: draft.marketplace_category_attrs ?? {},
      updated_at: rewrite.generated_at,
    };
    mockDrafts = mockDrafts.map((item) => (item.id === draft.id ? updated : item));
    return { draft: updated, rewrite };
  }
  return bffFetch<SourcingAiRewriteResponse>(
    `/v1/catalog/sourcing/drafts/${encodeURIComponent(draftId)}/ai-rewrite`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

/**
 * PATCH /v1/catalog/sourcing/drafts/{draft_id}
 *
 * 部分更新：只有显式给出的字段会被应用。`marketplace_category_attrs` 按渠道合并
 * （后端是浅合并，所以前端只需要送当前渠道那一份）。
 */
export async function updateSourcingDraft(
  draftId: string,
  body: SourcingDraftUpdateRequest,
): Promise<SourcingDraftRead> {
  if (shouldUseMockApi()) {
    const draft = mockDrafts.find((item) => item.id === draftId);
    if (!draft) {
      throw new Error(`Sourcing draft ${draftId} is not in the mock sourcing box.`);
    }
    const updated: SourcingDraftRead = { ...draft };
    if (body.optimized_title_en != null) {
      const title = body.optimized_title_en.trim();
      if (title) updated.optimized_title_en = title.slice(0, 200);
    }
    if (body.bullet_points != null) {
      const bullets = body.bullet_points
        .map((item) => String(item).trim())
        .filter(Boolean)
        .slice(0, 5);
      if (bullets.length > 0) updated.bullet_points = bullets;
    }
    if (body.target_price_usd != null) {
      updated.target_price_usd = moneyString(
        decimalInput(body.target_price_usd, Number(draft.target_price_usd)),
      );
    }
    if (body.suggested_category != null) {
      updated.suggested_category = body.suggested_category.trim() || draft.suggested_category;
    }
    if (body.suggested_hs_code != null) {
      updated.suggested_hs_code = body.suggested_hs_code.trim() || null;
    }
    if (body.localized_copies != null) {
      updated.localized_copies = {
        ...(draft.localized_copies ?? {}),
        ...body.localized_copies,
      };
    }
    if (body.marketplace_category_attrs != null) {
      updated.marketplace_category_attrs = {
        ...(draft.marketplace_category_attrs ?? {}),
        ...body.marketplace_category_attrs,
      };
    }
    updated.updated_at = new Date().toISOString();
    mockDrafts = mockDrafts.map((item) => (item.id === draft.id ? updated : item));
    return updated;
  }
  return bffFetch<SourcingDraftRead>(
    `/v1/catalog/sourcing/drafts/${encodeURIComponent(draftId)}`,
    { method: "PATCH", body: JSON.stringify(body) },
  );
}
