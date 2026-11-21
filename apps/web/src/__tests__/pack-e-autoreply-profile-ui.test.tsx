// Pack E · AI 查库自动回复 + AI 客户画像（UI + API 契约）
// 覆盖：自动回复开关 PATCH、一键执行 POST 与三条结果条文案、消息自动刷新、
// 抽屉「AI 客户画像」标签页与「AI 提炼画像并同步 CRM」、CRM 客户详情画像卡片，
// 以及 ContextDrawer 旧 props（不传 conversationId）的向后兼容。
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { I18nProvider } from "@/lib/i18n";
import { AuthSessionContext, type AuthSessionState } from "@/components/providers";
import type { Principal } from "@/lib/contracts";
import {
  fetchCallBody,
  fetchCallMethod,
  fetchCallUrl,
  normalizeFetchInput,
} from "@/__tests__/fetch-stub";
import {
  autoReplyActionLabel,
  autoReplyEvidenceCounts,
  extractCustomerAiProfile,
  fetchAutoReplySetting,
  fetchCustomerAiProfile,
  formatConfidencePercent,
  normalizeAutoReplyRun,
  runAiAutoReply,
  updateAutoReplySetting,
} from "@/lib/api-client/pack-e-autoreply";
import { ContextDrawer } from "@/components/inbox/context-drawer";
import InboxPage from "@/app/inbox/page";
import CrmCustomersPage from "@/app/crm/customers/page";

vi.mock("@/hooks/use-realtime-inbox", () => ({ useRealtimeInbox: vi.fn() }));
// 悬浮 Copilot 会自己拉提案列表，与本 Pack 无关。
vi.mock("@/components/ai/floating-copilot", () => ({
  FloatingCopilot: () => null,
}));

const PRINCIPAL: Principal = {
  user_id: "11111111-1111-1111-1111-111111111111",
  tenant_id: "22222222-2222-2222-2222-222222222222",
  role: "owner",
  email: "owner@qzled.example.com",
  tenant_slug: "qz-led-factory",
  onboarding_stage: "LiveProduction",
};

const CONVERSATION = {
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
  sla_due_at: null,
  csw_expires_at: null,
  human_agent_window_expires_at: null,
  last_message_preview: "Please quote 500 pcs LED-COB-24V-5M FOB Shenzhen",
  updated_at: "2026-11-19T08:00:00Z",
  is_pinned: false,
  tags: [],
};

const CUSTOMER_CONTEXT = {
  customer: {
    id: "cust-1",
    display_name: "Hans Weber",
    customer_type: "b2b_company_rep",
    ltv_usd: "42500.00",
    lifecycle_stage: "qualified",
  },
  company: null,
  identities: [],
  probable_duplicates: [],
  merge_events: [],
  recent_orders: [],
  recent_quotes: [],
};

function wrap(ui: React.ReactNode, locale: "zh" | "en" = "zh") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const authValue: AuthSessionState = {
    principal: PRINCIPAL,
    isLoading: false,
    logout: vi.fn(async () => {}),
    refreshSession: vi.fn(async () => {}),
  };
  return render(
    <QueryClientProvider client={qc}>
      <AuthSessionContext.Provider value={authValue}>
        <I18nProvider defaultLocale={locale}>{ui}</I18nProvider>
      </AuthSessionContext.Provider>
    </QueryClientProvider>
  );
}

// ---------------------------------------------------------------------------
// API 契约（真实 bffFetch 路径，非 mock 模式）
// ---------------------------------------------------------------------------

