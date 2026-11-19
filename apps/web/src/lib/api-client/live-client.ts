// Live API adapter: typed wrappers over the @hey-api/openapi-ts generated
// SDK (contracts/openapi.json). Every call goes through the
// Next.js BFF proxy (/api/bff -> FastAPI /api/v1/*) via the shared generated
// client configured in @/lib/api, so session cookies and the 401-refresh
// flow keep working exactly as bffFetch does.
//
// The generated contract types (ConversationRead, VariantRead, ...) are
// mapped here onto the UI-facing shapes the pages already consume
// (ConversationSummary, CatalogVariantApiItem, ...). Mappings are explicit
// and documented where the contract is narrower than the UI used to assume:
// nullable strings get safe defaults, contract-absent UI fields are derived
// from `attributes`/`metadata_json` or defaulted, never invented.
//
// Mock mode never reaches this file: data-source.ts branches on
// shouldUseMockApi() first for fixture-backed reads.
import { getBffClient, bffFetch } from "@/lib/api";
import { BffApiError, type ProblemDetail } from "@/lib/contracts";
import type {
  AiCapability,
  AutonomyLevel,
  ChannelStatus,
  ProposalStatus,
  ProposalType,
  ChannelType,
  ConversationStatus,
  FulfillmentStatus,
  Incoterm,
  InventoryEventType,
  MessageContentType,
  MessageDirection,
  MessageSenderType,
  OnboardingStage,
  OpportunityStage,
  OrderMode,
  PaginatedResponse,
  PaymentStatus,
  QuoteStatus,
} from "@/lib/contracts";
import type {
  AssistantChatRequest,
  AssistantChatResponse,
  CapabilityPolicyItem,
  CreditWalletRead,
  KbDocumentRead,
  ConversationRead,
  ConvertQuoteResponse,
  CustomerRead,
  FinanceOverviewRead,
  InventoryBalanceItemRead,
  InventoryLedgerRead,
  LiveSkuCheckRead,
  MessageRead,
  MoqTierInputOutput,
  OnboardingStateRead,
  OpportunityRead,
  OrderRead,
  QuoteItemRead,
  QuotePdfResponse,
  QuoteRead,
  ShipmentRead,
  VariantMappingWithLabelRead,
  VariantRead,
} from "./types.gen";
import {
  acceptQuoteApiV1B2bQuotesQuoteIdAcceptPost,
  approveProposalApiV1AiProposalsProposalIdApprovePost,
  assistantChatApiV1AiAssistantChatPost,
  deactivateKbDocumentApiV1AiKbDocumentsDocIdDelete,
  editApproveProposalApiV1AiProposalsProposalIdEditApprovePost,
  getBillingWalletApiV1BillingWalletGet,
  getCapabilitiesApiV1AiCapabilitiesGet,
  listKbDocumentsApiV1AiKbDocumentsGet,
  listProposalsApiV1AiProposalsGet,
  listWarehousesApiV1OrdersWarehousesGet,
  rejectProposalApiV1AiProposalsProposalIdRejectPost,
  translateTextApiV1AiTranslatePost,
  updateCapabilitiesApiV1AiCapabilitiesPatch,
  uploadKbDocumentApiV1AiKbDocumentsPost,
  advanceOnboardingStageApiV1OnboardingAdvancePost,
  completeWhatsappEmbeddedSignupApiV1ChannelsWhatsappEmbeddedSignupPost,
  convertQuoteToOrderApiV1B2bQuotesQuoteIdConvertToOrderPost,
  generateQuotePdfApiV1B2bQuotesQuoteIdPdfPost,
  getCustomerApiV1CustomersCustomerIdGet,
  getFinanceOverviewApiV1FinanceOverviewGet,
  getOnboardingStateApiV1OnboardingStateGet,
  getOrderApiV1OrdersOrderIdGet,
  listConversationMessagesApiV1InboxConversationsConversationIdMessagesGet,
  listInboxConversationsApiV1InboxConversationsGet,
  listInventoryBalancesApiV1OrdersInventoryBalancesGet,
  listInventoryLedgerApiV1OrdersInventoryLedgerGet,
  listOpportunitiesApiV1B2bOpportunitiesGet,
  listOrdersApiV1OrdersGet,
  listQuotesApiV1B2bQuotesGet,
  listVariantMappingsApiV1CatalogVariantsVariantIdMappingsGet,
  listVariantsApiV1CatalogVariantsGet,
  liveCheckSkuApiV1CatalogLiveCheckGet,
  mergeCustomersEndpointApiV1CustomersMergePost,
  postConversationMessageApiV1InboxConversationsConversationIdMessagesPost,
  refreshShipmentStatusApiV1OrdersOrderIdShipmentsShipmentIdRefreshTrackingPost,
  replayDeadLetterEventApiV1ChannelsDlqEventIdReplayPost,
  repriceQuoteVersionApiV1B2bQuotesQuoteIdRepriceVersionPost,
  shipOrderApiV1OrdersOrderIdShipPost,
  splitCustomersEndpointApiV1CustomersSplitPost,
  updateOnboardingPresetApiV1OnboardingPresetPatch,
  updateOpportunityStageApiV1B2bOpportunitiesOpportunityIdPatch,
  updateSkuMappingApiV1CatalogMappingsMappingIdPut,
  updateTenantSettingsApiV1TenantsSettingsPatch,
  updateVariantApiV1CatalogVariantsVariantIdPatch,
  verifyPaymentRecordApiV1FinancePaymentsPaymentIdVerifyPost,
} from "./sdk.gen";
import type { AiProposalItem } from "@/components/ai/proposal-review-card";
import type { ConversationSummary } from "@/components/inbox/conversation-list";
import type { InboxMessage } from "@/components/inbox/message-thread";

// ---------------------------------------------------------------------------
// UI-facing API item types (unchanged from the Wave 2 hand stub; they are
// the shapes the pages/components already consume, so mock and live stay
// identical at the UI boundary).
// ---------------------------------------------------------------------------

