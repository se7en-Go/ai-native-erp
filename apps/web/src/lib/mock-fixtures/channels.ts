// Structural mirrors of the channel settings shapes (app/settings/channels).

/**
 * 渠道应用市场接入结果的本地镜像：没有后端时演示 19 个渠道的完整接入闭环。
 * key 与 `MarketplaceChannelKey` 对齐，`externalAccountId` 由调用方按表单回填。
 *
 * 8 个全球化电商平台（Shopee / Lazada / Temu / SHEIN / AliExpress / Ozon /
 * Walmart / Mercado Libre）的条目与 `pack-f-sourcing.ts` 的
 * `GLOBAL_MARKETPLACE_CHANNEL_KEYS` 一一对应。
 */
export const mockChannelConnectResults: Record<
  | "whatsapp"
  | "telegram"
  | "messenger"
  | "instagram_dm"
  | "email"
  | "web_livechat"
  | "shopify"
  | "woocommerce"
  | "shopline"
  | "shoplazza"
  | "tiktok_shop"
  | "shopee"
  | "lazada"
  | "temu"
  | "shein"
  | "aliexpress"
  | "ozon"
  | "walmart"
  | "mercado_libre",
  { channel_account_id: string; external_account_id: string }
> = {
  whatsapp: {
    channel_account_id: "mock-whatsapp-account",
    external_account_id: "mock_phone_number_id",
  },
  telegram: { channel_account_id: "mock-telegram-account", external_account_id: "7012345678" },
  messenger: { channel_account_id: "mock-messenger-account", external_account_id: "1010101010" },
  instagram_dm: {
    channel_account_id: "mock-instagram-account",
    external_account_id: "17841400000000000",
  },
  email: { channel_account_id: "mock-email-account", external_account_id: "sales@qzled.example.com" },
  web_livechat: {
    channel_account_id: "mock-web-livechat-account",
    external_account_id: "sk_live_demo_qzled",
  },
  shopify: { channel_account_id: "mock-shopify-account", external_account_id: "qzled.myshopify.com" },
  woocommerce: {
    channel_account_id: "mock-woocommerce-account",
    external_account_id: "shop.qzled.example.com",
  },
  shopline: { channel_account_id: "mock-shopline-account", external_account_id: "qzled.myshopline.com" },
  shoplazza: {
    channel_account_id: "mock-shoplazza-account",
    external_account_id: "qzled.myshoplazza.com",
  },
  tiktok_shop: { channel_account_id: "mock-tiktok-account", external_account_id: "7480000000000000000" },
  shopee: { channel_account_id: "mock-shopee-account", external_account_id: "qzled_sg" },
  lazada: { channel_account_id: "mock-lazada-account", external_account_id: "qzled-lazada-sg" },
  temu: { channel_account_id: "mock-temu-account", external_account_id: "QZLED-MALL-01" },
  shein: { channel_account_id: "mock-shein-account", external_account_id: "qzled-shein-us" },
  aliexpress: { channel_account_id: "mock-aliexpress-account", external_account_id: "qzled_ae" },
  ozon: { channel_account_id: "mock-ozon-account", external_account_id: "qzled-ozon-ru" },
  walmart: { channel_account_id: "mock-walmart-account", external_account_id: "QZLED-WM-US" },
  mercado_libre: {
    channel_account_id: "mock-mercado-libre-account",
    external_account_id: "QZLED-ML-MX",
  },
};

/**
 * 8 个全球化电商平台的演示渠道账号：与 `GLOBAL_MARKETPLACE_CHANNEL_KEYS`
 * 顺序一致，让 mock 模式下的渠道列表 / 刊登目标直接看到全部新平台。
 */
export const MOCK_GLOBAL_MARKETPLACE_CHANNEL_ACCOUNTS = [
  {
    id: "ch-shopee-qzled",
    channel_type: "shopee",
    display_name: "QZ LED · Shopee SG",
    external_account_id: "qzled_sg",
    status: "active",
    rate_limit_profile: { mps: 20 },
    owner_user_id: null,
    owner_name: null,
    owner_email: null,
    visibility_scope: "shared",
    bot_username: null,
  },
  {
    id: "ch-lazada-qzled",
    channel_type: "lazada",
    display_name: "QZ LED · Lazada SG",
    external_account_id: "qzled-lazada-sg",
    status: "active",
    rate_limit_profile: { mps: 20 },
    owner_user_id: null,
    owner_name: null,
    owner_email: null,
    visibility_scope: "shared",
    bot_username: null,
  },
  {
    id: "ch-temu-qzled",
    channel_type: "temu",
    display_name: "QZ LED · Temu US",
    external_account_id: "QZLED-MALL-01",
    status: "active",
    rate_limit_profile: { mps: 20 },
    owner_user_id: null,
    owner_name: null,
    owner_email: null,
    visibility_scope: "shared",
    bot_username: null,
  },
  {
    id: "ch-shein-qzled",
    channel_type: "shein",
    display_name: "QZ LED · SHEIN US",
    external_account_id: "qzled-shein-us",
    status: "active",
    rate_limit_profile: { mps: 20 },
    owner_user_id: null,
    owner_name: null,
    owner_email: null,
    visibility_scope: "shared",
    bot_username: null,
  },
  {
    id: "ch-aliexpress-qzled",
    channel_type: "aliexpress",
    display_name: "QZ LED · AliExpress",
    external_account_id: "qzled_ae",
    status: "active",
    rate_limit_profile: { mps: 20 },
    owner_user_id: null,
    owner_name: null,
    owner_email: null,
    visibility_scope: "shared",
    bot_username: null,
  },
  {
    id: "ch-ozon-qzled",
    channel_type: "ozon",
    display_name: "QZ LED · Ozon RU",
    external_account_id: "qzled-ozon-ru",
    status: "active",
    rate_limit_profile: { mps: 20 },
    owner_user_id: null,
    owner_name: null,
    owner_email: null,
    visibility_scope: "shared",
    bot_username: null,
  },
  {
    id: "ch-walmart-qzled",
    channel_type: "walmart",
    display_name: "QZ LED · Walmart US",
    external_account_id: "QZLED-WM-US",
    status: "active",
    rate_limit_profile: { mps: 20 },
    owner_user_id: null,
    owner_name: null,
    owner_email: null,
    visibility_scope: "shared",
    bot_username: null,
  },
  {
    id: "ch-mercado-libre-qzled",
    channel_type: "mercado_libre",
    display_name: "QZ LED · Mercado Libre MX",
    external_account_id: "QZLED-ML-MX",
    status: "active",
    rate_limit_profile: { mps: 20 },
    owner_user_id: null,
    owner_name: null,
    owner_email: null,
    visibility_scope: "shared",
    bot_username: null,
  },
] as const;

