"use client";

import React from "react";
import Link from "next/link";
import {
  Globe,
  Layers,
  Mail,
  MessageSquare,
  Settings,
  ShoppingBag,
  Store,
  Video,
} from "lucide-react";
import { useI18n } from "@/lib/i18n";

export type ChannelFilterKey =
  | "all"
  | "whatsapp"
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
  { key: "all", zh: "全部渠道", en: "All", icon: Layers },
  { key: "whatsapp", zh: "WhatsApp", en: "WhatsApp", icon: MessageSquare },
  { key: "email", zh: "邮件", en: "Email", icon: Mail },
  { key: "web_livechat", zh: "网页客服", en: "WebChat", icon: Globe },
  { key: "shopify", zh: "Shopify", en: "Shopify", icon: ShoppingBag },
  { key: "woocommerce", zh: "WooCommerce", en: "WooCommerce", icon: Store },
  { key: "shopline", zh: "SHOPLINE", en: "SHOPLINE", icon: Store },
  { key: "shoplazza", zh: "Shoplazza", en: "Shoplazza", icon: Store },
  { key: "tiktok_shop", zh: "TikTok 店铺", en: "TikTok", icon: Video },
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
      className="hidden w-40 shrink-0 flex-col justify-between border-r border-slate-200 bg-white/80 p-2 lg:flex xl:w-44"
    >
      <div className="space-y-0.5">
        {CHANNEL_RAIL_ITEMS.map((item) => {
          const Icon = item.icon;
          const active = selectedChannel === item.key;
          const unread = unreadByChannel[item.key] ?? 0;
          return (
            <button
              key={item.key}
              type="button"
              onClick={() => onSelectChannel(item.key)}
              className={`flex w-full items-center justify-between rounded-lg px-2.5 py-2 text-[12px] transition-colors ${
                active
                  ? "bg-blue-50 font-medium text-blue-700"
                  : "text-slate-600 hover:bg-slate-100"
              }`}
            >
              <span className="flex min-w-0 items-center gap-2">
                <Icon className="h-3.5 w-3.5 shrink-0" />
                <span className="truncate">{bi(item.zh, item.en)}</span>
              </span>
              {unread > 0 && (
                <span className="rounded-full bg-blue-600 px-1.5 py-0.5 text-[10px] font-medium tabular-nums text-white">
                  {unread}
                </span>
              )}
            </button>
          );
        })}
      </div>

      <Link
        href="/settings/channels"
        className="mt-2 flex items-center gap-2 rounded-lg border border-slate-200 px-2.5 py-2 text-[12px] text-slate-600 hover:bg-slate-100"
      >
        <Settings className="h-3.5 w-3.5" />
        <span>{bi("渠道设置", "Channel settings")}</span>
      </Link>
    </div>
  );
}
