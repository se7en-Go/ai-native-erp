// Pack C UI tests: multi-city lead collection, published-website e-mail
// enrichment and real email outreach (Resend / SMTP). Pages run on fixtures
// (NEXT_PUBLIC_USE_MOCK_API=true) so the real UI code paths execute.
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import GrowthCollectPage from "@/app/growth/collect/page";
import GrowthCampaignsPage from "@/app/growth/campaigns/page";
import { AuthSessionContext, type AuthSessionState } from "@/components/providers";
import { I18nProvider } from "@/lib/i18n";
import type { Principal } from "@/lib/contracts";
import {
  createAndRunBatchCollection,
  emailEngineLabel,
  enrichProspectEmails,
  fetchEmailSenderAccounts,
  jobSourcePills,
  parseCities,
  parseKeywords,
  sendCampaignReal,
  sourceLabel,
} from "@/lib/api-client/pack-c-growth";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn(), prefetch: vi.fn() }),
  usePathname: () => "/growth/collect",
}));
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

function renderPage(ui: React.ReactNode, role: Principal["role"] = "owner") {
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

describe("Pack C growth pages", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("parses multi-city / multi-keyword input and runs one batch collection", async () => {
    const user = userEvent.setup();
    renderPage(<GrowthCollectPage />);

    const batch = await screen.findByRole("region", { name: "多城市批量采集" });
    const cities = within(batch).getByTestId("collect-cities-input");
    const keywords = within(batch).getByTestId("collect-keywords-input");
    const mode = within(batch).getByTestId("collect-provider-mode") as HTMLSelectElement;
    expect(mode.value).toBe("auto");

    await user.type(cities, "Dubai, Riyadh{enter}Warsaw");
    await user.type(keywords, "LED strip wholesaler, lighting distributor");
    await user.selectOptions(mode, "overpass");
    await user.click(within(batch).getByRole("button", { name: "开始批量采集" }));

    const feedback = await within(batch).findByRole("status");
    expect(feedback).toHaveTextContent(/覆盖 3 个城市 · 2 个关键词/);
    expect(feedback).toHaveTextContent(/OSM Overpass/);
  });

  it("keeps the free no-key source as the default and states the compliance boundary", async () => {
    renderPage(<GrowthCollectPage />);

    const mode = await screen.findByTestId("collect-provider-mode");
    expect(within(mode).getAllByRole("option").map((option) => option.textContent)).toEqual([
      "自动（有 Google Key 用 Places，无 Key 免费用 OSM Overpass）",
      "OpenStreetMap Overpass（免费免 Key）",
      "Google Places API",
    ]);

    const notice = screen.getByTestId("collect-compliance-notice");
    expect(notice).toHaveTextContent(/不抓取 WhatsApp \/ Facebook 私域数据/);
    expect(notice).toHaveTextContent(/封号风险/);
    expect(notice).toHaveTextContent(/官网/);
  });

  it("extracts published website mailboxes for prospects missing an e-mail", async () => {
    const user = userEvent.setup();
    renderPage(<GrowthCollectPage />);

    const panel = await screen.findByRole("region", { name: "线索邮箱提取" });
    const button = within(panel).getByTestId("enrich-emails-btn");
    await waitFor(() => expect(button).toBeEnabled());
    await user.click(button);

    const summary = await within(panel).findByTestId("enrich-emails-summary");
    expect(summary).toHaveTextContent("已扫描 1 家官网，成功提取 1 个采购邮箱并更新评级");
    // The mailbox the fixture website publishes becomes visible in the row.
    expect(within(panel).getByText("sales@lagos-lighting.example.ng")).toBeInTheDocument();
    expect(summary).toHaveTextContent(/1 家缺官网/);
  });

  it("only scans ticked prospects and never overwrites an existing mailbox", async () => {
    const user = userEvent.setup();
    renderPage(<GrowthCollectPage />);

    const panel = await screen.findByRole("region", { name: "线索邮箱提取" });
    await user.click(
      await within(panel).findByLabelText("选择 Gulf Lighting Trading LLC 用于邮箱提取")
    );
    await user.click(within(panel).getByTestId("enrich-emails-btn"));

    const summary = await within(panel).findByTestId("enrich-emails-summary");
    expect(summary).toHaveTextContent("已扫描 0 家官网，成功提取 0 个采购邮箱并更新评级");
    expect(summary).toHaveTextContent(/1 家已有邮箱/);
    // The existing mailbox stays untouched.
    expect(within(panel).getByText("aisha@gulf-lighting.example.ae")).toBeInTheDocument();
  });

  it("labels every job row with the provider that actually produced the leads", async () => {
    renderPage(<GrowthCollectPage />);

    const jobs = await screen.findByRole("table", { name: "采集任务列表" });
    await waitFor(() =>
      expect(within(jobs).getAllByTestId("job-source-pill").length).toBeGreaterThan(0)
    );
    const pill = within(jobs).getAllByTestId("job-source-pill")[0];
    expect(pill).toHaveTextContent("Overture POI");
    expect(pill).toHaveTextContent("6");
  });

  it("offers a real Resend/SMTP dry run and then a live send on email campaigns", async () => {
    const user = userEvent.setup();
    renderPage(<GrowthCampaignsPage />, "owner");

    const list = await screen.findByRole("region", { name: "营销战役列表" });
    const panel = (await within(list).findAllByTestId("campaign-real-send-panel"))[0];
    expect(panel).toBeDefined();
    expect(within(panel).getByText(/真实邮件发信（Resend \/ SMTP）/)).toBeInTheDocument();

    const mailbox = within(panel).getByTestId("campaign-mailbox-select");
    expect(
      within(mailbox).getByRole("option", { name: "自动选择默认发件邮箱" })
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(
        within(mailbox).getByRole("option", { name: "301Go Export Sales (sales@301go.net)" })
      ).toBeInTheDocument()
    );

    await user.click(within(panel).getByTestId("campaign-dry-run-btn"));
    const result = await within(panel).findByTestId("campaign-real-send-result");
    expect(result).toHaveTextContent("已发送 4 · 退订拦截 2 · 失败 0");
    expect(result).toHaveTextContent("发件邮箱");
    expect(result).toHaveTextContent("引擎");
    expect(result).toHaveTextContent("Resend");
    expect(within(panel).getByText("试算结果")).toBeInTheDocument();

    await user.selectOptions(mailbox, "mock-email-account-gmail");
    await user.click(within(panel).getByTestId("campaign-real-send-btn"));
    await waitFor(() =>
      expect(within(panel).getByTestId("campaign-real-send-result")).toHaveTextContent("SMTP")
    );
    expect(within(panel).getByText("真实发送结果")).toBeInTheDocument();
    expect(screen.getByText(/真实发信完成：已发送 4 封/)).toBeInTheDocument();
  });

  it("only renders the real-send panel for email campaigns", async () => {
    renderPage(<GrowthCampaignsPage />, "manager");
    const list = await screen.findByRole("region", { name: "营销战役列表" });
    await waitFor(() =>
      expect(within(list).getAllByTestId("campaign-real-send-panel").length).toBe(1)
    );
    // The single fixture campaign is an email campaign, so the panel carries its id.
    expect(within(list).getAllByTestId("campaign-real-send-panel")[0]).toHaveAttribute(
      "data-campaign-id",
      "campaign-dubai-milan"
    );
  });
});

