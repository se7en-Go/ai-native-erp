"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import {
  BadgeDollarSign,
  Boxes,
  Building2,
  Camera,
  ChevronLeft,
  ChevronRight,
  Compass,
  Globe,
  HandCoins,
  Layers,
  Mail,
  MessageCircle,
  MessageSquare,
  Package,
  Send,
  Settings,
  Shirt,
  ShoppingBag,
  ShoppingCart,
  Snowflake,
  Store,
  Truck,
  Video,
  Warehouse,
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
  | "tiktok_shop"
  | "alibaba_email"
  | "amazon_sp"
  | "ebay"
  | "shopee"
  | "lazada"
  | "temu"
  | "shein"
  | "aliexpress"
  | "ozon"
  | "walmart"
  | "mercado_libre";

const CHANNEL_RAIL_ITEMS: Array<{
  key: ChannelFilterKey;
  zh: string;
  en: string;
  icon: React.ComponentType<{ className?: string }>;
  /**
   * 未读徽标的配色（仅 8 个全球电商渠道显式声明）。
   * 旧渠道不传该字段，因此仍走原有白色徽标样式，视觉与既有单测均不受影响。
   */
  unreadTone?: string;
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
  {
    key: "alibaba_email",
    zh: "阿里国际站",
    en: "Alibaba",
    icon: Building2,
  },
  {
    key: "amazon_sp",
    zh: "Amazon",
    en: "Amazon",
    icon: Package,
  },
  {
    key: "ebay",
    zh: "eBay",
    en: "eBay",
    icon: BadgeDollarSign,
  },
  {
    key: "shopee",
    zh: "Shopee",
    en: "Shopee",
    icon: ShoppingCart,
    unreadTone: "bg-orange-100 text-orange-700 dark:bg-orange-500/20 dark:text-orange-300",
  },
  {
    key: "lazada",
    zh: "Lazada",
    en: "Lazada",
    icon: Truck,
    unreadTone: "bg-pink-100 text-pink-700 dark:bg-pink-500/20 dark:text-pink-300",
  },
  {
    key: "temu",
    zh: "Temu",
    en: "Temu",
    icon: Boxes,
    unreadTone: "bg-red-100 text-red-700 dark:bg-red-500/20 dark:text-red-300",
  },
  {
    key: "shein",
    zh: "SHEIN",
    en: "SHEIN",
    icon: Shirt,
    unreadTone: "bg-fuchsia-100 text-fuchsia-700 dark:bg-fuchsia-500/20 dark:text-fuchsia-300",
  },
  {
    key: "aliexpress",
    zh: "速卖通",
    en: "AliExpress",
    icon: Compass,
    unreadTone: "bg-amber-100 text-amber-800 dark:bg-amber-500/20 dark:text-amber-300",
  },
  {
    key: "ozon",
    zh: "Ozon",
    en: "Ozon",
    icon: Snowflake,
    unreadTone: "bg-cyan-100 text-cyan-700 dark:bg-cyan-500/20 dark:text-cyan-300",
  },
  {
    key: "walmart",
    zh: "沃尔玛",
    en: "Walmart",
    icon: Warehouse,
    unreadTone: "bg-indigo-100 text-indigo-700 dark:bg-indigo-500/20 dark:text-indigo-300",
  },
  {
    key: "mercado_libre",
    zh: "美客多",
    en: "Mercado Libre",
    icon: HandCoins,
    unreadTone: "bg-yellow-100 text-yellow-800 dark:bg-yellow-500/20 dark:text-yellow-300",
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
  const scrollerRef = useRef<HTMLDivElement | null>(null);
  const [canScrollLeft, setCanScrollLeft] = useState(false);
  const [canScrollRight, setCanScrollRight] = useState(false);

  const syncScrollState = useCallback(() => {
    const el = scrollerRef.current;
    if (!el) return;
    const maxScroll = Math.max(0, el.scrollWidth - el.clientWidth);
    setCanScrollLeft(el.scrollLeft > 1);
    setCanScrollRight(maxScroll - el.scrollLeft > 1);
  }, []);

  useEffect(() => {
    if (variant !== "compact") return;
    const el = scrollerRef.current;
    if (!el) return;

    syncScrollState();

    const handleWheel = (event: WheelEvent) => {
      if (el.scrollWidth <= el.clientWidth + 1) return;
      // Let native two-finger horizontal trackpad gestures work unobstructed.
      if (Math.abs(event.deltaX) >= Math.abs(event.deltaY)) return;
      if (event.deltaY === 0) return;

      const unit =
        event.deltaMode === 1
          ? 16
          : event.deltaMode === 2
            ? el.clientWidth
            : 1;
      const delta = event.deltaY * unit;
      const prev = el.scrollLeft;
      const maxScroll = Math.max(0, el.scrollWidth - el.clientWidth);
      const next = Math.max(0, Math.min(maxScroll, prev + delta));
      event.preventDefault();
      if (next !== prev) {
        el.scrollLeft = next;
        syncScrollState();
      }
    };

    el.addEventListener("wheel", handleWheel, { passive: false });
    el.addEventListener("scroll", syncScrollState, { passive: true });
    window.addEventListener("resize", syncScrollState);

    let observer: ResizeObserver | null = null;
    if (typeof ResizeObserver !== "undefined") {
      observer = new ResizeObserver(syncScrollState);
      observer.observe(el);
    }

    return () => {
      el.removeEventListener("wheel", handleWheel);
      el.removeEventListener("scroll", syncScrollState);
      window.removeEventListener("resize", syncScrollState);
      observer?.disconnect();
    };
  }, [variant, syncScrollState]);

  useEffect(() => {
    if (variant !== "compact") return;
    const el = scrollerRef.current;
    if (!el) return;
    const activeButton = el.querySelector<HTMLElement>(
      `[data-channel-key="${selectedChannel}"]`
    );
    if (activeButton && typeof activeButton.scrollIntoView === "function") {
      activeButton.scrollIntoView({
        block: "nearest",
        inline: "nearest",
        behavior: "smooth",
      });
    }
    syncScrollState();
  }, [variant, selectedChannel, syncScrollState]);

  const scrollByDirection = useCallback(
    (direction: -1 | 1) => {
      const el = scrollerRef.current;
      if (!el) return;
      const step = Math.max(220, Math.round(el.clientWidth * 0.65));
      if (typeof el.scrollBy === "function") {
        el.scrollBy({ left: direction * step, behavior: "smooth" });
      } else {
        el.scrollLeft += direction * step;
      }
      syncScrollState();
    },
    [syncScrollState]
  );

  if (variant === "compact") {
    return (
      <div
        data-testid="inbox-channel-rail"
        className="flex items-center gap-2 border-b border-gray-200 bg-white px-3 py-2 dark:border-gray-800 dark:bg-gray-900"
      >
        <div className="relative flex min-w-0 flex-1 items-center">
          {canScrollLeft && (
            <div className="pointer-events-none absolute inset-y-0 left-0 z-10 flex items-center bg-gradient-to-r from-white via-white/95 to-transparent pr-5 dark:from-gray-900 dark:via-gray-900/95">
              <button
                type="button"
                data-testid="inbox-channel-rail-scroll-left"
                aria-label={bi("向左滚动渠道", "Scroll channels left")}
                title={bi("向左滚动渠道", "Scroll channels left")}
                onClick={() => scrollByDirection(-1)}
                className="pointer-events-auto grid h-7 w-7 place-items-center rounded-full border border-gray-200 bg-white text-gray-600 shadow-xs transition-colors hover:border-blue-300 hover:bg-blue-50 hover:text-blue-700 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300 dark:hover:border-blue-500/40 dark:hover:bg-blue-500/15 dark:hover:text-blue-300"
              >
                <ChevronLeft className="h-4 w-4" />
              </button>
            </div>
          )}

          <div
            ref={scrollerRef}
            data-testid="inbox-channel-rail-scroll"
            className="flex min-w-0 flex-1 items-center gap-1.5 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
          >
            {CHANNEL_RAIL_ITEMS.map((item) => {
              const Icon = item.icon;
              const active = selectedChannel === item.key;
              const unread = unreadByChannel[item.key] ?? 0;
              return (
                <button
                  key={item.key}
                  type="button"
                  data-channel-key={item.key}
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
                          : (item.unreadTone ??
                            "bg-white text-blue-600 dark:bg-gray-900 dark:text-blue-300")
                      }`}
                    >
                      {unread}
                    </span>
                  )}
                </button>
              );
            })}
          </div>

          {canScrollRight && (
            <div className="pointer-events-none absolute inset-y-0 right-0 z-10 flex items-center bg-gradient-to-l from-white via-white/95 to-transparent pl-5 dark:from-gray-900 dark:via-gray-900/95">
              <button
                type="button"
                data-testid="inbox-channel-rail-scroll-right"
                aria-label={bi("向右滚动渠道", "Scroll channels right")}
                title={bi("向右滚动渠道", "Scroll channels right")}
                onClick={() => scrollByDirection(1)}
                className="pointer-events-auto grid h-7 w-7 place-items-center rounded-full border border-gray-200 bg-white text-gray-600 shadow-xs transition-colors hover:border-blue-300 hover:bg-blue-50 hover:text-blue-700 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300 dark:hover:border-blue-500/40 dark:hover:bg-blue-500/15 dark:hover:text-blue-300"
              >
                <ChevronRight className="h-4 w-4" />
              </button>
            </div>
          )}
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
                <span
                  className={`shrink-0 rounded-full px-1.5 py-0.5 text-[10px] font-bold tabular-nums ${
                    item.unreadTone ?? "bg-rose-500 text-white"
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
