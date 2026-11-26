"use client";

import React, { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowRightLeft,
  Loader2,
  Lock,
  ShieldCheck,
  Store,
  UserMinus,
} from "lucide-react";

import { Badge, Card, CardHeader, EmptyState, TableWrap } from "@/components/ui/page";
import type { ChannelAccountApiItem, TeamMemberApiItem } from "@/lib/api-client/live-client";
import {
  STORE_ACCESS_LEVELS,
  assignStoreScope,
  canManageStore,
  fetchStoreScopes,
  handoverImpactRows,
  revokeStoreScope,
  handoverMemberAssets,
  type StoreAccessLevel,
} from "@/lib/api-client/track6-team-kb";
import type { HandoverResult } from "@/lib/api-client/types.gen";
import { useI18n } from "@/lib/i18n";

const LEVEL_COPY: Record<StoreAccessLevel, { zh: string; en: string }> = {
  read: { zh: "只读", en: "Read only" },
  operate: { zh: "可接待", en: "Operate" },
  manage: { zh: "可管理", en: "Manage" },
};

function levelTone(level: StoreAccessLevel): "neutral" | "info" | "success" {
  if (level === "manage") return "success";
  if (level === "operate") return "info";
  return "neutral";
}

/**
 * Team store-scope governance + offboarding handover.
 *
 * Rendered inside the channel hub's Team/Governance section. Grants are the
 * server's `member_channel_scopes` rows; nothing about visibility, ownership or
 * the handover counts is computed in the browser.
 */
