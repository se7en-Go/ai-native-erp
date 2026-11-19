"use client";

import React, { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Copy, ShieldAlert, Sparkles, Rocket } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import {
  advanceOnboarding,
  completeWhatsappEmbeddedSignup,
  updateOnboardingPreset,
  updateTenantSettings,
} from "@/lib/api-client/live-client";
import {
  connectStorefront,
  runSandboxTest,
  saveWebchat,
  verifyEmailDns,
} from "@/lib/api-client/plan04-bridge";
import { fetchOnboardingState, fetchPendingProposalsCount } from "@/lib/data-source";

type StorefrontTab = "Shopify" | "WooCommerce" | "SHOPLINE" | "Shoplazza" | "TikTok Shop";

const SANDBOX_SCENARIOS = [
  {
    id: "b2c_wismo",
    label: "1. B2C WISMO Order & 17TRACK Lookup",
    message: "Hi, where is my order #SH-1042? Can you check tracking?",
  },
  {
    id: "strict_rag_warranty",
    label: "2. Strict RAG Warranty Citation Check",
    message: "What is the warranty and IP rating for COB 24V LED strip?",
  },
  {
    id: "b2b_moq_quote",
    label: "3. B2B 500 pcs FOB MOQ Quote Proposal",
    message: "Need FOB Shenzhen quote for 500 pcs LED-COB-24V-5M, 30% T/T.",
  },
];

