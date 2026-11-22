"use client";

import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Bell,
  ChevronDown,
  ChevronRight,
  ChevronsRight,
  LogOut,
  Moon,
  Plus,
  Search,
  ShieldAlert,
  Sun,
  X,
} from "lucide-react";
import { useAuthSession } from "@/components/providers";
import { CommandPalette, useCommandPalette } from "@/components/command-palette";
import { LiveFxTicker } from "@/components/live-fx-ticker";
import { useRealtimeInbox } from "@/hooks/use-realtime-inbox";
import { canAccessRoute } from "@/lib/auth-cookie";
import { useI18n } from "@/lib/i18n";
import { ALL_NAV_ITEMS, NAV_SECTIONS, filterNavSections, visibleNavSections } from "@/lib/nav-registry";
import { useTheme } from "@/lib/theme";

const ROLE_LABEL: Record<string, { zh: string; en: string }> = {
  owner: { zh: "企业所有者", en: "Owner" },
  admin: { zh: "管理员", en: "Admin" },
  manager: { zh: "业务经理", en: "Manager" },
  agent: { zh: "外贸业务员", en: "Sales agent" },
  finance: { zh: "财务", en: "Finance" },
};

const SIDEBAR_SCROLL_KEY = "erp_sidebar_scroll_top";
const SIDEBAR_SECTIONS_KEY = "erp_sidebar_section_overrides";
const SIDEBAR_COLLAPSED_KEY = "erp_sidebar_collapsed";

const useIsomorphicLayoutEffect =
  typeof window !== "undefined" ? useLayoutEffect : useEffect;

function readSessionStorage(key: string): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeSessionStorage(key: string, value: string): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(key, value);
  } catch {
    // Ignore quota or disabled storage errors.
  }
}

