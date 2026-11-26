"use client";

import React, { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, Plug, RefreshCw, ShieldAlert, Trash2, TrendingUp } from "lucide-react";

import { Badge, Card, CardHeader, EmptyState, StatCard, TableWrap } from "@/components/ui/page";
import { useAuthSession } from "@/components/providers";
import { useI18n } from "@/lib/i18n";
import {
  ADS_PLATFORMS,
  canManageAdsAccounts,
  canViewAdsSpend,
  connectAdAccount,
  deleteAdAccount,
  fetchAdAccounts,
  fetchAdsSpendSummary,
  formatAdsMoney,
  formatCount,
  formatMultiple,
  formatPct,
  platformLabel,
  syncAdAccount,
  type AdAccountConnect,
  type AdAccountRead,
  type AdsPlatform,
} from "@/lib/api-client/track9-visual-ads";

const DEFAULT_DRAFT: AdAccountConnect = {
  platform: "sandbox_ads",
  account_name: "",
  external_account_id: "",
  access_token: "",
  currency: "USD",
};

const SYNC_DAYS = 7;

/**
 * Read-only ad spend panel: connect / sync / disconnect, plus the ROAS & TACoS KPIs.
 *
 * Mounted on `/analytics`. Every money value and every ratio is the backend's
 * exact decimal string — the browser never recomputes ROAS or TACoS from floats,
 * and the access token is only ever sent (over the BFF) once, then replaced by a
 * masked fingerprint.
 */
