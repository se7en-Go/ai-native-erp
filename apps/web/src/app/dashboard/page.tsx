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
import { useI18n } from "@/lib/i18n";

export default function DashboardPage() {
  const { principal } = useAuthSession();
  const { bi } = useI18n();
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
      <div className="erp-page space-y-4">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-slate-900">
            {bi("经营总览", "Executive dashboard")}
          </h1>
          <p className="mt-1 text-[13px] leading-6 text-slate-500">
            {bi(
              "租户隔离的实时经营指标：待处理询盘、B2B 管道金额、待审 AI 提案、库存预警与渠道分布。",
              "Tenant-isolated live metrics: open inquiries, B2B pipeline value, pending AI proposals, stock alerts and channel mix."
            )}
          </p>
        </div>

        {dashboardQuery.isError && (
          <p role="alert" className="rounded border border-rose-200 bg-rose-50 p-2 text-xs text-rose-800">
            {dashboardQuery.error.message}
          </p>
        )}

        <div
          data-testid="dashboard-kpi-grid"
          className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"
        >
          <KpiCard
            label={bi("待处理询盘", "Open inquiries")}
            value={String(data?.open_inquiries ?? "—")}
            icon={Inbox}
          />
          <KpiCard
            label={bi("B2B 管道金额", "B2B pipeline value")}
            value={formatMoney(pipelineTotal, pipelineCurrency)}
            icon={ArrowRight}
          />
          <KpiCard
            label={bi("待审 AI 提案", "Pending AI proposals")}
            value={String(data?.pending_proposals ?? "—")}
            icon={Sparkles}
          />
          <KpiCard
            label={bi("待办任务", "Open tasks")}
            value={String(data?.open_tasks ?? "—")}
            icon={ClipboardList}
          />
        </div>

        {showFinancials ? (
          <div
            data-testid="dashboard-financials"
            className="erp-card grid gap-4 p-4 sm:grid-cols-2 xl:grid-cols-4"
          >
            <div>
              <p className="text-[11px] text-slate-400">{bi("B2C 订单", "B2C orders")}</p>
              <p className="text-lg font-semibold text-slate-900">{data?.b2c_orders ?? "—"}</p>
            </div>
            {(data?.financials ?? []).map((entry) => (
              <div key={entry.currency}>
                <p className="text-[11px] text-slate-400">
                  {bi("成本与净贡献毛利", "COGS & net margin")} ({entry.currency})
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
            className="rounded-xl border border-amber-200 bg-amber-50 p-3 text-[12px] text-amber-900"
          >
            <AlertTriangle className="mr-1 inline h-3.5 w-3.5" />
            {bi(
              `当前角色（${principal?.role}）不可查看成本与毛利，KPI 已自动脱敏。`,
              `Your role (${principal?.role}) cannot view costs and margins; KPIs are masked automatically.`
            )}
          </div>
        )}

        <div className="grid gap-4 lg:grid-cols-3">
          <section className="erp-card p-4">
            <h2 className="text-sm font-semibold text-slate-900">{bi("渠道会话分布", "Channel mix")}</h2>
            <ul data-testid="dashboard-channels" className="mt-2 space-y-1 text-xs text-slate-600">
              {(data?.channels ?? []).map((channel) => (
                <li key={channel.channel} className="flex justify-between">
                  <span>{channel.channel}</span>
                  <span className="font-medium text-slate-800">{channel.conversations}</span>
                </li>
              ))}
              {(data?.channels ?? []).length === 0 && (
                <li className="text-slate-400">{bi("暂无会话", "No conversations")}</li>
              )}
            </ul>
          </section>

          <section className="erp-card p-4">
            <h2 className="text-sm font-semibold text-slate-900">{bi("B2B 商机漏斗", "B2B funnel")}</h2>
            <ul data-testid="dashboard-funnel" className="mt-2 space-y-1 text-xs text-slate-600">
              {(data?.funnel ?? []).map((stage) => (
                <li key={stage.stage} className="flex justify-between">
                  <span>{stage.stage}</span>
                  <span className="font-medium text-slate-800">{stage.count}</span>
                </li>
              ))}
            </ul>
          </section>

          <section className="erp-card p-4">
            <h2 className="text-sm font-semibold text-slate-900">{bi("待办提醒", "Upcoming tasks")}</h2>
            <ul data-testid="dashboard-tasks" className="mt-2 space-y-1 text-xs text-slate-600">
              {(data?.upcoming_tasks ?? []).map((task) => (
                <li key={task.id} className="truncate">
                  {task.priority === "high" ? "🔴 " : "• "}
                  {task.title}
                </li>
              ))}
              {(data?.upcoming_tasks ?? []).length === 0 && (
                <li className="text-slate-400">{bi("暂无待办", "No tasks")}</li>
              )}
            </ul>
            <Link href="/tasks" className="mt-2 inline-block text-[12px] font-medium text-blue-600">
              {bi("查看全部任务 →", "View all tasks →")}
            </Link>
          </section>
        </div>

        <section className="erp-card p-4">
          <h2 className="text-sm font-semibold text-slate-900">{bi("快捷入口", "Quick links")}</h2>
          <div className="mt-2 flex flex-wrap gap-2 text-xs">
            {[
              { href: "/crm/leads", label: bi("线索池", "Leads") },
              { href: "/crm/opportunities", label: bi("商机看板", "Opportunities") },
              { href: "/supply-chain/purchase-orders", label: bi("采购收货", "Purchasing") },
              { href: "/wms", label: bi("WMS 库存", "WMS stock") },
              { href: "/analytics", label: bi("统计分析", "Analytics") },
            ].map((entry) => (
              <Link
                key={entry.href}
                href={entry.href}
                className="rounded-lg border border-slate-200 px-2.5 py-1 font-medium text-slate-600 hover:bg-slate-50"
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
    <div className="erp-card p-4">
      <div className="flex items-center justify-between text-[12px] font-medium text-slate-500">
        <span>{label}</span>
        <Icon className="h-4 w-4 text-slate-300" />
      </div>
      <p className="mt-1.5 text-[22px] font-semibold tabular-nums text-slate-900">{value}</p>
    </div>
  );
}
