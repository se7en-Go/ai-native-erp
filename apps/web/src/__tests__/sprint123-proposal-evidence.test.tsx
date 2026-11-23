import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { I18nProvider } from "@/lib/i18n";
import {
  ProposalReviewCard,
  extractToolEvidence,
  type AiProposalItem,
} from "@/components/ai/proposal-review-card";

function wrap(ui: React.ReactNode, locale: "zh" | "en" = "en") {
  return render(<I18nProvider defaultLocale={locale}>{ui}</I18nProvider>);
}

const GROUNDED_PROPOSAL: AiProposalItem = {
  id: "prop-201",
  capability: "sales_quoting",
  proposal_type: "quote_draft",
  conversation_id: "conv-1",
  proposed_payload: {
    sku: "LED-COB-24V-5M",
    quantity: 500,
    unit_price_usd: 14.2,
    incoterm: "FOB",
    source_tool_call_id: "sql-tool-sku-501",
    tool_evidence: [
      {
        tool_name: "get_live_sku_pricing_and_stock",
        sku: "LED-COB-24V-5M",
        available_qty: 1240,
        moq_tier_applied: 500,
        unit_price_usd: 14.2,
        lead_time_days: 7,
      },
      {
        tool_name: "get_fx_lock_snapshot",
        fx_rate_snapshot: "7.120000",
      },
    ],
  },
  citations: [{ doc_id: "doc-led", chunk_id: "c4", score: 0.89 }],
  confidence_score: 0.93,
  estimated_margin_pct: 24.5,
  min_margin_floor_pct: 15,
  source_tool_call_id: "sql-tool-sku-501",
  status: "pending",
  created_at: "2026-11-19T08:10:00Z",
};

const PLAIN_PROPOSAL: AiProposalItem = {
  ...GROUNDED_PROPOSAL,
  id: "prop-202",
  proposed_payload: { reply_text: "Thanks for your message." },
  source_tool_call_id: null,
  citations: [],
};

const renderCard = (proposal: AiProposalItem) =>
  wrap(
    <ProposalReviewCard
      proposal={proposal}
      canReview
      onApprove={vi.fn()}
      onEditApprove={vi.fn()}
      onReject={vi.fn()}
    />
  );

