/**
 * Phase 3A mock fixtures. Strictly UI fixtures for `NEXT_PUBLIC_USE_MOCK_API=true`;
 * the live path consumes the generated `@hey-api/openapi-ts` SDK instead.
 */
import type {
  CustomerRead,
  DashboardRead,
  FollowupRead,
  LeadRead,
  MovementRead,
  OpportunityRead,
  OverviewRead,
  PaginatedResponseLeadRead,
  PaginatedResponseFollowupRead,
  PaginatedResponseMovementRead,
  PaginatedResponsePurchaseOrderRead,
  PaginatedResponseStockRead,
  PaginatedResponseSupplierRead,
  PaginatedResponseTaskRead,
  PaginatedResponseOpportunityRead,
  PurchaseOrderRead,
  SearchRead,
  StockRead,
  SupplierRead,
  TaskRead,
} from "@/lib/api-client/types.gen";

const NOW = "2026-11-19T09:00:00Z";
const TENANT = "22222222-2222-2222-2222-222222222222";
const WAREHOUSE = "33333333-3333-3333-3333-333333333333";
const VARIANT = "44444444-4444-4444-4444-444444444444";
const CUSTOMER_HANS = "55555555-5555-5555-5555-555555555555";
const CUSTOMER_ELENA = "66666666-6666-6666-6666-666666666666";
const OWNER = "11111111-1111-1111-1111-111111111111";

function page<T>(items: T[]): { has_more: boolean; items: T[]; limit: number; offset: number; total: number } {
  return { items, total: items.length, limit: 20, offset: 0, has_more: false };
}

export const mockLeads: LeadRead[] = [
  {
    id: "lead-1",
    tenant_id: TENANT,
    customer_id: CUSTOMER_HANS,
    source_channel: "email",
    utm_campaign: "outdoor-lighting-q4",
    lead_type: "b2b_inquiry",
    qualification_score: 82,
    extracted_requirements: { sku: "LED-COB-24V-5M", qty: 500, incoterm: "FOB" },
    status: "qualified",
    created_at: NOW,
  },
  {
    id: "lead-2",
    tenant_id: TENANT,
    customer_id: CUSTOMER_ELENA,
    source_channel: "web_livechat",
    utm_campaign: null,
    lead_type: "b2b_inquiry",
    qualification_score: 35,
    extracted_requirements: {},
    status: "new",
    created_at: NOW,
  },
];

export const mockOpportunities: OpportunityRead[] = [
  {
    id: "opp-1",
    tenant_id: TENANT,
    lead_id: "lead-1",
    company_id: null,
    company_name: "Berlin Lighting GmbH",
    customer_id: CUSTOMER_HANS,
    title: "500m COB strip pilot order",
    stage: "quoting",
    expected_amount: "9820.00",
    currency: "USD",
    expected_close_date: "2026-12-05",
    owner_user_id: OWNER,
    created_at: NOW,
  },
  {
    id: "opp-2",
    tenant_id: TENANT,
    lead_id: null,
    company_id: null,
    company_name: null,
    customer_id: CUSTOMER_ELENA,
    title: "Retail sample pack",
    stage: "discovery",
    expected_amount: "640.00",
    currency: "USD",
    expected_close_date: null,
    owner_user_id: OWNER,
    created_at: NOW,
  },
];

export const mockCustomers: CustomerRead[] = [
  {
    id: CUSTOMER_HANS,
    tenant_id: TENANT,
    customer_type: "b2b_contact",
    display_name: "Hans Weber",
    primary_email: "hans.weber@berlin-lighting.de",
    primary_phone_e164: "+4930123456",
    country_code: "DE",
    postal_code: "10115",
    preferred_language: "de",
    iana_timezone: "Europe/Berlin",
    timezone_source: "identity",
    lifecycle_stage: "opportunity",
    ltv_usd: "18400.00",
    company_id: null,
    merged_into_customer_id: null,
    created_at: NOW,
    buyer_local_time: { formatted_time: "10:00", local_hour: 10, is_quiet_hours: false },
    identities: [],
  },
  {
    id: CUSTOMER_ELENA,
    tenant_id: TENANT,
    customer_type: "b2c_consumer",
    display_name: "Elena Vance",
    primary_email: "elena@example.com",
    primary_phone_e164: null,
    country_code: "US",
    postal_code: "10001",
    preferred_language: "en",
    iana_timezone: "America/New_York",
    timezone_source: "identity",
    lifecycle_stage: "customer",
    ltv_usd: "29.90",
    company_id: null,
    merged_into_customer_id: null,
    created_at: NOW,
    buyer_local_time: { formatted_time: "04:00", local_hour: 4, is_quiet_hours: true },
    identities: [],
  },
];

export const mockFollowups: FollowupRead[] = [
  {
    id: "followup-1",
    customer_id: CUSTOMER_HANS,
    opportunity_id: "opp-1",
    author_user_id: OWNER,
    followup_type: "email",
    summary: "Sent COB strip samples and the IP65 test report to the buyer.",
    next_action_at: "2026-11-22T09:00:00Z",
    created_at: NOW,
  },
];

export const mockTasks: TaskRead[] = [
  {
    id: "task-1",
    title: "Confirm 3000K vs 4000K sample preference with Hans",
    due_at: "2026-11-22T09:00:00Z",
    priority: "high",
    status: "open",
    linked_entity_type: "customer",
    linked_entity_id: CUSTOMER_HANS,
    assignee_user_id: OWNER,
    created_at: NOW,
  },
  {
    id: "task-2",
    title: "Chase Ningbo driver PMC for PO-DEMO-1001 ETD",
    due_at: "2026-11-26T09:00:00Z",
    priority: "normal",
    status: "open",
    linked_entity_type: null,
    linked_entity_id: null,
    assignee_user_id: OWNER,
    created_at: NOW,
  },
];

