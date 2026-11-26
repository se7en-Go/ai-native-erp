// Track 2 — B2B buyer magic-link self-service portal client.
//
// Two surfaces, two transports, deliberately kept apart:
//
//  * operator helpers go through `bffFetch` (`/api/bff/*`, session cookie, 401
//    refresh) because minting/revoking a magic link is a tenant-scoped act;
//  * buyer helpers go through the narrow public proxy (`/api/portal/*`) because a
//    buying company holds no ERP session — the opaque token in the URL is the only
//    credential, and it is never sent to the operator BFF namespace.
//
// Money stays a string end-to-end (the API speaks `Decimal`), so no IEEE-754
// rounding happens on the way to or from `NUMERIC` columns.
import { bffFetch } from "@/lib/api";
import { BffApiError, type ProblemDetail } from "@/lib/contracts";
import { shouldUseMockApi } from "@/lib/data-source";

export type PortalDocumentType = "pi" | "ci" | "pl";
export type PortalLanguage = "en" | "zh";

export interface PortalLink {
  id: string;
  quote_id: string;
  token_prefix: string;
  buyer_email?: string | null;
  allow_accept: boolean;
  allow_receipt_upload: boolean;
  expires_at?: string | null;
  revoked_at?: string | null;
  last_accessed_at?: string | null;
  accepted_at?: string | null;
  accepted_by_name?: string | null;
  is_active: boolean;
  created_at: string;
}

export interface PortalLinkCreateRequest {
  buyer_email?: string | null;
  allow_accept?: boolean;
  allow_receipt_upload?: boolean;
  expires_in_days?: number | null;
}

export interface PortalLinkCreateResponse {
  link: PortalLink;
  plain_token: string;
  portal_path: string;
}

export interface PortalSeller {
  company_name: string;
  address: string;
  phone: string;
  email: string;
  website: string;
  tax_or_reg_no: string;
  bank_beneficiary_name: string;
  bank_name: string;
  bank_account_no: string;
  bank_swift_code: string;
  bank_address: string;
  terms_notes: string;
}

export interface PortalQuoteLine {
  sku: string;
  description: string;
  quantity: number;
  unit_price: string;
  line_total: string;
}

export interface PortalQuote {
  quote_id: string;
  quote_no: string;
  version: number;
  status: string;
  incoterm: string;
  port_of_loading?: string | null;
  port_of_destination?: string | null;
  payment_terms: string;
  quote_currency: string;
  fx_rate_snapshot: string;
  fx_locked_until: string;
  freight_fee: string;
  insurance_fee: string;
  customs_duty_estimate: string;
  total_amount: string;
  items: PortalQuoteLine[];
}

export interface PortalReceivable {
  id: string;
  milestone_label: string;
  due_amount: string;
  paid_amount: string;
  currency: string;
  status: string;
  due_date?: string | null;
}

export interface PortalTrackingEvent {
  occurred_at?: string | null;
  description: string;
  location?: string | null;
  stage?: string | null;
}

export interface PortalShipment {
  id: string;
  carrier?: string | null;
  tracking_number: string;
  standardized_status: string;
  sub_status?: string | null;
  last_event_at?: string | null;
  estimated_delivery_at?: string | null;
  tracking_events: PortalTrackingEvent[];
}

export interface PortalOrder {
  order_id: string;
  order_no: string;
  fulfillment_status: string;
  payment_status: string;
  total_amount: string;
  currency: string;
  receivables: PortalReceivable[];
  shipments: PortalShipment[];
}

export interface BuyerPortalView {
  seller: PortalSeller;
  quote: PortalQuote;
  order?: PortalOrder | null;
  can_accept: boolean;
  can_upload_receipt: boolean;
  accepted_at?: string | null;
  accepted_by_name?: string | null;
}

export interface PortalAcceptResponse {
  quote_id: string;
  quote_no: string;
  status: string;
  accepted_at?: string | null;
  accepted_by_name?: string | null;
  order_id?: string | null;
}

export interface PortalReceiptRequest {
  rp_id: string;
  payment_method?: string;
  external_tx_ref?: string | null;
  amount_received: string;
  currency: string;
}

export interface PortalReorderRequest {
  notes?: string | null;
  requested_quantities?: Record<string, number> | null;
}

export interface PortalReorderResponse {
  new_quote_id: string;
  new_quote_no: string;
  total_amount: string;
  currency: string;
  status: string;
  created_at: string;
}

export interface PortalReceiptResponse {
  payment_id: string;
  rp_id: string;
  order_id: string;
  amount_received: string;
  currency: string;
  receipt_r2_key?: string | null;
  status: "pending_verification";
}

// ---------------------------------------------------------------------------
// Operator surface (`/api/bff/*`)
// ---------------------------------------------------------------------------

