"use client";

import React, { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Blocks,
  BookOpen,
  Layers,
  Lock,
  ShieldCheck,
  Trash2,
  Upload,
  Wallet,
} from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { useAuthSession } from "@/components/providers";
import { Badge, Card, CardHeader, EmptyState, Page, PageHeader } from "@/components/ui/page";
import { BentoStatCard } from "@/components/ui/bento-stat-card";
import { deleteKbDocument, updateCapability, uploadKbDocument } from "@/lib/api-client/live-client";
import {
  fetchCapabilities,
  fetchKbDocuments,
  fetchPendingProposalsCount,
  fetchWallet,
} from "@/lib/data-source";
import { canConfigureAiGovernance } from "@/lib/auth-cookie";
import { useI18n } from "@/lib/i18n";
import type { AiCapability, AutonomyLevel } from "@/lib/contracts";

const CAPABILITY_METADATA: Array<{
  key: AiCapability;
  titleZh: string;
  titleEn: string;
  descZh: string;
  descEn: string;
}> = [
  {
    key: "cs_support",
    titleZh: "1. AI 客服支持",
    titleEn: "1. AI customer support",
    descZh: "知识库引用作答、售前售后草稿生成、平台合规话术过滤。",
    descEn: "FAQ-grounded answers, pre/after-sales drafting and platform compliance filtering.",
  },
  {
    key: "sales_quoting",
    titleZh: "2. AI 销售与报价",
    titleEn: "2. AI sales & quoting",
    descZh: "B2B 询盘要素抽取、MOQ 阶梯价计算、形式发票草稿提案。",
    descEn: "B2B RFQ extraction, MOQ tier pricing and PI draft proposals.",
  },
  {
    key: "order_logistics",
    titleZh: "3. AI 订单与物流",
    titleEn: "3. AI orders & logistics",
    descZh: "确定性 WISMO 查单、17TRACK 72 小时停滞主动跟进。",
    descEn: "Deterministic WISMO lookup and 17TRACK 72h stagnation outreach.",
  },
  {
    key: "content_studio",
    titleZh: "4. AI 内容工坊",
    titleEn: "4. AI content studio",
    descZh: "多语言商品文案与合规 WhatsApp 模板草稿。",
    descEn: "Multilingual product copy and compliant WhatsApp template drafts.",
  },
  {
    key: "biz_analytics",
    titleZh: "5. AI 经营分析",
    titleEn: "5. AI business analytics",
    descZh: "只读 CVP 毛利诊断与库存周转问答。",
    descEn: "Read-only CVP margin diagnostics and inventory turnover Q&A.",
  },
  {
    key: "finance_recon",
    titleZh: "6. AI 财务对账",
    titleEn: "6. AI finance recon",
    descZh: "T/T 水单 OCR 匹配提案；资金核销严格限定 L0 人工执行。",
    descEn: "T/T slip OCR matching proposals; settlement stays strictly L0 human-only.",
  },
];

const AUTONOMY_LEVELS: AutonomyLevel[] = ["L0_copilot", "L1_proposal", "L2_auto"];

const LEVEL_COPY: Record<AutonomyLevel, { zh: string; en: string }> = {
  L0_copilot: { zh: "L0 人工把关", en: "L0 human-only" },
  L1_proposal: { zh: "L1 提案人审", en: "L1 human review" },
  L2_auto: { zh: "L2 自动执行", en: "L2 auto" },
};

