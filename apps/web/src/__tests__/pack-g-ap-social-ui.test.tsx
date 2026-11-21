// Pack G UI tests: supplier accounts-payable (AP) settlement + the AR/AP
// cash-flow loop on /finance, and the social attribution funnel on /social.
//
// The client-module cases run the real `bffFetch` path so the URLs, methods and
// bodies are verified against the locked contract; the page cases run the real
// UI code (live path with a stateful fetch stub for /finance, mock fixtures for
// /social). Money assertions are exact decimal strings — a float would show up
// immediately as `0.30000000000000004` or a recomputed cent.
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import FinancePage from "@/app/finance/page";
import SocialPage from "@/app/social/page";
import { AuthSessionContext, type AuthSessionState } from "@/components/providers";
import { I18nProvider } from "@/lib/i18n";
import type { Principal } from "@/lib/contracts";
import type {
  CashflowSummaryRead,
  PayableListRead,
  PayableRead,
} from "@/lib/api-client/types.gen";
import {
  addDecimal,
  compareDecimal,
  fetchCashflowSummary,
  fetchPayables,
  fetchSocialAttributionFunnel,
  formatCents,
  parseMoneyToCents,
  payableOutstanding,
  paySupplierPayable,
  pctString,
  resetMockApLedger,
  subDecimal,
  toPayablePaymentPayload,
} from "@/lib/api-client/pack-g-ap-social";
import {
  fetchCallMethod,
  fetchCallUrl,
  normalizeFetchInput,
} from "@/__tests__/fetch-stub";

vi.mock("@/hooks/use-realtime-inbox", () => ({ useRealtimeInbox: vi.fn() }));

function principal(role: Principal["role"]): Principal {
  return {
    user_id: "11111111-1111-1111-1111-111111111111",
    tenant_id: "22222222-2222-2222-2222-222222222222",
    role,
    email: `${role}@qzled.example.com`,
    tenant_slug: "qz-led-factory",
    onboarding_stage: "LiveProduction",
  };
}

function renderWithProviders(ui: React.ReactNode, role: Principal["role"] = "owner") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const authValue: AuthSessionState = {
    principal: principal(role),
    isLoading: false,
    logout: vi.fn(async () => {}),
    refreshSession: vi.fn(async () => {}),
  };
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthSessionContext.Provider value={authValue}>
        <I18nProvider defaultLocale="zh">{ui}</I18nProvider>
      </AuthSessionContext.Provider>
    </QueryClientProvider>
  );
}

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Exact decimal money
// ---------------------------------------------------------------------------

describe("Pack G exact decimal money", () => {
  it("adds and subtracts in integer cents, never in binary floats", () => {
    // 0.1 + 0.2 === 0.30000000000000004 in IEEE-754; the ledger must not.
    expect(addDecimal("0.10", "0.20")).toBe("0.30");
    expect(addDecimal("0.07", "0.01")).toBe("0.08");
    expect(subDecimal("48000.00", "20000.00")).toBe("28000.00");
    expect(subDecimal("96500.00", "35600.00")).toBe("60900.00");
    expect(subDecimal("0.00", "12.34")).toBe("-12.34");
    expect(parseMoneyToCents("1,234.50")).toBe(123450n);
    expect(formatCents(123450n)).toBe("1234.50");
    expect(compareDecimal("28600.00", "28600.00")).toBe(0);
    expect(compareDecimal("28600.01", "28600.00")).toBe(1);
    expect(compareDecimal("28599.99", "28600.00")).toBe(-1);
    expect(payableOutstanding({ due_amount: "48000.00", paid_amount: "20000.00" })).toBe(
      "28000.00"
    );
  });

  it("derives percentages with ROUND_HALF_UP and a hard 0.00 floor", () => {
    expect(pctString(36, 233)).toBe("15.45");
    expect(pctString(7, 42)).toBe("16.67");
    expect(pctString(1, 3)).toBe("33.33");
    expect(pctString(2, 3)).toBe("66.67");
    expect(pctString(42, 42)).toBe("100.00");
    expect(pctString(0, 42)).toBe("0.00");
    expect(pctString(5, 0)).toBe("0.00");
  });

  it("normalises an operator amount to a 2-dp string and rejects junk", () => {
    expect(toPayablePaymentPayload("1234.5", "  BANK-20261120-0123  ")).toEqual({
      amount: "1234.50",
      payment_ref: "BANK-20261120-0123",
    });
    expect(toPayablePaymentPayload("28600", "")).toEqual({ amount: "28600.00", payment_ref: "" });

    for (const amount of ["0", "0.00", "-5.00", "12.345", "", "abc", "1e3"]) {
      expect(() => toPayablePaymentPayload(amount, "BANK-1")).toThrow(/两位小数/);
    }
  });
});