export default function OnboardingPage() {
  const queryClient = useQueryClient();
  const [step, setStep] = useState<1 | 2 | 3 | 4 | 5>(1);
  const [preset, setPreset] = useState<
    "hybrid_factory_b2b_b2c" | "dtc_brand" | "pure_b2b_inquiry"
  >("hybrid_factory_b2b_b2c");
  const [companyName, setCompanyName] = useState("Quanzhou Smart LED Lighting Co., Ltd.");
  const [baseCurrency, setBaseCurrency] = useState("CNY");
  const [ianaTimezone, setIanaTimezone] = useState("Asia/Shanghai");

  const [isCoexistence, setIsCoexistence] = useState(false);
  const [coexistenceAck, setCoexistenceAck] = useState(false);
  const [dnsVerified, setDnsVerified] = useState(false);
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
      setDnsVerified(
        Boolean(
          stateQuery.data.dns_status?.spf_verified && stateQuery.data.dns_status?.dkim_verified
        )
      );
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
      return advanceOnboarding("ChannelConnecting", { preset_type: preset });
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
    onSuccess: (res) => {
      setDnsVerified(res.spf_verified && res.dkim_verified);
    },
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
          .filter(Boolean),
      ),
  });

  const sandboxTestMutation = useMutation({
    mutationFn: ({ scenarioId, message }: { scenarioId: string; message: string }) =>
      runSandboxTest(scenarioId, message),
    onSuccess: (res, vars) => {
      if (res.passed) {
        setSandboxPassed((prev) => ({ ...prev, [vars.scenarioId]: res.reply_text }));
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
    setCopiedNotice(`Copied ${label}`);
  }

  const all3SandboxPassed = Object.keys(sandboxPassed).length >= 3;

  return (
    <AppShell
      currentPath="/onboarding"
      pendingProposalsCount={pendingProposalsQuery.data?.total ?? 0}
    >
      <div className="mx-auto max-w-4xl space-y-5">
        <div className="rounded-lg border border-slate-200 bg-white p-4">
          <h1 className="text-lg font-bold text-slate-900">
            Self-Serve Hybrid B2B+B2C Factory Onboarding Wizard
          </h1>
          <div className="mt-3 grid grid-cols-5 gap-2 text-xs">
            {[
              "1. Factory Preset",
              "2. WA & Email Bridge",
              "3. Storefronts",
              "4. WebChat Embed",
              "5. AI Sandbox",
            ].map((label, idx) => {
              const num = (idx + 1) as 1 | 2 | 3 | 4 | 5;
              return (
                <button
                  key={label}
                  type="button"
                  onClick={() => setStep(num)}
                  className={`rounded-md border px-2 py-2 font-semibold ${
                    step === num
                      ? "border-indigo-600 bg-indigo-50 text-indigo-700"
                      : "border-slate-200 text-slate-600"
                  }`}
                >
                  {label}
                </button>
              );
            })}
          </div>
          {copiedNotice && (
            <div className="mt-2 text-xs font-medium text-emerald-700">{copiedNotice}</div>
          )}
        </div>

        {step === 1 && (
          <div className="rounded-lg border border-slate-200 bg-white p-5 text-xs space-y-4">
            <h2 className="text-sm font-bold text-slate-900">
              Step 1: Business Preset, Base Currency & Timezone
            </h2>
            <div className="grid gap-3 md:grid-cols-2">
              <div>
                <label className="block font-semibold text-slate-700">Business Preset</label>
                <select
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
                  className="mt-1 w-full rounded border border-slate-300 px-2.5 py-2"
                >
                  <option value="hybrid_factory_b2b_b2c">
                    Hybrid B2B+B2C Factory (Quanzhou Smart LED Lighting Preset)
                  </option>
                  <option value="dtc_brand">DTC Independent Store Brand</option>
                  <option value="pure_b2b_inquiry">Pure B2B Foreign Trade Factory</option>
                </select>
              </div>
              <div>
                <label className="block font-semibold text-slate-700">Factory / Legal Name</label>
                <input
                  type="text"
                  value={companyName}
                  onChange={(e) => setCompanyName(e.target.value)}
                  className="mt-1 w-full rounded border border-slate-300 px-2.5 py-2"
                />
              </div>
              <div>
                <label className="block font-semibold text-slate-700">Base Currency</label>
                <select
                  value={baseCurrency}
                  onChange={(e) => setBaseCurrency(e.target.value)}
                  className="mt-1 w-full rounded border border-slate-300 px-2.5 py-2"
                >
                  <option value="CNY">CNY</option>
                  <option value="USD">USD</option>
                </select>
              </div>
              <div>
                <label className="block font-semibold text-slate-700">IANA Timezone</label>
                <input
                  type="text"
                  value={ianaTimezone}
                  onChange={(e) => setIanaTimezone(e.target.value)}
                  className="mt-1 w-full rounded border border-slate-300 px-2.5 py-2"
                />
              </div>
            </div>
            <button
              type="button"
              data-testid="onboarding-save-step1"
              onClick={() => saveWorkspaceMutation.mutate()}
              className="rounded-md bg-indigo-600 px-4 py-2 font-semibold text-white hover:bg-indigo-700"
            >
              Save Preset & Continue
            </button>
          </div>
        )}

        {step === 2 && (
          <div className="rounded-lg border border-slate-200 bg-white p-5 text-xs space-y-5">
            <div>
              <h2 className="text-sm font-bold text-slate-900">
                Step 2A: Meta Embedded Signup v4 & WhatsApp Coexistence
              </h2>
              <label className="mt-2 inline-flex cursor-pointer items-center gap-2 font-medium">
                <input
                  type="checkbox"
                  data-testid="coexistence-mode-toggle"
                  checked={isCoexistence}
                  onChange={(e) => setIsCoexistence(e.target.checked)}
                />
                <span>
                  Enable WhatsApp Business App Coexistence (keep mobile app + sync 180-day 1:1 chats)
                </span>
              </label>

              {isCoexistence && (
                <div className="mt-2 rounded-md border border-amber-300 bg-amber-50 p-3 space-y-2 text-amber-950">
                  <div className="flex items-center gap-1.5 font-bold">
                    <ShieldAlert className="h-4 w-4 text-amber-600" />
                    <span>Coexistence 20 mps & Platform Constraints Checklist</span>
                  </div>
                  <ul className="list-disc pl-5 space-y-1">
                    <li>Cloud API throughput is capped at 20 mps (instead of 80 mps).</li>
                    <li>180-day 1:1 chats sync within 24h; group chats do not sync.</li>
                    <li>Disappearing messages and creating new broadcast lists are disabled on mobile.</li>
                  </ul>
                  <label className="flex items-center gap-2 font-semibold">
                    <input
                      type="checkbox"
                      data-testid="coexistence-20mps-checkbox"
                      checked={coexistenceAck}
                      onChange={(e) => setCoexistenceAck(e.target.checked)}
                    />
                    <span>I acknowledge the 20 mps Coexistence limit and mobile constraints</span>
                  </label>
                </div>
              )}

              <button
                type="button"
                data-testid="meta-embedded-signup-btn"
                disabled={isCoexistence && !coexistenceAck}
                onClick={() => metaSignupMutation.mutate()}
                className="mt-3 rounded-md bg-emerald-600 px-3.5 py-2 font-semibold text-white hover:bg-emerald-700 disabled:opacity-45"
              >
                Launch Meta Embedded Signup v4
              </button>
            </div>

            <div className="border-t border-slate-200 pt-4 space-y-2">
              <h2 className="text-sm font-bold text-slate-900">
                Step 2B: Zero-CASA Email Bridge, Made-in-China Forwarding & SPF/DKIM DNS
              </h2>
              <div className="flex items-center gap-2">
                <code className="rounded bg-slate-100 px-2.5 py-1.5 text-slate-800">
                  {forwardingAddr}
                </code>
                <button
                  type="button"
                  data-testid="copy-email-forwarding-btn"
                  onClick={() => copyText(forwardingAddr, "inbound forwarding address")}
                  className="inline-flex items-center gap-1 rounded border border-slate-300 px-2.5 py-1.5 font-medium hover:bg-slate-100"
                >
                  <Copy className="h-3.5 w-3.5" />
                  <span>Copy Address</span>
                </button>
              </div>
              <div className="flex items-center gap-2">
                <input
                  type="text"
                  value={emailDomain}
                  onChange={(e) => setEmailDomain(e.target.value)}
                  className="w-64 rounded border border-slate-300 px-2.5 py-1.5"
                />
                <button
                  type="button"
                  data-testid="verify-email-dns-btn"
                  onClick={() => verifyDnsMutation.mutate()}
                  className="rounded bg-slate-900 px-3 py-1.5 font-semibold text-white"
                >
                  Verify SPF & 2048-bit DKIM
                </button>
                {dnsVerified && (
                  <span
                    data-testid="dns-status-verified"
                    className="inline-flex items-center gap-1 text-emerald-700 font-semibold"
                  >
                    <CheckCircle2 className="h-4 w-4" />
                    <span>SPF + DKIM Verified</span>
                  </span>
                )}
              </div>
            </div>

            <div className="flex justify-end">
              <button
                type="button"
                data-testid="onboarding-next-btn"
                onClick={() => setStep(3)}
                className="rounded-md bg-indigo-600 px-4 py-2 font-semibold text-white"
              >
                Next: Storefront Connectors
              </button>
            </div>
          </div>
        )}

        {step === 3 && (
          <div className="rounded-lg border border-slate-200 bg-white p-5 text-xs space-y-4">
            <h2 className="text-sm font-bold text-slate-900">
              Step 3: P0 Storefronts (OAuth & Compliant BYOK Tabs)
            </h2>
            <div className="flex flex-wrap gap-1.5">
              {(["Shopify", "WooCommerce", "SHOPLINE", "Shoplazza", "TikTok Shop"] as StorefrontTab[]).map(
                (tab) => (
                  <button
                    key={tab}
                    type="button"
                    onClick={() => setStorefrontTab(tab)}
                    className={`rounded-md border px-3 py-1.5 font-semibold ${
                      storefrontTab === tab
                        ? "border-indigo-600 bg-indigo-600 text-white"
                        : "border-slate-200 bg-slate-50 text-slate-700"
                    }`}
                  >
                    {tab}
                  </button>
                )
              )}
            </div>

            <div className="rounded-md border border-slate-200 bg-slate-50 p-3 space-y-3">
              <div>
                <label className="block font-semibold text-slate-700">
                  {storefrontTab} Store Domain / Handle
                </label>
                <input
                  type="text"
                  value={shopDomain}
                  onChange={(e) => setShopDomain(e.target.value)}
                  className="mt-1 w-full rounded border border-slate-300 bg-white px-2.5 py-1.5"
                />
              </div>

              {storefrontTab === "TikTok Shop" && (
                <div>
                  <label className="block font-semibold text-slate-700">Authorization Mode</label>
                  <select
                    value={tiktokMode}
                    onChange={(e) => setTiktokMode(e.target.value as typeof tiktokMode)}
                    className="mt-1 w-full rounded border border-slate-300 bg-white px-2.5 py-1.5"
                  >
                    <option value="custom_app_25">Custom App (&lt;25 Stores Core Fulfillment)</option>
                    <option value="seller_byok">TikTok Shop Seller BYOK (In-house CS API)</option>
                    <option value="email_bridge">Seller Inbox Email-to-Ticket Bridge</option>
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
                    placeholder="Consumer Key / Private App Key (optional for OAuth)"
                    className="rounded border border-slate-300 bg-white px-2.5 py-1.5"
                  />
                  <input
                    type="password"
                    value={byokSecret}
                    onChange={(e) => setByokSecret(e.target.value)}
                    placeholder="Consumer Secret / Access Token"
                    className="rounded border border-slate-300 bg-white px-2.5 py-1.5"
                  />
                </div>
              )}

              <button
                type="button"
                onClick={() => connectStorefrontMutation.mutate()}
                className="rounded bg-slate-900 px-3 py-1.5 font-semibold text-white"
              >
                Connect {storefrontTab}
              </button>
            </div>

            <div className="flex justify-end">
              <button
                type="button"
                data-testid="onboarding-next-btn"
                onClick={() => setStep(4)}
                className="rounded-md bg-indigo-600 px-4 py-2 font-semibold text-white"
              >
                Next: WebChat Embed
              </button>
            </div>
          </div>
        )}

        {step === 4 && (
          <div className="rounded-lg border border-slate-200 bg-white p-5 text-xs space-y-4">
            <h2 className="text-sm font-bold text-slate-900">
              Step 4: Independent Storefront Web LiveChat &lt;script&gt; Snippet
            </h2>
            <div>
              <label className="block font-semibold text-slate-700">Allowed Origins (comma-separated)</label>
              <input
                type="text"
                value={allowedOriginsText}
                onChange={(e) => setAllowedOriginsText(e.target.value)}
                className="mt-1 w-full rounded border border-slate-300 px-2.5 py-1.5"
              />
            </div>

            <div className="rounded bg-slate-900 p-3 font-mono text-[11px] text-emerald-300">
              {snippetCode}
            </div>

            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                data-testid="copy-webchat-snippet-btn"
                onClick={() => copyText(snippetCode, "WebChat snippet")}
                className="inline-flex items-center gap-1 rounded border border-slate-300 px-3 py-1.5 font-semibold"
              >
                <Copy className="h-3.5 w-3.5" />
                <span>Copy &lt;script&gt; Snippet</span>
              </button>
              <button
                type="button"
                onClick={() => saveWebchatMutation.mutate()}
                className="rounded bg-slate-900 px-3 py-1.5 font-semibold text-white"
              >
                Save Allowed Origins
              </button>
            </div>

            <div className="flex justify-end">
              <button
                type="button"
                data-testid="onboarding-next-btn"
                onClick={() => setStep(5)}
                className="rounded-md bg-indigo-600 px-4 py-2 font-semibold text-white"
              >
                Next: 3-Message AI Sandbox
              </button>
            </div>
          </div>
        )}

        {step === 5 && (
          <div className="rounded-lg border border-slate-200 bg-white p-5 text-xs space-y-4">
            <h2 className="text-sm font-bold text-slate-900">
              Step 5: 3-Message AI Sandbox Verifier (Gate to LiveProduction)
            </h2>

            <div className="space-y-2.5">
              {SANDBOX_SCENARIOS.map((sc, idx) => {
                const reply = sandboxPassed[sc.id];
                return (
                  <div
                    key={sc.id}
                    className="rounded-md border border-slate-200 p-3 space-y-1.5"
                  >
                    <div className="flex items-center justify-between">
                      <span className="font-bold text-slate-900">{sc.label}</span>
                      <button
                        type="button"
                        data-testid={`run-sandbox-test-${idx + 1}`}
                        onClick={() =>
                          sandboxTestMutation.mutate({ scenarioId: sc.id, message: sc.message })
                        }
                        className="inline-flex items-center gap-1 rounded bg-indigo-600 px-2.5 py-1 font-semibold text-white"
                      >
                        <Sparkles className="h-3 w-3" />
                        <span>Run Test</span>
                      </button>
                    </div>
                    <div className="text-slate-500">Prompt: &ldquo;{sc.message}&rdquo;</div>
                    {reply && (
                      <div className="rounded bg-emerald-50 p-2 font-medium text-emerald-900">
                        PASS: {reply}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>

            <div className="flex justify-end border-t border-slate-200 pt-3">
              <button
                type="button"
                data-testid="activate-live-production-btn"
                disabled={!all3SandboxPassed || completeMutation.isPending}
                onClick={() => completeMutation.mutate()}
                className="inline-flex items-center gap-1.5 rounded-md bg-emerald-600 px-4 py-2 font-bold text-white hover:bg-emerald-700 disabled:opacity-45"
              >
                <Rocket className="h-4 w-4" />
                <span>Activate Live Production</span>
              </button>
            </div>
          </div>
        )}
      </div>
    </AppShell>
  );
}
