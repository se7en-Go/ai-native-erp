import { mockWallet } from "@/lib/mock-fixtures/ai";

// Wallet / finance figures shown in the workbench when the backend is not
// reachable. Agent roles never see these: AppShell hides /finance and the
// finance page itself is guarded by canAccessRoute (see Task 5.1).
export const mockFinanceSummary = {
  wallet: mockWallet,
  open_receivables_usd: "14500.50",
  open_payables_usd: "8200.00",
  monthly_profit_target_cny: 100000,
  currency: "USD",
};

// Finance overview (Task 5.6): 30% deposit verified, 70% balance open with
// shipment blocked, and one CVP snapshot with the 8-item margin waterfall.
export const mockFinanceOverview = {
  summary: {
    total_gross_revenue_base: "52332.00",
    total_net_margin_base: "16280.00",
    avg_margin_pct: "31.11",
    base_currency: "CNY",
  },
  receivables: [
    {
      id: "rp-30",
      order_id: "ord-b2b-1",
      order_no: "ORD-B2B-2026-01",
      order_mode: "b2b_wholesale",
      milestone_label: "b2b_30_deposit",
      due_amount: "2205.00",
      paid_amount: "2205.00",
      currency: "USD",
      status: "partial",
      shipment_blocked_70_unpaid: true,
      pending_payment_id: "pay-30",
      pending_tx_ref: "TT-20261119-DE",
    },
    {
      id: "rp-70",
      order_id: "ord-b2b-1",
      order_no: "ORD-B2B-2026-01",
      order_mode: "b2b_wholesale",
      milestone_label: "b2b_70_balance",
      due_amount: "5145.00",
      paid_amount: "0.00",
      currency: "USD",
      status: "open",
      shipment_blocked_70_unpaid: true,
      pending_payment_id: null,
      pending_tx_ref: null,
    },
  ],
  cvp_snapshots: [
    {
      order_id: "ord-b2b-1",
      order_no: "ORD-B2B-2026-01",
      base_currency: "CNY",
      gross_revenue_base: "52332.00",
      cogs_base: "22750.00",
      platform_commission_base: "0.00",
      payment_gateway_fee_base: "250.00",
      shipping_and_duty_base: "12800.00",
      channel_msg_cost_base: "12.00",
      ad_attribution_cost_base: "360.00",
      fx_gain_loss_base: "120.00",
      net_contribution_margin_base: "16280.00",
    },
  ],
};
