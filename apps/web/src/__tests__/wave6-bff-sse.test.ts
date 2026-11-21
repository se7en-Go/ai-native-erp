// Wave 6 / W6-A — SSE stability + BFF hardening regression suite.
//
// Covers three P1 audit findings:
//   1. The operator BFF must be a narrow allowlist, never an open backend proxy.
//   2. An SSE request that hits an expired access token must refresh in place
//      (and the refreshed cookies must land on the streamed response).
//   3. The inbox EventSource must react to `message.status_updated` and
//      reconcile missed events after a reconnect.
// Plus the edge auth gate (cookie presence -> 307 /login?next=...).
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { NextRequest, NextResponse } from "next/server";

import { GET } from "@/app/api/bff/[...path]/route";
import { useRealtimeInbox } from "@/hooks/use-realtime-inbox";
import { middleware } from "@/middleware";

type FetchMock = ReturnType<typeof vi.fn>;

const originalEnv = { ...process.env };

const isAllowedUpstreamPath = GET.isAllowedUpstreamPath;

function makeRequest(
  path: string,
  init?: { method?: string; headers?: Record<string, string>; body?: string }
): NextRequest {
  return new NextRequest(`http://localhost:3000/api/bff/${path}`, {
    method: init?.method ?? "GET",
    ...(init?.headers ? { headers: init.headers } : {}),
    ...(init?.body ? { body: init.body } : {}),
  });
}

function contextFor(path: string): { params: Promise<{ path: string[] }> } {
  return { params: Promise.resolve({ path: path.split("/") }) };
}

function middlewareRequest(path: string, cookies?: Record<string, string>): NextRequest {
  const cookieHeader = cookies
    ? Object.entries(cookies)
        .map(([name, value]) => `${name}=${value}`)
        .join("; ")
    : undefined;
  return new NextRequest(`http://localhost:3000${path}`, {
    ...(cookieHeader ? { headers: { cookie: cookieHeader } } : {}),
  });
}

function isAllowedThrough(response: { headers: Headers }): boolean {
  return response.headers.get("x-middleware-next") === "1";
}

beforeEach(() => {
  vi.unstubAllEnvs();
  process.env = { ...originalEnv };
});

