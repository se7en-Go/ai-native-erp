import { createClient } from "@hey-api/client-fetch";
import { bffFetch } from "@/lib/api";
import type {
  ChannelStatus,
  ChannelType,
  FulfillmentStatus,
  Incoterm,
  InventoryEventType,
  OnboardingStage,
  OpportunityStage,
  OrderMode,
  PaginatedResponse,
  PaymentStatus,
  QuoteStatus,
} from "@/lib/contracts";

// Hand-maintained stand-in for the @hey-api/openapi-ts generated client.
// The backend-owned contracts/openapi.json is not on this branch yet; once
// it lands, `pnpm openapi-ts` regenerates this directory from the contract
// and this file is replaced by the generated output. The shape below mirrors
// the generated client: a fetch client whose base URL the BFF layer sets.
/**
 * Absolute BFF base URL. @hey-api/client-fetch builds request URLs with
 * `new URL(...)`, which cannot parse a relative base (bffFetch calls fetch()
 * directly, so it never noticed). In the browser this is the app origin;
 * outside it (tests/SSR) it falls back to the local dev origin.
 */
export function bffBaseUrl(): string {
  const origin =
    typeof window !== "undefined" && window.location?.origin
      ? window.location.origin
      : "http://localhost:3000";
  return `${origin}/api/bff`;
}

export const client = createClient({
  baseUrl: bffBaseUrl(),
});

// ---------------------------------------------------------------------------
// Wave 2 transitional API types + typed GET helpers (Task 5.5).
// These mirror the shapes the generated client will expose once
// contracts/openapi.json lands; Wave 3 replaces this whole section with the
// openapi-ts output. The typed functions call the shared bffFetch transport
// (string URL, ProblemDetail errors) rather than the hey-api client's
// Request-object calls, so plan tests that stub global fetch keep working.
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

export async function getCatalogVariants(
  q = "",
  limit = 50,
  offset = 0,
): Promise<PaginatedResponse<CatalogVariantApiItem>> {
  const query = new URLSearchParams({
    q,
    limit: String(limit),
    offset: String(offset),
  });
  return bffFetch<PaginatedResponse<CatalogVariantApiItem>>(
    `/v1/catalog/variants?${query.toString()}`,
  );
}

export async function getVariantMappings(
  variantId: string,
): Promise<{
  variant: CatalogVariantApiItem;
  mappings: PlatformSkuMappingApiItem[];
}> {
  return bffFetch<{
    variant: CatalogVariantApiItem;
    mappings: PlatformSkuMappingApiItem[];
  }>(`/v1/catalog/variants/${variantId}/mappings`);
}

export async function getB2bOpportunities(
  limit = 50,
  offset = 0,
): Promise<PaginatedResponse<B2bOpportunityApiItem>> {
  const query = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
  });
  return bffFetch<PaginatedResponse<B2bOpportunityApiItem>>(
    `/v1/b2b/opportunities?${query.toString()}`,
  );
}

export async function getB2bQuotes(
  opportunityId = "",
  limit = 20,
  offset = 0,
): Promise<PaginatedResponse<B2bQuoteApiItem>> {
  const query = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
  });
  if (opportunityId) query.set("opportunity_id", opportunityId);
  return bffFetch<PaginatedResponse<B2bQuoteApiItem>>(
    `/v1/b2b/quotes?${query.toString()}`,
  );
}

// ---------------------------------------------------------------------------
// Wave 2 transitional API types + typed GET helpers (Task 5.6).
// Same replacement note as Task 5.5 above.
// ---------------------------------------------------------------------------

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

export async function getOrders(
  orderMode = "",
  limit = 20,
  offset = 0,
): Promise<PaginatedResponse<OrderSummaryApiItem>> {
  const query = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
  });
  if (orderMode) query.set("order_mode", orderMode);
  return bffFetch<PaginatedResponse<OrderSummaryApiItem>>(
    `/v1/orders?${query.toString()}`,
  );
}

export async function getOrderDetail(orderId: string): Promise<OrderDetailApiItem> {
  return bffFetch<OrderDetailApiItem>(`/v1/orders/${orderId}`);
}

export async function getInventoryBalances(
  variantId = "",
  limit = 20,
  offset = 0,
): Promise<PaginatedResponse<InventoryBalanceApiItem>> {
  const query = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
  });
  if (variantId) query.set("variant_id", variantId);
  return bffFetch<PaginatedResponse<InventoryBalanceApiItem>>(
    `/v1/orders/inventory/balances?${query.toString()}`,
  );
}

export async function getInventoryLedger(
  variantId = "",
  limit = 20,
  offset = 0,
): Promise<PaginatedResponse<InventoryLedgerApiItem>> {
  const query = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
  });
  if (variantId) query.set("variant_id", variantId);
  return bffFetch<PaginatedResponse<InventoryLedgerApiItem>>(
    `/v1/orders/inventory/ledger?${query.toString()}`,
  );
}

export async function getFinanceOverview(): Promise<FinanceOverviewApiItem> {
  return bffFetch<FinanceOverviewApiItem>("/v1/finance/overview");
}

// ---------------------------------------------------------------------------
// Wave 2 transitional API types + typed GET helpers (Task 5.7).
// Same replacement note as Task 5.5 above.
// ---------------------------------------------------------------------------

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

export async function getOnboardingState(): Promise<OnboardingStateApiItem> {
  return bffFetch<OnboardingStateApiItem>("/v1/onboarding/state");
}

export async function getChannelAccounts(): Promise<
  PaginatedResponse<ChannelAccountApiItem>
> {
  return bffFetch<PaginatedResponse<ChannelAccountApiItem>>(
    "/v1/channels/accounts",
  );
}

export async function getChannelDlq(): Promise<
  PaginatedResponse<DlqEventApiItem>
> {
  return bffFetch<PaginatedResponse<DlqEventApiItem>>("/v1/channels/dlq");
}

export async function getChannelConsents(): Promise<
  PaginatedResponse<ConsentLogApiItem>
> {
  return bffFetch<PaginatedResponse<ConsentLogApiItem>>(
    "/v1/channels/consents",
  );
}

export async function getChannelSuppressions(): Promise<
  PaginatedResponse<SuppressionLogApiItem>
> {
  return bffFetch<PaginatedResponse<SuppressionLogApiItem>>(
    "/v1/channels/suppressions",
  );
}
