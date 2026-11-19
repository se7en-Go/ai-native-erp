import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ESLint } from "eslint";
import path from "node:path";

import { AppShell } from "@/components/app-shell";
import { I18nProvider } from "@/lib/i18n";
import { AuthSessionContext, type AuthSessionState } from "@/components/providers";
import { useRealtimeInbox } from "@/hooks/use-realtime-inbox";
import { canAccessRoute } from "@/lib/auth-cookie";
import type { Principal } from "@/lib/contracts";

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

function renderWithProviders(
  ui: React.ReactNode,
  principal: Principal | null,
  initialLocale: "zh" | "en" = "en",
  queryClient?: QueryClient
) {
  const client =
    queryClient ??
    new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

  const authValue: AuthSessionState = {
    principal,
    isLoading: false,
    logout: vi.fn(async () => {}),
    refreshSession: vi.fn(async () => {}),
  };

  return render(
    <QueryClientProvider client={client}>
      <AuthSessionContext.Provider value={authValue}>
        <I18nProvider defaultLocale={initialLocale}>{ui}</I18nProvider>
      </AuthSessionContext.Provider>
    </QueryClientProvider>
  );
}

describe("Task 5.1 AppShell RBAC, i18n, SSE & ESLint Zero-DB Guard", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("hides Finance & CVP (/finance) from agent in sidebar and blocks /finance view", () => {
    expect(canAccessRoute("agent", "/finance")).toBe(false);
    expect(canAccessRoute("finance", "/finance")).toBe(true);

    renderWithProviders(
      <AppShell currentPath="/finance" pendingProposalsCount={3}>
        <div data-testid="finance-secret">COGS Secret Data</div>
      </AppShell>,
      makePrincipal("agent"),
      "en"
    );

    expect(screen.queryByRole("link", { name: /Finance & CVP/i })).toBeNull();
    expect(screen.getByRole("link", { name: /Inbox/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /AI Proposals/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /B2B CRM & Quotes/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Catalog/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Orders & Tracking/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Channels & Settings/i })).toBeInTheDocument();

    expect(screen.queryByTestId("finance-secret")).toBeNull();
    expect(screen.getByText(/ERR_INSUFFICIENT_ROLE/i)).toBeInTheDocument();
  });

  it("shows Finance & CVP to finance role and supports 中/EN toggle + sidebar collapse", () => {
    renderWithProviders(
      <AppShell currentPath="/finance" pendingProposalsCount={6}>
        <div data-testid="finance-secret">COGS Margin Waterfall</div>
      </AppShell>,
      makePrincipal("finance"),
      "en"
    );

    expect(screen.getByRole("link", { name: /Finance & CVP/i })).toBeInTheDocument();
    expect(screen.getByTestId("finance-secret")).toBeInTheDocument();

    const langBtn = screen.getByTestId("locale-toggle-btn");
    fireEvent.click(langBtn);
    expect(screen.getByRole("link", { name: /财务与 CVP/i })).toBeInTheDocument();

    const collapseBtn = screen.getByTestId("sidebar-collapse-btn");
    fireEvent.click(collapseBtn);
    expect(screen.getByTestId("sidebar-nav")).toHaveAttribute("data-collapsed", "true");
  });

  it("subscribes to SSE /api/bff/v1/inbox/events and invalidates TanStack queries", async () => {
    const listeners: Record<string, Array<(ev: MessageEvent) => void>> = {};
    const closeMock = vi.fn();

    class MockEventSource {
      url: string;
      withCredentials?: boolean;
      constructor(url: string, init?: EventSourceInit) {
        this.url = url;
        this.withCredentials = init?.withCredentials;
      }
      addEventListener(type: string, fn: (ev: MessageEvent) => void) {
        listeners[type] = listeners[type] ?? [];
        listeners[type].push(fn);
      }
      removeEventListener() {}
      close = closeMock;
    }

    vi.stubGlobal("EventSource", MockEventSource);

    const qc = new QueryClient();
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");

    function Probe() {
      useRealtimeInbox(true);
      return <div>sse-active</div>;
    }

    const { unmount } = renderWithProviders(<Probe />, makePrincipal("owner"), "en", qc);

    listeners["message.created"]?.forEach((cb) =>
      cb(
        new MessageEvent("message.created", {
          data: JSON.stringify({ conversation_id: "conv-1" }),
        })
      )
    );
    listeners["proposal.created"]?.forEach((cb) =>
      cb(new MessageEvent("proposal.created", { data: JSON.stringify({ proposal_id: "prop-1" }) }))
    );

    await waitFor(() => {
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["inbox", "conversations"] });
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["inbox", "messages", "conv-1"] });
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["ai", "proposals"] });
    });

    unmount();
    expect(closeMock).toHaveBeenCalledTimes(1);
  });

  it("enforces ESLint no-restricted-imports rule blocking SQL/DB packages in apps/web", async () => {
    const eslint = new ESLint({
      overrideConfigFile: path.resolve(process.cwd(), "eslint.config.mjs"),
    });
    const results = await eslint.lintText(
      `import { Pool } from "pg";\nexport const p = new Pool();\n`,
      { filePath: path.resolve(process.cwd(), "src/app/api/bff/forbidden-check.ts") }
    );
    const errors = results.flatMap((r) => r.messages);
    expect(errors.some((m) => m.ruleId === "no-restricted-imports")).toBe(true);
  });
});
