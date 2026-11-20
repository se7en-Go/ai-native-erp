"use client";

import React, { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AppShell } from "@/components/app-shell";
import { useAuthSession } from "@/components/providers";
import { ChannelRail, type ChannelFilterKey } from "@/components/inbox/channel-rail";
import {
  marketplaceChannel,
  marketplaceKeyForChannelFilter,
} from "@/components/channels/channel-marketplace";
import {
  ConversationList,
  type ConversationFilterTab,
} from "@/components/inbox/conversation-list";
import { MessageThread } from "@/components/inbox/message-thread";
import { InboxComposer } from "@/components/inbox/composer";
import { ContextDrawer } from "@/components/inbox/context-drawer";
import { FloatingCopilot } from "@/components/ai/floating-copilot";
import {
  fetchConversationMessages,
  fetchConversations,
  fetchPendingProposalsCount,
} from "@/lib/data-source";
import { useI18n } from "@/lib/i18n";

/** 管层视角切换：全部会话 / 只看我的 / 公司公海。 */
type OwnershipFilter = "all" | "mine" | "shared";

const OWNERSHIP_FILTERS: Array<{ value: OwnershipFilter; zh: string; en: string }> = [
  { value: "all", zh: "全部会话", en: "All" },
  { value: "mine", zh: "只看我的", en: "Mine" },
  { value: "shared", zh: "公司公海", en: "Shared" },
];

export default function InboxPage() {
  const { bi } = useI18n();
  const { principal } = useAuthSession();
  const [selectedChannel, setSelectedChannel] = useState<ChannelFilterKey>("all");
  const [activeFilter, setActiveFilter] = useState<ConversationFilterTab>("all");
  const [searchQuery, setSearchQuery] = useState("");
  const [selectedConversationId, setSelectedConversationId] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(true);
  const [ownershipFilter, setOwnershipFilter] = useState<OwnershipFilter>("all");
  // 抽屉 → 回复框的单向插入通道：nonce 变更即把片段追加到草稿。
  const [replyInsert, setReplyInsert] = useState<{ text: string; nonce: number } | null>(null);

  const canOverseeOwnership =
    principal?.role === "owner" ||
    principal?.role === "admin" ||
    principal?.role === "manager";

  const conversationsQuery = useQuery({
    queryKey: ["inbox", "conversations", selectedChannel, activeFilter, searchQuery],
    queryFn: () => fetchConversations(selectedChannel, activeFilter, searchQuery),
  });

  const allConversations = conversationsQuery.data?.items ?? [];

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
    queryFn: () => fetchConversationMessages(activeConversation!.id),
  });

  const pendingProposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () => fetchPendingProposalsCount(),
  });

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
        />
        <div className="flex min-w-0 flex-1 flex-col">
          <MessageThread
            conversation={activeConversation}
            messages={messagesQuery.data?.items ?? []}
            onToggleDrawer={() => setDrawerOpen((prev) => !prev)}
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
