"use client";

import React, { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Banknote,
  Download,
  Landmark,
  Link2,
  Percent,
  Receipt,
  TrendingUp,
  Wallet,
  X,
} from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { useAuthSession } from "@/components/providers";
import { PaymentMilestoneTable } from "@/components/finance/payment-milestone-table";
import { BentoStatCard } from "@/components/ui/bento-stat-card";
import { Badge, Card, CardHeader, EmptyState, Page, PageHeader } from "@/components/ui/page";
import { canVerifyFinanceSettlement } from "@/lib/auth-cookie";
import { fetchFinanceOverview, fetchPendingProposalsCount } from "@/lib/data-source";
import {
  compareDecimal,
  fetchCashflowSummary,
  fetchPayables,
  formatCents,
  parseMoneyToCents,
  payableOutstanding,
  paySupplierPayable,
  toPayablePaymentPayload,
} from "@/lib/api-client/pack-g-ap-social";
import type { PayableRead } from "@/lib/api-client/types.gen";
import { useI18n } from "@/lib/i18n";
import { formatMoney, type UserRole } from "@/lib/contracts";

/** AP status pill: `open -> partial -> paid`, exactly the server state machine. */
const AP_STATUS: Record<string, { zh: string; en: string; pill: string; dot: string }> = {
  open: {
    zh: "待付",
    en: "Open",
    pill: "border-blue-200/80 bg-blue-50 text-blue-800 dark:border-blue-800/70 dark:bg-blue-950/40 dark:text-blue-300",
    dot: "bg-blue-500",
  },
  partial: {
    zh: "部分付款",
    en: "Partial",
    pill: "border-amber-200/80 bg-amber-50 text-amber-900 dark:border-amber-800/70 dark:bg-amber-950/40 dark:text-amber-300",
    dot: "bg-amber-500",
  },
  paid: {
    zh: "已结清",
    en: "Settled",
    pill: "border-emerald-200/80 bg-emerald-50 text-emerald-800 dark:border-emerald-800/70 dark:bg-emerald-950/40 dark:text-emerald-300",
    dot: "bg-emerald-500",
  },
};

const AP_STATUS_FALLBACK = {
  pill: "border-slate-200/80 bg-slate-100 text-slate-700 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300",
  dot: "bg-slate-400",
};

