"use client";

import { useState } from "react";
import {
  Activity,
  ArrowUpRight,
  Bot,
  ChevronRight,
  CircleDollarSign,
  Filter,
  Shield,
  ToggleRight,
  Users,
} from "lucide-react";
import { BentoStatCard, type BentoAccent } from "@/components/ui/bento-stat-card";
import { Switch } from "@/components/ui/switch";
import { AUTONOMY, CAPABILITY_LABELS, formatCount, formatUsd } from "./presentation";
import type { DigitalEmployee, WorkforceMetrics } from "./types";
import { WorkforceDialog } from "./workforce-dialog";

const AUTONOMY_DOT: Record<DigitalEmployee["autonomy_level"], string> = {
  L2_auto: "bg-emerald-500",
  L1_proposal: "bg-amber-500",
  L0_copilot: "bg-rose-500",
};

const AUTONOMY_AVATAR: Record<DigitalEmployee["autonomy_level"], string> = {
  L2_auto: "bg-emerald-50 text-emerald-700 ring-1 ring-emerald-100",
  L1_proposal: "bg-blue-50 text-blue-700 ring-1 ring-blue-100",
  L0_copilot: "bg-rose-50 text-rose-700 ring-1 ring-rose-100",
};

const AUTONOMY_ACCENT: Record<DigitalEmployee["autonomy_level"], BentoAccent> = {
  L2_auto: "emerald",
  L1_proposal: "amber",
  L0_copilot: "rose",
};

