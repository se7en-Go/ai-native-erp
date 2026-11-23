"use client";

import React, { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  BadgeCheck,
  Boxes,
  CheckCircle2,
  Copy,
  FileSpreadsheet,
  ImagePlus,
  KeyRound,
  Loader2,
  PackageSearch,
  Plus,
  Search,
  ShieldCheck,
  Sparkles,
  Tags,
  Trash2,
  Truck,
  Upload,
  Wand2,
  X,
} from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { useAuthSession } from "@/components/providers";
import { SkuMappingDrawer } from "@/components/catalog/sku-mapping-drawer";
import { BentoStatCard } from "@/components/ui/bento-stat-card";
import { Badge, EmptyState, Page, PageHeader, TableWrap } from "@/components/ui/page";
import { SegmentedTabs, type SegmentedTabOption } from "@/components/ui/segmented-tabs";
import type {
  CatalogCsvImportResponse,
  CollectorTokenRead,
  MediaAssetRead,
  SourcingCollectRequest,
  SourcingDraftRead,
  SourcingPublishResult,
  SourcingVariantSpecInput,
} from "@/lib/api-client/types.gen";
import {
  SOURCING_DEFAULT_FX_RATE,
  SOURCING_DEFAULT_MARGIN_PCT,
  collectSourcingDraft,
  createCollectorToken,
  deleteSourcingDraft,
  fetchCollectorTokens,
  fetchSourcingDrafts,
  importSourcingDraftToCatalog,
  publishMasterProduct,
  publishSourcingDraft,
  revokeCollectorToken,
  sourcingStatusCopy,
  sourcingStoreAccounts,
  sourcingStoreLabel,
  uploadCatalogMedia,
} from "@/lib/api-client/pack-f-sourcing";
import {
  CATALOG_CSV_SAMPLE,
  createCatalogProductAndVariant,
  importCatalogCsv,
  productPayloadFromDraft,
  variantPayloadFromDraft,
  type CatalogSkuDraft,
} from "@/lib/api-client/pack-a-erp";
import {
  fetchCatalogVariants,
  fetchChannelAccounts,
  fetchPendingProposalsCount,
} from "@/lib/data-source";
import { isPrivilegedCostViewer } from "@/lib/auth-cookie";
import { useI18n } from "@/lib/i18n";
import { formatMoney } from "@/lib/contracts";

/** Writer roles locked by the backend catalog router (`_WRITER_ROLES`). */
const WRITER_ROLES = new Set(["owner", "admin", "manager"]);

/** 顶部两个视图：主档 SKU 库（默认）与 1688 采集搬家 & 多平台刊登。 */
type CatalogView = "master" | "sourcing";

function catalogViewOptions(
  bi: (zh: string, en: string) => string,
  skuCount: number,
): SegmentedTabOption<CatalogView>[] {
  return [
    {
      value: "master",
      testId: "catalog-tab-master",
      badge: skuCount,
      label: (
        <span className="inline-flex items-center gap-1.5">
          <Boxes className="h-3.5 w-3.5" />
          {bi("主档 SKU 库", "Master SKU library")}
        </span>
      ),
    },
    {
      value: "sourcing",
      testId: "catalog-tab-sourcing",
      label: (
        <span className="inline-flex items-center gap-1.5">
          <Wand2 className="h-3.5 w-3.5" />
          {bi("1688 采集搬家 & 多平台刊登", "1688 sourcing & multi-platform publishing")}
        </span>
      ),
    },
  ];
}

interface SkuFormState {
  spu_code: string;
  product_name: string;
  category: string;
  hs_code: string;
  sku: string;
  retail_price_usd: string;
  cost_price_base: string;
  lead_time_days: string;
  carton_qty: string;
  gross_weight_kg: string;
  net_weight_kg: string;
  cbm_per_carton: string;
  safety_stock_qty: string;
}

const EMPTY_SKU_FORM: SkuFormState = {
  spu_code: "",
  product_name: "",
  category: "",
  hs_code: "",
  sku: "",
  retail_price_usd: "",
  cost_price_base: "",
  lead_time_days: "7",
  carton_qty: "50",
  gross_weight_kg: "",
  net_weight_kg: "",
  cbm_per_carton: "",
  safety_stock_qty: "50",
};

function toDraft(form: SkuFormState): CatalogSkuDraft {
  return {
    spu_code: form.spu_code,
    product_name: form.product_name,
    category: form.category,
    hs_code: form.hs_code,
    sku: form.sku,
    retail_price_usd: form.retail_price_usd,
    cost_price_base: form.cost_price_base,
    lead_time_days: Number(form.lead_time_days) || 0,
    carton_qty: Number(form.carton_qty) || 1,
    gross_weight_kg: form.gross_weight_kg,
    net_weight_kg: form.net_weight_kg,
    cbm_per_carton: form.cbm_per_carton,
    safety_stock_qty: Number(form.safety_stock_qty) || 0,
  };
}

/** Client-side guard mirroring the server's `ProductCreate` / `VariantCreate` rules. */
function validateSkuForm(form: SkuFormState): string | null {
  const required: Array<[keyof SkuFormState, string, string]> = [
    ["spu_code", "SPU 编码", "SPU code"],
    ["product_name", "商品名称", "Product name"],
    ["category", "品类", "Category"],
    ["sku", "SKU 编码", "SKU code"],
    ["retail_price_usd", "零售价", "Retail price"],
  ];
  const blank = required.filter(([key]) => !form[key].trim());
  if (blank.length > 0) {
    return `${blank.map(([, zh]) => zh).join("、")} 为必填项。`;
  }
  if (!/^\d+(\.\d+)?$/.test(form.retail_price_usd.trim()) || Number(form.retail_price_usd) <= 0) {
    return "零售价必须是大于 0 的金额。";
  }
  if (form.cost_price_base.trim() && !/^\d+(\.\d+)?$/.test(form.cost_price_base.trim())) {
    return "成本价必须是合法金额，或留空。";
  }
  return null;
}

/**
 * PageHeader / CardHeader 为共享原语（无 dark 变体），
 * 此处以任意变体在页面层补齐深色模式下的标题对比度。
 */
const PAGE_DARK_HEADER =
  "dark:[&_header_h1]:text-gray-100 dark:[&_header_p]:text-gray-400 dark:[&_.erp-card-header_h2]:text-gray-100 dark:[&_.erp-card-header_p]:text-gray-400";

