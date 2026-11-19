"use client";

import React, { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Sparkles, Send, StickyNote, Languages, ShieldCheck, Info } from "lucide-react";
import { sendConversationMessage } from "@/lib/api-client/live-client";
import { aiAssist } from "@/lib/api-client/plan04-bridge";
import { translateText } from "@/lib/api-client/live-client";
import { BffApiError } from "@/lib/contracts";
import { InboxAiToolbar } from "@/components/ai-workforce/inbox-ai-toolbar";
import type { InboxToolbarIntent, QuickReplyTemplate } from "@/components/ai-workforce/types";
import type { ConversationSummary } from "@/components/inbox/conversation-list";
import type { InboxMessage } from "@/components/inbox/message-thread";
import { fetchInboxAssist, fetchQuickReplies } from "@/lib/phase3b-data";
import { useI18n } from "@/lib/i18n";

export function InboxComposer({
  conversation,
  messages = [],
}: {
  conversation: ConversationSummary | null;
  messages?: readonly InboxMessage[];
}) {
  const { t } = useI18n();
  const queryClient = useQueryClient();

  const [bodyText, setBodyText] = useState("");
  const [mode, setMode] = useState<"outbound" | "internal_note">("outbound");
  const [liveTranslate, setLiveTranslate] = useState(false);
  const [targetLang, setTargetLang] = useState<"en" | "es" | "de" | "fr" | "ja">("en");
  const [translatedPreview, setTranslatedPreview] = useState<string>("");
  const [strictRag, setStrictRag] = useState(false);
  const [complianceError, setComplianceError] = useState<string | null>(null);
  const [assistNotice, setAssistNotice] = useState<string | null>(null);
  const [assistError, setAssistError] = useState<string | null>(null);

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
    queryFn: fetchQuickReplies,
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
        setComplianceError("Failed to deliver message.");
      }
    },
  });

  if (!conversation) return null;

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
    <div data-testid="inbox-composer" className="border-t border-slate-200 bg-white p-3">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-1.5">
          <button
            type="button"
            onClick={() => aiAssistMutation.mutate("draft")}
            className="inline-flex items-center gap-1 rounded border border-indigo-200 bg-indigo-50 px-2 py-1 text-xs font-medium text-indigo-700 hover:bg-indigo-100"
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
        </div>

        <label className="inline-flex cursor-pointer items-center gap-1.5 text-xs font-medium text-slate-700">
          <input
            type="checkbox"
            data-testid="composer-strict-rag-toggle"
            checked={strictRag}
            onChange={(e) => setStrictRag(e.target.checked)}
            className="rounded border-slate-300"
          />
          <ShieldCheck className="h-3.5 w-3.5 text-emerald-600" />
          <span>{t("strictRag")}</span>
        </label>
      </div>

      {assistNotice && (
        <p
          data-testid="composer-assist-notice"
          className="mb-2 rounded border border-indigo-100 bg-indigo-50 px-2.5 py-1.5 text-xs text-indigo-900"
        >
          {assistNotice}
        </p>
      )}

      {assistError && (
        <p
          role="alert"
          data-testid="composer-assist-error"
          className="mb-2 flex items-start gap-1.5 rounded border border-amber-300 bg-amber-50 px-2.5 py-1.5 text-xs text-amber-900"
        >
          <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>{assistError}</span>
        </p>
      )}

      {complianceError && (
        <div
          role="alert"
          className="mb-2 rounded border border-rose-300 bg-rose-50 px-2.5 py-1.5 text-xs text-rose-800"
        >
          {complianceError}
        </div>
      )}

      <textarea
        data-testid="composer-textarea"
        rows={3}
        value={bodyText}
        onChange={(e) => setBodyText(e.target.value)}
        placeholder={
          mode === "internal_note"
            ? "Write an internal note (never sent to buyer)..."
            : "Type reply (enable Live Translate for Chinese -> Foreign language preview)..."
        }
        className={`w-full rounded-md border p-2.5 text-sm focus:outline-none ${
          mode === "internal_note"
            ? "border-amber-300 bg-amber-50/50"
            : "border-slate-300 focus:border-indigo-500"
        }`}
      />

      {liveTranslate && mode === "outbound" && translatedPreview && (
        <div
          data-testid="live-translation-preview"
          className="mt-1.5 rounded border border-indigo-100 bg-indigo-50 px-2.5 py-1.5 text-xs text-indigo-900"
        >
          <span className="font-semibold">Preview ({targetLang.toUpperCase()}): </span>
          {translatedPreview}
        </div>
      )}

      <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-3">
          <div className="inline-flex rounded-md border border-slate-200 p-0.5 text-xs">
            <button
              type="button"
              data-testid="composer-mode-outbound"
              onClick={() => setMode("outbound")}
              className={`rounded px-2 py-1 font-medium ${
                mode === "outbound" ? "bg-indigo-600 text-white" : "text-slate-600"
              }`}
            >
              {t("send")}
            </button>
            <button
              type="button"
              data-testid="composer-mode-internal-note"
              onClick={() => setMode("internal_note")}
              className={`inline-flex items-center gap-1 rounded px-2 py-1 font-medium ${
                mode === "internal_note" ? "bg-amber-500 text-white" : "text-slate-600"
              }`}
            >
              <StickyNote className="h-3 w-3" />
              <span>{t("internalNote")}</span>
            </button>
          </div>

          <label className="inline-flex cursor-pointer items-center gap-1.5 text-xs text-slate-700">
            <input
              type="checkbox"
              data-testid="live-translate-toggle"
              checked={liveTranslate}
              onChange={(e) => setLiveTranslate(e.target.checked)}
            />
            <Languages className="h-3.5 w-3.5 text-indigo-600" />
            <span>{t("liveTranslate")}</span>
          </label>

          <select
            aria-label="Target translation language"
            value={targetLang}
            onChange={(e) => setTargetLang(e.target.value as typeof targetLang)}
            className="rounded border border-slate-300 px-1.5 py-0.5 text-xs"
          >
            <option value="en">EN</option>
            <option value="es">ES</option>
            <option value="de">DE</option>
            <option value="fr">FR</option>
            <option value="ja">JA</option>
          </select>
        </div>

        <button
          type="button"
          data-testid="composer-submit-btn"
          disabled={!bodyText.trim() || sendMutation.isPending}
          onClick={() => sendMutation.mutate()}
          className={`inline-flex items-center gap-1.5 rounded-md px-3.5 py-1.5 text-xs font-semibold text-white disabled:opacity-50 ${
            mode === "internal_note"
              ? "bg-amber-600 hover:bg-amber-700"
              : "bg-indigo-600 hover:bg-indigo-700"
          }`}
        >
          <Send className="h-3.5 w-3.5" />
          <span>{mode === "internal_note" ? t("internalNote") : t("send")}</span>
        </button>
      </div>
    </div>
  );
}
