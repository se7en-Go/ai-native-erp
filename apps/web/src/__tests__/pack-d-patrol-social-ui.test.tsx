// Pack D UI tests: AI proactive patrol copilot, custom AI employees and the
// 14-platform social playbook matrix.
//
// The client-module cases run the real `bffFetch` path so the URLs/methods/bodies
// are verified against the locked contract; every page case runs the real UI code
// against the `NEXT_PUBLIC_USE_MOCK_API=true` fixtures.
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import AiEmployeesPage from "@/app/ai/employees/page";
import SocialPage from "@/app/social/page";
import { FloatingCopilot } from "@/components/ai/floating-copilot";
import { AuthSessionContext, type AuthSessionState } from "@/components/providers";
import { I18nProvider } from "@/lib/i18n";
import type { Principal } from "@/lib/contracts";
import {
  createCustomAiEmployee,
  dispatchPatrolEmployee,
  fetchDailyPatrolBrief,
  fetchSocialPlaybooks,
  toAiEmployeeCreatePayload,
} from "@/lib/api-client/pack-d-patrol";
import type { AiEmployeeCreate } from "@/lib/api-client/types.gen";
import {
  fetchCallBody,
  fetchCallMethod,
  fetchCallUrl,
  normalizeFetchInput,
} from "@/__tests__/fetch-stub";

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

