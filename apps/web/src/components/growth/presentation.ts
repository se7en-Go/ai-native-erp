import type { ProspectGrade, ProspectSyncStatus } from "./types";

export const panel = "rounded-2xl border border-slate-200 bg-white p-5 text-slate-900 shadow-xs";
export const field = "mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus-visible:outline-2 focus-visible:outline-teal-700";
export const button = "inline-flex items-center justify-center gap-2 rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm font-medium hover:bg-slate-50 focus-visible:outline-2 focus-visible:outline-teal-700 disabled:opacity-50";
export const primary = "inline-flex items-center justify-center gap-2 rounded-lg bg-teal-700 px-3 py-2 text-sm font-semibold text-white hover:bg-teal-800 focus-visible:outline-2 focus-visible:outline-teal-700 focus-visible:outline-offset-2 disabled:opacity-50";
export const gradeTone: Record<ProspectGrade, string> = {
  A: "bg-emerald-100 text-emerald-900", B: "bg-amber-100 text-amber-900", C: "bg-slate-100 text-slate-700",
};
export const syncLabel: Record<ProspectSyncStatus, string> = { raw: "待清洗", cleaned: "已清洗", synced_to_crm: "已同步 ERP", discarded: "已弃用" };
export function count(value: number) {
  return Number.isSafeInteger(value) && value >= 0 ? value.toLocaleString("zh-CN") : "—";
}
