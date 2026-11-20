"use client";

import React from "react";
import Link from "next/link";
import { ArrowRight, TrendingUp, type LucideIcon } from "lucide-react";

export type DashboardAccent = "blue" | "green" | "purple" | "orange" | "rose" | "slate";

const ICON_SURFACE: Record<DashboardAccent, { box: string; icon: string }> = {
  blue: {
    box: "bg-blue-50 dark:bg-blue-900/25",
    icon: "text-blue-600 dark:text-blue-400",
  },
  green: {
    box: "bg-emerald-50 dark:bg-emerald-900/25",
    icon: "text-emerald-600 dark:text-emerald-400",
  },
  purple: {
    box: "bg-purple-50 dark:bg-purple-900/25",
    icon: "text-purple-600 dark:text-purple-400",
  },
  orange: {
    box: "bg-orange-50 dark:bg-orange-900/25",
    icon: "text-orange-600 dark:text-orange-400",
  },
  rose: {
    box: "bg-rose-50 dark:bg-rose-900/25",
    icon: "text-rose-600 dark:text-rose-400",
  },
  slate: {
    box: "bg-gray-100 dark:bg-gray-800",
    icon: "text-gray-600 dark:text-gray-300",
  },
};

const BAR_FILL: Record<DashboardAccent, string> = {
  blue: "bg-blue-500 dark:bg-blue-500",
  green: "bg-emerald-500 dark:bg-emerald-500",
  purple: "bg-purple-500 dark:bg-purple-500",
  orange: "bg-orange-500 dark:bg-orange-500",
  rose: "bg-rose-500 dark:bg-rose-500",
  slate: "bg-gray-400 dark:bg-gray-500",
};

/**
 * 21st.dev Clean KPI Stat Card — crisp border, pastel icon badge, bold tabular figure,
 * optional status badge or action link. Supports light & dark modes cleanly.
 */
export function DashboardKpiCard({
  label,
  value,
  subtext,
  icon: Icon,
  accent = "blue",
  badge,
  href,
  ctaLabel,
  showTrend = true,
  testId,
}: {
  label: string;
  value: string;
  subtext?: string;
  icon: LucideIcon;
  accent?: DashboardAccent;
  badge?: string;
  href?: string;
  ctaLabel?: string;
  showTrend?: boolean;
  testId?: string;
}) {
  const surface = ICON_SURFACE[accent];

  return (
    <div
      data-testid={testId}
      className="flex flex-col justify-between rounded-xl border border-gray-200 bg-white p-5 shadow-sm transition-shadow hover:shadow-md dark:border-gray-800 dark:bg-gray-900"
    >
      <div>
        <div className="mb-3.5 flex items-center justify-between gap-2">
          <div className={`grid h-9 w-9 place-content-center rounded-lg ${surface.box}`}>
            <Icon className={`h-5 w-5 ${surface.icon}`} aria-hidden="true" />
          </div>
          {badge ? (
            <span className="rounded-full bg-gray-100 px-2.5 py-0.5 text-[11px] font-medium text-gray-600 dark:bg-gray-800 dark:text-gray-300">
              {badge}
            </span>
          ) : showTrend ? (
            <TrendingUp className="h-4 w-4 text-emerald-500" aria-hidden="true" />
          ) : null}
        </div>

        <h3 className="text-[13px] font-medium text-gray-600 dark:text-gray-400">{label}</h3>
        <p className="mt-1 truncate text-2xl font-bold tracking-tight text-gray-900 tabular-nums dark:text-gray-100">
          {value}
        </p>
      </div>

      {(subtext || (href && ctaLabel)) && (
        <div className="mt-2.5 flex items-center justify-between gap-2 text-[12.5px]">
          {subtext && (
            <span className="truncate font-medium text-gray-500 dark:text-gray-400">{subtext}</span>
          )}
          {href && ctaLabel && (
            <Link
              href={href}
              className="inline-flex shrink-0 items-center gap-1 font-semibold text-blue-600 transition-colors hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300"
            >
              <span>{ctaLabel}</span>
              <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
            </Link>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * 21st.dev Surface Card container with header + optional action link.
 */
export function DashboardCard({
  title,
  subtitle,
  action,
  testId,
  children,
  className = "",
}: {
  title: string;
  subtitle?: string;
  action?: React.ReactNode;
  testId?: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section
      data-testid={testId}
      className={`rounded-xl border border-gray-200 bg-white p-5 shadow-sm dark:border-gray-800 dark:bg-gray-900 ${className}`}
    >
      <div className="mb-4 flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <h2 className="text-[16px] font-semibold tracking-tight text-gray-900 dark:text-gray-100">
            {title}
          </h2>
          {subtitle && (
            <p className="mt-0.5 text-[12px] text-gray-500 dark:text-gray-400">{subtitle}</p>
          )}
        </div>
        {action && <div className="shrink-0">{action}</div>}
      </div>
      {children}
    </section>
  );
}

/**
 * 21st.dev Recent Activity Row — icon badge, concise title + description, right metadata.
 */
export function DashboardActivityRow({
  icon: Icon,
  accent = "blue",
  title,
  description,
  meta,
  href,
}: {
  icon: LucideIcon;
  accent?: DashboardAccent;
  title: string;
  description: string;
  meta?: string;
  href?: string;
}) {
  const surface = ICON_SURFACE[accent];
  const inner = (
    <div className="flex items-center gap-3.5 rounded-lg p-2.5 transition-colors hover:bg-gray-50 dark:hover:bg-gray-800/80">
      <div className={`grid h-9 w-9 shrink-0 place-content-center rounded-lg ${surface.box}`}>
        <Icon className={`h-4 w-4 ${surface.icon}`} aria-hidden="true" />
      </div>
      <div className="min-w-0 flex-1">
        <p className="truncate text-[13.5px] font-medium text-gray-900 dark:text-gray-100">
          {title}
        </p>
        <p className="truncate text-[12px] text-gray-500 dark:text-gray-400">{description}</p>
      </div>
      {meta && (
        <span className="shrink-0 text-[11.5px] font-medium text-gray-400 tabular-nums dark:text-gray-500">
          {meta}
        </span>
      )}
    </div>
  );

  if (href) {
    return (
      <Link href={href} className="block">
        {inner}
      </Link>
    );
  }
  return inner;
}

/**
 * 21st.dev Quick Stats thin progress bar row.
 */
export function DashboardProgressStat({
  label,
  valueLabel,
  percent,
  accent = "blue",
}: {
  label: string;
  valueLabel: string;
  percent: number;
  accent?: DashboardAccent;
}) {
  const clamped = Math.max(0, Math.min(100, percent));
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between gap-2 text-[13px]">
        <span className="truncate text-gray-600 dark:text-gray-400">{label}</span>
        <span className="shrink-0 font-semibold text-gray-900 tabular-nums dark:text-gray-100">
          {valueLabel}
        </span>
      </div>
      <div className="h-2 w-full overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700">
        <div
          className={`h-full rounded-full transition-all duration-300 ${BAR_FILL[accent]}`}
          style={{ width: `${clamped > 0 ? Math.max(4, clamped) : 0}%` }}
        />
      </div>
    </div>
  );
}