function renderWithProviders(ui: React.ReactNode, role: Principal["role"] = "owner") {
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

/** Copilot chrome queries (capabilities / proposals / wallet) still need a stub. */
function stubCopilotChrome(fetchMock = vi.fn()): void {
  fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
    const { url } = normalizeFetchInput(input);
    if (url.includes("/ai/proposals")) {
      return new Response(JSON.stringify({ items: [], total: 0, limit: 1, offset: 0, has_more: false }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (url.includes("/ai/capabilities")) {
      return new Response(JSON.stringify({ capabilities: [], l0_fallback: false }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (url.includes("/billing/wallet")) {
      return new Response(
        JSON.stringify({
          plan_tier: "Growth",
          included_credits_remaining: 1200,
          topup_credits_balance: 0,
          auto_downgrade_to_l0: false,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      );
    }
    return new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fetchMock);
}

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("Pack D client module (live bffFetch contract)", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "false");
  });

  it("reads the daily patrol brief through the BFF proxy", async () => {
    const fetchMock = vi.fn(async () =>
      new Response(
        JSON.stringify({
          generated_at: "2026-11-19T09:20:00Z",
          health_score: 91,
          summary_zh: "一切正常",
          summary_en: "All good",
          metrics: { expiring_quotes_count: 0 },
          alerts: [],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      )
    );
    vi.stubGlobal("fetch", fetchMock);

    const brief = await fetchDailyPatrolBrief();

    expect(brief.health_score).toBe(91);
    const call = fetchMock.mock.calls[0];
    expect(fetchCallUrl(call)).toBe("/api/bff/v1/ai/patrol/daily-brief");
    expect(fetchCallMethod(call) ?? "GET").not.toBe("POST");
  });

  it("dispatches an employee with a POST to the encoded code path", async () => {
    const fetchMock = vi.fn(async () =>
      new Response(
        JSON.stringify({
          employee_code: "b2b-sales",
          employee_name: "小光",
          employee_role_title: "B2B 外贸业务员",
          generated_at: "2026-11-19T09:20:00Z",
          action_summary_zh: "已创建跟进任务",
          action_summary_en: "Task created",
          metrics: {},
          findings: [],
          created_task_id: null,
          recommended_href: "/b2b/quotes",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      )
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await dispatchPatrolEmployee("b2b-sales");

    expect(result.recommended_href).toBe("/b2b/quotes");
    const call = fetchMock.mock.calls[0];
    expect(fetchCallUrl(call)).toBe("/api/bff/v1/ai/patrol/dispatch/b2b-sales");
    expect(fetchCallMethod(call)).toBe("POST");
  });

  it("creates a custom AI employee with the exact AiEmployeeCreate payload", async () => {
    const fetchMock = vi.fn(async () =>
      new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } })
    );
    vi.stubGlobal("fetch", fetchMock);

    const payload: AiEmployeeCreate = {
      code: "custom-led-sales",
      display_name: "小灯",
      role_title: "照明方案顾问",
      department: "商务中心",
      description: "只回答照明选型问题。",
      capability_codes: ["sales_quoting"],
      autonomy_level: "L1_proposal",
      system_persona: "报价必须引用实时 ERP 数据。",
      is_enabled: true,
    };

    await createCustomAiEmployee(payload);

    const call = fetchMock.mock.calls[0];
    expect(fetchCallUrl(call)).toBe("/api/bff/v1/ai/employees");
    expect(fetchCallMethod(call)).toBe("POST");
    expect(JSON.parse(await fetchCallBody(call))).toEqual(payload);
  });

  it("filters social playbooks by the region query parameter", async () => {
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify([]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    await fetchSocialPlaybooks("domestic");

    expect(fetchCallUrl(fetchMock.mock.calls[0])).toBe(
      "/api/bff/v1/social/playbooks?region=domestic"
    );
  });
});

describe("Pack D fixtures", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
  });

  it("serves the six patrol alert classes with a real health score", async () => {
    const brief = await fetchDailyPatrolBrief();
    expect(brief.health_score).toBe(78);
    expect(brief.alerts.map((alert) => alert.code)).toEqual([
      "expiring_quotes",
      "low_stock_skus",
      "stalled_shipments",
      "pending_payments",
      "pending_proposals",
      "unread_conversations",
    ]);
    expect(brief.alerts.every((alert) => Boolean(alert.employee_code))).toBe(true);
  });

  it("splits the 14 platforms into 7 overseas and 7 domestic playbooks", async () => {
    const all = await fetchSocialPlaybooks("all");
    const overseas = await fetchSocialPlaybooks("overseas");
    const domestic = await fetchSocialPlaybooks("domestic");

    expect(all).toHaveLength(14);
    expect(overseas).toHaveLength(7);
    expect(domestic).toHaveLength(7);
    // Publishing capability follows the locked playbook, never a UI guess.
    expect(all.find((row) => row.platform === "xiaohongshu")?.official_api_supported).toBe(false);
    expect(all.find((row) => row.platform === "instagram")?.official_api_supported).toBe(true);
    expect(all.every((row) => row.best_practices_zh.length > 0)).toBe(true);
    expect(all.every((row) => row.best_practices_en.length > 0)).toBe(true);
  });

  it("returns dispatch findings plus a follow-up task for a known employee", async () => {
    const result = await dispatchPatrolEmployee("logistics-forwarder");
    expect(result.employee_name).toBe("小海");
    expect(result.created_task_id).toBeTruthy();
    expect(result.findings[0].count).toBe(2);
    expect(result.recommended_href).toBe("/orders");
  });
});

describe("FloatingCopilot 主动巡检日报", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
    stubCopilotChrome();
  });

  it("keeps AI 对话 as the default panel and the chat input always mounted", async () => {
    renderWithProviders(<FloatingCopilot />);
    fireEvent.click(await screen.findByTestId("copilot-fab-btn"));

    expect(screen.getByTestId("copilot-tab-chat")).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("copilot-tab-patrol")).toHaveAttribute("aria-selected", "false");
    expect(screen.getByTestId("copilot-query-input")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("copilot-tab-patrol"));
    await waitFor(() => expect(screen.getByTestId("copilot-patrol-panel")).toBeInTheDocument());
    // The bottom composer stays available while the patrol panel is open.
    expect(screen.getByTestId("copilot-query-input")).toBeInTheDocument();
    expect(screen.queryByTestId("copilot-mode-strict-rag")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("copilot-tab-chat"));
    expect(screen.getByTestId("copilot-mode-strict-rag")).toBeInTheDocument();
  });

  it("renders the health bar, every alert card and a working dispatch", async () => {
    renderWithProviders(<FloatingCopilot />);
    fireEvent.click(await screen.findByTestId("copilot-fab-btn"));
    fireEvent.click(screen.getByTestId("copilot-tab-patrol"));

    await waitFor(() =>
      expect(screen.getByTestId("patrol-health-score")).toHaveTextContent("78/100")
    );
    const brief = screen.getByTestId("patrol-summary");
    expect(brief.textContent ?? "").toContain("6 类经营风险");
    expect(brief.textContent ?? "").not.toContain("[object Object]");

    const expiring = screen.getByTestId("patrol-alert-expiring_quotes");
    expect(expiring).toHaveTextContent("锁汇即将到期的报价");
    expect(expiring).toHaveTextContent("3");
    expect(expiring).toHaveTextContent("小光");

    expect(screen.getByTestId("patrol-alert-unread_conversations")).toHaveTextContent("7");

    fireEvent.click(screen.getByTestId("patrol-dispatch-b2b-sales"));

    const dispatched = await screen.findByTestId("patrol-dispatch-result-b2b-sales");
    expect(dispatched).toHaveTextContent("已创建跟进任务");
    expect(dispatched).toHaveTextContent("小光");
    expect(within(dispatched).getByRole("link")).toHaveAttribute("href", "/b2b/quotes");
  });
});

