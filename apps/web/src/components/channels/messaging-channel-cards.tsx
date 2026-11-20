"use client";

import React, { useMemo, useState } from "react";
import {
  ArrowUpRight,
  CheckCircle2,
  ChevronDown,
  Instagram,
  KeyRound,
  LockKeyhole,
  MessageCircle,
  Send,
  Sparkles,
} from "lucide-react";

import { useI18n } from "@/lib/i18n";
import type {
  ChannelVisibilityScope,
  InstagramConnectPayload,
  MessengerConnectPayload,
  MessagingConnectPayload,
  MessagingConnectResult,
  MessagingProviderKind,
  TeamMemberApiItem,
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
/** 从 @BotFather 整段回复里抠出 `digits:token`，允许被说明文字包裹。 */
const TELEGRAM_TOKEN_INLINE = /([0-9]{1,20}:[A-Za-z0-9_-]{10,})/;
/**
 * 单行输入框会抹掉换行，粘帖后 token 可能直接和后面的英文说明黏在一起
 * （`...7890Keep your token secure`）；这里在遇到下一个英文单词前截断。
 */
const TELEGRAM_TOKEN_BEFORE_PROSE =
  /([0-9]{1,20}:[A-Za-z0-9_-]{10,}?)(?=[A-Z][a-z]{2,})/;

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
  /** 品牌着色图标框，让三张接入卡片一眼可辨且不再像线框。 */
  iconTile: string;
  accentBar: string;
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
    iconTile: "bg-blue-50 text-blue-600 ring-blue-100",
    accentBar: "bg-blue-500",
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
    iconTile: "bg-rose-50 text-rose-600 ring-rose-100",
    accentBar: "bg-rose-500",
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
    iconTile: "bg-sky-50 text-sky-600 ring-sky-100",
    accentBar: "bg-sky-500",
    fields: [
      {
        field: "bot_token",
        testId: "telegram-bot-token-input",
        labelZh: "Bot 令牌（整段粘贴也行）",
        labelEn: "Bot token (paste the whole reply)",
        placeholder: "123456789:AAH...",
        secret: true,
      },
      {
        field: "display_name",
        testId: "telegram-display-name-input",
        labelZh: "账号显示名（可选）",
        labelEn: "Display name (optional)",
        placeholder: "留空将自动使用 Telegram 机器人名称",
      },
    ],
  },
};

function trimmed(value: string | undefined): string {
  return (value ?? "").trim();
}

/**
 * 快捷绑定核心：「粘贴即绑」。
 *
 * 既接受裸 token（`123456789:AAH...`），也接受 @BotFather 的整段回复：
 *
 * ```
 * Use this token to access the HTTP API:
 * 123456789:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
 * Keep your token secure and store it safely.
 * ```
 *
 * 解析失败时原样（去空白）返回，交给正则校验给出明确报错。
 */
export function extractTelegramBotToken(input: string): string {
  const raw = (input ?? "").trim();
  if (!raw) return "";
  // 1) 多行粘贴：token 通常独占一行。
  for (const line of raw.split(/\r?\n/)) {
    const candidate = line.trim();
    if (TELEGRAM_TOKEN.test(candidate)) return candidate;
  }
  // 2) 单行输入框抹掉换行后的粘连（token 后续跟着英文说明）。
  const glued = raw.match(TELEGRAM_TOKEN_BEFORE_PROSE);
  if (glued) return glued[1];
  // 3) 同一行内被说明文字包裹。
  const inline = raw.match(TELEGRAM_TOKEN_INLINE);
  if (inline) return inline[1];
  // 4) 已经是裸 token 或无法解析（交给正则校验报错）。
  return raw;
}

/** Bot ID = token 冒号前的数字段；不是合法 token 时返回 null。 */
export function telegramBotIdOf(token: string): string | null {
  const normalized = trimmed(token);
  if (!TELEGRAM_TOKEN.test(normalized)) return null;
  return normalized.split(":")[0];
}

/**
 * 校验草稿是否可提交。
 *
 * Telegram 的 `display_name` 是**可选**的（后端会用 `getMe` 自动回填机器人名称），
 * 只贴 Bot Token 就应该能一键绑定；Messenger / Instagram 仍强制要求显示名。
 */
export function isMessagingChannelDraftValid(
  kind: MessagingProviderKind,
  draft: Partial<MessagingChannelDraft>
): boolean {
  if (kind === "telegram") {
    return TELEGRAM_TOKEN.test(trimmed(draft.bot_token));
  }
  if (!trimmed(draft.display_name)) {
    return false;
  }
  if (kind === "messenger") {
    return META_ID.test(trimmed(draft.page_id)) && Boolean(trimmed(draft.page_access_token));
  }
  return META_ID.test(trimmed(draft.ig_account_id)) && Boolean(trimmed(draft.access_token));
}

