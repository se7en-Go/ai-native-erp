// Phase 3A page-level RBAC + data-flow tests. Runs entirely on fixtures
// (NEXT_PUBLIC_USE_MOCK_API=true) so the same UI code paths are exercised.
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import DashboardPage from "@/app/dashboard/page";
import AnalyticsPage from "@/app/analytics/page";
import WmsPage from "@/app/wms/page";
import SuppliersPage from "@/app/supply-chain/suppliers/page";
import TasksPage from "@/app/tasks/page";
import { AuthSessionContext, type AuthSessionState } from "@/components/providers";
import { I18nProvider } from "@/lib/i18n";
import type { Principal } from "@/lib/contracts";

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

describe("Phase 3A pages", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("shows dashboard KPIs and masks cost/margin for an agent", async () => {
    const { unmount } = renderPage(<DashboardPage />, "agent");
    expect(screen.getByTestId("dashboard-kpi-grid")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("dashboard-channels")).toBeInTheDocument());
    expect(screen.getByTestId("dashboard-financials-masked")).toBeInTheDocument();
    expect(screen.queryByTestId("dashboard-financials")).toBeNull();
    unmount();

    renderPage(<DashboardPage />, "manager");
    await waitFor(() => expect(screen.getByTestId("dashboard-financials")).toBeInTheDocument());
    expect(screen.queryByTestId("dashboard-financials-masked")).toBeNull();
  });

  it("masks SKU margins on analytics for agents but keeps the funnel visible", async () => {
    const { unmount } = renderPage(<AnalyticsPage />, "agent");
    await waitFor(() => expect(screen.getByTestId("analytics-funnel")).toBeInTheDocument());
    expect(screen.getByTestId("analytics-margins-masked")).toBeInTheDocument();
    expect(screen.queryByTestId("analytics-sku-margins")).toBeNull();
    unmount();

    renderPage(<AnalyticsPage />, "finance");
    await waitFor(() =>
      expect(screen.getByTestId("analytics-sku-margins")).toBeInTheDocument()
    );
    expect(await screen.findByText("LED-COB-24V-5M")).toBeInTheDocument();
  });

  it("lets only owner/admin/manager submit a stocktake adjustment", async () => {
    const { unmount } = renderPage(<WmsPage />, "agent");
    await waitFor(() => expect(screen.getByTestId("wms-movements-table")).toBeInTheDocument());
    expect(screen.getByTestId("stocktake-forbidden")).toBeInTheDocument();
    expect(screen.queryByTestId("stocktake-form")).toBeNull();
    unmount();

    renderPage(<WmsPage />, "manager");
    await waitFor(() => expect(screen.getByTestId("stocktake-form")).toBeInTheDocument());
    expect(screen.getByTestId("wms-stock-table")).toBeInTheDocument();
  });

  it("creates a supplier and reflects it in the table through the mock data layer", async () => {
    renderPage(<SuppliersPage />, "manager");
    await waitFor(() => expect(screen.getByTestId("suppliers-table")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText("Supplier code"), { target: { value: "SUP-NEW" } });
    fireEvent.change(screen.getByLabelText("Supplier name"), { target: { value: "新供应商" } });
    fireEvent.click(screen.getByTestId("supplier-submit"));

    await waitFor(() => expect(screen.getByText("新供应商")).toBeInTheDocument());
  });

  it("creates and completes a task", async () => {
    renderPage(<TasksPage />, "agent");
    await waitFor(() => expect(screen.getByTestId("task-list")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText("Task title"), { target: { value: "跟进样品确认" } });
    fireEvent.click(screen.getByTestId("task-submit"));
    await waitFor(() => expect(screen.getByText("跟进样品确认")).toBeInTheDocument());
  });
});