export interface MoqTierApiItem {
  min_qty: number;
  unit_price_usd: number;
}

export interface CatalogVariantApiItem {
  id: string;
  product_id: string;
  spu_code: string;
  category: string;
  hs_code?: string | null;
  certifications: string[];
  sku: string;
  attributes: Record<string, string>;
  cost_price_base?: string | null;
  base_currency: string;
  min_margin_floor_pct: string;
  retail_price_usd: string;
  b2b_moq_tiers: MoqTierApiItem[];
  lead_time_days: number;
}

export interface PlatformSkuMappingApiItem {
  id: string;
  variant_id: string;
  channel_account_id: string;
  channel_label?: string;
  external_product_id: string;
  external_sku: string;
  bundle_multiplier: number;
  sync_inventory_enabled: boolean;
}

export interface B2bOpportunityApiItem {
  id: string;
  title: string;
  company_name?: string | null;
  stage: OpportunityStage;
  expected_amount: string;
  currency: string;
}

export interface B2bQuoteItemApiItem {
  id: string;
  variant_id: string;
  sku_snapshot: string;
  quantity: number;
  moq_tier_applied?: number | null;
  unit_price_quote_ccy: string;
  line_total_quote_ccy: string;
}

export interface B2bQuoteApiItem {
  id: string;
  opportunity_id: string;
  quote_no: string;
  version: number;
  is_proforma_invoice: boolean;
  incoterm: Incoterm;
  port_of_loading?: string | null;
  port_of_destination?: string | null;
  payment_terms: string;
  base_currency: string;
  quote_currency: string;
  fx_rate_snapshot: string;
  fx_locked_until: string;
  freight_fee: string;
  insurance_fee: string;
  customs_duty_estimate: string;
  total_amount: string;
  pdf_r2_key?: string | null;
  download_url?: string | null;
  status: QuoteStatus;
  converted_order_id?: string | null;
  items: B2bQuoteItemApiItem[];
}

export interface OrderSummaryApiItem {
  id: string;
  order_no: string;
  order_mode: OrderMode;
  customer_name: string;
  fulfillment_status: FulfillmentStatus;
  payment_status: PaymentStatus;
  currency: string;
  total_amount: string;
  has_72h_stagnation_alert?: boolean;
  shipment_blocked_70_unpaid?: boolean;
}

export interface OrderShipmentApiItem {
  id: string;
  tracking_provider: string;
  tracking_number: string;
  carrier_code?: string | null;
  shipment_mode: string;
  standardized_status: string;
  sub_status?: string | null;
  is_stagnant_72h?: boolean;
  tracking_events: Array<{
    time_utc: string;
    location?: string;
    description: string;
  }>;
}

export interface OrderDetailApiItem {
  id: string;
  order_no: string;
  order_mode: string;
  fulfillment_status: string;
  payment_status: string;
  currency?: string;
  total_amount?: string;
  shipments: OrderShipmentApiItem[];
}

export interface InventoryBalanceApiItem {
  warehouse_id: string;
  variant_id: string;
  sku: string;
  on_hand_qty: number;
  reserved_qty: number;
}

export interface InventoryLedgerApiItem {
  id: number;
  variant_id: string;
  event_type: InventoryEventType;
  qty_delta_on_hand: number;
  qty_delta_reserved: number;
  reference_type: string;
  reference_id: string;
  idempotency_key: string;
  created_at: string;
}

export interface ReceivableMilestoneApiItem {
  id: string;
  order_id: string;
  order_no: string;
  order_mode: string;
  milestone_label: string;
  due_amount: string;
  paid_amount: string;
  currency: string;
  status: "open" | "partial" | "settled" | "overdue" | "void";
  shipment_blocked_70_unpaid?: boolean;
  pending_payment_id?: string | null;
  pending_tx_ref?: string | null;
}

export interface OrderProfitSnapshotApiItem {
  order_id: string;
  order_no: string;
  base_currency: string;
  gross_revenue_base: string;
  cogs_base: string;
  platform_commission_base: string;
  payment_gateway_fee_base: string;
  shipping_and_duty_base: string;
  channel_msg_cost_base: string;
  ad_attribution_cost_base: string;
  fx_gain_loss_base: string;
  net_contribution_margin_base: string;
}

export interface FinanceOverviewApiItem {
  summary: {
    total_gross_revenue_base: string;
    total_net_margin_base: string;
    avg_margin_pct: string;
    base_currency: string;
  };
  receivables: ReceivableMilestoneApiItem[];
  cvp_snapshots: OrderProfitSnapshotApiItem[];
}

export interface OnboardingStateApiItem {
  tenant_id: string;
  tenant_slug?: string;
  current_stage: OnboardingStage;
  preset_type: "hybrid_factory_b2b_b2c" | "dtc_brand" | "pure_b2b_inquiry";
  completed_steps?: string[];
  metadata_json?: Record<string, unknown>;
  base_currency?: string;
  iana_timezone?: string;
  inbound_forwarding_address?: string;
  dns_status?: {
    spf_verified: boolean;
    dkim_verified: boolean;
    return_path_verified: boolean;
  };
  site_key?: string;
  allowed_origins?: string[];
  coexistence_acknowledged?: boolean;
}

export interface ChannelAccountApiItem {
  id: string;
  channel_type: ChannelType;
  display_name: string;
  external_account_id: string;
  status: ChannelStatus;
  rate_limit_profile: { mps?: number; coexistence?: boolean };
}

export interface DlqEventApiItem {
  id: string;
  channel_type: ChannelType;
  external_event_id: string;
  error_message: string;
  retry_count: number;
  created_at: string;
}

export interface ConsentLogApiItem {
  id: string;
  customer_name: string;
  channel_type: string;
  consent_status: "opted_in" | "opted_out";
  proof_source: string;
  recorded_at: string;
}

export interface SuppressionLogApiItem {
  id: string;
  channel_type: string;
  normalized_address: string;
  reason: string;
  created_at: string;
}

