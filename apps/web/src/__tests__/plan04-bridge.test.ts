// @vitest-environment node
// Plan 04 generated SDK requests, plus compatibility checks for the remaining bridge.
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  approveProposal,
  assistantChat,
  deleteKbDocument,
  editApproveProposal,
  fetchCapabilities,
  fetchKbDocuments,
  fetchPendingProposalsCount,
  fetchWallet,
  listProposals,
  rejectProposal,
  translateText,
  updateCapability,
  uploadKbDocument,
} from "@/lib/api-client/live-client";
import {
  aiAssist,
  connectStorefront,
  fetchChannelAccounts,
  fetchChannelConsents,
  fetchChannelDlq,
  fetchChannelSuppressions,
  fetchCustomerContext,
  runSandboxTest,
  saveWebchat,
  verifyEmailDns,
} from "@/lib/api-client/plan04-bridge";

const BFF = "/api/bff";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function stubFetch(handler: (url: string, init?: RequestInit) => Response) {
  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    return handler(String(input), init);
  });
  vi.stubGlobal("fetch", mock);
  return mock;
}

function stubSdkFetch(handler: (url: string) => Response) {
  const mock = vi.fn(async (url: string, _init?: RequestInit) => handler(url));
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    if (input instanceof Request) {
      return mock(new URL(input.url).pathname + new URL(input.url).search, {
        method: input.method,
        body: await input.text(),
      });
    }
    return mock(String(input), init);
  });
  return mock;
}

