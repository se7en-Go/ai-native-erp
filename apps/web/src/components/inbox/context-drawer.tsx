"use client";

import React, { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowUpRight,
  Building2,
  ClipboardPaste,
  Fingerprint,
  GitMerge,
  Mail,
  Sparkles,
  Split,
  PackageSearch,
  FileText,
  Target,
  Truck,
} from "lucide-react";
import {
  getInboxCustomerContext,
  liveCheckSku,
  mergeCustomers,
  splitCustomers,
} from "@/lib/api-client/live-client";
import type { LiveSkuCheckRead } from "@/lib/api-client/types.gen";
import {
  fetchCustomerContext,
  type CustomerContextPayload,
} from "@/lib/api-client/plan04-bridge";
import {
  buyerTypeLabel,
  clampIntentScore,
  extractCustomerAiProfile,
  fetchCustomerAiProfile,
  priceSensitivityLabel,
  type CustomerAiProfile,
} from "@/lib/api-client/pack-e-autoreply";
import { formatMoney, type Incoterm } from "@/lib/contracts";
import { fetchCatalogVariants, shouldUseMockApi } from "@/lib/data-source";
import { useI18n } from "@/lib/i18n";

export type { CustomerContextPayload };

function fmtQty(value: number): string {
  return new Intl.NumberFormat("en-US").format(value);
}

/**
 * 把一次确定性 SQL 查库结果拼成可直接粘给买家的报价片段：
 * SKU + 实时库存 + 命中阶梯单价 + 交期。
 */
export function buildSkuReplySnippet(
  result: Pick<
    LiveSkuCheckRead,
    "sku" | "available_qty" | "unit_price_quote_ccy" | "moq_tier_applied" | "incoterm"
  >,
  leadTimeDays: number | null,
  locale: "zh" | "en",
): string {
  const price = formatMoney(result.unit_price_quote_ccy, "USD");
  const moq = result.moq_tier_applied ? `MOQ ${fmtQty(result.moq_tier_applied)}` : "MOQ";
  if (locale === "zh") {
    const lines = [
      `${result.sku}：现货 ${fmtQty(result.available_qty)} pcs`,
      `${moq} 阶梯价 ${price}/pc（${result.incoterm}）`,
    ];
    if (leadTimeDays !== null) lines.push(`交期：${leadTimeDays} 天`);
    return lines.join("\n");
  }
  const lines = [
    `${result.sku} — ${fmtQty(result.available_qty)} pcs in stock`,
    `${moq} tier price ${price}/pc (${result.incoterm})`,
  ];
  if (leadTimeDays !== null) lines.push(`Lead time: ${leadTimeDays} days`);
  return lines.join("\n");
}

