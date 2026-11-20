"use client";

import React from "react";
import Link from "next/link";
import { ArrowRight } from "lucide-react";

export type BentoAccent = "blue" | "emerald" | "amber" | "rose" | "violet" | "slate";

const ACCENT_ICON_BG: Record<BentoAccent, string> = {
  blue: "bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200/80",
  emerald: "bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200/80",
  amber: "bg-amber-50 text-amber-700 ring-1 ring-amber-200/80",
  rose: "bg-rose-50 text-rose-700 ring-1 ring-rose-200/80",
  violet: "bg-stone-100 text-stone-800 ring-1 ring-stone-200/90",
  slate: "bg-stone-100 text-stone-700 ring-1 ring-stone-200/90",
};

const ACCENT_BADGE: Record<BentoAccent, string> = {
  blue: "bg-emerald-50 text-emerald-800 border-emerald-200/90",
  emerald: "bg-emerald-50 text-emerald-800 border-emerald-200/90",
  amber: "bg-amber-50 text-amber-800 border-amber-200/90",
  rose: "bg-rose-50 text-rose-800 border-rose-200/90",
  violet: "bg-stone-100 text-stone-800 border-stone-200/90",
  slate: "bg-stone-100 text-stone-700 border-stone-200/90",
};

const ACCENT_SPARK: Record<BentoAccent, string> = {
  blue: "#059669",
  emerald: "#059669",
  amber: "#D97706",
  rose: "#E11D48",
  violet: "#18181B",
  slate: "#57534E",
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
  const stroke = hero ? "#A7F3D0" : color;
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
 * 21st.dev Command Center 风格 Bento 指标卡：
 * - `variant="hero"` 启用深玉翡翠渐变焦点卡（整屏视觉锚点，告别平平无奇）
 * - `variant="default"` 为暖陶石浮雕白卡 + 曜石墨 CTA
 */
export function BentoStatCard({
  label,
  value,
  badge,
  badgeTone,
  hint,
  icon: Icon,
  accent = "emerald",
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
      className={`group relative flex flex-col justify-between overflow-hidden rounded-[20px] p-5 transition-all duration-200 hover:-translate-y-0.5 ${
        isHero
          ? "border border-emerald-700/40 bg-gradient-to-br from-[#064E3B] via-[#065F46] to-[#0F766E] text-white shadow-[0_14px_34px_-12px_rgba(6,95,70,0.48),inset_0_1px_0_rgba(255,255,255,0.22)]"
          : "border border-[#E5E2DC] bg-white/95 text-zinc-950 shadow-[inset_0_1px_0_rgba(255,255,255,0.95),0_1px_2px_rgba(24,24,27,0.05),0_10px_26px_-14px_rgba(24,24,27,0.09)] hover:border-[#D4CFC6]"
      }`}
    >
      {isHero && (
        <div
          aria-hidden="true"
          className="pointer-events-none absolute -top-10 -right-10 h-36 w-36 rounded-full bg-emerald-300/20 blur-2xl"
        />
      )}

      <div className="relative flex items-start justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2.5">
          {Icon && (
            <span
              className={`grid h-9 w-9 shrink-0 place-items-center rounded-xl ${
                isHero
                  ? "bg-white/15 text-emerald-100 ring-1 ring-white/25 backdrop-blur-sm"
                  : ACCENT_ICON_BG[accent]
              }`}
            >
              <Icon className="h-4 w-4" />
            </span>
          )}
          <span
            className={`truncate text-[13px] font-semibold ${
              isHero ? "text-emerald-50" : "text-stone-700"
            }`}
          >
            {label}
          </span>
        </div>
        {badge && (
          <span
            className={`inline-flex shrink-0 items-center gap-1 rounded-full border px-2.5 py-0.5 text-[11px] font-semibold tabular-nums ${
              isHero
                ? "border-white/25 bg-white/15 text-white backdrop-blur-sm"
                : ACCENT_BADGE[pillTone]
            }`}
          >
            {badge}
          </span>
        )}
      </div>

      <div className="relative mt-4 flex items-end justify-between gap-3">
        <div
          className={`min-w-0 text-[30px] font-bold leading-none tracking-tight tabular-nums ${
            isHero ? "text-white" : "text-zinc-950"
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
          className={`relative mt-4 border-t pt-3 ${
            isHero ? "border-white/15" : "border-[#EFECE6]"
          }`}
        >
          {ctaLabel && ctaHref ? (
            <Link
              href={ctaHref}
              className={`flex w-full items-center justify-center gap-1.5 rounded-xl px-3.5 py-2 text-[12.5px] font-semibold transition-all ${
                isHero
                  ? "bg-white text-emerald-950 hover:bg-emerald-50"
                  : "bg-[#18181B] text-[#FAFAF9] shadow-xs hover:bg-[#065F46]"
              }`}
            >
              <span>{ctaLabel}</span>
              <ArrowRight className="h-3.5 w-3.5" />
            </Link>
          ) : (
            <div
              className={`truncate text-[12px] font-medium ${
                isHero ? "text-emerald-100/90" : "text-stone-600"
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
