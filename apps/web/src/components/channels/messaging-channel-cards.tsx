"use client";

import React, { useState } from "react";
import { CheckCircle2, Instagram, MessageCircle, Send } from "lucide-react";

import { useI18n } from "@/lib/i18n";
import type {
  InstagramConnectPayload,
  MessengerConnectPayload,
  MessagingConnectPayload,
  MessagingConnectResult,
  MessagingProviderKind,
  TelegramConnectPayload,
} from "@/lib/api-client/live-client";

export type MessagingChannelDraft = {
  page_id: string;
  page_access_token: string;
  ig_account_id: string;
  access_token: string;
  bot_token: string;
  display_name: string;
};

const EMPTY_DRAFT: MessagingChannelDraft = {
  page_id: "",
  page_access_token: "",
  ig_account_id: "",
  access_token: "",
  bot_token: "",
  display_name: "",
};

const META_ID = /^[0-9]{1,32}$/;
const TELEGRAM_TOKEN = /^[0-9]{1,20}:[A-Za-z0-9_-]{10,}$/;

type DraftField = keyof MessagingChannelDraft;

type FieldCopy = {
  field: DraftField;
  testId: string;
  labelZh: string;
  labelEn: string;
  placeholder?: string;
  secret?: boolean;
};

type CardCopy = {
  titleZh: string;
  titleEn: string;
  subtitleZh: string;
  subtitleEn: string;
  windowZh: string;
  windowEn: string;
  icon: React.ComponentType<{ className?: string }>;
  fields: FieldCopy[];
};

const CARDS: Record<MessagingProviderKind, CardCopy> = {
  messenger: {
    titleZh: "Facebook Messenger",
    titleEn: "Facebook Messenger",
    subtitleZh: "Meta 公共主页收件箱，使用 fb_psid 身份与 X-Hub-Signature-256 验签。",
    subtitleEn: "Meta Page inbox with fb_psid identity and X-Hub-Signature-256 verification.",
    windowZh: "24 小时标准回复窗口；人工客服可用 HUMAN_AGENT 标签延长至 7 天。",
    windowEn:
      "24h standard reply window, extended to 7 days with the HUMAN_AGENT tag for human agents only.",
    icon: MessageCircle,
    fields: [
      {
        field: "page_id",
        testId: "messenger-page-id-input",
        labelZh: "主页 ID",
        labelEn: "Page ID",
        placeholder: "1010101010101010",
      },
      {
        field: "page_access_token",
        testId: "messenger-access-token-input",
        labelZh: "主页访问令牌",
        labelEn: "Page access token",
        secret: true,
      },
      {
        field: "display_name",
        testId: "messenger-display-name-input",
        labelZh: "账号显示名",
        labelEn: "Display name",
      },
    ],
  },
  instagram_dm: {
    titleZh: "Instagram 私信",
    titleEn: "Instagram DM",
    subtitleZh: "Instagram Messaging，支持快拍提及与快拍回复，身份使用 ig_psid。",
    subtitleEn: "Instagram Messaging with story mentions, story replies and ig_psid identity.",
    windowZh: "超过 24 小时仅可使用 HUMAN_AGENT；Instagram 不支持营销标签。",
    windowEn: "Only HUMAN_AGENT is available beyond 24h; Instagram supports no marketing tags.",
    icon: Instagram,
    fields: [
      {
        field: "ig_account_id",
        testId: "instagram-account-id-input",
        labelZh: "Instagram 账号 ID",
        labelEn: "Instagram account ID",
        placeholder: "17841400000000000",
      },
      {
        field: "access_token",
        testId: "instagram-access-token-input",
        labelZh: "访问令牌",
        labelEn: "Instagram access token",
        secret: true,
      },
      {
        field: "display_name",
        testId: "instagram-display-name-input",
        labelZh: "账号显示名",
        labelEn: "Display name",
      },
    ],
  },
  telegram: {
    titleZh: "Telegram 商业版",
    titleEn: "Telegram Business",
    subtitleZh: "Bot API 与 Business Mode，回复从卖家本人账号发出，保留真实身份。",
    subtitleEn: "Bot API and Business Mode replies sent from the seller's own account.",
    windowZh: "Webhook 使用 secret token 验签；Business Mode 保留卖家真实身份。",
    windowEn: "Secret-token verified webhook; Business Mode keeps the seller's real identity.",
    icon: Send,
    fields: [
      {
        field: "bot_token",
        testId: "telegram-bot-token-input",
        labelZh: "Bot 令牌",
        labelEn: "Bot token",
        placeholder: "123456789:AAH...",
        secret: true,
      },
      {
        field: "display_name",
        testId: "telegram-display-name-input",
        labelZh: "账号显示名",
        labelEn: "Display name",
      },
    ],
  },
};

