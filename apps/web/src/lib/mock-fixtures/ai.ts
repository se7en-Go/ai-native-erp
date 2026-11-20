import type { AiCapability, ProposalStatus, ProposalType } from "@/lib/contracts";

// Structural mirror of AiProposalItem from components/ai/proposal-review-card
// (defined here so fixtures stay usable before Task 5.4 lands).
export interface AiProposalItem {
  id: string;
  capability: AiCapability;
  proposal_type: ProposalType;
  conversation_id?: string | null;
  proposed_payload: Record<string, unknown>;
  citations: Array<{ doc_id: string; chunk_id: string; score: number }>;
  confidence_score: number;
  estimated_margin_pct?: number | null;
  min_margin_floor_pct?: number | null;
  source_tool_call_id?: string | null;
  status: ProposalStatus;
  created_at: string;
}

export interface MockWallet {
  plan_tier: string;
  included_credits_remaining: number;
  topup_credits_balance: number;
  overage_spent_cents?: number;
  overage_spend_cap_cents?: number;
  auto_downgrade_to_l0: boolean;
}

export interface MockCapability {
  capability: string;
  configured_autonomy: string;
  max_allowed_autonomy: string;
  pending_proposals_7d: number;
  executed_actions_7d: number;
  metrics: Array<{
    label_key: string;
    kind: "count" | "money" | "percent";
    value: number;
    currency?: string;
  }>;
}

export const mockProposals: AiProposalItem[] = [
  {
    id: "prop-101",
    capability: "sales_quoting",
    proposal_type: "quote_draft",
    conversation_id: "conv-1",
    proposed_payload: {
      sku: "LED-COB-24V-5M",
      quantity: 500,
      unit_price_usd: 14.2,
      incoterm: "FOB",
    },
    citations: [{ doc_id: "doc-led", chunk_id: "c4", score: 0.89 }],
    confidence_score: 0.92,
    estimated_margin_pct: 24.5,
    min_margin_floor_pct: 15.0,
    status: "pending",
    created_at: "2026-11-19T08:10:00Z",
  },
];

export const mockWallet: MockWallet = {
  plan_tier: "Growth",
  included_credits_remaining: 4200,
  topup_credits_balance: 0,
  auto_downgrade_to_l0: false,
};

export const mockWalletEmpty: MockWallet = {
  plan_tier: "starter",
  included_credits_remaining: 0,
  topup_credits_balance: 0,
  overage_spent_cents: 5000,
  overage_spend_cap_cents: 5000,
  auto_downgrade_to_l0: true,
};

export const mockCapabilities: MockCapability[] = [
  {
    capability: "cs_support",
    configured_autonomy: "L2_auto",
    max_allowed_autonomy: "L2_auto",
    pending_proposals_7d: 1,
    executed_actions_7d: 12,
    metrics: [{ label_key: "open_threads", kind: "count", value: 12 }],
  },
  {
    capability: "sales_quoting",
    configured_autonomy: "L1_proposal",
    max_allowed_autonomy: "L1_proposal",
    pending_proposals_7d: 3,
    executed_actions_7d: 5,
    metrics: [{ label_key: "pending_quotes", kind: "count", value: 3 }],
  },
  {
    capability: "order_logistics",
    configured_autonomy: "L2_auto",
    max_allowed_autonomy: "L2_auto",
    pending_proposals_7d: 1,
    executed_actions_7d: 8,
    metrics: [{ label_key: "stagnant_shipments_72h", kind: "count", value: 1 }],
  },
  {
    capability: "content_studio",
    configured_autonomy: "L1_proposal",
    max_allowed_autonomy: "L1_proposal",
    pending_proposals_7d: 1,
    executed_actions_7d: 4,
    metrics: [{ label_key: "drafts_ready", kind: "count", value: 4 }],
  },
  {
    capability: "biz_analytics",
    configured_autonomy: "L2_auto",
    max_allowed_autonomy: "L2_auto",
    pending_proposals_7d: 0,
    executed_actions_7d: 2,
    metrics: [{ label_key: "net_margin_pct", kind: "percent", value: 28.4 }],
  },
  {
    capability: "finance_recon",
    configured_autonomy: "L0_copilot",
    max_allowed_autonomy: "L0_copilot",
    pending_proposals_7d: 0,
    executed_actions_7d: 0,
    metrics: [
      { label_key: "open_receivables_amount", kind: "money", value: 14500.5, currency: "USD" },
    ],
  },
];

export const mockKbDocuments = [
  {
    id: "kb-1",
    title: "LED_Warranty_Policy.md",
    chunk_count: 14,
    status: "indexed",
    created_at: "2026-11-19T00:00:00Z",
  },
];

export const mockCopilotChatReply = {
  reply_text: "COB LED 24V carries a 3-year warranty [doc-warranty#c1].",
  citations: [{ doc_id: "doc-warranty", chunk_id: "c1", score: 0.94 }],
  tool_calls: [
    { tool_call_id: "tool-call-77", tool_name: "get_live_sku_pricing_and_stock" },
  ],
  should_abstain: false,
};

/**
 * `POST /ai/conversations/{id}/propose-reply` 的演示回执：带确定性 SQL 工具
 * 证据（库存 / 阶梯价 / 交期），让审核卡片能展示「已核验 ERP 证据」。
 */
export function mockGroundedProposal(conversationId: string): AiProposalItem {
  return {
    id: `prop-grounded-${conversationId}`,
    capability: "sales_quoting",
    proposal_type: "quote_draft",
    conversation_id: conversationId,
    proposed_payload: {
      sku: "LED-COB-24V-5M",
      quantity: 500,
      unit_price_usd: 14.2,
      incoterm: "FOB",
      currency: "USD",
      reply_text:
        "Thanks for your inquiry — 500 pcs LED-COB-24V-5M is in stock at US$14.20/pc FOB Shenzhen, lead time 7 days.",
      source_tool_call_id: "sql-tool-demo-sku-1",
      tool_evidence: [
        {
          tool_name: "get_live_sku_pricing_and_stock",
          sku: "LED-COB-24V-5M",
          available_qty: 1240,
          moq_tier_applied: 500,
          unit_price_usd: 14.2,
          incoterm: "FOB",
          lead_time_days: 7,
        },
      ],
    },
    citations: [{ doc_id: "doc-led", chunk_id: "c4", score: 0.89 }],
    confidence_score: 0.93,
    estimated_margin_pct: 24.5,
    min_margin_floor_pct: 15.0,
    source_tool_call_id: "sql-tool-demo-sku-1",
    status: "pending",
    created_at: "2026-11-19T08:10:00Z",
  };
}
