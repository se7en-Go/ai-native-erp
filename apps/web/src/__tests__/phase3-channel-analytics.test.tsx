import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import FinancePage from "@/app/finance/page";
import WmsPage from "@/app/wms/page";
import { ChannelAnalyticsCard } from "@/components/finance/channel-analytics-card";
import { AuthSessionContext, type AuthSessionState } from "@/components/providers";
import { I18nProvider } from "@/lib/i18n";
import type { Principal } from "@/lib/contracts";

vi.mock("@/hooks/use-realtime-inbox", () => ({ useRealtimeInbox: vi.fn() }));

function renderWithSession(ui: React.ReactElement, role: Principal["role"] = "owner") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const principal: Principal = {
    user_id: "user-1",
    tenant_id: "tenant-1",
    email: "owner@example.com",
    full_name: "Owner",
    role,
  };
  const session: AuthSessionState = {
    principal,
    isLoading: false,
    logout: vi.fn(async () => {}),
    refreshSession: vi.fn(async () => {}),
  };
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthSessionContext.Provider value={session}>
        <I18nProvider>{ui}</I18nProvider>
      </AuthSessionContext.Provider>
    </QueryClientProvider>,
  );
}

describe("Phase 3 multi-platform analytics & 1688 replenishment UI", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  });

  it("renders multi-platform overview metrics and switches 7d/30d/90d windows", async () => {
    renderWithSession(<ChannelAnalyticsCard enabled />, "owner");

    expect(await screen.findByTestId("channel-analytics-card")).toBeInTheDocument();
    expect(await screen.findByTestId("analytics-row-shopify")).toBeInTheDocument();
    expect(screen.getByTestId("analytics-row-tiktok_shop")).toBeInTheDocument();
    expect(screen.getByTestId("analytics-total-roas")).toHaveTextContent("9.75x");

    fireEvent.click(screen.getByTestId("analytics-window-7d"));
    await waitFor(() =>
      expect(screen.getByTestId("analytics-total-units")).toHaveTextContent("142"),
    );
  });

  it("hides financial analytics when enabled=false for unprivileged roles", () => {
    renderWithSession(<ChannelAnalyticsCard enabled={false} />, "agent");
    expect(screen.queryByTestId("channel-analytics-card")).toBeNull();
  });

  it("embeds multi-platform analytics on /finance page", async () => {
    renderWithSession(<FinancePage />, "finance");
    expect(await screen.findByTestId("channel-analytics-card")).toBeInTheDocument();
  });

  it("renders SKU cross-channel velocity & 1688 one-click replenishment on /wms", async () => {
    renderWithSession(<WmsPage />, "manager");
    expect(await screen.findByTestId("wms-sku-velocity-card")).toBeInTheDocument();
    expect(await screen.findByTestId("velocity-risk-var-1")).toHaveTextContent("critical");
    expect(screen.getByTestId("velocity-1688-link-var-1")).toHaveAttribute(
      "href",
      expect.stringContaining("1688.com"),
    );

    fireEvent.click(screen.getByTestId("velocity-create-po-var-1"));
    expect(await screen.findByTestId("low-stock-po-modal")).toBeInTheDocument();
  });
});
