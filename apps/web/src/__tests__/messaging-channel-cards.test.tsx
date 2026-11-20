import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { I18nProvider } from "@/lib/i18n";
import { AuthSessionContext } from "@/components/providers";
import {
  MessagingChannelCard,
  extractTelegramBotToken,
  isMessagingChannelDraftValid,
} from "@/components/channels/messaging-channel-cards";
import { isEmailConnectDraftValid } from "@/components/channels/quick-connect-panel";
import { canAccessRoute } from "@/lib/auth-cookie";
import ChannelsSettingsPage from "@/app/settings/channels/page";
import type { Principal } from "@/lib/contracts";

const TELEGRAM_ACCOUNT = {
  id: "ch-telegram-1",
  channel_type: "telegram",
  display_name: "QZ LED 外贸机器人",
  external_account_id: "555000111",
  status: "active",
  rate_limit_profile: { mps: 30 },
  owner_user_id: "user-owner-1",
  owner_name: "企业所有者",
  owner_email: "owner@qzled.example.com",
  visibility_scope: "shared",
  bot_username: "qzled_shared_bot",
};

const MEMBERS = [
  {
    membership_id: "mem-owner-1",
    tenant_id: "t1",
    user_id: "user-owner-1",
    email: "owner@qzled.example.com",
    full_name: "企业所有者",
    role: "owner",
    is_active: true,
    created_at: "2026-10-01T00:00:00Z",
  },
  {
    membership_id: "mem-agent-1",
    tenant_id: "t1",
    user_id: "user-agent-1",
    email: "sales.li@qzled.example.com",
    full_name: "李明伦",
    role: "agent",
    is_active: true,
    created_at: "2026-10-08T00:00:00Z",
  },
];

vi.mock("@/lib/data-source", () => ({
  fetchChannelAccounts: vi.fn(async () => ({
    items: [
      {
        id: "ch-messenger-1",
        channel_type: "messenger",
        display_name: "QZ LED Facebook Page",
        external_account_id: "1010101010",
        status: "active",
        rate_limit_profile: { mps: 80 },
        owner_user_id: null,
        owner_name: null,
        owner_email: null,
        visibility_scope: "shared",
        bot_username: null,
      },
      TELEGRAM_ACCOUNT,
      {
        id: "ch-telegram-agent-1",
        channel_type: "telegram",
        display_name: "李明伦 个人机器人",
        external_account_id: "555000222",
        status: "active",
        rate_limit_profile: { mps: 30 },
        owner_user_id: "user-agent-1",
        owner_name: "李明伦",
        owner_email: "sales.li@qzled.example.com",
        visibility_scope: "personal",
        bot_username: "qzled_li_bot",
      },
    ],
    total: 3,
    limit: 20,
    offset: 0,
    has_more: false,
  })),
  fetchChannelDlq: vi.fn(async () => ({ items: [], total: 0, limit: 20, offset: 0, has_more: false })),
  fetchChannelConsents: vi.fn(async () => ({ items: [], total: 0, limit: 20, offset: 0, has_more: false })),
  fetchChannelSuppressions: vi.fn(async () => ({
    items: [],
    total: 0,
    limit: 20,
    offset: 0,
    has_more: false,
  })),
  fetchPendingProposalsCount: vi.fn(async () => ({ total: 0 })),
  replayDlqEvent: vi.fn(async () => ({ status: "replayed" })),
  fetchTeamMembers: vi.fn(async () => ({
    items: MEMBERS,
    total: MEMBERS.length,
    limit: 100,
    offset: 0,
    has_more: false,
  })),
  connectMessagingChannel: vi.fn(
    async (_kind: string, payload: Record<string, string | null>) => ({
      channel_account_id: "5e0f0a4c-0000-4000-8000-000000000001",
      external_account_id:
        payload.ig_account_id ?? payload.page_id ?? String(payload.bot_token).split(":")[0],
      bot_username: "qzled_sales_bot",
      visibility_scope: payload.visibility_scope ?? "shared",
    }),
  ),
  connectEmailChannel: vi.fn(async (payload: Record<string, string>) => ({
    channel_account_id: "5e0f0a4c-0000-4000-8000-000000000002",
    external_account_id: payload.email_address,
  })),
  connectMarketplaceChannel: vi.fn(
    async (request: { channel: string; body: Record<string, unknown> }) => ({
      channel_account_id: "5e0f0a4c-0000-4000-8000-000000000001",
      external_account_id: String(
        request.body.ig_account_id ??
          request.body.page_id ??
          request.body.phone_number_id ??
          request.body.email_address ??
          request.body.bot_token ??
          ""
      ).split(":")[0],
      bot_username: request.channel === "telegram" ? "qzled_sales_bot" : null,
      visibility_scope: (request.body.visibility_scope as string) ?? "shared",
    })
  ),
  updateChannelOwnership: vi.fn(async (accountId: string) => ({ ...TELEGRAM_ACCOUNT, id: accountId })),
  disconnectChannelAccount: vi.fn(async (accountId: string) => ({
    channel_account_id: accountId,
    status: "disconnected",
  })),
  inviteTeamMember: vi.fn(async (payload: Record<string, string>) => ({
    membership_id: "mem-new-1",
    tenant_id: "t1",
    user_id: "user-new-1",
    email: payload.email,
    full_name: payload.full_name,
    role: payload.role,
    is_active: true,
    created_at: "2026-11-20T00:00:00Z",
  })),
}));

