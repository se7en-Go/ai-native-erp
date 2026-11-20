"use client";

import { useI18n } from "@/lib/i18n";

import React, { type FormEvent, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AtSign, Building2, ListChecks, MailSearch, MapPinned, ShieldCheck } from "lucide-react";

import { AppShell } from "@/components/app-shell";
import { Page, PageHeader } from "@/components/ui/page";
import { GlobalProspectMap } from "@/components/growth/global-prospect-map";
import { LeadCollectorPanel } from "@/components/growth/lead-collector-panel";
import { ProspectCleaningTable } from "@/components/growth/prospect-cleaning-table";
import { count, field, panel } from "@/components/growth/presentation";
import type { Prospect } from "@/components/growth/types";
import { fetchPendingProposalsCount } from "@/lib/data-source";
import {
  type BatchCollectionJob,
  type BatchCollectionRequest,
  type CollectionProviderMode,
  type EnrichProspectEmailsResponse,
  createAndRunBatchCollection,
  enrichProspectEmails,
  fetchProvenance,
  jobSourcePills,
  parseCities,
  parseKeywords,
  sourceLabel,
} from "@/lib/api-client/pack-c-growth";
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

const PROVIDER_MODES: readonly {
  value: CollectionProviderMode;
  zh: string;
  en: string;
  noteZh: string;
  noteEn: string;
}[] = [
  {
    value: "auto",
    zh: "自动（有 Google Key 用 Places，无 Key 免费用 OSM Overpass）",
    en: "Auto (Places when a Google key exists, free OSM Overpass otherwise)",
    noteZh: "服务端按租户密钥自动选路；无需 Key 也能先跑通海外获客。",
    noteEn: "The server picks the route from your tenant key; no key is still fully usable.",
  },
  {
    value: "overpass",
    zh: "OpenStreetMap Overpass（免费免 Key）",
    en: "OpenStreetMap Overpass (free, no key)",
    noteZh: "ODbL 开放数据，零成本起步；由服务端限速并强制署名。",
    noteEn: "ODbL open data at zero cost; the server rate-limits it and enforces attribution.",
  },
  {
    value: "google_places",
    zh: "Google Places API",
    en: "Google Places API",
    noteZh: "评分与营业状态更全，需要租户自带 Google Places 密钥与配额。",
    noteEn: "Richer ratings and business status, needs your own Google Places key and quota.",
  },
];

const emeraldPrimary =
  "inline-flex items-center justify-center gap-2 rounded-xl bg-emerald-600 px-3.5 py-2 text-[13px] font-semibold text-white shadow-[0_2px_8px_-2px_rgba(5,150,105,0.55)] transition-colors hover:bg-emerald-700 focus-visible:outline-2 focus-visible:outline-emerald-700 focus-visible:outline-offset-2 disabled:shadow-none disabled:opacity-50";
const sourcePill =
  "inline-flex items-center gap-1 rounded-md border border-emerald-200 bg-emerald-50/90 px-1.5 py-0.5 text-[10.5px] font-semibold text-emerald-800";

/** Compliance banner: public maps + published website contacts only. */
function ComplianceNotice() {
  const { bi } = useI18n();
  return (
    <section
      aria-label={bi("合规采集声明", "Compliant collection notice")}
      data-testid="collect-compliance-notice"
      className="flex flex-wrap items-start gap-3 rounded-2xl border border-emerald-200/80 bg-emerald-50/70 p-4"
    >
      <span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-white text-emerald-700 ring-1 ring-emerald-200">
        <ShieldCheck aria-hidden="true" size={17} />
      </span>
      <div className="min-w-0 flex-1">
        <p className="text-[13px] font-bold text-emerald-950">
          {bi(
            "只采公开地图与官网公开联系方式 · 无伪造账号、无封号风险",
            "Public maps and published website contacts only · no fake accounts, no ban risk"
          )}
        </p>
        <p className="mt-1 text-[12.5px] leading-6 text-emerald-900/85">
          {bi(
            "不模拟登录、不绕验证码、不抓取 WhatsApp / Facebook 私域数据。企业名称、地址、电话来自 Google Places 或 OpenStreetMap Overpass 的公开 POI；邮箱只从企业官网公开的联系页面提取（公开网页 + 公开邮箱）。ODbL 署名、数据源配额与退订抑制全部由服务端强制执行。",
            "No login simulation, no captcha bypass, no WhatsApp / Facebook private-data scraping. Company names, addresses and phones come from public Google Places or OpenStreetMap Overpass POIs; e-mails are only harvested from the contact pages a company publishes (public web + public mailboxes). ODbL attribution, source quotas and suppression are enforced server-side."
          )}
        </p>
      </div>
    </section>
  );
}

