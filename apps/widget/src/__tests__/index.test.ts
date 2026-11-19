// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import ErpLiveChat from "../index";

describe("ErpLiveChat entrypoint", () => {
  afterEach(() => {
    ErpLiveChat.close();
    document.body.innerHTML = "";
    window.localStorage.clear();
  });

  it("exposes ErpLiveChat on window", () => {
    expect(window.ErpLiveChat).toBe(ErpLiveChat);
    expect(typeof ErpLiveChat.init).toBe("function");
    expect(typeof ErpLiveChat.open).toBe("function");
    expect(typeof ErpLiveChat.close).toBe("function");
    expect(typeof ErpLiveChat.identify).toBe("function");
  });

  it("init creates a widget instance and returns the same singleton", () => {
    const first = ErpLiveChat.init({
      siteKey: "site_abc",
      apiBase: "https://api.example.test",
      businessName: "Quanzhou LED Co.",
    });
    const second = ErpLiveChat.init({
      siteKey: "site_other",
      apiBase: "https://api.example.test",
    });

    expect(first).toBe(second);
    expect(document.querySelectorAll("#erp-livechat-host").length).toBe(1);
  });

  it("close() without an open panel does not throw", () => {
    expect(() => ErpLiveChat.close()).not.toThrow();
  });
});
