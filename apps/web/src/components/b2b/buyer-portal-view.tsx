"use client";

import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  Building2,
  CheckCircle2,
  Download,
  FileText,
  Loader2,
  PackageCheck,
  RefreshCw,
  ShieldCheck,
  Truck,
  Upload,
} from "lucide-react";

import {
  acceptBuyerPortalQuote,
  buyerDocumentDownloadUrl,
  fetchBuyerPortal,
  reorderBuyerPortalQuote,
  submitBuyerPortalReceipt,
  type BuyerPortalView,
  type PortalReorderResponse,
  type PortalLanguage,
  type PortalReceivable,
} from "@/lib/api-client/track2-portal";
import { BffApiError, formatMoney } from "@/lib/contracts";

/**
 * Standalone buyer-facing self-service portal surface (`/portal/<magic-link>`).
 *
 * Deliberately NOT wrapped in the operator `AppShell`: the visitor is a buying
 * company with no ERP session, no sidebar and no internal navigation. The page is
 * a plain white/slate/blue document surface — seller letterhead, PI/CI/PL downloads,
 * quote acceptance, T/T bank-slip upload and the 17TRACK shipment timeline.
 */

const COPY = {
  en: {
    portalTitle: "Buyer self-service portal",
    portalSubtitle: "Review your Proforma Invoice, accept it and upload your T/T slip.",
    secured: "Secure magic link",
    seller: "Seller",
    bank: "T/T bank wire details",
    beneficiary: "Beneficiary",
    bankName: "Bank name",
    accountNo: "Account number",
    swift: "SWIFT / BIC",
    bankAddress: "Bank address",
    taxNo: "Tax / Reg. No.",
    quote: "Proforma Invoice",
    quoteNo: "Quote No.",
    version: "Version",
    status: "Status",
    incoterm: "Incoterm",
    loadingPort: "Port of loading",
    destinationPort: "Port of destination",
    paymentTerms: "Payment terms",
    fxRate: "FX rate (locked)",
    sku: "SKU",
    description: "Description",
    qty: "Qty",
    unitPrice: "Unit Price",
    lineTotal: "Amount",
    freight: "Freight",
    insurance: "Insurance",
    duty: "Customs duty (est.)",
    total: "Total",
    documents: "Documents",
    downloadPi: "Download PI (PDF)",
    downloadCi: "Download CI (PDF)",
    downloadPl: "Download PL (PDF)",
    documentsPending: "Commercial Invoice and Packing List are issued once your order is confirmed.",
    reorderTitle: "Request reorder",
    reorderHint:
      "Re-price the same items into a fresh draft quotation. The seller confirms it before production.",
    reorderButton: "Request Reorder",
    reordering: "Requesting…",
    reorderSuccess: "Reorder draft created",
    reorderQuoteNo: "New quote",
    reorderTotal: "Draft total",
    reorderNote: "Draft only — no payment or production starts until the seller confirms.",
    reorderFailed: "Could not create the reorder draft",
    acceptTitle: "Accept this quotation",
    acceptHint: "Type your full name as the authorised buyer contact.",
    buyerName: "Your name",
    acceptButton: "Accept quotation",
    accepting: "Accepting…",
    acceptedTitle: "Quotation accepted",
    acceptedAt: "Accepted on",
    acceptedBy: "Accepted by",
    order: "Your order",
    orderNo: "Order No.",
    fulfillment: "Fulfillment",
    payment: "Payment",
    milestones: "Payment milestones",
    milestone: "Milestone",
    due: "Due",
    paid: "Paid",
    milestoneStatus: "Status",
    uploadTitle: "Upload T/T bank slip",
    uploadHint:
      "Upload the bank transfer receipt for a milestone. Finance verifies it before your order is released.",
    amount: "Amount transferred",
    reference: "Bank reference",
    file: "Bank slip (PDF / PNG / JPEG / WebP, max 5 MB)",
    submit: "Submit bank slip",
    submitting: "Submitting…",
    uploadSuccess: "Bank slip received. Finance will verify it shortly.",
    pendingVerification: "Pending Finance verification",
    noOrderYet: "Payment milestones and shipping appear once the seller confirms your order.",
    shipments: "Shipment tracking (17TRACK)",
    carrier: "Carrier",
    trackingNo: "Tracking No.",
    noEvents: "No tracking events yet.",
    retry: "Retry",
    loading: "Loading your quotation…",
    switchLanguage: "中文",
    terms: "Terms & conditions",
  },
  zh: {
    portalTitle: "买家自助服务门户",
    portalSubtitle: "查看形式发票（PI）、在线确认报价并上传银行水单。",
    secured: "专属安全链接",
    seller: "卖方",
    bank: "银行电汇 (T/T) 信息",
    beneficiary: "收款人",
    bankName: "开户银行",
    accountNo: "银行账号",
    swift: "SWIFT / BIC",
    bankAddress: "银行地址",
    taxNo: "税号 / 注册号",
    quote: "形式发票 (PI)",
    quoteNo: "报价单号",
    version: "版本",
    status: "状态",
    incoterm: "贸易术语",
    loadingPort: "起运港",
    destinationPort: "目的港",
    paymentTerms: "付款条件",
    fxRate: "锁定汇率",
    sku: "SKU",
    description: "品名与规格",
    qty: "数量",
    unitPrice: "单价",
    lineTotal: "金额",
    freight: "运费",
    insurance: "保险",
    duty: "关税（预估）",
    total: "总计",
    documents: "单据下载",
    downloadPi: "下载 PI（PDF）",
    downloadCi: "下载商业发票 CI（PDF）",
    downloadPl: "下载装箱单 PL（PDF）",
    documentsPending: "订单确认后即可下载商业发票与装箱单。",
    reorderTitle: "一键复购",
    reorderHint: "按原清单重新报价，生成一张全新的草稿报价单，由卖方确认后再生效。",
    reorderButton: "一键复购 / Request Reorder",
    reordering: "正在生成…",
    reorderSuccess: "已生成复购草稿",
    reorderQuoteNo: "新报价单号",
    reorderTotal: "草稿金额",
    reorderNote: "仅为草稿，卖方确认前不会收款也不会安排生产。",
    reorderFailed: "复购草稿创建失败",
    acceptTitle: "确认本报价单",
    acceptHint: "请填写买方授权联系人姓名。",
    buyerName: "您的姓名",
    acceptButton: "确认报价",
    accepting: "正在确认…",
    acceptedTitle: "报价已确认",
    acceptedAt: "确认时间",
    acceptedBy: "确认人",
    order: "您的订单",
    orderNo: "订单号",
    fulfillment: "履约状态",
    payment: "付款状态",
    milestones: "付款节点",
    milestone: "节点",
    due: "应付",
    paid: "已付",
    milestoneStatus: "状态",
    uploadTitle: "上传银行水单",
    uploadHint: "上传对应节点的电汇水单，财务核销后订单才会放行。",
    amount: "到账金额",
    reference: "银行流水号",
    file: "银行水单（PDF / PNG / JPEG / WebP，最大 5 MB）",
    submit: "提交水单",
    submitting: "正在提交…",
    uploadSuccess: "水单已收到，财务将尽快核销。",
    pendingVerification: "等待财务核销",
    noOrderYet: "卖方确认订单后，此处会显示付款节点与物流信息。",
    shipments: "物流轨迹（17TRACK）",
    carrier: "承运商",
    trackingNo: "运单号",
    noEvents: "暂无物流轨迹。",
    retry: "重试",
    loading: "正在加载报价信息…",
    switchLanguage: "EN",
    terms: "条款与说明",
  },
} as const;

