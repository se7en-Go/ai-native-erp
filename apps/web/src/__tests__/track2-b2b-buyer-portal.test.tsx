// Track 2 — B2B buyer magic-link self-service portal (web).
//
// Locks four things down:
//   1. the edge middleware lets `/portal/<token>` through with NO auth cookie while
//      `/b2b/quotes` still bounces to /login (buyer links must never need a session);
//   2. the dedicated `/api/portal/*` proxy only ever reaches `/api/v1/portal/*`;
//   3. the operator `<PortalLinkPanel />` mints/copies/revokes magic links;
//   4. the buyer page renders seller + PI + documents + milestones + shipments and
//      drives accept / T/T upload through the token-only transport.
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { NextRequest } from "next/server";

import BuyerPortalPage from "@/app/portal/[token]/page";
import { BuyerPortalPageView } from "@/components/b2b/buyer-portal-view";
import { GET } from "@/app/api/portal/[...path]/route";
import { PortalLinkPanel } from "@/components/b2b/portal-link-panel";
import { I18nProvider } from "@/lib/i18n";
import {
  acceptBuyerPortalQuote,
  createQuotePortalLink,
  fetchBuyerPortal,
  fetchQuotePortalLinks,
  revokeQuotePortalLink,
  submitBuyerPortalReceipt,
  type BuyerPortalView,
  type PortalLink,
} from "@/lib/api-client/track2-portal";
import { middleware } from "@/middleware";

vi.mock("@/lib/api-client/track2-portal", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api-client/track2-portal")>();
  return {
    ...actual,
    fetchBuyerPortal: vi.fn(),
    acceptBuyerPortalQuote: vi.fn(),
    submitBuyerPortalReceipt: vi.fn(),
    fetchQuotePortalLinks: vi.fn(),
    createQuotePortalLink: vi.fn(),
    revokeQuotePortalLink: vi.fn(),
  };
});

const resolvePortalUpstreamPath = GET.resolvePortalUpstreamPath;

const originalEnv = { ...process.env };

beforeEach(() => {
  vi.clearAllMocks();
  process.env = { ...originalEnv };
});

