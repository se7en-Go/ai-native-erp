// Wave 1 security regression: the BFF mock-mode bridge must never be reachable
// from a production build, and the login proxy must survive non-JSON upstreams.
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { NextRequest } from "next/server";

import { GET, POST } from "@/app/api/bff/[...path]/route";
import { normalizeWooCommerceStoreUrl } from "@/components/channels/channel-connect-drawer";

type FetchMock = ReturnType<typeof vi.fn>;

const originalEnv = { ...process.env };

function makeRequest(path: string, init?: { method?: string; body?: string }): NextRequest {
  return new NextRequest(`http://localhost:3000/api/bff/${path}`, {
    method: init?.method ?? "GET",
    ...(init?.body
      ? { body: init.body, headers: { "content-type": "application/json" } }
      : {}),
  });
}

function contextFor(path: string): { params: Promise<{ path: string[] }> } {
  return { params: Promise.resolve({ path: path.split("/") }) };
}

function calledUrls(fetchMock: FetchMock): string[] {
  return fetchMock.mock.calls.map((call) => String(call[0]));
}

/**
 * Next.js route modules cannot add extra top-level exports (the generated
 * `.next/types/.../route.ts` rejects them), so the pure mock-mode predicate is
 * exposed on the GET handler. Same function, no proxy round-trip.
 */
const isMockModeAllowed = GET.isMockModeAllowed;

describe("BFF mock-mode production hard-lock (Muse P0-05)", () => {
  beforeEach(() => {
    vi.unstubAllEnvs();
    process.env = { ...originalEnv };
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    process.env = { ...originalEnv };
    vi.restoreAllMocks();
  });

  it("isMockModeAllowed() refuses mock mode in production even with NEXT_PUBLIC_USE_MOCK_API=true", () => {
    const env = {
      NODE_ENV: "production",
      NEXT_PUBLIC_USE_MOCK_API: "true",
    } as NodeJS.ProcessEnv;

    expect(isMockModeAllowed(env)).toBe(false);
  });

  it("isMockModeAllowed() allows mock mode only outside production with the flag enabled", () => {
    expect(
      isMockModeAllowed({
        NODE_ENV: "development",
        NEXT_PUBLIC_USE_MOCK_API: "true",
      } as NodeJS.ProcessEnv)
    ).toBe(true);
    expect(
      isMockModeAllowed({
        NODE_ENV: "test",
        NEXT_PUBLIC_USE_MOCK_API: "true",
      } as NodeJS.ProcessEnv)
    ).toBe(true);

    // Flag off -> live path regardless of NODE_ENV.
    expect(
      isMockModeAllowed({
        NODE_ENV: "development",
        NEXT_PUBLIC_USE_MOCK_API: "false",
      } as NodeJS.ProcessEnv)
    ).toBe(false);
    expect(
      isMockModeAllowed({
        NODE_ENV: "development",
      } as NodeJS.ProcessEnv)
    ).toBe(false);
  });

  it("GET /auth/me in a production build proxies upstream instead of returning mock owner data", async () => {
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");

    const upstream = new Response(
      JSON.stringify({
        type: "https://erp.local/errors/unauthenticated",
        title: "Unauthenticated",
        status: 401,
        detail: "live upstream rejected the request",
        code: "ERR_UNAUTHENTICATED",
      }),
      { status: 401, headers: { "content-type": "application/json" } }
    );
    const fetchMock = vi.fn(async () => upstream.clone()) as unknown as FetchMock;
    vi.stubGlobal("fetch", fetchMock);

    const response = await GET(makeRequest("v1/auth/me"), contextFor("v1/auth/me"));
    const body = (await response.json()) as Record<string, unknown>;

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(calledUrls(fetchMock)[0]).toMatch(/\/api\/v1\/auth\/me$/);
    expect(response.status).toBe(401);
    // No mock principal, no mock role cookie.
    expect(body.principal).toBeUndefined();
    expect(body.tenant_slug).toBeUndefined();
    expect(response.headers.get("set-cookie")).toBeNull();
  });

  it("POST /auth/login in a production build never mints a mock owner session", async () => {
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");

    const upstream = new Response(
      JSON.stringify({
        type: "https://erp.local/errors/unauthenticated",
        title: "Unauthenticated",
        status: 401,
        detail: "invalid credentials",
        code: "ERR_UNAUTHENTICATED",
      }),
      { status: 401, headers: { "content-type": "application/json" } }
    );
    const fetchMock = vi.fn(async () => upstream.clone()) as unknown as FetchMock;
    vi.stubGlobal("fetch", fetchMock);

    const response = await POST(
      makeRequest("v1/auth/login", {
        method: "POST",
        body: JSON.stringify({ email: "owner@quanzhou-led.example.com", password: "x" }),
      }),
      contextFor("v1/auth/login")
    );
    const body = (await response.json()) as Record<string, unknown>;

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(calledUrls(fetchMock)[0]).toMatch(/\/api\/v1\/auth\/login$/);
    expect(response.status).toBe(401);
    expect(body.principal).toBeUndefined();
    expect(response.headers.get("set-cookie")).toBeNull();
  });
});

