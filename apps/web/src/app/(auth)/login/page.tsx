"use client";

import React, { useState } from "react";
import { useRouter } from "next/navigation";
import { motion } from "framer-motion";
import {
  ArrowRight,
  Bot,
  Factory,
  Globe2,
  Lock,
  ShieldCheck,
  Sparkles,
  TrendingUp,
  Users,
} from "lucide-react";
import { bffFetch } from "@/lib/api";
import { fetchOnboardingState } from "@/lib/data-source";
import { useAuthSession } from "@/components/providers";
import { useI18n } from "@/lib/i18n";
import { BffApiError, type Principal } from "@/lib/contracts";

const DEMO_ACCOUNTS = [
  {
    email: "owner@quanzhou-led.example.com",
    badgeZh: "所有者",
    badgeEn: "Owner",
    zh: "企业所有者 · 全模块权限",
    en: "Owner · full access",
    dot: "bg-emerald-500",
  },
  {
    email: "finance@quanzhou-led.example.com",
    badgeZh: "财务",
    badgeEn: "Finance",
    zh: "财务复核 · 水单核销与 CVP",
    en: "Finance · settlement & CVP",
    dot: "bg-amber-500",
  },
  {
    email: "agent@quanzhou-led.example.com",
    badgeZh: "业务员",
    badgeEn: "Agent",
    zh: "外贸业务员 · 沟通与阶梯报价",
    en: "Sales agent · inbox & quotes",
    dot: "bg-zinc-700",
  },
];

const DEMO_PASSWORD = "FactoryPass2026!";

