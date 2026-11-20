"use client";

import React, { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, CheckCircle2, Filter, Target, Users } from "lucide-react";

import { AppShell } from "@/components/app-shell";
import { useAuthSession } from "@/components/providers";
import { BentoStatCard } from "@/components/ui/bento-stat-card";
import { EmptyState, Page, PageHeader, TableWrap } from "@/components/ui/page";
import type { LeadStatus } from "@/lib/api-client/types.gen";
import { fetchPendingProposalsCount } from "@/lib/data-source";
import {
  convertPhase3aLeadToOpportunity,
  fetchPhase3aLeads,
  patchPhase3aLead,
} from "@/lib/phase3a-data";
import { useI18n } from "@/lib/i18n";
import { CRM_FIELD, CrmStatusPill, CrmTierBadge, tierFromScore } from "../_components/crm-ui";

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

  const summary = useMemo(() => {
    const qualified = leads.filter((lead) => lead.status === "qualified").length;
    const converted = leads.filter((lead) => lead.status === "converted").length;
    const avgScore =
      leads.length === 0
        ? 0
        : Math.round(
            leads.reduce((total, lead) => total + lead.qualification_score, 0) / leads.length
          );
    return { qualified, converted, avgScore };
  }, [leads]);

  return (
    <AppShell currentPath="/crm/leads" pendingProposalsCount={proposalsQuery.data?.total ?? 0}>
      <Page>
        <PageHeader
          title={bi("CRM 线索池", "CRM leads")}
          description={bi(
            "线索资格评分、来源渠道与一键转商机；转商机后线索状态自动变为 converted。",
            "Qualification score, source channel and one-click conversion; the lead becomes converted automatically."
          )}
        />

        <section
          aria-label={bi("线索关键指标", "Lead key metrics")}
          className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"
        >
          <BentoStatCard
            label={bi("线索总数", "Total leads")}
            value={leads.length}
            icon={Users}
            accent="blue"
            hint={bi("含全部来源渠道", "Every acquisition channel")}
          />
          <BentoStatCard
            label={bi("已资格确认", "Qualified")}
            value={summary.qualified}
            icon={CheckCircle2}
            accent="emerald"
            hint={bi("可一键转商机", "Ready for one-click conversion")}
          />
          <BentoStatCard
            label={bi("已转商机", "Converted")}
            value={summary.converted}
            icon={Target}
            accent="violet"
            hint={bi("状态自动变为 converted", "Status flips to converted automatically")}
          />
          <BentoStatCard
            label={bi("平均资格分", "Average score")}
            value={summary.avgScore}
            icon={Filter}
            accent="amber"
            hint={bi("A ≥ 70 · B ≥ 40 · C < 40", "A ≥ 70 · B ≥ 40 · C < 40")}
          />
        </section>

        <div className="flex flex-wrap items-center justify-between gap-2.5 rounded-2xl border border-slate-200/90 bg-white px-3.5 py-2.5 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[12.5px] font-semibold text-slate-800">
              {bi("筛选线索状态", "Filter by status")}
            </span>
            <select
              data-testid="lead-status-filter"
              value={status}
              onChange={(event) => setStatus(event.target.value as LeadStatus | "")}
              className={CRM_FIELD}
            >
              {STATUSES.map((value) => (
                <option key={value || "all"} value={value}>
                  {value || bi("全部状态", "All statuses")}
                </option>
              ))}
            </select>
          </div>
          <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-0.5 text-[11px] font-semibold tabular-nums text-slate-700">
            {leads.length} {bi("条线索", "leads")}
          </span>
        </div>

        {error && (
          <p
            role="alert"
            className="rounded-xl border border-rose-200 bg-rose-50 p-2.5 text-[12px] font-medium text-rose-800"
          >
            {error.message}
          </p>
        )}

        {leads.length === 0 ? (
          <EmptyState>
            {leadsQuery.isLoading ? bi("加载中…", "Loading…") : bi("暂无线索", "No leads")}
          </EmptyState>
        ) : (
          <TableWrap className="rounded-2xl">
            <table data-testid="leads-table" className="erp-table">
              <thead>
                <tr>
                  <th>{bi("线索 / 客户", "Lead / customer")}</th>
                  <th>{bi("来源", "Source")}</th>
                  <th>{bi("评分与等级", "Score & tier")}</th>
                  <th>{bi("状态", "Status")}</th>
                  <th className="text-right">{bi("操作", "Action")}</th>
                </tr>
              </thead>
              <tbody>
                {leads.map((lead) => {
                  const tier = tierFromScore(lead.qualification_score);
                  return (
                    <tr key={lead.id} data-testid={`lead-row-${lead.id}`}>
                      <td>
                        <div className="font-mono text-[12px] font-semibold text-slate-900">
                          {lead.id.slice(0, 8)}
                        </div>
                        <div className="font-mono text-[11px] text-slate-500">
                          {lead.customer_id.slice(0, 8)}
                        </div>
                      </td>
                      <td className="text-slate-700">
                        <span className="rounded-md bg-slate-100 px-1.5 py-0.5 text-[11px] font-medium text-slate-700">
                          {lead.source_channel}
                        </span>
                        {lead.utm_campaign ? (
                          <div className="mt-1 text-[11px] text-slate-500">{lead.utm_campaign}</div>
                        ) : null}
                      </td>
                      <td>
                        <div className="flex items-center gap-2">
                          <span className="text-[13px] font-bold tabular-nums text-slate-900">
                            {lead.qualification_score}
                          </span>
                          <CrmTierBadge tier={tier} />
                        </div>
                      </td>
                      <td>
                        <CrmStatusPill status={lead.status} />
                      </td>
                      <td>
                        <div className="flex justify-end gap-2">
                          <button
                            type="button"
                            data-testid={`qualify-lead-${lead.id}`}
                            disabled={lead.status === "converted" || qualify.isPending}
                            onClick={() => qualify.mutate(lead.id)}
                            className="rounded-lg border border-slate-200 bg-white px-2 py-1 text-[11px] font-semibold text-slate-700 transition-colors hover:border-emerald-200 hover:bg-emerald-50 hover:text-emerald-800 disabled:opacity-40"
                          >
                            {bi("标记已资格", "Mark qualified")}
                          </button>
                          <button
                            type="button"
                            data-testid={`convert-lead-${lead.id}`}
                            disabled={lead.status === "converted" || convert.isPending}
                            onClick={() => convert.mutate(lead)}
                            className="flex items-center gap-1 rounded-lg bg-blue-600 px-2 py-1 text-[11px] font-semibold text-white shadow-xs transition-colors hover:bg-blue-700 disabled:opacity-40"
                          >
                            {bi("转商机", "Convert")} <ArrowRight className="h-3 w-3" />
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </TableWrap>
        )}
      </Page>
    </AppShell>
  );
}
