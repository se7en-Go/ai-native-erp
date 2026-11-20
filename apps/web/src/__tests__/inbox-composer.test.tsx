import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nProvider } from "@/lib/i18n";
import { AuthSessionContext } from "@/components/providers";
import {
  fetchCallBody,
  fetchCallMethod,
  fetchCallUrl,
  normalizeFetchInput,
} from "@/__tests__/fetch-stub";
import { ChannelRail } from "@/components/inbox/channel-rail";
import { ConversationList, type ConversationSummary } from "@/components/inbox/conversation-list";
import { MessageThread, type InboxMessage } from "@/components/inbox/message-thread";
import { InboxComposer } from "@/components/inbox/composer";

const SAMPLE_CONV: ConversationSummary = {
  id: "conv-1",
  customer_id: "cust-1",
  customer_name: "Hans Weber",
  country_code: "DE",
  iana_timezone: "Europe/Berlin",
  channel_account_id: "acct-1",
  channel_type: "made_in_china_email",
  conversation_mode: "b2b_inquiry",
  status: "pending_human",
  unread_count: 2,
  sla_due_at: "2026-11-19T12:00:00Z",
  csw_expires_at: "2026-11-20T12:00:00Z",
  human_agent_window_expires_at: null,
  last_message_preview: "Please quote 500 pcs LED-COB-24V-5M FOB Shenzhen",
  updated_at: "2026-11-19T08:00:00Z",
};

const SAMPLE_MESSAGES: InboxMessage[] = [
  {
    id: "msg-1",
    conversation_id: "conv-1",
    channel_type: "made_in_china_email",
    direction: "inbound",
    sender_type: "customer",
    content_type: "text",
    body_text: "Need 500 pcs COB LED strip 24V FOB Shenzhen.",
    detected_language: "EN",
    translated_text_zh: "需要 500 条 COB LED 24V 灯带，FOB 深圳报价。",
    delivery_status: "read",
    source_timestamp: "2026-11-19T08:00:00Z",
  },
];

function wrap(ui: React.ReactNode, locale: "zh" | "en" = "zh") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AuthSessionContext.Provider
        value={{
          principal: {
            user_id: "u1",
            tenant_id: "t1",
            role: "owner",
            email: "owner@qzled.example.com",
          },
          isLoading: false,
          logout: async () => {},
          refreshSession: async () => {},
        }}
      >
        <I18nProvider defaultLocale={locale}>{ui}</I18nProvider>
      </AuthSessionContext.Provider>
    </QueryClientProvider>
  );
}

