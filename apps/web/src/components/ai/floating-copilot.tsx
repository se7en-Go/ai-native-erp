"use client";

import React, { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  Activity,
  AlertTriangle,
  ArrowUpRight,
  Bot,
  CalendarClock,
  ClipboardCheck,
  MessageSquare,
  Newspaper,
  PackageX,
  Radar,
  RefreshCw,
  Send,
  ShieldCheck,
  X,
} from "lucide-react";
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
import { SegmentedTabs, type SegmentedTabOption } from "@/components/ui/segmented-tabs";
import { useI18n } from "@/lib/i18n";
import { useAuthSession } from "@/components/providers";
import { fetchInboxAssist } from "@/lib/phase3b-data";
import {
  dispatchPatrolEmployee,
  fetchDailyPatrolBrief,
  patrolHealthDescriptor,
  type PatrolDispatchRead,
} from "@/lib/api-client/pack-d-patrol";

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

/* ------------------------------------------------------------------ *
 * Channel analytics (biz_analytics) — deterministic SQL payload rendering.
 * Every value below comes from `get_live_channel_sales_analytics`
 * (`totals` + `channels`) or `query_readonly_analytics_sql` (`rows`). Only
 * scalars are printed, so a nested payload can never render [object Object].
 * ------------------------------------------------------------------ */

type CopilotMode = "global" | "strict_rag" | "analytics";
type AnalyticsWindow = "7d" | "30d" | "90d";
type CopilotAskOverride = {
  message?: string;
  mode?: CopilotMode;
  window?: AnalyticsWindow;
};

const ANALYTICS_WINDOWS: readonly AnalyticsWindow[] = ["7d", "30d", "90d"];

function asPlainRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function scalarText(value: unknown): string {
  if (typeof value === "string") return value.trim() === "" ? "—" : value;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return "—";
}

function moneyText(value: unknown): string {
  if (typeof value !== "string" && typeof value !== "number") return "—";
  const normalized = typeof value === "string" ? value.replace(/,/g, "").trim() : value;
  if (normalized === "") return "—";
  const num = Number(normalized);
  if (!Number.isFinite(num)) return "—";
  return formatMoney(num, "USD");
}

function percentText(value: unknown): string {
  if (typeof value !== "string" && typeof value !== "number") return "—";
  const normalized = typeof value === "string" ? value.trim() : value;
  if (normalized === "") return "—";
  const num = Number(normalized);
  if (!Number.isFinite(num)) return "—";
  return `${num.toFixed(2)}%`;
}

function multiplierText(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value !== "string" && typeof value !== "number") return "—";
  const normalized = typeof value === "string" ? value.trim() : value;
  if (normalized === "" || normalized === "None") return "—";
  const num = Number(normalized);
  if (!Number.isFinite(num)) return "—";
  return `${num.toFixed(2)}x`;
}

function countText(value: unknown): string {
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  if (typeof value === "string" && value.trim() !== "" && Number.isFinite(Number(value))) {
    return String(Number(value));
  }
  return "—";
}

type ChannelAnalyticsRow = {
  key: string;
  label: string;
  channelType: string;
  orderCount: unknown;
  gmvBase: unknown;
  refundRatePct: unknown;
  netMarginBase: unknown;
  roas: unknown;
};

type ChannelAnalyticsTotals = {
  window: string | null;
  gmvBase: unknown;
  netMarginBase: unknown;
  adSpendBase: unknown;
  roas: unknown;
  refundRatePct: unknown;
  orderCount: unknown;
};

type ChannelAnalyticsView =
  | {
      kind: "channels";
      totals: ChannelAnalyticsTotals | null;
      rows: ChannelAnalyticsRow[];
    }
  | {
      kind: "rows";
      columns: string[];
      rows: Record<string, unknown>[];
    };

function isScalarCell(value: unknown): boolean {
  return (
    value === null ||
    typeof value === "string" ||
    (typeof value === "number" && Number.isFinite(value))
  );
}

