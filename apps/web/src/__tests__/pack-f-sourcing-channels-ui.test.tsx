// Pack F frontend: 三个新增外贸渠道（Alibaba.com 国际站 / Amazon / eBay）的接入
// 表单，以及 /catalog 顶部「主档 SKU 库 ↔ 1688 采集搬家 & 多平台刊登」闭环。
//
// 两层验证：
//   1. 纯 API 层：断言 `bffFetch` 打到契约端点、请求体字段名与 types.gen.ts 完全一致；
//   2. 页面/组件层：抽屉真实快捷配置表单 + 采集箱 → 入主档 → 多平台刊登的完整交互。
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import CatalogPage from "@/app/catalog/page";
import { ChannelConnectDrawer } from "@/components/channels/channel-connect-drawer";
import { ChannelRail } from "@/components/inbox/channel-rail";
import {
  CHANNEL_MARKETPLACE,
  marketplaceChannel,
  marketplaceKeyForChannelFilter,
  visibleMarketplaceItems,
} from "@/components/channels/channel-marketplace";
import { AuthSessionContext, type AuthSessionState } from "@/components/providers";
import { I18nProvider } from "@/lib/i18n";
import type { Principal } from "@/lib/contracts";
import type {
  AlibabaIntlConnectRequest,
  AmazonConnectRequest,
  EbayConnectRequest,
  SourcingCollectRequest,
  SourcingDraftRead,
} from "@/lib/api-client/types.gen";
import {
  SOURCING_DEFAULT_FX_RATE,
  SOURCING_DEFAULT_MARGIN_PCT,
  collectSourcingDraft,
  connectAlibabaIntl,
  connectAmazon,
  connectEbay,
  costAndTargetPrice,
  moqTiersFromTargetPrice,
  planSourcingDraft,
  publishSourcingDraft,
  sourcingStoreAccounts,
} from "@/lib/api-client/pack-f-sourcing";

vi.mock("@/hooks/use-realtime-inbox", () => ({ useRealtimeInbox: vi.fn() }));

const BFF = "/api/bff";

interface CapturedCall {
  url: string;
  method: string | undefined;
  bodyText: string | null;
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function stubFetch(handler: (call: CapturedCall) => Response) {
  const captured: CapturedCall[] = [];
  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const call: CapturedCall =
      input instanceof Request
        ? { url: input.url, method: input.method, bodyText: await input.text() }
        : {
            url: String(input),
            method: init?.method,
            bodyText: typeof init?.body === "string" ? init.body : null,
          };
    captured.push(call);
    return handler(call);
  });
  vi.stubGlobal("fetch", mock);
  return captured;
}

function principal(role: Principal["role"]): Principal {
  return {
    user_id: "11111111-1111-1111-1111-111111111111",
    tenant_id: "22222222-2222-2222-2222-222222222222",
    role,
    email: `${role}@qzled.example.com`,
    tenant_slug: "qz-led-factory",
    onboarding_stage: "LiveProduction",
  };
}

function renderWithProviders(ui: React.ReactNode, role: Principal["role"], locale: "zh" | "en") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const authValue: AuthSessionState = {
    principal: principal(role),
    isLoading: false,
    logout: vi.fn(async () => {}),
    refreshSession: vi.fn(async () => {}),
  };
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthSessionContext.Provider value={authValue}>
        <I18nProvider defaultLocale={locale}>{ui}</I18nProvider>
      </AuthSessionContext.Provider>
    </QueryClientProvider>
  );
}

// ---------------------------------------------------------------------------
// 1. Pack F API 层：契约端点与请求体字段
// ---------------------------------------------------------------------------

