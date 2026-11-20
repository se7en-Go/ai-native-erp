"use client";

import { useI18n } from "@/lib/i18n";

import React, { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { AppShell } from "@/components/app-shell";
import { fetchPendingProposalsCount } from "@/lib/data-source";
import {
  addPhase3aFollowup,
  fetchPhase3aCustomers,
  fetchPhase3aFollowups,
  type FollowupCreate,
} from "@/lib/phase3a-data";

const TYPES: FollowupCreate["followup_type"][] = ["email", "call", "meeting", "note"];

export default function CrmFollowupsPage() {
  const { bi } = useI18n();
  const queryClient = useQueryClient();
  const [customerId, setCustomerId] = useState("");
  const [followupType, setFollowupType] = useState<FollowupCreate["followup_type"]>("email");
  const [summary, setSummary] = useState("");

  const proposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () => fetchPendingProposalsCount(),
  });
  const customersQuery = useQuery({
    queryKey: ["crm", "customers"],
    queryFn: () => fetchPhase3aCustomers(),
  });
  const followupsQuery = useQuery({
    queryKey: ["crm", "followups", customerId],
    queryFn: () => fetchPhase3aFollowups(customerId || undefined),
  });
  const createFollowup = useMutation({
    mutationFn: (payload: FollowupCreate) => addPhase3aFollowup(payload),
    onSuccess: () => {
      setSummary("");
      void queryClient.invalidateQueries({ queryKey: ["crm", "followups"] });
    },
  });

  const customers = customersQuery.data?.items ?? [];
  const followups = followupsQuery.data?.items ?? [];
  const effectiveCustomer = customerId || customers[0]?.id || "";

  return (
    <AppShell currentPath="/crm/followups" pendingProposalsCount={proposalsQuery.data?.total ?? 0}>
      <div className="erp-page space-y-4">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-slate-900">{bi("跟进记录", "Follow-ups")}</h1>
          <p className="mt-1 text-[13px] leading-6 text-slate-500">{bi("按客户时间线记录邮件、电话、会议与内部备注。", "Log emails, calls, meetings and internal notes on the customer timeline.")}</p>
        </div>

        <form
          data-testid="followup-form"
          onSubmit={(event) => {
            event.preventDefault();
            createFollowup.mutate({
              customer_id: effectiveCustomer,
              followup_type: followupType,
              summary: summary.trim(),
              next_action_at: null,
              opportunity_id: null,
            });
          }}
          className="erp-card grid gap-2 p-3 sm:grid-cols-[1fr_140px_2fr_auto]"
        >
          <select
            aria-label="Customer"
            value={effectiveCustomer}
            onChange={(event) => setCustomerId(event.target.value)}
            className="rounded border border-slate-300 px-2 py-1 text-xs"
          >
            {customers.map((customer) => (
              <option key={customer.id} value={customer.id}>
                {customer.display_name}
              </option>
            ))}
          </select>
          <select
            aria-label="Follow-up type"
            value={followupType}
            onChange={(event) => setFollowupType(event.target.value as FollowupCreate["followup_type"])}
            className="rounded border border-slate-300 px-2 py-1 text-xs"
          >
            {TYPES.map((type) => (
              <option key={type} value={type}>
                {type}
              </option>
            ))}
          </select>
          <input
            aria-label="Follow-up summary"
            required
            minLength={1}
            maxLength={5000}
            value={summary}
            onChange={(event) => setSummary(event.target.value)}
            placeholder="本次跟进内容与下一步计划…"
            className="rounded border border-slate-300 px-2 py-1 text-xs"
          />
          <button
            type="submit"
            data-testid="followup-submit"
            disabled={!effectiveCustomer || summary.trim().length === 0 || createFollowup.isPending}
            className="rounded bg-blue-600 px-3 py-1 text-xs font-medium text-white hover:bg-blue-700 disabled:opacity-40"
          >
            {bi("新增跟进", "Add follow-up")}
          </button>
          {createFollowup.isError && (
            <p role="alert" className="sm:col-span-4 text-xs text-rose-700">
              {createFollowup.error.message}
            </p>
          )}
        </form>

        <ol data-testid="followup-timeline" className="space-y-2">
          {followups.map((followup) => (
            <li
              key={followup.id}
              data-testid={`followup-${followup.id}`}
              className="erp-card p-3 text-[13px]"
            >
              <div className="flex items-center justify-between text-[10px] uppercase text-slate-400">
                <span>{followup.followup_type}</span>
                <span>{new Date(followup.created_at).toLocaleString()}</span>
              </div>
              <p className="mt-1 text-slate-700">{followup.summary}</p>
              {followup.next_action_at && (
                <p className="mt-1 text-[10px] text-blue-600">
                  {bi("下一步", "Next action")}：{new Date(followup.next_action_at).toLocaleString()}
                </p>
              )}
            </li>
          ))}
          {followups.length === 0 && (
            <li className="rounded-lg border border-dashed border-slate-200 p-4 text-center text-xs text-slate-400">
              {followupsQuery.isLoading ? bi("加载中…", "Loading…") : bi("暂无跟进记录", "No follow-ups yet")}
            </li>
          )}
        </ol>
      </div>
    </AppShell>
  );
}