export default function CatalogPage() {
  const { principal } = useAuthSession();
  const { bi } = useI18n();
  const queryClient = useQueryClient();
  const [search, setSearch] = useState("");
  const [selectedVariantId, setSelectedVariantId] = useState<string | null>(null);
  // 视图开关：主档 SKU 库（默认，保证既有单测零回归）↔ 1688 采集搬家 & 多平台刊登。
  const [view, setView] = useState<CatalogView>("master");
  useEffect(() => {
    if (typeof window !== "undefined") {
      const tab = new URLSearchParams(window.location.search).get("tab");
      if (tab === "sourcing") setView("sourcing");
    }
  }, []);

  const [createOpen, setCreateOpen] = useState(false);
  const [form, setForm] = useState<SkuFormState>(EMPTY_SKU_FORM);
  const [formError, setFormError] = useState<string | null>(null);
  const [createdSku, setCreatedSku] = useState<{ sku: string; spu_code: string } | null>(null);

  const [csvOpen, setCsvOpen] = useState(false);
  const [csvText, setCsvText] = useState("");
  const [csvResult, setCsvResult] = useState<CatalogCsvImportResponse | null>(null);
  const [csvError, setCsvError] = useState<string | null>(null);

  const [masterPublishProduct, setMasterPublishProduct] = useState<{
    productId: string;
    spuCode: string;
    sku: string;
  } | null>(null);
  const [masterStores, setMasterStores] = useState<string[]>([]);
  const [masterLivePush, setMasterLivePush] = useState(true);
  const [masterPublishResults, setMasterPublishResults] = useState<SourcingPublishResult[] | null>(
    null,
  );
  const [masterPublishError, setMasterPublishError] = useState<string | null>(null);
  const [masterUploadedAsset, setMasterUploadedAsset] = useState<MediaAssetRead | null>(null);

  const canViewCost = isPrivilegedCostViewer(principal?.role);
  const canWriteCatalog = WRITER_ROLES.has(principal?.role ?? "");

  const pendingProposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () => fetchPendingProposalsCount(),
  });

  const variantsQuery = useQuery({
    queryKey: ["catalog", "variants", search],
    queryFn: () => fetchCatalogVariants(search),
  });

  const masterAccountsQuery = useQuery({
    queryKey: ["channels", "accounts"],
    queryFn: () => fetchChannelAccounts(),
    enabled: masterPublishProduct !== null,
  });

  const masterPublishMutation = useMutation({
    mutationFn: (input: { productId: string; channelAccountIds: string[]; push: boolean }) =>
      publishMasterProduct(input.productId, {
        channel_account_ids: input.channelAccountIds,
        price_multiplier: "1.00",
        stock_sync_ratio: "1.00",
        push_to_marketplace: input.push,
        initial_stock_quantity: 50,
      }),
    onSuccess: (res) => {
      setMasterPublishError(null);
      setMasterPublishResults(res);
    },
    onError: (err: Error) => {
      setMasterPublishError(err.message);
    },
  });

  const createSkuMutation = useMutation({
    mutationFn: (draft: CatalogSkuDraft) =>
      createCatalogProductAndVariant(productPayloadFromDraft(draft), variantPayloadFromDraft(draft)),
    onSuccess: (created) => {
      setCreatedSku({ sku: created.variant.sku, spu_code: created.product.spu_code });
      setForm(EMPTY_SKU_FORM);
      setFormError(null);
      setCreateOpen(false);
      void queryClient.invalidateQueries({ queryKey: ["catalog", "variants"] });
    },
  });

  const importCsvMutation = useMutation({
    mutationFn: (text: string) => importCatalogCsv(text),
    onSuccess: (result) => {
      setCsvResult(result);
      setCsvError(null);
      void queryClient.invalidateQueries({ queryKey: ["catalog", "variants"] });
    },
    onError: (error: Error) => {
      setCsvResult(null);
      setCsvError(error.message);
    },
  });

  const variants = variantsQuery.data?.items ?? [];

  const summary = useMemo(() => {
    const categories = new Set(variants.map((v) => v.category));
    const certs = new Set(variants.flatMap((v) => v.certifications));
    const tiers = variants.reduce((total, v) => total + v.b2b_moq_tiers.length, 0);
    return { categories: categories.size, certs: certs.size, tiers };
  }, [variants]);

  const setField = (key: keyof SkuFormState) => (value: string) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  return (
    <AppShell
      currentPath="/catalog"
      pendingProposalsCount={pendingProposalsQuery.data?.total ?? 0}
    >
      <Page className={PAGE_DARK_HEADER}>
        <PageHeader
          title={bi("商品主档与多平台 SKU 映射", "Product master & multi-platform SKU mapping")}
          description={bi(
            "B2B 阶梯报价与 B2C 店铺组合装扣减共享同一套主档 SKU；平台映射决定库存同步与组合装倍率。成本价对非授权角色自动脱敏。",
            "B2B tier quotes and B2C bundle deductions share one master SKU. Platform mappings drive inventory sync and bundle multipliers; COGS is masked for unauthorised roles."
          )}
          actions={
            <div className="flex flex-wrap items-center gap-2">
              <div className="relative">
                <Search className="pointer-events-none absolute top-2.5 left-2.5 h-4 w-4 text-gray-400 dark:text-gray-500" />
                <input
                  type="text"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder={bi("搜索 SPU、SKU 或 HS 编码…", "Search SPU, SKU or HS code…")}
                  aria-label={bi("搜索商品", "Search catalog")}
                  className="erp-input w-64 rounded-xl pl-8"
                />
              </div>
              {canWriteCatalog && (
                <>
                  <button
                    type="button"
                    data-testid="open-create-sku-btn"
                    onClick={() => {
                      setCreatedSku(null);
                      setFormError(null);
                      setCreateOpen(true);
                    }}
                    className="erp-btn rounded-xl bg-blue-600 text-[13px] font-semibold text-white shadow-xs transition-colors hover:bg-blue-700"
                  >
                    <Plus className="h-3.5 w-3.5" />
                    <span>{bi("新建商品 / SKU", "New product / SKU")}</span>
                  </button>
                  <button
                    type="button"
                    data-testid="open-csv-import-btn"
                    onClick={() => {
                      setCsvError(null);
                      setCsvResult(null);
                      setCsvOpen(true);
                    }}
                    className="erp-btn erp-btn-secondary rounded-xl text-[13px] font-semibold"
                  >
                    <FileSpreadsheet className="h-3.5 w-3.5" />
                    <span>{bi("批量导入 CSV", "Bulk CSV import")}</span>
                  </button>
                </>
              )}
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

        <div className="flex flex-wrap items-center justify-between gap-2">
          <SegmentedTabs<CatalogView>
            ariaLabel={bi("商品主档视图切换", "Catalog view switch")}
            value={view}
            onChange={setView}
            options={catalogViewOptions(bi, variants.length)}
            className="flex-wrap"
          />
          <span className="inline-flex items-center gap-1.5 rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 text-[11px] font-medium text-slate-600 dark:border-slate-700 dark:bg-slate-800/70 dark:text-slate-400">
            <ShieldCheck className="h-3 w-3 text-blue-600 dark:text-blue-400" />
            {view === "master"
              ? bi(
                  "阶梯价与平台映射共享同一套主档 SKU",
                  "Tier pricing and platform mappings share one master SKU",
                )
              : bi(
                  "采集 → AI 改写 → 入主档 → 多平台刊登，全链路可追溯",
                  "Collect → AI rewrite → import → publish, fully traceable",
                )}
          </span>
        </div>

        {createdSku && (
          <div
            data-testid="create-sku-success"
            className="flex flex-wrap items-center gap-2 rounded-xl border border-emerald-200 bg-emerald-50 px-3.5 py-2.5 text-[12.5px] font-semibold text-emerald-900 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-200"
          >
            <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-600" />
            <span>
              {bi("新建成功：", "Created: ")}
              <span className="font-mono">{createdSku.spu_code}</span>
              {" / "}
              <span className="font-mono">{createdSku.sku}</span>
            </span>
            <button
              type="button"
              onClick={() => setCreatedSku(null)}
              className="ml-auto rounded-md p-1 text-emerald-700 transition-colors hover:bg-emerald-100"
              aria-label={bi("关闭提示", "Dismiss")}
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        )}

        {view === "master" && (
          <>
        <section
          aria-label={bi("商品主档关键指标", "Catalog key metrics")}
          className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"
        >
          <BentoStatCard
            label={bi("主档 SKU", "Master SKUs")}
            value={variants.length}
            icon={Boxes}
            accent="blue"
            hint={bi("B2B 与 B2C 共用同一份主档", "One master shared by B2B and B2C")}
          />
          <BentoStatCard
            label={bi("覆盖类目", "Categories")}
            value={summary.categories}
            icon={Tags}
            accent="violet"
            hint={bi("类目决定默认 HS 编码与合规要求", "Drives default HS codes and compliance")}
          />
          <BentoStatCard
            label={bi("合规认证", "Compliance certificates")}
            value={summary.certs}
            icon={BadgeCheck}
            accent="emerald"
            hint={bi("CE / RoHS / UL 等证书挂载在变体上", "CE / RoHS / UL attach to each variant")}
          />
          <BentoStatCard
            label={bi("阶梯价档位", "MOQ price tiers")}
            value={summary.tiers}
            icon={PackageSearch}
            accent="amber"
            hint={bi("报价单落库时快照当前阶梯", "Quotes snapshot the active tier")}
          />
        </section>

        {variants.length === 0 ? (
          <EmptyState>
            {variantsQuery.isLoading
              ? bi("正在加载商品主档…", "Loading catalog…")
              : bi("没有匹配的商品，请调整关键词。", "No matching products. Try another keyword.")}
          </EmptyState>
        ) : (
          <TableWrap className="rounded-2xl border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-900">
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
                      <div className="flex items-center gap-2">
                        <span
                          aria-hidden="true"
                          className="grid h-7 w-7 shrink-0 place-items-center rounded-lg bg-blue-50 text-blue-600 ring-1 ring-blue-100"
                        >
                          <Boxes className="h-3.5 w-3.5" />
                        </span>
                        <div className="min-w-0">
                          <div className="font-mono text-[12.5px] font-semibold text-gray-900 dark:text-gray-100">
                            {v.sku}
                          </div>
                          <div className="text-[11px] font-medium text-gray-500 dark:text-gray-400">{v.spu_code}</div>
                        </div>
                      </div>
                    </td>
                    <td>
                      <div className="font-medium text-gray-800 dark:text-gray-200">{v.category}</div>
                      {v.hs_code && (
                        <div className="font-mono text-[11px] text-gray-500 dark:text-gray-400">HS {v.hs_code}</div>
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
                    <td className="text-right font-semibold tabular-nums text-gray-900 dark:text-gray-100">
                      {formatMoney(v.retail_price_usd, "USD")}
                    </td>
                    {canViewCost && (
                      <td className="text-right tabular-nums text-gray-700 dark:text-gray-300">
                        {formatMoney(v.cost_price_base, v.base_currency)}
                      </td>
                    )}
                    <td className="tabular-nums text-[12px] text-gray-700 dark:text-gray-300">
                      <div className="flex flex-wrap gap-1">
                        {v.b2b_moq_tiers.map((t) => (
                          <span
                            key={`${t.min_qty}-${t.unit_price_usd}`}
                            className="rounded-md border border-blue-100 bg-blue-50/70 px-1.5 py-0.5 font-medium text-blue-900"
                          >
                            {t.min_qty}+ / ${t.unit_price_usd}
                          </span>
                        ))}
                      </div>
                    </td>
                    <td className="text-right">
                      <div className="inline-flex flex-wrap items-center justify-end gap-1.5">
                        {canWriteCatalog && (
                          <button
                            type="button"
                            data-testid={`open-product-publish-${v.product_id}`}
                            onClick={() => {
                              setMasterPublishProduct({
                                productId: v.product_id,
                                spuCode: v.spu_code,
                                sku: v.sku,
                              });
                              setMasterStores([]);
                              setMasterPublishResults(null);
                              setMasterPublishError(null);
                              setMasterUploadedAsset(null);
                            }}
                            className="erp-btn rounded-xl border border-blue-200 bg-blue-50 py-1 px-2.5 text-[12px] font-semibold text-blue-700 hover:bg-blue-100 dark:border-blue-800/70 dark:bg-blue-950/40 dark:text-blue-300"
                          >
                            <Truck className="h-3.5 w-3.5" />
                            <span>{bi("全平台刊登", "Multi-publish")}</span>
                          </button>
                        )}
                        <button
                          type="button"
                          data-testid={`open-sku-drawer-${v.id}`}
                          onClick={() => setSelectedVariantId(v.id)}
                          className="erp-btn erp-btn-secondary rounded-xl py-1 text-[12px]"
                        >
                          <PackageSearch className="h-3.5 w-3.5" />
                          <span>{bi("配置映射", "Configure mappings")}</span>
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableWrap>
        )}
          </>
        )}

        {view === "sourcing" && <SourcingWorkspace canWrite={canWriteCatalog} />}
      </Page>

      {createOpen && (
        <Overlay testId="create-sku-modal" labelledBy="create-sku-title">
          <form
            onSubmit={(event) => {
              event.preventDefault();
              const invalid = validateSkuForm(form);
              if (invalid) {
                setFormError(invalid);
                return;
              }
              setFormError(null);
              createSkuMutation.mutate(toDraft(form));
            }}
            className="space-y-3.5"
          >
            <ModalHeading
              id="create-sku-title"
              title={bi("新建商品与 SKU", "New product & SKU")}
              description={bi(
                "先建 SPU 主档，再挂 SKU 变体；交期、装箱与安全库存会写成出口包装属性，直接驱动商业发票与补货预警。",
                "Create the SPU master, then attach the SKU variant. Lead time, packing and safety stock become export attributes that feed the CI/PL and replenishment alerts."
              )}
              closeLabel={bi("关闭新建商品弹窗", "Close the new product dialog")}
              onClose={() => setCreateOpen(false)}
            />

            <div className="grid gap-2.5 sm:grid-cols-2">
              <Field
                testId="sku-form-spu-code"
                label={bi("SPU 编码", "SPU code")}
                required
                value={form.spu_code}
                onChange={setField("spu_code")}
                placeholder="LED-COB-24V"
              />
              <Field
                testId="sku-form-product-name"
                label={bi("商品名称", "Product name")}
                required
                value={form.product_name}
                onChange={setField("product_name")}
                placeholder="COB LED Strip 24V 5m 4000K"
              />
              <Field
                testId="sku-form-category"
                label={bi("品类", "Category")}
                required
                value={form.category}
                onChange={setField("category")}
                placeholder="LED Strip Lighting"
              />
              <Field
                testId="sku-form-hs-code"
                label={bi("HS 编码", "HS code")}
                value={form.hs_code}
                onChange={setField("hs_code")}
                placeholder="94054200"
              />
              <Field
                testId="sku-form-sku"
                label={bi("SKU 编码", "SKU code")}
                required
                value={form.sku}
                onChange={setField("sku")}
                placeholder="LED-COB-24V-5M-4000K"
              />
              <Field
                testId="sku-form-retail-price"
                label={bi("零售价（USD）", "Retail price (USD)")}
                required
                type="number"
                step="0.01"
                value={form.retail_price_usd}
                onChange={setField("retail_price_usd")}
                placeholder="29.90"
              />
              <Field
                testId="sku-form-cost-price"
                label={bi("成本价（可空）", "Cost price (optional)")}
                type="number"
                step="0.01"
                value={form.cost_price_base}
                onChange={setField("cost_price_base")}
                placeholder="45.50"
              />
              <Field
                testId="sku-form-lead-time"
                label={bi("交期天数", "Lead time (days)")}
                type="number"
                min={0}
                value={form.lead_time_days}
                onChange={setField("lead_time_days")}
              />
            </div>

            <div className="rounded-2xl border border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-800/60/70 p-3">
              <h4 className="text-[12.5px] font-semibold text-gray-900 dark:text-gray-100">
                {bi("出口包装与安全库存", "Export packing & safety stock")}
              </h4>
              <p className="mt-0.5 text-[11.5px] leading-5 text-gray-600 dark:text-gray-400">
                {bi(
                  "单箱数量用于换算单件毛净重；安全库存阈值直接决定低库存补货预警。",
                  "Carton quantity converts to per-unit weights; the safety-stock threshold drives the replenishment alerts."
                )}
              </p>
              <div className="mt-2 grid gap-2.5 sm:grid-cols-3">
                <Field
                  testId="sku-form-carton-qty"
                  label={bi("每箱数量", "Carton qty")}
                  type="number"
                  min={1}
                  value={form.carton_qty}
                  onChange={setField("carton_qty")}
                />
                <Field
                  testId="sku-form-gross-weight"
                  label={bi("毛重 kg", "Gross weight kg")}
                  type="number"
                  step="0.001"
                  value={form.gross_weight_kg}
                  onChange={setField("gross_weight_kg")}
                  placeholder="11.500"
                />
                <Field
                  testId="sku-form-net-weight"
                  label={bi("净重 kg", "Net weight kg")}
                  type="number"
                  step="0.001"
                  value={form.net_weight_kg}
                  onChange={setField("net_weight_kg")}
                  placeholder="10.000"
                />
                <Field
                  testId="sku-form-cbm"
                  label={bi("单箱体积 CBM", "CBM per carton")}
                  type="number"
                  step="0.0001"
                  value={form.cbm_per_carton}
                  onChange={setField("cbm_per_carton")}
                  placeholder="0.032"
                />
                <Field
                  testId="sku-form-safety-stock"
                  label={bi("安全库存阈值", "Safety stock qty")}
                  type="number"
                  min={0}
                  value={form.safety_stock_qty}
                  onChange={setField("safety_stock_qty")}
                />
              </div>
            </div>

            {(formError || createSkuMutation.isError) && (
              <p
                role="alert"
                data-testid="create-sku-error"
                className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-[12px] font-medium text-rose-800 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-300"
              >
                {formError ?? createSkuMutation.error?.message}
              </p>
            )}

            <div className="flex items-center justify-end gap-2 border-t border-gray-200 dark:border-gray-800 pt-3">
              <button
                type="button"
                onClick={() => setCreateOpen(false)}
                className="erp-btn erp-btn-secondary rounded-xl"
              >
                {bi("取消", "Cancel")}
              </button>
              <button
                type="submit"
                data-testid="create-sku-submit"
                disabled={createSkuMutation.isPending}
                className="erp-btn rounded-xl bg-blue-600 font-semibold text-white shadow-xs transition-colors hover:bg-blue-700 disabled:opacity-40"
              >
                {createSkuMutation.isPending
                  ? bi("正在创建…", "Creating…")
                  : bi("创建商品与 SKU", "Create product & SKU")}
              </button>
            </div>
          </form>
        </Overlay>
      )}

      {csvOpen && (
        <Overlay testId="csv-import-modal" labelledBy="csv-import-title">
          <div className="space-y-3.5">
            <ModalHeading
              id="csv-import-title"
              title={bi("批量导入商品与 SKU", "Bulk import products & SKUs")}
              description={bi(
                "表头大小写不敏感；spu_code、product_name、sku、retail_price_usd 为必填列，其余列缺失时回落到安全默认值，可先导价格表再补包装资料。",
                "Headers are case-insensitive. spu_code, product_name, sku and retail_price_usd are mandatory; every other column falls back to a safe default so you can import a price list first and enrich packing later."
              )}
              closeLabel={bi("关闭批量导入弹窗", "Close the bulk import dialog")}
              onClose={() => setCsvOpen(false)}
            />

            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                data-testid="fill-csv-template"
                onClick={() => {
                  setCsvText(CATALOG_CSV_SAMPLE);
                  setCsvResult(null);
                  setCsvError(null);
                }}
                className="erp-btn erp-btn-secondary rounded-xl text-[12px]"
              >
                <FileSpreadsheet className="h-3.5 w-3.5" />
                <span>{bi("填入示例模板", "Fill sample template")}</span>
              </button>
              <label
                htmlFor="catalog-csv-file"
                className="erp-btn erp-btn-secondary cursor-pointer rounded-xl text-[12px]"
              >
                <Upload className="h-3.5 w-3.5" />
                <span>{bi("选择 .csv 文件", "Choose .csv file")}</span>
              </label>
              <input
                id="catalog-csv-file"
                data-testid="csv-file-input"
                type="file"
                accept=".csv,text/csv"
                className="sr-only"
                onChange={async (event) => {
                  const file = event.target.files?.[0];
                  if (!file) return;
                  setCsvText(await file.text());
                  setCsvResult(null);
                  setCsvError(null);
                }}
              />
              <span className="text-[11.5px] text-gray-500 dark:text-gray-400">
                {bi("也可以直接粘贴 CSV 文本", "Or paste the CSV text directly")}
              </span>
            </div>

            <textarea
              data-testid="csv-textarea"
              value={csvText}
              onChange={(event) => setCsvText(event.target.value)}
              rows={9}
              spellCheck={false}
              aria-label={bi("CSV 内容", "CSV content")}
              placeholder={bi(
                "spu_code,product_name,category,hs_code,sku,retail_price_usd…",
                "spu_code,product_name,category,hs_code,sku,retail_price_usd…"
              )}
              className="erp-input w-full rounded-xl font-mono text-[11.5px] leading-5"
            />

            {(csvError || importCsvMutation.isError) && (
              <p
                role="alert"
                data-testid="csv-import-error"
                className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-[12px] font-medium text-rose-800 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-300"
              >
                {csvError ?? importCsvMutation.error?.message}
              </p>
            )}

            {csvResult && (
              <div
                data-testid="csv-import-summary"
                className="space-y-1.5 rounded-xl border border-emerald-200 bg-emerald-50 px-3.5 py-2.5 text-[12.5px] font-semibold text-emerald-900 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-200"
              >
                <div className="flex items-center gap-2">
                  <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-600" />
                  <span className="tabular-nums">
                    {bi("新增商品", "Products created")} {csvResult.created_products ?? 0}
                    {" / "}
                    {bi("新增 SKU", "SKUs created")} {csvResult.created_variants ?? 0}
                    {" / "}
                    {bi("更新", "Updated")} {csvResult.updated_variants ?? 0}
                  </span>
                </div>
                {(csvResult.errors ?? []).length > 0 && (
                  <ul
                    data-testid="csv-import-errors"
                    className="max-h-32 list-disc space-y-0.5 overflow-y-auto pl-5 text-[11.5px] font-medium text-rose-800 dark:text-rose-300"
                  >
                    {(csvResult.errors ?? []).map((rowError) => (
                      <li key={rowError}>{rowError}</li>
                    ))}
                  </ul>
                )}
              </div>
            )}

            <div className="flex items-center justify-end gap-2 border-t border-gray-200 dark:border-gray-800 pt-3">
              <button
                type="button"
                onClick={() => setCsvOpen(false)}
                className="erp-btn erp-btn-secondary rounded-xl"
              >
                {bi("取消", "Cancel")}
              </button>
              <button
                type="button"
                data-testid="csv-import-submit"
                disabled={importCsvMutation.isPending || csvText.trim().length < 5}
                onClick={() => importCsvMutation.mutate(csvText)}
                className="erp-btn rounded-xl bg-blue-600 font-semibold text-white shadow-xs transition-colors hover:bg-blue-700 disabled:opacity-40"
              >
                {importCsvMutation.isPending
                  ? bi("正在导入…", "Importing…")
                  : bi("开始导入", "Start import")}
              </button>
            </div>
          </div>
        </Overlay>
      )}

      {selectedVariantId && (
        <SkuMappingDrawer
          variantId={selectedVariantId}
          role={principal?.role}
          onClose={() => setSelectedVariantId(null)}
        />
      )}

      {masterPublishProduct && (
        <Overlay testId="master-publish-modal" labelledBy="master-publish-title">
          <div className="space-y-3.5">
            <ModalHeading
              id="master-publish-title"
              title={bi("主档商品全平台一键刊登", "Multi-platform master product publish")}
              description={`${masterPublishProduct.spuCode} · ${masterPublishProduct.sku}`}
              closeLabel={bi("关闭主档刊登弹窗", "Close master publish dialog")}
              onClose={() => setMasterPublishProduct(null)}
            />

            <div className="rounded-xl border border-slate-200 bg-slate-50/70 p-3 dark:border-slate-700 dark:bg-slate-800/40">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-[12px] font-semibold text-slate-800 dark:text-slate-200">
                  {bi("商品图片（直传统一图床 MinIO/R2）", "Product image (unified MinIO/R2 media)")}
                </span>
                <label className="erp-btn erp-btn-secondary cursor-pointer rounded-lg py-1 px-2.5 text-[11.5px]">
                  <ImagePlus className="h-3.5 w-3.5" />
                  <span>{bi("上传实图", "Upload image")}</span>
                  <input
                    type="file"
                    accept="image/png,image/jpeg,image/webp,image/gif"
                    data-testid="master-media-upload-input"
                    className="sr-only"
                    onChange={async (event) => {
                      const file = event.target.files?.[0];
                      if (!file || !masterPublishProduct) return;
                      try {
                        const uploaded = await uploadCatalogMedia(file, {
                          productId: masterPublishProduct.productId,
                        });
                        setMasterUploadedAsset(uploaded);
                        setMasterPublishError(null);
                      } catch (err) {
                        setMasterPublishError(err instanceof Error ? err.message : "Upload error");
                      }
                    }}
                  />
                </label>
              </div>
              {masterUploadedAsset && (
                <p
                  data-testid="master-media-uploaded-ok"
                  className="mt-1.5 text-[11px] font-medium text-emerald-700 dark:text-emerald-300"
                >
                  {bi("已上传至图床：", "Uploaded to media hub: ")}
                  {masterUploadedAsset.mime_type} ({masterUploadedAsset.byte_size} B)
                </p>
              )}
            </div>

            <div className="space-y-2">
              <span className="text-[12px] font-semibold text-slate-800 dark:text-slate-200">
                {bi("选择目标销售店铺", "Select target sales channels")}
              </span>
              <div className="flex flex-wrap gap-1.5">
                {sourcingStoreAccounts(masterAccountsQuery.data?.items ?? []).map((store) => {
                  const checked = masterStores.includes(store.id);
                  return (
                    <label
                      key={store.id}
                      data-testid={`master-publish-store-${store.id}`}
                      className={`inline-flex cursor-pointer items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] font-semibold ${
                        checked
                          ? "border-blue-500 bg-blue-600 text-white"
                          : "border-slate-200 bg-white text-slate-700 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300"
                      }`}
                    >
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() =>
                          setMasterStores((cur) =>
                            cur.includes(store.id)
                              ? cur.filter((x) => x !== store.id)
                              : [...cur, store.id],
                          )
                        }
                        className="sr-only"
                      />
                      <span>{sourcingStoreLabel(store.channel_type)}</span>
                      <span className={checked ? "text-white/80" : "text-slate-500"}>
                        {store.display_name}
                      </span>
                    </label>
                  );
                })}
              </div>
            </div>

            <label className="flex cursor-pointer items-center gap-2 text-[12px] font-semibold text-slate-800 dark:text-slate-200">
              <input
                type="checkbox"
                data-testid="master-publish-live-toggle"
                checked={masterLivePush}
                onChange={() => setMasterLivePush((v) => !v)}
                className="h-3.5 w-3.5 rounded accent-blue-600"
              />
              <span>
                {bi(
                  "通过官方 API 实时推送到所选电商平台（全变体与图床图片）",
                  "Push live via official platform APIs (all variants & media)",
                )}
              </span>
            </label>

            {masterPublishError && (
              <p role="alert" className="rounded-lg bg-rose-50 px-3 py-2 text-[12px] text-rose-800">
                {masterPublishError}
              </p>
            )}

            {masterPublishResults && masterPublishResults.length > 0 && (
              <ul
                data-testid="master-publish-results"
                className="space-y-1 rounded-xl border border-emerald-200 bg-emerald-50 px-3 py-2 text-[11.5px] text-emerald-900"
              >
                {masterPublishResults.map((res) => (
                  <li key={res.mapping_id} className="flex flex-wrap items-center gap-2">
                    <span className="font-semibold">{sourcingStoreLabel(res.channel_type)}</span>
                    <span className="font-mono">{res.external_sku}</span>
                    <span>{res.external_product_id}</span>
                  </li>
                ))}
              </ul>
            )}

            <div className="flex justify-end gap-2 border-t border-slate-200 pt-3 dark:border-slate-700">
              <button
                type="button"
                onClick={() => setMasterPublishProduct(null)}
                className="erp-btn erp-btn-secondary rounded-xl"
              >
                {bi("关闭", "Close")}
              </button>
              <button
                type="button"
                data-testid="master-publish-submit"
                disabled={masterPublishMutation.isPending || masterStores.length === 0}
                onClick={() =>
                  masterPublishProduct &&
                  masterPublishMutation.mutate({
                    productId: masterPublishProduct.productId,
                    channelAccountIds: masterStores,
                    push: masterLivePush,
                  })
                }
                className="erp-btn rounded-xl bg-blue-600 px-4 py-1.5 text-[12.5px] font-semibold text-white hover:bg-blue-700 disabled:opacity-40"
              >
                {masterPublishMutation.isPending ? bi("刊登中…", "Publishing…") : bi("立即刊登", "Publish now")}
              </button>
            </div>
          </div>
        </Overlay>
      )}
    </AppShell>
  );
}

/** 居中遮罩抽屉壳：Esc 关闭、点遮罩关闭，保持命令中心风格。 */
function Overlay({
  testId,
  labelledBy,
  children,
}: {
  testId: string;
  labelledBy: string;
  children: React.ReactNode;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-slate-900/40 p-4 backdrop-blur-sm">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={labelledBy}
        data-testid={testId}
        className="erp-card my-6 w-full max-w-2xl rounded-[20px] p-4 shadow-2xl"
      >
        {children}
      </div>
    </div>
  );
}

function ModalHeading({
  id,
  title,
  description,
  closeLabel,
  onClose,
}: {
  id: string;
  title: string;
  description: string;
  closeLabel: string;
  onClose: () => void;
}) {
  return (
    <div className="flex items-start justify-between gap-3 border-b border-gray-200 dark:border-gray-800 pb-3">
      <div className="min-w-0">
        <h3 id={id} className="text-[15px] font-bold text-gray-900 dark:text-gray-100">
          {title}
        </h3>
        <p className="mt-1 text-[12px] leading-5 text-gray-600 dark:text-gray-400">{description}</p>
      </div>
      <button
        type="button"
        onClick={onClose}
        aria-label={closeLabel}
        className="rounded-lg border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 p-1.5 text-gray-600 dark:text-gray-400 shadow-xs transition-colors hover:bg-gray-100 dark:hover:bg-gray-800"
      >
        <X className="h-4 w-4" />
      </button>
    </div>
  );
}

function Field({
  testId,
  label,
  value,
  onChange,
  required = false,
  ...input
}: {
  testId: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  required?: boolean;
} & Omit<React.InputHTMLAttributes<HTMLInputElement>, "value" | "onChange">) {
  return (
    <label className="block">
      <span className="mb-1 flex items-center gap-1 text-[11.5px] font-semibold text-gray-700 dark:text-gray-300">
        {label}
        {required && <span className="text-rose-600">*</span>}
      </span>
      <input
        {...input}
        data-testid={testId}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="erp-input w-full rounded-xl tabular-nums"
      />
    </label>
  );
}

// ---------------------------------------------------------------------------
// 1688 采集搬家 & 多平台刊登（妙手 ERP 风格闭环）
// ---------------------------------------------------------------------------

const SOURCE_PLATFORM_OPTIONS = [
  { value: "1688", zh: "1688 链接", en: "1688 link" },
  { value: "taobao", zh: "淘宝 / 天猫", en: "Taobao / Tmall" },
  { value: "pdd", zh: "拼多多", en: "Pinduoduo" },
  { value: "alibaba", zh: "Alibaba 国际站", en: "Alibaba.com" },
  { value: "url", zh: "供应商 URL", en: "Supplier URL" },
  { value: "manual", zh: "粘贴中文文案", en: "Paste Chinese copy" },
] as const;

function SourcingWorkspace({ canWrite }: { canWrite: boolean }) {
  const { bi } = useI18n();
  const queryClient = useQueryClient();

  const [tokenName, setTokenName] = useState("");
  const [revealedToken, setRevealedToken] = useState<string | null>(null);
  const [copiedToken, setCopiedToken] = useState(false);
  const [tokenError, setTokenError] = useState<string | null>(null);

  const [platform, setPlatform] = useState<(typeof SOURCE_PLATFORM_OPTIONS)[number]["value"]>(
    "1688",
  );
  const [sourceUrl, setSourceUrl] = useState("");
  const [rawTitle, setRawTitle] = useState("");
  const [rawText, setRawText] = useState("");
  const [priceCny, setPriceCny] = useState("18.60");
  const [marginPct, setMarginPct] = useState(SOURCING_DEFAULT_MARGIN_PCT);
  const [fxRate, setFxRate] = useState(SOURCING_DEFAULT_FX_RATE);
  const [formError, setFormError] = useState<string | null>(null);
  const [collected, setCollected] = useState<string | null>(null);

  const [publishDraftId, setPublishDraftId] = useState<string | null>(null);
  const [publishSelection, setPublishSelection] = useState<string[]>([]);
  const [multiplier, setMultiplier] = useState("1.00");
  // 默认 false：不勾选时仍然只写本地 SKU 映射，绝不声称平台上架。
  const [pushToMarketplace, setPushToMarketplace] = useState(false);
  const [publishResults, setPublishResults] = useState<Record<string, SourcingPublishResult[]>>({});
  const [publishError, setPublishError] = useState<string | null>(null);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [optionName, setOptionName] = useState("Spec");
  const [variantRows, setVariantRows] = useState<
    Array<{ suffix: string; value: string; costCny: string }>
  >([]);
  const [imageUrlsText, setImageUrlsText] = useState("");
  const [draftMedia, setDraftMedia] = useState<Record<string, MediaAssetRead[]>>({});
  const [mediaUploadError, setMediaUploadError] = useState<string | null>(null);

  const draftsQuery = useQuery({
    queryKey: ["catalog", "sourcing", "drafts"],
    queryFn: () => fetchSourcingDrafts(),
  });

  const collectorTokensQuery = useQuery({
    queryKey: ["catalog", "sourcing", "collector-tokens"],
    queryFn: () => fetchCollectorTokens(),
  });

  const createTokenMutation = useMutation({
    mutationFn: (name: string) => createCollectorToken({ name }),
    onSuccess: (res) => {
      setRevealedToken(res.plain_token);
      setCopiedToken(false);
      setTokenName("");
      setTokenError(null);
      void queryClient.invalidateQueries({ queryKey: ["catalog", "sourcing", "collector-tokens"] });
    },
    onError: (err: Error) => setTokenError(err.message),
  });

  const revokeTokenMutation = useMutation({
    mutationFn: (tokenId: string) => revokeCollectorToken(tokenId),
    onSuccess: () => {
      setTokenError(null);
      void queryClient.invalidateQueries({ queryKey: ["catalog", "sourcing", "collector-tokens"] });
    },
    onError: (err: Error) => setTokenError(err.message),
  });

  const accountsQuery = useQuery({
    queryKey: ["channels", "accounts"],
    queryFn: () => fetchChannelAccounts(),
  });

  const stores = useMemo(
    () => sourcingStoreAccounts(accountsQuery.data?.items ?? []),
    [accountsQuery.data],
  );

  const drafts = draftsQuery.data ?? [];

  const invalidateDrafts = () => {
    void queryClient.invalidateQueries({ queryKey: ["catalog", "sourcing", "drafts"] });
  };

  const collectMutation = useMutation({
    mutationFn: (body: SourcingCollectRequest) => collectSourcingDraft(body),
    onSuccess: (draft) => {
      setCollected(draft.optimized_title_en);
      setFormError(null);
      setSourceUrl("");
      setRawTitle("");
      setRawText("");
      setVariantRows([]);
      setImageUrlsText("");
      invalidateDrafts();
    },
    onError: (error: Error) => {
      setCollected(null);
      setFormError(error.message);
    },
  });

  const importMutation = useMutation({
    mutationFn: (draftId: string) => importSourcingDraftToCatalog(draftId),
    onSuccess: () => {
      invalidateDrafts();
      void queryClient.invalidateQueries({ queryKey: ["catalog", "variants"] });
    },
  });

  const publishMutation = useMutation({
    mutationFn: (input: {
      draftId: string;
      channelAccountIds: string[];
      pushToMarketplace: boolean;
    }) =>
      publishSourcingDraft(input.draftId, {
        channel_account_ids: input.channelAccountIds,
        price_multiplier: multiplier.trim() || "1.00",
        stock_sync_ratio: "1.00",
        push_to_marketplace: input.pushToMarketplace,
      }),
    onSuccess: (results, input) => {
      setPublishError(null);
      setPublishResults((current) => ({ ...current, [input.draftId]: results }));
      setPublishDraftId(null);
      setPublishSelection([]);
      invalidateDrafts();
    },
    onError: (error: Error) => setPublishError(error.message),
  });

  const deleteMutation = useMutation({
    mutationFn: (draftId: string) => deleteSourcingDraft(draftId),
    onSuccess: () => invalidateDrafts(),
  });

  function submitCollect(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const url = sourceUrl.trim();
    const title = rawTitle.trim();
    const text = rawText.trim();
    const price = priceCny.trim();

    if (platform !== "manual" && !/^https?:\/\/\S+$/i.test(url)) {
      setFormError(bi("请粘贴以 http:// 或 https:// 开头的商品链接。", "Paste a product URL starting with http:// or https://."));
      return;
    }
    if (platform === "manual" && !title && !text) {
      setFormError(bi("至少粘贴中文标题或规格文案。", "Paste at least the Chinese title or spec copy."));
      return;
    }
    if (!/^\d+(\.\d+)?$/.test(price) || Number(price) <= 0) {
      setFormError(bi("进货价必须是大于 0 的金额（CNY）。", "The purchase price must be a positive CNY amount."));
      return;
    }
    setFormError(null);
    setCollected(null);
    const cleanedImages = imageUrlsText
      .split(/[\n,]+/)
      .map((u) => u.trim())
      .filter((u) => /^https?:\/\/\S+$/i.test(u));
    const validVariants: SourcingVariantSpecInput[] = variantRows
      .filter((r) => r.suffix.trim() && Number(r.costCny) > 0)
      .map((r) => ({
        sku_suffix: r.suffix.trim(),
        option_values: { [optionName.trim() || "Spec"]: r.value.trim() || r.suffix.trim() },
        cost_price_cny: r.costCny.trim(),
      }));
    const optionValues = Array.from(
      new Set(
        validVariants.map((v) => Object.values(v.option_values ?? {})[0]).filter(Boolean),
      ),
    );
    collectMutation.mutate({
      source_platform: platform,
      source_url: platform === "manual" ? null : url,
      raw_title: title || null,
      raw_text: text || null,
      raw_price_cny: price,
      images: cleanedImages,
      options_spec:
        validVariants.length > 0 && optionValues.length > 0
          ? [{ name: optionName.trim() || "Spec", values: optionValues }]
          : [],
      variants_spec: validVariants,
      target_margin_pct: marginPct.trim() || SOURCING_DEFAULT_MARGIN_PCT,
      fx_rate_usd_cny: fxRate.trim() || SOURCING_DEFAULT_FX_RATE,
    });
  }

  function toggleStore(storeId: string) {
    setPublishError(null);
    setPublishSelection((current) =>
      current.includes(storeId) ? current.filter((id) => id !== storeId) : [...current, storeId],
    );
  }

  return (
    <div data-testid="sourcing-workspace" className="space-y-4">
      <form
        data-testid="sourcing-collect-form"
        onSubmit={submitCollect}
        className="rounded-2xl border border-slate-200 bg-white p-4 shadow-xs dark:border-slate-800 dark:bg-slate-900"
      >
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="min-w-0">
            <h3 className="inline-flex items-center gap-1.5 text-[14px] font-bold text-slate-950 dark:text-slate-100">
              <Wand2 className="h-4 w-4 text-blue-600 dark:text-blue-400" />
              {bi("一键采集并 AI 优化", "One-click collect & AI rewrite")}
            </h3>
            <p className="mt-0.5 text-[11.5px] leading-5 text-slate-600 dark:text-slate-400">
              {bi(
                "贴 1688 链接或中文文案，自动生成英文 SEO 标题、5 条海外卖点、USD 零售价与 3 档 MOQ 阶梯价。",
                "Paste a 1688 link or Chinese copy — get an English SEO title, 5 selling points, a USD retail price and 3 MOQ tiers."
              )}
            </p>
          </div>
          <span className="inline-flex items-center gap-1 rounded-full border border-blue-200 bg-blue-50 px-2 py-0.5 text-[10.5px] font-semibold text-blue-800 dark:border-blue-800/70 dark:bg-blue-950/40 dark:text-blue-300">
            <Sparkles className="h-3 w-3" />
            {bi("确定性模板 · 可复算价格", "Deterministic template · recomputable price")}
          </span>
        </div>

        {canWrite ? (
          <>
            <div className="mt-3 grid gap-2.5 sm:grid-cols-2 xl:grid-cols-3">
              <label className="block">
                <span className="erp-label">{bi("采集来源", "Source")}</span>
                <select
                  data-testid="sourcing-source-platform"
                  value={platform}
                  onChange={(event) =>
                    setPlatform(event.target.value as (typeof SOURCE_PLATFORM_OPTIONS)[number]["value"])
                  }
                  className="erp-input mt-1 w-full rounded-xl"
                >
                  {SOURCE_PLATFORM_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {bi(option.zh, option.en)}
                    </option>
                  ))}
                </select>
              </label>

              <Field
                testId="sourcing-source-url"
                label={bi("商品链接（1688 / 供应商）", "Product URL (1688 / supplier)")}
                value={sourceUrl}
                onChange={setSourceUrl}
                placeholder="https://detail.1688.com/offer/680001234567.html"
                disabled={platform === "manual"}
              />
              <Field
                testId="sourcing-raw-title"
                label={bi("中文标题（可空）", "Chinese title (optional)")}
                value={rawTitle}
                onChange={setRawTitle}
                placeholder="COB 灯带 24V 5m 4000K 高显指"
              />
              <Field
                testId="sourcing-raw-price-cny"
                label={bi("进货价（CNY）", "Purchase price (CNY)")}
                value={priceCny}
                onChange={setPriceCny}
                type="number"
                step="0.01"
                min={0}
              />
              <Field
                testId="sourcing-target-margin"
                label={bi("目标毛利率（%）", "Target margin (%)")}
                value={marginPct}
                onChange={setMarginPct}
                type="number"
                step="1"
                min={1}
                max={90}
              />
              <Field
                testId="sourcing-fx-rate"
                label={bi("实时汇率（USD/CNY）", "FX rate (USD/CNY)")}
                value={fxRate}
                onChange={setFxRate}
                type="number"
                step="0.01"
                min={1}
              />
            </div>

            <label className="mt-2.5 block">
              <span className="erp-label">
                {bi("中文规格 / 卖点文案（可空，用于 AI 改写）", "Chinese spec copy (optional, feeds the AI rewrite)")}
              </span>
              <textarea
                data-testid="sourcing-raw-text"
                value={rawText}
                onChange={(event) => setRawText(event.target.value)}
                rows={3}
                placeholder={bi(
                  "例：铝材灯体，IP65 防水，支持遥控调光，50 只起订，7 天出样。",
                  "e.g. Aluminium body, IP65 waterproof, remote dimming, 50 pcs MOQ, 7-day samples."
                )}
                className="erp-input mt-1 w-full rounded-xl text-[12px] leading-5"
              />
            </label>

            <div className="mt-2.5">
              <button
                type="button"
                data-testid="sourcing-variants-toggle"
                onClick={() => setAdvancedOpen((v) => !v)}
                className="inline-flex items-center gap-1.5 text-[11.5px] font-semibold text-blue-700 hover:underline dark:text-blue-400"
              >
                <Plus className="h-3.5 w-3.5" />
                <span>
                  {advancedOpen
                    ? bi("收起多规格与图片配置", "Hide multi-variant & image options")
                    : bi("展开多规格矩阵与图片链接（可选）", "Multi-variant SKUs & image URLs (optional)")}
                </span>
              </button>

              {advancedOpen && (
                <div
                  data-testid="sourcing-variants-panel"
                  className="mt-2 space-y-2.5 rounded-xl border border-slate-200 bg-slate-50/70 p-3 dark:border-slate-700 dark:bg-slate-800/40"
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <label className="inline-flex items-center gap-2 text-[11.5px] font-semibold text-slate-700 dark:text-slate-300">
                      <span>{bi("规格维度名", "Option name")}</span>
                      <input
                        data-testid="sourcing-option-name"
                        value={optionName}
                        onChange={(e) => setOptionName(e.target.value)}
                        className="erp-input w-32 rounded-lg py-1"
                        placeholder="Color Temp"
                      />
                    </label>
                    <button
                      type="button"
                      data-testid="sourcing-add-variant-row"
                      onClick={() =>
                        setVariantRows((cur) => [
                          ...cur,
                          { suffix: `V${cur.length + 1}`, value: "", costCny: priceCny || "36.00" },
                        ])
                      }
                      className="erp-btn erp-btn-secondary rounded-lg py-1 px-2.5 text-[11px]"
                    >
                      <Plus className="h-3 w-3" />
                      <span>{bi("添加规格 SKU", "Add variant SKU")}</span>
                    </button>
                  </div>

                  {variantRows.length > 0 && (
                    <div className="space-y-1.5">
                      {variantRows.map((row, idx) => (
                        <div key={idx} className="grid grid-cols-12 gap-2">
                          <input
                            data-testid={`sourcing-variant-suffix-${idx}`}
                            value={row.suffix}
                            onChange={(e) =>
                              setVariantRows((cur) =>
                                cur.map((r, i) => (i === idx ? { ...r, suffix: e.target.value } : r)),
                              )
                            }
                            placeholder="SKU Suffix (3000K)"
                            className="erp-input col-span-3 rounded-lg py-1 text-[11.5px]"
                          />
                          <input
                            data-testid={`sourcing-variant-value-${idx}`}
                            value={row.value}
                            onChange={(e) =>
                              setVariantRows((cur) =>
                                cur.map((r, i) => (i === idx ? { ...r, value: e.target.value } : r)),
                              )
                            }
                            placeholder={bi("规格值 (3000K Warm)", "Option value")}
                            className="erp-input col-span-5 rounded-lg py-1 text-[11.5px]"
                          />
                          <input
                            data-testid={`sourcing-variant-cost-${idx}`}
                            type="number"
                            step="0.01"
                            value={row.costCny}
                            onChange={(e) =>
                              setVariantRows((cur) =>
                                cur.map((r, i) => (i === idx ? { ...r, costCny: e.target.value } : r)),
                              )
                            }
                            placeholder="CNY"
                            className="erp-input col-span-3 rounded-lg py-1 text-[11.5px]"
                          />
                          <button
                            type="button"
                            onClick={() => setVariantRows((cur) => cur.filter((_, i) => i !== idx))}
                            className="col-span-1 grid place-items-center rounded-lg border border-slate-200 text-slate-500 hover:text-rose-600"
                            aria-label="Remove variant"
                          >
                            <X className="h-3 w-3" />
                          </button>
                        </div>
                      ))}
                    </div>
                  )}

                  <label className="block">
                    <span className="text-[11px] font-semibold text-slate-600 dark:text-slate-400">
                      {bi("外部商品图 URL（每行一条，可选）", "External image URLs (one per line, optional)")}
                    </span>
                    <textarea
                      data-testid="sourcing-image-urls"
                      rows={2}
                      value={imageUrlsText}
                      onChange={(e) => setImageUrlsText(e.target.value)}
                      placeholder="https://cdn.example.com/product-main.jpg"
                      className="erp-input mt-1 w-full rounded-lg text-[11.5px]"
                    />
                  </label>
                </div>
              )}
            </div>

            <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
              <span className="text-[11px] font-medium text-slate-500 dark:text-slate-400">
                {bi(
                  "采集只抓公开商品页，绝不带登录态或内网地址。",
                  "Collection only reads public product pages — no logged-in or internal URLs."
                )}
              </span>
              <button
                type="submit"
                data-testid="sourcing-collect-submit"
                disabled={collectMutation.isPending}
                className="erp-btn rounded-xl bg-blue-600 px-4 py-2 text-[12.5px] font-semibold text-white shadow-xs transition-colors hover:bg-blue-700 disabled:opacity-40 dark:bg-blue-600 dark:hover:bg-blue-500"
              >
                {collectMutation.isPending ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Sparkles className="h-3.5 w-3.5" />
                )}
                <span>
                  {collectMutation.isPending
                    ? bi("正在采集并优化…", "Collecting & rewriting…")
                    : bi("采集并 AI 优化", "Collect & AI rewrite")}
                </span>
              </button>
            </div>
          </>
        ) : (
          <p
            data-testid="sourcing-readonly-note"
            className="mt-3 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-[12px] font-medium text-amber-900 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-200"
          >
            {bi(
              "当前角色可浏览采集箱，但采集、入主档与刊登需要 owner / admin / manager 权限。",
              "You can browse the sourcing box; collecting, importing and publishing need owner / admin / manager."
            )}
          </p>
        )}

        {formError && (
          <p
            role="alert"
            data-testid="sourcing-collect-error"
            className="mt-2.5 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-[12px] font-medium text-rose-800 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-300"
          >
            {formError}
          </p>
        )}

        {collected && (
          <div
            data-testid="sourcing-collect-success"
            className="mt-2.5 flex flex-wrap items-center gap-2 rounded-xl border border-emerald-200 bg-emerald-50 px-3 py-2 text-[12px] font-semibold text-emerald-900 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-200"
          >
            <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-emerald-600" />
            <span>
              {bi("已生成英文草稿：", "English draft created: ")}
              <span className="font-semibold">{collected}</span>
            </span>
          </div>
        )}
      </form>

      <section
        data-testid="collector-tokens-card"
        className="rounded-2xl border border-slate-200 bg-white p-4 shadow-xs dark:border-slate-800 dark:bg-slate-900"
      >
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <h3 className="inline-flex items-center gap-1.5 text-[13.5px] font-bold text-slate-950 dark:text-slate-100">
              <KeyRound className="h-4 w-4 text-blue-600 dark:text-blue-400" />
              {bi("浏览器一键采集插件授权码", "Browser collector extension tokens")}
            </h3>
            <p className="mt-0.5 text-[11.5px] text-slate-600 dark:text-slate-400">
              {bi(
                "在 Chrome / Ego Lite 加载 apps/collector-extension 后，填入此授权码即可从 1688 / 淘宝 / 拼多多 / Alibaba 详情页一键采集入库。",
                "Load apps/collector-extension in Chrome / Ego Lite and paste a token below to capture products from 1688, Taobao, PDD, and Alibaba."
              )}
            </p>
          </div>
          {canWrite && (
            <div className="flex flex-wrap items-center gap-2">
              <input
                type="text"
                data-testid="collector-token-name-input"
                value={tokenName}
                onChange={(e) => setTokenName(e.target.value)}
                placeholder={bi("例如：Chrome 1688 插件", "e.g. Chrome collector")}
                className="erp-input h-8 w-44 rounded-lg text-[12px]"
              />
              <button
                type="button"
                data-testid="collector-token-create-btn"
                disabled={createTokenMutation.isPending || !tokenName.trim()}
                onClick={() => createTokenMutation.mutate(tokenName.trim())}
                className="inline-flex h-8 items-center gap-1 rounded-lg bg-slate-900 px-3 text-[12px] font-semibold text-white transition hover:bg-slate-800 disabled:opacity-50 dark:bg-blue-600 dark:hover:bg-blue-500"
              >
                {createTokenMutation.isPending ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Plus className="h-3.5 w-3.5" />
                )}
                {bi("生成授权码", "Generate token")}
              </button>
            </div>
          )}
        </div>

        {tokenError && (
          <p className="mt-2 rounded-lg border border-rose-200 bg-rose-50 px-3 py-1.5 text-[11.5px] font-medium text-rose-800 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-300">
            {tokenError}
          </p>
        )}

        {revealedToken && (
          <div
            data-testid="collector-token-revealed"
            className="mt-2.5 flex flex-wrap items-center justify-between gap-2 rounded-xl border border-emerald-200 bg-emerald-50 px-3 py-2 text-[12px] text-emerald-950 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-200"
          >
            <div className="min-w-0">
              <p className="font-semibold">
                {bi("请立即复制（明文仅展示这一次）：", "Copy now (shown once only):")}
              </p>
              <code className="break-all font-mono text-[11.5px]">{revealedToken}</code>
            </div>
            <button
              type="button"
              data-testid="collector-token-copy-btn"
              onClick={() => {
                if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
                  void navigator.clipboard.writeText(revealedToken);
                }
                setCopiedToken(true);
              }}
              className="inline-flex items-center gap-1 rounded-lg border border-emerald-300 bg-white px-2.5 py-1 text-[11.5px] font-semibold text-emerald-800 hover:bg-emerald-100 dark:border-emerald-700 dark:bg-slate-900 dark:text-emerald-200"
            >
              <Copy className="h-3.5 w-3.5" />
              {copiedToken ? bi("已复制", "Copied") : bi("复制", "Copy")}
            </button>
          </div>
        )}

        {(collectorTokensQuery.data?.length ?? 0) > 0 && (
          <div data-testid="collector-token-list" className="mt-3 divide-y divide-slate-100 dark:divide-slate-800">
            {(collectorTokensQuery.data as CollectorTokenRead[]).map((tok) => {
              const isRevoked = Boolean(tok.revoked_at);
              return (
                <div
                  key={tok.id}
                  data-testid={`collector-token-row-${tok.id}`}
                  className="flex flex-wrap items-center justify-between gap-2 py-2 text-[12px]"
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-semibold text-slate-900 dark:text-slate-100">
                      {tok.name}
                    </span>
                    <code className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-slate-700 dark:bg-slate-800 dark:text-slate-300">
                      {tok.token_prefix}…
                    </code>
                    <span
                      className={
                        isRevoked
                          ? "rounded-full bg-slate-100 px-2 py-0.5 text-[10.5px] font-medium text-slate-500 dark:bg-slate-800 dark:text-slate-400"
                          : "rounded-full bg-emerald-50 px-2 py-0.5 text-[10.5px] font-semibold text-emerald-700 dark:bg-emerald-950/50 dark:text-emerald-300"
                      }
                    >
                      {isRevoked ? bi("已吊销", "Revoked") : bi("有效", "Active")}
                    </span>
                    {tok.last_used_at && (
                      <span className="text-[11px] text-slate-500 dark:text-slate-400">
                        {bi("最近使用", "Last used")}: {tok.last_used_at.slice(0, 10)}
                      </span>
                    )}
                  </div>
                  {canWrite && !isRevoked && (
                    <button
                      type="button"
                      data-testid={`collector-token-revoke-${tok.id}`}
                      disabled={revokeTokenMutation.isPending}
                      onClick={() => revokeTokenMutation.mutate(tok.id)}
                      className="rounded-lg border border-rose-200 px-2.5 py-1 text-[11px] font-semibold text-rose-700 hover:bg-rose-50 disabled:opacity-50 dark:border-rose-500/30 dark:text-rose-300 dark:hover:bg-rose-500/10"
                    >
                      {bi("吊销", "Revoke")}
                    </button>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </section>

      <section aria-label={bi("采集箱草稿", "Sourcing drafts")} className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3 className="inline-flex items-center gap-1.5 text-[13.5px] font-bold text-slate-950 dark:text-slate-100">
            <Truck className="h-4 w-4 text-blue-600 dark:text-blue-400" />
            {bi("采集箱", "Sourcing box")}
            <span className="rounded-full bg-slate-100 px-1.5 py-0.5 text-[10.5px] font-semibold tabular-nums text-slate-700 dark:bg-slate-800 dark:text-slate-300">
              {drafts.length}
            </span>
          </h3>
          <span className="text-[11px] font-medium text-slate-500 dark:text-slate-400">
            {bi(
              "草稿 → 入主档 SKU → 多平台刊登，一步都不会丢。",
              "Draft → master SKU → multi-platform listing, nothing gets lost."
            )}
          </span>
        </div>

        {draftsQuery.isLoading ? (
          <EmptyState testId="sourcing-draft-loading">
            {bi("正在加载采集箱…", "Loading the sourcing box…")}
          </EmptyState>
        ) : drafts.length === 0 ? (
          <EmptyState testId="sourcing-draft-empty">
            {bi(
              "采集箱还是空的：贴一条 1688 链接开始吧。",
              "The sourcing box is empty — paste a 1688 link to start."
            )}
          </EmptyState>
        ) : (
          <div data-testid="sourcing-draft-list" className="space-y-3">
            {drafts.map((draft) => (
              <SourcingDraftCard
                key={draft.id}
                draft={draft}
                canWrite={canWrite}
                stores={stores}
                publishOpen={publishDraftId === draft.id}
                publishSelection={publishSelection}
                multiplier={multiplier}
                livePushEnabled={pushToMarketplace}
                publishPending={publishMutation.isPending && publishMutation.variables?.draftId === draft.id}
                publishResults={publishResults[draft.id]}
                publishError={publishDraftId === draft.id ? publishError : null}
                importing={importMutation.isPending && importMutation.variables === draft.id}
                deleting={deleteMutation.isPending && deleteMutation.variables === draft.id}
                onTogglePublish={() => {
                  setPublishError(null);
                  setPublishDraftId((current) => (current === draft.id ? null : draft.id));
                  setPublishSelection([]);
                }}
                onToggleStore={toggleStore}
                onChangeMultiplier={setMultiplier}
                onToggleLivePush={() => setPushToMarketplace((current) => !current)}
                onConfirmPublish={() =>
                  publishMutation.mutate({
                    draftId: draft.id,
                    channelAccountIds: publishSelection,
                    pushToMarketplace,
                  })
                }
                onImport={() => importMutation.mutate(draft.id)}
                onDelete={() => deleteMutation.mutate(draft.id)}
                uploadedMedia={draftMedia[draft.id] ?? []}
                mediaUploadError={mediaUploadError}
                onUploadMedia={async (file) => {
                  try {
                    const asset = await uploadCatalogMedia(file, {
                      sourcingDraftId: draft.id,
                      productId: draft.imported_product_id ?? undefined,
                      altText: draft.optimized_title_en,
                    });
                    setMediaUploadError(null);
                    setDraftMedia((cur) => ({
                      ...cur,
                      [draft.id]: [...(cur[draft.id] ?? []), asset],
                    }));
                  } catch (err) {
                    setMediaUploadError(err instanceof Error ? err.message : "Upload failed");
                  }
                }}
              />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

function SourcingDraftCard({
  draft,
  canWrite,
  stores,
  publishOpen,
  publishSelection,
  multiplier,
  livePushEnabled,
  publishPending,
  publishResults,
  publishError,
  importing,
  deleting,
  onTogglePublish,
  onToggleStore,
  onChangeMultiplier,
  onToggleLivePush,
  onConfirmPublish,
  onImport,
  onDelete,
  uploadedMedia = [],
  mediaUploadError = null,
  onUploadMedia,
}: {
  draft: SourcingDraftRead;
  canWrite: boolean;
  stores: Array<{ id: string; channel_type: string; display_name: string }>;
  publishOpen: boolean;
  publishSelection: string[];
  multiplier: string;
  livePushEnabled: boolean;
  publishPending: boolean;
  publishResults?: SourcingPublishResult[];
  publishError: string | null;
  importing: boolean;
  deleting: boolean;
  onTogglePublish: () => void;
  onToggleStore: (storeId: string) => void;
  onChangeMultiplier: (value: string) => void;
  onToggleLivePush: () => void;
  onConfirmPublish: () => void;
  onImport: () => void;
  onDelete: () => void;
  uploadedMedia?: MediaAssetRead[];
  mediaUploadError?: string | null;
  onUploadMedia?: (file: File) => void;
}) {
  const { bi } = useI18n();
  // 草稿徽标优先使用真实刊登状态（`live_published` 只能由平台回执写入）。
  const status = sourcingStatusCopy(draft.listing_state ?? draft.status);

  return (
    <article
      data-testid={`sourcing-draft-${draft.id}`}
      className="rounded-2xl border border-slate-200 bg-white p-4 shadow-xs transition-colors hover:border-blue-200 dark:border-slate-800 dark:bg-slate-900 dark:hover:border-blue-800"
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-1.5">
            <span
              data-testid={`sourcing-draft-status-${draft.id}`}
              className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10.5px] font-semibold ${status.tone}`}
            >
              <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-current opacity-70" />
              {bi(status.zh, status.en)}
            </span>
            <span className="rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-[10.5px] font-medium text-slate-600 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-400">
              {draft.source_platform}
            </span>
            <span className="rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 font-mono text-[10.5px] font-medium text-slate-600 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-400">
              {draft.suggested_sku}
            </span>
          </div>
          <h4 className="mt-1.5 text-[13.5px] leading-5 font-bold text-slate-950 dark:text-slate-100">
            {draft.optimized_title_en}
          </h4>
          {draft.source_url && (
            <a
              href={draft.source_url}
              target="_blank"
              rel="noopener noreferrer"
              className="mt-0.5 block truncate text-[11px] font-medium text-blue-700 underline decoration-blue-300 underline-offset-2 hover:text-blue-800 dark:text-blue-400 dark:decoration-blue-700 dark:hover:text-blue-300"
            >
              {draft.source_url}
            </a>
          )}
        </div>

        <div className="shrink-0 text-right">
          <div className="text-[15px] font-bold tabular-nums text-slate-950 dark:text-slate-100">
            {formatMoney(draft.target_price_usd, "USD")}
          </div>
          <div className="text-[10.5px] font-medium text-slate-500 dark:text-slate-400">
            {bi("建议零售价", "Suggested retail")}
          </div>
        </div>
      </div>

      <ul className="mt-2 space-y-1">
        {draft.bullet_points.map((point) => (
          <li
            key={point}
            className="flex items-start gap-1.5 text-[11.5px] leading-5 text-slate-600 dark:text-slate-400"
          >
            <CheckCircle2 className="mt-0.5 h-3 w-3 shrink-0 text-emerald-600 dark:text-emerald-400" />
            <span>{point}</span>
          </li>
        ))}
      </ul>

      <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
        {draft.moq_tiers.map((tier) => (
          <span
            key={tier.min_qty}
            className="rounded-md border border-blue-100 bg-blue-50/70 px-1.5 py-0.5 text-[11px] font-semibold tabular-nums text-blue-900 dark:border-blue-900/70 dark:bg-blue-950/40 dark:text-blue-200"
          >
            {tier.min_qty}+ / {formatMoney(tier.unit_price_usd, "USD")}
          </span>
        ))}
        <span className="rounded-md border border-slate-200 bg-slate-50 px-1.5 py-0.5 text-[11px] font-medium tabular-nums text-slate-600 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-400">
          {bi("进货", "Cost")} ¥{draft.raw_price_cny} → {formatMoney(draft.cost_usd, "USD")}
        </span>
        <span className="rounded-md border border-slate-200 bg-slate-50 px-1.5 py-0.5 text-[11px] font-medium tabular-nums text-slate-600 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-400">
          {bi("汇率", "FX")} {draft.fx_rate_usd_cny} · {bi("毛利", "Margin")} {draft.target_margin_pct}%
        </span>
        {(draft.variants_spec ?? []).map((v) => (
          <span
            key={v.sku_suffix}
            data-testid={`sourcing-draft-variant-${draft.id}-${v.sku_suffix}`}
            className="rounded-md border border-violet-200 bg-violet-50/70 px-1.5 py-0.5 text-[11px] font-semibold tabular-nums text-violet-900 dark:border-violet-800/70 dark:bg-violet-950/40 dark:text-violet-200"
          >
            {v.sku_suffix}: {formatMoney(v.retail_price_usd ?? draft.target_price_usd, "USD")}
          </span>
        ))}
        {uploadedMedia.map((asset) => (
          <span
            key={asset.id}
            data-testid={`sourcing-draft-media-${draft.id}-${asset.id}`}
            className="inline-flex items-center gap-1 rounded-md border border-emerald-200 bg-emerald-50/70 px-1.5 py-0.5 text-[11px] font-semibold text-emerald-800 dark:border-emerald-800/70 dark:bg-emerald-950/40 dark:text-emerald-200"
          >
            <ImagePlus className="h-3 w-3" />
            {asset.mime_type.replace("image/", "").toUpperCase()} · {asset.byte_size}B
          </span>
        ))}
      </div>
      {mediaUploadError && (
        <p role="alert" className="mt-1.5 text-[11px] font-medium text-rose-700">
          {mediaUploadError}
        </p>
      )}

      {canWrite && (
        <div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t border-slate-200 pt-3 dark:border-slate-800">
          <span className="text-[11px] font-medium text-slate-500 dark:text-slate-400">
            {draft.imported_variant_id
              ? bi("已入主档，可直接刊登", "Imported — ready to publish")
              : bi("先入主档 SKU，再一键刊登", "Import to a master SKU, then publish")}
          </span>
          <div className="flex flex-wrap items-center gap-1.5">
            {onUploadMedia && (
              <label className="erp-btn cursor-pointer rounded-xl border border-slate-300 bg-white px-2.5 py-1.5 text-[11.5px] font-semibold text-slate-700 transition-colors hover:border-blue-300 hover:bg-blue-50 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-200">
                <ImagePlus className="h-3 w-3" />
                <span>{bi("上传实图", "Upload image")}</span>
                <input
                  type="file"
                  accept="image/png,image/jpeg,image/webp,image/gif"
                  data-testid={`sourcing-media-upload-${draft.id}`}
                  className="sr-only"
                  onChange={(event) => {
                    const file = event.target.files?.[0];
                    if (file) onUploadMedia(file);
                  }}
                />
              </label>
            )}
            <button
              type="button"
              data-testid={`sourcing-delete-${draft.id}`}
              onClick={onDelete}
              disabled={deleting}
              aria-label={bi("移出采集箱", "Remove from the sourcing box")}
              className="erp-btn rounded-xl border border-slate-200 bg-white px-2 py-1.5 text-[11.5px] font-semibold text-slate-600 transition-colors hover:border-rose-300 hover:bg-rose-50 hover:text-rose-700 disabled:opacity-40 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-400 dark:hover:border-rose-700 dark:hover:bg-rose-950/40 dark:hover:text-rose-300"
            >
              <Trash2 className="h-3 w-3" />
            </button>
            <button
              type="button"
              data-testid={`sourcing-import-${draft.id}`}
              onClick={onImport}
              disabled={importing}
              className="erp-btn rounded-xl border border-slate-300 bg-white px-3 py-1.5 text-[11.5px] font-semibold text-slate-900 transition-colors hover:border-emerald-300 hover:bg-emerald-50 hover:text-emerald-800 disabled:opacity-40 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-100 dark:hover:border-emerald-700 dark:hover:bg-emerald-950/40 dark:hover:text-emerald-300"
            >
              {importing ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                <PackageSearch className="h-3 w-3" />
              )}
              <span>{bi("一键入主档 SKU", "Import to master SKU")}</span>
            </button>
            <button
              type="button"
              data-testid={`sourcing-publish-${draft.id}`}
              aria-expanded={publishOpen}
              onClick={onTogglePublish}
              className="erp-btn rounded-xl bg-blue-600 px-3 py-1.5 text-[11.5px] font-semibold text-white shadow-xs transition-colors hover:bg-blue-700 dark:bg-blue-600 dark:hover:bg-blue-500"
            >
              <Truck className="h-3 w-3" />
              <span>{bi("多平台一键刊登", "Publish to stores")}</span>
            </button>
          </div>
        </div>
      )}

      {publishOpen && (
        <div
          data-testid={`sourcing-publish-panel-${draft.id}`}
          className="mt-3 space-y-2 rounded-xl border border-blue-200 bg-blue-50/50 px-3 py-2.5 dark:border-blue-900/70 dark:bg-blue-950/30"
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-[11.5px] font-semibold text-blue-900 dark:text-blue-200">
              {bi("选择已接入店铺", "Pick the connected stores")}
            </span>
            <label className="inline-flex items-center gap-1.5 text-[11px] font-medium text-slate-700 dark:text-slate-300">
              <span>{bi("加价倍率", "Price multiplier")}</span>
              <input
                data-testid={`sourcing-publish-multiplier-${draft.id}`}
                type="number"
                step="0.01"
                min={0.1}
                max={10}
                value={multiplier}
                onChange={(event) => onChangeMultiplier(event.target.value)}
                className="erp-input w-20 rounded-lg py-1 tabular-nums"
              />
            </label>
          </div>

          {stores.length === 0 ? (
            <p className="text-[11.5px] font-medium text-slate-600 dark:text-slate-400">
              {bi(
                "还没有接入可刊登的店铺，先去渠道中心绑定 Shopify / Amazon / eBay。",
                "No publishable store yet — bind Shopify / Amazon / eBay in the channel hub first."
              )}
            </p>
          ) : (
            <div className="flex flex-wrap gap-1.5">
              {stores.map((store) => {
                const checked = publishSelection.includes(store.id);
                return (
                  <label
                    key={store.id}
                    data-testid={`sourcing-publish-store-${store.id}`}
                    className={`inline-flex cursor-pointer items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] font-semibold transition-colors ${
                      checked
                        ? "border-blue-500 bg-blue-600 text-white"
                        : "border-slate-200 bg-white text-slate-700 hover:border-blue-300 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300 dark:hover:border-blue-700"
                    }`}
                  >
                    <input
                      type="checkbox"
                      data-testid={`sourcing-publish-check-${store.id}`}
                      checked={checked}
                      onChange={() => onToggleStore(store.id)}
                      className="sr-only"
                    />
                    <span>{sourcingStoreLabel(store.channel_type)}</span>
                    <span className={checked ? "text-white/80" : "text-slate-500 dark:text-slate-400"}>
                      {store.display_name}
                    </span>
                  </label>
                );
              })}
            </div>
          )}

          {publishError && (
            <p
              role="alert"
              data-testid={`sourcing-publish-error-${draft.id}`}
              className="rounded-lg border border-rose-200 bg-rose-50 px-2.5 py-1.5 text-[11.5px] font-medium text-rose-800 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-300"
            >
              {publishError}
            </p>
          )}

          {/* 实时上架开关：默认关闭，不勾选只写本地 SKU 映射，绝不声称平台上架。 */}
          <label className="flex cursor-pointer items-start gap-2 rounded-lg border border-blue-200 bg-white px-2.5 py-1.5 text-[11.5px] font-semibold text-slate-800 dark:border-blue-900/70 dark:bg-slate-900 dark:text-slate-200">
            <input
              type="checkbox"
              data-testid="sourcing-live-push-toggle"
              checked={livePushEnabled}
              onChange={onToggleLivePush}
              className="mt-0.5 h-3.5 w-3.5 shrink-0 rounded border-slate-300 text-blue-600 accent-blue-600"
            />
            <span className="min-w-0">
              {bi(
                "同时通过官方 API 实时上架到全平台（Shopify / TikTok / Amazon / eBay / Woo / Shopline / Shoplazza / Alibaba）",
                "Push live listing via official APIs (Shopify / TikTok / Amazon / eBay / Woo / Shopline / Shoplazza / Alibaba)"
              )}
              <span className="mt-0.5 block text-[10.5px] font-normal text-slate-500 dark:text-slate-400">
                {bi(
                  "不勾选只写本地 SKU 映射（映射就绪），不声称平台已上架。",
                  "Leave unchecked for local SKU mapping only — no claim of a live listing."
                )}
              </span>
            </span>
          </label>

          <div className="flex items-center justify-end gap-1.5">
            <button
              type="button"
              onClick={onTogglePublish}
              className="erp-btn rounded-xl border border-slate-200 bg-white px-2.5 py-1 text-[11.5px] font-semibold text-slate-700 hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300 dark:hover:bg-slate-800"
            >
              {bi("取消", "Cancel")}
            </button>
            <button
              type="button"
              data-testid={`sourcing-publish-confirm-${draft.id}`}
              onClick={onConfirmPublish}
              disabled={publishPending || publishSelection.length === 0}
              className="erp-btn rounded-xl bg-blue-600 px-3 py-1 text-[11.5px] font-semibold text-white shadow-xs transition-colors hover:bg-blue-700 disabled:opacity-40 dark:bg-blue-600 dark:hover:bg-blue-500"
            >
              {publishPending ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                <Truck className="h-3 w-3" />
              )}
              <span>{bi("确认刊登", "Confirm publish")}</span>
            </button>
          </div>
        </div>
      )}

      {publishResults && publishResults.length > 0 && (
        <ul
          data-testid={`sourcing-publish-results-${draft.id}`}
          className="mt-2.5 space-y-1 rounded-xl border border-emerald-200 bg-emerald-50/70 px-3 py-2 dark:border-emerald-800/70 dark:bg-emerald-950/30"
        >
          {publishResults.map((result) => {
            const resultStatus = sourcingStatusCopy(
              result.listing_state ?? draft.listing_state ?? draft.status
            );
            return (
              <li
                key={result.mapping_id}
                className="flex flex-wrap items-center gap-1.5 text-[11.5px] font-medium text-emerald-900 dark:text-emerald-200"
              >
                <CheckCircle2 className="h-3 w-3 shrink-0" />
                <span className="font-semibold">{sourcingStoreLabel(result.channel_type)}</span>
                <span className="font-mono text-[11px]">{result.external_sku}</span>
                <span className="tabular-nums">{formatMoney(result.price_usd, "USD")}</span>
                <span
                  data-testid={`sourcing-publish-result-status-${draft.id}-${result.channel_account_id}`}
                  className={`inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10.5px] font-semibold ${resultStatus.tone}`}
                >
                  {bi(resultStatus.zh, resultStatus.en)}
                </span>
                <span
                  data-testid={`sourcing-publish-result-external-id-${draft.id}-${result.channel_account_id}`}
                  className="max-w-full truncate font-mono text-[10.5px] text-emerald-800/80 dark:text-emerald-300/80"
                  title={result.external_product_id}
                >
                  {bi("平台 ID", "Platform ID")} {result.external_product_id}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </article>
  );
}
