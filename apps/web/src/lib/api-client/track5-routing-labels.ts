// Track 5 API client: order routing / anti-loss rules and carrier waybill labels.
//
// Every call goes through `bffFetch` from `@/lib/api`, so the browser only ever talks
// to the same-origin `/api/bff/*` shell (`/orders` is on the operator allowlist). The
// typed contract comes from `types.gen.ts`; no ORM/SQL lives in the web app.
//
// Mock mode (`NEXT_PUBLIC_USE_MOCK_API=true`) keeps the whole routing/label workflow
// clickable with the fixture store below, so page tests need zero backend.
import { bffFetch } from "@/lib/api";
import type {
  CarrierShippingLabelRead,
  OrderRoutingEvaluationRead,
  RoutingRuleCreate,
  RoutingRuleRead,
  RoutingRuleUpdate,
  ShippingLabelBookRequest,
} from "@/lib/api-client/types.gen";
import { shouldUseMockApi } from "@/lib/data-source";

export const ROUTING_CARRIERS = [
  { code: "sandbox_carrier", label: "Sandbox 沙箱" },
  { code: "yunexpress", label: "YunExpress 云途" },
  { code: "4px", label: "4PX 递四方" },
  { code: "dhl", label: "DHL Express" },
] as const;

export type RoutingCarrierCode = (typeof ROUTING_CARRIERS)[number]["code"];

/** One editable routing rule in the rule builder (all money stays a string). */
export interface RoutingRuleDraft {
  name: string;
  priority: string;
  destinationCountries: string;
  channelTypes: string;
  orderModes: string;
  minMarginPct: string;
  minTotalAmount: string;
  maxWeightKg: string;
  warehouseId: string;
  carrierCode: string;
  serviceCode: string;
  addTags: string;
  holdForReview: boolean;
  holdReason: string;
}

export const EMPTY_ROUTING_RULE_DRAFT: RoutingRuleDraft = {
  name: "",
  priority: "100",
  destinationCountries: "",
  channelTypes: "",
  orderModes: "",
  minMarginPct: "",
  minTotalAmount: "",
  maxWeightKg: "",
  warehouseId: "",
  carrierCode: "",
  serviceCode: "",
  addTags: "",
  holdForReview: false,
  holdReason: "",
};

const DECIMAL_PATTERN = /^-?\d+(\.\d+)?$/;
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function splitList(raw: string, transform: (value: string) => string): string[] {
  return raw
    .split(/[,，\s]+/)
    .map((value) => transform(value.trim()))
    .filter((value) => value.length > 0);
}

function decimalString(raw: string): string | null {
  const trimmed = raw.trim();
  if (!trimmed) return null;
  return DECIMAL_PATTERN.test(trimmed) ? trimmed : null;
}

/**
 * Build the `POST /orders/routing-rules` body from the rule-builder draft.
 *
 * Returns `{ error }` instead of throwing so the modal can render an inline message:
 * an anti-loss rule without a name or without any condition would be rejected by the
 * backend anyway, and letting the operator see why is cheaper than a 422 round trip.
 */
