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
  filterNavSections,
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

/** Every route the sidebar actually rendered, deduplicated (the brand link repeats /dashboard). */
function renderedSidebarHrefs(): string[] {
  const aside = screen.getByTestId("sidebar-nav");
  return [
    ...new Set(
      Array.from(aside.querySelectorAll("a[href]")).map((anchor) => anchor.getAttribute("href") ?? "")
    ),
  ];
}

describe("Sidebar navigation registry", () => {
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

  it("collapses the 24 routes into five plain-language groups", () => {
    expect(NAV_SECTIONS.map((section) => section.id)).toEqual([
      "core",
      "business",
      "trade",
      "growth_finance",
      "ai_system",
    ]);
    expect(NAV_SECTIONS.map((section) => section.items.length)).toEqual([3, 5, 5, 5, 6]);
    expect(ALL_NAV_ITEMS).toHaveLength(24);

    // 只有后两组默认折叠，前两组保持展开。
    expect(NAV_SECTIONS.filter((section) => section.defaultCollapsed).map((s) => s.id)).toEqual([
      "growth_finance",
      "ai_system",
    ]);

    const byId = (id: string) => NAV_SECTIONS.find((section) => section.id === id);
    expect(byId("core")?.items.map((item) => item.href)).toEqual([
      "/dashboard",
      "/inbox",
      "/tasks",
    ]);
    expect(byId("business")?.items.map((item) => item.href)).toEqual([
      "/b2b/quotes",
      "/crm/leads",
      "/crm/customers",
      "/crm/opportunities",
      "/crm/followups",
    ]);
    expect(byId("trade")?.items.map((item) => item.href)).toEqual([
      "/catalog",
      "/orders",
      "/wms",
      "/supply-chain/suppliers",
      "/supply-chain/purchase-orders",
    ]);
    expect(byId("growth_finance")?.items.map((item) => item.href)).toEqual([
      "/finance",
      "/analytics",
      "/growth/collect",
      "/growth/campaigns",
      "/social",
    ]);
    expect(byId("ai_system")?.items.map((item) => item.href)).toEqual([
      "/ai/proposals",
      "/ai/employees",
      "/ai/studio",
      "/ai/capabilities",
      "/settings/channels",
      "/onboarding",
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

  it("filters registry sections by Chinese label, English label and keywords", () => {
    const ownerSections = visibleNavSections("owner");
    const hrefsFor = (query: string) =>
      filterNavSections(ownerSections, query).flatMap((section) => section.items.map((i) => i.href));

    // 中文标签（UI 语言为英文时依然命中）与英文标签（UI 语言为中文时依然命中）。
    expect(hrefsFor("财务")).toEqual(["/finance"]);
    expect(hrefsFor("finance")).toEqual(["/finance"]);
    expect(hrefsFor("wms")).toEqual(["/wms"]);
    // 关键词：17TRACK / PI / 库存 等业务黑话都能搜到。
    expect(hrefsFor("17track")).toEqual(["/orders"]);
    expect(hrefsFor("pi")).toContain("/b2b/quotes");
    expect(hrefsFor("库存")).toEqual(["/wms"]);
    // 只保留命中分组，不留空壳分组。
    expect(filterNavSections(ownerSections, "zzz-nothing")).toEqual([]);
    expect(filterNavSections(ownerSections, "   ")).toEqual(ownerSections);
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
    const labels = { "/crm/leads": "线索池" };
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

describe("AppShell streamlined sidebar and Ctrl+K", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    window.sessionStorage.clear();
  });

  it("renders five groups, keeps legacy links and collapses a section", () => {
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

    for (const id of ["core", "business", "trade", "growth_finance", "ai_system"]) {
      expect(screen.getByTestId(`nav-section-${id}`)).toBeInTheDocument();
    }

    const businessToggle = screen.getByTestId("nav-section-toggle-business");
    expect(businessToggle).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(businessToggle);
    expect(businessToggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("link", { name: /Leads/i })).toBeNull();
  });

  it("starts with 财务与增长 / AI 与设置 collapsed without misleading item-count circles", () => {
    renderShell(
      <AppShell currentPath="/dashboard">
        <div>home</div>
      </AppShell>,
      makePrincipal("owner")
    );

    expect(screen.getByTestId("nav-section-toggle-growth_finance")).toHaveAttribute(
      "aria-expanded",
      "false"
    );
    expect(screen.getByTestId("nav-section-toggle-ai_system")).toHaveAttribute(
      "aria-expanded",
      "false"
    );
    // 折叠分组绝不把菜单条目数渲染成类似未读角标的圆圈。
    expect(screen.queryByTestId("nav-section-count-growth_finance")).toBeNull();
    expect(screen.queryByTestId("nav-section-count-ai_system")).toBeNull();
    expect(screen.queryByTestId("sidebar-group-pending-badge")).toBeNull();
    // 折叠组的条目默认不渲染，侧边栏保持清爽。
    expect(screen.queryByRole("link", { name: /Finance & CVP/i })).toBeNull();
    expect(screen.queryByRole("link", { name: /AI Proposals/i })).toBeNull();
  });

  it("shows only the real pending badge on collapsed AI & Settings and never duplicates counts", () => {
    renderShell(
      <AppShell currentPath="/dashboard" pendingProposalsCount={6}>
        <div>home</div>
      </AppShell>,
      makePrincipal("owner")
    );

    const aiToggle = screen.getByTestId("nav-section-toggle-ai_system");
    expect(aiToggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByTestId("sidebar-group-pending-badge").textContent).toBe("6");
    // Only one badge ("6") inside the collapsed AI & Settings header — no secondary item count.
    expect(aiToggle.querySelectorAll("span.rounded-full")).toHaveLength(1);
    // Other collapsed groups with zero pending items render zero badges.
    const financeToggle = screen.getByTestId("nav-section-toggle-growth_finance");
    expect(financeToggle.querySelectorAll("span.rounded-full")).toHaveLength(0);
  });

  it("preserves sidebar scroll position and manual group toggles across route remounts", () => {
    const first = renderShell(
      <AppShell currentPath="/ai/proposals" pendingProposalsCount={6}>
        <div>proposals</div>
      </AppShell>,
      makePrincipal("owner")
    );

    // User manually collapses Business & Customers and scrolls down to the AI section.
    fireEvent.click(screen.getByTestId("nav-section-toggle-business"));
    expect(screen.getByTestId("nav-section-toggle-business")).toHaveAttribute(
      "aria-expanded",
      "false"
    );

    const navBefore = screen.getByTestId("sidebar-nav-scroll");
    navBefore.scrollTop = 340;
    fireEvent.scroll(navBefore);

    // Clicking AI Knowledge Base triggers a page-level AppShell unmount + remount.
    const kbLink = navBefore.querySelector<HTMLAnchorElement>('a[href="/ai/capabilities"]');
    expect(kbLink).not.toBeNull();
    fireEvent.click(kbLink!);
    first.unmount();

    renderShell(
      <AppShell currentPath="/ai/capabilities" pendingProposalsCount={6}>
        <div>kb</div>
      </AppShell>,
      makePrincipal("owner")
    );

    expect(screen.getByTestId("sidebar-nav-scroll").scrollTop).toBe(340);
    expect(screen.getByTestId("nav-section-toggle-business")).toHaveAttribute(
      "aria-expanded",
      "false"
    );
    expect(screen.getByTestId("nav-section-toggle-ai_system")).toHaveAttribute(
      "aria-expanded",
      "true"
    );
  });

  it("auto-expands the group that owns the active route", () => {
    const { unmount } = renderShell(
      <AppShell currentPath="/finance" pendingProposalsCount={0}>
        <div>finance</div>
      </AppShell>,
      makePrincipal("finance")
    );
    expect(screen.getByTestId("nav-section-toggle-growth_finance")).toHaveAttribute(
      "aria-expanded",
      "true"
    );
    expect(screen.getByRole("link", { name: /Finance & CVP/i })).toBeInTheDocument();
    expect(screen.getByTestId("nav-section-toggle-ai_system")).toHaveAttribute(
      "aria-expanded",
      "false"
    );
    unmount();

    renderShell(
      <AppShell currentPath="/ai/proposals">
        <div>proposals</div>
      </AppShell>,
      makePrincipal("owner")
    );
    expect(screen.getByTestId("nav-section-toggle-ai_system")).toHaveAttribute(
      "aria-expanded",
      "true"
    );
    expect(screen.getByRole("link", { name: /AI Proposals/i })).toBeInTheDocument();
    // 另一组仍然保持折叠 —— 只展开“用户所在”的那一组。
    expect(screen.getByTestId("nav-section-toggle-growth_finance")).toHaveAttribute(
      "aria-expanded",
      "false"
    );
  });

  it("raises a rose pending-proposals badge on the collapsed AI & Settings header", () => {
    renderShell(
      <AppShell currentPath="/dashboard" pendingProposalsCount={4}>
        <div>home</div>
      </AppShell>,
      makePrincipal("owner")
    );

    const headerBadge = screen.getByTestId("sidebar-group-pending-badge");
    expect(headerBadge.textContent).toBe("4");
    expect(screen.queryByTestId("sidebar-pending-proposals-badge")).toBeNull();

    fireEvent.click(screen.getByTestId("nav-section-toggle-ai_system"));
    expect(screen.queryByTestId("sidebar-group-pending-badge")).toBeNull();
    expect(screen.getByTestId("sidebar-pending-proposals-badge").textContent).toBe("4");
  });

  it("filters modules across collapsed groups by Chinese or English text and clears instantly", () => {
    renderShell(
      <AppShell currentPath="/dashboard">
        <div>home</div>
      </AppShell>,
      makePrincipal("owner"),
      "en"
    );

    const input = screen.getByTestId("sidebar-filter-input");
    expect(input).toHaveAttribute("placeholder", "Filter modules…");
    expect(screen.queryByRole("link", { name: /Finance & CVP/i })).toBeNull();

    // 英文界面下输入中文：依然命中，并自动展开所属的折叠分组。
    fireEvent.change(input, { target: { value: "财务" } });
    expect(screen.getByTestId("nav-section-toggle-growth_finance")).toHaveAttribute(
      "aria-expanded",
      "true"
    );
    expect(screen.getByRole("link", { name: /Finance & CVP/i })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Inbox/i })).toBeNull();
    expect(screen.queryByTestId("nav-section-ai_system")).toBeNull();

    // 英文关键词同样命中，且跨折叠分组。
    fireEvent.change(input, { target: { value: "wms" } });
    expect(screen.getByRole("link", { name: /WMS Inventory/i })).toBeInTheDocument();
    expect(screen.queryByTestId("nav-section-business")).toBeNull();

    fireEvent.change(input, { target: { value: "zzz-nothing" } });
    expect(screen.getByTestId("sidebar-filter-empty")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("sidebar-filter-clear"));
    expect(screen.getByRole("link", { name: /Inbox/i })).toBeInTheDocument();
    expect(screen.getByTestId("sidebar-filter-input")).toHaveValue("");
    expect(screen.getByTestId("nav-section-toggle-growth_finance")).toHaveAttribute(
      "aria-expanded",
      "false"
    );
  });

  it("keeps all 24 routes reachable for owner and RBAC intact for agent", () => {
    const { unmount } = renderShell(
      <AppShell currentPath="/dashboard">
        <div>home</div>
      </AppShell>,
      makePrincipal("owner")
    );
    fireEvent.click(screen.getByTestId("nav-section-toggle-growth_finance"));
    fireEvent.click(screen.getByTestId("nav-section-toggle-ai_system"));
    const ownerHrefs = renderedSidebarHrefs().filter((href) =>
      ALL_NAV_ITEMS.some((item) => item.href === href)
    );
    expect(ownerHrefs.sort()).toEqual(ALL_NAV_ITEMS.map((item) => item.href).sort());
    unmount();

    renderShell(
      <AppShell currentPath="/settings/channels">
        <div data-testid="channels-body">Quick connect</div>
      </AppShell>,
      makePrincipal("agent")
    );
    expect(screen.getByTestId("channels-body")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Channels & Settings/i })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Finance & CVP/i })).toBeNull();
    expect(screen.queryByRole("link", { name: /Suppliers/i })).toBeNull();
  });

  it("keeps every route reachable in the icon rail when the sidebar is collapsed", () => {
    renderShell(
      <AppShell currentPath="/dashboard">
        <div>home</div>
      </AppShell>,
      makePrincipal("owner")
    );
    expect(screen.queryByTitle("Finance & CVP")).toBeNull();
    expect(screen.getByTestId("sidebar-filter-input")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("sidebar-collapse-btn"));
    expect(screen.getByTestId("sidebar-nav")).toHaveAttribute("data-collapsed", "true");
    expect(screen.queryByTestId("sidebar-filter-input")).toBeNull();
    // 图标栏模式：被折叠的分组也展开，24 个入口仍然可点。
    expect(screen.getByTitle("Finance & CVP")).toBeInTheDocument();
    expect(screen.getByTitle("AI Proposals")).toBeInTheDocument();
    const railHrefs = renderedSidebarHrefs().filter((href) =>
      ALL_NAV_ITEMS.some((item) => item.href === href)
    );
    expect(railHrefs).toHaveLength(24);
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
