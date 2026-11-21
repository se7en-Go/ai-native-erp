import React from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import { I18nProvider } from "@/lib/i18n";
import {
  CHANNEL_MARKETPLACE,
  ChannelMarketplaceGrid,
  type ChannelCapabilityTier,
} from "@/components/channels/channel-marketplace";
import { sourcingStatusCopy } from "@/lib/api-client/pack-f-sourcing";
import {
  postDeliveryLabel,
  publishModeHonestNote,
  publishModeLabel,
} from "@/components/social/presentation";

// ---------------------------------------------------------------------------
// Wave 5 / W5-C — 前端诚实态（能力徽标 / 刊登状态 / 送达口径）
// ---------------------------------------------------------------------------

function renderMarketplace(locale: "zh" | "en" = "zh", isAdmin = true) {
  return render(
    <I18nProvider defaultLocale={locale}>
      <ChannelMarketplaceGrid accounts={[]} isAdmin={isAdmin} onOpen={vi.fn()} />
    </I18nProvider>,
  );
}

const EXPECTED_TIER_BY_KEY: Record<string, ChannelCapabilityTier> = {
  whatsapp: "live_messaging",
  telegram: "live_messaging",
  messenger: "live_messaging",
  instagram_dm: "live_messaging",
  email: "live_messaging",
  web_livechat: "live_messaging",
  shopify: "order_sync",
  woocommerce: "order_sync",
  shopline: "order_sync",
  shoplazza: "order_sync",
  tiktok_shop: "order_sync",
  alibaba_intl: "order_sync",
  amazon: "order_sync",
  ebay: "order_sync",
};

describe("wave5 honest channel capability badges", () => {
  it("assigns an accurate capability tier to all 14 channels", () => {
    expect(CHANNEL_MARKETPLACE).toHaveLength(14);
    expect(
      Object.fromEntries(CHANNEL_MARKETPLACE.map((item) => [item.key, item.capabilityTier])),
    ).toEqual(EXPECTED_TIER_BY_KEY);

    const tiers = CHANNEL_MARKETPLACE.map((item) => item.capabilityTier);
    expect(tiers.filter((tier) => tier === "live_messaging")).toHaveLength(6);
    expect(tiers.filter((tier) => tier === "order_sync")).toHaveLength(8);
    expect(tiers.filter((tier) => tier === "credentials_only")).toHaveLength(0);

    for (const item of CHANNEL_MARKETPLACE) {
      expect(item.capabilityBadgeZh.length).toBeGreaterThan(0);
      expect(item.capabilityBadgeEn.length).toBeGreaterThan(0);
    }
  });

  it("renders one honest capability pill per card with the exact bilingual copy", () => {
    renderMarketplace("zh", true);

    for (const item of CHANNEL_MARKETPLACE) {
      const badge = screen.getByTestId(`channel-capability-${item.key}`);
      expect(badge).toBeInTheDocument();
      expect(badge.textContent).toContain(item.capabilityBadgeZh);
      expect(badge).toHaveAttribute("data-capability-tier", item.capabilityTier);
    }

    expect(screen.getAllByTestId(/^channel-capability-/)).toHaveLength(14);
    expect(screen.getByTestId("channel-capability-whatsapp").textContent).toContain(
      "实时消息收发",
    );
    expect(screen.getByTestId("channel-capability-shopify").textContent).toContain(
      "订单与库存同步",
    );
    expect(screen.getByTestId("channel-capability-amazon").textContent).toContain(
      "SP-API 实时刊登与拉单",
    );
    expect(screen.getByTestId("channel-capability-alibaba_intl").textContent).toContain(
      "实时刊登 · 订单与询盘",
    );
    expect(screen.getByTestId("channel-capability-ebay").textContent).toContain(
      "Sell API 实时刊登与拉单",
    );
  });

  it("renders the English capability badges without overclaiming", () => {
    renderMarketplace("en", true);

    expect(screen.getByTestId("channel-capability-telegram").textContent).toContain(
      "Live messaging",
    );
    expect(screen.getByTestId("channel-capability-woocommerce").textContent).toContain(
      "Order & stock sync",
    );
    expect(screen.getByTestId("channel-capability-ebay").textContent).toContain(
      "Sell API listing & order sync",
    );
    expect(screen.getByTestId("channel-capability-alibaba_intl").textContent).toContain(
      "Live listing · Orders & inquiries",
    );
    // 不再出现「实时消息」式的过度承诺。
    expect(screen.getByTestId("channel-capability-amazon").textContent).not.toContain(
      "Live messaging",
    );
    expect(screen.getByTestId("channel-capability-amazon").textContent).toContain(
      "SP-API listing & order sync",
    );
  });

  it("keeps every existing card testid and interaction intact", () => {
    renderMarketplace("zh", true);

    expect(screen.getByTestId("channel-marketplace")).toBeInTheDocument();
    expect(screen.getByTestId("marketplace-grid")).toBeInTheDocument();
    expect(screen.getAllByTestId(/^marketplace-card-/)).toHaveLength(14);
    for (const item of CHANNEL_MARKETPLACE) {
      expect(screen.getByTestId(`marketplace-status-${item.key}`)).toBeInTheDocument();
      expect(screen.getByTestId(`open-connect-${item.key}`)).toBeInTheDocument();
    }
    // 引导卡仍然只在「全部 + 无搜索词」时出现。
    expect(screen.getByTestId("marketplace-custom-channel-card")).toBeInTheDocument();
  });

  it("keeps the 3 foreign-trade channels honest about their live API listing push", () => {
    const amazon = CHANNEL_MARKETPLACE.find((item) => item.key === "amazon")!;
    const ebay = CHANNEL_MARKETPLACE.find((item) => item.key === "ebay")!;
    const alibaba = CHANNEL_MARKETPLACE.find((item) => item.key === "alibaba_intl")!;

    // 三个渠道都声明「实时刊登」+「订单回拉」，并明确凭证是信封加密托管。
    for (const item of [amazon, ebay, alibaba]) {
      expect(item.capabilityTier).toBe("order_sync");
      expect(item.descriptionZh).toContain("加密");
      expect(item.capabilityBadgeZh).toContain("实时刊登");
      expect(item.capabilityBadgeEn.toLowerCase()).toContain("listing");
    }
    expect(amazon.descriptionZh).toContain("SP-API");
    expect(ebay.descriptionZh).toContain("Sell API");
    expect(alibaba.descriptionZh).toContain("IOP");
    expect(alibaba.descriptionZh).toContain("询盘");
    // 旧口径（不推送商品上架 / 不做商品刊登推送）已不成立，必须已移除。
    for (const item of [amazon, ebay, alibaba]) {
      expect(item.descriptionZh).not.toContain("不推送商品上架");
      expect(item.descriptionZh).not.toContain("不做商品刊登推送");
    }
  });
});