export const mockSuppliers: SupplierRead[] = [
  {
    id: "supplier-1",
    code: "SUP-LED-01",
    name: "Ningbo Driver Components Co.",
    contact_name: "Li Wei",
    contact_email: "sales@driver-components.example.com",
    contact_phone: "+865748880000",
    country: "CN",
    lead_time_days: 21,
    payment_terms: "30% T/T deposit",
    rating: "4.50",
    status: "active",
    created_at: NOW,
    updated_at: NOW,
  },
];

export const mockPurchaseOrders: PurchaseOrderRead[] = [
  {
    id: "po-1",
    po_no: "PO-DEMO-1001",
    supplier_id: "supplier-1",
    warehouse_id: WAREHOUSE,
    currency: "USD",
    total_amount: "5820.00",
    status: "ordered",
    expected_arrival_date: "2026-12-03",
    notes: "COB strip reels, ship with next consolidated container.",
    created_by: OWNER,
    created_at: NOW,
    updated_at: NOW,
    items: [
      {
        id: "poi-1",
        variant_id: VARIANT,
        sku: "LED-COB-24V-5M",
        qty_ordered: 1200,
        qty_received: 0,
        unit_cost: "4.8500",
      },
    ],
  },
  {
    id: "po-2",
    po_no: "PO-DEMO-1002",
    supplier_id: "supplier-1",
    warehouse_id: WAREHOUSE,
    currency: "USD",
    total_amount: "1455.00",
    status: "received",
    expected_arrival_date: "2026-11-16",
    notes: "Arrived and inspected.",
    created_by: OWNER,
    created_at: NOW,
    updated_at: NOW,
    items: [
      {
        id: "poi-2",
        variant_id: VARIANT,
        sku: "LED-COB-24V-5M",
        qty_ordered: 300,
        qty_received: 300,
        unit_cost: "4.8500",
      },
    ],
  },
];

export const mockMovements: MovementRead[] = [
  {
    id: "movement-1",
    movement_no: "RCV-PO-DEMO-1002",
    warehouse_id: WAREHOUSE,
    variant_id: VARIANT,
    movement_type: "inbound_po",
    qty_delta: 300,
    reference_type: "purchase_order",
    reference_id: "po-2",
    operator_user_id: OWNER,
    notes: null,
    created_at: NOW,
  },
  {
    id: "movement-2",
    movement_no: "ADJ-OPENING-COUNT",
    warehouse_id: WAREHOUSE,
    variant_id: VARIANT,
    movement_type: "stocktake_adjust",
    qty_delta: -4,
    reference_type: "stocktake",
    reference_id: null,
    operator_user_id: OWNER,
    notes: "Opening count variance.",
    created_at: NOW,
  },
];

export const mockStock: StockRead[] = [
  {
    warehouse_id: WAREHOUSE,
    variant_id: VARIANT,
    sku: "LED-COB-24V-5M",
    on_hand_qty: 4296,
    reserved_qty: 500,
    available_qty: 3796,
  },
];

export const mockDashboard: DashboardRead = {
  open_inquiries: 3,
  pipeline_value: [{ currency: "USD", amount: "10460.00" }],
  pending_proposals: 2,
  b2c_orders: 12,
  low_stock_alerts: 1,
  open_tasks: 2,
  upcoming_tasks: mockTasks,
  channels: [
    { channel: "web_livechat", conversations: 5 },
    { channel: "email", conversations: 3 },
    { channel: "shopify", conversations: 2 },
  ],
  funnel: [
    { stage: "discovery", count: 4 },
    { stage: "sampling", count: 3 },
    { stage: "quoting", count: 2 },
    { stage: "negotiating", count: 1 },
    { stage: "closed_won", count: 2 },
    { stage: "closed_lost", count: 1 },
  ],
  financials: [{ currency: "USD", cost: "4120.00", net_contribution_margin: "2380.00" }],
};

export const mockOverview: OverviewRead = {
  channels: [
    { channel: "web_livechat", conversations: 5 },
    { channel: "email", conversations: 3 },
    { channel: "shopify", conversations: 2 },
  ],
  funnel: mockDashboard.funnel,
  won_conversion_pct: "15.38",
  sku_margins: [
    {
      variant_id: VARIANT,
      sku: "LED-COB-24V-5M",
      currency: "USD",
      units: 500,
      revenue: "14750.00",
      cost: "2425.00",
      gross_margin: "12325.00",
    },
  ],
};

export const mockSearch: SearchRead = {
  items: [
    { kind: "customer", id: CUSTOMER_HANS, label: "Hans Weber" },
    { kind: "order", id: "order-1", label: "DEMO-SHIPPED" },
    { kind: "sku", id: VARIANT, label: "LED-COB-24V-5M" },
  ],
};

export const mockPhase3aPages = {
  leads: page(mockLeads) as PaginatedResponseLeadRead,
  opportunities: page(mockOpportunities) as PaginatedResponseOpportunityRead,
  followups: page(mockFollowups) as PaginatedResponseFollowupRead,
  tasks: page(mockTasks) as PaginatedResponseTaskRead,
  suppliers: page(mockSuppliers) as PaginatedResponseSupplierRead,
  purchaseOrders: page(mockPurchaseOrders) as PaginatedResponsePurchaseOrderRead,
  movements: page(mockMovements) as PaginatedResponseMovementRead,
  stock: page(mockStock) as PaginatedResponseStockRead,
};
