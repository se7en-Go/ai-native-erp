"use client";

// Routing & anti-loss rule builder shown on /orders. Owner/Admin/Manager may create,
// re-prioritise, disable and delete rules; every role can read the active guard set.
import React, { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, Plus, ShieldAlert, Trash2 } from "lucide-react";
import { Badge } from "@/components/ui/page";
import { useAuthSession } from "@/components/providers";
import { useI18n } from "@/lib/i18n";
import {
  EMPTY_ROUTING_RULE_DRAFT,
  createRoutingRule,
  deleteRoutingRule,
  describeRoutingRule,
  fetchRoutingRules,
  routingRuleActionSummary,
  routingRulePayloadFromDraft,
  updateRoutingRule,
  type RoutingRuleDraft,
} from "@/lib/api-client/track5-routing-labels";

const MANAGER_ROLES = new Set(["owner", "admin", "manager"]);

export function RoutingRulesPanel({ defaultOpen = false }: { defaultOpen?: boolean }) {
  const { bi } = useI18n();
  const { principal } = useAuthSession();
  const queryClient = useQueryClient();
  const canManage = MANAGER_ROLES.has(principal?.role ?? "");
  const [open, setOpen] = useState(defaultOpen);
  const [draft, setDraft] = useState<RoutingRuleDraft>(EMPTY_ROUTING_RULE_DRAFT);
  const [error, setError] = useState<string | null>(null);
  const [createdName, setCreatedName] = useState<string | null>(null);

  const rulesQuery = useQuery({
    queryKey: ["orders", "routing-rules"],
    queryFn: fetchRoutingRules,
  });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["orders", "routing-rules"] });
  };

  const createMutation = useMutation({
    mutationFn: createRoutingRule,
    onSuccess: (rule) => {
      setCreatedName(rule.name);
      setDraft(EMPTY_ROUTING_RULE_DRAFT);
      setError(null);
      invalidate();
    },
    onError: (mutationError: Error) => setError(mutationError.message),
  });

  const toggleMutation = useMutation({
    mutationFn: ({ ruleId, isEnabled }: { ruleId: string; isEnabled: boolean }) =>
      updateRoutingRule(ruleId, { is_enabled: isEnabled }),
    onSuccess: invalidate,
    onError: (mutationError: Error) => setError(mutationError.message),
  });

  const deleteMutation = useMutation({
    mutationFn: deleteRoutingRule,
    onSuccess: () => {
      setCreatedName(null);
      invalidate();
    },
    onError: (mutationError: Error) => setError(mutationError.message),
  });

  const rules = rulesQuery.data ?? [];

  const submit = () => {
    const built = routingRulePayloadFromDraft(draft);
    if ("error" in built) {
      setError(built.error);
      return;
    }
    setError(null);
    createMutation.mutate(built.payload);
  };

  const patchDraft = <K extends keyof RoutingRuleDraft>(key: K, value: RoutingRuleDraft[K]) =>
    setDraft((previous) => ({ ...previous, [key]: value }));

  return (
    <section
      data-testid="routing-rules-panel"
      className="erp-card rounded-2xl border border-gray-200 bg-white p-3 dark:border-gray-800 dark:bg-gray-900"
    >
      <header className="flex flex-wrap items-center justify-between gap-2">
        <button
          type="button"
          data-testid="routing-rules-toggle"
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
          className="flex items-center gap-2 text-left"
        >
          {open ? (
            <ChevronDown className="h-4 w-4 text-gray-500 dark:text-gray-400" />
          ) : (
            <ChevronRight className="h-4 w-4 text-gray-500 dark:text-gray-400" />
          )}
          <ShieldAlert className="h-4 w-4 text-blue-600 dark:text-blue-400" />
          <span className="text-[13.5px] font-bold text-gray-900 dark:text-gray-100">
            {bi("审单路由与防亏规则", "Routing & anti-loss rules")}
          </span>
          <Badge>{rules.length}</Badge>
        </button>
        <p className="text-[11.5px] text-gray-500 dark:text-gray-400">
          {bi(
            "按优先级从小到大匹配；毛利率低于下限或为负时自动拦截出单，需主管放行。",
            "Rules run lowest priority number first. A margin below the floor (or negative) holds the order until a manager releases it."
          )}
        </p>
      </header>

      {open && (
        <div className="mt-3 space-y-3">
          {canManage && (
            <div
              data-testid="routing-rule-form"
              className="rounded-xl border border-gray-200 bg-gray-50/70 p-3 dark:border-gray-800 dark:bg-gray-800/40"
            >
              <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
                <Field
                  testId="routing-rule-name"
                  label={bi("规则名称", "Rule name")}
                  value={draft.name}
                  onChange={(value) => patchDraft("name", value)}
                  placeholder={bi("负毛利立即拦截", "Block negative margin")}
                />
                <Field
                  testId="routing-rule-priority"
                  label={bi("优先级（小者优先）", "Priority (lower first)")}
                  value={draft.priority}
                  onChange={(value) => patchDraft("priority", value)}
                  placeholder="10"
                />
                <Field
                  testId="routing-rule-countries"
                  label={bi("目的国（逗号分隔）", "Destination countries (CSV)")}
                  value={draft.destinationCountries}
                  onChange={(value) => patchDraft("destinationCountries", value)}
                  placeholder="SE, NO, DK"
                />
                <Field
                  testId="routing-rule-modes"
                  label={bi("订单模式（逗号分隔）", "Order modes (CSV)")}
                  value={draft.orderModes}
                  onChange={(value) => patchDraft("orderModes", value)}
                  placeholder="b2c_retail"
                />
                <Field
                  testId="routing-rule-margin"
                  label={bi("毛利率下限 %", "Margin floor %")}
                  value={draft.minMarginPct}
                  onChange={(value) => patchDraft("minMarginPct", value)}
                  placeholder="0.00"
                />
                <Field
                  testId="routing-rule-tags"
                  label={bi("附加标签（逗号分隔）", "Tags (CSV)")}
                  value={draft.addTags}
                  onChange={(value) => patchDraft("addTags", value)}
                  placeholder="anti-loss, eu-fast-lane"
                />
                <Field
                  testId="routing-rule-carrier"
                  label={bi("指定承运商", "Carrier")}
                  value={draft.carrierCode}
                  onChange={(value) => patchDraft("carrierCode", value)}
                  placeholder="yunexpress / 4px"
                />
                <Field
                  testId="routing-rule-service"
                  label={bi("指定服务", "Service code")}
                  value={draft.serviceCode}
                  onChange={(value) => patchDraft("serviceCode", value)}
                  placeholder="YunExpress-Expedited"
                />
              </div>
              <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
                <label className="flex items-center gap-2 text-[12px] font-medium text-gray-700 dark:text-gray-300">
                  <input
                    type="checkbox"
                    data-testid="routing-rule-hold"
                    checked={draft.holdForReview}
                    onChange={(event) => patchDraft("holdForReview", event.target.checked)}
                    className="h-3.5 w-3.5 rounded border-gray-300"
                  />
                  <span>{bi("命中即拦截审单", "Hold for review when matched")}</span>
                </label>
                <button
                  type="button"
                  data-testid="routing-rule-submit"
                  disabled={createMutation.isPending}
                  onClick={submit}
                  className="erp-btn rounded-xl bg-blue-600 px-3 py-1 text-[12px] font-semibold text-white hover:bg-blue-700 disabled:opacity-40"
                >
                  <Plus className="h-3 w-3" />
                  <span>
                    {createMutation.isPending
                      ? bi("保存中…", "Saving…")
                      : bi("保存规则", "Save rule")}
                  </span>
                </button>
              </div>
            </div>
          )}

          {error && (
            <p
              role="alert"
              data-testid="routing-rule-error"
              className="rounded-lg border border-rose-200 bg-rose-50 p-2 text-[11.5px] font-medium text-rose-800 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-300"
            >
              {error}
            </p>
          )}
          {createdName && (
            <p
              data-testid="routing-rule-created"
              className="rounded-lg border border-emerald-200 bg-emerald-50 p-2 text-[11.5px] font-medium text-emerald-800 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-300"
            >
              {bi(`已保存规则「${createdName}」`, `Saved rule "${createdName}"`)}
            </p>
          )}

          {rules.length === 0 ? (
            <p
              data-testid="routing-rules-empty"
              className="rounded-lg border border-dashed border-gray-300 p-2 text-[11.5px] text-gray-500 dark:border-gray-700 dark:text-gray-400"
            >
              {rulesQuery.isLoading
                ? bi("正在加载规则…", "Loading rules…")
                : bi(
                    "暂无路由规则。毛利率为负的订单仍会被内置防亏保护拦截。",
                    "No routing rules yet. Negative-margin orders are still held by the built-in anti-loss guard."
                  )}
            </p>
          ) : (
            <ul data-testid="routing-rules-list" className="space-y-1.5">
              {rules.map((rule) => (
                <li
                  key={rule.id}
                  data-testid={`routing-rule-row-${rule.id}`}
                  className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-gray-200 bg-white px-2.5 py-1.5 dark:border-gray-800 dark:bg-gray-900"
                >
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className="text-[12.5px] font-semibold text-gray-900 dark:text-gray-100">
                        {rule.name}
                      </span>
                      <Badge>P{rule.priority}</Badge>
                      <Badge
                        className={
                          rule.is_enabled
                            ? "border-emerald-200 bg-emerald-50 text-emerald-700"
                            : "border-gray-200 bg-gray-100 text-gray-500"
                        }
                      >
                        {rule.is_enabled ? bi("启用", "Enabled") : bi("停用", "Disabled")}
                      </Badge>
                    </div>
                    <p className="mt-0.5 text-[11px] text-gray-600 dark:text-gray-400">
                      {describeRoutingRule(rule)} → {routingRuleActionSummary(rule)}
                    </p>
                  </div>
                  {canManage && (
                    <div className="flex items-center gap-1">
                      <button
                        type="button"
                        data-testid={`routing-rule-toggle-${rule.id}`}
                        disabled={toggleMutation.isPending}
                        onClick={() =>
                          toggleMutation.mutate({
                            ruleId: rule.id,
                            isEnabled: !rule.is_enabled,
                          })
                        }
                        className="rounded-lg border border-gray-200 bg-white px-2 py-0.5 text-[11px] font-semibold text-gray-700 hover:bg-gray-50 dark:border-gray-800 dark:bg-gray-900 dark:text-gray-300"
                      >
                        {rule.is_enabled ? bi("停用", "Disable") : bi("启用", "Enable")}
                      </button>
                      <button
                        type="button"
                        data-testid={`routing-rule-delete-${rule.id}`}
                        disabled={deleteMutation.isPending}
                        onClick={() => deleteMutation.mutate(rule.id)}
                        aria-label={bi(`删除规则 ${rule.name}`, `Delete rule ${rule.name}`)}
                        className="rounded-lg border border-rose-200 bg-rose-50 p-1 text-rose-700 hover:bg-rose-100 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-300"
                      >
                        <Trash2 className="h-3 w-3" />
                      </button>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}

function Field({
  testId,
  label,
  value,
  onChange,
  placeholder,
}: {
  testId: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
}) {
  return (
    <label className="flex flex-col gap-1 text-[11px] font-medium text-gray-600 dark:text-gray-400">
      <span>{label}</span>
      <input
        type="text"
        data-testid={testId}
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
        className="erp-input w-full rounded-lg py-1 text-[12px]"
      />
    </label>
  );
}
