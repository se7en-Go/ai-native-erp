"use client";

// Per-order "Routing & Label" modal on /orders: shows the latest routing decision,
// surfaces an anti-loss hold with a Manager-only release action, books a carrier
// waybill (sandbox / YunExpress / 4PX / DHL) and links the 4x6 thermal PDF.
import React, { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Download,
  FileText,
  RefreshCw,
  ShieldCheck,
  Truck,
  Unlock,
  X,
} from "lucide-react";
import { Badge } from "@/components/ui/page";
import { useAuthSession } from "@/components/providers";
import { useI18n } from "@/lib/i18n";
import {
  ROUTING_CARRIERS,
  bookShippingLabel,
  evaluateOrderRouting,
  fetchRoutingEvaluation,
  fetchShippingLabels,
  releaseRoutingHold,
  shippingLabelPdfUrl,
  type RoutingCarrierCode,
} from "@/lib/api-client/track5-routing-labels";

const MANAGER_ROLES = new Set(["owner", "admin", "manager"]);

export function ShippingLabelModal({
  orderId,
  orderNo,
  onClose,
}: {
  orderId: string;
  orderNo?: string;
  onClose: () => void;
}) {
  const { bi } = useI18n();
  const { principal } = useAuthSession();
  const queryClient = useQueryClient();
  const canRelease = MANAGER_ROLES.has(principal?.role ?? "");
  const [carrier, setCarrier] = useState<RoutingCarrierCode>("sandbox_carrier");
  const [mode, setMode] = useState<"sandbox" | "live">("sandbox");
  const [releaseReason, setReleaseReason] = useState("");
  const [bookingError, setBookingError] = useState<{ code?: string; message: string } | null>(
    null,
  );
  const [bookedLabel, setBookedLabel] = useState<{
    trackingNumber: string;
    waybillNumber: string;
    labelId: string;
  } | null>(null);
  const autoEvaluated = useRef(false);

  const evaluationQuery = useQuery({
    queryKey: ["orders", orderId, "routing-evaluation"],
    queryFn: () => fetchRoutingEvaluation(orderId),
  });

  const labelsQuery = useQuery({
    queryKey: ["orders", orderId, "shipping-labels"],
    queryFn: () => fetchShippingLabels(orderId),
  });

  const evaluateMutation = useMutation({
    mutationFn: () => evaluateOrderRouting(orderId),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: ["orders", orderId, "routing-evaluation"],
      });
    },
  });

  const releaseMutation = useMutation({
    mutationFn: (reason: string) => releaseRoutingHold(orderId, reason),
    onSuccess: () => {
      setReleaseReason("");
      setBookingError(null);
      void queryClient.invalidateQueries({
        queryKey: ["orders", orderId, "routing-evaluation"],
      });
    },
    onError: (error: Error) => {
      const status = (error as { status?: number }).status;
      const code = (error as { code?: string }).code;
      setBookingError({
        code: code ?? `ERR_${status ?? "HOLD"}`,
        message: error.message,
      });
    },
  });

  const bookMutation = useMutation({
    mutationFn: () =>
      bookShippingLabel(orderId, {
        carrier_code: carrier,
        mode,
        override_hold: false,
      }),
    onSuccess: (label) => {
      setBookingError(null);
      setBookedLabel({
        trackingNumber: label.tracking_number,
        waybillNumber: label.waybill_number,
        labelId: label.id,
      });
      void queryClient.invalidateQueries({
        queryKey: ["orders", orderId, "shipping-labels"],
      });
    },
    onError: (error: Error) =>
      setBookingError({
        code: (error as { code?: string }).code,
        message: error.message,
      }),
  });

  // Exactly one automatic evaluation when the order has never been routed, so the
  // operator always sees a margin verdict before booking a waybill.
  useEffect(() => {
    if (autoEvaluated.current) return;
    if (!evaluationQuery.isSuccess) return;
    if (evaluationQuery.data !== null) return;
    autoEvaluated.current = true;
    evaluateMutation.mutate();
  }, [evaluationQuery.isSuccess, evaluationQuery.data, evaluateMutation]);

  const evaluation = evaluationQuery.data ?? null;
  const labels = labelsQuery.data ?? [];
  const isHeld = Boolean(evaluation?.is_held);
  // Only YunExpress and 4PX have a live booking contract; every other carrier family is
  // sandbox-only, and the backend rejects the combination with a 422.
  const liveBookable = mode === "sandbox" || carrier === "yunexpress" || carrier === "4px";

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-slate-900/40 p-4 backdrop-blur-sm">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="shipping-label-title"
        data-testid="shipping-label-modal"
        className="erp-card my-6 w-full max-w-3xl rounded-[20px] p-4 shadow-2xl"
      >
        <div className="flex items-start justify-between gap-3 border-b border-gray-200 pb-3 dark:border-gray-800">
          <div className="min-w-0">
            <h3
              id="shipping-label-title"
              className="text-[15px] font-bold text-gray-900 dark:text-gray-100"
            >
              {bi("审单路由 & 打面单", "Routing & carrier label")}
            </h3>
            <p className="mt-1 text-[12px] text-gray-600 dark:text-gray-400">
              {orderNo ? `${orderNo} · ` : ""}
              {bi(
                "先看毛利率拦截结论，再选择承运商下单面单；沙箱模式不产生真实运费。",
                "Review the margin verdict first, then book the waybill. Sandbox mode never spends real freight."
              )}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label={bi("关闭路由弹窗", "Close routing dialog")}
            data-testid="shipping-label-close"
            className="rounded-lg border border-gray-200 bg-white p-1.5 text-gray-600 shadow-xs hover:bg-gray-100 dark:border-gray-800 dark:bg-gray-900 dark:text-gray-400 dark:hover:bg-gray-800"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="mt-3 grid gap-3 lg:grid-cols-2">
          <section
            data-testid="routing-evaluation-card"
            className="rounded-xl border border-gray-200 bg-gray-50/70 p-3 dark:border-gray-800 dark:bg-gray-800/40"
          >
            <div className="flex items-center justify-between gap-2">
              <span className="flex items-center gap-1.5 text-[12.5px] font-bold text-gray-900 dark:text-gray-100">
                <ShieldCheck className="h-3.5 w-3.5 text-blue-600 dark:text-blue-400" />
                {bi("路由评估", "Routing verdict")}
              </span>
              <button
                type="button"
                data-testid="evaluate-routing-btn"
                disabled={evaluateMutation.isPending}
                onClick={() => evaluateMutation.mutate()}
                className="erp-btn erp-btn-secondary rounded-lg px-2 py-0.5 text-[11px]"
              >
                <RefreshCw className="h-3 w-3" />
                <span>{bi("重新评估", "Re-evaluate")}</span>
              </button>
            </div>

            {evaluationQuery.isLoading ? (
              <p className="mt-2 text-[11.5px] text-gray-500 dark:text-gray-400">
                {bi("正在读取评估…", "Loading verdict…")}
              </p>
            ) : evaluation === null ? (
              <p
                data-testid="routing-evaluation-empty"
                className="mt-2 text-[11.5px] text-gray-500 dark:text-gray-400"
              >
                {bi(
                  "该订单尚未路由，点击“重新评估”立即计算毛利与推荐承运商。",
                  "This order has no verdict yet. Re-evaluate to compute margin and carrier."
                )}
              </p>
            ) : (
              <div className="mt-2 space-y-1.5">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span
                    data-testid="routing-eval-margin"
                    className="font-mono text-[13px] font-bold tabular-nums text-gray-900 dark:text-gray-100"
                  >
                    {evaluation.estimated_margin_pct ?? "--"}%
                  </span>
                  <span className="text-[11px] text-gray-500 dark:text-gray-400">
                    {bi("预估毛利率", "estimated margin")}
                  </span>
                  {isHeld ? (
                    <Badge
                      data-testid="routing-hold-badge"
                      className="border-rose-200 bg-rose-50 text-rose-700 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-300"
                    >
                      {bi("已拦截", "Held")}
                    </Badge>
                  ) : (
                    <Badge
                      data-testid="routing-ok-badge"
                      className="border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-300"
                    >
                      {bi("可出单", "Clear")}
                    </Badge>
                  )}
                </div>
                <p className="text-[11.5px] text-gray-600 dark:text-gray-400">
                  {bi("推荐承运商", "Carrier")}：
                  {evaluation.recommended_carrier ?? "—"}
                  {evaluation.recommended_service
                    ? ` · ${evaluation.recommended_service}`
                    : ""}
                </p>
                <p className="text-[11.5px] text-gray-600 dark:text-gray-400">
                  {bi("命中规则", "Matched rules")}：{evaluation.matched_rule_ids.length}
                  {evaluation.applied_tags.length > 0
                    ? ` · ${evaluation.applied_tags.join(" / ")}`
                    : ""}
                </p>
                {isHeld && (
                  <div
                    data-testid="routing-hold-alert"
                    className="rounded-lg border border-rose-200 bg-rose-50 p-2 text-[11.5px] text-rose-800 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-300"
                  >
                    <div className="flex items-center gap-1 font-semibold">
                      <AlertTriangle className="h-3.5 w-3.5" />
                      <span>{bi("防亏拦截中", "Anti-loss hold active")}</span>
                    </div>
                    <p data-testid="routing-hold-reason" className="mt-0.5">
                      {evaluation.hold_reason ?? bi("需主管复核", "Manager review required")}
                    </p>
                    {canRelease ? (
                      <div className="mt-2 flex flex-wrap items-center gap-1.5">
                        <input
                          type="text"
                          data-testid="release-hold-reason"
                          value={releaseReason}
                          onChange={(event) => setReleaseReason(event.target.value)}
                          placeholder={bi("放行原因（必填）", "Release reason (required)")}
                          className="erp-input w-52 rounded-lg py-1 text-[11.5px]"
                        />
                        <button
                          type="button"
                          data-testid="release-hold-submit"
                          disabled={
                            releaseMutation.isPending || releaseReason.trim().length < 3
                          }
                          onClick={() => releaseMutation.mutate(releaseReason.trim())}
                          className="erp-btn rounded-lg bg-rose-600 px-2 py-1 text-[11.5px] font-semibold text-white hover:bg-rose-700 disabled:opacity-40"
                        >
                          <Unlock className="h-3 w-3" />
                          <span>{bi("主管放行", "Release hold")}</span>
                        </button>
                      </div>
                    ) : (
                      <p className="mt-1 text-[11px]">
                        {bi(
                          "仅 Owner / Admin / Manager 可放行。",
                          "Only an Owner, Admin or Manager can release this hold."
                        )}
                      </p>
                    )}
                  </div>
                )}
              </div>
            )}
          </section>

          <section
            data-testid="shipping-label-booking-card"
            className="rounded-xl border border-gray-200 bg-white p-3 dark:border-gray-800 dark:bg-gray-900"
          >
            <span className="flex items-center gap-1.5 text-[12.5px] font-bold text-gray-900 dark:text-gray-100">
              <Truck className="h-3.5 w-3.5 text-blue-600 dark:text-blue-400" />
              {bi("承运商下单", "Carrier booking")}
            </span>
            <div className="mt-2 grid gap-2 sm:grid-cols-2">
              <label className="flex flex-col gap-1 text-[11px] font-medium text-gray-600 dark:text-gray-400">
                <span>{bi("承运商", "Carrier")}</span>
                <select
                  data-testid="label-carrier-select"
                  value={carrier}
                  onChange={(event) =>
                    setCarrier(event.target.value as RoutingCarrierCode)
                  }
                  className="erp-input w-full rounded-lg py-1 text-[12px]"
                >
                  {ROUTING_CARRIERS.map((option) => (
                    <option key={option.code} value={option.code}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="flex flex-col gap-1 text-[11px] font-medium text-gray-600 dark:text-gray-400">
                <span>{bi("模式", "Mode")}</span>
                <select
                  data-testid="label-mode-select"
                  value={mode}
                  onChange={(event) => setMode(event.target.value as "sandbox" | "live")}
                  className="erp-input w-full rounded-lg py-1 text-[12px]"
                >
                  <option value="sandbox">{bi("沙箱（不产生运费）", "Sandbox (no spend)")}</option>
                  <option value="live">{bi("正式下单", "Live booking")}</option>
                </select>
              </label>
            </div>
            <button
              type="button"
              data-testid="book-label-submit"
              disabled={bookMutation.isPending || isHeld || !liveBookable}
              onClick={() => bookMutation.mutate()}
              className="erp-btn mt-2 w-full justify-center rounded-lg bg-blue-600 px-3 py-1.5 text-[12.5px] font-semibold text-white hover:bg-blue-700 disabled:opacity-40"
            >
              <FileText className="h-3.5 w-3.5" />
              <span>
                {bookMutation.isPending
                  ? bi("下单中…", "Booking…")
                  : bi("生成 4x6 面单", "Book 4x6 waybill")}
              </span>
            </button>
            {isHeld && (
              <p className="mt-1 text-[11px] text-rose-700 dark:text-rose-300">
                {bi(
                  "订单处于防亏拦截中，需主管放行后才能下单面单。",
                  "The order is held for anti-loss review; a manager must release it first."
                )}
              </p>
            )}
            {!liveBookable && (
              <p
                data-testid="label-live-mode-hint"
                className="mt-1 text-[11px] text-amber-700 dark:text-amber-300"
              >
                {bi(
                  "该承运商暂未接入正式下单接口，请改用沙箱模式或选择 YunExpress / 4PX。",
                  "This carrier has no live booking contract yet. Use sandbox mode, or pick YunExpress / 4PX."
                )}
              </p>
            )}

            {bookingError && (
              <p
                role="alert"
                data-testid="label-booking-error"
                className="mt-2 rounded-lg border border-rose-200 bg-rose-50 p-2 text-[11.5px] font-medium text-rose-800 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-300"
              >
                {bookingError.code ? `${bookingError.code}: ` : ""}
                {bookingError.message}
              </p>
            )}
            {bookedLabel && (
              <p
                data-testid="label-booking-success"
                className="mt-2 rounded-lg border border-emerald-200 bg-emerald-50 p-2 text-[11.5px] font-medium text-emerald-800 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-300"
              >
                {bi("已生成面单", "Waybill booked")} · {bookedLabel.waybillNumber} ·{" "}
                {bookedLabel.trackingNumber}
              </p>
            )}
          </section>
        </div>

        <section className="mt-3">
          <span className="text-[12.5px] font-bold text-gray-900 dark:text-gray-100">
            {bi("已生成面单", "Booked labels")}
          </span>
          {labels.length === 0 ? (
            <p
              data-testid="shipping-labels-empty"
              className="mt-1 rounded-lg border border-dashed border-gray-300 p-2 text-[11.5px] text-gray-500 dark:border-gray-700 dark:text-gray-400"
            >
              {labelsQuery.isLoading
                ? bi("正在加载面单…", "Loading labels…")
                : bi("还没有面单。", "No waybill booked yet.")}
            </p>
          ) : (
            <ul data-testid="shipping-labels-list" className="mt-1 space-y-1">
              {labels.map((label) => (
                <li
                  key={label.id}
                  data-testid={`shipping-label-row-${label.id}`}
                  className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-gray-200 px-2 py-1.5 dark:border-gray-800"
                >
                  <div className="min-w-0 text-[11.5px] text-gray-700 dark:text-gray-300">
                    <span className="font-semibold">{label.carrier_code}</span>
                    <span className="ml-1 font-mono">{label.tracking_number}</span>
                    <span className="ml-2 text-gray-500 dark:text-gray-400">
                      {label.waybill_number} · {label.mode} ·{" "}
                      {label.shipping_cost_estimate ?? "—"} {label.currency}
                    </span>
                  </div>
                  <a
                    data-testid={`download-label-${label.id}`}
                    href={shippingLabelPdfUrl(orderId, label.id)}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1 rounded-lg border border-gray-200 bg-white px-2 py-0.5 text-[11px] font-semibold text-gray-700 hover:border-blue-200 hover:bg-blue-50 hover:text-blue-700 dark:border-gray-800 dark:bg-gray-900 dark:text-gray-300"
                  >
                    <Download className="h-3 w-3" />
                    <span>{bi("4x6 PDF", "4x6 PDF")}</span>
                  </a>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </div>
  );
}
