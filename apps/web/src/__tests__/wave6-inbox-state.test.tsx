// Wave 6 · Inbox 状态隔离 / 筛选安全 / 加载错误提示
// 覆盖三件事：
//   1. AI 查库自动回复的结果与错误严格按会话隔离（含「在飞期间切走」的串台场景）；
//   2. 筛选把当前会话临时藏起来时，绝不静默回退到 conversations[0]（别人的会话）；
//   3. 会话 / 消息加载失败必须显式报错 + 可重试，不能假装收件箱是空的。
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { I18nProvider } from "@/lib/i18n";
import { AuthSessionContext, type AuthSessionState } from "@/components/providers";
import type { Principal } from "@/lib/contracts";
import { fetchCallUrl, normalizeFetchInput } from "@/__tests__/fetch-stub";
import type { ConversationSummary } from "@/components/inbox/conversation-list";
import InboxPage from "@/app/inbox/page";

vi.mock("@/hooks/use-realtime-inbox", () => ({ useRealtimeInbox: vi.fn() }));
// 抽屉 / 悬浮 Copilot 与本次状态隔离无关，替换成轻量桩，避免额外请求干扰断言。
vi.mock("@/components/inbox/context-drawer", () => ({
  ContextDrawer: () => <div data-testid="context-drawer-stub" />,
}));
vi.mock("@/components/ai/floating-copilot", () => ({ FloatingCopilot: () => null }));

const PRINCIPAL: Principal = {
  user_id: "11111111-1111-1111-1111-111111111111",
  tenant_id: "22222222-2222-2222-2222-222222222222",
  role: "owner",
  email: "owner@qzled.example.com",
  tenant_slug: "qz-led-factory",
  onboarding_stage: "LiveProduction",
};

function conversation(
  overrides: Partial<ConversationSummary> & Pick<ConversationSummary, "id" | "customer_name">,
): ConversationSummary {
  return {
    customer_id: `cust-${overrides.id}`,
    country_code: "DE",
    iana_timezone: "Europe/Berlin",
    channel_account_id: "acct-1",
    channel_type: "made_in_china_email",
    conversation_mode: "b2b_inquiry",
    status: "pending_human",
    unread_count: 0,
    sla_due_at: null,
    csw_expires_at: null,
    human_agent_window_expires_at: null,
    last_message_preview: "Please quote 500 pcs LED-COB-24V-5M FOB Shenzhen",
    updated_at: "2026-11-19T08:00:00Z",
    is_pinned: false,
    tags: [],
    ...overrides,
  };
}

// 两条不同买家的会话：conv-a 排在列表第一位（旧实现会静默回退到它）。
const CONV_A = conversation({ id: "conv-a", customer_name: "Grace Lim", unread_count: 2 });
const CONV_B = conversation({
  id: "conv-b",
  customer_name: "Hans Weber",
  country_code: "DE",
  unread_count: 0,
});

function runPayload(action: string, conversationId: string, replyText: string) {
  return {
    action,
    confidence: "0.8600",
    conversation_id: conversationId,
    message_id: `${conversationId}-msg-out`,
    proposal_id: null,
    reply_text: replyText,
    risk_flags: [],
    kb_hits: [{ chunk_id: `${conversationId}-kb-1` }],
    tool_evidence: [{ source_tool_call_id: `${conversationId}-sql-1` }],
  };
}

interface StubOptions {
  /** 按当前 filter 决定列表内容（模拟服务端筛选）。 */
  conversationsFor?: (filter: string) => ConversationSummary[];
  /** 自定义 POST /auto-reply/run 的响应（可挂起，用于「在飞期间切走」）。 */
  run?: (conversationId: string) => Promise<Response> | Response;
  /** 前 N 次会话列表请求直接失败（模拟后端抖动）。 */
  failConversationLoads?: number;
}

