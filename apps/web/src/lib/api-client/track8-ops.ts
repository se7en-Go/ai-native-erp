// Track 8 client module: production ops health (`GET /api/v1/ops/health`) and the
// tenant billing wallet (`/api/v1/billing/wallet` + `/api/v1/billing/usage`).
//
// Every request travels through `bffFetch` (`/api/bff/v1/...`), so the session
// cookie, the 401 refresh flow and the BFF role gate behave exactly like the
// rest of the workbench. The backend is authoritative for every number here:
// latency, Redis memory, stream backlog, dead-letter counts and credit balances
// are rendered as received — the browser never recomputes a health verdict.
//
// Mock mode (`NEXT_PUBLIC_USE_MOCK_API=true`) never reaches the network; the
// fixtures are mutable so a refresh in mock mode still returns server-shaped
// data.
import { bffFetch } from "@/lib/api";
import { shouldUseMockApi } from "@/lib/data-source";

export type OpsStatus = "ok" | "warn" | "critical";
export type OpsProbeStatus = "ok" | "critical";

// The wire contract is generated from FastAPI (`contracts/openapi.json` ->
// `types.gen.ts`), so this module never invents backend shapes: it only narrows
// the generated (partially optional) types into the fully-populated shapes the
// panel renders, and the compile-time guards at the bottom prove they still
// match the OpenAPI contract.
import type {
  CreditWalletRead,
  OpsHealthRead as GeneratedOpsHealthRead,
  OpsPostgresRead,
  OpsRedisRead,
  OpsStreamRead,
  PaginatedResponseUsageLedgerRead,
  UsageLedgerRead,
} from "@/lib/api-client/types.gen";

export type BillingWalletRead = CreditWalletRead;
export type BillingUsageEntry = UsageLedgerRead;
export type BillingUsagePage = PaginatedResponseUsageLedgerRead;

export interface OpsPostgresHealth {
  status: OpsProbeStatus;
  latency_ms: number;
  alembic_version: string | null;
  raw_webhook_status_counts: Record<string, number>;
  notification_deliveries_pending: number;
  inventory_sync_jobs_pending: number;
  dead_letter_events: number;
}

export interface OpsRedisHealth {
  status: OpsProbeStatus;
  latency_ms: number;
  used_memory_bytes: number | null;
  used_memory_human: string | null;
  connected_clients: number | null;
}

/** Fully-populated health snapshot (same contract, no optional fields left). */
export interface OpsHealthRead {
  status: OpsStatus;
  checked_at: string;
  queue_warn_depth: number;
  queue_critical_depth: number;
  max_backlog: number;
  alerts: string[];
  postgres: OpsPostgresHealth;
  redis: OpsRedisHealth;
  streams: OpsStreamRead[];
}

export type OpsStreamHealth = OpsStreamRead;

export interface OpsBillingSnapshot {
  health: OpsHealthRead;
  wallet: BillingWalletRead;
  usage: BillingUsagePage;
}

// Compile-time drift guards: `pnpm typecheck` fails the moment a backend schema
// change makes these normalized shapes incompatible with the generated contract.
type AssertAssignable<T extends U, U> = T;
export type OpsHealthContractGuard = AssertAssignable<OpsHealthRead, GeneratedOpsHealthRead>;
export type OpsPostgresContractGuard = AssertAssignable<OpsPostgresHealth, OpsPostgresRead>;
export type OpsRedisContractGuard = AssertAssignable<OpsRedisHealth, OpsRedisRead>;

// ---------------------------------------------------------------------------
// Defensive normalisation: a proxy/older backend must never crash the render.
// ---------------------------------------------------------------------------

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function asArray<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

