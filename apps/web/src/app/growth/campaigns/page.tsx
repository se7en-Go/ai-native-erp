"use client";

import { useI18n } from "@/lib/i18n";

import React, { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, MailCheck, Mails, Megaphone, Play, ShieldCheck } from "lucide-react";

import { AppShell } from "@/components/app-shell";
import { Page, PageHeader } from "@/components/ui/page";
import { CampaignBuilderPanel } from "@/components/growth/campaign-builder-panel";
import { button, count, field, panel, primary } from "@/components/growth/presentation";
import type { CampaignDraft, MarketingCampaign, SuppressionPreview } from "@/components/growth/types";
import {
  type EmailSenderAccount,
  type SendRealCampaignResponse,
  emailEngineLabel,
  fetchEmailSenderAccounts,
  sendCampaignReal,
} from "@/lib/api-client/pack-c-growth";
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

const emeraldPrimary =
  "inline-flex items-center justify-center gap-2 rounded-xl bg-emerald-600 px-3.5 py-2 text-[13px] font-semibold text-white shadow-[0_2px_8px_-2px_rgba(5,150,105,0.55)] transition-colors hover:bg-emerald-700 focus-visible:outline-2 focus-visible:outline-emerald-700 focus-visible:outline-offset-2 disabled:shadow-none disabled:opacity-50";
const emeraldSoft =
  "inline-flex items-center justify-center gap-2 rounded-xl border border-emerald-200 bg-emerald-50 px-3 py-2 text-[13px] font-semibold text-emerald-800 transition-colors hover:border-emerald-300 hover:bg-emerald-100 focus-visible:outline-2 focus-visible:outline-emerald-700 disabled:opacity-50";

