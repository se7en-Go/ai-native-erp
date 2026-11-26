"use client";

import React, { useMemo, useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  Bot,
  Boxes,
  CheckCircle2,
  ClipboardList,
  DollarSign,
  Download,
  Inbox,
  LineChart,
  Package,
  Sparkles,
  Target,
  TrendingUp,
} from "lucide-react";

import { AppShell } from "@/components/app-shell";
import { FloatingCopilot } from "@/components/ai/floating-copilot";
import { ChannelAnalyticsCard } from "@/components/finance/channel-analytics-card";
import { useAuthSession } from "@/components/providers";
import {
  DashboardActivityRow,
  DashboardCard,
  DashboardKpiCard,
  DashboardProgressStat,
  type DashboardAccent,
} from "@/components/ui/dashboard-primitives";
import { RevenueMarginAreaChart } from "@/components/ui/erp-charts";
import { SegmentedTabs } from "@/components/ui/segmented-tabs";
import {
  buildCvpTrend,
  sumDecimalStrings,
  type CvpTrendWindow,
} from "@/components/finance/revenue-trend";
import { isPrivilegedCostViewer } from "@/lib/auth-cookie";
import { formatMoney } from "@/lib/contracts";
import { fetchFinanceOverview, fetchPendingProposalsCount } from "@/lib/data-source";
import { fetchLowStockAlerts } from "@/lib/api-client/pack-a-erp";
import { fetchDailyPatrolBrief, patrolHealthDescriptor } from "@/lib/api-client/pack-d-patrol";
import { fetchPhase3aDashboard } from "@/lib/phase3a-data";
import { fetchWorkforceEmployees } from "@/lib/phase3b-data";
import { useI18n } from "@/lib/i18n";

const FUNNEL_STAGE_LABEL: Record<string, { zh: string; en: string; accent: DashboardAccent }> = {
  discovery: { zh: "需求挖掘", en: "Discovery", accent: "blue" },
  sampling: { zh: "寄样确认", en: "Sampling", accent: "purple" },
  quoting: { zh: "报价中", en: "Quoting", accent: "blue" },
  negotiating: { zh: "商务谈判", en: "Negotiating", accent: "orange" },
  closed_won: { zh: "已赢单", en: "Closed won", accent: "green" },
  closed_lost: { zh: "已输单", en: "Closed lost", accent: "slate" },
};

const CHANNEL_LABEL: Record<string, { zh: string; en: string; dot: string }> = {
  web_livechat: { zh: "官网在线客服", en: "Web LiveChat", dot: "bg-blue-500" },
  whatsapp: { zh: "WhatsApp", en: "WhatsApp", dot: "bg-emerald-500" },
  email: { zh: "外贸邮件", en: "Email", dot: "bg-indigo-500" },
  facebook_messenger: { zh: "Facebook Messenger", en: "Facebook Messenger", dot: "bg-sky-500" },
  instagram_dm: { zh: "Instagram 私信", en: "Instagram DM", dot: "bg-rose-500" },
  telegram: { zh: "Telegram", en: "Telegram", dot: "bg-cyan-500" },
  shopify: { zh: "Shopify 独立站", en: "Shopify storefront", dot: "bg-violet-500" },
  woocommerce: { zh: "WooCommerce 独立站", en: "WooCommerce store", dot: "bg-purple-500" },
  shopline: { zh: "SHOPLINE", en: "SHOPLINE", dot: "bg-teal-500" },
  shoplazza: { zh: "Shoplazza 店匠", en: "Shoplazza", dot: "bg-amber-500" },
  tiktok_shop: { zh: "TikTok Shop", en: "TikTok Shop", dot: "bg-pink-500" },
  amazon_sp: { zh: "Amazon 亚马逊", en: "Amazon", dot: "bg-orange-500" },
  ebay: { zh: "eBay", en: "eBay", dot: "bg-yellow-500" },
  alibaba_email: { zh: "阿里巴巴国际站", en: "Alibaba.com", dot: "bg-amber-600" },
  shopee: { zh: "Shopee 虾皮", en: "Shopee", dot: "bg-orange-600" },
  lazada: { zh: "Lazada 来赞达", en: "Lazada", dot: "bg-blue-600" },
  temu: { zh: "Temu", en: "Temu", dot: "bg-red-500" },
  shein: { zh: "SHEIN", en: "SHEIN", dot: "bg-zinc-700" },
  aliexpress: { zh: "AliExpress 速卖通", en: "AliExpress", dot: "bg-red-600" },
  ozon: { zh: "Ozon", en: "Ozon", dot: "bg-sky-600" },
  walmart: { zh: "Walmart 沃尔玛", en: "Walmart", dot: "bg-blue-700" },
  mercado_libre: { zh: "Mercado Libre 美客多", en: "Mercado Libre", dot: "bg-lime-600" },
};

