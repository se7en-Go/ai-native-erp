"use client";

import React, { createContext, useCallback, useContext, useState } from "react";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { bffFetch } from "@/lib/api";
import { PrincipalSchema, type Principal } from "@/lib/contracts";
import { I18nProvider } from "@/lib/i18n";

export interface AuthSessionState {
  principal: Principal | null;
  isLoading: boolean;
  logout: () => Promise<void>;
  refreshSession: () => Promise<void>;
}

export const AuthSessionContext = createContext<AuthSessionState>({
  principal: null,
  isLoading: true,
  logout: async () => {},
  refreshSession: async () => {},
});

function AuthSessionBridge({ children }: { children: React.ReactNode }) {
  const { data, isLoading, refetch } = useQuery({
    queryKey: ["auth", "me"],
    queryFn: async () => {
      try {
        const raw = await bffFetch<unknown>("/v1/auth/me");
        const parsed = PrincipalSchema.safeParse(raw);
        return parsed.success ? parsed.data : null;
      } catch {
        return null;
      }
    },
    retry: false,
    staleTime: 60_000,
  });

  const logout = useCallback(async () => {
    await bffFetch("/v1/auth/logout", { method: "POST" });
    await refetch();
    if (typeof window !== "undefined") {
      window.location.href = "/login";
    }
  }, [refetch]);

  const refreshSession = useCallback(async () => {
    await refetch();
  }, [refetch]);

  return (
    <AuthSessionContext.Provider
      value={{
        principal: data ?? null,
        isLoading,
        logout,
        refreshSession,
      }}
    >
      {children}
    </AuthSessionContext.Provider>
  );
}

export function Providers({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 10_000,
            refetchOnWindowFocus: false,
          },
        },
      })
  );

  return (
    <QueryClientProvider client={queryClient}>
      <I18nProvider defaultLocale="zh">
        <AuthSessionBridge>{children}</AuthSessionBridge>
      </I18nProvider>
    </QueryClientProvider>
  );
}

export function useAuthSession() {
  return useContext(AuthSessionContext);
}
