import type { PublishMode, SocialAccountStatus, SocialPlatform, SocialPostStatus, SocialRegion, SocialSentiment } from "./types";
import { PLAYBOOK_BY_PLATFORM } from "./platform-playbooks";

export const panel =
  "rounded-2xl border border-slate-200/90 bg-white p-5 text-slate-900 shadow-[0_1px_3px_rgba(15,23,42,0.05),0_6px_16px_-6px_rgba(15,23,42,0.04)]";
export const field =
  "mt-1 w-full rounded-xl border border-slate-200 bg-slate-50/80 px-3 py-2 text-[13px] font-medium text-slate-900 transition-colors placeholder:text-slate-400 focus:border-blue-500 focus:bg-white focus:outline-none focus:ring-[3px] focus:ring-blue-500/12";
export const button =
  "inline-flex items-center justify-center gap-2 rounded-xl border border-slate-200 bg-white px-3 py-2 text-[13px] font-semibold text-slate-700 shadow-xs transition-colors hover:border-slate-300 hover:bg-slate-50 focus-visible:outline-2 focus-visible:outline-blue-700 disabled:opacity-50";
export const primary =
  "inline-flex items-center justify-center gap-2 rounded-xl bg-blue-600 px-3 py-2 text-[13px] font-semibold text-white shadow-[0_2px_8px_-2px_rgba(37,99,235,0.55)] transition-colors hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-blue-700 focus-visible:outline-offset-2 disabled:shadow-none disabled:opacity-50";

export const accountStatusLabel: Record<SocialAccountStatus, string> = { connected: "已授权", disconnected: "未授权", token_expired: "授权过期" };
export const postStatusLabel: Record<SocialPostStatus, string> = { draft: "草稿", scheduled: "已排期", published: "已发布", failed: "失败" };
export const postStatusTone: Record<SocialPostStatus, string> = {
  draft: "bg-slate-100 text-slate-700 border-slate-200",
  scheduled: "bg-amber-50 text-amber-800 border-amber-200",
  published: "bg-emerald-50 text-emerald-800 border-emerald-200",
  failed: "bg-rose-50 text-rose-800 border-rose-200",
};
export const sentimentLabel: Record<SocialSentiment, string> = { positive: "正面", neutral: "中性", negative: "负面", lead_intent: "线索意向" };
export const sentimentTone: Record<SocialSentiment, string> = {
  positive: "border border-emerald-200 bg-emerald-50 text-emerald-800",
  neutral: "border border-slate-200 bg-slate-100 text-slate-700",
  negative: "border border-rose-200 bg-rose-50 text-rose-800",
  lead_intent: "border border-blue-200 bg-blue-50 text-blue-800",
};
export const regionLabel: Record<SocialRegion, string> = { domestic: "国内", overseas: "海外" };
export const publishModeLabel: Record<PublishMode, string> = { api: "API 自动发布", manual_export: "手动导出" };

/**
 * 发布模式补充说明：`API 自动发布` 只代表平台接口已授权，
 * 真正上线仍以平台回执为准；手动模式必须在官方后台人工确认。
 * 保留 `publishModeLabel` 原文不动（既有测试断言了「API 自动发布」）。
 */
export const publishModeHonestNote: Record<PublishMode, string> = {
  api: "接口已授权，上线以平台回执为准",
  manual_export: "需在官方后台人工确认发布",
};

/** 一条贴文到底有没有真的发到外部平台。 */
export type PostDeliveryState = "external_confirmed" | "manual_confirmed" | "recorded_local";

export interface PostDeliveryLabel {
  state: PostDeliveryState;
  zh: string;
  en: string;
  tone: string;
}

/**
 * 贴文送达口径：本地记录 / 官方后台人工确认 / 外部平台已确认。
 * 绝不把「本地已记录」渲染成「已发布上线」。
 */
export function postDeliveryLabel(post: {
  status: string;
  delivery_mode?: string | null;
  external_publish_confirmed?: boolean | null;
  external_post_id?: string | null;
}): PostDeliveryLabel {
  if (post.external_publish_confirmed === true || post.delivery_mode === "external_api") {
    return {
      state: "external_confirmed",
      zh: "外部平台已确认",
      en: "Confirmed externally",
      tone: "border-emerald-200 bg-emerald-50 text-emerald-800",
    };
  }
  if (post.delivery_mode === "manual_confirmed") {
    return {
      state: "manual_confirmed",
      zh: "官方后台手动确认",
      en: "Manually confirmed",
      tone: "border-sky-200 bg-sky-50 text-sky-800",
    };
  }
  return {
    state: "recorded_local",
    zh: "本地已记录（未外发）",
    en: "Recorded locally",
    tone: "border-slate-200 bg-slate-100 text-slate-700",
  };
}

export function platformLabel(platform: SocialPlatform): string {
  return PLAYBOOK_BY_PLATFORM[platform]?.label ?? platform;
}

export function count(value: number): string {
  return Number.isSafeInteger(value) && value >= 0 ? value.toLocaleString("zh-CN") : "—";
}