export default function AiCapabilitiesPage() {
  const { principal } = useAuthSession();
  const { bi } = useI18n();
  const queryClient = useQueryClient();
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [docTitle, setDocTitle] = useState("");

  const capabilitiesQuery = useQuery({
    queryKey: ["ai", "capabilities"],
    queryFn: () => fetchCapabilities(),
  });

  // The billing wallet contract is owner/admin/finance only; agents and managers
  // can still open this page and read capability governance.
  const canReadWallet =
    principal?.role === "owner" || principal?.role === "admin" || principal?.role === "finance";
  const walletQuery = useQuery({
    queryKey: ["ai", "wallet"],
    queryFn: () => fetchWallet(),
    enabled: canReadWallet,
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
  const isL0Fallback = Boolean(wallet && wallet.auto_downgrade_to_l0 && remainingCredits <= 0);

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
      <Page>
        <PageHeader
          title={bi("AI 治理矩阵与混合 RAG 知识库", "AI governance matrix & hybrid RAG knowledge base")}
          description={bi(
            "六项 AI 能力的 L0 / L1 / L2 自治矩阵（不提供 L3 全自动旁路）。实时价格与库存必须由 SQL 工具返回，禁止静态 RAG 编造。",
            "Autonomy matrix for six AI capabilities (no L3 bypass). Live prices and stock must come from SQL tools, never static RAG."
          )}
          badge={
            <Badge tone="success">
              <ShieldCheck className="h-3 w-3" />
              {bi("零幻觉接地", "Zero-hallucination grounding")}
            </Badge>
          }
          meta={
            wallet ? (
              <>
                <Badge tone="info">
                  <Wallet className="h-3 w-3" />
                  {bi("套餐", "Plan")} {wallet.plan_tier}
                </Badge>
                <Badge tone={remainingCredits > 0 ? "success" : "danger"}>
                  {bi("剩余额度", "Credits")} {remainingCredits}
                </Badge>
              </>
            ) : undefined
          }
        />

        {isL0Fallback && (
          <div
            data-testid="wallet-l0-fallback-alert"
            className="flex items-start gap-2 rounded-xl border border-amber-200 bg-amber-50 px-3.5 py-2.5 text-[12px] font-medium text-amber-900"
          >
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
            <span>
              {bi(
                "额度已用尽，AI 自动降级为 L0 纯人工模式；收发消息不受影响，仍 100% 在线。",
                "Credit wallet depleted — AI degraded to L0 human-only mode. Messaging stays 100% online."
              )}
            </span>
          </div>
        )}

        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <BentoStatCard
            label={bi("AI 能力项", "AI capabilities")}
            value={String(capabilitiesQuery.data?.capabilities.length ?? CAPABILITY_METADATA.length)}
            icon={Blocks}
            accent="blue"
            badge={bi("全栈可用", "Full stack")}
            hint={bi(
              "客服、报价、物流、内容、分析、财务对账",
              "Support, quoting, logistics, content, analytics, finance recon"
            )}
          />
          <BentoStatCard
            label={bi("自治级别", "Autonomy levels")}
            value="3"
            icon={Layers}
            accent="violet"
            badge={bi("无 L3 旁路", "No L3 bypass")}
            badgeTone="violet"
            hint={bi("L0 人工把关 / L1 提案人审 / L2 自动执行", "L0 human / L1 review / L2 auto")}
          />
          <BentoStatCard
            label={bi("永久锁定项", "Permanently locked")}
            value="1"
            icon={Lock}
            accent="rose"
            badge={bi("资金核销", "Cash settlement")}
            badgeTone="rose"
            hint={bi("财务对账永不开放 L2 自动执行", "Finance recon never runs at L2")}
          />
          <BentoStatCard
            label={bi("知识库文档", "Knowledge docs")}
            value={String(kbQuery.data?.items.length ?? "—")}
            icon={BookOpen}
            accent="emerald"
            badge={bi("混合检索", "Hybrid RAG")}
            hint={bi("BM25 + pgvector + 重排，低置信度转人工", "BM25 + pgvector + rerank; low confidence escalates")}
          />
        </div>

        <div className="grid gap-3 md:grid-cols-2 2xl:grid-cols-3">
          {CAPABILITY_METADATA.map((cap) => {
            const currentLevel = levelMap.get(cap.key) ?? "L1_proposal";
            const isFinance = cap.key === "finance_recon";
            const levelCopy = LEVEL_COPY[currentLevel] ?? { zh: currentLevel, en: currentLevel };

            return (
              <Card
                key={cap.key}
                className="rounded-2xl border border-slate-200/90 p-4 shadow-[0_1px_3px_rgba(15,23,42,0.06),0_6px_16px_-6px_rgba(15,23,42,0.05)] transition-shadow hover:shadow-[0_6px_20px_-10px_rgba(15,23,42,0.2)]"
              >
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <h2 className="text-sm font-semibold text-slate-900">
                      {bi(cap.titleZh, cap.titleEn)}
                    </h2>
                    <p className="mt-1 text-[12px] leading-5 text-slate-500">
                      {bi(cap.descZh, cap.descEn)}
                    </p>
                  </div>
                  {isFinance && (
                    <span
                      data-testid="finance-recon-l0-lock"
                      className="inline-flex shrink-0 items-center gap-1 rounded-full border border-rose-200 bg-rose-50 px-2 py-0.5 text-[10px] font-medium text-rose-700"
                    >
                      <Lock className="h-3 w-3" />
                      <span>L0 Settlement Lock</span>
                    </span>
                  )}
                </div>

                <div className="mt-3 flex items-center justify-between text-[11px] text-slate-500">
                  <span>{bi("当前自治级别", "Current autonomy")}</span>
                  <span className="font-medium text-slate-700">
                    {bi(levelCopy.zh, levelCopy.en)}
                  </span>
                </div>

                <div className="mt-2 grid grid-cols-3 gap-1 rounded-xl border border-slate-200/90 bg-slate-100/90 p-1">
                  {AUTONOMY_LEVELS.map((lvl) => {
                    const active = currentLevel === lvl;
                    const lockedL2 = isFinance && lvl === "L2_auto";
                    const copy = LEVEL_COPY[lvl];
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
                        className={`rounded-lg px-2 py-1.5 text-[12px] font-semibold transition-all duration-150 disabled:cursor-not-allowed disabled:opacity-45 ${
                          active
                            ? "bg-slate-900 text-white shadow-xs"
                            : "text-slate-600 hover:bg-white/80 hover:text-slate-950"
                        }`}
                      >
                        {bi(copy.zh, copy.en)}
                      </button>
                    );
                  })}
                </div>

                {isFinance && (
                  <p className="mt-2 text-[11px] text-slate-400">
                    {bi(
                      "资金动作不允许 L2 自动执行，L2 按钮永久锁定。",
                      "Cash actions can never run at L2; the L2 button stays locked."
                    )}
                  </p>
                )}
              </Card>
            );
          })}
        </div>

        <Card>
          <CardHeader
            icon={BookOpen}
            title={bi(
              "知识库文档（BM25 + pgvector HNSW + Cross-Encoder 重排）",
              "Knowledge base documents (BM25 + pgvector HNSW + cross-encoder rerank)"
            )}
            description={bi(
              "上传的保修政策、认证证书与产品规格将用于严格检索模式；低置信度一律转人工。",
              "Warranty policies, certificates and specs feed strict retrieval mode; low confidence always escalates to a human."
            )}
            actions={
              <Badge tone="info">
                {kbQuery.data?.items.length ?? 0} {bi("篇文档", "documents")}
              </Badge>
            }
          />

          <div className="flex flex-wrap items-center gap-2 border-b border-slate-100 p-4">
            <input
              type="text"
              value={docTitle}
              onChange={(e) => setDocTitle(e.target.value)}
              placeholder={bi("文档标题（可选）", "Document title (optional)")}
              aria-label={bi("文档标题", "Document title")}
              className="erp-input w-64"
            />
            <input
              type="file"
              accept=".pdf,.md,.txt,.docx"
              onChange={(e) => setSelectedFile(e.target.files?.[0] ?? null)}
              aria-label={bi("选择知识库文件", "Choose knowledge-base file")}
              className="max-w-64 text-[12px] text-slate-500"
            />
            <button
              type="button"
              disabled={!selectedFile || uploadKbMutation.isPending}
              onClick={() => uploadKbMutation.mutate()}
              className="erp-btn erp-btn-primary disabled:opacity-50"
            >
              <Upload className="h-3.5 w-3.5" />
              <span>{bi("上传并索引", "Upload & index")}</span>
            </button>
          </div>

          <div className="divide-y divide-slate-100">
            {(kbQuery.data?.items ?? []).map((doc) => (
              <div
                key={doc.id}
                className="flex flex-wrap items-center justify-between gap-2 px-4 py-2.5 text-[13px]"
              >
                <div className="flex min-w-0 flex-wrap items-center gap-2">
                  <span className="font-medium text-slate-900">{doc.title}</span>
                  <span className="tabular-nums text-[12px] text-slate-500">
                    {doc.chunk_count} {bi("个分块", "chunks")}
                  </span>
                  <Badge tone="success">{doc.status}</Badge>
                </div>
                <button
                  type="button"
                  onClick={() => deleteKbMutation.mutate(doc.id)}
                  className="rounded-md p-1.5 text-rose-600 hover:bg-rose-50"
                  aria-label={`${bi("删除", "Delete")} ${doc.title}`}
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </div>
            ))}
            {(kbQuery.data?.items ?? []).length === 0 && (
              <EmptyState className="m-4">
                {bi("知识库还没有文档。", "The knowledge base has no documents yet.")}
              </EmptyState>
            )}
          </div>
        </Card>

        <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-slate-800 bg-[#0B1220] px-5 py-4 text-white shadow-[0_10px_30px_-18px_rgba(15,23,42,0.6)]">
          <div className="flex min-w-0 items-center gap-3">
            <span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-white/10 text-emerald-300 ring-1 ring-white/15">
              <ShieldCheck className="h-4 w-4" aria-hidden="true" />
            </span>
            <div className="min-w-0">
              <p className="text-[13px] font-bold">{bi("治理红线", "Governance guardrails")}</p>
              <p className="mt-0.5 text-[11.5px] leading-5 text-slate-300">
                {bi(
                  "财务对账与资金核销永久锁定 L0；所有 L1 提案必须进入人工审核队列后才回写 ERP。",
                  "Finance recon and settlement stay permanently L0; every L1 proposal must pass human review before writing back to the ERP."
                )}
              </p>
            </div>
          </div>
          <span className="inline-flex shrink-0 items-center gap-1.5 rounded-full border border-white/15 bg-white/10 px-3 py-1 text-[11.5px] font-semibold tabular-nums">
            {bi("6 项能力 · 3 级自治", "6 capabilities · 3 levels")}
          </span>
        </div>
      </Page>
    </AppShell>
  );
}
