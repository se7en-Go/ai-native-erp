"use client";

import React, { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle2,
  Copy,
  Rocket,
  ShieldAlert,
  Sparkles,
  Wallet,
  Workflow,
} from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { BentoStatCard } from "@/components/ui/bento-stat-card";
import { Badge, Card, CardHeader, Page, PageHeader } from "@/components/ui/page";
import {
  advanceOnboarding,
  completeWhatsappEmbeddedSignup,
  runSandboxTest,
  updateOnboardingPreset,
  updateTenantSettings,
  verifyEmailDns,
} from "@/lib/api-client/live-client";
import type { SandboxScenarioId } from "@/lib/api-client/live-client";
import { connectStorefront, saveWebchat } from "@/lib/api-client/plan04-bridge";
import { fetchOnboardingState, fetchPendingProposalsCount } from "@/lib/data-source";
import { useI18n } from "@/lib/i18n";

type StorefrontTab = "Shopify" | "WooCommerce" | "SHOPLINE" | "Shoplazza" | "TikTok Shop";

const SANDBOX_SCENARIOS: Array<{ id: SandboxScenarioId; zh: string; en: string; message: string }> = [
  {
    id: "b2c_wismo",
    zh: "1. B2C 查单与 17TRACK 物流查询",
    en: "1. B2C WISMO order & 17TRACK lookup",
    message: "Hi, where is my order #SH-1042? Can you check tracking?",
  },
  {
    id: "strict_rag_warranty",
    zh: "2. 严格检索模式下的保修条款引用",
    en: "2. Strict RAG warranty citation check",
    message: "What is the warranty and IP rating for COB 24V LED strip?",
  },
  {
    id: "b2b_moq_quote",
    zh: "3. B2B 500 支 FOB 阶梯价报价提案",
    en: "3. B2B 500 pcs FOB MOQ quote proposal",
    message: "Need FOB Shenzhen quote for 500 pcs LED-COB-24V-5M, 30% T/T.",
  },
];

const STEP_KEYS = [
  { zh: "工厂预设", en: "Factory preset" },
  { zh: "WhatsApp 与邮箱", en: "WhatsApp & email" },
  { zh: "店铺接入", en: "Storefronts" },
  { zh: "网页客服嵌入", en: "WebChat embed" },
  { zh: "AI 沙箱验证", en: "AI sandbox" },
];