function trimmed(value: string | undefined): string {
  return (value ?? "").trim();
}

export function isMessagingChannelDraftValid(
  kind: MessagingProviderKind,
  draft: Partial<MessagingChannelDraft>
): boolean {
  if (!trimmed(draft.display_name)) {
    return false;
  }
  if (kind === "messenger") {
    return META_ID.test(trimmed(draft.page_id)) && Boolean(trimmed(draft.page_access_token));
  }
  if (kind === "instagram_dm") {
    return META_ID.test(trimmed(draft.ig_account_id)) && Boolean(trimmed(draft.access_token));
  }
  return TELEGRAM_TOKEN.test(trimmed(draft.bot_token));
}

function toPayload(
  kind: MessagingProviderKind,
  draft: MessagingChannelDraft
): MessagingConnectPayload {
  const display_name = trimmed(draft.display_name);
  if (kind === "messenger") {
    const payload: MessengerConnectPayload = {
      page_id: trimmed(draft.page_id),
      page_access_token: trimmed(draft.page_access_token),
      display_name,
    };
    return payload;
  }
  if (kind === "instagram_dm") {
    const payload: InstagramConnectPayload = {
      ig_account_id: trimmed(draft.ig_account_id),
      access_token: trimmed(draft.access_token),
      display_name,
    };
    return payload;
  }
  const payload: TelegramConnectPayload = {
    bot_token: trimmed(draft.bot_token),
    display_name,
  };
  return payload;
}

export type MessagingChannelCardProps = {
  kind: MessagingProviderKind;
  connected: boolean;
  onSubmit: (payload: MessagingConnectPayload) => Promise<MessagingConnectResult | void>;
};

export function MessagingChannelCard({ kind, connected, onSubmit }: MessagingChannelCardProps) {
  const { bi } = useI18n();
  const copy = CARDS[kind];
  const Icon = copy.icon;
  const [draft, setDraft] = useState<MessagingChannelDraft>(EMPTY_DRAFT);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [connectedId, setConnectedId] = useState<string | null>(null);

  const valid = isMessagingChannelDraftValid(kind, draft);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!valid || submitting) {
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const result = await onSubmit(toPayload(kind, draft));
      setConnectedId(result?.external_account_id ?? "");
      setDraft({ ...EMPTY_DRAFT, display_name: draft.display_name });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "ERR_CHANNEL_CONNECTION_FAILED");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form
      data-testid={`messaging-card-${kind}`}
      onSubmit={submit}
      className="space-y-2.5 rounded-xl border border-slate-200/80 bg-white p-3.5"
    >
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-start gap-2">
          <Icon className="mt-0.5 h-4 w-4 text-blue-600" />
          <div>
            <div className="text-[13px] font-semibold text-slate-900">
              {bi(copy.titleZh, copy.titleEn)}
            </div>
            <div className="mt-0.5 text-[12px] leading-5 text-slate-500">
              {bi(copy.subtitleZh, copy.subtitleEn)}
            </div>
            <div className="mt-1 text-[11px] leading-5 text-slate-400">
              {bi(copy.windowZh, copy.windowEn)}
            </div>
          </div>
        </div>
        {connected && (
          <span className="shrink-0 rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[10px] font-medium text-emerald-700">
            {bi("已连接", "connected")}
          </span>
        )}
      </div>

      {copy.fields.map((field) => (
        <label key={field.testId} className="block">
          <span className="erp-label">{bi(field.labelZh, field.labelEn)}</span>
          <input
            data-testid={field.testId}
            type={field.secret ? "password" : "text"}
            autoComplete={field.secret ? "off" : undefined}
            value={draft[field.field]}
            placeholder={field.placeholder}
            onChange={(event) =>
              setDraft((current) => ({ ...current, [field.field]: event.target.value }))
            }
            className="erp-input mt-1"
          />
        </label>
      ))}

      <button
        type="submit"
        data-testid={`connect-${kind}-btn`}
        disabled={!valid || submitting}
        className="erp-btn bg-slate-900 text-white disabled:bg-slate-300"
      >
        <CheckCircle2 className="h-3 w-3" />
        <span>
          {submitting ? bi("连接中…", "Connecting…") : bi("连接渠道", "Connect")}
        </span>
      </button>

      {error && (
        <div role="alert" className="rounded-lg bg-rose-50 px-2.5 py-1.5 text-[11px] text-rose-700">
          {error}
        </div>
      )}
      {connectedId !== null && (
        <div
          data-testid={`connect-success-${kind}`}
          className="rounded-lg bg-emerald-50 px-2.5 py-1.5 text-[11px] text-emerald-700"
        >
          {bi("已连接", "Connected")} {connectedId}
        </div>
      )}
    </form>
  );
}