// ---------------------------------------------------------------------------
// Client module (live bffFetch contract)
// ---------------------------------------------------------------------------

describe("Pack G client module (live bffFetch contract)", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "false");
  });

  it("reads the AP ledger from /v1/finance/payables", async () => {
    const list: PayableListRead = {
      items: [],
      total_due: "101400.00",
      total_paid: "35600.00",
      total_outstanding: "65800.00",
      currency: "CNY",
    };
    const fetchMock = vi.fn(async () => json(list));
    vi.stubGlobal("fetch", fetchMock);

    const result = await fetchPayables();

    expect(result.total_outstanding).toBe("65800.00");
    const call = fetchMock.mock.calls[0];
    expect(fetchCallUrl(call)).toBe("/api/bff/v1/finance/payables");
    expect(fetchCallMethod(call) ?? "GET").not.toBe("POST");
  });

  it("settles a payable with an exact string amount, never a JSON float", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
      json({
        id: "ap-0002",
        tenant_id: "t1",
        po_id: "po-0002",
        supplier_id: "sup-js",
        po_no: "PO-2026-1112",
        supplier_name: "东莞捷顺五金制品厂",
        due_amount: "28600.00",
        paid_amount: "28600.00",
        currency: "CNY",
        due_date: "2026-11-28",
        status: "paid",
        payment_ref: "BANK-20261120-0123",
        paid_at: "2026-11-20T02:00:00Z",
        created_at: "2026-11-02T02:00:00Z",
        updated_at: "2026-11-20T02:00:00Z",
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    const updated = await paySupplierPayable(
      "ap-0002",
      toPayablePaymentPayload("28600.00", "BANK-20261120-0123")
    );

    expect(updated.status).toBe("paid");
    const call = fetchMock.mock.calls[0];
    expect(fetchCallUrl(call)).toBe("/api/bff/v1/finance/payables/ap-0002/pay");
    expect(fetchCallMethod(call)).toBe("POST");
    const body = JSON.parse(String((call[1] as RequestInit | undefined)?.body)) as {
      amount: unknown;
      payment_ref: string;
    };
    expect(body.amount).toBe("28600.00");
    // A number here would be rejected by the backend's ExactDecimal.
    expect(typeof body.amount).toBe("string");
    expect(body.payment_ref).toBe("BANK-20261120-0123");
  });

  it("reads the AR/AP cash-flow summary and the social attribution funnel", async () => {
    const summary: CashflowSummaryRead = {
      currency: "CNY",
      ar_total: "128000.00",
      ar_collected: "96500.00",
      ar_outstanding: "31500.00",
      ap_total: "101400.00",
      ap_paid: "35600.00",
      ap_outstanding: "65800.00",
      net_cashflow: "60900.00",
    };
    const funnel = {
      published_posts: 65,
      total_posts: 85,
      social_leads_captured: 233,
      converted_crm_customers: 36,
      linked_quotes_count: 23,
      lead_to_customer_rate_pct: "15.45",
      platform_breakdown: [],
    };

    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const { url } = normalizeFetchInput(input);
      return json(url.includes("/social/attribution-funnel") ? funnel : summary);
    });
    vi.stubGlobal("fetch", fetchMock);

    const cashflow = await fetchCashflowSummary();
    const attribution = await fetchSocialAttributionFunnel();

    expect(cashflow.net_cashflow).toBe("60900.00");
    expect(attribution.lead_to_customer_rate_pct).toBe("15.45");
    expect(fetchCallUrl(fetchMock.mock.calls[0])).toBe(
      "/api/bff/v1/finance/cashflow-summary"
    );
    expect(fetchCallUrl(fetchMock.mock.calls[1])).toBe(
      "/api/bff/v1/social/attribution-funnel"
    );
  });
});

// ---------------------------------------------------------------------------
// Mock fixtures (NEXT_PUBLIC_USE_MOCK_API=true)
// ---------------------------------------------------------------------------

