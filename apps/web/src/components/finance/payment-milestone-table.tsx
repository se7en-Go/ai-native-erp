"use client";

import React, { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, Upload } from "lucide-react";
import { submitReceivablePayment, verifyPayment } from "@/lib/api-client/live-client";
import { canVerifyFinanceSettlement } from "@/lib/auth-cookie";
import { formatMoney, type UserRole } from "@/lib/contracts";

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
  const canVerify = canVerifyFinanceSettlement(role);
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

  return (
    <div className="space-y-6">
      <div className="rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="text-sm font-bold text-slate-900">
          B2B Deposit / Balance Milestones & L0 Cash Verification
        </h2>
        <div className="mt-3 overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="border-b border-slate-200 bg-slate-50 font-semibold text-slate-600">
              <tr>
                <th className="px-3 py-2">Order / Milestone</th>
                <th className="px-3 py-2">Due / Paid</th>
                <th className="px-3 py-2">Status & Guards</th>
                <th className="px-3 py-2">Upload T/T Slip</th>
                <th className="px-3 py-2">L0 Verify</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {receivables.map((rp) => (
                <tr key={rp.id}>
                  <td className="px-3 py-2.5">
                    <div className="font-semibold text-slate-900">{rp.order_no}</div>
                    <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px]">
                      {rp.milestone_label}
                    </span>
                  </td>
                  <td className="px-3 py-2.5">
                    <div>Due: {formatMoney(rp.due_amount, rp.currency)}</div>
                    <div className="text-slate-500">
                      Paid: {formatMoney(rp.paid_amount, rp.currency)}
                    </div>
                  </td>
                  <td className="px-3 py-2.5">
                    <span className="rounded bg-slate-100 px-1.5 py-0.5 font-medium">
                      {rp.status}
                    </span>
                    {rp.shipment_blocked_70_unpaid && (
                      <div
                        data-testid="balance-70-unpaid-warning"
                        className="mt-1 inline-flex items-center gap-1 rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-semibold text-amber-900"
                      >
                        <AlertTriangle className="h-3 w-3" />
                        <span>70% Balance Unpaid — Shipment Blocked</span>
                      </div>
                    )}
                  </td>
                  <td className="px-3 py-2.5">
                    {rp.status !== "settled" && (
                      <div className="flex flex-wrap items-center gap-1.5">
                        <input
                          type="text"
                          value={txRefs[rp.id] ?? ""}
                          onChange={(e) =>
                            setTxRefs((prev) => ({ ...prev, [rp.id]: e.target.value }))
                          }
                          placeholder="T/T Ref"
                          className="w-24 rounded border border-slate-300 px-1.5 py-1"
                        />
                        <input
                          type="text"
                          value={fxRates[rp.id] ?? "7.12"}
                          onChange={(e) =>
                            setFxRates((prev) => ({ ...prev, [rp.id]: e.target.value }))
                          }
                          placeholder="FX"
                          className="w-16 rounded border border-slate-300 px-1.5 py-1"
                        />
                        <input
                          type="file"
                          onChange={(e) =>
                            setSlips((prev) => ({
                              ...prev,
                              [rp.id]: e.target.files?.[0] ?? null,
                            }))
                          }
                          className="w-32 text-[10px]"
                        />
                        <button
                          type="button"
                          onClick={() => uploadPaymentMutation.mutate(rp)}
                          className="inline-flex items-center gap-1 rounded border border-slate-300 px-2 py-1 font-medium hover:bg-slate-100"
                        >
                          <Upload className="h-3 w-3" />
                          <span>Record</span>
                        </button>
                      </div>
                    )}
                  </td>
                  <td className="px-3 py-2.5">
                    {rp.pending_payment_id && (
                      <button
                        type="button"
                        data-testid={`verify-payment-btn-${rp.pending_payment_id}`}
                        disabled={!canVerify || verifyMutation.isPending}
                        onClick={() => verifyMutation.mutate(rp.pending_payment_id!)}
                        className="inline-flex items-center gap-1 rounded bg-emerald-600 px-2.5 py-1 font-semibold text-white hover:bg-emerald-700 disabled:opacity-40"
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
      </div>

      <div className="rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="text-sm font-bold text-slate-900">
          CVP Order Contribution Margin Waterfall
        </h2>
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          {cvpSnapshots.map((snap) => {
            const rev = Number(snap.gross_revenue_base);
            const net = Number(snap.net_contribution_margin_base);
            const pct = rev > 0 ? ((net / rev) * 100).toFixed(2) : "0.00";
            return (
              <div
                key={snap.order_id}
                data-testid={`cvp-waterfall-${snap.order_id}`}
                className="rounded-md border border-slate-200 bg-slate-50 p-3 text-xs space-y-1"
              >
                <div className="flex items-center justify-between border-b border-slate-200 pb-1.5 font-bold text-slate-900">
                  <span>{snap.order_no}</span>
                  <span className="text-emerald-700">
                    Net Margin: {formatMoney(snap.net_contribution_margin_base, snap.base_currency)} ({pct}%)
                  </span>
                </div>
                <div className="flex justify-between">
                  <span>+ Gross Revenue</span>
                  <span>{formatMoney(snap.gross_revenue_base, snap.base_currency)}</span>
                </div>
                <div className="flex justify-between text-rose-700">
                  <span>- COGS</span>
                  <span>-{formatMoney(snap.cogs_base, snap.base_currency)}</span>
                </div>
                <div className="flex justify-between text-rose-700">
                  <span>- Platform Commission</span>
                  <span>-{formatMoney(snap.platform_commission_base, snap.base_currency)}</span>
                </div>
                <div className="flex justify-between text-rose-700">
                  <span>- Payment Gateway Fee</span>
                  <span>-{formatMoney(snap.payment_gateway_fee_base, snap.base_currency)}</span>
                </div>
                <div className="flex justify-between text-rose-700">
                  <span>- Shipping & DDP Duty</span>
                  <span>-{formatMoney(snap.shipping_and_duty_base, snap.base_currency)}</span>
                </div>
                <div className="flex justify-between text-rose-700">
                  <span>- Channel Msg & Ad CPA</span>
                  <span>
                    -{formatMoney(
                      Number(snap.channel_msg_cost_base) + Number(snap.ad_attribution_cost_base),
                      snap.base_currency
                    )}
                  </span>
                </div>
                <div className="flex justify-between text-indigo-700">
                  <span>+/- FX Gain/Loss</span>
                  <span>{formatMoney(snap.fx_gain_loss_base, snap.base_currency)}</span>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
