"use client";

import React, { useMemo, useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Archive, CheckCircle2, FileText } from "lucide-react";

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
          <div>
            <h1 className="text-xl font-semibold tracking-tight text-slate-900">{bi("AI 素材工作台", "AI studio")}</h1>
            <p className="text-xs text-slate-500">
              {bi("关联真实商品主档 SKU 生成多语言文案；生成草稿永远从 draft 开始，批准动作由人工完成。", "Generate multilingual copy tied to real catalog SKUs; drafts always start as draft and approval stays manual.")}
            </p>
          </div>
          <Link
            href="/catalog"
            className="rounded border border-slate-200 px-2.5 py-1 text-[11px] font-medium text-slate-700 hover:bg-slate-100"
          >
            {bi("查看商品目录", "Open catalog")}
          </Link>
        </header>

        {providerBlocked && (
          <p
            role="alert"
            data-testid="studio-provider-unavailable"
            className="flex items-start gap-1.5 rounded border border-amber-300 bg-amber-50 p-2 text-xs text-amber-900"
          >
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
            <span>{t("aiProviderUnavailable")}</span>
          </p>
        )}

        {assetsQuery.error && (
          <p role="alert" data-testid="studio-error" className="rounded border border-rose-200 bg-rose-50 p-2 text-xs text-rose-800">
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
          className="rounded-2xl border border-slate-200 bg-white p-5 shadow-xs"
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
                  className="rounded-xl border border-slate-200 p-3"
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
                        className="inline-flex items-center gap-1 rounded border border-emerald-200 px-2 py-1 text-[11px] font-medium text-emerald-800 hover:bg-emerald-50 disabled:opacity-40"
                      >
                        <CheckCircle2 className="h-3 w-3" aria-hidden="true" />
                        {bi("批准", "Approve")}
                      </button>
                      <button
                        type="button"
                        data-testid={`archive-asset-${asset.id}`}
                        disabled={asset.status === "archived" || review.isPending}
                        onClick={() => review.mutate({ id: asset.id, status: "archived" })}
                        className="inline-flex items-center gap-1 rounded border border-slate-200 px-2 py-1 text-[11px] font-medium text-slate-700 hover:bg-slate-100 disabled:opacity-40"
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
