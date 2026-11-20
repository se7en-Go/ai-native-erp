"use client";

import { type FormEvent, useState } from "react";
import { Crosshair, Filter, ListChecks, MapPin, UploadCloud } from "lucide-react";
import { count, field, panel, primary } from "./presentation";
import type { CollectionMetrics, LeadCollectionRequest, LeadSourceProvider } from "./types";

const providers = {
  overture_poi: { label: "Overture Maps POI", help: "使用开放许可的地点数据，适合作为初始采集来源。" },
  overpass_osm: { label: "OpenStreetMap / Overpass", help: "遵循 ODbL 署名与再分发规则，由服务端控制公共接口配额。" },
  google_places: { label: "Google Places API (New)", help: "需配置租户自带密钥；地点详情的展示、保留和导出遵循 Google Places 条款。" },
  csv_import: { label: "CSV 导入", help: "仅导入有权使用的展会或企业目录数据；导入不等于同意营销触达。" },
} satisfies Record<LeadSourceProvider, { label: string; help: string }>;

const stats = [
  { key: "collected", label: "已采集", icon: MapPin }, { key: "cleaned", label: "已清洗", icon: Filter },
  { key: "synced", label: "已同步 ERP", icon: UploadCloud }, { key: "jobs", label: "采集任务", icon: ListChecks },
] as const;

export function LeadCollectorPanel({ metrics, onStart, pending = false }: {
  metrics: CollectionMetrics;
  onStart: (request: LeadCollectionRequest) => void | Promise<void>;
  pending?: boolean;
}) {
  const [source, setSource] = useState<LeadSourceProvider>("overture_poi");
  const [keywordText, setKeywordText] = useState("");
  const [region, setRegion] = useState("");
  const [limit, setLimit] = useState("100");
  const [rating, setRating] = useState("");
  const [requirePhone, setRequirePhone] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [feedback, setFeedback] = useState<{ ok: boolean; text: string } | null>(null);
  const isCsv = source === "csv_import";
  const busy = pending || submitting;
  const keywords = [...new Set(keywordText.split(/\r?\n/).map((keyword) => keyword.trim()).filter(Boolean))];
  const maxResults = Number(limit);
  const validLimit = Number.isSafeInteger(maxResults) && maxResults > 0;
  const validRating = !rating || /^(?:[0-4](?:\.\d{1,2})?|5(?:\.0{1,2})?)$/.test(rating);
  const canSubmit = validLimit && validRating && (isCsv ? !!file : !!(keywords.length && region.trim()));

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSubmit || busy) return;
    const filters = { keywords, target_region: region.trim(), max_results: maxResults, min_rating: rating || null, require_phone: requirePhone };
    const request: LeadCollectionRequest | null = source === "csv_import"
      ? file && { ...filters, source_provider: source, file }
      : { ...filters, source_provider: source };
    if (!request) return;
    setSubmitting(true);
    setFeedback(null);
    try {
      await onStart(request);
      setFeedback({ ok: true, text: "采集请求已提交。" });
    } catch {
      setFeedback({ ok: false, text: "采集请求提交失败，请重试。" });
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section aria-label="线索采集配置" className="space-y-4 text-slate-900">
      <div role="group" aria-label="线索采集关键指标" className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {stats.map(({ key, label, icon: Icon }) => <div key={key} className="min-w-0 rounded-xl border border-blue-100 bg-blue-50/65 p-3">
          <div className="flex items-center gap-1.5 text-xs text-blue-900"><Icon aria-hidden="true" size={14} />{label}</div>
          <p className="mt-1 break-all text-lg font-bold tabular-nums">{count(metrics[key])}</p>
        </div>)}
      </div>
      <form className={panel} onSubmit={submit} aria-busy={busy}>
        <h2 className="text-lg font-bold">配置采集任务</h2>
        <p className="mb-4 text-sm text-slate-600">按目标市场从许可数据源收集潜在企业。</p>
        <fieldset disabled={busy} className="space-y-4 disabled:opacity-65">
          <label className="block text-sm font-semibold">数据源
            <select value={source} className={field} onChange={(event) => { setSource(event.target.value as LeadSourceProvider); setFile(null); setFeedback(null); }}>
              {Object.entries(providers).map(([value, { label }]) => <option key={value} value={value}>{label}</option>)}
            </select>
          </label>
          <p className="rounded-lg bg-blue-50 px-3 py-2 text-xs leading-relaxed text-blue-900" aria-live="polite">{providers[source].help}</p>
          {isCsv && <label className="block text-sm font-semibold">CSV 文件
            <input type="file" accept=".csv,text/csv" aria-required="true" className={field} onChange={(event) => setFile(event.target.files?.[0] ?? null)} />
          </label>}
          <label className="block text-sm font-semibold">关键词
            <textarea rows={3} required={!isCsv} value={keywordText} onChange={(event) => setKeywordText(event.target.value)} className={field + " resize-y"} placeholder="每行一个，例如 lighting distributor" />
          </label>
          <label className="block text-sm font-semibold">目标城市 / 国家
            <input value={region} required={!isCsv} onChange={(event) => setRegion(event.target.value)} className={field} placeholder="例如 Berlin, DE" />
          </label>
          <div className="grid grid-cols-2 gap-3">
            <label className="block text-sm font-semibold">最大采集数
              <input type="number" min={1} step={1} required value={limit} onChange={(event) => setLimit(event.target.value)} className={field} />
            </label>
            <label className="block text-sm font-semibold">最低评分
              <input type="number" min={0} max={5} step="0.01" aria-invalid={!validRating} value={rating} onChange={(event) => setRating(event.target.value)} className={field} placeholder="不限" />
            </label>
          </div>
          {!validRating && <p role="alert" className="text-xs text-rose-700">请输入 0 到 5 之间、最多两位小数的评分。</p>}
          <label className="flex items-center gap-2 text-sm font-semibold"><input type="checkbox" role="switch" checked={requirePhone} onChange={(event) => setRequirePhone(event.target.checked)} className="accent-blue-700" />必须含电话</label>
          <button type="submit" className={primary} disabled={busy || !canSubmit}><Crosshair size={16} aria-hidden="true" />{busy ? "正在提交…" : "启动采集"}</button>
        </fieldset>
        {feedback && <p role={feedback.ok ? "status" : "alert"} className={"mt-3 text-sm " + (feedback.ok ? "text-blue-900" : "text-rose-700")}>{feedback.text}</p>}
      </form>
    </section>
  );
}
