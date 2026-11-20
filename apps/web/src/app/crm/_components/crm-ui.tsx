"use client";

import React from "react";

/**
 * CRM 模块内共享的呈现原语。
 * 只做样式与语义映射，不持有任何数据或请求逻辑，
 * 让线索池 / 客户档案 / 商机看板 / 跟进记录共享同一套 21st.dev 视觉语言。
 */

const AVATAR_PALETTE = [
  "bg-blue-100 text-blue-700 ring-blue-200",
  "bg-emerald-100 text-emerald-700 ring-emerald-200",
  "bg-violet-100 text-violet-700 ring-violet-200",
  "bg-amber-100 text-amber-800 ring-amber-200",
  "bg-rose-100 text-rose-700 ring-rose-200",
  "bg-cyan-100 text-cyan-700 ring-cyan-200",
  "bg-indigo-100 text-indigo-700 ring-indigo-200",
];

/** 名称 → 稳定色板下标，保证同一客户在列表与详情里颜色一致。 */
export function avatarToneClass(seed: string): string {
  let hash = 0;
  for (let i = 0; i < seed.length; i += 1) {
    hash = (hash * 31 + seed.charCodeAt(i)) % 997;
  }
  return AVATAR_PALETTE[hash % AVATAR_PALETTE.length];
}

export function initials(displayName: string): string {
  const parts = displayName.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

export function CrmAvatar({
  name,
  size = "md",
}: {
  name: string;
  size?: "sm" | "md" | "lg";
}) {
  const dimensions =
    size === "lg" ? "h-10 w-10 text-[13px]" : size === "sm" ? "h-7 w-7 text-[10.5px]" : "h-8 w-8 text-[11.5px]";
  return (
    <span
      aria-hidden="true"
      className={`grid shrink-0 place-items-center rounded-full font-bold ring-1 ${dimensions} ${avatarToneClass(
        name
      )}`}
    >
      {initials(name)}
    </span>
  );
}

export type CrmTier = "A" | "B" | "C";

const TIER_STYLE: Record<CrmTier, string> = {
  A: "border-emerald-200 bg-emerald-50 text-emerald-800",
  B: "border-amber-200 bg-amber-50 text-amber-800",
  C: "border-slate-200 bg-slate-100 text-slate-700",
};

/** 线索资格分 → A/B/C 等级，语义色与跟进优先级一致。 */
export function tierFromScore(score: number): CrmTier {
  if (score >= 70) return "A";
  if (score >= 40) return "B";
  return "C";
}

export function CrmTierBadge({ tier }: { tier: CrmTier }) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10.5px] font-bold ${TIER_STYLE[tier]}`}
    >
      {tier}
      <span className="font-medium opacity-80">级</span>
    </span>
  );
}

const STATUS_STYLE: Record<string, string> = {
  new: "border-slate-200 bg-slate-100 text-slate-700",
  contacted: "border-blue-200 bg-blue-50 text-blue-700",
  qualified: "border-emerald-200 bg-emerald-50 text-emerald-700",
  disqualified: "border-rose-200 bg-rose-50 text-rose-700",
  converted: "border-violet-200 bg-violet-50 text-violet-700",
  open: "border-blue-200 bg-blue-50 text-blue-700",
  done: "border-slate-200 bg-slate-100 text-slate-600",
  inactive: "border-slate-200 bg-slate-100 text-slate-600",
};

const STATUS_DOT: Record<string, string> = {
  new: "bg-slate-400",
  contacted: "bg-blue-500",
  qualified: "bg-emerald-500",
  disqualified: "bg-rose-500",
  converted: "bg-violet-500",
  open: "bg-blue-500",
  done: "bg-slate-400",
  inactive: "bg-slate-400",
};

export function CrmStatusPill({
  status,
  label,
}: {
  status: string;
  label?: string;
}) {
  const tone = STATUS_STYLE[status] ?? "border-slate-200 bg-slate-100 text-slate-700";
  const dot = STATUS_DOT[status] ?? "bg-slate-400";
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[10.5px] font-semibold ${tone}`}
    >
      <span aria-hidden="true" className={`h-1.5 w-1.5 rounded-full ${dot}`} />
      {label ?? status}
    </span>
  );
}

export const PRIORITY_DOT: Record<string, string> = {
  high: "bg-rose-500",
  normal: "bg-blue-500",
  low: "bg-slate-400",
};

export const PRIORITY_PILL: Record<string, string> = {
  high: "border-rose-200 bg-rose-50 text-rose-700",
  normal: "border-blue-200 bg-blue-50 text-blue-700",
  low: "border-slate-200 bg-slate-100 text-slate-600",
};

/** 21st.dev 工具栏输入框：柔和底色 + 聚焦白底，避免线框感。 */
export const CRM_FIELD =
  "rounded-xl border border-slate-200 bg-slate-50/80 px-2.5 py-1.5 text-[12.5px] text-slate-800 transition-colors placeholder:text-slate-400 focus:border-blue-500 focus:bg-white focus:outline-none focus:ring-[3px] focus:ring-blue-500/12";