describe("/ai/employees custom employees & patrol dispatch", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
  });

  it("maps the create form onto the AiEmployeeCreate contract without widening scope", () => {
    const payload = toAiEmployeeCreatePayload({
      code: " custom-led-sales ",
      display_name: " 小灯 ",
      role_title: " 照明方案顾问 ",
      department: " 商务中心 ",
      capability: "sales_quoting",
      autonomy_level: "L1_proposal",
      persona_prompt: " 报价必须引用实时 ERP 数据。 ",
    });
    expect(payload.code).toBe("custom-led-sales");
    expect(payload.display_name).toBe("小灯");
    expect(payload.capability_codes).toEqual(["sales_quoting"]);
    expect(payload.autonomy_level).toBe("L1_proposal");
    expect(payload.system_persona).toBe("报价必须引用实时 ERP 数据。");
    expect(payload.is_enabled).toBe(true);
    // Nothing outside the generated contract is invented.
    expect(Object.keys(payload).sort()).toEqual(
      [
        "autonomy_level",
        "capability_codes",
        "code",
        "department",
        "description",
        "display_name",
        "is_enabled",
        "role_title",
        "system_persona",
      ].sort()
    );
  });

  it("creates a custom AI employee from the modal and shows the confirmation", async () => {
    const user = userEvent.setup();
    renderWithProviders(<AiEmployeesPage />, "owner");

    await user.click(await screen.findByTestId("open-create-employee-btn"));
    const form = screen.getByTestId("create-employee-form");
    expect(screen.getByTestId("create-employee-submit")).toBeDisabled();

    fireEvent.change(within(form).getByTestId("create-employee-code"), {
      target: { value: "custom-led-sales" },
    });
    fireEvent.change(within(form).getByTestId("create-employee-name"), {
      target: { value: "小灯" },
    });
    fireEvent.change(within(form).getByTestId("create-employee-role"), {
      target: { value: "照明方案顾问" },
    });
    fireEvent.change(within(form).getByTestId("create-employee-department"), {
      target: { value: "商务中心" },
    });
    fireEvent.change(within(form).getByTestId("create-employee-capability"), {
      target: { value: "order_logistics" },
    });
    fireEvent.change(within(form).getByTestId("create-employee-autonomy"), {
      target: { value: "L0_copilot" },
    });
    fireEvent.change(within(form).getByTestId("create-employee-persona"), {
      target: { value: "只回答物流节点问题。" },
    });

    await waitFor(() => expect(screen.getByTestId("create-employee-submit")).toBeEnabled());
    await user.click(screen.getByTestId("create-employee-submit"));

    await waitFor(() =>
      expect(screen.getByTestId("employee-create-success")).toHaveTextContent("小灯")
    );
    expect(screen.queryByTestId("create-employee-form")).not.toBeInTheDocument();
  });

  it("dispatches a roster employee from the patrol board and shows the live finding", async () => {
    const user = userEvent.setup();
    renderWithProviders(<AiEmployeesPage />, "manager");

    const dispatchBtn = await screen.findByTestId("employee-dispatch-b2b-sales");
    await user.click(dispatchBtn);

    const result = await screen.findByTestId("employee-dispatch-result-b2b-sales");
    expect(result).toHaveTextContent("已创建跟进任务");
    expect(result).toHaveTextContent("锁汇 72 小时内到期");
    expect(within(result).getByRole("link")).toHaveAttribute("href", "/b2b/quotes");
  });

  it("hides the create-employee controls for an agent", async () => {
    renderWithProviders(<AiEmployeesPage />, "agent");
    await waitFor(() => expect(screen.getByTestId("employees-read-only")).toBeInTheDocument());
    expect(screen.queryByTestId("open-create-employee-btn")).not.toBeInTheDocument();
  });
});

describe("/social 平台玩法矩阵", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
  });

  it("renders the 14 operating cards and filters by region pills", async () => {
    const user = userEvent.setup();
    renderWithProviders(<SocialPage />);

    await user.click(await screen.findByTestId("social-playbooks-tab"));
    const board = await screen.findByTestId("social-playbooks-board");

    expect(screen.getByTestId("playbooks-region-all")).toHaveTextContent("(14)");
    expect(screen.getByTestId("playbooks-region-overseas")).toHaveTextContent("(7)");
    expect(screen.getByTestId("playbooks-region-domestic")).toHaveTextContent("(7)");
    await waitFor(() =>
      expect(within(board).getAllByTestId(/^playbook-card-/)).toHaveLength(14)
    );

    const instagram = screen.getByTestId("playbook-card-instagram");
    expect(instagram).toHaveTextContent("海外");
    expect(instagram).toHaveTextContent("官方 API 直发");
    expect(instagram).toHaveTextContent("受众画像");
    expect(instagram).toHaveTextContent("获客漏斗");
    expect(instagram).toHaveTextContent("实战要点");
    expect(instagram).toHaveTextContent("HUMAN_AGENT");
    expect(instagram).not.toHaveTextContent("[object Object]");

    await user.click(screen.getByTestId("playbooks-region-overseas"));
    await waitFor(() =>
      expect(within(board).getAllByTestId(/^playbook-card-/)).toHaveLength(7)
    );
    expect(screen.queryByTestId("playbook-card-xiaohongshu")).not.toBeInTheDocument();

    await user.click(screen.getByTestId("playbooks-region-domestic"));
    await waitFor(() =>
      expect(within(board).getAllByTestId(/^playbook-card-/)).toHaveLength(7)
    );
    const xiaohongshu = await screen.findByTestId("playbook-card-xiaohongshu");
    expect(xiaohongshu).toHaveTextContent("导出发布");
    expect(xiaohongshu).toHaveTextContent("国内");
    // Domestic manual-export platforms never claim API publishing.
    expect(xiaohongshu).not.toHaveTextContent("官方 API 直发");
  });
});
