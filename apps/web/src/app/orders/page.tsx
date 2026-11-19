"use client";

import React, { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Lock, Truck } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { ShipmentTimelineDrawer } from "@/components/orders/shipment-timeline-drawer";
import { shipOrder, requireDefaultWarehouseId } from "@/lib/api-client/live-client";
import { fetchOrders, fetchPendingProposalsCount, shouldUseMockApi } from "@/lib/data-source";
import { formatMoney, type OrderMode } from "@/lib/contracts";

export default function OrdersPage() {
  const queryClient = useQueryClient();
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
        // Mock mode: the BFF mock ignores the body, so any placeholder
        // passes; live mode requires a real warehouse UUID (see
        // live-client.requireDefaultWarehouseId).
        warehouseId: shouldUseMockApi() ? "wh-sz" : requireDefaultWarehouseId(),
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
      <div className="mx-auto max-w-6xl space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="text-lg font-bold text-slate-900">
              Unified Dual-Mode Orders, Inventory Ledger & 17TRACK
            </h1>
            <p className="text-xs text-slate-500">
              B2C retail and B2B wholesale share one atomic inventory ledger; wholesale dispatch enforces 70% balance settlement.
            </p>
          </div>
          <select
            value={modeFilter}
            onChange={(e) => setModeFilter(e.target.value as "" | OrderMode)}
            className="rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-xs"
          >
            <option value="">All Order Modes</option>
            <option value="b2c_retail">b2c_retail</option>
            <option value="b2b_sample">b2b_sample</option>
            <option value="b2b_wholesale">b2b_wholesale</option>
          </select>
        </div>

        <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
          <table className="w-full text-left text-xs">
            <thead className="border-b border-slate-200 bg-slate-50 font-semibold text-slate-600">
              <tr>
                <th className="px-3 py-2.5">Order No</th>
                <th className="px-3 py-2.5">Mode & Buyer</th>
                <th className="px-3 py-2.5">Fulfillment</th>
                <th className="px-3 py-2.5">Payment</th>
                <th className="px-3 py-2.5">Total</th>
                <th className="px-3 py-2.5">17TRACK & Dispatch</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {orders.map((ord) => {
                const blocked70Unpaid =
                  ord.order_mode === "b2b_wholesale" && ord.payment_status !== "paid";
                return (
                  <tr key={ord.id} className="hover:bg-slate-50">
                    <td className="px-3 py-2.5 font-semibold text-slate-900">
                      <div>{ord.order_no}</div>
                      {ord.has_72h_stagnation_alert && (
                        <span
                          data-testid={`order-72h-alert-${ord.id}`}
                          className="mt-1 inline-flex items-center gap-1 rounded bg-rose-100 px-1.5 py-0.5 text-[10px] font-semibold text-rose-800"
                        >
                          <AlertTriangle className="h-3 w-3" />
                          <span>72h Stagnation</span>
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-2.5">
                      <div>{ord.customer_name}</div>
                      <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px]">
                        {ord.order_mode}
                      </span>
                    </td>
                    <td className="px-3 py-2.5">{ord.fulfillment_status}</td>
                    <td className="px-3 py-2.5">
                      <div>{ord.payment_status}</div>
                      {blocked70Unpaid && (
                        <span
                          data-testid={`order-ship-blocked-${ord.id}`}
                          className="mt-1 inline-flex items-center gap-1 rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-semibold text-amber-900"
                        >
                          <Lock className="h-3 w-3" />
                          <span>70% Balance Unpaid</span>
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-2.5 font-semibold">
                      {formatMoney(ord.total_amount, ord.currency)}
                    </td>
                    <td className="px-3 py-2.5">
                      <div className="flex flex-wrap items-center gap-2">
                        <button
                          type="button"
                          data-testid={`open-shipment-drawer-${ord.id}`}
                          onClick={() => setDrawerOrderId(ord.id)}
                          className="inline-flex items-center gap-1 rounded border border-indigo-300 bg-indigo-50 px-2 py-1 font-semibold text-indigo-700"
                        >
                          <Truck className="h-3 w-3" />
                          <span>Timeline & Ledger</span>
                        </button>

                        <input
                          type="text"
                          value={trackingInputs[ord.id] ?? ""}
                          onChange={(e) =>
                            setTrackingInputs((prev) => ({ ...prev, [ord.id]: e.target.value }))
                          }
                          placeholder="Tracking #"
                          disabled={blocked70Unpaid}
                          className="w-28 rounded border border-slate-300 px-2 py-1 disabled:bg-slate-100"
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
                          className="rounded bg-slate-900 px-2 py-1 font-semibold text-white disabled:opacity-40"
                        >
                          Ship
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {drawerOrderId && (
        <ShipmentTimelineDrawer
          orderId={drawerOrderId}
          onClose={() => setDrawerOrderId(null)}
        />
      )}
    </AppShell>
  );
}
