// Track 7 client module: 1-level light BOM, in-transit headhaul stock transfers,
// and days-of-cover (DOC) weighted replenishment.
//
// Every request travels through `bffFetch` (`/api/bff/v1/supply-chain/...`), so the
// session cookie, the 401 refresh flow and the BFF role gate behave exactly like the
// rest of the workbench ("supply-chain" is on the operator allowlist). The browser
// never re-derives money: a rolled-up BOM cost, a weighted daily run rate and a DOC
// figure are the backend's exact decimal strings, rendered verbatim.
//
// Mock mode (`NEXT_PUBLIC_USE_MOCK_API=true`) never reaches the network. The
// fixtures below are mutable, so a mock BOM edit / assembly / transfer transition
// really moves the state the panel reads.
import { bffFetch } from "@/lib/api";
import { shouldUseMockApi } from "@/lib/data-source";

export type ShippingMethod = "sea_freight" | "air_freight" | "express" | "rail" | "truck";
export type TransferStatus = "draft" | "in_transit" | "received" | "cancelled";
export type RiskLevel = "critical" | "warning" | "healthy";

export interface BomComponentRead {
  id: string;
  parent_variant_id: string;
  parent_sku: string;
  component_variant_id: string;
  component_sku: string;
  qty_per_unit: string;
  waste_rate_pct: string;
  effective_qty_per_unit: string;
  component_unit_cost_base: string;
  line_unit_cost_base: string;
  notes: string | null;
  created_at: string;
  updated_at: string;
}

export interface BomKitRead {
  parent_variant_id: string;
  parent_sku: string;
  components: BomComponentRead[];
  component_count: number;
  rolled_up_unit_cost: string;
}

export interface BomListRead {
  items: BomKitRead[];
  total: number;
}

export interface BomCostRollupLineRead {
  component_id: string;
  component_variant_id: string;
  component_sku: string;
  qty_per_unit: string;
  waste_rate_pct: string;
  effective_qty_per_unit: string;
  component_unit_cost_base: string;
  line_unit_cost_base: string;
}

export interface BomCostRollupRead {
  parent_variant_id: string;
  parent_sku: string;
  base_currency: string;
  component_count: number;
  unit_cost_base: string;
  lines: BomCostRollupLineRead[];
}

export interface BomComponentUpsertRequest {
  parent_variant_id: string;
  component_variant_id: string;
  qty_per_unit: string;
  waste_rate_pct?: string;
  notes?: string | null;
}

export interface BomAssemblyConsumptionRead {
  component_variant_id: string;
  component_sku: string;
  qty_consumed: number;
}

export interface BomAssembleRequest {
  warehouse_id: string;
  qty_kits: number;
  request_id: string;
  notes?: string | null;
}

export interface BomAssembleRead {
  request_id: string;
  warehouse_id: string;
  parent_variant_id: string;
  parent_sku: string;
  qty_kits: number;
  parent_on_hand_qty: number;
  consumed: BomAssemblyConsumptionRead[];
  replayed: boolean;
}

export interface StockTransferLineRead {
  id: string;
  variant_id: string;
  sku: string;
  qty: number;
  received_qty: number;
  unit_freight_cost_base: string;
  line_freight_cost_base: string;
}

export interface StockTransferRead {
  id: string;
  tenant_id: string;
  transfer_no: string;
  source_warehouse_id: string;
  source_warehouse_name: string;
  destination_warehouse_id: string;
  destination_warehouse_name: string;
  status: TransferStatus;
  shipping_method: ShippingMethod;
  tracking_no: string | null;
  eta_days: number;
  dispatched_at: string | null;
  received_at: string | null;
  notes: string | null;
  created_by: string | null;
  created_at: string;
  updated_at: string;
  total_qty: number;
  total_freight_cost_base: string;
  lines: StockTransferLineRead[];
}

export interface StockTransferListRead {
  items: StockTransferRead[];
  total: number;
}

export interface StockTransferCreateRequest {
  source_warehouse_id: string;
  destination_warehouse_id: string;
  shipping_method?: ShippingMethod;
  tracking_no?: string | null;
  eta_days?: number;
  notes?: string | null;
  lines: Array<{
    variant_id: string;
    qty: number;
    unit_freight_cost_base?: string;
  }>;
}

export interface DocReplenishmentRowRead {
  variant_id: string;
  sku: string;
  product_name: string;
  local_available_qty: number;
  overseas_available_qty: number;
  in_transit_qty: number;
  total_available_qty: number;
  units_sold_7d: number;
  units_sold_30d: number;
  units_sold_90d: number;
  weighted_daily_sales: string;
  doc_days: string | null;
  suggested_reorder_qty: number;
  risk_level: RiskLevel;
}

