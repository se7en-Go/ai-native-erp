"use client";

import React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CircleDollarSign, FileSignature, Layers3, TrendingUp } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { QuotePiBuilder } from "@/components/b2b/quote-pi-builder";
import { BentoStatCard } from "@/components/ui/bento-stat-card";
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

/** 21st.dev Kanban 阶段配色：圆点 / 数量胶囊 / 卡片左色条保持一致。 */
const STAGE_ACCENT: Record<
  OpportunityStage,
  { dot: string; chip: string; bar: string; column: string; glow: string }
> = {
  discovery: {
    dot: "bg-slate-400",
    chip: "border-slate-200 bg-slate-100 text-slate-700",
    bar: "bg-slate-300",
    column: "bg-slate-50/70 border-slate-200/70",
    glow: "hover:border-slate-300",
  },
  sampling: {
    dot: "bg-violet-500",
    chip: "border-violet-200 bg-violet-50 text-violet-700",
    bar: "bg-violet-400",
    column: "bg-violet-50/40 border-violet-100",
    glow: "hover:border-violet-300",
  },
  quoting: {
    dot: "bg-blue-600",
    chip: "border-blue-200 bg-blue-50 text-blue-700",
    bar: "bg-blue-500",
    column: "bg-blue-50/40 border-blue-100",
    glow: "hover:border-blue-300",
  },
  negotiating: {
    dot: "bg-amber-500",
    chip: "border-amber-200 bg-amber-50 text-amber-800",
    bar: "bg-amber-400",
    column: "bg-amber-50/40 border-amber-100",
    glow: "hover:border-amber-300",
  },
  closed_won: {
    dot: "bg-emerald-500",
    chip: "border-emerald-200 bg-emerald-50 text-emerald-700",
    bar: "bg-emerald-500",
    column: "bg-emerald-50/40 border-emerald-100",
    glow: "hover:border-emerald-300",
  },
  closed_lost: {
    dot: "bg-rose-500",
    chip: "border-rose-200 bg-rose-50 text-rose-700",
    bar: "bg-rose-400",
    column: "bg-rose-50/40 border-rose-100",
    glow: "hover:border-rose-300",
  },
};

