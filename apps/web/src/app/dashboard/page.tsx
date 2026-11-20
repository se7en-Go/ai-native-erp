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

const FUNNEL_STAGE_LABEL: Record<string, { zh: string; en: string }> = {
  discovery: { zh: "需求挖掘", en: "Discovery" },
  sampling: { zh: "寄样确认", en: "Sampling" },
  quoting: { zh: "报价中", en: "Quoting" },
  negotiating: { zh: "商务谈判", en: "Negotiating" },
  closed_won: { zh: "已赢单", en: "Closed won" },
  closed_lost: { zh: "已输单", en: "Closed lost" },
};

const CHANNEL_LABEL: Record<string, { zh: string; en: string }> = {
  web_livechat: { zh: "官网在线客服", en: "Web LiveChat" },
  whatsapp: { zh: "WhatsApp", en: "WhatsApp" },
  email: { zh: "外贸邮件", en: "Email" },
  facebook_messenger: { zh: "Facebook Messenger", en: "Facebook Messenger" },
  instagram_dm: { zh: "Instagram 私信", en: "Instagram DM" },
  telegram: { zh: "Telegram", en: "Telegram" },
};

export default function DashboardPage() {
  const { principal } = useAuthSession();
  const { locale, bi } = useI18n();
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
          <h1 className="text-[22px] font-bold tracking-tight text-slate-950">
            {bi("经营总览", "Executive dashboard")}
          </h1>
          <p className="mt-1 text-[13.5px] leading-6 text-slate-700">
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
              <p className="text-[12.5px] font-semibold text-slate-700">{bi("B2C 订单", "B2C orders")}</p>
              <p className="mt-1 text-xl font-bold tabular-nums text-slate-950">{data?.b2c_orders ?? "—"}</p>
            </div>
            {(data?.financials ?? []).map((entry) => (
              <div key={entry.currency}>
                <p className="text-[12.5px] font-semibold text-slate-700">
                  {bi("成本与净贡献毛利", "COGS & net margin")} ({entry.currency})
                </p>
                <p className="mt-1 text-[15px] font-bold tabular-nums text-slate-950">
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
            className="rounded-xl border border-amber-300 bg-amber-50 p-3 text-[13px] font-medium text-amber-900"
          >
            <AlertTriangle className="mr-1 inline h-4 w-4" />
            {bi(
              `当前角色（${principal?.role}）不可查看成本与毛利，KPI 已自动脱敏。`,
              `Your role (${principal?.role}) cannot view costs and margins; KPIs are masked automatically.`
            )}
          </div>
        )}

        <div className="grid gap-4 lg:grid-cols-3">
          <section className="erp-card p-4">
            <h2 className="text-[15px] font-semibold text-slate-950">{bi("渠道会话分布", "Channel mix")}</h2>
            <ul data-testid="dashboard-channels" className="mt-3 space-y-2 text-[13px] text-slate-700">
              {(data?.channels ?? []).map((channel) => (
                <li key={channel.channel} className="flex items-center justify-between border-b border-slate-100 pb-1.5 last:border-0 last:pb-0">
                  <span className="font-medium text-slate-800">
                    {CHANNEL_LABEL[channel.channel]?.[locale] ?? channel.channel}
                  </span>
                  <span className="font-bold tabular-nums text-slate-950">{channel.conversations}</span>
                </li>
              ))}
              {(data?.channels ?? []).length === 0 && (
                <li className="text-slate-600">{bi("暂无会话", "No conversations")}</li>
              )}
            </ul>
          </section>

          <section className="erp-card p-4">
            <h2 className="text-[15px] font-semibold text-slate-950">{bi("B2B 商机漏斗", "B2B funnel")}</h2>
            <ul data-testid="dashboard-funnel" className="mt-3 space-y-2 text-[13px] text-slate-700">
              {(data?.funnel ?? []).map((stage) => (
                <li key={stage.stage} className="flex items-center justify-between border-b border-slate-100 pb-1.5 last:border-0 last:pb-0">
                  <span className="font-medium text-slate-800">
                    {FUNNEL_STAGE_LABEL[stage.stage]?.[locale] ?? stage.stage}
                  </span>
                  <span className="font-bold tabular-nums text-slate-950">{stage.count}</span>
                </li>
              ))}
            </ul>
          </section>

          <section className="erp-card p-4">
            <h2 className="text-[15px] font-semibold text-slate-950">{bi("待办提醒", "Upcoming tasks")}</h2>
            <ul data-testid="dashboard-tasks" className="mt-3 space-y-2 text-[13px] text-slate-800">
              {(data?.upcoming_tasks ?? []).map((task) => (
                <li key={task.id} className="truncate font-medium">
                  {task.priority === "high" ? "🔴 " : "• "}
                  {task.title}
                </li>
              ))}
              {(data?.upcoming_tasks ?? []).length === 0 && (
                <li className="text-slate-600">{bi("暂无待办", "No tasks")}</li>
              )}
            </ul>
            <Link href="/tasks" className="mt-3 inline-block text-[13px] font-semibold text-blue-700 hover:underline">
              {bi("查看全部任务 →", "View all tasks →")}
            </Link>
          </section>
        </div>

        <section className="erp-card p-4">
          <h2 className="text-[15px] font-semibold text-slate-950">{bi("快捷入口", "Quick links")}</h2>
          <div className="mt-3 flex flex-wrap gap-2 text-[13px]">
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
                className="rounded-lg border border-slate-300 bg-slate-50 px-3 py-1.5 font-semibold text-slate-800 transition-colors hover:border-blue-400 hover:bg-blue-50 hover:text-blue-700"
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
      <div className="flex items-center justify-between text-[13px] font-semibold text-slate-700">
        <span>{label}</span>
        <Icon className="h-4 w-4 text-slate-500" />
      </div>
      <p className="mt-1.5 text-[24px] font-bold tabular-nums text-slate-950">{value}</p>
    </div>
  );
}
