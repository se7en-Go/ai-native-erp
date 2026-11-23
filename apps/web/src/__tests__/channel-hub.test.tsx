import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useSearchParams } from "next/navigation";

import { I18nProvider } from "@/lib/i18n";
import { AuthSessionContext } from "@/components/providers";
import ChannelsSettingsPage from "@/app/settings/channels/page";
import {
  AGENT_MARKETPLACE_KEYS,
  CHANNEL_MARKETPLACE,
  connectedAccountCount,
  marketplaceChannel,
  marketplaceKeyForChannelFilter,
  visibleMarketplaceItems,
} from "@/components/channels/channel-marketplace";
import { livechatEmbedSnippet } from "@/components/channels/channel-marketplace";
import type { Principal } from "@/lib/contracts";
import type { ChannelAccountApiItem } from "@/lib/api-client/live-client";

const ACCOUNTS: ChannelAccountApiItem[] = [
  {
    id: "ch-telegram-1",
    channel_type: "telegram",
    display_name: "QZ LED 外贸机器人",
    external_account_id: "7012345678",
    status: "active",
    rate_limit_profile: { mps: 30 },
    owner_user_id: null,
    owner_name: null,
    owner_email: null,
    visibility_scope: "shared",
    bot_username: "qzled_sales_bot",
  },
  {
    id: "ch-web-1",
    channel_type: "web_livechat",
    display_name: "官网客服",
    external_account_id: "sk_live_existing",
    status: "active",
    rate_limit_profile: { mps: 50 },
    owner_user_id: null,
    owner_name: null,
    owner_email: null,
    visibility_scope: "shared",
    bot_username: null,
  },
];

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
];

vi.mock("@/lib/data-source", () => ({
  fetchChannelAccounts: vi.fn(async () => ({
    items: ACCOUNTS,
    total: ACCOUNTS.length,
    limit: 20,
    offset: 0,
    has_more: false,
  })),
  fetchChannelDlq: vi.fn(async () => ({
    items: [],
    total: 0,
    limit: 20,
    offset: 0,
    has_more: false,
  })),
  fetchChannelConsents: vi.fn(async () => ({
    items: [],
    total: 0,
    limit: 20,
    offset: 0,
    has_more: false,
  })),
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
  connectMarketplaceChannel: vi.fn(async () => ({
    channel_account_id: "acc-new",
    external_account_id: "sk_live_demo_qzled",
    visibility_scope: "shared",
    bot_username: "qzled_sales_bot",
  })),
  updateChannelOwnership: vi.fn(async () => ACCOUNTS[0]),
  disconnectChannelAccount: vi.fn(async (accountId: string) => ({
    channel_account_id: accountId,
    status: "disconnected",
  })),
  inviteTeamMember: vi.fn(async () => MEMBERS[0]),
  fetchPiTemplateSettings: vi.fn(async () => ({
    company_legal_name: "Quanzhou Smart LED Lighting Co., Ltd.",
    company_address: "No. 88 Photoelectric Industrial Park, Quanzhou, Fujian, China",
    contact_phone: "+86 595 2688 0000",
    contact_email: "sales@301go.net",
    website: "https://www.301go.net",
    tax_or_reg_no: "91350583MA2XQZLED8",
    bank_beneficiary_name: "Quanzhou Smart LED Lighting Co., Ltd.",
    bank_name: "Bank of China, Quanzhou Branch",
    bank_account_no: "4188 0000 0000 1234",
    bank_swift_code: "BKCHCNBJ73A",
    bank_address: "No. 188 Fengze Street, Quanzhou, Fujian, China",
    default_terms_notes: "30% T/T deposit, 70% before shipment.",
    footer_signature_title: "Export Sales Manager",
  })),
  updatePiTemplateSettings: vi.fn(async (payload: unknown) => payload),
}));

import { connectMarketplaceChannel, fetchPiTemplateSettings } from "@/lib/data-source";

function makePrincipal(role: Principal["role"]): Principal {
  return {
    user_id: role === "agent" ? "user-agent-1" : "user-owner-1",
    tenant_id: "t1",
    role,
    email: `${role}@qzled.example.com`,
    tenant_slug: "qz-led-factory",
  };
}

function renderHub(principal: Principal = makePrincipal("owner")) {
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
        <I18nProvider defaultLocale="en">
          <ChannelsSettingsPage />
        </I18nProvider>
      </AuthSessionContext.Provider>
    </QueryClientProvider>
  );
}

