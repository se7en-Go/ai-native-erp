"use client";

import React, { Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CircleDollarSign,
  FileSignature,
  Layers3,
  TrendingUp,
  UserRoundSearch,
} from "lucide-react";
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

/**
 * 21st.dev 纯净冷灰白 + 科技蓝：各阶段底板统一为白/深灰卡片，
 * 阶段区分只保留顶部小圆点与标题胶囊色，避免彩色底板拼贴出的视觉空洞。
 */
const STAGE_ACCENT: Record<OpportunityStage, { dot: string; chip: string }> = {
  discovery: {
    dot: "bg-slate-400",
    chip: "border-gray-200 bg-gray-100 text-gray-700 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300",
  },
  sampling: {
    dot: "bg-violet-500",
    chip: "border-violet-200 bg-violet-50 text-violet-700 dark:border-violet-500/40 dark:bg-violet-500/15 dark:text-violet-300",
  },
  quoting: {
    dot: "bg-blue-600 dark:bg-blue-500",
    chip: "border-blue-200 bg-blue-50 text-blue-700 dark:border-blue-500/40 dark:bg-blue-500/15 dark:text-blue-300",
  },
  negotiating: {
    dot: "bg-amber-500",
    chip: "border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-500/40 dark:bg-amber-500/15 dark:text-amber-300",
  },
  closed_won: {
    dot: "bg-emerald-500",
    chip: "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-500/40 dark:bg-emerald-500/15 dark:text-emerald-300",
  },
  closed_lost: {
    dot: "bg-rose-500",
    chip: "border-rose-200 bg-rose-50 text-rose-700 dark:border-rose-500/40 dark:bg-rose-500/15 dark:text-rose-300",
  },
};

/**
 * PageHeader / CardHeader 为共享原语（无 dark 变体），
 * 此处以任意变体在页面层补齐深色模式下的标题对比度。
 */
const PAGE_DARK_HEADER =
  "dark:[&_header_h1]:text-gray-100 dark:[&_header_p]:text-gray-400 dark:[&_.erp-card-header_h2]:text-gray-100 dark:[&_.erp-card-header_p]:text-gray-400";

/** 阶段底板：纯净白 / 深灰，圆角与边框节奏全列一致。 */
const STAGE_COLUMN_CLASS =
  "flex flex-col rounded-2xl border border-gray-200 bg-white p-2.5 dark:border-gray-800 dark:bg-gray-900";

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
  // `useSearchParams` needs a Suspense boundary so `next build` can still
  // prerender this route statically (mirrors /settings/channels).
  return (
    <Suspense fallback={<B2bQuotesFallback />}>
      <B2bQuotesPageInner />
    </Suspense>
  );
}

function B2bQuotesFallback() {
  return (
    <AppShell currentPath="/b2b/quotes">
      <Page>
        <div className="erp-card animate-pulse p-8 text-[13px] font-medium text-gray-500 dark:text-gray-400">
          正在加载 B2B 报价工作台…
        </div>
      </Page>
    </AppShell>
  );
}

