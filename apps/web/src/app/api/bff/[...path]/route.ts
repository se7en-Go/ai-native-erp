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
