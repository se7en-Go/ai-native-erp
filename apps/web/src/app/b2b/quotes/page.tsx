"use client";

import React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AppShell } from "@/components/app-shell";
import { QuotePiBuilder } from "@/components/b2b/quote-pi-builder";
import { Page, PageHeader } from "@/components/ui/page";
import { updateOpportunityStage } from "@/lib/api-client/live-client";
import {
  fetchB2bOpportunities,
  fetchB2bQuotes,
  fetchCatalogVariants,
  fetchPendingProposalsCount,
} from "@/lib/data-source";
import { useI18n } from "@/lib/i18n";
import { formatMoney, type OpportunityStage } from "@/lib/contracts";

const KANBAN_STAGES: OpportunityStage[] = [
  "discovery",
  "sampling",
  "quoting",
  "negotiating",
  "closed_won",
];

export default function B2bQuotesPage() {
  const queryClient = useQueryClient();
  const { bi } = useI18n();

  const STAGE_LABEL: Record<OpportunityStage, string> = {
    discovery: bi("需求挖掘", "Discovery"),
    sampling: bi("寄样确认", "Sampling"),
    quoting: bi("报价中", "Quoting"),
    negotiating: bi("商务谈判", "Negotiating"),
    closed_won: bi("已成交", "Closed won"),
    closed_lost: bi("已丢单", "Closed lost"),
  };

  const pendingProposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () => fetchPendingProposalsCount(),
  });

  const oppQuery = useQuery({
    queryKey: ["b2b", "opportunities"],
    queryFn: () => fetchB2bOpportunities(),
  });

  const quotesQuery = useQuery({
    queryKey: ["b2b", "quotes"],
    queryFn: () => fetchB2bQuotes(),
  });

  const variantsQuery = useQuery({
    queryKey: ["catalog", "variants"],
    queryFn: () => fetchCatalogVariants(""),
  });

  const moveStageMutation = useMutation({
    mutationFn: ({ id, stage }: { id: string; stage: OpportunityStage }) =>
      updateOpportunityStage(id, stage),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["b2b", "opportunities"] });
    },
  });

  const opportunities = oppQuery.data?.items ?? [];

  return (
    <AppShell
      currentPath="/b2b/quotes"
      pendingProposalsCount={pendingProposalsQuery.data?.total ?? 0}
    >
      <Page>
        <PageHeader
          title={bi("B2B 询盘报价与形式发票（PI）", "B2B pipeline & proforma invoice (PI) builder")}
          description={bi(
            "从询盘到成交的商机看板：锁定汇率、按阶梯价自动算 MOQ 单价、生成多语言 PI PDF，并一键转为批发订单。",
            "Track export inquiries from discovery to closed won: lock FX, auto-price MOQ tiers, generate multilingual PI PDFs and convert to wholesale orders in one click."
          )}
        />

        <div className="grid gap-3 md:grid-cols-3 xl:grid-cols-5">
          {KANBAN_STAGES.map((stage) => {
            const stageCards = opportunities.filter((o) => o.stage === stage);
            return (
              <div
                key={stage}
                data-testid={`kanban-col-${stage}`}
                className="rounded-xl border border-slate-200/80 bg-slate-100/60 p-2.5"
              >
                <div className="mb-2 flex items-center justify-between px-1 text-[12px] font-medium text-slate-600">
                  <span>{STAGE_LABEL[stage]}</span>
                  <span className="rounded-full bg-white px-1.5 py-0.5 text-[10px] tabular-nums text-slate-500">
                    {stageCards.length}
                  </span>
                </div>
                <div className="space-y-2">
                  {stageCards.map((card) => (
                    <div
                      key={card.id}
                      className="rounded-lg border border-slate-200/80 bg-white p-2.5 text-[12px] shadow-[0_1px_2px_rgba(15,23,42,0.04)]"
                    >
                      <div className="font-medium text-slate-900">{card.title}</div>
                      {card.company_name && (
                        <div className="mt-0.5 text-[11px] text-slate-500">{card.company_name}</div>
                      )}
                      <div className="mt-1 font-semibold tabular-nums text-blue-700">
                        {formatMoney(card.expected_amount, card.currency)}
                      </div>
                      <select
                        aria-label={`${bi("推进商机阶段", "Move stage")} · ${card.title}`}
                        value={card.stage}
                        onChange={(e) =>
                          moveStageMutation.mutate({
                            id: card.id,
                            stage: e.target.value as OpportunityStage,
                          })
                        }
                        className="mt-2 w-full rounded border border-slate-200 bg-white px-1.5 py-1 text-[11px] text-slate-600"
                      >
                        {KANBAN_STAGES.map((opt) => (
                          <option key={opt} value={opt}>
                            {STAGE_LABEL[opt]}
                          </option>
                        ))}
                      </select>
                    </div>
                  ))}
                  {stageCards.length === 0 && (
                    <p className="px-1 py-3 text-[11px] text-slate-400">
                      {bi("暂无商机", "No deals")}
                    </p>
                  )}
                </div>
              </div>
            );
          })}
        </div>

        <QuotePiBuilder
          quotes={quotesQuery.data?.items ?? []}
          variants={variantsQuery.data?.items ?? []}
        />
      </Page>
    </AppShell>
  );
}
