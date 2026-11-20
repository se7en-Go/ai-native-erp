import { describe, expect, it } from "vitest";

import { formatMoney } from "@/lib/contracts";
import {
  buildCvpTrend,
  sumDecimalStrings,
  type CvpSnapshotLike,
} from "@/components/finance/revenue-trend";

function snapshot(
  orderNo: string,
  gross: string,
  margin: string,
  currency = "CNY"
): CvpSnapshotLike {
  return {
    order_id: `id-${orderNo}`,
    order_no: orderNo,
    base_currency: currency,
    gross_revenue_base: gross,
    net_contribution_margin_base: margin,
  };
}

describe("CVP trend derivation (real snapshots only)", () => {
  it("sums decimal strings exactly without IEEE-754 drift", () => {
    expect(sumDecimalStrings(["0.10", "0.20"])).toBe("0.30");
    expect(sumDecimalStrings(["9007199254740993.000010", "0.000000"])).toBe(
      "9,007,199,254,740,993.00"
    );
    // 17.5 digits of precision would be lost by Number() arithmetic.
    expect(sumDecimalStrings(["123456789012345.01", "987654321098765.02"])).toBe(
      "1,111,111,110,111,110.03"
    );
    expect(sumDecimalStrings([])).toBe("0.00");
  });

  it("rounds half-up at two decimals and rejects malformed money strings", () => {
    expect(sumDecimalStrings(["1.004", "0.001"])).toBe("1.01");
    expect(sumDecimalStrings(["-2.005"])).toBe("-2.01");
    expect(() => sumDecimalStrings(["12,345"])).toThrow(/invalid decimal/);
  });

  it("builds per-order points plus an exact summary from the real snapshots", () => {
    const trend = buildCvpTrend(
      [
        snapshot("ORD-B2B-2026-01", "52332.00", "16280.00"),
        snapshot("ORD-B2C-2026-88", "432.00", "97.50"),
      ],
      "all"
    );

    expect(trend.points).toEqual([
      {
        label: "ORD-B2B-2026-01",
        revenue: 52332,
        margin: 16280,
        revenueText: "52,332.00",
        marginText: "16,280.00",
      },
      {
        label: "ORD-B2C-2026-88",
        revenue: 432,
        margin: 97.5,
        revenueText: "432.00",
        marginText: "97.50",
      },
    ]);
    expect(trend.revenue).toBe("52,764.00");
    expect(trend.margin).toBe("16,377.50");
    expect(trend.marginPct).toBe("31.04");
    expect(trend.orders).toBe(2);
    expect(trend.baseCurrency).toBe("CNY");
  });

  it("keeps the newest snapshots when a window is selected", () => {
    const snapshots = Array.from({ length: 7 }, (_, index) =>
      snapshot(`ORD-${index + 1}`, "100.00", "10.00")
    );

    const recent5 = buildCvpTrend(snapshots, "recent5");
    expect(recent5.points).toHaveLength(5);
    // The API returns snapshots ordered by recalculated_at DESC, so the head is newest.
    expect(recent5.points[0].label).toBe("ORD-1");
    expect(recent5.revenue).toBe("500.00");
    expect(recent5.margin).toBe("50.00");

    expect(buildCvpTrend(snapshots, "recent10").points).toHaveLength(7);
    expect(buildCvpTrend(snapshots, "all").points).toHaveLength(7);
  });

  it("never divides by zero and degrades cleanly with no snapshots", () => {
    const zeroRevenue = buildCvpTrend([snapshot("ORD-ZERO", "0.00", "0.00")], "all");
    expect(zeroRevenue.marginPct).toBe("0.00");

    const empty = buildCvpTrend([], "recent5");
    expect(empty.points).toEqual([]);
    expect(empty.revenue).toBe("0.00");
    expect(empty.margin).toBe("0.00");
    expect(empty.marginPct).toBe("0.00");
    expect(empty.orders).toBe(0);
  });

  it("formats comma-grouped decimal strings cleanly through formatMoney", () => {
    expect(formatMoney(sumDecimalStrings(["17240.00", "14220.00"]), "USD")).toBe("$31,460.00");
    const trend = buildCvpTrend(
      [
        snapshot("DEMO-B2B-2002", "14824.60", "5296.02", "USD"),
        snapshot("DEMO-B2B-2001", "17240.00", "6050.60", "USD"),
      ],
      "all"
    );
    expect(formatMoney(trend.revenue, trend.baseCurrency)).toBe("$32,064.60");
    expect(formatMoney(trend.margin, trend.baseCurrency)).toBe("$11,346.62");
  });
});
