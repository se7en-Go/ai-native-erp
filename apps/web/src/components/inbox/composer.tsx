"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowRight,
  DatabaseZap,
  FileText,
  Paperclip,
  Sparkles,
  Send,
  StickyNote,
  Languages,
  ShieldCheck,
  Info,
  MessageSquare,
  X,
} from "lucide-react";
import { sendConversationMessage } from "@/lib/api-client/live-client";
import { aiAssist } from "@/lib/api-client/plan04-bridge";
import { translateText } from "@/lib/api-client/live-client";
import {
  formatAttachmentSize,
  uploadInboxAttachment,
} from "@/lib/api-client/pack-b-inbox";
import { BffApiError } from "@/lib/contracts";
import { InboxAiToolbar } from "@/components/ai-workforce/inbox-ai-toolbar";
import type { InboxToolbarIntent, QuickReplyTemplate } from "@/components/ai-workforce/types";
import type { ConversationSummary } from "@/components/inbox/conversation-list";
import {
  isMarketplaceImChannel,
  type InboxMessage,
} from "@/components/inbox/message-thread";
import { fetchInboxAssist, fetchQuickReplies } from "@/lib/phase3b-data";
import { proposeGroundedReply } from "@/lib/data-source";
import { useI18n } from "@/lib/i18n";

/**
 * 实时翻译目标语言：覆盖外贸询盘最常用的语种。
 * 码位与 `/v1/ai/translate` 的 `target_lang`（ISO 639-1）一致。
 */
export const TARGET_LANGUAGES = [
  { code: "zh", zh: "中文 (ZH)", en: "Chinese (ZH)" },
  { code: "en", zh: "英语 (EN)", en: "English (EN)" },
  { code: "es", zh: "西班牙语 (ES)", en: "Spanish (ES)" },
  { code: "de", zh: "德语 (DE)", en: "German (DE)" },
  { code: "fr", zh: "法语 (FR)", en: "French (FR)" },
  { code: "ja", zh: "日语 (JA)", en: "Japanese (JA)" },
  { code: "ru", zh: "俄语 (RU)", en: "Russian (RU)" },
  { code: "ar", zh: "阿拉伯语 (AR)", en: "Arabic (AR)" },
  { code: "pt", zh: "葡语 (PT)", en: "Portuguese (PT)" },
  { code: "ko", zh: "韩语 (KO)", en: "Korean (KO)" },
] as const;

export type TargetLanguageCode = (typeof TARGET_LANGUAGES)[number]["code"];

/** Pack B：发图片 / PDF 的文件选择器接受的类型。 */
export const ATTACHMENT_ACCEPT = "image/*,.pdf,.xlsx,.csv,.txt";

