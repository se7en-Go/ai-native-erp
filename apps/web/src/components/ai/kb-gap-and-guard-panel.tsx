"use client";

import React, { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  BookOpen,
  Gauge,
  Loader2,
  Plus,
  ShieldAlert,
  Sparkles,
  Trash2,
  Wand2,
} from "lucide-react";

import { Badge, Card, CardHeader, EmptyState, StatCard, TableWrap } from "@/components/ui/page";
import {
  checkOutboundText,
  createKbGap,
  createRedlineRule,
  deleteRedlineRule,
  dismissKbGap,
  fetchAiAdoptionMetrics,
  fetchKbGaps,
  fetchRedlineRules,
  formatRatePct,
  promoteKbGap,
  validateRedlineDraft,
  type RedlineAction,
  type RedlineDraft,
  type RedlineMatchType,
} from "@/lib/api-client/track6-team-kb";
import type { KbGapRead, RedlineCheckResult } from "@/lib/api-client/types.gen";
import { useI18n } from "@/lib/i18n";

const DRAFT: RedlineDraft = {
  name: "",
  pattern: "",
  match_type: "keyword",
  action: "block",
};

function gapQuestionLabel(gap: KbGapRead, index: number): string {
  const trimmed = gap.customer_question.trim();
  return trimmed.length > 0 ? trimmed : `gap-${index}`;
}

/**
 * AI adoption gauge + outbound red-line guard + knowledge-base gap backlog.
 *
 * Mounted on /ai/proposals. Every number is the server's: the adoption rates are
 * exact decimal percentage strings (`"82.35"`), and the guard decision is
 * evaluated by the backend so a client-side regex difference can never let an
 * outbound message slip past a red line.
 */