const STATUS_TONE: Record<string, string> = {
  accepted: "border-emerald-200 bg-emerald-50 text-emerald-700",
  converted: "border-emerald-200 bg-emerald-50 text-emerald-700",
  sent: "border-blue-200 bg-blue-50 text-blue-700",
  draft: "border-slate-200 bg-slate-100 text-slate-600",
  expired: "border-amber-200 bg-amber-50 text-amber-800",
  settled: "border-emerald-200 bg-emerald-50 text-emerald-700",
  partial: "border-amber-200 bg-amber-50 text-amber-800",
  open: "border-blue-200 bg-blue-50 text-blue-700",
  void: "border-rose-200 bg-rose-50 text-rose-700",
};

const MILESTONE_LABEL: Record<string, { en: string; zh: string }> = {
  b2b_30_deposit: { en: "30% T/T deposit", zh: "30% 预付款" },
  b2b_70_balance: { en: "70% balance before shipment", zh: "70% 尾款（出货前）" },
};

function toneClass(status: string): string {
  return STATUS_TONE[status] ?? "border-slate-200 bg-slate-100 text-slate-600";
}

function milestoneLabel(label: string, lang: PortalLanguage): string {
  return MILESTONE_LABEL[label]?.[lang] ?? label;
}

function errorDetail(error: unknown): string {
  if (error instanceof BffApiError) return error.detail || error.code;
  if (error instanceof Error) return error.message;
  return "Unexpected error";
}

export interface BuyerPortalViewProps {
  token: string;
}