export interface DocReplenishmentRead {
  target_doc_days: number;
  generated_at: string;
  items: DocReplenishmentRowRead[];
  total: number;
  critical_count: number;
  warning_count: number;
}

/** One SKU option in the BOM / transfer selectors. */
export interface SkuOption {
  id: string;
  sku: string;
}

/** One warehouse option in the transfer / assembly selectors. */
export interface WarehouseOption {
  id: string;
  label: string;
}

/** `"sea_freight"` -> bilingual label, no recomputation of anything. */
export const SHIPPING_METHODS: readonly ShippingMethod[] = [
  "sea_freight",
  "air_freight",
  "express",
  "rail",
  "truck",
] as const;

const SHIPPING_LABELS: Record<ShippingMethod, { zh: string; en: string }> = {
  sea_freight: { zh: "海运头程", en: "Sea freight" },
  air_freight: { zh: "空运头程", en: "Air freight" },
  express: { zh: "国际快递", en: "Express" },
  rail: { zh: "中欧铁路", en: "Rail" },
  truck: { zh: "陆运", en: "Truck" },
};

export function shippingMethodLabel(method: ShippingMethod): string {
  return SHIPPING_LABELS[method]?.en ?? String(method);
}

export function shippingMethodLabelZh(method: ShippingMethod): string {
  return SHIPPING_LABELS[method]?.zh ?? String(method);
}

const STATUS_LABELS: Record<TransferStatus, { zh: string; en: string }> = {
  draft: { zh: "草稿", en: "Draft" },
  in_transit: { zh: "在途", en: "In transit" },
  received: { zh: "已签收", en: "Received" },
  cancelled: { zh: "已取消", en: "Cancelled" },
};

export function transferStatusLabel(status: TransferStatus): string {
  return STATUS_LABELS[status]?.en ?? String(status);
}

export function transferStatusLabelZh(status: TransferStatus): string {
  return STATUS_LABELS[status]?.zh ?? String(status);
}

/** Only `draft` loads may be dispatched or cancelled; only `in_transit` ones received. */
export function transferActions(status: TransferStatus): {
  canDispatch: boolean;
  canReceive: boolean;
  canCancel: boolean;
} {
  return {
    canDispatch: status === "draft",
    canReceive: status === "in_transit",
    canCancel: status === "draft",
  };
}

const RISK_LABELS: Record<RiskLevel, { zh: string; en: string }> = {
  critical: { zh: "紧急", en: "Critical" },
  warning: { zh: "预警", en: "Warning" },
  healthy: { zh: "健康", en: "Healthy" },
};

export function riskLevelLabel(risk: RiskLevel): string {
  return RISK_LABELS[risk]?.en ?? String(risk);
}

export function riskLevelLabelZh(risk: RiskLevel): string {
  return RISK_LABELS[risk]?.zh ?? String(risk);
}

/** Tailwind tone per risk band, shared by the badge and the row highlight. */
export function riskTone(risk: RiskLevel): string {
  if (risk === "critical") {
    return "border-rose-200 bg-rose-50 text-rose-700 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-300";
  }
  if (risk === "warning") {
    return "border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-300";
  }
  return "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-300";
}

/** `"3.50"` -> `"3.50 天"`; `null` (no demand) renders as an open-ended cover. */
export function formatDocDays(docDays: string | null): string {
  if (docDays === null || docDays === undefined || docDays === "") return "∞";
  return `${docDays} d`;
}

/** Renders the backend's decimal string, never a parsed float. */
export function formatDecimal(value: string | null | undefined, fallback = "0.0000"): string {
  if (value === null || value === undefined || value === "") return fallback;
  return value;
}

/** Sum of the per-transfer line quantities the server already returned. */
export function transferTotalQty(transfer: StockTransferRead): number {
  return transfer.total_qty > 0
    ? transfer.total_qty
    : transfer.lines.reduce((total, line) => total + line.qty, 0);
}

/**
 * Warehouse labels for the selectors: prefer the real names the transfer list
 * already carries, fall back to the short id the /wms page shows.
 */
export function mergeWarehouseLabels(
  warehouseIds: string[],
  transfers: StockTransferRead[],
): WarehouseOption[] {
  const known = new Map<string, string>();
  for (const transfer of transfers) {
    known.set(transfer.source_warehouse_id, transfer.source_warehouse_name);
    known.set(transfer.destination_warehouse_id, transfer.destination_warehouse_name);
  }
  return warehouseIds.map((id) => ({
    id,
    label: known.get(id) ?? `Warehouse ${id.slice(0, 8)}`,
  }));
}

// ---------------------------------------------------------------------------
// Exact fixed-point helpers (shared by the mock fixtures and the tests; live
// values are always the server's strings, never recomputed here).
// ---------------------------------------------------------------------------

const DEFAULT_SCALE = 4;

