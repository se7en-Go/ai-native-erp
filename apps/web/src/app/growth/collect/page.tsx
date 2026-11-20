"use client";

import { useI18n } from "@/lib/i18n";

import React, { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ListChecks, ShieldCheck } from "lucide-react";

import { AppShell } from "@/components/app-shell";
import { GlobalProspectMap } from "@/components/growth/global-prospect-map";
import { LeadCollectorPanel } from "@/components/growth/lead-collector-panel";
import { ProspectCleaningTable } from "@/components/growth/prospect-cleaning-table";
import { count, panel } from "@/components/growth/presentation";
import { fetchPendingProposalsCount } from "@/lib/data-source";
import {
  cleanGrowthProspects,
  downloadGrowthProspectsCsv,
  fetchCollectionMetrics,
  fetchGrowthProspects,
  fetchLeadJobs,
  startCollection,
  syncGrowthProspectsToCrm,
} from "@/lib/phase3c-data";

const STATUS_LABEL: Record<string, string> = {
  pending: "待执行",
  running: "采集中",
  completed: "已完成",
  failed: "失败",
};

export default function GrowthCollectPage() {
  const { bi } = useI18n();
  const queryClient = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const proposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () => fetchPendingProposalsCount(),
  });
  const metricsQuery = useQuery({
    queryKey: ["growth", "summary"],
    queryFn: fetchCollectionMetrics,
  });
  const jobsQuery = useQuery({ queryKey: ["growth", "jobs"], queryFn: fetchLeadJobs });
  const prospectsQuery = useQuery({
    queryKey: ["growth", "prospects"],
    queryFn: () => fetchGrowthProspects(),
  });

  function invalidateGrowth() {
    void queryClient.invalidateQueries({ queryKey: ["growth"] });
  }

  const collect = useMutation({ mutationFn: startCollection, onSuccess: invalidateGrowth });
  const clean = useMutation({
    mutationFn: (ids: readonly string[]) => cleanGrowthProspects(ids),
    onSuccess: invalidateGrowth,
  });
  const sync = useMutation({
    mutationFn: (ids: readonly string[]) => syncGrowthProspectsToCrm(ids),
    onSuccess: invalidateGrowth,
  });
  const exportCsv = useMutation({
    mutationFn: (ids: readonly string[]) => downloadGrowthProspectsCsv(ids),
  });

  const prospects = useMemo(() => prospectsQuery.data ?? [], [prospectsQuery.data]);
  const jobs = jobsQuery.data ?? [];
  const metrics = metricsQuery.data ?? { collected: 0, cleaned: 0, synced: 0, jobs: 0 };
  const busy = collect.isPending || clean.isPending || sync.isPending || exportCsv.isPending;
  const error = metricsQuery.error ?? jobsQuery.error ?? prospectsQuery.error ?? collect.error;

  return (
    <AppShell
      currentPath="/growth/collect"
      pendingProposalsCount={proposalsQuery.data?.total ?? 0}
    >
      <div className="erp-page-full space-y-5">
        <header className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="text-xl font-semibold tracking-tight text-slate-900">{bi("海外地图获客中心", "Global lead map")}</h1>
            <p className="text-xs text-slate-500">
              {bi("仅接入合规数据源：Google Places API (New)（租户自带密钥）、OpenStreetMap Overpass（ODbL）、Overture 开放 POI，以及已获授权的展会 / 名录 CSV。系统不抓取 Meta / WhatsApp 数据，导入本身也不构成营销同意。", "Only compliant sources: Google Places API (New) with your own key, OpenStreetMap Overpass (ODbL), Overture open POI and authorised trade-show/directory CSVs. We never scrape Meta or WhatsApp data, and importing never implies marketing consent.")}
            </p>
          </div>
          <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-200 bg-emerald-50 px-2.5 py-1 text-[11px] font-semibold text-emerald-800">
            <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" />
            {bi("合规采集 · ODbL 署名与配额由服务端强制", "Compliant collection · ODbL attribution and quota enforced server-side")}
          </span>
        </header>

        {error ? (
          <p role="alert" className="rounded-xl border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800">
            {bi("获客数据加载失败，请稍后重试。", "Failed to load lead data. Please retry.")}
          </p>
        ) : null}

        <div className="grid gap-5 lg:grid-cols-[minmax(0,26rem)_minmax(0,1fr)]">
          <LeadCollectorPanel
            metrics={metrics}
            pending={collect.isPending}
            onStart={(request) => collect.mutateAsync(request).then(() => undefined)}
          />
          <GlobalProspectMap
            prospects={prospects}
            selectedId={selectedId}
            onSelect={setSelectedId}
          />
        </div>

        <section aria-label="采集任务记录" className={panel}>
          <header className="mb-3 flex items-center justify-between gap-2">
            <h2 className="flex items-center gap-2 text-lg font-bold">
              <ListChecks aria-hidden="true" size={18} />
              {bi("采集任务记录", "Collection jobs")}
            </h2>
            <span className="text-sm text-slate-600">共 {count(jobs.length)} 个任务</span>
          </header>
          {!jobs.length ? (
            <p className="rounded-xl border border-dashed border-slate-300 p-4 text-sm text-slate-600">
              {bi("还没有采集任务。选择合规数据源并配置目标市场后即可开始。", "No collection jobs yet. Pick a compliant source and target market to start.")}
            </p>
          ) : (
            <div className="overflow-x-auto rounded-xl border border-slate-200">
              <table aria-label="采集任务列表" className="w-full min-w-[36rem] text-left text-sm">
                <thead className="bg-slate-50 text-slate-600">
                  <tr>
                    {["数据源", "目标区域", "关键词", "状态", "采集", "已清洗", "已同步"].map(
                      (label) => (
                        <th key={label} scope="col" className="whitespace-nowrap p-3 font-semibold">
                          {label}
                        </th>
                      )
                    )}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {jobs.map((job) => (
                    <tr key={job.id} className="hover:bg-slate-50">
                      <th scope="row" className="whitespace-nowrap p-3 font-semibold">
                        {job.source_provider}
                      </th>
                      <td className="p-3 text-slate-600">{job.target_region || "—"}</td>
                      <td className="max-w-64 break-words p-3 text-slate-600">
                        {job.keywords.length ? job.keywords.join(" / ") : bi("CSV 导入", "CSV import")}
                      </td>
                      <td className="whitespace-nowrap p-3">
                        <span
                          className={
                            "rounded-full px-2 py-0.5 text-xs font-semibold " +
                            (job.status === "completed"
                              ? "bg-emerald-100 text-emerald-900"
                              : job.status === "failed"
                                ? "bg-rose-100 text-rose-900"
                                : "bg-slate-100 text-slate-700")
                          }
                        >
                          {STATUS_LABEL[job.status] ?? job.status}
                        </span>
                        {job.error_message ? (
                          <p className="mt-1 text-xs text-rose-700">{job.error_message}</p>
                        ) : null}
                      </td>
                      <td className="p-3 tabular-nums">{count(job.collected_count)}</td>
                      <td className="p-3 tabular-nums">{count(job.cleaned_count)}</td>
                      <td className="p-3 tabular-nums">{count(job.synced_count)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        <ProspectCleaningTable
          prospects={prospects}
          selectedId={selectedId}
          onSelect={setSelectedId}
          pending={busy}
          onClean={(ids) => clean.mutateAsync(ids).then(() => undefined)}
          onSync={(ids) => sync.mutateAsync(ids).then(() => undefined)}
          onExportCsv={(ids) => exportCsv.mutateAsync(ids).then(() => undefined)}
        />
      </div>
    </AppShell>
  );
}
