"use client";

import React, { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  BarChart3,
  Inbox,
  MessagesSquare,
  Percent,
  ShieldAlert,
  Target,
  TrendingUp,
} from "lucide-react";

import { AppShell } from "@/components/app-shell";
import { useAuthSession } from "@/components/providers";
import { BentoStatCard } from "@/components/ui/bento-stat-card";
import { SegmentedTabs } from "@/components/ui/segmented-tabs";
import { ChannelDonutChart, HorizontalFunnelBars, RevenueMarginAreaChart } from "@/components/ui/erp-charts";
import { buildCvpTrend, type CvpTrendWindow } from "@/components/finance/revenue-trend";
import { isPrivilegedCostViewer } from "@/lib/auth-cookie";
import { formatMoney } from "@/lib/contracts";
import { fetchFinanceOverview, fetchPendingProposalsCount } from "@/lib/data-source";
import { fetchPhase3aDashboard, fetchPhase3aOverview } from "@/lib/phase3a-data";
import { useI18n } from "@/lib/i18n";

const FUNNEL_STAGE_LABEL: Record<string, { zh: string; en: string }> = {
  discovery: { zh: "需求挖掘", en: "Discovery" },
  sampling: { zh: "寄样确认", en: "Sampling" },
  quoting: { zh: "报价中", en: "Quoting" },
  negotiating: { zh: "商务谈判", en: "Negotiating" },
  closed_won: { zh: "已赢单", en: "Closed won" },
  closed_lost: { zh: "已输单", en: "Closed lost" },
};

const CHANNEL_COLOR: Record<string, string> = {
  web_livechat: "#2563EB",
  whatsapp: "#10B981",
  email: "#7C3AED",
  facebook_messenger: "#0EA5E9",
  instagram_dm: "#E11D48",
  telegram: "#F59E0B",
  shopify: "#0F172A",
};

const FALLBACK_COLORS = ["#2563EB", "#10B981", "#7C3AED", "#F59E0B", "#0EA5E9", "#E11D48"];

