"use client";

import React, { useState } from "react";
import { motion } from "framer-motion";
import { Eye, EyeOff, Loader2, Moon, Sun } from "lucide-react";

export interface SignInDemoAccount {
  email: string;
  badge: string;
  label: string;
  dot: string;
}

export interface SignInStatTile {
  value: string;
  description: string;
}

export interface SignInCollageImage {
  src: string;
  alt: string;
}

export interface AnimatedSignInProps {
  brandAccent: string;
  titlePrefix: string;
  subtitle: string;
  footerNote: string;
  locale: "zh" | "en";
  onToggleLocale: () => void;
  isDark: boolean;
  onToggleTheme: () => void;
  tenantSlug: string;
  onTenantSlugChange: (value: string) => void;
  email: string;
  onEmailChange: (value: string) => void;
  password: string;
  onPasswordChange: (value: string) => void;
  submitting: boolean;
  error: string | null;
  onSubmit: (event: React.FormEvent) => void;
  demoAccounts: SignInDemoAccount[];
  demoPasswordHint: string;
  demoHeading: string;
  onSelectDemoAccount: (email: string) => void;
  labels: {
    tenantSlug: string;
    email: string;
    password: string;
    submit: string;
    submitting: string;
    showPassword: string;
    hidePassword: string;
    toggleTheme: string;
  };
  collageImages: [
    SignInCollageImage,
    SignInCollageImage,
    SignInCollageImage,
    SignInCollageImage,
  ];
  orangeStat: SignInStatTile;
  greenStat: SignInStatTile;
}

/**
 * 21st.dev-inspired Animated Split Sign-In card:
 * - Left (3/5): 2x3 Bento collage (4 real domain photos + 2 bold animated stat tiles)
 * - Right (2/5): Clean sign-in form with password visibility toggle, theme & locale controls
 */
