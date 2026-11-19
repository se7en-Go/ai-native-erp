"use client";

import React, { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Building2,
  Fingerprint,
  GitMerge,
  Split,
  PackageSearch,
  FileText,
  Truck,
} from "lucide-react";
import { bffFetch } from "@/lib/api";
import {
  formatMoney,
  type ConfidenceLevel,
  type CustomerType,
  type IdentityType,
  type Incoterm,
} from "@/lib/contracts";

export interface CustomerContextPayload {
  customer: {
    id: string;
    display_name: string;
    customer_type: CustomerType;
    ltv_usd: string | number;
    lifecycle_stage: string;
  };
  company?: {
    id: string;
    legal_name: string;
    country_code: string;
    credit_tier: string;
    default_incoterm: Incoterm;
  } | null;
  identities: Array<{
    id: string;
    identity_type: IdentityType;
    identity_value: string;
    confidence_level: ConfidenceLevel;
  }>;
  probable_duplicates: Array<{
    candidate_customer_id: string;
    display_name: string;
    matched_signal: string;
    similarity_score: number;
  }>;
  merge_events: Array<{
    id: string;
    source_customer_id: string;
    target_customer_id: string;
    action: "merge" | "split";
    reason?: string | null;
  }>;
  recent_orders: Array<{
    id: string;
    order_no: string;
    order_mode: string;
    fulfillment_status: string;
    payment_status: string;
    total_amount: string | number;
    currency: string;
    tracking_number?: string | null;
    standardized_status?: string | null;
    sub_status?: string | null;
    is_stagnant_72h?: boolean;
  }>;
  recent_quotes: Array<{
    id: string;
    quote_no: string;
    version: number;
    incoterm: Incoterm;
    total_amount: string | number;
    quote_currency: string;
    status: string;
  }>;
}

interface LiveSkuCheckResult {
  sku: string;
  available_qty: number;
  unit_price_quote_ccy: string;
  moq_tier_applied: number;
  incoterm: Incoterm;
  source_tool_call_id: string;
}

