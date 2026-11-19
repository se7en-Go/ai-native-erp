// Phase 3B page tests: employees center + creative studio run entirely on
// fixtures (NEXT_PUBLIC_USE_MOCK_API=true) so the real UI code paths execute.
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import AiEmployeesPage from "@/app/ai/employees/page";
import AiStudioPage from "@/app/ai/studio/page";
import { FloatingCopilot } from "@/components/ai/floating-copilot";
import { AuthSessionContext, type AuthSessionState } from "@/components/providers";
import { I18nProvider } from "@/lib/i18n";
import type { Principal } from "@/lib/contracts";

const push = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace: vi.fn(), refresh: vi.fn(), prefetch: vi.fn() }),
  usePathname: () => "/ai/employees",
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

function renderPage(ui: React.ReactNode, role: Principal["role"]) {
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

describe("Phase 3B pages", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
    push.mockReset();
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("renders the five workforce KPIs from the real matrix fixture", async () => {
    renderPage(<AiEmployeesPage />, "owner");
    await waitFor(() =>
      expect(screen.getByRole("region", { name: "数字员工关键指标" })).toBeInTheDocument()
    );
    const kpis = screen.getByRole("region", { name: "数字员工关键指标" });
    for (const label of ["员工总数", "启用中", "近 7 天动作", "今日 LLM 成本", "自治模式"]) {
      expect(within(kpis).getByText(label)).toBeInTheDocument();
    }
    // Decimal money stays a string end to end.
    expect(within(kpis).getByText("0.184500 USD")).toBeInTheDocument();
    expect(within(kpis).getByText("286")).toBeInTheDocument();
    expect(
      await screen.findByRole("heading", { name: "AI 数字员工" })
    ).toBeInTheDocument();
    expect(screen.getByText("小光")).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent("[object Object]");
  });

  it("toggles an employee on/off and keeps the matrix navigating to real pages", async () => {
    const user = userEvent.setup();
    renderPage(<AiEmployeesPage />, "manager");
    await waitFor(() => expect(screen.getByText("小光")).toBeInTheDocument());

    const toggle = screen.getByRole("switch", { name: "启用小光" });
    expect(toggle).toBeChecked();
    await user.click(toggle);
    await waitFor(() => expect(screen.getByRole("switch", { name: "启用小光" })).not.toBeChecked());

    const board = await screen.findByRole("region", { name: "AI 自治分工矩阵" });
    expect(within(board).getByRole("heading", { name: "L2 全接管·AI 自动干" })).toBeInTheDocument();
    expect(within(board).getByRole("heading", { name: "L1 待确认·AI 提案人审" })).toBeInTheDocument();
    expect(within(board).getByRole("heading", { name: "L0 人工把关·AI 不碰" })).toBeInTheDocument();

    await user.click(within(board).getByRole("button", { name: /报价草稿/ }));
    expect(push).toHaveBeenCalledWith("/ai/proposals");

    await user.click(within(board).getByRole("button", { name: /财务收付与核销/ }));
    expect(push).toHaveBeenCalledWith("/ai/capabilities");
  });

  it("lets agents read the workforce but not toggle employees", async () => {
    renderPage(<AiEmployeesPage />, "agent");
    await waitFor(() => expect(screen.getByText("小光")).toBeInTheDocument());
    expect(screen.getByTestId("employees-read-only")).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "启用小光" })).toBeDisabled();
  });

  it("generates, saves and approves a studio asset against the real SKU list", async () => {
    const user = userEvent.setup();
    renderPage(<AiStudioPage />, "manager");
    await waitFor(() => expect(screen.getByTestId("studio-asset-library")).toBeInTheDocument());

    const skuSelect = await screen.findByLabelText("关联 SKU");
    await waitFor(() => expect(within(skuSelect).getAllByRole("option").length).toBeGreaterThan(1));
    await user.selectOptions(skuSelect, "LED-COB-24V-5M");
    fireEvent.change(screen.getByLabelText("创作要求"), {
      target: { value: "为德国经销商写商品文案" },
    });
    await user.click(screen.getByRole("button", { name: /生成文案/ }));

    await waitFor(() =>
      expect((screen.getByLabelText("生成结果") as HTMLTextAreaElement).value).toContain(
        "LED-COB-24V-5M"
      )
    );

    await user.click(screen.getByRole("button", { name: /保存素材/ }));
    await waitFor(() =>
      expect(screen.getByTestId("studio-asset-library")).toHaveTextContent("草稿")
    );

    // The seeded listing asset is already approved; the draft copy can be approved.
    expect(screen.getByTestId("studio-asset-asset-2")).toHaveTextContent("已批准");
    await user.click(screen.getByTestId("approve-asset-asset-1"));
    await waitFor(() =>
      expect(screen.getByTestId("studio-asset-asset-1")).toHaveTextContent("已批准")
    );
  });

  it("keeps the inbox toolbar wired to the mock assist layer inside the composer", async () => {
    const { InboxComposer } = await import("@/components/inbox/composer");
    renderPage(
      <InboxComposer
        conversation={{
          id: "conv-1",
          customer_id: "cust-1",
          customer_name: "Hans Weber",
          country_code: "DE",
          iana_timezone: "Europe/Berlin",
          channel_account_id: "acct-1",
          channel_type: "email",
          conversation_mode: "b2b_inquiry",
          status: "pending_human",
          unread_count: 1,
          sla_due_at: null,
          csw_expires_at: null,
          human_agent_window_expires_at: null,
          last_message_preview: "Please quote 500 pcs",
          updated_at: "2026-11-19T08:00:00Z",
        }}
        messages={[
          {
            id: "msg-1",
            conversation_id: "conv-1",
            channel_type: "email",
            direction: "inbound",
            sender_type: "customer",
            content_type: "text",
            body_text: "Please quote 500 pcs LED-COB-24V-5M FOB Shenzhen",
            delivery_status: "read",
            source_timestamp: "2026-11-19T08:00:00Z",
          },
        ]}
      />,
      "agent"
    );

    expect(await screen.findByRole("group", { name: "收件箱 AI 快捷工具" })).toBeInTheDocument();
    fireEvent.change(screen.getByTestId("composer-textarea"), {
      target: { value: "请帮我润色这句" },
    });
    fireEvent.click(screen.getByRole("button", { name: /AI 润色/ }));

    await waitFor(() =>
      expect((screen.getByTestId("composer-textarea") as HTMLTextAreaElement).value).toContain(
        "感谢您的来信"
      )
    );
    // The toolbar only edits the draft; nothing is sent automatically.
    expect(screen.getByTestId("composer-assist-notice")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /快捷短语/ }));
    const dialog = await screen.findByRole("dialog", { name: "快捷短语" });
    fireEvent.click(within(dialog).getByText("MOQ 起订量回复"));
    await waitFor(() =>
      expect((screen.getByTestId("composer-textarea") as HTMLTextAreaElement).value).toContain(
        "起订量为 100 条起"
      )
    );
  });

  it("renders formatted copilot quick chips and a plain-text AI brief", async () => {
    const user = userEvent.setup();
    renderPage(<FloatingCopilot />, "owner");

    fireEvent.click(await screen.findByTestId("copilot-fab-btn"));
    for (const chip of ["daily-brief", "pending-proposals", "today-followups", "stock-alerts"]) {
      expect(screen.getByTestId(`copilot-chip-${chip}`)).toBeInTheDocument();
    }
    expect(screen.getByTestId("copilot-chip-daily-brief")).toHaveTextContent("AI 日报");

    await user.click(screen.getByTestId("copilot-chip-daily-brief"));
    await waitFor(() => expect(screen.getByTestId("copilot-brief")).toBeInTheDocument());
    const brief = screen.getByTestId("copilot-brief");
    expect(brief).toHaveTextContent("今日 AI 日报");
    expect(brief).toHaveTextContent("待审提案");
    expect(brief).not.toHaveTextContent("[object Object]");
    // Navigation chips keep their real destinations.
    expect(screen.getByTestId("copilot-chip-pending-proposals")).toHaveAttribute(
      "href",
      "/ai/proposals"
    );
    expect(screen.getByTestId("copilot-chip-stock-alerts")).toHaveAttribute("href", "/wms");
    expect(screen.getByTestId("copilot-pending-link")).toBeInTheDocument();
  });
});
