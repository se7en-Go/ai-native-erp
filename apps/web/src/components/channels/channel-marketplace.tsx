"use client";

import React, { useMemo, useState } from "react";
import {
  CheckCircle2,
  Globe,
  Instagram,
  Mail,
  MessageCircle,
  MessageSquare,
  Music2,
  Plus,
  Search,
  Send,
  ShoppingBag,
  Store,
} from "lucide-react";

import { useI18n } from "@/lib/i18n";
import type {
  ChannelAccountApiItem,
  MarketplaceChannelKey,
} from "@/lib/api-client/live-client";
import type { ChannelType } from "@/lib/contracts";

export type MarketplaceCategory = "all" | "messaging" | "email_web" | "commerce";

/** 官网客服小组件脚本地址（与 apps/widget 构建产物一致）。 */
export const WIDGET_SCRIPT_SRC = "http://localhost:3001/widget.iife.js";

/** 生成可直接贴进网站 </body> 前的嵌入代码。 */
export function livechatEmbedSnippet(
  siteKey: string,
  tenantSlug?: string | null,
): string {
  return `<script src="${WIDGET_SCRIPT_SRC}" data-site-key="${siteKey}" data-tenant-slug="${
    tenantSlug || "demo-tenant"
  }"></script>`;
}

export type ChannelMarketplaceItem = {
  key: MarketplaceChannelKey;
  titleZh: string;
  titleEn: string;
  descriptionZh: string;
  descriptionEn: string;
  category: Exclude<MarketplaceCategory, "all">;
  icon: React.ComponentType<{ className?: string }>;
  /** 40x40 品牌着色图标底，一眼区分 11 个渠道。 */
  tile: string;
  accent: string;
  /** 计入「已接入 N 个」的账号类型（WhatsApp 含 Cloud + Coexistence）。 */
  channelTypes: ChannelType[];
  /** owner/admin 专属（公司资产），业务员不可见。 */
  adminOnly: boolean;
};

/**
 * 渠道应用市场清单：11 个渠道 = 即时通讯 4 + 邮件与官网 2 + 独立站与电商 5。
 * 顺序即产品优先级，与 Inbox 渠道筛选一一对应。
 */
