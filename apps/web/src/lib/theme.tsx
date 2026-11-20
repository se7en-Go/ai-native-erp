"use client";

import React, { createContext, useCallback, useContext, useEffect, useState } from "react";

export type UiTheme = "light" | "dark";

const STORAGE_KEY = "erp_ui_theme";

export interface ThemeContextValue {
  theme: UiTheme;
  isDark: boolean;
  setTheme: (next: UiTheme) => void;
  toggleTheme: () => void;
}

const ThemeContext = createContext<ThemeContextValue>({
  theme: "light",
  isDark: false,
  setTheme: () => {},
  toggleTheme: () => {},
});

function readStoredTheme(): UiTheme {
  if (typeof window === "undefined") return "light";
  try {
    const raw =
      typeof window.localStorage?.getItem === "function"
        ? window.localStorage.getItem(STORAGE_KEY)
        : null;
    if (raw === "dark" || raw === "light") return raw;
  } catch {
    // Ignore storage access errors in restricted browsing contexts.
  }
  return "light";
}

function persistTheme(theme: UiTheme) {
  if (typeof window === "undefined") return;
  try {
    if (typeof window.localStorage?.setItem === "function") {
      window.localStorage.setItem(STORAGE_KEY, theme);
    }
  } catch {
    // Ignore storage write errors.
  }
}

function applyThemeClass(theme: UiTheme) {
  if (typeof document === "undefined") return;
  document.documentElement.classList.toggle("dark", theme === "dark");
}

export function ThemeProvider({
  children,
  defaultTheme = "light",
}: {
  children: React.ReactNode;
  defaultTheme?: UiTheme;
}) {
  const [theme, setThemeState] = useState<UiTheme>(defaultTheme);

  useEffect(() => {
    const stored = readStoredTheme();
    setThemeState(stored);
    applyThemeClass(stored);
  }, []);

  const setTheme = useCallback((next: UiTheme) => {
    setThemeState(next);
    applyThemeClass(next);
    persistTheme(next);
  }, []);

  const toggleTheme = useCallback(() => {
    setThemeState((prev) => {
      const next: UiTheme = prev === "dark" ? "light" : "dark";
      applyThemeClass(next);
      persistTheme(next);
      return next;
    });
  }, []);

  return (
    <ThemeContext.Provider
      value={{
        theme,
        isDark: theme === "dark",
        setTheme,
        toggleTheme,
      }}
    >
      {children}
    </ThemeContext.Provider>
  );
}

export function useTheme(): ThemeContextValue {
  return useContext(ThemeContext);
}
