// Phase 3A live adapter tests: every call must hit the BFF URL from
// contracts/openapi.json with the documented method, query and JSON body.
// Mock fallback only activates with NEXT_PUBLIC_USE_MOCK_API=true.
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

import {
  addPhase3aPurchaseOrder,
  addPhase3aStocktake,
  addPhase3aTask,
  fetchPhase3aDashboard,
  fetchPhase3aLeads,
  fetchPhase3aPurchaseOrders,
  fetchPhase3aSuppliers,
  patchPhase3aLead,
  receivePhase3aPurchaseOrder,
  searchPhase3a,
  fetchPhase3aOverview,
} from "@/lib/phase3a-data";

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

const page = (items: unknown[]) => ({ items, total: items.length, limit: 50, offset: 0, has_more: false });

describe("Phase 3A live SDK adapters", () => {
  beforeEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("lists leads through the tenant-scoped BFF route with the status filter", async () => {
    const captured = stubFetch(() => jsonResponse(page([{ id: "lead-1", status: "qualified" }])));
    const result = await fetchPhase3aLeads("qualified");

    expect(result.items).toHaveLength(1);
    expect(captured).toHaveLength(1);
    expect(captured[0].url).toBe(
      `${BFF}/api/v1/b2b/leads?limit=50&lead_status=qualified`
    );
    expect(captured[0].method?.toLowerCase()).toBe("get");
  });

  it("patches a lead and receives purchase orders via the generated SDK", async () => {
    const captured = stubFetch((call) => {
      if (call.url.includes("/leads/")) return jsonResponse({ id: "lead-1", status: "qualified" });
      if (call.url.includes("/purchase-orders/") && call.url.endsWith("/receive")) {
        return jsonResponse({ id: "po-1", status: "received", items: [] });
      }
      return jsonResponse(page([{ id: "po-1", po_no: "PO-1", items: [] }]));
    });

    const lead = await patchPhase3aLead("lead-1", { status: "qualified" });
    expect(lead.status).toBe("qualified");
    expect(captured[0].url).toBe(`${BFF}/api/v1/b2b/leads/lead-1`);
    expect(captured[0].method?.toLowerCase()).toBe("patch");
    expect(JSON.parse(captured[0].bodyText ?? "{}")).toEqual({ status: "qualified" });

    const orders = await fetchPhase3aPurchaseOrders();
    expect(orders.items[0].po_no).toBe("PO-1");

    const received = await receivePhase3aPurchaseOrder("po-1", "rcpt-123");
    expect(received.status).toBe("received");
    const receiveCall = captured[captured.length - 1];
    expect(receiveCall.url).toBe(`${BFF}/api/v1/supply-chain/purchase-orders/po-1/receive`);
    expect(receiveCall.method?.toLowerCase()).toBe("post");
    expect(JSON.parse(receiveCall.bodyText ?? "{}")).toEqual({
      receipt_id: "rcpt-123",
      items: [],
    });
  });

  it("creates purchase orders with string unit costs and numeric quantities", async () => {
    const captured = stubFetch(() =>
      jsonResponse({ id: "po-9", po_no: "PO-9", status: "ordered", items: [] })
    );
    await addPhase3aPurchaseOrder({
      supplier_id: "sup-1",
      warehouse_id: "wh-1",
      currency: "USD",
      status: "ordered",
      items: [{ variant_id: "var-1", qty_ordered: 12, unit_cost: "4.8500" }],
    });

    expect(captured[0].url).toBe(`${BFF}/api/v1/supply-chain/purchase-orders`);
    expect(captured[0].method?.toLowerCase()).toBe("post");
    const body = JSON.parse(captured[0].bodyText ?? "{}");
    expect(body.items).toEqual([{ variant_id: "var-1", qty_ordered: 12, unit_cost: "4.8500" }]);
    expect(body.currency).toBe("USD");
  });

  it("only submits stocktake adjustments, never inbound/outbound/transfer", async () => {
    const captured = stubFetch(() =>
      jsonResponse({ id: "mov-1", movement_type: "stocktake_adjust", qty_delta: -2 })
    );
    await addPhase3aStocktake({
      movement_type: "stocktake_adjust",
      request_id: "req-1",
      warehouse_id: "wh-1",
      variant_id: "var-1",
      qty_delta: -2,
      notes: "count variance",
    });

    expect(captured[0].url).toBe(`${BFF}/api/v1/wms/movements`);
    const body = JSON.parse(captured[0].bodyText ?? "{}");
    expect(body.movement_type).toBe("stocktake_adjust");
    expect(body.request_id).toBe("req-1");
  });

  it("serves analytics, suppliers, tasks and global search", async () => {
    const captured = stubFetch((call) => {
      if (call.url.includes("/analytics/dashboard")) {
        return jsonResponse({ open_inquiries: 1, pipeline_value: [], funnel: [], channels: [] });
      }
      if (call.url.includes("/analytics/overview")) {
        return jsonResponse({ channels: [], funnel: [], won_conversion_pct: "0.00" });
      }
      if (call.url.includes("/supply-chain/suppliers")) return jsonResponse(page([]));
      if (call.url.includes("/b2b/tasks")) return jsonResponse(page([]));
      if (call.url.includes("/search")) return jsonResponse({ items: [] });
      return jsonResponse(page([]));
    });

    expect((await fetchPhase3aDashboard()).open_inquiries).toBe(1);
    expect((await fetchPhase3aOverview()).won_conversion_pct).toBe("0.00");
    expect((await fetchPhase3aSuppliers()).items).toEqual([]);
    await addPhase3aTask({ title: "Follow up Hans", priority: "high" });
    await searchPhase3a("needle");

    const urls = captured.map((call) => call.url);
    expect(urls).toContain(`${BFF}/api/v1/analytics/dashboard`);
    expect(urls).toContain(`${BFF}/api/v1/supply-chain/suppliers?limit=50`);
    expect(urls).toContain(`${BFF}/api/v1/search?q=needle`);
    expect(captured.some((call) => call.url.startsWith(`${BFF}/api/v1/b2b/tasks`))).toBe(true);
  });

  it("falls back to fixtures when NEXT_PUBLIC_USE_MOCK_API=true", async () => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
    const captured = stubFetch(() => jsonResponse({}));
    const dashboard = await fetchPhase3aDashboard();
    const leads = await fetchPhase3aLeads();
    const search = await searchPhase3a("hans");

    expect(dashboard.open_inquiries).toBe(3);
    expect(leads.items.length).toBeGreaterThan(0);
    expect(search.items.some((hit) => hit.label.includes("Hans"))).toBe(true);
    expect(captured).toHaveLength(0);
  });
});
