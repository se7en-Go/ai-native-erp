"use client";

import { ArrowUpRight, Bot, ClipboardCheck, LockKeyhole } from "lucide-react";
import { AUTONOMY, formatCount } from "./presentation";
import type { AutonomyDivisionItem, DivisionDestination } from "./types";

const columns = [
  { level: "L2_auto", icon: Bot, description: "已允许自动执行的低风险操作" },
  { level: "L1_proposal", icon: ClipboardCheck, description: "由 AI 拟定，必须由人工审核" },
  { level: "L0_copilot", icon: LockKeyhole, description: "高风险操作仅由人工执行" },
] as const;

export function AutonomyDivisionBoard({ items, onNavigate }: {
  items: readonly AutonomyDivisionItem[];
  onNavigate: (target: DivisionDestination) => void;
}) {
  return (
    <section aria-label="AI 自治分工矩阵" className="space-y-3 text-slate-900">
      <h2 className="text-xl font-bold">AI 自治分工矩阵</h2>
      <div className="grid gap-3 lg:grid-cols-3">
        {columns.map(({ level, icon: Icon, description }) => {
          const column = AUTONOMY[level];
          const entries = items.filter((item) => item.level === level);
          return (
            <section key={level} aria-label={column.heading} className={"rounded-2xl border p-4 " + column.tone}>
              <h3 className="flex items-center gap-2 font-bold"><Icon aria-hidden="true" size={18} />{column.heading}</h3>
              <p className="mt-1 text-sm">{description}</p>
              {entries.length ? <ul className="mt-4 space-y-2">
                {entries.map(({ id, title, description, count, target }) => {
                  const detail = <>
                    <span className="min-w-0 flex-1 break-words"><span className="block font-semibold">{title}</span>{description && <span className="block text-xs text-slate-600">{description}</span>}</span>
                    {count !== undefined && <span className="rounded-full bg-slate-100 px-2 py-1 font-semibold tabular-nums">{formatCount(count)}</span>}
                  </>;
                  const row = "flex w-full items-center gap-2 rounded-xl border border-white bg-white/85 p-3 text-left text-sm text-slate-900";
                  return <li key={id}>{target
                    ? <button type="button" className={row + " hover:border-current"} onClick={() => onNavigate(target)}>{detail}<ArrowUpRight aria-hidden="true" size={15} /></button>
                    : <div className={row}>{detail}</div>}</li>;
                })}
              </ul> : <p className="mt-4 text-sm">暂无记录</p>}
            </section>
          );
        })}
      </div>
    </section>
  );
}
