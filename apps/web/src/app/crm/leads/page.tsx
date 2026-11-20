"use client";

import { useI18n } from "@/lib/i18n";

import React, { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight } from "lucide-react";

import { AppShell } from "@/components/app-shell";
import { useAuthSession } from "@/components/providers";
import type { LeadStatus } from "@/lib/api-client/types.gen";
import { fetchPendingProposalsCount } from "@/lib/data-source";
import {
  convertPhase3aLeadToOpportunity,
  fetchPhase3aLeads,
  patchPhase3aLead,
} from "@/lib/phase3a-data";

const STATUSES: Array<LeadStatus | ""> = [
  "",
  "new",
  "contacted",
  "qualified",
  "disqualified",
  "converted",
];

export default function CrmLeadsPage() {
  const { bi } = useI18n();
  const queryClient = useQueryClient();
  const { principal } = useAuthSession();
  const [status, setStatus] = useState<LeadStatus | "">("");

  const proposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () => fetchPendingProposalsCount(),
  });
  const leadsQuery = useQuery({
    queryKey: ["crm", "leads", status],
    queryFn: () => fetchPhase3aLeads(status || undefined),
  });

  const qualify = useMutation({
    mutationFn: (leadId: string) => patchPhase3aLead(leadId, { status: "qualified" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["crm", "leads"] }),
  });
  const convert = useMutation({
    mutationFn: (lead: Parameters<typeof convertPhase3aLeadToOpportunity>[0]) =>
      convertPhase3aLeadToOpportunity(lead, principal?.user_id ?? ""),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["crm", "leads"] });
      void queryClient.invalidateQueries({ queryKey: ["crm", "opportunities"] });
    },
  });

  const leads = leadsQuery.data?.items ?? [];
  const error = qualify.error ?? convert.error ?? leadsQuery.error;

  return (
    <AppShell currentPath="/crm/leads" pendingProposalsCount={proposalsQuery.data?.total ?? 0}>
      <div className="erp-page space-y-4">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="text-xl font-semibold tracking-tight text-slate-900">{bi("CRM 线索池", "CRM leads")}</h1>
            <p className="text-xs text-slate-500">
              {bi("线索资格评分、来源渠道与一键转商机；转商机后线索状态自动变为 converted。", "Qualification score, source channel and one-click conversion; the lead becomes converted automatically.")}
            </p>
          </div>
          <select
            data-testid="lead-status-filter"
            value={status}
            onChange={(event) => setStatus(event.target.value as LeadStatus | "")}
            className="rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-xs"
          >
            {STATUSES.map((value) => (
              <option key={value || "all"} value={value}>
                {value || bi("全部状态", "All statuses")}
              </option>
            ))}
          </select>
        </div>

        {error && (
          <p role="alert" className="rounded border border-rose-200 bg-rose-50 p-2 text-xs text-rose-800">
            {error.message}
          </p>
        )}

        <div className="overflow-x-auto rounded-xl border border-slate-200/80 bg-white">
          <table data-testid="leads-table" className="erp-table">
            <thead className="border-b border-slate-200 bg-slate-50 font-semibold text-slate-600">
              <tr>
                <th>{bi("线索 / 客户", "Lead / customer")}</th>
                <th>{bi("来源", "Source")}</th>
                <th>{bi("评分", "Score")}</th>
                <th>{bi("状态", "Status")}</th>
                <th className="text-right">{bi("操作", "Action")}</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {leads.map((lead) => (
                <tr key={lead.id} data-testid={`lead-row-${lead.id}`}>
                  <td className="px-3 py-2">
                    <div className="font-medium text-slate-800">{lead.id.slice(0, 8)}</div>
                    <div className="text-slate-500">{lead.customer_id.slice(0, 8)}</div>
                  </td>
                  <td className="px-3 py-2 text-slate-600">
                    {lead.source_channel}
                    {lead.utm_campaign ? ` · ${lead.utm_campaign}` : ""}
                  </td>
                  <td className="px-3 py-2 font-semibold text-slate-800">
                    {lead.qualification_score}
                  </td>
                  <td className="px-3 py-2">
                    <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] uppercase text-slate-700">
                      {lead.status}
                    </span>
                  </td>
                  <td className="px-3 py-2">
                    <div className="flex gap-2">
                      <button
                        type="button"
                        data-testid={`qualify-lead-${lead.id}`}
                        disabled={lead.status === "converted" || qualify.isPending}
                        onClick={() => qualify.mutate(lead.id)}
                        className="rounded border border-slate-200 px-2 py-1 text-[11px] font-medium text-slate-700 hover:bg-slate-100 disabled:opacity-40"
                      >
                        {bi("标记已资格", "Mark qualified")}
                      </button>
                      <button
                        type="button"
                        data-testid={`convert-lead-${lead.id}`}
                        disabled={lead.status === "converted" || convert.isPending}
                        onClick={() => convert.mutate(lead)}
                        className="flex items-center gap-1 rounded bg-blue-600 px-2 py-1 text-[11px] font-medium text-white hover:bg-blue-700 disabled:opacity-40"
                      >
                        {bi("转商机", "Convert")} <ArrowRight className="h-3 w-3" />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
              {leads.length === 0 && (
                <tr>
                  <td colSpan={5} className="px-3 py-6 text-center text-slate-400">
                    {leadsQuery.isLoading ? bi("加载中…", "Loading…") : bi("暂无线索", "No leads")}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </AppShell>
  );
}
