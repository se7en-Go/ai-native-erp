import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { AppShell } from "@/components/app-shell";
import { CommandPalette, searchTarget } from "@/components/command-palette";
import { AuthSessionContext, type AuthSessionState } from "@/components/providers";
import { I18nProvider } from "@/lib/i18n";
import { canAccessRoute } from "@/lib/auth-cookie";
import {
  ALL_NAV_ITEMS,
  NAV_SECTIONS,
  commandPaletteEntries,
  filterCommandEntries,
  visibleNavSections,
} from "@/lib/nav-registry";
import type { Principal } from "@/lib/contracts";

vi.mock("@/hooks/use-realtime-inbox", () => ({
  useRealtimeInbox: vi.fn(),
}));

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

function renderShell(ui: React.ReactNode, principal: Principal | null, locale: "zh" | "en" = "en") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const authValue: AuthSessionState = {
    principal,
    isLoading: false,
    logout: vi.fn(async () => {}),
    refreshSession: vi.fn(async () => {}),
  };
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthSessionContext.Provider value={authValue}>
        <I18nProvider defaultLocale={locale}>{ui}</I18nProvider>
      </AuthSessionContext.Provider>
    </QueryClientProvider>
  );
}

describe("Phase 3A navigation registry", () => {
  it("keeps every Phase 1–2 route registered in exactly one section", () => {
    const legacyRoutes = [
      "/inbox",
      "/b2b/quotes",
      "/catalog",
      "/orders",
      "/finance",
      "/ai/proposals",
      "/ai/capabilities",
      "/settings/channels",
      "/onboarding",
    ];
    for (const href of legacyRoutes) {
      const matches = ALL_NAV_ITEMS.filter((item) => item.href === href);
      expect(matches, href).toHaveLength(1);
    }
    const hrefs = ALL_NAV_ITEMS.map((item) => item.href);
    expect(new Set(hrefs).size).toBe(hrefs.length);
  });

  it("registers the grouped sections and the Phase 3A/3C pages", () => {
    expect(NAV_SECTIONS.map((section) => section.id)).toEqual([
      "core",
      "business",
      "trade",
      "finance",
      "ai",
      "growth",
      "system",
    ]);
    // Phase 3C adds the growth group and Phase 3D appends /social, touching no
    // existing route.
    const growth = NAV_SECTIONS.find((section) => section.id === "growth");
    expect(growth?.items.map((item) => item.href)).toEqual([
      "/growth/collect",
      "/growth/campaigns",
      "/social",
    ]);
    const phase3aPages = [
      "/dashboard",
      "/crm/leads",
      "/crm/customers",
      "/crm/opportunities",
      "/crm/followups",
      "/supply-chain/suppliers",
      "/supply-chain/purchase-orders",
      "/wms",
      "/analytics",
      "/tasks",
    ];
    for (const href of phase3aPages) {
      expect(ALL_NAV_ITEMS.some((item) => item.href === href), href).toBe(true);
    }
  });

  it("hides operations and finance routes from agent, and shows them to manager", () => {
    const agentHrefs = visibleNavSections("agent").flatMap((s) => s.items.map((i) => i.href));
    expect(agentHrefs).not.toContain("/supply-chain/suppliers");
    expect(agentHrefs).not.toContain("/finance");
    expect(agentHrefs).toContain("/crm/leads");
    expect(agentHrefs).toContain("/wms");

    const managerHrefs = visibleNavSections("manager").flatMap((s) => s.items.map((i) => i.href));
    expect(managerHrefs).toContain("/supply-chain/purchase-orders");
    expect(managerHrefs).toContain("/finance");

    // Route guard and registry must agree. /settings/channels is the documented
    // exception: its link stays visible to every role, the route itself is guarded.
    for (const href of ALL_NAV_ITEMS.map((item) => item.href).filter(
      (href) => href !== "/settings/channels"
    )) {
      for (const role of ["owner", "admin", "manager", "agent", "finance"] as const) {
        const visible = visibleNavSections(role).some((s) => s.items.some((i) => i.href === href));
        if (visible) expect(canAccessRoute(role, href), `${role} ${href}`).toBe(true);
      }
    }
  });

  it("filters command entries by translated label, route and keyword", () => {
    const entries = commandPaletteEntries("manager");
    const labels = { "/crm/leads": "CRM 线索池" };
    expect(filterCommandEntries(entries, "线索", labels).map((e) => e.href)).toContain(
      "/crm/leads"
    );
    expect(filterCommandEntries(entries, "purchase order").map((e) => e.href)).toContain(
      "/supply-chain/purchase-orders"
    );
    expect(filterCommandEntries(entries, "zzz-nothing")).toEqual([]);
    expect(commandPaletteEntries("agent").some((e) => e.href === "/supply-chain/suppliers")).toBe(
      false
    );
  });

  it("maps tenant-scoped search hits to owning pages", () => {
    expect(searchTarget({ kind: "customer", id: "c1" })).toBe("/crm/customers?customer=c1");
    expect(searchTarget({ kind: "order", id: "o1" })).toBe("/orders?order=o1");
    expect(searchTarget({ kind: "quote", id: "q1" })).toBe("/b2b/quotes?quote=q1");
    expect(searchTarget({ kind: "sku", id: "s1" })).toBe("/catalog?sku=s1");
  });
});

