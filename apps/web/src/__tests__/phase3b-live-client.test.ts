// Phase 3B live adapter tests: the generated SDK must hit the exact BFF URLs
// from contracts/openapi.json, keep Decimal money as a string, and map the
// matrix onto the governance board without inventing tiles.
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

import type {
  AiEmployeeRead,
  CreativeAssetRead,
  WorkforceMatrixResponse,
} from "@/lib/api-client/types.gen";
import {
  destinationHref,
  fetchWorkforceMatrix,
  assistInbox,
  listEmployees,
  listQuickReplies,
  setEmployeeEnabled,
  createAsset,
  updateAsset,
  mapMetrics,
  mapDivisionItems,
  mapEmployee,
} from "@/lib/api-client/phase3b";

const BFF = "http://localhost:3000/api/bff";

interface CapturedCall {
  url: string;
  method: string | undefined;
  bodyText: string | null;
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function stubFetch(handler: (call: CapturedCall) => Response) {
  const captured: CapturedCall[] = [];
  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const call: CapturedCall =
      input instanceof Request
        ? { url: input.url, method: input.method, bodyText: await input.text() }
        : {
            url: String(input),
            method: init?.method,
            bodyText: typeof init?.body === "string" ? init.body : null,
          };
    captured.push(call);
    return handler(call);
  });
  vi.stubGlobal("fetch", mock);
  return captured;
}

const page = (items: unknown[]) => ({
  items,
  total: items.length,
  limit: 100,
  offset: 0,
  has_more: false,
});

const ASSET: CreativeAssetRead = {
  id: "asset-1",
  tenant_id: "t-1",
  asset_type: "product_copy",
  title: "German copy",
  language: "de",
  sku_ref: "LED-COB-24V-5M",
  prompt: "German copy",
  content_text: "Flexibles COB-LED-Band.",
  status: "draft",
  created_by: "u-1",
  created_at: "2026-11-19T09:00:00Z",
  updated_at: "2026-11-19T09:00:00Z",
  numeric_guard_passed: false,
  tool_calls: [{ tool_name: "get_live_sku_pricing_and_stock", ok: true }],
};

const EMPLOYEE: AiEmployeeRead = {
  id: "emp-1",
  tenant_id: "t-1",
  code: "b2b-sales",
  display_name: "小光",
  role_title: "B2B 外贸业务员",
  department: "商务中心",
  avatar_emoji: "💡",
  description: "整理 RFQ。",
  capability_codes: ["sales_quoting"],
  autonomy_level: "L1_proposal",
  is_enabled: true,
  tool_count: 5,
  system_persona: null,
  created_at: "2026-11-19T09:00:00Z",
  updated_at: "2026-11-19T09:00:00Z",
  actions_7d: 3,
  tasks_7d: 1,
};

const MATRIX: WorkforceMatrixResponse = {
  kpis: {
    total_employees: 4,
    active_employees: 3,
    actions_7d: 42,
    today_llm_cost_usd: "0.184500",
    autonomy_summary: { l2_auto: 1, l1_proposal: 2, l0_copilot: 1 },
  },
  l2_auto: [
    { capability: "order_logistics", display_name: "Order logistics", auto_executed_7d: 7,
      href: "/ai/proposals" },
  ],
  l1_pending: [
    { proposal_type: "quote_draft", label: "报价草稿", count: 2, href: "/ai/proposals" },
  ],
  l0_locked: [
    { code: "finance_receipt_settlement", title: "财务收付与核销", description: "人工执行。",
      href: "/ai/capabilities" },
  ],
};

