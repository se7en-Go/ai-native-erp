// Track 6 UI suite: team store-scope governance + offboarding handover panel on
// /settings/channels, and the AI adoption gauge + outbound red-line guard + KB
// gap backlog panel on /ai/proposals.
//
// The client-module cases run the real `bffFetch` path so URL, method and body
// are verified against the locked contract; the panel cases render the real
// components against a stateful fetch stub. Adoption rates are asserted as the
// backend's exact decimal percentage strings — never a recomputed float.
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import AiProposalsPage from "@/app/ai/proposals/page";
import { KbGapAndGuardPanel } from "@/components/ai/kb-gap-and-guard-panel";
import { TeamGovernancePanel } from "@/components/settings/team-governance-panel";
import { AuthSessionContext } from "@/components/providers";
import { fetchCallBody, fetchCallMethod, fetchCallUrl, normalizeFetchInput } from "@/__tests__/fetch-stub";
import { I18nProvider } from "@/lib/i18n";
import type { Principal } from "@/lib/contracts";
import type { ChannelAccountApiItem, TeamMemberApiItem } from "@/lib/api-client/live-client";
import type {
  AiAdoptionMetrics,
  HandoverResult,
  KbGapRead,
  RedlineRuleRead,
  StoreScopeRead,
} from "@/lib/api-client/types.gen";
import {
  accessLevelRank,
  assignStoreScope,
  canManageStore,
  canOperateStore,
  checkOutboundText,
  deleteRedlineRule,
  dismissKbGap,
  evaluateOutboundTextMock,
  fetchAiAdoptionMetrics,
  fetchKbGaps,
  fetchRedlineRules,
  fetchStoreScopes,
  formatRatePct,
  handoverImpactRows,
  handoverMemberAssets,
  promoteKbGap,
  resetTrack6Mocks,
  revokeStoreScope,
  validateRedlineDraft,
} from "@/lib/api-client/track6-team-kb";

vi.mock("@/hooks/use-realtime-inbox", () => ({ useRealtimeInbox: vi.fn() }));

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const MEMBERS: TeamMemberApiItem[] = [
  {
    membership_id: "mem-owner",
    tenant_id: "t1",
    user_id: "u-owner",
    email: "owner@qzled.example.com",
    full_name: "Zhou Owner",
    role: "owner",
    is_active: true,
    created_at: "2026-10-01T00:00:00Z",
  },
  {
    membership_id: "mem-agent",
    tenant_id: "t1",
    user_id: "u-agent",
    email: "agent@qzled.example.com",
    full_name: "Lin Wei",
    role: "agent",
    is_active: true,
    created_at: "2026-10-02T00:00:00Z",
  },
  {
    membership_id: "mem-leaver",
    tenant_id: "t1",
    user_id: "u-leaver",
    email: "leaver@qzled.example.com",
    full_name: "Chen Leaver",
    role: "agent",
    is_active: true,
    created_at: "2026-10-03T00:00:00Z",
  },
];

const ACCOUNTS: ChannelAccountApiItem[] = [
  {
    id: "ch-telegram",
    channel_type: "telegram",
    display_name: "QZ LED 外贸机器人",
    external_account_id: "7012345678",
    status: "active",
    rate_limit_profile: { mps: 30 },
    owner_user_id: "u-leaver",
    owner_name: "Chen Leaver",
    owner_email: "leaver@qzled.example.com",
    visibility_scope: "shared",
    bot_username: "qzled_sales_bot",
  },
  {
    id: "ch-web",
    channel_type: "web_livechat",
    display_name: "官网客服",
    external_account_id: "sk_live_existing",
    status: "active",
    rate_limit_profile: { mps: 50 },
    owner_user_id: null,
    owner_name: null,
    owner_email: null,
    visibility_scope: "shared",
    bot_username: null,
  },
];

const SCOPES: StoreScopeRead[] = [
  {
    id: "scope-1",
    tenant_id: "t1",
    user_id: "u-agent",
    user_email: "agent@qzled.example.com",
    user_full_name: "Lin Wei",
    channel_account_id: "ch-telegram",
    channel_type: "telegram",
    channel_display_name: "QZ LED 外贸机器人",
    access_level: "operate",
    created_at: "2026-11-20T08:00:00Z",
  },
  {
    id: "scope-2",
    tenant_id: "t1",
    user_id: "u-owner",
    user_email: "owner@qzled.example.com",
    user_full_name: "Zhou Owner",
    channel_account_id: "ch-web",
    channel_type: "web_livechat",
    channel_display_name: "官网客服",
    access_level: "manage",
    created_at: "2026-11-21T09:30:00Z",
  },
];

