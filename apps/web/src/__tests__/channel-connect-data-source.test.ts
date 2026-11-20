import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

import {
  connectMarketplaceChannel,
  connectShopifyCustomApp,
  connectShopline,
  connectShoplazza,
  connectTikTokShop,
  connectWebLivechat,
  connectWhatsAppDirect,
  connectWhatsAppEmbedded,
  connectWooCommerce,
} from "@/lib/data-source";
import { fetchCallMethod, fetchCallUrl } from "@/__tests__/fetch-stub";

/**
 * 数据层双模式：mock 模式下 11 个渠道都能走通（演示闭环），
 * 线上模式必须打到锁定的 BFF 端点。
 */
describe("channel marketplace data source", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("returns local mock results for every channel when NEXT_PUBLIC_USE_MOCK_API=true", async () => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");

    const whatsapp = await connectWhatsAppDirect({
      waba_id: "waba-1",
      phone_number_id: "phone-1",
      access_token: "EAAG-token",
      is_coexistence: true,
      visibility_scope: "personal",
      owner_user_id: "u-1",
    });
    expect(whatsapp).toMatchObject({
      channel_account_id: "mock-whatsapp-account",
      external_account_id: "phone-1",
      visibility_scope: "personal",
      owner_user_id: "u-1",
    });

    const embedded = await connectWhatsAppEmbedded({
      code: "AQD-code",
      waba_id: "waba-1",
      phone_number_id: "phone-2",
      is_coexistence: true,
      display_name: "QZ LED WhatsApp",
    });
    expect(embedded.external_account_id).toBe("phone-2");

    const livechat = await connectWebLivechat({ site_domain: "https://shop.example.com" });
    expect(livechat.channel_account_id).toBe("mock-web-livechat-account");
    expect(livechat.external_account_id).toBe("sk_live_demo_qzled");

    const shopify = await connectShopifyCustomApp({
      shop_domain: "qzled.myshopify.com",
      client_id: "cid",
      client_secret: "secret",
    });
    expect(shopify.external_account_id).toBe("qzled.myshopify.com");

    const woo = await connectWooCommerce({
      store_url: "https://shop.qzled.example.com",
      consumer_key: "ck_1",
      consumer_secret: "cs_1",
    });
    expect(woo.channel_account_id).toBe("mock-woocommerce-account");

    const shopline = await connectShopline({
      store_domain: "qzled.myshopline.com",
      access_token: "sl-token",
      app_secret: "sl-secret",
    });
    expect(shopline.external_account_id).toBe("qzled.myshopline.com");

    const shoplazza = await connectShoplazza({
      store_domain: "qzled.myshoplazza.com",
      access_token: "sp-token",
      client_secret: "sp-secret",
    });
    expect(shoplazza.external_account_id).toBe("qzled.myshoplazza.com");

    const tiktok = await connectTikTokShop({
      shop_id: "7480000000000000000",
      shop_cipher: "cipher",
      app_key: "tt-key",
      app_secret: "tt-secret",
      access_token: "tt-token",
      app_mode: "seller_byok",
    });
    expect(tiktok.external_account_id).toBe("7480000000000000000");

    // 抽屉统一入口同样走 mock，不再触发任何网络请求。
    const viaDispatcher = await connectMarketplaceChannel({
      channel: "shopify",
      body: {
        shop_domain: "other.myshopify.com",
        client_id: "cid",
        client_secret: "secret",
      },
    });
    expect(viaDispatcher.external_account_id).toBe("other.myshopify.com");
  });

  it("hits the real BFF endpoint when mock mode is off", async () => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "false");
    const fetchMock = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            channel_account_id: "acc-live",
            external_account_id: "sk_live_real",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await connectWebLivechat({ site_domain: "https://shop.example.com" });
    expect(result.external_account_id).toBe("sk_live_real");

    const call = fetchMock.mock.calls[0];
    expect(fetchCallUrl(call)).toBe("/api/bff/v1/channels/web-livechat/connect");
    expect(fetchCallMethod(call)).toBe("POST");
  });
});
