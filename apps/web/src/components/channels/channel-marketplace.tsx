"use client";

import React, { useMemo, useState } from "react";
import {
  BadgeDollarSign,
  Boxes,
  Building2,
  CheckCircle2,
  Compass,
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
  Shirt,
  ShoppingBag,
  ShoppingCart,
  Store,
  Truck,
} from "lucide-react";

import { useI18n } from "@/lib/i18n";
import type {
  ChannelAccountApiItem,
  MarketplaceChannelKey,
} from "@/lib/api-client/live-client";
import type { GlobalMarketplaceChannelKey } from "@/lib/api-client/pack-f-sourcing";
import type { ChannelType } from "@/lib/contracts";

export type MarketplaceCategory = "all" | "messaging" | "email_web" | "commerce";

/**
 * Pack F 新增的 3 个外贸高频渠道键（Alibaba.com 国际站 / Amazon / eBay）。
 * 「渠道应用市场」总数因此从 11 升到 14。
 */
export type SourcingMarketplaceChannelKey = "alibaba_intl" | "amazon" | "ebay";

/**
 * 本轮新增的 8 个全球电商渠道键（Shopee / Lazada / Temu / SHEIN /
 * AliExpress / Ozon / Walmart / Mercado Libre）。
 * 与后端 `ChannelType` 的 8 个同名枚举值一字不差，抽屉据此选接入端点。
 */
export const GLOBAL_MARKETPLACE_KEYS: GlobalMarketplaceChannelKey[] = [
  "shopee",
  "lazada",
  "temu",
  "shein",
  "aliexpress",
  "ozon",
  "walmart",
  "mercado_libre",
];

/** 是否属于本轮新增的 8 个全球电商渠道。 */
export function isGlobalMarketplaceKey(
  value: string | null | undefined,
): value is GlobalMarketplaceChannelKey {
  return (
    typeof value === "string" &&
    GLOBAL_MARKETPLACE_KEYS.includes(value as GlobalMarketplaceChannelKey)
  );
}

/** 网格 / 抽屉 / 深链统一使用的渠道键集合（旧 11 个 + Pack F 3 个 + 全球电商 8 个）。 */
export type ExtendedMarketplaceChannelKey =
  | MarketplaceChannelKey
  | SourcingMarketplaceChannelKey
  | GlobalMarketplaceChannelKey;

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
 * - order_sync：订单 / 库存 / 商品数据双向同步，但主通道不是通用 IM。
 * - credentials_only：只做凭证加密托管，不保证实时外发（当前 22 个渠道已全部脱离该档位）。
 *
 * PR #44 之后，Alibaba.com IOP / Amazon SP-API / eBay Sell API 都具备真实的
 * 刊登推送与订单回拉适配器，因此这三个渠道从「仅凭证」升级为 `order_sync`。
 * 本轮新增的 8 个全球电商渠道（Shopee / Lazada / Temu / SHEIN / AliExpress /
 * Ozon / Walmart / Mercado Libre）同样带真实订单与库存同步适配器，因此
 * 22 个渠道目前全部落在 `live_messaging` 或 `order_sync`，`credentials_only` 为空。
 *
 * 关于「买家站内私信 IM」：12 个大跨境电商渠道（下面 `MARKETPLACE_BUYER_IM_KEYS`）
 * 同时接入了平台买家 IM（`/api/v1/webhooks/im/{platform}` 验签入站 + 签名出站），
 * 但这并不改变订单同步档位——电商渠道的主身份仍然是店铺（订单 / 库存 / 刊登），
 * 因此档位保持 `order_sync`，只把徽标文案升级为「商品/库存/订单 + 买家 IM」，
 * 让能力描述与徽标同时如实反映两类能力（`wave5-honest-ui` 逐渠道锁定该档位）。
 */
export type ChannelCapabilityTier =
  | "live_messaging"
  | "order_sync"
  | "credentials_only";

