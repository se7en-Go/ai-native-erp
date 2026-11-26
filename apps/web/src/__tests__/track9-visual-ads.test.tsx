// Track 9 UI suite: sourcing visual-AI studio on /ai/studio and the read-only ad
// spend panel on /analytics.
//
// The client-module cases run the real `bffFetch` path, so URL, method and body
// are verified against the locked backend contract; the panel cases render the
// real components against a stateful fetch stub (live mode) or the mutable mock
// fixtures. Money and ratios are asserted as the backend's exact decimal
// strings — never a recomputed float.
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import AiStudioPage from "@/app/ai/studio/page";
import AnalyticsPage from "@/app/analytics/page";
import { AdsSpendPanel } from "@/components/analytics/ads-spend-panel";
import { VisualStudioPanel } from "@/components/catalog/visual-studio-panel";
import { AuthSessionContext, type AuthSessionState } from "@/components/providers";
import { I18nProvider } from "@/lib/i18n";
import type { Principal } from "@/lib/contracts";
import { fetchCallBody, fetchCallMethod, fetchCallUrl, normalizeFetchInput } from "@/__tests__/fetch-stub";
import {
  ADS_PLATFORMS,
  VISUAL_OPERATIONS,
  VISUAL_PROVIDERS,
  canManageAdsAccounts,
  canViewAdsSpend,
  canWriteVisualJobs,
  connectAdAccount,
  createVisualJob,
  deleteAdAccount,
  deleteVisualJob,
  emptySpendSummary,
  fetchAdAccounts,
  fetchAdsSpendSummary,
  fetchVisualJobs,
  formatAdsMoney,
  formatCount,
  formatMultiple,
  formatPct,
  normalizeSpendSummary,
  normalizeVisualJob,
  operationLabel,
  platformLabel,
  providerLabel,
  resetTrack9Mocks,
  syncAdAccount,
  type AdAccountRead,
  type AdSpendRowRead,
  type AdsSpendSummaryRead,
  type VisualJobRead,
} from "@/lib/api-client/track9-visual-ads";

vi.mock("@/hooks/use-realtime-inbox", () => ({ useRealtimeInbox: vi.fn() }));

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const PNG_1PX =
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/AF+7kGvAAAAAElFTkSuQmCC";

const JOBS: VisualJobRead[] = [
  {
    id: "job-1",
    tenant_id: "t1",
    variant_id: null,
    provider: "sandbox",
    operation: "remove_bg",
    source_image_url: "https://cdn.example.com/a.png",
    target_locale: null,
    prompt: "clean background",
    status: "completed",
    output_image_url: PNG_1PX,
    output_mime_type: "image/png",
    output_r2_key: null,
    error_message: null,
    created_by: "u-owner",
    created_at: "2026-11-27T02:00:00Z",
    completed_at: "2026-11-27T02:00:01Z",
  },
  {
    id: "job-2",
    tenant_id: "t1",
    variant_id: null,
    provider: "remove_bg",
    operation: "remove_bg",
    source_image_url: "https://cdn.example.com/b.png",
    target_locale: null,
    prompt: null,
    status: "failed",
    output_image_url: null,
    output_mime_type: null,
    output_r2_key: null,
    error_message:
      "remove.bg is not configured: set REMOVE_BG_API_KEY before requesting background removal.",
    created_by: "u-owner",
    created_at: "2026-11-27T02:05:00Z",
    completed_at: "2026-11-27T02:05:00Z",
  },
];

const ACCOUNTS: AdAccountRead[] = [
  {
    id: "ad-1",
    tenant_id: "t1",
    platform: "meta_ads",
    account_name: "Meta 主账户",
    external_account_id: "act_778899",
    currency: "USD",
    status: "active",
    last_synced_at: "2026-11-27T01:00:00Z",
    created_at: "2026-11-20T01:00:00Z",
    has_credentials: true,
    token_preview: "\u2022\u2022\u2022\u2022ab12",
  },
];

