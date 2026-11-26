// Track 3 client module: dual-basis profit reconciliation, marketplace settlement
// import, cross-border payout matching, and the accounting voucher export.
//
// Same rules as every other pack module:
// * requests travel through `bffFetch` (`/api/bff/v1/...`), so the session cookie,
//   the 401-refresh flow and the BFF role gate behave like the rest of the workbench;
// * the browser only sees the typed `/api/v1/finance` contract - zero SQL, zero ORM;
// * money never becomes a JavaScript float. The backend serialises `Decimal` as
//   strings; this module only re-formats them (integer cents / six-decimal rates
//   over `BigInt`), and the mock ledger runs the very same `BigInt` arithmetic, so
//   a cents-level disagreement shows up immediately in a test rather than as
//   `0.30000000000000004`;
// * mock mode (`NEXT_PUBLIC_USE_MOCK_API=true`) never touches the network, and the
//   fixture ledger is mutable so an imported statement really moves the dual-basis
//   report and the payout matcher.
import { bffFetch } from "@/lib/api";
import { shouldUseMockApi } from "@/lib/data-source";
import { formatCents, parseMoneyToCents } from "@/lib/api-client/pack-g-ap-social";
import type {
  DualBasisOrderRow,
  DualBasisProfitRead,
  ImportPayoutsApiV1FinancePayoutsImportPostData,
  ImportSettlementsApiV1FinanceSettlementsImportPostData,
  PayoutAutoMatchResultRead,
  PayoutImportResultRead,
  PayoutStatementListRead,
  PayoutStatementRead,
  SettlementBatchListRead,
  SettlementBatchRead,
  SettlementLineListRead,
  SettlementLineRead,
} from "@/lib/api-client/types.gen";

/** Body of `POST /v1/finance/settlements/import` (JSON branch). */
export type SettlementImportPayload =
  ImportSettlementsApiV1FinanceSettlementsImportPostData["body"];
/** Body of `POST /v1/finance/payouts/import` (JSON branch). */
export type PayoutImportPayload = ImportPayoutsApiV1FinancePayoutsImportPostData["body"];
/** One payout statement row admitted by `PayoutImportPayload.rows`. */
export type PayoutStatementRow = NonNullable<PayoutImportPayload["rows"]>[number];
/** Provider label admitted by `PayoutImportPayload`. */
export type PayoutProvider = PayoutImportPayload["provider"];
/** Voucher dialect admitted by `GET /v1/finance/journal-export`. */
export type JournalFormat = "standard" | "quickbooks" | "xero" | "kingdee";

/** Header the broker must supply alongside a raw `text/csv` settlement upload. */
export interface SettlementCsvHeader {
  channel_type: string;
  batch_ref: string;
  currency: string;
  period_start: string;
  period_end: string;
  fx_rate_to_base: string;
  channel_account_id?: string | null;
}

export const JOURNAL_FORMATS: readonly JournalFormat[] = [
  "standard",
  "quickbooks",
  "xero",
  "kingdee",
];

export const JOURNAL_FORMAT_LABEL: Record<JournalFormat, { zh: string; en: string }> = {
  standard: { zh: "通用分录", en: "Standard" },
  quickbooks: { zh: "QuickBooks", en: "QuickBooks" },
  xero: { zh: "Xero", en: "Xero" },
  kingdee: { zh: "金蝶 K/3", en: "Kingdee" },
};

export const PAYOUT_PROVIDERS: readonly PayoutProvider[] = [
  "pingpong",
  "lianlian",
  "payoneer",
  "bank_tt",
  "stripe",
  "other",
];

export const PAYOUT_PROVIDER_LABEL: Record<PayoutProvider, string> = {
  pingpong: "PingPong",
  lianlian: "连连支付 LianLian",
  payoneer: "Payoneer",
  bank_tt: "银行电汇 Bank T/T",
  stripe: "Stripe",
  other: "其他 Other",
};

// ---------------------------------------------------------------------------
// Exact money helpers (integer cents / six-decimal FX over BigInt)
// ---------------------------------------------------------------------------

const FX_SCALE = 1_000_000n;
const RATE_SHAPE = /^(-?)(\d+)(?:\.(\d*))?$/;

/** Parse a decimal FX string into a six-decimal `BigInt` (`"7.1"` -> `7100000n`). */
export function parseRateToE6(raw: string | number | null | undefined): bigint {
  if (raw === null || raw === undefined) return FX_SCALE;
  const cleaned = String(raw).replace(/,/g, "").trim();
  const match = RATE_SHAPE.exec(cleaned);
  if (!match) throw new Error(`无效汇率 / invalid FX rate: ${String(raw)}`);
  const sign = match[1] === "-" ? -1n : 1n;
  const whole = BigInt(match[2] ?? "0");
  const fraction = `${(match[3] ?? "").padEnd(6, "0")}000000`.slice(0, 6);
  return sign * (whole * FX_SCALE + BigInt(fraction));
}

/** Render a six-decimal FX `BigInt` back as the ledger's `NUMERIC(12, 6)` string. */
export function formatRateE6(rate: bigint): string {
  const negative = rate < 0n;
  const abs = negative ? -rate : rate;
  return `${negative ? "-" : ""}${abs / FX_SCALE}.${(abs % FX_SCALE).toString().padStart(6, "0")}`;
}

/** `cents * rate`, rounded half-up at the cent - the backend's `ROUND_HALF_UP` `_q2`. */
export function applyFx(cents: bigint, rateE6: bigint): bigint {
  const product = cents * rateE6;
  const negative = product < 0n;
  const abs = negative ? -product : product;
  const rounded = (abs + FX_SCALE / 2n) / FX_SCALE;
  return negative ? -rounded : rounded;
}

