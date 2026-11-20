"use client";

import React, { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  CheckCircle2,
  FileText,
  Loader2,
  Mail,
  Paperclip,
  Send,
  ShieldCheck,
  X,
} from "lucide-react";

import { fetchChannelAccounts, sendQuotePiEmail } from "@/lib/data-source";
import type {
  ChannelAccountApiItem,
  EmailLanguage,
  SendQuotePiEmailResponse,
} from "@/lib/api-client/live-client";
import { useI18n } from "@/lib/i18n";
import { BffApiError, formatMoney } from "@/lib/contracts";
import type { QuoteRecord } from "@/components/b2b/quote-pi-builder";

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[a-z]{2,}$/i;

export function defaultPiEmailSubject(quote: QuoteRecord): string {
  return `Proforma Invoice ${quote.quote_no} (${quote.incoterm} · ${quote.quote_currency} ${quote.total_amount})`;
}

/** 礼貌的出口随函：默认中英双版，用户可任意改写后再发。 */
export function defaultPiEmailBody(
  quote: QuoteRecord,
  language: EmailLanguage,
): string {
  const total = formatMoney(quote.total_amount, quote.quote_currency);
  if (language === "zh") {
    return [
      "尊敬的客户，您好：",
      "",
      `感谢您的询价。随函附上形式发票 ${quote.quote_no}（${quote.incoterm}，合计 ${total}）。`,
      `付款条件：${quote.payment_terms}。`,
      `报价汇率锁定至：${new Date(quote.fx_locked_until).toLocaleString("zh-CN")}。`,
      "",
      "如需调整数量、包装或贸易术语，请直接回复本邮件，我们会立即更新版本。",
      "",
      "顺祝商祺",
    ].join("\n");
  }
  return [
    "Dear Purchasing Team,",
    "",
    `Thank you for your inquiry. Please find attached Proforma Invoice ${quote.quote_no} (${quote.incoterm}, total ${total}).`,
    `Payment terms: ${quote.payment_terms}.`,
    `FX rate locked until: ${new Date(quote.fx_locked_until).toUTCString()}.`,
    "",
    "If you need a different quantity, packaging or incoterm, reply to this email and we will issue an updated version immediately.",
    "",
    "Best regards,",
  ].join("\n");
}

function inferEmailProvider(account: ChannelAccountApiItem): "Resend" | "SMTP" {
  const haystack = `${account.display_name} ${account.external_account_id}`.toLowerCase();
  return haystack.includes("resend") ? "Resend" : "SMTP";
}

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 KB";
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

const AUTO_ACCOUNT = "__auto__";

/**
 * 480px 右侧滑出抽屉：选发件邮箱账号 → 填收件人 → 选 PDF 语言 → 一键带附件发送。
 * 发送成功后直接展示后端回执（from_email / provider / pdf 文件名与体积）。
 */
