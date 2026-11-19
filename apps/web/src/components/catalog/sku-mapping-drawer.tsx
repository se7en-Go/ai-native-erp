"use client";

import React, { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2, X, Lock } from "lucide-react";
import { updateSkuMapping, updateVariant } from "@/lib/api-client/live-client";
import { fetchVariantDetail } from "@/lib/data-source";
import { isPrivilegedCostViewer } from "@/lib/auth-cookie";
import { formatMoney, type UserRole } from "@/lib/contracts";

export interface MoqTier {
  min_qty: number;
  unit_price_usd: number;
}

export interface CatalogVariantItem {
  id: string;
  product_id: string;
  spu_code: string;
  category: string;
  hs_code?: string | null;
  certifications: string[];
  sku: string;
  attributes: Record<string, string>;
  cost_price_base?: string | null;
  base_currency: string;
  min_margin_floor_pct: string;
  retail_price_usd: string;
  b2b_moq_tiers: MoqTier[];
  lead_time_days: number;
}

export interface PlatformSkuMappingItem {
  id: string;
  variant_id: string;
  channel_account_id: string;
  channel_label?: string;
  external_product_id: string;
  external_sku: string;
  bundle_multiplier: number;
  sync_inventory_enabled: boolean;
}

export function SkuMappingDrawer({
  variantId,
  role,
  onClose,
}: {
  variantId: string;
  role: UserRole | undefined;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const canViewCost = isPrivilegedCostViewer(role);

  const detailQuery = useQuery({
    queryKey: ["catalog", "variant", variantId, "mappings"],
    queryFn: () => fetchVariantDetail(variantId),
  });

  const [tiers, setTiers] = useState<MoqTier[]>([]);
  const [multipliers, setMultipliers] = useState<Record<string, number>>({});

  useEffect(() => {
    if (detailQuery.data) {
      setTiers(detailQuery.data.variant.b2b_moq_tiers ?? []);
      const nextMult: Record<string, number> = {};
      for (const m of detailQuery.data.mappings) {
        nextMult[m.id] = m.bundle_multiplier;
      }
      setMultipliers(nextMult);
    }
  }, [detailQuery.data]);

  const saveTiersMutation = useMutation({
    mutationFn: () => updateVariant(variantId, { b2b_moq_tiers: tiers }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["catalog", "variants"] });
      void queryClient.invalidateQueries({
        queryKey: ["catalog", "variant", variantId, "mappings"],
      });
    },
  });

  const saveMappingMutation = useMutation({
    mutationFn: (mapping: PlatformSkuMappingItem) =>
      updateSkuMapping(mapping.id, {
        external_sku: mapping.external_sku,
        bundle_multiplier: multipliers[mapping.id] ?? mapping.bundle_multiplier,
        sync_inventory_enabled: mapping.sync_inventory_enabled,
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: ["catalog", "variant", variantId, "mappings"],
      });
    },
  });

  const variant = detailQuery.data?.variant;
  const mappings = detailQuery.data?.mappings ?? [];

  return (
    <div
      data-testid="sku-mapping-drawer"
      className="fixed inset-y-0 right-0 z-50 flex w-full max-w-lg flex-col border-l border-slate-200 bg-white shadow-2xl"
    >
      <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
        <div>
          <h3 className="text-sm font-bold text-slate-900">
            SKU Master & Multi-Platform Mapping
          </h3>
          {variant && <p className="text-xs text-slate-500">{variant.sku}</p>}
        </div>
        <button
          type="button"
          onClick={onClose}
          className="rounded p-1 text-slate-500 hover:bg-slate-100"
          aria-label="Close SKU drawer"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="flex-1 space-y-5 overflow-y-auto p-4 text-xs">
        {variant && (
          <div className="rounded-md border border-slate-200 bg-slate-50 p-3 space-y-1.5">
            <div className="flex justify-between">
              <span className="text-slate-500">Retail Price (USD):</span>
              <strong>{formatMoney(variant.retail_price_usd, "USD")}</strong>
            </div>
            <div className="flex justify-between">
              <span className="text-slate-500">Min Margin Floor:</span>
              <strong>{variant.min_margin_floor_pct}%</strong>
            </div>
            {canViewCost ? (
              <div className="flex justify-between">
                <span className="text-slate-500">Base Cost ({variant.base_currency}):</span>
                <strong>{formatMoney(variant.cost_price_base, variant.base_currency)}</strong>
              </div>
            ) : (
              <div className="flex items-center gap-1 text-slate-500">
                <Lock className="h-3.5 w-3.5" />
                <span>Base manufacturing cost hidden for Agent role</span>
              </div>
            )}
          </div>
        )}

        <div>
          <div className="flex items-center justify-between">
            <h4 className="font-bold text-slate-900">B2B MOQ Tier Pricing (USD)</h4>
            <button
              type="button"
              onClick={() =>
                setTiers((prev) => [...prev, { min_qty: 100, unit_price_usd: 15.0 }])
              }
              className="inline-flex items-center gap-1 rounded border border-slate-300 px-2 py-1 font-medium hover:bg-slate-100"
            >
              <Plus className="h-3 w-3" />
              <span>Add Tier</span>
            </button>
          </div>

          <div className="mt-2 space-y-2">
            {tiers.map((tier, idx) => (
              <div key={idx} className="flex items-center gap-2">
                <label className="text-slate-500">Min Qty</label>
                <input
                  type="number"
                  min={1}
                  value={tier.min_qty}
                  onChange={(e) => {
                    const next = [...tiers];
                    next[idx] = { ...tier, min_qty: Number(e.target.value) || 1 };
                    setTiers(next);
                  }}
                  className="w-24 rounded border border-slate-300 px-2 py-1"
                />
                <label className="text-slate-500">Unit $</label>
                <input
                  type="number"
                  step="0.01"
                  value={tier.unit_price_usd}
                  onChange={(e) => {
                    const next = [...tiers];
                    next[idx] = { ...tier, unit_price_usd: Number(e.target.value) || 0 };
                    setTiers(next);
                  }}
                  className="w-24 rounded border border-slate-300 px-2 py-1"
                />
                <button
                  type="button"
                  onClick={() => setTiers((prev) => prev.filter((_, i) => i !== idx))}
                  className="rounded p-1 text-rose-600 hover:bg-rose-50"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </div>
            ))}
          </div>

          <button
            type="button"
            onClick={() => saveTiersMutation.mutate()}
            className="mt-2 rounded bg-indigo-600 px-3 py-1.5 font-semibold text-white hover:bg-indigo-700"
          >
            Save MOQ Tiers
          </button>
        </div>

        <div>
          <h4 className="font-bold text-slate-900">
            Platform SKU Mappings & Bundle Multiplier
          </h4>
          <p className="mt-0.5 text-[11px] text-slate-500">
            Bundle multiplier deducts N master SKUs per 1 storefront listing unit sold (e.g. 2-pack = 2).
          </p>

          <div className="mt-2 space-y-2">
            {mappings.map((mapping) => (
              <div
                key={mapping.id}
                className="rounded-md border border-slate-200 p-2.5 space-y-2"
              >
                <div className="flex items-center justify-between font-semibold text-slate-800">
                  <span>{mapping.channel_label ?? mapping.channel_account_id}</span>
                  <span className="rounded bg-slate-100 px-1.5 py-0.5">{mapping.external_sku}</span>
                </div>
                <div className="flex items-center gap-2">
                  <label className="text-slate-600">bundle_multiplier:</label>
                  <input
                    type="number"
                    min={1}
                    data-testid={`bundle-multiplier-input-${mapping.id}`}
                    value={multipliers[mapping.id] ?? mapping.bundle_multiplier}
                    onChange={(e) =>
                      setMultipliers((prev) => ({
                        ...prev,
                        [mapping.id]: Math.max(1, Number(e.target.value) || 1),
                      }))
                    }
                    className="w-20 rounded border border-slate-300 px-2 py-1"
                  />
                  <button
                    type="button"
                    data-testid={`save-mapping-btn-${mapping.id}`}
                    onClick={() => saveMappingMutation.mutate(mapping)}
                    className="rounded bg-slate-900 px-2.5 py-1 font-semibold text-white hover:bg-slate-800"
                  >
                    Save
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
