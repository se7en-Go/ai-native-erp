// Pack A frontend: live BFF contract tests for the dedicated pack-a-erp
// module, plus RBAC + data-flow tests for the upgraded /catalog, /orders and
// /wms pages (which run on fixtures with NEXT_PUBLIC_USE_MOCK_API=true).
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import CatalogPage from "@/app/catalog/page";
import OrdersPage from "@/app/orders/page";
import WmsPage from "@/app/wms/page";
import { AuthSessionContext, type AuthSessionState } from "@/components/providers";
import { I18nProvider } from "@/lib/i18n";
import type { Principal } from "@/lib/contracts";
import type {
  CatalogCsvImportResponse,
  LowStockListRead,
  ProductRead,
  PurchaseOrderRead,
  VariantRead,
  WmsWarehouseRead,
} from "@/lib/api-client/types.gen";
import {
  CATALOG_CSV_SAMPLE,
  commercialInvoicePdfUrl,
  createCatalogProductAndVariant,
  createPoFromLowStock,
  createWmsWarehouse,
  fetchLowStockAlerts,
  importCatalogCsv,
  packingAttributesFromDraft,
  packingListPdfUrl,
  productPayloadFromDraft,
  variantPayloadFromDraft,
  weightGramsFromPacking,
  type CatalogSkuDraft,
} from "@/lib/api-client/pack-a-erp";

vi.mock("@/hooks/use-realtime-inbox", () => ({ useRealtimeInbox: vi.fn() }));

// `bffFetch` keeps the request same-origin, so the URL is the relative BFF path.
const BFF = "/api/bff";

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

const draft: CatalogSkuDraft = {
  spu_code: "LED-COB-24V",
  product_name: "COB LED Strip 24V 5m 4000K",
  category: "LED Strip Lighting",
  hs_code: "94054200",
  sku: "LED-COB-24V-5M-4000K",
  retail_price_usd: "29.90",
  cost_price_base: "45.50",
  lead_time_days: 7,
  carton_qty: 50,
  gross_weight_kg: "11.500",
  net_weight_kg: "10.000",
  cbm_per_carton: "0.032",
  safety_stock_qty: 120,
};

const PRODUCT: ProductRead = {
  id: "11111111-1111-1111-1111-111111111111",
  tenant_id: "22222222-2222-2222-2222-222222222222",
  spu_code: draft.spu_code,
  category: draft.category,
  hs_code: "94054200",
  origin_country: "CN",
  i18n_specs: { en: { title: draft.product_name } },
  certifications: [],
  ai_faq_facts: {},
  created_at: "2026-01-05T09:15:00.000Z",
  updated_at: "2026-01-05T09:15:00.000Z",
};

const VARIANT: VariantRead = {
  id: "33333333-3333-3333-3333-333333333333",
  tenant_id: PRODUCT.tenant_id,
  product_id: PRODUCT.id,
  sku: draft.sku,
  attributes: { carton_qty: 50, safety_stock_qty: 120 },
  base_currency: "CNY",
  min_margin_floor_pct: "15.00",
  retail_price_usd: "29.90",
  b2b_moq_tiers: [],
  weight_grams: 200,
  carton_dims_cm: null,
  lead_time_days: 7,
  cost_price_base: "45.50",
  created_at: "2026-01-05T09:15:00.000Z",
};

describe("Pack A payload builders", () => {
  it("keeps money as strings and puts the product name in i18n_specs.en.title", () => {
    const product = productPayloadFromDraft(draft);
    expect(product).toMatchObject({
      spu_code: "LED-COB-24V",
      category: "LED Strip Lighting",
      hs_code: "94054200",
      i18n_specs: { en: { title: "COB LED Strip 24V 5m 4000K" } },
    });

    const variant = variantPayloadFromDraft(draft);
    expect(variant.retail_price_usd).toBe("29.90");
    expect(variant.cost_price_base).toBe("45.50");
    expect(variant.attributes).toEqual({
      carton_qty: 50,
      gross_weight_kg: "11.500",
      net_weight_kg: "10.000",
      cbm_per_carton: "0.032",
      safety_stock_qty: 120,
    });
    expect(variant.lead_time_days).toBe(7);
    expect(variant).not.toHaveProperty("product_id");
  });

  it("degrades an empty optional cost price to a zero base cost", () => {
    const variant = variantPayloadFromDraft({ ...draft, cost_price_base: "  " });
    expect(variant.cost_price_base).toBe("0");
    expect(productPayloadFromDraft({ ...draft, hs_code: "  " })).not.toHaveProperty("hs_code");
  });

  it("derives the per-unit gram weight from the carton net weight", () => {
    expect(weightGramsFromPacking({ carton_qty: 50, net_weight_kg: "10.000" })).toBe(200);
    expect(weightGramsFromPacking({ carton_qty: 25, net_weight_kg: "12.5" })).toBe(500);
    expect(weightGramsFromPacking({ carton_qty: 0, net_weight_kg: "" })).toBe(0);
    expect(packingAttributesFromDraft({ ...draft, carton_qty: 0 })).toMatchObject({
      carton_qty: 1,
    });
  });
});

