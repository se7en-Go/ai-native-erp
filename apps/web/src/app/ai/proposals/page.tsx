"use client";

import React, { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AppShell } from "@/components/app-shell";
import { useAuthSession } from "@/components/providers";
import { ProposalReviewCard, type AiProposalItem } from "@/components/ai/proposal-review-card";
import {
  approveProposal,
  editApproveProposal,
  rejectProposal,
} from "@/lib/api-client/plan04-bridge";
import { fetchPendingProposalsCount, fetchProposals } from "@/lib/data-source";
import type { ProposalStatus, ProposalType } from "@/lib/contracts";

export default function AiProposalsPage() {
  const { principal } = useAuthSession();
  const queryClient = useQueryClient();
  const [statusFilter, setStatusFilter] = useState<ProposalStatus>("pending");
  const [typeFilter, setTypeFilter] = useState<"" | ProposalType>("");

  const proposalsQuery = useQuery({
    queryKey: ["ai", "proposals", statusFilter, typeFilter],
    queryFn: () => fetchProposals<AiProposalItem>(statusFilter, typeFilter),
  });

  const pendingCountQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () => fetchPendingProposalsCount(),
  });

  const approveMutation = useMutation({
    mutationFn: (id: string) => approveProposal(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["ai", "proposals"] });
    },
  });

  const editApproveMutation = useMutation({
    mutationFn: ({ id, edited_payload }: { id: string; edited_payload: Record<string, unknown> }) =>
      editApproveProposal(id, edited_payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["ai", "proposals"] });
    },
  });

  const rejectMutation = useMutation({
    mutationFn: ({ id, reason }: { id: string; reason: string }) =>
      rejectProposal(id, reason),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["ai", "proposals"] });
    },
  });

  const items = proposalsQuery.data?.items ?? [];

  return (
    <AppShell
      currentPath="/ai/proposals"
      pendingProposalsCount={pendingCountQuery.data?.total ?? 0}
    >
      <div className="mx-auto max-w-5xl space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="text-lg font-bold text-slate-900">L1 AI Proposal Approval Queue</h1>
            <p className="text-xs text-slate-500">
              Review AI drafts, CRM extractions, B2B PI quotes, and identity merges. Edit diffs feed tenant-private Golden Few-shot evals.
            </p>
          </div>

          <div className="flex gap-2">
            <select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value as ProposalStatus)}
              className="rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-xs"
            >
              <option value="pending">pending</option>
              <option value="approved">approved</option>
              <option value="edited_and_approved">edited_and_approved</option>
              <option value="rejected">rejected</option>
            </select>

            <select
              value={typeFilter}
              onChange={(e) => setTypeFilter(e.target.value as "" | ProposalType)}
              className="rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-xs"
            >
              <option value="">All Proposal Types</option>
              <option value="reply_draft">reply_draft</option>
              <option value="crm_entity_extract">crm_entity_extract</option>
              <option value="quote_draft">quote_draft</option>
              <option value="follow_up_suggestion">follow_up_suggestion</option>
              <option value="identity_merge">identity_merge</option>
              <option value="refund_or_reship">refund_or_reship</option>
              <option value="payment_match">payment_match</option>
            </select>
          </div>
        </div>

        {items.length === 0 ? (
          <div className="rounded-lg border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">
            No proposals in {statusFilter} queue.
          </div>
        ) : (
          <div className="space-y-3">
            {items.map((proposal) => {
              const role = principal?.role;
              const canReview =
                role !== "finance" || proposal.proposal_type === "payment_match";
              return (
                <ProposalReviewCard
                  key={proposal.id}
                  proposal={proposal}
                  canReview={canReview}
                  onApprove={(id) => approveMutation.mutate(id)}
                  onEditApprove={(id, edited_payload) =>
                    editApproveMutation.mutate({ id, edited_payload })
                  }
                  onReject={(id, reason) =>
                    rejectMutation.mutate({ id, reason })
                  }
                />
              );
            })}
          </div>
        )}
      </div>
    </AppShell>
  );
}
