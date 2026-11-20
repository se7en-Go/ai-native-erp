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
import { marketplaceKeyForChannelFilter } from "@/components/channels/channel-marketplace";

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
}> = [
  {
    key: "all",
    zh: "全部渠道",
    en: "All",
    icon: Layers,
  },
  {
    key: "whatsapp",
    zh: "WhatsApp",
    en: "WhatsApp",
    icon: MessageSquare,
  },
  {
    key: "telegram",
    zh: "Telegram",
    en: "Telegram",
    icon: Send,
  },
  {
    key: "facebook_messenger",
    zh: "Messenger",
    en: "Messenger",
    icon: MessageCircle,
  },
  {
    key: "instagram_dm",
    zh: "Instagram 私信",
    en: "Instagram DM",
    icon: Camera,
  },
  {
    key: "email",
    zh: "邮件",
    en: "Email",
    icon: Mail,
  },
  {
    key: "web_livechat",
    zh: "网页客服",
    en: "WebChat",
    icon: Globe,
  },
  {
    key: "shopify",
    zh: "Shopify",
    en: "Shopify",
    icon: ShoppingBag,
  },
  {
    key: "woocommerce",
    zh: "WooCommerce",
    en: "WooCommerce",
    icon: Store,
  },
  {
    key: "shopline",
    zh: "SHOPLINE",
    en: "SHOPLINE",
    icon: Store,
  },
  {
    key: "shoplazza",
    zh: "Shoplazza",
    en: "Shoplazza",
    icon: Store,
  },
  {
    key: "tiktok_shop",
    zh: "TikTok 店铺",
    en: "TikTok",
    icon: Video,
  },
];

function settingsHrefOf(selectedChannel: ChannelFilterKey) {
  const connectKey = marketplaceKeyForChannelFilter(selectedChannel);
  return connectKey ? `/settings/channels?connect=${connectKey}` : "/settings/channels";
}

export function ChannelRail({
  selectedChannel,
  onSelectChannel,
  unreadByChannel,
  variant = "rail",
}: {
  selectedChannel: ChannelFilterKey;
  onSelectChannel: (channel: ChannelFilterKey) => void;
  unreadByChannel: Partial<Record<ChannelFilterKey, number>>;
  variant?: "rail" | "compact";
}) {
  const { bi } = useI18n();
  const settingsHref = settingsHrefOf(selectedChannel);

  if (variant === "compact") {
    return (
      <div
        data-testid="inbox-channel-rail"
        className="flex items-center gap-2 border-b border-gray-200 bg-white px-3 py-2 dark:border-gray-800 dark:bg-gray-900"
      >
        <div className="flex min-w-0 flex-1 items-center gap-1.5 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
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
                className={`inline-flex shrink-0 items-center gap-1.5 rounded-full px-3 py-1.5 text-[12px] font-medium transition-colors ${
                  active
                    ? "bg-blue-600 text-white"
                    : "bg-gray-100 text-gray-700 hover:bg-blue-50 hover:text-blue-700 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-blue-500/15 dark:hover:text-blue-300"
                }`}
              >
                <Icon className="h-3.5 w-3.5" />
                <span>{bi(item.zh, item.en)}</span>
                {unread > 0 && (
                  <span
                    className={`rounded-full px-1.5 py-0.5 text-[10px] font-bold tabular-nums ${
                      active
                        ? "bg-white/20 text-white"
                        : "bg-white text-blue-600 dark:bg-gray-900 dark:text-blue-300"
                    }`}
                  >
                    {unread}
                  </span>
                )}
              </button>
            );
          })}
        </div>

        <Link
          href={settingsHref}
          data-testid="inbox-channel-settings-link"
          className="inline-flex shrink-0 items-center gap-1 rounded-full border border-gray-200 bg-gray-50 px-2.5 py-1.5 text-[12px] font-medium text-gray-600 transition-colors hover:border-blue-200 hover:bg-blue-50 hover:text-blue-700 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300 dark:hover:border-blue-500/40 dark:hover:bg-blue-500/10 dark:hover:text-blue-300"
        >
          <Settings className="h-3.5 w-3.5" />
          <span>{bi("渠道设置", "Channel settings")}</span>
        </Link>
      </div>
    );
  }

  return (
    <div
      data-testid="inbox-channel-rail"
      className="hidden w-44 shrink-0 flex-col justify-between border-r border-gray-200 bg-white p-2.5 dark:border-gray-800 dark:bg-gray-900 lg:flex xl:w-48"
    >
      <div className="space-y-0.5">
        <p className="px-2 pb-1.5 text-[10.5px] font-semibold tracking-wide text-gray-500 dark:text-gray-400">
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
                  ? "bg-blue-50 font-semibold text-blue-700 ring-1 ring-blue-200 dark:bg-blue-500/15 dark:text-blue-200 dark:ring-blue-500/30"
                  : "font-medium text-gray-700 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-800"
              }`}
            >
              <span className="flex min-w-0 items-center gap-2">
                <span
                  className={`grid h-6 w-6 shrink-0 place-items-center rounded-lg transition-colors ${
                    active
                      ? "bg-blue-600 text-white"
                      : "bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-300"
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
        href={settingsHref}
        data-testid="inbox-channel-settings-link"
        className="mt-2 flex items-center gap-2 rounded-xl border border-gray-200 bg-gray-50 px-2.5 py-2 text-[12px] font-medium text-gray-700 transition-colors hover:border-blue-200 hover:bg-blue-50 hover:text-blue-700 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300 dark:hover:border-blue-500/40 dark:hover:bg-blue-500/10 dark:hover:text-blue-300"
      >
        <span className="grid h-6 w-6 shrink-0 place-items-center rounded-lg bg-white text-gray-600 ring-1 ring-gray-200 dark:bg-gray-900 dark:text-gray-300 dark:ring-gray-700">
          <Settings className="h-3.5 w-3.5" />
        </span>
        <span className="truncate">{bi("渠道设置", "Channel settings")}</span>
      </Link>
    </div>
  );
}
