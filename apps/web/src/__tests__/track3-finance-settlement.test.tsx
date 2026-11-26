// Track 3 UI tests: the dual-basis profit panel on /finance - dual-basis KPI cards,
// marketplace settlement CSV import, cross-border payout matching, and the balanced
// accounting voucher export.
//
// Two layers are exercised:
// * the client module in live mode (`bffFetch` real path, URL/query/body asserted
//   against the locked contract) plus its exact `BigInt` money arithmetic;
// * `<SettlementReconciliationPanel />` in mock mode, where the mutable in-memory
//   ledger really moves when a statement is imported or a payout is matched.
//
// Money assertions are exact decimal strings: `0.30000000000000004`, `6.12`
// instead of `6.13` (ROUND_HALF_EVEN), or a recomputed cent would fail immediately.
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { SettlementReconciliationPanel } from "@/components/finance/settlement-reconciliation-panel";
import { I18nProvider } from "@/lib/i18n";
import type { DualBasisProfitRead } from "@/lib/api-client/types.gen";
import {
  applyFx,
  autoMatchPayouts,
  csvSafeCell,
  fetchDualBasisProfit,
  fetchPayoutStatements,
  fetchSettlementBatches,
  formatExactMoney,
  formatRateE6,
  formatSignedMoney,
  impliedRateE6,
  importPayoutStatements,
  importSettlementCsv,
  journalExportHref,
  parseRateToE6,
  payoutsFromCsv,
  resetMockSettlementLedger,
  SAMPLE_PAYOUT_CSV,
  SAMPLE_SETTLEMENT_CSV,
  SAMPLE_SETTLEMENT_HEADER,
} from "@/lib/api-client/track3-settlement";
import { fetchCallBody, fetchCallMethod, fetchCallUrl, normalizeFetchInput } from "@/__tests__/fetch-stub";

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function renderPanel(role: "owner" | "finance" | "admin" | "manager" | "agent") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <I18nProvider defaultLocale="zh">
        <SettlementReconciliationPanel role={role} />
      </I18nProvider>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  resetMockSettlementLedger();
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Exact decimal arithmetic (no IEEE-754 anywhere)
// ---------------------------------------------------------------------------
describe("track3 exact decimal helpers", () => {
  it("rounds an FX multiplication half-up at the cent", () => {
    // 12.25 USD * 0.5 = 6.125 -> ROUND_HALF_UP 6.13 (ROUND_HALF_EVEN would say 6.12).
    expect(applyFx(1225n, parseRateToE6("0.500000"))).toBe(613n);
    expect(formatExactMoney("6.13", "CNY")).toBe("CN¥6.13");
    // A pure binary float would print 0.30000000000000004 here.
    expect(applyFx(10n, parseRateToE6("3.000000"))).toBe(30n);
    expect(applyFx(7n, parseRateToE6("7.100000"))).toBe(50n);
  });

  it("parses and formats six-decimal rates exactly", () => {
    expect(parseRateToE6("7.1")).toBe(7_100_000n);
    expect(parseRateToE6("0.500000")).toBe(500_000n);
    expect(formatRateE6(7_100_000n)).toBe("7.100000");
    expect(formatRateE6(impliedRateE6(82_355n, 11_550n))).toBe("7.130303");
  });

  it("formats base money and signed deltas without float conversion", () => {
    expect(formatExactMoney("816.50", "CNY")).toBe("CN¥816.50");
    expect(formatExactMoney("1633.00", "CNY")).toBe("CN¥1,633.00");
    expect(formatExactMoney("-2.60", "CNY")).toBe("-CN¥2.60");
    expect(formatSignedMoney("9.55", "CNY")).toBe("+CN¥9.55");
    expect(formatSignedMoney("-2.60", "CNY")).toBe("-CN¥2.60");
    expect(formatSignedMoney("0.00", "CNY")).toBe("CN¥0.00");
  });

  it("neutralises CSV formula cells but keeps genuine negative amounts", () => {
    expect(csvSafeCell("-120.00")).toBe("-120.00");
    expect(csvSafeCell("1065.00")).toBe("1065.00");
    expect(csvSafeCell("=HYPERLINK(\"http://evil\")")).toMatch(/^'=/);
    expect(csvSafeCell("@BATCH-1")).toMatch(/^'@/);
    expect(csvSafeCell("Sales Revenue")).toBe("Sales Revenue");
  });

  it("parses a payout CSV into exact string rows", () => {
    const rows = payoutsFromCsv(SAMPLE_PAYOUT_CSV);
    expect(rows).toHaveLength(1);
    expect(rows[0].payout_amount).toBe("115.50");
    expect(rows[0].settled_base_amount).toBe("823.55");
    expect(rows[0].expected_fx_rate).toBe("7.100000");
    expect(typeof rows[0].payout_amount).toBe("string");
  });
});

// ---------------------------------------------------------------------------
// Live client module (real bffFetch path)
// ---------------------------------------------------------------------------
describe("track3 live client", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "false");
  });

  it("imports a settlement as a raw text/csv body with the header in the query", async () => {
    const batch = {
      id: "batch-1",
      tenant_id: "t1",
      channel_type: "amazon_sp",
      channel_account_id: null,
      batch_ref: "AMZ-SET-202611",
      currency: "USD",
      period_start: "2026-11-01",
      period_end: "2026-11-30",
      gross_sales_ccy: "150.00",
      commission_ccy: "23.00",
      fulfillment_fee_ccy: "6.00",
      storage_fee_ccy: "1.50",
      ad_fee_ccy: "4.50",
      refund_ccy: "0.00",
      net_payout_ccy: "115.00",
      fx_rate_to_base: "7.100000",
      net_payout_base: "816.50",
      status: "imported",
      line_count: 2,
      created_at: "2026-11-24T02:00:00Z",
    };
    const fetchMock = vi.fn(async () => json(batch));
    vi.stubGlobal("fetch", fetchMock);

    const result = await importSettlementCsv(SAMPLE_SETTLEMENT_HEADER, SAMPLE_SETTLEMENT_CSV);

    expect(result.net_payout_base).toBe("816.50");
    const call = fetchMock.mock.calls[0] as unknown[];
    const url = fetchCallUrl(call);
    expect(url).toContain("/api/bff/v1/finance/settlements/import?");
    expect(url).toContain("channel_type=amazon_sp");
    expect(url).toContain("batch_ref=AMZ-SET-202611");
    expect(url).toContain("period_start=2026-11-01");
    expect(url).toContain("fx_rate_to_base=7.100000");
    expect(fetchCallMethod(call)).toBe("POST");
    const body = await fetchCallBody(call);
    expect(body.split("\n")[0]).toContain("external_order_id");
    expect(body).toContain("111-2223334-5556667");
  });

  it("reads the dual-basis report, the statements and the payouts from the locked paths", async () => {
    const report: DualBasisProfitRead = {
      base_currency: "CNY",
      estimated_revenue_base: "1065.00",
      estimated_net_margin_base: "356.95",
      settled_payout_base: "816.50",
      settled_net_profit_base: "366.50",
      commission_delta_base: "3.55",
      fulfillment_storage_delta_base: "3.25",
      ad_spend_base: "31.95",
      fx_variance_base: "0.00",
      total_profit_variance_base: "9.55",
      matched_orders_count: 2,
      unmatched_lines_count: 0,
      rows: [],
    };
    const urls: string[] = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const { url } = normalizeFetchInput(input, init);
      urls.push(url);
      if (url.includes("/profit/dual-basis")) return json(report);
      if (url.includes("/payouts")) {
        return json({
          items: [],
          total_count: 0,
          matched_count: 0,
          unmatched_count: 0,
          total_fx_variance_base: "0.00",
        });
      }
      return json({
        items: [],
        total_count: 0,
        total_net_payout_base: "0.00",
        base_currency: "CNY",
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    expect((await fetchDualBasisProfit()).total_profit_variance_base).toBe("9.55");
    expect((await fetchSettlementBatches()).base_currency).toBe("CNY");
    const payouts = await fetchPayoutStatements("unmatched");

    expect(urls).toContain("/api/bff/v1/finance/profit/dual-basis");
    expect(urls).toContain("/api/bff/v1/finance/settlements");
    expect(urls).toContain("/api/bff/v1/finance/payouts?match_status=unmatched");
    expect(payouts.unmatched_count).toBe(0);
  });

  it("builds the journal export URL and posts payouts as JSON", async () => {
    expect(journalExportHref("quickbooks")).toBe(
      "/api/bff/v1/finance/journal-export?format=quickbooks"
    );
    const fetchMock = vi.fn(async () =>
      json({ imported_count: 1, skipped_count: 0, matched_count: 0, items: [] })
    );
    vi.stubGlobal("fetch", fetchMock);
    await importPayoutStatements({
      provider: "lianlian",
      rows: [
        {
          external_tx_ref: "LL-1",
          payout_currency: "USD",
          payout_amount: "999.00",
          settled_base_currency: "CNY",
          settled_base_amount: "7092.90",
          expected_fx_rate: "7.100000",
        },
      ],
    });
    const call = fetchMock.mock.calls[0] as unknown[];
    expect(fetchCallUrl(call)).toBe("/api/bff/v1/finance/payouts/import");
    expect(fetchCallMethod(call)).toBe("POST");
    const body = JSON.parse(await fetchCallBody(call)) as {
      provider: string;
      rows: Array<{ payout_amount: unknown }>;
    };
    expect(body.provider).toBe("lianlian");
    // A JSON number here would be rejected by the backend's ExactDecimal.
    expect(typeof body.rows[0].payout_amount).toBe("string");
  });
});