// ---------------------------------------------------------------------------
// SDK result handling
// ---------------------------------------------------------------------------

function detailToString(detail: unknown, fallback: string): string {
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail)) {
    const msgs = detail
      .map((d) => {
        if (d && typeof d === "object") {
          const rec = d as Record<string, unknown>;
          return typeof rec.msg === "string" ? rec.msg : JSON.stringify(d);
        }
        return String(d);
      })
      .filter(Boolean);
    if (msgs.length > 0) return msgs.join("; ");
  }
  return fallback;
}

function toBffApiError(error: unknown, response: Response): BffApiError {
  const fallback: ProblemDetail = {
    type: "https://erp.local/errors/http-error",
    title: response.statusText || "Request failed",
    status: response.status,
    detail: `Request failed with status ${response.status}`,
    code: "ERR_API_FAILURE",
  };
  if (error && typeof error === "object") {
    const e = error as Partial<ProblemDetail> & { message?: string };
    return new BffApiError({
      type: typeof e.type === "string" ? e.type : fallback.type,
      title: typeof e.title === "string" ? e.title : fallback.title,
      status: response.status,
      detail: detailToString(
        (e as { detail?: unknown }).detail ?? e.message,
        fallback.detail,
      ),
      code: typeof e.code === "string" ? e.code : fallback.code,
    });
  }
  if (typeof error === "string" && error) {
    return new BffApiError({ ...fallback, detail: error });
  }
  return new BffApiError(fallback);
}

/** Unwrap a hey-api SDK result, throwing BffApiError (ProblemDetail) on any failure. */
export function unwrap<T>(result: {
  data: T | undefined;
  error: unknown;
  response: Response;
}): T {
  if (result.error !== undefined && result.error !== null) {
    throw toBffApiError(result.error, result.response);
  }
  if (!result.response.ok) {
    throw toBffApiError(result.error, result.response);
  }
  if (result.data === undefined) {
    throw new BffApiError({
      type: "https://erp.local/errors/empty-response",
      title: "Empty response",
      status: result.response.status,
      detail: "Upstream returned a successful status with no body.",
      code: "ERR_EMPTY_RESPONSE",
    });
  }
  return result.data;
}

/** Resolve the authenticated tenant's warehouse without guessing among multiple entries. */
async function resolveWarehouseId(): Promise<string> {
  const { default_warehouse_id, items } = unwrap(
    await listWarehousesApiV1OrdersWarehousesGet({ client: getBffClient() }),
  );
  if (default_warehouse_id) return default_warehouse_id;
  if (items.length === 1) return items[0].id;

  const ambiguous = items.length > 1;
  throw new BffApiError({
    type: "https://erp.local/errors/warehouse-selection",
    title: "No unique warehouse",
    status: 0,
    detail: ambiguous
      ? "Multiple warehouses are available. Set a default warehouse for this workspace before continuing."
      : "No warehouse is available. Add stock to a warehouse for this workspace before continuing.",
    code: ambiguous ? "ERR_WAREHOUSE_SELECTION_REQUIRED" : "ERR_NO_WAREHOUSE_CONFIGURED",
  });
}

// ---------------------------------------------------------------------------
// Mappers: generated contract type -> UI-facing type
// ---------------------------------------------------------------------------

function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/** UI channel keys: "all" means no filter; "whatsapp" is the Cloud API channel. */
export function mapChannelParam(channel: string): ChannelType | undefined {
  if (!channel || channel === "all") return undefined;
  if (channel === "whatsapp") return "whatsapp_cloud";
  return channel as ChannelType;
}

const INBOX_FILTERS = ["all", "unread", "pending_human", "sla_urgent"] as const;

export function mapConversation(c: ConversationRead): ConversationSummary {
  return {
    id: c.id,
    customer_id: c.customer_id,
    customer_name: c.customer_name ?? c.customer?.display_name ?? "",
    country_code: c.country_code,
    iana_timezone: c.iana_timezone,
    channel_account_id: c.channel_account_id,
    channel_type: c.channel_type as ChannelType,
    conversation_mode: c.conversation_mode,
    status: c.status as ConversationStatus,
    unread_count: c.unread_count,
    sla_due_at: c.sla_due_at,
    csw_expires_at: c.csw_expires_at,
    human_agent_window_expires_at: c.human_agent_window_expires_at,
    last_message_preview: c.last_message_preview ?? "",
    updated_at: c.updated_at,
  };
}

export function mapInboxMessage(m: MessageRead): InboxMessage {
  return {
    id: m.id,
    conversation_id: m.conversation_id,
    channel_type: m.channel_type as ChannelType,
    direction: m.direction as MessageDirection,
    sender_type: m.sender_type as MessageSenderType,
    content_type: m.content_type as MessageContentType,
    body_text: m.body_text ?? "",
    body_sanitized_html: m.body_sanitized_html,
    delivery_status: m.delivery_status,
    source_timestamp: m.source_timestamp,
  };
}

function mapMoqTier(t: MoqTierInputOutput): { min_qty: number; unit_price_usd: number } {
  return { min_qty: t.min_qty, unit_price_usd: Number(t.unit_price_usd) };
}

export function mapVariant(v: VariantRead): CatalogVariantApiItem {
  const attrs = v.attributes ?? {};
  const strAttr = (key: string): string | undefined =>
    typeof attrs[key] === "string" ? (attrs[key] as string) : undefined;
  const arrAttr = (key: string): string[] =>
    Array.isArray(attrs[key])
      ? (attrs[key] as unknown[]).filter((x): x is string => typeof x === "string")
      : [];
  const stringAttrs: Record<string, string> = {};
  for (const [key, value] of Object.entries(attrs)) {
    if (typeof value === "string") stringAttrs[key] = value;
  }
  return {
    id: v.id,
    product_id: v.product_id,
    // VariantRead has no first-class spu_code/category/hs_code/certifications;
    // tenants that need them store them in attributes (seeded this way).
    spu_code: strAttr("spu_code") ?? v.sku,
    category: strAttr("category") ?? "",
    hs_code: strAttr("hs_code") ?? null,
    certifications: arrAttr("certifications"),
    sku: v.sku,
    attributes: stringAttrs,
    cost_price_base: v.cost_price_base,
    base_currency: v.base_currency,
    min_margin_floor_pct: v.min_margin_floor_pct ?? "0.00",
    retail_price_usd: v.retail_price_usd,
    b2b_moq_tiers: v.b2b_moq_tiers.map(mapMoqTier),
    lead_time_days: v.lead_time_days,
  };
}

