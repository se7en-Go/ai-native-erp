"use client";

import React, { useMemo, useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowRight,
  ArrowUpRight,
  Boxes,
  Bot,
  ClipboardList,
  Download,
  Inbox,
  Layers,
  LineChart,
  Package,
  ShieldCheck,
  Sparkles,
  Store,
  Target,
  TrendingUp,
  Users,
} from "lucide-react";

import { AppShell } from "@/components/app-shell";
import { useAuthSession } from "@/components/providers";
import { BentoStatCard } from "@/components/ui/bento-stat-card";
import { SegmentedTabs } from "@/components/ui/segmented-tabs";
import { ChannelDonutChart, HorizontalFunnelBars, RevenueMarginAreaChart } from "@/components/ui/erp-charts";
import { buildCvpTrend, sumDecimalStrings, type CvpTrendWindow } from "@/components/finance/revenue-trend";
import { isPrivilegedCostViewer } from "@/lib/auth-cookie";
import { formatMoney } from "@/lib/contracts";
import { fetchFinanceOverview, fetchPendingProposalsCount } from "@/lib/data-source";
import { fetchPhase3aDashboard } from "@/lib/phase3a-data";
import { fetchWorkforceEmployees } from "@/lib/phase3b-data";
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
  shopify: { zh: "Shopify 独立站", en: "Shopify storefront" },
};

const CHANNEL_COLOR: Record<string, string> = {
  web_livechat: "#059669",
  whatsapp: "#0D9488",
  email: "#18181B",
  facebook_messenger: "#D97706",
  instagram_dm: "#E11D48",
  telegram: "#047857",
  shopify: "#57534E",
};

const FALLBACK_COLORS = ["#059669", "#18181B", "#0D9488", "#D97706", "#E11D48", "#57534E"];

