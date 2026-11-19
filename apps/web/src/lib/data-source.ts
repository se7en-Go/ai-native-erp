import { bffFetch } from "@/lib/api";
import {
  getB2bOpportunities,
  getB2bQuotes,
  getCatalogVariants,
  getChannelAccounts,
  getChannelConsents,
  getChannelDlq,
  getChannelSuppressions,
  getFinanceOverview,
  getInventoryBalances,
  getInventoryLedger,
  getOnboardingState,
  getOrderDetail,
  getOrders,
  getVariantMappings,
  type B2bOpportunityApiItem,
  type B2bQuoteApiItem,
  type CatalogVariantApiItem,
  type ChannelAccountApiItem,
  type ConsentLogApiItem,
  type DlqEventApiItem,
  type FinanceOverviewApiItem,
  type InventoryBalanceApiItem,
  type InventoryLedgerApiItem,
  type OnboardingStateApiItem,
  type OrderDetailApiItem,
  type OrderSummaryApiItem,
  type PlatformSkuMappingApiItem,
  type SuppressionLogApiItem,
} from "@/lib/api-client/client.gen";
import type {
  AiCapability,
  AutonomyLevel,
  PaginatedResponse,
  ProposalStatus,
  ProposalType,
} from "@/lib/contracts";
import {
  mockCapabilities,
  mockChannelAccounts,
  mockChannelConsents,
  mockChannelDlq,
  mockChannelSuppressions,
  mockConversationsPage,
  mockFinanceOverview,
  mockInventoryBalancesPage,
  mockInventoryLedgerPage,
  mockKbDocuments,
  mockMessagesPage,
  mockOnboardingState,
  mockOpportunitiesPage,
  mockOrderDetail,
  mockOrdersPage,
  mockProposals,
  mockQuotesPage,
  mockVariantDetail,
  mockVariantsPage,
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

export function fetchCatalogVariants(
  query: string,
): Promise<PaginatedResponse<CatalogVariantApiItem>> {
  if (shouldUseMockApi()) {
    return Promise.resolve(
      mockVariantsPage(query) as unknown as PaginatedResponse<CatalogVariantApiItem>,
    );
  }
  return getCatalogVariants(query, 50, 0);
}

export function fetchVariantDetail(
  variantId: string,
): Promise<{
  variant: CatalogVariantApiItem;
  mappings: PlatformSkuMappingApiItem[];
}> {
  if (shouldUseMockApi()) {
    return Promise.resolve(
      mockVariantDetail(variantId) as unknown as {
        variant: CatalogVariantApiItem;
        mappings: PlatformSkuMappingApiItem[];
      },
    );
  }
  return getVariantMappings(variantId);
}

export function fetchB2bOpportunities(): Promise<
  PaginatedResponse<B2bOpportunityApiItem>
> {
  if (shouldUseMockApi()) {
    return Promise.resolve(
      mockOpportunitiesPage() as unknown as PaginatedResponse<B2bOpportunityApiItem>,
    );
  }
  return getB2bOpportunities(50, 0);
}

export function fetchB2bQuotes(
  opportunityId = "",
): Promise<PaginatedResponse<B2bQuoteApiItem>> {
  if (shouldUseMockApi()) {
    return Promise.resolve(
      mockQuotesPage(opportunityId || undefined) as unknown as PaginatedResponse<B2bQuoteApiItem>,
    );
  }
  return getB2bQuotes(opportunityId, 20, 0);
}

export function fetchOrders(
  orderMode = "",
): Promise<PaginatedResponse<OrderSummaryApiItem>> {
  if (shouldUseMockApi()) {
    return Promise.resolve(
      mockOrdersPage(orderMode) as unknown as PaginatedResponse<OrderSummaryApiItem>,
    );
  }
  return getOrders(orderMode, 20, 0);
}

export function fetchOrderDetail(orderId: string): Promise<OrderDetailApiItem> {
  if (shouldUseMockApi()) {
    return Promise.resolve(mockOrderDetail as unknown as OrderDetailApiItem);
  }
  return getOrderDetail(orderId);
}

export function fetchInventoryBalances(): Promise<
  PaginatedResponse<InventoryBalanceApiItem>
> {
  if (shouldUseMockApi()) {
    return Promise.resolve(
      mockInventoryBalancesPage() as unknown as PaginatedResponse<InventoryBalanceApiItem>,
    );
  }
  return getInventoryBalances("", 20, 0);
}

export function fetchInventoryLedger(): Promise<
  PaginatedResponse<InventoryLedgerApiItem>
> {
  if (shouldUseMockApi()) {
    return Promise.resolve(
      mockInventoryLedgerPage() as unknown as PaginatedResponse<InventoryLedgerApiItem>,
    );
  }
  return getInventoryLedger("", 20, 0);
}

export function fetchFinanceOverview(): Promise<FinanceOverviewApiItem> {
  if (shouldUseMockApi()) {
    return Promise.resolve(
      mockFinanceOverview as unknown as FinanceOverviewApiItem,
    );
  }
  return getFinanceOverview();
}

export function fetchOnboardingState(): Promise<OnboardingStateApiItem> {
  if (shouldUseMockApi()) {
    return Promise.resolve(
      mockOnboardingState as unknown as OnboardingStateApiItem,
    );
  }
  return getOnboardingState();
}

export function fetchChannelAccounts(): Promise<
  PaginatedResponse<ChannelAccountApiItem>
> {
  if (shouldUseMockApi()) {
    return Promise.resolve(
      mockChannelAccounts as unknown as PaginatedResponse<ChannelAccountApiItem>,
    );
  }
  return getChannelAccounts();
}

export function fetchChannelDlq(): Promise<PaginatedResponse<DlqEventApiItem>> {
  if (shouldUseMockApi()) {
    return Promise.resolve(
      mockChannelDlq as unknown as PaginatedResponse<DlqEventApiItem>,
    );
  }
  return getChannelDlq();
}

export function fetchChannelConsents(): Promise<
  PaginatedResponse<ConsentLogApiItem>
> {
  if (shouldUseMockApi()) {
    return Promise.resolve(
      mockChannelConsents as unknown as PaginatedResponse<ConsentLogApiItem>,
    );
  }
  return getChannelConsents();
}

export function fetchChannelSuppressions(): Promise<
  PaginatedResponse<SuppressionLogApiItem>
> {
  if (shouldUseMockApi()) {
    return Promise.resolve(
      mockChannelSuppressions as unknown as PaginatedResponse<SuppressionLogApiItem>,
    );
  }
  return getChannelSuppressions();
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
