import { NextRequest, NextResponse } from "next/server";

import { ACCESS_COOKIE_NAME, REFRESH_COOKIE_NAME } from "@/lib/auth-cookie";

/**
 * Edge auth gate (Wave 6 / W6-A).
 *
 * The app shell is a client-rendered surface, so an unauthenticated deep link
 * used to paint the dashboard chrome, fire a burst of 401s and only *then*
 * bounce to /login. This middleware redirects before any RSC/HTML is rendered.
 *
 * It is deliberately a *cheap presence check*, not authorization: a forged
 * cookie still fails every backend call (RLS + JWT validation); the goal here
 * is a clean unauthenticated redirect and no flash of protected chrome.
 */

const PUBLIC_PATHS = new Set(["/login", "/favicon.ico"]);

const PUBLIC_PREFIXES = [
  "/api/",
  "/_next/",
  // B2B buyer self-service portal: the opaque magic-link token in the path is the
  // only credential, so these pages must render for buyers who hold no session.
  "/portal/",
];

const STATIC_FILE_EXTENSIONS = [
  ".png",
  ".jpg",
  ".jpeg",
  ".svg",
  ".webp",
  ".ico",
  ".css",
  ".js",
  ".woff",
  ".woff2",
  ".txt",
  ".map",
];

function isMockModeBypass(env: NodeJS.ProcessEnv = process.env): boolean {
  // Mirrors the BFF mock-mode gate: mock mode is only ever legal outside
  // production, so the e2e-mock suite keeps 100% green while a production
  // build can never disable auth through a public build flag.
  if (env.NODE_ENV === "production") return false;
  return env.NEXT_PUBLIC_USE_MOCK_API === "true";
}

function isPublicPath(pathname: string): boolean {
  if (PUBLIC_PATHS.has(pathname)) return true;
  if (PUBLIC_PREFIXES.some((prefix) => pathname.startsWith(prefix))) return true;
  const lower = pathname.toLowerCase();
  return STATIC_FILE_EXTENSIONS.some((ext) => lower.endsWith(ext));
}

function shouldRedirectToLogin(
  pathname: string,
  hasAuthCookie: boolean,
  env: NodeJS.ProcessEnv = process.env
): boolean {
  if (isMockModeBypass(env)) return false;
  if (isPublicPath(pathname)) return false;
  return !hasAuthCookie;
}

type AuthMiddleware = ((request: NextRequest) => NextResponse) & {
  shouldRedirectToLogin: typeof shouldRedirectToLogin;
  isPublicPath: typeof isPublicPath;
};

/**
 * Next.js allows only `middleware`/(`default`) and `config` exports from this
 * module, so the pure predicates ride on the middleware function itself —
 * directly unit-testable (`middleware.shouldRedirectToLogin(...)`) without a
 * Next runtime, while `next build` stays happy.
 */
export const middleware: AuthMiddleware = Object.assign(
  (request: NextRequest): NextResponse => {
    const { pathname, search } = request.nextUrl;
    const hasAuthCookie =
      request.cookies.has(ACCESS_COOKIE_NAME) || request.cookies.has(REFRESH_COOKIE_NAME);

    if (!shouldRedirectToLogin(pathname, hasAuthCookie)) {
      return NextResponse.next();
    }

    const loginUrl = new URL("/login", request.url);
    loginUrl.searchParams.set("next", `${pathname}${search}`);
    return NextResponse.redirect(loginUrl, 307);
  },
  { shouldRedirectToLogin, isPublicPath }
);

export const config = {
  // Skip API routes (the BFF enforces auth itself), Next internals and the
  // favicon; everything else goes through the cookie presence check.
  matcher: ["/((?!api|_next/static|_next/image|favicon.ico).*)"],
};
