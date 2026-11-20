"use client";

import { useState } from "react";
import { Activity, Bot, ChevronRight, CircleDollarSign, Shield, ToggleRight, Users } from "lucide-react";
import { AUTONOMY, CAPABILITY_LABELS, formatCount, formatUsd } from "./presentation";
import type { DigitalEmployee, WorkforceMetrics } from "./types";
import { WorkforceDialog } from "./workforce-dialog";

export function EmployeeCardGrid({ employees, metrics, onToggleEnabled, pendingEmployeeIds = [], canManage = true }: {
  employees: readonly DigitalEmployee[];
  metrics: WorkforceMetrics;
  onToggleEnabled: (id: string, enabled: boolean) => void;
  pendingEmployeeIds?: readonly string[];
  canManage?: boolean;
}) {
  const [role, setRole] = useState("");
  const [detailId, setDetailId] = useState<string | null>(null);
  const roles = [...new Set(employees.map((employee) => employee.role_title))].sort((a, b) => a.localeCompare(b, "zh-CN"));
  const activeRole = roles.includes(role) ? role : "";
  const visible = employees.filter((employee) => !activeRole || employee.role_title === activeRole);
  const detail = employees.find((employee) => employee.id === detailId);
  const mode = AUTONOMY[metrics.autonomyMode];
  const cards = [
    { label: "员工总数", value: formatCount(metrics.totalEmployees), icon: Users },
    { label: "启用中", value: formatCount(metrics.enabledEmployees), icon: ToggleRight },
    { label: "近 7 天动作", value: formatCount(metrics.actionsLast7Days), icon: Activity },
    { label: "今日 LLM 成本", value: formatUsd(metrics.llmCostTodayUsd), icon: CircleDollarSign },
    { label: "自治模式", value: mode?.badge ?? "—", icon: Shield, tone: mode?.tone },
  ];

  return (
    <section aria-label="AI 数字员工矩阵" className="space-y-5 text-slate-900">
      <section aria-label="数字员工关键指标" className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
        {cards.map(({ label, value, icon: Icon, tone }) => (
          <div key={label} className="min-w-0 rounded-2xl border border-slate-200 bg-white p-4 shadow-xs">
            <div className="mb-3 flex items-center gap-2 text-sm font-medium text-slate-600"><Icon aria-hidden="true" size={18} className="text-blue-600" />{label}</div>
            <div className={tone ? "inline-block rounded-full border px-2.5 py-1 text-xs font-semibold " + tone : "break-all text-lg font-bold tabular-nums"}>{value}</div>
          </div>
        ))}
      </section>
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-xl font-bold">AI 数字员工</h2>
          <p className="text-sm text-slate-600">按职责查看员工与自治范围</p>
        </div>
        <label className="flex items-center gap-2 text-sm font-medium">角色筛选
          <select value={activeRole} onChange={(event) => setRole(event.target.value)} className="rounded-lg border border-slate-300 bg-white p-2">
            <option value="">全部角色</option>
            {roles.map((value) => <option key={value}>{value}</option>)}
          </select>
        </label>
      </header>
      {!visible.length && <p className="rounded-xl border border-dashed border-slate-300 p-5 text-sm text-slate-600">暂无数字员工。</p>}
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {visible.map((employee) => {
          const autonomy = AUTONOMY[employee.autonomy_level];
          return (
            <article key={employee.id} className="min-w-0 rounded-2xl border border-slate-200 bg-white p-4 shadow-xs">
              <div className="flex items-start gap-3">
                <div aria-hidden="true" className="grid size-11 shrink-0 place-items-center rounded-xl bg-blue-50 text-xl text-blue-700">{employee.avatar_emoji || <Bot size={23} />}</div>
                <div className="min-w-0 flex-1 break-words">
                  <h3 className="font-bold">{employee.display_name}</h3>
                  <p className="text-sm text-slate-600">{employee.role_title} · {employee.department}</p>
                </div>
                <input type="checkbox" role="switch" aria-label={"启用" + employee.display_name} checked={employee.is_enabled}
                  disabled={!canManage || pendingEmployeeIds.includes(employee.id)} onChange={(event) => onToggleEnabled(employee.id, event.target.checked)} className="mt-2 size-4 accent-blue-600" />
              </div>
              <p className="mt-4 line-clamp-2 min-h-10 text-sm text-slate-700">{employee.description}</p>
              <footer className="mt-4 flex flex-wrap items-center justify-between gap-2 border-t border-slate-100 pt-3">
                <div className="flex flex-wrap items-center gap-2 text-xs text-slate-600">
                  <span className={"rounded-full border px-2 py-0.5 font-semibold " + (autonomy?.tone ?? "")}>{autonomy?.badge ?? "—"}</span>
                  <span>{formatCount(employee.tool_count)} 个工具</span>
                </div>
                <button type="button" aria-label={"查看" + employee.display_name + "详情"} onClick={() => setDetailId(employee.id)} className="flex items-center gap-1 rounded-lg px-2 py-1 text-sm font-semibold text-blue-700 hover:bg-blue-50">
                  详情 <ChevronRight aria-hidden="true" size={15} />
                </button>
              </footer>
            </article>
          );
        })}
      </div>
      {detail && (
        <WorkforceDialog title={detail.display_name} closeLabel="关闭详情" drawer onClose={() => setDetailId(null)}>
          <p className="font-semibold text-blue-700">{detail.role_title} · {detail.department}</p>
          <p className="mt-3 whitespace-pre-wrap break-words text-sm text-slate-700">{detail.description}</p>
          <dl className="mt-5 space-y-3 rounded-xl bg-slate-50 p-4 text-sm">
            <div><dt className="font-semibold">当前状态</dt><dd>{detail.is_enabled ? "启用中" : "已停用"}</dd></div>
            <div><dt className="font-semibold">自治级别</dt><dd>{AUTONOMY[detail.autonomy_level]?.badge ?? "—"}</dd></div>
            <div><dt className="font-semibold">工具数</dt><dd>{formatCount(detail.tool_count)}</dd></div>
            <div><dt className="font-semibold">职责能力</dt><dd>{detail.capability_codes.map((capability) => CAPABILITY_LABELS[capability]).filter(Boolean).join("、") || "—"}</dd></div>
          </dl>
        </WorkforceDialog>
      )}
    </section>
  );
}
