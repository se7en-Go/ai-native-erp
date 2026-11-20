import type { PublishMode, SocialAccountStatus, SocialPlatform, SocialPostStatus, SocialRegion, SocialSentiment } from "./types";
import { PLAYBOOK_BY_PLATFORM } from "./platform-playbooks";

export const panel = "rounded-xl border border-slate-200/80 bg-white p-5 text-slate-900 shadow-[0_1px_2px_rgba(15,23,42,0.04)]";
export const field = "mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-[13px] text-slate-900 focus-visible:outline-2 focus-visible:outline-blue-700";
export const button = "inline-flex items-center justify-center gap-2 rounded-lg border border-slate-300 bg-white px-3 py-2 text-[13px] font-medium hover:bg-slate-50 focus-visible:outline-2 focus-visible:outline-blue-700 disabled:opacity-50";
export const primary = "inline-flex items-center justify-center gap-2 rounded-lg bg-blue-700 px-3 py-2 text-[13px] font-medium text-white hover:bg-blue-800 focus-visible:outline-2 focus-visible:outline-blue-700 focus-visible:outline-offset-2 disabled:opacity-50";

export const accountStatusLabel: Record<SocialAccountStatus, string> = { connected: "已授权", disconnected: "未授权", token_expired: "授权过期" };
export const postStatusLabel: Record<SocialPostStatus, string> = { draft: "草稿", scheduled: "已排期", published: "已发布", failed: "失败" };
export const postStatusTone: Record<SocialPostStatus, string> = {
  draft: "bg-slate-100 text-slate-700 border-slate-300",
  scheduled: "bg-amber-100 text-amber-900 border-amber-300",
  published: "bg-emerald-100 text-emerald-900 border-emerald-300",
  failed: "bg-rose-100 text-rose-900 border-rose-300",
};
export const sentimentLabel: Record<SocialSentiment, string> = { positive: "正面", neutral: "中性", negative: "负面", lead_intent: "线索意向" };
export const sentimentTone: Record<SocialSentiment, string> = {
  positive: "bg-emerald-100 text-emerald-900",
  neutral: "bg-slate-100 text-slate-700",
  negative: "bg-rose-100 text-rose-900",
  lead_intent: "bg-blue-100 text-blue-900",
};
export const regionLabel: Record<SocialRegion, string> = { domestic: "国内", overseas: "海外" };
export const publishModeLabel: Record<PublishMode, string> = { api: "API 自动发布", manual_export: "手动导出" };

export function platformLabel(platform: SocialPlatform): string {
  return PLAYBOOK_BY_PLATFORM[platform]?.label ?? platform;
}

export function count(value: number): string {
  return Number.isSafeInteger(value) && value >= 0 ? value.toLocaleString("zh-CN") : "—";
}