/** `settled / payout` at six decimals, rounded half-up. */
export function impliedRateE6(settledCents: bigint, payoutCents: bigint): bigint {
  if (payoutCents === 0n) return FX_SCALE;
  const numerator = settledCents * FX_SCALE;
  return (numerator * 2n + payoutCents) / (payoutCents * 2n);
}

/** Currency-symbol prefix matching the rest of the workbench's `formatMoney`. */
const CURRENCY_SYMBOL: Record<string, string> = {
  CNY: "CN¥",
  USD: "US$",
  EUR: "€",
  GBP: "£",
  JPY: "JP¥",
  HKD: "HK$",
};

/**
 * Exactly-two-decimal rendering of a ledger string, grouped with integer maths.
 *
 * `Intl.NumberFormat` would route the amount through a float, so the symbol is
 * prepended by hand and the digits come straight out of `BigInt` cents.
 */
export function formatExactMoney(raw: string | number | null | undefined, currency = "CNY"): string {
  const symbol = CURRENCY_SYMBOL[currency.toUpperCase()] ?? `${currency} `;
  const cents = parseMoneyToCents(raw ?? "0");
  if (cents === null) return `${symbol}--`;
  const negative = cents < 0n;
  const abs = negative ? -cents : cents;
  const whole = (abs / 100n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const fraction = (abs % 100n).toString().padStart(2, "0");
  return `${negative ? "-" : ""}${symbol}${whole}.${fraction}`;
}

/** Signed delta rendering: `+1,234.56` / `-12.00`, always exact. */
export function formatSignedMoney(raw: string | number | null | undefined, currency = "CNY"): string {
  const cents = parseMoneyToCents(raw ?? "0") ?? 0n;
  const sign = cents > 0n ? "+" : "";
  return `${sign}${formatExactMoney(formatCents(cents), currency)}`;
}

/** CSV column order a settlement statement must carry (net is optional). */
export const SETTLEMENT_CSV_COLUMNS: readonly string[] = [
  "external_order_id",
  "sku",
  "gross_sales_ccy",
  "commission_ccy",
  "fulfillment_fee_ccy",
  "storage_fee_ccy",
  "ad_fee_ccy",
  "refund_ccy",
];

/** CSV column order a payout statement must carry. */
export const PAYOUT_CSV_COLUMNS: readonly string[] = [
  "external_tx_ref",
  "payout_currency",
  "payout_amount",
  "settled_base_currency",
  "settled_base_amount",
  "expected_fx_rate",
];

/** One-click demo statement: two Amazon orders plus one already-known SKU row. */
export const SAMPLE_SETTLEMENT_CSV = [
  SETTLEMENT_CSV_COLUMNS.join(","),
  "111-2223334-5556667,LED-COB-24V-5M,100.00,16.00,4.00,1.00,3.00,0.00",
  "AMZ-2002,LED-COB-12V-5M,50.00,7.00,2.00,0.50,1.50,0.00",
].join("\n");

/** Matching sample batch header for `SAMPLE_SETTLEMENT_CSV`. */
export const SAMPLE_SETTLEMENT_HEADER: SettlementCsvHeader = {
  channel_type: "amazon_sp",
  batch_ref: "AMZ-SET-202611",
  currency: "USD",
  period_start: "2026-11-01",
  period_end: "2026-11-30",
  fx_rate_to_base: "7.100000",
};

/** One-click demo collection statement for the payout matcher. */
export const SAMPLE_PAYOUT_CSV = [
  PAYOUT_CSV_COLUMNS.join(","),
  "PP-20261125-0001,USD,115.50,CNY,823.55,7.100000",
].join("\n");

function trimCell(value: string | null | undefined): string {
  return (value ?? "").trim();
}

// ---------------------------------------------------------------------------
// Live calls
// ---------------------------------------------------------------------------

/** `GET /v1/finance/settlements` - imported marketplace payout statements. */
export function fetchSettlementBatches(
  channelType?: string
): Promise<SettlementBatchListRead> {
  if (shouldUseMockApi()) return Promise.resolve(mockSettlementList());
  const query = channelType ? `?channel_type=${encodeURIComponent(channelType)}` : "";
  return bffFetch<SettlementBatchListRead>(`/v1/finance/settlements${query}`);
}

/**
 * `POST /v1/finance/settlements/import` - raw `text/csv` upload.
 *
 * The statement rows travel in the body and the batch header travels in the query
 * string, exactly the shape `SettlementImportRequest` documents as `text/csv`.
 * Nothing is recomputed in the browser: `net_settlement_ccy` is optional and the
 * server derives it with `Decimal`.
 */
export function importSettlementCsv(
  header: SettlementCsvHeader,
  csvText: string
): Promise<SettlementBatchRead> {
  if (shouldUseMockApi()) return Promise.resolve(mockImportSettlement(header, csvText));
  const params = new URLSearchParams({
    channel_type: header.channel_type,
    batch_ref: header.batch_ref,
    currency: header.currency,
    period_start: header.period_start,
    period_end: header.period_end,
    fx_rate_to_base: header.fx_rate_to_base,
  });
  if (header.channel_account_id) {
    params.set("channel_account_id", header.channel_account_id);
  }
  return bffFetch<SettlementBatchRead>(`/v1/finance/settlements/import?${params.toString()}`, {
    method: "POST",
    headers: { "Content-Type": "text/csv" },
    body: csvText,
  });
}

/** `GET /v1/finance/settlements/{batch_id}/lines` - per-order settlement rows. */
export function fetchSettlementLines(batchId: string): Promise<SettlementLineListRead> {
  if (shouldUseMockApi()) return Promise.resolve(mockSettlementLines(batchId));
  return bffFetch<SettlementLineListRead>(
    `/v1/finance/settlements/${encodeURIComponent(batchId)}/lines`
  );
}

/** `GET /v1/finance/payouts` - cross-border collection statements. */
export function fetchPayoutStatements(
  matchStatus?: "matched" | "unmatched"
): Promise<PayoutStatementListRead> {
  if (shouldUseMockApi()) return Promise.resolve(mockPayoutList());
  const query = matchStatus ? `?match_status=${encodeURIComponent(matchStatus)}` : "";
  return bffFetch<PayoutStatementListRead>(`/v1/finance/payouts${query}`);
}

/** `POST /v1/finance/payouts/import` - PingPong / LianLian / Payoneer statements. */
export function importPayoutStatements(
  payload: PayoutImportPayload
): Promise<PayoutImportResultRead> {
  if (shouldUseMockApi()) return Promise.resolve(mockImportPayouts(payload));
  return bffFetch<PayoutImportResultRead>("/v1/finance/payouts/import", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

/** `POST /v1/finance/payouts/auto-match` - reconcile every unmatched payout. */
export function autoMatchPayouts(): Promise<PayoutAutoMatchResultRead> {
  if (shouldUseMockApi()) return Promise.resolve(mockAutoMatch());
  return bffFetch<PayoutAutoMatchResultRead>("/v1/finance/payouts/auto-match", {
    method: "POST",
  });
}

/** `GET /v1/finance/profit/dual-basis` - estimated basis versus settled basis. */
export function fetchDualBasisProfit(): Promise<DualBasisProfitRead> {
  if (shouldUseMockApi()) return Promise.resolve(mockDualBasis());
  return bffFetch<DualBasisProfitRead>("/v1/finance/profit/dual-basis");
}

/**
 * `GET /v1/finance/journal-export` - balanced Debit/Credit voucher CSV.
 *
 * In live mode this is the BFF URL itself, so the browser streams the attachment.
 * In mock mode it is a `data:` URL rendered by the in-memory ledger.
 */
export function journalExportHref(exportFormat: JournalFormat): string {
  if (shouldUseMockApi()) {
    return `data:text/csv;charset=utf-8,${encodeURIComponent(buildMockJournalCsv(exportFormat))}`;
  }
  return `/api/bff/v1/finance/journal-export?format=${encodeURIComponent(exportFormat)}`;
}

/** Parse a pasted payout CSV into the structured `rows` the import endpoint accepts. */
export function payoutsFromCsv(csvText: string): PayoutStatementRow[] {
  const lines = csvText
    .replace(/^\ufeff/, "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0);
  if (lines.length < 2) {
    throw new Error("回款 CSV 至少需要表头与一行数据 / a payout CSV needs a header and one row.");
  }
  const header = lines[0].split(",").map((cell) => cell.trim());
  for (const required of ["external_tx_ref", "payout_currency", "payout_amount", "settled_base_amount"]) {
    if (!header.includes(required)) {
      throw new Error(`回款 CSV 缺少列 ${required} / missing column ${required}.`);
    }
  }
  return lines.slice(1).map((line) => {
    const cells = line.split(",");
    const row: Record<string, string> = {};
    header.forEach((name, index) => {
      row[name] = trimCell(cells[index]);
    });
    return {
      external_tx_ref: row.external_tx_ref ?? "",
      payout_currency: (row.payout_currency ?? "").toUpperCase(),
      payout_amount: row.payout_amount ?? "0.00",
      settled_base_currency: (row.settled_base_currency || "CNY").toUpperCase(),
      settled_base_amount: row.settled_base_amount ?? "0.00",
      expected_fx_rate: row.expected_fx_rate || null,
      actual_fx_rate: row.actual_fx_rate || null,
      paid_at: row.paid_at || null,
      reference_hint: row.reference_hint || null,
    };
  });
}

// ---------------------------------------------------------------------------
// Mock ledger (mirrors the `--with-demo-data` seed, mutable on purpose)
// ---------------------------------------------------------------------------

const MOCK_TENANT_ID = "22222222-2222-2222-2222-222222222222";
const MOCK_BASE_CURRENCY = "CNY";
const MOCK_MATCH_TOLERANCE = 200n;

interface MockEstimate {
  /** `order_no` used as the display key. */
  orderNo: string;
  /** `external_order_id`, the marketplace handle the statement quotes. */
  externalOrderId: string;
  grossCents: bigint;
  cogsCents: bigint;
  commissionCents: bigint;
  gatewayCents: bigint;
  shippingCents: bigint;
  adCents: bigint;
  netCents: bigint;
}

interface MockLine {
  id: string;
  batchId: string;
  externalOrderId: string;
  sku: string;
  currency: string;
  rateE6: bigint;
  grossCents: bigint;
  commissionCents: bigint;
  fulfillmentCents: bigint;
  storageCents: bigint;
  adCents: bigint;
  refundCents: bigint;
  netCents: bigint;
  netBaseCents: bigint;
}

interface MockBatch {
  id: string;
  channelType: string;
  batchRef: string;
  currency: string;
  periodStart: string;
  periodEnd: string;
  rateE6: bigint;
  grossCents: bigint;
  commissionCents: bigint;
  fulfillmentCents: bigint;
  storageCents: bigint;
  adCents: bigint;
  refundCents: bigint;
  netCents: bigint;
  status: "imported" | "reconciled";
  createdAt: string;
}

interface MockPayout {
  id: string;
  provider: PayoutProvider;
  externalTxRef: string;
  payoutCurrency: string;
  payoutCents: bigint;
  settledCurrency: string;
  settledCents: bigint;
  actualRateE6: bigint;
  expectedRateE6: bigint;
  varianceCents: bigint;
  matchedBatchId: string | null;
  matchedOrderNo: string | null;
  matched: boolean;
  paidAt: string;
}

/** Estimated CVP basis the order promised when it was confirmed. */
const MOCK_ESTIMATES: readonly MockEstimate[] = [
  {
    orderNo: "AMZ-2001",
    externalOrderId: "111-2223334-5556667",
    grossCents: 71_000n,
    cogsCents: 30_000n,
    commissionCents: 10_650n,
    gatewayCents: 1_000n,
    shippingCents: 3_000n,
    adCents: 2_130n,
    netCents: 24_220n,
  },
  {
    orderNo: "AMZ-2002",
    externalOrderId: "AMZ-2002",
    grossCents: 35_500n,
    cogsCents: 15_000n,
    commissionCents: 5_325n,
    gatewayCents: 500n,
    shippingCents: 2_000n,
    adCents: 1_200n,
    netCents: 11_475n,
  },
];

let mockBatchSeq = 1;
let mockPayoutSeq = 1;

function buildMockBatches(): MockBatch[] {
  return [
    {
      id: "batch-amz-202611",
      channelType: "amazon_sp",
      batchRef: SAMPLE_SETTLEMENT_HEADER.batch_ref,
      currency: "USD",
      periodStart: SAMPLE_SETTLEMENT_HEADER.period_start,
      periodEnd: SAMPLE_SETTLEMENT_HEADER.period_end,
      rateE6: parseRateToE6(SAMPLE_SETTLEMENT_HEADER.fx_rate_to_base),
      grossCents: 15_000n,
      commissionCents: 2_300n,
      fulfillmentCents: 600n,
      storageCents: 150n,
      adCents: 450n,
      refundCents: 0n,
      netCents: 11_500n,
      status: "imported",
      createdAt: "2026-11-24T02:00:00Z",
    },
  ];
}

function buildMockLines(): MockLine[] {
  const batch = buildMockBatches()[0];
  const rows: Array<[string, string, bigint, bigint, bigint, bigint, bigint, bigint]> = [
    ["111-2223334-5556667", "LED-COB-24V-5M", 10_000n, 1_600n, 400n, 100n, 300n, 0n],
    ["AMZ-2002", "LED-COB-12V-5M", 5_000n, 700n, 200n, 50n, 150n, 0n],
  ];
  return rows.map((row, index) => {
    const [externalOrderId, sku, gross, commission, fulfillment, storage, ad, refund] = row;
    const net = gross - commission - fulfillment - storage - ad - refund;
    return {
      id: `line-amz-${index + 1}`,
      batchId: batch.id,
      externalOrderId,
      sku,
      currency: batch.currency,
      rateE6: batch.rateE6,
      grossCents: gross,
      commissionCents: commission,
      fulfillmentCents: fulfillment,
      storageCents: storage,
      adCents: ad,
      refundCents: refund,
      netCents: net,
      netBaseCents: applyFx(net, batch.rateE6),
    };
  });
}

function buildMockPayouts(): MockPayout[] {
  const payoutCents = 11_550n;
  const settledCents = 82_355n;
  const expectedRateE6 = parseRateToE6("7.100000");
  return [
    {
      id: "payout-pp-1",
      provider: "pingpong",
      externalTxRef: "PP-20261125-0001",
      payoutCurrency: "USD",
      payoutCents,
      settledCurrency: MOCK_BASE_CURRENCY,
      settledCents,
      actualRateE6: impliedRateE6(settledCents, payoutCents),
      expectedRateE6,
      varianceCents: settledCents - applyFx(payoutCents, expectedRateE6),
      matchedBatchId: null,
      matchedOrderNo: null,
      matched: false,
      paidAt: "2026-11-25T02:30:00Z",
    },
  ];
}

let mockBatches: MockBatch[] = buildMockBatches();
let mockLines: MockLine[] = buildMockLines();
let mockPayouts: MockPayout[] = buildMockPayouts();

/** Restore the seeded reconciliation ledger (tests call this between cases). */
export function resetMockSettlementLedger(): void {
  mockBatches = buildMockBatches();
  mockLines = buildMockLines();
  mockPayouts = buildMockPayouts();
  mockBatchSeq = 1;
  mockPayoutSeq = 1;
}

function batchToRead(batch: MockBatch, lineCount: number): SettlementBatchRead {
  const fx = formatRateE6(batch.rateE6);
  return {
    id: batch.id,
    tenant_id: MOCK_TENANT_ID,
    channel_type: batch.channelType,
    channel_account_id: null,
    batch_ref: batch.batchRef,
    currency: batch.currency,
    period_start: batch.periodStart,
    period_end: batch.periodEnd,
    gross_sales_ccy: formatCents(batch.grossCents),
    commission_ccy: formatCents(batch.commissionCents),
    fulfillment_fee_ccy: formatCents(batch.fulfillmentCents),
    storage_fee_ccy: formatCents(batch.storageCents),
    ad_fee_ccy: formatCents(batch.adCents),
    refund_ccy: formatCents(batch.refundCents),
    net_payout_ccy: formatCents(batch.netCents),
    fx_rate_to_base: fx,
    net_payout_base: formatCents(applyFx(batch.netCents, batch.rateE6)),
    status: batch.status,
    line_count: lineCount,
    created_at: batch.createdAt,
  };
}

function lineToRead(line: MockLine): SettlementLineRead {
  const estimate = MOCK_ESTIMATES.find(
    (candidate) =>
      candidate.externalOrderId === line.externalOrderId || candidate.orderNo === line.externalOrderId
  );
  return {
    id: line.id,
    tenant_id: MOCK_TENANT_ID,
    batch_id: line.batchId,
    external_order_id: line.externalOrderId,
    order_id: estimate ? `order-${estimate.orderNo}` : null,
    sku: line.sku,
    gross_sales_ccy: formatCents(line.grossCents),
    commission_ccy: formatCents(line.commissionCents),
    fulfillment_fee_ccy: formatCents(line.fulfillmentCents),
    storage_fee_ccy: formatCents(line.storageCents),
    ad_fee_ccy: formatCents(line.adCents),
    refund_ccy: formatCents(line.refundCents),
    net_settlement_ccy: formatCents(line.netCents),
    fx_rate_to_base: formatRateE6(line.rateE6),
    net_settlement_base: formatCents(line.netBaseCents),
    created_at: `2026-11-24T02:0${mockBatchSeq}:00Z`,
  };
}

function payoutToRead(payout: MockPayout): PayoutStatementRead {
  return {
    id: payout.id,
    tenant_id: MOCK_TENANT_ID,
    provider: payout.provider,
    external_tx_ref: payout.externalTxRef,
    payout_currency: payout.payoutCurrency,
    payout_amount: formatCents(payout.payoutCents),
    settled_base_currency: payout.settledCurrency,
    settled_base_amount: formatCents(payout.settledCents),
    actual_fx_rate: formatRateE6(payout.actualRateE6),
    expected_fx_rate: formatRateE6(payout.expectedRateE6),
    fx_variance_base: formatCents(payout.varianceCents),
    matched_settlement_batch_id: payout.matchedBatchId,
    matched_order_id: payout.matchedOrderNo ? `order-${payout.matchedOrderNo}` : null,
    match_status: payout.matched ? "matched" : "unmatched",
    paid_at: payout.paidAt,
    created_at: payout.paidAt,
  };
}

function mockSettlementList(): SettlementBatchListRead {
  const items = mockBatches.map((batch) =>
    batchToRead(batch, mockLines.filter((line) => line.batchId === batch.id).length)
  );
  const total = items.reduce(
    (acc, item) => acc + (parseMoneyToCents(item.net_payout_base) ?? 0n),
    0n
  );
  return {
    items,
    total_count: items.length,
    total_net_payout_base: formatCents(total),
    base_currency: MOCK_BASE_CURRENCY,
  };
}

function mockSettlementLines(batchId: string): SettlementLineListRead {
  const batch = mockBatches.find((candidate) => candidate.id === batchId);
  const items = mockLines.filter((line) => line.batchId === batchId).map(lineToRead);
  return {
    items,
    total_count: items.length,
    matched_count: items.filter((item) => item.order_id !== null).length,
    batch_id: batchId,
    currency: batch?.currency ?? "USD",
  };
}

function mockPayoutList(): PayoutStatementListRead {
  const items = mockPayouts.map(payoutToRead);
  const variance = mockPayouts.reduce((acc, payout) => acc + payout.varianceCents, 0n);
  return {
    items,
    total_count: items.length,
    matched_count: items.filter((item) => item.match_status === "matched").length,
    unmatched_count: items.filter((item) => item.match_status === "unmatched").length,
    total_fx_variance_base: formatCents(variance),
  };
}

function mockImportSettlement(
  header: SettlementCsvHeader,
  csvText: string
): SettlementBatchRead {
  const existing = mockBatches.find(
    (batch) => batch.channelType === header.channel_type && batch.batchRef === header.batch_ref
  );
  if (existing) {
    return batchToRead(
      existing,
      mockLines.filter((line) => line.batchId === existing.id).length
    );
  }
  const rows = csvText
    .replace(/^\ufeff/, "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0);
  if (rows.length < 2) {
    throw new Error("结算单 CSV 至少需要表头与一行数据 / a settlement CSV needs a header and one row.");
  }
  const columns = rows[0].split(",").map((cell) => cell.trim());
  const missing = SETTLEMENT_CSV_COLUMNS.filter((column) => !columns.includes(column));
  if (missing.length > 0) {
    throw new Error(`结算单 CSV 缺少列 ${missing.join(", ")} / missing columns.`);
  }

  const rateE6 = parseRateToE6(header.fx_rate_to_base);
  const batchId = `batch-mock-${mockBatchSeq++}`;
  const created: MockLine[] = [];
  for (const row of rows.slice(1)) {
    const cells = row.split(",");
    const cell = (name: string): string => {
      const index = columns.indexOf(name);
      return index >= 0 ? trimCell(cells[index]) : "";
    };
    const cents = (name: string): bigint => parseMoneyToCents(cell(name) || "0.00") ?? 0n;
    const gross = cents("gross_sales_ccy");
    const commission = cents("commission_ccy");
    const fulfillment = cents("fulfillment_fee_ccy");
    const storage = cents("storage_fee_ccy");
    const ad = cents("ad_fee_ccy");
    const refund = cents("refund_ccy");
    const precomputed = cell("net_settlement_ccy");
    const net = precomputed
      ? (parseMoneyToCents(precomputed) ?? 0n)
      : gross - commission - fulfillment - storage - ad - refund;
    created.push({
      id: `line-mock-${mockBatchSeq}-${created.length}`,
      batchId,
      externalOrderId: cell("external_order_id"),
      sku: cell("sku"),
      currency: header.currency.toUpperCase(),
      rateE6,
      grossCents: gross,
      commissionCents: commission,
      fulfillmentCents: fulfillment,
      storageCents: storage,
      adCents: ad,
      refundCents: refund,
      netCents: net,
      netBaseCents: applyFx(net, rateE6),
    });
  }

  const sum = (pick: (line: MockLine) => bigint): bigint =>
    created.reduce((acc, line) => acc + pick(line), 0n);
  const batch: MockBatch = {
    id: batchId,
    channelType: header.channel_type,
    batchRef: header.batch_ref,
    currency: header.currency.toUpperCase(),
    periodStart: header.period_start,
    periodEnd: header.period_end,
    rateE6,
    grossCents: sum((line) => line.grossCents),
    commissionCents: sum((line) => line.commissionCents),
    fulfillmentCents: sum((line) => line.fulfillmentCents),
    storageCents: sum((line) => line.storageCents),
    adCents: sum((line) => line.adCents),
    refundCents: sum((line) => line.refundCents),
    netCents: sum((line) => line.netCents),
    status: "imported",
    createdAt: "2026-11-26T02:00:00Z",
  };
  mockBatches = [...mockBatches, batch];
  mockLines = [...mockLines, ...created];
  return batchToRead(batch, created.length);
}

function mockImportPayouts(payload: PayoutImportPayload): PayoutImportResultRead {
  const rows = payload.rows ?? [];
  if (rows.length === 0 && !payload.csv_text) {
    throw new Error("回款导入没有任何记录 / the payout import carries no statements.");
  }
  const parsed: PayoutStatementRow[] =
    rows.length > 0 ? [...rows] : payoutsFromCsv(payload.csv_text ?? "");
  const provider = payload.provider;
  const imported: MockPayout[] = [];
  let matched = 0;
  for (const row of parsed) {
    if (mockPayouts.some((payout) => payout.externalTxRef === row.external_tx_ref)) continue;
    const payoutCents = parseMoneyToCents(row.payout_amount) ?? 0n;
    const settledCents = parseMoneyToCents(row.settled_base_amount) ?? 0n;
    const actualRateE6 = row.actual_fx_rate
      ? parseRateToE6(row.actual_fx_rate)
      : impliedRateE6(settledCents, payoutCents);
    const expectedRateE6 = row.expected_fx_rate
      ? parseRateToE6(row.expected_fx_rate)
      : actualRateE6;
    const payout: MockPayout = {
      id: `payout-mock-${mockPayoutSeq++}`,
      provider,
      externalTxRef: row.external_tx_ref,
      payoutCurrency: (row.payout_currency ?? "USD").toUpperCase(),
      payoutCents,
      settledCurrency: (row.settled_base_currency ?? MOCK_BASE_CURRENCY).toUpperCase(),
      settledCents,
      actualRateE6,
      expectedRateE6,
      varianceCents: settledCents - applyFx(payoutCents, expectedRateE6),
      matchedBatchId: null,
      matchedOrderNo: null,
      matched: false,
      paidAt: row.paid_at ?? "2026-11-26T00:00:00Z",
    };
    if (row.reference_hint && matchByHint(payout, row.reference_hint)) {
      matched += 1;
    }
    imported.push(payout);
    mockPayouts = [...mockPayouts, payout];
  }
  return {
    imported_count: imported.length,
    skipped_count: parsed.length - imported.length,
    matched_count: matched,
    items: imported.map(payoutToRead),
  };
}

function matchByHint(payout: MockPayout, hint: string): boolean {
  const batch = mockBatches.find((candidate) => candidate.batchRef === hint.trim());
  if (batch) {
    payout.matchedBatchId = batch.id;
    payout.matched = true;
    batch.status = "reconciled";
    return true;
  }
  const estimate = MOCK_ESTIMATES.find(
    (candidate) =>
      candidate.orderNo === hint.trim() || candidate.externalOrderId === hint.trim()
  );
  if (estimate) {
    payout.matchedOrderNo = estimate.orderNo;
    payout.matched = true;
    return true;
  }
  return false;
}

function mockAutoMatch(): PayoutAutoMatchResultRead {
  const matched: PayoutAutoMatchResultRead["items"] = [];
  for (const payout of mockPayouts) {
    if (payout.matched) continue;
    const batch = mockBatches
      .filter(
        (candidate) =>
          candidate.status === "imported" && candidate.currency === payout.payoutCurrency
      )
      .map((candidate) => ({
        candidate,
        gap: abs(candidate.netCents - payout.payoutCents),
      }))
      .filter((entry) => entry.gap <= MOCK_MATCH_TOLERANCE)
      .sort((left, right) => (left.gap < right.gap ? -1 : left.gap > right.gap ? 1 : 0))[0];
    if (batch) {
      payout.matchedBatchId = batch.candidate.id;
      payout.matched = true;
      batch.candidate.status = "reconciled";
      matched.push(payoutToRead(payout));
      continue;
    }
    const order = MOCK_ESTIMATES.map((estimate) => ({
      estimate,
      gap: abs(estimate.grossCents - payout.payoutCents),
    }))
      .filter((entry) => entry.gap <= MOCK_MATCH_TOLERANCE)
      .sort((left, right) => (left.gap < right.gap ? -1 : left.gap > right.gap ? 1 : 0))[0];
    if (order) {
      payout.matchedOrderNo = order.estimate.orderNo;
      payout.matched = true;
      matched.push(payoutToRead(payout));
    }
  }
  const variance = mockPayouts.reduce((acc, payout) => acc + payout.varianceCents, 0n);
  return {
    matched_count: matched.length,
    remaining_unmatched_count: mockPayouts.filter((payout) => !payout.matched).length,
    total_fx_variance_base: formatCents(variance),
    items: matched,
  };
}

function abs(value: bigint): bigint {
  return value < 0n ? -value : value;
}

function mockDualBasis(): DualBasisProfitRead {
  const rows: DualBasisOrderRow[] = [];
  let estimatedRevenue = 0n;
  let estimatedNet = 0n;
  let settledPayout = 0n;
  let settledNet = 0n;
  let commissionDelta = 0n;
  let fulfillmentDelta = 0n;
  let adSpend = 0n;
  let fxVariance = 0n;

  for (const estimate of MOCK_ESTIMATES) {
    const lines = mockLines.filter(
      (line) =>
        line.externalOrderId === estimate.externalOrderId || line.externalOrderId === estimate.orderNo
    );
    if (lines.length === 0) continue;
    const sum = (pick: (line: MockLine) => bigint): bigint =>
      lines.reduce((acc, line) => acc + pick(line), 0n);
    const settledRevenue =
      sum((line) => applyFx(line.grossCents, line.rateE6)) -
      sum((line) => applyFx(line.refundCents, line.rateE6));
    const settledCommission = sum((line) => applyFx(line.commissionCents, line.rateE6));
    const settledFulfillment = sum((line) =>
      applyFx(line.fulfillmentCents + line.storageCents, line.rateE6)
    );
    const settledAd = sum((line) => applyFx(line.adCents, line.rateE6));
    const payoutBase = sum((line) => line.netBaseCents);
    const orderFx = mockPayouts
      .filter((payout) => payout.matchedOrderNo === estimate.orderNo)
      .reduce((acc, payout) => acc + payout.varianceCents, 0n);
    const profit = payoutBase - estimate.cogsCents + orderFx;

    rows.push({
      order_id: `order-${estimate.orderNo}`,
      order_no: estimate.orderNo,
      currency: lines[0].currency,
      estimated_revenue_base: formatCents(estimate.grossCents),
      estimated_commission_base: formatCents(estimate.commissionCents),
      estimated_fulfillment_storage_base: formatCents(estimate.shippingCents),
      estimated_ad_spend_base: formatCents(estimate.adCents),
      estimated_cogs_base: formatCents(estimate.cogsCents),
      estimated_net_profit_base: formatCents(estimate.netCents),
      settled_revenue_base: formatCents(settledRevenue),
      settled_commission_base: formatCents(settledCommission),
      settled_fulfillment_storage_base: formatCents(settledFulfillment),
      settled_ad_spend_base: formatCents(settledAd),
      settled_cogs_base: formatCents(estimate.cogsCents),
      settled_payout_base: formatCents(payoutBase),
      fx_variance_base: formatCents(orderFx),
      settled_net_profit_base: formatCents(profit),
      commission_delta_base: formatCents(settledCommission - estimate.commissionCents),
      fulfillment_storage_delta_base: formatCents(settledFulfillment - estimate.shippingCents),
      ad_delta_base: formatCents(settledAd - estimate.adCents),
      profit_variance_base: formatCents(profit - estimate.netCents),
    });

    estimatedRevenue += estimate.grossCents;
    estimatedNet += estimate.netCents;
    settledPayout += payoutBase;
    settledNet += profit;
    commissionDelta += settledCommission - estimate.commissionCents;
    fulfillmentDelta += settledFulfillment - estimate.shippingCents;
    adSpend += settledAd;
    fxVariance += orderFx;
  }

  return {
    base_currency: MOCK_BASE_CURRENCY,
    estimated_revenue_base: formatCents(estimatedRevenue),
    estimated_net_margin_base: formatCents(estimatedNet),
    settled_payout_base: formatCents(settledPayout),
    settled_net_profit_base: formatCents(settledNet),
    commission_delta_base: formatCents(commissionDelta),
    fulfillment_storage_delta_base: formatCents(fulfillmentDelta),
    ad_spend_base: formatCents(adSpend),
    fx_variance_base: formatCents(fxVariance),
    total_profit_variance_base: formatCents(settledNet - estimatedNet),
    matched_orders_count: rows.length,
    unmatched_lines_count: mockLines.filter(
      (line) =>
        !MOCK_ESTIMATES.some(
          (estimate) =>
            estimate.externalOrderId === line.externalOrderId || estimate.orderNo === line.externalOrderId
        )
    ).length,
    rows,
  };
}

// ---------------------------------------------------------------------------
// Mock voucher export (balanced by construction, exactly like the service)
// ---------------------------------------------------------------------------

interface MockJournalRow {
  voucher: string;
  date: string;
  accountCode: string;
  accountName: string;
  description: string;
  debitCents: bigint;
  creditCents: bigint;
}

function buildMockJournalCsv(exportFormat: JournalFormat): string {
  const rows: MockJournalRow[] = [];
  const clearedBatches = mockBatches.filter((batch) => batch.status === "reconciled");
  for (const batch of clearedBatches) {
    const voucher = `STL-${batch.batchRef}`;
    const date = batch.periodEnd;
    const label = `${batch.channelType} settlement ${batch.batchRef}`;
    const gross = applyFx(batch.grossCents, batch.rateE6);
    const commission = applyFx(batch.commissionCents, batch.rateE6);
    const fulfillment = applyFx(batch.fulfillmentCents + batch.storageCents, batch.rateE6);
    const ad = applyFx(batch.adCents, batch.rateE6);
    const refund = applyFx(batch.refundCents, batch.rateE6);
    const net = applyFx(batch.netCents, batch.rateE6);
    const cogs = mockLines
      .filter((line) => line.batchId === batch.id)
      .reduce((acc, line) => {
        const estimate = MOCK_ESTIMATES.find(
          (candidate) =>
            candidate.externalOrderId === line.externalOrderId ||
            candidate.orderNo === line.externalOrderId
        );
        return acc + (estimate?.cogsCents ?? 0n);
      }, 0n);
    rows.push({
      voucher,
      date,
      accountCode: "1110",
      accountName: "Marketplace Clearing",
      description: `${label} gross`,
      debitCents: gross,
      creditCents: 0n,
    });
    rows.push({
      voucher,
      date,
      accountCode: "4000",
      accountName: "Sales Revenue",
      description: label,
      debitCents: 0n,
      creditCents: gross,
    });
    const buckets: Array<[[string, string], bigint, string]> = [
      [["6100", "Platform Commission Expense"], commission, "commission"],
      [["6200", "Fulfillment & Storage Expense"], fulfillment, "fulfillment & storage"],
      [["6300", "Advertising Expense"], ad, "advertising"],
      [["4100", "Sales Returns"], refund, "refund"],
    ];
    for (const [[code, name], amount, what] of buckets) {
      if (amount === 0n) continue;
      rows.push({ voucher, date, accountCode: code, accountName: name, description: `${label} ${what}`, debitCents: amount, creditCents: 0n });
      rows.push({ voucher, date, accountCode: "1110", accountName: "Marketplace Clearing", description: `${label} ${what}`, debitCents: 0n, creditCents: amount });
    }
    if (cogs !== 0n) {
      rows.push({ voucher, date, accountCode: "5000", accountName: "Cost of Goods Sold", description: `${label} COGS`, debitCents: cogs, creditCents: 0n });
      rows.push({ voucher, date, accountCode: "1200", accountName: "Inventory", description: `${label} COGS`, debitCents: 0n, creditCents: cogs });
    }
    rows.push({ voucher, date, accountCode: "1000", accountName: "Cash / Bank", description: `${label} net payout`, debitCents: net, creditCents: 0n });
    rows.push({ voucher, date, accountCode: "1110", accountName: "Marketplace Clearing", description: `${label} net payout`, debitCents: 0n, creditCents: net });

    const residual = gross - (commission + fulfillment + ad + refund + net);
    if (residual > 0n) {
      rows.push({ voucher, date, accountCode: "7000", accountName: "FX Gain / Loss", description: `${label} clearing residual`, debitCents: residual, creditCents: 0n });
      rows.push({ voucher, date, accountCode: "1110", accountName: "Marketplace Clearing", description: `${label} clearing residual`, debitCents: 0n, creditCents: residual });
    } else if (residual < 0n) {
      rows.push({ voucher, date, accountCode: "1110", accountName: "Marketplace Clearing", description: `${label} clearing residual`, debitCents: -residual, creditCents: 0n });
      rows.push({ voucher, date, accountCode: "7000", accountName: "FX Gain / Loss", description: `${label} clearing residual`, debitCents: 0n, creditCents: -residual });
    }

    const payoutFx = mockPayouts
      .filter((payout) => payout.matchedBatchId === batch.id)
      .reduce((acc, payout) => acc + payout.varianceCents, 0n);
    if (payoutFx !== 0n) {
      rows.push(...fxRows(voucher, date, payoutFx, `${label} realised FX`));
    }
  }

  for (const payout of mockPayouts.filter((item) => item.matchedOrderNo !== null)) {
    const voucher = `PAYOUT-${payout.externalTxRef}`;
    const date = payout.paidAt.slice(0, 10);
    const expected = applyFx(payout.payoutCents, payout.expectedRateE6);
    const label = `${payout.provider} payout ${payout.externalTxRef}`;
    rows.push({ voucher, date, accountCode: "1000", accountName: "Cash / Bank", description: label, debitCents: payout.settledCents, creditCents: 0n });
    rows.push({ voucher, date, accountCode: "1100", accountName: "Accounts Receivable", description: label, debitCents: 0n, creditCents: expected });
    const variance = payout.settledCents - expected;
    if (variance !== 0n) rows.push(...fxRows(voucher, date, variance, label));
  }

  const header =
    exportFormat === "quickbooks"
      ? ["Journal No", "Journal Date", "Account Code", "Account Name", "Debit", "Credit", "Memo", "Currency"]
      : exportFormat === "xero"
        ? ["Narration", "Date", "Account Code", "Account Name", "Debit", "Credit", "Currency"]
        : exportFormat === "kingdee"
          ? ["凭证字号", "日期", "科目代码", "科目名称", "摘要", "借方本位币", "贷方本位币", "币别"]
          : ["voucher_no", "entry_date", "currency", "account_code", "account_name", "description", "debit_base", "credit_base"];

  const lines = [header.join(",")];
  for (const row of rows) {
    const debit = formatCents(row.debitCents);
    const credit = formatCents(row.creditCents);
    const cells =
      exportFormat === "quickbooks"
        ? [row.voucher, row.date, row.accountCode, row.accountName, debit, credit, row.description, MOCK_BASE_CURRENCY]
        : exportFormat === "xero"
          ? [row.description, row.date, row.accountCode, row.accountName, debit, credit, MOCK_BASE_CURRENCY]
          : exportFormat === "kingdee"
            ? [row.voucher, row.date, row.accountCode, row.accountName, row.description, debit, credit, MOCK_BASE_CURRENCY]
            : [row.voucher, row.date, MOCK_BASE_CURRENCY, row.accountCode, row.accountName, row.description, debit, credit];
    lines.push(cells.map(csvSafeCell).join(","));
  }
  return lines.join("\n");
}

function fxRows(voucher: string, date: string, variance: bigint, label: string): MockJournalRow[] {
  const amount = abs(variance);
  if (variance > 0n) {
    return [
      { voucher, date, accountCode: "1000", accountName: "Cash / Bank", description: label, debitCents: amount, creditCents: 0n },
      { voucher, date, accountCode: "7000", accountName: "FX Gain / Loss", description: label, debitCents: 0n, creditCents: amount },
    ];
  }
  return [
    { voucher, date, accountCode: "7000", accountName: "FX Gain / Loss", description: label, debitCents: amount, creditCents: 0n },
    { voucher, date, accountCode: "1000", accountName: "Cash / Bank", description: label, debitCents: 0n, creditCents: amount },
  ];
}

/** Mirror of the server's CSV formula guard: quote non-numeric formula-like cells. */
export function csvSafeCell(value: string): string {
  if (/^-?\d+(\.\d{1,6})?$/.test(value)) return value;
  if (/^[=+\-@\t\r]/.test(value)) return `'${value}`;
  return value;
}
