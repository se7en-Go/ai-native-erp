// Live marketplace push (Amazon SP-API / eBay Sell API / Alibaba IOP) + live social
// API publish wiring.
//
// 两层验证：
//   1. 传输层：`publishSourcingDraft` / `triggerMarketplaceOrderSync` / `publishSocialPost`
//      打到契约端点、请求体字段名与后端一致，mock 模式返回诚实口径；
//   2. 页面层：/catalog 的实时上架开关默认关闭，勾选后 `push_to_marketplace: true`
//      真的透传给 API，结果徽标区分「映射就绪」与「已上线刊登」。
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import CatalogPage from "@/app/catalog/page";
import { AuthSessionContext, type AuthSessionState } from "@/components/providers";
import { I18nProvider } from "@/lib/i18n";
import type { Principal } from "@/lib/contracts";
import type { PostRead } from "@/lib/api-client/types.gen";
import {
  mockSourcingDrafts,
  publishSourcingDraft,
  triggerMarketplaceOrderSync,
} from "@/lib/api-client/pack-f-sourcing";
import { publishSocialPost, socialPostDelivery } from "@/lib/phase3d-data";
import { postDeliveryLabel } from "@/components/social/presentation";

vi.mock("@/hooks/use-realtime-inbox", () => ({ useRealtimeInbox: vi.fn() }));

// 页面层要断言「勾选开关后 push_to_marketplace 真的进了请求体」，
// 所以对 pack-f-sourcing 做一次轻量包装：仍走真实 mock 实现，只记录调用参数。
const { publishSpy } = vi.hoisted(() => ({ publishSpy: vi.fn() }));

vi.mock("@/lib/api-client/pack-f-sourcing", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api-client/pack-f-sourcing")>();
  return {
    ...actual,
    publishSourcingDraft: (
      draftId: string,
      body: Parameters<typeof actual.publishSourcingDraft>[1],
    ) => {
      publishSpy(draftId, body);
      return actual.publishSourcingDraft(draftId, body);
    },
  };
});

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
    </QueryClientProvider>,
  );
}

const POST_ROW: PostRead = {
  id: "post-1",
  social_account_id: "acc-1",
  platform: "instagram",
  title: "IP65 work light",
  caption: "IP65 work light, factory direct",
  hashtags: ["#worklight"],
  media_urls: [],
  sku_ref: "LED-COB-24V-5M",
  status: "published",
  scheduled_at: null,
  published_at: "2026-11-20T09:30:00Z",
  external_post_id: "ig-remote-1",
  delivery_mode: "external_api",
  external_publish_confirmed: true,
  metrics: {},
  created_by: "11111111-1111-1111-1111-111111111111",
  created_at: "2026-11-19T09:00:00Z",
  updated_at: "2026-11-20T09:30:00Z",
};

// ---------------------------------------------------------------------------
// 1. Sourcing live marketplace push
// ---------------------------------------------------------------------------

