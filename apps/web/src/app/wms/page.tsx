"use client";

import { useI18n } from "@/lib/i18n";

import React, { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowDownUp,
  Boxes,
  CheckCircle2,
  ExternalLink,
  PackageCheck,
  Plus,
  RefreshCw,
  ShoppingCart,
  TrendingUp,
  Warehouse,
  X,
} from "lucide-react";

import { AppShell } from "@/components/app-shell";
import { BentoStatCard } from "@/components/ui/bento-stat-card";
import { Page, PageHeader, TableWrap } from "@/components/ui/page";
import { useAuthSession } from "@/components/providers";
import type {
  CreateStocktake,
  InventorySyncDispatchSummary,
  LowStockItemRead,
  SkuVelocityItem,
  WarehouseCreateRequest,
} from "@/lib/api-client/types.gen";
import {
  createPoFromLowStock,
  createWmsWarehouse,
  fetchLowStockAlerts,
  fetchSkuVelocity,
  listInventorySyncLogs,
  retryInventorySyncJob,
  triggerInventorySyncNow,
} from "@/lib/api-client/pack-a-erp";
import { fetchPendingProposalsCount } from "@/lib/data-source";
import {
  addPhase3aStocktake,
  fetchPhase3aMovements,
  fetchPhase3aStock,
  fetchPhase3aSuppliers,
  fetchPhase3aVariants,
  fetchPhase3aWarehouses,
} from "@/lib/phase3a-data";

const OPERATOR_ROLES = new Set(["owner", "admin", "manager"]);

interface WarehouseFormState {
  code: string;
  name: string;
  country_code: string;
  city: string;
}

const EMPTY_WAREHOUSE_FORM: WarehouseFormState = {
  code: "",
  name: "",
  country_code: "CN",
  city: "",
};

/**
 * PageHeader / CardHeader 为共享原语（无 dark 变体），
 * 此处以任意变体在页面层补齐深色模式下的标题对比度。
 */
const PAGE_DARK_HEADER =
  "dark:[&_header_h1]:text-gray-100 dark:[&_header_p]:text-gray-400 dark:[&_.erp-card-header_h2]:text-gray-100 dark:[&_.erp-card-header_p]:text-gray-400";