/**
 * 12 个「买家站内私信 IM」渠道的市场键：Shopee / Lazada / Temu / SHEIN /
 * AliExpress / Ozon / Walmart / Mercado Libre / TikTok Shop / eBay /
 * Amazon Seller Central / Alibaba.com 国际站。
 *
 * 这 12 个渠道除了订单 / 库存 / 刊登，还支持平台买家站内私信双向收发：
 * 入站 `/api/v1/webhooks/im/{platform}` 验签、出站签名发送、`/inbox` 统一收件箱。
 * 独立站（Shopify / WooCommerce / SHOPLINE / Shoplazza）没有平台买家 IM，
 * 因此不在本清单内，徽标保持纯「订单与库存同步」。
 */
export const MARKETPLACE_BUYER_IM_KEYS: ExtendedMarketplaceChannelKey[] = [
  "tiktok_shop",
  "amazon",
  "ebay",
  "alibaba_intl",
  "shopee",
  "lazada",
  "temu",
  "shein",
  "aliexpress",
  "ozon",
  "walmart",
  "mercado_libre",
];

/** 该渠道（市场键或渠道类型）是否属于「买家站内私信 IM」渠道。 */
export function hasMarketplaceBuyerIm(key: string | null | undefined): boolean {
  if (!key) return false;
  if (MARKETPLACE_BUYER_IM_KEYS.includes(key as ExtendedMarketplaceChannelKey)) return true;
  // 渠道类型与市场键的差异只有两个：amazon→amazon_sp、alibaba_intl→alibaba_email。
  const asKey = key === "amazon_sp" ? "amazon" : key === "alibaba_email" ? "alibaba_intl" : key;
  return MARKETPLACE_BUYER_IM_KEYS.includes(asKey as ExtendedMarketplaceChannelKey);
}

