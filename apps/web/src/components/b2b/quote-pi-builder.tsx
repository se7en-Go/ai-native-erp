"use client";

import React, { useEffect, useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ArrowRightCircle,
  CheckCircle2,
  Clock,
  Coins,
  Eye,
  FileDown,
  Layers3,
  Mail,
  RefreshCw,
  ScrollText,
} from "lucide-react";
import {
  acceptQuote,
  convertQuoteToOrder,
  generateQuotePdf,
  repriceQuoteVersion,
} from "@/lib/api-client/live-client";
import { quotePdfDownloadUrl } from "@/lib/data-source";
import { SendPiEmailModal } from "@/components/b2b/send-pi-email-modal";
import { useI18n } from "@/lib/i18n";
import { formatMoney, type Incoterm, type QuoteStatus } from "@/lib/contracts";
import { Badge, Card, CardHeader, EmptyState } from "@/components/ui/page";
import type { CatalogVariantItem, MoqTier } from "@/components/catalog/sku-mapping-drawer";

export interface QuoteItemView {
  id: string;
  variant_id: string;
  sku_snapshot: string;
  quantity: number;
  moq_tier_applied?: number | null;
  unit_price_quote_ccy: string;
  line_total_quote_ccy: string;
}

export interface QuoteRecord {
  id: string;
  opportunity_id: string;
  quote_no: string;
  version: number;
  is_proforma_invoice: boolean;
  incoterm: Incoterm;
  port_of_loading?: string | null;
  port_of_destination?: string | null;
  payment_terms: string;
  base_currency: string;
  quote_currency: string;
  fx_rate_snapshot: string;
  fx_locked_until: string;
  freight_fee: string;
  insurance_fee: string;
  customs_duty_estimate: string;
  total_amount: string;
  pdf_r2_key?: string | null;
  download_url?: string | null;
  status: QuoteStatus;
  converted_order_id?: string | null;
  items: QuoteItemView[];
}

export function resolveMoqTierPrice(
  tiers: MoqTier[],
  qty: number,
  fallbackPriceUsd: number
): { tierMinQty: number; unitPriceUsd: number } {
  const sorted = [...tiers].sort((a, b) => b.min_qty - a.min_qty);
  const matched = sorted.find((t) => qty >= t.min_qty);
  if (matched) {
    return { tierMinQty: matched.min_qty, unitPriceUsd: matched.unit_price_usd };
  }
  return { tierMinQty: 1, unitPriceUsd: fallbackPriceUsd };
}

/**
 * FX 锁定倒计时。E2E 与历史单测依赖英文标签文本，故此处保持英文技术文案。
 */
export function formatFxCountdown(fxLockedUntilIso: string): {
  expired: boolean;
  label: string;
} {
  const ms = new Date(fxLockedUntilIso).getTime() - Date.now();
  if (Number.isNaN(ms) || ms <= 0) {
    return { expired: true, label: "FX Lock Expired — Renewal Required" };
  }
  const totalMinutes = Math.floor(ms / 60_000);
  const hours = Math.floor(totalMinutes / 60);
  const mins = totalMinutes % 60;
  return { expired: false, label: `FX Locked: ${hours}h ${mins}m remaining` };
}

const QUOTE_STATUS_LABEL: Record<QuoteStatus, { zh: string; en: string }> = {
  draft: { zh: "草稿", en: "Draft" },
  pending_approval: { zh: "待审批", en: "Pending approval" },
  sent: { zh: "已发送", en: "Sent" },
  accepted: { zh: "买方已接受", en: "Accepted" },
  expired: { zh: "已过期", en: "Expired" },
  converted: { zh: "已转订单", en: "Converted" },
};

