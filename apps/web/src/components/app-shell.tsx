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
    // Nav link stays visible to every signed-in role; the /settings route
    // itself is still guarded by canAccessRoute (owner/admin only).
    roles: ["owner", "admin", "manager", "agent", "finance"],
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