describe("Pack E · 自动回复与画像 API 契约", () => {
  beforeEach(() => {
    vi.unstubAllEnvs();
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  });

  it("hits the five frozen endpoints with the exact contract payloads", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const { url } = normalizeFetchInput(input, init);
      if (url.includes("/auto-reply/run")) {
        return Response.json({
          action: "auto_replied",
          confidence: "0.8600",
          conversation_id: "conv-1",
          message_id: "msg-100",
          proposal_id: null,
          reply_text: "500 pcs FOB $14.20/pc",
          risk_flags: [],
          kb_hits: [{ chunk_id: "kb-1" }, { chunk_id: "kb-2" }],
          tool_evidence: [{ source_tool_call_id: "sql-sku-1" }],
        });
      }
      if (url.includes("/extract-profile")) {
        return Response.json({
          customer_id: "cust-1",
          purchase_intent_score: 74,
          buyer_type: "b2b_distributor",
          interested_skus: ["LED-COB-24V-5M"],
          next_best_action: "Send the PI today.",
          updated_at: "2026-11-21T00:00:00Z",
          synced_lead_id: "lead-9",
          synced_followup_id: "fu-9",
        });
      }
      if (url.includes("/customers/cust-1/profile")) {
        return Response.json({
          customer_id: "cust-1",
          purchase_intent_score: 74,
          updated_at: "2026-11-21T00:00:00Z",
        });
      }
      return Response.json({ conversation_id: "conv-1", ai_auto_reply_enabled: true });
    });
    vi.stubGlobal("fetch", fetchMock);

    expect(await fetchAutoReplySetting("conv-1")).toEqual({
      conversation_id: "conv-1",
      ai_auto_reply_enabled: true,
    });

    const toggled = await updateAutoReplySetting("conv-1", false);
    // 后端只回空对象时用请求值兜底，开关不会回弹。
    expect(toggled.ai_auto_reply_enabled).toBe(true);

    const run = await runAiAutoReply("conv-1", "auto", 3);
    expect(run.action).toBe("auto_replied");
    expect(autoReplyEvidenceCounts(run)).toEqual({ kbHits: 2, toolEvidence: 1 });

    const extracted = await extractCustomerAiProfile("conv-1", true);
    expect(extracted.synced_lead_id).toBe("lead-9");
    expect(extracted.synced_followup_id).toBe("fu-9");
    expect(extracted.purchase_intent_score).toBe(74);

    const profile = await fetchCustomerAiProfile("cust-1");
    expect(profile.customer_id).toBe("cust-1");
    // 缺省字段被归一化，UI 不必到处判空。
    expect(profile.key_requirements).toEqual([]);
    expect(profile.price_sensitivity).toBe("low");

    const runCall = fetchMock.mock.calls.find((call) =>
      fetchCallUrl(call).includes("/auto-reply/run")
    );
    expect(fetchCallMethod(runCall!)).toBe("POST");
    expect(JSON.parse(await fetchCallBody(runCall!))).toEqual({
      mode: "auto",
      max_kb_hits: 3,
    });

    const patchCall = fetchMock.mock.calls.find(
      (call) =>
        fetchCallUrl(call).includes("/v1/ai/conversations/conv-1/auto-reply") &&
        fetchCallMethod(call) === "PATCH"
    );
    expect(JSON.parse(await fetchCallBody(patchCall!))).toEqual({ enabled: false });

    const extractCall = fetchMock.mock.calls.find((call) =>
      fetchCallUrl(call).includes("/conversations/conv-1/extract-profile")
    );
    expect(fetchCallMethod(extractCall!)).toBe("POST");
    expect(JSON.parse(await fetchCallBody(extractCall!))).toEqual({ sync_to_crm: true });

    const profileCall = fetchMock.mock.calls.find((call) =>
      fetchCallUrl(call).includes("/v1/ai/customers/cust-1/profile")
    );
    expect(profileCall).toBeDefined();
    expect(fetchCallMethod(profileCall!)).toBeUndefined();
  });

  it("labels the three auto-reply actions and formats Decimal confidence", () => {
    expect(autoReplyActionLabel("auto_replied")).toBe("已自动回复客户");
    expect(autoReplyActionLabel("proposal_created")).toBe("触发安全红线已转 L1 提案");
    expect(autoReplyActionLabel("abstained")).toBe("信息不足已安全拒答");
    expect(autoReplyActionLabel("proposal_created", "en")).toContain("L1 proposal");
    expect(formatConfidencePercent("0.8600")).toBe("86%");
    expect(formatConfidencePercent(0.5)).toBe("50%");
    expect(autoReplyEvidenceCounts(normalizeAutoReplyRun(null, "conv-1"))).toEqual({
      kbHits: 0,
      toolEvidence: 0,
    });
  });
});