describe("Pack A export document links", () => {
  it("points at the same-origin BFF PDF routes with the language query", () => {
    expect(commercialInvoicePdfUrl("ord-1")).toBe(
      "/api/bff/v1/orders/ord-1/documents/commercial-invoice.pdf?language=en"
    );
    expect(commercialInvoicePdfUrl("ord-1", "zh")).toBe(
      "/api/bff/v1/orders/ord-1/documents/commercial-invoice.pdf?language=zh"
    );
    expect(packingListPdfUrl("ord-2")).toBe(
      "/api/bff/v1/orders/ord-2/documents/packing-list.pdf?language=en"
    );
    expect(packingListPdfUrl("ord-2", "zh")).toBe(
      "/api/bff/v1/orders/ord-2/documents/packing-list.pdf?language=zh"
    );
  });
});

describe("Pack A live BFF adapters", () => {
  beforeEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "false");
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("creates the product first and injects its id into the variant body", async () => {
    const captured = stubFetch((call) =>
      call.url.includes("/v1/catalog/products")
        ? jsonResponse(PRODUCT, 201)
        : jsonResponse(VARIANT, 201)
    );

    const created = await createCatalogProductAndVariant(
      productPayloadFromDraft(draft),
      variantPayloadFromDraft(draft)
    );

    expect(created.product.id).toBe(PRODUCT.id);
    expect(created.variant.id).toBe(VARIANT.id);
    expect(captured).toHaveLength(2);

    expect(captured[0].url).toBe(`${BFF}/v1/catalog/products`);
    expect(captured[0].method).toBe("POST");
    expect(JSON.parse(captured[0].bodyText ?? "{}")).toEqual(productPayloadFromDraft(draft));

    expect(captured[1].url).toBe(`${BFF}/v1/catalog/variants`);
    expect(captured[1].method).toBe("POST");
    expect(JSON.parse(captured[1].bodyText ?? "{}")).toMatchObject({
      product_id: PRODUCT.id,
      sku: draft.sku,
      retail_price_usd: "29.90",
      cost_price_base: "45.50",
      weight_grams: 200,
      lead_time_days: 7,
      attributes: { carton_qty: 50, safety_stock_qty: 120 },
    });
  });

  it("posts the raw CSV text to the bulk import route", async () => {
    const result: CatalogCsvImportResponse = {
      created_products: 2,
      created_variants: 3,
      updated_variants: 1,
      errors: ["Row 4: 'retail_price_usd' must be greater than zero"],
    };
    const captured = stubFetch(() => jsonResponse(result));

    const response = await importCatalogCsv(CATALOG_CSV_SAMPLE);

    expect(response).toEqual(result);
    expect(captured[0].url).toBe(`${BFF}/v1/catalog/variants/import-csv`);
    expect(captured[0].method).toBe("POST");
    expect(JSON.parse(captured[0].bodyText ?? "{}")).toEqual({ csv_text: CATALOG_CSV_SAMPLE });
  });

  it("reads the low-stock board and forwards a one-click replenishment payload", async () => {
    const lowStock: LowStockListRead = {
      items: [
        {
          variant_id: "var-1",
          sku: "LED-COB-24V-5M",
          product_name: "COB LED Strip 24V 5m 4000K",
          warehouse_id: "33333333-3333-3333-3333-333333333333",
          warehouse_name: "Quanzhou Main Warehouse",
          on_hand_qty: 40,
          reserved_qty: 12,
          available_qty: 28,
          safety_stock_qty: 120,
          suggested_reorder_qty: 212,
        },
      ],
      total: 1,
    };
    const purchaseOrder = {
      id: "po-1",
      po_no: "PO-2026-0001",
      supplier_id: "supplier-1",
      warehouse_id: "33333333-3333-3333-3333-333333333333",
      currency: "USD",
      total_amount: "3010.40",
      status: "draft",
      expected_arrival_date: null,
      notes: null,
      created_by: "11111111-1111-1111-1111-111111111111",
      created_at: "2026-01-05T09:15:00.000Z",
      updated_at: "2026-01-05T09:15:00.000Z",
      items: [
        {
          id: "poi-1",
          variant_id: "var-1",
          sku: "LED-COB-24V-5M",
          qty_ordered: 212,
          qty_received: 0,
          unit_cost: "14.20",
        },
      ],
    } satisfies PurchaseOrderRead;

    const captured = stubFetch((call) =>
      call.url.includes("/low-stock/create-po")
        ? jsonResponse(purchaseOrder, 201)
        : jsonResponse(lowStock)
    );

    const alerts = await fetchLowStockAlerts();
    expect(alerts.items[0].sku).toBe("LED-COB-24V-5M");
    expect(captured[0].url).toBe(`${BFF}/v1/wms/low-stock`);
    expect(captured[0].method).toBeUndefined();

    const po = await createPoFromLowStock({
      supplier_id: "supplier-1",
      variant_id: "var-1",
      quantity: 212,
      unit_cost: "14.20",
    });
    expect(po.po_no).toBe("PO-2026-0001");
    expect(captured[1].url).toBe(`${BFF}/v1/wms/low-stock/create-po`);
    expect(captured[1].method).toBe("POST");
    expect(JSON.parse(captured[1].bodyText ?? "{}")).toEqual({
      supplier_id: "supplier-1",
      variant_id: "var-1",
      quantity: 212,
      unit_cost: "14.20",
    });
  });

  it("creates a warehouse through the WMS route", async () => {
    const warehouse: WmsWarehouseRead = {
      id: "44444444-4444-4444-4444-444444444444",
      code: "SZ-BONDED-01",
      name: "Shenzhen Bonded Warehouse",
      country_code: "CN",
      city: "Shenzhen",
    };
    const captured = stubFetch(() => jsonResponse(warehouse, 201));

    const created = await createWmsWarehouse({
      code: "SZ-BONDED-01",
      name: "Shenzhen Bonded Warehouse",
      country_code: "CN",
      city: "Shenzhen",
    });

    expect(created).toEqual(warehouse);
    expect(captured[0].url).toBe(`${BFF}/v1/wms/warehouses`);
    expect(captured[0].method).toBe("POST");
    expect(JSON.parse(captured[0].bodyText ?? "{}")).toEqual({
      code: "SZ-BONDED-01",
      name: "Shenzhen Bonded Warehouse",
      country_code: "CN",
      city: "Shenzhen",
    });
  });
});

