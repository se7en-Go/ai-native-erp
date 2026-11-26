// Track 7 UI suite: 1-level light BOM + headhaul in-transit transfers + DOC
// replenishment panel on /wms.
//
// The client-module cases run the real `bffFetch` path so URL, method and body
// are verified against the locked contract; the panel cases render the real
// component against the stateful mock fixtures. Every money/cover figure is
// asserted as the backend's exact decimal string — never a recomputed float.
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import WmsPage from "@/app/wms/page";
import { BomTransferDocPanel } from "@/components/wms/bom-transfer-doc-panel";
import { AuthSessionContext, type AuthSessionState } from "@/components/providers";
import { I18nProvider } from "@/lib/i18n";
import type { Principal } from "@/lib/contracts";
import {
  addFixed,
  assembleBomKit,
  cancelStockTransfer,
  ceilFixed,
  componentUnitsFor,
  createStockTransfer,
  deleteBomComponent,
  dispatchStockTransfer,
  docRiskCounts,
  fetchBomCostRollup,
  fetchBomKits,
  fetchDocReplenishment,
  fetchStockTransfers,
  formatDecimal,
  formatDocDays,
  mergeWarehouseLabels,
  multiplyFixed,
  receiveStockTransfer,
  resetTrack7Mocks,
  riskLevelLabel,
  riskTone,
  shippingMethodLabel,
  sortDocRows,
  transferActions,
  transferStatusLabel,
  transferTotalQty,
  upsertBomComponent,
  wasteMultiplier,
  type DocReplenishmentRowRead,
  type StockTransferRead,
} from "@/lib/api-client/track7-bom-doc";
import { fetchCallBody, normalizeFetchInput } from "@/__tests__/fetch-stub";

vi.mock("@/hooks/use-realtime-inbox", () => ({ useRealtimeInbox: vi.fn() }));

// ---------------------------------------------------------------------------
// Rendering helpers
// ---------------------------------------------------------------------------

function principal(role: Principal["role"]): Principal {
  return {
    user_id: "11111111-1111-1111-1111-111111111111",
    tenant_id: "22222222-2222-2222-2222-222222222222",
    role,
    email: `${role}@qzled.example.com`,
    tenant_slug: "qz-led-factory",
    onboarding_stage: "LiveProduction",
  };
}

function renderWithSession(ui: React.ReactNode, role: Principal["role"] = "owner") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const session: AuthSessionState = {
    principal: principal(role),
    isLoading: false,
    logout: vi.fn(async () => {}),
    refreshSession: vi.fn(async () => {}),
  };
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthSessionContext.Provider value={session}>
        <I18nProvider defaultLocale="zh">{ui}</I18nProvider>
      </AuthSessionContext.Provider>
    </QueryClientProvider>,
  );
}

// ---------------------------------------------------------------------------
// Exact decimal helpers
// ---------------------------------------------------------------------------