function toScaled(value: string, scale: number): bigint {
  const trimmed = String(value).trim();
  if (trimmed === "" || trimmed === "0") return BigInt(0);
  const negative = trimmed.startsWith("-");
  const unsigned = negative ? trimmed.slice(1) : trimmed;
  const [whole, fraction = ""] = unsigned.split(".");
  const padded = (fraction + "0".repeat(scale)).slice(0, scale);
  const digits = `${whole || "0"}${padded}`.replace(/^0+(?=\d)/, "");
  const scaled = BigInt(digits || "0");
  return negative ? -scaled : scaled;
}

function fromScaled(value: bigint, scale: number): string {
  const negative = value < BigInt(0);
  const digits = (negative ? -value : value).toString().padStart(scale + 1, "0");
  const whole = digits.slice(0, digits.length - scale);
  const fraction = scale > 0 ? `.${digits.slice(digits.length - scale)}` : "";
  return `${negative ? "-" : ""}${whole}${fraction}`;
}

/** Exact fixed-point addition of decimal strings (`"33.9504" + "3.0000"`). */
export function addFixed(values: string[], scale = DEFAULT_SCALE): string {
  const total = values.reduce((sum, value) => sum + toScaled(value, scale), BigInt(0));
  return fromScaled(total, scale);
}

/** Exact fixed-point multiply, HALF_UP on the last kept digit. */
export function multiplyFixed(a: string, b: string, scale = DEFAULT_SCALE): string {
  const product = toScaled(a, scale) * toScaled(b, scale);
  const divisor = BigInt(10) ** BigInt(scale);
  const half = divisor / BigInt(2);
  const rounded = product >= BigInt(0) ? (product + half) / divisor : (product - half) / divisor;
  return fromScaled(rounded, scale);
}

/** `1 + pct / 100` as an exact fixed-point multiplier. */
export function wasteMultiplier(wasteRatePct: string, scale = DEFAULT_SCALE): string {
  return addFixed(["1", divideBy100(wasteRatePct, scale)], scale);
}

function divideBy100(value: string, scale: number): string {
  return fromScaled(toScaled(value, scale) / BigInt(100), scale);
}

/** Integer ceiling of a decimal string (`"4.5"` -> 5), never a float. */
export function ceilFixed(value: string, scale = DEFAULT_SCALE): number {
  const scaled = toScaled(value, scale);
  const divisor = BigInt(10) ** BigInt(scale);
  const whole = scaled / divisor;
  const remainder = scaled % divisor;
  return Number(remainder > BigInt(0) ? whole + BigInt(1) : whole);
}

/** Units consumed by assembling `qtyKits` of one component line. */
export function componentUnitsFor(
  qtyPerUnit: string,
  wasteRatePct: string,
  qtyKits: number,
): number {
  const gross = multiplyFixed(qtyPerUnit, wasteMultiplier(wasteRatePct));
  return ceilFixed(multiplyFixed(gross, String(qtyKits)));
}

// ---------------------------------------------------------------------------
// Mock fixtures (mutable, so mock-mode mutations are visible)
// ---------------------------------------------------------------------------

const MOCK_SKUS: SkuOption[] = [
  { id: "variant-kit-panel-600", sku: "KIT-LED-PANEL-600" },
  { id: "variant-panel-600", sku: "LED-PANEL-600-40W" },
  { id: "variant-driver-40", sku: "LED-DRIVER-40W" },
  { id: "variant-frame-al", sku: "LED-FRAME-AL-600" },
  { id: "variant-cob-5m", sku: "LED-COB-24V-5M" },
];

const MOCK_WAREHOUSES: WarehouseOption[] = [
  { id: "wh-cn-sz-factory", label: "SZ Factory 深圳总仓" },
  { id: "wh-us-lax-fba", label: "US LAX FBA 洛杉矶海外仓" },
];

