# Next.js Omnichannel Workbench UI & E2E Verification Implementation Plan

> **For agentic workers (Grok Bot / Superpowers):** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task using strict TDD (`test-driven-development`). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the complete `Next.js 15` (App Router, TypeScript, React 19) Omnichannel AI Workbench UI, thin Auth/SSE BFF shell, Hybrid B2B+B2C Factory self-serve onboarding wizard, and full-stack Playwright E2E verification suite consuming the `@hey-api/openapi-ts` client.

**Architecture:** `apps/web` operates strictly as a zero-SQL UI shell and thin Auth/SSE BFF proxy (`/api/bff/[...path]`) forwarding `HttpOnly` JWT cookies as `Authorization: Bearer` headers and streaming FastAPI SSE events (`EventSource`) to invalidate `TanStack Query v5` caches. All business payloads pass strict Zod + TypeScript contract guards aligned 1:1 with `contracts/openapi.json` to eliminate Nova-era UI bugs (`[object Object]`, raw internal monitoring keys, mismatched counters, and `L3` autonomy drift).

**Tech Stack:** `Node >=22`, `pnpm >=9`, `Next.js 15 (App Router)`, `React 19`, `TypeScript 5`, `TanStack Query v5`, `@hey-api/openapi-ts`, `@hey-api/client-fetch`, `Zod`, `Tailwind CSS v4`, `lucide-react`, `Vitest`, `@testing-library/react`, `ESLint`, `Playwright`.

## Global Constraints

1. **Runtime Versions & Package Managers**: Python `>=3.12` managed exclusively with `uv` (`apps/backend`); Node.js `>=22` managed exclusively with `pnpm >=9` (`apps/web`, `apps/widget`).
2. **Modular Monolith Boundaries**: All domain code lives under `apps/backend/src/erp_core/` in isolated modules (`shared`, `tenancy`, `channels`, `identity`, `crm`, `catalog`, `orders`, `finance`, `ai_engine`, `billing`). Cross-module database table imports are forbidden and enforced in CI via `import-linter` (`infra/importlinter.ini`).
3. **Zero Business SQL in Next.js**: `apps/web` is strictly a UI & thin Auth/SSE BFF shell. All business logic, SQL access, encryption, and LLM orchestration live in `apps/backend`.
4. **Zero Schema Drift**: FastAPI Pydantic v2 schemas are the single source of truth. Any schema change MUST regenerate `contracts/openapi.json` and `apps/web/src/lib/api-client/` via `@hey-api/openapi-ts`.
5. **Fail-Closed Multi-Tenant Isolation**: Every tenant-owned table MUST enable `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` and `ALTER TABLE ... FORCE ROW LEVEL SECURITY`, using `RESTRICTIVE` policy `tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid`. Runtime connections run ONLY as `app_runtime` (never `SUPERUSER`, never `BYPASSRLS`) behind PgBouncer `pool_mode = transaction`, using transaction-scoped `SELECT set_config('app.current_tenant_id', :tid, true)`.
6. **Envelope Encryption**: All third-party OAuth tokens, IMAP/SMTP secrets, and BYOK API keys MUST be encrypted with per-tenant 256-bit DEKs (wrapped by KMS/Master KEK) using AES-256-GCM bound to `AAD = f"{tenant_id}:{credential_id}:{credential_type}"`.
7. **Zero-Message-Loss Webhooks**: Webhook HTTP handlers verify raw-body HMAC signatures, persist into `raw_webhook_events` (`ON CONFLICT DO NOTHING`), enqueue to Redis Stream, and return `HTTP 200` in `<100ms` (never invoke LLMs or outbound HTTP inside webhook requests).
8. **Channel Compliance Guardrails**: WhatsApp outbound sends pass dual Redis Lua token buckets (`20 mps` Coexistence / `80 mps` Cloud + `1 msg/6s` pair limit, `4^X` exponential backoff on `131056`/`130429`), check `suppression_list` and `consent_records` before out-of-window templates, and enforce `bsuid` fallback. Marketplace outbound replies pass `OutboundComplianceFilter` blocking WhatsApp/email/`wa.me` off-platform leakage.
9. **17TRACK Strict Quota Protection**: `Tracking17TrackAdapter` MUST default to `cacheLevel=0` and reject `cacheLevel=1` in automated flows (preventing 10x quota burn).
10. **Financial Precision**: All monetary, FX rate, margin, and CVP calculations MUST use Python `decimal.Decimal` (`ROUND_HALF_UP`) and PostgreSQL `NUMERIC`, never IEEE-754 `float`.
11. **Zero-Hallucination AI Grounding**: Live prices, MOQs, stock levels, order statuses, and tracking events MUST come 100% from deterministic SQL tools (`ErpToolRegistry`) with `source_tool_call_id` verification—NEVER from static RAG chunks. `Strict Retrieval Mode` requires inline `[doc_id#chunk_id]` verification and forces abstention (`pending_human`) below Rerank score `0.62`.
12. **Strict TDD & Atomic Commits**: Every task follows Red → Green → Commit (`pytest` / `vitest` / `playwright`). Zero `TBD`, `TODO`, stub `pass` bodies, or skipped assertions.

---

### Task 5.1: Next.js 15 App Router Core Shell, Thin Auth/SSE BFF, i18n (`中/EN`) & RBAC Guards

**Files:**
- Create/Update: `apps/web/package.json`
- Create/Update: `apps/web/tsconfig.json`
- Create/Update: `apps/web/next.config.ts`
- Create/Update: `apps/web/openapi-ts.config.ts`
- Create: `apps/web/vitest.config.ts`
- Create: `apps/web/vitest.setup.ts`
- Create/Update: `apps/web/eslint.config.mjs`
- Create: `apps/web/src/app/globals.css`
- Create: `apps/web/src/app/layout.tsx`
- Create: `apps/web/src/app/page.tsx`
- Create: `apps/web/src/app/api/bff/[...path]/route.ts`
- Create: `apps/web/src/lib/contracts.ts`
- Create: `apps/web/src/lib/api.ts`
- Create: `apps/web/src/lib/auth-cookie.ts`
- Create: `apps/web/src/lib/i18n.ts`
- Create: `apps/web/src/components/providers.tsx`
- Create: `apps/web/src/hooks/use-realtime-inbox.ts`
- Create: `apps/web/src/components/app-shell.tsx`
- Create: `apps/web/src/app/(auth)/login/page.tsx`
- Test: `apps/web/src/__tests__/app-shell-rbac.test.tsx`

**Interfaces:**
- Consumes:
  - FastAPI endpoints via `/api/bff/v1/*`: `POST /api/v1/auth/login`, `POST /api/v1/auth/refresh`, `POST /api/v1/auth/logout`, `GET /api/v1/auth/me`, `GET /api/v1/inbox/events` (SSE)
  - Generated client in `apps/web/src/lib/api-client/` (`client.gen.ts` from `@hey-api/openapi-ts`)
- Produces:
  - `bffClient` (`apps/web/src/lib/api.ts`) configuring `@hey-api/openapi-ts` client with `baseUrl: "/api/bff"`
  - `canAccessRoute(role, pathname)`, `isPrivilegedCostViewer(role)`, `canVerifyFinanceSettlement(role)` (`apps/web/src/lib/auth-cookie.ts`)
  - `useI18n()` (`apps/web/src/lib/i18n.ts`)
  - `useAuthSession()` (`apps/web/src/components/providers.tsx`)
  - `useRealtimeInbox()` (`apps/web/src/hooks/use-realtime-inbox.ts`)
  - `AppShell` (`apps/web/src/components/app-shell.tsx`)
  - Canonical TypeScript enums, Zod schemas, `formatMoney`, `computeBuyerLocalTimeInfo` (`apps/web/src/lib/contracts.ts`)

> **Pre-flight Contract Sync:** Before running Task 5.1 tests, ensure `contracts/openapi.json` is up to date from `apps/backend` and run `pnpm --filter web openapi-ts` so `apps/web/src/lib/api-client/` matches the backend OpenAPI 3.1 spec.

- [ ] **Step 1: Write the failing test (`apps/web/src/__tests__/app-shell-rbac.test.tsx`)**

```tsx
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm --filter web test -- src/__tests__/app-shell-rbac.test.tsx`
Expected: FAIL (`Cannot find module '@/components/app-shell'` or missing Vitest config).

- [ ] **Step 3: Write complete implementation**

Create/update `apps/web/package.json`:

```json
{
  "name": "web",
  "version": "0.1.0",
  "private": true,
  "scripts": {
    "dev": "next dev --port 3000",
    "build": "next build",
    "start": "next start --port 3000",
    "lint": "eslint src",
    "test": "vitest run",
    "openapi-ts": "openapi-ts"
  },
  "dependencies": {
    "@hey-api/client-fetch": "^0.8.0",
    "@tanstack/react-query": "^5.66.0",
    "clsx": "^2.1.1",
    "lucide-react": "^0.475.0",
    "next": "^15.1.7",
    "react": "^19.0.0",
    "react-dom": "^19.0.0",
    "tailwind-merge": "^3.0.1",
    "zod": "^3.24.2"
  },
  "devDependencies": {
    "@hey-api/openapi-ts": "^0.65.0",
    "@tailwindcss/postcss": "^4.0.0",
    "@testing-library/jest-dom": "^6.6.3",
    "@testing-library/react": "^16.2.0",
    "@testing-library/user-event": "^14.6.1",
    "@types/node": "^22.13.0",
    "@types/react": "^19.0.8",
    "@types/react-dom": "^19.0.3",
    "@vitejs/plugin-react": "^4.3.4",
    "eslint": "^9.20.0",
    "eslint-config-next": "^15.1.7",
    "jsdom": "^26.0.0",
    "tailwindcss": "^4.0.0",
    "typescript": "^5.7.3",
    "typescript-eslint": "^8.24.0",
    "vitest": "^3.0.6"
  }
}
```

Create/update `apps/web/tsconfig.json`:

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "lib": ["dom", "dom.iterable", "esnext"],
    "allowJs": true,
    "skipLibCheck": true,
    "strict": true,
    "noEmit": true,
    "esModuleInterop": true,
    "module": "esnext",
    "moduleResolution": "bundler",
    "resolveJsonModule": true,
    "isolatedModules": true,
    "jsx": "preserve",
    "incremental": true,
    "plugins": [{ "name": "next" }],
    "paths": {
      "@/*": ["./src/*"]
    }
  },
  "include": ["next-env.d.ts", "**/*.ts", "**/*.tsx", ".next/types/**/*.ts"],
  "exclude": ["node_modules"]
}
```

Create/update `apps/web/next.config.ts`:

```ts
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
};

export default nextConfig;
```

Create/update `apps/web/openapi-ts.config.ts`:

```ts
import { defineConfig } from "@hey-api/openapi-ts";

export default defineConfig({
  input: "../../contracts/openapi.json",
  output: {
    path: "src/lib/api-client",
    format: "prettier",
  },
  plugins: [
    "@hey-api/typescript",
    "@hey-api/client-fetch",
    {
      name: "@hey-api/sdk",
      asClass: false,
    },
  ],
});
```

Create `apps/web/vitest.config.ts`:

```ts
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "node:path";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
    include: ["src/__tests__/**/*.test.{ts,tsx}"],
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
});
```

Create `apps/web/vitest.setup.ts`:

```ts
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

afterEach(() => {
  cleanup();
});

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    push: vi.fn(),
    replace: vi.fn(),
    refresh: vi.fn(),
    back: vi.fn(),
  }),
  usePathname: () => "/inbox",
  useSearchParams: () => new URLSearchParams(),
}));
```

Create/update `apps/web/eslint.config.mjs`:

```js
import tseslint from "typescript-eslint";

const FORBIDDEN_DB_MODULES = [
  "pg",
  "pg-pool",
  "postgres",
  "asyncpg",
  "drizzle-orm",
  "prisma",
  "@prisma/client",
  "kysely",
  "better-sqlite3",
  "sqlite3",
  "mysql2",
  "typeorm",
  "sequelize",
  "knex",
  "mongoose",
  "ioredis",
  "redis",
  "sqlalchemy",
];

export default tseslint.config(
  {
    ignores: [".next/**", "node_modules/**", "src/lib/api-client/**"],
  },
  ...tseslint.configs.recommended,
  {
    files: ["src/**/*.{ts,tsx}"],
    rules: {
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
      "@typescript-eslint/no-explicit-any": "warn",
      "no-restricted-imports": [
        "error",
        {
          paths: FORBIDDEN_DB_MODULES.map((name) => ({
            name,
            message:
              "Constraint #3 violation: apps/web must remain a zero-SQL thin UI/BFF shell. Call FastAPI via /api/bff/v1/* instead.",
          })),
          patterns: [
            {
              group: ["**/erp_core/**", "drizzle-orm/*", "@prisma/*"],
              message: "Constraint #3 violation: direct backend/ORM imports are forbidden in apps/web.",
            },
          ],
        },
      ],
    },
  }
);
```

Create `apps/web/src/app/globals.css`:

```css
@import "tailwindcss";

:root {
  --bg-canvas: #f8fafc;
  --fg-primary: #0f172a;
}

body {
  background-color: var(--bg-canvas);
  color: var(--fg-primary);
  font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
```

Create `apps/web/src/lib/contracts.ts`:

```ts
import { z } from "zod";

export const UserRoleSchema = z.enum(["owner", "admin", "manager", "agent", "finance"]);
export type UserRole = z.infer<typeof UserRoleSchema>;

export const OnboardingStageSchema = z.enum([
  "WorkspaceCreated",
  "ChannelConnecting",
  "ChannelVerified",
  "CatalogSyncing",
  "AISandboxReady",
  "LiveProduction",
]);
export type OnboardingStage = z.infer<typeof OnboardingStageSchema>;

export const ChannelTypeSchema = z.enum([
  "whatsapp_cloud",
  "whatsapp_coexistence",
  "email",
  "web_livechat",
  "shopify",
  "woocommerce",
  "shopline",
  "shoplazza",
  "tiktok_shop",
  "made_in_china_email",
  "alibaba_email",
  "messenger",
  "instagram_dm",
  "telegram",
  "amazon_sp",
  "ebay",
  "mercado_libre",
  "temu",
  "walmart",
  "shopee",
  "lazada",
]);
export type ChannelType = z.infer<typeof ChannelTypeSchema>;

export const ChannelStatusSchema = z.enum(["active", "token_expired", "rate_limited", "disconnected"]);
export type ChannelStatus = z.infer<typeof ChannelStatusSchema>;

export const ConversationModeSchema = z.enum(["b2c_support", "b2b_inquiry", "mixed"]);
export type ConversationMode = z.infer<typeof ConversationModeSchema>;

export const ConversationStatusSchema = z.enum(["open", "pending_human", "snoozed", "resolved"]);
export type ConversationStatus = z.infer<typeof ConversationStatusSchema>;

export const MessageDirectionSchema = z.enum(["inbound", "outbound", "internal_note"]);
export type MessageDirection = z.infer<typeof MessageDirectionSchema>;

export const MessageSenderTypeSchema = z.enum(["customer", "human_agent", "ai_agent", "system"]);
export type MessageSenderType = z.infer<typeof MessageSenderTypeSchema>;

export const MessageContentTypeSchema = z.enum([
  "text",
  "rich_html",
  "template",
  "media",
  "quote_card",
  "order_card",
]);
export type MessageContentType = z.infer<typeof MessageContentTypeSchema>;

export const DeliveryStatusSchema = z.enum(["queued", "sent", "delivered", "read", "failed"]);
export type DeliveryStatus = z.infer<typeof DeliveryStatusSchema>;

export const IdentityTypeSchema = z.enum([
  "email",
  "phone_e164",
  "whatsapp_waid",
  "whatsapp_bsuid",
  "ig_psid",
  "fb_psid",
  "shopify_customer_id",
  "tiktok_buyer_id",
  "amazon_buyer_email",
  "web_visitor_id",
]);
export type IdentityType = z.infer<typeof IdentityTypeSchema>;

export const ConfidenceLevelSchema = z.enum(["deterministic", "human_verified", "probabilistic"]);
export type ConfidenceLevel = z.infer<typeof ConfidenceLevelSchema>;

export const CustomerTypeSchema = z.enum(["b2c_consumer", "b2b_contact", "b2b_company_rep"]);
export type CustomerType = z.infer<typeof CustomerTypeSchema>;

export const OpportunityStageSchema = z.enum([
  "discovery",
  "sampling",
  "quoting",
  "negotiating",
  "closed_won",
  "closed_lost",
]);
export type OpportunityStage = z.infer<typeof OpportunityStageSchema>;

export const IncotermSchema = z.enum(["EXW", "FOB", "CIF", "DDP", "DAP"]);
export type Incoterm = z.infer<typeof IncotermSchema>;

export const QuoteStatusSchema = z.enum([
  "draft",
  "pending_approval",
  "sent",
  "accepted",
  "expired",
  "converted",
]);
export type QuoteStatus = z.infer<typeof QuoteStatusSchema>;

export const OrderModeSchema = z.enum(["b2c_retail", "b2b_sample", "b2b_wholesale"]);
export type OrderMode = z.infer<typeof OrderModeSchema>;

export const FulfillmentStatusSchema = z.enum([
  "pending_payment",
  "confirmed_reserved",
  "in_production",
  "partially_shipped",
  "shipped",
  "delivered",
  "exception",
  "cancelled",
  "refunded",
]);
export type FulfillmentStatus = z.infer<typeof FulfillmentStatusSchema>;

export const PaymentStatusSchema = z.enum([
  "unpaid",
  "deposit_paid",
  "partially_paid",
  "paid",
  "refunded",
]);
export type PaymentStatus = z.infer<typeof PaymentStatusSchema>;

export const InventoryEventTypeSchema = z.enum([
  "inbound_po",
  "reserve_order",
  "release_cancel",
  "deduct_shipped",
  "stocktake_adjust",
]);
export type InventoryEventType = z.infer<typeof InventoryEventTypeSchema>;

export const ProposalTypeSchema = z.enum([
  "reply_draft",
  "crm_entity_extract",
  "quote_draft",
  "follow_up_suggestion",
  "identity_merge",
  "refund_or_reship",
  "payment_match",
]);
export type ProposalType = z.infer<typeof ProposalTypeSchema>;

export const ProposalStatusSchema = z.enum([
  "pending",
  "approved",
  "edited_and_approved",
  "rejected",
  "expired",
  "failed",
]);
export type ProposalStatus = z.infer<typeof ProposalStatusSchema>;

export const AutonomyLevelSchema = z.enum(["L0_copilot", "L1_proposal", "L2_auto"]);
export type AutonomyLevel = z.infer<typeof AutonomyLevelSchema>;

export const AiCapabilitySchema = z.enum([
  "cs_support",
  "sales_quoting",
  "order_logistics",
  "content_studio",
  "biz_analytics",
  "finance_recon",
]);
export type AiCapability = z.infer<typeof AiCapabilitySchema>;

export const PrincipalSchema = z.object({
  user_id: z.string(),
  tenant_id: z.string(),
  role: UserRoleSchema,
  email: z.string().email(),
  tenant_slug: z.string().optional(),
  tenant_name: z.string().optional(),
  full_name: z.string().optional(),
  can_view_cost: z.boolean().optional(),
  onboarding_stage: OnboardingStageSchema.optional(),
});
export type Principal = z.infer<typeof PrincipalSchema>;

export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
}

export interface ProblemDetail {
  type: string;
  title: string;
  status: number;
  detail: string;
  instance?: string;
  code?: string;
}

export class BffApiError extends Error {
  status: number;
  code: string;
  detail: string;

  constructor(problem: ProblemDetail) {
    super(problem.detail || problem.title);
    this.name = "BffApiError";
    this.status = problem.status;
    this.code = problem.code ?? "ERR_BFF_UPSTREAM";
    this.detail = problem.detail;
  }
}

export function formatMoney(
  rawValue: unknown,
  currency: string = "USD",
  locale: string = "en-US"
): string {
  if (rawValue === null || rawValue === undefined || typeof rawValue === "object") {
    return `${currency} 0.00`;
  }
  const num = typeof rawValue === "number" ? rawValue : Number(rawValue);
  if (!Number.isFinite(num)) {
    return `${currency} 0.00`;
  }
  try {
    return new Intl.NumberFormat(locale, {
      style: "currency",
      currency,
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }).format(num);
  } catch {
    return `${currency} ${num.toFixed(2)}`;
  }
}

export interface BuyerLocalTimeDisplay {
  formattedTime: string;
  localHour24: number;
  isQuietHours: boolean;
  ianaTimezone: string;
}

export function computeBuyerLocalTimeInfo(
  ianaTimezone: string | null | undefined,
  now: Date = new Date()
): BuyerLocalTimeDisplay | null {
  if (!ianaTimezone) return null;
  try {
    const hourFmt = new Intl.DateTimeFormat("en-US", {
      timeZone: ianaTimezone,
      hour: "numeric",
      hour12: false,
    });
    const parsedHour = Number(hourFmt.format(now));
    const localHour24 = parsedHour === 24 ? 0 : parsedHour;

    const displayFmt = new Intl.DateTimeFormat("en-US", {
      timeZone: ianaTimezone,
      hour: "2-digit",
      minute: "2-digit",
      hour12: true,
    });

    return {
      formattedTime: displayFmt.format(now),
      localHour24,
      isQuietHours: localHour24 >= 21 || localHour24 < 8,
      ianaTimezone,
    };
  } catch {
    return null;
  }
}
```

Create `apps/web/src/lib/api.ts`:

```ts
import { client } from "@/lib/api-client/client.gen";
import type { ProblemDetail } from "@/lib/contracts";
import { BffApiError } from "@/lib/contracts";

let configured = false;

export function getBffClient() {
  if (!configured) {
    client.setConfig({
      baseUrl: "/api/bff",
      credentials: "include",
    });
    configured = true;
  }
  return client;
}

export async function bffFetch<T>(
  path: string,
  options: RequestInit & { parseJson?: boolean } = {}
): Promise<T> {
  getBffClient();
  const normalizedPath = path.startsWith("/api/bff")
    ? path
    : `/api/bff${path.startsWith("/") ? path : `/${path}`}`;

  const headers = new Headers(options.headers);
  if (!(options.body instanceof FormData) && !headers.has("Content-Type") && options.body) {
    headers.set("Content-Type", "application/json");
  }

  const res = await fetch(normalizedPath, {
    ...options,
    headers,
    credentials: "include",
  });

  if (!res.ok) {
    let problem: ProblemDetail = {
      type: "https://erp.local/errors/http-error",
      title: res.statusText || "Request failed",
      status: res.status,
      detail: `Request failed with status ${res.status}`,
      code: res.status === 403 ? "ERR_INSUFFICIENT_ROLE" : "ERR_API_FAILURE",
    };
    try {
      const parsed = (await res.json()) as Partial<ProblemDetail>;
      problem = { ...problem, ...parsed, status: res.status };
    } catch {
      // keep fallback problem detail
    }
    throw new BffApiError(problem);
  }

  if (options.parseJson === false || res.status === 204) {
    return undefined as T;
  }
  return (await res.json()) as T;
}
```

Create `apps/web/src/lib/auth-cookie.ts`:

```ts
import type { UserRole } from "@/lib/contracts";

export const ACCESS_COOKIE_NAME = "erp_access_token";
export const REFRESH_COOKIE_NAME = "erp_refresh_token";

export interface CookieOptions {
  httpOnly: boolean;
  sameSite: "lax";
  path: string;
  secure: boolean;
  maxAge: number;
}

export function buildAuthCookieOptions(maxAgeSeconds: number): CookieOptions {
  return {
    httpOnly: true,
    sameSite: "lax",
    path: "/",
    secure: process.env.NODE_ENV === "production",
    maxAge: maxAgeSeconds,
  };
}

export const ROUTE_ROLE_ALLOWLIST: Array<{ prefix: string; roles: UserRole[] }> = [
  { prefix: "/finance", roles: ["owner", "admin", "manager", "finance"] },
  { prefix: "/settings", roles: ["owner", "admin"] },
  { prefix: "/onboarding", roles: ["owner", "admin"] },
  { prefix: "/ai/capabilities", roles: ["owner", "admin", "manager", "agent", "finance"] },
  { prefix: "/ai/proposals", roles: ["owner", "admin", "manager", "agent", "finance"] },
  { prefix: "/b2b", roles: ["owner", "admin", "manager", "agent"] },
  { prefix: "/catalog", roles: ["owner", "admin", "manager", "agent", "finance"] },
  { prefix: "/orders", roles: ["owner", "admin", "manager", "agent", "finance"] },
  { prefix: "/inbox", roles: ["owner", "admin", "manager", "agent"] },
];

export function canAccessRoute(role: UserRole | undefined | null, pathname: string): boolean {
  if (!role) return false;
  const matched = ROUTE_ROLE_ALLOWLIST.find((entry) => pathname.startsWith(entry.prefix));
  if (!matched) return true;
  return matched.roles.includes(role);
}

export function isPrivilegedCostViewer(role: UserRole | undefined | null): boolean {
  return role === "owner" || role === "admin" || role === "manager" || role === "finance";
}

export function canVerifyFinanceSettlement(role: UserRole | undefined | null): boolean {
  return role === "owner" || role === "finance";
}

export function canConfigureAiGovernance(role: UserRole | undefined | null): boolean {
  return role === "owner" || role === "admin" || role === "manager";
}
```

Create `apps/web/src/lib/i18n.ts`:

```tsx
"use client";

import React, { createContext, useContext, useMemo, useState } from "react";

export type Locale = "zh" | "en";

const DICTIONARY = {
  zh: {
    brandTitle: "工贸全渠道工作台",
    navInbox: "聚合沟通 Inbox",
    navProposals: "AI 提案审核",
    navQuotes: "B2B CRM 与报价",
    navCatalog: "商品与 SKU 映射",
    navOrders: "订单与 17TRACK",
    navFinance: "财务与 CVP 毛利",
    navSettings: "渠道与合规设置",
    navCapabilities: "AI 治理与知识库",
    navOnboarding: "工厂入驻向导",
    logout: "退出登录",
    roleLabel: "当前角色",
    accessDeniedTitle: "权限不足 (ERR_INSUFFICIENT_ROLE)",
    accessDeniedDesc: "当前角色无权访问该财务或系统敏感页面，请联系 Owner 或 Admin。",
    quietHours: "休息时段",
    localTime: "当地时间",
    strictRag: "仅检索模式",
    globalMode: "全局引导模式",
    send: "发送回复",
    internalNote: "内部备注",
    liveTranslate: "实时翻译",
    aiDraft: "AI 起草",
    aiPolish: "AI 润色",
    aiSummarize: "会话摘要",
    showTranslation: "显示译文",
    hideTranslation: "收起译文",
    pendingApprovals: "待审核提案",
  },
  en: {
    brandTitle: "Hybrid Trade Workbench",
    navInbox: "Inbox",
    navProposals: "AI Proposals",
    navQuotes: "B2B CRM & Quotes",
    navCatalog: "Catalog",
    navOrders: "Orders & Tracking",
    navFinance: "Finance & CVP",
    navSettings: "Channels & Settings",
    navCapabilities: "AI Governance & KB",
    navOnboarding: "Onboarding Wizard",
    logout: "Sign out",
    roleLabel: "Role",
    accessDeniedTitle: "Access Denied (ERR_INSUFFICIENT_ROLE)",
    accessDeniedDesc: "Your role does not have permission to view this financial or administrative workspace.",
    quietHours: "Quiet Hours",
    localTime: "Local Time",
    strictRag: "Strict RAG",
    globalMode: "Global Guided",
    send: "Send",
    internalNote: "Internal Note",
    liveTranslate: "Live Translate",
    aiDraft: "AI Draft",
    aiPolish: "Polish",
    aiSummarize: "Summarize",
    showTranslation: "Show translation",
    hideTranslation: "Hide translation",
    pendingApprovals: "Pending Approvals",
  },
} as const;

export type I18nKey = keyof (typeof DICTIONARY)["en"];

export interface I18nContextValue {
  locale: Locale;
  setLocale: (loc: Locale) => void;
  toggleLocale: () => void;
  t: (key: I18nKey) => string;
}

const I18nContext = createContext<I18nContextValue>({
  locale: "en",
  setLocale: () => {},
  toggleLocale: () => {},
  t: (key) => DICTIONARY.en[key],
});

export function I18nProvider({
  children,
  defaultLocale = "zh",
}: {
  children: React.ReactNode;
  defaultLocale?: Locale;
}) {
  const [locale, setLocale] = useState<Locale>(defaultLocale);

  const value = useMemo<I18nContextValue>(
    () => ({
      locale,
      setLocale,
      toggleLocale: () => setLocale((prev) => (prev === "zh" ? "en" : "zh")),
      t: (key: I18nKey) => DICTIONARY[locale][key],
    }),
    [locale]
  );

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n() {
  return useContext(I18nContext);
}
```

Create `apps/web/src/app/api/bff/[...path]/route.ts`:

```ts
import { NextRequest, NextResponse } from "next/server";
import {
  ACCESS_COOKIE_NAME,
  REFRESH_COOKIE_NAME,
  buildAuthCookieOptions,
} from "@/lib/auth-cookie";

const BACKEND_BASE = (process.env.BFF_BACKEND_URL ?? "http://localhost:8000").replace(/\/$/, "");

function resolveUpstreamPath(segments: string[]): string | null {
  if (!segments || segments.length === 0) return null;
  for (const seg of segments) {
    if (!seg || seg === "." || seg === ".." || seg.includes("..") || seg.includes("://")) {
      return null;
    }
  }
  const joined = segments.join("/");
  if (joined === "healthz" || joined === "readyz") {
    return `/${joined}`;
  }
  if (joined.startsWith("api/")) {
    return `/${joined}`;
  }
  if (joined.startsWith("v1/")) {
    return `/api/${joined}`;
  }
  return `/api/v1/${joined}`;
}

function isSseRequest(request: NextRequest, upstreamPath: string): boolean {
  const accept = request.headers.get("accept") ?? "";
  return accept.includes("text/event-stream") || upstreamPath.endsWith("/events");
}

async function tryRefreshToken(refreshToken: string): Promise<{
  access_token: string;
  refresh_token?: string;
  expires_in?: number;
} | null> {
  const res = await fetch(`${BACKEND_BASE}/api/v1/auth/refresh`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: refreshToken }),
  });
  if (!res.ok) return null;
  return (await res.json()) as {
    access_token: string;
    refresh_token?: string;
    expires_in?: number;
  };
}

async function handleProxy(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> }
): Promise<Response> {
  const { path } = await context.params;
  const upstreamPath = resolveUpstreamPath(path);
  if (!upstreamPath) {
    return NextResponse.json(
      {
        type: "https://erp.local/errors/invalid-bff-path",
        title: "Invalid BFF Path",
        status: 400,
        detail: "Path segment rejected by BFF security filter.",
        code: "ERR_INVALID_BFF_PATH",
      },
      { status: 400 }
    );
  }

  const targetUrl = `${BACKEND_BASE}${upstreamPath}${request.nextUrl.search}`;

  if (upstreamPath === "/api/v1/auth/login" && request.method === "POST") {
    const bodyText = await request.text();
    const loginRes = await fetch(targetUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: bodyText,
    });
    const payload = await loginRes.json();
    if (!loginRes.ok) {
      return NextResponse.json(payload, { status: loginRes.status });
    }
    const { access_token, refresh_token, expires_in, principal, user } = payload as {
      access_token: string;
      refresh_token: string;
      expires_in?: number;
      principal?: unknown;
      user?: unknown;
    };
    const resolvedPrincipal = principal ?? user;
    const response = NextResponse.json({ principal: resolvedPrincipal }, { status: 200 });
    response.cookies.set(
      ACCESS_COOKIE_NAME,
      access_token,
      buildAuthCookieOptions(expires_in ?? 1800)
    );
    if (refresh_token) {
      response.cookies.set(
        REFRESH_COOKIE_NAME,
        refresh_token,
        buildAuthCookieOptions(60 * 60 * 24 * 14)
      );
    }
    return response;
  }

  if (upstreamPath === "/api/v1/auth/logout" && request.method === "POST") {
    const accessToken = request.cookies.get(ACCESS_COOKIE_NAME)?.value;
    if (accessToken) {
      await fetch(targetUrl, {
        method: "POST",
        headers: { Authorization: `Bearer ${accessToken}` },
      }).catch(() => {});
    }
    const response = NextResponse.json({ ok: true }, { status: 200 });
    response.cookies.set(ACCESS_COOKIE_NAME, "", buildAuthCookieOptions(0));
    response.cookies.set(REFRESH_COOKIE_NAME, "", buildAuthCookieOptions(0));
    return response;
  }

  const accessToken = request.cookies.get(ACCESS_COOKIE_NAME)?.value;
  const refreshToken = request.cookies.get(REFRESH_COOKIE_NAME)?.value;

  const forwardHeaders = new Headers();
  const contentType = request.headers.get("content-type");
  if (contentType) {
    forwardHeaders.set("content-type", contentType);
  }
  const accept = request.headers.get("accept");
  if (accept) {
    forwardHeaders.set("accept", accept);
  }
  if (accessToken) {
    forwardHeaders.set("authorization", `Bearer ${accessToken}`);
  }

  const hasBody = !["GET", "HEAD"].includes(request.method);
  const rawBody = hasBody ? await request.arrayBuffer() : undefined;

  let upstreamRes = await fetch(targetUrl, {
    method: request.method,
    headers: forwardHeaders,
    body: rawBody,
  });

  let refreshedTokens: {
    access_token: string;
    refresh_token?: string;
    expires_in?: number;
  } | null = null;

  if (upstreamRes.status === 401 && refreshToken && !isSseRequest(request, upstreamPath)) {
    refreshedTokens = await tryRefreshToken(refreshToken);
    if (refreshedTokens) {
      forwardHeaders.set("authorization", `Bearer ${refreshedTokens.access_token}`);
      upstreamRes = await fetch(targetUrl, {
        method: request.method,
        headers: forwardHeaders,
        body: rawBody,
      });
    }
  }

  if (isSseRequest(request, upstreamPath)) {
    return new Response(upstreamRes.body, {
      status: upstreamRes.status,
      headers: {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache, no-transform",
        Connection: "keep-alive",
        "X-Accel-Buffering": "no",
      },
    });
  }

  const respHeaders = new Headers();
  const upstreamCt = upstreamRes.headers.get("content-type");
  if (upstreamCt) {
    respHeaders.set("content-type", upstreamCt);
  }
  const contentDisposition = upstreamRes.headers.get("content-disposition");
  if (contentDisposition) {
    respHeaders.set("content-disposition", contentDisposition);
  }

  const response = new NextResponse(upstreamRes.body, {
    status: upstreamRes.status,
    headers: respHeaders,
  });

  if (refreshedTokens) {
    response.cookies.set(
      ACCESS_COOKIE_NAME,
      refreshedTokens.access_token,
      buildAuthCookieOptions(refreshedTokens.expires_in ?? 1800)
    );
    if (refreshedTokens.refresh_token) {
      response.cookies.set(
        REFRESH_COOKIE_NAME,
        refreshedTokens.refresh_token,
        buildAuthCookieOptions(60 * 60 * 24 * 14)
      );
    }
  }

  return response;
}

export async function GET(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  return handleProxy(req, ctx);
}
export async function POST(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  return handleProxy(req, ctx);
}
export async function PUT(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  return handleProxy(req, ctx);
}
export async function PATCH(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  return handleProxy(req, ctx);
}
export async function DELETE(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  return handleProxy(req, ctx);
}
```

Create `apps/web/src/components/providers.tsx`:

```tsx
"use client";

import React, { createContext, useCallback, useContext, useState } from "react";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { bffFetch } from "@/lib/api";
import { PrincipalSchema, type Principal } from "@/lib/contracts";
import { I18nProvider } from "@/lib/i18n";

export interface AuthSessionState {
  principal: Principal | null;
  isLoading: boolean;
  logout: () => Promise<void>;
  refreshSession: () => Promise<void>;
}

export const AuthSessionContext = createContext<AuthSessionState>({
  principal: null,
  isLoading: true,
  logout: async () => {},
  refreshSession: async () => {},
});

