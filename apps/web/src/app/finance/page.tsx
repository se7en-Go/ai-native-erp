"use client";

import React from "react";
import { useQuery } from "@tanstack/react-query";
import { Download, Landmark, Percent, TrendingUp } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { useAuthSession } from "@/components/providers";
import { PaymentMilestoneTable } from "@/components/finance/payment-milestone-table";
import { Card, Page, PageHeader, StatCard } from "@/components/ui/page";
import { fetchFinanceOverview, fetchPendingProposalsCount } from "@/lib/data-source";
import { useI18n } from "@/lib/i18n";
import { formatMoney } from "@/lib/contracts";

export default function FinancePage() {
  const { principal } = useAuthSession();
  const { bi } = useI18n();

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
      <Page>
        <PageHeader
          title={bi("财务收款与 CVP 边际毛利", "Finance settlements & CVP contribution margin")}
          description={bi(
            "B2B 定金 / 尾款里程碑、L0 水单人工复核，以及按订单拆解的净贡献毛利瀑布。金额与汇率全部使用 Decimal 与 NUMERIC，不使用浮点数。",
            "B2B deposit and balance milestones, L0 manual wire verification, and per-order net contribution margin waterfalls. Money and FX always use Decimal/NUMERIC, never floats."
          )}
          actions={
            <a
              href="/api/bff/v1/finance/cvp/export.csv"
              className="erp-btn erp-btn-secondary"
            >
              <Download className="h-3.5 w-3.5" />
              <span>{bi("导出对账 CSV", "Export reconciliation CSV")}</span>
            </a>
          }
        />

        {summary && (
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <StatCard
              label={bi("总营收（本位币）", "Total gross revenue")}
              value={formatMoney(summary.total_gross_revenue_base, summary.base_currency)}
              hint={bi("含 B2B 与 B2C 全部已确认订单", "All confirmed B2B and B2C orders")}
              icon={Landmark}
            />
            <StatCard
              label={bi("净贡献毛利", "Net contribution margin")}
              value={formatMoney(summary.total_net_margin_base, summary.base_currency)}
              hint={bi("已扣 COGS、佣金、物流与广告归因", "After COGS, fees, freight and ad attribution")}
              tone="success"
              icon={TrendingUp}
            />
            <StatCard
              label={bi("平均毛利率", "Average margin")}
              value={`${summary.avg_margin_pct}%`}
              hint={bi("按订单加权，保留两位小数", "Order-weighted, two decimals")}
              tone="info"
              icon={Percent}
            />
            <StatCard
              label={bi("待核销里程碑", "Milestones pending verification")}
              value={
                financeQuery.data?.receivables.filter((row) => row.pending_payment_id).length ?? 0
              }
              hint={bi("需财务或所有者执行 L0 复核", "Requires Finance/Owner L0 review")}
              tone="warning"
            />
          </div>
        )}

        {financeQuery.isError && (
          <Card className="border-rose-200 bg-rose-50 p-3 text-[12px] text-rose-800">
            {financeQuery.error.message}
          </Card>
        )}

        <PaymentMilestoneTable
          receivables={financeQuery.data?.receivables ?? []}
          cvpSnapshots={financeQuery.data?.cvp_snapshots ?? []}
          role={principal?.role}
        />
      </Page>
    </AppShell>
  );
}