function B2bQuotesPageInner() {
  const queryClient = useQueryClient();
  const { bi } = useI18n();
  const searchParams = useSearchParams();
  // Inbox 上下文抽屉的深链：/b2b/quotes?customer_id=...&customer_name=...
  const contextCustomerId = searchParams.get("customer_id");
  const contextCustomerName = searchParams.get("customer_name");
  const deepLinkQuoteId = searchParams.get("quoteId");
  const autoOpenSendEmail = searchParams.get("send_pi_email") === "1";

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
      <Page className={PAGE_DARK_HEADER}>
        <PageHeader
          title={bi("B2B 询盘报价与形式发票（PI）", "B2B pipeline & proforma invoice (PI) builder")}
          description={bi(
            "从询盘到成交的商机看板：锁定汇率、按阶梯价自动算 MOQ 单价、生成多语言 PI PDF，并一键转为批发订单。",
            "Track export inquiries from discovery to closed won: lock FX, auto-price MOQ tiers, generate multilingual PI PDFs and convert to wholesale orders in one click."
          )}
        />

        {contextCustomerName && (
          <div
            data-testid="quote-customer-context-banner"
            className="flex flex-wrap items-center gap-2.5 rounded-2xl border border-emerald-200 bg-emerald-50/70 px-3.5 py-2.5 dark:border-emerald-500/30 dark:bg-emerald-500/10"
          >
            <span
              aria-hidden="true"
              className="grid h-8 w-8 shrink-0 place-items-center rounded-xl bg-white text-emerald-700 ring-1 ring-emerald-200 dark:bg-gray-900 dark:text-emerald-400 dark:ring-emerald-500/30"
            >
              <UserRoundSearch className="h-4 w-4" />
            </span>
            <div className="min-w-0">
              <p className="text-[13px] font-semibold leading-5 text-emerald-950 dark:text-emerald-200">
                {bi(
                  `正在为客户「${contextCustomerName}」创建 B2B 报价单`,
                  `Creating a B2B quote for ${contextCustomerName}`
                )}
              </p>
              <p className="text-[11.5px] leading-5 text-emerald-800 dark:text-emerald-300/90">
                {bi(
                  "下方试算阶梯价后生成 PI，可直接发邮件给买家；客户档案、历史订单与身份图谱都在 Inbox 右侧面板里。",
                  "Price the MOQ tiers below, issue the PI and email it to the buyer; the customer profile, order history and identity graph stay in the Inbox drawer."
                )}
              </p>
            </div>
            {contextCustomerId && (
              <span className="ml-auto shrink-0 rounded-full border border-emerald-200 bg-white px-2 py-0.5 font-mono text-[10.5px] font-medium text-emerald-800 dark:border-emerald-500/30 dark:bg-gray-900 dark:text-emerald-300">
                {contextCustomerId}
              </span>
            )}
          </div>
        )}

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

        {/* 单行 5 列整齐铺满：1440px 下不再折成 3+2 留出空洞。 */}
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-5">
          {KANBAN_STAGES.map((stage) => {
            const stageCards = opportunities.filter((o) => o.stage === stage);
            const accent = STAGE_ACCENT[stage];
            const total = stageTotal(stageCards, "USD");
            return (
              <section
                key={stage}
                data-testid={`kanban-col-${stage}`}
                className={STAGE_COLUMN_CLASS}
              >
                <header className="flex items-center justify-between gap-2 px-1">
                  <span className="flex min-w-0 items-center gap-1.5">
                    <span className={`h-2 w-2 shrink-0 rounded-full ${accent.dot}`} />
                    <span className="truncate text-[12.5px] font-semibold text-gray-900 dark:text-gray-100">
                      {STAGE_LABEL[stage]}
                    </span>
                  </span>
                  <span
                    className={`shrink-0 rounded-full border px-2 py-0.5 text-[11px] font-semibold tabular-nums ${accent.chip}`}
                  >
                    {stageCards.length}
                  </span>
                </header>
                <div className="mt-1.5 flex items-baseline justify-between gap-2 border-b border-gray-100 px-1 pb-1.5 dark:border-gray-800">
                  <span className="text-[11px] font-medium text-gray-500 dark:text-gray-400">
                    {bi("阶段金额", "Stage value")}
                  </span>
                  <span className="text-[12px] font-bold tabular-nums text-gray-900 dark:text-gray-100">
                    {formatMoney(total.amount, total.currency)}
                    {total.mixed && (
                      <span className="ml-0.5 text-[10px] text-gray-400 dark:text-gray-500">*</span>
                    )}
                  </span>
                </div>

                <div className="mt-2 space-y-2">
                  {stageCards.map((card) => (
                    <article
                      key={card.id}
                      className="group rounded-xl border border-gray-200 bg-gray-50/70 p-2.5 transition-colors duration-200 hover:border-blue-300 hover:bg-white dark:border-gray-800 dark:bg-gray-800/40 dark:hover:border-blue-500/50 dark:hover:bg-gray-800"
                    >
                      <p className="truncate text-[12.5px] font-semibold text-gray-900 dark:text-gray-100">
                        {card.title}
                      </p>
                      {card.company_name && (
                        <p className="mt-0.5 truncate text-[11px] font-medium text-gray-500 dark:text-gray-400">
                          {card.company_name}
                        </p>
                      )}
                      <p className="mt-1.5 text-[13px] font-bold tabular-nums text-blue-700 dark:text-blue-400">
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
                        className="mt-2 w-full rounded-lg border border-gray-200 bg-white px-2 py-1 text-[11px] font-medium text-gray-700 transition-colors hover:border-gray-300 focus:border-blue-500 focus:outline-none dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200 dark:hover:border-gray-600"
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
                    <p className="rounded-xl border border-dashed border-gray-200 bg-gray-50/60 px-2 py-6 text-center text-[11px] font-medium text-gray-400 dark:border-gray-800 dark:bg-gray-900/40 dark:text-gray-500">
                      {bi("暂无商机", "No deals")}
                    </p>
                  )}
                </div>
              </section>
            );
          })}
        </div>

        <QuotePiBuilder
          quotes={quotes}
          variants={variantsQuery.data?.items ?? []}
          initialQuoteId={deepLinkQuoteId}
          autoOpenSendEmail={autoOpenSendEmail}
        />
      </Page>
    </AppShell>
  );
}