describe("Pack G fixtures", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
    resetMockApLedger();
  });

  it("seeds an AP ledger whose totals are exact cent sums", async () => {
    const list = await fetchPayables();

    expect(list.currency).toBe("CNY");
    expect(list.items).toHaveLength(4);
    expect(list.items.map((row) => row.status)).toEqual(["partial", "open", "paid", "open"]);
    expect(list.total_due).toBe("101400.00");
    expect(list.total_paid).toBe("35600.00");
    expect(list.total_outstanding).toBe("65800.00");

    const cashflow = await fetchCashflowSummary();
    expect(cashflow.ap_outstanding).toBe("65800.00");
    expect(cashflow.ar_outstanding).toBe("31500.00");
    // net cash flow = money in (AR collected) - money out (AP paid).
    expect(cashflow.net_cashflow).toBe("60900.00");
  });

  it("moves the ledger and the cash-flow summary when a mock settlement lands", async () => {
    const settled = await paySupplierPayable(
      "ap-0002",
      toPayablePaymentPayload("28600.00", "BANK-MOCK-1")
    );

    expect(settled.status).toBe("paid");
    expect(settled.paid_amount).toBe("28600.00");

    const list = await fetchPayables();
    expect(list.total_paid).toBe("64200.00");
    expect(list.total_outstanding).toBe("37200.00");

    const cashflow = await fetchCashflowSummary();
    expect(cashflow.ap_paid).toBe("64200.00");
    expect(cashflow.ap_outstanding).toBe("37200.00");
    expect(cashflow.net_cashflow).toBe("32300.00");
  });

  it("refuses an overpayment and a re-settlement like the server does", async () => {
    await expect(
      paySupplierPayable("ap-0004", toPayablePaymentPayload("999999.00", "BANK-X"))
    ).rejects.toThrow(/超过应付单 PO-2026-1116 的待付余额/);

    // ap-0001 is partially paid: 28000.00 outstanding, so 28000.01 is refused.
    await expect(
      paySupplierPayable("ap-0001", toPayablePaymentPayload("28000.01", "BANK-Y"))
    ).rejects.toThrow(/超过应付单/);

    await expect(
      paySupplierPayable("ap-0003", toPayablePaymentPayload("1.00", "BANK-Z"))
    ).rejects.toThrow(/已结清/);

    // The refused settlements never mutated the ledger.
    const list = await fetchPayables();
    expect(list.total_paid).toBe("35600.00");
  });

  it("derives the funnel rates from the seeded counts", async () => {
    const funnel = await fetchSocialAttributionFunnel();

    expect(funnel.total_posts).toBe(85);
    expect(funnel.published_posts).toBe(65);
    expect(funnel.social_leads_captured).toBe(233);
    expect(funnel.converted_crm_customers).toBe(36);
    expect(funnel.linked_quotes_count).toBe(23);
    expect(funnel.lead_to_customer_rate_pct).toBe("15.45");
    expect(funnel.platform_breakdown).toHaveLength(6);
    expect(
      funnel.platform_breakdown.find((row) => row.platform === "xiaohongshu")
    ).toMatchObject({ published_posts: 17, social_leads_captured: 68, lead_to_customer_rate_pct: "16.18" });
  });
});

// ---------------------------------------------------------------------------
// /finance AP + cash-flow page
// ---------------------------------------------------------------------------

function payableFixtures(): PayableRead[] {
  const base = {
    tenant_id: "22222222-2222-2222-2222-222222222222",
    currency: "CNY",
    created_at: "2026-11-01T02:00:00Z",
    updated_at: "2026-11-01T02:00:00Z",
    po_id: null,
    supplier_id: null,
  };
  return [
    {
      ...base,
      id: "ap-1",
      po_id: "po-1",
      supplier_id: "sup-hc",
      po_no: "PO-2026-1108",
      supplier_name: "深圳市恒彩光电有限公司",
      due_amount: "48000.00",
      paid_amount: "20000.00",
      due_date: "2026-12-05",
      status: "partial",
      payment_ref: "BANK-20261118-0091",
      paid_at: "2026-11-18T07:20:00Z",
    },
    {
      ...base,
      id: "ap-2",
      po_id: "po-2",
      supplier_id: "sup-js",
      po_no: "PO-2026-1112",
      supplier_name: "东莞捷顺五金制品厂",
      due_amount: "28600.00",
      paid_amount: "0.00",
      due_date: "2026-11-28",
      status: "open",
      payment_ref: null,
      paid_at: null,
    },
    {
      ...base,
      id: "ap-3",
      po_id: "po-3",
      supplier_id: "sup-mh",
      po_no: "PO-2026-1091",
      supplier_name: "中山市铭辉电子科技",
      due_amount: "15600.00",
      paid_amount: "15600.00",
      due_date: "2026-11-15",
      status: "paid",
      payment_ref: "BANK-20261110-0044",
      paid_at: "2026-11-10T03:05:00Z",
    },
  ];
}

