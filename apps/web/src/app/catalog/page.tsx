"use client";

import React, { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AppShell } from "@/components/app-shell";
import { useAuthSession } from "@/components/providers";
import { SkuMappingDrawer } from "@/components/catalog/sku-mapping-drawer";
import {
  fetchCatalogVariants,
  fetchPendingProposalsCount,
} from "@/lib/data-source";
import { isPrivilegedCostViewer } from "@/lib/auth-cookie";
import { formatMoney } from "@/lib/contracts";

export default function CatalogPage() {
  const { principal } = useAuthSession();
  const [search, setSearch] = useState("");
  const [selectedVariantId, setSelectedVariantId] = useState<string | null>(null);

  const canViewCost = isPrivilegedCostViewer(principal?.role);

  const pendingProposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () => fetchPendingProposalsCount(),
  });

  const variantsQuery = useQuery({
    queryKey: ["catalog", "variants", search],
    queryFn: () => fetchCatalogVariants(search),
  });

  const variants = variantsQuery.data?.items ?? [];

  return (
    <AppShell
      currentPath="/catalog"
      pendingProposalsCount={pendingProposalsQuery.data?.total ?? 0}
    >
      <div className="mx-auto max-w-6xl space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="text-lg font-bold text-slate-900">
              Unified Product Catalog & Platform SKU Mappings
            </h1>
            <p className="text-xs text-slate-500">
              Single master SKU source for B2B MOQ tier quoting and B2C storefront bundle deductions.
            </p>
          </div>
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search SPU, SKU, or HS code..."
            className="w-64 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs"
          />
        </div>

        <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
          <table className="w-full text-left text-xs">
            <thead className="border-b border-slate-200 bg-slate-50 font-semibold text-slate-600">
              <tr>
                <th className="px-3 py-2.5">SKU / SPU</th>
                <th className="px-3 py-2.5">Category & HS</th>
                <th className="px-3 py-2.5">Certifications</th>
                <th className="px-3 py-2.5">Retail (USD)</th>
                {canViewCost && (
                  <th data-testid="catalog-cost-header" className="px-3 py-2.5">
                    Base Cost
                  </th>
                )}
                <th className="px-3 py-2.5">MOQ Tiers</th>
                <th className="px-3 py-2.5">Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {variants.map((v) => (
                <tr key={v.id} className="hover:bg-slate-50">
                  <td className="px-3 py-2.5">
                    <div className="font-semibold text-slate-900">{v.sku}</div>
                    <div className="text-[11px] text-slate-500">{v.spu_code}</div>
                  </td>
                  <td className="px-3 py-2.5">
                    <div>{v.category}</div>
                    {v.hs_code && <div className="text-[11px] text-slate-500">HS {v.hs_code}</div>}
                  </td>
                  <td className="px-3 py-2.5">
                    <div className="flex flex-wrap gap-1">
                      {v.certifications.map((cert) => (
                        <span
                          key={cert}
                          className="rounded bg-emerald-50 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700"
                        >
                          {cert}
                        </span>
                      ))}
                    </div>
                  </td>
                  <td className="px-3 py-2.5 font-medium">
                    {formatMoney(v.retail_price_usd, "USD")}
                  </td>
                  {canViewCost && (
                    <td className="px-3 py-2.5 font-medium text-slate-700">
                      {formatMoney(v.cost_price_base, v.base_currency)}
                    </td>
                  )}
                  <td className="px-3 py-2.5">
                    {v.b2b_moq_tiers.map((t) => `${t.min_qty}+ @ $${t.unit_price_usd}`).join(" | ")}
                  </td>
                  <td className="px-3 py-2.5">
                    <button
                      type="button"
                      data-testid={`open-sku-drawer-${v.id}`}
                      onClick={() => setSelectedVariantId(v.id)}
                      className="rounded border border-indigo-300 bg-indigo-50 px-2.5 py-1 font-semibold text-indigo-700 hover:bg-indigo-100"
                    >
                      Configure Mappings
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {selectedVariantId && (
        <SkuMappingDrawer
          variantId={selectedVariantId}
          role={principal?.role}
          onClose={() => setSelectedVariantId(null)}
        />
      )}
    </AppShell>
  );
}
