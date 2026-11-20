"use client";

import React, { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { PackageSearch, Search, ShieldCheck } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { useAuthSession } from "@/components/providers";
import { SkuMappingDrawer } from "@/components/catalog/sku-mapping-drawer";
import { Badge, EmptyState, Page, PageHeader, TableWrap } from "@/components/ui/page";
import { fetchCatalogVariants, fetchPendingProposalsCount } from "@/lib/data-source";
import { isPrivilegedCostViewer } from "@/lib/auth-cookie";
import { useI18n } from "@/lib/i18n";
import { formatMoney } from "@/lib/contracts";

export default function CatalogPage() {
  const { principal } = useAuthSession();
  const { bi } = useI18n();
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
      <Page>
        <PageHeader
          title={bi("商品主档与多平台 SKU 映射", "Product master & multi-platform SKU mapping")}
          description={bi(
            "B2B 阶梯报价与 B2C 店铺组合装扣减共享同一套主档 SKU；平台映射决定库存同步与组合装倍率。成本价对非授权角色自动脱敏。",
            "B2B tier quotes and B2C bundle deductions share one master SKU. Platform mappings drive inventory sync and bundle multipliers; COGS is masked for unauthorised roles."
          )}
          actions={
            <div className="relative">
              <Search className="pointer-events-none absolute top-2.5 left-2.5 h-4 w-4 text-slate-400" />
              <input
                type="text"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder={bi("搜索 SPU、SKU 或 HS 编码…", "Search SPU, SKU or HS code…")}
                aria-label={bi("搜索商品", "Search catalog")}
                className="erp-input w-72 pl-8"
              />
            </div>
          }
          meta={
            canViewCost ? (
              <Badge tone="success">
                <ShieldCheck className="h-3 w-3" />
                {bi("成本价可见", "Cost price visible")}
              </Badge>
            ) : (
              <Badge tone="warning">
                <ShieldCheck className="h-3 w-3" />
                {bi("成本价已隐藏", "Cost price hidden")}
              </Badge>
            )
          }
        />

        {variants.length === 0 ? (
          <EmptyState>
            {variantsQuery.isLoading
              ? bi("正在加载商品主档…", "Loading catalog…")
              : bi("没有匹配的商品，请调整关键词。", "No matching products. Try another keyword.")}
          </EmptyState>
        ) : (
          <TableWrap>
            <table className="erp-table">
              <thead>
                <tr>
                  <th>{bi("SKU / SPU", "SKU / SPU")}</th>
                  <th>{bi("类目与 HS 编码", "Category & HS code")}</th>
                  <th>{bi("认证", "Certifications")}</th>
                  <th className="text-right">{bi("零售价（USD）", "Retail (USD)")}</th>
                  {canViewCost && (
                    <th data-testid="catalog-cost-header" className="text-right">
                      {bi("基础成本", "Base cost")}
                    </th>
                  )}
                  <th>{bi("MOQ 阶梯价", "MOQ tiers")}</th>
                  <th className="text-right">{bi("操作", "Action")}</th>
                </tr>
              </thead>
              <tbody>
                {variants.map((v) => (
                  <tr key={v.id}>
                    <td>
                      <div className="font-medium text-slate-900">{v.sku}</div>
                      <div className="text-[11px] text-slate-500">{v.spu_code}</div>
                    </td>
                    <td>
                      <div className="text-slate-700">{v.category}</div>
                      {v.hs_code && (
                        <div className="font-mono text-[11px] text-slate-500">HS {v.hs_code}</div>
                      )}
                    </td>
                    <td>
                      <div className="flex flex-wrap gap-1">
                        {v.certifications.map((cert) => (
                          <Badge key={cert} tone="success">
                            {cert}
                          </Badge>
                        ))}
                      </div>
                    </td>
                    <td className="text-right tabular-nums text-slate-800">
                      {formatMoney(v.retail_price_usd, "USD")}
                    </td>
                    {canViewCost && (
                      <td className="text-right tabular-nums text-slate-600">
                        {formatMoney(v.cost_price_base, v.base_currency)}
                      </td>
                    )}
                    <td className="tabular-nums text-[12px] text-slate-600">
                      <div className="flex flex-wrap gap-1">
                        {v.b2b_moq_tiers.map((t) => (
                          <span
                            key={`${t.min_qty}-${t.unit_price_usd}`}
                            className="rounded border border-slate-200 bg-slate-50 px-1.5 py-0.5"
                          >
                            {t.min_qty}+ / ${t.unit_price_usd}
                          </span>
                        ))}
                      </div>
                    </td>
                    <td className="text-right">
                      <button
                        type="button"
                        data-testid={`open-sku-drawer-${v.id}`}
                        onClick={() => setSelectedVariantId(v.id)}
                        className="erp-btn erp-btn-secondary py-1 text-[12px]"
                      >
                        <PackageSearch className="h-3.5 w-3.5" />
                        <span>{bi("配置映射", "Configure mappings")}</span>
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableWrap>
        )}
      </Page>

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