export function EmployeeCardGrid({
  employees,
  metrics,
  onToggleEnabled,
  pendingEmployeeIds = [],
  canManage = true,
}: {
  employees: readonly DigitalEmployee[];
  metrics: WorkforceMetrics;
  onToggleEnabled: (id: string, enabled: boolean) => void;
  pendingEmployeeIds?: readonly string[];
  canManage?: boolean;
}) {
  const [role, setRole] = useState("");
  const [detailId, setDetailId] = useState<string | null>(null);
  const roles = [...new Set(employees.map((employee) => employee.role_title))].sort((a, b) =>
    a.localeCompare(b, "zh-CN")
  );
  const activeRole = roles.includes(role) ? role : "";
  const visible = employees.filter(
    (employee) => !activeRole || employee.role_title === activeRole
  );
  const detail = employees.find((employee) => employee.id === detailId);
  const mode = AUTONOMY[metrics.autonomyMode];

  return (
    <section aria-label="AI 数字员工矩阵" className="space-y-5 text-slate-950">
      <section
        aria-label="数字员工关键指标"
        className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5"
      >
        <BentoStatCard
          label="员工总数"
          value={formatCount(metrics.totalEmployees)}
          icon={Users}
          accent="blue"
          hint="租户内配置的数字员工"
        />
        <BentoStatCard
          label="启用中"
          value={formatCount(metrics.enabledEmployees)}
          icon={ToggleRight}
          accent="emerald"
          badge="在线"
          hint="正在接管真实业务动作"
        />
        <BentoStatCard
          label="近 7 天动作"
          value={formatCount(metrics.actionsLast7Days)}
          icon={Activity}
          accent="violet"
          badge="7 天"
          badgeTone="violet"
          hint="来自 ai_proposals 的真实动作数"
        />
        <BentoStatCard
          label="今日 LLM 成本"
          value={formatUsd(metrics.llmCostTodayUsd)}
          icon={CircleDollarSign}
          accent="amber"
          hint="usage_ledger 逐笔记账，保留 Decimal 原值"
        />
        <BentoStatCard
          label="自治模式"
          value={mode?.badge ?? "—"}
          icon={Shield}
          accent={AUTONOMY_ACCENT[metrics.autonomyMode] ?? "slate"}
          hint="全体员工的默认自治级别"
        />
      </section>

      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-[18px] font-bold tracking-tight text-slate-950">AI 数字员工</h2>
          <p className="mt-0.5 text-[12.5px] text-slate-600">按职责查看员工与自治范围</p>
        </div>
        <label className="inline-flex items-center gap-2 rounded-xl border border-slate-200 bg-white px-3 py-1.5 text-[12.5px] font-semibold text-slate-700 shadow-xs">
          <Filter className="h-3.5 w-3.5 text-slate-400" aria-hidden="true" />
          角色筛选
          <select
            value={activeRole}
            onChange={(event) => setRole(event.target.value)}
            className="rounded-lg border border-slate-200 bg-slate-50 px-2 py-1 text-[12.5px] font-medium text-slate-800 outline-none focus:border-blue-500"
          >
            <option value="">全部角色</option>
            {roles.map((value) => (
              <option key={value}>{value}</option>
            ))}
          </select>
        </label>
      </header>

      {!visible.length && (
        <p className="rounded-2xl border border-dashed border-slate-300 bg-slate-50 p-5 text-[13px] font-medium text-slate-600">
          暂无数字员工。
        </p>
      )}

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {visible.map((employee) => {
          const autonomy = AUTONOMY[employee.autonomy_level];
          const pending = pendingEmployeeIds.includes(employee.id);
          return (
            <article
              key={employee.id}
              className="group relative flex min-w-0 flex-col rounded-2xl border border-slate-200/90 bg-white p-4 shadow-[0_1px_3px_rgba(15,23,42,0.06),0_6px_16px_-6px_rgba(15,23,42,0.05)] transition-all duration-200 hover:-translate-y-0.5 hover:border-blue-300 hover:shadow-[0_6px_20px_-8px_rgba(37,99,235,0.35)]"
            >
              <div className="flex items-start gap-3">
                <div className="relative shrink-0">
                  <div
                    aria-hidden="true"
                    className={`grid size-11 place-items-center rounded-2xl text-xl ${
                      AUTONOMY_AVATAR[employee.autonomy_level] ?? "bg-slate-100 text-slate-700"
                    }`}
                  >
                    {employee.avatar_emoji || <Bot size={22} />}
                  </div>
                  <span
                    className={`absolute -right-0.5 -bottom-0.5 h-3 w-3 rounded-full ring-2 ring-white ${
                      employee.is_enabled ? "bg-emerald-500" : "bg-slate-300"
                    }`}
                    aria-hidden="true"
                  />
                </div>
                <div className="min-w-0 flex-1">
                  <h3 className="truncate text-[14.5px] font-bold tracking-tight text-slate-950">
                    {employee.display_name}
                  </h3>
                  <p className="mt-0.5 truncate text-[12px] text-slate-600">
                    {employee.role_title} · {employee.department}
                  </p>
                </div>
                <Switch
                  checked={employee.is_enabled}
                  disabled={!canManage || pending}
                  onCheckedChange={(next) => onToggleEnabled(employee.id, next)}
                  aria-label={"启用" + employee.display_name}
                  size="sm"
                />
              </div>

              <p className="mt-3.5 line-clamp-2 min-h-10 text-[12.5px] leading-5 text-slate-700">
                {employee.description}
              </p>

              <footer className="mt-auto flex flex-wrap items-center justify-between gap-2 border-t border-slate-100 pt-3">
                <div className="flex flex-wrap items-center gap-2 text-[11.5px] text-slate-600">
                  <span
                    className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 font-semibold ${
                      autonomy?.tone ?? "border-slate-200 bg-slate-50 text-slate-700"
                    }`}
                  >
                    <span
                      className={`h-1.5 w-1.5 rounded-full ${
                        AUTONOMY_DOT[employee.autonomy_level] ?? "bg-slate-400"
                      }`}
                      aria-hidden="true"
                    />
                    {autonomy?.badge ?? "—"}
                  </span>
                  <span className="font-medium text-slate-500">
                    {formatCount(employee.tool_count)} 个工具
                  </span>
                </div>
                <button
                  type="button"
                  aria-label={"查看" + employee.display_name + "详情"}
                  onClick={() => setDetailId(employee.id)}
                  className="inline-flex items-center gap-1 rounded-lg px-2 py-1 text-[12.5px] font-semibold text-blue-700 transition-colors hover:bg-blue-50"
                >
                  详情
                  <ChevronRight aria-hidden="true" size={14} />
                </button>
              </footer>
            </article>
          );
        })}
      </div>

      {detail && (
        <WorkforceDialog
          title={detail.display_name}
          closeLabel="关闭详情"
          drawer
          onClose={() => setDetailId(null)}
        >
          <p className="flex items-center gap-1.5 text-[12.5px] font-semibold text-blue-700">
            {detail.role_title} · {detail.department}
            <ArrowUpRight className="h-3.5 w-3.5" aria-hidden="true" />
          </p>
          <p className="mt-3 whitespace-pre-wrap break-words text-[13px] leading-6 text-slate-700">
            {detail.description}
          </p>
          <dl className="mt-5 space-y-3 rounded-2xl border border-slate-200 bg-slate-50 p-4 text-[12.5px]">
            <div>
              <dt className="font-semibold text-slate-500">当前状态</dt>
              <dd className="mt-0.5 font-semibold text-slate-900">
                {detail.is_enabled ? "启用中" : "已停用"}
              </dd>
            </div>
            <div>
              <dt className="font-semibold text-slate-500">自治级别</dt>
              <dd className="mt-0.5 font-semibold text-slate-900">
                {AUTONOMY[detail.autonomy_level]?.badge ?? "—"}
              </dd>
            </div>
            <div>
              <dt className="font-semibold text-slate-500">工具数</dt>
              <dd className="mt-0.5 font-semibold tabular-nums text-slate-900">
                {formatCount(detail.tool_count)}
              </dd>
            </div>
            <div>
              <dt className="font-semibold text-slate-500">职责能力</dt>
              <dd className="mt-0.5 font-semibold text-slate-900">
                {detail.capability_codes
                  .map((capability) => CAPABILITY_LABELS[capability])
                  .filter(Boolean)
                  .join("、") || "—"}
              </dd>
            </div>
          </dl>
        </WorkforceDialog>
      )}
    </section>
  );
}
