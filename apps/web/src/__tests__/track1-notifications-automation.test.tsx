// Track 1 UI tests: the notification-channel + automation-rule panel.
//
// The live-path cases stub global fetch and assert the exact BFF URLs, methods and
// bodies the panel sends. The mock-mode case renders against
// `NEXT_PUBLIC_USE_MOCK_API=true` to prove the e2e-mock fallback never needs a backend.
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { NotificationAutomationPanel } from "@/components/ai/notification-automation-panel";
import { I18nProvider } from "@/lib/i18n";
import type {
  AutomationRuleRead,
  NotificationChannelRead,
  NotificationDeliveryRead,
} from "@/lib/api-client/types.gen";
import { normalizeFetchInput } from "@/__tests__/fetch-stub";

const CHANNEL_ID = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa";
const RULE_ID = "rrrrrrrr-1111-4111-8111-rrrrrrrrrrrr";
const NOW = "2026-11-26T09:00:00.000Z";

type Call = { url: string; method: string | undefined; body: string };

function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function channel(overrides: Partial<NotificationChannelRead> = {}): NotificationChannelRead {
  return {
    id: CHANNEL_ID,
    name: "运营飞书群",
    provider: "feishu",
    webhook_url_preview: "https://open.feishu.cn/***3456",
    has_signing_secret: true,
    min_priority: "P1",
    is_enabled: true,
    last_tested_at: NOW,
    last_error: null,
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

function rule(
  id: string,
  template: AutomationRuleRead["template_code"],
  priority: AutomationRuleRead["priority"],
  config: Record<string, unknown>,
): AutomationRuleRead {
  return {
    id,
    template_code: template,
    name: template,
    is_enabled: true,
    autonomy_level: "L1_proposal",
    priority,
    config_json: config,
    last_run_at: NOW,
    last_triggered_count: 3,
    created_at: NOW,
    updated_at: NOW,
  };
}

function delivery(): NotificationDeliveryRead {
  return {
    id: "dddddddd-1111-4111-8111-dddddddddddd",
    channel_id: CHANNEL_ID,
    channel_name: "运营飞书群",
    provider: "feishu",
    priority: "P0",
    event_type: "automation.negative_margin_hold",
    title: "P0 负毛利拦截",
    body: "P0 负毛利拦截",
    deep_link: null,
    status: "sent",
    attempts: 1,
    next_retry_at: null,
    http_status: 200,
    error_message: null,
    created_at: NOW,
    sent_at: NOW,
  };
}

interface FakeServer {
  channels: NotificationChannelRead[];
  rules: AutomationRuleRead[];
  deliveries: NotificationDeliveryRead[];
  calls: Call[];
  fetchMock: ReturnType<typeof vi.fn>;
}

/** Minimal stateful stand-in for the FastAPI notification + automation endpoints. */
function installFakeServer(): FakeServer {
  const channels: NotificationChannelRead[] = [channel()];
  const rules: AutomationRuleRead[] = [
    rule(RULE_ID, "low_doc_replenishment", "P1", { doc_days: 15, safety_stock_qty: 50 }),
    rule("rrrrrrrr-2222-4222-8222-rrrrrrrrrrrr", "negative_margin_hold", "P0", {
      min_margin_pct: "0.00",
    }),
    rule("rrrrrrrr-3333-4333-8333-rrrrrrrrrrrr", "stalled_shipment_soothe", "P1", {
      stalled_hours: 72,
    }),
    rule("rrrrrrrr-4444-4444-8444-rrrrrrrrrrrr", "cold_b2b_quote_followup", "P1", {
      cold_hours: 72,
    }),
    rule("rrrrrrrr-5555-4555-8555-rrrrrrrrrrrr", "daily_patrol_brief", "P2", {
      brief_hour_utc: 1,
    }),
  ];
  const deliveries: NotificationDeliveryRead[] = [delivery()];
  const calls: Call[] = [];

  const matched: Record<string, number> = {
    low_doc_replenishment: 3,
    negative_margin_hold: 1,
    stalled_shipment_soothe: 2,
    cold_b2b_quote_followup: 2,
    daily_patrol_brief: 4,
  };

  function report(dryRun: boolean) {
    const items = rules.map((row) => ({
      template_code: row.template_code,
      name: row.name,
      is_enabled: row.is_enabled,
      priority: row.priority,
      autonomy_level: row.autonomy_level,
      matched_count: row.is_enabled ? matched[row.template_code] : 0,
      proposals_created: dryRun ? 0 : Math.min(matched[row.template_code], 3),
      notifications_queued: dryRun ? 0 : 1,
      items_preview: [],
      note: dryRun ? "dry_run" : null,
    }));
    return {
      generated_at: NOW,
      dry_run: dryRun,
      seeded_defaults: false,
      total_matched: items.reduce((sum, item) => sum + item.matched_count, 0),
      total_proposals_created: items.reduce((sum, item) => sum + item.proposals_created, 0),
      total_notifications_queued: items.reduce(
        (sum, item) => sum + item.notifications_queued,
        0,
      ),
      rules: items,
    };
  }

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const { url, method } = normalizeFetchInput(input, init);
    const verb = (method ?? "GET").toUpperCase();
    const body = init?.body;
    calls.push({
      url,
      method: verb,
      body: typeof body === "string" ? body : "",
    });

    if (url.endsWith("/ai/notifications/channels") && verb === "GET") {
      return jsonResponse(channels);
    }
    if (url.endsWith("/ai/notifications/channels") && verb === "POST") {
      const parsed = JSON.parse(String(body)) as NotificationChannelRead & {
        secret?: string | null;
      };
      const created = channel({
        id: "created-channel-1",
        name: parsed.name,
        provider: parsed.provider,
        min_priority: parsed.min_priority,
        has_signing_secret: Boolean(parsed.secret),
        webhook_url_preview: "https://open.feishu.cn/***new1",
      });
      channels.unshift(created);
      return jsonResponse(created, 201);
    }
    if (/\/ai\/notifications\/channels\/[^/]+\/test$/.test(url)) {
      return jsonResponse({
        channel_id: CHANNEL_ID,
        ok: true,
        http_status: 200,
        error_message: null,
        tested_at: NOW,
      });
    }
    if (url.includes("/ai/notifications/channels/") && verb === "PATCH") {
      const id = url.split("/").pop() as string;
      const patch = JSON.parse(String(body)) as Partial<NotificationChannelRead>;
      const index = channels.findIndex((row) => row.id === id);
      channels[index] = { ...channels[index], ...patch } as NotificationChannelRead;
      return jsonResponse(channels[index]);
    }
    if (url.includes("/ai/notifications/channels/") && verb === "DELETE") {
      const id = url.split("/").pop() as string;
      const index = channels.findIndex((row) => row.id === id);
      if (index >= 0) channels.splice(index, 1);
      return new Response(null, { status: 204 });
    }
    if (url.includes("/ai/notifications/deliveries") && verb === "GET") {
      return jsonResponse({
        items: deliveries,
        total: deliveries.length,
        limit: 10,
        offset: 0,
        has_more: false,
      });
    }
    if (url.endsWith("/ai/automation-rules") && verb === "GET") {
      return jsonResponse(rules);
    }
    if (url.endsWith("/ai/automation-rules/seed-defaults")) {
      return jsonResponse({ created: 0, existing: rules.length, rules }, 201);
    }
    if (url.endsWith("/ai/automation-rules/evaluate")) {
      const parsed = JSON.parse(String(body ?? "{}")) as { dry_run?: boolean };
      return jsonResponse(report(parsed.dry_run === true));
    }
    if (url.includes("/ai/automation-rules/") && verb === "PATCH") {
      const id = url.split("/").pop() as string;
      const patch = JSON.parse(String(body)) as Partial<AutomationRuleRead>;
      const index = rules.findIndex((row) => row.id === id);
      rules[index] = {
        ...rules[index],
        ...patch,
        config_json: patch.config_json
          ? { ...rules[index].config_json, ...patch.config_json }
          : rules[index].config_json,
      } as AutomationRuleRead;
      return jsonResponse(rules[index]);
    }
    return jsonResponse({ detail: `unhandled ${verb} ${url}` }, 500);
  });

  vi.stubGlobal("fetch", fetchMock);
  return { channels, rules, deliveries, calls, fetchMock };
}

