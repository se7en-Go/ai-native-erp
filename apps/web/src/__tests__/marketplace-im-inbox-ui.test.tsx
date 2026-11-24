// 12 大跨境电商平台「买家站内私信 IM」前端契约。
//
// 1. `/inbox` 会话头部：会话渠道属于 12 个买家 IM 渠道时，必须在
//    `channel_type` 徽标旁渲染 `marketplace-im-channel-badge`
//    （`站内私信 IM` / `Marketplace Buyer IM`），独立站不渲染该徽标；
// 2. `/inbox` 输入框：同一批渠道 + `mode === "outbound"` 时渲染
//    `composer-marketplace-im-hint`，且发送按钮照常可用（不阻断出站）；
// 3. `/settings/channels` 渠道应用市场：12 张买家 IM 卡片的能力徽标同时
//    提示「商品/库存/订单 + 买家 IM」，独立站（Shopify / WooCommerce /
//    SHOPLINE / Shoplazza）保持纯 `order_sync` 且不提买家 IM。
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { AuthSessionContext } from "@/components/providers";
import {
  InboxComposer,
} from "@/components/inbox/composer";
import type { ConversationSummary } from "@/components/inbox/conversation-list";
import {
  MARKETPLACE_IM_CHANNEL_TYPES,
  MessageThread,
  type InboxMessage,
} from "@/components/inbox/message-thread";
import {
  CHANNEL_MARKETPLACE,
  ChannelMarketplaceGrid,
  MARKETPLACE_BUYER_IM_KEYS,
  marketplaceChannel,
} from "@/components/channels/channel-marketplace";
import { I18nProvider } from "@/lib/i18n";
import type { ChannelType } from "@/lib/contracts";

/** 9 个本轮重点验证的买家 IM 渠道（与需求清单一致）。 */
const MARKETPLACE_IM_CONVERSATIONS: ChannelType[] = [
  "shopee",
  "ozon",
  "temu",
  "mercado_libre",
  "lazada",
  "shein",
  "aliexpress",
  "walmart",
  "tiktok_shop",
];

/** 另外 3 个买家 IM 渠道：渠道键与市场键不同（amazon_sp / alibaba_email / ebay）。 */
const MARKETPLACE_IM_ALIAS_CONVERSATIONS: ChannelType[] = [
  "ebay",
  "amazon_sp",
  "alibaba_email",
];

/** 独立站渠道：没有任何平台买家 IM，不得出现买家 IM 徽标 / 提示。 */
const STOREFRONT_CONVERSATIONS: ChannelType[] = ["shopify", "woocommerce", "shopline", "shoplazza"];

/** 对应的 4 个独立站市场键（用于渠道应用市场卡片断言）。 */
const STOREFRONT_MARKETPLACE_KEYS = ["shopify", "woocommerce", "shopline", "shoplazza"] as const;

function conversationFor(channelType: ChannelType): ConversationSummary {
  return {
    id: `conv-${channelType}`,
    customer_id: `cust-${channelType}`,
    customer_name: "Maria Santos",
    country_code: "BR",
    iana_timezone: "America/Sao_Paulo",
    channel_account_id: `acct-${channelType}`,
    channel_type: channelType,
    conversation_mode: "b2c_support",
    status: "pending_human",
    unread_count: 1,
    sla_due_at: "2026-11-26T12:00:00Z",
    csw_expires_at: null,
    human_agent_window_expires_at: null,
    last_message_preview: "Is the 24V COB strip still in stock?",
    updated_at: "2026-11-26T08:00:00Z",
  };
}

function messagesFor(channelType: ChannelType): InboxMessage[] {
  return [
    {
      id: `msg-${channelType}`,
      conversation_id: `conv-${channelType}`,
      channel_type: channelType,
      direction: "inbound",
      sender_type: "customer",
      content_type: "text",
      body_text: "Is the 24V COB strip still in stock?",
      detected_language: "PT",
      delivery_status: "read",
      source_timestamp: "2026-11-26T08:00:00Z",
    },
  ];
}

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
    </QueryClientProvider>,
  );
}

function renderMarketplaceGrid(locale: "zh" | "en" = "zh") {
  return render(
    <I18nProvider defaultLocale={locale}>
      <ChannelMarketplaceGrid accounts={[]} isAdmin onOpen={vi.fn()} />
    </I18nProvider>,
  );
}

