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
  UserRole,
} from "@/lib/contracts";
import type {
  AiProposalRead,
  AssistantChatRequest,
  AssistantChatResponse,
  CapabilityPolicyItem,
  CreditWalletRead,
  KbDocumentRead,
  ConversationRead,
  ConvertQuoteResponse,
  CustomerRead,
  FinanceOverviewRead,
  FulfillmentPushResponse,
  InventoryBalanceItemRead,
  InventoryLedgerRead,
  LiveSkuCheckRead,
  MessageRead,
  MoqTierInputOutput,
  OnboardingStateRead,
  OpportunityRead,
  OrderRead,
  QuoteCreate,
  QuoteItemRead,
  QuotePdfResponse,
  QuoteRead,
  SandboxResult,
  ShipmentRead,
  VariantMappingWithLabelRead,
  VariantRead,
} from "./types.gen";
import type {
  ChannelAccountRead,
  InstagramConnectRequest,
  MessengerConnectRequest,
  TelegramConnectRequest,
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
  listChannelAccountsApiV1ChannelsAccountsGet,
  listChannelConsentsApiV1ChannelsConsentsGet,
  listChannelDlqApiV1ChannelsDlqGet,
  listChannelSuppressionsApiV1ChannelsSuppressionsGet,
  runSandboxTestApiV1OnboardingSandboxTestPost,
  verifyEmailDnsApiV1OnboardingEmailVerifyDnsPost,
  updateCapabilitiesApiV1AiCapabilitiesPatch,
  uploadKbDocumentApiV1AiKbDocumentsPost,
  advanceOnboardingStageApiV1OnboardingAdvancePost,
  completeWhatsappEmbeddedSignupApiV1ChannelsWhatsappEmbeddedSignupPost,
  connectInstagramApiV1ChannelsInstagramConnectPost,
  connectMessengerApiV1ChannelsMessengerConnectPost,
  connectTelegramApiV1ChannelsTelegramConnectPost,
  convertQuoteToOrderApiV1B2bQuotesQuoteIdConvertToOrderPost,
  createQuoteApiV1B2bQuotesPost,
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
  pushOrderFulfillmentEndpointApiV1OrdersOrderIdPushFulfillmentPost,
  refreshShipmentStatusApiV1OrdersOrderIdShipmentsShipmentIdRefreshTrackingPost,
  replayDeadLetterEventApiV1ChannelsDlqEventIdReplayPost,
  repriceQuoteVersionApiV1B2bQuotesQuoteIdRepriceVersionPost,
  sendQuoteApiV1B2bQuotesQuoteIdSendPost,
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
  safety_stock?: number;
  last_inventory_synced_qty?: number | null;
  last_inventory_synced_at?: string | null;
  last_sync_status?: string;
  last_sync_error?: string | null;
}

