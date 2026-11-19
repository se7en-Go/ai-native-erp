"use client";

import React from "react";
import { AlertTriangle, Clock, Search } from "lucide-react";
import type { ChannelType, ConversationMode, ConversationStatus } from "@/lib/contracts";

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

const FILTER_TABS: Array<{ key: ConversationFilterTab; label: string }> = [
  { key: "all", label: "all" },
  { key: "unread", label: "unread" },
  { key: "pending_human", label: "pending_human" },
  { key: "sla_urgent", label: "sla_urgent" },
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
  return (
    <div
      data-testid="inbox-conversation-list"
      className="flex w-80 shrink-0 flex-col border-r border-slate-200 bg-white"
    >
      <div className="border-b border-slate-200 p-2.5">
        <div className="relative">
          <Search className="pointer-events-none absolute top-2.5 left-2.5 h-3.5 w-3.5 text-slate-400" />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => onSearchChange(e.target.value)}
            placeholder="Search buyer, order, or email..."
            className="w-full rounded-md border border-slate-200 py-1.5 pr-3 pl-8 text-xs focus:border-indigo-500 focus:outline-none"
          />
        </div>

        <div className="mt-2 grid grid-cols-4 gap-1">
          {FILTER_TABS.map((tab) => {
            const active = activeFilter === tab.key;
            return (
              <button
                key={tab.key}
                type="button"
                aria-label={`Filter ${tab.key === "all" ? "everything" : tab.key}`}
                onClick={() => onSelectFilter(tab.key)}
                className={`truncate rounded px-1.5 py-1 text-[11px] font-medium ${
                  active
                    ? "bg-indigo-600 text-white"
                    : "bg-slate-100 text-slate-700 hover:bg-slate-200"
                }`}
              >
                {tab.label}
              </button>
            );
          })}
        </div>
      </div>

      <div className="flex-1 divide-y divide-slate-100 overflow-y-auto">
        {conversations.length === 0 ? (
          <div className="p-6 text-center text-xs text-slate-400">No conversations match filter.</div>
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
                className={`block w-full p-3 text-left transition-colors ${
                  selected ? "bg-indigo-50/70" : "hover:bg-slate-50"
                }`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-sm font-semibold text-slate-900">
                    {conv.customer_name} {conv.country_code ? `(${conv.country_code})` : ""}
                  </span>
                  {conv.unread_count > 0 && (
                    <span className="rounded-full bg-rose-600 px-1.5 py-0.5 text-[10px] font-semibold text-white">
                      {conv.unread_count}
                    </span>
                  )}
                </div>

                <p className="mt-1 line-clamp-2 text-xs text-slate-600">
                  {conv.last_message_preview}
                </p>

                <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[10px]">
                  <span className="rounded bg-slate-100 px-1.5 py-0.5 font-medium text-slate-700">
                    {conv.channel_type}
                  </span>
                  <span className="rounded bg-blue-50 px-1.5 py-0.5 font-medium text-blue-700">
                    {conv.conversation_mode}
                  </span>
                  {conv.status === "pending_human" && (
                    <span className="inline-flex items-center gap-0.5 rounded bg-amber-100 px-1.5 py-0.5 font-semibold text-amber-800">
                      <AlertTriangle className="h-3 w-3" />
                      pending_human
                    </span>
                  )}
                  {isSlaUrgent && (
                    <span className="inline-flex items-center gap-0.5 rounded bg-rose-100 px-1.5 py-0.5 font-semibold text-rose-800">
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