/** Real email delivery (Resend / SMTP) for one queued email campaign. */
function RealSendPanel({
  campaign,
  mailboxes,
  mailboxesLoading,
  selectedMailbox,
  onSelectMailbox,
  result,
  pending,
  pendingKind,
  onSend,
}: {
  campaign: MarketingCampaign;
  mailboxes: readonly EmailSenderAccount[];
  mailboxesLoading: boolean;
  selectedMailbox: string;
  onSelectMailbox: (mailboxId: string) => void;
  result: SendRealCampaignResponse | null;
  pending: boolean;
  pendingKind: "dry_run" | "real" | null;
  onSend: (kind: "dry_run" | "real") => void;
}) {
  const { bi } = useI18n();
  const busy = pending;

  return (
    <section
      aria-label={bi(`真实邮件发信 ${campaign.name}`, `Real email delivery ${campaign.name}`)}
      data-testid="campaign-real-send-panel"
      data-campaign-id={campaign.id}
      className="mt-3 rounded-xl border border-emerald-200/80 bg-emerald-50/50 p-3"
    >
      <header className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <h4 className="flex items-center gap-2 text-[13px] font-bold text-emerald-950">
            <Mails aria-hidden="true" size={15} />
            {bi("真实邮件发信（Resend / SMTP）", "Real email delivery (Resend / SMTP)")}
          </h4>
          <p className="mt-1 text-[12px] leading-5 text-emerald-900/85">
            {bi(
              "用你自己的发件邮箱真实投递：逐封复核退订/抑制名单与地址有效性，随信带 RFC 8058 一键退订头。试算只看数字，不发信、不落库。",
              "Delivered through your own mailbox: every recipient is re-checked against suppression and address validity, and each message carries the RFC 8058 unsubscribe header. A dry run only reports numbers — nothing is sent or persisted."
            )}
          </p>
        </div>
        {result ? (
          <span className="rounded-full border border-emerald-300 bg-white px-2 py-0.5 text-[10.5px] font-semibold text-emerald-800">
            {result.dry_run ? bi("试算结果", "Dry-run result") : bi("真实发送结果", "Live delivery result")}
          </span>
        ) : null}
      </header>

      <div className="mt-3 flex flex-wrap items-end gap-2">
        <label htmlFor={`mailbox-${campaign.id}`} className="block text-[12px] font-semibold text-emerald-950">
          {bi("发件邮箱", "Sending mailbox")}
          <select
            id={`mailbox-${campaign.id}`}
            data-testid="campaign-mailbox-select"
            value={selectedMailbox}
            disabled={busy}
            onChange={(event) => onSelectMailbox(event.target.value)}
            className={field}
          >
            <option value="">{bi("自动选择默认发件邮箱", "Auto-select the default mailbox")}</option>
            {mailboxes.map((mailbox) => (
              <option key={mailbox.id} value={mailbox.id}>
                {mailbox.label}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          data-testid="campaign-dry-run-btn"
          className={emeraldSoft}
          disabled={busy}
          onClick={() => onSend("dry_run")}
        >
          {pendingKind === "dry_run" ? (
            <LoaderCircle aria-hidden="true" size={15} className="animate-spin" />
          ) : (
            <MailCheck aria-hidden="true" size={15} />
          )}
          {bi("试算预览（Dry Run）", "Dry run preview")}
        </button>
        <button
          type="button"
          data-testid="campaign-real-send-btn"
          className={emeraldPrimary}
          disabled={busy}
          onClick={() => onSend("real")}
        >
          {pendingKind === "real" ? (
            <LoaderCircle aria-hidden="true" size={15} className="animate-spin" />
          ) : (
            <Mails aria-hidden="true" size={15} />
          )}
          {bi("真实合规发送", "Send for real")}
        </button>
      </div>

      {!mailboxesLoading && !mailboxes.length ? (
        <p className="mt-2 rounded-lg border border-amber-200 bg-amber-50 px-2.5 py-1.5 text-[11.5px] font-medium text-amber-900">
          {bi(
            "尚未连接可用的邮箱账号。请先在渠道中心连接 Resend 或 SMTP 邮箱。",
            "No active mailbox connected yet. Connect a Resend or SMTP mailbox in the channel centre first."
          )}
        </p>
      ) : null}

      {result ? (
        <div
          data-testid="campaign-real-send-result"
          className="mt-3 rounded-xl border border-emerald-200 bg-white/85 p-3"
        >
          <p className="text-[13px] font-bold tabular-nums text-emerald-950">
            已发送 {count(result.sent_count)} · 退订拦截 {count(result.suppressed_count)} · 失败{" "}
            {count(result.failed_count)}
          </p>
          <dl className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-[11.5px] font-medium text-emerald-900/85">
            <div className="flex gap-1">
              <dt>发件邮箱</dt>
              <dd className="break-all font-semibold text-emerald-950">{result.from_email}</dd>
            </div>
            <div className="flex gap-1">
              <dt>引擎</dt>
              <dd className="font-semibold text-emerald-950">{emailEngineLabel(result.provider)}</dd>
            </div>
          </dl>
        </div>
      ) : null}
    </section>
  );
}

export default function GrowthCampaignsPage() {
  const { bi } = useI18n();
  const queryClient = useQueryClient();
  const [previews, setPreviews] = useState<Record<string, SuppressionPreview>>({});
  const [feedback, setFeedback] = useState<{ error: boolean; text: string } | null>(null);
  const [mailboxByCampaign, setMailboxByCampaign] = useState<Record<string, string>>({});
  const [sendResults, setSendResults] = useState<Record<string, SendRealCampaignResponse>>({});

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
  const mailboxesQuery = useQuery({
    queryKey: ["growth", "email-mailboxes"],
    queryFn: fetchEmailSenderAccounts,
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

  const sendReal = useMutation({
    mutationFn: async ({
      campaign,
      kind,
    }: {
      campaign: MarketingCampaign;
      kind: "dry_run" | "real";
    }) =>
      await sendCampaignReal(campaign.id, {
        channel_account_id: mailboxByCampaign[campaign.id] || null,
        dry_run: kind === "dry_run",
      }),
    onSuccess: (result) => {
      setSendResults((prev) => ({ ...prev, [result.campaign_id]: result }));
      setFeedback({
        error: false,
        text: result.dry_run
          ? `试算完成：将发送 ${result.sent_count} 封，退订拦截 ${result.suppressed_count} 封，失败 ${result.failed_count} 封（发件邮箱 ${result.from_email}）。`
          : `真实发信完成：已发送 ${result.sent_count} 封，退订拦截 ${result.suppressed_count} 封，失败 ${result.failed_count} 封（发件邮箱 ${result.from_email}）。`,
      });
      void queryClient.invalidateQueries({ queryKey: ["growth", "campaigns"] });
    },
    onError: () => {
      setFeedback({
        error: true,
        text: "真实发信失败：请确认邮箱账号凭据、渠道归属与战役收件人状态后重试。",
      });
    },
  });

  const campaigns = campaignsQuery.data ?? [];
  const prospects = useMemo(() => prospectsQuery.data ?? [], [prospectsQuery.data]);
  const targetIds = useMemo(() => campaignTargetIds(prospects), [prospects]);
  const mailboxes = mailboxesQuery.data ?? [];
  const busy = save.isPending || preview.isPending || launch.isPending || sendReal.isPending;
  const error = campaignsQuery.error ?? prospectsQuery.error;

  return (
    <AppShell
      currentPath="/growth/campaigns"
      pendingProposalsCount={proposalsQuery.data?.total ?? 0}
    >
      <Page full className="space-y-5">
        <PageHeader
          title={bi("合规营销战役", "Compliant campaigns")}
          description={bi(
            "邮件战役强制应用全局退订拦截并随信发送 RFC 8058 List-Unsubscribe，可直接用真实发件邮箱（Resend / SMTP）投递；WhatsApp 战役要求有效 Opt-In、拦截美国 +1 营销号码，并由双令牌桶限速。预检结果只是快照，启动时服务端会重新校验。",
            "Email campaigns always apply global suppression plus RFC 8058 List-Unsubscribe and can be delivered through your own real mailbox (Resend / SMTP); WhatsApp campaigns require valid opt-in, block US +1 marketing numbers and are rate-limited by dual token buckets. Previews are snapshots; the server re-validates on launch."
          )}
          badge={
            <span className="inline-flex items-center gap-1.5 rounded-full border border-blue-200 bg-blue-50 px-2.5 py-1 text-[11px] font-semibold text-blue-900">
              <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" />
              {bi("发送前复核 · 零延迟 STOP 抑制", "Re-check before send · zero-delay STOP suppression")}
            </span>
          }
        />

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
          <header className="mb-4 flex flex-wrap items-center justify-between gap-2">
            <h2 className="flex items-center gap-2 text-[15px] font-bold text-slate-900">
              <span className="grid h-7 w-7 place-items-center rounded-lg bg-amber-50 text-amber-600 ring-1 ring-amber-100">
                <Megaphone aria-hidden="true" size={15} />
              </span>
              战役列表与合规预检
            </h2>
            <span className="rounded-full border border-emerald-200 bg-emerald-50 px-2.5 py-0.5 text-[11.5px] font-semibold tabular-nums text-emerald-800">
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
                  <li
                  key={campaign.id}
                  className="rounded-2xl border border-slate-200 bg-white p-4 shadow-[0_1px_2px_rgba(15,23,42,0.04)] transition-shadow hover:shadow-[0_6px_16px_-6px_rgba(15,23,42,0.2)]"
                >
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div className="min-w-0">
                        <h3 className="break-words text-[14px] font-bold text-slate-900">
                          {campaign.name}
                        </h3>
                        <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[10.5px] font-semibold">
                          <span className="rounded-md border border-slate-200 bg-slate-100 px-1.5 py-0.5 text-slate-700">
                            {campaign.channel_type === "email" ? "邮件" : "WhatsApp Cloud"}
                          </span>
                          <span className="rounded-full border border-blue-200 bg-blue-50 px-2 py-0.5 text-blue-800">
                            {STATUS_LABEL[campaign.status] ?? campaign.status}
                          </span>
                          <span className="font-medium text-slate-600">
                            目标 {count(campaign.total_targets)} 条 · 已发送{" "}
                            {count(campaign.sent_count)} · 拦截 {count(campaign.suppressed_count)}
                          </span>
                        </div>
                        {campaign.subject ? (
                          <p className="mt-1.5 text-[13px] text-slate-700">主题：{campaign.subject}</p>
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
                        className="mt-3 rounded-xl border border-blue-200/80 bg-blue-50/60 p-3"
                      >
                        <dl className="grid grid-cols-2 gap-2 text-sm">
                          <div className="rounded-xl bg-white/80 px-3 py-2 ring-1 ring-blue-100">
                            <dt className="text-[11.5px] font-medium text-slate-600">可发送</dt>
                            <dd className="text-[20px] font-bold leading-tight tabular-nums text-blue-900">
                              {count(snapshot.eligible_count)}
                            </dd>
                          </div>
                          <div className="rounded-xl bg-white/80 px-3 py-2 ring-1 ring-rose-100">
                            <dt className="text-[11.5px] font-medium text-slate-600">本次拦截</dt>
                            <dd className="text-[20px] font-bold leading-tight tabular-nums text-rose-800">
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

                    {campaign.channel_type === "email" ? (
                      <RealSendPanel
                        campaign={campaign}
                        mailboxes={mailboxes}
                        mailboxesLoading={mailboxesQuery.isPending}
                        selectedMailbox={mailboxByCampaign[campaign.id] ?? ""}
                        onSelectMailbox={(mailboxId) =>
                          setMailboxByCampaign((prev) => ({ ...prev, [campaign.id]: mailboxId }))
                        }
                        result={sendResults[campaign.id] ?? null}
                        pending={
                          sendReal.isPending &&
                          sendReal.variables?.campaign.id === campaign.id
                        }
                        pendingKind={
                          sendReal.isPending && sendReal.variables?.campaign.id === campaign.id
                            ? sendReal.variables.kind
                            : null
                        }
                        onSend={(kind) => sendReal.mutate({ campaign, kind })}
                      />
                    ) : null}
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      </Page>
    </AppShell>
  );
}
