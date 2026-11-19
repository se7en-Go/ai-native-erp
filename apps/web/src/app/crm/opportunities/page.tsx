"use client";

import React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { AppShell } from "@/components/app-shell";
import type { OpportunityRead, OpportunityStage } from "@/lib/api-client/types.gen";
import { formatMoney } from "@/lib/contracts";
import { fetchPendingProposalsCount } from "@/lib/data-source";
import { fetchPhase3aOpportunities, patchPhase3aOpportunityStage } from "@/lib/phase3a-data";

const STAGES: OpportunityStage[] = [
  "discovery",
  "sampling",
  "quoting",
  "negotiating",
  "closed_won",
  "closed_lost",
];

export default function CrmOpportunitiesPage() {
  const queryClient = useQueryClient();

  const proposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () => fetchPendingProposalsCount(),
  });
  const opportunitiesQuery = useQuery({
    queryKey: ["crm", "opportunities"],
    queryFn: () => fetchPhase3aOpportunities(),
  });
  const moveStage = useMutation({
    mutationFn: ({ id, stage }: { id: string; stage: OpportunityStage }) =>
      patchPhase3aOpportunityStage(id, stage),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["crm", "opportunities"] }),
  });

  const opportunities = opportunitiesQuery.data?.items ?? [];
  const byStage = (stage: OpportunityStage): OpportunityRead[] =>
    opportunities.filter((opportunity) => opportunity.stage === stage);

  return (
    <AppShell
      currentPath="/crm/opportunities"
      pendingProposalsCount={proposalsQuery.data?.total ?? 0}
    >
      <div className="mx-auto max-w-6xl space-y-4">
        <div>
          <h1 className="text-lg font-bold text-slate-900">商机阶段看板 / Opportunities</h1>
          <p className="text-xs text-slate-500">
            拖拽替代方案：使用卡片上的阶段选择器推进商机，写操作经租户作用域 API 校验。
          </p>
        </div>

        {(opportunitiesQuery.isError ?? false) && (
          <p role="alert" className="rounded border border-rose-200 bg-rose-50 p-2 text-xs text-rose-800">
            {opportunitiesQuery.error?.message}
          </p>
        )}

        <div data-testid="opportunity-board" className="grid gap-2 overflow-x-auto md:grid-cols-3 xl:grid-cols-6">
          {STAGES.map((stage) => (
            <section
              key={stage}
              data-testid={`opportunity-column-${stage}`}
              className="min-w-[180px] rounded-lg border border-slate-200 bg-white p-2"
            >
              <header className="flex items-center justify-between px-1 pb-1 text-[10px] font-semibold uppercase tracking-wide text-slate-500">
                <span>{stage}</span>
                <span>{byStage(stage).length}</span>
              </header>
              <div className="space-y-2">
                {byStage(stage).map((opportunity) => (
                  <article
                    key={opportunity.id}
                    data-testid={`opportunity-card-${opportunity.id}`}
                    className="rounded border border-slate-200 bg-slate-50 p-2 text-xs"
                  >
                    <p className="font-medium text-slate-800">{opportunity.title}</p>
                    <p className="text-[10px] text-slate-500">
                      {opportunity.company_name ?? opportunity.customer_id.slice(0, 8)}
                    </p>
                    <p className="mt-1 font-semibold text-slate-700">
                      {formatMoney(opportunity.expected_amount, opportunity.currency)}
                    </p>
                    <select
                      aria-label={`Move ${opportunity.title}`}
                      value={opportunity.stage}
                      onChange={(event) =>
                        moveStage.mutate({
                          id: opportunity.id,
                          stage: event.target.value as OpportunityStage,
                        })
                      }
                      className="mt-1 w-full rounded border border-slate-300 bg-white px-1 py-0.5 text-[10px]"
                    >
                      {STAGES.map((option) => (
                        <option key={option} value={option}>
                          {option}
                        </option>
                      ))}
                    </select>
                  </article>
                ))}
              </div>
            </section>
          ))}
        </div>
      </div>
    </AppShell>
  );
}