const ROWS: AdSpendRowRead[] = [
  {
    id: "row-1",
    ad_account_id: "ad-1",
    spend_date: "2026-11-26",
    campaign_name: "QZ-LED Retarget",
    sku: "LED-TUBE-1200",
    impressions: 3400,
    clicks: 85,
    ad_spend: "120.50",
    attributed_sales: "301.25",
    total_store_sales: "301.25",
    currency: "USD",
  },
];

const SUMMARY: AdsSpendSummaryRead = {
  tenant_id: "t1",
  days: 30,
  currency: "USD",
  total_ad_spend: "145.37",
  total_attributed_sales: "311.60",
  total_store_sales: "578.40",
  total_impressions: 3893,
  total_clicks: 204,
  roas: "2.14",
  tacos_pct: "25.13",
  ctr_pct: "5.24",
  rows: ROWS,
};

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function stubFetch(
  handler: (call: { url: string; method: string; bodyText: string | null }) => Response
): Array<{ url: string; method: string; bodyText: string | null }> {
  const calls: Array<{ url: string; method: string; bodyText: string | null }> = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const { url, method } = normalizeFetchInput(input, init);
      const bodyText =
        input instanceof Request
          ? await input.clone().text()
          : typeof init?.body === "string"
            ? init.body
            : null;
      const call = { url, method: (method ?? "GET").toUpperCase(), bodyText };
      calls.push(call);
      return handler(call);
    })
  );
  return calls;
}

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

function renderUi(ui: React.ReactNode, role: Principal["role"] = "owner", locale: "zh" | "en" = "zh") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const authValue: AuthSessionState = {
    principal: principal(role),
    isLoading: false,
    logout: vi.fn(async () => {}),
    refreshSession: vi.fn(async () => {}),
  };
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthSessionContext.Provider value={authValue}>
        <I18nProvider defaultLocale={locale}>{ui}</I18nProvider>
      </AuthSessionContext.Provider>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  resetTrack9Mocks();
  vi.unstubAllEnvs();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

describe("track9 render helpers", () => {
  it("renders decimal strings verbatim without float arithmetic", () => {
    expect(formatMultiple("2.14")).toBe("2.14x");
    expect(formatMultiple("0.00")).toBe("0.00x");
    expect(formatMultiple(null)).toBe("0.00x");
    expect(formatMultiple("")).toBe("0.00x");

    expect(formatPct("25.13")).toBe("25.13%");
    expect(formatPct(undefined)).toBe("0.00%");

    expect(formatAdsMoney("145.37", "usd")).toBe("USD 145.37");
    expect(formatAdsMoney(null, "USD")).toBe("USD 0.00");

    expect(formatCount(3893)).toBe("3,893");
    expect(formatCount(0)).toBe("0");
    expect(formatCount(null)).toBe("0");
  });

  it("maps provider / operation / platform codes to both locales", () => {
    expect(VISUAL_PROVIDERS.map((row) => row.value)).toEqual([
      "sandbox",
      "remove_bg",
      "openai_image",
    ]);
    expect(VISUAL_OPERATIONS.map((row) => row.value)).toEqual([
      "remove_bg",
      "scene_replace",
      "translate_overlay",
    ]);
    expect(ADS_PLATFORMS.map((row) => row.value)).toEqual([
      "sandbox_ads",
      "meta_ads",
      "amazon_ads",
    ]);
    expect(providerLabel("remove_bg", "zh")).toBe("remove.bg 去白底");
    expect(providerLabel("openai_image", "en")).toBe("OpenAI re-render");
    expect(operationLabel("translate_overlay", "zh")).toBe("图片文字翻译 translate_overlay");
    expect(operationLabel("translate_overlay", "en")).toBe("Translate overlay");
    expect(platformLabel("sandbox_ads", "zh")).toBe("Sandbox 沙箱广告");
  });

  it("mirrors the backend role matrix for both features", () => {
    // ADS_READ_ROLES = owner/admin/manager/finance, ADS_WRITE_ROLES = owner/admin/manager.
    expect(canViewAdsSpend("owner")).toBe(true);
    expect(canViewAdsSpend("finance")).toBe(true);
    expect(canViewAdsSpend("agent")).toBe(false);
    expect(canViewAdsSpend(null)).toBe(false);

    expect(canManageAdsAccounts("manager")).toBe(true);
    expect(canManageAdsAccounts("finance")).toBe(false);
    expect(canManageAdsAccounts("agent")).toBe(false);

    // FINANCE may read the visual studio but not spend provider credits.
    expect(canWriteVisualJobs("agent")).toBe(true);
    expect(canWriteVisualJobs("finance")).toBe(false);
  });

  it("normalises a non-contract body instead of crashing a render", () => {
    const job = normalizeVisualJob({});
    expect(job.id).toBe("");
    expect(job.provider).toBe("sandbox");
    expect(job.status).toBe("completed");
    expect(job.output_image_url).toBeNull();

    const summary = normalizeSpendSummary({ rows: "not-an-array" }, 7);
    expect(summary.days).toBe(7);
    expect(summary.roas).toBe("0.00");
    expect(summary.rows).toEqual([]);
    expect(emptySpendSummary().tacos_pct).toBe("0.00");
  });
});

