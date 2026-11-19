"use client";

import React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AppShell } from "@/components/app-shell";
import { QuotePiBuilder } from "@/components/b2b/quote-pi-builder";
import { bffFetch } from "@/lib/api";
import {
  fetchB2bOpportunities,
  fetchB2bQuotes,
  fetchCatalogVariants,
  fetchPendingProposalsCount,
} from "@/lib/data-source";
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
      bffFetch(`/v1/b2b/opportunities/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ stage }),
      }),
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
      <div className="mx-auto max-w-7xl space-y-5">
        <div>
          <h1 className="text-lg font-bold text-slate-900">
            B2B Pipeline Kanban & Versioned Proforma Invoice (PI) Builder
          </h1>
          <p className="text-xs text-slate-500">
            Track export inquiries from discovery through closed_won, lock FX rates, generate multilingual PDFs, and convert 1-Click to wholesale orders.
          </p>
        </div>

        <div className="grid gap-3 md:grid-cols-5">
          {KANBAN_STAGES.map((stage) => {
            const stageCards = opportunities.filter((o) => o.stage === stage);
            return (
              <div
                key={stage}
                data-testid={`kanban-col-${stage}`}
                className="rounded-lg border border-slate-200 bg-slate-100/70 p-2.5"
              >
                <div className="mb-2 flex items-center justify-between text-xs font-bold uppercase text-slate-700">
                  <span>{stage}</span>
                  <span className="rounded-full bg-white px-1.5 py-0.5 text-[10px]">
                    {stageCards.length}
                  </span>
                </div>
                <div className="space-y-2">
                  {stageCards.map((card) => (
                    <div
                      key={card.id}
                      className="rounded-md border border-slate-200 bg-white p-2.5 text-xs shadow-xs"
                    >
                      <div className="font-semibold text-slate-900">{card.title}</div>
                      {card.company_name && (
                        <div className="mt-0.5 text-[11px] text-slate-500">{card.company_name}</div>
                      )}
                      <div className="mt-1 font-bold text-indigo-700">
                        {formatMoney(card.expected_amount, card.currency)}
                      </div>
                      <select
                        aria-label={`Stage for ${card.title}`}
                        value={card.stage}
                        onChange={(e) =>
                          moveStageMutation.mutate({
                            id: card.id,
                            stage: e.target.value as OpportunityStage,
                          })
                        }
                        className="mt-2 w-full rounded border border-slate-200 px-1.5 py-1 text-[11px]"
                      >
                        {KANBAN_STAGES.map((opt) => (
                          <option key={opt} value={opt}>
                            {opt}
                          </option>
                        ))}
                      </select>
                    </div>
                  ))}
                </div>
              </div>
            );
          })}
        </div>

        <QuotePiBuilder
          quotes={quotesQuery.data?.items ?? []}
          variants={variantsQuery.data?.items ?? []}
        />
      </div>
    </AppShell>
  );
}
