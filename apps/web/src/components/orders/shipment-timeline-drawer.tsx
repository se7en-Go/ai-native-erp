"use client";

import React, { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Database, RefreshCw, Truck, X } from "lucide-react";
import { refreshShipmentTracking } from "@/lib/api-client/live-client";
import {
  fetchInventoryBalances,
  fetchInventoryLedger,
  fetchOrderDetail,
} from "@/lib/data-source";
import { useI18n } from "@/lib/i18n";
import { Badge } from "@/components/ui/page";

export function ShipmentTimelineDrawer({
  orderId,
  onClose,
}: {
  orderId: string;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const { bi } = useI18n();
  const [ledgerModalOpen, setLedgerModalOpen] = useState(false);

  const detailQuery = useQuery({
    queryKey: ["orders", orderId],
    queryFn: () => fetchOrderDetail(orderId),
  });

  const balancesQuery = useQuery({
    queryKey: ["orders", "inventory", "balances"],
    enabled: ledgerModalOpen,
    queryFn: () => fetchInventoryBalances(),
  });

  const ledgerQuery = useQuery({
    queryKey: ["orders", "inventory", "ledger"],
    enabled: ledgerModalOpen,
    queryFn: () => fetchInventoryLedger(),
  });

  const refreshTrackingMutation = useMutation({
    mutationFn: (shipmentId: string) => refreshShipmentTracking(orderId, shipmentId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["orders", orderId] });
    },
  });

  const order = detailQuery.data;

  return (
    <div
      data-testid="shipment-timeline-drawer"
      className="fixed inset-y-0 right-0 z-50 flex w-full max-w-xl flex-col border-l border-slate-200 bg-white shadow-2xl"
    >
      <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
        <div>
          <h3 className="text-sm font-semibold text-slate-900">
            {bi("17TRACK 物流时间轴与库存台账", "17TRACK timeline & inventory ledger")}
          </h3>
          {order && <p className="text-[11px] text-slate-500">{order.order_no}</p>}
        </div>
        <button
          type="button"
          onClick={onClose}
          className="rounded-md p-1.5 text-slate-500 hover:bg-slate-100"
          aria-label={bi("关闭物流抽屉", "Close shipment drawer")}
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="flex-1 space-y-4 overflow-y-auto p-4 text-[13px]">
        <div className="flex justify-end">
          <button
            type="button"
            data-testid="open-inventory-ledger-btn"
            onClick={() => setLedgerModalOpen(true)}
            className="erp-btn erp-btn-secondary"
          >
            <Database className="h-3.5 w-3.5" />
            <span>{bi("原子库存台账", "Atomic inventory ledger")}</span>
          </button>
        </div>

        {(order?.shipments ?? []).length === 0 ? (
          <div className="rounded-xl border border-dashed border-slate-200 p-6 text-center text-[12px] text-slate-400">
            {bi("该订单尚未出运。", "No shipments dispatched yet.")}
          </div>
        ) : (
          order!.shipments.map((ship) => (
            <div key={ship.id} className="space-y-2.5 rounded-xl border border-slate-200 p-3.5">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2 font-semibold text-slate-900">
                  <Truck className="h-4 w-4 text-blue-600" />
                  <span className="font-mono text-[12px]">{ship.tracking_number}</span>
                  {ship.carrier_code && <Badge>{ship.carrier_code}</Badge>}
                </div>

                <button
                  type="button"
                  onClick={() => refreshTrackingMutation.mutate(ship.id)}
                  className="erp-btn erp-btn-secondary py-1 text-[11px]"
                  title={bi(
                    "标准 1 credit 刷新，cacheLevel=0 强制回源",
                    "Standard 1-credit refresh with cacheLevel=0"
                  )}
                >
                  <RefreshCw className="h-3 w-3" />
                  <span>{bi("刷新物流（cacheLevel=0）", "Refresh (cacheLevel=0)")}</span>
                </button>
              </div>

              <div className="flex flex-wrap items-center gap-1.5">
                <Badge tone="info">{ship.standardized_status}</Badge>
                {ship.sub_status && <Badge>{ship.sub_status}</Badge>}
                {ship.is_stagnant_72h && (
                  <Badge tone="danger">
                    <AlertTriangle className="h-3 w-3" />
                    <span>{bi("72 小时停滞 / 异常预警", "72h stagnant / exception alert")}</span>
                  </Badge>
                )}
              </div>

              <div className="space-y-2 border-l-2 border-slate-200 pl-3">
                {ship.tracking_events.map((ev, idx) => (
                  <div key={idx} className="space-y-0.5">
                    <div className="font-medium text-slate-800">{ev.description}</div>
                    <div className="text-[11px] tabular-nums text-slate-500">
                      {ev.location ? `${ev.location} · ` : ""}
                      {new Date(ev.time_utc).toLocaleString()}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          ))
        )}
      </div>

      {ledgerModalOpen && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label={bi("库存台账", "Inventory ledger")}
          className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/40 p-4"
        >
          <div className="w-full max-w-2xl space-y-3 rounded-xl bg-white p-4 text-[12px] shadow-2xl">
            <div className="flex items-center justify-between border-b border-slate-200 pb-2">
              <h4 className="text-sm font-semibold text-slate-900">
                {bi("原子库存余额与不可变台账", "Atomic inventory balances & immutable ledger")}
              </h4>
              <button
                type="button"
                onClick={() => setLedgerModalOpen(false)}
                className="erp-btn erp-btn-secondary py-1"
              >
                {bi("关闭", "Close")}
              </button>
            </div>

            <div className="grid gap-2 sm:grid-cols-2">
              {(balancesQuery.data?.items ?? []).map((bal) => (
                <div
                  key={`${bal.warehouse_id}-${bal.variant_id}`}
                  className="rounded-lg border border-slate-200 bg-slate-50/70 p-2.5"
                >
                  <div className="font-medium text-slate-900">{bal.sku}</div>
                  <div className="mt-0.5 tabular-nums text-slate-600">
                    {bi("现存", "On hand")} <strong>{bal.on_hand_qty}</strong> ·{" "}
                    {bi("预占", "Reserved")} <strong>{bal.reserved_qty}</strong> ·{" "}
                    {bi("可用", "Available")}{" "}
                    <strong>{bal.on_hand_qty - bal.reserved_qty}</strong>
                  </div>
                </div>
              ))}
            </div>

            <div className="max-h-64 overflow-y-auto rounded-lg border border-slate-200">
              <table className="erp-table">
                <thead>
                  <tr>
                    <th>{bi("事件", "Event")}</th>
                    <th className="text-right">{bi("现存变动", "Δ on hand")}</th>
                    <th className="text-right">{bi("预占变动", "Δ reserved")}</th>
                    <th>{bi("幂等键", "Idempotency key")}</th>
                  </tr>
                </thead>
                <tbody>
                  {(ledgerQuery.data?.items ?? []).map((row) => (
                    <tr key={row.id}>
                      <td className="font-medium">{row.event_type}</td>
                      <td className="text-right tabular-nums">{row.qty_delta_on_hand}</td>
                      <td className="text-right tabular-nums">{row.qty_delta_reserved}</td>
                      <td className="font-mono text-[11px]">{row.idempotency_key}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
