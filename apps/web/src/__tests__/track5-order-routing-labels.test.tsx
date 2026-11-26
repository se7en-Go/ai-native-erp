// Track 5 frontend: routing / anti-loss rule builder and the per-order carrier label
// modal on /orders. Page specs run against the fixture store
// (NEXT_PUBLIC_USE_MOCK_API=true); the client specs stub global fetch and assert the
// exact same-origin BFF contract paths/bodies.
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import OrdersPage from "@/app/orders/page";
import { AuthSessionContext, type AuthSessionState } from "@/components/providers";
import { I18nProvider } from "@/lib/i18n";
import type { Principal } from "@/lib/contracts";
import {
  bookShippingLabel,
  createRoutingRule,
  deleteRoutingRule,
  evaluateOrderRouting,
  fetchRoutingEvaluation,
  fetchRoutingRules,
  releaseRoutingHold,
  resetTrack5MockStore,
  routingRulePayloadFromDraft,
  shippingLabelPdfUrl,
  updateRoutingRule,
  EMPTY_ROUTING_RULE_DRAFT,
} from "@/lib/api-client/track5-routing-labels";

vi.mock("@/hooks/use-realtime-inbox", () => ({ useRealtimeInbox: vi.fn() }));

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

function renderPage(ui: React.ReactNode, role: Principal["role"]) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const authValue: AuthSessionState = {
    principal: principal(role),
    isLoading: false,
    logout: vi.fn(async () => {}),
    refreshSession: vi.fn(async () => {}),
  };
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthSessionContext.Provider value={authValue}>
        <I18nProvider defaultLocale="zh">{ui}</I18nProvider>
      </AuthSessionContext.Provider>
    </QueryClientProvider>
  );
}

// ---------------------------------------------------------------------------
// Pure payload builder
// ---------------------------------------------------------------------------

describe("routing rule draft -> API payload", () => {
  it("normalizes CSV lists, keeps decimals as strings and maps actions", () => {
    const built = routingRulePayloadFromDraft({
      ...EMPTY_ROUTING_RULE_DRAFT,
      name: "  Nordic express lane  ",
      priority: "10",
      destinationCountries: "se, no，dk",
      orderModes: "B2C_Retail",
      minMarginPct: "8.50",
      maxWeightKg: "25",
      carrierCode: "yunexpress",
      serviceCode: "YunExpress-Expedited",
      addTags: "eu-fast-lane, priority",
      holdForReview: true,
      holdReason: "亏本预警",
    });
    expect("payload" in built).toBe(true);
    if (!("payload" in built)) return;
    expect(built.payload).toEqual({
      name: "Nordic express lane",
      priority: 10,
      is_enabled: true,
      conditions: {
        destination_countries: ["SE", "NO", "DK"],
        channel_types: [],
        order_modes: ["b2c_retail"],
        min_margin_pct: "8.50",
        min_total_amount: null,
        max_weight_kg: "25",
      },
      actions: {
        warehouse_id: null,
        carrier_code: "yunexpress",
        service_code: "YunExpress-Expedited",
        add_tags: ["eu-fast-lane", "priority"],
        hold_for_review: true,
        hold_reason: "亏本预警",
      },
    });
  });

  it("rejects a nameless rule, a condition-free rule and a non-decimal floor", () => {
    expect(routingRulePayloadFromDraft({ ...EMPTY_ROUTING_RULE_DRAFT })).toEqual({
      error: "规则名称不能为空 / Rule name is required.",
    });
    expect(
      routingRulePayloadFromDraft({ ...EMPTY_ROUTING_RULE_DRAFT, name: "Empty" }),
    ).toHaveProperty("error");
    const badMargin = routingRulePayloadFromDraft({
      ...EMPTY_ROUTING_RULE_DRAFT,
      name: "Bad margin",
      destinationCountries: "SE",
      minMarginPct: "abc",
    });
    expect(badMargin).toHaveProperty("error");
    const badWarehouse = routingRulePayloadFromDraft({
      ...EMPTY_ROUTING_RULE_DRAFT,
      name: "Bad warehouse",
      destinationCountries: "SE",
      warehouseId: "not-a-uuid",
    });
    expect(badWarehouse).toHaveProperty("error");
  });
});

// ---------------------------------------------------------------------------
// Live BFF contract (fetch stub)
// ---------------------------------------------------------------------------