// ---------------------------------------------------------------------------
// Client module: live BFF contract
// ---------------------------------------------------------------------------

describe("track9 client module (live BFF contract)", () => {
  it("talks only to the allowlisted /catalog and /finance routes", async () => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "false");
    const calls = stubFetch((call) => {
      if (call.url.endsWith("/v1/catalog/visual-jobs") && call.method === "GET")
        return jsonResponse(JOBS);
      if (call.url.endsWith("/v1/catalog/visual-jobs") && call.method === "POST")
        return jsonResponse(JOBS[0], 201);
      if (call.url.includes("/v1/catalog/visual-jobs/job-1") && call.method === "DELETE")
        return new Response(null, { status: 204 });
      if (call.url.endsWith("/v1/finance/ads/accounts") && call.method === "GET")
        return jsonResponse(ACCOUNTS);
      if (call.url.endsWith("/v1/finance/ads/accounts") && call.method === "POST")
        return jsonResponse(ACCOUNTS[0], 201);
      if (call.url.includes("/sync"))
        return jsonResponse({
          account: ACCOUNTS[0],
          rows_upserted: 1,
          days: 7,
          synced_at: "2026-11-27T03:00:00Z",
        });
      if (call.url.includes("/v1/finance/ads/accounts/ad-1") && call.method === "DELETE")
        return new Response(null, { status: 204 });
      if (call.url.includes("/v1/finance/ads/spend-summary")) return jsonResponse(SUMMARY);
      return jsonResponse({}, 200);
    });

    expect(await fetchVisualJobs()).toHaveLength(2);
    const created = await createVisualJob({
      provider: "sandbox",
      operation: "scene_replace",
      source_image_url: "https://cdn.example.com/a.png",
      prompt: "warm living room",
      target_locale: "es-MX",
    });
    expect(created.id).toBe("job-1");
    await deleteVisualJob("job-1");

    expect(await fetchAdAccounts()).toHaveLength(1);
    const account = await connectAdAccount({
      platform: "meta_ads",
      account_name: "Meta 主账户",
      external_account_id: "act_778899",
      access_token: "EAAG-secret",
      currency: "USD",
    });
    expect(account.token_preview).toBe("\u2022\u2022\u2022\u2022ab12");
    const synced = await syncAdAccount("ad-1", 7);
    expect(synced.rows_upserted).toBe(1);
    expect(synced.account.last_synced_at).toBe("2026-11-27T01:00:00Z");
    await deleteAdAccount("ad-1");
    const summary = await fetchAdsSpendSummary(30);
    expect(summary.roas).toBe("2.14");
    expect(summary.tacos_pct).toBe("25.13");
    expect(summary.ctr_pct).toBe("5.24");

    for (const call of calls) {
      expect(call.url.startsWith("/api/bff/v1/")).toBe(true);
      expect(call.url).not.toContain("/webhooks");
    }

    const createCall = calls.find(
      (call) => call.url.endsWith("/v1/catalog/visual-jobs") && call.method === "POST"
    );
    expect(createCall).toBeDefined();
    expect(JSON.parse(await fetchCallBody([createCall!.url, { body: createCall!.bodyText! }]))).toEqual({
      provider: "sandbox",
      operation: "scene_replace",
      source_image_url: "https://cdn.example.com/a.png",
      prompt: "warm living room",
      target_locale: "es-MX",
    });

    const connectCall = calls.find(
      (call) => call.url.endsWith("/v1/finance/ads/accounts") && call.method === "POST"
    );
    expect(connectCall).toBeDefined();
    expect(JSON.parse(connectCall!.bodyText ?? "{}")).toEqual({
      platform: "meta_ads",
      account_name: "Meta 主账户",
      external_account_id: "act_778899",
      access_token: "EAAG-secret",
      currency: "USD",
    });

    const syncCall = calls.find((call) => call.url.includes("/sync"));
    expect(syncCall?.url).toBe("/api/bff/v1/finance/ads/accounts/ad-1/sync?days=7");
    expect(syncCall?.method).toBe("POST");

    const deleteAccountCall = calls.find(
      (call) => call.url.includes("/v1/finance/ads/accounts/ad-1") && call.method === "DELETE"
    );
    expect(deleteAccountCall).toBeDefined();
    expect(deleteAccountCall!.url).toBe("/api/bff/v1/finance/ads/accounts/ad-1");

    expect(
      calls.some((call) => call.url === "/api/bff/v1/finance/ads/spend-summary?days=30")
    ).toBe(true);
    expect(fetchCallMethod([calls[0].url, { method: "GET" }])).toBe("GET");
    expect(fetchCallUrl([calls[0].url])).toBe("/api/bff/v1/catalog/visual-jobs");
  });

  it("survives a proxy that returns a non-contract body", async () => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "false");
    stubFetch(() => jsonResponse({}));
    expect(await fetchVisualJobs()).toEqual([]);
    expect(await fetchAdAccounts()).toEqual([]);
    const summary = await fetchAdsSpendSummary();
    expect(summary.rows).toEqual([]);
    expect(summary.total_ad_spend).toBe("0.00");
    expect(summary.roas).toBe("0.00");
  });
});

