// Track 10 frontend: buyer-portal one-click reorder, the warehouse batch print run +
// scan-to-ship bar on /orders, and the two new automation template labels.
//
// Client specs stub global fetch and assert the exact same-origin paths/bodies; UI specs
// run against the fixture store (`NEXT_PUBLIC_USE_MOCK_API=true`) so they need no backend.
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import OrdersPage from "@/app/orders/page";
import { GET as portalProxyGet } from "@/app/api/portal/[...path]/route";
import { NotificationAutomationPanel } from "@/components/ai/notification-automation-panel";
import { BuyerPortalPageView } from "@/components/b2b/buyer-portal-view";
import { BatchDispatchBar } from "@/components/orders/batch-dispatch-bar";
import { AuthSessionContext, type AuthSessionState } from "@/components/providers";
import { I18nProvider } from "@/lib/i18n";
import type { Principal } from "@/lib/contracts";
import {
  BATCH_SKIP_LABELS,
  batchLabelsPath,
  batchLabelsPdfPath,
  bookBatchShippingLabels,
  scanDispatch,
  scanDispatchPath,
} from "@/lib/api-client/track10-polish";
import { reorderBuyerPortalQuote } from "@/lib/api-client/track2-portal";
import {
  RULE_COPY,
  RULE_THRESHOLD_FIELD,
  templateLabel,
} from "@/lib/api-client/track1-notifications";
import { resetTrack5MockStore } from "@/lib/api-client/track5-routing-labels";

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

