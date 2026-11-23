import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useSearchParams } from "next/navigation";
import { I18nProvider } from "@/lib/i18n";
import {
  SendPiEmailModal,
  defaultPiEmailBody,
  defaultPiEmailSubject,
} from "@/components/b2b/send-pi-email-modal";
import type { QuoteRecord } from "@/components/b2b/quote-pi-builder";
import { AuthSessionContext } from "@/components/providers";
import B2bQuotesPage from "@/app/b2b/quotes/page";
import type { Principal } from "@/lib/contracts";
import {
  fetchCallBody,
  fetchCallMethod,
  normalizeFetchInput,
} from "@/__tests__/fetch-stub";

const QUOTE: QuoteRecord = {
  id: "q-1",
  opportunity_id: "opp-1",
  quote_no: "PI-202611-0042",
  version: 1,
  is_proforma_invoice: true,
  incoterm: "FOB",
  port_of_loading: "Shenzhen",
  port_of_destination: "Hamburg",
  payment_terms: "30% T/T deposit, 70% before shipment",
  base_currency: "CNY",
  quote_currency: "USD",
  fx_rate_snapshot: "7.120000",
  fx_locked_until: new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString(),
  freight_fee: "250.00",
  insurance_fee: "0.00",
  customs_duty_estimate: "0.00",
  total_amount: "7350.00",
  pdf_r2_key: "tenants/t1/quotes/PI-202611-0042-v1.pdf",
  download_url: null,
  status: "sent",
  items: [
    {
      id: "qi-1",
      variant_id: "var-1",
      sku_snapshot: "LED-COB-24V-5M",
      quantity: 500,
      moq_tier_applied: 500,
      unit_price_quote_ccy: "14.2000",
      line_total_quote_ccy: "7100.00",
    },
  ],
};

const EMAIL_ACCOUNTS = {
  items: [
    {
      id: "ch-email-1",
      channel_type: "email",
      display_name: "sales@301go.net (Resend)",
      external_account_id: "sales@301go.net",
      status: "active",
      rate_limit_profile: { mps: 10 },
      owner_user_id: null,
      owner_name: null,
      owner_email: null,
      visibility_scope: "shared",
      bot_username: null,
    },
    {
      id: "ch-email-2",
      channel_type: "email",
      display_name: "个人邮箱",
      external_account_id: "li@301go.net",
      status: "active",
      rate_limit_profile: { mps: 5 },
      owner_user_id: null,
      owner_name: null,
      owner_email: null,
      visibility_scope: "personal",
      bot_username: null,
    },
    {
      id: "ch-telegram-1",
      channel_type: "telegram",
      display_name: "QZ LED 外贸机器人",
      external_account_id: "7012345678",
      status: "active",
      rate_limit_profile: { mps: 30 },
      owner_user_id: null,
      owner_name: null,
      owner_email: null,
      visibility_scope: "shared",
      bot_username: "qzled_sales_bot",
    },
  ],
  total: 3,
  limit: 20,
  offset: 0,
  has_more: false,
};

vi.mock("@/lib/data-source", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/data-source")>();
  return {
    ...actual,
    fetchChannelAccounts: vi.fn(async () => EMAIL_ACCOUNTS),
    fetchPiTemplateSettings: vi.fn(async () => ({
      company_legal_name: "Quanzhou Smart LED Optoelectronics Co., Ltd.",
      company_address: "",
      contact_phone: "",
      contact_email: "sales@301go.net",
      website: "",
      tax_id: "",
      bank_beneficiary: "",
      bank_name: "",
      bank_account_no: "",
      bank_swift_code: "",
      bank_address: "",
      default_payment_terms: "",
      default_incoterm: "FOB",
      default_validity_days: 14,
      footer_note: "",
    })),
    sendQuotePiEmail: vi.fn(async () => ({
      quote_id: "q-1",
      quote_no: "PI-202611-0042",
      recipient_email: "purchasing@berlin-lighting.de",
      from_email: "sales@301go.net",
      provider: "resend" as const,
      external_message_id: "re-123",
      pdf_filename: "PI-202611-0042-ZH.pdf",
      pdf_size_bytes: 184_320,
      quote_status: "sent",
    })),
  };
});

import { fetchChannelAccounts, sendQuotePiEmail } from "@/lib/data-source";

function wrap(ui: React.ReactNode, locale: "zh" | "en" = "en") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <I18nProvider defaultLocale={locale}>{ui}</I18nProvider>
    </QueryClientProvider>
  );
}

