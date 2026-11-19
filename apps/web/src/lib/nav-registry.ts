import type { LucideIcon } from "lucide-react";
import {
  BarChart3,
  Briefcase,
  Building2,
  ClipboardList,
  Factory,
  FileText,
  Inbox,
  Landmark,
  LayoutDashboard,
  Package,
  PackageSearch,
  Settings,
  ShieldCheck,
  Sparkles,
  Truck,
  UserRound,
  Users,
  Wand2,
  Warehouse,
} from "lucide-react";

import type { UserRole } from "@/lib/contracts";
import type { I18nKey } from "@/lib/i18n";

/**
 * Single source of truth for navigation, RBAC visibility and the Ctrl+K palette.
 *
 * Adding a future vertical means adding one route group under `app/<module>/` and one
 * descriptor below — nothing in AppShell has to change.
 */
export type NavBadgeKey = "proposals";

export interface NavItem {
  href: string;
  labelKey: I18nKey;
  icon: LucideIcon;
  roles: UserRole[];
  badgeKey?: NavBadgeKey;
}

export interface NavSection {
  id: string;
  labelKey: I18nKey;
  items: NavItem[];
}

const ALL_ROLES: UserRole[] = ["owner", "admin", "manager", "agent", "finance"];
const SALES_ROLES: UserRole[] = ["owner", "admin", "manager", "agent"];
const OPS_ROLES: UserRole[] = ["owner", "admin", "manager", "finance"];

export const NAV_SECTIONS: NavSection[] = [
  {
    id: "core",
    labelKey: "navGroupCore",
    items: [
      { href: "/dashboard", labelKey: "navDashboard", icon: LayoutDashboard, roles: ALL_ROLES },
      { href: "/inbox", labelKey: "navInbox", icon: Inbox, roles: SALES_ROLES },
      { href: "/tasks", labelKey: "navTasks", icon: ClipboardList, roles: SALES_ROLES },
    ],
  },
  {
    id: "business",
    labelKey: "navGroupBusiness",
    items: [
      { href: "/b2b/quotes", labelKey: "navQuotes", icon: Briefcase, roles: SALES_ROLES },
      { href: "/crm/leads", labelKey: "navLeads", icon: UserRound, roles: SALES_ROLES },
      { href: "/crm/customers", labelKey: "navCustomers", icon: Users, roles: SALES_ROLES },
      {
        href: "/crm/opportunities",
        labelKey: "navOpportunities",
        icon: BarChart3,
        roles: SALES_ROLES,
      },
      { href: "/crm/followups", labelKey: "navFollowups", icon: FileText, roles: SALES_ROLES },
    ],
  },
  {
    id: "trade",
    labelKey: "navGroupTrade",
    items: [
      { href: "/catalog", labelKey: "navCatalog", icon: Package, roles: ALL_ROLES },
      { href: "/orders", labelKey: "navOrders", icon: Truck, roles: ALL_ROLES },
      {
        href: "/supply-chain/suppliers",
        labelKey: "navSuppliers",
        icon: Building2,
        roles: OPS_ROLES,
      },
      {
        href: "/supply-chain/purchase-orders",
        labelKey: "navPurchaseOrders",
        icon: Factory,
        roles: OPS_ROLES,
      },
      { href: "/wms", labelKey: "navWms", icon: Warehouse, roles: ALL_ROLES },
    ],
  },
  {
    id: "finance",
    labelKey: "navGroupFinance",
    items: [
      { href: "/finance", labelKey: "navFinance", icon: Landmark, roles: OPS_ROLES },
      { href: "/analytics", labelKey: "navAnalytics", icon: PackageSearch, roles: ALL_ROLES },
    ],
  },
  {
    id: "ai",
    labelKey: "navGroupAi",
    items: [
      {
        href: "/ai/proposals",
        labelKey: "navProposals",
        icon: Sparkles,
        roles: ALL_ROLES,
        badgeKey: "proposals",
      },
      {
        href: "/ai/capabilities",
        labelKey: "navCapabilities",
        icon: ShieldCheck,
        roles: ALL_ROLES,
      },
    ],
  },
  {
    id: "system",
    labelKey: "navGroupSystem",
    items: [
      { href: "/settings/channels", labelKey: "navSettings", icon: Settings, roles: ALL_ROLES },
      { href: "/onboarding", labelKey: "navOnboarding", icon: Wand2, roles: ["owner", "admin"] },
    ],
  },
];