export const CHANNEL_MARKETPLACE: ChannelMarketplaceItem[] = [
  {
    key: "whatsapp",
    titleZh: "WhatsApp Business",
    titleEn: "WhatsApp Business",
    descriptionZh: "粘贴 WABA 与手机号令牌一键接入，支持个人号与公司公海。",
    descriptionEn: "Paste your WABA and phone-number token to connect in one step.",
    category: "messaging",
    icon: MessageSquare,
    tile: "bg-emerald-50 text-emerald-700 ring-emerald-100",
    accent: "bg-emerald-500",
    channelTypes: ["whatsapp_cloud", "whatsapp_coexistence"],
    adminOnly: false,
  },
  {
    key: "telegram",
    titleZh: "Telegram 商业版",
    titleEn: "Telegram Business",
    descriptionZh: "粘贴 BotFather Token 一键接入，支持个人号与公司公海。",
    descriptionEn: "Paste a BotFather token to connect, personal or company-shared.",
    category: "messaging",
    icon: Send,
    tile: "bg-sky-50 text-sky-600 ring-sky-100",
    accent: "bg-sky-500",
    channelTypes: ["telegram"],
    adminOnly: false,
  },
  {
    key: "messenger",
    titleZh: "Facebook Messenger",
    titleEn: "Facebook Messenger",
    descriptionZh: "Meta 公共主页收件箱，fb_psid 身份与原始报文验签。",
    descriptionEn: "Meta Page inbox with fb_psid identity and signed raw bodies.",
    category: "messaging",
    icon: MessageCircle,
    tile: "bg-blue-50 text-blue-600 ring-blue-100",
    accent: "bg-blue-500",
    channelTypes: ["messenger"],
    adminOnly: true,
  },
  {
    key: "instagram_dm",
    titleZh: "Instagram 私信",
    titleEn: "Instagram DM",
    descriptionZh: "快拍提及与私信统一进 Inbox，ig_psid 身份识别买家。",
    descriptionEn: "Story mentions and DMs land in one inbox with ig_psid identity.",
    category: "messaging",
    icon: Instagram,
    tile: "bg-rose-50 text-rose-600 ring-rose-100",
    accent: "bg-rose-500",
    channelTypes: ["instagram_dm"],
    adminOnly: true,
  },
  {
    key: "email",
    titleZh: "工作邮箱（邮件）",
    titleEn: "Work email",
    descriptionZh: "Resend API 或 SMTP/IMAP 30 秒绑定，支持询盘邮箱转发。",
    descriptionEn: "Bind Resend or SMTP/IMAP in 30 seconds, inquiries forwarded.",
    category: "email_web",
    icon: Mail,
    tile: "bg-emerald-50 text-emerald-700 ring-emerald-100",
    accent: "bg-emerald-500",
    channelTypes: ["email", "alibaba_email", "made_in_china_email"],
    adminOnly: false,
  },
  {
    key: "web_livechat",
    titleZh: "官网网页客服",
    titleEn: "Website live chat",
    descriptionZh: "贴一个域名即生成嵌入脚本，访客消息实时进 Inbox。",
    descriptionEn: "One domain yields an embed script; visitor chats stream into Inbox.",
    category: "email_web",
    icon: Globe,
    tile: "bg-violet-50 text-violet-600 ring-violet-100",
    accent: "bg-violet-500",
    channelTypes: ["web_livechat"],
    adminOnly: true,
  },
  {
    key: "shopify",
    titleZh: "Shopify",
    titleEn: "Shopify",
    descriptionZh: "自定义应用授权，订单、库存与买家会话双向同步。",
    descriptionEn: "Custom-app auth keeps orders, stock and buyer chats in sync.",
    category: "commerce",
    icon: ShoppingBag,
    tile: "bg-lime-50 text-lime-700 ring-lime-100",
    accent: "bg-lime-600",
    channelTypes: ["shopify"],
    adminOnly: true,
  },
  {
    key: "woocommerce",
    titleZh: "WooCommerce",
    titleEn: "WooCommerce",
    descriptionZh: "Consumer Key/Secret 直连，WordPress 站点商品订单一键同步。",
    descriptionEn: "Consumer key/secret direct connect for WordPress catalogs and orders.",
    category: "commerce",
    icon: Store,
    tile: "bg-purple-50 text-purple-600 ring-purple-100",
    accent: "bg-purple-500",
    channelTypes: ["woocommerce"],
    adminOnly: true,
  },
  {
    key: "shopline",
    titleZh: "SHOPLINE",
    titleEn: "SHOPLINE",
    descriptionZh: "开放平台 Token 接入，东南亚站点多店铺统一管理。",
    descriptionEn: "Open-platform token for multi-store SEA operations.",
    category: "commerce",
    icon: Store,
    tile: "bg-sky-50 text-sky-600 ring-sky-100",
    accent: "bg-sky-500",
    channelTypes: ["shopline"],
    adminOnly: true,
  },
  {
    key: "shoplazza",
    titleZh: "Shoplazza",
    titleEn: "Shoplazza",
    descriptionZh: "店匠开放平台接入，独立站订单与买家消息同源。",
    descriptionEn: "Shoplazza open-platform connect with unified order and chat data.",
    category: "commerce",
    icon: Store,
    tile: "bg-indigo-50 text-indigo-600 ring-indigo-100",
    accent: "bg-indigo-500",
    channelTypes: ["shoplazza"],
    adminOnly: true,
  },
  {
    key: "tiktok_shop",
    titleZh: "TikTok Shop",
    titleEn: "TikTok Shop",
    descriptionZh: "卖家自备应用（BYOK），店铺 ID + Cipher 一键授权。",
    descriptionEn: "Seller BYOK with shop ID and cipher in one authorization.",
    category: "commerce",
    icon: Music2,
    tile: "bg-zinc-900/5 text-zinc-900 ring-zinc-200",
    accent: "bg-zinc-800",
    channelTypes: ["tiktok_shop"],
    adminOnly: true,
  },
];

/** 业务员个人可绑定的 3 个渠道：Telegram、个人工作邮箱、WhatsApp 共存号。 */
export const AGENT_MARKETPLACE_KEYS: MarketplaceChannelKey[] = [
  "telegram",
  "email",
  "whatsapp",
];

const MARKETPLACE_BY_KEY = new Map<MarketplaceChannelKey, ChannelMarketplaceItem>(
  CHANNEL_MARKETPLACE.map((item) => [item.key, item]),
);

/** URL `?connect=` 的白名单校验，避免非法参数打开空抽屉。 */
export function isMarketplaceChannelKey(value: string | null): value is MarketplaceChannelKey {
  if (!value) return false;
  return MARKETPLACE_BY_KEY.has(value as MarketplaceChannelKey);
}

export function marketplaceChannel(key: MarketplaceChannelKey): ChannelMarketplaceItem {
  return MARKETPLACE_BY_KEY.get(key)!;
}

/**
 * Inbox 渠道筛选 → 市场抽屉键。WhatsApp Cloud/Coexistence 合并到 `whatsapp`，
 * Messenger 的筛选键是 `facebook_messenger`。
 */
