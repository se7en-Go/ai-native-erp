"use client";

import React, { useCallback, useMemo, useState } from "react";
import { CheckCircle2, Download, Lock, ScanLine, SkipForward, TriangleAlert } from "lucide-react";

import { useI18n } from "@/lib/i18n";
import {
  BATCH_SKIP_LABELS,
  bookBatchShippingLabels,
  fetchBatchLabelsPdf,
  scanDispatch,
  type BatchLabelResult,
  type ScanDispatchResult,
} from "@/lib/api-client/track10-polish";

/**
 * Warehouse print-run + scan-to-ship bar for `/orders`.
 *
 * Two independent actions over the rows an operator ticked in the order table:
 *
 * 1. `批量打面单 (合并 PDF)` books (or reuses) one 4x6 waybill per selected order and
 *    merges every label PDF into a single sequential print run. Orders the backend
 *    refuses — unpaid 70% B2B balance, active negative-margin hold, cancelled order —
 *    are reported per row instead of aborting the whole run, so the warehouse prints
 *    everything that is genuinely shippable.
 * 2. `扫码核验发货` takes a barcode (waybill tracking number, shipment tracking number or
 *    order number) and reports the outcome instantly: shipped, or the exact 409 refusal.
 */

const CARRIERS = [
  { code: "sandbox_carrier", zh: "沙箱面单", en: "Sandbox label" },
  { code: "yunexpress", zh: "云途 YunExpress", en: "YunExpress" },
  { code: "4px", zh: "4PX 递四方", en: "4PX" },
  { code: "dhl", zh: "DHL Express", en: "DHL Express" },
] as const;

type CarrierCode = (typeof CARRIERS)[number]["code"];

export interface BatchDispatchOrderRef {
  id: string;
  order_no: string;
}

export interface BatchDispatchBarProps {
  orders: readonly BatchDispatchOrderRef[];
  selectedOrderIds: readonly string[];
  onSelectionCleared?: () => void;
}

