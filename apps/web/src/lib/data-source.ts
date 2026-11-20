import {
  getB2bOpportunities,
  getB2bQuotes,
  getCatalogVariants,
  getFinanceOverview,
  getInventoryBalances,
  getInventoryLedger,
  getOnboardingState,
  getOrderDetail,
  getOrders,
  getVariantMappings,
  getConversations,
  getConversationMessages,
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
} from "@/lib/api-client/live-client";
import {
  fetchCapabilities as liveFetchCapabilities,
  fetchKbDocuments as liveFetchKbDocuments,
  connectEmailChannel as liveConnectEmailChannel,
  connectInstagramChannel as liveConnectInstagramChannel,
  connectMessengerChannel as liveConnectMessengerChannel,
  connectTelegramChannel as liveConnectTelegramChannel,
  disconnectChannelAccount as liveDisconnectChannelAccount,
  fetchChannelAccounts as liveFetchChannelAccounts,
  fetchChannelConsents as liveFetchChannelConsents,
  fetchChannelDlq as liveFetchChannelDlq,
  fetchChannelSuppressions as liveFetchChannelSuppressions,
  fetchPendingProposalsCount as liveFetchPendingProposalsCount,
  fetchTeamMembers as liveFetchTeamMembers,
  fetchWallet as liveFetchWallet,
  inviteTeamMember as liveInviteTeamMember,
  listProposals,
  updateChannelOwnership as liveUpdateChannelOwnership,
  type BillingWalletResponse,
  type CapabilityConfigItem,
  type KbDocumentItem,
} from "@/lib/api-client/live-client";

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
  mockTeamMembers,
  mockWallet,
} from "@/lib/mock-fixtures";
import type {
  DisconnectChannelResponse,
  EmailConnectPayload,
  InstagramConnectPayload,
  InviteTeamMemberPayload,
  MessengerConnectPayload,
  MessagingConnectPayload,
  MessagingConnectResult,
  MessagingProviderKind,
  TeamMemberApiItem,
  TelegramConnectPayload,
  UpdateChannelOwnershipPayload,
} from "@/lib/api-client/live-client";
import type { AiProposalItem } from "@/components/ai/proposal-review-card";
import type { ConversationSummary } from "@/components/inbox/conversation-list";
import type { InboxMessage } from "@/components/inbox/message-thread";

export type { CapabilityConfigItem, KbDocumentItem };

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
  return getConversations(channel, filter, query);
}

export function fetchConversationMessages(
  conversationId: string,
): Promise<PaginatedResponse<InboxMessage>> {
  if (shouldUseMockApi()) {
    return Promise.resolve(mockMessagesPage(conversationId));
  }
  return getConversationMessages(conversationId);
}

export function fetchProposals(
  status: ProposalStatus,
  typeFilter: "" | ProposalType,
): Promise<PaginatedResponse<AiProposalItem>> {
  if (shouldUseMockApi()) {
    const items = mockProposals.filter(
      (p) => p.status === status && (!typeFilter || p.proposal_type === typeFilter),
    );
    return Promise.resolve({
      items,
      total: items.length,
      limit: 20,
      offset: 0,
      has_more: false,
    });
  }
  return listProposals(status, typeFilter);
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
  return liveFetchCapabilities();
}

export function fetchWallet(): Promise<BillingWalletResponse> {
  if (shouldUseMockApi()) {
    return Promise.resolve(mockWallet);
  }
  return liveFetchWallet();
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
  return liveFetchKbDocuments();
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
  return liveFetchChannelAccounts();
}

export async function connectMessagingChannel(
  kind: MessagingProviderKind,
  payload: MessagingConnectPayload,
): Promise<MessagingConnectResult> {
  if (shouldUseMockApi()) {
    const external =
      "page_id" in payload
        ? payload.page_id
        : "ig_account_id" in payload
          ? payload.ig_account_id
          : payload.bot_token.split(":")[0];
    return {
      channel_account_id: `mock-${kind}-account`,
      external_account_id: external,
    };
  }
  if (kind === "messenger") {
    return liveConnectMessengerChannel(payload as MessengerConnectPayload);
  }
  if (kind === "instagram_dm") {
    return liveConnectInstagramChannel(payload as InstagramConnectPayload);
  }
  return liveConnectTelegramChannel(payload as TelegramConnectPayload);
}