export type ChannelMarketplaceItem = {
  key: ExtendedMarketplaceChannelKey;
  titleZh: string;
  titleEn: string;
  descriptionZh: string;
  descriptionEn: string;
  category: Exclude<MarketplaceCategory, "all">;
  icon: React.ComponentType<{ className?: string }>;
  /** 40x40 品牌着色图标底，一眼区分 22 个渠道。 */
  tile: string;
  accent: string;
  /**
   * 计入「已接入 N 个」的账号类型（WhatsApp 含 Cloud + Coexistence）。
   * 8 个全球电商渠道的枚举值已在 `@/lib/contracts` 的 `ChannelTypeSchema` 中就位。
   */
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
 * 渠道应用市场清单：22 个渠道 = 即时通讯 4 + 邮件与官网 2 + 独立站与电商 16。
 * 顺序即产品优先级，与 Inbox 渠道筛选一一对应：
 * 第 12-14 个是 Pack F 外贸渠道，第 15-22 个是本轮 8 个全球电商渠道。
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
    descriptionZh:
      "卖家自备应用（BYOK），店铺 ID + Cipher 一键授权；商品、库存、订单与买家站内私信 IM 共用同一收件箱。",
    descriptionEn:
      "Seller BYOK with shop ID and cipher in one authorization; catalog, stock, orders and buyer IM share one inbox.",
    category: "commerce",
    icon: Music2,
    tile: "bg-zinc-900/5 text-zinc-900 ring-zinc-200 dark:bg-zinc-100/10 dark:text-zinc-100 dark:ring-zinc-700",
    accent: "bg-zinc-700 dark:bg-zinc-500",
    channelTypes: ["tiktok_shop"],
    adminOnly: true,
    capabilityTier: "order_sync",
    capabilityBadgeZh: "商品/库存/订单 + 买家 IM",
    capabilityBadgeEn: "Catalog/Stock/Orders + Buyer IM",
  },
  {
    key: "alibaba_intl",
    titleZh: "Alibaba.com 国际站",
    titleEn: "Alibaba.com",
    descriptionZh:
      "App Key/Secret 信封加密托管，IOP 官方接口实时推送刊登，订单、询盘与买家站内私信 IM 自动回拉进 ERP。",
    descriptionEn:
      "App key/secret vaulted (AES-256-GCM); the official IOP API pushes live listings and syncs orders, inquiries and buyer IM automatically.",
    category: "commerce",
    icon: Building2,
    tile: "bg-orange-50 text-orange-700 ring-orange-100",
    accent: "bg-orange-500",
    channelTypes: ["alibaba_email"],
    adminOnly: true,
    capabilityTier: "order_sync",
    capabilityBadgeZh: "实时刊登 · 订单与询盘 + 买家 IM",
    capabilityBadgeEn: "Live listing · Orders & inquiries + Buyer IM",
  },
  {
    key: "amazon",
    titleZh: "Amazon 卖家中心",
    titleEn: "Amazon Seller Central",
    descriptionZh:
      "Seller ID / LWA 与 Refresh Token 信封加密托管，SP-API 实时推送刊登并自动拉取订单与买家站内私信 IM 回写 ERP。",
    descriptionEn:
      "Seller ID, LWA keys and refresh token vaulted (AES-256-GCM); SP-API pushes live listings and pulls orders plus buyer IM back into the ERP.",
    category: "commerce",
    icon: Package,
    tile: "bg-amber-50 text-amber-700 ring-amber-100",
    accent: "bg-amber-500",
    channelTypes: ["amazon_sp"],
    adminOnly: true,
    capabilityTier: "order_sync",
    capabilityBadgeZh: "SP-API 实时刊登与拉单 + 买家 IM",
    capabilityBadgeEn: "SP-API listing & order sync + Buyer IM",
  },
  {
    key: "ebay",
    titleZh: "eBay",
    titleEn: "eBay",
    descriptionZh:
      "Seller Username / App + Cert ID 与 Refresh Token 信封加密托管，Sell API 实时推送刊登并自动拉取订单与买家站内私信 IM 回写 ERP。",
    descriptionEn:
      "Seller username, app/cert IDs and refresh token vaulted (AES-256-GCM); the Sell API pushes live listings and pulls orders plus buyer IM back into the ERP.",
    category: "commerce",
    icon: BadgeDollarSign,
    tile: "bg-sky-50 text-sky-700 ring-sky-100",
    accent: "bg-sky-600",
    channelTypes: ["ebay"],
    adminOnly: true,
    capabilityTier: "order_sync",
    capabilityBadgeZh: "Sell API 实时刊登与拉单 + 买家 IM",
    capabilityBadgeEn: "Sell API listing & order sync + Buyer IM",
  },
  {
    key: "shopee",
    titleZh: "Shopee",
    titleEn: "Shopee",
    descriptionZh:
      "跨境直邮 / 全托管 / 轻出海（light_export）三种履约模式任选，Partner ID 与 Key 信封加密托管，订单、库存与买家站内私信 IM 统一进收件箱。",
    descriptionEn:
      "Pick cross-border, full-managed or light-export fulfillment; partner ID and key are envelope-encrypted and orders, stock plus buyer IM land in one inbox.",
    category: "commerce",
    icon: ShoppingCart,
    tile: "bg-orange-50 text-orange-700 ring-orange-100",
    accent: "bg-orange-500",
    channelTypes: ["shopee"],
    adminOnly: true,
    capabilityTier: "order_sync",
    capabilityBadgeZh: "商品/库存/订单 + 买家 IM",
    capabilityBadgeEn: "Catalog/Stock/Orders + Buyer IM",
  },
  {
    key: "lazada",
    titleZh: "Lazada",
    titleEn: "Lazada",
    descriptionZh:
      "跨境自发货或全托管（full_managed）二选一，App Key/Secret 与 Access Token 信封加密，东南亚六站订单、库存与买家站内私信 IM 统一回拉。",
    descriptionEn:
      "Choose cross-border self-ship or full-managed; app key/secret and access token are envelope-encrypted and SEA orders, stock plus buyer IM pull back into one place.",
    category: "commerce",
    icon: Truck,
    tile: "bg-pink-50 text-pink-700 ring-pink-100",
    accent: "bg-pink-500",
    channelTypes: ["lazada"],
    adminOnly: true,
    capabilityTier: "order_sync",
    capabilityBadgeZh: "商品/库存/订单 + 买家 IM",
    capabilityBadgeEn: "Catalog/Stock/Orders + Buyer IM",
  },
  {
    key: "temu",
    titleZh: "Temu",
    titleEn: "Temu",
    descriptionZh:
      "全托管 / 半托管（semi_managed）/ 本土（local）三种备货方式都能接，卖家自备应用密钥信封加密，订单、库存与买家站内私信 IM 自动进 ERP。",
    descriptionEn:
      "Full-managed, semi-managed or local stock all supported; seller BYOK credentials are envelope-encrypted and orders, stock plus buyer IM land in the ERP.",
    category: "commerce",
    icon: Boxes,
    tile: "bg-red-50 text-red-600 ring-red-100",
    accent: "bg-red-500",
    channelTypes: ["temu"],
    adminOnly: true,
    capabilityTier: "order_sync",
    capabilityBadgeZh: "商品/库存/订单 + 买家 IM",
    capabilityBadgeEn: "Catalog/Stock/Orders + Buyer IM",
  },
  {
    key: "shein",
    titleZh: "SHEIN",
    titleEn: "SHEIN",
    descriptionZh:
      "自营（standard）与半托管（semi_managed）双模式，Open Key ID / Secret Key 信封加密托管，订单、库存与买家站内私信 IM 实时对齐。",
    descriptionEn:
      "Self-operated or semi-managed both supported; Open Key ID / Secret Key are envelope-encrypted and orders, stock plus buyer IM stay aligned.",
    category: "commerce",
    icon: Shirt,
    tile: "bg-fuchsia-50 text-fuchsia-700 ring-fuchsia-100",
    accent: "bg-fuchsia-600",
    channelTypes: ["shein"],
    adminOnly: true,
    capabilityTier: "order_sync",
    capabilityBadgeZh: "商品/库存/订单 + 买家 IM",
    capabilityBadgeEn: "Catalog/Stock/Orders + Buyer IM",
  },
  {
    key: "aliexpress",
    titleZh: "AliExpress 速卖通",
    titleEn: "AliExpress",
    descriptionZh:
      "自运营（standard）与托管（managed）两种履约方式，App Key/Secret 与 Access Token 信封加密，订单自动建单，买家站内私信 IM 同步进收件箱。",
    descriptionEn:
      "Self-operated or managed fulfillment; app key/secret and access token are envelope-encrypted, orders are created automatically and buyer IM lands in the inbox.",
    category: "commerce",
    icon: Compass,
    tile: "bg-amber-50 text-amber-700 ring-amber-100",
    accent: "bg-amber-500",
    channelTypes: ["aliexpress"],
    adminOnly: true,
    capabilityTier: "order_sync",
    capabilityBadgeZh: "商品/库存/订单 + 买家 IM",
    capabilityBadgeEn: "Catalog/Stock/Orders + Buyer IM",
  },
  {
    key: "ozon",
    titleZh: "Ozon",
    titleEn: "Ozon",
    descriptionZh:
      "FBS（自发货）与 FBO（平台仓代发）双轨可选，Client ID / API Key 信封加密，俄语区订单、库存与买家站内私信 IM 自动同步。",
    descriptionEn:
      "FBS self-ship or FBO platform-hosting; Client ID / API Key are envelope-encrypted and RU-region orders, stock plus buyer IM sync automatically.",
    category: "commerce",
    icon: Globe,
    tile: "bg-cyan-50 text-cyan-700 ring-cyan-100",
    accent: "bg-cyan-600",
    channelTypes: ["ozon"],
    adminOnly: true,
    capabilityTier: "order_sync",
    capabilityBadgeZh: "商品/库存/订单 + 买家 IM",
    capabilityBadgeEn: "Catalog/Stock/Orders + Buyer IM",
  },
  {
    key: "walmart",
    titleZh: "Walmart 沃尔玛",
    titleEn: "Walmart Marketplace",
    descriptionZh:
      "自发货（standard）或 WFS 平台仓（wfs）都支持，Client ID / Secret 信封加密，北美订单、库存与买家站内私信 IM 双向同步。",
    descriptionEn:
      "Self-ship or WFS platform hosting; client ID / secret are envelope-encrypted and North-America orders, stock plus buyer IM sync both ways.",
    category: "commerce",
    icon: Store,
    tile: "bg-indigo-50 text-indigo-700 ring-indigo-100",
    accent: "bg-indigo-500",
    channelTypes: ["walmart"],
    adminOnly: true,
    capabilityTier: "order_sync",
    capabilityBadgeZh: "商品/库存/订单 + 买家 IM",
    capabilityBadgeEn: "Catalog/Stock/Orders + Buyer IM",
  },
  {
    key: "mercado_libre",
    titleZh: "Mercado Libre 美客多",
    titleEn: "Mercado Libre",
    descriptionZh:
      "自发货（standard）与全托管（full_managed）任选，Access / Refresh Token 信封加密，拉美站点订单与买家站内私信 IM 自动回写 ERP。",
    descriptionEn:
      "Self-ship or full-managed fulfillment; access/refresh tokens are envelope-encrypted and LatAm orders plus buyer IM are written back into the ERP.",
    category: "commerce",
    icon: BadgeDollarSign,
    tile: "bg-yellow-50 text-yellow-700 ring-yellow-100",
    accent: "bg-yellow-500",
    channelTypes: ["mercado_libre"],
    adminOnly: true,
    capabilityTier: "order_sync",
    capabilityBadgeZh: "商品/库存/订单 + 买家 IM",
    capabilityBadgeEn: "Catalog/Stock/Orders + Buyer IM",
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

/** URL `?connect=` 的白名单校验，避免非法参数打开空抽屉。
 *
 * 实现校验的是全量 22 个渠道键（含 Pack F 与 8 个全球电商渠道）；
 * 返回的 `value is MarketplaceChannelKey` 断言仅为兼容既有调用方——
 * `app/settings/channels/page.tsx` 的 `drawerKey` 状态仍标注旧键集，
 * 而抽屉的 `channelKey` 入参已放宽到 `ExtendedMarketplaceChannelKey`，
 * 所以 8 个新渠道的深链在运行时能正常打开对应抽屉。
 */
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
    case "shopee":
      return "shopee";
    case "lazada":
      return "lazada";
    case "temu":
      return "temu";
    case "shein":
      return "shein";
    case "aliexpress":
      return "aliexpress";
    case "ozon":
      return "ozon";
    case "walmart":
      return "walmart";
    case "mercado_libre":
      return "mercado_libre";
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
  /**
   * 点「快捷绑定」时上抛渠道键（旧 11 + Pack F 3 + 全球电商 8）。
   * 用方法式签名（双变）：既接受 `ExtendedMarketplaceChannelKey` 处理器，
   * 也接受既有 `(key: MarketplaceChannelKey) => void` 的调用方，
   * 这样新增 8 个渠道时不必强制改动 `app/settings/channels/page.tsx`。
   */
  onOpen(key: ExtendedMarketplaceChannelKey): void;
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

      {/* 22 张渠道卡 + 1 张引导卡：md 两列时 22 卡恰好填满 11 行、引导卡横跨整行；
          xl 三列时 22 卡占 7 行余 1 格，引导卡横跨 2 格补齐最后一行，
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
                  onClick={() => onOpen(item.key)}
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

        {/* 第 23 张纯静态引导卡：只在「全部分类 + 无搜索词」时出现，
            不参与已接入计数，也不复用 marketplace-card-* testid。
            22 个渠道 + 1 张引导卡 → md 两列时引导卡横跨整行，
            xl 三列时横跨 2 格补齐最后一排，不留空洞。 */}
        {category === "all" && query.trim() === "" ? (
          <article
            data-testid="marketplace-custom-channel-card"
            className="group flex min-w-0 flex-col rounded-2xl border border-dashed border-slate-300 bg-slate-50/60 p-4 transition-colors hover:border-blue-400 hover:bg-blue-50/40 md:col-span-2 xl:col-span-2 dark:border-slate-700 dark:bg-slate-900/40 dark:hover:border-blue-700 dark:hover:bg-blue-950/30"
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
