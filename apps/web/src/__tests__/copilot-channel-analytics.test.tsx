// Copilot `biz_analytics` mode: RBAC-gated entry points, the exact request
// envelope (capability + context.window), and the structured channel panel
// rendered from the deterministic SQL tool payload. The backend allows
// BIZ_ANALYTICS for owner | admin | manager | finance and answers `agent`
// with 403, so the UI must not offer it to agents.
import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nProvider } from "@/lib/i18n";
import { AuthSessionContext } from "@/components/providers";
import { FloatingCopilot } from "@/components/ai/floating-copilot";
import {
  fetchCallBody,
  fetchCallMethod,
  fetchCallUrl,
  normalizeFetchInput,
} from "@/__tests__/fetch-stub";

type Role = "owner" | "agent" | "manager";

const CHANNEL_ANALYTICS_PAYLOAD = {
  reply_text: "Channel analytics for the selected window (deterministic SQL).",
  citations: [],
  should_abstain: false,
  tool_calls: [
    {
      tool_call_id: "tc_analytics_123",
      tool_name: "get_live_channel_sales_analytics",
      data: {
        window: "30d",
        channel_types_filter: [],
        totals: {
          channel_key: "all",
          channel_label: "All Channels",
          order_count: 1420,
          gmv_base: "128900.00",
          refunded_amount_base: "3029.15",
          refund_rate_pct: "2.35",
          ad_spend_base: "18420.00",
          net_margin_base: "24310.40",
          margin_pct: "18.86",
          roas: "7.00",
          window: "30d",
        },
        channels: [
          {
            channel_key: "acc-shopee",
            channel_type: "shopee",
            channel_label: "Shopee MY",
            order_count: 640,
            gmv_base: "62400.00",
            refund_rate_pct: "3.10",
            net_margin_base: "11800.00",
            roas: "6.40",
          },
          {
            channel_key: "acc-temu",
            channel_type: "temu",
            channel_label: "Temu US",
            order_count: 420,
            gmv_base: "38800.00",
            refund_rate_pct: "1.85",
            net_margin_base: "7350.00",
            roas: "8.25",
          },
          {
            channel_key: "acc-shopify",
            channel_type: "shopify",
            channel_label: "Shopify DTC",
            order_count: 360,
            gmv_base: "27700.00",
            refund_rate_pct: "0.60",
            net_margin_base: "5160.40",
            roas: "5.10",
          },
        ],
        source_tool_call_id: "tc_analytics_123",
      },
    },
  ],
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function stubCopilotFetch(chatResponse: unknown = CHANNEL_ANALYTICS_PAYLOAD) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const { url, method } = normalizeFetchInput(input, init);
    if (url.includes("/ai/assistant/chat") && method === "POST") {
      return jsonResponse(chatResponse);
    }
    if (url.includes("/ai/proposals")) {
      return jsonResponse({ items: [], total: 0, limit: 1, offset: 0, has_more: false });
    }
    if (url.includes("/billing/wallet")) {
      return jsonResponse({
        plan_tier: "Growth",
        included_credits_remaining: 1000,
        topup_credits_balance: 0,
        auto_downgrade_to_l0: false,
      });
    }
    if (url.includes("/ai/capabilities")) {
      return jsonResponse({ capabilities: [], l0_fallback: false });
    }
    return jsonResponse({});
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderWrap(ui: React.ReactNode, role: Role = "owner") {
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

async function openDrawer() {
  const fab = await screen.findByTestId("copilot-fab-btn");
  fireEvent.click(fab);
  await waitFor(() => expect(screen.getByTestId("copilot-drawer")).toBeInTheDocument());
}

describe("FloatingCopilot channel analytics (biz_analytics)", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("shows the analytics mode tab and channel chip to owner", async () => {
    stubCopilotFetch();
    renderWrap(<FloatingCopilot conversationId="conv-analytics" />, "owner");
    await openDrawer();

    expect(screen.getByTestId("copilot-mode-analytics")).toBeInTheDocument();
    expect(screen.getByTestId("copilot-chip-channel-analytics")).toBeInTheDocument();
  });

  it("hides the analytics mode tab and channel chip from agent (backend 403)", async () => {
    stubCopilotFetch();
    renderWrap(<FloatingCopilot conversationId="conv-agent" />, "agent");
    await openDrawer();

    // Backend RBAC: BIZ_ANALYTICS is rejected with 403 for the `agent` role.
    expect(screen.queryByTestId("copilot-mode-analytics")).toBeNull();
    expect(screen.queryByTestId("copilot-chip-channel-analytics")).toBeNull();
    expect(screen.getByTestId("copilot-mode-global")).toBeInTheDocument();
    expect(screen.getByTestId("copilot-mode-strict-rag")).toBeInTheDocument();
  });

  it("sends capability biz_analytics with context.window 30d and renders per-channel rows", async () => {
    const fetchMock = stubCopilotFetch();
    renderWrap(<FloatingCopilot conversationId="conv-analytics" />, "owner");
    await openDrawer();

    fireEvent.click(screen.getByTestId("copilot-chip-channel-analytics"));

    await waitFor(async () => {
      const chatCall = fetchMock.mock.calls.find((call) =>
        fetchCallUrl(call).includes("/ai/assistant/chat"),
      );
      expect(chatCall).toBeDefined();
      expect(fetchCallMethod(chatCall!)).toBe("POST");
      // The generated SDK resolves `/api/v1/...` under the `/api/bff` base.
      const chatUrl = fetchCallUrl(chatCall!);
      expect(chatUrl).toContain("/api/bff/");
      expect(chatUrl).toMatch(/\/v1\/ai\/assistant\/chat$/);
      const body = JSON.parse(await fetchCallBody(chatCall!)) as Record<string, unknown>;
      expect(body).toMatchObject({
        capability: "biz_analytics",
        strict_rag: false,
        context: { mode: "analytics", window: "30d" },
      });
    });

    const panel = await screen.findByTestId("copilot-analytics-table");
    expect(panel).toBeInTheDocument();

    // Totals bar: GMV, net margin, ad spend, ROAS, window.
    expect(screen.getByTestId("copilot-analytics-total-gmv")).toHaveTextContent("$128,900.00");
    expect(screen.getByTestId("copilot-analytics-total-net-margin")).toHaveTextContent(
      "$24,310.40",
    );
    expect(screen.getByTestId("copilot-analytics-total-ad-spend")).toHaveTextContent("$18,420.00");
    expect(screen.getByTestId("copilot-analytics-total-roas")).toHaveTextContent("7.00x");
    expect(screen.getByTestId("copilot-analytics-total-window")).toHaveTextContent("30d");

    const shopee = screen.getByTestId("copilot-analytics-channel-shopee");
    expect(shopee).toHaveTextContent("Shopee MY");
    expect(shopee).toHaveTextContent("640");
    expect(shopee).toHaveTextContent("$62,400.00");
    expect(shopee).toHaveTextContent("6.40x");
    expect(shopee).toHaveTextContent("$11,800.00");
    expect(shopee).toHaveTextContent("3.10%");

    const temu = screen.getByTestId("copilot-analytics-channel-temu");
    expect(temu).toHaveTextContent("Temu US");
    expect(temu).toHaveTextContent("$38,800.00");
    expect(temu).toHaveTextContent("8.25x");
    expect(temu).toHaveTextContent("$7,350.00");
    expect(temu).toHaveTextContent("1.85%");

    const shopify = screen.getByTestId("copilot-analytics-channel-shopify");
    expect(shopify).toHaveTextContent("Shopify DTC");
    expect(shopify).toHaveTextContent("$27,700.00");
    expect(shopify).toHaveTextContent("5.10x");
    expect(shopify).toHaveTextContent("$5,160.40");
    expect(shopify).toHaveTextContent("0.60%");

    // source_tool_call_id provenance pill stays visible below the panel.
    const box = screen.getByTestId("copilot-answer-box");
    expect(box).toHaveTextContent("tc_analytics_123");

    // No raw object leakage anywhere in the answer box.
    expect(box.textContent ?? "").not.toContain("[object Object]");
  });

  it("asks a natural-language analytics question with the 7d window after switching", async () => {
    const fetchMock = stubCopilotFetch();
    renderWrap(<FloatingCopilot conversationId="conv-analytics" />, "owner");
    await openDrawer();

    fireEvent.click(screen.getByTestId("copilot-mode-analytics"));
    const window7d = await screen.findByTestId("copilot-analytics-window-7d");
    fireEvent.click(window7d);

    fireEvent.change(screen.getByTestId("copilot-query-input"), {
      target: { value: "Which channel had the best ROAS last week?" },
    });
    fireEvent.click(screen.getByTestId("copilot-query-submit"));

    await waitFor(async () => {
      const chatCall = fetchMock.mock.calls.find((call) =>
        fetchCallUrl(call).includes("/ai/assistant/chat"),
      );
      expect(chatCall).toBeDefined();
      expect(fetchCallMethod(chatCall!)).toBe("POST");
      expect(fetchCallUrl(chatCall!)).toMatch(/\/v1\/ai\/assistant\/chat$/);
      const body = JSON.parse(await fetchCallBody(chatCall!)) as Record<string, unknown>;
      expect(body).toMatchObject({
        capability: "biz_analytics",
        strict_rag: false,
        message: "Which channel had the best ROAS last week?",
        context: { mode: "analytics", window: "7d" },
      });
    });

    await waitFor(() =>
      expect(screen.getByTestId("copilot-analytics-table")).toBeInTheDocument(),
    );
  });
});
