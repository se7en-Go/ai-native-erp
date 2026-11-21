"use client";

import React, { useState } from "react";
import { useRouter } from "next/navigation";
import { AnimatedSignIn } from "@/components/ui/sign-in";
import { bffFetch } from "@/lib/api";
import { fetchOnboardingState } from "@/lib/data-source";
import { useAuthSession } from "@/components/providers";
import { useI18n } from "@/lib/i18n";
import { useTheme } from "@/lib/theme";
import { BffApiError, type Principal } from "@/lib/contracts";

const DEMO_ACCOUNTS = [
  {
    email: "owner@quanzhou-led.example.com",
    badgeZh: "所有者",
    badgeEn: "Owner",
    zh: "企业所有者 · 全模块权限",
    en: "Owner · full access",
    dot: "bg-blue-500",
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
    dot: "bg-emerald-500",
  },
];

const DEMO_PASSWORD = "FactoryPass2026!";

/**
 * Demo-credential gate (Wave 1 / DS P1-12).
 *
 * The one-click role panel prints a real, hardcoded owner/finance/agent
 * password, so it must never reach a production build unless the operator
 * explicitly opts in. `NODE_ENV` is inlined by Next at build time, so a
 * production build always lands on `false` unless
 * `NEXT_PUBLIC_DEMO_LOGIN=true` (or a deliberate mock build) is set.
 */
const showDemoLogin =
  process.env.NEXT_PUBLIC_DEMO_LOGIN === "true" ||
  process.env.NEXT_PUBLIC_USE_MOCK_API === "true" ||
  process.env.NODE_ENV !== "production";

export default function LoginPage() {
  const router = useRouter();
  const { refreshSession } = useAuthSession();
  const { locale, toggleLocale, bi } = useI18n();
  const { isDark, toggleTheme } = useTheme();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [tenantSlug, setTenantSlug] = useState(showDemoLogin ? "quanzhou-led-factory" : "");
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
    <AnimatedSignIn
      titlePrefix={bi("登录", "Sign in to")}
      brandAccent={bi("工贸全渠道工作台", "Hybrid Trade ERP")}
      subtitle={bi(
        "输入企业工作区标识与账号，进入全渠道询盘、报价、订单与 CVP 指挥舱。",
        "Enter your workspace slug and work account to access inquiries, PI quotes, orders & CVP."
      )}
      footerNote={bi(
        "PG17 FORCE RLS 多租户强隔离 · AES-256-GCM 凭证信封加密 · 全链路审计留痕",
        "PG17 FORCE RLS tenant isolation · AES-256-GCM envelope crypto · full audit trail"
      )}
      locale={locale}
      onToggleLocale={toggleLocale}
      isDark={isDark}
      onToggleTheme={toggleTheme}
      tenantSlug={tenantSlug}
      onTenantSlugChange={setTenantSlug}
      email={email}
      onEmailChange={setEmail}
      password={password}
      onPasswordChange={setPassword}
      submitting={submitting}
      error={error}
      onSubmit={(e) => void handleSubmit(e)}
      demoHeading={showDemoLogin ? bi("演示角色 · 点击一键填充", "Demo roles · click to autofill") : ""}
      demoPasswordHint={showDemoLogin ? DEMO_PASSWORD : ""}
      demoAccounts={
        showDemoLogin
          ? DEMO_ACCOUNTS.map((item) => ({
              email: item.email,
              badge: bi(item.badgeZh, item.badgeEn),
              label: bi(item.zh, item.en),
              dot: item.dot,
            }))
          : []
      }
      onSelectDemoAccount={fillDemoAccount}
      labels={{
        tenantSlug: bi("工作区标识", "Tenant workspace slug"),
        email: bi("企业邮箱", "Work email"),
        password: bi("登录密码", "Password"),
        submit: bi("登录工作台", "Sign in"),
        submitting: bi("正在登录…", "Signing in…"),
        showPassword: bi("显示密码", "Show password"),
        hidePassword: bi("隐藏密码", "Hide password"),
        toggleTheme: bi("切换浅色或深色主题", "Toggle light or dark theme"),
      }}
      collageImages={[
        {
          src: "/images/login/trade-workspace.jpg",
          alt: bi("外贸数字化工作台", "Modern export trade desk"),
        },
        {
          src: "/images/login/smart-factory.jpg",
          alt: bi("智能灯具制造车间", "Smart LED manufacturing workshop"),
        },
        {
          src: "/images/login/global-logistics.jpg",
          alt: bi("国际海运集装箱港口", "Global container port logistics"),
        },
        {
          src: "/images/login/warehouse-fulfillment.jpg",
          alt: bi("外贸智能仓储履约", "Export warehouse fulfillment"),
        },
      ]}
      orangeStat={{
        value: "35.4%",
        description: bi(
          "Decimal 级 CVP 净贡献毛利实时核算，70% 尾款未核销自动阻断出库。",
          "Real-time Decimal CVP margin tracking with automatic 70% balance shipment guard."
        ),
      }}
      greenStat={{
        value: bi("11 渠道", "11 Channels"),
        description: bi(
          "聚合 WhatsApp、Telegram、外贸邮件与独立站，AI 提案人审后精准回写 ERP。",
          "Unify WhatsApp, Telegram, Email & storefronts with human-approved AI ERP grounding."
        ),
      }}
    />
  );
}
