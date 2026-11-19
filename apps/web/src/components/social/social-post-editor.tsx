"use client";

import { useState, type FormEvent, type KeyboardEvent } from "react";
import { CalendarClock, Hash, Save, Sparkles, X } from "lucide-react";
import { getPlaybook, PLATFORM_PLAYBOOKS } from "./platform-playbooks";
import { accountStatusLabel, button, field, panel, primary, publishModeLabel } from "./presentation";
import type { SkuOption, SocialAccount, SocialPlatform, SocialPostDraft } from "./types";

const captionLimits: Record<SocialPlatform, number> = {
  instagram: 2200, tiktok: 2200, youtube: 5000, facebook: 63206, linkedin: 3000, pinterest: 500, x: 280,
  xiaohongshu: 1000, douyin: 1000, kuaishou: 1000, bilibili: 2000, wechat_video: 1000, wechat_oa: 20000, weibo: 2000,
};
const MAX_HASHTAGS = 30;

function normalizeTag(raw: string): string {
  const cleaned = raw.trim().replace(/^#+/, "").replace(/\s+/g, " ").trim();
  return cleaned ? "#" + cleaned : "";
}

export function SocialPostEditor({ accounts, skus, initialPlatforms = [], onGenerateCaption, onSaveDraft, onSchedule }: {
  accounts: readonly SocialAccount[];
  skus: readonly SkuOption[];
  initialPlatforms?: readonly SocialPlatform[];
  onGenerateCaption: (request: { platform: SocialPlatform; sku_ref: string | null; prompt: string }) => Promise<string>;
  onSaveDraft: (drafts: readonly SocialPostDraft[]) => void | Promise<void>;
  onSchedule: (drafts: readonly SocialPostDraft[]) => void | Promise<void>;
}) {
  const [platforms, setPlatforms] = useState<readonly SocialPlatform[]>(initialPlatforms);
  const [title, setTitle] = useState("");
  const [caption, setCaption] = useState("");
  const [hashtags, setHashtags] = useState<readonly string[]>([]);
  const [tagInput, setTagInput] = useState("");
  const [tagError, setTagError] = useState<string | null>(null);
  const [sku, setSku] = useState("");
  const [when, setWhen] = useState("");
  const [pending, setPending] = useState<"generate" | "save" | "schedule" | null>(null);
  const [feedback, setFeedback] = useState<{ error: boolean; text: string } | null>(null);

  const connected = new Set(accounts.filter((account) => account.status === "connected").map((account) => account.platform));
  const manual = platforms.filter((platform) => getPlaybook(platform)?.publish_mode === "manual_export");
  const unauthorized = platforms.filter((platform) => !connected.has(platform));
  const busy = !!pending;
  const limit = platforms.length ? Math.min(...platforms.map((platform) => captionLimits[platform])) : null;
  const overLimit = limit !== null && caption.length > limit;
  const readyToSave = !!(platforms.length && caption.trim());
  const canSchedule = readyToSave && !!when && !manual.length && !unauthorized.length && !overLimit;

  function togglePlatform(platform: SocialPlatform, checked: boolean) {
    setPlatforms((prev) => checked ? [...prev, platform] : prev.filter((item) => item !== platform));
    setFeedback(null);
  }

  function addTag() {
    const tag = normalizeTag(tagInput);
    if (!tag) return;
    if (hashtags.some((existing) => existing.toLowerCase() === tag.toLowerCase())) {
      setTagInput("");
      setTagError("标签已存在。");
      return;
    }
    if (hashtags.length >= MAX_HASHTAGS) {
      setTagError("最多 " + MAX_HASHTAGS + " 个标签。");
      return;
    }
    setHashtags((prev) => [...prev, tag]);
    setTagInput("");
    setTagError(null);
  }

  function onTagKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Enter") { event.preventDefault(); addTag(); }
  }

  function drafts(scheduledAt: string | null): SocialPostDraft[] {
    return platforms.map((platform) => ({
      platform,
      title: title.trim(),
      caption: caption.trim(),
      hashtags,
      sku_ref: sku || null,
      scheduled_at: scheduledAt,
    }));
  }

  async function generate() {
    if (busy || !platforms.length || !caption.trim()) return;
    setPending("generate");
    setFeedback(null);
    try { setCaption(await onGenerateCaption({ platform: platforms[0], sku_ref: sku || null, prompt: caption.trim() })); }
    catch { setFeedback({ error: true, text: "AI 文案生成失败，请重试。" }); }
    finally { setPending(null); }
  }

  async function submit(action: "save" | "schedule") {
    if (busy) return;
    if (action === "save" ? !readyToSave : !canSchedule) return;
    const scheduledAt = action === "schedule" ? new Date(when) : null;
    if (action === "schedule" && (!scheduledAt || Number.isNaN(scheduledAt.getTime()))) {
      setFeedback({ error: true, text: "请填写有效的定时发布时间。" });
      return;
    }
    setPending(action);
    setFeedback(null);
    try {
      if (action === "save") await onSaveDraft(drafts(null));
      else await onSchedule(drafts(scheduledAt!.toISOString()));
      setFeedback({ error: false, text: action === "save" ? "草稿已保存。" : "已提交定时发布。" });
    } catch {
      setFeedback({ error: true, text: action === "save" ? "保存失败，请重试。" : "定时发布失败，请重试。" });
    } finally { setPending(null); }
  }

  return (
    <section aria-label="社媒贴文编辑器" className={panel}>
      <header className="mb-4">
        <h2 className="text-lg font-bold">贴文编辑器</h2>
        <p className="text-sm text-slate-600">每个平台生成一条贴文记录；手动发布平台只导出素材，不代为发布。</p>
      </header>
      <form onSubmit={(event: FormEvent<HTMLFormElement>) => { event.preventDefault(); void submit("schedule"); }} aria-busy={busy} className="grid gap-5 lg:grid-cols-2">
        <fieldset disabled={busy} className="space-y-3 disabled:opacity-65">
          <legend className="text-sm font-semibold">发布平台</legend>
          <p className="text-xs text-slate-600">已授权账号：{accounts.filter((account) => account.status === "connected").length} 个</p>
          <div className="max-h-64 space-y-1 overflow-y-auto rounded-xl border border-slate-200 p-2">
            {PLATFORM_PLAYBOOKS.map((playbook) => <label key={playbook.platform} className="flex items-center justify-between gap-2 rounded-lg px-2 py-1.5 text-sm hover:bg-slate-50">
              <span className="flex min-w-0 items-center gap-2">
                <input type="checkbox" aria-label={playbook.label} checked={platforms.includes(playbook.platform)} onChange={(event) => togglePlatform(playbook.platform, event.target.checked)} className="accent-fuchsia-700" />
                <span className="truncate">{playbook.emoji} {playbook.label}</span>
              </span>
              <span className={"shrink-0 rounded px-1.5 py-0.5 text-[11px] font-medium " + (playbook.publish_mode === "api" ? "bg-emerald-100 text-emerald-900" : "bg-amber-100 text-amber-900")}>
                {publishModeLabel[playbook.publish_mode]} · {accountStatusLabel[connected.has(playbook.platform) ? "connected" : "disconnected"]}
              </span>
            </label>)}
          </div>
          <label className="block text-sm font-semibold">贴文标题
            <input value={title} onChange={(event) => setTitle(event.target.value)} className={field} maxLength={200} />
          </label>
          <label className="block text-sm font-semibold">关联 SKU
            <select value={sku} onChange={(event) => setSku(event.target.value)} className={field}>
              <option value="">不关联</option>
              {skus.map((option) => <option key={option.sku} value={option.sku}>{option.sku} · {option.label}</option>)}
            </select>
          </label>
          <label className="block text-sm font-semibold">正文
            <textarea aria-label="正文" rows={6} value={caption} onChange={(event) => setCaption(event.target.value)} className={field + " resize-y"} placeholder="写贴文内容，或先写要点后用 AI 生成" />
          </label>
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-slate-600">
            <span aria-live="polite">{caption.length} 字符{limit !== null && " / 上限 " + limit}</span>
            <button type="button" disabled={busy || !platforms.length || !caption.trim()} className={button} onClick={() => void generate()}>
              <Sparkles aria-hidden="true" size={15} />{pending === "generate" ? "生成中…" : "AI 生成文案"}
            </button>
          </div>
          {overLimit && <p role="alert" className="text-xs text-rose-700">正文超出所选平台的字数上限，请精简后再排期。</p>}
        </fieldset>
        <div className="space-y-3">
          <fieldset disabled={busy} className="space-y-2 disabled:opacity-65">
            <legend className="flex items-center gap-1.5 text-sm font-semibold"><Hash aria-hidden="true" size={15} />Hashtag</legend>
            <div className="flex gap-2">
              <input aria-label="Hashtag" value={tagInput} onChange={(event) => { setTagInput(event.target.value); setTagError(null); }} onKeyDown={onTagKeyDown} className={field + " mt-0 flex-1"} placeholder="输入后按回车添加" />
              <button type="button" className={button + " mt-0"} disabled={!tagInput.trim()} onClick={addTag}>添加</button>
            </div>
            {tagError && <p role="alert" className="text-xs text-rose-700">{tagError}</p>}
            {!!hashtags.length && <ul aria-label="已添加标签" className="flex flex-wrap gap-1.5">
              {hashtags.map((tag) => <li key={tag} className="inline-flex items-center gap-1 rounded-full bg-fuchsia-50 px-2 py-0.5 text-xs font-medium text-fuchsia-900">
                {tag}
                <button type="button" aria-label={"移除标签 " + tag} className="rounded-full p-0.5 hover:bg-fuchsia-100" onClick={() => setHashtags((prev) => prev.filter((item) => item !== tag))}><X aria-hidden="true" size={12} /></button>
              </li>)}
            </ul>}
          </fieldset>
          <label className="block text-sm font-semibold">定时发布时间
            <input type="datetime-local" aria-label="定时发布时间" value={when} onChange={(event) => setWhen(event.target.value)} className={field} />
          </label>
          {!!manual.length && <p role="status" className="rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-900">
            {manual.map((platform) => getPlaybook(platform)?.label).join("、")}：需在官方创作中心人工确认发布，已阻止定时发布。
          </p>}
          {!!unauthorized.length && <p role="status" className="rounded-lg bg-slate-100 px-3 py-2 text-xs text-slate-700">
            未授权平台：{unauthorized.map((platform) => getPlaybook(platform)?.label).join("、")}，请先完成授权。
          </p>}
          <div className="flex flex-wrap gap-2">
            <button type="button" disabled={busy || !readyToSave} className={button} onClick={() => void submit("save")}><Save aria-hidden="true" size={15} />保存草稿</button>
            <button type="submit" disabled={busy || !canSchedule} className={primary}><CalendarClock aria-hidden="true" size={15} />定时发布</button>
          </div>
          {feedback && <p role={feedback.error ? "alert" : "status"} className={"text-sm " + (feedback.error ? "text-rose-700" : "text-fuchsia-900")}>{feedback.text}</p>}
        </div>
      </form>
    </section>
  );
}