export const ALL_NAV_ITEMS: NavItem[] = NAV_SECTIONS.flatMap((section) => section.items);

export function visibleNavSections(role: UserRole | null | undefined): NavSection[] {
  return NAV_SECTIONS.map((section) => ({
    ...section,
    items: section.items.filter((item) => !role || item.roles.includes(role)),
  })).filter((section) => section.items.length > 0);
}

export function isNavItemVisible(role: UserRole | null | undefined, href: string): boolean {
  const item = ALL_NAV_ITEMS.find((candidate) => candidate.href === href);
  return !item || !role || item.roles.includes(role);
}

export interface CommandEntry {
  id: string;
  group: "navigate" | "create";
  label: string;
  href: string;
  keywords: string;
  roles: UserRole[];
}

const CREATE_ACTIONS: Array<Omit<CommandEntry, "group">> = [
  {
    id: "create-lead",
    label: "新建线索 / New lead",
    href: "/crm/leads?action=new",
    keywords: "lead crm 线索",
    roles: SALES_ROLES,
  },
  {
    id: "create-followup",
    label: "新增跟进 / Log follow-up",
    href: "/crm/followups?action=new",
    keywords: "followup 跟进 crm",
    roles: SALES_ROLES,
  },
  {
    id: "create-task",
    label: "新建任务 / New task",
    href: "/tasks?action=new",
    keywords: "task reminder 任务 提醒",
    roles: SALES_ROLES,
  },
  {
    id: "create-supplier",
    label: "新建供应商 / New supplier",
    href: "/supply-chain/suppliers?action=new",
    keywords: "supplier 供应商 采购",
    roles: OPS_ROLES,
  },
  {
    id: "create-po",
    label: "新建采购单 / New purchase order",
    href: "/supply-chain/purchase-orders?action=new",
    keywords: "purchase order po 采购单",
    roles: OPS_ROLES,
  },
];

/** Navigation + quick-create entries the current role may run from Ctrl+K. */
export function commandPaletteEntries(role: UserRole | null | undefined): CommandEntry[] {
  const navEntries: CommandEntry[] = ALL_NAV_ITEMS.filter(
    (item) => !role || item.roles.includes(role)
  ).map((item) => ({
    id: `nav-${item.href}`,
    group: "navigate",
    label: item.href,
    href: item.href,
    keywords: item.href,
    roles: item.roles,
  }));
  const createEntries: CommandEntry[] = CREATE_ACTIONS.filter(
    (entry) => !role || entry.roles.includes(role)
  ).map((entry) => ({ ...entry, group: "create" }));
  return [...navEntries, ...createEntries];
}

/** Hyphens, slashes and underscores behave like spaces so "purchase order" matches the route. */
function normalizeSearchText(value: string): string {
  return value.toLowerCase().replace(/[-_/]+/g, " ").replace(/\s+/g, " ").trim();
}

export function filterCommandEntries(
  entries: CommandEntry[],
  query: string,
  labels: Record<string, string> = {}
): CommandEntry[] {
  const needle = normalizeSearchText(query);
  const decorated = entries.map((entry) => ({
    entry,
    searchable: normalizeSearchText(
      `${entry.label} ${labels[entry.href] ?? ""} ${entry.keywords}`
    ),
  }));
  return decorated
    .filter(({ searchable }) => !needle || searchable.includes(needle))
    .map(({ entry }) => entry);
}
