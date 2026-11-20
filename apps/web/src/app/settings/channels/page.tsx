"use client";

import React, { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Ban,
  MessagesSquare,
  RefreshCw,
  ShieldCheck,
  Siren,
  Sparkles,
  UserCheck,
} from "lucide-react";

import { AppShell } from "@/components/app-shell";
import { useAuthSession } from "@/components/providers";
import {
  ConnectedAccountsCard,
  type AccountsFilter,
} from "@/components/channels/connected-accounts-card";
import { MessagingChannelCard } from "@/components/channels/messaging-channel-cards";
import { QuickConnectPanel } from "@/components/channels/quick-connect-panel";
import { TeamMembersCard } from "@/components/channels/team-members-card";
import { BentoStatCard } from "@/components/ui/bento-stat-card";
import { SegmentedTabs } from "@/components/ui/segmented-tabs";
import { Badge, Card, CardHeader, EmptyState, Page, PageHeader } from "@/components/ui/page";
import { replayDlqEvent } from "@/lib/api-client/live-client";
import type {
  EmailConnectPayload,
  InviteTeamMemberPayload,
  MessagingConnectPayload,
  MessagingConnectResult,
  MessagingProviderKind,
  TelegramConnectPayload,
  UpdateChannelOwnershipPayload,
} from "@/lib/api-client/live-client";
import {
  connectEmailChannel,
  connectMessagingChannel,
  disconnectChannelAccount,
  fetchChannelAccounts,
  fetchChannelConsents,
  fetchChannelDlq,
  fetchChannelSuppressions,
  fetchPendingProposalsCount,
  fetchTeamMembers,
  inviteTeamMember,
  updateChannelOwnership,
} from "@/lib/data-source";
import { useI18n } from "@/lib/i18n";

/** Meta 公共主页渠道只能由 owner/admin 绑定（业务员看不到这两张卡）。 */
const ADMIN_MESSAGING_CHANNELS: ReadonlyArray<MessagingProviderKind> = [
  "messenger",
  "instagram_dm",
];

type ConnectTarget = MessagingProviderKind | "email";