export function AnimatedSignIn({
  brandAccent,
  titlePrefix,
  subtitle,
  footerNote,
  locale,
  onToggleLocale,
  isDark,
  onToggleTheme,
  tenantSlug,
  onTenantSlugChange,
  email,
  onEmailChange,
  password,
  onPasswordChange,
  submitting,
  error,
  onSubmit,
  demoAccounts,
  demoPasswordHint,
  demoHeading,
  onSelectDemoAccount,
  labels,
  collageImages,
  orangeStat,
  greenStat,
}: AnimatedSignInProps) {
  const [showPassword, setShowPassword] = useState(false);

  return (
    <div className="flex min-h-screen w-full items-center justify-center bg-[#edf3fa] p-4 transition-colors duration-300 sm:p-6 dark:bg-gray-950">
      <motion.div
        initial={{ opacity: 0, scale: 0.97 }}
        animate={{ opacity: 1, scale: 1 }}
        transition={{ duration: 0.4, ease: "easeOut" }}
        className="relative w-full max-w-6xl overflow-hidden rounded-2xl border border-gray-200/90 bg-white shadow-xl shadow-slate-900/5 dark:border-gray-800 dark:bg-gray-900 dark:shadow-black/40"
      >
        <div className="flex flex-col md:flex-row">
          {/* Left side — 2x3 Statistics & Export Industry Collage */}
          <div className="hidden w-full bg-gray-100/90 p-5 md:block md:w-[56%] lg:p-6 dark:bg-gray-950/60">
            <div className="grid h-full min-h-[600px] grid-cols-2 grid-rows-3 gap-4">
              {/* Row 1 Col 1: Photo 1 */}
              <div className="overflow-hidden rounded-xl bg-gray-200 dark:bg-gray-800">
                <img
                  src={collageImages[0].src}
                  alt={collageImages[0].alt}
                  className="h-full w-full object-cover transition-transform duration-500 hover:scale-[1.03]"
                />
              </div>

              {/* Row 1 Col 2: Vibrant Orange Stat Tile */}
              <motion.div
                initial={{ opacity: 0, y: 16 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.5, delay: 0.15, ease: "easeOut" }}
                className="flex flex-col items-center justify-center rounded-xl bg-orange-500 p-6 text-center text-white shadow-sm dark:bg-orange-600"
              >
                <div className="text-4xl font-bold tracking-tight tabular-nums lg:text-5xl">
                  {orangeStat.value}
                </div>
                <p className="mt-2.5 max-w-[220px] text-[13px] leading-5 text-white/95">
                  {orangeStat.description}
                </p>
              </motion.div>

              {/* Row 2 Col 1: Photo 2 */}
              <div className="overflow-hidden rounded-xl bg-gray-200 dark:bg-gray-800">
                <img
                  src={collageImages[1].src}
                  alt={collageImages[1].alt}
                  className="h-full w-full object-cover transition-transform duration-500 hover:scale-[1.03]"
                />
              </div>

              {/* Row 2 Col 2: Photo 3 */}
              <div className="overflow-hidden rounded-xl bg-gray-200 dark:bg-gray-800">
                <img
                  src={collageImages[2].src}
                  alt={collageImages[2].alt}
                  className="h-full w-full object-cover transition-transform duration-500 hover:scale-[1.03]"
                />
              </div>

              {/* Row 3 Col 1: Vibrant Green Stat Tile */}
              <motion.div
                initial={{ opacity: 0, y: 16 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.5, delay: 0.3, ease: "easeOut" }}
                className="flex flex-col items-center justify-center rounded-xl bg-emerald-500 p-6 text-center text-white shadow-sm dark:bg-emerald-600"
              >
                <div className="text-4xl font-bold tracking-tight tabular-nums lg:text-5xl">
                  {greenStat.value}
                </div>
                <p className="mt-2.5 max-w-[220px] text-[13px] leading-5 text-white/95">
                  {greenStat.description}
                </p>
              </motion.div>

              {/* Row 3 Col 2: Photo 4 */}
              <div className="overflow-hidden rounded-xl bg-gray-200 dark:bg-gray-800">
                <img
                  src={collageImages[3].src}
                  alt={collageImages[3].alt}
                  className="h-full w-full object-cover transition-transform duration-500 hover:scale-[1.03]"
                />
              </div>
            </div>
          </div>

          {/* Right side — Clean Sign-In Form */}
          <motion.div
            initial={{ opacity: 0, x: 16 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ duration: 0.45, ease: "easeOut" }}
            className="flex w-full flex-col justify-between bg-white p-7 sm:p-10 md:w-[44%] dark:bg-gray-900"
          >
            <div>
              {/* Top Utility Bar: Brand pill + Locale & Theme controls */}
              <div className="mb-7 flex items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <span className="grid h-8 w-8 place-content-center rounded-lg bg-gradient-to-br from-blue-500 to-blue-600 text-[12px] font-bold text-white shadow-xs">
                    QZ
                  </span>
                  <span className="text-[12.5px] font-semibold text-gray-700 dark:text-gray-300">
                    AI-Native ERP
                  </span>
                </div>

                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    data-testid="login-locale-toggle"
                    onClick={onToggleLocale}
                    className="inline-flex h-8 items-center rounded-lg border border-gray-200 bg-gray-50 p-0.5 text-[11.5px] font-semibold dark:border-gray-700 dark:bg-gray-800"
                  >
                    <span
                      className={`rounded-md px-2 py-0.5 transition-colors ${
                        locale === "zh"
                          ? "bg-blue-600 text-white"
                          : "text-gray-500 dark:text-gray-400"
                      }`}
                    >
                      中
                    </span>
                    <span
                      className={`rounded-md px-2 py-0.5 transition-colors ${
                        locale === "en"
                          ? "bg-blue-600 text-white"
                          : "text-gray-500 dark:text-gray-400"
                      }`}
                    >
                      EN
                    </span>
                  </button>

                  <button
                    type="button"
                    data-testid="login-theme-toggle"
                    onClick={onToggleTheme}
                    aria-label={labels.toggleTheme}
                    className="grid h-8 w-8 place-content-center rounded-lg border border-gray-200 bg-gray-50 text-gray-600 transition-colors hover:bg-gray-100 hover:text-gray-900 dark:border-gray-700 dark:bg-gray-800 dark:text-amber-400 dark:hover:bg-gray-700 dark:hover:text-amber-300"
                  >
                    {isDark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
                  </button>
                </div>
              </div>

              {/* Heading */}
              <div className="mb-6">
                <h1 className="text-2xl font-bold tracking-tight text-gray-900 dark:text-white">
                  {titlePrefix}{" "}
                  <span className="text-blue-600 dark:text-blue-400">{brandAccent}</span>
                </h1>
                <p className="mt-1.5 text-[13.5px] leading-6 text-gray-500 dark:text-gray-400">
                  {subtitle}
                </p>
              </div>

              {error && (
                <div
                  role="alert"
                  className="mb-4 rounded-lg border border-rose-200 bg-rose-50 px-3.5 py-2.5 text-[12.5px] font-medium text-rose-800 dark:border-rose-900/60 dark:bg-rose-950/40 dark:text-rose-200"
                >
                  {error}
                </div>
              )}

              {/* Sign-In Form */}
              <form onSubmit={onSubmit} className="space-y-4">
                <div className="space-y-1.5">
                  <label
                    htmlFor="tenant-slug"
                    className="block text-[13px] font-medium text-gray-700 dark:text-gray-300"
                  >
                    {labels.tenantSlug}
                  </label>
                  <input
                    id="tenant-slug"
                    name="tenantSlug"
                    type="text"
                    value={tenantSlug}
                    onChange={(e) => onTenantSlugChange(e.target.value)}
                    placeholder="quanzhou-led-factory"
                    autoComplete="organization"
                    className="block w-full rounded-lg border border-gray-300 bg-white px-3.5 py-2.5 text-[14px] text-gray-900 shadow-2xs transition-colors placeholder:text-gray-400 focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 focus:outline-none dark:border-gray-700 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500 dark:focus:border-blue-400"
                  />
                </div>

                <div className="space-y-1.5">
                  <label
                    htmlFor="email"
                    className="block text-[13px] font-medium text-gray-700 dark:text-gray-300"
                  >
                    {labels.email}
                  </label>
                  <input
                    id="email"
                    name="email"
                    type="email"
                    required
                    value={email}
                    onChange={(e) => onEmailChange(e.target.value)}
                    placeholder="owner@quanzhou-led.example.com"
                    autoComplete="username"
                    className="block w-full rounded-lg border border-gray-300 bg-white px-3.5 py-2.5 text-[14px] text-gray-900 shadow-2xs transition-colors placeholder:text-gray-400 focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 focus:outline-none dark:border-gray-700 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500 dark:focus:border-blue-400"
                  />
                </div>

                <div className="space-y-1.5">
                  <label
                    htmlFor="password"
                    className="block text-[13px] font-medium text-gray-700 dark:text-gray-300"
                  >
                    {labels.password}
                  </label>
                  <div className="relative">
                    <input
                      id="password"
                      name="password"
                      type={showPassword ? "text" : "password"}
                      required
                      value={password}
                      onChange={(e) => onPasswordChange(e.target.value)}
                      placeholder="••••••••••••"
                      autoComplete="current-password"
                      className="block w-full rounded-lg border border-gray-300 bg-white py-2.5 pr-10 pl-3.5 text-[14px] text-gray-900 shadow-2xs transition-colors placeholder:text-gray-400 focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 focus:outline-none dark:border-gray-700 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500 dark:focus:border-blue-400"
                    />
                    <button
                      type="button"
                      data-testid="toggle-password-visibility"
                      onClick={() => setShowPassword((prev) => !prev)}
                      aria-label={showPassword ? labels.hidePassword : labels.showPassword}
                      className="absolute inset-y-0 right-0 flex items-center pr-3 text-gray-400 transition-colors hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
                    >
                      {showPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                    </button>
                  </div>
                </div>

                <button
                  type="submit"
                  disabled={submitting}
                  className="mt-1 flex w-full items-center justify-center gap-2 rounded-lg bg-blue-600 px-4 py-2.5 text-[14px] font-semibold text-white shadow-sm transition-colors hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600 disabled:cursor-not-allowed disabled:opacity-65"
                >
                  {submitting && <Loader2 className="h-4 w-4 animate-spin" />}
                  <span>{submitting ? labels.submitting : labels.submit}</span>
                </button>
              </form>

              {/* Divider + 1-Click Demo Role Autofill */}
              <div className="relative my-5 flex items-center">
                <div className="flex-grow border-t border-gray-200 dark:border-gray-800" />
                <span className="mx-3 flex-shrink text-[11.5px] font-medium text-gray-400 dark:text-gray-500">
                  {demoHeading}
                </span>
                <div className="flex-grow border-t border-gray-200 dark:border-gray-800" />
              </div>

              <div className="space-y-2">
                {demoAccounts.map((account) => {
                  const active = email === account.email;
                  return (
                    <button
                      key={account.email}
                      type="button"
                      onClick={() => onSelectDemoAccount(account.email)}
                      className={`flex w-full items-center justify-between gap-2 rounded-lg border px-3 py-2 text-left transition-all ${
                        active
                          ? "border-blue-500 bg-blue-50/70 dark:border-blue-500 dark:bg-blue-900/30"
                          : "border-gray-200 bg-gray-50/60 hover:border-gray-300 hover:bg-white dark:border-gray-800 dark:bg-gray-800/50 dark:hover:border-gray-700 dark:hover:bg-gray-800"
                      }`}
                    >
                      <span className="flex min-w-0 items-center gap-2.5">
                        <span className={`h-2 w-2 shrink-0 rounded-full ${account.dot}`} />
                        <span className="min-w-0">
                          <span className="block truncate text-[12.5px] font-semibold text-gray-900 dark:text-gray-100">
                            {account.label}
                          </span>
                          <span className="block truncate text-[11px] text-gray-500 dark:text-gray-400">
                            {account.email}
                          </span>
                        </span>
                      </span>
                      <span className="shrink-0 rounded-md border border-gray-200 bg-white px-2 py-0.5 text-[11px] font-medium text-gray-600 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300">
                        {account.badge}
                      </span>
                    </button>
                  );
                })}
                <div className="flex items-center justify-end pt-0.5">
                  <code className="rounded bg-gray-100 px-2 py-0.5 font-mono text-[11px] text-gray-500 dark:bg-gray-800 dark:text-gray-400">
                    {demoPasswordHint}
                  </code>
                </div>
              </div>
            </div>

            <p className="mt-6 text-center text-[11.5px] text-gray-400 dark:text-gray-500">
              {footerNote}
            </p>
          </motion.div>
        </div>
      </motion.div>
    </div>
  );
}
