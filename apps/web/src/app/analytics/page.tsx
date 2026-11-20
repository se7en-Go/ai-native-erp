"use client";

import React from "react";
import { useQuery } from "@tanstack/react-query";
import { ShieldAlert } from "lucide-react";

import { AppShell } from "@/components/app-shell";
import { useAuthSession } from "@/components/providers";
import { isPrivilegedCostViewer } from "@/lib/auth-cookie";
import { formatMoney } from "@/lib/contracts";
import { fetchPendingProposalsCount } from "@/lib/data-source";
import { fetchPhase3aOverview } from "@/lib/phase3a-data";
import { useI18n } from "@/lib/i18n";

export default function AnalyticsPage() {
  const { bi } = useI18n();
  const { principal } = useAuthSession();
  const showMargins = isPrivilegedCostViewer(principal?.role);

  const proposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () => fetchPendingProposalsCount(),
  });
  const overviewQuery = useQuery({
    queryKey: ["analytics", "overview"],
    queryFn: () => fetchPhase3aOverview(),
  });

  const overview = overviewQuery.data;
  const totalChannel = (overview?.channels ?? []).reduce((sum, row) => sum + row.conversations, 0);

  return (
    <AppShell currentPath="/analytics" pendingProposalsCount={proposalsQuery.data?.total ?? 0}>
      <div className="erp-page space-y-4">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-slate-900">{bi("统计分析", "Analytics")}</h1>
          <p className="text-xs text-slate-500">
            {bi("渠道分布、B2B 漏斗转化与 SKU 毛利分析；成本与毛利对业务员角色自动脱敏。", "Channel mix, B2B funnel conversion and SKU margins; costs and margins are masked for sales agents.")}
          </p>
        </div>

        {overviewQuery.isError && (
          <p role="alert" className="rounded border border-rose-200 bg-rose-50 p-2 text-xs text-rose-800">
            {overviewQuery.error.message}
          </p>
        )}

        <div className="grid gap-4 lg:grid-cols-2">
          <section className="erp-card p-4">
            <h2 className="text-sm font-semibold text-slate-900">{bi("渠道分布", "Channel mix")}</h2>
            <ul data-testid="analytics-channels" className="mt-2 space-y-1 text-xs text-slate-600">
              {(overview?.channels ?? []).map((row) => (
                <li key={row.channel}>
                  <div className="flex justify-between">
                    <span>{row.channel}</span>
                    <span className="font-medium text-slate-800">
                      {row.conversations} (
                      {totalChannel ? Math.round((row.conversations / totalChannel) * 100) : 0}%)
                    </span>
                  </div>
                  <div className="mt-0.5 h-1 rounded bg-slate-100">
                    <div
                      className="h-1 rounded bg-blue-500"
                      style={{
                        width: `${totalChannel ? (row.conversations / totalChannel) * 100 : 0}%`,
                      }}
                    />
                  </div>
                </li>
              ))}
            </ul>
          </section>

          <section className="erp-card p-4">
            <h2 className="text-sm font-semibold text-slate-900">{bi("B2B 漏斗转化", "B2B funnel conversion")}</h2>
            <p data-testid="analytics-conversion" className="mt-1 text-xs text-slate-600">
              {bi("赢单转化率", "Win rate")}：<span className="font-semibold tabular-nums text-slate-900">
                {overview?.won_conversion_pct ?? "—"}%
              </span>
            </p>
            <ul data-testid="analytics-funnel" className="mt-2 space-y-1 text-xs text-slate-600">
              {(overview?.funnel ?? []).map((stage) => (
                <li key={stage.stage} className="flex justify-between">
                  <span>{stage.stage}</span>
                  <span className="font-medium text-slate-800">{stage.count}</span>
                </li>
              ))}
            </ul>
          </section>
        </div>

        <section className="erp-card p-4">
          <h2 className="text-sm font-semibold text-slate-900">{bi("SKU 毛利分析", "SKU margin analysis")}</h2>
          {showMargins ? (
            <div className="mt-2 overflow-x-auto">
              <table data-testid="analytics-sku-margins" className="erp-table">
                <thead className="text-[10px] uppercase text-slate-400">
                  <tr>
                    <th>SKU</th>
                    <th className="text-right">{bi("件数", "Units")}</th>
                    <th className="text-right">{bi("收入", "Revenue")}</th>
                    <th className="text-right">{bi("成本", "Cost")}</th>
                    <th className="text-right">{bi("毛利", "Margin")}</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 text-slate-700">
                  {(overview?.sku_margins ?? []).map((row) => (
                    <tr key={`${row.variant_id}-${row.currency}`}>
                      <td className="py-1.5 font-medium">{row.sku}</td>
                      <td className="py-1.5">{row.units}</td>
                      <td className="py-1.5">{formatMoney(row.revenue, row.currency)}</td>
                      <td className="py-1.5">{formatMoney(row.cost, row.currency)}</td>
                      <td className="py-1.5 font-semibold text-emerald-700">
                        {formatMoney(row.gross_margin, row.currency)}
                      </td>
                    </tr>
                  ))}
                  {(overview?.sku_margins ?? []).length === 0 && (
                    <tr>
                      <td colSpan={5} className="py-4 text-center text-slate-400">
                        {bi("暂无毛利数据", "No margin data")}
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          ) : (
            <p
              data-testid="analytics-margins-masked"
              className="mt-2 flex items-center gap-1.5 rounded border border-amber-200 bg-amber-50 p-2 text-xs text-amber-900"
            >
              <ShieldAlert className="h-3.5 w-3.5" />
              {bi(`当前角色（${principal?.role}）不可查看成本与毛利明细。`, `Your role (${principal?.role}) cannot view cost and margin detail.`)}
            </p>
          )}
        </section>
      </div>
    </AppShell>
  );
}
