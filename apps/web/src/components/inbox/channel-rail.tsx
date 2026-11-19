"use client";

import React from "react";
import Link from "next/link";
import {
  MessageSquare,
  Mail,
  Globe,
  ShoppingBag,
  Store,
  Video,
  Settings,
  Layers,
} from "lucide-react";

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
  label: string;
  icon: React.ComponentType<{ className?: string }>;
}> = [
  { key: "all", label: "All", icon: Layers },
  { key: "whatsapp", label: "WhatsApp", icon: MessageSquare },
  { key: "email", label: "Email", icon: Mail },
  { key: "web_livechat", label: "WebChat", icon: Globe },
  { key: "shopify", label: "Shopify", icon: ShoppingBag },
  { key: "woocommerce", label: "WooCommerce", icon: Store },
  { key: "shopline", label: "SHOPLINE", icon: Store },
  { key: "shoplazza", label: "Shoplazza", icon: Store },
  { key: "tiktok_shop", label: "TikTok", icon: Video },
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
  return (
    <div
      data-testid="inbox-channel-rail"
      className="flex w-44 shrink-0 flex-col justify-between border-r border-slate-200 bg-white p-2"
    >
      <div className="space-y-1">
        {CHANNEL_RAIL_ITEMS.map((item) => {
          const Icon = item.icon;
          const active = selectedChannel === item.key;
          const unread = unreadByChannel[item.key] ?? 0;
          return (
            <button
              key={item.key}
              type="button"
              onClick={() => onSelectChannel(item.key)}
              className={`flex w-full items-center justify-between rounded-md px-2.5 py-2 text-xs font-medium ${
                active
                  ? "bg-indigo-50 text-indigo-700"
                  : "text-slate-700 hover:bg-slate-100"
              }`}
            >
              <span className="flex items-center gap-2">
                <Icon className="h-3.5 w-3.5 shrink-0" />
                <span>{item.label}</span>
              </span>
              {unread > 0 && (
                <span className="rounded-full bg-indigo-600 px-1.5 py-0.5 text-[10px] font-semibold text-white">
                  {unread}
                </span>
              )}
            </button>
          );
        })}
      </div>

      <Link
        href="/settings/channels"
        className="mt-2 flex items-center gap-2 rounded-md border border-slate-200 px-2.5 py-2 text-xs font-medium text-slate-600 hover:bg-slate-100"
      >
        <Settings className="h-3.5 w-3.5" />
        <span>Channel Settings</span>
      </Link>
    </div>
  );
}
