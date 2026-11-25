"use client";

import React, { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowRightLeft,
  CheckCircle2,
  CircleDollarSign,
  FileSpreadsheet,
  FileText,
  Lock,
  PackageCheck,
  RefreshCw,
  Truck,
} from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { ShipmentTimelineDrawer } from "@/components/orders/shipment-timeline-drawer";
import { BentoStatCard } from "@/components/ui/bento-stat-card";
import { Badge, EmptyState, Page, PageHeader, TableWrap } from "@/components/ui/page";
import { pushOrderFulfillment, shipOrder } from "@/lib/api-client/live-client";
import {
  commercialInvoicePdfUrl,
  packingListPdfUrl,
  type ExportDocumentLanguage,
} from "@/lib/api-client/pack-a-erp";
import { fetchOrders, fetchPendingProposalsCount } from "@/lib/data-source";
import { useI18n } from "@/lib/i18n";
import { formatMoney, type OrderMode } from "@/lib/contracts";

const CHANNEL_LABEL: Record<string, string> = {
  shopify: "Shopify",
  woocommerce: "WooCommerce",
  shopline: "SHOPLINE",
  shoplazza: "Shoplazza",
  tiktok_shop: "TikTok Shop",
  amazon_sp: "Amazon",
  ebay: "eBay",
  alibaba_email: "Alibaba.com",
  shopee: "Shopee",
  lazada: "Lazada",
  temu: "Temu",
  shein: "SHEIN",
  aliexpress: "AliExpress",
  ozon: "Ozon",
  walmart: "Walmart",
  mercado_libre: "Mercado Libre",
};

const CARRIER_OPTIONS = [
  "DHL",
  "FedEx",
  "UPS",
  "USPS",
  "YunExpress",
  "4PX",
  "SF Express",
  "Other",
];

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

/** 履约状态圆点：与 21st.dev Data Grid 状态胶囊保持一致语义。 */
const FULFILLMENT_DOT: Record<string, string> = {
  pending_payment: "bg-amber-500",
  confirmed_reserved: "bg-blue-500",
  in_production: "bg-violet-500",
  partially_shipped: "bg-cyan-500",
  shipped: "bg-emerald-500",
  delivered: "bg-emerald-600",
  exception: "bg-rose-500",
  cancelled: "bg-slate-400",
  refunded: "bg-slate-400",
};

/**
 * PageHeader / CardHeader 为共享原语（无 dark 变体），
 * 此处以任意变体在页面层补齐深色模式下的标题对比度。
 */
const PAGE_DARK_HEADER =
  "dark:[&_header_h1]:text-gray-100 dark:[&_header_p]:text-gray-400 dark:[&_.erp-card-header_h2]:text-gray-100 dark:[&_.erp-card-header_p]:text-gray-400";

