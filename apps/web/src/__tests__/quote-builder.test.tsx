import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nProvider } from "@/lib/i18n";
import { AuthSessionContext } from "@/components/providers";
import CatalogPage from "@/app/catalog/page";
import B2bQuotesPage from "@/app/b2b/quotes/page";
import type { Principal } from "@/lib/contracts";
import {
  fetchCallBody,
  fetchCallMethod,
  fetchCallUrl,
  normalizeFetchInput,
} from "@/__tests__/fetch-stub";

// Contract-shaped VariantRead (contracts/openapi.json): spu_code / category /
// hs_code / certifications live in attributes, and MOQ unit prices arrive
// as strings. The live adapter maps this onto the UI shape.
const SAMPLE_VARIANT = {
  id: "var-1",
  product_id: "prod-1",
  sku: "LED-COB-24V-5M",
  attributes: {
    spu_code: "LED-COB-24V",
    category: "LED Strip Lighting",
    hs_code: "94054200",
    certifications: ["CE", "RoHS", "UL"],
    cct: "4000K",
    ip_rating: "IP65",
  },
  cost_price_base: "45.50",
  base_currency: "CNY",
  min_margin_floor_pct: "15.00",
  retail_price_usd: "29.90",
  b2b_moq_tiers: [
    { min_qty: 50, unit_price_usd: "18.5" },
    { min_qty: 500, unit_price_usd: "14.2" },
  ],
  lead_time_days: 7,
  tenant_id: "t1",
  created_at: "2026-11-19T00:00:00Z",
  weight_grams: 120,
  carton_dims_cm: null,
};

