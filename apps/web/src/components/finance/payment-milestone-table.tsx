"use client";

import React, { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  CheckCircle2,
  Landmark,
  Receipt,
  TrendingDown,
  TrendingUp,
  Upload,
} from "lucide-react";
import { submitReceivablePayment, verifyPayment } from "@/lib/api-client/live-client";
import { canVerifyFinanceSettlement, isPrivilegedCostViewer } from "@/lib/auth-cookie";
import { formatMoney, type UserRole } from "@/lib/contracts";
import { useI18n } from "@/lib/i18n";
import { Badge, Card, CardHeader, EmptyState } from "@/components/ui/page";
import { CvpWaterfallBar } from "@/components/ui/erp-charts";

export interface ReceivableMilestoneItem {
  id: string;
  order_id: string;
  order_no: string;
  order_mode: string;
  milestone_label: string;
  due_amount: string;
  paid_amount: string;
  currency: string;
  status: "open" | "partial" | "settled" | "overdue" | "void";
  shipment_blocked_70_unpaid?: boolean;
  pending_payment_id?: string | null;
  pending_tx_ref?: string | null;
}

export interface OrderProfitSnapshotItem {
  order_id: string;
  order_no: string;
  base_currency: string;
  gross_revenue_base: string;
  cogs_base: string;
  platform_commission_base: string;
  payment_gateway_fee_base: string;
  shipping_and_duty_base: string;
  channel_msg_cost_base: string;
  ad_attribution_cost_base: string;
  fx_gain_loss_base: string;
  net_contribution_margin_base: string;
}

const STATUS_TONE: Record<
  ReceivableMilestoneItem["status"],
  "neutral" | "success" | "warning" | "danger" | "info"
> = {
  open: "info",
  partial: "warning",
  settled: "success",
  overdue: "danger",
  void: "neutral",
};

/** Semantic dot colour per milestone state — 21st.dev status pill rhythm. */
const STATUS_DOT: Record<ReceivableMilestoneItem["status"], string> = {
  open: "bg-blue-500",
  partial: "bg-amber-500",
  settled: "bg-emerald-500",
  overdue: "bg-rose-500",
  void: "bg-slate-400",
};