export function ContextDrawer({
  customerId,
  conversationId = null,
  onInsertReplyText,
}: {
  customerId: string;
  /**
   * Pack E：当前会话 ID（可选）。画像提炼需要会话上下文，
   * 旧调用方不传也能正常渲染，只是提炼按钮置灰。
   */
  conversationId?: string | null;
  /** 把报价/库存片段直接插进 Inbox 回复框（可选，缺省时按钮不显示）。 */
  onInsertReplyText?: (text: string) => void;
}) {
  const { bi, locale } = useI18n();
  const queryClient = useQueryClient();
  const [activeTab, setActiveTab] = useState<"orders" | "quotes" | "sku" | "profile">(
    "orders",
  );
  const [mergeDialogOpen, setMergeDialogOpen] = useState(false);
  const [mergeReason, setMergeReason] = useState("确认买家为同一主体");
  const [skuInput, setSkuInput] = useState("");
  const [qtyInput, setQtyInput] = useState(100);
  const [incotermInput, setIncotermInput] = useState<Incoterm>("FOB");
  const [liveSkuResult, setLiveSkuResult] = useState<LiveSkuCheckRead | null>(null);

  const contextQuery = useQuery({
    queryKey: ["identity", "context", customerId],
    enabled: Boolean(customerId),
    queryFn: (): Promise<CustomerContextPayload> => shouldUseMockApi()
      ? fetchCustomerContext(customerId) : getInboxCustomerContext(customerId),
  });

  const mergeMutation = useMutation({
    mutationFn: (sourceCustomerId: string) =>
      mergeCustomers(sourceCustomerId, customerId, mergeReason),
    onSuccess: () => {
      setMergeDialogOpen(false);
      void queryClient.invalidateQueries({ queryKey: ["identity", "context", customerId] });
    },
  });

  const splitMutation = useMutation({
    mutationFn: (mergeEventId: string) =>
      splitCustomers(mergeEventId, "人工操作拆分"),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["identity", "context", customerId] });
    },
  });

  const liveSkuMutation = useMutation({
    mutationFn: () => liveCheckSku(skuInput.trim(), qtyInput, incotermInput),
    onSuccess: (data) => {
      setLiveSkuResult(data);
    },
  });

  // Pack E：AI 客户画像（懒加载，仅切到画像标签页才请求）。
  const profileQuery = useQuery({
    queryKey: ["ai", "customer-profile", customerId],
    enabled: Boolean(customerId) && activeTab === "profile",
    retry: false,
    queryFn: (): Promise<CustomerAiProfile> => fetchCustomerAiProfile(customerId),
  });

  const extractProfileMutation = useMutation({
    mutationFn: (targetConversationId: string) =>
      extractCustomerAiProfile(targetConversationId, true),
    onSuccess: (profile) => {
      queryClient.setQueryData(["ai", "customer-profile", profile.customer_id], profile);
      queryClient.setQueryData(["ai", "customer-profile", customerId], profile);
      void queryClient.invalidateQueries({ queryKey: ["crm", "leads"] });
    },
  });

  // 交期不在 live-check 响应里：按 SKU 回查商品档案拿真实 lead_time_days。
  const variantsQuery = useQuery({
    queryKey: ["catalog", "variants", "drawer"],
    queryFn: () => fetchCatalogVariants(""),
    enabled: activeTab === "sku",
  });

  const matchedLeadTimeDays = (() => {
    if (!liveSkuResult) return null;
    const variant = (variantsQuery.data?.items ?? []).find(
      (item) => item.sku === liveSkuResult.sku,
    );
    return variant ? variant.lead_time_days : null;
  })();

  const data = contextQuery.data;
  // 提炼刚返回的画像优先于缓存里的旧版本。
  const profile: CustomerAiProfile | null =
    extractProfileMutation.data ?? profileQuery.data ?? null;
  const intentScore = clampIntentScore(profile?.purchase_intent_score);
  const profileSynced =
    Boolean(profile?.synced_lead_id) || Boolean(profile?.synced_followup_id);

  return (
    <aside
      data-testid="inbox-context-drawer"
      className="hidden w-80 shrink-0 flex-col overflow-y-auto border-l border-slate-200/90 bg-white p-3 2xl:flex 2xl:w-88 dark:border-gray-800 dark:bg-gray-900"
    >
      {contextQuery.isLoading && (
        <div className="text-[12px] text-slate-400">{bi("正在加载客户档案…", "Loading customer…")}</div>
      )}
      {data && (
        <>
          <div className="rounded-2xl border border-slate-200/90 bg-slate-50/60 p-3">
            <div className="flex items-start justify-between gap-2">
              <div>
                <h3 className="text-[13.5px] font-bold text-slate-900">
                  {data.customer.display_name}
                </h3>
                <div className="mt-1 flex flex-wrap gap-1 text-[11px] font-medium">
                  <span className="rounded-md bg-blue-50 px-1.5 py-0.5 text-blue-700 ring-1 ring-blue-100">
                    {data.customer.customer_type}
                  </span>
                  <span className="rounded-md bg-white px-1.5 py-0.5 tabular-nums text-slate-700 ring-1 ring-slate-200">
                    LTV {formatMoney(data.customer.ltv_usd, "USD")}
                  </span>
                </div>
              </div>

              {(data.probable_duplicates.length > 0 || data.merge_events.length > 0) && (
                <button
                  type="button"
                  data-testid="open-merge-dialog-btn"
                  onClick={() => setMergeDialogOpen(true)}
                  className="inline-flex items-center gap-1 rounded-lg border border-amber-300 bg-amber-50 px-2 py-1 text-[11px] font-semibold text-amber-900 shadow-xs transition-colors hover:bg-amber-100"
                >
                  <GitMerge className="h-3 w-3" />
                  <span>{bi("合并 / 拆分", "Merge / split")} ({data.probable_duplicates.length})</span>
                </button>
              )}
            </div>

            {data.company && (
              <div className="mt-2 rounded-xl border border-slate-200 bg-white p-2.5 text-xs">
                <div className="flex items-center gap-1.5 font-semibold text-slate-800">
                  <Building2 className="h-3.5 w-3.5 text-slate-500" />
                  <span>{data.company.legal_name}</span>
                  <span>({data.company.country_code})</span>
                </div>
                <div className="mt-0.5 text-[11px] text-slate-600">
                  {bi("信用等级", "Credit")} {data.company.credit_tier} · {bi("默认贸易术语", "Default incoterm")}{" "}
                  {data.company.default_incoterm}
                </div>
              </div>
            )}

            <div className="mt-2.5 space-y-1">
              <div className="flex items-center gap-1 text-[11px] font-semibold text-slate-600">
                <Fingerprint className="h-3 w-3" />
                <span>{bi("统一身份图谱", "Unified identity graph")}</span>
              </div>
              <div className="flex flex-wrap gap-1">
                {data.identities.map((ident) => (
                  <span
                    key={ident.id}
                    data-testid={`identity-badge-${ident.identity_type}`}
                    className={`inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] font-medium ring-1 ${
                      ident.identity_type === "whatsapp_bsuid"
                        ? "bg-emerald-50 text-emerald-800 ring-emerald-200"
                        : "bg-slate-100 text-slate-700 ring-slate-200"
                    }`}
                  >
                    <span className="font-semibold">{ident.identity_type}:</span>
                    <span>{ident.identity_value}</span>
                  </span>
                ))}
              </div>
            </div>
          </div>

          <div className="mt-3 grid grid-cols-2 gap-0.5 rounded-xl border border-slate-200/80 bg-slate-100/80 p-1 text-[11px] font-semibold dark:border-gray-700 dark:bg-gray-800/80">
            <button
              type="button"
              onClick={() => setActiveTab("orders")}
              aria-pressed={activeTab === "orders"}
              className={`rounded-lg py-1 transition-colors ${
                activeTab === "orders"
                  ? "bg-white text-blue-700 shadow-xs ring-1 ring-slate-200/80 dark:bg-gray-900 dark:text-blue-300 dark:ring-gray-700"
                  : "text-slate-600 hover:text-slate-900 dark:text-gray-400 dark:hover:text-gray-100"
              }`}
            >
              {bi("B2C 订单与物流", "B2C orders + 17TRACK")}
            </button>
            <button
              type="button"
              onClick={() => setActiveTab("quotes")}
              aria-pressed={activeTab === "quotes"}
              className={`rounded-lg py-1 transition-colors ${
                activeTab === "quotes"
                  ? "bg-white text-blue-700 shadow-xs ring-1 ring-slate-200/80 dark:bg-gray-900 dark:text-blue-300 dark:ring-gray-700"
                  : "text-slate-600 hover:text-slate-900 dark:text-gray-400 dark:hover:text-gray-100"
              }`}
            >
              {bi("B2B 报价与 PI", "B2B quotes & PI")}
            </button>
            <button
              type="button"
              onClick={() => setActiveTab("sku")}
              aria-pressed={activeTab === "sku"}
              className={`rounded-lg py-1 transition-colors ${
                activeTab === "sku"
                  ? "bg-white text-blue-700 shadow-xs ring-1 ring-slate-200/80 dark:bg-gray-900 dark:text-blue-300 dark:ring-gray-700"
                  : "text-slate-600 hover:text-slate-900 dark:text-gray-400 dark:hover:text-gray-100"
              }`}
            >
              {bi("实时库存与 MOQ", "Live SKU & MOQ stock")}
            </button>
            <button
              type="button"
              data-testid="context-tab-profile"
              onClick={() => setActiveTab("profile")}
              aria-pressed={activeTab === "profile"}
              className={`inline-flex items-center justify-center gap-1 rounded-lg py-1 transition-colors ${
                activeTab === "profile"
                  ? "bg-white text-blue-700 shadow-xs ring-1 ring-slate-200/80 dark:bg-gray-900 dark:text-blue-300 dark:ring-gray-700"
                  : "text-slate-600 hover:text-slate-900 dark:text-gray-400 dark:hover:text-gray-100"
              }`}
            >
              <Sparkles className="h-3 w-3" />
              {bi("AI 客户画像", "AI buyer profile")}
            </button>
          </div>

          <div className="mt-3 flex-1 space-y-2">
            {activeTab === "profile" && (
              <div data-testid="context-profile-panel" className="space-y-2">
                <button
                  type="button"
                  data-testid="extract-crm-profile-btn"
                  disabled={!conversationId || extractProfileMutation.isPending}
                  onClick={() => {
                    if (conversationId) extractProfileMutation.mutate(conversationId);
                  }}
                  className="inline-flex w-full items-center justify-center gap-1.5 rounded-xl bg-blue-600 px-3 py-2 text-[12px] font-semibold text-white shadow-[0_2px_10px_-4px_rgba(37,99,235,0.55)] transition-colors hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-blue-500 dark:hover:bg-blue-400"
                >
                  <Sparkles className="h-3.5 w-3.5" />
                  <span>
                    {extractProfileMutation.isPending
                      ? bi("正在提炼画像…", "Extracting profile…")
                      : bi("AI 提炼画像并同步 CRM", "Extract profile & sync CRM")}
                  </span>
                </button>

                {!conversationId && (
                  <p
                    data-testid="profile-needs-conversation"
                    className="rounded-xl border border-slate-200 bg-slate-50/70 px-2.5 py-1.5 text-[11px] font-medium text-slate-600 dark:border-gray-700 dark:bg-gray-800/70 dark:text-gray-300"
                  >
                    {bi(
                      "从一条会话进入即可一键提炼：AI 会读完整个沟通上下文。",
                      "Open a conversation to extract: the AI reads the whole thread."
                    )}
                  </p>
                )}

                {extractProfileMutation.isError && (
                  <p
                    role="alert"
                    data-testid="profile-extract-error"
                    className="rounded-xl border border-rose-200 bg-rose-50 px-2.5 py-1.5 text-[11px] font-medium text-rose-800 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-300"
                  >
                    {extractProfileMutation.error instanceof Error &&
                    extractProfileMutation.error.message
                      ? extractProfileMutation.error.message
                      : bi("画像提炼失败，请重试。", "Profile extraction failed. Please retry.")}
                  </p>
                )}

                {profileSynced && (
                  <div
                    data-testid="profile-crm-sync-notice"
                    className="rounded-xl border border-emerald-200 bg-emerald-50 px-2.5 py-2 text-[11px] font-medium text-emerald-900 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-300"
                  >
                    <p className="font-semibold">
                      {bi("已同步 CRM：线索 + 跟进已创建", "Synced to CRM: lead + follow-up created")}
                    </p>
                    <p className="mt-0.5 tabular-nums">
                      {bi("线索", "Lead")} {profile?.synced_lead_id ?? "—"}
                      <span className="mx-1 text-emerald-400">·</span>
                      {bi("跟进", "Follow-up")} {profile?.synced_followup_id ?? "—"}
                    </p>
                  </div>
                )}

                {profile ? (
                  <div
                    data-testid="customer-ai-profile"
                    className="space-y-2 rounded-xl border border-slate-200 bg-white p-2.5 text-xs shadow-[0_1px_2px_rgba(15,23,42,0.04)] dark:border-gray-700 dark:bg-gray-900"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="inline-flex items-center gap-1 font-semibold text-slate-800 dark:text-gray-100">
                        <Target className="h-3.5 w-3.5 text-blue-600 dark:text-blue-400" />
                        {bi("采购意向分", "Purchase intent")}
                      </span>
                      <span
                        data-testid="profile-intent-score"
                        className="font-bold tabular-nums text-blue-700 dark:text-blue-300"
                      >
                        {intentScore}
                        <span className="text-[10px] font-medium text-slate-400 dark:text-gray-500">
                          /100
                        </span>
                      </span>
                    </div>
                    <div
                      role="progressbar"
                      aria-valuenow={intentScore}
                      aria-valuemin={0}
                      aria-valuemax={100}
                      aria-label={bi("采购意向分", "Purchase intent score")}
                      className="h-1.5 w-full overflow-hidden rounded-full bg-slate-100 dark:bg-gray-800"
                    >
                      <div
                        data-testid="profile-intent-bar"
                        className="h-full rounded-full bg-gradient-to-r from-blue-500 to-blue-600 transition-[width] duration-500 dark:from-blue-400 dark:to-blue-500"
                        style={{ width: `${intentScore}%` }}
                      />
                    </div>

                    <dl className="grid grid-cols-2 gap-1">
                      {[
                        [bi("买家类型", "Buyer type"), buyerTypeLabel(profile.buyer_type, locale)],
                        [
                          bi("国家 / 语种", "Country / language"),
                          `${profile.country_code ?? "—"} · ${(profile.preferred_language ?? "—").toUpperCase()}`,
                        ],
                        [
                          bi("预估采购额", "Est. order value"),
                          profile.estimated_order_value_usd
                            ? formatMoney(profile.estimated_order_value_usd, "USD")
                            : "—",
                        ],
                        [bi("目标贸易术语", "Target incoterm"), profile.target_incoterm ?? "—"],
                        [
                          bi("价格敏感度", "Price sensitivity"),
                          priceSensitivityLabel(profile.price_sensitivity, locale),
                        ],
                      ].map(([term, value]) => (
                        <div
                          key={term}
                          className="rounded-lg bg-slate-50/80 px-2 py-1 dark:bg-gray-800/60"
                        >
                          <dt className="text-[10px] font-medium text-slate-500 dark:text-gray-400">
                            {term}
                          </dt>
                          <dd className="truncate text-[11.5px] font-semibold text-slate-900 dark:text-gray-100">
                            {value}
                          </dd>
                        </div>
                      ))}
                    </dl>

                    {(profile.interested_skus ?? []).length > 0 && (
                      <div>
                        <p className="mb-1 inline-flex items-center gap-1 text-[10.5px] font-semibold text-slate-600 dark:text-gray-300">
                          <PackageSearch className="h-3 w-3 text-blue-600 dark:text-blue-400" />
                          {bi("感兴趣 SKU", "Interested SKUs")}
                        </p>
                        <div className="flex flex-wrap gap-1">
                          {(profile.interested_skus ?? []).map((sku) => (
                            <span
                              key={sku}
                              data-testid={`profile-sku-${sku}`}
                              className="rounded-md bg-blue-50 px-1.5 py-0.5 text-[10.5px] font-semibold text-blue-700 ring-1 ring-blue-200 dark:bg-blue-500/10 dark:text-blue-300 dark:ring-blue-500/30"
                            >
                              {sku}
                            </span>
                          ))}
                        </div>
                      </div>
                    )}

                    {profile.ai_summary && (
                      <p className="rounded-lg bg-slate-50/80 px-2 py-1.5 text-[11px] leading-relaxed text-slate-700 dark:bg-gray-800/60 dark:text-gray-300">
                        {profile.ai_summary}
                      </p>
                    )}

                    {(profile.key_requirements ?? []).length > 0 && (
                      <div>
                        <p className="mb-1 text-[10.5px] font-semibold text-slate-600 dark:text-gray-300">
                          {bi("核心需求", "Key requirements")}
                        </p>
                        <ul className="space-y-0.5">
                          {(profile.key_requirements ?? []).map((item) => (
                            <li
                              key={item}
                              className="flex items-start gap-1 text-[11px] text-slate-700 dark:text-gray-300"
                            >
                              <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-blue-500" />
                              <span>{item}</span>
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}

                    {(profile.pain_points ?? []).length > 0 && (
                      <div>
                        <p className="mb-1 text-[10.5px] font-semibold text-slate-600 dark:text-gray-300">
                          {bi("痛点", "Pain points")}
                        </p>
                        <ul className="space-y-0.5">
                          {(profile.pain_points ?? []).map((item) => (
                            <li
                              key={item}
                              className="flex items-start gap-1 text-[11px] text-slate-700 dark:text-gray-300"
                            >
                              <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-amber-500" />
                              <span>{item}</span>
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}

                    <div
                      data-testid="profile-next-best-action"
                      className="rounded-xl border border-blue-200 bg-blue-50/70 px-2.5 py-2 dark:border-blue-500/30 dark:bg-blue-500/10"
                    >
                      <p className="inline-flex items-center gap-1 text-[10.5px] font-semibold text-blue-800 dark:text-blue-300">
                        <ArrowUpRight className="h-3 w-3" />
                        {bi("下一步建议动作", "Next best action")}
                      </p>
                      <p className="mt-0.5 text-[11.5px] font-medium leading-relaxed text-blue-900 dark:text-blue-200">
                        {profile.next_best_action}
                      </p>
                    </div>

                    {(profile.tags ?? []).length > 0 && (
                      <div className="flex flex-wrap gap-1">
                        {(profile.tags ?? []).map((tag) => (
                          <span
                            key={tag}
                            className="rounded-full bg-slate-100 px-1.5 py-0.5 text-[10px] font-semibold text-slate-600 dark:bg-gray-800 dark:text-gray-300"
                          >
                            {tag}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                ) : (
                  <p
                    data-testid="profile-empty-hint"
                    className="rounded-xl border border-dashed border-slate-300 bg-slate-50/70 px-2.5 py-2 text-[11px] font-medium text-slate-600 dark:border-gray-700 dark:bg-gray-800/60 dark:text-gray-300"
                  >
                    {profileQuery.isLoading
                      ? bi("正在读取画像…", "Loading profile…")
                      : bi(
                          "该客户还没有 AI 画像。点上方按钮从整段沟通一键提炼并同步 CRM。",
                          "No AI profile yet. Use the button above to extract it from the thread and sync CRM."
                        )}
                  </p>
                )}
              </div>
            )}

            {activeTab === "orders" &&
              data.recent_orders.map((ord) => (
                <div
                  key={ord.id}
                  className="rounded-xl border border-slate-200 bg-white p-2.5 text-xs shadow-[0_1px_2px_rgba(15,23,42,0.04)]"
                >
                  <div className="flex items-center justify-between font-semibold text-slate-900">
                    <Link href={`/orders?orderId=${ord.id}`} className="text-blue-600 hover:underline">
                      {ord.order_no}
                    </Link>
                    <span>{formatMoney(ord.total_amount, ord.currency)}</span>
                  </div>
                  <div className="mt-1 flex flex-wrap gap-1 text-[11px]">
                    <span className="rounded bg-slate-100 px-1.5 py-0.5">{ord.fulfillment_status}</span>
                    <span className="rounded bg-slate-100 px-1.5 py-0.5">{ord.payment_status}</span>
                  </div>
                  {ord.tracking_number && (
                    <div className="mt-1.5 flex items-center gap-1 text-[11px] text-slate-600">
                      <Truck className="h-3 w-3" />
                      <span>
                        {ord.tracking_number} · {ord.standardized_status ?? bi("待更新", "Pending")}
                        {ord.sub_status ? ` (${ord.sub_status})` : ""}
                      </span>
                    </div>
                  )}
                </div>
              ))}

            {activeTab === "quotes" && (
              <div className="space-y-2">
                <Link
                  data-testid="context-new-quote-link"
                  href={`/b2b/quotes?customer_id=${encodeURIComponent(data.customer.id)}&customer_name=${encodeURIComponent(data.customer.display_name)}`}
                  className="flex items-center justify-between gap-2 rounded-xl border border-emerald-200 bg-emerald-50 px-2.5 py-2 text-[12px] font-semibold text-emerald-900 transition-colors hover:bg-emerald-100"
                >
                  <span>{bi("为此客户新建报价", "Create a quote for this buyer")}</span>
                  <ArrowUpRight className="h-3.5 w-3.5 shrink-0" />
                </Link>

                {data.recent_quotes.map((q) => (
                  <div
                    key={q.id}
                    className="rounded-xl border border-slate-200 bg-white p-2.5 text-xs shadow-[0_1px_2px_rgba(15,23,42,0.04)]"
                  >
                    <div className="flex items-center justify-between font-semibold text-slate-900">
                      <Link
                        href={`/b2b/quotes?quoteId=${q.id}&customer_id=${encodeURIComponent(data.customer.id)}&customer_name=${encodeURIComponent(data.customer.display_name)}`}
                        className="inline-flex items-center gap-1 text-blue-600 hover:underline"
                      >
                        <FileText className="h-3 w-3" />
                        <span>{q.quote_no}</span>
                        <span>v{q.version}</span>
                      </Link>
                      <span>{formatMoney(q.total_amount, q.quote_currency)}</span>
                    </div>
                    <div className="mt-1 flex flex-wrap items-center justify-between gap-1 text-[11px] text-slate-600">
                      <span>
                        {bi("贸易术语", "Incoterm")} {q.incoterm} · {bi("状态", "Status")} {q.status}
                      </span>
                      <Link
                        data-testid={`context-quote-send-pi-link-${q.id}`}
                        href={`/b2b/quotes?quoteId=${q.id}&customer_id=${encodeURIComponent(data.customer.id)}&customer_name=${encodeURIComponent(data.customer.display_name)}&send_pi_email=1`}
                        className="inline-flex items-center gap-1 rounded-md border border-emerald-200 bg-emerald-50 px-1.5 py-0.5 font-semibold text-emerald-800 transition-colors hover:bg-emerald-100"
                      >
                        <Mail className="h-3 w-3" />
                        <span>{bi("发 PI 邮件", "Send PI email")}</span>
                      </Link>
                    </div>
                  </div>
                ))}
              </div>
            )}

            {activeTab === "sku" && (
              <div className="space-y-2 rounded-xl border border-slate-200 bg-white p-2.5 text-xs shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
                <div className="flex items-center gap-1 font-semibold text-slate-800">
                  <PackageSearch className="h-3.5 w-3.5 text-blue-600" />
                  <span>{bi("确定性 SQL 库存与 MOQ 查询", "Deterministic SQL stock & MOQ lookup")}</span>
                </div>
                <input
                  data-testid="live-sku-input"
                  type="text"
                  value={skuInput}
                  onChange={(e) => setSkuInput(e.target.value)}
                  placeholder={bi("输入 SKU，例如 LED-COB-24V-5M", "SKU e.g. LED-COB-24V-5M")}
                  className="w-full rounded-lg border border-slate-200 bg-slate-50/70 px-2 py-1 text-xs focus:border-blue-500 focus:bg-white focus:outline-none"
                />
                <div className="flex gap-2">
                  <input
                    type="number"
                    min={1}
                    value={qtyInput}
                    onChange={(e) => setQtyInput(Number(e.target.value) || 1)}
                    className="w-24 rounded-lg border border-slate-200 bg-slate-50/70 px-2 py-1 text-xs focus:border-blue-500 focus:bg-white focus:outline-none"
                  />
                  <select
                    value={incotermInput}
                    onChange={(e) => setIncotermInput(e.target.value as Incoterm)}
                    className="rounded-lg border border-slate-200 bg-slate-50/70 px-2 py-1 text-xs focus:border-blue-500 focus:bg-white focus:outline-none"
                  >
                    <option value="EXW">EXW</option>
                    <option value="FOB">FOB</option>
                    <option value="CIF">CIF</option>
                    <option value="DDP">DDP</option>
                    <option value="DAP">DAP</option>
                  </select>
                  <button
                    type="button"
                    data-testid="live-sku-check-btn"
                    disabled={!skuInput.trim() || liveSkuMutation.isPending}
                    onClick={() => liveSkuMutation.mutate()}
                    className="rounded-lg bg-blue-600 px-2.5 py-1 font-semibold text-white shadow-xs transition-colors hover:bg-blue-700 disabled:opacity-50"
                  >
                    {bi("查询", "Check")}
                  </button>
                </div>

                {liveSkuResult && (
                  <div className="rounded-xl border border-emerald-200 bg-emerald-50 p-2 text-emerald-950">
                    <div>
                      {bi("可用库存", "Available stock")}：<strong>{liveSkuResult.available_qty}</strong>
                    </div>
                    <div>
                      {bi("单价", "Unit price")}（{liveSkuResult.incoterm}, MOQ{" "}
                      {liveSkuResult.moq_tier_applied}）：{" "}
                      <strong>{formatMoney(liveSkuResult.unit_price_quote_ccy, "USD")}</strong>
                    </div>
                    {matchedLeadTimeDays !== null && (
                      <div>
                        {bi("交期", "Lead time")}：<strong>{matchedLeadTimeDays}</strong>{" "}
                        {bi("天", "days")}
                      </div>
                    )}
                    <div className="mt-1 text-[10px] text-emerald-700">
                      {bi("工具接地 ID", "Tool grounding ID")}：{liveSkuResult.source_tool_call_id}
                    </div>
                    {onInsertReplyText && (
                      <button
                        type="button"
                        data-testid={`insert-sku-to-reply-${liveSkuResult.sku}`}
                        onClick={() =>
                          onInsertReplyText(
                            buildSkuReplySnippet(
                              liveSkuResult,
                              matchedLeadTimeDays,
                              locale,
                            ),
                          )
                        }
                        className="mt-2 inline-flex w-full items-center justify-center gap-1.5 rounded-lg bg-emerald-600 px-2.5 py-1.5 text-[11.5px] font-semibold text-white shadow-xs transition-colors hover:bg-emerald-700"
                      >
                        <ClipboardPaste className="h-3 w-3" />
                        <span>{bi("插入报价/库存", "Insert into reply")}</span>
                      </button>
                    )}
                  </div>
                )}
              </div>
            )}
          </div>

          {mergeDialogOpen && (
            <div
              role="dialog"
              aria-modal="true"
              className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
            >
              <div className="w-full max-w-md rounded-2xl border border-slate-200 bg-white p-4 shadow-2xl">
                <h4 className="text-sm font-bold text-slate-900">
                  {bi("身份合并与可逆拆分", "Identity merge & reversible split")}
                </h4>
                <div className="mt-2">
                  <label className="erp-label" htmlFor="merge-reason">
                    {bi("审计原因", "Audit reason")}
                  </label>
                  <input
                    id="merge-reason"
                    type="text"
                    value={mergeReason}
                    onChange={(e) => setMergeReason(e.target.value)}
                    className="mt-1 w-full rounded-lg border border-slate-200 bg-slate-50/70 px-2.5 py-1.5 text-xs focus:border-blue-500 focus:bg-white focus:outline-none"
                  />
                </div>

                <div className="mt-3 space-y-2">
                  {data.probable_duplicates.map((dup) => (
                    <div
                      key={dup.candidate_customer_id}
                      className="flex items-center justify-between rounded-xl border border-slate-200 bg-slate-50/60 p-2 text-xs"
                    >
                      <div>
                        <div className="font-semibold">{dup.display_name}</div>
                        <div className="text-[11px] text-slate-500">
                          {dup.matched_signal} ({Math.round(dup.similarity_score * 100)}%)
                        </div>
                      </div>
                      <button
                        type="button"
                        data-testid={`confirm-merge-${dup.candidate_customer_id}`}
                        onClick={() => mergeMutation.mutate(dup.candidate_customer_id)}
                        className="rounded-lg bg-blue-600 px-2.5 py-1 text-xs font-semibold text-white shadow-xs hover:bg-blue-700"
                      >
                        {bi("合并", "Merge")}
                      </button>
                    </div>
                  ))}

                  {data.merge_events
                    .filter((e) => e.action === "merge")
                    .map((ev) => (
                      <div
                        key={ev.id}
                        className="flex items-center justify-between rounded-xl border border-slate-200 bg-slate-50/60 p-2 text-xs"
                      >
                        <span>{bi("合并事件", "Merged event")} {ev.id}</span>
                        <button
                          type="button"
                          onClick={() => splitMutation.mutate(ev.id)}
                          className="inline-flex items-center gap-1 rounded border border-rose-300 px-2 py-1 text-rose-700"
                        >
                          <Split className="h-3 w-3" />
                          <span>{bi("拆分", "Split")}</span>
                        </button>
                      </div>
                    ))}
                </div>

                <div className="mt-4 flex justify-end">
                  <button
                    type="button"
                    onClick={() => setMergeDialogOpen(false)}
                    className="rounded-lg border border-slate-300 bg-white px-3 py-1 text-xs font-semibold text-slate-700 hover:bg-slate-50"
                  >
                    {bi("关闭", "Close")}
                  </button>
                </div>
              </div>
            </div>
          )}
        </>
      )}
    </aside>
  );
}