describe("Phase 3A AppShell grouped navigation and Ctrl+K", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("renders grouped sections, keeps legacy links and collapses a section", () => {
    renderShell(
      <AppShell currentPath="/dashboard">
        <div data-testid="dashboard-content">Executive home</div>
      </AppShell>,
      makePrincipal("owner")
    );

    expect(screen.getByRole("link", { name: /Dashboard/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Inbox/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /B2B CRM & Quotes/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Purchase Orders/i })).toBeInTheDocument();
    expect(screen.getByTestId("dashboard-content")).toBeInTheDocument();

    const businessToggle = screen.getByTestId("nav-section-toggle-business");
    expect(businessToggle).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(businessToggle);
    expect(businessToggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("link", { name: /CRM Leads/i })).toBeNull();
  });

  it("blocks /supply-chain for agent with the RBAC guard", () => {
    renderShell(
      <AppShell currentPath="/supply-chain/purchase-orders">
        <div data-testid="po-secret">supplier cost</div>
      </AppShell>,
      makePrincipal("agent")
    );
    expect(screen.queryByTestId("po-secret")).toBeNull();
    expect(screen.getByTestId("rbac-forbidden-guard")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Suppliers/i })).toBeNull();
  });

  it("opens the palette with Ctrl+K and navigates with an entry", async () => {
    // Live search path: stub fetch so the generated SDK gets an empty tenant result.
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ items: [] }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          })
      )
    );
    renderShell(
      <AppShell currentPath="/dashboard">
        <div>home</div>
      </AppShell>,
      makePrincipal("owner")
    );

    expect(screen.queryByTestId("command-palette")).toBeNull();
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    await waitFor(() => expect(screen.getByTestId("command-palette")).toBeInTheDocument());

    const input = screen.getByTestId("command-palette-input");
    fireEvent.change(input, { target: { value: "wms" } });
    const wms = await screen.findByTestId("command-entry-nav-/wms");
    expect(wms).toBeEnabled();

    fireEvent.change(input, { target: { value: "nothing-matches-this" } });
    await waitFor(() =>
      expect(screen.getByTestId("command-palette-empty")).toBeInTheDocument()
    );

    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("command-palette")).toBeNull());
  });

  it("disables command entries the role cannot reach", () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const authValue: AuthSessionState = {
      principal: makePrincipal("agent"),
      isLoading: false,
      logout: vi.fn(async () => {}),
      refreshSession: vi.fn(async () => {}),
    };
    render(
      <QueryClientProvider client={queryClient}>
        <AuthSessionContext.Provider value={authValue}>
          <I18nProvider defaultLocale="en">
            <CommandPalette open onClose={vi.fn()} />
          </I18nProvider>
        </AuthSessionContext.Provider>
      </QueryClientProvider>
    );
    expect(screen.getByTestId("command-entry-nav-/crm/leads")).toBeEnabled();
    expect(screen.queryByTestId("command-entry-create-po")).toBeNull();
  });
});
