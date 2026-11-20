import type { AiCapability, AutonomyLevel } from "@/lib/contracts";

export const AUTONOMY = {
  L2_auto: { badge: "L2 全接管", heading: "L2 全接管·AI 自动干", tone: "border-emerald-200 bg-emerald-50 text-emerald-900" },
  L1_proposal: { badge: "L1 待确认", heading: "L1 待确认·AI 提案人审", tone: "border-amber-200 bg-amber-50 text-amber-950" },
  L0_copilot: { badge: "L0 人工把关", heading: "L0 人工把关·AI 不碰", tone: "border-rose-200 bg-rose-50 text-rose-900" },
} as const satisfies Record<AutonomyLevel, { badge: string; heading: string; tone: string }>;

export const CAPABILITY_LABELS = {
  cs_support: "客服支持",
  sales_quoting: "销售报价",
  order_logistics: "订单物流",
  content_studio: "内容创作",
  biz_analytics: "经营分析",
  finance_recon: "财务对账",
} satisfies Record<AiCapability, string>;

export const button =
  "rounded-lg border border-gray-200 bg-white px-2.5 py-1.5 text-[12.5px] font-medium text-gray-700 transition-colors hover:border-blue-300 hover:bg-blue-50/60 hover:text-blue-700 focus-visible:outline-2 focus-visible:outline-blue-600 disabled:opacity-50 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-200 dark:hover:border-blue-600 dark:hover:bg-gray-700 dark:hover:text-blue-300";
export const field =
  "w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm text-gray-900 focus-visible:outline-2 focus-visible:outline-blue-600 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-100";

export function formatCount(value: number): string {
  return Number.isSafeInteger(value) && value >= 0 ? value.toLocaleString("zh-CN") : "—";
}

export function formatUsd(value: string | null): string {
  return typeof value === "string" && /^-?\d+(?:\.\d+)?$/.test(value) ? value + " USD" : "—";
}
