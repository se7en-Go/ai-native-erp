"use client";

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
      <div className="mx-auto max-w-6xl space-y-4">
        <div>
          <h1 className="text-lg font-bold text-slate-900">WMS 出入库 / Warehouse Movements</h1>
          <p className="text-xs text-slate-500">
            流水来自采购收货、订单出库与盘点调整；人工仅可提交盘点调整，且不可占用已预占库存。
          </p>
        </div>

        <section className="rounded-lg border border-slate-200 bg-white p-3">
          <h2 className="text-sm font-semibold text-slate-900">实时库存</h2>
          <table data-testid="wms-stock-table" className="mt-2 w-full text-left text-xs">
            <thead className="text-[10px] uppercase text-slate-400">
              <tr>
                <th className="py-1">SKU</th>
                <th className="py-1">仓</th>
                <th className="py-1">现存</th>
                <th className="py-1">预占</th>
                <th className="py-1">可用</th>
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
                    {stockQuery.isLoading ? "加载中…" : "暂无库存行"}
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
            className="grid gap-2 rounded-lg border border-slate-200 bg-white p-3 sm:grid-cols-4"
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
              className="rounded bg-indigo-600 px-3 py-1 text-xs font-medium text-white hover:bg-indigo-700 disabled:opacity-40"
            >
              提交盘点调整
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
            当前角色（{principal?.role}）仅可查看流水与库存，盘点调整需要 Owner / Admin / Manager。
          </p>
        )}

        <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
          <table data-testid="wms-movements-table" className="w-full text-left text-xs">
            <thead className="border-b border-slate-200 bg-slate-50 font-semibold text-slate-600">
              <tr>
                <th className="px-3 py-2.5">流水号</th>
                <th className="px-3 py-2.5">类型</th>
                <th className="px-3 py-2.5">数量</th>
                <th className="px-3 py-2.5">关联单据</th>
                <th className="px-3 py-2.5">时间</th>
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
                    {movementsQuery.isLoading ? "加载中…" : "暂无流水"}
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
