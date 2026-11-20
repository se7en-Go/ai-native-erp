"use client";

import { useMemo, useState } from "react";
import { Globe2, Link2, ShieldAlert, Users } from "lucide-react";
import { PLATFORM_PLAYBOOKS } from "./platform-playbooks";
import { accountStatusLabel, button, count, panel, primary, publishModeLabel, regionLabel } from "./presentation";
import type { PlatformPlaybook, SocialAccount, SocialRegion } from "./types";

const filters = [
  { value: "all", label: "全部" },
  { value: "domestic", label: "国内" },
  { value: "overseas", label: "海外" },
] as const;

export function SocialPlatformMatrix({ accounts, playbooks = PLATFORM_PLAYBOOKS, onConnect, onDisconnect, pendingPlatforms = [] }: {
  accounts: readonly SocialAccount[];
  playbooks?: readonly PlatformPlaybook[];
  onConnect: (platform: PlatformPlaybook["platform"]) => void;
  onDisconnect: (accountId: string) => void;
  pendingPlatforms?: readonly PlatformPlaybook["platform"][];
}) {
  const [region, setRegion] = useState<(typeof filters)[number]["value"]>("all");
  const [query, setQuery] = useState("");
  const accountByPlatform = useMemo(() => {
    const map = new Map<string, SocialAccount>();
    for (const account of accounts) if (!map.has(account.platform)) map.set(account.platform, account);
    return map;
  }, [accounts]);
  const needle = query.trim().toLowerCase();
  const visible = playbooks.filter((playbook) => {
    if (region !== "all" && playbook.region !== region) return false;
    if (!needle) return true;
    const account = accountByPlatform.get(playbook.platform);
    return [playbook.label, playbook.audience, playbook.platform, account?.handle ?? "", ...playbook.acquisition_plays]
      .some((value) => value.toLowerCase().includes(needle));
  });
  const followers = accounts.reduce((total, account) => total + account.followers_count, 0);
  const kpis = [
    { label: "平台总数", value: count(playbooks.length), icon: Globe2 },
    { label: "已授权", value: count(accounts.filter((account) => account.status === "connected").length), icon: Link2 },
    { label: "授权异常", value: count(accounts.filter((account) => account.status === "token_expired").length), icon: ShieldAlert },
    { label: "粉丝总数", value: count(followers), icon: Users },
  ];

  return (
    <section aria-label="社媒平台矩阵" className="space-y-4 text-slate-900">
      <div role="group" aria-label="社媒关键指标" className="grid grid-cols-2 gap-2 lg:grid-cols-4">
        {kpis.map(({ label, value, icon: Icon }) => <div key={label} className="min-w-0 rounded-xl border border-blue-100 bg-blue-50/60 p-3">
          <div className="flex items-center gap-1.5 text-xs text-blue-900"><Icon aria-hidden="true" size={14} />{label}</div>
          <p className="mt-1 break-all text-lg font-bold tabular-nums">{value}</p>
        </div>)}
      </div>
      <div className={panel}>
        <header className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="text-lg font-bold">社媒平台矩阵</h2>
            <p className="text-sm text-slate-600">按国内外市场筛选平台玩法，并管理授权状态。</p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <div role="group" aria-label="区域筛选" className="inline-flex rounded-lg border border-slate-200 p-0.5">
              {filters.map(({ value, label }) => <button key={value} type="button" aria-pressed={region === value} onClick={() => setRegion(value)}
                className={"rounded-md px-3 py-1.5 text-sm font-medium aria-pressed:bg-blue-700 aria-pressed:text-white hover:bg-slate-100"}>{label}</button>)}
            </div>
            <input type="search" aria-label="搜索平台" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索平台 / 玩法" className={button + " w-48"} />
          </div>
        </header>
        {!visible.length ? <p className="rounded-xl border border-dashed border-slate-300 p-4 text-sm text-slate-600">没有匹配的平台。</p> : (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {visible.map((playbook) => {
              const account = accountByPlatform.get(playbook.platform);
              const busy = pendingPlatforms.includes(playbook.platform);
              return (
                <article key={playbook.platform} aria-label={playbook.label} className="flex min-w-0 flex-col rounded-xl border border-slate-200 p-4">
                  <div className="flex items-start justify-between gap-2">
                    <h3 className="flex min-w-0 items-center gap-2 font-bold"><span aria-hidden="true">{playbook.emoji}</span><span className="break-words">{playbook.label}</span></h3>
                    <span className="shrink-0 rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-700">{regionLabel[playbook.region as SocialRegion]}</span>
                  </div>
                  <p className="mt-2 text-xs leading-relaxed text-slate-600">{playbook.audience}</p>
                  <ul className="mt-2 flex flex-wrap gap-1">
                    {playbook.acquisition_plays.slice(0, 3).map((play) => <li key={play} className="rounded bg-blue-50 px-1.5 py-0.5 text-[11px] font-medium text-blue-900">{play}</li>)}
                  </ul>
                  <p className="mt-3 flex flex-wrap items-center gap-1.5 text-xs">
                    <span className={"rounded px-1.5 py-0.5 font-semibold " + (playbook.publish_mode === "api" ? "bg-emerald-100 text-emerald-900" : "bg-amber-100 text-amber-900")}>{publishModeLabel[playbook.publish_mode]}</span>
                    <span className={"rounded px-1.5 py-0.5 font-semibold " + (account?.status === "connected" ? "bg-emerald-100 text-emerald-900" : account?.status === "token_expired" ? "bg-rose-100 text-rose-900" : "bg-slate-100 text-slate-700")}>
                      {accountStatusLabel[account?.status ?? "disconnected"]}
                    </span>
                  </p>
                  <p className="mt-3 min-h-10 break-words text-xs text-slate-600">{account ? account.display_name + " · " + account.handle + " · " + count(account.followers_count) + " 粉丝" : "尚未绑定账号"}</p>
                  <div className="mt-3 flex flex-wrap gap-2 border-t border-slate-100 pt-3">
                    {account && account.status !== "connected" && <button type="button" aria-label={"重新授权 " + playbook.label} disabled={busy} className={primary} onClick={() => onConnect(playbook.platform)}>重新授权</button>}
                    {account && <button type="button" aria-label={"断开 " + playbook.label} disabled={busy} className={button} onClick={() => onDisconnect(account.id)}>断开</button>}
                    {!account && <button type="button" aria-label={"去授权 " + playbook.label} disabled={busy} className={primary} onClick={() => onConnect(playbook.platform)}>去授权</button>}
                  </div>
                </article>
              );
            })}
          </div>
        )}
      </div>
    </section>
  );
}
