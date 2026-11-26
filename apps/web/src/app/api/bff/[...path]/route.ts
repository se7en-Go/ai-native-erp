import { NextRequest, NextResponse } from "next/server";
import {
  ACCESS_COOKIE_NAME,
  REFRESH_COOKIE_NAME,
  buildAuthCookieOptions,
} from "@/lib/auth-cookie";
import type { Principal, UserRole } from "@/lib/contracts";
import {
  mockCapabilities,
  mockConversationsPage,
  mockProposals,
  mockWallet,
} from "@/lib/mock-fixtures";

const BACKEND_BASE = (process.env.BFF_BACKEND_URL ?? "http://localhost:8000").replace(/\/$/, "");

/**
 * Mock-mode gate (Wave 1 / Muse P0-05).
 *
 * `NEXT_PUBLIC_USE_MOCK_API` is a *public* build flag, so a stray
 * `NEXT_PUBLIC_USE_MOCK_API=true` inlined into a production bundle must never
 * be able to turn the BFF into an unauthenticated owner-login bypass.
 * Production therefore hard-refuses mock mode regardless of the flag, while
 * `pnpm run dev` (NODE_ENV=development, used by the e2e-mock suite) keeps the
 * fixture-backed behaviour 100% intact.
 */
export type MockModePredicate = (env?: NodeJS.ProcessEnv) => boolean;

function isMockModeAllowed(env: NodeJS.ProcessEnv = process.env): boolean {
  if (env.NODE_ENV === "production") return false;
  return env.NEXT_PUBLIC_USE_MOCK_API === "true";
}

function problemDetail(
  status: number,
  title: string,
  code: string,
  detail: string
): {
  type: string;
  title: string;
  status: number;
  detail: string;
  code: string;
} {
  const slug = code.toLowerCase().replace(/_/g, "-");
  return { type: `https://erp.local/errors/${slug}`, title, status, detail, code };
}

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

/**
 * Operator-BFF upstream allowlist (Wave 6 / W6-A).
 *
 * The BFF is a *narrow* operator surface, not a general backend proxy. Only
 * `/api/v1/<prefix>` namespaces that operators actually use are exposed;
 * machine-to-machine ingress (`/api/v1/webhooks/*`), the public widget API
 * (`/api/v1/widget/*`), docs (`/docs`, `/openapi.json`, `/redoc`) and every
 * other backend route are explicitly refused with a 403 ProblemDetail.
 */
const ALLOWED_BFF_V1_PREFIXES = new Set([
  "ai",
  "analytics",
  "auth",
  "b2b",
  "billing",
  "catalog",
  "channels",
  "connectors",
  "customers",
  "finance",
  "growth",
  "identity",
  "inbox",
  "onboarding",
  "ops",
  "orders",
  "search",
  "social",
  "supply-chain",
  "tenancy",
  "tenants",
  "wms",
]);

const ALLOWED_HEALTH_PATHS = new Set(["/healthz", "/readyz"]);

export type AllowedPathPredicate = (upstreamPath: string) => boolean;

function isAllowedUpstreamPath(upstreamPath: string): boolean {
  if (ALLOWED_HEALTH_PATHS.has(upstreamPath)) return true;
  if (!upstreamPath.startsWith("/api/v1/")) return false;
  const prefix = upstreamPath.slice("/api/v1/".length).split("/", 1)[0] ?? "";
  return ALLOWED_BFF_V1_PREFIXES.has(prefix);
}

