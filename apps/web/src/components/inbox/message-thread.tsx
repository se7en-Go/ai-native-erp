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
  const { locale, t } = useI18n();
  const [expandedTranslations, setExpandedTranslations] = useState<Record<string, boolean>>({});

  if (!conversation) {
    return (
      <div className="flex flex-1 items-center justify-center bg-slate-50 text-sm text-slate-400">
        Select a conversation to begin.
      </div>
    );
  }

  const localTimeInfo = computeBuyerLocalTimeInfo(conversation.iana_timezone);

  function toggleTranslation(msgId: string) {
    setExpandedTranslations((prev) => ({ ...prev, [msgId]: !prev[msgId] }));
  }

  return (
    <div data-testid="inbox-message-thread" className="flex flex-1 flex-col bg-slate-50">
      <div className="flex items-center justify-between border-b border-slate-200 bg-white px-4 py-3">
        <div className="flex flex-wrap items-center gap-2.5">
          <h2 className="text-sm font-bold text-slate-900">{conversation.customer_name}</h2>
          {conversation.country_code && (
            <span className="rounded bg-slate-100 px-1.5 py-0.5 text-xs font-medium text-slate-700">
              {conversation.country_code}
            </span>
          )}
          {localTimeInfo && (
            <span
              data-testid="buyer-local-time-badge"
              className="inline-flex items-center gap-1 rounded-full border border-slate-200 bg-slate-50 px-2.5 py-0.5 text-xs text-slate-700"
            >
              <Clock className="h-3 w-3 text-slate-500" />
              <span>
                {t("localTime")}: {localTimeInfo.formattedTime} ({localTimeInfo.ianaTimezone})
              </span>
            </span>
          )}
          {localTimeInfo?.isQuietHours && (
            <span
              data-testid="quiet-hours-pill"
              className="inline-flex items-center gap-1 rounded-full bg-amber-100 px-2.5 py-0.5 text-xs font-semibold text-amber-900"
            >
              {t("quietHours")} (21:00–08:00)
            </span>
          )}
        </div>

        <button
          type="button"
          onClick={onToggleDrawer}
          className="inline-flex items-center gap-1 rounded-md border border-slate-200 px-2.5 py-1 text-xs font-medium text-slate-700 hover:bg-slate-100"
        >
          <PanelRight className="h-3.5 w-3.5" />
          <span>Context</span>
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
                className={`max-w-2xl rounded-lg border px-3.5 py-2.5 text-sm shadow-xs ${
                  isInternalNote
                    ? "border-amber-300 bg-amber-50 text-amber-950"
                    : isOutbound
                    ? "border-indigo-600 bg-indigo-600 text-white"
                    : "border-slate-200 bg-white text-slate-900"
                }`}
              >
                <div className="mb-1 flex items-center gap-2 text-[11px] opacity-80">
                  {isInternalNote && <StickyNote className="h-3 w-3" />}
                  <span className="font-semibold">{msg.sender_type}</span>
                  {msg.detected_language && (
                    <span className="inline-flex items-center gap-0.5 rounded bg-black/10 px-1 py-0.2">
                      <Globe className="h-2.5 w-2.5" />
                      [{msg.detected_language}]
                    </span>
                  )}
                  <span>{new Date(msg.source_timestamp).toLocaleTimeString()}</span>
                </div>

                <p className="whitespace-pre-wrap">{msg.body_text}</p>

                {translationText && (
                  <div className="mt-2 border-t border-black/10 pt-1.5">
                    <button
                      type="button"
                      data-testid={`toggle-msg-translation-${msg.id}`}
                      onClick={() => toggleTranslation(msg.id)}
                      className="inline-flex items-center gap-1 text-xs font-medium underline"
                    >
                      {isExpanded ? <EyeOff className="h-3 w-3" /> : <Eye className="h-3 w-3" />}
                      <span>{isExpanded ? t("hideTranslation") : t("showTranslation")}</span>
                    </button>
                    {isExpanded && <p className="mt-1 text-xs opacity-90">{translationText}</p>}
                  </div>
                )}

                {isOutbound && (
                  <div className="mt-1 flex items-center justify-end gap-1 text-[10px] opacity-80">
                    <Lock className="h-2.5 w-2.5" />
                    <span>{msg.delivery_status}</span>
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