describe("pack C growth adapters", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("normalises comma/newline city and keyword input", () => {
    expect(parseCities("Dubai, Riyadh\nWarsaw; dubai")).toEqual(["Dubai", "Riyadh", "Warsaw"]);
    expect(parseKeywords("LED strip wholesaler， lighting   distributor")).toEqual([
      "LED strip wholesaler",
      "lighting distributor",
    ]);
    const tooMany = Array.from({ length: 14 }, (_, index) => `City ${index}`).join(",");
    expect(parseCities(tooMany)).toHaveLength(10);
  });

  it("maps provider keys onto operator-facing labels", () => {
    expect(sourceLabel("overpass_osm")).toBe("OSM Overpass");
    expect(sourceLabel("google_places")).toBe("Google Places");
    expect(sourceLabel("fallback_catalog")).toBe("示例目录");
    expect(sourceLabel("fallback_catalog", "en")).toBe("Sample catalogue");
    expect(sourceLabel("some_new_provider")).toBe("some_new_provider");
    expect(emailEngineLabel("smtp_imap")).toBe("SMTP");
    expect(emailEngineLabel("resend")).toBe("Resend");
    expect(emailEngineLabel("")).toBe("Resend / SMTP");
  });

  it("prefers the real per-record sources and falls back to the requested provider", () => {
    expect(
      jobSourcePills({
        source_provider: "overpass_osm",
        sources: { overpass_osm: 3, fallback_catalog: 1 },
      })
    ).toEqual([
      { key: "overpass_osm", count: 3 },
      { key: "fallback_catalog", count: 1 },
    ]);
    expect(jobSourcePills({ source_provider: "google_places", sources: {} })).toEqual([
      { key: "google_places", count: null },
    ]);
  });

  it("creates and runs a batch collection in one gesture", async () => {
    const job = await createAndRunBatchCollection({
      cities: ["Dubai", "Riyadh"],
      keywords: ["LED strip wholesaler"],
      provider_mode: "google_places",
      max_results: 40,
    });
    expect(job.source_provider).toBe("google_places");
    expect(job.status).toBe("completed");
    expect(job.target_region).toBe("Dubai / Riyadh");
    expect(job.sources).toEqual({ google_places: 4 });
  });

  it("reports scanned websites and extracted mailboxes truthfully", async () => {
    const response = await enrichProspectEmails([
      "prospect-lagos",
      "prospect-dublin",
      "prospect-lagos",
    ]);
    expect(response.checked_count).toBe(1);
    expect(response.enriched_count).toBe(1);
    const statuses = new Map(response.items.map((item) => [item.prospect_id, item.status]));
    expect(statuses.get("prospect-dublin")).toBe("skipped_no_website");
    expect(statuses.get("prospect-lagos")).toBe("enriched");
    expect(response.items.filter((item) => item.prospect_id === "prospect-lagos")).toHaveLength(1);
  });

  it("lists active email mailboxes and resolves the delivery engine per mailbox", async () => {
    const mailboxes = await fetchEmailSenderAccounts();
    expect(mailboxes.map((mailbox) => mailbox.mailbox)).toEqual([
      "sales@301go.net",
      "sales.li@gmail.com",
    ]);
    expect(mailboxes[0].label).toBe("301Go Export Sales (sales@301go.net)");

    const dryRun = await sendCampaignReal("campaign-dubai-milan", {
      channel_account_id: null,
      dry_run: true,
    });
    expect(dryRun).toMatchObject({
      campaign_id: "campaign-dubai-milan",
      dry_run: true,
      provider: "resend",
      sent_count: 4,
      suppressed_count: 2,
      failed_count: 0,
    });

    const smtp = await sendCampaignReal("campaign-dubai-milan", {
      channel_account_id: "mock-email-account-gmail",
    });
    expect(smtp.provider).toBe("smtp_imap");
    expect(smtp.dry_run).toBe(false);
    expect(smtp.from_email).toContain("sales.li@gmail.com");
  });
});
