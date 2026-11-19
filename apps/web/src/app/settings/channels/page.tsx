"use client";

import React, { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, ShieldCheck, Ban } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { replayDlqEvent } from "@/lib/api-client/live-client";
import {
  fetchChannelAccounts,
  fetchChannelConsents,
  fetchChannelDlq,
  fetchChannelSuppressions,
  fetchPendingProposalsCount,
} from "@/lib/data-source";

export default function ChannelsSettingsPage() {
  const queryClient = useQueryClient();
  const [logTab, setLogTab] = useState<"consents" | "suppressions">("consents");

  const pendingProposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () => fetchPendingProposalsCount(),
  });

  const accountsQuery = useQuery({
    queryKey: ["channels", "accounts"],
    queryFn: () => fetchChannelAccounts(),
  });

  const dlqQuery = useQuery({
    queryKey: ["channels", "dlq"],
    queryFn: () => fetchChannelDlq(),
  });

  const consentsQuery = useQuery({
    queryKey: ["channels", "consents"],
    queryFn: () => fetchChannelConsents(),
  });

  const suppressionsQuery = useQuery({
    queryKey: ["channels", "suppressions"],
    queryFn: () => fetchChannelSuppressions(),
  });

  const replayMutation = useMutation({
    mutationFn: (dlqId: string) => replayDlqEvent(dlqId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["channels", "dlq"] });
    },
  });

  return (
    <AppShell
      currentPath="/settings/channels"
      pendingProposalsCount={pendingProposalsQuery.data?.total ?? 0}
    >
      <div className="mx-auto max-w-6xl space-y-5">
        <div>
          <h1 className="text-lg font-bold text-slate-900">
            Connected Channels, Dead-Letter Queue (DLQ) & Opt-In Compliance
          </h1>
          <p className="text-xs text-slate-500">
            Monitor rate-limit buckets (Coexistence vs Cloud API), replay DLQ webhooks, and audit consent/suppression records.
          </p>
        </div>

        <div className="rounded-lg border border-slate-200 bg-white p-4 text-xs">
          <h2 className="text-sm font-bold text-slate-900">Connected Channel Accounts</h2>
          <div className="mt-3 space-y-2">
            {(accountsQuery.data?.items ?? []).map((acct) => (
              <div
                key={acct.id}
                className="flex flex-wrap items-center justify-between gap-2 rounded border border-slate-200 p-2.5"
              >
                <div>
                  <span className="font-bold text-slate-900">{acct.display_name}</span>
                  <span className="ml-2 rounded bg-slate-100 px-1.5 py-0.5">
                    {acct.channel_type}
                  </span>
                </div>
                <div className="flex items-center gap-2">
                  {acct.rate_limit_profile?.mps && (
                    <span className="rounded bg-indigo-50 px-2 py-0.5 font-semibold text-indigo-700">
                      {acct.rate_limit_profile.mps} mps
                    </span>
                  )}
                  <span className="rounded bg-emerald-50 px-2 py-0.5 font-semibold text-emerald-700">
                    {acct.status}
                  </span>
                </div>
              </div>
            ))}
          </div>
        </div>

        <div className="rounded-lg border border-slate-200 bg-white p-4 text-xs">
          <h2 className="text-sm font-bold text-slate-900">Dead Letter Queue (DLQ) Replay</h2>
          <div className="mt-3 space-y-2">
            {(dlqQuery.data?.items ?? []).length === 0 ? (
              <div className="text-slate-400">Zero failed DLQ events.</div>
            ) : (
              dlqQuery.data!.items.map((ev) => (
                <div
                  key={ev.id}
                  className="flex flex-wrap items-center justify-between gap-2 rounded border border-rose-200 bg-rose-50/40 p-2.5"
                >
                  <div>
                    <div className="font-semibold text-slate-900">
                      {ev.channel_type} · {ev.external_event_id}
                    </div>
                    <div className="text-rose-700">
                      {ev.error_message} (retries: {ev.retry_count})
                    </div>
                  </div>
                  <button
                    type="button"
                    data-testid={`dlq-replay-btn-${ev.id}`}
                    onClick={() => replayMutation.mutate(ev.id)}
                    className="inline-flex items-center gap-1 rounded bg-slate-900 px-2.5 py-1 font-semibold text-white hover:bg-slate-800"
                  >
                    <RefreshCw className="h-3 w-3" />
                    <span>Replay</span>
                  </button>
                </div>
              ))
            )}
          </div>
        </div>

        <div className="rounded-lg border border-slate-200 bg-white p-4 text-xs">
          <div className="flex items-center gap-2 border-b border-slate-200 pb-2">
            <button
              type="button"
              data-testid="tab-consent-records"
              onClick={() => setLogTab("consents")}
              className={`inline-flex items-center gap-1 rounded px-3 py-1 font-semibold ${
                logTab === "consents" ? "bg-indigo-600 text-white" : "text-slate-600"
              }`}
            >
              <ShieldCheck className="h-3.5 w-3.5" />
              <span>Consent Ledger</span>
            </button>
            <button
              type="button"
              data-testid="tab-suppression-list"
              onClick={() => setLogTab("suppressions")}
              className={`inline-flex items-center gap-1 rounded px-3 py-1 font-semibold ${
                logTab === "suppressions" ? "bg-indigo-600 text-white" : "text-slate-600"
              }`}
            >
              <Ban className="h-3.5 w-3.5" />
              <span>Suppression List</span>
            </button>
          </div>

          <div className="mt-3 space-y-1.5">
            {logTab === "consents"
              ? (consentsQuery.data?.items ?? []).map((c) => (
                  <div
                    key={c.id}
                    className="flex items-center justify-between rounded border border-slate-100 p-2"
                  >
                    <div>
                      <strong>{c.customer_name}</strong> · {c.channel_type} ·{" "}
                      <span className="text-slate-500">{c.proof_source}</span>
                    </div>
                    <span className="rounded bg-emerald-50 px-2 py-0.5 font-semibold text-emerald-700">
                      {c.consent_status}
                    </span>
                  </div>
                ))
              : (suppressionsQuery.data?.items ?? []).map((s) => (
                  <div
                    key={s.id}
                    className="flex items-center justify-between rounded border border-slate-100 p-2"
                  >
                    <div>
                      <strong>{s.normalized_address}</strong> ({s.channel_type})
                    </div>
                    <span className="text-rose-700">{s.reason}</span>
                  </div>
                ))}
          </div>
        </div>
      </div>
    </AppShell>
  );
}