export function mapVariantMapping(
  m: VariantMappingWithLabelRead,
): PlatformSkuMappingApiItem {
  return {
    id: m.id,
    variant_id: m.variant_id,
    channel_account_id: m.channel_account_id,
    channel_label: m.channel_label ?? undefined,
    external_product_id: m.external_product_id,
    external_sku: m.external_sku,
    bundle_multiplier: m.bundle_multiplier,
    sync_inventory_enabled: m.sync_inventory_enabled,
  };
}

export function mapOpportunity(o: OpportunityRead): B2bOpportunityApiItem {
  return {
    id: o.id,
    title: o.title,
    company_name: o.company_name,
    stage: o.stage as OpportunityStage,
    expected_amount: o.expected_amount,
    currency: o.currency,
  };
}

export function mapQuote(q: QuoteRead): B2bQuoteApiItem {
  return {
    id: q.id,
    opportunity_id: q.opportunity_id,
    quote_no: q.quote_no,
    version: q.version,
    is_proforma_invoice: q.is_proforma_invoice,
    incoterm: q.incoterm as Incoterm,
    port_of_loading: q.port_of_loading,
    port_of_destination: q.port_of_destination,
    payment_terms: q.payment_terms,
    base_currency: q.base_currency,
    quote_currency: q.quote_currency,
    fx_rate_snapshot: q.fx_rate_snapshot,
    fx_locked_until: q.fx_locked_until,
    freight_fee: q.freight_fee,
    insurance_fee: q.insurance_fee,
    customs_duty_estimate: q.customs_duty_estimate,
    total_amount: q.total_amount,
    pdf_r2_key: q.pdf_r2_key ?? undefined,
    // QuoteRead carries only pdf_r2_key; the signed download_url comes
    // from POST /b2b/quotes/{id}/pdf (QuotePdfResponse).
    download_url: undefined,
    status: q.status as QuoteStatus,
    converted_order_id: q.converted_order_id ?? undefined,
    items: (q.items ?? []).map((item: QuoteItemRead) => ({
      id: item.id,
      variant_id: item.variant_id,
      sku_snapshot: item.sku_snapshot,
      quantity: item.quantity,
      moq_tier_applied: item.moq_tier_applied ?? undefined,
      unit_price_quote_ccy: item.unit_price_quote_ccy,
      line_total_quote_ccy: item.line_total_quote_ccy,
    })),
  };
}

export function mapTrackingEvent(ev: Record<string, unknown>): {
  time_utc: string;
  location?: string;
  description: string;
} {
  // 17TRACK latest_event dicts: time_utc (or time_iso), description, location.
  return {
    time_utc: asString(ev.time_utc ?? ev.time_iso),
    location: asString(ev.location) || undefined,
    description: asString(ev.description ?? ev.stage ?? ev.sub_status),
  };
}

export function mapShipment(s: ShipmentRead): OrderShipmentApiItem {
  return {
    id: s.id,
    tracking_provider: s.tracking_provider,
    tracking_number: s.tracking_number,
    carrier_code: s.carrier_code,
    shipment_mode: s.shipment_mode,
    standardized_status: s.standardized_status,
    sub_status: s.sub_status,
    is_stagnant_72h: s.is_stagnant_72h ?? false,
    tracking_events: (s.tracking_events ?? []).map((ev) =>
      mapTrackingEvent(ev as Record<string, unknown>),
    ),
  };
}

export function mapOrderSummary(o: OrderRead): OrderSummaryApiItem {
  return {
    id: o.id,
    order_no: o.order_no,
    order_mode: o.order_mode as OrderMode,
    customer_name: o.customer_name ?? "",
    fulfillment_status: o.fulfillment_status as FulfillmentStatus,
    payment_status: o.payment_status as PaymentStatus,
    currency: o.currency,
    total_amount: o.total_amount,
    has_72h_stagnation_alert: o.has_72h_stagnation_alert ?? false,
    // OrderRead exposes no per-order 70%-block flag; the block lives on
    // finance receivables (shipment_blocked_70_unpaid) and is enforced
    // server-side on ship, so the list shows it as not blocked.
    shipment_blocked_70_unpaid: false,
  };
}

export function mapOrderDetail(o: OrderRead): OrderDetailApiItem {
  return {
    id: o.id,
    order_no: o.order_no,
    order_mode: o.order_mode,
    fulfillment_status: o.fulfillment_status,
    payment_status: o.payment_status,
    currency: o.currency,
    total_amount: o.total_amount,
    shipments: (o.shipments ?? []).map(mapShipment),
  };
}

export function mapInventoryBalance(
  b: InventoryBalanceItemRead,
): InventoryBalanceApiItem {
  return {
    warehouse_id: b.warehouse_id,
    variant_id: b.variant_id,
    sku: b.sku,
    on_hand_qty: b.on_hand_qty,
    reserved_qty: b.reserved_qty,
  };
}

export function mapInventoryLedger(
  r: InventoryLedgerRead,
): InventoryLedgerApiItem {
  return {
    id: r.id,
    variant_id: r.variant_id,
    // InventoryEventType is locked by the master plan contract; the backend
    // enum and the UI enum carry the same five values.
    event_type: r.event_type as InventoryEventType,
    qty_delta_on_hand: r.qty_delta_on_hand,
    qty_delta_reserved: r.qty_delta_reserved,
    reference_type: r.reference_type,
    reference_id: r.reference_id,
    idempotency_key: r.idempotency_key,
    created_at: r.created_at,
  };
}