export function QuotePiBuilder({
  quotes,
  variants,
  initialQuoteId = null,
  autoOpenSendEmail = false,
}: {
  quotes: QuoteRecord[];
  variants: CatalogVariantItem[];
  /** Inbox 抽屉深链 ?quoteId=... 时直接选中该版本。 */
  initialQuoteId?: string | null;
  /** Inbox 抽屉的「发 PI 邮件」深链 ?send_pi_email=1 时直接打开发送抽屉。 */
  autoOpenSendEmail?: boolean;
}) {
  const queryClient = useQueryClient();
  const { bi } = useI18n();
  const resolvedInitialId =
    (initialQuoteId && quotes.some((q) => q.id === initialQuoteId)
      ? initialQuoteId
      : quotes[0]?.id) ?? "";
  const [selectedQuoteId, setSelectedQuoteId] = useState<string>(resolvedInitialId);
  const [autoOpenHandled, setAutoOpenHandled] = useState(false);
  const [previewVariantId, setPreviewVariantId] = useState<string>(variants[0]?.id ?? "");
  const [previewQty, setPreviewQty] = useState<number>(500);
  const [previewIncoterm, setPreviewIncoterm] = useState<Incoterm>("FOB");
  const [convertedBanner, setConvertedBanner] = useState<string | null>(null);
  const [pdfPreviewUrl, setPdfPreviewUrl] = useState<string | null>(null);
  const [emailQuoteId, setEmailQuoteId] = useState<string | null>(null);

  // 「发 PI 邮件」深链：数据到齐后再打开抽屉（首帧 quotes 还是空的）。
  useEffect(() => {
    if (!autoOpenSendEmail || autoOpenHandled || !resolvedInitialId) return;
    setEmailQuoteId(resolvedInitialId);
    setAutoOpenHandled(true);
  }, [autoOpenHandled, autoOpenSendEmail, resolvedInitialId]);

  const activeQuote =
    quotes.find((q) => q.id === selectedQuoteId) ??
    quotes.find((q) => q.id === resolvedInitialId) ??
    quotes[0] ??
    null;
  const activeVariant = variants.find((v) => v.id === previewVariantId) ?? variants[0] ?? null;
  const emailModalQuote = quotes.find((q) => q.id === emailQuoteId) ?? null;

  const moqPreview = useMemo(() => {
    if (!activeVariant) return null;
    return resolveMoqTierPrice(
      activeVariant.b2b_moq_tiers,
      previewQty,
      Number(activeVariant.retail_price_usd)
    );
  }, [activeVariant, previewQty]);

  const pdfMutation = useMutation({
    mutationFn: (quoteId: string) => generateQuotePdf(quoteId),
    onSuccess: (res) => {
      setPdfPreviewUrl(res.download_url);
      void queryClient.invalidateQueries({ queryKey: ["b2b", "quotes"] });
    },
  });

  const acceptMutation = useMutation({
    mutationFn: (quoteId: string) => acceptQuote(quoteId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["b2b", "quotes"] });
    },
  });

  const renewFxMutation = useMutation({
    mutationFn: (quoteId: string) =>
      repriceQuoteVersion(
        quoteId,
        activeQuote?.fx_rate_snapshot ?? "7.120000",
        new Date(Date.now() + 1000 * 60 * 60 * 48).toISOString(),
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["b2b", "quotes"] });
    },
  });

  const convertMutation = useMutation({
    mutationFn: (quoteId: string) => convertQuoteToOrder(quoteId),
    onSuccess: (res) => {
      // E2E 断言该成功提示包含 "Converted 1-Click to Order"，保持英文原文。
      setConvertedBanner(`Converted 1-Click to Order ${res.order.order_no} (${res.order.id})`);
      void queryClient.invalidateQueries({ queryKey: ["b2b", "quotes"] });
      void queryClient.invalidateQueries({ queryKey: ["orders"] });
    },
  });

  return (
    <div className="grid grid-cols-1 items-start gap-5 lg:grid-cols-12">
      {/* 左列（lg:col-span-5）：MOQ 阶梯价试算 + 精简报价合规提示 */}
      <Card className="min-w-0 overflow-hidden lg:col-span-5 lg:col-start-1">
        <CardHeader
          title={bi("MOQ 阶梯价试算", "MOQ tier price calculator")}
          description={bi("输入数量自动匹配最优阶梯价", "Quantity auto-matches the best tier")}
          icon={Coins}
        />
        <div className="space-y-3 p-4 text-[13px]">
          <div>
            <label className="erp-label" htmlFor="moq-preview-sku">
              {bi("选择商品 SKU", "Select SKU")}
            </label>
            <select
              id="moq-preview-sku"
              value={activeVariant?.id ?? ""}
              onChange={(e) => setPreviewVariantId(e.target.value)}
              className="erp-input mt-1.5 rounded-xl"
            >
              {variants.map((v) => (
                <option key={v.id} value={v.id}>
                  {v.sku}
                </option>
              ))}
            </select>
          </div>
          <div className="grid grid-cols-2 gap-2">
            <div>
              <label className="erp-label" htmlFor="moq-preview-qty-input">
                {bi("采购数量", "Order qty")}
              </label>
              <input
                id="moq-preview-qty-input"
                type="number"
                min={1}
                data-testid="moq-preview-qty-input"
                value={previewQty}
                onChange={(e) => setPreviewQty(Math.max(1, Number(e.target.value) || 1))}
                className="erp-input mt-1.5 rounded-xl tabular-nums"
              />
            </div>
            <div>
              <label className="erp-label" htmlFor="moq-preview-incoterm">
                {bi("贸易术语", "Incoterm")}
              </label>
              <select
                id="moq-preview-incoterm"
                value={previewIncoterm}
                onChange={(e) => setPreviewIncoterm(e.target.value as Incoterm)}
                className="erp-input mt-1.5 rounded-xl"
              >
                <option value="EXW">EXW</option>
                <option value="FOB">FOB</option>
                <option value="CIF">CIF</option>
                <option value="DDP">DDP</option>
                <option value="DAP">DAP</option>
              </select>
            </div>
          </div>

          {moqPreview && (
            <div className="relative overflow-hidden rounded-2xl border border-blue-200/80 bg-gradient-to-b from-blue-50 to-blue-50/40 p-3.5 ring-1 ring-blue-100/60 dark:border-blue-500/30 dark:from-blue-500/10 dark:to-blue-500/5 dark:ring-blue-500/20">
              <div className="flex items-center justify-between gap-2">
                <span className="inline-flex items-center gap-1.5 rounded-full border border-blue-200 bg-white/80 px-2 py-0.5 text-[11px] font-semibold text-blue-800 dark:border-blue-500/40 dark:bg-gray-900/70 dark:text-blue-300">
                  {bi("命中阶梯", "Applied tier")} · MOQ {moqPreview.tierMinQty}+
                </span>
                <span className="rounded-full bg-white/80 px-2 py-0.5 text-[11px] font-semibold text-gray-700 dark:bg-gray-900/70 dark:text-gray-300">
                  {previewIncoterm}
                </span>
              </div>
              <div
                data-testid="moq-preview-price"
                className="mt-2 text-[26px] font-bold leading-none tracking-tight tabular-nums text-blue-900 dark:text-blue-300"
              >
                {formatMoney(moqPreview.unitPriceUsd, "USD")}
                <span className="ml-1 text-[13px] font-semibold text-blue-700/80 dark:text-blue-400/80">
                  / pc
                </span>
              </div>
              <div className="mt-2 flex items-baseline justify-between gap-2 border-t border-blue-200/70 pt-2 text-[12px] dark:border-blue-500/30">
                <span className="font-medium text-gray-500 dark:text-gray-400">
                  {bi("预估小计", "Est. subtotal")}
                </span>
                <span className="font-bold tabular-nums text-gray-900 dark:text-gray-100">
                  {formatMoney(moqPreview.unitPriceUsd * previewQty, "USD")}
                </span>
              </div>
            </div>
          )}

          <div className="space-y-1.5 rounded-xl border border-gray-200/70 bg-gray-50 p-3 text-[12px] leading-5 text-gray-600 dark:border-gray-800 dark:bg-gray-800/50 dark:text-gray-400">
            <p>{bi("阶梯价规则：数量越大单价越低，报价单落库时快照保存。", "Tier pricing: larger quantities get lower unit prices, snapshotted into the quote.")}</p>
            <p>{bi("FOB/CIF 需确认装运港与目的港，DDP 需另计目的国关税。", "FOB/CIF require POL/POD; DDP must include destination duty.")}</p>
          </div>
        </div>
      </Card>

      {/* 精简合规提示：紧凑列表，与 MOQ 试算同列。 */}
      <Card className="min-w-0 overflow-hidden lg:col-span-5 lg:col-start-1">
        <CardHeader
          title={bi("报价条款与合规提示", "Quote terms & compliance")}
          description={bi("生成 PI 前请确认以下商务要素", "Confirm these commercial terms before issuing the PI")}
          icon={ScrollText}
        />
        <ul className="space-y-1.5 p-4 text-[12px] leading-5 text-gray-600 dark:text-gray-400">
          {[
            bi(
              "贸易术语：FOB 深圳 / CIF 汉堡 / EXW 工厂交货，按客户习惯选择。",
              "Incoterms: FOB Shenzhen / CIF Hamburg / EXW ex-works, chosen per buyer."
            ),
            bi(
              "付款方式：30% T/T 定金 + 70% 见提单副本付清，尾款未结清不出货。",
              "Payment: 30% T/T deposit plus 70% against B/L copy; no dispatch before balance."
            ),
            bi(
              "汇率锁定 48 小时，超期需重新取价并生成新版本。",
              "FX is locked for 48 hours; after expiry reprice and issue a new version."
            ),
            bi(
              "PI 中的 HS 编码、品牌与型号须与报关资料一致。",
              "HS code, brand and model on the PI must match customs documents."
            ),
          ].map((item) => (
            <li key={item} className="flex gap-2">
              <span
                aria-hidden="true"
                className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-blue-500 dark:bg-blue-400"
              />
              <span>{item}</span>
            </li>
          ))}
        </ul>
      </Card>

      {/* 右列（lg:col-span-7）：PI 版本管理、汇率、PDF 下载与一键发邮件 */}
      <Card className="min-w-0 self-start overflow-hidden lg:col-span-7 lg:col-start-6 lg:row-start-1">
        <CardHeader
          title={bi("形式发票（PI）版本管理", "Versioned proforma invoice (PI)")}
          description={bi(
            "每次改价生成新版本，汇率快照与条款随版本冻结",
            "Every reprice creates a new version with a frozen FX snapshot and terms"
          )}
          icon={Layers3}
          actions={
            <select
              aria-label={bi("选择报价单版本", "Select quote version")}
              value={activeQuote?.id ?? ""}
              onChange={(e) => setSelectedQuoteId(e.target.value)}
              className="erp-input w-auto rounded-xl py-1 text-[12px]"
            >
              {quotes.map((q) => (
                <option key={q.id} value={q.id}>
                  {q.quote_no} · v{q.version}
                </option>
              ))}
            </select>
          }
        />
        <div className="space-y-3 p-4 text-[13px]">
          {convertedBanner && (
            <div className="flex items-center gap-2 rounded-xl border border-emerald-200 bg-emerald-50 px-3 py-2.5 text-[12px] font-semibold text-emerald-900 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-200">
              <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-600 dark:text-emerald-400" />
              <span>{convertedBanner}</span>
            </div>
          )}

          {convertMutation.isError && (
            <p
              role="alert"
              className="rounded-lg border border-rose-200 bg-rose-50 p-2.5 text-[12px] text-rose-800 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-300"
            >
              {convertMutation.error.message}
            </p>
          )}

          {activeQuote ? (
            (() => {
              const fxState = formatFxCountdown(activeQuote.fx_locked_until);
              const canConvert = activeQuote.status === "accepted" && !fxState.expired;
              // 服务端渲染的 PI PDF 直链：永远可预览，不依赖先点“生成”。
              const downloadHref =
                pdfPreviewUrl ?? quotePdfDownloadUrl(activeQuote.id, "en");
              const statusCopy =
                QUOTE_STATUS_LABEL[activeQuote.status] ?? {
                  zh: activeQuote.status,
                  en: activeQuote.status,
                };

              return (
                <div className="space-y-3">
                  <div className="flex flex-wrap items-center justify-between gap-2 rounded-2xl border border-gray-200 bg-gray-50 px-3.5 py-2.5 dark:border-gray-800 dark:bg-gray-800/50">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-mono text-[13px] font-bold text-gray-900 dark:text-gray-100">
                        {activeQuote.quote_no}
                      </span>
                      <Badge>v{activeQuote.version}</Badge>
                      <Badge tone="info">{activeQuote.incoterm}</Badge>
                      <Badge tone={activeQuote.status === "accepted" ? "success" : "neutral"}>
                        {bi(statusCopy.zh, statusCopy.en)}
                      </Badge>
                    </div>

                    <span
                      data-testid={`fx-lock-badge-${activeQuote.id}`}
                      className={`inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-[11px] font-medium ${
                        fxState.expired
                          ? "border-rose-200 bg-rose-50 text-rose-700 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-300"
                          : "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-300"
                      }`}
                    >
                      <Clock className="h-3 w-3" />
                      <span className="tabular-nums">{fxState.label}</span>
                    </span>
                  </div>

                  <div className="grid gap-2 rounded-2xl border border-gray-200 bg-white p-3.5 text-[12px] leading-6 text-gray-700 sm:grid-cols-2 dark:border-gray-800 dark:bg-gray-900 dark:text-gray-300">
                    <div>
                      {bi("航线", "Route")}：{activeQuote.port_of_loading ?? "Shenzhen"} →{" "}
                      {activeQuote.port_of_destination ?? bi("买方港口", "Buyer port")}
                    </div>
                    <div className="tabular-nums">
                      {bi("汇率快照", "FX snapshot")}：1 {activeQuote.quote_currency} ={" "}
                      {activeQuote.fx_rate_snapshot} {activeQuote.base_currency}
                    </div>
                    <div className="sm:col-span-2">
                      {bi("付款条件", "Payment terms")}：{activeQuote.payment_terms}
                    </div>
                  </div>

                  <div className="overflow-x-auto rounded-2xl border border-gray-200 dark:border-gray-800">
                    <table className="erp-table">
                      <thead>
                        <tr>
                          <th>SKU</th>
                          <th className="text-right">{bi("数量", "Qty")}</th>
                          <th className="text-right">{bi("单价", "Unit price")}</th>
                          <th className="text-right">{bi("行小计", "Line total")}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {activeQuote.items.map((item) => (
                          <tr key={item.id}>
                            <td className="font-medium text-gray-800 dark:text-gray-200">{item.sku_snapshot}</td>
                            <td className="text-right tabular-nums">{item.quantity}</td>
                            <td className="text-right tabular-nums">
                              {formatMoney(item.unit_price_quote_ccy, activeQuote.quote_currency)}
                            </td>
                            <td className="text-right font-medium tabular-nums">
                              {formatMoney(item.line_total_quote_ccy, activeQuote.quote_currency)}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>

                  <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-gray-200 bg-gray-50/80 px-3.5 py-3 dark:border-gray-800 dark:bg-gray-800/40">
                    <div className="min-w-0">
                      <div className="text-[11px] font-medium text-gray-500 dark:text-gray-400">
                        {bi("合同总额", "Contract total")}
                      </div>
                      <div className="text-[22px] font-bold leading-tight tabular-nums text-gray-900 dark:text-gray-100">
                        {formatMoney(activeQuote.total_amount, activeQuote.quote_currency)}
                      </div>
                    </div>

                    <div className="flex flex-wrap items-center gap-2">
                      <button
                        type="button"
                        onClick={() => pdfMutation.mutate(activeQuote.id)}
                        className="erp-btn erp-btn-secondary rounded-xl"
                      >
                        <FileDown className="h-3.5 w-3.5" />
                        <span>{bi("生成 PI PDF", "Generate PI PDF")}</span>
                      </button>

                      <a
                        data-testid={`preview-pi-pdf-${activeQuote.id}`}
                        href={downloadHref}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="erp-btn rounded-xl border border-blue-200 bg-blue-50 text-blue-700 hover:bg-blue-100 dark:border-blue-500/40 dark:bg-blue-500/10 dark:text-blue-300 dark:hover:bg-blue-500/20"
                      >
                        <Eye className="h-3.5 w-3.5" />
                        <span>{bi("预览 PDF", "Preview PDF")}</span>
                      </a>

                      <button
                        type="button"
                        data-testid={`open-send-pi-email-${activeQuote.id}`}
                        onClick={() => setEmailQuoteId(activeQuote.id)}
                        className="erp-btn rounded-xl bg-blue-600 text-white shadow-[0_2px_8px_-2px_rgba(37,99,235,0.55)] transition-colors hover:bg-blue-700"
                      >
                        <Mail className="h-3.5 w-3.5" />
                        <span>{bi("发 PI 邮件", "Send PI email")}</span>
                      </button>

                      {fxState.expired && (
                        <button
                          type="button"
                          onClick={() => renewFxMutation.mutate(activeQuote.id)}
                          className="erp-btn rounded-xl border border-amber-200 bg-amber-50 text-amber-900 hover:bg-amber-100 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-300 dark:hover:bg-amber-500/20"
                        >
                          <RefreshCw className="h-3.5 w-3.5" />
                          <span>{bi("续锁汇率", "Renew FX lock")}</span>
                        </button>
                      )}

                      {activeQuote.status !== "accepted" && activeQuote.status !== "converted" && (
                        <button
                          type="button"
                          onClick={() => acceptMutation.mutate(activeQuote.id)}
                          className="erp-btn rounded-xl border border-gray-200 bg-white text-gray-700 hover:bg-gray-50 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200 dark:hover:bg-gray-800"
                        >
                          <CheckCircle2 className="h-3.5 w-3.5" />
                          <span>Mark Accepted</span>
                        </button>
                      )}

                      <button
                        type="button"
                        data-testid={`convert-quote-btn-${activeQuote.id}`}
                        disabled={!canConvert || convertMutation.isPending}
                        onClick={() => convertMutation.mutate(activeQuote.id)}
                        className="erp-btn erp-btn-primary rounded-xl shadow-[0_2px_8px_-2px_rgba(37,99,235,0.55)] disabled:opacity-50"
                      >
                        <ArrowRightCircle className="h-3.5 w-3.5" />
                        <span>{bi("一键转批发订单", "1-Click Convert to Order")}</span>
                      </button>
                    </div>
                  </div>
                </div>
              );
            })()
          ) : (
            <EmptyState>{bi("暂无报价单。", "No quotes yet.")}</EmptyState>
          )}
        </div>
      </Card>

      {emailModalQuote && (
        <SendPiEmailModal
          quote={emailModalQuote}
          onClose={() => setEmailQuoteId(null)}
        />
      )}
    </div>
  );
}
