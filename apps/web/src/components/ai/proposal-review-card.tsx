"use client";

import React, { useState } from "react";
import { Check, Edit3, XCircle, AlertTriangle, ShieldCheck } from "lucide-react";
import type { AiCapability, ProposalStatus, ProposalType } from "@/lib/contracts";

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

  function submitEdit() {
    try {
      const parsed = JSON.parse(jsonDraft) as Record<string, unknown>;
      setJsonError(null);
      onEditApprove(proposal.id, parsed);
      setEditing(false);
    } catch {
      setJsonError("Invalid JSON syntax. Please check braces and quotes.");
    }
  }

  return (
    <div
      data-testid={`proposal-card-${proposal.id}`}
      className="rounded-lg border border-slate-200 bg-white p-4 shadow-xs"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <span className="rounded bg-indigo-50 px-2 py-0.5 font-semibold text-indigo-700">
            {proposal.capability}
          </span>
          <span className="rounded bg-slate-100 px-2 py-0.5 font-medium text-slate-800">
            {proposal.proposal_type}
          </span>
          <span className="text-slate-500">
            Confidence: {(proposal.confidence_score * 100).toFixed(1)}%
          </span>
          <span className="rounded bg-slate-100 px-2 py-0.5 text-slate-600">
            {proposal.status}
          </span>
        </div>

        {proposal.estimated_margin_pct !== null && proposal.estimated_margin_pct !== undefined && (
          <div
            data-testid={`proposal-margin-${proposal.id}`}
            className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-semibold ${
              belowMarginFloor
                ? "bg-rose-100 text-rose-800"
                : "bg-emerald-100 text-emerald-800"
            }`}
          >
            {belowMarginFloor ? (
              <AlertTriangle className="h-3.5 w-3.5" />
            ) : (
              <ShieldCheck className="h-3.5 w-3.5" />
            )}
            <span>
              Est. Margin: {proposal.estimated_margin_pct.toFixed(1)}% (Floor:{" "}
              {marginFloor.toFixed(1)}%)
            </span>
          </div>
        )}
      </div>

      {!editing ? (
        <pre className="mt-3 overflow-x-auto rounded-md bg-slate-50 p-3 text-xs text-slate-800">
          {JSON.stringify(proposal.proposed_payload, null, 2)}
        </pre>
      ) : (
        <div className="mt-3 space-y-2">
          <textarea
            data-testid={`proposal-json-editor-${proposal.id}`}
            rows={6}
            value={jsonDraft}
            onChange={(e) => setJsonDraft(e.target.value)}
            className="w-full rounded-md border border-indigo-300 bg-white p-2.5 font-mono text-xs"
          />
          {jsonError && <div className="text-xs text-rose-600">{jsonError}</div>}
          <div className="flex gap-2">
            <button
              type="button"
              data-testid={`proposal-edit-submit-${proposal.id}`}
              onClick={submitEdit}
              className="rounded-md bg-indigo-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-indigo-700"
            >
              Save Edit & Approve
            </button>
            <button
              type="button"
              onClick={() => setEditing(false)}
              className="rounded-md border border-slate-300 px-3 py-1.5 text-xs font-medium"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {proposal.citations.length > 0 && (
        <div className="mt-2.5 flex flex-wrap gap-1.5">
          {proposal.citations.map((cit) => (
            <span
              key={`${cit.doc_id}-${cit.chunk_id}`}
              className="rounded bg-emerald-50 px-2 py-0.5 text-[11px] font-medium text-emerald-800"
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
            className="inline-flex items-center gap-1 rounded-md bg-emerald-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-emerald-700 disabled:opacity-50"
          >
            <Check className="h-3.5 w-3.5" />
            <span>Approve</span>
          </button>

          <button
            type="button"
            disabled={!canReview}
            data-testid={`proposal-edit-toggle-${proposal.id}`}
            onClick={() => setEditing((prev) => !prev)}
            className="inline-flex items-center gap-1 rounded-md border border-indigo-300 bg-indigo-50 px-3 py-1.5 text-xs font-semibold text-indigo-700 hover:bg-indigo-100 disabled:opacity-50"
          >
            <Edit3 className="h-3.5 w-3.5" />
            <span>Edit & Approve</span>
          </button>

          <button
            type="button"
            disabled={!canReview}
            data-testid={`proposal-reject-toggle-${proposal.id}`}
            onClick={() => setRejecting((prev) => !prev)}
            className="inline-flex items-center gap-1 rounded-md border border-rose-300 px-3 py-1.5 text-xs font-semibold text-rose-700 hover:bg-rose-50 disabled:opacity-50"
          >
            <XCircle className="h-3.5 w-3.5" />
            <span>Reject</span>
          </button>
        </div>
      )}

      {rejecting && (
        <div className="mt-2.5 flex gap-2">
          <input
            type="text"
            data-testid={`proposal-reject-reason-${proposal.id}`}
            value={rejectionReason}
            onChange={(e) => setRejectionReason(e.target.value)}
            placeholder="Reason for rejecting AI proposal..."
            className="flex-1 rounded border border-slate-300 px-2.5 py-1 text-xs"
          />
          <button
            type="button"
            data-testid={`proposal-reject-submit-${proposal.id}`}
            disabled={!rejectionReason.trim()}
            onClick={() => {
              onReject(proposal.id, rejectionReason.trim());
              setRejecting(false);
            }}
            className="rounded bg-rose-600 px-3 py-1 text-xs font-semibold text-white disabled:opacity-50"
          >
            Confirm Reject
          </button>
        </div>
      )}
    </div>
  );
}
