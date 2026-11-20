import React from "react";
import { describe, it, expect, beforeEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";

import { I18nProvider, LOCALE_STORAGE_KEY, useI18n } from "@/lib/i18n";

function Probe() {
  const { locale, bi, t, toggleLocale } = useI18n();
  return (
    <div>
      <span data-testid="locale">{locale}</span>
      <span data-testid="bi">{bi("收款里程碑", "Cash milestones")}</span>
      <span data-testid="nav">{t("navFinance")}</span>
      <button type="button" onClick={toggleLocale}>
        toggle
      </button>
    </div>
  );
}

/**
 * jsdom in this workspace does not expose window.localStorage, so install a
 * spec-compliant in-memory Storage before the provider mounts.
 */
function installStorage(): Storage {
  const store = new Map<string, string>();
  const storage: Storage = {
    get length() {
      return store.size;
    },
    clear: () => store.clear(),
    getItem: (key) => store.get(key) ?? null,
    key: (index) => [...store.keys()][index] ?? null,
    removeItem: (key) => {
      store.delete(key);
    },
    setItem: (key, value) => {
      store.set(key, String(value));
    },
  };
  Object.defineProperty(window, "localStorage", { configurable: true, value: storage });
  return storage;
}

describe("Stage 1 i18n single-language contract", () => {
  beforeEach(() => {
    installStorage().clear();
  });

  it("defaults to Chinese and renders bi() as pure Chinese", () => {
    render(
      <I18nProvider>
        <Probe />
      </I18nProvider>
    );
    expect(screen.getByTestId("locale").textContent).toBe("zh");
    expect(screen.getByTestId("bi").textContent).toBe("收款里程碑");
    expect(screen.getByTestId("nav").textContent).toBe("财务与 CVP 毛利");
  });

  it("persists the preference to localStorage#erp_locale and switches to pure English", () => {
    render(
      <I18nProvider>
        <Probe />
      </I18nProvider>
    );
    fireEvent.click(screen.getByRole("button", { name: "toggle" }));

    expect(screen.getByTestId("locale").textContent).toBe("en");
    expect(screen.getByTestId("bi").textContent).toBe("Cash milestones");
    expect(screen.getByTestId("nav").textContent).toBe("Finance & CVP");
    expect(window.localStorage.getItem(LOCALE_STORAGE_KEY)).toBe("en");
  });

  it("hydrates the persisted locale on mount", async () => {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, "en");
    render(
      <I18nProvider>
        <Probe />
      </I18nProvider>
    );
    await act(async () => {});
    expect(screen.getByTestId("locale").textContent).toBe("en");
    expect(screen.getByTestId("bi").textContent).toBe("Cash milestones");
  });
});
