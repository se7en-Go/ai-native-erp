"use client";

import React, { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  BadgeCheck,
  Boxes,
  CheckCircle2,
  FileSpreadsheet,
  PackageSearch,
  Plus,
  Search,
  ShieldCheck,
  Tags,
  Upload,
  X,
} from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { useAuthSession } from "@/components/providers";
import { SkuMappingDrawer } from "@/components/catalog/sku-mapping-drawer";
import { BentoStatCard } from "@/components/ui/bento-stat-card";
import { Badge, EmptyState, Page, PageHeader, TableWrap } from "@/components/ui/page";
import type { CatalogCsvImportResponse } from "@/lib/api-client/types.gen";
import {
  CATALOG_CSV_SAMPLE,
  createCatalogProductAndVariant,
  importCatalogCsv,
  productPayloadFromDraft,
  variantPayloadFromDraft,
  type CatalogSkuDraft,
} from "@/lib/api-client/pack-a-erp";
import { fetchCatalogVariants, fetchPendingProposalsCount } from "@/lib/data-source";
import { isPrivilegedCostViewer } from "@/lib/auth-cookie";
import { useI18n } from "@/lib/i18n";
import { formatMoney } from "@/lib/contracts";

/** Writer roles locked by the backend catalog router (`_WRITER_ROLES`). */
const WRITER_ROLES = new Set(["owner", "admin", "manager"]);

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

export default function CatalogPage() {
  const { principal } = useAuthSession();
  const { bi } = useI18n();
  const queryClient = useQueryClient();
  const [search, setSearch] = useState("");
  const [selectedVariantId, setSelectedVariantId] = useState<string | null>(null);

  const [createOpen, setCreateOpen] = useState(false);
  const [form, setForm] = useState<SkuFormState>(EMPTY_SKU_FORM);
  const [formError, setFormError] = useState<string | null>(null);
  const [createdSku, setCreatedSku] = useState<{ sku: string; spu_code: string } | null>(null);

  const [csvOpen, setCsvOpen] = useState(false);
  const [csvText, setCsvText] = useState("");
  const [csvResult, setCsvResult] = useState<CatalogCsvImportResponse | null>(null);
  const [csvError, setCsvError] = useState<string | null>(null);

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
      <Page>
        <PageHeader
          title={bi("商品主档与多平台 SKU 映射", "Product master & multi-platform SKU mapping")}
          description={bi(
            "B2B 阶梯报价与 B2C 店铺组合装扣减共享同一套主档 SKU；平台映射决定库存同步与组合装倍率。成本价对非授权角色自动脱敏。",
            "B2B tier quotes and B2C bundle deductions share one master SKU. Platform mappings drive inventory sync and bundle multipliers; COGS is masked for unauthorised roles."
          )}
          actions={
            <div className="flex flex-wrap items-center gap-2">
              <div className="relative">
                <Search className="pointer-events-none absolute top-2.5 left-2.5 h-4 w-4 text-slate-400" />
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
                    className="erp-btn rounded-xl bg-[#18181B] text-[13px] font-semibold text-[#FAFAF9] shadow-xs transition-colors hover:bg-[#065F46]"
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

        {createdSku && (
          <div
            data-testid="create-sku-success"
            className="flex flex-wrap items-center gap-2 rounded-xl border border-emerald-200 bg-emerald-50 px-3.5 py-2.5 text-[12.5px] font-semibold text-emerald-900"
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
          <TableWrap className="rounded-2xl">
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
                          <div className="font-mono text-[12.5px] font-semibold text-slate-900">
                            {v.sku}
                          </div>
                          <div className="text-[11px] font-medium text-slate-500">{v.spu_code}</div>
                        </div>
                      </div>
                    </td>
                    <td>
                      <div className="font-medium text-slate-800">{v.category}</div>
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
                    <td className="text-right font-semibold tabular-nums text-slate-900">
                      {formatMoney(v.retail_price_usd, "USD")}
                    </td>
                    {canViewCost && (
                      <td className="text-right tabular-nums text-slate-700">
                        {formatMoney(v.cost_price_base, v.base_currency)}
                      </td>
                    )}
                    <td className="tabular-nums text-[12px] text-slate-700">
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
                      <button
                        type="button"
                        data-testid={`open-sku-drawer-${v.id}`}
                        onClick={() => setSelectedVariantId(v.id)}
                        className="erp-btn erp-btn-secondary rounded-xl py-1 text-[12px]"
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

            <div className="rounded-2xl border border-slate-200 bg-slate-50/70 p-3">
              <h4 className="text-[12.5px] font-semibold text-slate-900">
                {bi("出口包装与安全库存", "Export packing & safety stock")}
              </h4>
              <p className="mt-0.5 text-[11.5px] leading-5 text-slate-600">
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
                className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-[12px] font-medium text-rose-800"
              >
                {formError ?? createSkuMutation.error?.message}
              </p>
            )}

            <div className="flex items-center justify-end gap-2 border-t border-slate-200 pt-3">
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
                className="erp-btn rounded-xl bg-[#18181B] font-semibold text-[#FAFAF9] shadow-xs transition-colors hover:bg-[#065F46] disabled:opacity-40"
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
              <span className="text-[11.5px] text-slate-500">
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
                className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-[12px] font-medium text-rose-800"
              >
                {csvError ?? importCsvMutation.error?.message}
              </p>
            )}

            {csvResult && (
              <div
                data-testid="csv-import-summary"
                className="space-y-1.5 rounded-xl border border-emerald-200 bg-emerald-50 px-3.5 py-2.5 text-[12.5px] font-semibold text-emerald-900"
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
                    className="max-h-32 list-disc space-y-0.5 overflow-y-auto pl-5 text-[11.5px] font-medium text-rose-800"
                  >
                    {(csvResult.errors ?? []).map((rowError) => (
                      <li key={rowError}>{rowError}</li>
                    ))}
                  </ul>
                )}
              </div>
            )}

            <div className="flex items-center justify-end gap-2 border-t border-slate-200 pt-3">
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
                className="erp-btn rounded-xl bg-[#18181B] font-semibold text-[#FAFAF9] shadow-xs transition-colors hover:bg-[#065F46] disabled:opacity-40"
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
    <div className="flex items-start justify-between gap-3 border-b border-slate-200 pb-3">
      <div className="min-w-0">
        <h3 id={id} className="text-[15px] font-bold text-slate-950">
          {title}
        </h3>
        <p className="mt-1 text-[12px] leading-5 text-slate-600">{description}</p>
      </div>
      <button
        type="button"
        onClick={onClose}
        aria-label={closeLabel}
        className="rounded-lg border border-slate-200 bg-white p-1.5 text-slate-600 shadow-xs transition-colors hover:bg-slate-100"
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
      <span className="mb-1 flex items-center gap-1 text-[11.5px] font-semibold text-slate-700">
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
