"use client";

import React, { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarClock, Mail, MessageSquare, Phone, Plus, UsersRound } from "lucide-react";

import { AppShell } from "@/components/app-shell";
import { BentoStatCard } from "@/components/ui/bento-stat-card";
import { EmptyState, Page, PageHeader } from "@/components/ui/page";
import { fetchPendingProposalsCount } from "@/lib/data-source";
import {
  addPhase3aFollowup,
  fetchPhase3aCustomers,
  fetchPhase3aFollowups,
  type FollowupCreate,
} from "@/lib/phase3a-data";
import { useI18n } from "@/lib/i18n";
import { CRM_FIELD } from "../_components/crm-ui";

const TYPES: FollowupCreate["followup_type"][] = ["email", "call", "meeting", "note"];

const TYPE_META: Record<
  FollowupCreate["followup_type"],
  { zh: string; en: string; icon: React.ComponentType<{ className?: string }>; tone: string }
> = {
  email: {
    zh: "邮件",
    en: "Email",
    icon: Mail,
    tone: "bg-blue-50 text-blue-600 ring-blue-100",
  },
  call: {
    zh: "电话",
    en: "Call",
    icon: Phone,
    tone: "bg-emerald-50 text-emerald-600 ring-emerald-100",
  },
  meeting: {
    zh: "会议",
    en: "Meeting",
    icon: UsersRound,
    tone: "bg-violet-50 text-violet-600 ring-violet-100",
  },
  note: {
    zh: "内部备注",
    en: "Note",
    icon: MessageSquare,
    tone: "bg-amber-50 text-amber-600 ring-amber-100",
  },
};

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

  const summaryCounts = useMemo(() => {
    const counts: Record<FollowupCreate["followup_type"], number> = {
      email: 0,
      call: 0,
      meeting: 0,
      note: 0,
    };
    for (const row of followups) {
      counts[row.followup_type] = (counts[row.followup_type] ?? 0) + 1;
    }
    const pendingNext = followups.filter((row) => row.next_action_at).length;
    return { ...counts, pendingNext };
  }, [followups]);

  return (
    <AppShell currentPath="/crm/followups" pendingProposalsCount={proposalsQuery.data?.total ?? 0}>
      <Page>
        <PageHeader
          title={bi("跟进记录", "Follow-ups")}
          description={bi(
            "按客户时间线记录邮件、电话、会议与内部备注。",
            "Log emails, calls, meetings and internal notes on the customer timeline."
          )}
        />

        <section
          aria-label={bi("跟进关键指标", "Follow-up key metrics")}
          className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"
        >
          <BentoStatCard
            label={bi("跟进总数", "Total follow-ups")}
            value={followups.length}
            icon={CalendarClock}
            accent="blue"
            hint={bi("按当前客户筛选统计", "Counted for the selected customer")}
          />
          <BentoStatCard
            label={bi("邮件与电话", "Email & calls")}
            value={summaryCounts.email + summaryCounts.call}
            icon={Mail}
            accent="emerald"
            hint={bi("异步触达渠道", "Asynchronous outreach")}
          />
          <BentoStatCard
            label={bi("会议与备注", "Meetings & notes")}
            value={summaryCounts.meeting + summaryCounts.note}
            icon={UsersRound}
            accent="violet"
            hint={bi("同步沟通与内部记录", "Live conversations and internal notes")}
          />
          <BentoStatCard
            label={bi("待办下一步", "Pending next actions")}
            value={summaryCounts.pendingNext}
            icon={CalendarClock}
            accent="amber"
            hint={bi("已设置下次跟进时间", "Next action time is scheduled")}
          />
        </section>

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
          className="erp-card grid gap-2 p-3.5 sm:grid-cols-[1fr_140px_2fr_auto]"
        >
          <select
            aria-label="Customer"
            value={effectiveCustomer}
            onChange={(event) => setCustomerId(event.target.value)}
            className={CRM_FIELD}
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
            className={CRM_FIELD}
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
            className={CRM_FIELD}
          />
          <button
            type="submit"
            data-testid="followup-submit"
            disabled={!effectiveCustomer || summary.trim().length === 0 || createFollowup.isPending}
            className="inline-flex items-center justify-center gap-1.5 rounded-xl bg-blue-600 px-3 py-1.5 text-[12px] font-semibold text-white shadow-[0_2px_8px_-2px_rgba(37,99,235,0.55)] transition-colors hover:bg-blue-700 disabled:shadow-none disabled:opacity-40"
          >
            <Plus className="h-3.5 w-3.5" />
            {bi("新增跟进", "Add follow-up")}
          </button>
          {createFollowup.isError && (
            <p role="alert" className="text-[12px] font-medium text-rose-700 sm:col-span-4">
              {createFollowup.error.message}
            </p>
          )}
        </form>

        {followups.length === 0 ? (
          <EmptyState>
            {followupsQuery.isLoading
              ? bi("加载中…", "Loading…")
              : bi("暂无跟进记录", "No follow-ups yet")}
          </EmptyState>
        ) : (
          <ol data-testid="followup-timeline" className="relative space-y-2.5 pl-1">
            {followups.map((followup) => {
              const meta = TYPE_META[followup.followup_type] ?? TYPE_META.note;
              const Icon = meta.icon;
              return (
                <li
                  key={followup.id}
                  data-testid={`followup-${followup.id}`}
                  className="erp-card flex gap-3 p-3.5 text-[13px] transition-shadow hover:shadow-[0_4px_14px_rgba(15,23,42,0.08)]"
                >
                  <span
                    aria-hidden="true"
                    className={`grid h-8 w-8 shrink-0 place-items-center rounded-xl ring-1 ${meta.tone}`}
                  >
                    <Icon className="h-4 w-4" />
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span className="text-[11px] font-semibold tracking-wide text-slate-600">
                        {bi(meta.zh, meta.en)}
                      </span>
                      <span className="text-[10.5px] font-medium tabular-nums text-slate-500">
                        {new Date(followup.created_at).toLocaleString()}
                      </span>
                    </div>
                    <p className="mt-1 leading-6 text-slate-800">{followup.summary}</p>
                    {followup.next_action_at && (
                      <p className="mt-1 inline-flex items-center gap-1 rounded-md bg-blue-50 px-1.5 py-0.5 text-[10.5px] font-semibold text-blue-800 ring-1 ring-blue-100">
                        <CalendarClock className="h-3 w-3" />
                        {bi("下一步", "Next action")}：
                        {new Date(followup.next_action_at).toLocaleString()}
                      </p>
                    )}
                  </div>
                </li>
              );
            })}
          </ol>
        )}

      </Page>
    </AppShell>
  );
}
