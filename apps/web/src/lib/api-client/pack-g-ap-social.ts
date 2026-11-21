// Pack G client module: supplier accounts-payable (AP) settlement together with
// the AR/AP cash-flow summary, plus the social -> CRM -> quote attribution funnel.
//
// Same rules as the other pack modules:
// * every request travels through `bffFetch` (`/api/bff/v1/...`), so the session
//   cookie, the 401-refresh flow and the BFF role gate behave exactly like the
//   rest of the workbench;
// * the browser only ever sees the typed `/api/v1/finance` + `/api/v1/social`
//   contract — zero SQL, zero ORM, no supplier banking credentials;
// * money is never computed in JavaScript floating point. The backend already
//   returns `decimal.Decimal` serialised as strings; the few additions this
//   module performs (mock ledger totals, outstanding balances) run over integer
//   cents with `BigInt`, and an operator-entered amount is normalised back to a
//   two-decimal string before it is sent, so a `float` never reaches
//   `PayablePaymentRequest.amount`;
// * mock mode (`NEXT_PUBLIC_USE_MOCK_API=true`) never reaches the network. The
//   fixtures below mirror the `--with-demo-data` seed so the screens stay
//   reviewable without a backend, and they are mutable so a mock settlement
//   really moves the ledger and the cash-flow summary.
import { bffFetch } from "@/lib/api";
import { shouldUseMockApi } from "@/lib/data-source";
import type {
  CashflowSummaryRead,
  PayableListRead,
  PayablePaymentRequest,
  PayableRead,
  SocialAttributionFunnelRead,
  SocialPlatformFunnelItem,
} from "@/lib/api-client/types.gen";

// ---------------------------------------------------------------------------
// Exact decimal money (no IEEE-754 arithmetic anywhere in this module)
// ---------------------------------------------------------------------------

const MONEY_SHAPE = /^-?\d+(\.\d{1,2})?$/;

/**
 * Parse a decimal money string into integer cents.
 *
 * `null` means "not a money string" (empty, units, more than two decimals); the
 * caller decides whether that is a hard error or a defensive zero.
 */
export function parseMoneyToCents(value: string | number | null | undefined): bigint | null {
  if (value === null || value === undefined) return null;
  const raw = String(value).replace(/,/g, "").trim();
  if (!MONEY_SHAPE.test(raw)) return null;
  const negative = raw.startsWith("-");
  const unsigned = negative ? raw.slice(1) : raw;
  const [whole, fraction = ""] = unsigned.split(".");
  const cents = BigInt(whole) * 100n + BigInt(fraction.padEnd(2, "0"));
  return negative ? -cents : cents;
}

/** Render integer cents back as the ledger's `NUMERIC(14, 2)` string form. */
export function formatCents(cents: bigint): string {
  const negative = cents < 0n;
  const abs = negative ? -cents : cents;
  return `${negative ? "-" : ""}${abs / 100n}.${(abs % 100n).toString().padStart(2, "0")}`;
}

/** Exact `a + b` for two money strings; an unparsable operand counts as `0.00`. */
export function addDecimal(a: string, b: string): string {
  return formatCents((parseMoneyToCents(a) ?? 0n) + (parseMoneyToCents(b) ?? 0n));
}

/** Exact `a - b` for two money strings; an unparsable operand counts as `0.00`. */
export function subDecimal(a: string, b: string): string {
  return formatCents((parseMoneyToCents(a) ?? 0n) - (parseMoneyToCents(b) ?? 0n));
}

/** `-1 | 0 | 1` — exact comparison, so an overpayment is caught before the POST. */
export function compareDecimal(a: string, b: string): number {
  const left = parseMoneyToCents(a) ?? 0n;
  const right = parseMoneyToCents(b) ?? 0n;
  if (left === right) return 0;
  return left > right ? 1 : -1;
}

/** Outstanding balance of one payable: `due_amount - paid_amount`, exactly. */
export function payableOutstanding(payable: {
  due_amount: string;
  paid_amount: string;
}): string {
  return subDecimal(payable.due_amount, payable.paid_amount);
}

/**
 * Percentage with 2 dp, `ROUND_HALF_UP`, matching the backend's `_pct` helper.
 * Integer division over `BigInt` keeps it exact; an empty funnel is `"0.00"`.
 */
