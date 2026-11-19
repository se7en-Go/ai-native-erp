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