describe("Plan 04 API over BFF", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("listProposals passes status/typeFilter as query params", async () => {
    const fetchMock = stubSdkFetch(() =>
      jsonResponse({ items: [], total: 0, limit: 20, offset: 0, has_more: false }),
    );
    await listProposals("pending", "quote_draft");
    expect(fetchMock.mock.calls[0][0]).toBe(
      `${BFF}/api/v1/ai/proposals?status=pending&limit=20&offset=0&proposal_type=quote_draft`,
    );
    await listProposals("approved", "");
    expect(fetchMock.mock.calls[1][0]).toBe(
      `${BFF}/api/v1/ai/proposals?status=approved&limit=20&offset=0`,
    );
  });

  it("fetchPendingProposalsCount uses limit=1", async () => {
    const fetchMock = stubSdkFetch(() => jsonResponse({ total: 3 }));
    const res = await fetchPendingProposalsCount();
    expect(fetchMock.mock.calls[0][0]).toBe(
      `${BFF}/api/v1/ai/proposals?status=pending&limit=1&offset=0`,
    );
    expect(res.total).toBe(3);
  });

  it("proposal decisions post to the exact Plan 04 paths with contract bodies", async () => {
    const fetchMock = stubSdkFetch(() => jsonResponse({ ok: true }));
    await approveProposal("prop-1");
    expect(fetchMock.mock.calls[0][0]).toBe(`${BFF}/api/v1/ai/proposals/prop-1/approve`);
    expect(fetchMock.mock.calls[0][1]?.method).toBe("POST");

    await editApproveProposal("prop-1", { total_amount: "7000.00" });
    expect(fetchMock.mock.calls[1][0]).toBe(
      `${BFF}/api/v1/ai/proposals/prop-1/edit-approve`,
    );
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({
      edited_payload: { total_amount: "7000.00" },
    });

    await rejectProposal("prop-1", "Price too high");
    expect(fetchMock.mock.calls[2][0]).toBe(`${BFF}/api/v1/ai/proposals/prop-1/reject`);
    expect(JSON.parse(String(fetchMock.mock.calls[2][1]?.body))).toEqual({
      reason: "Price too high",
    });
  });

  it("capabilities, wallet and kb documents use the locked paths", async () => {
    const fetchMock = stubSdkFetch((url) => {
      if (url.includes("/ai/capabilities")) {
        return jsonResponse({ capabilities: [] });
      }
      if (url.includes("/billing/wallet")) {
        return jsonResponse({
          plan_tier: "Growth",
          included_credits_remaining: 4200,
          topup_credits_balance: 0,
        });
      }
      return jsonResponse({ items: [], total: 0, limit: 20, offset: 0, has_more: false });
    });
    await fetchCapabilities();
    expect(fetchMock.mock.calls[0][0]).toBe(`${BFF}/api/v1/ai/capabilities`);
    expect((await fetchWallet()).included_credits_remaining).toBe(4200);
    expect(fetchMock.mock.calls[1][0]).toBe(`${BFF}/api/v1/billing/wallet`);
    await fetchKbDocuments();
    expect(fetchMock.mock.calls[2][0]).toBe(`${BFF}/api/v1/ai/kb/documents`);

    await updateCapability("cs_support", "L1_proposal");
    expect(fetchMock.mock.calls[3][0]).toBe(`${BFF}/api/v1/ai/capabilities`);
    expect(fetchMock.mock.calls[3][1]?.method).toBe("PATCH");
    expect(JSON.parse(String(fetchMock.mock.calls[3][1]?.body))).toEqual({
      settings: { cs_support: "L1_proposal" },
    });

    const form = new FormData();
    form.append("file", new Blob(["x"]), "doc.pdf");
    form.append("title", "Demo KB");
    await uploadKbDocument(form);
    expect(String(fetchMock.mock.calls[4][1]?.body)).toContain('filename="doc.pdf"');
    expect(fetchMock.mock.calls[4][0]).toBe(`${BFF}/api/v1/ai/kb/documents`);
    expect(fetchMock.mock.calls[4][1]?.method).toBe("POST");

    await deleteKbDocument("doc-1");
    expect(fetchMock.mock.calls[5][0]).toBe(`${BFF}/api/v1/ai/kb/documents/doc-1`);
    expect(fetchMock.mock.calls[5][1]?.method).toBe("DELETE");
  });

  it("assistantChat and translateText send the exact bodies", async () => {
    const fetchMock = stubSdkFetch(() =>
      jsonResponse({
        reply_text: "ok",
        citations: [],
        should_abstain: false,
      }),
    );
    await assistantChat({
      capability: "cs_support",
      message: "Do you offer free helicopter shipping to Mars?",
      strict_rag: true,
      channel_type: "web_livechat",
      context: { mode: "strict_rag" },
    });
    expect(fetchMock.mock.calls[0][0]).toBe(`${BFF}/api/v1/ai/assistant/chat`);
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toMatchObject({
      capability: "cs_support",
      strict_rag: true,
      message: "Do you offer free helicopter shipping to Mars?",
    });

    stubSdkFetch(() => jsonResponse({ translated_text: "Translated: Hola" }));
    const res = await translateText("Hola", "en");
    expect(res.translated_text).toBe("Translated: Hola");
  });

  it("aiAssist posts action/draft_text/strict_rag to the conversation path", async () => {
    const fetchMock = stubFetch(() =>
      jsonResponse({ text: "Draft: hi", abstained: false }),
    );
    const res = await aiAssist("conv-1", {
      action: "draft",
      draft_text: "hello",
      strict_rag: false,
    });
    expect(fetchMock.mock.calls[0][0]).toBe(
      `${BFF}/v1/inbox/conversations/conv-1/ai-assist`,
    );
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({
      action: "draft",
      draft_text: "hello",
      strict_rag: false,
    });
    expect(res.text).toBe("Draft: hi");
  });

  it("customer context keeps the /identity path (no contract equivalent)", async () => {
    const fetchMock = stubFetch(() => jsonResponse({ customer: { id: "cust-1" } }));
    await fetchCustomerContext("cust-1");
    expect(fetchMock.mock.calls[0][0]).toBe(
      `${BFF}/v1/identity/customers/cust-1/context`,
    );
  });

  it("onboarding bridge mutations keep their pre-contract paths and bodies", async () => {
    const fetchMock = stubFetch((url) => {
      if (url.includes("verify-dns")) {
        return jsonResponse({
          spf_verified: true,
          dkim_verified: true,
          return_path_verified: true,
        });
      }
      if (url.includes("sandbox-test")) {
        return jsonResponse({ passed: true, reply_text: "Verified grounded response" });
      }
      return jsonResponse({ ok: true });
    });
    const dns = await verifyEmailDns("mail.qzled.example.com");
    expect(fetchMock.mock.calls[0][0]).toBe(`${BFF}/v1/onboarding/email/verify-dns`);
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({
      domain: "mail.qzled.example.com",
    });
    expect(dns.spf_verified).toBe(true);

    const sandbox = await runSandboxTest("b2c_wismo", "Hi, where is my order?");
    expect(fetchMock.mock.calls[1][0]).toBe(`${BFF}/v1/onboarding/sandbox-test`);
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({
      scenario_id: "b2c_wismo",
      message: "Hi, where is my order?",
    });
    expect(sandbox.passed).toBe(true);

    await connectStorefront({
      platform: "shopify",
      shop_domain: "qzled.myshopify.com",
      auth_mode: "oauth",
      credentials: {},
    });
    expect(fetchMock.mock.calls[2][0]).toBe(`${BFF}/v1/onboarding/storefront/connect`);

    await saveWebchat(["https://shop.qzled.example.com"]);
    expect(fetchMock.mock.calls[3][0]).toBe(`${BFF}/v1/onboarding/webchat`);
    expect(fetchMock.mock.calls[3][1]?.method).toBe("PUT");
    expect(JSON.parse(String(fetchMock.mock.calls[3][1]?.body))).toEqual({
      allowed_origins: ["https://shop.qzled.example.com"],
    });
  });

  it("channel lists keep their paths (no contract list operation yet)", async () => {
    const fetchMock = stubFetch(() =>
      jsonResponse({ items: [], total: 0, limit: 20, offset: 0, has_more: false }),
    );
    await fetchChannelAccounts();
    expect(fetchMock.mock.calls[0][0]).toBe(`${BFF}/v1/channels/accounts`);
    await fetchChannelDlq();
    expect(fetchMock.mock.calls[1][0]).toBe(`${BFF}/v1/channels/dlq`);
    await fetchChannelConsents();
    expect(fetchMock.mock.calls[2][0]).toBe(`${BFF}/v1/channels/consents`);
    await fetchChannelSuppressions();
    expect(fetchMock.mock.calls[3][0]).toBe(`${BFF}/v1/channels/suppressions`);
  });
});
