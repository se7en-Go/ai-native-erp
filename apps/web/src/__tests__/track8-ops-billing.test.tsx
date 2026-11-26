// Track 8 UI suite: production ops health + tenant billing wallet panel on
// /settings/channels → "运维与合规", plus the client module's locked contract
// (`/api/bff/v1/ops/health`, `/api/bff/v1/billing/wallet`, `/v1/billing/usage`).
//
// The client cases run the real `bffFetch` path so URL/method are verified
// against the backend route names; the panel cases render the real component
// against the mutable mock fixtures. No number is recomputed in the browser —
// latency, memory, backlog and credit balances are asserted exactly as the
// backend serialises them.
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { OpsHealthPanel } from "@/components/settings/ops-health-panel";
import { normalizeFetchInput } from "@/__tests__/fetch-stub";
import { I18nProvider } from "@/lib/i18n";
import {
  bytesToHuman,
  fetchBillingUsage,
  fetchBillingWallet,
  fetchOpsHealth,
  fetchOpsSnapshot,
  formatMilliseconds,
  mockOpsRefreshCount,
  normalizeOpsHealth,
  opsStatusLabel,
  opsStatusTone,
  overageSummary,
  resetTrack8Mocks,
  totalFailedEvents,
} from "@/lib/api-client/track8-ops";

