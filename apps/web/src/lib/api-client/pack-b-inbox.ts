// Pack B (Inbox 导出沟通增强) 专用 API 模块。
//
// 契约已经冻结在 contracts/openapi.json，但 `sdk.gen.ts` / `types.gen.ts` 由
// 后端同学统一重新生成，前端不得手改。因此本文件只做两件事：
//   1. 用现成的 `bffFetch`（/api/bff -> FastAPI /api/v1/*，带 Cookie 与 401 刷新）
//      调用 Pack B 新增端点：会话置顶/标签 PATCH、会话附件上传、AI 翻译；
//   2. 把契约类型（ConversationRead / MessageRead）映射成 UI 类型，并补齐
//      `is_pinned` / `tags` / `attachments` 这些既有 mapper 尚未透传的字段。
//
// Mock 模式（NEXT_PUBLIC_USE_MOCK_API=true）下全部走 mock-fixtures 的本地回退，
// 保证零后端也能完整演示置顶、标签、附件与翻译交互。
import { bffFetch } from "@/lib/api";
import { shouldUseMockApi } from "@/lib/data-source";
import {
  mapChannelParam,
  mapConversation,
  mapInboxMessage,
} from "@/lib/api-client/live-client";
import type { ConversationRead, MessageRead } from "@/lib/api-client/types.gen";
import type { PaginatedResponse } from "@/lib/contracts";
import type { ConversationSummary } from "@/components/inbox/conversation-list";
import type { InboxAttachment, InboxMessage } from "@/components/inbox/message-thread";
import { mockConversationsPage, mockMessagesPage } from "@/lib/mock-fixtures";

const INBOX_FILTERS = ["all", "unread", "pending_human", "sla_urgent"] as const;

/** PATCH /v1/inbox/conversations/{id} 的增量载荷（字段名与契约一致）。 */
export interface ConversationMetaPatch {
  is_pinned?: boolean;
  tags?: string[];
}

/** 会话元数据写回后的最小投影：UI 只需要确认后的置顶态与标签。 */
export interface ConversationMetaResult {
  is_pinned?: boolean;
  tags?: string[];
}

export interface InboxAttachmentUploadResult {
  id: string;
  conversation_id?: string;
  body_text?: string | null;
  attachments?: InboxAttachment[];
}

// ---------------------------------------------------------------------------
// 纯工具函数
// ---------------------------------------------------------------------------

/**
 * 把后端返回的附件地址改写成浏览器可达的 BFF 代理地址。
 * 后端下发 `/api/v1/inbox/attachments/<message_id>/<idx>`，浏览器需要
 * `/api/bff/v1/inbox/attachments/...` 才能带上会话 Cookie 并通过代理。
 * 已经是 BFF 地址或 S3 预签名绝对地址时原样返回。
 */
export function inboxAttachmentUrl(downloadUrl: string | null | undefined): string {
  const url = (downloadUrl ?? "").trim();
  if (!url) return "";
  if (/^https?:\/\//i.test(url)) return url;
  if (url.startsWith("/api/bff/")) return url;
  if (url.startsWith("/api/v1/")) return `/api/bff${url.slice("/api".length)}`;
  if (url.startsWith("/v1/")) return `/api/bff${url}`;
  return url.startsWith("/") ? url : `/${url}`;
}

/** 人类可读附件体积：12 B / 4.2 KB / 3.1 MB。 */
export function formatAttachmentSize(sizeBytes: number | null | undefined): string {
  if (typeof sizeBytes !== "number" || !Number.isFinite(sizeBytes) || sizeBytes < 0) {
    return "";
  }
  if (sizeBytes < 1024) return `${sizeBytes} B`;
  if (sizeBytes < 1024 * 1024) return `${(sizeBytes / 1024).toFixed(1)} KB`;
  return `${(sizeBytes / (1024 * 1024)).toFixed(1)} MB`;
}

function toStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string" && item.length > 0);
}