export function pctString(numerator: number, denominator: number): string {
  if (!Number.isFinite(numerator) || !Number.isFinite(denominator)) return "0.00";
  if (denominator <= 0) return "0.00";
  const n = BigInt(Math.max(Math.round(numerator), 0)) * 10_000n;
  const d = BigInt(Math.round(denominator));
  const rounded = (n * 2n + d) / (d * 2n);
  return `${rounded / 100n}.${(rounded % 100n).toString().padStart(2, "0")}`;
}

/**
 * Operator input -> `PayablePaymentRequest`.
 *
 * The amount is re-serialised as an exact two-decimal string: the generated
 * contract accepts `number | string`, but the backend's `ExactDecimal` rejects a
 * JSON float, so nothing here may pass `1234.5` through untouched.
 */
export function toPayablePaymentPayload(
  amount: string,
  paymentRef: string
): PayablePaymentRequest {
  const cents = parseMoneyToCents(amount);
  if (cents === null || cents <= 0n) {
    throw new Error("付款金额必须是大于 0 的两位小数。");
  }
  return { amount: formatCents(cents), payment_ref: paymentRef.trim() };
}

// ---------------------------------------------------------------------------
// Live calls
// ---------------------------------------------------------------------------

/**
 * `GET /v1/finance/payables` — the tenant AP ledger.
 *
 * The backend auto-books an `open` payable for every purchase order that has no
 * AP document yet, so this list is the live PO -> AP sync, not a manual ledger.
 */
export function fetchPayables(): Promise<PayableListRead> {
  if (shouldUseMockApi()) return Promise.resolve(mockPayableList());
  return bffFetch<PayableListRead>("/v1/finance/payables");
}

/**
 * `POST /v1/finance/payables/{payable_id}/pay` — L0 supplier settlement.
 *
 * Role gate is server-side (`AP_PAY_ROLES = OWNER, FINANCE`) and the server also
 * refuses an overpayment (`ERR_PAYABLE_OVERPAYMENT`) and a re-settlement
 * (`ERR_PAYABLE_ALREADY_PAID`); the UI gate is only a convenience.
 */
export function paySupplierPayable(
  payableId: string,
  payload: PayablePaymentRequest
): Promise<PayableRead> {
  if (shouldUseMockApi()) return mockRecordPayment(payableId, payload);
  return bffFetch<PayableRead>(
    `/v1/finance/payables/${encodeURIComponent(payableId)}/pay`,
    { method: "POST", body: JSON.stringify(payload) }
  );
}

/**
 * `GET /v1/finance/cashflow-summary` — AR/AP position and net cash flow.
 *
 * `net_cashflow = ar_collected - ap_paid`; every field is a base-currency
 * decimal string produced by SQL, never recomputed in the browser.
 */
export function fetchCashflowSummary(): Promise<CashflowSummaryRead> {
  if (shouldUseMockApi()) return Promise.resolve(mockCashflowSummary());
  return bffFetch<CashflowSummaryRead>("/v1/finance/cashflow-summary");
}

/**
 * `GET /v1/social/attribution-funnel` — social posts -> leads -> CRM -> quotes.
 *
 * Deterministic SQL attribution (mention platform first, then the lead's own
 * `source_channel`), so the funnel and `lead_to_customer_rate_pct` are tenant
 * truth rather than a client-side estimate.
 */
export function fetchSocialAttributionFunnel(): Promise<SocialAttributionFunnelRead> {
  if (shouldUseMockApi()) return Promise.resolve(cloneFunnel(mockAttributionFunnel()));
  return bffFetch<SocialAttributionFunnelRead>("/v1/social/attribution-funnel");
}

// ---------------------------------------------------------------------------
// Mock fixtures (mirror the `--with-demo-data` seed)
// ---------------------------------------------------------------------------

const MOCK_TENANT_ID = "22222222-2222-2222-2222-222222222222";
const MOCK_BASE_CURRENCY = "CNY";
/** Base-currency receivables already collected / booked, as the seed books them. */
const MOCK_AR = { total: "128000.00", collected: "96500.00" } as const;

