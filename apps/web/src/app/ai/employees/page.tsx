"use client";

import React, { useMemo } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, ShieldCheck } from "lucide-react";

import { AppShell } from "@/components/app-shell";
import { useAuthSession } from "@/components/providers";
import { EmployeeCardGrid } from "@/components/ai-workforce/employee-card-grid";
import { AutonomyDivisionBoard } from "@/components/ai-workforce/autonomy-division-board";
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
import { useI18n } from "@/lib/i18n";

export default function AiEmployeesPage() {
  const { bi } = useI18n();
  const router = useRouter();
  const queryClient = useQueryClient();
  const { principal } = useAuthSession();
  const canManage = canConfigureAiGovernance(principal?.role);

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

  const toggle = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      setWorkforceEmployeeEnabled(id, enabled),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["ai", "workforce"] });
    },
  });

  const employees = useMemo(() => employeesQuery.data ?? [], [employeesQuery.data]);
  const matrix = matrixQuery.data;
  const metrics = useMemo(() => (matrix ? mapMetrics(matrix.kpis) : null), [matrix]);
  const divisionItems = useMemo(() => (matrix ? mapDivisionItems(matrix) : []), [matrix]);
  const pendingIds = toggle.isPending && toggle.variables ? [toggle.variables.id] : [];
  const error = employeesQuery.error ?? matrixQuery.error ?? toggle.error;

  function navigate(target: DivisionDestination) {
    router.push(destinationHref(target));
  }

  return (
    <AppShell
      currentPath="/ai/employees"
      pendingProposalsCount={proposalsQuery.data?.total ?? 0}
    >
      <div className="erp-page space-y-5">
        <header className="relative overflow-hidden rounded-2xl border border-slate-200/90 bg-white p-5 shadow-[0_1px_3px_rgba(15,23,42,0.06),0_10px_30px_-18px_rgba(15,23,42,0.18)]">
          <div
            aria-hidden="true"
            className="pointer-events-none absolute -top-20 -right-12 h-48 w-48 rounded-full bg-violet-100/50 blur-3xl"
          />
          <div className="relative flex flex-wrap items-start justify-between gap-3">
            <div className="flex min-w-0 items-start gap-3.5">
              <span className="grid h-12 w-12 shrink-0 place-items-center rounded-2xl bg-gradient-to-br from-violet-600 to-indigo-600 text-white shadow-lg shadow-violet-600/25">
                <Bot className="h-6 w-6" aria-hidden="true" />
              </span>
              <div className="min-w-0">
                <h1 className="text-[22px] font-bold tracking-tight text-slate-950">{bi("AI 数字员工中心", "AI employees")}</h1>
                <p className="mt-1 max-w-3xl text-[13px] leading-6 text-slate-600">
                  {bi("每名数字员工都绑定真实的 AI 能力与 ERP 工具；近 7 天动作、任务与成本全部来自 ai_proposals 与 usage_ledger 真实数据。", "Every AI employee is bound to real capabilities and ERP tools; 7-day actions, tasks and costs all come from ai_proposals and usage_ledger.")}
                </p>
                <span className="mt-2.5 inline-flex items-center gap-1.5 rounded-full border border-emerald-200/80 bg-emerald-50 px-2.5 py-0.5 text-[11.5px] font-semibold text-emerald-800">
                  <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" />
                  {bi("L0 红线由系统强制执行", "L0 guardrails enforced by the system")}
                </span>
              </div>
            </div>
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
    </AppShell>
  );
}