describe("Track 1 · PI PDF preview & one-click PI email", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("prefills the subject/body from the quote and localises the cover note", () => {
    expect(defaultPiEmailSubject(QUOTE)).toBe(
      "Proforma Invoice PI-202611-0042 (FOB · USD 7350.00)"
    );
    expect(defaultPiEmailBody(QUOTE, "en")).toContain("Dear Purchasing Team");
    expect(defaultPiEmailBody(QUOTE, "en")).toContain("PI-202611-0042");
    expect(defaultPiEmailBody(QUOTE, "zh")).toContain("尊敬的客户");
    expect(defaultPiEmailBody(QUOTE, "zh")).toContain("PI-202611-0042");
  });

  it("lists only active email accounts, switches PDF language and sends the PI email", async () => {
    wrap(<SendPiEmailModal quote={QUOTE} onClose={vi.fn()} />);

    expect(screen.getByTestId("send-pi-email-modal").getAttribute("role")).toBe("dialog");
    expect(screen.getByTestId("send-pi-email-subject")).toHaveValue(
      "Proforma Invoice PI-202611-0042 (FOB · USD 7350.00)"
    );

    // 1 auto option + 2 active email accounts (telegram is filtered out).
    const accountSelect = screen.getByTestId("send-pi-email-account");
    await waitFor(() => {
      expect(accountSelect.querySelectorAll("option")).toHaveLength(3);
    });
    const optionLabels = Array.from(accountSelect.querySelectorAll("option")).map(
      (option) => option.textContent
    );
    expect(optionLabels[0]).toContain("Auto-select");
    expect(optionLabels[1]).toContain("sales@301go.net (Resend)");
    expect(optionLabels[2]).toContain("个人邮箱 (SMTP)");
    expect(optionLabels.join(" ")).not.toContain("QZ LED 外贸机器人");

    fireEvent.change(screen.getByTestId("send-pi-email-recipient"), {
      target: { value: "purchasing@berlin-lighting.de" },
    });
    fireEvent.change(accountSelect, { target: { value: "ch-email-1" } });
    fireEvent.click(screen.getByTestId("send-pi-email-language-zh"));
    fireEvent.click(screen.getByTestId("send-pi-email-submit"));

    await waitFor(() => {
      expect(sendQuotePiEmail).toHaveBeenCalledWith(
        "q-1",
        expect.objectContaining({
          recipient_email: "purchasing@berlin-lighting.de",
          channel_account_id: "ch-email-1",
          language: "zh",
          subject: "Proforma Invoice PI-202611-0042 (FOB · USD 7350.00)",
          message_body: expect.stringContaining("尊敬的客户"),
        })
      );
    });

    const receipt = await screen.findByTestId("send-pi-email-success");
    expect(receipt.textContent).toContain("sales@301go.net");
    expect(receipt.textContent).toContain("purchasing@berlin-lighting.de");
    expect(receipt.textContent).toContain("Resend");
    expect(receipt.textContent).toContain("PI-202611-0042-ZH.pdf");
    expect(receipt.textContent).toContain("180.0 KB");
  });

  it("keeps the send button disabled until a valid recipient is typed", async () => {
    wrap(<SendPiEmailModal quote={QUOTE} onClose={vi.fn()} />);

    const submit = screen.getByTestId("send-pi-email-submit");
    expect(submit).toBeDisabled();

    fireEvent.change(screen.getByTestId("send-pi-email-recipient"), {
      target: { value: "not-an-email" },
    });
    expect(submit).toBeDisabled();

    fireEvent.change(screen.getByTestId("send-pi-email-recipient"), {
      target: { value: "buyer@example.de" },
    });
    expect(submit).not.toBeDisabled();
    expect(fetchChannelAccounts).toHaveBeenCalled();
  });

  it("POSTs the locked send-email contract through the BFF proxy", async () => {
    const actual = await vi.importActual<typeof import("@/lib/data-source")>(
      "@/lib/data-source"
    );
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const { url } = normalizeFetchInput(input, init);
      expect(url).toContain("/api/bff/v1/b2b/quotes/q-1/send-email");
      return Response.json({
        quote_id: "q-1",
        quote_no: "PI-202611-0042",
        recipient_email: "buyer@example.de",
        from_email: "sales@301go.net",
        provider: "smtp_imap",
        external_message_id: null,
        pdf_filename: "PI-202611-0042-EN.pdf",
        pdf_size_bytes: 1024,
        quote_status: "sent",
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    const receipt = await actual.sendQuotePiEmail("q-1", {
      recipient_email: "buyer@example.de",
      language: "en",
    });

    expect(receipt.provider).toBe("smtp_imap");
    const call = fetchMock.mock.calls[0];
    expect(fetchCallMethod(call as unknown[])).toBe("POST");
    expect(JSON.parse(await fetchCallBody(call as unknown[]))).toEqual({
      recipient_email: "buyer@example.de",
      channel_account_id: null,
      subject: null,
      message_body: null,
      language: "en",
    });
    vi.unstubAllGlobals();
  });

  it("renders the direct PDF preview link and opens the send modal from the quote builder", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const { url, method } = normalizeFetchInput(input, init);
      if (url.includes("/ai/proposals")) {
        return Response.json({ items: [], total: 0, limit: 1, offset: 0, has_more: false });
      }
      if (url.includes("/catalog/variants")) {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0, has_more: false });
      }
      if (url.includes("/b2b/opportunities")) {
        return Response.json({ items: [], total: 0, limit: 50, offset: 0, has_more: false });
      }
      if (url.includes("/b2b/quotes") && (!method || method === "GET")) {
        return Response.json({
          items: [
            {
              id: "q-1",
              opportunity_id: "opp-1",
              quote_no: "PI-202611-0042",
              version: 1,
              is_proforma_invoice: true,
              incoterm: "FOB",
              port_of_loading: "Shenzhen",
              port_of_destination: "Hamburg",
              payment_terms: "30% T/T deposit",
              base_currency: "CNY",
              quote_currency: "USD",
              fx_rate_snapshot: "7.120000",
              fx_locked_until: new Date(Date.now() + 86_400_000).toISOString(),
              freight_fee: "0.00",
              insurance_fee: "0.00",
              customs_duty_estimate: "0.00",
              total_amount: "7350.00",
              pdf_r2_key: null,
              download_url: null,
              status: "sent",
              items: [],
            },
          ],
          total: 1,
          limit: 20,
          offset: 0,
          has_more: false,
        });
      }
      return Response.json({});
    });
    vi.stubGlobal("fetch", fetchMock);

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <AuthSessionContext.Provider
          value={{
            principal: {
              user_id: "u1",
              tenant_id: "t1",
              role: "owner" as Principal["role"],
              email: "owner@qzled.example.com",
            },
            isLoading: false,
            logout: async () => {},
            refreshSession: async () => {},
          }}
        >
          <I18nProvider defaultLocale="en">
            <B2bQuotesPage />
          </I18nProvider>
        </AuthSessionContext.Provider>
      </QueryClientProvider>
    );

    const preview = await screen.findByTestId("preview-pi-pdf-q-1");
    expect(preview.getAttribute("href")).toBe(
      "/api/bff/v1/b2b/quotes/q-1/pdf/download?language=en"
    );
    expect(preview.getAttribute("target")).toBe("_blank");
    expect(preview.getAttribute("rel")).toBe("noopener noreferrer");

    fireEvent.click(screen.getByTestId("open-send-pi-email-q-1"));
    expect(await screen.findByTestId("send-pi-email-modal")).toBeTruthy();
    vi.unstubAllGlobals();
  });

  it("shows the buyer context banner when arriving from the Inbox drawer", async () => {
    vi.mocked(useSearchParams).mockReturnValue(
      new URLSearchParams(
        "customer_id=cust-target&customer_name=Hans%20Weber"
      ) as unknown as ReturnType<typeof useSearchParams>
    );
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const { url } = normalizeFetchInput(input, init);
      if (url.includes("/ai/proposals")) {
        return Response.json({ items: [], total: 0, limit: 1, offset: 0, has_more: false });
      }
      if (url.includes("/catalog/variants")) {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0, has_more: false });
      }
      if (url.includes("/b2b/opportunities")) {
        return Response.json({ items: [], total: 0, limit: 50, offset: 0, has_more: false });
      }
      if (url.includes("/b2b/quotes")) {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0, has_more: false });
      }
      return Response.json({});
    });
    vi.stubGlobal("fetch", fetchMock);

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <AuthSessionContext.Provider
          value={{
            principal: {
              user_id: "u1",
              tenant_id: "t1",
              role: "owner" as Principal["role"],
              email: "owner@qzled.example.com",
            },
            isLoading: false,
            logout: async () => {},
            refreshSession: async () => {},
          }}
        >
          <I18nProvider defaultLocale="en">
            <B2bQuotesPage />
          </I18nProvider>
        </AuthSessionContext.Provider>
      </QueryClientProvider>
    );

    const banner = await screen.findByTestId("quote-customer-context-banner");
    expect(banner.textContent).toContain("Creating a B2B quote for Hans Weber");
    expect(banner.textContent).toContain("cust-target");

    vi.mocked(useSearchParams).mockReturnValue(
      new URLSearchParams() as unknown as ReturnType<typeof useSearchParams>
    );
    vi.unstubAllGlobals();
  });

  it("auto-opens the send drawer from the Inbox ?quoteId&send_pi_email deep link", async () => {
    vi.mocked(useSearchParams).mockReturnValue(
      new URLSearchParams("quoteId=q-1&send_pi_email=1") as unknown as ReturnType<
        typeof useSearchParams
      >
    );
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const { url, method } = normalizeFetchInput(input, init);
      if (url.includes("/ai/proposals")) {
        return Response.json({ items: [], total: 0, limit: 1, offset: 0, has_more: false });
      }
      if (url.includes("/catalog/variants")) {
        return Response.json({ items: [], total: 0, limit: 20, offset: 0, has_more: false });
      }
      if (url.includes("/b2b/opportunities")) {
        return Response.json({ items: [], total: 0, limit: 50, offset: 0, has_more: false });
      }
      if (url.includes("/b2b/quotes") && (!method || method === "GET")) {
        return Response.json({
          items: [
            {
              id: "q-1",
              opportunity_id: "opp-1",
              quote_no: "PI-202611-0042",
              version: 1,
              is_proforma_invoice: true,
              incoterm: "FOB",
              port_of_loading: "Shenzhen",
              port_of_destination: "Hamburg",
              payment_terms: "30% T/T deposit",
              base_currency: "CNY",
              quote_currency: "USD",
              fx_rate_snapshot: "7.120000",
              fx_locked_until: new Date(Date.now() + 86_400_000).toISOString(),
              freight_fee: "0.00",
              insurance_fee: "0.00",
              customs_duty_estimate: "0.00",
              total_amount: "7350.00",
              pdf_r2_key: null,
              download_url: null,
              status: "sent",
              items: [],
            },
          ],
          total: 1,
          limit: 20,
          offset: 0,
          has_more: false,
        });
      }
      return Response.json({});
    });
    vi.stubGlobal("fetch", fetchMock);

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <AuthSessionContext.Provider
          value={{
            principal: {
              user_id: "u1",
              tenant_id: "t1",
              role: "owner" as Principal["role"],
              email: "owner@qzled.example.com",
            },
            isLoading: false,
            logout: async () => {},
            refreshSession: async () => {},
          }}
        >
          <I18nProvider defaultLocale="en">
            <B2bQuotesPage />
          </I18nProvider>
        </AuthSessionContext.Provider>
      </QueryClientProvider>
    );

    expect(await screen.findByTestId("send-pi-email-modal")).toBeTruthy();
    expect(screen.getByTestId("send-pi-email-subject")).toHaveValue(
      "Proforma Invoice PI-202611-0042 (FOB · USD 7350.00)"
    );

    vi.mocked(useSearchParams).mockReturnValue(
      new URLSearchParams() as unknown as ReturnType<typeof useSearchParams>
    );
    vi.unstubAllGlobals();
  });

  it("appends company sign-off to cover note and localizes receipt quote status in Chinese", async () => {
    expect(defaultPiEmailBody(QUOTE, "en")).toContain("Best regards,\nExport Sales Team");
    expect(defaultPiEmailBody(QUOTE, "zh")).toContain("顺祝商祺，\n外贸销售团队");
    expect(
      defaultPiEmailBody(QUOTE, "en", "Quanzhou Smart LED Optoelectronics Co., Ltd.")
    ).toContain("Best regards,\nQuanzhou Smart LED Optoelectronics Co., Ltd.");

    vi.mocked(sendQuotePiEmail).mockResolvedValueOnce({
      quote_id: "q-1",
      quote_no: "PI-202611-0042",
      recipient_email: "buyer@example.com",
      from_email: "sales@301go.net",
      provider: "resend",
      external_message_id: "re-999",
      pdf_filename: "PI-202611-0042-v1.pdf",
      pdf_size_bytes: 4096,
      quote_status: "pending_approval",
    });

    wrap(<SendPiEmailModal quote={QUOTE} onClose={vi.fn()} />, "zh");

    await waitFor(() => {
      expect(
        (screen.getByTestId("send-pi-email-body") as HTMLTextAreaElement).value
      ).toContain("Quanzhou Smart LED Optoelectronics Co., Ltd.");
    });

    fireEvent.change(screen.getByTestId("send-pi-email-recipient"), {
      target: { value: "buyer@example.com" },
    });
    fireEvent.click(screen.getByTestId("send-pi-email-submit"));

    const receipt = await screen.findByTestId("send-pi-email-success");
    expect(receipt.textContent).toContain("待审批");
    expect(receipt.textContent).not.toContain("pending_approval");
  });
});