export function routingRulePayloadFromDraft(
  draft: RoutingRuleDraft,
): { payload: RoutingRuleCreate } | { error: string } {
  const name = draft.name.trim();
  if (!name) return { error: "规则名称不能为空 / Rule name is required." };
  const priority = Number.parseInt(draft.priority || "100", 10);
  if (!Number.isFinite(priority) || priority < 0) {
    return { error: "优先级必须是非负整数 / Priority must be a non-negative integer." };
  }
  const minMarginPct = decimalString(draft.minMarginPct);
  if (draft.minMarginPct.trim() && minMarginPct === null) {
    return { error: "最低毛利率必须是数字 / Minimum margin must be a decimal." };
  }
  const minTotalAmount = decimalString(draft.minTotalAmount);
  if (draft.minTotalAmount.trim() && minTotalAmount === null) {
    return { error: "最低订单金额必须是数字 / Minimum order amount must be a decimal." };
  }
  const maxWeightKg = decimalString(draft.maxWeightKg);
  if (draft.maxWeightKg.trim() && maxWeightKg === null) {
    return { error: "最大重量必须是数字 / Max weight must be a decimal." };
  }
  const warehouseId = draft.warehouseId.trim();
  if (warehouseId && !UUID_PATTERN.test(warehouseId)) {
    return { error: "仓库 ID 必须是 UUID / Warehouse id must be a UUID." };
  }
  const destinationCountries = splitList(draft.destinationCountries, (v) => v.toUpperCase());
  const channelTypes = splitList(draft.channelTypes, (v) => v.toLowerCase());
  const orderModes = splitList(draft.orderModes, (v) => v.toLowerCase());
  const hasScope =
    destinationCountries.length > 0 ||
    channelTypes.length > 0 ||
    orderModes.length > 0 ||
    minTotalAmount !== null ||
    maxWeightKg !== null;
  if (!hasScope && minMarginPct === null) {
    return {
      error:
        "至少需要一个条件（国家/渠道/订单模式/金额/重量）或毛利率下限 / At least one condition or margin floor is required.",
    };
  }
  const carrierCode = draft.carrierCode.trim();
  const serviceCode = draft.serviceCode.trim();
  return {
    payload: {
      name,
      priority,
      is_enabled: true,
      conditions: {
        destination_countries: destinationCountries,
        channel_types: channelTypes,
        order_modes: orderModes,
        min_margin_pct: minMarginPct,
        min_total_amount: minTotalAmount,
        max_weight_kg: maxWeightKg,
      },
      actions: {
        warehouse_id: warehouseId || null,
        carrier_code: carrierCode || null,
        service_code: serviceCode || null,
        add_tags: splitList(draft.addTags, (v) => v),
        hold_for_review: draft.holdForReview,
        hold_reason: draft.holdReason.trim() || null,
      },
    },
  };
}

/** Render an existing rule back into the draft shape (for inline editing). */
export function routingRuleDraftFromRule(rule: RoutingRuleRead): RoutingRuleDraft {
  return {
    name: rule.name,
    priority: String(rule.priority),
    destinationCountries: (rule.conditions.destination_countries ?? []).join(", "),
    channelTypes: (rule.conditions.channel_types ?? []).join(", "),
    orderModes: (rule.conditions.order_modes ?? []).join(", "),
    minMarginPct: rule.conditions.min_margin_pct ?? "",
    minTotalAmount: rule.conditions.min_total_amount ?? "",
    maxWeightKg: rule.conditions.max_weight_kg ?? "",
    warehouseId: rule.actions.warehouse_id ?? "",
    carrierCode: rule.actions.carrier_code ?? "",
    serviceCode: rule.actions.service_code ?? "",
    addTags: (rule.actions.add_tags ?? []).join(", "),
    holdForReview: Boolean(rule.actions.hold_for_review),
    holdReason: rule.actions.hold_reason ?? "",
  };
}

/** Human summary of a rule's condition set for the rules table. */
export function describeRoutingRule(rule: RoutingRuleRead): string {
  const parts: string[] = [];
  const countries = rule.conditions.destination_countries ?? [];
  if (countries.length > 0) parts.push(`国家 ${countries.join("/")}`);
  const channels = rule.conditions.channel_types ?? [];
  if (channels.length > 0) parts.push(`渠道 ${channels.join("/")}`);
  const modes = rule.conditions.order_modes ?? [];
  if (modes.length > 0) parts.push(`模式 ${modes.join("/")}`);
  if (rule.conditions.min_margin_pct) parts.push(`毛利 ≥ ${rule.conditions.min_margin_pct}%`);
  if (rule.conditions.min_total_amount) parts.push(`金额 ≥ ${rule.conditions.min_total_amount}`);
  if (rule.conditions.max_weight_kg) parts.push(`重量 ≤ ${rule.conditions.max_weight_kg}kg`);
  return parts.length > 0 ? parts.join(" · ") : "无条件（全局风控）";
}

