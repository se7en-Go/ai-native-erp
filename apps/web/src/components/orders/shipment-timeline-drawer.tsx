"use client";

import React, { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, RefreshCw, Truck, X, Database } from "lucide-react";
import { bffFetch } from "@/lib/api";
import {
  fetchInventoryBalances,
  fetchInventoryLedger,
  fetchOrderDetail,
} from "@/lib/data-source";

export function ShipmentTimelineDrawer({
  orderId,
  onClose,
}: {
  orderId: string;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
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
    mutationFn: (shipmentId: string) =>
      bffFetch(`/v1/orders/${orderId}/shipments/${shipmentId}/refresh-tracking`, {
        method: "POST",
        body: JSON.stringify({ cache_level: 0 }),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["orders", orderId] });
    },
  });

  const order = detailQuery.data;

  return (
    <div
      data-testid="shipment-timeline-drawer"
      className="fixed inset-y-0 right-0 z-50 flex w-full max-w-lg flex-col border-l border-slate-200 bg-white shadow-2xl"
    >
      <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
        <div>
          <h3 className="text-sm font-bold text-slate-900">
            17TRACK 9+30 Timeline & Inventory Ledger
          </h3>
          {order && <p className="text-xs text-slate-500">{order.order_no}</p>}
        </div>
        <button
          type="button"
          onClick={onClose}
          className="rounded p-1 text-slate-500 hover:bg-slate-100"
          aria-label="Close shipment drawer"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="flex-1 space-y-4 overflow-y-auto p-4 text-xs">
        <div className="flex justify-end">
          <button
            type="button"
            data-testid="open-inventory-ledger-btn"
            onClick={() => setLedgerModalOpen(true)}
            className="inline-flex items-center gap-1.5 rounded border border-slate-300 bg-slate-50 px-2.5 py-1.5 font-semibold text-slate-700 hover:bg-slate-100"
          >
            <Database className="h-3.5 w-3.5" />
            <span>Atomic Inventory Ledger</span>
          </button>
        </div>

        {(order?.shipments ?? []).length === 0 ? (
          <div className="rounded border border-slate-200 p-4 text-center text-slate-400">
            No shipments dispatched yet.
          </div>
        ) : (
          order!.shipments.map((ship) => (
            <div key={ship.id} className="rounded-lg border border-slate-200 p-3 space-y-2.5">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-1.5 font-bold text-slate-900">
                  <Truck className="h-4 w-4 text-indigo-600" />
                  <span>{ship.tracking_number}</span>
                  {ship.carrier_code && (
                    <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px]">
                      {ship.carrier_code}
                    </span>
                  )}
                </div>

                <button
                  type="button"
                  onClick={() => refreshTrackingMutation.mutate(ship.id)}
                  className="inline-flex items-center gap-1 rounded border border-slate-300 px-2 py-1 text-[11px] font-medium hover:bg-slate-100"
                  title="Standard 1-credit cacheLevel=0 refresh"
                >
                  <RefreshCw className="h-3 w-3" />
                  <span>Refresh (cacheLevel=0)</span>
                </button>
              </div>

              <div className="flex flex-wrap items-center gap-1.5">
                <span className="rounded bg-indigo-50 px-2 py-0.5 font-semibold text-indigo-700">
                  {ship.standardized_status}
                </span>
                {ship.sub_status && (
                  <span className="rounded bg-slate-100 px-2 py-0.5 text-slate-700">
                    {ship.sub_status}
                  </span>
                )}
                {ship.is_stagnant_72h && (
                  <span className="inline-flex items-center gap-1 rounded bg-rose-100 px-2 py-0.5 font-semibold text-rose-800">
                    <AlertTriangle className="h-3 w-3" />
                    <span>72h Stagnant / Exception Alert</span>
                  </span>
                )}
              </div>

              <div className="space-y-2 border-l-2 border-slate-200 pl-3">
                {ship.tracking_events.map((ev, idx) => (
                  <div key={idx} className="space-y-0.5">
                    <div className="font-semibold text-slate-800">{ev.description}</div>
                    <div className="text-[11px] text-slate-500">
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
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
        >
          <div className="w-full max-w-2xl rounded-lg bg-white p-4 shadow-xl text-xs space-y-3">
            <div className="flex items-center justify-between border-b border-slate-200 pb-2">
              <h4 className="text-sm font-bold text-slate-900">
                Atomic Inventory Balances & Immutable Ledger
              </h4>
              <button
                type="button"
                onClick={() => setLedgerModalOpen(false)}
                className="rounded border border-slate-300 px-2 py-0.5"
              >
                Close
              </button>
            </div>

            <div className="grid gap-2 sm:grid-cols-2">
              {(balancesQuery.data?.items ?? []).map((bal) => (
                <div
                  key={`${bal.warehouse_id}-${bal.variant_id}`}
                  className="rounded border border-slate-200 bg-slate-50 p-2"
                >
                  <div className="font-bold text-slate-900">{bal.sku}</div>
                  <div>
                    On Hand: <strong>{bal.on_hand_qty}</strong> · Reserved:{" "}
                    <strong>{bal.reserved_qty}</strong> · Available:{" "}
                    <strong>{bal.on_hand_qty - bal.reserved_qty}</strong>
                  </div>
                </div>
              ))}
            </div>

            <div className="max-h-64 overflow-y-auto border border-slate-200 rounded">
              <table className="w-full text-left">
                <thead className="bg-slate-50 text-slate-600">
                  <tr>
                    <th className="px-2 py-1.5">Event</th>
                    <th className="px-2 py-1.5">Δ On Hand</th>
                    <th className="px-2 py-1.5">Δ Reserved</th>
                    <th className="px-2 py-1.5">Idempotency Key</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {(ledgerQuery.data?.items ?? []).map((row) => (
                    <tr key={row.id}>
                      <td className="px-2 py-1.5 font-semibold">{row.event_type}</td>
                      <td className="px-2 py-1.5">{row.qty_delta_on_hand}</td>
                      <td className="px-2 py-1.5">{row.qty_delta_reserved}</td>
                      <td className="px-2 py-1.5 font-mono text-[11px]">{row.idempotency_key}</td>
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