import {
  connectMarketplaceChannel,
  disconnectChannelAccount,
  fetchChannelConsents,
  fetchChannelDlq,
  fetchChannelSuppressions,
  fetchTeamMembers,
  inviteTeamMember,
  updateChannelOwnership,
} from "@/lib/data-source";

function makePrincipal(role: Principal["role"]): Principal {
  return {
    user_id: role === "agent" ? "user-agent-1" : "user-owner-1",
    tenant_id: "t1",
    role,
    email: `${role}@qzled.example.com`,
  };
}

function renderWithProviders(ui: React.ReactNode, principal: Principal = makePrincipal("owner")) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AuthSessionContext.Provider
        value={{
          principal,
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

describe("Phase 3D messaging channel connections", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("requires a page id, access token and display name before enabling Messenger connect", () => {
    const onSubmit = vi.fn(async () => {});
    renderWithProviders(
      <MessagingChannelCard kind="messenger" connected={false} onSubmit={onSubmit} />
    );

    const button = screen.getByTestId("connect-messenger-btn") as HTMLButtonElement;
    expect(button.disabled).toBe(true);

    fireEvent.change(screen.getByTestId("messenger-page-id-input"), {
      target: { value: "1010101010" },
    });
    fireEvent.change(screen.getByTestId("messenger-access-token-input"), {
      target: { value: "  EAAG-secret-token  " },
    });
    fireEvent.change(screen.getByTestId("messenger-display-name-input"), {
      target: { value: "QZ LED Page" },
    });
    expect(button.disabled).toBe(false);

    fireEvent.click(button);
    expect(onSubmit).toHaveBeenCalledWith({
      page_id: "1010101010",
      page_access_token: "EAAG-secret-token",
      display_name: "QZ LED Page",
    });
  });

  it("rejects Telegram tokens without a bot id and surfaces provider failures", async () => {
    expect(isMessagingChannelDraftValid("telegram", { bot_token: "abc", display_name: "Bot" })).toBe(
      false
    );
    expect(
      isMessagingChannelDraftValid("telegram", {
        bot_token: "123456:AAH-test-token",
        display_name: "Bot",
      })
    ).toBe(true);

    const onSubmit = vi.fn(async () => {
      throw new Error("ERR_INVALID_CHANNEL_CONNECTION");
    });
    renderWithProviders(<MessagingChannelCard kind="telegram" connected={false} onSubmit={onSubmit} />);
    fireEvent.change(screen.getByTestId("telegram-bot-token-input"), {
      target: { value: "123456:AAH-test-token" },
    });
    fireEvent.change(screen.getByTestId("telegram-display-name-input"), {
      target: { value: "Telegram Sales" },
    });
    fireEvent.click(screen.getByTestId("connect-telegram-btn"));

    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toContain("ERR_INVALID_CHANNEL_CONNECTION");
    });
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("opens the instagram drawer from the marketplace and connects the channel", async () => {
    renderWithProviders(<ChannelsSettingsPage />);

    expect(await screen.findByTestId("marketplace-card-instagram_dm")).toBeTruthy();
    fireEvent.click(screen.getByTestId("open-connect-instagram_dm"));
    expect(await screen.findByTestId("channel-connect-drawer-instagram_dm")).toBeTruthy();

    fireEvent.change(screen.getByTestId("instagram-account-id-input"), {
      target: { value: "17841400000000000" },
    });
    fireEvent.change(screen.getByTestId("instagram-access-token-input"), {
      target: { value: "IGQVJ-test-token" },
    });
    fireEvent.change(screen.getByTestId("instagram-display-name-input"), {
      target: { value: "QZ LED Instagram" },
    });
    fireEvent.click(screen.getByTestId("connect-instagram_dm-btn"));

    await waitFor(() => {
      expect(connectMarketplaceChannel).toHaveBeenCalledWith({
        channel: "instagram_dm",
        body: {
          ig_account_id: "17841400000000000",
          access_token: "IGQVJ-test-token",
          display_name: "QZ LED Instagram",
          visibility_scope: "shared",
        },
      });
    });
    expect((await screen.findByTestId("connect-success-instagram_dm")).textContent).toContain(
      "17841400000000000"
    );
  });
});

describe("0009 quick channel binding — paste a whole BotFather reply", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  const BOTFATHER_REPLY = [
    "Done! Congratulations on your new bot. You will find it at t.me/qzled_sales_bot.",
    "Use this token to access the HTTP API:",
    "7012345678:AAH-demo_token_value_1234567890",
    "Keep your token secure and store it safely, it can be used by anyone to control your bot.",
  ].join("\n");

  it("extracts the exact digits:token pair from raw tokens and pasted BotFather text", () => {
    expect(extractTelegramBotToken("7012345678:AAH-demo_token_value_1234567890")).toBe(
      "7012345678:AAH-demo_token_value_1234567890"
    );
    expect(extractTelegramBotToken(`  ${BOTFATHER_REPLY}  `)).toBe(
      "7012345678:AAH-demo_token_value_1234567890"
    );
    expect(extractTelegramBotToken("not-a-token")).toBe("not-a-token");
    expect(extractTelegramBotToken("")).toBe("");
    // Telegram 的显示名是可选字段：只贴 token 即视为可提交。
    expect(
      isMessagingChannelDraftValid("telegram", {
        bot_token: "7012345678:AAH-demo_token_value_1234567890",
      })
    ).toBe(true);
    expect(isMessagingChannelDraftValid("messenger", { page_id: "1", page_access_token: "x" })).toBe(
      false
    );
  });

  it("binds Telegram with a single paste: no display name typed, personal scope locked to self", async () => {
    const onSubmit = vi.fn(async () => ({
      channel_account_id: "acc-1",
      external_account_id: "7012345678",
      bot_username: "qzled_sales_bot",
      visibility_scope: "personal" as const,
    }));
    renderWithProviders(
      <MessagingChannelCard
        kind="telegram"
        connected={false}
        isAdmin={false}
        currentUserId="user-agent-1"
        onSubmit={onSubmit}
      />
    );

    const button = screen.getByTestId("connect-telegram-btn") as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(screen.getByTestId("ownership-locked-telegram")).toBeTruthy();

    fireEvent.change(screen.getByTestId("telegram-bot-token-input"), {
      target: { value: BOTFATHER_REPLY },
    });

    expect(screen.getByTestId("telegram-detected-bot-id").textContent).toContain(
      "7012345678"
    );
    expect((screen.getByTestId("telegram-bot-token-input") as HTMLInputElement).value).toBe(
      "7012345678:AAH-demo_token_value_1234567890"
    );
    expect(button.disabled).toBe(false);

    fireEvent.click(button);

    await waitFor(() => {
      expect(onSubmit).toHaveBeenCalledWith({
        bot_token: "7012345678:AAH-demo_token_value_1234567890",
        visibility_scope: "personal",
        owner_user_id: "user-agent-1",
      });
    });

    const success = await screen.findByTestId("connect-success-telegram");
    const link = screen.getByTestId("telegram-open-bot-link") as HTMLAnchorElement;
    expect(success.textContent).toContain("7012345678");
    expect(link.getAttribute("href")).toBe("https://t.me/qzled_sales_bot");
  });

  it("shows the 3-step BotFather guide on demand", () => {
    renderWithProviders(
      <MessagingChannelCard kind="telegram" connected={false} onSubmit={vi.fn(async () => {})} />
    );
    expect(screen.queryByTestId("telegram-guide-steps")).toBeNull();
    fireEvent.click(screen.getByTestId("telegram-guide-toggle"));
    const steps = screen.getByTestId("telegram-guide-steps");
    expect(steps.textContent).toContain("/newbot");
    expect(steps.textContent).toContain("webhook");
  });

  it("validates Resend and SMTP email drafts, and connects a mailbox", async () => {
    expect(
      isEmailConnectDraftValid("resend", { email_address: "alice@company.com", api_key: "re_x" })
    ).toBe(false);
    expect(
      isEmailConnectDraftValid("resend", {
        email_address: "alice@company.com",
        api_key: "re_123456789",
      })
    ).toBe(true);
    expect(
      isEmailConnectDraftValid("smtp_imap", {
        email_address: "alice@company.com",
        smtp_host: "smtp.company.com",
        smtp_username: "alice@company.com",
        smtp_password: "secret-pass",
      })
    ).toBe(true);

    const onSubmit = vi.fn(async () => ({
      channel_account_id: "acc-email",
      external_account_id: "alice@company.com",
    }));
    const { EmailQuickConnectCard } = await import("@/components/channels/quick-connect-panel");
    renderWithProviders(
      <EmailQuickConnectCard
        connected={false}
        isAdmin={false}
        currentUserId="user-agent-1"
        onSubmit={onSubmit}
      />
    );

    const button = screen.getByTestId("connect-email-btn") as HTMLButtonElement;
    expect(button.disabled).toBe(true);

    fireEvent.change(screen.getByTestId("email-address-input"), {
      target: { value: "alice@company.com" },
    });
    fireEvent.change(screen.getByTestId("email-api-key-input"), {
      target: { value: "re_123456789" },
    });
    expect(button.disabled).toBe(false);
    fireEvent.click(button);

    await waitFor(() => {
      expect(onSubmit).toHaveBeenCalledWith(
        expect.objectContaining({
          email_address: "alice@company.com",
          provider: "resend",
          api_key: "re_123456789",
          visibility_scope: "personal",
          owner_user_id: "user-agent-1",
        })
      );
    });
    expect((await screen.findByTestId("connect-success-email")).textContent).toContain(
      "alice@company.com"
    );
  });
});

