import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import CatalogPage from "@/app/catalog/page";
import { AuthSessionContext, type AuthSessionState } from "@/components/providers";
import { I18nProvider } from "@/lib/i18n";
import type { Principal } from "@/lib/contracts";

vi.mock("@/hooks/use-realtime-inbox", () => ({ useRealtimeInbox: vi.fn() }));

function renderCatalog(role: Principal["role"] = "owner") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const authValue: AuthSessionState = {
    principal: {
      user_id: "11111111-1111-1111-1111-111111111111",
      tenant_id: "22222222-2222-2222-2222-222222222222",
      role,
      email: "owner@qzled.example.com",
      tenant_slug: "qz-led-factory",
      onboarding_stage: "LiveProduction",
    },
    isLoading: false,
    logout: vi.fn(async () => {}),
    refreshSession: vi.fn(async () => {}),
  };
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthSessionContext.Provider value={authValue}>
        <I18nProvider defaultLocale="zh">
          <CatalogPage />
        </I18nProvider>
      </AuthSessionContext.Provider>
    </QueryClientProvider>,
  );
}

describe("Phase 4: Collector API tokens UI", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("creates, reveals once, and revokes a browser collector token", async () => {
    renderCatalog("owner");

    fireEvent.click(await screen.findByTestId("catalog-tab-sourcing"));
    const card = await screen.findByTestId("collector-tokens-card");
    expect(card).toBeInTheDocument();

    fireEvent.change(screen.getByTestId("collector-token-name-input"), {
      target: { value: "My 1688 Collector" },
    });
    fireEvent.click(screen.getByTestId("collector-token-create-btn"));

    const revealed = await screen.findByTestId("collector-token-revealed");
    expect(revealed.textContent).toContain("erp_col_");

    const row = await screen.findByTestId("collector-token-row-col-tok-mock-02");
    expect(row.textContent).toContain("My 1688 Collector");
    expect(row.textContent).toContain("有效");

    fireEvent.click(screen.getByTestId("collector-token-revoke-col-tok-mock-02"));

    await waitFor(() => {
      const updated = screen.getByTestId("collector-token-row-col-tok-mock-02");
      expect(updated.textContent).toContain("已吊销");
    });
  });
});
