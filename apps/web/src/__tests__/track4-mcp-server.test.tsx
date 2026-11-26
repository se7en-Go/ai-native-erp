import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { McpServerPanel } from "@/components/ai/mcp-server-panel";
import { AuthSessionContext, type AuthSessionState } from "@/components/providers";
import { I18nProvider } from "@/lib/i18n";
import type { Principal } from "@/lib/contracts";

function renderPanel(role: Principal["role"] = "owner") {
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
          <McpServerPanel />
        </I18nProvider>
      </AuthSessionContext.Provider>
    </QueryClientProvider>,
  );
}

describe("Track 4: tenant MCP server panel", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("renders the exposed L2/L1 catalog and the blocked L0 red line", async () => {
    renderPanel("owner");

    const catalog = await screen.findByTestId("mcp-tool-catalog");
    expect(catalog).toBeInTheDocument();

    const skuRow = await screen.findByTestId("mcp-tool-row-get_live_sku_pricing_and_stock");
    expect(within(skuRow).getByText("L2 只读自动")).toBeInTheDocument();
    expect(within(skuRow).getByText("mcp:read")).toBeInTheDocument();

    const draftRow = screen.getByTestId("mcp-tool-row-create_l1_proposal_draft");
    expect(within(draftRow).getByText("L1 提案人审")).toBeInTheDocument();
    expect(within(draftRow).getByText("mcp:propose")).toBeInTheDocument();

    expect(screen.getByTestId("mcp-tool-blocked-settle_receivable")).toBeInTheDocument();
    expect(screen.getByTestId("mcp-tool-blocked-adjust_inventory")).toBeInTheDocument();
  });

  it("issues a token, reveals the plaintext once, then revokes it", async () => {
    renderPanel("owner");

    // Seed token is listed with its hashed prefix only.
    expect(await screen.findByTestId("mcp-token-row-mcp-tok-mock-1")).toBeInTheDocument();
    expect(screen.getByTestId("mcp-token-row-mcp-tok-mock-1").textContent).not.toContain(
      "plain_token",
    );

    fireEvent.change(screen.getByTestId("mcp-token-name-input"), {
      target: { value: "Claude Desktop 销售助手" },
    });
    fireEvent.click(screen.getByTestId("mcp-token-scope-mcp:propose"));
    fireEvent.change(screen.getByTestId("mcp-token-allowed-tools"), {
      target: { value: "get_live_sku_pricing_and_stock, calc_b2b_quote_matrix" },
    });
    fireEvent.click(screen.getByTestId("mcp-token-create-btn"));

    const revealed = await screen.findByTestId("mcp-token-revealed");
    expect(revealed.textContent).toContain("erp_mcp_");
    expect(revealed.textContent).toContain("明文仅展示这一次");

    // Copy button flips to the copied state.
    fireEvent.click(screen.getByTestId("mcp-token-copy-btn"));
    await waitFor(() => {
      expect(screen.getByTestId("mcp-token-copy-btn").textContent).toContain("已复制");
    });

    const row = await screen.findByTestId("mcp-token-row-mcp-tok-mock-02");
    expect(row.textContent).toContain("Claude Desktop 销售助手");
    expect(row.textContent).toContain("有效");
    expect(row.textContent).toContain("mcp:read");
    expect(row.textContent).toContain("mcp:propose");
    expect(row.textContent).toContain("白名单: 2");

    fireEvent.click(screen.getByTestId("mcp-token-revoke-mcp-tok-mock-02"));

    await waitFor(() => {
      expect(screen.getByTestId("mcp-token-row-mcp-tok-mock-02").textContent).toContain("已吊销");
    });
  });

  it("shows a copyable Claude Desktop / Cursor / OpenClaw config snippet", async () => {
    renderPanel("owner");

    const snippet = await screen.findByTestId("mcp-config-snippet");
    expect(snippet.textContent).toContain("mcpServers");
    expect(snippet.textContent).toContain("ai-native-erp-mcp");
    expect(snippet.textContent).toContain("/api/v1/mcp");
    expect(snippet.textContent).toContain("Bearer erp_mcp_");

    fireEvent.click(screen.getByTestId("mcp-config-copy-btn"));
    await waitFor(() => {
      expect(screen.getByTestId("mcp-config-copy-btn").textContent).toContain("已复制");
    });
  });

  it("hides token issuance from roles that cannot mint tokens", async () => {
    renderPanel("agent");

    expect(await screen.findByTestId("mcp-panel-readonly-note")).toBeInTheDocument();
    expect(screen.queryByTestId("mcp-token-create-btn")).toBeNull();
    expect(screen.queryByTestId("mcp-token-name-input")).toBeNull();
    // The catalog stays readable for every role.
    expect(await screen.findByTestId("mcp-tool-catalog")).toBeInTheDocument();
  });
});
