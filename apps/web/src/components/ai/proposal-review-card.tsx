"use client";

import React, { useState } from "react";
import {
  AlertTriangle,
  Check,
  CircleCheck,
  Edit3,
  Send,
  ShieldCheck,
  XCircle,
} from "lucide-react";
import { useI18n } from "@/lib/i18n";
import type { AiCapability, ProposalStatus, ProposalType } from "@/lib/contracts";
import { Badge, Card } from "@/components/ui/page";

export interface AiProposalItem {
  id: string;
  capability: AiCapability;
  proposal_type: ProposalType;
  conversation_id?: string | null;
  proposed_payload: Record<string, unknown>;
  citations: Array<{ doc_id: string; chunk_id: string; score: number }>;
  confidence_score: number;
  estimated_margin_pct?: number | null;
  min_margin_floor_pct?: number | null;
  source_tool_call_id?: string | null;
  status: ProposalStatus;
  created_at: string;
  /** 批准后后端回写：`queued_for_delivery` 为 true 说明已自动排队外发。 */
  execution_result?: { [key: string]: unknown } | null;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

/** 把 `proposed_payload.tool_evidence` 展平成可读的「字段 / 真实值」列表。 */
export function extractToolEvidence(
  payload: Record<string, unknown>,
): Array<{ label: string; value: string }> {
  const raw = payload.tool_evidence;
  if (!Array.isArray(raw)) return [];
  const entries: Array<{ label: string; value: string }> = [];
  for (const item of raw) {
    const record = asRecord(item);
    if (record) {
      for (const [key, value] of Object.entries(record)) {
        if (value === null || value === undefined) continue;
        if (typeof value === "object") continue;
        entries.push({ label: key, value: String(value) });
      }
    } else if (typeof item === "string" || typeof item === "number") {
      entries.push({ label: "evidence", value: String(item) });
    }
  }
  return entries;
}

const CAPABILITY_COPY: Record<AiCapability, { zh: string; en: string }> = {
  cs_support: { zh: "客服支持", en: "CS support" },
  sales_quoting: { zh: "销售报价", en: "Sales & quoting" },
  order_logistics: { zh: "订单物流", en: "Order logistics" },
  content_studio: { zh: "内容工坊", en: "Content studio" },
  biz_analytics: { zh: "经营分析", en: "Business analytics" },
  finance_recon: { zh: "财务对账", en: "Finance recon" },
};

const STATUS_COPY: Record<ProposalStatus, { zh: string; en: string }> = {
  pending: { zh: "待审核", en: "Pending" },
  approved: { zh: "已批准", en: "Approved" },
  edited_and_approved: { zh: "编辑后批准", en: "Edited & approved" },
  rejected: { zh: "已驳回", en: "Rejected" },
  expired: { zh: "已过期", en: "Expired" },
  failed: { zh: "执行失败", en: "Failed" },
};

export function ProposalReviewCard({
  proposal,
  canReview,
  onApprove,
  onEditApprove,
  onReject,
}: {
  proposal: AiProposalItem;
  canReview: boolean;
  onApprove: (id: string) => void;
  onEditApprove: (id: string, editedPayload: Record<string, unknown>) => void;
  onReject: (id: string, reason: string) => void;
}) {
  const { bi } = useI18n();
  const [editing, setEditing] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [jsonDraft, setJsonDraft] = useState(() =>
    JSON.stringify(proposal.proposed_payload, null, 2)
  );
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [rejectionReason, setRejectionReason] = useState("");

  const marginFloor = proposal.min_margin_floor_pct ?? 15.0;
  const belowMarginFloor =
    proposal.estimated_margin_pct !== null &&
    proposal.estimated_margin_pct !== undefined &&
    proposal.estimated_margin_pct < marginFloor;
  const capabilityCopy = CAPABILITY_COPY[proposal.capability] ?? {
    zh: proposal.capability,
    en: proposal.capability,
  };
  const statusCopy = STATUS_COPY[proposal.status] ?? {
    zh: proposal.status,
    en: proposal.status,
  };

  // 确定性工具证据：优先看提案顶层告的 tool call id，再看 payload 里的证据清单。
  const payloadSourceToolCallId =
    typeof proposal.proposed_payload.source_tool_call_id === "string"
      ? proposal.proposed_payload.source_tool_call_id
      : null;
  const sourceToolCallId = proposal.source_tool_call_id ?? payloadSourceToolCallId;
  const toolEvidence = extractToolEvidence(proposal.proposed_payload);
  const hasGroundedEvidence = Boolean(sourceToolCallId) || toolEvidence.length > 0;

  // 批准并已下发客户的证据：execution_result 可能在提案顶层，也可能在 payload 里。
  const executionResult =
    asRecord(proposal.execution_result) ??
    asRecord(proposal.proposed_payload.execution_result);
  const queuedForDelivery = executionResult?.queued_for_delivery === true;
  const isApproved =
    proposal.status === "approved" || proposal.status === "edited_and_approved";

  function submitEdit() {
    try {
      const parsed = JSON.parse(jsonDraft) as Record<string, unknown>;
      setJsonError(null);
      onEditApprove(proposal.id, parsed);
      setEditing(false);
    } catch {
      setJsonError(bi("JSON 格式有误，请检查括号与引号。", "Invalid JSON syntax. Check braces and quotes."));
    }
  }

  return (
    <Card
      data-testid={`proposal-card-${proposal.id}`}
      className="min-w-0 max-w-full overflow-hidden rounded-2xl border border-slate-200/90 p-4 shadow-[0_1px_3px_rgba(15,23,42,0.06),0_6px_16px_-6px_rgba(15,23,42,0.05)] transition-shadow hover:shadow-[0_10px_28px_-16px_rgba(15,23,42,0.25)] dark:border-slate-800 dark:bg-slate-900"
    >
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 flex-wrap items-center gap-1.5 text-[12px]">
          <Badge tone="info">{bi(capabilityCopy.zh, capabilityCopy.en)}</Badge>
          {/* 技术码保留原文：E2E 依赖精确的 quote_draft 文本 */}
          <span className="rounded-md border border-slate-200 bg-slate-50 px-1.5 py-0.5 font-mono text-[11px] font-medium text-slate-700 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300">
            {proposal.proposal_type}
          </span>
          <span className="tabular-nums text-slate-500 dark:text-slate-400">
            {bi("置信度", "Confidence")} {(proposal.confidence_score * 100).toFixed(1)}%
          </span>
          <Badge tone={proposal.status === "pending" ? "warning" : "neutral"}>
            {bi(statusCopy.zh, statusCopy.en)}
          </Badge>
        </div>

        {proposal.estimated_margin_pct !== null && proposal.estimated_margin_pct !== undefined && (
          <div
            data-testid={`proposal-margin-${proposal.id}`}
            className={`inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-[11px] font-medium ${
              belowMarginFloor
                ? "border-rose-200 bg-rose-50 text-rose-700 dark:border-rose-800/70 dark:bg-rose-950/40 dark:text-rose-300"
                : "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-800/70 dark:bg-emerald-950/40 dark:text-emerald-300"
            }`}
          >
            {belowMarginFloor ? (
              <AlertTriangle className="h-3.5 w-3.5" />
            ) : (
              <ShieldCheck className="h-3.5 w-3.5" />
            )}
            <span className="tabular-nums">
              {bi("预估毛利率", "Est. margin")} {proposal.estimated_margin_pct.toFixed(1)}% (
              {bi("底线", "floor")} {marginFloor.toFixed(1)}%)
            </span>
          </div>
        )}
      </div>

      {!editing ? (
        <pre className="mt-3 max-h-72 w-full max-w-full min-w-0 overflow-x-auto overflow-y-auto rounded-xl border border-slate-200 bg-slate-50/70 p-3 font-mono text-[11.5px] leading-5 break-all whitespace-pre-wrap text-slate-800 dark:border-slate-700 dark:bg-slate-950/60 dark:text-slate-200">
          {JSON.stringify(proposal.proposed_payload, null, 2)}
        </pre>
      ) : (
        <div className="mt-3 min-w-0 space-y-2">
          <textarea
            data-testid={`proposal-json-editor-${proposal.id}`}
            rows={6}
            value={jsonDraft}
            onChange={(e) => setJsonDraft(e.target.value)}
            aria-label={bi("编辑提案 JSON", "Edit proposal JSON")}
            className="w-full max-w-full min-w-0 rounded-lg border border-blue-300 bg-white p-2.5 font-mono text-[12px] focus:border-blue-500 focus:outline-none dark:border-blue-700 dark:bg-slate-950 dark:text-slate-100"
          />
          {jsonError && (
            <div className="text-[12px] text-rose-600 dark:text-rose-400">{jsonError}</div>
          )}
          <div className="flex gap-2">
            <button
              type="button"
              data-testid={`proposal-edit-submit-${proposal.id}`}
              onClick={submitEdit}
              className="inline-flex items-center gap-1.5 rounded-xl bg-blue-600 px-3 py-1.5 text-[12.5px] font-semibold text-white shadow-xs transition-colors hover:bg-blue-700 dark:bg-blue-600 dark:hover:bg-blue-500"
            >
              {bi("保存并批准", "Save edit & approve")}
            </button>
            <button
              type="button"
              onClick={() => setEditing(false)}
              className="inline-flex items-center gap-1.5 rounded-xl border border-slate-200 bg-white px-3 py-1.5 text-[12.5px] font-semibold text-slate-700 transition-colors hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-200 dark:hover:bg-slate-800"
            >
              {bi("取消", "Cancel")}
            </button>
          </div>
        </div>
      )}

      {hasGroundedEvidence && (
        <div
          data-testid={`proposal-evidence-${proposal.id}`}
          className="mt-2.5 space-y-1.5"
        >
          <div className="flex flex-wrap items-center gap-1.5">
            <span
              data-testid={`proposal-evidence-badge-${proposal.id}`}
              className="inline-flex items-center gap-1 rounded-full border border-emerald-200 bg-emerald-50 px-2.5 py-0.5 text-[11px] font-semibold text-emerald-800 dark:border-emerald-800/70 dark:bg-emerald-950/40 dark:text-emerald-300"
            >
              <ShieldCheck className="h-3.5 w-3.5" />
              {bi("已核验 ERP 证据", "Verified ERP evidence")}
            </span>
            {sourceToolCallId && (
              <code
                data-testid={`proposal-tool-call-${proposal.id}`}
                className="max-w-full min-w-0 truncate rounded-md border border-emerald-200 bg-white px-1.5 py-0.5 font-mono text-[10.5px] font-medium text-emerald-700 dark:border-emerald-800/70 dark:bg-slate-900 dark:text-emerald-300"
              >
                {sourceToolCallId}
              </code>
            )}
          </div>

          {toolEvidence.length > 0 && (
            <details
              data-testid={`proposal-evidence-preview-${proposal.id}`}
              className="rounded-xl border border-emerald-200 bg-emerald-50/60 px-2.5 py-2 text-[11.5px] text-emerald-950 dark:border-emerald-800/70 dark:bg-emerald-950/30 dark:text-emerald-200"
            >
              <summary className="cursor-pointer select-none font-semibold text-emerald-900 dark:text-emerald-200">
                {bi(
                  `展开确定性工具证据（${toolEvidence.length} 项）`,
                  `Show deterministic tool evidence (${toolEvidence.length})`
                )}
              </summary>
              <ul className="mt-1.5 grid gap-1 sm:grid-cols-2">
                {toolEvidence.map((entry, index) => (
                  <li
                    key={`${entry.label}-${index}-${entry.value}`}
                    className="flex min-w-0 items-baseline justify-between gap-2 rounded-lg bg-white/80 px-2 py-1 dark:bg-slate-900/70"
                  >
                    <span className="min-w-0 truncate font-mono text-[10.5px] text-emerald-700 dark:text-emerald-400">
                      {entry.label}
                    </span>
                    <span className="min-w-0 truncate font-semibold tabular-nums text-emerald-950 dark:text-emerald-200">
                      {entry.value}
                    </span>
                  </li>
                ))}
              </ul>
            </details>
          )}
        </div>
      )}

      {queuedForDelivery && (
        <div
          data-testid={`proposal-delivery-banner-${proposal.id}`}
          className="mt-2.5 flex items-center gap-2 rounded-xl border border-emerald-200 bg-emerald-50 px-3 py-2 text-[12px] font-semibold text-emerald-900 dark:border-emerald-800/70 dark:bg-emerald-950/40 dark:text-emerald-300"
        >
          <Send className="h-3.5 w-3.5 shrink-0 text-emerald-600" />
          <span>
            {bi(
              "已自动加入外发队列，正在发送给客户",
              "Queued for delivery — sending to the customer now"
            )}
          </span>
        </div>
      )}

      {isApproved && !queuedForDelivery && (
        <div
          className="mt-2.5 flex items-center gap-2 rounded-xl border border-emerald-200 bg-white px-3 py-2 text-[12px] font-semibold text-emerald-800 dark:border-emerald-800/70 dark:bg-slate-900 dark:text-emerald-300"
        >
          <CircleCheck className="h-3.5 w-3.5 shrink-0 text-emerald-600" />
          <span>{bi("审核已通过，可直接执行下发", "Approved and ready to execute")}</span>
        </div>
      )}

      {proposal.citations.length > 0 && (
        <div className="mt-2.5 flex min-w-0 flex-wrap items-center gap-1.5">
          <span className="text-[11px] text-slate-400 dark:text-slate-500">
            {bi("证据溯源", "Evidence")}
          </span>
          {proposal.citations.map((cit) => (
            <span
              key={`${cit.doc_id}-${cit.chunk_id}`}
              className="max-w-full rounded-md border border-emerald-200 bg-emerald-50 px-1.5 py-0.5 font-mono text-[10px] font-medium text-emerald-700 tabular-nums dark:border-emerald-800/70 dark:bg-emerald-950/40 dark:text-emerald-300"
            >
              [{cit.doc_id}#{cit.chunk_id}] · {cit.score.toFixed(2)}
            </span>
          ))}
        </div>
      )}

      {proposal.status === "pending" && (
        <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-slate-100 pt-3 dark:border-slate-800">
          <button
            type="button"
            disabled={!canReview || belowMarginFloor}
            data-testid={`proposal-approve-${proposal.id}`}
            onClick={() => onApprove(proposal.id)}
            className="inline-flex items-center gap-1.5 rounded-xl bg-blue-600 px-3 py-1.5 text-[12.5px] font-semibold text-white shadow-xs transition-colors hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-40 dark:bg-blue-600 dark:hover:bg-blue-500"
          >
            <Check className="h-3.5 w-3.5" />
            {/* E2E 以 "Approve" 精确匹配按钮名，保持英文原文 */}
            <span>Approve</span>
          </button>

          <button
            type="button"
            disabled={!canReview}
            data-testid={`proposal-edit-toggle-${proposal.id}`}
            onClick={() => setEditing((prev) => !prev)}
            className="inline-flex items-center gap-1.5 rounded-xl border border-blue-200 bg-blue-50 px-3 py-1.5 text-[12.5px] font-semibold text-blue-700 transition-colors hover:bg-blue-100 disabled:cursor-not-allowed disabled:opacity-40 dark:border-blue-800 dark:bg-blue-950/40 dark:text-blue-300 dark:hover:bg-blue-900/50"
          >
            <Edit3 className="h-3.5 w-3.5" />
            <span>{bi("编辑后批准", "Edit & approve")}</span>
          </button>

          <button
            type="button"
            disabled={!canReview}
            data-testid={`proposal-reject-toggle-${proposal.id}`}
            onClick={() => setRejecting((prev) => !prev)}
            className="inline-flex items-center gap-1.5 rounded-xl border border-rose-200 bg-white px-3 py-1.5 text-[12.5px] font-semibold text-rose-700 transition-colors hover:bg-rose-50 disabled:cursor-not-allowed disabled:opacity-40 dark:border-rose-800 dark:bg-slate-900 dark:text-rose-300 dark:hover:bg-rose-950/40"
          >
            <XCircle className="h-3.5 w-3.5" />
            <span>{bi("驳回", "Reject")}</span>
          </button>

          {belowMarginFloor && (
            <span className="text-[11px] text-rose-600 dark:text-rose-400">
              {bi(
                "预估毛利低于底线，需先编辑价格再批准。",
                "Estimated margin is below the floor; edit the price before approving."
              )}
            </span>
          )}
        </div>
      )}

      {rejecting && (
        <div className="mt-2.5 flex min-w-0 flex-wrap gap-2">
          <input
            type="text"
            data-testid={`proposal-reject-reason-${proposal.id}`}
            value={rejectionReason}
            onChange={(e) => setRejectionReason(e.target.value)}
            placeholder={bi("请填写驳回原因…", "Reason for rejecting…")}
            aria-label={bi("驳回原因", "Rejection reason")}
            className="erp-input min-w-0 flex-1"
          />
          <button
            type="button"
            data-testid={`proposal-reject-submit-${proposal.id}`}
            disabled={!rejectionReason.trim()}
            onClick={() => {
              onReject(proposal.id, rejectionReason.trim());
              setRejecting(false);
            }}
            className="inline-flex items-center gap-1.5 rounded-xl bg-rose-600 px-3 py-1.5 text-[12.5px] font-semibold text-white shadow-xs transition-colors hover:bg-rose-700 disabled:opacity-40"
          >
            {bi("确认驳回", "Confirm reject")}
          </button>
        </div>
      )}
    </Card>
  );
}