describe("WooCommerce store URL normalization (GPT #24)", () => {
  it("upgrades a bare domain to https and strips trailing slashes", () => {
    expect(normalizeWooCommerceStoreUrl("shop.example.com")).toBe("https://shop.example.com");
    expect(normalizeWooCommerceStoreUrl("  shop.example.com/  ")).toBe("https://shop.example.com");
    expect(normalizeWooCommerceStoreUrl("shop.example.com/store/")).toBe(
      "https://shop.example.com/store"
    );
    expect(normalizeWooCommerceStoreUrl("shop.example.com:8443")).toBe(
      "https://shop.example.com:8443"
    );
  });

  it("keeps an explicit http(s) scheme and leaves blanks blank", () => {
    expect(normalizeWooCommerceStoreUrl("http://shop.example.com/")).toBe("http://shop.example.com");
    expect(normalizeWooCommerceStoreUrl("https://shop.example.com")).toBe(
      "https://shop.example.com"
    );
    expect(normalizeWooCommerceStoreUrl("   ")).toBe("");
  });
});

describe("BFF login proxy non-JSON upstream safety (Muse P2-06)", () => {
  beforeEach(() => {
    vi.unstubAllEnvs();
    process.env = { ...originalEnv };
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    process.env = { ...originalEnv };
    vi.restoreAllMocks();
  });

  it("returns a structured 502 ProblemDetail when the upstream 502 body is HTML", async () => {
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "false");

    const fetchMock = vi.fn(
      async () =>
        new Response("<html><body>502 Bad Gateway</body></html>", {
          status: 502,
          headers: { "content-type": "text/html" },
        })
    ) as unknown as FetchMock;
    vi.stubGlobal("fetch", fetchMock);

    const response = await POST(
      makeRequest("v1/auth/login", {
        method: "POST",
        body: JSON.stringify({ email: "owner@quanzhou-led.example.com", password: "x" }),
      }),
      contextFor("v1/auth/login")
    );
    const body = (await response.json()) as Record<string, unknown>;

    expect(response.status).toBe(502);
    expect(body.code).toBe("ERR_UPSTREAM_AUTH_INVALID_RESPONSE");
    expect(body.status).toBe(502);
    expect(typeof body.detail).toBe("string");
    expect(response.headers.get("set-cookie")).toBeNull();
  });

  it("returns a structured 502 ProblemDetail when the upstream body is empty", async () => {
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "false");

    const fetchMock = vi.fn(async () => new Response("", { status: 200 })) as unknown as FetchMock;
    vi.stubGlobal("fetch", fetchMock);

    const response = await POST(
      makeRequest("v1/auth/login", {
        method: "POST",
        body: JSON.stringify({ email: "owner@quanzhou-led.example.com", password: "x" }),
      }),
      contextFor("v1/auth/login")
    );
    const body = (await response.json()) as Record<string, unknown>;

    expect(response.status).toBe(502);
    expect(body.code).toBe("ERR_UPSTREAM_AUTH_INVALID_RESPONSE");
    expect(response.headers.get("set-cookie")).toBeNull();
  });

  it("never sets auth cookies when access_token is missing or blank", async () => {
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "false");

    const fetchMock = vi.fn(
      async () =>
        new Response(JSON.stringify({ access_token: "", refresh_token: "rt-live" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        })
    ) as unknown as FetchMock;
    vi.stubGlobal("fetch", fetchMock);

    const response = await POST(
      makeRequest("v1/auth/login", {
        method: "POST",
        body: JSON.stringify({ email: "owner@quanzhou-led.example.com", password: "x" }),
      }),
      contextFor("v1/auth/login")
    );
    const body = (await response.json()) as Record<string, unknown>;

    expect(response.status).toBe(502);
    expect(body.code).toBe("ERR_UPSTREAM_AUTH_MISSING_TOKEN");
    expect(response.headers.get("set-cookie")).toBeNull();
  });

  it("still proxies a healthy upstream login and sets the auth cookies", async () => {
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "false");

    const fetchMock = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            access_token: "at-live-token",
            refresh_token: "rt-live-token",
            expires_in: 900,
            principal: {
              user_id: "u-live-owner",
              tenant_id: "t-live",
              role: "owner",
              email: "owner@qzled.example.com",
            },
          }),
          { status: 200, headers: { "content-type": "application/json" } }
        )
    ) as unknown as FetchMock;
    vi.stubGlobal("fetch", fetchMock);

    const response = await POST(
      makeRequest("v1/auth/login", {
        method: "POST",
        body: JSON.stringify({ email: "owner@qzled.example.com", password: "live-pass" }),
      }),
      contextFor("v1/auth/login")
    );
    const body = (await response.json()) as { principal?: { user_id?: string } };

    expect(response.status).toBe(200);
    expect(body.principal?.user_id).toBe("u-live-owner");
    const setCookie = response.headers.get("set-cookie") ?? "";
    expect(setCookie).toContain("at-live-token");
    expect(setCookie).toContain("rt-live-token");
  });
});