const RECEIVABLE_STATUSES = ["open", "partial", "settled", "overdue", "void"] as const;

export function mapFinanceOverview(f: FinanceOverviewRead): FinanceOverviewApiItem {
  return {
    summary: {
      total_gross_revenue_base: f.summary.total_gross_revenue_base,
      total_net_margin_base: f.summary.total_net_margin_base,
      avg_margin_pct: f.summary.avg_margin_pct,
      base_currency: f.summary.base_currency,
    },
    receivables: f.receivables.map((r) => ({
      id: r.id,
      order_id: r.order_id,
      order_no: r.order_no,
      order_mode: r.order_mode,
      milestone_label: r.milestone_label,
      due_amount: r.due_amount,
      paid_amount: r.paid_amount,
      currency: r.currency,
      status: (
        RECEIVABLE_STATUSES.includes(r.status as (typeof RECEIVABLE_STATUSES)[number])
          ? r.status
          : "open"
      ) as ReceivableMilestoneApiItem["status"],
      shipment_blocked_70_unpaid: r.shipment_blocked_70_unpaid,
      pending_payment_id: r.pending_payment_id,
      pending_tx_ref: r.pending_tx_ref,
    })),
    cvp_snapshots: f.cvp_snapshots.map((s) => ({
      order_id: s.order_id,
      order_no: s.order_no,
      base_currency: s.base_currency,
      gross_revenue_base: s.gross_revenue_base,
      cogs_base: s.cogs_base,
      platform_commission_base: s.platform_commission_base,
      payment_gateway_fee_base: s.payment_gateway_fee_base,
      shipping_and_duty_base: s.shipping_and_duty_base,
      channel_msg_cost_base: s.channel_msg_cost_base,
      ad_attribution_cost_base: s.ad_attribution_cost_base,
      fx_gain_loss_base: s.fx_gain_loss_base,
      net_contribution_margin_base: s.net_contribution_margin_base,
    })),
  };
}

const PRESET_TYPES = ["hybrid_factory_b2b_b2c", "dtc_brand", "pure_b2b_inquiry"] as const;

export function mapOnboardingState(s: OnboardingStateRead): OnboardingStateApiItem {
  // OnboardingStateRead only carries metadata_json for the extra fields the
  // wizard reads (base_currency, dns_status, site_key, allowed_origins...);
  // the backend merges advance() step_metadata into it, so read them there.
  const meta = (s.metadata_json ?? {}) as Record<string, unknown>;
  const metaStr = (key: string): string | undefined =>
    typeof meta[key] === "string" ? (meta[key] as string) : undefined;
  const metaBool = (key: string): boolean | undefined =>
    typeof meta[key] === "boolean" ? (meta[key] as boolean) : undefined;
  const metaStrArray = (key: string): string[] | undefined =>
    Array.isArray(meta[key]) &&
    (meta[key] as unknown[]).every((x) => typeof x === "string")
      ? (meta[key] as string[])
      : undefined;
  const dnsRaw = meta.dns_status as Record<string, unknown> | undefined;
  const dnsStatus =
    dnsRaw && typeof dnsRaw === "object"
      ? {
          spf_verified: dnsRaw.spf_verified === true,
          dkim_verified: dnsRaw.dkim_verified === true,
          return_path_verified: dnsRaw.return_path_verified === true,
        }
      : undefined;
  return {
    tenant_id: s.tenant_id,
    tenant_slug: metaStr("tenant_slug"),
    current_stage: s.current_stage as OnboardingStage,
    preset_type: (
      PRESET_TYPES.includes(s.preset_type as (typeof PRESET_TYPES)[number])
        ? s.preset_type
        : "hybrid_factory_b2b_b2c"
    ) as OnboardingStateApiItem["preset_type"],
    completed_steps: s.completed_steps,
    metadata_json: s.metadata_json,
    base_currency: metaStr("base_currency"),
    iana_timezone: metaStr("iana_timezone"),
    inbound_forwarding_address: metaStr("inbound_forwarding_address"),
    dns_status: dnsStatus,
    site_key: metaStr("site_key"),
    allowed_origins: metaStrArray("allowed_origins"),
    coexistence_acknowledged: metaBool("coexistence_acknowledged"),
  };
}

// ---------------------------------------------------------------------------
// Live fetchers (generated SDK, BFF-proxied). Same names/signatures as the
// Wave 2 hand helpers where one existed, so data-source.ts only changes its
// import source.
// ---------------------------------------------------------------------------

export async function getConversations(
  channel: string,
  filter: string,
  query: string,
): Promise<PaginatedResponse<ConversationSummary>> {
  const data = unwrap(
    await listInboxConversationsApiV1InboxConversationsGet({
      client: getBffClient(),
      query: {
        filter: (
          INBOX_FILTERS.includes(filter as (typeof INBOX_FILTERS)[number])
            ? filter
            : "all"
        ) as (typeof INBOX_FILTERS)[number],
        channel: mapChannelParam(channel),
        q: query || undefined,
        limit: 25,
        offset: 0,
      },
    }),
  );
  return {
    items: data.items.map(mapConversation),
    total: data.total,
    limit: data.limit,
    offset: data.offset,
    has_more: data.has_more,
  };
}

export async function getConversationMessages(
  conversationId: string,
): Promise<PaginatedResponse<InboxMessage>> {
  const data = unwrap(
    await listConversationMessagesApiV1InboxConversationsConversationIdMessagesGet({
      client: getBffClient(),
      path: { conversation_id: conversationId },
      query: { limit: 50, offset: 0 },
    }),
  );
  return {
    items: data.items.map(mapInboxMessage),
    total: data.total,
    limit: data.limit,
    offset: data.offset,
    has_more: data.has_more,
  };
}