const WORKBENCH_LINKS = [
  {
    href: "/crm/leads",
    icon: Target,
    accent: "bg-blue-50 text-blue-600 ring-1 ring-blue-100",
    zh: "线索池",
    en: "Lead pool",
    descZh: "未分配询盘与线索评分",
    descEn: "Unassigned inquiries & scoring",
  },
  {
    href: "/crm/opportunities",
    icon: TrendingUp,
    accent: "bg-violet-50 text-violet-600 ring-1 ring-violet-100",
    zh: "商机看板",
    en: "Opportunity board",
    descZh: "阶段推进与预计金额",
    descEn: "Stage progression & value",
  },
  {
    href: "/supply-chain/purchase-orders",
    icon: Package,
    accent: "bg-amber-50 text-amber-600 ring-1 ring-amber-100",
    zh: "采购收货",
    en: "Purchasing",
    descZh: "供应商交期与入库",
    descEn: "Supplier ETAs & receiving",
  },
  {
    href: "/wms",
    icon: Boxes,
    accent: "bg-emerald-50 text-emerald-600 ring-1 ring-emerald-100",
    zh: "WMS 库存",
    en: "WMS stock",
    descZh: "在手、占用与盘点流水",
    descEn: "On-hand, reserved & stocktakes",
  },
  {
    href: "/ai/employees",
    icon: Bot,
    accent: "bg-slate-100 text-slate-700 ring-1 ring-slate-200",
    zh: "AI 数字员工",
    en: "AI employees",
    descZh: "自治级别与近 7 天动作",
    descEn: "Autonomy levels & 7-day actions",
  },
] as const;

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
  const marginSparkline = useMemo(
    () =>
      [...snapshots]
        .slice(0, 8)
        .reverse()
        .map((snap) => Number(snap.net_contribution_margin_base))
        .filter((value) => Number.isFinite(value)),
    [snapshots]
  );
  const enabledEmployees = (workforceQuery.data ?? []).filter((row) => row.is_enabled).length;
  const channelRows = data?.channels ?? [];
  const channelTotal = channelRows.reduce((sum, row) => sum + row.conversations, 0);
  const highPriorityTasks = (data?.upcoming_tasks ?? []).filter(
    (task) => task.priority === "high"
  ).length;

  return (
    <AppShell currentPath="/dashboard" pendingProposalsCount={proposalsQuery.data?.total ?? 0}>
      <div className="erp-page space-y-4">
        {/* ── 顶部问候与态势横幅 ───────────────────────────────── */}
        <section className="relative overflow-hidden rounded-[20px] border border-[#E5E2DC] bg-white/95 p-5 shadow-[inset_0_1px_0_rgba(255,255,255,0.95),0_1px_2px_rgba(24,24,27,0.05),0_10px_26px_-14px_rgba(24,24,27,0.09)]">
          <div
            aria-hidden="true"
            className="pointer-events-none absolute -top-24 -right-16 h-56 w-56 rounded-full bg-emerald-200/35 blur-3xl"
          />
          <div
            aria-hidden="true"
            className="pointer-events-none absolute -bottom-28 left-1/3 h-56 w-56 rounded-full bg-amber-100/40 blur-3xl"
          />
          <div className="relative flex flex-wrap items-start justify-between gap-4">
            <div className="flex min-w-0 items-start gap-3.5">
              <span className="grid h-12 w-12 shrink-0 place-items-center rounded-2xl bg-gradient-to-br from-[#18181B] to-[#065F46] text-white shadow-lg shadow-emerald-950/20">
                <Layers className="h-6 w-6" aria-hidden="true" />
              </span>
              <div className="min-w-0">
                <h1 className="text-[22px] font-bold tracking-tight text-slate-950">
                  {bi("外贸执行总览", "Trade execution overview")}
                </h1>
                <p className="mt-1 max-w-2xl text-[13px] leading-6 text-slate-600">
                  {bi(
                    "租户隔离的实时经营态势：询盘、管道、CVP 边际毛利、商机漏斗与 AI 协同全在同一屏。",
                    "Tenant-isolated live picture: inquiries, pipeline, CVP margin, funnel and AI collaboration on one screen."
                  )}
                </p>
                <div className="mt-2.5 flex flex-wrap items-center gap-2">
                  <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-200/80 bg-emerald-50 px-2.5 py-0.5 text-[11.5px] font-semibold text-emerald-800">
                    <span className="relative flex h-1.5 w-1.5">
                      <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-500 opacity-70" />
                      <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-emerald-600" />
                    </span>
                    {bi(
                      `${enabledEmployees || "—"} 位 AI 数字员工在线`,
                      `${enabledEmployees || "—"} AI employees online`
                    )}
                  </span>
                  <span className="inline-flex items-center gap-1 rounded-full border border-slate-200 bg-slate-50 px-2.5 py-0.5 text-[11.5px] font-medium text-slate-600">
                    <ShieldCheck className="h-3 w-3 text-slate-500" aria-hidden="true" />
                    {bi("租户隔离 · RLS 强制", "Tenant-isolated · RLS forced")}
                  </span>
                  <span className="inline-flex items-center gap-1 rounded-full border border-slate-200 bg-white px-2.5 py-0.5 text-[11.5px] font-medium text-slate-600">
                    <Users className="h-3 w-3 text-slate-500" aria-hidden="true" />
                    {bi("当前角色", "Role")} {principal?.role ?? "—"}
                  </span>
                </div>
              </div>
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <Link
                href="/finance"
                className="inline-flex items-center gap-1.5 rounded-xl bg-slate-900 px-3.5 py-2 text-[12.5px] font-semibold text-white shadow-xs transition-colors hover:bg-slate-800"
              >
                <LineChart className="h-3.5 w-3.5" aria-hidden="true" />
                {bi("跳转财务对账", "Open finance")}
              </Link>
              <Link
                href="/analytics"
                className="inline-flex items-center gap-1.5 rounded-xl border border-slate-200 bg-white px-3.5 py-2 text-[12.5px] font-semibold text-slate-700 shadow-xs transition-colors hover:border-slate-300 hover:bg-slate-50"
              >
                <TrendingUp className="h-3.5 w-3.5 text-slate-500" aria-hidden="true" />
                {bi("统计分析", "Analytics")}
              </Link>
              <a
                href="/api/bff/v1/finance/cvp/export.csv"
                className="inline-flex items-center gap-1.5 rounded-xl border border-slate-200 bg-white px-3.5 py-2 text-[12.5px] font-semibold text-slate-700 shadow-xs transition-colors hover:border-slate-300 hover:bg-slate-50"
              >
                <Download className="h-3.5 w-3.5 text-slate-500" aria-hidden="true" />
                {bi("导出对账", "Export")}
              </a>
            </div>
          </div>
        </section>

        {dashboardQuery.isError && (
          <p
            role="alert"
            className="rounded-2xl border border-rose-200 bg-rose-50 px-4 py-2.5 text-[12.5px] font-medium text-rose-800"
          >
            {dashboardQuery.error.message}
          </p>
        )}

        {/* ── 4 列 Bento KPI ──────────────────────────────────── */}
        <div data-testid="dashboard-kpi-grid" className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <BentoStatCard
            label={bi("待处理询盘", "Open inquiries")}
            value={String(data?.open_inquiries ?? "—")}
            icon={Inbox}
            accent="blue"
            badge={bi("实时", "Live")}
            hint={bi("B2B / 混合模式会话待人工跟进", "B2B and mixed-mode threads awaiting a human")}
          />
          <BentoStatCard
            label={bi("B2B 管道金额", "B2B pipeline value")}
            value={formatMoney(pipelineTotal, pipelineCurrency)}
            icon={TrendingUp}
            accent="violet"
            badge={bi(`${pipelineCurrencies} 个币种`, `${pipelineCurrencies} ccy`)}
            badgeTone="violet"
            hint={bi("未关单商机预计金额（按币种汇总）", "Open opportunity value grouped by currency")}
          />

          {showFinancials ? (
            <BentoStatCard
              variant="hero"
              label={bi("净贡献毛利", "Net contribution margin")}
              value={formatMoney(netMarginTotal, primaryFinancial?.currency ?? "CNY")}
              icon={Sparkles}
              accent="emerald"
              badge={bi("已扣全成本", "Net of costs")}
              sparkline={marginSparkline.length >= 2 ? marginSparkline : undefined}
              hint={bi(
                `成本 ${formatMoney(cogsTotal, primaryFinancial?.currency ?? "CNY")} · B2C 订单 ${data?.b2c_orders ?? 0}`,
                `COGS ${formatMoney(cogsTotal, primaryFinancial?.currency ?? "CNY")} · B2C orders ${data?.b2c_orders ?? 0}`
              )}
              testId="dashboard-financials"
            />
          ) : (
            <div
              data-testid="dashboard-financials-masked"
              className="flex flex-col justify-between rounded-2xl border border-amber-200/90 bg-amber-50/70 p-5 shadow-[0_1px_3px_rgba(15,23,42,0.05)]"
            >
              <div className="flex items-start gap-2.5">
                <span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-white text-amber-600 ring-1 ring-amber-200">
                  <AlertTriangle className="h-4 w-4" aria-hidden="true" />
                </span>
                <span className="text-[13px] font-semibold text-amber-900">
                  {bi("净贡献毛利已脱敏", "Net margin masked")}
                </span>
              </div>
              <div className="mt-4 text-[26px] font-bold leading-none tracking-tight text-amber-900/60">
                • • •
              </div>
              <p className="mt-4 border-t border-amber-200/70 pt-3 text-[12px] font-medium text-amber-900">
                {bi(
                  `当前角色（${principal?.role ?? "—"}）不可查看成本与毛利，KPI 已自动脱敏。`,
                  `Your role (${principal?.role ?? "—"}) cannot view costs and margins; KPIs are masked automatically.`
                )}
              </p>
            </div>
          )}

          <BentoStatCard
            label={bi("待审 AI 提案", "Pending AI proposals")}
            value={String(data?.pending_proposals ?? "—")}
            icon={Bot}
            accent="amber"
            badge={
              (data?.pending_proposals ?? 0) > 0
                ? bi("高优待核", "High priority")
                : bi("队列已清空", "Queue clear")
            }
            ctaLabel={bi("立即审阅与审批", "Review & approve now")}
            ctaHref="/ai/proposals"
            hint={bi("L1 提案需人工确认后才回写 ERP", "L1 proposals write back only after human approval")}
          />
        </div>

        {/* ── 主分析行：8 / 4 ─────────────────────────────────── */}
        <div className="grid gap-4 lg:grid-cols-12">
          <section className="rounded-2xl border border-slate-200/90 bg-white shadow-[0_1px_3px_rgba(15,23,42,0.06),0_6px_16px_-6px_rgba(15,23,42,0.05)] lg:col-span-8">
            <div className="flex flex-wrap items-start justify-between gap-3 border-b border-slate-100 p-5">
              <div className="min-w-0">
                <h2 className="flex items-center gap-2 text-[15px] font-bold tracking-tight text-slate-950">
                  <TrendingUp className="h-4 w-4 text-blue-600" aria-hidden="true" />
                  {bi("营收与 CVP 边际毛利趋势", "Revenue & CVP margin trend")}
                </h2>
                <p className="mt-1 text-[12px] leading-5 text-slate-600">
                  {bi(
                    "逐笔订单取自订单利润快照（本位币），不做任何时间插值或模拟数据。",
                    "One point per real order profit snapshot in base currency — no interpolation, no synthetic data."
                  )}
                </p>
              </div>
              {showFinancials && (
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
              )}
            </div>

            {showFinancials ? (
              <div className="space-y-4 p-5 pt-4">
                <div className="grid gap-3 rounded-xl bg-slate-50/80 p-3.5 sm:grid-cols-3">
                  <SummaryCell
                    label={bi("营收（选定范围）", "Revenue (window)")}
                    value={formatMoney(trend.revenue, trend.baseCurrency)}
                    tone="blue"
                  />
                  <SummaryCell
                    label={bi("净贡献毛利", "Net contribution margin")}
                    value={formatMoney(trend.margin, trend.baseCurrency)}
                    suffix={`${trend.marginPct}%`}
                    tone="emerald"
                  />
                  <SummaryCell
                    label={bi("B2C 订单（累计）", "B2C orders (all time)")}
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
                  <div className="grid h-[200px] place-items-center rounded-xl border border-dashed border-slate-200 bg-slate-50/60 text-[13px] font-medium text-slate-500">
                    {trend.points.length === 1
                      ? bi(
                          "仅 1 笔订单利润快照，已展示汇总数字；至少 2 笔才能绘制趋势曲线。",
                          "Only one order snapshot so far — summary shown; two are needed for a curve."
                        )
                      : bi("暂无订单利润快照，完成首笔订单后自动出现趋势。", "No order profit snapshots yet.")}
                  </div>
                )}

                <p className="text-[11.5px] leading-5 text-slate-500">
                  {bi(
                    `数据来源：finance/overview 订单利润快照（按重算时间倒序，当前 ${trend.orders} 笔；接口单次上限 100 笔）。金额用于展示，计算全程走固定点整数，不经过浮点求和。`,
                    `Source: finance/overview order profit snapshots (recalculated_at DESC, ${trend.orders} rows, API cap 100). Amounts are display-only and summed with fixed-point integers, never float.`
                  )}
                </p>
              </div>
            ) : (
              <div
                data-testid="dashboard-trend-masked"
                className="m-5 flex items-start gap-2 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-[12.5px] font-medium text-amber-900"
              >
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" aria-hidden="true" />
                <span>
                  {bi(
                    "营收与毛利趋势仅对 Owner / Admin / Manager / Finance 开放，当前角色已脱敏。",
                    "Revenue and margin trends are limited to Owner/Admin/Manager/Finance; masked for your role."
                  )}
                </span>
              </div>
            )}
          </section>

          <section
            data-testid="dashboard-funnel"
            className="flex flex-col justify-between rounded-2xl border border-slate-200/90 bg-white p-5 shadow-[0_1px_3px_rgba(15,23,42,0.06),0_6px_16px_-6px_rgba(15,23,42,0.05)] lg:col-span-4"
          >
            <div>
              <h2 className="flex items-center gap-2 text-[15px] font-bold tracking-tight text-slate-950">
                <Target className="h-4 w-4 text-blue-600" aria-hidden="true" />
                {bi("跨境商机漏斗转化", "Cross-border opportunity funnel")}
              </h2>
              <p className="mt-1 text-[12px] leading-5 text-slate-600">
                {bi("按商机阶段统计的真实数量。", "Real counts grouped by opportunity stage.")}
              </p>
            </div>

            <div className="mt-4">
              <HorizontalFunnelBars
                items={(data?.funnel ?? []).map((stage) => ({
                  key: stage.stage,
                  label: FUNNEL_STAGE_LABEL[stage.stage]?.[locale] ?? stage.stage,
                  count: stage.count,
                  tone:
                    stage.stage === "closed_won"
                      ? "emerald"
                      : stage.stage === "closed_lost"
                        ? "slate"
                        : "blue",
                }))}
              />
              {(data?.funnel ?? []).length === 0 && (
                <p className="text-[12.5px] text-slate-500">{bi("暂无商机数据", "No opportunities yet")}</p>
              )}
            </div>

            <div className="mt-4 rounded-xl border border-emerald-100 bg-emerald-50/70 p-3.5">
              <p className="flex items-center gap-1.5 text-[12.5px] font-bold text-emerald-900">
                <Sparkles className="h-3.5 w-3.5" aria-hidden="true" />
                {bi("AI 赋能提示", "AI enablement")}
              </p>
              <p className="mt-1 text-[11.5px] leading-5 text-emerald-800">
                {bi(
                  `有 ${data?.pending_proposals ?? 0} 条 L1 提案待人工确认；批准后 AI 会自动把报价、CRM 字段与跟进任务回写 ERP，漏斗推进不再靠手工录入。`,
                  `${data?.pending_proposals ?? 0} L1 proposals await human review; approving writes quotes, CRM fields and follow-ups back to the ERP automatically.`
                )}
              </p>
            </div>
          </section>
        </div>

        {/* ── 底部 3 列 Bento ────────────────────────────────── */}
        <div className="grid gap-4 lg:grid-cols-3">
          <section
            data-testid="dashboard-channels"
            className="rounded-2xl border border-slate-200/90 bg-white p-5 shadow-[0_1px_3px_rgba(15,23,42,0.06),0_6px_16px_-6px_rgba(15,23,42,0.05)]"
          >
            <h2 className="flex items-center gap-2 text-[15px] font-bold tracking-tight text-slate-950">
              <Store className="h-4 w-4 text-blue-600" aria-hidden="true" />
              {bi("全域询盘来源", "Inquiry source mix")}
            </h2>
            <p className="mt-1 text-[12px] leading-5 text-slate-600">
              {bi("按渠道账号统计的真实会话量。", "Real conversation counts per channel account.")}
            </p>
            <div className="mt-4">
              <ChannelDonutChart
                centerValue={String(channelTotal)}
                centerLabel={bi("会话总数", "Conversations")}
                data={channelRows.map((row, index) => ({
                  name: CHANNEL_LABEL[row.channel]?.[locale] ?? row.channel,
                  value: row.conversations,
                  color: CHANNEL_COLOR[row.channel] ?? FALLBACK_COLORS[index % FALLBACK_COLORS.length],
                  meta: channelTotal
                    ? `${Math.round((row.conversations / channelTotal) * 100)}%`
                    : undefined,
                }))}
              />
              {channelRows.length === 0 && (
                <p className="mt-2 text-[12.5px] text-slate-500">
                  {bi("暂无会话", "No conversations")}
                </p>
              )}
            </div>
          </section>

          <section className="rounded-2xl border border-slate-200/90 bg-white p-5 shadow-[0_1px_3px_rgba(15,23,42,0.06),0_6px_16px_-6px_rgba(15,23,42,0.05)]">
            <h2 className="flex items-center gap-2 text-[15px] font-bold tracking-tight text-slate-950">
              <Layers className="h-4 w-4 text-blue-600" aria-hidden="true" />
              {bi("快捷工作台与 AI 协同", "Workbench & AI collaboration")}
            </h2>
            <p className="mt-1 text-[12px] leading-5 text-slate-600">
              {bi("一键进入高频业务面，全部为实时数据。", "Jump into high-frequency surfaces, all live data.")}
            </p>
            <ul className="mt-3 space-y-1.5">
              {WORKBENCH_LINKS.map(({ href, icon: Icon, accent, zh, en, descZh, descEn }) => (
                <li key={href}>
                  <Link
                    href={href}
                    className="group flex items-center gap-3 rounded-xl border border-transparent px-2 py-2 transition-colors hover:border-slate-200 hover:bg-slate-50"
                  >
                    <span className={`grid h-9 w-9 shrink-0 place-items-center rounded-xl ${accent}`}>
                      <Icon className="h-4 w-4" aria-hidden="true" />
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-[13px] font-semibold text-slate-900">
                        {bi(zh, en)}
                      </span>
                      <span className="block truncate text-[11.5px] text-slate-500">
                        {bi(descZh, descEn)}
                      </span>
                    </span>
                    <ArrowUpRight
                      className="h-3.5 w-3.5 shrink-0 text-slate-300 transition-colors group-hover:text-blue-600"
                      aria-hidden="true"
                    />
                  </Link>
                </li>
              ))}
            </ul>
          </section>

          <section
            data-testid="dashboard-tasks"
            className="flex flex-col rounded-2xl border border-slate-200/90 bg-white p-5 shadow-[0_1px_3px_rgba(15,23,42,0.06),0_6px_16px_-6px_rgba(15,23,42,0.05)]"
          >
            <div className="flex items-start justify-between gap-2">
              <div>
                <h2 className="flex items-center gap-2 text-[15px] font-bold tracking-tight text-slate-950">
                  <ClipboardList className="h-4 w-4 text-blue-600" aria-hidden="true" />
                  {bi("外贸待办与高优提醒", "Trade to-dos & alerts")}
                </h2>
                <p className="mt-1 text-[12px] leading-5 text-slate-600">
                  {bi("按到期时间排序的真实任务。", "Real tasks ordered by due date.")}
                </p>
              </div>
              {highPriorityTasks > 0 && (
                <span className="inline-flex shrink-0 items-center gap-1 rounded-full border border-rose-200/80 bg-rose-50 px-2.5 py-0.5 text-[11px] font-semibold text-rose-700">
                  {bi(`高优 ${highPriorityTasks}`, `${highPriorityTasks} urgent`)}
                </span>
              )}
            </div>

            <ul className="mt-3 flex-1 space-y-1.5">
              {(data?.upcoming_tasks ?? []).map((task) => (
                <li
                  key={task.id}
                  className="flex items-start gap-2 rounded-xl border border-slate-100 bg-slate-50/60 px-3 py-2"
                >
                  <span
                    className={`mt-1 h-1.5 w-1.5 shrink-0 rounded-full ${
                      task.priority === "high" ? "bg-rose-500" : "bg-slate-300"
                    }`}
                    aria-hidden="true"
                  />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[12.5px] font-semibold text-slate-900">
                      {task.title}
                    </span>
                    <span className="mt-0.5 block text-[11px] text-slate-500">
                      {task.priority === "high"
                        ? bi("高优先级 · 需当日处理", "High priority · handle today")
                        : bi("常规跟进", "Routine follow-up")}
                    </span>
                  </span>
                </li>
              ))}
              {(data?.upcoming_tasks ?? []).length === 0 && (
                <li className="rounded-xl border border-dashed border-slate-200 bg-slate-50/60 px-3 py-4 text-center text-[12.5px] font-medium text-slate-500">
                  {bi("今日没有待办，保持节奏。", "No tasks today — keep the momentum.")}
                </li>
              )}
            </ul>

            <Link
              href="/tasks"
              className="mt-3 inline-flex items-center justify-center gap-1.5 rounded-xl border border-slate-200 bg-white px-3 py-2 text-[12.5px] font-semibold text-slate-800 shadow-xs transition-colors hover:border-slate-300 hover:bg-slate-50"
            >
              {bi("查看全部任务", "View all tasks")}
              <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
            </Link>
          </section>
        </div>
      </div>
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
    tone === "blue" ? "text-slate-950" : tone === "emerald" ? "text-emerald-700" : "text-slate-900";
  return (
    <div className="min-w-0">
      <p className="truncate text-[11.5px] font-medium text-slate-500">{label}</p>
      <p className={`mt-1 flex flex-wrap items-baseline gap-1.5 text-[17px] font-bold tabular-nums ${valueTone}`}>
        <span className="truncate">{value}</span>
        {suffix && (
          <span className="rounded-full bg-emerald-50 px-1.5 py-0.2 text-[11px] font-semibold text-emerald-700">
            {suffix}
          </span>
        )}
      </p>
    </div>
  );
}
