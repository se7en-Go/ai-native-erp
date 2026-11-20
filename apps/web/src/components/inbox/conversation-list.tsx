"use client";

import React from "react";
import Link from "next/link";
import { AlertTriangle, Clock, LockKeyhole, Pin, Search, Tag } from "lucide-react";
import type { ChannelType, ConversationMode, ConversationStatus } from "@/lib/contracts";
import { useI18n } from "@/lib/i18n";

export type ConversationFilterTab = "all" | "unread" | "pending_human" | "sla_urgent";

export interface ConversationSummary {
  id: string;
  customer_id: string;
  customer_name: string;
  country_code?: string | null;
  iana_timezone?: string | null;
  channel_account_id: string;
  channel_type: ChannelType;
  conversation_mode: ConversationMode;
  status: ConversationStatus;
  unread_count: number;
  sla_due_at?: string | null;
  csw_expires_at?: string | null;
  human_agent_window_expires_at?: string | null;
  last_message_preview: string;
  updated_at: string;
  /** 归属与路由信息（后端增量下发，缺省即未分配）。 */
  assigned_user_id?: string | null;
  assigned_user_name?: string | null;
  channel_account_display_name?: string | null;
  channel_visibility_scope?: "shared" | "personal" | null;
  /** Pack B 增量字段：会话置顶与服务端标签集合（缺省即未置顶 / 无标签）。 */
  is_pinned?: boolean;
  tags?: string[];
}

/** 「按绑定账号筛选」下拉里的一个渠道账号。 */
export interface ConversationAccountOption {
  id: string;
  display_name: string;
}

/**
 * 筛选标签保留机器码（all / unread / pending_human / sla_urgent）作为可访问名称，
 * 保证既有单测与运维语义一致；中文说明通过 title 提供。
 */
const FILTER_TABS: Array<{
  key: ConversationFilterTab;
  label: string;
  zh: string;
  titleZh: string;
}> = [
  { key: "all", label: "all", zh: "全部", titleZh: "全部会话" },
  { key: "unread", label: "unread", zh: "未读", titleZh: "未读会话" },
  { key: "pending_human", label: "pending_human", zh: "待接管", titleZh: "待人工接管" },
  { key: "sla_urgent", label: "sla_urgent", zh: "SLA", titleZh: "SLA 紧急" },
];

/** 买家首字母彩色头像：按名称稳定散列到 21st.dev 常用柔和冷色板。 */
const AVATAR_PALETTE = [
  "bg-blue-100 text-blue-700 ring-blue-200 dark:bg-blue-500/15 dark:text-blue-300 dark:ring-blue-500/30",
  "bg-sky-100 text-sky-700 ring-sky-200 dark:bg-sky-500/15 dark:text-sky-300 dark:ring-sky-500/30",
  "bg-indigo-100 text-indigo-700 ring-indigo-200 dark:bg-indigo-500/15 dark:text-indigo-300 dark:ring-indigo-500/30",
  "bg-slate-100 text-slate-700 ring-slate-200 dark:bg-slate-700 dark:text-slate-200 dark:ring-slate-600",
  "bg-violet-100 text-violet-700 ring-violet-200 dark:bg-violet-500/15 dark:text-violet-300 dark:ring-violet-500/30",
  "bg-cyan-100 text-cyan-700 ring-cyan-200 dark:bg-cyan-500/15 dark:text-cyan-300 dark:ring-cyan-500/30",
];

function avatarTone(name: string): string {
  let hash = 0;
  for (let i = 0; i < name.length; i += 1) {
    hash = (hash * 31 + name.charCodeAt(i)) % 997;
  }
  return AVATAR_PALETTE[hash % AVATAR_PALETTE.length];
}

