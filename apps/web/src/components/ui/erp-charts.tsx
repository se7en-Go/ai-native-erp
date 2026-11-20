"use client";

import React from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Cell,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

export interface TrendPoint {
  label: string;
  revenue: number;
  margin: number;
}

/**
 * 21st.dev + Stitch 风格平滑双曲线面积图（营收 vs CVP 边际毛利）。
 */
export function RevenueMarginAreaChart({
  data,
  revenueLabel = "总营收",
  marginLabel = "净贡献毛利",
  currencyPrefix = "$",
}: {
  data: TrendPoint[];
  revenueLabel?: string;
  marginLabel?: string;
  currencyPrefix?: string;
}) {
  return (
    <div className="h-[260px] w-full min-w-0">
      <ResponsiveContainer width="100%" height="100%" minWidth={240} minHeight={220}>
        <AreaChart data={data} margin={{ top: 10, right: 12, left: 0, bottom: 0 }}>
          <defs>
            <linearGradient id="erpRevenueGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor="#2563EB" stopOpacity={0.2} />
              <stop offset="95%" stopColor="#2563EB" stopOpacity={0.01} />
            </linearGradient>
            <linearGradient id="erpMarginGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor="#10B981" stopOpacity={0.26} />
              <stop offset="95%" stopColor="#10B981" stopOpacity={0.02} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#E5E7EB" strokeOpacity={0.65} />
          <XAxis
            dataKey="label"
            axisLine={false}
            tickLine={false}
            tick={{ fill: "#6B7280", fontSize: 12 }}
            dy={8}
          />
          <YAxis
            axisLine={false}
            tickLine={false}
            tick={{ fill: "#6B7280", fontSize: 12 }}
            tickFormatter={(v: number) =>
              v >= 1000 ? `${currencyPrefix}${(v / 1000).toFixed(0)}k` : `${currencyPrefix}${v}`
            }
            width={52}
          />
          <Tooltip
            contentStyle={{
              backgroundColor: "#111827",
              borderColor: "#1F2937",
              borderRadius: "10px",
              color: "#F9FAFB",
              fontSize: "12px",
              boxShadow: "0 12px 30px rgba(15, 23, 42, 0.28)",
            }}
            itemStyle={{ color: "#F9FAFB", fontWeight: 600 }}
            labelStyle={{ color: "#9CA3AF", marginBottom: "4px" }}
            formatter={(value, name) => [
              `${currencyPrefix}${Number(value ?? 0).toLocaleString("en-US", {
                minimumFractionDigits: 2,
                maximumFractionDigits: 2,
              })}`,
              name === "revenue" ? revenueLabel : marginLabel,
            ]}
          />
          <Area
            type="monotone"
            dataKey="revenue"
            name="revenue"
            stroke="#2563EB"
            strokeWidth={2.4}
            fill="url(#erpRevenueGrad)"
            activeDot={{ r: 5, fill: "#2563EB", stroke: "#fff", strokeWidth: 2 }}
          />
          <Area
            type="monotone"
            dataKey="margin"
            name="margin"
            stroke="#10B981"
            strokeWidth={2.4}
            fill="url(#erpMarginGrad)"
            activeDot={{ r: 5, fill: "#10B981", stroke: "#fff", strokeWidth: 2 }}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

export interface DonutSlice {
  name: string;
  value: number;
  color: string;
  meta?: string;
}

/**
 * 21st.dev 中心数据甜甜圈图 + 语义圆点图例。
 */
export function ChannelDonutChart({
  data,
  centerValue,
  centerLabel,
}: {
  data: DonutSlice[];
  centerValue: string;
  centerLabel: string;
}) {
  const safeData =
    data.length > 0 && data.some((d) => d.value > 0)
      ? data
      : [{ name: "暂无数据", value: 1, color: "#E2E8F0" }];

  return (
    <div className="flex flex-col gap-4">
      <div className="relative mx-auto h-[176px] w-[176px]">
        <ResponsiveContainer width="100%" height="100%" minWidth={160} minHeight={160}>
          <PieChart>
            <Pie
              data={safeData}
              dataKey="value"
              nameKey="name"
              cx="50%"
              cy="50%"
              innerRadius={56}
              outerRadius={78}
              paddingAngle={3}
              stroke="none"
            >
              {safeData.map((entry, idx) => (
                <Cell key={`${entry.name}-${idx}`} fill={entry.color} />
              ))}
            </Pie>
            <Tooltip
              contentStyle={{
                backgroundColor: "#0F172A",
                borderColor: "#1E293B",
                borderRadius: "10px",
                color: "#F8FAFC",
                fontSize: "12px",
              }}
            />
          </PieChart>
        </ResponsiveContainer>
        <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center text-center">
          <span className="text-[20px] font-bold tabular-nums text-slate-950">{centerValue}</span>
          <span className="text-[11px] font-medium text-slate-600">{centerLabel}</span>
        </div>
      </div>

      <ul className="space-y-2 text-[12.5px]">
        {data.map((slice) => (
          <li
            key={slice.name}
            className="flex items-center justify-between gap-2 rounded-lg px-2 py-1 hover:bg-slate-50"
          >
            <span className="flex min-w-0 items-center gap-2">
              <span
                className="h-2.5 w-2.5 shrink-0 rounded-full"
                style={{ backgroundColor: slice.color }}
              />
              <span className="truncate font-medium text-slate-800">{slice.name}</span>
            </span>
            <span className="flex shrink-0 items-center gap-2 tabular-nums">
              <span className="font-bold text-slate-950">{slice.value}</span>
              {slice.meta && <span className="text-[11.5px] text-slate-500">{slice.meta}</span>}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export interface FunnelItem {
  key: string;
  label: string;
  count: number;
  sublabel?: string;
  tone?: "blue" | "emerald" | "slate";
}

/**
 * 21st.dev + Stitch 水平转化漏斗进度条。
 */
export function HorizontalFunnelBars({ items }: { items: FunnelItem[] }) {
  const max = Math.max(...items.map((i) => i.count), 1);

  return (
    <div className="space-y-3">
      {items.map((item) => {
        const pct = item.count > 0 ? Math.max(12, Math.round((item.count / max) * 100)) : 4;
        const barColor =
          item.tone === "emerald"
            ? "bg-gradient-to-r from-[#065F46] to-[#10B981]"
            : item.tone === "slate"
              ? "bg-stone-400"
              : "bg-gradient-to-r from-[#18181B] to-[#3F3F46]";

        return (
          <div key={item.key} className="space-y-1.5">
            <div className="flex items-center justify-between gap-2 text-[12.5px]">
              <span className="font-semibold text-slate-800">{item.label}</span>
              <span className="tabular-nums font-bold text-slate-950">
                {item.count}
                {item.sublabel && (
                  <span className="ml-1.5 font-normal text-slate-500">{item.sublabel}</span>
                )}
              </span>
            </div>
            <div className="h-2.5 w-full overflow-hidden rounded-full bg-slate-100">
              <div
                className={`h-full rounded-full transition-all duration-500 ${barColor}`}
                style={{ width: `${pct}%` }}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}

/**
 * CVP 订单成本与净毛利比例堆叠条。
 */
export function CvpWaterfallBar({
  grossRevenue,
  cogs,
  feesAndLogistics,
  netMargin,
}: {
  grossRevenue: number;
  cogs: number;
  feesAndLogistics: number;
  netMargin: number;
}) {
  const denom = Math.max(grossRevenue, 0.01);
  const cogsPct = Math.min(100, Math.max(0, (cogs / denom) * 100));
  const feePct = Math.min(100 - cogsPct, Math.max(0, (feesAndLogistics / denom) * 100));
  const marginPct = Math.max(0, 100 - cogsPct - feePct);

  return (
    <div className="space-y-1.5">
      <div className="flex h-3 w-full overflow-hidden rounded-full bg-slate-100">
        <div
          className="h-full bg-slate-700 transition-all duration-500"
          style={{ width: `${cogsPct}%` }}
          title={`COGS ${cogsPct.toFixed(1)}%`}
        />
        <div
          className="h-full bg-amber-500 transition-all duration-500"
          style={{ width: `${feePct}%` }}
          title={`费用与物流 ${feePct.toFixed(1)}%`}
        />
        <div
          className={`h-full transition-all duration-500 ${
            netMargin >= 0 ? "bg-emerald-500" : "bg-rose-500"
          }`}
          style={{ width: `${marginPct}%` }}
          title={`净贡献毛利 ${marginPct.toFixed(1)}%`}
        />
      </div>
      <div className="flex flex-wrap items-center gap-4 text-[11.5px] font-medium text-slate-600 tabular-nums">
        <span className="inline-flex items-center gap-1.5">
          <span className="h-2 w-2 rounded-full bg-slate-700" />
          COGS {cogsPct.toFixed(1)}%
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="h-2 w-2 rounded-full bg-amber-500" />
          费用与物流 {feePct.toFixed(1)}%
        </span>
        <span className="inline-flex items-center gap-1.5 text-emerald-700">
          <span className="h-2 w-2 rounded-full bg-emerald-500" />
          净贡献毛利 {marginPct.toFixed(1)}%
        </span>
      </div>
    </div>
  );
}