function renderPanel(canManage = true) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <I18nProvider defaultLocale="zh">
        <NotificationAutomationPanel canManage={canManage} />
      </I18nProvider>
    </QueryClientProvider>,
  );
}

let server: FakeServer;

beforeEach(() => {
  server = installFakeServer();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("NotificationAutomationPanel", () => {
  it("renders channels, the delivery log and all five automation rules", async () => {
    renderPanel();

    const channelList = await screen.findByTestId("channel-list");
    expect(within(channelList).getByText("运营飞书群")).toBeInTheDocument();
    expect(screen.getByText("https://open.feishu.cn/***3456")).toBeInTheDocument();
    const log = await screen.findByTestId("delivery-log");
    expect(within(log).getByText("P0 负毛利拦截")).toBeInTheDocument();
    expect(within(log).getByText("已送达")).toBeInTheDocument();
    expect(screen.getByText("P0 负毛利拦截")).toBeInTheDocument();

    for (const code of [
      "low_doc_replenishment",
      "negative_margin_hold",
      "stalled_shipment_soothe",
      "cold_b2b_quote_followup",
      "daily_patrol_brief",
    ]) {
      expect(screen.getByTestId(`rule-card-${code}`)).toBeInTheDocument();
    }
    expect(screen.getByTestId("rule-threshold-low_doc_replenishment")).toHaveValue("15");
    expect(screen.getByTestId("rule-threshold-negative_margin_hold")).toHaveValue("0.00");
  });

  it("creates a Feishu channel with a signing secret and a P0 minimum priority", async () => {
    const user = userEvent.setup();
    renderPanel();

    await screen.findByTestId("channel-list");
    await user.type(screen.getByTestId("channel-name-input"), "财务飞书群");
    await user.selectOptions(screen.getByTestId("channel-provider-select"), "feishu");
    await user.type(
      screen.getByTestId("channel-url-input"),
      "https://open.feishu.cn/open-apis/bot/v2/hook/newtoken",
    );
    await user.type(screen.getByTestId("channel-secret-input"), "sec-track1");
    await user.selectOptions(screen.getByTestId("channel-priority-select"), "P0");
    fireEvent.submit(screen.getByTestId("channel-create-form"));

    expect(await screen.findByTestId("channel-create-success")).toHaveTextContent("财务飞书群");
    const post = server.calls.find(
      (call) => call.method === "POST" && call.url.endsWith("/ai/notifications/channels"),
    );
    expect(post?.url).toBe("/api/bff/v1/ai/notifications/channels");
    expect(JSON.parse(post?.body ?? "{}")).toEqual({
      name: "财务飞书群",
      provider: "feishu",
      webhook_url: "https://open.feishu.cn/open-apis/bot/v2/hook/newtoken",
      secret: "sec-track1",
      min_priority: "P0",
      is_enabled: true,
    });
    expect(
      within(await screen.findByTestId("channel-list")).getByText("财务飞书群"),
    ).toBeInTheDocument();
  });

  it("toggles an automation rule and updates its priority through PATCH", async () => {
    const user = userEvent.setup();
    renderPanel();

    const card = await screen.findByTestId("rule-card-low_doc_replenishment");
    await user.click(within(card).getByTestId("rule-toggle-low_doc_replenishment"));

    await waitFor(() => {
      const patch = server.calls.find(
        (call) => call.method === "PATCH" && call.url.includes(RULE_ID),
      );
      expect(patch).toBeDefined();
      expect(JSON.parse(patch?.body ?? "{}")).toEqual({ is_enabled: false });
    });
    await waitFor(() => {
      const refreshed = screen.getByTestId("rule-card-low_doc_replenishment");
      expect(within(refreshed).getByText("停用")).toBeInTheDocument();
    });

    await user.selectOptions(
      screen.getByTestId("rule-priority-low_doc_replenishment"),
      "P0",
    );
    await waitFor(() => {
      const patches = server.calls.filter(
        (call) => call.method === "PATCH" && call.url.includes(RULE_ID),
      );
      expect(patches.length).toBeGreaterThanOrEqual(2);
      expect(JSON.parse(patches[patches.length - 1].body)).toEqual({ priority: "P0" });
    });
  });

  it("saves an integer threshold edit as config_json", async () => {
    const user = userEvent.setup();
    renderPanel();

    const input = await screen.findByTestId("rule-threshold-stalled_shipment_soothe");
    fireEvent.change(input, { target: { value: "96" } });
    await user.click(screen.getByTestId("rule-save-threshold-stalled_shipment_soothe"));

    await waitFor(() => {
      const patch = server.calls.find(
        (call) =>
          call.method === "PATCH" &&
          call.url.includes("rrrrrrrr-3333-4333-8333-rrrrrrrrrrrr"),
      );
      expect(JSON.parse(patch?.body ?? "{}")).toEqual({ config_json: { stalled_hours: 96 } });
    });
  });

  it("runs a dry-run and shows live matched counts without creating anything", async () => {
    const user = userEvent.setup();
    renderPanel();

    await screen.findByTestId("rule-card-low_doc_replenishment");
    await user.click(screen.getByTestId("rule-dry-run"));

    const banner = await screen.findByTestId("automation-report");
    expect(banner).toHaveTextContent("Dry-Run 结果");
    expect(banner).toHaveTextContent("命中 12 项");
    expect(banner).toHaveTextContent("新建提案 0 条");
    expect(screen.getByTestId("rule-matched-low_doc_replenishment")).toHaveTextContent(
      "本次命中 3 · 提案 0 · 通知 0",
    );

    const evaluation = server.calls.find((call) =>
      call.url.endsWith("/ai/automation-rules/evaluate"),
    );
    expect(evaluation?.url).toBe("/api/bff/v1/ai/automation-rules/evaluate");
    expect(evaluation?.method).toBe("POST");
    expect(JSON.parse(evaluation?.body ?? "{}")).toEqual({ dry_run: true });
    // A dry run must not enqueue deliveries through the UI either.
    expect(
      server.calls.filter((call) => call.url.endsWith("/deliveries/retry-due")),
    ).toHaveLength(0);
  });

  it("executes the rules and reports created proposals and notifications", async () => {
    const user = userEvent.setup();
    renderPanel();

    await screen.findByTestId("rule-card-low_doc_replenishment");
    await user.click(screen.getByTestId("rule-execute"));

    const banner = await screen.findByTestId("automation-report");
    expect(banner).toHaveTextContent("执行结果");
    expect(banner).toHaveTextContent("新建提案 11 条");
    expect(banner).toHaveTextContent("排队通知 5 条");
    expect(screen.getByTestId("rule-matched-daily_patrol_brief")).toHaveTextContent(
      "本次命中 4 · 提案 3 · 通知 1",
    );

    const evaluation = server.calls.find((call) =>
      call.url.endsWith("/ai/automation-rules/evaluate"),
    );
    expect(JSON.parse(evaluation?.body ?? "{}")).toEqual({ dry_run: false });
  });

  it("sends a channel test ping and deletes a channel", async () => {
    const user = userEvent.setup();
    renderPanel();

    await screen.findByTestId("channel-list");
    await user.click(screen.getByTestId(`channel-test-${CHANNEL_ID}`));
    expect(await screen.findByTestId(`channel-test-result-${CHANNEL_ID}`)).toHaveTextContent(
      "测试成功",
    );
    expect(
      server.calls.some(
        (call) =>
          call.method === "POST" &&
          call.url === `/api/bff/v1/ai/notifications/channels/${CHANNEL_ID}/test`,
      ),
    ).toBe(true);

    await user.click(screen.getByTestId(`channel-delete-${CHANNEL_ID}`));
    await waitFor(() => {
      expect(
        server.calls.some(
          (call) =>
            call.method === "DELETE" &&
            call.url === `/api/bff/v1/ai/notifications/channels/${CHANNEL_ID}`,
        ),
      ).toBe(true);
    });
    expect(await screen.findByTestId("channel-empty")).toBeInTheDocument();
  });

  it("hides manage actions for read-only roles but keeps the log and rules visible", async () => {
    renderPanel(false);

    expect(await screen.findByTestId("channel-list")).toBeInTheDocument();
    expect(screen.getByTestId("channel-read-only")).toBeInTheDocument();
    expect(screen.queryByTestId("channel-create-form")).not.toBeInTheDocument();
    expect(screen.queryByTestId("rule-dry-run")).not.toBeInTheDocument();
    expect(screen.getByTestId("delivery-log")).toBeInTheDocument();
    expect(screen.getByTestId("rule-card-negative_margin_hold")).toBeInTheDocument();
  });
});

describe("NotificationAutomationPanel mock mode", () => {
  it("renders the local fixtures without any network call", async () => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);

    renderPanel();

    expect(await screen.findByTestId("channel-list")).toBeInTheDocument();
    expect(screen.getByTestId("rule-card-low_doc_replenishment")).toBeInTheDocument();
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});
