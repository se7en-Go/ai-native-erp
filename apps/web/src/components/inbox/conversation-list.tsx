"use client";

import React from "react";
import Link from "next/link";
import { AlertTriangle, Clock, LockKeyhole, Search } from "lucide-react";
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

/** 买家首字母彩色头像：按名称稳定散列到 21st.dev 常用柔和色板。 */
const AVATAR_PALETTE = [
  "bg-blue-100 text-blue-700 ring-blue-200",
  "bg-emerald-100 text-emerald-700 ring-emerald-200",
  "bg-violet-100 text-violet-700 ring-violet-200",
  "bg-amber-100 text-amber-800 ring-amber-200",
  "bg-rose-100 text-rose-700 ring-rose-200",
  "bg-cyan-100 text-cyan-700 ring-cyan-200",
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

/** 渠道展示色：与左侧渠道栏保持一致，让会话列表一眼可辨来源。 */
const CHANNEL_DOT: Record<string, string> = {
  whatsapp_cloud: "bg-emerald-500",
  whatsapp: "bg-emerald-500",
  email: "bg-blue-500",
  made_in_china_email: "bg-blue-500",
  web_livechat: "bg-violet-500",
  shopify: "bg-lime-500",
  woocommerce: "bg-purple-500",
  shopline: "bg-sky-500",
  shoplazza: "bg-indigo-500",
  tiktok_shop: "bg-slate-700",
  telegram: "bg-sky-500",
  messenger: "bg-blue-600",
  instagram_dm: "bg-rose-500",
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
      className: "bg-emerald-50 text-emerald-800 ring-emerald-200/80",
    };
  }
  if (conv.assigned_user_name) {
    return {
      zh: `负责人 · ${conv.assigned_user_name}`,
      en: `Owner · ${conv.assigned_user_name}`,
      className: "bg-indigo-50 text-indigo-700 ring-indigo-200/80",
    };
  }
  return {
    zh: "公司公海",
    en: "Company shared",
    className: "bg-slate-100 text-slate-700 ring-slate-200/80",
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
}) {
  const { bi } = useI18n();
  const unreadTotal = conversations.reduce((total, conv) => total + conv.unread_count, 0);

  return (
    <div
      data-testid="inbox-conversation-list"
      className="flex w-72 shrink-0 flex-col border-r border-slate-200/90 bg-white xl:w-80"
    >
      <div className="space-y-2 border-b border-slate-200/90 p-2.5">
        <div className="flex items-center justify-between gap-2 px-0.5">
          <span className="text-[12.5px] font-semibold text-slate-800">
            {bi("会话列表", "Conversations")}
          </span>
          {unreadTotal > 0 && (
            <span className="rounded-full bg-rose-50 px-2 py-0.5 text-[10.5px] font-bold tabular-nums text-rose-700 ring-1 ring-rose-200">
              {unreadTotal} {bi("条未读", "unread")}
            </span>
          )}
        </div>

        <div className="relative">
          <Search className="pointer-events-none absolute top-1/2 left-2.5 h-3.5 w-3.5 -translate-y-1/2 text-slate-400" />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => onSearchChange(e.target.value)}
            placeholder={bi("搜索买家、订单号或邮箱…", "Search buyer, order or email…")}
            aria-label={bi("搜索会话", "Search conversations")}
            className="w-full rounded-xl border border-slate-200 bg-slate-50/80 py-1.5 pr-2.5 pl-8 text-[12px] text-slate-800 placeholder:text-slate-400 focus:border-blue-500 focus:bg-white focus:outline-none focus:ring-[3px] focus:ring-blue-500/12"
          />
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
                    : "bg-slate-100/90 text-slate-600 hover:bg-slate-200/80 hover:text-slate-900"
                }`}
              >
                {bi(tab.zh, tab.label)}
              </button>
            );
          })}
        </div>

        {headerSlot}
      </div>

      <div className="flex-1 divide-y divide-slate-100/90 overflow-y-auto">
        {conversations.length === 0 ? (
          <div className="m-3 space-y-2 rounded-2xl border border-dashed border-slate-300 bg-slate-50/70 p-6 text-center text-[12px] font-medium text-slate-600">
            <div>{bi("没有符合筛选条件的会话。", "No conversations match this filter.")}</div>
            <div className="text-[11px] font-normal text-slate-500">
              {bi(
                "还没有接到客户消息？先绑定一个渠道，客户回复会直接进来。",
                "No inbound messages yet? Bind a channel first and replies land here."
              )}
            </div>
            <Link
              href="/settings/channels"
              data-testid="inbox-quick-connect-link"
              className="inline-flex items-center gap-1 rounded-lg border border-emerald-200 bg-emerald-50 px-2 py-1 text-[11px] font-semibold text-emerald-800 transition-colors hover:bg-emerald-100"
            >
              {bi("快捷绑定渠道 →", "Quick connect a channel →")}
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
                    ? "border-blue-600 bg-blue-50/70"
                    : "border-transparent hover:bg-slate-50"
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
                      <span className="truncate text-[13px] font-semibold text-slate-900">
                        {conv.customer_name}
                        {conv.country_code ? ` (${conv.country_code})` : ""}
                      </span>
                      {conv.unread_count > 0 && (
                        <span className="shrink-0 rounded-full bg-rose-500 px-1.5 py-0.5 text-[10px] font-bold tabular-nums text-white">
                          {conv.unread_count}
                        </span>
                      )}
                    </div>

                    <p className="mt-0.5 line-clamp-2 text-[12px] leading-5 text-slate-600">
                      {conv.last_message_preview}
                    </p>

                    <div className="mt-1.5 flex flex-wrap items-center gap-1 text-[10px] font-medium">
                      <span className="inline-flex items-center gap-1 rounded-md bg-slate-100 px-1.5 py-0.5 text-slate-700 ring-1 ring-slate-200/70">
                        <span
                          aria-hidden="true"
                          className={`h-1.5 w-1.5 rounded-full ${
                            CHANNEL_DOT[conv.channel_type] ?? "bg-slate-400"
                          }`}
                        />
                        {conv.channel_type}
                      </span>
                      <span className="rounded-md bg-blue-50 px-1.5 py-0.5 text-blue-700 ring-1 ring-blue-100">
                        {conv.conversation_mode}
                      </span>
                      {conv.status === "pending_human" && (
                        <span className="inline-flex items-center gap-0.5 rounded-md bg-amber-50 px-1.5 py-0.5 text-amber-800 ring-1 ring-amber-200/70">
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
                        <span className="inline-flex items-center gap-0.5 rounded-md bg-rose-50 px-1.5 py-0.5 text-rose-700 ring-1 ring-rose-200/70">
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
