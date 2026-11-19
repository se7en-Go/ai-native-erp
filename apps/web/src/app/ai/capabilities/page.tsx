"use client";

import React, { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Lock, Trash2, Upload } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { useAuthSession } from "@/components/providers";
import {
  deleteKbDocument,
  updateCapability,
  uploadKbDocument,
} from "@/lib/api-client/plan04-bridge";
import {
  fetchCapabilities,
  fetchKbDocuments,
  fetchPendingProposalsCount,
  fetchWallet,
} from "@/lib/data-source";
import { canConfigureAiGovernance } from "@/lib/auth-cookie";
import type { AiCapability, AutonomyLevel } from "@/lib/contracts";

const CAPABILITY_METADATA: Array<{ key: AiCapability; title: string; desc: string }> = [
  {
    key: "cs_support",
    title: "1. AI Customer Support (cs_support)",
    desc: "FAQ citations, pre-sale/after-sale drafting, platform compliance filtering.",
  },
  {
    key: "sales_quoting",
    title: "2. AI Sales & Quoting (sales_quoting)",
    desc: "B2B RFQ extraction, MOQ tier calculation, PI draft proposals.",
  },
  {
    key: "order_logistics",
    title: "3. AI Order & Logistics (order_logistics)",
    desc: "Deterministic WISMO lookup, 17TRACK 72h stagnation outreach.",
  },
  {
    key: "content_studio",
    title: "4. AI Content Studio (content_studio)",
    desc: "Multilingual i18n specs & compliant WhatsApp template drafting.",
  },
  {
    key: "biz_analytics",
    title: "5. AI Business Analytics (biz_analytics)",
    desc: "Read-only CVP margin diagnostics and inventory turnover Q&A.",
  },
  {
    key: "finance_recon",
    title: "6. AI Finance Recon (finance_recon)",
    desc: "T/T slip OCR matching proposals; cash settlement strictly L0 human-only.",
  },
];

const AUTONOMY_LEVELS: AutonomyLevel[] = ["L0_copilot", "L1_proposal", "L2_auto"];

