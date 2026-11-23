// Phase 5 — 8 大全球电商 / 外贸平台（Shopee / Lazada / Temu / SHEIN /
// AliExpress / Ozon / Walmart / Mercado Libre）的前端契约。
//
// 三层验证，全部只依赖「锁定的共享契约」，不依赖任何内部实现细节：
//   1. 渠道应用市场：`CHANNEL_MARKETPLACE` 22 项 = 6 即时消息 + 16 订单同步，
//      8 个新渠道各自的键 / 渠道类型 / 分类 / 诚实能力档位都逐项断言；
//   2. Inbox 筛选与刊登目标：`marketplaceKeyForChannelFilter` 对 8 个新键
//      （以及对应 `ChannelType`）一一映射；`SOURCING_PUBLISHABLE_CHANNEL_TYPES`
//      扩到 16 且 `sourcingStoreLabel` 每个类型都有可读文案；
//   3. 快捷绑定抽屉：8 个新渠道提交后必须打到
//      `/api/bff/v1/channels/<slug>/connect`，请求体带 `fulfillment_mode`，
//      且成功面板绝不回显任何密钥明文。
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { ChannelConnectDrawer } from "@/components/channels/channel-connect-drawer";
import {
  CHANNEL_MARKETPLACE,
  ChannelMarketplaceGrid,
  marketplaceChannel,
  marketplaceKeyForChannelFilter,
  visibleMarketplaceItems,
  type ExtendedMarketplaceChannelKey,
} from "@/components/channels/channel-marketplace";
import { I18nProvider } from "@/lib/i18n";
import {
  SOURCING_PUBLISHABLE_CHANNEL_TYPES,
  sourcingStoreLabel,
} from "@/lib/api-client/pack-f-sourcing";

const BFF = "/api/bff";

/** 8 个新渠道的锁定契约：集市键 / 渠道类型 / 接入端点 slug / 履约模式枚举 / 品牌文案。 */
const NEW_MARKETPLACES = [
  {
    key: "shopee",
    channelType: "shopee",
    slug: "shopee",
    fulfillmentModes: ["standard", "full_managed", "light_export"],
    brand: /shopee/i,
    storeLabel: /shopee/i,
  },
  {
    key: "lazada",
    channelType: "lazada",
    slug: "lazada",
    fulfillmentModes: ["standard", "full_managed"],
    brand: /lazada/i,
    storeLabel: /lazada/i,
  },
  {
    key: "temu",
    channelType: "temu",
    slug: "temu",
    fulfillmentModes: ["standard", "semi_managed", "local"],
    brand: /temu/i,
    storeLabel: /temu/i,
  },
  {
    key: "shein",
    channelType: "shein",
    slug: "shein",
    fulfillmentModes: ["standard", "semi_managed"],
    brand: /shein/i,
    storeLabel: /shein/i,
  },
  {
    key: "aliexpress",
    channelType: "aliexpress",
    slug: "aliexpress",
    fulfillmentModes: ["standard", "managed"],
    brand: /aliexpress/i,
    storeLabel: /aliexpress/i,
  },
  {
    key: "ozon",
    channelType: "ozon",
    slug: "ozon",
    fulfillmentModes: ["fbs", "fbo"],
    brand: /ozon/i,
    storeLabel: /ozon/i,
  },
  {
    key: "walmart",
    channelType: "walmart",
    slug: "walmart",
    fulfillmentModes: ["standard", "wfs"],
    brand: /walmart/i,
    storeLabel: /walmart/i,
  },
  {
    key: "mercado_libre",
    channelType: "mercado_libre",
    slug: "mercado-libre",
    fulfillmentModes: ["standard", "full_managed"],
    brand: /mercado\s*libre/i,
    storeLabel: /mercado\s*libre/i,
  },
] as const;

type NewMarketplace = (typeof NEW_MARKETPLACES)[number];