/** Narrow the first tool call's `data` into a renderable analytics view. */
export function readChannelAnalytics(data: unknown): ChannelAnalyticsView | null {
  const record = asPlainRecord(data);
  if (!record) return null;

  const totalsRecord = asPlainRecord(record.totals);
  const rawChannels = Array.isArray(record.channels) ? record.channels : null;

  if (totalsRecord || rawChannels) {
    const rows: ChannelAnalyticsRow[] = [];
    (rawChannels ?? []).forEach((raw, index) => {
      const row = asPlainRecord(raw);
      if (!row) return;
      const channelType = typeof row.channel_type === "string" ? row.channel_type : "";
      const rawLabel = typeof row.channel_label === "string" ? row.channel_label.trim() : "";
      rows.push({
        key: `${channelType || "channel"}-${index}`,
        label: rawLabel !== "" ? rawLabel : channelType || `#${index + 1}`,
        channelType,
        orderCount: row.order_count,
        gmvBase: row.gmv_base,
        refundRatePct: row.refund_rate_pct,
        netMarginBase: row.net_margin_base,
        roas: row.roas,
      });
    });

    return {
      kind: "channels",
      totals: totalsRecord
        ? {
            window:
              typeof record.window === "string"
                ? record.window
                : typeof totalsRecord.window === "string"
                ? totalsRecord.window
                : null,
            gmvBase: totalsRecord.gmv_base,
            netMarginBase: totalsRecord.net_margin_base,
            adSpendBase: totalsRecord.ad_spend_base,
            roas: totalsRecord.roas,
            refundRatePct: totalsRecord.refund_rate_pct,
            orderCount: totalsRecord.order_count,
          }
        : null,
      rows,
    };
  }

  const rawRows = Array.isArray(record.rows) ? record.rows : null;
  if (!rawRows) return null;

  const objectRows = rawRows.flatMap((row) => {
    const parsed = asPlainRecord(row);
    return parsed ? [parsed] : [];
  });
  if (objectRows.length === 0) return null;

  const columns: string[] = [];
  for (const row of objectRows) {
    for (const [key, value] of Object.entries(row)) {
      if (columns.length >= 6) break;
      if (!columns.includes(key) && isScalarCell(value)) columns.push(key);
    }
  }
  if (columns.length === 0) return null;

  return { kind: "rows", columns, rows: objectRows };
}

const CHANNEL_TABLE_HEADERS = [
  { zh: "渠道", en: "Channel" },
  { zh: "订单", en: "Orders" },
  { zh: "GMV", en: "GMV" },
  { zh: "退款率", en: "Refund" },
  { zh: "净毛利", en: "Net margin" },
  { zh: "ROAS", en: "ROAS" },
] as const;

