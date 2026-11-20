import type { ProspectGrade, ProspectSyncStatus } from "./types";

export const panel =
  "rounded-2xl border border-slate-200/90 bg-white p-5 text-slate-900 shadow-[0_1px_3px_rgba(15,23,42,0.05),0_6px_16px_-6px_rgba(15,23,42,0.04)]";
export const field =
  "mt-1 w-full rounded-xl border border-slate-200 bg-slate-50/80 px-3 py-2 text-[13px] font-medium text-slate-900 transition-colors placeholder:text-slate-400 focus:border-blue-500 focus:bg-white focus:outline-none focus:ring-[3px] focus:ring-blue-500/12";
export const button =
  "inline-flex items-center justify-center gap-2 rounded-xl border border-slate-200 bg-white px-3 py-2 text-[13px] font-semibold text-slate-700 shadow-xs transition-colors hover:border-slate-300 hover:bg-slate-50 focus-visible:outline-2 focus-visible:outline-blue-700 disabled:opacity-50";
export const primary =
  "inline-flex items-center justify-center gap-2 rounded-xl bg-blue-600 px-3 py-2 text-[13px] font-semibold text-white shadow-[0_2px_8px_-2px_rgba(37,99,235,0.55)] transition-colors hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-blue-700 focus-visible:outline-offset-2 disabled:shadow-none disabled:opacity-50";
export const gradeTone: Record<ProspectGrade, string> = {
  A: "bg-emerald-100 text-emerald-800 ring-1 ring-emerald-200",
  B: "bg-amber-100 text-amber-800 ring-1 ring-amber-200",
  C: "bg-slate-100 text-slate-700 ring-1 ring-slate-200",
};
export const syncLabel: Record<ProspectSyncStatus, string> = { raw: "待清洗", cleaned: "已清洗", synced_to_crm: "已同步 ERP", discarded: "已弃用" };
export function count(value: number) {
  return Number.isSafeInteger(value) && value >= 0 ? value.toLocaleString("zh-CN") : "—";
}