function asChannelKey(value: string): ExtendedMarketplaceChannelKey {
  // 运行时就是锁定契约里的字符串键；类型在共享契约更新后自然收敛。
  return value as ExtendedMarketplaceChannelKey;
}

function renderGrid(locale: "zh" | "en" = "zh") {
  return render(
    <I18nProvider defaultLocale={locale}>
      <ChannelMarketplaceGrid accounts={[]} isAdmin onOpen={vi.fn()} />
    </I18nProvider>,
  );
}

interface CapturedCall {
  url: string;
  method: string | undefined;
  bodyText: string | null;
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function stubFetch(handler: (call: CapturedCall) => Response) {
  const captured: CapturedCall[] = [];
  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const call: CapturedCall =
      input instanceof Request
        ? { url: input.url, method: input.method, bodyText: await input.text() }
        : {
            url: String(input),
            method: init?.method,
            bodyText: typeof init?.body === "string" ? init.body : null,
          };
    captured.push(call);
    return handler(call);
  });
  vi.stubGlobal("fetch", mock);
  return captured;
}

const SECRET = "erp-demo-secret-9f2c";
const EXTERNAL_ACCOUNT_ID = "qzled-global-store-1";

/** 按字段语义填一个「一定合法」的值，避免把测试绑死到某个渠道的字段清单。 */
function plausibleValue(testId: string, type: string): string {
  const id = testId.toLowerCase();
  if (type === "email" || id.includes("email")) return "sales@qzled.example.com";
  if (id.includes("domain") || id.includes("url")) return "https://qzled.example.com";
  if (id.includes("display") || id.includes("name")) return "QZ LED Global";
  if (id.includes("region")) return "SG";
  if (id.includes("site")) return "MLM";
  if (id.includes("secret") || id.includes("key") || id.includes("token") || type === "password") {
    return SECRET;
  }
  return "1234567890";
}

/**
 * 抽屉表单的通用填充：所有文本框填合法值、空 select 取第一个选项、
 * 若履约模式是切换按钮则点亮第一个。返回填过的 (testid, value) 便于断言零回显。
 */
function fillDrawerForm(form: HTMLElement): Array<{ testId: string; value: string }> {
  const filled: Array<{ testId: string; value: string }> = [];

  for (const input of Array.from(form.querySelectorAll<HTMLInputElement>("input"))) {
    if (input.type === "checkbox" || input.type === "radio" || input.disabled) continue;
    const testId = input.dataset.testid ?? input.name ?? "input";
    const value = plausibleValue(testId, input.type);
    fireEvent.change(input, { target: { value } });
    filled.push({ testId, value });
  }

  for (const select of Array.from(form.querySelectorAll<HTMLSelectElement>("select"))) {
    if (select.disabled) continue;
    const testId = select.dataset.testid ?? "select";
    const firstOption = Array.from(select.options).find((option) => option.value !== "");
    if (!select.value && firstOption) {
      fireEvent.change(select, { target: { value: firstOption.value } });
      filled.push({ testId, value: firstOption.value });
    }
  }

  const fulfillmentToggle = Array.from(
    form.querySelectorAll<HTMLButtonElement>("button"),
  ).find(
    (button) =>
      /fulfillment/i.test(button.dataset.testid ?? "") &&
      button.getAttribute("aria-pressed") !== "true",
  );
  if (fulfillmentToggle) fireEvent.click(fulfillmentToggle);

  return filled;
}

function renderDrawer(key: string) {
  return render(
    <I18nProvider defaultLocale="en">
      <ChannelConnectDrawer
        channelKey={asChannelKey(key)}
        accounts={[]}
        isAdmin
        currentUserId="user-owner-1"
        members={[]}
        tenantSlug="qz-led-factory"
        onSubmit={vi.fn()}
        onClose={vi.fn()}
        onViewAccounts={vi.fn()}
      />
    </I18nProvider>,
  );
}

