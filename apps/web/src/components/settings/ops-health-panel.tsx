"use client";

import React from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  AlertTriangle,
  Database,
  HardDrive,
  RefreshCw,
  ServerCog,
  Wallet,
} from "lucide-react";

import { Badge, Card, CardHeader, EmptyState, TableWrap } from "@/components/ui/page";
import {
  bytesToHuman,
  fetchOpsSnapshot,
  formatMilliseconds,
  opsStatusLabel,
  opsStatusTone,
  overageSummary,
  totalFailedEvents,
  type OpsHealthRead,
  type OpsStatus,
} from "@/lib/api-client/track8-ops";
import { useI18n } from "@/lib/i18n";

/**
 * Production ops + billing panel for `/settings/channels` → `运维与合规`.
 *
 * Owner/admin only. Every value rendered here comes verbatim from
 * `GET /api/v1/ops/health`, `GET /api/v1/billing/wallet` and
 * `GET /api/v1/billing/usage`; the browser never derives a health verdict, a
 * latency figure or a credit balance on its own.
 */
export function OpsHealthPanel({ isAdmin }: { isAdmin: boolean }) {
  const { bi, locale } = useI18n();
  const snapshotQuery = useQuery({
    queryKey: ["track8", "ops-health"],
    queryFn: () => fetchOpsSnapshot(),
    enabled: isAdmin,
    staleTime: 15_000,
  });

  if (!isAdmin) return null;

  const snapshot = snapshotQuery.data;
  const health: OpsHealthRead | undefined = snapshot?.health;
  const wallet = snapshot?.wallet;
  const usage = snapshot?.usage;
  const status: OpsStatus = health?.status ?? "critical";
  const failed = health ? totalFailedEvents(health) : 0;

  return (
    <div className="w-full space-y-3" data-testid="ops-health-panel">
      <Card>
        <CardHeader
          icon={Activity}
          title={bi("生产健康巡检", "Production health")}
          description={bi(
            "数据库延迟、Redis 内存、Stream 积压与死信台账；Prometheus 指标见 /metrics。",
            "Database latency, Redis memory, stream backlog and dead letters; Prometheus metrics live on /metrics."
          )}
          actions={
            <>
              {health ? (
                <Badge tone={opsStatusTone(status)} data-testid="ops-status-badge">
                  {opsStatusLabel(status, locale)}
                </Badge>
              ) : (
                <Badge tone="neutral" data-testid="ops-status-badge">
                  {bi("检查中", "Checking")}
                </Badge>
              )}
              <button
                type="button"
                data-testid="ops-refresh-btn"
                onClick={() => void snapshotQuery.refetch()}
                disabled={snapshotQuery.isFetching}
                className="erp-btn rounded-xl border border-gray-200 bg-white text-gray-700 shadow-xs transition-colors hover:bg-gray-50 disabled:opacity-60 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200 dark:hover:bg-gray-800"
              >
                <RefreshCw
                  className={`h-3 w-3 ${snapshotQuery.isFetching ? "animate-spin" : ""}`}
                />
                <span>{bi("刷新", "Refresh")}</span>
              </button>
            </>
          }
        />

        {snapshotQuery.isLoading ? (
          <div className="animate-pulse p-4 text-[13px] font-medium text-gray-500 dark:text-gray-400">
            {bi("正在采集运维指标…", "Collecting operations metrics…")}
          </div>
        ) : snapshotQuery.isError ? (
          <div className="p-4">
            <EmptyState testId="ops-health-error">
              {bi(
                "运维接口不可用：请先确认后端 /readyz 与 /metrics 正常。",
                "Operations endpoint unavailable: confirm the backend /readyz and /metrics first."
              )}
            </EmptyState>
          </div>
        ) : health ? (
          <div className="space-y-3 p-4">
            {(health.alerts ?? []).length > 0 && (
              <ul
                data-testid="ops-alerts"
                className="space-y-1.5 rounded-xl border border-amber-200 bg-amber-50/70 px-3 py-2.5 dark:border-amber-800/60 dark:bg-amber-950/30"
              >
                {health.alerts.map((alert) => (
                  <li
                    key={alert}
                    className="flex items-start gap-1.5 text-[12.5px] leading-5 text-amber-800 dark:text-amber-200"
                  >
                    <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                    <span>{alert}</span>
                  </li>
                ))}
              </ul>
            )}

            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
              <OpsMetric
                tone={health.postgres.status === "ok" ? "neutral" : "danger"}
                icon={<Database className="h-3.5 w-3.5" />}
                label={bi("Postgres 延迟", "Postgres latency")}
                value={formatMilliseconds(health.postgres.latency_ms)}
                testId="ops-db-latency"
                hint={
                  health.postgres.alembic_version
                    ? `alembic ${health.postgres.alembic_version}`
                    : bi("未知版本", "unknown revision")
                }
              />
              <OpsMetric
                tone={health.redis.status === "ok" ? "neutral" : "danger"}
                icon={<HardDrive className="h-3.5 w-3.5" />}
                label={bi("Redis 内存", "Redis memory")}
                value={
                  health.redis.used_memory_human ??
                  bytesToHuman(health.redis.used_memory_bytes)
                }
                testId="ops-redis-memory"
                hint={`PING ${formatMilliseconds(health.redis.latency_ms)}`}
              />
              <OpsMetric
                tone={failed > 0 ? "warning" : "success"}
                icon={<ServerCog className="h-3.5 w-3.5" />}
                label={bi("死信 / 失败事件", "Dead letters / failed")}
                value={failed}
                testId="ops-failed-events"
                hint={
                  health.postgres.notification_deliveries_pending +
                  health.postgres.inventory_sync_jobs_pending >
                  0
                    ? bi(
                        `通知待发 ${health.postgres.notification_deliveries_pending} · 库存同步 ${health.postgres.inventory_sync_jobs_pending}`,
                        `notifications ${health.postgres.notification_deliveries_pending} · inventory ${health.postgres.inventory_sync_jobs_pending}`
                      )
                    : bi("通知与库存队列为空", "Notification and inventory queues are empty")
                }
              />
              <OpsMetric
                tone={health.max_backlog >= health.queue_critical_depth ? "danger" : "neutral"}
                icon={<Activity className="h-3.5 w-3.5" />}
                label={bi("最大 Stream 积压", "Peak stream backlog")}
                value={health.max_backlog}
                testId="ops-max-backlog"
                hint={bi(
                  `warn ≥ ${health.queue_warn_depth} · critical ≥ ${health.queue_critical_depth}`,
                  `warn ≥ ${health.queue_warn_depth} · critical ≥ ${health.queue_critical_depth}`
                )}
              />
            </div>

            <TableWrap>
              <table className="erp-table" data-testid="ops-stream-table">
                <thead>
                  <tr>
                    <th>{bi("Redis Stream", "Redis stream")}</th>
                    <th>{bi("积压条数", "Backlog")}</th>
                    <th>{bi("未确认", "Pending")}</th>
                    <th>{bi("消费者", "Consumers")}</th>
                  </tr>
                </thead>
                <tbody>
                  {(health.streams ?? []).length === 0 ? (
                    <tr>
                      <td colSpan={4} className="text-gray-500 dark:text-gray-400">
                        {bi("暂无 Stream 积压。", "No stream backlog.")}
                      </td>
                    </tr>
                  ) : (
                    health.streams.map((stream) => (
                      <tr key={stream.stream} data-testid="ops-stream-row">
                        <td className="font-semibold text-gray-900 dark:text-gray-100">
                          {stream.stream}
                        </td>
                        <td className="tabular-nums">{stream.length}</td>
                        <td className="tabular-nums">{stream.pending ?? "—"}</td>
                        <td className="tabular-nums">{stream.consumers ?? "—"}</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </TableWrap>
          </div>
        ) : null}
      </Card>

      <Card>
        <CardHeader
          icon={Wallet}
          title={bi("租户计费钱包", "Tenant billing wallet")}
          description={bi(
            "Stripe 闭环充值、额度消耗与降级状态（金额精度由后端 Decimal 保证）。",
            "Stripe closed-loop top-ups, credit consumption and downgrade state — money precision is server-side Decimal."
          )}
          actions={
            wallet ? (
              <Badge tone={wallet.auto_downgrade_to_l0 ? "info" : "warning"}>
                {wallet.auto_downgrade_to_l0 ? "L0 auto-downgrade on" : "overage enabled"}
              </Badge>
            ) : null
          }
        />
        <div className="space-y-3 p-4">
          {!wallet ? (
            <EmptyState testId="ops-wallet-empty">
              {bi("钱包数据未返回。", "No wallet data returned.")}
            </EmptyState>
          ) : (
            <>
              <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
                <OpsMetric
                  tone="neutral"
                  icon={<Wallet className="h-3.5 w-3.5" />}
                  label={bi("套餐", "Plan tier")}
                  value={wallet.plan_tier}
                  testId="ops-wallet-plan"
                  hint={bi(
                    `周期结束 ${wallet.billing_cycle_end}`,
                    `cycle ends ${wallet.billing_cycle_end}`
                  )}
                />
                <OpsMetric
                  tone={wallet.total_credits_available > 0 ? "success" : "danger"}
                  icon={<Activity className="h-3.5 w-3.5" />}
                  label={bi("可用额度", "Credits available")}
                  value={wallet.total_credits_available}
                  testId="ops-wallet-balance"
                  hint={bi(
                    `含赠送 ${wallet.included_credits_remaining} · 充值 ${wallet.topup_credits_balance}`,
                    `included ${wallet.included_credits_remaining} · topup ${wallet.topup_credits_balance}`
                  )}
                />
                <OpsMetric
                  tone="neutral"
                  icon={<Database className="h-3.5 w-3.5" />}
                  label={bi("超量消费", "Overage spend")}
                  value={overageSummary(wallet)}
                  testId="ops-wallet-overage"
                  hint={bi(
                    `小数结转 ${wallet.fractional_credit_carry}`,
                    `fractional carry ${wallet.fractional_credit_carry}`
                  )}
                />
                <OpsMetric
                  tone="neutral"
                  icon={<RefreshCw className="h-3.5 w-3.5" />}
                  label={bi("最近更新时间", "Last updated")}
                  value={wallet.updated_at}
                  testId="ops-wallet-updated"
                  hint={bi("服务端写入时间", "written by the backend")}
                />
              </div>

              <TableWrap>
                <table className="erp-table" data-testid="ops-usage-table">
                  <thead>
                    <tr>
                      <th>{bi("时间", "Time")}</th>
                      <th>{bi("类型", "Metric")}</th>
                      <th>{bi("提供方", "Provider")}</th>
                      <th>{bi("消耗额度", "Units")}</th>
                      <th>{bi("成本 (USD)", "Cost (USD)")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(usage?.items ?? []).length === 0 ? (
                      <tr>
                        <td colSpan={5} className="text-gray-500 dark:text-gray-400">
                          {bi("暂无额度消耗记录。", "No credit consumption yet.")}
                        </td>
                      </tr>
                    ) : (
                      (usage?.items ?? []).map((entry) => (
                        <tr key={entry.id} data-testid="ops-usage-row">
                          <td className="whitespace-nowrap text-gray-600 dark:text-gray-400">
                            {entry.created_at}
                          </td>
                          <td>
                            <span className="rounded-md bg-gray-100 px-1.5 py-0.5 text-[11px] font-medium text-gray-700 dark:bg-gray-800 dark:text-gray-300">
                              {entry.metric_type}
                            </span>
                          </td>
                          <td className="text-gray-600 dark:text-gray-400">
                            {entry.model_or_provider}
                          </td>
                          <td className="tabular-nums">{entry.units_consumed}</td>
                          <td className="tabular-nums">{entry.raw_provider_cost_usd}</td>
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </TableWrap>
            </>
          )}
        </div>
      </Card>
    </div>
  );
}

function OpsMetric({
  label,
  value,
  hint,
  tone,
  icon,
  testId,
}: {
  label: string;
  value: React.ReactNode;
  hint?: React.ReactNode;
  tone: "neutral" | "success" | "warning" | "danger";
  icon?: React.ReactNode;
  testId: string;
}) {
  const toneClass =
    tone === "danger"
      ? "border-rose-200 bg-rose-50/70 dark:border-rose-800/60 dark:bg-rose-950/30"
      : tone === "warning"
        ? "border-amber-200 bg-amber-50/70 dark:border-amber-800/60 dark:bg-amber-950/30"
        : tone === "success"
          ? "border-emerald-200 bg-emerald-50/70 dark:border-emerald-800/60 dark:bg-emerald-950/30"
          : "border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-900";
  return (
    <div className={`min-w-0 rounded-xl border px-3 py-2.5 ${toneClass}`} data-testid={testId}>
      <div className="flex items-center gap-1.5 text-[11px] font-medium text-gray-500 dark:text-gray-400">
        {icon}
        <span>{label}</span>
      </div>
      <div className="mt-1 truncate text-[15px] font-semibold tabular-nums text-gray-900 dark:text-gray-100">
        {value}
      </div>
      {hint && (
        <div className="mt-0.5 truncate text-[11px] text-gray-500 dark:text-gray-400">{hint}</div>
      )}
    </div>
  );
}
