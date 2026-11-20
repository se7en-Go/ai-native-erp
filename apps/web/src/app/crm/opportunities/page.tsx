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

/** 阶段配色：颜色只作用于状态点，底板统一为纯净白 / 深灰卡片。 */
const STAGE_ACCENT: Record<
  OpportunityStage,
  { dot: string; label: { zh: string; en: string } }
> = {
  discovery: {
    dot: "bg-slate-400",
    label: { zh: "需求挖掘", en: "Discovery" },
  },
  sampling: {
    dot: "bg-violet-500",
    label: { zh: "寄样确认", en: "Sampling" },
  },
  quoting: {
    dot: "bg-blue-600 dark:bg-blue-500",
    label: { zh: "报价中", en: "Quoting" },
  },
  negotiating: {
    dot: "bg-amber-500",
    label: { zh: "商务谈判", en: "Negotiating" },
  },
  closed_won: {
    dot: "bg-emerald-500",
    label: { zh: "已成交", en: "Closed won" },
  },
  closed_lost: {
    dot: "bg-rose-500",
    label: { zh: "已丢单", en: "Closed lost" },
  },
};

/**
 * PageHeader / CardHeader 为共享原语（无 dark 变体），
 * 此处以任意变体在页面层补齐深色模式下的标题对比度。
 */
const PAGE_DARK_HEADER =
  "dark:[&_header_h1]:text-gray-100 dark:[&_header_p]:text-gray-400 dark:[&_.erp-card-header_h2]:text-gray-100 dark:[&_.erp-card-header_p]:text-gray-400";

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
      <Page className={PAGE_DARK_HEADER}>
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
            className="rounded-xl border border-rose-200 bg-rose-50 p-2.5 text-[12px] font-medium text-rose-800 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-300"
          >
            {opportunitiesQuery.error?.message}
          </p>
        )}

        <div
          data-testid="opportunity-board"
          className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6"
        >
          {STAGES.map((stage) => {
            const cards = byStage(stage);
            const accent = STAGE_ACCENT[stage];
            return (
              <section
                key={stage}
                data-testid={`opportunity-column-${stage}`}
                className="flex min-w-0 flex-col rounded-2xl border border-gray-200 bg-white p-2.5 dark:border-gray-800 dark:bg-gray-900"
              >
                <header className="flex items-center justify-between gap-2 px-1 pb-2">
                  <span className="flex min-w-0 items-center gap-1.5">
                    <span className={`h-2 w-2 shrink-0 rounded-full ${accent.dot}`} />
                    <span className="truncate text-[11.5px] font-semibold text-gray-900 dark:text-gray-100">
                      {bi(accent.label.zh, accent.label.en)}
                    </span>
                  </span>
                  <span className="shrink-0 rounded-full border border-gray-200 bg-gray-50 px-2 py-0.5 text-[10.5px] font-bold tabular-nums text-gray-600 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300">
                    {cards.length}
                  </span>
                </header>
                <div className="space-y-2">
                  {cards.map((opportunity) => (
                    <article
                      key={opportunity.id}
                      data-testid={`opportunity-card-${opportunity.id}`}
                      className="rounded-xl border border-gray-200 bg-gray-50/70 p-2.5 text-[12px] transition-colors duration-200 hover:border-blue-300 hover:bg-white dark:border-gray-800 dark:bg-gray-800/40 dark:hover:border-blue-500/50 dark:hover:bg-gray-800"
                    >
                      <div className="flex min-w-0 items-start gap-2">
                        <CrmAvatar
                          name={opportunity.company_name ?? opportunity.title}
                          size="sm"
                        />
                        <div className="min-w-0">
                          <p className="truncate font-semibold text-gray-900 dark:text-gray-100">
                            {opportunity.title}
                          </p>
                          <p className="truncate text-[10.5px] font-medium text-gray-500 dark:text-gray-400">
                            {opportunity.company_name ?? opportunity.customer_id.slice(0, 8)}
                          </p>
                        </div>
                      </div>
                      <p className="mt-1.5 text-[12.5px] font-bold tabular-nums text-blue-700 dark:text-blue-400">
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
                        className="mt-2 w-full rounded-lg border border-gray-200 bg-white px-2 py-1 text-[10.5px] font-medium text-gray-700 transition-colors hover:border-gray-300 focus:border-blue-500 focus:outline-none dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200 dark:hover:border-gray-600"
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
                    <p className="rounded-xl border border-dashed border-gray-200 bg-gray-50/60 px-2 py-5 text-center text-[11px] font-medium text-gray-400 dark:border-gray-800 dark:bg-gray-900/40 dark:text-gray-500">
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