export async function sendConversationMessage(
  conversationId: string,
  body: {
    direction: "outbound" | "internal_note";
    content_type: MessageContentType;
    body_text: string;
    target_language?: string;
    translate_before_send?: boolean;
  },
) {
  return unwrap(
    await postConversationMessageApiV1InboxConversationsConversationIdMessagesPost({
      client: getBffClient(),
      path: { conversation_id: conversationId },
      body: {
        direction: body.direction,
        content_type: body.content_type,
        body_text: body.body_text,
        target_language: body.target_language ?? null,
        translate_before_send: body.translate_before_send ?? false,
      },
    }),
  );
}

export async function getCatalogVariants(
  q = "",
  limit = 50,
  offset = 0,
): Promise<PaginatedResponse<CatalogVariantApiItem>> {
  const data = unwrap(
    await listVariantsApiV1CatalogVariantsGet({
      client: getBffClient(),
      query: { q: q || undefined, limit, offset },
    }),
  );
  return {
    items: data.items.map(mapVariant),
    total: data.total,
    limit: data.limit,
    offset: data.offset,
    has_more: data.has_more,
  };
}

export async function getVariantMappings(variantId: string): Promise<{
  variant: CatalogVariantApiItem;
  mappings: PlatformSkuMappingApiItem[];
}> {
  const bundle = unwrap(
    await listVariantMappingsApiV1CatalogVariantsVariantIdMappingsGet({
      client: getBffClient(),
      path: { variant_id: variantId },
    }),
  );
  return {
    variant: mapVariant(bundle.variant),
    mappings: bundle.mappings.map(mapVariantMapping),
  };
}

export async function updateVariant(
  variantId: string,
  body: { b2b_moq_tiers: MoqTierApiItem[] },
) {
  return unwrap(
    await updateVariantApiV1CatalogVariantsVariantIdPatch({
      client: getBffClient(),
      path: { variant_id: variantId },
      body: {
        b2b_moq_tiers: body.b2b_moq_tiers.map((t) => ({
          min_qty: t.min_qty,
          unit_price_usd: t.unit_price_usd,
        })),
      },
    }),
  );
}

export async function updateSkuMapping(
  mappingId: string,
  body: {
    external_sku?: string;
    bundle_multiplier?: number;
    sync_inventory_enabled?: boolean;
  },
) {
  return unwrap(
    await updateSkuMappingApiV1CatalogMappingsMappingIdPut({
      client: getBffClient(),
      path: { mapping_id: mappingId },
      body: {
        external_sku: body.external_sku ?? null,
        bundle_multiplier: body.bundle_multiplier ?? null,
        sync_inventory_enabled: body.sync_inventory_enabled ?? null,
      },
    }),
  );
}

export async function liveCheckSku(
  sku: string,
  qty: number,
  incoterm: string,
): Promise<LiveSkuCheckRead> {
  return unwrap(
    await liveCheckSkuApiV1CatalogLiveCheckGet({
      client: getBffClient(),
      query: { sku, qty, incoterm },
    }),
  );
}

export async function getB2bOpportunities(
  limit = 50,
  offset = 0,
): Promise<PaginatedResponse<B2bOpportunityApiItem>> {
  const data = unwrap(
    await listOpportunitiesApiV1B2bOpportunitiesGet({
      client: getBffClient(),
      query: { limit, offset },
    }),
  );
  return {
    items: data.items.map(mapOpportunity),
    total: data.total,
    limit: data.limit,
    offset: data.offset,
    has_more: data.has_more,
  };
}

export async function updateOpportunityStage(id: string, stage: OpportunityStage) {
  return unwrap(
    await updateOpportunityStageApiV1B2bOpportunitiesOpportunityIdPatch({
      client: getBffClient(),
      path: { opportunity_id: id },
      body: { stage },
    }),
  );
}

export async function getB2bQuotes(
  opportunityId = "",
  limit = 20,
  offset = 0,
): Promise<PaginatedResponse<B2bQuoteApiItem>> {
  const data = unwrap(
    await listQuotesApiV1B2bQuotesGet({
      client: getBffClient(),
      query: {
        opportunity_id: opportunityId || undefined,
        limit,
        offset,
      },
    }),
  );
  return {
    items: data.items.map(mapQuote),
    total: data.total,
    limit: data.limit,
    offset: data.offset,
    has_more: data.has_more,
  };
}

export async function acceptQuote(quoteId: string) {
  return unwrap(
    await acceptQuoteApiV1B2bQuotesQuoteIdAcceptPost({
      client: getBffClient(),
      path: { quote_id: quoteId },
    }),
  );
}

export async function convertQuoteToOrder(
  quoteId: string,
  opts: {
    warehouseId?: string;
    depositRatio?: number;
    orderMode?: "b2b_wholesale" | "b2b_sample";
  } = {},
): Promise<ConvertQuoteResponse> {
  return unwrap(
    await convertQuoteToOrderApiV1B2bQuotesQuoteIdConvertToOrderPost({
      client: getBffClient(),
      path: { quote_id: quoteId },
      body: {
        warehouse_id: opts.warehouseId ?? await resolveWarehouseId(),
        deposit_ratio: opts.depositRatio,
        order_mode: opts.orderMode,
      },
    }),
  );
}

export async function generateQuotePdf(quoteId: string): Promise<QuotePdfResponse> {
  return unwrap(
    await generateQuotePdfApiV1B2bQuotesQuoteIdPdfPost({
      client: getBffClient(),
      path: { quote_id: quoteId },
    }),
  );
}

export async function repriceQuoteVersion(
  quoteId: string,
  fxRateSnapshot: string,
  fxLockedUntil: string,
) {
  return unwrap(
    await repriceQuoteVersionApiV1B2bQuotesQuoteIdRepriceVersionPost({
      client: getBffClient(),
      path: { quote_id: quoteId },
      body: {
        fx_rate_snapshot: fxRateSnapshot,
        fx_locked_until: fxLockedUntil,
      },
    }),
  );
}

