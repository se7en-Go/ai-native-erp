import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import DashboardPage from "@/app/dashboard/page";
import { AuthSessionContext, type AuthSessionState } from "@/components/providers";
import { I18nProvider } from "@/lib/i18n";
import { ThemeProvider } from "@/lib/theme";
import type { Principal } from "@/lib/contracts";

vi.mock("@/hooks/use-realtime-inbox", () => ({ useRealtimeInbox: vi.fn() }));

function makePrincipal(role: Principal["role"]): Principal {
  return {
    user_id: "11111111-1111-1111-1111-111111111111",
    tenant_id: "22222222-2222-2222-2222-222222222222",
    role,
    email: `${role}@qzled.example.com`,
    tenant_slug: "qz-led-factory",
    onboarding_stage: "LiveProduction",
  };
}

function renderDashboard(role: Principal["role"]) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const authValue: AuthSessionState = {
    principal: makePrincipal(role),
    isLoading: false,
    logout: vi.fn(async () => {}),
    refreshSession: vi.fn(async () => {}),
  };
  return render(
    <QueryClientProvider client={queryClient}>
      <ThemeProvider defaultTheme="light">
        <AuthSessionContext.Provider value={authValue}>
          <I18nProvider defaultLocale="zh">
            <DashboardPage />
          </I18nProvider>
        </AuthSessionContext.Provider>
      </ThemeProvider>
    </QueryClientProvider>
  );
}

describe("21st.dev Dashboard & Light/Dark Theme", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
    if (typeof window.localStorage?.clear === "function") {
      window.localStorage.clear();
    }
    document.documentElement.classList.remove("dark");
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    document.documentElement.classList.remove("dark");
  });

  it("toggles dark mode on html root and updates toggle button aria-label", async () => {
    renderDashboard("owner");

    const toggle = screen.getByTestId("theme-toggle-btn");
    expect(document.documentElement.classList.contains("dark")).toBe(false);

    fireEvent.click(toggle);
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(toggle.getAttribute("aria-label")).toContain("浅色");

    fireEvent.click(toggle);
    expect(document.documentElement.classList.contains("dark")).toBe(false);
    expect(toggle.getAttribute("aria-label")).toContain("深色");
  });

  it("renders real ERP KPIs, patrol health score, recent activity, and collapsible sidebar", async () => {
    renderDashboard("owner");

    expect(screen.getByTestId("dashboard-kpi-grid")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("dashboard-financials")).toBeInTheDocument());
    expect(screen.getByTestId("dashboard-funnel")).toBeInTheDocument();
    expect(screen.getByTestId("dashboard-channels")).toBeInTheDocument();
    expect(screen.getByTestId("dashboard-tasks")).toBeInTheDocument();

    const sidebar = screen.getByTestId("sidebar-nav");
    expect(sidebar.getAttribute("data-collapsed")).toBe("false");
    fireEvent.click(screen.getByTestId("sidebar-collapse-btn"));
    expect(sidebar.getAttribute("data-collapsed")).toBe("true");
  });
});
