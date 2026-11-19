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