type OwnershipChoice = {
  visibility_scope?: ChannelVisibilityScope;
  owner_user_id?: string | null;
};

function toPayload(
  kind: MessagingProviderKind,
  draft: MessagingChannelDraft,
  ownership: OwnershipChoice = {}
): MessagingConnectPayload {
  const display_name = trimmed(draft.display_name);
  const ownershipFields: OwnershipChoice = {
    ...(ownership.visibility_scope ? { visibility_scope: ownership.visibility_scope } : {}),
    ...(ownership.visibility_scope === "personal"
      ? { owner_user_id: ownership.owner_user_id ?? null }
      : {}),
  };
  if (kind === "messenger") {
    const payload: MessengerConnectPayload = {
      page_id: trimmed(draft.page_id),
      page_access_token: trimmed(draft.page_access_token),
      display_name,
      ...ownershipFields,
    };
    return payload;
  }
  if (kind === "instagram_dm") {
    const payload: InstagramConnectPayload = {
      ig_account_id: trimmed(draft.ig_account_id),
      access_token: trimmed(draft.access_token),
      display_name,
      ...ownershipFields,
    };
    return payload;
  }
  // Telegram：显示名留空时整段省略，交给后端 getMe 自动回填。
  const payload: TelegramConnectPayload = {
    bot_token: trimmed(draft.bot_token),
    ...(display_name ? { display_name } : {}),
    ...ownershipFields,
  };
  return payload;
}

export type OwnershipSelectorProps = {
  kind: MessagingProviderKind | "email";
  scope: ChannelVisibilityScope;
  ownerId: string;
  members: TeamMemberApiItem[];
  currentUserId?: string | null;
  onChangeScope: (scope: ChannelVisibilityScope) => void;
  onChangeOwner: (ownerId: string) => void;
};

/** 归属选择器：公司公海 ↔ 指定业务员个人专属。 */
export function OwnershipSelector({
  kind,
  scope,
  ownerId,
  members,
  currentUserId,
  onChangeScope,
  onChangeOwner,
}: OwnershipSelectorProps) {
  const { bi } = useI18n();
  const activeMembers = members.filter((m) => m.is_active);

  return (
    <div
      data-testid={`ownership-selector-${kind}`}
      className="rounded-xl border border-slate-200/90 bg-[#FAF8F5] px-2.5 py-2"
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-[11px] font-semibold text-stone-500">
          {bi("绑定归属", "Ownership")}
        </span>
        <div className="inline-flex items-center gap-0.5 rounded-lg border border-[#E5E2DC] bg-white/80 p-0.5">
          <button
            type="button"
            data-testid={`ownership-shared-${kind}`}
            title={bi("公司公海：全团队共同接管", "Company shared pool visible to the whole team")}
            aria-pressed={scope === "shared"}
            onClick={() => onChangeScope("shared")}
            className={`rounded-md px-2 py-0.5 text-[11px] font-semibold transition-colors ${
              scope === "shared"
                ? "bg-[#18181B] text-[#FAFAF9]"
                : "text-stone-600 hover:text-zinc-950"
            }`}
          >
            {bi("公司公海", "Company shared")}
          </button>
          <button
            type="button"
            data-testid={`ownership-personal-${kind}`}
            title={bi(
              "指定业务员个人专属：仅归属人与老板/主管可见",
              "Assign to a salesperson: visible to the owner and managers"
            )}
            aria-pressed={scope === "personal"}
            onClick={() => onChangeScope("personal")}
            className={`rounded-md px-2 py-0.5 text-[11px] font-semibold transition-colors ${
              scope === "personal"
                ? "bg-emerald-600 text-white"
                : "text-stone-600 hover:text-zinc-950"
            }`}
          >
            {bi("指定业务员", "Assign to salesperson")}
          </button>
        </div>
      </div>

      {scope === "personal" && (
        <label className="mt-1.5 block">
          <span className="text-[11px] font-medium text-stone-600">
            {bi("归谁所有（个人专属）", "Personal owner")}
          </span>
          <select
            data-testid={`ownership-owner-select-${kind}`}
            value={ownerId}
            onChange={(event) => onChangeOwner(event.target.value)}
            className="erp-input mt-1 rounded-lg text-[12px]"
          >
            <option value="">{bi("请选择业务员…", "Select a salesperson…")}</option>
            {activeMembers.map((member) => (
              <option key={member.membership_id} value={member.user_id}>
                {member.full_name || member.email}
                {member.user_id === currentUserId ? bi("（我）", " (me)") : ""}
              </option>
            ))}
          </select>
        </label>
      )}
    </div>
  );
}