describe("sourcing live marketplace push", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("returns a live published listing when push_to_marketplace is true", async () => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");

    const results = await publishSourcingDraft("src-draft-1", {
      channel_account_ids: ["ch-amazon-qzled", "ch-ebay-qzled"],
      price_multiplier: "1.00",
      stock_sync_ratio: "1.00",
      push_to_marketplace: true,
      initial_stock_quantity: 120,
    });

    expect(results).toHaveLength(2);
    for (const result of results) {
      expect(result.listing_state).toBe("live_published");
      expect(result.external_listing_confirmed).toBe(true);
      expect(result.publish_mode).toBe("live_marketplace_api");
      expect(result.external_product_id).toMatch(/^ext-src-draft-1-/);
    }
    expect(results.map((row) => row.channel_type)).toEqual(["amazon_sp", "ebay"]);

    const draft = mockSourcingDrafts().find((row) => row.id === "src-draft-1")!;
    expect(draft.status).toBe("published");
    expect(draft.listing_state).toBe("live_published");
    expect(draft.attributes.initial_stock_quantity).toBe(120);
    expect(draft.publish_history).toHaveLength(2);
    expect(draft.publish_history?.every((row) => row.publish_mode === "live_marketplace_api")).toBe(
      true,
    );
  });

  it("stays local SKU mapping only when push_to_marketplace is omitted", async () => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");

    const results = await publishSourcingDraft("src-draft-2", {
      channel_account_ids: ["ch-shopify-qzled"],
      price_multiplier: "1.00",
      stock_sync_ratio: "1.00",
    });

    expect(results[0].listing_state).toBe("mapping_ready");
    expect(results[0].external_listing_confirmed).toBe(false);
    expect(results[0].publish_mode).toBe("local_sku_mapping");

    const draft = mockSourcingDrafts().find((row) => row.id === "src-draft-2")!;
    // 本地状态保持「已刊登（本地记录）」，绝不自我宣称平台已上线。
    expect(draft.status).toBe("published");
    expect(draft.listing_state).toBeUndefined();
  });

  it("posts to the marketplace sync-orders endpoint in live mode", async () => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "false");
    const captured = stubFetch(() =>
      jsonResponse({
        channel_account_id: "ch/amazon 1",
        channel_type: "amazon_sp",
        scanned: 4,
        recovered: 3,
        updated: 2,
        failed: 1,
      }),
    );

    const stats = await triggerMarketplaceOrderSync("ch/amazon 1", 48);

    expect(captured[0].url).toBe(
      `${BFF}/v1/channels/marketplace/ch%2Famazon%201/sync-orders?lookback_hours=48`,
    );
    expect(captured[0].method).toBe("POST");
    expect(stats).toEqual({
      channel_account_id: "ch/amazon 1",
      channel_type: "amazon_sp",
      scanned: 4,
      recovered: 3,
      updated: 2,
      failed: 1,
    });
  });

  it("returns clean order sync stats in mock mode with a 24h default lookback", async () => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
    const captured = stubFetch(() => jsonResponse({}));

    const stats = await triggerMarketplaceOrderSync("ch-amazon-qzled");

    expect(stats).toEqual({
      channel_account_id: "ch-amazon-qzled",
      channel_type: "amazon_sp",
      scanned: 2,
      recovered: 1,
      updated: 1,
      failed: 0,
    });
    // mock 模式零网络请求。
    expect(captured).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// 2. Social live API publish
// ---------------------------------------------------------------------------

describe("social live API publish", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("sends execute_remote in the publish body and maps the platform receipt", async () => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "false");
    const captured = stubFetch(() => jsonResponse(POST_ROW));

    const post = await publishSocialPost("post-1", false, { executeRemote: true });

    expect(captured[0].url).toBe(`${BFF}/v1/social/posts/post-1/publish`);
    expect(captured[0].method).toBe("POST");
    expect(JSON.parse(captured[0].bodyText ?? "{}")).toEqual({
      manual_confirmation: false,
      execute_remote: true,
    });
    expect(post.delivery_mode).toBe("external_api");
    expect(post.external_publish_confirmed).toBe(true);
    expect(post.external_post_id).toBe("ig-remote-1");
    expect(postDeliveryLabel(socialPostDelivery(post)).state).toBe("external_confirmed");
    expect(postDeliveryLabel(socialPostDelivery(post)).zh).toBe("外部平台已确认");
  });

  it("defaults execute_remote to false so manual platforms are never auto-posted", async () => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "false");
    const captured = stubFetch(() => jsonResponse(POST_ROW));

    await publishSocialPost("post-1", true);

    expect(JSON.parse(captured[0].bodyText ?? "{}")).toEqual({
      manual_confirmation: true,
      execute_remote: false,
    });
  });

  it("stamps an external_api receipt in mock mode when executeRemote is requested", async () => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");

    const post = await publishSocialPost("post-mock-tiktok-scheduled", false, {
      executeRemote: true,
    });

    expect(post.status).toBe("published");
    expect(post.delivery_mode).toBe("external_api");
    expect(post.external_publish_confirmed).toBe(true);
    expect(post.external_post_id).toBe("remote-mock-post-moc");
    expect(postDeliveryLabel(socialPostDelivery(post)).state).toBe("external_confirmed");
  });

  it("keeps the mock receipt local when the remote toggle is off", async () => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");

    const post = await publishSocialPost("post-mock-xiaohongshu-draft", true);

    expect(post.status).toBe("published");
    expect(post.delivery_mode).toBe("manual_confirmed");
    expect(post.external_publish_confirmed).toBe(false);
    expect(post.external_post_id).toBeNull();
    const label = postDeliveryLabel(socialPostDelivery(post));
    expect(label.state).toBe("manual_confirmed");
    expect(label.zh).toBe("官方后台手动确认");
  });
});

