// Track 10 API client: batch 4x6 waybills, merged print run and scan-to-ship.
//
// Every call goes through `bffFetch` (`/api/bff/v1/orders/...`, session cookie + 401
// refresh), so the browser never learns a carrier credential and never talks to the
// backend directly. Money stays a string end-to-end: the API speaks `Decimal`.
//
// Mock mode (`NEXT_PUBLIC_USE_MOCK_API=true`) keeps the whole warehouse flow clickable
// on the fixture order ids (`ord-b2b-1`, `ord-b2c-1`, ...) with zero backend.
import { bffFetch } from "@/lib/api";
import type {
  BatchLabelPdfRequest,
  BatchShippingLabelRequest,
  BatchShippingLabelResponse,
  ScanDispatchRequest,
  ScanDispatchResponse,
} from "@/lib/api-client/types.gen";
import { shouldUseMockApi } from "@/lib/data-source";

export type BatchLabelPayload = BatchShippingLabelRequest;
export type BatchLabelResult = BatchShippingLabelResponse;
export type BatchLabelBooked = NonNullable<BatchShippingLabelResponse["booked"]>[number];
export type BatchLabelSkipped = NonNullable<BatchShippingLabelResponse["skipped"]>[number];
export type BatchPdfPayload = BatchLabelPdfRequest;
export type ScanDispatchResult = ScanDispatchResponse;
export type ScanSkipCode = BatchLabelSkipped["reason_code"];

/** Human copy for every per-order skip the backend can report. */
export const BATCH_SKIP_LABELS: Record<ScanSkipCode, { zh: string; en: string }> = {
  order_not_found: { zh: "订单不存在", en: "Order not found" },
  order_inactive: { zh: "订单已取消/退款", en: "Order cancelled or refunded" },
  held_for_review: { zh: "负毛利风控拦截", en: "Held by anti-loss routing" },
  balance_unpaid: { zh: "70% 尾款未结清", en: "70% balance unpaid" },
  booking_failed: { zh: "承运商下单失败", en: "Carrier booking failed" },
};

export function batchLabelsPath(): string {
  return "/v1/orders/shipping-labels/batch";
}

export function batchLabelsPdfPath(): string {
  return "/v1/orders/shipping-labels/batch-pdf";
}

export function scanDispatchPath(): string {
  return "/v1/orders/scan-dispatch";
}

/** Book (or reuse) one waybill per order; refused orders come back in `skipped`. */
export async function bookBatchShippingLabels(
  payload: BatchLabelPayload
): Promise<BatchLabelResult> {
  if (shouldUseMockApi()) {
    return mockBatchBooking(payload);
  }
  return bffFetch<BatchLabelResult>(batchLabelsPath(), {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

/** Merge the booked 4x6 labels into a single sequential print-run PDF. */
export async function fetchBatchLabelsPdf(payload: BatchPdfPayload): Promise<Blob> {
  if (shouldUseMockApi()) {
    return new Blob([MOCK_LABEL_PDF], { type: "application/pdf" });
  }
  const response = await fetch(`/api/bff${batchLabelsPdfPath()}`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    throw new Error(`Batch label PDF failed with status ${response.status}`);
  }
  return response.blob();
}

/** Warehouse barcode scan: release the parcel, or surface the 409 refusal code. */
export async function scanDispatch(code: string): Promise<ScanDispatchResult> {
  const payload: ScanDispatchRequest = { code };
  if (shouldUseMockApi()) {
    return mockScanDispatch(code);
  }
  return bffFetch<ScanDispatchResult>(scanDispatchPath(), {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

// ---------------------------------------------------------------------------
// Mock fixtures (only reachable with NEXT_PUBLIC_USE_MOCK_API=true)
// ---------------------------------------------------------------------------

const MOCK_NOW = "2026-11-26T09:00:00.000Z";
//: One-page placeholder so the download path is exercised without a real renderer.
const MOCK_LABEL_PDF = "%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n";

export const MOCK_BLOCKED_ORDER_IDS: readonly string[] = ["ord-b2b-1"];

function mockTrackingNumber(orderId: string): string {
  let hash = 2166136261;
  for (let index = 0; index < orderId.length; index += 1) {
    hash ^= orderId.charCodeAt(index);
    hash = Math.imul(hash, 16777619) >>> 0;
  }
  return `SBX${String(hash).padStart(9, "0")}`;
}

function mockBatchBooking(payload: BatchLabelPayload): BatchLabelResult {
  const carrier = payload.carrier_code ?? "sandbox_carrier";
  const mode = payload.mode ?? "sandbox";
  const booked: BatchLabelBooked[] = [];
  const skipped: BatchLabelSkipped[] = [];
  for (const orderId of payload.order_ids) {
    if (MOCK_BLOCKED_ORDER_IDS.includes(orderId)) {
      skipped.push({
        order_id: orderId,
        order_no: orderId === "ord-b2b-1" ? "ORD-B2B-2026-01" : null,
        reason_code: "balance_unpaid",
        reason: "The 70% B2B balance is still open; Finance must settle it first.",
      });
      continue;
    }
    const tracking = mockTrackingNumber(`${orderId}:${carrier}:${mode}`);
    booked.push({
      order_id: orderId,
      order_no: orderId === "ord-b2c-1" ? "ORD-B2C-2026-88" : `ORD-${orderId}`,
      label_id: `label-${orderId}`,
      tracking_number: tracking,
      waybill_number: tracking,
      carrier_code: carrier,
      service_code: payload.service_code ?? "Sandbox-Standard",
      mode,
      reused: false,
      shipping_cost_estimate: "12.50",
      currency: "USD",
    });
  }
  return { booked, skipped };
}

function mockScanDispatch(code: string): ScanDispatchResult {
  const trimmed = code.trim();
  if (!trimmed) {
    throw new Error("A scanned code is required.");
  }
  if (trimmed.toUpperCase().startsWith("SBX")) {
    return {
      order_id: "ord-b2c-1",
      order_no: "ORD-B2C-2026-88",
      tracking_number: trimmed.toUpperCase(),
      status: "shipped",
      dispatched_at: MOCK_NOW,
    };
  }
  if (trimmed === "ORD-B2B-2026-01") {
    throw new Error("ERR_BALANCE_PAYMENT_REQUIRED");
  }
  if (trimmed === "ORD-B2C-2026-88") {
    return {
      order_id: "ord-b2c-1",
      order_no: "ORD-B2C-2026-88",
      tracking_number: null,
      status: "shipped",
      dispatched_at: MOCK_NOW,
    };
  }
  throw new Error("ERR_SCAN_CODE_NOT_FOUND");
}