function ChannelAnalyticsPanel({ view }: { view: ChannelAnalyticsView }) {
  const { bi } = useI18n();

  return (
    <div
      data-testid="copilot-analytics-table"
      className="mt-2 rounded-lg border border-slate-200 bg-white p-2"
    >
      {view.kind === "channels" && view.totals ? (
        <dl
          data-testid="copilot-analytics-totals"
          className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-slate-100 pb-1.5"
        >
          {[
            { id: "gmv", label: bi("GMV", "GMV"), value: moneyText(view.totals.gmvBase) },
            {
              id: "net-margin",
              label: bi("净毛利", "Net margin"),
              value: moneyText(view.totals.netMarginBase),
            },
            {
              id: "ad-spend",
              label: bi("广告花费", "Ad spend"),
              value: moneyText(view.totals.adSpendBase),
            },
            { id: "roas", label: "ROAS", value: multiplierText(view.totals.roas) },
            {
              id: "window",
              label: bi("窗口", "Window"),
              value: view.totals.window ?? "—",
            },
          ].map((cell) => (
            <div key={cell.id} className="flex items-baseline gap-1">
              <dt className="text-[10px] font-medium text-slate-500">{cell.label}</dt>
              <dd
                data-testid={`copilot-analytics-total-${cell.id}`}
                className="text-[11px] font-bold tabular-nums text-slate-900"
              >
                {cell.value}
              </dd>
            </div>
          ))}
        </dl>
      ) : null}

      {view.kind === "channels" ? (
        view.rows.length > 0 ? (
          <table className="mt-1.5 w-full text-left text-[10.5px]">
            <thead>
              <tr className="text-[10px] font-semibold text-slate-500">
                {CHANNEL_TABLE_HEADERS.map((header) => (
                  <th key={header.en} scope="col" className="pb-1 pr-1.5 font-semibold">
                    {bi(header.zh, header.en)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {view.rows.map((row) => (
                <tr
                  key={row.key}
                  data-testid={`copilot-analytics-channel-${row.channelType || "row"}`}
                  className="border-t border-slate-100 text-slate-800"
                >
                  <th scope="row" className="py-0.5 pr-1.5 text-left font-semibold">
                    {row.label}
                  </th>
                  <td className="py-0.5 pr-1.5 tabular-nums">{countText(row.orderCount)}</td>
                  <td className="py-0.5 pr-1.5 tabular-nums">{moneyText(row.gmvBase)}</td>
                  <td className="py-0.5 pr-1.5 tabular-nums">{percentText(row.refundRatePct)}</td>
                  <td className="py-0.5 pr-1.5 tabular-nums">{moneyText(row.netMarginBase)}</td>
                  <td className="py-0.5 tabular-nums">{multiplierText(row.roas)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p data-testid="copilot-analytics-empty" className="pt-1.5 text-[10.5px] text-slate-500">
            {bi("该窗口内没有渠道成交数据。", "No channel sales in this window.")}
          </p>
        )
      ) : (
        <div className="mt-1.5 overflow-x-auto">
          <table className="w-full text-left text-[10.5px]">
            <thead>
              <tr className="text-[10px] font-semibold text-slate-500">
                {view.columns.map((column) => (
                  <th key={column} scope="col" className="pb-1 pr-1.5 font-semibold">
                    {column}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {view.rows.map((row, index) => (
                <tr
                  key={`sql-row-${index}`}
                  data-testid={`copilot-analytics-sql-row-${index}`}
                  className="border-t border-slate-100 text-slate-800"
                >
                  {view.columns.map((column) => (
                    <td key={column} className="py-0.5 pr-1.5 tabular-nums">
                      {scalarText(row[column])}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
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

const PATROL_SEVERITY_DOT: Record<string, string> = {
  high: "bg-rose-500",
  medium: "bg-amber-500",
  info: "bg-sky-500",
};

const PATROL_HEALTH_BAR: Record<string, string> = {
  emerald: "bg-emerald-500",
  amber: "bg-amber-500",
  rose: "bg-rose-500",
};

/**
 * 「主动巡检日报」：经营健康度 + 六类实时告警，每条告警都能一键呼叫负责的
 * 数字员工。数据全部来自服务端 SQL 巡检端点，前端不做任何指标推算。
 */
function PatrolBriefPanel() {
  const { bi } = useI18n();
  const [dispatched, setDispatched] = useState<Record<string, PatrolDispatchRead>>({});

  const briefQuery = useQuery({
    queryKey: ["ai", "patrol", "daily-brief"],
    queryFn: () => fetchDailyPatrolBrief(),
  });

  const dispatchMutation = useMutation({
    mutationFn: (employeeCode: string) => dispatchPatrolEmployee(employeeCode),
    onSuccess: (result, employeeCode) => {
      setDispatched((prev) => ({ ...prev, [employeeCode]: result }));
    },
  });

  const brief = briefQuery.data;
  const score = typeof brief?.health_score === "number" ? brief.health_score : 0;
  const health = patrolHealthDescriptor(score);
  const alerts = brief?.alerts ?? [];

  return (
    <div
      data-testid="copilot-patrol-panel"
      className="flex-1 space-y-2.5 overflow-y-auto p-3"
    >
      <section
        data-testid="patrol-health-bar"
        aria-label={bi("经营健康度", "Business health")}
        className="rounded-2xl border border-emerald-200/80 bg-gradient-to-br from-emerald-50 via-white to-white p-3 shadow-[0_1px_2px_rgba(15,23,42,0.04)]"
      >
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <p className="inline-flex items-center gap-1 text-[10.5px] font-bold tracking-wide text-emerald-800 uppercase">
              <Activity className="h-3 w-3" aria-hidden="true" />
              {bi("经营健康度", "Business health")}
            </p>
            <p className="mt-0.5 text-[11px] font-semibold text-slate-700">
              {bi(health.zh, health.en)}
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            <span
              data-testid="patrol-health-score"
              className="rounded-lg bg-white px-2 py-0.5 text-[13px] font-bold tabular-nums text-emerald-700 ring-1 ring-emerald-200"
            >
              {score}/100
            </span>
            <button
              type="button"
              data-testid="patrol-refresh-btn"
              onClick={() => void briefQuery.refetch()}
              disabled={briefQuery.isFetching}
              aria-label={bi("刷新巡检日报", "Refresh patrol brief")}
              className="rounded-lg border border-emerald-200 bg-white p-1.5 text-emerald-700 transition-colors hover:bg-emerald-50 disabled:opacity-50"
            >
              <RefreshCw
                className={`h-3 w-3 ${briefQuery.isFetching ? "animate-spin" : ""}`}
                aria-hidden="true"
              />
            </button>
          </div>
        </div>

        <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-emerald-100">
          <div
            className={`h-full rounded-full transition-all duration-500 ${
              PATROL_HEALTH_BAR[health.tone] ?? "bg-emerald-500"
            }`}
            style={{ width: `${Math.min(100, Math.max(0, score))}%` }}
          />
        </div>

        {brief ? (
          <p
            data-testid="patrol-summary"
            className="mt-2 text-[11px] leading-5 text-slate-700"
          >
            {bi(brief.summary_zh, brief.summary_en)}
          </p>
        ) : (
          <p data-testid="patrol-loading" className="mt-2 text-[11px] text-slate-500">
            {bi("正在执行租户范围内的实时巡检…", "Running the tenant-scoped live patrol…")}
          </p>
        )}
      </section>

      {briefQuery.error ? (
        <p
          role="alert"
          data-testid="patrol-error"
          className="rounded-xl border border-rose-200 bg-rose-50 p-2.5 text-[11px] font-medium text-rose-800"
        >
          {bi("巡检数据加载失败，请重试。", "Failed to load the patrol brief. Please retry.")}
        </p>
      ) : null}

      {alerts.map((alert) => {
        const result = dispatched[alert.employee_code];
        const pending = dispatchMutation.isPending && dispatchMutation.variables === alert.employee_code;
        return (
          <article
            key={alert.code}
            data-testid={`patrol-alert-${alert.code}`}
            className="rounded-2xl border border-slate-200 bg-white p-2.5 shadow-[0_1px_2px_rgba(15,23,42,0.04)]"
          >
            <div className="flex items-start justify-between gap-2">
              <div className="flex min-w-0 items-start gap-1.5">
                <span
                  className={`mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full ${
                    PATROL_SEVERITY_DOT[alert.severity] ?? "bg-slate-400"
                  }`}
                  aria-hidden="true"
                />
                <div className="min-w-0">
                  <h3 className="text-[12px] font-semibold tracking-tight text-slate-900">
                    {bi(alert.title_zh, alert.title_en)}
                  </h3>
                  <p className="mt-0.5 text-[11px] leading-5 text-slate-600">
                    {bi(alert.detail_zh, alert.detail_en)}
                  </p>
                </div>
              </div>
              <span className="shrink-0 rounded-full bg-rose-50 px-1.5 py-0.5 text-[10.5px] font-bold tabular-nums text-rose-700 ring-1 ring-rose-100">
                {alert.count}
              </span>
            </div>

            <div className="mt-2 flex items-center justify-between gap-2 border-t border-slate-100 pt-2">
              <span className="inline-flex min-w-0 items-center gap-1 text-[10.5px] font-medium text-slate-600">
                <Bot className="h-3 w-3 shrink-0 text-emerald-600" aria-hidden="true" />
                <span className="truncate">{alert.employee_name}</span>
              </span>
              <button
                type="button"
                data-testid={`patrol-dispatch-${alert.employee_code}`}
                disabled={pending}
                onClick={() => dispatchMutation.mutate(alert.employee_code)}
                className="inline-flex shrink-0 items-center gap-1 rounded-lg bg-blue-600 px-2 py-1 text-[11px] font-semibold text-white transition-colors hover:bg-blue-700 disabled:opacity-50"
              >
                <Radar className="h-3 w-3" aria-hidden="true" />
                {pending ? bi("呼叫中…", "Dispatching…") : bi("呼叫", "Dispatch")}
              </button>
            </div>

            {result ? (
              <div
                data-testid={`patrol-dispatch-result-${alert.employee_code}`}
                className="mt-2 rounded-xl border border-emerald-200 bg-emerald-50 p-2 text-[10.5px] leading-4 text-emerald-900"
              >
                <p className="font-bold">
                  {bi("已创建跟进任务", "Follow-up task created")} · {result.employee_name}
                </p>
                <p className="mt-0.5">{bi(result.action_summary_zh, result.action_summary_en)}</p>
                <Link
                  href={result.recommended_href}
                  className="mt-1 inline-flex items-center gap-0.5 font-semibold text-emerald-800 underline decoration-emerald-300 underline-offset-2"
                >
                  {bi("查看跟进", "Open follow-up")}
                  <ArrowUpRight className="h-3 w-3" aria-hidden="true" />
                </Link>
              </div>
            ) : null}
          </article>
        );
      })}

      {dispatchMutation.error ? (
        <p
          role="alert"
          data-testid="patrol-dispatch-error"
          className="rounded-xl border border-rose-200 bg-rose-50 p-2.5 text-[11px] font-medium text-rose-800"
        >
          {bi("派工失败，请稍后重试。", "Dispatch failed. Please retry.")}
        </p>
      ) : null}
    </div>
  );
}

export function FloatingCopilot({ conversationId }: { conversationId?: string | null }) {
  const { t, bi } = useI18n();
  const { principal } = useAuthSession();
  const [open, setOpen] = useState(false);
  // 默认停留在 AI 对话：底部输入框常驻，两块面板只切换顶部主体。
  const [panel, setPanel] = useState<"chat" | "patrol">("chat");
  const [mode, setMode] = useState<CopilotMode>("global");
  const [analyticsWindow, setAnalyticsWindow] = useState<AnalyticsWindow>("30d");
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
  // BIZ_ANALYTICS is allowed for owner | admin | manager | finance; the backend
  // answers `agent` with 403, so the analytics entry points stay hidden for them.
  const canQueryAnalytics =
    principal?.role === "owner" ||
    principal?.role === "admin" ||
    principal?.role === "manager" ||
    principal?.role === "finance";
  const walletQuery = useQuery({
    queryKey: ["ai", "wallet"],
    queryFn: () => fetchWallet(),
    enabled: canReadWallet,
  });

  const pendingProposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () => fetchPendingProposalsCount(),
  });

  const askMutation = useMutation<
    AssistantChatResult,
    Error,
    CopilotAskOverride | undefined
  >({
    mutationFn: (override) => {
      const effectiveMode = override?.mode ?? mode;
      const effectiveWindow = override?.window ?? analyticsWindow;
      const message = override?.message ?? query;
      return assistantChat({
        capability:
          effectiveMode === "analytics"
            ? "biz_analytics"
            : effectiveMode === "strict_rag"
            ? "cs_support"
            : "sales_quoting",
        conversation_id: conversationId ?? undefined,
        message,
        strict_rag: effectiveMode === "strict_rag",
        channel_type: "web_livechat",
        context:
          effectiveMode === "analytics"
            ? { mode: "analytics", window: effectiveWindow }
            : { mode: effectiveMode },
      });
    },
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
  const analyticsView = readChannelAnalytics(lastResult?.tool_calls?.[0]?.data ?? null);

  const modeTabs: SegmentedTabOption<CopilotMode>[] = [
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
  ];
  if (canQueryAnalytics) {
    modeTabs.push({
      value: "analytics",
      label: (
        <span className="inline-flex items-center gap-1">
          <Activity className="h-3 w-3" aria-hidden="true" />
          {bi("经营分析", "Analytics")}
        </span>
      ),
      testId: "copilot-mode-analytics",
    });
  }

  const askAnalyticsChip = () => {
    const message = bi(
      "查看近30天全平台 GMV、退款率、佣金、净毛利与 ROAS",
      "Show 30d multi-platform GMV, refund rate, commission, net margin, and ROAS"
    );
    setPanel("chat");
    setMode("analytics");
    if (query.trim() === "") setQuery(message);
    askMutation.mutate({ message, mode: "analytics", window: analyticsWindow });
  };

  return (
    <div
      className={
        open
          ? "fixed right-4 top-20 z-40"
          : "fixed right-0 top-1/2 z-40 -translate-y-1/2"
      }
    >
      {!open ? (
        <button
          type="button"
          data-testid="copilot-fab-btn"
          onClick={() => setOpen(true)}
          aria-label={bi("打开 AI 副驾驶", "Open AI copilot")}
          title={bi("AI 副驾驶与巡检日报", "AI Copilot & Daily Patrol")}
          className="group relative flex flex-col items-center justify-center gap-1 rounded-l-xl border border-r-0 border-blue-500/40 bg-blue-600 px-2.5 py-3 text-white shadow-lg shadow-blue-600/25 transition-all hover:bg-blue-700 hover:pr-3.5"
        >
          <Bot className="h-5 w-5" />
          <span className="text-[10px] font-semibold tracking-wider">AI</span>
          {pendingCount > 0 && (
            <span className="absolute -top-2 -left-2 flex h-5 min-w-5 items-center justify-center rounded-full bg-rose-600 px-1 text-[10px] font-bold text-white ring-2 ring-white dark:ring-gray-900">
              {pendingCount}
            </span>
          )}
        </button>
      ) : (
        <div
          data-testid="copilot-drawer"
          className="flex h-[min(600px,calc(100vh-6.5rem))] w-[min(390px,calc(100vw-2rem))] flex-col rounded-2xl border border-gray-200 bg-white shadow-2xl dark:border-gray-800 dark:bg-gray-900"
        >
          <div className="flex items-center justify-between rounded-t-2xl border-b border-blue-700/40 bg-gradient-to-r from-blue-600 to-indigo-600 px-3.5 py-3 text-white">
            <div className="flex items-center gap-2">
              <Bot className="h-4 w-4 text-blue-100" />
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

          <div className="flex items-center justify-between gap-2 border-b border-slate-200 bg-white px-3 py-2">
            <SegmentedTabs
              ariaLabel={bi("副驾驶面板", "Copilot panels")}
              value={panel}
              onChange={setPanel}
              size="sm"
              options={[
                {
                  value: "chat",
                  label: (
                    <span className="inline-flex items-center gap-1">
                      <MessageSquare className="h-3 w-3" aria-hidden="true" />
                      {bi("AI 对话", "AI chat")}
                    </span>
                  ),
                  testId: "copilot-tab-chat",
                },
                {
                  value: "patrol",
                  label: (
                    <span className="inline-flex items-center gap-1">
                      <Radar className="h-3 w-3" aria-hidden="true" />
                      {bi("主动巡检日报", "Patrol brief")}
                    </span>
                  ),
                  testId: "copilot-tab-patrol",
                },
              ]}
            />
            <span className="shrink-0 text-[10.5px] font-medium text-slate-500">
              {panel === "patrol"
                ? bi("实时 SQL 巡检", "Live SQL patrol")
                : bi("底栏随时可提问", "Ask any time below")}
            </span>
          </div>

          {panel === "chat" ? (
            <>
            <div className="flex items-center justify-between border-b border-slate-200 bg-slate-50 px-3 py-2 text-xs">
              <SegmentedTabs
                ariaLabel={t("strictRag")}
                value={mode}
                onChange={setMode}
                size="sm"
                options={modeTabs}
              />

              {wallet && (
                <span className="text-[11px] tabular-nums text-slate-500">
                  {bi("额度", "Credits")} {remainingCredits}
                </span>
              )}
            </div>

            {mode === "analytics" && (
              <div
                data-testid="copilot-analytics-window-row"
                className="flex items-center gap-1 border-b border-slate-200 bg-white px-3 py-1.5"
              >
                <span className="mr-0.5 text-[10px] font-medium text-slate-500">
                  {bi("统计窗口", "Window")}
                </span>
                {ANALYTICS_WINDOWS.map((window) => {
                  const active = analyticsWindow === window;
                  return (
                    <button
                      key={window}
                      type="button"
                      data-testid={`copilot-analytics-window-${window}`}
                      aria-pressed={active}
                      onClick={() => setAnalyticsWindow(window)}
                      className={`rounded-full px-2 py-0.5 text-[10.5px] font-semibold tabular-nums transition-colors ${
                        active
                          ? "bg-blue-600 text-white"
                          : "bg-slate-100 text-slate-600 hover:bg-slate-200"
                      }`}
                    >
                      {window}
                    </button>
                  );
                })}
              </div>
            )}

            <div className="border-b border-slate-200 bg-white px-3 py-2">
              <div className="flex flex-wrap gap-1.5">
                {canQueryAnalytics && (
                  <button
                    type="button"
                    data-testid="copilot-chip-channel-analytics"
                    disabled={askMutation.isPending}
                    onClick={askAnalyticsChip}
                    className="inline-flex items-center gap-1 rounded-full border border-indigo-200 bg-indigo-50 px-2.5 py-1 text-[11px] font-semibold text-indigo-700 transition-colors hover:bg-indigo-100 disabled:opacity-50"
                  >
                    <Activity className="h-3 w-3" aria-hidden="true" />
                    {bi("全平台 GMV/ROAS", "Channel GMV/ROAS")}
                  </button>
                )}
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
                  {analyticsView && <ChannelAnalyticsPanel view={analyticsView} />}
                </div>
              )}
            </div>
            </>
          ) : (
            <PatrolBriefPanel />
          )}

          <div className="border-t border-slate-200 p-2.5">
            <div className="flex gap-1.5">
              <input
                data-testid="copilot-query-input"
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={
                  mode === "analytics"
                    ? bi(
                        "问本窗口内全平台 GMV、退款率或单渠道 ROAS…",
                        "Ask window GMV, refund rate or per-channel ROAS…"
                      )
                    : bi(
                        "问库存、物流进度或知识库条款…",
                        "Ask SKU stock, order tracking or KB terms…"
                      )
                }
                className="flex-1 rounded-md border border-slate-300 px-2.5 py-1.5 text-xs focus:border-blue-500 focus:outline-none"
              />
              <button
                type="button"
                data-testid="copilot-query-submit"
                disabled={!query.trim() || askMutation.isPending}
                onClick={() => {
                  // 在巡检页直接提问时自动切回对话面板，答案永远可见。
                  setPanel("chat");
                  askMutation.mutate();
                }}
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
