"use client";

import React, { useMemo, useState } from "react";
import { CheckCircle2, LockKeyhole, Mail, ShieldCheck, Sparkles } from "lucide-react";

import { useI18n } from "@/lib/i18n";
import type {
  ChannelVisibilityScope,
  EmailConnectPayload,
  MessagingConnectPayload,
  MessagingConnectResult,
  TeamMemberApiItem,
  TelegramConnectPayload,
} from "@/lib/api-client/live-client";
import {
  MessagingChannelCard,
  OwnershipSelector,
} from "@/components/channels/messaging-channel-cards";
export type EmailProvider = "resend" | "smtp_imap";

export type EmailConnectDraft = {
  email_address: string;
  display_name: string;
  api_key: string;
  smtp_host: string;
  smtp_port: string;
  smtp_username: string;
  smtp_password: string;
  imap_host: string;
  imap_port: string;
};

const EMPTY_EMAIL_DRAFT: EmailConnectDraft = {
  email_address: "",
  display_name: "",
  api_key: "",
  smtp_host: "",
  smtp_port: "465",
  smtp_username: "",
  smtp_password: "",
  imap_host: "",
  imap_port: "993",
};

const EMAIL_ADDRESS = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;

function trimmed(value: string | undefined): string {
  return (value ?? "").trim();
}

/** 邮箱草稿可提交判定：Resend 只要有 API Key，SMTP/IMAP 需要完整登录信息。 */
export function isEmailConnectDraftValid(
  provider: EmailProvider,
  draft: Partial<EmailConnectDraft>
): boolean {
  if (!EMAIL_ADDRESS.test(trimmed(draft.email_address))) return false;
  if (provider === "resend") {
    return trimmed(draft.api_key).length >= 8;
  }
  return (
    Boolean(trimmed(draft.smtp_host)) &&
    Boolean(trimmed(draft.smtp_username)) &&
    Boolean(trimmed(draft.smtp_password))
  );
}

function toEmailPayload(
  provider: EmailProvider,
  draft: EmailConnectDraft,
  ownership: { visibility_scope?: ChannelVisibilityScope; owner_user_id?: string | null },
): EmailConnectPayload {
  const display_name = trimmed(draft.display_name);
  const email_address = trimmed(draft.email_address);
  const portOf = (value: string): number | null => {
    const parsed = Number(value);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
  };
  const base: EmailConnectPayload = {
    email_address,
    display_name: display_name || null,
    provider,
    visibility_scope: ownership.visibility_scope,
    owner_user_id: ownership.owner_user_id ?? null,
  };
  if (provider === "resend") {
    return { ...base, api_key: trimmed(draft.api_key) };
  }
  return {
    ...base,
    smtp_host: trimmed(draft.smtp_host),
    smtp_port: portOf(draft.smtp_port),
    smtp_username: trimmed(draft.smtp_username),
    smtp_password: trimmed(draft.smtp_password),
    imap_host: trimmed(draft.imap_host) || null,
    imap_port: portOf(draft.imap_port),
  };
}

export type EmailQuickConnectCardProps = {
  connected: boolean;
  onSubmit: (payload: EmailConnectPayload) => Promise<MessagingConnectResult | void>;
  isAdmin?: boolean;
  members?: TeamMemberApiItem[];
  currentUserId?: string | null;
};

/**
 * 个人工作邮箱 / 公司发件邮箱的快捷绑定卡。
 * 业务员用公司 Resend Key 或自己的 SMTP 邮箱，30 秒内即可收信。
 */