const MOCK_BOM: BomKitRead[] = [
  {
    parent_variant_id: "variant-kit-panel-600",
    parent_sku: "KIT-LED-PANEL-600",
    component_count: 3,
    rolled_up_unit_cost: "85.0300",
    components: [
      {
        id: "bom-mock-1",
        parent_variant_id: "variant-kit-panel-600",
        parent_sku: "KIT-LED-PANEL-600",
        component_variant_id: "variant-panel-600",
        component_sku: "LED-PANEL-600-40W",
        qty_per_unit: "1.0000",
        waste_rate_pct: "0.00",
        effective_qty_per_unit: "1.0000",
        component_unit_cost_base: "58.0000",
        line_unit_cost_base: "58.0000",
        notes: null,
        created_at: "2026-11-20T02:00:00Z",
        updated_at: "2026-11-20T02:00:00Z",
      },
      {
        id: "bom-mock-2",
        parent_variant_id: "variant-kit-panel-600",
        parent_sku: "KIT-LED-PANEL-600",
        component_variant_id: "variant-driver-40",
        component_sku: "LED-DRIVER-40W",
        qty_per_unit: "1.0000",
        waste_rate_pct: "2.00",
        effective_qty_per_unit: "1.0200",
        component_unit_cost_base: "12.5000",
        line_unit_cost_base: "12.7500",
        notes: "2% 装配损耗",
        created_at: "2026-11-20T02:05:00Z",
        updated_at: "2026-11-20T02:05:00Z",
      },
      {
        id: "bom-mock-3",
        parent_variant_id: "variant-kit-panel-600",
        parent_sku: "KIT-LED-PANEL-600",
        component_variant_id: "variant-frame-al",
        component_sku: "LED-FRAME-AL-600",
        qty_per_unit: "2.0000",
        waste_rate_pct: "5.00",
        effective_qty_per_unit: "2.1000",
        component_unit_cost_base: "6.8000",
        line_unit_cost_base: "14.2800",
        notes: null,
        created_at: "2026-11-20T02:10:00Z",
        updated_at: "2026-11-20T02:10:00Z",
      },
    ],
  },
];

const MOCK_COMPONENT_COSTS: Record<string, string> = {
  "variant-panel-600": "58.0000",
  "variant-driver-40": "12.5000",
  "variant-frame-al": "6.8000",
  "variant-cob-5m": "31.2000",
};

const MOCK_TRANSFERS: StockTransferRead[] = [
  {
    id: "transfer-mock-draft",
    tenant_id: "t1",
    transfer_no: "TRF-MOCK-0001",
    source_warehouse_id: "wh-cn-sz-factory",
    source_warehouse_name: "SZ Factory 深圳总仓",
    destination_warehouse_id: "wh-us-lax-fba",
    destination_warehouse_name: "US LAX FBA 洛杉矶海外仓",
    status: "draft",
    shipping_method: "sea_freight",
    tracking_no: null,
    eta_days: 28,
    dispatched_at: null,
    received_at: null,
    notes: "第 4 周补货拼柜",
    created_by: "u-mock-owner",
    created_at: "2026-11-24T02:00:00Z",
    updated_at: "2026-11-24T02:00:00Z",
    total_qty: 90,
    total_freight_cost_base: "112.5000",
    lines: [
      {
        id: "transfer-mock-draft-l1",
        variant_id: "variant-panel-600",
        sku: "LED-PANEL-600-40W",
        qty: 60,
        received_qty: 0,
        unit_freight_cost_base: "1.2500",
        line_freight_cost_base: "75.0000",
      },
      {
        id: "transfer-mock-draft-l2",
        variant_id: "variant-driver-40",
        sku: "LED-DRIVER-40W",
        qty: 30,
        received_qty: 0,
        unit_freight_cost_base: "1.2500",
        line_freight_cost_base: "37.5000",
      },
    ],
  },
  {
    id: "transfer-mock-transit",
    tenant_id: "t1",
    transfer_no: "TRF-MOCK-0002",
    source_warehouse_id: "wh-cn-sz-factory",
    source_warehouse_name: "SZ Factory 深圳总仓",
    destination_warehouse_id: "wh-us-lax-fba",
    destination_warehouse_name: "US LAX FBA 洛杉矶海外仓",
    status: "in_transit",
    shipping_method: "air_freight",
    tracking_no: "MATS-889900",
    eta_days: 9,
    dispatched_at: "2026-11-18T06:00:00Z",
    received_at: null,
    notes: "空运急补",
    created_by: "u-mock-owner",
    created_at: "2026-11-17T02:00:00Z",
    updated_at: "2026-11-18T06:00:00Z",
    total_qty: 40,
    total_freight_cost_base: "320.0000",
    lines: [
      {
        id: "transfer-mock-transit-l1",
        variant_id: "variant-panel-600",
        sku: "LED-PANEL-600-40W",
        qty: 40,
        received_qty: 0,
        unit_freight_cost_base: "8.0000",
        line_freight_cost_base: "320.0000",
      },
    ],
  },
];

