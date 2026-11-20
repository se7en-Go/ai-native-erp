"use client";

import React, { useState } from "react";
import { useRouter } from "next/navigation";
import { Factory, Globe2, Languages, Lock, ShieldCheck, Users } from "lucide-react";
import { bffFetch } from "@/lib/api";
import { fetchOnboardingState } from "@/lib/data-source";
import { useAuthSession } from "@/components/providers";
import { useI18n } from "@/lib/i18n";
import { BffApiError, type Principal } from "@/lib/contracts";

const DEMO_ACCOUNTS = [
  {
    email: "owner@quanzhou-led.example.com",
    zh: "企业所有者 · 全模块",
    en: "Owner · full access",
  },
  {
    email: "finance@quanzhou-led.example.com",
    zh: "财务 · 收款核销与 CVP",
    en: "Finance · settlement & CVP",
  },
  {
    email: "agent@quanzhou-led.example.com",
    zh: "外贸业务员 · 沟通与报价",
    en: "Sales agent · inbox & quotes",
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
        // Mock mode (and any future backend that returns it): the principal
        // carries the stage directly.
        if (res.principal.onboarding_stage !== "LiveProduction") {
          router.push("/onboarding");
        } else {
          router.push(landingPage);
        }
      } else {
        // Live mode: PrincipalResponse has no onboarding_stage, so ask the
        // onboarding state endpoint instead of assuming LiveProduction.
        // A failed lookup must not block a successful login: fall back to
        // the onboarding wizard, which is the safe side.
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
    <div className="grid min-h-screen bg-[#F3F5F9] lg:grid-cols-[1.05fr_0.95fr]">
      {/* 品牌叙事区：深曜石蓝灰，强调外贸工贸一体的业务定位 */}
      <section className="relative flex flex-col justify-between overflow-hidden bg-[#0B1220] p-8 text-slate-300 lg:p-12">
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-0 opacity-[0.18]"
          style={{
            backgroundImage:
              "radial-gradient(circle at 20% 15%, #2563EB 0, transparent 38%), radial-gradient(circle at 78% 72%, #0EA5E9 0, transparent 42%)",
          }}
        />
        <div className="relative">
          <div className="flex items-center gap-3">
            <span className="grid h-10 w-10 place-items-center rounded-xl bg-blue-600 text-sm font-bold text-white">
              QZ
            </span>
            <div>
              <p className="text-sm font-semibold text-white">
                {bi("工贸全渠道外贸工作台", "Hybrid Trade Workbench")}
              </p>
              <p className="text-[11px] text-slate-500">Quanzhou Smart LED Lighting Co., Ltd.</p>
            </div>
          </div>

          <h1 className="mt-12 max-w-lg text-[28px] leading-tight font-semibold text-white">
            {bi(
              "把询盘、报价、订单、收款与 AI 员工，装进同一块外贸工作台。",
              "Inquiries, quotations, orders, settlements and AI coworkers in one trade workbench."
            )}
          </h1>
          <p className="mt-3 max-w-lg text-[13px] leading-7 text-slate-400">
            {bi(
              "面向中国工贸一体企业的 B2B + B2C 全渠道经营系统：聚合沟通、形式发票、70% 尾款风控、17TRACK 物流追踪与 Decimal 级 CVP 毛利核算，全部在同一租户边界内闭环。",
              "A B2B + B2C operating system for Chinese manufacturers: unified inbox, proforma invoices, balance controls, 17TRACK logistics and Decimal-precise CVP margins inside one tenant boundary."
            )}
          </p>

          <ul className="mt-8 grid max-w-lg gap-3 text-[12px] sm:grid-cols-2">
            {[
              {
                icon: Globe2,
                zh: "中英双语界面，业务员零门槛上手",
                en: "Bilingual UI, zero onboarding friction",
              },
              {
                icon: ShieldCheck,
                zh: "L0 财务复核，资金动作人工把关",
                en: "L0 finance review, human-in-the-loop",
              },
              {
                icon: Factory,
                zh: "商品主档与多平台 SKU 一一映射",
                en: "Master catalog mapped to every channel",
              },
              {
                icon: Users,
                zh: "角色权限分级，成本毛利自动脱敏",
                en: "Role-based access with automatic COGS masking",
              },
            ].map((item) => (
              <li
                key={item.zh}
                className="flex items-start gap-2 rounded-xl border border-slate-800/80 bg-slate-900/40 p-3"
              >
                <item.icon className="mt-0.5 h-4 w-4 shrink-0 text-blue-400" />
                <span className="leading-5 text-slate-300">{bi(item.zh, item.en)}</span>
              </li>
            ))}
          </ul>
        </div>

        <p className="relative mt-10 text-[11px] text-slate-500">
          {bi(
            "租户隔离 · 行级安全策略（RLS）强制执行 · 令牌信封加密",
            "Tenant isolation · enforced row-level security · envelope-encrypted tokens"
          )}
        </p>
      </section>

      {/* 登录区：纯白精致卡片 */}
      <section className="flex items-center justify-center p-6 lg:p-12">
        <div className="w-full max-w-[440px]">
          <div className="mb-4 flex items-center justify-between">
            <div>
              <h2 className="text-lg font-semibold text-slate-900">
                {bi("登录企业工作区", "Sign in to your workspace")}
              </h2>
              <p className="mt-0.5 text-[12px] text-slate-500">
                {bi("使用企业邮箱与工作区标识登录", "Use your corporate email and workspace slug")}
              </p>
            </div>
            <button
              type="button"
              onClick={toggleLocale}
              className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-[12px] font-medium text-slate-600 hover:border-slate-300"
            >
              <Languages className="h-3.5 w-3.5" />
              {locale === "zh" ? "EN" : "中文"}
            </button>
          </div>

          <div className="erp-card p-6">
            {error && (
              <div
                role="alert"
                className="mb-4 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-[12px] text-rose-800"
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
                  <Factory className="pointer-events-none absolute top-2.5 left-2.5 h-4 w-4 text-slate-400" />
                  <input
                    id="tenant-slug"
                    type="text"
                    value={tenantSlug}
                    onChange={(e) => setTenantSlug(e.target.value)}
                    className="erp-input pl-8"
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
                  <Users className="pointer-events-none absolute top-2.5 left-2.5 h-4 w-4 text-slate-400" />
                  <input
                    id="email"
                    type="email"
                    required
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    className="erp-input pl-8"
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
                  <Lock className="pointer-events-none absolute top-2.5 left-2.5 h-4 w-4 text-slate-400" />
                  <input
                    id="password"
                    type="password"
                    required
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    className="erp-input pl-8"
                    placeholder="••••••••••••"
                    autoComplete="current-password"
                  />
                </div>
              </div>

              <button
                type="submit"
                disabled={submitting}
                className="erp-btn erp-btn-primary w-full py-2"
              >
                {submitting
                  ? bi("正在登录…", "Signing in…")
                  : bi("登录工作台", "Sign in")}
              </button>
            </form>

            <div className="mt-5 border-t border-slate-100 pt-4">
              <p className="text-[11px] font-medium text-slate-500">
                {bi("演示账号 · 点击一键填充", "Demo accounts · click to fill")}
              </p>
              <ul className="mt-2 space-y-1.5">
                {DEMO_ACCOUNTS.map((account) => (
                  <li key={account.email}>
                    <button
                      type="button"
                      onClick={() => fillDemoAccount(account.email)}
                      className="flex w-full items-center justify-between gap-2 rounded-lg border border-slate-200 px-2.5 py-2 text-left transition-colors hover:border-blue-200 hover:bg-blue-50/40"
                    >
                      <span className="min-w-0">
                        <span className="block truncate text-[12px] font-medium text-slate-800">
                          {bi(account.zh, account.en)}
                        </span>
                        <span className="block truncate text-[11px] text-slate-500">
                          {account.email}
                        </span>
                      </span>
                      <span className="shrink-0 rounded border border-slate-200 bg-white px-1.5 py-0.5 text-[10px] text-slate-500">
                        {bi("填充", "Fill")}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
              <p className="mt-2 text-[11px] text-slate-400">
                {bi("统一演示密码", "Shared demo password")}：
                <code className="ml-1 rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-slate-600">
                  {DEMO_PASSWORD}
                </code>
              </p>
            </div>
          </div>

          <p className="mt-4 text-center text-[11px] text-slate-400">
            {bi(
              "登录即表示同意企业数据使用规范；所有操作留痕并纳入审计。",
              "By signing in you accept the tenant data policy; every action is audited."
            )}
          </p>
        </div>
      </section>
    </div>
  );
}
