"use client";

import React, { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Bot, Send, ShieldCheck, X, AlertTriangle, ArrowUpRight, CalendarClock, PackageX, ClipboardCheck, Newspaper } from "lucide-react";
import { z } from "zod";
import {
  assistantChat,
  fetchCapabilities,
  fetchPendingProposalsCount,
  fetchWallet,
  type AssistantChatResult,
  type BillingWalletResponse,
} from "@/lib/api-client/live-client";
import {
  AiCapabilitySchema,
  AutonomyLevelSchema,
  formatMoney,
  type AiCapability,
} from "@/lib/contracts";
import { SegmentedTabs } from "@/components/ui/segmented-tabs";
import { useI18n } from "@/lib/i18n";
import { useAuthSession } from "@/components/providers";
import { fetchInboxAssist } from "@/lib/phase3b-data";

const SAFE_METRIC_LABELS: Record<string, { zh: string; en: string }> = {
  open_threads: { zh: "进行中会话", en: "Open threads" },
  pending_quotes: { zh: "待确认报价", en: "Pending quotes" },
  stagnant_shipments_72h: { zh: "72h 停滞运单", en: "72h stagnant shipments" },
  drafts_ready: { zh: "待审内容草稿", en: "Content drafts ready" },
  net_margin_pct: { zh: "净贡献毛利率", en: "Net contribution margin" },
  monthly_profit_target: { zh: "月度利润目标", en: "Monthly profit target" },
  open_receivables_amount: { zh: "未结应收", en: "Open receivables" },
};

const CAPABILITY_TITLES: Record<AiCapability, { zh: string; en: string }> = {
  cs_support: { zh: "客服支持", en: "CS support" },
  sales_quoting: { zh: "销售报价", en: "Sales & quoting" },
  order_logistics: { zh: "订单物流", en: "Orders & logistics" },
  content_studio: { zh: "内容工坊", en: "Content studio" },
  biz_analytics: { zh: "经营分析", en: "Business analytics" },
  finance_recon: { zh: "财务对账", en: "Finance recon" },
};

export const CopilotMetricSchema = z.object({
  label_key: z.string(),
  kind: z.enum(["count", "money", "percent", "status"]),
  value: z.union([z.number().finite(), z.string()]),
  currency: z.string().length(3).optional(),
});

export const CopilotSummaryCardSchema = z.object({
  capability: AiCapabilitySchema,
  autonomy_level: AutonomyLevelSchema,
  metrics: z.array(CopilotMetricSchema),
});

export type CopilotSummaryCard = z.infer<typeof CopilotSummaryCardSchema>;

export function sanitizeCopilotCards(rawCards: unknown[]): CopilotSummaryCard[] {
  if (!Array.isArray(rawCards)) return [];
  const clean: CopilotSummaryCard[] = [];

  for (const rawCard of rawCards) {
    if (!rawCard || typeof rawCard !== "object") continue;
    const record = rawCard as Record<string, unknown>;
    const capParsed = AiCapabilitySchema.safeParse(record.capability);
    const levelParsed = AutonomyLevelSchema.safeParse(
      record.configured_autonomy ?? record.autonomy_level
    );
    if (!capParsed.success || !levelParsed.success) continue;

    const rawMetrics = Array.isArray(record.metrics)
      ? record.metrics
      : [
          {
            label_key: "open_threads",
            kind: "count",
            value:
              typeof record.executed_actions_7d === "number" ? record.executed_actions_7d : 0,
          },
        ];
    const safeMetrics: z.infer<typeof CopilotMetricSchema>[] = [];

    for (const m of rawMetrics) {
      if (!m || typeof m !== "object") continue;
      const metricObj = m as Record<string, unknown>;
      const key = typeof metricObj.label_key === "string" ? metricObj.label_key : "";
      // Never allow unmapped raw dotted/internal keys such as tax.filing.overdue
      if (!(key in SAFE_METRIC_LABELS)) continue;

      const rawVal = metricObj.value;
      const safeScalar: number | string =
        typeof rawVal === "number" && Number.isFinite(rawVal)
          ? rawVal
          : typeof rawVal === "string"
          ? rawVal
          : 0;

      const parsedMetric = CopilotMetricSchema.safeParse({
        label_key: key,
        kind: metricObj.kind,
        value: safeScalar,
        currency: typeof metricObj.currency === "string" ? metricObj.currency : undefined,
      });
      if (parsedMetric.success) {
        safeMetrics.push(parsedMetric.data);
      }
    }

    clean.push({
      capability: capParsed.data,
      autonomy_level: levelParsed.data,
      metrics: safeMetrics,
    });
  }

  return clean;
}