describe("marketplace buyer IM channels: conversation header badge", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ items: [] }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it.each(MARKETPLACE_IM_CONVERSATIONS)(
    "renders the buyer IM badge for a %s conversation",
    (channelType: ChannelType) => {
      wrap(
        <MessageThread
          conversation={conversationFor(channelType)}
          messages={messagesFor(channelType)}
          onToggleDrawer={vi.fn()}
        />,
        "zh",
      );

      const badge = screen.getByTestId("marketplace-im-channel-badge");
      expect(badge).toBeInTheDocument();
      expect(badge.textContent).toContain("站内私信 IM");
      expect(badge.getAttribute("data-marketplace-im-channel")).toBe(channelType);
      // 渠道类型徽标本身仍然保留（徽标只是附加信息）。
      expect(screen.getByTestId("inbox-message-thread").textContent).toContain(channelType);
    },
  );

  it.each(MARKETPLACE_IM_ALIAS_CONVERSATIONS)(
    "renders the buyer IM badge for the aliased %s channel",
    (channelType: ChannelType) => {
      wrap(
        <MessageThread
          conversation={conversationFor(channelType)}
          messages={messagesFor(channelType)}
          onToggleDrawer={vi.fn()}
        />,
        "zh",
      );
      expect(screen.getByTestId("marketplace-im-channel-badge").textContent).toContain(
        "站内私信 IM",
      );
    },
  );

  it("renders the English buyer IM badge copy", () => {
    wrap(
      <MessageThread
        conversation={conversationFor("shopee")}
        messages={messagesFor("shopee")}
        onToggleDrawer={vi.fn()}
      />,
      "en",
    );

    const badge = screen.getByTestId("marketplace-im-channel-badge");
    expect(badge.textContent).toContain("Marketplace Buyer IM");
    expect(badge.textContent).not.toContain("站内私信");
  });

  it.each(STOREFRONT_CONVERSATIONS)(
    "never renders the buyer IM badge for the %s storefront",
    (channelType: ChannelType) => {
      wrap(
        <MessageThread
          conversation={conversationFor(channelType)}
          messages={messagesFor(channelType)}
          onToggleDrawer={vi.fn()}
        />,
        "zh",
      );

      expect(screen.getByTestId("inbox-message-thread")).toBeInTheDocument();
      expect(screen.queryByTestId("marketplace-im-channel-badge")).toBeNull();
    },
  );
});

describe("marketplace buyer IM channels: composer hint", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ items: [] }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it.each(MARKETPLACE_IM_CONVERSATIONS)(
    "shows the buyer IM hint and keeps sending enabled for %s",
    (channelType: ChannelType) => {
      wrap(<InboxComposer conversation={conversationFor(channelType)} />, "zh");

      const hint = screen.getByTestId("composer-marketplace-im-hint");
      expect(hint.textContent).toContain("平台买家站内私信直连");
      expect(hint.textContent).toContain("AI 起草与实时翻译可用");
      expect(hint.getAttribute("data-marketplace-im-channel")).toBe(channelType);

      // 出站回复照常可用：输入内容后发送按钮必须从 disabled 变为可用。
      const submit = screen.getByTestId("composer-submit-btn") as HTMLButtonElement;
      expect(submit).toBeDisabled();
      fireEvent.change(screen.getByTestId("composer-textarea"), {
        target: { value: "Yes — 480 pcs are in stock, shipping today." },
      });
      expect(submit).not.toBeDisabled();

      // 切到内部备注后不再显示买家 IM 直连提示（内部备注永不外发）。
      fireEvent.click(screen.getByTestId("composer-mode-internal-note"));
      expect(screen.queryByTestId("composer-marketplace-im-hint")).toBeNull();
    },
  );

  it.each(MARKETPLACE_IM_ALIAS_CONVERSATIONS)(
    "shows the buyer IM hint for the aliased %s channel",
    (channelType: ChannelType) => {
      wrap(<InboxComposer conversation={conversationFor(channelType)} />, "zh");
      expect(screen.getByTestId("composer-marketplace-im-hint")).toBeInTheDocument();
    },
  );

  it("renders the English buyer IM hint copy", () => {
    wrap(<InboxComposer conversation={conversationFor("temu")} />, "en");
    const hint = screen.getByTestId("composer-marketplace-im-hint");
    expect(hint.textContent).toContain("Direct Marketplace Buyer IM");
    expect(hint.textContent).toContain("AI drafts & translation enabled");
  });

  it.each(STOREFRONT_CONVERSATIONS)(
    "never shows the buyer IM hint for the %s storefront",
    (channelType: ChannelType) => {
      wrap(<InboxComposer conversation={conversationFor(channelType)} />, "zh");
      expect(screen.queryByTestId("composer-marketplace-im-hint")).toBeNull();
    },
  );
});

