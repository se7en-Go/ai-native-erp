// Structural mirrors of OpportunityCard (app/b2b/quotes/page) and QuoteRecord
// (components/b2b/quote-pi-builder). fx_locked_until is generated relative to
// now so the mock PI never shows an expired FX lock.
export interface MockOpportunity {
  id: string;
  title: string;
  company_name?: string | null;
  stage: "discovery" | "sampling" | "quoting" | "negotiating" | "closed_won";
  expected_amount: string;
  currency: string;
}

export interface MockQuoteItem {
  id: string;
  variant_id: string;
  sku_snapshot: string;
  quantity: number;
  moq_tier_applied?: number | null;
  unit_price_quote_ccy: string;
  line_total_quote_ccy: string;
}

export interface MockQuote {
  id: string;
  opportunity_id: string;
  quote_no: string;
  version: number;
  is_proforma_invoice: boolean;
  incoterm: string;
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
  status: string;
  converted_order_id?: string | null;
  items: MockQuoteItem[];
}

export const mockOpportunities: MockOpportunity[] = [
  {
    id: "opp-1",
    title: "Berlin Lighting 500pcs COB Strip",
    company_name: "Berlin Lighting GmbH",
    stage: "quoting",
    expected_amount: "7100.00",
    currency: "USD",
  },
  {
    id: "opp-2",
    title: "Warsaw Retail Chain Panel Trial",
    company_name: "Lumen Polska Sp. z o.o.",
    stage: "sampling",
    expected_amount: "3200.00",
    currency: "USD",
  },
  {
    id: "opp-3",
    title: "Dubai Hotel Corridor Retrofit",
    company_name: "Gulf Hospitality Group",
    stage: "discovery",
    expected_amount: "15400.00",
    currency: "USD",
  },
];

export const mockQuotes: MockQuote[] = [
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
    fx_locked_until: new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString(),
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
];

export function mockOpportunitiesPage(): {
  items: MockOpportunity[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
} {
  return {
    items: mockOpportunities,
    total: mockOpportunities.length,
    limit: 50,
    offset: 0,
    has_more: false,
  };
}

export function mockQuotesPage(opportunityId?: string): {
  items: MockQuote[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
} {
  const items = opportunityId
    ? mockQuotes.filter((q) => q.opportunity_id === opportunityId)
    : mockQuotes;
  return { items, total: items.length, limit: 20, offset: 0, has_more: false };
}