// ---------------------------------------------------------------------------
// Client module: mock mode
// ---------------------------------------------------------------------------

describe("track9 client module (mock mode)", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
  });

  it("mutates the in-memory fixtures without touching the network", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    const jobs = await fetchVisualJobs();
    expect(jobs).toHaveLength(1);
    const rendered = await createVisualJob({
      provider: "sandbox",
      operation: "remove_bg",
      source_image_url: "https://cdn.example.com/a.png",
    });
    expect(rendered.status).toBe("completed");
    expect(rendered.output_image_url).toContain("data:image/png;base64,");
    expect(await fetchVisualJobs()).toHaveLength(2);
    await deleteVisualJob(rendered.id);
    expect(await fetchVisualJobs()).toHaveLength(1);

    const failed = await createVisualJob({
      provider: "remove_bg",
      operation: "remove_bg",
      source_image_url: "https://cdn.example.com/a.png",
    });
    expect(failed.status).toBe("failed");
    expect(failed.error_message).toContain("REMOVE_BG_API_KEY");

    expect(await fetchAdAccounts()).toHaveLength(1);
    const account = await connectAdAccount({
      platform: "amazon_ads",
      account_name: "Amazon 北美",
      external_account_id: "amz-1",
      access_token: "amz-token",
      currency: "usd",
    });
    expect(account.currency).toBe("USD");
    expect(account.has_credentials).toBe(true);
    expect(JSON.stringify(account)).not.toContain("amz-token");
    expect(await fetchAdAccounts()).toHaveLength(2);

    const synced = await syncAdAccount(account.id, 7);
    expect(synced.days).toBe(7);
    expect(synced.rows_upserted).toBe(3);
    await deleteAdAccount(account.id);
    expect(await fetchAdAccounts()).toHaveLength(1);

    const summary = await fetchAdsSpendSummary(30);
    expect(summary.roas).toBe("2.14");
    expect(summary.tacos_pct).toBe("25.13");
    expect(summary.rows).toHaveLength(3);

    expect(fetchMock).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Visual studio panel
// ---------------------------------------------------------------------------

describe("VisualStudioPanel", () => {
  it("renders the provider/operation selectors and a real <img> preview", async () => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
    renderUi(<VisualStudioPanel />);

    await waitFor(() => expect(screen.getByTestId("visual-studio-panel")).toBeInTheDocument());
    const providerSelect = screen.getByTestId("visual-provider-select") as HTMLSelectElement;
    expect(Array.from(providerSelect.options).map((option) => option.textContent)).toEqual([
      "Sandbox 沙箱预览",
      "remove.bg 去白底",
      "OpenAI 场景重绘",
    ]);
    const operationSelect = screen.getByTestId("visual-operation-select") as HTMLSelectElement;
    expect(Array.from(operationSelect.options).map((option) => option.textContent)).toEqual([
      "去白底 remove_bg",
      "换场景 scene_replace",
      "图片文字翻译 translate_overlay",
    ]);

    const image = (await screen.findByTestId("visual-job-image-vj-mock-1")) as HTMLImageElement;
    expect(image.getAttribute("src")).toContain("data:image/png;base64,");
    expect(image.getAttribute("alt")).toContain("去白底");
  });

  it("submits a job and deletes it again", async () => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
    renderUi(<VisualStudioPanel />);
    await waitFor(() => expect(screen.getByTestId("visual-studio-form")).toBeInTheDocument());

    fireEvent.change(screen.getByTestId("visual-provider-select"), {
      target: { value: "sandbox" },
    });
    fireEvent.change(screen.getByTestId("visual-operation-select"), {
      target: { value: "scene_replace" },
    });
    fireEvent.change(screen.getByTestId("visual-source-input"), {
      target: { value: "https://cdn.example.com/products/led-tube.png" },
    });
    fireEvent.change(screen.getByTestId("visual-prompt-input"), {
      target: { value: "warm living room" },
    });
    fireEvent.change(screen.getByTestId("visual-locale-input"), {
      target: { value: "es-MX" },
    });
    fireEvent.click(screen.getByTestId("visual-submit"));

    await waitFor(() => expect(screen.getByTestId("visual-job-vj-mock-2")).toBeInTheDocument());
    expect(screen.getByTestId("visual-job-image-vj-mock-2")).toBeInTheDocument();
    expect(screen.getByTestId("visual-job-vj-mock-2")).toHaveTextContent("es-MX");

    fireEvent.click(screen.getByTestId("visual-job-delete-vj-mock-2"));
    await waitFor(() =>
      expect(screen.queryByTestId("visual-job-vj-mock-2")).toBeNull()
    );
  });

  it("shows the vendor failure reason for a provider without a key", async () => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
    renderUi(<VisualStudioPanel />);
    await waitFor(() => expect(screen.getByTestId("visual-studio-form")).toBeInTheDocument());

    fireEvent.change(screen.getByTestId("visual-provider-select"), {
      target: { value: "remove_bg" },
    });
    fireEvent.click(screen.getByTestId("visual-submit"));

    await waitFor(() =>
      expect(screen.getByTestId("visual-studio-error")).toHaveTextContent("REMOVE_BG_API_KEY")
    );
    expect(screen.getByTestId("visual-failed-count")).toHaveTextContent("1");
  });

  it("locks the render action for FINANCE but keeps the job list readable", async () => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
    renderUi(<VisualStudioPanel />, "finance");
    await waitFor(() =>
      expect(screen.getByTestId("visual-job-vj-mock-1")).toBeInTheDocument()
    );
    expect(screen.getByTestId("visual-studio-panel")).toBeInTheDocument();
    expect(screen.getByTestId("visual-submit")).toBeDisabled();
    expect(screen.getByTestId("visual-job-delete-vj-mock-1")).toBeDisabled();
  });
});