/** 契约里 `attachments` 只是 `{ [key: string]: unknown }[]`，这里做安全投影。 */
export function mapInboxAttachments(raw: unknown): InboxAttachment[] {
  if (!Array.isArray(raw)) return [];
  const attachments: InboxAttachment[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const record = item as Record<string, unknown>;
    const downloadUrl = typeof record.download_url === "string" ? record.download_url : "";
    const filename = typeof record.filename === "string" ? record.filename : "";
    if (!downloadUrl && !filename) continue;
    attachments.push({
      filename,
      mime_type: typeof record.mime_type === "string" ? record.mime_type : undefined,
      size_bytes: typeof record.size_bytes === "number" ? record.size_bytes : undefined,
      download_url: downloadUrl,
      storage_key: typeof record.storage_key === "string" ? record.storage_key : null,
    });
  }
  return attachments;
}

/**
 * `mapConversation`（live-client）尚未透传 Pack B 字段，这里在它的投影上补齐
 * `is_pinned` / `tags`，避免重写一份重复的映射逻辑。
 */
export function mapPackBConversation(conversation: ConversationRead): ConversationSummary {
  const raw = conversation as unknown as Record<string, unknown>;
  return {
    ...mapConversation(conversation),
    is_pinned: raw.is_pinned === true,
    tags: toStringArray(raw.tags),
  };
}

/** 消息投影 + 附件（既有 mapper 只透传正文与译文）。 */
export function mapPackBInboxMessage(message: MessageRead): InboxMessage {
  const raw = message as unknown as Record<string, unknown>;
  const mapped = mapInboxMessage(message);
  const attachments = mapInboxAttachments(raw.attachments);
  return attachments.length > 0 ? { ...mapped, attachments } : mapped;
}

// ---------------------------------------------------------------------------
// 写操作
// ---------------------------------------------------------------------------

/**
 * PATCH /v1/inbox/conversations/{id} —— 置顶 / 取消置顶 / 覆盖标签集合。
 * `tags` 是整体覆盖语义（契约 tags: string[]），调用方负责传入完整标签列表。
 */
export async function updateConversationMeta(
  conversationId: string,
  payload: ConversationMetaPatch,
): Promise<ConversationMetaResult> {
  if (shouldUseMockApi()) {
    return { is_pinned: payload.is_pinned, tags: payload.tags ?? [] };
  }
  const body: Record<string, unknown> = {};
  if (typeof payload.is_pinned === "boolean") body.is_pinned = payload.is_pinned;
  if (Array.isArray(payload.tags)) body.tags = payload.tags;

  const updated = await bffFetch<Record<string, unknown>>(
    `/v1/inbox/conversations/${conversationId}`,
    {
      method: "PATCH",
      body: JSON.stringify(body),
    },
  );
  return {
    is_pinned: updated.is_pinned === true ? true : payload.is_pinned,
    tags: Array.isArray(updated.tags) ? toStringArray(updated.tags) : payload.tags,
  };
}

/**
 * POST /api/bff/v1/inbox/conversations/{id}/attachments —— multipart 上传图片 / PDF。
 * 关键点：FormData 由浏览器自动带 boundary，绝不能手动设置 Content-Type
 * （bffFetch 已对 FormData 跳过默认 JSON 头）。
 */
export async function uploadInboxAttachment(
  conversationId: string,
  file: File,
  caption?: string,
): Promise<InboxAttachmentUploadResult> {
  const trimmedCaption = (caption ?? "").trim();
  if (shouldUseMockApi()) {
    return {
      id: `mock-attachment-${Date.now()}`,
      conversation_id: conversationId,
      body_text: trimmedCaption || file.name,
      attachments: [
        {
          filename: file.name,
          mime_type: file.type || "application/octet-stream",
          size_bytes: file.size,
          download_url: "",
          storage_key: null,
        },
      ],
    };
  }

  const form = new FormData();
  form.append("file", file, file.name);
  if (trimmedCaption) form.append("caption", trimmedCaption);

  const created = await bffFetch<Record<string, unknown>>(
    `/api/bff/v1/inbox/conversations/${conversationId}/attachments`,
    { method: "POST", body: form },
  );

  return {
    id: typeof created.id === "string" ? created.id : "",
    conversation_id:
      typeof created.conversation_id === "string"
        ? created.conversation_id
        : conversationId,
    body_text: typeof created.body_text === "string" ? created.body_text : null,
    attachments: mapInboxAttachments(created.attachments),
  };
}

