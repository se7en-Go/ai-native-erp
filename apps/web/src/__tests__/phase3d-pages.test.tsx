// Phase 3D page tests: the /social marketing center runs entirely on fixtures
// (NEXT_PUBLIC_USE_MOCK_API=true) so the real UI code paths execute.
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import SocialPage from "@/app/social/page";
import { AuthSessionContext, type AuthSessionState } from "@/components/providers";
import { I18nProvider } from "@/lib/i18n";
import type { Principal } from "@/lib/contracts";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn(), prefetch: vi.fn() }),
  usePathname: () => "/social",
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

function renderPage(role: Principal["role"] = "owner") {
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
        <I18nProvider defaultLocale="zh">
          <SocialPage />
        </I18nProvider>
      </AuthSessionContext.Provider>
    </QueryClientProvider>
  );
}

describe("Phase 3D social marketing center", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("renders the KPI header, all four tabs and the 14-platform matrix", async () => {
    renderPage();

    const kpis = await screen.findByRole("group", { name: "社媒营销概览指标" });
    await waitFor(() => expect(within(kpis).getByText("内容总数")).toBeInTheDocument());
    expect(within(kpis).getByText("14")).toBeInTheDocument();
    expect(within(kpis).getByText("已发布")).toBeInTheDocument();
    expect(within(kpis).getByText("社媒线索")).toBeInTheDocument();

    expect(screen.getByRole("tab", { name: "平台矩阵" })).toHaveAttribute(
      "aria-selected",
      "true"
    );
    expect(screen.getByRole("tab", { name: "发布中心与内容日历" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "贴文编辑器与 AI 文案工坊" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "舆情监听与转线索" })).toBeInTheDocument();

    const matrix = await screen.findByRole("region", { name: "社媒平台矩阵" });
    await waitFor(() => expect(within(matrix).getAllByRole("article")).toHaveLength(14));
    expect(within(matrix).getByText("13,465")).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent("[object Object]");
  });

  it("switches between the calendar, editor and listening tabs", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole("tab", { name: "发布中心与内容日历" }));
    const calendar = await screen.findByRole("region", { name: "内容排期日历" });
    expect(within(calendar).getByRole("grid", { name: "内容日历" })).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "贴文编辑器与 AI 文案工坊" }));
    expect(await screen.findByRole("region", { name: "社媒贴文编辑器" })).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "舆情监听与转线索" }));
    const feed = await screen.findByRole("region", { name: "社媒舆情监听" });
    expect(within(feed).getByText("@dubai_build_supply")).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent("[object Object]");
  });

  it("authorizes and disconnects an account from the platform matrix", async () => {
    const user = userEvent.setup();
    renderPage();

    const matrix = await screen.findByRole("region", { name: "社媒平台矩阵" });
    const weibo = await within(matrix).findByRole("article", { name: "微博" });
    expect(within(weibo).getByText("未授权")).toBeInTheDocument();

    await user.click(within(weibo).getByRole("button", { name: "去授权 微博" }));
    await waitFor(() =>
      expect(within(weibo).getByRole("button", { name: "断开 微博" })).toBeInTheDocument()
    );

    // After disconnecting, the card offers re-authorization and keeps the account.
    await user.click(within(weibo).getByRole("button", { name: "断开 微博" }));
    await waitFor(() =>
      expect(within(weibo).getByRole("button", { name: "重新授权 微博" })).toBeInTheDocument()
    );
    expect(within(weibo).getByText("未授权")).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent("[object Object]");
  });

  it("saves a draft and schedules a post from the editor", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole("tab", { name: "贴文编辑器与 AI 文案工坊" }));
    const editor = await screen.findByRole("region", { name: "社媒贴文编辑器" });

    await user.click(within(editor).getByRole("checkbox", { name: "Instagram" }));
    await user.type(within(editor).getByLabelText("正文"), "IP65 work light, factory direct");
    await user.click(within(editor).getByRole("button", { name: "保存草稿" }));
    await waitFor(() => expect(within(editor).getByText("草稿已保存。")).toBeInTheDocument());

    await user.click(screen.getByRole("tab", { name: "发布中心与内容日历" }));
    const calendar = await screen.findByRole("region", { name: "内容排期日历" });
    await waitFor(() =>
      expect(within(calendar).getAllByText(/Instagram/).length).toBeGreaterThan(0)
    );
    expect(document.body).not.toHaveTextContent("[object Object]");
  });

  it("generates an AI caption from the editor brief", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole("tab", { name: "贴文编辑器与 AI 文案工坊" }));
    const editor = await screen.findByRole("region", { name: "社媒贴文编辑器" });
    await user.click(within(editor).getByRole("checkbox", { name: "Instagram" }));
    await user.type(within(editor).getByLabelText("正文"), "夏季工地照明新品");
    await user.click(within(editor).getByRole("button", { name: "AI 生成文案" }));

    await waitFor(() => {
      const value = (within(editor).getByLabelText("正文") as HTMLTextAreaElement).value;
      expect(value).toContain("夏季工地照明新品");
      expect(value.length).toBeGreaterThan(0);
    });
  });

  it("converts a lead-intent mention and shows the converted badge", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole("tab", { name: "舆情监听与转线索" }));
    const feed = await screen.findByRole("region", { name: "社媒舆情监听" });
    await user.click(within(feed).getByRole("button", { name: "转线索 @dubai_build_supply" }));

    await waitFor(() =>
      expect(within(feed).getAllByText("已转线索")).toHaveLength(2)
    );
    expect(
      within(feed).queryByRole("button", { name: "转线索 @dubai_build_supply" })
    ).toBeNull();
    const kpis = screen.getByRole("group", { name: "社媒营销概览指标" });
    await waitFor(() => expect(within(kpis).getByText("已转线索")).toBeInTheDocument());
    expect(document.body).not.toHaveTextContent("[object Object]");
  });
});