// ---------------------------------------------------------------------------
// Panel: dual-basis cards
// ---------------------------------------------------------------------------
describe("SettlementReconciliationPanel dual-basis cards", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
  });

  it("renders the estimated, settled and variance KPIs from exact decimal strings", async () => {
    renderPanel("finance");

    await waitFor(() =>
      expect(screen.getByTestId("dual-basis-estimated-net")).toHaveTextContent("CN¥356.95")
    );
    expect(screen.getByTestId("dual-basis-settled-payout")).toHaveTextContent("CN¥816.50");
    expect(screen.getByTestId("dual-basis-settled-net")).toHaveTextContent("CN¥366.50");
    expect(screen.getByTestId("dual-basis-variance")).toHaveTextContent("+CN¥9.55");
    expect(screen.getByTestId("dual-basis-commission-delta")).toHaveTextContent("+CN¥3.55");
    expect(screen.getByTestId("dual-basis-fulfillment-delta")).toHaveTextContent("+CN¥3.25");
    expect(screen.getByTestId("dual-basis-ad-spend")).toHaveTextContent("CN¥31.95");

    const rowA = screen.getByTestId("dual-basis-row-AMZ-2001");
    expect(rowA).toHaveTextContent("AMZ-2001");
    expect(rowA).toHaveTextContent("CN¥710.00");
    expect(rowA).toHaveTextContent("+CN¥7.10");
    expect(rowA).toHaveTextContent("-CN¥2.60");
    const rowB = screen.getByTestId("dual-basis-row-AMZ-2002");
    expect(rowB).toHaveTextContent("-CN¥3.55");

    for (const testId of [
      "dual-basis-estimated-net",
      "dual-basis-settled-payout",
      "dual-basis-settled-net",
      "dual-basis-variance",
    ]) {
      const text = screen.getByTestId(testId).textContent ?? "";
      expect(text).not.toContain("0000000");
      expect(text).not.toContain("[object Object]");
    }
  });

  it("is hidden for an agent, because the server refuses that role with 403", () => {
    renderPanel("agent");
    expect(screen.queryByTestId("settlement-reconciliation-panel")).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Panel: settlement CSV import
// ---------------------------------------------------------------------------
describe("SettlementReconciliationPanel settlement import", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
  });

  it("prefills the sample statement and moves the settled payout after import", async () => {
    const user = userEvent.setup();
    renderPanel("finance");

    await waitFor(() =>
      expect(screen.getByTestId("dual-basis-settled-payout")).toHaveTextContent("CN¥816.50")
    );

    await user.click(screen.getByTestId("settlement-tab-import"));
    await user.click(screen.getByTestId("settlement-sample-fill"));

    const csvInput = screen.getByTestId("settlement-csv-input") as HTMLTextAreaElement;
    expect(csvInput.value.split("\n")).toHaveLength(3);
    expect(csvInput.value).toContain("111-2223334-5556667");

    // A second statement period so the import creates a new batch instead of
    // short-circuiting on the idempotency key.
    fireEvent.change(screen.getByTestId("settlement-header-ref"), {
      target: { value: "AMZ-SET-202612" },
    });
    await user.click(screen.getByTestId("settlement-import-submit"));

    await waitFor(() =>
      expect(screen.getByTestId("settlement-feedback")).toHaveTextContent("AMZ-SET-202612")
    );
    expect(screen.getByTestId("settlement-feedback")).toHaveTextContent("已导入");
    expect(screen.getByTestId("settlement-batch-AMZ-SET-202612")).toHaveTextContent("115.00");

    // The demo ledger pays one statement twice, so the settled payout doubles while
    // every order keeps a single COGS snapshot.
    await user.click(screen.getByTestId("settlement-tab-dual-basis"));
    await waitFor(() =>
      expect(screen.getByTestId("dual-basis-settled-payout")).toHaveTextContent("CN¥1,633.00")
    );
    expect(screen.getByTestId("dual-basis-estimated-net")).toHaveTextContent("CN¥356.95");
  });

  it("shows the settlement rows with their auto-linked orders", async () => {
    const user = userEvent.setup();
    renderPanel("owner");

    await user.click(await screen.findByTestId("settlement-tab-import"));
    const batchRow = await screen.findByTestId("settlement-batch-AMZ-SET-202611");
    expect(batchRow).toHaveTextContent("amazon_sp");
    expect(batchRow).toHaveTextContent("2 行");
    await user.click(batchRow);

    await waitFor(() => expect(screen.getByText("结算明细（订单自动回挂）")).toBeInTheDocument());
    const matched = screen.getAllByText("已匹配");
    expect(matched.length).toBeGreaterThanOrEqual(2);
  });

  it("disables the import button for a manager (read-only role)", async () => {
    const user = userEvent.setup();
    renderPanel("manager");
    await user.click(await screen.findByTestId("settlement-tab-import"));
    expect(screen.getByTestId("settlement-import-submit")).toBeDisabled();
    expect(screen.getByTestId("settlement-import")).toHaveTextContent("需 OWNER / FINANCE / ADMIN");
  });
});

