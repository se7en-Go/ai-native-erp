"use client";

import React from "react";

/**
 * 企业级工作台基础原语。全部为纯展示组件，不含数据逻辑，
 * 让 25 个页面共享同一套 spacing / 边框 / 字号节奏。
 */

export type Tone = "neutral" | "success" | "warning" | "danger" | "info";

const TONE_CLASS: Record<Tone, string> = {
  neutral: "border-slate-300 bg-slate-100 text-slate-700",
  success: "border-emerald-300 bg-emerald-50 text-emerald-800",
  warning: "border-amber-300 bg-amber-50 text-amber-900",
  danger: "border-rose-300 bg-rose-50 text-rose-800",
  info: "border-blue-300 bg-blue-50 text-blue-800",
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

/** 普通工作台页面：宽屏流式容器，替代历史 `max-w-5xl` / `max-w-6xl`。 */
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
    <div className={`${full ? "erp-page-full" : "erp-page"} space-y-4 ${className}`}>
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
          <h1 className="text-[22px] font-bold tracking-tight text-slate-950">{title}</h1>
          {badge}
        </div>
        {description && (
          <p className="mt-1 max-w-5xl text-[13.5px] leading-6 text-slate-700">{description}</p>
        )}
        {meta && <div className="mt-2 flex flex-wrap items-center gap-2">{meta}</div>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </header>
  );
}

export function Card({
  children,
  className = "",
  ...rest
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div {...rest} className={`erp-card ${className}`}>
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
        <h2 className="flex items-center gap-2 text-[15px] font-semibold text-slate-950">
          {Icon && <Icon className="h-4 w-4 text-slate-600" />}
          {title}
        </h2>
        {description && (
          <p className="mt-1 text-[12.5px] leading-5 text-slate-600">{description}</p>
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
    neutral: "text-slate-900",
    success: "text-emerald-700",
    warning: "text-amber-700",
    danger: "text-rose-700",
    info: "text-blue-700",
  };
  return (
    <div data-testid={testId} className="erp-card p-4">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[13px] font-semibold text-slate-700">{label}</span>
        {Icon && <Icon className="h-4 w-4 text-slate-500" />}
      </div>
      <div className={`mt-1.5 text-[24px] font-bold tabular-nums ${accent[tone]}`}>
        {value}
      </div>
      {hint && <div className="mt-1 text-[12px] text-slate-600">{hint}</div>}
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
      className={`rounded-xl border border-dashed border-slate-300 bg-slate-50 p-8 text-center text-[13px] font-medium text-slate-600 ${className}`}
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
    <div className={`overflow-x-auto rounded-xl border border-slate-300 bg-white shadow-xs ${className}`}>
      {children}
    </div>
  );
}

export function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-[12px] font-semibold tracking-wide text-slate-600">{children}</div>
  );
}