export function KbGapAndGuardPanel({ days = 30 }: { days?: number }) {
  const { bi } = useI18n();
  const queryClient = useQueryClient();

  const [draft, setDraft] = useState<RedlineDraft>(DRAFT);
  const [draftError, setDraftError] = useState<string | null>(null);
  const [testText, setTestText] = useState("");
  const [testResult, setTestResult] = useState<RedlineCheckResult | null>(null);

  const [newQuestion, setNewQuestion] = useState("");
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [gapError, setGapError] = useState<string | null>(null);
  const [promotingGapId, setPromotingGapId] = useState<string | null>(null);

  const adoptionQuery = useQuery({
    queryKey: ["track6", "adoption-metrics", days],
    queryFn: () => fetchAiAdoptionMetrics(days),
  });

  const rulesQuery = useQuery({
    queryKey: ["track6", "redline-rules"],
    queryFn: () => fetchRedlineRules(),
  });

  const gapsQuery = useQuery({
    queryKey: ["track6", "kb-gaps"],
    queryFn: () => fetchKbGaps(),
  });

  const rules = rulesQuery.data ?? [];
  const gaps = gapsQuery.data?.items ?? [];
  const adoption = adoptionQuery.data;

  const openGapCount = useMemo(
    () => gaps.filter((gap) => gap.status === "open").length,
    [gaps]
  );
  const blockingRuleCount = useMemo(
    () => rules.filter((rule) => rule.is_enabled && rule.action === "block").length,
    [rules]
  );

  const createRuleMutation = useMutation({
    mutationFn: () => createRedlineRule(draft),
    onSuccess: () => {
      setDraftError(null);
      setDraft(DRAFT);
      void queryClient.invalidateQueries({ queryKey: ["track6", "redline-rules"] });
    },
    onError: (error: unknown) => {
      setDraftError(error instanceof Error ? error.message : String(error));
    },
  });

  const deleteRuleMutation = useMutation({
    mutationFn: (ruleId: string) => deleteRedlineRule(ruleId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["track6", "redline-rules"] });
    },
    onError: (error: unknown) => {
      setDraftError(error instanceof Error ? error.message : String(error));
    },
  });

  const testMutation = useMutation({
    mutationFn: () => checkOutboundText(testText),
    onSuccess: (result) => setTestResult(result),
    onError: (error: unknown) => {
      setDraftError(error instanceof Error ? error.message : String(error));
    },
  });

  const createGapMutation = useMutation({
    mutationFn: () => createKbGap({ customer_question: newQuestion.trim() }),
    onSuccess: () => {
      setGapError(null);
      setNewQuestion("");
      void queryClient.invalidateQueries({ queryKey: ["track6", "kb-gaps"] });
    },
    onError: (error: unknown) => {
      setGapError(error instanceof Error ? error.message : String(error));
    },
  });

  const promoteMutation = useMutation({
    mutationFn: ({ gapId, answer }: { gapId: string; answer: string }) =>
      promoteKbGap(gapId, answer),
    onSuccess: () => {
      setGapError(null);
      setPromotingGapId(null);
      void queryClient.invalidateQueries({ queryKey: ["track6", "kb-gaps"] });
    },
    onError: (error: unknown) => {
      setGapError(error instanceof Error ? error.message : String(error));
    },
  });

  const dismissMutation = useMutation({
    mutationFn: (gapId: string) => dismissKbGap(gapId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["track6", "kb-gaps"] });
    },
    onError: (error: unknown) => {
      setGapError(error instanceof Error ? error.message : String(error));
    },
  });

  const submitDraft = () => {
    const verdict = validateRedlineDraft(draft);
    if (!verdict.ok) {
      setDraftError(verdict.error);
      return;
    }
    createRuleMutation.mutate();
  };

  return (
    <div className="min-w-0 space-y-3" data-testid="kb-gap-and-guard-panel">
      <Card data-testid="adoption-card">
        <CardHeader
          icon={Gauge}
          title={bi("AI 提案采纳率", "AI proposal adoption")}
          description={bi(
            `近 ${days} 天人工审核结果：直接采纳 + 编辑后采纳 = 真正被用起来的 AI 提案。比率由服务端 Decimal 精确计算。`,
            `Human review over the last ${days} days: direct adopt + edited adopt = proposals actually put to work. Rates are computed server-side with exact decimals.`
          )}
          actions={
            <Badge tone={(adoption?.pending ?? 0) > 0 ? "warning" : "success"}>
              {bi("待审核", "Pending")} {adoption?.pending ?? 0}
            </Badge>
          }
        />

        <div className="grid gap-3 p-4 sm:grid-cols-2 xl:grid-cols-4">
          <StatCard
            testId="adoption-total"
            label={bi("提案总数", "Proposals")}
            value={adoption?.total ?? 0}
            tone="neutral"
            icon={Sparkles}
            hint={bi(
              `窗口 ${adoption?.window_days ?? days} 天`,
              `${adoption?.window_days ?? days}-day window`
            )}
          />
          <StatCard
            testId="adoption-adopted"
            label={bi("已采纳", "Adopted")}
            value={adoption?.adopted ?? 0}
            tone="success"
            icon={BookOpen}
            hint={bi(
              `直接 ${adoption?.approved ?? 0} · 编辑 ${adoption?.edited_and_approved ?? 0}`,
              `Direct ${adoption?.approved ?? 0} · edited ${adoption?.edited_and_approved ?? 0}`
            )}
          />
          <StatCard
            testId="adoption-rate"
            label={bi("采纳率", "Adoption rate")}
            value={formatRatePct(adoption?.adoption_rate_pct)}
            tone="info"
            hint={bi(
              `审核基数 ${adoption?.review_base ?? 0}`,
              `${adoption?.review_base ?? 0} reviewed`
            )}
          />
          <StatCard
            testId="adoption-edit-rate"
            label={bi("编辑后采纳占比", "Edited-of-adopted")}
            value={formatRatePct(adoption?.edit_adopt_rate_pct)}
            tone="warning"
            hint={bi(
              `驳回率 ${formatRatePct(adoption?.rejection_rate_pct)}`,
              `Rejected ${formatRatePct(adoption?.rejection_rate_pct)}`
            )}
          />
        </div>

        <div className="px-4 pb-4">
          {(adoption?.by_type ?? []).length === 0 ? (
            <EmptyState testId="adoption-by-type-empty">
              {bi("窗口内还没有提案。", "No proposals in this window.")}
            </EmptyState>
          ) : (
            <TableWrap>
              <table className="erp-table">
                <thead>
                  <tr>
                    <th>{bi("提案类型", "Proposal type")}</th>
                    <th>{bi("总数", "Total")}</th>
                    <th>{bi("采纳", "Adopted")}</th>
                    <th>{bi("待审", "Pending")}</th>
                    <th>{bi("驳回", "Rejected")}</th>
                    <th>{bi("采纳率", "Adoption")}</th>
                  </tr>
                </thead>
                <tbody>
                  {(adoption?.by_type ?? []).map((row) => (
                    <tr key={row.proposal_type} data-testid={`adoption-type-${row.proposal_type}`}>
                      <td className="font-semibold text-gray-900 dark:text-gray-100">
                        {row.proposal_type}
                      </td>
                      <td className="tabular-nums">{row.total}</td>
                      <td className="tabular-nums">
                        {row.approved + row.edited_and_approved}
                      </td>
                      <td className="tabular-nums">{row.pending}</td>
                      <td className="tabular-nums">{row.rejected}</td>
                      <td className="tabular-nums font-semibold">
                        {formatRatePct(row.adoption_rate_pct)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableWrap>
          )}
        </div>
      </Card>

      <Card data-testid="redline-card">
        <CardHeader
          icon={ShieldAlert}
          title={bi("外发红线守卫", "Outbound red-line guard")}
          description={bi(
            "命中 block 规则即拦截（报价口径、押金承诺、禁用词），命中 warn 规则只提醒。实时测试框用同一套服务端规则判定。",
            "A block hit stops the message (price floors, deposit promises, banned wording); a warn hit only flags it. The live tester uses the same server-side rules."
          )}
          actions={
            <Badge tone={blockingRuleCount > 0 ? "danger" : "neutral"}>
              {bi(`${blockingRuleCount} 条拦截规则`, `${blockingRuleCount} blocking`)}
            </Badge>
          }
        />

        <div className="flex flex-wrap items-end gap-2 border-b border-gray-200 p-4 dark:border-gray-800">
          <label className="min-w-[150px] flex-1">
            <span className="mb-1 block text-[11px] font-semibold text-gray-500 dark:text-gray-400">
              {bi("规则名称", "Rule name")}
            </span>
            <input
              data-testid="redline-name"
              value={draft.name}
              onChange={(event) => setDraft({ ...draft, name: event.target.value })}
              placeholder={bi("禁提押金", "No deposit talk")}
              className="w-full rounded-xl border border-gray-200 bg-white px-2.5 py-1.5 text-[12.5px] font-medium text-gray-800 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200"
            />
          </label>
          <label className="min-w-[200px] flex-[2]">
            <span className="mb-1 block text-[11px] font-semibold text-gray-500 dark:text-gray-400">
              {bi("匹配内容（≤200 字符）", "Pattern (≤200 chars)")}
            </span>
            <input
              data-testid="redline-pattern"
              value={draft.pattern}
              onChange={(event) => setDraft({ ...draft, pattern: event.target.value })}
              placeholder="deposit"
              className="w-full rounded-xl border border-gray-200 bg-white px-2.5 py-1.5 font-mono text-[12px] text-gray-800 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200"
            />
          </label>
          <label className="min-w-[110px]">
            <span className="mb-1 block text-[11px] font-semibold text-gray-500 dark:text-gray-400">
              {bi("匹配方式", "Match")}
            </span>
            <select
              data-testid="redline-match-type"
              value={draft.match_type}
              onChange={(event) =>
                setDraft({ ...draft, match_type: event.target.value as RedlineMatchType })
              }
              className="w-full rounded-xl border border-gray-200 bg-white px-2.5 py-1.5 text-[12.5px] font-medium text-gray-800 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200"
            >
              <option value="keyword">{bi("关键词", "Keyword")}</option>
              <option value="regex">{bi("正则", "Regex")}</option>
            </select>
          </label>
          <label className="min-w-[100px]">
            <span className="mb-1 block text-[11px] font-semibold text-gray-500 dark:text-gray-400">
              {bi("命中动作", "Action")}
            </span>
            <select
              data-testid="redline-action"
              value={draft.action}
              onChange={(event) =>
                setDraft({ ...draft, action: event.target.value as RedlineAction })
              }
              className="w-full rounded-xl border border-gray-200 bg-white px-2.5 py-1.5 text-[12.5px] font-medium text-gray-800 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200"
            >
              <option value="block">{bi("拦截", "Block")}</option>
              <option value="warn">{bi("提醒", "Warn")}</option>
            </select>
          </label>
          <button
            type="button"
            data-testid="redline-add"
            disabled={createRuleMutation.isPending}
            onClick={submitDraft}
            className="erp-btn rounded-xl bg-blue-600 text-white shadow-xs transition-colors hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-blue-600 dark:hover:bg-blue-500"
          >
            {createRuleMutation.isPending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Plus className="h-3.5 w-3.5" />
            )}
            <span>{bi("新增规则", "Add rule")}</span>
          </button>
        </div>

        {draftError && (
          <div
            data-testid="redline-error"
            className="mx-4 mt-3 rounded-xl border border-rose-200 bg-rose-50 px-3 py-2 text-[12px] font-medium text-rose-700 dark:border-rose-800/70 dark:bg-rose-950/40 dark:text-rose-300"
          >
            {draftError}
          </div>
        )}

        <div className="space-y-3 p-4">
          {rules.length === 0 ? (
            <EmptyState testId="redline-empty">
              {rulesQuery.isLoading
                ? bi("正在加载红线规则…", "Loading red lines…")
                : bi("还没有红线规则。", "No red-line rules yet.")}
            </EmptyState>
          ) : (
            <ul className="space-y-2">
              {rules.map((rule) => (
                <li
                  key={rule.id}
                  data-testid={`redline-rule-${rule.id}`}
                  className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-gray-200 bg-white px-3 py-2 dark:border-gray-800 dark:bg-gray-900"
                >
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className="text-[13px] font-semibold text-gray-900 dark:text-gray-100">
                        {rule.name}
                      </span>
                      <Badge tone={rule.action === "block" ? "danger" : "warning"}>
                        {rule.action === "block" ? bi("拦截", "Block") : bi("提醒", "Warn")}
                      </Badge>
                      <Badge tone="neutral">
                        {rule.match_type === "regex" ? bi("正则", "Regex") : bi("关键词", "Keyword")}
                      </Badge>
                      {!rule.is_enabled && <Badge tone="neutral">{bi("已停用", "Disabled")}</Badge>}
                    </div>
                    <code className="mt-0.5 block truncate font-mono text-[11.5px] text-gray-500 dark:text-gray-400">
                      {rule.pattern}
                    </code>
                  </div>
                  <button
                    type="button"
                    data-testid={`redline-delete-${rule.id}`}
                    disabled={deleteRuleMutation.isPending}
                    onClick={() => deleteRuleMutation.mutate(rule.id)}
                    className="erp-btn rounded-lg border border-gray-200 bg-white text-[11.5px] font-semibold text-gray-700 transition-colors hover:bg-gray-50 disabled:opacity-50 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300"
                  >
                    <Trash2 className="h-3 w-3" />
                    <span>{bi("删除", "Delete")}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}

          <div className="rounded-xl border border-gray-200 bg-gray-50/70 p-3 dark:border-gray-800 dark:bg-gray-800/40">
            <div className="mb-2 text-[12px] font-semibold text-gray-700 dark:text-gray-300">
              {bi("实时外发文案测试", "Live outbound text test")}
            </div>
            <textarea
              data-testid="redline-test-input"
              value={testText}
              onChange={(event) => setTestText(event.target.value)}
              rows={3}
              placeholder={bi(
                "粘贴准备发给客户的回复，例如：Please wire the deposit first.",
                "Paste the reply you are about to send, e.g. Please wire the deposit first."
              )}
              className="w-full rounded-xl border border-gray-200 bg-white p-2.5 text-[12.5px] text-gray-800 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200"
            />
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <button
                type="button"
                data-testid="redline-test-run"
                disabled={testMutation.isPending || testText.trim().length === 0}
                onClick={() => testMutation.mutate()}
                className="erp-btn rounded-xl bg-gray-900 text-white shadow-xs transition-colors hover:bg-gray-800 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-gray-100 dark:text-gray-900"
              >
                {testMutation.isPending ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Wand2 className="h-3.5 w-3.5" />
                )}
                <span>{bi("检测", "Check")}</span>
              </button>
              {testResult && (
                <div data-testid="redline-test-result" className="flex flex-wrap items-center gap-1.5">
                  <Badge
                    tone={testResult.blocked ? "danger" : testResult.warnings.length > 0 ? "warning" : "success"}
                  >
                    <span data-testid="redline-test-blocked">
                      {testResult.blocked
                        ? bi("已拦截", "Blocked")
                        : bi("可放行", "Passable")}
                    </span>
                  </Badge>
                  {testResult.matched_rules.map((rule) => (
                    <span
                      key={`${rule.id}-${rule.matched_text}`}
                      data-testid={`redline-match-${rule.id}`}
                      className="rounded-md border border-gray-200 bg-white px-1.5 py-0.5 font-mono text-[11px] text-gray-700 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300"
                    >
                      {rule.name}: {rule.matched_text}
                    </span>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
      </Card>

      <Card data-testid="kb-gap-card">
        <CardHeader
          icon={BookOpen}
          title={bi("知识库缺口清单", "Knowledge-base gap list")}
          description={bi(
            "客户问了但知识库里没有的问题。补上答案即可一键转为正式 FAQ 文档（自动切块 + 向量化），并写审计日志。",
            "Questions customers asked that the KB cannot answer. Add the answer and it becomes a real FAQ document (chunked + embedded) with an audit trail."
          )}
          actions={
            <Badge tone={openGapCount > 0 ? "warning" : "success"}>
              {bi(`${openGapCount} 条待补`, `${openGapCount} open`)}
            </Badge>
          }
        />

        <div className="flex flex-wrap items-end gap-2 border-b border-gray-200 p-4 dark:border-gray-800">
          <label className="min-w-[220px] flex-1">
            <span className="mb-1 block text-[11px] font-semibold text-gray-500 dark:text-gray-400">
              {bi("记录客户新问题", "Log a new customer question")}
            </span>
            <input
              data-testid="kb-gap-new-question"
              value={newQuestion}
              onChange={(event) => setNewQuestion(event.target.value)}
              placeholder={bi("Do you ship DDP to Chile?", "Do you ship DDP to Chile?")}
              className="w-full rounded-xl border border-gray-200 bg-white px-2.5 py-1.5 text-[12.5px] font-medium text-gray-800 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200"
            />
          </label>
          <button
            type="button"
            data-testid="kb-gap-new-submit"
            disabled={createGapMutation.isPending || newQuestion.trim().length < 3}
            onClick={() => createGapMutation.mutate()}
            className="erp-btn rounded-xl border border-gray-200 bg-white text-[12px] font-semibold text-gray-800 shadow-xs transition-colors hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200"
          >
            <Plus className="h-3.5 w-3.5" />
            <span>{bi("入清单", "Add to list")}</span>
          </button>
        </div>

        {gapError && (
          <div
            data-testid="kb-gap-error"
            className="mx-4 mt-3 rounded-xl border border-rose-200 bg-rose-50 px-3 py-2 text-[12px] font-medium text-rose-700 dark:border-rose-800/70 dark:bg-rose-950/40 dark:text-rose-300"
          >
            {gapError}
          </div>
        )}

        <div className="space-y-2 p-4">
          {gaps.length === 0 ? (
            <EmptyState testId="kb-gap-empty">
              {gapsQuery.isLoading
                ? bi("正在加载缺口清单…", "Loading gaps…")
                : bi("知识库覆盖良好，暂无缺口。", "No gaps — the KB covers everything so far.")}
            </EmptyState>
          ) : (
            gaps.map((gap, index) => {
              const isOpen = gap.status === "open";
              const answer = answers[gap.id] ?? "";
              return (
                <div
                  key={gap.id}
                  data-testid={`kb-gap-${gap.id}`}
                  className="rounded-xl border border-gray-200 bg-white p-3 dark:border-gray-800 dark:bg-gray-900"
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="min-w-0">
                      <div className="text-[13px] font-semibold text-gray-900 dark:text-gray-100">
                        {gapQuestionLabel(gap, index)}
                      </div>
                      <div className="text-[11.5px] text-gray-500 dark:text-gray-400">
                        {bi("被问到", "Asked")} {gap.frequency_count}
                        {bi(" 次", " times")} · {gap.detected_language.toUpperCase()} · {gap.status}
                      </div>
                    </div>
                    <div className="flex flex-wrap items-center gap-1.5">
                      {gap.promoted_kb_document_id && (
                        <Badge tone="success">
                          {bi("已转知识库", "Promoted to KB")}
                        </Badge>
                      )}
                      {isOpen && (
                        <>
                          <button
                            type="button"
                            data-testid={`kb-gap-promote-${gap.id}`}
                            onClick={() =>
                              setPromotingGapId(promotingGapId === gap.id ? null : gap.id)
                            }
                            className="erp-btn rounded-lg bg-emerald-600 text-[11.5px] font-semibold text-white transition-colors hover:bg-emerald-700"
                          >
                            <Sparkles className="h-3 w-3" />
                            <span>{bi("补答案转知识库", "Promote to knowledge base")}</span>
                          </button>
                          <button
                            type="button"
                            data-testid={`kb-gap-dismiss-${gap.id}`}
                            disabled={dismissMutation.isPending}
                            onClick={() => dismissMutation.mutate(gap.id)}
                            className="erp-btn rounded-lg border border-gray-200 bg-white text-[11.5px] font-semibold text-gray-700 transition-colors hover:bg-gray-50 disabled:opacity-50 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300"
                          >
                            {bi("忽略", "Dismiss")}
                          </button>
                        </>
                      )}
                    </div>
                  </div>

                  {promotingGapId === gap.id && isOpen && (
                    <div className="mt-2 space-y-2">
                      <textarea
                        data-testid={`kb-gap-answer-${gap.id}`}
                        value={answer}
                        onChange={(event) =>
                          setAnswers({ ...answers, [gap.id]: event.target.value })
                        }
                        rows={3}
                        placeholder={bi(
                          "写入标准答案（会被切块并向量化；价格、库存、交期数字会被自动脱敏，读取时一律走实时 ERP 工具）。",
                          "Write the canonical answer (it gets chunked and embedded; price, stock and lead-time numbers are auto-redacted because live values must come from ERP tools)."
                        )}
                        className="w-full rounded-xl border border-gray-200 bg-white p-2.5 text-[12.5px] text-gray-800 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200"
                      />
                      <button
                        type="button"
                        data-testid={`kb-gap-answer-submit-${gap.id}`}
                        disabled={
                          promoteMutation.isPending || (answers[gap.id] ?? "").trim().length < 3
                        }
                        onClick={() =>
                          promoteMutation.mutate({ gapId: gap.id, answer: (answers[gap.id] ?? "").trim() })
                        }
                        className="erp-btn rounded-xl bg-emerald-600 text-white shadow-xs transition-colors hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        {promoteMutation.isPending ? (
                          <Loader2 className="h-3.5 w-3.5 animate-spin" />
                        ) : (
                          <BookOpen className="h-3.5 w-3.5" />
                        )}
                        <span>{bi("确认转为 FAQ 文档", "Create FAQ document")}</span>
                      </button>
                    </div>
                  )}
                </div>
              );
            })
          )}
        </div>
      </Card>
    </div>
  );
}

export default KbGapAndGuardPanel;