// ---------------------------------------------------------------------------
// Panel: payout matching
// ---------------------------------------------------------------------------
describe("SettlementReconciliationPanel payout matching", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
  });

  it("lists the payout with its exact FX rate and variance, then matches it", async () => {
    const user = userEvent.setup();
    renderPanel("finance");

    await user.click(await screen.findByTestId("settlement-tab-payout"));
    const row = await screen.findByTestId("payout-row-PP-20261125-0001");
    // 823.55 / 115.50 = 7.130303 (6 dp, ROUND_HALF_UP)
    expect(row).toHaveTextContent("7.130303");
    expect(row).toHaveTextContent("7.100000");
    expect(row).toHaveTextContent("+CN¥3.50");
    expect(row).toHaveTextContent("PingPong");
    expect(within(row).getByText("待匹配")).toBeInTheDocument();

    await user.click(screen.getByTestId("payout-auto-match"));

    await waitFor(() =>
      expect(screen.getByTestId("settlement-feedback")).toHaveTextContent("本次匹配 1 笔")
    );
    const matchedRow = await screen.findByTestId("payout-row-PP-20261125-0001");
    expect(within(matchedRow).getByText("已匹配结算单")).toBeInTheDocument();
    expect(screen.getByTestId("payout-matched-count")).toHaveTextContent("已匹配 1 / 1");
  });

  it("skips a duplicate payout reference on import", async () => {
    const user = userEvent.setup();
    renderPanel("finance");

    await user.click(await screen.findByTestId("settlement-tab-payout"));
    await user.click(screen.getByTestId("payout-sample-fill"));
    await user.click(screen.getByTestId("payout-import-submit"));

    await waitFor(() =>
      expect(screen.getByTestId("settlement-feedback")).toHaveTextContent("跳过 1 笔重复")
    );
  });

  it("reports a malformed payout CSV instead of posting it", async () => {
    const user = userEvent.setup();
    renderPanel("finance");

    await user.click(await screen.findByTestId("settlement-tab-payout"));
    fireEvent.change(screen.getByTestId("payout-csv-input"), {
      target: { value: "only_one_column\n1,2" },
    });
    await user.click(screen.getByTestId("payout-import-submit"));

    await waitFor(() =>
      expect(screen.getByTestId("settlement-feedback")).toHaveTextContent("缺少列")
    );
  });
});

