"use client";

import React, { useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Clock, FileDown, CheckCircle2, RefreshCw, ArrowRightCircle } from "lucide-react";
import {
  acceptQuote,
  convertQuoteToOrder,
  generateQuotePdf,
  repriceQuoteVersion,
} from "@/lib/api-client/live-client";
import { formatMoney, type Incoterm, type QuoteStatus } from "@/lib/contracts";
import type { CatalogVariantItem, MoqTier } from "@/components/catalog/sku-mapping-drawer";

export interface QuoteItemView {
  id: string;
  variant_id: string;
  sku_snapshot: string;
  quantity: number;
  moq_tier_applied?: number | null;
  unit_price_quote_ccy: string;
  line_total_quote_ccy: string;
}

export interface QuoteRecord {
  id: string;
  opportunity_id: string;
  quote_no: string;
  version: number;
  is_proforma_invoice: boolean;
  incoterm: Incoterm;
  port_of_loading?: string | null;
  port_of_destination?: string | null;
  payment_terms: string;
  base_currency: string;
  quote_currency: string;
  fx_rate_snapshot: string;
  fx_locked_until: string;
  freight_fee: string;
  insurance_fee: string;
  customs_duty_estimate: string;
  total_amount: string;
  pdf_r2_key?: string | null;
  download_url?: string | null;
  status: QuoteStatus;
  converted_order_id?: string | null;
  items: QuoteItemView[];
}

export function resolveMoqTierPrice(
  tiers: MoqTier[],
  qty: number,
  fallbackPriceUsd: number
): { tierMinQty: number; unitPriceUsd: number } {
  const sorted = [...tiers].sort((a, b) => b.min_qty - a.min_qty);
  const matched = sorted.find((t) => qty >= t.min_qty);
  if (matched) {
    return { tierMinQty: matched.min_qty, unitPriceUsd: matched.unit_price_usd };
  }
  return { tierMinQty: 1, unitPriceUsd: fallbackPriceUsd };
}

export function formatFxCountdown(fxLockedUntilIso: string): {
  expired: boolean;
  label: string;
} {
  const ms = new Date(fxLockedUntilIso).getTime() - Date.now();
  if (Number.isNaN(ms) || ms <= 0) {
    return { expired: true, label: "FX Lock Expired — Renewal Required" };
  }
  const totalMinutes = Math.floor(ms / 60_000);
  const hours = Math.floor(totalMinutes / 60);
  const mins = totalMinutes % 60;
  return { expired: false, label: `FX Locked: ${hours}h ${mins}m remaining` };
}