export default function AnalyticsPage() {
  const { bi, locale } = useI18n();
  const { principal } = useAuthSession();
  const showMargins = isPrivilegedCostViewer(principal?.role);
  const [trendWindow, setTrendWindow] = useState<CvpTrendWindow>("recent5");

  const proposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () => fetchPendingProposalsCount(),
  });
  const overviewQuery = useQuery({
    queryKey: ["analytics", "overview"],
    queryFn: () => fetchPhase3aOverview(),
  });
  const dashboardQuery = useQuery({
    queryKey: ["analytics", "dashboard"],
    queryFn: () => fetchPhase3aDashboard(),
  });
  const financeQuery = useQuery({
    queryKey: ["finance", "overview"],
    enabled: showMargins,
    queryFn: () => fetchFinanceOverview(),
  });

  const overview = overviewQuery.data;
  const totalChannel = (overview?.channels ?? []).reduce((sum, row) => sum + row.conversations, 0);
  const funnel = overview?.funnel ?? [];
  const funnelTotal = funnel.reduce((sum, stage) => sum + stage.count, 0);
  const wonCount = funnel.find((stage) => stage.stage === "closed_won")?.count ?? 0;

  const snapshots = showMargins ? (financeQuery.data?.cvp_snapshots ?? []) : [];
  const trend = buildCvpTrend(snapshots, trendWindow);

  return (
    <AppShell currentPath="/analytics" pendingProposalsCount={proposalsQuery.data?.total ?? 0}>
      <div className="erp-page space-y-4">
        {/* ── 页头 ─────────────────────────────────────────────── */}
        <header className="flex flex-wrap items-end justify-between gap-3">
          <div className="min-w-0">
            <h1 className="flex items-center gap-2 text-[22px] font-bold tracking-tight text-slate-950">
              <span className="grid h-9 w-9 place-items-center rounded-xl bg-blue-50 text-blue-600 ring-1 ring-blue-100">
                <BarChart3 className="h-4.5 w-4.5" aria-hidden="true" />
              </span>
              {bi("统计分析", "Analytics")}
            </h1>
            <p className="mt-1.5 max-w-3xl text-[13px] leading-6 text-slate-600">
              {bi(
                "渠道构成、B2B 商机漏斗、CVP 边际毛利与 SKU 毛利表现；成本与毛利对业务员角色自动脱敏，所有数字均来自真实接口。",
                "Channel mix, B2B opportunity funnel, CVP contribution margin and SKU performance; costs and margins are masked for sales agents and every number comes from a real endpoint."
              )}
            </p>
          </div>
          <span className="inline-flex items-center gap-1.5 rounded-full border border-slate-200 bg-white px-3 py-1 text-[11.5px] font-semibold text-slate-600 shadow-xs">
            <TrendingUp className="h-3.5 w-3.5 text-emerald-600" aria-hidden="true" />
            {bi("商业 BI 仪表盘", "Business BI dashboard")}
          </span>
        </header>

        {overviewQuery.isError && (
          <p
            role="alert"
            className="rounded-2xl border border-rose-200 bg-rose-50 px-4 py-2.5 text-[12.5px] font-medium text-rose-800"
          >
            {overviewQuery.error.message}
          </p>
        )}

        {/* ── KPI 行 ───────────────────────────────────────────── */}
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <BentoStatCard
            label={bi("商机总量", "Total opportunities")}
            value={String(funnelTotal)}
            icon={Target}
            accent="blue"
            badge={bi(`${funnel.length} 个阶段`, `${funnel.length} stages`)}
            hint={bi("覆盖从需求挖掘到输单的全部阶段", "From discovery through to lost deals")}
          />
          <BentoStatCard
            label={bi("赢单转化率", "Win rate")}
            value={`${overview?.won_conversion_pct ?? "—"}%`}
            icon={Percent}
            accent="emerald"
            badge={bi(`已赢单 ${wonCount}`, `${wonCount} won`)}
            hint={bi("赢单数 ÷ 全部商机数（后端 Decimal 计算）", "Won ÷ all opportunities, computed with Decimal")}
            testId="analytics-kpi-conversion"
          />
          <BentoStatCard
            label={bi("渠道会话总数", "Total conversations")}
            value={String(totalChannel)}
            icon={MessagesSquare}
            accent="violet"
            badge={bi(`${overview?.channels?.length ?? 0} 个渠道`, `${overview?.channels?.length ?? 0} channels`)}
            badgeTone="violet"
            hint={bi("跨邮件、WhatsApp、在线客服等全渠道", "Email, WhatsApp, live chat and more")}
          />
          <BentoStatCard
            label={bi("待处理询盘", "Open inquiries")}
            value={String(dashboardQuery.data?.open_inquiries ?? "—")}
            icon={Inbox}
            accent="amber"
            badge={bi("实时", "Live")}
            hint={bi("B2B / 混合模式会话待人工跟进", "B2B and mixed-mode threads awaiting a human")}
          />
        </div>

        {/* ── 趋势 + 渠道甜甜圈 ────────────────────────────────── */}
        <div className="grid gap-4 lg:grid-cols-12">
          <section className="rounded-2xl border border-slate-200/90 bg-white shadow-[0_1px_3px_rgba(15,23,42,0.06),0_6px_16px_-6px_rgba(15,23,42,0.05)] lg:col-span-8">
            <div className="flex flex-wrap items-start justify-between gap-3 border-b border-slate-100 p-5">
              <div className="min-w-0">
                <h2 className="flex items-center gap-2 text-[15px] font-bold tracking-tight text-slate-950">
                  <TrendingUp className="h-4 w-4 text-blue-600" aria-hidden="true" />
                  {bi("逐单营收与净贡献毛利", "Revenue & net margin per order")}
                </h2>
                <p className="mt-1 text-[12px] leading-5 text-slate-600">
                  {bi(
                    "每个点是一笔真实订单的利润快照（本位币），不做时间插值。",
                    "Each point is one real order profit snapshot in base currency — no time interpolation."
                  )}
                </p>
              </div>
              {showMargins && (
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

            {showMargins ? (
              <div className="space-y-3 p-5 pt-4">
                <div className="flex flex-wrap items-center gap-4 rounded-xl bg-slate-50/80 px-3.5 py-2.5 text-[12.5px]">
                  <span className="inline-flex items-center gap-1.5 font-medium text-slate-600">
                    <span className="h-2 w-2 rounded-full bg-blue-600" />
                    {bi("营收", "Revenue")}
                    <b className="tabular-nums text-slate-950">
                      {formatMoney(trend.revenue, trend.baseCurrency)}
                    </b>
                  </span>
                  <span className="inline-flex items-center gap-1.5 font-medium text-slate-600">
                    <span className="h-2 w-2 rounded-full bg-emerald-500" />
                    {bi("净贡献毛利", "Net margin")}
                    <b className="tabular-nums text-emerald-700">
                      {formatMoney(trend.margin, trend.baseCurrency)}
                    </b>
                    <span className="rounded-full bg-emerald-50 px-1.5 py-0.2 text-[11px] font-semibold text-emerald-700">
                      {trend.marginPct}%
                    </span>
                  </span>
                  <span className="ml-auto text-[11.5px] text-slate-500">
                    {bi(`${trend.orders} 笔订单快照`, `${trend.orders} order snapshots`)}
                  </span>
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
                  <div className="grid h-[200px] place-items-center rounded-xl border border-dashed border-slate-200 bg-slate-50/60 text-center text-[12.5px] font-medium text-slate-500">
                    {bi(
                      "订单利润快照不足 2 笔，暂时无法绘制趋势曲线。",
                      "Fewer than two order snapshots — not enough to draw a curve."
                    )}
                  </div>
                )}
              </div>
            ) : (
              <div
                data-testid="analytics-trend-masked"
                className="m-5 flex items-start gap-2 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-[12.5px] font-medium text-amber-900"
              >
                <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" aria-hidden="true" />
                <span>
                  {bi(
                    "营收与毛利趋势仅对 Owner / Admin / Manager / Finance 开放。",
                    "Revenue and margin trends are limited to Owner/Admin/Manager/Finance."
                  )}
                </span>
              </div>
            )}
          </section>

          <section className="rounded-2xl border border-slate-200/90 bg-white p-5 shadow-[0_1px_3px_rgba(15,23,42,0.06),0_6px_16px_-6px_rgba(15,23,42,0.05)] lg:col-span-4">
            <h2 className="text-[15px] font-bold tracking-tight text-slate-950">
              {bi("渠道分布", "Channel mix")}
            </h2>
            <p className="mt-1 text-[12px] leading-5 text-slate-600">
              {bi("会话量占比（真实渠道统计）", "Share of conversations per channel")}
            </p>
            <div data-testid="analytics-channels" className="mt-4">
              <ChannelDonutChart
                centerValue={String(totalChannel)}
                centerLabel={bi("会话总数", "Conversations")}
                data={(overview?.channels ?? []).map((row, index) => ({
                  name: row.channel,
                  value: row.conversations,
                  color: CHANNEL_COLOR[row.channel] ?? FALLBACK_COLORS[index % FALLBACK_COLORS.length],
                  meta: totalChannel
                    ? `${Math.round((row.conversations / totalChannel) * 100)}%`
                    : undefined,
                }))}
              />
            </div>
          </section>
        </div>

        {/* ── 漏斗 + SKU 毛利 ─────────────────────────────────── */}
        <div className="grid gap-4 lg:grid-cols-12">
          <section className="rounded-2xl border border-slate-200/90 bg-white p-5 shadow-[0_1px_3px_rgba(15,23,42,0.06),0_6px_16px_-6px_rgba(15,23,42,0.05)] lg:col-span-5">
            <div className="flex items-start justify-between gap-2">
              <div>
                <h2 className="flex items-center gap-2 text-[15px] font-bold tracking-tight text-slate-950">
                  <Target className="h-4 w-4 text-blue-600" aria-hidden="true" />
                  {bi("B2B 漏斗转化", "B2B funnel conversion")}
                </h2>
                <p className="mt-1 text-[12px] leading-5 text-slate-600">
                  {bi("阶段数量与赢单转化率", "Stage counts and win conversion")}
                </p>
              </div>
              <span
                data-testid="analytics-conversion"
                className="inline-flex shrink-0 items-center gap-1 rounded-full border border-emerald-200/80 bg-emerald-50 px-2.5 py-0.5 text-[11.5px] font-semibold tabular-nums text-emerald-800"
              >
                <Percent className="h-3 w-3" aria-hidden="true" />
                {bi("赢单转化率", "Win rate")} {overview?.won_conversion_pct ?? "—"}%
              </span>
            </div>

            <div data-testid="analytics-funnel" className="mt-4">
              <HorizontalFunnelBars
                items={funnel.map((stage) => ({
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
              {funnel.length === 0 && (
                <p className="text-[12.5px] text-slate-500">{bi("暂无商机数据", "No opportunities yet")}</p>
              )}
            </div>
          </section>

          <section className="rounded-2xl border border-slate-200/90 bg-white shadow-[0_1px_3px_rgba(15,23,42,0.06),0_6px_16px_-6px_rgba(15,23,42,0.05)] lg:col-span-7">
            <div className="flex flex-wrap items-start justify-between gap-2 border-b border-slate-100 p-5">
              <div>
                <h2 className="text-[15px] font-bold tracking-tight text-slate-950">
                  {bi("SKU 毛利分析", "SKU margin analysis")}
                </h2>
                <p className="mt-1 text-[12px] leading-5 text-slate-600">
                  {bi("按销量、营收、成本与毛利拆解到 SKU", "Units, revenue, cost and margin per SKU")}
                </p>
              </div>
              {showMargins && (
                <span className="inline-flex shrink-0 items-center gap-1 rounded-full border border-slate-200 bg-slate-50 px-2.5 py-0.5 text-[11px] font-semibold text-slate-600">
                  {bi(`${overview?.sku_margins?.length ?? 0} 个 SKU`, `${overview?.sku_margins?.length ?? 0} SKUs`)}
                </span>
              )}
            </div>

            {showMargins ? (
              <div className="overflow-x-auto">
                <table data-testid="analytics-sku-margins" className="erp-table">
                  <thead>
                    <tr>
                      <th>SKU</th>
                      <th className="text-right">{bi("件数", "Units")}</th>
                      <th className="text-right">{bi("收入", "Revenue")}</th>
                      <th className="text-right">{bi("成本", "Cost")}</th>
                      <th className="text-right">{bi("毛利", "Margin")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(overview?.sku_margins ?? []).map((row) => (
                      <tr key={`${row.variant_id}-${row.currency}`}>
                        <td className="font-semibold text-slate-900">{row.sku}</td>
                        <td className="text-right tabular-nums text-slate-700">{row.units}</td>
                        <td className="text-right tabular-nums text-slate-800">
                          {formatMoney(row.revenue, row.currency)}
                        </td>
                        <td className="text-right tabular-nums text-slate-600">
                          {formatMoney(row.cost, row.currency)}
                        </td>
                        <td className="text-right tabular-nums">
                          <span className="inline-flex items-center gap-1 rounded-full border border-emerald-200/80 bg-emerald-50 px-2 py-0.5 text-[11.5px] font-bold text-emerald-800">
                            {formatMoney(row.gross_margin, row.currency)}
                          </span>
                        </td>
                      </tr>
                    ))}
                    {(overview?.sku_margins ?? []).length === 0 && (
                      <tr>
                        <td colSpan={5} className="py-6 text-center text-[12.5px] text-slate-500">
                          {bi("暂无毛利数据", "No margin data")}
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="p-5">
                <p
                  data-testid="analytics-margins-masked"
                  className="flex items-start gap-2 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-[12.5px] font-medium text-amber-900"
                >
                  <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" aria-hidden="true" />
                  {bi(
                    `当前角色（${principal?.role}）不可查看成本与毛利明细。`,
                    `Your role (${principal?.role}) cannot view cost and margin detail.`
                  )}
                </p>
              </div>
            )}
          </section>
        </div>
      </div>
    </AppShell>
  );
}
