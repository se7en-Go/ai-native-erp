"use client";

import React, { useState } from "react";
import {
  Clock,
  Download,
  Eye,
  EyeOff,
  Globe,
  Languages,
  Loader2,
  Lock,
  PanelRight,
  Pin,
  StickyNote,
  Tag,
} from "lucide-react";
import {
  initialsOf,
  type ConversationSummary,
} from "@/components/inbox/conversation-list";
import {
  computeBuyerLocalTimeInfo,
  type ChannelType,
  type DeliveryStatus,
  type MessageContentType,
  type MessageDirection,
  type MessageSenderType,
} from "@/lib/contracts";
import {
  formatAttachmentSize,
  inboxAttachmentUrl,
  translateBubbleText,
} from "@/lib/api-client/pack-b-inbox";
import { useI18n } from "@/lib/i18n";

/** 附件投影（契约 attachments 为自由 JSON，后端实际下发这四个字段）。 */
export interface InboxAttachment {
  filename?: string;
  mime_type?: string;
  size_bytes?: number;
  download_url?: string;
  storage_key?: string | null;
}

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
  /** Pack B：图片 / PDF / 表格附件（缺省即纯文本消息）。 */
  attachments?: InboxAttachment[];
}

function isImageAttachment(attachment: InboxAttachment): boolean {
  if (attachment.mime_type?.startsWith("image/")) return true;
  return /\.(png|jpe?g|gif|webp|bmp|svg|heic)$/i.test(attachment.filename ?? "");
}

