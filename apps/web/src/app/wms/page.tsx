"use client";

import { useI18n } from "@/lib/i18n";

import React, { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowDownUp, Boxes, PackageCheck, Warehouse } from "lucide-react";

import { AppShell } from "@/components/app-shell";
import { BentoStatCard } from "@/components/ui/bento-stat-card";
import { Page, PageHeader, TableWrap } from "@/components/ui/page";
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

  const summary = useMemo(() => {
    const onHand = stock.reduce((total, row) => total + row.on_hand_qty, 0);
    const reserved = stock.reduce((total, row) => total + row.reserved_qty, 0);
    const inbound = movements
      .filter((m) => m.qty_delta > 0)
      .reduce((total, m) => total + m.qty_delta, 0);
    return { onHand, reserved, inbound, available: onHand - reserved };
  }, [stock, movements]);

  return (
    <AppShell currentPath="/wms" pendingProposalsCount={proposalsQuery.data?.total ?? 0}>
      <Page>
        <PageHeader
          title={bi("WMS 出入库台账", "WMS movements")}
          description={bi(
            "流水来自采购收货、订单出库与盘点调整；人工仅可提交盘点调整，且不可占用已预占库存。",
            "Movements come from receiving, order dispatch and stocktakes; manual stocktake adjustments can never consume reserved stock."
          )}
        />

        <section
          aria-label={bi("库存关键指标", "Inventory key metrics")}
          className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"
        >
          <BentoStatCard
            label={bi("现存总量", "Total on hand")}
            value={summary.onHand}
            icon={Boxes}
            accent="blue"
            hint={bi("跨全部仓库合计", "Across every warehouse")}
          />
          <BentoStatCard
            label={bi("已预占", "Reserved")}
            value={summary.reserved}
            icon={Warehouse}
            accent="amber"
            hint={bi("盘点调整不可占用预占库存", "Stocktakes can never consume reserved stock")}
          />
          <BentoStatCard
            label={bi("可用库存", "Available")}
            value={summary.available}
            icon={PackageCheck}
            accent="emerald"
            hint={bi("现存减去预占", "On hand minus reserved")}
          />
          <BentoStatCard
            label={bi("累计入库", "Total inbound")}
            value={summary.inbound}
            icon={ArrowDownUp}
            accent="violet"
            hint={bi("按当前台账正数流水汇总", "Summed from positive movements")}
          />
        </section>

        <TableWrap className="rounded-2xl">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 bg-slate-50/80 px-3.5 py-2.5">
            <h2 className="text-[13.5px] font-semibold text-slate-900">
              {bi("实时库存", "Live stock")}
            </h2>
            <span className="rounded-full border border-blue-200 bg-blue-50 px-2 py-0.5 text-[11px] font-semibold tabular-nums text-blue-800">
              {stock.length} {bi("行", "rows")}
            </span>
          </div>
          <table data-testid="wms-stock-table" className="erp-table">
            <thead>
              <tr>
                <th>SKU</th>
                <th>{bi("仓库", "Warehouse")}</th>
                <th className="text-right">{bi("现存", "On hand")}</th>
                <th className="text-right">{bi("预占", "Reserved")}</th>
                <th className="text-right">{bi("可用", "Available")}</th>
              </tr>
            </thead>
            <tbody>
              {stock.map((row) => (
                <tr key={`${row.warehouse_id}-${row.variant_id}`}>
                  <td className="py-1.5">
                    <span className="font-mono text-[12px] font-semibold text-slate-900">
                      {row.sku}
                    </span>
                  </td>
                  <td className="py-1.5 text-slate-600">
                    <span className="rounded-md bg-slate-100 px-1.5 py-0.5 font-mono text-[11px]">
                      {row.warehouse_id.slice(0, 8)}
                    </span>
                  </td>
                  <td className="py-1.5 tabular-nums text-slate-800">{row.on_hand_qty}</td>
                  <td className="py-1.5 tabular-nums text-amber-800">{row.reserved_qty}</td>
                  <td className="py-1.5 font-semibold tabular-nums text-emerald-700">
                    {row.available_qty}
                  </td>
                </tr>
              ))}
              {stock.length === 0 && (
                <tr>
                  <td colSpan={5} className="py-4 text-center font-medium text-slate-500">
                    {stockQuery.isLoading ? bi("加载中…", "Loading…") : bi("暂无库存行", "No stock rows")}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </TableWrap>

        {canAdjust ? (
          <form
            data-testid="stocktake-form"
            onSubmit={(event) => {
              event.preventDefault();
              adjust.mutate();
            }}
            className="erp-card grid gap-2 p-3.5 sm:grid-cols-4"
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
              className="rounded-lg bg-blue-600 px-3 py-1 text-xs font-semibold text-white shadow-xs transition-colors hover:bg-blue-700 disabled:opacity-40"
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
            className="rounded-xl border border-slate-200 bg-slate-50 p-3 text-[12px] font-medium text-slate-700"
          >
            {bi(`当前角色（${principal?.role}）仅可查看流水与库存，盘点调整需要 Owner / Admin / Manager。`, `Your role (${principal?.role}) can only view movements and stock; stocktakes require Owner / Admin / Manager.`)}
          </p>
        )}

        <TableWrap className="rounded-2xl">
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
                  <td className="px-3 py-2">
                    <span className="font-mono text-[12px] font-semibold text-slate-900">
                      {movement.movement_no}
                    </span>
                  </td>
                  <td className="px-3 py-2">
                    <span className="rounded-md bg-slate-100 px-1.5 py-0.5 text-[11px] font-medium text-slate-700">
                      {movement.movement_type}
                    </span>
                  </td>
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
        </TableWrap>
      </Page>
    </AppShell>
  );
}

function uuid(): string {
  const cryptoRef = globalThis.crypto;
  if (cryptoRef && typeof cryptoRef.randomUUID === "function") return cryptoRef.randomUUID();
  const hex = [...Array(32)].map(() => Math.floor(Math.random() * 16).toString(16)).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-4${hex.slice(13, 16)}-a${hex.slice(17, 20)}-${hex.slice(20, 32)}`;
}
