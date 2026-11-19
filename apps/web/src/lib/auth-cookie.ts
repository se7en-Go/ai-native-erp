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