export default function OnboardingPage() {
  const { bi } = useI18n();
  const queryClient = useQueryClient();
  const [step, setStep] = useState<1 | 2 | 3 | 4 | 5>(1);
  const [preset, setPreset] = useState<
    "hybrid_factory_b2b_b2c" | "dtc_brand" | "pure_b2b_inquiry"
  >("hybrid_factory_b2b_b2c");
  const [companyName, setCompanyName] = useState("泉州市智能光电有限公司");
  const [baseCurrency, setBaseCurrency] = useState("CNY");
  const [ianaTimezone, setIanaTimezone] = useState("Asia/Shanghai");

  const [isCoexistence, setIsCoexistence] = useState(false);
  const [coexistenceAck, setCoexistenceAck] = useState(false);
  const [dnsStatus, setDnsStatus] = useState<{
    spf_verified: boolean;
    dkim_verified: boolean;
    return_path_verified: boolean;
  }>();
  const dnsVerified = Boolean(
    dnsStatus?.spf_verified && dnsStatus.dkim_verified && dnsStatus.return_path_verified
  );
  const [emailDomain, setEmailDomain] = useState("mail.qzled.example.com");

  const [storefrontTab, setStorefrontTab] = useState<StorefrontTab>("Shopify");
  const [shopDomain, setShopDomain] = useState("qzled.myshopify.com");
  const [byokKey, setByokKey] = useState("");
  const [byokSecret, setByokSecret] = useState("");
  const [tiktokMode, setTiktokMode] = useState<"custom_app_25" | "seller_byok" | "email_bridge">(
    "custom_app_25"
  );

  const [allowedOriginsText, setAllowedOriginsText] = useState("https://shop.qzled.example.com");
  const [sandboxPassed, setSandboxPassed] = useState<Record<string, string>>({});
  const [copiedNotice, setCopiedNotice] = useState<string | null>(null);

  const pendingProposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () => fetchPendingProposalsCount(),
  });

  const stateQuery = useQuery({
    queryKey: ["onboarding", "state"],
    queryFn: () => fetchOnboardingState(),
  });

  useEffect(() => {
    if (stateQuery.data) {
      setPreset(stateQuery.data.preset_type || "hybrid_factory_b2b_b2c");
      setBaseCurrency(stateQuery.data.base_currency || "CNY");
      setIanaTimezone(stateQuery.data.iana_timezone || "Asia/Shanghai");
      setAllowedOriginsText((stateQuery.data.allowed_origins ?? []).join(", "));
      if (stateQuery.data.dns_status) setDnsStatus(stateQuery.data.dns_status);
      const savedStep = {
        WorkspaceCreated: 1,
        ChannelConnecting: 2,
        ChannelVerified: 3,
        CatalogSyncing: 4,
        AISandboxReady: 5,
        LiveProduction: 5,
      } as const;
      const next = savedStep[stateQuery.data.current_stage] ?? 1;
      setStep((current) => (current < next ? next : current));
    }
  }, [stateQuery.data]);

  const saveWorkspaceMutation = useMutation({
    mutationFn: async () => {
      await updateOnboardingPreset(preset);
      await updateTenantSettings({
        name: companyName,
        base_currency: baseCurrency,
        timezone: ianaTimezone,
      });
      return advanceOnboarding("ChannelConnecting", {
        preset_type: preset,
        base_currency: baseCurrency,
        iana_timezone: ianaTimezone,
      });
    },
    onSuccess: () => {
      setStep(2);
    },
  });

  const metaSignupMutation = useMutation({
    mutationFn: () =>
      // Contract route is /v1/channels/whatsapp/embedded-signup (not
      // /v1/onboarding/whatsapp/embedded-signup), and EmbeddedSignupRequest
      // has no coexistence_checklist_ack field — the checkbox only gates
      // the button in the UI.
      completeWhatsappEmbeddedSignup({
        code: "meta_v4_auth_code_demo",
        waba_id: "waba_qz_001",
        phone_number_id: "phone_qz_001",
        is_coexistence: isCoexistence,
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["onboarding", "state"] });
    },
  });

  const verifyDnsMutation = useMutation({
    mutationFn: () => verifyEmailDns(emailDomain),
    onSuccess: setDnsStatus,
  });

  const nextMutation = useMutation({
    mutationFn: async (from: 2 | 3 | 4) => {
      if (from === 2) {
        await advanceOnboarding("ChannelVerified", {
          dns_status: dnsStatus,
          coexistence_acknowledged: isCoexistence && coexistenceAck,
        });
      } else if (from === 3) {
        await advanceOnboarding("CatalogSyncing", {});
      } else {
        await advanceOnboarding("AISandboxReady", {});
      }
    },
    onSuccess: (_, from) => setStep(from === 2 ? 3 : from === 3 ? 4 : 5),
  });

  const connectStorefrontMutation = useMutation({
    mutationFn: () =>
      connectStorefront({
        platform: storefrontTab.toLowerCase().replace(" ", "_"),
        shop_domain: shopDomain,
        auth_mode: storefrontTab === "TikTok Shop" ? tiktokMode : "oauth",
        credentials: byokKey ? { key: byokKey, secret: byokSecret } : {},
      }),
  });

  const saveWebchatMutation = useMutation({
    mutationFn: () =>
      saveWebchat(
        allowedOriginsText
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean)
      ),
  });

  const sandboxTestMutation = useMutation({
    mutationFn: ({ scenarioId, message }: { scenarioId: SandboxScenarioId; message: string }) =>
      runSandboxTest(scenarioId, message),
    onSuccess: (res, vars) => {
      if (res.passed) {
        setSandboxPassed((prev) => ({ ...prev, [vars.scenarioId]: res.reply_text ?? "" }));
      }
    },
  });

  const completeMutation = useMutation({
    mutationFn: () => advanceOnboarding("LiveProduction", { sandbox_passed: 3 }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["onboarding", "state"] });
    },
  });

  const forwardingAddr =
    stateQuery.data?.inbound_forwarding_address ?? "quanzhou-led-factory@inbound.mail.example.com";
  const siteKey = stateQuery.data?.site_key ?? "wc_site_qz_001";
  const snippetCode = `<script src="https://cdn.erp.local/widget.js" data-site-key="${siteKey}" async></script>`;

  function copyText(text: string, label: string) {
    if (navigator.clipboard) {
      void navigator.clipboard.writeText(text);
    }
    setCopiedNotice(`${bi("已复制", "Copied")} ${label}`);
  }

  const all3SandboxPassed = Object.keys(sandboxPassed).length >= 3;

  const progressPct = Math.round((step / 5) * 100);

  return (
    <AppShell
      currentPath="/onboarding"
      pendingProposalsCount={pendingProposalsQuery.data?.total ?? 0}
    >
      <Page>
        <PageHeader
          title={bi("工厂快速上线向导", "Self-serve factory onboarding")}
          description={bi(
            "五步完成 B2B + B2C 混合工厂上线：业务预设、WhatsApp 与邮箱桥接、店铺接入、网页客服嵌入、AI 沙箱验证。",
            "Five steps to launch a hybrid B2B + B2C factory: preset, WhatsApp & email bridge, storefronts, WebChat embed and AI sandbox verification."
          )}
          badge={
            <Badge tone={step === 5 && all3SandboxPassed ? "success" : "info"}>
              {bi("第", "Step")} {step} / 5
            </Badge>
          }
        />

        <Card className="overflow-hidden">
          <div className="flex items-start justify-between gap-1.5 border-b border-slate-200/90 bg-slate-50/60 px-4 py-4">
            {STEP_KEYS.map((label, idx) => {
              const num = (idx + 1) as 1 | 2 | 3 | 4 | 5;
              const done = step > num;
              const active = step === num;
              return (
                <div
                  key={label.zh}
                  aria-current={active ? "step" : undefined}
                  className="relative flex min-w-0 flex-1 flex-col items-center gap-1.5 text-center"
                >
                  {idx > 0 && (
                    <span
                      aria-hidden="true"
                      className={`absolute top-4 right-1/2 h-[2px] w-[calc(100%+0.375rem)] rounded-full ${
                        step > idx ? "bg-emerald-400" : "bg-slate-200"
                      }`}
                    />
                  )}
                  <span
                    className={`relative z-10 grid h-8 w-8 shrink-0 place-items-center rounded-full text-[11.5px] font-bold transition-colors ${
                      active
                        ? "bg-blue-600 text-white shadow-[0_0_0_4px_rgba(37,99,235,0.15)]"
                        : done
                          ? "bg-emerald-500 text-white"
                          : "border border-slate-200 bg-white text-slate-500"
                    }`}
                  >
                    {done ? <CheckCircle2 className="h-4 w-4" /> : num}
                  </span>
                  <span
                    className={`min-w-0 max-w-full truncate text-[11px] font-semibold ${
                      active ? "text-blue-800" : done ? "text-emerald-700" : "text-slate-600"
                    }`}
                  >
                    {bi(label.zh, label.en)}
                  </span>
                </div>
              );
            })}
          </div>
          <div className="px-4 py-3">
            <div className="flex items-center justify-between gap-2 text-[11.5px] font-semibold text-slate-600">
              <span>
                {bi("上线进度", "Launch progress")} · {bi("第", "Step")} {step} / 5
              </span>
              <span className="tabular-nums text-blue-700">{progressPct}%</span>
            </div>
            <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-slate-100">
              <div
                className="h-1.5 rounded-full bg-gradient-to-r from-blue-500 to-blue-600 transition-[width] duration-300"
                style={{ width: `${progressPct}%` }}
              />
            </div>
            {copiedNotice && (
              <div className="mt-2 inline-flex items-center gap-1 rounded-lg border border-emerald-200 bg-emerald-50 px-2 py-1 text-[12px] font-semibold text-emerald-800">
                <CheckCircle2 className="h-3 w-3" />
                {copiedNotice}
              </div>
            )}
          </div>
        </Card>

        {(saveWorkspaceMutation.error || nextMutation.error || completeMutation.error) && (
          <p role="alert" className="text-[13px] text-rose-700">
            {(saveWorkspaceMutation.error ?? nextMutation.error ?? completeMutation.error)?.message}
          </p>
        )}

        {step === 1 && (
          <Card>
            <CardHeader
              title={bi("第 1 步：业务预设、本位币与时区", "Step 1: business preset, base currency & timezone")}
              description={bi(
                "预设决定默认渠道组合与单据模板；本位币用于 CVP 汇总，时区决定买家当地时间的静默时段判断。",
                "The preset drives default channels and document templates; base currency drives CVP aggregation and timezone drives buyer quiet hours."
              )}
            />
            <div className="grid gap-3 p-4 md:grid-cols-2">
              <div>
                <label className="erp-label" htmlFor="onboarding-preset-select">
                  {bi("业务预设", "Business preset")}
                </label>
                <select
                  id="onboarding-preset-select"
                  data-testid="onboarding-preset-select"
                  value={preset}
                  onChange={(e) =>
                    setPreset(
                      e.target.value as
                        | "hybrid_factory_b2b_b2c"
                        | "dtc_brand"
                        | "pure_b2b_inquiry"
                    )
                  }
                  className="erp-input mt-1.5"
                >
                  <option value="hybrid_factory_b2b_b2c">
                    {bi(
                      "工贸一体 B2B + B2C 工厂（泉州智能光电预设）",
                      "Hybrid B2B+B2C factory (Quanzhou Smart LED preset)"
                    )}
                  </option>
                  <option value="dtc_brand">
                    {bi("DTC 独立站品牌", "DTC independent store brand")}
                  </option>
                  <option value="pure_b2b_inquiry">
                    {bi("纯 B2B 外贸工厂", "Pure B2B foreign trade factory")}
                  </option>
                </select>
              </div>
              <div>
                <label className="erp-label" htmlFor="onboarding-company-name">
                  {bi("工厂 / 法定名称", "Factory / legal name")}
                </label>
                <input
                  id="onboarding-company-name"
                  type="text"
                  value={companyName}
                  onChange={(e) => setCompanyName(e.target.value)}
                  className="erp-input mt-1.5"
                />
              </div>
              <div>
                <label className="erp-label" htmlFor="onboarding-base-currency">
                  {bi("本位币", "Base currency")}
                </label>
                <select
                  id="onboarding-base-currency"
                  value={baseCurrency}
                  onChange={(e) => setBaseCurrency(e.target.value)}
                  className="erp-input mt-1.5"
                >
                  <option value="CNY">CNY · 人民币</option>
                  <option value="USD">USD · 美元</option>
                </select>
              </div>
              <div>
                <label className="erp-label" htmlFor="onboarding-timezone">
                  {bi("IANA 时区", "IANA timezone")}
                </label>
                <input
                  id="onboarding-timezone"
                  type="text"
                  value={ianaTimezone}
                  onChange={(e) => setIanaTimezone(e.target.value)}
                  className="erp-input mt-1.5"
                />
              </div>
            </div>
            <div className="flex justify-end border-t border-slate-200/90 bg-slate-50/60 p-4">
              <button
                type="button"
                data-testid="onboarding-save-step1"
                disabled={saveWorkspaceMutation.isPending}
                onClick={() => saveWorkspaceMutation.mutate()}
                className="erp-btn erp-btn-primary"
              >
                {bi("保存并继续", "Save & continue")}
              </button>
            </div>
          </Card>
        )}

        {step === 2 && (
          <div className="space-y-4">
            <Card>
              <CardHeader
                title={bi("第 2A 步：Meta 嵌入式注册 v4 与 WhatsApp 共存模式", "Step 2A: Meta embedded signup v4 & WhatsApp coexistence")}
                description={bi(
                  "共存模式可保留业务员手机上的 WhatsApp Business App，并同步 180 天内的一对一会话。",
                  "Coexistence keeps the WhatsApp Business App on the sales rep's phone and syncs 180 days of 1:1 chats."
                )}
              />
              <div className="space-y-3 p-4">
                <label className="inline-flex cursor-pointer items-start gap-2 text-[13px] font-medium text-slate-700">
                  <input
                    type="checkbox"
                    data-testid="coexistence-mode-toggle"
                    checked={isCoexistence}
                    onChange={(e) => setIsCoexistence(e.target.checked)}
                    className="mt-0.5"
                  />
                  <span>
                    {bi(
                      "启用 WhatsApp Business App 共存模式（保留手机 App 并同步 180 天一对一聊天）",
                      "Enable WhatsApp Business App coexistence (keep the mobile app and sync 180 days of 1:1 chats)"
                    )}
                  </span>
                </label>

                {isCoexistence && (
                  <div className="space-y-2 rounded-xl border border-amber-200 bg-amber-50 p-3 text-[12px] text-amber-950">
                    <div className="flex items-center gap-1.5 font-semibold">
                      <ShieldAlert className="h-4 w-4 text-amber-600" />
                      <span>{bi("共存模式 20 mps 与平台限制确认", "Coexistence 20 mps & platform constraints checklist")}</span>
                    </div>
                    <ul className="list-disc space-y-1 pl-5 leading-6">
                      <li>
                        {bi(
                          "Cloud API 吞吐上限降为 20 mps（标准为 80 mps）。",
                          "Cloud API throughput is capped at 20 mps (instead of 80 mps)."
                        )}
                      </li>
                      <li>
                        {bi(
                          "180 天一对一聊天 24 小时内同步；群聊不同步。",
                          "180-day 1:1 chats sync within 24h; group chats do not sync."
                        )}
                      </li>
                      <li>
                        {bi(
                          "手机端将禁用阅后即焚与新建广播列表。",
                          "Disappearing messages and new broadcast lists are disabled on mobile."
                        )}
                      </li>
                    </ul>
                    <label className="flex items-start gap-2 font-medium">
                      <input
                        type="checkbox"
                        data-testid="coexistence-20mps-checkbox"
                        checked={coexistenceAck}
                        onChange={(e) => setCoexistenceAck(e.target.checked)}
                        className="mt-0.5"
                      />
                      <span>
                        {bi(
                          "我已知悉 20 mps 共存限制与手机端功能约束",
                          "I acknowledge the 20 mps coexistence limit and mobile constraints"
                        )}
                      </span>
                    </label>
                  </div>
                )}

                <button
                  type="button"
                  data-testid="meta-embedded-signup-btn"
                  disabled={isCoexistence && !coexistenceAck}
                  onClick={() => metaSignupMutation.mutate()}
                  className="erp-btn bg-emerald-600 text-white hover:bg-emerald-700 disabled:opacity-45"
                >
                  {bi("启动 Meta 嵌入式注册 v4", "Launch Meta Embedded Signup v4")}
                </button>
              </div>
            </Card>

            <Card>
              <CardHeader
                title={bi("第 2B 步：零 CASA 邮箱桥接、Made-in-China 转发与 SPF/DKIM", "Step 2B: zero-CASA email bridge, Made-in-China forwarding & SPF/DKIM")}
                description={bi(
                  "把询盘邮箱转发到专属入站地址即可，无需域名解析商授权；发送域仍需 SPF 与 2048 位 DKIM。",
                  "Forward the inquiry mailbox to your dedicated inbound address with no registrar authorisation; the sending domain still needs SPF and 2048-bit DKIM."
                )}
              />
              <div className="space-y-3 p-4">
                <div className="flex flex-wrap items-center gap-2">
                  <code className="rounded-lg border border-slate-200 bg-slate-50 px-2.5 py-1.5 font-mono text-[12px] text-slate-800">
                    {forwardingAddr}
                  </code>
                  <button
                    type="button"
                    data-testid="copy-email-forwarding-btn"
                    onClick={() => copyText(forwardingAddr, bi("入站转发地址", "inbound forwarding address"))}
                    className="erp-btn erp-btn-secondary"
                  >
                    <Copy className="h-3.5 w-3.5" />
                    <span>{bi("复制地址", "Copy address")}</span>
                  </button>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <input
                    type="text"
                    value={emailDomain}
                    onChange={(e) => setEmailDomain(e.target.value)}
                    aria-label={bi("发信域名", "Sending domain")}
                    className="erp-input w-64"
                  />
                  <button
                    type="button"
                    data-testid="verify-email-dns-btn"
                    onClick={() => verifyDnsMutation.mutate()}
                    className="erp-btn bg-slate-900 text-white hover:bg-slate-800"
                  >
                    {bi("验证 SPF 与 2048 位 DKIM", "Verify SPF & 2048-bit DKIM")}
                  </button>
                  {dnsVerified && (
                    <span
                      data-testid="dns-status-verified"
                      className="inline-flex items-center gap-1 text-[12px] font-medium text-emerald-700"
                    >
                      <CheckCircle2 className="h-4 w-4" />
                      <span>{bi("SPF + DKIM 已验证", "SPF + DKIM verified")}</span>
                    </span>
                  )}
                </div>
              </div>
              <div className="flex justify-end border-t border-slate-200/90 bg-slate-50/60 p-4">
                <button
                  type="button"
                  data-testid="onboarding-next-btn"
                  disabled={!dnsVerified || nextMutation.isPending}
                  onClick={() => nextMutation.mutate(2)}
                  className="erp-btn erp-btn-primary disabled:opacity-45"
                >
                  {bi("下一步：店铺接入", "Next: storefront connectors")}
                </button>
              </div>
            </Card>
          </div>
        )}

        {step === 3 && (
          <Card>
            <CardHeader
              title={bi("第 3 步：P0 店铺接入（OAuth 与合规 BYOK）", "Step 3: P0 storefronts (OAuth & compliant BYOK)")}
              description={bi(
                "Shopify 走标准 OAuth；WooCommerce / SHOPLINE / Shoplazza 使用自有 API Key；TikTok Shop 支持自定义应用与卖家 BYOK。",
                "Shopify uses standard OAuth; WooCommerce / SHOPLINE / Shoplazza use your own API keys; TikTok Shop supports a custom app and seller BYOK."
              )}
            />
            <div className="space-y-4 p-4">
              <div className="flex flex-wrap gap-1.5">
                {(
                  ["Shopify", "WooCommerce", "SHOPLINE", "Shoplazza", "TikTok Shop"] as StorefrontTab[]
                ).map((tab) => (
                  <button
                    key={tab}
                    type="button"
                    onClick={() => setStorefrontTab(tab)}
                    aria-pressed={storefrontTab === tab}
                    className={`rounded-xl border px-3 py-1.5 text-[12px] font-semibold transition-colors ${
                      storefrontTab === tab
                        ? "border-blue-600 bg-blue-600 text-white shadow-[0_2px_8px_-2px_rgba(37,99,235,0.55)]"
                        : "border-slate-200 bg-white text-slate-700 hover:border-slate-300 hover:bg-slate-50"
                    }`}
                  >
                    {tab}
                  </button>
                ))}
              </div>

              <div className="space-y-3 rounded-2xl border border-slate-200 bg-slate-50/60 p-3.5">
                <div>
                  <label className="erp-label" htmlFor="storefront-domain">
                    {bi(`${storefrontTab} 店铺域名 / 句柄`, `${storefrontTab} store domain / handle`)}
                  </label>
                  <input
                    id="storefront-domain"
                    type="text"
                    value={shopDomain}
                    onChange={(e) => setShopDomain(e.target.value)}
                    className="erp-input mt-1.5"
                  />
                </div>

                {storefrontTab === "TikTok Shop" && (
                  <div>
                    <label className="erp-label" htmlFor="tiktok-auth-mode">
                      {bi("授权模式", "Authorization mode")}
                    </label>
                    <select
                      id="tiktok-auth-mode"
                      value={tiktokMode}
                      onChange={(e) => setTiktokMode(e.target.value as typeof tiktokMode)}
                      className="erp-input mt-1.5"
                    >
                      <option value="custom_app_25">
                        {bi("自定义应用（25 店以内核心履约）", "Custom app (<25 stores core fulfilment)")}
                      </option>
                      <option value="seller_byok">
                        {bi("TikTok Shop 卖家 BYOK（自有客服 API）", "TikTok Shop seller BYOK (in-house CS API)")}
                      </option>
                      <option value="email_bridge">
                        {bi("卖家邮箱转工单桥接", "Seller inbox email-to-ticket bridge")}
                      </option>
                    </select>
                  </div>
                )}

                {(storefrontTab === "WooCommerce" ||
                  storefrontTab === "SHOPLINE" ||
                  storefrontTab === "Shoplazza" ||
                  storefrontTab === "TikTok Shop") && (
                  <div className="grid gap-2 md:grid-cols-2">
                    <input
                      type="text"
                      value={byokKey}
                      onChange={(e) => setByokKey(e.target.value)}
                      placeholder={bi("Consumer Key / 私有应用 Key（OAuth 可留空）", "Consumer key / private app key (optional for OAuth)")}
                      aria-label={bi("应用 Key", "App key")}
                      className="erp-input"
                    />
                    <input
                      type="password"
                      value={byokSecret}
                      onChange={(e) => setByokSecret(e.target.value)}
                      placeholder={bi("Consumer Secret / 访问令牌", "Consumer secret / access token")}
                      aria-label={bi("应用密钥", "App secret")}
                      className="erp-input"
                    />
                  </div>
                )}

                <button
                  type="button"
                  onClick={() => connectStorefrontMutation.mutate()}
                  className="erp-btn bg-slate-900 text-white hover:bg-slate-800"
                >
                  {bi(`连接 ${storefrontTab}`, `Connect ${storefrontTab}`)}
                </button>
              </div>
            </div>
            <div className="flex justify-end border-t border-slate-200/90 bg-slate-50/60 p-4">
              <button
                type="button"
                data-testid="onboarding-next-btn"
                disabled={nextMutation.isPending}
                onClick={() => nextMutation.mutate(3)}
                className="erp-btn erp-btn-primary"
              >
                {bi("下一步：网页客服嵌入", "Next: WebChat embed")}
              </button>
            </div>
          </Card>
        )}

        {step === 4 && (
          <Card>
            <CardHeader
              title={bi("第 4 步：独立站网页客服 <script> 嵌入", "Step 4: storefront WebChat <script> snippet")}
              description={bi(
                "只允许白名单来源加载组件；脚本异步加载，不阻塞店铺首屏。",
                "Only allow-listed origins can load the widget; the script loads async and never blocks the storefront."
              )}
            />
            <div className="space-y-4 p-4">
              <div>
                <label className="erp-label" htmlFor="webchat-origins">
                  {bi("允许的来源（逗号分隔）", "Allowed origins (comma separated)")}
                </label>
                <input
                  id="webchat-origins"
                  type="text"
                  value={allowedOriginsText}
                  onChange={(e) => setAllowedOriginsText(e.target.value)}
                  className="erp-input mt-1.5"
                />
              </div>

              <div className="break-all rounded-2xl bg-[#0B1220] p-3.5 font-mono text-[11px] leading-6 text-emerald-300 ring-1 ring-slate-900/10">
                {snippetCode}
              </div>

              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  data-testid="copy-webchat-snippet-btn"
                  onClick={() => copyText(snippetCode, "WebChat Snippet")}
                  className="erp-btn erp-btn-secondary"
                >
                  <Copy className="h-3.5 w-3.5" />
                  <span>{bi("复制 <script> 片段", "Copy <script> snippet")}</span>
                </button>
                <button
                  type="button"
                  onClick={() => saveWebchatMutation.mutate()}
                  className="erp-btn bg-slate-900 text-white hover:bg-slate-800"
                >
                  {bi("保存来源白名单", "Save allowed origins")}
                </button>
              </div>
            </div>
            <div className="flex justify-end border-t border-slate-200/90 bg-slate-50/60 p-4">
              <button
                type="button"
                data-testid="onboarding-next-btn"
                disabled={nextMutation.isPending}
                onClick={() => nextMutation.mutate(4)}
                className="erp-btn erp-btn-primary"
              >
                {bi("下一步：3 条消息 AI 沙箱", "Next: 3-message AI sandbox")}
              </button>
            </div>
          </Card>
        )}

        {step === 5 && (
          <div className="space-y-4">
            <Card>
              <CardHeader
                title={bi("第 5 步：3 条消息 AI 沙箱验证（进入正式环境的门槛）", "Step 5: 3-message AI sandbox verifier (gate to LiveProduction)")}
                description={bi(
                  "三条真实业务消息全部通过后，才允许激活正式环境；失败不写入生产数据。",
                  "All three real business messages must pass before LiveProduction can be activated; failures never write production data."
                )}
                actions={
                  <Badge tone={all3SandboxPassed ? "success" : "warning"}>
                    {Object.keys(sandboxPassed).length}/3 {bi("已通过", "passed")}
                  </Badge>
                }
              />
              <div className="space-y-2.5 p-4">
                {SANDBOX_SCENARIOS.map((sc, idx) => {
                  const reply = sandboxPassed[sc.id];
                  return (
                    <div
                      key={sc.id}
                      className="space-y-1.5 rounded-2xl border border-slate-200 bg-white p-3.5 shadow-[0_1px_2px_rgba(15,23,42,0.04)]"
                    >
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <span className="text-[13px] font-medium text-slate-900">
                          {bi(sc.zh, sc.en)}
                        </span>
                        <button
                          type="button"
                          data-testid={`run-sandbox-test-${idx + 1}`}
                          onClick={() =>
                            sandboxTestMutation.mutate({ scenarioId: sc.id, message: sc.message })
                          }
                          className="erp-btn erp-btn-primary py-1 text-[12px]"
                        >
                          <Sparkles className="h-3 w-3" />
                          <span>{bi("运行测试", "Run test")}</span>
                        </button>
                      </div>
                      <div className="text-[12px] text-slate-500">
                        {bi("测试消息", "Prompt")}：&ldquo;{sc.message}&rdquo;
                      </div>
                      {reply && (
                        <div className="rounded-xl border border-emerald-200 bg-emerald-50 px-2.5 py-1.5 text-[12px] font-semibold text-emerald-900">
                          {bi("通过", "PASS")}: {reply}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
              <div className="flex flex-wrap items-center justify-between gap-2 border-t border-slate-200/90 bg-slate-50/60 p-4">
                <p className="text-[11px] text-slate-500">
                  {bi(
                    "激活后所有 AI 提案仍需人工审核，资金动作永久锁定 L0。",
                    "After activation every AI proposal still needs human review; cash actions stay permanently L0."
                  )}
                </p>
                <button
                  type="button"
                  data-testid="activate-live-production-btn"
                  disabled={!all3SandboxPassed || completeMutation.isPending}
                  onClick={() => completeMutation.mutate()}
                  className="erp-btn bg-emerald-600 font-semibold text-white hover:bg-emerald-700 disabled:opacity-45"
                >
                  <Rocket className="h-4 w-4" />
                  <span>{bi("激活正式环境", "Activate Live Production")}</span>
                </button>
              </div>
            </Card>

            <section
              aria-label={bi("上线检查关键指标", "Launch readiness metrics")}
              className="grid gap-3 sm:grid-cols-3"
            >
              <BentoStatCard
                label={bi("已通过场景", "Sandbox scenarios")}
                value={`${Object.keys(sandboxPassed).length} / 3`}
                icon={Sparkles}
                accent={all3SandboxPassed ? "emerald" : "amber"}
                hint={bi("三条业务消息全部通过才可激活", "All three must pass before activation")}
              />
              <BentoStatCard
                label={bi("业务预设", "Preset")}
                value={<span className="text-[15px] leading-tight">{preset}</span>}
                icon={Workflow}
                accent="blue"
                hint={bi("决定默认渠道与单据模板", "Drives default channels and templates")}
              />
              <BentoStatCard
                label={bi("本位币", "Base currency")}
                value={baseCurrency}
                icon={Wallet}
                accent="violet"
                hint={bi("用于 CVP 汇总与利润核算", "Used for CVP aggregation")}
              />
            </section>
          </div>
        )}
      </Page>
    </AppShell>
  );
}