function AuthSessionBridge({ children }: { children: React.ReactNode }) {
  const { data, isLoading, refetch } = useQuery({
    queryKey: ["auth", "me"],
    queryFn: async () => {
      try {
        const raw = await bffFetch<unknown>("/v1/auth/me");
        const parsed = PrincipalSchema.safeParse(raw);
        return parsed.success ? parsed.data : null;
      } catch {
        return null;
      }
    },
    retry: false,
    staleTime: 60_000,
  });

  const logout = useCallback(async () => {
    await bffFetch("/v1/auth/logout", { method: "POST" });
    await refetch();
    if (typeof window !== "undefined") {
      window.location.href = "/login";
    }
  }, [refetch]);

  const refreshSession = useCallback(async () => {
    await refetch();
  }, [refetch]);

  return (
    <AuthSessionContext.Provider
      value={{
        principal: data ?? null,
        isLoading,
        logout,
        refreshSession,
      }}
    >
      {children}
    </AuthSessionContext.Provider>
  );
}

export function Providers({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 10_000,
            refetchOnWindowFocus: false,
          },
        },
      })
  );

  return (
    <QueryClientProvider client={queryClient}>
      <I18nProvider defaultLocale="zh">
        <AuthSessionBridge>{children}</AuthSessionBridge>
      </I18nProvider>
    </QueryClientProvider>
  );
}

export function useAuthSession() {
  return useContext(AuthSessionContext);
}
```

Create `apps/web/src/hooks/use-realtime-inbox.ts`:

```ts
"use client";

import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";

interface SsePayload {
  conversation_id?: string;
  order_id?: string;
  proposal_id?: string;
}

function safeParseJson(raw: string): SsePayload {
  try {
    return JSON.parse(raw) as SsePayload;
  } catch {
    return {};
  }
}

export function useRealtimeInbox(enabled: boolean = true) {
  const queryClient = useQueryClient();

  useEffect(() => {
    if (!enabled || typeof EventSource === "undefined") {
      return;
    }

    const source = new EventSource("/api/bff/v1/inbox/events", {
      withCredentials: true,
    });

    const onMessageCreated = (ev: MessageEvent) => {
      const payload = safeParseJson(ev.data);
      void queryClient.invalidateQueries({ queryKey: ["inbox", "conversations"] });
      if (payload.conversation_id) {
        void queryClient.invalidateQueries({
          queryKey: ["inbox", "messages", payload.conversation_id],
        });
      }
    };

    const onConversationUpdated = (ev: MessageEvent) => {
      const payload = safeParseJson(ev.data);
      void queryClient.invalidateQueries({ queryKey: ["inbox", "conversations"] });
      if (payload.conversation_id) {
        void queryClient.invalidateQueries({
          queryKey: ["inbox", "conversation", payload.conversation_id],
        });
      }
    };

    const onProposalEvent = () => {
      void queryClient.invalidateQueries({ queryKey: ["ai", "proposals"] });
      void queryClient.invalidateQueries({ queryKey: ["ai", "overview"] });
    };

    const onShipmentUpdated = (ev: MessageEvent) => {
      const payload = safeParseJson(ev.data);
      void queryClient.invalidateQueries({ queryKey: ["orders"] });
      if (payload.order_id) {
        void queryClient.invalidateQueries({ queryKey: ["orders", payload.order_id] });
      }
    };

    const onWalletUpdated = () => {
      void queryClient.invalidateQueries({ queryKey: ["ai", "overview"] });
      void queryClient.invalidateQueries({ queryKey: ["ai", "wallet"] });
    };

    const onDlqUpdated = () => {
      void queryClient.invalidateQueries({ queryKey: ["channels", "dlq"] });
    };

    source.addEventListener("message.created", onMessageCreated as EventListener);
    source.addEventListener("conversation.updated", onConversationUpdated as EventListener);
    source.addEventListener("proposal.created", onProposalEvent as EventListener);
    source.addEventListener("proposal.resolved", onProposalEvent as EventListener);
    source.addEventListener("shipment.updated", onShipmentUpdated as EventListener);
    source.addEventListener("wallet.updated", onWalletUpdated as EventListener);
    source.addEventListener("dlq.updated", onDlqUpdated as EventListener);

    return () => {
      source.close();
    };
  }, [enabled, queryClient]);
}
```

Create `apps/web/src/components/app-shell.tsx`:

```tsx
"use client";

import React, { useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Inbox,
  Sparkles,
  Briefcase,
  Package,
  Truck,
  Landmark,
  Settings,
  ShieldCheck,
  Wand2,
  PanelLeftClose,
  PanelLeftOpen,
  Languages,
  LogOut,
  ShieldAlert,
} from "lucide-react";
import { useAuthSession } from "@/components/providers";
import { useRealtimeInbox } from "@/hooks/use-realtime-inbox";
import { canAccessRoute } from "@/lib/auth-cookie";
import { useI18n, type I18nKey } from "@/lib/i18n";
import type { UserRole } from "@/lib/contracts";

interface NavItem {
  href: string;
  labelKey: I18nKey;
  icon: React.ComponentType<{ className?: string }>;
  roles: UserRole[];
  badgeKey?: "proposals";
}

const PRIMARY_NAV_ITEMS: NavItem[] = [
  { href: "/inbox", labelKey: "navInbox", icon: Inbox, roles: ["owner", "admin", "manager", "agent"] },
  {
    href: "/ai/proposals",
    labelKey: "navProposals",
    icon: Sparkles,
    roles: ["owner", "admin", "manager", "agent", "finance"],
    badgeKey: "proposals",
  },
  {
    href: "/b2b/quotes",
    labelKey: "navQuotes",
    icon: Briefcase,
    roles: ["owner", "admin", "manager", "agent"],
  },
  {
    href: "/catalog",
    labelKey: "navCatalog",
    icon: Package,
    roles: ["owner", "admin", "manager", "agent", "finance"],
  },
  {
    href: "/orders",
    labelKey: "navOrders",
    icon: Truck,
    roles: ["owner", "admin", "manager", "agent", "finance"],
  },
  {
    href: "/finance",
    labelKey: "navFinance",
    icon: Landmark,
    roles: ["owner", "admin", "manager", "finance"],
  },
  {
    href: "/settings/channels",
    labelKey: "navSettings",
    icon: Settings,
    roles: ["owner", "admin"],
  },
];

const SECONDARY_NAV_ITEMS: NavItem[] = [
  {
    href: "/ai/capabilities",
    labelKey: "navCapabilities",
    icon: ShieldCheck,
    roles: ["owner", "admin", "manager", "agent", "finance"],
  },
  {
    href: "/onboarding",
    labelKey: "navOnboarding",
    icon: Wand2,
    roles: ["owner", "admin"],
  },
];

export function AppShell({
  children,
  currentPath,
  pendingProposalsCount = 0,
}: {
  children: React.ReactNode;
  currentPath?: string;
  pendingProposalsCount?: number;
}) {
  const pathname = currentPath ?? usePathname();
  const { principal, isLoading, logout } = useAuthSession();
  const { locale, toggleLocale, t } = useI18n();
  const [collapsed, setCollapsed] = useState(false);

  useRealtimeInbox(Boolean(principal));

  const role = principal?.role ?? null;
  const visiblePrimary = PRIMARY_NAV_ITEMS.filter((item) => !role || item.roles.includes(role));
  const visibleSecondary = SECONDARY_NAV_ITEMS.filter((item) => !role || item.roles.includes(role));
  const allowedCurrentRoute = !role || canAccessRoute(role, pathname);

  return (
    <div className="flex min-h-screen bg-slate-50 text-slate-900">
      <aside
        data-testid="sidebar-nav"
        data-collapsed={collapsed ? "true" : "false"}
        className={`flex flex-col border-r border-slate-200 bg-white transition-all ${
          collapsed ? "w-16" : "w-60"
        }`}
      >
        <div className="flex h-14 items-center justify-between border-b border-slate-200 px-3">
          {!collapsed && <span className="truncate text-sm font-semibold">{t("brandTitle")}</span>}
          <button
            type="button"
            data-testid="sidebar-collapse-btn"
            onClick={() => setCollapsed((prev) => !prev)}
            className="rounded p-1.5 text-slate-500 hover:bg-slate-100"
            aria-label="Toggle sidebar"
          >
            {collapsed ? <PanelLeftOpen className="h-4 w-4" /> : <PanelLeftClose className="h-4 w-4" />}
          </button>
        </div>

        <nav className="flex-1 space-y-1 p-2">
          {visiblePrimary.map((item) => {
            const Icon = item.icon;
            const active = pathname.startsWith(item.href);
            return (
              <Link
                key={item.href}
                href={item.href}
                className={`flex items-center justify-between rounded-md px-2.5 py-2 text-sm font-medium ${
                  active
                    ? "bg-indigo-50 text-indigo-700"
                    : "text-slate-700 hover:bg-slate-100"
                }`}
              >
                <span className="flex items-center gap-2.5">
                  <Icon className="h-4 w-4 shrink-0" />
                  {!collapsed && <span>{t(item.labelKey)}</span>}
                </span>
                {!collapsed && item.badgeKey === "proposals" && pendingProposalsCount > 0 && (
                  <span
                    data-testid="sidebar-pending-proposals-badge"
                    className="rounded-full bg-rose-600 px-2 py-0.5 text-xs font-semibold text-white"
                  >
                    {pendingProposalsCount}
                  </span>
                )}
              </Link>
            );
          })}

          <div className="my-2 border-t border-slate-200 pt-2">
            {visibleSecondary.map((item) => {
              const Icon = item.icon;
              const active = pathname.startsWith(item.href);
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  className={`flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm font-medium ${
                    active
                      ? "bg-indigo-50 text-indigo-700"
                      : "text-slate-600 hover:bg-slate-100"
                  }`}
                >
                  <Icon className="h-4 w-4 shrink-0" />
                  {!collapsed && <span>{t(item.labelKey)}</span>}
                </Link>
              );
            })}
          </div>
        </nav>

        <div className="border-t border-slate-200 p-3 text-xs text-slate-600">
          {!collapsed && principal && (
            <div className="mb-2 space-y-0.5">
              <div className="truncate font-medium text-slate-800">{principal.email}</div>
              <div>
                {t("roleLabel")}: <span className="font-semibold uppercase">{principal.role}</span>
              </div>
            </div>
          )}
          <button
            type="button"
            onClick={() => void logout()}
            className="flex w-full items-center justify-center gap-1.5 rounded border border-slate-200 px-2 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-100"
          >
            <LogOut className="h-3.5 w-3.5" />
            {!collapsed && <span>{t("logout")}</span>}
          </button>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 items-center justify-between border-b border-slate-200 bg-white px-4">
          <div className="text-sm font-medium text-slate-500">
            {principal?.tenant_slug ?? "hybrid-factory-workspace"}
          </div>
          <div className="flex items-center gap-3">
            <button
              type="button"
              data-testid="locale-toggle-btn"
              onClick={toggleLocale}
              className="flex items-center gap-1.5 rounded-md border border-slate-300 px-2.5 py-1 text-xs font-medium text-slate-700 hover:bg-slate-100"
            >
              <Languages className="h-3.5 w-3.5" />
              <span>{locale === "zh" ? "中 / EN" : "EN / 中"}</span>
            </button>
          </div>
        </header>

        <main className="flex-1 overflow-auto p-4">
          {isLoading ? (
            <div className="p-6 text-sm text-slate-500">Loading workspace session...</div>
          ) : !allowedCurrentRoute ? (
            <div
              data-testid="rbac-forbidden-guard"
              className="mx-auto mt-12 max-w-lg rounded-lg border border-rose-200 bg-rose-50 p-6 text-rose-900"
            >
              <div className="flex items-center gap-2 text-base font-semibold">
                <ShieldAlert className="h-5 w-5 text-rose-600" />
                <span>{t("accessDeniedTitle")}</span>
              </div>
              <p className="mt-2 text-sm text-rose-800">{t("accessDeniedDesc")}</p>
            </div>
          ) : (
            children
          )}
        </main>
      </div>
    </div>
  );
}
```

Create `apps/web/src/app/layout.tsx`:

```tsx
import type { Metadata } from "next";
import React from "react";
import "./globals.css";
import { Providers } from "@/components/providers";