export function routingRuleActionSummary(rule: RoutingRuleRead): string {
  const parts: string[] = [];
  if (rule.actions.carrier_code) parts.push(rule.actions.carrier_code);
  if (rule.actions.service_code) parts.push(rule.actions.service_code);
  if (rule.actions.hold_for_review) parts.push("拦截审单");
  if ((rule.actions.add_tags ?? []).length > 0) {
    parts.push(`标签 ${(rule.actions.add_tags ?? []).join("/")}`);
  }
  return parts.length > 0 ? parts.join(" · ") : "仅记录";
}

// ---------------------------------------------------------------------------
// URLs (same-origin BFF paths, usable directly as <a href>)
// ---------------------------------------------------------------------------

export function routingRulesPath(): string {
  return "/v1/orders/routing-rules";
}

export function routingEvaluationPath(orderId: string): string {
  return `/v1/orders/${orderId}/routing-evaluation`;
}

export function evaluateRoutingPath(orderId: string): string {
  return `/v1/orders/${orderId}/evaluate-routing`;
}

export function releaseHoldPath(orderId: string): string {
  return `/v1/orders/${orderId}/release-hold`;
}

export function shippingLabelsPath(orderId: string): string {
  return `/v1/orders/${orderId}/shipping-labels`;
}

export function shippingLabelBookPath(orderId: string): string {
  return `/v1/orders/${orderId}/shipping-label`;
}

/** Inline PDF link for a booked 4x6 thermal waybill. */
export function shippingLabelPdfUrl(orderId: string, labelId: string): string {
  return `/api/bff/v1/orders/${orderId}/shipping-labels/${labelId}/pdf`;
}

// ---------------------------------------------------------------------------
// Fixture store (mock mode only)
// ---------------------------------------------------------------------------

function hashDigits(seed: string, length: number): string {
  let hash = 2166136261;
  for (let index = 0; index < seed.length; index += 1) {
    hash ^= seed.charCodeAt(index);
    hash = Math.imul(hash, 16777619) >>> 0;
  }
  let digits = String(hash).padStart(10, "0");
  while (digits.length < length) {
    hash = Math.imul(hash ^ 0x9e3779b9, 16777619) >>> 0;
    digits += String(hash).padStart(10, "0");
  }
  return digits.slice(0, length);
}

const MOCK_TENANT_ID = "22222222-2222-2222-2222-222222222222";
const MOCK_RELEASED_BY = "11111111-1111-1111-1111-111111111111";

const SEED_ROUTING_RULES: RoutingRuleRead[] = [
  {
    id: "rule-negative-margin",
    name: "负毛利立即拦截",
    priority: 10,
    is_enabled: true,
    conditions: {
      destination_countries: [],
      channel_types: [],
      order_modes: [],
      min_margin_pct: "0.00",
      min_total_amount: null,
      max_weight_kg: null,
    },
    actions: {
      warehouse_id: null,
      carrier_code: null,
      service_code: null,
      add_tags: ["anti-loss"],
      hold_for_review: true,
      hold_reason: "负毛利必须由主管放行",
    },
    created_at: "2026-11-20T08:00:00.000Z",
    updated_at: "2026-11-20T08:00:00.000Z",
  },
  {
    id: "rule-eu-fast-lane",
    name: "北欧快线 → YunExpress",
    priority: 100,
    is_enabled: true,
    conditions: {
      destination_countries: ["SE", "NO", "DK", "FI"],
      channel_types: [],
      order_modes: ["b2c_retail"],
      min_margin_pct: null,
      min_total_amount: null,
      max_weight_kg: "25.00",
    },
    actions: {
      warehouse_id: null,
      carrier_code: "yunexpress",
      service_code: "YunExpress-Expedited",
      add_tags: ["eu-fast-lane"],
      hold_for_review: false,
      hold_reason: null,
    },
    created_at: "2026-11-20T08:05:00.000Z",
    updated_at: "2026-11-20T08:05:00.000Z",
  },
];

