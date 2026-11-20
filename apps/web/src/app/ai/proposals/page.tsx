"use client";

import React, { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Filter, Inbox as InboxIcon } from "lucide-react";

import { AppShell } from "@/components/app-shell";
import { useAuthSession } from "@/components/providers";
import { ProposalReviewCard } from "@/components/ai/proposal-review-card";
import { Badge, EmptyState, Page, PageHeader } from "@/components/ui/page";
import { SegmentedTabs } from "@/components/ui/segmented-tabs";
import { approveProposal, editApproveProposal, rejectProposal } from "@/lib/api-client/live-client";
import { fetchPendingProposalsCount, fetchProposals } from "@/lib/data-source";
import { useI18n } from "@/lib/i18n";
import type { ProposalStatus, ProposalType } from "@/lib/contracts";

const STATUS_COPY: Record<ProposalStatus, { zh: string; en: string }> = {
  pending: { zh: "待审核", en: "Pending" },
  approved: { zh: "已批准", en: "Approved" },
  edited_and_approved: { zh: "编辑后批准", en: "Edited & approved" },
  rejected: { zh: "已驳回", en: "Rejected" },
  expired: { zh: "已过期", en: "Expired" },
  failed: { zh: "执行失败", en: "Failed" },
};

const TYPE_COPY: Record<ProposalType, { zh: string; en: string }> = {
  reply_draft: { zh: "回复草稿", en: "Reply draft" },
  crm_entity_extract: { zh: "客户信息抽取", en: "CRM extraction" },
  quote_draft: { zh: "报价草稿", en: "Quote draft" },
  follow_up_suggestion: { zh: "跟进建议", en: "Follow-up suggestion" },
  identity_merge: { zh: "身份合并", en: "Identity merge" },
  refund_or_reship: { zh: "退款或补发", en: "Refund or reship" },
  payment_match: { zh: "收款匹配", en: "Payment match" },
};

export default function AiProposalsPage() {
  const { principal } = useAuthSession();
  const { bi } = useI18n();
  const queryClient = useQueryClient();
  const [statusFilter, setStatusFilter] = useState<ProposalStatus>("pending");
  const [typeFilter, setTypeFilter] = useState<"" | ProposalType>("");

  const proposalsQuery = useQuery({
    queryKey: ["ai", "proposals", statusFilter, typeFilter],
    queryFn: () => fetchProposals(statusFilter, typeFilter),
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
      <Page className="min-w-0">
        <PageHeader
          title={bi("AI 提案人工审核队列", "AI proposal review queue")}
          description={bi(
            "所有 L1 级 AI 动作都必须由人审批：回复草稿、客户信息抽取、B2B 报价与身份合并。编辑差异会沉淀为租户私有 Golden Few-shot 评测集。",
            "Every L1 AI action needs human approval: reply drafts, CRM extraction, B2B quotes and identity merges. Edited diffs feed the tenant-private Golden Few-shot eval set."
          )}
          badge={
            <Badge tone={(pendingCountQuery.data?.total ?? 0) > 0 ? "warning" : "success"}>
              {bi("待审核", "Pending")} {pendingCountQuery.data?.total ?? 0}
            </Badge>
          }
          actions={
            <div className="flex min-w-0 max-w-full flex-wrap items-center gap-2 text-slate-900 dark:text-slate-100">
              <SegmentedTabs<ProposalStatus>
                ariaLabel={bi("按状态筛选", "Filter by status")}
                value={statusFilter}
                onChange={setStatusFilter}
                size="sm"
                options={(["pending", "approved", "rejected", "expired"] as ProposalStatus[]).map(
                  (status) => ({
                    value: status,
                    label: bi(STATUS_COPY[status].zh, STATUS_COPY[status].en),
                    testId: `proposal-status-tab-${status}`,
                    badge: status === "pending" ? (pendingCountQuery.data?.total ?? 0) : undefined,
                  })
                )}
              />
              <label className="inline-flex max-w-full min-w-0 items-center gap-1.5 rounded-xl border border-slate-200 bg-white px-2.5 py-1.5 text-[12px] font-medium text-slate-600 shadow-xs dark:border-slate-700 dark:bg-slate-900 dark:text-slate-400">
                <Filter className="h-3.5 w-3.5 shrink-0 text-slate-400 dark:text-slate-500" aria-hidden="true" />
                <select
                  value={typeFilter}
                  aria-label={bi("按提案类型筛选", "Filter by proposal type")}
                  onChange={(e) => setTypeFilter(e.target.value as "" | ProposalType)}
                  className="min-w-0 bg-transparent text-[12px] font-semibold text-slate-800 outline-none dark:text-slate-200"
                >
                  <option value="">{bi("全部提案类型", "All proposal types")}</option>
                  {(Object.keys(TYPE_COPY) as ProposalType[]).map((type) => (
                    <option key={type} value={type}>
                      {bi(TYPE_COPY[type].zh, TYPE_COPY[type].en)}
                    </option>
                  ))}
                </select>
              </label>
            </div>
          }
        />

        {items.length === 0 ? (
          <EmptyState>
            <InboxIcon className="mx-auto mb-2 h-5 w-5 text-slate-300 dark:text-slate-600" />
            {bi(
              `当前「${STATUS_COPY[statusFilter].zh}」队列没有提案。`,
              `No proposals in the ${STATUS_COPY[statusFilter].en.toLowerCase()} queue.`
            )}
          </EmptyState>
        ) : (
          <div className="grid min-w-0 grid-cols-1 gap-3 2xl:grid-cols-2">
            {items.map((proposal) => {
              const role = principal?.role;
              const canReview =
                role !== "finance" || proposal.proposal_type === "payment_match";
              return (
                <div key={proposal.id} className="min-w-0">
                  <ProposalReviewCard
                    proposal={proposal}
                    canReview={canReview}
                    onApprove={(id) => approveMutation.mutate(id)}
                    onEditApprove={(id, edited_payload) =>
                      editApproveMutation.mutate({ id, edited_payload })
                    }
                    onReject={(id, reason) => rejectMutation.mutate({ id, reason })}
                  />
                </div>
              );
            })}
          </div>
        )}
      </Page>
    </AppShell>
  );
}