function stubSearchParams(search: string) {
  vi.mocked(useSearchParams).mockReturnValue(
    new URLSearchParams(search) as unknown as ReturnType<typeof useSearchParams>,
  );
}

describe("channel marketplace catalogue", () => {
  it("lists all 22 inbox channels across 3 categories and maps inbox filters", () => {
    expect(CHANNEL_MARKETPLACE).toHaveLength(22);
    expect(CHANNEL_MARKETPLACE.filter((item) => item.category === "messaging")).toHaveLength(4);
    expect(CHANNEL_MARKETPLACE.filter((item) => item.category === "email_web")).toHaveLength(2);
    expect(CHANNEL_MARKETPLACE.filter((item) => item.category === "commerce")).toHaveLength(16);
    expect(new Set(CHANNEL_MARKETPLACE.map((item) => item.key)).size).toBe(22);

    expect(visibleMarketplaceItems(true)).toHaveLength(22);
    expect(visibleMarketplaceItems(false).map((item) => item.key)).toEqual(AGENT_MARKETPLACE_KEYS);

    // WhatsApp Cloud 与 Coexistence 合并统计到同一个市场卡片。
    expect(
      connectedAccountCount(marketplaceChannel("whatsapp"), [
        { ...ACCOUNTS[0], channel_type: "whatsapp_cloud" },
        { ...ACCOUNTS[0], channel_type: "whatsapp_coexistence" },
        ACCOUNTS[0],
      ]),
    ).toBe(2);

    expect(marketplaceKeyForChannelFilter("facebook_messenger")).toBe("messenger");
    expect(marketplaceKeyForChannelFilter("whatsapp_coexistence")).toBe("whatsapp");
    expect(marketplaceKeyForChannelFilter("all")).toBeNull();
    expect(marketplaceKeyForChannelFilter("unmapped")).toBeNull();
  });
});