function stubFetch(options: StubOptions = {}) {
  let conversationAttempts = 0;
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const { url } = normalizeFetchInput(input, init);
    const parsed = new URL(url, "http://localhost:3000");
    const path = parsed.pathname;

    if (path.endsWith("/v1/inbox/conversations")) {
      conversationAttempts += 1;
      if (conversationAttempts <= (options.failConversationLoads ?? 0)) {
        throw new TypeError("Failed to fetch");
      }
      const filter = parsed.searchParams.get("filter") ?? "all";
      const items = (options.conversationsFor ?? (() => [CONV_A, CONV_B]))(filter);
      return Response.json({
        items,
        total: items.length,
        limit: 25,
        offset: 0,
        has_more: false,
      });
    }

    const messagesMatch = path.match(/\/v1\/inbox\/conversations\/([^/]+)\/messages$/);
    if (messagesMatch) {
      const conversationId = messagesMatch[1];
      return Response.json({
        items: [
          {
            id: `${conversationId}-msg-1`,
            conversation_id: conversationId,
            channel_type: "made_in_china_email",
            direction: "inbound",
            sender_type: "customer",
            content_type: "text",
            body_text: `Inbound for ${conversationId}`,
            delivery_status: "read",
            source_timestamp: "2026-11-19T08:00:00Z",
            attachments: [],
          },
        ],
        total: 1,
        limit: 50,
        offset: 0,
        has_more: false,
      });
    }

    const runMatch = path.match(/\/v1\/ai\/conversations\/([^/]+)\/auto-reply\/run$/);
    if (runMatch) {
      const conversationId = runMatch[1];
      if (options.run) return options.run(conversationId);
      return Response.json(runPayload("auto_replied", conversationId, `reply for ${conversationId}`));
    }

    const settingMatch = path.match(/\/v1\/ai\/conversations\/([^/]+)\/auto-reply$/);
    if (settingMatch) {
      return Response.json({
        conversation_id: settingMatch[1],
        ai_auto_reply_enabled: false,
      });
    }

    return Response.json({ items: [], total: 0, limit: 20, offset: 0, has_more: false });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function wrap(ui: React.ReactNode, locale: "zh" | "en" = "zh") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const authValue: AuthSessionState = {
    principal: PRINCIPAL,
    isLoading: false,
    logout: vi.fn(async () => {}),
    refreshSession: vi.fn(async () => {}),
  };
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthSessionContext.Provider value={authValue}>
        <I18nProvider defaultLocale={locale}>{ui}</I18nProvider>
      </AuthSessionContext.Provider>
    </QueryClientProvider>
  );
}

/** 消息线程头部展示的买家名：用来判断「当前到底是哪条会话」。 */
function threadHeaderText(): string {
  return screen.getByTestId("inbox-message-thread").textContent ?? "";
}