// ---------------------------------------------------------------------------
// 读操作
// ---------------------------------------------------------------------------

export async function translateBubbleText(
  text: string,
  targetLang = "zh",
): Promise<string> {
  if (!text.trim()) return "";
  if (shouldUseMockApi()) return text;
  const response = await bffFetch<{ translated_text?: string }>("/v1/ai/translate", {
    method: "POST",
    body: JSON.stringify({ text, target_lang: targetLang }),
  });
  return typeof response.translated_text === "string" ? response.translated_text : "";
}

export interface InboxConversationFilters {
  channel?: string;
  filter?: string;
  query?: string;
  /** 「按绑定账号筛选」下拉的值；空 / null 表示全部账号。 */
  channelAccountId?: string | null;
  /** 服务端 pinned-first 排序 + 仅看置顶。 */
  pinnedOnly?: boolean;
  /** 单个标签过滤。 */
  tag?: string | null;
  limit?: number;
  offset?: number;
}

/**
 * GET /v1/inbox/conversations —— 带上 `channel_account_id` / `pinned_only` / `tag`，
 * 让服务端完成置顶优先排序与账号过滤，而不是前端分页后再切。
 */
export async function fetchFilteredConversations(
  filters: InboxConversationFilters = {},
): Promise<PaginatedResponse<ConversationSummary>> {
  const {
    channel = "all",
    filter = "all",
    query = "",
    channelAccountId = null,
    pinnedOnly = false,
    tag = null,
    limit = 25,
    offset = 0,
  } = filters;

  if (shouldUseMockApi()) {
    const page = mockConversationsPage(channel, filter, query);
    let items = page.items;
    if (channelAccountId) {
      items = items.filter((item) => item.channel_account_id === channelAccountId);
    }
    if (pinnedOnly) items = items.filter((item) => item.is_pinned === true);
    if (tag) items = items.filter((item) => (item.tags ?? []).includes(tag));
    const ordered = [...items].sort(
      (a, b) => Number(b.is_pinned === true) - Number(a.is_pinned === true),
    );
    return { ...page, items: ordered, total: ordered.length };
  }

  const params = new URLSearchParams();
  params.set(
    "filter",
    INBOX_FILTERS.includes(filter as (typeof INBOX_FILTERS)[number]) ? filter : "all",
  );
  const channelParam = mapChannelParam(channel);
  if (channelParam) params.set("channel", channelParam);
  if (query.trim()) params.set("q", query.trim());
  if (channelAccountId) params.set("channel_account_id", channelAccountId);
  if (pinnedOnly) params.set("pinned_only", "true");
  if (tag) params.set("tag", tag);
  params.set("limit", String(limit));
  params.set("offset", String(offset));

  const page = await bffFetch<PaginatedResponse<ConversationRead>>(
    `/v1/inbox/conversations?${params.toString()}`,
  );
  return { ...page, items: page.items.map(mapPackBConversation) };
}

/**
 * GET /v1/inbox/conversations/{id}/messages —— 会话消息（保留附件）。
 * 走 bffFetch 而不是生成 SDK，是为了不落回会丢弃 `attachments` 的旧 mapper。
 */
export async function fetchInboxMessages(
  conversationId: string,
  limit = 50,
): Promise<PaginatedResponse<InboxMessage>> {
  if (shouldUseMockApi()) return mockMessagesPage(conversationId);
  const page = await bffFetch<PaginatedResponse<MessageRead>>(
    `/v1/inbox/conversations/${conversationId}/messages?limit=${limit}&offset=0`,
  );
  return { ...page, items: page.items.map(mapPackBInboxMessage) };
}