// ---------------------------------------------------------------------------
// 归属与团队（Plan 0009）：全部为 bffFetch 包装，mock 模式下保留纯本地降级，
// 以便没有后端时也能完整演示“快捷绑定 + 业务员归属”流程。
// ---------------------------------------------------------------------------

/** 邮箱快捷绑定：Resend API Key 或 SMTP/IMAP 邮箱。 */
export async function connectEmailChannel(
  payload: EmailConnectPayload,
): Promise<MessagingConnectResult> {
  if (shouldUseMockApi()) {
    return {
      channel_account_id: `mock-email-${payload.email_address}`,
      external_account_id: payload.email_address,
      display_name: payload.display_name ?? null,
      visibility_scope: payload.visibility_scope,
      owner_user_id: payload.owner_user_id ?? null,
    };
  }
  return liveConnectEmailChannel(payload);
}

/** 切换归属（公司公海 ↔ 指定业务员个人专属）。 */
export async function updateChannelOwnership(
  accountId: string,
  payload: UpdateChannelOwnershipPayload,
): Promise<ChannelAccountApiItem> {
  if (shouldUseMockApi()) {
    const existing = mockChannelAccounts.items.find((item) => item.id === accountId);
    return {
      id: accountId,
      channel_type:
        (existing?.channel_type as ChannelAccountApiItem["channel_type"]) ?? "telegram",
      display_name: existing?.display_name ?? accountId,
      external_account_id: existing?.external_account_id ?? accountId,
      status: "active",
      rate_limit_profile: existing?.rate_limit_profile ?? {},
      owner_user_id: payload.owner_user_id ?? null,
      owner_name: null,
      owner_email: null,
      visibility_scope: payload.visibility_scope,
      bot_username: existing?.bot_username ?? null,
    };
  }
  return liveUpdateChannelOwnership(accountId, payload);
}

/** 断开渠道账号。 */
export function disconnectChannelAccount(
  accountId: string,
): Promise<DisconnectChannelResponse> {
  if (shouldUseMockApi()) {
    return Promise.resolve({ channel_account_id: accountId, status: "disconnected" });
  }
  return liveDisconnectChannelAccount(accountId);
}

/** 团队成员列表（owner/admin）。 */
export function fetchTeamMembers(): Promise<PaginatedResponse<TeamMemberApiItem>> {
  if (shouldUseMockApi()) {
    return Promise.resolve(
      mockTeamMembers as unknown as PaginatedResponse<TeamMemberApiItem>,
    );
  }
  return liveFetchTeamMembers();
}

/** 新建业务员 / 团队成员（owner/admin）；mock 模式下追加到内存名单。 */
export async function inviteTeamMember(
  payload: InviteTeamMemberPayload,
): Promise<TeamMemberApiItem> {
  if (shouldUseMockApi()) {
    const member: TeamMemberApiItem = {
      membership_id: `mock-membership-${payload.email}`,
      tenant_id: "mock-tenant",
      user_id: `mock-user-${payload.email}`,
      email: payload.email,
      full_name: payload.full_name,
      role: payload.role,
      is_active: true,
      created_at: new Date().toISOString(),
    };
    mockTeamMembers.items.push(member);
    mockTeamMembers.total = mockTeamMembers.items.length;
    return member;
  }
  return liveInviteTeamMember(payload);
}

export function fetchChannelDlq(): Promise<PaginatedResponse<DlqEventApiItem>> {
  if (shouldUseMockApi()) {
    return Promise.resolve(
      mockChannelDlq as unknown as PaginatedResponse<DlqEventApiItem>,
    );
  }
  return liveFetchChannelDlq();
}

export function fetchChannelConsents(): Promise<
  PaginatedResponse<ConsentLogApiItem>
> {
  if (shouldUseMockApi()) {
    return Promise.resolve(
      mockChannelConsents as unknown as PaginatedResponse<ConsentLogApiItem>,
    );
  }
  return liveFetchChannelConsents();
}

export function fetchChannelSuppressions(): Promise<
  PaginatedResponse<SuppressionLogApiItem>
> {
  if (shouldUseMockApi()) {
    return Promise.resolve(
      mockChannelSuppressions as unknown as PaginatedResponse<SuppressionLogApiItem>,
    );
  }
  return liveFetchChannelSuppressions();
}

export function fetchPendingProposalsCount(): Promise<{ total: number }> {
  if (shouldUseMockApi()) {
    return Promise.resolve({
      total: mockProposals.filter((p) => p.status === "pending").length,
    });
  }
  return liveFetchPendingProposalsCount();
}
