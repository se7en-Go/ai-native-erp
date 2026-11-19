import { test, expect, type Locator } from "@playwright/test";
import {
  appJson,
  collectPageErrors,
  enqueueInquiry,
  loginViaUi,
  postSignedWebhook,
  shopifyDomain,
  signHmacSha256Base64,
  waitForInquiry,
} from "../helpers/auth-and-seed";
import { isMockMode } from "../helpers/environment";

async function expectBuyerClock(thread: Locator) {
  const badge = thread.getByTestId("buyer-local-time-badge");
  await expect(badge).toBeVisible();
  const badgeText = await badge.innerText();
  const match = badgeText.match(/(\d{1,2}):\d{2}\s*([AP]M)\s*\([^)]+\)/i);
  if (!match) throw new Error(`Expected a local time with timezone, got: ${badgeText}`);
  const hour = Number(match[1]) % 12 + (match[2].toUpperCase() === "PM" ? 12 : 0);
  const quietHours = thread.getByTestId("quiet-hours-pill");
  if (hour >= 21 || hour < 8) {
    await expect(quietHours).toBeVisible();
  } else {
    await expect(quietHours).toHaveCount(0);
  }
}

test.describe("Hybrid B2C WISMO, Strict RAG Abstention & Agent RBAC", () => {
  test("handles Shopify/WebChat WISMO, Strict RAG abstention, and blocks Agent from Finance & COGS", async ({
    page,
    request,
  }) => {
    const errors = collectPageErrors(page);
    const shopifySecret = process.env.SHOPIFY_CLIENT_SECRET ?? "dev-shopify-secret";

    const shopOrderId = `gid://shopify/Order/${process.env.E2E_SHOPIFY_ORDER_ID}`;
    async function deliverShopifyOrder(topic: string) {
      const orderPayload = { admin_graphql_api_id: shopOrderId, updated_at: new Date().toISOString() };
      expect(await postSignedWebhook(request, "/api/v1/webhooks/shopify", orderPayload, {
        "X-Shopify-Shop-Domain": shopifyDomain(),
        "X-Shopify-Topic": topic,
        "X-Shopify-Hmac-Sha256": signHmacSha256Base64(JSON.stringify(orderPayload), shopifySecret),
      })).toMatchObject({ status: "accepted" });
    }
    if (!isMockMode) {
      await enqueueInquiry(request); // Also supports running this spec on its own (deduplicated).
      await deliverShopifyOrder("orders/create");
    }

    await loginViaUi(page, "owner@quanzhou-led.example.com", "FactoryPass2026!");
    if (!isMockMode) {
      await waitForInquiry(page);
      const orders = async () => (await appJson(page, "/orders")).items;
      await expect.poll(orders, { timeout: 15_000 }).toEqual(expect.arrayContaining([
        expect.objectContaining({ external_order_id: shopOrderId, fulfillment_status: "confirmed_reserved" }),
      ]));
      await deliverShopifyOrder("orders/updated");
      await expect.poll(orders, { timeout: 15_000 }).toEqual(expect.arrayContaining([
        expect.objectContaining({ external_order_id: shopOrderId, fulfillment_status: "shipped" }),
      ]));
    }
    await page.goto("/inbox");

    const conversations = page.getByTestId("inbox-conversation-list");
    const thread = page.getByTestId("inbox-message-thread");
    const inquiry = conversations.getByRole("button")
      .filter({ has: page.getByText("b2b_inquiry", { exact: true }) }).first();
    const webChat = conversations.getByRole("button")
      .filter({ has: page.getByText("b2c_support", { exact: true }) })
      .filter({ has: page.getByText("web_livechat", { exact: true }) }).first();

    // Verify the selected buyer, then clock/quiet hours for each thread.
    // Two buyers may share a timezone; their displayed times need not differ.
    async function openThread(row: Locator) {
      await expect(row).toBeVisible({ timeout: 15_000 });
      const buyer = (await row.getAttribute("aria-label"))?.split(" — ")[0];
      if (!buyer) throw new Error("Conversation row must identify its buyer");
      await row.click();
      await expect(thread.getByRole("heading", { name: buyer, exact: true })).toBeVisible();
      await expectBuyerClock(thread);
      return buyer;
    }

    const inquiryBuyer = await openThread(inquiry);
    const supportBuyer = await openThread(webChat);
    expect(supportBuyer).not.toBe(inquiryBuyer);

    // Per-message translation toggle + live-translate composer preview.
    const msgToggle = page.locator('[data-testid^="toggle-msg-translation-"]').first();
    await expect(msgToggle).toBeVisible();
    await msgToggle.click();
    await page.getByTestId("live-translate-toggle").click();
    await page.getByTestId("composer-textarea").fill("Hello, your order is on its way.");
    const preview = page.getByTestId("live-translation-preview");
    await expect(preview).toBeVisible({ timeout: 10_000 });
    if (isMockMode) {
      await expect(preview).toContainText("Translated:");
    } else {
      // Live: the real /ai/translate pipeline charges credits and returns provider text.
      await expect(preview).toContainText("Preview (EN):");
      await expect(preview).toContainText("Your order is on its way.");
    }

    // Open Floating Copilot & toggle Strict RAG
    await page.getByTestId("copilot-fab-btn").click();
    await expect(page.getByTestId("copilot-drawer")).toBeVisible();
    const drawerText = await page.getByTestId("copilot-drawer").textContent();
    expect(drawerText).not.toContain("[object Object]");
    expect(drawerText).not.toContain("tax.filing.overdue");

    await page.getByTestId("copilot-mode-strict-rag").click();
    await page
      .getByTestId("copilot-query-input")
      .fill("Do you offer free helicopter shipping to Mars?");
    await page.getByTestId("copilot-query-submit").click();
    await expect(page.getByTestId("copilot-answer-box")).toContainText(/Abstained/i, {
      timeout: 15_000,
    });

    // Now test Agent RBAC boundaries
    await loginViaUi(page, "agent@quanzhou-led.example.com", "FactoryPass2026!");
    await page.goto("/inbox");
    await expect(page.locator('a[href="/finance"]')).toHaveCount(0);

    await page.goto("/finance");
    await expect(page.getByTestId("rbac-forbidden-guard")).toBeVisible();

    await page.goto("/catalog");
    const catalog = page.getByRole("table");
    const skuRow = catalog.getByRole("row").filter({
      has: page.getByText("LED-COB-24V-5M", { exact: true }),
    });
    await expect(skuRow).toBeVisible();
    await expect(catalog.getByTestId("catalog-cost-header")).toHaveCount(0);
    // Check the populated row too: a hidden heading alone must not mask a cost cell.
    await expect(skuRow.getByRole("cell")).toHaveCount(await catalog.getByRole("columnheader").count());

    errors.assertClean();
  });
});