/** Chips are navigation shortcuts plus one real daily brief; never raw payloads. */
const COPILOT_CHIPS = [
  { id: "daily-brief", label: "AI 日报", labelEn: "Daily brief", icon: Newspaper },
  {
    id: "pending-proposals",
    label: "待审提案",
    labelEn: "Pending proposals",
    icon: ClipboardCheck,
    href: "/ai/proposals",
  },
  {
    id: "today-followups",
    label: "今日跟进",
    labelEn: "Today's follow-ups",
    icon: CalendarClock,
    href: "/crm/followups",
  },
  { id: "stock-alerts", label: "库存预警", labelEn: "Stock alerts", icon: PackageX, href: "/wms" },
] as const;

/** Only plain strings are rendered, so a nested payload can never print [object Object]. */
export function copilotBriefText(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function renderSafeMetricValue(metric: z.infer<typeof CopilotMetricSchema>): string {
  if (metric.kind === "money") {
    return formatMoney(metric.value, metric.currency ?? "USD");
  }
  if (metric.kind === "percent") {
    const num = typeof metric.value === "number" ? metric.value : Number(metric.value);
    return Number.isFinite(num) ? `${num.toFixed(1)}%` : "0.0%";
  }
  return typeof metric.value === "object" ? "—" : String(metric.value);
}

export type { AssistantChatResult, BillingWalletResponse };

export function FloatingCopilot({ conversationId }: { conversationId?: string | null }) {
  const { t, bi } = useI18n();
  const { principal } = useAuthSession();
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<"global" | "strict_rag">("global");
  const [query, setQuery] = useState("");
  const [lastResult, setLastResult] = useState<AssistantChatResult | null>(null);
  const [lastBrief, setLastBrief] = useState("");
  const [briefError, setBriefError] = useState<string | null>(null);

  const capabilitiesQuery = useQuery({
    queryKey: ["ai", "capabilities"],
    queryFn: () => fetchCapabilities(),
  });

  // GET /billing/wallet is owner/admin/finance only; agents reach the copilot too.
  const canReadWallet =
    principal?.role === "owner" || principal?.role === "admin" || principal?.role === "finance";
  const walletQuery = useQuery({
    queryKey: ["ai", "wallet"],
    queryFn: () => fetchWallet(),
    enabled: canReadWallet,
  });

  const pendingProposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () => fetchPendingProposalsCount(),
  });

  const askMutation = useMutation({
    mutationFn: () =>
      assistantChat({
        capability: mode === "strict_rag" ? "cs_support" : "sales_quoting",
        conversation_id: conversationId ?? undefined,
        message: query,
        strict_rag: mode === "strict_rag",
        channel_type: "web_livechat",
        context: { mode },
      }),
    onSuccess: (res) => {
      setLastResult(res);
    },
  });

  const briefMutation = useMutation({
    mutationFn: () =>
      fetchInboxAssist("daily_brief", { conversationId: conversationId ?? null, language: "zh" }),
    onSuccess: (result) => {
      setBriefError(null);
      setLastBrief(copilotBriefText(result.text));
    },
    onError: (error) => {
      setLastBrief("");
      setBriefError(error.message);
    },
  });

  const pendingCount = pendingProposalsQuery.data?.total ?? 0;
  const wallet = walletQuery.data;
  const remainingCredits =
    (wallet?.included_credits_remaining ?? 0) + (wallet?.topup_credits_balance ?? 0);
  const degradedToL0 = Boolean(
    wallet && (wallet.auto_downgrade_to_l0 && remainingCredits <= 0)
  );
  const safeCards = sanitizeCopilotCards(capabilitiesQuery.data?.capabilities ?? []);

  return (
    <div className="fixed right-5 bottom-5 z-40">
      {!open ? (
        <button
          type="button"
          data-testid="copilot-fab-btn"
          onClick={() => setOpen(true)}
          aria-label={bi("打开 AI 副驾驶", "Open AI copilot")}
          className="relative flex h-12 w-12 items-center justify-center rounded-full bg-blue-600 text-white shadow-lg shadow-blue-600/25 hover:bg-blue-700"
        >
          <Bot className="h-6 w-6" />
          {pendingCount > 0 && (
            <span className="absolute -top-1 -right-1 rounded-full bg-rose-600 px-1.5 py-0.5 text-[10px] font-bold text-white">
              {pendingCount}
            </span>
          )}
        </button>
      ) : (
        <div
          data-testid="copilot-drawer"
          className="flex h-[560px] w-96 flex-col rounded-2xl border border-slate-200 bg-white shadow-[0_24px_60px_-20px_rgba(15,23,42,0.45)]"
        >
          <div className="flex items-center justify-between rounded-t-2xl border-b border-slate-800 bg-[#0B1220] px-3.5 py-3 text-white">
            <div className="flex items-center gap-2">
              <Bot className="h-4 w-4 text-emerald-400" />
              <span className="text-sm font-semibold">{bi("AI 业务副驾驶", "AI Workbench Copilot")}</span>
              {degradedToL0 && (
                <span className="rounded bg-amber-500 px-1.5 py-0.5 text-[10px] font-bold text-slate-950">
                  L0 Fallback
                </span>
              )}
            </div>

            <div className="flex items-center gap-2">
              <Link
                href="/ai/proposals"
                data-testid="copilot-pending-link"
                className="inline-flex items-center gap-1 rounded-full bg-rose-600 px-2 py-0.5 text-[11px] font-semibold text-white hover:bg-rose-700"
              >
                <span>
                  {t("pendingApprovals")}: {pendingCount}
                </span>
                <ArrowUpRight className="h-3 w-3" />
              </Link>
              <button
                type="button"
                onClick={() => setOpen(false)}
                className="rounded p-1 hover:bg-slate-800"
                aria-label={bi("关闭副驾驶", "Close copilot")}
              >
                <X className="h-4 w-4" />
              </button>
            </div>
          </div>

          <div className="flex items-center justify-between border-b border-slate-200 bg-slate-50 px-3 py-2 text-xs">
            <SegmentedTabs
              ariaLabel={t("strictRag")}
              value={mode}
              onChange={setMode}
              size="sm"
              options={[
                {
                  value: "global",
                  label: t("globalMode"),
                  testId: "copilot-mode-global",
                },
                {
                  value: "strict_rag",
                  label: (
                    <span className="inline-flex items-center gap-1">
                      <ShieldCheck className="h-3 w-3" aria-hidden="true" />
                      {t("strictRag")}
                    </span>
                  ),
                  testId: "copilot-mode-strict-rag",
                },
              ]}
            />

            {wallet && (
              <span className="text-[11px] tabular-nums text-slate-500">
                {bi("额度", "Credits")} {remainingCredits}
              </span>
            )}
          </div>

          <div className="border-b border-slate-200 bg-white px-3 py-2">
            <div className="flex flex-wrap gap-1.5">
              {COPILOT_CHIPS.map(({ id, label, labelEn, icon: Icon, ...rest }) => {
                const href = "href" in rest ? rest.href : undefined;
                return href ? (
                  <Link
                    key={id}
                    href={href}
                    data-testid={`copilot-chip-${id}`}
                    className="inline-flex items-center gap-1 rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 text-[11px] font-medium text-slate-700 transition-colors hover:border-blue-300 hover:bg-blue-50 hover:text-blue-700"
                  >
                    <Icon className="h-3 w-3" aria-hidden="true" />
                    {bi(label, labelEn)}
                  </Link>
                ) : (
                  <button
                    key={id}
                    type="button"
                    data-testid={`copilot-chip-${id}`}
                    disabled={briefMutation.isPending}
                    onClick={() => briefMutation.mutate()}
                    className="inline-flex items-center gap-1 rounded-full border border-blue-200 bg-blue-50 px-2.5 py-1 text-[11px] font-semibold text-blue-700 transition-colors hover:bg-blue-100 disabled:opacity-50"
                  >
                    <Icon className="h-3 w-3" aria-hidden="true" />
                    {briefMutation.isPending ? bi("生成中…", "Generating…") : bi(label, labelEn)}
                  </button>
                );
              })}
            </div>
            {briefError && (
              <p role="alert" data-testid="copilot-brief-error" className="mt-1.5 text-[11px] text-rose-700">
                {briefError}
              </p>
            )}
            {lastBrief && (
              <p
                data-testid="copilot-brief"
                className="mt-2 max-h-32 overflow-y-auto whitespace-pre-wrap rounded-xl border border-slate-200 bg-slate-50 p-2.5 text-[11px] leading-relaxed text-slate-800"
              >
                {lastBrief}
              </p>
            )}
          </div>

          <div className="flex-1 space-y-2.5 overflow-y-auto p-3">
            <div className="grid grid-cols-2 gap-2">
              {safeCards.map((card) => (
                <div
                  key={card.capability}
                  className="rounded-xl border border-slate-200 bg-white p-2.5 text-xs shadow-xs"
                >
                  <div className="flex items-center justify-between gap-1">
                    <span className="font-semibold text-slate-800">
                      {bi(CAPABILITY_TITLES[card.capability].zh, CAPABILITY_TITLES[card.capability].en)}
                    </span>
                    <span className="rounded bg-white px-1 py-0.5 text-[10px] font-medium text-slate-600">
                      {card.autonomy_level}
                    </span>
                  </div>
                  <div className="mt-1.5 space-y-1">
                    {card.metrics.map((metric) => (
                      <div key={metric.label_key} className="flex items-center justify-between">
                        <span className="text-[11px] text-slate-500">
                          {bi(
                            SAFE_METRIC_LABELS[metric.label_key].zh,
                            SAFE_METRIC_LABELS[metric.label_key].en
                          )}
                        </span>
                        <span className="font-semibold text-slate-900">
                          {renderSafeMetricValue(metric)}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>

            {lastResult && (
              <div
                data-testid="copilot-answer-box"
                className={`rounded-xl border p-3 text-xs ${
                  lastResult.should_abstain
                    ? "border-amber-300 bg-amber-50 text-amber-950"
                    : "border-blue-200 bg-blue-50/70 text-slate-900"
                }`}
              >
                {lastResult.should_abstain && (
                  <div className="mb-1 flex items-center gap-1 font-semibold text-amber-800">
                    <AlertTriangle className="h-3.5 w-3.5" />
                    <span>Abstained (Strict RAG &lt; 0.62) — Escalated to Human</span>
                  </div>
                )}
                {/* Citations are rendered once as verified chips below, so
                    strip the inline [doc#chunk] markers from the prose to
                    avoid rendering the same citation twice. */}
                <p className="whitespace-pre-wrap">
                  {(lastResult.reply_text ?? "")
                    .replace(/\[[^\]]+#[^\]]+\]/g, "")
                    .replace(/  +/g, " ")
                    .trim()}
                </p>
                {lastResult.citations.length > 0 && (
                  <div className="mt-1.5 flex flex-wrap gap-1">
                    {lastResult.citations.map((cit) => (
                      <span
                        key={`${cit.doc_id}-${cit.chunk_id}`}
                        className="rounded bg-white px-1.5 py-0.5 text-[10px] font-medium text-emerald-700"
                      >
                        [{cit.doc_id}#{cit.chunk_id}] ({cit.score.toFixed(2)})
                      </span>
                    ))}
                  </div>
                )}
                {lastResult.tool_calls?.[0]?.tool_call_id && (
                  <div className="mt-1 text-[10px] text-slate-500">
                    {bi("SQL 工具已校验", "SQL tool verified")}：{lastResult.tool_calls[0].tool_call_id}
                  </div>
                )}
              </div>
            )}
          </div>

          <div className="border-t border-slate-200 p-2.5">
            <div className="flex gap-1.5">
              <input
                data-testid="copilot-query-input"
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={bi("问库存、物流进度或知识库条款…", "Ask SKU stock, order tracking or KB terms…")}
                className="flex-1 rounded-md border border-slate-300 px-2.5 py-1.5 text-xs focus:border-blue-500 focus:outline-none"
              />
              <button
                type="button"
                data-testid="copilot-query-submit"
                disabled={!query.trim() || askMutation.isPending}
                onClick={() => askMutation.mutate()}
                className="rounded-md bg-blue-600 px-3 py-1.5 text-white hover:bg-blue-700 disabled:opacity-50"
              >
                <Send className="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
