import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nProvider } from "@/lib/i18n";
import { AuthSessionContext } from "@/components/providers";
import { ContextDrawer } from "@/components/inbox/context-drawer";
import type { CustomerContextPayload } from "@/lib/api-client/plan04-bridge";
import { FloatingCopilot, sanitizeCopilotCards } from "@/components/ai/floating-copilot";
import {
  fetchCallBody,
  fetchCallMethod,
  fetchCallUrl,
  normalizeFetchInput,
} from "@/__tests__/fetch-stub";

const MOCK_CUSTOMER_CONTEXT: CustomerContextPayload = {
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
  identities: [
    {
      id: "id-1",
      identity_type: "whatsapp_bsuid",
      identity_value: "bsuid_de_998122",
      confidence_level: "deterministic",
    },
    {
      id: "id-2",
      identity_type: "email",
      identity_value: "hans.weber@berlin-lighting.de",
      confidence_level: "deterministic",
    },
  ],
  probable_duplicates: [
    {
      candidate_customer_id: "cust-dup-9",
      display_name: "H. Weber",
      matched_signal: "email_domain + postal_code",
      similarity_score: 0.91,
    },
  ],
  merge_events: [],
  recent_orders: [
    {
      id: "ord-1",
      order_no: "ORD-2026-001",
      order_mode: "b2c_retail",
      fulfillment_status: "shipped",
      payment_status: "paid",
      total_amount: "189.00",
      currency: "USD",
      tracking_number: "YT26119000123",
      standardized_status: "InTransit",
      sub_status: "InTransit_CustomsCleared",
      is_stagnant_72h: false,
    },
  ],
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

function renderWrap(ui: React.ReactNode, role: "owner" | "agent" = "owner") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AuthSessionContext.Provider
        value={{
          principal: {
            user_id: "u1",
            tenant_id: "t1",
            role,
            email: `${role}@qzled.example.com`,
          },
          isLoading: false,
          logout: async () => {},
          refreshSession: async () => {},
        }}
      >
        <I18nProvider defaultLocale="en">{ui}</I18nProvider>
      </AuthSessionContext.Provider>
    </QueryClientProvider>
  );
}

