"use client";

import React, { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeftRight,
  BadgeCheck,
  Download,
  FileSpreadsheet,
  Landmark,
  Scale,
  Upload,
  Wallet,
  Wand2,
} from "lucide-react";

import { Badge, Card, CardHeader, EmptyState } from "@/components/ui/page";
import type { DualBasisOrderRow, SettlementLineRead } from "@/lib/api-client/types.gen";
import {
  autoMatchPayouts,
  fetchDualBasisProfit,
  fetchPayoutStatements,
  fetchSettlementBatches,
  fetchSettlementLines,
  formatExactMoney,
  formatRateE6,
  formatSignedMoney,
  importPayoutStatements,
  importSettlementCsv,
  JOURNAL_FORMATS,
  JOURNAL_FORMAT_LABEL,
  journalExportHref,
  PAYOUT_PROVIDER_LABEL,
  PAYOUT_PROVIDERS,
  payoutsFromCsv,
  parseRateToE6,
  SAMPLE_PAYOUT_CSV,
  SAMPLE_SETTLEMENT_CSV,
  SAMPLE_SETTLEMENT_HEADER,
  type JournalFormat,
  type PayoutProvider,
  type SettlementCsvHeader,
} from "@/lib/api-client/track3-settlement";
import type { UserRole } from "@/lib/contracts";
import { useI18n } from "@/lib/i18n";

/** Roles the settlement routes allow to read (mirrors `SETTLEMENT_READ_ROLES`). */
const SETTLEMENT_READ_ROLES: readonly UserRole[] = ["owner", "admin", "manager", "finance"];
/** Roles the settlement routes allow to mutate (mirrors `SETTLEMENT_WRITE_ROLES`). */
const SETTLEMENT_WRITE_ROLES: readonly UserRole[] = ["owner", "finance", "admin"];

type TabKey = "dual-basis" | "import" | "payout" | "journal";

const TABS: ReadonlyArray<{ key: TabKey; zh: string; en: string }> = [
  { key: "dual-basis", zh: "1. 双口径利润对比", en: "1. Dual-basis profit" },
  { key: "import", zh: "2. 平台结算单导入", en: "2. Settlement import" },
  { key: "payout", zh: "3. PingPong/连连/Payoneer 回款对账", en: "3. Payout matching" },
  { key: "journal", zh: "4. 会计凭证导出", en: "4. Journal export" },
];

function DeltaCell({ value, currency }: { value: string | undefined; currency: string }) {
  const negative = (value ?? "").startsWith("-");
  const zero = value === "0.00" || value === "0.0" || value === undefined;
  return (
    <span
      className={
        "tabular-nums text-[12.5px] font-semibold " +
        (zero
          ? "text-slate-500 dark:text-slate-400"
          : negative
            ? "text-rose-600 dark:text-rose-400"
            : "text-emerald-600 dark:text-emerald-400")
      }
    >
      {formatSignedMoney(value, currency)}
    </span>
  );
}

function KpiTile({
  testId,
  label,
  value,
  hint,
  icon: Icon,
  highlight = false,
}: {
  testId: string;
  label: string;
  value: string;
  hint: string;
  icon: React.ComponentType<{ className?: string }>;
  highlight?: boolean;
}) {
  return (
    <div
      data-testid={testId}
      className={
        "min-w-0 rounded-xl border p-3 " +
        (highlight
          ? "border-blue-500/40 bg-gradient-to-br from-blue-600 via-blue-600 to-indigo-600 text-white shadow-md shadow-blue-600/20"
          : "border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900")
      }
    >
      <div className="flex items-center gap-2">
        <span
          className={
            "grid h-7 w-7 shrink-0 place-items-center rounded-lg " +
            (highlight
              ? "bg-white/15 text-white ring-1 ring-white/25"
              : "bg-blue-50 text-blue-600 ring-1 ring-blue-100 dark:bg-blue-900/30 dark:text-blue-400 dark:ring-blue-800/60")
          }
        >
          <Icon className="h-3.5 w-3.5" />
        </span>
        <span
          className={
            "min-w-0 truncate text-[12px] font-semibold " +
            (highlight ? "text-blue-50" : "text-slate-600 dark:text-slate-400")
          }
        >
          {label}
        </span>
      </div>
      <div
        className={
          "mt-2 truncate text-[18px] font-bold tabular-nums leading-tight " +
          (highlight ? "text-white" : "text-slate-950 dark:text-slate-100")
        }
      >
        {value}
      </div>
      <div
        className={
          "mt-1 truncate text-[11px] font-medium " +
          (highlight ? "text-blue-100/90" : "text-slate-500 dark:text-slate-400")
        }
      >
        {hint}
      </div>
    </div>
  );
}

const INPUT_CLASS =
  "mt-1 h-9 w-full rounded-lg border border-slate-300 bg-white px-2.5 text-[12.5px] text-slate-900 shadow-xs outline-none transition-colors placeholder:text-slate-400 focus:border-blue-500 focus:ring-2 focus:ring-blue-500/15 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-100";
