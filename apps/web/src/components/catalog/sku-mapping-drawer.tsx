"use client";

import React, { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Lock, Plus, Trash2, X } from "lucide-react";
import { updateSkuMapping, updateVariant } from "@/lib/api-client/live-client";
import { fetchVariantDetail } from "@/lib/data-source";
import { isPrivilegedCostViewer } from "@/lib/auth-cookie";
import { useI18n } from "@/lib/i18n";
import { formatMoney, type UserRole } from "@/lib/contracts";
import { Badge } from "@/components/ui/page";

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
  const { bi } = useI18n();
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
      className="fixed inset-y-0 right-0 z-50 flex w-full max-w-xl flex-col border-l border-slate-200 bg-white shadow-2xl"
    >
      <div className="flex items-center justify-between border-b border-slate-200 bg-slate-50/70 px-4 py-3">
        <div>
          <h3 className="text-[14px] font-bold text-slate-900">
            {bi("SKU 主档与多平台映射", "SKU master & platform mapping")}
          </h3>
          {variant && (
            <p className="mt-0.5 font-mono text-[11px] font-semibold text-blue-700">{variant.sku}</p>
          )}
        </div>
        <button
          type="button"
          onClick={onClose}
          className="rounded-lg border border-slate-200 bg-white p-1.5 text-slate-600 shadow-xs transition-colors hover:bg-slate-100"
          aria-label={bi("关闭 SKU 抽屉", "Close SKU drawer")}
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="flex-1 space-y-5 overflow-y-auto p-4 text-[13px]">
        {variant && (
          <div className="space-y-1.5 rounded-2xl border border-slate-200 bg-gradient-to-b from-slate-50 to-white p-3.5 ring-1 ring-slate-100">
            <div className="flex justify-between">
              <span className="text-slate-600">{bi("零售价（USD）", "Retail price (USD)")}</span>
              <strong className="tabular-nums text-slate-900">
                {formatMoney(variant.retail_price_usd, "USD")}
              </strong>
            </div>
            <div className="flex justify-between">
              <span className="text-slate-500">{bi("最低毛利底线", "Min margin floor")}</span>
              <strong className="tabular-nums">{variant.min_margin_floor_pct}%</strong>
            </div>
            {canViewCost ? (
              <div className="flex justify-between">
                <span className="text-slate-500">
                  {bi("基础成本", "Base cost")}（{variant.base_currency}）
                </span>
                <strong className="tabular-nums">
                  {formatMoney(variant.cost_price_base, variant.base_currency)}
                </strong>
              </div>
            ) : (
              <div className="flex items-center gap-1.5 text-[12px] text-amber-700">
                <Lock className="h-3.5 w-3.5" />
                <span>{bi("成本价对业务员角色隐藏", "Base manufacturing cost hidden for agents")}</span>
              </div>
            )}
          </div>
        )}

        <div>
          <div className="flex items-center justify-between">
            <h4 className="text-sm font-semibold text-slate-900">
              {bi("B2B MOQ 阶梯价（USD）", "B2B MOQ tier pricing (USD)")}
            </h4>
            <button
              type="button"
              onClick={() =>
                setTiers((prev) => [...prev, { min_qty: 100, unit_price_usd: 15.0 }])
              }
              className="erp-btn erp-btn-secondary py-1 text-[12px]"
            >
              <Plus className="h-3 w-3" />
              <span>{bi("新增阶梯", "Add tier")}</span>
            </button>
          </div>

          <div className="mt-2 space-y-2">
            {tiers.map((tier, idx) => (
              <div
                key={idx}
                className="flex flex-wrap items-center gap-2 rounded-xl border border-slate-200/80 bg-slate-50/50 px-2.5 py-2"
              >
                <label className="text-[12px] text-slate-500" htmlFor={`tier-qty-${idx}`}>
                  {bi("起订量", "Min qty")}
                </label>
                <input
                  id={`tier-qty-${idx}`}
                  type="number"
                  min={1}
                  value={tier.min_qty}
                  onChange={(e) => {
                    const next = [...tiers];
                    next[idx] = { ...tier, min_qty: Number(e.target.value) || 1 };
                    setTiers(next);
                  }}
                  className="erp-input w-24 py-1 tabular-nums"
                />
                <label className="text-[12px] text-slate-500" htmlFor={`tier-price-${idx}`}>
                  {bi("单价 $", "Unit $")}
                </label>
                <input
                  id={`tier-price-${idx}`}
                  type="number"
                  step="0.01"
                  value={tier.unit_price_usd}
                  onChange={(e) => {
                    const next = [...tiers];
                    next[idx] = { ...tier, unit_price_usd: Number(e.target.value) || 0 };
                    setTiers(next);
                  }}
                  className="erp-input w-24 py-1 tabular-nums"
                />
                <button
                  type="button"
                  onClick={() => setTiers((prev) => prev.filter((_, i) => i !== idx))}
                  className="rounded-md p-1.5 text-rose-600 hover:bg-rose-50"
                  aria-label={bi("删除该阶梯", "Delete tier")}
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </div>
            ))}
          </div>

          <button
            type="button"
            onClick={() => saveTiersMutation.mutate()}
            className="erp-btn erp-btn-primary mt-3 rounded-xl shadow-[0_2px_8px_-2px_rgba(37,99,235,0.55)]"
          >
            {bi("保存阶梯价", "Save MOQ tiers")}
          </button>
        </div>

        <div>
          <h4 className="text-sm font-semibold text-slate-900">
            {bi("平台 SKU 映射与组合装倍率", "Platform SKU mappings & bundle multiplier")}
          </h4>
          <p className="mt-1 text-[12px] leading-5 text-slate-500">
            {bi(
              "组合装倍率 = 每卖出 1 个店铺 listing 扣减多少个主档 SKU（例如 2 支装 = 2）。",
              "Bundle multiplier = how many master SKUs are deducted per storefront listing sold (2-pack = 2)."
            )}
          </p>

          <div className="mt-2 space-y-2">
            {mappings.map((mapping) => (
              <div
                key={mapping.id}
                className="space-y-2 rounded-xl border border-slate-200 bg-white p-3 shadow-[0_1px_2px_rgba(15,23,42,0.04)]"
              >
                <div className="flex flex-wrap items-center justify-between gap-2 font-medium text-slate-800">
                  <span>{mapping.channel_label ?? mapping.channel_account_id}</span>
                  <Badge>{mapping.external_sku}</Badge>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <label
                    className="text-[12px] text-slate-600"
                    htmlFor={`bundle-multiplier-input-${mapping.id}`}
                  >
                    {bi("组合装倍率", "Bundle multiplier")}
                  </label>
                  <input
                    id={`bundle-multiplier-input-${mapping.id}`}
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
                    className="erp-input w-20 py-1 tabular-nums"
                  />
                  <button
                    type="button"
                    data-testid={`save-mapping-btn-${mapping.id}`}
                    onClick={() => saveMappingMutation.mutate(mapping)}
                    className="erp-btn rounded-lg bg-slate-900 text-white hover:bg-slate-800"
                  >
                    {bi("保存", "Save")}
                  </button>
                </div>
              </div>
            ))}
            {mappings.length === 0 && (
              <p className="rounded-xl border border-dashed border-slate-200 p-4 text-center text-[12px] text-slate-400">
                {bi("尚未绑定任何平台 listing。", "No platform listings mapped yet.")}
              </p>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
