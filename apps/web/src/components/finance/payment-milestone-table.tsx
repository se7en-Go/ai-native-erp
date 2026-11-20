"use client";

import React, { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, Landmark, Receipt, Upload } from "lucide-react";
import { submitReceivablePayment, verifyPayment } from "@/lib/api-client/live-client";
import { canVerifyFinanceSettlement, isPrivilegedCostViewer } from "@/lib/auth-cookie";
import { formatMoney, type UserRole } from "@/lib/contracts";
import { useI18n } from "@/lib/i18n";
import { Badge, Card, CardHeader, EmptyState } from "@/components/ui/page";

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

const STATUS_TONE: Record<ReceivableMilestoneItem["status"], "neutral" | "success" | "warning" | "danger" | "info"> = {
  open: "info",
  partial: "warning",
  settled: "success",
  overdue: "danger",
  void: "neutral",
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

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader
          icon={Receipt}
          title={bi("B2B 定金 / 尾款里程碑与 L0 资金复核", "B2B deposit / balance milestones & L0 cash verification")}
          description={bi(
            "水单凭证与汇兑流水必须由财务或所有者逐笔复核；尾款未结清前，订单出库被系统强制阻断。",
            "Wire receipts and FX flows must be verified line by line by Finance/Owner; dispatch stays blocked until the balance is settled."
          )}
          actions={
            <Badge tone="info">
              {bi("里程碑", "Milestones")} · {receivables.length}
            </Badge>
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
                        <div className="font-medium text-slate-900">{rp.order_no}</div>
                        <span className="mt-0.5 inline-block rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] text-slate-600">
                          {rp.milestone_label}
                        </span>
                      </td>
                      <td className="tabular-nums">
                        <div className="text-slate-800">
                          {bi("应收", "Due")} {formatMoney(rp.due_amount, rp.currency)}
                        </div>
                        <div className="text-[12px] text-slate-500">
                          {bi("已收", "Paid")} {formatMoney(rp.paid_amount, rp.currency)}
                        </div>
                      </td>
                      <td>
                        <Badge tone={STATUS_TONE[rp.status]}>{statusLabel[rp.status]}</Badge>
                        {rp.shipment_blocked_70_unpaid && (
                          <div
                            data-testid="balance-70-unpaid-warning"
                            className="mt-1 inline-flex items-center gap-1 rounded border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-[10px] font-semibold text-amber-900"
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
                              className="erp-input w-24 py-1 text-[12px]"
                            />
                            <input
                              type="text"
                              value={fxRates[rp.id] ?? "7.12"}
                              onChange={(e) =>
                                setFxRates((prev) => ({ ...prev, [rp.id]: e.target.value }))
                              }
                              placeholder={bi("汇率", "FX")}
                              aria-label={bi("实际汇率", "Actual FX rate")}
                              className="erp-input w-16 py-1 text-[12px] tabular-nums"
                            />
                            <label className="inline-flex cursor-pointer items-center gap-1 rounded-lg border border-slate-300 bg-white px-2 py-1 text-[12px] text-slate-600 hover:bg-slate-50">
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
                              className="erp-btn erp-btn-secondary py-1 text-[12px]"
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
                            className="erp-btn bg-emerald-600 py-1 text-[12px] text-white hover:bg-emerald-700 disabled:opacity-40"
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

      <Card>
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
          className={`grid gap-3 p-4 ${
            cvpSnapshots.length > 2 ? "md:grid-cols-2 2xl:grid-cols-3" : ""
          }`}
        >
          {cvpSnapshots.map((snap) => {
            const rev = Number(snap.gross_revenue_base);
            const net = Number(snap.net_contribution_margin_base);
            const pct = rev > 0 ? ((net / rev) * 100).toFixed(2) : "0.00";
            return (
              <div
                key={snap.order_id}
                data-testid={`cvp-waterfall-${snap.order_id}`}
                className="space-y-1.5 rounded-xl border border-slate-200 bg-slate-50/60 p-3.5 text-[12px]"
              >
                <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 pb-2">
                  <span className="font-semibold text-slate-900">{snap.order_no}</span>
                  <span className="font-semibold tabular-nums text-emerald-700">
                    Net Margin: {formatMoney(snap.net_contribution_margin_base, snap.base_currency)}{" "}
                    ({pct}%)
                  </span>
                </div>
                <Row
                  label={bi("+ 营业收入", "+ Gross revenue")}
                  value={formatMoney(snap.gross_revenue_base, snap.base_currency)}
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
}: {
  label: string;
  value: string;
  negative?: boolean;
  info?: boolean;
}) {
  return (
    <div className="flex items-center justify-between gap-2">
      <span className="text-slate-500">{label}</span>
      <span
        className={`tabular-nums ${
          negative ? "text-rose-700" : info ? "text-blue-700" : "text-slate-800"
        }`}
      >
        {value}
      </span>
    </div>
  );
}