export function initialsOf(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

/** 渠道展示色：保持冷色系区分。 */
const CHANNEL_DOT: Record<string, string> = {
  whatsapp_cloud: "bg-blue-500",
  whatsapp: "bg-blue-500",
  email: "bg-sky-500",
  made_in_china_email: "bg-sky-500",
  web_livechat: "bg-indigo-500",
  shopify: "bg-blue-600",
  woocommerce: "bg-violet-500",
  shopline: "bg-sky-500",
  shoplazza: "bg-indigo-500",
  tiktok_shop: "bg-slate-700",
  telegram: "bg-cyan-500",
  messenger: "bg-blue-600",
  instagram_dm: "bg-violet-600",
};

type OwnershipPill = { zh: string; en: string; className: string };

/** 会话归属胶囊：个人专属 > 指定负责人 > 公司公海。 */
export function conversationOwnershipPill(
  conv: Pick<
    ConversationSummary,
    "channel_visibility_scope" | "assigned_user_name" | "assigned_user_id"
  >,
  personalLabelZh = "个人",
): OwnershipPill {
  if (conv.channel_visibility_scope === "personal") {
    const owner = conv.assigned_user_name;
    return {
      zh: `专属 · ${owner ?? personalLabelZh}`,
      en: `Personal · ${owner ?? "me"}`,
      className:
        "bg-blue-50 text-blue-700 ring-blue-200/80 dark:bg-blue-500/10 dark:text-blue-300 dark:ring-blue-500/30",
    };
  }
  if (conv.assigned_user_name) {
    return {
      zh: `负责人 · ${conv.assigned_user_name}`,
      en: `Owner · ${conv.assigned_user_name}`,
      className:
        "bg-gray-100 text-gray-700 ring-gray-200/80 dark:bg-gray-800 dark:text-gray-200 dark:ring-gray-700",
    };
  }
  return {
    zh: "公司公海",
    en: "Company shared",
    className:
      "bg-gray-100 text-gray-700 ring-gray-200/80 dark:bg-gray-800 dark:text-gray-200 dark:ring-gray-700",
  };
}

export function ConversationList({
  conversations,
  selectedConversationId,
  activeFilter,
  searchQuery,
  onSelectFilter,
  onSearchChange,
  onSelectConversation,
  headerSlot,
  quickConnectHref = "/settings/channels",
  quickConnectLabel,
  channelAccounts = [],
  channelAccountFilter = "all",
  onChannelAccountFilterChange,
  pinnedOnly = false,
  onTogglePinnedOnly,
}: {
  conversations: ConversationSummary[];
  selectedConversationId: string | null;
  activeFilter: ConversationFilterTab;
  searchQuery: string;
  onSelectFilter: (filter: ConversationFilterTab) => void;
  onSearchChange: (value: string) => void;
  onSelectConversation: (conversationId: string) => void;
  /** 管层专用的归属筛选条（全部会话 / 只看我的 / 公司公海）。 */
  headerSlot?: React.ReactNode;
  /** 当前渠道筛选对应的渠道中心深链（`?connect=<channel>` 直达抽屉）。 */
  quickConnectHref?: string;
  quickConnectLabel?: string;
  /** Pack B：已绑定渠道账号列表，用于「按绑定账号筛选」下拉。 */
  channelAccounts?: ConversationAccountOption[];
  /** 当前选中的渠道账号 id；`all` 表示全部账号。 */
  channelAccountFilter?: string;
  onChannelAccountFilterChange?: (channelAccountId: string) => void;
  /** 仅看置顶开关。 */
  pinnedOnly?: boolean;
  onTogglePinnedOnly?: () => void;
}) {
  const { bi } = useI18n();
  const unreadTotal = conversations.reduce((total, conv) => total + conv.unread_count, 0);

  return (
    <div
      data-testid="inbox-conversation-list"
      className="flex min-h-0 w-72 shrink-0 flex-col border-r border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-900 xl:w-80"
    >
      <div className="space-y-2 border-b border-gray-200 p-2.5 dark:border-gray-800">
        <div className="flex items-center justify-between gap-2 px-0.5">
          <span className="text-[12.5px] font-semibold text-gray-900 dark:text-gray-100">
            {bi("会话列表", "Conversations")}
          </span>
          {unreadTotal > 0 && (
            <span className="rounded-full bg-rose-50 px-2 py-0.5 text-[10.5px] font-bold tabular-nums text-rose-700 ring-1 ring-rose-200 dark:bg-rose-500/10 dark:text-rose-300 dark:ring-rose-500/30">
              {unreadTotal} {bi("条未读", "unread")}
            </span>
          )}
        </div>

        <div className="relative">
          <Search className="pointer-events-none absolute top-1/2 left-2.5 h-3.5 w-3.5 -translate-y-1/2 text-gray-400" />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => onSearchChange(e.target.value)}
            placeholder={bi("搜索买家、订单号或邮箱…", "Search buyer, order or email…")}
            aria-label={bi("搜索会话", "Search conversations")}
            className="w-full rounded-xl border border-gray-200 bg-gray-50 py-1.5 pr-2.5 pl-8 text-[12px] text-gray-800 placeholder:text-gray-400 focus:border-blue-500 focus:bg-white focus:outline-none focus:ring-[3px] focus:ring-blue-500/12 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder:text-gray-500 dark:focus:bg-gray-900"
          />
        </div>

        <div className="flex items-center gap-1.5">
          <select
            data-testid="inbox-account-filter"
            aria-label={bi("按绑定账号筛选", "Filter by connected account")}
            value={channelAccountFilter}
            onChange={(e) => onChannelAccountFilterChange?.(e.target.value)}
            className="min-w-0 flex-1 truncate rounded-lg border border-gray-200 bg-gray-50 px-2 py-1 text-[11.5px] font-semibold text-gray-700 transition-colors hover:border-blue-200 focus:border-blue-500 focus:bg-white focus:outline-none focus:ring-[3px] focus:ring-blue-500/12 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-200 dark:focus:bg-gray-900"
          >
            <option value="all">{bi("全部账号", "All accounts")}</option>
            {channelAccounts.map((account) => (
              <option key={account.id} value={account.id}>
                {account.display_name}
              </option>
            ))}
          </select>

          <button
            type="button"
            data-testid="inbox-pinned-only-btn"
            aria-pressed={pinnedOnly}
            title={bi("仅看置顶会话", "Pinned conversations only")}
            onClick={() => onTogglePinnedOnly?.()}
            className={`inline-flex shrink-0 items-center gap-1 rounded-lg border px-2 py-1 text-[11.5px] font-semibold transition-colors ${
              pinnedOnly
                ? "border-blue-600 bg-blue-600 text-white shadow-[0_1px_6px_-2px_rgba(37,99,235,0.45)]"
                : "border-gray-200 bg-white text-gray-600 hover:border-blue-200 hover:bg-blue-50 hover:text-blue-700 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300 dark:hover:border-blue-500/40 dark:hover:bg-blue-500/10 dark:hover:text-blue-300"
            }`}
          >
            <Pin className="h-3 w-3" />
            <span>{bi("仅看置顶", "Pinned")}</span>
          </button>
        </div>

        <div className="grid grid-cols-4 gap-1">
          {FILTER_TABS.map((tab) => {
            const active = activeFilter === tab.key;
            return (
              <button
                key={tab.key}
                type="button"
                aria-pressed={active}
                title={bi(tab.titleZh, tab.label)}
                aria-label={`Filter ${tab.key === "all" ? "everything" : tab.key}`}
                onClick={() => onSelectFilter(tab.key)}
                className={`truncate rounded-lg px-1.5 py-1 text-[11px] font-semibold transition-all duration-150 ${
                  active
                    ? "bg-blue-600 text-white shadow-[0_1px_4px_-1px_rgba(37,99,235,0.6)]"
                    : "bg-gray-100 text-gray-600 hover:bg-gray-200 hover:text-gray-900 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700 dark:hover:text-white"
                }`}
              >
                {bi(tab.zh, tab.label)}
              </button>
            );
          })}
        </div>

        {headerSlot}
      </div>

      <div className="flex-1 divide-y divide-gray-100 overflow-y-auto dark:divide-gray-800">
        {conversations.length === 0 ? (
          <div className="m-3 space-y-2 rounded-2xl border border-dashed border-gray-300 bg-gray-50 p-6 text-center text-[12px] font-medium text-gray-600 dark:border-gray-700 dark:bg-gray-800/60 dark:text-gray-300">
            <div>{bi("没有符合筛选条件的会话。", "No conversations match this filter.")}</div>
            <div className="text-[11px] font-normal text-gray-500 dark:text-gray-400">
              {bi(
                "还没有接到客户消息？先绑定一个渠道，客户回复会直接进来。",
                "No inbound messages yet? Bind a channel first and replies land here."
              )}
            </div>
            <Link
              href={quickConnectHref}
              data-testid="inbox-quick-connect-link"
              className="inline-flex items-center gap-1 rounded-lg border border-blue-200 bg-blue-50 px-2 py-1 text-[11px] font-semibold text-blue-700 transition-colors hover:bg-blue-100 dark:border-blue-500/30 dark:bg-blue-500/10 dark:text-blue-300 dark:hover:bg-blue-500/15"
            >
              {quickConnectLabel ?? bi("快捷绑定渠道 →", "Quick connect a channel →")}
            </Link>
          </div>
        ) : (
          conversations.map((conv) => {
            const selected = conv.id === selectedConversationId;
            const isSlaUrgent =
              conv.sla_due_at !== null &&
              conv.sla_due_at !== undefined &&
              new Date(conv.sla_due_at).getTime() - Date.now() < 1000 * 60 * 60 * 4;
            const ownership = conversationOwnershipPill(conv, bi("个人", "Personal"));

            return (
              <button
                key={conv.id}
                type="button"
                aria-label={`${conv.customer_name} — ${conv.last_message_preview}`}
                data-testid={`conversation-item-${conv.id}`}
                onClick={() => onSelectConversation(conv.id)}
                className={`relative block w-full border-l-[3px] px-3 py-2.5 text-left transition-colors ${
                  selected
                    ? "border-blue-600 bg-blue-50/70 dark:bg-blue-500/10"
                    : "border-transparent hover:bg-gray-50 dark:hover:bg-gray-800/70"
                }`}
              >
                <div className="flex items-start gap-2.5">
                  <span
                    aria-hidden="true"
                    className={`mt-0.5 grid h-8 w-8 shrink-0 place-items-center rounded-full text-[11.5px] font-bold ring-1 ${avatarTone(
                      conv.customer_name
                    )}`}
                  >
                    {initialsOf(conv.customer_name)}
                  </span>

                  <div className="min-w-0 flex-1">
                    <div className="flex items-center justify-between gap-2">
                      <span className="flex min-w-0 items-center gap-1 truncate text-[13px] font-semibold text-gray-900 dark:text-gray-100">
                        {conv.is_pinned && (
                          <Pin
                            aria-hidden="true"
                            data-testid={`conversation-pin-${conv.id}`}
                            className="h-3 w-3 shrink-0 rotate-45 text-blue-600"
                          />
                        )}
                        <span className="truncate">
                          {conv.customer_name}
                          {conv.country_code ? ` (${conv.country_code})` : ""}
                        </span>
                      </span>
                      {conv.unread_count > 0 && (
                        <span className="shrink-0 rounded-full bg-rose-500 px-1.5 py-0.5 text-[10px] font-bold tabular-nums text-white">
                          {conv.unread_count}
                        </span>
                      )}
                    </div>

                    <p className="mt-0.5 line-clamp-2 text-[12px] leading-5 text-gray-600 dark:text-gray-400">
                      {conv.last_message_preview}
                    </p>

                    {(conv.tags ?? []).length > 0 && (
                      <div className="mt-1 flex flex-wrap items-center gap-1">
                        {(conv.tags ?? []).map((tag) => (
                          <span
                            key={tag}
                            data-testid={`conversation-tag-${conv.id}-${tag}`}
                            className="inline-flex items-center gap-0.5 rounded-md bg-blue-50 px-1.5 py-0.5 text-[10px] font-semibold text-blue-700 ring-1 ring-blue-200/80 dark:bg-blue-500/10 dark:text-blue-300 dark:ring-blue-500/30"
                          >
                            <Tag className="h-2.5 w-2.5" />
                            {tag}
                          </span>
                        ))}
                      </div>
                    )}

                    <div className="mt-1.5 flex flex-wrap items-center gap-1 text-[10px] font-medium">
                      <span className="inline-flex items-center gap-1 rounded-md bg-gray-100 px-1.5 py-0.5 text-gray-700 ring-1 ring-gray-200/70 dark:bg-gray-800 dark:text-gray-200 dark:ring-gray-700">
                        <span
                          aria-hidden="true"
                          className={`h-1.5 w-1.5 rounded-full ${
                            CHANNEL_DOT[conv.channel_type] ?? "bg-slate-400"
                          }`}
                        />
                        {conv.channel_type}
                      </span>
                      <span className="rounded-md bg-blue-50 px-1.5 py-0.5 text-blue-700 ring-1 ring-blue-100 dark:bg-blue-500/10 dark:text-blue-300 dark:ring-blue-500/30">
                        {conv.conversation_mode}
                      </span>
                      {conv.status === "pending_human" && (
                        <span className="inline-flex items-center gap-0.5 rounded-md bg-amber-50 px-1.5 py-0.5 text-amber-800 ring-1 ring-amber-200/70 dark:bg-amber-500/10 dark:text-amber-300 dark:ring-amber-500/30">
                          <AlertTriangle className="h-2.5 w-2.5" />
                          pending_human
                        </span>
                      )}
                      <span
                        data-testid={`conversation-ownership-${conv.id}`}
                        title={
                          conv.channel_visibility_scope === "personal"
                            ? bi(
                                "个人专属渠道，仅归属人与主管可见",
                                "Personal channel for its owner and managers"
                              )
                            : bi(
                                "公司公海渠道，团队共同接管",
                                "Company shared channel handled by the team"
                              )
                        }
                        className={`inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 ring-1 ${ownership.className}`}
                      >
                        <LockKeyhole className="h-2.5 w-2.5" />
                        {bi(ownership.zh, ownership.en)}
                      </span>
                      {isSlaUrgent && (
                        <span className="inline-flex items-center gap-0.5 rounded-md bg-rose-50 px-1.5 py-0.5 text-rose-700 ring-1 ring-rose-200/70 dark:bg-rose-500/10 dark:text-rose-300 dark:ring-rose-500/30">
                          <Clock className="h-2.5 w-2.5" />
                          SLA
                        </span>
                      )}
                    </div>
                  </div>
                </div>
              </button>
            );
          })
        )}
      </div>
    </div>
  );
}