function buildMockPayables(): PayableRead[] {
  const rows: Array<Omit<PayableRead, "tenant_id" | "created_at" | "updated_at">> = [
    {
      id: "ap-0001",
      po_id: "po-0001",
      supplier_id: "sup-hc",
      po_no: "PO-2026-1108",
      supplier_name: "深圳市恒彩光电有限公司",
      due_amount: "48000.00",
      paid_amount: "20000.00",
      currency: MOCK_BASE_CURRENCY,
      due_date: "2026-12-05",
      status: "partial",
      payment_ref: "BANK-20261118-0091",
      paid_at: "2026-11-18T07:20:00Z",
    },
    {
      id: "ap-0002",
      po_id: "po-0002",
      supplier_id: "sup-js",
      po_no: "PO-2026-1112",
      supplier_name: "东莞捷顺五金制品厂",
      due_amount: "28600.00",
      paid_amount: "0.00",
      currency: MOCK_BASE_CURRENCY,
      due_date: "2026-11-28",
      status: "open",
      payment_ref: null,
      paid_at: null,
    },
    {
      id: "ap-0003",
      po_id: "po-0003",
      supplier_id: "sup-mh",
      po_no: "PO-2026-1091",
      supplier_name: "中山市铭辉电子科技",
      due_amount: "15600.00",
      paid_amount: "15600.00",
      currency: MOCK_BASE_CURRENCY,
      due_date: "2026-11-15",
      status: "paid",
      payment_ref: "BANK-20261110-0044",
      paid_at: "2026-11-10T03:05:00Z",
    },
    {
      id: "ap-0004",
      po_id: "po-0004",
      supplier_id: "sup-xd",
      po_no: "PO-2026-1116",
      supplier_name: "惠州信达铝业有限公司",
      due_amount: "9200.00",
      paid_amount: "0.00",
      currency: MOCK_BASE_CURRENCY,
      due_date: "2026-12-18",
      status: "open",
      payment_ref: null,
      paid_at: null,
    },
  ];

  return rows.map((row, index) => ({
    ...row,
    tenant_id: MOCK_TENANT_ID,
    created_at: `2026-11-0${index + 1}T02:00:00Z`,
    updated_at: row.paid_at ?? `2026-11-0${index + 1}T02:00:00Z`,
  }));
}

// Mutable on purpose: a mock settlement has to move the ledger *and* the
// cash-flow summary, exactly like the live POST does.
let mockPayableRows: PayableRead[] = buildMockPayables();

/** Restore the seeded AP ledger (tests call this between cases). */
export function resetMockApLedger(): void {
  mockPayableRows = buildMockPayables();
}

function summariseLedger(rows: PayableRead[], currency: string): PayableListRead {
  const scoped = rows.filter((row) => row.currency.toUpperCase() === currency.toUpperCase());
  const total_due = scoped.reduce((acc, row) => addDecimal(acc, row.due_amount), "0.00");
  const total_paid = scoped.reduce((acc, row) => addDecimal(acc, row.paid_amount), "0.00");
  return {
    items: rows.map((row) => ({ ...row })),
    total_due,
    total_paid,
    total_outstanding: subDecimal(total_due, total_paid),
    currency,
  };
}

function mockPayableList(): PayableListRead {
  return summariseLedger(mockPayableRows, MOCK_BASE_CURRENCY);
}

function mockCashflowSummary(): CashflowSummaryRead {
  const { total_due, total_paid, total_outstanding } = summariseLedger(
    mockPayableRows,
    MOCK_BASE_CURRENCY
  );
  return {
    currency: MOCK_BASE_CURRENCY,
    ar_total: MOCK_AR.total,
    ar_collected: MOCK_AR.collected,
    ar_outstanding: subDecimal(MOCK_AR.total, MOCK_AR.collected),
    ap_total: total_due,
    ap_paid: total_paid,
    ap_outstanding: total_outstanding,
    // Net cash flow the backend computes: money in (AR collected) minus money
    // out (AP paid) - never `ar_total - ap_total`, which would count unpaid money.
    net_cashflow: subDecimal(MOCK_AR.collected, total_paid),
  };
}