export const metadata: Metadata = {
  title: "Hybrid Omnichannel AI Workbench & ERP",
  description: "Multi-tenant B2C + B2B Cross-Border Omnichannel AI Workbench",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
```

Create `apps/web/src/app/page.tsx`:

```tsx
import { redirect } from "next/navigation";

export default function HomePage() {
  redirect("/inbox");
}
```

Create `apps/web/src/app/(auth)/login/page.tsx`:

```tsx
"use client";

import React, { useState } from "react";
import { useRouter } from "next/navigation";
import { bffFetch } from "@/lib/api";
import { useAuthSession } from "@/components/providers";
import { BffApiError, type Principal } from "@/lib/contracts";

export default function LoginPage() {
  const router = useRouter();
  const { refreshSession } = useAuthSession();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [tenantSlug, setTenantSlug] = useState("quanzhou-led-factory");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const res = await bffFetch<{ principal: Principal }>("/v1/auth/login", {
        method: "POST",
        body: JSON.stringify({
          email,
          password,
          tenant_slug: tenantSlug.trim() || undefined,
        }),
      });
      await refreshSession();
      if (res.principal.onboarding_stage && res.principal.onboarding_stage !== "LiveProduction") {
        router.push("/onboarding");
      } else {
        router.push("/inbox");
      }
    } catch (err) {
      if (err instanceof BffApiError) {
        setError(`${err.code}: ${err.detail}`);
      } else {
        setError("Authentication failed. Please check credentials.");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-100 p-4">
      <div className="w-full max-w-md rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
        <h1 className="text-xl font-bold text-slate-900">Omnichannel AI Workbench</h1>
        <p className="mt-1 text-sm text-slate-500">
          Sign in to your B2B + B2C Hybrid Factory tenant workspace.
        </p>

        {error && (
          <div
            role="alert"
            className="mt-4 rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-800"
          >
            {error}
          </div>
        )}

        <form onSubmit={(e) => void handleSubmit(e)} className="mt-5 space-y-4">
          <div>
            <label htmlFor="tenant-slug" className="block text-xs font-semibold text-slate-700">
              Tenant Workspace Slug
            </label>
            <input
              id="tenant-slug"
              type="text"
              value={tenantSlug}
              onChange={(e) => setTenantSlug(e.target.value)}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
              placeholder="quanzhou-led-factory"
            />
          </div>

          <div>
            <label htmlFor="email" className="block text-xs font-semibold text-slate-700">
              Email
            </label>
            <input
              id="email"
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
              placeholder="owner@quanzhou-led.example.com"
            />
          </div>

          <div>
            <label htmlFor="password" className="block text-xs font-semibold text-slate-700">
              Password
            </label>
            <input
              id="password"
              type="password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
            />
          </div>

          <button
            type="submit"
            disabled={submitting}
            className="w-full rounded-md bg-indigo-600 px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-700 disabled:opacity-50"
          >
            {submitting ? "Signing in..." : "Sign In"}
          </button>
        </form>
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Run test & static checks to verify they pass**

Run: `pnpm --filter web test -- src/__tests__/app-shell-rbac.test.tsx && pnpm --filter web lint`
Expected: PASS (`4 passed` in `app-shell-rbac.test.tsx` + `0` ESLint errors).

- [ ] **Step 5: Commit**

```bash
git add apps/web/
git commit -m "feat(web): add Next.js 15 app shell, thin Auth/SSE BFF, RBAC guards, and i18n"
```

---

### Task 5.2: 4-Column Omnichannel Inbox (`/inbox`)

**Files:**
- Create: `apps/web/src/components/inbox/channel-rail.tsx`
- Create: `apps/web/src/components/inbox/conversation-list.tsx`
- Create: `apps/web/src/components/inbox/message-thread.tsx`
- Create: `apps/web/src/components/inbox/composer.tsx`
- Create: `apps/web/src/app/inbox/page.tsx`
- Test: `apps/web/src/__tests__/inbox-composer.test.tsx`

**Interfaces:**
- Consumes:
  - `GET /api/bff/v1/inbox/conversations?channel=&filter=&q=&limit=20&offset=0`
  - `GET /api/bff/v1/inbox/conversations/{id}/messages?limit=50&offset=0`
  - `POST /api/bff/v1/inbox/conversations/{id}/messages` (`{ direction, content_type, body_text, target_language?, translate_before_send?, use_human_agent_tag? }`)
  - `POST /api/bff/v1/inbox/conversations/{id}/ai-assist` (`{ action, draft_text?, strict_rag }`)
  - `POST /api/bff/v1/ai/translate` (`{ text, target_lang }`)
  - `computeBuyerLocalTimeInfo()` (`apps/web/src/lib/contracts.ts`)
- Produces:
  - `ChannelRail` (`All`, `WhatsApp`, `Email`, `WebChat`, `Shopify`, `WooCommerce`, `SHOPLINE`, `Shoplazza`, `TikTok`)
  - `ConversationList` (`all` / `unread` / `pending_human` / `sla_urgent` tabs)
  - `MessageThread` (local timezone badge + `休息时段` pill, collapsible translation, internal notes)
  - `InboxComposer` (bidirectional live translation preview, `AI Draft`/`Polish`/`Summarize`, `Strict RAG` switch, `Send` vs `Internal Note`)

- [ ] **Step 1: Write the failing test (`apps/web/src/__tests__/inbox-composer.test.tsx`)**

```tsx
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nProvider } from "@/lib/i18n";
import { AuthSessionContext } from "@/components/providers";
import { ChannelRail } from "@/components/inbox/channel-rail";
import { ConversationList, type ConversationSummary } from "@/components/inbox/conversation-list";
import { MessageThread, type InboxMessage } from "@/components/inbox/message-thread";
import { InboxComposer } from "@/components/inbox/composer";

const SAMPLE_CONV: ConversationSummary = {
  id: "conv-1",
  customer_id: "cust-1",
  customer_name: "Hans Weber",
  country_code: "DE",
  iana_timezone: "Europe/Berlin",
  channel_account_id: "acct-1",
  channel_type: "made_in_china_email",
  conversation_mode: "b2b_inquiry",
  status: "pending_human",
  unread_count: 2,
  sla_due_at: "2026-11-19T12:00:00Z",
  csw_expires_at: "2026-11-20T12:00:00Z",
  human_agent_window_expires_at: null,
  last_message_preview: "Please quote 500 pcs LED-COB-24V-5M FOB Shenzhen",
  updated_at: "2026-11-19T08:00:00Z",
};

const SAMPLE_MESSAGES: InboxMessage[] = [
  {
    id: "msg-1",
    conversation_id: "conv-1",
    channel_type: "made_in_china_email",
    direction: "inbound",
    sender_type: "customer",
    content_type: "text",
    body_text: "Need 500 pcs COB LED strip 24V FOB Shenzhen.",
    detected_language: "EN",
    translated_text_zh: "需要 500 条 COB LED 24V 灯带，FOB 深圳报价。",
    delivery_status: "read",
    source_timestamp: "2026-11-19T08:00:00Z",
  },
];

function wrap(ui: React.ReactNode, locale: "zh" | "en" = "zh") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AuthSessionContext.Provider
        value={{
          principal: {
            user_id: "u1",
            tenant_id: "t1",
            role: "owner",
            email: "owner@qzled.example.com",
          },
          isLoading: false,
          logout: async () => {},
          refreshSession: async () => {},
        }}
      >
        <I18nProvider defaultLocale={locale}>{ui}</I18nProvider>
      </AuthSessionContext.Provider>
    </QueryClientProvider>
  );
}

describe("Task 5.2 4-Column Inbox & Composer", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("renders all 9 channel groups and switches conversation filter tabs", () => {
    const onChannel = vi.fn();
    const onFilter = vi.fn();

    wrap(
      <div>
        <ChannelRail selectedChannel="all" onSelectChannel={onChannel} unreadByChannel={{}} />
        <ConversationList
          conversations={[SAMPLE_CONV]}
          selectedConversationId="conv-1"
          activeFilter="all"
          searchQuery=""
          onSelectFilter={onFilter}
          onSearchChange={vi.fn()}
          onSelectConversation={vi.fn()}
        />
      </div>,
      "en"
    );

    for (const label of [
      "All",
      "WhatsApp",
      "Email",
      "WebChat",
      "Shopify",
      "WooCommerce",
      "SHOPLINE",
      "Shoplazza",
      "TikTok",
    ]) {
      expect(screen.getByRole("button", { name: new RegExp(label, "i") })).toBeInTheDocument();
    }

    fireEvent.click(screen.getByRole("button", { name: /pending_human/i }));
    expect(onFilter).toHaveBeenCalledWith("pending_human");

    fireEvent.click(screen.getByRole("button", { name: /sla_urgent/i }));
    expect(onFilter).toHaveBeenCalledWith("sla_urgent");
  });

  it("displays 休息时段 quiet-hours warning pill when buyer local time is 21:00-08:00 and toggles translation", () => {
    // 2026-11-19T22:30:00Z -> Europe/Berlin (UTC+1) = 23:30 (quiet hours!)
    vi.setSystemTime(new Date("2026-11-19T22:30:00Z"));

    wrap(
      <MessageThread
        conversation={SAMPLE_CONV}
        messages={SAMPLE_MESSAGES}
        onToggleDrawer={vi.fn()}
      />,
      "zh"
    );

    expect(screen.getByTestId("buyer-local-time-badge")).toBeInTheDocument();
    expect(screen.getByTestId("quiet-hours-pill")).toHaveTextContent("休息时段");

    const toggleTransBtn = screen.getByTestId("toggle-msg-translation-msg-1");
    fireEvent.click(toggleTransBtn);
    expect(screen.getByText(/需要 500 条 COB LED 24V 灯带/)).toBeInTheDocument();
  });

  it("submits internal_note via composer and previews bidirectional translation + Strict RAG draft", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/ai/translate")) {
        return new Response(
          JSON.stringify({
            translated_text: "We can quote 500 pcs at $14.20 FOB Shenzhen.",
            detected_source_lang: "zh",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/ai-assist")) {
        return new Response(
          JSON.stringify({
            text: "MOQ 500 tier price is $14.20/pc FOB Shenzhen [doc-led#c2].",
            citations: [{ doc_id: "doc-led", chunk_id: "c2", score: 0.91 }],
            abstained: false,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/messages") && init?.method === "POST") {
        return new Response(
          JSON.stringify({
            id: "msg-new",
            conversation_id: "conv-1",
            channel_type: "made_in_china_email",
            direction: "internal_note",
            sender_type: "human_agent",
            content_type: "text",
            body_text: "Check with production lead time",
            delivery_status: "sent",
            source_timestamp: "2026-11-19T08:05:00Z",
          }),
          { status: 201, headers: { "Content-Type": "application/json" } }
        );
      }
      return new Response("{}", { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);

    wrap(<InboxComposer conversation={SAMPLE_CONV} />, "en");

    // Toggle Strict RAG and click AI Draft
    fireEvent.click(screen.getByTestId("composer-strict-rag-toggle"));
    fireEvent.click(screen.getByRole("button", { name: /AI Draft/i }));

    await waitFor(() => {
      const assistCall = fetchMock.mock.calls.find((c) => String(c[0]).includes("/ai-assist"));
      expect(assistCall).toBeDefined();
      expect(JSON.parse(String(assistCall?.[1]?.body))).toMatchObject({
        action: "draft",
        strict_rag: true,
      });
    });

    // Enable live translate preview
    fireEvent.click(screen.getByTestId("live-translate-toggle"));
    fireEvent.change(screen.getByTestId("composer-textarea"), {
      target: { value: "我们500条可以报14.20美金" },
    });

    await waitFor(() => {
      expect(screen.getByTestId("live-translation-preview")).toHaveTextContent(
        "$14.20 FOB Shenzhen"
      );
    });

    // Switch to Internal Note and submit
    fireEvent.click(screen.getByTestId("composer-mode-internal-note"));
    fireEvent.change(screen.getByTestId("composer-textarea"), {
      target: { value: "Check with production lead time" },
    });
    fireEvent.click(screen.getByTestId("composer-submit-btn"));

    await waitFor(() => {
      const postCall = fetchMock.mock.calls.find(
        (c) => String(c[0]).includes("/messages") && c[1]?.method === "POST"
      );
      expect(postCall).toBeDefined();
      expect(JSON.parse(String(postCall?.[1]?.body))).toMatchObject({
        direction: "internal_note",
        body_text: "Check with production lead time",
      });
    });
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm --filter web test -- src/__tests__/inbox-composer.test.tsx`
Expected: FAIL (`Cannot find module '@/components/inbox/channel-rail'`).

- [ ] **Step 3: Write complete implementation**

Create `apps/web/src/components/inbox/channel-rail.tsx`:

```tsx
"use client";

import React from "react";
import Link from "next/link";
import {
  MessageSquare,
  Mail,
  Globe,
  ShoppingBag,
  Store,
  Video,
  Settings,
  Layers,
} from "lucide-react";

export type ChannelFilterKey =
  | "all"
  | "whatsapp"
  | "email"
  | "web_livechat"
  | "shopify"
  | "woocommerce"
  | "shopline"
  | "shoplazza"
  | "tiktok_shop";

const CHANNEL_RAIL_ITEMS: Array<{
  key: ChannelFilterKey;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
}> = [
  { key: "all", label: "All", icon: Layers },
  { key: "whatsapp", label: "WhatsApp", icon: MessageSquare },
  { key: "email", label: "Email", icon: Mail },
  { key: "web_livechat", label: "WebChat", icon: Globe },
  { key: "shopify", label: "Shopify", icon: ShoppingBag },
  { key: "woocommerce", label: "WooCommerce", icon: Store },
  { key: "shopline", label: "SHOPLINE", icon: Store },
  { key: "shoplazza", label: "Shoplazza", icon: Store },
  { key: "tiktok_shop", label: "TikTok", icon: Video },
];

export function ChannelRail({
  selectedChannel,
  onSelectChannel,
  unreadByChannel,
}: {
  selectedChannel: ChannelFilterKey;
  onSelectChannel: (channel: ChannelFilterKey) => void;
  unreadByChannel: Partial<Record<ChannelFilterKey, number>>;
}) {
  return (
    <div
      data-testid="inbox-channel-rail"
      className="flex w-44 shrink-0 flex-col justify-between border-r border-slate-200 bg-white p-2"
    >
      <div className="space-y-1">
        {CHANNEL_RAIL_ITEMS.map((item) => {
          const Icon = item.icon;
          const active = selectedChannel === item.key;
          const unread = unreadByChannel[item.key] ?? 0;
          return (
            <button
              key={item.key}
              type="button"
              onClick={() => onSelectChannel(item.key)}
              className={`flex w-full items-center justify-between rounded-md px-2.5 py-2 text-xs font-medium ${
                active
                  ? "bg-indigo-50 text-indigo-700"
                  : "text-slate-700 hover:bg-slate-100"
              }`}
            >
              <span className="flex items-center gap-2">
                <Icon className="h-3.5 w-3.5 shrink-0" />
                <span>{item.label}</span>
              </span>
              {unread > 0 && (
                <span className="rounded-full bg-indigo-600 px-1.5 py-0.5 text-[10px] font-semibold text-white">
                  {unread}
                </span>
              )}
            </button>
          );
        })}
      </div>

      <Link
        href="/settings/channels"
        className="mt-2 flex items-center gap-2 rounded-md border border-slate-200 px-2.5 py-2 text-xs font-medium text-slate-600 hover:bg-slate-100"
      >
        <Settings className="h-3.5 w-3.5" />
        <span>Channel Settings</span>
      </Link>
    </div>
  );
}
```

Create `apps/web/src/components/inbox/conversation-list.tsx`:

```tsx
"use client";

import React from "react";
import { AlertTriangle, Clock, Search } from "lucide-react";
import type { ChannelType, ConversationMode, ConversationStatus } from "@/lib/contracts";

export type ConversationFilterTab = "all" | "unread" | "pending_human" | "sla_urgent";

export interface ConversationSummary {
  id: string;
  customer_id: string;
  customer_name: string;
  country_code?: string | null;
  iana_timezone?: string | null;
  channel_account_id: string;
  channel_type: ChannelType;
  conversation_mode: ConversationMode;
  status: ConversationStatus;
  unread_count: number;
  sla_due_at?: string | null;
  csw_expires_at?: string | null;
  human_agent_window_expires_at?: string | null;
  last_message_preview: string;
  updated_at: string;
}

const FILTER_TABS: Array<{ key: ConversationFilterTab; label: string }> = [
  { key: "all", label: "all" },
  { key: "unread", label: "unread" },
  { key: "pending_human", label: "pending_human" },
  { key: "sla_urgent", label: "sla_urgent" },
];

export function ConversationList({
  conversations,
  selectedConversationId,
  activeFilter,
  searchQuery,
  onSelectFilter,
  onSearchChange,
  onSelectConversation,
}: {
  conversations: ConversationSummary[];
  selectedConversationId: string | null;
  activeFilter: ConversationFilterTab;
  searchQuery: string;
  onSelectFilter: (filter: ConversationFilterTab) => void;
  onSearchChange: (value: string) => void;
  onSelectConversation: (conversationId: string) => void;
}) {
  return (
    <div
      data-testid="inbox-conversation-list"
      className="flex w-80 shrink-0 flex-col border-r border-slate-200 bg-white"
    >
      <div className="border-b border-slate-200 p-2.5">
        <div className="relative">
          <Search className="pointer-events-none absolute top-2.5 left-2.5 h-3.5 w-3.5 text-slate-400" />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => onSearchChange(e.target.value)}
            placeholder="Search buyer, order, or email..."
            className="w-full rounded-md border border-slate-200 py-1.5 pr-3 pl-8 text-xs focus:border-indigo-500 focus:outline-none"
          />
        </div>

        <div className="mt-2 grid grid-cols-4 gap-1">
          {FILTER_TABS.map((tab) => {
            const active = activeFilter === tab.key;
            return (
              <button
                key={tab.key}
                type="button"
                onClick={() => onSelectFilter(tab.key)}
                className={`truncate rounded px-1.5 py-1 text-[11px] font-medium ${
                  active
                    ? "bg-indigo-600 text-white"
                    : "bg-slate-100 text-slate-700 hover:bg-slate-200"
                }`}
              >
                {tab.label}
              </button>
            );
          })}
        </div>
      </div>

      <div className="flex-1 divide-y divide-slate-100 overflow-y-auto">
        {conversations.length === 0 ? (
          <div className="p-6 text-center text-xs text-slate-400">No conversations match filter.</div>
        ) : (
          conversations.map((conv) => {
            const selected = conv.id === selectedConversationId;
            const isSlaUrgent =
              conv.sla_due_at !== null &&
              conv.sla_due_at !== undefined &&
              new Date(conv.sla_due_at).getTime() - Date.now() < 1000 * 60 * 60 * 4;

            return (
              <button
                key={conv.id}
                type="button"
                data-testid={`conversation-item-${conv.id}`}
                onClick={() => onSelectConversation(conv.id)}
                className={`block w-full p-3 text-left transition-colors ${
                  selected ? "bg-indigo-50/70" : "hover:bg-slate-50"
                }`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-sm font-semibold text-slate-900">
                    {conv.customer_name} {conv.country_code ? `(${conv.country_code})` : ""}
                  </span>
                  {conv.unread_count > 0 && (
                    <span className="rounded-full bg-rose-600 px-1.5 py-0.5 text-[10px] font-semibold text-white">
                      {conv.unread_count}
                    </span>
                  )}
                </div>

                <p className="mt-1 line-clamp-2 text-xs text-slate-600">
                  {conv.last_message_preview}
                </p>

                <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[10px]">
                  <span className="rounded bg-slate-100 px-1.5 py-0.5 font-medium text-slate-700">
                    {conv.channel_type}
                  </span>
                  <span className="rounded bg-blue-50 px-1.5 py-0.5 font-medium text-blue-700">
                    {conv.conversation_mode}
                  </span>
                  {conv.status === "pending_human" && (
                    <span className="inline-flex items-center gap-0.5 rounded bg-amber-100 px-1.5 py-0.5 font-semibold text-amber-800">
                      <AlertTriangle className="h-3 w-3" />
                      pending_human
                    </span>
                  )}
                  {isSlaUrgent && (
                    <span className="inline-flex items-center gap-0.5 rounded bg-rose-100 px-1.5 py-0.5 font-semibold text-rose-800">
                      <Clock className="h-3 w-3" />
                      SLA
                    </span>
                  )}
                </div>
              </button>
            );
          })
        )}
      </div>
    </div>
  );
}
```

Create `apps/web/src/components/inbox/message-thread.tsx`:

```tsx
"use client";

import React, { useState } from "react";
import { Clock, Eye, EyeOff, Globe, Lock, PanelRight, StickyNote } from "lucide-react";
import type { ConversationSummary } from "@/components/inbox/conversation-list";
import {
  computeBuyerLocalTimeInfo,
  type ChannelType,
  type DeliveryStatus,
  type MessageContentType,
  type MessageDirection,
  type MessageSenderType,
} from "@/lib/contracts";
import { useI18n } from "@/lib/i18n";

export interface InboxMessage {
  id: string;
  conversation_id: string;
  channel_type: ChannelType;
  direction: MessageDirection;
  sender_type: MessageSenderType;
  content_type: MessageContentType;
  body_text: string;
  body_sanitized_html?: string | null;
  detected_language?: string | null;
  translated_text_zh?: string | null;
  translated_text_en?: string | null;
  delivery_status: DeliveryStatus;
  source_timestamp: string;
}

export function MessageThread({
  conversation,
  messages,
  onToggleDrawer,
}: {
  conversation: ConversationSummary | null;
  messages: InboxMessage[];
  onToggleDrawer: () => void;
}) {
  const { locale, t } = useI18n();
  const [expandedTranslations, setExpandedTranslations] = useState<Record<string, boolean>>({});

  if (!conversation) {
    return (
      <div className="flex flex-1 items-center justify-center bg-slate-50 text-sm text-slate-400">
        Select a conversation to begin.
      </div>
    );
  }

  const localTimeInfo = computeBuyerLocalTimeInfo(conversation.iana_timezone);

  function toggleTranslation(msgId: string) {
    setExpandedTranslations((prev) => ({ ...prev, [msgId]: !prev[msgId] }));
  }

  return (
    <div data-testid="inbox-message-thread" className="flex flex-1 flex-col bg-slate-50">
      <div className="flex items-center justify-between border-b border-slate-200 bg-white px-4 py-3">
        <div className="flex flex-wrap items-center gap-2.5">
          <h2 className="text-sm font-bold text-slate-900">{conversation.customer_name}</h2>
          {conversation.country_code && (
            <span className="rounded bg-slate-100 px-1.5 py-0.5 text-xs font-medium text-slate-700">
              {conversation.country_code}
            </span>
          )}
          {localTimeInfo && (
            <span
              data-testid="buyer-local-time-badge"
              className="inline-flex items-center gap-1 rounded-full border border-slate-200 bg-slate-50 px-2.5 py-0.5 text-xs text-slate-700"
            >
              <Clock className="h-3 w-3 text-slate-500" />
              <span>
                {t("localTime")}: {localTimeInfo.formattedTime} ({localTimeInfo.ianaTimezone})
              </span>
            </span>
          )}
          {localTimeInfo?.isQuietHours && (
            <span
              data-testid="quiet-hours-pill"
              className="inline-flex items-center gap-1 rounded-full bg-amber-100 px-2.5 py-0.5 text-xs font-semibold text-amber-900"
            >
              {t("quietHours")} (21:00–08:00)
            </span>
          )}
        </div>

        <button
          type="button"
          onClick={onToggleDrawer}
          className="inline-flex items-center gap-1 rounded-md border border-slate-200 px-2.5 py-1 text-xs font-medium text-slate-700 hover:bg-slate-100"
        >
          <PanelRight className="h-3.5 w-3.5" />
          <span>Context</span>
        </button>
      </div>

      <div className="flex-1 space-y-3 overflow-y-auto p-4">
        {messages.map((msg) => {
          const isInternalNote = msg.direction === "internal_note";
          const isOutbound = msg.direction === "outbound";
          const translationText =
            locale === "zh" ? msg.translated_text_zh : msg.translated_text_en;
          const isExpanded = Boolean(expandedTranslations[msg.id]);

          return (
            <div
              key={msg.id}
              data-testid={`message-bubble-${msg.id}`}
              className={`flex flex-col ${isOutbound ? "items-end" : "items-start"}`}
            >
              <div
                className={`max-w-2xl rounded-lg border px-3.5 py-2.5 text-sm shadow-xs ${
                  isInternalNote
                    ? "border-amber-300 bg-amber-50 text-amber-950"
                    : isOutbound
                    ? "border-indigo-600 bg-indigo-600 text-white"
                    : "border-slate-200 bg-white text-slate-900"
                }`}
              >
                <div className="mb-1 flex items-center gap-2 text-[11px] opacity-80">
                  {isInternalNote && <StickyNote className="h-3 w-3" />}
                  <span className="font-semibold">{msg.sender_type}</span>
                  {msg.detected_language && (
                    <span className="inline-flex items-center gap-0.5 rounded bg-black/10 px-1 py-0.2">
                      <Globe className="h-2.5 w-2.5" />
                      [{msg.detected_language}]
                    </span>
                  )}
                  <span>{new Date(msg.source_timestamp).toLocaleTimeString()}</span>
                </div>

                <p className="whitespace-pre-wrap">{msg.body_text}</p>

                {translationText && (
                  <div className="mt-2 border-t border-black/10 pt-1.5">
                    <button
                      type="button"
                      data-testid={`toggle-msg-translation-${msg.id}`}
                      onClick={() => toggleTranslation(msg.id)}
                      className="inline-flex items-center gap-1 text-xs font-medium underline"
                    >
                      {isExpanded ? <EyeOff className="h-3 w-3" /> : <Eye className="h-3 w-3" />}
                      <span>{isExpanded ? t("hideTranslation") : t("showTranslation")}</span>
                    </button>
                    {isExpanded && <p className="mt-1 text-xs opacity-90">{translationText}</p>}
                  </div>
                )}

                {isOutbound && (
                  <div className="mt-1 flex items-center justify-end gap-1 text-[10px] opacity-80">
                    <Lock className="h-2.5 w-2.5" />
                    <span>{msg.delivery_status}</span>
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
```

Create `apps/web/src/components/inbox/composer.tsx`:

```tsx
"use client";

import React, { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Sparkles, Send, StickyNote, Languages, ShieldCheck } from "lucide-react";
import { bffFetch } from "@/lib/api";
import { BffApiError } from "@/lib/contracts";
import type { ConversationSummary } from "@/components/inbox/conversation-list";
import { useI18n } from "@/lib/i18n";

export function InboxComposer({ conversation }: { conversation: ConversationSummary | null }) {
  const { t } = useI18n();
  const queryClient = useQueryClient();

  const [bodyText, setBodyText] = useState("");
  const [mode, setMode] = useState<"outbound" | "internal_note">("outbound");
  const [liveTranslate, setLiveTranslate] = useState(false);
  const [targetLang, setTargetLang] = useState<"en" | "es" | "de" | "fr" | "ja">("en");
  const [translatedPreview, setTranslatedPreview] = useState<string>("");
  const [strictRag, setStrictRag] = useState(false);
  const [complianceError, setComplianceError] = useState<string | null>(null);

  useEffect(() => {
    if (!liveTranslate || !bodyText.trim() || mode === "internal_note") {
      setTranslatedPreview("");
      return;
    }
    const timer = setTimeout(() => {
      void bffFetch<{ translated_text: string }>("/v1/ai/translate", {
        method: "POST",
        body: JSON.stringify({ text: bodyText, target_lang: targetLang }),
      })
        .then((res) => setTranslatedPreview(res.translated_text))
        .catch(() => setTranslatedPreview(""));
    }, 150);

    return () => clearTimeout(timer);
  }, [liveTranslate, bodyText, targetLang, mode]);

  const aiAssistMutation = useMutation({
    mutationFn: async (action: "draft" | "polish" | "summarize") => {
      if (!conversation) return null;
      return bffFetch<{ text: string; abstained: boolean }>(
        `/v1/inbox/conversations/${conversation.id}/ai-assist`,
        {
          method: "POST",
          body: JSON.stringify({
            action,
            draft_text: bodyText || undefined,
            strict_rag: strictRag,
          }),
        }
      );
    },
    onSuccess: (res) => {
      if (res?.text) {
        setBodyText(res.text);
      }
    },
  });

  const sendMutation = useMutation({
    mutationFn: async () => {
      if (!conversation) return null;
      const finalBody =
        mode === "outbound" && liveTranslate && translatedPreview
          ? translatedPreview
          : bodyText;
      return bffFetch(`/v1/inbox/conversations/${conversation.id}/messages`, {
        method: "POST",
        body: JSON.stringify({
          direction: mode,
          content_type: "text",
          body_text: finalBody,
          target_language: liveTranslate ? targetLang : undefined,
          translate_before_send: mode === "outbound" && liveTranslate,
        }),
      });
    },
    onSuccess: () => {
      setBodyText("");
      setTranslatedPreview("");
      setComplianceError(null);
      if (conversation) {
        void queryClient.invalidateQueries({
          queryKey: ["inbox", "messages", conversation.id],
        });
        void queryClient.invalidateQueries({ queryKey: ["inbox", "conversations"] });
      }
    },
    onError: (err) => {
      if (err instanceof BffApiError) {
        setComplianceError(`${err.code}: ${err.detail}`);
      } else {
        setComplianceError("Failed to deliver message.");
      }
    },
  });

  if (!conversation) return null;

  return (
    <div data-testid="inbox-composer" className="border-t border-slate-200 bg-white p-3">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-1.5">
          <button
            type="button"
            onClick={() => aiAssistMutation.mutate("draft")}
            className="inline-flex items-center gap-1 rounded border border-indigo-200 bg-indigo-50 px-2 py-1 text-xs font-medium text-indigo-700 hover:bg-indigo-100"
          >
            <Sparkles className="h-3 w-3" />
            <span>{t("aiDraft")}</span>
          </button>
          <button
            type="button"
            onClick={() => aiAssistMutation.mutate("polish")}
            className="rounded border border-slate-200 px-2 py-1 text-xs font-medium text-slate-700 hover:bg-slate-100"
          >
            {t("aiPolish")}
          </button>
          <button
            type="button"
            onClick={() => aiAssistMutation.mutate("summarize")}
            className="rounded border border-slate-200 px-2 py-1 text-xs font-medium text-slate-700 hover:bg-slate-100"
          >
            {t("aiSummarize")}
          </button>
        </div>

        <label className="inline-flex cursor-pointer items-center gap-1.5 text-xs font-medium text-slate-700">
          <input
            type="checkbox"
            data-testid="composer-strict-rag-toggle"
            checked={strictRag}
            onChange={(e) => setStrictRag(e.target.checked)}
            className="rounded border-slate-300"
          />
          <ShieldCheck className="h-3.5 w-3.5 text-emerald-600" />
          <span>{t("strictRag")}</span>
        </label>
      </div>

      {complianceError && (
        <div
          role="alert"
          className="mb-2 rounded border border-rose-300 bg-rose-50 px-2.5 py-1.5 text-xs text-rose-800"
        >
          {complianceError}
        </div>
      )}

      <textarea
        data-testid="composer-textarea"
        rows={3}
        value={bodyText}
        onChange={(e) => setBodyText(e.target.value)}
        placeholder={
          mode === "internal_note"
            ? "Write an internal note (never sent to buyer)..."
            : "Type reply (enable Live Translate for Chinese -> Foreign language preview)..."
        }
        className={`w-full rounded-md border p-2.5 text-sm focus:outline-none ${
          mode === "internal_note"
            ? "border-amber-300 bg-amber-50/50"
            : "border-slate-300 focus:border-indigo-500"
        }`}
      />

      {liveTranslate && mode === "outbound" && translatedPreview && (
        <div
          data-testid="live-translation-preview"
          className="mt-1.5 rounded border border-indigo-100 bg-indigo-50 px-2.5 py-1.5 text-xs text-indigo-900"
        >
          <span className="font-semibold">Preview ({targetLang.toUpperCase()}): </span>
          {translatedPreview}
        </div>
      )}

      <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-3">
          <div className="inline-flex rounded-md border border-slate-200 p-0.5 text-xs">
            <button
              type="button"
              data-testid="composer-mode-outbound"
              onClick={() => setMode("outbound")}
              className={`rounded px-2 py-1 font-medium ${
                mode === "outbound" ? "bg-indigo-600 text-white" : "text-slate-600"
              }`}
            >
              {t("send")}
            </button>
            <button
              type="button"
              data-testid="composer-mode-internal-note"
              onClick={() => setMode("internal_note")}
              className={`inline-flex items-center gap-1 rounded px-2 py-1 font-medium ${
                mode === "internal_note" ? "bg-amber-500 text-white" : "text-slate-600"
              }`}
            >
              <StickyNote className="h-3 w-3" />
              <span>{t("internalNote")}</span>
            </button>
          </div>

          <label className="inline-flex cursor-pointer items-center gap-1.5 text-xs text-slate-700">
            <input
              type="checkbox"
              data-testid="live-translate-toggle"
              checked={liveTranslate}
              onChange={(e) => setLiveTranslate(e.target.checked)}
            />
            <Languages className="h-3.5 w-3.5 text-indigo-600" />
            <span>{t("liveTranslate")}</span>
          </label>

          <select
            aria-label="Target translation language"
            value={targetLang}
            onChange={(e) => setTargetLang(e.target.value as typeof targetLang)}
            className="rounded border border-slate-300 px-1.5 py-0.5 text-xs"
          >
            <option value="en">EN</option>
            <option value="es">ES</option>
            <option value="de">DE</option>
            <option value="fr">FR</option>
            <option value="ja">JA</option>
          </select>
        </div>

        <button
          type="button"
          data-testid="composer-submit-btn"
          disabled={!bodyText.trim() || sendMutation.isPending}
          onClick={() => sendMutation.mutate()}
          className={`inline-flex items-center gap-1.5 rounded-md px-3.5 py-1.5 text-xs font-semibold text-white disabled:opacity-50 ${
            mode === "internal_note"
              ? "bg-amber-600 hover:bg-amber-700"
              : "bg-indigo-600 hover:bg-indigo-700"
          }`}
        >
          <Send className="h-3.5 w-3.5" />
          <span>{mode === "internal_note" ? t("internalNote") : t("send")}</span>
        </button>
      </div>
    </div>
  );
}
```

Create `apps/web/src/app/inbox/page.tsx`:

```tsx
"use client";

import React, { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AppShell } from "@/components/app-shell";
import { ChannelRail, type ChannelFilterKey } from "@/components/inbox/channel-rail";
import {
  ConversationList,
  type ConversationFilterTab,
  type ConversationSummary,
} from "@/components/inbox/conversation-list";
import { MessageThread, type InboxMessage } from "@/components/inbox/message-thread";
import { InboxComposer } from "@/components/inbox/composer";
import { bffFetch } from "@/lib/api";
import type { PaginatedResponse } from "@/lib/contracts";

export default function InboxPage() {
  const [selectedChannel, setSelectedChannel] = useState<ChannelFilterKey>("all");
  const [activeFilter, setActiveFilter] = useState<ConversationFilterTab>("all");
  const [searchQuery, setSearchQuery] = useState("");
  const [selectedConversationId, setSelectedConversationId] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(true);

  const conversationsQuery = useQuery({
    queryKey: ["inbox", "conversations", selectedChannel, activeFilter, searchQuery],
    queryFn: () => {
      const params = new URLSearchParams({
        channel: selectedChannel,
        filter: activeFilter,
        q: searchQuery,
        limit: "25",
        offset: "0",
      });
      return bffFetch<PaginatedResponse<ConversationSummary>>(
        `/v1/inbox/conversations?${params.toString()}`
      );
    },
  });

  const conversations = conversationsQuery.data?.items ?? [];
  const activeConversation = useMemo(
    () =>
      conversations.find((c) => c.id === selectedConversationId) ?? conversations[0] ?? null,
    [conversations, selectedConversationId]
  );

  const messagesQuery = useQuery({
    queryKey: ["inbox", "messages", activeConversation?.id],
    enabled: Boolean(activeConversation?.id),
    queryFn: () =>
      bffFetch<PaginatedResponse<InboxMessage>>(
        `/v1/inbox/conversations/${activeConversation!.id}/messages?limit=50&offset=0`
      ),
  });

  const pendingProposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () =>
      bffFetch<{ total: number }>("/v1/ai/proposals?status=pending&limit=1&offset=0"),
  });

  return (
    <AppShell pendingProposalsCount={pendingProposalsQuery.data?.total ?? 0}>
      <div className="flex h-[calc(100vh-5.5rem)] overflow-hidden rounded-lg border border-slate-200 bg-white">
        <ChannelRail
          selectedChannel={selectedChannel}
          onSelectChannel={setSelectedChannel}
          unreadByChannel={{}}
        />
        <ConversationList
          conversations={conversations}
          selectedConversationId={activeConversation?.id ?? null}
          activeFilter={activeFilter}
          searchQuery={searchQuery}
          onSelectFilter={setActiveFilter}
          onSearchChange={setSearchQuery}
          onSelectConversation={setSelectedConversationId}
        />
        <div className="flex min-w-0 flex-1 flex-col">
          <MessageThread
            conversation={activeConversation}
            messages={messagesQuery.data?.items ?? []}
            onToggleDrawer={() => setDrawerOpen((prev) => !prev)}
          />
          <InboxComposer conversation={activeConversation} />
        </div>
        {drawerOpen && activeConversation && (
          <div
            data-testid="inbox-context-placeholder"
            className="w-80 shrink-0 border-l border-slate-200 bg-white p-3 text-xs text-slate-500"
          >
            Customer Context Panel ({activeConversation.customer_name})
          </div>
        )}
      </div>
    </AppShell>
  );
}
```

- [ ] **Step 4: Run test & static checks to verify they pass**

Run: `pnpm --filter web test -- src/__tests__/inbox-composer.test.tsx && pnpm --filter web lint`
Expected: PASS (`3 passed` in `inbox-composer.test.tsx` + `0` ESLint errors).

- [ ] **Step 5: Commit**

```bash
git add apps/web/src/components/inbox/ apps/web/src/app/inbox/ apps/web/src/__tests__/inbox-composer.test.tsx
git commit -m "feat(web): build 4-column omnichannel inbox with quiet-hours pill and live translation"
```


---

### Task 5.3: Dual-Mode Customer Context Drawer & Floating AI Copilot (Zero `[object Object]`)

**Files:**
- Create: `apps/web/src/components/inbox/context-drawer.tsx`
- Create: `apps/web/src/components/ai/floating-copilot.tsx`
- Modify: `apps/web/src/app/inbox/page.tsx`
- Test: `apps/web/src/__tests__/copilot-render.test.tsx`

**Interfaces:**
- Consumes:
  - `GET /api/bff/v1/identity/customers/{customerId}/context`
  - `POST /api/bff/v1/identity/merge` (`{ source_customer_id, target_customer_id, reason }`)
  - `POST /api/bff/v1/identity/split` (`{ merge_event_id, reason }`)
  - `GET /api/bff/v1/catalog/live-check?sku=&qty=&incoterm=`
  - `GET /api/bff/v1/ai/capabilities`
  - `GET /api/bff/v1/billing/wallet`
  - `GET /api/bff/v1/ai/proposals?status=pending&limit=1&offset=0`
  - `POST /api/bff/v1/ai/assistant/chat` (`{ capability, message, strict_rag, channel_type, conversation_id?, context? }`)
  - `isPrivilegedCostViewer()` & `formatMoney()` (`apps/web/src/lib/auth-cookie.ts`, `apps/web/src/lib/contracts.ts`)
- Produces:
  - `ContextDrawer` (`apps/web/src/components/inbox/context-drawer.tsx`)
  - `FloatingCopilot` + `sanitizeCopilotCards()` (`apps/web/src/components/ai/floating-copilot.tsx`)

- [ ] **Step 1: Write the failing test (`apps/web/src/__tests__/copilot-render.test.tsx`)**

```tsx
import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nProvider } from "@/lib/i18n";
import { AuthSessionContext } from "@/components/providers";
import { ContextDrawer, type CustomerContextPayload } from "@/components/inbox/context-drawer";
import { FloatingCopilot, sanitizeCopilotCards } from "@/components/ai/floating-copilot";

const MOCK_CUSTOMER_CONTEXT: CustomerContextPayload = {
  customer: {
    id: "cust-target",
    display_name: "Hans Weber",
    customer_type: "b2b_company_rep",
    ltv_usd: "42500.00",
    lifecycle_stage: "qualified",
  },
  company: {
    id: "comp-1",
    legal_name: "Berlin Lighting GmbH",
    country_code: "DE",
    credit_tier: "prepaid",
    default_incoterm: "FOB",
  },
  identities: [
    {
      id: "id-1",
      identity_type: "whatsapp_bsuid",
      identity_value: "bsuid_de_998122",
      confidence_level: "deterministic",
    },
    {
      id: "id-2",
      identity_type: "email",
      identity_value: "hans.weber@berlin-lighting.de",
      confidence_level: "deterministic",
    },
  ],
  probable_duplicates: [
    {
      candidate_customer_id: "cust-dup-9",
      display_name: "H. Weber",
      matched_signal: "email_domain + postal_code",
      similarity_score: 0.91,
    },
  ],
  merge_events: [],
  recent_orders: [
    {
      id: "ord-1",
      order_no: "ORD-2026-001",
      order_mode: "b2c_retail",
      fulfillment_status: "shipped",
      payment_status: "paid",
      total_amount: "189.00",
      currency: "USD",
      tracking_number: "YT26119000123",
      standardized_status: "InTransit",
      sub_status: "InTransit_CustomsCleared",
      is_stagnant_72h: false,
    },
  ],
  recent_quotes: [
    {
      id: "q-1",
      quote_no: "PI-202611-0042",
      version: 1,
      incoterm: "FOB",
      total_amount: "7100.00",
      quote_currency: "USD",
      status: "sent",
    },
  ],
};

function renderWrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AuthSessionContext.Provider
        value={{
          principal: {
            user_id: "u1",
            tenant_id: "t1",
            role: "owner",
            email: "owner@qzled.example.com",
          },
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

describe("Task 5.3 ContextDrawer & FloatingCopilot", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("sanitizes malformed objects and raw dotted internal keys so [object Object] never renders", async () => {
    const sanitized = sanitizeCopilotCards([
      {
        capability: "finance_recon",
        autonomy_level: "L0_copilot",
        metrics: [
          {
            label_key: "monthly_profit_target",
            kind: "money",
            value: { target: 100000 } as unknown as number,
            currency: "CNY",
          },
          {
            label_key: "tax.filing.overdue",
            kind: "count",
            value: 0,
          },
          {
            label_key: "open_receivables_amount",
            kind: "money",
            value: 14500.5,
            currency: "USD",
          },
        ],
      },
    ]);

    expect(sanitized[0].metrics.some((m) => m.label_key === "tax.filing.overdue")).toBe(false);

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/ai/proposals")) {
          return new Response(
            JSON.stringify({ items: [], total: 6, limit: 1, offset: 0, has_more: true }),
            { status: 200, headers: { "Content-Type": "application/json" } }
          );
        }
        if (url.includes("/billing/wallet")) {
          return new Response(
            JSON.stringify({
              plan_tier: "Growth",
              included_credits_remaining: 4200,
              topup_credits_balance: 0,
              auto_downgrade_to_l0: false,
            }),
            { status: 200, headers: { "Content-Type": "application/json" } }
          );
        }
        if (url.includes("/ai/capabilities")) {
          return new Response(
            JSON.stringify({
              capabilities: [
                {
                  capability: "cs_support",
                  configured_autonomy: "L2_auto",
                  pending_proposals_7d: 1,
                  executed_actions_7d: 12,
                  metrics: [{ label_key: "open_threads", kind: "count", value: 12 }],
                },
                {
                  capability: "sales_quoting",
                  configured_autonomy: "L1_proposal",
                  pending_proposals_7d: 3,
                  executed_actions_7d: 5,
                  metrics: [{ label_key: "pending_quotes", kind: "count", value: 3 }],
                },
                {
                  capability: "order_logistics",
                  configured_autonomy: "L2_auto",
                  pending_proposals_7d: 1,
                  executed_actions_7d: 8,
                  metrics: [{ label_key: "stagnant_shipments_72h", kind: "count", value: 1 }],
                },
                {
                  capability: "content_studio",
                  configured_autonomy: "L1_proposal",
                  pending_proposals_7d: 1,
                  executed_actions_7d: 4,
                  metrics: [{ label_key: "drafts_ready", kind: "count", value: 4 }],
                },
                {
                  capability: "biz_analytics",
                  configured_autonomy: "L2_auto",
                  pending_proposals_7d: 0,
                  executed_actions_7d: 2,
                  metrics: [{ label_key: "net_margin_pct", kind: "percent", value: 28.4 }],
                },
                {
                  capability: "finance_recon",
                  configured_autonomy: "L0_copilot",
                  pending_proposals_7d: 0,
                  executed_actions_7d: 0,
                  metrics: [
                    {
                      label_key: "monthly_profit_target",
                      kind: "money",
                      value: { broken: true },
                      currency: "CNY",
                    },
                    { label_key: "tax.filing.overdue", kind: "count", value: 0 },
                    {
                      label_key: "open_receivables_amount",
                      kind: "money",
                      value: 14500.5,
                      currency: "USD",
                    },
                  ],
                },
              ],
            }),
            { status: 200, headers: { "Content-Type": "application/json" } }
          );
        }
        if (url.includes("/ai/assistant/chat")) {
          return new Response(
            JSON.stringify({
              reply_text: "COB LED 24V carries a 3-year warranty [doc-warranty#c1].",
              citations: [{ doc_id: "doc-warranty", chunk_id: "c1", score: 0.94 }],
              tool_calls: [{ tool_call_id: "tool-call-77", tool_name: "get_live_sku_pricing_and_stock" }],
              should_abstain: false,
            }),
            { status: 200, headers: { "Content-Type": "application/json" } }
          );
        }
        return new Response("{}", { status: 200 });
      })
    );

    renderWrap(<FloatingCopilot conversationId="conv-1" />);

    const fab = await screen.findByTestId("copilot-fab-btn");
    expect(fab).toHaveTextContent("6");
    fireEvent.click(fab);

    await waitFor(() => {
      expect(screen.getByTestId("copilot-drawer")).toBeInTheDocument();
    });

    const drawerText = screen.getByTestId("copilot-drawer").textContent ?? "";
    expect(drawerText).not.toContain("[object Object]");
    expect(drawerText).not.toContain("tax.filing.overdue");
    expect(drawerText).toContain("$14,500.50");
    expect(drawerText).toContain("28.4%");

    const proposalsLink = screen.getByTestId("copilot-pending-link");
    expect(proposalsLink).toHaveAttribute("href", "/ai/proposals");

    fireEvent.click(screen.getByTestId("copilot-mode-strict-rag"));
    fireEvent.change(screen.getByTestId("copilot-query-input"), {
      target: { value: "What is the warranty?" },
    });
    fireEvent.click(screen.getByTestId("copilot-query-submit"));

    await waitFor(() => {
      expect(screen.getByText(/doc-warranty#c1/)).toBeInTheDocument();
      expect(screen.getByText(/tool-call-77/)).toBeInTheDocument();
    });
  });

  it("renders BSUID badge, switches ContextDrawer tabs, and executes probable duplicate merge", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/identity/customers/cust-target/context")) {
        return new Response(JSON.stringify(MOCK_CUSTOMER_CONTEXT), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/identity/merge") && init?.method === "POST") {
        return new Response(
          JSON.stringify({ merge_event_id: "me-1", target_customer_id: "cust-target" }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/catalog/live-check")) {
        return new Response(
          JSON.stringify({
            sku: "LED-COB-24V-5M",
            available_qty: 1240,
            unit_price_quote_ccy: "14.20",
            moq_tier_applied: 500,
            incoterm: "FOB",
            source_tool_call_id: "sql-sku-99",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      return new Response("{}", { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);

    renderWrap(<ContextDrawer customerId="cust-target" />);

    await waitFor(() => {
      expect(screen.getByText("bsuid_de_998122")).toBeInTheDocument();
    });

    expect(screen.getByText("ORD-2026-001")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /B2B Quotes & PI/i }));
    expect(screen.getByText("PI-202611-0042")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Live SKU & MOQ Stock/i }));
    fireEvent.change(screen.getByTestId("live-sku-input"), {
      target: { value: "LED-COB-24V-5M" },
    });
    fireEvent.click(screen.getByTestId("live-sku-check-btn"));

    await waitFor(() => {
      expect(screen.getByText(/1240/)).toBeInTheDocument();
      expect(screen.getByText(/sql-sku-99/)).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId("open-merge-dialog-btn"));
    fireEvent.click(screen.getByTestId("confirm-merge-cust-dup-9"));

    await waitFor(() => {
      const mergeCall = fetchMock.mock.calls.find((c) => String(c[0]).includes("/identity/merge"));
      expect(mergeCall).toBeDefined();
      expect(JSON.parse(String(mergeCall?.[1]?.body))).toMatchObject({
        source_customer_id: "cust-dup-9",
        target_customer_id: "cust-target",
      });
    });
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm --filter web test -- src/__tests__/copilot-render.test.tsx`
Expected: FAIL (`Cannot find module '@/components/inbox/context-drawer'`).

- [ ] **Step 3: Write complete implementation**

Create `apps/web/src/components/inbox/context-drawer.tsx`:

```tsx
"use client";

import React, { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Building2,
  Fingerprint,
  GitMerge,
  Split,
  PackageSearch,
  FileText,
  Truck,
} from "lucide-react";
import { bffFetch } from "@/lib/api";
import {
  formatMoney,
  type ConfidenceLevel,
  type CustomerType,
  type IdentityType,
  type Incoterm,
} from "@/lib/contracts";

export interface CustomerContextPayload {
  customer: {
    id: string;
    display_name: string;
    customer_type: CustomerType;
    ltv_usd: string | number;
    lifecycle_stage: string;
  };
  company?: {
    id: string;
    legal_name: string;
    country_code: string;
    credit_tier: string;
    default_incoterm: Incoterm;
  } | null;
  identities: Array<{
    id: string;
    identity_type: IdentityType;
    identity_value: string;
    confidence_level: ConfidenceLevel;
  }>;
  probable_duplicates: Array<{
    candidate_customer_id: string;
    display_name: string;
    matched_signal: string;
    similarity_score: number;
  }>;
  merge_events: Array<{
    id: string;
    source_customer_id: string;
    target_customer_id: string;
    action: "merge" | "split";
    reason?: string | null;
  }>;
  recent_orders: Array<{
    id: string;
    order_no: string;
    order_mode: string;
    fulfillment_status: string;
    payment_status: string;
    total_amount: string | number;
    currency: string;
    tracking_number?: string | null;
    standardized_status?: string | null;
    sub_status?: string | null;
    is_stagnant_72h?: boolean;
  }>;
  recent_quotes: Array<{
    id: string;
    quote_no: string;
    version: number;
    incoterm: Incoterm;
    total_amount: string | number;
    quote_currency: string;
    status: string;
  }>;
}

interface LiveSkuCheckResult {
  sku: string;
  available_qty: number;
  unit_price_quote_ccy: string;
  moq_tier_applied: number;
  incoterm: Incoterm;
  source_tool_call_id: string;
}

export function ContextDrawer({ customerId }: { customerId: string }) {
  const queryClient = useQueryClient();
  const [activeTab, setActiveTab] = useState<"orders" | "quotes" | "sku">("orders");
  const [mergeDialogOpen, setMergeDialogOpen] = useState(false);
  const [mergeReason, setMergeReason] = useState("Confirmed matching buyer identity");
  const [skuInput, setSkuInput] = useState("");
  const [qtyInput, setQtyInput] = useState(100);
  const [incotermInput, setIncotermInput] = useState<Incoterm>("FOB");
  const [liveSkuResult, setLiveSkuResult] = useState<LiveSkuCheckResult | null>(null);

  const contextQuery = useQuery({
    queryKey: ["identity", "context", customerId],
    enabled: Boolean(customerId),
    queryFn: () =>
      bffFetch<CustomerContextPayload>(`/v1/identity/customers/${customerId}/context`),
  });

  const mergeMutation = useMutation({
    mutationFn: (sourceCustomerId: string) =>
      bffFetch("/v1/identity/merge", {
        method: "POST",
        body: JSON.stringify({
          source_customer_id: sourceCustomerId,
          target_customer_id: customerId,
          reason: mergeReason,
        }),
      }),
    onSuccess: () => {
      setMergeDialogOpen(false);
      void queryClient.invalidateQueries({ queryKey: ["identity", "context", customerId] });
    },
  });

  const splitMutation = useMutation({
    mutationFn: (mergeEventId: string) =>
      bffFetch("/v1/identity/split", {
        method: "POST",
        body: JSON.stringify({
          merge_event_id: mergeEventId,
          reason: "Manual operator split",
        }),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["identity", "context", customerId] });
    },
  });

  const liveSkuMutation = useMutation({
    mutationFn: () => {
      const params = new URLSearchParams({
        sku: skuInput.trim(),
        qty: String(qtyInput),
        incoterm: incotermInput,
      });
      return bffFetch<LiveSkuCheckResult>(`/v1/catalog/live-check?${params.toString()}`);
    },
    onSuccess: (data) => {
      setLiveSkuResult(data);
    },
  });

  const data = contextQuery.data;

  return (
    <aside
      data-testid="inbox-context-drawer"
      className="flex w-88 shrink-0 flex-col overflow-y-auto border-l border-slate-200 bg-white p-3"
    >
      {contextQuery.isLoading && <div className="text-xs text-slate-400">Loading customer...</div>}
      {data && (
        <>
          <div className="border-b border-slate-200 pb-3">
            <div className="flex items-start justify-between gap-2">
              <div>
                <h3 className="text-sm font-bold text-slate-900">{data.customer.display_name}</h3>
                <div className="mt-1 flex flex-wrap gap-1 text-[11px]">
                  <span className="rounded bg-indigo-50 px-1.5 py-0.5 font-medium text-indigo-700">
                    {data.customer.customer_type}
                  </span>
                  <span className="rounded bg-slate-100 px-1.5 py-0.5 text-slate-700">
                    LTV {formatMoney(data.customer.ltv_usd, "USD")}
                  </span>
                </div>
              </div>

              {(data.probable_duplicates.length > 0 || data.merge_events.length > 0) && (
                <button
                  type="button"
                  data-testid="open-merge-dialog-btn"
                  onClick={() => setMergeDialogOpen(true)}
                  className="inline-flex items-center gap-1 rounded border border-amber-300 bg-amber-50 px-2 py-1 text-[11px] font-semibold text-amber-900 hover:bg-amber-100"
                >
                  <GitMerge className="h-3 w-3" />
                  <span>Merge / Split ({data.probable_duplicates.length})</span>
                </button>
              )}
            </div>

            {data.company && (
              <div className="mt-2 rounded-md border border-slate-200 bg-slate-50 p-2 text-xs">
                <div className="flex items-center gap-1.5 font-semibold text-slate-800">
                  <Building2 className="h-3.5 w-3.5 text-slate-500" />
                  <span>{data.company.legal_name}</span>
                  <span>({data.company.country_code})</span>
                </div>
                <div className="mt-0.5 text-[11px] text-slate-600">
                  Credit: {data.company.credit_tier} · Default {data.company.default_incoterm}
                </div>
              </div>
            )}

            <div className="mt-2.5 space-y-1">
              <div className="flex items-center gap-1 text-[11px] font-semibold text-slate-500">
                <Fingerprint className="h-3 w-3" />
                <span>Unified Identity Graph</span>
              </div>
              <div className="flex flex-wrap gap-1">
                {data.identities.map((ident) => (
                  <span
                    key={ident.id}
                    data-testid={`identity-badge-${ident.identity_type}`}
                    className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] ${
                      ident.identity_type === "whatsapp_bsuid"
                        ? "border border-emerald-200 bg-emerald-50 text-emerald-800"
                        : "bg-slate-100 text-slate-700"
                    }`}
                  >
                    <span className="font-semibold">{ident.identity_type}:</span>
                    <span>{ident.identity_value}</span>
                  </span>
                ))}
              </div>
            </div>
          </div>

          <div className="mt-3 grid grid-cols-3 gap-1 rounded-md bg-slate-100 p-1 text-[11px] font-medium">
            <button
              type="button"
              onClick={() => setActiveTab("orders")}
              className={`rounded py-1 ${
                activeTab === "orders" ? "bg-white text-indigo-700 shadow-xs" : "text-slate-600"
              }`}
            >
              B2C Orders + 17TRACK
            </button>
            <button
              type="button"
              onClick={() => setActiveTab("quotes")}
              className={`rounded py-1 ${
                activeTab === "quotes" ? "bg-white text-indigo-700 shadow-xs" : "text-slate-600"
              }`}
            >
              B2B Quotes & PI
            </button>
            <button
              type="button"
              onClick={() => setActiveTab("sku")}
              className={`rounded py-1 ${
                activeTab === "sku" ? "bg-white text-indigo-700 shadow-xs" : "text-slate-600"
              }`}
            >
              Live SKU & MOQ Stock
            </button>
          </div>

          <div className="mt-3 flex-1 space-y-2">
            {activeTab === "orders" &&
              data.recent_orders.map((ord) => (
                <div
                  key={ord.id}
                  className="rounded-md border border-slate-200 p-2.5 text-xs"
                >
                  <div className="flex items-center justify-between font-semibold text-slate-900">
                    <Link href={`/orders?orderId=${ord.id}`} className="text-indigo-600 hover:underline">
                      {ord.order_no}
                    </Link>
                    <span>{formatMoney(ord.total_amount, ord.currency)}</span>
                  </div>
                  <div className="mt-1 flex flex-wrap gap-1 text-[11px]">
                    <span className="rounded bg-slate-100 px-1.5 py-0.5">{ord.fulfillment_status}</span>
                    <span className="rounded bg-slate-100 px-1.5 py-0.5">{ord.payment_status}</span>
                  </div>
                  {ord.tracking_number && (
                    <div className="mt-1.5 flex items-center gap-1 text-[11px] text-slate-600">
                      <Truck className="h-3 w-3" />
                      <span>
                        {ord.tracking_number} · {ord.standardized_status ?? "Pending"}
                        {ord.sub_status ? ` (${ord.sub_status})` : ""}
                      </span>
                    </div>
                  )}
                </div>
              ))}

            {activeTab === "quotes" &&
              data.recent_quotes.map((q) => (
                <div key={q.id} className="rounded-md border border-slate-200 p-2.5 text-xs">
                  <div className="flex items-center justify-between font-semibold text-slate-900">
                    <Link
                      href={`/b2b/quotes?quoteId=${q.id}`}
                      className="inline-flex items-center gap-1 text-indigo-600 hover:underline"
                    >
                      <FileText className="h-3 w-3" />
                      <span>
                        {q.quote_no} v{q.version}
                      </span>
                    </Link>
                    <span>{formatMoney(q.total_amount, q.quote_currency)}</span>
                  </div>
                  <div className="mt-1 text-[11px] text-slate-600">
                    Incoterm: {q.incoterm} · Status: {q.status}
                  </div>
                </div>
              ))}

            {activeTab === "sku" && (
              <div className="space-y-2 rounded-md border border-slate-200 p-2.5 text-xs">
                <div className="flex items-center gap-1 font-semibold text-slate-800">
                  <PackageSearch className="h-3.5 w-3.5 text-indigo-600" />
                  <span>Deterministic SQL Stock & MOQ Lookup</span>
                </div>
                <input
                  data-testid="live-sku-input"
                  type="text"
                  value={skuInput}
                  onChange={(e) => setSkuInput(e.target.value)}
                  placeholder="SKU e.g. LED-COB-24V-5M"
                  className="w-full rounded border border-slate-300 px-2 py-1 text-xs"
                />
                <div className="flex gap-2">
                  <input
                    type="number"
                    min={1}
                    value={qtyInput}
                    onChange={(e) => setQtyInput(Number(e.target.value) || 1)}
                    className="w-24 rounded border border-slate-300 px-2 py-1 text-xs"
                  />
                  <select
                    value={incotermInput}
                    onChange={(e) => setIncotermInput(e.target.value as Incoterm)}
                    className="rounded border border-slate-300 px-2 py-1 text-xs"
                  >
                    <option value="EXW">EXW</option>
                    <option value="FOB">FOB</option>
                    <option value="CIF">CIF</option>
                    <option value="DDP">DDP</option>
                    <option value="DAP">DAP</option>
                  </select>
                  <button
                    type="button"
                    data-testid="live-sku-check-btn"
                    disabled={!skuInput.trim() || liveSkuMutation.isPending}
                    onClick={() => liveSkuMutation.mutate()}
                    className="rounded bg-indigo-600 px-2.5 py-1 font-semibold text-white hover:bg-indigo-700 disabled:opacity-50"
                  >
                    Check
                  </button>
                </div>

                {liveSkuResult && (
                  <div className="rounded bg-emerald-50 p-2 text-emerald-950">
                    <div>
                      Available Stock: <strong>{liveSkuResult.available_qty}</strong>
                    </div>
                    <div>
                      Unit Price ({liveSkuResult.incoterm}, MOQ {liveSkuResult.moq_tier_applied}):{" "}
                      <strong>{formatMoney(liveSkuResult.unit_price_quote_ccy, "USD")}</strong>
                    </div>
                    <div className="mt-1 text-[10px] text-emerald-700">
                      Tool Grounding ID: {liveSkuResult.source_tool_call_id}
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>

          {mergeDialogOpen && (
            <div
              role="dialog"
              aria-modal="true"
              className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
            >
              <div className="w-full max-w-md rounded-lg bg-white p-4 shadow-lg">
                <h4 className="text-sm font-bold text-slate-900">
                  Identity Merge & Reversible Split
                </h4>
                <div className="mt-2">
                  <label className="block text-xs font-medium text-slate-600">Audit Reason</label>
                  <input
                    type="text"
                    value={mergeReason}
                    onChange={(e) => setMergeReason(e.target.value)}
                    className="mt-1 w-full rounded border border-slate-300 px-2.5 py-1.5 text-xs"
                  />
                </div>

                <div className="mt-3 space-y-2">
                  {data.probable_duplicates.map((dup) => (
                    <div
                      key={dup.candidate_customer_id}
                      className="flex items-center justify-between rounded border border-slate-200 p-2 text-xs"
                    >
                      <div>
                        <div className="font-semibold">{dup.display_name}</div>
                        <div className="text-[11px] text-slate-500">
                          {dup.matched_signal} ({Math.round(dup.similarity_score * 100)}%)
                        </div>
                      </div>
                      <button
                        type="button"
                        data-testid={`confirm-merge-${dup.candidate_customer_id}`}
                        onClick={() => mergeMutation.mutate(dup.candidate_customer_id)}
                        className="rounded bg-indigo-600 px-2.5 py-1 text-xs font-semibold text-white"
                      >
                        Merge
                      </button>
                    </div>
                  ))}

                  {data.merge_events
                    .filter((e) => e.action === "merge")
                    .map((ev) => (
                      <div
                        key={ev.id}
                        className="flex items-center justify-between rounded border border-slate-200 p-2 text-xs"
                      >
                        <span>Merged event {ev.id}</span>
                        <button
                          type="button"
                          onClick={() => splitMutation.mutate(ev.id)}
                          className="inline-flex items-center gap-1 rounded border border-rose-300 px-2 py-1 text-rose-700"
                        >
                          <Split className="h-3 w-3" />
                          <span>Split</span>
                        </button>
                      </div>
                    ))}
                </div>

                <div className="mt-4 flex justify-end">
                  <button
                    type="button"
                    onClick={() => setMergeDialogOpen(false)}
                    className="rounded border border-slate-300 px-3 py-1 text-xs font-medium"
                  >
                    Close
                  </button>
                </div>
              </div>
            </div>
          )}
        </>
      )}
    </aside>
  );
}
```

Create `apps/web/src/components/ai/floating-copilot.tsx`:

```tsx
"use client";

