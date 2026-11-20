"use client";

import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

export type Locale = "zh" | "en";

/** Persisted language preference. Shared with AppShell and the onboarding wizard. */
export const LOCALE_STORAGE_KEY = "erp_locale";

const DICTIONARY = {
  zh: {
    brandTitle: "工贸全渠道工作台",
    brandSubtitle: "Quanzhou Smart LED · 外贸企业版",
    navInbox: "聚合沟通",
    navProposals: "AI 提案",
    navQuotes: "快速报价",
    navCatalog: "产品库与 SKU",
    navOrders: "订单与物流",
    navFinance: "财务与毛利",
    navSettings: "渠道中心",
    navCapabilities: "AI 知识库",
    navOnboarding: "入驻向导",
    navGroupCore: "常用工作台",
    navGroupBusiness: "业务与客户",
    navGroupTrade: "产品与库存",
    navGroupGrowthFinance: "财务与增长",
    navGroupAiSystem: "AI 与设置",
    navEmployees: "AI 员工",
    navStudio: "AI 素材",
    navGrowthCollect: "海外获客",
    navGrowthCampaigns: "营销战役",
    navSocial: "社媒营销",
    navDashboard: "经营总览",
    navTasks: "任务提醒",
    navLeads: "线索池",
    navCustomers: "客户管理",
    navOpportunities: "商机看板",
    navFollowups: "跟进记录",
    navSuppliers: "供应商",
    navPurchaseOrders: "采购订单",
    navWms: "WMS 库存",
    navAnalytics: "统计分析",
    commandPaletteTitle: "全局搜索与命令",
    commandPalettePlaceholder: "搜索客户、订单、报价或 SKU，也可直接跳转页面…",
    commandPaletteEmpty: "没有匹配结果",
    commandPaletteHint: "Ctrl+K 打开，Esc 关闭",
    logout: "退出登录",
    roleLabel: "当前角色",
    workspaceLabel: "当前工作区",
    breadcrumbHome: "工作台",
    accessDeniedTitle: "权限不足 (ERR_INSUFFICIENT_ROLE)",
    accessDeniedDesc: "当前角色无权访问该财务或系统敏感页面，请联系 Owner 或 Admin。",
    quietHours: "休息时段",
    localTime: "当地时间",
    strictRag: "仅检索模式",
    globalMode: "全局引导模式",
    send: "发送回复",
    internalNote: "内部备注",
    liveTranslate: "实时翻译",
    aiDraft: "AI 起草",
    aiPolish: "AI 润色",
    aiSummarize: "会话摘要",
    showTranslation: "显示译文",
    hideTranslation: "收起译文",
    pendingApprovals: "待审核提案",
    aiAssistFailed: "AI 服务暂不可用",
    aiProviderUnavailable:
      "未配置大模型供应商密钥，AI 生成暂不可用（ERR_AI_PROVIDER_UNAVAILABLE）。",
    evidence: "证据",
    loadingWorkspace: "正在加载工作区…",
    exportCsv: "导出报表",
  },
  en: {
    brandTitle: "Hybrid Trade Workbench",
    brandSubtitle: "Quanzhou Smart LED · Enterprise",
    navInbox: "Inbox",
    navProposals: "AI Proposals",
    navQuotes: "B2B CRM & Quotes",
    navCatalog: "Catalog",
    navOrders: "Orders & Tracking",
    navFinance: "Finance & CVP",
    navSettings: "Channels & Settings",
    navCapabilities: "AI Governance",
    navOnboarding: "Onboarding",
    navGroupCore: "Core Workspace",
    navGroupBusiness: "Business & Customers",
    navGroupTrade: "Products & Inventory",
    navGroupGrowthFinance: "Finance & Growth",
    navGroupAiSystem: "AI & Settings",
    navEmployees: "AI Employees",
    navStudio: "AI Studio",
    navGrowthCollect: "Lead Collect",
    navGrowthCampaigns: "Campaigns",
    navSocial: "Social Matrix",
    navDashboard: "Dashboard",
    navTasks: "Tasks",
    navLeads: "Leads",
    navCustomers: "Customers",
    navOpportunities: "Opportunities",
    navFollowups: "Follow-ups",
    navSuppliers: "Suppliers",
    navPurchaseOrders: "Purchase Orders",
    navWms: "WMS Inventory",
    navAnalytics: "Analytics",
    commandPaletteTitle: "Global search & commands",
    commandPalettePlaceholder: "Search customers, orders, quotes or SKUs, or jump to a page…",
    commandPaletteEmpty: "No matches",
    commandPaletteHint: "Ctrl+K to open, Esc to close",
    logout: "Sign out",
    roleLabel: "Role",
    workspaceLabel: "Workspace",
    breadcrumbHome: "Workbench",
    accessDeniedTitle: "Access Denied (ERR_INSUFFICIENT_ROLE)",
    accessDeniedDesc:
      "Your role does not have permission to view this financial or administrative workspace.",
    quietHours: "Quiet Hours",
    localTime: "Local Time",
    strictRag: "Strict RAG",
    globalMode: "Global Guided",
    send: "Send",
    internalNote: "Internal Note",
    liveTranslate: "Live Translate",
    aiDraft: "AI Draft",
    aiPolish: "Polish",
    aiSummarize: "Summarize",
    showTranslation: "Show translation",
    hideTranslation: "Hide translation",
    pendingApprovals: "Pending Approvals",
    aiAssistFailed: "AI assistance is unavailable",
    aiProviderUnavailable:
      "No LLM provider key is configured, so AI generation is unavailable (ERR_AI_PROVIDER_UNAVAILABLE).",
    evidence: "Evidence",
    loadingWorkspace: "Loading workspace session…",
    exportCsv: "Export",
  },
} as const;

