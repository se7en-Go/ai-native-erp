"use client";

import React, { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Boxes,
  CheckCircle2,
  Layers,
  PackageCheck,
  Plus,
  Ship,
  Trash2,
  Truck,
} from "lucide-react";

import { useAuthSession } from "@/components/providers";
import { Badge, Card, CardHeader, EmptyState, TableWrap } from "@/components/ui/page";
import { useI18n } from "@/lib/i18n";
import {
  SHIPPING_METHODS,
  assembleBomKit,
  cancelStockTransfer,
  createStockTransfer,
  deleteBomComponent,
  dispatchStockTransfer,
  docRiskCounts,
  fetchBomCostRollup,
  fetchBomKits,
  fetchDocReplenishment,
  fetchSkuOptions,
  fetchStockTransfers,
  fetchWarehouseOptions,
  formatDecimal,
  formatDocDays,
  receiveStockTransfer,
  riskLevelLabel,
  riskLevelLabelZh,
  riskTone,
  shippingMethodLabel,
  shippingMethodLabelZh,
  transferActions,
  transferStatusLabel,
  transferStatusLabelZh,
  upsertBomComponent,
  type ShippingMethod,
  type StockTransferRead,
} from "@/lib/api-client/track7-bom-doc";

const OPERATOR_ROLES = new Set(["owner", "admin", "manager"]);

/** Target DOC presets offered by the replenishment model. */
const TARGET_DOC_OPTIONS = [7, 14, 30, 45, 60, 90, 180] as const;

type TabKey = "bom" | "transfers" | "doc";