const QUICK_WORKBENCH_CHIPS = [
  { href: "/crm/leads", icon: Target, zh: "线索池", en: "Leads" },
  { href: "/catalog", icon: Package, zh: "产品库与 SKU", en: "Catalog" },
  { href: "/orders", icon: ClipboardList, zh: "订单与单证", en: "Orders" },
  { href: "/wms", icon: Boxes, zh: "WMS 库存", en: "WMS" },
  { href: "/ai/employees", icon: Bot, zh: "AI 员工", en: "AI Staff" },
] as const;

const PATROL_CATEGORY_ROUTE: Record<string, string> = {
  expiring_quotes: "/b2b/quotes",
  low_stock: "/wms",
  stalled_shipments: "/orders",
  pending_receivables: "/finance",
  pending_proposals: "/ai/proposals",
  unread_chats: "/inbox",
};

export default function DashboardPage() {
  const { principal } = useAuthSession();
  const { bi, locale } = useI18n();
  const showFinancials = isPrivilegedCostViewer(principal?.role);
  const [trendWindow, setTrendWindow] = useState<CvpTrendWindow>("recent5");

  const dashboardQuery = useQuery({
    queryKey: ["analytics", "dashboard"],
    queryFn: () => fetchPhase3aDashboard(),
  });
  const proposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () => fetchPendingProposalsCount(),
  });
  const financeQuery = useQuery({
    queryKey: ["finance", "overview"],
    enabled: showFinancials,
    queryFn: () => fetchFinanceOverview(),
  });
  const workforceQuery = useQuery({
    queryKey: ["ai", "workforce", "employees"],
    queryFn: fetchWorkforceEmployees,
  });
  const patrolQuery = useQuery({
    queryKey: ["ai", "patrol", "daily-brief"],
    queryFn: () => fetchDailyPatrolBrief(),
  });
  const lowStockQuery = useQuery({
    queryKey: ["wms", "low-stock", "dashboard-summary"],
    queryFn: () => fetchLowStockAlerts(),
  });

  const data = dashboardQuery.data;
  const pipelineCurrency = data?.pipeline_value?.[0]?.currency ?? "USD";
  const pipelineTotal = sumDecimalStrings(
    (data?.pipeline_value ?? []).map((entry) => String(entry.amount))
  );
  const pipelineCurrencies = (data?.pipeline_value ?? []).length;

  const snapshots = showFinancials ? (financeQuery.data?.cvp_snapshots ?? []) : [];
  const trend = buildCvpTrend(snapshots, trendWindow);

  const financials = data?.financials ?? [];
  const primaryFinancial = financials[0];
  const netMarginTotal = primaryFinancial
    ? sumDecimalStrings(financials.map((entry) => String(entry.net_contribution_margin)))
    : null;
  const cogsTotal = primaryFinancial
    ? sumDecimalStrings(financials.map((entry) => String(entry.cost)))
    : null;

  const enabledEmployees = (workforceQuery.data ?? []).filter((row) => row.is_enabled).length;
  const channelRows = useMemo(
    () => [...(data?.channels ?? [])].sort((a, b) => b.conversations - a.conversations),
    [data?.channels]
  );
  const channelTotal = channelRows.reduce((sum, row) => sum + row.conversations, 0);

  const funnelRows = data?.funnel ?? [];
  const funnelMax = Math.max(...funnelRows.map((s) => s.count), 1);
  const patrolBrief = patrolQuery.data;
  const healthScore = patrolBrief?.health_score ?? null;
  const healthDescriptor = healthScore !== null ? patrolHealthDescriptor(healthScore) : null;
  const lowStockCount = lowStockQuery.data?.total ?? lowStockQuery.data?.items?.length ?? 0;

  // Merge real AI patrol alerts + real upcoming trade tasks into one clean 21st Recent Activity stream.
  const activityItems = useMemo(() => {
    const rows: Array<{
      id: string;
      icon: typeof Activity;
      accent: DashboardAccent;
      title: string;
      description: string;
      meta: string;
      href: string;
    }> = [];

    for (const alert of (patrolBrief?.alerts ?? []).slice(0, 3)) {
      const severityAccent: DashboardAccent =
        alert.severity === "high"
          ? "rose"
          : alert.severity === "medium"
            ? "orange"
            : "blue";
      rows.push({
        id: `patrol-${alert.code}`,
        icon: alert.severity === "high" ? AlertTriangle : Activity,
        accent: severityAccent,
        title: bi(alert.title_zh, alert.title_en),
        description: bi(alert.detail_zh, alert.detail_en),
        meta: alert.employee_name || bi("AI 巡检", "AI patrol"),
        href: alert.action_href || PATROL_CATEGORY_ROUTE[alert.code] || "/ai/employees",
      });
    }

    for (const task of (data?.upcoming_tasks ?? []).slice(0, 4)) {
      rows.push({
        id: `task-${task.id}`,
        icon: task.priority === "high" ? ClipboardList : CheckCircle2,
        accent: task.priority === "high" ? "orange" : "green",
        title: task.title,
        description:
          task.priority === "high"
            ? bi("高优先级待办 · 建议当日处理", "High priority task · handle today")
            : bi("外贸常规跟进任务", "Routine trade follow-up"),
        meta: task.priority === "high" ? bi("高优", "Urgent") : bi("待办", "To-do"),
        href: "/tasks",
      });
    }

    return rows;
  }, [patrolBrief?.alerts, data?.upcoming_tasks, bi]);

  return (
    <AppShell currentPath="/dashboard" pendingProposalsCount={proposalsQuery.data?.total ?? 0}>
      <div className="erp-page space-y-6">
        {/* ── 21st.dev Clean Header ─────────────────────────────── */}
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="min-w-0">
            <h1 className="text-2xl font-bold tracking-tight text-gray-900 sm:text-3xl dark:text-gray-100">
              {bi("经营总览", "Dashboard")}
            </h1>
            <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[13.5px] text-gray-500 dark:text-gray-400">
              <span className="inline-flex items-center gap-1.5 font-medium text-emerald-600 dark:text-emerald-400">
                <span className="h-2 w-2 rounded-full bg-emerald-500" />
                {bi(
                  `${enabledEmployees || "—"} 位 AI 员工在线`,
                  `${enabledEmployees || "—"} AI staff online`
                )}
              </span>
              <span aria-hidden="true">·</span>
              <span>
                {bi(
                  "实时外贸询盘、管道、CVP 边际毛利与巡检动态",
                  "Live inquiries, B2B pipeline, CVP margin & AI patrol"
                )}
              </span>
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            {showFinancials && (
              <Link
                href="/finance"
                className="inline-flex h-9 items-center gap-1.5 rounded-lg border border-gray-200 bg-white px-3 text-[12.5px] font-semibold text-gray-700 shadow-xs transition-colors hover:bg-gray-50 hover:text-gray-900 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200 dark:hover:bg-gray-800"
              >
                <LineChart className="h-3.5 w-3.5 text-blue-600 dark:text-blue-400" />
                <span>{bi("财务对账", "Finance")}</span>
              </Link>
            )}
            <Link
              href="/analytics"
              className="inline-flex h-9 items-center gap-1.5 rounded-lg border border-gray-200 bg-white px-3 text-[12.5px] font-semibold text-gray-700 shadow-xs transition-colors hover:bg-gray-50 hover:text-gray-900 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200 dark:hover:bg-gray-800"
            >
              <BarChart3 className="h-3.5 w-3.5 text-gray-500 dark:text-gray-400" />
              <span>{bi("统计分析", "Analytics")}</span>
            </Link>
            {showFinancials && (
              <a
                href="/api/bff/v1/finance/cvp/export.csv"
                className="inline-flex h-9 items-center gap-1.5 rounded-lg border border-gray-200 bg-white px-3 text-[12.5px] font-semibold text-gray-700 shadow-xs transition-colors hover:bg-gray-50 hover:text-gray-900 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200 dark:hover:bg-gray-800"
              >
                <Download className="h-3.5 w-3.5 text-gray-500 dark:text-gray-400" />
                <span>{bi("导出对账", "Export")}</span>
              </a>
            )}
          </div>
        </div>

        {dashboardQuery.isError && (
          <p
            role="alert"
            className="rounded-xl border border-rose-200 bg-rose-50 px-4 py-2.5 text-[12.5px] font-medium text-rose-800 dark:border-rose-900/60 dark:bg-rose-950/50 dark:text-rose-200"
          >
            {dashboardQuery.error.message}
          </p>
        )}

        {/* ── 4-Column 21st.dev KPI Grid ───────────────────────── */}
        <div
          data-testid="dashboard-kpi-grid"
          className="grid grid-cols-1 gap-5 md:grid-cols-2 lg:grid-cols-4"
        >
          <DashboardKpiCard
            label={bi("待处理询盘", "Open inquiries")}
            value={String(data?.open_inquiries ?? "—")}
            icon={Inbox}
            accent="blue"
            subtext={bi("B2B / 混合会话待跟进", "Threads awaiting reply")}
            href="/inbox"
            ctaLabel={bi("进入沟通", "Inbox")}
          />

          <DashboardKpiCard
            label={bi("B2B 管道金额", "B2B pipeline value")}
            value={formatMoney(pipelineTotal, pipelineCurrency)}
            icon={DollarSign}
            accent="green"
            badge={bi(`${pipelineCurrencies} 币种`, `${pipelineCurrencies} ccy`)}
            subtext={bi("未关单商机汇总", "Open deals value")}
            href="/crm/opportunities"
            ctaLabel={bi("商机", "Deals")}
          />

          {showFinancials ? (
            <DashboardKpiCard
              testId="dashboard-financials"
              label={bi("净贡献毛利", "Net contribution margin")}
              value={formatMoney(netMarginTotal, primaryFinancial?.currency ?? "CNY")}
              icon={TrendingUp}
              accent="purple"
              badge={bi("已扣全成本", "Net CVP")}
              subtext={bi(
                `成本 ${formatMoney(cogsTotal, primaryFinancial?.currency ?? "CNY")} · B2C ${data?.b2c_orders ?? 0} 单`,
                `COGS ${formatMoney(cogsTotal, primaryFinancial?.currency ?? "CNY")} · B2C ${data?.b2c_orders ?? 0}`
              )}
            />
          ) : (
            <div
              data-testid="dashboard-financials-masked"
              className="flex flex-col justify-between rounded-xl border border-amber-200 bg-amber-50/70 p-5 shadow-sm dark:border-amber-900/50 dark:bg-amber-950/30"
            >
              <div className="mb-3 flex items-center justify-between">
                <div className="grid h-9 w-9 place-content-center rounded-lg bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300">
                  <AlertTriangle className="h-5 w-5" aria-hidden="true" />
                </div>
                <span className="rounded-full bg-amber-100 px-2.5 py-0.5 text-[11px] font-medium text-amber-800 dark:bg-amber-900/50 dark:text-amber-200">
                  RBAC
                </span>
              </div>
              <div>
                <h3 className="text-[13px] font-medium text-amber-900 dark:text-amber-200">
                  {bi("净贡献毛利已脱敏", "Net margin masked")}
                </h3>
                <p className="mt-1 text-2xl font-bold text-amber-900/60 dark:text-amber-300/60">
                  • • •
                </p>
                <p className="mt-2 text-[12px] text-amber-800 dark:text-amber-300/90">
                  {bi(
                    `当前角色（${principal?.role ?? "—"}）不展示成本与毛利。`,
                    `Hidden for role (${principal?.role ?? "—"}).`
                  )}
                </p>
              </div>
            </div>
          )}

          <DashboardKpiCard
            label={bi("待审 AI 提案", "Pending AI proposals")}
            value={String(data?.pending_proposals ?? "—")}
            icon={Bot}
            accent="orange"
            badge={
              (data?.pending_proposals ?? 0) > 0
                ? bi("待确认", "Review")
                : bi("已清空", "Clear")
            }
            subtext={bi("L1 人工确认后回写 ERP", "Human-approved writeback")}
            href="/ai/proposals"
            ctaLabel={bi("去审批", "Approve")}
          />
        </div>

        {/* ── 21st.dev 2:1 Main Grid ───────────────────────────── */}
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
          {/* Left 2 Columns: Trend + Recent Trade Activity */}
          <div className="space-y-6 lg:col-span-2">
            <DashboardCard
              title={bi("营收与 CVP 边际毛利趋势", "Revenue & CVP margin trend")}
              subtitle={bi(
                "逐笔订单取自真实订单利润快照（固定点精度，无模拟插值）。",
                "Real per-order profit snapshots (fixed-point precision, no synthetic interpolation)."
              )}
              action={
                showFinancials ? (
                  <SegmentedTabs<CvpTrendWindow>
                    ariaLabel={bi("趋势范围", "Trend range")}
                    value={trendWindow}
                    onChange={setTrendWindow}
                    size="sm"
                    options={[
                      { value: "recent5", label: bi("近 5 单", "Latest 5") },
                      { value: "recent10", label: bi("近 10 单", "Latest 10") },
                      { value: "all", label: bi("全部", "All") },
                    ]}
                  />
                ) : null
              }
            >
              {showFinancials ? (
                <div className="space-y-4">
                  <div className="grid grid-cols-1 gap-3 rounded-lg border border-gray-100 bg-gray-50 p-3.5 sm:grid-cols-3 dark:border-gray-800 dark:bg-gray-800/60">
                    <SummaryCell
                      label={bi("营收（选定范围）", "Revenue (window)")}
                      value={formatMoney(trend.revenue, trend.baseCurrency)}
                      tone="blue"
                    />
                    <SummaryCell
                      label={bi("净贡献毛利", "Net margin")}
                      value={formatMoney(trend.margin, trend.baseCurrency)}
                      suffix={`${trend.marginPct}%`}
                      tone="emerald"
                    />
                    <SummaryCell
                      label={bi("B2C 订单（累计）", "B2C orders")}
                      value={String(data?.b2c_orders ?? "—")}
                      tone="slate"
                    />
                  </div>

                  {trend.points.length >= 2 ? (
                    <RevenueMarginAreaChart
                      data={trend.points.map((point) => ({
                        label: point.label.replace(/^ORD-/, ""),
                        revenue: point.revenue,
                        margin: point.margin,
                      }))}
                      revenueLabel={bi("营收", "Revenue")}
                      marginLabel={bi("净贡献毛利", "Net margin")}
                      currencyPrefix={trend.baseCurrency === "CNY" ? "¥" : "$"}
                    />
                  ) : (
                    <div className="grid h-[180px] place-items-center rounded-lg border border-dashed border-gray-200 bg-gray-50 text-[13px] font-medium text-gray-500 dark:border-gray-700 dark:bg-gray-800/40 dark:text-gray-400">
                      {trend.points.length === 1
                        ? bi(
                            "仅 1 笔订单利润快照，已展示汇总数字；满 2 笔自动绘制趋势曲线。",
                            "1 profit snapshot so far — curve appears once 2 orders exist."
                          )
                        : bi("暂无订单利润快照。", "No order profit snapshots yet.")}
                    </div>
                  )}
                </div>
              ) : (
                <div
                  data-testid="dashboard-trend-masked"
                  className="flex items-start gap-2.5 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-[12.5px] font-medium text-amber-900 dark:border-amber-900/50 dark:bg-amber-950/30 dark:text-amber-200"
                >
                  <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400" />
                  <span>
                    {bi(
                      "营收与毛利趋势仅对 Owner / Admin / Manager / Finance 开放，当前角色已脱敏。",
                      "Revenue and margin trends are restricted to Owner/Admin/Manager/Finance."
                    )}
                  </span>
                </div>
              )}
            </DashboardCard>

            <ChannelAnalyticsCard enabled={showFinancials} />

            {/* 21st.dev Recent Activity — Real AI Patrol Alerts + Trade To-dos */}
            <DashboardCard
              testId="dashboard-tasks"
              title={bi("实时外贸动态与待办", "Recent trade activity & tasks")}
              subtitle={bi(
                "整合 AI 主动巡检预警与到期外贸跟进任务，点击直达处理。",
                "Live AI patrol alerts & upcoming trade follow-ups."
              )}
              action={
                <Link
                  href="/tasks"
                  className="text-[13px] font-medium text-blue-600 transition-colors hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300"
                >
                  {bi("查看全部", "View all")}
                </Link>
              }
            >
              {activityItems.length > 0 ? (
                <div className="space-y-1">
                  {activityItems.map((row) => (
                    <DashboardActivityRow
                      key={row.id}
                      icon={row.icon}
                      accent={row.accent}
                      title={row.title}
                      description={row.description}
                      meta={row.meta}
                      href={row.href}
                    />
                  ))}
                </div>
              ) : (
                <div className="rounded-lg border border-dashed border-gray-200 bg-gray-50 px-4 py-6 text-center text-[13px] text-gray-500 dark:border-gray-700 dark:bg-gray-800/40 dark:text-gray-400">
                  {bi("当前无紧急预警与逾期任务，经营节奏良好。", "No urgent alerts or overdue tasks.")}
                </div>
              )}
            </DashboardCard>
          </div>

          {/* Right Column: Quick Stats (Patrol Health + Funnel) & Top Channels */}
          <div className="space-y-6">
            <DashboardCard
              testId="dashboard-funnel"
              title={bi("经营健康与商机进度", "Health & funnel stats")}
              subtitle={bi("AI 巡检健康分与商机各阶段分布", "AI patrol score & deal stage breakdown")}
            >
              <div className="space-y-4">
                {healthScore !== null && healthDescriptor && (
                  <div className="rounded-lg border border-blue-100 bg-blue-50/60 p-3 dark:border-blue-900/40 dark:bg-blue-950/30">
                    <DashboardProgressStat
                      label={bi(
                        `AI 经营健康分 · ${healthDescriptor.zh}`,
                        `AI Health · ${healthDescriptor.en}`
                      )}
                      valueLabel={`${healthScore} / 100`}
                      percent={healthScore}
                      accent={
                        healthDescriptor.tone === "emerald"
                          ? "green"
                          : healthDescriptor.tone === "amber"
                            ? "orange"
                            : "rose"
                      }
                    />
                  </div>
                )}

                {funnelRows.length > 0 ? (
                  funnelRows.map((stage) => {
                    const meta = FUNNEL_STAGE_LABEL[stage.stage];
                    const pct = Math.round((stage.count / funnelMax) * 100);
                    return (
                      <DashboardProgressStat
                        key={stage.stage}
                        label={meta?.[locale] ?? stage.stage}
                        valueLabel={bi(`${stage.count} 个`, `${stage.count}`)}
                        percent={pct}
                        accent={meta?.accent ?? "blue"}
                      />
                    );
                  })
                ) : (
                  <p className="text-[12.5px] text-gray-500 dark:text-gray-400">
                    {bi("暂无商机数据", "No opportunities yet")}
                  </p>
                )}

                <div className="flex items-start gap-2 rounded-lg border border-gray-200 bg-gray-50 p-3 text-[12px] text-gray-600 dark:border-gray-800 dark:bg-gray-800/60 dark:text-gray-300">
                  <Sparkles className="mt-0.5 h-3.5 w-3.5 shrink-0 text-blue-600 dark:text-blue-400" />
                  <span>
                    {bi(
                      `当前 ${data?.pending_proposals ?? 0} 条 AI 提案待审，审批后自动回写报价与跟进记录。`,
                      `${data?.pending_proposals ?? 0} AI proposals pending approval for ERP writeback.`
                    )}
                  </span>
                </div>
              </div>
            </DashboardCard>

            <DashboardCard
              testId="dashboard-channels"
              title={bi("全渠道询盘排行", "Top inquiry channels")}
              subtitle={bi(
                `累计 ${channelTotal} 条真实会话`,
                `${channelTotal} live conversations`
              )}
              action={
                <Link
                  href="/inbox"
                  className="text-[13px] font-medium text-blue-600 transition-colors hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300"
                >
                  {bi("聚合沟通", "Inbox")}
                </Link>
              }
            >
              <div className="space-y-2.5">
                {channelRows.length > 0 ? (
                  channelRows.map((row) => {
                    const meta = CHANNEL_LABEL[row.channel];
                    const share = channelTotal
                      ? Math.round((row.conversations / channelTotal) * 100)
                      : 0;
                    return (
                      <div
                        key={row.channel}
                        className="flex items-center justify-between gap-2 rounded-lg px-2 py-1.5 text-[13px] transition-colors hover:bg-gray-50 dark:hover:bg-gray-800"
                      >
                        <span className="flex min-w-0 items-center gap-2.5">
                          <span
                            className={`h-2.5 w-2.5 shrink-0 rounded-full ${meta?.dot ?? "bg-blue-500"}`}
                          />
                          <span className="truncate text-gray-700 dark:text-gray-300">
                            {meta?.[locale] ?? row.channel}
                          </span>
                        </span>
                        <span className="flex shrink-0 items-center gap-2 tabular-nums">
                          <span className="font-semibold text-gray-900 dark:text-gray-100">
                            {row.conversations}
                          </span>
                          <span className="w-9 text-right text-[11.5px] text-gray-400 dark:text-gray-500">
                            {share}%
                          </span>
                        </span>
                      </div>
                    );
                  })
                ) : (
                  <p className="text-[12.5px] text-gray-500 dark:text-gray-400">
                    {bi("暂无渠道会话", "No conversations yet")}
                  </p>
                )}
              </div>

              {lowStockCount > 0 && (
                <Link
                  href="/wms"
                  className="mt-4 flex items-center justify-between rounded-lg border border-orange-200 bg-orange-50 px-3 py-2 text-[12px] font-medium text-orange-800 transition-colors hover:bg-orange-100 dark:border-orange-900/50 dark:bg-orange-950/30 dark:text-orange-300"
                >
                  <span className="inline-flex items-center gap-1.5">
                    <AlertTriangle className="h-3.5 w-3.5" />
                    {bi(`低库存 SKU 预警：${lowStockCount} 项`, `Low-stock SKUs: ${lowStockCount}`)}
                  </span>
                  <span className="font-semibold">{bi("去补货 →", "Restock →")}</span>
                </Link>
              )}

              <div className="mt-4 border-t border-gray-100 pt-3.5 dark:border-gray-800">
                <p className="mb-2 text-[11.5px] font-semibold tracking-wide text-gray-400 uppercase dark:text-gray-500">
                  {bi("高频直达", "Quick jump")}
                </p>
                <div className="flex flex-wrap gap-1.5">
                  {QUICK_WORKBENCH_CHIPS.map(({ href, icon: Icon, zh, en }) => (
                    <Link
                      key={href}
                      href={href}
                      className="inline-flex items-center gap-1.5 rounded-lg border border-gray-200 bg-gray-50 px-2.5 py-1.5 text-[12px] font-medium text-gray-700 transition-colors hover:border-blue-300 hover:bg-blue-50 hover:text-blue-700 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300 dark:hover:border-blue-700 dark:hover:bg-blue-900/30 dark:hover:text-blue-300"
                    >
                      <Icon className="h-3.5 w-3.5" aria-hidden="true" />
                      <span>{bi(zh, en)}</span>
                    </Link>
                  ))}
                </div>
              </div>
            </DashboardCard>
          </div>
        </div>
      </div>
      <FloatingCopilot />
    </AppShell>
  );
}

function SummaryCell({
  label,
  value,
  suffix,
  tone,
}: {
  label: string;
  value: string;
  suffix?: string;
  tone: "blue" | "emerald" | "slate";
}) {
  const valueTone =
    tone === "blue"
      ? "text-blue-600 dark:text-blue-400"
      : tone === "emerald"
        ? "text-emerald-600 dark:text-emerald-400"
        : "text-gray-900 dark:text-gray-100";
  return (
    <div className="min-w-0">
      <p className="truncate text-[11.5px] font-medium text-gray-500 dark:text-gray-400">{label}</p>
      <p
        className={`mt-1 flex flex-wrap items-baseline gap-1.5 text-[17px] font-bold tabular-nums ${valueTone}`}
      >
        <span className="truncate">{value}</span>
        {suffix && (
          <span className="rounded-full bg-emerald-50 px-1.5 py-0.5 text-[11px] font-semibold text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300">
            {suffix}
          </span>
        )}
      </p>
    </div>
  );
}
