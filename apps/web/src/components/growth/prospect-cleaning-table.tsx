"use client";

import { useState } from "react";
import { ArrowDownToLine, DatabaseZap, ListFilter } from "lucide-react";
import { button, field, gradeTone, panel, primary, syncLabel } from "./presentation";
import type { Prospect, ProspectGrade, ProspectSyncStatus } from "./types";

export function ProspectCleaningTable({ prospects, selectedId, onSelect, onClean, onSync, onExportCsv, pending = false }: {
  prospects: readonly Prospect[];
  selectedId?: string | null;
  onSelect?: (id: string) => void;
  onClean: (ids: readonly string[]) => void | Promise<void>;
  onSync: (ids: readonly string[]) => void | Promise<void>;
  /** Delegate to the source-aware export API instead of serializing provider data in the browser. */
  onExportCsv: (ids: readonly string[]) => void | Promise<void>;
  pending?: boolean;
}) {
  const [grade, setGrade] = useState<ProspectGrade | "">("");
  const [status, setStatus] = useState<ProspectSyncStatus | "">("");
  const [checked, setChecked] = useState<readonly string[]>([]);
  const [operation, setOperation] = useState<"clean" | "sync" | "export" | null>(null);
  const [error, setError] = useState(false);
  const visible = prospects.filter((row) => (!grade || row.grade === grade) && (!status || row.sync_status === status));
  const checkedRows = visible.filter((row) => checked.includes(row.id));
  const scope = checkedRows.length ? checkedRows : visible;
  const cleanIds = scope.filter((row) => row.sync_status === "raw").map((row) => row.id);
  const syncIds = scope.filter((row) => row.sync_status === "cleaned" && !row.crm_lead_id).map((row) => row.id);
  const exportIds = scope.map((row) => row.id);
  const busy = pending || !!operation;

  async function run(action: "clean" | "sync" | "export") {
    const ids = action === "clean" ? cleanIds : action === "sync" ? syncIds : exportIds;
    const handler = action === "clean" ? onClean : action === "sync" ? onSync : onExportCsv;
    if (busy || !ids.length) return;
    setOperation(action);
    setError(false);
    try { await handler(ids); }
    catch { setError(true); }
    finally { setOperation(null); }
  }

  return (
    <section aria-label="线索清洗池" aria-busy={busy} className={panel}>
      <header className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-bold">线索清洗池</h2>
          <p className="text-sm text-slate-600">勾选线索以限定批量操作范围；未勾选时使用当前筛选结果。</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button type="button" className={button} disabled={busy || !cleanIds.length} onClick={() => void run("clean")}><ListFilter aria-hidden="true" size={15} />批量去重清洗</button>
          <button type="button" className={button} disabled={busy || !exportIds.length} onClick={() => void run("export")}><ArrowDownToLine aria-hidden="true" size={15} />导出 CSV</button>
          <button type="button" className={primary} disabled={busy || !syncIds.length} onClick={() => void run("sync")}><DatabaseZap aria-hidden="true" size={15} />一键同步到 ERP 线索池</button>
        </div>
      </header>
      <div className="mb-3 flex flex-wrap items-end gap-3">
        <label className="text-sm font-medium">评级筛选
          <select value={grade} disabled={busy} onChange={(event) => { setGrade(event.target.value as ProspectGrade | ""); setChecked([]); }} className={field}>
            <option value="">全部评级</option>{["A", "B", "C"].map((item) => <option key={item} value={item}>{item} 级</option>)}
          </select>
        </label>
        <label className="text-sm font-medium">状态筛选
          <select value={status} disabled={busy} onChange={(event) => { setStatus(event.target.value as ProspectSyncStatus | ""); setChecked([]); }} className={field}>
            <option value="">全部状态</option>{Object.entries(syncLabel).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </label>
        <p className="pb-2 text-sm text-slate-600" aria-live="polite">{visible.length} 条线索 · 已选 {checkedRows.length} 条</p>
      </div>
      {error && <p role="alert" className="mb-3 text-sm text-rose-700">操作未完成，请重试。</p>}
      {!visible.length ? <p className="rounded-xl border border-dashed border-slate-300 p-4 text-sm text-slate-600">没有符合条件的线索。</p> : (
        <div className="overflow-x-auto rounded-xl border border-slate-200">
          <table aria-label="线索清洗结果" className="w-full min-w-[38rem] text-left text-sm">
            <thead className="bg-slate-50 text-slate-600"><tr>
              <th scope="col" className="p-3"><input type="checkbox" aria-label="选择所有显示线索" disabled={busy} checked={checkedRows.length === visible.length} className="accent-blue-700" onChange={(event) => setChecked(event.target.checked ? visible.map((row) => row.id) : [])} /></th>
              {["企业", "地区", "评级", "评分", "WhatsApp", "状态"].map((label) => <th scope="col" className="whitespace-nowrap p-3 font-semibold" key={label}>{label}</th>)}
            </tr></thead>
            <tbody className="divide-y divide-slate-100">
              {visible.map((row) => <tr key={row.id} className={selectedId === row.id ? "bg-blue-50" : "hover:bg-slate-50"}>
                <td className="p-3"><input type="checkbox" aria-label={"选择 " + row.company_name} checked={checked.includes(row.id)} disabled={busy} className="accent-blue-700" onChange={(event) => setChecked((prev) => event.target.checked ? [...prev, row.id] : prev.filter((id) => id !== row.id))} /></td>
                <th scope="row" className="max-w-64 break-words p-3 font-semibold">{onSelect ? <button type="button" aria-pressed={selectedId === row.id} className="text-left text-blue-800 hover:underline" onClick={() => onSelect(row.id)}>{row.company_name}</button> : row.company_name}</th>
                <td className="p-3 text-slate-600">{[row.city, row.country].filter(Boolean).join(" · ") || "—"}</td>
                <td className="p-3"><span className={"rounded-full px-2 py-0.5 font-semibold " + gradeTone[row.grade]}>{row.grade}</span></td>
                <td className="p-3 tabular-nums">{row.rating ?? "—"}</td>
                <td className="whitespace-nowrap p-3"><span className={"rounded-full px-2 py-0.5 text-xs font-medium " + (row.whatsapp_reachable === true ? "bg-emerald-100 text-emerald-900" : "bg-slate-100 text-slate-700")}>
                  {row.whatsapp_reachable === true ? "WhatsApp 可达" : row.whatsapp_reachable === false ? "不可达" : "未确认"}
                </span></td>
                <td className="whitespace-nowrap p-3 text-slate-700">{syncLabel[row.sync_status]}</td>
              </tr>)}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