describe("Track 2 · Verified ERP evidence on the proposal review card", () => {
  it("flattens tool_evidence into readable label/value pairs", () => {
    const entries = extractToolEvidence({
      tool_evidence: [
        { sku: "LED-COB-24V-5M", available_qty: 1240, nested: { ignored: true } },
        "sql-sku-99",
      ],
    });
    expect(entries).toEqual([
      { label: "sku", value: "LED-COB-24V-5M" },
      { label: "available_qty", value: "1240" },
      { label: "evidence", value: "sql-sku-99" },
    ]);
  });

  it("shows the verified ERP evidence pill, tool call id and collapsible evidence preview", () => {
    renderCard(GROUNDED_PROPOSAL);

    const badge = screen.getByTestId("proposal-evidence-badge-prop-201");
    expect(badge.textContent).toContain("Verified ERP evidence");
    expect(screen.getByTestId("proposal-tool-call-prop-201").textContent).toBe(
      "sql-tool-sku-501"
    );

    const preview = screen.getByTestId("proposal-evidence-preview-prop-201");
    expect(preview.tagName.toLowerCase()).toBe("details");
    // Real SKU / stock / tier numbers the AI checked are visible to reviewers.
    expect(preview.textContent).toContain("get_live_sku_pricing_and_stock");
    expect(preview.textContent).toContain("LED-COB-24V-5M");
    expect(preview.textContent).toContain("1240");
    expect(preview.textContent).toContain("7.120000");
    expect(preview.querySelector("summary")?.textContent).toContain("(8)");
  });

  it("falls back to proposed_payload.source_tool_call_id when the top-level id is absent", () => {
    renderCard({ ...GROUNDED_PROPOSAL, id: "prop-203", source_tool_call_id: null });
    expect(screen.getByTestId("proposal-tool-call-prop-203").textContent).toBe(
      "sql-tool-sku-501"
    );
  });

  it("renders no evidence UI for a plain proposal without tool grounding", () => {
    renderCard(PLAIN_PROPOSAL);
    expect(screen.queryByTestId("proposal-evidence-prop-202")).toBeNull();
    expect(screen.queryByTestId("proposal-evidence-badge-prop-202")).toBeNull();
  });

  it("confirms dispatch with a banner once approved and queued for delivery", () => {
    renderCard({
      ...GROUNDED_PROPOSAL,
      id: "prop-204",
      status: "approved",
      execution_result: { queued_for_delivery: true, outbound_message_id: "msg-1" },
    });

    const banner = screen.getByTestId("proposal-delivery-banner-prop-204");
    expect(banner.textContent).toContain("Queued for delivery");
  });

  it("keeps the approve/reject controls for a pending proposal", () => {
    const onApprove = vi.fn();
    wrap(
      <ProposalReviewCard
        proposal={GROUNDED_PROPOSAL}
        canReview
        onApprove={onApprove}
        onEditApprove={vi.fn()}
        onReject={vi.fn()}
      />
    );

    fireEvent.click(screen.getByTestId("proposal-approve-prop-201"));
    expect(onApprove).toHaveBeenCalledWith("prop-201");
    expect(screen.getByTestId("proposal-reject-toggle-prop-201")).toBeTruthy();
  });

  it("renders readable reply preview, Chinese risk pills, collapsible JSON, and localized 批准 button", () => {
    const onEditApprove = vi.fn();
    const replyProposal: AiProposalItem = {
      id: "prop-tg-1",
      capability: "cs_support",
      proposal_type: "reply_draft",
      conversation_id: "conv-tg",
      proposed_payload: {
        body_text: "Hello seven,\n\nA specialist will review your request shortly.",
        risk_flags: ["order_confirmation", "below_floor_price"],
        channel_type: "telegram",
        customer_name: "seven",
        reasoning_summary: "Risk guardrail matched order_confirmation, below_floor_price.",
        kb_citations: [{ doc_id: "d1", chunk_id: "c1", doc_title: "COB 24V Warranty" }],
      },
      citations: [],
      confidence_score: 0.35,
      status: "pending",
      created_at: "2026-11-23T01:40:00Z",
    };

    wrap(
      <ProposalReviewCard
        proposal={replyProposal}
        canReview
        onApprove={vi.fn()}
        onEditApprove={onEditApprove}
        onReject={vi.fn()}
      />,
      "zh"
    );

    // Localized primary button label
    expect(screen.getByTestId("proposal-approve-prop-tg-1").textContent).toContain("批准");

    // Readable message body and risk flags instead of raw JSON dump
    const bodyPreview = screen.getByTestId("proposal-body-preview-prop-tg-1");
    expect(bodyPreview.textContent).toContain("A specialist will review your request shortly.");
    const riskBanner = screen.getByTestId("proposal-risk-flags-prop-tg-1");
    expect(riskBanner.textContent).toContain("要求确认下单");
    expect(riskBanner.textContent).toContain("低于底价");

    // Raw JSON is tucked inside a collapsible <details>
    const rawDetails = screen.getByTestId("proposal-raw-json-prop-tg-1");
    expect(rawDetails.tagName.toLowerCase()).toBe("details");
    expect(rawDetails.hasAttribute("open")).toBe(false);

    // Clicking 编辑后批准 opens plain-text body editor and preserves other payload fields on submit
    fireEvent.click(screen.getByTestId("proposal-edit-toggle-prop-tg-1"));
    const bodyEditor = screen.getByTestId("proposal-body-editor-prop-tg-1") as HTMLTextAreaElement;
    fireEvent.change(bodyEditor, {
      target: { value: "Hi seven, our 500 pcs FOB price is $12.00/pc." },
    });
    fireEvent.click(screen.getByTestId("proposal-edit-submit-prop-tg-1"));

    expect(onEditApprove).toHaveBeenCalledWith("prop-tg-1", {
      ...replyProposal.proposed_payload,
      body_text: "Hi seven, our 500 pcs FOB price is $12.00/pc.",
    });
  });
});
