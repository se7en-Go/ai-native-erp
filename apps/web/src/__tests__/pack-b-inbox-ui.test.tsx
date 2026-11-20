// Pack B · Inbox 导出沟通增强 UI 测试
// 覆盖：一键气泡翻译、实时翻译多语种（RU/AR/PT/KO）、图片 / PDF 附件上传、
// 会话置顶与标签管理、按绑定账号筛选（服务端查询参数下推）。
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
  ConversationList,
  type ConversationSummary,
} from "@/components/inbox/conversation-list";
import { MessageThread, type InboxMessage } from "@/components/inbox/message-thread";
import {
  ATTACHMENT_ACCEPT,
  InboxComposer,
  TARGET_LANGUAGES,
} from "@/components/inbox/composer";
import {
  formatAttachmentSize,
  inboxAttachmentUrl,
} from "@/lib/api-client/pack-b-inbox";
import InboxPage from "@/app/inbox/page";

vi.mock("@/hooks/use-realtime-inbox", () => ({ useRealtimeInbox: vi.fn() }));
// 页面两侧的上下文抽屉 / 悬浮 Copilot 不在本次 Pack B 范围内，替换成轻量桩，
// 让用例只聚焦会话置顶、标签与账号筛选。
vi.mock("@/components/inbox/context-drawer", () => ({
  ContextDrawer: () => <div data-testid="context-drawer-stub" />,
}));
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

const CONVERSATION: ConversationSummary = {
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
  is_pinned: true,
  tags: ["VIP"],
};