const MOCK_DOC_ROWS: DocReplenishmentRowRead[] = [
  {
    variant_id: "variant-panel-600",
    sku: "LED-PANEL-600-40W",
    product_name: "600x600 LED Panel 40W",
    local_available_qty: 120,
    overseas_available_qty: 30,
    in_transit_qty: 40,
    total_available_qty: 190,
    units_sold_7d: 70,
    units_sold_30d: 300,
    units_sold_90d: 900,
    weighted_daily_sales: "10.0000",
    doc_days: "19.00",
    suggested_reorder_qty: 110,
    risk_level: "healthy",
  },
  {
    variant_id: "variant-driver-40",
    sku: "LED-DRIVER-40W",
    product_name: "40W Constant Current Driver",
    local_available_qty: 60,
    overseas_available_qty: 0,
    in_transit_qty: 30,
    total_available_qty: 90,
    units_sold_7d: 28,
    units_sold_30d: 120,
    units_sold_90d: 360,
    weighted_daily_sales: "4.0000",
    doc_days: "22.50",
    suggested_reorder_qty: 30,
    risk_level: "healthy",
  },
  {
    variant_id: "variant-cob-5m",
    sku: "LED-COB-24V-5M",
    product_name: "COB LED Strip 24V 5m",
    local_available_qty: 12,
    overseas_available_qty: 4,
    in_transit_qty: 0,
    total_available_qty: 16,
    units_sold_7d: 63,
    units_sold_30d: 270,
    units_sold_90d: 810,
    weighted_daily_sales: "9.0000",
    doc_days: "1.78",
    suggested_reorder_qty: 254,
    risk_level: "critical",
  },
  {
    variant_id: "variant-frame-al",
    sku: "LED-FRAME-AL-600",
    product_name: "Aluminium Frame 600",
    local_available_qty: 55,
    overseas_available_qty: 10,
    in_transit_qty: 0,
    total_available_qty: 65,
    units_sold_7d: 42,
    units_sold_30d: 180,
    units_sold_90d: 540,
    weighted_daily_sales: "6.0000",
    doc_days: "10.83",
    suggested_reorder_qty: 115,
    risk_level: "warning",
  },
];

const mockState = {
  bom: [...MOCK_BOM],
  transfers: [...MOCK_TRANSFERS],
  docRows: [...MOCK_DOC_ROWS],
};

/** Restore the mock fixtures (tests only; mock mode only). */
export function resetTrack7Mocks(): void {
  mockState.bom = MOCK_BOM.map((kit) => ({
    ...kit,
    components: [...kit.components],
  }));
  mockState.transfers = MOCK_TRANSFERS.map((transfer) => ({ ...transfer }));
  mockState.docRows = [...MOCK_DOC_ROWS];
}

// ---------------------------------------------------------------------------
// Defensive normalisation: a proxy/older backend must never crash a render.
// ---------------------------------------------------------------------------

function asArray<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