describe("0009 salesperson ownership — RBAC and admin-only data", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("lets an agent reach /settings/channels but still blocks /finance", () => {
    expect(canAccessRoute("agent", "/settings/channels")).toBe(true);
    expect(canAccessRoute("manager", "/settings/channels")).toBe(true);
    expect(canAccessRoute("finance", "/settings/channels")).toBe(false);
    expect(canAccessRoute("agent", "/finance")).toBe(false);
    expect(canAccessRoute("agent", "/settings")).toBe(false);
  });

  it("renders only 2 tabs and 3 personal channels for a salesperson, with zero admin calls", async () => {
    renderWithProviders(<ChannelsSettingsPage />, makePrincipal("agent"));

    expect(await screen.findByTestId("channel-marketplace")).toBeTruthy();

    // 业务员只看到 Telegram / 工作邮箱 / WhatsApp 共存号三个个人渠道。
    expect(screen.getByTestId("marketplace-card-telegram")).toBeTruthy();
    expect(screen.getByTestId("marketplace-card-email")).toBeTruthy();
    expect(screen.getByTestId("marketplace-card-whatsapp")).toBeTruthy();
    expect(screen.queryByTestId("marketplace-card-messenger")).toBeNull();
    expect(screen.queryByTestId("marketplace-card-shopify")).toBeNull();
    expect(screen.queryByTestId("marketplace-card-web_livechat")).toBeNull();

    // 只有 2 个分区：应用市场 + 已接入账号；团队/合规 tab 不渲染。
    expect(screen.getByTestId("hub-tab-marketplace")).toBeTruthy();
    expect(screen.getByTestId("hub-tab-accounts")).toBeTruthy();
    expect(screen.queryByTestId("hub-tab-team")).toBeNull();
    expect(screen.queryByTestId("hub-tab-compliance")).toBeNull();
    expect(screen.queryByTestId("team-members-card")).toBeNull();

    // 业务员的绑定归属锁定为个人专属。
    fireEvent.click(screen.getByTestId("open-connect-telegram"));
    expect(screen.getByTestId("ownership-locked-telegram")).toBeTruthy();
    fireEvent.click(screen.getByTestId("channel-drawer-close"));
    expect(screen.queryByTestId("channel-connect-drawer-telegram")).toBeNull();

    fireEvent.click(screen.getByTestId("open-connect-email"));
    expect(screen.getByTestId("ownership-locked-email")).toBeTruthy();

    // 打开 WhatsApp 抽屉：共存模式被锁定为开启。
    fireEvent.click(screen.getByTestId("channel-drawer-close"));
    fireEvent.click(screen.getByTestId("open-connect-whatsapp"));
    const coexistence = screen.getByTestId("whatsapp-coexistence-toggle") as HTMLInputElement;
    expect(coexistence.checked).toBe(true);
    expect(coexistence.disabled).toBe(true);

    expect(fetchChannelDlq).not.toHaveBeenCalled();
    expect(fetchChannelConsents).not.toHaveBeenCalled();
    expect(fetchChannelSuppressions).not.toHaveBeenCalled();
    expect(fetchTeamMembers).not.toHaveBeenCalled();
  });

  it("lets the owner assign a salesperson, invite a new one and disconnect an account", async () => {
    renderWithProviders(<ChannelsSettingsPage />, makePrincipal("owner"));

    // 团队成员与开号在独立 tab；等成员加载完成，归属下拉才有可选项。
    fireEvent.click(await screen.findByTestId("hub-tab-team"));
    await screen.findByTestId("team-member-row-mem-agent-1");

    fireEvent.click(screen.getByTestId("hub-tab-accounts"));

    // 归属徽标：公海账号显示「公司公海」，并带 Telegram 机器人直链。
    const badge = await screen.findByTestId("account-ownership-badge-ch-telegram-1");
    expect(badge.textContent).toContain("Company shared");
    expect(
      (screen.getByTestId("account-bot-link-ch-telegram-1") as HTMLAnchorElement).getAttribute(
        "href"
      )
    ).toBe("https://t.me/qzled_shared_bot");

    fireEvent.click(screen.getByTestId("account-toggle-ownership-ch-telegram-1"));
    fireEvent.click(screen.getByTestId("ownership-edit-personal-ch-telegram-1"));
    fireEvent.change(screen.getByTestId("ownership-edit-owner-ch-telegram-1"), {
      target: { value: "user-agent-1" },
    });
    fireEvent.click(screen.getByTestId("ownership-save-ch-telegram-1"));

    await waitFor(() => {
      expect(updateChannelOwnership).toHaveBeenCalledWith("ch-telegram-1", {
        visibility_scope: "personal",
        owner_user_id: "user-agent-1",
        reassign_open_conversations: true,
      });
    });

    // 成员与开号在独立 tab。
    fireEvent.click(screen.getByTestId("hub-tab-team"));

    // 新建业务员 → 刷新成员列表。
    fireEvent.change(screen.getByTestId("member-invite-name"), {
      target: { value: "王小明" },
    });
    fireEvent.change(screen.getByTestId("member-invite-email"), {
      target: { value: "wang@qzled.example.com" },
    });
    fireEvent.change(screen.getByTestId("member-invite-password"), {
      target: { value: "StrongPass123" },
    });
    fireEvent.click(screen.getByTestId("member-invite-submit"));

    await waitFor(() => {
      expect(inviteTeamMember).toHaveBeenCalledWith({
        email: "wang@qzled.example.com",
        full_name: "王小明",
        password: "StrongPass123",
        role: "agent",
      });
    });
    expect((await screen.findByTestId("member-invite-success")).textContent).toContain(
      "wang@qzled.example.com"
    );

    // 回到账号 tab：管理员可断开任意账号。
    fireEvent.click(screen.getByTestId("hub-tab-accounts"));
    fireEvent.click(await screen.findByTestId("account-disconnect-ch-messenger-1"));
    await waitFor(() => {
      expect(disconnectChannelAccount).toHaveBeenCalledWith("ch-messenger-1");
    });
  });

  it("filters connected accounts by ownership (mine / shared) for a salesperson", async () => {
    renderWithProviders(<ChannelsSettingsPage />, makePrincipal("agent"));
    fireEvent.click(await screen.findByTestId("hub-tab-accounts"));
    await screen.findByTestId("channel-account-row-ch-telegram-agent-1");

    fireEvent.click(screen.getByTestId("accounts-filter-shared"));
    expect(screen.queryByTestId("channel-account-row-ch-telegram-agent-1")).toBeNull();
    expect(screen.getByTestId("channel-account-row-ch-messenger-1")).toBeTruthy();
    // 业务员在公海账号上没有断开按钮，只有自己的个人渠道才有。
    expect(screen.queryByTestId("account-disconnect-ch-messenger-1")).toBeNull();

    fireEvent.click(screen.getByTestId("accounts-filter-mine"));
    expect(screen.queryByTestId("channel-account-row-ch-messenger-1")).toBeNull();
    expect(screen.getByTestId("channel-account-row-ch-telegram-agent-1")).toBeTruthy();
    expect(screen.getByTestId("account-disconnect-ch-telegram-agent-1")).toBeTruthy();

    fireEvent.click(screen.getByTestId("accounts-filter-all"));
    expect(screen.getByTestId("channel-account-row-ch-telegram-1")).toBeTruthy();
  });
});
