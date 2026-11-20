import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nProvider } from "@/lib/i18n";
import { InboxComposer } from "@/components/inbox/composer";
import type { ConversationSummary } from "@/components/inbox/conversation-list";
import {
  fetchCallBody,
  fetchCallMethod,
  fetchCallUrl,
  normalizeFetchInput,
} from "@/__tests__/fetch-stub";

const CONVERSATION: ConversationSummary = {
  id: "conv-1",
  customer_id: "cust-1",
  customer_name: "Hans Weber",
  country_code: "DE",
  iana_timezone: "Europe/Berlin",
  channel_account_id: "acct-1",
  channel_type: "email",
  conversation_mode: "b2b_inquiry",
  status: "pending_human",
  unread_count: 1,
  sla_due_at: "2026-11-19T12:00:00Z",
  csw_expires_at: null,
  human_agent_window_expires_at: null,
  last_message_preview: "Please quote 500 pcs",
  updated_at: "2026-11-19T08:00:00Z",
};

const GROUNDED_PROPOSAL = {
  id: "prop-grounded-1",
  capability: "sales_quoting",
  proposal_type: "quote_draft",
  conversation_id: "conv-1",
  proposed_payload: {
    sku: "LED-COB-24V-5M",
    quantity: 500,
    source_tool_call_id: "sql-tool-demo-sku-1",
    tool_evidence: [{ sku: "LED-COB-24V-5M", available_qty: 1240 }],
  },
  citations: [],
  confidence_score: "0.93",
  estimated_margin_pct: "24.5",
  status: "pending",
  created_at: "2026-11-19T08:10:00Z",
};

function wrap(ui: React.ReactNode, locale: "zh" | "en" = "en") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <I18nProvider defaultLocale={locale}>{ui}</I18nProvider>
    </QueryClientProvider>
  );
}

describe("Track 2 · Inbox 『AI 查库生成提案』 trigger", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("calls POST /ai/conversations/{id}/propose-reply and links straight to the review queue", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const { url, method } = normalizeFetchInput(input, init);
      if (url.includes("/ai/conversations/conv-1/propose-reply") && method === "POST") {
        return Response.json(GROUNDED_PROPOSAL, { status: 201 });
      }
      return Response.json({ items: [], total: 0 });
    });
    vi.stubGlobal("fetch", fetchMock);

    wrap(<InboxComposer conversation={CONVERSATION} />);

    const button = screen.getByTestId("propose-grounded-reply-btn");
    expect(button.textContent).toContain("AI grounded proposal");
    fireEvent.click(button);

    await waitFor(() => {
      const call = fetchMock.mock.calls.find((c) =>
        fetchCallUrl(c).includes("/propose-reply")
      );
      expect(call).toBeDefined();
      expect(fetchCallUrl(call!)).toContain(
        "/api/bff/v1/ai/conversations/conv-1/propose-reply"
      );
      expect(fetchCallMethod(call!)).toBe("POST");
    });

    const link = await screen.findByTestId("propose-grounded-reply-link");
    expect(link.getAttribute("href")).toBe("/ai/proposals");
    expect(link.textContent).toContain("Grounded proposal ready");
    expect(await fetchCallBody(
      fetchMock.mock.calls.find((c) => fetchCallUrl(c).includes("/propose-reply"))!
    )).toBe("{}");
  });

  it("surfaces a readable error when the proposal endpoint fails", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const { url, method } = normalizeFetchInput(input, init);
      if (url.includes("/propose-reply") && method === "POST") {
        return Response.json(
          {
            type: "https://erp.local/errors/llm-unavailable",
            title: "LLM unavailable",
            status: 503,
            detail: "ERR_AI_PROVIDER_UNAVAILABLE",
            code: "ERR_AI_PROVIDER_UNAVAILABLE",
          },
          { status: 503 }
        );
      }
      return Response.json({ items: [], total: 0 });
    });
    vi.stubGlobal("fetch", fetchMock);

    wrap(<InboxComposer conversation={CONVERSATION} />);
    fireEvent.click(screen.getByTestId("propose-grounded-reply-btn"));

    await waitFor(() => {
      expect(screen.getByTestId("composer-assist-error").textContent).toContain(
        "ERR_AI_PROVIDER_UNAVAILABLE"
      );
    });
    expect(screen.queryByTestId("propose-grounded-reply-success")).toBeNull();
  });

  it("appends an externally inserted stock snippet into the draft", async () => {
    wrap(
      <InboxComposer
        conversation={CONVERSATION}
        insertRequest={{
          text: "LED-COB-24V-5M — 1,240 pcs in stock\nMOQ 500 tier price $14.20/pc (FOB)",
          nonce: 1,
        }}
      />
    );

    await waitFor(() => {
      expect(screen.getByTestId("composer-textarea")).toHaveValue(
        "LED-COB-24V-5M — 1,240 pcs in stock\nMOQ 500 tier price $14.20/pc (FOB)"
      );
    });
    expect(screen.getByTestId("composer-assist-notice").textContent).toContain(
      "Live stock & tier price inserted"
    );
  });
});