export async function getOrders(
  orderMode = "",
  limit = 20,
  offset = 0,
): Promise<PaginatedResponse<OrderSummaryApiItem>> {
  const data = unwrap(
    await listOrdersApiV1OrdersGet({
      client: getBffClient(),
      query: {
        order_mode: (orderMode || undefined) as OrderMode | undefined,
        limit,
        offset,
      },
    }),
  );
  return {
    items: data.items.map(mapOrderSummary),
    total: data.total,
    limit: data.limit,
    offset: data.offset,
    has_more: data.has_more,
  };
}

export async function getOrderDetail(orderId: string): Promise<OrderDetailApiItem> {
  const order = unwrap(
    await getOrderApiV1OrdersOrderIdGet({
      client: getBffClient(),
      path: { order_id: orderId },
    }),
  );
  return mapOrderDetail(order);
}

export async function shipOrder(
  orderId: string,
  opts: {
    warehouseId?: string;
    trackingNumber: string;
    carrierCode?: string;
    shipmentMode?: string;
  },
) {
  return unwrap(
    await shipOrderApiV1OrdersOrderIdShipPost({
      client: getBffClient(),
      path: { order_id: orderId },
      body: {
        warehouse_id: opts.warehouseId ?? await resolveWarehouseId(),
        tracking_number: opts.trackingNumber,
        carrier_code: opts.carrierCode ?? "DHL",
        shipment_mode: opts.shipmentMode ?? "parcel",
      },
    }),
  );
}

export async function refreshShipmentTracking(orderId: string, shipmentId: string) {
  return unwrap(
    await refreshShipmentStatusApiV1OrdersOrderIdShipmentsShipmentIdRefreshTrackingPost(
      {
        client: getBffClient(),
        path: { order_id: orderId, shipment_id: shipmentId },
        body: { cache_level: 0 },
      },
    ),
  );
}

export async function getInventoryBalances(
  variantId = "",
  limit = 20,
  offset = 0,
): Promise<PaginatedResponse<InventoryBalanceApiItem>> {
  const data = unwrap(
    await listInventoryBalancesApiV1OrdersInventoryBalancesGet({
      client: getBffClient(),
      query: { variant_id: variantId || undefined, limit, offset },
    }),
  );
  return {
    items: data.items.map(mapInventoryBalance),
    total: data.total,
    limit: data.limit,
    offset: data.offset,
    has_more: data.has_more,
  };
}

export async function getInventoryLedger(
  variantId = "",
  limit = 20,
  offset = 0,
): Promise<PaginatedResponse<InventoryLedgerApiItem>> {
  const data = unwrap(
    await listInventoryLedgerApiV1OrdersInventoryLedgerGet({
      client: getBffClient(),
      query: { variant_id: variantId || undefined, limit, offset },
    }),
  );
  return {
    items: data.items.map(mapInventoryLedger),
    total: data.total,
    limit: data.limit,
    offset: data.offset,
    has_more: data.has_more,
  };
}

export async function getFinanceOverview(): Promise<FinanceOverviewApiItem> {
  const overview = unwrap(
    await getFinanceOverviewApiV1FinanceOverviewGet({
      client: getBffClient(),
    }),
  );
  return mapFinanceOverview(overview);
}

export async function verifyPayment(paymentId: string) {
  return unwrap(
    await verifyPaymentRecordApiV1FinancePaymentsPaymentIdVerifyPost({
      client: getBffClient(),
      path: { payment_id: paymentId },
    }),
  );
}

// Receivable payment upload is multipart FormData; the generated SDK call
// would need the same body, so it stays on the shared bffFetch transport
// (still BFF-proxied to the real contract endpoint).
export async function submitReceivablePayment(
  receivableId: string,
  form: FormData,
): Promise<unknown> {
  return bffFetch<unknown>(`/v1/finance/receivables/${receivableId}/payments`, {
    method: "POST",
    body: form,
  });
}

export async function getOnboardingState(): Promise<OnboardingStateApiItem> {
  const state = unwrap(
    await getOnboardingStateApiV1OnboardingStateGet({
      client: getBffClient(),
    }),
  );
  return mapOnboardingState(state);
}

export async function updateOnboardingPreset(
  presetType: OnboardingStateApiItem["preset_type"],
): Promise<OnboardingStateApiItem> {
  const state = unwrap(
    await updateOnboardingPresetApiV1OnboardingPresetPatch({
      client: getBffClient(),
      body: { preset_type: presetType },
    }),
  );
  return mapOnboardingState(state);
}

export async function updateTenantSettings(body: {
  name?: string;
  base_currency?: string;
  timezone?: string;
}) {
  return unwrap(
    await updateTenantSettingsApiV1TenantsSettingsPatch({
      client: getBffClient(),
      body: {
        name: body.name ?? null,
        base_currency: body.base_currency ?? null,
        timezone: body.timezone ?? null,
      },
    }),
  );
}

export async function advanceOnboarding(
  targetStage: OnboardingStage,
  stepMetadata?: Record<string, unknown>,
): Promise<OnboardingStateApiItem> {
  const state = unwrap(
    await advanceOnboardingStageApiV1OnboardingAdvancePost({
      client: getBffClient(),
      body: {
        target_stage: targetStage,
        step_metadata: stepMetadata,
      },
    }),
  );
  return mapOnboardingState(state);
}

export async function replayDlqEvent(eventId: string) {
  return unwrap(
    await replayDeadLetterEventApiV1ChannelsDlqEventIdReplayPost({
      client: getBffClient(),
      path: { event_id: eventId },
    }),
  );
}

export async function completeWhatsappEmbeddedSignup(body: {
  code: string;
  waba_id: string;
  phone_number_id: string;
  is_coexistence?: boolean;
  display_name?: string;
}) {
  return unwrap(
    await completeWhatsappEmbeddedSignupApiV1ChannelsWhatsappEmbeddedSignupPost({
      client: getBffClient(),
      body: {
        code: body.code,
        waba_id: body.waba_id,
        phone_number_id: body.phone_number_id,
        is_coexistence: body.is_coexistence,
        display_name: body.display_name,
      },
    }),
  );
}

