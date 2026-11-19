"use client";

import React from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, ArrowRight, ClipboardList, Inbox, Sparkles } from "lucide-react";

import { AppShell } from "@/components/app-shell";
import { useAuthSession } from "@/components/providers";
import { isPrivilegedCostViewer } from "@/lib/auth-cookie";
import { formatMoney } from "@/lib/contracts";
import { fetchPendingProposalsCount } from "@/lib/data-source";
import { fetchPhase3aDashboard } from "@/lib/phase3a-data";

export default function DashboardPage() {
  const { principal } = useAuthSession();
  const showFinancials = isPrivilegedCostViewer(principal?.role);

  const dashboardQuery = useQuery({
    queryKey: ["analytics", "dashboard"],
    queryFn: () => fetchPhase3aDashboard(),
  });
  const proposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () => fetchPendingProposalsCount(),
  });

  const data = dashboardQuery.data;
  const pipelineTotal = (data?.pipeline_value ?? []).reduce(
    (sum, entry) => sum + Number(entry.amount),
    0
  );
  const pipelineCurrency = data?.pipeline_value?.[0]?.currency ?? "USD";

  return (
    <AppShell
      currentPath="/dashboard"
      pendingProposalsCount={proposalsQuery.data?.total ?? 0}
    >
      <div className="mx-auto max-w-6xl space-y-4">
        <div>
          <h1 className="text-lg font-bold text-slate-900">经营总览 / Executive Dashboard</h1>
          <p className="text-xs text-slate-500">
            租户隔离的实时指标：询盘、B2B 管道、待审提案、库存预警与渠道分布。
          </p>
        </div>

        {dashboardQuery.isError && (
          <p role="alert" className="rounded border border-rose-200 bg-rose-50 p-2 text-xs text-rose-800">
            {dashboardQuery.error.message}
          </p>
        )}

        <div data-testid="dashboard-kpi-grid" className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <KpiCard label="待处理询盘" value={String(data?.open_inquiries ?? "—")} icon={Inbox} />
          <KpiCard
            label="B2B 管道金额"
            value={formatMoney(pipelineTotal, pipelineCurrency)}
            icon={ArrowRight}
          />
          <KpiCard
            label="待审 AI 提案"
            value={String(data?.pending_proposals ?? "—")}
            icon={Sparkles}
          />
          <KpiCard
            label="待办任务"
            value={String(data?.open_tasks ?? "—")}
            icon={ClipboardList}
          />
        </div>

        {showFinancials ? (
          <div
            data-testid="dashboard-financials"
            className="grid gap-3 rounded-lg border border-slate-200 bg-white p-4 sm:grid-cols-3"
          >
            <div>
              <p className="text-[10px] uppercase text-slate-400">B2C 订单</p>
              <p className="text-lg font-semibold text-slate-900">{data?.b2c_orders ?? "—"}</p>
            </div>
            {(data?.financials ?? []).map((entry) => (
              <div key={entry.currency}>
                <p className="text-[10px] uppercase text-slate-400">
                  COGS / 净贡献毛利 ({entry.currency})
                </p>
                <p className="text-sm font-semibold text-slate-900">
                  {formatMoney(entry.cost, entry.currency)} →{" "}
                  <span className="text-emerald-700">
                    {formatMoney(entry.net_contribution_margin, entry.currency)}
                  </span>
                </p>
              </div>
            ))}
          </div>
        ) : (
          <div
            data-testid="dashboard-financials-masked"
            className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-900"
          >
            <AlertTriangle className="mr-1 inline h-3.5 w-3.5" />
            当前角色（{principal?.role}）不可查看成本与毛利，KPI 已自动脱敏。
          </div>
        )}

        <div className="grid gap-3 lg:grid-cols-3">
          <section className="rounded-lg border border-slate-200 bg-white p-4">
            <h2 className="text-sm font-semibold text-slate-900">渠道概览</h2>
            <ul data-testid="dashboard-channels" className="mt-2 space-y-1 text-xs text-slate-600">
              {(data?.channels ?? []).map((channel) => (
                <li key={channel.channel} className="flex justify-between">
                  <span>{channel.channel}</span>
                  <span className="font-medium text-slate-800">{channel.conversations}</span>
                </li>
              ))}
              {(data?.channels ?? []).length === 0 && <li className="text-slate-400">暂无会话</li>}
            </ul>
          </section>

          <section className="rounded-lg border border-slate-200 bg-white p-4">
            <h2 className="text-sm font-semibold text-slate-900">B2B 漏斗</h2>
            <ul data-testid="dashboard-funnel" className="mt-2 space-y-1 text-xs text-slate-600">
              {(data?.funnel ?? []).map((stage) => (
                <li key={stage.stage} className="flex justify-between">
                  <span>{stage.stage}</span>
                  <span className="font-medium text-slate-800">{stage.count}</span>
                </li>
              ))}
            </ul>
          </section>

          <section className="rounded-lg border border-slate-200 bg-white p-4">
            <h2 className="text-sm font-semibold text-slate-900">待办提醒</h2>
            <ul data-testid="dashboard-tasks" className="mt-2 space-y-1 text-xs text-slate-600">
              {(data?.upcoming_tasks ?? []).map((task) => (
                <li key={task.id} className="truncate">
                  {task.priority === "high" ? "🔴 " : "• "}
                  {task.title}
                </li>
              ))}
              {(data?.upcoming_tasks ?? []).length === 0 && (
                <li className="text-slate-400">暂无待办</li>
              )}
            </ul>
            <Link href="/tasks" className="mt-2 inline-block text-xs font-medium text-indigo-600">
              查看全部任务 →
            </Link>
          </section>
        </div>

        <section className="rounded-lg border border-slate-200 bg-white p-4">
          <h2 className="text-sm font-semibold text-slate-900">快捷入口</h2>
          <div className="mt-2 flex flex-wrap gap-2 text-xs">
            {[
              { href: "/crm/leads", label: "线索池" },
              { href: "/crm/opportunities", label: "商机看板" },
              { href: "/supply-chain/purchase-orders", label: "采购收货" },
              { href: "/wms", label: "WMS 库存" },
              { href: "/analytics", label: "统计分析" },
            ].map((entry) => (
              <Link
                key={entry.href}
                href={entry.href}
                className="rounded border border-slate-200 px-2.5 py-1 font-medium text-slate-700 hover:bg-slate-100"
              >
                {entry.label}
              </Link>
            ))}
          </div>
        </section>
      </div>
    </AppShell>
  );
}

function KpiCard({
  label,
  value,
  icon: Icon,
}: {
  label: string;
  value: string;
  icon: React.ComponentType<{ className?: string }>;
}) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-3">
      <div className="flex items-center justify-between text-[10px] uppercase text-slate-400">
        <span>{label}</span>
        <Icon className="h-3.5 w-3.5" />
      </div>
      <p className="mt-1 text-lg font-semibold text-slate-900">{value}</p>
    </div>
  );
}
