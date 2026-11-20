"use client";

import React, { useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, Plus, Radar, ShieldCheck, Sparkles } from "lucide-react";

import { AppShell } from "@/components/app-shell";
import { useAuthSession } from "@/components/providers";
import { EmployeeCardGrid } from "@/components/ai-workforce/employee-card-grid";
import { AutonomyDivisionBoard } from "@/components/ai-workforce/autonomy-division-board";
import { WorkforceDialog } from "@/components/ai-workforce/workforce-dialog";
import { AUTONOMY, CAPABILITY_LABELS, field } from "@/components/ai-workforce/presentation";
import type { DivisionDestination } from "@/components/ai-workforce/types";
import { canConfigureAiGovernance } from "@/lib/auth-cookie";
import { fetchPendingProposalsCount } from "@/lib/data-source";
import {
  destinationHref,
  fetchWorkforceEmployees,
  fetchWorkforceMatrix,
  setWorkforceEmployeeEnabled,
} from "@/lib/phase3b-data";
import { mapDivisionItems, mapMetrics } from "@/lib/api-client/phase3b";
import {
  createCustomAiEmployee,
  dispatchPatrolEmployee,
  fetchPatrolRoster,
  isCustomEmployeeFormReady,
  toAiEmployeeCreatePayload,
  type CustomEmployeeForm,
  type PatrolDispatchRead,
} from "@/lib/api-client/pack-d-patrol";
import type { AiEmployeeCreate } from "@/lib/api-client/types.gen";
import { AiCapabilitySchema, AutonomyLevelSchema } from "@/lib/contracts";
import type { AiCapability, AutonomyLevel } from "@/lib/contracts";
import { useI18n } from "@/lib/i18n";

const CAPABILITY_ORDER: readonly AiCapability[] = AiCapabilitySchema.options;
const AUTONOMY_ORDER: readonly AutonomyLevel[] = AutonomyLevelSchema.options;

const EMPTY_FORM: CustomEmployeeForm = {
  code: "",
  display_name: "",
  role_title: "",
  department: "",
  capability: "cs_support",
  autonomy_level: "L1_proposal",
  persona_prompt: "",
};

