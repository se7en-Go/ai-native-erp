// Wave 3A: Live adapter tests. The generated SDK is exercised through
// vi.stubGlobal("fetch") — every assertion checks the real BFF URL, query
// params and JSON payload the adapter sends, against contracts/openapi.json.
//
// Note the SDK calls fetch(Request) (not fetch(url, init)), so the stub
// captures { url, method, bodyText } per call instead of reading mock.calls.
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  convertQuoteToOrder,
  fetchChannelAccounts,
  fetchChannelConsents,
  fetchChannelDlq,
  fetchChannelSuppressions,
  runSandboxTest,
  verifyEmailDns,
  generateQuotePdf,
  getConversationMessages,
  getConversations,
  getFinanceOverview,
  getOnboardingState,
  getCatalogVariants,
  getOrderDetail,
  getOrders,
  getVariantMappings,
  liveCheckSku,
  mapChannelParam,
  mergeCustomers,
  replayDlqEvent,
  sendConversationMessage,
  shipOrder,
  splitCustomers,
  unwrap,
  verifyPayment,
} from "@/lib/api-client/live-client";
import { BffApiError } from "@/lib/contracts";

const BFF = "http://localhost:3000/api/bff";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

interface CapturedCall {
  url: string;
  method: string | undefined;
  bodyText: string | null;
}

function stubFetch(handler: (url: string) => Response) {
  const captured: CapturedCall[] = [];
  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (input instanceof Request) {
      captured.push({
        url: input.url,
        method: input.method,
        bodyText: await input.text(),
      });
    } else {
      captured.push({
        url: String(input),
        method: init?.method,
        bodyText: typeof init?.body === "string" ? init.body : null,
      });
    }
    return handler(captured[captured.length - 1].url);
  });
  vi.stubGlobal("fetch", mock);
  return { mock, captured };
}

const CONVERSATION_PAGE = {
  items: [
    {
      id: "conv-1",
      customer_id: "cust-1",
      customer_name: null,
      customer: { display_name: "Hans Weber" },
      country_code: "DE",
      iana_timezone: "Europe/Berlin",
      channel_account_id: "ch-1",
      channel_type: "whatsapp_cloud",
      conversation_mode: "b2c_support",
      status: "open",
      unread_count: 2,
      sla_due_at: null,
      csw_expires_at: null,
      human_agent_window_expires_at: null,
      last_message_preview: null,
      updated_at: "2026-11-19T00:00:00Z",
      tenant_id: "t1",
    },
  ],
  total: 1,
  limit: 25,
  offset: 0,
  has_more: false,
};

