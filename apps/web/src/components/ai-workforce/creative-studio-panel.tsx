"use client";

import { useState, type FormEvent } from "react";
import { Copy, Save, Sparkles, WandSparkles } from "lucide-react";
import { button, field } from "./presentation";
import type { CreativeAsset, CreativeAssetRequest, CreativeAssetType, SkuOption } from "./types";

const assetTypes: Record<CreativeAssetType, string> = {
  product_copy: "商品文案", listing: "平台 Listing", email_template: "邮件模板", social_caption: "社媒配文", ad_hook: "广告开场钩子",
};
const defaultLanguages = [
  { code: "en", label: "英语" }, { code: "zh", label: "中文" }, { code: "es", label: "西班牙语" },
  { code: "de", label: "德语" }, { code: "fr", label: "法语" }, { code: "ja", label: "日语" },
];
function SparklesIcon() {
  return <Sparkles size={14} aria-hidden="true" className="text-blue-600" />;
}

const primary = "inline-flex items-center gap-2 rounded-xl bg-blue-600 px-4 py-2 text-[13px] font-semibold text-white shadow-xs transition-colors hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-blue-600 focus-visible:outline-offset-2 disabled:opacity-50";

export function CreativeStudioPanel({ skus, languages = defaultLanguages, onGenerate, onSave, onCopy }: {
  skus: readonly SkuOption[];
  languages?: readonly { code: string; label: string }[];
  onGenerate: (request: CreativeAssetRequest) => Promise<readonly CreativeAsset[]>;
  onSave: (asset: CreativeAsset) => void | Promise<void>;
  onCopy?: (text: string) => void | Promise<void>;
}) {
  const [assetType, setAssetType] = useState<CreativeAssetType>("product_copy");
  const [sku, setSku] = useState("");
  const [chosen, setChosen] = useState(() => languages[0] ? [languages[0].code] : []);
  const [prompt, setPrompt] = useState("");
  const [assets, setAssets] = useState<readonly CreativeAsset[]>([]);
  const [selected, setSelected] = useState(0);
  const [pending, setPending] = useState<"generate" | "save" | "copy" | null>(null);
  const [feedback, setFeedback] = useState<{ error: boolean; text: string } | null>(null);

  const validLanguages = chosen.filter((code) => languages.some((language) => language.code === code));
  const validSku = skus.some((option) => option.sku === sku) ? sku : "";
  const result = assets[selected];
  const languageName = (code: string) => languages.find((item) => item.code === code)?.label ?? code;

  async function generate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending || !validLanguages.length || !prompt.trim()) return;
    setPending("generate");
    setFeedback(null);
    try {
      const next = await onGenerate({ asset_type: assetType, sku_ref: validSku || null, languages: validLanguages, prompt: prompt.trim() });
      setAssets(next);
      setSelected(0);
      if (!next.length) setFeedback({ error: false, text: "未返回可用素材，请调整创作要求后重试。" });
    } catch {
      setFeedback({ error: true, text: "生成失败，请稍后重试。" });
    } finally {
      setPending(null);
    }
  }

  async function act(action: "copy" | "save") {
    if (!result?.content_text.trim() || pending) return;
    setPending(action);
    setFeedback(null);
    try {
      if (action === "save") await onSave(result);
      else if (onCopy) await onCopy(result.content_text);
      else await navigator.clipboard.writeText(result.content_text);
      setFeedback({ error: false, text: action === "save" ? "素材已保存。" : "文案已复制。" });
    } catch {
      setFeedback({ error: true, text: action === "save" ? "保存失败，请重试。" : "复制失败，可从结果框手动复制。" });
    } finally {
      setPending(null);
    }
  }

  return (
    <section
      aria-label="AI 文案与素材工坊"
      className="rounded-xl border border-gray-200 bg-white p-5 text-gray-900 shadow-sm dark:border-gray-800 dark:bg-gray-900 dark:text-gray-100"
    >
      <header className="mb-5 flex flex-wrap items-start justify-between gap-3 border-b border-gray-100 pb-4 dark:border-gray-800">
        <div className="flex min-w-0 items-start gap-3">
          <span className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-gradient-to-br from-blue-600 to-indigo-600 text-white shadow-sm">
            <WandSparkles size={18} aria-hidden="true" />
          </span>
          <div className="min-w-0">
            <h2 className="text-[18px] font-bold tracking-tight">AI 文案与素材工坊</h2>
            <p className="mt-0.5 text-[12.5px] text-slate-600">从商品目录选择 SKU，为不同市场生成可编辑的文案草稿。</p>
          </div>
        </div>
        <span className="inline-flex shrink-0 items-center gap-1.5 rounded-full border border-blue-200/80 bg-blue-50 px-2.5 py-1 text-[11.5px] font-semibold text-blue-700">
          创意工作台 · 草稿永远人工批准
        </span>
      </header>
      <div className="grid gap-5 lg:grid-cols-2">
        <form onSubmit={generate}>
          <fieldset disabled={!!pending} className="space-y-4 disabled:opacity-65">
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="block text-[12.5px] font-semibold text-slate-800">素材类型
                <select value={assetType} onChange={(event) => setAssetType(event.target.value as CreativeAssetType)} className={field + " mt-1"}>
                  {Object.entries(assetTypes).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                </select>
              </label>
              <label className="block text-[12.5px] font-semibold text-slate-800">关联 SKU
                <select value={validSku} onChange={(event) => setSku(event.target.value)} className={field + " mt-1"}>
                  <option value="">不关联</option>
                  {skus.map(({ sku, label }) => <option key={sku} value={sku}>{sku} · {label}</option>)}
                </select>
              </label>
            </div>
            <fieldset className="flex flex-wrap gap-2">
              <legend className="mb-1.5 text-[12.5px] font-semibold text-slate-800">输出语言</legend>
              {languages.map(({ code, label }) => {
                const on = validLanguages.includes(code);
                return (
                  <label
                    key={code}
                    className={"inline-flex cursor-pointer items-center gap-1.5 rounded-full border px-3 py-1.5 text-[12.5px] font-medium transition-colors " + (on ? "border-blue-300 bg-blue-50 text-blue-800" : "border-slate-200 bg-white text-slate-700 hover:border-slate-300 hover:bg-slate-50")}
                  >
                    <input type="checkbox" checked={on} className="accent-blue-600" onChange={(event) => setChosen((prev) => event.target.checked ? [...prev, code] : prev.filter((item) => item !== code))} />{label}
                  </label>
                );
              })}
            </fieldset>
            <label className="block text-[12.5px] font-semibold text-slate-800">创作要求
              <textarea required rows={4} value={prompt} onChange={(event) => setPrompt(event.target.value)} className={field + " mt-1 resize-y"} placeholder="例如：为德国经销商介绍应用场景和已验证的产品规格" />
            </label>
            <button type="submit" disabled={!prompt.trim() || !validLanguages.length || !!pending} className={primary}><WandSparkles size={16} aria-hidden="true" />{pending === "generate" ? "正在生成…" : "生成文案"}</button>
          </fieldset>
        </form>
        <section
          aria-label="素材预览"
          className="min-w-0 rounded-2xl border border-blue-100 bg-slate-50/80 p-4"
          aria-busy={!!pending}
        >
          <h3 className="mb-3 flex items-center gap-1.5 text-[13px] font-bold text-slate-900">
            <SparklesIcon /> 素材预览
          </h3>
          {result ? <>
            <div role="group" aria-label="生成版本" className="mb-3 flex flex-wrap gap-2">
              {assets.map((asset, index) => <button key={asset.id ?? asset.language + ":" + index} type="button" aria-pressed={index === selected} disabled={!!pending}
                onClick={() => { setSelected(index); setFeedback(null); }} className={button + " aria-pressed:border-blue-600 aria-pressed:bg-blue-50"}>{languageName(asset.language)}</button>)}
            </div>
            <p className="mb-2 break-words text-sm font-semibold">{result.title}</p>
            <textarea aria-label="生成结果" value={result.content_text} disabled={!!pending} rows={8} className={field + " resize-y"} onChange={(event) => {
              const text = event.target.value;
              setAssets((prev) => prev.map((asset, index) => index === selected ? { ...asset, content_text: text } : asset));
              setFeedback(null);
            }} />
            <div className="mt-2 flex flex-wrap gap-2">
              <button type="button" className={button + " inline-flex items-center gap-2"} disabled={!result.content_text.trim() || !!pending} onClick={() => void act("copy")}><Copy size={15} aria-hidden="true" />复制</button>
              <button type="button" className={primary} disabled={!result.content_text.trim() || !!pending} onClick={() => void act("save")}><Save size={15} aria-hidden="true" />保存素材</button>
            </div>
          </> : <p className="rounded-xl border border-dashed border-slate-300 bg-white/70 p-4 text-center text-[12.5px] font-medium text-slate-500">选择类型和语言后，生成的文案会显示在这里。</p>}
        </section>
      </div>
      {feedback && <p role={feedback.error ? "alert" : "status"} className={"mt-4 rounded-xl border px-3 py-2 text-[12.5px] font-medium " + (feedback.error ? "border-rose-200 bg-rose-50 text-rose-700" : "border-blue-200 bg-blue-50 text-blue-700")}>{feedback.text}</p>}
    </section>
  );
}
