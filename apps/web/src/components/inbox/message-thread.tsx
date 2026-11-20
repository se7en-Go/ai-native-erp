"use client";

import React, { useState } from "react";
import { Clock, Eye, EyeOff, Globe, Lock, PanelRight, StickyNote } from "lucide-react";
import type { ConversationSummary } from "@/components/inbox/conversation-list";
import {
  computeBuyerLocalTimeInfo,
  type ChannelType,
  type DeliveryStatus,
  type MessageContentType,
  type MessageDirection,
  type MessageSenderType,
} from "@/lib/contracts";
import { useI18n } from "@/lib/i18n";

export interface InboxMessage {
  id: string;
  conversation_id: string;
  channel_type: ChannelType;
  direction: MessageDirection;
  sender_type: MessageSenderType;
  content_type: MessageContentType;
  body_text: string;
  body_sanitized_html?: string | null;
  detected_language?: string | null;
  translated_text_zh?: string | null;
  translated_text_en?: string | null;
  delivery_status: DeliveryStatus;
  source_timestamp: string;
}

export function MessageThread({
  conversation,
  messages,
  onToggleDrawer,
}: {
  conversation: ConversationSummary | null;
  messages: InboxMessage[];
  onToggleDrawer: () => void;
}) {
  const { locale, t, bi } = useI18n();
  const [expandedTranslations, setExpandedTranslations] = useState<Record<string, boolean>>({});

  if (!conversation) {
    return (
      <div className="flex flex-1 items-center justify-center bg-slate-50 text-[13px] text-slate-400">
        {bi("选择左侧一条会话开始沟通。", "Select a conversation to begin.")}
      </div>
    );
  }

  const localTimeInfo = computeBuyerLocalTimeInfo(conversation.iana_timezone);

  function toggleTranslation(msgId: string) {
    setExpandedTranslations((prev) => ({ ...prev, [msgId]: !prev[msgId] }));
  }

  return (
    <div data-testid="inbox-message-thread" className="flex min-h-0 flex-1 flex-col bg-slate-50">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 bg-white px-4 py-3">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-sm font-semibold text-slate-900">{conversation.customer_name}</h2>
          {conversation.country_code && <span className="text-[11px] text-slate-400">{conversation.country_code}</span>}
          {localTimeInfo && (
            <span
              data-testid="buyer-local-time-badge"
              className="inline-flex items-center gap-1 rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-[11px] tabular-nums text-slate-600"
            >
              <Clock className="h-3 w-3 text-slate-400" />
              <span>
                {t("localTime")} {localTimeInfo.formattedTime} ({localTimeInfo.ianaTimezone})
              </span>
            </span>
          )}
          {localTimeInfo?.isQuietHours && (
            <span
              data-testid="quiet-hours-pill"
              className="inline-flex items-center gap-1 rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-[11px] font-medium text-amber-800"
            >
              {t("quietHours")} (21:00–08:00)
            </span>
          )}
        </div>

        <button
          type="button"
          onClick={onToggleDrawer}
          className="erp-btn erp-btn-secondary py-1 text-[12px]"
        >
          <PanelRight className="h-3.5 w-3.5" />
          <span>{bi("客户上下文", "Context")}</span>
        </button>
      </div>

      <div className="flex-1 space-y-3 overflow-y-auto p-4">
        {messages.map((msg) => {
          const isInternalNote = msg.direction === "internal_note";
          const isOutbound = msg.direction === "outbound";
          const translationText =
            locale === "zh" ? msg.translated_text_zh : msg.translated_text_en;
          const isExpanded = Boolean(expandedTranslations[msg.id]);

          return (
            <div
              key={msg.id}
              data-testid={`message-bubble-${msg.id}`}
              className={`flex flex-col ${isOutbound ? "items-end" : "items-start"}`}
            >
              <div
                className={`max-w-2xl rounded-xl border px-3.5 py-2.5 text-[13px] leading-6 shadow-[0_1px_2px_rgba(15,23,42,0.04)] ${
                  isInternalNote
                    ? "border-amber-200 bg-amber-50 text-amber-950"
                    : isOutbound
                      ? "border-blue-600 bg-blue-600 text-white"
                      : "border-slate-200 bg-white text-slate-900"
                }`}
              >
                <div className="mb-1 flex items-center gap-2 text-[11px] opacity-75">
                  {isInternalNote && <StickyNote className="h-3 w-3" />}
                  <span className="font-medium">{msg.sender_type}</span>
                  {msg.detected_language && (
                    <span className="inline-flex items-center gap-0.5 rounded bg-black/10 px-1 py-px">
                      <Globe className="h-2.5 w-2.5" />
                      [{msg.detected_language}]
                    </span>
                  )}
                  <span className="tabular-nums">
                    {new Date(msg.source_timestamp).toLocaleTimeString()}
                  </span>
                </div>

                <p className="whitespace-pre-wrap">{msg.body_text}</p>

                {translationText && (
                  <div className="mt-2 border-t border-black/10 pt-1.5">
                    <button
                      type="button"
                      data-testid={`toggle-msg-translation-${msg.id}`}
                      onClick={() => toggleTranslation(msg.id)}
                      className="inline-flex items-center gap-1 text-[11px] font-medium underline decoration-dotted"
                    >
                      {isExpanded ? <EyeOff className="h-3 w-3" /> : <Eye className="h-3 w-3" />}
                      <span>{isExpanded ? t("hideTranslation") : t("showTranslation")}</span>
                    </button>
                    {isExpanded && <p className="mt-1 text-[12px] opacity-90">{translationText}</p>}
                  </div>
                )}

                {isOutbound && (
                  <div className="mt-1 flex items-center justify-end gap-1 text-[10px] opacity-70">
                    <Lock className="h-2.5 w-2.5" />
                    <span>{msg.delivery_status}</span>
                  </div>
                )}
              </div>
            </div>
          );
        })}
        {messages.length === 0 && (
          <p className="py-8 text-center text-[12px] text-slate-400">
            {bi("该会话还没有消息。", "No messages in this conversation yet.")}
          </p>
        )}
      </div>
    </div>
  );
}
