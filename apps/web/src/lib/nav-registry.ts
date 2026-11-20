import type { LucideIcon } from "lucide-react";
import {
  BarChart3,
  Bot,
  Briefcase,
  Building2,
  ClipboardList,
  Factory,
  FileText,
  Inbox,
  Landmark,
  MapPinned,
  Megaphone,
  LayoutDashboard,
  Package,
  PackageSearch,
  Palette,
  Settings,
  Share2,
  ShieldCheck,
  Sparkles,
  Truck,
  UserRound,
  Users,
  Wand2,
  Warehouse,
} from "lucide-react";

import type { UserRole } from "@/lib/contracts";
import { allLocaleLabels, type I18nKey } from "@/lib/i18n";

/**
 * Single source of truth for navigation, RBAC visibility and the Ctrl+K palette.
 *
 * Adding a future vertical means adding one route group under `app/<module>/` and one
 * descriptor below — nothing in AppShell has to change.
 *
 * The sidebar is deliberately shaped like Nova ERP: five plain-language groups, two of
 * them collapsed by default, plus a quick filter that searches every group at once.
 */
export type NavBadgeKey = "proposals";

export interface NavItem {
  href: string;
  labelKey: I18nKey;
  icon: LucideIcon;
  roles: UserRole[];
  badgeKey?: NavBadgeKey;
  /**
   * Extra search terms (Chinese + English) for the sidebar quick filter, so
   * "报价单", "pi" or "quotation" all find /b2b/quotes.
   */
  keywords?: string;
}

