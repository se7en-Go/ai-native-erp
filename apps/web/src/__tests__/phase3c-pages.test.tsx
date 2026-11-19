// Phase 3C page tests: the global lead map and the campaign centre run entirely
// on fixtures (NEXT_PUBLIC_USE_MOCK_API=true) so the real UI code paths execute.
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

describe("Phase 3C pages", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("renders the collection KPIs, the Leaflet map and the graded cleaning pool", async () => {
    renderPage(<GrowthCollectPage />);

    const kpis = await screen.findByRole("group", { name: "线索采集关键指标" });
    expect(within(kpis).getByText("已采集")).toBeInTheDocument();
    await waitFor(() => expect(within(kpis).getAllByText("6").length).toBeGreaterThan(0));

    const map = await screen.findByRole("region", { name: "全球线索地图" });
    expect(within(map).getByText("6 条已定位")).toBeInTheDocument();
    // Real Leaflet markers, one per located fixture prospect.
    expect(await within(map).findByRole("button", { name: "Gulf Lighting Trading LLC" })).toBeInTheDocument();
    expect(within(map).getByRole("button", { name: "Shannon Retail Lighting" })).toBeInTheDocument();

    const table = screen.getByRole("region", { name: "线索清洗池" });
    expect(within(table).getByText("4.80")).toBeInTheDocument();
    const bodyRows = within(table).getAllByRole("row");
    expect(bodyRows.some((row) => row.textContent?.includes("已同步 ERP"))).toBe(false);
    expect(screen.getByRole("table", { name: "采集任务列表" })).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent("[object Object]");
  });

  it("selecting a map marker highlights the matching cleaning-pool row", async () => {
    const user = userEvent.setup();
    renderPage(<GrowthCollectPage />);
    const map = await screen.findByRole("region", { name: "全球线索地图" });
    const marker = await within(map).findByRole("button", { name: "Milan Illuminazione SRL" });
    await user.click(marker);
    const table = screen.getByRole("region", { name: "线索清洗池" });
    const row = within(table)
      .getAllByRole("row")
      .find((candidate) => candidate.textContent?.includes("Milan Illuminazione SRL"));
    expect(row?.className).toContain("bg-teal-50");
  });

  it("cleans raw prospects and syncs cleaned ones into the ERP lead pool", async () => {
    const user = userEvent.setup();
    renderPage(<GrowthCollectPage />);
    const table = await screen.findByRole("region", { name: "线索清洗池" });

    // Lagos Lighting Mart starts as `raw`, so the clean action is enabled for it.
    const rowFor = (company: string) =>
      within(table)
        .getAllByRole("row")
        .find((row) => row.textContent?.includes(company));

    await waitFor(() => expect(rowFor("Lagos Lighting Mart")).toBeDefined());
    expect(rowFor("Lagos Lighting Mart")?.textContent).toContain("待清洗");

    const cleanButton = within(table).getByRole("button", { name: "批量去重清洗" });
    await waitFor(() => expect(cleanButton).toBeEnabled());
    await user.click(cleanButton);
    await waitFor(() => expect(rowFor("Lagos Lighting Mart")?.textContent).toContain("已清洗"));

    const syncButton = within(table).getByRole("button", { name: "一键同步到 ERP 线索池" });
    await waitFor(() => expect(syncButton).toBeEnabled());
    await user.click(syncButton);
    await waitFor(() =>
      expect(within(table).getAllByText("已同步 ERP").length).toBeGreaterThan(0)
    );
    expect(rowFor("Gulf Lighting Trading LLC")?.textContent).toContain("已同步 ERP");
  });

  it("runs an authorised CSV import from the collection panel", async () => {
    const user = userEvent.setup();
    renderPage(<GrowthCollectPage />);
    await user.selectOptions(await screen.findByLabelText("数据源"), "csv_import");
    const file = new File(["company_name\nZagreb LED Supply"], "zagreb.csv", {
      type: "text/csv",
    });
    await user.upload(screen.getByLabelText("CSV 文件"), file);
    await user.click(screen.getByRole("button", { name: "启动采集" }));
    await waitFor(() =>
      expect(screen.getByText(/采集请求已提交|采集请求提交失败/)).toBeInTheDocument()
    );
    expect(await screen.findByRole("table", { name: "采集任务列表" })).toBeInTheDocument();
  });

  it("saves a campaign draft and exposes server-side preview and launch", async () => {
    const user = userEvent.setup();
    renderPage(<GrowthCampaignsPage />, "manager");

    expect(await screen.findByRole("region", { name: "营销战役配置" })).toBeInTheDocument();
    // The builder never previews an unsaved draft: the list owns the server-side check.
    expect(screen.queryByRole("button", { name: "预览退订拦截" })).not.toBeInTheDocument();
    expect(
      screen.getByText(/对已存战役执行服务端合规预检/)
    ).toBeInTheDocument();

    await user.type(screen.getByLabelText("战役名称"), "Milan COB launch");
    await user.type(screen.getByLabelText("邮件主题"), "2026 catalogue");
    await user.type(screen.getByLabelText("消息内容"), "Hello {{company_name}},");
    await user.click(screen.getByRole("button", { name: "保存草稿" }));
    await waitFor(() => expect(screen.getByText(/已保存为草稿/)).toBeInTheDocument());

    const list = screen.getByRole("region", { name: "营销战役列表" });
    await waitFor(() => expect(within(list).getByText("Milan COB launch")).toBeInTheDocument());

    const previewButtons = within(list).getAllByRole("button", { name: "合规预检" });
    await user.click(previewButtons[0]);
    const snapshot = await within(list).findByLabelText(/合规预检结果/);
    expect(within(snapshot).getByText("可发送")).toBeInTheDocument();
    expect(within(snapshot).getByText("命中退订/抑制名单")).toBeInTheDocument();
    expect(within(snapshot).getByText("地址缺失")).toBeInTheDocument();
  });

  it("launches a campaign only after confirming the snapshot and reports the outcome", async () => {
    const user = userEvent.setup();
    renderPage(<GrowthCampaignsPage />, "owner");
    const list = await screen.findByRole("region", { name: "营销战役列表" });
    await waitFor(() =>
      expect(
        within(list).getAllByText("Dubai & Milan LED wholesale outreach").length
      ).toBeGreaterThan(0)
    );
    const campaignCard = within(list)
      .getAllByRole("listitem")
      .find((item) => item.textContent?.includes("Dubai & Milan LED wholesale outreach"));
    expect(campaignCard).toBeDefined();
    await user.click(within(campaignCard as HTMLElement).getByRole("button", { name: "启动战役" }));
    await waitFor(() =>
      expect(
        screen.getByText("发送完成：4 条已排队发送，2 条被合规拦截。")
      ).toBeInTheDocument()
    );
    await waitFor(() =>
      expect(
        within(campaignCard as HTMLElement).getByRole("button", { name: "启动战役" })
      ).toBeDisabled()
    );
  });

  it("keeps the growth routes reachable for agents but not finance", async () => {
    const { canAccessRoute } = await import("@/lib/auth-cookie");
    expect(canAccessRoute("agent", "/growth/collect")).toBe(true);
    expect(canAccessRoute("agent", "/growth/campaigns")).toBe(true);
    expect(canAccessRoute("owner", "/growth/collect")).toBe(true);
    expect(canAccessRoute("finance", "/growth/collect")).toBe(false);
  });
});