import React, { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Bot, Send, ShieldCheck, X, AlertTriangle, ArrowUpRight } from "lucide-react";
import { z } from "zod";
import { bffFetch } from "@/lib/api";
import {
  AiCapabilitySchema,
  AutonomyLevelSchema,
  formatMoney,
  type AiCapability,
  type AutonomyLevel,
} from "@/lib/contracts";
import { useI18n } from "@/lib/i18n";

const SAFE_METRIC_LABELS: Record<string, string> = {
  open_threads: "Open Threads",
  pending_quotes: "Pending Quotes",
  stagnant_shipments_72h: "72h Stagnant Shipments",
  drafts_ready: "Content Drafts Ready",
  net_margin_pct: "Net Contribution Margin",
  monthly_profit_target: "Monthly Profit Target",
  open_receivables_amount: "Open Receivables",
};

const CAPABILITY_TITLES: Record<AiCapability, string> = {
  cs_support: "CS Support",
  sales_quoting: "Sales & Quoting",
  order_logistics: "Orders & Logistics",
  content_studio: "Content Studio",
  biz_analytics: "Business Analytics",
  finance_recon: "Finance Recon",
};

export const CopilotMetricSchema = z.object({
  label_key: z.string(),
  kind: z.enum(["count", "money", "percent", "status"]),
  value: z.union([z.number().finite(), z.string()]),
  currency: z.string().length(3).optional(),
});

export const CopilotSummaryCardSchema = z.object({
  capability: AiCapabilitySchema,
  autonomy_level: AutonomyLevelSchema,
  metrics: z.array(CopilotMetricSchema),
});

export type CopilotSummaryCard = z.infer<typeof CopilotSummaryCardSchema>;

export function sanitizeCopilotCards(rawCards: unknown[]): CopilotSummaryCard[] {
  if (!Array.isArray(rawCards)) return [];
  const clean: CopilotSummaryCard[] = [];

  for (const rawCard of rawCards) {
    if (!rawCard || typeof rawCard !== "object") continue;
    const record = rawCard as Record<string, unknown>;
    const capParsed = AiCapabilitySchema.safeParse(record.capability);
    const levelParsed = AutonomyLevelSchema.safeParse(
      record.configured_autonomy ?? record.autonomy_level
    );
    if (!capParsed.success || !levelParsed.success) continue;

    const rawMetrics = Array.isArray(record.metrics)
      ? record.metrics
      : [
          {
            label_key: "open_threads",
            kind: "count",
            value:
              typeof record.executed_actions_7d === "number" ? record.executed_actions_7d : 0,
          },
        ];
    const safeMetrics: z.infer<typeof CopilotMetricSchema>[] = [];

    for (const m of rawMetrics) {
      if (!m || typeof m !== "object") continue;
      const metricObj = m as Record<string, unknown>;
      const key = typeof metricObj.label_key === "string" ? metricObj.label_key : "";
      // Never allow unmapped raw dotted/internal keys such as tax.filing.overdue
      if (!(key in SAFE_METRIC_LABELS)) continue;

      const rawVal = metricObj.value;
      const safeScalar: number | string =
        typeof rawVal === "number" && Number.isFinite(rawVal)
          ? rawVal
          : typeof rawVal === "string"
          ? rawVal
          : 0;

      const parsedMetric = CopilotMetricSchema.safeParse({
        label_key: key,
        kind: metricObj.kind,
        value: safeScalar,
        currency: typeof metricObj.currency === "string" ? metricObj.currency : undefined,
      });
      if (parsedMetric.success) {
        safeMetrics.push(parsedMetric.data);
      }
    }

    clean.push({
      capability: capParsed.data,
      autonomy_level: levelParsed.data,
      metrics: safeMetrics,
    });
  }

  return clean;
}

function renderSafeMetricValue(metric: z.infer<typeof CopilotMetricSchema>): string {
  if (metric.kind === "money") {
    return formatMoney(metric.value, metric.currency ?? "USD");
  }
  if (metric.kind === "percent") {
    const num = typeof metric.value === "number" ? metric.value : Number(metric.value);
    return Number.isFinite(num) ? `${num.toFixed(1)}%` : "0.0%";
  }
  return typeof metric.value === "object" ? "—" : String(metric.value);
}

interface BillingWalletResponse {
  plan_tier: string;
  included_credits_remaining: number;
  topup_credits_balance: number;
  auto_downgrade_to_l0?: boolean;
}

interface AssistantChatResult {
  reply_text: string;
  citations: Array<{ doc_id: string; chunk_id: string; score: number }>;
  tool_calls?: Array<{ tool_call_id?: string; tool_name?: string }>;
  should_abstain: boolean;
  degraded_to_l0?: boolean;
}