export function marketplaceKeyForChannelFilter(filter: string): MarketplaceChannelKey | null {
  switch (filter) {
    case "whatsapp":
    case "whatsapp_cloud":
    case "whatsapp_coexistence":
      return "whatsapp";
    case "telegram":
      return "telegram";
    case "messenger":
    case "facebook_messenger":
      return "messenger";
    case "instagram_dm":
      return "instagram_dm";
    case "email":
    case "alibaba_email":
    case "made_in_china_email":
      return "email";
    case "web_livechat":
      return "web_livechat";
    case "shopify":
      return "shopify";
    case "woocommerce":
      return "woocommerce";
    case "shopline":
      return "shopline";
    case "shoplazza":
      return "shoplazza";
    case "tiktok_shop":
      return "tiktok_shop";
    default:
      return null;
  }
}

/** 某个市场渠道当前已接入的账号数（WhatsApp 同时统计 Cloud 与 Coexistence）。 */
export function connectedAccountCount(
  item: ChannelMarketplaceItem,
  accounts: ChannelAccountApiItem[],
): number {
  return accounts.filter((account) =>
    item.channelTypes.includes(account.channel_type),
  ).length;
}

export function visibleMarketplaceItems(isAdmin: boolean): ChannelMarketplaceItem[] {
  return isAdmin
    ? CHANNEL_MARKETPLACE
    : AGENT_MARKETPLACE_KEYS.map((key) => marketplaceChannel(key));
}

const CATEGORY_COPY: Record<
  MarketplaceCategory,
  { zh: string; en: string }
> = {
  all: { zh: "全部", en: "All" },
  messaging: { zh: "即时通讯", en: "Messaging" },
  email_web: { zh: "邮件与官网", en: "Email & web" },
  commerce: { zh: "独立站与电商", en: "Storefronts" },
};

export type ChannelMarketplaceGridProps = {
  accounts: ChannelAccountApiItem[];
  isAdmin: boolean;
  onOpen: (key: MarketplaceChannelKey) => void;
};

/**
 * 渠道应用市场：干净的 3 列卡片墙，卡片上不放任何内联表单——
 * 点击「快捷绑定」才打开右侧 480px 抽屉。
 */