function isSseRequest(request: NextRequest, upstreamPath: string): boolean {
  const accept = request.headers.get("accept") ?? "";
  return (
    accept.includes("text/event-stream") ||
    upstreamPath.endsWith("/stream") ||
    upstreamPath.endsWith("/events")
  );
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

function applyRefreshedCookies(
  response: NextResponse,
  refreshedTokens: { access_token: string; refresh_token?: string; expires_in?: number } | null
): void {
  if (!refreshedTokens) return;
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

const MOCK_ROLE_COOKIE_NAME = "erp_mock_role";
const MOCK_ROLES: UserRole[] = ["owner", "admin", "manager", "agent", "finance"];

function mockRoleFromEmail(email: string): UserRole {
  const prefix = email.split("@")[0]?.toLowerCase() ?? "";
  return (MOCK_ROLES.find((r) => prefix === r || prefix.startsWith(`${r}.`)) ??
    "owner") as UserRole;
}

function mockPrincipal(role: UserRole, email: string): Principal {
  return {
    user_id: `u-mock-${role}`,
    tenant_id: "t1",
    role,
    email,
    tenant_slug: "quanzhou-led-factory",
    tenant_name: "Quanzhou Smart LED Lighting Co., Ltd.",
    can_view_cost: role !== "agent",
    onboarding_stage: "LiveProduction",
  };
}

async function readJsonBody(request: NextRequest): Promise<Record<string, unknown>> {
  try {
    return (await request.json()) as Record<string, unknown>;
  } catch {
    return {};
  }
}

/**
 * Mock-mode BFF responses. Returns null when mock mode is off (live path)
 * so the request falls through to the real upstream proxy.
 */
async function handleMockMode(
  request: NextRequest,
  upstreamPath: string
): Promise<Response | null> {
  // Fail closed: a production build never serves mock fixtures/owner data.
  if (process.env.NODE_ENV === "production") return null;
  if (!isMockModeAllowed()) return null;
  const method = request.method;
  const isMutation = ["POST", "PUT", "PATCH", "DELETE"].includes(method);

  // Keep the inbox SSE stream alive in mock mode so EventSource never
  // reconnect-loops against a dead backend. The contract endpoint is
  // /api/v1/inbox/stream; /events is kept only for backward compatibility
  // with older clients.
  if (
    upstreamPath === "/api/v1/inbox/stream" ||
    upstreamPath === "/api/v1/inbox/events"
  ) {
    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(": mock heartbeat\n\n"));
        const timer = setInterval(() => {
          try {
            controller.enqueue(new TextEncoder().encode(": mock heartbeat\n\n"));
          } catch {
            clearInterval(timer);
          }
        }, 25_000);
      },
      cancel() {},
    });
    return new Response(stream, {
      headers: {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache, no-transform",
        Connection: "keep-alive",
      },
    });
  }

  if (upstreamPath === "/api/v1/auth/login" && method === "POST") {
    const body = await readJsonBody(request);
    const email = typeof body.email === "string" ? body.email : "owner@quanzhou-led.example.com";
    const role = mockRoleFromEmail(email);
    const response = NextResponse.json({ principal: mockPrincipal(role, email) }, { status: 200 });
    response.cookies.set(
      MOCK_ROLE_COOKIE_NAME,
      role,
      buildAuthCookieOptions(60 * 60 * 24 * 14)
    );
    return response;
  }

  if (upstreamPath === "/api/v1/auth/me" && method === "GET") {
    const role = request.cookies.get(MOCK_ROLE_COOKIE_NAME)?.value as UserRole | undefined;
    if (!role || !MOCK_ROLES.includes(role)) {
      return NextResponse.json(
        {
          type: "https://erp.local/errors/unauthenticated",
          title: "Unauthenticated",
          status: 401,
          detail: "No mock session. Sign in first.",
          code: "ERR_UNAUTHENTICATED",
        },
        { status: 401 }
      );
    }
    return NextResponse.json(
      mockPrincipal(role, `${role}@quanzhou-led.example.com`)
    );
  }

  if (upstreamPath === "/api/v1/auth/logout" && method === "POST") {
    const response = NextResponse.json({ ok: true }, { status: 200 });
    response.cookies.set(MOCK_ROLE_COOKIE_NAME, "", buildAuthCookieOptions(0));
    return response;
  }

  // Reads the Copilot calls directly (the data-source mock layer does not
  // sit in front of these).
  if (method === "GET" && upstreamPath === "/api/v1/ai/capabilities") {
    return NextResponse.json({ capabilities: mockCapabilities });
  }
  if (method === "GET" && upstreamPath === "/api/v1/billing/wallet") {
    return NextResponse.json(mockWallet);
  }
  if (
    method === "GET" &&
    upstreamPath.match(/^\/api\/v1\/identity\/customers\/[^/]+\/context$/)
  ) {
    const customerId = upstreamPath.split("/")[5] ?? "cust-1";
    const convo = mockConversationsPage().items.find(
      (c) => c.customer_id === customerId
    );
    const name = convo?.customer_name ?? "Hans Weber";
    return NextResponse.json({
      customer: {
        id: customerId,
        display_name: name,
        customer_type: "company",
        ltv_usd: "7100.00",
        lifecycle_stage: "customer",
      },
      company: {
        id: "comp-1",
        legal_name: "Berlin Lighting GmbH",
        country_code: "DE",
        credit_tier: "B",
        default_incoterm: "FOB",
      },
      identities: [
        {
          id: "ident-1",
          identity_type: "email",
          identity_value: "hans.weber@berlin-lighting.de",
          confidence_level: "high",
        },
      ],
      probable_duplicates: [],
      merge_events: [],
      recent_orders: [
        {
          id: "ord-b2b-1",
          order_no: "ORD-B2B-1",
          order_mode: "b2b",
          fulfillment_status: "in_production",
          payment_status: "partial",
          total_amount: "7100.00",
          currency: "USD",
          tracking_number: "DHL99887766",
          standardized_status: "CustomsHold",
          sub_status: "CustomsHold",
          is_stagnant_72h: true,
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
          status: "accepted",
        },
      ],
    });
  }
  if (method === "GET" && upstreamPath === "/api/v1/ai/proposals") {
    const status = request.nextUrl.searchParams.get("status");
    const items = status
      ? mockProposals.filter((p) => p.status === status)
      : mockProposals;
    return NextResponse.json({
      items,
      total: items.length,
      limit: 20,
      offset: 0,
      has_more: false,
    });
  }

  if (method === "GET" && upstreamPath === "/api/v1/finance/fx-rates/live") {
    return NextResponse.json({
      base: "USD",
      quote: "CNY",
      usd_cny: "7.2410",
      rates: [
        { pair: "USD/CNY", base: "USD", quote: "CNY", rate: "7.2410" },
        { pair: "EUR/CNY", base: "EUR", quote: "CNY", rate: "7.8520" },
        { pair: "GBP/CNY", base: "GBP", quote: "CNY", rate: "9.1640" },
        { pair: "USD/HKD", base: "USD", quote: "HKD", rate: "7.7820" },
      ],
      source: "ECB (Frankfurter)",
      as_of_date: "2026-11-20",
      fetched_at: new Date().toISOString(),
      is_live: true,
      refresh_interval_seconds: 3600,
    });
  }

  if (method === "GET" && upstreamPath === "/api/v1/orders/warehouses") {
    return NextResponse.json({
      default_warehouse_id: "wh-sz",
      items: [{ id: "wh-sz", is_default: true }],
    });
  }

  if (!isMutation) {
    return NextResponse.json(
      {
        type: "https://erp.local/errors/mock-not-implemented",
        title: "Mock Not Implemented",
        status: 404,
        detail: `No mock response for ${method} ${upstreamPath}.`,
        code: "ERR_MOCK_NOT_IMPLEMENTED",
      },
      { status: 404 }
    );
  }

  // Mutations: special-case the ones whose response body the UI reads.
  if (upstreamPath.match(/^\/api\/v1\/b2b\/quotes\/[^/]+\/convert-to-order$/)) {
    return NextResponse.json(
      { order: { id: "ord-99", order_no: "ORD-B2B-99" } },
      { status: 201 }
    );
  }
  if (upstreamPath.match(/^\/api\/v1\/b2b\/quotes\/[^/]+\/pdf$/)) {
    return NextResponse.json({
      download_url: "https://minio.local/erp-assets/PI-202611-0042-v1.pdf",
    });
  }
  if (upstreamPath === "/api/v1/onboarding/email/verify-dns") {
    return NextResponse.json({
      spf_verified: true,
      dkim_verified: true,
      return_path_verified: true,
    });
  }
  if (upstreamPath === "/api/v1/onboarding/sandbox-test") {
    return NextResponse.json({
      passed: true,
      reply_text: "Verified grounded response [doc-1#c1]",
      citations: [{ doc_id: "doc-1", chunk_id: "c1", score: 0.92 }],
      source_tool_call_id: "tool-1",
      latency_ms: 410,
    });
  }
  if (upstreamPath === "/api/v1/onboarding/workspace") {
    return NextResponse.json({ stage: "ChannelConnecting" });
  }
  if (upstreamPath === "/api/v1/onboarding/advance") {
    return NextResponse.json({ stage: "LiveProduction" });
  }
  if (upstreamPath === "/api/v1/ai/assistant/chat") {
    const body = await readJsonBody(request);
    const message = typeof body.message === "string" ? body.message : "";
    const strictRag = body.strict_rag === true;
    if (strictRag && /mars|helicopter/i.test(message)) {
      return NextResponse.json({
        reply_text:
          "I cannot verify this from the knowledge base. No grounded answer available for this request.",
        citations: [],
        should_abstain: true,
      });
    }
    return NextResponse.json({
      reply_text:
        "The LED-COB-24V-5M strip carries a 3-year warranty and IP65 rating [doc-led#c4].",
      citations: [{ doc_id: "doc-led", chunk_id: "c4", score: 0.89 }],
      tool_calls: [{ tool_call_id: "tool-call-77", tool_name: "get_live_sku_pricing_and_stock" }],
      should_abstain: false,
    });
  }
  if (upstreamPath === "/api/v1/ai/translate") {
    const body = await readJsonBody(request);
    const text = typeof body.text === "string" ? body.text : "";
    return NextResponse.json({ translated_text: `Translated: ${text}` });
  }
  if (upstreamPath.match(/^\/api\/v1\/inbox\/conversations\/[^/]+\/ai-assist$/)) {
    return NextResponse.json({
      text: "Draft: Thanks for reaching out — your order is on its way.",
      abstained: false,
    });
  }
  if (upstreamPath.match(/^\/api\/v1\/finance\/payments\/[^/]+\/verify$/)) {
    return NextResponse.json({ ok: true, order_fulfillment_status: "in_production" });
  }

  return NextResponse.json({ ok: true });
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

  // Fail closed before any upstream fetch (and before mock-mode fixtures) so
  // webhook ingress, the public widget API and backend docs can never be
  // reached through the operator BFF.
  if (!isAllowedUpstreamPath(upstreamPath)) {
    return NextResponse.json(
      problemDetail(
        403,
        "Forbidden BFF Path",
        "ERR_BFF_PATH_FORBIDDEN",
        "This upstream path is not exposed through the operator BFF."
      ),
      { status: 403 }
    );
  }

  const targetUrl = `${BACKEND_BASE}${upstreamPath}${request.nextUrl.search}`;

  // Mock mode (E2E_MODE=mock / NEXT_PUBLIC_USE_MOCK_API=true): serve the
  // Playwright suite without any backend. Auth is a role cookie derived
  // from the login email prefix; reads that the client data-source does
  // not already serve from mock-fixtures are answered here (Copilot
  // capabilities/wallet/pending-count call bffFetch directly); mutations
  // return success payloads shaped the way the UI reads them. Live mode
  // never reaches this branch.
  const mocked = await handleMockMode(request, upstreamPath);
  if (mocked) return mocked;

  if (upstreamPath === "/api/v1/auth/login" && request.method === "POST") {
    const bodyText = await request.text();
    const loginRes = await fetch(targetUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: bodyText,
    });
    // Never let a non-JSON (HTML 502 from a proxy/ingress, empty body, etc.)
    // response blow up into an unhandled SyntaxError -> opaque 500.
    const rawLoginBody = await loginRes.text();
    let payload: Record<string, unknown> | null = null;
    try {
      const parsed: unknown = rawLoginBody.trim().length > 0 ? JSON.parse(rawLoginBody) : null;
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        payload = parsed as Record<string, unknown>;
      }
    } catch {
      payload = null;
    }

    if (!loginRes.ok) {
      if (payload) {
        return NextResponse.json(payload, { status: loginRes.status });
      }
      return NextResponse.json(
        problemDetail(
          502,
          "Upstream Authentication Failed",
          "ERR_UPSTREAM_AUTH_INVALID_RESPONSE",
          `Auth upstream returned status ${loginRes.status} with a non-JSON body.`
        ),
        { status: 502 }
      );
    }

    if (!payload) {
      return NextResponse.json(
        problemDetail(
          502,
          "Upstream Authentication Failed",
          "ERR_UPSTREAM_AUTH_INVALID_RESPONSE",
          "Auth upstream returned an empty or non-JSON body."
        ),
        { status: 502 }
      );
    }

    const access_token = payload.access_token;
    if (typeof access_token !== "string" || access_token.length === 0) {
      return NextResponse.json(
        problemDetail(
          502,
          "Upstream Authentication Failed",
          "ERR_UPSTREAM_AUTH_MISSING_TOKEN",
          "Auth upstream response did not contain a usable access_token."
        ),
        { status: 502 }
      );
    }
    const refresh_token =
      typeof payload.refresh_token === "string" && payload.refresh_token.length > 0
        ? payload.refresh_token
        : undefined;
    const expires_in = typeof payload.expires_in === "number" ? payload.expires_in : undefined;
    const resolvedPrincipal = payload.principal ?? payload.user;
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

  // Refresh applies to SSE too: a long-lived EventSource that hits an expired
  // access token must recover in place instead of reconnect-looping on 401.
  if (upstreamRes.status === 401 && refreshToken) {
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
    const sseResponse = new NextResponse(upstreamRes.body, {
      status: upstreamRes.status,
      headers: {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache, no-transform",
        Connection: "keep-alive",
        "X-Accel-Buffering": "no",
      },
    });
    applyRefreshedCookies(sseResponse, refreshedTokens);
    return sseResponse;
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

  applyRefreshedCookies(response, refreshedTokens);

  return response;
}