describe("Phase 3B live SDK adapters", () => {
  beforeEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("lists digital employees through the tenant-scoped BFF route", async () => {
    const captured = stubFetch(() => jsonResponse(page([EMPLOYEE])));
    const employees = await listEmployees();

    expect(captured[0].url).toBe(`${BFF}/api/v1/ai/employees?limit=100`);
    expect(captured[0].method?.toLowerCase()).toBe("get");
    expect(employees).toHaveLength(1);
    expect(employees[0]).toMatchObject({
      id: "emp-1",
      display_name: "小光",
      autonomy_level: "L1_proposal",
      actions_7d: 3,
      tasks_7d: 1,
    });
  });

  it("toggles an employee with a PATCH body and no extra fields", async () => {
    const captured = stubFetch(() => jsonResponse({ ...EMPLOYEE, is_enabled: false }));
    const updated = await setEmployeeEnabled("emp-1", false);

    expect(captured[0].url).toBe(`${BFF}/api/v1/ai/employees/emp-1`);
    expect(captured[0].method?.toLowerCase()).toBe("patch");
    expect(JSON.parse(captured[0].bodyText ?? "{}")).toEqual({ is_enabled: false });
    expect(updated.is_enabled).toBe(false);
  });

  it("keeps today's LLM cost as a Decimal string and derives the autonomy badge", async () => {
    const captured = stubFetch(() => jsonResponse(MATRIX));
    const matrix = await fetchWorkforceMatrix();

    expect(captured[0].url).toBe(`${BFF}/api/v1/ai/employees/matrix`);
    const metrics = mapMetrics(matrix.kpis);
    expect(metrics.llmCostTodayUsd).toBe("0.184500");
    expect(metrics.actionsLast7Days).toBe(42);
    // Two L1 employees out-vote one L2 and one L0.
    expect(metrics.autonomyMode).toBe("L1_proposal");
    expect(mapMetrics({ ...matrix.kpis, autonomy_summary: { l2_auto: 1, l1_proposal: 1, l0_copilot: 0 } })
      .autonomyMode).toBe("L1_proposal");
    // An empty roster reports nothing automated, never an aspirational L2 badge.
    expect(mapMetrics({ ...matrix.kpis, autonomy_summary: { l2_auto: 0, l1_proposal: 0, l0_copilot: 0 } })
      .autonomyMode).toBe("L0_copilot");
  });

  it("maps the matrix onto exactly three governance columns with real destinations", () => {
    const items = mapDivisionItems(MATRIX);
    expect(items.map((item) => item.level)).toEqual(["L2_auto", "L1_proposal", "L0_copilot"]);
    expect(items[0].count).toBe(7);
    expect(items[1].count).toBe(2);
    // L0 red lines carry no count: they are policy, not work items.
    expect(items[2].count).toBeUndefined();
    expect(items[1].target && destinationHref(items[1].target)).toBe("/ai/proposals");
    expect(items[2].target && destinationHref(items[2].target)).toBe("/ai/capabilities");
  });

  it("posts the inbox assist action and returns the grounded-free draft", async () => {
    const captured = stubFetch(() =>
      jsonResponse({
        action: "polish",
        text: "Rewritten politely.",
        model_used: "gemini-3.1-flash-lite",
        credits_consumed: "0.2500",
        numeric_guard_passed: false,
        requires_human_review: true,
      })
    );
    const result = await assistInbox({ action: "polish", text: "改礼貌一点", language: "zh" });

    expect(captured[0].url).toBe(`${BFF}/api/v1/ai/inbox/assist`);
    expect(captured[0].method?.toLowerCase()).toBe("post");
    expect(JSON.parse(captured[0].bodyText ?? "{}")).toMatchObject({
      action: "polish",
      text: "改礼貌一点",
      language: "zh",
    });
    expect(result.requires_human_review).toBe(true);
    expect(result.text).toBe("Rewritten politely.");
  });

  it("surfaces ERR_AI_PROVIDER_UNAVAILABLE instead of fabricating a draft", async () => {
    stubFetch(() =>
      jsonResponse(
        {
          type: "https://erp.local/errors/ai-provider-unavailable",
          title: "AI Provider Unavailable",
          status: 503,
          detail: "No LLM provider key is configured for this workspace.",
          instance: "/api/v1/ai/inbox/assist",
          code: "ERR_AI_PROVIDER_UNAVAILABLE",
        },
        503
      )
    );
    await expect(assistInbox({ action: "summarize", text: "thread" })).rejects.toMatchObject({
      code: "ERR_AI_PROVIDER_UNAVAILABLE",
    });
  });

  it("reads quick replies and saves/updates studio assets", async () => {
    const captured = stubFetch((call) => {
      if (call.method?.toLowerCase() === "patch") {
        return jsonResponse({ ...ASSET, status: "approved" });
      }
      if (call.method?.toLowerCase() === "post") {
        return jsonResponse(ASSET, 201);
      }
      return jsonResponse(
        page([
          {
            id: "qr-1",
            tenant_id: "t-1",
            title: "MOQ 回复",
            category: "报价",
            language: "zh",
            body_text: "起订量 100 条起。",
            shortcut_code: "moq-zh",
            created_at: "2026-11-19T09:00:00Z",
          },
        ])
      );
    });

    const replies = await listQuickReplies();
    expect(captured[0].url).toBe(`${BFF}/api/v1/ai/quick-replies?limit=100`);
    expect(replies[0].shortcut_code).toBe("moq-zh");

    const saved = await createAsset({
      asset_type: "product_copy",
      language: "de",
      sku_ref: "LED-COB-24V-5M",
      prompt: "German copy",
      generate: true,
    });
    const createCall = captured[captured.length - 1];
    expect(createCall.url).toBe(`${BFF}/api/v1/ai/studio/assets`);
    expect(createCall.method?.toLowerCase()).toBe("post");
    expect(JSON.parse(createCall.bodyText ?? "{}")).toMatchObject({
      asset_type: "product_copy",
      generate: true,
      sku_ref: "LED-COB-24V-5M",
    });
    expect(saved.numeric_guard_passed).toBe(false);

    const approved = await updateAsset("asset-1", "approved");
    const patchCall = captured[captured.length - 1];
    expect(patchCall.url).toBe(`${BFF}/api/v1/ai/studio/assets/asset-1`);
    expect(JSON.parse(patchCall.bodyText ?? "{}")).toEqual({ status: "approved" });
    expect(approved.status).toBe("approved");
  });

  it("never coerces a missing Decimal cost into a number", () => {
    const metrics = mapMetrics({
      total_employees: 0,
      active_employees: 0,
      actions_7d: 0,
      today_llm_cost_usd: "0.000000",
      autonomy_summary: { l2_auto: 0, l1_proposal: 0, l0_copilot: 0 },
    });
    expect(metrics.llmCostTodayUsd).toBe("0.000000");
    expect(mapEmployee({ ...EMPLOYEE, actions_7d: undefined, tasks_7d: undefined })).toMatchObject({
      actions_7d: 0,
      tasks_7d: 0,
    });
  });
});
