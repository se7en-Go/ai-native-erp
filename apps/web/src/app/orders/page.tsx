"use client";

import React, { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowRightLeft, Lock, Truck } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { ShipmentTimelineDrawer } from "@/components/orders/shipment-timeline-drawer";
import { Badge, EmptyState, Page, PageHeader, TableWrap } from "@/components/ui/page";
import { shipOrder } from "@/lib/api-client/live-client";
import { fetchOrders, fetchPendingProposalsCount } from "@/lib/data-source";
import { useI18n } from "@/lib/i18n";
import { formatMoney, type OrderMode } from "@/lib/contracts";

const MODE_LABEL: Record<OrderMode, { zh: string; en: string }> = {
  b2c_retail: { zh: "B2C 零售", en: "B2C retail" },
  b2b_sample: { zh: "B2B 样品", en: "B2B sample" },
  b2b_wholesale: { zh: "B2B 批发", en: "B2B wholesale" },
};

const FULFILLMENT_LABEL: Record<string, { zh: string; en: string }> = {
  pending_payment: { zh: "待付款", en: "Pending payment" },
  confirmed_reserved: { zh: "已确认预留", en: "Confirmed & reserved" },
  in_production: { zh: "生产中", en: "In production" },
  partially_shipped: { zh: "部分出运", en: "Partially shipped" },
  shipped: { zh: "已出运", en: "Shipped" },
  delivered: { zh: "已签收", en: "Delivered" },
  exception: { zh: "物流异常", en: "Exception" },
  cancelled: { zh: "已取消", en: "Cancelled" },
  refunded: { zh: "已退款", en: "Refunded" },
};