/** Multi-city / multi-keyword batch recipe with an explicit data-source mode. */
function BatchCollectionPanel({
  pending,
  onStart,
}: {
  pending: boolean;
  onStart: (request: BatchCollectionRequest) => Promise<BatchCollectionJob | null>;
}) {
  const { bi, locale } = useI18n();
  const [citiesText, setCitiesText] = useState("");
  const [keywordsText, setKeywordsText] = useState("");
  const [providerMode, setProviderMode] = useState<CollectionProviderMode>("auto");
  const [limit, setLimit] = useState("120");
  const [rating, setRating] = useState("");
  const [feedback, setFeedback] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const cities = parseCities(citiesText);
  const keywords = parseKeywords(keywordsText);
  const mode = PROVIDER_MODES.find((row) => row.value === providerMode) ?? PROVIDER_MODES[0];
  const maxResults = Number(limit);
  const validLimit = Number.isSafeInteger(maxResults) && maxResults >= 1 && maxResults <= 1000;
  const validRating = !rating || /^(?:[0-4](?:\.\d{1,2})?|5(?:\.0{1,2})?)$/.test(rating);
  const busy = pending || submitting;
  const canSubmit = cities.length > 0 && keywords.length > 0 && validLimit && validRating && !busy;

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    setFeedback(null);
    try {
      const job = await onStart({
        cities,
        keywords,
        provider_mode: providerMode,
        max_results: maxResults,
        min_rating: rating || null,
      });
      const pills = job
        ? jobSourcePills(job)
            .map((pill) => `${sourceLabel(pill.key, locale)}${pill.count === null ? "" : ` ${count(pill.count)}`}`)
            .join(" + ")
        : sourceLabel(providerMode === "google_places" ? "google_places" : "overpass_osm", locale);
      setFeedback({
        tone: "ok",
        text: bi(
          `批量采集已提交：覆盖 ${cities.length} 个城市 · ${keywords.length} 个关键词 · 采集 ${count(job?.collected_count ?? 0)} 条线索（来源 ${pills}）`,
          `Batch collection submitted: ${cities.length} cities · ${keywords.length} keywords · ${count(job?.collected_count ?? 0)} leads (source ${pills})`
        ),
      });
    } catch {
      setFeedback({
        tone: "error",
        text: bi(
          "批量采集提交失败：请检查城市与关键词，或稍后重试。",
          "Batch collection failed. Check the cities and keywords, then retry."
        ),
      });
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section aria-label={bi("多城市批量采集", "Multi-city batch collection")} className={panel}>
      <header className="mb-3 flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 className="flex items-center gap-2 text-[15px] font-bold text-slate-900">
            <span className="grid h-7 w-7 place-items-center rounded-lg bg-emerald-50 text-emerald-700 ring-1 ring-emerald-100">
              <MapPinned aria-hidden="true" size={15} />
            </span>
            {bi("多城市批量采集", "Multi-city batch collection")}
          </h2>
          <p className="mt-1 text-[12.5px] leading-6 text-slate-600">
            {bi(
              "一次提交多个城市与多个关键词，服务端按城市边界框逐城采集后统一清洗去重。",
              "Submit many cities and keywords at once; the server collects per city bounding box, then cleans and de-duplicates."
            )}
          </p>
        </div>
        <span className="rounded-full border border-emerald-200 bg-emerald-50 px-2.5 py-0.5 text-[11px] font-semibold text-emerald-800">
          {bi("免 Key 可跑通", "Runs without a key")}
        </span>
      </header>

      <form onSubmit={submit} aria-busy={busy} className="space-y-3">
        <div className="grid gap-3 lg:grid-cols-2">
          <label htmlFor="collect-cities" className="block text-[13px] font-semibold text-slate-800">
            {bi("目标城市（逗号或换行分隔，最多 10 个）", "Target cities (comma or newline separated, max 10)")}
            <textarea
              id="collect-cities"
              data-testid="collect-cities-input"
              rows={2}
              value={citiesText}
              disabled={busy}
              onChange={(event) => {
                setCitiesText(event.target.value);
                setFeedback(null);
              }}
              placeholder="Dubai, Riyadh, Warsaw"
              className={field + " resize-y font-medium"}
            />
          </label>
          <label htmlFor="collect-keywords" className="block text-[13px] font-semibold text-slate-800">
            {bi("批量关键词（逗号或换行分隔，最多 20 个）", "Batch keywords (comma or newline separated, max 20)")}
            <textarea
              id="collect-keywords"
              data-testid="collect-keywords-input"
              rows={2}
              value={keywordsText}
              disabled={busy}
              onChange={(event) => {
                setKeywordsText(event.target.value);
                setFeedback(null);
              }}
              placeholder="LED strip wholesaler, lighting distributor"
              className={field + " resize-y font-medium"}
            />
          </label>
        </div>

        <div className="grid gap-3 sm:grid-cols-3">
          <label htmlFor="collect-provider-mode" className="block text-[13px] font-semibold text-slate-800 sm:col-span-3 lg:col-span-1">
            {bi("数据源模式", "Data-source mode")}
            <select
              id="collect-provider-mode"
              data-testid="collect-provider-mode"
              value={providerMode}
              disabled={busy}
              onChange={(event) => {
                setProviderMode(event.target.value as CollectionProviderMode);
                setFeedback(null);
              }}
              className={field}
            >
              {PROVIDER_MODES.map((row) => (
                <option key={row.value} value={row.value}>
                  {bi(row.zh, row.en)}
                </option>
              ))}
            </select>
          </label>
          <label htmlFor="collect-batch-limit" className="block text-[13px] font-semibold text-slate-800">
            {bi("单任务线索上限", "Lead cap per job")}
            <input
              id="collect-batch-limit"
              type="number"
              min={1}
              max={1000}
              step={1}
              aria-invalid={!validLimit}
              value={limit}
              disabled={busy}
              onChange={(event) => setLimit(event.target.value)}
              className={field}
            />
          </label>
          <label htmlFor="collect-batch-rating" className="block text-[13px] font-semibold text-slate-800">
            {bi("批量最低评分", "Minimum rating")}
            <input
              id="collect-batch-rating"
              type="number"
              min={0}
              max={5}
              step="0.01"
              aria-invalid={!validRating}
              value={rating}
              disabled={busy}
              onChange={(event) => setRating(event.target.value)}
              placeholder={bi("不限", "Any")}
              className={field}
            />
          </label>
        </div>

        <p className="rounded-xl border border-slate-200 bg-slate-50/80 px-3 py-2 text-[12px] leading-relaxed font-medium text-slate-700">
          {bi(mode.noteZh, mode.noteEn)}
        </p>
        {!validLimit ? (
          <p role="alert" className="text-xs font-medium text-rose-700">
            {bi("单任务线索上限需为 1 到 1000 的整数。", "The lead cap must be an integer between 1 and 1000.")}
          </p>
        ) : null}
        {!validRating ? (
          <p role="alert" className="text-xs font-medium text-rose-700">
            {bi("请输入 0 到 5 之间、最多两位小数的评分。", "Enter a rating between 0 and 5 with at most two decimals.")}
          </p>
        ) : null}

        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="rounded-full border border-slate-200 bg-white px-2.5 py-0.5 text-[11.5px] font-semibold tabular-nums text-slate-700" aria-live="polite">
            {bi("已识别", "Detected")} {cities.length} {bi("个城市", "cities")} · {keywords.length}{" "}
            {bi("个关键词", "keywords")}
          </p>
          <button type="submit" disabled={!canSubmit} className={emeraldPrimary}>
            <MapPinned aria-hidden="true" size={15} />
            {bi("开始批量采集", "Run batch collection")}
          </button>
        </div>

        {feedback ? (
          <p
            role={feedback.tone === "error" ? "alert" : "status"}
            className={
              "rounded-xl border p-3 text-[12.5px] font-medium " +
              (feedback.tone === "error"
                ? "border-rose-200 bg-rose-50 text-rose-800"
                : "border-emerald-200 bg-emerald-50 text-emerald-900")
            }
          >
            {feedback.text}
          </p>
        ) : null}
      </form>
    </section>
  );
}