// ---------------------------------------------------------------------------
// 3. /catalog publish panel wiring
// ---------------------------------------------------------------------------

describe("catalog sourcing live push toggle", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
    publishSpy.mockClear();
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("renders the live push toggle unchecked and forwards push_to_marketplace once checked", async () => {
    renderWithProviders(<CatalogPage />, "manager", "zh");
    fireEvent.click(await screen.findByTestId("catalog-tab-sourcing"));

    const card = await screen.findByTestId("sourcing-draft-src-draft-1");
    fireEvent.click(within(card).getByTestId("sourcing-publish-src-draft-1"));

    const panel = await screen.findByTestId("sourcing-publish-panel-src-draft-1");
    const toggle = within(panel).getByTestId("sourcing-live-push-toggle");
    expect(toggle).toBeInTheDocument();
    // 默认关闭：既有「只写本地映射」的语义零回归。
    expect(toggle).not.toBeChecked();
    expect(panel.textContent).toContain("实时上架");

    fireEvent.click(screen.getByTestId("sourcing-publish-check-ch-amazon-qzled"));
    fireEvent.click(toggle);
    expect(toggle).toBeChecked();
    fireEvent.click(screen.getByTestId("sourcing-publish-confirm-src-draft-1"));

    await waitFor(() => expect(publishSpy).toHaveBeenCalledTimes(1));
    expect(publishSpy).toHaveBeenCalledWith(
      "src-draft-1",
      expect.objectContaining({
        channel_account_ids: ["ch-amazon-qzled"],
        push_to_marketplace: true,
      }),
    );

    const results = await screen.findByTestId("sourcing-publish-results-src-draft-1");
    const badge = within(results).getByTestId(
      "sourcing-publish-result-status-src-draft-1-ch-amazon-qzled",
    );
    expect(badge.textContent).toContain("已上线刊登");
    expect(
      within(results).getByTestId("sourcing-publish-result-external-id-src-draft-1-ch-amazon-qzled")
        .textContent,
    ).toContain("ext-src-draft-1-1");
    // 平台确认后草稿徽标同步升级。
    await waitFor(() =>
      expect(
        screen.getByTestId("sourcing-draft-status-src-draft-1").textContent,
      ).toContain("已上线刊登"),
    );
  });

  it("keeps push_to_marketplace false when the operator leaves the toggle off", async () => {
    renderWithProviders(<CatalogPage />, "manager", "zh");
    fireEvent.click(await screen.findByTestId("catalog-tab-sourcing"));

    const card = await screen.findByTestId("sourcing-draft-src-draft-3");
    fireEvent.click(within(card).getByTestId("sourcing-publish-src-draft-3"));

    const panel = await screen.findByTestId("sourcing-publish-panel-src-draft-3");
    expect(within(panel).getByTestId("sourcing-live-push-toggle")).not.toBeChecked();

    fireEvent.click(screen.getByTestId("sourcing-publish-check-ch-shopify-qzled"));
    fireEvent.click(screen.getByTestId("sourcing-publish-confirm-src-draft-3"));

    await waitFor(() => expect(publishSpy).toHaveBeenCalledTimes(1));
    expect(publishSpy).toHaveBeenCalledWith(
      "src-draft-3",
      expect.objectContaining({ push_to_marketplace: false }),
    );

    const results = await screen.findByTestId("sourcing-publish-results-src-draft-3");
    expect(
      within(results).getByTestId("sourcing-publish-result-status-src-draft-3-ch-shopify-qzled")
        .textContent,
    ).toContain("映射就绪");
    expect(screen.getByTestId("sourcing-draft-status-src-draft-3").textContent).toContain("已刊登");
  });
});
