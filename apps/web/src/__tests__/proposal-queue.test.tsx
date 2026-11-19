import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nProvider } from "@/lib/i18n";
import { AuthSessionContext } from "@/components/providers";
import AiProposalsPage from "@/app/ai/proposals/page";
import AiCapabilitiesPage from "@/app/ai/capabilities/page";
import type { Principal } from "@/lib/contracts";

function renderWithAuth(ui: React.ReactNode, role: Principal["role"] = "owner") {
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

describe("Task 5.4 L1 Proposal Queue & AI Governance Console", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("displays margin floor, [doc#chunk] citations, and executes Edit & Approve mutation", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/ai/proposals?") || url.endsWith("/ai/proposals")) {
        return new Response(
          JSON.stringify({
            items: [
              {
                id: "prop-101",
                capability: "sales_quoting",
                proposal_type: "quote_draft",
                conversation_id: "conv-1",
                proposed_payload: {
                  sku: "LED-COB-24V-5M",
                  quantity: 500,
                  unit_price_usd: 14.2,
                  incoterm: "FOB",
                },
                citations: [{ doc_id: "doc-led", chunk_id: "c4", score: 0.89 }],
                confidence_score: 0.92,
                estimated_margin_pct: 24.5,
                min_margin_floor_pct: 15.0,
                status: "pending",
                created_at: "2026-11-19T08:10:00Z",
              },
            ],
            total: 1,
            limit: 20,
            offset: 0,
            has_more: false,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/ai/proposals/prop-101/edit-approve") && init?.method === "POST") {
        return new Response(JSON.stringify({ id: "prop-101", status: "edited_and_approved" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response("{}", { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);

    renderWithAuth(<AiProposalsPage />, "owner");

    await waitFor(() => {
      expect(screen.getByText(/24.5%/)).toBeInTheDocument();
      expect(screen.getByText(/15.0%/)).toBeInTheDocument();
      expect(screen.getByText(/\[doc-led#c4\]/)).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId("proposal-edit-toggle-prop-101"));
    const editor = screen.getByTestId("proposal-json-editor-prop-101");
    fireEvent.change(editor, {
      target: {
        value: JSON.stringify({
          sku: "LED-COB-24V-5M",
          quantity: 500,
          unit_price_usd: 13.8,
          incoterm: "FOB",
        }),
      },
    });
    fireEvent.click(screen.getByTestId("proposal-edit-submit-prop-101"));

    await waitFor(() => {
      const editCall = fetchMock.mock.calls.find((c) =>
        String(c[0]).includes("/ai/proposals/prop-101/edit-approve")
      );
      expect(editCall).toBeDefined();
      expect(JSON.parse(String(editCall?.[1]?.body))).toMatchObject({
        edited_payload: { unit_price_usd: 13.8 },
      });
    });
  });

  it("locks finance_recon from L2_auto and displays L0 fallback alert when credits reach 0", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/ai/proposals")) {
          return new Response(
            JSON.stringify({ items: [], total: 0, limit: 1, offset: 0, has_more: false }),
            { status: 200, headers: { "Content-Type": "application/json" } }
          );
        }
        if (url.includes("/billing/wallet")) {
          return new Response(
            JSON.stringify({
              plan_tier: "starter",
              included_credits_remaining: 0,
              topup_credits_balance: 0,
              overage_spent_cents: 5000,
              overage_spend_cap_cents: 5000,
              auto_downgrade_to_l0: true,
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
                  max_allowed_autonomy: "L2_auto",
                },
                {
                  capability: "sales_quoting",
                  configured_autonomy: "L1_proposal",
                  max_allowed_autonomy: "L1_proposal",
                },
                {
                  capability: "order_logistics",
                  configured_autonomy: "L2_auto",
                  max_allowed_autonomy: "L2_auto",
                },
                {
                  capability: "content_studio",
                  configured_autonomy: "L1_proposal",
                  max_allowed_autonomy: "L1_proposal",
                },
                {
                  capability: "biz_analytics",
                  configured_autonomy: "L2_auto",
                  max_allowed_autonomy: "L2_auto",
                },
                {
                  capability: "finance_recon",
                  configured_autonomy: "L0_copilot",
                  max_allowed_autonomy: "L0_copilot",
                },
              ],
            }),
            { status: 200, headers: { "Content-Type": "application/json" } }
          );
        }
        if (url.includes("/ai/kb/documents")) {
          return new Response(
            JSON.stringify({
              items: [
                {
                  id: "kb-1",
                  title: "LED_Warranty_Policy.md",
                  chunk_count: 14,
                  status: "indexed",
                  created_at: "2026-11-19T00:00:00Z",
                },
              ],
              total: 1,
              limit: 20,
              offset: 0,
              has_more: false,
            }),
            { status: 200, headers: { "Content-Type": "application/json" } }
          );
        }
        return new Response("{}", { status: 200 });
      })
    );

    renderWithAuth(<AiCapabilitiesPage />, "owner");

    await waitFor(() => {
      expect(screen.getByTestId("wallet-l0-fallback-alert")).toBeInTheDocument();
      expect(screen.getByTestId("finance-recon-l0-lock")).toBeInTheDocument();
    });

    const financeL2Btn = screen.getByTestId("cap-btn-finance_recon-L2_auto");
    expect(financeL2Btn).toBeDisabled();
    expect(screen.getByText("LED_Warranty_Policy.md")).toBeInTheDocument();
  });
});