describe("pack-f sourcing connect endpoints", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("posts the Alibaba.com international connect body to its own endpoint", async () => {
    const captured = stubFetch(() =>
      jsonResponse({
        channel_account_id: "acc-alibaba-1",
        channel_type: "alibaba_email",
        credential_type: "api_key",
        display_name: "QZ LED 国际站",
        external_account_id: "qzled",
        status: "active",
        visibility_scope: "shared",
      }, 201),
    );

    const body: AlibabaIntlConnectRequest = {
      app_key: "alibaba-app-key",
      app_secret: "alibaba-app-secret",
      display_name: "QZ LED 国际站",
      inquiry_email: "sales@company.com",
      store_id: "qzled",
      visibility_scope: "shared",
    };
    const result = await connectAlibabaIntl(body);

    expect(captured[0].url).toBe(`${BFF}/v1/channels/alibaba-intl/connect`);
    expect(captured[0].method).toBe("POST");
    expect(JSON.parse(captured[0].bodyText ?? "{}")).toEqual(body);
    expect(result.channel_account_id).toBe("acc-alibaba-1");
    expect(result.external_account_id).toBe("qzled");
    expect(result.visibility_scope).toBe("shared");
  });

  it("posts the Amazon LWA body and never echoes the secret back", async () => {
    const captured = stubFetch(() =>
      jsonResponse(
        {
          channel_account_id: "acc-amazon-1",
          channel_type: "amazon_sp",
          credential_type: "oauth",
          display_name: "QZ LED Amazon US",
          external_account_id: "A2EUQ1WTGCTBG2",
          status: "active",
          visibility_scope: "shared",
        },
        201,
      ),
    );

    const body: AmazonConnectRequest = {
      display_name: "QZ LED Amazon US",
      lwa_client_id: "amzn1.application-oa2-client.demo",
      lwa_client_secret: "amzn-lwa-secret",
      marketplace_id: "ATVPDKIKX0DER",
      refresh_token: "Atzr|IwEBdemo",
      seller_id: "A2EUQ1WTGCTBG2",
    };
    const result = await connectAmazon(body);

    expect(captured[0].url).toBe(`${BFF}/v1/channels/amazon/connect`);
    expect(JSON.parse(captured[0].bodyText ?? "{}")).toEqual(body);
    expect(result.external_account_id).toBe("A2EUQ1WTGCTBG2");
    expect(JSON.stringify(result)).not.toContain("amzn-lwa-secret");
    expect(JSON.stringify(result)).not.toContain("Atzr|IwEBdemo");
  });

  it("posts the eBay app/cert body to the eBay connect endpoint", async () => {
    const captured = stubFetch(() =>
      jsonResponse(
        {
          channel_account_id: "acc-ebay-1",
          channel_type: "ebay",
          credential_type: "oauth",
          display_name: "qzled_official",
          external_account_id: "qzled_official",
          status: "active",
          visibility_scope: "shared",
        },
        201,
      ),
    );

    const body: EbayConnectRequest = {
      app_id: "QzLedERP-App-PRD-demo",
      cert_id: "ebay-cert-id",
      display_name: "qzled_official",
      refresh_token: "v^1.1#i^1#demo",
      seller_username: "qzled_official",
      site_id: "EBAY-US",
    };
    const result = await connectEbay(body);

    expect(captured[0].url).toBe(`${BFF}/v1/channels/ebay/connect`);
    expect(JSON.parse(captured[0].bodyText ?? "{}")).toEqual(body);
    expect(result.channel_account_id).toBe("acc-ebay-1");
  });
});

