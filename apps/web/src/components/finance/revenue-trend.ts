/**
 * CVP trend derivation.
 *
 * The dashboard and analytics pages may only visualise numbers the backend
 * actually returned. This module turns the real per-order profit snapshots of
 * `GET /finance/overview` into a chart series and an exact summary, using
 * fixed-point integer maths (BigInt) so displayed money never drifts through
 * IEEE-754 rounding — same rule the backend enforces with `Decimal`/`NUMERIC`.
 *
 * Deliberately NOT a time series: the API exposes no daily aggregation and this
 * UI layer must not invent one. Each chart point is one real order snapshot.
 */

export interface CvpSnapshotLike {
  order_id: string;
  order_no: string;
  base_currency: string;
  gross_revenue_base: string;
  net_contribution_margin_base: string;
}

export type CvpTrendWindow = "recent5" | "recent10" | "all";

export interface CvpTrendPoint {
  label: string;
  /** Chart geometry only (pixels); the exact value is `revenueText`. */
  revenue: number;
  margin: number;
  revenueText: string;
  marginText: string;
}

export interface CvpTrendResult {
  points: CvpTrendPoint[];
  /** Exact decimal strings, 2 dp, ROUND_HALF_UP. */
  revenue: string;
  margin: string;
  marginPct: string;
  orders: number;
  baseCurrency: string;
}

const SCALE = 6;
const FACTOR = BigInt(10) ** BigInt(SCALE);

function parseScaled(input: string): bigint {
  const value = input.trim();
  const match = /^([+-]?)(\d+)(?:\.(\d+))?$/.exec(value);
  if (!match) throw new Error(`invalid decimal: ${input}`);
  const sign = match[1] === "-" ? BigInt(-1) : BigInt(1);
  const whole = BigInt(match[2]);
  const rawFraction = match[3] ?? "";
  const head = rawFraction.slice(0, SCALE).padEnd(SCALE, "0");
  let scaled = whole * FACTOR + (head ? BigInt(head) : BigInt(0));
  const tail = rawFraction.slice(SCALE);
  // ROUND_HALF_UP on the truncated digits so parsing never silently truncates.
  if (tail && Number(tail[0]) >= 5) scaled += BigInt(1);
  return sign * scaled;
}

/** Integer division with ROUND_HALF_UP; `denominator` must be positive. */
function divideRound(numerator: bigint, denominator: bigint): bigint {
  if (denominator <= BigInt(0)) throw new Error("invalid denominator");
  const negative = numerator < BigInt(0);
  const abs = negative ? -numerator : numerator;
  let quotient = abs / denominator;
  if ((abs % denominator) * BigInt(2) >= denominator) quotient += BigInt(1);
  return negative ? -quotient : quotient;
}

function groupThousands(digits: string): string {
  return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

function formatScaled(scaled: bigint, digits = 2): string {
  const drop = SCALE - digits;
  const value =
    drop > 0
      ? divideRound(scaled, BigInt(10) ** BigInt(drop))
      : scaled * BigInt(10) ** BigInt(-drop);
  const negative = value < BigInt(0);
  const abs = negative ? -value : value;
  const base = BigInt(10) ** BigInt(digits);
  const whole = abs / base;
  const fraction = (abs % base).toString().padStart(digits, "0");
  const text = digits > 0 ? `${groupThousands(whole.toString())}.${fraction}` : groupThousands(whole.toString());
  return negative ? `-${text}` : text;
}

/** Exact sum of newline-free decimal money strings, formatted at 2 dp. */
export function sumDecimalStrings(values: readonly string[]): string {
  const total = values.reduce((acc, value) => acc + parseScaled(value), BigInt(0));
  return formatScaled(total, 2);
}

/**
 * Build the CVP chart series from the real profit snapshots.
 *
 * `recent5` / `recent10` keep the head of the list: the API orders snapshots by
 * `recalculated_at DESC`, so the head is the most recently recalculated set.
 */
export function buildCvpTrend(
  snapshots: readonly CvpSnapshotLike[],
  window: CvpTrendWindow = "all"
): CvpTrendResult {
  const limited =
    window === "all" ? snapshots : snapshots.slice(0, window === "recent5" ? 5 : 10);

  const points: CvpTrendPoint[] = [];
  let revenueScaled = BigInt(0);
  let marginScaled = BigInt(0);

  for (const snapshot of limited) {
    const revenue = parseScaled(snapshot.gross_revenue_base);
    const margin = parseScaled(snapshot.net_contribution_margin_base);
    revenueScaled += revenue;
    marginScaled += margin;
    points.push({
      label: snapshot.order_no,
      revenue: Number(revenue) / 10 ** SCALE,
      margin: Number(margin) / 10 ** SCALE,
      revenueText: formatScaled(revenue, 2),
      marginText: formatScaled(margin, 2),
    });
  }

  const marginPct =
    revenueScaled === BigInt(0)
      ? formatScaled(BigInt(0), 2)
      : formatScaled(divideRound(marginScaled * BigInt(100) * FACTOR, revenueScaled), 2);

  return {
    points,
    revenue: formatScaled(revenueScaled, 2),
    margin: formatScaled(marginScaled, 2),
    marginPct,
    orders: points.length,
    baseCurrency: snapshots[0]?.base_currency ?? "CNY",
  };
}