type BffRouteContext = { params: Promise<{ path: string[] }> };

/**
 * Next.js route modules may only export HTTP verbs plus segment config
 * (`.next/types/app/api/bff/[...path]/route.ts` enforces that with
 * `checkFields<Diff<...>>`), so the pure predicate cannot be a top-level named
 * export. It rides on the GET handler instead, which keeps it directly
 * unit-testable (`GET.isMockModeAllowed(env)`) without a proxy round-trip.
 */
type BffRouteHandler = ((req: NextRequest, ctx: BffRouteContext) => Promise<Response>) & {
  isMockModeAllowed: MockModePredicate;
  isAllowedUpstreamPath: AllowedPathPredicate;
};

export const GET: BffRouteHandler = Object.assign(
  (req: NextRequest, ctx: BffRouteContext) => handleProxy(req, ctx),
  { isMockModeAllowed, isAllowedUpstreamPath }
);

export async function POST(req: NextRequest, ctx: BffRouteContext) {
  return handleProxy(req, ctx);
}
export async function PUT(req: NextRequest, ctx: BffRouteContext) {
  return handleProxy(req, ctx);
}
export async function PATCH(req: NextRequest, ctx: BffRouteContext) {
  return handleProxy(req, ctx);
}
export async function DELETE(req: NextRequest, ctx: BffRouteContext) {
  return handleProxy(req, ctx);
}