describe("pack-f sourcing collect → import → publish transport", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  const DRAFT: SourcingDraftRead = planSourcingDraft(
    {
      source_platform: "1688",
      source_url: "https://detail.1688.com/offer/680001234567.html",
      raw_title: "COB LED Strip 24V 5m",
      raw_price_cny: "18.60",
      fx_rate_usd_cny: SOURCING_DEFAULT_FX_RATE,
      target_margin_pct: SOURCING_DEFAULT_MARGIN_PCT,
      category: "LED Strip Lighting",
    },
    { id: "11111111-2222-3333-4444-555555555555", now: "2026-02-11T02:20:00.000Z" },
  );

  it("collects a 1688 link into a deterministic USD draft", async () => {
    const captured = stubFetch(() => jsonResponse(DRAFT, 201));
    const body: SourcingCollectRequest = {
      source_platform: "1688",
      source_url: "https://detail.1688.com/offer/680001234567.html",
      raw_title: "COB LED Strip 24V 5m",
      raw_price_cny: "18.60",
      fx_rate_usd_cny: SOURCING_DEFAULT_FX_RATE,
      target_margin_pct: SOURCING_DEFAULT_MARGIN_PCT,
    };
    const draft = await collectSourcingDraft(body);

    expect(captured[0].url).toBe(`${BFF}/v1/catalog/sourcing/collect`);
    expect(captured[0].method).toBe("POST");
    expect(JSON.parse(captured[0].bodyText ?? "{}")).toEqual(body);
    expect(draft.optimized_title_en).toContain("OEM Factory Direct Wholesale");
    expect(draft.bullet_points).toHaveLength(5);
    expect(draft.moq_tiers).toHaveLength(3);
  });

  it("recomputes CNY → USD cost and the tier ladder deterministically", () => {
    const pricing = costAndTargetPrice({
      raw_price_cny: 18.6,
      fx_rate_usd_cny: 7.2,
      target_margin_pct: 35,
    });
    expect(pricing.cost_usd).toBe("2.58");
    expect(pricing.target_price_usd).toBe("3.97");
    expect(moqTiersFromTargetPrice(pricing.target_price_usd)).toEqual([
      { min_qty: 100, unit_price_usd: "3.97" },
      { min_qty: 500, unit_price_usd: "3.77" },
      { min_qty: 2000, unit_price_usd: "3.57" },
    ]);
  });

  it("publishes a draft to the selected channel accounts", async () => {
    const captured = stubFetch(() =>
      jsonResponse([
        {
          channel_account_id: "ch-shopify-qzled",
          channel_type: "shopify",
          external_sku: "SHOPIFY-SRC-COB",
          external_product_id: "gid://shopify/Product/1",
          price_usd: "4.37",
          stock_sync_ratio: "1.00",
          mapping_id: "map-1",
          published_at: "2026-02-11T03:00:00.000Z",
        },
      ]),
    );

    const results = await publishSourcingDraft(DRAFT.id, {
      channel_account_ids: ["ch-shopify-qzled"],
      price_multiplier: "1.10",
      stock_sync_ratio: "1.00",
    });

    expect(captured[0].url).toBe(`${BFF}/v1/catalog/sourcing/drafts/${DRAFT.id}/publish`);
    expect(captured[0].method).toBe("POST");
    expect(JSON.parse(captured[0].bodyText ?? "{}")).toEqual({
      channel_account_ids: ["ch-shopify-qzled"],
      price_multiplier: "1.10",
      stock_sync_ratio: "1.00",
    });
    expect(results[0].external_sku).toBe("SHOPIFY-SRC-COB");
  });

  it("falls back to demo store accounts when nothing publishable is connected yet", () => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
    const demo = sourcingStoreAccounts([
      { id: "ch-wa-1", channel_type: "whatsapp_cloud", display_name: "WhatsApp" },
    ]);
    expect(demo.map((store) => store.channel_type)).toEqual([
      "shopify",
      "amazon_sp",
      "ebay",
      "alibaba_email",
      "tiktok_shop",
    ]);

    const real = sourcingStoreAccounts([
      { id: "ch-shopify-1", channel_type: "shopify", display_name: "qzled.myshopify.com" },
    ]);
    expect(real).toEqual([
      { id: "ch-shopify-1", channel_type: "shopify", display_name: "qzled.myshopify.com" },
    ]);
  });
});

// ---------------------------------------------------------------------------
// 2. 渠道应用市场 / Inbox 药丸条 / 快捷绑定抽屉
// ---------------------------------------------------------------------------