describe("live-client (generated SDK over the BFF)", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("mapChannelParam: all is omitted, whatsapp maps to whatsapp_cloud", () => {
    expect(mapChannelParam("all")).toBeUndefined();
    expect(mapChannelParam("")).toBeUndefined();
    expect(mapChannelParam("whatsapp")).toBe("whatsapp_cloud");
    expect(mapChannelParam("email")).toBe("email");
    expect(mapChannelParam("shopify")).toBe("shopify");
  });

  it("getConversations calls the contract path with mapped channel/filter and null defaults", async () => {
    const { mock, captured } = stubFetch(() => jsonResponse(CONVERSATION_PAGE));
    const page = await getConversations("whatsapp", "unread", "hans");
    expect(mock).toHaveBeenCalledTimes(1);
    const url = new URL(captured[0].url);
    expect(`${url.origin}${url.pathname}`).toBe(
      `${BFF}/api/v1/inbox/conversations`,
    );
    expect(url.searchParams.get("filter")).toBe("unread");
    expect(url.searchParams.get("channel")).toBe("whatsapp_cloud");
    expect(url.searchParams.get("q")).toBe("hans");
    expect(url.searchParams.get("limit")).toBe("25");
    expect(url.searchParams.get("offset")).toBe("0");
    expect(page.items).toHaveLength(1);
    expect(page.items[0].customer_name).toBe("Hans Weber");
    expect(page.items[0].last_message_preview).toBe("");
    expect(page.total).toBe(1);
  });

  it("getConversations omits the channel param for 'all'", async () => {
    const { captured } = stubFetch(() => jsonResponse(CONVERSATION_PAGE));
    await getConversations("all", "all", "");
    const url = new URL(captured[0].url);
    expect(url.searchParams.has("channel")).toBe(false);
    expect(url.searchParams.has("q")).toBe(false);
  });

  it("getConversationMessages maps nullable body_text to empty string", async () => {
    const { captured } = stubFetch(() =>
      jsonResponse({
        items: [
          {
            id: "m-1",
            conversation_id: "conv-1",
            channel_type: "email",
            direction: "inbound",
            sender_type: "customer",
            content_type: "text",
            body_text: null,
            body_sanitized_html: null,
            delivery_status: "delivered",
            source_timestamp: "2026-11-19T00:00:00Z",
          },
        ],
        total: 1,
        limit: 50,
        offset: 0,
        has_more: false,
      }),
    );
    const page = await getConversationMessages("conv-1");
    const url = new URL(captured[0].url);
    expect(`${url.origin}${url.pathname}`).toBe(
      `${BFF}/api/v1/inbox/conversations/conv-1/messages`,
    );
    expect(url.searchParams.get("limit")).toBe("50");
    expect(page.items[0].body_text).toBe("");
  });

  it("sendConversationMessage posts the contract body", async () => {
    const { captured } = stubFetch(() =>
      jsonResponse({ id: "m-9", status: "queued" }, 201),
    );
    await sendConversationMessage("conv-1", {
      direction: "outbound",
      content_type: "text",
      body_text: "Hello",
      target_language: "de",
      translate_before_send: true,
    });
    expect(captured[0].url).toBe(`${BFF}/api/v1/inbox/conversations/conv-1/messages`);
    expect(captured[0].method).toBe("POST");
    expect(JSON.parse(captured[0].bodyText ?? "")).toMatchObject({
      direction: "outbound",
      content_type: "text",
      body_text: "Hello",
      target_language: "de",
      translate_before_send: true,
    });
  });

  it("getCatalogVariants derives UI fields from attributes and coerces MOQ prices", async () => {
    stubFetch(() =>
      jsonResponse({
        items: [
          {
            id: "var-1",
            product_id: "prod-1",
            sku: "LED-COB-24V-5M",
            attributes: {
              spu_code: "LED-COB-24V",
              category: "LED Strip Lighting",
              hs_code: "94054200",
              certifications: ["CE", "RoHS"],
              cct: "4000K",
            },
            cost_price_base: "45.50",
            base_currency: "CNY",
            min_margin_floor_pct: null,
            retail_price_usd: "29.90",
            b2b_moq_tiers: [{ min_qty: 50, unit_price_usd: "18.5" }],
            lead_time_days: 7,
            tenant_id: "t1",
            created_at: "2026-11-19T00:00:00Z",
            weight_grams: 120,
            carton_dims_cm: null,
          },
        ],
        total: 1,
        limit: 50,
        offset: 0,
        has_more: false,
      }),
    );
    const page = await getCatalogVariants("LED", 50, 0);
    const v = page.items[0];
    expect(v.spu_code).toBe("LED-COB-24V");
    expect(v.category).toBe("LED Strip Lighting");
    expect(v.hs_code).toBe("94054200");
    expect(v.certifications).toEqual(["CE", "RoHS"]);
    expect(v.min_margin_floor_pct).toBe("0.00");
    expect(v.b2b_moq_tiers).toEqual([{ min_qty: 50, unit_price_usd: 18.5 }]);
    expect(v.attributes).toEqual(
      expect.objectContaining({ cct: "4000K", spu_code: "LED-COB-24V" }),
    );
  });

  it("getVariantMappings returns the bundle mapped to UI shapes", async () => {
    const { captured } = stubFetch(() =>
      jsonResponse({
        variant: {
          id: "var-1",
          product_id: "prod-1",
          sku: "LED-COB-24V-5M",
          attributes: {},
          cost_price_base: null,
          base_currency: "CNY",
          min_margin_floor_pct: "15.00",
          retail_price_usd: "29.90",
          b2b_moq_tiers: [],
          lead_time_days: 7,
          tenant_id: "t1",
          created_at: "2026-11-19T00:00:00Z",
          weight_grams: 120,
          carton_dims_cm: null,
        },
        mappings: [
          {
            id: "map-1",
            variant_id: "var-1",
            channel_account_id: "ch-shop-1",
            channel_label: "Shopify US Flagship",
            external_product_id: "gid://shopify/Product/991",
            external_sku: "COB-24V-2PACK",
            bundle_multiplier: 2,
            sync_inventory_enabled: true,
            tenant_id: "t1",
            created_at: "2026-11-19T00:00:00Z",
          },
        ],
      }),
    );
    const bundle = await getVariantMappings("var-1");
    expect(captured[0].url).toBe(`${BFF}/api/v1/catalog/variants/var-1/mappings`);
    expect(bundle.variant.spu_code).toBe("LED-COB-24V-5M");
    expect(bundle.mappings[0].channel_label).toBe("Shopify US Flagship");
  });

  it("convertQuoteToOrder prefers the tenant default when several warehouses exist", async () => {
    const { captured } = stubFetch((url) => url.endsWith("/warehouses")
      ? jsonResponse({ default_warehouse_id: "wh-2", items: [{ id: "wh-1" }, { id: "wh-2" }] })
      : jsonResponse({ order: { id: "ord-1", order_no: "ORD-1" } }, 201));
    const result = await convertQuoteToOrder("q-1");
    expect(result.order.order_no).toBe("ORD-1");
    expect(captured.map(({url, method}) => [url, method])).toEqual([
      [`${BFF}/api/v1/orders/warehouses`, "GET"],
      [`${BFF}/api/v1/b2b/quotes/q-1/convert-to-order`, "POST"],
    ]);
    expect(JSON.parse(captured[1].bodyText ?? "")).toMatchObject({ warehouse_id: "wh-2" });
  });

  it("shipOrder uses the only warehouse when no default is set", async () => {
    const { captured } = stubFetch((url) => url.endsWith("/warehouses")
      ? jsonResponse({ default_warehouse_id: null, items: [{ id: "wh-1" }] })
      : jsonResponse({ id: "sh-1" }, 201));
    await shipOrder("ord-1", { trackingNumber: "DHL99887766" });
    expect(captured[0].url).toBe(`${BFF}/api/v1/orders/warehouses`);
    expect(captured[1].url).toBe(`${BFF}/api/v1/orders/ord-1/ship`);
    expect(JSON.parse(captured[1].bodyText ?? "")).toMatchObject({
      warehouse_id: "wh-1",
      tracking_number: "DHL99887766",
      carrier_code: "DHL",
      shipment_mode: "parcel",
    });
  });

  it.each([
    [[], "ERR_NO_WAREHOUSE_CONFIGURED"],
    [[{ id: "wh-1" }, { id: "wh-2" }], "ERR_WAREHOUSE_SELECTION_REQUIRED"],
  ] as const)("does not convert or ship with no unambiguous warehouse (%j)", async (items, code) => {
    const { captured } = stubFetch(() => jsonResponse({ default_warehouse_id: null, items }));
    await expect(convertQuoteToOrder("q-1")).rejects.toMatchObject({ code });
    await expect(shipOrder("ord-1", { trackingNumber: "ABC" })).rejects.toMatchObject({ code });
    expect(captured.map(({ url }) => url)).toEqual(Array(2).fill(`${BFF}/api/v1/orders/warehouses`));
  });

  it("uses an explicitly selected warehouse without looking up the default", async () => {
    const { captured } = stubFetch(() => jsonResponse({ order: { id: "ord-1" } }, 201));
    await convertQuoteToOrder("q-1", { warehouseId: "wh-selected" });
    expect(captured).toHaveLength(1);
    expect(JSON.parse(captured[0].bodyText ?? "")).toMatchObject({ warehouse_id: "wh-selected" });
  });

  it("generateQuotePdf returns the signed download_url", async () => {
    stubFetch(() =>
      jsonResponse({
        download_url: "https://minio.local/PI-1.pdf",
        pdf_r2_key: "k",
        quote_id: "q-1",
        quote_no: "PI-1",
        version: 1,
      }),
    );
    const pdf = await generateQuotePdf("q-1");
    expect(pdf.download_url).toBe("https://minio.local/PI-1.pdf");
  });

  it("getOrders omits order_mode when empty and maps nullable customer_name", async () => {
    const { captured } = stubFetch(() =>
      jsonResponse({
        items: [
          {
            id: "ord-1",
            order_no: "ORD-1",
            order_mode: "b2b_wholesale",
            customer_name: null,
            fulfillment_status: "in_production",
            payment_status: "partial",
            currency: "USD",
            total_amount: "7100.00",
            tenant_id: "t1",
            created_at: "2026-11-19T00:00:00Z",
            updated_at: "2026-11-19T00:00:00Z",
          },
        ],
        total: 1,
        limit: 20,
        offset: 0,
        has_more: false,
      }),
    );
    const page = await getOrders("", 20, 0);
    const url = new URL(captured[0].url);
    expect(url.searchParams.has("order_mode")).toBe(false);
    expect(page.items[0].customer_name).toBe("");
    expect(page.items[0].shipment_blocked_70_unpaid).toBe(false);
  });

  it("getOrderDetail maps 17TRACK events with time_iso/description fallbacks", async () => {
    stubFetch(() =>
      jsonResponse({
        id: "ord-1",
        order_no: "ORD-1",
        order_mode: "b2b_wholesale",
        fulfillment_status: "shipped",
        payment_status: "partial",
        currency: "USD",
        total_amount: "7100.00",
        shipments: [
          {
            id: "sh-1",
            tracking_provider: "17track",
            tracking_number: "DHL99887766",
            carrier_code: "DHL",
            shipment_mode: "parcel",
            standardized_status: "InTransit",
            sub_status: null,
            is_stagnant_72h: true,
            tracking_events: [
              { time_iso: "2026-11-16T06:00:00Z", location: "Frankfurt DE" },
              {
                time_utc: "2026-11-15T06:00:00Z",
                description: "Departed facility",
              },
            ],
          },
        ],
      }),
    );
    const detail = await getOrderDetail("ord-1");
    expect(detail.shipments).toHaveLength(1);
    expect(detail.shipments[0].tracking_events[0]).toEqual({
      time_utc: "2026-11-16T06:00:00Z",
      location: "Frankfurt DE",
      description: "",
    });
    expect(detail.shipments[0].tracking_events[1].description).toBe(
      "Departed facility",
    );
  });

  it("getFinanceOverview falls back unknown receivable statuses to open", async () => {
    stubFetch(() =>
      jsonResponse({
        summary: {
          total_gross_revenue_base: "100.00",
          total_net_margin_base: "30.00",
          avg_margin_pct: "30.00",
          base_currency: "USD",
        },
        receivables: [
          {
            id: "rp-1",
            order_id: "ord-1",
            order_no: "ORD-1",
            order_mode: "b2b_wholesale",
            milestone_label: "30% deposit",
            due_amount: "30.00",
            paid_amount: "0.00",
            currency: "USD",
            status: "weird_status",
            shipment_blocked_70_unpaid: true,
          },
        ],
        cvp_snapshots: [],
      }),
    );
    const overview = await getFinanceOverview();
    expect(overview.receivables[0].status).toBe("open");
    expect(overview.receivables[0].shipment_blocked_70_unpaid).toBe(true);
  });

  it("getOnboardingState reads wizard extras from metadata_json", async () => {
    stubFetch(() =>
      jsonResponse({
        tenant_id: "t1",
        current_stage: "ChannelConnecting",
        preset_type: "hybrid_factory_b2b_b2c",
        completed_steps: ["WorkspaceCreated"],
        metadata_json: {
          base_currency: "CNY",
          iana_timezone: "Asia/Shanghai",
          site_key: "wc_site_qz_001",
          allowed_origins: ["https://shop.qzled.example.com"],
          dns_status: {
            spf_verified: true,
            dkim_verified: true,
            return_path_verified: false,
          },
        },
        updated_at: "2026-11-19T00:00:00Z",
      }),
    );
    const state = await getOnboardingState();
    expect(state.current_stage).toBe("ChannelConnecting");
    expect(state.base_currency).toBe("CNY");
    expect(state.iana_timezone).toBe("Asia/Shanghai");
    expect(state.site_key).toBe("wc_site_qz_001");
    expect(state.allowed_origins).toEqual(["https://shop.qzled.example.com"]);
    expect(state.dns_status).toEqual({
      spf_verified: true,
      dkim_verified: true,
      return_path_verified: false,
    });
  });

  it("liveCheckSku sends sku/qty/incoterm as query params", async () => {
    const { captured } = stubFetch(() =>
      jsonResponse({
        sku: "LED-COB-24V-5M",
        available_qty: 1240,
        unit_price_quote_ccy: "14.20",
        moq_tier_applied: 500,
        incoterm: "FOB",
        source_tool_call_id: "sql-sku-99",
      }),
    );
    const res = await liveCheckSku("LED-COB-24V-5M", 100, "FOB");
    const url = new URL(captured[0].url);
    expect(`${url.origin}${url.pathname}`).toBe(`${BFF}/api/v1/catalog/live-check`);
    expect(url.searchParams.get("sku")).toBe("LED-COB-24V-5M");
    expect(url.searchParams.get("qty")).toBe("100");
    expect(url.searchParams.get("incoterm")).toBe("FOB");
    expect(res.source_tool_call_id).toBe("sql-sku-99");
  });

  it("merge/split use the contract /customers paths with contract bodies", async () => {
    const { captured } = stubFetch(() => jsonResponse({ ok: true }));
    await mergeCustomers("cust-dup-9", "cust-target", "Confirmed matching buyer identity");
    expect(captured[0].url).toBe(`${BFF}/api/v1/customers/merge`);
    expect(JSON.parse(captured[0].bodyText ?? "")).toMatchObject({
      source_customer_id: "cust-dup-9",
      target_customer_id: "cust-target",
      reason: "Confirmed matching buyer identity",
    });

    await splitCustomers("me-1", "Manual operator split");
    expect(captured[1].url).toBe(`${BFF}/api/v1/customers/split`);
    expect(JSON.parse(captured[1].bodyText ?? "")).toMatchObject({
      merge_event_id: "me-1",
      reason: "Manual operator split",
    });
  });

  it("verifyPayment and replayDlqEvent hit the contract mutation paths", async () => {
    const { captured } = stubFetch(() => jsonResponse({ ok: true }));
    await verifyPayment("pay-1");
    expect(captured[0].url).toBe(`${BFF}/api/v1/finance/payments/pay-1/verify`);
    expect(captured[0].method).toBe("POST");
    await replayDlqEvent("dlq-1");
    expect(captured[1].url).toBe(`${BFF}/api/v1/channels/dlq/dlq-1/replay`);
  });

  it("unwrap turns a 422 FastAPI validation error into BffApiError", async () => {
    stubFetch(() =>
      jsonResponse(
        {
          detail: [
            { loc: ["query", "channel"], msg: "Input should be a valid enum", type: "enum" },
          ],
        },
        422,
      ),
    );
    let caught: unknown;
    try {
      await getConversations("all", "all", "");
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeInstanceOf(BffApiError);
    expect((caught as BffApiError).status).toBe(422);
    expect((caught as BffApiError).detail).toContain("valid enum");
  });

  it("unwrap throws BffApiError on a ProblemDetail error body", () => {
    expect(() =>
      unwrap({
        data: undefined,
        error: {
          type: "https://erp.local/errors/not-found",
          title: "Not Found",
          status: 404,
          detail: "No such conversation.",
          code: "ERR_NOT_FOUND",
        },
        response: new Response(null, { status: 404, statusText: "Not Found" }),
      }),
    ).toThrowError(BffApiError);
    try {
      unwrap({
        data: undefined,
        error: { detail: "No such conversation.", title: "Not Found" },
        response: new Response(null, { status: 404, statusText: "Not Found" }),
      });
    } catch (err) {
      expect((err as BffApiError).status).toBe(404);
      expect((err as BffApiError).detail).toBe("No such conversation.");
    }
  });
  it("onboarding readiness probes and channel settings reads use generated SDK paths", async () => {
    const { captured } = stubFetch((url) => {
      if (url.includes("/email/verify-dns")) {
        return jsonResponse({ spf_verified: true, dkim_verified: true, return_path_verified: true });
      }
      if (url.includes("/sandbox-test")) {
        return jsonResponse({ passed: true, reply_text: "ok", source_tool_call_id: "tool-1" });
      }
      if (url.includes("/channels/accounts")) {
        return jsonResponse({
          items: [{
            id: "ch-1", channel_type: "email", external_account_id: "a@inbound.mail.example.com",
            display_name: "Email bridge", status: "active",
            rate_limit_profile: { mps: null, coexistence: null },
          }],
          total: 1, limit: 20, offset: 0, has_more: false,
        });
      }
      if (url.includes("/channels/dlq")) {
        return jsonResponse({
          items: [{
            id: "d-1", channel_type: "email", external_event_id: null,
            error_message: "boom", retry_count: 2, created_at: "2026-11-19T00:00:00Z",
          }],
          total: 1, limit: 20, offset: 0, has_more: false,
        });
      }
      return jsonResponse({ items: [], total: 0, limit: 20, offset: 0, has_more: false });
    });

    expect(await verifyEmailDns("mail.example.com")).toMatchObject({ spf_verified: true });
    expect((await runSandboxTest("b2c_wismo", "where is my order?"))["passed"]).toBe(true);
    const accounts = await fetchChannelAccounts();
    expect(accounts.items[0].rate_limit_profile).toEqual({ mps: undefined, coexistence: undefined });
    const dlq = await fetchChannelDlq();
    expect(dlq.items[0].external_event_id).toBe("");
    await fetchChannelConsents();
    await fetchChannelSuppressions();

    expect(captured.map(({ url }) => new URL(url).pathname)).toEqual([
      "/api/bff/api/v1/onboarding/email/verify-dns",
      "/api/bff/api/v1/onboarding/sandbox-test",
      "/api/bff/api/v1/channels/accounts",
      "/api/bff/api/v1/channels/dlq",
      "/api/bff/api/v1/channels/consents",
      "/api/bff/api/v1/channels/suppressions",
    ]);
    expect(JSON.parse(captured[1].bodyText ?? "")).toEqual({
      scenario_id: "b2c_wismo", message: "where is my order?",
    });
  });

});