const INBOUND_MESSAGE: InboxMessage = {
  id: "msg-9",
  conversation_id: "conv-1",
  channel_type: "made_in_china_email",
  direction: "inbound",
  sender_type: "customer",
  content_type: "text",
  body_text: "Could you ship 300 pcs by air to Hamburg?",
  detected_language: "EN",
  delivery_status: "read",
  source_timestamp: "2026-11-19T08:00:00Z",
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

/** 会话列表 / 消息流 / 渠道账号 / 提案计数的统一 fetch 桩。 */
function stubInboxFetch() {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const { url, method } = normalizeFetchInput(input, init);

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
            body_text: "Could you ship 300 pcs by air to Hamburg?",
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
    if (url.includes("/inbox/conversations/conv-1") && method === "PATCH") {
      const body = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
      return Response.json({
        ...CONVERSATION,
        ...body,
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
      return Response.json({
        items: [
          {
            id: "acct-1",
            channel_type: "made_in_china_email",
            display_name: "sales@301go.net",
            external_account_id: "sales@301go.net",
            status: "active",
          },
          {
            id: "acct-wa",
            channel_type: "whatsapp_cloud",
            display_name: "Quanzhou LED Bot",
            external_account_id: "waba_phone_1",
            status: "active",
          },
        ],
        total: 2,
        limit: 20,
        offset: 0,
        has_more: false,
      });
    }
    if (url.includes("/ai/proposals")) {
      return Response.json({ items: [], total: 0, limit: 20, offset: 0, has_more: false });
    }
    return Response.json({});
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("Pack B · 附件地址与体积格式化", () => {
  it("rewrites the contract download path onto the BFF proxy", () => {
    expect(inboxAttachmentUrl("/api/v1/inbox/attachments/msg-1/0")).toBe(
      "/api/bff/v1/inbox/attachments/msg-1/0"
    );
    expect(inboxAttachmentUrl("/api/bff/v1/inbox/attachments/msg-1/0")).toBe(
      "/api/bff/v1/inbox/attachments/msg-1/0"
    );
    expect(inboxAttachmentUrl("https://s3.example.com/a.png")).toBe(
      "https://s3.example.com/a.png"
    );
    expect(inboxAttachmentUrl("")).toBe("");
  });

  it("formats attachment sizes for the composer pill and file chips", () => {
    expect(formatAttachmentSize(420)).toBe("420 B");
    expect(formatAttachmentSize(4301)).toBe("4.2 KB");
    expect(formatAttachmentSize(3 * 1024 * 1024 + 1024 * 100)).toBe("3.1 MB");
    expect(formatAttachmentSize(undefined)).toBe("");
  });
});

describe("Pack B · 会话列表账号筛选与置顶标签", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the connected-account dropdown, the pinned toggle, pin icon and tag chips", () => {
    const onAccount = vi.fn();
    const onTogglePinned = vi.fn();

    wrap(
      <ConversationList
        conversations={[CONVERSATION]}
        selectedConversationId="conv-1"
        activeFilter="all"
        searchQuery=""
        onSelectFilter={vi.fn()}
        onSearchChange={vi.fn()}
        onSelectConversation={vi.fn()}
        channelAccounts={[
          { id: "acct-1", display_name: "sales@301go.net" },
          { id: "acct-wa", display_name: "Quanzhou LED Bot" },
        ]}
        channelAccountFilter="all"
        onChannelAccountFilterChange={onAccount}
        pinnedOnly={false}
        onTogglePinnedOnly={onTogglePinned}
      />,
      "zh"
    );

    const select = screen.getByTestId("inbox-account-filter") as HTMLSelectElement;
    expect(select).toHaveValue("all");
    // 下拉必须同时给出「全部账号」与两个真实绑定账号。
    expect(select.options[0].textContent).toBe("全部账号");
    expect(Array.from(select.options).map((o) => o.textContent)).toEqual([
      "全部账号",
      "sales@301go.net",
      "Quanzhou LED Bot",
    ]);

    fireEvent.change(select, { target: { value: "acct-wa" } });
    expect(onAccount).toHaveBeenCalledWith("acct-wa");

    fireEvent.click(screen.getByTestId("inbox-pinned-only-btn"));
    expect(onTogglePinned).toHaveBeenCalledTimes(1);

    expect(screen.getByTestId("conversation-pin-conv-1")).toBeInTheDocument();
    expect(screen.getByTestId("conversation-tag-conv-1-VIP")).toHaveTextContent("VIP");
  });
});

describe("Pack B · 一键气泡翻译与附件渲染", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("translates an inbound bubble to Chinese, caches it and can hide the callout", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const { url, method } = normalizeFetchInput(input, init);
      if (url.includes("/ai/translate") && method === "POST") {
        return Response.json({
          translated_text: "能否空运 300 条到汉堡？",
          target_lang: "zh",
        });
      }
      return Response.json({});
    });
    vi.stubGlobal("fetch", fetchMock);

    wrap(
      <MessageThread
        conversation={CONVERSATION}
        messages={[INBOUND_MESSAGE]}
        onToggleDrawer={vi.fn()}
      />,
      "zh"
    );

    fireEvent.click(screen.getByTestId("translate-msg-msg-9"));

    await waitFor(() =>
      expect(screen.getByTestId("translated-bubble-msg-9")).toHaveTextContent(
        "能否空运 300 条到汉堡？"
      )
    );

    const translateCall = fetchMock.mock.calls.find((call) =>
      fetchCallUrl(call).includes("/ai/translate")
    );
    expect(translateCall).toBeDefined();
    expect(JSON.parse(await fetchCallBody(translateCall!))).toEqual({
      text: "Could you ship 300 pcs by air to Hamburg?",
      target_lang: "zh",
    });

    // 隐藏后仍缓存译文，再次展开无需重新请求。
    fireEvent.click(screen.getByTestId("hide-translated-bubble-msg-9"));
    expect(screen.queryByTestId("translated-bubble-msg-9")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("show-translated-bubble-msg-9"));
    expect(screen.getByTestId("translated-bubble-msg-9")).toHaveTextContent(
      "能否空运 300 条到汉堡？"
    );
    expect(
      fetchMock.mock.calls.filter((call) => fetchCallUrl(call).includes("/ai/translate"))
    ).toHaveLength(1);
  });

  it("renders image previews and PDF chips for message attachments", () => {
    const withAttachments: InboxMessage = {
      ...INBOUND_MESSAGE,
      id: "msg-10",
      body_text: "Here are the drawings.",
      attachments: [
        {
          filename: "lighting-drawing.png",
          mime_type: "image/png",
          size_bytes: 20480,
          download_url: "/api/v1/inbox/attachments/msg-10/0",
        },
        {
          filename: "spec-sheet.pdf",
          mime_type: "application/pdf",
          size_bytes: 512000,
          download_url: "/api/v1/inbox/attachments/msg-10/1",
        },
      ],
    };

    wrap(
      <MessageThread
        conversation={CONVERSATION}
        messages={[withAttachments]}
        onToggleDrawer={vi.fn()}
      />,
      "zh"
    );

    const image = screen.getByTestId("message-attachment-image-msg-10-0") as HTMLAnchorElement;
    expect(image.getAttribute("href")).toBe("/api/bff/v1/inbox/attachments/msg-10/0");
    expect(image.getAttribute("target")).toBe("_blank");
    expect(image.querySelector("img")?.getAttribute("src")).toBe(
      "/api/bff/v1/inbox/attachments/msg-10/0"
    );

    const fileChip = screen.getByTestId("message-attachment-file-msg-10-1");
    expect(fileChip).toHaveTextContent("spec-sheet.pdf");
    expect(fileChip).toHaveTextContent("500.0 KB");
    expect(fileChip.getAttribute("href")).toBe(
      "/api/bff/v1/inbox/attachments/msg-10/1"
    );
  });
});

