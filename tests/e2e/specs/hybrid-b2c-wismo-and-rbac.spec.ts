import { test, expect } from "@playwright/test";
import {
  collectPageErrors,
  isMockMode,
  loginViaUi,
  postSignedWebhook,
  signHmacSha256Base64,
} from "../helpers/auth-and-seed";

test.describe("Hybrid B2C WISMO, Strict RAG Abstention & Agent RBAC", () => {
  test("handles Shopify/WebChat WISMO, Strict RAG abstention, and blocks Agent from Finance & COGS", async ({
    page,
    request,
  }) => {
    const errors = collectPageErrors(page);
    const shopifySecret = process.env.SHOPIFY_CLIENT_SECRET ?? "dev-shopify-secret";

    if (!isMockMode) {
      // Live mode only: seed the Shopify order through the real webhook.
      // Mock mode already ships the WISMO conversation in mock-fixtures.
      const orderPayload = {
        id: 9901042,
        name: "#SH-1042",
        email: "elena.vance@gmail.com",
        total_price: "89.90",
        currency: "USD",
        line_items: [{ sku: "LED-COB-24V-5M", quantity: 3, price: "29.90" }],
      };
      await postSignedWebhook(
        request,
        "/api/v1/webhooks/shopify",
        orderPayload,
        "X-Shopify-Hmac-SHA256",
        signHmacSha256Base64(JSON.stringify(orderPayload), shopifySecret)
      );
    }

    await loginViaUi(page, "owner@quanzhou-led.example.com", "FactoryPass2026!");
    await page.goto("/inbox");

    // Buyer local time + quiet-hours context switches per conversation.
    await expect(page.getByTestId("conversation-item-conv-1")).toBeVisible({
      timeout: 15_000,
    });
    await page.getByTestId("conversation-item-conv-1").click();
    await expect(page.getByTestId("buyer-local-time-badge")).toBeVisible();
    await expect(page.getByTestId("quiet-hours-pill")).toBeVisible();
    const berlinBadge = await page.getByTestId("buyer-local-time-badge").textContent();
    await page.getByTestId("conversation-item-conv-2").click();
    await expect(page.getByTestId("buyer-local-time-badge")).toBeVisible();
    const newYorkBadge = await page.getByTestId("buyer-local-time-badge").textContent();
    expect(newYorkBadge).not.toEqual(berlinBadge);
    await page.getByTestId("conversation-item-conv-1").click();

    // Per-message translation toggle + live-translate composer preview.
    const msgToggle = page.locator('[data-testid^="toggle-msg-translation-"]').first();
    await expect(msgToggle).toBeVisible();
    await msgToggle.click();
    await page.getByTestId("live-translate-toggle").click();
    await page.getByTestId("composer-textarea").fill("Hello, your order is on its way.");
    await expect(page.getByTestId("live-translation-preview")).toBeVisible({ timeout: 10_000 });
    await expect(page.getByTestId("live-translation-preview")).toContainText("Translated:");

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
    await expect(page.getByTestId("catalog-cost-header")).toHaveCount(0);
    await expect(page.getByText("LED-COB-24V-5M")).toBeVisible();

    errors.assertClean();
  });
});
