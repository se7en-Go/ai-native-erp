"use client";

import React, { useState } from "react";
import { CheckCircle2, Instagram, MessageCircle, Send } from "lucide-react";

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
  label: string;
  placeholder?: string;
  secret?: boolean;
};

type CardCopy = {
  title: string;
  subtitle: string;
  window: string;
  icon: React.ComponentType<{ className?: string }>;
  fields: FieldCopy[];
};

const CARDS: Record<MessagingProviderKind, CardCopy> = {
  messenger: {
    title: "Facebook Messenger",
    subtitle: "Meta Page inbox with fb_psid identity and X-Hub-Signature-256 verification.",
    window:
      "24h standard reply window, extended to 7 days with the HUMAN_AGENT tag for human agents only.",
    icon: MessageCircle,
    fields: [
      { field: "page_id", testId: "messenger-page-id-input", label: "Page ID", placeholder: "1010101010101010" },
      {
        field: "page_access_token",
        testId: "messenger-access-token-input",
        label: "Page access token",
        secret: true,
      },
      { field: "display_name", testId: "messenger-display-name-input", label: "Display name" },
    ],
  },
  instagram_dm: {
    title: "Instagram DM",
    subtitle: "Instagram Messaging with story mentions, story replies and ig_psid identity.",
    window: "Only HUMAN_AGENT is available beyond 24h; Instagram supports no marketing tags.",
    icon: Instagram,
    fields: [
      {
        field: "ig_account_id",
        testId: "instagram-account-id-input",
        label: "Instagram account ID",
        placeholder: "17841400000000000",
      },
      {
        field: "access_token",
        testId: "instagram-access-token-input",
        label: "Instagram access token",
        secret: true,
      },
      {
        field: "display_name",
        testId: "instagram-display-name-input",
        label: "Display name",
      },
    ],
  },
  telegram: {
    title: "Telegram Business",
    subtitle: "Bot API and Business Mode replies sent from the seller's own account.",
    window: "Secret-token verified webhook; Business Mode keeps the seller's real identity.",
    icon: Send,
    fields: [
      {
        field: "bot_token",
        testId: "telegram-bot-token-input",
        label: "Bot token",
        placeholder: "123456789:AAH...",
        secret: true,
      },
      {
        field: "display_name",
        testId: "telegram-display-name-input",
        label: "Display name",
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
      className="space-y-2 rounded border border-slate-200 p-3"
    >
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-start gap-2">
          <Icon className="mt-0.5 h-4 w-4 text-indigo-600" />
          <div>
            <div className="font-bold text-slate-900">{copy.title}</div>
            <div className="text-slate-500">{copy.subtitle}</div>
            <div className="mt-1 text-[11px] text-slate-400">{copy.window}</div>
          </div>
        </div>
        {connected && (
          <span className="rounded bg-emerald-50 px-2 py-0.5 font-semibold text-emerald-700">
            connected
          </span>
        )}
      </div>

      {copy.fields.map((field) => (
        <label key={field.testId} className="block">
          <span className="text-slate-600">{field.label}</span>
          <input
            data-testid={field.testId}
            type={field.secret ? "password" : "text"}
            autoComplete={field.secret ? "off" : undefined}
            value={draft[field.field]}
            placeholder={field.placeholder}
            onChange={(event) =>
              setDraft((current) => ({ ...current, [field.field]: event.target.value }))
            }
            className="mt-1 w-full rounded border border-slate-200 px-2 py-1"
          />
        </label>
      ))}

      <button
        type="submit"
        data-testid={`connect-${kind}-btn`}
        disabled={!valid || submitting}
        className="inline-flex items-center gap-1 rounded bg-slate-900 px-2.5 py-1 font-semibold text-white disabled:bg-slate-300"
      >
        <CheckCircle2 className="h-3 w-3" />
        <span>{submitting ? "Connecting…" : "Connect"}</span>
      </button>

      {error && (
        <div role="alert" className="rounded bg-rose-50 px-2 py-1 text-rose-700">
          {error}
        </div>
      )}
      {connectedId !== null && (
        <div
          data-testid={`connect-success-${kind}`}
          className="rounded bg-emerald-50 px-2 py-1 text-emerald-700"
        >
          Connected {connectedId}
        </div>
      )}
    </form>
  );
}
