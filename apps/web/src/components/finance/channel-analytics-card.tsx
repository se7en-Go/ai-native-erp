"use client";

import React, { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { BarChart3 } from "lucide-react";

import {
  fetchChannelAnalyticsOverview,
  type AnalyticsWindow,
} from "@/lib/api-client/pack-a-erp";
import { formatMoney } from "@/lib/contracts";
import { useI18n } from "@/lib/i18n";

export function ChannelAnalyticsCard({
  enabled = true,
}: {
  enabled?: boolean;
}) {
  const { bi } = useI18n();
  const [windowVal, setWindowVal] = useState<AnalyticsWindow>("30d");

  const query = useQuery({
    queryKey: ["finance", "channel-analytics", windowVal],
    enabled,
    queryFn: () => fetchChannelAnalyticsOverview(windowVal),
  });

  if (!enabled) {
    return null;
  }

  const data = query.data;
  const currency = data?.base_currency ?? "CNY";
  const totals = data?.totals;
  const channels = data?.channels ?? [];

  return (
    <section
      data-testid="channel-analytics-card"
      className="erp-card overflow-hidden rounded-[20px] border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900"
    >
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 bg-slate-50/70 px-4 py-3 dark:border-slate-800 dark:bg-slate-800/40">
        <div className="flex items-center gap-2">
          <BarChart3 className="h-4 w-4 text-blue-600" />
          <div>
            <h2 className="text-[14px] font-semibold text-slate-900 dark:text-slate-100">
              {bi("全平台销量 & 毛利分布", "Multi-platform sales & margin analytics")}
            </h2>
            <p className="text-[11.5px] text-slate-500 dark:text-slate-400">
              {bi(
                "按渠道拆解 GMV、退款率、佣金、广告投放、净贡献毛利与 ROAS（全部 Decimal 精度）。",
                "Per-channel GMV, refund rate, commission, ad spend, net margin and ROAS (Decimal precision)."
              )}
            </p>
          </div>
        </div>

        <div
          role="group"
          aria-label={bi("分析时间窗口", "Analytics window")}
          className="inline-flex rounded-xl border border-slate-200 bg-white p-0.5 dark:border-slate-700 dark:bg-slate-900"
        >
          {(["7d", "30d", "90d"] as const).map((win) => (
            <button
              key={win}
              type="button"
              data-testid={`analytics-window-${win}`}
              onClick={() => setWindowVal(win)}
              className={`rounded-lg px-2.5 py-1 text-[11.5px] font-semibold transition-colors ${
                windowVal === win
                  ? "bg-blue-600 text-white"
                  : "text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800"
              }`}
            >
              {win.toUpperCase()}
            </button>
          ))}
        </div>
      </div>

      {totals && (
        <div className="grid gap-2.5 border-b border-slate-100 bg-slate-50/40 px-4 py-3 sm:grid-cols-5 dark:border-slate-800 dark:bg-slate-800/20">
          <div>
            <div className="text-[11px] font-medium text-slate-500">{bi("全渠道 GMV", "Total GMV")}</div>
            <div data-testid="analytics-total-gmv" className="text-[14px] font-bold tabular-nums text-slate-900 dark:text-slate-100">
              {formatMoney(totals.gmv_base, currency)}
            </div>
          </div>
          <div>
            <div className="text-[11px] font-medium text-slate-500">{bi("总销量", "Units sold")}</div>
            <div data-testid="analytics-total-units" className="text-[14px] font-bold tabular-nums text-slate-900 dark:text-slate-100">
              {totals.units_sold} ({totals.order_count} {bi("单", "orders")})
            </div>
          </div>
          <div>
            <div className="text-[11px] font-medium text-slate-500">{bi("净贡献毛利", "Net margin")}</div>
            <div data-testid="analytics-total-margin" className="text-[14px] font-bold tabular-nums text-emerald-700 dark:text-emerald-300">
              {formatMoney(totals.net_margin_base, currency)} ({totals.margin_pct}%)
            </div>
          </div>
          <div>
            <div className="text-[11px] font-medium text-slate-500">{bi("广告费用", "Ad spend")}</div>
            <div className="text-[14px] font-bold tabular-nums text-slate-900 dark:text-slate-100">
              {formatMoney(totals.ad_spend_base, currency)}
            </div>
          </div>
          <div>
            <div className="text-[11px] font-medium text-slate-500">ROAS</div>
            <div data-testid="analytics-total-roas" className="text-[14px] font-bold tabular-nums text-blue-700 dark:text-blue-300">
              {totals.roas ? `${totals.roas}x` : "—"}
            </div>
          </div>
        </div>
      )}

      <div className="overflow-x-auto">
        <table data-testid="channel-analytics-table" className="erp-table">
          <thead>
            <tr>
              <th>{bi("渠道与店铺", "Channel")}</th>
              <th className="text-right">{bi("订单 / 销量", "Orders / units")}</th>
              <th className="text-right">GMV</th>
              <th className="text-right">{bi("退款（率）", "Refunds")}</th>
              <th className="text-right">{bi("佣金 / 广告", "Commission / ads")}</th>
              <th className="text-right">{bi("净毛利（率）", "Net margin")}</th>
              <th className="text-right">ROAS</th>
            </tr>
          </thead>
          <tbody>
            {channels.map((row) => (
              <tr key={row.channel_key} data-testid={`analytics-row-${row.channel_type}`}>
                <td className="py-2">
                  <div className="text-[12px] font-semibold text-slate-900 dark:text-slate-100">
                    {row.channel_label}
                  </div>
                  <div className="font-mono text-[11px] text-slate-500 dark:text-slate-400">
                    {row.channel_type}
                  </div>
                </td>
                <td className="py-2 text-right font-mono text-[12px] tabular-nums">
                  {row.order_count} / {row.units_sold}
                </td>
                <td className="py-2 text-right font-mono text-[12px] font-semibold tabular-nums">
                  {formatMoney(row.gmv_base, currency)}
                </td>
                <td className="py-2 text-right font-mono text-[12px] tabular-nums">
                  {formatMoney(row.refunded_amount_base, currency)} ({row.refund_rate_pct}%)
                </td>
                <td className="py-2 text-right font-mono text-[11.5px] tabular-nums text-slate-600 dark:text-slate-400">
                  {formatMoney(row.platform_commission_base, currency)} /{" "}
                  {formatMoney(row.ad_spend_base, currency)}
                </td>
                <td className="py-2 text-right font-mono text-[12px] font-semibold tabular-nums text-emerald-700 dark:text-emerald-300">
                  {formatMoney(row.net_margin_base, currency)} ({row.margin_pct}%)
                </td>
                <td className="py-2 text-right font-mono text-[12px] font-semibold tabular-nums text-blue-700 dark:text-blue-300">
                  {row.roas ? `${row.roas}x` : "—"}
                </td>
              </tr>
            ))}
            {channels.length === 0 && (
              <tr>
                <td colSpan={7} className="py-5 text-center text-[12px] text-slate-500 dark:text-slate-400">
                  {query.isLoading
                    ? bi("正在统计跨平台经营数据…", "Loading channel analytics…")
                    : bi("选定窗口内暂无跨平台订单或广告数据。", "No channel orders or ad metrics in this window yet.")}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