export default function AiCapabilitiesPage() {
  const { principal } = useAuthSession();
  const queryClient = useQueryClient();
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [docTitle, setDocTitle] = useState("");

  const capabilitiesQuery = useQuery({
    queryKey: ["ai", "capabilities"],
    queryFn: () => fetchCapabilities(),
  });

  const walletQuery = useQuery({
    queryKey: ["ai", "wallet"],
    queryFn: () => fetchWallet(),
  });

  const pendingProposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () => fetchPendingProposalsCount(),
  });

  const kbQuery = useQuery({
    queryKey: ["ai", "kb", "documents"],
    queryFn: () => fetchKbDocuments(),
  });

  const updateCapabilityMutation = useMutation({
    mutationFn: ({
      capability,
      autonomy_level,
    }: {
      capability: AiCapability;
      autonomy_level: AutonomyLevel;
    }) => updateCapability(capability, autonomy_level),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["ai", "capabilities"] });
    },
  });

  const uploadKbMutation = useMutation({
    mutationFn: async () => {
      if (!selectedFile) return;
      const form = new FormData();
      form.append("file", selectedFile);
      form.append("title", docTitle.trim() || selectedFile.name);
      return uploadKbDocument(form);
    },
    onSuccess: () => {
      setSelectedFile(null);
      setDocTitle("");
      void queryClient.invalidateQueries({ queryKey: ["ai", "kb", "documents"] });
    },
  });

  const deleteKbMutation = useMutation({
    mutationFn: (id: string) => deleteKbDocument(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["ai", "kb", "documents"] });
    },
  });

  const canConfigure = canConfigureAiGovernance(principal?.role);
  const wallet = walletQuery.data;
  const remainingCredits =
    (wallet?.included_credits_remaining ?? 0) + (wallet?.topup_credits_balance ?? 0);
  const isL0Fallback = Boolean(
    wallet && wallet.auto_downgrade_to_l0 && remainingCredits <= 0
  );

  const levelMap = new Map(
    (capabilitiesQuery.data?.capabilities ?? []).map((c) => [
      c.capability,
      c.configured_autonomy,
    ])
  );

  return (
    <AppShell
      currentPath="/ai/capabilities"
      pendingProposalsCount={pendingProposalsQuery.data?.total ?? 0}
    >
      <div className="mx-auto max-w-5xl space-y-5">
        <div className="rounded-lg border border-slate-200 bg-white p-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h1 className="text-lg font-bold text-slate-900">
                6-Capability AI Governance & Hybrid RAG Knowledge Base
              </h1>
              <p className="text-xs text-slate-500">
                Canonical L0 / L1 / L2 autonomy matrix (no L3 bypass). Live prices and stock stay strictly SQL tool-grounded.
              </p>
            </div>
            {wallet && (
              <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs">
                <div>
                  Plan: <strong>{wallet.plan_tier}</strong> · Remaining Credits:{" "}
                  <strong>{remainingCredits}</strong>
                </div>
              </div>
            )}
          </div>

          {isL0Fallback && (
            <div
              data-testid="wallet-l0-fallback-alert"
              className="mt-3 flex items-center gap-2 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs font-medium text-amber-900"
            >
              <AlertTriangle className="h-4 w-4 shrink-0 text-amber-600" />
              <span>
                Credit wallet depleted — AI automatically degraded to L0_copilot human-only mode. Inbound/outbound messaging remains 100% online.
              </span>
            </div>
          )}
        </div>

        <div className="grid gap-3 md:grid-cols-2">
          {CAPABILITY_METADATA.map((cap) => {
            const currentLevel = levelMap.get(cap.key) ?? "L1_proposal";
            const isFinance = cap.key === "finance_recon";

            return (
              <div
                key={cap.key}
                className="rounded-lg border border-slate-200 bg-white p-4"
              >
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <h2 className="text-sm font-bold text-slate-900">{cap.title}</h2>
                    <p className="mt-0.5 text-xs text-slate-500">{cap.desc}</p>
                  </div>
                  {isFinance && (
                    <span
                      data-testid="finance-recon-l0-lock"
                      className="inline-flex items-center gap-1 rounded bg-rose-100 px-2 py-0.5 text-[11px] font-semibold text-rose-800"
                    >
                      <Lock className="h-3 w-3" />
                      <span>L0 Settlement Lock</span>
                    </span>
                  )}
                </div>

                <div className="mt-3 grid grid-cols-3 gap-1.5">
                  {AUTONOMY_LEVELS.map((lvl) => {
                    const active = currentLevel === lvl;
                    const lockedL2 = isFinance && lvl === "L2_auto";
                    return (
                      <button
                        key={lvl}
                        type="button"
                        data-testid={`cap-btn-${cap.key}-${lvl}`}
                        disabled={!canConfigure || lockedL2 || updateCapabilityMutation.isPending}
                        onClick={() =>
                          updateCapabilityMutation.mutate({
                            capability: cap.key,
                            autonomy_level: lvl,
                          })
                        }
                        className={`rounded-md border px-2 py-1.5 text-xs font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-45 ${
                          active
                            ? "border-indigo-600 bg-indigo-600 text-white"
                            : "border-slate-200 bg-slate-50 text-slate-700 hover:bg-slate-100"
                        }`}
                      >
                        {lvl}
                      </button>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>

        <div className="rounded-lg border border-slate-200 bg-white p-4">
          <h2 className="text-sm font-bold text-slate-900">
            Knowledge Base Documents (BM25 + pgvector HNSW + Cross-Encoder Rerank)
          </h2>

          <div className="mt-3 flex flex-wrap items-center gap-2">
            <input
              type="text"
              value={docTitle}
              onChange={(e) => setDocTitle(e.target.value)}
              placeholder="Document title (optional)"
              className="rounded border border-slate-300 px-2.5 py-1.5 text-xs"
            />
            <input
              type="file"
              accept=".pdf,.md,.txt,.docx"
              onChange={(e) => setSelectedFile(e.target.files?.[0] ?? null)}
              className="text-xs"
            />
            <button
              type="button"
              disabled={!selectedFile || uploadKbMutation.isPending}
              onClick={() => uploadKbMutation.mutate()}
              className="inline-flex items-center gap-1 rounded bg-indigo-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-indigo-700 disabled:opacity-50"
            >
              <Upload className="h-3.5 w-3.5" />
              <span>Upload & Index</span>
            </button>
          </div>

          <div className="mt-4 divide-y divide-slate-100">
            {(kbQuery.data?.items ?? []).map((doc) => (
              <div key={doc.id} className="flex items-center justify-between py-2 text-xs">
                <div>
                  <span className="font-semibold text-slate-900">{doc.title}</span>
                  <span className="ml-2 text-slate-500">{doc.chunk_count} chunks</span>
                  <span className="ml-2 rounded bg-emerald-50 px-1.5 py-0.5 text-emerald-700">
                    {doc.status}
                  </span>
                </div>
                <button
                  type="button"
                  onClick={() => deleteKbMutation.mutate(doc.id)}
                  className="rounded p-1 text-rose-600 hover:bg-rose-50"
                  aria-label={`Delete ${doc.title}`}
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </div>
            ))}
          </div>
        </div>
      </div>
    </AppShell>
  );
}
