"use client";

import React, { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Ban, MessagesSquare, RefreshCw, ShieldCheck, Siren, UserCheck } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { MessagingChannelCard } from "@/components/channels/messaging-channel-cards";
import { BentoStatCard } from "@/components/ui/bento-stat-card";
import { SegmentedTabs } from "@/components/ui/segmented-tabs";
import { Badge, Card, CardHeader, EmptyState, Page, PageHeader } from "@/components/ui/page";
import { replayDlqEvent } from "@/lib/api-client/live-client";
import {
  connectMessagingChannel,
  fetchChannelAccounts,
  fetchChannelConsents,
  fetchChannelDlq,
  fetchChannelSuppressions,
  fetchPendingProposalsCount,
} from "@/lib/data-source";
import { useI18n } from "@/lib/i18n";

const MESSAGING_CHANNELS = ["messenger", "instagram_dm", "telegram"] as const;

export default function ChannelsSettingsPage() {
  const { bi } = useI18n();
  const queryClient = useQueryClient();
  const [logTab, setLogTab] = useState<"consents" | "suppressions">("consents");

  const pendingProposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () => fetchPendingProposalsCount(),
  });

  const accountsQuery = useQuery({
    queryKey: ["channels", "accounts"],
    queryFn: () => fetchChannelAccounts(),
  });

  const dlqQuery = useQuery({
    queryKey: ["channels", "dlq"],
    queryFn: () => fetchChannelDlq(),
  });

  const consentsQuery = useQuery({
    queryKey: ["channels", "consents"],
    queryFn: () => fetchChannelConsents(),
  });

  const suppressionsQuery = useQuery({
    queryKey: ["channels", "suppressions"],
    queryFn: () => fetchChannelSuppressions(),
  });

  const replayMutation = useMutation({
    mutationFn: (dlqId: string) => replayDlqEvent(dlqId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["channels", "dlq"] });
    },
  });

  const connectMutation = useMutation({
    mutationFn: ({
      kind,
      payload,
    }: {
      kind: (typeof MESSAGING_CHANNELS)[number];
      payload: Parameters<typeof connectMessagingChannel>[1];
    }) => connectMessagingChannel(kind, payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["channels", "accounts"] });
    },
  });

  const accounts = accountsQuery.data?.items ?? [];
  const dlqItems = dlqQuery.data?.items ?? [];

  return (
    <AppShell
      currentPath="/settings/channels"
      pendingProposalsCount={pendingProposalsQuery.data?.total ?? 0}
    >
      <Page>
        <PageHeader
          title={bi("渠道接入与合规退订", "Channel connections & compliance suppression")}
          description={bi(
            "绑定 WhatsApp、邮件、Messenger、Instagram 与 Telegram 账号；监控双令牌桶限速、重放死信队列，并审计订阅同意与退订名单。令牌一律信封加密存储，Webhook 按原始报文验签。",
            "Bind WhatsApp, email, Messenger, Instagram and Telegram accounts; monitor dual token buckets, replay the dead-letter queue and audit opt-in/opt-out records. Tokens are envelope-encrypted and webhooks verify the raw body."
          )}
          badge={
            <Badge tone="success">
              <ShieldCheck className="h-3 w-3" />
              {bi("零延迟 STOP 抑制", "Zero-delay STOP suppression")}
            </Badge>
          }
        />

        <section
          aria-label={bi("渠道关键指标", "Channel key metrics")}
          className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"
        >
          <BentoStatCard
            label={bi("已接入账号", "Connected accounts")}
            value={accounts.length}
            icon={MessagesSquare}
            accent="blue"
            hint={bi("WhatsApp / 邮件 / Meta / Telegram", "WhatsApp / email / Meta / Telegram")}
          />
          <BentoStatCard
            label={bi("死信队列事件", "Dead-letter events")}
            value={dlqItems.length}
            icon={Siren}
            accent={dlqItems.length > 0 ? "rose" : "emerald"}
            hint={bi("失败事件不丢，可原样重放", "Failed events are retained and replayable")}
          />
          <BentoStatCard
            label={bi("订阅同意记录", "Consent records")}
            value={consentsQuery.data?.items.length ?? 0}
            icon={UserCheck}
            accent="violet"
            hint={bi("每次 Opt-In 都留存证明来源", "Every opt-in keeps its proof source")}
          />
          <BentoStatCard
            label={bi("退订 / 抑制名单", "Suppression list")}
            value={suppressionsQuery.data?.items.length ?? 0}
            icon={Ban}
            accent={suppressionsQuery.data?.items.length ? "rose" : "slate"}
            hint={bi("零延迟 STOP 抑制", "Zero-delay STOP suppression")}
          />
        </section>

        <Card>
          <CardHeader
            title={bi("Messenger、Instagram 私信与 Telegram 商业版", "Messenger, Instagram DM & Telegram Business")}
            description={bi(
              "绑定 Meta 与 Telegram 消息账号。供应商令牌落库前信封加密，每个 Webhook 都按原始请求体验签。",
              "Bind Meta and Telegram messaging accounts. Provider tokens are envelope-encrypted at rest and every webhook verifies the raw body."
            )}
          />
          <div className="grid gap-3 p-4 md:grid-cols-3 xl:gap-4">
            {MESSAGING_CHANNELS.map((kind) => (
              <MessagingChannelCard
                key={kind}
                kind={kind}
                connected={accounts.some((acct) => acct.channel_type === kind)}
                onSubmit={(payload) => connectMutation.mutateAsync({ kind, payload })}
              />
            ))}
          </div>
        </Card>

        <div className="grid gap-4 xl:grid-cols-2">
          <Card>
            <CardHeader
              title={bi("已接入渠道账号", "Connected channel accounts")}
              actions={
                <Badge tone="info">
                  {accounts.length} {bi("个账号", "accounts")}
                </Badge>
              }
            />
            <div className="space-y-2 p-4">
              {accounts.map((acct) => (
                <div
                  key={acct.id}
                  className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-slate-200 bg-slate-50/60 px-3 py-2.5"
                >
                  <div className="flex min-w-0 flex-wrap items-center gap-2">
                    <span className="grid h-7 w-7 shrink-0 place-items-center rounded-lg bg-white text-blue-600 ring-1 ring-slate-200">
                      <MessagesSquare className="h-3.5 w-3.5" />
                    </span>
                    <span className="truncate text-[13px] font-semibold text-slate-900">
                      {acct.display_name}
                    </span>
                    <Badge>{acct.channel_type}</Badge>
                  </div>
                  <div className="flex items-center gap-1.5">
                    {acct.rate_limit_profile?.mps && (
                      <Badge tone="info">{acct.rate_limit_profile.mps} mps</Badge>
                    )}
                    <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[10.5px] font-semibold text-emerald-800">
                      <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
                      {acct.status}
                    </span>
                  </div>
                </div>
              ))}
              {accounts.length === 0 && (
                <EmptyState>{bi("尚未接入任何渠道账号。", "No channel accounts connected yet.")}</EmptyState>
              )}
            </div>
          </Card>

          <Card>
            <CardHeader
              title={bi("死信队列（DLQ）重放", "Dead-letter queue (DLQ) replay")}
              description={bi(
                "失败事件不丢：修复下游后可原样重放，幂等键保证不重复入账。",
                "Failed events are never dropped: replay verbatim after fixing downstream, with idempotency keys preventing double posting."
              )}
            />
            <div className="space-y-2 p-4">
              {dlqItems.length === 0 ? (
                <EmptyState>
                  {bi("当前没有失败的 DLQ 事件。", "Zero failed DLQ events.")}
                </EmptyState>
              ) : (
                dlqItems.map((ev) => (
                  <div
                    key={ev.id}
                    className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-rose-200 bg-rose-50/60 px-3 py-2.5"
                  >
                    <div className="min-w-0">
                      <div className="text-[13px] font-medium text-slate-900">
                        {ev.channel_type} · {ev.external_event_id}
                      </div>
                      <div className="text-[12px] text-rose-700">
                        {ev.error_message} ({bi("重试", "retries")} {ev.retry_count})
                      </div>
                    </div>
                    <button
                      type="button"
                      data-testid={`dlq-replay-btn-${ev.id}`}
                      onClick={() => replayMutation.mutate(ev.id)}
                      className="erp-btn rounded-xl bg-slate-900 text-white shadow-xs hover:bg-slate-800"
                    >
                      <RefreshCw className="h-3 w-3" />
                      <span>{bi("重放", "Replay")}</span>
                    </button>
                  </div>
                ))
              )}
            </div>
          </Card>
        </div>

        <Card>
          <div className="flex flex-wrap items-center gap-2 border-b border-slate-200/90 bg-slate-50/60 p-3">
            <SegmentedTabs
              ariaLabel={bi("合规台账视图", "Compliance ledger view")}
              value={logTab}
              onChange={setLogTab}
              options={[
                {
                  value: "consents",
                  label: (
                    <span className="inline-flex items-center gap-1.5">
                      <ShieldCheck className="h-3.5 w-3.5" />
                      {bi("订阅同意台账", "Consent ledger")}
                    </span>
                  ),
                  testId: "tab-consent-records",
                },
                {
                  value: "suppressions",
                  label: (
                    <span className="inline-flex items-center gap-1.5">
                      <Ban className="h-3.5 w-3.5" />
                      {bi("退订 / 抑制名单", "Suppression list")}
                    </span>
                  ),
                  testId: "tab-suppression-list",
                },
              ]}
            />
            <span className="ml-auto text-[11px] font-medium text-slate-600">
              {bi("静默时段 21:00–08:00 不发送任何营销消息", "Quiet hours 21:00–08:00 block all marketing sends")}
            </span>
          </div>

          <div className="space-y-1.5 p-4">
            {logTab === "consents"
              ? (consentsQuery.data?.items ?? []).map((c) => (
                  <div
                    key={c.id}
                    className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-slate-200/80 bg-slate-50/60 px-3 py-2 text-[12px]"
                  >
                    <div className="flex flex-wrap items-center gap-1.5">
                      <strong className="font-semibold text-slate-900">{c.customer_name}</strong>
                      <span className="text-slate-400">·</span>
                      <span className="rounded-md bg-slate-100 px-1.5 py-0.5 text-[11px] font-medium text-slate-700">
                        {c.channel_type}
                      </span>
                      <span className="text-slate-400">·</span>
                      <span className="text-slate-600">{c.proof_source}</span>
                    </div>
                    <Badge tone="success">{c.consent_status}</Badge>
                  </div>
                ))
              : (suppressionsQuery.data?.items ?? []).map((s) => (
                  <div
                    key={s.id}
                    className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-slate-200/80 bg-slate-50/60 px-3 py-2 text-[12px]"
                  >
                    <div>
                      <strong className="font-semibold text-slate-900">{s.normalized_address}</strong>
                      <span className="text-slate-600"> ({s.channel_type})</span>
                    </div>
                    <span className="rounded-md border border-rose-200 bg-rose-50 px-1.5 py-0.5 text-[11px] font-medium text-rose-700">
                      {s.reason}
                    </span>
                  </div>
                ))}
          </div>
        </Card>
      </Page>
    </AppShell>
  );
}