export default function OrdersPage() {
  const queryClient = useQueryClient();
  const { bi } = useI18n();
  const [modeFilter, setModeFilter] = useState<"" | OrderMode>("");
  const [drawerOrderId, setDrawerOrderId] = useState<string | null>(null);
  const [trackingInputs, setTrackingInputs] = useState<Record<string, string>>({});

  const pendingProposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () => fetchPendingProposalsCount(),
  });

  const ordersQuery = useQuery({
    queryKey: ["orders", modeFilter],
    queryFn: () => fetchOrders(modeFilter),
  });

  const shipMutation = useMutation({
    mutationFn: ({ orderId, trackingNumber }: { orderId: string; trackingNumber: string }) =>
      shipOrder(orderId, {
        trackingNumber,
        carrierCode: "DHL",
        shipmentMode: "parcel",
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["orders"] });
    },
  });

  const orders = ordersQuery.data?.items ?? [];

  return (
    <AppShell
      currentPath="/orders"
      pendingProposalsCount={pendingProposalsQuery.data?.total ?? 0}
    >
      <Page>
        <PageHeader
          title={bi("全渠道订单与 17TRACK 物流追踪", "Omnichannel orders & 17TRACK logistics")}
          description={bi(
            "B2C 零售与 B2B 批发共用同一本库存台账；批发出库前强制校验 70% 尾款是否结清，异常物流按 72 小时停滞规则预警。",
            "B2C retail and B2B wholesale share one inventory ledger. Wholesale dispatch requires the 70% balance settled and logistics exceptions raise after 72 hours of stagnation."
          )}
          actions={
            <select
              value={modeFilter}
              aria-label={bi("按订单模式筛选", "Filter by order mode")}
              onChange={(e) => setModeFilter(e.target.value as "" | OrderMode)}
              className="erp-input w-auto"
            >
              <option value="">{bi("全部订单模式", "All order modes")}</option>
              <option value="b2c_retail">{bi("B2C 零售", "B2C retail")}</option>
              <option value="b2b_sample">{bi("B2B 样品", "B2B sample")}</option>
              <option value="b2b_wholesale">{bi("B2B 批发", "B2B wholesale")}</option>
            </select>
          }
        />

        {shipMutation.isError && (
          <p
            role="alert"
            className="rounded-xl border border-rose-200 bg-rose-50 p-2.5 text-[12px] text-rose-800"
          >
            {shipMutation.error.message}
          </p>
        )}

        {orders.length === 0 ? (
          <EmptyState>
            {ordersQuery.isLoading
              ? bi("正在加载订单…", "Loading orders…")
              : bi("暂无订单。", "No orders yet.")}
          </EmptyState>
        ) : (
          <TableWrap>
            <table className="erp-table">
              <thead>
                <tr>
                  <th>{bi("订单号", "Order no.")}</th>
                  <th>{bi("模式与买家", "Mode & buyer")}</th>
                  <th>{bi("履约状态", "Fulfillment")}</th>
                  <th>{bi("收款状态", "Payment")}</th>
                  <th className="text-right">{bi("订单金额", "Total")}</th>
                  <th className="text-right">{bi("17TRACK 与出库", "17TRACK & dispatch")}</th>
                </tr>
              </thead>
              <tbody>
                {orders.map((ord) => {
                  const blocked70Unpaid =
                    ord.order_mode === "b2b_wholesale" && ord.payment_status !== "paid";
                  const modeCopy = MODE_LABEL[ord.order_mode] ?? {
                    zh: ord.order_mode,
                    en: ord.order_mode,
                  };
                  const fulfillmentCopy = FULFILLMENT_LABEL[ord.fulfillment_status] ?? {
                    zh: ord.fulfillment_status,
                    en: ord.fulfillment_status,
                  };
                  return (
                    <tr key={ord.id}>
                      <td>
                        <div className="font-medium text-slate-900">{ord.order_no}</div>
                        {ord.has_72h_stagnation_alert && (
                          <span
                            data-testid={`order-72h-alert-${ord.id}`}
                            className="mt-1 inline-flex items-center gap-1 rounded border border-rose-200 bg-rose-50 px-1.5 py-0.5 text-[10px] font-medium text-rose-700"
                          >
                            <AlertTriangle className="h-3 w-3" />
                            <span>{bi("72 小时物流停滞", "72h stagnation")}</span>
                          </span>
                        )}
                      </td>
                      <td>
                        <div className="text-slate-800">{ord.customer_name}</div>
                        <Badge className="mt-0.5">{bi(modeCopy.zh, modeCopy.en)}</Badge>
                      </td>
                      <td>
                        <Badge tone={ord.fulfillment_status === "exception" ? "danger" : "info"}>
                          {bi(fulfillmentCopy.zh, fulfillmentCopy.en)}
                        </Badge>
                      </td>
                      <td>
                        <div className="text-slate-700">{ord.payment_status}</div>
                        {blocked70Unpaid && (
                          <span
                            data-testid={`order-ship-blocked-${ord.id}`}
                            className="mt-1 inline-flex items-center gap-1 rounded border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-[10px] font-medium text-amber-900"
                          >
                            <Lock className="h-3 w-3" />
                            <span>{bi("70% 尾款未结清", "70% balance unpaid")}</span>
                          </span>
                        )}
                      </td>
                      <td className="text-right font-medium tabular-nums text-slate-800">
                        {formatMoney(ord.total_amount, ord.currency)}
                      </td>
                      <td>
                        <div className="flex flex-wrap items-center justify-end gap-2">
                          <button
                            type="button"
                            data-testid={`open-shipment-drawer-${ord.id}`}
                            onClick={() => setDrawerOrderId(ord.id)}
                            className="erp-btn erp-btn-secondary py-1 text-[12px]"
                          >
                            <Truck className="h-3 w-3" />
                            <span>{bi("物流时间轴与台账", "Timeline & ledger")}</span>
                          </button>

                          <input
                            type="text"
                            value={trackingInputs[ord.id] ?? ""}
                            onChange={(e) =>
                              setTrackingInputs((prev) => ({ ...prev, [ord.id]: e.target.value }))
                            }
                            placeholder={bi("运单号", "Tracking #")}
                            aria-label={bi("运单号", "Tracking number")}
                            disabled={blocked70Unpaid}
                            className="erp-input w-28 py-1 text-[12px] disabled:bg-slate-100"
                          />
                          <button
                            type="button"
                            disabled={blocked70Unpaid || !(trackingInputs[ord.id] ?? "").trim()}
                            onClick={() =>
                              shipMutation.mutate({
                                orderId: ord.id,
                                trackingNumber: trackingInputs[ord.id] ?? "",
                              })
                            }
                            className="erp-btn bg-slate-900 text-white hover:bg-slate-800 disabled:opacity-40"
                          >
                            <ArrowRightLeft className="h-3 w-3" />
                            <span>{bi("出库", "Ship")}</span>
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </TableWrap>
        )}
      </Page>

      {drawerOrderId && (
        <ShipmentTimelineDrawer
          orderId={drawerOrderId}
          onClose={() => setDrawerOrderId(null)}
        />
      )}
    </AppShell>
  );
}
