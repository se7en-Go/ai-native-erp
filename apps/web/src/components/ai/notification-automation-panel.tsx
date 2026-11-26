"use client";

import React, { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  Bell,
  CheckCircle2,
  Clock,
  FlaskConical,
  Play,
  Plus,
  RefreshCw,
  Send,
  Sparkles,
  Trash2,
  TriangleAlert,
  Webhook,
} from "lucide-react";

import { Badge, Card, CardHeader, EmptyState, TableWrap } from "@/components/ui/page";
import { field } from "@/components/ai-workforce/presentation";
import {
  DELIVERY_STATUS_LABELS,
  PRIORITY_LABELS,
  PROVIDER_LABELS,
  RULE_COPY,
  RULE_THRESHOLD_FIELD,
  createNotificationChannel,
  deleteNotificationChannel,
  evaluateAutomationRules,
  fetchAutomationRules,
  fetchNotificationChannels,
  fetchNotificationDeliveries,
  seedAutomationRules,
  testNotificationChannel,
  thresholdValue,
  updateAutomationRule,
  updateNotificationChannel,
  type AutomationReport,
  type AutomationTemplateCode,
  type NotificationChannelInput,
  type NotificationPriority,
  type NotificationProvider,
} from "@/lib/api-client/track1-notifications";
import { useI18n } from "@/lib/i18n";

const PROVIDERS: readonly NotificationProvider[] = [
  "feishu",
  "dingtalk",
  "wecom",
  "generic_webhook",
];
const PRIORITIES: readonly NotificationPriority[] = ["P0", "P1", "P2"];
const TEMPLATE_ORDER: readonly AutomationTemplateCode[] = [
  "low_doc_replenishment",
  "negative_margin_hold",
  "stalled_shipment_soothe",
  "cold_b2b_quote_followup",
  "daily_patrol_brief",
  "negative_review_ticket",
  "high_adoption_faq_extract",
];

const EMPTY_FORM: NotificationChannelInput = {
  name: "",
  provider: "feishu",
  webhook_url: "",
  secret: "",
  min_priority: "P1",
  is_enabled: true,
};

function statusTone(
  status: keyof typeof DELIVERY_STATUS_LABELS,
): "success" | "warning" | "danger" | "info" {
  if (status === "sent") return "success";
  if (status === "retrying") return "warning";
  if (status === "failed") return "danger";
  return "info";
}

function priorityTone(priority: NotificationPriority): "danger" | "warning" | "neutral" {
  if (priority === "P0") return "danger";
  if (priority === "P1") return "warning";
  return "neutral";
}

