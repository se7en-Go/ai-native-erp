"use client";

import { useState } from "react";
import { MessageCircle, UserPlus } from "lucide-react";
import { field, panel, primary, sentimentLabel, sentimentTone, platformLabel } from "./presentation";
import type { SocialMention, SocialSentiment } from "./types";

const sentiments: SocialSentiment[] = ["positive", "neutral", "negative", "lead_intent"];

export function SocialMentionsFeed({ mentions, onConvertToLead, pendingId }: {
  mentions: readonly SocialMention[];
  onConvertToLead: (mentionId: string) => void | Promise<void>;
  pendingId?: string | null;
}) {
  const [sentiment, setSentiment] = useState<SocialSentiment | "">("");
  const [error, setError] = useState<string | null>(null);
  const visible = mentions.filter((mention) => !sentiment || mention.sentiment === sentiment);

  return (
    <section aria-label="社媒舆情监听" className={panel}>
      <header className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="flex items-center gap-2 text-[15px] font-bold text-slate-900">
            <span className="grid h-7 w-7 place-items-center rounded-lg bg-violet-50 text-violet-600 ring-1 ring-violet-100">
              <MessageCircle aria-hidden="true" size={15} />
            </span>
            舆情监听与转线索
          </h2>
          <p className="mt-0.5 text-[12.5px] leading-6 text-slate-600">
            仅展示公开提及内容；转线索后写入 ERP 线索池。
          </p>
        </div>
        <label className="text-sm font-medium">情感筛选
          <select value={sentiment} onChange={(event) => setSentiment(event.target.value as SocialSentiment | "")} className={field}>
            <option value="">全部情感</option>
            {sentiments.map((value) => <option key={value} value={value}>{sentimentLabel[value]}</option>)}
          </select>
        </label>
      </header>
      {error && <p role="alert" className="mb-2 text-sm text-rose-700">{error}</p>}
      {!visible.length ? <p className="rounded-xl border border-dashed border-slate-300 bg-slate-50/70 p-4 text-[12.5px] font-medium text-slate-600">没有符合条件的提及。</p> : (
        <ul className="space-y-2">
          {visible.map((mention) => (
            <li
              key={mention.id}
              className="flex flex-wrap items-start justify-between gap-3 rounded-xl border border-slate-200 bg-white p-3 shadow-[0_1px_2px_rgba(15,23,42,0.04)] transition-shadow hover:shadow-[0_4px_14px_rgba(15,23,42,0.08)]"
            >
              <div className="min-w-0 flex-1">
                <p className="flex flex-wrap items-center gap-1.5 text-xs text-slate-600">
                  <span className="rounded bg-slate-100 px-1.5 py-0.5 font-semibold text-slate-800">{platformLabel(mention.platform)}</span>
                  <span className="font-semibold text-slate-800">{mention.author_handle}</span>
                  <span>{mention.occurred_at.slice(0, 10)}</span>
                  <span className={"rounded-full px-2 py-0.5 text-[10.5px] font-semibold " + sentimentTone[mention.sentiment]}>{sentimentLabel[mention.sentiment]}</span>
                </p>
                <p className="mt-1 break-words text-sm text-slate-800">{mention.content_text}</p>
              </div>
              {mention.converted_lead_id
                ? <span className="rounded-full bg-emerald-100 px-2.5 py-1 text-xs font-semibold text-emerald-900">已转线索</span>
                : <button type="button" aria-label={"转线索 " + mention.author_handle} disabled={pendingId === mention.id} className={primary}
                  onClick={() => { setError(null); void Promise.resolve(onConvertToLead(mention.id)).catch(() => setError("转线索失败，请重试。")); }}>
                  <UserPlus aria-hidden="true" size={15} />转线索
                </button>}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
