"use client";

import React, {
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Ban,
  FileText,
  LayoutGrid,
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
  AGENT_MARKETPLACE_KEYS,
  ChannelMarketplaceGrid,
  isMarketplaceChannelKey,
  visibleMarketplaceItems,
} from "@/components/channels/channel-marketplace";
import { ChannelConnectDrawer } from "@/components/channels/channel-connect-drawer";
import {
  ConnectedAccountsCard,
  type AccountsFilter,
} from "@/components/channels/connected-accounts-card";
import { TeamMembersCard } from "@/components/channels/team-members-card";
import { TeamGovernancePanel } from "@/components/settings/team-governance-panel";
import { PiTemplateCard } from "@/components/channels/pi-template-card";
import { BentoStatCard } from "@/components/ui/bento-stat-card";
import {
  SegmentedTabs,
  type SegmentedTabOption,
} from "@/components/ui/segmented-tabs";
import {
  Badge,
  Card,
  CardHeader,
  EmptyState,
  Page,
  PageHeader,
  TableWrap,
} from "@/components/ui/page";
import { replayDlqEvent } from "@/lib/api-client/live-client";
import type {
  InviteTeamMemberPayload,
  MarketplaceChannelKey,
  MarketplaceConnectRequest,
  MessagingConnectResult,
  UpdateChannelOwnershipPayload,
} from "@/lib/api-client/live-client";
import {
  connectMarketplaceChannel,
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

type HubTab = "marketplace" | "accounts" | "team" | "pi_template" | "compliance";

export default function ChannelsSettingsPage() {
  return (
    <Suspense fallback={<ChannelsHubFallback />}>
      <ChannelsHub />
    </Suspense>
  );
}

function ChannelsHubFallback() {
  return (
    <AppShell currentPath="/settings/channels">
      <Page>
        <div className="erp-card animate-pulse p-8 text-[13px] font-medium text-slate-500 dark:text-slate-400">
          正在加载渠道中心…
        </div>
      </Page>
    </AppShell>
  );
}

function ChannelsHub() {
  const { bi } = useI18n();
  const router = useRouter();
  const searchParams = useSearchParams();
  const { principal } = useAuthSession();
  const queryClient = useQueryClient();

  const [tab, setTab] = useState<HubTab>("marketplace");
  const [drawerKey, setDrawerKey] = useState<MarketplaceChannelKey | null>(null);
  const [logTab, setLogTab] = useState<"consents" | "suppressions">("consents");

  const isAdmin = principal?.role === "owner" || principal?.role === "admin";
  const currentUserId = principal?.user_id ?? null;
  // 业务员默认只看自己的个人渠道，老板/管理员默认看全部；principal 异步加载完成后自动同步。
  const [selectedAccountsFilter, setSelectedAccountsFilter] = useState<AccountsFilter | null>(
    null,
  );
  const accountsFilter: AccountsFilter = selectedAccountsFilter ?? (isAdmin ? "all" : "mine");

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

  const refreshAccounts = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["channels", "accounts"] });
  }, [queryClient]);

  const connectMutation = useMutation({
    mutationFn: (request: MarketplaceConnectRequest): Promise<MessagingConnectResult> =>
      connectMarketplaceChannel(request),
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

  const marketplaceCount = visibleMarketplaceItems(isAdmin).length;

  /** 允许当前角色打开的抽屉键：业务员只能开 3 个个人渠道。 */
  const canOpenDrawer = useCallback(
    (key: MarketplaceChannelKey) => isAdmin || AGENT_MARKETPLACE_KEYS.includes(key),
    [isAdmin],
  );

  const closeDrawer = useCallback(() => {
    setDrawerKey(null);
    if (searchParams.get("connect")) {
      router.replace("/settings/channels");
    }
  }, [router, searchParams]);

  const openDrawer = useCallback(
    (key: MarketplaceChannelKey) => {
      if (!canOpenDrawer(key)) return;
      setDrawerKey(key);
    },
    [canOpenDrawer],
  );

  // Inbox 的「快捷绑定此渠道 →」直达：/settings/channels?connect=<channel>
  const connectParam = searchParams.get("connect");
  useEffect(() => {
    if (!isMarketplaceChannelKey(connectParam)) return;
    if (!canOpenDrawer(connectParam)) return;
    setDrawerKey(connectParam);
  }, [canOpenDrawer, connectParam]);

  // 直达指定分栏：/settings/channels?tab=team 等
  const tabParam = searchParams.get("tab");
  useEffect(() => {
    if (tabParam === "marketplace" || tabParam === "accounts") {
      setTab(tabParam);
      return;
    }
    if (
      isAdmin &&
      (tabParam === "team" || tabParam === "pi_template" || tabParam === "compliance")
    ) {
      setTab(tabParam);
    }
  }, [isAdmin, tabParam]);

  const tabs = useMemo<SegmentedTabOption<HubTab>[]>(() => {
    const options: SegmentedTabOption<HubTab>[] = [
      {
        value: "marketplace" as const,
        label: (
          <span className="inline-flex items-center gap-1.5">
            <LayoutGrid className="h-3.5 w-3.5" />
            {isAdmin ? bi("渠道应用市场", "Marketplace") : bi("个人渠道绑定", "My channels")}
          </span>
        ),
        badge: marketplaceCount,
        testId: "hub-tab-marketplace",
      },
      {
        value: "accounts" as const,
        label: isAdmin
          ? bi("已接入账号", "Connected accounts")
          : bi("我的已接入账号", "My connected accounts"),
        badge: accounts.length,
        testId: "hub-tab-accounts",
      },
    ];
    if (isAdmin) {
      options.push(
        {
          value: "team" as const,
          label: bi("团队成员", "Team"),
          badge: members.length,
          testId: "hub-tab-team",
        },
        {
          value: "pi_template" as const,
          label: (
            <span className="inline-flex items-center gap-1.5">
              <FileText className="h-3.5 w-3.5" />
              {bi("PI 模板", "PI template")}
            </span>
          ),
          testId: "hub-tab-pi_template",
        },
        {
          value: "compliance" as const,
          label: bi("运维与合规", "Ops & compliance"),
          badge: dlqItems.length,
          testId: "hub-tab-compliance",
        },
      );
    }
    return options;
  }, [accounts.length, bi, dlqItems.length, isAdmin, marketplaceCount, members.length]);

  return (
    <AppShell
      currentPath="/settings/channels"
      pendingProposalsCount={pendingProposalsQuery.data?.total ?? 0}
    >
      <Page>
        <PageHeader
          title={bi("渠道中心", "Channel hub")}
          description={
            isAdmin
              ? bi(
                  "11 个渠道一处接入：即时通讯、工作邮箱与独立站。令牌信封加密，Webhook 按原始报文验签。",
                  "Eleven channels in one hub: messaging, work email and storefronts — envelope-encrypted tokens and signed webhooks."
                )
              : bi(
                  "绑定你自己的 Telegram、工作邮箱与 WhatsApp 共存号，客户回复直接进你的 Inbox。",
                  "Bind your own Telegram, work mailbox and WhatsApp coexistence number; replies land in your inbox."
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
          data-testid="channel-kpi-strip"
          className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"
        >
          <BentoStatCard
            label={bi("已接入账号", "Connected accounts")}
            value={accounts.length}
            icon={MessagesSquare}
            accent="blue"
            hint={bi(
              "WhatsApp / Telegram / 邮箱 / 独立站",
              "WhatsApp / Telegram / email / storefronts"
            )}
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

        <div className="flex flex-wrap items-center justify-between gap-2">
          <SegmentedTabs
            ariaLabel={bi("渠道中心分区", "Channel hub sections")}
            value={tab}
            onChange={setTab}
            options={tabs}
            className="flex-wrap"
          />
          <span className="text-[11px] font-medium text-slate-500 dark:text-slate-400">
            {bi(
              "令牌一律信封加密，从不以明文回显",
              "Tokens stay envelope-encrypted and are never echoed back"
            )}
          </span>
        </div>

        {tab === "marketplace" && (
          <div className="w-full">
            <ChannelMarketplaceGrid
              accounts={accounts}
              isAdmin={isAdmin}
              onOpen={openDrawer}
            />
          </div>
        )}

        {tab === "accounts" && (
          <div className="w-full">
            <ConnectedAccountsCard
              accounts={accounts}
              isAdmin={isAdmin}
              currentUserId={currentUserId}
              members={members}
              tenantSlug={principal?.tenant_slug ?? null}
              filter={accountsFilter}
              onFilterChange={setSelectedAccountsFilter}
              onQuickConnect={() => setTab("marketplace")}
              onChangeOwnership={(accountId, payload) =>
                ownershipMutation.mutateAsync({ accountId, payload }).then(() => undefined)
              }
              onDisconnect={(accountId) =>
                disconnectMutation.mutateAsync(accountId).then(() => undefined)
              }
            />
          </div>
        )}

        {tab === "team" && isAdmin && (
          <div className="w-full space-y-3">
            <TeamMembersCard
              members={members}
              accounts={accounts}
              isLoading={membersQuery.isLoading}
              onInvite={(payload) => inviteMutation.mutateAsync(payload)}
            />
            <TeamGovernancePanel
              members={members}
              accounts={accounts}
              isAdmin={isAdmin}
            />
          </div>
        )}

        {tab === "pi_template" && isAdmin && (
          <div className="w-full">
            <PiTemplateCard enabled={isAdmin} />
          </div>
        )}

        {tab === "compliance" && isAdmin && (
          <div className="w-full space-y-3">
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
                      className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-rose-200 bg-rose-50/60 px-3 py-2.5 dark:border-rose-800/60 dark:bg-rose-950/30"
                    >
                      <div className="min-w-0">
                        <div className="text-[13px] font-medium text-slate-900 dark:text-slate-100">
                          {ev.channel_type} · {ev.external_event_id}
                        </div>
                        <div className="text-[12px] text-rose-700 dark:text-rose-300">
                          {ev.error_message} ({bi("重试", "retries")} {ev.retry_count})
                        </div>
                      </div>
                      <button
                        type="button"
                        data-testid={`dlq-replay-btn-${ev.id}`}
                        onClick={() => replayMutation.mutate(ev.id)}
                        className="erp-btn rounded-xl bg-blue-600 text-white shadow-xs transition-colors hover:bg-blue-700 dark:bg-blue-600 dark:hover:bg-blue-500"
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
              <div className="flex flex-wrap items-center gap-2 border-b border-slate-200 bg-slate-50/80 p-3 dark:border-slate-800 dark:bg-slate-800/60">
                <SegmentedTabs
                  ariaLabel={bi("合规台账视图", "Compliance ledger view")}
                  value={logTab}
                  onChange={setLogTab}
                  size="sm"
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
                <span className="ml-auto text-[11px] font-medium text-slate-600 dark:text-slate-400">
                  {bi(
                    "静默时段 21:00–08:00 不发送任何营销消息",
                    "Quiet hours 21:00–08:00 block all marketing sends"
                  )}
                </span>
              </div>

              <div className="p-4">
                {logTab === "consents" ? (
                  (consentsQuery.data?.items ?? []).length === 0 ? (
                    <EmptyState>
                      {bi("还没有订阅同意记录。", "No consent records yet.")}
                    </EmptyState>
                  ) : (
                    <TableWrap>
                      <table className="erp-table">
                        <thead>
                          <tr>
                            <th>{bi("客户", "Customer")}</th>
                            <th>{bi("渠道", "Channel")}</th>
                            <th>{bi("证明来源", "Proof source")}</th>
                            <th>{bi("状态", "Status")}</th>
                          </tr>
                        </thead>
                        <tbody>
                          {(consentsQuery.data?.items ?? []).map((c) => (
                            <tr key={c.id}>
                              <td className="font-semibold text-slate-900 dark:text-slate-100">
                                {c.customer_name}
                              </td>
                              <td>
                                <span className="rounded-md bg-slate-50 px-1.5 py-0.5 text-[11px] font-medium text-slate-700 ring-1 ring-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:ring-slate-700">
                                  {c.channel_type}
                                </span>
                              </td>
                              <td className="text-slate-600 dark:text-slate-400">{c.proof_source}</td>
                              <td>
                                <Badge tone="success">{c.consent_status}</Badge>
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </TableWrap>
                  )
                ) : (suppressionsQuery.data?.items ?? []).length === 0 ? (
                  <EmptyState>
                    {bi("抑制名单为空，暂无退订客户。", "The suppression list is empty.")}
                  </EmptyState>
                ) : (
                  <TableWrap>
                    <table className="erp-table">
                      <thead>
                        <tr>
                          <th>{bi("标准化地址", "Normalized address")}</th>
                          <th>{bi("渠道", "Channel")}</th>
                          <th>{bi("原因", "Reason")}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {(suppressionsQuery.data?.items ?? []).map((r) => (
                          <tr key={r.id}>
                            <td className="font-semibold text-slate-900 dark:text-slate-100">
                              {r.normalized_address}
                            </td>
                            <td className="text-slate-600 dark:text-slate-400">{r.channel_type}</td>
                            <td>
                              <span className="rounded-md border border-rose-200 bg-rose-50 px-1.5 py-0.5 text-[11px] font-medium text-rose-700 dark:border-rose-800/70 dark:bg-rose-950/40 dark:text-rose-300">
                                {r.reason}
                              </span>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </TableWrap>
                )}
              </div>
            </Card>
          </div>
        )}
      </Page>

      <ChannelConnectDrawer
        channelKey={drawerKey}
        accounts={accounts}
        isAdmin={isAdmin}
        currentUserId={currentUserId}
        members={members}
        tenantSlug={principal?.tenant_slug ?? null}
        onSubmit={(request) => connectMutation.mutateAsync(request)}
        onConnected={refreshAccounts}
        onClose={closeDrawer}
        onViewAccounts={() => {
          closeDrawer();
          setTab("accounts");
        }}
      />
    </AppShell>
  );
}
