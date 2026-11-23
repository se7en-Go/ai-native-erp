import React from "react";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import WmsPage from "@/app/wms/page";
import { SkuMappingDrawer } from "@/components/catalog/sku-mapping-drawer";
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

describe("Phase 2 multi-platform inventory sync UI", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  });

  it("renders multi-platform sync logs, triggers sync-now, and retries dead-letter jobs", async () => {
    renderWithSession(<WmsPage />, "owner");

    expect(await screen.findByTestId("wms-inventory-sync-card")).toBeInTheDocument();
    expect(await screen.findByTestId("sync-job-status-sync-job-shopify-1")).toHaveTextContent(
      "succeeded",
    );
    expect(await screen.findByTestId("sync-job-status-sync-job-tiktok-1")).toHaveTextContent(
      "dead_letter",
    );
    expect(screen.getByTestId("retry-sync-job-sync-job-tiktok-1")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("wms-sync-now-btn"));
    await waitFor(() =>
      expect(screen.getByTestId("wms-sync-summary-banner")).toHaveTextContent("2"),
    );

    fireEvent.click(screen.getByTestId("retry-sync-job-sync-job-tiktok-1"));
    await waitFor(() =>
      expect(screen.getByTestId("retry-sync-job-sync-job-tiktok-1")).toBeInTheDocument(),
    );
  });

  it("renders channel safety_stock input inside SkuMappingDrawer", async () => {
    renderWithSession(
      <SkuMappingDrawer variantId="var-1" role="owner" onClose={vi.fn()} />,
      "owner",
    );
    expect(await screen.findByTestId("sku-mapping-drawer")).toBeInTheDocument();
  });
});
