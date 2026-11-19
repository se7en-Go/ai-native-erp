"use client";

import React, { useMemo } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ShieldCheck } from "lucide-react";

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

export default function AiEmployeesPage() {
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
      <div className="mx-auto max-w-7xl space-y-5">
        <header className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="text-lg font-bold text-slate-900">AI 数字员工中心 / AI Employees</h1>
            <p className="text-xs text-slate-500">
              每名数字员工都绑定真实的 AI 能力与 ERP 工具；近 7 天动作、任务与成本全部来自
              `ai_proposals` 与 `usage_ledger` 真实数据。
            </p>
          </div>
          <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-200 bg-emerald-50 px-2.5 py-1 text-[11px] font-semibold text-emerald-800">
            <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" />
            L0 红线由系统强制执行
          </span>
        </header>

        {error && (
          <p
            role="alert"
            data-testid="employees-error"
            className="rounded border border-rose-200 bg-rose-50 p-2 text-xs text-rose-800"
          >
            {error.message}
          </p>
        )}

        {!canManage && (
          <p
            data-testid="employees-read-only"
            className="rounded border border-amber-200 bg-amber-50 p-2 text-xs text-amber-900"
          >
            当前角色可查看数字员工矩阵，但只有 Owner / Admin / Manager 可以启停员工。
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
            正在加载数字员工…
          </p>
        )}

        {matrix ? (
          <div data-testid="workforce-matrix">
            <AutonomyDivisionBoard items={divisionItems} onNavigate={navigate} />
          </div>
        ) : (
          <p data-testid="workforce-matrix-loading" className="text-sm text-slate-500">
            正在加载分工矩阵…
          </p>
        )}
      </div>
    </AppShell>
  );
}