function renderWithProviders(ui: React.ReactNode, role: Principal["role"] = "manager") {
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
// Public buyer-portal proxy
// ---------------------------------------------------------------------------

describe("portal proxy reorder allowlist", () => {
  it("proxies reorder (and the two pre-existing actions) but nothing else", () => {
    expect(portalProxyGet.resolvePortalUpstreamPath(["erp_b2b_TOKEN123", "reorder"])).toBe(
      "/api/v1/portal/erp_b2b_TOKEN123/reorder"
    );
    expect(portalProxyGet.resolvePortalUpstreamPath(["erp_b2b_TOKEN123", "accept"])).toBe(
      "/api/v1/portal/erp_b2b_TOKEN123/accept"
    );
    expect(portalProxyGet.resolvePortalUpstreamPath(["erp_b2b_TOKEN123", "receipt"])).toBe(
      "/api/v1/portal/erp_b2b_TOKEN123/receipt"
    );
    expect(
      portalProxyGet.resolvePortalUpstreamPath(["erp_b2b_TOKEN123", "documents", "pi"])
    ).toBe("/api/v1/portal/erp_b2b_TOKEN123/documents/pi");
    expect(
      portalProxyGet.resolvePortalUpstreamPath(["erp_b2b_TOKEN123", "orders"])
    ).toBeNull();
    expect(portalProxyGet.resolvePortalUpstreamPath(["not-a-token", "reorder"])).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Buyer-portal reorder client + view
// ---------------------------------------------------------------------------

function mockBuyerView() {
  return {
    seller: {
      company_name: "Quanzhou Smart LED Lighting Co., Ltd.",
      address: "88 Export Avenue, Quanzhou, Fujian, China",
      phone: "+86 595 8888 0000",
      email: "export@qzled.example.com",
      website: "https://qzled.example.com",
      tax_or_reg_no: "91350500MA2XXXXX1A",
      bank_beneficiary_name: "Quanzhou Smart LED Lighting Co., Ltd.",
      bank_name: "Bank of China, Quanzhou Branch",
      bank_account_no: "4060 1234 5678 9012 345",
      bank_swift_code: "BKCHCNBJ73A",
      bank_address: "Quanzhou, Fujian, China",
      terms_notes: "30% T/T deposit, 70% balance before shipment.",
    },
    quote: {
      quote_id: "quote-1",
      quote_no: "PI-202611-0042",
      version: 1,
      status: "sent",
      incoterm: "FOB",
      port_of_loading: "Xiamen, China",
      port_of_destination: "Hamburg, Germany",
      payment_terms: "30% T/T deposit, 70% before shipment",
      quote_currency: "USD",
      fx_rate_snapshot: "7.200000",
      fx_locked_until: "2026-12-10T00:00:00.000Z",
      freight_fee: "200.00",
      insurance_fee: "0.00",
      customs_duty_estimate: "0.00",
      total_amount: "4450.00",
      items: [
        {
          sku: "LED-STRIP-5M-IP65",
          description: "5M IP65 Smart LED Strip 24V",
          quantity: 500,
          unit_price: "8.5000",
          line_total: "4250.00",
        },
      ],
    },
    order: null,
    can_accept: true,
    can_upload_receipt: false,
    accepted_at: null,
    accepted_by_name: null,
  };
}

describe("buyer portal reorder", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("posts to the public portal proxy with no session cookie", async () => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "false");
    const captured = stubFetch(() =>
      jsonResponse({
        new_quote_id: "quote-2",
        new_quote_no: "PI-REO-202611-ABC123",
        total_amount: "4450.00",
        currency: "USD",
        status: "draft",
        created_at: "2026-11-26T09:00:00.000Z",
      })
    );

    const result = await reorderBuyerPortalQuote("erp_b2b_abc123", {});
    expect(result.new_quote_no).toBe("PI-REO-202611-ABC123");
    expect(result.status).toBe("draft");
    expect(captured[0].url).toBe("/api/portal/erp_b2b_abc123/reorder");
    expect(captured[0].method).toBe("POST");
    expect(JSON.parse(captured[0].bodyText ?? "{}")).toEqual({});
  });

  it("renders the 一键复购 button and its confirmation banner", async () => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "false");
    const captured = stubFetch((call) => {
      if (call.url.endsWith("/reorder")) {
        return jsonResponse(
          {
            new_quote_id: "quote-2",
            new_quote_no: "PI-REO-202611-ABC123",
            total_amount: "4450.00",
            currency: "USD",
            status: "draft",
            created_at: "2026-11-26T09:00:00.000Z",
          },
          201
        );
      }
      return jsonResponse(mockBuyerView());
    });

    render(
      <I18nProvider defaultLocale="zh">
        <BuyerPortalPageView token="erp_b2b_abc123" />
      </I18nProvider>
    );

    const button = await screen.findByTestId("portal-reorder-button");
    expect(button).toHaveTextContent("Request Reorder");
    expect(screen.queryByTestId("portal-reorder-banner")).toBeNull();

    fireEvent.click(button);

    const banner = await screen.findByTestId("portal-reorder-banner");
    expect(banner).toHaveTextContent("PI-REO-202611-ABC123");
    expect(screen.getByTestId("portal-reorder-total")).toHaveTextContent("$4,450.00");
    expect(captured.some((call) => call.url.endsWith("/reorder"))).toBe(true);
  });

  it("surfaces a rejected reorder instead of pretending it succeeded", async () => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "false");
    stubFetch((call) => {
      if (call.url.endsWith("/reorder")) {
        return jsonResponse(
          {
            type: "https://erp.local/errors/invalid-portal-token",
            title: "Invalid Portal Link",
            status: 401,
            detail: "This buyer portal link has been revoked.",
            code: "ERR_INVALID_PORTAL_TOKEN",
          },
          401
        );
      }
      return jsonResponse(mockBuyerView());
    });

    render(
      <I18nProvider defaultLocale="zh">
        <BuyerPortalPageView token="erp_b2b_abc123" />
      </I18nProvider>
    );
    fireEvent.click(await screen.findByTestId("portal-reorder-button"));
    const error = await screen.findByTestId("portal-reorder-error");
    expect(error).toHaveTextContent("revoked");
    expect(screen.queryByTestId("portal-reorder-banner")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Batch print run + scan-to-ship client contract
// ---------------------------------------------------------------------------

describe("track10 batch/scan BFF contract", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("books a batch and merges the label PDFs", async () => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "false");
    const captured = stubFetch((call) => {
      if (call.url.endsWith("/batch-pdf")) {
        return new Response("%PDF-1.4 merged", {
          status: 200,
          headers: { "Content-Type": "application/pdf" },
        });
      }
      return jsonResponse(
        {
          booked: [
            {
              order_id: "ord-1",
              order_no: "ORD-1",
              label_id: "label-1",
              tracking_number: "SBX0001",
              waybill_number: "SBX0001",
              carrier_code: "sandbox_carrier",
              service_code: "Sandbox-Standard",
              mode: "sandbox",
              reused: false,
              shipping_cost_estimate: "12.50",
              currency: "USD",
            },
          ],
          skipped: [],
        },
        201
      );
    });

    const result = await bookBatchShippingLabels({
      order_ids: ["ord-1"],
      carrier_code: "sandbox_carrier",
      mode: "sandbox",
    });
    expect(result.booked?.[0].tracking_number).toBe("SBX0001");
    expect(captured[0].url).toBe(`/api/bff${batchLabelsPath()}`);
    expect(captured[0].method).toBe("POST");
    expect(JSON.parse(captured[0].bodyText ?? "{}")).toEqual({
      order_ids: ["ord-1"],
      carrier_code: "sandbox_carrier",
      mode: "sandbox",
    });

    const pdf = await (await import("@/lib/api-client/track10-polish")).fetchBatchLabelsPdf({
      label_ids: ["label-1"],
    });
    expect(pdf.type).toBe("application/pdf");
    expect(captured[1].url).toBe(`/api/bff${batchLabelsPdfPath()}`);
    expect(JSON.parse(captured[1].bodyText ?? "{}")).toEqual({ label_ids: ["label-1"] });
  });

  it("posts the scanned barcode and propagates the 409 refusal code", async () => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "false");
    const captured = stubFetch((call) =>
      call.url.endsWith("/scan-dispatch") && JSON.parse(call.bodyText ?? "{}").code === "SBX0001"
        ? jsonResponse({
            order_id: "ord-1",
            order_no: "ORD-1",
            tracking_number: "SBX0001",
            status: "shipped",
            dispatched_at: "2026-11-26T09:00:00.000Z",
          })
        : jsonResponse(
            {
              type: "https://erp.local/errors/balance-payment-required",
              title: "Balance Payment Required",
              status: 409,
              detail: "Order ORD-B2B-2026-01 still owes the 70% B2B balance.",
              code: "ERR_BALANCE_PAYMENT_REQUIRED",
            },
            409
          )
    );

    const shipped = await scanDispatch("SBX0001");
    expect(shipped.status).toBe("shipped");
    expect(captured[0].url).toBe(`/api/bff${scanDispatchPath()}`);
    expect(JSON.parse(captured[0].bodyText ?? "{}")).toEqual({ code: "SBX0001" });

    await expect(scanDispatch("ORD-B2B-2026-01")).rejects.toThrow(
      /70% B2B balance/
    );
  });
});