/**
 * Mirror `PayableService.record_payment`: `open -> partial -> paid`, never an
 * overpayment, never a re-settlement, all in exact cents. Async so a rejected
 * settlement surfaces as a rejected promise, exactly like the live POST.
 */
async function mockRecordPayment(
  payableId: string,
  payload: PayablePaymentRequest
): Promise<PayableRead> {
  const index = mockPayableRows.findIndex((row) => row.id === payableId);
  if (index < 0) {
    throw new Error(`应付单 ${payableId} 不存在。`);
  }
  const row = mockPayableRows[index];
  const amountCents = parseMoneyToCents(payload.amount);
  if (amountCents === null || amountCents <= 0n) {
    throw new Error("付款金额必须是大于 0 的两位小数。");
  }
  const outstanding = payableOutstanding(row);
  if (row.status === "paid" || compareDecimal(outstanding, "0.00") <= 0) {
    throw new Error(`应付单 ${row.po_no} 已结清，不能重复付款。`);
  }
  if (compareDecimal(formatCents(amountCents), outstanding) > 0) {
    throw new Error(`付款金额超过应付单 ${row.po_no} 的待付余额 ${outstanding}。`);
  }

  const paidAmount = addDecimal(row.paid_amount, formatCents(amountCents));
  const settled = compareDecimal(paidAmount, row.due_amount) >= 0;
  const paidAt = payload.paid_at ?? new Date().toISOString();
  const updated: PayableRead = {
    ...row,
    paid_amount: paidAmount,
    status: settled ? "paid" : "partial",
    payment_ref: payload.payment_ref?.trim() || row.payment_ref,
    paid_at: paidAt,
    updated_at: paidAt,
  };
  mockPayableRows = mockPayableRows.map((item, i) => (i === index ? updated : item));
  return { ...updated };
}

/**
 * Seeded funnel: overseas 4 + domestic 2 platforms with real posts, captured
 * leads and converted CRM customers, so every rate below is derived, not typed.
 */
const MOCK_FUNNEL_PLATFORM_SEED: ReadonlyArray<
  Omit<SocialPlatformFunnelItem, "lead_to_customer_rate_pct">
> = [
  { platform: "instagram", total_posts: 18, published_posts: 14, social_leads_captured: 42, converted_crm_customers: 7 },
  { platform: "tiktok", total_posts: 16, published_posts: 12, social_leads_captured: 55, converted_crm_customers: 9 },
  { platform: "facebook", total_posts: 9, published_posts: 7, social_leads_captured: 12, converted_crm_customers: 2 },
  { platform: "youtube", total_posts: 6, published_posts: 4, social_leads_captured: 9, converted_crm_customers: 1 },
  { platform: "xiaohongshu", total_posts: 21, published_posts: 17, social_leads_captured: 68, converted_crm_customers: 11 },
  { platform: "douyin", total_posts: 15, published_posts: 11, social_leads_captured: 47, converted_crm_customers: 6 },
];

const MOCK_LINKED_QUOTES_COUNT = 23;

function mockAttributionFunnel(): SocialAttributionFunnelRead {
  const platform_breakdown: SocialPlatformFunnelItem[] = MOCK_FUNNEL_PLATFORM_SEED.map(
    (row) => ({
      ...row,
      lead_to_customer_rate_pct: pctString(
        row.converted_crm_customers,
        row.social_leads_captured
      ),
    })
  );
  const sum = (pick: (row: SocialPlatformFunnelItem) => number) =>
    platform_breakdown.reduce((acc, row) => acc + pick(row), 0);
  const social_leads_captured = sum((row) => row.social_leads_captured);
  const converted_crm_customers = sum((row) => row.converted_crm_customers);

  return {
    published_posts: sum((row) => row.published_posts),
    total_posts: sum((row) => row.total_posts),
    social_leads_captured,
    converted_crm_customers,
    linked_quotes_count: MOCK_LINKED_QUOTES_COUNT,
    lead_to_customer_rate_pct: pctString(converted_crm_customers, social_leads_captured),
    platform_breakdown,
  };
}

function cloneFunnel(funnel: SocialAttributionFunnelRead): SocialAttributionFunnelRead {
  return {
    ...funnel,
    platform_breakdown: funnel.platform_breakdown.map((row) => ({ ...row })),
  };
}
