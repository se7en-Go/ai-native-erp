"use client";

import React, { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Building2,
  CheckCircle2,
  Landmark,
  Loader2,
  PenLine,
  Save,
  ScrollText,
} from "lucide-react";

import {
  fetchPiTemplateSettings,
  updatePiTemplateSettings,
} from "@/lib/data-source";
import type { PiTemplateSettings } from "@/lib/api-client/live-client";
import { BffApiError } from "@/lib/contracts";
import { useI18n } from "@/lib/i18n";
import { Card, CardHeader } from "@/components/ui/page";

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[a-z]{2,}$/i;

type FieldKey = keyof PiTemplateSettings;

interface FieldSpec {
  key: FieldKey;
  zh: string;
  en: string;
  placeholder?: string;
  required?: boolean;
  mono?: boolean;
}

const LETTERHEAD_FIELDS: FieldSpec[] = [
  {
    key: "company_legal_name",
    zh: "公司法定全称",
    en: "Legal company name",
    placeholder: "Quanzhou Smart LED Lighting Co., Ltd.",
    required: true,
  },
  {
    key: "company_address",
    zh: "公司地址",
    en: "Company address",
    placeholder: "No. 88 Photoelectric Industrial Park, Quanzhou, Fujian, China",
  },
  {
    key: "contact_phone",
    zh: "联系电话",
    en: "Contact phone",
    placeholder: "+86 595 2688 0000",
  },
  {
    key: "contact_email",
    zh: "联系邮箱",
    en: "Contact email",
    placeholder: "sales@your-domain.com",
    required: true,
    mono: true,
  },
  {
    key: "website",
    zh: "官网",
    en: "Website",
    placeholder: "https://www.your-domain.com",
    mono: true,
  },
  {
    key: "tax_or_reg_no",
    zh: "税号 / 注册号",
    en: "Tax / registration no.",
    placeholder: "91350583MA2XQZLED8",
    mono: true,
  },
];

const BANK_FIELDS: FieldSpec[] = [
  {
    key: "bank_beneficiary_name",
    zh: "收款人名称（Beneficiary）",
    en: "Beneficiary name",
    required: true,
  },
  { key: "bank_name", zh: "开户银行（Bank Name）", en: "Bank name" },
  {
    key: "bank_account_no",
    zh: "银行账号（Account No.）",
    en: "Account no.",
    mono: true,
  },
  {
    key: "bank_swift_code",
    zh: "SWIFT / BIC",
    en: "SWIFT / BIC",
    mono: true,
  },
  { key: "bank_address", zh: "银行地址", en: "Bank address" },
];

function defaultSectionFields(): PiTemplateSettings {
  return {
    company_legal_name: "",
    company_address: "",
    contact_phone: "",
    contact_email: "",
    website: "",
    tax_or_reg_no: "",
    bank_beneficiary_name: "",
    bank_name: "",
    bank_account_no: "",
    bank_swift_code: "",
    bank_address: "",
    default_terms_notes: "",
    footer_signature_title: "",
  };
}

/**
 * PI 抬头模板：卖方抬头 / T/T 银行电汇资料 / 默认条款与签章三段式。
 * 所有字段由租户自己维护，PI PDF 渲染直接读这份设置——零硬编码。
 * 仅 owner / admin 挂载（`enabled` 控制是否发请求）。
 */