/** 单列金额汇总：同一列可能混币种，按首位币种展示并标注混合。 */
function stageTotal(
  cards: Array<{ expected_amount: string; currency: string }>,
  emptyCurrency: string
): { amount: string; currency: string; mixed: boolean } {
  if (cards.length === 0) return { amount: "0", currency: emptyCurrency, mixed: false };
  const currency = cards[0].currency;
  const mixed = cards.some((card) => card.currency !== currency);
  const sum = cards
    .filter((card) => card.currency === currency)
    .reduce((total, card) => total + Number(card.expected_amount || 0), 0);
  return { amount: sum.toFixed(2), currency, mixed };
}

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
  const quotes = quotesQuery.data?.items ?? [];
  const wonDeals = opportunities.filter((o) => o.stage === "closed_won");
  const activeDeals = opportunities.filter(
    (o) => o.stage !== "closed_won" && o.stage !== "closed_lost"
  );
  const openValue = activeDeals.reduce((total, o) => total + Number(o.expected_amount || 0), 0);
  const wonValue = wonDeals.reduce((total, o) => total + Number(o.expected_amount || 0), 0);
  const winRate =
    opportunities.length === 0
      ? 0
      : Math.round((wonDeals.length / opportunities.length) * 100);

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

        <section
          aria-label={bi("商机管道关键指标", "Pipeline key metrics")}
          className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"
        >
          <BentoStatCard
            label={bi("进行中商机", "Open deals")}
            value={activeDeals.length}
            icon={Layers3}
            accent="blue"
            hint={bi("覆盖挖掘到谈判的全部阶段", "Spans discovery through negotiating")}
          />
          <BentoStatCard
            variant="hero"
            label={bi("在谈金额", "Open pipeline value")}
            value={formatMoney(openValue.toFixed(2), "USD")}
            icon={CircleDollarSign}
            accent="violet"
            hint={bi("未含已成交与已丢单", "Excludes won and lost deals")}
          />
          <BentoStatCard
            label={bi("已成交金额", "Won value")}
            value={formatMoney(wonValue.toFixed(2), "USD")}
            icon={TrendingUp}
            accent="emerald"
            badge={`${wonDeals.length}`}
            badgeTone="emerald"
            hint={bi("已转化为批发订单或待出运", "Converted to wholesale orders or awaiting dispatch")}
          />
          <BentoStatCard
            label={bi("商机赢单率", "Win rate")}
            value={`${winRate}%`}
            icon={FileSignature}
            accent="amber"
            badge={`${quotes.length} ${bi("张 PI", "PIs")}`}
            badgeTone="amber"
            hint={bi("按全部商机口径计算", "Computed across all recorded deals")}
          />
        </section>

        <div className="grid gap-3 overflow-x-auto pb-1 md:grid-cols-3 2xl:grid-cols-5">
          {KANBAN_STAGES.map((stage) => {
            const stageCards = opportunities.filter((o) => o.stage === stage);
            const accent = STAGE_ACCENT[stage];
            const total = stageTotal(stageCards, "USD");
            return (
              <section
                key={stage}
                data-testid={`kanban-col-${stage}`}
                className={`flex min-w-[220px] flex-col rounded-2xl border p-2.5 ${accent.column}`}
              >
                <header className="flex items-center justify-between gap-2 px-1">
                  <span className="flex min-w-0 items-center gap-1.5">
                    <span className={`h-2 w-2 shrink-0 rounded-full ${accent.dot}`} />
                    <span className="truncate text-[12.5px] font-semibold text-slate-800">
                      {STAGE_LABEL[stage]}
                    </span>
                  </span>
                  <span
                    className={`shrink-0 rounded-full border px-2 py-0.5 text-[11px] font-semibold tabular-nums ${accent.chip}`}
                  >
                    {stageCards.length}
                  </span>
                </header>
                <div className="mt-1.5 flex items-baseline justify-between gap-2 px-1">
                  <span className="text-[11px] font-medium text-slate-600">
                    {bi("阶段金额", "Stage value")}
                  </span>
                  <span className="text-[12px] font-bold tabular-nums text-slate-900">
                    {formatMoney(total.amount, total.currency)}
                    {total.mixed && <span className="ml-0.5 text-[10px] text-slate-500">*</span>}
                  </span>
                </div>

                <div className="mt-2.5 space-y-2">
                  {stageCards.map((card) => (
                    <article
                      key={card.id}
                      className={`group rounded-xl border border-slate-200/90 border-l-[3px] bg-white p-2.5 shadow-[0_1px_2px_rgba(15,23,42,0.04)] transition-all duration-200 hover:-translate-y-0.5 hover:shadow-[0_6px_16px_-6px_rgba(15,23,42,0.22)] ${accent.glow}`}
                    >
                      <div className="flex items-start justify-between gap-2">
                        <div className="min-w-0">
                          <p className="truncate text-[12.5px] font-semibold text-slate-900">
                            {card.title}
                          </p>
                          {card.company_name && (
                            <p className="mt-0.5 truncate text-[11px] font-medium text-slate-500">
                              {card.company_name}
                            </p>
                          )}
                        </div>
                        <span
                          aria-hidden="true"
                          className={`mt-0.5 h-6 w-[3px] shrink-0 rounded-full ${accent.bar}`}
                        />
                      </div>
                      <p className="mt-1.5 text-[13px] font-bold tabular-nums text-blue-700">
                        {formatMoney(card.expected_amount, card.currency)}
                      </p>
                      <select
                        aria-label={`${bi("推进商机阶段", "Move stage")} · ${card.title}`}
                        value={card.stage}
                        onChange={(e) =>
                          moveStageMutation.mutate({
                            id: card.id,
                            stage: e.target.value as OpportunityStage,
                          })
                        }
                        className="mt-2 w-full rounded-lg border border-slate-200 bg-slate-50/80 px-2 py-1 text-[11px] font-medium text-slate-700 transition-colors hover:border-slate-300 hover:bg-white focus:border-blue-500 focus:bg-white focus:outline-none"
                      >
                        {KANBAN_STAGES.map((opt) => (
                          <option key={opt} value={opt}>
                            {STAGE_LABEL[opt]}
                          </option>
                        ))}
                      </select>
                    </article>
                  ))}
                  {stageCards.length === 0 && (
                    <p className="rounded-xl border border-dashed border-slate-300/80 bg-white/60 px-2 py-4 text-center text-[11px] font-medium text-slate-500">
                      {bi("暂无商机", "No deals")}
                    </p>
                  )}
                </div>
              </section>
            );
          })}
        </div>

        <QuotePiBuilder quotes={quotes} variants={variantsQuery.data?.items ?? []} />
      </Page>
    </AppShell>
  );
}