export const mockChannelAccounts = {
  items: [
    {
      id: "ch-1",
      channel_type: "whatsapp_coexistence",
      display_name: "QZ LED WA Business (+86)",
      external_account_id: "waba_phone_1",
      status: "active",
      rate_limit_profile: { mps: 20, coexistence: true },
      owner_user_id: null,
      owner_name: null,
      owner_email: null,
      visibility_scope: "shared",
      bot_username: null,
    },
    {
      id: "ch-telegram-1",
      channel_type: "telegram",
      display_name: "QZ LED 外贸机器人",
      external_account_id: "7012345678",
      status: "active",
      rate_limit_profile: { mps: 30 },
      owner_user_id: "22222222-2222-2222-2222-2222222222a1",
      owner_name: "李明伦",
      owner_email: "sales.li@qzled.example.com",
      visibility_scope: "personal",
      bot_username: "qzled_sales_bot",
    },
  ],
  total: 2,
  limit: 20,
  offset: 0,
  has_more: false,
};

/**
 * 10 个渠道账号的合并镜像：2 个既有 IM 渠道 + 8 个全球化电商平台。
 *
 * 注意：`mockChannelAccounts` 必须继续只含非可刊登渠道（IM），
 * 否则 `sourcingStoreAccounts()` 的「无已接入店铺 → 补 5 个演示店铺」
 * 回退就不再生效，`pack-f-sourcing-channels-ui.test.tsx` 的刊登目标断言会碎。
 * 需要看到新平台的渠道列表请用本导出。
 */
export const mockChannelAccountsWithGlobalMarketplaces = {
  items: [...mockChannelAccounts.items, ...MOCK_GLOBAL_MARKETPLACE_CHANNEL_ACCOUNTS],
  total: mockChannelAccounts.total + MOCK_GLOBAL_MARKETPLACE_CHANNEL_ACCOUNTS.length,
  limit: mockChannelAccounts.limit,
  offset: mockChannelAccounts.offset,
  has_more: false,
};

/** 工作区成员（业务员账号）：GET /tenants/memberships 的镜像。 */
export const mockTeamMembers = {
  items: [
    {
      membership_id: "mem-owner-1",
      tenant_id: "tenant-qz-1",
      user_id: "22222222-2222-2222-2222-2222222222a0",
      email: "owner@qzled.example.com",
      full_name: "企业所有者",
      role: "owner",
      is_active: true,
      created_at: "2026-10-01T00:00:00Z",
    },
    {
      membership_id: "mem-agent-1",
      tenant_id: "tenant-qz-1",
      user_id: "22222222-2222-2222-2222-2222222222a1",
      email: "sales.li@qzled.example.com",
      full_name: "李明伦",
      role: "agent",
      is_active: true,
      created_at: "2026-10-08T00:00:00Z",
    },
  ],
  total: 2,
  limit: 100,
  offset: 0,
  has_more: false,
};

export const mockChannelDlq = {
  items: [
    {
      id: "dlq-1",
      channel_type: "whatsapp_coexistence",
      external_event_id: "wamid.fail.1",
      error_message: "131056 Pair rate limit exceeded",
      retry_count: 6,
      created_at: "2026-11-19T08:00:00Z",
    },
  ],
  total: 1,
  limit: 20,
  offset: 0,
  has_more: false,
};

export const mockChannelConsents = {
  items: [
    {
      id: "con-1",
      customer_name: "Elena Vance",
      channel_type: "whatsapp_marketing",
      consent_status: "opted_in",
      proof_source: "shopify_checkout_checkbox",
      recorded_at: "2026-11-19T07:00:00Z",
    },
  ],
  total: 1,
  limit: 20,
  offset: 0,
  has_more: false,
};

export const mockChannelSuppressions = {
  items: [
    {
      id: "sup-1",
      channel_type: "email",
      normalized_address: "unsubscribe-test@example.com",
      reason: "RFC 8058 One-Click Opt-Out",
      created_at: "2026-11-19T06:00:00Z",
    },
  ],
  total: 1,
  limit: 20,
  offset: 0,
  has_more: false,
};