export async function mergeCustomers(
  sourceCustomerId: string,
  targetCustomerId: string,
  reason: string,
) {
  return unwrap(
    await mergeCustomersEndpointApiV1CustomersMergePost({
      client: getBffClient(),
      body: {
        source_customer_id: sourceCustomerId,
        target_customer_id: targetCustomerId,
        reason,
      },
    }),
  );
}

export async function splitCustomers(mergeEventId: string, reason: string) {
  return unwrap(
    await splitCustomersEndpointApiV1CustomersSplitPost({
      client: getBffClient(),
      body: { merge_event_id: mergeEventId, reason },
    }),
  );
}

export async function getCustomer(customerId: string): Promise<CustomerRead> {
  return unwrap(
    await getCustomerApiV1CustomersCustomerIdGet({
      client: getBffClient(),
      path: { customer_id: customerId },
    }),
  );
}

// Plan 04: generated requests with explicit adapters for UI-only presentation shapes.
export type CapabilityConfigItem = Pick<CapabilityPolicyItem,
  "capability" | "configured_autonomy" | "max_allowed_autonomy">;
export type KbDocumentItem = Pick<KbDocumentRead, "id" | "title" | "chunk_count" | "status" | "created_at">;
export type BillingWalletResponse = Pick<CreditWalletRead,
  "plan_tier" | "included_credits_remaining" | "topup_credits_balance" | "auto_downgrade_to_l0">;

function mapCitations(values: Array<Record<string, unknown>> = []): AiProposalItem["citations"] {
  return values.flatMap(({ doc_id, chunk_id, score }) =>
    typeof doc_id === "string" && typeof chunk_id === "string" && typeof score === "number"
      ? [{ doc_id, chunk_id, score }]
      : [],
  );
}

export type AssistantChatResult = Pick<AssistantChatResponse,
  "reply_text" | "should_abstain" | "degraded_to_l0"> & {
    citations: AiProposalItem["citations"];
    tool_calls: Array<{ tool_call_id?: string; tool_name?: string }>;
};

export async function listProposals(
  status: ProposalStatus,
  typeFilter: "" | ProposalType,
): Promise<PaginatedResponse<AiProposalItem>> {
  const data = unwrap(await listProposalsApiV1AiProposalsGet({
    client: getBffClient(),
    query: { status, limit: 20, offset: 0, proposal_type: typeFilter || undefined },
  }));
  return {
    ...data,
    items: data.items.map((proposal) => ({
      ...proposal,
      citations: mapCitations(proposal.citations),
      confidence_score: Number(proposal.confidence_score),
      estimated_margin_pct: proposal.estimated_margin_pct === null
        ? null : Number(proposal.estimated_margin_pct),
      source_tool_call_id: asString(proposal.proposed_payload.source_tool_call_id) || null,
    })),
  };
}

export async function fetchPendingProposalsCount(): Promise<{ total: number }> {
  const { total } = unwrap(await listProposalsApiV1AiProposalsGet({
    client: getBffClient(), query: { status: "pending", limit: 1, offset: 0 },
  }));
  return { total };
}

export async function approveProposal(id: string) {
  return unwrap(await approveProposalApiV1AiProposalsProposalIdApprovePost({
    client: getBffClient(), path: { proposal_id: id },
  }));
}

export async function editApproveProposal(id: string, editedPayload: Record<string, unknown>) {
  return unwrap(await editApproveProposalApiV1AiProposalsProposalIdEditApprovePost({
    client: getBffClient(), path: { proposal_id: id }, body: { edited_payload: editedPayload },
  }));
}

export async function rejectProposal(id: string, reason: string) {
  return unwrap(await rejectProposalApiV1AiProposalsProposalIdRejectPost({
    client: getBffClient(), path: { proposal_id: id }, body: { reason },
  }));
}

export async function fetchCapabilities() {
  return unwrap(await getCapabilitiesApiV1AiCapabilitiesGet({ client: getBffClient() }));
}

export async function updateCapability(capability: AiCapability, autonomy: AutonomyLevel) {
  return unwrap(await updateCapabilitiesApiV1AiCapabilitiesPatch({
    client: getBffClient(), body: { settings: { [capability]: autonomy } },
  }));
}

export async function fetchWallet(): Promise<BillingWalletResponse> {
  return unwrap(await getBillingWalletApiV1BillingWalletGet({ client: getBffClient() }));
}

export async function fetchKbDocuments() {
  return unwrap(await listKbDocumentsApiV1AiKbDocumentsGet({ client: getBffClient() }));
}

export async function uploadKbDocument(form: FormData) {
  const file = form.get("file");
  const title = form.get("title");
  if (!(file instanceof Blob) || typeof title !== "string") {
    throw new TypeError("A knowledge base upload requires a file and title.");
  }
  return unwrap(await uploadKbDocumentApiV1AiKbDocumentsPost({
    client: getBffClient(),
    // hey-api 0.65 emits string for OpenAPI binary; its multipart serializer accepts File.
    body: { file: file as unknown as string, title },
  }));
}

export async function deleteKbDocument(id: string) {
  return unwrap(await deactivateKbDocumentApiV1AiKbDocumentsDocIdDelete({
    client: getBffClient(), path: { doc_id: id },
  }));
}

export async function assistantChat(body: AssistantChatRequest): Promise<AssistantChatResult> {
  const data = unwrap(await assistantChatApiV1AiAssistantChatPost({ client: getBffClient(), body }));
  return {
    ...data,
    citations: mapCitations(data.citations),
    tool_calls: (data.tool_calls ?? []).map(({ tool_call_id, tool_name }) => ({
      tool_call_id: asString(tool_call_id) || undefined,
      tool_name: asString(tool_name) || undefined,
    })),
  };
}

export async function translateText(text: string, targetLang: string) {
  return unwrap(await translateTextApiV1AiTranslatePost({
    client: getBffClient(), body: { text, target_lang: targetLang },
  }));
}