// ---------------------------------------------------------------------------
// 1. 渠道应用市场：22 项 = 6 即时消息 + 16 订单同步
// ---------------------------------------------------------------------------

describe("phase5 global marketplaces in the channel marketplace", () => {
  it("grows the marketplace to 22 channels with 16 order-sync marketplaces", () => {
    expect(CHANNEL_MARKETPLACE).toHaveLength(22);
    expect(visibleMarketplaceItems(true)).toHaveLength(22);
    expect(new Set(CHANNEL_MARKETPLACE.map((item) => item.key)).size).toBe(22);

    const tiers = CHANNEL_MARKETPLACE.map((item) => item.capabilityTier);
    expect(tiers.filter((tier) => tier === "live_messaging")).toHaveLength(6);
    expect(tiers.filter((tier) => tier === "order_sync")).toHaveLength(16);
    expect(tiers.filter((tier) => tier === "credentials_only")).toHaveLength(0);

    const byCategory = (category: string) =>
      CHANNEL_MARKETPLACE.filter((item) => item.category === category).length;
    expect(byCategory("messaging")).toBe(4);
    expect(byCategory("email_web")).toBe(2);
    expect(byCategory("commerce")).toBe(16);
  });

  it("keeps every pre-existing channel untouched (no regression on the old 14)", () => {
    const legacyKeys = [
      "whatsapp",
      "telegram",
      "messenger",
      "instagram_dm",
      "email",
      "web_livechat",
      "shopify",
      "woocommerce",
      "shopline",
      "shoplazza",
      "tiktok_shop",
      "alibaba_intl",
      "amazon",
      "ebay",
    ];
    const keys = CHANNEL_MARKETPLACE.map((item) => item.key);
    for (const key of legacyKeys) expect(keys).toContain(key);
    // 8 个新渠道排在旧 14 个之后，顺序即产品优先级。
    expect(keys.slice(0, 14)).toEqual(legacyKeys);
  });

  it.each(NEW_MARKETPLACES)(
    "registers $key as a commerce order-sync marketplace card",
    (marketplace: NewMarketplace) => {
      const item = marketplaceChannel(asChannelKey(marketplace.key));
      expect(item).toBeDefined();
      expect(item.key).toBe(marketplace.key);
      expect(item.category).toBe("commerce");
      expect(item.adminOnly).toBe(true);
      expect(item.channelTypes).toEqual([marketplace.channelType]);
      expect(item.capabilityTier).toBe("order_sync");
      expect(item.titleZh).toMatch(marketplace.brand);
      expect(item.titleEn).toMatch(marketplace.brand);
      expect(item.descriptionZh.length).toBeGreaterThan(8);
      expect(item.descriptionEn.length).toBeGreaterThan(8);
      expect(item.capabilityBadgeZh.length).toBeGreaterThan(0);
      expect(item.capabilityBadgeEn.length).toBeGreaterThan(0);
      // 诚实口径：订单同步渠道不得声称「实时消息收发」。
      expect(item.capabilityTier).not.toBe("live_messaging");
      expect(item.capabilityBadgeEn.toLowerCase()).not.toContain("live messaging");
    },
  );

  it("renders 22 cards plus the custom-channel guide card, with one badge each", () => {
    renderGrid("zh");

    expect(screen.getAllByTestId(/^marketplace-card-/)).toHaveLength(22);
    expect(screen.getByTestId("marketplace-custom-channel-card")).toBeInTheDocument();

    const badges = screen.getAllByTestId(/^channel-capability-/);
    expect(badges).toHaveLength(22);
    const orderSyncBadges = badges.filter(
      (badge) => badge.getAttribute("data-capability-tier") === "order_sync",
    );
    expect(orderSyncBadges).toHaveLength(16);

    for (const marketplace of NEW_MARKETPLACES) {
      expect(screen.getByTestId(`marketplace-card-${marketplace.key}`)).toBeInTheDocument();
      expect(screen.getByTestId(`marketplace-status-${marketplace.key}`)).toBeInTheDocument();
      expect(screen.getByTestId(`open-connect-${marketplace.key}`)).toBeInTheDocument();
      const badge = screen.getByTestId(`channel-capability-${marketplace.key}`);
      expect(badge).toHaveAttribute("data-capability-tier", "order_sync");
      expect(badge.textContent ?? "").not.toContain("实时消息收发");
    }

    // 分类计数：独立站与电商 = 16。
    expect(screen.getByTestId("marketplace-category-commerce").textContent).toContain("16");
    expect(screen.getByTestId("marketplace-category-messaging").textContent).toContain("4");
  });

  it("keeps the 8 new marketplaces admin-only (agents never see company stores)", () => {
    expect(visibleMarketplaceItems(false)).toHaveLength(3);
    for (const marketplace of NEW_MARKETPLACES) {
      expect(visibleMarketplaceItems(false).map((item) => item.key)).not.toContain(
        marketplace.key,
      );
    }

    render(
      <I18nProvider defaultLocale="zh">
        <ChannelMarketplaceGrid accounts={[]} isAdmin={false} onOpen={vi.fn()} />
      </I18nProvider>,
    );
    for (const marketplace of NEW_MARKETPLACES) {
      expect(screen.queryByTestId(`marketplace-card-${marketplace.key}`)).toBeNull();
    }
    // 业务员仍然只看得到 3 个个人可绑定渠道。
    expect(screen.getAllByTestId(/^marketplace-card-/)).toHaveLength(3);
  });
});