describe("5-tab channel hub", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    stubSearchParams("");
    Object.assign(navigator, {
      clipboard: { writeText: vi.fn(async () => {}) },
    });
  });

  it("renders 5 admin tabs (marketplace, accounts, team, PI template, compliance)", async () => {
    renderHub();

    expect(await screen.findByTestId("channel-kpi-strip")).toBeTruthy();
    expect(screen.getByTestId("hub-tab-marketplace").textContent).toContain("22");
    expect(screen.getByTestId("hub-tab-compliance")).toBeTruthy();
    expect(screen.getByTestId("hub-tab-pi_template")).toBeTruthy();
    await waitFor(() => {
      expect(screen.getByTestId("hub-tab-accounts").textContent).toContain("2");
      expect(screen.getByTestId("hub-tab-team").textContent).toContain("1");
    });

    expect(screen.getByTestId("marketplace-grid").className).toContain("xl:grid-cols-3");
    expect(screen.getAllByTestId(/^marketplace-card-/)).toHaveLength(22);
    expect(screen.getByTestId("marketplace-status-telegram").textContent).toContain(
      "1 connected"
    );
    expect(screen.getByTestId("marketplace-status-shopify").textContent).toContain(
      "Not connected"
    );
  });

  it("keeps exactly 2 tabs for agents (no team / PI template / compliance)", async () => {
    renderHub(makePrincipal("agent"));

    expect(await screen.findByTestId("hub-tab-marketplace")).toBeTruthy();
    expect(screen.getByTestId("hub-tab-accounts")).toBeTruthy();
    expect(screen.queryByTestId("hub-tab-team")).toBeNull();
    expect(screen.queryByTestId("hub-tab-pi_template")).toBeNull();
    expect(screen.queryByTestId("hub-tab-compliance")).toBeNull();
    // The admin-only PI template endpoint is never requested for an agent.
    expect(fetchPiTemplateSettings).not.toHaveBeenCalled();
  });

  it("opens the PI template tab and renders the tenant letterhead editor", async () => {
    renderHub();

    fireEvent.click(await screen.findByTestId("hub-tab-pi_template"));

    await waitFor(() => {
      expect(screen.getByTestId("pi-template-company_legal_name")).toHaveValue(
        "Quanzhou Smart LED Lighting Co., Ltd."
      );
    });
    expect(screen.getByTestId("pi-template-bank_swift_code")).toHaveValue("BKCHCNBJ73A");
    expect(screen.getByTestId("pi-template-default_terms_notes")).toBeTruthy();
  });

  it("filters the grid by category and search without any inline forms", async () => {
    renderHub();
    await screen.findByTestId("marketplace-grid");

    fireEvent.click(screen.getByTestId("marketplace-category-messaging"));
    expect(screen.getAllByTestId(/^marketplace-card-/)).toHaveLength(4);
    expect(screen.queryByTestId("marketplace-card-shopify")).toBeNull();

    fireEvent.click(screen.getByTestId("marketplace-category-commerce"));
    expect(screen.getAllByTestId(/^marketplace-card-/)).toHaveLength(16);

    fireEvent.click(screen.getByTestId("marketplace-category-all"));
    fireEvent.change(screen.getByTestId("marketplace-search"), {
      target: { value: "telegram" },
    });
    expect(screen.getAllByTestId(/^marketplace-card-/)).toHaveLength(1);
    expect(screen.getByTestId("marketplace-card-telegram")).toBeTruthy();

    // 主页面不再有任何内联表单：卡片只负责打开抽屉。
    expect(screen.queryByTestId("connect-form-telegram")).toBeNull();
  });

  it("opens and closes the 480px slide-over drawer", async () => {
    renderHub();
    fireEvent.click(await screen.findByTestId("open-connect-shopify"));

    const drawer = screen.getByTestId("channel-connect-drawer-shopify");
    expect(drawer.getAttribute("role")).toBe("dialog");
    expect(drawer.className).toContain("max-w-[480px]");
    expect(screen.getByTestId("connect-form-shopify")).toBeTruthy();

    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByTestId("channel-connect-drawer-shopify")).toBeNull();

    fireEvent.click(screen.getByTestId("open-connect-web_livechat"));
    fireEvent.click(screen.getByTestId("channel-drawer-backdrop"));
    expect(screen.queryByTestId("channel-connect-drawer-web_livechat")).toBeNull();
  });

  it("connects WhatsApp through the direct-token form", async () => {
    renderHub();
    fireEvent.click(await screen.findByTestId("open-connect-whatsapp"));

    fireEvent.change(screen.getByTestId("whatsapp-waba-id-input"), {
      target: { value: "102290129340398" },
    });
    fireEvent.change(screen.getByTestId("whatsapp-phone-number-id-input"), {
      target: { value: "106540352242922" },
    });
    fireEvent.change(screen.getByTestId("whatsapp-access-token-input"), {
      target: { value: "EAAG-permanent-token" },
    });
    fireEvent.click(screen.getByTestId("connect-whatsapp-btn"));

    await waitFor(() => {
      expect(connectMarketplaceChannel).toHaveBeenCalledWith({
        channel: "whatsapp",
        mode: "direct",
        body: {
          waba_id: "102290129340398",
          phone_number_id: "106540352242922",
          access_token: "EAAG-permanent-token",
          is_coexistence: true,
          visibility_scope: "shared",
        },
      });
    });

    fireEvent.click(screen.getByTestId("drawer-view-accounts"));
    expect(await screen.findByTestId("connected-accounts-card")).toBeTruthy();
    expect(screen.queryByTestId("channel-connect-drawer-whatsapp")).toBeNull();
  });

  it("switches WhatsApp to Meta Embedded Signup mode", async () => {
    renderHub();
    fireEvent.click(await screen.findByTestId("open-connect-whatsapp"));
    fireEvent.click(screen.getByTestId("whatsapp-mode-embedded"));

    expect(screen.queryByTestId("whatsapp-access-token-input")).toBeNull();
    fireEvent.change(screen.getByTestId("whatsapp-waba-id-input"), {
      target: { value: "102290129340398" },
    });
    fireEvent.change(screen.getByTestId("whatsapp-phone-number-id-input"), {
      target: { value: "106540352242922" },
    });
    fireEvent.change(screen.getByTestId("whatsapp-code-input"), {
      target: { value: "AQD-embedded-code" },
    });
    fireEvent.change(screen.getByTestId("whatsapp-display-name-input"), {
      target: { value: "QZ LED WhatsApp" },
    });
    fireEvent.click(screen.getByTestId("connect-whatsapp-btn"));

    await waitFor(() => {
      expect(connectMarketplaceChannel).toHaveBeenCalledWith({
        channel: "whatsapp",
        mode: "embedded",
        body: {
          code: "AQD-embedded-code",
          waba_id: "102290129340398",
          phone_number_id: "106540352242922",
          is_coexistence: true,
          display_name: "QZ LED WhatsApp",
          visibility_scope: "shared",
        },
      });
    });
  });

  it("auto-extracts a pasted BotFather reply and links the new bot", async () => {
    renderHub();
    fireEvent.click(await screen.findByTestId("open-connect-telegram"));

    fireEvent.change(screen.getByTestId("telegram-bot-token-input"), {
      target: {
        value:
          "Use this token to access the HTTP API:\n7012345678:AAH-demo_token_value_1234567890\nKeep your token secure.",
      },
    });
    expect(screen.getByTestId("telegram-detected-bot-id").textContent).toContain("7012345678");

    fireEvent.click(screen.getByTestId("connect-telegram-btn"));
    await waitFor(() => {
      expect(connectMarketplaceChannel).toHaveBeenCalledWith({
        channel: "telegram",
        body: {
          bot_token: "7012345678:AAH-demo_token_value_1234567890",
          visibility_scope: "shared",
        },
      });
    });
    expect(
      (await screen.findByTestId("telegram-open-bot-link")).getAttribute("href")
    ).toBe("https://t.me/qzled_sales_bot");
  });

  it("connects Shopify through the custom-app connector form", async () => {
    renderHub();
    fireEvent.click(await screen.findByTestId("open-connect-shopify"));

    fireEvent.change(screen.getByTestId("shopify-shop-domain-input"), {
      target: { value: "qzled.myshopify.com" },
    });
    fireEvent.change(screen.getByTestId("shopify-client-id-input"), {
      target: { value: "shopify-client-id" },
    });
    fireEvent.change(screen.getByTestId("shopify-client-secret-input"), {
      target: { value: "shopify-client-secret" },
    });
    fireEvent.click(screen.getByTestId("connect-shopify-btn"));

    await waitFor(() => {
      expect(connectMarketplaceChannel).toHaveBeenCalledWith({
        channel: "shopify",
        body: {
          shop_domain: "qzled.myshopify.com",
          client_id: "shopify-client-id",
          client_secret: "shopify-client-secret",
        },
      });
    });
  });

  it("shows a copyable widget snippet after connecting Web LiveChat", async () => {
    renderHub();
    fireEvent.click(await screen.findByTestId("open-connect-web_livechat"));

    fireEvent.change(screen.getByTestId("web-livechat-site-domain-input"), {
      target: { value: "https://shop.example.com" },
    });
    fireEvent.click(screen.getByTestId("connect-web_livechat-btn"));

    const snippet = await screen.findByTestId("livechat-embed-snippet");
    expect(snippet.textContent).toContain("http://localhost:3001/widget.iife.js");
    expect(snippet.textContent).toContain('data-site-key="sk_live_demo_qzled"');
    expect(snippet.textContent).toContain('data-tenant-slug="qz-led-factory"');

    fireEvent.click(screen.getByTestId("copy-livechat-snippet"));
    await waitFor(() => {
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
        livechatEmbedSnippet("sk_live_demo_qzled", "qz-led-factory")
      );
    });
    expect(screen.getByTestId("copy-livechat-snippet").textContent).toContain("Copied");
  });

  it("shows the existing Web LiveChat embed snippet under the accounts tab", async () => {
    renderHub();
    fireEvent.click(await screen.findByTestId("hub-tab-accounts"));

    const snippet = await screen.findByTestId("livechat-snippet-ch-web-1");
    expect(snippet.textContent).toContain('data-site-key="sk_live_existing"');
    expect(screen.getByTestId("accounts-quick-connect-btn")).toBeTruthy();

    fireEvent.click(screen.getByTestId("accounts-quick-connect-btn"));
    expect(await screen.findByTestId("marketplace-grid")).toBeTruthy();
  });

  it("auto-opens the drawer from ?connect=whatsapp and blocks admin channels for agents", async () => {
    stubSearchParams("connect=whatsapp");
    const { unmount } = renderHub(makePrincipal("agent"));

    expect(await screen.findByTestId("channel-connect-drawer-whatsapp")).toBeTruthy();
    unmount();

    stubSearchParams("connect=shopify");
    renderHub(makePrincipal("agent"));
    await screen.findByTestId("channel-marketplace");
    expect(screen.queryByTestId("channel-connect-drawer-shopify")).toBeNull();
  });

  it("opens the deep-linked admin channel drawer for the owner", async () => {
    stubSearchParams("connect=shopify");
    renderHub(makePrincipal("owner"));

    expect(await screen.findByTestId("channel-connect-drawer-shopify")).toBeTruthy();
  });
});