export function fetchQuotePortalLinks(quoteId: string): Promise<PortalLink[]> {
  if (shouldUseMockApi()) {
    return Promise.resolve(mockPortalStore.filter((link) => link.quote_id === quoteId));
  }
  return bffFetch<PortalLink[]>(`/b2b/quotes/${quoteId}/portal-links`);
}

export function createQuotePortalLink(
  quoteId: string,
  payload: PortalLinkCreateRequest = {}
): Promise<PortalLinkCreateResponse> {
  if (shouldUseMockApi()) {
    return Promise.resolve(mockCreatePortalLink(quoteId, payload));
  }
  return bffFetch<PortalLinkCreateResponse>(`/b2b/quotes/${quoteId}/portal-links`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function revokeQuotePortalLink(tokenId: string): Promise<PortalLink> {
  if (shouldUseMockApi()) {
    return Promise.resolve(mockRevokePortalLink(tokenId));
  }
  return bffFetch<PortalLink>(`/b2b/portal-links/${tokenId}`, { method: "DELETE" });
}

// ---------------------------------------------------------------------------
// Buyer surface (`/api/portal/*` — no session, token is the credential)
// ---------------------------------------------------------------------------

async function portalFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (!(init.body instanceof FormData) && !headers.has("Content-Type") && init.body) {
    headers.set("Content-Type", "application/json");
  }
  const res = await fetch(`/api/portal${path}`, {
    ...init,
    headers,
    cache: "no-store",
  });
  if (!res.ok) {
    let problem: ProblemDetail = {
      type: "https://erp.local/errors/http-error",
      title: res.statusText || "Request failed",
      status: res.status,
      detail: `Request failed with status ${res.status}`,
      code: res.status === 403 ? "ERR_PORTAL_REQUEST_FORBIDDEN" : "ERR_API_FAILURE",
    };
    try {
      const parsed = (await res.json()) as Partial<ProblemDetail>;
      problem = { ...problem, ...parsed, status: res.status };
    } catch {
      // keep the fallback problem detail
    }
    throw new BffApiError(problem);
  }
  return (await res.json()) as T;
}

export function buyerPortalPagePath(token: string): string {
  return `/portal/${encodeURIComponent(token)}`;
}

export function fetchBuyerPortal(token: string): Promise<BuyerPortalView> {
  if (shouldUseMockApi()) {
    return Promise.resolve(mockBuyerPortalView());
  }
  return portalFetch<BuyerPortalView>(`/${encodeURIComponent(token)}`);
}

export function acceptBuyerPortalQuote(
  token: string,
  buyerName: string
): Promise<PortalAcceptResponse> {
  if (shouldUseMockApi()) {
    return Promise.resolve({
      quote_id: mockBuyerPortalView().quote.quote_id,
      quote_no: mockBuyerPortalView().quote.quote_no,
      status: "accepted",
      accepted_at: new Date().toISOString(),
      accepted_by_name: buyerName,
      order_id: null,
    });
  }
  return portalFetch<PortalAcceptResponse>(`/${encodeURIComponent(token)}/accept`, {
    method: "POST",
    body: JSON.stringify({ buyer_name: buyerName }),
  });
}

/**
 * One-click reorder: re-prices the same SKUs into a brand new **draft** quotation.
 *
 * The backend answers with a buyer-safe projection only (`new_quote_no`, total, status) —
 * never a cost or margin field — so the return type carries exactly that.
 */
export function reorderBuyerPortalQuote(
  token: string,
  payload: PortalReorderRequest = {}
): Promise<PortalReorderResponse> {
  if (shouldUseMockApi()) {
    const view = mockBuyerPortalView();
    return Promise.resolve({
      new_quote_id: "mock-quote-reorder-1",
      new_quote_no: "PI-REO-202611-MOCK01",
      total_amount: view.quote.total_amount,
      currency: view.quote.quote_currency,
      status: "draft",
      created_at: MOCK_NOW,
    });
  }
  return portalFetch<PortalReorderResponse>(`/${encodeURIComponent(token)}/reorder`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function submitBuyerPortalReceipt(
  token: string,
  payload: PortalReceiptRequest,
  file?: File | null
): Promise<PortalReceiptResponse> {
  if (shouldUseMockApi()) {
    return Promise.resolve({
      payment_id: "mock-payment-1",
      rp_id: payload.rp_id,
      order_id: "mock-order-1",
      amount_received: payload.amount_received,
      currency: payload.currency,
      receipt_r2_key: file ? `receipts/mock/portal/${file.name}` : null,
      status: "pending_verification",
    });
  }
  if (!file) {
    return portalFetch<PortalReceiptResponse>(`/${encodeURIComponent(token)}/receipt`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }
  const form = new FormData();
  form.set("rp_id", payload.rp_id);
  form.set("payment_method", payload.payment_method ?? "bank_transfer");
  if (payload.external_tx_ref) form.set("external_tx_ref", payload.external_tx_ref);
  form.set("amount_received", payload.amount_received);
  form.set("currency", payload.currency);
  form.set("receipt_file", file);
  return portalFetch<PortalReceiptResponse>(`/${encodeURIComponent(token)}/receipt`, {
    method: "POST",
    body: form,
  });
}

export function buyerDocumentDownloadUrl(
  token: string,
  docType: PortalDocumentType,
  language: PortalLanguage = "en"
): string {
  const params = new URLSearchParams({ language });
  return `/api/portal/${encodeURIComponent(token)}/documents/${docType}?${params.toString()}`;
}

// ---------------------------------------------------------------------------
// Mock-mode fixtures (only reachable with NEXT_PUBLIC_USE_MOCK_API=true)
// ---------------------------------------------------------------------------

const MOCK_NOW = "2026-11-26T09:00:00.000Z";
let mockPortalStore: PortalLink[] = [];

function mockLinkFor(quoteId: string, payload: PortalLinkCreateRequest): PortalLinkCreateResponse {
  const token = `erp_b2b_mock${Math.random().toString(36).slice(2, 12)}`;
  const link: PortalLink = {
    id: `portal-link-${mockPortalStore.length + 1}`,
    quote_id: quoteId,
    token_prefix: token.slice(0, 16),
    buyer_email: payload.buyer_email ?? null,
    allow_accept: payload.allow_accept ?? true,
    allow_receipt_upload: payload.allow_receipt_upload ?? true,
    expires_at: null,
    revoked_at: null,
    last_accessed_at: null,
    accepted_at: null,
    accepted_by_name: null,
    is_active: true,
    created_at: MOCK_NOW,
  };
  mockPortalStore = [link, ...mockPortalStore];
  return { link, plain_token: token, portal_path: `/portal/${token}` };
}

function mockCreatePortalLink(
  quoteId: string,
  payload: PortalLinkCreateRequest
): PortalLinkCreateResponse {
  return mockLinkFor(quoteId, payload);
}

function mockRevokePortalLink(tokenId: string): PortalLink {
  const current = mockPortalStore.find((link) => link.id === tokenId);
  const revoked: PortalLink = {
    ...(current ?? {
      id: tokenId,
      quote_id: "quote-1",
      token_prefix: "erp_b2b_mock",
      buyer_email: null,
      allow_accept: true,
      allow_receipt_upload: true,
      expires_at: null,
      last_accessed_at: null,
      accepted_at: null,
      accepted_by_name: null,
      created_at: MOCK_NOW,
    }),
    revoked_at: MOCK_NOW,
    is_active: false,
  };
  mockPortalStore = mockPortalStore.map((link) =>
    link.id === tokenId ? revoked : link
  );
  return revoked;
}

function mockBuyerPortalView(): BuyerPortalView {
  return {
    seller: {
      company_name: "Quanzhou Smart LED Lighting Co., Ltd.",
      address: "Export Processing Zone, Quanzhou, Fujian, China",
      phone: "+86 595 8888 0000",
      email: "export@quanzhou-led.example.com",
      website: "https://quanzhou-led.example.com",
      tax_or_reg_no: "91350500MA2XXXXX1A",
      bank_beneficiary_name: "Quanzhou Smart LED Lighting Co., Ltd.",
      bank_name: "Bank of China, Quanzhou Branch",
      bank_account_no: "4060 1234 5678 9012 345",
      bank_swift_code: "BKCHCNBJ73A",
      bank_address: "Quanzhou, Fujian, China",
      terms_notes: "30% T/T deposit, 70% balance before shipment.",
    },
    quote: {
      quote_id: "quote-1",
      quote_no: "PI-202611-0042",
      version: 1,
      status: "sent",
      incoterm: "FOB",
      port_of_loading: "Xiamen, China",
      port_of_destination: "Hamburg, Germany",
      payment_terms: "30% T/T deposit, 70% before shipment",
      quote_currency: "USD",
      fx_rate_snapshot: "7.200000",
      fx_locked_until: "2026-12-10T00:00:00.000Z",
      freight_fee: "200.00",
      insurance_fee: "0.00",
      customs_duty_estimate: "0.00",
      total_amount: "4450.00",
      items: [
        {
          sku: "LED-STRIP-5M-IP65",
          description: "5M IP65 Smart LED Strip 24V",
          quantity: 500,
          unit_price: "8.5000",
          line_total: "4250.00",
        },
      ],
    },
    order: null,
    can_accept: true,
    can_upload_receipt: false,
    accepted_at: null,
    accepted_by_name: null,
  };
}