export function InboxComposer({
  conversation,
  messages = [],
  insertRequest = null,
}: {
  conversation: ConversationSummary | null;
  messages?: readonly InboxMessage[];
  /** 外部（抽屉里的「插入报价/库存」）向草稿追加文本；每次传入新对象即插入一次。 */
  insertRequest?: { text: string; nonce: number } | null;
}) {
  const { t, bi } = useI18n();
  const queryClient = useQueryClient();

  const [bodyText, setBodyText] = useState("");
  const [mode, setMode] = useState<"outbound" | "internal_note">("outbound");
  const [liveTranslate, setLiveTranslate] = useState(false);
  const [targetLang, setTargetLang] = useState<TargetLanguageCode>("en");
  const [translatedPreview, setTranslatedPreview] = useState<string>("");
  const [strictRag, setStrictRag] = useState(false);
  const [complianceError, setComplianceError] = useState<string | null>(null);
  const [assistNotice, setAssistNotice] = useState<string | null>(null);
  const [assistError, setAssistError] = useState<string | null>(null);
  const [proposalNotice, setProposalNotice] = useState(false);
  // Pack B 附件上传状态：选中文件先以预览胶囊展示，点发送才真正上传。
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [attachmentNotice, setAttachmentNotice] = useState<string | null>(null);
  const [attachmentError, setAttachmentError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // 抽屉里点「插入报价/库存」：直接追加到当前草稿，发送仍需人工确认。
  // 用对象身份而非 nonce 判重，语言切换重渲染不会重复插入同一段文本。
  const handledInsertRef = useRef<{ text: string; nonce: number } | null>(null);
  useEffect(() => {
    if (!insertRequest || insertRequest === handledInsertRef.current) return;
    handledInsertRef.current = insertRequest;
    const snippet = insertRequest.text;
    setBodyText((previous) => (previous.trim() ? `${previous.trim()}\n\n${snippet}` : snippet));
    setAssistNotice(bi("已插入实时库存与阶梯报价片段", "Live stock & tier price inserted"));
  }, [bi, insertRequest]);

  useEffect(() => {
    if (!liveTranslate || !bodyText.trim() || mode === "internal_note") {
      setTranslatedPreview("");
      return;
    }
    const timer = setTimeout(() => {
      void translateText(bodyText, targetLang)
        .then((res) => setTranslatedPreview(res.translated_text))
        .catch(() => setTranslatedPreview(""));
    }, 150);

    return () => clearTimeout(timer);
  }, [liveTranslate, bodyText, targetLang, mode]);

  const phrasesQuery = useQuery({
    queryKey: ["ai", "quick-replies"],
    queryFn: async () => (await fetchQuickReplies()) ?? [],
    enabled: Boolean(conversation),
  });

  const transcript = useMemo(
    () =>
      messages
        .slice(-8)
        .map((message) => `${message.sender_type}: ${message.body_text}`)
        .join("\n")
        .slice(0, 4000),
    [messages]
  );

  const aiAssistMutation = useMutation({
    mutationFn: async (action: "draft" | "polish" | "summarize") => {
      if (!conversation) return null;
      return aiAssist(conversation.id, {
        action,
        draft_text: bodyText || undefined,
        strict_rag: strictRag,
      });
    },
    onSuccess: (res) => {
      if (res?.text) {
        setBodyText(res.text);
      }
    },
  });

  // Inbox AI toolbar: every action only writes into the draft, never sends.
  const assistMutation = useMutation({
    mutationFn: async ({ intent, text }: { intent: InboxToolbarIntent; text: string }) =>
      fetchInboxAssist(intent, {
        text,
        conversationId: conversation?.id ?? null,
        language: "zh",
      }),
    onSuccess: (result) => {
      setAssistError(null);
      if (result.text) setBodyText(result.text);
      const review = result.requires_human_review
        ? " · 数字未通过工具校验，请人工核对后再发送"
        : "";
      setAssistNotice(
        result.model_used ? `AI 已生成草稿 · ${result.model_used}${review}` : null
      );
    },
    onError: (error) => {
      setAssistNotice(null);
      setAssistError(
        error instanceof BffApiError && error.code === "ERR_AI_PROVIDER_UNAVAILABLE"
          ? t("aiProviderUnavailable")
          : error.message || t("aiAssistFailed")
      );
    },
  });

  const sendMutation = useMutation({
    mutationFn: async () => {
      if (!conversation) return null;
      const finalBody =
        mode === "outbound" && liveTranslate && translatedPreview
          ? translatedPreview
          : bodyText;
      return sendConversationMessage(conversation.id, {
        direction: mode,
        content_type: "text",
        body_text: finalBody,
        target_language: liveTranslate ? targetLang : undefined,
        translate_before_send: mode === "outbound" && liveTranslate,
      });
    },
    onSuccess: () => {
      setBodyText("");
      setTranslatedPreview("");
      setComplianceError(null);
      setAssistNotice(null);
      if (conversation) {
        void queryClient.invalidateQueries({
          queryKey: ["inbox", "messages", conversation.id],
        });
        void queryClient.invalidateQueries({ queryKey: ["inbox", "conversations"] });
      }
    },
    onError: (err) => {
      if (err instanceof BffApiError) {
        setComplianceError(`${err.code}: ${err.detail}`);
      } else {
        setComplianceError(bi("消息发送失败，请重试。", "Failed to deliver message."));
      }
    },
  });

  // Pack B：图片 / PDF 附件上传（multipart，成功后刷新消息流）。
  const attachmentMutation = useMutation({
    mutationFn: async () => {
      if (!conversation || !selectedFile) return null;
      return uploadInboxAttachment(conversation.id, selectedFile, bodyText);
    },
    onSuccess: () => {
      setSelectedFile(null);
      setAttachmentError(null);
      setAttachmentNotice(
        bi("附件已上传并进入发送队列", "Attachment uploaded to the outbound queue")
      );
      setBodyText("");
      setTranslatedPreview("");
      if (fileInputRef.current) fileInputRef.current.value = "";
      if (conversation) {
        void queryClient.invalidateQueries({
          queryKey: ["inbox", "messages", conversation.id],
        });
        void queryClient.invalidateQueries({ queryKey: ["inbox", "conversations"] });
      }
    },
    onError: (error) => {
      setAttachmentNotice(null);
      setAttachmentError(
        error instanceof BffApiError
          ? `${error.code}: ${error.detail}`
          : bi("附件上传失败，请重试。", "Attachment upload failed. Please retry.")
      );
    },
  });

  // 「AI 查库生成提案」：让 AI 先跑确定性 SQL 工具，再生成带 ERP 证据的待审提案。
  const proposeMutation = useMutation({
    mutationFn: async () => {
      if (!conversation) return null;
      return proposeGroundedReply(conversation.id);
    },
    onSuccess: (proposal) => {
      if (!proposal) return;
      setProposalNotice(true);
      setAssistError(null);
      void queryClient.invalidateQueries({ queryKey: ["ai", "proposals"] });
      void queryClient.invalidateQueries({ queryKey: ["ai", "proposals", "pending-count"] });
    },
    onError: (error) => {
      setProposalNotice(false);
      setAssistError(
        error instanceof BffApiError
          ? error.detail
          : bi("提案生成失败，请稍后重试。", "Failed to generate a grounded proposal."),
      );
    },
  });

  if (!conversation) return null;

  // 12 个买家站内私信 IM 渠道：出站回复直连平台买家消息 API，不走邮件 / 短信。
  const marketplaceIm = isMarketplaceImChannel(conversation.channel_type);
  const showMarketplaceImHint = marketplaceIm && mode === "outbound";

  const phrases: readonly QuickReplyTemplate[] = phrasesQuery.data ?? [];

  function runAssist(intent: InboxToolbarIntent) {
    const draft = bodyText.trim();
    const source =
      intent === "polish"
        ? draft
        : intent === "summarize"
          ? transcript || draft
          : draft || transcript;
    if (!source) {
      setAssistNotice(null);
      setAssistError("请先输入草稿内容，或在会话中选择一条买家消息。");
      return;
    }
    assistMutation.mutate({ intent, text: source });
  }

  function insertPhrase(phrase: QuickReplyTemplate) {
    // Quick replies are inserted into the editable draft; sending stays manual.
    setBodyText((previous) => (previous.trim() ? `${previous}\n${phrase.body_text}` : phrase.body_text));
    setAssistNotice(`已插入快捷短语：${phrase.title}`);
  }

  return (
    <div className="border-t border-gray-200 bg-gray-50 px-3 py-3 dark:border-gray-800 dark:bg-gray-950">
      <div
        data-testid="inbox-composer"
        className="rounded-2xl border border-gray-200 bg-white p-3 shadow-[0_2px_10px_-4px_rgba(15,23,42,0.16)] dark:border-gray-800 dark:bg-gray-900"
      >
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-1.5">
            <button
              type="button"
              onClick={() => aiAssistMutation.mutate("draft")}
              className="inline-flex items-center gap-1 rounded-lg border border-gray-200 bg-white px-2 py-1 text-[12px] font-semibold text-gray-700 transition-colors hover:border-blue-200 hover:bg-blue-50 hover:text-blue-800 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300 dark:hover:border-blue-500/40 dark:hover:bg-blue-500/10 dark:hover:text-blue-300"
            >
              <Sparkles className="h-3 w-3" />
              <span>{t("aiDraft")}</span>
            </button>
            <InboxAiToolbar
              phrases={phrases}
              onAction={runAssist}
              onInsertPhrase={insertPhrase}
              disabled={assistMutation.isPending}
              pendingAction={assistMutation.isPending ? assistMutation.variables.intent : null}
            />
            <button
              type="button"
              data-testid="propose-grounded-reply-btn"
              disabled={proposeMutation.isPending}
              onClick={() => proposeMutation.mutate()}
              className="inline-flex items-center gap-1 rounded-lg border border-blue-200 bg-blue-50 px-2 py-1 text-[12px] font-semibold text-blue-700 transition-colors hover:bg-blue-100 disabled:opacity-50 dark:border-blue-500/30 dark:bg-blue-500/10 dark:text-blue-300 dark:hover:bg-blue-500/15"
            >
              <DatabaseZap className="h-3 w-3" />
              <span>{bi("AI 查库生成提案", "AI grounded proposal")}</span>
            </button>
          </div>

          <label className="inline-flex cursor-pointer items-center gap-1.5 rounded-lg bg-gray-100 px-2 py-1 text-[11.5px] font-semibold text-gray-900 ring-1 ring-gray-200/70 dark:bg-gray-800 dark:text-gray-100 dark:ring-gray-700">
            <input
              type="checkbox"
              data-testid="composer-strict-rag-toggle"
              checked={strictRag}
              onChange={(e) => setStrictRag(e.target.checked)}
              className="rounded border-gray-300 dark:border-gray-600"
            />
            <ShieldCheck className="h-3.5 w-3.5 text-blue-600" />
            <span>{t("strictRag")}</span>
          </label>
        </div>

        {proposalNotice && (
          <div
            data-testid="propose-grounded-reply-success"
            className="mb-2 flex flex-wrap items-center gap-2 rounded-xl border border-blue-200 bg-blue-50 px-2.5 py-1.5 text-[12px] font-semibold text-blue-900 dark:border-blue-500/30 dark:bg-blue-500/10 dark:text-blue-100"
          >
            <ShieldCheck className="h-3.5 w-3.5 shrink-0 text-blue-600" />
            <Link
              data-testid="propose-grounded-reply-link"
              href="/ai/proposals"
              className="inline-flex items-center gap-1 underline decoration-blue-400 underline-offset-2 hover:text-blue-700 dark:hover:text-blue-300"
            >
              <span>
                {bi(
                  "已生成带 ERP 证据的提案，去审核并发送",
                  "Grounded proposal ready — review and send"
                )}
              </span>
              <ArrowRight className="h-3.5 w-3.5" />
            </Link>
          </div>
        )}

        {assistNotice && (
          <p
            data-testid="composer-assist-notice"
            className="mb-2 rounded-xl border border-blue-100 bg-blue-50 px-2.5 py-1.5 text-[12px] font-medium text-blue-900"
          >
            {assistNotice}
          </p>
        )}

        {assistError && (
          <p
            role="alert"
            data-testid="composer-assist-error"
            className="mb-2 flex items-start gap-1.5 rounded-xl border border-amber-200 bg-amber-50 px-2.5 py-1.5 text-[12px] text-amber-900"
          >
            <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>{assistError}</span>
          </p>
        )}

        {complianceError && (
          <div
            role="alert"
            className="mb-2 rounded-xl border border-rose-200 bg-rose-50 px-2.5 py-1.5 text-[12px] font-medium text-rose-800"
          >
            {complianceError}
          </div>
        )}

        {attachmentNotice && (
          <p
            data-testid="composer-attachment-notice"
            className="mb-2 flex items-center gap-1.5 rounded-xl border border-blue-200 bg-blue-50 px-2.5 py-1.5 text-[12px] font-medium text-blue-900 dark:border-blue-500/30 dark:bg-blue-500/10 dark:text-blue-100"
          >
            <Paperclip className="h-3.5 w-3.5 shrink-0 text-blue-600" />
            <span>{attachmentNotice}</span>
          </p>
        )}

        {attachmentError && (
          <p
            role="alert"
            data-testid="composer-attachment-error"
            className="mb-2 rounded-xl border border-rose-200 bg-rose-50 px-2.5 py-1.5 text-[12px] font-medium text-rose-800"
          >
            {attachmentError}
          </p>
        )}

        {selectedFile && (
          <div
            data-testid="attach-preview-pill"
            className="mb-2 flex w-fit max-w-full items-center gap-2 rounded-xl border border-blue-200 bg-blue-50/80 px-2.5 py-1 text-[11.5px] font-semibold text-blue-900 dark:border-blue-500/30 dark:bg-blue-500/10 dark:text-blue-100"
          >
            <FileText className="h-3.5 w-3.5 shrink-0 text-blue-700" />
            <span className="max-w-[220px] truncate">{selectedFile.name}</span>
            <span className="shrink-0 tabular-nums text-blue-700/80 dark:text-blue-300/80">
              {formatAttachmentSize(selectedFile.size)}
            </span>
            <button
              type="button"
              data-testid="attach-preview-remove"
              aria-label={bi("移除待发送附件", "Remove pending attachment")}
              onClick={() => {
                setSelectedFile(null);
                if (fileInputRef.current) fileInputRef.current.value = "";
              }}
              className="grid h-4 w-4 shrink-0 place-items-center rounded-full text-blue-700 transition-colors hover:bg-blue-200/80 hover:text-blue-900 dark:text-blue-300 dark:hover:bg-blue-500/15 dark:hover:text-blue-200"
            >
              <X className="h-3 w-3" />
            </button>
          </div>
        )}

        {showMarketplaceImHint && (
          <p
            data-testid="composer-marketplace-im-hint"
            data-marketplace-im-channel={conversation.channel_type}
            className="mb-2 flex items-start gap-1.5 rounded-xl border border-violet-200 bg-violet-50/80 px-2.5 py-1.5 text-[11.5px] font-medium text-violet-900 dark:border-violet-500/30 dark:bg-violet-500/10 dark:text-violet-200"
          >
            <MessageSquare className="mt-0.5 h-3.5 w-3.5 shrink-0 text-violet-600 dark:text-violet-300" />
            <span>
              {bi(
                "平台买家站内私信直连 · AI 起草与实时翻译可用",
                "Direct Marketplace Buyer IM · AI drafts & translation enabled"
              )}
            </span>
          </p>
        )}

        <textarea
          data-testid="composer-textarea"
          rows={3}
          value={bodyText}
          onChange={(e) => setBodyText(e.target.value)}
          placeholder={
            mode === "internal_note"
              ? bi("写一条内部备注（不会发送给买家）…", "Write an internal note (never sent to buyer)…")
              : bi("输入回复内容；开启实时翻译可预览外文效果…", "Type a reply; enable Live Translate to preview the foreign-language text…")
          }
          aria-label={bi("消息输入框", "Message composer")}
          className={`w-full resize-y rounded-xl border p-3 text-[13px] leading-6 text-gray-900 placeholder:text-gray-400 focus:outline-none focus:ring-[3px] dark:text-gray-100 dark:placeholder:text-gray-500 ${
            mode === "internal_note"
              ? "border-amber-300 bg-amber-50/50 focus:border-amber-500 focus:ring-amber-500/12 dark:border-amber-500/30 dark:bg-amber-500/10"
              : "border-gray-200 bg-gray-50 focus:border-blue-500 focus:bg-white focus:ring-blue-500/12 dark:border-gray-700 dark:bg-gray-800 dark:focus:bg-gray-900"
          }`}
        />

        {liveTranslate && mode === "outbound" && translatedPreview && (
          <div
            data-testid="live-translation-preview"
            className="mt-2 rounded-xl border border-blue-100 bg-blue-50 px-2.5 py-1.5 text-[12px] text-blue-900"
          >
            <span className="font-semibold">Preview ({targetLang.toUpperCase()}): </span>
            {translatedPreview}
          </div>
        )}

        <div className="mt-2.5 flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-3">
            <div className="inline-flex rounded-xl border border-gray-200 bg-gray-100 p-0.5 text-[12px] dark:border-gray-700 dark:bg-gray-800">
              <button
                type="button"
                data-testid="composer-mode-outbound"
                aria-pressed={mode === "outbound"}
                onClick={() => setMode("outbound")}
                className={`rounded-lg px-2.5 py-1 font-semibold transition-colors ${
                  mode === "outbound"
                    ? "bg-white text-blue-700 shadow-xs ring-1 ring-gray-200/80 dark:bg-gray-900 dark:ring-gray-700"
                    : "text-gray-600 hover:text-gray-900 dark:text-gray-300 dark:hover:text-white"
                }`}
              >
                {bi("回复", "Reply")}
              </button>
              <button
                type="button"
                data-testid="composer-mode-internal-note"
                aria-pressed={mode === "internal_note"}
                onClick={() => setMode("internal_note")}
                className={`inline-flex items-center gap-1 rounded-lg px-2.5 py-1 font-semibold transition-colors ${
                  mode === "internal_note"
                    ? "bg-amber-500 text-white shadow-xs"
                    : "text-gray-600 hover:text-gray-900 dark:text-gray-300 dark:hover:text-white"
                }`}
              >
                <StickyNote className="h-3 w-3" />
                <span>{t("internalNote")}</span>
              </button>
            </div>

            <label className="inline-flex cursor-pointer items-center gap-1.5 text-[11.5px] font-medium text-gray-700 dark:text-gray-300">
              <input
                type="checkbox"
                data-testid="live-translate-toggle"
                checked={liveTranslate}
                onChange={(e) => setLiveTranslate(e.target.checked)}
              />
              <Languages className="h-3.5 w-3.5 text-blue-600" />
              <span>{t("liveTranslate")}</span>
            </label>

            <select
              aria-label={bi("目标翻译语言", "Target translation language")}
              data-testid="composer-target-language"
              value={targetLang}
              onChange={(e) => setTargetLang(e.target.value as TargetLanguageCode)}
              className="rounded-lg border border-gray-200 bg-white px-1.5 py-1 text-[11.5px] font-semibold text-gray-700 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200"
            >
              {TARGET_LANGUAGES.map((language) => (
                <option key={language.code} value={language.code}>
                  {bi(language.zh, language.en)}
                </option>
              ))}
            </select>

            <input
              ref={fileInputRef}
              type="file"
              hidden
              data-testid="attach-file-input"
              accept={ATTACHMENT_ACCEPT}
              onChange={(e) => {
                const file = e.target.files?.[0] ?? null;
                setSelectedFile(file);
                setAttachmentError(null);
                setAttachmentNotice(null);
              }}
            />
            <button
              type="button"
              data-testid="attach-file-btn"
              aria-label={bi("发图片 / PDF", "Attach image or PDF")}
              onClick={() => fileInputRef.current?.click()}
              className="inline-flex items-center gap-1 rounded-lg border border-gray-200 bg-gray-50 px-2 py-1 text-[11.5px] font-semibold text-gray-700 transition-colors hover:border-blue-200 hover:bg-blue-50 hover:text-blue-700 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300 dark:hover:border-blue-500/40 dark:hover:bg-blue-500/10 dark:hover:text-blue-300"
            >
              <Paperclip className="h-3.5 w-3.5" />
              <span>{bi("发图片 / PDF", "Attach image / PDF")}</span>
            </button>
          </div>

          <button
            type="button"
            data-testid="composer-submit-btn"
            disabled={
              (!bodyText.trim() && !selectedFile) ||
              sendMutation.isPending ||
              attachmentMutation.isPending
            }
            onClick={() => {
              // 选中文件时发送即上传（可带 textarea 里的说明作为 caption）。
              if (selectedFile) {
                attachmentMutation.mutate();
                return;
              }
              sendMutation.mutate();
            }}
            className={`inline-flex items-center gap-1.5 rounded-xl px-4 py-1.5 text-[12.5px] font-semibold text-white shadow-[0_2px_8px_-2px_rgba(37,99,235,0.55)] transition-colors disabled:shadow-none disabled:opacity-50 ${
              mode === "internal_note"
                ? "bg-amber-600 hover:bg-amber-700"
                : "bg-blue-600 hover:bg-blue-700"
            }`}
          >
            <Send className="h-3.5 w-3.5" />
            <span>{mode === "internal_note" ? t("internalNote") : t("send")}</span>
          </button>
        </div>
      </div>
    </div>
  );
}