// ---------------------------------------------------------------------------
// 2. Inbox 筛选 → 抽屉键 & 多平台刊登目标
// ---------------------------------------------------------------------------

describe("phase5 inbox filters and publish targets", () => {
  it.each(NEW_MARKETPLACES)(
    "maps the $key channel filter to its own marketplace drawer",
    (marketplace: NewMarketplace) => {
      // 集市键本身与对应 ChannelType 都要映射到同一个抽屉。
      expect(marketplaceKeyForChannelFilter(marketplace.key)).toBe(marketplace.key);
      expect(marketplaceKeyForChannelFilter(marketplace.channelType)).toBe(marketplace.key);
    },
  );

  it("keeps the legacy filter mappings intact", () => {
    expect(marketplaceKeyForChannelFilter("whatsapp_cloud")).toBe("whatsapp");
    expect(marketplaceKeyForChannelFilter("facebook_messenger")).toBe("messenger");
    expect(marketplaceKeyForChannelFilter("alibaba_email")).toBe("alibaba_intl");
    expect(marketplaceKeyForChannelFilter("amazon_sp")).toBe("amazon");
    expect(marketplaceKeyForChannelFilter("not_a_channel")).toBeNull();
  });

  it("publishes to 16 storefront types with a readable label each", () => {
    expect(SOURCING_PUBLISHABLE_CHANNEL_TYPES).toHaveLength(16);
    expect(new Set(SOURCING_PUBLISHABLE_CHANNEL_TYPES).size).toBe(16);

    const labels = SOURCING_PUBLISHABLE_CHANNEL_TYPES.map((channelType) =>
      sourcingStoreLabel(channelType),
    );
    for (const label of labels) {
      expect(label.length).toBeGreaterThan(2);
      // 不再退化成 `MERCADO LIBRE` 式的大写下划线原值。
      expect(label).not.toContain("_");
    }
    expect(new Set(labels).size).toBe(16);
  });

  it.each(NEW_MARKETPLACES)(
    "exposes a publishable storefront label for $channelType",
    (marketplace: NewMarketplace) => {
      expect(
        (SOURCING_PUBLISHABLE_CHANNEL_TYPES as readonly string[]).includes(
          marketplace.channelType,
        ),
      ).toBe(true);
      const label = sourcingStoreLabel(marketplace.channelType);
      expect(label).toMatch(marketplace.storeLabel);
      // 品牌本来就全大写（SHEIN / eBay）时也必须是人工维护的文案，而非下划线回退。
      expect(label).not.toContain("_");
    },
  );
});