describe("Track 7 exact fixed-point helpers", () => {
  it("multiplies and sums decimal strings without touching floats", () => {
    expect(multiplyFixed("12.3456", "2.7500")).toBe("33.9504");
    expect(multiplyFixed("3.0000", "1.0000")).toBe("3.0000");
    expect(addFixed(["33.9504", "3.0000"])).toBe("36.9504");
    expect(wasteMultiplier("10.00")).toBe("1.1000");
    expect(wasteMultiplier("0.00")).toBe("1.0000");
    expect(wasteMultiplier("2.50")).toBe("1.0250");
  });

  it("rounds waste-adjusted component demand up to whole units", () => {
    expect(ceilFixed("4.5000")).toBe(5);
    expect(ceilFixed("4.0000")).toBe(4);
    // 1 unit/kit at 50% waste over 3 kits = 4.5 -> 5 units, never a silent short.
    expect(componentUnitsFor("1.0000", "50.00", 3)).toBe(5);
    // 2 units/kit at 0% waste over 3 kits = 6 units.
    expect(componentUnitsFor("2.0000", "0.00", 3)).toBe(6);
    expect(componentUnitsFor("2.5000", "10.00", 2)).toBe(6); // 5.5 -> 6
  });

  it("formats server decimals verbatim and renders open-ended cover", () => {
    expect(formatDecimal("85.0300")).toBe("85.0300");
    expect(formatDecimal(undefined)).toBe("0.0000");
    expect(formatDocDays("3.50")).toBe("3.50 d");
    expect(formatDocDays(null)).toBe("∞");
    expect(shippingMethodLabel("air_freight")).toBe("Air freight");
    expect(transferStatusLabel("in_transit")).toBe("In transit");
    expect(riskLevelLabel("warning")).toBe("Warning");
    expect(riskTone("critical")).toContain("rose");
    expect(riskTone("healthy")).toContain("emerald");
  });

  it("only exposes the transitions the backend state machine allows", () => {
    expect(transferActions("draft")).toEqual({
      canDispatch: true,
      canReceive: false,
      canCancel: true,
    });
    expect(transferActions("in_transit")).toEqual({
      canDispatch: false,
      canReceive: true,
      canCancel: false,
    });
    expect(transferActions("received")).toEqual({
      canDispatch: false,
      canReceive: false,
      canCancel: false,
    });
    expect(transferActions("cancelled")).toEqual({
      canDispatch: false,
      canReceive: false,
      canCancel: false,
    });
  });

  it("labels warehouses from the transfer list, else from the short id", () => {
    const options = mergeWarehouseLabels(["wh-cn", "wh-us", "wh-unknown"], [
      {
        id: "t1",
        tenant_id: "t",
        transfer_no: "TRF-1",
        source_warehouse_id: "wh-cn",
        source_warehouse_name: "SZ Factory",
        destination_warehouse_id: "wh-us",
        destination_warehouse_name: "US LAX FBA",
        status: "draft",
        shipping_method: "sea_freight",
        tracking_no: null,
        eta_days: 25,
        dispatched_at: null,
        received_at: null,
        notes: null,
        created_by: null,
        created_at: "2026-11-20T00:00:00Z",
        updated_at: "2026-11-20T00:00:00Z",
        total_qty: 10,
        total_freight_cost_base: "12.5000",
        lines: [],
      },
    ] as StockTransferRead[]);
    expect(options[0]).toEqual({ id: "wh-cn", label: "SZ Factory" });
    expect(options[1]).toEqual({ id: "wh-us", label: "US LAX FBA" });
    expect(options[2].label).toBe("Warehouse wh-unkno");
  });

  it("sorts DOC rows critical-first and counts the risk bands", () => {
    const rows: DocReplenishmentRowRead[] = [
      {
        variant_id: "v-healthy",
        sku: "A-HEALTHY",
        product_name: "A",
        local_available_qty: 100,
        overseas_available_qty: 0,
        in_transit_qty: 0,
        total_available_qty: 100,
        units_sold_7d: 70,
        units_sold_30d: 300,
        units_sold_90d: 900,
        weighted_daily_sales: "10.0000",
        doc_days: "10.00",
        suggested_reorder_qty: 200,
        risk_level: "healthy",
      },
      {
        variant_id: "v-critical",
        sku: "B-CRITICAL",
        product_name: "B",
        local_available_qty: 5,
        overseas_available_qty: 0,
        in_transit_qty: 0,
        total_available_qty: 5,
        units_sold_7d: 0,
        units_sold_30d: 0,
        units_sold_90d: 0,
        weighted_daily_sales: "0.0000",
        doc_days: null,
        suggested_reorder_qty: 0,
        risk_level: "healthy",
      },
      {
        variant_id: "v-warning",
        sku: "C-WARNING",
        product_name: "C",
        local_available_qty: 10,
        overseas_available_qty: 0,
        in_transit_qty: 0,
        total_available_qty: 10,
        units_sold_7d: 0,
        units_sold_30d: 0,
        units_sold_90d: 0,
        weighted_daily_sales: "1.0000",
        doc_days: "10.00",
        suggested_reorder_qty: 20,
        risk_level: "warning",
      },
    ];
    const sorted = sortDocRows(rows);
    expect(sorted[0].variant_id).toBe("v-warning");
    // No demand sorts last (open-ended cover), never first.
    expect(sorted[sorted.length - 1].variant_id).toBe("v-critical");
    expect(docRiskCounts(rows)).toEqual({
      critical: 0,
      warning: 1,
      healthy: 2,
    });
  });
});