describe("track5 routing/label BFF contract", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("reads, creates, patches and deletes routing rules", async () => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "false");
    const rule = {
      id: "rule-1",
      name: "Block negative margin",
      priority: 10,
      is_enabled: true,
      conditions: {
        destination_countries: ["SE"],
        channel_types: [],
        order_modes: [],
        min_margin_pct: "0.00",
        min_total_amount: null,
        max_weight_kg: null,
      },
      actions: {
        warehouse_id: null,
        carrier_code: "yunexpress",
        service_code: null,
        add_tags: ["anti-loss"],
        hold_for_review: true,
        hold_reason: null,
      },
      created_at: "2026-11-26T00:00:00.000Z",
      updated_at: "2026-11-26T00:00:00.000Z",
    };
    const captured = stubFetch((call) => {
      if (call.method === "DELETE") return new Response(null, { status: 204 });
      if (call.method === "POST") return jsonResponse(rule, 201);
      if (call.method === "PATCH") return jsonResponse({ ...rule, is_enabled: false });
      return jsonResponse([rule]);
    });

    expect(await fetchRoutingRules()).toEqual([rule]);
    expect(captured[0].url).toBe("/api/bff/v1/orders/routing-rules");

    const created = await createRoutingRule({
      name: "Block negative margin",
      priority: 10,
      conditions: { destination_countries: ["SE"], min_margin_pct: "0.00" },
      actions: { hold_for_review: true, add_tags: ["anti-loss"] },
    });
    expect(created.id).toBe("rule-1");
    expect(captured[1].url).toBe("/api/bff/v1/orders/routing-rules");
    expect(captured[1].method).toBe("POST");
    expect(JSON.parse(captured[1].bodyText ?? "{}")).toMatchObject({
      name: "Block negative margin",
      conditions: { destination_countries: ["SE"], min_margin_pct: "0.00" },
    });

    const patched = await updateRoutingRule("rule-1", { is_enabled: false });
    expect(patched.is_enabled).toBe(false);
    expect(captured[2].url).toBe("/api/bff/v1/orders/routing-rules/rule-1");
    expect(captured[2].method).toBe("PATCH");

    await deleteRoutingRule("rule-1");
    expect(captured[3].url).toBe("/api/bff/v1/orders/routing-rules/rule-1");
    expect(captured[3].method).toBe("DELETE");
  });

  it("evaluates, releases the hold and books a carrier label", async () => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "false");
    const evaluation = {
      id: "eval-1",
      tenant_id: "22222222-2222-2222-2222-222222222222",
      order_id: "ord-1",
      matched_rule_ids: ["rule-1"],
      recommended_warehouse_id: null,
      recommended_carrier: "4px",
      recommended_service: "4PX-Standard",
      estimated_margin_pct: "-12.5000",
      is_held: true,
      hold_reason: "Estimated margin -12.5% is negative",
      hold_released_by: null,
      hold_released_at: null,
      applied_tags: ["anti-loss"],
      evaluated_at: "2026-11-26T00:00:00.000Z",
    };
    const label = {
      id: "label-1",
      tenant_id: "22222222-2222-2222-2222-222222222222",
      order_id: "ord-1",
      shipment_id: null,
      carrier_code: "4px",
      service_code: "4PX-Standard",
      tracking_number: "4PX3000123456CN",
      waybill_number: "4PX3000123456CN",
      label_format: "pdf_4x6",
      label_r2_key: null,
      mode: "sandbox",
      shipping_cost_estimate: "16.35",
      currency: "USD",
      status: "created",
      created_at: "2026-11-26T00:00:00.000Z",
    };
    const captured = stubFetch((call) => {
      if (call.url.endsWith("/shipping-labels")) return jsonResponse([]);
      if (call.url.endsWith("/shipping-label")) return jsonResponse(label, 201);
      return jsonResponse(evaluation, call.method === "POST" ? 201 : 200);
    });

    expect(await fetchRoutingEvaluation("ord-1")).toEqual(evaluation);
    expect(captured[0].url).toBe("/api/bff/v1/orders/ord-1/routing-evaluation");

    const evaluated = await evaluateOrderRouting("ord-1");
    expect(evaluated.is_held).toBe(true);
    expect(captured[1].url).toBe("/api/bff/v1/orders/ord-1/evaluate-routing");
    expect(captured[1].method).toBe("POST");

    const released = await releaseRoutingHold("ord-1", "Freight renegotiated");
    expect(released.id).toBe("eval-1");
    expect(captured[2].url).toBe("/api/bff/v1/orders/ord-1/release-hold");
    expect(captured[2].method).toBe("POST");
    expect(JSON.parse(captured[2].bodyText ?? "{}")).toEqual({
      reason: "Freight renegotiated",
    });

    const booked = await bookShippingLabel("ord-1", {
      carrier_code: "4px",
      mode: "sandbox",
    });
    expect(booked.tracking_number).toBe("4PX3000123456CN");
    expect(captured[3].url).toBe("/api/bff/v1/orders/ord-1/shipping-label");
    expect(captured[3].method).toBe("POST");
    expect(JSON.parse(captured[3].bodyText ?? "{}")).toEqual({
      carrier_code: "4px",
      mode: "sandbox",
    });

    expect(shippingLabelPdfUrl("ord-1", "label-1")).toBe(
      "/api/bff/v1/orders/ord-1/shipping-labels/label-1/pdf",
    );
  });

  it("treats a 404 routing evaluation as 'not routed yet'", async () => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "false");
    stubFetch(() =>
      jsonResponse(
        {
          type: "https://erp.local/errors/routing-evaluation-not-found",
          title: "Routing Evaluation Not Found",
          status: 404,
          detail: "not routed",
          instance: "/api/v1/orders/ord-1/routing-evaluation",
          code: "ERR_ROUTING_EVALUATION_NOT_FOUND",
        },
        404,
      ),
    );
    expect(await fetchRoutingEvaluation("ord-1")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Page-level specs (fixture store)
// ---------------------------------------------------------------------------

describe("routing & label UI", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
    resetTrack5MockStore();
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("creates, disables and deletes a routing rule from the /orders builder", async () => {
    renderPage(<OrdersPage />, "manager");
    await waitFor(() => expect(screen.getByTestId("routing-rules-panel")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("routing-rules-toggle"));
    await waitFor(() => expect(screen.getByTestId("routing-rule-form")).toBeInTheDocument());
    expect(screen.getByTestId("routing-rules-list")).toHaveTextContent("负毛利立即拦截");

    const ruleName = `SE 亏损拦截 ${Date.now()}`;
    fireEvent.change(screen.getByTestId("routing-rule-name"), {
      target: { value: ruleName },
    });
    fireEvent.change(screen.getByTestId("routing-rule-priority"), { target: { value: "5" } });
    fireEvent.change(screen.getByTestId("routing-rule-countries"), {
      target: { value: "se, no" },
    });
    fireEvent.change(screen.getByTestId("routing-rule-margin"), {
      target: { value: "12.50" },
    });
    fireEvent.change(screen.getByTestId("routing-rule-carrier"), {
      target: { value: "4px" },
    });
    fireEvent.change(screen.getByTestId("routing-rule-tags"), {
      target: { value: "eu-loss-guard" },
    });
    fireEvent.click(screen.getByTestId("routing-rule-hold"));
    fireEvent.click(screen.getByTestId("routing-rule-submit"));

    const created = await screen.findByTestId("routing-rule-created");
    expect(created).toHaveTextContent(ruleName);

    const list = screen.getByTestId("routing-rules-list");
    const row = (await within(list).findByText(ruleName)).closest("li");
    expect(row).not.toBeNull();
    const rowScope = within(row as HTMLElement);
    expect(rowScope.getByText(/国家 SE\/NO/)).toBeInTheDocument();
    expect(rowScope.getByText(/毛利 ≥ 12\.50%/)).toBeInTheDocument();
    expect(row).toHaveTextContent("4px");
    expect(row).toHaveTextContent("拦截审单");

    const toggle = rowScope.getByRole("button", { name: "停用" });
    fireEvent.click(toggle);
    await waitFor(() => expect(rowScope.getByRole("button", { name: "启用" })).toBeInTheDocument());

    fireEvent.click(rowScope.getByRole("button", { name: `删除规则 ${ruleName}` }));
    await waitFor(() => expect(screen.queryByText(ruleName)).toBeNull());
  });

  it("blocks an empty rule inline instead of calling the API", async () => {
    renderPage(<OrdersPage />, "manager");
    await waitFor(() => expect(screen.getByTestId("routing-rules-panel")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("routing-rules-toggle"));
    fireEvent.change(screen.getByTestId("routing-rule-name"), {
      target: { value: "No conditions rule" },
    });
    fireEvent.click(screen.getByTestId("routing-rule-submit"));
    expect(await screen.findByTestId("routing-rule-error")).toHaveTextContent("至少需要一个条件");
    expect(screen.queryByTestId("routing-rule-created")).toBeNull();
  });

  it("hides rule authoring from agents but keeps the guard set readable", async () => {
    renderPage(<OrdersPage />, "agent");
    await waitFor(() => expect(screen.getByTestId("routing-rules-panel")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("routing-rules-toggle"));
    await waitFor(() => expect(screen.getByTestId("routing-rules-list")).toBeInTheDocument());
    expect(screen.queryByTestId("routing-rule-form")).toBeNull();
    expect(screen.queryByTestId("routing-rule-submit")).toBeNull();
    expect(screen.queryByTestId("routing-rule-delete-rule-negative-margin")).toBeNull();
  });

  it("holds a negative-margin order, releases it as manager and books a 4x6 label", async () => {
    renderPage(<OrdersPage />, "manager");
    fireEvent.click(await screen.findByTestId("open-routing-modal-ord-b2b-1"));

    expect(await screen.findByTestId("shipping-label-modal")).toBeInTheDocument();
    const margin = await screen.findByTestId("routing-eval-margin");
    expect(margin).toHaveTextContent("-12.5000");
    expect(screen.getByTestId("routing-hold-alert")).toBeInTheDocument();
    expect(screen.getByTestId("routing-hold-badge")).toBeInTheDocument();
    expect(screen.getByTestId("book-label-submit")).toBeDisabled();
    expect(screen.getByTestId("release-hold-submit")).toBeDisabled();

    fireEvent.change(screen.getByTestId("release-hold-reason"), {
      target: { value: "主管已确认亏损可接受" },
    });
    fireEvent.click(screen.getByTestId("release-hold-submit"));

    await waitFor(() => expect(screen.queryByTestId("routing-hold-alert")).toBeNull());
    expect(screen.getByTestId("routing-ok-badge")).toBeInTheDocument();

    fireEvent.change(screen.getByTestId("label-carrier-select"), {
      target: { value: "yunexpress" },
    });
    fireEvent.click(screen.getByTestId("book-label-submit"));

    const success = await screen.findByTestId("label-booking-success");
    expect(success).toHaveTextContent("SBX");

    const labelRow = await screen.findByTestId("shipping-label-row-label-ord-b2b-1-0");
    expect(labelRow).toHaveTextContent("yunexpress");
    const pdfLink = within(labelRow).getByTestId("download-label-label-ord-b2b-1-0");
    expect(pdfLink).toHaveAttribute(
      "href",
      "/api/bff/v1/orders/ord-b2b-1/shipping-labels/label-ord-b2b-1-0/pdf",
    );
    expect(pdfLink).toHaveAttribute("target", "_blank");
  });

  it("shows the hold to agents without the release controls", async () => {
    renderPage(<OrdersPage />, "agent");
    fireEvent.click(await screen.findByTestId("open-routing-modal-ord-b2b-1"));
    expect(await screen.findByTestId("routing-hold-alert")).toBeInTheDocument();
    expect(screen.queryByTestId("release-hold-reason")).toBeNull();
    expect(screen.getByTestId("book-label-submit")).toBeDisabled();
  });

  it("books straight through for a healthy-margin order", async () => {
    renderPage(<OrdersPage />, "manager");
    fireEvent.click(await screen.findByTestId("open-routing-modal-ord-b2c-1"));
    expect(await screen.findByTestId("routing-ok-badge")).toBeInTheDocument();
    expect(screen.queryByTestId("routing-hold-alert")).toBeNull();

    // DHL has no live booking contract yet: the hint shows and the button locks.
    fireEvent.change(screen.getByTestId("label-carrier-select"), {
      target: { value: "dhl" },
    });
    fireEvent.change(screen.getByTestId("label-mode-select"), {
      target: { value: "live" },
    });
    expect(screen.getByTestId("label-live-mode-hint")).toBeInTheDocument();
    expect(screen.getByTestId("book-label-submit")).toBeDisabled();

    fireEvent.change(screen.getByTestId("label-mode-select"), {
      target: { value: "sandbox" },
    });
    expect(screen.queryByTestId("label-live-mode-hint")).toBeNull();
    expect(screen.getByTestId("book-label-submit")).not.toBeDisabled();

    fireEvent.click(screen.getByTestId("book-label-submit"));
    expect(await screen.findByTestId("label-booking-success")).toHaveTextContent("SBX");
    expect(screen.queryByTestId("label-booking-error")).toBeNull();
  });
});