// ---------------------------------------------------------------------------
// 3. 快捷绑定抽屉：8 个新渠道各自打到自己端点，带 fulfillment_mode、零密钥回显
// ---------------------------------------------------------------------------

describe("phase5 global marketplace connect drawer", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it.each(NEW_MARKETPLACES)(
    "submits $key to /api/bff/v1/channels/$slug/connect with fulfillment_mode",
    async (marketplace: NewMarketplace) => {
      const captured = stubFetch(() =>
        jsonResponse(
          {
            channel_account_id: `acc-${marketplace.channelType}-1`,
            channel_type: marketplace.channelType,
            credential_type: `${marketplace.channelType}_app`,
            display_name: "QZ LED Global",
            external_account_id: EXTERNAL_ACCOUNT_ID,
            status: "active",
            visibility_scope: "shared",
          },
          201,
        ),
      );

      renderDrawer(marketplace.key);

      expect(screen.getByTestId(`channel-connect-drawer-${marketplace.key}`)).toBeInTheDocument();
      const form = screen.getByTestId(`connect-form-${marketplace.key}`);
      const submit = screen.getByTestId(
        `connect-${marketplace.key}-btn`,
      ) as HTMLButtonElement;
      expect(submit).toBeDisabled();

      // 履约模式下拉必须是后端 Literal 的完整枚举，不能自行发明档位。
      const fulfillmentSelect =
        (screen.queryByTestId(
          `${marketplace.key}-fulfillment-mode-select`,
        ) as HTMLSelectElement | null) ?? (form.querySelector("select") as HTMLSelectElement | null);
      expect(fulfillmentSelect).not.toBeNull();
      if (!fulfillmentSelect) {
        throw new Error(`${marketplace.key} drawer is missing a fulfillment-mode select`);
      }
      const offeredModes = Array.from(fulfillmentSelect.options).map((option) => option.value);
      expect([...offeredModes].sort()).toEqual([...marketplace.fulfillmentModes].sort());
      expect(marketplace.fulfillmentModes).toContain(fulfillmentSelect.value);

      const filled = fillDrawerForm(form);
      await waitFor(() => expect(submit).toBeEnabled());
      fireEvent.click(submit);

      await waitFor(() => expect(captured).toHaveLength(1));
      const call = captured[0];
      expect(call.url).toBe(`${BFF}/v1/channels/${marketplace.slug}/connect`);
      expect(call.method).toBe("POST");

      const body = JSON.parse(call.bodyText ?? "{}") as Record<string, unknown>;
      // 请求体绝不夹带集市键 / 端点 slug 之类的前端内部字段。
      expect(Object.keys(body)).not.toContain("channel");
      expect(Object.keys(body)).not.toContain("channel_key");
      expect(typeof body.display_name).toBe("string");
      expect((body.display_name as string).length).toBeGreaterThan(0);
      expect(marketplace.fulfillmentModes).toContain(body.fulfillment_mode);

      // 密钥必须原样提交给后端（由后端信封加密），前端不得先自行脱敏或改写。
      expect(call.bodyText ?? "").toContain(SECRET);

      const success = await screen.findByTestId(`connect-success-${marketplace.key}`);
      expect(success.textContent ?? "").toContain(EXTERNAL_ACCOUNT_ID);

      // 零密钥回显：成功面板与整页可见文本都不得出现填过的任何密钥明文。
      expect(filled.some((field) => field.value === SECRET)).toBe(true);
      expect(success.textContent ?? "").not.toContain(SECRET);
      expect(document.body.textContent ?? "").not.toContain(SECRET);
      expect(document.querySelector('input[type="password"]')).not.toBeNull();
    },
  );
});