export default function LoginPage() {
  const router = useRouter();
  const { refreshSession } = useAuthSession();
  const { locale, toggleLocale, bi } = useI18n();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [tenantSlug, setTenantSlug] = useState("quanzhou-led-factory");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    if (!email.trim() || !password) {
      setError(bi("请填写邮箱与密码。", "Enter both email and password."));
      return;
    }
    setSubmitting(true);
    try {
      const res = await bffFetch<{ principal: Principal }>("/v1/auth/login", {
        method: "POST",
        body: JSON.stringify({
          email: email.trim(),
          password,
          tenant_slug: tenantSlug.trim() || undefined,
        }),
      });
      await refreshSession();
      const landingPage = res.principal.role === "finance" ? "/finance" : "/inbox";
      if (res.principal.onboarding_stage) {
        if (res.principal.onboarding_stage !== "LiveProduction") {
          router.push("/onboarding");
        } else {
          router.push(landingPage);
        }
      } else {
        let stage: string | undefined;
        try {
          stage = (await fetchOnboardingState()).current_stage;
        } catch {
          stage = undefined;
        }
        router.push(stage === "LiveProduction" ? landingPage : "/onboarding");
      }
    } catch (err) {
      if (err instanceof BffApiError) {
        setError(`${err.code}: ${err.detail}`);
      } else {
        setError(
          bi(
            "登录失败，请检查工作区、邮箱与密码。",
            "Authentication failed. Please check workspace, email and password."
          )
        );
      }
    } finally {
      setSubmitting(false);
    }
  }

  function fillDemoAccount(demoEmail: string) {
    setEmail(demoEmail);
    setPassword(DEMO_PASSWORD);
    setError(null);
  }

  return (
    <div className="grid min-h-screen bg-[#F4F2EE] lg:grid-cols-[1.15fr_0.85fr]">
      {/* 左侧：21st.dev 深森曜石指挥舱视觉展板 */}
      <section className="relative flex flex-col justify-between overflow-hidden bg-[#081311] p-8 text-stone-200 lg:p-14">
        {/* 层次氛围光晕 + 微点阵纹理 */}
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-0"
          style={{
            backgroundImage:
              "radial-gradient(circle at 18% 18%, rgba(16,185,129,0.24) 0, transparent 42%), radial-gradient(circle at 82% 76%, rgba(13,148,136,0.22) 0, transparent 45%), radial-gradient(circle at 55% 40%, rgba(217,119,6,0.10) 0, transparent 40%)",
          }}
        />
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-0 opacity-[0.14]"
          style={{
            backgroundImage: "radial-gradient(rgba(255,255,255,0.45) 1px, transparent 1px)",
            backgroundSize: "24px 24px",
          }}
        />

        {/* 顶部品牌标识 */}
        <div className="relative flex items-center justify-between">
          <div className="flex items-center gap-3">
            <span className="relative grid h-11 w-11 place-items-center rounded-2xl bg-gradient-to-br from-emerald-500 to-teal-700 text-[15px] font-bold text-white shadow-[0_8px_24px_rgba(16,185,129,0.35)]">
              QZ
              <span className="absolute -top-0.5 -right-0.5 h-3 w-3 rounded-full bg-emerald-300 ring-2 ring-[#081311]" />
            </span>
            <div>
              <div className="text-[15px] font-bold tracking-tight text-white">
                {bi("工贸全渠道外贸工作台", "Hybrid Foreign Trade Command Center")}
              </div>
              <div className="text-[11.5px] font-medium text-emerald-200/70">
                Quanzhou Smart LED Lighting · AI-Native ERP
              </div>
            </div>
          </div>

          <span className="hidden items-center gap-1.5 rounded-full border border-emerald-400/25 bg-emerald-500/10 px-3 py-1 text-[11.5px] font-semibold text-emerald-200 backdrop-blur-md sm:inline-flex">
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
            {bi("PG17 RLS 租户硬隔离", "PG17 FORCE RLS")}
          </span>
        </div>

        {/* 中部核心叙事 + 21st.dev 风格实时玻璃预览卡片 */}
        <motion.div
          initial={{ opacity: 0, y: 14 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.45, ease: "easeOut" }}
          className="relative my-10 max-w-2xl space-y-8"
        >
          <div className="space-y-4">
            <span className="inline-flex items-center gap-1.5 rounded-full border border-white/15 bg-white/5 px-3 py-1 text-[12px] font-medium text-emerald-200">
              <Sparkles className="h-3.5 w-3.5 text-emerald-400" />
              {bi("专为中国工贸一体出海团队打造", "Built for Chinese export manufacturers")}
            </span>
            <h1 className="text-[32px] leading-[1.22] font-bold tracking-tight text-white lg:text-[40px]">
              {bi(
                "把询盘、报价、订单、收款与 AI 员工，装进同一座外贸指挥舱。",
                "Inquiries, tiered PI quotes, 17TRACK logistics, CVP margins & AI employees in one command center."
              )}
            </h1>
            <p className="max-w-xl text-[14px] leading-7 text-stone-300/90">
              {bi(
                "打通 WhatsApp、外贸邮件、阿里/MIC 与海外社媒，70% 尾款未核销强制阻断出库，每一笔毛利精确到 Decimal，AI 提案人工确认再落单。",
                "Unify WhatsApp, Email, Meta & Telegram with deterministic SQL grounding, Decimal CVP margin tracking, and human-in-the-loop L0/L1 approvals."
              )}
            </p>
          </div>

          {/* 玻璃拟态实时指标预览（21st.dev Command Center 招牌质感） */}
          <div className="rounded-3xl border border-white/12 bg-white/[0.06] p-5 shadow-[0_24px_60px_-20px_rgba(0,0,0,0.6),inset_0_1px_0_rgba(255,255,255,0.14)] backdrop-blur-xl">
            <div className="flex items-center justify-between border-b border-white/10 pb-3.5">
              <div className="flex items-center gap-2 text-[12px] font-semibold text-emerald-200">
                <span className="h-2 w-2 rounded-full bg-emerald-400" />
                {bi("实时经营快照 · 泉州 LED 演示工贸租户", "Live Workspace Preview · Quanzhou LED")}
              </div>
              <span className="rounded-full border border-white/15 bg-white/10 px-2.5 py-0.5 text-[11px] font-semibold tabular-nums text-white">
                USD/CNY 7.1200
              </span>
            </div>

            <div className="mt-4 grid gap-3 sm:grid-cols-3">
              <div className="rounded-2xl border border-emerald-400/25 bg-gradient-to-br from-emerald-600/35 to-teal-700/25 p-3.5">
                <div className="flex items-center justify-between text-[11px] font-medium text-emerald-100/90">
                  <span>{bi("CVP 平均毛利率", "Avg CVP Margin")}</span>
                  <TrendingUp className="h-3.5 w-3.5 text-emerald-300" />
                </div>
                <div className="mt-2 text-[22px] font-bold tabular-nums text-white">35.39%</div>
                <div className="mt-1 text-[11px] text-emerald-200/80">
                  {bi("净毛利 $11,346.62", "Net $11,346.62")}
                </div>
              </div>

              <div className="rounded-2xl border border-white/10 bg-white/[0.05] p-3.5">
                <div className="flex items-center justify-between text-[11px] font-medium text-stone-300">
                  <span>{bi("AI 数字员工", "AI Workforce")}</span>
                  <Bot className="h-3.5 w-3.5 text-emerald-400" />
                </div>
                <div className="mt-2 text-[22px] font-bold tabular-nums text-white">9 / 9</div>
                <div className="mt-1 text-[11px] text-stone-400">
                  {bi("L2/L1/L0 自治矩阵", "L2/L1/L0 governed")}
                </div>
              </div>

              <div className="rounded-2xl border border-white/10 bg-white/[0.05] p-3.5">
                <div className="flex items-center justify-between text-[11px] font-medium text-stone-300">
                  <span>{bi("风控合规引擎", "Compliance Guard")}</span>
                  <ShieldCheck className="h-3.5 w-3.5 text-amber-300" />
                </div>
                <div className="mt-2 text-[22px] font-bold tabular-nums text-white">L0</div>
                <div className="mt-1 text-[11px] text-stone-400">
                  {bi("水单复核 · 零幻觉", "Manual cash verify")}
                </div>
              </div>
            </div>

            <div className="mt-4 flex flex-wrap items-center gap-2 text-[11.5px] text-stone-300">
              <span className="inline-flex items-center gap-1.5 rounded-full border border-white/10 bg-white/[0.04] px-3 py-1">
                <Globe2 className="h-3.5 w-3.5 text-emerald-400" />
                {bi("中英双语实时翻译", "Bilingual live translation")}
              </span>
              <span className="inline-flex items-center gap-1.5 rounded-full border border-white/10 bg-white/[0.04] px-3 py-1">
                <ShieldCheck className="h-3.5 w-3.5 text-emerald-400" />
                {bi("角色分级 · 成本自动脱敏", "RBAC cost masking")}
              </span>
              <span className="inline-flex items-center gap-1.5 rounded-full border border-white/10 bg-white/[0.04] px-3 py-1">
                <Lock className="h-3.5 w-3.5 text-emerald-400" />
                {bi("AES-256-GCM 凭证信封加密", "Envelope crypto")}
              </span>
            </div>
          </div>
        </motion.div>

        {/* 底部安全声明 */}
        <p className="relative text-[11.5px] text-stone-400">
          {bi(
            "多租户行级安全策略（FORCE RLS）强制执行 · 财务与报价全链路留痕审计",
            "PostgreSQL 17 FORCE RLS · Envelope Encryption · Full Audit Trail"
          )}
        </p>
      </section>

      {/* 右侧：暖陶灰画布上的精致悬浮登录卡 */}
      <section className="relative flex items-center justify-center p-6 lg:p-14">
        <motion.div
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.4, delay: 0.08, ease: "easeOut" }}
          className="w-full max-w-[448px]"
        >
          <div className="mb-5 flex items-center justify-between">
            <div>
              <h2 className="text-[22px] font-bold tracking-tight text-zinc-950">
                {bi("登录企业工作区", "Sign in to workspace")}
              </h2>
              <p className="mt-1 text-[13px] text-stone-600">
                {bi("输入企业工作区标识与账号进入指挥舱", "Enter your workspace slug and credentials")}
              </p>
            </div>
            <button
              type="button"
              onClick={toggleLocale}
              className="inline-flex items-center rounded-full border border-[#E5E2DC] bg-white p-0.5 text-[12px] font-semibold shadow-xs"
            >
              <span
                className={`rounded-full px-2.5 py-1 transition-all ${
                  locale === "zh" ? "bg-[#18181B] text-white" : "text-stone-500"
                }`}
              >
                中
              </span>
              <span
                className={`rounded-full px-2.5 py-1 transition-all ${
                  locale === "en" ? "bg-[#18181B] text-white" : "text-stone-500"
                }`}
              >
                EN
              </span>
            </button>
          </div>

          <div className="rounded-3xl border border-[#E5E2DC] bg-white p-7 shadow-[inset_0_1px_0_rgba(255,255,255,0.95),0_20px_50px_-24px_rgba(24,24,27,0.18)]">
            {error && (
              <div
                role="alert"
                className="mb-4 rounded-xl border border-rose-200 bg-rose-50 px-3.5 py-2.5 text-[12.5px] font-medium text-rose-800"
              >
                {error}
              </div>
            )}

            <form onSubmit={(e) => void handleSubmit(e)} className="space-y-4">
              <div>
                <label htmlFor="tenant-slug" className="erp-label">
                  {bi("工作区标识", "Tenant workspace slug")}
                </label>
                <div className="relative mt-1.5">
                  <Factory className="pointer-events-none absolute top-3 left-3 h-4 w-4 text-stone-400" />
                  <input
                    id="tenant-slug"
                    type="text"
                    value={tenantSlug}
                    onChange={(e) => setTenantSlug(e.target.value)}
                    className="erp-input py-2.5 pl-9"
                    placeholder="quanzhou-led-factory"
                    autoComplete="organization"
                  />
                </div>
              </div>

              <div>
                <label htmlFor="email" className="erp-label">
                  {bi("企业邮箱", "Work email")}
                </label>
                <div className="relative mt-1.5">
                  <Users className="pointer-events-none absolute top-3 left-3 h-4 w-4 text-stone-400" />
                  <input
                    id="email"
                    type="email"
                    required
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    className="erp-input py-2.5 pl-9"
                    placeholder="owner@quanzhou-led.example.com"
                    autoComplete="username"
                  />
                </div>
              </div>

              <div>
                <label htmlFor="password" className="erp-label">
                  {bi("登录密码", "Password")}
                </label>
                <div className="relative mt-1.5">
                  <Lock className="pointer-events-none absolute top-3 left-3 h-4 w-4 text-stone-400" />
                  <input
                    id="password"
                    type="password"
                    required
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    className="erp-input py-2.5 pl-9"
                    placeholder="••••••••••••"
                    autoComplete="current-password"
                  />
                </div>
              </div>

              <button
                type="submit"
                disabled={submitting}
                className="group flex w-full items-center justify-center gap-2 rounded-xl bg-[#18181B] px-4 py-3 text-[13.5px] font-semibold text-[#FAFAF9] shadow-[0_8px_20px_-8px_rgba(24,24,27,0.5)] transition-all hover:bg-[#065F46] disabled:opacity-60"
              >
                <span>
                  {submitting
                    ? bi("正在进入工作台…", "Signing in…")
                    : bi("登录工作台", "Sign in to workspace")}
                </span>
                <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-0.5" />
              </button>
            </form>

            <div className="mt-6 border-t border-[#EFECE6] pt-5">
              <div className="flex items-center justify-between">
                <p className="text-[11.5px] font-semibold text-stone-600">
                  {bi("演示角色 · 点击一键填充", "Demo roles · click to autofill")}
                </p>
                <code className="rounded-md bg-[#F4F2EE] px-2 py-0.5 font-mono text-[11px] font-medium text-stone-700">
                  {DEMO_PASSWORD}
                </code>
              </div>

              <ul className="mt-2.5 space-y-2">
                {DEMO_ACCOUNTS.map((account) => {
                  const selected = email === account.email;
                  return (
                    <li key={account.email}>
                      <button
                        type="button"
                        onClick={() => fillDemoAccount(account.email)}
                        className={`flex w-full items-center justify-between gap-2 rounded-xl border px-3 py-2.5 text-left transition-all ${
                          selected
                            ? "border-emerald-400 bg-emerald-50/70 shadow-xs"
                            : "border-[#E5E2DC] bg-[#FAF8F5]/70 hover:border-stone-300 hover:bg-white"
                        }`}
                      >
                        <span className="flex min-w-0 items-center gap-2.5">
                          <span className={`h-2 w-2 shrink-0 rounded-full ${account.dot}`} />
                          <span className="min-w-0">
                            <span className="block truncate text-[12.5px] font-semibold text-zinc-900">
                              {bi(account.zh, account.en)}
                            </span>
                            <span className="block truncate text-[11px] text-stone-500">
                              {account.email}
                            </span>
                          </span>
                        </span>
                        <span className="shrink-0 rounded-full border border-[#E5E2DC] bg-white px-2.5 py-0.5 text-[11px] font-semibold text-stone-700">
                          {bi(account.badgeZh, account.badgeEn)}
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            </div>
          </div>

          <p className="mt-5 text-center text-[11.5px] text-stone-500">
            {bi(
              "登录即表示同意企业数据安全与合规审计规范。",
              "By signing in you accept the enterprise data security & audit policy."
            )}
          </p>
        </motion.div>
      </section>
    </div>
  );
}