// ---------------------------------------------------------------------------
// Batch dispatch bar (fixture store)
// ---------------------------------------------------------------------------

const BATCH_ORDERS = [
  { id: "ord-b2c-1", order_no: "ORD-B2C-2026-88" },
  { id: "ord-b2b-1", order_no: "ORD-B2B-2026-01" },
];

describe("batch dispatch bar", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("books the shippable orders, reports the skipped balance and merges the PDF", async () => {
    const createObjectURL = vi.fn(() => "blob:track10-merged-labels");
    vi.stubGlobal("URL", { ...URL, createObjectURL });
    renderWithProviders(
      <BatchDispatchBar
        orders={BATCH_ORDERS}
        selectedOrderIds={["ord-b2c-1", "ord-b2b-1"]}
      />
    );
    expect(screen.getByTestId("batch-selected-count")).toHaveTextContent("2");

    fireEvent.click(screen.getByTestId("batch-label-button"));

    const summary = await screen.findByTestId("batch-label-summary");
    expect(summary).toHaveTextContent("已出 1 张面单，跳过 1 单");
    const bookedList = screen.getByTestId("batch-booked-list");
    expect(within(bookedList).getByText(/ORD-B2C-2026-88/)).toBeInTheDocument();
    expect(screen.getByTestId("batch-skip-ord-b2b-1")).toHaveTextContent(
      BATCH_SKIP_LABELS.balance_unpaid.zh
    );
    const pdfLink = await screen.findByTestId("batch-pdf-link");
    expect(pdfLink).toHaveAttribute("href", "blob:track10-merged-labels");
    expect(pdfLink).toHaveAttribute("download", "shipping-labels-batch.pdf");
    expect(createObjectURL).toHaveBeenCalledTimes(1);
  });

  it("disables the print run until a row is selected", () => {
    renderWithProviders(<BatchDispatchBar orders={BATCH_ORDERS} selectedOrderIds={[]} />);
    expect(screen.getByTestId("batch-label-button")).toBeDisabled();
    expect(screen.getByTestId("batch-selected-count")).toHaveTextContent("0");
  });

  it("scans a waybill into shipped and explains a blocked balance", async () => {
    renderWithProviders(<BatchDispatchBar orders={BATCH_ORDERS} selectedOrderIds={[]} />);

    fireEvent.change(screen.getByTestId("scan-code-input"), {
      target: { value: "SBX000123456" },
    });
    fireEvent.click(screen.getByTestId("scan-submit"));
    const feedback = await screen.findByTestId("scan-feedback");
    expect(feedback).toHaveTextContent("ORD-B2C-2026-88");
    expect(feedback).toHaveTextContent("shipped");

    fireEvent.change(screen.getByTestId("scan-code-input"), {
      target: { value: "ORD-B2B-2026-01" },
    });
    fireEvent.click(screen.getByTestId("scan-submit"));
    const error = await screen.findByTestId("scan-error");
    expect(error).toHaveTextContent("70% 尾款未结清");
  });

  it("keeps the scan button disabled for an empty barcode", () => {
    renderWithProviders(<BatchDispatchBar orders={BATCH_ORDERS} selectedOrderIds={[]} />);
    expect(screen.getByTestId("scan-submit")).toBeDisabled();
    fireEvent.change(screen.getByTestId("scan-code-input"), { target: { value: "  " } });
    expect(screen.getByTestId("scan-submit")).toBeDisabled();
  });
});