describe("Task 5.3 ContextDrawer & FloatingCopilot", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("does not request the finance-only credit wallet for an agent", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const { url } = normalizeFetchInput(input);
      if (url.includes("/ai/proposals")) {
        return new Response(JSON.stringify({ items: [], total: 0 }), {
          status: 200, headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/ai/capabilities")) {
        return new Response(JSON.stringify({ capabilities: [], l0_fallback: false }), {
          status: 200, headers: { "Content-Type": "application/json" },
        });
      }
      return new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } });
    });
    vi.stubGlobal("fetch", fetchMock);

    renderWrap(<FloatingCopilot conversationId="conv-1" />, "agent");
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const urls = fetchMock.mock.calls.map((call) => fetchCallUrl(call));
    expect(urls.some((url) => url.includes("/billing/wallet"))).toBe(false);
  });

  it("sanitizes malformed objects and raw dotted internal keys so [object Object] never renders", async () => {
    const sanitized = sanitizeCopilotCards([
      {
        capability: "finance_recon",
        autonomy_level: "L0_copilot",
        metrics: [
          {
            label_key: "monthly_profit_target",
            kind: "money",
            value: { target: 100000 } as unknown as number,
            currency: "CNY",
          },
          {
            label_key: "tax.filing.overdue",
            kind: "count",
            value: 0,
          },
          {
            label_key: "open_receivables_amount",
            kind: "money",
            value: 14500.5,
            currency: "USD",
          },
        ],
      },
    ]);

    expect(sanitized[0].metrics.some((m) => m.label_key === "tax.filing.overdue")).toBe(false);

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const { url } = normalizeFetchInput(input);
        if (url.includes("/ai/proposals")) {
          return new Response(
            JSON.stringify({ items: [], total: 6, limit: 1, offset: 0, has_more: true }),
            { status: 200, headers: { "Content-Type": "application/json" } }
          );
        }
        if (url.includes("/billing/wallet")) {
          return new Response(
            JSON.stringify({
              plan_tier: "Growth",
              included_credits_remaining: 4200,
              topup_credits_balance: 0,
              auto_downgrade_to_l0: false,
            }),
            { status: 200, headers: { "Content-Type": "application/json" } }
          );
        }
        if (url.includes("/ai/capabilities")) {
          return new Response(
            JSON.stringify({
              capabilities: [
                {
                  capability: "cs_support",
                  configured_autonomy: "L2_auto",
                  pending_proposals_7d: 1,
                  executed_actions_7d: 12,
                  metrics: [{ label_key: "open_threads", kind: "count", value: 12 }],
                },
                {
                  capability: "sales_quoting",
                  configured_autonomy: "L1_proposal",
                  pending_proposals_7d: 3,
                  executed_actions_7d: 5,
                  metrics: [{ label_key: "pending_quotes", kind: "count", value: 3 }],
                },
                {
                  capability: "order_logistics",
                  configured_autonomy: "L2_auto",
                  pending_proposals_7d: 1,
                  executed_actions_7d: 8,
                  metrics: [{ label_key: "stagnant_shipments_72h", kind: "count", value: 1 }],
                },
                {
                  capability: "content_studio",
                  configured_autonomy: "L1_proposal",
                  pending_proposals_7d: 1,
                  executed_actions_7d: 4,
                  metrics: [{ label_key: "drafts_ready", kind: "count", value: 4 }],
                },
                {
                  capability: "biz_analytics",
                  configured_autonomy: "L2_auto",
                  pending_proposals_7d: 0,
                  executed_actions_7d: 2,
                  metrics: [{ label_key: "net_margin_pct", kind: "percent", value: 28.4 }],
                },
                {
                  capability: "finance_recon",
                  configured_autonomy: "L0_copilot",
                  pending_proposals_7d: 0,
                  executed_actions_7d: 0,
                  metrics: [
                    {
                      label_key: "monthly_profit_target",
                      kind: "money",
                      value: { broken: true },
                      currency: "CNY",
                    },
                    { label_key: "tax.filing.overdue", kind: "count", value: 0 },
                    {
                      label_key: "open_receivables_amount",
                      kind: "money",
                      value: 14500.5,
                      currency: "USD",
                    },
                  ],
                },
              ],
            }),
            { status: 200, headers: { "Content-Type": "application/json" } }
          );
        }
        if (url.includes("/ai/assistant/chat")) {
          return new Response(
            JSON.stringify({
              reply_text: "COB LED 24V carries a 3-year warranty [doc-warranty#c1].",
              citations: [{ doc_id: "doc-warranty", chunk_id: "c1", score: 0.94 }],
              tool_calls: [{ tool_call_id: "tool-call-77", tool_name: "get_live_sku_pricing_and_stock" }],
              should_abstain: false,
            }),
            { status: 200, headers: { "Content-Type": "application/json" } }
          );
        }
        return new Response("{}", { status: 200 });
      })
    );

    renderWrap(<FloatingCopilot conversationId="conv-1" />);

    const fab = await screen.findByTestId("copilot-fab-btn");
    // The pending-proposals badge renders once the query resolves; wait for
    // it instead of asserting synchronously (the fab itself mounts first).
    await waitFor(() => expect(fab).toHaveTextContent("6"));
    fireEvent.click(fab);

    await waitFor(() => {
      expect(screen.getByTestId("copilot-drawer")).toBeInTheDocument();
    });

    const drawerText = screen.getByTestId("copilot-drawer").textContent ?? "";
    expect(drawerText).not.toContain("[object Object]");
    expect(drawerText).not.toContain("tax.filing.overdue");
    expect(drawerText).toContain("$14,500.50");
    expect(drawerText).toContain("28.4%");

    const proposalsLink = screen.getByTestId("copilot-pending-link");
    expect(proposalsLink).toHaveAttribute("href", "/ai/proposals");

    fireEvent.click(screen.getByTestId("copilot-mode-strict-rag"));
    fireEvent.change(screen.getByTestId("copilot-query-input"), {
      target: { value: "What is the warranty?" },
    });
    fireEvent.click(screen.getByTestId("copilot-query-submit"));

    await waitFor(() => {
      expect(screen.getByText(/doc-warranty#c1/)).toBeInTheDocument();
      expect(screen.getByText(/tool-call-77/)).toBeInTheDocument();
    });
  });

  it("renders BSUID badge, switches ContextDrawer tabs, and executes probable duplicate merge", async () => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
    try {
      await exerciseContextDrawer();
    } finally {
      vi.unstubAllEnvs();
    }
  });

  async function exerciseContextDrawer() {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const { url, method } = normalizeFetchInput(input, init);
      if (url.includes("/identity/customers/cust-target/context")) {
        return new Response(JSON.stringify(MOCK_CUSTOMER_CONTEXT), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/customers/merge") && method === "POST") {
        return new Response(
          JSON.stringify({ merge_event_id: "me-1", target_customer_id: "cust-target" }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/catalog/live-check")) {
        return new Response(
          JSON.stringify({
            sku: "LED-COB-24V-5M",
            available_qty: 1240,
            unit_price_quote_ccy: "14.20",
            moq_tier_applied: 500,
            incoterm: "FOB",
            source_tool_call_id: "sql-sku-99",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      return new Response("{}", { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);

    renderWrap(<ContextDrawer customerId="cust-target" />);

    await waitFor(() => {
      expect(screen.getByText("bsuid_de_998122")).toBeInTheDocument();
    });

    expect(screen.getByText("ORD-2026-001")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /B2B Quotes & PI/i }));
    expect(screen.getByText("PI-202611-0042")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Live SKU & MOQ Stock/i }));
    fireEvent.change(screen.getByTestId("live-sku-input"), {
      target: { value: "LED-COB-24V-5M" },
    });
    fireEvent.click(screen.getByTestId("live-sku-check-btn"));

    await waitFor(() => {
      expect(screen.getByText(/1240/)).toBeInTheDocument();
      expect(screen.getByText(/sql-sku-99/)).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId("open-merge-dialog-btn"));
    fireEvent.click(screen.getByTestId("confirm-merge-cust-dup-9"));

    await waitFor(async () => {
      const mergeCall = fetchMock.mock.calls.find((c) =>
        fetchCallUrl(c).includes("/customers/merge"),
      );
      expect(mergeCall).toBeDefined();
      expect(fetchCallMethod(mergeCall!)).toBe("POST");
      expect(JSON.parse(await fetchCallBody(mergeCall!))).toMatchObject({
        source_customer_id: "cust-dup-9",
        target_customer_id: "cust-target",
      });
    });
  }

  it("sends cs_support for global copilot mode and renders accurate non-RAG abstention reason with empty reply fallback", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const { url, method } = normalizeFetchInput(input, init);
      if (url.includes("/ai/proposals")) {
        return Response.json({ items: [], total: 0, limit: 1, offset: 0, has_more: false });
      }
      if (url.includes("/billing/wallet")) {
        return Response.json({
          plan_tier: "Growth",
          included_credits_remaining: 4000,
          topup_credits_balance: 0,
          auto_downgrade_to_l0: false,
        });
      }
      if (url.includes("/ai/capabilities")) {
        return Response.json({ capabilities: [] });
      }
      if (url.includes("/ai/assistant/chat") && method === "POST") {
        return Response.json({
          reply_text: "",
          citations: [],
          tool_calls: [{ tool_call_id: "tc-99", tool_name: "get_live_sku_pricing_and_stock" }],
          should_abstain: true,
          reason: "grounding_verification_failed",
        });
      }
      return Response.json({});
    });
    vi.stubGlobal("fetch", fetchMock);

    renderWrap(<FloatingCopilot />, "owner");
    fireEvent.click(await screen.findByTestId("copilot-fab-btn"));
    await waitFor(() => expect(screen.getByTestId("copilot-drawer")).toBeInTheDocument());

    fireEvent.change(screen.getByTestId("copilot-query-input"), {
      target: { value: "Check LED-COB-24V-5M price" },
    });
    fireEvent.click(screen.getByTestId("copilot-query-submit"));

    await waitFor(async () => {
      const chatCall = fetchMock.mock.calls.find((c) =>
        fetchCallUrl(c).includes("/ai/assistant/chat"),
      );
      expect(chatCall).toBeDefined();
      expect(JSON.parse(await fetchCallBody(chatCall!))).toMatchObject({
        capability: "cs_support",
        strict_rag: false,
        message: "Check LED-COB-24V-5M price",
      });
    });

    const banner = await screen.findByTestId("copilot-abstain-banner");
    expect(banner).toHaveTextContent("SQL grounding check failed");
    expect(banner).not.toHaveTextContent("Strict RAG < 0.62");
    expect(screen.getByTestId("copilot-abstain-fallback")).toBeInTheDocument();
  });

  it("renders unverified pre-chat lead details clearly in ContextDrawer without merging identities", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const { url } = normalizeFetchInput(input);
        if (url.includes("/customers/cust-target")) {
          return Response.json({
            id: "cust-target",
            tenant_id: "t1",
            display_name: "Visitor 9f1e2d",
            customer_type: "b2c_consumer",
            ltv_usd: "0.00",
            lifecycle_stage: "lead",
            created_at: "2026-11-26T00:00:00Z",
            identities: [
              {
                id: "id-web-1",
                identity_type: "web_visitor_id",
                identity_value: "visitor-uuid-1",
                confidence_level: "deterministic",
              },
            ],
            merge_proposals: [],
          });
        }
        if (url.includes("/orders") || url.includes("/b2b/opportunities")) {
          return Response.json({ items: [], total: 0, limit: 5, offset: 0, has_more: false });
        }
        return Response.json({});
      }),
    );

    renderWrap(
      <ContextDrawer
        customerId="cust-target"
        unverifiedPrechat={{
          email: "buyer.b2b.audit@nordiclight.de",
          whatsapp_phone: "+4915123456789",
          marketing_opt_in: true,
        }}
      />,
      "owner",
    );

    const card = await screen.findByTestId("unverified-prechat-card");
    expect(card).toHaveTextContent("Pre-chat lead (unverified)");
    expect(card).toHaveTextContent("buyer.b2b.audit@nordiclight.de");
    expect(card).toHaveTextContent("+4915123456789");
    expect(card).toHaveTextContent("Marketing opt-in");
    expect(screen.getByTestId("identity-badge-web_visitor_id")).toBeInTheDocument();
  });
});