const SEED_EVALUATIONS: Record<string, OrderRoutingEvaluationRead> = {
  "ord-b2b-1": {
    id: "eval-ord-b2b-1",
    tenant_id: MOCK_TENANT_ID,
    order_id: "ord-b2b-1",
    matched_rule_ids: ["rule-negative-margin"],
    recommended_warehouse_id: null,
    recommended_carrier: "4px",
    recommended_service: "4PX-Standard",
    estimated_margin_pct: "-12.5000",
    is_held: true,
    hold_reason: "Estimated margin -12.5% is negative (built-in anti-loss guard)",
    hold_released_by: null,
    hold_released_at: null,
    applied_tags: ["anti-loss"],
    evaluated_at: "2026-11-25T02:00:00.000Z",
  },
  "ord-b2c-1": {
    id: "eval-ord-b2c-1",
    tenant_id: MOCK_TENANT_ID,
    order_id: "ord-b2c-1",
    matched_rule_ids: ["rule-eu-fast-lane"],
    recommended_warehouse_id: null,
    recommended_carrier: "yunexpress",
    recommended_service: "YunExpress-Expedited",
    estimated_margin_pct: "38.2000",
    is_held: false,
    hold_reason: null,
    hold_released_by: null,
    hold_released_at: null,
    applied_tags: ["eu-fast-lane"],
    evaluated_at: "2026-11-25T02:00:00.000Z",
  },
};

const mockRoutingRules: RoutingRuleRead[] = structuredClone(SEED_ROUTING_RULES);
const mockEvaluations: Record<string, OrderRoutingEvaluationRead> =
  structuredClone(SEED_EVALUATIONS);
let mockLabels: Record<string, CarrierShippingLabelRead[]> = {};

/** Restore the fixture store (tests call this between specs; mock mode only). */
export function resetTrack5MockStore(): void {
  mockRoutingRules.splice(0, mockRoutingRules.length, ...structuredClone(SEED_ROUTING_RULES));
  for (const key of Object.keys(mockEvaluations)) delete mockEvaluations[key];
  Object.assign(mockEvaluations, structuredClone(SEED_EVALUATIONS));
  mockLabels = {};
}

function mockEvaluationFor(orderId: string): OrderRoutingEvaluationRead {
  const existing = mockEvaluations[orderId];
  if (existing) return existing;
  const held = orderId.includes("b2b");
  const evaluation: OrderRoutingEvaluationRead = {
    id: `eval-${orderId}`,
    tenant_id: MOCK_TENANT_ID,
    order_id: orderId,
    matched_rule_ids: held ? ["rule-negative-margin"] : ["rule-eu-fast-lane"],
    recommended_warehouse_id: null,
    recommended_carrier: held ? "4px" : "yunexpress",
    recommended_service: held ? "4PX-Standard" : "YunExpress-Expedited",
    estimated_margin_pct: held ? "-12.5000" : "38.2000",
    is_held: held,
    hold_reason: held
      ? "Estimated margin -12.5% is negative (built-in anti-loss guard)"
      : null,
    hold_released_by: null,
    hold_released_at: null,
    applied_tags: held ? ["anti-loss"] : ["eu-fast-lane"],
    evaluated_at: new Date("2026-11-25T02:00:00.000Z").toISOString(),
  };
  mockEvaluations[orderId] = evaluation;
  return evaluation;
}

// ---------------------------------------------------------------------------
// Live calls (with mock fallback)
// ---------------------------------------------------------------------------

export async function fetchRoutingRules(): Promise<RoutingRuleRead[]> {
  if (shouldUseMockApi()) {
    return [...mockRoutingRules].sort((a, b) => a.priority - b.priority);
  }
  return bffFetch<RoutingRuleRead[]>(routingRulesPath());
}

