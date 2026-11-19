"use client";

import React, { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AppShell } from "@/components/app-shell";
import { ChannelRail, type ChannelFilterKey } from "@/components/inbox/channel-rail";
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

export default function InboxPage() {
  const [selectedChannel, setSelectedChannel] = useState<ChannelFilterKey>("all");
  const [activeFilter, setActiveFilter] = useState<ConversationFilterTab>("all");
  const [searchQuery, setSearchQuery] = useState("");
  const [selectedConversationId, setSelectedConversationId] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(true);

  const conversationsQuery = useQuery({
    queryKey: ["inbox", "conversations", selectedChannel, activeFilter, searchQuery],
    queryFn: () => fetchConversations(selectedChannel, activeFilter, searchQuery),
  });

  const conversations = conversationsQuery.data?.items ?? [];
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

  return (
    <AppShell pendingProposalsCount={pendingProposalsQuery.data?.total ?? 0}>
      <div className="flex h-[calc(100vh-5.5rem)] overflow-hidden rounded-lg border border-slate-200 bg-white">
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
          />
        </div>
        {drawerOpen && activeConversation && (
          <ContextDrawer customerId={activeConversation.customer_id} />
        )}
      </div>
      <FloatingCopilot conversationId={activeConversation?.id ?? null} />
    </AppShell>
  );
}