describe("3 new foreign-trade channels in the marketplace & inbox rail", () => {
  it("grows the marketplace to 14 channels with the 3 new keys", () => {
    expect(CHANNEL_MARKETPLACE).toHaveLength(14);
    expect(visibleMarketplaceItems(true)).toHaveLength(14);

    const alibaba = marketplaceChannel("alibaba_intl");
    expect(alibaba.channelTypes).toEqual(["alibaba_email"]);
    expect(alibaba.titleZh).toContain("Alibaba");
    expect(marketplaceChannel("amazon").channelTypes).toEqual(["amazon_sp"]);
    expect(marketplaceChannel("ebay").channelTypes).toEqual(["ebay"]);

    // 归属说明：三个外贸渠道都挂在「独立站与电商」分类下。
    expect(alibaba.category).toBe("commerce");
    expect(marketplaceChannel("amazon").category).toBe("commerce");
    expect(marketplaceChannel("ebay").category).toBe("commerce");
  });

  it("maps the new inbox filters to their own marketplace drawers", () => {
    expect(marketplaceKeyForChannelFilter("alibaba_email")).toBe("alibaba_intl");
    expect(marketplaceKeyForChannelFilter("amazon_sp")).toBe("amazon");
    expect(marketplaceKeyForChannelFilter("ebay")).toBe("ebay");
    // 传统工作邮箱仍然映射到邮箱抽屉，不会被国际站抢走。
    expect(marketplaceKeyForChannelFilter("email")).toBe("email");
  });

  it("renders the 3 new filter pills in both rail variants", () => {
    const { unmount } = renderWithProviders(
      <ChannelRail
        variant="compact"
        selectedChannel="alibaba_email"
        onSelectChannel={vi.fn()}
        unreadByChannel={{}}
      />,
      "owner",
      "zh",
    );
    for (const label of ["阿里国际站", "Amazon", "eBay"]) {
      expect(screen.getByRole("button", { name: new RegExp(label, "i") })).toBeInTheDocument();
    }
    expect(
      screen.getByTestId("inbox-channel-settings-link").getAttribute("href"),
    ).toBe("/settings/channels?connect=alibaba_intl");
    unmount();

    renderWithProviders(
      <ChannelRail
        selectedChannel="amazon_sp"
        onSelectChannel={vi.fn()}
        unreadByChannel={{ amazon_sp: 3 }}
      />,
      "owner",
      "en",
    );
    expect(screen.getByRole("button", { name: /amazon/i })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(
      screen.getByTestId("inbox-channel-settings-link").getAttribute("href"),
    ).toBe("/settings/channels?connect=amazon");
  });

  it("connects Amazon from the drawer with encrypted direct-connect fields", async () => {
    const captured = stubFetch(() =>
      jsonResponse(
        {
          channel_account_id: "acc-amazon-9",
          channel_type: "amazon_sp",
          credential_type: "oauth",
          display_name: "QZ LED Amazon US",
          external_account_id: "A2EUQ1WTGCTBG2",
          status: "active",
          visibility_scope: "shared",
        },
        201,
      ),
    );

    renderWithProviders(
      <ChannelConnectDrawer
        channelKey="amazon"
        accounts={[]}
        isAdmin
        currentUserId="user-owner-1"
        members={[]}
        tenantSlug="qz-led-factory"
        onSubmit={vi.fn()}
        onClose={vi.fn()}
        onViewAccounts={vi.fn()}
      />,
      "owner",
      "en",
    );

    expect(screen.getByTestId("connect-form-amazon")).toBeInTheDocument();
    expect(screen.getByTestId("connect-amazon-btn")).toBeDisabled();

    fireEvent.change(screen.getByTestId("amazon-seller-id-input"), {
      target: { value: "A2EUQ1WTGCTBG2" },
    });
    fireEvent.change(screen.getByTestId("amazon-marketplace-id-input"), {
      target: { value: "ATVPDKIKX0DER" },
    });
    fireEvent.change(screen.getByTestId("amazon-lwa-client-id"), {
      target: { value: "amzn1.application-oa2-client.demo" },
    });
    fireEvent.change(screen.getByTestId("amazon-lwa-client-secret"), {
      target: { value: "amzn-lwa-secret" },
    });
    fireEvent.change(screen.getByTestId("amazon-refresh-token"), {
      target: { value: "Atzr|IwEBdemo" },
    });
    fireEvent.change(screen.getByTestId("amazon-display-name-input"), {
      target: { value: "QZ LED Amazon US" },
    });
    fireEvent.click(screen.getByTestId("connect-amazon-btn"));

    await waitFor(() => {
      expect(captured.some((call) => call.url === `${BFF}/v1/channels/amazon/connect`)).toBe(true);
    });
    const call = captured.find((item) => item.url === `${BFF}/v1/channels/amazon/connect`);
    expect(JSON.parse(call?.bodyText ?? "{}")).toEqual({
      display_name: "QZ LED Amazon US",
      seller_id: "A2EUQ1WTGCTBG2",
      marketplace_id: "ATVPDKIKX0DER",
      lwa_client_id: "amzn1.application-oa2-client.demo",
      lwa_client_secret: "amzn-lwa-secret",
      refresh_token: "Atzr|IwEBdemo",
    });

    const success = await screen.findByTestId("connect-success-amazon");
    expect(success).toHaveTextContent("A2EUQ1WTGCTBG2");
    // 密钥绝不回显：成功面板里不允许出现任何 Secret / Refresh Token 明文。
    expect(success.textContent ?? "").not.toContain("amzn-lwa-secret");
    expect(success.textContent ?? "").not.toContain("Atzr|IwEBdemo");
    expect(document.querySelector('input[type="password"]')).not.toBeNull();
  });

  it("connects Alibaba.com with App Key/Secret + inquiry mailbox", async () => {
    const captured = stubFetch(() =>
      jsonResponse(
        {
          channel_account_id: "acc-alibaba-9",
          channel_type: "alibaba_email",
          credential_type: "api_key",
          display_name: "QZ LED 国际站",
          external_account_id: "qzled",
          status: "active",
          visibility_scope: "shared",
        },
        201,
      ),
    );

    renderWithProviders(
      <ChannelConnectDrawer
        channelKey="alibaba_intl"
        accounts={[]}
        isAdmin
        currentUserId="user-owner-1"
        members={[]}
        tenantSlug="qz-led-factory"
        onSubmit={vi.fn()}
        onClose={vi.fn()}
        onViewAccounts={vi.fn()}
      />,
      "owner",
      "en",
    );

    fireEvent.change(screen.getByTestId("alibaba-app-key-input"), {
      target: { value: "alibaba-app-key" },
    });
    fireEvent.change(screen.getByTestId("alibaba-app-secret-input"), {
      target: { value: "alibaba-app-secret" },
    });
    fireEvent.change(screen.getByTestId("alibaba-store-id-input"), {
      target: { value: "qzled" },
    });
    fireEvent.change(screen.getByTestId("alibaba-inquiry-email-input"), {
      target: { value: "sales@company.com" },
    });
    fireEvent.change(screen.getByTestId("alibaba_intl-display-name-input"), {
      target: { value: "QZ LED 国际站" },
    });
    fireEvent.click(screen.getByTestId("alibaba-scope-shared"));
    fireEvent.click(screen.getByTestId("connect-alibaba_intl-btn"));

    await waitFor(() => {
      expect(
        captured.some((call) => call.url === `${BFF}/v1/channels/alibaba-intl/connect`),
      ).toBe(true);
    });
    const call = captured.find(
      (item) => item.url === `${BFF}/v1/channels/alibaba-intl/connect`,
    );
    expect(JSON.parse(call?.bodyText ?? "{}")).toEqual({
      app_key: "alibaba-app-key",
      app_secret: "alibaba-app-secret",
      display_name: "QZ LED 国际站",
      store_id: "qzled",
      inquiry_email: "sales@company.com",
      visibility_scope: "shared",
    });
    expect((await screen.findByTestId("connect-success-alibaba_intl")).textContent ?? "").not.toContain(
      "alibaba-app-secret",
    );
  });

  it("connects eBay with seller username, site id, app/cert and refresh token", async () => {
    const captured = stubFetch(() =>
      jsonResponse(
        {
          channel_account_id: "acc-ebay-9",
          channel_type: "ebay",
          credential_type: "oauth",
          display_name: "qzled_official",
          external_account_id: "qzled_official",
          status: "active",
          visibility_scope: "shared",
        },
        201,
      ),
    );

    renderWithProviders(
      <ChannelConnectDrawer
        channelKey="ebay"
        accounts={[]}
        isAdmin
        currentUserId="user-owner-1"
        members={[]}
        tenantSlug="qz-led-factory"
        onSubmit={vi.fn()}
        onClose={vi.fn()}
        onViewAccounts={vi.fn()}
      />,
      "owner",
      "en",
    );

    fireEvent.change(screen.getByTestId("ebay-seller-username-input"), {
      target: { value: "qzled_official" },
    });
    fireEvent.change(screen.getByTestId("ebay-site-id-input"), {
      target: { value: "EBAY-US" },
    });
    fireEvent.change(screen.getByTestId("ebay-app-id-input"), {
      target: { value: "QzLedERP-App-PRD-demo" },
    });
    fireEvent.change(screen.getByTestId("ebay-cert-id-input"), {
      target: { value: "ebay-cert-id" },
    });
    fireEvent.change(screen.getByTestId("ebay-refresh-token"), {
      target: { value: "v^1.1#i^1#demo" },
    });
    fireEvent.click(screen.getByTestId("connect-ebay-btn"));

    await waitFor(() => {
      expect(captured.some((call) => call.url === `${BFF}/v1/channels/ebay/connect`)).toBe(true);
    });
    const call = captured.find((item) => item.url === `${BFF}/v1/channels/ebay/connect`);
    expect(JSON.parse(call?.bodyText ?? "{}")).toEqual({
      display_name: "eBay",
      seller_username: "qzled_official",
      site_id: "EBAY-US",
      app_id: "QzLedERP-App-PRD-demo",
      cert_id: "ebay-cert-id",
      refresh_token: "v^1.1#i^1#demo",
    });
  });
});

// ---------------------------------------------------------------------------
// 3. /catalog 采集搬家 & 多平台刊登闭环
// ---------------------------------------------------------------------------

describe("catalog sourcing workspace", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("keeps the master SKU library as the default view", async () => {
    renderWithProviders(<CatalogPage />, "manager", "zh");

    const master = await screen.findByTestId("catalog-tab-master");
    expect(master).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("catalog-tab-sourcing")).toBeInTheDocument();
    // 默认视图仍是主档表格，零回归。
    await waitFor(() => expect(screen.getByText("LED-COB-24V-5M")).toBeInTheDocument());
    expect(screen.queryByTestId("sourcing-workspace")).toBeNull();
  });

  it("switches to the sourcing tab and collects a 1688 link into an English draft", async () => {
    renderWithProviders(<CatalogPage />, "manager", "zh");

    fireEvent.click(await screen.findByTestId("catalog-tab-sourcing"));
    expect(await screen.findByTestId("sourcing-workspace")).toBeInTheDocument();
    expect(screen.getByTestId("sourcing-collect-form")).toBeInTheDocument();
    // 采集箱既有 mock 草稿立刻可见。
    expect(await screen.findByTestId("sourcing-draft-src-draft-1")).toBeInTheDocument();

    fireEvent.change(screen.getByTestId("sourcing-source-url"), {
      target: { value: "https://detail.1688.com/offer/680001234567.html" },
    });
    fireEvent.change(screen.getByTestId("sourcing-raw-title"), {
      target: { value: "COB 灯带 24V 5m 4000K" },
    });
    fireEvent.change(screen.getByTestId("sourcing-raw-price-cny"), {
      target: { value: "18.60" },
    });
    fireEvent.change(screen.getByTestId("sourcing-target-margin"), {
      target: { value: "35" },
    });
    fireEvent.change(screen.getByTestId("sourcing-fx-rate"), {
      target: { value: "7.20" },
    });
    fireEvent.click(screen.getByTestId("sourcing-collect-submit"));

    const success = await screen.findByTestId("sourcing-collect-success");
    expect(success.textContent).toContain("Factory Direct");

    // 新草稿卡片：英文 SEO 标题 + 5 条卖点 + USD 零售价 + 3 档 MOQ 阶梯价。
    const fresh = await screen.findByTestId("sourcing-draft-src-draft-mock-4");
    expect(fresh.textContent).toContain("Factory Direct");
    expect(within(fresh).getAllByRole("listitem")).toHaveLength(5);
    expect(fresh.textContent).toContain("3.97");
    for (const tier of ["100+", "500+", "2000+"]) {
      expect(fresh.textContent).toContain(tier);
    }
    expect(within(fresh).getByTestId("sourcing-draft-status-src-draft-mock-4").textContent).toContain(
      "草稿",
    );
  });

  it("rejects an invalid collect form without calling the API", async () => {
    renderWithProviders(<CatalogPage />, "manager", "zh");
    fireEvent.click(await screen.findByTestId("catalog-tab-sourcing"));

    fireEvent.change(screen.getByTestId("sourcing-source-url"), {
      target: { value: "detail.1688.com/offer/1.html" },
    });
    fireEvent.click(screen.getByTestId("sourcing-collect-submit"));

    const error = await screen.findByTestId("sourcing-collect-error");
    expect(error.textContent).toContain("http://");
    expect(screen.queryByTestId("sourcing-collect-success")).toBeNull();
  });

  it("imports a draft into the master SKU library in one click", async () => {
    renderWithProviders(<CatalogPage />, "manager", "zh");
    fireEvent.click(await screen.findByTestId("catalog-tab-sourcing"));

    const card = await screen.findByTestId("sourcing-draft-src-draft-1");
    expect(within(card).getByTestId("sourcing-draft-status-src-draft-1").textContent).toContain(
      "草稿",
    );
    fireEvent.click(within(card).getByTestId("sourcing-import-src-draft-1"));

    await waitFor(() => {
      expect(
        screen.getByTestId("sourcing-draft-status-src-draft-1").textContent,
      ).toContain("已入主档");
    });
    expect(screen.getByTestId("sourcing-draft-src-draft-1").textContent).toContain(
      "已入主档，可直接刊登",
    );
  });

  it("publishes a draft to multiple connected stores and lists every external SKU", async () => {
    renderWithProviders(<CatalogPage />, "manager", "zh");
    fireEvent.click(await screen.findByTestId("catalog-tab-sourcing"));

    const card = await screen.findByTestId("sourcing-draft-src-draft-2");
    fireEvent.click(within(card).getByTestId("sourcing-publish-src-draft-2"));

    const panel = await screen.findByTestId("sourcing-publish-panel-src-draft-2");
    expect(panel.textContent).toContain("Shopify");
    expect(panel.textContent).toContain("Amazon");
    expect(panel.textContent).toContain("eBay");

    fireEvent.change(screen.getByTestId("sourcing-publish-multiplier-src-draft-2"), {
      target: { value: "1.20" },
    });
    fireEvent.click(screen.getByTestId("sourcing-publish-check-ch-shopify-qzled"));
    fireEvent.click(screen.getByTestId("sourcing-publish-check-ch-amazon-qzled"));
    fireEvent.click(screen.getByTestId("sourcing-publish-confirm-src-draft-2"));

    const results = await screen.findByTestId("sourcing-publish-results-src-draft-2");
    expect(within(results).getAllByRole("listitem")).toHaveLength(2);
    expect(results.textContent).toContain("Shopify");
    expect(results.textContent).toContain("Amazon");
    expect(screen.getByTestId("sourcing-draft-status-src-draft-2").textContent).toContain(
      "已刊登",
    );
  });

  it("lets read-only roles browse the sourcing box but not collect", async () => {
    renderWithProviders(<CatalogPage />, "agent", "zh");
    fireEvent.click(await screen.findByTestId("catalog-tab-sourcing"));

    expect(await screen.findByTestId("sourcing-readonly-note")).toBeInTheDocument();
    expect(screen.queryByTestId("sourcing-collect-submit")).toBeNull();
    expect(await screen.findByTestId("sourcing-draft-src-draft-1")).toBeInTheDocument();
  });
});
