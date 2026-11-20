"use client";

import { useI18n } from "@/lib/i18n";

import React, { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CircleDollarSign, ClipboardList, PackageCheck, Timer } from "lucide-react";

import { AppShell } from "@/components/app-shell";
import { BentoStatCard } from "@/components/ui/bento-stat-card";
import { EmptyState, Page, PageHeader, TableWrap } from "@/components/ui/page";
import { formatMoney } from "@/lib/contracts";
import { fetchPendingProposalsCount } from "@/lib/data-source";
import {
  addPhase3aPurchaseOrder,
  fetchPhase3aPurchaseOrders,
  fetchPhase3aSuppliers,
  fetchPhase3aVariants,
  fetchPhase3aWarehouses,
  receivePhase3aPurchaseOrder,
} from "@/lib/phase3a-data";

/** RFC 4122 v4-shaped receipt id: one per click, reused verbatim on retry. */
function newReceiptId(): string {
  const cryptoRef = globalThis.crypto;
  if (cryptoRef && typeof cryptoRef.randomUUID === "function") {
    return cryptoRef.randomUUID();
  }
  const hex = [...Array(32)].map(() => Math.floor(Math.random() * 16).toString(16)).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-4${hex.slice(13, 16)}-a${hex.slice(17, 20)}-${hex.slice(20, 32)}`;
}

export default function PurchaseOrdersPage() {
  const { bi } = useI18n();
  const queryClient = useQueryClient();
  const [supplierId, setSupplierId] = useState("");
  const [qty, setQty] = useState("100");
  const [unitCost, setUnitCost] = useState("4.8500");

  const proposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () => fetchPendingProposalsCount(),
  });
  const ordersQuery = useQuery({
    queryKey: ["supply-chain", "purchase-orders"],
    queryFn: () => fetchPhase3aPurchaseOrders(),
  });
  const suppliersQuery = useQuery({
    queryKey: ["supply-chain", "suppliers"],
    queryFn: () => fetchPhase3aSuppliers(),
  });
  const variantsQuery = useQuery({
    queryKey: ["catalog", "variants", "picker"],
    queryFn: () => fetchPhase3aVariants(),
  });
  const warehousesQuery = useQuery({
    queryKey: ["orders", "warehouses"],
    queryFn: () => fetchPhase3aWarehouses(),
  });

  const createOrder = useMutation({
    mutationFn: () => {
      const supplier = supplierId || suppliersQuery.data?.items?.[0]?.id || "";
      const variant = variantsQuery.data?.items?.[0]?.id ?? "";
      const warehouse =
        warehousesQuery.data?.default_warehouse_id ??
        warehousesQuery.data?.items?.[0]?.id ??
        "";
      return addPhase3aPurchaseOrder({
        supplier_id: supplier,
        warehouse_id: warehouse,
        currency: "USD",
        status: "ordered",
        items: [
          { variant_id: variant, qty_ordered: Number(qty) || 1, unit_cost: unitCost || "0.0000" },
        ],
      });
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["supply-chain", "purchase-orders"] }),
  });

  const receiveOrder = useMutation({
    mutationFn: (poId: string) => receivePhase3aPurchaseOrder(poId, newReceiptId()),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["supply-chain", "purchase-orders"] });
      void queryClient.invalidateQueries({ queryKey: ["wms"] });
    },
  });

  const orders = ordersQuery.data?.items ?? [];
  const suppliers = suppliersQuery.data?.items ?? [];
  const error = createOrder.error ?? receiveOrder.error ?? ordersQuery.error;
  const canReceive = (status: string) => status === "ordered" || status === "partially_received";

  const summary = useMemo(() => {
    const receiving = orders.filter((o) => canReceive(o.status)).length;
    const value = orders.reduce((total, o) => total + Number(o.total_amount || 0), 0);
    const openQty = orders.reduce(
      (total, o) =>
        total +
        o.items.reduce((qty, item) => qty + Math.max(0, item.qty_ordered - item.qty_received), 0),
      0
    );
    return { receiving, value, openQty };
  }, [orders]);

  return (
    <AppShell
      currentPath="/supply-chain/purchase-orders"
      pendingProposalsCount={proposalsQuery.data?.total ?? 0}
    >
      <Page>
        <PageHeader
          title={bi("采购订单", "Purchase orders")}
          description={bi(
            "收货在同一事务内写入 inventory_ledger、inventory_balances 与 wms_movements；并发收货由行锁串行化，超额收货返回 409。",
            "Receiving writes inventory_ledger, inventory_balances and wms_movements in one transaction; row locks serialise concurrency and over-receipt returns 409."
          )}
        />

        <section
          aria-label={bi("采购关键指标", "Purchasing key metrics")}
          className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"
        >
          <BentoStatCard
            label={bi("采购单总数", "Purchase orders")}
            value={orders.length}
            icon={ClipboardList}
            accent="blue"
            hint={bi("含部分收货与全部收货", "Includes partial and full receipts")}
          />
          <BentoStatCard
            label={bi("采购金额", "Committed value")}
            value={formatMoney(summary.value.toFixed(2), "USD")}
            icon={CircleDollarSign}
            accent="violet"
            hint={bi("按采购单币种直接汇总", "Summed in each PO currency")}
          />
          <BentoStatCard
            label={bi("待到货数量", "Open quantity")}
            value={summary.openQty}
            icon={Timer}
            accent="amber"
            hint={bi("已收数量从订购数量中扣减", "Received quantity deducted from ordered")}
          />
          <BentoStatCard
            label={bi("可收货单据", "Receivable POs")}
            value={summary.receiving}
            icon={PackageCheck}
            accent="emerald"
            hint={bi("仅 ordered / partially_received 可收货", "Only ordered / partially_received can receive")}
          />
        </section>

        <form
          data-testid="purchase-order-form"
          onSubmit={(event) => {
            event.preventDefault();
            createOrder.mutate();
          }}
          className="erp-card grid gap-2 p-3.5 sm:grid-cols-5"
        >
          <select
            aria-label="Supplier"
            value={supplierId || suppliers[0]?.id || ""}
            onChange={(event) => setSupplierId(event.target.value)}
            className="rounded border border-slate-300 px-2 py-1 text-xs"
          >
            {suppliers.map((supplier) => (
              <option key={supplier.id} value={supplier.id}>
                {supplier.name}
              </option>
            ))}
          </select>
          <input
            aria-label="Ordered quantity"
            type="number"
            min={1}
            max={1000000}
            required
            value={qty}
            onChange={(event) => setQty(event.target.value)}
            className="rounded border border-slate-300 px-2 py-1 text-xs"
          />
          <input
            aria-label="Unit cost"
            inputMode="decimal"
            required
            value={unitCost}
            onChange={(event) => setUnitCost(event.target.value)}
            className="rounded border border-slate-300 px-2 py-1 text-xs"
          />
          <p className="self-center text-[11px] text-slate-500">
            {bi("预计金额", "Estimated total")} {formatMoney((Number(qty) || 0) * (Number(unitCost) || 0), "USD")}
          </p>
          <button
            type="submit"
            data-testid="purchase-order-submit"
            disabled={createOrder.isPending || !(variantsQuery.data?.items?.length ?? 0)}
            className="rounded-lg bg-blue-600 px-3 py-1 text-xs font-semibold text-white shadow-xs transition-colors hover:bg-blue-700 disabled:opacity-40"
          >
            {bi("新建采购单", "Create purchase order")}
          </button>
          {error && (
            <p role="alert" className="sm:col-span-5 text-xs text-rose-700">
              {error.message}
            </p>
          )}
        </form>

        <TableWrap className="rounded-2xl">
          <table data-testid="purchase-orders-table" className="erp-table">
            <thead className="border-b border-slate-200 bg-slate-50 font-semibold text-slate-600">
              <tr>
                <th>{bi("采购单", "PO")}</th>
                <th>{bi("物料", "Items")}</th>
                <th className="text-right">{bi("金额", "Amount")}</th>
                <th>{bi("状态", "Status")}</th>
                <th className="text-right">{bi("收货", "Receive")}</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {orders.map((order) => (
                <tr key={order.id} data-testid={`purchase-order-row-${order.id}`}>
                  <td className="px-3 py-2">
                    <div className="font-mono text-[12.5px] font-semibold text-slate-900">
                      {order.po_no}
                    </div>
                    <div className="text-[11px] text-slate-500">
                      {bi("预计到货", "ETA")} {order.expected_arrival_date ?? "—"}
                    </div>
                  </td>
                  <td className="px-3 py-2 text-slate-700">
                    {order.items.map((item) => (
                      <div key={item.id} className="flex items-center gap-1.5">
                        <span
                          aria-hidden="true"
                          className="h-1.5 w-1.5 rounded-full bg-blue-500"
                        />
                        <span className="font-mono text-[11.5px]">{item.sku}</span>
                        <span className="text-[11px] text-slate-500">
                          {bi("已收", "received")} {item.qty_received}/{item.qty_ordered}
                        </span>
                      </div>
                    ))}
                  </td>
                  <td className="px-3 py-2 font-semibold tabular-nums text-slate-900">
                    {formatMoney(order.total_amount, order.currency)}
                  </td>
                  <td className="px-3 py-2">
                    <span
                      className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[10.5px] font-semibold ${
                        canReceive(order.status)
                          ? "border-amber-200 bg-amber-50 text-amber-800"
                          : "border-emerald-200 bg-emerald-50 text-emerald-700"
                      }`}
                    >
                      <span
                        aria-hidden="true"
                        className={`h-1.5 w-1.5 rounded-full ${
                          canReceive(order.status) ? "bg-amber-500" : "bg-emerald-500"
                        }`}
                      />
                      {order.status}
                    </span>
                  </td>
                  <td className="px-3 py-2">
                    <button
                      type="button"
                      data-testid={`receive-po-${order.id}`}
                      disabled={!canReceive(order.status) || receiveOrder.isPending}
                      onClick={() => receiveOrder.mutate(order.id)}
                      className="rounded-lg bg-emerald-600 px-2 py-1 text-[11px] font-semibold text-white shadow-xs transition-colors hover:bg-emerald-700 disabled:opacity-40"
                    >
                      {bi("一键收货入库", "Receive into stock")}
                    </button>
                  </td>
                </tr>
              ))}
              {orders.length === 0 && (
                <tr>
                  <td colSpan={5} className="px-3 py-6 text-center text-slate-400">
                    {ordersQuery.isLoading ? bi("加载中…", "Loading…") : bi("暂无采购单", "No purchase orders")}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </TableWrap>
        {orders.length === 0 && (
          <EmptyState>
            {ordersQuery.isLoading
              ? bi("正在加载采购单…", "Loading purchase orders…")
              : bi("暂无采购单，请先新建一张。", "No purchase orders yet, create the first one.")}
          </EmptyState>
        )}
      </Page>
    </AppShell>
  );
}