export function FloatingCopilot({ conversationId }: { conversationId?: string | null }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<"global" | "strict_rag">("global");
  const [query, setQuery] = useState("");
  const [lastResult, setLastResult] = useState<AssistantChatResult | null>(null);

  const capabilitiesQuery = useQuery({
    queryKey: ["ai", "capabilities"],
    queryFn: () => bffFetch<{ capabilities: unknown[] }>("/v1/ai/capabilities"),
  });

  const walletQuery = useQuery({
    queryKey: ["ai", "wallet"],
    queryFn: () => bffFetch<BillingWalletResponse>("/v1/billing/wallet"),
  });

  const pendingProposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () =>
      bffFetch<{ total: number }>("/v1/ai/proposals?status=pending&limit=1&offset=0"),
  });

  const askMutation = useMutation({
    mutationFn: () =>
      bffFetch<AssistantChatResult>("/v1/ai/assistant/chat", {
        method: "POST",
        body: JSON.stringify({
          capability: mode === "strict_rag" ? "cs_support" : "sales_quoting",
          conversation_id: conversationId ?? undefined,
          message: query,
          strict_rag: mode === "strict_rag",
          channel_type: "web_livechat",
          context: { mode },
        }),
      }),
    onSuccess: (res) => {
      setLastResult(res);
    },
  });

  const pendingCount = pendingProposalsQuery.data?.total ?? 0;
  const wallet = walletQuery.data;
  const remainingCredits =
    (wallet?.included_credits_remaining ?? 0) + (wallet?.topup_credits_balance ?? 0);
  const degradedToL0 = Boolean(
    wallet && (wallet.auto_downgrade_to_l0 && remainingCredits <= 0)
  );
  const safeCards = sanitizeCopilotCards(capabilitiesQuery.data?.capabilities ?? []);

  return (
    <div className="fixed right-5 bottom-5 z-40">
      {!open ? (
        <button
          type="button"
          data-testid="copilot-fab-btn"
          onClick={() => setOpen(true)}
          className="relative flex h-12 w-12 items-center justify-center rounded-full bg-indigo-600 text-white shadow-lg hover:bg-indigo-700"
        >
          <Bot className="h-6 w-6" />
          {pendingCount > 0 && (
            <span className="absolute -top-1 -right-1 rounded-full bg-rose-600 px-1.5 py-0.5 text-[10px] font-bold text-white">
              {pendingCount}
            </span>
          )}
        </button>
      ) : (
        <div
          data-testid="copilot-drawer"
          className="flex h-[540px] w-96 flex-col rounded-xl border border-slate-200 bg-white shadow-2xl"
        >
          <div className="flex items-center justify-between border-b border-slate-200 bg-slate-900 px-3.5 py-2.5 text-white rounded-t-xl">
            <div className="flex items-center gap-2">
              <Bot className="h-4 w-4 text-emerald-400" />
              <span className="text-sm font-semibold">AI Workbench Copilot</span>
              {degradedToL0 && (
                <span className="rounded bg-amber-500 px-1.5 py-0.5 text-[10px] font-bold text-slate-950">
                  L0 Fallback
                </span>
              )}
            </div>

            <div className="flex items-center gap-2">
              <Link
                href="/ai/proposals"
                data-testid="copilot-pending-link"
                className="inline-flex items-center gap-1 rounded-full bg-rose-600 px-2 py-0.5 text-[11px] font-semibold text-white hover:bg-rose-700"
              >
                <span>
                  {t("pendingApprovals")}: {pendingCount}
                </span>
                <ArrowUpRight className="h-3 w-3" />
              </Link>
              <button
                type="button"
                onClick={() => setOpen(false)}
                className="rounded p-1 hover:bg-slate-800"
                aria-label="Close copilot"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
          </div>

          <div className="flex items-center justify-between border-b border-slate-200 bg-slate-50 px-3 py-2 text-xs">
            <div className="inline-flex rounded border border-slate-200 bg-white p-0.5">
              <button
                type="button"
                data-testid="copilot-mode-global"
                onClick={() => setMode("global")}
                className={`rounded px-2 py-0.5 font-medium ${
                  mode === "global" ? "bg-indigo-600 text-white" : "text-slate-600"
                }`}
              >
                {t("globalMode")}
              </button>
              <button
                type="button"
                data-testid="copilot-mode-strict-rag"
                onClick={() => setMode("strict_rag")}
                className={`inline-flex items-center gap-1 rounded px-2 py-0.5 font-medium ${
                  mode === "strict_rag" ? "bg-emerald-600 text-white" : "text-slate-600"
                }`}
              >
                <ShieldCheck className="h-3 w-3" />
                <span>{t("strictRag")}</span>
              </button>
            </div>

            {wallet && (
              <span className="text-[11px] text-slate-500">Credits: {remainingCredits}</span>
            )}
          </div>

          <div className="flex-1 space-y-2.5 overflow-y-auto p-3">
            <div className="grid grid-cols-2 gap-2">
              {safeCards.map((card) => (
                <div
                  key={card.capability}
                  className="rounded-lg border border-slate-200 bg-slate-50 p-2 text-xs"
                >
                  <div className="flex items-center justify-between gap-1">
                    <span className="font-semibold text-slate-800">
                      {CAPABILITY_TITLES[card.capability]}
                    </span>
                    <span className="rounded bg-white px-1 py-0.5 text-[10px] font-medium text-slate-600">
                      {card.autonomy_level}
                    </span>
                  </div>
                  <div className="mt-1.5 space-y-1">
                    {card.metrics.map((metric) => (
                      <div key={metric.label_key} className="flex items-center justify-between">
                        <span className="text-[11px] text-slate-500">
                          {SAFE_METRIC_LABELS[metric.label_key]}
                        </span>
                        <span className="font-semibold text-slate-900">
                          {renderSafeMetricValue(metric)}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>

            {lastResult && (
              <div
                data-testid="copilot-answer-box"
                className={`rounded-lg border p-2.5 text-xs ${
                  lastResult.should_abstain
                    ? "border-amber-300 bg-amber-50 text-amber-950"
                    : "border-indigo-200 bg-indigo-50/70 text-slate-900"
                }`}
              >
                {lastResult.should_abstain && (
                  <div className="mb-1 flex items-center gap-1 font-semibold text-amber-800">
                    <AlertTriangle className="h-3.5 w-3.5" />
                    <span>Abstained (Strict RAG &lt; 0.62) — Escalated to Human</span>
                  </div>
                )}
                <p className="whitespace-pre-wrap">{lastResult.reply_text}</p>
                {lastResult.citations.length > 0 && (
                  <div className="mt-1.5 flex flex-wrap gap-1">
                    {lastResult.citations.map((cit) => (
                      <span
                        key={`${cit.doc_id}-${cit.chunk_id}`}
                        className="rounded bg-white px-1.5 py-0.5 text-[10px] font-medium text-emerald-700"
                      >
                        [{cit.doc_id}#{cit.chunk_id}] ({cit.score.toFixed(2)})
                      </span>
                    ))}
                  </div>
                )}
                {lastResult.tool_calls?.[0]?.tool_call_id && (
                  <div className="mt-1 text-[10px] text-slate-500">
                    SQL Tool Verified: {lastResult.tool_calls[0].tool_call_id}
                  </div>
                )}
              </div>
            )}
          </div>

          <div className="border-t border-slate-200 p-2.5">
            <div className="flex gap-1.5">
              <input
                data-testid="copilot-query-input"
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Ask SKU stock, order tracking, or KB..."
                className="flex-1 rounded-md border border-slate-300 px-2.5 py-1.5 text-xs focus:border-indigo-500 focus:outline-none"
              />
              <button
                type="button"
                data-testid="copilot-query-submit"
                disabled={!query.trim() || askMutation.isPending}
                onClick={() => askMutation.mutate()}
                className="rounded-md bg-indigo-600 px-3 py-1.5 text-white hover:bg-indigo-700 disabled:opacity-50"
              >
                <Send className="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
```

Modify `apps/web/src/app/inbox/page.tsx` to replace the context placeholder with `ContextDrawer` and mount `FloatingCopilot`:

```tsx
"use client";

import React, { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AppShell } from "@/components/app-shell";
import { ChannelRail, type ChannelFilterKey } from "@/components/inbox/channel-rail";
import {
  ConversationList,
  type ConversationFilterTab,
  type ConversationSummary,
} from "@/components/inbox/conversation-list";
import { MessageThread, type InboxMessage } from "@/components/inbox/message-thread";
import { InboxComposer } from "@/components/inbox/composer";
import { ContextDrawer } from "@/components/inbox/context-drawer";
import { FloatingCopilot } from "@/components/ai/floating-copilot";
import { bffFetch } from "@/lib/api";
import type { PaginatedResponse } from "@/lib/contracts";

export default function InboxPage() {
  const [selectedChannel, setSelectedChannel] = useState<ChannelFilterKey>("all");
  const [activeFilter, setActiveFilter] = useState<ConversationFilterTab>("all");
  const [searchQuery, setSearchQuery] = useState("");
  const [selectedConversationId, setSelectedConversationId] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(true);

  const conversationsQuery = useQuery({
    queryKey: ["inbox", "conversations", selectedChannel, activeFilter, searchQuery],
    queryFn: () => {
      const params = new URLSearchParams({
        channel: selectedChannel,
        filter: activeFilter,
        q: searchQuery,
        limit: "25",
        offset: "0",
      });
      return bffFetch<PaginatedResponse<ConversationSummary>>(
        `/v1/inbox/conversations?${params.toString()}`
      );
    },
  });

  const conversations = conversationsQuery.data?.items ?? [];
  const activeConversation = useMemo(
    () =>
      conversations.find((c) => c.id === selectedConversationId) ?? conversations[0] ?? null,
    [conversations, selectedConversationId]
  );

  const messagesQuery = useQuery({
    queryKey: ["inbox", "messages", activeConversation?.id],
    enabled: Boolean(activeConversation?.id),
    queryFn: () =>
      bffFetch<PaginatedResponse<InboxMessage>>(
        `/v1/inbox/conversations/${activeConversation!.id}/messages?limit=50&offset=0`
      ),
  });

  const pendingProposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () =>
      bffFetch<{ total: number }>("/v1/ai/proposals?status=pending&limit=1&offset=0"),
  });

  return (
    <AppShell pendingProposalsCount={pendingProposalsQuery.data?.total ?? 0}>
      <div className="flex h-[calc(100vh-5.5rem)] overflow-hidden rounded-lg border border-slate-200 bg-white">
        <ChannelRail
          selectedChannel={selectedChannel}
          onSelectChannel={setSelectedChannel}
          unreadByChannel={{}}
        />
        <ConversationList
          conversations={conversations}
          selectedConversationId={activeConversation?.id ?? null}
          activeFilter={activeFilter}
          searchQuery={searchQuery}
          onSelectFilter={setActiveFilter}
          onSearchChange={setSearchQuery}
          onSelectConversation={setSelectedConversationId}
        />
        <div className="flex min-w-0 flex-1 flex-col">
          <MessageThread
            conversation={activeConversation}
            messages={messagesQuery.data?.items ?? []}
            onToggleDrawer={() => setDrawerOpen((prev) => !prev)}
          />
          <InboxComposer conversation={activeConversation} />
        </div>
        {drawerOpen && activeConversation && (
          <ContextDrawer customerId={activeConversation.customer_id} />
        )}
      </div>
      <FloatingCopilot conversationId={activeConversation?.id ?? null} />
    </AppShell>
  );
}
```

- [ ] **Step 4: Run test & static checks to verify they pass**

Run: `pnpm --filter web test -- src/__tests__/copilot-render.test.tsx && pnpm --filter web lint`
Expected: PASS (`2 passed` in `copilot-render.test.tsx` + `0` ESLint errors).

- [ ] **Step 5: Commit**

```bash
git add apps/web/src/components/inbox/context-drawer.tsx apps/web/src/components/ai/floating-copilot.tsx apps/web/src/app/inbox/page.tsx apps/web/src/__tests__/copilot-render.test.tsx
git commit -m "feat(web): add customer context drawer with BSUID merge/split and Zod-guarded AI copilot"
```

---

### Task 5.4: L1 AI Proposal Queue (`/ai/proposals`) & Governance / KB Console (`/ai/capabilities`)

**Files:**
- Create: `apps/web/src/components/ai/proposal-review-card.tsx`
- Create: `apps/web/src/app/ai/proposals/page.tsx`
- Create: `apps/web/src/app/ai/capabilities/page.tsx`
- Test: `apps/web/src/__tests__/proposal-queue.test.tsx`

**Interfaces:**
- Consumes:
  - `GET /api/bff/v1/ai/proposals?status=pending&proposal_type=&limit=20&offset=0`
  - `POST /api/bff/v1/ai/proposals/{id}/approve`
  - `POST /api/bff/v1/ai/proposals/{id}/edit-approve` (`{ edited_payload }`)
  - `POST /api/bff/v1/ai/proposals/{id}/reject` (`{ reason }`)
  - `GET /api/bff/v1/ai/capabilities`
  - `PATCH /api/bff/v1/ai/capabilities` (`{ settings: { [capability]: autonomy_level } }`)
  - `GET /api/bff/v1/billing/wallet`
  - `GET /api/bff/v1/ai/kb/documents`
  - `POST /api/bff/v1/ai/kb/documents` (`FormData`: `file`, `title`)
  - `DELETE /api/bff/v1/ai/kb/documents/{id}`
  - `canConfigureAiGovernance()` (`apps/web/src/lib/auth-cookie.ts`)
- Produces:
  - `ProposalReviewCard` (`apps/web/src/components/ai/proposal-review-card.tsx`)
  - `/ai/proposals` (`apps/web/src/app/ai/proposals/page.tsx`)
  - `/ai/capabilities` (`apps/web/src/app/ai/capabilities/page.tsx`)

- [ ] **Step 1: Write the failing test (`apps/web/src/__tests__/proposal-queue.test.tsx`)**

```tsx
import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nProvider } from "@/lib/i18n";
import { AuthSessionContext } from "@/components/providers";
import AiProposalsPage from "@/app/ai/proposals/page";
import AiCapabilitiesPage from "@/app/ai/capabilities/page";
import type { Principal } from "@/lib/contracts";

function renderWithAuth(ui: React.ReactNode, role: Principal["role"] = "owner") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AuthSessionContext.Provider
        value={{
          principal: {
            user_id: "u1",
            tenant_id: "t1",
            role,
            email: `${role}@qzled.example.com`,
          },
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

describe("Task 5.4 L1 Proposal Queue & AI Governance Console", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("displays margin floor, [doc#chunk] citations, and executes Edit & Approve mutation", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/ai/proposals?") || url.endsWith("/ai/proposals")) {
        return new Response(
          JSON.stringify({
            items: [
              {
                id: "prop-101",
                capability: "sales_quoting",
                proposal_type: "quote_draft",
                conversation_id: "conv-1",
                proposed_payload: {
                  sku: "LED-COB-24V-5M",
                  quantity: 500,
                  unit_price_usd: 14.2,
                  incoterm: "FOB",
                },
                citations: [{ doc_id: "doc-led", chunk_id: "c4", score: 0.89 }],
                confidence_score: 0.92,
                estimated_margin_pct: 24.5,
                min_margin_floor_pct: 15.0,
                status: "pending",
                created_at: "2026-11-19T08:10:00Z",
              },
            ],
            total: 1,
            limit: 20,
            offset: 0,
            has_more: false,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/ai/proposals/prop-101/edit-approve") && init?.method === "POST") {
        return new Response(JSON.stringify({ id: "prop-101", status: "edited_and_approved" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response("{}", { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);

    renderWithAuth(<AiProposalsPage />, "owner");

    await waitFor(() => {
      expect(screen.getByText(/24.5%/)).toBeInTheDocument();
      expect(screen.getByText(/15.0%/)).toBeInTheDocument();
      expect(screen.getByText(/\[doc-led#c4\]/)).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId("proposal-edit-toggle-prop-101"));
    const editor = screen.getByTestId("proposal-json-editor-prop-101");
    fireEvent.change(editor, {
      target: {
        value: JSON.stringify({
          sku: "LED-COB-24V-5M",
          quantity: 500,
          unit_price_usd: 13.8,
          incoterm: "FOB",
        }),
      },
    });
    fireEvent.click(screen.getByTestId("proposal-edit-submit-prop-101"));

    await waitFor(() => {
      const editCall = fetchMock.mock.calls.find((c) =>
        String(c[0]).includes("/ai/proposals/prop-101/edit-approve")
      );
      expect(editCall).toBeDefined();
      expect(JSON.parse(String(editCall?.[1]?.body))).toMatchObject({
        edited_payload: { unit_price_usd: 13.8 },
      });
    });
  });

  it("locks finance_recon from L2_auto and displays L0 fallback alert when credits reach 0", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/ai/proposals")) {
          return new Response(
            JSON.stringify({ items: [], total: 0, limit: 1, offset: 0, has_more: false }),
            { status: 200, headers: { "Content-Type": "application/json" } }
          );
        }
        if (url.includes("/billing/wallet")) {
          return new Response(
            JSON.stringify({
              plan_tier: "starter",
              included_credits_remaining: 0,
              topup_credits_balance: 0,
              overage_spent_cents: 5000,
              overage_spend_cap_cents: 5000,
              auto_downgrade_to_l0: true,
            }),
            { status: 200, headers: { "Content-Type": "application/json" } }
          );
        }
        if (url.includes("/ai/capabilities")) {
          return new Response(
            JSON.stringify({
              capabilities: [
                {
                  capability: "cs_support",
                  configured_autonomy: "L2_auto",
                  max_allowed_autonomy: "L2_auto",
                },
                {
                  capability: "sales_quoting",
                  configured_autonomy: "L1_proposal",
                  max_allowed_autonomy: "L1_proposal",
                },
                {
                  capability: "order_logistics",
                  configured_autonomy: "L2_auto",
                  max_allowed_autonomy: "L2_auto",
                },
                {
                  capability: "content_studio",
                  configured_autonomy: "L1_proposal",
                  max_allowed_autonomy: "L1_proposal",
                },
                {
                  capability: "biz_analytics",
                  configured_autonomy: "L2_auto",
                  max_allowed_autonomy: "L2_auto",
                },
                {
                  capability: "finance_recon",
                  configured_autonomy: "L0_copilot",
                  max_allowed_autonomy: "L0_copilot",
                },
              ],
            }),
            { status: 200, headers: { "Content-Type": "application/json" } }
          );
        }
        if (url.includes("/ai/kb/documents")) {
          return new Response(
            JSON.stringify({
              items: [
                {
                  id: "kb-1",
                  title: "LED_Warranty_Policy.md",
                  chunk_count: 14,
                  status: "indexed",
                  created_at: "2026-11-19T00:00:00Z",
                },
              ],
              total: 1,
              limit: 20,
              offset: 0,
              has_more: false,
            }),
            { status: 200, headers: { "Content-Type": "application/json" } }
          );
        }
        return new Response("{}", { status: 200 });
      })
    );

    renderWithAuth(<AiCapabilitiesPage />, "owner");

    await waitFor(() => {
      expect(screen.getByTestId("wallet-l0-fallback-alert")).toBeInTheDocument();
      expect(screen.getByTestId("finance-recon-l0-lock")).toBeInTheDocument();
    });

    const financeL2Btn = screen.getByTestId("cap-btn-finance_recon-L2_auto");
    expect(financeL2Btn).toBeDisabled();
    expect(screen.getByText("LED_Warranty_Policy.md")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm --filter web test -- src/__tests__/proposal-queue.test.tsx`
Expected: FAIL (`Cannot find module '@/app/ai/proposals/page'`).

- [ ] **Step 3: Write complete implementation**

Create `apps/web/src/components/ai/proposal-review-card.tsx`:

```tsx
"use client";

import React, { useState } from "react";
import { Check, Edit3, XCircle, AlertTriangle, ShieldCheck } from "lucide-react";
import type { AiCapability, ProposalStatus, ProposalType } from "@/lib/contracts";

export interface AiProposalItem {
  id: string;
  capability: AiCapability;
  proposal_type: ProposalType;
  conversation_id?: string | null;
  proposed_payload: Record<string, unknown>;
  citations: Array<{ doc_id: string; chunk_id: string; score: number }>;
  confidence_score: number;
  estimated_margin_pct?: number | null;
  min_margin_floor_pct?: number | null;
  source_tool_call_id?: string | null;
  status: ProposalStatus;
  created_at: string;
}

export function ProposalReviewCard({
  proposal,
  canReview,
  onApprove,
  onEditApprove,
  onReject,
}: {
  proposal: AiProposalItem;
  canReview: boolean;
  onApprove: (id: string) => void;
  onEditApprove: (id: string, editedPayload: Record<string, unknown>) => void;
  onReject: (id: string, reason: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [jsonDraft, setJsonDraft] = useState(() =>
    JSON.stringify(proposal.proposed_payload, null, 2)
  );
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [rejectionReason, setRejectionReason] = useState("");

  const marginFloor = proposal.min_margin_floor_pct ?? 15.0;
  const belowMarginFloor =
    proposal.estimated_margin_pct !== null &&
    proposal.estimated_margin_pct !== undefined &&
    proposal.estimated_margin_pct < marginFloor;

  function submitEdit() {
    try {
      const parsed = JSON.parse(jsonDraft) as Record<string, unknown>;
      setJsonError(null);
      onEditApprove(proposal.id, parsed);
      setEditing(false);
    } catch {
      setJsonError("Invalid JSON syntax. Please check braces and quotes.");
    }
  }

  return (
    <div
      data-testid={`proposal-card-${proposal.id}`}
      className="rounded-lg border border-slate-200 bg-white p-4 shadow-xs"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <span className="rounded bg-indigo-50 px-2 py-0.5 font-semibold text-indigo-700">
            {proposal.capability}
          </span>
          <span className="rounded bg-slate-100 px-2 py-0.5 font-medium text-slate-800">
            {proposal.proposal_type}
          </span>
          <span className="text-slate-500">
            Confidence: {(proposal.confidence_score * 100).toFixed(1)}%
          </span>
          <span className="rounded bg-slate-100 px-2 py-0.5 text-slate-600">
            {proposal.status}
          </span>
        </div>

        {proposal.estimated_margin_pct !== null && proposal.estimated_margin_pct !== undefined && (
          <div
            data-testid={`proposal-margin-${proposal.id}`}
            className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-semibold ${
              belowMarginFloor
                ? "bg-rose-100 text-rose-800"
                : "bg-emerald-100 text-emerald-800"
            }`}
          >
            {belowMarginFloor ? (
              <AlertTriangle className="h-3.5 w-3.5" />
            ) : (
              <ShieldCheck className="h-3.5 w-3.5" />
            )}
            <span>
              Est. Margin: {proposal.estimated_margin_pct.toFixed(1)}% (Floor:{" "}
              {marginFloor.toFixed(1)}%)
            </span>
          </div>
        )}
      </div>

      {!editing ? (
        <pre className="mt-3 overflow-x-auto rounded-md bg-slate-50 p-3 text-xs text-slate-800">
          {JSON.stringify(proposal.proposed_payload, null, 2)}
        </pre>
      ) : (
        <div className="mt-3 space-y-2">
          <textarea
            data-testid={`proposal-json-editor-${proposal.id}`}
            rows={6}
            value={jsonDraft}
            onChange={(e) => setJsonDraft(e.target.value)}
            className="w-full rounded-md border border-indigo-300 bg-white p-2.5 font-mono text-xs"
          />
          {jsonError && <div className="text-xs text-rose-600">{jsonError}</div>}
          <div className="flex gap-2">
            <button
              type="button"
              data-testid={`proposal-edit-submit-${proposal.id}`}
              onClick={submitEdit}
              className="rounded-md bg-indigo-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-indigo-700"
            >
              Save Edit & Approve
            </button>
            <button
              type="button"
              onClick={() => setEditing(false)}
              className="rounded-md border border-slate-300 px-3 py-1.5 text-xs font-medium"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {proposal.citations.length > 0 && (
        <div className="mt-2.5 flex flex-wrap gap-1.5">
          {proposal.citations.map((cit) => (
            <span
              key={`${cit.doc_id}-${cit.chunk_id}`}
              className="rounded bg-emerald-50 px-2 py-0.5 text-[11px] font-medium text-emerald-800"
            >
              [{cit.doc_id}#{cit.chunk_id}] · {cit.score.toFixed(2)}
            </span>
          ))}
        </div>
      )}

      {proposal.status === "pending" && (
        <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-slate-100 pt-3">
          <button
            type="button"
            disabled={!canReview || belowMarginFloor}
            data-testid={`proposal-approve-${proposal.id}`}
            onClick={() => onApprove(proposal.id)}
            className="inline-flex items-center gap-1 rounded-md bg-emerald-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-emerald-700 disabled:opacity-50"
          >
            <Check className="h-3.5 w-3.5" />
            <span>Approve</span>
          </button>

          <button
            type="button"
            disabled={!canReview}
            data-testid={`proposal-edit-toggle-${proposal.id}`}
            onClick={() => setEditing((prev) => !prev)}
            className="inline-flex items-center gap-1 rounded-md border border-indigo-300 bg-indigo-50 px-3 py-1.5 text-xs font-semibold text-indigo-700 hover:bg-indigo-100 disabled:opacity-50"
          >
            <Edit3 className="h-3.5 w-3.5" />
            <span>Edit & Approve</span>
          </button>

          <button
            type="button"
            disabled={!canReview}
            data-testid={`proposal-reject-toggle-${proposal.id}`}
            onClick={() => setRejecting((prev) => !prev)}
            className="inline-flex items-center gap-1 rounded-md border border-rose-300 px-3 py-1.5 text-xs font-semibold text-rose-700 hover:bg-rose-50 disabled:opacity-50"
          >
            <XCircle className="h-3.5 w-3.5" />
            <span>Reject</span>
          </button>
        </div>
      )}

      {rejecting && (
        <div className="mt-2.5 flex gap-2">
          <input
            type="text"
            data-testid={`proposal-reject-reason-${proposal.id}`}
            value={rejectionReason}
            onChange={(e) => setRejectionReason(e.target.value)}
            placeholder="Reason for rejecting AI proposal..."
            className="flex-1 rounded border border-slate-300 px-2.5 py-1 text-xs"
          />
          <button
            type="button"
            data-testid={`proposal-reject-submit-${proposal.id}`}
            disabled={!rejectionReason.trim()}
            onClick={() => {
              onReject(proposal.id, rejectionReason.trim());
              setRejecting(false);
            }}
            className="rounded bg-rose-600 px-3 py-1 text-xs font-semibold text-white disabled:opacity-50"
          >
            Confirm Reject
          </button>
        </div>
      )}
    </div>
  );
}
```

Create `apps/web/src/app/ai/proposals/page.tsx`:

```tsx
"use client";

import React, { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AppShell } from "@/components/app-shell";
import { useAuthSession } from "@/components/providers";
import { ProposalReviewCard, type AiProposalItem } from "@/components/ai/proposal-review-card";
import { bffFetch } from "@/lib/api";
import type { PaginatedResponse, ProposalStatus, ProposalType } from "@/lib/contracts";

export default function AiProposalsPage() {
  const { principal } = useAuthSession();
  const queryClient = useQueryClient();
  const [statusFilter, setStatusFilter] = useState<ProposalStatus>("pending");
  const [typeFilter, setTypeFilter] = useState<"" | ProposalType>("");

  const proposalsQuery = useQuery({
    queryKey: ["ai", "proposals", statusFilter, typeFilter],
    queryFn: () => {
      const params = new URLSearchParams({
        status: statusFilter,
        limit: "20",
        offset: "0",
      });
      if (typeFilter) params.set("proposal_type", typeFilter);
      return bffFetch<PaginatedResponse<AiProposalItem>>(`/v1/ai/proposals?${params.toString()}`);
    },
  });

  const pendingCountQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () =>
      bffFetch<{ total: number }>("/v1/ai/proposals?status=pending&limit=1&offset=0"),
  });

  const approveMutation = useMutation({
    mutationFn: (id: string) =>
      bffFetch(`/v1/ai/proposals/${id}/approve`, { method: "POST" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["ai", "proposals"] });
    },
  });

  const editApproveMutation = useMutation({
    mutationFn: ({ id, edited_payload }: { id: string; edited_payload: Record<string, unknown> }) =>
      bffFetch(`/v1/ai/proposals/${id}/edit-approve`, {
        method: "POST",
        body: JSON.stringify({ edited_payload }),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["ai", "proposals"] });
    },
  });

  const rejectMutation = useMutation({
    mutationFn: ({ id, reason }: { id: string; reason: string }) =>
      bffFetch(`/v1/ai/proposals/${id}/reject`, {
        method: "POST",
        body: JSON.stringify({ reason }),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["ai", "proposals"] });
    },
  });

  const items = proposalsQuery.data?.items ?? [];

  return (
    <AppShell
      currentPath="/ai/proposals"
      pendingProposalsCount={pendingCountQuery.data?.total ?? 0}
    >
      <div className="mx-auto max-w-5xl space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="text-lg font-bold text-slate-900">L1 AI Proposal Approval Queue</h1>
            <p className="text-xs text-slate-500">
              Review AI drafts, CRM extractions, B2B PI quotes, and identity merges. Edit diffs feed tenant-private Golden Few-shot evals.
            </p>
          </div>

          <div className="flex gap-2">
            <select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value as ProposalStatus)}
              className="rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-xs"
            >
              <option value="pending">pending</option>
              <option value="approved">approved</option>
              <option value="edited_and_approved">edited_and_approved</option>
              <option value="rejected">rejected</option>
            </select>

            <select
              value={typeFilter}
              onChange={(e) => setTypeFilter(e.target.value as "" | ProposalType)}
              className="rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-xs"
            >
              <option value="">All Proposal Types</option>
              <option value="reply_draft">reply_draft</option>
              <option value="crm_entity_extract">crm_entity_extract</option>
              <option value="quote_draft">quote_draft</option>
              <option value="follow_up_suggestion">follow_up_suggestion</option>
              <option value="identity_merge">identity_merge</option>
              <option value="refund_or_reship">refund_or_reship</option>
              <option value="payment_match">payment_match</option>
            </select>
          </div>
        </div>

        {items.length === 0 ? (
          <div className="rounded-lg border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">
            No proposals in {statusFilter} queue.
          </div>
        ) : (
          <div className="space-y-3">
            {items.map((proposal) => {
              const role = principal?.role;
              const canReview =
                role !== "finance" || proposal.proposal_type === "payment_match";
              return (
                <ProposalReviewCard
                  key={proposal.id}
                  proposal={proposal}
                  canReview={canReview}
                  onApprove={(id) => approveMutation.mutate(id)}
                  onEditApprove={(id, edited_payload) =>
                    editApproveMutation.mutate({ id, edited_payload })
                  }
                  onReject={(id, reason) =>
                    rejectMutation.mutate({ id, reason })
                  }
                />
              );
            })}
          </div>
        )}
      </div>
    </AppShell>
  );
}
```

Create `apps/web/src/app/ai/capabilities/page.tsx`:

```tsx
"use client";

import React, { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Lock, Trash2, Upload } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { useAuthSession } from "@/components/providers";
import { bffFetch } from "@/lib/api";
import { canConfigureAiGovernance } from "@/lib/auth-cookie";
import type { AiCapability, AutonomyLevel, PaginatedResponse } from "@/lib/contracts";

interface CapabilityConfigItem {
  capability: AiCapability;
  configured_autonomy: AutonomyLevel;
  max_allowed_autonomy: AutonomyLevel;
}

interface CapabilitiesApiResponse {
  capabilities: CapabilityConfigItem[];
}

interface BillingWalletApiResponse {
  plan_tier: string;
  included_credits_remaining: number;
  topup_credits_balance: number;
  overage_spent_cents?: number;
  overage_spend_cap_cents?: number;
  auto_downgrade_to_l0: boolean;
}

interface KbDocumentItem {
  id: string;
  title: string;
  chunk_count: number;
  status: "pending" | "indexed" | "failed";
  created_at: string;
}

const CAPABILITY_METADATA: Array<{ key: AiCapability; title: string; desc: string }> = [
  {
    key: "cs_support",
    title: "1. AI Customer Support (cs_support)",
    desc: "FAQ citations, pre-sale/after-sale drafting, platform compliance filtering.",
  },
  {
    key: "sales_quoting",
    title: "2. AI Sales & Quoting (sales_quoting)",
    desc: "B2B RFQ extraction, MOQ tier calculation, PI draft proposals.",
  },
  {
    key: "order_logistics",
    title: "3. AI Order & Logistics (order_logistics)",
    desc: "Deterministic WISMO lookup, 17TRACK 72h stagnation outreach.",
  },
  {
    key: "content_studio",
    title: "4. AI Content Studio (content_studio)",
    desc: "Multilingual i18n specs & compliant WhatsApp template drafting.",
  },
  {
    key: "biz_analytics",
    title: "5. AI Business Analytics (biz_analytics)",
    desc: "Read-only CVP margin diagnostics and inventory turnover Q&A.",
  },
  {
    key: "finance_recon",
    title: "6. AI Finance Recon (finance_recon)",
    desc: "T/T slip OCR matching proposals; cash settlement strictly L0 human-only.",
  },
];

const AUTONOMY_LEVELS: AutonomyLevel[] = ["L0_copilot", "L1_proposal", "L2_auto"];

export default function AiCapabilitiesPage() {
  const { principal } = useAuthSession();
  const queryClient = useQueryClient();
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [docTitle, setDocTitle] = useState("");

  const capabilitiesQuery = useQuery({
    queryKey: ["ai", "capabilities"],
    queryFn: () => bffFetch<CapabilitiesApiResponse>("/v1/ai/capabilities"),
  });

  const walletQuery = useQuery({
    queryKey: ["ai", "wallet"],
    queryFn: () => bffFetch<BillingWalletApiResponse>("/v1/billing/wallet"),
  });

  const pendingProposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () =>
      bffFetch<{ total: number }>("/v1/ai/proposals?status=pending&limit=1&offset=0"),
  });

  const kbQuery = useQuery({
    queryKey: ["ai", "kb", "documents"],
    queryFn: () => bffFetch<PaginatedResponse<KbDocumentItem>>("/v1/ai/kb/documents"),
  });

  const updateCapabilityMutation = useMutation({
    mutationFn: ({
      capability,
      autonomy_level,
    }: {
      capability: AiCapability;
      autonomy_level: AutonomyLevel;
    }) =>
      bffFetch("/v1/ai/capabilities", {
        method: "PATCH",
        body: JSON.stringify({ settings: { [capability]: autonomy_level } }),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["ai", "capabilities"] });
    },
  });

  const uploadKbMutation = useMutation({
    mutationFn: async () => {
      if (!selectedFile) return;
      const form = new FormData();
      form.append("file", selectedFile);
      form.append("title", docTitle.trim() || selectedFile.name);
      return bffFetch("/v1/ai/kb/documents", {
        method: "POST",
        body: form,
      });
    },
    onSuccess: () => {
      setSelectedFile(null);
      setDocTitle("");
      void queryClient.invalidateQueries({ queryKey: ["ai", "kb", "documents"] });
    },
  });

  const deleteKbMutation = useMutation({
    mutationFn: (id: string) => bffFetch(`/v1/ai/kb/documents/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["ai", "kb", "documents"] });
    },
  });

  const canConfigure = canConfigureAiGovernance(principal?.role);
  const wallet = walletQuery.data;
  const remainingCredits =
    (wallet?.included_credits_remaining ?? 0) + (wallet?.topup_credits_balance ?? 0);
  const isL0Fallback = Boolean(
    wallet && wallet.auto_downgrade_to_l0 && remainingCredits <= 0
  );

  const levelMap = new Map(
    (capabilitiesQuery.data?.capabilities ?? []).map((c) => [
      c.capability,
      c.configured_autonomy,
    ])
  );

  return (
    <AppShell
      currentPath="/ai/capabilities"
      pendingProposalsCount={pendingProposalsQuery.data?.total ?? 0}
    >
      <div className="mx-auto max-w-5xl space-y-5">
        <div className="rounded-lg border border-slate-200 bg-white p-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h1 className="text-lg font-bold text-slate-900">
                6-Capability AI Governance & Hybrid RAG Knowledge Base
              </h1>
              <p className="text-xs text-slate-500">
                Canonical L0 / L1 / L2 autonomy matrix (no L3 bypass). Live prices and stock stay strictly SQL tool-grounded.
              </p>
            </div>
            {wallet && (
              <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs">
                <div>
                  Plan: <strong>{wallet.plan_tier}</strong> · Remaining Credits:{" "}
                  <strong>{remainingCredits}</strong>
                </div>
              </div>
            )}
          </div>

          {isL0Fallback && (
            <div
              data-testid="wallet-l0-fallback-alert"
              className="mt-3 flex items-center gap-2 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs font-medium text-amber-900"
            >
              <AlertTriangle className="h-4 w-4 shrink-0 text-amber-600" />
              <span>
                Credit wallet depleted — AI automatically degraded to L0_copilot human-only mode. Inbound/outbound messaging remains 100% online.
              </span>
            </div>
          )}
        </div>

        <div className="grid gap-3 md:grid-cols-2">
          {CAPABILITY_METADATA.map((cap) => {
            const currentLevel = levelMap.get(cap.key) ?? "L1_proposal";
            const isFinance = cap.key === "finance_recon";

            return (
              <div
                key={cap.key}
                className="rounded-lg border border-slate-200 bg-white p-4"
              >
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <h2 className="text-sm font-bold text-slate-900">{cap.title}</h2>
                    <p className="mt-0.5 text-xs text-slate-500">{cap.desc}</p>
                  </div>
                  {isFinance && (
                    <span
                      data-testid="finance-recon-l0-lock"
                      className="inline-flex items-center gap-1 rounded bg-rose-100 px-2 py-0.5 text-[11px] font-semibold text-rose-800"
                    >
                      <Lock className="h-3 w-3" />
                      <span>L0 Settlement Lock</span>
                    </span>
                  )}
                </div>

                <div className="mt-3 grid grid-cols-3 gap-1.5">
                  {AUTONOMY_LEVELS.map((lvl) => {
                    const active = currentLevel === lvl;
                    const lockedL2 = isFinance && lvl === "L2_auto";
                    return (
                      <button
                        key={lvl}
                        type="button"
                        data-testid={`cap-btn-${cap.key}-${lvl}`}
                        disabled={!canConfigure || lockedL2 || updateCapabilityMutation.isPending}
                        onClick={() =>
                          updateCapabilityMutation.mutate({
                            capability: cap.key,
                            autonomy_level: lvl,
                          })
                        }
                        className={`rounded-md border px-2 py-1.5 text-xs font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-45 ${
                          active
                            ? "border-indigo-600 bg-indigo-600 text-white"
                            : "border-slate-200 bg-slate-50 text-slate-700 hover:bg-slate-100"
                        }`}
                      >
                        {lvl}
                      </button>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>

        <div className="rounded-lg border border-slate-200 bg-white p-4">
          <h2 className="text-sm font-bold text-slate-900">
            Knowledge Base Documents (BM25 + pgvector HNSW + Cross-Encoder Rerank)
          </h2>

          <div className="mt-3 flex flex-wrap items-center gap-2">
            <input
              type="text"
              value={docTitle}
              onChange={(e) => setDocTitle(e.target.value)}
              placeholder="Document title (optional)"
              className="rounded border border-slate-300 px-2.5 py-1.5 text-xs"
            />
            <input
              type="file"
              accept=".pdf,.md,.txt,.docx"
              onChange={(e) => setSelectedFile(e.target.files?.[0] ?? null)}
              className="text-xs"
            />
            <button
              type="button"
              disabled={!selectedFile || uploadKbMutation.isPending}
              onClick={() => uploadKbMutation.mutate()}
              className="inline-flex items-center gap-1 rounded bg-indigo-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-indigo-700 disabled:opacity-50"
            >
              <Upload className="h-3.5 w-3.5" />
              <span>Upload & Index</span>
            </button>
          </div>

          <div className="mt-4 divide-y divide-slate-100">
            {(kbQuery.data?.items ?? []).map((doc) => (
              <div key={doc.id} className="flex items-center justify-between py-2 text-xs">
                <div>
                  <span className="font-semibold text-slate-900">{doc.title}</span>
                  <span className="ml-2 text-slate-500">{doc.chunk_count} chunks</span>
                  <span className="ml-2 rounded bg-emerald-50 px-1.5 py-0.5 text-emerald-700">
                    {doc.status}
                  </span>
                </div>
                <button
                  type="button"
                  onClick={() => deleteKbMutation.mutate(doc.id)}
                  className="rounded p-1 text-rose-600 hover:bg-rose-50"
                  aria-label={`Delete ${doc.title}`}
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </div>
            ))}
          </div>
        </div>
      </div>
    </AppShell>
  );
}
```

- [ ] **Step 4: Run test & static checks to verify they pass**

Run: `pnpm --filter web test -- src/__tests__/proposal-queue.test.tsx && pnpm --filter web lint`
Expected: PASS (`2 passed` in `proposal-queue.test.tsx` + `0` ESLint errors).

- [ ] **Step 5: Commit**

```bash
git add apps/web/src/components/ai/proposal-review-card.tsx apps/web/src/app/ai/proposals/ apps/web/src/app/ai/capabilities/ apps/web/src/__tests__/proposal-queue.test.tsx
git commit -m "feat(web): add L1 AI proposal approval queue and L0-L2 governance/KB console"
```


---

### Task 5.5: Catalog & Multi-Platform SKU Mapping (`/catalog`) + B2B Pipeline & PI Quote Builder (`/b2b/quotes`)

**Files:**
- Create: `apps/web/src/components/catalog/sku-mapping-drawer.tsx`
- Create: `apps/web/src/app/catalog/page.tsx`
- Create: `apps/web/src/components/b2b/quote-pi-builder.tsx`
- Create: `apps/web/src/app/b2b/quotes/page.tsx`
- Test: `apps/web/src/__tests__/quote-builder.test.tsx`

**Interfaces:**
- Consumes:
  - `GET /api/bff/v1/catalog/variants?q=&limit=20&offset=0`
  - `GET /api/bff/v1/catalog/variants/{id}/mappings`
  - `PATCH /api/bff/v1/catalog/variants/{id}` (`{ b2b_moq_tiers?, min_margin_floor_pct?, retail_price_usd?, lead_time_days? }`)
  - `PUT /api/bff/v1/catalog/mappings/{id}` (`{ bundle_multiplier, sync_inventory_enabled, external_sku }`)
  - `GET /api/bff/v1/b2b/opportunities?limit=50&offset=0`
  - `PATCH /api/bff/v1/b2b/opportunities/{id}` (`{ stage }`)
  - `GET /api/bff/v1/b2b/quotes?opportunity_id=&limit=20&offset=0`
  - `POST /api/bff/v1/b2b/quotes`
  - `POST /api/bff/v1/b2b/quotes/{id}/pdf`
  - `POST /api/bff/v1/b2b/quotes/{id}/accept`
  - `POST /api/bff/v1/b2b/quotes/{id}/reprice-version`
  - `POST /api/bff/v1/b2b/quotes/{id}/convert-to-order`
  - `isPrivilegedCostViewer()` (`apps/web/src/lib/auth-cookie.ts`)
- Produces:
  - `SkuMappingDrawer` (`apps/web/src/components/catalog/sku-mapping-drawer.tsx`)
  - `QuotePiBuilder` + `resolveMoqTierPrice()` (`apps/web/src/components/b2b/quote-pi-builder.tsx`)
  - `/catalog` (`apps/web/src/app/catalog/page.tsx`)
  - `/b2b/quotes` (`apps/web/src/app/b2b/quotes/page.tsx`)

- [ ] **Step 1: Write the failing test (`apps/web/src/__tests__/quote-builder.test.tsx`)**

```tsx
import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nProvider } from "@/lib/i18n";
import { AuthSessionContext } from "@/components/providers";
import CatalogPage from "@/app/catalog/page";
import B2bQuotesPage from "@/app/b2b/quotes/page";
import type { Principal } from "@/lib/contracts";

const SAMPLE_VARIANT = {
  id: "var-1",
  product_id: "prod-1",
  spu_code: "LED-COB-24V",
  category: "LED Strip Lighting",
  hs_code: "94054200",
  certifications: ["CE", "RoHS", "UL"],
  sku: "LED-COB-24V-5M",
  attributes: { cct: "4000K", ip_rating: "IP65" },
  cost_price_base: "45.50",
  base_currency: "CNY",
  min_margin_floor_pct: "15.00",
  retail_price_usd: "29.90",
  b2b_moq_tiers: [
    { min_qty: 50, unit_price_usd: 18.5 },
    { min_qty: 500, unit_price_usd: 14.2 },
  ],
  lead_time_days: 7,
};

function wrapAuth(ui: React.ReactNode, role: Principal["role"]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AuthSessionContext.Provider
        value={{
          principal: {
            user_id: "u1",
            tenant_id: "t1",
            role,
            email: `${role}@qzled.example.com`,
          },
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

describe("Task 5.5 Catalog COGS Masking, SKU Mapping & B2B PI Quote Builder", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("hides cost_price_base from agent in Catalog and SkuMappingDrawer while saving bundle_multiplier", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/ai/proposals")) {
        return new Response(JSON.stringify({ items: [], total: 0, limit: 1, offset: 0, has_more: false }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/catalog/variants/var-1/mappings")) {
        return new Response(
          JSON.stringify({
            variant: SAMPLE_VARIANT,
            mappings: [
              {
                id: "map-1",
                variant_id: "var-1",
                channel_account_id: "ch-shop-1",
                channel_label: "Shopify US Flagship",
                external_product_id: "gid://shopify/Product/991",
                external_sku: "COB-24V-2PACK",
                bundle_multiplier: 2,
                sync_inventory_enabled: true,
              },
            ],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/catalog/variants")) {
        return new Response(
          JSON.stringify({
            items: [SAMPLE_VARIANT],
            total: 1,
            limit: 20,
            offset: 0,
            has_more: false,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/catalog/mappings/map-1") && init?.method === "PUT") {
        return new Response(JSON.stringify({ ok: true }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response("{}", { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);

    wrapAuth(<CatalogPage />, "agent");

    await waitFor(() => {
      expect(screen.getByText("LED-COB-24V-5M")).toBeInTheDocument();
    });

    // Agent must NEVER see cost_price_base (45.50 CNY)
    expect(screen.queryByText(/45\.50/)).toBeNull();
    expect(screen.queryByTestId("catalog-cost-header")).toBeNull();

    fireEvent.click(screen.getByTestId("open-sku-drawer-var-1"));

    await waitFor(() => {
      expect(screen.getByTestId("sku-mapping-drawer")).toBeInTheDocument();
    });
    expect(screen.queryByText(/45\.50/)).toBeNull();

    const multInput = screen.getByTestId("bundle-multiplier-input-map-1");
    fireEvent.change(multInput, { target: { value: "3" } });
    fireEvent.click(screen.getByTestId("save-mapping-btn-map-1"));

    await waitFor(() => {
      const putCall = fetchMock.mock.calls.find(
        (c) => String(c[0]).includes("/catalog/mappings/map-1") && c[1]?.method === "PUT"
      );
      expect(putCall).toBeDefined();
      expect(JSON.parse(String(putCall?.[1]?.body))).toMatchObject({ bundle_multiplier: 3 });
    });
  });

  it("auto-computes MOQ tier pricing, shows Kanban stages, and converts accepted quote 1-Click", async () => {
    const futureIso = new Date(Date.now() + 1000 * 60 * 60 * 24).toISOString();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/ai/proposals")) {
        return new Response(JSON.stringify({ items: [], total: 0, limit: 1, offset: 0, has_more: false }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/catalog/variants")) {
        return new Response(
          JSON.stringify({ items: [SAMPLE_VARIANT], total: 1, limit: 20, offset: 0, has_more: false }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/b2b/opportunities")) {
        return new Response(
          JSON.stringify({
            items: [
              {
                id: "opp-1",
                title: "Berlin Lighting 500pcs COB Strip",
                company_name: "Berlin Lighting GmbH",
                stage: "quoting",
                expected_amount: "7100.00",
                currency: "USD",
              },
            ],
            total: 1,
            limit: 50,
            offset: 0,
            has_more: false,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/b2b/quotes/q-1/convert-to-order") && init?.method === "POST") {
        return new Response(
          JSON.stringify({ order: { id: "ord-99", order_no: "ORD-B2B-99" } }),
          {
            status: 201,
            headers: { "Content-Type": "application/json" },
          }
        );
      }
      if (url.includes("/b2b/quotes")) {
        return new Response(
          JSON.stringify({
            items: [
              {
                id: "q-1",
                opportunity_id: "opp-1",
                quote_no: "PI-202611-0042",
                version: 1,
                is_proforma_invoice: true,
                incoterm: "FOB",
                port_of_loading: "Shenzhen",
                port_of_destination: "Hamburg",
                payment_terms: "30% T/T deposit, 70% before shipment",
                base_currency: "CNY",
                quote_currency: "USD",
                fx_rate_snapshot: "7.120000",
                fx_locked_until: futureIso,
                freight_fee: "250.00",
                insurance_fee: "0.00",
                customs_duty_estimate: "0.00",
                total_amount: "7350.00",
                pdf_r2_key: "tenants/t1/quotes/PI-202611-0042-v1.pdf",
                download_url: "https://minio.local/erp-assets/PI-202611-0042-v1.pdf",
                status: "accepted",
                items: [
                  {
                    id: "qi-1",
                    variant_id: "var-1",
                    sku_snapshot: "LED-COB-24V-5M",
                    quantity: 500,
                    moq_tier_applied: 500,
                    unit_price_quote_ccy: "14.2000",
                    line_total_quote_ccy: "7100.00",
                  },
                ],
              },
            ],
            total: 1,
            limit: 20,
            offset: 0,
            has_more: false,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      return new Response("{}", { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);

    wrapAuth(<B2bQuotesPage />, "manager");

    await waitFor(() => {
      expect(screen.getByText("Berlin Lighting 500pcs COB Strip")).toBeInTheDocument();
      expect(screen.getByText("PI-202611-0042")).toBeInTheDocument();
    });

    for (const stage of ["discovery", "sampling", "quoting", "negotiating", "closed_won"]) {
      expect(screen.getByTestId(`kanban-col-${stage}`)).toBeInTheDocument();
    }

    // Verify MOQ auto-price preview calculator
    const qtyInput = screen.getByTestId("moq-preview-qty-input");
    fireEvent.change(qtyInput, { target: { value: "60" } });
    expect(screen.getByTestId("moq-preview-price")).toHaveTextContent("$18.50");

    fireEvent.change(qtyInput, { target: { value: "500" } });
    expect(screen.getByTestId("moq-preview-price")).toHaveTextContent("$14.20");

    // Verify 1-Click Convert to Order
    fireEvent.click(screen.getByTestId("convert-quote-btn-q-1"));
    await waitFor(() => {
      expect(screen.getByText(/ORD-B2B-99/)).toBeInTheDocument();
    });
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm --filter web test -- src/__tests__/quote-builder.test.tsx`
Expected: FAIL (`Cannot find module '@/app/catalog/page'`).

- [ ] **Step 3: Write complete implementation**

Create `apps/web/src/components/catalog/sku-mapping-drawer.tsx`:

```tsx
"use client";

import React, { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2, X, Lock } from "lucide-react";
import { bffFetch } from "@/lib/api";
import { isPrivilegedCostViewer } from "@/lib/auth-cookie";
import { formatMoney, type UserRole } from "@/lib/contracts";

export interface MoqTier {
  min_qty: number;
  unit_price_usd: number;
}

export interface CatalogVariantItem {
  id: string;
  product_id: string;
  spu_code: string;
  category: string;
  hs_code?: string | null;
  certifications: string[];
  sku: string;
  attributes: Record<string, string>;
  cost_price_base?: string | null;
  base_currency: string;
  min_margin_floor_pct: string;
  retail_price_usd: string;
  b2b_moq_tiers: MoqTier[];
  lead_time_days: number;
}

export interface PlatformSkuMappingItem {
  id: string;
  variant_id: string;
  channel_account_id: string;
  channel_label?: string;
  external_product_id: string;
  external_sku: string;
  bundle_multiplier: number;
  sync_inventory_enabled: boolean;
}

export function SkuMappingDrawer({
  variantId,
  role,
  onClose,
}: {
  variantId: string;
  role: UserRole | undefined;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const canViewCost = isPrivilegedCostViewer(role);

  const detailQuery = useQuery({
    queryKey: ["catalog", "variant", variantId, "mappings"],
    queryFn: () =>
      bffFetch<{ variant: CatalogVariantItem; mappings: PlatformSkuMappingItem[] }>(
        `/v1/catalog/variants/${variantId}/mappings`
      ),
  });

  const [tiers, setTiers] = useState<MoqTier[]>([]);
  const [multipliers, setMultipliers] = useState<Record<string, number>>({});

  useEffect(() => {
    if (detailQuery.data) {
      setTiers(detailQuery.data.variant.b2b_moq_tiers ?? []);
      const nextMult: Record<string, number> = {};
      for (const m of detailQuery.data.mappings) {
        nextMult[m.id] = m.bundle_multiplier;
      }
      setMultipliers(nextMult);
    }
  }, [detailQuery.data]);

  const saveTiersMutation = useMutation({
    mutationFn: () =>
      bffFetch(`/v1/catalog/variants/${variantId}`, {
        method: "PATCH",
        body: JSON.stringify({ b2b_moq_tiers: tiers }),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["catalog", "variants"] });
      void queryClient.invalidateQueries({
        queryKey: ["catalog", "variant", variantId, "mappings"],
      });
    },
  });

  const saveMappingMutation = useMutation({
    mutationFn: (mapping: PlatformSkuMappingItem) =>
      bffFetch(`/v1/catalog/mappings/${mapping.id}`, {
        method: "PUT",
        body: JSON.stringify({
          external_sku: mapping.external_sku,
          bundle_multiplier: multipliers[mapping.id] ?? mapping.bundle_multiplier,
          sync_inventory_enabled: mapping.sync_inventory_enabled,
        }),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: ["catalog", "variant", variantId, "mappings"],
      });
    },
  });

  const variant = detailQuery.data?.variant;
  const mappings = detailQuery.data?.mappings ?? [];

  return (
    <div
      data-testid="sku-mapping-drawer"
      className="fixed inset-y-0 right-0 z-50 flex w-full max-w-lg flex-col border-l border-slate-200 bg-white shadow-2xl"
    >
      <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
        <div>
          <h3 className="text-sm font-bold text-slate-900">
            SKU Master & Multi-Platform Mapping
          </h3>
          {variant && <p className="text-xs text-slate-500">{variant.sku}</p>}
        </div>
        <button
          type="button"
          onClick={onClose}
          className="rounded p-1 text-slate-500 hover:bg-slate-100"
          aria-label="Close SKU drawer"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="flex-1 space-y-5 overflow-y-auto p-4 text-xs">
        {variant && (
          <div className="rounded-md border border-slate-200 bg-slate-50 p-3 space-y-1.5">
            <div className="flex justify-between">
              <span className="text-slate-500">Retail Price (USD):</span>
              <strong>{formatMoney(variant.retail_price_usd, "USD")}</strong>
            </div>
            <div className="flex justify-between">
              <span className="text-slate-500">Min Margin Floor:</span>
              <strong>{variant.min_margin_floor_pct}%</strong>
            </div>
            {canViewCost ? (
              <div className="flex justify-between">
                <span className="text-slate-500">Base Cost ({variant.base_currency}):</span>
                <strong>{formatMoney(variant.cost_price_base, variant.base_currency)}</strong>
              </div>
            ) : (
              <div className="flex items-center gap-1 text-slate-500">
                <Lock className="h-3.5 w-3.5" />
                <span>Base manufacturing cost hidden for Agent role</span>
              </div>
            )}
          </div>
        )}

        <div>
          <div className="flex items-center justify-between">
            <h4 className="font-bold text-slate-900">B2B MOQ Tier Pricing (USD)</h4>
            <button
              type="button"
              onClick={() =>
                setTiers((prev) => [...prev, { min_qty: 100, unit_price_usd: 15.0 }])
              }
              className="inline-flex items-center gap-1 rounded border border-slate-300 px-2 py-1 font-medium hover:bg-slate-100"
            >
              <Plus className="h-3 w-3" />
              <span>Add Tier</span>
            </button>
          </div>

          <div className="mt-2 space-y-2">
            {tiers.map((tier, idx) => (
              <div key={idx} className="flex items-center gap-2">
                <label className="text-slate-500">Min Qty</label>
                <input
                  type="number"
                  min={1}
                  value={tier.min_qty}
                  onChange={(e) => {
                    const next = [...tiers];
                    next[idx] = { ...tier, min_qty: Number(e.target.value) || 1 };
                    setTiers(next);
                  }}
                  className="w-24 rounded border border-slate-300 px-2 py-1"
                />
                <label className="text-slate-500">Unit $</label>
                <input
                  type="number"
                  step="0.01"
                  value={tier.unit_price_usd}
                  onChange={(e) => {
                    const next = [...tiers];
                    next[idx] = { ...tier, unit_price_usd: Number(e.target.value) || 0 };
                    setTiers(next);
                  }}
                  className="w-24 rounded border border-slate-300 px-2 py-1"
                />
                <button
                  type="button"
                  onClick={() => setTiers((prev) => prev.filter((_, i) => i !== idx))}
                  className="rounded p-1 text-rose-600 hover:bg-rose-50"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </div>
            ))}
          </div>

          <button
            type="button"
            onClick={() => saveTiersMutation.mutate()}
            className="mt-2 rounded bg-indigo-600 px-3 py-1.5 font-semibold text-white hover:bg-indigo-700"
          >
            Save MOQ Tiers
          </button>
        </div>

        <div>
          <h4 className="font-bold text-slate-900">
            Platform SKU Mappings & Bundle Multiplier
          </h4>
          <p className="mt-0.5 text-[11px] text-slate-500">
            Bundle multiplier deducts N master SKUs per 1 storefront listing unit sold (e.g. 2-pack = 2).
          </p>

          <div className="mt-2 space-y-2">
            {mappings.map((mapping) => (
              <div
                key={mapping.id}
                className="rounded-md border border-slate-200 p-2.5 space-y-2"
              >
                <div className="flex items-center justify-between font-semibold text-slate-800">
                  <span>{mapping.channel_label ?? mapping.channel_account_id}</span>
                  <span className="rounded bg-slate-100 px-1.5 py-0.5">{mapping.external_sku}</span>
                </div>
                <div className="flex items-center gap-2">
                  <label className="text-slate-600">bundle_multiplier:</label>
                  <input
                    type="number"
                    min={1}
                    data-testid={`bundle-multiplier-input-${mapping.id}`}
                    value={multipliers[mapping.id] ?? mapping.bundle_multiplier}
                    onChange={(e) =>
                      setMultipliers((prev) => ({
                        ...prev,
                        [mapping.id]: Math.max(1, Number(e.target.value) || 1),
                      }))
                    }
                    className="w-20 rounded border border-slate-300 px-2 py-1"
                  />
                  <button
                    type="button"
                    data-testid={`save-mapping-btn-${mapping.id}`}
                    onClick={() => saveMappingMutation.mutate(mapping)}
                    className="rounded bg-slate-900 px-2.5 py-1 font-semibold text-white hover:bg-slate-800"
                  >
                    Save
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
```

Create `apps/web/src/app/catalog/page.tsx`:

```tsx
"use client";

import React, { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AppShell } from "@/components/app-shell";
import { useAuthSession } from "@/components/providers";
import {
  SkuMappingDrawer,
  type CatalogVariantItem,
} from "@/components/catalog/sku-mapping-drawer";
import { bffFetch } from "@/lib/api";
import { isPrivilegedCostViewer } from "@/lib/auth-cookie";
import { formatMoney, type PaginatedResponse } from "@/lib/contracts";

export default function CatalogPage() {
  const { principal } = useAuthSession();
  const [search, setSearch] = useState("");
  const [selectedVariantId, setSelectedVariantId] = useState<string | null>(null);

  const canViewCost = isPrivilegedCostViewer(principal?.role);

  const pendingProposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () =>
      bffFetch<{ total: number }>("/v1/ai/proposals?status=pending&limit=1&offset=0"),
  });

  const variantsQuery = useQuery({
    queryKey: ["catalog", "variants", search],
    queryFn: () =>
      bffFetch<PaginatedResponse<CatalogVariantItem>>(
        `/v1/catalog/variants?q=${encodeURIComponent(search)}&limit=50&offset=0`
      ),
  });

  const variants = variantsQuery.data?.items ?? [];

  return (
    <AppShell
      currentPath="/catalog"
      pendingProposalsCount={pendingProposalsQuery.data?.total ?? 0}
    >
      <div className="mx-auto max-w-6xl space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="text-lg font-bold text-slate-900">
              Unified Product Catalog & Platform SKU Mappings
            </h1>
            <p className="text-xs text-slate-500">
              Single master SKU source for B2B MOQ tier quoting and B2C storefront bundle deductions.
            </p>
          </div>
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search SPU, SKU, or HS code..."
            className="w-64 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs"
          />
        </div>

        <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
          <table className="w-full text-left text-xs">
            <thead className="border-b border-slate-200 bg-slate-50 font-semibold text-slate-600">
              <tr>
                <th className="px-3 py-2.5">SKU / SPU</th>
                <th className="px-3 py-2.5">Category & HS</th>
                <th className="px-3 py-2.5">Certifications</th>
                <th className="px-3 py-2.5">Retail (USD)</th>
                {canViewCost && (
                  <th data-testid="catalog-cost-header" className="px-3 py-2.5">
                    Base Cost
                  </th>
                )}
                <th className="px-3 py-2.5">MOQ Tiers</th>
                <th className="px-3 py-2.5">Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {variants.map((v) => (
                <tr key={v.id} className="hover:bg-slate-50">
                  <td className="px-3 py-2.5">
                    <div className="font-semibold text-slate-900">{v.sku}</div>
                    <div className="text-[11px] text-slate-500">{v.spu_code}</div>
                  </td>
                  <td className="px-3 py-2.5">
                    <div>{v.category}</div>
                    {v.hs_code && <div className="text-[11px] text-slate-500">HS {v.hs_code}</div>}
                  </td>
                  <td className="px-3 py-2.5">
                    <div className="flex flex-wrap gap-1">
                      {v.certifications.map((cert) => (
                        <span
                          key={cert}
                          className="rounded bg-emerald-50 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700"
                        >
                          {cert}
                        </span>
                      ))}
                    </div>
                  </td>
                  <td className="px-3 py-2.5 font-medium">
                    {formatMoney(v.retail_price_usd, "USD")}
                  </td>
                  {canViewCost && (
                    <td className="px-3 py-2.5 font-medium text-slate-700">
                      {formatMoney(v.cost_price_base, v.base_currency)}
                    </td>
                  )}
                  <td className="px-3 py-2.5">
                    {v.b2b_moq_tiers.map((t) => `${t.min_qty}+ @ $${t.unit_price_usd}`).join(" | ")}
                  </td>
                  <td className="px-3 py-2.5">
                    <button
                      type="button"
                      data-testid={`open-sku-drawer-${v.id}`}
                      onClick={() => setSelectedVariantId(v.id)}
                      className="rounded border border-indigo-300 bg-indigo-50 px-2.5 py-1 font-semibold text-indigo-700 hover:bg-indigo-100"
                    >
                      Configure Mappings
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {selectedVariantId && (
        <SkuMappingDrawer
          variantId={selectedVariantId}
          role={principal?.role}
          onClose={() => setSelectedVariantId(null)}
        />
      )}
    </AppShell>
  );
}
```

Create `apps/web/src/components/b2b/quote-pi-builder.tsx`:

```tsx
"use client";

import React, { useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Clock, FileDown, CheckCircle2, RefreshCw, ArrowRightCircle } from "lucide-react";
import { bffFetch } from "@/lib/api";
import { formatMoney, type Incoterm, type QuoteStatus } from "@/lib/contracts";
import type { CatalogVariantItem, MoqTier } from "@/components/catalog/sku-mapping-drawer";

export interface QuoteItemView {
  id: string;
  variant_id: string;
  sku_snapshot: string;
  quantity: number;
  moq_tier_applied?: number | null;
  unit_price_quote_ccy: string;
  line_total_quote_ccy: string;
}

export interface QuoteRecord {
  id: string;
  opportunity_id: string;
  quote_no: string;
  version: number;
  is_proforma_invoice: boolean;
  incoterm: Incoterm;
  port_of_loading?: string | null;
  port_of_destination?: string | null;
  payment_terms: string;
  base_currency: string;
  quote_currency: string;
  fx_rate_snapshot: string;
  fx_locked_until: string;
  freight_fee: string;
  insurance_fee: string;
  customs_duty_estimate: string;
  total_amount: string;
  pdf_r2_key?: string | null;
  download_url?: string | null;
  status: QuoteStatus;
  converted_order_id?: string | null;
  items: QuoteItemView[];
}

export function resolveMoqTierPrice(
  tiers: MoqTier[],
  qty: number,
  fallbackPriceUsd: number
): { tierMinQty: number; unitPriceUsd: number } {
  const sorted = [...tiers].sort((a, b) => b.min_qty - a.min_qty);
  const matched = sorted.find((t) => qty >= t.min_qty);
  if (matched) {
    return { tierMinQty: matched.min_qty, unitPriceUsd: matched.unit_price_usd };
  }
  return { tierMinQty: 1, unitPriceUsd: fallbackPriceUsd };
}

export function formatFxCountdown(fxLockedUntilIso: string): {
  expired: boolean;
  label: string;
} {
  const ms = new Date(fxLockedUntilIso).getTime() - Date.now();
  if (Number.isNaN(ms) || ms <= 0) {
    return { expired: true, label: "FX Lock Expired — Renewal Required" };
  }
  const totalMinutes = Math.floor(ms / 60_000);
  const hours = Math.floor(totalMinutes / 60);
  const mins = totalMinutes % 60;
  return { expired: false, label: `FX Locked: ${hours}h ${mins}m remaining` };
}

export function QuotePiBuilder({
  quotes,
  variants,
}: {
  quotes: QuoteRecord[];
  variants: CatalogVariantItem[];
}) {
  const queryClient = useQueryClient();
  const [selectedQuoteId, setSelectedQuoteId] = useState<string>(quotes[0]?.id ?? "");
  const [previewVariantId, setPreviewVariantId] = useState<string>(variants[0]?.id ?? "");
  const [previewQty, setPreviewQty] = useState<number>(500);
  const [previewIncoterm, setPreviewIncoterm] = useState<Incoterm>("FOB");
  const [convertedBanner, setConvertedBanner] = useState<string | null>(null);
  const [pdfPreviewUrl, setPdfPreviewUrl] = useState<string | null>(null);

  const activeQuote = quotes.find((q) => q.id === selectedQuoteId) ?? quotes[0] ?? null;
  const activeVariant = variants.find((v) => v.id === previewVariantId) ?? variants[0] ?? null;

  const moqPreview = useMemo(() => {
    if (!activeVariant) return null;
    return resolveMoqTierPrice(
      activeVariant.b2b_moq_tiers,
      previewQty,
      Number(activeVariant.retail_price_usd)
    );
  }, [activeVariant, previewQty]);

  const pdfMutation = useMutation({
    mutationFn: (quoteId: string) =>
      bffFetch<{ download_url: string }>(`/v1/b2b/quotes/${quoteId}/pdf`, {
        method: "POST",
      }),
    onSuccess: (res) => {
      setPdfPreviewUrl(res.download_url);
      void queryClient.invalidateQueries({ queryKey: ["b2b", "quotes"] });
    },
  });

  const acceptMutation = useMutation({
    mutationFn: (quoteId: string) =>
      bffFetch(`/v1/b2b/quotes/${quoteId}/accept`, { method: "POST" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["b2b", "quotes"] });
    },
  });

  const renewFxMutation = useMutation({
    mutationFn: (quoteId: string) =>
      bffFetch(`/v1/b2b/quotes/${quoteId}/reprice-version`, {
        method: "POST",
        body: JSON.stringify({
          fx_rate_snapshot: activeQuote?.fx_rate_snapshot ?? "7.120000",
          fx_locked_until: new Date(Date.now() + 1000 * 60 * 60 * 48).toISOString(),
        }),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["b2b", "quotes"] });
    },
  });

  const convertMutation = useMutation({
    mutationFn: (quoteId: string) =>
      bffFetch<{ order: { id: string; order_no: string } }>(
        `/v1/b2b/quotes/${quoteId}/convert-to-order`,
        { method: "POST" }
      ),
    onSuccess: (res) => {
      setConvertedBanner(`Converted 1-Click to Order ${res.order.order_no} (${res.order.id})`);
      void queryClient.invalidateQueries({ queryKey: ["b2b", "quotes"] });
      void queryClient.invalidateQueries({ queryKey: ["orders"] });
    },
  });

  return (
    <div className="grid gap-4 lg:grid-cols-3">
      <div className="rounded-lg border border-slate-200 bg-white p-4 text-xs space-y-3">
        <h3 className="text-sm font-bold text-slate-900">MOQ Tier Auto-Price Calculator</h3>
        <div>
          <label className="block font-medium text-slate-600">Select SKU</label>
          <select
            value={activeVariant?.id ?? ""}
            onChange={(e) => setPreviewVariantId(e.target.value)}
            className="mt-1 w-full rounded border border-slate-300 px-2 py-1.5"
          >
            {variants.map((v) => (
              <option key={v.id} value={v.id}>
                {v.sku}
              </option>
            ))}
          </select>
        </div>
        <div className="grid grid-cols-2 gap-2">
          <div>
            <label className="block font-medium text-slate-600">Order Qty</label>
            <input
              type="number"
              min={1}
              data-testid="moq-preview-qty-input"
              value={previewQty}
              onChange={(e) => setPreviewQty(Math.max(1, Number(e.target.value) || 1))}
              className="mt-1 w-full rounded border border-slate-300 px-2 py-1.5"
            />
          </div>
          <div>
            <label className="block font-medium text-slate-600">Incoterm</label>
            <select
              value={previewIncoterm}
              onChange={(e) => setPreviewIncoterm(e.target.value as Incoterm)}
              className="mt-1 w-full rounded border border-slate-300 px-2 py-1.5"
            >
              <option value="EXW">EXW</option>
              <option value="FOB">FOB</option>
              <option value="CIF">CIF</option>
              <option value="DDP">DDP</option>
              <option value="DAP">DAP</option>
            </select>
          </div>
        </div>
        {moqPreview && (
          <div className="rounded-md border border-indigo-200 bg-indigo-50 p-3 space-y-1">
            <div className="text-slate-700">
              Applied Tier: <strong>MOQ {moqPreview.tierMinQty}+</strong> ({previewIncoterm})
            </div>
            <div data-testid="moq-preview-price" className="text-base font-bold text-indigo-900">
              {formatMoney(moqPreview.unitPriceUsd, "USD")} / pc
            </div>
            <div className="text-slate-600">
              Est. Subtotal: {formatMoney(moqPreview.unitPriceUsd * previewQty, "USD")}
            </div>
          </div>
        )}
      </div>

      <div className="lg:col-span-2 rounded-lg border border-slate-200 bg-white p-4 text-xs space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3 className="text-sm font-bold text-slate-900">Versioned Proforma Invoice (PI)</h3>
          <select
            value={activeQuote?.id ?? ""}
            onChange={(e) => setSelectedQuoteId(e.target.value)}
            className="rounded border border-slate-300 px-2 py-1 text-xs"
          >
            {quotes.map((q) => (
              <option key={q.id} value={q.id}>
                {q.quote_no} (v{q.version} · {q.status})
              </option>
            ))}
          </select>
        </div>

        {convertedBanner && (
          <div className="rounded border border-emerald-300 bg-emerald-50 px-3 py-2 font-semibold text-emerald-900">
            {convertedBanner}
          </div>
        )}

        {activeQuote ? (
          (() => {
            const fxState = formatFxCountdown(activeQuote.fx_locked_until);
            const canConvert = activeQuote.status === "accepted" && !fxState.expired;
            const downloadHref = pdfPreviewUrl ?? activeQuote.download_url ?? null;

            return (
              <div className="space-y-3">
                <div className="flex flex-wrap items-center justify-between gap-2 rounded bg-slate-50 p-2.5">
                  <div>
                    <span className="text-sm font-bold text-slate-900">{activeQuote.quote_no}</span>
                    <span className="ml-2 rounded bg-slate-200 px-1.5 py-0.5">
                      v{activeQuote.version}
                    </span>
                    <span className="ml-2 rounded bg-indigo-100 px-1.5 py-0.5 font-semibold text-indigo-800">
                      {activeQuote.incoterm}
                    </span>
                    <span className="ml-2 rounded bg-slate-200 px-1.5 py-0.5 font-semibold">
                      {activeQuote.status}
                    </span>
                  </div>

                  <span
                    data-testid={`fx-lock-badge-${activeQuote.id}`}
                    className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 font-semibold ${
                      fxState.expired
                        ? "bg-rose-100 text-rose-800"
                        : "bg-emerald-100 text-emerald-800"
                    }`}
                  >
                    <Clock className="h-3 w-3" />
                    <span>{fxState.label}</span>
                  </span>
                </div>

                <div className="grid grid-cols-2 gap-2 text-slate-600">
                  <div>
                    Route: {activeQuote.port_of_loading ?? "Shenzhen"} →{" "}
                    {activeQuote.port_of_destination ?? "Buyer Port"}
                  </div>
                  <div>
                    FX Snapshot: 1 {activeQuote.quote_currency} = {activeQuote.fx_rate_snapshot}{" "}
                    {activeQuote.base_currency}
                  </div>
                  <div className="col-span-2">Payment Terms: {activeQuote.payment_terms}</div>
                </div>

                <table className="w-full border border-slate-200 text-left">
                  <thead className="bg-slate-50 text-slate-600">
                    <tr>
                      <th className="px-2 py-1.5">SKU</th>
                      <th className="px-2 py-1.5">Qty</th>
                      <th className="px-2 py-1.5">Unit Price</th>
                      <th className="px-2 py-1.5">Line Total</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {activeQuote.items.map((item) => (
                      <tr key={item.id}>
                        <td className="px-2 py-1.5 font-medium">{item.sku_snapshot}</td>
                        <td className="px-2 py-1.5">{item.quantity}</td>
                        <td className="px-2 py-1.5">
                          {formatMoney(item.unit_price_quote_ccy, activeQuote.quote_currency)}
                        </td>
                        <td className="px-2 py-1.5 font-semibold">
                          {formatMoney(item.line_total_quote_ccy, activeQuote.quote_currency)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>

                <div className="flex flex-wrap items-center justify-between gap-2 border-t border-slate-200 pt-3">
                  <div className="text-sm font-bold text-slate-900">
                    Total: {formatMoney(activeQuote.total_amount, activeQuote.quote_currency)}
                  </div>

                  <div className="flex flex-wrap gap-2">
                    <button
                      type="button"
                      onClick={() => pdfMutation.mutate(activeQuote.id)}
                      className="inline-flex items-center gap-1 rounded border border-slate-300 px-2.5 py-1.5 font-semibold text-slate-700 hover:bg-slate-100"
                    >
                      <FileDown className="h-3.5 w-3.5" />
                      <span>Generate PI PDF</span>
                    </button>

                    {downloadHref && (
                      <a
                        href={downloadHref}
                        target="_blank"
                        rel="noreferrer"
                        className="inline-flex items-center gap-1 rounded border border-indigo-200 bg-indigo-50 px-2.5 py-1.5 font-semibold text-indigo-700"
                      >
                        Preview PDF
                      </a>
                    )}

                    {fxState.expired && (
                      <button
                        type="button"
                        onClick={() => renewFxMutation.mutate(activeQuote.id)}
                        className="inline-flex items-center gap-1 rounded border border-amber-300 bg-amber-50 px-2.5 py-1.5 font-semibold text-amber-900"
                      >
                        <RefreshCw className="h-3.5 w-3.5" />
                        <span>Renew FX Lock</span>
                      </button>
                    )}

                    {activeQuote.status !== "accepted" && activeQuote.status !== "converted" && (
                      <button
                        type="button"
                        onClick={() => acceptMutation.mutate(activeQuote.id)}
                        className="inline-flex items-center gap-1 rounded bg-slate-800 px-2.5 py-1.5 font-semibold text-white"
                      >
                        <CheckCircle2 className="h-3.5 w-3.5" />
                        <span>Mark Accepted</span>
                      </button>
                    )}

                    <button
                      type="button"
                      data-testid={`convert-quote-btn-${activeQuote.id}`}
                      disabled={!canConvert || convertMutation.isPending}
                      onClick={() => convertMutation.mutate(activeQuote.id)}
                      className="inline-flex items-center gap-1 rounded bg-emerald-600 px-3 py-1.5 font-semibold text-white hover:bg-emerald-700 disabled:opacity-50"
                    >
                      <ArrowRightCircle className="h-3.5 w-3.5" />
                      <span>1-Click Convert to Order</span>
                    </button>
                  </div>
                </div>
              </div>
            );
          })()
        ) : (
          <div className="p-4 text-center text-slate-400">No quotes yet.</div>
        )}
      </div>
    </div>
  );
}
```

Create `apps/web/src/app/b2b/quotes/page.tsx`:

```tsx
"use client";

import React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AppShell } from "@/components/app-shell";
import { QuotePiBuilder, type QuoteRecord } from "@/components/b2b/quote-pi-builder";
import type { CatalogVariantItem } from "@/components/catalog/sku-mapping-drawer";
import { bffFetch } from "@/lib/api";
import { formatMoney, type OpportunityStage, type PaginatedResponse } from "@/lib/contracts";

interface OpportunityCard {
  id: string;
  title: string;
  company_name?: string | null;
  stage: OpportunityStage;
  expected_amount: string;
  currency: string;
}

const KANBAN_STAGES: OpportunityStage[] = [
  "discovery",
  "sampling",
  "quoting",
  "negotiating",
  "closed_won",
];

export default function B2bQuotesPage() {
  const queryClient = useQueryClient();

  const pendingProposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () =>
      bffFetch<{ total: number }>("/v1/ai/proposals?status=pending&limit=1&offset=0"),
  });

  const oppQuery = useQuery({
    queryKey: ["b2b", "opportunities"],
    queryFn: () =>
      bffFetch<PaginatedResponse<OpportunityCard>>("/v1/b2b/opportunities?limit=50&offset=0"),
  });

  const quotesQuery = useQuery({
    queryKey: ["b2b", "quotes"],
    queryFn: () => bffFetch<PaginatedResponse<QuoteRecord>>("/v1/b2b/quotes?limit=20&offset=0"),
  });

  const variantsQuery = useQuery({
    queryKey: ["catalog", "variants"],
    queryFn: () => bffFetch<PaginatedResponse<CatalogVariantItem>>("/v1/catalog/variants?limit=50&offset=0"),
  });

  const moveStageMutation = useMutation({
    mutationFn: ({ id, stage }: { id: string; stage: OpportunityStage }) =>
      bffFetch(`/v1/b2b/opportunities/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ stage }),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["b2b", "opportunities"] });
    },
  });

  const opportunities = oppQuery.data?.items ?? [];

  return (
    <AppShell
      currentPath="/b2b/quotes"
      pendingProposalsCount={pendingProposalsQuery.data?.total ?? 0}
    >
      <div className="mx-auto max-w-7xl space-y-5">
        <div>
          <h1 className="text-lg font-bold text-slate-900">
            B2B Pipeline Kanban & Versioned Proforma Invoice (PI) Builder
          </h1>
          <p className="text-xs text-slate-500">
            Track export inquiries from discovery through closed_won, lock FX rates, generate multilingual PDFs, and convert 1-Click to wholesale orders.
          </p>
        </div>

        <div className="grid gap-3 md:grid-cols-5">
          {KANBAN_STAGES.map((stage) => {
            const stageCards = opportunities.filter((o) => o.stage === stage);
            return (
              <div
                key={stage}
                data-testid={`kanban-col-${stage}`}
                className="rounded-lg border border-slate-200 bg-slate-100/70 p-2.5"
              >
                <div className="mb-2 flex items-center justify-between text-xs font-bold uppercase text-slate-700">
                  <span>{stage}</span>
                  <span className="rounded-full bg-white px-1.5 py-0.5 text-[10px]">
                    {stageCards.length}
                  </span>
                </div>
                <div className="space-y-2">
                  {stageCards.map((card) => (
                    <div
                      key={card.id}
                      className="rounded-md border border-slate-200 bg-white p-2.5 text-xs shadow-xs"
                    >
                      <div className="font-semibold text-slate-900">{card.title}</div>
                      {card.company_name && (
                        <div className="mt-0.5 text-[11px] text-slate-500">{card.company_name}</div>
                      )}
                      <div className="mt-1 font-bold text-indigo-700">
                        {formatMoney(card.expected_amount, card.currency)}
                      </div>
                      <select
                        aria-label={`Stage for ${card.title}`}
                        value={card.stage}
                        onChange={(e) =>
                          moveStageMutation.mutate({
                            id: card.id,
                            stage: e.target.value as OpportunityStage,
                          })
                        }
                        className="mt-2 w-full rounded border border-slate-200 px-1.5 py-1 text-[11px]"
                      >
                        {KANBAN_STAGES.map((opt) => (
                          <option key={opt} value={opt}>
                            {opt}
                          </option>
                        ))}
                      </select>
                    </div>
                  ))}
                </div>
              </div>
            );
          })}
        </div>

        <QuotePiBuilder
          quotes={quotesQuery.data?.items ?? []}
          variants={variantsQuery.data?.items ?? []}
        />
      </div>
    </AppShell>
  );
}
```

- [ ] **Step 4: Run test & static checks to verify they pass**

Run: `pnpm --filter web test -- src/__tests__/quote-builder.test.tsx && pnpm --filter web lint`
Expected: PASS (`2 passed` in `quote-builder.test.tsx` + `0` ESLint errors).

- [ ] **Step 5: Commit**

```bash
git add apps/web/src/components/catalog/ apps/web/src/app/catalog/ apps/web/src/components/b2b/ apps/web/src/app/b2b/ apps/web/src/__tests__/quote-builder.test.tsx
git commit -m "feat(web): add catalog COGS RBAC masking, bundle multiplier mapping, and B2B PI builder"
```

---

### Task 5.6: Unified Orders, Inventory Ledger & 17TRACK (`/orders`) + Finance & CVP Margin (`/finance`)

**Files:**
- Create: `apps/web/src/components/orders/shipment-timeline-drawer.tsx`
- Create: `apps/web/src/app/orders/page.tsx`
- Create: `apps/web/src/components/finance/payment-milestone-table.tsx`
- Create: `apps/web/src/app/finance/page.tsx`
- Test: `apps/web/src/__tests__/finance-cvp.test.tsx`

**Interfaces:**
- Consumes:
  - `GET /api/bff/v1/orders?order_mode=&limit=20&offset=0`
  - `GET /api/bff/v1/orders/{id}`
  - `POST /api/bff/v1/orders/{id}/ship` (`{ tracking_number, carrier_code, shipment_mode }`)
  - `POST /api/bff/v1/orders/{id}/shipments/{shipmentId}/refresh-tracking` (`{ cache_level: 0 }`)
  - `GET /api/bff/v1/orders/inventory/balances?variant_id=&limit=20&offset=0`
  - `GET /api/bff/v1/orders/inventory/ledger?variant_id=&limit=20&offset=0`
  - `GET /api/bff/v1/finance/overview`
  - `POST /api/bff/v1/finance/receivables/{id}/payments` (`FormData`)
  - `POST /api/bff/v1/finance/payments/{paymentId}/verify`
  - `canVerifyFinanceSettlement()` (`apps/web/src/lib/auth-cookie.ts`)
- Produces:
  - `ShipmentTimelineDrawer` (`apps/web/src/components/orders/shipment-timeline-drawer.tsx`)
  - `/orders` (`apps/web/src/app/orders/page.tsx`)
  - `PaymentMilestoneTable` (`apps/web/src/components/finance/payment-milestone-table.tsx`)
  - `/finance` (`apps/web/src/app/finance/page.tsx`)

- [ ] **Step 1: Write the failing test (`apps/web/src/__tests__/finance-cvp.test.tsx`)**

```tsx
import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nProvider } from "@/lib/i18n";
import { AuthSessionContext } from "@/components/providers";
import FinancePage from "@/app/finance/page";
import OrdersPage from "@/app/orders/page";
import type { Principal } from "@/lib/contracts";

function wrap(ui: React.ReactNode, role: Principal["role"]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AuthSessionContext.Provider
        value={{
          principal: {
            user_id: "u1",
            tenant_id: "t1",
            role,
            email: `${role}@qzled.example.com`,
          },
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

describe("Task 5.6 Orders 17TRACK + Inventory Ledger & Finance CVP Margin", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("shows 70% unpaid warning, enforces finance/owner verification, and renders CVP waterfall", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/ai/proposals")) {
        return new Response(JSON.stringify({ items: [], total: 0, limit: 1, offset: 0, has_more: false }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/finance/payments/pay-30/verify") && init?.method === "POST") {
        return new Response(
          JSON.stringify({ ok: true, order_fulfillment_status: "in_production" }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/finance/overview")) {
        return new Response(
          JSON.stringify({
            summary: {
              total_gross_revenue_base: "52332.00",
              total_net_margin_base: "16280.00",
              avg_margin_pct: "31.11",
              base_currency: "CNY",
            },
            receivables: [
              {
                id: "rp-30",
                order_id: "ord-b2b-1",
                order_no: "ORD-B2B-2026-01",
                order_mode: "b2b_wholesale",
                milestone_label: "b2b_30_deposit",
                due_amount: "2205.00",
                paid_amount: "2205.00",
                currency: "USD",
                status: "partial",
                shipment_blocked_70_unpaid: true,
                pending_payment_id: "pay-30",
                pending_tx_ref: "TT-20261119-DE",
              },
              {
                id: "rp-70",
                order_id: "ord-b2b-1",
                order_no: "ORD-B2B-2026-01",
                order_mode: "b2b_wholesale",
                milestone_label: "b2b_70_balance",
                due_amount: "5145.00",
                paid_amount: "0.00",
                currency: "USD",
                status: "open",
                shipment_blocked_70_unpaid: true,
                pending_payment_id: null,
                pending_tx_ref: null,
              },
            ],
            cvp_snapshots: [
              {
                order_id: "ord-b2b-1",
                order_no: "ORD-B2B-2026-01",
                base_currency: "CNY",
                gross_revenue_base: "52332.00",
                cogs_base: "22750.00",
                platform_commission_base: "0.00",
                payment_gateway_fee_base: "250.00",
                shipping_and_duty_base: "12800.00",
                channel_msg_cost_base: "12.00",
                ad_attribution_cost_base: "360.00",
                fx_gain_loss_base: "120.00",
                net_contribution_margin_base: "16280.00",
              },
            ],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      return new Response("{}", { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);

    wrap(<FinancePage />, "finance");

    await waitFor(() => {
      expect(screen.getAllByTestId("balance-70-unpaid-warning").length).toBeGreaterThan(0);
    });

    expect(screen.getByTestId("cvp-waterfall-ord-b2b-1")).toHaveTextContent("CN¥16,280.00");
    expect(screen.getByTestId("cvp-waterfall-ord-b2b-1").textContent).not.toContain("[object Object]");

    fireEvent.click(screen.getByTestId("verify-payment-btn-pay-30"));

    await waitFor(() => {
      const verifyCall = fetchMock.mock.calls.find((c) =>
        String(c[0]).includes("/finance/payments/pay-30/verify")
      );
      expect(verifyCall).toBeDefined();
    });
  });

  it("blocks agent on /finance and renders 17TRACK 72h stagnation alert + inventory ledger on /orders", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/ai/proposals")) {
        return new Response(JSON.stringify({ items: [], total: 0, limit: 1, offset: 0, has_more: false }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/orders/inventory/balances")) {
        return new Response(
          JSON.stringify({
            items: [
              {
                warehouse_id: "wh-sz",
                variant_id: "var-1",
                sku: "LED-COB-24V-5M",
                on_hand_qty: 1500,
                reserved_qty: 500,
              },
            ],
            total: 1,
            limit: 20,
            offset: 0,
            has_more: false,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/orders/inventory/ledger")) {
        return new Response(
          JSON.stringify({
            items: [
              {
                id: 101,
                variant_id: "var-1",
                event_type: "reserve_order",
                qty_delta_on_hand: 0,
                qty_delta_reserved: 500,
                reference_type: "order",
                reference_id: "ord-b2b-1",
                idempotency_key: "reserve:ord-b2b-1:var-1",
                created_at: "2026-11-19T08:00:00Z",
              },
            ],
            total: 1,
            limit: 20,
            offset: 0,
            has_more: false,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/orders/ord-b2b-1")) {
        return new Response(
          JSON.stringify({
            id: "ord-b2b-1",
            order_no: "ORD-B2B-2026-01",
            order_mode: "b2b_wholesale",
            fulfillment_status: "in_production",
            payment_status: "deposit_paid",
            currency: "USD",
            total_amount: "7350.00",
            shipments: [
              {
                id: "ship-1",
                tracking_provider: "17track",
                tracking_number: "DHL99887766",
                carrier_code: "DHL",
                shipment_mode: "air_freight",
                standardized_status: "Alert",
                sub_status: "CustomsHold",
                is_stagnant_72h: true,
                tracking_events: [
                  {
                    time_utc: "2026-11-16T06:00:00Z",
                    location: "Frankfurt DE",
                    description: "Awaiting commercial invoice clearance",
                  },
                ],
              },
            ],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/orders")) {
        return new Response(
          JSON.stringify({
            items: [
              {
                id: "ord-b2b-1",
                order_no: "ORD-B2B-2026-01",
                order_mode: "b2b_wholesale",
                customer_name: "Berlin Lighting GmbH",
                fulfillment_status: "in_production",
                payment_status: "deposit_paid",
                currency: "USD",
                total_amount: "7350.00",
                has_72h_stagnation_alert: true,
              },
            ],
            total: 1,
            limit: 20,
            offset: 0,
            has_more: false,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      return new Response("{}", { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);

    // Agent blocked on FinancePage
    const { unmount } = wrap(<FinancePage />, "agent");
    expect(screen.getByTestId("rbac-forbidden-guard")).toBeInTheDocument();
    unmount();

    // OrdersPage renders 70%-unpaid shipment block & 17TRACK 72h stagnation alert
    wrap(<OrdersPage />, "manager");

    await waitFor(() => {
      expect(screen.getByText("ORD-B2B-2026-01")).toBeInTheDocument();
      expect(screen.getByTestId("order-72h-alert-ord-b2b-1")).toBeInTheDocument();
      expect(screen.getByTestId("order-ship-blocked-ord-b2b-1")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId("open-shipment-drawer-ord-b2b-1"));

    await waitFor(() => {
      expect(screen.getByText("DHL99887766")).toBeInTheDocument();
      expect(screen.getByText(/CustomsHold/)).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId("open-inventory-ledger-btn"));
    await waitFor(() => {
      expect(screen.getByText("reserve:ord-b2b-1:var-1")).toBeInTheDocument();
    });
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm --filter web test -- src/__tests__/finance-cvp.test.tsx`
Expected: FAIL (`Cannot find module '@/app/finance/page'`).

- [ ] **Step 3: Write complete implementation**

Create `apps/web/src/components/orders/shipment-timeline-drawer.tsx`:

```tsx
"use client";

import React, { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, RefreshCw, Truck, X, Database } from "lucide-react";
import { bffFetch } from "@/lib/api";
import type { InventoryEventType, PaginatedResponse } from "@/lib/contracts";

interface ShipmentDetail {
  id: string;
  tracking_provider: string;
  tracking_number: string;
  carrier_code?: string | null;
  shipment_mode: string;
  standardized_status: string;
  sub_status?: string | null;
  is_stagnant_72h?: boolean;
  tracking_events: Array<{
    time_utc: string;
    location?: string;
    description: string;
  }>;
}

interface OrderDetailPayload {
  id: string;
  order_no: string;
  order_mode: string;
  fulfillment_status: string;
  payment_status: string;
  shipments: ShipmentDetail[];
}

interface InventoryBalanceItem {
  warehouse_id: string;
  variant_id: string;
  sku: string;
  on_hand_qty: number;
  reserved_qty: number;
}

interface InventoryLedgerItem {
  id: number;
  variant_id: string;
  event_type: InventoryEventType;
  qty_delta_on_hand: number;
  qty_delta_reserved: number;
  reference_type: string;
  reference_id: string;
  idempotency_key: string;
  created_at: string;
}

export function ShipmentTimelineDrawer({
  orderId,
  onClose,
}: {
  orderId: string;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [ledgerModalOpen, setLedgerModalOpen] = useState(false);

  const detailQuery = useQuery({
    queryKey: ["orders", orderId],
    queryFn: () => bffFetch<OrderDetailPayload>(`/v1/orders/${orderId}`),
  });

  const balancesQuery = useQuery({
    queryKey: ["orders", "inventory", "balances"],
    enabled: ledgerModalOpen,
    queryFn: () =>
      bffFetch<PaginatedResponse<InventoryBalanceItem>>("/v1/orders/inventory/balances"),
  });

  const ledgerQuery = useQuery({
    queryKey: ["orders", "inventory", "ledger"],
    enabled: ledgerModalOpen,
    queryFn: () =>
      bffFetch<PaginatedResponse<InventoryLedgerItem>>("/v1/orders/inventory/ledger"),
  });

  const refreshTrackingMutation = useMutation({
    mutationFn: (shipmentId: string) =>
      bffFetch(`/v1/orders/${orderId}/shipments/${shipmentId}/refresh-tracking`, {
        method: "POST",
        body: JSON.stringify({ cache_level: 0 }),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["orders", orderId] });
    },
  });

  const order = detailQuery.data;

  return (
    <div
      data-testid="shipment-timeline-drawer"
      className="fixed inset-y-0 right-0 z-50 flex w-full max-w-lg flex-col border-l border-slate-200 bg-white shadow-2xl"
    >
      <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
        <div>
          <h3 className="text-sm font-bold text-slate-900">
            17TRACK 9+30 Timeline & Inventory Ledger
          </h3>
          {order && <p className="text-xs text-slate-500">{order.order_no}</p>}
        </div>
        <button
          type="button"
          onClick={onClose}
          className="rounded p-1 text-slate-500 hover:bg-slate-100"
          aria-label="Close shipment drawer"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="flex-1 space-y-4 overflow-y-auto p-4 text-xs">
        <div className="flex justify-end">
          <button
            type="button"
            data-testid="open-inventory-ledger-btn"
            onClick={() => setLedgerModalOpen(true)}
            className="inline-flex items-center gap-1.5 rounded border border-slate-300 bg-slate-50 px-2.5 py-1.5 font-semibold text-slate-700 hover:bg-slate-100"
          >
            <Database className="h-3.5 w-3.5" />
            <span>Atomic Inventory Ledger</span>
          </button>
        </div>

        {(order?.shipments ?? []).length === 0 ? (
          <div className="rounded border border-slate-200 p-4 text-center text-slate-400">
            No shipments dispatched yet.
          </div>
        ) : (
          order!.shipments.map((ship) => (
            <div key={ship.id} className="rounded-lg border border-slate-200 p-3 space-y-2.5">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-1.5 font-bold text-slate-900">
                  <Truck className="h-4 w-4 text-indigo-600" />
                  <span>{ship.tracking_number}</span>
                  {ship.carrier_code && (
                    <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px]">
                      {ship.carrier_code}
                    </span>
                  )}
                </div>

                <button
                  type="button"
                  onClick={() => refreshTrackingMutation.mutate(ship.id)}
                  className="inline-flex items-center gap-1 rounded border border-slate-300 px-2 py-1 text-[11px] font-medium hover:bg-slate-100"
                  title="Standard 1-credit cacheLevel=0 refresh"
                >
                  <RefreshCw className="h-3 w-3" />
                  <span>Refresh (cacheLevel=0)</span>
                </button>
              </div>

              <div className="flex flex-wrap items-center gap-1.5">
                <span className="rounded bg-indigo-50 px-2 py-0.5 font-semibold text-indigo-700">
                  {ship.standardized_status}
                </span>
                {ship.sub_status && (
                  <span className="rounded bg-slate-100 px-2 py-0.5 text-slate-700">
                    {ship.sub_status}
                  </span>
                )}
                {ship.is_stagnant_72h && (
                  <span className="inline-flex items-center gap-1 rounded bg-rose-100 px-2 py-0.5 font-semibold text-rose-800">
                    <AlertTriangle className="h-3 w-3" />
                    <span>72h Stagnant / Exception Alert</span>
                  </span>
                )}
              </div>

              <div className="space-y-2 border-l-2 border-slate-200 pl-3">
                {ship.tracking_events.map((ev, idx) => (
                  <div key={idx} className="space-y-0.5">
                    <div className="font-semibold text-slate-800">{ev.description}</div>
                    <div className="text-[11px] text-slate-500">
                      {ev.location ? `${ev.location} · ` : ""}
                      {new Date(ev.time_utc).toLocaleString()}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          ))
        )}
      </div>

      {ledgerModalOpen && (
        <div
          role="dialog"
          aria-modal="true"
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
        >
          <div className="w-full max-w-2xl rounded-lg bg-white p-4 shadow-xl text-xs space-y-3">
            <div className="flex items-center justify-between border-b border-slate-200 pb-2">
              <h4 className="text-sm font-bold text-slate-900">
                Atomic Inventory Balances & Immutable Ledger
              </h4>
              <button
                type="button"
                onClick={() => setLedgerModalOpen(false)}
                className="rounded border border-slate-300 px-2 py-0.5"
              >
                Close
              </button>
            </div>

            <div className="grid gap-2 sm:grid-cols-2">
              {(balancesQuery.data?.items ?? []).map((bal) => (
                <div
                  key={`${bal.warehouse_id}-${bal.variant_id}`}
                  className="rounded border border-slate-200 bg-slate-50 p-2"
                >
                  <div className="font-bold text-slate-900">{bal.sku}</div>
                  <div>
                    On Hand: <strong>{bal.on_hand_qty}</strong> · Reserved:{" "}
                    <strong>{bal.reserved_qty}</strong> · Available:{" "}
                    <strong>{bal.on_hand_qty - bal.reserved_qty}</strong>
                  </div>
                </div>
              ))}
            </div>

            <div className="max-h-64 overflow-y-auto border border-slate-200 rounded">
              <table className="w-full text-left">
                <thead className="bg-slate-50 text-slate-600">
                  <tr>
                    <th className="px-2 py-1.5">Event</th>
                    <th className="px-2 py-1.5">Δ On Hand</th>
                    <th className="px-2 py-1.5">Δ Reserved</th>
                    <th className="px-2 py-1.5">Idempotency Key</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {(ledgerQuery.data?.items ?? []).map((row) => (
                    <tr key={row.id}>
                      <td className="px-2 py-1.5 font-semibold">{row.event_type}</td>
                      <td className="px-2 py-1.5">{row.qty_delta_on_hand}</td>
                      <td className="px-2 py-1.5">{row.qty_delta_reserved}</td>
                      <td className="px-2 py-1.5 font-mono text-[11px]">{row.idempotency_key}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
```

Create `apps/web/src/app/orders/page.tsx`:

```tsx
"use client";

import React, { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Lock, Truck } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { ShipmentTimelineDrawer } from "@/components/orders/shipment-timeline-drawer";
import { bffFetch } from "@/lib/api";
import {
  formatMoney,
  type FulfillmentStatus,
  type OrderMode,
  type PaginatedResponse,
  type PaymentStatus,
} from "@/lib/contracts";

interface OrderSummaryRow {
  id: string;
  order_no: string;
  order_mode: OrderMode;
  customer_name: string;
  fulfillment_status: FulfillmentStatus;
  payment_status: PaymentStatus;
  currency: string;
  total_amount: string;
  has_72h_stagnation_alert?: boolean;
}

export default function OrdersPage() {
  const queryClient = useQueryClient();
  const [modeFilter, setModeFilter] = useState<"" | OrderMode>("");
  const [drawerOrderId, setDrawerOrderId] = useState<string | null>(null);
  const [trackingInputs, setTrackingInputs] = useState<Record<string, string>>({});

  const pendingProposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () =>
      bffFetch<{ total: number }>("/v1/ai/proposals?status=pending&limit=1&offset=0"),
  });

  const ordersQuery = useQuery({
    queryKey: ["orders", modeFilter],
    queryFn: () => {
      const params = new URLSearchParams({ limit: "30", offset: "0" });
      if (modeFilter) params.set("order_mode", modeFilter);
      return bffFetch<PaginatedResponse<OrderSummaryRow>>(`/v1/orders?${params.toString()}`);
    },
  });

  const shipMutation = useMutation({
    mutationFn: ({ orderId, trackingNumber }: { orderId: string; trackingNumber: string }) =>
      bffFetch(`/v1/orders/${orderId}/ship`, {
        method: "POST",
        body: JSON.stringify({
          tracking_number: trackingNumber,
          carrier_code: "DHL",
          shipment_mode: "parcel",
        }),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["orders"] });
    },
  });

  const orders = ordersQuery.data?.items ?? [];

  return (
    <AppShell
      currentPath="/orders"
      pendingProposalsCount={pendingProposalsQuery.data?.total ?? 0}
    >
      <div className="mx-auto max-w-6xl space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="text-lg font-bold text-slate-900">
              Unified Dual-Mode Orders, Inventory Ledger & 17TRACK
            </h1>
            <p className="text-xs text-slate-500">
              B2C retail and B2B wholesale share one atomic inventory ledger; wholesale dispatch enforces 70% balance settlement.
            </p>
          </div>
          <select
            value={modeFilter}
            onChange={(e) => setModeFilter(e.target.value as "" | OrderMode)}
            className="rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-xs"
          >
            <option value="">All Order Modes</option>
            <option value="b2c_retail">b2c_retail</option>
            <option value="b2b_sample">b2b_sample</option>
            <option value="b2b_wholesale">b2b_wholesale</option>
          </select>
        </div>

        <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
          <table className="w-full text-left text-xs">
            <thead className="border-b border-slate-200 bg-slate-50 font-semibold text-slate-600">
              <tr>
                <th className="px-3 py-2.5">Order No</th>
                <th className="px-3 py-2.5">Mode & Buyer</th>
                <th className="px-3 py-2.5">Fulfillment</th>
                <th className="px-3 py-2.5">Payment</th>
                <th className="px-3 py-2.5">Total</th>
                <th className="px-3 py-2.5">17TRACK & Dispatch</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {orders.map((ord) => {
                const blocked70Unpaid =
                  ord.order_mode === "b2b_wholesale" && ord.payment_status !== "paid";
                return (
                  <tr key={ord.id} className="hover:bg-slate-50">
                    <td className="px-3 py-2.5 font-semibold text-slate-900">
                      <div>{ord.order_no}</div>
                      {ord.has_72h_stagnation_alert && (
                        <span
                          data-testid={`order-72h-alert-${ord.id}`}
                          className="mt-1 inline-flex items-center gap-1 rounded bg-rose-100 px-1.5 py-0.5 text-[10px] font-semibold text-rose-800"
                        >
                          <AlertTriangle className="h-3 w-3" />
                          <span>72h Stagnation</span>
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-2.5">
                      <div>{ord.customer_name}</div>
                      <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px]">
                        {ord.order_mode}
                      </span>
                    </td>
                    <td className="px-3 py-2.5">{ord.fulfillment_status}</td>
                    <td className="px-3 py-2.5">
                      <div>{ord.payment_status}</div>
                      {blocked70Unpaid && (
                        <span
                          data-testid={`order-ship-blocked-${ord.id}`}
                          className="mt-1 inline-flex items-center gap-1 rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-semibold text-amber-900"
                        >
                          <Lock className="h-3 w-3" />
                          <span>70% Balance Unpaid</span>
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-2.5 font-semibold">
                      {formatMoney(ord.total_amount, ord.currency)}
                    </td>
                    <td className="px-3 py-2.5">
                      <div className="flex flex-wrap items-center gap-2">
                        <button
                          type="button"
                          data-testid={`open-shipment-drawer-${ord.id}`}
                          onClick={() => setDrawerOrderId(ord.id)}
                          className="inline-flex items-center gap-1 rounded border border-indigo-300 bg-indigo-50 px-2 py-1 font-semibold text-indigo-700"
                        >
                          <Truck className="h-3 w-3" />
                          <span>Timeline & Ledger</span>
                        </button>

                        <input
                          type="text"
                          value={trackingInputs[ord.id] ?? ""}
                          onChange={(e) =>
                            setTrackingInputs((prev) => ({ ...prev, [ord.id]: e.target.value }))
                          }
                          placeholder="Tracking #"
                          disabled={blocked70Unpaid}
                          className="w-28 rounded border border-slate-300 px-2 py-1 disabled:bg-slate-100"
                        />
                        <button
                          type="button"
                          disabled={blocked70Unpaid || !(trackingInputs[ord.id] ?? "").trim()}
                          onClick={() =>
                            shipMutation.mutate({
                              orderId: ord.id,
                              trackingNumber: trackingInputs[ord.id] ?? "",
                            })
                          }
                          className="rounded bg-slate-900 px-2 py-1 font-semibold text-white disabled:opacity-40"
                        >
                          Ship
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {drawerOrderId && (
        <ShipmentTimelineDrawer
          orderId={drawerOrderId}
          onClose={() => setDrawerOrderId(null)}
        />
      )}
    </AppShell>
  );
}
```

Create `apps/web/src/components/finance/payment-milestone-table.tsx`:

```tsx
"use client";

import React, { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, Upload } from "lucide-react";
import { bffFetch } from "@/lib/api";
import { canVerifyFinanceSettlement } from "@/lib/auth-cookie";
import { formatMoney, type UserRole } from "@/lib/contracts";

export interface ReceivableMilestoneItem {
  id: string;
  order_id: string;
  order_no: string;
  order_mode: string;
  milestone_label: string;
  due_amount: string;
  paid_amount: string;
  currency: string;
  status: "open" | "partial" | "settled" | "overdue" | "void";
  shipment_blocked_70_unpaid?: boolean;
  pending_payment_id?: string | null;
  pending_tx_ref?: string | null;
}

export interface OrderProfitSnapshotItem {
  order_id: string;
  order_no: string;
  base_currency: string;
  gross_revenue_base: string;
  cogs_base: string;
  platform_commission_base: string;
  payment_gateway_fee_base: string;
  shipping_and_duty_base: string;
  channel_msg_cost_base: string;
  ad_attribution_cost_base: string;
  fx_gain_loss_base: string;
  net_contribution_margin_base: string;
}

export function PaymentMilestoneTable({
  receivables,
  cvpSnapshots,
  role,
}: {
  receivables: ReceivableMilestoneItem[];
  cvpSnapshots: OrderProfitSnapshotItem[];
  role: UserRole | undefined;
}) {
  const queryClient = useQueryClient();
  const canVerify = canVerifyFinanceSettlement(role);
  const [txRefs, setTxRefs] = useState<Record<string, string>>({});
  const [fxRates, setFxRates] = useState<Record<string, string>>({});
  const [slips, setSlips] = useState<Record<string, File | null>>({});

  const uploadPaymentMutation = useMutation({
    mutationFn: async (rp: ReceivableMilestoneItem) => {
      const form = new FormData();
      form.append("payment_method", "tt_wire");
      form.append("external_tx_ref", txRefs[rp.id] || `TT-${rp.order_no}`);
      form.append("amount_received", rp.due_amount);
      form.append("currency", rp.currency);
      form.append("actual_fx_rate", fxRates[rp.id] || "7.12");
      form.append("gateway_fee", "35.00");
      if (slips[rp.id]) {
        form.append("receipt_file", slips[rp.id]!);
      }
      return bffFetch(`/v1/finance/receivables/${rp.id}/payments`, {
        method: "POST",
        body: form,
      });
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["finance", "overview"] });
    },
  });

  const verifyMutation = useMutation({
    mutationFn: (paymentId: string) =>
      bffFetch(`/v1/finance/payments/${paymentId}/verify`, { method: "POST" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["finance", "overview"] });
      void queryClient.invalidateQueries({ queryKey: ["orders"] });
    },
  });

  return (
    <div className="space-y-6">
      <div className="rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="text-sm font-bold text-slate-900">
          B2B Deposit / Balance Milestones & L0 Cash Verification
        </h2>
        <div className="mt-3 overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="border-b border-slate-200 bg-slate-50 font-semibold text-slate-600">
              <tr>
                <th className="px-3 py-2">Order / Milestone</th>
                <th className="px-3 py-2">Due / Paid</th>
                <th className="px-3 py-2">Status & Guards</th>
                <th className="px-3 py-2">Upload T/T Slip</th>
                <th className="px-3 py-2">L0 Verify</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {receivables.map((rp) => (
                <tr key={rp.id}>
                  <td className="px-3 py-2.5">
                    <div className="font-semibold text-slate-900">{rp.order_no}</div>
                    <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px]">
                      {rp.milestone_label}
                    </span>
                  </td>
                  <td className="px-3 py-2.5">
                    <div>Due: {formatMoney(rp.due_amount, rp.currency)}</div>
                    <div className="text-slate-500">
                      Paid: {formatMoney(rp.paid_amount, rp.currency)}
                    </div>
                  </td>
                  <td className="px-3 py-2.5">
                    <span className="rounded bg-slate-100 px-1.5 py-0.5 font-medium">
                      {rp.status}
                    </span>
                    {rp.shipment_blocked_70_unpaid && (
                      <div
                        data-testid="balance-70-unpaid-warning"
                        className="mt-1 inline-flex items-center gap-1 rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-semibold text-amber-900"
                      >
                        <AlertTriangle className="h-3 w-3" />
                        <span>70% Balance Unpaid — Shipment Blocked</span>
                      </div>
                    )}
                  </td>
                  <td className="px-3 py-2.5">
                    {rp.status !== "settled" && (
                      <div className="flex flex-wrap items-center gap-1.5">
                        <input
                          type="text"
                          value={txRefs[rp.id] ?? ""}
                          onChange={(e) =>
                            setTxRefs((prev) => ({ ...prev, [rp.id]: e.target.value }))
                          }
                          placeholder="T/T Ref"
                          className="w-24 rounded border border-slate-300 px-1.5 py-1"
                        />
                        <input
                          type="text"
                          value={fxRates[rp.id] ?? "7.12"}
                          onChange={(e) =>
                            setFxRates((prev) => ({ ...prev, [rp.id]: e.target.value }))
                          }
                          placeholder="FX"
                          className="w-16 rounded border border-slate-300 px-1.5 py-1"
                        />
                        <input
                          type="file"
                          onChange={(e) =>
                            setSlips((prev) => ({
                              ...prev,
                              [rp.id]: e.target.files?.[0] ?? null,
                            }))
                          }
                          className="w-32 text-[10px]"
                        />
                        <button
                          type="button"
                          onClick={() => uploadPaymentMutation.mutate(rp)}
                          className="inline-flex items-center gap-1 rounded border border-slate-300 px-2 py-1 font-medium hover:bg-slate-100"
                        >
                          <Upload className="h-3 w-3" />
                          <span>Record</span>
                        </button>
                      </div>
                    )}
                  </td>
                  <td className="px-3 py-2.5">
                    {rp.pending_payment_id && (
                      <button
                        type="button"
                        data-testid={`verify-payment-btn-${rp.pending_payment_id}`}
                        disabled={!canVerify || verifyMutation.isPending}
                        onClick={() => verifyMutation.mutate(rp.pending_payment_id!)}
                        className="inline-flex items-center gap-1 rounded bg-emerald-600 px-2.5 py-1 font-semibold text-white hover:bg-emerald-700 disabled:opacity-40"
                      >
                        <CheckCircle2 className="h-3.5 w-3.5" />
                        <span>Verify ({rp.pending_tx_ref ?? "T/T"})</span>
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="text-sm font-bold text-slate-900">
          CVP Order Contribution Margin Waterfall
        </h2>
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          {cvpSnapshots.map((snap) => {
            const rev = Number(snap.gross_revenue_base);
            const net = Number(snap.net_contribution_margin_base);
            const pct = rev > 0 ? ((net / rev) * 100).toFixed(2) : "0.00";
            return (
              <div
                key={snap.order_id}
                data-testid={`cvp-waterfall-${snap.order_id}`}
                className="rounded-md border border-slate-200 bg-slate-50 p-3 text-xs space-y-1"
              >
                <div className="flex items-center justify-between border-b border-slate-200 pb-1.5 font-bold text-slate-900">
                  <span>{snap.order_no}</span>
                  <span className="text-emerald-700">
                    Net Margin: {formatMoney(snap.net_contribution_margin_base, snap.base_currency)} ({pct}%)
                  </span>
                </div>
                <div className="flex justify-between">
                  <span>+ Gross Revenue</span>
                  <span>{formatMoney(snap.gross_revenue_base, snap.base_currency)}</span>
                </div>
                <div className="flex justify-between text-rose-700">
                  <span>- COGS</span>
                  <span>-{formatMoney(snap.cogs_base, snap.base_currency)}</span>
                </div>
                <div className="flex justify-between text-rose-700">
                  <span>- Platform Commission</span>
                  <span>-{formatMoney(snap.platform_commission_base, snap.base_currency)}</span>
                </div>
                <div className="flex justify-between text-rose-700">
                  <span>- Payment Gateway Fee</span>
                  <span>-{formatMoney(snap.payment_gateway_fee_base, snap.base_currency)}</span>
                </div>
                <div className="flex justify-between text-rose-700">
                  <span>- Shipping & DDP Duty</span>
                  <span>-{formatMoney(snap.shipping_and_duty_base, snap.base_currency)}</span>
                </div>
                <div className="flex justify-between text-rose-700">
                  <span>- Channel Msg & Ad CPA</span>
                  <span>
                    -{formatMoney(
                      Number(snap.channel_msg_cost_base) + Number(snap.ad_attribution_cost_base),
                      snap.base_currency
                    )}
                  </span>
                </div>
                <div className="flex justify-between text-indigo-700">
                  <span>+/- FX Gain/Loss</span>
                  <span>{formatMoney(snap.fx_gain_loss_base, snap.base_currency)}</span>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
```

Create `apps/web/src/app/finance/page.tsx`:

```tsx
"use client";

import React from "react";
import { useQuery } from "@tanstack/react-query";
import { Download } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { useAuthSession } from "@/components/providers";
import {
  PaymentMilestoneTable,
  type OrderProfitSnapshotItem,
  type ReceivableMilestoneItem,
} from "@/components/finance/payment-milestone-table";
import { bffFetch } from "@/lib/api";
import { formatMoney } from "@/lib/contracts";

interface FinanceOverviewResponse {
  summary: {
    total_gross_revenue_base: string;
    total_net_margin_base: string;
    avg_margin_pct: string;
    base_currency: string;
  };
  receivables: ReceivableMilestoneItem[];
  cvp_snapshots: OrderProfitSnapshotItem[];
}

export default function FinancePage() {
  const { principal } = useAuthSession();

  const pendingProposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () =>
      bffFetch<{ total: number }>("/v1/ai/proposals?status=pending&limit=1&offset=0"),
  });

  const financeQuery = useQuery({
    queryKey: ["finance", "overview"],
    enabled: Boolean(principal && principal.role !== "agent"),
    queryFn: () => bffFetch<FinanceOverviewResponse>("/v1/finance/overview"),
  });

  const summary = financeQuery.data?.summary;

  return (
    <AppShell
      currentPath="/finance"
      pendingProposalsCount={pendingProposalsQuery.data?.total ?? 0}
    >
      <div className="mx-auto max-w-6xl space-y-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="text-lg font-bold text-slate-900">
              Finance Milestones & Real-Time CVP Contribution Margin
            </h1>
            <p className="text-xs text-slate-500">
              Strict L0 verification by Finance/Owner for T/T wires and exact Decimal CVP ledger snapshots.
            </p>
          </div>
          <a
            href="/api/bff/v1/finance/cvp/export.csv"
            className="inline-flex items-center gap-1.5 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 hover:bg-slate-100"
          >
            <Download className="h-3.5 w-3.5" />
            <span>Export QuickBooks/Xero CSV</span>
          </a>
        </div>

        {summary && (
          <div className="grid gap-3 md:grid-cols-3">
            <div className="rounded-lg border border-slate-200 bg-white p-4">
              <div className="text-xs text-slate-500">Total Gross Revenue</div>
              <div className="mt-1 text-xl font-bold text-slate-900">
                {formatMoney(summary.total_gross_revenue_base, summary.base_currency)}
              </div>
            </div>
            <div className="rounded-lg border border-slate-200 bg-white p-4">
              <div className="text-xs text-slate-500">Total Net Contribution Margin</div>
              <div className="mt-1 text-xl font-bold text-emerald-700">
                {formatMoney(summary.total_net_margin_base, summary.base_currency)}
              </div>
            </div>
            <div className="rounded-lg border border-slate-200 bg-white p-4">
              <div className="text-xs text-slate-500">Average Margin %</div>
              <div className="mt-1 text-xl font-bold text-indigo-700">
                {summary.avg_margin_pct}%
              </div>
            </div>
          </div>
        )}

        <PaymentMilestoneTable
          receivables={financeQuery.data?.receivables ?? []}
          cvpSnapshots={financeQuery.data?.cvp_snapshots ?? []}
          role={principal?.role}
        />
      </div>
    </AppShell>
  );
}
```

- [ ] **Step 4: Run test & static checks to verify they pass**

Run: `pnpm --filter web test -- src/__tests__/finance-cvp.test.tsx && pnpm --filter web lint`
Expected: PASS (`2 passed` in `finance-cvp.test.tsx` + `0` ESLint errors).

- [ ] **Step 5: Commit**

```bash
git add apps/web/src/components/orders/ apps/web/src/app/orders/ apps/web/src/components/finance/ apps/web/src/app/finance/ apps/web/src/__tests__/finance-cvp.test.tsx
git commit -m "feat(web): add 17TRACK order timeline, inventory ledger modal, and finance CVP console"
```


---

### Task 5.7: Self-Serve Hybrid Factory Onboarding Wizard (`/onboarding`) & Channel Settings (`/settings/channels`)

**Files:**
- Create: `apps/web/src/app/onboarding/page.tsx`
- Create: `apps/web/src/app/settings/channels/page.tsx`
- Test: `apps/web/src/__tests__/onboarding-wizard.test.tsx`

**Interfaces:**
- Consumes:
  - `GET /api/bff/v1/onboarding/state`
  - `POST /api/bff/v1/onboarding/workspace` (`{ preset, company_name, base_currency, iana_timezone }`)
  - `POST /api/bff/v1/onboarding/whatsapp/embedded-signup` (`{ code, waba_id, phone_number_id, is_coexistence, coexistence_checklist_ack }`)
  - `POST /api/bff/v1/onboarding/email/verify-dns` (`{ domain }`)
  - `POST /api/bff/v1/onboarding/storefront/connect` (`{ platform, shop_domain, auth_mode, credentials }`)
  - `PUT /api/bff/v1/onboarding/webchat` (`{ allowed_origins }`)
  - `POST /api/bff/v1/onboarding/sandbox-test` (`{ scenario_id, message }`)
  - `POST /api/bff/v1/onboarding/complete`
  - `GET /api/bff/v1/channels/accounts`
  - `GET /api/bff/v1/channels/dlq`
  - `POST /api/bff/v1/channels/dlq/{id}/replay`
  - `GET /api/bff/v1/channels/consents`
  - `GET /api/bff/v1/channels/suppressions`
  - `POST /api/bff/v1/channels/suppressions`
- Produces:
  - `/onboarding` (`apps/web/src/app/onboarding/page.tsx`)
  - `/settings/channels` (`apps/web/src/app/settings/channels/page.tsx`)

- [ ] **Step 1: Write the failing test (`apps/web/src/__tests__/onboarding-wizard.test.tsx`)**

```tsx
import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nProvider } from "@/lib/i18n";
import { AuthSessionContext } from "@/components/providers";
import OnboardingPage from "@/app/onboarding/page";
import ChannelsSettingsPage from "@/app/settings/channels/page";

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AuthSessionContext.Provider
        value={{
          principal: {
            user_id: "u1",
            tenant_id: "t1",
            role: "owner",
            email: "owner@qzled.example.com",
            tenant_slug: "qz-led-factory",
          },
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

describe("Task 5.7 Onboarding Wizard & Channel Settings", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    Object.assign(navigator, {
      clipboard: { writeText: vi.fn(async () => {}) },
    });
  });

  it("pre-selects Hybrid Factory preset, enforces Coexistence 20 mps ack, and passes 3-message AI sandbox", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/ai/proposals")) {
        return new Response(JSON.stringify({ items: [], total: 0, limit: 1, offset: 0, has_more: false }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/onboarding/state")) {
        return new Response(
          JSON.stringify({
            tenant_id: "t1",
            tenant_slug: "quanzhou-led-factory",
            current_stage: "WorkspaceCreated",
            preset_type: "hybrid_factory_b2b_b2c",
            base_currency: "CNY",
            iana_timezone: "Asia/Shanghai",
            inbound_forwarding_address: "quanzhou-led-factory@inbound.mail.example.com",
            dns_status: { spf_verified: false, dkim_verified: false, return_path_verified: false },
            site_key: "wc_site_qz_001",
            allowed_origins: ["https://shop.qzled.example.com"],
            coexistence_acknowledged: false,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/onboarding/workspace") && init?.method === "POST") {
        return new Response(JSON.stringify({ stage: "ChannelConnecting" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/onboarding/email/verify-dns") && init?.method === "POST") {
        return new Response(
          JSON.stringify({
            spf_verified: true,
            dkim_verified: true,
            return_path_verified: true,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/onboarding/sandbox-test") && init?.method === "POST") {
        return new Response(
          JSON.stringify({
            passed: true,
            reply_text: "Verified grounded response [doc-1#c1]",
            citations: [{ doc_id: "doc-1", chunk_id: "c1", score: 0.92 }],
            source_tool_call_id: "tool-1",
            latency_ms: 410,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/onboarding/complete") && init?.method === "POST") {
        return new Response(JSON.stringify({ stage: "LiveProduction" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response("{}", { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);

    wrap(<OnboardingPage />);

    // Step 1: Preset pre-selected
    await waitFor(() => {
      const presetSelect = screen.getByTestId("onboarding-preset-select") as HTMLSelectElement;
      expect(presetSelect.value).toBe("hybrid_factory_b2b_b2c");
    });

    fireEvent.click(screen.getByTestId("onboarding-save-step1"));

    // Step 2: Coexistence 20 mps checklist gates Coexistence launch button
    await waitFor(() => {
      expect(screen.getByTestId("coexistence-mode-toggle")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId("coexistence-mode-toggle"));
    const metaLaunchBtn = screen.getByTestId("meta-embedded-signup-btn");
    expect(metaLaunchBtn).toBeDisabled();

    fireEvent.click(screen.getByTestId("coexistence-20mps-checkbox"));
    expect(metaLaunchBtn).not.toBeDisabled();

    // Email forwarding & DNS check
    fireEvent.click(screen.getByTestId("copy-email-forwarding-btn"));
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
      "quanzhou-led-factory@inbound.mail.example.com"
    );
    fireEvent.click(screen.getByTestId("verify-email-dns-btn"));
    await waitFor(() => {
      expect(screen.getByTestId("dns-status-verified")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId("onboarding-next-btn"));

    // Step 3: Storefronts (Shopify, WooCommerce, SHOPLINE, Shoplazza, TikTok Shop)
    for (const tab of ["Shopify", "WooCommerce", "SHOPLINE", "Shoplazza", "TikTok Shop"]) {
      const tabBtn = screen.getByRole("button", { name: tab });
      fireEvent.click(tabBtn);
      expect(tabBtn).toBeInTheDocument();
    }

    fireEvent.click(screen.getByTestId("onboarding-next-btn"));

    // Step 4: WebChat <script> snippet copy
    fireEvent.click(screen.getByTestId("copy-webchat-snippet-btn"));
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
      expect.stringContaining("wc_site_qz_001")
    );

    fireEvent.click(screen.getByTestId("onboarding-next-btn"));

    // Step 5: Run 3-message AI Sandbox verifier
    const goLiveBtn = screen.getByTestId("activate-live-production-btn");
    expect(goLiveBtn).toBeDisabled();

    fireEvent.click(screen.getByTestId("run-sandbox-test-1"));
    fireEvent.click(screen.getByTestId("run-sandbox-test-2"));
    fireEvent.click(screen.getByTestId("run-sandbox-test-3"));

    await waitFor(() => {
      expect(goLiveBtn).not.toBeDisabled();
    });
  });

  it("renders DLQ replay button and consent/suppression viewer in /settings/channels", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/ai/proposals")) {
        return new Response(JSON.stringify({ items: [], total: 0, limit: 1, offset: 0, has_more: false }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/channels/accounts")) {
        return new Response(
          JSON.stringify({
            items: [
              {
                id: "ch-1",
                channel_type: "whatsapp_coexistence",
                display_name: "QZ LED WA Business (+86)",
                external_account_id: "waba_phone_1",
                status: "active",
                rate_limit_profile: { mps: 20, coexistence: true },
              },
            ],
            total: 1,
            limit: 20,
            offset: 0,
            has_more: false,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/channels/dlq/dlq-1/replay") && init?.method === "POST") {
        return new Response(JSON.stringify({ ok: true, event_id: "dlq-1" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/channels/dlq")) {
        return new Response(
          JSON.stringify({
            items: [
              {
                id: "dlq-1",
                channel_type: "whatsapp_coexistence",
                external_event_id: "wamid.fail.1",
                error_message: "131056 Pair rate limit exceeded",
                retry_count: 6,
                created_at: "2026-11-19T08:00:00Z",
              },
            ],
            total: 1,
            limit: 20,
            offset: 0,
            has_more: false,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/channels/consents")) {
        return new Response(
          JSON.stringify({
            items: [
              {
                id: "con-1",
                customer_name: "Elena Vance",
                channel_type: "whatsapp_marketing",
                consent_status: "opted_in",
                proof_source: "shopify_checkout_checkbox",
                recorded_at: "2026-11-19T07:00:00Z",
              },
            ],
            total: 1,
            limit: 20,
            offset: 0,
            has_more: false,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/channels/suppressions")) {
        return new Response(
          JSON.stringify({
            items: [
              {
                id: "sup-1",
                channel_type: "email",
                normalized_address: "unsubscribe-test@example.com",
                reason: "RFC 8058 One-Click Opt-Out",
                created_at: "2026-11-19T06:00:00Z",
              },
            ],
            total: 1,
            limit: 20,
            offset: 0,
            has_more: false,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      return new Response("{}", { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);

    wrap(<ChannelsSettingsPage />);

    await waitFor(() => {
      expect(screen.getByText("QZ LED WA Business (+86)")).toBeInTheDocument();
      expect(screen.getByText(/20 mps/)).toBeInTheDocument();
      expect(screen.getByText(/131056 Pair rate limit exceeded/)).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId("dlq-replay-btn-dlq-1"));
    await waitFor(() => {
      const replayCall = fetchMock.mock.calls.find((c) =>
        String(c[0]).includes("/channels/dlq/dlq-1/replay")
      );
      expect(replayCall).toBeDefined();
    });

    expect(screen.getByText("shopify_checkout_checkbox")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("tab-suppression-list"));
    expect(screen.getByText("unsubscribe-test@example.com")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm --filter web test -- src/__tests__/onboarding-wizard.test.tsx`
Expected: FAIL (`Cannot find module '@/app/onboarding/page'`).

- [ ] **Step 3: Write complete implementation**

Create `apps/web/src/app/onboarding/page.tsx`:

```tsx
"use client";

import React, { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Copy, ShieldAlert, Sparkles, Rocket } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { bffFetch } from "@/lib/api";
import type { OnboardingStage } from "@/lib/contracts";

interface OnboardingStatePayload {
  tenant_id: string;
  tenant_slug?: string;
  current_stage: OnboardingStage;
  preset_type: "hybrid_factory_b2b_b2c" | "dtc_brand" | "pure_b2b_inquiry";
  completed_steps?: string[];
  metadata_json?: Record<string, unknown>;
  base_currency?: string;
  iana_timezone?: string;
  inbound_forwarding_address?: string;
  dns_status?: {
    spf_verified: boolean;
    dkim_verified: boolean;
    return_path_verified: boolean;
  };
  site_key?: string;
  allowed_origins?: string[];
  coexistence_acknowledged?: boolean;
}

type StorefrontTab = "Shopify" | "WooCommerce" | "SHOPLINE" | "Shoplazza" | "TikTok Shop";

const SANDBOX_SCENARIOS = [
  {
    id: "b2c_wismo",
    label: "1. B2C WISMO Order & 17TRACK Lookup",
    message: "Hi, where is my order #SH-1042? Can you check tracking?",
  },
  {
    id: "strict_rag_warranty",
    label: "2. Strict RAG Warranty Citation Check",
    message: "What is the warranty and IP rating for COB 24V LED strip?",
  },
  {
    id: "b2b_moq_quote",
    label: "3. B2B 500 pcs FOB MOQ Quote Proposal",
    message: "Need FOB Shenzhen quote for 500 pcs LED-COB-24V-5M, 30% T/T.",
  },
];

export default function OnboardingPage() {
  const queryClient = useQueryClient();
  const [step, setStep] = useState<1 | 2 | 3 | 4 | 5>(1);
  const [preset, setPreset] = useState<
    "hybrid_factory_b2b_b2c" | "dtc_brand" | "pure_b2b_inquiry"
  >("hybrid_factory_b2b_b2c");
  const [companyName, setCompanyName] = useState("Quanzhou Smart LED Lighting Co., Ltd.");
  const [baseCurrency, setBaseCurrency] = useState("CNY");
  const [ianaTimezone, setIanaTimezone] = useState("Asia/Shanghai");

  const [isCoexistence, setIsCoexistence] = useState(false);
  const [coexistenceAck, setCoexistenceAck] = useState(false);
  const [dnsVerified, setDnsVerified] = useState(false);
  const [emailDomain, setEmailDomain] = useState("mail.qzled.example.com");

  const [storefrontTab, setStorefrontTab] = useState<StorefrontTab>("Shopify");
  const [shopDomain, setShopDomain] = useState("qzled.myshopify.com");
  const [byokKey, setByokKey] = useState("");
  const [byokSecret, setByokSecret] = useState("");
  const [tiktokMode, setTiktokMode] = useState<"custom_app_25" | "seller_byok" | "email_bridge">(
    "custom_app_25"
  );

  const [allowedOriginsText, setAllowedOriginsText] = useState("https://shop.qzled.example.com");
  const [sandboxPassed, setSandboxPassed] = useState<Record<string, string>>({});
  const [copiedNotice, setCopiedNotice] = useState<string | null>(null);

  const pendingProposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () =>
      bffFetch<{ total: number }>("/v1/ai/proposals?status=pending&limit=1&offset=0"),
  });

  const stateQuery = useQuery({
    queryKey: ["onboarding", "state"],
    queryFn: () => bffFetch<OnboardingStatePayload>("/v1/onboarding/state"),
  });

  useEffect(() => {
    if (stateQuery.data) {
      setPreset(stateQuery.data.preset_type || "hybrid_factory_b2b_b2c");
      setBaseCurrency(stateQuery.data.base_currency || "CNY");
      setIanaTimezone(stateQuery.data.iana_timezone || "Asia/Shanghai");
      setAllowedOriginsText((stateQuery.data.allowed_origins ?? []).join(", "));
      setDnsVerified(
        Boolean(
          stateQuery.data.dns_status?.spf_verified && stateQuery.data.dns_status?.dkim_verified
        )
      );
    }
  }, [stateQuery.data]);

  const saveWorkspaceMutation = useMutation({
    mutationFn: async () => {
      await bffFetch("/v1/onboarding/preset", {
        method: "PATCH",
        body: JSON.stringify({ preset_type: preset }),
      });
      await bffFetch("/v1/tenants/settings", {
        method: "PATCH",
        body: JSON.stringify({
          name: companyName,
          base_currency: baseCurrency,
          timezone: ianaTimezone,
        }),
      });
      return bffFetch("/v1/onboarding/advance", {
        method: "POST",
        body: JSON.stringify({
          target_stage: "ChannelConnecting",
          step_metadata: { preset_type: preset },
        }),
      });
    },
    onSuccess: () => {
      setStep(2);
    },
  });

  const metaSignupMutation = useMutation({
    mutationFn: () =>
      bffFetch("/v1/onboarding/whatsapp/embedded-signup", {
        method: "POST",
        body: JSON.stringify({
          code: "meta_v4_auth_code_demo",
          waba_id: "waba_qz_001",
          phone_number_id: "phone_qz_001",
          is_coexistence: isCoexistence,
          coexistence_checklist_ack: coexistenceAck,
        }),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["onboarding", "state"] });
    },
  });

  const verifyDnsMutation = useMutation({
    mutationFn: () =>
      bffFetch<{ spf_verified: boolean; dkim_verified: boolean; return_path_verified: boolean }>(
        "/v1/onboarding/email/verify-dns",
        {
          method: "POST",
          body: JSON.stringify({ domain: emailDomain }),
        }
      ),
    onSuccess: (res) => {
      setDnsVerified(res.spf_verified && res.dkim_verified);
    },
  });

  const connectStorefrontMutation = useMutation({
    mutationFn: () =>
      bffFetch("/v1/onboarding/storefront/connect", {
        method: "POST",
        body: JSON.stringify({
          platform: storefrontTab.toLowerCase().replace(" ", "_"),
          shop_domain: shopDomain,
          auth_mode: storefrontTab === "TikTok Shop" ? tiktokMode : "oauth",
          credentials: byokKey ? { key: byokKey, secret: byokSecret } : {},
        }),
      }),
  });

  const saveWebchatMutation = useMutation({
    mutationFn: () =>
      bffFetch("/v1/onboarding/webchat", {
        method: "PUT",
        body: JSON.stringify({
          allowed_origins: allowedOriginsText
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean),
        }),
      }),
  });

  const sandboxTestMutation = useMutation({
    mutationFn: ({ scenarioId, message }: { scenarioId: string; message: string }) =>
      bffFetch<{ passed: boolean; reply_text: string }>("/v1/onboarding/sandbox-test", {
        method: "POST",
        body: JSON.stringify({ scenario_id: scenarioId, message }),
      }),
    onSuccess: (res, vars) => {
      if (res.passed) {
        setSandboxPassed((prev) => ({ ...prev, [vars.scenarioId]: res.reply_text }));
      }
    },
  });

  const completeMutation = useMutation({
    mutationFn: () =>
      bffFetch("/v1/onboarding/advance", {
        method: "POST",
        body: JSON.stringify({
          target_stage: "LiveProduction",
          step_metadata: { sandbox_passed: 3 },
        }),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["onboarding", "state"] });
    },
  });

  const forwardingAddr =
    stateQuery.data?.inbound_forwarding_address ?? "quanzhou-led-factory@inbound.mail.example.com";
  const siteKey = stateQuery.data?.site_key ?? "wc_site_qz_001";
  const snippetCode = `<script src="https://cdn.erp.local/widget.js" data-site-key="${siteKey}" async></script>`;

  function copyText(text: string, label: string) {
    if (navigator.clipboard) {
      void navigator.clipboard.writeText(text);
    }
    setCopiedNotice(`Copied ${label}`);
  }

  const all3SandboxPassed = Object.keys(sandboxPassed).length >= 3;

  return (
    <AppShell
      currentPath="/onboarding"
      pendingProposalsCount={pendingProposalsQuery.data?.total ?? 0}
    >
      <div className="mx-auto max-w-4xl space-y-5">
        <div className="rounded-lg border border-slate-200 bg-white p-4">
          <h1 className="text-lg font-bold text-slate-900">
            Self-Serve Hybrid B2B+B2C Factory Onboarding Wizard
          </h1>
          <div className="mt-3 grid grid-cols-5 gap-2 text-xs">
            {[
              "1. Factory Preset",
              "2. WA & Email Bridge",
              "3. Storefronts",
              "4. WebChat Embed",
              "5. AI Sandbox",
            ].map((label, idx) => {
              const num = (idx + 1) as 1 | 2 | 3 | 4 | 5;
              return (
                <button
                  key={label}
                  type="button"
                  onClick={() => setStep(num)}
                  className={`rounded-md border px-2 py-2 font-semibold ${
                    step === num
                      ? "border-indigo-600 bg-indigo-50 text-indigo-700"
                      : "border-slate-200 text-slate-600"
                  }`}
                >
                  {label}
                </button>
              );
            })}
          </div>
          {copiedNotice && (
            <div className="mt-2 text-xs font-medium text-emerald-700">{copiedNotice}</div>
          )}
        </div>

        {step === 1 && (
          <div className="rounded-lg border border-slate-200 bg-white p-5 text-xs space-y-4">
            <h2 className="text-sm font-bold text-slate-900">
              Step 1: Business Preset, Base Currency & Timezone
            </h2>
            <div className="grid gap-3 md:grid-cols-2">
              <div>
                <label className="block font-semibold text-slate-700">Business Preset</label>
                <select
                  data-testid="onboarding-preset-select"
                  value={preset}
                  onChange={(e) =>
                    setPreset(
                      e.target.value as
                        | "hybrid_factory_b2b_b2c"
                        | "dtc_brand"
                        | "pure_b2b_inquiry"
                    )
                  }
                  className="mt-1 w-full rounded border border-slate-300 px-2.5 py-2"
                >
                  <option value="hybrid_factory_b2b_b2c">
                    Hybrid B2B+B2C Factory (Quanzhou Smart LED Lighting Preset)
                  </option>
                  <option value="dtc_brand">DTC Independent Store Brand</option>
                  <option value="pure_b2b_inquiry">Pure B2B Foreign Trade Factory</option>
                </select>
              </div>
              <div>
                <label className="block font-semibold text-slate-700">Factory / Legal Name</label>
                <input
                  type="text"
                  value={companyName}
                  onChange={(e) => setCompanyName(e.target.value)}
                  className="mt-1 w-full rounded border border-slate-300 px-2.5 py-2"
                />
              </div>
              <div>
                <label className="block font-semibold text-slate-700">Base Currency</label>
                <select
                  value={baseCurrency}
                  onChange={(e) => setBaseCurrency(e.target.value)}
                  className="mt-1 w-full rounded border border-slate-300 px-2.5 py-2"
                >
                  <option value="CNY">CNY</option>
                  <option value="USD">USD</option>
                </select>
              </div>
              <div>
                <label className="block font-semibold text-slate-700">IANA Timezone</label>
                <input
                  type="text"
                  value={ianaTimezone}
                  onChange={(e) => setIanaTimezone(e.target.value)}
                  className="mt-1 w-full rounded border border-slate-300 px-2.5 py-2"
                />
              </div>
            </div>
            <button
              type="button"
              data-testid="onboarding-save-step1"
              onClick={() => saveWorkspaceMutation.mutate()}
              className="rounded-md bg-indigo-600 px-4 py-2 font-semibold text-white hover:bg-indigo-700"
            >
              Save Preset & Continue
            </button>
          </div>
        )}

        {step === 2 && (
          <div className="rounded-lg border border-slate-200 bg-white p-5 text-xs space-y-5">
            <div>
              <h2 className="text-sm font-bold text-slate-900">
                Step 2A: Meta Embedded Signup v4 & WhatsApp Coexistence
              </h2>
              <label className="mt-2 inline-flex cursor-pointer items-center gap-2 font-medium">
                <input
                  type="checkbox"
                  data-testid="coexistence-mode-toggle"
                  checked={isCoexistence}
                  onChange={(e) => setIsCoexistence(e.target.checked)}
                />
                <span>
                  Enable WhatsApp Business App Coexistence (keep mobile app + sync 180-day 1:1 chats)
                </span>
              </label>

              {isCoexistence && (
                <div className="mt-2 rounded-md border border-amber-300 bg-amber-50 p-3 space-y-2 text-amber-950">
                  <div className="flex items-center gap-1.5 font-bold">
                    <ShieldAlert className="h-4 w-4 text-amber-600" />
                    <span>Coexistence 20 mps & Platform Constraints Checklist</span>
                  </div>
                  <ul className="list-disc pl-5 space-y-1">
                    <li>Cloud API throughput is capped at 20 mps (instead of 80 mps).</li>
                    <li>180-day 1:1 chats sync within 24h; group chats do not sync.</li>
                    <li>Disappearing messages and creating new broadcast lists are disabled on mobile.</li>
                  </ul>
                  <label className="flex items-center gap-2 font-semibold">
                    <input
                      type="checkbox"
                      data-testid="coexistence-20mps-checkbox"
                      checked={coexistenceAck}
                      onChange={(e) => setCoexistenceAck(e.target.checked)}
                    />
                    <span>I acknowledge the 20 mps Coexistence limit and mobile constraints</span>
                  </label>
                </div>
              )}

              <button
                type="button"
                data-testid="meta-embedded-signup-btn"
                disabled={isCoexistence && !coexistenceAck}
                onClick={() => metaSignupMutation.mutate()}
                className="mt-3 rounded-md bg-emerald-600 px-3.5 py-2 font-semibold text-white hover:bg-emerald-700 disabled:opacity-45"
              >
                Launch Meta Embedded Signup v4
              </button>
            </div>

            <div className="border-t border-slate-200 pt-4 space-y-2">
              <h2 className="text-sm font-bold text-slate-900">
                Step 2B: Zero-CASA Email Bridge, Made-in-China Forwarding & SPF/DKIM DNS
              </h2>
              <div className="flex items-center gap-2">
                <code className="rounded bg-slate-100 px-2.5 py-1.5 text-slate-800">
                  {forwardingAddr}
                </code>
                <button
                  type="button"
                  data-testid="copy-email-forwarding-btn"
                  onClick={() => copyText(forwardingAddr, "inbound forwarding address")}
                  className="inline-flex items-center gap-1 rounded border border-slate-300 px-2.5 py-1.5 font-medium hover:bg-slate-100"
                >
                  <Copy className="h-3.5 w-3.5" />
                  <span>Copy Address</span>
                </button>
              </div>
              <div className="flex items-center gap-2">
                <input
                  type="text"
                  value={emailDomain}
                  onChange={(e) => setEmailDomain(e.target.value)}
                  className="w-64 rounded border border-slate-300 px-2.5 py-1.5"
                />
                <button
                  type="button"
                  data-testid="verify-email-dns-btn"
                  onClick={() => verifyDnsMutation.mutate()}
                  className="rounded bg-slate-900 px-3 py-1.5 font-semibold text-white"
                >
                  Verify SPF & 2048-bit DKIM
                </button>
                {dnsVerified && (
                  <span
                    data-testid="dns-status-verified"
                    className="inline-flex items-center gap-1 text-emerald-700 font-semibold"
                  >
                    <CheckCircle2 className="h-4 w-4" />
                    <span>SPF + DKIM Verified</span>
                  </span>
                )}
              </div>
            </div>

            <div className="flex justify-end">
              <button
                type="button"
                data-testid="onboarding-next-btn"
                onClick={() => setStep(3)}
                className="rounded-md bg-indigo-600 px-4 py-2 font-semibold text-white"
              >
                Next: Storefront Connectors
              </button>
            </div>
          </div>
        )}

        {step === 3 && (
          <div className="rounded-lg border border-slate-200 bg-white p-5 text-xs space-y-4">
            <h2 className="text-sm font-bold text-slate-900">
              Step 3: P0 Storefronts (OAuth & Compliant BYOK Tabs)
            </h2>
            <div className="flex flex-wrap gap-1.5">
              {(["Shopify", "WooCommerce", "SHOPLINE", "Shoplazza", "TikTok Shop"] as StorefrontTab[]).map(
                (tab) => (
                  <button
                    key={tab}
                    type="button"
                    onClick={() => setStorefrontTab(tab)}
                    className={`rounded-md border px-3 py-1.5 font-semibold ${
                      storefrontTab === tab
                        ? "border-indigo-600 bg-indigo-600 text-white"
                        : "border-slate-200 bg-slate-50 text-slate-700"
                    }`}
                  >
                    {tab}
                  </button>
                )
              )}
            </div>

            <div className="rounded-md border border-slate-200 bg-slate-50 p-3 space-y-3">
              <div>
                <label className="block font-semibold text-slate-700">
                  {storefrontTab} Store Domain / Handle
                </label>
                <input
                  type="text"
                  value={shopDomain}
                  onChange={(e) => setShopDomain(e.target.value)}
                  className="mt-1 w-full rounded border border-slate-300 bg-white px-2.5 py-1.5"
                />
              </div>

              {storefrontTab === "TikTok Shop" && (
                <div>
                  <label className="block font-semibold text-slate-700">Authorization Mode</label>
                  <select
                    value={tiktokMode}
                    onChange={(e) => setTiktokMode(e.target.value as typeof tiktokMode)}
                    className="mt-1 w-full rounded border border-slate-300 bg-white px-2.5 py-1.5"
                  >
                    <option value="custom_app_25">Custom App (&lt;25 Stores Core Fulfillment)</option>
                    <option value="seller_byok">TikTok Shop Seller BYOK (In-house CS API)</option>
                    <option value="email_bridge">Seller Inbox Email-to-Ticket Bridge</option>
                  </select>
                </div>
              )}

              {(storefrontTab === "WooCommerce" ||
                storefrontTab === "SHOPLINE" ||
                storefrontTab === "Shoplazza" ||
                storefrontTab === "TikTok Shop") && (
                <div className="grid gap-2 md:grid-cols-2">
                  <input
                    type="text"
                    value={byokKey}
                    onChange={(e) => setByokKey(e.target.value)}
                    placeholder="Consumer Key / Private App Key (optional for OAuth)"
                    className="rounded border border-slate-300 bg-white px-2.5 py-1.5"
                  />
                  <input
                    type="password"
                    value={byokSecret}
                    onChange={(e) => setByokSecret(e.target.value)}
                    placeholder="Consumer Secret / Access Token"
                    className="rounded border border-slate-300 bg-white px-2.5 py-1.5"
                  />
                </div>
              )}

              <button
                type="button"
                onClick={() => connectStorefrontMutation.mutate()}
                className="rounded bg-slate-900 px-3 py-1.5 font-semibold text-white"
              >
                Connect {storefrontTab}
              </button>
            </div>

            <div className="flex justify-end">
              <button
                type="button"
                data-testid="onboarding-next-btn"
                onClick={() => setStep(4)}
                className="rounded-md bg-indigo-600 px-4 py-2 font-semibold text-white"
              >
                Next: WebChat Embed
              </button>
            </div>
          </div>
        )}

        {step === 4 && (
          <div className="rounded-lg border border-slate-200 bg-white p-5 text-xs space-y-4">
            <h2 className="text-sm font-bold text-slate-900">
              Step 4: Independent Storefront Web LiveChat &lt;script&gt; Snippet
            </h2>
            <div>
              <label className="block font-semibold text-slate-700">Allowed Origins (comma-separated)</label>
              <input
                type="text"
                value={allowedOriginsText}
                onChange={(e) => setAllowedOriginsText(e.target.value)}
                className="mt-1 w-full rounded border border-slate-300 px-2.5 py-1.5"
              />
            </div>

            <div className="rounded bg-slate-900 p-3 font-mono text-[11px] text-emerald-300">
              {snippetCode}
            </div>

            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                data-testid="copy-webchat-snippet-btn"
                onClick={() => copyText(snippetCode, "WebChat snippet")}
                className="inline-flex items-center gap-1 rounded border border-slate-300 px-3 py-1.5 font-semibold"
              >
                <Copy className="h-3.5 w-3.5" />
                <span>Copy &lt;script&gt; Snippet</span>
              </button>
              <button
                type="button"
                onClick={() => saveWebchatMutation.mutate()}
                className="rounded bg-slate-900 px-3 py-1.5 font-semibold text-white"
              >
                Save Allowed Origins
              </button>
            </div>

            <div className="flex justify-end">
              <button
                type="button"
                data-testid="onboarding-next-btn"
                onClick={() => setStep(5)}
                className="rounded-md bg-indigo-600 px-4 py-2 font-semibold text-white"
              >
                Next: 3-Message AI Sandbox
              </button>
            </div>
          </div>
        )}

        {step === 5 && (
          <div className="rounded-lg border border-slate-200 bg-white p-5 text-xs space-y-4">
            <h2 className="text-sm font-bold text-slate-900">
              Step 5: 3-Message AI Sandbox Verifier (Gate to LiveProduction)
            </h2>

            <div className="space-y-2.5">
              {SANDBOX_SCENARIOS.map((sc, idx) => {
                const reply = sandboxPassed[sc.id];
                return (
                  <div
                    key={sc.id}
                    className="rounded-md border border-slate-200 p-3 space-y-1.5"
                  >
                    <div className="flex items-center justify-between">
                      <span className="font-bold text-slate-900">{sc.label}</span>
                      <button
                        type="button"
                        data-testid={`run-sandbox-test-${idx + 1}`}
                        onClick={() =>
                          sandboxTestMutation.mutate({ scenarioId: sc.id, message: sc.message })
                        }
                        className="inline-flex items-center gap-1 rounded bg-indigo-600 px-2.5 py-1 font-semibold text-white"
                      >
                        <Sparkles className="h-3 w-3" />
                        <span>Run Test</span>
                      </button>
                    </div>
                    <div className="text-slate-500">Prompt: &ldquo;{sc.message}&rdquo;</div>
                    {reply && (
                      <div className="rounded bg-emerald-50 p-2 font-medium text-emerald-900">
                        PASS: {reply}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>

            <div className="flex justify-end border-t border-slate-200 pt-3">
              <button
                type="button"
                data-testid="activate-live-production-btn"
                disabled={!all3SandboxPassed || completeMutation.isPending}
                onClick={() => completeMutation.mutate()}
                className="inline-flex items-center gap-1.5 rounded-md bg-emerald-600 px-4 py-2 font-bold text-white hover:bg-emerald-700 disabled:opacity-45"
              >
                <Rocket className="h-4 w-4" />
                <span>Activate Live Production</span>
              </button>
            </div>
          </div>
        )}
      </div>
    </AppShell>
  );
}
```

Create `apps/web/src/app/settings/channels/page.tsx`:

```tsx
"use client";

import React, { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, ShieldCheck, Ban } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { bffFetch } from "@/lib/api";
import type { ChannelStatus, ChannelType, PaginatedResponse } from "@/lib/contracts";

interface ChannelAccountItem {
  id: string;
  channel_type: ChannelType;
  display_name: string;
  external_account_id: string;
  status: ChannelStatus;
  rate_limit_profile: { mps?: number; coexistence?: boolean };
}

interface DlqEventItem {
  id: string;
  channel_type: ChannelType;
  external_event_id: string;
  error_message: string;
  retry_count: number;
  created_at: string;
}

interface ConsentLogItem {
  id: string;
  customer_name: string;
  channel_type: string;
  consent_status: "opted_in" | "opted_out";
  proof_source: string;
  recorded_at: string;
}

interface SuppressionLogItem {
  id: string;
  channel_type: string;
  normalized_address: string;
  reason: string;
  created_at: string;
}

export default function ChannelsSettingsPage() {
  const queryClient = useQueryClient();
  const [logTab, setLogTab] = useState<"consents" | "suppressions">("consents");

  const pendingProposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () =>
      bffFetch<{ total: number }>("/v1/ai/proposals?status=pending&limit=1&offset=0"),
  });

  const accountsQuery = useQuery({
    queryKey: ["channels", "accounts"],
    queryFn: () => bffFetch<PaginatedResponse<ChannelAccountItem>>("/v1/channels/accounts"),
  });

  const dlqQuery = useQuery({
    queryKey: ["channels", "dlq"],
    queryFn: () => bffFetch<PaginatedResponse<DlqEventItem>>("/v1/channels/dlq"),
  });

  const consentsQuery = useQuery({
    queryKey: ["channels", "consents"],
    queryFn: () => bffFetch<PaginatedResponse<ConsentLogItem>>("/v1/channels/consents"),
  });

  const suppressionsQuery = useQuery({
    queryKey: ["channels", "suppressions"],
    queryFn: () => bffFetch<PaginatedResponse<SuppressionLogItem>>("/v1/channels/suppressions"),
  });

  const replayMutation = useMutation({
    mutationFn: (dlqId: string) =>
      bffFetch(`/v1/channels/dlq/${dlqId}/replay`, { method: "POST" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["channels", "dlq"] });
    },
  });

  return (
    <AppShell
      currentPath="/settings/channels"
      pendingProposalsCount={pendingProposalsQuery.data?.total ?? 0}
    >
      <div className="mx-auto max-w-6xl space-y-5">
        <div>
          <h1 className="text-lg font-bold text-slate-900">
            Connected Channels, Dead-Letter Queue (DLQ) & Opt-In Compliance
          </h1>
          <p className="text-xs text-slate-500">
            Monitor rate-limit buckets (20 mps Coexistence vs 80 mps Cloud), replay DLQ webhooks, and audit consent/suppression records.
          </p>
        </div>

        <div className="rounded-lg border border-slate-200 bg-white p-4 text-xs">
          <h2 className="text-sm font-bold text-slate-900">Connected Channel Accounts</h2>
          <div className="mt-3 space-y-2">
            {(accountsQuery.data?.items ?? []).map((acct) => (
              <div
                key={acct.id}
                className="flex flex-wrap items-center justify-between gap-2 rounded border border-slate-200 p-2.5"
              >
                <div>
                  <span className="font-bold text-slate-900">{acct.display_name}</span>
                  <span className="ml-2 rounded bg-slate-100 px-1.5 py-0.5">
                    {acct.channel_type}
                  </span>
                </div>
                <div className="flex items-center gap-2">
                  {acct.rate_limit_profile?.mps && (
                    <span className="rounded bg-indigo-50 px-2 py-0.5 font-semibold text-indigo-700">
                      {acct.rate_limit_profile.mps} mps
                    </span>
                  )}
                  <span className="rounded bg-emerald-50 px-2 py-0.5 font-semibold text-emerald-700">
                    {acct.status}
                  </span>
                </div>
              </div>
            ))}
          </div>
        </div>

        <div className="rounded-lg border border-slate-200 bg-white p-4 text-xs">
          <h2 className="text-sm font-bold text-slate-900">Dead Letter Queue (DLQ) Replay</h2>
          <div className="mt-3 space-y-2">
            {(dlqQuery.data?.items ?? []).length === 0 ? (
              <div className="text-slate-400">Zero failed DLQ events.</div>
            ) : (
              dlqQuery.data!.items.map((ev) => (
                <div
                  key={ev.id}
                  className="flex flex-wrap items-center justify-between gap-2 rounded border border-rose-200 bg-rose-50/40 p-2.5"
                >
                  <div>
                    <div className="font-semibold text-slate-900">
                      {ev.channel_type} · {ev.external_event_id}
                    </div>
                    <div className="text-rose-700">
                      {ev.error_message} (retries: {ev.retry_count})
                    </div>
                  </div>
                  <button
                    type="button"
                    data-testid={`dlq-replay-btn-${ev.id}`}
                    onClick={() => replayMutation.mutate(ev.id)}
                    className="inline-flex items-center gap-1 rounded bg-slate-900 px-2.5 py-1 font-semibold text-white hover:bg-slate-800"
                  >
                    <RefreshCw className="h-3 w-3" />
                    <span>Replay</span>
                  </button>
                </div>
              ))
            )}
          </div>
        </div>

        <div className="rounded-lg border border-slate-200 bg-white p-4 text-xs">
          <div className="flex items-center gap-2 border-b border-slate-200 pb-2">
            <button
              type="button"
              data-testid="tab-consent-records"
              onClick={() => setLogTab("consents")}
              className={`inline-flex items-center gap-1 rounded px-3 py-1 font-semibold ${
                logTab === "consents" ? "bg-indigo-600 text-white" : "text-slate-600"
              }`}
            >
              <ShieldCheck className="h-3.5 w-3.5" />
              <span>Consent Ledger</span>
            </button>
            <button
              type="button"
              data-testid="tab-suppression-list"
              onClick={() => setLogTab("suppressions")}
              className={`inline-flex items-center gap-1 rounded px-3 py-1 font-semibold ${
                logTab === "suppressions" ? "bg-indigo-600 text-white" : "text-slate-600"
              }`}
            >
              <Ban className="h-3.5 w-3.5" />
              <span>Suppression List</span>
            </button>
          </div>

          <div className="mt-3 space-y-1.5">
            {logTab === "consents"
              ? (consentsQuery.data?.items ?? []).map((c) => (
                  <div
                    key={c.id}
                    className="flex items-center justify-between rounded border border-slate-100 p-2"
                  >
                    <div>
                      <strong>{c.customer_name}</strong> · {c.channel_type} ·{" "}
                      <span className="text-slate-500">{c.proof_source}</span>
                    </div>
                    <span className="rounded bg-emerald-50 px-2 py-0.5 font-semibold text-emerald-700">
                      {c.consent_status}
                    </span>
                  </div>
                ))
              : (suppressionsQuery.data?.items ?? []).map((s) => (
                  <div
                    key={s.id}
                    className="flex items-center justify-between rounded border border-slate-100 p-2"
                  >
                    <div>
                      <strong>{s.normalized_address}</strong> ({s.channel_type})
                    </div>
                    <span className="text-rose-700">{s.reason}</span>
                  </div>
                ))}
          </div>
        </div>
      </div>
    </AppShell>
  );
}
```

- [ ] **Step 4: Run test & static checks to verify they pass**

Run: `pnpm --filter web test -- src/__tests__/onboarding-wizard.test.tsx && pnpm --filter web lint`
Expected: PASS (`2 passed` in `onboarding-wizard.test.tsx` + `0` ESLint errors).

- [ ] **Step 5: Commit**

```bash
git add apps/web/src/app/onboarding/ apps/web/src/app/settings/ apps/web/src/__tests__/onboarding-wizard.test.tsx
git commit -m "feat(web): add 5-step Hybrid Factory onboarding wizard and DLQ/compliance settings"
```

---

### Task 5.8: End-to-End Integration & Playwright Verification Suite (`tests/e2e/`)

**Files:**
- Create: `tests/e2e/package.json`
- Create: `tests/e2e/tsconfig.json`
- Create: `tests/e2e/playwright.config.ts`
- Create: `tests/e2e/helpers/auth-and-seed.ts`
- Create: `tests/e2e/specs/hybrid-b2b-quote-to-cvp.spec.ts`
- Create: `tests/e2e/specs/hybrid-b2c-wismo-and-rbac.spec.ts`
- Create: `tests/e2e/specs/onboarding-smoke.spec.ts`

**Interfaces:**
- Consumes:
  - Live `apps/web` (`http://localhost:3000`) & `apps/backend` (`http://localhost:8000`) seeded with `uv --directory apps/backend run python scripts/seed_hybrid_factory.py`
  - Signed webhook endpoints `/api/v1/webhooks/email/inbound`, `/api/v1/webhooks/shopify`, `/api/v1/webhooks/17track`
  - Full UI routes: `/login`, `/inbox`, `/ai/proposals`, `/ai/capabilities`, `/catalog`, `/b2b/quotes`, `/orders`, `/finance`, `/onboarding`, `/settings/channels`
- Produces:
  - Executable Playwright E2E regression suite under `tests/e2e/`

- [ ] **Step 1: Write Playwright project config & test specs (TDD verification against full stack)**

Create `tests/e2e/package.json`:

```json
{
  "name": "e2e",
  "version": "0.1.0",
  "private": true,
  "scripts": {
    "test": "playwright test",
    "test:headed": "playwright test --headed"
  },
  "devDependencies": {
    "@playwright/test": "^1.50.1",
    "@types/node": "^22.13.0",
    "typescript": "^5.7.3"
  }
}
```

Create `tests/e2e/tsconfig.json`:

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "commonjs",
    "strict": true,
    "esModuleInterop": true,
    "skipLibCheck": true
  },
  "include": ["**/*.ts"]
}
```

Create `tests/e2e/playwright.config.ts`:

```ts
import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./specs",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3000",
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
```

Create `tests/e2e/helpers/auth-and-seed.ts`:

```ts
import crypto from "node:crypto";
import type { Page, APIRequestContext } from "@playwright/test";

export const E2E_API_URL = process.env.E2E_API_URL ?? "http://localhost:8000";

export function signHmacSha256Hex(rawBody: string, secret: string): string {
  return crypto.createHmac("sha256", secret).update(rawBody, "utf8").digest("hex");
}

export function signHmacSha256Base64(rawBody: string, secret: string): string {
  return crypto.createHmac("sha256", secret).update(rawBody, "utf8").digest("base64");
}

export async function loginViaUi(
  page: Page,
  email: string,
  password: string,
  tenantSlug = "quanzhou-led-factory"
) {
  await page.goto("/login");
  await page.fill("#tenant-slug", tenantSlug);
  await page.fill("#email", email);
  await page.fill("#password", password);
  await page.click('button[type="submit"]');
  await page.waitForURL(/\/(inbox|onboarding)/);
}

export async function postSignedWebhook(
  request: APIRequestContext,
  path: string,
  payload: Record<string, unknown>,
  headerName: string,
  signature: string
) {
  const rawBody = JSON.stringify(payload);
  const res = await request.post(`${E2E_API_URL}${path}`, {
    data: rawBody,
    headers: {
      "Content-Type": "application/json",
      [headerName]: signature,
    },
  });
  if (!res.ok()) {
    throw new Error(`Webhook ${path} failed with ${res.status()}: ${await res.text()}`);
  }
  return res.json();
}
```

Create `tests/e2e/specs/hybrid-b2b-quote-to-cvp.spec.ts`:

```ts
import { test, expect } from "@playwright/test";
import { loginViaUi, postSignedWebhook, signHmacSha256Hex } from "../helpers/auth-and-seed";

test.describe("Hybrid B2B Flow: Made-in-China Inquiry -> L1 CRM/Quote -> 1-Click Order -> 30%/70% T/T -> 17TRACK -> CVP", () => {
  test("executes complete B2B quote-to-CVP margin lifecycle", async ({ page, request }) => {
    const emailSecret = process.env.RESEND_WEBHOOK_SECRET ?? "dev-resend-secret";
    const track17Secret = process.env.TRACK17_WEBHOOK_SECRET ?? "dev-17track-secret";

    const inquiryBody = {
      event_id: `mic-e2e-${Date.now()}`,
      from_email: "en_notification@made-in-china.com",
      reply_to_email: "hans.weber@berlin-lighting.de",
      buyer_name: "Hans Weber",
      company_name: "Berlin Lighting GmbH",
      country_code: "DE",
      iana_timezone: "Europe/Berlin",
      to_address: "quanzhou-led-factory@inbound.mail.example.com",
      subject: "RFQ: 500 pcs LED-COB-24V-5M FOB Shenzhen",
      text_body:
        "Hello, we need FOB Shenzhen quotation for 500 pcs LED-COB-24V-5M. Payment 30% T/T deposit and 70% before shipment.",
    };
    const rawInquiry = JSON.stringify(inquiryBody);
    await postSignedWebhook(
      request,
      "/api/v1/webhooks/email/inbound",
      inquiryBody,
      "X-Webhook-Signature",
      signHmacSha256Hex(rawInquiry, emailSecret)
    );

    await loginViaUi(page, "owner@quanzhou-led.example.com", "FactoryPass2026!");
    await page.goto("/inbox");

    await expect(page.getByText("Hans Weber")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("buyer-local-time-badge")).toBeVisible();

    // Approve L1 CRM Extract & Quote Draft proposals
    await page.goto("/ai/proposals");
    const crmApproveButtons = page.locator('[data-testid^="proposal-approve-"]');
    await expect(crmApproveButtons.first()).toBeVisible({ timeout: 15_000 });
    await crmApproveButtons.first().click();

    // Verify B2B Quote PI and 1-Click Convert to Order
    await page.goto("/b2b/quotes");
    const convertBtn = page.locator('[data-testid^="convert-quote-btn-"]').first();
    await expect(convertBtn).toBeVisible({ timeout: 15_000 });
    await convertBtn.click();
    await expect(page.getByText(/Converted 1-Click to Order/)).toBeVisible();

    // Verify 70% balance unpaid blocks dispatch on /orders
    await page.goto("/orders");
    await expect(page.locator('[data-testid^="order-ship-blocked-"]').first()).toBeVisible();

    // Finance user verifies 30% deposit & 70% balance milestones
    await loginViaUi(page, "finance@quanzhou-led.example.com", "FactoryPass2026!");
    await page.goto("/finance");
    const verifyButtons = page.locator('[data-testid^="verify-payment-btn-"]');
    const count = await verifyButtons.count();
    for (let i = 0; i < count; i++) {
      await verifyButtons.first().click();
    }

    // Trigger 17TRACK Delivered webhook
    const trackPayload = {
      event: "TRACKING_UPDATED",
      data: {
        number: "DHL99887766",
        track_info: {
          latest_status: { status: "Delivered", sub_status: "Delivered_Other" },
          latest_event: {
            time_utc: new Date().toISOString(),
            location: "Berlin, DE",
            description: "Delivered to consignee",
          },
        },
      },
    };
    await postSignedWebhook(
      request,
      "/api/v1/webhooks/17track",
      trackPayload,
      "sign",
      signHmacSha256Hex(JSON.stringify(trackPayload), track17Secret)
    );

    // Assert CVP net margin waterfall renders cleanly with zero [object Object]
    await page.reload();
    const cvpCard = page.locator('[data-testid^="cvp-waterfall-"]').first();
    await expect(cvpCard).toBeVisible();
    const text = await cvpCard.textContent();
    expect(text).toContain("Net Margin:");
    expect(text).not.toContain("[object Object]");
    expect(text).not.toContain("NaN");
  });
});
```

Create `tests/e2e/specs/hybrid-b2c-wismo-and-rbac.spec.ts`:

```ts
import { test, expect } from "@playwright/test";
import { loginViaUi, postSignedWebhook, signHmacSha256Base64 } from "../helpers/auth-and-seed";

test.describe("Hybrid B2C WISMO, Strict RAG Abstention & Agent RBAC", () => {
  test("handles Shopify/WebChat WISMO, Strict RAG abstention, and blocks Agent from Finance & COGS", async ({
    page,
    request,
  }) => {
    const shopifySecret = process.env.SHOPIFY_CLIENT_SECRET ?? "dev-shopify-secret";
    const orderPayload = {
      id: 9901042,
      name: "#SH-1042",
      email: "elena.vance@gmail.com",
      total_price: "89.90",
      currency: "USD",
      line_items: [{ sku: "LED-COB-24V-5M", quantity: 3, price: "29.90" }],
    };
    await postSignedWebhook(
      request,
      "/api/v1/webhooks/shopify",
      orderPayload,
      "X-Shopify-Hmac-SHA256",
      signHmacSha256Base64(JSON.stringify(orderPayload), shopifySecret)
    );

    await loginViaUi(page, "owner@quanzhou-led.example.com", "FactoryPass2026!");
    await page.goto("/inbox");

    // Open Floating Copilot & toggle Strict RAG
    await page.getByTestId("copilot-fab-btn").click();
    await expect(page.getByTestId("copilot-drawer")).toBeVisible();
    const drawerText = await page.getByTestId("copilot-drawer").textContent();
    expect(drawerText).not.toContain("[object Object]");
    expect(drawerText).not.toContain("tax.filing.overdue");

    await page.getByTestId("copilot-mode-strict-rag").click();
    await page
      .getByTestId("copilot-query-input")
      .fill("Do you offer free helicopter shipping to Mars?");
    await page.getByTestId("copilot-query-submit").click();
    await expect(page.getByTestId("copilot-answer-box")).toContainText(/Abstained/i, {
      timeout: 15_000,
    });

    // Now test Agent RBAC boundaries
    await loginViaUi(page, "agent@quanzhou-led.example.com", "FactoryPass2026!");
    await page.goto("/inbox");
    await expect(page.locator('a[href="/finance"]')).toHaveCount(0);

    await page.goto("/finance");
    await expect(page.getByTestId("rbac-forbidden-guard")).toBeVisible();

    await page.goto("/catalog");
    await expect(page.getByTestId("catalog-cost-header")).toHaveCount(0);
    await expect(page.getByText("LED-COB-24V-5M")).toBeVisible();
  });
});
```

Create `tests/e2e/specs/onboarding-smoke.spec.ts`:

```ts
import { test, expect } from "@playwright/test";
import { loginViaUi } from "../helpers/auth-and-seed";

test.describe("Hybrid Factory Self-Serve Onboarding Wizard Smoke", () => {
  test("completes 5-step onboarding wizard and unlocks LiveProduction", async ({ page }) => {
    await loginViaUi(page, "owner@quanzhou-led.example.com", "FactoryPass2026!");
    await page.goto("/onboarding");

    await expect(page.getByTestId("onboarding-preset-select")).toHaveValue(
      "hybrid_factory_b2b_b2c"
    );
    await page.getByTestId("onboarding-save-step1").click();

    await page.getByTestId("coexistence-mode-toggle").check();
    await expect(page.getByTestId("meta-embedded-signup-btn")).toBeDisabled();
    await page.getByTestId("coexistence-20mps-checkbox").check();
    await expect(page.getByTestId("meta-embedded-signup-btn")).toBeEnabled();

    await page.getByTestId("verify-email-dns-btn").click();
    await expect(page.getByTestId("dns-status-verified")).toBeVisible();
    await page.getByTestId("onboarding-next-btn").click();

    await expect(page.getByRole("button", { name: "TikTok Shop" })).toBeVisible();
    await page.getByTestId("onboarding-next-btn").click();

    await expect(page.getByTestId("copy-webchat-snippet-btn")).toBeVisible();
    await page.getByTestId("onboarding-next-btn").click();

    await expect(page.getByTestId("activate-live-production-btn")).toBeDisabled();
    await page.getByTestId("run-sandbox-test-1").click();
    await page.getByTestId("run-sandbox-test-2").click();
    await page.getByTestId("run-sandbox-test-3").click();
    await expect(page.getByTestId("activate-live-production-btn")).toBeEnabled();
  });
});
```

- [ ] **Step 2: Run unit & Playwright build checks before live run (Red -> Green verification)**

Run: `pnpm --dir tests/e2e install && pnpm --dir tests/e2e exec playwright install chromium && pnpm --dir tests/e2e exec tsc --noEmit`
Expected: `0` TypeScript errors in `tests/e2e`.

- [ ] **Step 3: Boot full stack & execute full frontend build + unit tests + lint + Playwright E2E**

Run:
1. Seed database & start backend/worker (`docker compose -f infra/docker-compose.yml up -d`, `uv --directory apps/backend run alembic upgrade head`, `uv --directory apps/backend run python scripts/seed_hybrid_factory.py`, start FastAPI `:8000` & Taskiq worker, start Next.js `:3000`).
2. Sync OpenAPI client & run full verification suite:
```bash
uv --directory apps/backend run python scripts/export_openapi.py > contracts/openapi.json
pnpm --filter web openapi-ts
pnpm --filter web build && pnpm --filter web test && pnpm --filter web lint && pnpm --dir tests/e2e test
```
Expected:
- `pnpm --filter web build`: PASS (Next.js 15 production build succeeds with zero type errors).
- `pnpm --filter web test`: PASS (`app-shell-rbac.test.tsx`, `inbox-composer.test.tsx`, `copilot-render.test.tsx`, `proposal-queue.test.tsx`, `quote-builder.test.tsx`, `finance-cvp.test.tsx`, `onboarding-wizard.test.tsx` — all green).
- `pnpm --filter web lint`: PASS (`0` errors, zero SQL/DB imports).
- `pnpm --dir tests/e2e test`: PASS (`3 passed`: `hybrid-b2b-quote-to-cvp.spec.ts`, `hybrid-b2c-wismo-and-rbac.spec.ts`, `onboarding-smoke.spec.ts`).

- [ ] **Step 4: Verify zero uncommitted contract drift**

Run: `git status --short`
Expected: Only clean committed files; `contracts/openapi.json` and `apps/web/src/lib/api-client/` in sync.

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/
git commit -m "test(e2e): add Playwright Hybrid Factory B2B/B2C/RBAC/Onboarding E2E suite"
```