afterEach(() => {
  vi.unstubAllEnvs();
  process.env = { ...originalEnv };
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// 1. BFF upstream path allowlist
// ---------------------------------------------------------------------------

describe("BFF upstream path allowlist (W6-A)", () => {
  it("allows operator namespaces and health probes", () => {
    expect(isAllowedUpstreamPath("/healthz")).toBe(true);
    expect(isAllowedUpstreamPath("/readyz")).toBe(true);
    expect(isAllowedUpstreamPath("/api/v1/inbox/stream")).toBe(true);
    expect(isAllowedUpstreamPath("/api/v1/inbox/events")).toBe(true);
    expect(isAllowedUpstreamPath("/api/v1/orders")).toBe(true);
    expect(isAllowedUpstreamPath("/api/v1/orders/ord-1/documents/packing-list.pdf")).toBe(true);
    expect(isAllowedUpstreamPath("/api/v1/b2b/quotes/q-1/pdf")).toBe(true);
    expect(isAllowedUpstreamPath("/api/v1/finance/cvp/export.csv")).toBe(true);
  });

  it("blocks webhook ingress, the public widget API, docs and anything else", () => {
    expect(isAllowedUpstreamPath("/api/v1/webhooks/telegram")).toBe(false);
    expect(isAllowedUpstreamPath("/api/v1/webhooks/email")).toBe(false);
    expect(isAllowedUpstreamPath("/api/v1/widget/sessions")).toBe(false);
    expect(isAllowedUpstreamPath("/docs")).toBe(false);
    expect(isAllowedUpstreamPath("/openapi.json")).toBe(false);
    expect(isAllowedUpstreamPath("/redoc")).toBe(false);
    expect(isAllowedUpstreamPath("/api/v1")).toBe(false);
    expect(isAllowedUpstreamPath("/api/v2/orders")).toBe(false);
    // Prefix look-alikes must not sneak through (`inboxx` != `inbox`).
    expect(isAllowedUpstreamPath("/api/v1/inboxx/stream")).toBe(false);
  });

  it("returns 403 ERR_BFF_PATH_FORBIDDEN for a webhook path without calling fetch", async () => {
    const fetchMock = vi.fn() as unknown as FetchMock;
    vi.stubGlobal("fetch", fetchMock);

    const response = await GET(
      makeRequest("v1/webhooks/telegram", { method: "POST", body: "{}" }),
      contextFor("v1/webhooks/telegram")
    );
    const body = (await response.json()) as Record<string, unknown>;

    expect(response.status).toBe(403);
    expect(body.code).toBe("ERR_BFF_PATH_FORBIDDEN");
    expect(body.title).toBe("Forbidden BFF Path");
    expect(body.status).toBe(403);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("blocks the allowlist even in mock mode (mock fixtures are not a bypass)", async () => {
    vi.stubEnv("NODE_ENV", "test");
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
    const fetchMock = vi.fn() as unknown as FetchMock;
    vi.stubGlobal("fetch", fetchMock);

    const response = await GET(
      makeRequest("v1/widget/sessions", { method: "POST", body: "{}" }),
      contextFor("v1/widget/sessions")
    );

    expect(response.status).toBe(403);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// 2. SSE 401 -> refresh -> retry, cookies applied to the streamed response
// ---------------------------------------------------------------------------

describe("BFF SSE 401 refresh (W6-A)", () => {
  it("refreshes the token and streams with refreshed Set-Cookie headers", async () => {
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "false");

    let upstreamCalls = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/v1/auth/refresh")) {
        return new Response(
          JSON.stringify({
            access_token: "at-refreshed",
            refresh_token: "rt-refreshed",
            expires_in: 900,
          }),
          { status: 200, headers: { "content-type": "application/json" } }
        );
      }
      upstreamCalls += 1;
      if (upstreamCalls === 1) {
        return new Response(JSON.stringify({ code: "ERR_UNAUTHENTICATED" }), {
          status: 401,
          headers: { "content-type": "application/json" },
        });
      }
      const headers = new Headers(init?.headers ?? {});
      expect(headers.get("authorization")).toBe("Bearer at-refreshed");
      return new Response("event: connected\ndata: {}\n\n", {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      });
    }) as unknown as FetchMock;
    vi.stubGlobal("fetch", fetchMock);

    const response = await GET(
      makeRequest("v1/inbox/stream", {
        headers: {
          accept: "text/event-stream",
          cookie: "erp_access_token=at-expired; erp_refresh_token=rt-old",
        },
      }),
      contextFor("v1/inbox/stream")
    );

    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toContain("text/event-stream");
    expect(response.headers.get("cache-control")).toContain("no-transform");
    expect(response.headers.get("x-accel-buffering")).toBe("no");

    // Refreshed cookies ride on the SSE response itself.
    const setCookie = response.headers.get("set-cookie") ?? "";
    expect(setCookie).toContain("erp_access_token");
    expect(setCookie).toContain("at-refreshed");
    const cookies = new Map(
      (response as NextResponse).cookies.getAll().map((c) => [c.name, c.value])
    );
    expect(cookies.get("erp_access_token")).toBe("at-refreshed");
    expect(cookies.get("erp_refresh_token")).toBe("rt-refreshed");

    expect(await response.text()).toContain("event: connected");

    // 1st upstream 401 -> refresh -> 2nd upstream 200.
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(upstreamCalls).toBe(2);
  });

  it("treats a /stream path as SSE even without an Accept header", async () => {
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "false");

    const fetchMock = vi.fn(
      async () =>
        new Response("event: connected\ndata: {}\n\n", {
          status: 200,
          headers: { "content-type": "text/event-stream" },
        })
    ) as unknown as FetchMock;
    vi.stubGlobal("fetch", fetchMock);

    const response = await GET(
      makeRequest("v1/inbox/stream", {
        headers: { cookie: "erp_access_token=at-live" },
      }),
      contextFor("v1/inbox/stream")
    );

    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toContain("text/event-stream");
    expect(response.headers.get("x-accel-buffering")).toBe("no");
  });
});

// ---------------------------------------------------------------------------
// 3. useRealtimeInbox: status updates + reconnect reconciliation
// ---------------------------------------------------------------------------

describe("useRealtimeInbox (W6-A)", () => {
  class MockEventSource {
    static instances: MockEventSource[] = [];
    url: string;
    withCredentials?: boolean;
    listeners: Record<string, Array<(ev: Event) => void>> = {};
    close = vi.fn();

    constructor(url: string, init?: EventSourceInit) {
      this.url = url;
      this.withCredentials = init?.withCredentials;
      MockEventSource.instances.push(this);
    }

    addEventListener(type: string, fn: (ev: Event) => void) {
      this.listeners[type] = this.listeners[type] ?? [];
      this.listeners[type].push(fn);
    }

    removeEventListener() {}

    emit(type: string, ev: Event) {
      this.listeners[type]?.forEach((fn) => fn(ev));
    }
  }

  function renderInboxHook() {
    MockEventSource.instances = [];
    vi.stubGlobal("EventSource", MockEventSource);

    const queryClient = new QueryClient();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const wrapper = ({ children }: { children: React.ReactNode }) =>
      React.createElement(QueryClientProvider, { client: queryClient }, children);

    const rendered = renderHook(() => useRealtimeInbox(true), { wrapper });
    const source = MockEventSource.instances[0];
    expect(source).toBeDefined();
    return { ...rendered, source, invalidateSpy };
  }

  it("invalidates inbox queries on message.status_updated", () => {
    const { source, invalidateSpy, unmount } = renderInboxHook();

    source.emit(
      "message.status_updated",
      new MessageEvent("message.status_updated", {
        data: JSON.stringify({ conversation_id: "conv-42", status: "read" }),
      })
    );

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["inbox", "conversations"] });
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: ["inbox", "messages", "conv-42"],
    });
    unmount();
  });

  it("reconciles the inbox baseline on a reconnect open event", () => {
    const { source, invalidateSpy, unmount } = renderInboxHook();

    // First open: nothing was missed, so no refetch storm.
    source.emit("open", new Event("open"));
    expect(invalidateSpy).not.toHaveBeenCalledWith({ queryKey: ["inbox", "messages"] });

    invalidateSpy.mockClear();

    // Drop -> reconnect: missed Pub/Sub events are unrecoverable, refetch all.
    source.emit("error", new Event("error"));
    source.emit("open", new Event("open"));

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["inbox", "conversations"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["inbox", "messages"] });
    unmount();
  });
});