function asNumber(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function asString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function asNullableNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function asStatus(value: unknown, allowed: readonly string[]): OpsStatus {
  return allowed.includes(String(value)) ? (String(value) as OpsStatus) : "critical";
}

function normaliseCounts(value: unknown): Record<string, number> {
  const source = asRecord(value);
  const counts: Record<string, number> = {};
  for (const [key, raw] of Object.entries(source)) {
    if (typeof raw === "number" && Number.isFinite(raw)) counts[key] = raw;
  }
  return counts;
}

export function normalizeOpsHealth(raw: unknown): OpsHealthRead {
  const health = asRecord(raw);
  const postgres = asRecord(health.postgres);
  const redis = asRecord(health.redis);
  return {
    status: asStatus(health.status, ["ok", "warn", "critical"]),
    checked_at: asString(health.checked_at),
    queue_warn_depth: asNumber(health.queue_warn_depth, 100),
    queue_critical_depth: asNumber(health.queue_critical_depth, 1000),
    max_backlog: asNumber(health.max_backlog),
    alerts: asArray<string>(health.alerts).filter((a): a is string => typeof a === "string"),
    postgres: {
      status: asStatus(postgres.status, ["ok", "critical"]) as OpsProbeStatus,
      latency_ms: asNumber(postgres.latency_ms),
      alembic_version:
        typeof postgres.alembic_version === "string" ? postgres.alembic_version : null,
      raw_webhook_status_counts: normaliseCounts(postgres.raw_webhook_status_counts),
      notification_deliveries_pending: asNumber(postgres.notification_deliveries_pending),
      inventory_sync_jobs_pending: asNumber(postgres.inventory_sync_jobs_pending),
      dead_letter_events: asNumber(postgres.dead_letter_events),
    },
    redis: {
      status: asStatus(redis.status, ["ok", "critical"]) as OpsProbeStatus,
      latency_ms: asNumber(redis.latency_ms),
      used_memory_bytes: asNullableNumber(redis.used_memory_bytes),
      used_memory_human:
        typeof redis.used_memory_human === "string" ? redis.used_memory_human : null,
      connected_clients: asNullableNumber(redis.connected_clients),
    },
    streams: asArray<Record<string, unknown>>(health.streams).map((stream) => ({
      stream: asString(stream.stream),
      length: asNumber(stream.length),
      pending: asNullableNumber(stream.pending),
      consumers: asNullableNumber(stream.consumers),
    })),
  };
}

export function normalizeWallet(raw: unknown): BillingWalletRead {
  const wallet = asRecord(raw);
  return {
    tenant_id: asString(wallet.tenant_id),
    plan_tier: asString(wallet.plan_tier, "starter"),
    included_credits_remaining: asNumber(wallet.included_credits_remaining),
    topup_credits_balance: asNumber(wallet.topup_credits_balance),
    total_credits_available: asNumber(wallet.total_credits_available),
    fractional_credit_carry: asString(wallet.fractional_credit_carry, "0"),
    overage_spend_cap_cents: asNumber(wallet.overage_spend_cap_cents),
    overage_spent_cents: asNumber(wallet.overage_spent_cents),
    auto_downgrade_to_l0: wallet.auto_downgrade_to_l0 !== false,
    billing_cycle_end: asString(wallet.billing_cycle_end),
    updated_at: asString(wallet.updated_at),
  };
}

export function normalizeUsagePage(raw: unknown): BillingUsagePage {
  const page = asRecord(raw);
  const items = asArray<Record<string, unknown>>(page.items).map((entry) => ({
    id: asNumber(entry.id),
    idempotency_key: asString(entry.idempotency_key),
    metric_type: asString(entry.metric_type) as BillingUsageEntry["metric_type"],
    units_consumed: asString(entry.units_consumed, "0"),
    raw_provider_cost_usd: asString(entry.raw_provider_cost_usd, "0"),
    model_or_provider: asString(entry.model_or_provider),
    stripe_meter_synced: entry.stripe_meter_synced === true,
    stripe_meter_event_id:
      typeof entry.stripe_meter_event_id === "string" ? entry.stripe_meter_event_id : null,
    created_at: asString(entry.created_at),
  }));
  return {
    items,
    total: asNumber(page.total, items.length),
    limit: asNumber(page.limit, 20),
    offset: asNumber(page.offset),
    has_more: page.has_more === true,
  };
}

// ---------------------------------------------------------------------------
// Presentation helpers (pure, unit-testable)
// ---------------------------------------------------------------------------

export type OpsTone = "success" | "warning" | "danger";

export function opsStatusTone(status: OpsStatus): OpsTone {
  if (status === "ok") return "success";
  if (status === "warn") return "warning";
  return "danger";
}

export function opsStatusLabel(status: OpsStatus, locale: "zh" | "en" = "zh"): string {
  const labels: Record<OpsStatus, { zh: string; en: string }> = {
    ok: { zh: "健康", en: "Healthy" },
    warn: { zh: "降级", en: "Degraded" },
    critical: { zh: "严重", en: "Critical" },
  };
  return labels[status][locale];
}

/** Dead-lettered webhook rows plus retained DLQ events — the operator's backlog. */
export function totalFailedEvents(health: OpsHealthRead): number {
  const counts = health.postgres.raw_webhook_status_counts;
  return (
    health.postgres.dead_letter_events +
    (counts.dead_letter ?? 0) +
    (counts.failed ?? 0)
  );
}

export function bytesToHuman(bytes: number | null): string {
  if (bytes === null || !Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(1)} ${units[unit]}`;
}

/** `3.2` → `"3.2 ms"`; keeps one decimal so a slow probe is visible. */
export function formatMilliseconds(value: number): string {
  if (!Number.isFinite(value) || value < 0) return "—";
  return `${value.toFixed(1)} ms`;
}

export function overageSummary(wallet: BillingWalletRead): string {
  if (wallet.overage_spend_cap_cents <= 0) return "0.00 / 0.00 USD (off)";
  return `${(wallet.overage_spent_cents / 100).toFixed(2)} / ${(
    wallet.overage_spend_cap_cents / 100
  ).toFixed(2)} USD`;
}

// ---------------------------------------------------------------------------
// Mock fixtures (mutable so refresh-in-mock-mode still works)
// ---------------------------------------------------------------------------

const MOCK_OPS_HEALTH: OpsHealthRead = {
  status: "warn",
  checked_at: "2026-11-26T02:30:00Z",
  queue_warn_depth: 100,
  queue_critical_depth: 1000,
  max_backlog: 128,
  alerts: ["Queue backlog 128 on 'stream:webhooks:whatsapp_cloud' exceeded the warn depth 100."],
  postgres: {
    status: "ok",
    latency_ms: 2.7,
    alembic_version: "0025_team_ops_kb_gaps",
    raw_webhook_status_counts: { processed: 4120, queued: 3, dead_letter: 1 },
    notification_deliveries_pending: 2,
    inventory_sync_jobs_pending: 0,
    dead_letter_events: 1,
  },
  redis: {
    status: "ok",
    latency_ms: 0.6,
    used_memory_bytes: 12_345_678,
    used_memory_human: "11.77M",
    connected_clients: 14,
  },
  streams: [
    {
      stream: "stream:webhooks:whatsapp_cloud",
      length: 128,
      pending: 4,
      consumers: 2,
    },
    { stream: "stream:outbound:messages", length: 6, pending: 0, consumers: 1 },
    { stream: "stream:erp:events", length: 0, pending: 0, consumers: null },
  ],
};

const MOCK_WALLET: BillingWalletRead = {
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
};

const MOCK_USAGE: BillingUsagePage = {
  items: [
    {
      id: 9071,
      idempotency_key: "chat:conv-42",
      metric_type: "ai_credit",
      units_consumed: "1.5000",
      raw_provider_cost_usd: "0.012345",
      model_or_provider: "gpt-4o-mini",
      stripe_meter_synced: true,
      stripe_meter_event_id: "mtr_1",
      created_at: "2026-11-26T02:10:00Z",
    },
    {
      id: 9070,
      idempotency_key: "billing:event:in_ops_failed_1",
      metric_type: "ai_credit",
      units_consumed: "0.0000",
      raw_provider_cost_usd: "0",
      model_or_provider: "stripe_downgrade",
      stripe_meter_synced: true,
      stripe_meter_event_id: null,
      created_at: "2026-11-25T22:00:00Z",
    },
  ],
  total: 2,
  limit: 5,
  offset: 0,
  has_more: false,
};

const mockState = {
  health: { ...MOCK_OPS_HEALTH },
  wallet: { ...MOCK_WALLET },
  usage: { ...MOCK_USAGE, items: [...MOCK_USAGE.items] },
  refreshCount: 0,
};

/** Restore the mutable mock fixtures (used by `beforeEach` in the test suite). */
export function resetTrack8Mocks(): void {
  mockState.health = { ...MOCK_OPS_HEALTH };
  mockState.wallet = { ...MOCK_WALLET };
  mockState.usage = { ...MOCK_USAGE, items: MOCK_USAGE.items.map((item) => ({ ...item })) };
  mockState.refreshCount = 0;
}

export function mockOpsRefreshCount(): number {
  return mockState.refreshCount;
}

// ---------------------------------------------------------------------------
// Data access
// ---------------------------------------------------------------------------

export async function fetchOpsHealth(): Promise<OpsHealthRead> {
  if (shouldUseMockApi()) {
    mockState.refreshCount += 1;
    return { ...mockState.health, alerts: [...mockState.health.alerts] };
  }
  return normalizeOpsHealth(await bffFetch<unknown>("/v1/ops/health"));
}

export async function fetchBillingWallet(): Promise<BillingWalletRead> {
  if (shouldUseMockApi()) return { ...mockState.wallet };
  return normalizeWallet(await bffFetch<unknown>("/v1/billing/wallet"));
}

export async function fetchBillingUsage(limit = 5): Promise<BillingUsagePage> {
  if (shouldUseMockApi()) {
    const items = mockState.usage.items.slice(0, limit);
    return { ...mockState.usage, items, limit, total: mockState.usage.items.length };
  }
  return normalizeUsagePage(await bffFetch<unknown>(`/v1/billing/usage?limit=${limit}&offset=0`));
}

export async function fetchOpsSnapshot(): Promise<OpsBillingSnapshot> {
  const [health, wallet, usage] = await Promise.all([
    fetchOpsHealth(),
    fetchBillingWallet(),
    fetchBillingUsage(5),
  ]);
  return { health, wallet, usage };
}