export function TeamGovernancePanel({
  members,
  accounts,
  isAdmin,
}: {
  members: TeamMemberApiItem[];
  accounts: ChannelAccountApiItem[];
  isAdmin: boolean;
}) {
  const { bi } = useI18n();
  const queryClient = useQueryClient();

  const activeMembers = useMemo(
    () => (members ?? []).filter((member) => member.is_active),
    [members]
  );
  const activeAccounts = useMemo(
    () => (accounts ?? []).filter((account) => account.status === "active"),
    [accounts]
  );

  const [selectedMember, setSelectedMember] = useState("");
  const [selectedAccount, setSelectedAccount] = useState("");
  const [selectedLevel, setSelectedLevel] = useState<StoreAccessLevel>("operate");
  const [grantError, setGrantError] = useState<string | null>(null);

  const [handoverFrom, setHandoverFrom] = useState("");
  const [handoverTo, setHandoverTo] = useState("");
  const [deactivateLeaver, setDeactivateLeaver] = useState(true);
  const [confirmed, setConfirmed] = useState(false);
  const [handoverError, setHandoverError] = useState<string | null>(null);
  const [handoverResult, setHandoverResult] = useState<HandoverResult | null>(null);

  const scopesQuery = useQuery({
    queryKey: ["track6", "store-scopes"],
    queryFn: () => fetchStoreScopes(),
    enabled: isAdmin,
  });

  const scopes = scopesQuery.data ?? [];
  const memberLabel = useMemo(() => {
    const map = new Map<string, string>();
    for (const member of members ?? []) {
      map.set(member.user_id, `${member.full_name} · ${member.email}`);
    }
    return map;
  }, [members]);
  const accountLabel = useMemo(() => {
    const map = new Map<string, string>();
    for (const account of accounts ?? []) {
      map.set(account.id, `${account.display_name} (${account.channel_type})`);
    }
    return map;
  }, [accounts]);

  const assignMutation = useMutation({
    mutationFn: () =>
      assignStoreScope({
        user_id: selectedMember,
        channel_account_id: selectedAccount,
        access_level: selectedLevel,
      }),
    onSuccess: () => {
      setGrantError(null);
      void queryClient.invalidateQueries({ queryKey: ["track6", "store-scopes"] });
    },
    onError: (error: unknown) => {
      setGrantError(error instanceof Error ? error.message : String(error));
    },
  });

  const revokeMutation = useMutation({
    mutationFn: (scopeId: string) => revokeStoreScope(scopeId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["track6", "store-scopes"] });
    },
    onError: (error: unknown) => {
      setGrantError(error instanceof Error ? error.message : String(error));
    },
  });

  const handoverMutation = useMutation({
    mutationFn: (dryRun: boolean) =>
      handoverMemberAssets({
        from_user_id: handoverFrom,
        to_user_id: handoverTo,
        deactivate_from_user: deactivateLeaver,
        dry_run: dryRun,
      }),
    onSuccess: (result) => {
      setHandoverError(null);
      setHandoverResult(result);
      if (!result.dry_run) {
        setConfirmed(false);
        void queryClient.invalidateQueries({ queryKey: ["track6", "store-scopes"] });
        void queryClient.invalidateQueries({ queryKey: ["tenants", "memberships"] });
        void queryClient.invalidateQueries({ queryKey: ["channels", "accounts"] });
      }
    },
    onError: (error: unknown) => {
      setHandoverError(error instanceof Error ? error.message : String(error));
    },
  });

  if (!isAdmin) return null;

  const canAssign = Boolean(selectedMember && selectedAccount) && !assignMutation.isPending;
  const canHandover =
    Boolean(handoverFrom && handoverTo) && handoverFrom !== handoverTo && !handoverMutation.isPending;

  return (
    <div className="space-y-3" data-testid="team-governance-panel">
      <Card data-testid="store-scope-card">
        <CardHeader
          icon={Store}
          title={bi("门店 / 渠道权限分配", "Store & channel permission grants")}
          description={bi(
            "按人授权：谁能看、谁能接待、谁能改配置。离职时整批移交，客户不会因为人走了而断线。",
            "Grant per person: who can read, who can operate, who can reconfigure. On offboarding the whole batch moves over, so customers never go dark."
          )}
          actions={
            <Badge tone="info">
              <ShieldCheck className="h-3 w-3" />
              {bi(`${scopes.length} 条授权`, `${scopes.length} grants`)}
            </Badge>
          }
        />

        <div className="flex flex-wrap items-end gap-2 border-b border-gray-200 p-4 dark:border-gray-800">
          <label className="min-w-[190px] flex-1">
            <span className="mb-1 block text-[11px] font-semibold text-gray-500 dark:text-gray-400">
              {bi("团队成员", "Team member")}
            </span>
            <select
              data-testid="store-scope-member"
              value={selectedMember}
              onChange={(event) => setSelectedMember(event.target.value)}
              className="w-full rounded-xl border border-gray-200 bg-white px-2.5 py-1.5 text-[12.5px] font-medium text-gray-800 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200"
            >
              <option value="">{bi("选择成员…", "Select a member…")}</option>
              {activeMembers.map((member) => (
                <option key={member.membership_id} value={member.user_id}>
                  {member.full_name} · {member.email}
                </option>
              ))}
            </select>
          </label>

          <label className="min-w-[190px] flex-1">
            <span className="mb-1 block text-[11px] font-semibold text-gray-500 dark:text-gray-400">
              {bi("渠道账号 / 门店", "Channel account / store")}
            </span>
            <select
              data-testid="store-scope-account"
              value={selectedAccount}
              onChange={(event) => setSelectedAccount(event.target.value)}
              className="w-full rounded-xl border border-gray-200 bg-white px-2.5 py-1.5 text-[12.5px] font-medium text-gray-800 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200"
            >
              <option value="">{bi("选择渠道…", "Select a channel…")}</option>
              {activeAccounts.map((account) => (
                <option key={account.id} value={account.id}>
                  {account.display_name} ({account.channel_type})
                </option>
              ))}
            </select>
          </label>

          <label className="min-w-[130px]">
            <span className="mb-1 block text-[11px] font-semibold text-gray-500 dark:text-gray-400">
              {bi("权限级别", "Access level")}
            </span>
            <select
              data-testid="store-scope-level"
              value={selectedLevel}
              onChange={(event) => setSelectedLevel(event.target.value as StoreAccessLevel)}
              className="w-full rounded-xl border border-gray-200 bg-white px-2.5 py-1.5 text-[12.5px] font-medium text-gray-800 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200"
            >
              {STORE_ACCESS_LEVELS.map((level) => (
                <option key={level} value={level}>
                  {bi(LEVEL_COPY[level].zh, LEVEL_COPY[level].en)}
                </option>
              ))}
            </select>
          </label>

          <button
            type="button"
            data-testid="store-scope-assign"
            disabled={!canAssign}
            onClick={() => assignMutation.mutate()}
            className="erp-btn rounded-xl bg-blue-600 text-white shadow-xs transition-colors hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-blue-600 dark:hover:bg-blue-500"
          >
            {assignMutation.isPending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <ShieldCheck className="h-3.5 w-3.5" />
            )}
            <span>{bi("授权", "Grant")}</span>
          </button>
        </div>

        {grantError && (
          <div
            data-testid="store-scope-error"
            className="mx-4 mt-3 rounded-xl border border-rose-200 bg-rose-50 px-3 py-2 text-[12px] font-medium text-rose-700 dark:border-rose-800/70 dark:bg-rose-950/40 dark:text-rose-300"
          >
            {grantError}
          </div>
        )}

        <div className="p-4">
          {scopes.length === 0 ? (
            <EmptyState testId="store-scope-empty">
              {scopesQuery.isLoading
                ? bi("正在加载授权…", "Loading grants…")
                : bi("还没有任何门店权限，先为成员授权。", "No store grants yet — assign one above.")}
            </EmptyState>
          ) : (
            <TableWrap>
              <table className="erp-table">
                <thead>
                  <tr>
                    <th>{bi("成员", "Member")}</th>
                    <th>{bi("渠道 / 门店", "Channel / store")}</th>
                    <th>{bi("权限", "Access")}</th>
                    <th>{bi("操作", "Actions")}</th>
                  </tr>
                </thead>
                <tbody>
                  {scopes.map((scope) => (
                    <tr key={scope.id} data-testid={`store-scope-row-${scope.id}`}>
                      <td className="font-semibold text-gray-900 dark:text-gray-100">
                        {scope.user_email ??
                          memberLabel.get(scope.user_id) ?? (
                            <span className="inline-flex items-center gap-1 text-gray-500">
                              <Lock className="h-3 w-3" />
                              {scope.user_id}
                            </span>
                          )}
                      </td>
                      <td className="text-gray-600 dark:text-gray-400">
                        {scope.channel_display_name ??
                          accountLabel.get(scope.channel_account_id) ??
                          scope.channel_account_id}
                      </td>
                      <td>
                        <Badge tone={levelTone(scope.access_level)}>
                          {bi(LEVEL_COPY[scope.access_level].zh, LEVEL_COPY[scope.access_level].en)}
                        </Badge>
                      </td>
                      <td>
                        <button
                          type="button"
                          data-testid={`store-scope-revoke-${scope.id}`}
                          disabled={revokeMutation.isPending || !canManageStore(scope.access_level)}
                          title={
                            canManageStore(scope.access_level)
                              ? bi("撤销授权", "Revoke grant")
                              : bi("只有管理级授权可以撤销", "Only manage-level grants can be revoked")
                          }
                          onClick={() => revokeMutation.mutate(scope.id)}
                          className="erp-btn rounded-lg border border-rose-200 bg-rose-50 text-[11.5px] font-semibold text-rose-700 transition-colors hover:bg-rose-100 disabled:cursor-not-allowed disabled:opacity-50 dark:border-rose-800/70 dark:bg-rose-950/40 dark:text-rose-300"
                        >
                          {bi("撤销", "Revoke")}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableWrap>
          )}
        </div>
      </Card>

      <Card data-testid="handover-card">
        <CardHeader
          icon={UserMinus}
          title={bi("离职交接（整批移交）", "Offboarding handover (batch)")}
          description={bi(
            "先试算再执行：商机、会话、渠道账号与门店权限一次性移交，可选停用离职成员。数字全部由服务端统计。",
            "Preview first, then execute: opportunities, conversations, channel accounts and store grants move in one atomic pass; optionally deactivate the leaver. Every number comes from the server."
          )}
          actions={
            <Badge tone="warning">
              <AlertTriangle className="h-3 w-3" />
              {bi("不可撤销", "Irreversible")}
            </Badge>
          }
        />

        <div className="flex flex-wrap items-end gap-2 p-4">
          <label className="min-w-[190px] flex-1">
            <span className="mb-1 block text-[11px] font-semibold text-gray-500 dark:text-gray-400">
              {bi("离职成员", "Departing member")}
            </span>
            <select
              data-testid="handover-from"
              value={handoverFrom}
              onChange={(event) => {
                setHandoverFrom(event.target.value);
                setHandoverResult(null);
                setConfirmed(false);
              }}
              className="w-full rounded-xl border border-gray-200 bg-white px-2.5 py-1.5 text-[12.5px] font-medium text-gray-800 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200"
            >
              <option value="">{bi("选择离职成员…", "Select the leaver…")}</option>
              {activeMembers.map((member) => (
                <option key={member.membership_id} value={member.user_id}>
                  {member.full_name} · {member.email}
                </option>
              ))}
            </select>
          </label>

          <label className="min-w-[190px] flex-1">
            <span className="mb-1 block text-[11px] font-semibold text-gray-500 dark:text-gray-400">
              {bi("交接给", "Hand over to")}
            </span>
            <select
              data-testid="handover-to"
              value={handoverTo}
              onChange={(event) => {
                setHandoverTo(event.target.value);
                setHandoverResult(null);
                setConfirmed(false);
              }}
              className="w-full rounded-xl border border-gray-200 bg-white px-2.5 py-1.5 text-[12.5px] font-medium text-gray-800 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200"
            >
              <option value="">{bi("选择接手成员…", "Select the successor…")}</option>
              {activeMembers
                .filter((member) => member.user_id !== handoverFrom)
                .map((member) => (
                  <option key={member.membership_id} value={member.user_id}>
                    {member.full_name} · {member.email}
                  </option>
                ))}
            </select>
          </label>

          <label className="inline-flex items-center gap-1.5 text-[12px] font-medium text-gray-600 dark:text-gray-400">
            <input
              type="checkbox"
              data-testid="handover-deactivate"
              checked={deactivateLeaver}
              onChange={(event) => setDeactivateLeaver(event.target.checked)}
              className="h-3.5 w-3.5 rounded border-gray-300"
            />
            {bi("同时停用离职成员", "Deactivate the leaver")}
          </label>

          <button
            type="button"
            data-testid="handover-preview"
            disabled={!canHandover}
            onClick={() => handoverMutation.mutate(true)}
            className="erp-btn rounded-xl border border-gray-200 bg-white text-[12px] font-semibold text-gray-800 shadow-xs transition-colors hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200"
          >
            <ArrowRightLeft className="h-3.5 w-3.5" />
            <span>{bi("试算影响范围", "Preview impact")}</span>
          </button>

          <label className="inline-flex items-center gap-1.5 text-[12px] font-medium text-gray-600 dark:text-gray-400">
            <input
              type="checkbox"
              data-testid="handover-confirm"
              checked={confirmed}
              onChange={(event) => setConfirmed(event.target.checked)}
              className="h-3.5 w-3.5 rounded border-gray-300"
            />
            {bi("我确认执行", "I confirm")}
          </label>

          <button
            type="button"
            data-testid="handover-execute"
            disabled={!canHandover || !confirmed}
            onClick={() => handoverMutation.mutate(false)}
            className="erp-btn rounded-xl bg-rose-600 text-white shadow-xs transition-colors hover:bg-rose-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {handoverMutation.isPending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <UserMinus className="h-3.5 w-3.5" />
            )}
            <span>{bi("执行交接", "Execute handover")}</span>
          </button>
        </div>

        {handoverError && (
          <div
            data-testid="handover-error"
            className="mx-4 mb-3 rounded-xl border border-rose-200 bg-rose-50 px-3 py-2 text-[12px] font-medium text-rose-700 dark:border-rose-800/70 dark:bg-rose-950/40 dark:text-rose-300"
          >
            {handoverError}
          </div>
        )}

        <div className="px-4 pb-4">
          {handoverResult === null ? (
            <EmptyState testId="handover-result-empty">
              {bi(
                "先点「试算影响范围」，确认数字后再执行。",
                "Run “Preview impact” first, then execute once the numbers match."
              )}
            </EmptyState>
          ) : (
            <div
              data-testid="handover-result"
              data-dry-run={handoverResult.dry_run ? "true" : "false"}
              className="rounded-xl border border-gray-200 bg-gray-50/70 p-3 dark:border-gray-800 dark:bg-gray-800/40"
            >
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <Badge tone={handoverResult.dry_run ? "info" : "success"}>
                  {handoverResult.dry_run
                    ? bi("试算结果（未改动数据）", "Preview (nothing changed)")
                    : bi("交接已完成", "Handover executed")}
                </Badge>
                <span
                  data-testid="handover-total"
                  className="text-[12px] font-semibold text-gray-700 dark:text-gray-300"
                >
                  {bi("合计", "Total")} {handoverResult.total_reassigned}
                </span>
                {handoverResult.from_user_deactivated && (
                  <Badge tone="warning">
                    {bi("离职成员已停用", "Leaver deactivated")}
                  </Badge>
                )}
              </div>
              <dl className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
                {handoverImpactRows(handoverResult).map((row) => (
                  <div
                    key={row.key}
                    data-testid={`handover-count-${row.key}`}
                    className="rounded-lg border border-gray-200 bg-white px-2.5 py-2 dark:border-gray-700 dark:bg-gray-900"
                  >
                    <dt className="text-[11px] font-medium text-gray-500 dark:text-gray-400">
                      {bi(row.label_zh, row.label_en)}
                    </dt>
                    <dd className="text-[16px] font-bold tabular-nums text-gray-900 dark:text-gray-100">
                      {row.count}
                    </dd>
                  </div>
                ))}
              </dl>
            </div>
          )}
        </div>
      </Card>
    </div>
  );
}

export default TeamGovernancePanel;