// ---------------------------------------------------------------------------
// Page-level specs (fixture-backed, i.e. NEXT_PUBLIC_USE_MOCK_API=true)
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

function renderPage(ui: React.ReactNode, role: Principal["role"]) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const authValue: AuthSessionState = {
    principal: principal(role),
    isLoading: false,
    logout: vi.fn(async () => {}),
    refreshSession: vi.fn(async () => {}),
  };
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthSessionContext.Provider value={authValue}>
        <I18nProvider defaultLocale="zh">{ui}</I18nProvider>
      </AuthSessionContext.Provider>
    </QueryClientProvider>
  );
}

describe("Pack A pages", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("hides catalog authoring from agents and exposes it to managers", async () => {
    const { unmount } = renderPage(<CatalogPage />, "agent");
    await waitFor(() => expect(screen.getByText("LED-COB-24V-5M")).toBeInTheDocument());
    expect(screen.queryByTestId("open-create-sku-btn")).toBeNull();
    expect(screen.queryByTestId("open-csv-import-btn")).toBeNull();
    unmount();

    renderPage(<CatalogPage />, "manager");
    await waitFor(() => expect(screen.getByTestId("open-create-sku-btn")).toBeInTheDocument());
    expect(screen.getByTestId("open-csv-import-btn")).toBeInTheDocument();
  });

  it("creates a product + SKU from the drawer and reports the created codes", async () => {
    renderPage(<CatalogPage />, "manager");
    await waitFor(() => expect(screen.getByTestId("open-create-sku-btn")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("open-create-sku-btn"));
    expect(screen.getByTestId("create-sku-modal")).toBeInTheDocument();

    fireEvent.change(screen.getByTestId("sku-form-spu-code"), {
      target: { value: "LED-COB-24V" },
    });
    fireEvent.change(screen.getByTestId("sku-form-product-name"), {
      target: { value: "COB LED Strip 24V 5m 4000K" },
    });
    fireEvent.change(screen.getByTestId("sku-form-category"), {
      target: { value: "LED Strip Lighting" },
    });
    fireEvent.change(screen.getByTestId("sku-form-hs-code"), {
      target: { value: "94054200" },
    });
    fireEvent.change(screen.getByTestId("sku-form-sku"), {
      target: { value: "LED-COB-24V-10M-6000K" },
    });
    fireEvent.change(screen.getByTestId("sku-form-retail-price"), {
      target: { value: "54.90" },
    });
    fireEvent.change(screen.getByTestId("sku-form-net-weight"), {
      target: { value: "12.000" },
    });
    fireEvent.change(screen.getByTestId("sku-form-gross-weight"), {
      target: { value: "13.800" },
    });
    fireEvent.change(screen.getByTestId("sku-form-carton-qty"), { target: { value: "25" } });
    fireEvent.change(screen.getByTestId("sku-form-safety-stock"), { target: { value: "60" } });
    fireEvent.click(screen.getByTestId("create-sku-submit"));

    const success = await screen.findByTestId("create-sku-success");
    expect(success).toHaveTextContent("LED-COB-24V");
    expect(success).toHaveTextContent("LED-COB-24V-10M-6000K");
    expect(screen.queryByTestId("create-sku-modal")).toBeNull();
  });

  it("rejects an invalid create-SKU form without leaving the modal", async () => {
    renderPage(<CatalogPage />, "manager");
    await waitFor(() => expect(screen.getByTestId("open-create-sku-btn")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("open-create-sku-btn"));
    fireEvent.change(screen.getByTestId("sku-form-spu-code"), { target: { value: "LED-X" } });
    fireEvent.click(screen.getByTestId("create-sku-submit"));

    expect(await screen.findByTestId("create-sku-error")).toHaveTextContent("为必填项");
    expect(screen.getByTestId("create-sku-modal")).toBeInTheDocument();
  });

  it("fills the sample CSV, imports it and shows a crisp summary", async () => {
    renderPage(<CatalogPage />, "manager");
    await waitFor(() => expect(screen.getByTestId("open-csv-import-btn")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("open-csv-import-btn"));
    expect(screen.getByTestId("csv-import-modal")).toBeInTheDocument();
    expect(screen.getByTestId("csv-import-submit")).toBeDisabled();

    fireEvent.click(screen.getByTestId("fill-csv-template"));
    expect((screen.getByTestId("csv-textarea") as HTMLTextAreaElement).value).toContain(
      "spu_code,product_name"
    );
    fireEvent.click(screen.getByTestId("csv-import-submit"));

    const summary = await screen.findByTestId("csv-import-summary");
    // The sample sheet has 2 SPUs and 3 SKUs.
    expect(summary).toHaveTextContent("新增商品 2");
    expect(summary).toHaveTextContent("新增 SKU 3");
    expect(summary).toHaveTextContent("更新 0");
    expect(screen.queryByTestId("csv-import-errors")).toBeNull();
  });

  it("surfaces per-row CSV errors instead of failing silently", async () => {
    renderPage(<CatalogPage />, "manager");
    await waitFor(() => expect(screen.getByTestId("open-csv-import-btn")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("open-csv-import-btn"));
    fireEvent.change(screen.getByTestId("csv-textarea"), {
      target: {
        value:
          "spu_code,product_name,sku,retail_price_usd\nLED-A,Panel A,LED-A-1,abc\nLED-B,Panel B,LED-B-1,19.90",
      },
    });
    fireEvent.click(screen.getByTestId("csv-import-submit"));

    const errors = await screen.findByTestId("csv-import-errors");
    expect(errors).toHaveTextContent("第 2 行零售价不是合法金额");
    expect(screen.getByTestId("csv-import-summary")).toHaveTextContent("新增 SKU 1");
  });

  it("renders CI/PL links per order and switches the PDF language", async () => {
    renderPage(<OrdersPage />, "agent");
    await waitFor(() => expect(screen.getByTestId("download-ci-ord-b2b-1")).toBeInTheDocument());

    const ci = screen.getByTestId("download-ci-ord-b2b-1");
    const pl = screen.getByTestId("download-pl-ord-b2b-1");
    expect(ci).toHaveAttribute(
      "href",
      "/api/bff/v1/orders/ord-b2b-1/documents/commercial-invoice.pdf?language=en"
    );
    expect(pl).toHaveAttribute(
      "href",
      "/api/bff/v1/orders/ord-b2b-1/documents/packing-list.pdf?language=en"
    );
    expect(ci).toHaveAttribute("target", "_blank");
    expect(ci).toHaveAttribute("rel", "noopener noreferrer");

    fireEvent.click(screen.getByTestId("pdf-lang-zh"));
    await waitFor(() =>
      expect(screen.getByTestId("download-ci-ord-b2b-1")).toHaveAttribute(
        "href",
        "/api/bff/v1/orders/ord-b2b-1/documents/commercial-invoice.pdf?language=zh"
      )
    );
    expect(screen.getByTestId("pdf-lang-zh")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("download-pl-ord-b2b-1")).toHaveAttribute(
      "href",
      "/api/bff/v1/orders/ord-b2b-1/documents/packing-list.pdf?language=zh"
    );
  });

  it("shows the low-stock board to every role but only lets operators restock", async () => {
    const { unmount } = renderPage(<WmsPage />, "agent");
    await waitFor(() => expect(screen.getByTestId("wms-low-stock-card")).toBeInTheDocument());
    await waitFor(() =>
      expect(screen.getByTestId("low-stock-badge-var-1")).toHaveTextContent("28 / 120")
    );
    expect(screen.queryByTestId("low-stock-create-po-var-1")).toBeNull();
    expect(screen.queryByTestId("open-create-warehouse-btn")).toBeNull();
    unmount();

    renderPage(<WmsPage />, "manager");
    await waitFor(() =>
      expect(screen.getByTestId("low-stock-create-po-var-1")).toBeInTheDocument()
    );
    expect(screen.getByTestId("open-create-warehouse-btn")).toBeInTheDocument();
  });

  it("creates a warehouse from the inline dialog", async () => {
    renderPage(<WmsPage />, "owner");
    await waitFor(() =>
      expect(screen.getByTestId("open-create-warehouse-btn")).toBeInTheDocument()
    );

    fireEvent.click(screen.getByTestId("open-create-warehouse-btn"));
    expect(screen.getByTestId("create-warehouse-modal")).toBeInTheDocument();

    fireEvent.change(screen.getByTestId("warehouse-form-code"), {
      target: { value: "SZ-BONDED-01" },
    });
    fireEvent.change(screen.getByTestId("warehouse-form-name"), {
      target: { value: "Shenzhen Bonded Warehouse" },
    });
    fireEvent.change(screen.getByTestId("warehouse-form-city"), { target: { value: "Shenzhen" } });
    fireEvent.click(screen.getByTestId("warehouse-submit"));

    const created = await screen.findByTestId("wms-created-warehouses");
    expect(created).toHaveTextContent("SZ-BONDED-01");
    expect(created).toHaveTextContent("Shenzhen Bonded Warehouse");
    expect(screen.queryByTestId("create-warehouse-modal")).toBeNull();
  });

  it("turns a low-stock SKU into a purchase order with a link to the PO page", async () => {
    renderPage(<WmsPage />, "manager");
    await waitFor(() =>
      expect(screen.getByTestId("low-stock-create-po-var-1")).toBeInTheDocument()
    );

    fireEvent.click(screen.getByTestId("low-stock-create-po-var-1"));
    expect(screen.getByTestId("low-stock-po-modal")).toBeInTheDocument();
    // Pre-filled from the backend suggestion and the active supplier.
    expect((screen.getByTestId("low-stock-po-quantity") as HTMLInputElement).value).toBe("212");
    expect((screen.getByTestId("low-stock-po-supplier") as HTMLSelectElement).value).toBe(
      "supplier-1"
    );
    expect(screen.getByTestId("low-stock-po-submit")).toBeDisabled();

    fireEvent.change(screen.getByTestId("low-stock-po-unit-cost"), {
      target: { value: "14.20" },
    });
    fireEvent.click(screen.getByTestId("low-stock-po-submit"));

    const banner = await screen.findByTestId("low-stock-po-created");
    expect(banner).toHaveTextContent("PO-MOCK-");
    expect(banner).toHaveTextContent("3010.40");
    const link = screen.getByTestId("low-stock-po-link");
    expect(link).toHaveAttribute("href", "/supply-chain/purchase-orders");
    expect(screen.queryByTestId("low-stock-po-modal")).toBeNull();
  });
});