export function MessageThread({
  conversation,
  messages,
  onToggleDrawer,
  headerActions,
}: {
  conversation: ConversationSummary | null;
  messages: InboxMessage[];
  onToggleDrawer: () => void;
  /** 会话级操作槽（置顶 / 标签管理），由 /inbox 页面注入。 */
  headerActions?: React.ReactNode;
}) {
  const { locale, t, bi } = useI18n();
  const [expandedTranslations, setExpandedTranslations] = useState<Record<string, boolean>>({});
  // Pack B：一键气泡翻译的结果缓存（按 message.id），以及每条的加载/隐藏状态。
  const [bubbleTranslations, setBubbleTranslations] = useState<Record<string, string>>({});
  const [hiddenBubbleTranslations, setHiddenBubbleTranslations] = useState<
    Record<string, boolean>
  >({});
  const [translatingId, setTranslatingId] = useState<string | null>(null);
  const [translateError, setTranslateError] = useState<Record<string, string>>({});

  if (!conversation) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-2 bg-gray-50 text-[13px] font-medium text-gray-600 dark:bg-gray-950 dark:text-gray-400">
        <span className="grid h-12 w-12 place-items-center rounded-2xl border border-dashed border-gray-300 bg-white text-gray-400 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-500">
          <PanelRight className="h-5 w-5" />
        </span>
        {bi("选择左侧一条会话开始沟通。", "Select a conversation to begin.")}
      </div>
    );
  }

  const localTimeInfo = computeBuyerLocalTimeInfo(conversation.iana_timezone);

  function toggleTranslation(msgId: string) {
    setExpandedTranslations((prev) => ({ ...prev, [msgId]: !prev[msgId] }));
  }

  async function runBubbleTranslation(msg: InboxMessage) {
    if (!msg.body_text.trim()) return;
    setTranslatingId(msg.id);
    setTranslateError((prev) => {
      const next = { ...prev };
      delete next[msg.id];
      return next;
    });
    try {
      const translated = await translateBubbleText(msg.body_text, "zh");
      setBubbleTranslations((prev) => ({ ...prev, [msg.id]: translated }));
      setHiddenBubbleTranslations((prev) => ({ ...prev, [msg.id]: false }));
    } catch (error) {
      setTranslateError((prev) => ({
        ...prev,
        [msg.id]:
          error instanceof Error && error.message
            ? error.message
            : bi("翻译失败，请重试。", "Translation failed. Please retry."),
      }));
    } finally {
      setTranslatingId(null);
    }
  }

  const conversationTags = conversation.tags ?? [];

  return (
    <div
      data-testid="inbox-message-thread"
      className="flex min-h-0 flex-1 flex-col bg-gray-50 dark:bg-gray-950"
    >
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-gray-200 bg-white px-4 py-2.5 dark:border-gray-800 dark:bg-gray-900">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <span
            aria-hidden="true"
            className="grid h-8 w-8 shrink-0 place-items-center rounded-full bg-blue-100 text-[11.5px] font-bold text-blue-700 ring-1 ring-blue-200 dark:bg-blue-500/15 dark:text-blue-300 dark:ring-blue-500/30"
          >
            {initialsOf(conversation.customer_name)}
          </span>
          <h2 className="truncate text-[14px] font-bold text-gray-900 dark:text-gray-100">
            {conversation.customer_name}
          </h2>
          {conversation.is_pinned && (
            <Pin
              aria-hidden="true"
              data-testid="conversation-header-pin"
              className="h-3.5 w-3.5 shrink-0 rotate-45 text-blue-600"
            />
          )}
          {conversation.country_code && (
            <span className="rounded-md bg-gray-100 px-1.5 py-0.5 text-[10.5px] font-semibold text-gray-700 ring-1 ring-gray-200/70 dark:bg-gray-800 dark:text-gray-200 dark:ring-gray-700">
              {conversation.country_code}
            </span>
          )}
          {conversation.channel_type && (
            <span className="rounded-md bg-white px-1.5 py-0.5 text-[10.5px] font-medium text-gray-600 ring-1 ring-gray-200 dark:bg-gray-900 dark:text-gray-300 dark:ring-gray-700">
              {conversation.channel_type}
            </span>
          )}
          {conversationTags.length > 0 && (
            <span className="flex flex-wrap items-center gap-1">
              {conversationTags.map((tag) => (
                <span
                  key={tag}
                  data-testid={`conversation-header-tag-${tag}`}
                  className="inline-flex items-center gap-0.5 rounded-md bg-blue-50 px-1.5 py-0.5 text-[10.5px] font-semibold text-blue-700 ring-1 ring-blue-200/80 dark:bg-blue-500/10 dark:text-blue-300 dark:ring-blue-500/30"
                >
                  <Tag className="h-2.5 w-2.5" />
                  {tag}
                </span>
              ))}
            </span>
          )}
          {localTimeInfo && (
            <span
              data-testid="buyer-local-time-badge"
              className="inline-flex items-center gap-1 rounded-full border border-gray-200 bg-gray-50 px-2 py-0.5 text-[11px] font-medium tabular-nums text-gray-700 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300"
            >
              <Clock className="h-3 w-3 text-gray-500" />
              <span>
                {t("localTime")} {localTimeInfo.formattedTime} ({localTimeInfo.ianaTimezone})
              </span>
            </span>
          )}
          {localTimeInfo?.isQuietHours && (
            <span
              data-testid="quiet-hours-pill"
              className="inline-flex items-center gap-1 rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-[11px] font-semibold text-amber-800 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-300"
            >
              {t("quietHours")} (21:00–08:00)
            </span>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-1.5">
          {headerActions}
          <button
            type="button"
            onClick={onToggleDrawer}
            className="inline-flex items-center gap-1 rounded-xl border border-gray-200 bg-white px-3 py-1 text-[12px] font-medium text-gray-700 transition-colors hover:border-blue-200 hover:bg-blue-50 hover:text-blue-700 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300 dark:hover:border-blue-500/40 dark:hover:bg-blue-500/10 dark:hover:text-blue-300"
          >
            <PanelRight className="h-3.5 w-3.5" />
            <span>{bi("客户上下文", "Context")}</span>
          </button>
        </div>
      </div>

      <div className="flex-1 space-y-3.5 overflow-y-auto px-4 py-4">
        {messages.map((msg) => {
          const isInternalNote = msg.direction === "internal_note";
          const isOutbound = msg.direction === "outbound";
          const isInboundCustomer = msg.direction === "inbound";
          const translationText =
            locale === "zh" ? msg.translated_text_zh : msg.translated_text_en;
          const isExpanded = Boolean(expandedTranslations[msg.id]);
          const bubbleTranslation = bubbleTranslations[msg.id];
          const bubbleTranslationHidden = Boolean(hiddenBubbleTranslations[msg.id]);
          const attachments = msg.attachments ?? [];

          return (
            <div
              key={msg.id}
              data-testid={`message-bubble-${msg.id}`}
              className={`flex flex-col ${isOutbound ? "items-end" : "items-start"}`}
            >
              <div
                className={`max-w-2xl rounded-2xl border px-3.5 py-2.5 text-[13px] leading-6 ${
                  isInternalNote
                    ? "border-amber-200 bg-amber-50/90 text-amber-950 shadow-[0_1px_2px_rgba(180,83,9,0.08)] dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-100"
                    : isOutbound
                      ? "rounded-br-md border-blue-600 bg-blue-600 text-white shadow-[0_6px_18px_-8px_rgba(37,99,235,0.45)]"
                      : "rounded-bl-md border-gray-200 bg-white text-gray-900 shadow-[0_2px_8px_-4px_rgba(15,23,42,0.18)] dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100"
                }`}
              >
                <div
                  className={`mb-1 flex items-center gap-2 text-[10.5px] font-medium ${
                    isInternalNote
                      ? "text-amber-800 dark:text-amber-300"
                      : isOutbound
                        ? "text-blue-100"
                        : "text-gray-500 dark:text-gray-400"
                  }`}
                >
                  {isInternalNote && <StickyNote className="h-3 w-3" />}
                  <span>{msg.sender_type}</span>
                  {msg.detected_language && (
                    <span
                      className={`inline-flex items-center gap-0.5 rounded px-1 py-px ring-1 ${
                        isOutbound
                          ? "bg-white/15 text-white ring-white/25"
                          : "bg-gray-100 text-gray-700 ring-gray-200 dark:bg-gray-700 dark:text-gray-200 dark:ring-gray-600"
                      }`}
                    >
                      <Globe className="h-2.5 w-2.5" />
                      [{msg.detected_language}]
                    </span>
                  )}
                  <span className="tabular-nums">
                    {new Date(msg.source_timestamp).toLocaleTimeString()}
                  </span>
                </div>

                <p className="whitespace-pre-wrap">{msg.body_text}</p>

                {attachments.length > 0 && (
                  <div
                    data-testid={`message-attachments-${msg.id}`}
                    className="mt-2 flex flex-col gap-1.5"
                  >
                    {attachments.map((attachment, index) => {
                      const href = inboxAttachmentUrl(attachment.download_url);
                      const label =
                        attachment.filename ||
                        bi(`附件 ${index + 1}`, `Attachment ${index + 1}`);
                      if (isImageAttachment(attachment) && href) {
                        return (
                          <a
                            key={`${msg.id}-attachment-${index}`}
                            data-testid={`message-attachment-image-${msg.id}-${index}`}
                            href={href}
                            target="_blank"
                            rel="noreferrer noopener"
                            title={bi("点击查看原图", "Open full image")}
                            className="block w-fit overflow-hidden rounded-xl border border-gray-200 bg-gray-50 shadow-[0_2px_8px_-4px_rgba(15,23,42,0.25)] transition-transform duration-150 hover:scale-[1.01] dark:border-gray-700 dark:bg-gray-900"
                          >
                            {/* 契约附件地址需带 Cookie 走 BFF，故不用 next/image 优化器。 */}
                            <img
                              src={href}
                              alt={label}
                              className="max-h-52 max-w-[260px] object-cover"
                            />
                          </a>
                        );
                      }
                      const sizeLabel = formatAttachmentSize(attachment.size_bytes);
                      return (
                        <a
                          key={`${msg.id}-attachment-${index}`}
                          data-testid={`message-attachment-file-${msg.id}-${index}`}
                          href={href || undefined}
                          download={attachment.filename || undefined}
                          target="_blank"
                          rel="noreferrer noopener"
                          className={`inline-flex w-fit max-w-full items-center gap-1.5 rounded-xl border px-2 py-1 text-[11.5px] font-semibold transition-colors ${
                            isOutbound
                              ? "border-white/25 bg-white/10 text-white hover:bg-white/15"
                              : "border-gray-200 bg-gray-50 text-gray-800 hover:bg-gray-100 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200 dark:hover:bg-gray-800"
                          }`}
                        >
                          <Download className="h-3 w-3 shrink-0" />
                          <span className="max-w-[200px] truncate">{label}</span>
                          {sizeLabel && (
                            <span className="shrink-0 tabular-nums opacity-70">
                              {sizeLabel}
                            </span>
                          )}
                        </a>
                      );
                    })}
                  </div>
                )}

                {translationText && (
                  <div
                    className={`mt-2 border-t pt-1.5 ${
                      isOutbound ? "border-white/25" : "border-gray-200 dark:border-gray-700"
                    }`}
                  >
                    <button
                      type="button"
                      data-testid={`toggle-msg-translation-${msg.id}`}
                      onClick={() => toggleTranslation(msg.id)}
                      className="inline-flex items-center gap-1 text-[11px] font-semibold underline decoration-dotted underline-offset-2"
                    >
                      {isExpanded ? <EyeOff className="h-3 w-3" /> : <Eye className="h-3 w-3" />}
                      <span>{isExpanded ? t("hideTranslation") : t("showTranslation")}</span>
                    </button>
                    {isExpanded && (
                      <p
                        className={`mt-1 rounded-lg px-2 py-1.5 text-[12px] ${
                          isOutbound
                            ? "bg-white/12 text-white"
                            : "bg-gray-50 text-gray-800 dark:bg-gray-900 dark:text-gray-200"
                        }`}
                      >
                        {translationText}
                      </p>
                    )}
                  </div>
                )}

                {isOutbound && (
                  <div className="mt-1 flex items-center justify-end gap-1 text-[10px] font-medium text-blue-100">
                    <Lock className="h-2.5 w-2.5" />
                    <span>{msg.delivery_status}</span>
                  </div>
                )}
              </div>

              {isInboundCustomer && msg.body_text.trim().length > 0 && (
                <div className="mt-1 flex flex-col items-start gap-1">
                  <button
                    type="button"
                    data-testid={`translate-msg-${msg.id}`}
                    aria-label={bi("把这条买家消息译成中文", "Translate this buyer message to Chinese")}
                    disabled={translatingId === msg.id}
                    onClick={() => void runBubbleTranslation(msg)}
                    className="inline-flex items-center gap-1 rounded-lg border border-blue-200 bg-blue-50/80 px-2 py-0.5 text-[11px] font-semibold text-blue-700 transition-colors hover:bg-blue-100 disabled:opacity-60 dark:border-blue-500/30 dark:bg-blue-500/10 dark:text-blue-300 dark:hover:bg-blue-500/15"
                  >
                    {translatingId === msg.id ? (
                      <Loader2 className="h-3 w-3 animate-spin" />
                    ) : (
                      <Languages className="h-3 w-3" />
                    )}
                    <span>{bi("译成中文", "Translate to ZH")}</span>
                  </button>

                  {translateError[msg.id] && (
                    <p
                      role="alert"
                      data-testid={`translate-msg-error-${msg.id}`}
                      className="rounded-lg border border-rose-200 bg-rose-50 px-2 py-0.5 text-[11px] font-medium text-rose-700 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-300"
                    >
                      {translateError[msg.id]}
                    </p>
                  )}

                  {bubbleTranslation && !bubbleTranslationHidden && (
                    <div
                      data-testid={`translated-bubble-${msg.id}`}
                      className="max-w-2xl rounded-2xl rounded-tl-md border border-blue-200 bg-blue-50/85 px-3 py-1.5 text-[12.5px] leading-6 text-blue-950 shadow-[0_2px_8px_-4px_rgba(37,99,235,0.25)] dark:border-blue-500/30 dark:bg-blue-500/10 dark:text-blue-100"
                    >
                      <div className="mb-0.5 flex items-center gap-1.5 text-[10px] font-semibold text-blue-700 dark:text-blue-300">
                        <Languages className="h-2.5 w-2.5" />
                        <span>{bi("中文译文", "Chinese translation")}</span>
                        <button
                          type="button"
                          data-testid={`hide-translated-bubble-${msg.id}`}
                          onClick={() =>
                            setHiddenBubbleTranslations((prev) => ({
                              ...prev,
                              [msg.id]: true,
                            }))
                          }
                          className="ml-auto inline-flex items-center gap-0.5 rounded px-1 py-px font-semibold text-blue-700/80 hover:bg-blue-100 hover:text-blue-900 dark:text-blue-300/90 dark:hover:bg-blue-500/15 dark:hover:text-blue-200"
                        >
                          <EyeOff className="h-2.5 w-2.5" />
                          {bi("隐藏", "Hide")}
                        </button>
                      </div>
                      <p className="whitespace-pre-wrap">{bubbleTranslation}</p>
                    </div>
                  )}

                  {bubbleTranslation && bubbleTranslationHidden && (
                    <button
                      type="button"
                      data-testid={`show-translated-bubble-${msg.id}`}
                      onClick={() =>
                        setHiddenBubbleTranslations((prev) => ({
                          ...prev,
                          [msg.id]: false,
                        }))
                      }
                      className="inline-flex items-center gap-1 text-[11px] font-semibold text-blue-700 underline decoration-dotted underline-offset-2 dark:text-blue-300"
                    >
                      <Eye className="h-3 w-3" />
                      {bi("显示译文", "Show translation")}
                    </button>
                  )}
                </div>
              )}
            </div>
          );
        })}
        {messages.length === 0 && (
          <p className="rounded-2xl border border-dashed border-gray-300 bg-white/70 py-8 text-center text-[12px] font-medium text-gray-600 dark:border-gray-700 dark:bg-gray-900/60 dark:text-gray-400">
            {bi("该会话还没有消息。", "No messages in this conversation yet.")}
          </p>
        )}
      </div>
    </div>
  );
}
