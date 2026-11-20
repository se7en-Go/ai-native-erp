"use client";

import { useI18n } from "@/lib/i18n";

import React, { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { AppShell } from "@/components/app-shell";
import { useAuthSession } from "@/components/providers";
import type { CreateStocktake } from "@/lib/api-client/types.gen";
import { fetchPendingProposalsCount } from "@/lib/data-source";
import {
  addPhase3aStocktake,
  fetchPhase3aMovements,
  fetchPhase3aStock,
  fetchPhase3aVariants,
  fetchPhase3aWarehouses,
} from "@/lib/phase3a-data";

const OPERATOR_ROLES = new Set(["owner", "admin", "manager"]);

export default function WmsPage() {
  const { bi } = useI18n();
  const queryClient = useQueryClient();
  const { principal } = useAuthSession();
  const [variantId, setVariantId] = useState("");
  const [delta, setDelta] = useState("1");
  const [notes, setNotes] = useState("");

  const canAdjust = OPERATOR_ROLES.has(principal?.role ?? "");

  const proposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () => fetchPendingProposalsCount(),
  });
  const movementsQuery = useQuery({
    queryKey: ["wms", "movements"],
    queryFn: () => fetchPhase3aMovements(),
  });
  const stockQuery = useQuery({ queryKey: ["wms", "stock"], queryFn: () => fetchPhase3aStock() });
  const variantsQuery = useQuery({
    queryKey: ["catalog", "variants", "picker"],
    queryFn: () => fetchPhase3aVariants(),
  });
  const warehousesQuery = useQuery({
    queryKey: ["orders", "warehouses"],
    queryFn: () => fetchPhase3aWarehouses(),
  });

  const adjust = useMutation({
    mutationFn: () => {
      const payload: CreateStocktake = {
        movement_type: "stocktake_adjust",
        request_id: uuid(),
        warehouse_id:
          warehousesQuery.data?.default_warehouse_id ?? warehousesQuery.data?.items?.[0]?.id ?? "",
        variant_id: variantId || variantsQuery.data?.items?.[0]?.id || "",
        qty_delta: Number(delta) || 0,
        notes: notes.trim() || null,
      };
      return addPhase3aStocktake(payload);
    },
    onSuccess: () => {
      setNotes("");
      void queryClient.invalidateQueries({ queryKey: ["wms"] });
    },
  });

  const movements = movementsQuery.data?.items ?? [];
  const stock = stockQuery.data?.items ?? [];

  return (
    <AppShell currentPath="/wms" pendingProposalsCount={proposalsQuery.data?.total ?? 0}>
      <div className="erp-page space-y-4">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-slate-900">{bi("WMS 出入库台账", "WMS movements")}</h1>
          <p className="text-xs text-slate-500">
            {bi("流水来自采购收货、订单出库与盘点调整；人工仅可提交盘点调整，且不可占用已预占库存。", "Movements come from receiving, order dispatch and stocktakes; manual stocktake adjustments can never consume reserved stock.")}
          </p>
        </div>

        <section className="erp-card p-3">
          <h2 className="text-sm font-semibold text-slate-900">{bi("实时库存", "Live stock")}</h2>
          <table data-testid="wms-stock-table" className="erp-table mt-2">
            <thead className="text-[10px] uppercase text-slate-400">
              <tr>
                <th>SKU</th>
                <th>{bi("仓库", "Warehouse")}</th>
                <th className="text-right">{bi("现存", "On hand")}</th>
                <th className="text-right">{bi("预占", "Reserved")}</th>
                <th className="text-right">{bi("可用", "Available")}</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 text-slate-700">
              {stock.map((row) => (
                <tr key={`${row.warehouse_id}-${row.variant_id}`}>
                  <td className="py-1.5 font-medium">{row.sku}</td>
                  <td className="py-1.5 text-slate-500">{row.warehouse_id.slice(0, 8)}</td>
                  <td className="py-1.5">{row.on_hand_qty}</td>
                  <td className="py-1.5">{row.reserved_qty}</td>
                  <td className="py-1.5 font-semibold">{row.available_qty}</td>
                </tr>
              ))}
              {stock.length === 0 && (
                <tr>
                  <td colSpan={5} className="py-4 text-center text-slate-400">
                    {stockQuery.isLoading ? bi("加载中…", "Loading…") : bi("暂无库存行", "No stock rows")}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </section>

        {canAdjust ? (
          <form
            data-testid="stocktake-form"
            onSubmit={(event) => {
              event.preventDefault();
              adjust.mutate();
            }}
            className="erp-card grid gap-2 p-3 sm:grid-cols-4"
          >
            <select
              aria-label="Stocktake SKU"
              value={variantId || variantsQuery.data?.items?.[0]?.id || ""}
              onChange={(event) => setVariantId(event.target.value)}
              className="rounded border border-slate-300 px-2 py-1 text-xs"
            >
              {(variantsQuery.data?.items ?? []).map((variant) => (
                <option key={variant.id} value={variant.id}>
                  {variant.sku}
                </option>
              ))}
            </select>
            <input
              aria-label="Quantity delta"
              type="number"
              required
              value={delta}
              onChange={(event) => setDelta(event.target.value)}
              className="rounded border border-slate-300 px-2 py-1 text-xs"
            />
            <input
              aria-label="Stocktake notes"
              value={notes}
              onChange={(event) => setNotes(event.target.value)}
              placeholder="盘点说明"
              className="rounded border border-slate-300 px-2 py-1 text-xs"
            />
            <button
              type="submit"
              data-testid="stocktake-submit"
              disabled={adjust.isPending || Number(delta) === 0}
              className="rounded bg-blue-600 px-3 py-1 text-xs font-medium text-white hover:bg-blue-700 disabled:opacity-40"
            >
              {bi("提交盘点调整", "Submit stocktake")}
            </button>
            {adjust.isError && (
              <p role="alert" className="sm:col-span-4 text-xs text-rose-700">
                {adjust.error.message}
              </p>
            )}
          </form>
        ) : (
          <p
            data-testid="stocktake-forbidden"
            className="rounded border border-slate-200 bg-slate-50 p-2 text-xs text-slate-500"
          >
            {bi(`当前角色（${principal?.role}）仅可查看流水与库存，盘点调整需要 Owner / Admin / Manager。`, `Your role (${principal?.role}) can only view movements and stock; stocktakes require Owner / Admin / Manager.`)}
          </p>
        )}

        <div className="overflow-x-auto rounded-xl border border-slate-200/80 bg-white">
          <table data-testid="wms-movements-table" className="erp-table">
            <thead className="border-b border-slate-200 bg-slate-50 font-semibold text-slate-600">
              <tr>
                <th>{bi("流水号", "Movement no.")}</th>
                <th>{bi("类型", "Type")}</th>
                <th className="text-right">{bi("数量", "Qty")}</th>
                <th>{bi("关联单据", "Reference")}</th>
                <th>{bi("时间", "Time")}</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {movements.map((movement) => (
                <tr key={movement.id} data-testid={`movement-row-${movement.id}`}>
                  <td className="px-3 py-2 font-medium text-slate-800">{movement.movement_no}</td>
                  <td className="px-3 py-2 text-slate-600">{movement.movement_type}</td>
                  <td
                    className={`px-3 py-2 font-semibold ${
                      movement.qty_delta >= 0 ? "text-emerald-700" : "text-rose-700"
                    }`}
                  >
                    {movement.qty_delta > 0 ? `+${movement.qty_delta}` : movement.qty_delta}
                  </td>
                  <td className="px-3 py-2 text-slate-500">
                    {movement.reference_type ?? "—"}
                  </td>
                  <td className="px-3 py-2 text-slate-500">
                    {new Date(movement.created_at).toLocaleString()}
                  </td>
                </tr>
              ))}
              {movements.length === 0 && (
                <tr>
                  <td colSpan={5} className="px-3 py-6 text-center text-slate-400">
                    {movementsQuery.isLoading ? bi("加载中…", "Loading…") : bi("暂无流水", "No movements")}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </AppShell>
  );
}

function uuid(): string {
  const cryptoRef = globalThis.crypto;
  if (cryptoRef && typeof cryptoRef.randomUUID === "function") return cryptoRef.randomUUID();
  const hex = [...Array(32)].map(() => Math.floor(Math.random() * 16).toString(16)).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-4${hex.slice(13, 16)}-a${hex.slice(17, 20)}-${hex.slice(20, 32)}`;
}
