"use client";

import React, { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pin, Plus, Tag, X } from "lucide-react";
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
  const { bi } = useI18n();
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
      className="grid grid-cols-3 gap-1 rounded-lg bg-[#F4F2EE] p-0.5"
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
            className={`truncate rounded-md px-1.5 py-1 text-[10.5px] font-semibold transition-colors ${
              active
                ? "bg-emerald-600 text-white shadow-xs"
                : "text-stone-600 hover:bg-white/80 hover:text-zinc-950"
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
        data-testid="toggle-pin-conv-btn"
        aria-pressed={activeConversation.is_pinned === true}
        disabled={metaMutation.isPending}
        onClick={() => togglePin(activeConversation)}
        className={`inline-flex items-center gap-1 rounded-lg border px-2 py-1 text-[11.5px] font-semibold transition-colors disabled:opacity-60 ${
          activeConversation.is_pinned
            ? "border-emerald-300 bg-emerald-600 text-white shadow-[0_1px_6px_-2px_rgba(5,150,105,0.7)] hover:bg-emerald-700"
            : "border-[#E5E2DC] bg-white text-stone-700 hover:border-emerald-200 hover:bg-emerald-50 hover:text-emerald-800"
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
        <div className="inline-flex items-center gap-1 rounded-lg border border-[#E5E2DC] bg-[#FAF8F5] px-1.5 py-1">
          <Tag className="h-3 w-3 shrink-0 text-emerald-700" />
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
            className="w-20 border-0 bg-transparent p-0 text-[11.5px] font-medium text-stone-800 placeholder:text-stone-400 focus:outline-none"
          />
          <button
            type="button"
            data-testid="add-conv-tag-submit"
            aria-label={bi("确认添加标签", "Confirm new tag")}
            disabled={!tagDraft.trim()}
            onClick={() => commitTag(activeConversation, tagDraft)}
            className="grid h-4 w-4 shrink-0 place-items-center rounded-full text-emerald-700 transition-colors hover:bg-emerald-100 disabled:opacity-40"
          >
            <Plus className="h-3 w-3" />
          </button>
        </div>

        <button
          type="button"
          data-testid="conv-tag-panel-btn"
          aria-expanded={tagPanelOpen}
          onClick={() => setTagPanelOpen((prev) => !prev)}
          className="inline-flex items-center gap-1 rounded-lg border border-[#E5E2DC] bg-white px-2 py-1 text-[11.5px] font-semibold text-stone-700 transition-colors hover:border-emerald-200 hover:bg-emerald-50 hover:text-emerald-800"
        >
          <Tag className="h-3 w-3" />
          <span>{bi("标签", "Tags")}</span>
        </button>

        {tagPanelOpen && (
          <div
            data-testid="conv-tag-panel"
            className="absolute top-8 left-0 z-20 w-56 rounded-xl border border-[#E5E2DC] bg-white p-2 shadow-[0_12px_32px_-12px_rgba(15,23,42,0.32)]"
          >
            <p className="mb-1.5 px-0.5 text-[10.5px] font-semibold text-stone-500">
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
                        ? "bg-emerald-600 text-white ring-emerald-700"
                        : "bg-emerald-50 text-emerald-800 ring-emerald-200 hover:bg-emerald-100"
                    }`}
                  >
                    {tag}
                  </button>
                );
              })}
            </div>
            {(activeConversation.tags ?? []).length > 0 && (
              <div className="mt-2 space-y-1 border-t border-stone-100 pt-1.5">
                <p className="px-0.5 text-[10.5px] font-semibold text-stone-500">
                  {bi("已打标签", "Applied")}
                </p>
                {(activeConversation.tags ?? []).map((tag) => (
                  <div
                    key={tag}
                    className="flex items-center justify-between gap-1 rounded-md bg-stone-50 px-1.5 py-0.5 text-[11px] font-medium text-stone-700"
                  >
                    <span className="truncate">{tag}</span>
                    <button
                      type="button"
                      data-testid={`remove-conv-tag-${tag}`}
                      aria-label={bi(`移除标签 ${tag}`, `Remove tag ${tag}`)}
                      onClick={() => removeTag(activeConversation, tag)}
                      className="grid h-4 w-4 shrink-0 place-items-center rounded-full text-stone-500 hover:bg-rose-50 hover:text-rose-700"
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
          className="rounded-md bg-rose-50 px-1.5 py-0.5 text-[10.5px] font-medium text-rose-700 ring-1 ring-rose-200"
        >
          {metaError}
        </span>
      )}
    </div>
  ) : undefined;

  return (
    <AppShell pendingProposalsCount={pendingProposalsQuery.data?.total ?? 0}>
      <div className="flex h-[calc(100vh-4rem)] w-full overflow-hidden bg-white">
        <ChannelRail
          selectedChannel={selectedChannel}
          onSelectChannel={setSelectedChannel}
          unreadByChannel={{}}
        />
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
        <div className="flex min-w-0 flex-1 flex-col">
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
            onInsertReplyText={(text) =>
              setReplyInsert((previous) => ({
                text,
                nonce: (previous?.nonce ?? 0) + 1,
              }))
            }
          />
        )}
      </div>
      <FloatingCopilot conversationId={activeConversation?.id ?? null} />
    </AppShell>
  );
}
