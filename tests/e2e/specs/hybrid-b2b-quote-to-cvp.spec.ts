import { test, expect } from "@playwright/test";
import {
  appJson,
  collectPageErrors,
  enqueueInquiry,
  loginViaUi,
  postSignedWebhook,
  sign17Track,
  trackingNumber,
  waitForInquiry,
} from "../helpers/auth-and-seed";
import { isMockMode } from "../helpers/environment";

test.describe("Hybrid B2B Flow: Made-in-China Inquiry -> L1 CRM/Quote -> 1-Click Order -> 30%/70% T/T -> 17TRACK -> CVP", () => {
  test("executes complete B2B quote-to-CVP margin lifecycle", async ({ page, request }) => {
    const errors = collectPageErrors(page);
    const track17Secret = process.env.TRACK17_WEBHOOK_SECRET ?? "dev-17track-secret";

    if (!isMockMode) {
      expect(await enqueueInquiry(request)).toMatchObject({ status: "accepted" });
    }

    await loginViaUi(page, "owner@quanzhou-led.example.com", "FactoryPass2026!");
    if (!isMockMode) await waitForInquiry(page);
    await page.goto("/inbox");

    const inquiry = page.getByTestId("inbox-conversation-list")
      .getByRole("button", { name: /^Hans Weber — / })
      .filter({ has: page.getByText("b2b_inquiry", { exact: true }) });
    await expect(inquiry).toBeVisible({ timeout: 15_000 });
    await inquiry.click();
    const thread = page.getByTestId("inbox-message-thread");
    await expect(thread.getByRole("heading", { name: "Hans Weber", exact: true })).toBeVisible();
    await expect(thread.getByTestId("buyer-local-time-badge")).toBeVisible();
    if (!isMockMode) {
      await expect(page.getByTestId("inbox-context-drawer").getByText("hans.weber@berlin-lighting.de")).toBeVisible();
    }

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
        /^\/api\/bff\/api\/v1\/ai\/proposals\/[^/]+\/approve$/.test(new URL(res.url()).pathname)
      ),
      approveBtn.click(),
    ]);
    expect(approval.ok()).toBe(true);
    const approvedQuoteId = isMockMode ? null : (await approval.json()).execution_result?.quote_id;
    if (!isMockMode) {
      expect(approvedQuoteId).toBeTruthy();
      const sent = await page.request.post(`/api/bff/api/v1/b2b/quotes/${approvedQuoteId}/send`);
      expect(sent.ok(), await sent.text()).toBe(true);
      expect(await sent.json()).toMatchObject({ status: "sent" });
    }

    // Convert the PI created by the approved proposal, after recording buyer acceptance.
    await page.goto("/b2b/quotes");
    const convertBtn = approvedQuoteId
      ? page.getByTestId(`convert-quote-btn-${approvedQuoteId}`)
      : page.locator('[data-testid^="convert-quote-btn-"]').first();
    await expect(convertBtn).toBeVisible({ timeout: 15_000 });
    if (!isMockMode) await page.getByRole("button", { name: "Mark Accepted" }).click();
    const [converted] = await Promise.all([
      page.waitForResponse((res) => res.request().method() === "POST" &&
        res.url().endsWith("/convert-to-order")),
      convertBtn.click(),
    ]);
    expect(converted.ok()).toBe(true);
    await expect(page.getByText(/Converted 1-Click to Order/)).toBeVisible();
    const { order } = await converted.json();

    // Verify 70% balance unpaid blocks dispatch on /orders
    await page.goto("/orders");
    await expect(page.locator('[data-testid^="order-ship-blocked-"]').first()).toBeVisible();

    // Verify the pending 30% deposit; the 70% balance is still unpaid.
    await loginViaUi(page, "finance@quanzhou-led.example.com", "FactoryPass2026!");
    if (!isMockMode) {
      const { items } = await appJson(page, `/finance/receivables?order_id=${order.id}`);
      const milestone = items.find((r: { milestone_label: string }) =>
        r.milestone_label === "b2b_30_deposit");
      expect(milestone).toBeTruthy();
      const receipt = await page.request.post(`/api/bff/api/v1/finance/receivables/${milestone.id}/payments`, {
        multipart: {
          payment_method: "wire_transfer", external_tx_ref: `DEMO-${order.id}`,
          amount_received: milestone.due_amount, currency: milestone.currency, actual_fx_rate: "1",
          receipt_file: { name: "deposit.txt", mimeType: "text/plain", buffer: Buffer.from("Demo T/T receipt") },
        },
      });
      expect(receipt.status(), await receipt.text()).toBe(201);
      expect(await receipt.json()).toMatchObject({ verified_by_user_id: null });
    }
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
      // Exercise provider delivery on a separately seeded, already shipped order.
      // The new quote's order must stay blocked while its balance is unpaid.
      const { items } = await appJson(page, "/orders");
      const shipped = items.find((row: { order_no: string }) => row.order_no === "DEMO-SHIPPED");
      expect(shipped).toBeTruthy();
      const trackPayload = {
        event: "TRACKING_UPDATED",
        data: {
          number: trackingNumber(),
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
        { sign: sign17Track(JSON.stringify(trackPayload), track17Secret) }
      );
      await expect.poll(() => appJson(page, `/orders/${shipped.id}/shipments`),
        { timeout: 15_000 }).toEqual(expect.arrayContaining([
          expect.objectContaining({ tracking_number: trackingNumber(), standardized_status: "delivered" }),
        ]));
    }

    // Assert CVP net margin waterfall renders cleanly with zero [object Object].
    // Reload with domcontentloaded (not the default load event): the page
    // holds an open SSE stream and the dev server can be slow to settle
    // the load event on small runners, which flaked this reload at the
    // 30 s test timeout. The cvpCard expectation below is the real gate
    // and auto-waits for the refetched data.
    await page.reload({ waitUntil: "domcontentloaded" });
    const cvpCard = isMockMode ? page.locator('[data-testid^="cvp-waterfall-"]').first()
      : page.getByTestId(`cvp-waterfall-${order.id}`);
    await expect(cvpCard).toBeVisible({ timeout: 15_000 });
    const text = await cvpCard.textContent();
    expect(text).toContain("Net Margin:");
    expect(text).not.toContain("[object Object]");
    expect(text).not.toContain("NaN");

    errors.assertClean();
  });
});