/** Website e-mail enrichment: the prospects action toolbar plus provenance pills. */
function ProspectEmailPanel({
  prospects,
  provenance,
  pending,
  onEnrich,
}: {
  prospects: readonly Prospect[];
  provenance: Record<string, string>;
  pending: boolean;
  onEnrich: (ids: readonly string[]) => Promise<EnrichProspectEmailsResponse | null>;
}) {
  const { bi, locale } = useI18n();
  const [checked, setChecked] = useState<readonly string[]>([]);
  const [result, setResult] = useState<EnrichProspectEmailsResponse | null>(null);
  const [error, setError] = useState(false);
  const [running, setRunning] = useState(false);

  const missing = useMemo(() => prospects.filter((row) => !row.email), [prospects]);
  const scope = checked.length ? prospects.filter((row) => checked.includes(row.id)) : missing;
  const scopeIds = scope.map((row) => row.id);
  const busy = pending || running;
  const enrichedById = useMemo(() => {
    const map = new Map<string, EnrichProspectEmailsResponse["items"][number]>();
    for (const item of result?.items ?? []) map.set(item.prospect_id, item);
    return map;
  }, [result]);

  async function run() {
    if (busy || !scopeIds.length) return;
    setRunning(true);
    setError(false);
    try {
      const response = await onEnrich(scopeIds);
      if (response) setResult(response);
    } catch {
      setError(true);
    } finally {
      setRunning(false);
    }
  }

  const skippedNoWebsite = (result?.items ?? []).filter(
    (item) => item.status === "skipped_no_website"
  ).length;
  const alreadyHadEmail = (result?.items ?? []).filter(
    (item) => item.status === "already_has_email"
  ).length;

  return (
    <section aria-label={bi("线索邮箱提取", "Prospect e-mail enrichment")} className={panel}>
      <header className="mb-3 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="flex items-center gap-2 text-[15px] font-bold text-slate-900">
            <span className="grid h-7 w-7 place-items-center rounded-lg bg-emerald-50 text-emerald-700 ring-1 ring-emerald-100">
              <MailSearch aria-hidden="true" size={15} />
            </span>
            {bi("官网公开邮箱提取", "Published website e-mails")}
          </h2>
          <p className="mt-1 text-[12.5px] leading-6 text-slate-600">
            {bi(
              "勾选线索以限定范围；未勾选时自动扫描所有缺邮箱的线索。只读取企业官网公开的联系页面，不猜测、不拼接邮箱。",
              "Tick prospects to narrow the scope; with nothing ticked every prospect missing an e-mail is scanned. Only public contact pages are read — addresses are never guessed."
            )}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            data-testid="enrich-emails-btn"
            className={emeraldPrimary}
            disabled={busy || !scopeIds.length}
            onClick={() => void run()}
          >
            <AtSign aria-hidden="true" size={15} />
            {bi("官网提取邮箱", "Extract website e-mails")}
          </button>
        </div>
      </header>

      {error ? (
        <p role="alert" className="mb-3 rounded-xl border border-rose-200 bg-rose-50 p-3 text-[12.5px] font-medium text-rose-800">
          {bi("邮箱提取失败，请确认线索官网可访问后重试。", "E-mail extraction failed. Check the prospect website, then retry.")}
        </p>
      ) : null}

      {result ? (
        <div
          role="status"
          data-testid="enrich-emails-summary"
          className="mb-3 rounded-xl border border-emerald-200 bg-emerald-50/80 p-3"
        >
          <p className="text-[13px] font-bold text-emerald-950">
            {bi(
              `已扫描 ${count(result.checked_count)} 家官网，成功提取 ${count(result.enriched_count)} 个采购邮箱并更新评级`,
              `Scanned ${count(result.checked_count)} websites, extracted ${count(result.enriched_count)} buyer mailboxes and refreshed grades`
            )}
          </p>
          {skippedNoWebsite || alreadyHadEmail ? (
            <p className="mt-1 text-[11.5px] font-medium text-emerald-900/80">
              {bi(
                `其中 ${count(skippedNoWebsite)} 家缺官网、${count(alreadyHadEmail)} 家已有邮箱（未覆盖）`,
                `${count(skippedNoWebsite)} without a website, ${count(alreadyHadEmail)} already had a mailbox (kept)`
              )}
            </p>
          ) : null}
        </div>
      ) : null}

      {!prospects.length ? (
        <p className="rounded-xl border border-dashed border-slate-300 p-4 text-[12.5px] font-medium text-slate-600">
          {bi("还没有线索。先完成一次批量采集。", "No prospects yet. Run a batch collection first.")}
        </p>
      ) : (
        <div className="overflow-x-auto rounded-2xl border border-slate-200">
          <table aria-label={bi("线索邮箱覆盖", "Prospect e-mail coverage")} className="w-full min-w-[40rem] text-left text-[13px]">
            <thead className="bg-slate-100/80 text-[11.5px] font-semibold text-slate-700">
              <tr>
                <th scope="col" className="p-3">
                  <input
                    type="checkbox"
                    aria-label={bi("选择所有线索用于邮箱提取", "Select every prospect for e-mail extraction")}
                    checked={checked.length === prospects.length && prospects.length > 0}
                    disabled={busy}
                    className="accent-emerald-600"
                    onChange={(event) =>
                      setChecked(event.target.checked ? prospects.map((row) => row.id) : [])
                    }
                  />
                </th>
                {[bi("企业", "Company"), bi("官网", "Website"), bi("采购邮箱", "Buyer mailbox"), bi("来源", "Source"), bi("评级", "Grade")].map(
                  (label) => (
                    <th key={label} scope="col" className="whitespace-nowrap p-3 font-semibold">
                      {label}
                    </th>
                  )
                )}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {prospects.map((row) => {
                const enriched = enrichedById.get(row.id);
                const email = enriched?.email ?? row.email;
                const provider = provenance[row.id];
                return (
                  <tr key={row.id} className="transition-colors hover:bg-emerald-50/40">
                    <td className="p-3">
                      <input
                        type="checkbox"
                        aria-label={bi(`选择 ${row.company_name} 用于邮箱提取`, `Select ${row.company_name} for e-mail extraction`)}
                        checked={checked.includes(row.id)}
                        disabled={busy}
                        className="accent-emerald-600"
                        onChange={(event) =>
                          setChecked((prev) =>
                            event.target.checked ? [...prev, row.id] : prev.filter((id) => id !== row.id)
                          )
                        }
                      />
                    </td>
                    <th scope="row" className="max-w-56 break-words p-3 font-semibold text-slate-900">
                      {row.company_name}
                    </th>
                    <td className="max-w-56 break-words p-3 text-slate-700">
                      {row.website ? (
                        <a
                          href={row.website.startsWith("http") ? row.website : `https://${row.website}`}
                          target="_blank"
                          rel="noreferrer"
                          className="inline-flex items-center gap-1 text-emerald-800 hover:underline"
                        >
                          <Building2 aria-hidden="true" size={13} />
                          {row.website.replace(/^https?:\/\//, "")}
                        </a>
                      ) : (
                        <span className="text-slate-400">{bi("无官网", "None")}</span>
                      )}
                    </td>
                    <td className="max-w-56 break-words p-3">
                      {email ? (
                        <span className="font-medium text-slate-800">{email}</span>
                      ) : (
                        <span className="rounded-md border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-[10.5px] font-semibold text-amber-800">
                          {bi("待提取", "Missing")}
                        </span>
                      )}
                    </td>
                    <td className="whitespace-nowrap p-3">
                      {provider ? (
                        <span className={sourcePill}>{sourceLabel(provider, locale)}</span>
                      ) : (
                        <span className="text-[11.5px] text-slate-400">—</span>
                      )}
                    </td>
                    <td className="p-3">
                      <span className="inline-flex h-5 w-5 items-center justify-center rounded-full bg-emerald-100 text-[11px] font-bold text-emerald-800 ring-1 ring-emerald-200">
                        {enriched?.grade ?? row.grade}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export default function GrowthCollectPage() {
  const { bi, locale } = useI18n();
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
  const provenanceQuery = useQuery({
    queryKey: ["growth", "provenance"],
    queryFn: fetchProvenance,
  });

  function invalidateGrowth() {
    void queryClient.invalidateQueries({ queryKey: ["growth"] });
  }

  const collect = useMutation({ mutationFn: startCollection, onSuccess: invalidateGrowth });
  const batch = useMutation({
    mutationFn: (request: BatchCollectionRequest) => createAndRunBatchCollection(request),
    onSuccess: invalidateGrowth,
    // A failed batch still persists a `failed` job row with its audit message.
    onError: invalidateGrowth,
  });
  const enrich = useMutation({
    mutationFn: (ids: readonly string[]) => enrichProspectEmails(ids),
    onSuccess: invalidateGrowth,
  });
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
  const provenance = provenanceQuery.data ?? { jobs: {}, prospects: {} };
  const jobs = jobsQuery.data ?? [];
  const metrics = metricsQuery.data ?? { collected: 0, cleaned: 0, synced: 0, jobs: 0 };
  const busy =
    collect.isPending || batch.isPending || enrich.isPending || clean.isPending || sync.isPending || exportCsv.isPending;
  const error = metricsQuery.error ?? jobsQuery.error ?? prospectsQuery.error ?? collect.error;

  return (
    <AppShell
      currentPath="/growth/collect"
      pendingProposalsCount={proposalsQuery.data?.total ?? 0}
    >
      <Page full className="space-y-5">
        <PageHeader
          title={bi("海外地图获客中心", "Global lead map")}
          description={bi(
            "多城市批量采集公开 POI，并从企业官网公开联系页提取采购邮箱；仅接入合规数据源：Google Places API (New)（租户自带密钥）、OpenStreetMap Overpass（ODbL）与已获授权的展会 / 名录 CSV。系统不抓取 Meta / WhatsApp 数据，导入本身也不构成营销同意。",
            "Batch-collect public POIs across cities and extract buyer e-mails from the contact pages companies publish. Only compliant sources: Google Places API (New) with your own key, OpenStreetMap Overpass (ODbL) and authorised trade-show/directory CSVs. We never scrape Meta or WhatsApp data, and importing never implies marketing consent."
          )}
          badge={
            <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-200 bg-emerald-50 px-2.5 py-1 text-[11px] font-semibold text-emerald-800">
              <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" />
              {bi(
                "合规采集 · ODbL 署名与配额由服务端强制",
                "Compliant collection · ODbL attribution and quota enforced server-side"
              )}
            </span>
          }
        />

        <ComplianceNotice />

        <BatchCollectionPanel
          pending={batch.isPending}
          onStart={(request) => batch.mutateAsync(request)}
        />

        {error ? (
          <p role="alert" className="rounded-xl border border-rose-200 bg-rose-50 p-3 text-[13px] font-medium text-rose-800">
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
          <header className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <h2 className="flex items-center gap-2 text-[15px] font-bold text-slate-900">
              <span className="grid h-7 w-7 place-items-center rounded-lg bg-violet-50 text-violet-600 ring-1 ring-violet-100">
                <ListChecks aria-hidden="true" size={15} />
              </span>
              {bi("采集任务记录", "Collection jobs")}
            </h2>
            <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-0.5 text-[11.5px] font-semibold tabular-nums text-slate-700">
              共 {count(jobs.length)} 个任务
            </span>
          </header>
          {!jobs.length ? (
            <p className="rounded-xl border border-dashed border-slate-300 p-4 text-sm text-slate-600">
              {bi("还没有采集任务。选择合规数据源并配置目标市场后即可开始。", "No collection jobs yet. Pick a compliant source and target market to start.")}
            </p>
          ) : (
            <div className="overflow-x-auto rounded-2xl border border-slate-200">
              <table aria-label="采集任务列表" className="w-full min-w-[36rem] text-left text-[13px]">
                <thead className="bg-slate-100/80 text-[11.5px] font-semibold text-slate-700">
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
                    <tr key={job.id} className="transition-colors hover:bg-blue-50/50">
                      <th scope="row" className="p-3 font-semibold text-slate-900">
                        <span className="flex flex-wrap items-center gap-1">
                          {jobSourcePills({
                            source_provider: job.source_provider,
                            sources: provenance.jobs[job.id],
                          }).map((pill) => (
                            <span key={pill.key} data-testid="job-source-pill" className={sourcePill}>
                              {sourceLabel(pill.key, locale)}
                              {pill.count === null ? null : (
                                <span className="tabular-nums text-emerald-700/80">· {count(pill.count)}</span>
                              )}
                            </span>
                          ))}
                        </span>
                      </th>
                      <td className="p-3 text-slate-700">{job.target_region || "—"}</td>
                      <td className="max-w-64 break-words p-3 text-slate-700">
                        {job.keywords.length ? job.keywords.join(" / ") : bi("CSV 导入", "CSV import")}
                      </td>
                      <td className="whitespace-nowrap p-3">
                        <span
                          className={
                            "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[10.5px] font-semibold " +
                            (job.status === "completed"
                              ? "border-emerald-200 bg-emerald-50 text-emerald-800"
                              : job.status === "failed"
                                ? "border-rose-200 bg-rose-50 text-rose-800"
                                : "border-slate-200 bg-slate-100 text-slate-700")
                          }
                        >
                          <span
                            aria-hidden="true"
                            className={
                              "h-1.5 w-1.5 rounded-full " +
                              (job.status === "completed"
                                ? "bg-emerald-500"
                                : job.status === "failed"
                                  ? "bg-rose-500"
                                  : "bg-slate-400")
                            }
                          />
                          {STATUS_LABEL[job.status] ?? job.status}
                        </span>
                        {job.error_message ? (
                          <p className="mt-1 text-xs text-rose-700">{job.error_message}</p>
                        ) : null}
                      </td>
                      <td className="p-3 font-medium tabular-nums text-slate-800">
                        {count(job.collected_count)}
                      </td>
                      <td className="p-3 font-medium tabular-nums text-slate-800">
                        {count(job.cleaned_count)}
                      </td>
                      <td className="p-3 font-medium tabular-nums text-emerald-700">
                        {count(job.synced_count)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        <ProspectEmailPanel
          prospects={prospects}
          provenance={provenance.prospects}
          pending={enrich.isPending}
          onEnrich={(ids) => enrich.mutateAsync(ids)}
        />

        <ProspectCleaningTable
          prospects={prospects}
          selectedId={selectedId}
          onSelect={setSelectedId}
          pending={busy}
          onClean={(ids) => clean.mutateAsync(ids).then(() => undefined)}
          onSync={(ids) => sync.mutateAsync(ids).then(() => undefined)}
          onExportCsv={(ids) => exportCsv.mutateAsync(ids).then(() => undefined)}
        />
      </Page>
    </AppShell>
  );
}
