import { defineConfig, devices } from "@playwright/test";

// Dual-mode E2E:
// - E2E_MODE=mock (default): the config auto-starts apps/web in dev mode
//   with NEXT_PUBLIC_USE_MOCK_API=true, so the full UI flow runs in real
//   Chromium with zero backend. The BFF mock branch (apps/web/src/app/
//   api/bff/[...path]/route.ts) serves mock auth + mutation responses.
// - E2E_MODE=live: no webServer is started; the suite expects a real
//   apps/web (http://localhost:3000) and backend (http://localhost:8000)
//   seeded with scripts/seed_hybrid_factory.py, per Plan 05 Task 5.8.
export const E2E_MODE = process.env.E2E_MODE ?? "mock";
const isMock = E2E_MODE === "mock";

export default defineConfig({
  testDir: "./specs",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3000",
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },
  ...(isMock
    ? {
        webServer: {
          // webServer runs with cwd = tests/e2e, so ../../ reaches the repo root.
          command:
            "NEXT_PUBLIC_USE_MOCK_API=true pnpm --dir ../../apps/web dev -p 3000",
          url: "http://localhost:3000/login",
          reuseExistingServer: !process.env.CI,
          timeout: 180_000,
        },
      }
    : {}),
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