function asNumber(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function asText(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function normalizeBomList(raw: unknown): BomListRead {
  const page = (raw ?? {}) as Partial<BomListRead>;
  const items = asArray<BomKitRead>(page.items).map((kit) => ({
    ...kit,
    components: asArray<BomComponentRead>(kit.components),
    component_count: asNumber(kit.component_count, asArray(kit.components).length),
    rolled_up_unit_cost: asText(kit.rolled_up_unit_cost, "0.0000"),
  }));
  return { items, total: asNumber(page.total, items.length) };
}

function normalizeTransferList(raw: unknown): StockTransferListRead {
  const page = (raw ?? {}) as Partial<StockTransferListRead>;
  const items = asArray<StockTransferRead>(page.items).map((transfer) => ({
    ...transfer,
    lines: asArray<StockTransferLineRead>(transfer.lines),
    total_qty: asNumber(transfer.total_qty),
    total_freight_cost_base: asText(transfer.total_freight_cost_base, "0.0000"),
  }));
  return { items, total: asNumber(page.total, items.length) };
}

function normalizeDoc(raw: unknown): DocReplenishmentRead {
  const page = (raw ?? {}) as Partial<DocReplenishmentRead>;
  const items = asArray<DocReplenishmentRowRead>(page.items).map((row) => ({
    ...row,
    weighted_daily_sales: asText(row.weighted_daily_sales, "0.0000"),
    doc_days: typeof row.doc_days === "string" ? row.doc_days : null,
    suggested_reorder_qty: asNumber(row.suggested_reorder_qty),
  }));
  return {
    target_doc_days: asNumber(page.target_doc_days, 30),
    generated_at: asText(page.generated_at),
    items,
    total: asNumber(page.total, items.length),
    critical_count: asNumber(
      page.critical_count,
      items.filter((row) => row.risk_level === "critical").length,
    ),
    warning_count: asNumber(
      page.warning_count,
      items.filter((row) => row.risk_level === "warning").length,
    ),
  };
}

// ---------------------------------------------------------------------------
// Mock-mode implementations (exact fixed-point math, never a float)
// ---------------------------------------------------------------------------

function mockRollup(parentVariantId: string): BomCostRollupRead {
  const kit = mockState.bom.find((item) => item.parent_variant_id === parentVariantId);
  return {
    parent_variant_id: parentVariantId,
    parent_sku: kit?.parent_sku ?? "",
    base_currency: "CNY",
    component_count: kit?.components.length ?? 0,
    unit_cost_base: kit?.rolled_up_unit_cost ?? "0.0000",
    lines: (kit?.components ?? []).map((component) => ({
      component_id: component.id,
      component_variant_id: component.component_variant_id,
      component_sku: component.component_sku,
      qty_per_unit: component.qty_per_unit,
      waste_rate_pct: component.waste_rate_pct,
      effective_qty_per_unit: component.effective_qty_per_unit,
      component_unit_cost_base: component.component_unit_cost_base,
      line_unit_cost_base: component.line_unit_cost_base,
    })),
  };
}

function mockRebuildKit(parentVariantId: string): void {
  const kit = mockState.bom.find((item) => item.parent_variant_id === parentVariantId);
  if (!kit) return;
  kit.component_count = kit.components.length;
  kit.rolled_up_unit_cost = addFixed(kit.components.map((item) => item.line_unit_cost_base));
}

function mockComponentFrom(
  parentVariantId: string,
  parentSku: string,
  componentId: string,
  qtyPerUnit: string,
  wasteRatePct: string,
  notes: string | null,
): BomComponentRead {
  const sku = MOCK_SKUS.find((option) => option.id === componentId)?.sku ?? componentId;
  const unitCost = MOCK_COMPONENT_COSTS[componentId] ?? "0.0000";
  const effective = multiplyFixed(qtyPerUnit, wasteMultiplier(wasteRatePct));
  return {
    id: `bom-mock-${Math.random().toString(36).slice(2, 10)}`,
    parent_variant_id: parentVariantId,
    parent_sku: parentSku,
    component_variant_id: componentId,
    component_sku: sku,
    qty_per_unit: qtyPerUnit,
    waste_rate_pct: wasteRatePct,
    effective_qty_per_unit: effective,
    component_unit_cost_base: unitCost,
    line_unit_cost_base: multiplyFixed(effective, unitCost),
    notes,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  };
}

// ---------------------------------------------------------------------------
// Pickers
// ---------------------------------------------------------------------------

/** SKU options: `/{v1}/catalog/variants` in live mode, fixture kits in mock mode. */
export async function fetchSkuOptions(): Promise<SkuOption[]> {
  if (shouldUseMockApi()) return [...MOCK_SKUS];
  const raw = await bffFetch<unknown>("/v1/catalog/variants?limit=100&offset=0");
  return asArray<{ id: string; sku: string }>((raw as { items?: unknown })?.items).map((item) => ({
    id: item.id,
    sku: item.sku,
  }));
}

/** Warehouse options: `/v1/orders/warehouses` ids enriched with transfer names. */
export async function fetchWarehouseOptions(
  transfers: StockTransferRead[] = [],
): Promise<WarehouseOption[]> {
  if (shouldUseMockApi()) return [...MOCK_WAREHOUSES];
  const raw = await bffFetch<unknown>("/v1/orders/warehouses");
  const ids = asArray<{ id: string }>((raw as { items?: unknown })?.items).map((item) => item.id);
  return mergeWarehouseLabels(ids, transfers);
}

// ---------------------------------------------------------------------------
// 1-level light BOM
// ---------------------------------------------------------------------------

export async function fetchBomKits(parentVariantId?: string): Promise<BomListRead> {
  if (shouldUseMockApi()) {
    const items = parentVariantId
      ? mockState.bom.filter((kit) => kit.parent_variant_id === parentVariantId)
      : [...mockState.bom];
    return { items, total: items.length };
  }
  const query = parentVariantId ? `?parent_variant_id=${encodeURIComponent(parentVariantId)}` : "";
  return normalizeBomList(await bffFetch<unknown>(`/v1/supply-chain/bom${query}`));
}

export async function upsertBomComponent(
  payload: BomComponentUpsertRequest,
): Promise<BomComponentRead> {
  const wasteRatePct = payload.waste_rate_pct ?? "0";
  if (shouldUseMockApi()) {
    let kit = mockState.bom.find((item) => item.parent_variant_id === payload.parent_variant_id);
    if (!kit) {
      kit = {
        parent_variant_id: payload.parent_variant_id,
        parent_sku:
          MOCK_SKUS.find((option) => option.id === payload.parent_variant_id)?.sku ??
          payload.parent_variant_id,
        components: [],
        component_count: 0,
        rolled_up_unit_cost: "0.0000",
      };
      mockState.bom.push(kit);
    }
    const existing = kit.components.find(
      (item) => item.component_variant_id === payload.component_variant_id,
    );
    const next = mockComponentFrom(
      payload.parent_variant_id,
      kit.parent_sku,
      payload.component_variant_id,
      payload.qty_per_unit,
      wasteRatePct,
      payload.notes ?? null,
    );
    if (existing) {
      existing.qty_per_unit = next.qty_per_unit;
      existing.waste_rate_pct = next.waste_rate_pct;
      existing.effective_qty_per_unit = next.effective_qty_per_unit;
      existing.line_unit_cost_base = next.line_unit_cost_base;
      existing.notes = next.notes;
      existing.updated_at = next.updated_at;
      mockRebuildKit(payload.parent_variant_id);
      return existing;
    }
    kit.components.push(next);
    mockRebuildKit(payload.parent_variant_id);
    return next;
  }
  return await bffFetch<BomComponentRead>("/v1/supply-chain/bom/components", {
    method: "POST",
    body: JSON.stringify({ ...payload, waste_rate_pct: wasteRatePct }),
  });
}

export async function deleteBomComponent(componentId: string): Promise<void> {
  if (shouldUseMockApi()) {
    for (const kit of mockState.bom) {
      const before = kit.components.length;
      kit.components = kit.components.filter((item) => item.id !== componentId);
      if (kit.components.length !== before) {
        mockRebuildKit(kit.parent_variant_id);
        return;
      }
    }
    return;
  }
  await bffFetch<unknown>(`/v1/supply-chain/bom/components/${encodeURIComponent(componentId)}`, {
    method: "DELETE",
    parseJson: false,
  });
}

export async function fetchBomCostRollup(parentVariantId: string): Promise<BomCostRollupRead> {
  if (shouldUseMockApi()) return mockRollup(parentVariantId);
  const raw = await bffFetch<unknown>(
    `/v1/supply-chain/bom/${encodeURIComponent(parentVariantId)}/cost-rollup`,
  );
  const rollup = (raw ?? {}) as Partial<BomCostRollupRead>;
  return {
    parent_variant_id: asText(rollup.parent_variant_id, parentVariantId),
    parent_sku: asText(rollup.parent_sku),
    base_currency: asText(rollup.base_currency, "CNY"),
    component_count: asNumber(rollup.component_count),
    unit_cost_base: asText(rollup.unit_cost_base, "0.0000"),
    lines: asArray<BomCostRollupLineRead>(rollup.lines),
  };
}

export async function assembleBomKit(
  parentVariantId: string,
  payload: BomAssembleRequest,
): Promise<BomAssembleRead> {
  if (shouldUseMockApi()) {
    const kit = mockState.bom.find((item) => item.parent_variant_id === parentVariantId);
    const consumed = (kit?.components ?? []).map((component) => ({
      component_variant_id: component.component_variant_id,
      component_sku: component.component_sku,
      qty_consumed: componentUnitsFor(
        component.qty_per_unit,
        component.waste_rate_pct,
        payload.qty_kits,
      ),
    }));
    return {
      request_id: payload.request_id,
      warehouse_id: payload.warehouse_id,
      parent_variant_id: parentVariantId,
      parent_sku: kit?.parent_sku ?? "",
      qty_kits: payload.qty_kits,
      parent_on_hand_qty: payload.qty_kits,
      consumed,
      replayed: false,
    };
  }
  return await bffFetch<BomAssembleRead>(
    `/v1/supply-chain/bom/${encodeURIComponent(parentVariantId)}/assemble`,
    { method: "POST", body: JSON.stringify(payload) },
  );
}

// ---------------------------------------------------------------------------
// In-transit headhaul stock transfers
// ---------------------------------------------------------------------------

export async function fetchStockTransfers(): Promise<StockTransferListRead> {
  if (shouldUseMockApi()) {
    return {
      items: [...mockState.transfers],
      total: mockState.transfers.length,
    };
  }
  return normalizeTransferList(await bffFetch<unknown>("/v1/supply-chain/transfers"));
}

export async function createStockTransfer(
  payload: StockTransferCreateRequest,
): Promise<StockTransferRead> {
  if (shouldUseMockApi()) {
    const transferNo = `TRF-MOCK-${String(mockState.transfers.length + 1).padStart(4, "0")}`;
    const source =
      MOCK_WAREHOUSES.find((item) => item.id === payload.source_warehouse_id)?.label ??
      "SZ Factory 深圳总仓";
    const destination =
      MOCK_WAREHOUSES.find((item) => item.id === payload.destination_warehouse_id)?.label ??
      "US LAX FBA 洛杉矶海外仓";
    const lines: StockTransferLineRead[] = payload.lines.map((line, index) => {
      const unit = line.unit_freight_cost_base ?? "0.0000";
      return {
        id: `${transferNo}-l${index + 1}`,
        variant_id: line.variant_id,
        sku: MOCK_SKUS.find((option) => option.id === line.variant_id)?.sku ?? line.variant_id,
        qty: line.qty,
        received_qty: 0,
        unit_freight_cost_base: unit,
        line_freight_cost_base: multiplyFixed(unit, String(line.qty)),
      };
    });
    const created: StockTransferRead = {
      id: `transfer-mock-${mockState.transfers.length + 1}`,
      tenant_id: "t1",
      transfer_no: transferNo,
      source_warehouse_id: payload.source_warehouse_id,
      source_warehouse_name: source,
      destination_warehouse_id: payload.destination_warehouse_id,
      destination_warehouse_name: destination,
      status: "draft",
      shipping_method: payload.shipping_method ?? "sea_freight",
      tracking_no: payload.tracking_no ?? null,
      eta_days: payload.eta_days ?? 25,
      dispatched_at: null,
      received_at: null,
      notes: payload.notes ?? null,
      created_by: "u-mock-owner",
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      total_qty: lines.reduce((total, line) => total + line.qty, 0),
      total_freight_cost_base: addFixed(lines.map((line) => line.line_freight_cost_base)),
      lines,
    };
    mockState.transfers = [created, ...mockState.transfers];
    return created;
  }
  return await bffFetch<StockTransferRead>("/v1/supply-chain/transfers", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

async function transitionTransfer(
  transferId: string,
  action: "dispatch" | "receive" | "cancel",
): Promise<StockTransferRead> {
  if (shouldUseMockApi()) {
    const transfer = mockState.transfers.find((item) => item.id === transferId);
    if (!transfer) throw new Error("Stock transfer not found");
    const now = new Date().toISOString();
    if (action === "dispatch") {
      if (transfer.status !== "draft") {
        throw new Error(
          `Only draft transfers can be dispatched (this one is '${transfer.status}').`,
        );
      }
      transfer.status = "in_transit";
      transfer.dispatched_at = now;
    } else if (action === "receive") {
      if (transfer.status !== "in_transit") {
        throw new Error(
          `Only in-transit transfers can be received (this one is '${transfer.status}').`,
        );
      }
      transfer.status = "received";
      transfer.received_at = now;
      transfer.lines = transfer.lines.map((line) => ({
        ...line,
        received_qty: line.qty,
      }));
    } else {
      if (transfer.status !== "draft") {
        throw new Error(
          `Only draft transfers can be cancelled (this one is '${transfer.status}').`,
        );
      }
      transfer.status = "cancelled";
    }
    transfer.updated_at = now;
    return transfer;
  }
  return await bffFetch<StockTransferRead>(
    `/v1/supply-chain/transfers/${encodeURIComponent(transferId)}/${action}`,
    { method: "POST", body: JSON.stringify({}) },
  );
}

export function dispatchStockTransfer(transferId: string): Promise<StockTransferRead> {
  return transitionTransfer(transferId, "dispatch");
}

export function receiveStockTransfer(transferId: string): Promise<StockTransferRead> {
  return transitionTransfer(transferId, "receive");
}

export function cancelStockTransfer(transferId: string): Promise<StockTransferRead> {
  return transitionTransfer(transferId, "cancel");
}

// ---------------------------------------------------------------------------
// Days-of-cover (DOC) weighted replenishment
// ---------------------------------------------------------------------------

/** Re-sort + re-count a DOC page exactly like the backend does (critical first). */
export function sortDocRows(rows: DocReplenishmentRowRead[]): DocReplenishmentRowRead[] {
  const rank: Record<RiskLevel, number> = {
    critical: 0,
    warning: 1,
    healthy: 2,
  };
  return [...rows].sort((a, b) => {
    const byRisk = rank[a.risk_level] - rank[b.risk_level];
    if (byRisk !== 0) return byRisk;
    const aDays = a.doc_days === null ? Number.POSITIVE_INFINITY : Number(a.doc_days);
    const bDays = b.doc_days === null ? Number.POSITIVE_INFINITY : Number(b.doc_days);
    if (aDays !== bDays) return aDays - bDays;
    return a.sku.localeCompare(b.sku);
  });
}

export function docRiskCounts(rows: DocReplenishmentRowRead[]): {
  critical: number;
  warning: number;
  healthy: number;
} {
  return {
    critical: rows.filter((row) => row.risk_level === "critical").length,
    warning: rows.filter((row) => row.risk_level === "warning").length,
    healthy: rows.filter((row) => row.risk_level === "healthy").length,
  };
}

export async function fetchDocReplenishment(targetDocDays = 30): Promise<DocReplenishmentRead> {
  if (shouldUseMockApi()) {
    const rows = sortDocRows(mockState.docRows);
    const counts = docRiskCounts(rows);
    return {
      target_doc_days: targetDocDays,
      generated_at: new Date().toISOString(),
      items: rows,
      total: rows.length,
      critical_count: counts.critical,
      warning_count: counts.warning,
    };
  }
  return normalizeDoc(
    await bffFetch<unknown>(
      `/v1/supply-chain/doc-replenishment?target_doc_days=${encodeURIComponent(String(targetDocDays))}`,
    ),
  );
}