export default function WmsPage() {
  const { bi } = useI18n();
  const queryClient = useQueryClient();
  const { principal } = useAuthSession();
  const [variantId, setVariantId] = useState("");
  const [delta, setDelta] = useState("1");
  const [notes, setNotes] = useState("");

  const [warehouseOpen, setWarehouseOpen] = useState(false);
  const [warehouseForm, setWarehouseForm] = useState<WarehouseFormState>(EMPTY_WAREHOUSE_FORM);
  const [warehouseError, setWarehouseError] = useState<string | null>(null);
  const [createdWarehouses, setCreatedWarehouses] = useState<WarehouseCreateRequest[]>([]);

  const [poItem, setPoItem] = useState<LowStockItemRead | null>(null);
  const [poSupplierId, setPoSupplierId] = useState("");
  const [poQuantity, setPoQuantity] = useState("");
  const [poUnitCost, setPoUnitCost] = useState("");
  const [createdPo, setCreatedPo] = useState<{ po_no: string; total_amount: string; currency: string } | null>(
    null
  );
  const [syncSummary, setSyncSummary] = useState<InventorySyncDispatchSummary | null>(null);

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
  const lowStockQuery = useQuery({
    queryKey: ["wms", "low-stock"],
    queryFn: () => fetchLowStockAlerts(),
  });
  const velocityQuery = useQuery({
    queryKey: ["wms", "sku-velocity"],
    queryFn: () => fetchSkuVelocity("30d", 50),
  });
  const syncLogsQuery = useQuery({
    queryKey: ["wms", "sync-logs"],
    queryFn: () => listInventorySyncLogs({ limit: 30, offset: 0 }),
  });
  const suppliersQuery = useQuery({
    queryKey: ["supply-chain", "suppliers", "picker"],
    queryFn: () => fetchPhase3aSuppliers(),
    enabled: canAdjust,
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

  const syncNowMutation = useMutation({
    mutationFn: () => triggerInventorySyncNow(),
    onSuccess: (res) => {
      setSyncSummary(res);
      void queryClient.invalidateQueries({ queryKey: ["wms", "sync-logs"] });
    },
  });

  const retrySyncMutation = useMutation({
    mutationFn: (jobId: string) => retryInventorySyncJob(jobId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["wms", "sync-logs"] });
    },
  });

  const movements = movementsQuery.data?.items ?? [];
  const stock = stockQuery.data?.items ?? [];
  const lowStockItems = lowStockQuery.data?.items ?? [];
  const velocityItems = velocityQuery.data?.items ?? [];
  const syncJobs = syncLogsQuery.data?.items ?? [];
  const suppliers = suppliersQuery.data?.items ?? [];

  const createWarehouseMutation = useMutation({
    mutationFn: (payload: WarehouseCreateRequest) => createWmsWarehouse(payload),
    onSuccess: (_created, payload) => {
      setCreatedWarehouses((prev) => [...prev, payload]);
      setWarehouseForm(EMPTY_WAREHOUSE_FORM);
      setWarehouseError(null);
      setWarehouseOpen(false);
      void queryClient.invalidateQueries({ queryKey: ["orders", "warehouses"] });
      void queryClient.invalidateQueries({ queryKey: ["wms"] });
    },
  });

  const createPoMutation = useMutation({
    mutationFn: (item: LowStockItemRead) =>
      createPoFromLowStock({
        supplier_id: poSupplierId,
        variant_id: item.variant_id,
        quantity: Number(poQuantity),
        unit_cost: poUnitCost.trim() || "0",
      }),
    onSuccess: (po) => {
      setCreatedPo({ po_no: po.po_no, total_amount: po.total_amount, currency: po.currency });
      setPoItem(null);
      setPoQuantity("");
      setPoUnitCost("");
      void queryClient.invalidateQueries({ queryKey: ["wms", "low-stock"] });
      void queryClient.invalidateQueries({ queryKey: ["supply-chain", "purchase-orders"] });
    },
  });

  /** 打开补货弹窗时预填建议补货量与供应商首个可选项。 */
  const openReplenishment = (item: LowStockItemRead, defaultCost?: string | null) => {
    const preferred = suppliers.find((supplier) => supplier.status === "active") ?? suppliers[0];
    setPoSupplierId(preferred?.id ?? "");
    setPoQuantity(String(Math.max(1, item.suggested_reorder_qty)));
    setPoUnitCost(defaultCost ?? "");
    setCreatedPo(null);
    setPoItem(item);
  };

  const openVelocityReplenishment = (item: SkuVelocityItem) => {
    const defaultWh =
      warehousesQuery.data?.default_warehouse_id ?? warehousesQuery.data?.items?.[0]?.id ?? "";
    const whName = bi("默认仓库", "Default warehouse");
    openReplenishment(
      {
        variant_id: item.variant_id,
        sku: item.sku,
        product_name: item.product_name,
        warehouse_id: defaultWh,
        warehouse_name: whName,
        on_hand_qty: item.available_qty,
        reserved_qty: 0,
        available_qty: item.available_qty,
        safety_stock_qty: item.lead_time_days,
        suggested_reorder_qty: Math.max(1, item.recommended_reorder_qty || item.lead_time_days * 14),
        sourcing_url: item.sourcing_url ?? null,
      },
      item.cost_price_base ?? null
    );
  };

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
      <Page className={PAGE_DARK_HEADER}>
        <PageHeader
          title={bi("WMS 出入库台账", "WMS movements")}
          description={bi(
            "流水来自采购收货、订单出库与盘点调整；人工仅可提交盘点调整，且不可占用已预占库存。",
            "Movements come from receiving, order dispatch and stocktakes; manual stocktake adjustments can never consume reserved stock."
          )}
          actions={
            canAdjust ? (
              <button
                type="button"
                data-testid="open-create-warehouse-btn"
                onClick={() => {
                  setWarehouseError(null);
                  setWarehouseOpen(true);
                }}
                className="erp-btn rounded-xl bg-blue-600 text-[13px] font-semibold text-white shadow-xs transition-colors hover:bg-blue-700"
              >
                <Plus className="h-3.5 w-3.5" />
                <span>{bi("新建仓库", "New warehouse")}</span>
              </button>
            ) : undefined
          }
        />

        {createdWarehouses.length > 0 && (
          <div
            data-testid="wms-created-warehouses"
            className="flex flex-wrap items-center gap-2 rounded-xl border border-emerald-200 bg-emerald-50 px-3.5 py-2.5 text-[12px] font-semibold text-emerald-900 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-200"
          >
            <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-600" />
            <span>{bi("本次已新建仓库：", "Warehouses created in this session:")}</span>
            {createdWarehouses.map((warehouse) => (
              <span
                key={warehouse.code}
                className="rounded-md border border-emerald-300 bg-white dark:bg-gray-900 px-1.5 py-0.5 font-mono text-[11px] dark:border-emerald-500/40 dark:text-emerald-300"
              >
                {warehouse.code} · {warehouse.name}
                {warehouse.city ? ` · ${warehouse.city}` : ""}
              </span>
            ))}
          </div>
        )}

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

        <section
          data-testid="wms-low-stock-card"
          className="erp-card overflow-hidden rounded-[20px]"
        >
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-gray-200 dark:border-gray-800 bg-gray-50/80 dark:bg-gray-800/50 px-3.5 py-2.5">
            <div className="flex items-center gap-2">
              <AlertTriangle
                className={`h-4 w-4 ${lowStockItems.length > 0 ? "text-rose-600" : "text-emerald-600"}`}
              />
              <h2 className="text-[13.5px] font-semibold text-gray-900 dark:text-gray-100">
                {bi("低库存补货预警", "Low-stock replenishment alerts")}
              </h2>
              <span className="rounded-full border border-rose-200 bg-rose-50 px-2 py-0.5 text-[11px] font-semibold tabular-nums text-rose-700 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-300">
                {lowStockItems.length} {bi("项待补", "to restock")}
              </span>
            </div>
            <span className="text-[11.5px] font-medium text-gray-500 dark:text-gray-400">
              {bi(
                "可用库存低于安全库存阈值时自动列出，建议补货量 = max(安全库存 × 2 − 可用, 安全库存)",
                "Listed automatically when available stock drops below its safety threshold; suggested qty = max(safety × 2 − available, safety)"
              )}
            </span>
          </div>

          {createdPo && (
            <div
              data-testid="low-stock-po-created"
              className="flex flex-wrap items-center gap-2 border-b border-emerald-200 bg-emerald-50 px-3.5 py-2.5 text-[12px] font-semibold text-emerald-900 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-200"
            >
              <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-600" />
              <span>
                {bi("已生成采购单", "Purchase order created")}{" "}
                <span className="font-mono">{createdPo.po_no}</span>{" · "}
                <span className="tabular-nums">
                  {createdPo.currency} {createdPo.total_amount}
                </span>
              </span>
              <a
                data-testid="low-stock-po-link"
                href="/supply-chain/purchase-orders"
                className="ml-auto inline-flex items-center gap-1 rounded-lg border border-emerald-300 bg-white dark:bg-gray-900 px-2 py-1 text-[11.5px] font-semibold text-emerald-800 transition-colors hover:bg-emerald-100 dark:border-emerald-500/40 dark:text-emerald-300 dark:hover:bg-emerald-500/20"
              >
                <ShoppingCart className="h-3 w-3" />
                <span>{bi("去采购单验收", "Open purchase orders")}</span>
              </a>
            </div>
          )}

          <table data-testid="wms-low-stock-table" className="erp-table">
            <thead>
              <tr>
                <th>{bi("SKU 与商品", "SKU & product")}</th>
                <th>{bi("仓库", "Warehouse")}</th>
                <th className="text-right">{bi("可用 / 安全库存", "Available / safety")}</th>
                <th className="text-right">{bi("建议补货量", "Suggested qty")}</th>
                {canAdjust && <th className="text-right">{bi("操作", "Action")}</th>}
              </tr>
            </thead>
            <tbody>
              {lowStockItems.map((item) => (
                <tr key={`${item.warehouse_id}-${item.variant_id}`}>
                  <td className="py-2">
                    <div className="font-mono text-[12px] font-semibold text-gray-900 dark:text-gray-100">{item.sku}</div>
                    <div className="flex flex-wrap items-center gap-1.5 text-[11px] font-medium text-gray-500 dark:text-gray-400">
                      <span>{item.product_name}</span>
                      {item.sourcing_url && (
                        <a
                          href={item.sourcing_url}
                          target="_blank"
                          rel="noreferrer"
                          className="inline-flex items-center gap-0.5 text-orange-600 hover:underline dark:text-orange-400"
                        >
                          <span>1688</span>
                          <ExternalLink className="h-3 w-3" />
                        </a>
                      )}
                    </div>
                  </td>
                  <td className="py-2 text-gray-600 dark:text-gray-400">{item.warehouse_name}</td>
                  <td className="py-2 text-right">
                    <span
                      data-testid={`low-stock-badge-${item.variant_id}`}
                      className="inline-flex items-center gap-1 rounded-full border border-rose-200 bg-rose-50 px-2 py-0.5 text-[11px] font-semibold tabular-nums text-rose-700 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-300"
                    >
                      {item.available_qty} / {item.safety_stock_qty}
                    </span>
                  </td>
                  <td className="py-2 text-right font-semibold tabular-nums text-gray-900 dark:text-gray-100">
                    {item.suggested_reorder_qty}
                  </td>
                  {canAdjust && (
                    <td className="py-2 text-right">
                      <button
                        type="button"
                        data-testid={`low-stock-create-po-${item.variant_id}`}
                        onClick={() => openReplenishment(item)}
                        className="erp-btn erp-btn-secondary rounded-xl py-1 text-[12px]"
                      >
                        <ShoppingCart className="h-3 w-3" />
                        <span>{bi("一键转采购单", "Create purchase order")}</span>
                      </button>
                    </td>
                  )}
                </tr>
              ))}
              {lowStockItems.length === 0 && (
                <tr>
                  <td
                    colSpan={canAdjust ? 5 : 4}
                    className="py-4 text-center text-[12px] font-medium text-gray-500 dark:text-gray-400"
                  >
                    {lowStockQuery.isLoading
                      ? bi("正在检查安全库存…", "Checking safety stock…")
                      : bi("全部 SKU 均高于安全库存阈值。", "Every SKU is above its safety stock.")}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </section>

        <section
          data-testid="wms-sku-velocity-card"
          className="erp-card overflow-hidden rounded-[20px]"
        >
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-gray-200 dark:border-gray-800 bg-gray-50/80 dark:bg-gray-800/50 px-3.5 py-2.5">
            <div className="flex items-center gap-2">
              <TrendingUp className="h-4 w-4 text-emerald-600" />
              <h2 className="text-[13.5px] font-semibold text-gray-900 dark:text-gray-100">
                {bi(
                  "SKU 跨平台动销、可售天数预警 & 1688 一键补货",
                  "Cross-channel SKU velocity, days-of-cover & 1688 replenishment"
                )}
              </h2>
            </div>
            <span className="text-[11.5px] font-medium text-gray-500 dark:text-gray-400">
              {bi(
                "近 30 天全平台合并日均销量 ÷ 可用库存 = 可售天数；自动关联 1688 选品链接",
                "30d cross-channel daily sales vs available stock; auto-linked 1688 sourcing URLs"
              )}
            </span>
          </div>

          <table data-testid="wms-sku-velocity-table" className="erp-table">
            <thead>
              <tr>
                <th>{bi("SKU 与货源", "SKU & 1688 source")}</th>
                <th>{bi("平台销量构成", "Channel split")}</th>
                <th className="text-right">{bi("30 天销量 / 日均", "30d / daily")}</th>
                <th className="text-right">{bi("可用 / 可售天数", "Avail / cover")}</th>
                <th>{bi("断货风险", "Risk")}</th>
                <th className="text-right">{bi("建议补货", "Reorder")}</th>
                {canAdjust && <th className="text-right">{bi("操作", "Action")}</th>}
              </tr>
            </thead>
            <tbody>
              {velocityItems.map((item) => {
                const riskTone =
                  item.stockout_risk === "out_of_stock" || item.stockout_risk === "critical"
                    ? "border-rose-200 bg-rose-50 text-rose-700 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-300"
                    : item.stockout_risk === "warning"
                      ? "border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-300"
                      : "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-300";
                return (
                  <tr key={item.variant_id} data-testid={`velocity-row-${item.variant_id}`}>
                    <td className="py-2">
                      <div className="font-mono text-[12px] font-semibold text-gray-900 dark:text-gray-100">
                        {item.sku}
                      </div>
                      <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-gray-500 dark:text-gray-400">
                        <span>{item.product_name}</span>
                        {item.sourcing_url && (
                          <a
                            href={item.sourcing_url}
                            target="_blank"
                            rel="noreferrer"
                            data-testid={`velocity-1688-link-${item.variant_id}`}
                            className="inline-flex items-center gap-0.5 font-semibold text-orange-600 hover:underline dark:text-orange-400"
                          >
                            <span>1688</span>
                            <ExternalLink className="h-3 w-3" />
                          </a>
                        )}
                      </div>
                    </td>
                    <td className="py-2">
                      <div className="flex flex-wrap gap-1">
                        {item.channel_breakdown.map((ch, idx) => (
                          <span
                            key={`${ch.channel_type}-${idx}`}
                            className="rounded-md bg-gray-100 px-1.5 py-0.5 text-[10.5px] font-medium text-gray-700 dark:bg-gray-800 dark:text-gray-300"
                          >
                            {ch.channel_label}: {ch.units_sold}
                          </span>
                        ))}
                        {item.channel_breakdown.length === 0 && (
                          <span className="text-[11px] text-gray-400">—</span>
                        )}
                      </div>
                    </td>
                    <td className="py-2 text-right font-mono text-[12px] tabular-nums">
                      {item.total_units_sold} / {item.avg_daily_units}
                    </td>
                    <td className="py-2 text-right font-mono text-[12px] tabular-nums">
                      {item.available_qty} /{" "}
                      {item.days_of_cover !== null && item.days_of_cover !== undefined
                        ? `${item.days_of_cover}d`
                        : "∞"}
                    </td>
                    <td className="py-2">
                      <span
                        data-testid={`velocity-risk-${item.variant_id}`}
                        className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[10.5px] font-semibold ${riskTone}`}
                      >
                        {item.stockout_risk}
                      </span>
                    </td>
                    <td className="py-2 text-right font-mono text-[12px] font-semibold tabular-nums">
                      {item.recommended_reorder_qty}
                    </td>
                    {canAdjust && (
                      <td className="py-2 text-right">
                        <button
                          type="button"
                          data-testid={`velocity-create-po-${item.variant_id}`}
                          onClick={() => openVelocityReplenishment(item)}
                          className="erp-btn erp-btn-secondary rounded-xl py-1 text-[11.5px]"
                        >
                          <ShoppingCart className="h-3 w-3" />
                          <span>{bi("1688 一键补货", "1688 Restock")}</span>
                        </button>
                      </td>
                    )}
                  </tr>
                );
              })}
              {velocityItems.length === 0 && (
                <tr>
                  <td
                    colSpan={canAdjust ? 7 : 6}
                    className="py-4 text-center text-[12px] font-medium text-gray-500 dark:text-gray-400"
                  >
                    {velocityQuery.isLoading
                      ? bi("正在计算跨平台动销速度…", "Calculating SKU velocity…")
                      : bi("暂无 SKU 动销记录。", "No SKU velocity data yet.")}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </section>

        <section
          data-testid="wms-inventory-sync-card"
          className="erp-card overflow-hidden rounded-[20px]"
        >
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-gray-200 dark:border-gray-800 bg-gray-50/80 dark:bg-gray-800/50 px-3.5 py-2.5">
            <div className="flex items-center gap-2">
              <RefreshCw className="h-4 w-4 text-blue-600" />
              <h2 className="text-[13.5px] font-semibold text-gray-900 dark:text-gray-100">
                {bi("全平台实时库存同步", "Multi-platform real-time inventory sync")}
              </h2>
              <span className="rounded-full border border-blue-200 bg-blue-50 px-2 py-0.5 text-[11px] font-semibold tabular-nums text-blue-700 dark:border-blue-500/30 dark:bg-blue-500/10 dark:text-blue-300">
                {syncJobs.length} {bi("条日志", "jobs")}
              </span>
            </div>
            {canAdjust && (
              <button
                type="button"
                data-testid="wms-sync-now-btn"
                disabled={syncNowMutation.isPending}
                onClick={() => syncNowMutation.mutate()}
                className="erp-btn rounded-xl bg-blue-600 px-3 py-1.5 text-[12px] font-semibold text-white shadow-xs transition-colors hover:bg-blue-700 disabled:opacity-40"
              >
                <RefreshCw
                  className={`h-3.5 w-3.5 ${syncNowMutation.isPending ? "animate-spin" : ""}`}
                />
                <span>
                  {syncNowMutation.isPending
                    ? bi("正在同步…", "Syncing…")
                    : bi("一键同步全平台库存", "Sync all channels now")}
                </span>
              </button>
            )}
          </div>

          {syncSummary && (
            <div
              data-testid="wms-sync-summary-banner"
              className="flex flex-wrap items-center gap-2 border-b border-blue-200 bg-blue-50/80 px-3.5 py-2 text-[12px] font-medium text-blue-900 dark:border-blue-500/30 dark:bg-blue-500/10 dark:text-blue-200"
            >
              <span>
                {bi(
                  `本次已触发 ${syncSummary.enqueued ?? 0} 个渠道映射：成功 ${syncSummary.succeeded ?? 0}，重试中 ${syncSummary.failed ?? 0}，死信 ${syncSummary.dead_letter ?? 0}`,
                  `Triggered ${syncSummary.enqueued ?? 0} mappings: ${syncSummary.succeeded ?? 0} succeeded, ${syncSummary.failed ?? 0} retrying, ${syncSummary.dead_letter ?? 0} dead-letter`
                )}
              </span>
            </div>
          )}

          {syncNowMutation.isError && (
            <p
              role="alert"
              className="border-b border-rose-200 bg-rose-50 px-3.5 py-2 text-[12px] text-rose-700"
            >
              {syncNowMutation.error.message}
            </p>
          )}

          <table data-testid="wms-sync-logs-table" className="erp-table">
            <thead>
              <tr>
                <th>{bi("渠道与店铺", "Channel")}</th>
                <th>{bi("平台 SKU / 主档 SKU", "External / master SKU")}</th>
                <th className="text-right">{bi("目标 / 已同步", "Target / synced")}</th>
                <th>{bi("状态与触发源", "Status")}</th>
                <th>{bi("错误明细", "Error")}</th>
                {canAdjust && <th className="text-right">{bi("操作", "Action")}</th>}
              </tr>
            </thead>
            <tbody>
              {syncJobs.map((job) => {
                const canRetry =
                  canAdjust && (job.status === "failed" || job.status === "dead_letter");
                const statusTone =
                  job.status === "succeeded"
                    ? "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-300"
                    : job.status === "failed" || job.status === "dead_letter"
                      ? "border-rose-200 bg-rose-50 text-rose-700 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-300"
                      : "border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-300";
                return (
                  <tr key={job.id} data-testid={`sync-job-row-${job.id}`}>
                    <td className="py-2">
                      <div className="text-[12px] font-semibold text-gray-900 dark:text-gray-100">
                        {job.channel_label ?? job.channel_type}
                      </div>
                      <div className="font-mono text-[11px] text-gray-500 dark:text-gray-400">
                        {job.channel_type}
                      </div>
                    </td>
                    <td className="py-2">
                      <div className="font-mono text-[12px] font-semibold text-gray-900 dark:text-gray-100">
                        {job.external_sku}
                      </div>
                      {job.internal_sku && (
                        <div className="font-mono text-[11px] text-gray-500 dark:text-gray-400">
                          {job.internal_sku}
                        </div>
                      )}
                    </td>
                    <td className="py-2 text-right font-mono text-[12px] tabular-nums">
                      {job.requested_qty} / {job.synced_qty ?? "—"}
                    </td>
                    <td className="py-2">
                      <span
                        data-testid={`sync-job-status-${job.id}`}
                        className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-semibold ${statusTone}`}
                      >
                        {job.status}
                      </span>
                      <div className="mt-0.5 text-[10.5px] text-gray-400">
                        {job.trigger_source} · #{job.attempts}
                      </div>
                    </td>
                    <td className="max-w-xs py-2 text-[11px] text-rose-700 dark:text-rose-300">
                      {job.error_code ? `${job.error_code}: ${job.error_detail ?? ""}` : "—"}
                    </td>
                    {canAdjust && (
                      <td className="py-2 text-right">
                        {canRetry ? (
                          <button
                            type="button"
                            data-testid={`retry-sync-job-${job.id}`}
                            disabled={retrySyncMutation.isPending}
                            onClick={() => retrySyncMutation.mutate(job.id)}
                            className="erp-btn erp-btn-secondary rounded-xl py-1 text-[11.5px]"
                          >
                            {bi("重试推送", "Retry")}
                          </button>
                        ) : (
                          <span className="text-[11px] text-gray-400">—</span>
                        )}
                      </td>
                    )}
                  </tr>
                );
              })}
              {syncJobs.length === 0 && (
                <tr>
                  <td
                    colSpan={canAdjust ? 6 : 5}
                    className="py-4 text-center text-[12px] font-medium text-gray-500 dark:text-gray-400"
                  >
                    {syncLogsQuery.isLoading
                      ? bi("加载库存同步日志…", "Loading inventory sync logs…")
                      : bi("暂无多平台库存同步记录；库存变动或点击上方按钮将自动触发。", "No channel sync jobs yet; stock changes or Sync Now will populate this log.")}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </section>

        <TableWrap className="rounded-2xl border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-900">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-gray-200 dark:border-gray-800 bg-gray-50/80 dark:bg-gray-800/50 px-3.5 py-2.5">
            <h2 className="text-[13.5px] font-semibold text-gray-900 dark:text-gray-100">
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
                    <span className="font-mono text-[12px] font-semibold text-gray-900 dark:text-gray-100">
                      {row.sku}
                    </span>
                  </td>
                  <td className="py-1.5 text-gray-600 dark:text-gray-400">
                    <span className="rounded-md bg-gray-100 dark:bg-gray-800 px-1.5 py-0.5 font-mono text-[11px]">
                      {row.warehouse_id.slice(0, 8)}
                    </span>
                  </td>
                  <td className="py-1.5 tabular-nums text-gray-800 dark:text-gray-200">{row.on_hand_qty}</td>
                  <td className="py-1.5 tabular-nums text-amber-800">{row.reserved_qty}</td>
                  <td className="py-1.5 font-semibold tabular-nums text-emerald-700">
                    {row.available_qty}
                  </td>
                </tr>
              ))}
              {stock.length === 0 && (
                <tr>
                  <td colSpan={5} className="py-4 text-center font-medium text-gray-500 dark:text-gray-400">
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
              className="rounded border border-gray-300 dark:border-gray-700 px-2 py-1 text-xs"
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
              className="rounded border border-gray-300 dark:border-gray-700 px-2 py-1 text-xs"
            />
            <input
              aria-label="Stocktake notes"
              value={notes}
              onChange={(event) => setNotes(event.target.value)}
              placeholder="盘点说明"
              className="rounded border border-gray-300 dark:border-gray-700 px-2 py-1 text-xs"
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
            className="rounded-xl border border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-800/60 p-3 text-[12px] font-medium text-gray-700 dark:text-gray-300"
          >
            {bi(`当前角色（${principal?.role}）仅可查看流水与库存，盘点调整需要 Owner / Admin / Manager。`, `Your role (${principal?.role}) can only view movements and stock; stocktakes require Owner / Admin / Manager.`)}
          </p>
        )}

        <TableWrap className="rounded-2xl border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-900">
          <table data-testid="wms-movements-table" className="erp-table">
            <thead className="border-b border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-800/60 font-semibold text-gray-600 dark:text-gray-400">
              <tr>
                <th>{bi("流水号", "Movement no.")}</th>
                <th>{bi("类型", "Type")}</th>
                <th className="text-right">{bi("数量", "Qty")}</th>
                <th>{bi("关联单据", "Reference")}</th>
                <th>{bi("时间", "Time")}</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100 dark:divide-gray-800">
              {movements.map((movement) => (
                <tr key={movement.id} data-testid={`movement-row-${movement.id}`}>
                  <td className="px-3 py-2">
                    <span className="font-mono text-[12px] font-semibold text-gray-900 dark:text-gray-100">
                      {movement.movement_no}
                    </span>
                  </td>
                  <td className="px-3 py-2">
                    <span className="rounded-md bg-gray-100 dark:bg-gray-800 px-1.5 py-0.5 text-[11px] font-medium text-gray-700 dark:text-gray-300">
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
                  <td className="px-3 py-2 text-gray-500 dark:text-gray-400">
                    {movement.reference_type ?? "—"}
                  </td>
                  <td className="px-3 py-2 text-gray-500 dark:text-gray-400">
                    {new Date(movement.created_at).toLocaleString()}
                  </td>
                </tr>
              ))}
              {movements.length === 0 && (
                <tr>
                  <td colSpan={5} className="px-3 py-6 text-center text-gray-400 dark:text-gray-500">
                    {movementsQuery.isLoading ? bi("加载中…", "Loading…") : bi("暂无流水", "No movements")}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </TableWrap>

        {warehouseOpen && (
          <Overlay testId="create-warehouse-modal" labelledBy="create-warehouse-title">
            <form
              onSubmit={(event) => {
                event.preventDefault();
                if (!warehouseForm.code.trim() || !warehouseForm.name.trim()) {
                  setWarehouseError(bi("仓库编码与名称为必填项。", "Warehouse code and name are required."));
                  return;
                }
                if (!/^[A-Za-z]{2}$/.test(warehouseForm.country_code.trim())) {
                  setWarehouseError(bi("国家代码必须是两位字母。", "Country code must be two letters."));
                  return;
                }
                setWarehouseError(null);
                createWarehouseMutation.mutate({
                  code: warehouseForm.code.trim(),
                  name: warehouseForm.name.trim(),
                  country_code: warehouseForm.country_code.trim().toUpperCase(),
                  city: warehouseForm.city.trim(),
                });
              }}
              className="space-y-3.5"
            >
              <div className="flex items-start justify-between gap-3 border-b border-gray-200 dark:border-gray-800 pb-3">
                <div className="min-w-0">
                  <h3 id="create-warehouse-title" className="text-[15px] font-bold text-gray-900 dark:text-gray-100">
                    {bi("新建仓库", "New warehouse")}
                  </h3>
                  <p className="mt-1 text-[12px] leading-5 text-gray-600 dark:text-gray-400">
                    {bi(
                      "仓库编码唯一且用于采购单与库存台账；建完仓库才能把低库存 SKU 转成采购单。",
                      "The warehouse code is unique and used by purchase orders and the inventory ledger. Replenishment requires at least one warehouse."
                    )}
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => setWarehouseOpen(false)}
                  aria-label={bi("关闭新建仓库弹窗", "Close the new warehouse dialog")}
                  className="rounded-lg border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 p-1.5 text-gray-600 dark:text-gray-400 shadow-xs transition-colors hover:bg-gray-100 dark:hover:bg-gray-800"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>

              <div className="grid gap-2.5 sm:grid-cols-2">
                <FormField
                  testId="warehouse-form-code"
                  label={bi("仓库编码", "Warehouse code")}
                  required
                  value={warehouseForm.code}
                  onChange={(value) => setWarehouseForm((prev) => ({ ...prev, code: value }))}
                  placeholder="SZ-BONDED-01"
                />
                <FormField
                  testId="warehouse-form-name"
                  label={bi("仓库名称", "Warehouse name")}
                  required
                  value={warehouseForm.name}
                  onChange={(value) => setWarehouseForm((prev) => ({ ...prev, name: value }))}
                  placeholder="Shenzhen Bonded Warehouse"
                />
                <FormField
                  testId="warehouse-form-country"
                  label={bi("国家代码", "Country code")}
                  value={warehouseForm.country_code}
                  onChange={(value) => setWarehouseForm((prev) => ({ ...prev, country_code: value }))}
                  placeholder="CN"
                  maxLength={2}
                />
                <FormField
                  testId="warehouse-form-city"
                  label={bi("城市", "City")}
                  value={warehouseForm.city}
                  onChange={(value) => setWarehouseForm((prev) => ({ ...prev, city: value }))}
                  placeholder="Shenzhen"
                />
              </div>

              {(warehouseError || createWarehouseMutation.isError) && (
                <p
                  role="alert"
                  data-testid="create-warehouse-error"
                  className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-[12px] font-medium text-rose-800 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-300"
                >
                  {warehouseError ?? createWarehouseMutation.error?.message}
                </p>
              )}

              <div className="flex items-center justify-end gap-2 border-t border-gray-200 dark:border-gray-800 pt-3">
                <button
                  type="button"
                  onClick={() => setWarehouseOpen(false)}
                  className="erp-btn erp-btn-secondary rounded-xl"
                >
                  {bi("取消", "Cancel")}
                </button>
                <button
                  type="submit"
                  data-testid="warehouse-submit"
                  disabled={createWarehouseMutation.isPending}
                  className="erp-btn rounded-xl bg-blue-600 font-semibold text-white shadow-xs transition-colors hover:bg-blue-700 disabled:opacity-40"
                >
                  {createWarehouseMutation.isPending
                    ? bi("正在创建…", "Creating…")
                    : bi("创建仓库", "Create warehouse")}
                </button>
              </div>
            </form>
          </Overlay>
        )}

        {poItem && (
          <Overlay testId="low-stock-po-modal" labelledBy="low-stock-po-title">
            <form
              onSubmit={(event) => {
                event.preventDefault();
                createPoMutation.mutate(poItem);
              }}
              className="space-y-3.5"
            >
              <div className="flex items-start justify-between gap-3 border-b border-gray-200 dark:border-gray-800 pb-3">
                <div className="min-w-0">
                  <h3 id="low-stock-po-title" className="text-[15px] font-bold text-gray-900 dark:text-gray-100">
                    {bi("一键转采购单", "Create purchase order")}
                  </h3>
                  <p className="mt-1 text-[12px] leading-5 text-gray-600 dark:text-gray-400">
                    {bi(
                      "生成的采购单为草稿状态，供应商确认单价后可在采购单页提交并收货入库。",
                      "The generated purchase order starts as a draft. Confirm the unit cost with the supplier, then submit and receive it from the purchase-order page."
                    )}
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => setPoItem(null)}
                  aria-label={bi("关闭补货弹窗", "Close the replenishment dialog")}
                  className="rounded-lg border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 p-1.5 text-gray-600 dark:text-gray-400 shadow-xs transition-colors hover:bg-gray-100 dark:hover:bg-gray-800"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>

              <div className="rounded-xl border border-rose-200 bg-rose-50 px-3.5 py-2.5 text-[12px] font-semibold text-rose-800 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-300">
                <span className="font-mono">{poItem.sku}</span>
                {" · "}
                {poItem.product_name}
                {" · "}
                <span className="tabular-nums">
                  {bi("可用", "Available")} {poItem.available_qty} / {bi("安全", "Safety")}{" "}
                  {poItem.safety_stock_qty}
                </span>
              </div>

              <div className="grid gap-2.5 sm:grid-cols-2">
                <label className="block sm:col-span-2">
                  <span className="mb-1 flex items-center gap-1 text-[11.5px] font-semibold text-gray-700 dark:text-gray-300">
                    {bi("供应商", "Supplier")}
                    <span className="text-rose-600">*</span>
                  </span>
                  <select
                    data-testid="low-stock-po-supplier"
                    value={poSupplierId}
                    onChange={(event) => setPoSupplierId(event.target.value)}
                    className="erp-input w-full rounded-xl"
                  >
                    <option value="">{bi("请选择供应商", "Select a supplier")}</option>
                    {suppliers.map((supplier) => (
                      <option key={supplier.id} value={supplier.id}>
                        {supplier.code} · {supplier.name}
                      </option>
                    ))}
                  </select>
                </label>
                <FormField
                  testId="low-stock-po-quantity"
                  label={bi("采购数量", "Order quantity")}
                  required
                  type="number"
                  min={1}
                  value={poQuantity}
                  onChange={setPoQuantity}
                />
                <FormField
                  testId="low-stock-po-unit-cost"
                  label={bi("单价（USD）", "Unit cost (USD)")}
                  required
                  type="number"
                  step="0.01"
                  value={poUnitCost}
                  onChange={setPoUnitCost}
                  placeholder="14.20"
                />
              </div>

              {createPoMutation.isError && (
                <p
                  role="alert"
                  className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-[12px] font-medium text-rose-800 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-300"
                >
                  {createPoMutation.error.message}
                </p>
              )}

              <div className="flex items-center justify-end gap-2 border-t border-gray-200 dark:border-gray-800 pt-3">
                <button
                  type="button"
                  onClick={() => setPoItem(null)}
                  className="erp-btn erp-btn-secondary rounded-xl"
                >
                  {bi("取消", "Cancel")}
                </button>
                <button
                  type="submit"
                  data-testid="low-stock-po-submit"
                  disabled={
                    createPoMutation.isPending ||
                    !poSupplierId ||
                    !(Number(poQuantity) > 0) ||
                    !(Number(poUnitCost) >= 0) ||
                    poUnitCost.trim().length === 0
                  }
                  className="erp-btn rounded-xl bg-blue-600 font-semibold text-white shadow-xs transition-colors hover:bg-blue-700 disabled:opacity-40"
                >
                  {createPoMutation.isPending
                    ? bi("正在生成…", "Creating…")
                    : bi("生成采购单", "Create purchase order")}
                </button>
              </div>
            </form>
          </Overlay>
        )}
      </Page>
    </AppShell>
  );
}