describe("Pack B · 回复框多语种与附件上传", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("offers Chinese/EN/ES/DE/FR/JA/RU/AR/PT/KO and previews RU translation", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const { url, method } = normalizeFetchInput(input, init);
      if (url.includes("/ai/translate") && method === "POST") {
        return Response.json({
          translated_text: "Мы можем отгрузить 300 шт. авиа.",
          target_lang: "ru",
        });
      }
      return Response.json({});
    });
    vi.stubGlobal("fetch", fetchMock);

    wrap(<InboxComposer conversation={CONVERSATION} />, "zh");

    expect(TARGET_LANGUAGES.map((l) => l.code)).toEqual([
      "zh",
      "en",
      "es",
      "de",
      "fr",
      "ja",
      "ru",
      "ar",
      "pt",
      "ko",
    ]);

    const languageSelect = screen.getByTestId(
      "composer-target-language"
    ) as HTMLSelectElement;
    const labels = Array.from(languageSelect.options).map((o) => o.textContent ?? "");
    for (const label of ["中文 (ZH)", "俄语 (RU)", "阿拉伯语 (AR)", "葡语 (PT)", "韩语 (KO)"]) {
      expect(labels).toContain(label);
    }

    fireEvent.change(languageSelect, { target: { value: "ru" } });
    fireEvent.click(screen.getByTestId("live-translate-toggle"));
    fireEvent.change(screen.getByTestId("composer-textarea"), {
      target: { value: "我们空运 300 条没问题" },
    });

    await waitFor(() =>
      expect(screen.getByTestId("live-translation-preview")).toHaveTextContent(
        "Мы можем отгрузить 300 шт. авиа."
      )
    );

    const translateCall = fetchMock.mock.calls.find((call) =>
      fetchCallUrl(call).includes("/ai/translate")
    );
    expect(JSON.parse(await fetchCallBody(translateCall!))).toMatchObject({
      text: "我们空运 300 条没问题",
      target_lang: "ru",
    });
  });

  it("previews a picked PDF, removes it, and uploads it as multipart on send", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const { url } = normalizeFetchInput(input, init);
      if (url.includes("/inbox/conversations/conv-1/attachments")) {
        return Response.json({
          id: "msg-uploaded",
          conversation_id: "conv-1",
          body_text: "请查收规格书",
          attachments: [
            {
              filename: "spec-sheet.pdf",
              mime_type: "application/pdf",
              size_bytes: 512000,
              download_url: "/api/v1/inbox/attachments/msg-uploaded/0",
            },
          ],
        });
      }
      return Response.json({});
    });
    vi.stubGlobal("fetch", fetchMock);

    wrap(<InboxComposer conversation={CONVERSATION} />, "zh");

    const fileInput = screen.getByTestId("attach-file-input") as HTMLInputElement;
    expect(fileInput.getAttribute("accept")).toBe(ATTACHMENT_ACCEPT);
    expect(fileInput.multiple).toBe(false);

    const pdf = new File(["%PDF-1.7 spec"], "spec-sheet.pdf", {
      type: "application/pdf",
    });
    fireEvent.change(fileInput, { target: { files: [pdf] } });

    expect(screen.getByTestId("attach-preview-pill")).toHaveTextContent("spec-sheet.pdf");
    expect(screen.getByTestId("attach-preview-pill")).toHaveTextContent("13 B");

    // 移除后不残留，重新选择再发送。
    fireEvent.click(screen.getByTestId("attach-preview-remove"));
    expect(screen.queryByTestId("attach-preview-pill")).not.toBeInTheDocument();

    fireEvent.change(fileInput, { target: { files: [pdf] } });
    fireEvent.change(screen.getByTestId("composer-textarea"), {
      target: { value: "请查收规格书" },
    });

    const attachBtn = screen.getByTestId("attach-file-btn");
    expect(attachBtn).toHaveTextContent("发图片 / PDF");

    fireEvent.click(screen.getByTestId("composer-submit-btn"));

    await waitFor(() =>
      expect(screen.getByTestId("composer-attachment-notice")).toBeInTheDocument()
    );

    const uploadCall = fetchMock.mock.calls.find((call) =>
      fetchCallUrl(call).includes("/inbox/conversations/conv-1/attachments")
    );
    expect(uploadCall).toBeDefined();
    expect(fetchCallUrl(uploadCall!)).toBe(
      "/api/bff/v1/inbox/conversations/conv-1/attachments"
    );
    expect(fetchCallMethod(uploadCall!)).toBe("POST");

    const init = uploadCall![1] as RequestInit;
    expect(init.body).toBeInstanceOf(FormData);
    // 浏览器必须自己写 multipart boundary：不得手动设置 Content-Type。
    expect(new Headers(init.headers).get("content-type")).toBeNull();

    const form = init.body as FormData;
    const sentFile = form.get("file") as File;
    expect(sentFile.name).toBe("spec-sheet.pdf");
    expect(sentFile.type).toBe("application/pdf");
    expect(form.get("caption")).toBe("请查收规格书");

    // 上传成功后清空草稿与预览胶囊。
    expect(screen.queryByTestId("attach-preview-pill")).not.toBeInTheDocument();
    expect((screen.getByTestId("composer-textarea") as HTMLTextAreaElement).value).toBe("");
  });
});