export default function OrdersPage() {
  const queryClient = useQueryClient();
  const { bi } = useI18n();
  const [modeFilter, setModeFilter] = useState<"" | OrderMode>("");
  const [drawerOrderId, setDrawerOrderId] = useState<string | null>(null);
  const [trackingInputs, setTrackingInputs] = useState<Record<string, string>>({});
  const [carrierInputs, setCarrierInputs] = useState<Record<string, string>>({});
  const [providerInputs, setProviderInputs] = useState<Record<string, "manual" | "17track">>({});
  // Export documents are rendered server-side from the locked contract, so the
  // language pill only swaps the `?language=` query of the same PDF route.
  const [pdfLanguage, setPdfLanguage] = useState<ExportDocumentLanguage>("en");

  const pendingProposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () => fetchPendingProposalsCount(),
  });

  const ordersQuery = useQuery({
    queryKey: ["orders", modeFilter],
    queryFn: () => fetchOrders(modeFilter),
  });

  const shipMutation = useMutation({
    mutationFn: ({
      orderId,
      trackingNumber,
      carrierCode,
      trackingProvider,
    }: {
      orderId: string;
      trackingNumber: string;
      carrierCode: string;
      trackingProvider: "manual" | "17track";
    }) =>
      shipOrder(orderId, {
        trackingNumber,
        carrierCode,
        shipmentMode: "parcel",
        trackingProvider,
        autoResolveWarehouse: true,
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["orders"] });
    },
  });

  const pushFulfillmentMutation = useMutation({
    mutationFn: (orderId: string) => pushOrderFulfillment(orderId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["orders"] });
    },
  });

  const orders = ordersQuery.data?.items ?? [];

  const summary = useMemo(() => {
    const blocked = orders.filter(
      (o) => o.order_mode === "b2b_wholesale" && o.payment_status !== "paid"
    ).length;
    const shipped = orders.filter(
      (o) => o.fulfillment_status === "shipped" || o.fulfillment_status === "delivered"
    ).length;
    const alerts = orders.filter((o) => o.has_72h_stagnation_alert).length;
    const value = orders.reduce((total, o) => total + Number(o.total_amount || 0), 0);
    return { blocked, shipped, alerts, value };
  }, [orders]);

  return (
    <AppShell
      currentPath="/orders"
      pendingProposalsCount={pendingProposalsQuery.data?.total ?? 0}
    >
      <Page className={PAGE_DARK_HEADER}>
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
              className="erp-input w-auto rounded-xl"
            >
              <option value="">{bi("全部订单模式", "All order modes")}</option>
              <option value="b2c_retail">{bi("B2C 零售", "B2C retail")}</option>
              <option value="b2b_sample">{bi("B2B 样品", "B2B sample")}</option>
              <option value="b2b_wholesale">{bi("B2B 批发", "B2B wholesale")}</option>
            </select>
          }
        />

        <section
          aria-label={bi("订单关键指标", "Order key metrics")}
          className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"
        >
          <BentoStatCard
            label={bi("订单总数", "Total orders")}
            value={orders.length}
            icon={PackageCheck}
            accent="blue"
            hint={bi("B2C 零售与 B2B 批发合并统计", "B2C retail and B2B wholesale combined")}
          />
          <BentoStatCard
            label={bi("订单金额", "Order value")}
            value={formatMoney(summary.value.toFixed(2), "USD")}
            icon={CircleDollarSign}
            accent="violet"
            hint={bi("按订单币种折算前合计", "Summed before currency conversion")}
          />
          <BentoStatCard
            label={bi("尾款未结清", "Balance unpaid")}
            value={summary.blocked}
            icon={Lock}
            accent="amber"
            badge={summary.blocked > 0 ? bi("已拦截出库", "Dispatch blocked") : undefined}
            badgeTone="amber"
            hint={bi("B2B 批发 70% 尾款未结清不出货", "Wholesale ships only after the 70% balance")}
          />
          <BentoStatCard
            label={bi("物流异常预警", "Logistics alerts")}
            value={summary.alerts}
            icon={AlertTriangle}
            accent={summary.alerts > 0 ? "rose" : "emerald"}
            badge={`${summary.shipped} ${bi("已出运", "shipped")}`}
            badgeTone="emerald"
            hint={bi("72 小时无轨迹更新自动预警", "Auto-alerts after 72h without tracking updates")}
          />
        </section>

        {(shipMutation.isError || pushFulfillmentMutation.isError) && (
          <p
            role="alert"
            className="rounded-xl border border-rose-200 bg-rose-50 p-2.5 text-[12px] font-medium text-rose-800 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-300"
          >
            {shipMutation.error?.message ?? pushFulfillmentMutation.error?.message}
          </p>
        )}

        {orders.length === 0 ? (
          <EmptyState>
            {ordersQuery.isLoading
              ? bi("正在加载订单…", "Loading orders…")
              : bi("暂无订单。", "No orders yet.")}
          </EmptyState>
        ) : (
          <TableWrap className="rounded-2xl border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-900">
            <table className="erp-table">
              <thead>
                <tr>
                  <th>{bi("订单号", "Order no.")}</th>
                  <th>{bi("模式与买家", "Mode & buyer")}</th>
                  <th>{bi("履约状态", "Fulfillment")}</th>
                  <th>{bi("收款状态", "Payment")}</th>
                  <th className="text-right">{bi("订单金额", "Total")}</th>
                  <th>
                    <div className="flex flex-wrap items-center gap-2">
                      <span>{bi("出口单据", "Export docs")}</span>
                      <div
                        role="group"
                        aria-label={bi("PDF 语言切换", "PDF language")}
                        className="inline-flex items-center gap-0.5 rounded-full border border-gray-200 bg-gray-50 p-0.5 dark:border-gray-800 dark:bg-gray-800"
                      >
                        {(
                          [
                            { key: "en" as const, label: "EN" },
                            { key: "zh" as const, label: "中文" },
                          ]
                        ).map((option) => (
                          <button
                            key={option.key}
                            type="button"
                            data-testid={`pdf-lang-${option.key}`}
                            aria-pressed={pdfLanguage === option.key}
                            onClick={() => setPdfLanguage(option.key)}
                            className={`rounded-full px-2 py-0.5 text-[10.5px] font-semibold transition-colors ${
                              pdfLanguage === option.key
                                ? "bg-blue-600 text-white shadow-xs"
                                : "text-gray-600 hover:bg-white dark:text-gray-400 dark:hover:bg-gray-700"
                            }`}
                          >
                            {option.label}
                          </button>
                        ))}
                      </div>
                    </div>
                  </th>
                  <th className="text-right">{bi("17TRACK 与出库", "17TRACK & dispatch")}</th>
                </tr>
              </thead>
              <tbody>
                {orders.map((ord) => {
                  const blocked70Unpaid =
                    ord.order_mode === "b2b_wholesale" && ord.payment_status !== "paid";
                  const isTerminalOrShipped =
                    ord.fulfillment_status === "shipped" ||
                    ord.fulfillment_status === "delivered" ||
                    ord.fulfillment_status === "cancelled" ||
                    ord.fulfillment_status === "refunded";
                  const modeCopy = MODE_LABEL[ord.order_mode] ?? {
                    zh: ord.order_mode,
                    en: ord.order_mode,
                  };
                  const fulfillmentCopy = FULFILLMENT_LABEL[ord.fulfillment_status] ?? {
                    zh: ord.fulfillment_status,
                    en: ord.fulfillment_status,
                  };
                  const channelDisplay = ord.channel_type
                    ? (CHANNEL_LABEL[ord.channel_type] ?? ord.channel_type)
                    : null;
                  const extSync = ord.external_fulfillment;
                  const carrierCode = carrierInputs[ord.id] ?? "DHL";
                  const trackingProvider = providerInputs[ord.id] ?? "manual";
                  return (
                    <tr key={ord.id}>
                      <td>
                        <div className="font-mono text-[12.5px] font-semibold text-gray-900 dark:text-gray-100">
                          {ord.order_no}
                        </div>
                        {channelDisplay && (
                          <div className="mt-1 flex flex-wrap items-center gap-1">
                            <span
                              data-testid={`order-channel-${ord.id}`}
                              className="inline-flex items-center rounded-md border border-indigo-200 bg-indigo-50 px-1.5 py-0.5 text-[10px] font-semibold text-indigo-700 dark:border-indigo-500/30 dark:bg-indigo-500/10 dark:text-indigo-300"
                            >
                              {channelDisplay}
                            </span>
                            {ord.channel_label && (
                              <span className="max-w-[140px] truncate text-[10px] text-gray-500 dark:text-gray-400">
                                {ord.channel_label}
                              </span>
                            )}
                          </div>
                        )}
                        {ord.has_72h_stagnation_alert && (
                          <span
                            data-testid={`order-72h-alert-${ord.id}`}
                            className="mt-1 inline-flex items-center gap-1 rounded-md border border-rose-200 bg-rose-50 px-1.5 py-0.5 text-[10px] font-semibold text-rose-700 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-300"
                          >
                            <AlertTriangle className="h-3 w-3" />
                            <span>{bi("72 小时物流停滞", "72h stagnation")}</span>
                          </span>
                        )}
                      </td>
                      <td>
                        <div className="font-medium text-gray-800 dark:text-gray-200">{ord.customer_name}</div>
                        <Badge className="mt-1">{bi(modeCopy.zh, modeCopy.en)}</Badge>
                      </td>
                      <td>
                        <div className="flex flex-col items-start gap-1">
                          <span
                            className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-semibold ${
                              ord.fulfillment_status === "exception"
                                ? "border-rose-200 bg-rose-50 text-rose-700 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-300"
                                : "border-gray-200 bg-white text-gray-700 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300"
                            }`}
                          >
                            <span
                              aria-hidden="true"
                              className={`h-1.5 w-1.5 rounded-full ${
                                FULFILLMENT_DOT[ord.fulfillment_status] ?? "bg-slate-400"
                              }`}
                            />
                            {bi(fulfillmentCopy.zh, fulfillmentCopy.en)}
                          </span>
                          {extSync?.status === "synced" && (
                            <span
                              data-testid={`order-fulfillment-sync-${ord.id}`}
                              className="inline-flex items-center gap-1 rounded-md border border-emerald-200 bg-emerald-50 px-1.5 py-0.5 text-[10px] font-semibold text-emerald-700 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-300"
                              title={extSync.external_fulfillment_id ?? undefined}
                            >
                              <CheckCircle2 className="h-3 w-3" />
                              <span>
                                {bi(
                                  `已回传 ${channelDisplay ?? "平台"}`,
                                  `Synced to ${channelDisplay ?? "store"}`,
                                )}
                              </span>
                            </span>
                          )}
                          {extSync?.status === "failed" && (
                            <button
                              type="button"
                              data-testid={`order-push-fulfillment-${ord.id}`}
                              disabled={pushFulfillmentMutation.isPending}
                              onClick={() => pushFulfillmentMutation.mutate(ord.id)}
                              className="inline-flex items-center gap-1 rounded-md border border-rose-200 bg-rose-50 px-1.5 py-0.5 text-[10px] font-semibold text-rose-700 hover:bg-rose-100 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-300"
                              title={extSync.error_detail ?? extSync.error_code ?? undefined}
                            >
                              <RefreshCw className="h-3 w-3" />
                              <span>{bi("回传失败 · 重试", "Sync failed · Retry")}</span>
                            </button>
                          )}
                          {!extSync &&
                            channelDisplay &&
                            (ord.fulfillment_status === "shipped" ||
                              ord.fulfillment_status === "delivered") && (
                              <button
                                type="button"
                                data-testid={`order-push-fulfillment-${ord.id}`}
                                disabled={pushFulfillmentMutation.isPending}
                                onClick={() => pushFulfillmentMutation.mutate(ord.id)}
                                className="inline-flex items-center gap-1 rounded-md border border-blue-200 bg-blue-50 px-1.5 py-0.5 text-[10px] font-semibold text-blue-700 hover:bg-blue-100 dark:border-blue-500/30 dark:bg-blue-500/10 dark:text-blue-300"
                              >
                                <RefreshCw className="h-3 w-3" />
                                <span>
                                  {bi(
                                    `同步到 ${channelDisplay}`,
                                    `Sync to ${channelDisplay}`,
                                  )}
                                </span>
                              </button>
                            )}
                        </div>
                      </td>
                      <td>
                        <div className="font-medium text-gray-700 dark:text-gray-300">{ord.payment_status}</div>
                        {ord.refunded_amount && (
                          <span
                            data-testid={`order-refunded-${ord.id}`}
                            className="mt-1 inline-flex items-center rounded-md border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-[10px] font-semibold text-amber-800 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-300"
                          >
                            {bi("已退", "Refunded")}{" "}
                            {formatMoney(ord.refunded_amount, ord.currency)}
                          </span>
                        )}
                        {blocked70Unpaid && (
                          <span
                            data-testid={`order-ship-blocked-${ord.id}`}
                            className="mt-1 inline-flex items-center gap-1 rounded-md border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-[10px] font-semibold text-amber-900 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-300"
                          >
                            <Lock className="h-3 w-3" />
                            <span>{bi("70% 尾款未结清", "70% balance unpaid")}</span>
                          </span>
                        )}
                      </td>
                      <td className="text-right font-semibold tabular-nums text-gray-900 dark:text-gray-100">
                        {formatMoney(ord.total_amount, ord.currency)}
                      </td>
                      <td>
                        <div className="flex flex-wrap items-center gap-1.5">
                          <a
                            data-testid={`download-ci-${ord.id}`}
                            href={commercialInvoicePdfUrl(ord.id, pdfLanguage)}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="inline-flex items-center gap-1 rounded-lg border border-gray-200 bg-white px-2 py-1 text-[11.5px] font-semibold text-gray-700 shadow-xs transition-colors hover:border-blue-200 hover:bg-blue-50 hover:text-blue-700 dark:border-gray-800 dark:bg-gray-900 dark:text-gray-300 dark:hover:border-blue-500/40 dark:hover:bg-blue-500/10 dark:hover:text-blue-300"
                          >
                            <FileText className="h-3 w-3" />
                            <span>{bi("商业发票 CI", "Commercial invoice CI")}</span>
                          </a>
                          <a
                            data-testid={`download-pl-${ord.id}`}
                            href={packingListPdfUrl(ord.id, pdfLanguage)}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="inline-flex items-center gap-1 rounded-lg border border-gray-200 bg-white px-2 py-1 text-[11.5px] font-semibold text-gray-700 shadow-xs transition-colors hover:border-blue-200 hover:bg-blue-50 hover:text-blue-700 dark:border-gray-800 dark:bg-gray-900 dark:text-gray-300 dark:hover:border-blue-500/40 dark:hover:bg-blue-500/10 dark:hover:text-blue-300"
                          >
                            <FileSpreadsheet className="h-3 w-3" />
                            <span>{bi("装箱单 PL", "Packing list PL")}</span>
                          </a>
                        </div>
                      </td>
                      <td>
                        <div className="flex flex-wrap items-center justify-end gap-2">
                          <button
                            type="button"
                            data-testid={`open-shipment-drawer-${ord.id}`}
                            onClick={() => setDrawerOrderId(ord.id)}
                            className="erp-btn erp-btn-secondary rounded-xl py-1 text-[12px]"
                          >
                            <Truck className="h-3 w-3" />
                            <span>{bi("物流时间轴与台账", "Timeline & ledger")}</span>
                          </button>

                          {!isTerminalOrShipped && (
                            <>
                              <select
                                data-testid={`order-carrier-${ord.id}`}
                                value={carrierCode}
                                onChange={(e) =>
                                  setCarrierInputs((prev) => ({
                                    ...prev,
                                    [ord.id]: e.target.value,
                                  }))
                                }
                                aria-label={bi("物流商", "Carrier")}
                                disabled={blocked70Unpaid}
                                className="erp-input w-24 rounded-xl py-1 text-[12px] disabled:bg-gray-100 dark:disabled:bg-gray-800"
                              >
                                {CARRIER_OPTIONS.map((c) => (
                                  <option key={c} value={c}>
                                    {c}
                                  </option>
                                ))}
                              </select>
                              <select
                                data-testid={`order-provider-${ord.id}`}
                                value={trackingProvider}
                                onChange={(e) =>
                                  setProviderInputs((prev) => ({
                                    ...prev,
                                    [ord.id]: e.target.value as "manual" | "17track",
                                  }))
                                }
                                aria-label={bi("追踪模式", "Tracking mode")}
                                disabled={blocked70Unpaid}
                                className="erp-input w-24 rounded-xl py-1 text-[12px] disabled:bg-gray-100 dark:disabled:bg-gray-800"
                              >
                                <option value="manual">{bi("手动直传", "Manual")}</option>
                                <option value="17track">17TRACK</option>
                              </select>
                              <input
                                type="text"
                                data-testid={`order-tracking-input-${ord.id}`}
                                value={trackingInputs[ord.id] ?? ""}
                                onChange={(e) =>
                                  setTrackingInputs((prev) => ({
                                    ...prev,
                                    [ord.id]: e.target.value,
                                  }))
                                }
                                placeholder={bi("运单号", "Tracking #")}
                                aria-label={bi("运单号", "Tracking number")}
                                disabled={blocked70Unpaid}
                                className="erp-input w-32 rounded-xl py-1 text-[12px] disabled:bg-gray-100 dark:disabled:bg-gray-800"
                              />
                              <button
                                type="button"
                                data-testid={`order-ship-btn-${ord.id}`}
                                disabled={
                                  blocked70Unpaid ||
                                  shipMutation.isPending ||
                                  !(trackingInputs[ord.id] ?? "").trim()
                                }
                                onClick={() =>
                                  shipMutation.mutate({
                                    orderId: ord.id,
                                    trackingNumber: (trackingInputs[ord.id] ?? "").trim(),
                                    carrierCode,
                                    trackingProvider,
                                  })
                                }
                                className="erp-btn rounded-xl bg-blue-600 text-white shadow-xs transition-colors hover:bg-blue-700 disabled:opacity-40"
                              >
                                <ArrowRightLeft className="h-3 w-3" />
                                <span>{bi("出库", "Ship")}</span>
                              </button>
                            </>
                          )}
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
        <ShipmentTimelineDrawer orderId={drawerOrderId} onClose={() => setDrawerOrderId(null)} />
      )}
    </AppShell>
  );
}