const RULES: RedlineRuleRead[] = [
  {
    id: "rule-1",
    tenant_id: "t1",
    name: "No deposit talk",
    pattern: "deposit",
    match_type: "keyword",
    action: "block",
    is_enabled: true,
    created_at: "2026-11-18T02:00:00Z",
  },
  {
    id: "rule-2",
    tenant_id: "t1",
    name: "No guaranteed lead time",
    pattern: "\\bguarantee[ds]?\\b",
    match_type: "regex",
    action: "warn",
    is_enabled: true,
    created_at: "2026-11-18T02:05:00Z",
  },
];

const GAPS: KbGapRead[] = [
  {
    id: "gap-1",
    tenant_id: "t1",
    conversation_id: "conv-1",
    customer_question: "Do you ship DDP to Chile?",
    detected_language: "en",
    frequency_count: 7,
    status: "open",
    resolved_answer: null,
    promoted_kb_document_id: null,
    created_at: "2026-11-19T04:00:00Z",
    updated_at: "2026-11-24T04:00:00Z",
  },
];

const ADOPTION: AiAdoptionMetrics = {
  tenant_id: "t1",
  window_days: 30,
  total: 10,
  pending: 2,
  approved: 3,
  edited_and_approved: 1,
  rejected: 2,
  expired: 1,
  failed: 1,
  adopted: 4,
  review_base: 6,
  adoption_rate_pct: "66.67",
  direct_adopt_rate_pct: "50.00",
  edit_adopt_rate_pct: "25.00",
  rejection_rate_pct: "33.33",
  by_type: [
    {
      proposal_type: "reply_draft",
      total: 7,
      pending: 2,
      approved: 3,
      edited_and_approved: 1,
      rejected: 1,
      adoption_rate_pct: "80.00",
    },
  ],
};

