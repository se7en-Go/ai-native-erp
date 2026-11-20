"use client";

import React from "react";
import { AlertTriangle, Clock, Search } from "lucide-react";
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

export function ConversationList({
  conversations,
  selectedConversationId,
  activeFilter,
  searchQuery,
  onSelectFilter,
  onSearchChange,
  onSelectConversation,
}: {
  conversations: ConversationSummary[];
  selectedConversationId: string | null;
  activeFilter: ConversationFilterTab;
  searchQuery: string;
  onSelectFilter: (filter: ConversationFilterTab) => void;
  onSearchChange: (value: string) => void;
  onSelectConversation: (conversationId: string) => void;
}) {
  const { bi } = useI18n();
  return (
    <div
      data-testid="inbox-conversation-list"
      className="flex w-72 shrink-0 flex-col border-r border-slate-200 bg-white xl:w-80"
    >
      <div className="border-b border-slate-200 p-2.5">
        <div className="relative">
          <Search className="pointer-events-none absolute top-2.5 left-2.5 h-3.5 w-3.5 text-slate-400" />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => onSearchChange(e.target.value)}
            placeholder={bi("搜索买家、订单号或邮箱…", "Search buyer, order or email…")}
            aria-label={bi("搜索会话", "Search conversations")}
            className="erp-input py-1.5 pl-8 text-[12px]"
          />
        </div>

        <div className="mt-2 grid grid-cols-4 gap-1">
          {FILTER_TABS.map((tab) => {
            const active = activeFilter === tab.key;
            return (
              <button
                key={tab.key}
                type="button"
                title={bi(tab.titleZh, tab.label)}
                aria-label={`Filter ${tab.key === "all" ? "everything" : tab.key}`}
                onClick={() => onSelectFilter(tab.key)}
                className={`truncate rounded-md px-1.5 py-1 text-[11px] font-medium transition-colors ${
                  active
                    ? "bg-blue-600 text-white"
                    : "bg-slate-100 text-slate-600 hover:bg-slate-200"
                }`}
              >
                {bi(tab.zh, tab.label)}
              </button>
            );
          })}
        </div>
      </div>

      <div className="flex-1 divide-y divide-slate-100 overflow-y-auto">
        {conversations.length === 0 ? (
          <div className="p-6 text-center text-[12px] text-slate-400">
            {bi("没有符合筛选条件的会话。", "No conversations match this filter.")}
          </div>
        ) : (
          conversations.map((conv) => {
            const selected = conv.id === selectedConversationId;
            const isSlaUrgent =
              conv.sla_due_at !== null &&
              conv.sla_due_at !== undefined &&
              new Date(conv.sla_due_at).getTime() - Date.now() < 1000 * 60 * 60 * 4;

            return (
              <button
                key={conv.id}
                type="button"
                aria-label={`${conv.customer_name} — ${conv.last_message_preview}`}
                data-testid={`conversation-item-${conv.id}`}
                onClick={() => onSelectConversation(conv.id)}
                className={`block w-full border-l-2 p-3 text-left transition-colors ${
                  selected
                    ? "border-blue-500 bg-blue-50/60"
                    : "border-transparent hover:bg-slate-50"
                }`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-[13px] font-medium text-slate-900">
                    {conv.customer_name}
                    {conv.country_code ? ` (${conv.country_code})` : ""}
                  </span>
                  {conv.unread_count > 0 && (
                    <span className="rounded-full bg-rose-500 px-1.5 py-0.5 text-[10px] font-medium tabular-nums text-white">
                      {conv.unread_count}
                    </span>
                  )}
                </div>

                <p className="mt-1 line-clamp-2 text-[12px] leading-5 text-slate-600">
                  {conv.last_message_preview}
                </p>

                <div className="mt-1.5 flex flex-wrap items-center gap-1 text-[10px]">
                  <span className="rounded bg-slate-100 px-1.5 py-0.5 text-slate-600">
                    {conv.channel_type}
                  </span>
                  <span className="rounded bg-blue-50 px-1.5 py-0.5 text-blue-700">
                    {conv.conversation_mode}
                  </span>
                  {conv.status === "pending_human" && (
                    <span className="inline-flex items-center gap-0.5 rounded bg-amber-50 px-1.5 py-0.5 font-medium text-amber-800">
                      <AlertTriangle className="h-3 w-3" />
                      pending_human
                    </span>
                  )}
                  {isSlaUrgent && (
                    <span className="inline-flex items-center gap-0.5 rounded bg-rose-50 px-1.5 py-0.5 font-medium text-rose-700">
                      <Clock className="h-3 w-3" />
                      SLA
                    </span>
                  )}
                </div>
              </button>
            );
          })
        )}
      </div>
    </div>
  );
}