export function ContextDrawer({ customerId }: { customerId: string }) {
  const queryClient = useQueryClient();
  const [activeTab, setActiveTab] = useState<"orders" | "quotes" | "sku">("orders");
  const [mergeDialogOpen, setMergeDialogOpen] = useState(false);
  const [mergeReason, setMergeReason] = useState("Confirmed matching buyer identity");
  const [skuInput, setSkuInput] = useState("");
  const [qtyInput, setQtyInput] = useState(100);
  const [incotermInput, setIncotermInput] = useState<Incoterm>("FOB");
  const [liveSkuResult, setLiveSkuResult] = useState<LiveSkuCheckResult | null>(null);

  const contextQuery = useQuery({
    queryKey: ["identity", "context", customerId],
    enabled: Boolean(customerId),
    queryFn: () =>
      bffFetch<CustomerContextPayload>(`/v1/identity/customers/${customerId}/context`),
  });

  const mergeMutation = useMutation({
    mutationFn: (sourceCustomerId: string) =>
      bffFetch("/v1/identity/merge", {
        method: "POST",
        body: JSON.stringify({
          source_customer_id: sourceCustomerId,
          target_customer_id: customerId,
          reason: mergeReason,
        }),
      }),
    onSuccess: () => {
      setMergeDialogOpen(false);
      void queryClient.invalidateQueries({ queryKey: ["identity", "context", customerId] });
    },
  });

  const splitMutation = useMutation({
    mutationFn: (mergeEventId: string) =>
      bffFetch("/v1/identity/split", {
        method: "POST",
        body: JSON.stringify({
          merge_event_id: mergeEventId,
          reason: "Manual operator split",
        }),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["identity", "context", customerId] });
    },
  });

  const liveSkuMutation = useMutation({
    mutationFn: () => {
      const params = new URLSearchParams({
        sku: skuInput.trim(),
        qty: String(qtyInput),
        incoterm: incotermInput,
      });
      return bffFetch<LiveSkuCheckResult>(`/v1/catalog/live-check?${params.toString()}`);
    },
    onSuccess: (data) => {
      setLiveSkuResult(data);
    },
  });

  const data = contextQuery.data;

  return (
    <aside
      data-testid="inbox-context-drawer"
      className="flex w-88 shrink-0 flex-col overflow-y-auto border-l border-slate-200 bg-white p-3"
    >
      {contextQuery.isLoading && <div className="text-xs text-slate-400">Loading customer...</div>}
      {data && (
        <>
          <div className="border-b border-slate-200 pb-3">
            <div className="flex items-start justify-between gap-2">
              <div>
                <h3 className="text-sm font-bold text-slate-900">{data.customer.display_name}</h3>
                <div className="mt-1 flex flex-wrap gap-1 text-[11px]">
                  <span className="rounded bg-indigo-50 px-1.5 py-0.5 font-medium text-indigo-700">
                    {data.customer.customer_type}
                  </span>
                  <span className="rounded bg-slate-100 px-1.5 py-0.5 text-slate-700">
                    LTV {formatMoney(data.customer.ltv_usd, "USD")}
                  </span>
                </div>
              </div>

              {(data.probable_duplicates.length > 0 || data.merge_events.length > 0) && (
                <button
                  type="button"
                  data-testid="open-merge-dialog-btn"
                  onClick={() => setMergeDialogOpen(true)}
                  className="inline-flex items-center gap-1 rounded border border-amber-300 bg-amber-50 px-2 py-1 text-[11px] font-semibold text-amber-900 hover:bg-amber-100"
                >
                  <GitMerge className="h-3 w-3" />
                  <span>Merge / Split ({data.probable_duplicates.length})</span>
                </button>
              )}
            </div>

            {data.company && (
              <div className="mt-2 rounded-md border border-slate-200 bg-slate-50 p-2 text-xs">
                <div className="flex items-center gap-1.5 font-semibold text-slate-800">
                  <Building2 className="h-3.5 w-3.5 text-slate-500" />
                  <span>{data.company.legal_name}</span>
                  <span>({data.company.country_code})</span>
                </div>
                <div className="mt-0.5 text-[11px] text-slate-600">
                  Credit: {data.company.credit_tier} · Default {data.company.default_incoterm}
                </div>
              </div>
            )}

            <div className="mt-2.5 space-y-1">
              <div className="flex items-center gap-1 text-[11px] font-semibold text-slate-500">
                <Fingerprint className="h-3 w-3" />
                <span>Unified Identity Graph</span>
              </div>
              <div className="flex flex-wrap gap-1">
                {data.identities.map((ident) => (
                  <span
                    key={ident.id}
                    data-testid={`identity-badge-${ident.identity_type}`}
                    className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] ${
                      ident.identity_type === "whatsapp_bsuid"
                        ? "border border-emerald-200 bg-emerald-50 text-emerald-800"
                        : "bg-slate-100 text-slate-700"
                    }`}
                  >
                    <span className="font-semibold">{ident.identity_type}:</span>
                    <span>{ident.identity_value}</span>
                  </span>
                ))}
              </div>
            </div>
          </div>

          <div className="mt-3 grid grid-cols-3 gap-1 rounded-md bg-slate-100 p-1 text-[11px] font-medium">
            <button
              type="button"
              onClick={() => setActiveTab("orders")}
              className={`rounded py-1 ${
                activeTab === "orders" ? "bg-white text-indigo-700 shadow-xs" : "text-slate-600"
              }`}
            >
              B2C Orders + 17TRACK
            </button>
            <button
              type="button"
              onClick={() => setActiveTab("quotes")}
              className={`rounded py-1 ${
                activeTab === "quotes" ? "bg-white text-indigo-700 shadow-xs" : "text-slate-600"
              }`}
            >
              B2B Quotes & PI
            </button>
            <button
              type="button"
              onClick={() => setActiveTab("sku")}
              className={`rounded py-1 ${
                activeTab === "sku" ? "bg-white text-indigo-700 shadow-xs" : "text-slate-600"
              }`}
            >
              Live SKU & MOQ Stock
            </button>
          </div>

          <div className="mt-3 flex-1 space-y-2">
            {activeTab === "orders" &&
              data.recent_orders.map((ord) => (
                <div
                  key={ord.id}
                  className="rounded-md border border-slate-200 p-2.5 text-xs"
                >
                  <div className="flex items-center justify-between font-semibold text-slate-900">
                    <Link href={`/orders?orderId=${ord.id}`} className="text-indigo-600 hover:underline">
                      {ord.order_no}
                    </Link>
                    <span>{formatMoney(ord.total_amount, ord.currency)}</span>
                  </div>
                  <div className="mt-1 flex flex-wrap gap-1 text-[11px]">
                    <span className="rounded bg-slate-100 px-1.5 py-0.5">{ord.fulfillment_status}</span>
                    <span className="rounded bg-slate-100 px-1.5 py-0.5">{ord.payment_status}</span>
                  </div>
                  {ord.tracking_number && (
                    <div className="mt-1.5 flex items-center gap-1 text-[11px] text-slate-600">
                      <Truck className="h-3 w-3" />
                      <span>
                        {ord.tracking_number} · {ord.standardized_status ?? "Pending"}
                        {ord.sub_status ? ` (${ord.sub_status})` : ""}
                      </span>
                    </div>
                  )}
                </div>
              ))}

            {activeTab === "quotes" &&
              data.recent_quotes.map((q) => (
                <div key={q.id} className="rounded-md border border-slate-200 p-2.5 text-xs">
                  <div className="flex items-center justify-between font-semibold text-slate-900">
                    <Link
                      href={`/b2b/quotes?quoteId=${q.id}`}
                      className="inline-flex items-center gap-1 text-indigo-600 hover:underline"
                    >
                      <FileText className="h-3 w-3" />
                      <span>{q.quote_no}</span>
                      <span>v{q.version}</span>
                    </Link>
                    <span>{formatMoney(q.total_amount, q.quote_currency)}</span>
                  </div>
                  <div className="mt-1 text-[11px] text-slate-600">
                    Incoterm: {q.incoterm} · Status: {q.status}
                  </div>
                </div>
              ))}

            {activeTab === "sku" && (
              <div className="space-y-2 rounded-md border border-slate-200 p-2.5 text-xs">
                <div className="flex items-center gap-1 font-semibold text-slate-800">
                  <PackageSearch className="h-3.5 w-3.5 text-indigo-600" />
                  <span>Deterministic SQL Stock & MOQ Lookup</span>
                </div>
                <input
                  data-testid="live-sku-input"
                  type="text"
                  value={skuInput}
                  onChange={(e) => setSkuInput(e.target.value)}
                  placeholder="SKU e.g. LED-COB-24V-5M"
                  className="w-full rounded border border-slate-300 px-2 py-1 text-xs"
                />
                <div className="flex gap-2">
                  <input
                    type="number"
                    min={1}
                    value={qtyInput}
                    onChange={(e) => setQtyInput(Number(e.target.value) || 1)}
                    className="w-24 rounded border border-slate-300 px-2 py-1 text-xs"
                  />
                  <select
                    value={incotermInput}
                    onChange={(e) => setIncotermInput(e.target.value as Incoterm)}
                    className="rounded border border-slate-300 px-2 py-1 text-xs"
                  >
                    <option value="EXW">EXW</option>
                    <option value="FOB">FOB</option>
                    <option value="CIF">CIF</option>
                    <option value="DDP">DDP</option>
                    <option value="DAP">DAP</option>
                  </select>
                  <button
                    type="button"
                    data-testid="live-sku-check-btn"
                    disabled={!skuInput.trim() || liveSkuMutation.isPending}
                    onClick={() => liveSkuMutation.mutate()}
                    className="rounded bg-indigo-600 px-2.5 py-1 font-semibold text-white hover:bg-indigo-700 disabled:opacity-50"
                  >
                    Check
                  </button>
                </div>

                {liveSkuResult && (
                  <div className="rounded bg-emerald-50 p-2 text-emerald-950">
                    <div>
                      Available Stock: <strong>{liveSkuResult.available_qty}</strong>
                    </div>
                    <div>
                      Unit Price ({liveSkuResult.incoterm}, MOQ {liveSkuResult.moq_tier_applied}):{" "}
                      <strong>{formatMoney(liveSkuResult.unit_price_quote_ccy, "USD")}</strong>
                    </div>
                    <div className="mt-1 text-[10px] text-emerald-700">
                      Tool Grounding ID: {liveSkuResult.source_tool_call_id}
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>

          {mergeDialogOpen && (
            <div
              role="dialog"
              aria-modal="true"
              className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
            >
              <div className="w-full max-w-md rounded-lg bg-white p-4 shadow-lg">
                <h4 className="text-sm font-bold text-slate-900">
                  Identity Merge & Reversible Split
                </h4>
                <div className="mt-2">
                  <label className="block text-xs font-medium text-slate-600">Audit Reason</label>
                  <input
                    type="text"
                    value={mergeReason}
                    onChange={(e) => setMergeReason(e.target.value)}
                    className="mt-1 w-full rounded border border-slate-300 px-2.5 py-1.5 text-xs"
                  />
                </div>

                <div className="mt-3 space-y-2">
                  {data.probable_duplicates.map((dup) => (
                    <div
                      key={dup.candidate_customer_id}
                      className="flex items-center justify-between rounded border border-slate-200 p-2 text-xs"
                    >
                      <div>
                        <div className="font-semibold">{dup.display_name}</div>
                        <div className="text-[11px] text-slate-500">
                          {dup.matched_signal} ({Math.round(dup.similarity_score * 100)}%)
                        </div>
                      </div>
                      <button
                        type="button"
                        data-testid={`confirm-merge-${dup.candidate_customer_id}`}
                        onClick={() => mergeMutation.mutate(dup.candidate_customer_id)}
                        className="rounded bg-indigo-600 px-2.5 py-1 text-xs font-semibold text-white"
                      >
                        Merge
                      </button>
                    </div>
                  ))}

                  {data.merge_events
                    .filter((e) => e.action === "merge")
                    .map((ev) => (
                      <div
                        key={ev.id}
                        className="flex items-center justify-between rounded border border-slate-200 p-2 text-xs"
                      >
                        <span>Merged event {ev.id}</span>
                        <button
                          type="button"
                          onClick={() => splitMutation.mutate(ev.id)}
                          className="inline-flex items-center gap-1 rounded border border-rose-300 px-2 py-1 text-rose-700"
                        >
                          <Split className="h-3 w-3" />
                          <span>Split</span>
                        </button>
                      </div>
                    ))}
                </div>

                <div className="mt-4 flex justify-end">
                  <button
                    type="button"
                    onClick={() => setMergeDialogOpen(false)}
                    className="rounded border border-slate-300 px-3 py-1 text-xs font-medium"
                  >
                    Close
                  </button>
                </div>
              </div>
            </div>
          )}
        </>
      )}
    </aside>
  );
}
