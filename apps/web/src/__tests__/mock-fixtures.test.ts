import { describe, it, expect } from "vitest";
import {
  mockConversations,
  mockConversationsPage,
  mockMessagesPage,
  mockProposals,
  mockCapabilities,
  mockWallet,
  mockCustomerContext,
  mockChannelAccounts,
  mockChannelSuppressions,
  mockFinanceOverview,
  mockInventoryLedgerPage,
  mockOnboardingState,
  mockOpportunitiesPage,
  mockOrdersPage,
  mockQuotesPage,
  mockVariantDetail,
  mockVariantsPage,
} from "@/lib/mock-fixtures";
import {
  ChannelTypeSchema,
  ConversationStatusSchema,
  ProposalTypeSchema,
  formatMoney,
} from "@/lib/contracts";

describe("mock-fixtures (no-backend isolation layer)", () => {
  it("conversations use canonical channel/status enums and filter correctly", () => {
    expect(mockConversations.length).toBeGreaterThan(0);
    for (const c of mockConversations) {
      expect(ChannelTypeSchema.safeParse(c.channel_type).success).toBe(true);
      expect(ConversationStatusSchema.safeParse(c.status).success).toBe(true);
    }
    expect(mockConversationsPage("all", "unread").items.length).toBe(2);
    expect(mockConversationsPage("all", "pending_human").items).toHaveLength(1);
    expect(mockConversationsPage("all", "all", "hans").items).toHaveLength(1);
    expect(mockMessagesPage("conv-1").items).toHaveLength(2);
    expect(mockMessagesPage("conv-unknown").items).toHaveLength(0);
  });

  it("proposals and capabilities use canonical enums and renderable money", () => {
    expect(mockProposals).toHaveLength(1);
    expect(ProposalTypeSchema.safeParse(mockProposals[0].proposal_type).success).toBe(true);
    expect(mockProposals[0].estimated_margin_pct).toBe(24.5);
    expect(formatMoney(14500.5, "USD")).toBe("$14,500.50");
    expect(mockCapabilities.map((c) => c.capability)).toContain("finance_recon");
    expect(mockCapabilities.find((c) => c.capability === "finance_recon")?.max_allowed_autonomy).toBe(
      "L0_copilot",
    );
    expect(mockWallet.included_credits_remaining).toBe(4200);
  });

  it("customer context carries a deterministic bsuid identity", () => {
    expect(
      mockCustomerContext.identities.some(
        (i) => i.identity_type === "whatsapp_bsuid" && i.identity_value === "bsuid_de_998122",
      ),
    ).toBe(true);
    expect(mockCustomerContext.recent_orders[0].order_no).toBe("ORD-2026-001");
    expect(mockCustomerContext.recent_quotes[0].quote_no).toBe("PI-202611-0042");
  });

  it("catalog and B2B mocks mirror the 5.5 contract shapes", () => {
    expect(mockVariantsPage("LED-COB").items).toHaveLength(1);
    expect(mockVariantsPage("").total).toBe(2);
    const detail = mockVariantDetail("var-1");
    expect(detail.variant.sku).toBe("LED-COB-24V-5M");
    expect(detail.mappings[0].bundle_multiplier).toBe(2);
    expect(mockOpportunitiesPage().items.map((o) => o.stage)).toContain("quoting");
    const quote = mockQuotesPage().items[0];
    expect(quote.quote_no).toBe("PI-202611-0042");
    expect(new Date(quote.fx_locked_until).getTime()).toBeGreaterThan(Date.now());
  });

  it("orders and finance mocks mirror the 5.6 contract shapes", () => {
    expect(mockOrdersPage("b2b_wholesale").items).toHaveLength(1);
    expect(mockOrdersPage("").total).toBe(2);
    expect(mockOrdersPage().items[0].has_72h_stagnation_alert).toBe(true);
    expect(mockInventoryLedgerPage().items[0].idempotency_key).toBe(
      "reserve:ord-b2b-1:var-1",
    );
    expect(mockFinanceOverview.receivables[1].shipment_blocked_70_unpaid).toBe(true);
    expect(mockFinanceOverview.cvp_snapshots[0].net_contribution_margin_base).toBe(
      "16280.00",
    );
  });

  it("onboarding and channel mocks mirror the 5.7 contract shapes", () => {
    expect(mockOnboardingState.preset_type).toBe("hybrid_factory_b2b_b2c");
    expect(mockOnboardingState.site_key).toBe("wc_site_qz_001");
    expect(mockChannelAccounts.items[0].rate_limit_profile.mps).toBe(20);
    expect(mockChannelSuppressions.items[0].normalized_address).toBe(
      "unsubscribe-test@example.com",
    );
  });
});