export function ChannelMarketplaceGrid({
  accounts,
  isAdmin,
  onOpen,
}: ChannelMarketplaceGridProps) {
  const { bi } = useI18n();
  const [category, setCategory] = useState<MarketplaceCategory>("all");
  const [query, setQuery] = useState("");

  const visibleItems = useMemo(() => visibleMarketplaceItems(isAdmin), [isAdmin]);

  const categoryOptions = useMemo(() => {
    const counts: Record<MarketplaceCategory, number> = {
      all: visibleItems.length,
      messaging: 0,
      email_web: 0,
      commerce: 0,
    };
    for (const item of visibleItems) counts[item.category] += 1;
    return (["all", "messaging", "email_web", "commerce"] as MarketplaceCategory[]).map(
      (value) => ({ value, count: counts[value] }),
    );
  }, [visibleItems]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return visibleItems.filter((item) => {
      if (category !== "all" && item.category !== category) return false;
      if (!needle) return true;
      return (
        item.titleZh.toLowerCase().includes(needle) ||
        item.titleEn.toLowerCase().includes(needle) ||
        item.descriptionZh.toLowerCase().includes(needle) ||
        item.descriptionEn.toLowerCase().includes(needle)
      );
    });
  }, [category, query, visibleItems]);

  return (
    <section
      data-testid="channel-marketplace"
      aria-label={bi("渠道应用市场", "Channel marketplace")}
      className="space-y-3"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        {isAdmin && (
        <div className="flex flex-wrap items-center gap-1 rounded-xl border border-[#E5E2DC] bg-[#EFECE6]/90 p-1">
          {categoryOptions.map((option) => {
            const active = category === option.value;
            return (
              <button
                key={option.value}
                type="button"
                aria-pressed={active}
                data-testid={`marketplace-category-${option.value}`}
                onClick={() => setCategory(option.value)}
                className={`inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-[12px] font-semibold transition-colors ${
                  active
                    ? "bg-[#18181B] text-[#FAFAF9] shadow-xs"
                    : "text-stone-600 hover:bg-white/80 hover:text-zinc-950"
                }`}
              >
                <span>{bi(CATEGORY_COPY[option.value].zh, CATEGORY_COPY[option.value].en)}</span>
                <span
                  className={`rounded-full px-1.5 text-[10.5px] font-semibold tabular-nums ${
                    active ? "bg-white/20 text-white" : "bg-stone-200/80 text-stone-700"
                  }`}
                >
                  {option.count}
                </span>
              </button>
            );
          })}
        </div>
        )}

        <label className="relative block w-full sm:w-64">
          <Search className="pointer-events-none absolute top-1/2 left-2.5 h-3.5 w-3.5 -translate-y-1/2 text-stone-400" />
          <input
            type="search"
            value={query}
            data-testid="marketplace-search"
            onChange={(event) => setQuery(event.target.value)}
            placeholder={bi("搜索渠道名称…", "Search channels…")}
            aria-label={bi("搜索渠道名称", "Search channels")}
            className="w-full rounded-xl border border-[#E5E2DC] bg-white py-1.5 pr-2.5 pl-8 text-[12.5px] text-zinc-950 placeholder:text-stone-400 focus:border-[#059669] focus:outline-none focus:ring-[3px] focus:ring-emerald-500/15"
          />
        </label>
      </div>

      <div
        data-testid="marketplace-grid"
        className="grid grid-cols-1 gap-3.5 md:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4"
      >
        {filtered.map((item) => {
          const Icon = item.icon;
          const connected = connectedAccountCount(item, accounts);
          return (
            <article
              key={item.key}
              data-testid={`marketplace-card-${item.key}`}
              className="group flex flex-col rounded-2xl border border-[#E5E2DC] bg-white p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.95),0_1px_2px_rgba(24,24,27,0.04)] transition-all duration-200 hover:-translate-y-0.5 hover:border-[#D4CFC6] hover:shadow-[0_10px_26px_-16px_rgba(24,24,27,0.24)]"
            >
              <div className="flex items-start gap-3">
                <span
                  aria-hidden="true"
                  className={`grid h-10 w-10 shrink-0 place-items-center rounded-xl ring-1 ${item.tile}`}
                >
                  <Icon className="h-[18px] w-[18px]" />
                </span>
                <div className="min-w-0 flex-1">
                  <div className="flex items-start justify-between gap-2">
                    <h3 className="truncate text-[13.5px] font-bold text-zinc-950">
                      {bi(item.titleZh, item.titleEn)}
                    </h3>
                    <span
                      data-testid={`marketplace-status-${item.key}`}
                      className={`inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2 py-0.5 text-[10.5px] font-semibold ${
                        connected > 0
                          ? "border-emerald-200 bg-emerald-50 text-emerald-800"
                          : "border-stone-200 bg-stone-100 text-stone-600"
                      }`}
                    >
                      <span
                        aria-hidden="true"
                        className={`h-1.5 w-1.5 rounded-full ${
                          connected > 0 ? "bg-emerald-500" : "bg-stone-400"
                        }`}
                      />
                      {connected > 0
                        ? bi(`已接入 ${connected} 个`, `${connected} connected`)
                        : bi("未接入", "Not connected")}
                    </span>
                  </div>
                  <p className="mt-1 text-[11.5px] leading-5 text-stone-600">
                    {!isAdmin && item.key === "whatsapp"
                      ? bi(
                          "手机 WhatsApp App 共存号，限速 20 mps，一键接入个人号。",
                          "Phone-app coexistence number at 20 mps for your personal channels.",
                        )
                      : bi(item.descriptionZh, item.descriptionEn)}
                  </p>
                </div>
              </div>

              <span
                aria-hidden="true"
                className={`mt-3 h-[3px] w-9 rounded-full ${item.accent}`}
              />

              <div className="mt-3 flex items-center justify-between gap-2 border-t border-[#EFECE6] pt-3">
                <span className="inline-flex items-center gap-1 rounded-full border border-[#E5E2DC] bg-[#FAF8F5] px-2 py-0.5 text-[10.5px] font-medium text-stone-600">
                  {item.adminOnly
                    ? bi("仅公司公海 / 指派", "Company-shared / assignable")
                    : isAdmin
                      ? bi("支持个人专属 / 公司公海", "Personal or company-shared")
                      : bi("个人专属（仅自己与主管可见）", "Personal (you and managers)")}
                </span>
                <button
                  type="button"
                  data-testid={`open-connect-${item.key}`}
                  onClick={() => onOpen(item.key)}
                  className={`erp-btn shrink-0 rounded-xl px-3 py-1.5 text-[12px] font-semibold ${
                    connected > 0
                      ? "border border-[#DCD8D0] bg-white text-zinc-900 hover:border-[#C7C2B8] hover:bg-[#FAF8F5]"
                      : "bg-[#059669] text-white shadow-xs hover:bg-[#047857]"
                  }`}
                >
                  {connected > 0 ? <Plus className="h-3 w-3" /> : <CheckCircle2 className="h-3 w-3" />}
                  <span>{connected > 0 ? bi("添加账号", "Add account") : bi("快捷绑定", "Quick connect")}</span>
                </button>
              </div>
            </article>
          );
        })}
      </div>

      {filtered.length === 0 && (
        <div
          data-testid="marketplace-empty"
          className="rounded-2xl border border-dashed border-[#DCD8D0] bg-white/70 px-4 py-10 text-center text-[12.5px] font-medium text-stone-500"
        >
          {bi("没有匹配的渠道，换个关键词试试。", "No channel matches — try another keyword.")}
        </div>
      )}
    </section>
  );
}
