import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nProvider } from "@/lib/i18n";
import {
  ContextDrawer,
  buildSkuReplySnippet,
} from "@/components/inbox/context-drawer";
import { normalizeFetchInput } from "@/__tests__/fetch-stub";

// The drawer reads the customer context through plan04-bridge in mock mode, so
// its payload stays fully stubbed at the fetch boundary.
vi.mock("@/lib/data-source", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/data-source")>();
  return { ...actual, shouldUseMockApi: () => true };
});

const CUSTOMER_CONTEXT = {
  customer: {
    id: "cust-target",
    display_name: "Hans Weber",
    customer_type: "b2b_company_rep",
    ltv_usd: "42500.00",
    lifecycle_stage: "qualified",
  },
  company: {
    id: "comp-1",
    legal_name: "Berlin Lighting GmbH",
    country_code: "DE",
    credit_tier: "prepaid",
    default_incoterm: "FOB",
  },
  identities: [],
  probable_duplicates: [],
  merge_events: [],
  recent_orders: [],
  recent_quotes: [
    {
      id: "q-1",
      quote_no: "PI-202611-0042",
      version: 1,
      incoterm: "FOB",
      total_amount: "7100.00",
      quote_currency: "USD",
      status: "sent",
    },
  ],
};

function wrap(ui: React.ReactNode, locale: "zh" | "en" = "en") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <I18nProvider defaultLocale={locale}>{ui}</I18nProvider>
    </QueryClientProvider>
  );
}

function stubDrawerFetch() {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const { url } = normalizeFetchInput(input, init);
    if (url.includes("/identity/customers/cust-target/context")) {
      return Response.json(CUSTOMER_CONTEXT);
    }
    if (url.includes("/catalog/variants")) {
      return Response.json({
        items: [
          {
            id: "var-1",
            product_id: "prod-1",
            sku: "LED-COB-24V-5M",
            attributes: {},
            cost_price_base: "45.50",
            base_currency: "CNY",
            min_margin_floor_pct: "15.00",
            retail_price_usd: "29.90",
            b2b_moq_tiers: [{ min_qty: 500, unit_price_usd: "14.20" }],
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
      });
    }
    if (url.includes("/catalog/live-check")) {
      return Response.json({
        sku: "LED-COB-24V-5M",
        available_qty: 1240,
        unit_price_quote_ccy: "14.20",
        moq_tier_applied: 500,
        incoterm: "FOB",
        source_tool_call_id: "sql-sku-99",
      });
    }
    return Response.json({});
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("Track 3 · Inbox context drawer quick insert & quote deep links", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("builds a buyer-ready stock snippet with the real MOQ tier price and lead time", () => {
    const result = {
      sku: "LED-COB-24V-5M",
      available_qty: 1240,
      unit_price_quote_ccy: "14.20",
      moq_tier_applied: 500,
      incoterm: "FOB",
      source_tool_call_id: "sql-sku-99",
    };

    expect(buildSkuReplySnippet(result, 7, "en")).toBe(
      "LED-COB-24V-5M — 1,240 pcs in stock\nMOQ 500 tier price $14.20/pc (FOB)\nLead time: 7 days"
    );
    expect(buildSkuReplySnippet(result, 7, "zh")).toBe(
      "LED-COB-24V-5M：现货 1,240 pcs\nMOQ 500 阶梯价 $14.20/pc（FOB）\n交期：7 天"
    );
    expect(buildSkuReplySnippet(result, null, "en")).not.toContain("Lead time");
  });

  it("inserts live stock + MOQ tier price straight into the reply composer", async () => {
    stubDrawerFetch();
    const onInsertReplyText = vi.fn();

    wrap(
      <ContextDrawer customerId="cust-target" onInsertReplyText={onInsertReplyText} />,
      "en"
    );

    await waitFor(() => {
      expect(screen.getByText("Hans Weber")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: /Live SKU & MOQ Stock/i }));
    fireEvent.change(screen.getByTestId("live-sku-input"), {
      target: { value: "LED-COB-24V-5M" },
    });
    fireEvent.click(screen.getByTestId("live-sku-check-btn"));

    const insertBtn = await screen.findByTestId("insert-sku-to-reply-LED-COB-24V-5M");
    // Lead time is joined from the catalog variant; wait for it before inserting.
    await waitFor(() => {
      expect(screen.getByText(/Lead time/)).toBeInTheDocument();
    });

    fireEvent.click(insertBtn);

    expect(onInsertReplyText).toHaveBeenCalledWith(
      "LED-COB-24V-5M — 1,240 pcs in stock\nMOQ 500 tier price $14.20/pc (FOB)\nLead time: 7 days"
    );
  });

  it("deep-links a new quote and the PI email flow for the active buyer", async () => {
    stubDrawerFetch();

    wrap(<ContextDrawer customerId="cust-target" />, "en");

    await waitFor(() => {
      expect(screen.getByText("Hans Weber")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: /B2B Quotes & PI/i }));

    const newQuote = screen.getByTestId("context-new-quote-link");
    expect(newQuote.getAttribute("href")).toBe(
      "/b2b/quotes?customer_id=cust-target&customer_name=Hans%20Weber"
    );
    expect(newQuote.textContent).toContain("Create a quote for this buyer");

    const sendPi = screen.getByTestId("context-quote-send-pi-link-q-1");
    expect(sendPi.getAttribute("href")).toBe(
      "/b2b/quotes?quoteId=q-1&customer_id=cust-target&customer_name=Hans%20Weber&send_pi_email=1"
    );
    expect(sendPi.textContent).toContain("Send PI email");
  });

  it("hides the quick-insert button when no composer callback is wired", async () => {
    stubDrawerFetch();

    wrap(<ContextDrawer customerId="cust-target" />, "en");
    await waitFor(() => {
      expect(screen.getByText("Hans Weber")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: /Live SKU & MOQ Stock/i }));
    fireEvent.change(screen.getByTestId("live-sku-input"), {
      target: { value: "LED-COB-24V-5M" },
    });
    fireEvent.click(screen.getByTestId("live-sku-check-btn"));

    await waitFor(() => {
      expect(screen.getByText(/sql-sku-99/)).toBeInTheDocument();
    });
    expect(screen.queryByTestId("insert-sku-to-reply-LED-COB-24V-5M")).toBeNull();
  });
});
