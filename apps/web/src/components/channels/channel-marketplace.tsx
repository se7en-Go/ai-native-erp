"use client";

import React, { useMemo, useState } from "react";
import {
  BadgeDollarSign,
  Building2,
  CheckCircle2,
  Globe,
  Instagram,
  Mail,
  MessageCircle,
  MessageSquare,
  Music2,
  Package,
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

/**
 * Pack F 新增的 3 个外贸高频渠道键（Alibaba.com 国际站 / Amazon / eBay）。
 * 「渠道应用市场」总数因此从 11 升到 14。
 */
export type SourcingMarketplaceChannelKey = "alibaba_intl" | "amazon" | "ebay";

/** 网格 / 抽屉 / 深链统一使用的渠道键集合（旧 11 个 + 新 3 个）。 */
export type ExtendedMarketplaceChannelKey =
  | MarketplaceChannelKey
  | SourcingMarketplaceChannelKey;

export const SOURCING_MARKETPLACE_KEYS: SourcingMarketplaceChannelKey[] = [
  "alibaba_intl",
  "amazon",
  "ebay",
];

/** 是否属于 Pack F 新增的 3 个外贸渠道。 */
export function isSourcingMarketplaceKey(
  value: string | null | undefined,
): value is SourcingMarketplaceChannelKey {
  return typeof value === "string" && SOURCING_MARKETPLACE_KEYS.includes(value as SourcingMarketplaceChannelKey);
}

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

/**
 * 渠道「真实能力」三档（诚实标注，不夸大）：
 * - live_messaging：真的能收发实时消息（Webhook 验签 + 出站发送）。
 * - order_sync：订单 / 库存 / 商品数据双向同步，但不承载会话消息。
 * - credentials_only：只做凭证加密托管，不保证实时外发（当前 14 个渠道已全部脱离该档位）。
 *
 * PR #44 之后，Alibaba.com IOP / Amazon SP-API / eBay Sell API 都具备真实的
 * 刊登推送与订单回拉适配器，因此这三个渠道从「仅凭证」升级为 `order_sync`。
 */
export type ChannelCapabilityTier =
  | "live_messaging"
  | "order_sync"
  | "credentials_only";

export type ChannelMarketplaceItem = {
  key: ExtendedMarketplaceChannelKey;
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
  /** 诚实能力档位 + 双语徽标文案（卡片上必须展示，避免过度承诺）。 */
  capabilityTier: ChannelCapabilityTier;
  capabilityBadgeZh: string;
  capabilityBadgeEn: string;
};

/** 能力徽标配色：即时消息=翠绿，订单同步=天蓝，仅凭证=琥珀。 */
export const CAPABILITY_BADGE_TONE: Record<ChannelCapabilityTier, string> = {
  live_messaging:
    "border-emerald-200 bg-emerald-50 text-emerald-800 dark:border-emerald-800/70 dark:bg-emerald-950/40 dark:text-emerald-300",
  order_sync:
    "border-sky-200 bg-sky-50 text-sky-800 dark:border-sky-800/70 dark:bg-sky-950/40 dark:text-sky-300",
  credentials_only:
    "border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-800/70 dark:bg-amber-950/40 dark:text-amber-300",
};

/**
 * 渠道应用市场清单：14 个渠道 = 即时通讯 4 + 邮件与官网 2 + 独立站与电商 8。
 * 顺序即产品优先级，与 Inbox 渠道筛选一一对应（末 3 个为 Pack F 外贸渠道）。
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
    capabilityTier: "live_messaging",
    capabilityBadgeZh: "实时消息收发",
    capabilityBadgeEn: "Live messaging",
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
    capabilityTier: "live_messaging",
    capabilityBadgeZh: "实时消息收发",
    capabilityBadgeEn: "Live messaging",
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
    capabilityTier: "live_messaging",
    capabilityBadgeZh: "实时消息收发",
    capabilityBadgeEn: "Live messaging",
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
    capabilityTier: "live_messaging",
    capabilityBadgeZh: "实时消息收发",
    capabilityBadgeEn: "Live messaging",
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
    channelTypes: ["email", "made_in_china_email"],
    adminOnly: false,
    capabilityTier: "live_messaging",
    capabilityBadgeZh: "实时消息收发",
    capabilityBadgeEn: "Live messaging",
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
    capabilityTier: "live_messaging",
    capabilityBadgeZh: "实时消息收发",
    capabilityBadgeEn: "Live messaging",
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
    capabilityTier: "order_sync",
    capabilityBadgeZh: "订单与库存同步",
    capabilityBadgeEn: "Order & stock sync",
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
    capabilityTier: "order_sync",
    capabilityBadgeZh: "订单与库存同步",
    capabilityBadgeEn: "Order & stock sync",
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
    capabilityTier: "order_sync",
    capabilityBadgeZh: "订单与库存同步",
    capabilityBadgeEn: "Order & stock sync",
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
    capabilityTier: "order_sync",
    capabilityBadgeZh: "订单与库存同步",
    capabilityBadgeEn: "Order & stock sync",
  },
  {
    key: "tiktok_shop",
    titleZh: "TikTok Shop",
    titleEn: "TikTok Shop",
    descriptionZh: "卖家自备应用（BYOK），店铺 ID + Cipher 一键授权。",
    descriptionEn: "Seller BYOK with shop ID and cipher in one authorization.",
    category: "commerce",
    icon: Music2,
    tile: "bg-zinc-900/5 text-zinc-900 ring-zinc-200 dark:bg-zinc-100/10 dark:text-zinc-100 dark:ring-zinc-700",
    accent: "bg-zinc-700 dark:bg-zinc-500",
    channelTypes: ["tiktok_shop"],
    adminOnly: true,
    capabilityTier: "order_sync",
    capabilityBadgeZh: "订单与库存同步",
    capabilityBadgeEn: "Order & stock sync",
  },
  {
    key: "alibaba_intl",
    titleZh: "Alibaba.com 国际站",
    titleEn: "Alibaba.com",
    descriptionZh:
      "App Key/Secret 信封加密托管，IOP 官方接口实时推送刊登，订单与询盘自动回拉进 ERP。",
    descriptionEn:
      "App key/secret vaulted (AES-256-GCM); the official IOP API pushes live listings and syncs orders and inquiries automatically.",
    category: "commerce",
    icon: Building2,
    tile: "bg-orange-50 text-orange-700 ring-orange-100",
    accent: "bg-orange-500",
    channelTypes: ["alibaba_email"],
    adminOnly: true,
    capabilityTier: "order_sync",
    capabilityBadgeZh: "实时刊登 · 订单与询盘",
    capabilityBadgeEn: "Live listing · Orders & inquiries",
  },
  {
    key: "amazon",
    titleZh: "Amazon 卖家中心",
    titleEn: "Amazon Seller Central",
    descriptionZh:
      "Seller ID / LWA 与 Refresh Token 信封加密托管，SP-API 实时推送刊登并自动拉取订单回写 ERP。",
    descriptionEn:
      "Seller ID, LWA keys and refresh token vaulted (AES-256-GCM); SP-API pushes live listings and pulls orders back into the ERP.",
    category: "commerce",
    icon: Package,
    tile: "bg-amber-50 text-amber-700 ring-amber-100",
    accent: "bg-amber-500",
    channelTypes: ["amazon_sp"],
    adminOnly: true,
    capabilityTier: "order_sync",
    capabilityBadgeZh: "SP-API 实时刊登与拉单",
    capabilityBadgeEn: "SP-API listing & order sync",
  },
  {
    key: "ebay",
    titleZh: "eBay",
    titleEn: "eBay",
    descriptionZh:
      "Seller Username / App + Cert ID 与 Refresh Token 信封加密托管，Sell API 实时推送刊登并自动拉取订单回写 ERP。",
    descriptionEn:
      "Seller username, app/cert IDs and refresh token vaulted (AES-256-GCM); the Sell API pushes live listings and pulls orders back into the ERP.",
    category: "commerce",
    icon: BadgeDollarSign,
    tile: "bg-sky-50 text-sky-700 ring-sky-100",
    accent: "bg-sky-600",
    channelTypes: ["ebay"],
    adminOnly: true,
    capabilityTier: "order_sync",
    capabilityBadgeZh: "Sell API 实时刊登与拉单",
    capabilityBadgeEn: "Sell API listing & order sync",
  },
];

/** 业务员个人可绑定的 3 个渠道：Telegram、个人工作邮箱、WhatsApp 共存号。 */
export const AGENT_MARKETPLACE_KEYS: MarketplaceChannelKey[] = [
  "telegram",
  "email",
  "whatsapp",
];

const MARKETPLACE_BY_KEY = new Map<ExtendedMarketplaceChannelKey, ChannelMarketplaceItem>(
  CHANNEL_MARKETPLACE.map((item) => [item.key, item]),
);

/** URL `?connect=` 的白名单校验，避免非法参数打开空抽屉。 */
export function isMarketplaceChannelKey(value: string | null): value is MarketplaceChannelKey {
  if (!value) return false;
  return MARKETPLACE_BY_KEY.has(value as ExtendedMarketplaceChannelKey);
}

export function marketplaceChannel(key: ExtendedMarketplaceChannelKey): ChannelMarketplaceItem {
  return MARKETPLACE_BY_KEY.get(key)!;
}

/**
 * Inbox 渠道筛选 → 市场抽屉键。WhatsApp Cloud/Coexistence 合并到 `whatsapp`，
 * Messenger 的筛选键是 `facebook_messenger`。
 */
export function marketplaceKeyForChannelFilter(
  filter: string,
): ExtendedMarketplaceChannelKey | null {
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
    case "made_in_china_email":
      return "email";
    case "alibaba_email":
    case "alibaba_intl":
      return "alibaba_intl";
    case "amazon_sp":
    case "amazon":
      return "amazon";
    case "ebay":
      return "ebay";
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
        <div className="flex flex-wrap items-center gap-1 rounded-xl border border-slate-200 bg-slate-100/90 p-1 dark:border-slate-700 dark:bg-slate-800/70">
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
                    ? "bg-blue-600 text-white shadow-xs dark:bg-blue-600"
                    : "text-slate-600 hover:bg-white/80 hover:text-slate-950 dark:text-slate-400 dark:hover:bg-slate-700/70 dark:hover:text-slate-100"
                }`}
              >
                <span>{bi(CATEGORY_COPY[option.value].zh, CATEGORY_COPY[option.value].en)}</span>
                <span
                  className={`rounded-full px-1.5 text-[10.5px] font-semibold tabular-nums ${
                    active
                      ? "bg-white/20 text-white"
                      : "bg-slate-200/80 text-slate-700 dark:bg-slate-700 dark:text-slate-300"
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
          <Search className="pointer-events-none absolute top-1/2 left-2.5 h-3.5 w-3.5 -translate-y-1/2 text-slate-400 dark:text-slate-500" />
          <input
            type="search"
            value={query}
            data-testid="marketplace-search"
            onChange={(event) => setQuery(event.target.value)}
            placeholder={bi("搜索渠道名称…", "Search channels…")}
            aria-label={bi("搜索渠道名称", "Search channels")}
            className="w-full rounded-xl border border-slate-200 bg-white py-1.5 pr-2.5 pl-8 text-[12.5px] text-slate-950 placeholder:text-slate-400 focus:border-blue-500 focus:outline-none focus:ring-[3px] focus:ring-blue-500/15 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100 dark:placeholder:text-slate-500"
          />
        </label>
      </div>

      {/* 14 张渠道卡 + 1 张引导卡：md 两列时引导卡横跨整行、xl 三列时正好 15 格，
          两种断点下都不留孤立空洞。 */}
      <div
        data-testid="marketplace-grid"
        className="grid grid-cols-1 gap-3.5 md:grid-cols-2 xl:grid-cols-3"
      >
        {filtered.map((item) => {
          const Icon = item.icon;
          const connected = connectedAccountCount(item, accounts);
          return (
            <article
              key={item.key}
              data-testid={`marketplace-card-${item.key}`}
              className="group flex min-w-0 flex-col rounded-2xl border border-slate-200 bg-white p-4 shadow-xs transition-all duration-200 hover:-translate-y-0.5 hover:border-blue-300 hover:shadow-[0_10px_26px_-16px_rgba(37,99,235,0.28)] dark:border-slate-800 dark:bg-slate-900 dark:hover:border-blue-700"
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
                    <h3 className="truncate text-[13.5px] font-bold text-slate-950 dark:text-slate-100">
                      {bi(item.titleZh, item.titleEn)}
                    </h3>
                    <span
                      data-testid={`marketplace-status-${item.key}`}
                      className={`inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2 py-0.5 text-[10.5px] font-semibold ${
                        connected > 0
                          ? "border-emerald-200 bg-emerald-50 text-emerald-800 dark:border-emerald-800/70 dark:bg-emerald-950/40 dark:text-emerald-300"
                          : "border-slate-200 bg-slate-100 text-slate-600 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-400"
                      }`}
                    >
                      <span
                        aria-hidden="true"
                        className={`h-1.5 w-1.5 rounded-full ${
                          connected > 0 ? "bg-emerald-500" : "bg-slate-400 dark:bg-slate-500"
                        }`}
                      />
                      {connected > 0
                        ? bi(`已接入 ${connected} 个`, `${connected} connected`)
                        : bi("未接入", "Not connected")}
                    </span>
                  </div>
                  <p className="mt-1 text-[11.5px] leading-5 text-slate-600 dark:text-slate-400">
                    {!isAdmin && item.key === "whatsapp"
                      ? bi(
                          "手机 WhatsApp App 共存号，限速 20 mps，一键接入个人号。",
                          "Phone-app coexistence number at 20 mps for your personal channels.",
                        )
                      : bi(item.descriptionZh, item.descriptionEn)}
                  </p>
                </div>
              </div>

              {/* 诚实能力徽标：每个渠道都明确标注「能做什么 / 不能做什么」。 */}
              <div className="mt-2 flex flex-wrap items-center gap-1.5">
                <span
                  data-testid={`channel-capability-${item.key}`}
                  data-capability-tier={item.capabilityTier}
                  className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10.5px] font-semibold ${
                    CAPABILITY_BADGE_TONE[item.capabilityTier]
                  }`}
                >
                  <span aria-hidden="true" className="h-1 w-1 rounded-full bg-current opacity-70" />
                  {bi(item.capabilityBadgeZh, item.capabilityBadgeEn)}
                </span>
              </div>

              <span
                aria-hidden="true"
                className={`mt-3 h-[3px] w-9 rounded-full ${item.accent}`}
              />

              <div className="mt-3 flex items-center justify-between gap-2 border-t border-slate-200 pt-3 dark:border-slate-800">
                <span className="inline-flex min-w-0 items-center gap-1 truncate rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-[10.5px] font-medium text-slate-600 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-400">
                  {item.adminOnly
                    ? bi("仅公司公海 / 指派", "Company-shared / assignable")
                    : isAdmin
                      ? bi("支持个人专属 / 公司公海", "Personal or company-shared")
                      : bi("个人专属（仅自己与主管可见）", "Personal (you and managers)")}
                </span>
                <button
                  type="button"
                  data-testid={`open-connect-${item.key}`}
                  onClick={() => onOpen(item.key as MarketplaceChannelKey)}
                  className={`erp-btn shrink-0 rounded-xl px-3 py-1.5 text-[12px] font-semibold ${
                    connected > 0
                      ? "border border-slate-300 bg-white text-slate-900 hover:border-slate-400 hover:bg-slate-50 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-100 dark:hover:bg-slate-800"
                      : "bg-blue-600 text-white shadow-xs hover:bg-blue-700 dark:bg-blue-600 dark:hover:bg-blue-500"
                  }`}
                >
                  {connected > 0 ? <Plus className="h-3 w-3" /> : <CheckCircle2 className="h-3 w-3" />}
                  <span>{connected > 0 ? bi("添加账号", "Add account") : bi("快捷绑定", "Quick connect")}</span>
                </button>
              </div>
            </article>
          );
        })}

        {/* 第 15 张纯静态引导卡：只在「全部分类 + 无搜索词」时出现，
            不参与已接入计数，也不复用 marketplace-card-* testid。
            14 个渠道 + 1 张引导卡 → 3 列网格完全齐平、2 列时引导卡横跨两列，不留空洞。 */}
        {category === "all" && query.trim() === "" ? (
          <article
            data-testid="marketplace-custom-channel-card"
            className="group flex min-w-0 flex-col rounded-2xl border border-dashed border-slate-300 bg-slate-50/60 p-4 transition-colors hover:border-blue-400 hover:bg-blue-50/40 md:col-span-2 xl:col-span-1 dark:border-slate-700 dark:bg-slate-900/40 dark:hover:border-blue-700 dark:hover:bg-blue-950/30"
          >
            <div className="flex items-start gap-3">
              <span
                aria-hidden="true"
                className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-blue-50 text-blue-600 ring-1 ring-blue-100 dark:bg-blue-900/30 dark:text-blue-400 dark:ring-blue-800/60"
              >
                <Plus className="h-[18px] w-[18px]" />
              </span>
              <div className="min-w-0 flex-1">
                <h3 className="truncate text-[13.5px] font-bold text-slate-950 dark:text-slate-100">
                  {bi("自定义 Webhook / API 渠道", "Custom webhook / API channel")}
                </h3>
                <p className="mt-1 text-[11.5px] leading-5 text-slate-600 dark:text-slate-400">
                  {bi(
                    "自有系统或尚未覆盖的平台：提供入站 Webhook 与出站 API，原始报文验签后统一进 Inbox。",
                    "Your own system or a platform not covered yet: inbound webhook plus outbound API, verified raw bodies land in Inbox."
                  )}
                </p>
              </div>
            </div>

            <span aria-hidden="true" className="mt-3 h-[3px] w-9 rounded-full bg-blue-500/80" />

            <div className="mt-3 flex items-center justify-between gap-2 border-t border-slate-200 pt-3 dark:border-slate-800">
              <span className="inline-flex min-w-0 items-center gap-1 truncate rounded-full border border-slate-200 bg-white px-2 py-0.5 text-[10.5px] font-medium text-slate-600 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-400">
                {bi("需技术人员接入", "Needs an engineer to wire it")}
              </span>
              <span className="erp-btn shrink-0 cursor-default rounded-xl border border-slate-300 bg-white px-3 py-1.5 text-[12px] font-semibold text-slate-900 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-100">
                {bi("联系管理员开通", "Ask an admin")}
              </span>
            </div>
          </article>
        ) : null}
      </div>

      {filtered.length === 0 && (
        <div
          data-testid="marketplace-empty"
          className="rounded-2xl border border-dashed border-slate-300 bg-white/70 px-4 py-10 text-center text-[12.5px] font-medium text-slate-500 dark:border-slate-700 dark:bg-slate-900/70 dark:text-slate-400"
        >
          {bi("没有匹配的渠道，换个关键词试试。", "No channel matches — try another keyword.")}
        </div>
      )}
    </section>
  );
}
