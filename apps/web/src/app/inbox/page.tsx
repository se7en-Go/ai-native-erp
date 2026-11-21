"use client";

import React, { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pin, Plus, Sparkles, Tag, X } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { useAuthSession } from "@/components/providers";
import { ChannelRail, type ChannelFilterKey } from "@/components/inbox/channel-rail";
import {
  marketplaceChannel,
  marketplaceKeyForChannelFilter,
} from "@/components/channels/channel-marketplace";
import {
  ConversationList,
  type ConversationAccountOption,
  type ConversationFilterTab,
  type ConversationSummary,
} from "@/components/inbox/conversation-list";
import { MessageThread } from "@/components/inbox/message-thread";
import { InboxComposer } from "@/components/inbox/composer";
import { ContextDrawer } from "@/components/inbox/context-drawer";
import { FloatingCopilot } from "@/components/ai/floating-copilot";
import {
  fetchChannelAccounts,
  fetchPendingProposalsCount,
} from "@/lib/data-source";
import {
  fetchFilteredConversations,
  fetchInboxMessages,
  updateConversationMeta,
  type ConversationMetaPatch,
} from "@/lib/api-client/pack-b-inbox";
import {
  autoReplyActionLabel,
  autoReplyActionTone,
  autoReplyEvidenceCounts,
  fetchAutoReplySetting,
  formatConfidencePercent,
  runAiAutoReply,
  updateAutoReplySetting,
  type AutoReplyRun,
} from "@/lib/api-client/pack-e-autoreply";
import { useI18n } from "@/lib/i18n";

/** 管层视角切换：全部会话 / 只看我的 / 公司公海。 */
type OwnershipFilter = "all" | "mine" | "shared";

const OWNERSHIP_FILTERS: Array<{ value: OwnershipFilter; zh: string; en: string }> = [
  { value: "all", zh: "全部会话", en: "All" },
  { value: "mine", zh: "只看我的", en: "Mine" },
  { value: "shared", zh: "公司公海", en: "Shared" },
];

/** 标签管理里的快捷预设（外贸跟单真实工作流）。 */
const PRESET_TAGS = ["大客户", "询盘中", "待寄样", "VIP"] as const;

type ConversationMeta = { is_pinned: boolean; tags: string[] };

function metaOf(conversation: ConversationSummary): ConversationMeta {
  return {
    is_pinned: conversation.is_pinned === true,
    tags: conversation.tags ?? [],
  };
}