function wrapAuth(ui: React.ReactNode, role: Principal["role"]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AuthSessionContext.Provider
        value={{
          principal: {
            user_id: "u1",
            tenant_id: "t1",
            role,
            email: `${role}@qzled.example.com`,
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

describe("Task 5.5 Catalog COGS Masking, SKU Mapping & B2B PI Quote Builder", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("hides cost_price_base from agent in Catalog and SkuMappingDrawer while saving bundle_multiplier", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const { url, method } = normalizeFetchInput(input, init);
      if (url.includes("/ai/proposals")) {
        return new Response(JSON.stringify({ items: [], total: 0, limit: 1, offset: 0, has_more: false }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/catalog/variants/var-1/mappings")) {
        return new Response(
          JSON.stringify({
            variant: SAMPLE_VARIANT,
            mappings: [
              {
                id: "map-1",
                variant_id: "var-1",
                channel_account_id: "ch-shop-1",
                channel_label: "Shopify US Flagship",
                external_product_id: "gid://shopify/Product/991",
                external_sku: "COB-24V-2PACK",
                bundle_multiplier: 2,
                sync_inventory_enabled: true,
              },
            ],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/catalog/variants")) {
        return new Response(
          JSON.stringify({
            items: [SAMPLE_VARIANT],
            total: 1,
            limit: 20,
            offset: 0,
            has_more: false,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/catalog/mappings/map-1") && method === "PUT") {
        return new Response(JSON.stringify({ ok: true }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response("{}", { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);

    wrapAuth(<CatalogPage />, "agent");

    await waitFor(() => {
      expect(screen.getByText("LED-COB-24V-5M")).toBeInTheDocument();
    });

    // Agent must NEVER see cost_price_base (45.50 CNY)
    expect(screen.queryByText(/45\.50/)).toBeNull();
    expect(screen.queryByTestId("catalog-cost-header")).toBeNull();

    fireEvent.click(screen.getByTestId("open-sku-drawer-var-1"));

    await waitFor(() => {
      expect(screen.getByTestId("sku-mapping-drawer")).toBeInTheDocument();
    });
    expect(screen.queryByText(/45\.50/)).toBeNull();

    // The drawer shell renders immediately; the mapping rows populate once
    // the detail query resolves, so wait for the input instead of asserting
    // synchronously (same async-mount race as the Wave 1 copilot badge).
    const multInput = await screen.findByTestId("bundle-multiplier-input-map-1");
    fireEvent.change(multInput, { target: { value: "3" } });
    fireEvent.click(screen.getByTestId("save-mapping-btn-map-1"));

    await waitFor(async () => {
      const putCall = fetchMock.mock.calls.find(
        (c) =>
          fetchCallUrl(c).includes("/catalog/mappings/map-1") &&
          fetchCallMethod(c) === "PUT",
      );
      expect(putCall).toBeDefined();
      expect(JSON.parse(await fetchCallBody(putCall!))).toMatchObject({ bundle_multiplier: 3 });
    });
  });

  it("auto-computes MOQ tier pricing, shows Kanban stages, and converts accepted quote 1-Click", async () => {
    const futureIso = new Date(Date.now() + 1000 * 60 * 60 * 24).toISOString();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const { url, method } = normalizeFetchInput(input, init);
      if (url.includes("/ai/proposals")) {
        return new Response(JSON.stringify({ items: [], total: 0, limit: 1, offset: 0, has_more: false }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/catalog/variants")) {
        return new Response(
          JSON.stringify({ items: [SAMPLE_VARIANT], total: 1, limit: 20, offset: 0, has_more: false }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/b2b/opportunities")) {
        return new Response(
          JSON.stringify({
            items: [
              {
                id: "opp-1",
                title: "Berlin Lighting 500pcs COB Strip",
                company_name: "Berlin Lighting GmbH",
                stage: "quoting",
                expected_amount: "7100.00",
                currency: "USD",
              },
            ],
            total: 1,
            limit: 50,
            offset: 0,
            has_more: false,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.endsWith("/orders/warehouses")) {
        return Response.json({ default_warehouse_id: "wh-test-uuid", items: [{ id: "wh-test-uuid", is_default: true }] });
      }
      if (url.includes("/b2b/quotes/q-1/convert-to-order") && method === "POST") {
        return new Response(
          JSON.stringify({ order: { id: "ord-99", order_no: "ORD-B2B-99" } }),
          {
            status: 201,
            headers: { "Content-Type": "application/json" },
          }
        );
      }
      if (url.includes("/b2b/quotes")) {
        return new Response(
          JSON.stringify({
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
                payment_terms: "30% T/T deposit, 70% before shipment",
                base_currency: "CNY",
                quote_currency: "USD",
                fx_rate_snapshot: "7.120000",
                fx_locked_until: futureIso,
                freight_fee: "250.00",
                insurance_fee: "0.00",
                customs_duty_estimate: "0.00",
                total_amount: "7350.00",
                pdf_r2_key: "tenants/t1/quotes/PI-202611-0042-v1.pdf",
                download_url: "https://minio.local/erp-assets/PI-202611-0042-v1.pdf",
                status: "accepted",
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

    wrapAuth(<B2bQuotesPage />, "manager");

    await waitFor(() => {
      expect(screen.getByText("Berlin Lighting 500pcs COB Strip")).toBeInTheDocument();
      expect(screen.getByText("PI-202611-0042")).toBeInTheDocument();
    });

    for (const stage of ["discovery", "sampling", "quoting", "negotiating", "closed_won"]) {
      expect(screen.getByTestId(`kanban-col-${stage}`)).toBeInTheDocument();
    }

    // Verify MOQ auto-price preview calculator
    const qtyInput = screen.getByTestId("moq-preview-qty-input");
    fireEvent.change(qtyInput, { target: { value: "60" } });
    expect(screen.getByTestId("moq-preview-price")).toHaveTextContent("$18.50");

    fireEvent.change(qtyInput, { target: { value: "500" } });
    expect(screen.getByTestId("moq-preview-price")).toHaveTextContent("$14.20");

    // Verify 1-Click Convert to Order
    fireEvent.click(screen.getByTestId("convert-quote-btn-q-1"));
    await waitFor(() => {
      expect(screen.getByText(/ORD-B2B-99/)).toBeInTheDocument();
    });
  });
});
