"use client";

import React from "react";
import Link from "next/link";
import { ArrowRight } from "lucide-react";

export type BentoAccent = "blue" | "emerald" | "amber" | "rose" | "violet" | "slate";

const ACCENT_ICON_BG: Record<BentoAccent, string> = {
  blue: "bg-blue-50 text-blue-600 ring-1 ring-blue-100 dark:bg-blue-900/30 dark:text-blue-400 dark:ring-blue-800/60",
  emerald:
    "bg-emerald-50 text-emerald-600 ring-1 ring-emerald-100 dark:bg-emerald-900/30 dark:text-emerald-400 dark:ring-emerald-800/60",
  amber:
    "bg-amber-50 text-amber-600 ring-1 ring-amber-100 dark:bg-amber-900/30 dark:text-amber-400 dark:ring-amber-800/60",
  rose: "bg-rose-50 text-rose-600 ring-1 ring-rose-100 dark:bg-rose-900/30 dark:text-rose-400 dark:ring-rose-800/60",
  violet:
    "bg-purple-50 text-purple-600 ring-1 ring-purple-100 dark:bg-purple-900/30 dark:text-purple-400 dark:ring-purple-800/60",
  slate:
    "bg-gray-100 text-gray-600 ring-1 ring-gray-200 dark:bg-gray-800 dark:text-gray-300 dark:ring-gray-700",
};

const ACCENT_BADGE: Record<BentoAccent, string> = {
  blue: "bg-blue-50 text-blue-700 border-blue-200/80 dark:bg-blue-900/30 dark:text-blue-300 dark:border-blue-800",
  emerald:
    "bg-emerald-50 text-emerald-700 border-emerald-200/80 dark:bg-emerald-900/30 dark:text-emerald-300 dark:border-emerald-800",
  amber:
    "bg-amber-50 text-amber-700 border-amber-200/80 dark:bg-amber-900/30 dark:text-amber-300 dark:border-amber-800",
  rose: "bg-rose-50 text-rose-700 border-rose-200/80 dark:bg-rose-900/30 dark:text-rose-300 dark:border-rose-800",
  violet:
    "bg-purple-50 text-purple-700 border-purple-200/80 dark:bg-purple-900/30 dark:text-purple-300 dark:border-purple-800",
  slate:
    "bg-gray-100 text-gray-700 border-gray-200 dark:bg-gray-800 dark:text-gray-300 dark:border-gray-700",
};

const ACCENT_SPARK: Record<BentoAccent, string> = {
  blue: "#2563EB",
  emerald: "#10B981",
  amber: "#D97706",
  rose: "#E11D48",
  violet: "#7C3AED",
  slate: "#64748B",
};

function MiniSparkline({
  points,
  color,
  hero = false,
}: {
  points: number[];
  color: string;
  hero?: boolean;
}) {
  if (points.length < 2) return null;
  const min = Math.min(...points);
  const max = Math.max(...points);
  const span = max - min || 1;
  const w = 92;
  const h = 32;
  const coords = points.map((v, idx) => {
    const x = (idx / (points.length - 1)) * w;
    const y = h - 3 - ((v - min) / span) * (h - 6);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const d = `M ${coords.join(" L ")}`;
  const area = `${d} L ${w},${h} L 0,${h} Z`;
  const stroke = hero ? "#BFDBFE" : color;
  const gradId = `spark-${stroke.replace("#", "")}-${points.length}-${hero ? "h" : "n"}`;

  return (
    <svg
      viewBox={`0 0 ${w} ${h}`}
      className="h-8 w-[92px] shrink-0 overflow-visible"
      aria-hidden="true"
    >
      <defs>
        <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={stroke} stopOpacity={hero ? "0.35" : "0.22"} />
          <stop offset="100%" stopColor={stroke} stopOpacity="0.0" />
        </linearGradient>
      </defs>
      <path d={area} fill={`url(#${gradId})`} />
      <path
        d={d}
        fill="none"
        stroke={stroke}
        strokeWidth="2.2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/**
 * 21st.dev Clean Cool Gray + Tech Blue KPI Card (full light & dark mode support).
 */
export function BentoStatCard({
  label,
  value,
  badge,
  badgeTone,
  hint,
  icon: Icon,
  accent = "blue",
  variant = "default",
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
  variant?: "default" | "hero";
  sparkline?: number[];
  ctaLabel?: string;
  ctaHref?: string;
  testId?: string;
}) {
  const pillTone = badgeTone ?? accent;
  const isHero = variant === "hero";

  return (
    <div
      data-testid={testId}
      className={`group relative flex min-w-0 flex-col justify-between overflow-hidden rounded-xl p-5 transition-all duration-200 ${
        isHero
          ? "border border-blue-500/40 bg-gradient-to-br from-blue-600 via-blue-600 to-indigo-600 text-white shadow-md shadow-blue-600/20"
          : "border border-gray-200 bg-white text-gray-900 shadow-sm hover:shadow-md dark:border-gray-800 dark:bg-gray-900 dark:text-gray-100"
      }`}
    >
      <div className="relative flex items-start justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2.5">
          {Icon && (
            <span
              className={`grid h-9 w-9 shrink-0 place-items-center rounded-lg ${
                isHero
                  ? "bg-white/15 text-white ring-1 ring-white/25 backdrop-blur-sm"
                  : ACCENT_ICON_BG[accent]
              }`}
            >
              <Icon className="h-4 w-4" />
            </span>
          )}
          <span
            className={`min-w-0 truncate text-[13px] font-medium ${
              isHero ? "text-blue-50" : "text-gray-600 dark:text-gray-400"
            }`}
          >
            {label}
          </span>
        </div>
        {badge && (
          <span
            className={`inline-flex shrink-0 items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-semibold tabular-nums ${
              isHero
                ? "border-white/25 bg-white/15 text-white backdrop-blur-sm"
                : ACCENT_BADGE[pillTone]
            }`}
          >
            {badge}
          </span>
        )}
      </div>

      <div className="relative mt-3.5 flex items-end justify-between gap-3">
        <div
          className={`min-w-0 truncate text-2xl font-bold leading-tight tracking-tight tabular-nums ${
            isHero ? "text-white" : "text-gray-900 dark:text-gray-100"
          }`}
        >
          {value}
        </div>
        {sparkline && sparkline.length >= 2 && (
          <MiniSparkline points={sparkline} color={ACCENT_SPARK[accent]} hero={isHero} />
        )}
      </div>

      {(hint || (ctaLabel && ctaHref)) && (
        <div
          className={`relative mt-3.5 border-t pt-2.5 ${
            isHero ? "border-white/15" : "border-gray-100 dark:border-gray-800"
          }`}
        >
          {ctaLabel && ctaHref ? (
            <Link
              href={ctaHref}
              className={`flex w-full items-center justify-center gap-1.5 rounded-lg px-3 py-1.5 text-[12.5px] font-semibold transition-colors ${
                isHero
                  ? "bg-white text-blue-700 hover:bg-blue-50"
                  : "bg-blue-600 text-white shadow-xs hover:bg-blue-700"
              }`}
            >
              <span>{ctaLabel}</span>
              <ArrowRight className="h-3.5 w-3.5" />
            </Link>
          ) : (
            <div
              className={`truncate text-[12px] font-medium ${
                isHero ? "text-blue-100/90" : "text-gray-500 dark:text-gray-400"
              }`}
            >
              {hint}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