/** 居中遮罩弹窗壳：与商品主档新建 / CSV 导入保持同一套命令中心风格。 */
function Overlay({
  testId,
  labelledBy,
  children,
}: {
  testId: string;
  labelledBy: string;
  children: React.ReactNode;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-slate-900/40 p-4 backdrop-blur-sm">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={labelledBy}
        data-testid={testId}
        className="erp-card my-6 w-full max-w-xl rounded-[20px] p-4 shadow-2xl"
      >
        {children}
      </div>
    </div>
  );
}

function FormField({
  testId,
  label,
  value,
  onChange,
  required = false,
  ...input
}: {
  testId: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  required?: boolean;
} & Omit<React.InputHTMLAttributes<HTMLInputElement>, "value" | "onChange">) {
  return (
    <label className="block">
      <span className="mb-1 flex items-center gap-1 text-[11.5px] font-semibold text-gray-700 dark:text-gray-300">
        {label}
        {required && <span className="text-rose-600">*</span>}
      </span>
      <input
        {...input}
        data-testid={testId}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="erp-input w-full rounded-xl tabular-nums"
      />
    </label>
  );
}

function uuid(): string {
  const cryptoRef = globalThis.crypto;
  if (cryptoRef && typeof cryptoRef.randomUUID === "function") return cryptoRef.randomUUID();
  const hex = [...Array(32)].map(() => Math.floor(Math.random() * 16).toString(16)).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-4${hex.slice(13, 16)}-a${hex.slice(17, 20)}-${hex.slice(20, 32)}`;
}