export function SendPiEmailModal({
  quote,
  onClose,
}: {
  quote: QuoteRecord;
  onClose: () => void;
}) {
  const { bi } = useI18n();
  const queryClient = useQueryClient();

  const [language, setLanguage] = useState<EmailLanguage>("en");
  const [recipientEmail, setRecipientEmail] = useState("");
  const [channelAccountId, setChannelAccountId] = useState<string>(AUTO_ACCOUNT);
  const [subject, setSubject] = useState(() => defaultPiEmailSubject(quote));
  const [messageBody, setMessageBody] = useState(() => defaultPiEmailBody(quote, "en"));
  const [subjectTouched, setSubjectTouched] = useState(false);
  const [bodyTouched, setBodyTouched] = useState(false);
  const [receipt, setReceipt] = useState<SendQuotePiEmailResponse | null>(null);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  // 语言切换时，未被手动改过的主题/正文自动换成对应语言模板。
  useEffect(() => {
    if (!subjectTouched) setSubject(defaultPiEmailSubject(quote));
    if (!bodyTouched) setMessageBody(defaultPiEmailBody(quote, language));
  }, [bodyTouched, language, quote, subjectTouched]);

  const accountsQuery = useQuery({
    queryKey: ["channels", "accounts"],
    queryFn: () => fetchChannelAccounts(),
  });

  const emailAccounts = useMemo(
    () =>
      (accountsQuery.data?.items ?? []).filter(
        (account) => account.channel_type === "email" && account.status === "active",
      ),
    [accountsQuery.data?.items],
  );

  const recipientValid = EMAIL_PATTERN.test(recipientEmail.trim());

  const sendMutation = useMutation({
    mutationFn: () =>
      sendQuotePiEmail(quote.id, {
        recipient_email: recipientEmail.trim(),
        channel_account_id:
          channelAccountId === AUTO_ACCOUNT ? null : channelAccountId,
        subject: subject.trim() || null,
        message_body: messageBody.trim() || null,
        language,
      }),
    onSuccess: (result) => {
      setReceipt(result);
      void queryClient.invalidateQueries({ queryKey: ["b2b", "quotes"] });
    },
  });

  const errorMessage =
    sendMutation.error instanceof BffApiError
      ? sendMutation.error.detail
      : sendMutation.error?.message ?? null;

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <button
        type="button"
        aria-label={bi("关闭发送 PI 邮件", "Close send PI email")}
        data-testid="send-pi-email-backdrop"
        onClick={onClose}
        className="absolute inset-0 cursor-default bg-zinc-950/35 backdrop-blur-[1px]"
      />
      <aside
        role="dialog"
        aria-modal="true"
        aria-labelledby="send-pi-email-title"
        data-testid="send-pi-email-modal"
        className="relative flex h-full w-full max-w-[480px] flex-col border-l border-gray-200 bg-white shadow-[-24px_0_60px_-24px_rgba(15,23,42,0.35)] dark:border-gray-800 dark:bg-gray-900"
      >
        <header className="flex items-start justify-between gap-2 border-b border-gray-200 bg-white px-5 py-4 dark:border-gray-800 dark:bg-gray-900">
          <div className="flex min-w-0 items-start gap-3">
            <span
              aria-hidden="true"
              className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-blue-50 text-blue-600 ring-1 ring-blue-200 dark:bg-blue-500/10 dark:text-blue-400 dark:ring-blue-500/30"
            >
              <Mail className="h-[18px] w-[18px]" />
            </span>
            <div className="min-w-0">
              <h2
                id="send-pi-email-title"
                className="text-[15px] font-bold tracking-tight text-gray-900 dark:text-gray-100"
              >
                {bi("发送 PI 邮件", "Send PI email")}
              </h2>
              <p className="mt-0.5 text-[12px] leading-5 text-gray-500 dark:text-gray-400">
                <span className="font-mono tabular-nums">{quote.quote_no}</span> ·{" "}
                {formatMoney(quote.total_amount, quote.quote_currency)} ·{" "}
                {quote.incoterm}
              </p>
            </div>
          </div>
          <button
            type="button"
            data-testid="send-pi-email-close"
            onClick={onClose}
            className="grid h-8 w-8 shrink-0 place-items-center rounded-lg border border-gray-200 bg-white text-gray-500 transition-colors hover:bg-gray-50 hover:text-gray-900 dark:border-gray-800 dark:bg-gray-900 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-100"
          >
            <X className="h-4 w-4" />
          </button>
        </header>

        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-5 py-4">
          {receipt ? (
            <div
              data-testid="send-pi-email-success"
              className="space-y-3 rounded-2xl border border-emerald-200 bg-emerald-50/70 p-4 dark:border-emerald-500/30 dark:bg-emerald-500/10"
            >
              <div className="flex items-center gap-2 text-[13px] font-bold text-emerald-900 dark:text-emerald-200">
                <CheckCircle2 className="h-4 w-4 text-emerald-600 dark:text-emerald-400" />
                <span>{bi("PI 邮件已发出", "PI email delivered")}</span>
              </div>
              <dl className="space-y-1.5 text-[12px] leading-5 text-emerald-950 dark:text-emerald-100">
                <div className="flex justify-between gap-3">
                  <dt className="font-medium text-emerald-800 dark:text-emerald-300/90">
                    {bi("发件邮箱", "From")}
                  </dt>
                  <dd className="truncate font-semibold">{receipt.from_email}</dd>
                </div>
                <div className="flex justify-between gap-3">
                  <dt className="font-medium text-emerald-800 dark:text-emerald-300/90">
                    {bi("收件人", "Recipient")}
                  </dt>
                  <dd className="truncate font-semibold">{receipt.recipient_email}</dd>
                </div>
                <div className="flex justify-between gap-3">
                  <dt className="font-medium text-emerald-800 dark:text-emerald-300/90">
                    {bi("发送通道", "Provider")}
                  </dt>
                  <dd className="font-semibold">
                    {receipt.provider === "resend" ? "Resend" : "SMTP / IMAP"}
                  </dd>
                </div>
                <div className="flex justify-between gap-3">
                  <dt className="font-medium text-emerald-800 dark:text-emerald-300/90">
                    {bi("附件", "Attachment")}
                  </dt>
                  <dd className="min-w-0 truncate text-right font-semibold tabular-nums">
                    {receipt.pdf_filename} · {formatBytes(receipt.pdf_size_bytes)}
                  </dd>
                </div>
                <div className="flex justify-between gap-3">
                  <dt className="font-medium text-emerald-800 dark:text-emerald-300/90">
                    {bi("报价状态", "Quote status")}
                  </dt>
                  <dd className="font-mono text-[11.5px] font-semibold">
                    {receipt.quote_status}
                  </dd>
                </div>
              </dl>
              <div className="flex flex-wrap gap-2 pt-1">
                <Link
                  href="/b2b/quotes"
                  className="erp-btn erp-btn-primary rounded-xl text-[12px]"
                >
                  {bi("返回报价列表", "Back to quotes")}
                </Link>
                <button
                  type="button"
                  onClick={() => {
                    setReceipt(null);
                    setRecipientEmail("");
                  }}
                  className="erp-btn erp-btn-secondary rounded-xl text-[12px]"
                >
                  {bi("再发一封", "Send another")}
                </button>
              </div>
            </div>
          ) : (
            <>
              <div>
                <label className="erp-label" htmlFor="send-pi-recipient">
                  {bi("收件人邮箱", "Recipient email")}
                  <span className="ml-0.5 text-rose-600">*</span>
                </label>
                <input
                  id="send-pi-recipient"
                  data-testid="send-pi-email-recipient"
                  type="email"
                  inputMode="email"
                  value={recipientEmail}
                  onChange={(e) => setRecipientEmail(e.target.value)}
                  placeholder="purchasing@buyer.example.com"
                  className="erp-input mt-1.5"
                />
                {recipientEmail.length > 0 && !recipientValid && (
                  <p className="mt-1 text-[11.5px] font-medium text-rose-700">
                    {bi("请填写有效的收件人邮箱地址。", "Enter a valid recipient email address.")}
                  </p>
                )}
              </div>

              <div>
                <label className="erp-label" htmlFor="send-pi-account">
                  {bi("发件邮箱账号", "Sending account")}
                </label>
                <select
                  id="send-pi-account"
                  data-testid="send-pi-email-account"
                  value={channelAccountId}
                  onChange={(e) => setChannelAccountId(e.target.value)}
                  className="erp-input mt-1.5"
                >
                  <option value={AUTO_ACCOUNT}>
                    {bi("自动选择默认发件邮箱", "Auto-select the default sender")}
                  </option>
                  {emailAccounts.map((account) => (
                    <option key={account.id} value={account.id}>
                      {`${account.display_name || account.external_account_id} (${inferEmailProvider(account)})`}
                    </option>
                  ))}
                </select>
                {accountsQuery.isLoading && (
                  <p className="mt-1 text-[11.5px] text-gray-500 dark:text-gray-400">
                    {bi("正在读取已接入邮箱账号…", "Loading connected mailboxes…")}
                  </p>
                )}
                {!accountsQuery.isLoading && emailAccounts.length === 0 && (
                  <p className="mt-1 text-[11.5px] text-amber-800 dark:text-amber-400">
                    {bi(
                      "还没有接入邮箱账号，将使用租户默认发件邮箱。",
                      "No mailbox connected yet — the tenant default sender will be used."
                    )}
                  </p>
                )}
              </div>

              <div>
                <span className="erp-label">{bi("PDF 语言", "PDF language")}</span>
                <div className="mt-1.5 inline-flex rounded-xl border border-gray-200 bg-white p-0.5 dark:border-gray-800 dark:bg-gray-900">
                  {(["en", "zh"] as EmailLanguage[]).map((option) => {
                    const active = language === option;
                    return (
                      <button
                        key={option}
                        type="button"
                        data-testid={`send-pi-email-language-${option}`}
                        aria-pressed={active}
                        onClick={() => setLanguage(option)}
                        className={`rounded-lg px-3 py-1 text-[12px] font-semibold transition-colors ${
                          active
                            ? "bg-blue-600 text-white shadow-xs"
                            : "text-gray-600 hover:bg-gray-50 hover:text-gray-900 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-100"
                        }`}
                      >
                        {option.toUpperCase()}
                      </button>
                    );
                  })}
                </div>
              </div>

              <div>
                <label className="erp-label" htmlFor="send-pi-subject">
                  {bi("邮件主题", "Subject")}
                </label>
                <input
                  id="send-pi-subject"
                  data-testid="send-pi-email-subject"
                  type="text"
                  value={subject}
                  onChange={(e) => {
                    setSubjectTouched(true);
                    setSubject(e.target.value);
                  }}
                  className="erp-input mt-1.5"
                />
              </div>

              <div>
                <label className="erp-label" htmlFor="send-pi-body">
                  {bi("邮件正文", "Message body")}
                </label>
                <textarea
                  id="send-pi-body"
                  data-testid="send-pi-email-body"
                  rows={7}
                  value={messageBody}
                  onChange={(e) => {
                    setBodyTouched(true);
                    setMessageBody(e.target.value);
                  }}
                  className="erp-input mt-1.5 resize-y leading-6"
                />
              </div>

              <div className="flex items-start gap-2 rounded-xl border border-gray-200 bg-gray-50/70 px-3 py-2.5 text-[11.5px] leading-5 text-gray-600 dark:border-gray-800 dark:bg-gray-800/40 dark:text-gray-400">
                <Paperclip className="mt-0.5 h-3.5 w-3.5 shrink-0 text-blue-600 dark:text-blue-400" />
                <span>
                  {bi(
                    `附件：${quote.quote_no}-${language.toUpperCase()}.pdf（由后端实时渲染并随邮件发送）`,
                    `Attachment: ${quote.quote_no}-${language.toUpperCase()}.pdf (rendered server-side and attached)`
                  )}
                </span>
              </div>

              <div className="flex items-start gap-2 rounded-xl border border-emerald-200 bg-emerald-50/60 px-3 py-2.5 text-[11.5px] leading-5 text-emerald-900 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-200">
                <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-600 dark:text-emerald-400" />
                <span>
                  {bi(
                    "邮箱令牌以信封加密保存，服务端解密后发信；退订客户命中抑制名单时自动拦截。",
                    "Mailbox tokens stay envelope-encrypted and are decrypted server-side only; suppressed recipients are blocked automatically."
                  )}
                </span>
              </div>

              {errorMessage && (
                <p
                  role="alert"
                  data-testid="send-pi-email-error"
                  className="flex items-start gap-1.5 rounded-xl border border-rose-200 bg-rose-50 px-3 py-2.5 text-[12px] font-medium text-rose-800 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-300"
                >
                  <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                  <span>{errorMessage}</span>
                </p>
              )}
            </>
          )}
        </div>

        {!receipt && (
          <footer className="flex items-center justify-between gap-2 border-t border-gray-200 bg-white px-5 py-3 dark:border-gray-800 dark:bg-gray-900">
            <span className="inline-flex items-center gap-1 text-[11.5px] font-medium text-gray-500 dark:text-gray-400">
              <FileText className="h-3.5 w-3.5" />
              {bi("PI PDF 自动附在邮件中", "The PI PDF is attached automatically")}
            </span>
            <button
              type="button"
              data-testid="send-pi-email-submit"
              disabled={!recipientValid || sendMutation.isPending}
              onClick={() => sendMutation.mutate()}
              className="erp-btn rounded-xl bg-blue-600 px-4 py-2 text-[13px] font-semibold text-white shadow-xs transition-colors hover:bg-blue-700 disabled:opacity-50"
            >
              {sendMutation.isPending ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Send className="h-3.5 w-3.5" />
              )}
              <span>{bi("发送 PI 邮件", "Send PI email")}</span>
            </button>
          </footer>
        )}
      </aside>
    </div>
  );
}
