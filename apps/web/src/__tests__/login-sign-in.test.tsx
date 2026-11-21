import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import LoginPage from "@/app/(auth)/login/page";
import { AuthSessionContext, type AuthSessionState } from "@/components/providers";
import { I18nProvider } from "@/lib/i18n";
import { ThemeProvider } from "@/lib/theme";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

function renderLogin() {
  const authValue: AuthSessionState = {
    principal: null,
    isLoading: false,
    logout: vi.fn(async () => {}),
    refreshSession: vi.fn(async () => {}),
  };
  return render(
    <ThemeProvider defaultTheme="light">
      <AuthSessionContext.Provider value={authValue}>
        <I18nProvider defaultLocale="zh">
          <LoginPage />
        </I18nProvider>
      </AuthSessionContext.Provider>
    </ThemeProvider>
  );
}

describe("21st.dev AnimatedSignIn Login Page", () => {
  beforeEach(() => {
    document.documentElement.classList.remove("dark");
  });

  it("renders tenant/email/password inputs, toggles password visibility, autofills demo role, and toggles dark theme", () => {
    renderLogin();

    const tenantInput = document.querySelector("#tenant-slug") as HTMLInputElement;
    const emailInput = document.querySelector("#email") as HTMLInputElement;
    const passwordInput = document.querySelector("#password") as HTMLInputElement;
    expect(tenantInput).not.toBeNull();
    expect(emailInput).not.toBeNull();
    expect(passwordInput).not.toBeNull();
    expect(passwordInput.type).toBe("password");

    fireEvent.click(screen.getByTestId("toggle-password-visibility"));
    expect(passwordInput.type).toBe("text");

    fireEvent.click(screen.getByText("owner@quanzhou-led.example.com"));
    expect(emailInput.value).toBe("owner@quanzhou-led.example.com");
    expect(passwordInput.value).toBe("FactoryPass2026!");

    fireEvent.click(screen.getByTestId("login-theme-toggle"));
    expect(document.documentElement.classList.contains("dark")).toBe(true);
  });
});