export function PaymentMilestoneTable({
  receivables,
  cvpSnapshots,
  role,
}: {
  receivables: ReceivableMilestoneItem[];
  cvpSnapshots: OrderProfitSnapshotItem[];
  role: UserRole | undefined;
}) {
  const queryClient = useQueryClient();
  const { bi } = useI18n();
  const canVerify = canVerifyFinanceSettlement(role);
  const showCost = isPrivilegedCostViewer(role);
  const [txRefs, setTxRefs] = useState<Record<string, string>>({});
  const [fxRates, setFxRates] = useState<Record<string, string>>({});
  const [slips, setSlips] = useState<Record<string, File | null>>({});

  const uploadPaymentMutation = useMutation({
    mutationFn: async (rp: ReceivableMilestoneItem) => {
      const form = new FormData();
      form.append("payment_method", "tt_wire");
      form.append("external_tx_ref", txRefs[rp.id] || `TT-${rp.order_no}`);
      form.append("amount_received", rp.due_amount);
      form.append("currency", rp.currency);
      form.append("actual_fx_rate", fxRates[rp.id] || "7.12");
      form.append("gateway_fee", "35.00");
      if (slips[rp.id]) {
        form.append("receipt_file", slips[rp.id]!);
      }
      return submitReceivablePayment(rp.id, form);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["finance", "overview"] });
    },
  });

  const verifyMutation = useMutation({
    mutationFn: (paymentId: string) => verifyPayment(paymentId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["finance", "overview"] });
      void queryClient.invalidateQueries({ queryKey: ["orders"] });
    },
  });

  const statusLabel: Record<ReceivableMilestoneItem["status"], string> = {
    open: bi("待收款", "Open"),
    partial: bi("部分收款", "Partial"),
    settled: bi("已核销", "Settled"),
    overdue: bi("已逾期", "Overdue"),
    void: bi("已作废", "Void"),
  };

  const settledCount = receivables.filter((rp) => rp.status === "settled").length;
  const blockedCount = receivables.filter((rp) => rp.shipment_blocked_70_unpaid).length;

  return (
    <div className="space-y-4">
      <Card className="overflow-hidden">
        <CardHeader
          icon={Receipt}
          title={bi(
            "B2B 定金 / 尾款里程碑与 L0 资金复核",
            "B2B deposit / balance milestones & L0 cash verification"
          )}
          description={bi(
            "水单凭证与汇兑流水必须由财务或所有者逐笔复核；尾款未结清前，订单出库被系统强制阻断。",
            "Wire receipts and FX flows must be verified line by line by Finance/Owner; dispatch stays blocked until the balance is settled."
          )}
          actions={
            <div className="flex flex-wrap items-center gap-1.5">
              <Badge tone="info">
                {bi("里程碑", "Milestones")} · {receivables.length}
              </Badge>
              <Badge tone="success">
                {bi("已核销", "Settled")} · {settledCount}
              </Badge>
              {blockedCount > 0 && (
                <Badge tone="warning">
                  {bi("出库阻断", "Dispatch blocked")} · {blockedCount}
                </Badge>
              )}
            </div>
          }
        />
        <div className="p-0">
          {receivables.length === 0 ? (
            <EmptyState className="m-4">
              {bi("暂无待处理的收款里程碑。", "No receivable milestones at the moment.")}
            </EmptyState>
          ) : (
            <div className="overflow-x-auto">
              <table className="erp-table">
                <thead>
                  <tr>
                    <th>{bi("订单 / 里程碑", "Order / milestone")}</th>
                    <th>{bi("应收 / 已收", "Due / paid")}</th>
                    <th>{bi("状态与风控", "Status & controls")}</th>
                    <th>{bi("上传水单", "Upload T/T slip")}</th>
                    <th className="text-right">{bi("L0 复核", "L0 verification")}</th>
                  </tr>
                </thead>
                <tbody>
                  {receivables.map((rp) => (
                    <tr key={rp.id}>
                      <td>
                        <div className="text-[13px] font-bold tracking-tight text-slate-950">
                          {rp.order_no}
                        </div>
                        <span className="mt-1 inline-flex items-center gap-1 rounded-md border border-slate-200 bg-slate-50 px-1.5 py-0.5 font-mono text-[10.5px] font-medium text-slate-600">
                          {rp.milestone_label}
                        </span>
                      </td>
                      <td className="tabular-nums">
                        <div className="text-[13px] font-semibold text-slate-900">
                          {bi("应收", "Due")} {formatMoney(rp.due_amount, rp.currency)}
                        </div>
                        <div className="mt-0.5 text-[11.5px] text-slate-500">
                          {bi("已收", "Paid")} {formatMoney(rp.paid_amount, rp.currency)}
                        </div>
                      </td>
                      <td>
                        <span
                          className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[11px] font-semibold ${
                            STATUS_TONE[rp.status] === "success"
                              ? "border-emerald-200/80 bg-emerald-50 text-emerald-800"
                              : STATUS_TONE[rp.status] === "warning"
                                ? "border-amber-200/80 bg-amber-50 text-amber-900"
                                : STATUS_TONE[rp.status] === "danger"
                                  ? "border-rose-200/80 bg-rose-50 text-rose-800"
                                  : STATUS_TONE[rp.status] === "info"
                                    ? "border-blue-200/80 bg-blue-50 text-blue-800"
                                    : "border-slate-200/80 bg-slate-100 text-slate-700"
                          }`}
                        >
                          <span
                            className={`h-1.5 w-1.5 rounded-full ${STATUS_DOT[rp.status]}`}
                            aria-hidden="true"
                          />
                          {statusLabel[rp.status]}
                        </span>
                        {rp.shipment_blocked_70_unpaid && (
                          <div
                            data-testid="balance-70-unpaid-warning"
                            className="mt-1.5 inline-flex items-center gap-1 rounded-md border border-amber-300 bg-amber-50 px-1.5 py-0.5 text-[10px] font-bold text-amber-900"
                          >
                            <AlertTriangle className="h-3 w-3" />
                            <span>70% Balance Unpaid — Shipment Blocked</span>
                          </div>
                        )}
                      </td>
                      <td>
                        {rp.status !== "settled" && (
                          <div className="flex flex-wrap items-center gap-1.5">
                            <input
                              type="text"
                              value={txRefs[rp.id] ?? ""}
                              onChange={(e) =>
                                setTxRefs((prev) => ({ ...prev, [rp.id]: e.target.value }))
                              }
                              placeholder={bi("水单号", "T/T ref")}
                              aria-label={bi("水单号", "T/T reference")}
                              className="h-8 w-24 rounded-lg border border-slate-300 bg-white px-2 text-[12px] text-slate-800 shadow-xs outline-none transition-colors placeholder:text-slate-400 focus:border-blue-500 focus:ring-2 focus:ring-blue-500/15"
                            />
                            <input
                              type="text"
                              value={fxRates[rp.id] ?? "7.12"}
                              onChange={(e) =>
                                setFxRates((prev) => ({ ...prev, [rp.id]: e.target.value }))
                              }
                              placeholder={bi("汇率", "FX")}
                              aria-label={bi("实际汇率", "Actual FX rate")}
                              className="h-8 w-16 rounded-lg border border-slate-300 bg-white px-2 text-[12px] tabular-nums text-slate-800 shadow-xs outline-none transition-colors placeholder:text-slate-400 focus:border-blue-500 focus:ring-2 focus:ring-blue-500/15"
                            />
                            <label className="inline-flex h-8 cursor-pointer items-center gap-1 rounded-lg border border-slate-200 bg-white px-2 text-[12px] font-medium text-slate-600 shadow-xs transition-colors hover:border-slate-300 hover:bg-slate-50">
                              <Upload className="h-3 w-3" />
                              <span>{bi("选择水单", "Choose slip")}</span>
                              <input
                                type="file"
                                aria-label={bi("水单附件", "T/T slip attachment")}
                                onChange={(e) =>
                                  setSlips((prev) => ({
                                    ...prev,
                                    [rp.id]: e.target.files?.[0] ?? null,
                                  }))
                                }
                                className="hidden"
                              />
                            </label>
                            <button
                              type="button"
                              onClick={() => uploadPaymentMutation.mutate(rp)}
                              className="inline-flex h-8 items-center gap-1 rounded-lg bg-slate-900 px-2.5 text-[12px] font-semibold text-white shadow-xs transition-colors hover:bg-slate-800"
                            >
                              <Upload className="h-3 w-3" />
                              <span>{bi("登记收款", "Record")}</span>
                            </button>
                          </div>
                        )}
                      </td>
                      <td className="text-right">
                        {rp.pending_payment_id && (
                          <button
                            type="button"
                            data-testid={`verify-payment-btn-${rp.pending_payment_id}`}
                            disabled={!canVerify || verifyMutation.isPending}
                            onClick={() => verifyMutation.mutate(rp.pending_payment_id!)}
                            className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-emerald-600 px-2.5 text-[12px] font-semibold text-white shadow-xs transition-colors hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-40"
                          >
                            <CheckCircle2 className="h-3.5 w-3.5" />
                            <span>Verify ({rp.pending_tx_ref ?? "T/T"})</span>
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </Card>

      <Card className="overflow-hidden">
        <CardHeader
          icon={Landmark}
          title={bi("CVP 订单贡献毛利瀑布", "CVP order contribution margin waterfall")}
          description={bi(
            "从营收逐项扣减 COGS、平台佣金、支付通道费、头程与 DDP 关税、消息与广告 CPA，再加回汇兑损益。",
            "Revenue minus COGS, platform commission, gateway fees, freight and DDP duty, message and ad CPA, plus FX gain/loss."
          )}
          actions={
            <Badge tone={showCost ? "success" : "warning"}>
              {showCost
                ? bi("含成本明细", "Cost details visible")
                : bi("成本已脱敏", "Costs masked")}
            </Badge>
          }
        />
        <div
          className={`grid gap-3 p-4 ${cvpSnapshots.length > 2 ? "md:grid-cols-2 2xl:grid-cols-3" : ""}`}
        >
          {cvpSnapshots.map((snap) => {
            const rev = Number(snap.gross_revenue_base);
            const cogs = Number(snap.cogs_base);
            const net = Number(snap.net_contribution_margin_base);
            const pct = rev > 0 ? ((net / rev) * 100).toFixed(2) : "0.00";
            // Everything that is not COGS and not net margin: commission, gateway,
            // freight/duty, message + ad CPA, net of FX gain/loss. Derived so the
            // stacked bar always adds up to exactly 100% of the real revenue.
            const feesAndLogistics = rev - cogs - net;
            return (
              <div
                key={snap.order_id}
                data-testid={`cvp-waterfall-${snap.order_id}`}
                className="space-y-3 rounded-2xl border border-slate-200/90 bg-white p-4 shadow-[0_1px_3px_rgba(15,23,42,0.05)] transition-shadow hover:shadow-[0_4px_14px_rgba(15,23,42,0.07)]"
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="truncate text-[13px] font-bold tracking-tight text-slate-950">
                    {snap.order_no}
                  </span>
                  <span
                    className={`inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-[11.5px] font-semibold tabular-nums ${
                      net >= 0
                        ? "border-emerald-200/80 bg-emerald-50 text-emerald-800"
                        : "border-rose-200/80 bg-rose-50 text-rose-800"
                    }`}
                  >
                    {net >= 0 ? (
                      <TrendingUp className="h-3 w-3" aria-hidden="true" />
                    ) : (
                      <TrendingDown className="h-3 w-3" aria-hidden="true" />
                    )}
                    Net Margin:{" "}
                    {formatMoney(snap.net_contribution_margin_base, snap.base_currency)} ({pct}%)
                  </span>
                </div>

                <div className="rounded-xl border border-slate-100 bg-slate-50/70 p-3">
                  <CvpWaterfallBar
                    grossRevenue={rev}
                    cogs={cogs}
                    feesAndLogistics={feesAndLogistics}
                    netMargin={net}
                  />
                </div>

                <div className="grid gap-x-4 gap-y-1.5 sm:grid-cols-2">
                  <Row
                    label={bi("+ 营业收入", "+ Gross revenue")}
                    value={formatMoney(snap.gross_revenue_base, snap.base_currency)}
                    strong
                  />
                  <Row
                    label={bi("- 商品成本 (COGS)", "- COGS")}
                    value={`-${formatMoney(snap.cogs_base, snap.base_currency)}`}
                    negative
                  />
                  <Row
                    label={bi("- 平台佣金", "- Platform commission")}
                    value={`-${formatMoney(snap.platform_commission_base, snap.base_currency)}`}
                    negative
                  />
                  <Row
                    label={bi("- 支付通道费", "- Payment gateway fee")}
                    value={`-${formatMoney(snap.payment_gateway_fee_base, snap.base_currency)}`}
                    negative
                  />
                  <Row
                    label={bi("- 头程运费与 DDP 关税", "- Shipping & DDP duty")}
                    value={`-${formatMoney(snap.shipping_and_duty_base, snap.base_currency)}`}
                    negative
                  />
                  <Row
                    label={bi("- 渠道消息与广告 CPA", "- Channel msg & ad CPA")}
                    value={`-${formatMoney(
                      Number(snap.channel_msg_cost_base) + Number(snap.ad_attribution_cost_base),
                      snap.base_currency
                    )}`}
                    negative
                  />
                  <Row
                    label={bi("+/- 汇兑损益", "+/- FX gain/loss")}
                    value={formatMoney(snap.fx_gain_loss_base, snap.base_currency)}
                    info
                  />
                  <Row
                    label={bi("= 净贡献毛利", "= Net contribution margin")}
                    value={formatMoney(snap.net_contribution_margin_base, snap.base_currency)}
                    strong
                  />
                </div>
              </div>
            );
          })}
          {cvpSnapshots.length === 0 && (
            <EmptyState className="md:col-span-2 2xl:col-span-3">
              {bi("暂无订单毛利快照。", "No order margin snapshots yet.")}
            </EmptyState>
          )}
        </div>
      </Card>
    </div>
  );
}

function Row({
  label,
  value,
  negative = false,
  info = false,
  strong = false,
}: {
  label: string;
  value: string;
  negative?: boolean;
  info?: boolean;
  strong?: boolean;
}) {
  return (
    <div className="flex items-baseline justify-between gap-2 border-b border-dashed border-slate-100 py-1 last:border-0">
      <span className="text-[11.5px] font-medium text-slate-500">{label}</span>
      <span
        className={`text-[12px] tabular-nums ${
          negative
            ? "text-rose-700"
            : info
              ? "text-blue-700"
              : strong
                ? "font-bold text-slate-950"
                : "text-slate-800"
        }`}
      >
        {value}
      </span>
    </div>
  );
}
