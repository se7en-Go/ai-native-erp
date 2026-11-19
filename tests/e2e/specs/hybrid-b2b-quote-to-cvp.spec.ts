import { test, expect } from "@playwright/test";
import {
  collectPageErrors,
  isMockMode,
  loginViaUi,
  postSignedWebhook,
  signHmacSha256Hex,
} from "../helpers/auth-and-seed";

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

    // The list row reads "Hans Weber (DE)" while the thread header reads
    // "Hans Weber", so getByText is ambiguous — use the row testid.
    await expect(page.getByTestId("conversation-item-conv-1")).toBeVisible({
      timeout: 15_000,
    });
    await expect(page.getByTestId("buyer-local-time-badge")).toBeVisible();

    // Approve L1 CRM Extract & Quote Draft proposals
    await page.goto("/ai/proposals");
    const crmApproveButtons = page.locator('[data-testid^="proposal-approve-"]');
    await expect(crmApproveButtons.first()).toBeVisible({ timeout: 15_000 });
    await crmApproveButtons.first().click();

    // Verify B2B Quote PI and 1-Click Convert to Order
    await page.goto("/b2b/quotes");
    const convertBtn = page.locator('[data-testid^="convert-quote-btn-"]').first();
    await expect(convertBtn).toBeVisible({ timeout: 15_000 });
    await convertBtn.click();
    await expect(page.getByText(/Converted 1-Click to Order/)).toBeVisible();

    // Verify 70% balance unpaid blocks dispatch on /orders
    await page.goto("/orders");
    await expect(page.locator('[data-testid^="order-ship-blocked-"]').first()).toBeVisible();

    // Finance user verifies 30% deposit & 70% balance milestones
    await loginViaUi(page, "finance@quanzhou-led.example.com", "FactoryPass2026!");
    await page.goto("/finance");
    const verifyButtons = page.locator('[data-testid^="verify-payment-btn-"]');
    const count = await verifyButtons.count();
    for (let i = 0; i < count; i++) {
      await verifyButtons.first().click();
    }

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

    // Assert CVP net margin waterfall renders cleanly with zero [object Object]
    await page.reload();
    const cvpCard = page.locator('[data-testid^="cvp-waterfall-"]').first();
    await expect(cvpCard).toBeVisible();
    const text = await cvpCard.textContent();
    expect(text).toContain("Net Margin:");
    expect(text).not.toContain("[object Object]");
    expect(text).not.toContain("NaN");

    errors.assertClean();
  });
});