/** Compact cash-flow tile: label, exact decimal value, one-line provenance hint. */
function CashflowTile({
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
        "min-w-0 rounded-xl border p-3.5 transition-colors " +
        (highlight
          ? "border-blue-500/40 bg-gradient-to-br from-blue-600 via-blue-600 to-indigo-600 text-white shadow-md shadow-blue-600/20"
          : "border-slate-200 bg-white shadow-[0_1px_2px_rgba(15,23,42,0.04)] hover:border-slate-300 dark:border-slate-800 dark:bg-slate-900 dark:hover:border-slate-700")
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
          "mt-2.5 truncate text-[19px] font-bold tabular-nums leading-tight tracking-tight " +
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

/**
 * 应付账款 (AP) + 现金流闭环。
 *
 * 采购订单在服务端被自动登记为 `open` 应付单，所以这张表就是真实的
 * PO -> AP 同步结果；只有 OWNER / FINANCE（`canVerifyFinanceSettlement`）能
 * 登记付款，服务端还会再拦一次超额与重复结算。
 */
function ApCashflowBoard({ role }: { role: UserRole | undefined }) {
  const { bi } = useI18n();
  const queryClient = useQueryClient();
  const canPay = canVerifyFinanceSettlement(role);

  const payablesQuery = useQuery({
    queryKey: ["finance", "payables"],
    queryFn: () => fetchPayables(),
  });
  const cashflowQuery = useQuery({
    queryKey: ["finance", "cashflow-summary"],
    queryFn: () => fetchCashflowSummary(),
  });

  const payables = payablesQuery.data?.items ?? [];
  const cashflow = cashflowQuery.data;
  const currency = cashflow?.currency ?? payablesQuery.data?.currency ?? "CNY";

  const [payingId, setPayingId] = useState<string | null>(null);
  const [amount, setAmount] = useState("");
  const [paymentRef, setPaymentRef] = useState("");
  const [feedback, setFeedback] = useState<{ error: boolean; text: string } | null>(null);

  const pay = useMutation({
    mutationFn: (input: { payableId: string; amount: string; paymentRef: string }) =>
      paySupplierPayable(
        input.payableId,
        toPayablePaymentPayload(input.amount, input.paymentRef)
      ),
    onSuccess: (updated) => {
      setPayingId(null);
      setAmount("");
      setPaymentRef("");
      setFeedback({
        error: false,
        text: bi(
          `${updated.po_no} 已登记付款，应付余额与现金流已刷新。`,
          `Payment recorded for ${updated.po_no}; the AP balance and cash flow are refreshed.`
        ),
      });
      void queryClient.invalidateQueries({ queryKey: ["finance", "payables"] });
      void queryClient.invalidateQueries({ queryKey: ["finance", "cashflow-summary"] });
      void queryClient.invalidateQueries({ queryKey: ["finance", "overview"] });
    },
    onError: (error: Error) => {
      setFeedback({ error: true, text: error.message });
    },
  });

  const openPaymentForm = (row: PayableRead) => {
    setFeedback(null);
    if (payingId === row.id) {
      setPayingId(null);
      return;
    }
    setPayingId(row.id);
    setAmount(payableOutstanding(row));
    setPaymentRef("");
  };

  const submitPayment = (row: PayableRead) => {
    const cents = parseMoneyToCents(amount);
    if (cents === null || cents <= 0n) {
      setFeedback({
        error: true,
        text: bi("请输入大于 0 的付款金额（最多两位小数）。", "Enter an amount above zero with at most two decimals."),
      });
      return;
    }
    const outstanding = payableOutstanding(row);
    if (compareDecimal(formatCents(cents), outstanding) > 0) {
      setFeedback({
        error: true,
        text: bi(
          `付款金额不能超过待付余额 ${formatMoney(outstanding, row.currency)}。`,
          `The payment cannot exceed the outstanding balance ${formatMoney(outstanding, row.currency)}.`
        ),
      });
      return;
    }
    setFeedback(null);
    pay.mutate({ payableId: row.id, amount, paymentRef });
  };

  const pendingCount = payables.filter((row) => row.status !== "paid").length;
  const queryError = payablesQuery.error ?? cashflowQuery.error;

  return (
    <section data-testid="finance-ap-section" className="space-y-3.5">
      <header className="flex flex-wrap items-end justify-between gap-2">
        <div className="min-w-0">
          <h2 className="flex items-center gap-2 text-[15px] font-bold tracking-tight text-slate-950 dark:text-slate-100">
            <span className="grid h-7 w-7 place-items-center rounded-lg bg-blue-50 text-blue-600 ring-1 ring-blue-100 dark:bg-blue-900/30 dark:text-blue-400 dark:ring-blue-800/60">
              <Banknote aria-hidden="true" size={15} />
            </span>
            {bi("应付账款 (AP) 与现金流收支闭环", "Supplier payables (AP) & cash-flow loop")}
          </h2>
          <p className="mt-1 max-w-4xl text-[12.5px] leading-6 text-slate-600 dark:text-slate-400">
            {bi(
              "采购订单一落地即自动生成应付单；付款只登记真实银行水单，状态严格 open → partial → paid。金额全部为后端 Decimal 字符串，前端不做浮点加减。",
              "Every purchase order auto-books a payable; settlement only records a real bank transfer and the status moves strictly open → partial → paid. All amounts are backend Decimal strings — no client-side float arithmetic."
            )}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge tone="info">
            {bi("应付单", "Payables")} · {payables.length}
          </Badge>
          <Badge tone={pendingCount ? "warning" : "success"}>
            {bi("待付", "Outstanding")} · {pendingCount}
          </Badge>
          <Badge tone="neutral">{currency}</Badge>
        </div>
      </header>

      <div className="grid gap-2.5 sm:grid-cols-2 xl:grid-cols-4">
        <CashflowTile
          testId="cashflow-ar-collected"
          label={bi("应收已收 AR", "AR collected")}
          value={formatMoney(cashflow?.ar_collected, currency)}
          hint={
            cashflow?.ar_outstanding
              ? bi(
                  `未收 ${formatMoney(cashflow.ar_outstanding, currency)}`,
                  `Outstanding ${formatMoney(cashflow.ar_outstanding, currency)}`
                )
              : bi("等待财务对账", "Awaiting reconciliation")
          }
          icon={Landmark}
        />
        <CashflowTile
          testId="cashflow-ap-paid"
          label={bi("应付已付 AP", "AP paid")}
          value={formatMoney(cashflow?.ap_paid, currency)}
          hint={
            cashflow?.ap_total
              ? bi(
                  `应付总额 ${formatMoney(cashflow.ap_total, currency)}`,
                  `Total AP ${formatMoney(cashflow.ap_total, currency)}`
                )
              : bi("暂无已付供应商款", "No supplier payment yet")
          }
          icon={Receipt}
        />
        <CashflowTile
          testId="cashflow-ap-outstanding"
          label={bi("待付供应商 AP", "AP outstanding")}
          value={formatMoney(cashflow?.ap_outstanding, currency)}
          hint={bi("超额与重复付款在服务端被拒", "Over- and re-payment rejected server-side")}
          icon={Link2}
        />
        <CashflowTile
          testId="cashflow-net-cashflow"
          label={bi("净现金流", "Net cash flow")}
          value={formatMoney(cashflow?.net_cashflow, currency)}
          hint={bi("应收已收 − 应付已付", "AR collected − AP paid")}
          icon={Wallet}
          highlight
        />
      </div>

      {queryError && (
        <p
          role="alert"
          data-testid="finance-ap-error"
          className="rounded-xl border border-rose-200 bg-rose-50 p-3 text-[12.5px] font-medium text-rose-800 dark:border-rose-800/70 dark:bg-rose-950/40 dark:text-rose-300"
        >
          {queryError.message}
        </p>
      )}

      {feedback && (
        <p
          role={feedback.error ? "alert" : "status"}
          data-testid="finance-ap-feedback"
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

      <Card className="overflow-hidden">
        <CardHeader
          icon={Receipt}
          title={bi("采购应付单（自动同步自采购订单）", "Supplier payables (synced from purchase orders)")}
          description={bi(
            "到期日、供应商与金额都来自采购订单本身；登记付款必须填写银行水单号，便于日后与银行流水逐笔核对。",
            "Due date, supplier and amount all come from the purchase order itself; a bank reference is required so every settlement can be reconciled against the statement."
          )}
          actions={
            <Badge tone={canPay ? "info" : "neutral"}>
              {canPay
                ? bi("OWNER / FINANCE 可登记付款", "OWNER / FINANCE can settle")
                : bi("只读视图", "Read-only view")}
            </Badge>
          }
        />
        {payables.length === 0 ? (
          <EmptyState className="m-4" testId="finance-ap-empty">
            {bi(
              "当前没有应付单；采购订单确认后会自动登记。",
              "No payables yet; they are booked as soon as a purchase order is confirmed."
            )}
          </EmptyState>
        ) : (
          <div className="overflow-x-auto">
            <table className="erp-table">
              <thead>
                <tr>
                  <th>{bi("采购单号 / 供应商", "PO / supplier")}</th>
                  <th>{bi("应付 / 已付", "Due / paid")}</th>
                  <th>{bi("到期日", "Due date")}</th>
                  <th>{bi("状态", "Status")}</th>
                  <th className="text-right">{bi("付款", "Settlement")}</th>
                </tr>
              </thead>
              <tbody>
                {payables.map((row) => {
                  const status = AP_STATUS[row.status] ?? null;
                  const outstanding = payableOutstanding(row);
                  const settled = row.status === "paid";
                  return (
                    <React.Fragment key={row.id}>
                      <tr data-testid={`ap-row-${row.po_no}`}>
                        <td>
                          <div className="text-[13px] font-bold tracking-tight text-slate-950 dark:text-slate-100">
                            {row.po_no}
                          </div>
                          <div className="mt-0.5 truncate text-[11.5px] text-slate-600 dark:text-slate-400">
                            {row.supplier_name}
                          </div>
                          {row.payment_ref && (
                            <span className="mt-1 inline-flex items-center gap-1 rounded-md border border-slate-200 bg-slate-50 px-1.5 py-0.5 font-mono text-[10.5px] font-medium text-slate-600 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300">
                              {row.payment_ref}
                            </span>
                          )}
                        </td>
                        <td className="tabular-nums">
                          <div className="text-[13px] font-semibold text-slate-900 dark:text-slate-100">
                            {formatMoney(row.due_amount, row.currency)}
                          </div>
                          <div className="mt-0.5 text-[11.5px] text-slate-500 dark:text-slate-400">
                            {bi("已付", "Paid")} {formatMoney(row.paid_amount, row.currency)} ·{" "}
                            {bi("待付", "Open")} {formatMoney(outstanding, row.currency)}
                          </div>
                        </td>
                        <td className="whitespace-nowrap text-[12.5px] tabular-nums text-slate-700 dark:text-slate-300">
                          {row.due_date ?? bi("未约定", "Not agreed")}
                        </td>
                        <td>
                          <span
                            data-testid={`ap-status-${row.po_no}`}
                            className={
                              "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[11px] font-semibold " +
                              (status ? status.pill : AP_STATUS_FALLBACK.pill)
                            }
                          >
                            <span
                              aria-hidden="true"
                              className={
                                "h-1.5 w-1.5 rounded-full " +
                                (status ? status.dot : AP_STATUS_FALLBACK.dot)
                              }
                            />
                            {status ? bi(status.zh, status.en) : row.status}
                          </span>
                        </td>
                        <td className="text-right">
                          {canPay ? (
                            <button
                              type="button"
                              data-testid="pay-ap-btn"
                              data-payable-id={row.id}
                              data-po-no={row.po_no}
                              disabled={settled || pay.isPending}
                              aria-expanded={payingId === row.id}
                              onClick={() => openPaymentForm(row)}
                              className={
                                "inline-flex items-center gap-1.5 rounded-xl px-3 py-1.5 text-[12.5px] font-semibold transition-colors " +
                                (settled
                                  ? "cursor-not-allowed border border-slate-200 bg-slate-50 text-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-500"
                                  : "bg-blue-600 text-white shadow-[0_2px_8px_-2px_rgba(37,99,235,0.55)] hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-700")
                              }
                            >
                              <Banknote aria-hidden="true" size={14} />
                              {settled ? bi("已结清", "Settled") : bi("登记付款", "Record payment")}
                            </button>
                          ) : (
                            <span className="text-[11.5px] text-slate-400 dark:text-slate-500">
                              {bi("需 OWNER / FINANCE", "OWNER / FINANCE only")}
                            </span>
                          )}
                        </td>
                      </tr>
                      {payingId === row.id && (
                        <tr data-testid={`ap-pay-form-${row.po_no}`}>
                          <td colSpan={5} className="bg-slate-50/80 dark:bg-slate-800/50">
                            <div className="flex flex-wrap items-end gap-2.5">
                              <label className="min-w-0">
                                <span className="block text-[11px] font-semibold text-slate-600 dark:text-slate-400">
                                  {bi("付款金额", "Amount")} · {row.currency}
                                </span>
                                <input
                                  type="text"
                                  inputMode="decimal"
                                  data-testid="pay-ap-amount-input"
                                  aria-label={bi("付款金额", "Payment amount")}
                                  value={amount}
                                  onChange={(event) => setAmount(event.target.value)}
                                  placeholder={outstanding}
                                  className="mt-1 h-9 w-36 rounded-lg border border-slate-300 bg-white px-2.5 text-[13px] tabular-nums text-slate-900 shadow-xs outline-none transition-colors placeholder:text-slate-400 focus:border-blue-500 focus:ring-2 focus:ring-blue-500/15 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-100"
                                />
                              </label>
                              <label className="min-w-0 flex-1">
                                <span className="block text-[11px] font-semibold text-slate-600 dark:text-slate-400">
                                  {bi("银行水单号", "Bank reference")}
                                </span>
                                <input
                                  type="text"
                                  data-testid="pay-ap-ref-input"
                                  aria-label={bi("银行水单号", "Bank reference")}
                                  value={paymentRef}
                                  onChange={(event) => setPaymentRef(event.target.value)}
                                  placeholder="BANK-20261120-0123"
                                  className="mt-1 h-9 w-full rounded-lg border border-slate-300 bg-white px-2.5 font-mono text-[12.5px] text-slate-900 shadow-xs outline-none transition-colors placeholder:text-slate-400 focus:border-blue-500 focus:ring-2 focus:ring-blue-500/15 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-100"
                                />
                              </label>
                              <div className="flex items-center gap-2">
                                <button
                                  type="button"
                                  data-testid="pay-ap-submit"
                                  disabled={pay.isPending}
                                  onClick={() => submitPayment(row)}
                                  className="inline-flex items-center gap-1.5 rounded-xl bg-blue-600 px-3.5 py-2 text-[12.5px] font-semibold text-white shadow-[0_2px_8px_-2px_rgba(37,99,235,0.55)] transition-colors hover:bg-blue-700 disabled:opacity-50"
                                >
                                  <Receipt aria-hidden="true" size={14} />
                                  {pay.isPending
                                    ? bi("提交中…", "Submitting…")
                                    : bi("提交付款", "Submit")}
                                </button>
                                <button
                                  type="button"
                                  data-testid="pay-ap-cancel"
                                  onClick={() => setPayingId(null)}
                                  className="inline-flex items-center gap-1.5 rounded-xl border border-slate-200 bg-white px-3 py-2 text-[12.5px] font-semibold text-slate-700 transition-colors hover:border-slate-300 hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300 dark:hover:bg-slate-800"
                                >
                                  <X aria-hidden="true" size={14} />
                                  {bi("取消", "Cancel")}
                                </button>
                              </div>
                              <p className="w-full text-[11px] text-slate-500 dark:text-slate-400">
                                {bi(
                                  `待付余额 ${formatMoney(outstanding, row.currency)}；超过余额的付款会在前端与服务端双重拦截。`,
                                  `Outstanding ${formatMoney(outstanding, row.currency)}; an overpayment is rejected both in the UI and on the server.`
                                )}
                              </p>
                            </div>
                          </td>
                        </tr>
                      )}
                    </React.Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </section>
  );
}

export default function FinancePage() {
  const { principal } = useAuthSession();
  const { bi } = useI18n();

  const pendingProposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () => fetchPendingProposalsCount(),
  });

  const financeQuery = useQuery({
    queryKey: ["finance", "overview"],
    enabled: Boolean(principal && principal.role !== "agent"),
    queryFn: () => fetchFinanceOverview(),
  });

  const summary = financeQuery.data?.summary;
  const pendingMilestones =
    financeQuery.data?.receivables.filter((row) => row.pending_payment_id).length ?? 0;
  const settledMilestones =
    financeQuery.data?.receivables.filter((row) => row.status === "settled").length ?? 0;

  return (
    <AppShell
      currentPath="/finance"
      pendingProposalsCount={pendingProposalsQuery.data?.total ?? 0}
    >
      <Page>
        <PageHeader
          title={bi("财务收款与 CVP 边际毛利", "Finance settlements & CVP contribution margin")}
          description={bi(
            "B2B 定金 / 尾款里程碑、L0 水单人工复核，以及按订单拆解的净贡献毛利瀑布。金额与汇率全部使用 Decimal 与 NUMERIC，不使用浮点数。",
            "B2B deposit and balance milestones, L0 manual wire verification, and per-order net contribution margin waterfalls. Money and FX always use Decimal/NUMERIC, never floats."
          )}
          actions={
            <a
              href="/api/bff/v1/finance/cvp/export.csv"
              className="inline-flex items-center gap-2 rounded-xl border border-slate-200 bg-white px-3.5 py-2 text-[13px] font-semibold text-slate-800 shadow-xs transition-colors hover:border-slate-300 hover:bg-slate-50"
            >
              <Download className="h-4 w-4 text-slate-500" />
              <span>{bi("导出对账 CSV", "Export reconciliation CSV")}</span>
            </a>
          }
        />

        {summary && (
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <BentoStatCard
              label={bi("总营收（本位币）", "Total gross revenue")}
              value={formatMoney(summary.total_gross_revenue_base, summary.base_currency)}
              icon={Landmark}
              accent="blue"
              badge={summary.base_currency}
              hint={bi("含 B2B 与 B2C 全部已确认订单", "All confirmed B2B and B2C orders")}
              testId="finance-kpi-revenue"
            />
            <BentoStatCard
              variant="hero"
              label={bi("净贡献毛利", "Net contribution margin")}
              value={formatMoney(summary.total_net_margin_base, summary.base_currency)}
              icon={TrendingUp}
              accent="emerald"
              badge={`${summary.avg_margin_pct}%`}
              hint={bi(
                "已扣 COGS、佣金、物流与广告归因",
                "After COGS, fees, freight and ad attribution"
              )}
              testId="finance-kpi-net-margin"
            />
            <BentoStatCard
              label={bi("平均毛利率", "Average margin")}
              value={`${summary.avg_margin_pct}%`}
              icon={Percent}
              accent="violet"
              badge={bi("订单加权", "Order-weighted")}
              badgeTone="violet"
              hint={bi("保留两位小数，四舍五入", "Two decimals, ROUND_HALF_UP")}
              testId="finance-kpi-avg-margin"
            />
            <BentoStatCard
              label={bi("待核销里程碑", "Milestones pending verification")}
              value={String(pendingMilestones)}
              icon={Receipt}
              accent="amber"
              badge={bi(`已核销 ${settledMilestones}`, `Settled ${settledMilestones}`)}
              badgeTone="slate"
              hint={bi("需财务或所有者执行 L0 复核", "Requires Finance/Owner L0 review")}
              testId="finance-kpi-pending-milestones"
            />
          </div>
        )}

        {financeQuery.isError && (
          <div
            role="alert"
            className="rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-[12.5px] font-medium text-rose-800"
          >
            {financeQuery.error.message}
          </div>
        )}

        <PaymentMilestoneTable
          receivables={financeQuery.data?.receivables ?? []}
          cvpSnapshots={financeQuery.data?.cvp_snapshots ?? []}
          baseCurrency={summary?.base_currency}
          role={principal?.role}
        />

        <ApCashflowBoard role={principal?.role} />
      </Page>
    </AppShell>
  );
}
