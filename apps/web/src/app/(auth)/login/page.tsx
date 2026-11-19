"use client";

import React, { useState } from "react";
import { useRouter } from "next/navigation";
import { bffFetch } from "@/lib/api";
import { fetchOnboardingState } from "@/lib/data-source";
import { useAuthSession } from "@/components/providers";
import { BffApiError, type Principal } from "@/lib/contracts";

export default function LoginPage() {
  const router = useRouter();
  const { refreshSession } = useAuthSession();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [tenantSlug, setTenantSlug] = useState("quanzhou-led-factory");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const res = await bffFetch<{ principal: Principal }>("/v1/auth/login", {
        method: "POST",
        body: JSON.stringify({
          email,
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
        setError("Authentication failed. Please check credentials.");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-100 p-4">
      <div className="w-full max-w-md rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
        <h1 className="text-xl font-bold text-slate-900">Omnichannel AI Workbench</h1>
        <p className="mt-1 text-sm text-slate-500">
          Sign in to your B2B + B2C Hybrid Factory tenant workspace.
        </p>

        {error && (
          <div
            role="alert"
            className="mt-4 rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-800"
          >
            {error}
          </div>
        )}

        <form onSubmit={(e) => void handleSubmit(e)} className="mt-5 space-y-4">
          <div>
            <label htmlFor="tenant-slug" className="block text-xs font-semibold text-slate-700">
              Tenant Workspace Slug
            </label>
            <input
              id="tenant-slug"
              type="text"
              value={tenantSlug}
              onChange={(e) => setTenantSlug(e.target.value)}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
              placeholder="quanzhou-led-factory"
            />
          </div>

          <div>
            <label htmlFor="email" className="block text-xs font-semibold text-slate-700">
              Email
            </label>
            <input
              id="email"
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
              placeholder="owner@quanzhou-led.example.com"
            />
          </div>

          <div>
            <label htmlFor="password" className="block text-xs font-semibold text-slate-700">
              Password
            </label>
            <input
              id="password"
              type="password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
            />
          </div>

          <button
            type="submit"
            disabled={submitting}
            className="w-full rounded-md bg-indigo-600 px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-700 disabled:opacity-50"
          >
            {submitting ? "Signing in..." : "Sign In"}
          </button>
        </form>
      </div>
    </div>
  );
}