export function AdsSpendPanel({ days = 30 }: { days?: number }) {
  const { bi, locale } = useI18n();
  const { principal } = useAuthSession();
  const queryClient = useQueryClient();
  const canRead = canViewAdsSpend(principal?.role);
  const canManage = canManageAdsAccounts(principal?.role);

  const [draft, setDraft] = useState<AdAccountConnect>(DEFAULT_DRAFT);
  const [error, setError] = useState<string | null>(null);
  const [syncingId, setSyncingId] = useState<string | null>(null);

  const accountsQuery = useQuery({
    queryKey: ["track9", "ads-accounts"],
    queryFn: fetchAdAccounts,
    enabled: canRead,
  });
  const summaryQuery = useQuery({
    queryKey: ["track9", "ads-summary", days],
    queryFn: () => fetchAdsSpendSummary(days),
    enabled: canRead,
  });

  const accounts = useMemo(() => accountsQuery.data ?? [], [accountsQuery.data]);
  const summary = summaryQuery.data;

  const connect = useMutation({
    mutationFn: () => connectAdAccount(draft),
    onSuccess: (account: AdAccountRead) => {
      setError(null);
      setDraft(DEFAULT_DRAFT);
      queryClient.setQueryData<AdAccountRead[]>(["track9", "ads-accounts"], (prev) => [
        ...(prev ?? []),
        account,
      ]);
    },
    onError: (err: Error) => setError(err.message),
  });

  const remove = useMutation({
    mutationFn: (accountId: string) => deleteAdAccount(accountId),
    onSuccess: (_data, accountId) => {
      queryClient.setQueryData<AdAccountRead[]>(["track9", "ads-accounts"], (prev) =>
        (prev ?? []).filter((account) => account.id !== accountId),
      );
      void queryClient.invalidateQueries({ queryKey: ["track9", "ads-summary"] });
    },
    onError: (err: Error) => setError(err.message),
  });

  const sync = useMutation({
    mutationFn: (accountId: string) => syncAdAccount(accountId, SYNC_DAYS),
    onMutate: (accountId: string) => setSyncingId(accountId),
    onSuccess: (result) => {
      setError(null);
      queryClient.setQueryData<AdAccountRead[]>(["track9", "ads-accounts"], (prev) =>
        (prev ?? []).map((account) =>
          account.id === result.account.id ? result.account : account,
        ),
      );
      void queryClient.invalidateQueries({ queryKey: ["track9", "ads-summary"] });
    },
    onError: (err: Error) => setError(err.message),
    onSettled: () => setSyncingId(null),
  });

  if (!canRead) {
    return (
      <Card data-testid="ads-spend-masked" className="p-5">
        <p className="flex items-start gap-2 text-[12.5px] font-medium text-amber-800 dark:text-amber-300">
          <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
          {bi(
            `当前角色（${principal?.role ?? "—"}）不可查看广告花费与 ROAS。`,
            `Your role (${principal?.role ?? "—"}) cannot view ad spend or ROAS.`
          )}
        </p>
      </Card>
    );
  }

  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    connect.mutate();
  }

  return (
    <Card data-testid="ads-spend-panel" className="p-5">
      <CardHeader
        icon={TrendingUp}
        title={bi("广告花费与 ROAS", "Ad spend & ROAS")}
        description={bi(
          "Meta / Amazon 广告账户只读拉取每日花费与归因销售额；ROAS、TACoS 与 CTR 由后端 Decimal 计算并以字符串下发。",
          "Meta / Amazon ad accounts are pulled read-only; ROAS, TACoS and CTR are computed with backend Decimal maths and shipped as strings."
        )}
        actions={
          <Badge tone="info" data-testid="ads-window-badge">
            {bi(`近 ${summary?.days ?? days} 天`, `Last ${summary?.days ?? days} days`)}
          </Badge>
        }
      />

      <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          testId="ads-kpi-spend"
          label={bi("总广告花费", "Total ad spend")}
          value={formatAdsMoney(summary?.total_ad_spend, summary?.currency)}
          hint={bi("窗口内所有广告活动", "All campaigns in the window")}
          tone="info"
        />
        <StatCard
          testId="ads-kpi-attributed"
          label={bi("归因销售额", "Attributed sales")}
          value={formatAdsMoney(summary?.total_attributed_sales, summary?.currency)}
          hint={bi("广告直接归因的成交额", "Revenue the ads are credited with")}
          tone="success"
        />
        <StatCard
          testId="ads-kpi-roas"
          label="ROAS"
          value={formatMultiple(summary?.roas)}
          hint={bi("归因销售额 ÷ 广告花费", "Attributed sales ÷ ad spend")}
          tone="neutral"
        />
        <StatCard
          testId="ads-kpi-tacos"
          label="TACoS %"
          value={formatPct(summary?.tacos_pct)}
          hint={bi("广告花费 ÷ 全店销售额", "Ad spend ÷ total store sales")}
          tone="warning"
        />
      </div>

      {error && (
        <p
          role="alert"
          data-testid="ads-spend-error"
          className="mt-3 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-[12.5px] font-medium text-rose-800 dark:border-rose-800/70 dark:bg-rose-950/40 dark:text-rose-300"
        >
          {error}
        </p>
      )}

      <form
        data-testid="ads-connect-form"
        onSubmit={submit}
        className="mt-4 grid gap-3 rounded-xl border border-gray-200 bg-gray-50/70 p-4 dark:border-gray-700 dark:bg-gray-800/40 sm:grid-cols-2 xl:grid-cols-5"
      >
        <label className="flex flex-col gap-1 text-[12px] font-semibold text-gray-700 dark:text-gray-300">
          {bi("广告平台", "Platform")}
          <select
            data-testid="ads-platform-select"
            value={draft.platform}
            onChange={(event) =>
              setDraft({ ...draft, platform: event.target.value as AdsPlatform })
            }
            className="rounded-lg border border-gray-200 bg-white px-2.5 py-1.5 text-[12.5px] font-normal text-gray-900 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-100"
          >
            {ADS_PLATFORMS.map((option) => (
              <option key={option.value} value={option.value}>
                {locale === "zh" ? option.label_zh : option.label_en}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1 text-[12px] font-semibold text-gray-700 dark:text-gray-300">
          {bi("账户名称", "Account name")}
          <input
            data-testid="ads-account-name-input"
            type="text"
            required
            value={draft.account_name}
            onChange={(event) => setDraft({ ...draft, account_name: event.target.value })}
            className="rounded-lg border border-gray-200 bg-white px-2.5 py-1.5 text-[12.5px] font-normal text-gray-900 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-100"
          />
        </label>

        <label className="flex flex-col gap-1 text-[12px] font-semibold text-gray-700 dark:text-gray-300">
          {bi("平台账户 ID", "External account id")}
          <input
            data-testid="ads-external-id-input"
            type="text"
            required
            value={draft.external_account_id}
            onChange={(event) =>
              setDraft({ ...draft, external_account_id: event.target.value })
            }
            placeholder="act_1234567890"
            className="rounded-lg border border-gray-200 bg-white px-2.5 py-1.5 font-mono text-[12px] font-normal text-gray-900 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-100"
          />
        </label>

        <label className="flex flex-col gap-1 text-[12px] font-semibold text-gray-700 dark:text-gray-300">
          {bi("访问令牌", "Access token")}
          <input
            data-testid="ads-token-input"
            type="password"
            required
            autoComplete="off"
            value={draft.access_token}
            onChange={(event) => setDraft({ ...draft, access_token: event.target.value })}
            className="rounded-lg border border-gray-200 bg-white px-2.5 py-1.5 font-mono text-[12px] font-normal text-gray-900 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-100"
          />
        </label>

        <div className="flex items-end">
          <button
            type="submit"
            data-testid="ads-connect-submit"
            disabled={!canManage || connect.isPending}
            className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 px-3.5 py-2 text-[12.5px] font-semibold text-white transition-colors hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {connect.isPending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
            ) : (
              <Plug className="h-3.5 w-3.5" aria-hidden="true" />
            )}
            {bi("连接广告账户", "Connect account")}
          </button>
        </div>
      </form>

      <div className="mt-4">
        <h3 className="mb-2 text-[13px] font-semibold text-gray-800 dark:text-gray-200">
          {bi("已连接的广告账户", "Connected ad accounts")}
        </h3>
        {accounts.length === 0 ? (
          <EmptyState testId="ads-accounts-empty">
            {bi("还没有连接广告账户。", "No ad account connected yet.")}
          </EmptyState>
        ) : (
          <ul className="space-y-2">
            {accounts.map((account) => (
              <li
                key={account.id}
                data-testid={`ads-account-${account.id}`}
                className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-gray-200 bg-white px-3.5 py-2.5 dark:border-gray-700 dark:bg-gray-900"
              >
                <div className="min-w-0">
                  <p className="truncate text-[13px] font-semibold text-gray-900 dark:text-gray-100">
                    {account.account_name}
                  </p>
                  <p className="text-[11.5px] text-gray-500 dark:text-gray-400">
                    {platformLabel(account.platform, locale)} · {account.external_account_id} ·{" "}
                    {account.currency} · {account.token_preview}
                    {account.last_synced_at
                      ? ` · ${bi("上次同步", "last synced")} ${account.last_synced_at}`
                      : ""}
                  </p>
                </div>
                <div className="flex items-center gap-1.5">
                  <button
                    type="button"
                    data-testid={`ads-sync-${account.id}`}
                    disabled={!canManage || syncingId === account.id}
                    onClick={() => sync.mutate(account.id)}
                    className="inline-flex items-center gap-1 rounded-lg border border-emerald-200 bg-emerald-50/60 px-2.5 py-1 text-[11.5px] font-semibold text-emerald-800 transition-colors hover:bg-emerald-100 disabled:cursor-not-allowed disabled:opacity-40 dark:border-emerald-800/70 dark:bg-emerald-950/40 dark:text-emerald-300"
                  >
                    {syncingId === account.id ? (
                      <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
                    ) : (
                      <RefreshCw className="h-3 w-3" aria-hidden="true" />
                    )}
                    {bi("立即同步广告数据", "Sync ad spend now")}
                  </button>
                  <button
                    type="button"
                    data-testid={`ads-delete-${account.id}`}
                    disabled={!canManage || remove.isPending}
                    onClick={() => remove.mutate(account.id)}
                    className="inline-flex items-center gap-1 rounded-lg border border-gray-200 px-2.5 py-1 text-[11.5px] font-semibold text-gray-700 transition-colors hover:bg-gray-100 disabled:cursor-not-allowed disabled:opacity-40 dark:border-gray-700 dark:text-gray-300 dark:hover:bg-gray-800"
                  >
                    <Trash2 className="h-3 w-3" aria-hidden="true" />
                    {bi("删除", "Delete")}
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="mt-4">
        <h3 className="mb-2 flex flex-wrap items-center justify-between gap-2 text-[13px] font-semibold text-gray-800 dark:text-gray-200">
          <span>{bi("每日广告活动明细", "Daily campaign breakdown")}</span>
          <span className="text-[11.5px] font-normal text-gray-500 dark:text-gray-400">
            {bi(
              `${formatCount(summary?.total_impressions)} 次展示 · ${formatCount(
                summary?.total_clicks
              )} 次点击 · CTR ${formatPct(summary?.ctr_pct)}`,
              `${formatCount(summary?.total_impressions)} impressions · ${formatCount(
                summary?.total_clicks
              )} clicks · CTR ${formatPct(summary?.ctr_pct)}`
            )}
          </span>
        </h3>
        <TableWrap>
          <table data-testid="ads-spend-table" className="erp-table">
            <thead>
              <tr>
                <th>{bi("日期", "Date")}</th>
                <th>{bi("广告活动", "Campaign")}</th>
                <th>SKU</th>
                <th className="text-right">{bi("展示", "Impr.")}</th>
                <th className="text-right">{bi("点击", "Clicks")}</th>
                <th className="text-right">{bi("花费", "Spend")}</th>
                <th className="text-right">{bi("归因销售额", "Attributed")}</th>
                <th className="text-right">{bi("全店销售额", "Store sales")}</th>
              </tr>
            </thead>
            <tbody>
              {(summary?.rows ?? []).map((row) => (
                <tr key={row.id} data-testid={`ads-spend-row-${row.id}`}>
                  <td className="tabular-nums text-gray-700 dark:text-gray-300">{row.spend_date}</td>
                  <td className="font-semibold text-gray-900 dark:text-gray-100">
                    {row.campaign_name}
                  </td>
                  <td className="font-mono text-[11.5px] text-gray-600 dark:text-gray-400">
                    {row.sku ?? "—"}
                  </td>
                  <td className="text-right tabular-nums">{formatCount(row.impressions)}</td>
                  <td className="text-right tabular-nums">{formatCount(row.clicks)}</td>
                  <td className="text-right tabular-nums">
                    {formatAdsMoney(row.ad_spend, row.currency)}
                  </td>
                  <td className="text-right tabular-nums text-emerald-700 dark:text-emerald-400">
                    {formatAdsMoney(row.attributed_sales, row.currency)}
                  </td>
                  <td className="text-right tabular-nums text-gray-600 dark:text-gray-400">
                    {formatAdsMoney(row.total_store_sales, row.currency)}
                  </td>
                </tr>
              ))}
              {(summary?.rows ?? []).length === 0 && (
                <tr>
                  <td
                    colSpan={8}
                    className="py-6 text-center text-[12.5px] text-gray-500 dark:text-gray-400"
                  >
                    {bi(
                      "窗口内还没有广告花费数据，先连接账户并同步。",
                      "No ad spend in this window yet — connect an account and sync."
                    )}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </TableWrap>
      </div>
    </Card>
  );
}
