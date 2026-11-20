"use client";

import React from "react";

/**
 * 企业级工作台基础原语。全部为纯展示组件，不含数据逻辑，
 * 让全站页面共享同一套 21st.dev 冷灰白/科技蓝 spacing、边框、字号与深浅色节奏。
 */

export type Tone = "neutral" | "success" | "warning" | "danger" | "info";

const TONE_CLASS: Record<Tone, string> = {
  neutral:
    "border-gray-200 bg-gray-100 text-gray-700 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300",
  success:
    "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-800/70 dark:bg-emerald-900/30 dark:text-emerald-300",
  warning:
    "border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-800/70 dark:bg-amber-900/30 dark:text-amber-300",
  danger:
    "border-rose-200 bg-rose-50 text-rose-700 dark:border-rose-800/70 dark:bg-rose-900/30 dark:text-rose-300",
  info:
    "border-blue-200 bg-blue-50 text-blue-700 dark:border-blue-800/70 dark:bg-blue-900/30 dark:text-blue-300",
};

export function Badge({
  tone = "neutral",
  children,
  className = "",
  ...rest
}: React.HTMLAttributes<HTMLSpanElement> & { tone?: Tone }) {
  return (
    <span
      {...rest}
      className={`inline-flex items-center gap-1 whitespace-nowrap rounded-full border px-2 py-0.5 text-[11px] font-medium ${TONE_CLASS[tone]} ${className}`}
    >
      {children}
    </span>
  );
}

/** 普通工作台页面：宽屏流式容器。 */
export function Page({
  children,
  full = false,
  className = "",
}: {
  children: React.ReactNode;
  /** 多栏分屏页面（inbox / 地图获客 / 社媒）占满宽度 */
  full?: boolean;
  className?: string;
}) {
  return (
    <div className={`${full ? "erp-page-full" : "erp-page"} min-w-0 space-y-5 ${className}`}>
      {children}
    </div>
  );
}

export function PageHeader({
  title,
  description,
  actions,
  badge,
  meta,
}: {
  title: string;
  description?: string;
  actions?: React.ReactNode;
  badge?: React.ReactNode;
  meta?: React.ReactNode;
}) {
  return (
    <header className="flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-[22px] font-bold tracking-tight text-gray-900 dark:text-gray-100">
            {title}
          </h1>
          {badge}
        </div>
        {description && (
          <p className="mt-1 max-w-5xl text-[13.5px] leading-6 text-gray-600 dark:text-gray-400">
            {description}
          </p>
        )}
        {meta && <div className="mt-2 flex flex-wrap items-center gap-2">{meta}</div>}
      </div>
      {actions && <div className="flex min-w-0 flex-wrap items-center gap-2">{actions}</div>}
    </header>
  );
}

export function Card({
  children,
  className = "",
  ...rest
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div {...rest} className={`erp-card min-w-0 ${className}`}>
      {children}
    </div>
  );
}

export function CardHeader({
  title,
  description,
  actions,
  icon: Icon,
}: {
  title: React.ReactNode;
  description?: React.ReactNode;
  actions?: React.ReactNode;
  icon?: React.ComponentType<{ className?: string }>;
}) {
  return (
    <div className="erp-card-header">
      <div className="min-w-0">
        <h2 className="flex items-center gap-2 text-[15px] font-semibold text-gray-900 dark:text-gray-100">
          {Icon && <Icon className="h-4 w-4 text-blue-600 dark:text-blue-400" />}
          {title}
        </h2>
        {description && (
          <p className="mt-1 text-[12.5px] leading-5 text-gray-500 dark:text-gray-400">
            {description}
          </p>
        )}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export function StatCard({
  label,
  value,
  hint,
  tone = "neutral",
  icon: Icon,
  testId,
}: {
  label: string;
  value: React.ReactNode;
  hint?: React.ReactNode;
  tone?: Tone;
  icon?: React.ComponentType<{ className?: string }>;
  testId?: string;
}) {
  const accent: Record<Tone, string> = {
    neutral: "text-gray-900 dark:text-gray-100",
    success: "text-emerald-600 dark:text-emerald-400",
    warning: "text-amber-600 dark:text-amber-400",
    danger: "text-rose-600 dark:text-rose-400",
    info: "text-blue-600 dark:text-blue-400",
  };
  return (
    <div data-testid={testId} className="erp-card min-w-0 p-4">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[13px] font-medium text-gray-600 dark:text-gray-400">{label}</span>
        {Icon && <Icon className="h-4 w-4 text-gray-400 dark:text-gray-500" />}
      </div>
      <div className={`mt-1.5 text-[24px] font-bold tabular-nums ${accent[tone]}`}>{value}</div>
      {hint && <div className="mt-1 text-[12px] text-gray-500 dark:text-gray-400">{hint}</div>}
    </div>
  );
}

export function EmptyState({
  children,
  testId,
  className = "",
}: {
  children: React.ReactNode;
  testId?: string;
  className?: string;
}) {
  return (
    <div
      data-testid={testId}
      className={`rounded-xl border border-dashed border-gray-200 bg-gray-50 p-8 text-center text-[13px] font-medium text-gray-500 dark:border-gray-700 dark:bg-gray-800/40 dark:text-gray-400 ${className}`}
    >
      {children}
    </div>
  );
}

/** 表格外层：统一圆角、清晰边框与横向滚动。 */
export function TableWrap({
  children,
  className = "",
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`overflow-x-auto rounded-xl border border-gray-200 bg-white shadow-xs dark:border-gray-800 dark:bg-gray-900 ${className}`}
    >
      {children}
    </div>
  );
}

export function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-[12px] font-semibold tracking-wide text-gray-600 dark:text-gray-400">
      {children}
    </div>
  );
}
