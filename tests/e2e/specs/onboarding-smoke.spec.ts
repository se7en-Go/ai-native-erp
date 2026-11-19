import { test, expect } from "@playwright/test";
import { appJson, collectPageErrors, demoSlug, loginViaUi } from "../helpers/auth-and-seed";
import { isMockMode } from "../helpers/environment";

test.describe("Hybrid Factory Self-Serve Onboarding Wizard Smoke", () => {
  test("completes 5-step onboarding wizard and unlocks LiveProduction", async ({ page }) => {
    const errors = collectPageErrors(page);
    await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
    if (isMockMode) {
      await loginViaUi(page, "owner@quanzhou-led.example.com", "FactoryPass2026!");
    } else {
      await loginViaUi(page, "onboarding@quanzhou-led.example.com", "FactoryPass2026!", `${demoSlug()}-onboarding`);
      expect(await appJson(page, "/onboarding/state")).toMatchObject({
        current_stage: "WorkspaceCreated", completed_steps: ["WorkspaceCreated"],
      });
    }
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
    const activate = page.getByTestId("activate-live-production-btn");
    await expect(activate).toBeEnabled();
    const [advance] = await Promise.all([
      page.waitForResponse((res) =>
        new URL(res.url()).pathname === "/api/bff/api/v1/onboarding/advance" &&
        res.request().method() === "POST"
      ),
      activate.click(),
    ]);
    expect(advance.ok()).toBe(true);
    expect(advance.request().postDataJSON()).toMatchObject({
      target_stage: "LiveProduction",
      step_metadata: { sandbox_passed: 3 },
    });
    if (isMockMode) {
      // The mock mutation acknowledges the transition but does not persist it.
      expect(await advance.json()).toMatchObject({ stage: "LiveProduction" });
    } else {
      expect(await advance.json()).toMatchObject({ current_stage: "LiveProduction" });
      expect(await appJson(page, "/onboarding/state")).toMatchObject({
        current_stage: "LiveProduction",
        completed_steps: ["WorkspaceCreated", "ChannelConnecting", "ChannelVerified",
          "CatalogSyncing", "AISandboxReady", "LiveProduction"],
      });
    }

    errors.assertClean();
  });
});