export function NotificationAutomationPanel({ canManage = true }: { canManage?: boolean }) {
  const { bi } = useI18n();
  const queryClient = useQueryClient();

  const [form, setForm] = useState<NotificationChannelInput>(EMPTY_FORM);
  const [formError, setFormError] = useState<string | null>(null);
  const [createdName, setCreatedName] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<
    Record<string, { ok: boolean; detail: string }>
  >({});
  const [report, setReport] = useState<AutomationReport | null>(null);
  const [thresholdDraft, setThresholdDraft] = useState<Record<string, string>>({});
  const [ruleError, setRuleError] = useState<string | null>(null);

  const channelsQuery = useQuery({
    queryKey: ["track1", "notification-channels"],
    queryFn: fetchNotificationChannels,
  });
  const deliveriesQuery = useQuery({
    queryKey: ["track1", "notification-deliveries"],
    queryFn: () => fetchNotificationDeliveries(10),
  });
  const rulesQuery = useQuery({
    queryKey: ["track1", "automation-rules"],
    queryFn: fetchAutomationRules,
  });

  const rules = useMemo(() => rulesQuery.data ?? [], [rulesQuery.data]);
  const ruleByCode = useMemo(() => {
    const map = new Map<AutomationTemplateCode, (typeof rules)[number]>();
    for (const rule of rules) map.set(rule.template_code, rule);
    return map;
  }, [rules]);
  const reportByCode = useMemo(() => {
    const map = new Map<AutomationTemplateCode, AutomationReport["rules"][number]>();
    for (const item of report?.rules ?? []) map.set(item.template_code, item);
    return map;
  }, [report]);

  // Seed the editable threshold inputs the first time the rule rows arrive.
  useEffect(() => {
    if (rules.length === 0) return;
    setThresholdDraft((prev) => {
      const next = { ...prev };
      for (const rule of rules) {
        const fieldSpec = RULE_THRESHOLD_FIELD[rule.template_code];
        if (next[rule.template_code] === undefined) {
          next[rule.template_code] = thresholdValue(rule, fieldSpec.key);
        }
      }
      return next;
    });
  }, [rules]);

  const createChannel = useMutation({
    mutationFn: (input: NotificationChannelInput) => createNotificationChannel(input),
    onSuccess: (channel) => {
      setCreatedName(channel.name);
      setFormError(null);
      setForm(EMPTY_FORM);
      void queryClient.invalidateQueries({ queryKey: ["track1", "notification-channels"] });
    },
    onError: () => {
      setCreatedName(null);
      setFormError(
        bi(
          "创建失败：请确认 Webhook 地址是公网 http(s) 地址，且名称不为空。",
          "Creation failed. Check that the webhook URL is a public http(s) address and the name is not blank.",
        ),
      );
    },
  });

  const toggleChannel = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      updateNotificationChannel(id, { is_enabled: enabled }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["track1", "notification-channels"] });
    },
  });

  const removeChannel = useMutation({
    mutationFn: (id: string) => deleteNotificationChannel(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["track1", "notification-channels"] });
      void queryClient.invalidateQueries({ queryKey: ["track1", "notification-deliveries"] });
    },
  });

  const pingChannel = useMutation({
    mutationFn: (id: string) => testNotificationChannel(id),
    onSuccess: (result, id) => {
      setTestResults((prev) => ({
        ...prev,
        [id]: {
          ok: result.ok,
          detail: result.ok
            ? bi(`测试成功 · HTTP ${result.http_status ?? 200}`, `Test passed · HTTP ${result.http_status ?? 200}`)
            : bi(
                `测试失败：${result.error_message ?? "未知错误"}`,
                `Test failed: ${result.error_message ?? "unknown error"}`,
              ),
        },
      }));
      void queryClient.invalidateQueries({ queryKey: ["track1", "notification-channels"] });
    },
    onError: (_error, id) => {
      setTestResults((prev) => ({
        ...prev,
        [id]: { ok: false, detail: bi("测试请求被拒绝。", "Test request was rejected.") },
      }));
    },
  });

  const seedRules = useMutation({
    mutationFn: () => seedAutomationRules(),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["track1", "automation-rules"] });
    },
  });

  const patchRule = useMutation({
    mutationFn: ({
      id,
      patch,
    }: {
      id: string;
      patch: Parameters<typeof updateAutomationRule>[1];
    }) => updateAutomationRule(id, patch),
    onSuccess: () => {
      setRuleError(null);
      void queryClient.invalidateQueries({ queryKey: ["track1", "automation-rules"] });
    },
    onError: () => {
      setRuleError(
        bi("规则更新失败，请检查阈值是否为合法数字。", "Rule update failed. Check the threshold value."),
      );
    },
  });

  const evaluate = useMutation({
    mutationFn: (dryRun: boolean) => evaluateAutomationRules(dryRun),
    onSuccess: (result) => {
      setReport(result);
      void queryClient.invalidateQueries({ queryKey: ["track1", "automation-rules"] });
      void queryClient.invalidateQueries({ queryKey: ["track1", "notification-deliveries"] });
    },
    onError: () => {
      setRuleError(bi("执行失败，请稍后重试。", "Evaluation failed. Please retry."));
    },
  });

  const formReady = form.name.trim().length > 0 && form.webhook_url.trim().length > 0;
  const channels = channelsQuery.data ?? [];
  const deliveries = deliveriesQuery.data?.items ?? [];
  const error = channelsQuery.error ?? deliveriesQuery.error ?? rulesQuery.error;

  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!formReady) return;
    createChannel.mutate({
      ...form,
      name: form.name.trim(),
      webhook_url: form.webhook_url.trim(),
      secret: form.secret?.trim() ? form.secret.trim() : null,
    });
  }

  function saveThreshold(ruleCode: AutomationTemplateCode) {
    const rule = ruleByCode.get(ruleCode);
    if (!rule) return;
    const fieldSpec = RULE_THRESHOLD_FIELD[ruleCode];
    const raw = (thresholdDraft[ruleCode] ?? "").trim();
    if (!raw) {
      setRuleError(bi("阈值不能为空。", "Threshold must not be empty."));
      return;
    }
    const value = fieldSpec.kind === "int" ? Number(raw) : raw;
    if (fieldSpec.kind === "int" && !Number.isInteger(value)) {
      setRuleError(bi("阈值必须为整数。", "Threshold must be an integer."));
      return;
    }
    patchRule.mutate({
      id: rule.id,
      patch: { config_json: { [fieldSpec.key]: value } },
    });
  }

  return (
    <div data-testid="track1-notification-panel" className="space-y-5">
      {error ? (
        <p
          role="alert"
          data-testid="track1-panel-error"
          className="rounded-2xl border border-rose-200 bg-rose-50 px-4 py-2.5 text-[12.5px] font-medium text-rose-800 dark:border-rose-800/70 dark:bg-rose-950/40 dark:text-rose-300"
        >
          {error.message}
        </p>
      ) : null}

      {/* ---------------------------------------------------------------- */}
      {/* Section 1 - notification channels + delivery log                   */}
      {/* ---------------------------------------------------------------- */}
      <Card>
        <CardHeader
          icon={Bell}
          title={bi("外部通知渠道", "External notification channels")}
          description={bi(
            "飞书 / 钉钉 / 企业微信 / 通用 Webhook 由后台按优先级投递；签名密钥使用信封加密存储，接口只返回掩码预览。",
            "Feishu / DingTalk / WeCom / generic webhooks deliver by priority. Signing secrets are envelope-encrypted and the API only returns a masked preview."
          )}
        />

        {channels.length === 0 ? (
          <EmptyState testId="channel-empty">
            {bi(
              "还没有通知渠道。添加一个飞书或企业微信群，P0/P1 告警才能找到人。",
              "No channel yet. Add a Feishu or WeCom group so P0/P1 alerts reach a human."
            )}
          </EmptyState>
        ) : (
          <ul data-testid="channel-list" className="mt-3 space-y-2">
            {channels.map((channel) => {
              const result = testResults[channel.id];
              const pendingToggle =
                toggleChannel.isPending && toggleChannel.variables?.id === channel.id;
              return (
                <li
                  key={channel.id}
                  data-testid={`channel-row-${channel.id}`}
                  className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-gray-200 bg-white px-3 py-2.5 dark:border-gray-800 dark:bg-gray-900"
                >
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-[13.5px] font-semibold text-gray-900 dark:text-gray-100">
                        {channel.name}
                      </span>
                      <Badge tone="info">{bi(PROVIDER_LABELS[channel.provider].zh, PROVIDER_LABELS[channel.provider].en)}</Badge>
                      <Badge tone={priorityTone(channel.min_priority)}>
                        {bi(
                          PRIORITY_LABELS[channel.min_priority].zh,
                          PRIORITY_LABELS[channel.min_priority].en
                        )}
                      </Badge>
                      <Badge tone={channel.is_enabled ? "success" : "neutral"}>
                        {channel.is_enabled ? bi("启用", "Enabled") : bi("停用", "Disabled")}
                      </Badge>
                      {channel.has_signing_secret ? (
                        <Badge tone="success">{bi("已配置签名密钥", "Signing secret set")}</Badge>
                      ) : null}
                    </div>
                    <p className="mt-1 truncate font-mono text-[11.5px] text-gray-500 dark:text-gray-400">
                      {channel.webhook_url_preview}
                    </p>
                    {channel.last_error ? (
                      <p className="mt-1 text-[11.5px] font-medium text-rose-700 dark:text-rose-400">
                        {channel.last_error}
                      </p>
                    ) : null}
                    {result ? (
                      <p
                        data-testid={`channel-test-result-${channel.id}`}
                        className={`mt-1 text-[11.5px] font-medium ${
                          result.ok
                            ? "text-emerald-700 dark:text-emerald-400"
                            : "text-rose-700 dark:text-rose-400"
                        }`}
                      >
                        {result.detail}
                      </p>
                    ) : null}
                  </div>

                  {canManage ? (
                    <div className="flex flex-wrap items-center gap-2">
                      <button
                        type="button"
                        data-testid={`channel-test-${channel.id}`}
                        disabled={pingChannel.isPending && pingChannel.variables === channel.id}
                        onClick={() => pingChannel.mutate(channel.id)}
                        className="inline-flex items-center gap-1 rounded-lg border border-blue-200 bg-white px-2.5 py-1.5 text-[12px] font-semibold text-blue-700 transition-colors hover:bg-blue-50 disabled:opacity-50 dark:border-blue-800 dark:bg-gray-900 dark:text-blue-300 dark:hover:bg-blue-950/40"
                      >
                        <Send aria-hidden="true" size={13} />
                        {bi("测试 Ping", "Test ping")}
                      </button>
                      <button
                        type="button"
                        data-testid={`channel-toggle-${channel.id}`}
                        disabled={pendingToggle}
                        onClick={() =>
                          toggleChannel.mutate({ id: channel.id, enabled: !channel.is_enabled })
                        }
                        className="rounded-lg border border-gray-200 bg-white px-2.5 py-1.5 text-[12px] font-semibold text-gray-700 transition-colors hover:bg-gray-50 disabled:opacity-50 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200 dark:hover:bg-gray-800"
                      >
                        {channel.is_enabled ? bi("停用", "Disable") : bi("启用", "Enable")}
                      </button>
                      <button
                        type="button"
                        data-testid={`channel-delete-${channel.id}`}
                        disabled={removeChannel.isPending && removeChannel.variables === channel.id}
                        onClick={() => removeChannel.mutate(channel.id)}
                        aria-label={bi(`删除渠道 ${channel.name}`, `Delete channel ${channel.name}`)}
                        className="inline-flex items-center gap-1 rounded-lg border border-rose-200 bg-white px-2.5 py-1.5 text-[12px] font-semibold text-rose-700 transition-colors hover:bg-rose-50 disabled:opacity-50 dark:border-rose-800 dark:bg-gray-900 dark:text-rose-300 dark:hover:bg-rose-950/40"
                      >
                        <Trash2 aria-hidden="true" size={13} />
                        {bi("删除", "Delete")}
                      </button>
                    </div>
                  ) : null}
                </li>
              );
            })}
          </ul>
        )}

        {canManage ? (
          <form
            data-testid="channel-create-form"
            onSubmit={submit}
            className="mt-4 rounded-xl border border-dashed border-gray-200 bg-gray-50/60 p-3 dark:border-gray-700 dark:bg-gray-800/40"
          >
            <p className="mb-2 flex items-center gap-1.5 text-[12.5px] font-semibold text-gray-700 dark:text-gray-200">
              <Plus aria-hidden="true" size={14} />
              {bi("新增通知渠道", "Add notification channel")}
            </p>
            <div className="grid gap-2 sm:grid-cols-2">
              <label className="block text-[12px] font-semibold text-gray-600 dark:text-gray-300">
                {bi("渠道名称", "Channel name")}
                <input
                  data-testid="channel-name-input"
                  value={form.name}
                  onChange={(event) => setForm((prev) => ({ ...prev, name: event.target.value }))}
                  placeholder={bi("运营飞书群", "Ops Feishu group")}
                  className={field}
                  required
                />
              </label>
              <label className="block text-[12px] font-semibold text-gray-600 dark:text-gray-300">
                {bi("渠道类型", "Provider")}
                <select
                  data-testid="channel-provider-select"
                  value={form.provider}
                  onChange={(event) =>
                    setForm((prev) => ({
                      ...prev,
                      provider: event.target.value as NotificationProvider,
                    }))
                  }
                  className={field}
                >
                  {PROVIDERS.map((provider) => (
                    <option key={provider} value={provider}>
                      {bi(PROVIDER_LABELS[provider].zh, PROVIDER_LABELS[provider].en)}
                    </option>
                  ))}
                </select>
              </label>
              <label className="block text-[12px] font-semibold text-gray-600 dark:text-gray-300 sm:col-span-2">
                {bi("Webhook 地址", "Webhook URL")}
                <input
                  data-testid="channel-url-input"
                  value={form.webhook_url}
                  onChange={(event) =>
                    setForm((prev) => ({ ...prev, webhook_url: event.target.value }))
                  }
                  placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/..."
                  className={field}
                  required
                />
              </label>
              <label className="block text-[12px] font-semibold text-gray-600 dark:text-gray-300">
                {bi("签名密钥（可选）", "Signing secret (optional)")}
                <input
                  data-testid="channel-secret-input"
                  type="password"
                  autoComplete="off"
                  value={form.secret ?? ""}
                  onChange={(event) => setForm((prev) => ({ ...prev, secret: event.target.value }))}
                  placeholder={bi("仅加密存储，不再回显", "Encrypted at rest, never echoed")}
                  className={field}
                />
              </label>
              <label className="block text-[12px] font-semibold text-gray-600 dark:text-gray-300">
                {bi("最低接收优先级", "Minimum priority")}
                <select
                  data-testid="channel-priority-select"
                  value={form.min_priority}
                  onChange={(event) =>
                    setForm((prev) => ({
                      ...prev,
                      min_priority: event.target.value as NotificationPriority,
                    }))
                  }
                  className={field}
                >
                  {PRIORITIES.map((priority) => (
                    <option key={priority} value={priority}>
                      {bi(PRIORITY_LABELS[priority].zh, PRIORITY_LABELS[priority].en)}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
              <p className="text-[11.5px] text-gray-500 dark:text-gray-400">
                {bi(
                  "P0 只收紧急告警，P1 收 P0+P1，P2 全收。仅公网 http(s) 地址可用。",
                  "P0 receives urgent alerts only, P1 receives P0+P1, P2 receives everything. Only public http(s) URLs are accepted."
                )}
              </p>
              <button
                type="submit"
                data-testid="channel-create-submit"
                disabled={!formReady || createChannel.isPending}
                className="rounded-xl bg-blue-600 px-3.5 py-2 text-[13px] font-semibold text-white transition-colors hover:bg-blue-700 disabled:opacity-50 dark:bg-blue-600 dark:hover:bg-blue-500"
              >
                {createChannel.isPending
                  ? bi("创建中…", "Creating…")
                  : bi("添加渠道", "Add channel")}
              </button>
            </div>
            {formError ? (
              <p
                role="alert"
                data-testid="channel-create-error"
                className="mt-2 text-[12px] font-medium text-rose-700 dark:text-rose-400"
              >
                {formError}
              </p>
            ) : null}
            {createdName ? (
              <p
                data-testid="channel-create-success"
                role="status"
                className="mt-2 text-[12px] font-medium text-emerald-700 dark:text-emerald-400"
              >
                {bi(`已添加渠道：${createdName}`, `Channel added: ${createdName}`)}
              </p>
            ) : null}
          </form>
        ) : (
          <p
            data-testid="channel-read-only"
            className="mt-3 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-[12px] font-medium text-amber-900 dark:border-amber-800/70 dark:bg-amber-950/40 dark:text-amber-300"
          >
            {bi(
              "当前角色可查看渠道与投递日志，只有 Owner / Admin / Manager 可以增删改。",
              "Your role can read channels and the delivery log; only Owner / Admin / Manager can change them."
            )}
          </p>
        )}

        <div className="mt-5">
          <div className="mb-2 flex items-center justify-between gap-2">
            <h3 className="flex items-center gap-1.5 text-[13px] font-semibold text-gray-800 dark:text-gray-100">
              <Activity aria-hidden="true" size={14} className="text-blue-600 dark:text-blue-400" />
              {bi("最近投递日志", "Recent delivery log")}
            </h3>
            <button
              type="button"
              data-testid="delivery-refresh"
              onClick={() => {
                void deliveriesQuery.refetch();
              }}
              className="inline-flex items-center gap-1 rounded-lg border border-gray-200 bg-white px-2 py-1 text-[11.5px] font-semibold text-gray-600 hover:bg-gray-50 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300 dark:hover:bg-gray-800"
            >
              <RefreshCw aria-hidden="true" size={12} />
              {bi("刷新", "Refresh")}
            </button>
          </div>
          {deliveries.length === 0 ? (
            <EmptyState testId="delivery-empty">
              {bi("还没有投递记录。", "No delivery yet.")}
            </EmptyState>
          ) : (
            <TableWrap>
              <table data-testid="delivery-log" className="w-full min-w-[640px] text-left">
                <thead className="bg-gray-50 text-[11.5px] uppercase tracking-wide text-gray-500 dark:bg-gray-800/60 dark:text-gray-400">
                  <tr>
                    <th className="px-3 py-2 font-semibold">{bi("标题", "Title")}</th>
                    <th className="px-3 py-2 font-semibold">{bi("渠道", "Channel")}</th>
                    <th className="px-3 py-2 font-semibold">{bi("优先级", "Priority")}</th>
                    <th className="px-3 py-2 font-semibold">{bi("状态", "Status")}</th>
                    <th className="px-3 py-2 font-semibold">{bi("尝试", "Attempts")}</th>
                    <th className="px-3 py-2 font-semibold">HTTP</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100 text-[12.5px] dark:divide-gray-800">
                  {deliveries.map((delivery) => (
                    <tr key={delivery.id} data-testid={`delivery-row-${delivery.id}`}>
                      <td className="max-w-[280px] truncate px-3 py-2 font-medium text-gray-800 dark:text-gray-100">
                        {delivery.title}
                      </td>
                      <td className="px-3 py-2 text-gray-600 dark:text-gray-300">
                        {delivery.channel_name}
                      </td>
                      <td className="px-3 py-2">
                        <Badge tone={priorityTone(delivery.priority)}>{delivery.priority}</Badge>
                      </td>
                      <td className="px-3 py-2">
                        <Badge tone={statusTone(delivery.status)}>
                          {bi(
                            DELIVERY_STATUS_LABELS[delivery.status].zh,
                            DELIVERY_STATUS_LABELS[delivery.status].en
                          )}
                        </Badge>
                      </td>
                      <td className="px-3 py-2 tabular-nums text-gray-600 dark:text-gray-300">
                        {delivery.attempts}
                      </td>
                      <td className="px-3 py-2 tabular-nums text-gray-600 dark:text-gray-300">
                        {delivery.http_status ?? "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableWrap>
          )}
        </div>
      </Card>

      {/* ---------------------------------------------------------------- */}
      {/* Section 2 - event automation rules                                 */}
      {/* ---------------------------------------------------------------- */}
      <Card>
        <CardHeader
          icon={Sparkles}
          title={bi("事件自动化规则", "Event automation rules")}
          description={bi(
            "五条规则全部读取租户真实数据，命中即生成 L1 待审提案并按优先级推送。Dry-Run 只读不写。",
            "All five rules read real tenant rows and draft L1 proposals plus priority-pushed alerts. Dry-run writes nothing."
          )}
          actions={
            canManage ? (
              <button
                type="button"
                data-testid="automation-seed-defaults"
                disabled={seedRules.isPending}
                onClick={() => seedRules.mutate()}
                className="inline-flex items-center gap-1 rounded-lg border border-blue-200 bg-white px-2.5 py-1.5 text-[12px] font-semibold text-blue-700 hover:bg-blue-50 disabled:opacity-50 dark:border-blue-800 dark:bg-gray-900 dark:text-blue-300 dark:hover:bg-blue-950/40"
              >
                <Plus aria-hidden="true" size={13} />
                {bi("初始化默认规则", "Seed defaults")}
              </button>
            ) : null
          }
        />

        {canManage ? (
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <button
              type="button"
              data-testid="rule-dry-run"
              disabled={evaluate.isPending}
              onClick={() => evaluate.mutate(true)}
              className="inline-flex items-center gap-1 rounded-lg border border-gray-200 bg-white px-2.5 py-1.5 text-[12px] font-semibold text-gray-700 hover:bg-gray-50 disabled:opacity-50 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200 dark:hover:bg-gray-800"
            >
              <FlaskConical aria-hidden="true" size={13} />
              {bi("Dry-Run 试算", "Dry run")}
            </button>
            <button
              type="button"
              data-testid="rule-execute"
              disabled={evaluate.isPending}
              onClick={() => evaluate.mutate(false)}
              className="inline-flex items-center gap-1 rounded-lg bg-blue-600 px-2.5 py-1.5 text-[12px] font-semibold text-white hover:bg-blue-700 disabled:opacity-50 dark:bg-blue-600 dark:hover:bg-blue-500"
            >
              <Play aria-hidden="true" size={13} />
              {bi("立即执行", "Execute now")}
            </button>
            {evaluate.isPending ? (
              <span className="text-[11.5px] text-gray-500 dark:text-gray-400">
                {bi("正在扫描真实数据…", "Scanning live data…")}
              </span>
            ) : null}
          </div>
        ) : null}

        {report ? (
          <div
            data-testid="automation-report"
            role="status"
            className={`mt-3 rounded-xl border px-3 py-2.5 text-[12.5px] ${
              report.dry_run
                ? "border-blue-200 bg-blue-50 text-blue-900 dark:border-blue-800/70 dark:bg-blue-950/40 dark:text-blue-200"
                : "border-emerald-200 bg-emerald-50 text-emerald-900 dark:border-emerald-800/70 dark:bg-emerald-950/40 dark:text-emerald-200"
            }`}
          >
            <p className="font-semibold">
              {report.dry_run
                ? bi("Dry-Run 结果（未写入数据库）", "Dry-run result (nothing written)")
                : bi("执行结果", "Execution result")}
            </p>
            <p className="mt-0.5">
              {bi(
                `命中 ${report.total_matched} 项 · 新建提案 ${report.total_proposals_created} 条 · 排队通知 ${report.total_notifications_queued} 条`,
                `${report.total_matched} matched · ${report.total_proposals_created} proposals · ${report.total_notifications_queued} notifications queued`
              )}
            </p>
          </div>
        ) : null}

        {ruleError ? (
          <p
            role="alert"
            data-testid="rule-error"
            className="mt-2 rounded-xl border border-rose-200 bg-rose-50 px-3 py-2 text-[12px] font-medium text-rose-800 dark:border-rose-800/70 dark:bg-rose-950/40 dark:text-rose-300"
          >
            {ruleError}
          </p>
        ) : null}

        {rules.length === 0 ? (
          <EmptyState testId="rule-empty">
            {bi(
              "还没有自动化规则。点击「初始化默认规则」创建五条默认模板。",
              "No automation rule yet. Use “Seed defaults” to create the five templates."
            )}
          </EmptyState>
        ) : (
          <div className="mt-3 grid gap-3 lg:grid-cols-2">
            {TEMPLATE_ORDER.map((code) => {
              const rule = ruleByCode.get(code);
              if (!rule) return null;
              const copy = RULE_COPY[code];
              const fieldSpec = RULE_THRESHOLD_FIELD[code];
              const evaluation = reportByCode.get(code);
              return (
                <div
                  key={rule.id}
                  data-testid={`rule-card-${code}`}
                  className="rounded-xl border border-gray-200 bg-white p-3 dark:border-gray-800 dark:bg-gray-900"
                >
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div className="min-w-0">
                      <h4 className="flex items-center gap-1.5 text-[13.5px] font-semibold text-gray-900 dark:text-gray-100">
                        {bi(copy.zh, copy.en)}
                        <Badge tone={priorityTone(rule.priority)}>{rule.priority}</Badge>
                        <Badge tone={rule.is_enabled ? "success" : "neutral"}>
                          {rule.is_enabled ? bi("启用", "Enabled") : bi("停用", "Disabled")}
                        </Badge>
                      </h4>
                      <p className="mt-1 text-[12px] leading-5 text-gray-500 dark:text-gray-400">
                        {bi(copy.description_zh, copy.description_en)}
                      </p>
                    </div>
                    {canManage ? (
                      <button
                        type="button"
                        data-testid={`rule-toggle-${code}`}
                        onClick={() =>
                          patchRule.mutate({
                            id: rule.id,
                            patch: { is_enabled: !rule.is_enabled },
                          })
                        }
                        className="rounded-lg border border-gray-200 bg-white px-2 py-1 text-[11.5px] font-semibold text-gray-700 hover:bg-gray-50 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200 dark:hover:bg-gray-800"
                      >
                        {rule.is_enabled ? bi("停用", "Disable") : bi("启用", "Enable")}
                      </button>
                    ) : null}
                  </div>

                  <div className="mt-2 flex flex-wrap items-end gap-2">
                    <label className="text-[11.5px] font-semibold text-gray-600 dark:text-gray-300">
                      {bi(fieldSpec.zh, fieldSpec.en)}
                      <input
                        data-testid={`rule-threshold-${code}`}
                        value={thresholdDraft[code] ?? thresholdValue(rule, fieldSpec.key)}
                        onChange={(event) =>
                          setThresholdDraft((prev) => ({ ...prev, [code]: event.target.value }))
                        }
                        inputMode={fieldSpec.kind === "int" ? "numeric" : "decimal"}
                        className={`${field} mt-0.5 w-28 py-1.5`}
                        disabled={!canManage}
                      />
                    </label>
                    <label className="text-[11.5px] font-semibold text-gray-600 dark:text-gray-300">
                      {bi("优先级", "Priority")}
                      <select
                        data-testid={`rule-priority-${code}`}
                        value={rule.priority}
                        onChange={(event) =>
                          patchRule.mutate({
                            id: rule.id,
                            patch: { priority: event.target.value as NotificationPriority },
                          })
                        }
                        className={`${field} mt-0.5 w-28 py-1.5`}
                        disabled={!canManage}
                      >
                        {PRIORITIES.map((priority) => (
                          <option key={priority} value={priority}>
                            {priority}
                          </option>
                        ))}
                      </select>
                    </label>
                    {canManage ? (
                      <button
                        type="button"
                        data-testid={`rule-save-threshold-${code}`}
                        onClick={() => saveThreshold(code)}
                        disabled={patchRule.isPending}
                        className="rounded-lg border border-blue-200 bg-white px-2 py-1.5 text-[11.5px] font-semibold text-blue-700 hover:bg-blue-50 disabled:opacity-50 dark:border-blue-800 dark:bg-gray-900 dark:text-blue-300 dark:hover:bg-blue-950/40"
                      >
                        {bi("保存阈值", "Save threshold")}
                      </button>
                    ) : null}
                  </div>

                  <div className="mt-2 flex flex-wrap items-center gap-3 text-[11.5px] text-gray-500 dark:text-gray-400">
                    <span className="inline-flex items-center gap-1">
                      <Clock aria-hidden="true" size={12} />
                      {rule.last_run_at
                        ? bi(
                            `上次运行 ${rule.last_run_at.slice(0, 16).replace("T", " ")}`,
                            `Last run ${rule.last_run_at.slice(0, 16).replace("T", " ")}`
                          )
                        : bi("尚未运行", "Never run")}
                    </span>
                    <span data-testid={`rule-matched-${code}`} className="font-semibold">
                      {evaluation
                        ? bi(
                            `本次命中 ${evaluation.matched_count} · 提案 ${evaluation.proposals_created} · 通知 ${evaluation.notifications_queued}`,
                            `${evaluation.matched_count} matched · ${evaluation.proposals_created} proposals · ${evaluation.notifications_queued} notifications`
                          )
                        : bi(
                            `上次命中 ${rule.last_triggered_count}`,
                            `Last matched ${rule.last_triggered_count}`
                          )}
                    </span>
                    <a
                      href={copy.href}
                      className="inline-flex items-center gap-1 font-semibold text-blue-700 underline decoration-blue-300 underline-offset-2 dark:text-blue-300"
                    >
                      <Webhook aria-hidden="true" size={12} />
                      {bi("查看业务页", "Open workspace")}
                    </a>
                  </div>

                  {evaluation?.items_preview?.length ? (
                    <ul className="mt-2 space-y-0.5 text-[11.5px] text-gray-600 dark:text-gray-400">
                      {evaluation.items_preview.slice(0, 3).map((item, index) => (
                        <li key={index} className="truncate">
                          ·{" "}
                          {Object.entries(item)
                            .slice(0, 3)
                            .map(([key, value]) => `${key}=${String(value)}`)
                            .join(" ")}
                        </li>
                      ))}
                    </ul>
                  ) : null}
                </div>
              );
            })}
          </div>
        )}

        <p className="mt-3 flex items-start gap-1.5 text-[11.5px] leading-5 text-gray-500 dark:text-gray-400">
          {report && !report.dry_run ? (
            <CheckCircle2 aria-hidden="true" size={13} className="mt-0.5 text-emerald-600" />
          ) : (
            <TriangleAlert aria-hidden="true" size={13} className="mt-0.5 text-amber-500" />
          )}
          {bi(
            "自动化只创建 L1 待审提案与通知，不会自动改价、退款或对外发信。",
            "Automation only drafts L1 proposals and notifications; it never reprices, refunds or messages buyers."
          )}
        </p>
      </Card>
    </div>
  );
}

export default NotificationAutomationPanel;