export type MessagingChannelCardProps = {
  kind: MessagingProviderKind;
  connected: boolean;
  onSubmit: (payload: MessagingConnectPayload) => Promise<MessagingConnectResult | void>;
  /**
   * true: owner/admin，可切换「公司公海 / 指定业务员」。
   * false: 业务员，锁定个人专属（仅自己与老板/主管可见）。
   * undefined: 无归属语义的历史渲染路径（不发归属字段）。
   */
  isAdmin?: boolean;
  members?: TeamMemberApiItem[];
  currentUserId?: string | null;
};

export function MessagingChannelCard({
  kind,
  connected,
  onSubmit,
  isAdmin,
  members = [],
  currentUserId,
}: MessagingChannelCardProps) {
  const { bi } = useI18n();
  const copy = CARDS[kind];
  const Icon = copy.icon;
  const [draft, setDraft] = useState<MessagingChannelDraft>(EMPTY_DRAFT);
  const [adminScope, setAdminScope] = useState<ChannelVisibilityScope>("shared");
  const [ownerId, setOwnerId] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<MessagingConnectResult | null>(null);
  const [guideOpen, setGuideOpen] = useState(false);

  const extractedToken = kind === "telegram" ? extractTelegramBotToken(draft.bot_token) : "";
  const detectedBotId = telegramBotIdOf(extractedToken);

  const ownership: OwnershipChoice = useMemo(() => {
    if (isAdmin === true) {
      return adminScope === "personal"
        ? { visibility_scope: "personal", owner_user_id: ownerId || null }
        : { visibility_scope: "shared" };
    }
    if (isAdmin === false) {
      return { visibility_scope: "personal", owner_user_id: currentUserId ?? null };
    }
    return {};
  }, [adminScope, currentUserId, isAdmin, ownerId]);

  const ownershipReady =
    isAdmin !== true || adminScope === "shared" || Boolean(ownerId);
  const valid = isMessagingChannelDraftValid(kind, draft) && ownershipReady;

  function updateField(field: DraftField, value: string) {
    setDraft((current) => ({
      ...current,
      [field]: field === "bot_token" ? extractTelegramBotToken(value) : value,
    }));
    setResult(null);
  }

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!valid || submitting) {
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const connectedResult = await onSubmit(toPayload(kind, draft, ownership));
      setResult(connectedResult ?? { channel_account_id: "", external_account_id: "" });
      setDraft({ ...EMPTY_DRAFT, display_name: draft.display_name });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "ERR_CHANNEL_CONNECTION_FAILED");
    } finally {
      setSubmitting(false);
    }
  }

  const botUsername = result?.bot_username ?? null;

  return (
    <form
      data-testid={`messaging-card-${kind}`}
      onSubmit={submit}
      className="flex flex-col space-y-3 rounded-2xl border border-slate-200/90 bg-white p-4 shadow-[0_1px_2px_rgba(15,23,42,0.04)] transition-all duration-200 hover:-translate-y-0.5 hover:border-slate-300 hover:shadow-[0_6px_16px_-6px_rgba(15,23,42,0.2)]"
    >
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-start gap-2.5">
          <span
            aria-hidden="true"
            className={`grid h-9 w-9 shrink-0 place-items-center rounded-xl ring-1 ${copy.iconTile}`}
          >
            <Icon className="h-4 w-4" />
          </span>
          <div className="min-w-0">
            <div className="text-[13.5px] font-bold text-slate-900">
              {bi(copy.titleZh, copy.titleEn)}
            </div>
            <div className="mt-0.5 text-[11.5px] leading-5 text-slate-600">
              {bi(copy.subtitleZh, copy.subtitleEn)}
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

      <p className="rounded-xl border border-slate-200/80 bg-slate-50/80 px-2.5 py-1.5 text-[11px] leading-5 font-medium text-slate-600">
        {bi(copy.windowZh, copy.windowEn)}
      </p>

      <span aria-hidden="true" className={`h-[3px] w-10 rounded-full ${copy.accentBar}`} />

      {copy.fields.map((field) => (
        <label key={field.testId} className="block">
          <span className="erp-label">{bi(field.labelZh, field.labelEn)}</span>
          <input
            data-testid={field.testId}
            type={field.secret ? "password" : "text"}
            autoComplete={field.secret ? "off" : undefined}
            value={draft[field.field]}
            placeholder={field.placeholder}
            onChange={(event) => updateField(field.field, event.target.value)}
            className="erp-input mt-1 rounded-xl"
          />
        </label>
      ))}

      {kind === "telegram" && detectedBotId && (
        <span
          data-testid="telegram-detected-bot-id"
          className="inline-flex items-center gap-1.5 self-start rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[10.5px] font-semibold text-emerald-800"
        >
          <Sparkles className="h-3 w-3" />
          {bi(`已自动识别 Bot ID: ${detectedBotId}`, `Bot ID detected: ${detectedBotId}`)}
        </span>
      )}

      {kind === "telegram" && (
        <div className="rounded-xl border border-slate-200/90 bg-[#FAF8F5]">
          <button
            type="button"
            data-testid="telegram-guide-toggle"
            aria-expanded={guideOpen}
            onClick={() => setGuideOpen((prev) => !prev)}
            className="flex w-full items-center justify-between gap-2 px-2.5 py-1.5 text-[11px] font-semibold text-stone-700"
          >
            <span className="inline-flex items-center gap-1.5">
              <KeyRound className="h-3 w-3 text-emerald-700" />
              {bi("3 步拿到 Bot Token（@BotFather）", "3 steps to a bot token (@BotFather)")}
            </span>
            <ChevronDown
              className={`h-3.5 w-3.5 transition-transform ${guideOpen ? "rotate-180" : ""}`}
            />
          </button>
          {guideOpen && (
            <ol
              data-testid="telegram-guide-steps"
              className="space-y-1 px-2.5 pb-2 text-[11px] leading-5 text-stone-600"
            >
              <li>
                <strong className="text-stone-900">1.</strong>{" "}
                {bi("打开 @BotFather 发送 /newbot", "Open @BotFather and send /newbot")}
              </li>
              <li>
                <strong className="text-stone-900">2.</strong>{" "}
                {bi(
                  "复制 HTTP API Token 整段粘贴进来",
                  "Copy the HTTP API token and paste the whole block"
                )}
              </li>
              <li>
                <strong className="text-stone-900">3.</strong>{" "}
                {bi(
                  "点一键连接，系统自动配置 Webhook 并加密保管",
                  "Click connect; the webhook is configured and the token encrypted"
                )}
              </li>
            </ol>
          )}
        </div>
      )}

      {isAdmin === true && (
        <OwnershipSelector
          kind={kind}
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
          data-testid={`ownership-locked-${kind}`}
          className="inline-flex items-center gap-1.5 self-start rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[10.5px] font-semibold text-emerald-800"
        >
          <LockKeyhole className="h-3 w-3" />
          {bi("个人专属（仅自己与老板/主管可见）", "Personal (visible to you and your managers)")}
        </div>
      )}

      <button
        type="submit"
        data-testid={`connect-${kind}-btn`}
        disabled={!valid || submitting}
        className="erp-btn mt-auto rounded-xl bg-slate-900 font-semibold text-white shadow-xs transition-colors hover:bg-slate-800 disabled:bg-slate-300 disabled:shadow-none"
      >
        <CheckCircle2 className="h-3 w-3" />
        <span>
          {submitting
            ? bi("连接中…", "Connecting…")
            : kind === "telegram"
              ? bi("一键连接 Telegram", "Connect Telegram in one click")
              : bi("连接渠道", "Connect")}
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
          data-testid={`connect-success-${kind}`}
          className="rounded-xl border border-emerald-200 bg-emerald-50 px-2.5 py-2 text-[11px] font-medium text-emerald-800"
        >
          <div className="flex flex-wrap items-center gap-1.5">
            <CheckCircle2 className="h-3.5 w-3.5" />
            {bi("已连接", "Connected")} {result.external_account_id}
            {result.visibility_scope && (
              <span className="rounded-full bg-white/70 px-1.5 py-0.5 text-[10px] font-semibold text-emerald-900 ring-1 ring-emerald-200">
                {result.visibility_scope === "personal"
                  ? bi("个人专属", "Personal")
                  : bi("公司公海", "Company shared")}
              </span>
            )}
          </div>
          {kind === "telegram" && botUsername && (
            <a
              data-testid="telegram-open-bot-link"
              href={`https://t.me/${botUsername}`}
              target="_blank"
              rel="noreferrer"
              className="mt-1 inline-flex items-center gap-1 font-semibold text-emerald-800 underline decoration-emerald-400 underline-offset-2 hover:text-emerald-900"
            >
              @{botUsername} · {bi("去 Telegram 发消息测试 →", "Test it on Telegram →")}
              <ArrowUpRight className="h-3 w-3" />
            </a>
          )}
        </div>
      )}
    </form>
  );
}
