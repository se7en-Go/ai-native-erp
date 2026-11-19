"use client";

import React from "react";
import { useQuery } from "@tanstack/react-query";
import { Download } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { useAuthSession } from "@/components/providers";
import { PaymentMilestoneTable } from "@/components/finance/payment-milestone-table";
import {
  fetchFinanceOverview,
  fetchPendingProposalsCount,
} from "@/lib/data-source";
import { formatMoney } from "@/lib/contracts";

export default function FinancePage() {
  const { principal } = useAuthSession();

  const pendingProposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () => fetchPendingProposalsCount(),
  });

  const financeQuery = useQuery({
    queryKey: ["finance", "overview"],
    enabled: Boolean(principal && principal.role !== "agent"),
    queryFn: () => fetchFinanceOverview(),
  });

  const summary = financeQuery.data?.summary;

  return (
    <AppShell
      currentPath="/finance"
      pendingProposalsCount={pendingProposalsQuery.data?.total ?? 0}
    >
      <div className="mx-auto max-w-6xl space-y-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="text-lg font-bold text-slate-900">
              Finance Milestones & Real-Time CVP Contribution Margin
            </h1>
            <p className="text-xs text-slate-500">
              Strict L0 verification by Finance/Owner for T/T wires and exact Decimal CVP ledger snapshots.
            </p>
          </div>
          <a
            href="/api/bff/v1/finance/cvp/export.csv"
            className="inline-flex items-center gap-1.5 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 hover:bg-slate-100"
          >
            <Download className="h-3.5 w-3.5" />
            <span>Export QuickBooks/Xero CSV</span>
          </a>
        </div>

        {summary && (
          <div className="grid gap-3 md:grid-cols-3">
            <div className="rounded-lg border border-slate-200 bg-white p-4">
              <div className="text-xs text-slate-500">Total Gross Revenue</div>
              <div className="mt-1 text-xl font-bold text-slate-900">
                {formatMoney(summary.total_gross_revenue_base, summary.base_currency)}
              </div>
            </div>
            <div className="rounded-lg border border-slate-200 bg-white p-4">
              <div className="text-xs text-slate-500">Total Net Contribution Margin</div>
              <div className="mt-1 text-xl font-bold text-emerald-700">
                {formatMoney(summary.total_net_margin_base, summary.base_currency)}
              </div>
            </div>
            <div className="rounded-lg border border-slate-200 bg-white p-4">
              <div className="text-xs text-slate-500">Average Margin %</div>
              <div className="mt-1 text-xl font-bold text-indigo-700">
                {summary.avg_margin_pct}%
              </div>
            </div>
          </div>
        )}

        <PaymentMilestoneTable
          receivables={financeQuery.data?.receivables ?? []}
          cvpSnapshots={financeQuery.data?.cvp_snapshots ?? []}
          role={principal?.role}
        />
      </div>
    </AppShell>
  );
}
