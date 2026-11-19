import { bffFetch } from "@/lib/api";
import type {
  AiCapability,
  AutonomyLevel,
  PaginatedResponse,
  ProposalStatus,
  ProposalType,
} from "@/lib/contracts";
import {
  mockCapabilities,
  mockConversationsPage,
  mockKbDocuments,
  mockMessagesPage,
  mockProposals,
  mockWallet,
} from "@/lib/mock-fixtures";
import type { ConversationSummary } from "@/components/inbox/conversation-list";
import type { InboxMessage } from "@/components/inbox/message-thread";

// Mock mode is opt-in only: set NEXT_PUBLIC_USE_MOCK_API=true to run the
// workbench with zero backend. The default (unset/false) always calls the
// real BFF, so plan tests that stub global fetch keep exercising that path.
export function shouldUseMockApi(): boolean {
  return process.env.NEXT_PUBLIC_USE_MOCK_API === "true";
}

export function fetchConversations(
  channel: string,
  filter: string,
  query: string,
): Promise<PaginatedResponse<ConversationSummary>> {
  if (shouldUseMockApi()) {
    return Promise.resolve(mockConversationsPage(channel, filter, query));
  }
  const params = new URLSearchParams({
    channel,
    filter,
    q: query,
    limit: "25",
    offset: "0",
  });
  return bffFetch<PaginatedResponse<ConversationSummary>>(
    `/v1/inbox/conversations?${params.toString()}`,
  );
}

export function fetchConversationMessages(
  conversationId: string,
): Promise<PaginatedResponse<InboxMessage>> {
  if (shouldUseMockApi()) {
    return Promise.resolve(mockMessagesPage(conversationId));
  }
  return bffFetch<PaginatedResponse<InboxMessage>>(
    `/v1/inbox/conversations/${conversationId}/messages?limit=50&offset=0`,
  );
}

export interface CapabilityConfigItem {
  capability: AiCapability;
  configured_autonomy: AutonomyLevel;
  max_allowed_autonomy: AutonomyLevel;
}

export interface KbDocumentItem {
  id: string;
  title: string;
  chunk_count: number;
  status: "pending" | "indexed" | "failed";
  created_at: string;
}

export function fetchProposals<T>(
  status: ProposalStatus,
  typeFilter: "" | ProposalType,
): Promise<PaginatedResponse<T>> {
  if (shouldUseMockApi()) {
    const items = mockProposals.filter(
      (p) => p.status === status && (!typeFilter || p.proposal_type === typeFilter),
    );
    return Promise.resolve({
      items: items as unknown as T[],
      total: items.length,
      limit: 20,
      offset: 0,
      has_more: false,
    });
  }
  const params = new URLSearchParams({ status, limit: "20", offset: "0" });
  if (typeFilter) params.set("proposal_type", typeFilter);
  return bffFetch<PaginatedResponse<T>>(`/v1/ai/proposals?${params.toString()}`);
}

export function fetchCapabilities(): Promise<{
  capabilities: CapabilityConfigItem[];
}> {
  if (shouldUseMockApi()) {
    return Promise.resolve({
      capabilities: mockCapabilities.map((c) => ({
        capability: c.capability as AiCapability,
        configured_autonomy: c.configured_autonomy as AutonomyLevel,
        max_allowed_autonomy: c.max_allowed_autonomy as AutonomyLevel,
      })),
    });
  }
  return bffFetch<{ capabilities: CapabilityConfigItem[] }>("/v1/ai/capabilities");
}

export function fetchWallet(): Promise<typeof mockWallet> {
  if (shouldUseMockApi()) {
    return Promise.resolve(mockWallet);
  }
  return bffFetch<typeof mockWallet>("/v1/billing/wallet");
}

export function fetchKbDocuments(): Promise<PaginatedResponse<KbDocumentItem>> {
  if (shouldUseMockApi()) {
    return Promise.resolve({
      items: mockKbDocuments as KbDocumentItem[],
      total: mockKbDocuments.length,
      limit: 20,
      offset: 0,
      has_more: false,
    });
  }
  return bffFetch<PaginatedResponse<KbDocumentItem>>("/v1/ai/kb/documents");
}

export function fetchPendingProposalsCount(): Promise<{ total: number }> {
  if (shouldUseMockApi()) {
    return Promise.resolve({
      total: mockProposals.filter((p) => p.status === "pending").length,
    });
  }
  return bffFetch<{ total: number }>(
    "/v1/ai/proposals?status=pending&limit=1&offset=0",
  );
}
