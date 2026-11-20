import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nProvider } from "@/lib/i18n";
import { PiTemplateCard } from "@/components/channels/pi-template-card";
import type { PiTemplateSettings } from "@/lib/api-client/live-client";

const TEMPLATE: PiTemplateSettings = {
  company_legal_name: "Quanzhou Smart LED Lighting Co., Ltd.",
  company_address: "No. 88 Photoelectric Industrial Park, Quanzhou, Fujian, China",
  contact_phone: "+86 595 2688 0000",
  contact_email: "sales@301go.net",
  website: "https://www.301go.net",
  tax_or_reg_no: "91350583MA2XQZLED8",
  bank_beneficiary_name: "Quanzhou Smart LED Lighting Co., Ltd.",
  bank_name: "Bank of China, Quanzhou Branch",
  bank_account_no: "4188 0000 0000 1234",
  bank_swift_code: "BKCHCNBJ73A",
  bank_address: "No. 188 Fengze Street, Quanzhou, Fujian, China",
  default_terms_notes: "1. 30% T/T deposit, 70% before shipment.",
  footer_signature_title: "Export Sales Manager",
};

vi.mock("@/lib/data-source", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/data-source")>();
  return {
    ...actual,
    fetchPiTemplateSettings: vi.fn(async () => ({ ...TEMPLATE })),
    updatePiTemplateSettings: vi.fn(async (payload: PiTemplateSettings) => ({ ...payload })),
  };
});

import { fetchPiTemplateSettings, updatePiTemplateSettings } from "@/lib/data-source";

function wrap(ui: React.ReactNode, locale: "zh" | "en" = "en") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <I18nProvider defaultLocale={locale}>{ui}</I18nProvider>
    </QueryClientProvider>
  );
}

describe("Track 3 · Configurable PI template & letterhead", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("loads the tenant letterhead, bank wire details and default terms", async () => {
    wrap(<PiTemplateCard enabled />);

    await waitFor(() => {
      expect(screen.getByTestId("pi-template-company_legal_name")).toHaveValue(
        TEMPLATE.company_legal_name
      );
    });
    expect(screen.getByTestId("pi-template-company_address")).toHaveValue(
      TEMPLATE.company_address
    );
    expect(screen.getByTestId("pi-template-tax_or_reg_no")).toHaveValue(
      TEMPLATE.tax_or_reg_no
    );
    expect(screen.getByTestId("pi-template-bank_beneficiary_name")).toHaveValue(
      TEMPLATE.bank_beneficiary_name
    );
    expect(screen.getByTestId("pi-template-bank_swift_code")).toHaveValue("BKCHCNBJ73A");
    expect(screen.getByTestId("pi-template-default_terms_notes")).toHaveValue(
      TEMPLATE.default_terms_notes
    );
    expect(screen.getByTestId("pi-template-footer_signature_title")).toHaveValue(
      "Export Sales Manager"
    );
    expect(fetchPiTemplateSettings).toHaveBeenCalledTimes(1);
  });

  it("saves edited letterhead values through updatePiTemplateSettings", async () => {
    wrap(<PiTemplateCard enabled />);

    const legalName = await screen.findByTestId("pi-template-company_legal_name");
    await waitFor(() => expect(legalName).toHaveValue(TEMPLATE.company_legal_name));

    fireEvent.change(legalName, { target: { value: "Quanzhou Smart LED (Xiamen) Co., Ltd." } });
    fireEvent.change(screen.getByTestId("pi-template-bank_account_no"), {
      target: { value: "6222 0000 0000 9876" },
    });
    fireEvent.change(screen.getByTestId("pi-template-default_terms_notes"), {
      target: { value: "50% T/T deposit, 50% before shipment." },
    });

    fireEvent.click(screen.getByTestId("pi-template-save-btn"));

    await waitFor(() => {
      expect(updatePiTemplateSettings).toHaveBeenCalledWith(
        expect.objectContaining({
          company_legal_name: "Quanzhou Smart LED (Xiamen) Co., Ltd.",
          bank_account_no: "6222 0000 0000 9876",
          default_terms_notes: "50% T/T deposit, 50% before shipment.",
          bank_swift_code: "BKCHCNBJ73A",
        })
      );
    });

    const savedBadge = await screen.findByTestId("pi-template-saved-badge");
    expect(savedBadge.textContent).toContain("Saved");
  });

  it("blocks saving while a required field is missing and never fetches when disabled", async () => {
    wrap(<PiTemplateCard enabled={false} />);

    // `enabled` false keeps `agent` from ever firing an admin request.
    expect(fetchPiTemplateSettings).not.toHaveBeenCalled();
    expect(screen.getByTestId("pi-template-save-btn")).toBeDisabled();

    fireEvent.change(screen.getByTestId("pi-template-company_legal_name"), {
      target: { value: "Quanzhou Smart LED Lighting Co., Ltd." },
    });
    expect(screen.getByTestId("pi-template-save-btn")).toBeDisabled();
  });
});
