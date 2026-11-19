import { test, expect } from "@playwright/test";
import {
  collectPageErrors,
  loginViaUi,
  postSignedWebhook,
  signHmacSha256Hex,
} from "../helpers/auth-and-seed";
import { isMockMode } from "../helpers/environment";

test.describe("Hybrid B2B Flow: Made-in-China Inquiry -> L1 CRM/Quote -> 1-Click Order -> 30%/70% T/T -> 17TRACK -> CVP", () => {
  test("executes complete B2B quote-to-CVP margin lifecycle", async ({ page, request }) => {
    const errors = collectPageErrors(page);
    const emailSecret = process.env.RESEND_WEBHOOK_SECRET ?? "dev-resend-secret";
    const track17Secret = process.env.TRACK17_WEBHOOK_SECRET ?? "dev-17track-secret";

    if (!isMockMode) {
      // Live mode only: seed the inquiry through the real email webhook.
      // Mock mode already ships Hans Weber / Berlin Lighting in mock-fixtures.
      const inquiryBody = {
        event_id: `mic-e2e-${Date.now()}`,
        from_email: "en_notification@made-in-china.com",
        reply_to_email: "hans.weber@berlin-lighting.de",
        buyer_name: "Hans Weber",
        company_name: "Berlin Lighting GmbH",
        country_code: "DE",
        iana_timezone: "Europe/Berlin",
        to_address: "quanzhou-led-factory@inbound.mail.example.com",
        subject: "RFQ: 500 pcs LED-COB-24V-5M FOB Shenzhen",
        text_body:
          "Hello, we need FOB Shenzhen quotation for 500 pcs LED-COB-24V-5M. Payment 30% T/T deposit and 70% before shipment.",
      };
      const rawInquiry = JSON.stringify(inquiryBody);
      await postSignedWebhook(
        request,
        "/api/v1/webhooks/email/inbound",
        inquiryBody,
        "X-Webhook-Signature",
        signHmacSha256Hex(rawInquiry, emailSecret)
      );
    }

    await loginViaUi(page, "owner@quanzhou-led.example.com", "FactoryPass2026!");
    await page.goto("/inbox");

    const inquiry = page.getByTestId("inbox-conversation-list")
      .getByRole("button", { name: /^Hans Weber — / })
      .filter({ has: page.getByText("b2b_inquiry", { exact: true }) });
    await expect(inquiry).toBeVisible({ timeout: 15_000 });
    await inquiry.click();
    const thread = page.getByTestId("inbox-message-thread");
    await expect(thread.getByRole("heading", { name: "Hans Weber", exact: true })).toBeVisible();
    await expect(thread.getByTestId("buyer-local-time-badge")).toBeVisible();

    // Approve the L1 quote draft, not whichever proposal happens to appear first.
    await page.goto("/ai/proposals");
    const quoteDraft = page.locator('[data-testid^="proposal-card-"]').filter({
      has: page.getByText("quote_draft", { exact: true }),
    });
    const approveBtn = quoteDraft.getByRole("button", { name: "Approve", exact: true });
    await expect(approveBtn).toBeVisible({ timeout: 15_000 });
    const [approval] = await Promise.all([
      page.waitForResponse((res) =>
        res.request().method() === "POST" &&
        /^\/api\/bff\/v1\/ai\/proposals\/[^/]+\/approve$/.test(new URL(res.url()).pathname)
      ),
      approveBtn.click(),
    ]);
    expect(approval.ok()).toBe(true);

    // Verify B2B Quote PI and 1-Click Convert to Order
    await page.goto("/b2b/quotes");
    const convertBtn = page.locator('[data-testid^="convert-quote-btn-"]').first();
    await expect(convertBtn).toBeVisible({ timeout: 15_000 });
    await convertBtn.click();
    await expect(page.getByText(/Converted 1-Click to Order/)).toBeVisible();

    // Verify 70% balance unpaid blocks dispatch on /orders
    await page.goto("/orders");
    await expect(page.locator('[data-testid^="order-ship-blocked-"]').first()).toBeVisible();

    // Verify the pending 30% deposit; the 70% balance is still unpaid.
    await loginViaUi(page, "finance@quanzhou-led.example.com", "FactoryPass2026!");
    await page.goto("/finance");
    const deposit = page.getByRole("row").filter({
      has: page.getByText("b2b_30_deposit", { exact: true }),
    });
    const balance = page.getByRole("row").filter({
      has: page.getByText("b2b_70_balance", { exact: true }),
    });
    await expect(balance.getByText("70% Balance Unpaid — Shipment Blocked")).toBeVisible();
    const [verification] = await Promise.all([
      page.waitForResponse((res) =>
        res.request().method() === "POST" &&
        /^\/api\/bff\/api\/v1\/finance\/payments\/[^/]+\/verify$/.test(new URL(res.url()).pathname)
      ),
      deposit.getByRole("button", { name: /^Verify \(/ }).click(),
    ]);
    expect(verification.ok()).toBe(true);

    if (!isMockMode) {
      // Live mode only: trigger 17TRACK Delivered webhook.
      const trackPayload = {
        event: "TRACKING_UPDATED",
        data: {
          number: "DHL99887766",
          track_info: {
            latest_status: { status: "Delivered", sub_status: "Delivered_Other" },
            latest_event: {
              time_utc: new Date().toISOString(),
              location: "Berlin, DE",
              description: "Delivered to consignee",
            },
          },
        },
      };
      await postSignedWebhook(
        request,
        "/api/v1/webhooks/17track",
        trackPayload,
        "sign",
        signHmacSha256Hex(JSON.stringify(trackPayload), track17Secret)
      );
    }

    // Assert CVP net margin waterfall renders cleanly with zero [object Object].
    // Reload with domcontentloaded (not the default load event): the page
    // holds an open SSE stream and the dev server can be slow to settle
    // the load event on small runners, which flaked this reload at the
    // 30 s test timeout. The cvpCard expectation below is the real gate
    // and auto-waits for the refetched data.
    await page.reload({ waitUntil: "domcontentloaded" });
    const cvpCard = page.locator('[data-testid^="cvp-waterfall-"]').first();
    await expect(cvpCard).toBeVisible({ timeout: 15_000 });
    const text = await cvpCard.textContent();
    expect(text).toContain("Net Margin:");
    expect(text).not.toContain("[object Object]");
    expect(text).not.toContain("NaN");

    errors.assertClean();
  });
});
