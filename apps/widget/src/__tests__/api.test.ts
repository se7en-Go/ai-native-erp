import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WidgetApiClient } from "../api";
import type { WidgetSessionResponse } from "../api";

const API_BASE = "https://api.example.test";

const sessionResponse: WidgetSessionResponse = {
  session_token: "tok_abc123",
  visitor_id: "visitor-1",
  customer_id: "cust-1",
  conversation_id: "conv-1",
  business_name: "Quanzhou LED Co.",
  messages: [
    {
      id: "m1",
      direction: "outbound",
      sender_type: "human_agent",
      body_text: "Hello, how can I help?",
      created_at: "2026-11-19T00:00:00.000Z",
    },
  ],
};

function jsonResponse(payload: unknown, ok = true, status = 200): Response {
  return {
    ok,
    status,
    json: () => Promise.resolve(payload),
  } as unknown as Response;
}

describe("WidgetApiClient", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("createSession posts to /api/v1/widget/session and returns the session", async () => {
    fetchMock.mockResolvedValue(jsonResponse(sessionResponse));
    const client = new WidgetApiClient(API_BASE);

    const result = await client.createSession({
      site_key: "site_abc",
      page_url: "https://ledstore.example.com/products/cob-24v",
      sku: "LED-COB-24V",
    });

    expect(result).toEqual(sessionResponse);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${API_BASE}/api/v1/widget/session`);
    expect(init.method).toBe("POST");
    expect(init.headers).toEqual({ "Content-Type": "application/json" });
    expect(JSON.parse(init.body as string)).toEqual({
      site_key: "site_abc",
      page_url: "https://ledstore.example.com/products/cob-24v",
      sku: "LED-COB-24V",
    });
  });

  it("createSession forwards the pre-chat opt-in payload", async () => {
    fetchMock.mockResolvedValue(jsonResponse(sessionResponse));
    const client = new WidgetApiClient(API_BASE);

    await client.createSession({
      site_key: "site_abc",
      page_url: "https://ledstore.example.com/",
      prechat_opt_in: {
        email: "buyer@example.de",
        whatsapp_phone: "+491701112233",
        marketing_opt_in: true,
      },
    });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string).prechat_opt_in).toEqual({
      email: "buyer@example.de",
      whatsapp_phone: "+491701112233",
      marketing_opt_in: true,
    });
  });

  it("createSession throws on a non-OK response", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: "Forbidden" }, false, 403));
    const client = new WidgetApiClient(API_BASE);

    await expect(
      client.createSession({
        site_key: "site_abc",
        page_url: "https://ledstore.example.com/",
      }),
    ).rejects.toThrow("Widget session error: 403");
  });

  it("listMessages sends the bearer token and returns items", async () => {
    const items = sessionResponse.messages;
    fetchMock.mockResolvedValue(jsonResponse({ items }));
    const client = new WidgetApiClient(API_BASE);

    const result = await client.listMessages("tok_abc123");

    expect(result).toEqual(items);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${API_BASE}/api/v1/widget/messages`);
    expect(init.headers).toEqual({ Authorization: "Bearer tok_abc123" });
  });

  it("listMessages throws on a non-OK response", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: "Unauthorized" }, false, 401));
    const client = new WidgetApiClient(API_BASE);

    await expect(client.listMessages("bad-token")).rejects.toThrow(
      "Widget list error: 401",
    );
  });

  it("sendMessage posts body_text, page_url and sku with the bearer token", async () => {
    const sent = {
      id: "m2",
      direction: "inbound",
      sender_type: "customer",
      body_text: "Is IP67 strip dimmable?",
      created_at: "2026-11-19T00:01:00.000Z",
    };
    fetchMock.mockResolvedValue(jsonResponse(sent));
    const client = new WidgetApiClient(API_BASE);

    const result = await client.sendMessage(
      "tok_abc123",
      "Is IP67 strip dimmable?",
      "https://ledstore.example.com/products/cob-24v",
      "LED-COB-24V",
    );

    expect(result).toEqual(sent);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${API_BASE}/api/v1/widget/messages`);
    expect(init.method).toBe("POST");
    expect(init.headers).toEqual({
      "Content-Type": "application/json",
      Authorization: "Bearer tok_abc123",
    });
    expect(JSON.parse(init.body as string)).toEqual({
      body_text: "Is IP67 strip dimmable?",
      page_url: "https://ledstore.example.com/products/cob-24v",
      sku: "LED-COB-24V",
    });
  });

  it("sendMessage throws on a non-OK response", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: "Bad request" }, false, 400));
    const client = new WidgetApiClient(API_BASE);

    await expect(
      client.sendMessage("tok_abc123", "hi", "https://ledstore.example.com/"),
    ).rejects.toThrow("Widget send error: 400");
  });

  it("identify posts the HMAC payload with the bearer token", async () => {
    const upgraded = {
      session_token: "tok_upgraded",
      customer_id: "cust-vip",
      merged: true,
    };
    fetchMock.mockResolvedValue(jsonResponse(upgraded));
    const client = new WidgetApiClient(API_BASE);

    const result = await client.identify("tok_abc123", {
      email: "returning.vip@ledbuyer.de",
      timestamp: 1763520000,
      hmac_signature: "valid_sig",
    });

    expect(result).toEqual(upgraded);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${API_BASE}/api/v1/widget/identify`);
    expect(init.method).toBe("POST");
    expect(init.headers).toEqual({
      "Content-Type": "application/json",
      Authorization: "Bearer tok_abc123",
    });
    expect(JSON.parse(init.body as string)).toEqual({
      email: "returning.vip@ledbuyer.de",
      timestamp: 1763520000,
      hmac_signature: "valid_sig",
    });
  });

  it("identify throws on a non-OK response", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: "Bad HMAC" }, false, 401));
    const client = new WidgetApiClient(API_BASE);

    await expect(
      client.identify("tok_abc123", {
        email: "returning.vip@ledbuyer.de",
        timestamp: 1763520000,
        hmac_signature: "bad_sig",
      }),
    ).rejects.toThrow("Widget identify error: 401");
  });
});
