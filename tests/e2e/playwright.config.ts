import { defineConfig, devices } from "@playwright/test";

import { isMockMode } from "./helpers/environment";

// Mock owns localhost:3000 and serves fixture data and mock BFF mutations.
// Live starts nothing. Run Web with NEXT_PUBLIC_USE_MOCK_API=false and point
// its BFF at the seeded backend. E2E_BASE_URL can override the live Web URL;
// E2E_API_URL targets the backend directly for signed live webhook calls.
// Remaining live prerequisites include scenario data (inbox threads with
// buyer timezones, quote/payment/CVP data), the onboarding, AI/billing and
// inbound email API surfaces used by the specs, and a warehouse UUID via
// NEXT_PUBLIC_DEFAULT_WAREHOUSE_ID for quote conversion and shipment.
const localWebURL = "http://localhost:3000";
const baseURL = isMockMode ? localWebURL : (process.env.E2E_BASE_URL ?? localWebURL);

export default defineConfig({
  testDir: "./specs",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  timeout: 60_000, // Includes cold Next.js route compilation in mock mode.
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },
  ...(isMockMode
    ? {
        webServer: {
          cwd: "../../apps/web", // Relative to this config.
          command: "pnpm run dev",
          env: { NEXT_PUBLIC_USE_MOCK_API: "true" },
          url: new URL("/login", baseURL).href,
          reuseExistingServer: false, // Never silently run mock against an existing live app.
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
