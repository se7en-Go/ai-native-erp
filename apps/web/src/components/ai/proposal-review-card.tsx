"use client";

import React, { useState } from "react";
import { AlertTriangle, Check, Edit3, ShieldCheck, XCircle } from "lucide-react";
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
      className="rounded-2xl border border-slate-200/90 p-4 shadow-[0_1px_3px_rgba(15,23,42,0.06),0_6px_16px_-6px_rgba(15,23,42,0.05)] transition-shadow hover:shadow-[0_10px_28px_-16px_rgba(15,23,42,0.25)]"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-1.5 text-[12px]">
          <Badge tone="info">{bi(capabilityCopy.zh, capabilityCopy.en)}</Badge>
          {/* 技术码保留原文：E2E 依赖精确的 quote_draft 文本 */}
          <span className="rounded-md border border-slate-200 bg-slate-50 px-1.5 py-0.5 font-mono text-[11px] font-medium text-slate-700">
            {proposal.proposal_type}
          </span>
          <span className="tabular-nums text-slate-500">
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
                ? "border-rose-200 bg-rose-50 text-rose-700"
                : "border-emerald-200 bg-emerald-50 text-emerald-700"
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
        <pre className="mt-3 max-h-72 overflow-auto rounded-xl border border-slate-200 bg-slate-50/70 p-3 text-[11.5px] leading-5 text-slate-800">
          {JSON.stringify(proposal.proposed_payload, null, 2)}
        </pre>
      ) : (
        <div className="mt-3 space-y-2">
          <textarea
            data-testid={`proposal-json-editor-${proposal.id}`}
            rows={6}
            value={jsonDraft}
            onChange={(e) => setJsonDraft(e.target.value)}
            aria-label={bi("编辑提案 JSON", "Edit proposal JSON")}
            className="w-full rounded-lg border border-blue-300 bg-white p-2.5 font-mono text-[12px] focus:border-blue-500 focus:outline-none"
          />
          {jsonError && <div className="text-[12px] text-rose-600">{jsonError}</div>}
          <div className="flex gap-2">
            <button
              type="button"
              data-testid={`proposal-edit-submit-${proposal.id}`}
              onClick={submitEdit}
              className="inline-flex items-center gap-1.5 rounded-xl bg-blue-600 px-3 py-1.5 text-[12.5px] font-semibold text-white shadow-xs transition-colors hover:bg-blue-700"
            >
              {bi("保存并批准", "Save edit & approve")}
            </button>
            <button
              type="button"
              onClick={() => setEditing(false)}
              className="inline-flex items-center gap-1.5 rounded-xl border border-slate-200 bg-white px-3 py-1.5 text-[12.5px] font-semibold text-slate-700 transition-colors hover:bg-slate-50"
            >
              {bi("取消", "Cancel")}
            </button>
          </div>
        </div>
      )}

      {proposal.citations.length > 0 && (
        <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
          <span className="text-[11px] text-slate-400">{bi("证据溯源", "Evidence")}</span>
          {proposal.citations.map((cit) => (
            <span
              key={`${cit.doc_id}-${cit.chunk_id}`}
              className="rounded-md border border-emerald-200 bg-emerald-50 px-1.5 py-0.5 font-mono text-[10px] font-medium text-emerald-700 tabular-nums"
            >
              [{cit.doc_id}#{cit.chunk_id}] · {cit.score.toFixed(2)}
            </span>
          ))}
        </div>
      )}

      {proposal.status === "pending" && (
        <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-slate-100 pt-3">
          <button
            type="button"
            disabled={!canReview || belowMarginFloor}
            data-testid={`proposal-approve-${proposal.id}`}
            onClick={() => onApprove(proposal.id)}
            className="inline-flex items-center gap-1.5 rounded-xl bg-emerald-600 px-3 py-1.5 text-[12.5px] font-semibold text-white shadow-xs transition-colors hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-40"
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
            className="inline-flex items-center gap-1.5 rounded-xl border border-blue-200 bg-blue-50 px-3 py-1.5 text-[12.5px] font-semibold text-blue-700 transition-colors hover:bg-blue-100 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <Edit3 className="h-3.5 w-3.5" />
            <span>{bi("编辑后批准", "Edit & approve")}</span>
          </button>

          <button
            type="button"
            disabled={!canReview}
            data-testid={`proposal-reject-toggle-${proposal.id}`}
            onClick={() => setRejecting((prev) => !prev)}
            className="inline-flex items-center gap-1.5 rounded-xl border border-rose-200 bg-white px-3 py-1.5 text-[12.5px] font-semibold text-rose-700 transition-colors hover:bg-rose-50 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <XCircle className="h-3.5 w-3.5" />
            <span>{bi("驳回", "Reject")}</span>
          </button>

          {belowMarginFloor && (
            <span className="text-[11px] text-rose-600">
              {bi(
                "预估毛利低于底线，需先编辑价格再批准。",
                "Estimated margin is below the floor; edit the price before approving."
              )}
            </span>
          )}
        </div>
      )}

      {rejecting && (
        <div className="mt-2.5 flex flex-wrap gap-2">
          <input
            type="text"
            data-testid={`proposal-reject-reason-${proposal.id}`}
            value={rejectionReason}
            onChange={(e) => setRejectionReason(e.target.value)}
            placeholder={bi("请填写驳回原因…", "Reason for rejecting…")}
            aria-label={bi("驳回原因", "Rejection reason")}
            className="erp-input flex-1"
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