afterEach(() => {
  process.env = { ...originalEnv };
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// 1. Edge auth gate
// ---------------------------------------------------------------------------

function middlewareRequest(path: string, cookies?: Record<string, string>): NextRequest {
  const cookieHeader = cookies
    ? Object.entries(cookies)
        .map(([name, value]) => `${name}=${value}`)
        .join("; ")
    : undefined;
  return new NextRequest(`http://localhost:3000${path}`, {
    ...(cookieHeader ? { headers: { cookie: cookieHeader } } : {}),
  });
}

function isAllowedThrough(response: { headers: Headers }): boolean {
  return response.headers.get("x-middleware-next") === "1";
}

describe("edge auth gate (portal is public, operator routes are not)", () => {
  it("treats /portal/<token> as public and /b2b/quotes as protected", () => {
    expect(middleware.isPublicPath("/portal/erp_b2b_abc123")).toBe(true);
    expect(middleware.isPublicPath("/portal")).toBe(false);
    expect(middleware.isPublicPath("/b2b/quotes")).toBe(false);
    expect(middleware.isPublicPath("/login")).toBe(true);
    expect(middleware.isPublicPath("/api/bff/api/v1/b2b/quotes")).toBe(true);

    expect(middleware.shouldRedirectToLogin("/portal/erp_b2b_abc123", false)).toBe(false);
    expect(middleware.shouldRedirectToLogin("/b2b/quotes", false)).toBe(true);
    expect(middleware.shouldRedirectToLogin("/b2b/quotes", true)).toBe(false);
  });

  it("lets a buyer magic link through without any auth cookie", () => {
    const response = middleware(middlewareRequest("/portal/erp_b2b_abc123"));
    expect(isAllowedThrough(response)).toBe(true);
  });

  it("still redirects an unauthenticated operator deep link to /login", () => {
    const response = middleware(middlewareRequest("/b2b/quotes"));
    expect(response.status).toBe(307);
    const location = response.headers.get("location") ?? "";
    expect(location).toContain("/login");
    expect(location).toContain("next=%2Fb2b%2Fquotes");
  });

  it("keeps an authenticated operator session on /b2b/quotes", () => {
    const response = middleware(middlewareRequest("/b2b/quotes", { erp_access_token: "t" }));
    expect(isAllowedThrough(response)).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// 2. Public portal proxy
// ---------------------------------------------------------------------------

type FetchMock = ReturnType<typeof vi.fn>;

function portalContext(path: string): { params: Promise<{ path: string[] }> } {
  return { params: Promise.resolve({ path: path.split("/") }) };
}

function portalRequest(path: string, init?: { method?: string; headers?: Record<string, string>; body?: string }) {
  return new NextRequest(`http://localhost:3000/api/portal/${path}`, {
    method: init?.method ?? "GET",
    ...(init?.headers ? { headers: init.headers } : {}),
    ...(init?.body ? { body: init.body } : {}),
  });
}

describe("public /api/portal proxy", () => {
  it("maps only well-formed portal paths onto /api/v1/portal/*", () => {
    expect(resolvePortalUpstreamPath(["erp_b2b_abc123"])).toBe("/api/v1/portal/erp_b2b_abc123");
    expect(resolvePortalUpstreamPath(["erp_b2b_abc123", "accept"])).toBe(
      "/api/v1/portal/erp_b2b_abc123/accept"
    );
    expect(resolvePortalUpstreamPath(["erp_b2b_abc123", "receipt"])).toBe(
      "/api/v1/portal/erp_b2b_abc123/receipt"
    );
    expect(resolvePortalUpstreamPath(["erp_b2b_abc123", "documents", "pl"])).toBe(
      "/api/v1/portal/erp_b2b_abc123/documents/pl"
    );
  });

  it("refuses traversal, non-portal tokens and non-portal subpaths", () => {
    expect(resolvePortalUpstreamPath([])).toBeNull();
    expect(resolvePortalUpstreamPath(["..", "erp_b2b_abc"])).toBeNull();
    expect(resolvePortalUpstreamPath(["erp_b2b_abc", ".."])).toBeNull();
    expect(resolvePortalUpstreamPath(["erp_b2b_abc", "documents", ".."])).toBeNull();
    expect(resolvePortalUpstreamPath(["api", "v1", "orders"])).toBeNull();
    expect(resolvePortalUpstreamPath(["erp_col_abc123"])).toBeNull();
    expect(resolvePortalUpstreamPath(["erp_b2b_abc", "documents", "exe"])).toBeNull();
    expect(resolvePortalUpstreamPath(["erp_b2b_abc", "accept", "extra"])).toBeNull();
  });

  it("403s a non-portal path without opening an upstream socket", async () => {
    const fetchMock = vi.fn() as unknown as FetchMock;
    vi.stubGlobal("fetch", fetchMock);

    const response = await GET(
      portalRequest("api/v1/orders", { method: "POST", body: "{}" }),
      portalContext("api/v1/orders")
    );
    const body = (await response.json()) as Record<string, unknown>;

    expect(response.status).toBe(403);
    expect(body.code).toBe("ERR_PORTAL_PATH_FORBIDDEN");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("proxies a buyer PDF through untouched and marks it no-store", async () => {
    const pdfBytes = new Uint8Array([0x25, 0x50, 0x44, 0x46, 0x2d, 0x31, 0x2e, 0x34]);
    const fetchMock = vi.fn(async () => {
      return new Response(pdfBytes, {
        status: 200,
        headers: {
          "content-type": "application/pdf",
          "content-disposition": 'attachment; filename="PI-202611-0042-v1.pdf"',
        },
      });
    }) as unknown as FetchMock;
    vi.stubGlobal("fetch", fetchMock);

    const response = await GET(
      portalRequest("erp_b2b_abc123/documents/pi?language=zh"),
      portalContext("erp_b2b_abc123/documents/pi")
    );

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [targetUrl] = fetchMock.mock.calls[0] as [string];
    expect(targetUrl).toContain("/api/v1/portal/erp_b2b_abc123/documents/pi");
    expect(targetUrl).toContain("language=zh");
    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toBe("application/pdf");
    expect(response.headers.get("content-disposition")).toContain("PI-202611-0042-v1.pdf");
    expect(response.headers.get("cache-control")).toBe("no-store");
    const bytes = new Uint8Array(await response.arrayBuffer());
    expect(Array.from(bytes.slice(0, 5))).toEqual([0x25, 0x50, 0x44, 0x46, 0x2d]);
  });

  it("forwards a buyer accept POST with its JSON body", async () => {
    const fetchMock = vi.fn(async () => {
      return new Response(JSON.stringify({ status: "accepted" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }) as unknown as FetchMock;
    vi.stubGlobal("fetch", fetchMock);

    const { POST } = await import("@/app/api/portal/[...path]/route");
    const response = await POST(
      portalRequest("erp_b2b_abc123/accept", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ buyer_name: "Carlos Mendez" }),
      }),
      portalContext("erp_b2b_abc123/accept")
    );

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ status: "accepted" });
    const [targetUrl, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(targetUrl).toContain("/api/v1/portal/erp_b2b_abc123/accept");
    expect(init.method).toBe("POST");
    expect(new TextDecoder().decode(init.body as ArrayBuffer)).toContain("Carlos Mendez");
    expect((init.headers as Headers).get("content-type")).toBe("application/json");
  });
});

// ---------------------------------------------------------------------------
// 3. Operator panel
// ---------------------------------------------------------------------------

const QUOTES = [
  { id: "quote-1", quote_no: "PI-202611-0042", version: 1, status: "sent" },
  { id: "quote-2", quote_no: "PI-202611-0043", version: 2, status: "draft" },
];

function linkFixture(overrides: Partial<PortalLink> = {}): PortalLink {
  return {
    id: "link-1",
    quote_id: "quote-1",
    token_prefix: "erp_b2b_test1234",
    buyer_email: "carlos@iberialight.example.com",
    allow_accept: true,
    allow_receipt_upload: true,
    expires_at: null,
    revoked_at: null,
    last_accessed_at: null,
    accepted_at: null,
    accepted_by_name: null,
    is_active: true,
    created_at: "2026-11-26T09:00:00.000Z",
    ...overrides,
  };
}

function renderPanel() {
  return render(
    <I18nProvider>
      <PortalLinkPanel quotes={QUOTES} />
    </I18nProvider>
  );
}

describe("<PortalLinkPanel />", () => {
  beforeEach(() => {
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: vi.fn(async () => undefined) },
      configurable: true,
    });
  });

  it("lists active links for the selected quote and revokes them", async () => {
    const active = linkFixture();
    const revoked = linkFixture({ id: "link-1", is_active: false, revoked_at: "2026-11-26T10:00:00Z" });
    vi.mocked(fetchQuotePortalLinks).mockResolvedValue([active]);
    vi.mocked(revokeQuotePortalLink).mockResolvedValue(revoked);

    renderPanel();

    await waitFor(() => expect(vi.mocked(fetchQuotePortalLinks)).toHaveBeenCalledWith("quote-1"));
    expect(await screen.findByTestId("portal-link-list")).toBeInTheDocument();
    expect(screen.getByText("erp_b2b_test1234…")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("portal-revoke-link-1"));

    await waitFor(() => expect(vi.mocked(revokeQuotePortalLink)).toHaveBeenCalledWith("link-1"));
    await waitFor(() =>
      expect(screen.getByTestId("portal-revoke-link-1")).toBeDisabled()
    );
  });

  it("generates a one-time portallink, shows the /portal/<token> URL and copies it", async () => {
    vi.mocked(fetchQuotePortalLinks).mockResolvedValue([]);
    vi.mocked(createQuotePortalLink).mockResolvedValue({
      link: linkFixture(),
      plain_token: "erp_b2b_fresh_token_value",
      portal_path: "/portal/erp_b2b_fresh_token_value",
    });

    renderPanel();

    await waitFor(() => expect(screen.getByTestId("portal-link-empty")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("portal-generate-button"));

    const url = await screen.findByTestId("portal-fresh-link-url");
    expect(url.textContent).toContain("/portal/erp_b2b_fresh_token_value");
    expect(vi.mocked(createQuotePortalLink)).toHaveBeenCalledWith("quote-1", {});

    fireEvent.click(screen.getByTestId("portal-copy-button"));
    await waitFor(() =>
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
        expect.stringContaining("/portal/erp_b2b_fresh_token_value")
      )
    );
  });

  it("switches the quote scope when another quote is selected", async () => {
    vi.mocked(fetchQuotePortalLinks).mockResolvedValue([]);
    renderPanel();

    await waitFor(() => expect(vi.mocked(fetchQuotePortalLinks)).toHaveBeenCalledWith("quote-1"));
    fireEvent.change(screen.getByTestId("portal-quote-select"), {
      target: { value: "quote-2" },
    });
    await waitFor(() => expect(vi.mocked(fetchQuotePortalLinks)).toHaveBeenCalledWith("quote-2"));
  });

  it("surfaces an API error instead of crashing", async () => {
    const { BffApiError } = await import("@/lib/contracts");
    vi.mocked(fetchQuotePortalLinks).mockRejectedValue(
      new BffApiError({
        type: "https://erp.local/errors/forbidden",
        title: "Forbidden",
        status: 403,
        detail: "This quote is outside your workspace.",
        code: "ERR_BFF_PATH_FORBIDDEN",
      })
    );

    renderPanel();

    const error = await screen.findByTestId("portal-link-error");
    expect(error.textContent).toContain("This quote is outside your workspace.");
  });
});

// ---------------------------------------------------------------------------
// 4. Buyer portal page
// ---------------------------------------------------------------------------

function buyerViewFixture(overrides: Partial<BuyerPortalView> = {}): BuyerPortalView {
  return {
    seller: {
      company_name: "Quanzhou Smart LED Lighting Co., Ltd.",
      address: "88 Export Avenue, Quanzhou, Fujian, China",
      phone: "+86 595 8888 0000",
      email: "export@qzled.example.com",
      website: "https://qzled.example.com",
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
      port_of_destination: "Valencia, Spain",
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
    order: {
      order_id: "order-1",
      order_no: "ORD-PORTAL-A",
      fulfillment_status: "pending_payment",
      payment_status: "unpaid",
      total_amount: "4450.00",
      currency: "USD",
      receivables: [
        {
          id: "rp-deposit",
          milestone_label: "b2b_30_deposit",
          due_amount: "1335.00",
          paid_amount: "0.00",
          currency: "USD",
          status: "open",
          due_date: null,
        },
        {
          id: "rp-balance",
          milestone_label: "b2b_70_balance",
          due_amount: "3115.00",
          paid_amount: "0.00",
          currency: "USD",
          status: "open",
          due_date: null,
        },
      ],
      shipments: [
        {
          id: "ship-1",
          carrier: "DHL",
          tracking_number: "DHL99887766",
          standardized_status: "InTransit",
          sub_status: "CustomsHold",
          last_event_at: "2026-11-20T08:15:00.000Z",
          estimated_delivery_at: null,
          tracking_events: [
            {
              occurred_at: "2026-11-20T08:15:00.000Z",
              description: "Departed Xiamen sorting centre",
              location: "Xiamen, CN",
              stage: "InTransit",
            },
          ],
        },
      ],
    },
    can_accept: true,
    can_upload_receipt: true,
    accepted_at: null,
    accepted_by_name: null,
    ...overrides,
  };
}

describe("<BuyerPortalPageView />", () => {
  it("renders seller, PI lines, documents, milestones and the 17TRACK timeline", async () => {
    vi.mocked(fetchBuyerPortal).mockResolvedValue(buyerViewFixture());

    render(<BuyerPortalPageView token="erp_b2b_test_token" />);

    expect(await screen.findByTestId("buyer-portal-seller-name")).toHaveTextContent(
      "Quanzhou Smart LED Lighting Co., Ltd."
    );
    expect(await screen.findByText("LED-STRIP-5M-IP65")).toBeInTheDocument();
    expect(screen.getByTestId("buyer-portal-quote-total").textContent).toContain("4,450.00");
    expect(screen.getByTestId("buyer-portal-quote-status")).toHaveTextContent("sent");

    // Documents link to the narrow public proxy, not the operator BFF.
    expect(screen.getByTestId("download-pi").getAttribute("href")).toBe(
      "/api/portal/erp_b2b_test_token/documents/pi?language=en"
    );
    expect(screen.getByTestId("download-ci").getAttribute("href")).toContain(
      "/api/portal/erp_b2b_test_token/documents/ci"
    );
    expect(screen.getByTestId("download-pl").getAttribute("href")).toContain(
      "/api/portal/erp_b2b_test_token/documents/pl"
    );

    expect(screen.getByTestId("milestone-list")).toHaveTextContent("$1,335.00");
    expect(screen.getByTestId("milestone-list")).toHaveTextContent("$3,115.00");

    const timeline = screen.getByTestId("shipment-timeline");
    expect(timeline).toHaveTextContent("Departed Xiamen sorting centre");
    expect(timeline).toHaveTextContent("Xiamen, CN");
    expect(screen.getByText(/DHL99887766/)).toBeInTheDocument();
  });

  it("accepts the quotation through the token-only transport", async () => {
    vi.mocked(fetchBuyerPortal).mockResolvedValue(buyerViewFixture());
    vi.mocked(acceptBuyerPortalQuote).mockResolvedValue({
      quote_id: "quote-1",
      quote_no: "PI-202611-0042",
      status: "accepted",
      accepted_at: "2026-11-26T10:00:00.000Z",
      accepted_by_name: "Carlos Mendez",
      order_id: null,
    });

    render(<BuyerPortalPageView token="erp_b2b_test_token" />);

    const nameInput = await screen.findByTestId("accept-buyer-name");
    fireEvent.change(nameInput, { target: { value: "Carlos Mendez" } });
    fireEvent.click(screen.getByTestId("accept-quote-button"));

    await waitFor(() =>
      expect(vi.mocked(acceptBuyerPortalQuote)).toHaveBeenCalledWith(
        "erp_b2b_test_token",
        "Carlos Mendez"
      )
    );
    expect(await screen.findByTestId("accept-success")).toBeInTheDocument();
    expect(screen.getByTestId("buyer-portal-quote-status")).toHaveTextContent("accepted");
  });

  it("submits a T/T bank slip and reports the pending Finance verification", async () => {
    vi.mocked(fetchBuyerPortal).mockResolvedValue(buyerViewFixture());
    vi.mocked(submitBuyerPortalReceipt).mockResolvedValue({
      payment_id: "payment-1",
      rp_id: "rp-deposit",
      order_id: "order-1",
      amount_received: "1335.00",
      currency: "USD",
      receipt_r2_key: "receipts/t/portal/abc_slip.png",
      status: "pending_verification",
    });

    render(<BuyerPortalPageView token="erp_b2b_test_token" />);

    await screen.findByTestId("receipt-form");
    await waitFor(() =>
      expect((screen.getByTestId("receipt-amount") as HTMLInputElement).value).toBe("1335.00")
    );
    fireEvent.change(screen.getByTestId("receipt-reference"), {
      target: { value: "TT-WEB-0001" },
    });
    fireEvent.click(screen.getByTestId("receipt-submit"));

    await waitFor(() =>
      expect(vi.mocked(submitBuyerPortalReceipt)).toHaveBeenCalledWith(
        "erp_b2b_test_token",
        expect.objectContaining({
          rp_id: "rp-deposit",
          amount_received: "1335.00",
          currency: "USD",
          external_tx_ref: "TT-WEB-0001",
        }),
        null
      )
    );
    expect(await screen.findByTestId("receipt-success")).toBeInTheDocument();
  });

  it("switches between EN and ZH without leaving the page", async () => {
    vi.mocked(fetchBuyerPortal).mockResolvedValue(buyerViewFixture());

    render(<BuyerPortalPageView token="erp_b2b_test_token" />);

    await screen.findByTestId("buyer-portal-seller-name");
    expect(screen.getByTestId("download-pi").textContent).toContain("Download PI");

    fireEvent.click(screen.getByTestId("portal-lang-toggle"));

    await waitFor(() =>
      expect(screen.getByTestId("download-pi").textContent).toContain("下载 PI")
    );
    expect(screen.getByTestId("receipt-submit").textContent).toContain("提交水单");
  });

  it("shows a retryable error for an expired or revoked link", async () => {
    const { BffApiError } = await import("@/lib/contracts");
    vi.mocked(fetchBuyerPortal).mockRejectedValue(
      new BffApiError({
        type: "https://erp.local/errors/invalid-portal-token",
        title: "Invalid Portal Link",
        status: 401,
        detail: "This buyer portal link has been revoked.",
        code: "ERR_INVALID_PORTAL_TOKEN",
      })
    );

    render(<BuyerPortalPageView token="erp_b2b_dead" />);

    const error = await screen.findByTestId("buyer-portal-error");
    expect(error.textContent).toContain("This buyer portal link has been revoked.");
  });

  it("keeps the default route export wired to the dynamic token param", () => {
    expect(typeof BuyerPortalPage).toBe("function");
  });
});
