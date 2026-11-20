"use client";

import { useMemo, useState } from "react";
import { Globe2, Link2, Search, ShieldAlert, Users } from "lucide-react";
import { BentoStatCard } from "@/components/ui/bento-stat-card";
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
      <div role="group" aria-label="社媒关键指标" className="grid grid-cols-2 gap-2.5 lg:grid-cols-4">
        {kpis.map(({ label, value, icon: Icon }) => (
          <BentoStatCard
            key={label}
            label={label}
            value={value}
            icon={Icon}
            accent={label === "授权异常" ? "rose" : label === "粉丝总数" ? "violet" : label === "已授权" ? "emerald" : "blue"}
          />
        ))}
      </div>
      <div className={panel}>
        <header className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="text-[15px] font-bold text-slate-900">社媒平台矩阵</h2>
            <p className="mt-0.5 text-[12.5px] leading-6 text-slate-600">
              按国内外市场筛选平台玩法，并管理授权状态。
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <div
              role="group"
              aria-label="区域筛选"
              className="inline-flex rounded-xl border border-slate-200/90 bg-slate-100/90 p-1"
            >
              {filters.map(({ value, label }) => (
                <button
                  key={value}
                  type="button"
                  aria-pressed={region === value}
                  onClick={() => setRegion(value)}
                  className="rounded-lg px-3 py-1.5 text-[12.5px] font-semibold text-slate-600 transition-colors hover:bg-white/70 hover:text-slate-900 aria-pressed:bg-slate-900 aria-pressed:text-white aria-pressed:shadow-xs"
                >
                  {label}
                </button>
              ))}
            </div>
            <label className="relative">
              <Search className="pointer-events-none absolute top-1/2 left-2.5 h-3.5 w-3.5 -translate-y-1/2 text-slate-400" />
              <input
                type="search"
                aria-label="搜索平台"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="搜索平台 / 玩法"
                className="w-48 rounded-xl border border-slate-200 bg-slate-50/80 py-1.5 pr-2.5 pl-8 text-[12.5px] text-slate-800 placeholder:text-slate-400 focus:border-blue-500 focus:bg-white focus:outline-none focus:ring-[3px] focus:ring-blue-500/12"
              />
            </label>
          </div>
        </header>
        {!visible.length ? <p className="rounded-xl border border-dashed border-slate-300 bg-slate-50/70 p-4 text-[12.5px] font-medium text-slate-600">没有匹配的平台。</p> : (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {visible.map((playbook) => {
              const account = accountByPlatform.get(playbook.platform);
              const busy = pendingPlatforms.includes(playbook.platform);
              return (
                <article
                  key={playbook.platform}
                  aria-label={playbook.label}
                  className="flex min-w-0 flex-col rounded-2xl border border-slate-200 bg-white p-4 shadow-[0_1px_2px_rgba(15,23,42,0.04)] transition-all duration-200 hover:-translate-y-0.5 hover:border-slate-300 hover:shadow-[0_6px_16px_-6px_rgba(15,23,42,0.2)]"
                >
                  <div className="flex items-start justify-between gap-2">
                    <h3 className="flex min-w-0 items-center gap-2 text-[13.5px] font-bold text-slate-900">
                      <span aria-hidden="true" className="grid h-7 w-7 shrink-0 place-items-center rounded-lg bg-slate-50 text-[13px] ring-1 ring-slate-200">
                        {playbook.emoji}
                      </span>
                      <span className="break-words">{playbook.label}</span>
                    </h3>
                    <span className="shrink-0 rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-[10.5px] font-semibold text-slate-700">
                      {regionLabel[playbook.region as SocialRegion]}
                    </span>
                  </div>
                  <p className="mt-2 text-[11.5px] leading-relaxed text-slate-600">{playbook.audience}</p>
                  <ul className="mt-2 flex flex-wrap gap-1">
                    {playbook.acquisition_plays.slice(0, 3).map((play) => (
                      <li key={play} className="rounded-md border border-blue-100 bg-blue-50 px-1.5 py-0.5 text-[10.5px] font-semibold text-blue-800">
                        {play}
                      </li>
                    ))}
                  </ul>
                  <p className="mt-3 flex flex-wrap items-center gap-1.5 text-[11px]">
                    <span className={"rounded-full border px-2 py-0.5 font-semibold " + (playbook.publish_mode === "api" ? "border-emerald-200 bg-emerald-50 text-emerald-800" : "border-amber-200 bg-amber-50 text-amber-800")}>{publishModeLabel[playbook.publish_mode]}</span>
                    <span className={"inline-flex items-center gap-1 rounded-full border px-2 py-0.5 font-semibold " + (account?.status === "connected" ? "border-emerald-200 bg-emerald-50 text-emerald-800" : account?.status === "token_expired" ? "border-rose-200 bg-rose-50 text-rose-800" : "border-slate-200 bg-slate-100 text-slate-700")}>
                      <span aria-hidden="true" className={"h-1.5 w-1.5 rounded-full " + (account?.status === "connected" ? "bg-emerald-500" : account?.status === "token_expired" ? "bg-rose-500" : "bg-slate-400")} />
                      {accountStatusLabel[account?.status ?? "disconnected"]}
                    </span>
                  </p>
                  <p className="mt-3 min-h-10 break-words text-[11.5px] text-slate-600">{account ? account.display_name + " · " + account.handle + " · " + count(account.followers_count) + " 粉丝" : "尚未绑定账号"}</p>
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