export default function ChannelsSettingsPage() {
  const { bi } = useI18n();
  const { principal } = useAuthSession();
  const queryClient = useQueryClient();
  const [logTab, setLogTab] = useState<"consents" | "suppressions">("consents");

  const isAdmin = principal?.role === "owner" || principal?.role === "admin";
  const currentUserId = principal?.user_id ?? null;
  // 业务员默认只看自己的个人渠道，老板/管理员默认看全部。
  const [accountsFilter, setAccountsFilter] = useState<AccountsFilter>(
    isAdmin ? "all" : "mine",
  );

  const pendingProposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () => fetchPendingProposalsCount(),
  });

  const accountsQuery = useQuery({
    queryKey: ["channels", "accounts"],
    queryFn: () => fetchChannelAccounts(),
  });

  // Admin-only 数据：业务员绝不触发这些请求（避免 403 噪音）。
  const membersQuery = useQuery({
    queryKey: ["tenants", "memberships"],
    queryFn: () => fetchTeamMembers(),
    enabled: isAdmin,
  });

  const dlqQuery = useQuery({
    queryKey: ["channels", "dlq"],
    queryFn: () => fetchChannelDlq(),
    enabled: isAdmin,
  });

  const consentsQuery = useQuery({
    queryKey: ["channels", "consents"],
    queryFn: () => fetchChannelConsents(),
    enabled: isAdmin,
  });

  const suppressionsQuery = useQuery({
    queryKey: ["channels", "suppressions"],
    queryFn: () => fetchChannelSuppressions(),
    enabled: isAdmin,
  });

  const replayMutation = useMutation({
    mutationFn: (dlqId: string) => replayDlqEvent(dlqId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["channels", "dlq"] });
    },
  });

  const refreshAccounts = () => {
    void queryClient.invalidateQueries({ queryKey: ["channels", "accounts"] });
  };

  const connectMutation = useMutation({
    mutationFn: ({
      kind,
      payload,
    }: {
      kind: ConnectTarget;
      payload: MessagingConnectPayload | EmailConnectPayload;
    }): Promise<MessagingConnectResult> =>
      kind === "email"
        ? connectEmailChannel(payload as EmailConnectPayload)
        : connectMessagingChannel(kind, payload as MessagingConnectPayload),
    onSuccess: refreshAccounts,
  });

  const ownershipMutation = useMutation({
    mutationFn: ({
      accountId,
      payload,
    }: {
      accountId: string;
      payload: UpdateChannelOwnershipPayload;
    }) => updateChannelOwnership(accountId, payload),
    onSuccess: refreshAccounts,
  });

  const disconnectMutation = useMutation({
    mutationFn: (accountId: string) => disconnectChannelAccount(accountId),
    onSuccess: refreshAccounts,
  });

  const inviteMutation = useMutation({
    mutationFn: (payload: InviteTeamMemberPayload) => inviteTeamMember(payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["tenants", "memberships"] });
      refreshAccounts();
    },
  });

  const accounts = accountsQuery.data?.items ?? [];
  const members = membersQuery.data?.items ?? [];
  const dlqItems = dlqQuery.data?.items ?? [];
  const personalAccounts = accounts.filter((acct) => acct.visibility_scope === "personal");
  const sharedAccounts = accounts.filter((acct) => acct.visibility_scope !== "personal");
  const myAccounts = personalAccounts.filter(
    (acct) => !currentUserId || acct.owner_user_id === currentUserId,
  );

  const emailConnected = accounts.some((acct) => acct.channel_type === "email");
  const telegramConnected = accounts.some((acct) => acct.channel_type === "telegram");

  return (
    <AppShell
      currentPath="/settings/channels"
      pendingProposalsCount={pendingProposalsQuery.data?.total ?? 0}
    >
      <Page>
        <PageHeader
          title={
            isAdmin
              ? bi("渠道接入、业务员归属与合规退订", "Channels, salesperson ownership & compliance")
              : bi("我的渠道快捷绑定", "My channel quick connect")
          }
          description={
            isAdmin
              ? bi(
                  "粘贴即绑 Telegram 机器人与工作邮箱；公司公海或指定业务员个人专属一键切换。令牌一律信封加密，Webhook 按原始报文验签。",
                  "Paste-and-bind Telegram bots and work mailboxes; switch between company-shared and salesperson-personal ownership. Tokens are envelope-encrypted and webhooks verify the raw body."
                )
              : bi(
                  "只贴一段 Bot Token 就能连上 Telegram；工作邮箱也能 30 秒绑定。个人专属渠道仅你与老板/主管可见，客户回复直接进你的 Inbox。",
                  "Paste one bot token to connect Telegram, or bind your work mailbox in 30 seconds. Personal channels stay visible to you and your managers, with replies landing in your inbox."
                )
          }
          badge={
            <Badge tone="success">
              <ShieldCheck className="h-3 w-3" />
              {bi("令牌信封加密 · 零延迟 STOP 抑制", "Envelope encryption · zero-delay STOP")}
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
            hint={bi("Telegram / 邮箱 / WhatsApp / Meta", "Telegram / email / WhatsApp / Meta")}
          />
          <BentoStatCard
            label={bi("我的个人渠道", "My personal channels")}
            value={myAccounts.length}
            icon={Sparkles}
            accent="emerald"
            hint={bi("仅自己与老板/主管可见", "Visible to you and your managers")}
          />
          <BentoStatCard
            label={bi("公司公海渠道", "Company shared channels")}
            value={sharedAccounts.length}
            icon={MessagesSquare}
            accent="violet"
            hint={bi("团队共同接管，不随人离职", "Team-handled, not tied to one person")}
          />
          {isAdmin ? (
            <BentoStatCard
              label={bi("死信队列事件", "Dead-letter events")}
              value={dlqItems.length}
              icon={Siren}
              accent={dlqItems.length > 0 ? "rose" : "emerald"}
              hint={bi("失败事件不丢，可原样重放", "Failed events are retained and replayable")}
            />
          ) : (
            <BentoStatCard
              label={bi("个人绑定建议", "Personal binding tips")}
              value={bi("零配置", "Zero setup")}
              icon={UserCheck}
              accent="slate"
              hint={bi("只贴 Bot Token 即可绑定", "One pasted bot token is enough")}
            />
          )}
        </section>

        <QuickConnectPanel
          isAdmin={isAdmin}
          currentUserId={currentUserId}
          members={members}
          telegramConnected={telegramConnected}
          emailConnected={emailConnected}
          onConnectTelegram={(payload: TelegramConnectPayload) =>
            connectMutation.mutateAsync({ kind: "telegram", payload })
          }
          onConnectEmail={(payload: EmailConnectPayload) =>
            connectMutation.mutateAsync({ kind: "email", payload })
          }
        />

        {isAdmin && (
          <Card>
            <CardHeader
              title={bi(
                "Messenger 与 Instagram 私信（公司主页）",
                "Messenger & Instagram DM (company pages)"
              )}
              description={bi(
                "Meta 公共主页属于公司资产：绑定后默认进入公司公海，也可以直接指派给某位业务员个人专属。",
                "Meta pages are company assets: bindings default to the shared pool but can be assigned to a salesperson."
              )}
            />
            <div className="grid gap-3 p-4 md:grid-cols-2 xl:gap-4">
              {ADMIN_MESSAGING_CHANNELS.map((kind) => (
                <MessagingChannelCard
                  key={kind}
                  kind={kind}
                  connected={accounts.some((acct) => acct.channel_type === kind)}
                  isAdmin
                  currentUserId={currentUserId}
                  members={members}
                  onSubmit={(payload) => connectMutation.mutateAsync({ kind, payload })}
                />
              ))}
            </div>
          </Card>
        )}

        <ConnectedAccountsCard
          accounts={accounts}
          isAdmin={isAdmin}
          currentUserId={currentUserId}
          members={members}
          filter={accountsFilter}
          onFilterChange={setAccountsFilter}
          onChangeOwnership={(accountId, payload) =>
            ownershipMutation.mutateAsync({ accountId, payload }).then(() => undefined)
          }
          onDisconnect={(accountId) =>
            disconnectMutation.mutateAsync(accountId).then(() => undefined)
          }
        />

        {isAdmin && (
          <TeamMembersCard
            members={members}
            accounts={accounts}
            isLoading={membersQuery.isLoading}
            onInvite={(payload) => inviteMutation.mutateAsync(payload)}
          />
        )}

        {isAdmin && (
          <>
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
                  {bi(
                    "静默时段 21:00–08:00 不发送任何营销消息",
                    "Quiet hours 21:00–08:00 block all marketing sends"
                  )}
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
                          <strong className="font-semibold text-slate-900">
                            {s.normalized_address}
                          </strong>
                          <span className="text-slate-600"> ({s.channel_type})</span>
                        </div>
                        <span className="rounded-md border border-rose-200 bg-rose-50 px-1.5 py-0.5 text-[11px] font-medium text-rose-700">
                          {s.reason}
                        </span>
                      </div>
                    ))}
              </div>
            </Card>
          </>
        )}
      </Page>
    </AppShell>
  );
}
