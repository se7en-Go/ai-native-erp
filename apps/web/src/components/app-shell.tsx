"use client";

import React, { useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  ChevronDown,
  ChevronRight,
  LogOut,
  PanelLeftClose,
  PanelLeftOpen,
  Plus,
  Search,
  ShieldAlert,
  TrendingUp,
} from "lucide-react";
import { useAuthSession } from "@/components/providers";
import { CommandPalette, useCommandPalette } from "@/components/command-palette";
import { useRealtimeInbox } from "@/hooks/use-realtime-inbox";
import { canAccessRoute } from "@/lib/auth-cookie";
import { useI18n } from "@/lib/i18n";
import { ALL_NAV_ITEMS, NAV_SECTIONS, visibleNavSections } from "@/lib/nav-registry";

const ROLE_LABEL: Record<string, { zh: string; en: string }> = {
  owner: { zh: "企业所有者", en: "Owner" },
  admin: { zh: "管理员", en: "Admin" },
  manager: { zh: "业务经理", en: "Manager" },
  agent: { zh: "外贸业务员", en: "Sales agent" },
  finance: { zh: "财务", en: "Finance" },
};

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
  const { locale, toggleLocale, t, bi } = useI18n();
  const [collapsed, setCollapsed] = useState(false);
  const [collapsedSections, setCollapsedSections] = useState<Record<string, boolean>>({});
  const palette = useCommandPalette();

  useRealtimeInbox(Boolean(principal && canAccessRoute(principal.role, "/inbox")));

  const role = principal?.role ?? null;
  const sections = visibleNavSections(role);
  const allowedCurrentRoute = !role || canAccessRoute(role, pathname);

  // 面包屑：当前路由命中的导航条目 + 所属分组，全部走 i18n，不出现中英斜杠。
  const activeItem = ALL_NAV_ITEMS.filter((item) => pathname.startsWith(item.href)).sort(
    (a, b) => b.href.length - a.href.length
  )[0];
  const activeSection = NAV_SECTIONS.find((section) =>
    section.items.some((item) => item.href === activeItem?.href)
  );
  const pageTitle = activeItem ? t(activeItem.labelKey) : t("breadcrumbHome");
  const sectionTitle = activeSection ? t(activeSection.labelKey) : t("breadcrumbHome");
  const roleLabel = role ? ROLE_LABEL[role]?.[locale] ?? role : null;
  const userInitial = (principal?.email?.[0] ?? "Q").toUpperCase();
  const canCreateQuote = !role || canAccessRoute(role, "/b2b/quotes");

  return (
    <div className="flex min-h-screen bg-[#F1F4F9] text-slate-950">
      <aside
        data-testid="sidebar-nav"
        data-collapsed={collapsed ? "true" : "false"}
        className={`flex shrink-0 flex-col border-r border-slate-200/90 bg-[#F6F8FB] text-slate-800 transition-[width] duration-200 ${
          collapsed ? "w-16" : "w-64"
        }`}
      >
        <div className="flex h-16 items-center justify-between gap-2 border-b border-slate-200/80 px-3.5">
          {!collapsed && (
            <Link href="/dashboard" className="flex min-w-0 items-center gap-2.5">
              <span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-gradient-to-br from-blue-600 to-indigo-600 text-[13px] font-bold text-white shadow-sm shadow-blue-600/20">
                QZ
              </span>
              <span className="min-w-0">
                <span className="block truncate text-[14px] font-bold tracking-tight text-slate-950">
                  {t("brandTitle")}
                </span>
                <span className="block truncate text-[11px] font-medium text-slate-500">
                  {t("brandSubtitle")}
                </span>
              </span>
            </Link>
          )}
          <button
            type="button"
            data-testid="sidebar-collapse-btn"
            onClick={() => setCollapsed((prev) => !prev)}
            className="rounded-lg p-1.5 text-slate-500 transition-colors hover:bg-slate-200/70 hover:text-slate-950"
            aria-label={bi("收起或展开侧边栏", "Toggle sidebar")}
          >
            {collapsed ? (
              <PanelLeftOpen className="h-4 w-4" />
            ) : (
              <PanelLeftClose className="h-4 w-4" />
            )}
          </button>
        </div>

        <nav className="flex-1 space-y-2 overflow-y-auto px-2.5 py-3.5">
          {sections.map((section) => {
            const sectionCollapsed = Boolean(collapsedSections[section.id]);
            return (
              <div key={section.id} data-testid={`nav-section-${section.id}`} className="pb-0.5">
                {!collapsed && (
                  <button
                    type="button"
                    data-testid={`nav-section-toggle-${section.id}`}
                    aria-expanded={!sectionCollapsed}
                    onClick={() =>
                      setCollapsedSections((prev) => ({
                        ...prev,
                        [section.id]: !prev[section.id],
                      }))
                    }
                    className="mb-1 flex w-full items-center gap-1 rounded-md px-2.5 py-1 text-[11.5px] font-semibold tracking-wide text-slate-500 transition-colors hover:text-slate-900"
                  >
                    {sectionCollapsed ? (
                      <ChevronRight className="h-3 w-3 text-slate-400" />
                    ) : (
                      <ChevronDown className="h-3 w-3 text-slate-400" />
                    )}
                    {t(section.labelKey)}
                  </button>
                )}
                {!sectionCollapsed && (
                  <div className="space-y-0.5">
                    {section.items.map((item) => {
                      const Icon = item.icon;
                      const active = pathname.startsWith(item.href);
                      return (
                        <Link
                          key={item.href}
                          href={item.href}
                          aria-current={active ? "page" : undefined}
                          title={collapsed ? t(item.labelKey) : undefined}
                          className={`group relative flex items-center justify-between gap-2 rounded-xl px-3 py-2 text-[13.5px] transition-all duration-150 ${
                            active
                              ? "bg-[#EBF2FF] font-semibold text-[#1D4ED8] shadow-[inset_0_0_0_1px_rgba(37,99,235,0.14)]"
                              : "font-medium text-slate-700 hover:bg-slate-200/60 hover:text-slate-950"
                          }`}
                        >
                          <span className="flex min-w-0 items-center gap-2.5">
                            <Icon
                              className={`h-4 w-4 shrink-0 transition-colors ${
                                active ? "text-blue-600" : "text-slate-500 group-hover:text-slate-900"
                              }`}
                            />
                            {!collapsed && <span className="truncate">{t(item.labelKey)}</span>}
                          </span>
                          {!collapsed &&
                            item.badgeKey === "proposals" &&
                            pendingProposalsCount > 0 ? (
                            <span
                              data-testid="sidebar-pending-proposals-badge"
                              className="rounded-full bg-rose-500 px-2 py-0.5 text-[11px] font-bold tabular-nums text-white shadow-xs"
                            >
                              {pendingProposalsCount}
                            </span>
                          ) : !collapsed && active ? (
                            <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-blue-600" />
                          ) : null}
                        </Link>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
        </nav>

        <div className="border-t border-slate-200/90 bg-white/70 p-3">
          {!collapsed && principal && (
            <div className="mb-2.5 flex items-center gap-2.5">
              <div className="relative grid h-9 w-9 shrink-0 place-items-center rounded-full bg-blue-600 text-[13px] font-bold text-white shadow-xs">
                {userInitial}
                <span className="absolute -bottom-0.5 -right-0.5 h-2.5 w-2.5 rounded-full bg-emerald-500 ring-2 ring-white" />
              </div>
              <div className="min-w-0 flex-1">
                <div className="truncate text-[12.5px] font-semibold text-slate-900">
                  {principal.email}
                </div>
                <div className="mt-0.5 flex items-center gap-1.5 text-[11px] font-medium text-slate-500">
                  <span className="text-emerald-600">{bi("在线", "Online")}</span>
                  <span>·</span>
                  <span>{roleLabel}</span>
                </div>
              </div>
            </div>
          )}
          <button
            type="button"
            onClick={() => void logout()}
            className={`flex w-full items-center gap-2 rounded-xl border border-slate-200 bg-white px-2.5 py-1.5 text-[12px] font-medium text-slate-700 transition-colors hover:border-slate-300 hover:bg-slate-50 hover:text-slate-950 ${
              collapsed ? "justify-center" : ""
            }`}
            aria-label={t("logout")}
          >
            <LogOut className="h-3.5 w-3.5 text-slate-500" />
            {!collapsed && <span>{t("logout")}</span>}
          </button>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex h-16 items-center justify-between gap-3 border-b border-slate-200/90 bg-white/90 px-4 backdrop-blur-md lg:px-6">
          <div className="flex min-w-0 items-center gap-2.5 text-[13.5px]">
            <span className="hidden font-medium text-slate-500 sm:inline">{sectionTitle}</span>
            <ChevronRight className="hidden h-3.5 w-3.5 shrink-0 text-slate-400 sm:block" />
            <span className="truncate font-bold text-slate-950">{pageTitle}</span>
            <span className="ml-1 hidden items-center gap-1.5 rounded-full border border-slate-200 bg-slate-50 px-2.5 py-0.5 text-[11.5px] font-semibold tabular-nums text-slate-700 xl:inline-flex">
              <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
              <span>USD/CNY</span>
              <span className="text-emerald-700">7.1200</span>
              <TrendingUp className="h-3 w-3 text-emerald-600" />
            </span>
          </div>

          <div className="flex items-center gap-2.5">
            <button
              type="button"
              data-testid="open-command-palette-btn"
              onClick={palette.toggle}
              aria-label={bi("打开全局搜索", "Open global search")}
              className="flex items-center gap-2.5 rounded-full border border-slate-200/90 bg-slate-100/80 px-3.5 py-1.5 text-[12.5px] font-medium text-slate-600 transition-colors hover:border-slate-300 hover:bg-white hover:text-slate-950"
            >
              <Search className="h-3.5 w-3.5 text-slate-500" />
              <span className="hidden md:inline">{t("commandPaletteTitle")}</span>
              <kbd className="hidden rounded-md border border-slate-200 bg-white px-1.5 py-0.5 text-[10px] font-semibold text-slate-500 md:inline">
                ⌘K
              </kbd>
            </button>

            <button
              type="button"
              data-testid="locale-toggle-btn"
              onClick={toggleLocale}
              aria-label={bi("切换语言", "Switch language")}
              className="inline-flex items-center rounded-full border border-slate-200/90 bg-slate-100/90 p-0.5 text-[12px] font-semibold transition-colors hover:border-slate-300"
            >
              <span
                className={`rounded-full px-2.5 py-1 transition-all ${
                  locale === "zh"
                    ? "bg-white text-slate-950 shadow-xs"
                    : "text-slate-500"
                }`}
              >
                中
              </span>
              <span
                className={`rounded-full px-2.5 py-1 transition-all ${
                  locale === "en"
                    ? "bg-white text-slate-950 shadow-xs"
                    : "text-slate-500"
                }`}
              >
                EN
              </span>
            </button>

            {canCreateQuote && (
              <Link
                href="/b2b/quotes"
                className="hidden items-center gap-1.5 rounded-full bg-blue-600 px-3.5 py-1.5 text-[12.5px] font-semibold text-white shadow-xs shadow-blue-600/20 transition-colors hover:bg-blue-700 sm:inline-flex"
              >
                <Plus className="h-3.5 w-3.5" />
                <span>{bi("新建报价 / PI", "New quote / PI")}</span>
              </Link>
            )}

            {roleLabel && (
              <span className="hidden items-center gap-1.5 rounded-full border border-slate-200 bg-slate-50 px-3 py-1.5 text-[12px] font-semibold text-slate-700 2xl:inline-flex">
                {roleLabel}
              </span>
            )}
          </div>
        </header>

        <main className="min-w-0 flex-1">
          {isLoading ? (
            <div className="p-6 text-[13px] font-medium text-slate-700">{t("loadingWorkspace")}</div>
          ) : !allowedCurrentRoute ? (
            <div className="erp-page">
              <div
                data-testid="rbac-forbidden-guard"
                className="mx-auto mt-16 max-w-lg rounded-xl border border-rose-200 bg-rose-50 p-6 text-rose-900"
              >
                <div className="flex items-center gap-2 text-base font-semibold">
                  <ShieldAlert className="h-5 w-5 text-rose-600" />
                  <span>{t("accessDeniedTitle")}</span>
                </div>
                <p className="mt-2 text-[13px] leading-6 text-rose-800">{t("accessDeniedDesc")}</p>
              </div>
            </div>
          ) : (
            children
          )}
        </main>
      </div>

      <CommandPalette open={palette.open} onClose={() => palette.setOpen(false)} />
    </div>
  );
}