export function EmailQuickConnectCard({
  connected,
  onSubmit,
  isAdmin,
  members = [],
  currentUserId,
}: EmailQuickConnectCardProps) {
  const { bi } = useI18n();
  const [provider, setProvider] = useState<EmailProvider>("resend");
  const [draft, setDraft] = useState<EmailConnectDraft>(EMPTY_EMAIL_DRAFT);
  const [adminScope, setAdminScope] = useState<ChannelVisibilityScope>("shared");
  const [ownerId, setOwnerId] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<MessagingConnectResult | null>(null);

  const ownership = useMemo(() => {
    if (isAdmin === true) {
      return adminScope === "personal"
        ? { visibility_scope: "personal" as const, owner_user_id: ownerId || null }
        : { visibility_scope: "shared" as const };
    }
    if (isAdmin === false) {
      return { visibility_scope: "personal" as const, owner_user_id: currentUserId ?? null };
    }
    return {};
  }, [adminScope, currentUserId, isAdmin, ownerId]);

  const ownershipReady = isAdmin !== true || adminScope === "shared" || Boolean(ownerId);
  const valid = isEmailConnectDraftValid(provider, draft) && ownershipReady;

  function update<K extends keyof EmailConnectDraft>(field: K, value: string) {
    setDraft((current) => ({ ...current, [field]: value }));
    setResult(null);
  }

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!valid || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const connectedResult = await onSubmit(toEmailPayload(provider, draft, ownership));
      setResult(connectedResult ?? { channel_account_id: "", external_account_id: "" });
      setDraft({ ...EMPTY_EMAIL_DRAFT, display_name: draft.display_name });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "ERR_CHANNEL_CONNECTION_FAILED");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form
      data-testid="email-quick-connect-card"
      onSubmit={submit}
      className="flex flex-col space-y-3 rounded-2xl border border-slate-200/90 bg-white p-4 shadow-[0_1px_2px_rgba(15,23,42,0.04)] transition-all duration-200 hover:-translate-y-0.5 hover:border-slate-300 hover:shadow-[0_6px_16px_-6px_rgba(15,23,42,0.2)]"
    >
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-start gap-2.5">
          <span
            aria-hidden="true"
            className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-emerald-50 text-emerald-700 ring-1 ring-emerald-100"
          >
            <Mail className="h-4 w-4" />
          </span>
          <div className="min-w-0">
            <div className="text-[13.5px] font-bold text-slate-900">
              {bi("个人工作邮箱 / 公司发件邮箱", "Work email / company sender")}
            </div>
            <div className="mt-0.5 text-[11.5px] leading-5 text-slate-600">
              {bi(
                "Resend API Key 或 SMTP/IMAP 邮箱，30 秒完成绑定，SPF/DKIM 自动校验。",
                "Bind a Resend API key or an SMTP/IMAP mailbox in 30 seconds; SPF/DKIM verified automatically."
              )}
            </div>
          </div>
        </div>
        <span
          className={`inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2 py-0.5 text-[10.5px] font-semibold ${
            connected
              ? "border-emerald-200 bg-emerald-50 text-emerald-800"
              : "border-slate-200 bg-slate-100 text-slate-600"
          }`}
        >
          <span
            aria-hidden="true"
            className={`h-1.5 w-1.5 rounded-full ${connected ? "bg-emerald-500" : "bg-slate-400"}`}
          />
          {connected ? bi("已连接", "connected") : bi("未连接", "disconnected")}
        </span>
      </div>

      <span aria-hidden="true" className="h-[3px] w-10 rounded-full bg-emerald-500" />

      <label className="block">
        <span className="erp-label">{bi("邮箱地址", "Email address")}</span>
        <input
          data-testid="email-address-input"
          type="email"
          value={draft.email_address}
          placeholder="alice@company.com"
          onChange={(event) => update("email_address", event.target.value)}
          className="erp-input mt-1 rounded-xl"
        />
      </label>

      <label className="block">
        <span className="erp-label">{bi("账号显示名（可选）", "Display name (optional)")}</span>
        <input
          data-testid="email-display-name-input"
          type="text"
          value={draft.display_name}
          placeholder={bi("留空自动使用邮箱地址", "Leave blank to reuse the address")}
          onChange={(event) => update("display_name", event.target.value)}
          className="erp-input mt-1 rounded-xl"
        />
      </label>

      <div className="inline-flex items-center gap-0.5 self-start rounded-lg border border-[#E5E2DC] bg-[#EFECE6]/90 p-0.5">
        <button
          type="button"
          data-testid="email-provider-resend"
          aria-pressed={provider === "resend"}
          onClick={() => setProvider("resend")}
          className={`rounded-md px-2 py-0.5 text-[11px] font-semibold transition-colors ${
            provider === "resend" ? "bg-[#18181B] text-[#FAFAF9]" : "text-stone-600"
          }`}
        >
          Resend API
        </button>
        <button
          type="button"
          data-testid="email-provider-smtp_imap"
          aria-pressed={provider === "smtp_imap"}
          onClick={() => setProvider("smtp_imap")}
          className={`rounded-md px-2 py-0.5 text-[11px] font-semibold transition-colors ${
            provider === "smtp_imap" ? "bg-[#18181B] text-[#FAFAF9]" : "text-stone-600"
          }`}
        >
          SMTP / IMAP
        </button>
      </div>

      {provider === "resend" ? (
        <label className="block">
          <span className="erp-label">{bi("Resend API Key", "Resend API key")}</span>
          <input
            data-testid="email-api-key-input"
            type="password"
            autoComplete="off"
            value={draft.api_key}
            placeholder="re_..."
            onChange={(event) => update("api_key", event.target.value)}
            className="erp-input mt-1 rounded-xl"
          />
        </label>
      ) : (
        <div className="space-y-2">
          <div className="grid grid-cols-[1fr_88px] gap-2">
            <label className="block">
              <span className="erp-label">SMTP Host</span>
              <input
                data-testid="email-smtp-host-input"
                type="text"
                value={draft.smtp_host}
                placeholder="smtp.qiye.aliyun.com"
                onChange={(event) => update("smtp_host", event.target.value)}
                className="erp-input mt-1 rounded-xl"
              />
            </label>
            <label className="block">
              <span className="erp-label">Port</span>
              <input
                data-testid="email-smtp-port-input"
                type="text"
                value={draft.smtp_port}
                onChange={(event) => update("smtp_port", event.target.value)}
                className="erp-input mt-1 rounded-xl"
              />
            </label>
          </div>
          <label className="block">
            <span className="erp-label">{bi("SMTP 用户名", "SMTP username")}</span>
            <input
              data-testid="email-smtp-username-input"
              type="text"
              value={draft.smtp_username}
              placeholder="alice@company.com"
              onChange={(event) => update("smtp_username", event.target.value)}
              className="erp-input mt-1 rounded-xl"
            />
          </label>
          <label className="block">
            <span className="erp-label">{bi("SMTP 密码 / 授权码", "SMTP password")}</span>
            <input
              data-testid="email-smtp-password-input"
              type="password"
              autoComplete="off"
              value={draft.smtp_password}
              onChange={(event) => update("smtp_password", event.target.value)}
              className="erp-input mt-1 rounded-xl"
            />
          </label>
        </div>
      )}

      {isAdmin === true && (
        <OwnershipSelector
          kind="email"
          scope={adminScope}
          ownerId={ownerId}
          members={members}
          currentUserId={currentUserId}
          onChangeScope={setAdminScope}
          onChangeOwner={setOwnerId}
        />
      )}

      {isAdmin === false && (
        <div
          data-testid="ownership-locked-email"
          className="inline-flex items-center gap-1.5 self-start rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[10.5px] font-semibold text-emerald-800"
        >
          <LockKeyhole className="h-3 w-3" />
          {bi("个人专属（仅自己与老板/主管可见）", "Personal (visible to you and your managers)")}
        </div>
      )}

      <button
        type="submit"
        data-testid="connect-email-btn"
        disabled={!valid || submitting}
        className="erp-btn mt-auto rounded-xl bg-[#059669] font-semibold text-white shadow-xs transition-colors hover:bg-[#047857] disabled:bg-slate-300 disabled:shadow-none"
      >
        <CheckCircle2 className="h-3 w-3" />
        <span>
          {submitting ? bi("连接中…", "Connecting…") : bi("一键连接邮箱", "Connect email")}
        </span>
      </button>

      {error && (
        <div
          role="alert"
          className="rounded-xl border border-rose-200 bg-rose-50 px-2.5 py-1.5 text-[11px] font-medium text-rose-700"
        >
          {error}
        </div>
      )}
      {result !== null && (
        <div
          data-testid="connect-success-email"
          className="flex flex-wrap items-center gap-1.5 rounded-xl border border-emerald-200 bg-emerald-50 px-2.5 py-2 text-[11px] font-medium text-emerald-800"
        >
          <CheckCircle2 className="h-3.5 w-3.5" />
          {bi("已连接", "Connected")} {result.external_account_id}
        </div>
      )}
    </form>
  );
}

