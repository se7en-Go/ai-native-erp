import { describe, it, expect } from "vitest";
import {
  mockConversations,
  mockConversationsPage,
  mockMessagesPage,
  mockProposals,
  mockCapabilities,
  mockWallet,
  mockCustomerContext,
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
});