// ---------------------------------------------------------------------------
// 4. Edge auth middleware
// ---------------------------------------------------------------------------

describe("auth middleware (W6-A)", () => {
  it("redirects an unauthenticated /dashboard to /login?next=...", () => {
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "false");

    const response = middleware(middlewareRequest("/dashboard"));

    expect(response.status).toBe(307);
    const location = response.headers.get("location") ?? "";
    expect(location).toContain("/login?next=%2Fdashboard");
  });

  it("preserves the query string in the next parameter", () => {
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "false");

    const response = middleware(middlewareRequest("/inbox/conversations?tab=open"));
    const location = response.headers.get("location") ?? "";

    expect(response.status).toBe(307);
    expect(location).toContain("next=%2Finbox%2Fconversations%3Ftab%3Dopen");
  });

  it("allows public paths without a session", () => {
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "false");

    expect(isAllowedThrough(middleware(middlewareRequest("/login")))).toBe(true);
    expect(isAllowedThrough(middleware(middlewareRequest("/api/bff/v1/auth/me")))).toBe(true);
    expect(isAllowedThrough(middleware(middlewareRequest("/_next/static/chunk.js")))).toBe(true);
    expect(isAllowedThrough(middleware(middlewareRequest("/favicon.ico")))).toBe(true);
    expect(isAllowedThrough(middleware(middlewareRequest("/brand/logo.svg")))).toBe(true);
  });

  it("allows a request carrying erp_refresh_token (access token may be expired)", () => {
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "false");

    const withRefresh = middleware(
      middlewareRequest("/dashboard", { erp_refresh_token: "rt-live" })
    );
    expect(isAllowedThrough(withRefresh)).toBe(true);

    const withAccess = middleware(
      middlewareRequest("/dashboard", { erp_access_token: "at-live" })
    );
    expect(isAllowedThrough(withAccess)).toBe(true);
  });

  it("bypasses the gate entirely outside production when mock mode is on", () => {
    vi.stubEnv("NODE_ENV", "test");
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");

    expect(isAllowedThrough(middleware(middlewareRequest("/dashboard")))).toBe(true);
    expect(isAllowedThrough(middleware(middlewareRequest("/")))).toBe(true);
  });

  it("never bypasses in a production build even with the mock flag set", () => {
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");

    const response = middleware(middlewareRequest("/dashboard"));
    expect(response.status).toBe(307);
  });
});