describe("Wave 6 · Inbox 会话状态隔离", () => {
  beforeEach(() => {
    vi.unstubAllEnvs();
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("scopes an AI auto-reply result to its own conversation (no cross-conversation bleed)", async () => {
    stubFetch({
      run: (conversationId) =>
        Response.json(runPayload("auto_replied", conversationId, `reply for ${conversationId}`)),
    });

    wrap(<InboxPage />, "zh");

    // 初始加载：无人工选中 → 无缝打开第一条会话（conv-a）。
    await waitFor(() => expect(screen.getByTestId("inbox-message-thread")).toHaveTextContent("Grace Lim"));

    fireEvent.click(screen.getByTestId("inbox-ai-autoreply-run-btn"));
    const resultA = await screen.findByTestId("inbox-autoreply-result");
    expect(resultA).toHaveTextContent("已自动回复客户");
    expect(resultA).toHaveTextContent("reply for conv-a");

    // 切到 conv-b：conv-a 的结果条绝不能跟过来。
    fireEvent.click(screen.getByTestId("conversation-item-conv-b"));
    await waitFor(() => expect(threadHeaderText()).toContain("Hans Weber"));
    await waitFor(() => expect(screen.queryByTestId("inbox-autoreply-result")).not.toBeInTheDocument());

    // 切回 conv-a：结果条恢复。
    fireEvent.click(screen.getByTestId("conversation-item-conv-a"));
    await waitFor(() => expect(threadHeaderText()).toContain("Grace Lim"));
    const restored = await screen.findByTestId("inbox-autoreply-result");
    expect(restored).toHaveTextContent("reply for conv-a");
  });

  it("attaches an in-flight auto-reply to its originating conversation even after switching away", async () => {
    let release: (() => void) | null = null;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const runCalls: string[] = [];

    stubFetch({
      run: async (conversationId) => {
        runCalls.push(conversationId);
        await gate;
        return Response.json(
          runPayload("auto_replied", conversationId, `reply for ${conversationId}`),
        );
      },
    });

    wrap(<InboxPage />, "zh");
    await waitFor(() => expect(threadHeaderText()).toContain("Grace Lim"));

    // 在 conv-a 上发起查库回复（此刻 promise 一直挂着）。
    fireEvent.click(screen.getByTestId("inbox-ai-autoreply-run-btn"));
    await waitFor(() => expect(runCalls).toEqual(["conv-a"]));

    // 结果还没回来就切到 conv-b（另一个买家）。
    fireEvent.click(screen.getByTestId("conversation-item-conv-b"));
    await waitFor(() => expect(threadHeaderText()).toContain("Hans Weber"));

    // 让 conv-a 的请求现在完成。
    await act(async () => {
      release?.();
      await Promise.resolve();
    });

    // conv-b 上绝不能出现 conv-a 的结果。
    await waitFor(() => expect(screen.queryByTestId("inbox-autoreply-result")).not.toBeInTheDocument());
    expect(threadHeaderText()).toContain("Hans Weber");

    // 回到 conv-a 时才看得到自己的结果。
    fireEvent.click(screen.getByTestId("conversation-item-conv-a"));
    await waitFor(() => expect(threadHeaderText()).toContain("Grace Lim"));
    const result = await screen.findByTestId("inbox-autoreply-result");
    expect(result).toHaveTextContent("reply for conv-a");
  });

  it("does not fall back to another buyer when a filter temporarily hides the selected conversation", async () => {
    // 「未读」筛选下只剩 conv-a（conv-b 被服务端筛掉）。
    stubFetch({
      conversationsFor: (filter) => (filter === "unread" ? [CONV_A] : [CONV_A, CONV_B]),
    });

    wrap(<InboxPage />, "zh");
    await waitFor(() => expect(screen.getByTestId("conversation-item-conv-b")).toBeInTheDocument());

    // 手动选中 conv-b。
    fireEvent.click(screen.getByTestId("conversation-item-conv-b"));
    await waitFor(() => expect(threadHeaderText()).toContain("Hans Weber"));

    // 切到「未读」筛选：conv-b 从列表里消失。
    fireEvent.click(screen.getByRole("button", { name: "Filter unread" }));
    await waitFor(() => expect(screen.queryByTestId("conversation-item-conv-b")).not.toBeInTheDocument());

    // 关键：头部 / 回复框仍然是 conv-b，绝不静默跳到 conversations[0]（Grace Lim）。
    expect(threadHeaderText()).toContain("Hans Weber");
    expect(threadHeaderText()).not.toContain("Grace Lim");
    expect(screen.getByTestId("inbox-composer")).toBeInTheDocument();
  });

  it("renders a role=alert load-error banner and retries cleanly when the conversation fetch fails", async () => {
    const fetchMock = stubFetch({ failConversationLoads: 1 });

    wrap(<InboxPage />, "zh");

    const banner = await screen.findByTestId("inbox-load-error");
    expect(banner).toHaveAttribute("role", "alert");
    expect(banner).toHaveTextContent("收件箱加载失败");
    // 失败时不能假装「没有会话」——错误提示必须和空态并存可见。
    expect(screen.getByTestId("inbox-conversation-list")).toBeInTheDocument();

    const retry = screen.getByTestId("inbox-load-error-retry");
    fireEvent.click(retry);

    await waitFor(() =>
      expect(screen.getByTestId("conversation-item-conv-a")).toBeInTheDocument()
    );
    await waitFor(() => expect(screen.queryByTestId("inbox-load-error")).not.toBeInTheDocument());
    expect(screen.getByTestId("conversation-item-conv-b")).toBeInTheDocument();

    // 重试确实重新打了会话列表接口（1 次失败 + 至少 1 次成功）。
    const listCalls = fetchMock.mock.calls.filter((call) =>
      fetchCallUrl(call).includes("/v1/inbox/conversations?")
    );
    expect(listCalls.length).toBeGreaterThanOrEqual(2);
  });
});