// ---------------------------------------------------------------------------
// Client contract: real bffFetch against a stubbed transport
// ---------------------------------------------------------------------------

interface CapturedCall {
  url: string;
  method: string;
  body: string;
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("Track 7 API client contract", () => {
  const captured: CapturedCall[] = [];

  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "false");
    captured.length = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const { url, method } = normalizeFetchInput(input, init);
        const body = await fetchCallBody([input, init]);
        captured.push({ url, method: method ?? "GET", body });
        if (url.includes("/cost-rollup")) {
          return jsonResponse({
            parent_variant_id: "variant-kit",
            parent_sku: "KIT-LED-PANEL-600",
            base_currency: "CNY",
            component_count: 2,
            unit_cost_base: "36.9504",
            lines: [
              {
                component_id: "bom-1",
                component_variant_id: "variant-strip",
                component_sku: "LED-COB-24V-5M",
                qty_per_unit: "2.5000",
                waste_rate_pct: "10.00",
                effective_qty_per_unit: "2.7500",
                component_unit_cost_base: "12.3456",
                line_unit_cost_base: "33.9504",
              },
            ],
          });
        }
        if (url.includes("/doc-replenishment")) {
          return jsonResponse({
            target_doc_days: 45,
            generated_at: "2026-11-25T00:00:00Z",
            total: 1,
            critical_count: 1,
            warning_count: 0,
            items: [
              {
                variant_id: "variant-strip",
                sku: "LED-COB-24V-5M",
                product_name: "COB Strip",
                local_available_qty: 5,
                overseas_available_qty: 20,
                in_transit_qty: 5,
                total_available_qty: 30,
                units_sold_7d: 70,
                units_sold_30d: 300,
                units_sold_90d: 900,
                weighted_daily_sales: "10.0000",
                doc_days: "3.00",
                suggested_reorder_qty: 420,
                risk_level: "critical",
              },
            ],
          });
        }
        if (url.includes("/bom/components")) {
          return jsonResponse(
            {
              id: "bom-new",
              parent_variant_id: "variant-kit",
              parent_sku: "KIT-LED-PANEL-600",
              component_variant_id: "variant-strip",
              component_sku: "LED-COB-24V-5M",
              qty_per_unit: "2.5000",
              waste_rate_pct: "10.00",
              effective_qty_per_unit: "2.7500",
              component_unit_cost_base: "12.3456",
              line_unit_cost_base: "33.9504",
              notes: null,
              created_at: "2026-11-25T00:00:00Z",
              updated_at: "2026-11-25T00:00:00Z",
            },
            201,
          );
        }
        if (url.includes("/assemble")) {
          return jsonResponse(
            {
              request_id: "req-1",
              warehouse_id: "wh-cn",
              parent_variant_id: "variant-kit",
              parent_sku: "KIT-LED-PANEL-600",
              qty_kits: 3,
              parent_on_hand_qty: 3,
              consumed: [
                {
                  component_variant_id: "variant-strip",
                  component_sku: "LED-COB-24V-5M",
                  qty_consumed: 6,
                },
              ],
              replayed: false,
            },
            201,
          );
        }
        if (url.includes("/dispatch") || url.includes("/receive") || url.includes("/cancel")) {
          return jsonResponse({
            id: "transfer-1",
            tenant_id: "t1",
            transfer_no: "TRF-0001",
            source_warehouse_id: "wh-cn",
            source_warehouse_name: "SZ Factory",
            destination_warehouse_id: "wh-us",
            destination_warehouse_name: "US LAX FBA",
            status: url.includes("/receive")
              ? "received"
              : url.includes("/cancel")
                ? "cancelled"
                : "in_transit",
            shipping_method: "sea_freight",
            tracking_no: null,
            eta_days: 25,
            dispatched_at: null,
            received_at: null,
            notes: null,
            created_by: null,
            created_at: "2026-11-25T00:00:00Z",
            updated_at: "2026-11-25T00:00:00Z",
            total_qty: 10,
            total_freight_cost_base: "12.5000",
            lines: [],
          });
        }
        if (url.includes("/transfers")) {
          return jsonResponse(
            {
              items: [
                {
                  id: "transfer-1",
                  tenant_id: "t1",
                  transfer_no: "TRF-0001",
                  source_warehouse_id: "wh-cn",
                  source_warehouse_name: "SZ Factory",
                  destination_warehouse_id: "wh-us",
                  destination_warehouse_name: "US LAX FBA",
                  status: "draft",
                  shipping_method: "sea_freight",
                  tracking_no: null,
                  eta_days: 25,
                  dispatched_at: null,
                  received_at: null,
                  notes: null,
                  created_by: null,
                  created_at: "2026-11-25T00:00:00Z",
                  updated_at: "2026-11-25T00:00:00Z",
                  total_qty: 10,
                  total_freight_cost_base: "12.5000",
                  lines: [],
                },
              ],
              total: 1,
            },
            201,
          );
        }
        if (url.includes("/bom")) {
          return jsonResponse({
            items: [
              {
                parent_variant_id: "variant-kit",
                parent_sku: "KIT-LED-PANEL-600",
                components: [],
                component_count: 0,
                rolled_up_unit_cost: "0.0000",
              },
            ],
            total: 1,
          });
        }
        return jsonResponse({ items: [], total: 0 });
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("reads kits, rollups, transfers and DOC from the locked supply-chain paths", async () => {
    const kits = await fetchBomKits("variant-kit");
    expect(kits.total).toBe(1);
    expect(captured[0].url).toContain("/api/bff/v1/supply-chain/bom?parent_variant_id=variant-kit");

    const rollup = await fetchBomCostRollup("variant-kit");
    expect(rollup.unit_cost_base).toBe("36.9504");
    expect(captured[1].url).toContain("/api/bff/v1/supply-chain/bom/variant-kit/cost-rollup");

    const transfers = await fetchStockTransfers();
    expect(transfers.items[0].transfer_no).toBe("TRF-0001");
    expect(transferTotalQty(transfers.items[0])).toBe(10);

    const doc = await fetchDocReplenishment(45);
    expect(captured[3].url).toContain(
      "/api/bff/v1/supply-chain/doc-replenishment?target_doc_days=45",
    );
    expect(doc.target_doc_days).toBe(45);
    expect(doc.items[0].weighted_daily_sales).toBe("10.0000");
    expect(doc.items[0].doc_days).toBe("3.00");
    expect(doc.critical_count).toBe(1);
  });

  it("writes BOM components, assemblies and transfer transitions with the right verbs", async () => {
    await upsertBomComponent({
      parent_variant_id: "variant-kit",
      component_variant_id: "variant-strip",
      qty_per_unit: "2.5000",
      waste_rate_pct: "10.00",
    });
    expect(captured[0].url).toContain("/api/bff/v1/supply-chain/bom/components");
    expect(captured[0].method).toBe("POST");
    expect(JSON.parse(captured[0].body)).toMatchObject({
      parent_variant_id: "variant-kit",
      component_variant_id: "variant-strip",
      qty_per_unit: "2.5000",
      waste_rate_pct: "10.00",
    });

    await deleteBomComponent("bom-new");
    expect(captured[1].method).toBe("DELETE");
    expect(captured[1].url).toContain("/api/bff/v1/supply-chain/bom/components/bom-new");

    const assembled = await assembleBomKit("variant-kit", {
      warehouse_id: "wh-cn",
      qty_kits: 3,
      request_id: "req-1",
    });
    expect(captured[2].method).toBe("POST");
    expect(captured[2].url).toContain("/api/bff/v1/supply-chain/bom/variant-kit/assemble");
    expect(JSON.parse(captured[2].body)).toMatchObject({
      qty_kits: 3,
      request_id: "req-1",
    });
    expect(assembled.consumed[0].qty_consumed).toBe(6);

    await createStockTransfer({
      source_warehouse_id: "wh-cn",
      destination_warehouse_id: "wh-us",
      shipping_method: "air_freight",
      lines: [{ variant_id: "variant-strip", qty: 10 }],
    });
    expect(captured[3].method).toBe("POST");
    expect(captured[3].url).toContain("/api/bff/v1/supply-chain/transfers");
    expect(JSON.parse(captured[3].body)).toMatchObject({
      shipping_method: "air_freight",
    });

    await dispatchStockTransfer("transfer-1");
    await receiveStockTransfer("transfer-1");
    await cancelStockTransfer("transfer-1");
    expect(captured.slice(4).map((call) => call.url.split("/api/bff")[1])).toEqual([
      "/v1/supply-chain/transfers/transfer-1/dispatch",
      "/v1/supply-chain/transfers/transfer-1/receive",
      "/v1/supply-chain/transfers/transfer-1/cancel",
    ]);
    expect(captured.slice(4).every((call) => call.method === "POST")).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Panel behaviour (mock fixtures)
// ---------------------------------------------------------------------------

describe("BomTransferDocPanel on /wms", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
    resetTrack7Mocks();
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  });

  it("renders the kit formula with the server rolled-up cost", async () => {
    renderWithSession(<BomTransferDocPanel />);

    expect(await screen.findByTestId("bom-components-table")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByTestId("bom-rolled-up-cost")).toHaveTextContent("85.0300"),
    );
    const table = screen.getByTestId("bom-components-table");
    expect(within(table).getByText("LED-PANEL-600-40W")).toBeInTheDocument();
    expect(within(table).getByText("LED-DRIVER-40W")).toBeInTheDocument();
    expect(within(table).getByText("1.0200")).toBeInTheDocument();
    expect(screen.queryByTestId("track7-readonly-hint")).toBeNull();
  });

  it("adds and removes a component, keeping the rolled-up cost exact", async () => {
    renderWithSession(<BomTransferDocPanel />);
    await screen.findByTestId("bom-components-table");
    await waitFor(() =>
      expect(screen.getByTestId("bom-rolled-up-cost")).toHaveTextContent("85.0300"),
    );

    fireEvent.change(screen.getByTestId("bom-component-select"), {
      target: { value: "variant-cob-5m" },
    });
    fireEvent.change(screen.getByTestId("bom-qty-per-unit"), {
      target: { value: "1" },
    });
    fireEvent.change(screen.getByTestId("bom-waste-rate"), {
      target: { value: "0" },
    });
    fireEvent.click(screen.getByTestId("bom-add-component"));

    // 85.0300 + 31.2000 exactly, no float drift.
    await waitFor(() =>
      expect(screen.getByTestId("bom-rolled-up-cost")).toHaveTextContent("116.2300"),
    );
    const table = screen.getByTestId("bom-components-table");
    expect(within(table).getByText("LED-COB-24V-5M")).toBeInTheDocument();

    const rows = within(table).getAllByRole("row");
    const cobRow = rows.find((row) => row.textContent?.includes("LED-COB-24V-5M"));
    expect(cobRow).toBeTruthy();
    fireEvent.click(
      within(cobRow as HTMLElement).getByRole("button", {
        name: /移除|Remove/i,
      }),
    );
    await waitFor(() =>
      expect(screen.getByTestId("bom-rolled-up-cost")).toHaveTextContent("85.0300"),
    );
  });

  it("assembles a kit and reports the consumed component lines", async () => {
    renderWithSession(<BomTransferDocPanel />);
    await screen.findByTestId("bom-components-table");

    // The assembly button waits for the warehouse picker to resolve.
    await waitFor(() => expect(screen.getByTestId("bom-assemble-submit")).not.toBeDisabled());
    fireEvent.change(screen.getByTestId("bom-assemble-qty"), {
      target: { value: "4" },
    });
    fireEvent.click(screen.getByTestId("bom-assemble-submit"));

    const note = await screen.findByTestId("track7-note");
    expect(note).toHaveTextContent("4");
    expect(note).toHaveTextContent("3");
  });

  it("creates a draft transfer, dispatches it, then receives it", async () => {
    renderWithSession(<BomTransferDocPanel />);
    fireEvent.click(await screen.findByTestId("track7-tab-transfers"));
    await screen.findByTestId("transfer-table");

    fireEvent.change(screen.getByTestId("transfer-qty"), {
      target: { value: "12" },
    });
    fireEvent.click(screen.getByTestId("transfer-create"));
    const created = await screen.findByTestId("track7-note");
    expect(created).toHaveTextContent("TRF-MOCK-0003");

    const newRow = await screen.findByTestId("transfer-row-transfer-mock-3");
    expect(within(newRow).getByTestId("transfer-status-transfer-mock-3")).toHaveTextContent("草稿");

    fireEvent.click(within(newRow).getByTestId("transfer-dispatch-transfer-mock-3"));
    await waitFor(() =>
      expect(screen.getByTestId("transfer-status-transfer-mock-3")).toHaveTextContent("在途"),
    );

    fireEvent.click(screen.getByTestId("transfer-receive-transfer-mock-3"));
    await waitFor(() =>
      expect(screen.getByTestId("transfer-status-transfer-mock-3")).toHaveTextContent("已签收"),
    );
    expect(screen.queryByTestId("transfer-dispatch-transfer-mock-3")).toBeNull();
    expect(screen.queryByTestId("transfer-cancel-transfer-mock-3")).toBeNull();
  });

  it("cancels a draft transfer but never offers receiving for it", async () => {
    renderWithSession(<BomTransferDocPanel />);
    fireEvent.click(await screen.findByTestId("track7-tab-transfers"));
    await screen.findByTestId("transfer-table");

    const draft = screen.getByTestId("transfer-row-transfer-mock-draft");
    expect(within(draft).queryByTestId("transfer-receive-transfer-mock-draft")).toBeNull();
    fireEvent.click(within(draft).getByTestId("transfer-cancel-transfer-mock-draft"));
    await waitFor(() =>
      expect(screen.getByTestId("transfer-status-transfer-mock-draft")).toHaveTextContent("已取消"),
    );
  });

  it("shows DOC cover with local, in-transit and overseas stock plus risk badges", async () => {
    renderWithSession(<BomTransferDocPanel />);
    fireEvent.click(await screen.findByTestId("track7-tab-doc"));
    await screen.findByTestId("doc-table");

    // Critical SKU is listed first.
    const rows = within(screen.getByTestId("doc-table")).getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("LED-COB-24V-5M");

    const critical = screen.getByTestId("doc-row-variant-cob-5m");
    expect(within(critical).getByTestId("doc-days-variant-cob-5m")).toHaveTextContent("1.78 d");
    expect(within(critical).getByTestId("doc-suggested-variant-cob-5m")).toHaveTextContent("254");
    expect(within(critical).getByTestId("doc-risk-variant-cob-5m")).toHaveTextContent("紧急");

    const panel = screen.getByTestId("doc-row-variant-panel-600");
    expect(within(panel).getByTestId("doc-weighted-variant-panel-600")).toHaveTextContent(
      "10.0000",
    );

    fireEvent.change(screen.getByTestId("doc-target-days"), {
      target: { value: "90" },
    });
    await waitFor(() =>
      expect(screen.getByTestId("doc-table")).toHaveTextContent("LED-COB-24V-5M"),
    );
  });

  it("keeps the panel read-only for agents", async () => {
    renderWithSession(<BomTransferDocPanel />, "agent");

    expect(await screen.findByTestId("track7-readonly-hint")).toHaveTextContent("agent");
    await screen.findByTestId("bom-components-table");
    expect(screen.queryByTestId("bom-add-component")).toBeNull();
    expect(screen.queryByTestId("bom-assemble-submit")).toBeNull();
    expect(screen.queryByTestId("bom-delete-component-bom-mock-1")).toBeNull();

    fireEvent.click(screen.getByTestId("track7-tab-transfers"));
    await screen.findByTestId("transfer-table");
    expect(screen.queryByTestId("transfer-create")).toBeNull();
    expect(screen.queryByTestId("transfer-dispatch-transfer-mock-draft")).toBeNull();
  });

  it("is mounted on the /wms page next to the inventory cards", async () => {
    renderWithSession(<WmsPage />, "manager");
    expect(await screen.findByTestId("bom-transfer-doc-panel")).toBeInTheDocument();
    expect(await screen.findByTestId("wms-low-stock-card")).toBeInTheDocument();
  });
});