export function BuyerPortalPageView({ token }: BuyerPortalViewProps) {
  const [lang, setLang] = useState<PortalLanguage>("en");
  const [view, setView] = useState<BuyerPortalView | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [buyerName, setBuyerName] = useState("");
  const [accepting, setAccepting] = useState(false);
  const [acceptError, setAcceptError] = useState<string | null>(null);
  const [receiptError, setReceiptError] = useState<string | null>(null);
  const [receiptSubmitting, setReceiptSubmitting] = useState(false);
  const [receiptSuccess, setReceiptSuccess] = useState<string | null>(null);
  const [selectedRpId, setSelectedRpId] = useState<string>("");
  const [amountReceived, setAmountReceived] = useState("");
  const [txRef, setTxRef] = useState("");
  const [receiptFile, setReceiptFile] = useState<File | null>(null);
  const [reordering, setReordering] = useState(false);
  const [reorderError, setReorderError] = useState<string | null>(null);
  const [reorderResult, setReorderResult] = useState<PortalReorderResponse | null>(null);

  const copy = COPY[lang];

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const payload = await fetchBuyerPortal(token);
      setView(payload);
      const openMilestone = payload.order?.receivables.find(
        (row) => row.status === "open" || row.status === "partial"
      );
      if (openMilestone) {
        setSelectedRpId(openMilestone.id);
        setAmountReceived(
          (Number(openMilestone.due_amount) - Number(openMilestone.paid_amount)).toFixed(2)
        );
      }
    } catch (error) {
      setLoadError(errorDetail(error));
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void load();
  }, [load]);

  const milestones = view?.order?.receivables ?? [];

  const onSelectMilestone = useCallback(
    (rpId: string) => {
      setSelectedRpId(rpId);
      const row = milestones.find((item) => item.id === rpId);
      if (row) {
        setAmountReceived((Number(row.due_amount) - Number(row.paid_amount)).toFixed(2));
      }
    },
    [milestones]
  );

  const onAccept = useCallback(async () => {
    const name = buyerName.trim();
    if (!name) {
      setAcceptError(lang === "zh" ? "请填写姓名。" : "Please enter your name.");
      return;
    }
    setAccepting(true);
    setAcceptError(null);
    try {
      const result = await acceptBuyerPortalQuote(token, name);
      setView((current) =>
        current
          ? {
              ...current,
              quote: { ...current.quote, status: result.status },
              can_accept: false,
              accepted_at: result.accepted_at ?? new Date().toISOString(),
              accepted_by_name: result.accepted_by_name ?? name,
            }
          : current
      );
    } catch (error) {
      setAcceptError(errorDetail(error));
    } finally {
      setAccepting(false);
    }
  }, [buyerName, lang, token]);

  const onReorder = useCallback(async () => {
    setReordering(true);
    setReorderError(null);
    setReorderResult(null);
    try {
      const result = await reorderBuyerPortalQuote(token, {});
      setReorderResult(result);
    } catch (error) {
      setReorderError(`${copy.reorderFailed}: ${errorDetail(error)}`);
    } finally {
      setReordering(false);
    }
  }, [copy.reorderFailed, token]);

  const onSubmitReceipt = useCallback(
    async (event: React.FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      if (!selectedRpId) {
        setReceiptError(lang === "zh" ? "请选择付款节点。" : "Choose a payment milestone.");
        return;
      }
      const milestone = milestones.find((item) => item.id === selectedRpId);
      if (!milestone) {
        setReceiptError(lang === "zh" ? "请选择付款节点。" : "Choose a payment milestone.");
        return;
      }
      setReceiptSubmitting(true);
      setReceiptError(null);
      setReceiptSuccess(null);
      try {
        await submitBuyerPortalReceipt(
          token,
          {
            rp_id: selectedRpId,
            payment_method: "bank_transfer",
            external_tx_ref: txRef.trim() || null,
            amount_received: amountReceived.trim(),
            currency: milestone.currency,
          },
          receiptFile
        );
        setReceiptSuccess(copy.uploadSuccess);
        setTxRef("");
        setReceiptFile(null);
      } catch (error) {
        setReceiptError(errorDetail(error));
      } finally {
        setReceiptSubmitting(false);
      }
    },
    [amountReceived, copy.uploadSuccess, lang, milestones, receiptFile, selectedRpId, token, txRef]
  );

  const currency = view?.quote.quote_currency ?? "USD";
  const accepted = Boolean(view && (view.accepted_at || view.quote.status === "accepted"));

  const shipmentTimeline = useMemo(() => {
    const shipments = view?.order?.shipments ?? [];
    return shipments.flatMap((shipment) =>
      shipment.tracking_events.map((event, index) => ({
        key: `${shipment.id}-${index}`,
        shipment,
        event,
      }))
    );
  }, [view]);

  return (
    <main data-testid="buyer-portal-root" className="min-h-screen bg-slate-50 text-slate-900">
      <div className="mx-auto w-full max-w-5xl px-4 py-8 sm:px-6 lg:py-12">
        <header className="mb-6 flex flex-wrap items-start justify-between gap-4">
          <div className="flex items-start gap-3">
            <span className="grid h-11 w-11 place-items-center rounded-2xl bg-blue-600 text-white">
              <Building2 className="h-5 w-5" aria-hidden="true" />
            </span>
            <div>
              <p
                data-testid="buyer-portal-seller-name"
                className="text-lg font-semibold tracking-tight text-slate-900"
              >
                {view?.seller.company_name ?? copy.seller}
              </p>
              <p className="text-[13px] text-slate-500">{copy.portalSubtitle}</p>
              <p className="mt-1 inline-flex items-center gap-1.5 rounded-full border border-blue-200 bg-blue-50 px-2.5 py-0.5 text-[11px] font-medium text-blue-700">
                <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" />
                {copy.secured}
              </p>
            </div>
          </div>
          <button
            type="button"
            data-testid="portal-lang-toggle"
            onClick={() => setLang(lang === "en" ? "zh" : "en")}
            className="rounded-xl border border-slate-200 bg-white px-3 py-1.5 text-[12px] font-medium text-slate-600 transition-colors hover:border-blue-300 hover:text-blue-700"
          >
            {copy.switchLanguage}
          </button>
        </header>

        {loading && (
          <section className="flex items-center gap-2 rounded-2xl border border-slate-200 bg-white p-6 text-[13px] text-slate-500">
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
            {copy.loading}
          </section>
        )}

        {!loading && loadError && (
          <section
            data-testid="buyer-portal-error"
            className="rounded-2xl border border-rose-200 bg-rose-50 p-5 text-[13px] text-rose-700"
          >
            <p className="flex items-center gap-2 font-semibold">
              <AlertTriangle className="h-4 w-4" aria-hidden="true" />
              {loadError}
            </p>
            <button
              type="button"
              onClick={() => void load()}
              className="mt-3 rounded-lg border border-rose-300 bg-white px-3 py-1.5 text-[12px] font-medium text-rose-700"
            >
              {copy.retry}
            </button>
          </section>
        )}

        {!loading && view && (
          <div className="space-y-5">
            {/* Seller + bank wire block */}
            <section className="grid gap-4 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm md:grid-cols-2">
              <div>
                <h2 className="text-[13px] font-semibold text-slate-900">{copy.seller}</h2>
                <p className="mt-1 text-[13px] font-medium text-slate-800">
                  {view.seller.company_name}
                </p>
                <dl className="mt-2 space-y-0.5 text-[12.5px] text-slate-600">
                  {view.seller.address && <dd>{view.seller.address}</dd>}
                  {view.seller.email && <dd>{view.seller.email}</dd>}
                  {view.seller.phone && <dd className="tabular-nums">{view.seller.phone}</dd>}
                  {view.seller.website && <dd>{view.seller.website}</dd>}
                  {view.seller.tax_or_reg_no && (
                    <dd>
                      {copy.taxNo}: <span className="tabular-nums">{view.seller.tax_or_reg_no}</span>
                    </dd>
                  )}
                </dl>
              </div>
              <div className="rounded-xl border border-slate-200 bg-slate-50/70 p-3">
                <h2 className="text-[13px] font-semibold text-slate-900">{copy.bank}</h2>
                <dl className="mt-1.5 grid gap-x-3 gap-y-1 text-[12.5px] sm:grid-cols-2">
                  {[
                    [copy.beneficiary, view.seller.bank_beneficiary_name],
                    [copy.bankName, view.seller.bank_name],
                    [copy.accountNo, view.seller.bank_account_no],
                    [copy.swift, view.seller.bank_swift_code],
                    [copy.bankAddress, view.seller.bank_address],
                  ]
                    .filter(([, value]) => Boolean(value))
                    .map(([label, value]) => (
                      <React.Fragment key={label}>
                        <dt className="text-slate-500">{label}</dt>
                        <dd className="font-medium tabular-nums text-slate-800">{value}</dd>
                      </React.Fragment>
                    ))}
                </dl>
              </div>
            </section>

            {/* Quote / PI */}
            <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h2 className="text-[13px] font-semibold text-slate-900">{copy.quote}</h2>
                <span
                  data-testid="buyer-portal-quote-status"
                  className={`rounded-full border px-2.5 py-0.5 text-[11px] font-medium ${toneClass(view.quote.status)}`}
                >
                  {view.quote.status}
                </span>
              </div>
              <dl className="mt-3 grid gap-x-4 gap-y-1.5 text-[12.5px] sm:grid-cols-2 lg:grid-cols-3">
                <div>
                  <dt className="text-slate-500">{copy.quoteNo}</dt>
                  <dd className="font-medium tabular-nums text-slate-900">{view.quote.quote_no}</dd>
                </div>
                <div>
                  <dt className="text-slate-500">{copy.version}</dt>
                  <dd className="font-medium tabular-nums text-slate-900">
                    v{view.quote.version}
                  </dd>
                </div>
                <div>
                  <dt className="text-slate-500">{copy.incoterm}</dt>
                  <dd className="font-medium text-slate-900">{view.quote.incoterm}</dd>
                </div>
                <div>
                  <dt className="text-slate-500">{copy.loadingPort}</dt>
                  <dd className="font-medium text-slate-900">{view.quote.port_of_loading ?? "—"}</dd>
                </div>
                <div>
                  <dt className="text-slate-500">{copy.destinationPort}</dt>
                  <dd className="font-medium text-slate-900">
                    {view.quote.port_of_destination ?? "—"}
                  </dd>
                </div>
                <div>
                  <dt className="text-slate-500">{copy.fxRate}</dt>
                  <dd className="font-medium tabular-nums text-slate-900">
                    1 {currency} = {Number(view.quote.fx_rate_snapshot).toFixed(6)}
                  </dd>
                </div>
                <div className="sm:col-span-2 lg:col-span-3">
                  <dt className="text-slate-500">{copy.paymentTerms}</dt>
                  <dd className="font-medium text-slate-900">{view.quote.payment_terms}</dd>
                </div>
              </dl>

              <div className="mt-4 overflow-x-auto">
                <table data-testid="buyer-portal-items" className="w-full text-[12.5px]">
                  <thead>
                    <tr className="bg-slate-50/80 text-left text-[11px] font-medium text-slate-500">
                      <th className="px-3 py-2">{copy.sku}</th>
                      <th className="px-3 py-2">{copy.description}</th>
                      <th className="px-3 py-2 text-right">{copy.qty}</th>
                      <th className="px-3 py-2 text-right">{copy.unitPrice}</th>
                      <th className="px-3 py-2 text-right">{copy.lineTotal}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {view.quote.items.map((item) => (
                      <tr key={item.sku} className="border-t border-slate-100">
                        <td className="px-3 py-2 font-medium tabular-nums text-slate-900">
                          {item.sku}
                        </td>
                        <td className="px-3 py-2 text-slate-700">{item.description}</td>
                        <td className="px-3 py-2 text-right tabular-nums text-slate-700">
                          {item.quantity}
                        </td>
                        <td className="px-3 py-2 text-right tabular-nums text-slate-700">
                          {formatMoney(item.unit_price, currency)}
                        </td>
                        <td className="px-3 py-2 text-right font-medium tabular-nums text-slate-900">
                          {formatMoney(item.line_total, currency)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <dl className="mt-4 ml-auto w-full max-w-xs space-y-1 text-[12.5px]">
                {[
                  [copy.freight, view.quote.freight_fee],
                  [copy.insurance, view.quote.insurance_fee],
                  [copy.duty, view.quote.customs_duty_estimate],
                ].map(([label, value]) => (
                  <div key={label} className="flex items-center justify-between gap-3">
                    <dt className="text-slate-500">{label}</dt>
                    <dd className="tabular-nums text-slate-700">
                      {formatMoney(value, currency)}
                    </dd>
                  </div>
                ))}
                <div className="flex items-center justify-between gap-3 border-t border-slate-200 pt-1.5">
                  <dt className="font-semibold text-slate-900">{copy.total}</dt>
                  <dd
                    data-testid="buyer-portal-quote-total"
                    className="font-semibold tabular-nums text-slate-900"
                  >
                    {formatMoney(view.quote.total_amount, currency)}
                  </dd>
                </div>
              </dl>
            </section>

            {/* Documents */}
            <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
              <h2 className="flex items-center gap-2 text-[13px] font-semibold text-slate-900">
                <FileText className="h-4 w-4 text-blue-600" aria-hidden="true" />
                {copy.documents}
              </h2>
              <div className="mt-3 flex flex-wrap gap-2">
                <a
                  data-testid="download-pi"
                  href={buyerDocumentDownloadUrl(token, "pi", lang)}
                  className="inline-flex items-center gap-1.5 rounded-xl border border-slate-200 bg-white px-3 py-1.5 text-[12px] font-medium text-slate-700 transition-colors hover:border-blue-300 hover:text-blue-700"
                >
                  <Download className="h-3.5 w-3.5" aria-hidden="true" />
                  {copy.downloadPi}
                </a>
                {view.order ? (
                  <>
                    <a
                      data-testid="download-ci"
                      href={buyerDocumentDownloadUrl(token, "ci", lang)}
                      className="inline-flex items-center gap-1.5 rounded-xl border border-slate-200 bg-white px-3 py-1.5 text-[12px] font-medium text-slate-700 transition-colors hover:border-blue-300 hover:text-blue-700"
                    >
                      <Download className="h-3.5 w-3.5" aria-hidden="true" />
                      {copy.downloadCi}
                    </a>
                    <a
                      data-testid="download-pl"
                      href={buyerDocumentDownloadUrl(token, "pl", lang)}
                      className="inline-flex items-center gap-1.5 rounded-xl border border-slate-200 bg-white px-3 py-1.5 text-[12px] font-medium text-slate-700 transition-colors hover:border-blue-300 hover:text-blue-700"
                    >
                      <Download className="h-3.5 w-3.5" aria-hidden="true" />
                      {copy.downloadPl}
                    </a>
                  </>
                ) : (
                  <span className="text-[12px] text-slate-500">{copy.documentsPending}</span>
                )}
              </div>
              {view.seller.terms_notes && (
                <div className="mt-3 rounded-xl bg-slate-50 p-3">
                  <p className="text-[11px] font-medium text-slate-500">{copy.terms}</p>
                  <p className="mt-1 whitespace-pre-line text-[12px] text-slate-600">
                    {view.seller.terms_notes}
                  </p>
                </div>
              )}
            </section>

            {/* One-click reorder */}
            <section
              data-testid="portal-reorder-section"
              className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm"
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <h2 className="flex items-center gap-2 text-[13px] font-semibold text-slate-900">
                    <RefreshCw className="h-4 w-4 text-blue-600" aria-hidden="true" />
                    {copy.reorderTitle}
                  </h2>
                  <p className="mt-0.5 text-[12px] text-slate-500">{copy.reorderHint}</p>
                </div>
                <button
                  type="button"
                  data-testid="portal-reorder-button"
                  onClick={() => void onReorder()}
                  disabled={reordering}
                  className="inline-flex items-center gap-1.5 rounded-xl border border-blue-200 bg-blue-50 px-3.5 py-2 text-[12.5px] font-semibold text-blue-700 transition-colors hover:bg-blue-100 disabled:opacity-60"
                >
                  <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
                  {reordering ? copy.reordering : copy.reorderButton}
                </button>
              </div>
              {reorderResult && (
                <div
                  data-testid="portal-reorder-banner"
                  className="mt-3 rounded-xl border border-emerald-200 bg-emerald-50 p-3"
                >
                  <p className="flex items-center gap-1.5 text-[12.5px] font-semibold text-emerald-800">
                    <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
                    {copy.reorderSuccess}
                  </p>
                  <dl className="mt-1.5 grid gap-x-4 gap-y-1 text-[12px] sm:grid-cols-2">
                    <div className="flex items-center justify-between gap-3 sm:block">
                      <dt className="text-emerald-700/80">{copy.reorderQuoteNo}</dt>
                      <dd className="font-medium tabular-nums text-emerald-900">
                        {reorderResult.new_quote_no}
                      </dd>
                    </div>
                    <div className="flex items-center justify-between gap-3 sm:block">
                      <dt className="text-emerald-700/80">{copy.reorderTotal}</dt>
                      <dd
                        data-testid="portal-reorder-total"
                        className="font-semibold tabular-nums text-emerald-900"
                      >
                        {formatMoney(reorderResult.total_amount, reorderResult.currency)}
                      </dd>
                    </div>
                  </dl>
                  <p className="mt-1.5 text-[11.5px] text-emerald-700/90">{copy.reorderNote}</p>
                </div>
              )}
              {reorderError && (
                <p data-testid="portal-reorder-error" className="mt-2 text-[12px] text-rose-600">
                  {reorderError}
                </p>
              )}
            </section>

            {/* Accept */}
            <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
              {accepted ? (
                <div data-testid="accept-success" className="flex items-start gap-2">
                  <CheckCircle2 className="mt-0.5 h-4 w-4 text-emerald-600" aria-hidden="true" />
                  <div>
                    <p className="text-[13px] font-semibold text-emerald-700">
                      {copy.acceptedTitle}
                    </p>
                    <p className="text-[12.5px] text-slate-600">
                      {copy.acceptedBy}: {view.accepted_by_name ?? "—"}
                      {view.accepted_at ? ` · ${copy.acceptedAt} ${new Date(view.accepted_at).toLocaleString()}` : ""}
                    </p>
                  </div>
                </div>
              ) : view.can_accept ? (
                <form
                  onSubmit={(event) => {
                    event.preventDefault();
                    void onAccept();
                  }}
                >
                  <h2 className="text-[13px] font-semibold text-slate-900">{copy.acceptTitle}</h2>
                  <p className="mt-0.5 text-[12px] text-slate-500">{copy.acceptHint}</p>
                  <div className="mt-3 flex flex-wrap items-end gap-2">
                    <label className="flex-1 min-w-[220px] text-[12px] font-medium text-slate-600">
                      {copy.buyerName}
                      <input
                        data-testid="accept-buyer-name"
                        value={buyerName}
                        onChange={(event) => setBuyerName(event.target.value)}
                        maxLength={120}
                        className="mt-1 w-full rounded-xl border border-slate-200 bg-white px-3 py-2 text-[13px] text-slate-900 outline-none transition-colors focus:border-blue-500"
                      />
                    </label>
                    <button
                      type="submit"
                      data-testid="accept-quote-button"
                      disabled={accepting}
                      className="inline-flex items-center gap-1.5 rounded-xl bg-blue-600 px-4 py-2 text-[12.5px] font-semibold text-white transition-colors hover:bg-blue-700 disabled:opacity-60"
                    >
                      {accepting ? copy.accepting : copy.acceptButton}
                    </button>
                  </div>
                  {acceptError && (
                    <p className="mt-2 text-[12px] text-rose-600">{acceptError}</p>
                  )}
                </form>
              ) : null}
            </section>

            {/* Order: milestones + T/T upload + shipments */}
            {view.order ? (
              <>
                <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <h2 className="flex items-center gap-2 text-[13px] font-semibold text-slate-900">
                      <PackageCheck className="h-4 w-4 text-blue-600" aria-hidden="true" />
                      {copy.order}
                    </h2>
                    <p className="text-[12px] text-slate-500">
                      {copy.orderNo}:{" "}
                      <span className="font-medium tabular-nums text-slate-800">
                        {view.order.order_no}
                      </span>
                    </p>
                  </div>
                  <div className="mt-2 flex flex-wrap gap-2 text-[11.5px]">
                    <span className={`rounded-full border px-2.5 py-0.5 font-medium ${toneClass(view.order.payment_status)}`}>
                      {copy.payment}: {view.order.payment_status}
                    </span>
                    <span className={`rounded-full border px-2.5 py-0.5 font-medium ${toneClass(view.order.fulfillment_status)}`}>
                      {copy.fulfillment}: {view.order.fulfillment_status}
                    </span>
                    <span className="rounded-full border border-slate-200 bg-slate-100 px-2.5 py-0.5 font-medium tabular-nums text-slate-600">
                      {formatMoney(view.order.total_amount, view.order.currency)}
                    </span>
                  </div>

                  <h3 className="mt-4 text-[12.5px] font-semibold text-slate-900">
                    {copy.milestones}
                  </h3>
                  <div className="mt-2 overflow-x-auto">
                    <table data-testid="milestone-list" className="w-full text-[12.5px]">
                      <thead>
                        <tr className="bg-slate-50/80 text-left text-[11px] font-medium text-slate-500">
                          <th className="px-3 py-2">{copy.milestone}</th>
                          <th className="px-3 py-2 text-right">{copy.due}</th>
                          <th className="px-3 py-2 text-right">{copy.paid}</th>
                          <th className="px-3 py-2">{copy.milestoneStatus}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {milestones.map((row: PortalReceivable) => (
                          <tr key={row.id} className="border-t border-slate-100">
                            <td className="px-3 py-2 font-medium text-slate-800">
                              {milestoneLabel(row.milestone_label, lang)}
                            </td>
                            <td className="px-3 py-2 text-right tabular-nums text-slate-700">
                              {formatMoney(row.due_amount, row.currency)}
                            </td>
                            <td className="px-3 py-2 text-right tabular-nums text-slate-700">
                              {formatMoney(row.paid_amount, row.currency)}
                            </td>
                            <td className="px-3 py-2">
                              <span className={`rounded-full border px-2 py-0.5 text-[11px] font-medium ${toneClass(row.status)}`}>
                                {row.status}
                              </span>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>

                  {view.can_upload_receipt && (
                    <form
                      onSubmit={onSubmitReceipt}
                      data-testid="receipt-form"
                      className="mt-4 rounded-xl border border-slate-200 bg-slate-50/60 p-3"
                    >
                      <h3 className="flex items-center gap-1.5 text-[12.5px] font-semibold text-slate-900">
                        <Upload className="h-3.5 w-3.5 text-blue-600" aria-hidden="true" />
                        {copy.uploadTitle}
                      </h3>
                      <p className="mt-0.5 text-[11.5px] text-slate-500">{copy.uploadHint}</p>
                      <div className="mt-2 grid gap-2 sm:grid-cols-2">
                        <label className="text-[12px] font-medium text-slate-600">
                          {copy.milestone}
                          <select
                            data-testid="receipt-milestone"
                            value={selectedRpId}
                            onChange={(event) => onSelectMilestone(event.target.value)}
                            className="mt-1 w-full rounded-xl border border-slate-200 bg-white px-3 py-2 text-[13px] text-slate-900 outline-none focus:border-blue-500"
                          >
                            {milestones.map((row) => (
                              <option key={row.id} value={row.id} disabled={row.status === "settled"}>
                                {milestoneLabel(row.milestone_label, lang)} · {row.currency}{" "}
                                {row.due_amount}
                              </option>
                            ))}
                          </select>
                        </label>
                        <label className="text-[12px] font-medium text-slate-600">
                          {copy.amount}
                          <input
                            data-testid="receipt-amount"
                            value={amountReceived}
                            onChange={(event) => setAmountReceived(event.target.value)}
                            inputMode="decimal"
                            className="mt-1 w-full rounded-xl border border-slate-200 bg-white px-3 py-2 text-[13px] tabular-nums text-slate-900 outline-none focus:border-blue-500"
                          />
                        </label>
                        <label className="text-[12px] font-medium text-slate-600">
                          {copy.reference}
                          <input
                            data-testid="receipt-reference"
                            value={txRef}
                            onChange={(event) => setTxRef(event.target.value)}
                            maxLength={120}
                            className="mt-1 w-full rounded-xl border border-slate-200 bg-white px-3 py-2 text-[13px] tabular-nums text-slate-900 outline-none focus:border-blue-500"
                          />
                        </label>
                        <label className="text-[12px] font-medium text-slate-600">
                          {copy.file}
                          <input
                            data-testid="receipt-file"
                            type="file"
                            accept="application/pdf,image/png,image/jpeg,image/webp"
                            onChange={(event) => setReceiptFile(event.target.files?.[0] ?? null)}
                            className="mt-1 w-full rounded-xl border border-slate-200 bg-white px-2 py-1.5 text-[12px] text-slate-700"
                          />
                        </label>
                      </div>
                      <div className="mt-2 flex items-center gap-2">
                        <button
                          type="submit"
                          data-testid="receipt-submit"
                          disabled={receiptSubmitting}
                          className="rounded-xl bg-blue-600 px-3.5 py-2 text-[12.5px] font-semibold text-white transition-colors hover:bg-blue-700 disabled:opacity-60"
                        >
                          {receiptSubmitting ? copy.submitting : copy.submit}
                        </button>
                        <span className={`rounded-full border px-2 py-0.5 text-[11px] font-medium ${toneClass("open")}`}>
                          {copy.pendingVerification}
                        </span>
                      </div>
                      {receiptError && (
                        <p className="mt-2 text-[12px] text-rose-600">{receiptError}</p>
                      )}
                      {receiptSuccess && (
                        <p data-testid="receipt-success" className="mt-2 text-[12px] text-emerald-700">
                          {receiptSuccess}
                        </p>
                      )}
                    </form>
                  )}
                </section>

                <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
                  <h2 className="flex items-center gap-2 text-[13px] font-semibold text-slate-900">
                    <Truck className="h-4 w-4 text-blue-600" aria-hidden="true" />
                    {copy.shipments}
                  </h2>
                  {view.order.shipments.length === 0 && (
                    <p className="mt-2 text-[12.5px] text-slate-500">{copy.noEvents}</p>
                  )}
                  {view.order.shipments.map((shipment) => (
                    <div key={shipment.id} className="mt-3">
                      <p className="text-[12.5px] font-medium tabular-nums text-slate-800">
                        {copy.carrier}: {shipment.carrier ?? "—"} · {copy.trackingNo}:{" "}
                        {shipment.tracking_number} · {shipment.standardized_status}
                      </p>
                      <ol
                        data-testid="shipment-timeline"
                        className="mt-2 space-y-2 border-l border-slate-200 pl-4"
                      >
                        {shipment.tracking_events.map((event, index) => (
                          <li key={`${shipment.id}-${index}`} className="relative">
                            <span className="absolute -left-[21px] top-1.5 h-2 w-2 rounded-full bg-blue-600" />
                            <p className="text-[12.5px] font-medium text-slate-800">
                              {event.description}
                            </p>
                            <p className="text-[11.5px] text-slate-500">
                              {[event.location, event.stage, event.occurred_at]
                                .filter(Boolean)
                                .join(" · ")}
                            </p>
                          </li>
                        ))}
                        {shipment.tracking_events.length === 0 && (
                          <li className="text-[12.5px] text-slate-500">{copy.noEvents}</li>
                        )}
                      </ol>
                    </div>
                  ))}
                  {shipmentTimeline.length === 0 && view.order.shipments.length === 0 && (
                    <p className="mt-2 text-[12.5px] text-slate-500">{copy.noEvents}</p>
                  )}
                </section>
              </>
            ) : (
              <section className="rounded-2xl border border-dashed border-slate-200 bg-white p-5 text-[12.5px] text-slate-500">
                {copy.noOrderYet}
              </section>
            )}
          </div>
        )}
      </div>
    </main>
  );
}

