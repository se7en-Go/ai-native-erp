import crypto from "node:crypto";
import { expect, type Page, type APIRequestContext } from "@playwright/test";
import { isMockMode } from "./environment";

const E2E_API_URL = process.env.E2E_API_URL ?? "http://localhost:8000";

export function signHmacSha256Hex(rawBody: string, secret: string): string {
  return crypto.createHmac("sha256", secret).update(rawBody, "utf8").digest("hex");
}

export function signHmacSha256Base64(rawBody: string, secret: string | Buffer): string {
  return crypto.createHmac("sha256", secret).update(rawBody, "utf8").digest("base64");
}

/** 17TRACK v2.2 `sign` header: sha256(body + "/" + webhook_secret), lowercase hex. */
export function sign17Track(rawBody: string, secret: string): string {
  return crypto.createHash("sha256").update(rawBody + "/" + secret, "utf8").digest("hex");
}

export async function loginViaUi(
  page: Page,
  email: string,
  password = "FactoryPass2026!",
  tenantSlug = isMockMode ? "quanzhou-led-factory" : demoSlug()
) {
  await page.goto("/login");
  await page.fill("#tenant-slug", tenantSlug);
  await page.fill("#email", email);
  await page.fill("#password", password);
  await page.click('button[type="submit"]');
  await page.waitForURL(email.startsWith("finance@") ? /\/finance$/ : /\/(inbox|onboarding)$/);
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
  headers: Record<string, string>
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
      ...headers,
    },
  });
  if (!res.ok()) {
    throw new Error(`Webhook ${path} failed with ${res.status()}: ${await res.text()}`);
  }
  return res.json();
}

export function demoSlug() {
  return process.env.E2E_RUN_SLUG ?? "quanzhou-led-factory";
}

export const shopifyDomain = () => `${demoSlug()}.myshopify.com`;
export const trackingNumber = () => `DHL-${demoSlug().toUpperCase()}`;

export async function enqueueInquiry(request: APIRequestContext) {
  const id = `inquiry-${demoSlug()}`;
  const payload = {
    type: "email.received",
    data: {
      message_id: `<${id}@mail.example.com>`,
      from: "Made-in-China <en_notification@made-in-china.com>",
      reply_to: ["hans.weber@berlin-lighting.de"],
      to: [`${demoSlug()}@inbound.mail.example.com`],
      subject: "RFQ: 500 pcs LED-COB-24V-5M FOB Shenzhen",
      text: "Buyer Name: Hans Weber\nCompany Name: Berlin Lighting GmbH\nCountry/Region: Germany\n" +
        "Email: hans.weber@berlin-lighting.de\n" +
        "We need FOB Shenzhen quotation for 500 pcs LED-COB-24V-5M. Payment 30% T/T deposit.",
    },
  };
  const secret = process.env.RESEND_WEBHOOK_SECRET ?? "dev-resend-secret";
  const key = secret.startsWith("whsec_") ? Buffer.from(secret.slice(6), "base64") : secret;
  const timestamp = String(Math.floor(Date.now() / 1000));
  const signature = signHmacSha256Base64(`${id}.${timestamp}.${JSON.stringify(payload)}`, key);
  return postSignedWebhook(request, "/api/v1/webhooks/email", payload, {
    "svix-id": id, "svix-timestamp": timestamp, "svix-signature": `v1,${signature}`,
  });
}

export async function appJson(page: Page, path: string) {
  const response = await page.request.get(`/api/bff/api/v1${path}`);
  expect(response.ok(), `GET ${path}: ${response.status()}`).toBe(true);
  return response.json();
}

export async function waitForInquiry(page: Page) {
  await expect.poll(async () => (await appJson(page, "/inbox/conversations")).items,
    { timeout: 15_000 }).toEqual(expect.arrayContaining([
      expect.objectContaining({ subject: "RFQ: 500 pcs LED-COB-24V-5M FOB Shenzhen" }),
    ]));
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
      if (isMockMode && res.status() === 401 && new URL(res.url()).pathname === "/api/bff/v1/auth/me") {
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