/** Stateful /finance stub: a settlement really moves the ledger and the summary. */
function stubFinanceApFetch(): ReturnType<typeof vi.fn> {
  let rows = payableFixtures();
  const arCollected = "96500.00";

  const totals = () => {
    const totalDue = rows.reduce((acc, row) => addDecimal(acc, row.due_amount), "0.00");
    const totalPaid = rows.reduce((acc, row) => addDecimal(acc, row.paid_amount), "0.00");
    return {
      total_due: totalDue,
      total_paid: totalPaid,
      total_outstanding: subDecimal(totalDue, totalPaid),
    };
  };

  const fetchMock = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const { url, method } = normalizeFetchInput(input, init);

      if (url.includes("/ai/proposals")) {
        return json({ items: [], total: 0, limit: 1, offset: 0, has_more: false });
      }
      if (url.includes("/finance/overview")) {
        return json({
          summary: {
            total_gross_revenue_base: "52332.00",
            total_net_margin_base: "16280.00",
            avg_margin_pct: "31.11",
            base_currency: "CNY",
          },
          receivables: [],
          cvp_snapshots: [],
        });
      }
      if (url.includes("/finance/cashflow-summary")) {
        const { total_due, total_paid, total_outstanding } = totals();
        return json({
          currency: "CNY",
          ar_total: "128000.00",
          ar_collected: arCollected,
          ar_outstanding: subDecimal("128000.00", arCollected),
          ap_total: total_due,
          ap_paid: total_paid,
          ap_outstanding: total_outstanding,
          net_cashflow: subDecimal(arCollected, total_paid),
        });
      }
      if (url.includes("/finance/payables/") && url.includes("/pay") && method === "POST") {
        const payableId = url.split("/finance/payables/")[1]?.replace(/\/pay$/, "") ?? "";
        const payload = JSON.parse(String(init?.body ?? "{}")) as {
          amount: string;
          payment_ref?: string;
        };
        const target = rows.find((row) => row.id === payableId);
        if (!target) return json({ detail: "not found" }, );
        const paidAmount = addDecimal(target.paid_amount, payload.amount);
        const updated: PayableRead = {
          ...target,
          paid_amount: paidAmount,
          status: compareDecimal(paidAmount, target.due_amount) >= 0 ? "paid" : "partial",
          payment_ref: payload.payment_ref ?? target.payment_ref,
          paid_at: "2026-11-20T02:00:00Z",
          updated_at: "2026-11-20T02:00:00Z",
        };
        rows = rows.map((row) => (row.id === payableId ? updated : row));
        return json(updated);
      }
      if (url.includes("/finance/payables")) {
        return json({ items: rows, ...totals(), currency: "CNY" });
      }
      return json({});
    }
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("/finance 应付账款 AP 与现金流收支闭环", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "false");
  });

  it("renders the 4 cash-flow tiles, the AP ledger and settles a supplier with an exact amount", async () => {
    const user = userEvent.setup();
    const fetchMock = stubFinanceApFetch();

    renderWithProviders(<FinancePage />, "finance");

    // AR milestones + CVP waterfall stay mounted next to the new AP block.
    await waitFor(() => expect(screen.getByTestId("finance-ap-section")).toBeInTheDocument());
    // 应收账款里程碑 + CVP 瀑布的既有 KPI 卡片仍然在页面上。
    await waitFor(() =>
      expect(screen.getByTestId("finance-kpi-revenue")).toHaveTextContent("CN¥52,332.00")
    );
    expect(screen.getByTestId("finance-kpi-net-margin")).toBeInTheDocument();

    // 4 列现金流汇总卡：数值全部是后端 Decimal 字符串。
    await waitFor(() =>
      expect(screen.getByTestId("cashflow-ap-paid")).toHaveTextContent("CN¥35,600.00")
    );
    expect(screen.getByTestId("cashflow-ar-collected")).toHaveTextContent("CN¥96,500.00");
    expect(screen.getByTestId("cashflow-ap-outstanding")).toHaveTextContent("CN¥56,600.00");
    expect(screen.getByTestId("cashflow-net-cashflow")).toHaveTextContent("CN¥60,900.00");
    // No float artefacts, no [object Object] anywhere in the money tiles.
    for (const testId of [
      "cashflow-ar-collected",
      "cashflow-ap-paid",
      "cashflow-ap-outstanding",
      "cashflow-net-cashflow",
    ]) {
      expect(screen.getByTestId(testId).textContent ?? "").not.toContain("0000000");
      expect(screen.getByTestId(testId).textContent ?? "").not.toContain("[object Object]");
    }

    // 应付单列表：采购单号 / 供应商 / 应付已付 / 到期日 / 状态徽标。
    const partialRow = screen.getByTestId("ap-row-PO-2026-1108");
    expect(partialRow).toHaveTextContent("深圳市恒彩光电有限公司");
    expect(partialRow).toHaveTextContent("CN¥48,000.00");
    expect(partialRow).toHaveTextContent("CN¥20,000.00");
    expect(partialRow).toHaveTextContent("2026-12-05");
    expect(screen.getByTestId("ap-status-PO-2026-1108")).toHaveTextContent("部分付款");
    expect(screen.getByTestId("ap-status-PO-2026-1112")).toHaveTextContent("待付");
    expect(screen.getByTestId("ap-status-PO-2026-1091")).toHaveTextContent("已结清");

    const paidRowButton = within(screen.getByTestId("ap-row-PO-2026-1091")).getByTestId(
      "pay-ap-btn"
    );
    expect(paidRowButton).toBeDisabled();

    // Open the inline settlement form on the fully unpaid PO.
    const openRow = screen.getByTestId("ap-row-PO-2026-1112");
    await user.click(within(openRow).getByTestId("pay-ap-btn"));
    const form = await screen.findByTestId("ap-pay-form-PO-2026-1112");
    const amountInput = within(form).getByTestId("pay-ap-amount-input");
    expect(amountInput).toHaveValue("28600.00");

    // An overpayment is refused before any request leaves the browser.
    fireEvent.change(amountInput, { target: { value: "99999.00" } });
    fireEvent.click(within(form).getByTestId("pay-ap-submit"));
    await waitFor(() =>
      expect(screen.getByTestId("finance-ap-feedback")).toHaveTextContent(
        "付款金额不能超过待付余额 CN¥28,600.00"
      )
    );
    expect(
      fetchMock.mock.calls.some((call) =>
        /\/finance\/payables\/[^/]+\/pay$/.test(fetchCallUrl(call))
      )
    ).toBe(false);

    // A legitimate partial settlement posts the exact decimal string.
    fireEvent.change(amountInput, { target: { value: "18600.00" } });
    fireEvent.change(within(form).getByTestId("pay-ap-ref-input"), {
      target: { value: "BANK-TEST-0001" },
    });
    await user.click(within(form).getByTestId("pay-ap-submit"));

    await waitFor(() => {
      const payCall = fetchMock.mock.calls.find(
        (call) =>
          fetchCallUrl(call) === "/api/bff/v1/finance/payables/ap-2/pay" &&
          fetchCallMethod(call) === "POST"
      );
      expect(payCall).toBeDefined();
      const body = JSON.parse(String((payCall?.[1] as RequestInit).body)) as {
        amount: unknown;
        payment_ref: string;
      };
      expect(body.amount).toBe("18600.00");
      expect(body.payment_ref).toBe("BANK-TEST-0001");
    });

    // The list, the row state and the cash flow all refresh right away.
    await waitFor(() =>
      expect(screen.getByTestId("cashflow-ap-paid")).toHaveTextContent("CN¥54,200.00")
    );
    expect(screen.getByTestId("cashflow-ap-outstanding")).toHaveTextContent("CN¥38,000.00");
    expect(screen.getByTestId("cashflow-net-cashflow")).toHaveTextContent("CN¥42,300.00");
    await waitFor(() =>
      expect(screen.getByTestId("ap-row-PO-2026-1112")).toHaveTextContent("CN¥18,600.00")
    );
    expect(screen.getByTestId("ap-status-PO-2026-1112")).toHaveTextContent("部分付款");
    expect(screen.getByTestId("finance-ap-feedback")).toHaveTextContent("PO-2026-1112");
    expect(screen.queryByTestId("ap-pay-form-PO-2026-1112")).not.toBeInTheDocument();
  });

  it("hides the settlement button from roles outside OWNER / FINANCE", async () => {
    stubFinanceApFetch();

    renderWithProviders(<FinancePage />, "manager");

    await waitFor(() => expect(screen.getByTestId("finance-ap-section")).toBeInTheDocument());
    await waitFor(() =>
      expect(screen.getByTestId("ap-row-PO-2026-1108")).toBeInTheDocument()
    );
    expect(screen.queryByTestId("pay-ap-btn")).not.toBeInTheDocument();
    expect(screen.getByTestId("finance-ap-section")).toHaveTextContent("需 OWNER / FINANCE");
    // Reads are still allowed for a manager.
    expect(screen.getByTestId("cashflow-ap-paid")).toHaveTextContent("CN¥35,600.00");
  });
});