export type I18nKey = keyof (typeof DICTIONARY)["en"];

/**
 * Every locale variant of one key, joined — the sidebar quick filter matches both, so
 * typing 「财务」 still finds the item while the UI renders English (and vice-versa).
 */
export function allLocaleLabels(key: I18nKey): string {
  return `${DICTIONARY.zh[key]} ${DICTIONARY.en[key]}`;
}

export interface I18nContextValue {
  locale: Locale;
  setLocale: (loc: Locale) => void;
  toggleLocale: () => void;
  t: (key: I18nKey) => string;
  /**
   * Single-language copy helper: `bi("收款里程碑", "Cash milestones")`.
   * Pages must never render `中文 / English` side by side.
   */
  bi: (zh: string, en: string) => string;
}

const I18nContext = createContext<I18nContextValue>({
  locale: "zh",
  setLocale: () => {},
  toggleLocale: () => {},
  t: (key) => DICTIONARY.zh[key],
  bi: (zh) => zh,
});

export function I18nProvider({
  children,
  defaultLocale = "zh",
}: {
  children: React.ReactNode;
  defaultLocale?: Locale;
}) {
  const [locale, setLocaleState] = useState<Locale>(defaultLocale);

  // Read the persisted preference after mount so SSR markup and the first
  // client render stay identical (no hydration mismatch).
  useEffect(() => {
    try {
      const stored = window.localStorage.getItem(LOCALE_STORAGE_KEY);
      if (stored === "zh" || stored === "en") {
        setLocaleState(stored);
      }
    } catch {
      // Private mode / disabled storage: keep the default locale.
    }
  }, []);

  const setLocale = useCallback((next: Locale) => {
    setLocaleState(next);
    try {
      window.localStorage.setItem(LOCALE_STORAGE_KEY, next);
    } catch {
      // Storage is best-effort; the in-memory locale still applies.
    }
  }, []);

  const toggleLocale = useCallback(() => {
    setLocale(locale === "zh" ? "en" : "zh");
  }, [locale, setLocale]);

  const value = useMemo<I18nContextValue>(
    () => ({
      locale,
      setLocale,
      toggleLocale,
      t: (key: I18nKey) => DICTIONARY[locale][key],
      bi: (zh: string, en: string) => (locale === "zh" ? zh : en),
    }),
    [locale, setLocale, toggleLocale]
  );

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n() {
  return useContext(I18nContext);
}