export async function createRoutingRule(
  payload: RoutingRuleCreate,
): Promise<RoutingRuleRead> {
  if (shouldUseMockApi()) {
    const now = new Date("2026-11-26T00:00:00.000Z").toISOString();
    const created: RoutingRuleRead = {
      id: `rule-${Date.now()}-${mockRoutingRules.length}`,
      name: payload.name,
      priority: payload.priority ?? 100,
      is_enabled: payload.is_enabled ?? true,
      conditions: {
        destination_countries: payload.conditions?.destination_countries ?? [],
        channel_types: payload.conditions?.channel_types ?? [],
        order_modes: payload.conditions?.order_modes ?? [],
        min_margin_pct:
          payload.conditions?.min_margin_pct === undefined ||
          payload.conditions?.min_margin_pct === null
            ? null
            : String(payload.conditions.min_margin_pct),
        min_total_amount:
          payload.conditions?.min_total_amount === undefined ||
          payload.conditions?.min_total_amount === null
            ? null
            : String(payload.conditions.min_total_amount),
        max_weight_kg:
          payload.conditions?.max_weight_kg === undefined ||
          payload.conditions?.max_weight_kg === null
            ? null
            : String(payload.conditions.max_weight_kg),
      },
      actions: {
        warehouse_id: payload.actions?.warehouse_id ?? null,
        carrier_code: payload.actions?.carrier_code ?? null,
        service_code: payload.actions?.service_code ?? null,
        add_tags: payload.actions?.add_tags ?? [],
        hold_for_review: payload.actions?.hold_for_review ?? false,
        hold_reason: payload.actions?.hold_reason ?? null,
      },
      created_at: now,
      updated_at: now,
    };
    mockRoutingRules.push(created);
    return created;
  }
  return bffFetch<RoutingRuleRead>(routingRulesPath(), {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function updateRoutingRule(
  ruleId: string,
  patch: RoutingRuleUpdate,
): Promise<RoutingRuleRead> {
  if (shouldUseMockApi()) {
    const rule = mockRoutingRules.find((item) => item.id === ruleId);
    if (!rule) throw new Error(`Routing rule ${ruleId} not found.`);
    if (patch.name !== undefined && patch.name !== null) rule.name = patch.name;
    if (patch.priority !== undefined && patch.priority !== null) {
      rule.priority = patch.priority;
    }
    if (patch.is_enabled !== undefined && patch.is_enabled !== null) {
      rule.is_enabled = patch.is_enabled;
    }
    if (patch.conditions) {
      rule.conditions = {
        ...rule.conditions,
        destination_countries: patch.conditions.destination_countries ?? [],
        channel_types: patch.conditions.channel_types ?? [],
        order_modes: patch.conditions.order_modes ?? [],
        min_margin_pct:
          patch.conditions.min_margin_pct === undefined ||
          patch.conditions.min_margin_pct === null
            ? null
            : String(patch.conditions.min_margin_pct),
        min_total_amount:
          patch.conditions.min_total_amount === undefined ||
          patch.conditions.min_total_amount === null
            ? null
            : String(patch.conditions.min_total_amount),
        max_weight_kg:
          patch.conditions.max_weight_kg === undefined ||
          patch.conditions.max_weight_kg === null
            ? null
            : String(patch.conditions.max_weight_kg),
      };
    }
    if (patch.actions) {
      rule.actions = {
        warehouse_id: patch.actions.warehouse_id ?? rule.actions.warehouse_id ?? null,
        carrier_code: patch.actions.carrier_code ?? null,
        service_code: patch.actions.service_code ?? null,
        add_tags: patch.actions.add_tags ?? [],
        hold_for_review: patch.actions.hold_for_review ?? false,
        hold_reason: patch.actions.hold_reason ?? null,
      };
    }
    rule.updated_at = new Date("2026-11-26T00:00:00.000Z").toISOString();
    return rule;
  }
  return bffFetch<RoutingRuleRead>(`${routingRulesPath()}/${ruleId}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

export async function deleteRoutingRule(ruleId: string): Promise<void> {
  if (shouldUseMockApi()) {
    const index = mockRoutingRules.findIndex((item) => item.id === ruleId);
    if (index >= 0) mockRoutingRules.splice(index, 1);
    return;
  }
  await bffFetch<void>(`${routingRulesPath()}/${ruleId}`, {
    method: "DELETE",
    parseJson: false,
  });
}

export async function fetchRoutingEvaluation(
  orderId: string,
): Promise<OrderRoutingEvaluationRead | null> {
  if (shouldUseMockApi()) {
    return mockEvaluations[orderId] ?? null;
  }
  try {
    return await bffFetch<OrderRoutingEvaluationRead>(routingEvaluationPath(orderId));
  } catch (error) {
    if (
      typeof error === "object" &&
      error !== null &&
      "status" in error &&
      (error as { status?: number }).status === 404
    ) {
      return null;
    }
    throw error;
  }
}

export async function evaluateOrderRouting(
  orderId: string,
): Promise<OrderRoutingEvaluationRead> {
  if (shouldUseMockApi()) {
    const evaluation = mockEvaluationFor(orderId);
    mockEvaluations[orderId] = evaluation;
    return evaluation;
  }
  return bffFetch<OrderRoutingEvaluationRead>(evaluateRoutingPath(orderId), {
    method: "POST",
  });
}

export async function releaseRoutingHold(
  orderId: string,
  reason: string,
): Promise<OrderRoutingEvaluationRead> {
  if (shouldUseMockApi()) {
    const evaluation = mockEvaluations[orderId] ?? mockEvaluationFor(orderId);
    const released: OrderRoutingEvaluationRead = {
      ...evaluation,
      is_held: false,
      hold_released_by: MOCK_RELEASED_BY,
      hold_released_at: "2026-11-26T03:00:00.000Z",
    };
    mockEvaluations[orderId] = released;
    return released;
  }
  return bffFetch<OrderRoutingEvaluationRead>(releaseHoldPath(orderId), {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

export async function fetchShippingLabels(
  orderId: string,
): Promise<CarrierShippingLabelRead[]> {
  if (shouldUseMockApi()) {
    return mockLabels[orderId] ?? [];
  }
  return bffFetch<CarrierShippingLabelRead[]>(shippingLabelsPath(orderId));
}

export async function bookShippingLabel(
  orderId: string,
  payload: ShippingLabelBookRequest,
): Promise<CarrierShippingLabelRead> {
  if (shouldUseMockApi()) {
    const evaluation = mockEvaluations[orderId] ?? mockEvaluationFor(orderId);
    if (evaluation.is_held && !payload.override_hold) {
      const error = new Error(
        `Routing evaluation held order: ${evaluation.hold_reason ?? "anti-loss hold"}`,
      ) as Error & { status?: number; code?: string };
      error.status = 409;
      error.code = "ERR_ORDER_HELD_FOR_REVIEW";
      throw error;
    }
    const carrier = payload.carrier_code ?? "sandbox_carrier";
    const existing = mockLabels[orderId] ?? [];
    const seed = `${orderId}:${carrier}:${existing.length}`;
    const waybill = `SBX${hashDigits(seed, 12)}`;
    const label: CarrierShippingLabelRead = {
      id: `label-${orderId}-${existing.length}`,
      tenant_id: MOCK_TENANT_ID,
      order_id: orderId,
      shipment_id: null,
      carrier_code: carrier,
      service_code: payload.service_code ?? "Sandbox-Standard",
      tracking_number: `${waybill}CN`,
      waybill_number: waybill,
      label_format: "pdf_4x6",
      label_r2_key: null,
      mode: payload.mode ?? "sandbox",
      shipping_cost_estimate: "12.50",
      currency: payload.currency ?? "USD",
      status: "created",
      created_at: "2026-11-26T03:05:00.000Z",
    };
    mockLabels[orderId] = [label, ...existing];
    return label;
  }
  return bffFetch<CarrierShippingLabelRead>(shippingLabelBookPath(orderId), {
    method: "POST",
    body: JSON.stringify(payload),
  });
}
