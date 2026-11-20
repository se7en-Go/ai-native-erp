"use client";

import React, { useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Layers3, TrendingUp, Trophy } from "lucide-react";

import { AppShell } from "@/components/app-shell";
import { BentoStatCard } from "@/components/ui/bento-stat-card";
import { Page, PageHeader } from "@/components/ui/page";
import type { OpportunityRead, OpportunityStage } from "@/lib/api-client/types.gen";
import { formatMoney } from "@/lib/contracts";
import { fetchPendingProposalsCount } from "@/lib/data-source";
import { fetchPhase3aOpportunities, patchPhase3aOpportunityStage } from "@/lib/phase3a-data";
import { useI18n } from "@/lib/i18n";
import { CrmAvatar } from "../_components/crm-ui";

const STAGES: OpportunityStage[] = [
  "discovery",
  "sampling",
  "quoting",
  "negotiating",
  "closed_won",
  "closed_lost",
];

const STAGE_ACCENT: Record<
  OpportunityStage,
  { dot: string; bar: string; column: string; label: { zh: string; en: string } }
> = {
  discovery: {
    dot: "bg-slate-400",
    bar: "bg-slate-300",
    column: "bg-slate-50/70",
    label: { zh: "需求挖掘", en: "Discovery" },
  },
  sampling: {
    dot: "bg-violet-500",
    bar: "bg-violet-400",
    column: "bg-violet-50/40",
    label: { zh: "寄样确认", en: "Sampling" },
  },
  quoting: {
    dot: "bg-blue-600",
    bar: "bg-blue-500",
    column: "bg-blue-50/40",
    label: { zh: "报价中", en: "Quoting" },
  },
  negotiating: {
    dot: "bg-amber-500",
    bar: "bg-amber-400",
    column: "bg-amber-50/40",
    label: { zh: "商务谈判", en: "Negotiating" },
  },
  closed_won: {
    dot: "bg-emerald-500",
    bar: "bg-emerald-500",
    column: "bg-emerald-50/40",
    label: { zh: "已成交", en: "Closed won" },
  },
  closed_lost: {
    dot: "bg-rose-500",
    bar: "bg-rose-400",
    column: "bg-rose-50/40",
    label: { zh: "已丢单", en: "Closed lost" },
  },
};