// ---------------------------------------------------------------------------
// Mock 模式（零后端演示 & 状态一致性）
// ---------------------------------------------------------------------------

describe("Pack E · mock 模式下的自动回复与画像", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  });

  it("persists the toggle, rotates the three run actions and syncs a profile", async () => {
    expect((await fetchAutoReplySetting("conv-mock")).ai_auto_reply_enabled).toBe(true);
    const off = await updateAutoReplySetting("conv-mock", false);
    expect(off.ai_auto_reply_enabled).toBe(false);
    expect((await fetchAutoReplySetting("conv-mock")).ai_auto_reply_enabled).toBe(false);

    const first = await runAiAutoReply("conv-mock", "auto");
    const second = await runAiAutoReply("conv-mock", "auto");
    const third = await runAiAutoReply("conv-mock", "auto");
    expect([first.action, second.action, third.action]).toEqual([
      "auto_replied",
      "proposal_created",
      "abstained",
    ]);
    expect(autoReplyEvidenceCounts(first)).toEqual({ kbHits: 2, toolEvidence: 2 });
    expect(second.risk_flags).toContain("refund");
    expect((await runAiAutoReply("conv-mock", "proposal_only")).action).toBe(
      "proposal_created"
    );

    const extracted = await extractCustomerAiProfile("conv-mock", true);
    expect(extracted.synced_lead_id).toBeTruthy();
    expect(extracted.synced_followup_id).toBeTruthy();
    const fetched = await fetchCustomerAiProfile(extracted.customer_id);
    expect(fetched.purchase_intent_score).toBe(extracted.purchase_intent_score);
  });
});

// ---------------------------------------------------------------------------
// /inbox 页面：开关 + 一键执行 + 结果条 + 消息自动刷新
// ---------------------------------------------------------------------------

const AUTO_REPLIED = {
  action: "auto_replied",
  confidence: "0.8600",
  conversation_id: "conv-1",
  message_id: "msg-100",
  proposal_id: null,
  reply_text: "500 pcs LED-COB-24V-5M FOB $14.20/pc, lead time 7 days.",
  risk_flags: [],
  kb_hits: [{ chunk_id: "kb-1" }, { chunk_id: "kb-2" }],
  tool_evidence: [{ source_tool_call_id: "sql-sku-1" }, { source_tool_call_id: "sql-price-2" }],
};

const PROPOSAL_ONLY = {
  action: "proposal_created",
  confidence: "0.6200",
  conversation_id: "conv-1",
  message_id: null,
  proposal_id: "prop-77",
  reply_text: null,
  risk_flags: ["refund"],
  kb_hits: [{ chunk_id: "kb-refund" }],
  tool_evidence: [{ source_tool_call_id: "sql-order-3" }],
};

