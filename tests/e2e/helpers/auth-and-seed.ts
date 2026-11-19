import crypto from "node:crypto";
import type { Page, APIRequestContext } from "@playwright/test";

export const E2E_API_URL = process.env.E2E_API_URL ?? "http://localhost:8000";

/** "mock" runs the UI against NEXT_PUBLIC_USE_MOCK_API=true with no backend;
 * "live" runs the Plan 05 Task 5.8 full-stack contract (seeded backend). */
export const E2E_MODE = process.env.E2E_MODE ?? "mock";
export const isMockMode = E2E_MODE === "mock";
export const isLiveMode = E2E_MODE === "live";

export function signHmacSha256Hex(rawBody: string, secret: string): string {
  return crypto.createHmac("sha256", secret).update(rawBody, "utf8").digest("hex");
}

export function signHmacSha256Base64(rawBody: string, secret: string): string {
  return crypto.createHmac("sha256", secret).update(rawBody, "utf8").digest("base64");
}

export async function loginViaUi(
  page: Page,
  email: string,
  password = "FactoryPass2026!",
  tenantSlug = "quanzhou-led-factory"
) {
  await page.goto("/login");
  await page.fill("#tenant-slug", tenantSlug);
  await page.fill("#email", email);
  await page.fill("#password", password);
  await page.click('button[type="submit"]');
  await page.waitForURL(/\/(inbox|onboarding)/);
}

/**
 * Live-only: posts a signed webhook to the backend. Throws in mock mode —
 * mock specs must not call it; the mock fixtures already contain the
 * seeded conversations, proposals, quotes and orders.
 */
export async function postSignedWebhook(
  request: APIRequestContext,
  path: string,
  payload: Record<string, unknown>,
  headerName: string,
  signature: string
) {
  if (isMockMode) {
    throw new Error(
      `postSignedWebhook(${path}) is live-only; mock mode seeds data from mock-fixtures instead`
    );
  }
  const rawBody = JSON.stringify(payload);
  const res = await request.post(`${E2E_API_URL}${path}`, {
    data: rawBody,
    headers: {
      "Content-Type": "application/json",
      [headerName]: signature,
    },
  });
  if (!res.ok()) {
    throw new Error(`Webhook ${path} failed with ${res.status()}: ${await res.text()}`);
  }
  return res.json();
}

export interface PageErrorCollector {
  pageErrors: Error[];
  consoleErrors: string[];
  failedResponses: string[];
  assertClean: () => void;
}

// One known-benign console entry, by design in both modes: the auth
// session bridge probes GET /api/bff/v1/auth/me on every page load,
// including /login before any session exists; the backend (and the mock
// BFF) answers 401 and the bridge treats that as "no session". It is not
// an application error, so it is allowlisted here by exact URL+status.
// Everything else — any pageerror, any other console error, any other
// failed response — fails the test.
const BENIGN_FAILED_RESPONSE = /\/api\/bff\/v1\/auth\/me.* 401/;

/**
 * Records every uncaught pageerror, console error, and failed HTTP
 * response from the moment it is attached. Call assertClean() at the end
 * of each test: any entry fails the test with the captured messages.
 */
export function collectPageErrors(page: Page): PageErrorCollector {
  const pageErrors: Error[] = [];
  const consoleErrors: string[] = [];
  const failedResponses: string[] = [];
  let sawBenignAuthMe401 = false;
  page.on("pageerror", (err) => pageErrors.push(err));
  page.on("console", (msg) => {
    if (msg.type() === "error") consoleErrors.push(msg.text());
  });
  page.on("response", (res) => {
    if (res.status() >= 400) {
      const entry = `${res.url()} ${res.status()}`;
      if (BENIGN_FAILED_RESPONSE.test(entry)) {
        // The generic "Failed to load resource ... 401" console line
        // Chrome emits for this probe carries no URL; correlate it via
        // this flag so only this one probe is ever excused.
        sawBenignAuthMe401 = true;
      } else {
        failedResponses.push(entry);
      }
    }
  });
  return {
    pageErrors,
    consoleErrors,
    failedResponses,
    assertClean() {
      if (pageErrors.length > 0) {
        throw new Error(
          `Uncaught pageerror(s):\n${pageErrors.map((e) => ` - ${e.message}`).join("\n")}`
        );
      }
      if (consoleErrors.length > 0) {
        const unexpected = consoleErrors.filter(
          (m) =>
            !(
              sawBenignAuthMe401 &&
              /Failed to load resource.*401 \(Unauthorized\)/.test(m)
            )
        );
        if (unexpected.length > 0) {
          throw new Error(
            `Console error(s):\n${unexpected.map((m) => ` - ${m}`).join("\n")}\nFailed response(s) seen:\n${failedResponses.map((m) => ` - ${m}`).join("\n") || " (none recorded)"}`
          );
        }
      }
      if (failedResponses.length > 0) {
        throw new Error(
          `Failed response(s):\n${failedResponses.map((m) => ` - ${m}`).join("\n")}`
        );
      }
    },
  };
}