export function BatchDispatchBar({
  orders,
  selectedOrderIds,
  onSelectionCleared,
}: BatchDispatchBarProps) {
  const { bi } = useI18n();
  const [carrier, setCarrier] = useState<CarrierCode>("sandbox_carrier");
  const [mode, setMode] = useState<"sandbox" | "live">("sandbox");
  const [booking, setBooking] = useState(false);
  const [bookError, setBookError] = useState<string | null>(null);
  const [bookResult, setBookResult] = useState<BatchLabelResult | null>(null);
  const [pdfUrl, setPdfUrl] = useState<string | null>(null);
  const [scanCode, setScanCode] = useState("");
  const [scanning, setScanning] = useState(false);
  const [scanError, setScanError] = useState<string | null>(null);
  const [scanBlocked, setScanBlocked] = useState(false);
  const [scanResult, setScanResult] = useState<ScanDispatchResult | null>(null);

  const selectedOrders = useMemo(() => {
    const byId = new Map(orders.map((order) => [order.id, order]));
    return selectedOrderIds
      .map((id) => byId.get(id))
      .filter((order): order is BatchDispatchOrderRef => Boolean(order));
  }, [orders, selectedOrderIds]);

  const onBookAndMerge = useCallback(async () => {
    if (selectedOrders.length === 0) return;
    setBooking(true);
    setBookError(null);
    setBookResult(null);
    setPdfUrl(null);
    try {
      const result = await bookBatchShippingLabels({
        order_ids: selectedOrders.map((order) => order.id),
        carrier_code: carrier,
        mode,
      });
      setBookResult(result);
      const labelIds = (result.booked ?? []).map((item) => item.label_id);
      if (labelIds.length === 0) {
        return;
      }
      const blob = await fetchBatchLabelsPdf({ label_ids: labelIds });
      if (typeof URL !== "undefined" && typeof URL.createObjectURL === "function") {
        setPdfUrl(URL.createObjectURL(blob));
      }
      onSelectionCleared?.();
    } catch (error) {
      setBookError(error instanceof Error ? error.message : String(error));
    } finally {
      setBooking(false);
    }
  }, [carrier, mode, onSelectionCleared, selectedOrders]);

  const onScan = useCallback(
    async (event: React.FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      const code = scanCode.trim();
      if (!code) {
        setScanError(bi("请输入运单号或订单号。", "Enter a tracking number or order number."));
        return;
      }
      setScanning(true);
      setScanError(null);
      setScanBlocked(false);
      setScanResult(null);
      try {
        const result = await scanDispatch(code);
        setScanResult(result);
        setScanCode("");
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        const blocked =
          message === "ERR_BALANCE_PAYMENT_REQUIRED" || message === "ERR_ORDER_HELD_FOR_REVIEW";
        setScanBlocked(blocked);
        setScanError(
          message === "ERR_BALANCE_PAYMENT_REQUIRED"
            ? bi(
                "70% 尾款未结清，无法放行发货。",
                "The 70% B2B balance is unpaid; the parcel stays blocked."
              )
            : message === "ERR_ORDER_HELD_FOR_REVIEW"
              ? bi(
                  "负毛利风控拦截，需主管放行。",
                  "Held by the anti-loss guard; a manager must release it."
                )
              : message === "ERR_SCAN_CODE_NOT_FOUND"
                ? bi("未找到该条码对应的面单/订单。", "No waybill or order matches that code.")
                : message
        );
      } finally {
        setScanning(false);
      }
    },
    [bi, scanCode]
  );

  const booked = bookResult?.booked ?? [];
  const skipped = bookResult?.skipped ?? [];

  return (
    <section
      data-testid="batch-dispatch-bar"
      aria-label={bi("批量面单与扫码发货", "Batch labels & scan-to-ship")}
      className="space-y-3 rounded-2xl border border-gray-200 bg-white p-3.5 dark:border-gray-800 dark:bg-gray-900"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[12px] font-semibold text-gray-700 dark:text-gray-300">
          {bi("已选订单", "Selected orders")}：
          <span data-testid="batch-selected-count" className="tabular-nums">
            {selectedOrders.length}
          </span>
        </span>
        <select
          data-testid="batch-carrier-select"
          aria-label={bi("批量面单承运商", "Batch carrier")}
          value={carrier}
          onChange={(event) => setCarrier(event.target.value as CarrierCode)}
          className="erp-input w-auto rounded-xl py-1 text-[12px]"
        >
          {CARRIERS.map((option) => (
            <option key={option.code} value={option.code}>
              {bi(option.zh, option.en)}
            </option>
          ))}
        </select>
        <select
          data-testid="batch-mode-select"
          aria-label={bi("批量面单模式", "Batch label mode")}
          value={mode}
          onChange={(event) => setMode(event.target.value as "sandbox" | "live")}
          className="erp-input w-auto rounded-xl py-1 text-[12px]"
        >
          <option value="sandbox">{bi("沙箱", "Sandbox")}</option>
          <option value="live">{bi("正式", "Live")}</option>
        </select>
        <button
          type="button"
          data-testid="batch-label-button"
          disabled={booking || selectedOrders.length === 0}
          onClick={() => void onBookAndMerge()}
          className="erp-btn rounded-xl bg-blue-600 text-white shadow-xs transition-colors hover:bg-blue-700 disabled:opacity-40"
        >
          <Download className="h-3.5 w-3.5" aria-hidden="true" />
          <span>
            {booking
              ? bi("正在生成面单…", "Booking labels…")
              : bi("批量打面单 (合并 PDF)", "Batch labels (merged PDF)")}
          </span>
        </button>
        {pdfUrl && (
          <a
            data-testid="batch-pdf-link"
            href={pdfUrl}
            download="shipping-labels-batch.pdf"
            className="inline-flex items-center gap-1 rounded-lg border border-gray-200 bg-white px-2 py-1 text-[11.5px] font-semibold text-gray-700 hover:border-blue-200 hover:bg-blue-50 hover:text-blue-700 dark:border-gray-800 dark:bg-gray-900 dark:text-gray-300"
          >
            <Download className="h-3 w-3" aria-hidden="true" />
            {bi("下载合并 PDF", "Download merged PDF")}
          </a>
        )}
      </div>

      {bookError && (
        <p
          role="alert"
          data-testid="batch-label-error"
          className="rounded-xl border border-rose-200 bg-rose-50 p-2.5 text-[12px] font-medium text-rose-800 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-300"
        >
          {bookError}
        </p>
      )}

      {bookResult && (
        <div data-testid="batch-label-summary" className="space-y-1.5 text-[12px]">
          <p className="flex items-center gap-1.5 font-semibold text-emerald-700 dark:text-emerald-300">
            <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
            {bi(
              `已出 ${booked.length} 张面单，跳过 ${skipped.length} 单`,
              `${booked.length} label(s) booked, ${skipped.length} order(s) skipped`
            )}
          </p>
          {booked.length > 0 && (
            <ul data-testid="batch-booked-list" className="space-y-0.5 text-gray-600 dark:text-gray-400">
              {booked.map((item) => (
                <li key={item.label_id} className="tabular-nums">
                  {item.order_no} · {item.tracking_number}
                  {item.reused ? bi("（复用）", " (reused)") : ""}
                </li>
              ))}
            </ul>
          )}
          {skipped.length > 0 && (
            <ul data-testid="batch-skip-list" className="space-y-0.5 text-amber-800 dark:text-amber-300">
              {skipped.map((item) => (
                <li key={item.order_id} className="flex flex-wrap items-center gap-1.5">
                  <SkipForward className="h-3 w-3 shrink-0" aria-hidden="true" />
                  <span className="font-semibold tabular-nums">
                    {item.order_no ?? item.order_id}
                  </span>
                  <span data-testid={`batch-skip-${item.order_id}`}>
                    {bi(
                      BATCH_SKIP_LABELS[item.reason_code].zh,
                      BATCH_SKIP_LABELS[item.reason_code].en
                    )}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      <form
        onSubmit={onScan}
        data-testid="scan-dispatch-form"
        className="flex flex-wrap items-center gap-2 border-t border-gray-100 pt-3 dark:border-gray-800"
      >
        <label className="flex items-center gap-1.5 text-[12px] font-medium text-gray-600 dark:text-gray-400">
          <ScanLine className="h-3.5 w-3.5" aria-hidden="true" />
          {bi("扫码核验发货", "Scan to ship")}
        </label>
        <input
          data-testid="scan-code-input"
          value={scanCode}
          onChange={(event) => setScanCode(event.target.value)}
          placeholder={bi("运单号 / 订单号", "Tracking # / order no.")}
          aria-label={bi("扫码运单号", "Scanned code")}
          className="erp-input w-56 rounded-xl py-1 text-[12px] tabular-nums"
        />
        <button
          type="submit"
          data-testid="scan-submit"
          disabled={scanning || scanCode.trim().length === 0}
          className="erp-btn rounded-xl bg-gray-900 text-white shadow-xs transition-colors hover:bg-gray-800 disabled:opacity-40 dark:bg-gray-700 dark:hover:bg-gray-600"
        >
          {scanning ? bi("核验中…", "Verifying…") : bi("核验发货", "Verify & ship")}
        </button>
      </form>

      {scanResult && (
        <p
          data-testid="scan-feedback"
          className="flex flex-wrap items-center gap-1.5 rounded-xl border border-emerald-200 bg-emerald-50 p-2.5 text-[12px] font-medium text-emerald-800 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-300"
        >
          <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
          <span className="tabular-nums">{scanResult.order_no}</span>
          <span>→ {scanResult.status}</span>
          {scanResult.tracking_number && (
            <span className="tabular-nums text-emerald-700/80">
              · {scanResult.tracking_number}
            </span>
          )}
        </p>
      )}

      {scanError && (
        <p
          role="alert"
          data-testid="scan-error"
          className="flex items-center gap-1.5 rounded-xl border border-amber-200 bg-amber-50 p-2.5 text-[12px] font-medium text-amber-900 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-300"
        >
          {scanBlocked ? (
            <Lock className="h-3.5 w-3.5" aria-hidden="true" />
          ) : (
            <TriangleAlert className="h-3.5 w-3.5" aria-hidden="true" />
          )}
          {scanError}
        </p>
      )}
    </section>
  );
}