export function QuotePiBuilder({
  quotes,
  variants,
}: {
  quotes: QuoteRecord[];
  variants: CatalogVariantItem[];
}) {
  const queryClient = useQueryClient();
  const [selectedQuoteId, setSelectedQuoteId] = useState<string>(quotes[0]?.id ?? "");
  const [previewVariantId, setPreviewVariantId] = useState<string>(variants[0]?.id ?? "");
  const [previewQty, setPreviewQty] = useState<number>(500);
  const [previewIncoterm, setPreviewIncoterm] = useState<Incoterm>("FOB");
  const [convertedBanner, setConvertedBanner] = useState<string | null>(null);
  const [pdfPreviewUrl, setPdfPreviewUrl] = useState<string | null>(null);

  const activeQuote = quotes.find((q) => q.id === selectedQuoteId) ?? quotes[0] ?? null;
  const activeVariant = variants.find((v) => v.id === previewVariantId) ?? variants[0] ?? null;

  const moqPreview = useMemo(() => {
    if (!activeVariant) return null;
    return resolveMoqTierPrice(
      activeVariant.b2b_moq_tiers,
      previewQty,
      Number(activeVariant.retail_price_usd)
    );
  }, [activeVariant, previewQty]);

  const pdfMutation = useMutation({
    mutationFn: (quoteId: string) => generateQuotePdf(quoteId),
    onSuccess: (res) => {
      setPdfPreviewUrl(res.download_url);
      void queryClient.invalidateQueries({ queryKey: ["b2b", "quotes"] });
    },
  });

  const acceptMutation = useMutation({
    mutationFn: (quoteId: string) => acceptQuote(quoteId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["b2b", "quotes"] });
    },
  });

  const renewFxMutation = useMutation({
    mutationFn: (quoteId: string) =>
      repriceQuoteVersion(
        quoteId,
        activeQuote?.fx_rate_snapshot ?? "7.120000",
        new Date(Date.now() + 1000 * 60 * 60 * 48).toISOString(),
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["b2b", "quotes"] });
    },
  });

  const convertMutation = useMutation({
    mutationFn: (quoteId: string) => convertQuoteToOrder(quoteId),
    onSuccess: (res) => {
      setConvertedBanner(`Converted 1-Click to Order ${res.order.order_no} (${res.order.id})`);
      void queryClient.invalidateQueries({ queryKey: ["b2b", "quotes"] });
      void queryClient.invalidateQueries({ queryKey: ["orders"] });
    },
  });

  return (
    <div className="grid gap-4 lg:grid-cols-3">
      <div className="rounded-lg border border-slate-200 bg-white p-4 text-xs space-y-3">
        <h3 className="text-sm font-bold text-slate-900">MOQ Tier Auto-Price Calculator</h3>
        <div>
          <label className="block font-medium text-slate-600">Select SKU</label>
          <select
            value={activeVariant?.id ?? ""}
            onChange={(e) => setPreviewVariantId(e.target.value)}
            className="mt-1 w-full rounded border border-slate-300 px-2 py-1.5"
          >
            {variants.map((v) => (
              <option key={v.id} value={v.id}>
                {v.sku}
              </option>
            ))}
          </select>
        </div>
        <div className="grid grid-cols-2 gap-2">
          <div>
            <label className="block font-medium text-slate-600">Order Qty</label>
            <input
              type="number"
              min={1}
              data-testid="moq-preview-qty-input"
              value={previewQty}
              onChange={(e) => setPreviewQty(Math.max(1, Number(e.target.value) || 1))}
              className="mt-1 w-full rounded border border-slate-300 px-2 py-1.5"
            />
          </div>
          <div>
            <label className="block font-medium text-slate-600">Incoterm</label>
            <select
              value={previewIncoterm}
              onChange={(e) => setPreviewIncoterm(e.target.value as Incoterm)}
              className="mt-1 w-full rounded border border-slate-300 px-2 py-1.5"
            >
              <option value="EXW">EXW</option>
              <option value="FOB">FOB</option>
              <option value="CIF">CIF</option>
              <option value="DDP">DDP</option>
              <option value="DAP">DAP</option>
            </select>
          </div>
        </div>
        {moqPreview && (
          <div className="rounded-md border border-indigo-200 bg-indigo-50 p-3 space-y-1">
            <div className="text-slate-700">
              Applied Tier: <strong>MOQ {moqPreview.tierMinQty}+</strong> ({previewIncoterm})
            </div>
            <div data-testid="moq-preview-price" className="text-base font-bold text-indigo-900">
              {formatMoney(moqPreview.unitPriceUsd, "USD")} / pc
            </div>
            <div className="text-slate-600">
              Est. Subtotal: {formatMoney(moqPreview.unitPriceUsd * previewQty, "USD")}
            </div>
          </div>
        )}
      </div>

      <div className="lg:col-span-2 rounded-lg border border-slate-200 bg-white p-4 text-xs space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3 className="text-sm font-bold text-slate-900">Versioned Proforma Invoice (PI)</h3>
          <select
            value={activeQuote?.id ?? ""}
            onChange={(e) => setSelectedQuoteId(e.target.value)}
            className="rounded border border-slate-300 px-2 py-1 text-xs"
          >
            {quotes.map((q) => (
              <option key={q.id} value={q.id}>
                {q.quote_no} (v{q.version} · {q.status})
              </option>
            ))}
          </select>
        </div>

        {convertedBanner && (
          <div className="rounded border border-emerald-300 bg-emerald-50 px-3 py-2 font-semibold text-emerald-900">
            {convertedBanner}
          </div>
        )}

        {convertMutation.isError && (
          <p role="alert" className="rounded border border-rose-200 bg-rose-50 p-2 text-xs text-rose-800">
            {convertMutation.error.message}
          </p>
        )}

        {activeQuote ? (
          (() => {
            const fxState = formatFxCountdown(activeQuote.fx_locked_until);
            const canConvert = activeQuote.status === "accepted" && !fxState.expired;
            const downloadHref = pdfPreviewUrl ?? activeQuote.download_url ?? null;

            return (
              <div className="space-y-3">
                <div className="flex flex-wrap items-center justify-between gap-2 rounded bg-slate-50 p-2.5">
                  <div>
                    <span className="text-sm font-bold text-slate-900">{activeQuote.quote_no}</span>
                    <span className="ml-2 rounded bg-slate-200 px-1.5 py-0.5">
                      v{activeQuote.version}
                    </span>
                    <span className="ml-2 rounded bg-indigo-100 px-1.5 py-0.5 font-semibold text-indigo-800">
                      {activeQuote.incoterm}
                    </span>
                    <span className="ml-2 rounded bg-slate-200 px-1.5 py-0.5 font-semibold">
                      {activeQuote.status}
                    </span>
                  </div>

                  <span
                    data-testid={`fx-lock-badge-${activeQuote.id}`}
                    className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 font-semibold ${
                      fxState.expired
                        ? "bg-rose-100 text-rose-800"
                        : "bg-emerald-100 text-emerald-800"
                    }`}
                  >
                    <Clock className="h-3 w-3" />
                    <span>{fxState.label}</span>
                  </span>
                </div>

                <div className="grid grid-cols-2 gap-2 text-slate-600">
                  <div>
                    Route: {activeQuote.port_of_loading ?? "Shenzhen"} →{" "}
                    {activeQuote.port_of_destination ?? "Buyer Port"}
                  </div>
                  <div>
                    FX Snapshot: 1 {activeQuote.quote_currency} = {activeQuote.fx_rate_snapshot}{" "}
                    {activeQuote.base_currency}
                  </div>
                  <div className="col-span-2">Payment Terms: {activeQuote.payment_terms}</div>
                </div>

                <table className="w-full border border-slate-200 text-left">
                  <thead className="bg-slate-50 text-slate-600">
                    <tr>
                      <th className="px-2 py-1.5">SKU</th>
                      <th className="px-2 py-1.5">Qty</th>
                      <th className="px-2 py-1.5">Unit Price</th>
                      <th className="px-2 py-1.5">Line Total</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {activeQuote.items.map((item) => (
                      <tr key={item.id}>
                        <td className="px-2 py-1.5 font-medium">{item.sku_snapshot}</td>
                        <td className="px-2 py-1.5">{item.quantity}</td>
                        <td className="px-2 py-1.5">
                          {formatMoney(item.unit_price_quote_ccy, activeQuote.quote_currency)}
                        </td>
                        <td className="px-2 py-1.5 font-semibold">
                          {formatMoney(item.line_total_quote_ccy, activeQuote.quote_currency)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>

                <div className="flex flex-wrap items-center justify-between gap-2 border-t border-slate-200 pt-3">
                  <div className="text-sm font-bold text-slate-900">
                    Total: {formatMoney(activeQuote.total_amount, activeQuote.quote_currency)}
                  </div>

                  <div className="flex flex-wrap gap-2">
                    <button
                      type="button"
                      onClick={() => pdfMutation.mutate(activeQuote.id)}
                      className="inline-flex items-center gap-1 rounded border border-slate-300 px-2.5 py-1.5 font-semibold text-slate-700 hover:bg-slate-100"
                    >
                      <FileDown className="h-3.5 w-3.5" />
                      <span>Generate PI PDF</span>
                    </button>

                    {downloadHref && (
                      <a
                        href={downloadHref}
                        target="_blank"
                        rel="noreferrer"
                        className="inline-flex items-center gap-1 rounded border border-indigo-200 bg-indigo-50 px-2.5 py-1.5 font-semibold text-indigo-700"
                      >
                        Preview PDF
                      </a>
                    )}

                    {fxState.expired && (
                      <button
                        type="button"
                        onClick={() => renewFxMutation.mutate(activeQuote.id)}
                        className="inline-flex items-center gap-1 rounded border border-amber-300 bg-amber-50 px-2.5 py-1.5 font-semibold text-amber-900"
                      >
                        <RefreshCw className="h-3.5 w-3.5" />
                        <span>Renew FX Lock</span>
                      </button>
                    )}

                    {activeQuote.status !== "accepted" && activeQuote.status !== "converted" && (
                      <button
                        type="button"
                        onClick={() => acceptMutation.mutate(activeQuote.id)}
                        className="inline-flex items-center gap-1 rounded bg-slate-800 px-2.5 py-1.5 font-semibold text-white"
                      >
                        <CheckCircle2 className="h-3.5 w-3.5" />
                        <span>Mark Accepted</span>
                      </button>
                    )}

                    <button
                      type="button"
                      data-testid={`convert-quote-btn-${activeQuote.id}`}
                      disabled={!canConvert || convertMutation.isPending}
                      onClick={() => convertMutation.mutate(activeQuote.id)}
                      className="inline-flex items-center gap-1 rounded bg-emerald-600 px-3 py-1.5 font-semibold text-white hover:bg-emerald-700 disabled:opacity-50"
                    >
                      <ArrowRightCircle className="h-3.5 w-3.5" />
                      <span>1-Click Convert to Order</span>
                    </button>
                  </div>
                </div>
              </div>
            );
          })()
        ) : (
          <div className="p-4 text-center text-slate-400">No quotes yet.</div>
        )}
      </div>
    </div>
  );
}
