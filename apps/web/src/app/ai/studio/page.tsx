"use client";

import React, { useMemo, useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Archive, ArrowUpRight, CheckCircle2, FileText, WandSparkles } from "lucide-react";

import { AppShell } from "@/components/app-shell";
import { useAuthSession } from "@/components/providers";
import { useI18n } from "@/lib/i18n";
import { CreativeStudioPanel } from "@/components/ai-workforce/creative-studio-panel";
import type { CreativeAsset, SkuOption } from "@/components/ai-workforce/types";
import { fetchCatalogVariants, fetchPendingProposalsCount } from "@/lib/data-source";
import {
  fetchStudioAssets,
  generateStudioAssets,
  isProviderUnavailable,
  saveStudioAsset,
  setStudioAssetStatus,
} from "@/lib/phase3b-data";

const STATUS_LABELS: Record<string, string> = {
  draft: "草稿",
  approved: "已批准",
  archived: "已归档",
};

export default function AiStudioPage() {
  const { t, bi } = useI18n();
  const queryClient = useQueryClient();
  useAuthSession();
  const [providerBlocked, setProviderBlocked] = useState(false);

  const proposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () => fetchPendingProposalsCount(),
  });
  const variantsQuery = useQuery({
    queryKey: ["catalog", "variants", "studio"],
    queryFn: () => fetchCatalogVariants(""),
  });
  const assetsQuery = useQuery({
    queryKey: ["ai", "studio", "assets"],
    queryFn: fetchStudioAssets,
  });

  const save = useMutation({
    mutationFn: (asset: CreativeAsset) => saveStudioAsset(asset),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["ai", "studio", "assets"] }),
  });
  const review = useMutation({
    mutationFn: ({ id, status }: { id: string; status: "approved" | "archived" }) =>
      setStudioAssetStatus(id, status),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["ai", "studio", "assets"] }),
  });

  const skus: SkuOption[] = useMemo(
    () =>
      (variantsQuery.data?.items ?? []).map((variant) => ({
        sku: variant.sku,
        label: variant.spu_code || variant.category || variant.sku,
      })),
    [variantsQuery.data]
  );
  const assets = assetsQuery.data ?? [];

  async function handleGenerate(request: Parameters<typeof generateStudioAssets>[0]) {
    try {
      return await generateStudioAssets(request);
    } catch (error) {
      if (isProviderUnavailable(error)) setProviderBlocked(true);
      throw error;
    }
  }

  async function handleSave(asset: CreativeAsset) {
    setProviderBlocked(false);
    await save.mutateAsync(asset);
  }

  async function handleCopy(text: string) {
    if (typeof navigator !== "undefined" && navigator.clipboard) {
      await navigator.clipboard.writeText(text);
    }
  }

  return (
    <AppShell currentPath="/ai/studio" pendingProposalsCount={proposalsQuery.data?.total ?? 0}>
      <div className="erp-page space-y-5">
        <header className="flex flex-wrap items-end justify-between gap-3">
          <div className="flex min-w-0 items-start gap-3.5">
            <span className="grid h-12 w-12 shrink-0 place-items-center rounded-2xl bg-gradient-to-br from-emerald-600 to-teal-600 text-white shadow-lg shadow-emerald-600/25">
              <WandSparkles className="h-6 w-6" aria-hidden="true" />
            </span>
            <div className="min-w-0">
              <h1 className="text-[22px] font-bold tracking-tight text-slate-950">{bi("AI 素材工作台", "AI studio")}</h1>
              <p className="mt-1 max-w-3xl text-[13px] leading-6 text-slate-600">
                {bi("关联真实商品主档 SKU 生成多语言文案；生成草稿永远从 draft 开始，批准动作由人工完成。", "Generate multilingual copy tied to real catalog SKUs; drafts always start as draft and approval stays manual.")}
              </p>
            </div>
          </div>
          <Link
            href="/catalog"
            className="inline-flex items-center gap-1.5 rounded-xl border border-slate-200 bg-white px-3 py-1.5 text-[12px] font-semibold text-slate-700 shadow-xs transition-colors hover:border-slate-300 hover:bg-slate-50"
          >
            {bi("查看商品目录", "Open catalog")}
            <ArrowUpRight className="h-3.5 w-3.5 text-slate-400" aria-hidden="true" />
          </Link>
        </header>

        {providerBlocked && (
          <p
            role="alert"
            data-testid="studio-provider-unavailable"
            className="flex items-start gap-2 rounded-2xl border border-amber-300 bg-amber-50 px-4 py-2.5 text-[12.5px] font-medium text-amber-900"
          >
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
            <span>{t("aiProviderUnavailable")}</span>
          </p>
        )}

        {assetsQuery.error && (
          <p role="alert" data-testid="studio-error" className="rounded-2xl border border-rose-200 bg-rose-50 px-4 py-2.5 text-[12.5px] font-medium text-rose-800">
            {assetsQuery.error.message}
          </p>
        )}

        <CreativeStudioPanel
          skus={skus}
          onGenerate={handleGenerate}
          onSave={handleSave}
          onCopy={handleCopy}
        />

        <section
          aria-label="素材库"
          data-testid="studio-asset-library"
          className="rounded-2xl border border-slate-200/90 bg-white p-5 shadow-[0_1px_3px_rgba(15,23,42,0.06),0_6px_16px_-6px_rgba(15,23,42,0.05)]"
        >
          <h2 className="mb-3 flex items-center gap-2 text-base font-bold text-slate-900">
            <FileText className="h-4 w-4 text-blue-600" aria-hidden="true" />
            {bi("素材库", "Asset library")}
          </h2>
          {assets.length === 0 ? (
            <p className="text-[13px] text-slate-500">{bi("还没有保存的素材。", "No saved assets yet.")}</p>
          ) : (
            <ul className="space-y-2.5">
              {assets.map((asset) => (
                <li
                  key={asset.id}
                  data-testid={`studio-asset-${asset.id}`}
                  className="rounded-xl border border-slate-200/90 bg-white p-3.5 transition-shadow hover:shadow-[0_4px_14px_rgba(15,23,42,0.07)]"
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="min-w-0">
                      <p className="truncate text-sm font-semibold text-slate-900">{asset.title}</p>
                      <p className="text-[11px] text-slate-500">
                        {asset.asset_type} · {asset.language}
                        {asset.sku_ref ? ` · ${asset.sku_ref}` : ""} ·{" "}
                        {STATUS_LABELS[asset.status] ?? asset.status}
                      </p>
                    </div>
                    <div className="flex gap-1.5">
                      <button
                        type="button"
                        data-testid={`approve-asset-${asset.id}`}
                        disabled={asset.status === "approved" || review.isPending}
                        onClick={() => review.mutate({ id: asset.id, status: "approved" })}
                        className="inline-flex items-center gap-1 rounded-lg border border-emerald-200 bg-emerald-50/60 px-2.5 py-1 text-[11.5px] font-semibold text-emerald-800 transition-colors hover:bg-emerald-100 disabled:opacity-40"
                      >
                        <CheckCircle2 className="h-3 w-3" aria-hidden="true" />
                        {bi("批准", "Approve")}
                      </button>
                      <button
                        type="button"
                        data-testid={`archive-asset-${asset.id}`}
                        disabled={asset.status === "archived" || review.isPending}
                        onClick={() => review.mutate({ id: asset.id, status: "archived" })}
                        className="inline-flex items-center gap-1 rounded-lg border border-slate-200 bg-white px-2.5 py-1 text-[11.5px] font-semibold text-slate-700 transition-colors hover:bg-slate-100 disabled:opacity-40"
                      >
                        <Archive className="h-3 w-3" aria-hidden="true" />
                        {bi("归档", "Archive")}
                      </button>
                    </div>
                  </div>
                  <p className="mt-2 line-clamp-3 whitespace-pre-wrap break-words text-xs text-slate-700">
                    {asset.content_text}
                  </p>
                  {asset.numeric_guard_passed === false && asset.status === "draft" && (
                    <p className="mt-1.5 text-[11px] font-medium text-amber-800">
                      {bi("数字未被工具校验，发出前请人工核对。", "Numbers were not tool-verified; review before publishing.")}
                    </p>
                  )}
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </AppShell>
  );
}
