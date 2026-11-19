"use client";

import React, { createContext, useContext, useMemo, useState } from "react";

export type Locale = "zh" | "en";

const DICTIONARY = {
  zh: {
    brandTitle: "工贸全渠道工作台",
    navInbox: "聚合沟通 Inbox",
    navProposals: "AI 提案审核",
    navQuotes: "B2B CRM 与报价",
    navCatalog: "商品与 SKU 映射",
    navOrders: "订单与 17TRACK",
    navFinance: "财务与 CVP 毛利",
    navSettings: "渠道与合规设置",
    navCapabilities: "AI 治理与知识库",
    navOnboarding: "工厂入驻向导",
    navGroupCore: "核心工作台",
    navGroupBusiness: "业务核心",
    navGroupTrade: "交易与供应链",
    navGroupFinance: "财务中心",
    navGroupAi: "AI 赋能",
    navEmployees: "AI 数字员工",
    navStudio: "AI 素材工作台",
    navGroupGrowth: "获客与社媒",
    navGrowthCollect: "海外地图获客",
    navGrowthCampaigns: "合规营销战役",
    navSocial: "社媒营销中心",
    navGroupSystem: "系统工具",
    navDashboard: "经营总览",
    navTasks: "任务提醒",
    navLeads: "CRM 线索池",
    navCustomers: "客户档案",
    navOpportunities: "商机看板",
    navFollowups: "跟进记录",
    navSuppliers: "供应商名录",
    navPurchaseOrders: "采购订单",
    navWms: "WMS 出入库",
    navAnalytics: "统计分析",
    commandPaletteTitle: "全局搜索与命令",
    commandPalettePlaceholder: "搜索客户 / 订单 / 报价 / SKU，或跳转页面…",
    commandPaletteEmpty: "没有匹配结果",
    commandPaletteHint: "Ctrl+K 打开，Esc 关闭",
    logout: "退出登录",
    roleLabel: "当前角色",
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
    aiProviderUnavailable: "未配置大模型供应商密钥，AI 生成暂不可用（ERR_AI_PROVIDER_UNAVAILABLE）。",
    evidence: "证据",
  },
  en: {
    brandTitle: "Hybrid Trade Workbench",
    navInbox: "Inbox",
    navProposals: "AI Proposals",
    navQuotes: "B2B CRM & Quotes",
    navCatalog: "Catalog",
    navOrders: "Orders & Tracking",
    navFinance: "Finance & CVP",
    navSettings: "Channels & Settings",
    navCapabilities: "AI Governance & KB",
    navOnboarding: "Onboarding Wizard",
    navGroupCore: "Core Workspace",
    navGroupBusiness: "Business Core",
    navGroupTrade: "Trade & Supply",
    navGroupFinance: "Finance",
    navGroupAi: "AI Enablement",
    navEmployees: "AI Employees",
    navStudio: "AI Studio",
    navGroupGrowth: "Growth & Social",
    navGrowthCollect: "Global Lead Map",
    navGrowthCampaigns: "Compliant Campaigns",
    navSocial: "Social Marketing Center",
    navGroupSystem: "System",
    navDashboard: "Dashboard",
    navTasks: "Tasks",
    navLeads: "CRM Leads",
    navCustomers: "Customers",
    navOpportunities: "Opportunities",
    navFollowups: "Follow-ups",
    navSuppliers: "Suppliers",
    navPurchaseOrders: "Purchase Orders",
    navWms: "WMS Movements",
    navAnalytics: "Analytics",
    commandPaletteTitle: "Global search & commands",
    commandPalettePlaceholder: "Search customers / orders / quotes / SKUs, or jump to a page…",
    commandPaletteEmpty: "No matches",
    commandPaletteHint: "Ctrl+K to open, Esc to close",
    logout: "Sign out",
    roleLabel: "Role",
    accessDeniedTitle: "Access Denied (ERR_INSUFFICIENT_ROLE)",
    accessDeniedDesc: "Your role does not have permission to view this financial or administrative workspace.",
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
  },
} as const;

export type I18nKey = keyof (typeof DICTIONARY)["en"];

export interface I18nContextValue {
  locale: Locale;
  setLocale: (loc: Locale) => void;
  toggleLocale: () => void;
  t: (key: I18nKey) => string;
}

const I18nContext = createContext<I18nContextValue>({
  locale: "en",
  setLocale: () => {},
  toggleLocale: () => {},
  t: (key) => DICTIONARY.en[key],
});

export function I18nProvider({
  children,
  defaultLocale = "zh",
}: {
  children: React.ReactNode;
  defaultLocale?: Locale;
}) {
  const [locale, setLocale] = useState<Locale>(defaultLocale);

  const value = useMemo<I18nContextValue>(
    () => ({
      locale,
      setLocale,
      toggleLocale: () => setLocale((prev) => (prev === "zh" ? "en" : "zh")),
      t: (key: I18nKey) => DICTIONARY[locale][key],
    }),
    [locale]
  );

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n() {
  return useContext(I18nContext);
}
