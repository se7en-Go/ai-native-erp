// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LiveChatWidgetApp } from "../widget-app";

const API_BASE = "https://api.example.test";

const sessionPayload = {
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

describe("LiveChatWidgetApp", () => {
  const fetchMock = vi.fn();
  const apps: LiveChatWidgetApp[] = [];

  function lastShadow(): ShadowRoot {
    const hosts = document.querySelectorAll("#erp-livechat-host");
    const host = hosts[hosts.length - 1];
    if (!host.shadowRoot) throw new Error("widget host has no shadow root");
    return host.shadowRoot;
  }

  function createApp(): LiveChatWidgetApp {
    const app = new LiveChatWidgetApp({
      siteKey: "site_abc",
      apiBase: API_BASE,
      businessName: "Quanzhou LED Co.",
    });
    apps.push(app);
    return app;
  }

  beforeEach(() => {
    fetchMock.mockReset();
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      if (url === `${API_BASE}/api/v1/widget/session`) {
        return Promise.resolve(jsonResponse(sessionPayload));
      }
      if (url === `${API_BASE}/api/v1/widget/messages` && init?.method === "POST") {
        const body = JSON.parse(init.body as string) as { body_text: string };
        return Promise.resolve(
          jsonResponse({
            id: "m2",
            direction: "inbound",
            sender_type: "customer",
            body_text: body.body_text,
            created_at: "2026-11-19T00:01:00.000Z",
          }),
        );
      }
      if (url === `${API_BASE}/api/v1/widget/messages`) {
        return Promise.resolve(jsonResponse({ items: sessionPayload.messages }));
      }
      if (url === `${API_BASE}/api/v1/widget/identify`) {
        return Promise.resolve(
          jsonResponse({
            session_token: "tok_upgraded",
            customer_id: "cust-vip",
            merged: true,
          }),
        );
      }
      return Promise.resolve(jsonResponse({}, false, 404));
    });
    vi.stubGlobal("fetch", fetchMock);
    document.body.innerHTML = "";
    window.localStorage.clear();
  });

  afterEach(() => {
    for (const app of apps) app.close();
    apps.length = 0;
    vi.unstubAllGlobals();
    vi.useRealTimers();
    document.body.innerHTML = "";
    window.localStorage.clear();
  });

  it("renders a launcher and a closed panel inside a Shadow DOM host", () => {
    createApp();
    const shadow = lastShadow();

    expect(shadow.querySelector(".launcher")).not.toBeNull();
    expect(shadow.querySelector(".launcher")?.textContent).toBe("Chat");
    const panel = shadow.querySelector(".panel");
    expect(panel).not.toBeNull();
    expect(panel?.classList.contains("open")).toBe(false);
    // Panel lives in the shadow tree, not in the light DOM.
    expect(document.querySelector(".panel")).toBeNull();
    expect(shadow.querySelector("style")).not.toBeNull();
  });

  it("pre-chat marketing opt-in checkbox is NEVER pre-checked", () => {
    createApp();
    const shadow = lastShadow();

    const checkbox = shadow.querySelector(
      '.prechat input[type="checkbox"]',
    ) as HTMLInputElement | null;
    expect(checkbox).not.toBeNull();
    expect(checkbox?.checked).toBe(false);
    expect(checkbox?.hasAttribute("checked")).toBe(false);
  });

  it("open() shows the panel, creates a session and renders its messages", async () => {
    const app = createApp();

    await app.open();

    const shadow = lastShadow();
    expect(shadow.querySelector(".panel")?.classList.contains("open")).toBe(true);
    expect(fetchMock).toHaveBeenCalledWith(
      `${API_BASE}/api/v1/widget/session`,
      expect.objectContaining({ method: "POST" }),
    );
    expect(window.localStorage.getItem("erp_livechat_site_abc_vid")).toBe("visitor-1");
    const bubbles = shadow.querySelectorAll(".body .msg");
    expect(bubbles.length).toBe(1);
    expect(bubbles[0].textContent).toBe("Hello, how can I help?");
    expect(bubbles[0].classList.contains("outbound")).toBe(true);
  });

  it("close() hides the panel", async () => {
    const app = createApp();
    await app.open();

    app.close();

    expect(lastShadow().querySelector(".panel")?.classList.contains("open")).toBe(
      false,
    );
  });

  it("toggle() opens then closes the panel", async () => {
    const app = createApp();

    await app.toggle();
    expect(lastShadow().querySelector(".panel")?.classList.contains("open")).toBe(true);

    await app.toggle();
    expect(lastShadow().querySelector(".panel")?.classList.contains("open")).toBe(
      false,
    );
  });

  it("sending a message appends an inbound bubble and clears the input", async () => {
    const app = createApp();
    await app.open();
    const shadow = lastShadow();
    const input = shadow.querySelector(
      "form.footer input",
    ) as HTMLInputElement | null;
    const form = shadow.querySelector("form.footer") as HTMLFormElement | null;
    expect(input).not.toBeNull();
    expect(form).not.toBeNull();

    input!.value = "Is IP67 strip dimmable?";
    form!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await vi.waitFor(() => {
      expect(shadow.querySelectorAll(".body .msg").length).toBe(2);
    });

    const bubbles = shadow.querySelectorAll(".body .msg");
    expect(bubbles[1].textContent).toBe("Is IP67 strip dimmable?");
    expect(bubbles[1].classList.contains("inbound")).toBe(true);
    expect(input!.value).toBe("");
    expect(fetchMock).toHaveBeenCalledWith(
      `${API_BASE}/api/v1/widget/messages`,
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({
          Authorization: "Bearer tok_abc123",
        }),
      }),
    );
  });

  it("identify() calls /identify and later sends use the upgraded token", async () => {
    const app = createApp();
    await app.open();

    await app.identify({
      email: "returning.vip@ledbuyer.de",
      timestamp: 1763520000,
      hmac_signature: "valid_sig",
    });

    expect(fetchMock).toHaveBeenCalledWith(
      `${API_BASE}/api/v1/widget/identify`,
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({
          Authorization: "Bearer tok_abc123",
        }),
      }),
    );

    const shadow = lastShadow();
    const input = shadow.querySelector("form.footer input") as HTMLInputElement;
    const form = shadow.querySelector("form.footer") as HTMLFormElement;
    input.value = "hello again";
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await vi.waitFor(() => {
      expect(shadow.querySelectorAll(".body .msg").length).toBe(2);
    });
    const sendCall = fetchMock.mock.calls.find(
      ([url, init]) =>
        url === `${API_BASE}/api/v1/widget/messages` &&
        (init as RequestInit | undefined)?.method === "POST",
    );
    expect(sendCall).toBeDefined();
    expect((sendCall?.[1] as RequestInit).headers).toEqual(
      expect.objectContaining({ Authorization: "Bearer tok_upgraded" }),
    );
  });

  it("polls /messages while open", async () => {
    vi.useFakeTimers();
    const app = createApp();

    await app.open();
    expect(fetchMock.mock.calls.filter(([url]) => url.endsWith("/messages")).length).toBe(
      0,
    );

    await vi.advanceTimersByTimeAsync(4000);

    const pollCalls = fetchMock.mock.calls.filter(
      ([url, init]) =>
        url === `${API_BASE}/api/v1/widget/messages` &&
        (init as RequestInit | undefined)?.method !== "POST",
    );
    expect(pollCalls.length).toBeGreaterThanOrEqual(1);
    expect(pollCalls[0][1]).toEqual(
      expect.objectContaining({
        headers: { Authorization: "Bearer tok_abc123" },
      }),
    );
  });
});