export interface NavSection {
  id: string;
  labelKey: I18nKey;
  items: NavItem[];
  /**
   * Rendered collapsed on first paint. The AppShell still auto-expands the section
   * whenever the active route lives inside it, so the current page is never hidden.
   */
  defaultCollapsed?: boolean;
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
      {
        href: "/b2b/quotes",
        labelKey: "navQuotes",
        icon: Briefcase,
        roles: SALES_ROLES,
        keywords: "quote quotation pi 报价 报价单 报盘 proforma",
      },
      {
        href: "/crm/leads",
        labelKey: "navLeads",
        icon: UserRound,
        roles: SALES_ROLES,
        keywords: "lead prospect 线索 潜客",
      },
      {
        href: "/crm/customers",
        labelKey: "navCustomers",
        icon: Users,
        roles: SALES_ROLES,
        keywords: "customer buyer account 客户 买家",
      },
      {
        href: "/crm/opportunities",
        labelKey: "navOpportunities",
        icon: BarChart3,
        roles: SALES_ROLES,
        keywords: "opportunity deal pipeline 商机 漏斗",
      },
      {
        href: "/crm/followups",
        labelKey: "navFollowups",
        icon: FileText,
        roles: SALES_ROLES,
        keywords: "followup follow up activity 跟进",
      },
    ],
  },
  {
    id: "trade",
    labelKey: "navGroupTrade",
    items: [
      {
        href: "/catalog",
        labelKey: "navCatalog",
        icon: Package,
        roles: ALL_ROLES,
        keywords: "catalog sku product 商品 产品 价格",
      },
      {
        href: "/orders",
        labelKey: "navOrders",
        icon: Truck,
        roles: ALL_ROLES,
        keywords: "order tracking 17track logistics 订单 物流 运单",
      },
      {
        href: "/wms",
        labelKey: "navWms",
        icon: Warehouse,
        roles: ALL_ROLES,
        keywords: "wms inventory stock warehouse 库存 出入库 仓库",
      },
      {
        href: "/supply-chain/suppliers",
        labelKey: "navSuppliers",
        icon: Building2,
        roles: OPS_ROLES,
        keywords: "supplier vendor 供应商 采购",
      },
      {
        href: "/supply-chain/purchase-orders",
        labelKey: "navPurchaseOrders",
        icon: Factory,
        roles: OPS_ROLES,
        keywords: "purchase order po sourcing 采购 采购单",
      },
    ],
  },
  {
    id: "growth_finance",
    labelKey: "navGroupGrowthFinance",
    defaultCollapsed: true,
    items: [
      {
        href: "/finance",
        labelKey: "navFinance",
        icon: Landmark,
        roles: OPS_ROLES,
        keywords: "finance cvp margin profit cogs 财务 毛利 成本 利润",
      },
      {
        href: "/analytics",
        labelKey: "navAnalytics",
        icon: PackageSearch,
        roles: ALL_ROLES,
        keywords: "analytics report stats 分析 统计 报表",
      },
      {
        href: "/growth/collect",
        labelKey: "navGrowthCollect",
        icon: MapPinned,
        roles: SALES_ROLES,
        keywords: "lead collect map prospecting 获客 地图 拓客",
      },
      {
        href: "/growth/campaigns",
        labelKey: "navGrowthCampaigns",
        icon: Megaphone,
        roles: SALES_ROLES,
        keywords: "campaign marketing outreach 营销 战役 群发",
      },
      {
        href: "/social",
        labelKey: "navSocial",
        icon: Share2,
        roles: SALES_ROLES,
        keywords: "social matrix facebook instagram tiktok 社媒 矩阵",
      },
    ],
  },
  {
    id: "ai_system",
    labelKey: "navGroupAiSystem",
    defaultCollapsed: true,
    items: [
      {
        href: "/ai/proposals",
        labelKey: "navProposals",
        icon: Sparkles,
        roles: ALL_ROLES,
        badgeKey: "proposals",
        keywords: "ai proposal approval 提案 审核 建议",
      },
      {
        href: "/ai/employees",
        labelKey: "navEmployees",
        icon: Bot,
        roles: ALL_ROLES,
        keywords: "ai employee agent workforce 员工 数字员工",
      },
      {
        href: "/ai/studio",
        labelKey: "navStudio",
        icon: Palette,
        roles: ALL_ROLES,
        keywords: "ai studio content copy 素材 文案 生成",
      },
      {
        href: "/ai/capabilities",
        labelKey: "navCapabilities",
        icon: ShieldCheck,
        roles: ALL_ROLES,
        keywords: "ai governance knowledge base rag 知识库 治理",
      },
      {
        href: "/settings/channels",
        labelKey: "navSettings",
        icon: Settings,
        roles: ALL_ROLES,
        keywords: "settings channel telegram whatsapp email 渠道 设置 绑定",
      },
      {
        href: "/onboarding",
        labelKey: "navOnboarding",
        icon: Wand2,
        roles: ["owner", "admin"],
        keywords: "onboarding wizard setup 入驻 向导 初始化",
      },
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

/** Hyphens, slashes, ampersands and underscores behave like spaces for the sidebar filter. */
function normalizeNavQuery(value: string): string {
  return value
    .toLowerCase()
    .replace(/[-_/&]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

/**
 * Sidebar quick filter. Matches against the Chinese label, the English label, the
 * extra keywords and the route itself, so one query works in either UI language.
 * Groups with no surviving item disappear; the caller renders the survivors expanded.
 */
export function filterNavSections(sections: NavSection[], query: string): NavSection[] {
  const needle = normalizeNavQuery(query);
  if (!needle) return sections;
  return sections
    .map((section) => ({
      ...section,
      items: section.items.filter((item) => {
        const haystack = normalizeNavQuery(
          `${allLocaleLabels(item.labelKey)} ${item.keywords ?? ""} ${item.href}`
        );
        return haystack.includes(needle);
      }),
    }))
    .filter((section) => section.items.length > 0);
}

export interface CommandEntry {
  id: string;
  group: "navigate" | "create";
  /** Chinese label (zh mode). */
  label: string;
  /** English label for en mode; falls back to `label` when absent. */
  labelEn?: string;
  href: string;
  keywords: string;
  roles: UserRole[];
}

const CREATE_ACTIONS: Array<Omit<CommandEntry, "group">> = [
  {
    id: "create-lead",
    label: "新建线索",
    labelEn: "New lead",
    href: "/crm/leads?action=new",
    keywords: "lead crm 线索",
    roles: SALES_ROLES,
  },
  {
    id: "create-followup",
    label: "新增跟进",
    labelEn: "Log follow-up",
    href: "/crm/followups?action=new",
    keywords: "followup 跟进 crm",
    roles: SALES_ROLES,
  },
  {
    id: "create-task",
    label: "新建任务",
    labelEn: "New task",
    href: "/tasks?action=new",
    keywords: "task reminder 任务 提醒",
    roles: SALES_ROLES,
  },
  {
    id: "create-supplier",
    label: "新建供应商",
    labelEn: "New supplier",
    href: "/supply-chain/suppliers?action=new",
    keywords: "supplier 供应商 采购",
    roles: OPS_ROLES,
  },
  {
    id: "create-po",
    label: "新建采购单",
    labelEn: "New purchase order",
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
      `${entry.label} ${entry.labelEn ?? ""} ${labels[entry.href] ?? ""} ${entry.keywords}`
    ),
  }));
  return decorated
    .filter(({ searchable }) => !needle || searchable.includes(needle))
    .map(({ entry }) => entry);
}
