"use client";

import { useI18n } from "@/lib/i18n";

import React, { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Megaphone, Play, ShieldCheck } from "lucide-react";

import { AppShell } from "@/components/app-shell";
import { CampaignBuilderPanel } from "@/components/growth/campaign-builder-panel";
import { button, count, panel, primary } from "@/components/growth/presentation";
import type { CampaignDraft, MarketingCampaign, SuppressionPreview } from "@/components/growth/types";
import { fetchPendingProposalsCount } from "@/lib/data-source";
import {
  campaignTargetIds,
  fetchGrowthProspects,
  fetchMarketingCampaigns,
  launchMarketingCampaign,
  previewMarketingCampaign,
  saveCampaignDraft,
} from "@/lib/phase3c-data";

const STATUS_LABEL: Record<string, string> = {
  draft: "草稿",
  scheduled: "已排期",
  running: "发送中",
  paused: "已暂停",
  completed: "已完成",
};

export default function GrowthCampaignsPage() {
  const { bi } = useI18n();
  const queryClient = useQueryClient();
  const [previews, setPreviews] = useState<Record<string, SuppressionPreview>>({});
  const [feedback, setFeedback] = useState<{ error: boolean; text: string } | null>(null);

  const proposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () => fetchPendingProposalsCount(),
  });
  const campaignsQuery = useQuery({
    queryKey: ["growth", "campaigns"],
    queryFn: fetchMarketingCampaigns,
  });
  const prospectsQuery = useQuery({
    queryKey: ["growth", "prospects"],
    queryFn: () => fetchGrowthProspects(),
  });

  const save = useMutation({
    mutationFn: (draft: CampaignDraft) => saveCampaignDraft(draft),
    onSuccess: (campaign) => {
      setFeedback({ error: false, text: `战役「${campaign.name}」已保存为草稿。` });
      void queryClient.invalidateQueries({ queryKey: ["growth", "campaigns"] });
    },
    onError: () => setFeedback({ error: true, text: "保存草稿失败，请重试。" }),
  });

  const preview = useMutation({
    mutationFn: async (campaign: MarketingCampaign) => ({
      campaign,
      data: await previewMarketingCampaign(campaign.id),
    }),
    onSuccess: ({ campaign, data }) => {
      setPreviews((prev) => ({ ...prev, [campaign.id]: data }));
      setFeedback({ error: false, text: `已刷新「${campaign.name}」的服务端合规预检。` });
    },
    onError: () => setFeedback({ error: true, text: "无法获取合规预检结果，请重试。" }),
  });

  const launch = useMutation({
    mutationFn: (campaign: MarketingCampaign) => launchMarketingCampaign(campaign.id),
    onSuccess: (result) => {
      setFeedback({
        error: false,
        text: `发送完成：${result.sent} 条已排队发送，${result.suppressed} 条被合规拦截。`,
      });
      void queryClient.invalidateQueries({ queryKey: ["growth", "campaigns"] });
    },
    onError: () => setFeedback({ error: true, text: "启动战役失败：战役可能已完成或状态不允许发送。" }),
  });

  const campaigns = campaignsQuery.data ?? [];
  const prospects = useMemo(() => prospectsQuery.data ?? [], [prospectsQuery.data]);
  const targetIds = useMemo(() => campaignTargetIds(prospects), [prospects]);
  const busy = save.isPending || preview.isPending || launch.isPending;
  const error = campaignsQuery.error ?? prospectsQuery.error;

  return (
    <AppShell
      currentPath="/growth/campaigns"
      pendingProposalsCount={proposalsQuery.data?.total ?? 0}
    >
      <div className="erp-page-full space-y-5">
        <header className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="text-xl font-semibold tracking-tight text-slate-900">{bi("合规营销战役", "Compliant campaigns")}</h1>
            <p className="text-xs text-slate-500">
              {bi("邮件战役强制应用全局退订拦截并随信发送 RFC 8058 List-Unsubscribe；WhatsApp 战役要求有效 Opt-In、拦截美国 +1 营销号码，并由双令牌桶限速。预检结果只是快照，启动时服务端会重新校验。", "Email campaigns always apply global suppression plus RFC 8058 List-Unsubscribe; WhatsApp campaigns require valid opt-in, block US +1 marketing numbers and are rate-limited by dual token buckets. Previews are snapshots; the server re-validates on launch.")}
            </p>
          </div>
          <span className="inline-flex items-center gap-1.5 rounded-full border border-blue-200 bg-blue-50 px-2.5 py-1 text-[11px] font-semibold text-blue-900">
            <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" />
            {bi("发送前复核 · 零延迟 STOP 抑制", "Re-check before send · zero-delay STOP suppression")}
          </span>
        </header>

        {error ? (
          <p role="alert" className="rounded-xl border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800">
            {bi("战役数据加载失败，请稍后重试。", "Failed to load campaign data. Please retry.")}
          </p>
        ) : null}
        {feedback ? (
          <p
            role={feedback.error ? "alert" : "status"}
            className={
              "rounded-xl border p-3 text-sm " +
              (feedback.error
                ? "border-rose-200 bg-rose-50 text-rose-800"
                : "border-blue-200 bg-blue-50 text-blue-900")
            }
          >
            {feedback.text}
          </p>
        ) : null}

        <CampaignBuilderPanel
          targetIds={targetIds}
          onSaveDraft={(draft) => save.mutateAsync(draft).then(() => undefined)}
        />

        <section aria-label="营销战役列表" className={panel}>
          <header className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <h2 className="flex items-center gap-2 text-lg font-bold">
              <Megaphone aria-hidden="true" size={18} />
              战役列表与合规预检
            </h2>
            <span className="text-sm text-slate-600">
              {bi("已清洗可触达线索", "Cleaned reachable leads")} {count(targetIds.length)}
            </span>
          </header>
          {!campaigns.length ? (
            <p className="rounded-xl border border-dashed border-slate-300 p-4 text-sm text-slate-600">
              {bi("还没有营销战役。配置渠道与内容后保存草稿，再执行预检与启动。", "No campaigns yet. Configure channel and content, save a draft, then preview and launch.")}
            </p>
          ) : (
            <ul className="space-y-3">
              {campaigns.map((campaign) => {
                const snapshot = previews[campaign.id];
                return (
                  <li key={campaign.id} className="rounded-xl border border-slate-200 p-4">
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div className="min-w-0">
                        <h3 className="break-words font-bold text-slate-900">{campaign.name}</h3>
                        <p className="text-xs text-slate-600">
                          {campaign.channel_type === "email" ? "邮件" : "WhatsApp Cloud"} ·{" "}
                          {STATUS_LABEL[campaign.status] ?? campaign.status} · 目标{" "}
                          {count(campaign.total_targets)} 条 · 已发送 {count(campaign.sent_count)} ·
                          拦截 {count(campaign.suppressed_count)}
                        </p>
                        {campaign.subject ? (
                          <p className="mt-1 text-sm text-slate-700">主题：{campaign.subject}</p>
                        ) : null}
                      </div>
                      <div className="flex flex-wrap gap-2">
                        <button
                          type="button"
                          className={button}
                          disabled={busy}
                          onClick={() => preview.mutate(campaign)}
                        >
                          <ShieldCheck aria-hidden="true" size={15} />
                          合规预检
                        </button>
                        <button
                          type="button"
                          className={primary}
                          disabled={busy || !["draft", "scheduled", "paused"].includes(campaign.status)}
                          onClick={() => launch.mutate(campaign)}
                        >
                          <Play aria-hidden="true" size={15} />
                          启动战役
                        </button>
                      </div>
                    </div>
                    {snapshot ? (
                      <div
                        aria-label={`战役 ${campaign.name} 合规预检结果`}
                        className="mt-3 rounded-lg border border-blue-100 bg-blue-50/60 p-3"
                      >
                        <dl className="grid grid-cols-2 gap-2 text-sm">
                          <div>
                            <dt>可发送</dt>
                            <dd className="text-lg font-bold text-blue-900">
                              {count(snapshot.eligible_count)}
                            </dd>
                          </div>
                          <div>
                            <dt>本次拦截</dt>
                            <dd className="text-lg font-bold text-rose-800">
                              {count(snapshot.suppressed_count)}
                            </dd>
                          </div>
                        </dl>
                        {snapshot.reasons.length ? (
                          <ul className="mt-2 space-y-1 border-t border-blue-100 pt-2 text-sm">
                            {snapshot.reasons.map((row) => (
                              <li key={row.reason} className="flex justify-between gap-2">
                                <span>{row.reason}</span>
                                <span>{count(row.count)} 条</span>
                              </li>
                            ))}
                          </ul>
                        ) : (
                          <p className="mt-2 text-sm text-slate-700">没有线索被合规规则拦截。</p>
                        )}
                      </div>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      </div>
    </AppShell>
  );
}
