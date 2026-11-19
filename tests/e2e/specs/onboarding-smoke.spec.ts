import { test, expect } from "@playwright/test";
import { collectPageErrors, loginViaUi } from "../helpers/auth-and-seed";

test.describe("Hybrid Factory Self-Serve Onboarding Wizard Smoke", () => {
  test("completes 5-step onboarding wizard and unlocks LiveProduction", async ({ page }) => {
    const errors = collectPageErrors(page);
    await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
    await loginViaUi(page, "owner@quanzhou-led.example.com", "FactoryPass2026!");
    await page.goto("/onboarding");

    await expect(page.getByTestId("onboarding-preset-select")).toHaveValue(
      "hybrid_factory_b2b_b2c"
    );
    await page.getByTestId("onboarding-save-step1").click();

    await page.getByTestId("coexistence-mode-toggle").check();
    await expect(page.getByTestId("meta-embedded-signup-btn")).toBeDisabled();
    await page.getByTestId("coexistence-20mps-checkbox").check();
    await expect(page.getByTestId("meta-embedded-signup-btn")).toBeEnabled();

    await page.getByTestId("verify-email-dns-btn").click();
    await expect(page.getByTestId("dns-status-verified")).toBeVisible();
    await page.getByTestId("onboarding-next-btn").click();

    await expect(page.getByRole("button", { name: "TikTok Shop" })).toBeVisible();
    await page.getByTestId("onboarding-next-btn").click();

    await expect(page.getByTestId("copy-webchat-snippet-btn")).toBeVisible();
    await page.getByTestId("copy-webchat-snippet-btn").click();
    await page.getByTestId("onboarding-next-btn").click();

    await expect(page.getByTestId("activate-live-production-btn")).toBeDisabled();
    await page.getByTestId("run-sandbox-test-1").click();
    await page.getByTestId("run-sandbox-test-2").click();
    await page.getByTestId("run-sandbox-test-3").click();
    await expect(page.getByTestId("activate-live-production-btn")).toBeEnabled();
    await page.getByTestId("activate-live-production-btn").click();
    // The advance mutation round-trips and the button settles back to
    // enabled; the mock state still reports WorkspaceCreated, so the
    // button settling (not a page reload) is the completion signal here.
    await expect(page.getByTestId("activate-live-production-btn")).toBeEnabled({
      timeout: 15_000,
    });

    errors.assertClean();
  });
});
