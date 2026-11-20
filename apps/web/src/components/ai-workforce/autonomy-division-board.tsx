"use client";

import { ArrowUpRight, Bot, ClipboardCheck, LockKeyhole } from "lucide-react";
import { AUTONOMY, formatCount } from "./presentation";
import type { AutonomyDivisionItem, DivisionDestination } from "./types";

const columns = [
  { level: "L2_auto", icon: Bot, description: "已允许自动执行的低风险操作", dot: "bg-emerald-500" },
  { level: "L1_proposal", icon: ClipboardCheck, description: "由 AI 拟定，必须由人工审核", dot: "bg-amber-500" },
  { level: "L0_copilot", icon: LockKeyhole, description: "高风险操作仅由人工执行", dot: "bg-rose-500" },
] as const;

export function AutonomyDivisionBoard({ items, onNavigate }: {
  items: readonly AutonomyDivisionItem[];
  onNavigate: (target: DivisionDestination) => void;
}) {
  return (
    <section aria-label="AI 自治分工矩阵" className="space-y-3 text-slate-950">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-[18px] font-bold tracking-tight">AI 自治分工矩阵</h2>
        <span className="text-[11.5px] font-medium text-slate-500">L2 自动执行 · L1 提案人审 · L0 人工把关</span>
      </div>
      <div className="grid gap-3 lg:grid-cols-3">
        {columns.map(({ level, icon: Icon, description, dot }) => {
          const column = AUTONOMY[level];
          const entries = items.filter((item) => item.level === level);
          return (
            <section
              key={level}
              aria-label={column.heading}
              className={"rounded-2xl border p-4 shadow-[0_1px_3px_rgba(15,23,42,0.04)] " + column.tone}
            >
              <h3 className="flex items-center gap-2 text-[13.5px] font-bold">
                <Icon aria-hidden="true" size={17} />
                {column.heading}
                <span className={"ml-auto h-2 w-2 rounded-full " + dot} aria-hidden="true" />
              </h3>
              <p className="mt-1 text-[12px] leading-5 opacity-80">{description}</p>
              {entries.length ? <ul className="mt-4 space-y-2">
                {entries.map(({ id, title, description, count, target }) => {
                  const detail = <>
                    <span className="min-w-0 flex-1 break-words"><span className="block font-semibold">{title}</span>{description && <span className="block text-xs text-slate-600">{description}</span>}</span>
                    {count !== undefined && <span className="shrink-0 rounded-full bg-slate-900/5 px-2 py-0.5 text-[11.5px] font-bold tabular-nums">{formatCount(count)}</span>}
                  </>;
                  const row = "flex w-full items-center gap-2 rounded-xl border border-white/80 bg-white/90 p-3 text-left text-[12.5px] text-slate-900 shadow-xs transition-all hover:border-current/30 hover:shadow-sm";
                  return <li key={id}>{target
                    ? <button type="button" className={row + " hover:border-current"} onClick={() => onNavigate(target)}>{detail}<ArrowUpRight aria-hidden="true" size={15} /></button>
                    : <div className={row}>{detail}</div>}</li>;
                })}
              </ul> : <p className="mt-4 rounded-xl border border-dashed border-current/20 p-3 text-[12px] font-medium opacity-70">暂无记录</p>}
            </section>
          );
        })}
      </div>
    </section>
  );
}