export default function InboxPage() {
  const { bi, locale } = useI18n();
  const { principal } = useAuthSession();
  const queryClient = useQueryClient();
  const [selectedChannel, setSelectedChannel] = useState<ChannelFilterKey>("all");
  const [activeFilter, setActiveFilter] = useState<ConversationFilterTab>("all");
  const [searchQuery, setSearchQuery] = useState("");
  const [selectedConversationId, setSelectedConversationId] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(true);
  const [ownershipFilter, setOwnershipFilter] = useState<OwnershipFilter>("all");
  // Pack B：按绑定账号筛选 + 仅看置顶（都下推到服务端查询参数）。
  const [channelAccountFilter, setChannelAccountFilter] = useState<string>("all");
  const [pinnedOnly, setPinnedOnly] = useState(false);
  // Pack B：置顶 / 标签的乐观覆盖（服务端返回前 UI 立即响应）。
  const [metaOverrides, setMetaOverrides] = useState<Record<string, ConversationMeta>>({});
  const [tagDraft, setTagDraft] = useState("");
  const [tagPanelOpen, setTagPanelOpen] = useState(false);
  const [metaError, setMetaError] = useState<string | null>(null);
  // 抽屉 → 回复框的单向插入通道：nonce 变更即把片段追加到草稿。
  const [replyInsert, setReplyInsert] = useState<{ text: string; nonce: number } | null>(null);
  // Pack E：AI 查库自动回复的结果条（可手动关闭）与错误提示。
  const [autoReplyResult, setAutoReplyResult] = useState<AutoReplyRun | null>(null);
  const [autoReplyError, setAutoReplyError] = useState<string | null>(null);

  const canOverseeOwnership =
    principal?.role === "owner" ||
    principal?.role === "admin" ||
    principal?.role === "manager";

  const conversationsQuery = useQuery({
    queryKey: [
      "inbox",
      "conversations",
      selectedChannel,
      activeFilter,
      searchQuery,
      channelAccountFilter,
      pinnedOnly,
    ],
    queryFn: () =>
      fetchFilteredConversations({
        channel: selectedChannel,
        filter: activeFilter,
        query: searchQuery,
        channelAccountId: channelAccountFilter === "all" ? null : channelAccountFilter,
        pinnedOnly,
      }),
  });

  const channelAccountsQuery = useQuery({
    queryKey: ["channels", "accounts", "inbox-filter"],
    queryFn: () => fetchChannelAccounts(),
  });

  const channelAccountOptions: ConversationAccountOption[] = useMemo(
    () =>
      (channelAccountsQuery.data?.items ?? []).map((account) => ({
        id: account.id,
        display_name: account.display_name || account.external_account_id,
      })),
    [channelAccountsQuery.data]
  );

  const allConversations: ConversationSummary[] = useMemo(() => {
    const items = conversationsQuery.data?.items ?? [];
    return items.map((conversation) => {
      const override = metaOverrides[conversation.id];
      return override ? { ...conversation, ...override } : conversation;
    });
  }, [conversationsQuery.data, metaOverrides]);

  const conversations = useMemo(() => {
    if (!canOverseeOwnership || ownershipFilter === "all") return allConversations;
    if (ownershipFilter === "mine") {
      return allConversations.filter(
        (conv) =>
          Boolean(principal?.user_id) && conv.assigned_user_id === principal?.user_id,
      );
    }
    return allConversations.filter((conv) => !conv.assigned_user_id);
  }, [allConversations, canOverseeOwnership, ownershipFilter, principal?.user_id]);

  const activeConversation = useMemo(
    () =>
      conversations.find((c) => c.id === selectedConversationId) ?? conversations[0] ?? null,
    [conversations, selectedConversationId]
  );

  const messagesQuery = useQuery({
    queryKey: ["inbox", "messages", activeConversation?.id],
    enabled: Boolean(activeConversation?.id),
    queryFn: () => fetchInboxMessages(activeConversation!.id),
  });

  const pendingProposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () => fetchPendingProposalsCount(),
  });

  // Pack E：当前会话的 AI 自动回复开关（失败不影响页面，默认按「关」）。
  const autoReplySettingQuery = useQuery({
    queryKey: ["ai", "auto-reply-setting", activeConversation?.id],
    enabled: Boolean(activeConversation?.id),
    retry: false,
    queryFn: () => fetchAutoReplySetting(activeConversation!.id),
  });

  const autoReplyEnabled = autoReplySettingQuery.data?.ai_auto_reply_enabled === true;

  const autoReplyToggleMutation = useMutation({
    mutationFn: ({ conversationId, enabled }: { conversationId: string; enabled: boolean }) =>
      updateAutoReplySetting(conversationId, enabled),
    onSuccess: (setting) => {
      queryClient.setQueryData(
        ["ai", "auto-reply-setting", setting.conversation_id],
        setting,
      );
      setAutoReplyError(null);
    },
    onError: (error) => {
      setAutoReplyError(
        error instanceof Error && error.message
          ? error.message
          : bi("自动回复开关保存失败，请重试。", "Failed to save auto-reply toggle."),
      );
    },
  });

  const autoReplyRunMutation = useMutation({
    mutationFn: ({ conversationId }: { conversationId: string }) =>
      runAiAutoReply(conversationId, "auto"),
    onSuccess: (result) => {
      setAutoReplyResult(result);
      setAutoReplyError(null);
      // 自动回复往往已写入一条出站消息：立即刷新对话区。
      void queryClient.invalidateQueries({
        queryKey: ["inbox", "messages", result.conversation_id],
      });
      void queryClient.invalidateQueries({ queryKey: ["inbox", "conversations"] });
    },
    onError: (error) => {
      setAutoReplyError(
        error instanceof Error && error.message
          ? error.message
          : bi("AI 自动回复执行失败，请重试。", "AI auto-reply failed. Please retry."),
      );
    },
  });

  // Pack B：会话元数据（置顶 / 标签）写回，失败回滚乐观覆盖。
  const metaMutation = useMutation({
    mutationFn: async ({
      conversationId,
      patch,
    }: {
      conversationId: string;
      patch: ConversationMetaPatch;
      previous: ConversationMeta;
    }) => updateConversationMeta(conversationId, patch),
    onSuccess: (result, variables) => {
      setMetaOverrides((prev) => {
        const merged = { ...prev[variables.conversationId], ...variables.patch };
        return {
          ...prev,
          [variables.conversationId]: {
            is_pinned: Boolean(merged.is_pinned ?? result.is_pinned),
            tags: merged.tags ?? result.tags ?? [],
          },
        };
      });
      setMetaError(null);
      void queryClient.invalidateQueries({ queryKey: ["inbox", "conversations"] });
    },
    onError: (error, variables) => {
      setMetaOverrides((prev) => ({
        ...prev,
        [variables.conversationId]: variables.previous,
      }));
      setMetaError(
        error instanceof Error && error.message
          ? error.message
          : bi("会话标签或置顶保存失败，请重试。", "Failed to save pin/tags. Please retry.")
      );
    },
  });

  function applyMeta(conversation: ConversationSummary, patch: ConversationMetaPatch) {
    const previous = metaOf(conversation);
    const optimistic: ConversationMeta = {
      is_pinned: patch.is_pinned ?? previous.is_pinned,
      tags: patch.tags ?? previous.tags,
    };
    setMetaOverrides((prev) => ({ ...prev, [conversation.id]: optimistic }));
    metaMutation.mutate({ conversationId: conversation.id, patch, previous });
  }

  function togglePin(conversation: ConversationSummary) {
    const current = metaOf(conversation);
    applyMeta(conversation, { is_pinned: !current.is_pinned });
  }

  function commitTag(conversation: ConversationSummary, rawTag: string) {
    const tag = rawTag.trim();
    if (!tag) return;
    const current = metaOf(conversation);
    if (current.tags.includes(tag)) {
      setTagDraft("");
      return;
    }
    applyMeta(conversation, { tags: [...current.tags, tag] });
    setTagDraft("");
  }

  function removeTag(conversation: ConversationSummary, tag: string) {
    const current = metaOf(conversation);
    applyMeta(conversation, { tags: current.tags.filter((item) => item !== tag) });
  }

  // 渠道筛选 → 渠道中心深链：直接打开对应渠道的快捷绑定抽屉。
  const quickConnectKey =
    selectedChannel === "all" ? null : marketplaceKeyForChannelFilter(selectedChannel);
  const quickConnectHref = quickConnectKey
    ? `/settings/channels?connect=${quickConnectKey}`
    : "/settings/channels";
  const quickConnectLabel = quickConnectKey
    ? bi(
        `快捷绑定 ${marketplaceChannel(quickConnectKey).titleZh} →`,
        `Quick connect ${marketplaceChannel(quickConnectKey).titleEn} →`,
      )
    : undefined;

  const ownershipSlot = canOverseeOwnership ? (
    <div
      role="group"
      aria-label={bi("按归属筛选会话", "Filter conversations by ownership")}
      className="grid grid-cols-3 gap-1 rounded-xl bg-gray-100 p-0.5 dark:bg-gray-800"
    >
      {OWNERSHIP_FILTERS.map((option) => {
        const active = ownershipFilter === option.value;
        return (
          <button
            key={option.value}
            type="button"
            aria-pressed={active}
            data-testid={`inbox-ownership-${option.value}`}
            onClick={() => setOwnershipFilter(option.value)}
            className={`truncate rounded-lg px-1.5 py-1 text-[10.5px] font-semibold transition-colors ${
              active
                ? "bg-blue-600 text-white shadow-xs"
                : "text-gray-600 hover:bg-white hover:text-gray-950 dark:text-gray-300 dark:hover:bg-gray-700 dark:hover:text-white"
            }`}
          >
            {bi(option.zh, option.en)}
          </button>
        );
      })}
    </div>
  ) : undefined;

  const headerActions = activeConversation ? (
    <div className="flex flex-wrap items-center gap-1.5">
      <button
        type="button"
        data-testid="inbox-ai-autoreply-toggle"
        role="switch"
        aria-checked={autoReplyEnabled}
        aria-label={bi("AI 自动回复开关", "AI auto-reply switch")}
        disabled={autoReplyToggleMutation.isPending || autoReplySettingQuery.isLoading}
        onClick={() =>
          autoReplyToggleMutation.mutate({
            conversationId: activeConversation.id,
            enabled: !autoReplyEnabled,
          })
        }
        className={`inline-flex items-center gap-1 rounded-lg border px-2 py-1 text-[11.5px] font-semibold transition-colors disabled:opacity-60 ${
          autoReplyEnabled
            ? "border-blue-600 bg-blue-50 text-blue-700 hover:bg-blue-100 dark:border-blue-500/60 dark:bg-blue-500/15 dark:text-blue-300 dark:hover:bg-blue-500/25"
            : "border-gray-200 bg-white text-gray-600 hover:border-blue-200 hover:bg-blue-50 hover:text-blue-700 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-400 dark:hover:border-blue-500/40 dark:hover:bg-blue-500/10 dark:hover:text-blue-300"
        }`}
      >
        <Sparkles className={`h-3 w-3 ${autoReplyEnabled ? "" : "opacity-60"}`} />
        <span>
          {bi("AI 自动回复", "AI auto-reply")}
          <span
            data-testid="inbox-ai-autoreply-state"
            className={`ml-1 rounded px-1 py-px text-[10px] font-bold ${
              autoReplyEnabled
                ? "bg-blue-600 text-white dark:bg-blue-500"
                : "bg-gray-200 text-gray-600 dark:bg-gray-700 dark:text-gray-300"
            }`}
          >
            {autoReplyEnabled ? bi("开", "On") : bi("关", "Off")}
          </span>
        </span>
      </button>

      <button
        type="button"
        data-testid="inbox-ai-autoreply-run-btn"
        disabled={autoReplyRunMutation.isPending || !activeConversation.id}
        onClick={() =>
          autoReplyRunMutation.mutate({ conversationId: activeConversation.id })
        }
        className="inline-flex items-center gap-1 rounded-lg bg-blue-600 px-2 py-1 text-[11.5px] font-semibold text-white shadow-[0_1px_8px_-3px_rgba(37,99,235,0.6)] transition-colors hover:bg-blue-700 disabled:opacity-60 dark:bg-blue-500 dark:hover:bg-blue-400"
      >
        <Sparkles className="h-3 w-3" />
        <span>
          {autoReplyRunMutation.isPending
            ? bi("查库回复中…", "Grounded reply…")
            : bi("AI 查库自动回复", "AI grounded reply")}
        </span>
      </button>

      <button
        type="button"
        data-testid="toggle-pin-conv-btn"
        aria-pressed={activeConversation.is_pinned === true}
        disabled={metaMutation.isPending}
        onClick={() => togglePin(activeConversation)}
        className={`inline-flex items-center gap-1 rounded-lg border px-2 py-1 text-[11.5px] font-semibold transition-colors disabled:opacity-60 ${
          activeConversation.is_pinned
            ? "border-blue-600 bg-blue-600 text-white shadow-[0_1px_6px_-2px_rgba(37,99,235,0.45)] hover:bg-blue-700"
            : "border-gray-200 bg-white text-gray-700 hover:border-blue-200 hover:bg-blue-50 hover:text-blue-700 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300 dark:hover:border-blue-500/40 dark:hover:bg-blue-500/10 dark:hover:text-blue-300"
        }`}
      >
        <Pin className={`h-3 w-3 ${activeConversation.is_pinned ? "rotate-45" : ""}`} />
        <span>
          {activeConversation.is_pinned
            ? bi("取消置顶", "Unpin")
            : bi("置顶", "Pin")}
        </span>
      </button>

      <div className="relative flex items-center gap-1">
        <div className="inline-flex items-center gap-1 rounded-lg border border-gray-200 bg-gray-50 px-1.5 py-1 dark:border-gray-700 dark:bg-gray-800">
          <Tag className="h-3 w-3 shrink-0 text-blue-600" />
          <input
            type="text"
            data-testid="add-conv-tag-input"
            value={tagDraft}
            onChange={(event) => setTagDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                commitTag(activeConversation, tagDraft);
              }
            }}
            placeholder={bi("加标签…", "Add tag…")}
            aria-label={bi("添加会话标签", "Add conversation tag")}
            className="w-20 border-0 bg-transparent p-0 text-[11.5px] font-medium text-gray-800 placeholder:text-gray-400 focus:outline-none dark:text-gray-100 dark:placeholder:text-gray-500"
          />
          <button
            type="button"
            data-testid="add-conv-tag-submit"
            aria-label={bi("确认添加标签", "Confirm new tag")}
            disabled={!tagDraft.trim()}
            onClick={() => commitTag(activeConversation, tagDraft)}
            className="grid h-4 w-4 shrink-0 place-items-center rounded-full text-blue-600 transition-colors hover:bg-blue-100 disabled:opacity-40 dark:hover:bg-blue-500/15"
          >
            <Plus className="h-3 w-3" />
          </button>
        </div>

        <button
          type="button"
          data-testid="conv-tag-panel-btn"
          aria-expanded={tagPanelOpen}
          onClick={() => setTagPanelOpen((prev) => !prev)}
          className="inline-flex items-center gap-1 rounded-lg border border-gray-200 bg-white px-2 py-1 text-[11.5px] font-semibold text-gray-700 transition-colors hover:border-blue-200 hover:bg-blue-50 hover:text-blue-700 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300 dark:hover:border-blue-500/40 dark:hover:bg-blue-500/10 dark:hover:text-blue-300"
        >
          <Tag className="h-3 w-3" />
          <span>{bi("标签", "Tags")}</span>
        </button>

        {tagPanelOpen && (
          <div
            data-testid="conv-tag-panel"
            className="absolute top-8 left-0 z-20 w-56 rounded-xl border border-gray-200 bg-white p-2 shadow-[0_12px_32px_-12px_rgba(15,23,42,0.32)] dark:border-gray-700 dark:bg-gray-900"
          >
            <p className="mb-1.5 px-0.5 text-[10.5px] font-semibold text-gray-500 dark:text-gray-400">
              {bi("快捷标签", "Quick tags")}
            </p>
            <div className="flex flex-wrap gap-1">
              {PRESET_TAGS.map((tag) => {
                const applied = (activeConversation.tags ?? []).includes(tag);
                return (
                  <button
                    key={tag}
                    type="button"
                    data-testid={`conv-tag-preset-${tag}`}
                    aria-pressed={applied}
                    onClick={() =>
                      applied
                        ? removeTag(activeConversation, tag)
                        : applyMeta(activeConversation, {
                            tags: [...(activeConversation.tags ?? []), tag],
                          })
                    }
                    className={`rounded-md px-1.5 py-0.5 text-[11px] font-semibold ring-1 transition-colors ${
                      applied
                        ? "bg-blue-600 text-white ring-blue-700"
                        : "bg-blue-50 text-blue-700 ring-blue-200 hover:bg-blue-100 dark:bg-blue-500/10 dark:text-blue-300 dark:ring-blue-500/30 dark:hover:bg-blue-500/15"
                    }`}
                  >
                    {tag}
                  </button>
                );
              })}
            </div>
            {(activeConversation.tags ?? []).length > 0 && (
              <div className="mt-2 space-y-1 border-t border-gray-100 pt-1.5 dark:border-gray-800">
                <p className="px-0.5 text-[10.5px] font-semibold text-gray-500 dark:text-gray-400">
                  {bi("已打标签", "Applied")}
                </p>
                {(activeConversation.tags ?? []).map((tag) => (
                  <div
                    key={tag}
                    className="flex items-center justify-between gap-1 rounded-md bg-gray-50 px-1.5 py-0.5 text-[11px] font-medium text-gray-700 dark:bg-gray-800 dark:text-gray-200"
                  >
                    <span className="truncate">{tag}</span>
                    <button
                      type="button"
                      data-testid={`remove-conv-tag-${tag}`}
                      aria-label={bi(`移除标签 ${tag}`, `Remove tag ${tag}`)}
                      onClick={() => removeTag(activeConversation, tag)}
                      className="grid h-4 w-4 shrink-0 place-items-center rounded-full text-gray-500 hover:bg-rose-50 hover:text-rose-700 dark:hover:bg-rose-500/10"
                    >
                      <X className="h-2.5 w-2.5" />
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      {metaError && (
        <span
          role="alert"
          data-testid="conversation-meta-error"
          className="rounded-md bg-rose-50 px-1.5 py-0.5 text-[10.5px] font-medium text-rose-700 ring-1 ring-rose-200 dark:bg-rose-500/10 dark:text-rose-300 dark:ring-rose-500/30"
        >
          {metaError}
        </span>
      )}

      {autoReplyError && (
        <span
          role="alert"
          data-testid="inbox-ai-autoreply-error"
          className="rounded-md bg-rose-50 px-1.5 py-0.5 text-[10.5px] font-medium text-rose-700 ring-1 ring-rose-200 dark:bg-rose-500/10 dark:text-rose-300 dark:ring-rose-500/30"
        >
          {autoReplyError}
        </span>
      )}
    </div>
  ) : undefined;

  const autoReplyEvidence = autoReplyResult
    ? autoReplyEvidenceCounts(autoReplyResult)
    : null;
  const autoReplyTone = autoReplyResult
    ? autoReplyActionTone(autoReplyResult.action)
    : "neutral";
  const autoReplyToneClass =
    autoReplyTone === "success"
      ? "border-blue-200/80 bg-blue-50/70 text-blue-900 dark:border-blue-500/30 dark:bg-blue-500/10 dark:text-blue-100"
      : autoReplyTone === "warning"
        ? "border-amber-200/80 bg-amber-50/70 text-amber-900 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-100"
        : "border-gray-200 bg-white text-gray-800 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200";

  return (
    <AppShell pendingProposalsCount={pendingProposalsQuery.data?.total ?? 0}>
      <div className="flex h-[calc(100vh-4rem)] min-h-0 w-full flex-col overflow-hidden bg-gray-50 dark:bg-gray-950">
        <ChannelRail
          variant="compact"
          selectedChannel={selectedChannel}
          onSelectChannel={setSelectedChannel}
          unreadByChannel={{}}
        />
        <div className="flex min-h-0 flex-1 overflow-hidden bg-white dark:bg-gray-900">
          <ConversationList
            conversations={conversations}
            selectedConversationId={activeConversation?.id ?? null}
            activeFilter={activeFilter}
            searchQuery={searchQuery}
            onSelectFilter={setActiveFilter}
            onSearchChange={setSearchQuery}
            onSelectConversation={setSelectedConversationId}
            headerSlot={ownershipSlot}
            quickConnectHref={quickConnectHref}
            quickConnectLabel={quickConnectLabel}
            channelAccounts={channelAccountOptions}
            channelAccountFilter={channelAccountFilter}
            onChannelAccountFilterChange={setChannelAccountFilter}
            pinnedOnly={pinnedOnly}
            onTogglePinnedOnly={() => setPinnedOnly((prev) => !prev)}
          />
          <div className="flex min-h-0 min-w-0 flex-1 overflow-hidden">
            <div className="flex min-h-0 min-w-0 flex-1 flex-col">
              {autoReplyResult && (
                <div
                  data-testid="inbox-autoreply-result"
                  role="status"
                  className={`mx-4 mt-3 flex items-start gap-2 rounded-xl border px-3 py-2 shadow-[0_1px_2px_rgba(15,23,42,0.04)] ${autoReplyToneClass}`}
                >
                  <Sparkles className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                  <div className="min-w-0 flex-1">
                    <p className="text-[12px] font-semibold">
                      {autoReplyActionLabel(autoReplyResult.action, locale)}
                    </p>
                    <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[10.5px] font-medium">
                      <span
                        data-testid="inbox-autoreply-kb-hits"
                        className="rounded-md bg-white/70 px-1.5 py-0.5 ring-1 ring-black/5 dark:bg-white/10 dark:ring-white/10"
                      >
                        {bi("知识库命中", "KB hits")} {autoReplyEvidence?.kbHits ?? 0}{" "}
                        {bi("条", "chunks")}
                      </span>
                      <span
                        data-testid="inbox-autoreply-tool-evidence"
                        className="rounded-md bg-white/70 px-1.5 py-0.5 ring-1 ring-black/5 dark:bg-white/10 dark:ring-white/10"
                      >
                        {bi("ERP 工具证据", "ERP tool evidence")}{" "}
                        {autoReplyEvidence?.toolEvidence ?? 0} {bi("条", "rows")}
                      </span>
                      <span className="tabular-nums opacity-80">
                        {bi("置信度", "Confidence")}{" "}
                        {formatConfidencePercent(autoReplyResult.confidence)}
                      </span>
                      {(autoReplyResult.risk_flags ?? []).length > 0 && (
                        <span
                          data-testid="inbox-autoreply-risk-flags"
                          className="rounded-md bg-rose-100 px-1.5 py-0.5 font-semibold text-rose-700 dark:bg-rose-500/20 dark:text-rose-200"
                        >
                          {bi("风险红线", "Risk red line")}：{(autoReplyResult.risk_flags ?? []).join(" · ")}
                        </span>
                      )}
                    </div>
                    {autoReplyResult.reply_text && (
                      <p className="mt-1.5 line-clamp-2 text-[11px] leading-relaxed opacity-80">
                        {autoReplyResult.reply_text}
                      </p>
                    )}
                  </div>
                  <button
                    type="button"
                    data-testid="inbox-autoreply-result-dismiss"
                    aria-label={bi("关闭自动回复结果", "Dismiss auto-reply result")}
                    onClick={() => setAutoReplyResult(null)}
                    className="grid h-5 w-5 shrink-0 place-items-center rounded-full opacity-60 transition-colors hover:bg-black/5 hover:opacity-100 dark:hover:bg-white/10"
                  >
                    <X className="h-3 w-3" />
                  </button>
                </div>
              )}
              <MessageThread
                conversation={activeConversation}
                messages={messagesQuery.data?.items ?? []}
                onToggleDrawer={() => setDrawerOpen((prev) => !prev)}
                headerActions={headerActions}
              />
              <InboxComposer
                conversation={activeConversation}
                messages={messagesQuery.data?.items ?? []}
                insertRequest={replyInsert}
              />
            </div>
            {drawerOpen && activeConversation && (
              <ContextDrawer
                customerId={activeConversation.customer_id}
                conversationId={activeConversation.id}
                onInsertReplyText={(text) =>
                  setReplyInsert((previous) => ({
                    text,
                    nonce: (previous?.nonce ?? 0) + 1,
                  }))
                }
              />
            )}
          </div>
        </div>
      </div>
      <FloatingCopilot conversationId={activeConversation?.id ?? null} />
    </AppShell>
  );
}