export default function AiEmployeesPage() {
  const { bi } = useI18n();
  const router = useRouter();
  const queryClient = useQueryClient();
  const { principal } = useAuthSession();
  const canManage = canConfigureAiGovernance(principal?.role);

  const [createOpen, setCreateOpen] = useState(false);
  const [form, setForm] = useState<CustomEmployeeForm>(EMPTY_FORM);
  const [createError, setCreateError] = useState<string | null>(null);
  const [createdName, setCreatedName] = useState<string | null>(null);
  const [dispatched, setDispatched] = useState<Record<string, PatrolDispatchRead>>({});

  const proposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () => fetchPendingProposalsCount(),
  });
  const employeesQuery = useQuery({
    queryKey: ["ai", "workforce", "employees"],
    queryFn: fetchWorkforceEmployees,
  });
  const matrixQuery = useQuery({
    queryKey: ["ai", "workforce", "matrix"],
    queryFn: fetchWorkforceMatrix,
  });
  // The cards render the UI shape, which drops `code`; the patrol endpoint needs
  // the real backend code, so the roster is joined by `id` here.
  const rosterQuery = useQuery({
    queryKey: ["ai", "patrol", "roster"],
    queryFn: fetchPatrolRoster,
  });

  const toggle = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      setWorkforceEmployeeEnabled(id, enabled),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["ai", "workforce"] });
    },
  });

  const createEmployee = useMutation({
    mutationFn: (payload: AiEmployeeCreate) => createCustomAiEmployee(payload),
    onSuccess: (employee) => {
      setCreateError(null);
      setCreatedName(employee.display_name);
      setCreateOpen(false);
      setForm(EMPTY_FORM);
      void queryClient.invalidateQueries({ queryKey: ["ai", "workforce"] });
      void queryClient.invalidateQueries({ queryKey: ["ai", "patrol", "roster"] });
    },
    onError: () => {
      setCreatedName(null);
      setCreateError(bi("创建失败，请检查员工编码是否重复后重试。", "Creation failed. Check that the employee code is unique and retry."));
    },
  });

  const patrolDispatch = useMutation({
    mutationFn: (employeeCode: string) => dispatchPatrolEmployee(employeeCode),
    onSuccess: (result, employeeCode) => {
      setDispatched((prev) => ({ ...prev, [employeeCode]: result }));
    },
  });

  const employees = useMemo(() => employeesQuery.data ?? [], [employeesQuery.data]);
  const matrix = matrixQuery.data;
  const metrics = useMemo(() => (matrix ? mapMetrics(matrix.kpis) : null), [matrix]);
  const divisionItems = useMemo(() => (matrix ? mapDivisionItems(matrix) : []), [matrix]);
  const pendingIds = toggle.isPending && toggle.variables ? [toggle.variables.id] : [];
  const error = employeesQuery.error ?? matrixQuery.error ?? toggle.error;

  const codeById = useMemo(() => {
    const map = new Map<string, string>();
    for (const row of rosterQuery.data ?? []) map.set(row.id, row.code);
    return map;
  }, [rosterQuery.data]);

  function navigate(target: DivisionDestination) {
    router.push(destinationHref(target));
  }

  function submitCreate(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!isCustomEmployeeFormReady(form)) return;
    createEmployee.mutate(toAiEmployeeCreatePayload(form));
  }

  const formReady = isCustomEmployeeFormReady(form);

  return (
    <AppShell
      currentPath="/ai/employees"
      pendingProposalsCount={proposalsQuery.data?.total ?? 0}
    >
      <div className="erp-page space-y-5">
        <header className="relative overflow-hidden rounded-[20px] border border-[#E5E2DC] bg-white/95 p-5 shadow-[inset_0_1px_0_rgba(255,255,255,0.95),0_1px_2px_rgba(24,24,27,0.05),0_10px_26px_-14px_rgba(24,24,27,0.09)]">
          <div
            aria-hidden="true"
            className="pointer-events-none absolute -top-20 -right-12 h-48 w-48 rounded-full bg-emerald-200/35 blur-3xl"
          />
          <div className="relative flex flex-wrap items-start justify-between gap-3">
            <div className="flex min-w-0 items-start gap-3.5">
              <span className="grid h-12 w-12 shrink-0 place-items-center rounded-2xl bg-gradient-to-br from-[#18181B] to-[#065F46] text-white shadow-lg shadow-emerald-950/20">
                <Bot className="h-6 w-6" aria-hidden="true" />
              </span>
              <div className="min-w-0">
                <h1 className="text-[22px] font-bold tracking-tight text-slate-950">{bi("AI 数字员工中心", "AI employees")}</h1>
                <p className="mt-1 max-w-3xl text-[13px] leading-6 text-slate-600">
                  {bi("每名数字员工都绑定真实的 AI 能力与 ERP 工具；近 7 天动作、任务与成本全部来自 ai_proposals 与 usage_ledger 真实数据。", "Every AI employee is bound to real capabilities and ERP tools; 7-day actions, tasks and costs all come from ai_proposals and usage_ledger.")}
                </p>
                <div className="mt-2.5 flex flex-wrap items-center gap-2">
                  <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-200/80 bg-emerald-50 px-2.5 py-0.5 text-[11.5px] font-semibold text-emerald-800">
                    <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" />
                    {bi("L0 红线由系统强制执行", "L0 guardrails enforced by the system")}
                  </span>
                  <span className="inline-flex items-center gap-1.5 rounded-full border border-slate-200 bg-slate-50 px-2.5 py-0.5 text-[11.5px] font-semibold text-slate-700">
                    <Radar className="h-3.5 w-3.5" aria-hidden="true" />
                    {bi("主动巡检可比人先发现风险", "Proactive patrol spots risk first")}
                  </span>
                </div>
              </div>
            </div>

            {canManage && (
              <button
                type="button"
                data-testid="open-create-employee-btn"
                onClick={() => {
                  setCreateError(null);
                  setCreatedName(null);
                  setCreateOpen(true);
                }}
                className="inline-flex shrink-0 items-center gap-1.5 rounded-xl bg-[#18181B] px-3.5 py-2 text-[13px] font-semibold text-white shadow-sm transition-colors hover:bg-emerald-700 focus-visible:outline-2 focus-visible:outline-emerald-700 focus-visible:outline-offset-2"
              >
                <Plus className="h-4 w-4" aria-hidden="true" />
                {bi("新增自定义 AI 员工", "Add custom AI employee")}
              </button>
            )}
          </div>
        </header>

        {error && (
          <p
            role="alert"
            data-testid="employees-error"
            className="rounded-2xl border border-rose-200 bg-rose-50 px-4 py-2.5 text-[12.5px] font-medium text-rose-800"
          >
            {error.message}
          </p>
        )}

        {createdName && (
          <p
            data-testid="employee-create-success"
            role="status"
            className="rounded-2xl border border-emerald-200 bg-emerald-50 px-4 py-2.5 text-[12.5px] font-semibold text-emerald-900"
          >
            {bi(`已创建自定义 AI 员工：${createdName}`, `Custom AI employee created: ${createdName}`)}
          </p>
        )}

        {!canManage && (
          <p
            data-testid="employees-read-only"
            className="rounded-2xl border border-amber-200 bg-amber-50 px-4 py-2.5 text-[12.5px] font-medium text-amber-900"
          >
            {bi("当前角色可查看数字员工矩阵，但只有 Owner / Admin / Manager 可以启停员工。", "Your role can view the matrix, but only Owner / Admin / Manager can enable or disable employees.")}
          </p>
        )}

        {metrics ? (
          <EmployeeCardGrid
            employees={employees}
            metrics={metrics}
            onToggleEnabled={(id, enabled) => toggle.mutate({ id, enabled })}
            pendingEmployeeIds={pendingIds}
            canManage={canManage}
          />
        ) : (
          <p data-testid="employees-loading" className="text-sm text-slate-500">
            {bi("正在加载数字员工…", "Loading AI employees…")}
          </p>
        )}

        {employees.length > 0 && codeById.size > 0 ? (
          <section
            data-testid="employee-dispatch-board"
            aria-label={bi("巡检派工台", "Patrol dispatch board")}
            className="rounded-2xl border border-slate-200/90 bg-white p-4 shadow-[0_1px_3px_rgba(15,23,42,0.06),0_6px_16px_-6px_rgba(15,23,42,0.05)]"
          >
            <header className="mb-3 flex flex-wrap items-center justify-between gap-2">
              <div>
                <h2 className="flex items-center gap-2 text-[15px] font-bold tracking-tight text-slate-950">
                  <span className="grid h-7 w-7 place-items-center rounded-lg bg-emerald-50 text-emerald-700 ring-1 ring-emerald-100">
                    <Radar aria-hidden="true" size={15} />
                  </span>
                  {bi("呼叫巡检", "Call for patrol")}
                </h2>
                <p className="mt-1 text-[12.5px] leading-6 text-slate-600">
                  {bi(
                    "每名员工都能立即跑一次只读巡检，结果来自租户范围内的真实 ERP 数据，并会生成跟进任务。",
                    "Every employee can run a read-only patrol immediately; results come from tenant-scoped ERP rows and open a follow-up task."
                  )}
                </p>
              </div>
            </header>

            <ul className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
              {employees.map((employee) => {
                const code = codeById.get(employee.id);
                if (!code) return null;
                const result = dispatched[code];
                const pending = patrolDispatch.isPending && patrolDispatch.variables === code;
                return (
                  <li
                    key={employee.id}
                    className="flex min-w-0 flex-col rounded-xl border border-slate-200 bg-slate-50/60 p-3"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="flex min-w-0 items-center gap-2">
                        <span
                          aria-hidden="true"
                          className="grid size-8 shrink-0 place-items-center rounded-xl bg-white text-base ring-1 ring-slate-200"
                        >
                          {employee.avatar_emoji || <Bot size={16} />}
                        </span>
                        <span className="min-w-0">
                          <span
                            className="block truncate font-mono text-[12.5px] font-bold text-slate-900"
                            title={employee.display_name}
                          >
                            {code}
                          </span>
                          <span className="block truncate text-[11.5px] text-slate-600">
                            {employee.role_title}
                          </span>
                        </span>
                      </span>
                      <button
                        type="button"
                        data-testid={`employee-dispatch-${code}`}
                        disabled={pending}
                        aria-label={bi(
                          `呼叫${employee.display_name}巡检`,
                          `Run patrol with ${employee.display_name}`
                        )}
                        onClick={() => patrolDispatch.mutate(code)}
                        className="inline-flex shrink-0 items-center gap-1 rounded-lg border border-emerald-200 bg-white px-2 py-1 text-[12px] font-semibold text-emerald-800 transition-colors hover:bg-emerald-50 disabled:opacity-50"
                      >
                        <Radar aria-hidden="true" size={13} />
                        {pending ? bi("巡检中…", "Patrolling…") : bi("呼叫巡检", "Patrol")}
                      </button>
                    </div>

                    {result ? (
                      <div
                        data-testid={`employee-dispatch-result-${code}`}
                        className="mt-2 rounded-lg border border-emerald-200 bg-emerald-50 p-2 text-[11.5px] leading-5 text-emerald-900"
                      >
                        <p className="font-semibold">
                          {bi("已创建跟进任务", "Follow-up task created")} · {result.employee_name}
                        </p>
                        <p className="mt-0.5">
                          {bi(result.action_summary_zh, result.action_summary_en)}
                        </p>
                        {result.findings[0] ? (
                          <p className="mt-0.5 text-[11px] text-emerald-800">
                            {bi(result.findings[0].title_zh, result.findings[0].title_en)} ·{" "}
                            {result.findings[0].count}
                          </p>
                        ) : null}
                        <a
                          href={result.recommended_href}
                          className="mt-1 inline-block font-semibold text-emerald-800 underline decoration-emerald-300 underline-offset-2"
                        >
                          {bi("查看跟进", "Open follow-up")}
                        </a>
                      </div>
                    ) : null}
                  </li>
                );
              })}
            </ul>

            {patrolDispatch.error ? (
              <p
                role="alert"
                data-testid="employee-dispatch-error"
                className="mt-2 rounded-xl border border-rose-200 bg-rose-50 px-3 py-2 text-[12px] font-medium text-rose-800"
              >
                {bi("派工失败，请稍后重试。", "Dispatch failed. Please retry.")}
              </p>
            ) : null}
          </section>
        ) : null}

        {matrix ? (
          <div data-testid="workforce-matrix">
            <AutonomyDivisionBoard items={divisionItems} onNavigate={navigate} />
          </div>
        ) : (
          <p data-testid="workforce-matrix-loading" className="text-sm text-slate-500">
            {bi("正在加载分工矩阵…", "Loading division matrix…")}
          </p>
        )}
      </div>

      {createOpen && (
        <WorkforceDialog
          title={bi("新增自定义 AI 员工", "Add custom AI employee")}
          closeLabel={bi("关闭新增员工", "Close create employee")}
          onClose={() => setCreateOpen(false)}
        >
          <form data-testid="create-employee-form" onSubmit={submitCreate} className="space-y-3">
            <p className="rounded-xl border border-emerald-200/80 bg-emerald-50/70 px-3 py-2 text-[12px] leading-5 text-emerald-900">
              <Sparkles className="mr-1 inline h-3.5 w-3.5" aria-hidden="true" />
              {bi(
                "员工编码是巡检与派工的唯一标识，创建后即出现在呼叫巡检列表。工具授权范围严格跟随所选能力，不会被悄悄放大。",
                "The code is the unique patrol and dispatch identifier; a created employee appears in the patrol list immediately. Tool grants follow the selected capability and are never silently widened."
              )}
            </p>

            <label className="block text-[12.5px] font-semibold text-slate-700">
              {bi("员工编码", "Employee code")}
              <input
                data-testid="create-employee-code"
                value={form.code}
                onChange={(event) => setForm((prev) => ({ ...prev, code: event.target.value }))}
                placeholder="custom-led-sales"
                className={field}
                required
              />
            </label>

            <label className="block text-[12.5px] font-semibold text-slate-700">
              {bi("展示名称", "Display name")}
              <input
                data-testid="create-employee-name"
                value={form.display_name}
                onChange={(event) =>
                  setForm((prev) => ({ ...prev, display_name: event.target.value }))
                }
                placeholder="小灯"
                className={field}
                required
              />
            </label>

            <label className="block text-[12.5px] font-semibold text-slate-700">
              {bi("岗位名称", "Role title")}
              <input
                data-testid="create-employee-role"
                value={form.role_title}
                onChange={(event) =>
                  setForm((prev) => ({ ...prev, role_title: event.target.value }))
                }
                placeholder="照明方案顾问"
                className={field}
                required
              />
            </label>

            <label className="block text-[12.5px] font-semibold text-slate-700">
              {bi("所属部门", "Department")}
              <input
                data-testid="create-employee-department"
                value={form.department}
                onChange={(event) =>
                  setForm((prev) => ({ ...prev, department: event.target.value }))
                }
                placeholder="商务中心"
                className={field}
                required
              />
            </label>

            <div className="grid gap-3 sm:grid-cols-2">
              <label className="block text-[12.5px] font-semibold text-slate-700">
                {bi("职责能力", "Capability")}
                <select
                  data-testid="create-employee-capability"
                  value={form.capability}
                  onChange={(event) =>
                    setForm((prev) => ({
                      ...prev,
                      capability: event.target.value as AiCapability,
                    }))
                  }
                  className={field}
                >
                  {CAPABILITY_ORDER.map((capability) => (
                    <option key={capability} value={capability}>
                      {CAPABILITY_LABELS[capability]}
                    </option>
                  ))}
                </select>
              </label>

              <label className="block text-[12.5px] font-semibold text-slate-700">
                {bi("自治级别", "Autonomy level")}
                <select
                  data-testid="create-employee-autonomy"
                  value={form.autonomy_level}
                  onChange={(event) =>
                    setForm((prev) => ({
                      ...prev,
                      autonomy_level: event.target.value as AutonomyLevel,
                    }))
                  }
                  className={field}
                >
                  {AUTONOMY_ORDER.map((level) => (
                    <option key={level} value={level}>
                      {AUTONOMY[level].heading}
                    </option>
                  ))}
                </select>
              </label>
            </div>

            <label className="block text-[12.5px] font-semibold text-slate-700">
              {bi("人设提示词（可选）", "Persona prompt (optional)")}
              <textarea
                data-testid="create-employee-persona"
                value={form.persona_prompt}
                onChange={(event) =>
                  setForm((prev) => ({ ...prev, persona_prompt: event.target.value }))
                }
                rows={3}
                placeholder={bi("例如：只回答照明选型问题，报价一律引用实时 ERP 数据。", "e.g. Answer lighting selection only; quotes must cite live ERP data.")}
                className={field}
              />
            </label>

            {createError && (
              <p role="alert" data-testid="create-employee-error" className="text-[12px] font-medium text-rose-700">
                {createError}
              </p>
            )}

            <div className="flex items-center justify-end gap-2 border-t border-slate-200 pt-3">
              <button
                type="button"
                onClick={() => setCreateOpen(false)}
                className="rounded-xl border border-slate-200 bg-white px-3 py-2 text-[13px] font-semibold text-slate-700 hover:bg-slate-50"
              >
                {bi("取消", "Cancel")}
              </button>
              <button
                type="submit"
                data-testid="create-employee-submit"
                disabled={!formReady || createEmployee.isPending}
                className="rounded-xl bg-[#18181B] px-3.5 py-2 text-[13px] font-semibold text-white transition-colors hover:bg-emerald-700 disabled:opacity-50"
              >
                {createEmployee.isPending
                  ? bi("创建中…", "Creating…")
                  : bi("创建员工", "Create employee")}
              </button>
            </div>
          </form>
        </WorkforceDialog>
      )}
    </AppShell>
  );
}
