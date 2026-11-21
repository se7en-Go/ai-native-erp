"use client";

import React, { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import {
  ArrowLeft,
  ArrowUpRight,
  Check,
  CheckCircle2,
  ChevronDown,
  Copy,
  KeyRound,
  Loader2,
  LockKeyhole,
  ShieldCheck,
  Sparkles,
  X,
} from "lucide-react";

import { useI18n } from "@/lib/i18n";
import {
  OwnershipSelector,
  extractTelegramBotToken,
  isMessagingChannelDraftValid,
  telegramBotIdOf,
} from "@/components/channels/messaging-channel-cards";
import { isEmailConnectDraftValid } from "@/components/channels/quick-connect-panel";
import {
  isSourcingMarketplaceKey,
  livechatEmbedSnippet,
  marketplaceChannel,
  type ExtendedMarketplaceChannelKey,
} from "@/components/channels/channel-marketplace";
import {
  connectAlibabaIntl,
  connectAmazon,
  connectEbay,
  type SourcingChannelKey,
} from "@/lib/api-client/pack-f-sourcing";
import type {
  ChannelAccountApiItem,
  ChannelVisibilityScope,
  EmailConnectPayload,
  InstagramConnectPayload,
  MarketplaceChannelKey,
  MarketplaceConnectRequest,
  MessengerConnectPayload,
  MessagingConnectResult,
  ShopifyCustomAppConnectPayload,
  ShoplineConnectPayload,
  ShoplazzaConnectPayload,
  TeamMemberApiItem,
  TelegramConnectPayload,
  TikTokShopConnectPayload,
  WebLivechatConnectPayload,
  WhatsAppDirectConnectPayload,
  WhatsAppEmbeddedSignupPayload,
  WooCommerceConnectPayload,
} from "@/lib/api-client/live-client";

export type WhatsAppConnectMode = "direct" | "embedded";
type EmailProvider = "resend" | "smtp_imap";

type Draft = {
  bot_token: string;
  display_name: string;
  waba_id: string;
  phone_number_id: string;
  access_token: string;
  code: string;
  email_address: string;
  api_key: string;
  smtp_host: string;
  smtp_port: string;
  smtp_username: string;
  smtp_password: string;
  imap_host: string;
  imap_port: string;
  page_id: string;
  page_access_token: string;
  ig_account_id: string;
  site_domain: string;
  shop_domain: string;
  client_id: string;
  client_secret: string;
  admin_access_token: string;
  store_url: string;
  consumer_key: string;
  consumer_secret: string;
  webhook_secret: string;
  store_domain: string;
  app_secret: string;
  shop_id: string;
  shop_cipher: string;
  app_key: string;
  refresh_token: string;
  seller_id: string;
  marketplace_id: string;
  site_id: string;
  seller_username: string;
  store_id: string;
  inquiry_email: string;
  lwa_client_id: string;
  lwa_client_secret: string;
  app_id: string;
  cert_id: string;
};

const EMPTY_DRAFT: Draft = {
  bot_token: "",
  display_name: "",
  waba_id: "",
  phone_number_id: "",
  access_token: "",
  code: "",
  email_address: "",
  api_key: "",
  smtp_host: "",
  smtp_port: "465",
  smtp_username: "",
  smtp_password: "",
  imap_host: "",
  imap_port: "993",
  page_id: "",
  page_access_token: "",
  ig_account_id: "",
  site_domain: "",
  shop_domain: "",
  client_id: "",
  client_secret: "",
  admin_access_token: "",
  store_url: "",
  consumer_key: "",
  consumer_secret: "",
  webhook_secret: "",
  store_domain: "",
  app_secret: "",
  shop_id: "",
  shop_cipher: "",
  app_key: "",
  refresh_token: "",
  seller_id: "",
  marketplace_id: "ATVPDKIKX0DER",
  site_id: "EBAY-US",
  seller_username: "",
  store_id: "",
  inquiry_email: "",
  lwa_client_id: "",
  lwa_client_secret: "",
  app_id: "",
  cert_id: "",
};

const META_ID = /^[0-9]{1,32}$/;
const DOMAIN = /^(https?:\/\/)?([a-z0-9-]+\.)+[a-z]{2,}(:\d{2,5})?(\/[\w\-./?%&=]*)?$/i;

function trimmed(value: string): string {
  return value.trim();
}

/**
 * WooCommerce REST credentials are addressed by an absolute base URL, so a bare
 * domain (`shop.example.com`) must be upgraded to `https://shop.example.com`
 * (trailing slashes stripped) before it reaches the connector — otherwise the
 * backend builds a relative/invalid REST base and signature verification fails
 * (GPT #24).
 */
export function normalizeWooCommerceStoreUrl(raw: string): string {
  const value = trimmed(raw);
  if (!value) return "";
  const withScheme = /^[a-z][a-z0-9+.-]*:\/\//i.test(value) ? value : `https://${value}`;
  return withScheme.replace(/\/+$/, "");
}

/** 三步入驻指引：抽屉里默认折叠，点开就是可照抄的中文步骤。 */
const GUIDES: Record<ExtendedMarketplaceChannelKey, Array<{ zh: string; en: string }>> = {
  whatsapp: [
    { zh: "在 Meta 开发者后台创建 WhatsApp Business 应用", en: "Create a WhatsApp Business app in Meta for Developers" },
    { zh: "复制 WABA ID 与手机号 ID（或走 Embedded Signup 授权）", en: "Copy the WABA ID and phone number ID, or use Embedded Signup" },
    { zh: "粘贴永久访问令牌，勾选共存模式后一键绑定", en: "Paste the permanent token, tick coexistence and connect" },
  ],
  telegram: [
    { zh: "打开 @BotFather 发送 /newbot 创建机器人", en: "Open @BotFather and send /newbot" },
    { zh: "把整段 Token 回复粘贴进来，系统自动识别 Bot ID", en: "Paste the whole reply — the bot ID is auto-detected" },
    { zh: "一键连接后自动配置 Webhook，令牌信封加密保管", en: "Connect to auto-configure the webhook and encrypt the token" },
  ],
  messenger: [
    { zh: "在 Meta 应用里添加 Messenger 产品", en: "Add the Messenger product in your Meta app" },
    { zh: "生成主页访问令牌并复制主页 ID", en: "Generate a Page access token and copy the Page ID" },
    { zh: "粘贴后一键绑定，24 小时客服窗口自动生效", en: "Bind it and the 24h reply window is applied automatically" },
  ],
  instagram_dm: [
    { zh: "将 Instagram 专业号关联到 Meta 公共主页", en: "Link your Instagram professional account to a Meta Page" },
    { zh: "复制 IG 账号 ID 与长期访问令牌", en: "Copy the IG account ID and a long-lived access token" },
    { zh: "一键连接，快拍提及与私信统一进 Inbox", en: "Connect so story mentions and DMs land in one inbox" },
  ],
  email: [
    { zh: "准备 Resend API Key，或企业邮箱的 SMTP 授权码", en: "Prepare a Resend key or your mailbox SMTP credentials" },
    { zh: "填入邮箱地址，选择 Resend 或 SMTP/IMAP", en: "Enter the address and pick Resend or SMTP/IMAP" },
    { zh: "一键绑定后自动校验 SPF/DKIM，询盘直接进 Inbox", en: "SPF/DKIM are verified and inquiries land in the inbox" },
  ],
  web_livechat: [
    { zh: "填写官网站点域名（如 https://shop.example.com）", en: "Enter your site domain (e.g. https://shop.example.com)" },
    { zh: "一键绑定后复制生成的嵌入脚本", en: "Copy the embed snippet generated after connecting" },
    { zh: "把脚本放进网站 </body> 前，访客消息实时进 Inbox", en: "Paste the snippet before </body> to stream visitor chats" },
  ],
  shopify: [
    { zh: "在 Shopify 后台创建自定义应用并授予订单/商品权限", en: "Create a custom app in Shopify admin with order/product scopes" },
    { zh: "复制 Client ID、Client Secret（可选 Admin API Token）", en: "Copy the client ID, secret and optional admin token" },
    { zh: "粘贴店铺域名后一键绑定，订单与库存自动同步", en: "Bind with the shop domain to sync orders and stock" },
  ],
  woocommerce: [
    { zh: "WooCommerce → 设置 → 高级 → REST API 新建密钥", en: "WooCommerce → Settings → Advanced → REST API → add key" },
    { zh: "权限选「读/写」，复制 Consumer Key 与 Secret", en: "Grant read/write and copy the consumer key and secret" },
    { zh: "填入店铺地址后一键绑定，Webhook 自动注册", en: "Enter the store URL to bind and register the webhook" },
  ],
  shopline: [
    { zh: "在 SHOPLINE 开放平台创建私有应用", en: "Create a private app on the SHOPLINE open platform" },
    { zh: "复制店铺域名、Access Token 与 App Secret", en: "Copy the store domain, access token and app secret" },
    { zh: "一键绑定，多店铺商品与订单统一管理", en: "Bind once to manage multi-store catalog and orders" },
  ],
  shoplazza: [
    { zh: "在店匠开放平台创建应用并授权店铺", en: "Create an app on the Shoplazza platform and authorize the store" },
    { zh: "复制店铺域名、Access Token 与 Client Secret", en: "Copy the store domain, access token and client secret" },
    { zh: "一键绑定，独立站订单与买家消息同源", en: "Bind so storefront orders and buyer chats share one source" },
  ],
  tiktok_shop: [
    { zh: "在 TikTok Shop 合作伙伴中心创建卖家自备应用", en: "Create a seller BYOK app in TikTok Shop Partner Center" },
    { zh: "复制 Shop ID、Shop Cipher、App Key 与 App Secret", en: "Copy the shop ID, shop cipher, app key and secret" },
    { zh: "补上访问令牌后一键绑定，订单与 TikTok 会话同步", en: "Add the access token to sync orders and TikTok chats" },
  ],
  alibaba_intl: [
    { zh: "在 Alibaba.com 卖家中心 → 开放平台申请 App Key / Secret", en: "Apply for an App Key/Secret in Alibaba.com Seller Central → Open Platform" },
    { zh: "填写店铺 ID 与询盘接收邮箱（可留空沿用工作邮箱）", en: "Enter the store ID and the inquiry mailbox (optional)" },
    { zh: "一键绑定后询盘自动进 Inbox，密钥信封加密保存", en: "Bind once so inquiries land in Inbox with envelope-encrypted keys" },
  ],
  amazon: [
    { zh: "Seller Central → 开发者中心注册 LWA 应用", en: "Register an LWA app in Seller Central → Developer Central" },
    { zh: "复制 Seller ID、Marketplace ID、LWA Client ID / Secret 与 Refresh Token", en: "Copy the seller ID, marketplace ID, LWA client ID/secret and refresh token" },
    { zh: "一键加密直连，订单、库存与买家消息同步进主档", en: "Direct-connect to sync orders, stock and buyer messages" },
  ],
  ebay: [
    { zh: "eBay 开发者计划创建生产应用，拿到 App ID / Cert ID", en: "Create a production app in the eBay developer program for the App/Cert IDs" },
    { zh: "用 OAuth 换取 Refresh Token，并确认卖家用户名与站点 ID", en: "Exchange an OAuth refresh token and confirm the seller username and site ID" },
    { zh: "一键加密直连，eBay 订单与刊登 SKU 双向同步", en: "Direct-connect for two-way order and listing SKU sync" },
  ],
};

const FIELD_LABEL_COPY = {
  displayNameOptional: { zh: "账号显示名（可选）", en: "Display name (optional)" },
  displayName: { zh: "账号显示名", en: "Display name" },
};

type FieldProps = {
  testId: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  secret?: boolean;
  type?: string;
  hint?: string;
};

function Field({
  testId,
  label,
  value,
  onChange,
  placeholder,
  secret,
  type = "text",
  hint,
}: FieldProps) {
  return (
    <label className="block">
      <span className="erp-label">{label}</span>
      <input
        data-testid={testId}
        type={secret ? "password" : type}
        autoComplete={secret ? "off" : undefined}
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
        className="erp-input mt-1 rounded-xl"
      />
      {hint && (
        <span className="mt-1 block text-[11px] text-slate-500 dark:text-slate-400">{hint}</span>
      )}
    </label>
  );
}

export type ChannelConnectDrawerProps = {
  channelKey: ExtendedMarketplaceChannelKey | null;
  accounts: ChannelAccountApiItem[];
  isAdmin: boolean;
  currentUserId?: string | null;
  members: TeamMemberApiItem[];
  tenantSlug?: string | null;
  onSubmit: (request: MarketplaceConnectRequest) => Promise<MessagingConnectResult>;
  onConnected?: () => void;
  onClose: () => void;
  onViewAccounts: () => void;
};

/**
 * 480px 右侧快捷绑定抽屉：所有渠道表单都收进这里，主页面保持干净的卡片墙。
 * ESC 可关闭，背景遮罩半透明；2K/4K 屏幕上也不会被拉宽。
 */
export function ChannelConnectDrawer(props: ChannelConnectDrawerProps) {
  const { channelKey, onClose } = props;
  const { bi } = useI18n();

  useEffect(() => {
    if (!channelKey) return;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [channelKey, onClose]);

  if (!channelKey) return null;
  const item = marketplaceChannel(channelKey);
  const Icon = item.icon;

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <button
        type="button"
        aria-label={bi("关闭快捷绑定", "Close quick connect")}
        data-testid="channel-drawer-backdrop"
        onClick={onClose}
        className="absolute inset-0 cursor-default bg-slate-950/35 backdrop-blur-[1px] dark:bg-slate-950/60"
      />
      <aside
        role="dialog"
        aria-modal="true"
        aria-labelledby="channel-drawer-title"
        data-testid={`channel-connect-drawer-${channelKey}`}
        className="relative flex h-full w-full max-w-[480px] flex-col border-l border-slate-200 bg-slate-50 shadow-[-24px_0_60px_-24px_rgba(15,23,42,0.4)] dark:border-slate-800 dark:bg-slate-950"
      >
        <header className="flex items-start justify-between gap-2 border-b border-slate-200 bg-white px-5 py-4 dark:border-slate-800 dark:bg-slate-900">
          <div className="flex min-w-0 items-start gap-3">
            <span
              aria-hidden="true"
              className={`grid h-10 w-10 shrink-0 place-items-center rounded-xl ring-1 ${item.tile}`}
            >
              <Icon className="h-[18px] w-[18px]" />
            </span>
            <div className="min-w-0">
              <h2
                id="channel-drawer-title"
                className="truncate text-[15px] font-bold text-slate-950 dark:text-slate-100"
              >
                {bi(`快捷绑定 ${item.titleZh}`, `Quick connect ${item.titleEn}`)}
              </h2>
              <p className="mt-0.5 text-[11.5px] leading-5 text-slate-600 dark:text-slate-400">
                {bi(item.descriptionZh, item.descriptionEn)}
              </p>
            </div>
          </div>
          <button
            type="button"
            data-testid="channel-drawer-close"
            onClick={onClose}
            className="grid h-7 w-7 shrink-0 place-items-center rounded-lg border border-slate-200 bg-white text-slate-500 transition-colors hover:bg-slate-100 hover:text-slate-950 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-400 dark:hover:bg-slate-800 dark:hover:text-slate-100"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </header>

        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-5 py-4">
          <div className="flex items-center gap-1.5 rounded-xl border border-emerald-200/80 bg-emerald-50/70 px-2.5 py-1.5 text-[11px] font-semibold text-emerald-800 dark:border-emerald-800/70 dark:bg-emerald-950/40 dark:text-emerald-300">
            <ShieldCheck className="h-3 w-3" />
            {bi("令牌信封加密 · 原始报文验签", "Envelope-encrypted tokens · signed raw bodies")}
          </div>
          <ChannelConnectForm key={channelKey} {...props} channelKey={channelKey} />
        </div>
      </aside>
    </div>
  );
}

type ChannelConnectFormProps = ChannelConnectDrawerProps & {
  channelKey: ExtendedMarketplaceChannelKey;
};

function ChannelConnectForm({
  channelKey,
  accounts,
  isAdmin,
  currentUserId,
  members,
  tenantSlug,
  onSubmit,
  onConnected,
  onClose,
  onViewAccounts,
}: ChannelConnectFormProps) {
  const { bi } = useI18n();
  const item = marketplaceChannel(channelKey);
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [waMode, setWaMode] = useState<WhatsAppConnectMode>("direct");
  const [emailProvider, setEmailProvider] = useState<EmailProvider>("resend");
  const [isCoexistence, setIsCoexistence] = useState(true);
  const [scope, setScope] = useState<ChannelVisibilityScope>(isAdmin ? "shared" : "personal");
  const [ownerId, setOwnerId] = useState(isAdmin ? "" : currentUserId ?? "");
  const [guideOpen, setGuideOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<MessagingConnectResult | null>(null);
  const [copied, setCopied] = useState(false);

  const connectedCount = accounts.filter((account) =>
    item.channelTypes.includes(account.channel_type),
  ).length;

  const ownership = useMemo(() => {
    if (!isAdmin) {
      return {
        visibility_scope: "personal" as const,
        owner_user_id: currentUserId ?? null,
      };
    }
    return scope === "personal"
      ? { visibility_scope: "personal" as const, owner_user_id: ownerId || null }
      : { visibility_scope: "shared" as const };
  }, [currentUserId, isAdmin, ownerId, scope]);

  const ownershipReady = isAdmin ? scope === "shared" || Boolean(ownerId) : true;
  const isSourcingChannel = isSourcingMarketplaceKey(channelKey);
  const showsOwnership =
    !isSourcingChannel &&
    (channelKey === "telegram" ||
    channelKey === "whatsapp" ||
    channelKey === "email" ||
    channelKey === "messenger" ||
      channelKey === "instagram_dm" ||
      channelKey === "web_livechat");

  function update<K extends keyof Draft>(field: K, value: string) {
    setDraft((current) => ({
      ...current,
      [field]: field === "bot_token" ? extractTelegramBotToken(value) : value,
    }));
    setResult(null);
    setError(null);
  }

  const extractedToken = channelKey === "telegram" ? extractTelegramBotToken(draft.bot_token) : "";
  const detectedBotId = telegramBotIdOf(extractedToken);

  const formValid = useMemo(() => {
    switch (channelKey) {
      case "telegram":
        return isMessagingChannelDraftValid("telegram", { bot_token: draft.bot_token });
      case "messenger":
        return (
          META_ID.test(trimmed(draft.page_id)) &&
          Boolean(trimmed(draft.page_access_token)) &&
          Boolean(trimmed(draft.display_name))
        );
      case "instagram_dm":
        return (
          META_ID.test(trimmed(draft.ig_account_id)) &&
          Boolean(trimmed(draft.access_token)) &&
          Boolean(trimmed(draft.display_name))
        );
      case "email":
        return isEmailConnectDraftValid(emailProvider, draft);
      case "whatsapp":
        return waMode === "embedded"
          ? Boolean(trimmed(draft.code)) &&
              Boolean(trimmed(draft.waba_id)) &&
              Boolean(trimmed(draft.phone_number_id)) &&
              Boolean(trimmed(draft.display_name))
          : Boolean(trimmed(draft.waba_id)) &&
              Boolean(trimmed(draft.phone_number_id)) &&
              Boolean(trimmed(draft.access_token));
      case "web_livechat":
        return DOMAIN.test(trimmed(draft.site_domain));
      case "shopify":
        return (
          DOMAIN.test(trimmed(draft.shop_domain)) &&
          Boolean(trimmed(draft.client_id)) &&
          Boolean(trimmed(draft.client_secret))
        );
      case "woocommerce":
        return (
          DOMAIN.test(trimmed(draft.store_url)) &&
          Boolean(trimmed(draft.consumer_key)) &&
          Boolean(trimmed(draft.consumer_secret))
        );
      case "shopline":
        return (
          DOMAIN.test(trimmed(draft.store_domain)) &&
          Boolean(trimmed(draft.access_token)) &&
          Boolean(trimmed(draft.app_secret))
        );
      case "shoplazza":
        return (
          DOMAIN.test(trimmed(draft.store_domain)) &&
          Boolean(trimmed(draft.access_token)) &&
          Boolean(trimmed(draft.client_secret))
        );
      case "tiktok_shop":
        return (
          Boolean(trimmed(draft.shop_id)) &&
          Boolean(trimmed(draft.shop_cipher)) &&
          Boolean(trimmed(draft.app_key)) &&
          Boolean(trimmed(draft.app_secret)) &&
          Boolean(trimmed(draft.access_token))
        );
      case "alibaba_intl":
        return (
          Boolean(trimmed(draft.app_key)) &&
          Boolean(trimmed(draft.app_secret)) &&
          Boolean(trimmed(draft.store_id))
        );
      case "amazon":
        return (
          Boolean(trimmed(draft.seller_id)) &&
          Boolean(trimmed(draft.lwa_client_id)) &&
          Boolean(trimmed(draft.lwa_client_secret)) &&
          Boolean(trimmed(draft.refresh_token))
        );
      case "ebay":
        return (
          Boolean(trimmed(draft.seller_username)) &&
          Boolean(trimmed(draft.app_id)) &&
          Boolean(trimmed(draft.cert_id)) &&
          Boolean(trimmed(draft.refresh_token))
        );
      default:
        return false;
    }
  }, [channelKey, draft, emailProvider, waMode]);

  const valid = formValid && ownershipReady;

  function buildRequest(key: MarketplaceChannelKey): MarketplaceConnectRequest {
    const displayName = trimmed(draft.display_name);
    switch (key) {
      case "telegram": {
        const body: TelegramConnectPayload = {
          bot_token: trimmed(draft.bot_token),
          ...(displayName ? { display_name: displayName } : {}),
          ...ownership,
        };
        return { channel: "telegram", body };
      }
      case "messenger": {
        const body: MessengerConnectPayload = {
          page_id: trimmed(draft.page_id),
          page_access_token: trimmed(draft.page_access_token),
          display_name: displayName,
          ...ownership,
        };
        return { channel: "messenger", body };
      }
      case "instagram_dm": {
        const body: InstagramConnectPayload = {
          ig_account_id: trimmed(draft.ig_account_id),
          access_token: trimmed(draft.access_token),
          display_name: displayName,
          ...ownership,
        };
        return { channel: "instagram_dm", body };
      }
      case "email": {
        const portOf = (value: string): number | null => {
          const parsed = Number(value);
          return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
        };
        const body: EmailConnectPayload = {
          email_address: trimmed(draft.email_address),
          display_name: displayName || null,
          provider: emailProvider,
          ...ownership,
          ...(emailProvider === "resend"
            ? { api_key: trimmed(draft.api_key) }
            : {
                smtp_host: trimmed(draft.smtp_host),
                smtp_port: portOf(draft.smtp_port),
                smtp_username: trimmed(draft.smtp_username),
                smtp_password: trimmed(draft.smtp_password),
                imap_host: trimmed(draft.imap_host) || null,
                imap_port: portOf(draft.imap_port),
              }),
        };
        return { channel: "email", body };
      }
      case "whatsapp": {
        if (waMode === "embedded") {
          const body: WhatsAppEmbeddedSignupPayload = {
            code: trimmed(draft.code),
            waba_id: trimmed(draft.waba_id),
            phone_number_id: trimmed(draft.phone_number_id),
            is_coexistence: isCoexistence,
            display_name: displayName,
            ...ownership,
          };
          return { channel: "whatsapp", mode: "embedded", body };
        }
        const body: WhatsAppDirectConnectPayload = {
          waba_id: trimmed(draft.waba_id),
          phone_number_id: trimmed(draft.phone_number_id),
          access_token: trimmed(draft.access_token),
          is_coexistence: isCoexistence,
          ...(displayName ? { display_name: displayName } : {}),
          ...ownership,
        };
        return { channel: "whatsapp", mode: "direct", body };
      }
      case "web_livechat": {
        const body: WebLivechatConnectPayload = {
          site_domain: trimmed(draft.site_domain),
          display_name: displayName || null,
          ...ownership,
        };
        return { channel: "web_livechat", body };
      }
      case "shopify": {
        const body: ShopifyCustomAppConnectPayload = {
          shop_domain: trimmed(draft.shop_domain),
          client_id: trimmed(draft.client_id),
          client_secret: trimmed(draft.client_secret),
          ...(trimmed(draft.admin_access_token)
            ? { access_token: trimmed(draft.admin_access_token) }
            : {}),
        };
        return { channel: "shopify", body };
      }
      case "woocommerce": {
        const body: WooCommerceConnectPayload = {
          store_url: normalizeWooCommerceStoreUrl(draft.store_url),
          consumer_key: trimmed(draft.consumer_key),
          consumer_secret: trimmed(draft.consumer_secret),
          ...(trimmed(draft.webhook_secret)
            ? { webhook_secret: trimmed(draft.webhook_secret) }
            : {}),
        };
        return { channel: "woocommerce", body };
      }
      case "shopline": {
        const body: ShoplineConnectPayload = {
          store_domain: trimmed(draft.store_domain),
          access_token: trimmed(draft.access_token),
          app_secret: trimmed(draft.app_secret),
        };
        return { channel: "shopline", body };
      }
      case "shoplazza": {
        const body: ShoplazzaConnectPayload = {
          store_domain: trimmed(draft.store_domain),
          access_token: trimmed(draft.access_token),
          client_secret: trimmed(draft.client_secret),
        };
        return { channel: "shoplazza", body };
      }
      case "tiktok_shop": {
        const body: TikTokShopConnectPayload = {
          shop_id: trimmed(draft.shop_id),
          shop_cipher: trimmed(draft.shop_cipher),
          app_key: trimmed(draft.app_key),
          app_secret: trimmed(draft.app_secret),
          access_token: trimmed(draft.access_token),
          app_mode: "seller_byok",
        };
        return { channel: "tiktok_shop", body };
      }
    }
  }

  /**
   * Pack F 三个外贸渠道走各自的后端接入端点（密钥字段原样提交、绝不回显）。
   * 其余 11 个渠道仍由 `onSubmit`（connectMarketplaceChannel）统一分发。
   */
  function submitSourcingChannel(key: SourcingChannelKey): Promise<MessagingConnectResult> {
    const displayName = trimmed(draft.display_name) || item.titleEn;
    switch (key) {
      case "alibaba_intl":
        return connectAlibabaIntl({
          app_key: trimmed(draft.app_key),
          app_secret: trimmed(draft.app_secret),
          display_name: displayName,
          store_id: trimmed(draft.store_id),
          ...(trimmed(draft.inquiry_email)
            ? { inquiry_email: trimmed(draft.inquiry_email) }
            : {}),
          visibility_scope: scope === "shared" ? "shared" : "private",
        });
      case "amazon":
        return connectAmazon({
          display_name: displayName,
          seller_id: trimmed(draft.seller_id),
          marketplace_id: trimmed(draft.marketplace_id) || "ATVPDKIKX0DER",
          lwa_client_id: trimmed(draft.lwa_client_id),
          lwa_client_secret: trimmed(draft.lwa_client_secret),
          refresh_token: trimmed(draft.refresh_token),
        });
      case "ebay":
        return connectEbay({
          display_name: displayName,
          seller_username: trimmed(draft.seller_username),
          site_id: trimmed(draft.site_id) || "EBAY-US",
          app_id: trimmed(draft.app_id),
          cert_id: trimmed(draft.cert_id),
          refresh_token: trimmed(draft.refresh_token),
        });
    }
  }

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!valid || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const connected = isSourcingMarketplaceKey(channelKey)
        ? await submitSourcingChannel(channelKey)
        : await onSubmit(buildRequest(channelKey));
      setResult(connected);
      onConnected?.();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "ERR_CHANNEL_CONNECTION_FAILED");
    } finally {
      setSubmitting(false);
    }
  }

  const embedSnippet = livechatEmbedSnippet(
    result?.external_account_id ?? "",
    tenantSlug,
  );

  async function copyEmbedSnippet() {
    try {
      await navigator.clipboard?.writeText(embedSnippet);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }

  const guide = GUIDES[channelKey];
  const displayNameTestId =
    channelKey === "instagram_dm"
      ? "instagram-display-name-input"
      : channelKey === "web_livechat"
        ? "web-livechat-display-name-input"
        : `${channelKey}-display-name-input`;

  const displayNameField = (
    <Field
      testId={displayNameTestId}
      label={bi(FIELD_LABEL_COPY.displayNameOptional.zh, FIELD_LABEL_COPY.displayNameOptional.en)}
      value={draft.display_name}
      onChange={(value) => update("display_name", value)}
      placeholder={bi("留空自动使用渠道默认名称", "Leave blank to use the channel default")}
    />
  );

  return (
    <form
      data-testid={`connect-form-${channelKey}`}
      onSubmit={submit}
      className="flex flex-col space-y-3"
    >
      <div className="rounded-xl border border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900">
        <button
          type="button"
          data-testid="connect-guide-toggle"
          aria-expanded={guideOpen}
          onClick={() => setGuideOpen((prev) => !prev)}
          className="flex w-full items-center justify-between gap-2 px-3 py-2 text-[11.5px] font-semibold text-slate-700 dark:text-slate-300"
        >
          <span className="inline-flex items-center gap-1.5">
            <KeyRound className="h-3 w-3 text-emerald-700" />
            {bi("3 步完成接入（中文指引）", "3-step setup guide")}
          </span>
          <ChevronDown
            className={`h-3.5 w-3.5 transition-transform ${guideOpen ? "rotate-180" : ""}`}
          />
        </button>
        {guideOpen && (
          <ol
            data-testid="connect-guide-steps"
            className="space-y-1 border-t border-slate-200 px-3 py-2 text-[11px] leading-5 text-slate-600 dark:border-slate-800 dark:text-slate-400"
          >
            {guide.map((step, index) => (
              <li key={step.en}>
                <strong className="text-slate-900 dark:text-slate-100">{index + 1}.</strong>{" "}
                {bi(step.zh, step.en)}
              </li>
            ))}
          </ol>
        )}
      </div>

      {channelKey === "telegram" && (
        <>
          <Field
            testId="telegram-bot-token-input"
            label={bi("Bot 令牌（整段粘贴也行）", "Bot token (paste the whole reply)")}
            value={draft.bot_token}
            onChange={(value) => update("bot_token", value)}
            placeholder="123456789:AAH..."
            secret
          />
          {detectedBotId && (
            <span
              data-testid="telegram-detected-bot-id"
              className="inline-flex items-center gap-1.5 self-start rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[10.5px] font-semibold text-emerald-800"
            >
              <Sparkles className="h-3 w-3" />
              {bi(`已自动识别 Bot ID: ${detectedBotId}`, `Bot ID detected: ${detectedBotId}`)}
            </span>
          )}
          {displayNameField}
        </>
      )}

      {channelKey === "whatsapp" && (
        <>
          <div className="inline-flex items-center gap-0.5 self-start rounded-lg border border-slate-200 bg-slate-100/90 p-0.5 dark:border-slate-700 dark:bg-slate-800/70">
            <button
              type="button"
              data-testid="whatsapp-mode-direct"
              aria-pressed={waMode === "direct"}
              onClick={() => setWaMode("direct")}
              className={`rounded-md px-2 py-0.5 text-[11px] font-semibold transition-colors ${
                waMode === "direct"
                  ? "bg-blue-600 text-white shadow-xs"
                  : "text-slate-600 dark:text-slate-400"
              }`}
            >
              {bi("直连 Token 快捷绑定", "Direct token")}
            </button>
            <button
              type="button"
              data-testid="whatsapp-mode-embedded"
              aria-pressed={waMode === "embedded"}
              onClick={() => setWaMode("embedded")}
              className={`rounded-md px-2 py-0.5 text-[11px] font-semibold transition-colors ${
                waMode === "embedded"
                  ? "bg-blue-600 text-white shadow-xs"
                  : "text-slate-600 dark:text-slate-400"
              }`}
            >
              {bi("Meta Embedded Signup 授权码", "Embedded Signup")}
            </button>
          </div>
          <Field
            testId="whatsapp-waba-id-input"
            label="WABA ID"
            value={draft.waba_id}
            onChange={(value) => update("waba_id", value)}
            placeholder="102290129340398"
          />
          <Field
            testId="whatsapp-phone-number-id-input"
            label={bi("手机号 ID", "Phone number ID")}
            value={draft.phone_number_id}
            onChange={(value) => update("phone_number_id", value)}
            placeholder="106540352242922"
          />
          {waMode === "embedded" ? (
            <Field
              testId="whatsapp-code-input"
              label={bi("Embedded Signup 授权码", "Embedded Signup code")}
              value={draft.code}
              onChange={(value) => update("code", value)}
              secret
            />
          ) : (
            <Field
              testId="whatsapp-access-token-input"
              label={bi("永久访问令牌", "Permanent access token")}
              value={draft.access_token}
              onChange={(value) => update("access_token", value)}
              secret
            />
          )}
          {waMode === "embedded" ? (
            <Field
              testId="whatsapp-display-name-input"
              label={bi(FIELD_LABEL_COPY.displayName.zh, FIELD_LABEL_COPY.displayName.en)}
              value={draft.display_name}
              onChange={(value) => update("display_name", value)}
            />
          ) : (
            displayNameField
          )}
          <label
            className={`flex items-start gap-2 rounded-xl border px-2.5 py-2 text-[11.5px] ${
              isAdmin
                ? "border-slate-200 bg-white text-slate-700 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300"
                : "border-emerald-200 bg-emerald-50/70 text-emerald-900"
            }`}
          >
            <input
              type="checkbox"
              data-testid="whatsapp-coexistence-toggle"
              checked={isCoexistence}
              disabled={!isAdmin}
              onChange={(event) => setIsCoexistence(event.target.checked)}
              className="mt-0.5 h-3.5 w-3.5 rounded border-slate-300 text-blue-600 disabled:opacity-70 dark:border-slate-600"
            />
            <span>
              <strong className="font-semibold">
                {bi("手机 WhatsApp App 共存模式", "Phone app coexistence")}
              </strong>
              <span className="text-slate-500 dark:text-slate-400"> · </span>
              {bi("限速 20 mps", "20 mps limit")}
              {!isAdmin && (
                <span className="ml-1 inline-flex items-center gap-1 rounded-full bg-emerald-600/10 px-1.5 py-0.5 text-[10px] font-semibold text-emerald-800">
                  <LockKeyhole className="h-2.5 w-2.5" />
                  {bi("已锁定", "Locked")}
                </span>
              )}
            </span>
          </label>
        </>
      )}

      {channelKey === "email" && (
        <>
          <Field
            testId="email-address-input"
            label={bi("邮箱地址", "Email address")}
            value={draft.email_address}
            onChange={(value) => update("email_address", value)}
            placeholder="sales@company.com"
            type="email"
          />
          <div className="inline-flex items-center gap-0.5 self-start rounded-lg border border-slate-200 bg-slate-100/90 p-0.5 dark:border-slate-700 dark:bg-slate-800/70">
            <button
              type="button"
              data-testid="email-provider-resend"
              aria-pressed={emailProvider === "resend"}
              onClick={() => setEmailProvider("resend")}
              className={`rounded-md px-2 py-0.5 text-[11px] font-semibold transition-colors ${
                emailProvider === "resend"
                  ? "bg-blue-600 text-white shadow-xs"
                  : "text-slate-600 dark:text-slate-400"
              }`}
            >
              Resend API
            </button>
            <button
              type="button"
              data-testid="email-provider-smtp_imap"
              aria-pressed={emailProvider === "smtp_imap"}
              onClick={() => setEmailProvider("smtp_imap")}
              className={`rounded-md px-2 py-0.5 text-[11px] font-semibold transition-colors ${
                emailProvider === "smtp_imap"
                  ? "bg-blue-600 text-white shadow-xs"
                  : "text-slate-600 dark:text-slate-400"
              }`}
            >
              SMTP / IMAP
            </button>
          </div>
          {emailProvider === "resend" ? (
            <Field
              testId="email-api-key-input"
              label="Resend API Key"
              value={draft.api_key}
              onChange={(value) => update("api_key", value)}
              placeholder="re_..."
              secret
            />
          ) : (
            <div className="space-y-2">
              <div className="grid grid-cols-[1fr_88px] gap-2">
                <Field
                  testId="email-smtp-host-input"
                  label="SMTP Host"
                  value={draft.smtp_host}
                  onChange={(value) => update("smtp_host", value)}
                  placeholder="smtp.qiye.aliyun.com"
                />
                <Field
                  testId="email-smtp-port-input"
                  label={bi("端口", "Port")}
                  value={draft.smtp_port}
                  onChange={(value) => update("smtp_port", value)}
                />
              </div>
              <Field
                testId="email-smtp-username-input"
                label={bi("SMTP 用户名", "SMTP username")}
                value={draft.smtp_username}
                onChange={(value) => update("smtp_username", value)}
                placeholder="sales@company.com"
              />
              <Field
                testId="email-smtp-password-input"
                label={bi("SMTP 密码 / 授权码", "SMTP password")}
                value={draft.smtp_password}
                onChange={(value) => update("smtp_password", value)}
                secret
              />
            </div>
          )}
          {displayNameField}
          <p className="rounded-xl border border-slate-200 bg-white px-2.5 py-2 text-[11px] leading-5 text-slate-600 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-400">
            {bi(
              "阿里国际站 / 中国制造网询盘可转发到该邮箱，绑定后自动归集进 Inbox。",
              "Alibaba / Made-in-China inquiries can be forwarded to this mailbox and land in the inbox."
            )}
          </p>
        </>
      )}

      {channelKey === "messenger" && (
        <>
          <Field
            testId="messenger-page-id-input"
            label={bi("主页 ID", "Page ID")}
            value={draft.page_id}
            onChange={(value) => update("page_id", value)}
            placeholder="1010101010101010"
          />
          <Field
            testId="messenger-access-token-input"
            label={bi("主页访问令牌", "Page access token")}
            value={draft.page_access_token}
            onChange={(value) => update("page_access_token", value)}
            secret
          />
          <Field
            testId="messenger-display-name-input"
            label={bi(FIELD_LABEL_COPY.displayName.zh, FIELD_LABEL_COPY.displayName.en)}
            value={draft.display_name}
            onChange={(value) => update("display_name", value)}
          />
        </>
      )}

      {channelKey === "instagram_dm" && (
        <>
          <Field
            testId="instagram-account-id-input"
            label={bi("Instagram 账号 ID", "Instagram account ID")}
            value={draft.ig_account_id}
            onChange={(value) => update("ig_account_id", value)}
            placeholder="17841400000000000"
          />
          <Field
            testId="instagram-access-token-input"
            label={bi("访问令牌", "Access token")}
            value={draft.access_token}
            onChange={(value) => update("access_token", value)}
            secret
          />
          <Field
            testId="instagram-display-name-input"
            label={bi(FIELD_LABEL_COPY.displayName.zh, FIELD_LABEL_COPY.displayName.en)}
            value={draft.display_name}
            onChange={(value) => update("display_name", value)}
          />
        </>
      )}

      {channelKey === "web_livechat" && (
        <>
          <Field
            testId="web-livechat-site-domain-input"
            label={bi("官网站点域名", "Site domain")}
            value={draft.site_domain}
            onChange={(value) => update("site_domain", value)}
            placeholder="https://shop.example.com"
          />
          {displayNameField}
        </>
      )}

      {channelKey === "shopify" && (
        <>
          <Field
            testId="shopify-shop-domain-input"
            label={bi("店铺域名", "Shop domain")}
            value={draft.shop_domain}
            onChange={(value) => update("shop_domain", value)}
            placeholder="qzled.myshopify.com"
          />
          <Field
            testId="shopify-client-id-input"
            label="Client ID"
            value={draft.client_id}
            onChange={(value) => update("client_id", value)}
          />
          <Field
            testId="shopify-client-secret-input"
            label="Client Secret"
            value={draft.client_secret}
            onChange={(value) => update("client_secret", value)}
            secret
          />
          <Field
            testId="shopify-access-token-input"
            label={bi("Admin API Token（可选）", "Admin API token (optional)")}
            value={draft.admin_access_token}
            onChange={(value) => update("admin_access_token", value)}
            secret
          />
        </>
      )}

      {channelKey === "woocommerce" && (
        <>
          <Field
            testId="woocommerce-store-url-input"
            label={bi("店铺地址", "Store URL")}
            value={draft.store_url}
            onChange={(value) => update("store_url", value)}
            placeholder="https://shop.example.com"
          />
          <Field
            testId="woocommerce-consumer-key-input"
            label="Consumer Key"
            value={draft.consumer_key}
            onChange={(value) => update("consumer_key", value)}
            placeholder="ck_..."
          />
          <Field
            testId="woocommerce-consumer-secret-input"
            label="Consumer Secret"
            value={draft.consumer_secret}
            onChange={(value) => update("consumer_secret", value)}
            placeholder="cs_..."
            secret
          />
          <Field
            testId="woocommerce-webhook-secret-input"
            label={bi("Webhook Secret（可选）", "Webhook secret (optional)")}
            value={draft.webhook_secret}
            onChange={(value) => update("webhook_secret", value)}
            secret
          />
        </>
      )}

      {channelKey === "shopline" && (
        <>
          <Field
            testId="shopline-store-domain-input"
            label={bi("店铺域名", "Store domain")}
            value={draft.store_domain}
            onChange={(value) => update("store_domain", value)}
            placeholder="qzled.myshopline.com"
          />
          <Field
            testId="shopline-access-token-input"
            label="Access Token"
            value={draft.access_token}
            onChange={(value) => update("access_token", value)}
            secret
          />
          <Field
            testId="shopline-app-secret-input"
            label="App Secret"
            value={draft.app_secret}
            onChange={(value) => update("app_secret", value)}
            secret
          />
        </>
      )}

      {channelKey === "shoplazza" && (
        <>
          <Field
            testId="shoplazza-store-domain-input"
            label={bi("店铺域名", "Store domain")}
            value={draft.store_domain}
            onChange={(value) => update("store_domain", value)}
            placeholder="qzled.myshoplazza.com"
          />
          <Field
            testId="shoplazza-access-token-input"
            label="Access Token"
            value={draft.access_token}
            onChange={(value) => update("access_token", value)}
            secret
          />
          <Field
            testId="shoplazza-client-secret-input"
            label="Client Secret"
            value={draft.client_secret}
            onChange={(value) => update("client_secret", value)}
            secret
          />
        </>
      )}

      {channelKey === "tiktok_shop" && (
        <>
          <Field
            testId="tiktok-shop-id-input"
            label="Shop ID"
            value={draft.shop_id}
            onChange={(value) => update("shop_id", value)}
            placeholder="7480000000000000000"
          />
          <Field
            testId="tiktok-shop-cipher-input"
            label="Shop Cipher"
            value={draft.shop_cipher}
            onChange={(value) => update("shop_cipher", value)}
            secret
          />
          <Field
            testId="tiktok-app-key-input"
            label="App Key"
            value={draft.app_key}
            onChange={(value) => update("app_key", value)}
          />
          <Field
            testId="tiktok-app-secret-input"
            label="App Secret"
            value={draft.app_secret}
            onChange={(value) => update("app_secret", value)}
            secret
          />
          <Field
            testId="tiktok-access-token-input"
            label="Access Token"
            value={draft.access_token}
            onChange={(value) => update("access_token", value)}
            secret
          />
          <p className="rounded-xl border border-slate-200 bg-white px-2.5 py-2 text-[11px] leading-5 text-slate-600 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-400">
            {bi("接入模式：卖家自备应用（seller_byok）", "Mode: seller BYOK (seller_byok)")}
          </p>
        </>
      )}

      {channelKey === "alibaba_intl" && (
        <>
          <Field
            testId="alibaba-app-key-input"
            label="App Key"
            value={draft.app_key}
            onChange={(value) => update("app_key", value)}
            placeholder="1234567"
          />
          <Field
            testId="alibaba-app-secret-input"
            label="App Secret"
            value={draft.app_secret}
            onChange={(value) => update("app_secret", value)}
            placeholder={bi("提交后不再回显", "Never echoed back after submit")}
            secret
          />
          <Field
            testId="alibaba-store-id-input"
            label={bi("国际站店铺 ID", "Store ID")}
            value={draft.store_id}
            onChange={(value) => update("store_id", value)}
            placeholder="qzled"
          />
          <Field
            testId="alibaba-inquiry-email-input"
            label={bi("询盘接收邮箱（可选）", "Inquiry mailbox (optional)")}
            value={draft.inquiry_email}
            onChange={(value) => update("inquiry_email", value)}
            placeholder="sales@company.com"
            type="email"
            hint={bi(
              "留空则沿用该账号的工作邮箱，询盘不会丢。",
              "Leave blank to reuse the account mailbox — inquiries are never dropped."
            )}
          />
          {displayNameField}
          <div className="inline-flex items-center gap-0.5 self-start rounded-lg border border-slate-200 bg-slate-100/90 p-0.5 dark:border-slate-700 dark:bg-slate-800/70">
            <button
              type="button"
              data-testid="alibaba-scope-shared"
              aria-pressed={scope === "shared"}
              onClick={() => setScope("shared")}
              className={`rounded-md px-2 py-0.5 text-[11px] font-semibold transition-colors ${
                scope === "shared"
                  ? "bg-blue-600 text-white shadow-xs"
                  : "text-slate-600 dark:text-slate-400"
              }`}
            >
              {bi("公司公海（team shared）", "Company shared")}
            </button>
            <button
              type="button"
              data-testid="alibaba-scope-private"
              aria-pressed={scope !== "shared"}
              onClick={() => setScope("personal")}
              className={`rounded-md px-2 py-0.5 text-[11px] font-semibold transition-colors ${
                scope !== "shared"
                  ? "bg-blue-600 text-white shadow-xs"
                  : "text-slate-600 dark:text-slate-400"
              }`}
            >
              {bi("专属（private）", "Private")}
            </button>
          </div>
        </>
      )}

      {channelKey === "amazon" && (
        <>
          <Field
            testId="amazon-seller-id-input"
            label="Seller ID"
            value={draft.seller_id}
            onChange={(value) => update("seller_id", value)}
            placeholder="A2EUQ1WTGCTBG2"
          />
          <Field
            testId="amazon-marketplace-id-input"
            label="Marketplace ID"
            value={draft.marketplace_id}
            onChange={(value) => update("marketplace_id", value)}
            placeholder="ATVPDKIKX0DER"
            hint={bi("美国站 ATVPDKIKX0DER，欧洲站 A1PA6795UKMFR9。", "US: ATVPDKIKX0DER · EU: A1PA6795UKMFR9.")}
          />
          <Field
            testId="amazon-lwa-client-id"
            label="LWA Client ID"
            value={draft.lwa_client_id}
            onChange={(value) => update("lwa_client_id", value)}
            placeholder="amzn1.application-oa2-client..."
          />
          <Field
            testId="amazon-lwa-client-secret"
            label="LWA Client Secret"
            value={draft.lwa_client_secret}
            onChange={(value) => update("lwa_client_secret", value)}
            placeholder={bi("提交后不再回显", "Never echoed back after submit")}
            secret
          />
          <Field
            testId="amazon-refresh-token"
            label="Refresh Token"
            value={draft.refresh_token}
            onChange={(value) => update("refresh_token", value)}
            placeholder="Atzr|IwEB..."
            secret
          />
          {displayNameField}
        </>
      )}

      {channelKey === "ebay" && (
        <>
          <Field
            testId="ebay-seller-username-input"
            label={bi("卖家用户名", "Seller username")}
            value={draft.seller_username}
            onChange={(value) => update("seller_username", value)}
            placeholder="qzled_official"
          />
          <Field
            testId="ebay-site-id-input"
            label="Site ID"
            value={draft.site_id}
            onChange={(value) => update("site_id", value)}
            placeholder="EBAY-US"
            hint={bi("美国 EBAY-US，英国 EBAY-GB，德国 EBAY-DE。", "US: EBAY-US · UK: EBAY-GB · DE: EBAY-DE.")}
          />
          <Field
            testId="ebay-app-id-input"
            label="App ID（Client ID）"
            value={draft.app_id}
            onChange={(value) => update("app_id", value)}
            placeholder="QzLedERP-App-PRD-..."
          />
          <Field
            testId="ebay-cert-id-input"
            label="Cert ID（Client Secret）"
            value={draft.cert_id}
            onChange={(value) => update("cert_id", value)}
            placeholder={bi("提交后不再回显", "Never echoed back after submit")}
            secret
          />
          <Field
            testId="ebay-refresh-token"
            label="Refresh Token"
            value={draft.refresh_token}
            onChange={(value) => update("refresh_token", value)}
            placeholder="v^1.1#i^1#..."
            secret
          />
          {displayNameField}
        </>
      )}

      {showsOwnership &&
        (isAdmin ? (
          <OwnershipSelector
            kind={channelKey}
            scope={scope}
            ownerId={ownerId}
            members={members}
            currentUserId={currentUserId}
            onChangeScope={setScope}
            onChangeOwner={setOwnerId}
          />
        ) : (
          <div
            data-testid={`ownership-locked-${channelKey}`}
            className="inline-flex items-center gap-1.5 self-start rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[10.5px] font-semibold text-emerald-800"
          >
            <LockKeyhole className="h-3 w-3" />
            {bi("个人专属（仅自己与老板/主管可见）", "Personal (visible to you and your managers)")}
          </div>
        ))}

      <div className="flex items-center justify-between gap-2 pt-1">
        <span className="text-[11px] font-medium text-slate-500 dark:text-slate-400">
          {connectedCount > 0
            ? bi(`当前已接入 ${connectedCount} 个账号`, `${connectedCount} account(s) connected`)
            : bi("尚未接入该渠道", "No account yet")}
        </span>
        <button
          type="submit"
          data-testid={`connect-${channelKey}-btn`}
          disabled={!valid || submitting}
          className="erp-btn rounded-xl bg-blue-600 px-4 py-2 font-semibold text-white shadow-xs transition-colors hover:bg-blue-700 disabled:bg-slate-300 disabled:shadow-none dark:bg-blue-600 dark:hover:bg-blue-500 dark:disabled:bg-slate-700"
        >
          {submitting ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <CheckCircle2 className="h-3.5 w-3.5" />
          )}
          <span>
            {submitting
              ? bi("连接中…", "Connecting…")
              : bi(`一键连接 ${item.titleZh}`, `Connect ${item.titleEn}`)}
          </span>
        </button>
      </div>

      {error && (
        <div
          role="alert"
          data-testid="channel-connect-error"
          className="rounded-xl border border-rose-200 bg-rose-50 px-2.5 py-1.5 text-[11px] font-medium text-rose-700"
        >
          {error}
        </div>
      )}

      {result && (
        <div
          data-testid={`connect-success-${channelKey}`}
          className="space-y-2 rounded-2xl border border-emerald-200 bg-emerald-50 px-3 py-3 text-[11.5px] font-medium text-emerald-900"
        >
          <div className="flex flex-wrap items-center gap-1.5">
            <CheckCircle2 className="h-3.5 w-3.5" />
            <span className="font-semibold">{bi("接入成功", "Connected")}</span>
            <span className="text-emerald-700">{result.external_account_id}</span>
            {result.visibility_scope && (
              <span className="rounded-full bg-white/80 px-1.5 py-0.5 text-[10px] font-semibold text-emerald-900 ring-1 ring-emerald-200">
                {result.visibility_scope === "personal"
                  ? bi("个人专属", "Personal")
                  : bi("公司公海", "Company shared")}
              </span>
            )}
          </div>

          {channelKey === "telegram" && result.bot_username && (
            <a
              data-testid="telegram-open-bot-link"
              href={`https://t.me/${result.bot_username}`}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 font-semibold text-emerald-800 underline decoration-emerald-400 underline-offset-2 hover:text-emerald-900"
            >
              @{result.bot_username} · {bi("去 Telegram 发消息测试 →", "Test it on Telegram →")}
              <ArrowUpRight className="h-3 w-3" />
            </a>
          )}

          {channelKey === "web_livechat" && (
            <div className="space-y-1.5 rounded-xl border border-emerald-200 bg-white/85 p-2">
              <div className="text-[10.5px] font-semibold text-slate-600 dark:text-slate-400">
                {bi("网站嵌入代码（放到 </body> 前）", "Embed snippet (before </body>)")}
              </div>
              <code
                data-testid="livechat-embed-snippet"
                className="block overflow-x-auto rounded-lg bg-slate-900 px-2 py-1.5 font-mono text-[10px] leading-5 break-all whitespace-pre-wrap text-emerald-100 dark:bg-slate-950 dark:text-emerald-200"
              >
                {embedSnippet}
              </code>
              <button
                type="button"
                data-testid="copy-livechat-snippet"
                onClick={() => void copyEmbedSnippet()}
                className="erp-btn rounded-lg border border-emerald-300 bg-white px-2 py-1 text-[10.5px] font-semibold text-emerald-800 hover:bg-emerald-50"
              >
                {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
                <span>
                  {copied
                    ? bi("已复制", "Copied")
                    : bi("一键复制嵌入代码", "Copy embed snippet")}
                </span>
              </button>
            </div>
          )}

          <div className="flex flex-wrap items-center gap-1.5">
            <button
              type="button"
              data-testid="drawer-view-accounts"
              onClick={onViewAccounts}
              className="erp-btn rounded-lg bg-blue-600 px-2.5 py-1 text-[11px] font-semibold text-white transition-colors hover:bg-blue-700 dark:bg-blue-600 dark:hover:bg-blue-500"
            >
              {bi("查看已接入账号", "View connected accounts")}
            </button>
            <Link
              href="/inbox"
              data-testid="drawer-back-to-inbox"
              onClick={onClose}
              className="erp-btn rounded-lg border border-slate-300 bg-white px-2.5 py-1 text-[11px] font-semibold text-slate-900 hover:bg-slate-50 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-100 dark:hover:bg-slate-800"
            >
              <ArrowLeft className="h-3 w-3" />
              <span>{bi("返回收件箱", "Back to inbox")}</span>
            </Link>
          </div>
        </div>
      )}

      <div className="pt-1 text-[10.5px] leading-5 text-slate-500 dark:text-slate-400">
        {bi(
          "所有令牌使用信封加密保存，只用于该账号；Webhook 均按原始报文验签。",
          "Every token is envelope-encrypted per account and webhooks verify raw bodies."
        )}
      </div>
    </form>
  );
}