const LABEL_CLASS = "block text-[11px] font-semibold text-slate-600 dark:text-slate-400";
const PRIMARY_BUTTON =
  "inline-flex items-center gap-1.5 rounded-xl bg-blue-600 px-3.5 py-2 text-[12.5px] font-semibold text-white shadow-[0_2px_8px_-2px_rgba(37,99,235,0.55)] transition-colors hover:bg-blue-700 disabled:opacity-50";
const SECONDARY_BUTTON =
  "inline-flex items-center gap-1.5 rounded-xl border border-slate-200 bg-white px-3 py-2 text-[12.5px] font-semibold text-slate-700 transition-colors hover:border-slate-300 hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300 dark:hover:bg-slate-800";

/**
 * 双口径利润 + 平台结算单 + 跨境回款 + 会计凭证。
 *
 * 「估算口径」来自订单确认时的 CVP 快照，「结算口径」来自平台结算单的真实扣费，
 * 差异精确到分（后端 Decimal / NUMERIC，前端只用整数分与 6 位汇率重新排版）。
 */
export function SettlementReconciliationPanel({ role }: { role?: UserRole }) {
  const { bi } = useI18n();
  const queryClient = useQueryClient();
  const [tab, setTab] = useState<TabKey>("dual-basis");

  const canRead = Boolean(role && SETTLEMENT_READ_ROLES.includes(role));
  const canWrite = Boolean(role && SETTLEMENT_WRITE_ROLES.includes(role));

  const dualQuery = useQuery({
    queryKey: ["finance", "dual-basis"],
    enabled: canRead,
    queryFn: () => fetchDualBasisProfit(),
  });
  const batchesQuery = useQuery({
    queryKey: ["finance", "settlements"],
    enabled: canRead,
    queryFn: () => fetchSettlementBatches(),
  });
  const payoutsQuery = useQuery({
    queryKey: ["finance", "payouts"],
    enabled: canRead,
    queryFn: () => fetchPayoutStatements(),
  });

  const [header, setHeader] = useState<SettlementCsvHeader>({ ...SAMPLE_SETTLEMENT_HEADER });
  const [settlementCsv, setSettlementCsv] = useState("");
  const [provider, setProvider] = useState<PayoutProvider>("pingpong");
  const [payoutCsv, setPayoutCsv] = useState("");
  const [selectedBatchId, setSelectedBatchId] = useState<string | null>(null);
  const [journalFormat, setJournalFormat] = useState<JournalFormat>("standard");
  const [feedback, setFeedback] = useState<{ error: boolean; text: string } | null>(null);

  const linesQuery = useQuery({
    queryKey: ["finance", "settlement-lines", selectedBatchId],
    enabled: canRead && Boolean(selectedBatchId),
    queryFn: () => fetchSettlementLines(selectedBatchId as string),
  });

  const invalidateReconciliation = () => {
    for (const key of [
      ["finance", "dual-basis"],
      ["finance", "settlements"],
      ["finance", "payouts"],
      ["finance", "settlement-lines"],
    ]) {
      void queryClient.invalidateQueries({ queryKey: key });
    }
  };

  const importSettlement = useMutation({
    mutationFn: () => importSettlementCsv(header, settlementCsv),
    onSuccess: (batch) => {
      setFeedback({
        error: false,
        text: bi(
          `结算单 ${batch.batch_ref} 已导入 ${batch.line_count} 行，净回款 ${formatExactMoney(batch.net_payout_ccy, batch.currency)}。`,
          `Statement ${batch.batch_ref} imported with ${batch.line_count} rows; net payout ${formatExactMoney(batch.net_payout_ccy, batch.currency)}.`
        ),
      });
      setSelectedBatchId(batch.id);
      invalidateReconciliation();
    },
    onError: (error: Error) => setFeedback({ error: true, text: error.message }),
  });

  const importPayouts = useMutation({
    mutationFn: () =>
      importPayoutStatements({
        provider,
        csv_text: payoutCsv,
      }),
    onSuccess: (result) => {
      setFeedback({
        error: false,
        text: bi(
          `回款导入 ${result.imported_count} 笔（跳过 ${result.skipped_count} 笔重复），已自动匹配 ${result.matched_count} 笔。`,
          `Imported ${result.imported_count} payouts (${result.skipped_count} duplicates skipped) and matched ${result.matched_count}.`
        ),
      });
      invalidateReconciliation();
    },
    onError: (error: Error) => setFeedback({ error: true, text: error.message }),
  });

  const matchPayouts = useMutation({
    mutationFn: () => autoMatchPayouts(),
    onSuccess: (result) => {
      const currency = dualQuery.data?.base_currency ?? "CNY";
      setFeedback({
        error: false,
        text: bi(
          `本次匹配 ${result.matched_count} 笔，剩余 ${result.remaining_unmatched_count} 笔未匹配；累计汇兑损益 ${formatExactMoney(result.total_fx_variance_base, currency)}。`,
          `Matched ${result.matched_count} payout(s), ${result.remaining_unmatched_count} still unmatched; cumulative FX gain/loss ${formatExactMoney(result.total_fx_variance_base, currency)}.`
        ),
      });
      invalidateReconciliation();
    },
    onError: (error: Error) => setFeedback({ error: true, text: error.message }),
  });

  const dual = dualQuery.data;
  const baseCurrency = dual?.base_currency ?? "CNY";
  const rows: DualBasisOrderRow[] = useMemo(() => dual?.rows ?? [], [dual]);
  const batches = batchesQuery.data?.items ?? [];
  const payouts = payoutsQuery.data?.items ?? [];
  const settlementLines: SettlementLineRead[] = linesQuery.data?.items ?? [];

  if (!canRead) {
    return null;
  }

  const queryError = dualQuery.error ?? batchesQuery.error ?? payoutsQuery.error;

  return (
    <section data-testid="settlement-reconciliation-panel" className="space-y-3.5">
      <header className="flex flex-wrap items-end justify-between gap-2">
        <div className="min-w-0">
          <h2 className="flex items-center gap-2 text-[15px] font-bold tracking-tight text-slate-950 dark:text-slate-100">
            <span className="grid h-7 w-7 place-items-center rounded-lg bg-blue-50 text-blue-600 ring-1 ring-blue-100 dark:bg-blue-900/30 dark:text-blue-400 dark:ring-blue-800/60">
              <Scale aria-hidden="true" size={15} />
            </span>
            {bi("双口径利润对账与跨境回款核销", "Dual-basis profit reconciliation & cross-border payouts")}
          </h2>
          <p className="mt-1 max-w-4xl text-[12.5px] leading-6 text-slate-600 dark:text-slate-400">
            {bi(
              "订单估算口径（CVP 快照）与平台结算口径（真实扣佣、仓储、广告、退款 + 汇兑损益）逐单对比；金额全部为后端 Decimal 字符串，前端只用整数分与 6 位汇率排版。",
              "The order-estimated CVP basis is compared order by order with the settled marketplace basis — real commission, storage, ad spend, refunds and FX gain/loss. All amounts are backend Decimal strings; the browser only re-formats integer cents and six-decimal rates."
            )}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge tone="info">
            {bi("结算单", "Statements")} · {batchesQuery.data?.total_count ?? 0}
          </Badge>
          <Badge tone={(payoutsQuery.data?.unmatched_count ?? 0) > 0 ? "warning" : "success"}>
            {bi("待匹配回款", "Unmatched payouts")} · {payoutsQuery.data?.unmatched_count ?? 0}
          </Badge>
          <Badge tone="neutral">{baseCurrency}</Badge>
        </div>
      </header>

      <div
        role="tablist"
        aria-label={bi("结算对账分栏", "Settlement reconciliation tabs")}
        className="flex flex-wrap gap-1.5"
      >
        {TABS.map((entry) => (
          <button
            key={entry.key}
            type="button"
            role="tab"
            aria-selected={tab === entry.key}
            data-testid={`settlement-tab-${entry.key}`}
            onClick={() => setTab(entry.key)}
            className={
              "rounded-xl border px-3 py-1.5 text-[12.5px] font-semibold transition-colors " +
              (tab === entry.key
                ? "border-blue-500/40 bg-blue-600 text-white shadow-[0_2px_8px_-2px_rgba(37,99,235,0.55)]"
                : "border-slate-200 bg-white text-slate-700 hover:border-slate-300 hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300")
            }
          >
            {bi(entry.zh, entry.en)}
          </button>
        ))}
      </div>

      {queryError && (
        <p
          role="alert"
          data-testid="settlement-error"
          className="rounded-xl border border-rose-200 bg-rose-50 p-3 text-[12.5px] font-medium text-rose-800 dark:border-rose-800/70 dark:bg-rose-950/40 dark:text-rose-300"
        >
          {queryError.message}
        </p>
      )}

      {feedback && (
        <p
          role={feedback.error ? "alert" : "status"}
          data-testid="settlement-feedback"
          className={
            "rounded-xl border p-3 text-[12.5px] font-medium " +
            (feedback.error
              ? "border-rose-200 bg-rose-50 text-rose-800 dark:border-rose-800/70 dark:bg-rose-950/40 dark:text-rose-300"
              : "border-blue-200 bg-blue-50 text-blue-900 dark:border-blue-800/70 dark:bg-blue-950/40 dark:text-blue-200")
          }
        >
          {feedback.text}
        </p>
      )}

      {tab === "dual-basis" && (
        <div data-testid="settlement-dual-basis" className="space-y-3">
          <div className="grid gap-2.5 sm:grid-cols-2 xl:grid-cols-4">
            <KpiTile
              testId="dual-basis-estimated-net"
              label={bi("估算口径净利", "Estimated net profit")}
              value={formatExactMoney(dual?.estimated_net_margin_base, baseCurrency)}
              hint={bi(
                `估算营收 ${formatExactMoney(dual?.estimated_revenue_base, baseCurrency)}`,
                `Estimated revenue ${formatExactMoney(dual?.estimated_revenue_base, baseCurrency)}`
              )}
              icon={FileSpreadsheet}
            />
            <KpiTile
              testId="dual-basis-settled-payout"
              label={bi("结算口径净回款", "Settled net payout")}
              value={formatExactMoney(dual?.settled_payout_base, baseCurrency)}
              hint={bi(
                `已匹配订单 ${dual?.matched_orders_count ?? 0} · 未匹配行 ${dual?.unmatched_lines_count ?? 0}`,
                `Matched orders ${dual?.matched_orders_count ?? 0} · unmatched rows ${dual?.unmatched_lines_count ?? 0}`
              )}
              icon={Wallet}
            />
            <KpiTile
              testId="dual-basis-settled-net"
              label={bi("结算口径净利", "Settled net profit")}
              value={formatExactMoney(dual?.settled_net_profit_base, baseCurrency)}
              hint={bi("净回款 − COGS + 汇兑损益", "Net payout − COGS + FX variance")}
              icon={Landmark}
            />
            <KpiTile
              testId="dual-basis-variance"
              label={bi("利润总差异", "Total profit variance")}
              value={formatSignedMoney(dual?.total_profit_variance_base, baseCurrency)}
              hint={bi("结算口径 − 估算口径", "Settled basis − estimated basis")}
              icon={ArrowLeftRight}
              highlight
            />
          </div>

          <div className="grid gap-2.5 sm:grid-cols-2 xl:grid-cols-4">
            <KpiTile
              testId="dual-basis-commission-delta"
              label={bi("佣金差异", "Commission delta")}
              value={formatSignedMoney(dual?.commission_delta_base, baseCurrency)}
              hint={bi("平台实扣 − 估算佣金", "Actual − estimated commission")}
              icon={Scale}
            />
            <KpiTile
              testId="dual-basis-fulfillment-delta"
              label={bi("履约+仓储差异", "Fulfillment & storage delta")}
              value={formatSignedMoney(dual?.fulfillment_storage_delta_base, baseCurrency)}
              hint={bi("平台实扣 − 估算运费/关税", "Actual − estimated shipping & duty")}
              icon={Upload}
            />
            <KpiTile
              testId="dual-basis-fx-variance"
              label={bi("汇兑损益", "FX gain / loss")}
              value={formatSignedMoney(dual?.fx_variance_base, baseCurrency)}
              hint={bi("实际结汇 − 锁定期望汇率", "Actual settlement − locked expected rate")}
              icon={ArrowLeftRight}
            />
            <KpiTile
              testId="dual-basis-ad-spend"
              label={bi("广告费（结算口径）", "Ad spend (settled)")}
              value={formatExactMoney(dual?.ad_spend_base, baseCurrency)}
              hint={bi("平台结算单实扣广告费", "Charged on the settlement statement")}
              icon={BadgeCheck}
            />
          </div>

          <Card className="overflow-hidden">
            <CardHeader
              icon={Scale}
              title={bi("逐单双口径对比", "Order-by-order comparison")}
              description={bi(
                "每一行是一个同时拥有 CVP 快照与结算明细的订单；差异列 = 结算口径 − 估算口径。",
                "Each row is an order that has both a CVP snapshot and settlement lines; the delta columns are settled minus estimated."
              )}
              actions={<Badge tone="info">{bi("精确到分", "Exact to the cent")}</Badge>}
            />
            {rows.length === 0 ? (
              <EmptyState className="m-4" testId="dual-basis-empty">
                {bi(
                  "尚无同时具备估算快照与结算明细的订单；导入平台结算单后自动生成对比。",
                  "No order has both an estimated snapshot and settlement lines yet; import a statement to build the comparison."
                )}
              </EmptyState>
            ) : (
              <div className="overflow-x-auto">
                <table className="erp-table">
                  <thead>
                    <tr>
                      <th>{bi("订单号", "Order")}</th>
                      <th className="text-right">{bi("估算营收", "Est. revenue")}</th>
                      <th className="text-right">{bi("结算营收", "Settled revenue")}</th>
                      <th className="text-right">{bi("佣金差异", "Commission Δ")}</th>
                      <th className="text-right">{bi("履约+仓储差异", "Fulfill Δ")}</th>
                      <th className="text-right">{bi("广告差异", "Ad Δ")}</th>
                      <th className="text-right">{bi("估算净利", "Est. net")}</th>
                      <th className="text-right">{bi("结算净利", "Settled net")}</th>
                      <th className="text-right">{bi("利润差异", "Profit Δ")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((row) => (
                      <tr key={row.order_id} data-testid={`dual-basis-row-${row.order_no}`}>
                        <td>
                          <div className="text-[13px] font-bold tracking-tight text-slate-950 dark:text-slate-100">
                            {row.order_no}
                          </div>
                          <div className="mt-0.5 text-[11px] text-slate-500 dark:text-slate-400">
                            {row.currency} · {bi("净回款", "payout")}{" "}
                            {formatExactMoney(row.settled_payout_base, baseCurrency)}
                          </div>
                        </td>
                        <td className="text-right tabular-nums">
                          {formatExactMoney(row.estimated_revenue_base, baseCurrency)}
                        </td>
                        <td className="text-right tabular-nums">
                          {formatExactMoney(row.settled_revenue_base, baseCurrency)}
                        </td>
                        <td className="text-right">
                          <DeltaCell value={row.commission_delta_base} currency={baseCurrency} />
                        </td>
                        <td className="text-right">
                          <DeltaCell
                            value={row.fulfillment_storage_delta_base}
                            currency={baseCurrency}
                          />
                        </td>
                        <td className="text-right">
                          <DeltaCell value={row.ad_delta_base} currency={baseCurrency} />
                        </td>
                        <td className="text-right tabular-nums">
                          {formatExactMoney(row.estimated_net_profit_base, baseCurrency)}
                        </td>
                        <td className="text-right tabular-nums">
                          {formatExactMoney(row.settled_net_profit_base, baseCurrency)}
                        </td>
                        <td className="text-right">
                          <DeltaCell value={row.profit_variance_base} currency={baseCurrency} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </div>
      )}

      {tab === "import" && (
        <div data-testid="settlement-import" className="space-y-3">
          <Card className="overflow-hidden">
            <CardHeader
              icon={Upload}
              title={bi("平台结算单导入（CSV / JSON）", "Settlement statement import (CSV / JSON)")}
              description={bi(
                "粘贴平台导出的 CSV：external_order_id / sku / 销售额 / 佣金 / 履约费 / 仓储费 / 广告费 / 退款。服务端用 Decimal 逐行核算，并按 external_order_id 或订单号自动回挂订单。",
                "Paste the marketplace CSV: external_order_id / sku / gross / commission / fulfillment / storage / ad / refund. The server derives every net amount with Decimal and auto-links each row to its order."
              )}
              actions={
                <Badge tone={canWrite ? "info" : "neutral"}>
                  {canWrite
                    ? bi("OWNER / FINANCE / ADMIN 可导入", "OWNER / FINANCE / ADMIN can import")
                    : bi("只读视图", "Read-only view")}
                </Badge>
              }
            />
            <div className="space-y-3 p-4">
              <div className="grid gap-2.5 sm:grid-cols-2 xl:grid-cols-3">
                <label>
                  <span className={LABEL_CLASS}>{bi("渠道", "Channel")}</span>
                  <input
                    data-testid="settlement-header-channel"
                    className={INPUT_CLASS}
                    value={header.channel_type}
                    onChange={(event) =>
                      setHeader((prev) => ({ ...prev, channel_type: event.target.value }))
                    }
                  />
                </label>
                <label>
                  <span className={LABEL_CLASS}>{bi("结算单号", "Batch ref")}</span>
                  <input
                    data-testid="settlement-header-ref"
                    className={INPUT_CLASS}
                    value={header.batch_ref}
                    onChange={(event) =>
                      setHeader((prev) => ({ ...prev, batch_ref: event.target.value }))
                    }
                  />
                </label>
                <label>
                  <span className={LABEL_CLASS}>{bi("币种", "Currency")}</span>
                  <input
                    data-testid="settlement-header-currency"
                    className={INPUT_CLASS}
                    value={header.currency}
                    onChange={(event) =>
                      setHeader((prev) => ({ ...prev, currency: event.target.value }))
                    }
                  />
                </label>
                <label>
                  <span className={LABEL_CLASS}>{bi("账期开始", "Period start")}</span>
                  <input
                    type="date"
                    data-testid="settlement-header-period-start"
                    className={INPUT_CLASS}
                    value={header.period_start}
                    onChange={(event) =>
                      setHeader((prev) => ({ ...prev, period_start: event.target.value }))
                    }
                  />
                </label>
                <label>
                  <span className={LABEL_CLASS}>{bi("账期结束", "Period end")}</span>
                  <input
                    type="date"
                    data-testid="settlement-header-period-end"
                    className={INPUT_CLASS}
                    value={header.period_end}
                    onChange={(event) =>
                      setHeader((prev) => ({ ...prev, period_end: event.target.value }))
                    }
                  />
                </label>
                <label>
                  <span className={LABEL_CLASS}>{bi("锁定期末汇率 → 本位币", "Locked FX → base")}</span>
                  <input
                    data-testid="settlement-header-fx"
                    inputMode="decimal"
                    className={INPUT_CLASS}
                    value={header.fx_rate_to_base}
                    onChange={(event) =>
                      setHeader((prev) => ({ ...prev, fx_rate_to_base: event.target.value }))
                    }
                  />
                  <span className="mt-1 block text-[10.5px] text-slate-500 dark:text-slate-400">
                    {bi("6 位小数，服务端 ROUND_HALF_UP", "6 decimals, ROUND_HALF_UP server-side")} ·{" "}
                    {formatRateE6(
                      (() => {
                        try {
                          return parseRateToE6(header.fx_rate_to_base);
                        } catch {
                          return parseRateToE6("1.000000");
                        }
                      })()
                    )}
                  </span>
                </label>
              </div>

              <label className="block">
                <span className={LABEL_CLASS}>{bi("结算明细 CSV", "Settlement CSV")}</span>
                <textarea
                  data-testid="settlement-csv-input"
                  aria-label={bi("结算明细 CSV", "Settlement CSV")}
                  value={settlementCsv}
                  onChange={(event) => setSettlementCsv(event.target.value)}
                  rows={7}
                  placeholder={SAMPLE_SETTLEMENT_CSV}
                  className="mt-1 w-full rounded-lg border border-slate-300 bg-white p-2.5 font-mono text-[11.5px] leading-5 text-slate-900 shadow-xs outline-none transition-colors focus:border-blue-500 focus:ring-2 focus:ring-blue-500/15 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-100"
                />
              </label>

              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  data-testid="settlement-sample-fill"
                  onClick={() => {
                    setSettlementCsv(SAMPLE_SETTLEMENT_CSV);
                    setHeader({ ...SAMPLE_SETTLEMENT_HEADER });
                  }}
                  className={SECONDARY_BUTTON}
                >
                  <Wand2 aria-hidden="true" size={14} />
                  {bi("填入示例结算单", "Fill sample statement")}
                </button>
                <button
                  type="button"
                  data-testid="settlement-import-submit"
                  disabled={!canWrite || importSettlement.isPending}
                  onClick={() => importSettlement.mutate()}
                  className={PRIMARY_BUTTON}
                >
                  <Upload aria-hidden="true" size={14} />
                  {importSettlement.isPending
                    ? bi("导入中…", "Importing…")
                    : bi("导入结算单", "Import statement")}
                </button>
                {!canWrite && (
                  <span className="text-[11.5px] text-slate-400 dark:text-slate-500">
                    {bi("需 OWNER / FINANCE / ADMIN", "OWNER / FINANCE / ADMIN only")}
                  </span>
                )}
              </div>
            </div>
          </Card>

          <Card className="overflow-hidden">
            <CardHeader
              icon={FileSpreadsheet}
              title={bi("已导入结算单", "Imported statements")}
              description={bi(
                "点击任意结算单可查看逐单明细与订单自动回挂结果。",
                "Select a statement to inspect its per-order rows and their auto-linked orders."
              )}
            />
            {batches.length === 0 ? (
              <EmptyState className="m-4" testId="settlement-batches-empty">
                {bi("尚无结算单。", "No settlement statement yet.")}
              </EmptyState>
            ) : (
              <div className="overflow-x-auto">
                <table className="erp-table">
                  <thead>
                    <tr>
                      <th>{bi("结算单 / 渠道", "Batch / channel")}</th>
                      <th>{bi("账期", "Period")}</th>
                      <th className="text-right">{bi("销售额", "Gross")}</th>
                      <th className="text-right">{bi("佣金", "Commission")}</th>
                      <th className="text-right">{bi("净回款", "Net payout")}</th>
                      <th className="text-right">{bi("本位币净额", "Net (base)")}</th>
                      <th>{bi("状态", "Status")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {batches.map((batch) => (
                      <tr
                        key={batch.id}
                        data-testid={`settlement-batch-${batch.batch_ref}`}
                        onClick={() => setSelectedBatchId(batch.id)}
                        className="cursor-pointer"
                      >
                        <td>
                          <div className="text-[13px] font-bold tracking-tight text-slate-950 dark:text-slate-100">
                            {batch.batch_ref}
                          </div>
                          <div className="mt-0.5 text-[11px] text-slate-500 dark:text-slate-400">
                            {batch.channel_type} · {batch.line_count} {bi("行", "rows")}
                          </div>
                        </td>
                        <td className="whitespace-nowrap text-[12px] tabular-nums text-slate-700 dark:text-slate-300">
                          {batch.period_start} → {batch.period_end}
                        </td>
                        <td className="text-right tabular-nums">
                          {formatExactMoney(batch.gross_sales_ccy, batch.currency)}
                        </td>
                        <td className="text-right tabular-nums">
                          {formatExactMoney(batch.commission_ccy, batch.currency)}
                        </td>
                        <td className="text-right tabular-nums font-semibold">
                          {formatExactMoney(batch.net_payout_ccy, batch.currency)}
                        </td>
                        <td className="text-right tabular-nums">
                          {formatExactMoney(batch.net_payout_base, baseCurrency)}
                        </td>
                        <td>
                          <Badge tone={batch.status === "reconciled" ? "success" : "info"}>
                            {batch.status === "reconciled"
                              ? bi("已对账", "Reconciled")
                              : bi("已导入", "Imported")}
                          </Badge>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {selectedBatchId && settlementLines.length > 0 && (
              <div className="border-t border-slate-200 p-4 dark:border-slate-800">
                <h3 className="mb-2 text-[12.5px] font-bold text-slate-900 dark:text-slate-100">
                  {bi("结算明细（订单自动回挂）", "Settlement rows (auto-linked orders)")}
                </h3>
                <div className="overflow-x-auto">
                  <table className="erp-table">
                    <thead>
                      <tr>
                        <th>{bi("平台订单号 / SKU", "External order / SKU")}</th>
                        <th>{bi("回挂订单", "Linked order")}</th>
                        <th className="text-right">{bi("销售额", "Gross")}</th>
                        <th className="text-right">{bi("净结算", "Net")}</th>
                        <th className="text-right">{bi("本位币净额", "Net (base)")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {settlementLines.map((line) => (
                        <tr key={line.id} data-testid={`settlement-line-${line.id}`}>
                          <td>
                            <div className="text-[12.5px] font-semibold text-slate-900 dark:text-slate-100">
                              {line.external_order_id ?? "—"}
                            </div>
                            <div className="mt-0.5 text-[11px] text-slate-500 dark:text-slate-400">
                              {line.sku ?? "—"}
                            </div>
                          </td>
                          <td>
                            {line.order_id ? (
                              <Badge tone="success">{bi("已匹配", "Matched")}</Badge>
                            ) : (
                              <Badge tone="warning">{bi("未匹配", "Unmatched")}</Badge>
                            )}
                          </td>
                          <td className="text-right tabular-nums">
                            {formatExactMoney(line.gross_sales_ccy, baseCurrency)}
                          </td>
                          <td className="text-right tabular-nums">
                            {formatExactMoney(line.net_settlement_ccy, baseCurrency)}
                          </td>
                          <td className="text-right tabular-nums">
                            {formatExactMoney(line.net_settlement_base, baseCurrency)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </Card>
        </div>
      )}

      {tab === "payout" && (
        <div data-testid="settlement-payouts" className="space-y-3">
          <Card className="overflow-hidden">
            <CardHeader
              icon={Wallet}
              title={bi(
                "跨境回款对账（PingPong / 连连 / Payoneer / 电汇 / Stripe）",
                "Cross-border payout matching (PingPong / LianLian / Payoneer / T-T / Stripe)"
              )}
              description={bi(
                "导入回款水单后按参考号或「币种 + 最接近净回款金额（±2.00 容差）」自动匹配平台结算单或 B2B 订单，并记录实际结汇汇率与汇兑损益。",
                "After import, each payout is matched by reference or by currency plus the closest net payout (±2.00 tolerance) to a statement or a B2B order, recording the realised FX rate and gain/loss."
              )}
            />
            <div className="space-y-3 p-4">
              <div className="grid gap-2.5 sm:grid-cols-2">
                <label>
                  <span className={LABEL_CLASS}>{bi("回款渠道", "Provider")}</span>
                  <select
                    data-testid="payout-provider"
                    className={INPUT_CLASS}
                    value={provider}
                    onChange={(event) => setProvider(event.target.value as PayoutProvider)}
                  >
                    {PAYOUT_PROVIDERS.map((entry) => (
                      <option key={entry} value={entry}>
                        {PAYOUT_PROVIDER_LABEL[entry]}
                      </option>
                    ))}
                  </select>
                </label>
                <div className="flex items-end gap-2">
                  <button
                    type="button"
                    data-testid="payout-sample-fill"
                    onClick={() => setPayoutCsv(SAMPLE_PAYOUT_CSV)}
                    className={SECONDARY_BUTTON}
                  >
                    <Wand2 aria-hidden="true" size={14} />
                    {bi("填入示例回款", "Fill sample payout")}
                  </button>
                </div>
              </div>

              <label className="block">
                <span className={LABEL_CLASS}>{bi("回款水单 CSV", "Payout statement CSV")}</span>
                <textarea
                  data-testid="payout-csv-input"
                  aria-label={bi("回款水单 CSV", "Payout statement CSV")}
                  value={payoutCsv}
                  onChange={(event) => setPayoutCsv(event.target.value)}
                  rows={5}
                  placeholder={SAMPLE_PAYOUT_CSV}
                  className="mt-1 w-full rounded-lg border border-slate-300 bg-white p-2.5 font-mono text-[11.5px] leading-5 text-slate-900 shadow-xs outline-none transition-colors focus:border-blue-500 focus:ring-2 focus:ring-blue-500/15 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-100"
                />
              </label>

              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  data-testid="payout-import-submit"
                  disabled={!canWrite || importPayouts.isPending}
                  onClick={() => {
                    try {
                      payoutsFromCsv(payoutCsv);
                    } catch (error) {
                      setFeedback({ error: true, text: (error as Error).message });
                      return;
                    }
                    importPayouts.mutate();
                  }}
                  className={PRIMARY_BUTTON}
                >
                  <Upload aria-hidden="true" size={14} />
                  {importPayouts.isPending
                    ? bi("导入中…", "Importing…")
                    : bi("导入回款水单", "Import payouts")}
                </button>
                <button
                  type="button"
                  data-testid="payout-auto-match"
                  disabled={!canWrite || matchPayouts.isPending}
                  onClick={() => matchPayouts.mutate()}
                  className={SECONDARY_BUTTON}
                >
                  <ArrowLeftRight aria-hidden="true" size={14} />
                  {matchPayouts.isPending
                    ? bi("匹配中…", "Matching…")
                    : bi("自动匹配回款", "Auto-match payouts")}
                </button>
                <span
                  data-testid="payout-matched-count"
                  className="text-[11.5px] font-semibold text-slate-600 dark:text-slate-400"
                >
                  {bi("已匹配", "Matched")} {payoutsQuery.data?.matched_count ?? 0} /{" "}
                  {payoutsQuery.data?.total_count ?? 0} ·{" "}
                  {bi("汇兑损益", "FX")}{" "}
                  {formatSignedMoney(payoutsQuery.data?.total_fx_variance_base, baseCurrency)}
                </span>
              </div>
            </div>
          </Card>

          <Card className="overflow-hidden">
            <CardHeader
              icon={BadgeCheck}
              title={bi("回款水单列表", "Payout statements")}
              description={bi(
                "实际结汇汇率 = 结汇本位币金额 ÷ 回款金额，保留 6 位小数；汇兑损益 = 结汇金额 − 回款金额 × 期望汇率。",
                "Actual FX = settled base amount ÷ payout amount at 6 dp; FX variance = settled − payout × expected rate."
              )}
            />
            {payouts.length === 0 ? (
              <EmptyState className="m-4" testId="payouts-empty">
                {bi("尚无回款水单。", "No payout statement yet.")}
              </EmptyState>
            ) : (
              <div className="overflow-x-auto">
                <table className="erp-table">
                  <thead>
                    <tr>
                      <th>{bi("流水号 / 渠道", "Reference / provider")}</th>
                      <th className="text-right">{bi("回款金额", "Payout")}</th>
                      <th className="text-right">{bi("结汇本位币", "Settled (base)")}</th>
                      <th className="text-right">{bi("实际汇率", "Actual FX")}</th>
                      <th className="text-right">{bi("期望汇率", "Expected FX")}</th>
                      <th className="text-right">{bi("汇兑损益", "FX gain/loss")}</th>
                      <th>{bi("匹配状态", "Match")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {payouts.map((payout) => (
                      <tr key={payout.id} data-testid={`payout-row-${payout.external_tx_ref}`}>
                        <td>
                          <div className="text-[12.5px] font-semibold text-slate-900 dark:text-slate-100">
                            {payout.external_tx_ref}
                          </div>
                          <div className="mt-0.5 text-[11px] text-slate-500 dark:text-slate-400">
                            {PAYOUT_PROVIDER_LABEL[payout.provider as PayoutProvider] ??
                              payout.provider}
                          </div>
                        </td>
                        <td className="text-right tabular-nums">
                          {formatExactMoney(payout.payout_amount, payout.payout_currency)}
                        </td>
                        <td className="text-right tabular-nums">
                          {formatExactMoney(payout.settled_base_amount, baseCurrency)}
                        </td>
                        <td className="text-right tabular-nums">{payout.actual_fx_rate}</td>
                        <td className="text-right tabular-nums">{payout.expected_fx_rate}</td>
                        <td className="text-right">
                          <DeltaCell value={payout.fx_variance_base} currency={baseCurrency} />
                        </td>
                        <td>
                          {payout.match_status === "matched" ? (
                            <Badge tone="success">
                              {payout.matched_settlement_batch_id
                                ? bi("已匹配结算单", "Matched statement")
                                : bi("已匹配订单", "Matched order")}
                            </Badge>
                          ) : (
                            <Badge tone="warning">{bi("待匹配", "Unmatched")}</Badge>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </div>
      )}

      {tab === "journal" && (
        <div data-testid="settlement-journal" className="space-y-3">
          <Card className="overflow-hidden">
            <CardHeader
              icon={Download}
              title={bi("会计凭证导出（借贷必平）", "Accounting voucher export (Debits == Credits)")}
              description={bi(
                "针对每张已对账结算单与每笔匹配订单生成复式分录：应收/平台清算、销售收入、COGS、存货、平台佣金、履约与仓储、广告费、汇兑损益、现金/银行。导出前服务端断言借贷合计精确相等。",
                "Every reconciled statement and matched order becomes a double-entry voucher: receivable/marketplace clearing, revenue, COGS, inventory, commission, fulfillment & storage, advertising, FX gain/loss and cash. The server asserts that total debits equal total credits before returning a single byte."
              )}
              actions={<Badge tone="success">{bi("借贷平衡校验", "Balance asserted")}</Badge>}
            />
            <div className="space-y-3 p-4">
              <div
                role="group"
                aria-label={bi("导出格式", "Export format")}
                className="flex flex-wrap gap-1.5"
              >
                {JOURNAL_FORMATS.map((entry) => (
                  <button
                    key={entry}
                    type="button"
                    data-testid={`journal-format-${entry}`}
                    aria-pressed={journalFormat === entry}
                    onClick={() => setJournalFormat(entry)}
                    className={
                      "rounded-xl border px-3 py-1.5 text-[12.5px] font-semibold transition-colors " +
                      (journalFormat === entry
                        ? "border-blue-500/40 bg-blue-600 text-white"
                        : "border-slate-200 bg-white text-slate-700 hover:border-slate-300 hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300")
                    }
                  >
                    {bi(JOURNAL_FORMAT_LABEL[entry].zh, JOURNAL_FORMAT_LABEL[entry].en)}
                  </button>
                ))}
              </div>
              <a
                data-testid="journal-download"
                href={journalExportHref(journalFormat)}
                download={`journal-${journalFormat}.csv`}
                className={PRIMARY_BUTTON}
              >
                <Download aria-hidden="true" size={14} />
                {bi(
                  `下载 ${JOURNAL_FORMAT_LABEL[journalFormat].zh} CSV`,
                  `Download ${JOURNAL_FORMAT_LABEL[journalFormat].en} CSV`
                )}
              </a>
              <p className="text-[11.5px] text-slate-500 dark:text-slate-400">
                {bi(
                  "所有金额为两位小数本位币，汇率保留 6 位；任何非数字文本若以 = + - @ 开头会被加单引号，避免 CSV 公式注入。",
                  "Amounts are two-decimal base currency and rates keep six decimals; any non-numeric cell starting with = + - @ is prefixed with an apostrophe to block CSV formula injection."
                )}
              </p>
            </div>
          </Card>
        </div>
      )}
    </section>
  );
}