// ---------------------------------------------------------------------------
// Panel: journal export
// ---------------------------------------------------------------------------
describe("SettlementReconciliationPanel journal export", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
  });

  it("downloads a balanced voucher CSV in all four dialects", async () => {
    // Reconciling the seeded payout turns the statement into a voucher source.
    await autoMatchPayouts();
    const user = userEvent.setup();
    renderPanel("finance");

    await user.click(await screen.findByTestId("settlement-tab-journal"));

    const standardHref = screen.getByTestId("journal-download").getAttribute("href") ?? "";
    expect(standardHref.startsWith("data:text/csv;charset=utf-8,")).toBe(true);
    const standardCsv = decodeURIComponent(standardHref.split(",")[1] ?? "");
    const standardRows = standardCsv.trim().split("\n");
    expect(standardRows[0]).toContain("debit_base");
    const debitTotal = standardRows
      .slice(1)
      .reduce((acc, line) => acc + Math.round(Number(line.split(",")[6]) * 100), 0);
    const creditTotal = standardRows
      .slice(1)
      .reduce((acc, line) => acc + Math.round(Number(line.split(",")[7]) * 100), 0);
    expect(debitTotal).toBe(creditTotal);
    expect(debitTotal).toBe(258350);
    expect(standardCsv).toContain("Marketplace Clearing");
    expect(standardCsv).toContain("FX Gain / Loss");

    await user.click(screen.getByTestId("journal-format-quickbooks"));
    const quickbooksCsv = decodeURIComponent(
      (screen.getByTestId("journal-download").getAttribute("href") ?? "").split(",")[1] ?? ""
    );
    expect(quickbooksCsv.split("\n")[0]).toContain("Journal No");

    await user.click(screen.getByTestId("journal-format-xero"));
    const xeroCsv = decodeURIComponent(
      (screen.getByTestId("journal-download").getAttribute("href") ?? "").split(",")[1] ?? ""
    );
    expect(xeroCsv.split("\n")[0]).toContain("Narration");

    await user.click(screen.getByTestId("journal-format-kingdee"));
    const kingdeeCsv = decodeURIComponent(
      (screen.getByTestId("journal-download").getAttribute("href") ?? "").split(",")[1] ?? ""
    );
    expect(kingdeeCsv.split("\n")[0]).toContain("借方本位币");
  });
});