function callShape(input: RequestInfo | URL, init?: RequestInit): { url: string; method: string } {
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
  return (
    <QueryClientProvider client={queryClient}>
      <I18nProvider defaultLocale="en">{ui}</I18nProvider>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  resetTrack8Mocks();
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

// ---------------------------------------------------------------------------
// Pure contract helpers
// ---------------------------------------------------------------------------

describe("track8 ops presentation helpers", () => {
  it("maps every backend status to a badge tone and a bilingual label", () => {
    expect(opsStatusTone("ok")).toBe("success");
    expect(opsStatusTone("warn")).toBe("warning");
    expect(opsStatusTone("critical")).toBe("danger");
    expect(opsStatusLabel("ok", "en")).toBe("Healthy");
    expect(opsStatusLabel("warn", "zh")).toBe("降级");
    expect(opsStatusLabel("critical", "en")).toBe("Critical");
  });

  it("formats Redis bytes and latency without inventing precision", () => {
    expect(bytesToHuman(512)).toBe("512 B");
    expect(bytesToHuman(12_345_678)).toBe("11.8 MB");
    expect(bytesToHuman(null)).toBe("—");
    expect(formatMilliseconds(2.74)).toBe("2.7 ms");
    expect(formatMilliseconds(Number.NaN)).toBe("—");
  });

  it("surfaces overage spend with an explicit off switch", () => {
    expect(
      overageSummary({
        tenant_id: "t1",
        plan_tier: "starter",
        included_credits_remaining: 0,
        topup_credits_balance: 0,
        total_credits_available: 0,
        fractional_credit_carry: "0",
        overage_spend_cap_cents: 0,
        overage_spent_cents: 0,
        auto_downgrade_to_l0: true,
        billing_cycle_end: "2026-12-20T00:00:00Z",
        updated_at: "2026-11-26T00:00:00Z",
      })
    ).toBe("0.00 / 0.00 USD (off)");
  });

  it("never crashes on a partial/garbage payload", () => {
    const health = normalizeOpsHealth({
      status: "unexpected",
      postgres: { latency_ms: "not-a-number", raw_webhook_status_counts: { dead_letter: 2, x: "y" } },
      streams: [{ stream: "stream:erp:events", length: 3 }],
    });
    expect(health.status).toBe("critical");
    expect(health.postgres.latency_ms).toBe(0);
    expect(health.postgres.raw_webhook_status_counts).toEqual({ dead_letter: 2 });
    expect(health.streams).toHaveLength(1);
    expect(health.streams[0].pending).toBeNull();
    expect(health.queue_warn_depth).toBe(100);
    expect(totalFailedEvents(health)).toBe(2);
    expect(normalizeOpsHealth(null).alerts).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// Client module contract (live BFF path)
// ---------------------------------------------------------------------------

describe("track8 client module", () => {
  it("fetches ops health, wallet and usage from the operator BFF routes", async () => {
    const seen: { url: string; method: string }[] = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const shape = callShape(input, init);
      seen.push(shape);
      if (shape.url.endsWith("/v1/ops/health")) {
        return json({
          status: "ok",
          checked_at: "2026-11-26T02:30:00Z",
          queue_warn_depth: 100,
          queue_critical_depth: 1000,
          max_backlog: 4,
          alerts: [],
          postgres: {
            status: "ok",
            latency_ms: 1.5,
            alembic_version: "0025_team_ops_kb_gaps",
            raw_webhook_status_counts: { processed: 10 },
            notification_deliveries_pending: 0,
            inventory_sync_jobs_pending: 0,
            dead_letter_events: 0,
          },
          redis: {
            status: "ok",
            latency_ms: 0.4,
            used_memory_bytes: 2048,
            used_memory_human: "2.00K",
            connected_clients: 3,
          },
          streams: [{ stream: "stream:erp:events", length: 4, pending: 0, consumers: 1 }],
        });
      }
      if (shape.url.endsWith("/v1/billing/wallet")) {
        return json({
          tenant_id: "t1",
          plan_tier: "growth",
          included_credits_remaining: 6400,
          topup_credits_balance: 120,
          total_credits_available: 6520,
          fractional_credit_carry: "0.2500",
          overage_spend_cap_cents: 5000,
          overage_spent_cents: 0,
          auto_downgrade_to_l0: true,
          billing_cycle_end: "2026-12-20T00:00:00Z",
          updated_at: "2026-11-26T02:30:00Z",
        });
      }
      if (shape.url.includes("/v1/billing/usage")) {
        return json({ items: [], total: 0, limit: 5, offset: 0, has_more: false });
      }
      throw new Error(`unexpected fetch: ${shape.url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "false");

    const snapshot = await fetchOpsSnapshot();

    expect(seen.map((call) => `${call.method} ${call.url}`)).toEqual([
      "GET /api/bff/v1/ops/health",
      "GET /api/bff/v1/billing/wallet",
      "GET /api/bff/v1/billing/usage?limit=5&offset=0",
    ]);
    expect(snapshot.health.status).toBe("ok");
    expect(snapshot.health.postgres.latency_ms).toBe(1.5);
    expect(snapshot.health.streams[0].stream).toBe("stream:erp:events");
    expect(snapshot.wallet.total_credits_available).toBe(6520);
    expect(snapshot.usage.items).toEqual([]);
  });

  it("keeps the three data accessors independent and typed", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const shape = callShape(input, init);
      if (shape.url.endsWith("/v1/ops/health")) return json({ status: "warn" });
      if (shape.url.endsWith("/v1/billing/wallet")) return json({ plan_tier: "starter" });
      return json({ items: [{ id: 1 }], total: 1, limit: 5, offset: 0, has_more: false });
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "false");

    const health = await fetchOpsHealth();
    expect(health.status).toBe("warn");
    expect(health.max_backlog).toBe(0);
    expect((await fetchBillingWallet()).plan_tier).toBe("starter");
    expect(await fetchBillingUsage(5)).toMatchObject({ total: 1, items: [{ id: 1 }] });
  });
});

// ---------------------------------------------------------------------------
// Panel rendering (mock mode fixtures)
// ---------------------------------------------------------------------------

describe("OpsHealthPanel", () => {
  it("renders status, latency, memory, backlog and wallet from the server snapshot", async () => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");

    render(provider(<OpsHealthPanel isAdmin />));

    await waitFor(() =>
      expect(screen.getByTestId("ops-status-badge")).toHaveTextContent("Degraded")
    );
    expect(screen.getByTestId("ops-db-latency")).toHaveTextContent("2.7 ms");
    expect(screen.getByTestId("ops-redis-memory")).toHaveTextContent("11.77M");
    expect(screen.getByTestId("ops-max-backlog")).toHaveTextContent("128");
    // dead-lettered raw webhook (1) + retained DLQ event (1) = 2 operator items.
    expect(screen.getByTestId("ops-failed-events")).toHaveTextContent("2");
    expect(screen.getByTestId("ops-alerts")).toHaveTextContent("stream:webhooks:whatsapp_cloud");

    await waitFor(() => expect(screen.getAllByTestId("ops-stream-row")).toHaveLength(3));
    const rows = screen.getAllByTestId("ops-stream-row");
    expect(rows[0]).toHaveTextContent("stream:webhooks:whatsapp_cloud");
    expect(rows[0]).toHaveTextContent("128");

    expect(screen.getByTestId("ops-wallet-plan")).toHaveTextContent("growth");
    expect(screen.getByTestId("ops-wallet-balance")).toHaveTextContent("6520");
    expect(screen.getByTestId("ops-usage-table")).toHaveTextContent("stripe_downgrade");
    expect(screen.getAllByTestId("ops-usage-row")).toHaveLength(2);
  });

  it("refresh re-reads the backend snapshot", async () => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");

    render(provider(<OpsHealthPanel isAdmin />));
    await waitFor(() =>
      expect(screen.getByTestId("ops-status-badge")).toHaveTextContent("Degraded")
    );
    await waitFor(() => expect(screen.getByTestId("ops-refresh-btn")).toBeEnabled());
    const before = mockOpsRefreshCount();

    fireEvent.click(screen.getByTestId("ops-refresh-btn"));

    await waitFor(() => expect(mockOpsRefreshCount()).toBeGreaterThan(before));
    expect(screen.getByTestId("ops-status-badge")).toHaveTextContent("Degraded");
  });

  it("renders nothing for non-admin roles (defence in depth)", () => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");

    const { container } = render(provider(<OpsHealthPanel isAdmin={false} />));

    expect(container.querySelector('[data-testid="ops-health-panel"]')).toBeNull();
    expect(screen.queryByTestId("ops-status-badge")).toBeNull();
  });

  it("shows an actionable error state when the ops endpoint fails", async () => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "false");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        json(
          {
            type: "https://erp.local/errors/insufficient-role",
            title: "Insufficient Role Permissions",
            status: 403,
            detail: "Role 'agent' is not authorized for this operation",
            code: "ERR_INSUFFICIENT_ROLE",
          },
          403
        )
      )
    );

    render(provider(<OpsHealthPanel isAdmin />));

    expect(await screen.findByTestId("ops-health-error")).toHaveTextContent(
      "/readyz and /metrics"
    );
  });
});