function stubInboxFetch(runResponse: Record<string, unknown>) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const { url, method } = normalizeFetchInput(input, init);

    if (url.includes("/ai/conversations/conv-1/auto-reply/run")) {
      return Response.json(runResponse);
    }
    if (url.includes("/ai/conversations/conv-1/auto-reply")) {
      if (method === "PATCH") {
        const body = JSON.parse(String(init?.body ?? "{}")) as { enabled?: boolean };
        return Response.json({
          conversation_id: "conv-1",
          ai_auto_reply_enabled: body.enabled === true,
        });
      }
      return Response.json({ conversation_id: "conv-1", ai_auto_reply_enabled: false });
    }
    if (url.includes("/inbox/conversations/conv-1/messages")) {
      return Response.json({
        items: [
          {
            id: "msg-9",
            conversation_id: "conv-1",
            channel_type: "made_in_china_email",
            direction: "inbound",
            sender_type: "customer",
            content_type: "text",
            body_text: "Please quote 500 pcs LED-COB-24V-5M FOB Shenzhen",
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
    if (url.includes("/inbox/conversations")) {
      return Response.json({
        items: [CONVERSATION],
        total: 1,
        limit: 25,
        offset: 0,
        has_more: false,
      });
    }
    if (url.includes("/channels/accounts")) {
      return Response.json({ items: [], total: 0, limit: 20, offset: 0, has_more: false });
    }
    if (url.includes("/ai/proposals")) {
      return Response.json({ items: [], total: 0, limit: 20, offset: 0, has_more: false });
    }
    if (url.includes("/identity/customers/")) {
      return Response.json(CUSTOMER_CONTEXT);
    }
    return Response.json({ items: [], total: 0, limit: 20, offset: 0, has_more: false });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("Pack E · /inbox 开关与一键 AI 查库自动回复", () => {
  beforeEach(() => {
    vi.unstubAllEnvs();
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  });

  it("toggles auto-reply via PATCH and surfaces a dismissible result bar with evidence", async () => {
    const fetchMock = stubInboxFetch(AUTO_REPLIED);

    wrap(<InboxPage />, "zh");

    const toggle = await screen.findByTestId("inbox-ai-autoreply-toggle");
    await waitFor(() => expect(toggle).toHaveTextContent("关"));
    expect(toggle).toHaveAttribute("role", "switch");
    expect(toggle).toHaveAttribute("aria-checked", "false");

    fireEvent.click(toggle);
    await waitFor(() => expect(toggle).toHaveTextContent("开"));

    const patchCall = fetchMock.mock.calls.find(
      (call) =>
        fetchCallUrl(call).includes("/v1/ai/conversations/conv-1/auto-reply") &&
        fetchCallMethod(call) === "PATCH"
    );
    expect(JSON.parse(await fetchCallBody(patchCall!))).toEqual({ enabled: true });

    const messageCallsBefore = fetchMock.mock.calls.filter((call) =>
      fetchCallUrl(call).includes("/inbox/conversations/conv-1/messages")
    ).length;

    fireEvent.click(screen.getByTestId("inbox-ai-autoreply-run-btn"));

    const result = await screen.findByTestId("inbox-autoreply-result");
    expect(result).toHaveTextContent("已自动回复客户");
    expect(screen.getByTestId("inbox-autoreply-kb-hits")).toHaveTextContent("2");
    expect(screen.getByTestId("inbox-autoreply-tool-evidence")).toHaveTextContent("2");
    expect(result).toHaveTextContent("86%");
    expect(result).toHaveTextContent("lead time 7 days");

    const runCall = fetchMock.mock.calls.find((call) =>
      fetchCallUrl(call).includes("/v1/ai/conversations/conv-1/auto-reply/run")
    );
    expect(fetchCallMethod(runCall!)).toBe("POST");
    expect(JSON.parse(await fetchCallBody(runCall!))).toEqual({
      mode: "auto",
      max_kb_hits: 3,
    });

    // 执行后自动刷新消息列表。
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.filter((call) =>
          fetchCallUrl(call).includes("/inbox/conversations/conv-1/messages")
        ).length
      ).toBeGreaterThan(messageCallsBefore)
    );

    fireEvent.click(screen.getByTestId("inbox-autoreply-result-dismiss"));
    expect(screen.queryByTestId("inbox-autoreply-result")).not.toBeInTheDocument();
  });

  it("shows the safety red-line escalation when the run is forced into an L1 proposal", async () => {
    stubInboxFetch(PROPOSAL_ONLY);

    wrap(<InboxPage />, "zh");

    fireEvent.click(await screen.findByTestId("inbox-ai-autoreply-run-btn"));

    const result = await screen.findByTestId("inbox-autoreply-result");
    expect(result).toHaveTextContent("触发安全红线已转 L1 提案");
    expect(screen.getByTestId("inbox-autoreply-risk-flags")).toHaveTextContent("refund");
    expect(screen.getByTestId("inbox-autoreply-kb-hits")).toHaveTextContent("1");
    expect(screen.getByTestId("inbox-autoreply-tool-evidence")).toHaveTextContent("1");
  });
});

// ---------------------------------------------------------------------------
// ContextDrawer：「AI 客户画像」标签页
// ---------------------------------------------------------------------------

describe("Pack E · 抽屉 AI 客户画像标签页", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  });

  function stubDrawerFetch() {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const { url } = normalizeFetchInput(input, init);
      if (url.includes("/identity/customers/cust-1/context")) {
        return Response.json(CUSTOMER_CONTEXT);
      }
      return Response.json({ items: [], total: 0, limit: 20, offset: 0, has_more: false });
    });
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  it("extracts the profile and syncs CRM from the new tab", async () => {
    stubDrawerFetch();

    wrap(
      <ContextDrawer customerId="cust-1" conversationId="conv-1" />,
      "zh"
    );

    await waitFor(() => expect(screen.getByText("Hans Weber")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("context-tab-profile"));

    const panel = await screen.findByTestId("context-profile-panel");
    expect(panel).toBeInTheDocument();

    const extractBtn = screen.getByTestId("extract-crm-profile-btn");
    expect(extractBtn).toHaveTextContent("AI 提炼画像并同步 CRM");
    expect(extractBtn).not.toBeDisabled();

    fireEvent.click(extractBtn);

    await waitFor(() =>
      expect(screen.getByTestId("profile-crm-sync-notice")).toBeInTheDocument()
    );
    expect(screen.getByTestId("customer-ai-profile")).toBeInTheDocument();
    expect(screen.getByTestId("profile-intent-score")).toHaveTextContent("82");
    expect(screen.getByTestId("profile-intent-bar")).toHaveStyle({ width: "82%" });
    expect(screen.getByTestId("profile-sku-LED-COB-24V-5M")).toBeInTheDocument();
    expect(screen.getByTestId("profile-next-best-action")).toHaveTextContent(
      "今天内发 FOB 阶梯价 PI"
    );
    expect(screen.getByTestId("profile-crm-sync-notice")).toHaveTextContent(
      "mock-lead-conv-1"
    );
  });

  it("stays backward compatible when the old caller only passes customerId", async () => {
    stubDrawerFetch();

    wrap(<ContextDrawer customerId="cust-1" />, "en");

    await waitFor(() => expect(screen.getByText("Hans Weber")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("context-tab-profile"));

    // 没有会话上下文：仍能打开画像页，只是提炼按钮置灰并给出引导。
    expect(await screen.findByTestId("profile-needs-conversation")).toBeInTheDocument();
    expect(screen.getByTestId("extract-crm-profile-btn")).toBeDisabled();
    // 老单测关心的既有标签页仍在。
    expect(screen.getByRole("button", { name: /Live SKU & MOQ Stock/i })).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// CRM 客户详情：精简 AI 画像卡片
// ---------------------------------------------------------------------------

describe("Pack E · CRM 客户详情 AI 画像卡片", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  });

  it("shows the profile card for the selected customer", async () => {
    wrap(<CrmCustomersPage />, "zh");

    const card = await screen.findByTestId("crm-ai-profile-card");
    expect(card).toHaveTextContent("AI 客户画像");
    await waitFor(() =>
      expect(screen.getByTestId("crm-ai-intent-score")).toHaveTextContent("82/100")
    );
    expect(screen.getByTestId("crm-ai-next-best-action")).toHaveTextContent(
      "今天内发 FOB 阶梯价 PI"
    );
  });
});