describe("marketplace buyer IM channels: channel marketplace cards", () => {
  it("covers exactly the 12 buyer IM channels and maps to the inbox badge set", () => {
    expect(MARKETPLACE_BUYER_IM_KEYS).toHaveLength(12);
    expect(new Set(MARKETPLACE_BUYER_IM_KEYS).size).toBe(12);
    expect(MARKETPLACE_IM_CHANNEL_TYPES.size).toBe(12);

    const marketplaceKeys = CHANNEL_MARKETPLACE.map((item) => item.key);
    for (const key of MARKETPLACE_BUYER_IM_KEYS) {
      expect(marketplaceKeys).toContain(key);
      const item = marketplaceChannel(key);
      expect(item.category).toBe("commerce");
      expect(item.adminOnly).toBe(true);
      // 每张卡片恰好对应一个 `ChannelType`，且该类型必须被 Inbox 徽标识别。
      expect(item.channelTypes).toHaveLength(1);
      expect(MARKETPLACE_IM_CHANNEL_TYPES.has(item.channelTypes[0])).toBe(true);
    }
  });

  it("advertises catalog / stock / orders plus buyer IM on the 12 cards", () => {
    for (const key of MARKETPLACE_BUYER_IM_KEYS) {
      const item = marketplaceChannel(key);
      // 档位不变：电商渠道的主身份仍是店铺（订单 / 库存 / 刊登）。
      expect(item.capabilityTier).toBe("order_sync");
      expect(item.capabilityBadgeZh).toContain("买家 IM");
      expect(item.capabilityBadgeEn).toContain("Buyer IM");
      expect(item.capabilityBadgeEn).toMatch(/Catalog\/Stock\/Orders|listing/i);
      // 描述同样双语点出买家站内私信 IM。
      expect(item.descriptionZh).toContain("买家站内私信 IM");
      expect(item.descriptionEn.toLowerCase()).toContain("buyer im");
    }
  });

  it("renders the buyer IM badge text on all 12 marketplace cards", () => {
    renderMarketplaceGrid("zh");

    for (const key of MARKETPLACE_BUYER_IM_KEYS) {
      const badge = screen.getByTestId(`channel-capability-${key}`);
      expect(badge.textContent).toContain("买家 IM");
      expect(badge.getAttribute("data-capability-tier")).toBe("order_sync");
      // 诚实口径：不冒充「实时消息收发」，也不冒充满级 IM 渠道。
      expect(badge.textContent).not.toContain("实时消息收发");
    }
    expect(screen.getAllByTestId(/^channel-capability-/)).toHaveLength(22);
  });

  it("renders the English buyer IM badge text on the marketplace cards", () => {
    renderMarketplaceGrid("en");
    for (const key of MARKETPLACE_BUYER_IM_KEYS) {
      const badge = screen.getByTestId(`channel-capability-${key}`);
      expect(badge.textContent).toContain("Buyer IM");
      expect(badge.textContent.toLowerCase()).not.toContain("live messaging");
    }
  });

  it.each(STOREFRONT_MARKETPLACE_KEYS)(
    "keeps the %s storefront at pure order sync without buyer IM claims",
    (key: (typeof STOREFRONT_MARKETPLACE_KEYS)[number]) => {
      expect(MARKETPLACE_BUYER_IM_KEYS as string[]).not.toContain(key);
      const item = marketplaceChannel(key);
      expect(item.capabilityTier).toBe("order_sync");
      expect(item.capabilityBadgeZh).not.toContain("买家 IM");
      expect(item.capabilityBadgeEn).not.toContain("Buyer IM");
    },
  );
});