/** `bffFetch` omits `method` for GET, so normalise it here before matching. */
function callShape(
  input: RequestInfo | URL,
  init?: RequestInit
): { url: string; method: string } {
  const { url, method } = normalizeFetchInput(input, init);
  return { url, method: (method ?? "GET").toUpperCase() };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function provider(ui: React.ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const principal: Principal = {
    user_id: "u-owner",
    tenant_id: "t1",
    role: "owner",
    email: "owner@qzled.example.com",
  };
  return (
    <QueryClientProvider client={queryClient}>
      <AuthSessionContext.Provider
        value={{
          principal,
          isLoading: false,
          logout: async () => {},
          refreshSession: async () => {},
        }}
      >
        <I18nProvider defaultLocale="en">{ui}</I18nProvider>
      </AuthSessionContext.Provider>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  resetTrack6Mocks();
  vi.unstubAllEnvs();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

// ---------------------------------------------------------------------------
// Pure contract helpers
// ---------------------------------------------------------------------------

describe("track6 store-scope ranking helpers", () => {
  it("orders read < operate < manage exactly like the database CHECK", () => {
    expect(accessLevelRank("read")).toBe(0);
    expect(accessLevelRank("operate")).toBe(1);
    expect(accessLevelRank("manage")).toBe(2);

    expect(canOperateStore("read")).toBe(false);
    expect(canOperateStore("operate")).toBe(true);
    expect(canOperateStore("manage")).toBe(true);
    expect(canOperateStore(null)).toBe(false);
    expect(canOperateStore(undefined)).toBe(false);

    expect(canManageStore("operate")).toBe(false);
    expect(canManageStore("manage")).toBe(true);
    expect(canManageStore(null)).toBe(false);
  });

  it("renders server decimal percentages without any float arithmetic", () => {
    expect(formatRatePct("66.67")).toBe("66.67%");
    expect(formatRatePct("0.00")).toBe("0.00%");
    expect(formatRatePct(null)).toBe("0.00%");
    expect(formatRatePct("")).toBe("0.00%");
  });

  it("builds the handover impact rows from the server counts", () => {
    const result: HandoverResult = {
      tenant_id: "t1",
      from_user_id: "u-leaver",
      to_user_id: "u-agent",
      dry_run: true,
      opportunities_reassigned: 4,
      conversations_reassigned: 12,
      channel_accounts_reassigned: 2,
      store_scopes_reassigned: 3,
      total_reassigned: 21,
      from_user_deactivated: false,
    };
    const rows = handoverImpactRows(result);
    expect(rows.map((row) => row.key)).toEqual([
      "opportunities",
      "conversations",
      "channel_accounts",
      "store_scopes",
    ]);
    expect(rows.map((row) => row.count)).toEqual([4, 12, 2, 3]);
    expect(rows.reduce((sum, row) => sum + row.count, 0)).toBe(result.total_reassigned);
  });
});

describe("red-line draft validation mirrors the server", () => {
  it("accepts a plain keyword rule", () => {
    expect(
      validateRedlineDraft({ name: "Deposit", pattern: "deposit", match_type: "keyword", action: "block" })
    ).toEqual({ ok: true, error: null });
  });

  it("rejects an empty name, an empty pattern and an over-long pattern", () => {
    expect(
      validateRedlineDraft({ name: "  ", pattern: "x", match_type: "keyword", action: "block" }).ok
    ).toBe(false);
    expect(
      validateRedlineDraft({ name: "x", pattern: "  ", match_type: "keyword", action: "block" }).ok
    ).toBe(false);
    const tooLong = validateRedlineDraft({
      name: "x",
      pattern: "a".repeat(201),
      match_type: "regex",
      action: "block",
    });
    expect(tooLong.ok).toBe(false);
    expect(tooLong.error).toContain("200");
  });

  it("rejects an uncompilable regex but accepts a valid one", () => {
    expect(
      validateRedlineDraft({ name: "x", pattern: "(unclosed", match_type: "regex", action: "block" })
        .ok
    ).toBe(false);
    expect(
      validateRedlineDraft({ name: "x", pattern: "(un)?closed", match_type: "regex", action: "block" })
        .ok
    ).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Client module: live BFF contract
// ---------------------------------------------------------------------------

describe("track6 client module (live BFF contract)", () => {
  it("talks to the allowlisted /tenants and /ai routes only", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const { url, method } = callShape(input, init);
      if (url.endsWith("/v1/tenants/store-scopes") && method === "GET") return json(SCOPES);
      if (url.endsWith("/v1/tenants/store-scopes") && method === "POST") return json(SCOPES[0], 201);
      if (url.includes("/v1/tenants/store-scopes/") && method === "DELETE")
        return new Response(null, { status: 204 });
      if (url.endsWith("/v1/tenants/handover") && method === "POST")
        return json({
          tenant_id: "t1",
          from_user_id: "u-leaver",
          to_user_id: "u-agent",
          dry_run: true,
          opportunities_reassigned: 4,
          conversations_reassigned: 12,
          channel_accounts_reassigned: 2,
          store_scopes_reassigned: 3,
          total_reassigned: 21,
          from_user_deactivated: true,
        });
      if (url.endsWith("/v1/ai/redline-rules") && method === "GET") return json(RULES);
      if (url.includes("/v1/ai/redline-rules/") && method === "DELETE")
        return new Response(null, { status: 204 });
      if (url.endsWith("/v1/ai/redline-rules/check") && method === "POST")
        return json({
          blocked: true,
          blocks: ["deposit"],
          warnings: ["guaranteed"],
          matched_rules: [
            { id: "rule-1", name: "No deposit talk", match_type: "keyword", action: "block", matched_text: "deposit" },
          ],
        });
      if (url.includes("/v1/ai/kb-gaps?status=open")) return json({ items: GAPS, total: 1, limit: 50, offset: 0, has_more: false });
      if (url.includes("/promote") && method === "POST")
        return json({ gap: { ...GAPS[0], status: "resolved" }, document_id: "doc-9", chunks_created: 1 });
      if (url.includes("/dismiss") && method === "POST")
        return json({ ...GAPS[0], status: "dismissed" });
      if (url.includes("/v1/ai/adoption-metrics")) return json(ADOPTION);
      return json({}, 200);
    });
    vi.stubGlobal("fetch", fetchMock);

    expect(await fetchStoreScopes()).toHaveLength(2);
    await assignStoreScope({
      user_id: "u-agent",
      channel_account_id: "ch-telegram",
      access_level: "manage",
    });
    await revokeStoreScope("scope-1");
    await handoverMemberAssets({
      from_user_id: "u-leaver",
      to_user_id: "u-agent",
      deactivate_from_user: true,
      dry_run: true,
    });
    expect(await fetchRedlineRules()).toHaveLength(2);
    await deleteRedlineRule("rule-1");
    const verdict = await checkOutboundText("Deposit required, guaranteed.");
    expect(verdict.blocked).toBe(true);
    const page = await fetchKbGaps("open");
    expect(page.items).toHaveLength(1);
    const promoted = await promoteKbGap("gap-1", "Yes, DDP Santiago.");
    expect(promoted.document_id).toBe("doc-9");
    await dismissKbGap("gap-1");
    const metrics = await fetchAiAdoptionMetrics(30);
    expect(metrics.adoption_rate_pct).toBe("66.67");

    const urls = fetchMock.mock.calls.map((call) => fetchCallUrl(call));
    for (const url of urls) {
      expect(url.startsWith("/api/bff/v1/")).toBe(true);
      expect(url).not.toContain("/webhooks");
    }

    const grantCall = fetchMock.mock.calls.find(
      (call) =>
        fetchCallUrl(call).endsWith("/v1/tenants/store-scopes") && fetchCallMethod(call) === "POST"
    );
    expect(grantCall).toBeDefined();
    expect(JSON.parse(await fetchCallBody(grantCall!))).toEqual({
      user_id: "u-agent",
      channel_account_id: "ch-telegram",
      access_level: "manage",
    });

    const handoverCall = fetchMock.mock.calls.find((call) =>
      fetchCallUrl(call).endsWith("/v1/tenants/handover")
    );
    expect(JSON.parse(await fetchCallBody(handoverCall!))).toMatchObject({
      from_user_id: "u-leaver",
      to_user_id: "u-agent",
      deactivate_from_user: true,
      dry_run: true,
    });

    const checkCall = fetchMock.mock.calls.find((call) =>
      fetchCallUrl(call).endsWith("/v1/ai/redline-rules/check")
    );
    expect(JSON.parse(await fetchCallBody(checkCall!))).toEqual({
      content: "Deposit required, guaranteed.",
    });

    const promoteCall = fetchMock.mock.calls.find((call) =>
      fetchCallUrl(call).endsWith("/v1/ai/kb-gaps/gap-1/promote")
    );
    expect(JSON.parse(await fetchCallBody(promoteCall!))).toEqual({
      resolved_answer: "Yes, DDP Santiago.",
    });

    expect(urls.some((url) => url.includes("/v1/ai/adoption-metrics?days=30"))).toBe(true);
  });

  it("survives a proxy returning a non-contract body instead of crashing a render", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({}))
    );
    expect(await fetchStoreScopes()).toEqual([]);
    expect(await fetchRedlineRules()).toEqual([]);
    const page = await fetchKbGaps();
    expect(page.items).toEqual([]);
    expect(page.total).toBe(0);
    const metrics = await fetchAiAdoptionMetrics();
    expect(metrics.adoption_rate_pct).toBe("0.00");
    expect(metrics.by_type).toEqual([]);
    const verdict = await checkOutboundText("hello");
    expect(verdict).toEqual({ blocked: false, blocks: [], warnings: [], matched_rules: [] });
  });
});

// ---------------------------------------------------------------------------
// Client module: mock mode
// ---------------------------------------------------------------------------

describe("track6 client module (mock mode)", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
  });

  it("mutates the in-memory fixtures without touching the network", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    expect(await fetchStoreScopes()).toHaveLength(2);
    const created = await assignStoreScope({
      user_id: "u-leaver",
      channel_account_id: "ch-web",
      access_level: "read",
    });
    expect(created.access_level).toBe("read");
    expect(await fetchStoreScopes()).toHaveLength(3);

    await revokeStoreScope(created.id);
    expect(await fetchStoreScopes()).toHaveLength(2);

    const preview = await handoverMemberAssets({
      from_user_id: "u-mock-agent",
      to_user_id: "u-mock-manager",
      dry_run: true,
    });
    expect(preview.dry_run).toBe(true);
    expect(preview.store_scopes_reassigned).toBe(1);

    const gap = await promoteKbGap("gap-mock-1", "Yes, DDP Santiago 18 days.");
    expect(gap.gap.status).toBe("resolved");
    expect(gap.chunks_created).toBe(1);

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("evaluates block/warn rules with the same precedence as the server", async () => {
    const rules = await fetchRedlineRules();
    const clean = await checkOutboundText("We ship 500 units next week.");
    expect(clean.blocked).toBe(false);
    expect(clean.matched_rules).toEqual([]);

    const warned = await checkOutboundText("Delivery is guaranteed.");
    expect(warned.blocked).toBe(false);
    expect(warned.warnings).toEqual(["guaranteed"]);

    const blocked = await checkOutboundText("Please wire the DEPOSIT first.");
    expect(blocked.blocked).toBe(true);
    expect(blocked.blocks).toEqual(["deposit"]);

    const disabled = [...rules, { ...RULES[0], id: "rule-off", pattern: "discontinued", is_enabled: false }];
    expect(evaluateOutboundTextMock("This SKU is discontinued.", disabled).blocked).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Team governance panel
// ---------------------------------------------------------------------------

describe("TeamGovernancePanel", () => {
  it("renders nothing for a non-admin and never loads grants", async () => {
    const fetchMock = vi.fn(async () => json(SCOPES));
    vi.stubGlobal("fetch", fetchMock);
    const { container } = render(
      provider(
        <TeamGovernancePanel members={MEMBERS} accounts={ACCOUNTS} isAdmin={false} />
      )
    );
    expect(container.querySelector('[data-testid="team-governance-panel"]')).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("lists grants, assigns a new one and revokes a manage-level grant", async () => {
    let scopes = [...SCOPES];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const { url, method } = callShape(input, init);
      if (url.endsWith("/v1/tenants/store-scopes") && method === "GET") return json(scopes);
      if (url.endsWith("/v1/tenants/store-scopes") && method === "POST") {
        const body = JSON.parse(await fetchCallBody([input, init]));
        const created: StoreScopeRead = {
          id: "scope-new",
          tenant_id: "t1",
          user_id: body.user_id,
          user_email: "leaver@qzled.example.com",
          user_full_name: "Chen Leaver",
          channel_account_id: body.channel_account_id,
          channel_type: "web_livechat",
          channel_display_name: "官网客服",
          access_level: body.access_level,
          created_at: "2026-11-25T10:00:00Z",
        };
        scopes = [...scopes, created];
        return json(created, 201);
      }
      if (url.includes("/v1/tenants/store-scopes/scope-2") && method === "DELETE") {
        scopes = scopes.filter((scope) => scope.id !== "scope-2");
        return new Response(null, { status: 204 });
      }
      return json({}, 200);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(provider(<TeamGovernancePanel members={MEMBERS} accounts={ACCOUNTS} isAdmin />));

    await waitFor(() => {
      expect(screen.getByTestId("store-scope-row-scope-1")).toBeInTheDocument();
      expect(screen.getByTestId("store-scope-row-scope-2")).toBeInTheDocument();
    });
    // Only manage-level grants can be revoked from the table.
    expect(screen.getByTestId("store-scope-revoke-scope-1")).toBeDisabled();
    expect(screen.getByTestId("store-scope-revoke-scope-2")).not.toBeDisabled();

    fireEvent.change(screen.getByTestId("store-scope-member"), {
      target: { value: "u-leaver" },
    });
    fireEvent.change(screen.getByTestId("store-scope-account"), {
      target: { value: "ch-web" },
    });
    fireEvent.change(screen.getByTestId("store-scope-level"), {
      target: { value: "manage" },
    });
    fireEvent.click(screen.getByTestId("store-scope-assign"));

    await waitFor(() => {
      expect(screen.getByTestId("store-scope-row-scope-new")).toBeInTheDocument();
    });
    const postCall = fetchMock.mock.calls.find(
      (call) =>
        fetchCallUrl(call).endsWith("/v1/tenants/store-scopes") && fetchCallMethod(call) === "POST"
    );
    expect(JSON.parse(await fetchCallBody(postCall!))).toEqual({
      user_id: "u-leaver",
      channel_account_id: "ch-web",
      access_level: "manage",
    });

    fireEvent.click(screen.getByTestId("store-scope-revoke-scope-2"));
    await waitFor(() => {
      expect(screen.queryByTestId("store-scope-row-scope-2")).toBeNull();
    });
  });

  it("previews the handover before enabling the destructive execute", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const { url, method } = callShape(input, init);
      if (url.endsWith("/v1/tenants/store-scopes") && method === "GET") return json(SCOPES);
      if (url.endsWith("/v1/tenants/handover") && method === "POST") {
        const body = JSON.parse(await fetchCallBody([input, init]));
        return json({
          tenant_id: "t1",
          from_user_id: body.from_user_id,
          to_user_id: body.to_user_id,
          dry_run: body.dry_run,
          opportunities_reassigned: 4,
          conversations_reassigned: 12,
          channel_accounts_reassigned: 2,
          store_scopes_reassigned: 3,
          total_reassigned: 21,
          from_user_deactivated: Boolean(body.deactivate_from_user),
        });
      }
      return json({}, 200);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(provider(<TeamGovernancePanel members={MEMBERS} accounts={ACCOUNTS} isAdmin />));
    await waitFor(() => expect(screen.getByTestId("store-scope-row-scope-1")).toBeInTheDocument());

    expect(screen.getByTestId("handover-execute")).toBeDisabled();

    fireEvent.change(screen.getByTestId("handover-from"), {
      target: { value: "u-leaver" },
    });
    fireEvent.change(screen.getByTestId("handover-to"), {
      target: { value: "u-agent" },
    });
    // The successor picker can never offer the leaver again.
    const successorOptions = Array.from(
      (screen.getByTestId("handover-to") as HTMLSelectElement).options
    ).map((option) => option.value);
    expect(successorOptions).not.toContain("u-leaver");

    fireEvent.click(screen.getByTestId("handover-preview"));

    await waitFor(() => {
      expect(screen.getByTestId("handover-result")).toHaveAttribute("data-dry-run", "true");
    });
    expect(screen.getByTestId("handover-count-opportunities").textContent).toContain("4");
    expect(screen.getByTestId("handover-count-conversations").textContent).toContain("12");
    expect(screen.getByTestId("handover-count-store_scopes").textContent).toContain("3");
    expect(screen.getByTestId("handover-total").textContent).toContain("21");
    expect(screen.getByTestId("handover-execute")).toBeDisabled();

    const previewCall = fetchMock.mock.calls.find((call) =>
      fetchCallUrl(call).endsWith("/v1/tenants/handover")
    );
    expect(JSON.parse(await fetchCallBody(previewCall!))).toMatchObject({ dry_run: true });

    fireEvent.click(screen.getByTestId("handover-confirm"));
    expect(screen.getByTestId("handover-execute")).not.toBeDisabled();

    fireEvent.click(screen.getByTestId("handover-execute"));
    await waitFor(() => {
      expect(screen.getByTestId("handover-result")).toHaveAttribute("data-dry-run", "false");
    });
    const executeCall = fetchMock.mock.calls
      .filter((call) => fetchCallUrl(call).endsWith("/v1/tenants/handover"))
      .at(-1);
    expect(JSON.parse(await fetchCallBody(executeCall!))).toMatchObject({ dry_run: false });
  });
});

// ---------------------------------------------------------------------------
// AI adoption + red-line + KB gap panel
// ---------------------------------------------------------------------------

describe("KbGapAndGuardPanel", () => {
  function panelFetch(overrides: { promoteStatus?: number } = {}) {
    let gaps = [...GAPS];
    let rules = [...RULES];
    return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const { url, method } = callShape(input, init);
      if (url.includes("/v1/ai/adoption-metrics")) return json(ADOPTION);
      if (url.endsWith("/v1/ai/redline-rules") && method === "GET") return json(rules);
      if (url.endsWith("/v1/ai/redline-rules") && method === "POST") {
        const body = JSON.parse(await fetchCallBody([input, init]));
        const created: RedlineRuleRead = {
          id: "rule-new",
          tenant_id: "t1",
          name: body.name,
          pattern: body.pattern,
          match_type: body.match_type,
          action: body.action,
          is_enabled: true,
          created_at: "2026-11-25T10:00:00Z",
        };
        rules = [...rules, created];
        return json(created, 201);
      }
      if (url.includes("/v1/ai/redline-rules/rule-1") && method === "DELETE") {
        rules = rules.filter((rule) => rule.id !== "rule-1");
        return new Response(null, { status: 204 });
      }
      if (url.endsWith("/v1/ai/redline-rules/check") && method === "POST") {
        const body = JSON.parse(await fetchCallBody([input, init]));
        const matched = rules
          .filter((rule) => body.content.toLowerCase().includes(rule.pattern.toLowerCase()))
          .map((rule) => ({
            id: rule.id,
            name: rule.name,
            match_type: rule.match_type,
            action: rule.action,
            matched_text: rule.pattern,
          }));
        return json({
          blocked: matched.some((rule) => rule.action === "block"),
          blocks: matched.filter((rule) => rule.action === "block").map((rule) => rule.matched_text),
          warnings: matched.filter((rule) => rule.action === "warn").map((rule) => rule.matched_text),
          matched_rules: matched,
        });
      }
      if (url.includes("/v1/ai/kb-gaps") && method === "GET") {
        return json({ items: gaps, total: gaps.length, limit: 50, offset: 0, has_more: false });
      }
      if (url.includes("/promote") && method === "POST") {
        if (overrides.promoteStatus && overrides.promoteStatus !== 200) {
          return json({ code: "ERR_KB_GAP_DISMISSED", detail: "dismissed" }, overrides.promoteStatus);
        }
        const gapId = url.split("/").slice(-2)[0];
        gaps = gaps.map((gap) =>
          gap.id === gapId
            ? { ...gap, status: "resolved", promoted_kb_document_id: "doc-9", resolved_answer: "Yes." }
            : gap
        );
        return json({ gap: gaps[0], document_id: "doc-9", chunks_created: 1 });
      }
      if (url.includes("/dismiss") && method === "POST") {
        gaps = gaps.map((gap) => ({ ...gap, status: "dismissed" }));
        return json(gaps[0]);
      }
      return json({}, 200);
    });
  }

  it("shows the server adoption rates as exact decimal percentages", async () => {
    vi.stubGlobal("fetch", panelFetch());
    render(provider(<KbGapAndGuardPanel />));

    await waitFor(() => {
      expect(screen.getByTestId("adoption-rate").textContent).toContain("66.67%");
    });
    expect(screen.getByTestId("adoption-total").textContent).toContain("10");
    expect(screen.getByTestId("adoption-adopted").textContent).toContain("4");
    expect(screen.getByTestId("adoption-edit-rate").textContent).toContain("25.00%");
    expect(screen.getByTestId("adoption-edit-rate").textContent).toContain("33.33%");
    expect(screen.getByTestId("adoption-type-reply_draft").textContent).toContain("80.00%");
  });

  it("blocks an outbound message through the live tester", async () => {
    vi.stubGlobal("fetch", panelFetch());
    render(provider(<KbGapAndGuardPanel />));

    await waitFor(() => expect(screen.getByTestId("redline-rule-rule-1")).toBeInTheDocument());

    fireEvent.change(screen.getByTestId("redline-test-input"), {
      target: { value: "Please wire the deposit first." },
    });
    fireEvent.click(screen.getByTestId("redline-test-run"));

    await waitFor(() => {
      expect(screen.getByTestId("redline-test-blocked").textContent).toContain("Blocked");
    });
    expect(screen.getByTestId("redline-match-rule-1").textContent).toContain("deposit");
  });

  it("rejects an invalid regex locally without calling the API", async () => {
    const fetchMock = panelFetch();
    vi.stubGlobal("fetch", fetchMock);
    render(provider(<KbGapAndGuardPanel />));
    await waitFor(() => expect(screen.getByTestId("redline-rule-rule-1")).toBeInTheDocument());
    const callsBefore = fetchMock.mock.calls.length;

    fireEvent.change(screen.getByTestId("redline-name"), { target: { value: "Broken" } });
    fireEvent.change(screen.getByTestId("redline-pattern"), { target: { value: "(unclosed" } });
    fireEvent.change(screen.getByTestId("redline-match-type"), { target: { value: "regex" } });
    fireEvent.click(screen.getByTestId("redline-add"));

    await waitFor(() => {
      expect(screen.getByTestId("redline-error").textContent).toContain("regex");
    });
    expect(fetchMock.mock.calls.length).toBe(callsBefore);
  });

  it("creates and deletes a red-line rule", async () => {
    const fetchMock = panelFetch();
    vi.stubGlobal("fetch", fetchMock);
    render(provider(<KbGapAndGuardPanel />));
    await waitFor(() => expect(screen.getByTestId("redline-rule-rule-1")).toBeInTheDocument());

    fireEvent.change(screen.getByTestId("redline-name"), { target: { value: "No refund promise" } });
    fireEvent.change(screen.getByTestId("redline-pattern"), { target: { value: "full refund" } });
    fireEvent.click(screen.getByTestId("redline-add"));

    await waitFor(() => expect(screen.getByTestId("redline-rule-rule-new")).toBeInTheDocument());
    const postCall = fetchMock.mock.calls.find(
      (call) =>
        fetchCallUrl(call).endsWith("/v1/ai/redline-rules") && fetchCallMethod(call) === "POST"
    );
    expect(JSON.parse(await fetchCallBody(postCall!))).toMatchObject({
      name: "No refund promise",
      pattern: "full refund",
      match_type: "keyword",
      action: "block",
    });

    fireEvent.click(screen.getByTestId("redline-delete-rule-1"));
    await waitFor(() => expect(screen.queryByTestId("redline-rule-rule-1")).toBeNull());
  });

  it("promotes a KB gap with the inline answer editor", async () => {
    const fetchMock = panelFetch();
    vi.stubGlobal("fetch", fetchMock);
    render(provider(<KbGapAndGuardPanel />));

    await waitFor(() => expect(screen.getByTestId("kb-gap-gap-1")).toBeInTheDocument());
    expect(screen.getByTestId("kb-gap-gap-1").textContent).toContain("7");

    fireEvent.click(screen.getByTestId("kb-gap-promote-gap-1"));
    const editor = await screen.findByTestId("kb-gap-answer-gap-1");
    fireEvent.change(editor, { target: { value: "Yes, we ship DDP to Chile." } });
    fireEvent.click(screen.getByTestId("kb-gap-answer-submit-gap-1"));

    await waitFor(() => {
      expect(screen.getByTestId("kb-gap-gap-1").textContent).toContain("Promoted to KB");
    });
    const promoteCall = fetchMock.mock.calls.find((call) =>
      fetchCallUrl(call).endsWith("/v1/ai/kb-gaps/gap-1/promote")
    );
    expect(JSON.parse(await fetchCallBody(promoteCall!))).toEqual({
      resolved_answer: "Yes, we ship DDP to Chile.",
    });
  });

  it("surfaces a 409 when a dismissed gap is promoted again", async () => {
    vi.stubGlobal("fetch", panelFetch({ promoteStatus: 409 }));
    render(provider(<KbGapAndGuardPanel />));

    await waitFor(() => expect(screen.getByTestId("kb-gap-gap-1")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("kb-gap-promote-gap-1"));
    fireEvent.change(await screen.findByTestId("kb-gap-answer-gap-1"), {
      target: { value: "Yes, we ship DDP to Chile." },
    });
    fireEvent.click(screen.getByTestId("kb-gap-answer-submit-gap-1"));

    await waitFor(() => {
      expect(screen.getByTestId("kb-gap-error")).toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// Page mounting
// ---------------------------------------------------------------------------

describe("panels are mounted on the real pages", () => {
  it("renders the gap/guard panel on /ai/proposals", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const { url } = callShape(input, init);
        if (url.includes("/ai/proposals")) {
          return json({ items: [], total: 0, limit: 20, offset: 0, has_more: false });
        }
        if (url.includes("/ai/adoption-metrics")) return json(ADOPTION);
        if (url.endsWith("/ai/redline-rules")) return json(RULES);
        if (url.includes("/ai/kb-gaps")) {
          return json({ items: GAPS, total: 1, limit: 50, offset: 0, has_more: false });
        }
        return json({}, 200);
      })
    );

    render(provider(<AiProposalsPage />));

    expect(await screen.findByTestId("kb-gap-and-guard-panel")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByTestId("adoption-rate").textContent).toContain("66.67%");
      expect(screen.getByTestId("kb-gap-gap-1")).toBeInTheDocument();
    });
  });
});
