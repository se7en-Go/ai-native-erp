"use client";

import React from "react";
import Link from "next/link";
import {
  Camera,
  Globe,
  Layers,
  Mail,
  MessageCircle,
  MessageSquare,
  Send,
  Settings,
  ShoppingBag,
  Store,
  Video,
} from "lucide-react";
import { useI18n } from "@/lib/i18n";

export type ChannelFilterKey =
  | "all"
  | "whatsapp"
  | "telegram"
  | "facebook_messenger"
  | "instagram_dm"
  | "email"
  | "web_livechat"
  | "shopify"
  | "woocommerce"
  | "shopline"
  | "shoplazza"
  | "tiktok_shop";

const CHANNEL_RAIL_ITEMS: Array<{
  key: ChannelFilterKey;
  zh: string;
  en: string;
  icon: React.ComponentType<{ className?: string }>;
  accent: string;
  activeAccent: string;
}> = [
  {
    key: "all",
    zh: "全部渠道",
    en: "All",
    icon: Layers,
    accent: "bg-slate-100 text-slate-600 ring-1 ring-slate-200",
    activeAccent: "bg-blue-600 text-white ring-1 ring-blue-600",
  },
  {
    key: "whatsapp",
    zh: "WhatsApp",
    en: "WhatsApp",
    icon: MessageSquare,
    accent: "bg-emerald-50 text-emerald-600 ring-1 ring-emerald-100",
    activeAccent: "bg-emerald-600 text-white ring-1 ring-emerald-600",
  },
  {
    key: "telegram",
    zh: "Telegram",
    en: "Telegram",
    icon: Send,
    accent: "bg-sky-50 text-sky-600 ring-1 ring-sky-100",
    activeAccent: "bg-sky-600 text-white ring-1 ring-sky-600",
  },
  {
    key: "facebook_messenger",
    zh: "Messenger",
    en: "Messenger",
    icon: MessageCircle,
    accent: "bg-blue-50 text-blue-600 ring-1 ring-blue-100",
    activeAccent: "bg-blue-600 text-white ring-1 ring-blue-600",
  },
  {
    key: "instagram_dm",
    zh: "Instagram 私信",
    en: "Instagram DM",
    icon: Camera,
    accent: "bg-rose-50 text-rose-600 ring-1 ring-rose-100",
    activeAccent: "bg-rose-600 text-white ring-1 ring-rose-600",
  },
  {
    key: "email",
    zh: "邮件",
    en: "Email",
    icon: Mail,
    accent: "bg-blue-50 text-blue-600 ring-1 ring-blue-100",
    activeAccent: "bg-blue-600 text-white ring-1 ring-blue-600",
  },
  {
    key: "web_livechat",
    zh: "网页客服",
    en: "WebChat",
    icon: Globe,
    accent: "bg-violet-50 text-violet-600 ring-1 ring-violet-100",
    activeAccent: "bg-violet-600 text-white ring-1 ring-violet-600",
  },
  {
    key: "shopify",
    zh: "Shopify",
    en: "Shopify",
    icon: ShoppingBag,
    accent: "bg-lime-50 text-lime-700 ring-1 ring-lime-100",
    activeAccent: "bg-lime-600 text-white ring-1 ring-lime-600",
  },
  {
    key: "woocommerce",
    zh: "WooCommerce",
    en: "WooCommerce",
    icon: Store,
    accent: "bg-purple-50 text-purple-600 ring-1 ring-purple-100",
    activeAccent: "bg-purple-600 text-white ring-1 ring-purple-600",
  },
  {
    key: "shopline",
    zh: "SHOPLINE",
    en: "SHOPLINE",
    icon: Store,
    accent: "bg-sky-50 text-sky-600 ring-1 ring-sky-100",
    activeAccent: "bg-sky-600 text-white ring-1 ring-sky-600",
  },
  {
    key: "shoplazza",
    zh: "Shoplazza",
    en: "Shoplazza",
    icon: Store,
    accent: "bg-indigo-50 text-indigo-600 ring-1 ring-indigo-100",
    activeAccent: "bg-indigo-600 text-white ring-1 ring-indigo-600",
  },
  {
    key: "tiktok_shop",
    zh: "TikTok 店铺",
    en: "TikTok",
    icon: Video,
    accent: "bg-slate-900/5 text-slate-800 ring-1 ring-slate-200",
    activeAccent: "bg-slate-900 text-white ring-1 ring-slate-900",
  },
];

export function ChannelRail({
  selectedChannel,
  onSelectChannel,
  unreadByChannel,
}: {
  selectedChannel: ChannelFilterKey;
  onSelectChannel: (channel: ChannelFilterKey) => void;
  unreadByChannel: Partial<Record<ChannelFilterKey, number>>;
}) {
  const { bi } = useI18n();
  return (
    <div
      data-testid="inbox-channel-rail"
      className="hidden w-44 shrink-0 flex-col justify-between border-r border-slate-200/90 bg-white/95 p-2.5 lg:flex xl:w-48"
    >
      <div className="space-y-0.5">
        <p className="px-2 pb-1.5 text-[10.5px] font-semibold tracking-wide text-slate-500">
          {bi("渠道筛选", "Channels")}
        </p>
        {CHANNEL_RAIL_ITEMS.map((item) => {
          const Icon = item.icon;
          const active = selectedChannel === item.key;
          const unread = unreadByChannel[item.key] ?? 0;
          return (
            <button
              key={item.key}
              type="button"
              aria-pressed={active}
              onClick={() => onSelectChannel(item.key)}
              className={`group flex w-full items-center justify-between gap-1.5 rounded-xl px-1.5 py-1.5 text-left text-[12.5px] transition-all duration-150 ${
                active
                  ? "bg-blue-50/90 font-semibold text-blue-800 ring-1 ring-blue-200/80"
                  : "font-medium text-slate-700 hover:bg-slate-100/90"
              }`}
            >
              <span className="flex min-w-0 items-center gap-2">
                <span
                  className={`grid h-6 w-6 shrink-0 place-items-center rounded-lg transition-colors ${
                    active ? item.activeAccent : item.accent
                  }`}
                >
                  <Icon className="h-3.5 w-3.5" />
                </span>
                <span className="truncate">{bi(item.zh, item.en)}</span>
              </span>
              {unread > 0 && (
                <span className="shrink-0 rounded-full bg-rose-500 px-1.5 py-0.5 text-[10px] font-bold tabular-nums text-white">
                  {unread}
                </span>
              )}
            </button>
          );
        })}
      </div>

      <Link
        href="/settings/channels"
        className="mt-2 flex items-center gap-2 rounded-xl border border-slate-200 bg-slate-50/80 px-2.5 py-2 text-[12px] font-medium text-slate-700 transition-colors hover:border-slate-300 hover:bg-white"
      >
        <span className="grid h-6 w-6 shrink-0 place-items-center rounded-lg bg-white text-slate-600 ring-1 ring-slate-200">
          <Settings className="h-3.5 w-3.5" />
        </span>
        <span className="truncate">{bi("渠道设置", "Channel settings")}</span>
      </Link>
    </div>
  );
}