export type QuickConnectPanelProps = {
  isAdmin: boolean;
  currentUserId?: string | null;
  members: TeamMemberApiItem[];
  telegramConnected: boolean;
  emailConnected: boolean;
  onConnectTelegram: (
    payload: TelegramConnectPayload,
  ) => Promise<MessagingConnectResult | void>;
  onConnectEmail: (payload: EmailConnectPayload) => Promise<MessagingConnectResult | void>;
};

/**
 * 「快捷绑定」总览：老板看到公海/指派两种口径，业务员只看到个人专属口径。
 */
export function QuickConnectPanel({
  isAdmin,
  currentUserId,
  members,
  telegramConnected,
  emailConnected,
  onConnectTelegram,
  onConnectEmail,
}: QuickConnectPanelProps) {
  const { bi } = useI18n();
  return (
    <section
      data-testid="quick-connect-panel"
      aria-label={bi("快捷绑定", "Quick connect")}
      className="space-y-3"
    >
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h2 className="flex items-center gap-2 text-[15px] font-semibold text-slate-950">
            <Sparkles className="h-4 w-4 text-emerald-700" />
            {isAdmin
              ? bi(
                  "快捷绑定：Telegram 机器人 + 工作邮箱",
                  "Quick connect: Telegram bot + work email"
                )
              : bi(
                  "我的个人快捷绑定：Telegram + 个人工作邮箱",
                  "My personal quick connect: Telegram + work email"
                )}
          </h2>
          <p className="mt-1 text-[12.5px] leading-5 text-slate-600">
            {isAdmin
              ? bi(
                  "粘贴即绑：Bot Token 整段粘贴自动识别，公海或指定业务员一句话搞定。",
                  "Paste-and-bind: drop the whole BotFather reply and choose shared or personal ownership."
                )
              : bi(
                  "绑定后只有你与老板/主管可见，客户回复直接进你的 Inbox。",
                  "Your bindings stay visible to you and your managers; replies land in your inbox."
                )}
          </p>
        </div>
        <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[10.5px] font-semibold text-emerald-800">
          <ShieldCheck className="h-3 w-3" />
          {bi("令牌信封加密 · Webhook 验签", "Envelope-encrypted tokens · verified webhooks")}
        </span>
      </div>

      <div className="grid gap-3 md:grid-cols-2 xl:gap-4">
        <MessagingChannelCard
          kind="telegram"
          connected={telegramConnected}
          onSubmit={(payload: MessagingConnectPayload) =>
            onConnectTelegram(payload as TelegramConnectPayload)
          }
          isAdmin={isAdmin}
          currentUserId={currentUserId}
          members={members}
        />
        <EmailQuickConnectCard
          connected={emailConnected}
          onSubmit={onConnectEmail}
          isAdmin={isAdmin}
          currentUserId={currentUserId}
          members={members}
        />
      </div>
    </section>
  );
}