// ---------------------------------------------------------------------------
// /orders page integration
// ---------------------------------------------------------------------------

describe("/orders batch dispatch integration", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
    resetTrack5MockStore();
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("selects rows with checkboxes and drives the print run from the selection", async () => {
    renderWithProviders(<OrdersPage />, "manager");
    await waitFor(() => expect(screen.getByTestId("batch-dispatch-bar")).toBeInTheDocument());

    const b2c = await screen.findByTestId("order-select-ord-b2c-1");
    fireEvent.click(b2c);
    await waitFor(() =>
      expect(screen.getByTestId("batch-selected-count")).toHaveTextContent("1")
    );

    fireEvent.click(screen.getByTestId("batch-label-button"));
    const summary = await screen.findByTestId("batch-label-summary");
    expect(summary).toHaveTextContent("已出 1 张面单");
    // The selection is cleared after a successful print run.
    await waitFor(() =>
      expect(screen.getByTestId("batch-selected-count")).toHaveTextContent("0")
    );
  });

  it("selects and clears every row with the header checkbox", async () => {
    renderWithProviders(<OrdersPage />, "manager");
    // Wait for the fixture rows: select-all maps over the loaded order list.
    await screen.findByTestId("order-select-ord-b2b-1");
    const selectAll = await screen.findByTestId("order-select-all");
    fireEvent.click(selectAll);
    await waitFor(() =>
      expect(screen.getByTestId("batch-selected-count")).toHaveTextContent("2")
    );
    fireEvent.click(screen.getByTestId("order-select-all"));
    await waitFor(() =>
      expect(screen.getByTestId("batch-selected-count")).toHaveTextContent("0")
    );
  });
});

// ---------------------------------------------------------------------------
// Automation template labels (7 templates)
// ---------------------------------------------------------------------------

describe("automation template labels", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("labels and thresholds the two Track 10 templates bilingually", () => {
    expect(templateLabel("negative_review_ticket", "zh")).toBe("差评 / 投诉工单");
    expect(templateLabel("negative_review_ticket", "en")).toBe("Negative review ticket");
    expect(templateLabel("high_adoption_faq_extract", "zh")).toBe("高采纳回复转 FAQ");
    expect(templateLabel("high_adoption_faq_extract", "en")).toBe("High-adoption FAQ extract");
    expect(Object.keys(RULE_COPY)).toHaveLength(7);
    expect(Object.keys(RULE_THRESHOLD_FIELD)).toHaveLength(7);
    expect(RULE_THRESHOLD_FIELD.negative_review_ticket.key).toBe("lookback_hours");
    expect(RULE_THRESHOLD_FIELD.high_adoption_faq_extract).toMatchObject({
      key: "min_confidence",
      kind: "decimal",
    });
  });

  it("renders all seven templates in the /ai/employees automation panel", async () => {
    renderWithProviders(<NotificationAutomationPanel canManage />, "owner");
    const panel = await screen.findByTestId("track1-notification-panel");
    // Every seeded template renders exactly one editable card.
    for (const code of [
      "low_doc_replenishment",
      "negative_margin_hold",
      "stalled_shipment_soothe",
      "cold_b2b_quote_followup",
      "daily_patrol_brief",
      "negative_review_ticket",
      "high_adoption_faq_extract",
    ]) {
      expect(await screen.findByTestId(`rule-card-${code}`)).toBeInTheDocument();
    }
    expect(panel).toHaveTextContent("差评 / 投诉工单");
    expect(panel).toHaveTextContent("高采纳回复转 FAQ");
    expect(screen.getByTestId("rule-threshold-negative_review_ticket")).toBeInTheDocument();
    expect(screen.getByTestId("rule-threshold-high_adoption_faq_extract")).toBeInTheDocument();
  });
});