export function PiTemplateCard({ enabled = true }: { enabled?: boolean }) {
  const { bi } = useI18n();
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<PiTemplateSettings>(defaultSectionFields);
  const [savedAt, setSavedAt] = useState<number | null>(null);

  const templateQuery = useQuery({
    queryKey: ["tenants", "pi-template"],
    queryFn: () => fetchPiTemplateSettings(),
    enabled,
  });

  useEffect(() => {
    if (templateQuery.data) setDraft(templateQuery.data);
  }, [templateQuery.data]);

  const saveMutation = useMutation({
    mutationFn: (payload: PiTemplateSettings) => updatePiTemplateSettings(payload),
    onSuccess: (result) => {
      setDraft(result);
      setSavedAt(Date.now());
      void queryClient.invalidateQueries({ queryKey: ["tenants", "pi-template"] });
    },
  });

  const missingRequired = useMemo(
    () =>
      !draft.company_legal_name.trim() ||
      !EMAIL_PATTERN.test(draft.contact_email.trim()) ||
      !draft.bank_beneficiary_name.trim(),
    [draft.bank_beneficiary_name, draft.company_legal_name, draft.contact_email],
  );

  const errorMessage =
    saveMutation.error instanceof BffApiError
      ? saveMutation.error.detail
      : saveMutation.error?.message ?? null;

  function setField(key: FieldKey, value: string) {
    setDraft((previous) => ({ ...previous, [key]: value }));
    setSavedAt(null);
  }

  function renderField(spec: FieldSpec) {
    return (
      <div key={spec.key} className={spec.key === "company_address" ? "sm:col-span-2" : ""}>
        <label className="erp-label" htmlFor={`pi-template-${spec.key}`}>
          {bi(spec.zh, spec.en)}
          {spec.required && <span className="ml-0.5 text-rose-600">*</span>}
        </label>
        <input
          id={`pi-template-${spec.key}`}
          data-testid={`pi-template-${spec.key}`}
          type={spec.key === "contact_email" ? "email" : "text"}
          value={draft[spec.key]}
          onChange={(event) => setField(spec.key, event.target.value)}
          placeholder={spec.placeholder}
          className={`erp-input mt-1.5 ${spec.mono ? "font-mono text-[12.5px]" : ""}`}
        />
      </div>
    );
  }

  return (
    <div className="w-full space-y-3">
      <Card>
        <CardHeader
          title={bi("PI 抬头与收款资料", "PI letterhead & payment details")}
          description={bi(
            "这里填写的卖方抬头、银行电汇资料与默认条款会直接渲染进每一份 PI PDF，改完保存即可对全团队生效。",
            "Everything here is rendered into every PI PDF; save once and the whole team issues consistent documents."
          )}
          icon={ScrollText}
          actions={
            <button
              type="button"
              data-testid="pi-template-save-btn"
              disabled={!enabled || missingRequired || saveMutation.isPending}
              onClick={() => saveMutation.mutate(draft)}
              className="erp-btn erp-btn-primary rounded-xl disabled:opacity-50"
            >
              {saveMutation.isPending ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Save className="h-3.5 w-3.5" />
              )}
              <span>{bi("保存 PI 模板", "Save PI template")}</span>
            </button>
          }
        />

        <div className="space-y-4 p-4">
          {templateQuery.isLoading && (
            <p
              data-testid="pi-template-loading"
              className="text-[12.5px] font-medium text-stone-500"
            >
              {bi("正在加载 PI 模板…", "Loading PI template…")}
            </p>
          )}

          {templateQuery.isError && (
            <p
              role="alert"
              className="rounded-xl border border-rose-200 bg-rose-50 px-3 py-2.5 text-[12px] font-medium text-rose-800"
            >
              {templateQuery.error instanceof BffApiError
                ? templateQuery.error.detail
                : bi("PI 模板加载失败。", "Failed to load the PI template.")}
            </p>
          )}

          {savedAt !== null && (
            <div
              data-testid="pi-template-saved-badge"
              className="flex items-center gap-2 rounded-xl border border-emerald-200 bg-emerald-50 px-3 py-2.5 text-[12px] font-semibold text-emerald-900"
            >
              <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-600" />
              <span>
                {bi(
                  "已保存，之后生成的 PI PDF 全部使用新抬头与银行资料。",
                  "Saved — every PI PDF generated from now on uses the new letterhead and bank details."
                )}
              </span>
            </div>
          )}

          {errorMessage && (
            <p
              role="alert"
              data-testid="pi-template-error"
              className="rounded-xl border border-rose-200 bg-rose-50 px-3 py-2.5 text-[12px] font-medium text-rose-800"
            >
              {errorMessage}
            </p>
          )}

          <section className="space-y-3 rounded-2xl border border-[#EFECE6] bg-white p-3.5">
            <h3 className="flex items-center gap-1.5 text-[13px] font-bold text-zinc-950">
              <Building2 className="h-4 w-4 text-emerald-600" />
              {bi("卖方公司抬头", "Seller letterhead")}
            </h3>
            <div className="grid gap-3 sm:grid-cols-2">
              {LETTERHEAD_FIELDS.map(renderField)}
            </div>
          </section>

          <section className="space-y-3 rounded-2xl border border-[#EFECE6] bg-white p-3.5">
            <h3 className="flex items-center gap-1.5 text-[13px] font-bold text-zinc-950">
              <Landmark className="h-4 w-4 text-emerald-600" />
              {bi("银行电汇资料（T/T）", "T/T bank wire details")}
            </h3>
            <div className="grid gap-3 sm:grid-cols-2">
              {BANK_FIELDS.map(renderField)}
            </div>
            <p className="rounded-xl bg-[#FAF8F5] px-3 py-2 text-[11.5px] leading-5 text-stone-600">
              {bi(
                "银行资料会原样印在 PI 的汇款指引区，请与开户行回单保持一致。",
                "These details print verbatim in the PI remittance block — keep them identical to the bank receipt."
              )}
            </p>
          </section>

          <section className="space-y-3 rounded-2xl border border-[#EFECE6] bg-white p-3.5">
            <h3 className="flex items-center gap-1.5 text-[13px] font-bold text-zinc-950">
              <PenLine className="h-4 w-4 text-emerald-600" />
              {bi("默认贸易条款与签章", "Default terms & signature")}
            </h3>
            <div>
              <label className="erp-label" htmlFor="pi-template-default_terms_notes">
                {bi("默认条款说明", "Default terms notes")}
              </label>
              <textarea
                id="pi-template-default_terms_notes"
                data-testid="pi-template-default_terms_notes"
                rows={5}
                value={draft.default_terms_notes}
                onChange={(event) => setField("default_terms_notes", event.target.value)}
                placeholder={bi(
                  "例如：30% T/T 定金 + 70% 见提单副本付清；交期 7-15 天；汇率锁定 48 小时。",
                  "e.g. 30% T/T deposit + 70% against B/L copy; lead time 7-15 days; FX locked for 48h."
                )}
                className="erp-input mt-1.5 resize-y leading-6"
              />
            </div>
            <div className="sm:w-1/2">
              <label className="erp-label" htmlFor="pi-template-footer_signature_title">
                {bi("签章栏标题", "Signature title")}
              </label>
              <input
                id="pi-template-footer_signature_title"
                data-testid="pi-template-footer_signature_title"
                type="text"
                value={draft.footer_signature_title}
                onChange={(event) =>
                  setField("footer_signature_title", event.target.value)
                }
                placeholder={bi("例如：出口部销售经理", "e.g. Export Sales Manager")}
                className="erp-input mt-1.5"
              />
            </div>
          </section>
        </div>
      </Card>
    </div>
  );
}
