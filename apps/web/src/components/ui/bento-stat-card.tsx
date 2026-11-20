"use client";

import React from "react";
import Link from "next/link";
import { ArrowRight } from "lucide-react";

export type BentoAccent = "blue" | "emerald" | "amber" | "rose" | "violet" | "slate";

const ACCENT_ICON_BG: Record<BentoAccent, string> = {
  blue: "bg-blue-50 text-blue-600 ring-1 ring-blue-100",
  emerald: "bg-emerald-50 text-emerald-600 ring-1 ring-emerald-100",
  amber: "bg-amber-50 text-amber-600 ring-1 ring-amber-100",
  rose: "bg-rose-50 text-rose-600 ring-1 ring-rose-100",
  violet: "bg-violet-50 text-violet-600 ring-1 ring-violet-100",
  slate: "bg-slate-100 text-slate-700 ring-1 ring-slate-200",
};

const ACCENT_BADGE: Record<BentoAccent, string> = {
  blue: "bg-blue-50 text-blue-700 border-blue-200/80",
  emerald: "bg-emerald-50 text-emerald-700 border-emerald-200/80",
  amber: "bg-amber-50 text-amber-800 border-amber-200/80",
  rose: "bg-rose-50 text-rose-700 border-rose-200/80",
  violet: "bg-violet-50 text-violet-700 border-violet-200/80",
  slate: "bg-slate-100 text-slate-700 border-slate-200/80",
};

const ACCENT_SPARK: Record<BentoAccent, string> = {
  blue: "#2563EB",
  emerald: "#10B981",
  amber: "#D97706",
  rose: "#E11D48",
  violet: "#7C3AED",
  slate: "#475569",
};

function MiniSparkline({ points, color }: { points: number[]; color: string }) {
  if (points.length < 2) return null;
  const min = Math.min(...points);
  const max = Math.max(...points);
  const span = max - min || 1;
  const w = 88;
  const h = 30;
  const coords = points.map((v, idx) => {
    const x = (idx / (points.length - 1)) * w;
    const y = h - 3 - ((v - min) / span) * (h - 6);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const d = `M ${coords.join(" L ")}`;
  const area = `${d} L ${w},${h} L 0,${h} Z`;
  const gradId = `spark-${color.replace("#", "")}-${points.length}`;

  return (
    <svg
      viewBox={`0 0 ${w} ${h}`}
      className="h-8 w-[88px] shrink-0 overflow-visible"
      aria-hidden="true"
    >
      <defs>
        <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.22" />
          <stop offset="100%" stopColor={color} stopOpacity="0.0" />
        </linearGradient>
      </defs>
      <path d={area} fill={`url(#${gradId})`} />
      <path
        d={d}
        fill="none"
        stroke={color}
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/**
 * 21st.dev + Stitch 风格 Bento 指标卡：
 * - 柔和着色图标徽章 + 状态/增长胶囊 + 可选迷你趋势折线 + 可选底部操作 CTA。
 */
export function BentoStatCard({
  label,
  value,
  badge,
  badgeTone,
  hint,
  icon: Icon,
  accent = "blue",
  sparkline,
  ctaLabel,
  ctaHref,
  testId,
}: {
  label: string;
  value: React.ReactNode;
  badge?: React.ReactNode;
  badgeTone?: BentoAccent;
  hint?: React.ReactNode;
  icon?: React.ComponentType<{ className?: string }>;
  accent?: BentoAccent;
  sparkline?: number[];
  ctaLabel?: string;
  ctaHref?: string;
  testId?: string;
}) {
  const pillTone = badgeTone ?? accent;

  return (
    <div
      data-testid={testId}
      className="group relative flex flex-col justify-between rounded-2xl border border-slate-200/90 bg-white p-5 shadow-[0_1px_3px_rgba(15,23,42,0.06),0_6px_16px_-6px_rgba(15,23,42,0.05)] transition-all duration-200 hover:-translate-y-0.5 hover:border-slate-300 hover:shadow-[0_4px_14px_rgba(15,23,42,0.08)]"
    >
      <div className="flex items-start justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2.5">
          {Icon && (
            <span
              className={`grid h-9 w-9 shrink-0 place-items-center rounded-xl ${ACCENT_ICON_BG[accent]}`}
            >
              <Icon className="h-4 w-4" />
            </span>
          )}
          <span className="truncate text-[13px] font-semibold text-slate-700">{label}</span>
        </div>
        {badge && (
          <span
            className={`inline-flex shrink-0 items-center gap-1 rounded-full border px-2.5 py-0.5 text-[11px] font-semibold tabular-nums ${ACCENT_BADGE[pillTone]}`}
          >
            {badge}
          </span>
        )}
      </div>

      <div className="mt-4 flex items-end justify-between gap-3">
        <div className="min-w-0 text-[28px] font-bold leading-none tracking-tight tabular-nums text-slate-950">
          {value}
        </div>
        {sparkline && sparkline.length >= 2 && (
          <MiniSparkline points={sparkline} color={ACCENT_SPARK[accent]} />
        )}
      </div>

      {(hint || (ctaLabel && ctaHref)) && (
        <div className="mt-4 border-t border-slate-100 pt-3">
          {ctaLabel && ctaHref ? (
            <Link
              href={ctaHref}
              className="flex w-full items-center justify-center gap-1.5 rounded-xl bg-blue-600 px-3 py-2 text-[12.5px] font-semibold text-white shadow-xs transition-colors hover:bg-blue-700"
            >
              <span>{ctaLabel}</span>
              <ArrowRight className="h-3.5 w-3.5" />
            </Link>
          ) : (
            <div className="truncate text-[12px] font-medium text-slate-600">{hint}</div>
          )}
        </div>
      )}
    </div>
  );
}
