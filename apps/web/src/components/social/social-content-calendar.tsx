"use client";

import { useMemo, useState } from "react";
import { CalendarDays, ChevronLeft, ChevronRight, Plus } from "lucide-react";
import { addDays, formatTime, isoToKey, monthLabel, monthMatrix, parseKey, WEEKDAYS, weekKeys } from "./calendar-utils";
import { button, panel, postStatusLabel, postStatusTone, platformLabel } from "./presentation";
import type { SocialPost } from "./types";

const statuses = ["draft", "scheduled", "published", "failed"] as const;

export function SocialContentCalendar({ posts, anchor, timeZone = "UTC", onSelectPost, onCreatePost }: {
  posts: readonly SocialPost[];
  anchor: string;
  timeZone?: string;
  onSelectPost?: (postId: string) => void;
  onCreatePost?: (dayKey: string) => void;
}) {
  const [view, setView] = useState<"month" | "week">("month");
  const [cursor, setCursor] = useState(anchor.slice(0, 10));
  const byDay = useMemo(() => {
    const map = new Map<string, SocialPost[]>();
    for (const post of posts) {
      const key = isoToKey(post.scheduled_at ?? post.published_at);
      if (!key) continue;
      map.set(key, [...(map.get(key) ?? []), post]);
    }
    for (const list of map.values()) list.sort((a, b) => (a.scheduled_at ?? "").localeCompare(b.scheduled_at ?? ""));
    return map;
  }, [posts]);
  const cells = view === "month" ? monthMatrix(cursor) : weekKeys(cursor).map((key) => ({ key, inMonth: true }));

  function shift(step: number) {
    const date = parseKey(cursor);
    if (view === "week") setCursor(addDays(cursor, step * 7));
    else setCursor(new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + step, 1)).toISOString().slice(0, 10));
  }

  return (
    <section aria-label="内容排期日历" className={panel}>
      <header className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="flex items-center gap-2 text-[15px] font-bold text-slate-900">
            <span className="grid h-7 w-7 place-items-center rounded-lg bg-blue-50 text-blue-600 ring-1 ring-blue-100">
              <CalendarDays aria-hidden="true" size={15} />
            </span>
            内容排期日历
          </h2>
          <p className="mt-0.5 text-[12.5px] font-medium tabular-nums text-slate-600">{view === "month" ? monthLabel(cursor) : "本周 " + weekKeys(cursor)[0] + " 至 " + weekKeys(cursor)[6]}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button type="button" aria-label={view === "month" ? "上个月" : "上一周"} className={button} onClick={() => shift(-1)}><ChevronLeft aria-hidden="true" size={16} /></button>
          <button type="button" aria-label={view === "month" ? "下个月" : "下一周"} className={button} onClick={() => shift(1)}><ChevronRight aria-hidden="true" size={16} /></button>
          <div role="group" aria-label="日历视图" className="inline-flex rounded-xl border border-slate-200/90 bg-slate-100/90 p-1">
            {(["month", "week"] as const).map((mode) => (
              <button
                key={mode}
                type="button"
                aria-pressed={view === mode}
                onClick={() => setView(mode)}
                className="rounded-lg px-3 py-1.5 text-[12.5px] font-semibold text-slate-600 transition-colors hover:bg-white/70 hover:text-slate-900 aria-pressed:bg-slate-900 aria-pressed:text-white aria-pressed:shadow-xs"
              >
                {mode === "month" ? "月视图" : "周视图"}
              </button>
            ))}
          </div>
        </div>
      </header>
      <ul aria-label="状态图例" className="mb-3 flex flex-wrap gap-2 text-xs">
        {statuses.map((status) => <li key={status} className={"rounded-full border px-2 py-0.5 font-medium " + postStatusTone[status]}>{postStatusLabel[status]}</li>)}
      </ul>
      <div role="grid" aria-label="内容日历" className="overflow-hidden rounded-2xl border border-slate-200">
        <div role="row" className="grid grid-cols-7 border-b border-slate-200 bg-slate-50 text-xs font-semibold text-slate-600">
          {WEEKDAYS.map((day) => <div role="columnheader" key={day} aria-label={"周" + day} className="p-2 text-center">周{day}</div>)}
        </div>
        {Array.from({ length: Math.ceil(cells.length / 7) }, (_, rowIndex) => (
          <div role="row" key={rowIndex} className="grid grid-cols-7 border-b border-slate-100 last:border-b-0">
            {cells.slice(rowIndex * 7, rowIndex * 7 + 7).map(({ key, inMonth }) => {
              const dayPosts = byDay.get(key) ?? [];
              return (
                <div role="gridcell" key={key} aria-label={key + "，" + dayPosts.length + " 篇内容"} className={"min-h-24 min-w-0 border-r border-slate-100 p-1.5 last:border-r-0 " + (inMonth ? "bg-white" : "bg-slate-50 text-slate-400")}>
                  <div className="flex items-center justify-between gap-1">
                    <span className="text-[11.5px] font-bold tabular-nums text-slate-700">
                      {parseKey(key).getUTCDate()}
                    </span>
                    {onCreatePost && <button type="button" aria-label={"在 " + key + " 排期"} className="rounded p-0.5 text-slate-500 hover:bg-slate-100" onClick={() => onCreatePost(key)}><Plus aria-hidden="true" size={13} /></button>}
                  </div>
                  <span aria-hidden="true" className="sr-only">{key}</span>
                  <ul className="mt-1 space-y-1">
                    {dayPosts.map((post) => <li key={post.id}>
                      <button type="button" aria-label={platformLabel(post.platform) + " " + formatTime(post.scheduled_at ?? post.published_at ?? "", timeZone) + " " + postStatusLabel[post.status]}
                        onClick={() => onSelectPost?.(post.id)} disabled={!onSelectPost}
                        className={"w-full truncate rounded border px-1 py-0.5 text-left text-[11px] font-medium " + postStatusTone[post.status] + (onSelectPost ? "" : " cursor-default")}>
                        {platformLabel(post.platform)} {formatTime(post.scheduled_at ?? post.published_at ?? "", timeZone)}
                      </button>
                    </li>)}
                  </ul>
                </div>
              );
            })}
          </div>
        ))}
      </div>
      <p className="mt-2 text-[11.5px] font-medium text-slate-600">
        时间按 {timeZone} 显示；排期仅表示计划，实际发布以平台回执为准。
      </p>
    </section>
  );
}