describe("Task 5.2 4-Column Inbox & Composer", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("renders all 9 channel groups and switches conversation filter tabs", () => {
    const onChannel = vi.fn();
    const onFilter = vi.fn();

    wrap(
      <div>
        <ChannelRail selectedChannel="all" onSelectChannel={onChannel} unreadByChannel={{}} />
        <ConversationList
          conversations={[SAMPLE_CONV]}
          selectedConversationId="conv-1"
          activeFilter="all"
          searchQuery=""
          onSelectFilter={onFilter}
          onSearchChange={vi.fn()}
          onSelectConversation={vi.fn()}
        />
      </div>,
      "en"
    );

    for (const label of [
      "All",
      "WhatsApp",
      "Email",
      "WebChat",
      "Shopify",
      "WooCommerce",
      "SHOPLINE",
      "Shoplazza",
      "TikTok",
    ]) {
      expect(screen.getByRole("button", { name: new RegExp(label, "i") })).toBeInTheDocument();
    }

    fireEvent.click(screen.getByRole("button", { name: /pending_human/i }));
    expect(onFilter).toHaveBeenCalledWith("pending_human");

    fireEvent.click(screen.getByRole("button", { name: /sla_urgent/i }));
    expect(onFilter).toHaveBeenCalledWith("sla_urgent");
  });

  it("displays 休息时段 quiet-hours warning pill when buyer local time is 21:00-08:00 and toggles translation", () => {
    // 2026-11-19T22:30:00Z -> Europe/Berlin (UTC+1) = 23:30 (quiet hours!)
    vi.setSystemTime(new Date("2026-11-19T22:30:00Z"));

    wrap(
      <MessageThread
        conversation={SAMPLE_CONV}
        messages={SAMPLE_MESSAGES}
        onToggleDrawer={vi.fn()}
      />,
      "zh"
    );

    expect(screen.getByTestId("buyer-local-time-badge")).toBeInTheDocument();
    expect(screen.getByTestId("quiet-hours-pill")).toHaveTextContent("休息时段");

    const toggleTransBtn = screen.getByTestId("toggle-msg-translation-msg-1");
    fireEvent.click(toggleTransBtn);
    expect(screen.getByText(/需要 500 条 COB LED 24V 灯带/)).toBeInTheDocument();
  });

  it("submits internal_note via composer and previews bidirectional translation + Strict RAG draft", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const { url, method } = normalizeFetchInput(input, init);
      if (url.includes("/ai/translate")) {
        return new Response(
          JSON.stringify({
            translated_text: "We can quote 500 pcs at $14.20 FOB Shenzhen.",
            detected_source_lang: "zh",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/ai-assist")) {
        return new Response(
          JSON.stringify({
            text: "MOQ 500 tier price is $14.20/pc FOB Shenzhen [doc-led#c2].",
            citations: [{ doc_id: "doc-led", chunk_id: "c2", score: 0.91 }],
            abstained: false,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/messages") && method === "POST") {
        return new Response(
          JSON.stringify({
            id: "msg-new",
            conversation_id: "conv-1",
            channel_type: "made_in_china_email",
            direction: "internal_note",
            sender_type: "human_agent",
            content_type: "text",
            body_text: "Check with production lead time",
            delivery_status: "sent",
            source_timestamp: "2026-11-19T08:05:00Z",
          }),
          { status: 201, headers: { "Content-Type": "application/json" } }
        );
      }
      return new Response("{}", { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);

    wrap(<InboxComposer conversation={SAMPLE_CONV} />, "en");

    // Toggle Strict RAG and click AI Draft
    fireEvent.click(screen.getByTestId("composer-strict-rag-toggle"));
    fireEvent.click(screen.getByRole("button", { name: /AI Draft/i }));

    await waitFor(async () => {
      const assistCall = fetchMock.mock.calls.find((c) =>
        fetchCallUrl(c).includes("/ai-assist"),
      );
      expect(assistCall).toBeDefined();
      expect(JSON.parse(await fetchCallBody(assistCall!))).toMatchObject({
        action: "draft",
        strict_rag: true,
      });
    });

    // Enable live translate preview
    fireEvent.click(screen.getByTestId("live-translate-toggle"));
    fireEvent.change(screen.getByTestId("composer-textarea"), {
      target: { value: "我们500条可以报14.20美金" },
    });

    await waitFor(() => {
      expect(screen.getByTestId("live-translation-preview")).toHaveTextContent(
        "$14.20 FOB Shenzhen"
      );
    });

    // Switch to Internal Note and submit
    fireEvent.click(screen.getByTestId("composer-mode-internal-note"));
    fireEvent.change(screen.getByTestId("composer-textarea"), {
      target: { value: "Check with production lead time" },
    });
    fireEvent.click(screen.getByTestId("composer-submit-btn"));

    await waitFor(async () => {
      const postCall = fetchMock.mock.calls.find(
        (c) => fetchCallUrl(c).includes("/messages") && fetchCallMethod(c) === "POST",
      );
      expect(postCall).toBeDefined();
      expect(JSON.parse(await fetchCallBody(postCall!))).toMatchObject({
        direction: "internal_note",
        body_text: "Check with production lead time",
      });
    });
  });

  it("deep-links the empty-state CTA straight to the active channel drawer", () => {
    wrap(
      <ConversationList
        conversations={[]}
        selectedConversationId={null}
        activeFilter="all"
        searchQuery=""
        onSelectFilter={vi.fn()}
        onSearchChange={vi.fn()}
        onSelectConversation={vi.fn()}
        quickConnectHref="/settings/channels?connect=shopify"
        quickConnectLabel="快捷绑定 Shopify →"
      />,
      "zh"
    );

    const link = screen.getByTestId("inbox-quick-connect-link") as HTMLAnchorElement;
    expect(link.getAttribute("href")).toBe("/settings/channels?connect=shopify");
    expect(link.textContent).toContain("Shopify");
  });

  it("keeps the channel rail settings link contextual to the selected channel", () => {
    wrap(
      <ChannelRail selectedChannel="whatsapp" onSelectChannel={vi.fn()} unreadByChannel={{}} />,
      "zh"
    );

    expect(
      (screen.getByTestId("inbox-channel-settings-link") as HTMLAnchorElement).getAttribute("href")
    ).toBe("/settings/channels?connect=whatsapp");
  });

  it("sends the all-channels rail link to the plain channel hub", () => {
    wrap(
      <ChannelRail selectedChannel="all" onSelectChannel={vi.fn()} unreadByChannel={{}} />,
      "zh"
    );

    expect(
      (screen.getByTestId("inbox-channel-settings-link") as HTMLAnchorElement).getAttribute("href")
    ).toBe("/settings/channels");
  });
});