function uuid(): string {
  const cryptoRef = globalThis.crypto;
  if (cryptoRef && typeof cryptoRef.randomUUID === "function") return cryptoRef.randomUUID();
  const hex = [...Array(32)].map(() => Math.floor(Math.random() * 16).toString(16)).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-4${hex.slice(13, 16)}-a${hex.slice(17, 20)}-${hex.slice(20, 32)}`;
}

/**
 * Track 7 supply-chain panel: 1-level light BOM + headhaul in-transit transfers +
 * days-of-cover replenishment, mounted on /wms.
 *
 * Nothing here re-derives money or cover: the rolled-up BOM cost, the weighted
 * daily run rate and the DOC figure are the backend's exact decimal strings.
 * Mutations are all server-validated (1-level BOM guard, state machine, stock
 * availability); the panel only mirrors the state the server returns.
 */
export function BomTransferDocPanel() {
  const { bi } = useI18n();
  const { principal } = useAuthSession();
  const queryClient = useQueryClient();
  const canOperate = OPERATOR_ROLES.has(principal?.role ?? "");

  const [tab, setTab] = useState<TabKey>("bom");
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionNote, setActionNote] = useState<string | null>(null);

  const skusQuery = useQuery({
    queryKey: ["track7", "sku-options"],
    queryFn: fetchSkuOptions,
  });
  const transfersQuery = useQuery({
    queryKey: ["track7", "transfers"],
    queryFn: fetchStockTransfers,
  });
  const warehousesQuery = useQuery({
    queryKey: ["track7", "warehouse-options"],
    queryFn: () => fetchWarehouseOptions(transfersQuery.data?.items ?? []),
  });

  const skus = skusQuery.data ?? [];
  const transfers = transfersQuery.data?.items ?? [];
  const warehouses = warehousesQuery.data ?? [];
  const defaultWarehouseId = warehouses[0]?.id ?? "";

  const invalidateTransfers = () =>
    void queryClient.invalidateQueries({ queryKey: ["track7", "transfers"] });

  // ---------------------------------------------------------------- BOM state
  const [parentId, setParentId] = useState("");
  const [componentId, setComponentId] = useState("");
  const [qtyPerUnit, setQtyPerUnit] = useState("1");
  const [wasteRatePct, setWasteRatePct] = useState("0");
  const [assembleQty, setAssembleQty] = useState("1");
  const [assembleWarehouseId, setAssembleWarehouseId] = useState("");

  const selectedParentId = parentId || skus[0]?.id || "";
  const bomQuery = useQuery({
    queryKey: ["track7", "bom", selectedParentId],
    queryFn: () => fetchBomKits(selectedParentId || undefined),
    enabled: Boolean(selectedParentId),
  });
  const rollupQuery = useQuery({
    queryKey: ["track7", "bom-rollup", selectedParentId],
    queryFn: () => fetchBomCostRollup(selectedParentId),
    enabled: Boolean(selectedParentId),
  });
  const kits = bomQuery.data?.items ?? [];
  const activeKit = kits.find((kit) => kit.parent_variant_id === selectedParentId) ?? kits[0];
  const rolledUpCost = rollupQuery.data?.unit_cost_base ?? "0.0000";

  const upsertMutation = useMutation({
    mutationFn: () =>
      upsertBomComponent({
        parent_variant_id: selectedParentId,
        component_variant_id: componentId || skus[1]?.id || "",
        qty_per_unit: qtyPerUnit.trim() || "1",
        waste_rate_pct: wasteRatePct.trim() || "0",
      }),
    onSuccess: () => {
      setActionError(null);
      setActionNote(
        bi("配方已保存，滚加成本已重算。", "Component saved; rolled-up cost recomputed."),
      );
      void queryClient.invalidateQueries({ queryKey: ["track7", "bom"] });
      void queryClient.invalidateQueries({
        queryKey: ["track7", "bom-rollup"],
      });
    },
    onError: (error: Error) => {
      setActionNote(null);
      setActionError(error.message);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (componentRowId: string) => deleteBomComponent(componentRowId),
    onSuccess: () => {
      setActionError(null);
      setActionNote(bi("子料已移除。", "Component removed."));
      void queryClient.invalidateQueries({ queryKey: ["track7", "bom"] });
      void queryClient.invalidateQueries({
        queryKey: ["track7", "bom-rollup"],
      });
    },
    onError: (error: Error) => {
      setActionNote(null);
      setActionError(error.message);
    },
  });

  const assembleMutation = useMutation({
    mutationFn: () =>
      assembleBomKit(selectedParentId, {
        warehouse_id: assembleWarehouseId || defaultWarehouseId,
        qty_kits: Math.max(1, Number(assembleQty) || 1),
        request_id: uuid(),
      }),
    onSuccess: (result) => {
      setActionError(null);
      setActionNote(
        bi(
          `已按配方组装 ${result.qty_kits} 套，扣减 ${result.consumed.length} 项子料，套件现存 ${result.parent_on_hand_qty}。`,
          `Assembled ${result.qty_kits} kit(s), consumed ${result.consumed.length} component line(s); kit on hand ${result.parent_on_hand_qty}.`,
        ),
      );
      void queryClient.invalidateQueries({ queryKey: ["track7", "bom"] });
      void queryClient.invalidateQueries({ queryKey: ["track7", "doc"] });
      void queryClient.invalidateQueries({ queryKey: ["wms"] });
    },
    onError: (error: Error) => {
      setActionNote(null);
      setActionError(error.message);
    },
  });

  // ---------------------------------------------------------- transfer state
  const [sourceWarehouseId, setSourceWarehouseId] = useState("");
  const [destinationWarehouseId, setDestinationWarehouseId] = useState("");
  const [shippingMethod, setShippingMethod] = useState<ShippingMethod>("sea_freight");
  const [transferSkuId, setTransferSkuId] = useState("");
  const [transferQty, setTransferQty] = useState("10");
  const [transferTrackingNo, setTransferTrackingNo] = useState("");

  const resolvedSourceId = sourceWarehouseId || warehouses[0]?.id || "";
  const resolvedDestinationId =
    destinationWarehouseId ||
    warehouses.find((warehouse) => warehouse.id !== resolvedSourceId)?.id ||
    "";

  const createTransferMutation = useMutation({
    mutationFn: () =>
      createStockTransfer({
        source_warehouse_id: resolvedSourceId,
        destination_warehouse_id: resolvedDestinationId,
        shipping_method: shippingMethod,
        tracking_no: transferTrackingNo.trim() || null,
        lines: [
          {
            variant_id: transferSkuId || skus[0]?.id || "",
            qty: Math.max(1, Number(transferQty) || 1),
          },
        ],
      }),
    onSuccess: (transfer) => {
      setActionError(null);
      setActionNote(
        bi(
          `已创建头程调拨单 ${transfer.transfer_no}（草稿）。`,
          `Headhaul transfer ${transfer.transfer_no} created as a draft.`,
        ),
      );
      setTransferTrackingNo("");
      invalidateTransfers();
    },
    onError: (error: Error) => {
      setActionNote(null);
      setActionError(error.message);
    },
  });

  const transitionMutation = useMutation({
    mutationFn: (input: { id: string; action: "dispatch" | "receive" | "cancel" }) => {
      if (input.action === "dispatch") return dispatchStockTransfer(input.id);
      if (input.action === "receive") return receiveStockTransfer(input.id);
      return cancelStockTransfer(input.id);
    },
    onSuccess: (transfer) => {
      setActionError(null);
      setActionNote(
        bi(
          `调拨单 ${transfer.transfer_no} 状态已更新为 ${transferStatusLabelZh(transfer.status)}。`,
          `Transfer ${transfer.transfer_no} is now ${transferStatusLabel(transfer.status)}.`,
        ),
      );
      invalidateTransfers();
      void queryClient.invalidateQueries({ queryKey: ["track7", "doc"] });
      void queryClient.invalidateQueries({ queryKey: ["wms"] });
    },
    onError: (error: Error) => {
      setActionNote(null);
      setActionError(error.message);
    },
  });

  // --------------------------------------------------------------- DOC state
  const [targetDocDays, setTargetDocDays] = useState<number>(30);
  const docQuery = useQuery({
    queryKey: ["track7", "doc", targetDocDays],
    queryFn: () => fetchDocReplenishment(targetDocDays),
  });
  const docRows = docQuery.data?.items ?? [];
  const docCounts = useMemo(() => docRiskCounts(docRows), [docRows]);

  return (
    <Card data-testid="bom-transfer-doc-panel" className="overflow-hidden rounded-[20px]">
      <CardHeader
        icon={Layers}
        title={bi(
          "轻 BOM 组合、头程在途与 DOC 补货",
          "Light BOM kits, headhaul in-transit & DOC replenishment",
        )}
        description={bi(
          "一级轻 BOM 组装扣减子料；头程调拨在途期间不计入可售；按 7/30/90 天加权日均销量算出可售天数与建议补货量。",
          "1-level kits consume components on assembly; headhaul units stay in transit until receipt; DOC uses 7/30/90-day weighted daily sales.",
        )}
      />

      <div
        role="tablist"
        aria-label={bi("轻 BOM、头程、可售天数", "BOM, transfers, days of cover")}
        className="flex flex-wrap gap-1.5 border-b border-gray-200 dark:border-gray-800 bg-gray-50/80 dark:bg-gray-800/50 px-3.5 py-2"
      >
        {(
          [
            { key: "bom", zh: "1. 轻 BOM 组合配方", en: "1. Light BOM" },
            {
              key: "transfers",
              zh: "2. 头程在途调拨单",
              en: "2. Headhaul transfers",
            },
            {
              key: "doc",
              zh: "3. 可售天数 (DOC) 补货模型",
              en: "3. DOC replenishment",
            },
          ] as Array<{ key: TabKey; zh: string; en: string }>
        ).map((item) => {
          const active = tab === item.key;
          return (
            <button
              key={item.key}
              type="button"
              role="tab"
              aria-selected={active}
              data-testid={`track7-tab-${item.key}`}
              onClick={() => {
                setTab(item.key);
                setActionError(null);
                setActionNote(null);
              }}
              className={`rounded-xl px-2.5 py-1.5 text-[12px] font-semibold transition-colors ${
                active
                  ? "bg-blue-600 text-white shadow-xs"
                  : "border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800"
              }`}
            >
              {bi(item.zh, item.en)}
            </button>
          );
        })}
      </div>

      {(actionError || actionNote) && (
        <div
          role={actionError ? "alert" : undefined}
          data-testid={actionError ? "track7-error" : "track7-note"}
          className={`flex items-start gap-2 border-b px-3.5 py-2 text-[12px] font-medium ${
            actionError
              ? "border-rose-200 bg-rose-50 text-rose-800 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-300"
              : "border-emerald-200 bg-emerald-50 text-emerald-900 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-200"
          }`}
        >
          {actionError ? (
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          ) : (
            <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          )}
          <span>{actionError ?? actionNote}</span>
        </div>
      )}

      {!canOperate && (
        <p
          data-testid="track7-readonly-hint"
          className="border-b border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-800/60 px-3.5 py-2 text-[12px] font-medium text-gray-600 dark:text-gray-400"
        >
          {bi(
            `当前角色（${principal?.role ?? "-"}）仅可查看配方、在途与可售天数；修改需要 Owner / Admin / Manager。`,
            `Your role (${principal?.role ?? "-"}) can view formulas, in-transit loads and DOC only; changes require Owner / Admin / Manager.`,
          )}
        </p>
      )}

      {tab === "bom" && (
        <div className="space-y-3 p-3.5">
          {canOperate && (
            <div className="grid gap-2.5 sm:grid-cols-2 xl:grid-cols-5">
              <label className="block">
                <span className="mb-1 block text-[11.5px] font-semibold text-gray-700 dark:text-gray-300">
                  {bi("套件 SKU（父）", "Kit SKU (parent)")}
                </span>
                <select
                  data-testid="bom-parent-select"
                  value={selectedParentId}
                  onChange={(event) => setParentId(event.target.value)}
                  className="erp-input w-full rounded-xl font-mono text-[12px]"
                >
                  {skus.map((option) => (
                    <option key={option.id} value={option.id}>
                      {option.sku}
                    </option>
                  ))}
                </select>
              </label>
              <label className="block">
                <span className="mb-1 block text-[11.5px] font-semibold text-gray-700 dark:text-gray-300">
                  {bi("子料 SKU", "Component SKU")}
                </span>
                <select
                  data-testid="bom-component-select"
                  value={componentId || skus[1]?.id || ""}
                  onChange={(event) => setComponentId(event.target.value)}
                  className="erp-input w-full rounded-xl font-mono text-[12px]"
                >
                  {skus.map((option) => (
                    <option key={option.id} value={option.id}>
                      {option.sku}
                    </option>
                  ))}
                </select>
              </label>
              <label className="block">
                <span className="mb-1 block text-[11.5px] font-semibold text-gray-700 dark:text-gray-300">
                  {bi("单套用量", "Qty per unit")}
                </span>
                <input
                  data-testid="bom-qty-per-unit"
                  type="number"
                  min="0.0001"
                  step="0.0001"
                  value={qtyPerUnit}
                  onChange={(event) => setQtyPerUnit(event.target.value)}
                  className="erp-input w-full rounded-xl tabular-nums"
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-[11.5px] font-semibold text-gray-700 dark:text-gray-300">
                  {bi("损耗率 (%)", "Waste rate (%)")}
                </span>
                <input
                  data-testid="bom-waste-rate"
                  type="number"
                  min="0"
                  max="100"
                  step="0.01"
                  value={wasteRatePct}
                  onChange={(event) => setWasteRatePct(event.target.value)}
                  className="erp-input w-full rounded-xl tabular-nums"
                />
              </label>
              <div className="flex items-end">
                <button
                  type="button"
                  data-testid="bom-add-component"
                  disabled={
                    !selectedParentId || upsertMutation.isPending || !(Number(qtyPerUnit) > 0)
                  }
                  onClick={() => upsertMutation.mutate()}
                  className="erp-btn w-full rounded-xl bg-blue-600 text-[12px] font-semibold text-white shadow-xs transition-colors hover:bg-blue-700 disabled:opacity-40"
                >
                  <Plus className="h-3.5 w-3.5" />
                  <span>{bi("保存子料", "Save component")}</span>
                </button>
              </div>
            </div>
          )}

          <div className="flex flex-wrap items-center gap-3 rounded-xl border border-blue-200 bg-blue-50/80 px-3.5 py-2.5 dark:border-blue-500/30 dark:bg-blue-500/10">
            <span className="text-[12px] font-semibold text-blue-900 dark:text-blue-200">
              {bi("滚加单套成本（服务端 Decimal）", "Rolled-up unit cost (server Decimal)")}
            </span>
            <span
              data-testid="bom-rolled-up-cost"
              className="rounded-lg border border-blue-300 bg-white px-2 py-0.5 font-mono text-[13px] font-bold tabular-nums text-blue-900 dark:border-blue-500/40 dark:bg-gray-900 dark:text-blue-200"
            >
              {formatDecimal(rolledUpCost)} {rollupQuery.data?.base_currency ?? "CNY"}
            </span>
            <span className="text-[11.5px] font-medium text-blue-800/80 dark:text-blue-300/80">
              {bi(
                `子料 ${rollupQuery.data?.component_count ?? 0} 项 · 成本 = Σ 子料成本 × 单套用量 × (1 + 损耗率)`,
                `${rollupQuery.data?.component_count ?? 0} component line(s) · cost = Σ component cost × qty × (1 + waste%)`,
              )}
            </span>
          </div>

          <TableWrap>
            <table data-testid="bom-components-table" className="erp-table">
              <thead>
                <tr>
                  <th>{bi("子料 SKU", "Component SKU")}</th>
                  <th className="text-right">{bi("单套用量", "Qty/unit")}</th>
                  <th className="text-right">{bi("损耗率", "Waste")}</th>
                  <th className="text-right">{bi("含损耗用量", "With waste")}</th>
                  <th className="text-right">{bi("子料单位成本", "Unit cost")}</th>
                  <th className="text-right">{bi("单套成本", "Line cost")}</th>
                  {canOperate && <th className="text-right">{bi("操作", "Action")}</th>}
                </tr>
              </thead>
              <tbody>
                {(activeKit?.components ?? []).map((component) => (
                  <tr key={component.id} data-testid={`bom-component-row-${component.id}`}>
                    <td className="py-1.5 font-mono text-[12px] font-semibold text-gray-900 dark:text-gray-100">
                      {component.component_sku}
                    </td>
                    <td className="py-1.5 text-right font-mono tabular-nums">
                      {formatDecimal(component.qty_per_unit)}
                    </td>
                    <td className="py-1.5 text-right font-mono tabular-nums">
                      {formatDecimal(component.waste_rate_pct, "0.00")}%
                    </td>
                    <td className="py-1.5 text-right font-mono tabular-nums text-blue-800 dark:text-blue-300">
                      {formatDecimal(component.effective_qty_per_unit)}
                    </td>
                    <td className="py-1.5 text-right font-mono tabular-nums">
                      {formatDecimal(component.component_unit_cost_base)}
                    </td>
                    <td className="py-1.5 text-right font-mono font-semibold tabular-nums">
                      {formatDecimal(component.line_unit_cost_base)}
                    </td>
                    {canOperate && (
                      <td className="py-1.5 text-right">
                        <button
                          type="button"
                          data-testid={`bom-delete-component-${component.id}`}
                          disabled={deleteMutation.isPending}
                          onClick={() => deleteMutation.mutate(component.id)}
                          className="erp-btn erp-btn-secondary rounded-xl py-1 text-[11.5px]"
                        >
                          <Trash2 className="h-3 w-3" />
                          <span>{bi("移除", "Remove")}</span>
                        </button>
                      </td>
                    )}
                  </tr>
                ))}
                {(activeKit?.components.length ?? 0) === 0 && (
                  <tr>
                    <td
                      colSpan={canOperate ? 7 : 6}
                      className="py-4 text-center text-[12px] font-medium text-gray-500 dark:text-gray-400"
                    >
                      {bomQuery.isLoading
                        ? bi("加载配方…", "Loading formula…")
                        : bi("该套件还没有子料配方。", "This kit has no components yet.")}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </TableWrap>

          {canOperate && (
            <div className="grid gap-2.5 rounded-xl border border-gray-200 dark:border-gray-800 bg-gray-50/70 dark:bg-gray-800/40 p-3 sm:grid-cols-4">
              <label className="block sm:col-span-2">
                <span className="mb-1 block text-[11.5px] font-semibold text-gray-700 dark:text-gray-300">
                  {bi("组装仓库", "Assembly warehouse")}
                </span>
                <select
                  data-testid="bom-assemble-warehouse"
                  value={assembleWarehouseId || defaultWarehouseId}
                  onChange={(event) => setAssembleWarehouseId(event.target.value)}
                  className="erp-input w-full rounded-xl text-[12px]"
                >
                  {warehouses.map((warehouse) => (
                    <option key={warehouse.id} value={warehouse.id}>
                      {warehouse.label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="block">
                <span className="mb-1 block text-[11.5px] font-semibold text-gray-700 dark:text-gray-300">
                  {bi("组装套数", "Kits to build")}
                </span>
                <input
                  data-testid="bom-assemble-qty"
                  type="number"
                  min="1"
                  step="1"
                  value={assembleQty}
                  onChange={(event) => setAssembleQty(event.target.value)}
                  className="erp-input w-full rounded-xl tabular-nums"
                />
              </label>
              <div className="flex items-end">
                <button
                  type="button"
                  data-testid="bom-assemble-submit"
                  disabled={
                    assembleMutation.isPending ||
                    (activeKit?.components.length ?? 0) === 0 ||
                    !defaultWarehouseId
                  }
                  onClick={() => assembleMutation.mutate()}
                  className="erp-btn w-full rounded-xl bg-emerald-600 text-[12px] font-semibold text-white shadow-xs transition-colors hover:bg-emerald-700 disabled:opacity-40"
                >
                  <Boxes className="h-3.5 w-3.5" />
                  <span>{bi("按配方组装扣减子料", "Assemble & consume")}</span>
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {tab === "transfers" && (
        <div className="space-y-3 p-3.5">
          {canOperate && (
            <div className="grid gap-2.5 sm:grid-cols-2 xl:grid-cols-6">
              <label className="block">
                <span className="mb-1 block text-[11.5px] font-semibold text-gray-700 dark:text-gray-300">
                  {bi("发出仓", "Source warehouse")}
                </span>
                <select
                  data-testid="transfer-source"
                  value={resolvedSourceId}
                  onChange={(event) => setSourceWarehouseId(event.target.value)}
                  className="erp-input w-full rounded-xl text-[12px]"
                >
                  {warehouses.map((warehouse) => (
                    <option key={warehouse.id} value={warehouse.id}>
                      {warehouse.label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="block">
                <span className="mb-1 block text-[11.5px] font-semibold text-gray-700 dark:text-gray-300">
                  {bi("目的仓（海外/FBA）", "Destination (overseas/FBA)")}
                </span>
                <select
                  data-testid="transfer-destination"
                  value={resolvedDestinationId}
                  onChange={(event) => setDestinationWarehouseId(event.target.value)}
                  className="erp-input w-full rounded-xl text-[12px]"
                >
                  {warehouses.map((warehouse) => (
                    <option key={warehouse.id} value={warehouse.id}>
                      {warehouse.label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="block">
                <span className="mb-1 block text-[11.5px] font-semibold text-gray-700 dark:text-gray-300">
                  {bi("运输方式", "Shipping method")}
                </span>
                <select
                  data-testid="transfer-method"
                  value={shippingMethod}
                  onChange={(event) => setShippingMethod(event.target.value as ShippingMethod)}
                  className="erp-input w-full rounded-xl text-[12px]"
                >
                  {SHIPPING_METHODS.map((method) => (
                    <option key={method} value={method}>
                      {bi(shippingMethodLabelZh(method), shippingMethodLabel(method))}
                    </option>
                  ))}
                </select>
              </label>
              <label className="block">
                <span className="mb-1 block text-[11.5px] font-semibold text-gray-700 dark:text-gray-300">
                  {bi("SKU", "SKU")}
                </span>
                <select
                  data-testid="transfer-sku"
                  value={transferSkuId || skus[0]?.id || ""}
                  onChange={(event) => setTransferSkuId(event.target.value)}
                  className="erp-input w-full rounded-xl font-mono text-[12px]"
                >
                  {skus.map((option) => (
                    <option key={option.id} value={option.id}>
                      {option.sku}
                    </option>
                  ))}
                </select>
              </label>
              <label className="block">
                <span className="mb-1 block text-[11.5px] font-semibold text-gray-700 dark:text-gray-300">
                  {bi("数量", "Quantity")}
                </span>
                <input
                  data-testid="transfer-qty"
                  type="number"
                  min="1"
                  step="1"
                  value={transferQty}
                  onChange={(event) => setTransferQty(event.target.value)}
                  className="erp-input w-full rounded-xl tabular-nums"
                />
              </label>
              <div className="flex items-end">
                <button
                  type="button"
                  data-testid="transfer-create"
                  disabled={
                    createTransferMutation.isPending ||
                    !resolvedSourceId ||
                    !resolvedDestinationId ||
                    resolvedSourceId === resolvedDestinationId ||
                    !(Number(transferQty) > 0)
                  }
                  onClick={() => createTransferMutation.mutate()}
                  className="erp-btn w-full rounded-xl bg-blue-600 text-[12px] font-semibold text-white shadow-xs transition-colors hover:bg-blue-700 disabled:opacity-40"
                >
                  <Ship className="h-3.5 w-3.5" />
                  <span>{bi("新建草稿", "New draft")}</span>
                </button>
              </div>
              <label className="block sm:col-span-2 xl:col-span-3">
                <span className="mb-1 block text-[11.5px] font-semibold text-gray-700 dark:text-gray-300">
                  {bi("头程运单号（可选）", "Headhaul tracking no. (optional)")}
                </span>
                <input
                  data-testid="transfer-tracking"
                  value={transferTrackingNo}
                  onChange={(event) => setTransferTrackingNo(event.target.value)}
                  placeholder="MATS-…"
                  className="erp-input w-full rounded-xl font-mono text-[12px]"
                />
              </label>
            </div>
          )}

          <TableWrap>
            <table data-testid="transfer-table" className="erp-table">
              <thead>
                <tr>
                  <th>{bi("调拨单号", "Transfer no.")}</th>
                  <th>{bi("航线", "Route")}</th>
                  <th>{bi("运输 / 运单号", "Method / tracking")}</th>
                  <th className="text-right">{bi("数量", "Qty")}</th>
                  <th className="text-right">{bi("头程运费", "Freight")}</th>
                  <th>{bi("状态", "Status")}</th>
                  {canOperate && <th className="text-right">{bi("操作", "Action")}</th>}
                </tr>
              </thead>
              <tbody>
                {transfers.map((transfer: StockTransferRead) => {
                  const actions = transferActions(transfer.status);
                  return (
                    <tr key={transfer.id} data-testid={`transfer-row-${transfer.id}`}>
                      <td className="py-1.5">
                        <div className="font-mono text-[12px] font-semibold text-gray-900 dark:text-gray-100">
                          {transfer.transfer_no}
                        </div>
                        <div className="text-[10.5px] text-gray-500 dark:text-gray-400">
                          {transfer.lines.map((line) => `${line.sku} × ${line.qty}`).join(" · ")}
                        </div>
                      </td>
                      <td className="py-1.5 text-[11.5px] text-gray-700 dark:text-gray-300">
                        <div className="flex items-center gap-1">
                          <Truck className="h-3 w-3 shrink-0 text-gray-400" />
                          <span>{transfer.source_warehouse_name}</span>
                        </div>
                        <div className="mt-0.5 text-gray-500 dark:text-gray-400">
                          → {transfer.destination_warehouse_name}
                        </div>
                      </td>
                      <td className="py-1.5 text-[11.5px] text-gray-700 dark:text-gray-300">
                        <div>
                          {bi(
                            shippingMethodLabelZh(transfer.shipping_method),
                            shippingMethodLabel(transfer.shipping_method),
                          )}
                        </div>
                        <div className="font-mono text-[10.5px] text-gray-500 dark:text-gray-400">
                          {transfer.tracking_no ?? "—"} · ETA {transfer.eta_days}d
                        </div>
                      </td>
                      <td className="py-1.5 text-right font-mono tabular-nums">
                        {transfer.total_qty}
                      </td>
                      <td className="py-1.5 text-right font-mono tabular-nums">
                        {formatDecimal(transfer.total_freight_cost_base)}
                      </td>
                      <td className="py-1.5">
                        <span data-testid={`transfer-status-${transfer.id}`}>
                          <Badge
                            tone={
                              transfer.status === "received"
                                ? "success"
                                : transfer.status === "in_transit"
                                  ? "info"
                                  : transfer.status === "cancelled"
                                    ? "danger"
                                    : "neutral"
                            }
                          >
                            {bi(
                              transferStatusLabelZh(transfer.status),
                              transferStatusLabel(transfer.status),
                            )}
                          </Badge>
                        </span>
                      </td>
                      {canOperate && (
                        <td className="py-1.5 text-right">
                          <div className="flex flex-wrap justify-end gap-1">
                            {actions.canDispatch && (
                              <button
                                type="button"
                                data-testid={`transfer-dispatch-${transfer.id}`}
                                disabled={transitionMutation.isPending}
                                onClick={() =>
                                  transitionMutation.mutate({
                                    id: transfer.id,
                                    action: "dispatch",
                                  })
                                }
                                className="erp-btn erp-btn-secondary rounded-xl py-1 text-[11px]"
                              >
                                {bi("发货出库", "Dispatch")}
                              </button>
                            )}
                            {actions.canReceive && (
                              <button
                                type="button"
                                data-testid={`transfer-receive-${transfer.id}`}
                                disabled={transitionMutation.isPending}
                                onClick={() =>
                                  transitionMutation.mutate({
                                    id: transfer.id,
                                    action: "receive",
                                  })
                                }
                                className="erp-btn rounded-xl bg-emerald-600 px-2 py-1 text-[11px] font-semibold text-white shadow-xs transition-colors hover:bg-emerald-700 disabled:opacity-40"
                              >
                                <PackageCheck className="h-3 w-3" />
                                <span>{bi("签收入库", "Receive")}</span>
                              </button>
                            )}
                            {actions.canCancel && (
                              <button
                                type="button"
                                data-testid={`transfer-cancel-${transfer.id}`}
                                disabled={transitionMutation.isPending}
                                onClick={() =>
                                  transitionMutation.mutate({
                                    id: transfer.id,
                                    action: "cancel",
                                  })
                                }
                                className="erp-btn erp-btn-secondary rounded-xl py-1 text-[11px]"
                              >
                                {bi("取消草稿", "Cancel draft")}
                              </button>
                            )}
                          </div>
                        </td>
                      )}
                    </tr>
                  );
                })}
                {transfers.length === 0 && (
                  <tr>
                    <td
                      colSpan={canOperate ? 7 : 6}
                      className="py-4 text-center text-[12px] font-medium text-gray-500 dark:text-gray-400"
                    >
                      {transfersQuery.isLoading
                        ? bi("加载调拨单…", "Loading transfers…")
                        : bi("暂无头程调拨单。", "No headhaul transfers yet.")}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </TableWrap>
        </div>
      )}

      {tab === "doc" && (
        <div className="space-y-3 p-3.5">
          <div className="flex flex-wrap items-center gap-2.5">
            <label className="flex items-center gap-2">
              <span className="text-[11.5px] font-semibold text-gray-700 dark:text-gray-300">
                {bi("目标可售天数", "Target days of cover")}
              </span>
              <select
                data-testid="doc-target-days"
                value={targetDocDays}
                onChange={(event) => setTargetDocDays(Number(event.target.value))}
                className="erp-input rounded-xl text-[12px] tabular-nums"
              >
                {TARGET_DOC_OPTIONS.map((days) => (
                  <option key={days} value={days}>
                    {days} d
                  </option>
                ))}
              </select>
            </label>
            <Badge tone="danger">
              {bi("紧急", "Critical")} {docCounts.critical}
            </Badge>
            <Badge tone="warning">
              {bi("预警", "Warning")} {docCounts.warning}
            </Badge>
            <Badge tone="success">
              {bi("健康", "Healthy")} {docCounts.healthy}
            </Badge>
            <span className="text-[11.5px] font-medium text-gray-500 dark:text-gray-400">
              {bi(
                "加权日均销量 = 7 天 × 50% + 30 天 × 30% + 90 天 × 20%；在途头程计入总可用但不可售。",
                "Weighted daily sales = 7d × 50% + 30d × 30% + 90d × 20%; in-transit units count as cover, not as sellable.",
              )}
            </span>
          </div>

          <TableWrap>
            <table data-testid="doc-table" className="erp-table">
              <thead>
                <tr>
                  <th>{bi("SKU", "SKU")}</th>
                  <th className="text-right">{bi("本地可用", "Local")}</th>
                  <th className="text-right">{bi("在途头程", "In transit")}</th>
                  <th className="text-right">{bi("海外仓可用", "Overseas")}</th>
                  <th className="text-right">{bi("加权日均销量", "Weighted daily")}</th>
                  <th className="text-right">{bi("DOC 可售天数", "DOC days")}</th>
                  <th className="text-right">{bi("建议补货量", "Suggested reorder")}</th>
                  <th>{bi("风险", "Risk")}</th>
                </tr>
              </thead>
              <tbody>
                {docRows.map((row) => (
                  <tr key={row.variant_id} data-testid={`doc-row-${row.variant_id}`}>
                    <td className="py-1.5">
                      <div className="font-mono text-[12px] font-semibold text-gray-900 dark:text-gray-100">
                        {row.sku}
                      </div>
                      <div className="text-[10.5px] text-gray-500 dark:text-gray-400">
                        {row.product_name}
                      </div>
                    </td>
                    <td className="py-1.5 text-right font-mono tabular-nums">
                      {row.local_available_qty}
                    </td>
                    <td className="py-1.5 text-right font-mono tabular-nums text-blue-800 dark:text-blue-300">
                      {row.in_transit_qty}
                    </td>
                    <td className="py-1.5 text-right font-mono tabular-nums">
                      {row.overseas_available_qty}
                    </td>
                    <td className="py-1.5 text-right font-mono tabular-nums">
                      <span data-testid={`doc-weighted-${row.variant_id}`}>
                        {formatDecimal(row.weighted_daily_sales)}
                      </span>
                    </td>
                    <td className="py-1.5 text-right font-mono font-semibold tabular-nums">
                      <span data-testid={`doc-days-${row.variant_id}`}>
                        {formatDocDays(row.doc_days)}
                      </span>
                    </td>
                    <td className="py-1.5 text-right font-mono font-semibold tabular-nums">
                      <span data-testid={`doc-suggested-${row.variant_id}`}>
                        {row.suggested_reorder_qty}
                      </span>
                    </td>
                    <td className="py-1.5">
                      <span
                        data-testid={`doc-risk-${row.variant_id}`}
                        className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[10.5px] font-semibold ${riskTone(row.risk_level)}`}
                      >
                        {bi(riskLevelLabelZh(row.risk_level), riskLevelLabel(row.risk_level))}
                      </span>
                    </td>
                  </tr>
                ))}
                {docRows.length === 0 && (
                  <tr>
                    <td
                      colSpan={8}
                      className="py-4 text-center text-[12px] font-medium text-gray-500 dark:text-gray-400"
                    >
                      {docQuery.isLoading
                        ? bi("正在计算可售天数…", "Computing days of cover…")
                        : bi("暂无在售 SKU。", "No sellable SKUs yet.")}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </TableWrap>

          {docRows.length === 0 && !docQuery.isLoading && (
            <EmptyState testId="doc-empty">
              {bi("没有可用于 DOC 计算的 SKU。", "No SKU is available for the DOC model yet.")}
            </EmptyState>
          )}
        </div>
      )}
    </Card>
  );
}