// ---------------------------------------------------------------------------
// /social 获客链路看板
// ---------------------------------------------------------------------------

describe("/social 获客链路看板", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
  });

  it("renders the 4-step funnel, the conversion rate and the per-platform table", async () => {
    const user = userEvent.setup();
    renderWithProviders(<SocialPage />);

    // Every existing tab stays available, the attribution tab is added last.
    await screen.findByTestId("social-playbooks-tab");
    await user.click(screen.getByTestId("social-tab-attribution"));

    const board = await screen.findByTestId("social-attribution-board");
    expect(board.textContent ?? "").not.toContain("[object Object]");

    // 4 步归因漏斗卡片：已发布内容 → 线索 → CRM 客户 → 关联报价单。
    await waitFor(() =>
      expect(screen.getByTestId("funnel-step-published")).toHaveTextContent("65")
    );
    expect(screen.getByTestId("funnel-step-published")).toHaveTextContent("内容总数 85 篇");
    expect(screen.getByTestId("funnel-step-leads")).toHaveTextContent("233");
    expect(screen.getByTestId("funnel-step-customers")).toHaveTextContent("36");
    expect(screen.getByTestId("funnel-step-quotes")).toHaveTextContent("23");

    // Visual conversion bars are proportional: captured leads is the widest
    // step (233), so it fills the track and published content (65) is ~28%.
    const barWidth = (testId: string) =>
      screen.getByTestId(testId).querySelector<HTMLElement>("div[style]")?.style.width;
    expect(barWidth("funnel-step-leads")).toBe("100%");
    expect(barWidth("funnel-step-published")).toBe("28%");
    expect(barWidth("funnel-step-customers")).toBe("15%");
    expect(barWidth("funnel-step-quotes")).toBe("10%");

    // 综合转化率来自后端字符串，前端不做浮点重算。
    expect(screen.getByTestId("funnel-conversion-rate")).toHaveTextContent("15.45");
    expect(screen.getByTestId("funnel-conversion-rate")).toHaveTextContent(
      "捕获线索 233 → 转化客户 36"
    );

    // 分平台归因明细表。
    const table = screen.getByTestId("funnel-platform-table");
    const instagram = within(table).getByTestId("funnel-platform-row-instagram");
    expect(instagram).toHaveTextContent("Instagram");
    expect(instagram).toHaveTextContent("14 / 18");
    expect(instagram).toHaveTextContent("42");
    expect(instagram).toHaveTextContent("16.67%");

    const xiaohongshu = within(table).getByTestId("funnel-platform-row-xiaohongshu");
    expect(xiaohongshu).toHaveTextContent("小红书");
    expect(xiaohongshu).toHaveTextContent("17 / 21");
    expect(xiaohongshu).toHaveTextContent("16.18%");

    const douyin = within(table).getByTestId("funnel-platform-row-douyin");
    expect(douyin).toHaveTextContent("抖音");
    expect(douyin).toHaveTextContent("12.77%");

    expect(within(table).getAllByTestId(/^funnel-platform-row-/)).toHaveLength(6);
  });

  it("keeps the previous tabs reachable after switching to the funnel", async () => {
    const user = userEvent.setup();
    renderWithProviders(<SocialPage />);

    await user.click(await screen.findByTestId("social-tab-attribution"));
    await screen.findByTestId("social-attribution-board");

    await user.click(screen.getByTestId("social-playbooks-tab"));
    const playbooks = await screen.findByTestId("social-playbooks-board");
    await waitFor(() =>
      expect(within(playbooks).getAllByTestId(/^playbook-card-/)).toHaveLength(14)
    );
    expect(screen.queryByTestId("social-attribution-board")).not.toBeInTheDocument();
  });
});