describe("Pack B · /inbox 页面置顶、标签与账号筛选", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("pushes account/pinned filters to the server, then pins and tags the open conversation", async () => {
    const fetchMock = stubInboxFetch();

    wrap(<InboxPage />, "zh");

    await waitFor(() =>
      expect(screen.getByTestId("conversation-item-conv-1")).toBeInTheDocument()
    );

    // 会话头部：已置顶状态 + 已打标签同步显示。
    expect(screen.getByTestId("conversation-header-pin")).toBeInTheDocument();
    expect(screen.getByTestId("conversation-header-tag-VIP")).toBeInTheDocument();

    // 「按绑定账号筛选」下拉由 /channels/accounts 填充。
    const accountSelect = screen.getByTestId("inbox-account-filter") as HTMLSelectElement;
    await waitFor(() =>
      expect(Array.from(accountSelect.options).map((o) => o.textContent)).toEqual([
        "全部账号",
        "sales@301go.net",
        "Quanzhou LED Bot",
      ])
    );

    fireEvent.change(accountSelect, { target: { value: "acct-wa" } });
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some((call) =>
          fetchCallUrl(call).includes("channel_account_id=acct-wa")
        )
      ).toBe(true);
    });

    fireEvent.click(screen.getByTestId("inbox-pinned-only-btn"));
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some((call) => fetchCallUrl(call).includes("pinned_only=true"))
      ).toBe(true);
    });

    // 取消置顶：PATCH 携带 { is_pinned: false }，头部置顶图标随即消失。
    fireEvent.click(screen.getByTestId("toggle-pin-conv-btn"));
    await waitFor(() => {
      const patchCall = fetchMock.mock.calls.find(
        (call) =>
          fetchCallUrl(call).includes("/inbox/conversations/conv-1") &&
          fetchCallMethod(call) === "PATCH"
      );
      expect(patchCall).toBeDefined();
    });
    const pinPatch = fetchMock.mock.calls.find(
      (call) =>
        fetchCallUrl(call).includes("/inbox/conversations/conv-1") &&
        fetchCallMethod(call) === "PATCH"
    );
    expect(JSON.parse(await fetchCallBody(pinPatch!))).toEqual({ is_pinned: false });
    await waitFor(() =>
      expect(screen.queryByTestId("conversation-header-pin")).not.toBeInTheDocument()
    );

    // 自定义标签：输入框回车 → PATCH 整体覆盖 tags（保留既有 VIP）。
    fireEvent.change(screen.getByTestId("add-conv-tag-input"), {
      target: { value: "大客户" },
    });
    fireEvent.keyDown(screen.getByTestId("add-conv-tag-input"), { key: "Enter" });

    await waitFor(async () => {
      const tagCalls = fetchMock.mock.calls.filter(
        (call) =>
          fetchCallUrl(call).includes("/inbox/conversations/conv-1") &&
          fetchCallMethod(call) === "PATCH"
      );
      const tagged = await Promise.all(tagCalls.map((call) => fetchCallBody(call)));
      expect(tagged.some((body) => body.includes("大客户"))).toBe(true);
    });
    expect(screen.getByTestId("conversation-header-tag-大客户")).toBeInTheDocument();
    expect(screen.getByTestId("conversation-header-tag-VIP")).toBeInTheDocument();

    // 预设标签面板：点「待寄样」直接写入服务端。
    fireEvent.click(screen.getByTestId("conv-tag-panel-btn"));
    fireEvent.click(screen.getByTestId("conv-tag-preset-待寄样"));
    await waitFor(() =>
      expect(screen.getByTestId("conversation-header-tag-待寄样")).toBeInTheDocument()
    );

    // 移除标签同样走 PATCH。
    fireEvent.click(screen.getByTestId("remove-conv-tag-待寄样"));
    await waitFor(() =>
      expect(screen.queryByTestId("conversation-header-tag-待寄样")).not.toBeInTheDocument()
    );
  });
});
