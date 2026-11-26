import { NextRequest, NextResponse } from "next/server";

/**
 * Public B2B buyer portal proxy.
 *
 * The operator BFF (`/api/bff/*`) is a *narrow authenticated* surface: buying
 * companies hold no ERP session, so a buyer link must never be routed through it.
 * This route is the mirror image — a deliberately tiny, unauthenticated proxy that
 * can only ever reach `/api/v1/portal/<token>...` on the backend:
 *
 *   GET  /api/portal/<token>                     -> view + quote + order
 *   POST /api/portal/<token>/accept              -> accept the quote
 *   POST /api/portal/<token>/receipt             -> upload a T/T bank slip
 *   GET  /api/portal/<token>/documents/<pi|ci|pl> -> PDF bytes
 *
 * Everything else (webhooks, docs, the operator API, any other namespace) is
 * refused before a socket is opened, so this proxy cannot become an SSRF pivot.
 */

const BACKEND_BASE = (process.env.BFF_BACKEND_URL ?? "http://localhost:8000").replace(/\/$/, "");

const PORTAL_TOKEN_PATTERN = /^erp_b2b_[A-Za-z0-9_-]+$/;
const PORTAL_ACTIONS = new Set(["accept", "receipt"]);
const PORTAL_DOCUMENT_TYPES = new Set(["pi", "ci", "pl"]);

export type PortalPathPredicate = (segments: string[]) => string | null;

/**
 * Map decoded catch-all segments onto the one backend namespace this proxy serves.
 * Returns `null` for anything that is not a well-formed portal path.
 */
function resolvePortalUpstreamPath(segments: string[]): string | null {
  if (!segments || segments.length === 0 || segments.length > 3) return null;
  for (const segment of segments) {
    if (
      !segment ||
      segment === "." ||
      segment === ".." ||
      segment.includes("..") ||
      segment.includes("/") ||
      segment.includes("\\") ||
      segment.includes("://")
    ) {
      return null;
    }
  }
  const [token, ...rest] = segments;
  if (!PORTAL_TOKEN_PATTERN.test(token)) return null;
  const base = `/api/v1/portal/${token}`;
  if (rest.length === 0) return base;
  if (rest.length === 1 && PORTAL_ACTIONS.has(rest[0])) return `${base}/${rest[0]}`;
  if (
    rest.length === 2 &&
    rest[0] === "documents" &&
    PORTAL_DOCUMENT_TYPES.has(rest[1])
  ) {
    return `${base}/documents/${rest[1]}`;
  }
  return null;
}

function problem(
  status: number,
  title: string,
  code: string,
  detail: string
): NextResponse {
  return NextResponse.json(
    {
      type: `https://erp.local/errors/${code.toLowerCase().replace(/_/g, "-")}`,
      title,
      status,
      detail,
      code,
    },
    { status }
  );
}

type PortalRouteContext = { params: Promise<{ path: string[] }> };

async function handleProxy(request: NextRequest, context: PortalRouteContext): Promise<Response> {
  const { path } = await context.params;
  const upstreamPath = resolvePortalUpstreamPath(path ?? []);
  if (!upstreamPath) {
    return problem(
      403,
      "Forbidden Portal Path",
      "ERR_PORTAL_PATH_FORBIDDEN",
      "Only /api/portal/<magic-link-token>/... buyer routes are proxied."
    );
  }

  const forwardHeaders = new Headers();
  const contentType = request.headers.get("content-type");
  if (contentType) forwardHeaders.set("content-type", contentType);
  const accept = request.headers.get("accept");
  if (accept) forwardHeaders.set("accept", accept);

  const hasBody = !["GET", "HEAD"].includes(request.method);
  const rawBody = hasBody ? await request.arrayBuffer() : undefined;

  let upstream: Response;
  try {
    upstream = await fetch(`${BACKEND_BASE}${upstreamPath}${request.nextUrl.search}`, {
      method: request.method,
      headers: forwardHeaders,
      body: rawBody,
      cache: "no-store",
      redirect: "manual",
    });
  } catch {
    return problem(
      502,
      "Portal Upstream Unavailable",
      "ERR_PORTAL_UPSTREAM_UNAVAILABLE",
      "The ERP backend could not be reached for this buyer portal request."
    );
  }

  const responseHeaders = new Headers();
  const upstreamContentType = upstream.headers.get("content-type");
  if (upstreamContentType) responseHeaders.set("content-type", upstreamContentType);
  const contentDisposition = upstream.headers.get("content-disposition");
  if (contentDisposition) responseHeaders.set("content-disposition", contentDisposition);
  // A magic link is a bearer credential: never let a shared cache store the payload.
  responseHeaders.set("cache-control", "no-store");
  responseHeaders.set("x-robots-tag", "noindex, nofollow");

  return new NextResponse(upstream.body, {
    status: upstream.status,
    headers: responseHeaders,
  });
}

type PortalRouteHandler = ((req: NextRequest, ctx: PortalRouteContext) => Promise<Response>) & {
  resolvePortalUpstreamPath: PortalPathPredicate;
};

/**
 * Next.js only allows HTTP verbs plus segment config to be exported from a route
 * module, so the pure predicate rides on the GET handler (same pattern as the
 * operator BFF), which keeps it unit-testable without a proxy round-trip.
 */
export const GET: PortalRouteHandler = Object.assign(
  (req: NextRequest, ctx: PortalRouteContext) => handleProxy(req, ctx),
  { resolvePortalUpstreamPath }
);

export async function POST(req: NextRequest, ctx: PortalRouteContext) {
  return handleProxy(req, ctx);
}
