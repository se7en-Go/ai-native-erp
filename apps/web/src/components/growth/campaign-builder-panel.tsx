"use client";

import { useState } from "react";
import { Save, ShieldCheck } from "lucide-react";
import { button, count, field, panel, primary } from "./presentation";
import type { CampaignChannel, CampaignDraft, SuppressionPreview } from "./types";

export function CampaignBuilderPanel({ targetIds, onPreview, onSaveDraft }: {
  targetIds: readonly string[];
  onPreview: (draft: CampaignDraft) => Promise<SuppressionPreview>;
  onSaveDraft: (draft: CampaignDraft) => void | Promise<void>;
}) {
  const [channel, setChannel] = useState<CampaignChannel>("email");
  const [name, setName] = useState("");
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [result, setResult] = useState<{ data: SuppressionPreview; key: string } | null>(null);
  const [pending, setPending] = useState<"preview" | "save" | null>(null);
  const [feedback, setFeedback] = useState<{ error: boolean; text: string } | null>(null);
  const email = channel === "email";
  const uniqueTargets = [...new Set(targetIds)];
  const draft: CampaignDraft = { name: name.trim(), channel_type: channel, subject: email ? subject.trim() : null, body_template: body.trim(), target_ids: uniqueTargets };
  const ready = !!(draft.name && draft.body_template && (!email || draft.subject));
  const key = JSON.stringify(draft);
  const preview = result?.key === key ? result.data : null;

  function clearResult() { setResult(null); setFeedback(null); }

  async function run(action: "preview" | "save") {
    if (pending || !ready || (action === "preview" && !uniqueTargets.length)) return;
    setPending(action);
    setFeedback(null);
    if (action === "preview") setResult(null);
    try {
      if (action === "preview") setResult({ data: await onPreview(draft), key });
      else { await onSaveDraft(draft); setFeedback({ error: false, text: "战役草稿已保存。" }); }
    } catch {
      setFeedback({ error: true, text: action === "preview" ? "无法获取退订拦截预览，请重试。" : "保存失败，请重试。" });
    } finally {
      setPending(null);
    }
  }

  return (
    <section aria-label="营销战役配置" className={panel}>
      <h2 className="text-lg font-bold">营销战役配置</h2>
      <p className="mb-4 text-sm text-slate-600">预览仅反映当前检查结果；正式发送前由服务端重新校验。</p>
      <div className="grid gap-5 lg:grid-cols-2">
        <form onSubmit={(event) => { event.preventDefault(); void run("save"); }}>
          <fieldset disabled={!!pending} className="space-y-3 disabled:opacity-65">
            <label className="block text-sm font-semibold">战役渠道
              <select value={channel} onChange={(event) => { clearResult(); setChannel(event.target.value as CampaignChannel); }} className={field}>
                <option value="email">邮件</option><option value="whatsapp_cloud">WhatsApp Cloud</option>
              </select>
            </label>
            <label className="block text-sm font-semibold">战役名称
              <input required value={name} onChange={(event) => { clearResult(); setName(event.target.value); }} className={field} />
            </label>
            {email && <label className="block text-sm font-semibold">邮件主题
              <input required value={subject} onChange={(event) => { clearResult(); setSubject(event.target.value); }} className={field} />
            </label>}
            <label className="block text-sm font-semibold">消息内容
              <textarea required rows={5} value={body} onChange={(event) => { clearResult(); setBody(event.target.value); }} className={field + " resize-y"} />
            </label>
            <div className="flex flex-wrap gap-2">
              <button type="button" disabled={!ready || !uniqueTargets.length || !!pending} className={button} onClick={() => void run("preview")}><ShieldCheck aria-hidden="true" size={16} />{pending === "preview" ? "检查中…" : "预览退订拦截"}</button>
              <button type="submit" disabled={!ready || !!pending} className={primary}><Save aria-hidden="true" size={16} />保存草稿</button>
            </div>
          </fieldset>
        </form>
        <aside className="rounded-xl border border-teal-100 bg-teal-50/60 p-4">
          <h3 className="flex items-center gap-2 font-bold text-teal-900"><ShieldCheck aria-hidden="true" size={18} />合规检查</h3>
          <p className="mt-2 text-sm text-slate-700">{email
            ? "服务端必须应用全局退订拦截，并随营销邮件发送 RFC 8058 List-Unsubscribe 一键退订头。"
            : "须有有效的 WhatsApp Opt-In；STOP 退订、美国营销限制与频率限制由发送网关强制执行。"}</p>
          <p className="mt-3 text-sm font-semibold">已选 {count(uniqueTargets.length)} 个目标</p>
          {preview ? <section aria-label="拦截结果" aria-live="polite" className="mt-3 rounded-lg border border-teal-100 bg-white p-3">
            <dl className="grid grid-cols-2 gap-2 text-sm">
              <div><dt>预览可触达</dt><dd className="text-lg font-bold text-teal-900">{count(preview.eligible_count)}</dd></div>
              <div><dt>本次拦截</dt><dd className="text-lg font-bold text-rose-800">{count(preview.suppressed_count)}</dd></div>
            </dl>
            {!!preview.reasons.length && <ul className="mt-2 space-y-1 border-t border-slate-100 pt-2 text-sm">
              {preview.reasons.map(({ reason, count: amount }) => <li key={reason} className="flex justify-between gap-2"><span>{reason}</span><span>{count(amount)} 条</span></li>)}
            </ul>}
          </section> : <p className="mt-3 text-sm text-slate-600">{uniqueTargets.length ? "尚未生成当前配置的拦截结果。" : "选择目标线索后才能预览。"}</p>}
        </aside>
      </div>
      {feedback && <p role={feedback.error ? "alert" : "status"} className={"mt-3 text-sm " + (feedback.error ? "text-rose-700" : "text-teal-900")}>{feedback.text}</p>}
    </section>
  );
}