export default function CrmOpportunitiesPage() {
  const { bi } = useI18n();
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

  const summary = useMemo(() => {
    const open = opportunities.filter(
      (o) => o.stage !== "closed_won" && o.stage !== "closed_lost"
    );
    const won = opportunities.filter((o) => o.stage === "closed_won");
    return {
      open: open.length,
      openValue: open.reduce((total, o) => total + Number(o.expected_amount || 0), 0),
      won: won.length,
    };
  }, [opportunities]);

  return (
    <AppShell
      currentPath="/crm/opportunities"
      pendingProposalsCount={proposalsQuery.data?.total ?? 0}
    >
      <Page>
        <PageHeader
          title={bi("商机阶段看板", "Opportunity board")}
          description={bi(
            "使用卡片上的阶段选择器推进商机；所有写操作经租户作用域 API 校验。",
            "Advance deals with the stage selector on each card; every write is validated by the tenant-scoped API."
          )}
        />

        <section
          aria-label={bi("商机关键指标", "Opportunity key metrics")}
          className="grid gap-3 sm:grid-cols-3"
        >
          <BentoStatCard
            label={bi("进行中商机", "Open deals")}
            value={summary.open}
            icon={Layers3}
            accent="blue"
            hint={bi("不含已成交与已丢单", "Excludes won and lost")}
          />
          <BentoStatCard
            label={bi("在谈金额", "Open pipeline value")}
            value={formatMoney(summary.openValue.toFixed(2), "USD")}
            icon={TrendingUp}
            accent="violet"
            hint={bi("按商机预期金额合计", "Summed expected amounts")}
          />
          <BentoStatCard
            label={bi("已成交", "Closed won")}
            value={summary.won}
            icon={Trophy}
            accent="emerald"
            hint={bi("可一键转为批发订单", "Convertible to wholesale orders")}
          />
        </section>

        {(opportunitiesQuery.isError ?? false) && (
          <p
            role="alert"
            className="rounded-xl border border-rose-200 bg-rose-50 p-2.5 text-[12px] font-medium text-rose-800"
          >
            {opportunitiesQuery.error?.message}
          </p>
        )}

        <div
          data-testid="opportunity-board"
          className="grid gap-3 overflow-x-auto pb-1 md:grid-cols-3 2xl:grid-cols-6"
        >
          {STAGES.map((stage) => {
            const cards = byStage(stage);
            const accent = STAGE_ACCENT[stage];
            return (
              <section
                key={stage}
                data-testid={`opportunity-column-${stage}`}
                className={`min-w-[200px] rounded-2xl border border-slate-200/80 p-2.5 ${accent.column}`}
              >
                <header className="flex items-center justify-between gap-2 px-1 pb-2">
                  <span className="flex min-w-0 items-center gap-1.5">
                    <span className={`h-2 w-2 shrink-0 rounded-full ${accent.dot}`} />
                    <span className="truncate text-[11.5px] font-semibold text-slate-800">
                      {bi(accent.label.zh, accent.label.en)}
                    </span>
                  </span>
                  <span className="shrink-0 rounded-full border border-slate-200 bg-white px-2 py-0.5 text-[10.5px] font-bold tabular-nums text-slate-700">
                    {cards.length}
                  </span>
                </header>
                <div className="space-y-2">
                  {cards.map((opportunity) => (
                    <article
                      key={opportunity.id}
                      data-testid={`opportunity-card-${opportunity.id}`}
                      className="rounded-xl border border-slate-200/90 border-l-[3px] bg-white p-2.5 text-[12px] shadow-[0_1px_2px_rgba(15,23,42,0.04)] transition-all duration-200 hover:-translate-y-0.5 hover:border-slate-300 hover:shadow-[0_6px_16px_-6px_rgba(15,23,42,0.22)]"
                    >
                      <div className="flex items-start justify-between gap-2">
                        <div className="flex min-w-0 items-start gap-2">
                          <CrmAvatar
                            name={opportunity.company_name ?? opportunity.title}
                            size="sm"
                          />
                          <div className="min-w-0">
                            <p className="truncate font-semibold text-slate-900">
                              {opportunity.title}
                            </p>
                            <p className="truncate text-[10.5px] font-medium text-slate-500">
                              {opportunity.company_name ?? opportunity.customer_id.slice(0, 8)}
                            </p>
                          </div>
                        </div>
                        <span
                          aria-hidden="true"
                          className={`mt-0.5 h-6 w-[3px] shrink-0 rounded-full ${accent.bar}`}
                        />
                      </div>
                      <p className="mt-1.5 text-[12.5px] font-bold tabular-nums text-slate-800">
                        {formatMoney(opportunity.expected_amount, opportunity.currency)}
                      </p>
                      <select
                        aria-label={`${bi("推进商机", "Move deal")} ${opportunity.title}`}
                        value={opportunity.stage}
                        onChange={(event) =>
                          moveStage.mutate({
                            id: opportunity.id,
                            stage: event.target.value as OpportunityStage,
                          })
                        }
                        className="mt-2 w-full rounded-lg border border-slate-200 bg-slate-50/80 px-2 py-1 text-[10.5px] font-medium text-slate-700 transition-colors hover:border-slate-300 hover:bg-white focus:border-blue-500 focus:bg-white focus:outline-none"
                      >
                        {STAGES.map((option) => (
                          <option key={option} value={option}>
                            {option}
                          </option>
                        ))}
                      </select>
                    </article>
                  ))}
                  {cards.length === 0 && (
                    <p className="rounded-xl border border-dashed border-slate-300/80 bg-white/60 px-2 py-4 text-center text-[11px] font-medium text-slate-500">
                      {bi("暂无商机", "No deals")}
                    </p>
                  )}
                </div>
              </section>
            );
          })}
        </div>
      </Page>
    </AppShell>
  );
}