// ---------------------------------------------------------------------------
// Ads spend panel
// ---------------------------------------------------------------------------

describe("AdsSpendPanel", () => {
  it("renders the KPI strip from backend decimal strings and the campaign table", async () => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
    renderUi(<AdsSpendPanel />);

    await waitFor(() =>
      expect(screen.getByTestId("ads-kpi-spend")).toHaveTextContent("USD 145.37")
    );
    expect(screen.getByTestId("ads-kpi-attributed")).toHaveTextContent("USD 311.60");
    expect(screen.getByTestId("ads-kpi-roas")).toHaveTextContent("2.14x");
    expect(screen.getByTestId("ads-kpi-tacos")).toHaveTextContent("25.13%");

    const table = screen.getByTestId("ads-spend-table");
    expect(within(table).getByText("QZ-LED Retarget 再营销")).toBeInTheDocument();
    expect(within(table).getByText("LED-TUBE-1200")).toBeInTheDocument();
    expect(within(table).getByText("USD 54.00")).toBeInTheDocument();
    expect(within(table).getByText("USD 118.80")).toBeInTheDocument();
    expect(screen.getByTestId("ads-window-badge")).toHaveTextContent("近 30 天");
  });

  it("connects an account, syncs with 立即同步广告数据 and deletes it", async () => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
    renderUi(<AdsSpendPanel />);
    await waitFor(() => expect(screen.getByTestId("ads-connect-form")).toBeInTheDocument());

    fireEvent.change(screen.getByTestId("ads-platform-select"), {
      target: { value: "amazon_ads" },
    });
    fireEvent.change(screen.getByTestId("ads-account-name-input"), {
      target: { value: "Amazon 北美" },
    });
    fireEvent.change(screen.getByTestId("ads-external-id-input"), {
      target: { value: "amz-7788" },
    });
    fireEvent.change(screen.getByTestId("ads-token-input"), {
      target: { value: "amz-secret-token" },
    });
    fireEvent.click(screen.getByTestId("ads-connect-submit"));

    const accountRow = await screen.findByTestId("ads-account-ad-mock-2");
    expect(accountRow).toHaveTextContent("Amazon 北美");
    // The token is never echoed back into the DOM.
    expect(accountRow.textContent).not.toContain("amz-secret-token");

    const syncButton = screen.getByTestId("ads-sync-ad-mock-2");
    expect(syncButton).toHaveTextContent("立即同步广告数据");
    fireEvent.click(syncButton);
    await waitFor(() => expect(syncButton).not.toBeDisabled());

    fireEvent.click(screen.getByTestId("ads-delete-ad-mock-2"));
    await waitFor(() => expect(screen.queryByTestId("ads-account-ad-mock-2")).toBeNull());
  });

  it("masks ad spend entirely for AGENT", () => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
    renderUi(<AdsSpendPanel />, "agent");
    expect(screen.getByTestId("ads-spend-masked")).toBeInTheDocument();
    expect(screen.queryByTestId("ads-spend-panel")).toBeNull();
    expect(screen.queryByTestId("ads-connect-form")).toBeNull();
  });

  it("lets FINANCE read the KPIs but not connect or sync an account", async () => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
    renderUi(<AdsSpendPanel />, "finance");
    await waitFor(() => expect(screen.getByTestId("ads-kpi-roas")).toHaveTextContent("2.14x"));
    expect(screen.getByTestId("ads-spend-panel")).toBeInTheDocument();
    expect(screen.getByTestId("ads-connect-submit")).toBeDisabled();
    expect(screen.getByTestId("ads-sync-ad-mock-1")).toBeDisabled();
    expect(screen.getByTestId("ads-delete-ad-mock-1")).toBeDisabled();
  });
});

// ---------------------------------------------------------------------------
// Page mounts
// ---------------------------------------------------------------------------

describe("track9 page mounts", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
  });

  it("mounts the visual studio panel on /ai/studio", async () => {
    renderUi(<AiStudioPage />);
    await waitFor(() => expect(screen.getByTestId("visual-studio-panel")).toBeInTheDocument());
    expect(screen.getByTestId("visual-studio-form")).toBeInTheDocument();
  });

  it("mounts the ad spend panel on /analytics", async () => {
    renderUi(<AnalyticsPage />, "manager");
    await waitFor(() =>
      expect(screen.getByTestId("ads-kpi-spend")).toHaveTextContent("USD 145.37")
    );
    expect(screen.getByTestId("ads-spend-panel")).toBeInTheDocument();
  });
});