export interface B2bOpportunityApiItem {
  id: string;
  customer_id?: string;
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

export interface ExternalFulfillmentStatus {
  status: "synced" | "failed";
  channel?: string | null;
  tracking_number?: string | null;
  carrier_company?: string | null;
  external_fulfillment_id?: string | null;
  error_code?: string | null;
  error_detail?: string | null;
  synced_at?: string | null;
  failed_at?: string | null;
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
  channel_type?: string | null;
  channel_label?: string | null;
  external_order_id?: string | null;
  refunded_amount?: string | null;
  external_fulfillment?: ExternalFulfillmentStatus | null;
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
  channel_type?: string | null;
  channel_label?: string | null;
  external_order_id?: string | null;
  refunded_amount?: string | null;
  external_fulfillment?: ExternalFulfillmentStatus | null;
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

export type ChannelVisibilityScope = "shared" | "personal";

/**
 * 渠道账号：`ChannelAccountRead` 的 UI 投影。ownership 相关字段目前由后端
 * 增量返回（sdk.gen.ts 会在 Backend 合并后重新生成），因此这里全部声明为
 * 可选，并通过 `mapChannelAccount` 从原始响应里安全读取。
 */
export interface ChannelAccountApiItem {
  id: string;
  channel_type: ChannelType;
  display_name: string;
  external_account_id: string;
  status: ChannelStatus;
  rate_limit_profile: { mps?: number; coexistence?: boolean };
  owner_user_id?: string | null;
  owner_name?: string | null;
  owner_email?: string | null;
  visibility_scope?: ChannelVisibilityScope | null;
  bot_username?: string | null;
}

/** 工作区成员（业务员）账号，对应 GET /tenants/memberships。 */
export interface TeamMemberApiItem {
  membership_id: string;
  tenant_id: string;
  user_id: string;
  email: string;
  full_name: string;
  role: UserRole;
  is_active: boolean;
  created_at: string;
}

/** 契约里的名字（TeamMemberItem）与 UI 侧类型同义。 */
export type TeamMemberItem = TeamMemberApiItem;
/** 契约里的名字（ChannelAccountItem）与 UI 侧类型同义。 */
export type ChannelAccountItem = ChannelAccountApiItem;

export interface InviteTeamMemberPayload {
  email: string;
  full_name: string;
  password: string;
  role: UserRole;
}

export interface UpdateChannelOwnershipPayload {
  visibility_scope: ChannelVisibilityScope;
  owner_user_id?: string | null;
  reassign_open_conversations?: boolean;
}

export interface DisconnectChannelResponse {
  channel_account_id: string;
  status: "disconnected";
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

function optionalString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function optionalVisibilityScope(value: unknown): ChannelVisibilityScope | null {
  return value === "shared" || value === "personal" ? value : null;
}

/** `ChannelAccountRead` 加上后端增量下发的 ownership 字段（重新生成前先安全读取）。 */
type ChannelAccountRow = ChannelAccountRead &
  Partial<{
    owner_user_id: string | null;
    owner_name: string | null;
    owner_email: string | null;
    visibility_scope: ChannelVisibilityScope | null;
    bot_username: string | null;
  }>;

/** 渠道账号投影：ownership 字段缺省时统一回落为 null，UI 直接读不会拿到 undefined。 */
export function mapChannelAccount(row: ChannelAccountRow): ChannelAccountApiItem {
  const raw = row as unknown as Record<string, unknown>;
  return {
    id: row.id,
    channel_type: row.channel_type,
    display_name: row.display_name,
    external_account_id: row.external_account_id,
    status: row.status,
    rate_limit_profile: {
      mps: row.rate_limit_profile?.mps ?? undefined,
      coexistence: row.rate_limit_profile?.coexistence ?? undefined,
    },
    owner_user_id: optionalString(raw.owner_user_id),
    owner_name: optionalString(raw.owner_name),
    owner_email: optionalString(raw.owner_email),
    visibility_scope: optionalVisibilityScope(raw.visibility_scope),
    bot_username: optionalString(raw.bot_username),
  };
}

export function mapConversation(c: ConversationRead): ConversationSummary {
  // ConversationRead 尚未包含归属字段（后端增量下发），先按原始报文安全读取。
  const raw = c as unknown as Record<string, unknown>;
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
    assigned_user_id: optionalString(raw.assigned_user_id),
    assigned_user_name: optionalString(raw.assigned_user_name),
    channel_account_display_name: optionalString(raw.channel_account_display_name),
    channel_visibility_scope: optionalVisibilityScope(raw.channel_visibility_scope),
    unverified_prechat: c.unverified_prechat
      ? {
          email: c.unverified_prechat.email ?? null,
          whatsapp_phone: c.unverified_prechat.whatsapp_phone ?? null,
          marketing_opt_in: Boolean(c.unverified_prechat.marketing_opt_in),
        }
      : null,
  };
}

export function mapInboxMessage(m: MessageRead): InboxMessage {
  // Message-level translations are stored on the message's channel_metadata (the
  // contract has no dedicated translation fields), so surface them for the thread.
  const meta = (m.channel_metadata ?? {}) as Record<string, unknown>;
  const translations =
    meta.translations && typeof meta.translations === "object"
      ? (meta.translations as Record<string, unknown>)
      : {};
  const translation = (lang: string): string | undefined =>
    typeof translations[lang] === "string" ? (translations[lang] as string) : undefined;
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
    detected_language:
      typeof meta.detected_language === "string" ? meta.detected_language : undefined,
    translated_text_en: translation("en"),
    translated_text_zh: translation("zh"),
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
    safety_stock: m.safety_stock,
    last_inventory_synced_qty: m.last_inventory_synced_qty ?? null,
    last_inventory_synced_at: m.last_inventory_synced_at ?? null,
    last_sync_status: m.last_sync_status,
    last_sync_error: m.last_sync_error ?? null,
  };
}

export function mapOpportunity(o: OpportunityRead): B2bOpportunityApiItem {
  return {
    id: o.id,
    customer_id: o.customer_id,
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

function parseExternalFulfillment(
  meta: Record<string, unknown> | undefined,
): ExternalFulfillmentStatus | null {
  const raw = meta?.external_fulfillment;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const obj = raw as Record<string, unknown>;
  const status = obj.status === "synced" || obj.status === "failed" ? obj.status : null;
  if (!status) return null;
  return {
    status,
    channel: typeof obj.channel === "string" ? obj.channel : null,
    tracking_number: typeof obj.tracking_number === "string" ? obj.tracking_number : null,
    carrier_company: typeof obj.carrier_company === "string" ? obj.carrier_company : null,
    external_fulfillment_id:
      typeof obj.external_fulfillment_id === "string" ? obj.external_fulfillment_id : null,
    error_code: typeof obj.error_code === "string" ? obj.error_code : null,
    error_detail: typeof obj.error_detail === "string" ? obj.error_detail : null,
    synced_at: typeof obj.synced_at === "string" ? obj.synced_at : null,
    failed_at: typeof obj.failed_at === "string" ? obj.failed_at : null,
  };
}

function parseRefundedAmount(meta: Record<string, unknown> | undefined): string | null {
  const raw = meta?.refunded_amount;
  if (typeof raw === "string" && Number(raw) > 0) return raw;
  if (typeof raw === "number" && raw > 0) return raw.toFixed(2);
  return null;
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
    channel_type: o.channel_type ?? null,
    channel_label: o.channel_label ?? null,
    external_order_id: o.external_order_id ?? null,
    refunded_amount: parseRefundedAmount(o.channel_metadata),
    external_fulfillment: parseExternalFulfillment(o.channel_metadata),
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
    channel_type: o.channel_type ?? null,
    channel_label: o.channel_label ?? null,
    external_order_id: o.external_order_id ?? null,
    refunded_amount: parseRefundedAmount(o.channel_metadata),
    external_fulfillment: parseExternalFulfillment(o.channel_metadata),
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
    safety_stock?: number;
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
        safety_stock: body.safety_stock ?? null,
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

export async function createB2bQuote(payload: QuoteCreate): Promise<B2bQuoteApiItem> {
  const quote = unwrap(
    await createQuoteApiV1B2bQuotesPost({
      client: getBffClient(),
      body: payload,
    }),
  );
  return mapQuote(quote);
}

export async function sendQuote(quoteId: string): Promise<B2bQuoteApiItem> {
  const quote = unwrap(
    await sendQuoteApiV1B2bQuotesQuoteIdSendPost({
      client: getBffClient(),
      path: { quote_id: quoteId },
    }),
  );
  return mapQuote(quote);
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
    trackingProvider?: "manual" | "17track";
    autoResolveWarehouse?: boolean;
  },
) {
  const warehouseId = opts.autoResolveWarehouse
    ? opts.warehouseId ?? undefined
    : opts.warehouseId ?? (await resolveWarehouseId());
  return unwrap(
    await shipOrderApiV1OrdersOrderIdShipPost({
      client: getBffClient(),
      path: { order_id: orderId },
      body: {
        ...(warehouseId !== undefined ? { warehouse_id: warehouseId } : {}),
        tracking_number: opts.trackingNumber,
        carrier_code: opts.carrierCode ?? "DHL",
        shipment_mode: opts.shipmentMode ?? "parcel",
        ...(opts.trackingProvider ? { tracking_provider: opts.trackingProvider } : {}),
      },
    }),
  );
}

export async function pushOrderFulfillment(
  orderId: string,
): Promise<FulfillmentPushResponse> {
  return unwrap(
    await pushOrderFulfillmentEndpointApiV1OrdersOrderIdPushFulfillmentPost({
      client: getBffClient(),
      path: { order_id: orderId },
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

export type MessagingProviderKind = "messenger" | "instagram_dm" | "telegram";

export type MessengerConnectPayload = {
  page_id: string;
  page_access_token: string;
  display_name: string;
  visibility_scope?: ChannelVisibilityScope;
  owner_user_id?: string | null;
};

export type InstagramConnectPayload = {
  ig_account_id: string;
  access_token: string;
  display_name: string;
  visibility_scope?: ChannelVisibilityScope;
  owner_user_id?: string | null;
};

/**
 * Telegram 一处贴即绑：`display_name` 可选——留空时后端会用 `getMe`
 * 拉到的机器人名称回填，因此只贴 Bot Token 就能完成绑定。
 */
export type TelegramConnectPayload = {
  bot_token: string;
  display_name?: string;
  visibility_scope?: ChannelVisibilityScope;
  owner_user_id?: string | null;
};

/** Resend API Key 与通用 SMTP/IMAP 邮箱共用一张快捷绑定卡。 */
export type EmailConnectPayload = {
  email_address: string;
  display_name?: string | null;
  provider?: "resend" | "smtp_imap";
  api_key?: string | null;
  smtp_host?: string | null;
  smtp_port?: number | null;
  smtp_username?: string | null;
  smtp_password?: string | null;
  imap_host?: string | null;
  imap_port?: number | null;
  visibility_scope?: ChannelVisibilityScope;
  owner_user_id?: string | null;
};

export type MessagingConnectPayload =
  | MessengerConnectPayload
  | InstagramConnectPayload
  | TelegramConnectPayload;

export type MessagingConnectResult = {
  channel_account_id: string;
  external_account_id: string;
  display_name?: string | null;
  owner_user_id?: string | null;
  visibility_scope?: ChannelVisibilityScope;
  bot_username?: string | null;
};

/** 连接响应里后端增量下发的字段（sdk.gen.ts 重新生成前先安全读取）。 */
function mapConnectResult(
  result: { channel_account_id: string; external_account_id: string },
): MessagingConnectResult {
  const raw = result as unknown as Record<string, unknown>;
  return {
    channel_account_id: result.channel_account_id,
    external_account_id: result.external_account_id,
    display_name: optionalString(raw.display_name),
    owner_user_id: optionalString(raw.owner_user_id),
    visibility_scope: optionalVisibilityScope(raw.visibility_scope) ?? undefined,
    bot_username: optionalString(raw.bot_username),
  };
}

export async function connectMessengerChannel(
  body: MessengerConnectPayload,
): Promise<MessagingConnectResult> {
  return mapConnectResult(
    unwrap(
      await connectMessengerApiV1ChannelsMessengerConnectPost({
        client: getBffClient(),
        // ownership 字段比当前生成的请求体更新，合并后即可去掉这层断言。
        body: body as unknown as MessengerConnectRequest,
      }),
    ),
  );
}

export async function connectInstagramChannel(
  body: InstagramConnectPayload,
): Promise<MessagingConnectResult> {
  return mapConnectResult(
    unwrap(
      await connectInstagramApiV1ChannelsInstagramConnectPost({
        client: getBffClient(),
        body: body as unknown as InstagramConnectRequest,
      }),
    ),
  );
}

export async function connectTelegramChannel(
  body: TelegramConnectPayload,
): Promise<MessagingConnectResult> {
  return mapConnectResult(
    unwrap(
      await connectTelegramApiV1ChannelsTelegramConnectPost({
        client: getBffClient(),
        body: body as unknown as TelegramConnectRequest,
      }),
    ),
  );
}

/** 邮箱快捷绑定（Resend / SMTP-IMAP）：走 bffFetch，因为契约端点尚未生成 SDK。 */
export async function connectEmailChannel(
  body: EmailConnectPayload,
): Promise<MessagingConnectResult> {
  const raw = await bffFetch<Record<string, unknown>>("/v1/channels/email/connect", {
    method: "POST",
    body: JSON.stringify(body),
  });
  return mapConnectResult({
    ...raw,
    channel_account_id: String(raw.channel_account_id ?? ""),
    external_account_id: String(raw.external_account_id ?? ""),
  });
}

// ---------------------------------------------------------------------------
// 渠道应用市场（11 个渠道）：WhatsApp 直连/Embedded Signup、网页客服、独立站连接器。
// 契约端点尚未生成 SDK 的部分全部走 bffFetch，Backend 合并后无需改动调用方。
// ---------------------------------------------------------------------------

/** 渠道应用市场的 11 个渠道键（Telegram / 邮箱 / Meta 之外含 5 个建站连接器）。 */
export type MarketplaceChannelKey =
  | "whatsapp"
  | "telegram"
  | "messenger"
  | "instagram_dm"
  | "email"
  | "web_livechat"
  | "shopify"
  | "woocommerce"
  | "shopline"
  | "shoplazza"
  | "tiktok_shop";

export type WhatsAppDirectConnectPayload = {
  waba_id: string;
  phone_number_id: string;
  access_token: string;
  is_coexistence: boolean;
  display_name?: string | null;
  visibility_scope?: ChannelVisibilityScope;
  owner_user_id?: string | null;
};

export type WhatsAppEmbeddedSignupPayload = {
  code: string;
  waba_id: string;
  phone_number_id: string;
  is_coexistence: boolean;
  display_name: string;
  visibility_scope?: ChannelVisibilityScope;
  owner_user_id?: string | null;
};

export type WebLivechatConnectPayload = {
  site_domain: string;
  display_name?: string | null;
  visibility_scope?: ChannelVisibilityScope;
  owner_user_id?: string | null;
};

export type ShopifyCustomAppConnectPayload = {
  shop_domain: string;
  client_id: string;
  client_secret: string;
  access_token?: string | null;
};

export type WooCommerceConnectPayload = {
  store_url: string;
  consumer_key: string;
  consumer_secret: string;
  webhook_secret?: string | null;
  wp_username?: string | null;
  wp_app_password?: string | null;
};

export type ShoplineConnectPayload = {
  store_domain: string;
  access_token: string;
  app_secret: string;
};

export type ShoplazzaConnectPayload = {
  store_domain: string;
  access_token: string;
  client_secret: string;
};

export type TikTokShopConnectPayload = {
  shop_id: string;
  shop_cipher: string;
  app_key: string;
  app_secret: string;
  access_token: string;
  app_mode: "seller_byok";
};

/**
 * 一次接入请求：抽屉按渠道构造，数据层按 `channel` 分发到对应端点。
 * WhatsApp 有直连 Token 与 Meta Embedded Signup 两种模式。
 */
export type MarketplaceConnectRequest =
  | { channel: "whatsapp"; mode: "direct"; body: WhatsAppDirectConnectPayload }
  | { channel: "whatsapp"; mode: "embedded"; body: WhatsAppEmbeddedSignupPayload }
  | { channel: "telegram"; body: TelegramConnectPayload }
  | { channel: "messenger"; body: MessengerConnectPayload }
  | { channel: "instagram_dm"; body: InstagramConnectPayload }
  | { channel: "email"; body: EmailConnectPayload }
  | { channel: "web_livechat"; body: WebLivechatConnectPayload }
  | { channel: "shopify"; body: ShopifyCustomAppConnectPayload }
  | { channel: "woocommerce"; body: WooCommerceConnectPayload }
  | { channel: "shopline"; body: ShoplineConnectPayload }
  | { channel: "shoplazza"; body: ShoplazzaConnectPayload }
  | { channel: "tiktok_shop"; body: TikTokShopConnectPayload };

/**
 * 连接器的响应字段尚未进入契约：`channel_account_id` / `external_account_id`
 * 之外一律按可选读取，避免连接成功却因字段差异抛错。
 */
function mapConnectorConnectResult(raw: Record<string, unknown>): MessagingConnectResult {
  return mapConnectResult({
    ...raw,
    channel_account_id: String(raw.channel_account_id ?? raw.id ?? ""),
    external_account_id: String(raw.external_account_id ?? raw.site_key ?? raw.shop_domain ?? ""),
  });
}

async function postChannelConnect(
  path: string,
  body: Record<string, unknown>,
): Promise<MessagingConnectResult> {
  const raw = await bffFetch<Record<string, unknown>>(path, {
    method: "POST",
    body: JSON.stringify(body),
  });
  return mapConnectorConnectResult(raw);
}

/** WhatsApp Cloud API 直连 Token 快捷绑定（WABA + 手机号 + 永久令牌）。 */
export async function connectWhatsAppDirect(
  body: WhatsAppDirectConnectPayload,
): Promise<MessagingConnectResult> {
  return postChannelConnect("/v1/channels/whatsapp/connect", body);
}

/** WhatsApp Meta Embedded Signup 授权码换令牌。 */
export async function connectWhatsAppEmbedded(
  body: WhatsAppEmbeddedSignupPayload,
): Promise<MessagingConnectResult> {
  return postChannelConnect("/v1/channels/whatsapp/embedded-signup", body);
}

/** 官网网页客服：一个域名即生成可复制的嵌入脚本。 */
export async function connectWebLivechat(
  body: WebLivechatConnectPayload,
): Promise<MessagingConnectResult> {
  return postChannelConnect("/v1/channels/web-livechat/connect", body);
}

/** Shopify 自定义应用（Client ID / Secret，可选 Admin API Token）。 */
export async function connectShopifyCustomApp(
  body: ShopifyCustomAppConnectPayload,
): Promise<MessagingConnectResult> {
  return postChannelConnect("/v1/connectors/shopify/custom-app", body);
}

/** WooCommerce REST 凭证回调（Consumer Key / Secret）。 */
export async function connectWooCommerce(
  body: WooCommerceConnectPayload,
): Promise<MessagingConnectResult> {
  return postChannelConnect("/v1/connectors/woocommerce/auth-callback", body);
}

/** SHOPLINE 开放平台连接器。 */
export async function connectShopline(
  body: ShoplineConnectPayload,
): Promise<MessagingConnectResult> {
  return postChannelConnect("/v1/connectors/shopline/connect", body);
}

/** Shoplazza 开放平台连接器。 */
export async function connectShoplazza(
  body: ShoplazzaConnectPayload,
): Promise<MessagingConnectResult> {
  return postChannelConnect("/v1/connectors/shoplazza/connect", body);
}

/** TikTok Shop 卖家自备应用（seller_byok）连接器。 */
export async function connectTikTokShop(
  body: TikTokShopConnectPayload,
): Promise<MessagingConnectResult> {
  return postChannelConnect("/v1/connectors/tiktok/connect", body);
}

/** 按渠道键分发一次接入请求（抽屉只负责收集字段）。 */
export async function connectMarketplaceChannel(
  request: MarketplaceConnectRequest,
): Promise<MessagingConnectResult> {
  switch (request.channel) {
    case "whatsapp":
      return request.mode === "embedded"
        ? connectWhatsAppEmbedded(request.body)
        : connectWhatsAppDirect(request.body);
    case "telegram":
      return connectTelegramChannel(request.body);
    case "messenger":
      return connectMessengerChannel(request.body);
    case "instagram_dm":
      return connectInstagramChannel(request.body);
    case "email":
      return connectEmailChannel(request.body);
    case "web_livechat":
      return connectWebLivechat(request.body);
    case "shopify":
      return connectShopifyCustomApp(request.body);
    case "woocommerce":
      return connectWooCommerce(request.body);
    case "shopline":
      return connectShopline(request.body);
    case "shoplazza":
      return connectShoplazza(request.body);
    case "tiktok_shop":
      return connectTikTokShop(request.body);
  }
}

/** 切换归属：公司公海 ↔ 指定业务员个人专属（owner/admin）。 */
export async function updateChannelOwnership(
  accountId: string,
  body: UpdateChannelOwnershipPayload,
): Promise<ChannelAccountApiItem> {
  const row = await bffFetch<ChannelAccountRow>(
    `/v1/channels/accounts/${encodeURIComponent(accountId)}/ownership`,
    {
      method: "PATCH",
      body: JSON.stringify({
        visibility_scope: body.visibility_scope,
        owner_user_id: body.owner_user_id ?? null,
        reassign_open_conversations: body.reassign_open_conversations ?? false,
      }),
    },
  );
  return mapChannelAccount(row);
}

/** 断开渠道账号：owner/admin 任意，agent/manager 仅限本人个人渠道。 */
export async function disconnectChannelAccount(
  accountId: string,
): Promise<DisconnectChannelResponse> {
  return bffFetch<DisconnectChannelResponse>(
    `/v1/channels/accounts/${encodeURIComponent(accountId)}`,
    { method: "DELETE" },
  );
}

/** 团队成员（业务员账号）列表：owner/admin。 */
export async function fetchTeamMembers(
  limit = 100,
  offset = 0,
): Promise<PaginatedResponse<TeamMemberApiItem>> {
  return bffFetch<PaginatedResponse<TeamMemberApiItem>>(
    `/v1/tenants/memberships?limit=${limit}&offset=${offset}`,
  );
}

/** 新建业务员 / 团队成员：owner/admin。 */
export async function inviteTeamMember(
  body: InviteTeamMemberPayload,
): Promise<TeamMemberApiItem> {
  return bffFetch<TeamMemberApiItem>("/v1/tenants/memberships", {
    method: "POST",
    body: JSON.stringify(body),
  });
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

// Compose the drawer from tenant-scoped SDK reads instead of the legacy mock route.
export async function getInboxCustomerContext(customerId: string) {
  const [customer, orders, opportunities] = await Promise.all([
    getCustomer(customerId),
    listOrdersApiV1OrdersGet({ client: getBffClient(), query: { customer_id: customerId, limit: 5 } })
      .then(unwrap),
    listOpportunitiesApiV1B2bOpportunitiesGet({ client: getBffClient(), query: { limit: 50 } })
      .then(unwrap),
  ]);
  const quotePages = await Promise.all(opportunities.items
    .filter((opportunity) => opportunity.customer_id === customerId)
    .slice(0, 5).map((opportunity) => getB2bQuotes(opportunity.id, 5)));
  return {
    customer,
    identities: customer.identities ?? [],
    probable_duplicates: (customer.merge_proposals ?? [])
      .filter((proposal) => proposal.source_customer_id === customerId)
      .map((proposal) => ({
        candidate_customer_id: proposal.target_customer_id,
        display_name: proposal.target_display_name,
        matched_signal: proposal.reason,
        similarity_score: proposal.similarity_score,
      })),
    merge_events: [], // The customer contract does not expose merge history.
    recent_orders: orders.items.map((order) => {
      const shipment = order.shipments?.[0];
      return { ...order, tracking_number: shipment?.tracking_number,
        standardized_status: shipment?.standardized_status, sub_status: shipment?.sub_status };
    }),
    recent_quotes: quotePages.flatMap((page) => page.items).slice(0, 5),
  };
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
  "reply_text" | "should_abstain" | "degraded_to_l0" | "reason"> & {
    citations: AiProposalItem["citations"];
    // `data` is the deterministic SQL tool payload (e.g. the channel analytics
    // rollup from `get_live_channel_sales_analytics`); absent when a turn is
    // trace-only, so the key set stays { tool_call_id, tool_name } in that case.
    tool_calls: Array<{
      tool_call_id?: string;
      tool_name?: string;
      data?: Record<string, unknown>;
    }>;
};

/**
 * `AiProposalRead` → UI 提案卡片。工具接地 ID 由后端写在 `proposed_payload`
 * 里（契约没有独立字段），这里统一提升到 `source_tool_call_id` 供审核人看。
 */
export function mapProposal(proposal: AiProposalRead): AiProposalItem {
  return {
    ...proposal,
    citations: mapCitations(proposal.citations),
    confidence_score: Number(proposal.confidence_score),
    estimated_margin_pct: proposal.estimated_margin_pct === null
      ? null : Number(proposal.estimated_margin_pct),
    source_tool_call_id: asString(proposal.proposed_payload.source_tool_call_id) || null,
  };
}

export async function listProposals(
  status: ProposalStatus,
  typeFilter: "" | ProposalType,
): Promise<PaginatedResponse<AiProposalItem>> {
  const data = unwrap(await listProposalsApiV1AiProposalsGet({
    client: getBffClient(),
    query: { status, limit: 20, offset: 0, proposal_type: typeFilter || undefined },
  }));
  return { ...data, items: data.items.map(mapProposal) };
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
    tool_calls: (data.tool_calls ?? []).map(({ tool_call_id, tool_name, ...rest }) => ({
      tool_call_id: asString(tool_call_id) || undefined,
      tool_name: asString(tool_name) || undefined,
      ...(rest.data && typeof rest.data === "object"
        ? { data: rest.data as Record<string, unknown> }
        : {}),
    })),
  };
}

export async function translateText(text: string, targetLang: string) {
  return unwrap(await translateTextApiV1AiTranslatePost({
    client: getBffClient(), body: { text, target_lang: targetLang },
  }));
}

// Onboarding readiness probes and channel settings reads (generated SDK).
export async function verifyEmailDns(domain: string) {
  return unwrap(await verifyEmailDnsApiV1OnboardingEmailVerifyDnsPost({
    client: getBffClient(), body: { domain },
  }));
}

export type SandboxScenarioId = "b2c_wismo" | "strict_rag_warranty" | "b2b_moq_quote";

export async function runSandboxTest(
  scenarioId: SandboxScenarioId,
  message: string,
): Promise<SandboxResult> {
  return unwrap(await runSandboxTestApiV1OnboardingSandboxTestPost({
    client: getBffClient(), body: { scenario_id: scenarioId, message },
  }));
}

export async function fetchChannelAccounts(): Promise<PaginatedResponse<ChannelAccountApiItem>> {
  const page = unwrap(await listChannelAccountsApiV1ChannelsAccountsGet({
    client: getBffClient(), query: { limit: 20, offset: 0 },
  }));
  return {
    ...page,
    items: page.items.map((row) => mapChannelAccount(row as ChannelAccountRow)),
  };
}

export async function fetchChannelDlq(): Promise<PaginatedResponse<DlqEventApiItem>> {
  const page = unwrap(await listChannelDlqApiV1ChannelsDlqGet({
    client: getBffClient(), query: { limit: 20, offset: 0 },
  }));
  return {
    ...page,
    items: page.items.map((row) => ({
      ...row,
      external_event_id: row.external_event_id ?? "",
    })),
  };
}

export async function fetchChannelConsents(): Promise<PaginatedResponse<ConsentLogApiItem>> {
  return unwrap(await listChannelConsentsApiV1ChannelsConsentsGet({
    client: getBffClient(), query: { limit: 20, offset: 0 },
  }));
}

export async function fetchChannelSuppressions(): Promise<PaginatedResponse<SuppressionLogApiItem>> {
  return unwrap(await listChannelSuppressionsApiV1ChannelsSuppressionsGet({
    client: getBffClient(), query: { limit: 20, offset: 0 },
  }));
}

// ---------------------------------------------------------------------------
// Sprint 1/2/3 locked endpoints (master plan) that are not in the generated
// SDK yet. All of them go through the shared bffFetch transport, so the Next
// BFF proxy, session cookies and the 401-refresh flow behave exactly like the
// generated client. Once `pnpm openapi-ts` regenerates sdk.gen.ts these
// wrappers can delegate to it without touching any caller.
// ---------------------------------------------------------------------------

export type EmailLanguage = "en" | "zh";

export interface SendQuotePiEmailPayload {
  recipient_email: string;
  channel_account_id?: string | null;
  subject?: string | null;
  message_body?: string | null;
  language?: EmailLanguage;
}

export interface SendQuotePiEmailResponse {
  quote_id: string;
  quote_no: string;
  recipient_email: string;
  from_email: string;
  provider: "resend" | "smtp_imap";
  external_message_id?: string | null;
  pdf_filename: string;
  pdf_size_bytes: number;
  quote_status: string;
}

/**
 * 直接可用的 PI PDF 下载地址：交给 <a href target="_blank"> 用浏览器原生
 * 下载流，不经过 JS，也不会把二进制 PDF 读进内存。
 */
export function quotePdfDownloadUrl(
  quoteId: string,
  language: EmailLanguage = "en",
): string {
  return `/api/bff/v1/b2b/quotes/${encodeURIComponent(quoteId)}/pdf/download?language=${language}`;
}

/** 一键把已生成的 PI PDF 作为附件发给客户（Resend 或 SMTP/IMAP 发件账号）。 */
export async function sendQuotePiEmail(
  quoteId: string,
  payload: SendQuotePiEmailPayload,
): Promise<SendQuotePiEmailResponse> {
  return bffFetch<SendQuotePiEmailResponse>(
    `/v1/b2b/quotes/${encodeURIComponent(quoteId)}/send-email`,
    {
      method: "POST",
      body: JSON.stringify({
        recipient_email: payload.recipient_email,
        channel_account_id: payload.channel_account_id ?? null,
        subject: payload.subject ?? null,
        message_body: payload.message_body ?? null,
        language: payload.language ?? "en",
      }),
    },
  );
}

/** 让 AI 用确定性 SQL 工具查库后生成一条带 ERP 证据的回复提案（L1 待审）。 */
export async function proposeGroundedReply(
  conversationId: string,
): Promise<AiProposalItem> {
  const proposal = await bffFetch<AiProposalRead>(
    `/v1/ai/conversations/${encodeURIComponent(conversationId)}/propose-reply`,
    { method: "POST", body: JSON.stringify({}) },
  );
  return mapProposal(proposal);
}

/** PI 抬头 / 银行电汇资料 / 默认条款：全部由租户在 UI 里维护，零硬编码。 */
export interface PiTemplateSettings {
  company_legal_name: string;
  company_address: string;
  contact_phone: string;
  contact_email: string;
  website: string;
  tax_or_reg_no: string;
  bank_beneficiary_name: string;
  bank_name: string;
  bank_account_no: string;
  bank_swift_code: string;
  bank_address: string;
  default_terms_notes: string;
  footer_signature_title: string;
}

const PI_TEMPLATE_FIELDS = [
  "company_legal_name",
  "company_address",
  "contact_phone",
  "contact_email",
  "website",
  "tax_or_reg_no",
  "bank_beneficiary_name",
  "bank_name",
  "bank_account_no",
  "bank_swift_code",
  "bank_address",
  "default_terms_notes",
  "footer_signature_title",
] as const satisfies ReadonlyArray<keyof PiTemplateSettings>;

/** 后端可能只回传部分字段：全部补齐为空串，输入框永远是受控的。 */
export function normalizePiTemplateSettings(
  raw: Record<string, unknown> | PiTemplateSettings | null | undefined,
): PiTemplateSettings {
  const source = (raw ?? {}) as Record<string, unknown>;
  const out = {} as PiTemplateSettings;
  for (const field of PI_TEMPLATE_FIELDS) {
    const value = source[field];
    out[field] = typeof value === "string" ? value : "";
  }
  return out;
}

export async function fetchPiTemplateSettings(): Promise<PiTemplateSettings> {
  return normalizePiTemplateSettings(
    await bffFetch<Record<string, unknown>>("/v1/tenants/pi-template"),
  );
}

export async function updatePiTemplateSettings(
  payload: PiTemplateSettings,
): Promise<PiTemplateSettings> {
  return normalizePiTemplateSettings(
    await bffFetch<Record<string, unknown>>("/v1/tenants/pi-template", {
      method: "PATCH",
      body: JSON.stringify(normalizePiTemplateSettings(payload)),
    }),
  );
}