function readSavedSectionOverrides(): Record<string, boolean> {
  const raw = readSessionStorage(SIDEBAR_SECTIONS_KEY);
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    const clean: Record<string, boolean> = {};
    for (const [key, val] of Object.entries(parsed)) {
      if (typeof val === "boolean") clean[key] = val;
    }
    return clean;
  } catch {
    return {};
  }
}

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
  const { isDark, toggleTheme } = useTheme();
  const [collapsed, setCollapsed] = useState(false);
  // Manual section overrides: `{ [sectionId]: true }` = expanded, `false` = collapsed.
  // Sections without an override fall back to their `defaultCollapsed` flag.
  const [sectionOverrides, setSectionOverrides] = useState<Record<string, boolean>>({});
  const [sidebarHydrated, setSidebarHydrated] = useState(false);
  const [filterQuery, setFilterQuery] = useState("");
  const navRef = useRef<HTMLElement | null>(null);
  const restoredPathRef = useRef<string | null>(null);
  const palette = useCommandPalette();

  useRealtimeInbox(Boolean(principal && canAccessRoute(principal.role, "/inbox")));

  const role = principal?.role ?? null;
  const allowedCurrentRoute = !role || canAccessRoute(role, pathname);

  const activeItem = ALL_NAV_ITEMS.filter((item) => pathname.startsWith(item.href)).sort(
    (a, b) => b.href.length - a.href.length
  )[0];
  const activeSection = NAV_SECTIONS.find((section) =>
    section.items.some((item) => item.href === activeItem?.href)
  );
  const activeSectionId = activeSection?.id;

  const persistSidebarScroll = useCallback(() => {
    if (!navRef.current) return;
    writeSessionStorage(SIDEBAR_SCROLL_KEY, String(Math.max(0, Math.round(navRef.current.scrollTop))));
  }, []);

  // Hydrate persisted sidebar UI state (collapsed rail + manual section toggles) on mount
  // so route navigation does not wipe user choices when page-level AppShell remounts.
  useIsomorphicLayoutEffect(() => {
    const savedCollapsed = readSessionStorage(SIDEBAR_COLLAPSED_KEY);
    if (savedCollapsed === "true") {
      setCollapsed(true);
    }
    const savedOverrides = readSavedSectionOverrides();
    if (Object.keys(savedOverrides).length > 0 || activeSectionId) {
      setSectionOverrides((previous) => {
        const merged = { ...savedOverrides, ...previous };
        if (activeSectionId) {
          merged[activeSectionId] = true;
        }
        return merged;
      });
    }
    setSidebarHydrated(true);
  }, [activeSectionId]);

  // Restore sidebar scroll position once per route transition after sections expand so
  // clicking lower items (e.g. AI 知识库) keeps the sidebar anchored instead of jumping to top.
  useIsomorphicLayoutEffect(() => {
    if (!sidebarHydrated || restoredPathRef.current === pathname) return;
    const navEl = navRef.current;
    if (!navEl) return;
    restoredPathRef.current = pathname;

    const rawScroll = readSessionStorage(SIDEBAR_SCROLL_KEY);
    if (rawScroll !== null) {
      const savedTop = Number(rawScroll);
      if (Number.isFinite(savedTop) && savedTop >= 0) {
        navEl.scrollTop = savedTop;
      }
    }
    const activeLink = navEl.querySelector<HTMLElement>('a[aria-current="page"]');
    if (activeLink && typeof activeLink.getBoundingClientRect === "function") {
      const navRect = navEl.getBoundingClientRect();
      const linkRect = activeLink.getBoundingClientRect();
      if (
        navRect.height > 0 &&
        (linkRect.top < navRect.top || linkRect.bottom > navRect.bottom)
      ) {
        activeLink.scrollIntoView?.({ block: "nearest" });
        persistSidebarScroll();
      }
    }
  }, [sidebarHydrated, pathname, persistSidebarScroll]);

  const visibleSections = useMemo(() => visibleNavSections(role), [role]);
  const isFiltering = filterQuery.trim().length > 0;
  const sections = useMemo(
    () => (isFiltering ? filterNavSections(visibleSections, filterQuery) : visibleSections),
    [isFiltering, visibleSections, filterQuery]
  );

  const pageTitle = activeItem ? t(activeItem.labelKey) : t("breadcrumbHome");
  const sectionTitle = activeSection ? t(activeSection.labelKey) : t("breadcrumbHome");
  const roleLabel = role ? ROLE_LABEL[role]?.[locale] ?? role : null;
  const userInitial = (principal?.email?.[0] ?? "Q").toUpperCase();
  const canCreateQuote = !role || canAccessRoute(role, "/b2b/quotes");
  const canViewProposals = !role || canAccessRoute(role, "/ai/proposals");

  return (
    <div className="flex min-h-screen w-full bg-gray-50 text-gray-900 transition-colors dark:bg-gray-950 dark:text-gray-100">
      <aside
        data-testid="sidebar-nav"
        data-collapsed={collapsed ? "true" : "false"}
        className={`sticky top-0 flex h-screen shrink-0 flex-col border-r border-gray-200 bg-white text-gray-700 shadow-xs transition-[width] duration-300 ease-in-out dark:border-gray-800 dark:bg-gray-900 dark:text-gray-300 ${
          collapsed ? "w-16" : "w-64"
        }`}
      >
        {/* Brand Header (21st.dev clean gradient mark + workspace title) */}
        <div className="border-b border-gray-200 px-3 py-3.5 dark:border-gray-800">
          <Link
            href="/dashboard"
            className={`flex items-center rounded-lg p-1.5 transition-colors hover:bg-gray-50 dark:hover:bg-gray-800 ${
              collapsed ? "justify-center" : "gap-3"
            }`}
          >
            <span className="relative grid h-9 w-9 shrink-0 place-content-center rounded-lg bg-gradient-to-br from-blue-500 to-blue-600 text-[13px] font-bold text-white shadow-sm">
              QZ
              <span className="absolute -top-0.5 -right-0.5 h-2.5 w-2.5 rounded-full bg-emerald-400 ring-2 ring-white dark:ring-gray-900" />
            </span>
            {!collapsed && (
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[13.5px] font-semibold text-gray-900 dark:text-gray-100">
                  {t("brandTitle")}
                </span>
                <span className="block truncate text-[11px] font-medium text-gray-500 dark:text-gray-400">
                  {t("brandSubtitle")}
                </span>
              </span>
            )}
          </Link>
        </div>

        {/* Live Module Search */}
        {!collapsed && (
          <div className="px-3 pt-3">
            <div className="relative flex items-center">
              <Search className="pointer-events-none absolute left-2.5 h-3.5 w-3.5 text-gray-400 dark:text-gray-500" />
              <input
                data-testid="sidebar-filter-input"
                type="search"
                value={filterQuery}
                onChange={(event) => setFilterQuery(event.target.value)}
                placeholder={bi("搜索功能模块…", "Filter modules…")}
                aria-label={bi("搜索功能模块", "Filter modules")}
                className="h-8 w-full rounded-lg border border-gray-200 bg-gray-50 pr-7 pl-8 text-[12.5px] font-medium text-gray-800 outline-none transition-colors placeholder:text-gray-400 focus:border-blue-500 focus:bg-white focus:ring-2 focus:ring-blue-100 dark:border-gray-700 dark:bg-gray-800/80 dark:text-gray-100 dark:placeholder:text-gray-500 dark:focus:border-blue-500 dark:focus:bg-gray-900 dark:focus:ring-blue-900/40 [&::-webkit-search-cancel-button]:hidden"
              />
              {filterQuery.length > 0 && (
                <button
                  type="button"
                  data-testid="sidebar-filter-clear"
                  onClick={() => setFilterQuery("")}
                  aria-label={bi("清除搜索", "Clear filter")}
                  className="absolute right-1.5 grid h-5 w-5 place-items-center rounded-md text-gray-400 transition-colors hover:bg-gray-200/70 hover:text-gray-800 dark:hover:bg-gray-700 dark:hover:text-gray-100"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              )}
            </div>
          </div>
        )}

        {/* 5-Group Navigation */}
        <nav
          ref={navRef}
          data-testid="sidebar-nav-scroll"
          onScroll={persistSidebarScroll}
          className="flex-1 space-y-2 overflow-y-auto px-2 py-3"
        >
          {sections.map((section) => {
            const containsActiveRoute = activeSectionId === section.id;
            const expanded =
              collapsed || isFiltering
                ? true
                : (sectionOverrides[section.id] ??
                  (containsActiveRoute || !section.defaultCollapsed));
            const showPendingBadge = section.id === "ai_system" && pendingProposalsCount > 0;
            return (
              <div
                key={section.id}
                data-testid={`nav-section-${section.id}`}
                data-expanded={expanded ? "true" : "false"}
                className="pb-0.5"
              >
                {!collapsed && (
                  <button
                    type="button"
                    data-testid={`nav-section-toggle-${section.id}`}
                    aria-expanded={expanded}
                    onClick={() =>
                      setSectionOverrides((previous) => {
                        const next = { ...previous, [section.id]: !expanded };
                        writeSessionStorage(SIDEBAR_SECTIONS_KEY, JSON.stringify(next));
                        return next;
                      })
                    }
                    className="mb-1 flex w-full items-center gap-1 rounded-md px-2.5 py-1 text-[11px] font-semibold tracking-wider text-gray-400 uppercase transition-colors hover:text-gray-700 dark:text-gray-500 dark:hover:text-gray-300"
                  >
                    {expanded ? (
                      <ChevronDown className="h-3 w-3 shrink-0 text-gray-400 dark:text-gray-500" />
                    ) : (
                      <ChevronRight className="h-3 w-3 shrink-0 text-gray-400 dark:text-gray-500" />
                    )}
                    <span className="flex-1 truncate text-left">{t(section.labelKey)}</span>
                    {!expanded && showPendingBadge && (
                      <span
                        data-testid="sidebar-group-pending-badge"
                        title={bi("待审核 AI 提案", "Pending AI proposals")}
                        className="rounded-full bg-blue-500 px-1.5 py-px text-[10px] font-bold tracking-normal text-white tabular-nums dark:bg-blue-600"
                      >
                        {pendingProposalsCount}
                      </span>
                    )}
                  </button>
                )}
                {expanded && (
                  <div className="space-y-0.5">
                    {section.items.map((item) => {
                      const Icon = item.icon;
                      const active = pathname.startsWith(item.href);
                      return (
                        <Link
                          key={item.href}
                          href={item.href}
                          onClick={persistSidebarScroll}
                          aria-current={active ? "page" : undefined}
                          title={collapsed ? t(item.labelKey) : undefined}
                          className={`group relative flex h-10 items-center justify-between gap-2 rounded-md px-2.5 text-[13.5px] transition-all duration-150 ${
                            active
                              ? "border-l-2 border-blue-500 bg-blue-50 font-semibold text-blue-700 shadow-xs dark:bg-blue-900/40 dark:text-blue-300"
                              : "font-medium text-gray-600 hover:bg-gray-50 hover:text-gray-900 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-100"
                          } ${collapsed ? "justify-center px-0" : ""}`}
                        >
                          <span
                            className={`flex min-w-0 items-center ${
                              collapsed ? "justify-center" : "gap-2.5"
                            }`}
                          >
                            <Icon
                              className={`h-4 w-4 shrink-0 transition-colors ${
                                active
                                  ? "text-blue-600 dark:text-blue-400"
                                  : "text-gray-400 group-hover:text-gray-700 dark:text-gray-500 dark:group-hover:text-gray-200"
                              }`}
                            />
                            {!collapsed && <span className="truncate">{t(item.labelKey)}</span>}
                          </span>
                          {!collapsed &&
                          item.badgeKey === "proposals" &&
                          pendingProposalsCount > 0 ? (
                            <span
                              data-testid="sidebar-pending-proposals-badge"
                              className="flex h-5 min-w-5 items-center justify-center rounded-full bg-blue-500 px-1.5 text-[11px] font-semibold text-white tabular-nums dark:bg-blue-600"
                            >
                              {pendingProposalsCount}
                            </span>
                          ) : null}
                        </Link>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
          {isFiltering && sections.length === 0 && (
            <p
              data-testid="sidebar-filter-empty"
              className="px-3 py-6 text-center text-[12px] font-medium text-gray-500 dark:text-gray-400"
            >
              {bi("没有匹配的模块", "No matching modules")}
            </p>
          )}
        </nav>

        {/* Compact Account Footer */}
        <div className="border-t border-gray-200 p-2.5 dark:border-gray-800">
          {!collapsed && principal && (
            <div className="mb-2 flex items-center gap-2.5 px-1">
              <div className="relative grid h-8 w-8 shrink-0 place-content-center rounded-full bg-blue-600 text-[12px] font-bold text-white shadow-xs">
                {userInitial}
                <span className="absolute -right-0.5 -bottom-0.5 h-2.5 w-2.5 rounded-full bg-emerald-500 ring-2 ring-white dark:ring-gray-900" />
              </div>
              <div className="min-w-0 flex-1">
                <div className="truncate text-[12px] font-semibold text-gray-900 dark:text-gray-100">
                  {principal.email}
                </div>
                <div className="flex items-center gap-1 text-[11px] font-medium text-gray-500 dark:text-gray-400">
                  <span className="text-emerald-600 dark:text-emerald-400">{bi("在线", "Online")}</span>
                  <span>·</span>
                  <span>{roleLabel}</span>
                </div>
              </div>
            </div>
          )}
          <button
            type="button"
            onClick={() => void logout()}
            className={`flex w-full items-center gap-2 rounded-lg border border-gray-200 bg-white px-2.5 py-1.5 text-[12px] font-medium text-gray-600 transition-colors hover:bg-gray-50 hover:text-gray-900 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300 dark:hover:bg-gray-800 dark:hover:text-gray-100 ${
              collapsed ? "justify-center" : ""
            }`}
            aria-label={t("logout")}
          >
            <LogOut className="h-3.5 w-3.5 text-gray-400 dark:text-gray-500" />
            {!collapsed && <span>{t("logout")}</span>}
          </button>
        </div>

        {/* 21st.dev Bottom Collapsible Toggle */}
        <button
          type="button"
          data-testid="sidebar-collapse-btn"
          onClick={() =>
            setCollapsed((prev) => {
              const next = !prev;
              writeSessionStorage(SIDEBAR_COLLAPSED_KEY, String(next));
              return next;
            })
          }
          aria-label={bi("收起或展开侧边栏", "Toggle sidebar")}
          className="flex items-center border-t border-gray-200 px-3 py-2.5 text-gray-600 transition-colors hover:bg-gray-50 hover:text-gray-900 dark:border-gray-800 dark:text-gray-300 dark:hover:bg-gray-800 dark:hover:text-gray-100"
        >
          <span className="grid h-7 w-10 shrink-0 place-content-center">
            <ChevronsRight
              className={`h-4 w-4 text-gray-500 transition-transform duration-300 dark:text-gray-400 ${
                collapsed ? "" : "rotate-180"
              }`}
            />
          </span>
          {!collapsed && (
            <span className="text-[13px] font-medium">{bi("收起侧栏", "Hide")}</span>
          )}
        </button>
      </aside>

      {/* Main Content Shell */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex h-14 items-center justify-between gap-3 border-b border-gray-200 bg-white/90 px-4 backdrop-blur-md lg:px-7 dark:border-gray-800 dark:bg-gray-900/90">
          <div className="flex min-w-0 items-center gap-2 text-[13.5px]">
            <span className="hidden font-medium text-gray-500 sm:inline dark:text-gray-400">
              {sectionTitle}
            </span>
            <ChevronRight className="hidden h-3.5 w-3.5 shrink-0 text-gray-400 sm:block dark:text-gray-600" />
            <span className="truncate font-semibold text-gray-900 dark:text-gray-100">
              {pageTitle}
            </span>
            <LiveFxTicker enabled={Boolean(principal)} />
          </div>

          <div className="flex items-center gap-2">
            <button
              type="button"
              data-testid="open-command-palette-btn"
              onClick={palette.toggle}
              aria-label={bi("打开全局搜索", "Open global search")}
              className="flex h-9 items-center gap-2 rounded-lg border border-gray-200 bg-gray-50 px-3 text-[12.5px] font-medium text-gray-600 transition-colors hover:border-gray-300 hover:bg-white hover:text-gray-900 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700 dark:hover:text-gray-100"
            >
              <Search className="h-3.5 w-3.5 text-gray-400" />
              <span className="hidden md:inline">{t("commandPaletteTitle")}</span>
              <kbd className="hidden rounded border border-gray-200 bg-white px-1.5 py-0.5 text-[10px] font-semibold text-gray-500 md:inline dark:border-gray-600 dark:bg-gray-900 dark:text-gray-400">
                ⌘K
              </kbd>
            </button>

            {canViewProposals && (
              <Link
                href="/ai/proposals"
                data-testid="header-notifications-link"
                aria-label={bi("通知提醒", "Notifications")}
                title={bi("审批与巡检通知", "Approval & patrol notifications")}
                className="relative grid h-9 w-9 place-content-center rounded-lg border border-gray-200 bg-white text-gray-600 transition-colors hover:bg-gray-50 hover:text-gray-900 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300 dark:hover:bg-gray-800 dark:hover:text-gray-100"
              >
                <Bell className="h-4 w-4" />
                {pendingProposalsCount > 0 && (
                  <span className="absolute -top-1 -right-1 h-2.5 w-2.5 rounded-full bg-rose-500 ring-2 ring-white dark:ring-gray-900" />
                )}
              </Link>
            )}

            <button
              type="button"
              data-testid="theme-toggle-btn"
              onClick={toggleTheme}
              aria-label={bi(
                isDark ? "切换浅色主题" : "切换深色主题",
                isDark ? "Switch to light mode" : "Switch to dark mode"
              )}
              title={bi(
                isDark ? "切换浅色主题" : "切换深色主题",
                isDark ? "Switch to light mode" : "Switch to dark mode"
              )}
              className="grid h-9 w-9 place-content-center rounded-lg border border-gray-200 bg-white text-gray-600 transition-colors hover:bg-gray-50 hover:text-gray-900 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300 dark:hover:bg-gray-800 dark:hover:text-gray-100"
            >
              {isDark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
            </button>

            <button
              type="button"
              data-testid="locale-toggle-btn"
              onClick={toggleLocale}
              aria-label={bi("切换语言", "Switch language")}
              className="inline-flex h-9 items-center rounded-lg border border-gray-200 bg-gray-50 p-0.5 text-[12px] font-semibold transition-colors hover:border-gray-300 dark:border-gray-700 dark:bg-gray-800"
            >
              <span
                className={`rounded-md px-2 py-1 transition-all ${
                  locale === "zh"
                    ? "bg-blue-600 text-white shadow-xs"
                    : "text-gray-500 dark:text-gray-400"
                }`}
              >
                中
              </span>
              <span
                className={`rounded-md px-2 py-1 transition-all ${
                  locale === "en"
                    ? "bg-blue-600 text-white shadow-xs"
                    : "text-gray-500 dark:text-gray-400"
                }`}
              >
                EN
              </span>
            </button>

            {canCreateQuote && (
              <Link
                href="/b2b/quotes"
                className="hidden h-9 items-center gap-1.5 rounded-lg bg-blue-600 px-3.5 text-[12.5px] font-semibold text-white shadow-xs transition-colors hover:bg-blue-700 sm:inline-flex"
              >
                <Plus className="h-3.5 w-3.5" />
                <span>{bi("新建报价 / PI", "New quote / PI")}</span>
              </Link>
            )}

            {roleLabel && (
              <span className="hidden items-center rounded-lg border border-gray-200 bg-gray-50 px-2.5 py-1.5 text-[12px] font-medium text-gray-600 2xl:inline-flex dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300">
                {roleLabel}
              </span>
            )}
          </div>
        </header>

        <main className="min-w-0 flex-1">
          {isLoading ? (
            <div className="p-6 text-[13px] font-medium text-gray-600 dark:text-gray-300">
              {t("loadingWorkspace")}
            </div>
          ) : !allowedCurrentRoute ? (
            <div className="erp-page">
              <div
                data-testid="rbac-forbidden-guard"
                className="mx-auto mt-16 max-w-lg rounded-xl border border-rose-200 bg-rose-50 p-6 text-rose-900 dark:border-rose-900/50 dark:bg-rose-950/50 dark:text-rose-200"
              >
                <div className="flex items-center gap-2 text-base font-semibold">
                  <ShieldAlert className="h-5 w-5 text-rose-600 dark:text-rose-400" />
                  <span>{t("accessDeniedTitle")}</span>
                </div>
                <p className="mt-2 text-[13px] leading-6 text-rose-800 dark:text-rose-300">
                  {t("accessDeniedDesc")}
                </p>
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
