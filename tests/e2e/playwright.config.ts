import { randomBytes } from "node:crypto";
import { defineConfig, devices } from "@playwright/test";

import { isMockMode } from "./helpers/environment";

const localWebURL = "http://localhost:3000";
const managedLive = !isMockMode && !process.env.E2E_BASE_URL && !process.env.E2E_API_URL;
const baseURL = isMockMode ? localWebURL : (process.env.E2E_BASE_URL ?? localWebURL);
if (managedLive && !process.env.E2E_RUN_SLUG) {
  // Worker processes inherit the same run namespace from the Playwright runner.
  const suffix = randomBytes(6).toString("hex");
  process.env.E2E_RUN_SLUG = `quanzhou-led-e2e-${suffix}`;
  process.env.E2E_SHOPIFY_ORDER_ID = BigInt(`0x${suffix}`).toString();
}

export default defineConfig({
  testDir: "./specs",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: isMockMode && process.env.CI ? 1 : 0,
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
    : managedLive
      ? {
          webServer: {
            cwd: "../..",
            command: "exec uv run --no-sync python tests/e2e/fixtures/live_stack.py",
            url: `${localWebURL}/login`,
            reuseExistingServer: false,
            timeout: 180_000,
            gracefulShutdown: { signal: "SIGTERM", timeout: 15_000 },
            stdout: "pipe",
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
