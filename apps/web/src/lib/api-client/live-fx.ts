import { bffFetch } from "@/lib/api";
import { shouldUseMockApi } from "@/lib/data-source";
import type { LiveFxRatesRead } from "@/lib/api-client/types.gen";

export const mockLiveFxRates: LiveFxRatesRead = {
  base: "USD",
  quote: "CNY",
  usd_cny: "7.2410",
  rates: [
    { pair: "USD/CNY", base: "USD", quote: "CNY", rate: "7.2410" },
    { pair: "EUR/CNY", base: "EUR", quote: "CNY", rate: "7.8520" },
    { pair: "GBP/CNY", base: "GBP", quote: "CNY", rate: "9.1640" },
    { pair: "USD/HKD", base: "USD", quote: "HKD", rate: "7.7820" },
  ],
  source: "ECB (Frankfurter)",
  as_of_date: "2026-11-20",
  fetched_at: "2026-11-20T08:00:00Z",
  is_live: true,
  refresh_interval_seconds: 3600,
};

export async function fetchLiveFxRates(options?: {
  base?: string;
  forceRefresh?: boolean;
}): Promise<LiveFxRatesRead> {
  if (shouldUseMockApi()) {
    return mockLiveFxRates;
  }
  const params = new URLSearchParams();
  if (options?.base) params.set("base", options.base);
  if (options?.forceRefresh) params.set("force_refresh", "true");
  const qs = params.toString();
  return bffFetch<LiveFxRatesRead>(`/v1/finance/fx-rates/live${qs ? `?${qs}` : ""}`);
}