describe("wave5 honest sourcing status copy", () => {
  it("labels local SKU mapping as mapping ready, not live published", () => {
    const mapping = sourcingStatusCopy("mapping_ready");
    expect(mapping.zh).toBe("映射就绪（待平台确认）");
    expect(mapping.en).toBe("Mapping ready");
    expect(mapping.zh).not.toBe("已上线刊登");
    expect(mapping.tone).toContain("amber");

    const live = sourcingStatusCopy("live_published");
    expect(live.zh).toBe("已上线刊登");
    expect(live.en).toBe("Live published");
    expect(live.tone).toContain("emerald");

    // 区分「本地映射」与「平台已上线」：两者文案与色调都不同。
    expect(mapping.zh).not.toBe(live.zh);
    expect(mapping.tone).not.toBe(live.tone);
  });

  it("keeps the existing 已刊登 substring for locally recorded publishes", () => {
    // 既有断言（pack-f-sourcing-channels-ui.test.tsx）使用 toContain("已刊登")，
    // 所以「本地记录」必须保留该子串，同时显式标注未获平台确认。
    const published = sourcingStatusCopy("published");
    expect(published.zh).toContain("已刊登");
    expect(published.zh).toContain("本地记录");
    expect(published.en).toBe("Published (local)");
  });

  it("keeps imported / draft / unknown statuses unchanged", () => {
    expect(sourcingStatusCopy("imported").zh).toBe("已入主档");
    expect(sourcingStatusCopy("imported").en).toBe("Imported");
    expect(sourcingStatusCopy("draft").zh).toBe("草稿");
    expect(sourcingStatusCopy("draft").en).toBe("Draft");
    expect(sourcingStatusCopy("something_else").zh).toBe("草稿");
    expect(sourcingStatusCopy("something_else").en).toBe("Draft");
  });
});

describe("wave5 honest social delivery & publish mode copy", () => {
  it("distinguishes external confirmation, manual confirmation and local recording", () => {
    const external = postDeliveryLabel({
      status: "published",
      delivery_mode: "external_api",
      external_publish_confirmed: true,
      external_post_id: "ig-123",
    });
    expect(external.state).toBe("external_confirmed");
    expect(external.zh).toBe("外部平台已确认");
    expect(external.en).toBe("Confirmed externally");

    // 只凭 confirmed 标记（没有 delivery_mode）也算外部确认。
    expect(
      postDeliveryLabel({ status: "published", external_publish_confirmed: true }).state,
    ).toBe("external_confirmed");

    const manual = postDeliveryLabel({
      status: "published",
      delivery_mode: "manual_confirmed",
    });
    expect(manual.state).toBe("manual_confirmed");
    expect(manual.zh).toBe("官方后台手动确认");
    expect(manual.en).toBe("Manually confirmed");

    const local = postDeliveryLabel({ status: "published", delivery_mode: "recorded_local" });
    expect(local.state).toBe("recorded_local");
    expect(local.zh).toBe("本地已记录（未外发）");
    expect(local.en).toBe("Recorded locally");

    // 缺省（后端尚未回传任何送达字段）绝不当成已外发。
    const unknown = postDeliveryLabel({ status: "published" });
    expect(unknown.state).toBe("recorded_local");
    expect(unknown.zh).not.toContain("已确认");
  });

  it("keeps a distinct tone per delivery state", () => {
    const tones = [
      postDeliveryLabel({ status: "published", delivery_mode: "external_api" }).tone,
      postDeliveryLabel({ status: "published", delivery_mode: "manual_confirmed" }).tone,
      postDeliveryLabel({ status: "published" }).tone,
    ];
    expect(new Set(tones).size).toBe(3);
  });

  it("keeps the existing publish mode labels and adds an honest note", () => {
    // 既有断言（social-components.test.tsx）硬编码「API 自动发布」，不得改动。
    expect(publishModeLabel.api).toBe("API 自动发布");
    expect(publishModeLabel.manual_export).toBe("手动导出");
    expect(publishModeHonestNote.api).toContain("平台回执");
    expect(publishModeHonestNote.manual_export).toContain("官方后台");
  });
});
