import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nProvider } from "@/lib/i18n";
import { AuthSessionContext } from "@/components/providers";
import OnboardingPage from "@/app/onboarding/page";
import ChannelsSettingsPage from "@/app/settings/channels/page";
import { fetchCallUrl, normalizeFetchInput } from "@/__tests__/fetch-stub";

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AuthSessionContext.Provider
        value={{
          principal: {
            user_id: "u1",
            tenant_id: "t1",
            role: "owner",
            email: "owner@qzled.example.com",
            tenant_slug: "qz-led-factory",
          },
          isLoading: false,
          logout: async () => {},
          refreshSession: async () => {},
        }}
      >
        <I18nProvider defaultLocale="en">{ui}</I18nProvider>
      </AuthSessionContext.Provider>
    </QueryClientProvider>
  );
}

describe("Task 5.7 Onboarding Wizard & Channel Settings", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    Object.assign(navigator, {
      clipboard: { writeText: vi.fn(async () => {}) },
    });
  });

  it("pre-selects Hybrid Factory preset, enforces Coexistence 20 mps ack, and passes 3-message AI sandbox", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const { url, method } = normalizeFetchInput(input, init);
      if (url.includes("/ai/proposals")) {
        return new Response(JSON.stringify({ items: [], total: 0, limit: 1, offset: 0, has_more: false }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/onboarding/state")) {
        return new Response(
          JSON.stringify({
            tenant_id: "t1",
            tenant_slug: "quanzhou-led-factory",
            current_stage: "WorkspaceCreated",
            preset_type: "hybrid_factory_b2b_b2c",
            base_currency: "CNY",
            iana_timezone: "Asia/Shanghai",
            inbound_forwarding_address: "quanzhou-led-factory@inbound.mail.example.com",
            dns_status: { spf_verified: false, dkim_verified: false, return_path_verified: false },
            site_key: "wc_site_qz_001",
            allowed_origins: ["https://shop.qzled.example.com"],
            coexistence_acknowledged: false,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/onboarding/workspace") && method === "POST") {
        return new Response(JSON.stringify({ stage: "ChannelConnecting" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/onboarding/email/verify-dns") && method === "POST") {
        return new Response(
          JSON.stringify({
            spf_verified: true,
            dkim_verified: true,
            return_path_verified: true,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/onboarding/sandbox-test") && method === "POST") {
        return new Response(
          JSON.stringify({
            passed: true,
            reply_text: "Verified grounded response [doc-1#c1]",
            citations: [{ doc_id: "doc-1", chunk_id: "c1", score: 0.92 }],
            source_tool_call_id: "tool-1",
            latency_ms: 410,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/onboarding/complete") && method === "POST") {
        return new Response(JSON.stringify({ stage: "LiveProduction" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response("{}", { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);

    wrap(<OnboardingPage />);

    // Step 1: Preset pre-selected
    await waitFor(() => {
      const presetSelect = screen.getByTestId("onboarding-preset-select") as HTMLSelectElement;
      expect(presetSelect.value).toBe("hybrid_factory_b2b_b2c");
    });

    fireEvent.click(screen.getByTestId("onboarding-save-step1"));

    // Step 2: Coexistence 20 mps checklist gates Coexistence launch button
    await waitFor(() => {
      expect(screen.getByTestId("coexistence-mode-toggle")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId("coexistence-mode-toggle"));
    const metaLaunchBtn = screen.getByTestId("meta-embedded-signup-btn");
    expect(metaLaunchBtn).toBeDisabled();

    fireEvent.click(screen.getByTestId("coexistence-20mps-checkbox"));
    expect(metaLaunchBtn).not.toBeDisabled();

    // Email forwarding & DNS check
    fireEvent.click(screen.getByTestId("copy-email-forwarding-btn"));
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
      "quanzhou-led-factory@inbound.mail.example.com"
    );
    fireEvent.click(screen.getByTestId("verify-email-dns-btn"));
    await waitFor(() => {
      expect(screen.getByTestId("dns-status-verified")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId("onboarding-next-btn"));

    // Step 3: Storefronts (Shopify, WooCommerce, SHOPLINE, Shoplazza, TikTok Shop)
    for (const tab of ["Shopify", "WooCommerce", "SHOPLINE", "Shoplazza", "TikTok Shop"]) {
      const tabBtn = screen.getByRole("button", { name: tab });
      fireEvent.click(tabBtn);
      expect(tabBtn).toBeInTheDocument();
    }

    fireEvent.click(screen.getByTestId("onboarding-next-btn"));

    // Step 4: WebChat <script> snippet copy
    fireEvent.click(screen.getByTestId("copy-webchat-snippet-btn"));
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
      expect.stringContaining("wc_site_qz_001")
    );

    fireEvent.click(screen.getByTestId("onboarding-next-btn"));

    // Step 5: Run 3-message AI Sandbox verifier
    const goLiveBtn = screen.getByTestId("activate-live-production-btn");
    expect(goLiveBtn).toBeDisabled();

    fireEvent.click(screen.getByTestId("run-sandbox-test-1"));
    fireEvent.click(screen.getByTestId("run-sandbox-test-2"));
    fireEvent.click(screen.getByTestId("run-sandbox-test-3"));

    await waitFor(() => {
      expect(goLiveBtn).not.toBeDisabled();
    });
  });

  it("renders DLQ replay button and consent/suppression viewer in /settings/channels", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const { url, method } = normalizeFetchInput(input, init);
      if (url.includes("/ai/proposals")) {
        return new Response(JSON.stringify({ items: [], total: 0, limit: 1, offset: 0, has_more: false }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/channels/accounts")) {
        return new Response(
          JSON.stringify({
            items: [
              {
                id: "ch-1",
                channel_type: "whatsapp_coexistence",
                display_name: "QZ LED WA Business (+86)",
                external_account_id: "waba_phone_1",
                status: "active",
                rate_limit_profile: { mps: 20, coexistence: true },
              },
            ],
            total: 1,
            limit: 20,
            offset: 0,
            has_more: false,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/channels/dlq/dlq-1/replay") && method === "POST") {
        return new Response(JSON.stringify({ ok: true, event_id: "dlq-1" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/channels/dlq")) {
        return new Response(
          JSON.stringify({
            items: [
              {
                id: "dlq-1",
                channel_type: "whatsapp_coexistence",
                external_event_id: "wamid.fail.1",
                error_message: "131056 Pair rate limit exceeded",
                retry_count: 6,
                created_at: "2026-11-19T08:00:00Z",
              },
            ],
            total: 1,
            limit: 20,
            offset: 0,
            has_more: false,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/channels/consents")) {
        return new Response(
          JSON.stringify({
            items: [
              {
                id: "con-1",
                customer_name: "Elena Vance",
                channel_type: "whatsapp_marketing",
                consent_status: "opted_in",
                proof_source: "shopify_checkout_checkbox",
                recorded_at: "2026-11-19T07:00:00Z",
              },
            ],
            total: 1,
            limit: 20,
            offset: 0,
            has_more: false,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/channels/suppressions")) {
        return new Response(
          JSON.stringify({
            items: [
              {
                id: "sup-1",
                channel_type: "email",
                normalized_address: "unsubscribe-test@example.com",
                reason: "RFC 8058 One-Click Opt-Out",
                created_at: "2026-11-19T06:00:00Z",
              },
            ],
            total: 1,
            limit: 20,
            offset: 0,
            has_more: false,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      return new Response("{}", { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);

    wrap(<ChannelsSettingsPage />);

    await waitFor(() => {
      expect(screen.getByText("QZ LED WA Business (+86)")).toBeInTheDocument();
      expect(screen.getByText(/20 mps/)).toBeInTheDocument();
      expect(screen.getByText(/131056 Pair rate limit exceeded/)).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId("dlq-replay-btn-dlq-1"));
    await waitFor(() => {
      const replayCall = fetchMock.mock.calls.find((c) =>
        fetchCallUrl(c).includes("/channels/dlq/dlq-1/replay"),
      );
      expect(replayCall).toBeDefined();
    });

    expect(screen.getByText("shopify_checkout_checkbox")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("tab-suppression-list"));
    expect(screen.getByText("unsubscribe-test@example.com")).toBeInTheDocument();
  });
});
