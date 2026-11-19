// Phase 3A live adapters over the generated @hey-api/openapi-ts SDK.
// Every request goes through the shared BFF-configured client (@/lib/api), so
// session cookies and the 401 refresh flow behave exactly like bffFetch.
import { getBffClient } from "@/lib/api";
import type {
  CreateStocktake,
  CustomerRead,
  DashboardRead,
  FollowupCreate,
  FollowupRead,
  FollowupUpdate,
  LeadRead,
  LeadUpdate,
  MovementRead,
  OpportunityRead,
  OverviewRead,
  PaginatedResponseFollowupRead,
  PaginatedResponseLeadRead,
  PaginatedResponseMovementRead,
  PaginatedResponseOpportunityRead,
  PaginatedResponsePurchaseOrderRead,
  PaginatedResponseStockRead,
  PaginatedResponseSupplierRead,
  PaginatedResponseTaskRead,
  PurchaseOrderCreate,
  PurchaseOrderRead,
  PurchaseOrderUpdate,
  ReceivePurchaseOrder,
  SearchRead,
  StockRead,
  SupplierCreate,
  SupplierRead,
  SupplierUpdate,
  TaskCreate,
  TaskRead,
  TaskUpdate,
} from "@/lib/api-client/types.gen";
import {
  createFollowupApiV1B2bFollowupsPost,
  createOpportunityApiV1B2bOpportunitiesPost,
  createPurchaseOrderApiV1SupplyChainPurchaseOrdersPost,
  createStocktakeApiV1WmsMovementsPost,
  createSupplierApiV1SupplyChainSuppliersPost,
  createTaskApiV1B2bTasksPost,
  dashboardApiV1AnalyticsDashboardGet,
  globalSearchApiV1SearchGet,
  listCustomersApiV1CustomersGet,
  listFollowupsApiV1B2bFollowupsGet,
  listLeadsApiV1B2bLeadsGet,
  listMovementsApiV1WmsMovementsGet,
  listOpportunitiesApiV1B2bOpportunitiesGet,
  listPurchaseOrdersApiV1SupplyChainPurchaseOrdersGet,
  listStockApiV1WmsStockGet,
  listSuppliersApiV1SupplyChainSuppliersGet,
  listVariantsApiV1CatalogVariantsGet,
  listWarehousesApiV1OrdersWarehousesGet,
  listTasksApiV1B2bTasksGet,
  overviewApiV1AnalyticsOverviewGet,
  patchLeadApiV1B2bLeadsLeadIdPatch,
  receivePurchaseOrderApiV1SupplyChainPurchaseOrdersPoIdReceivePost,
  updateFollowupApiV1B2bFollowupsFollowupIdPatch,
  updatePurchaseOrderApiV1SupplyChainPurchaseOrdersPoIdPatch,
  updateSupplierApiV1SupplyChainSuppliersSupplierIdPatch,
  updateOpportunityStageApiV1B2bOpportunitiesOpportunityIdPatch,
  updateTaskApiV1B2bTasksTaskIdPatch,
} from "@/lib/api-client/sdk.gen";

const client = () => getBffClient();

/** Wire-safe payloads for the generated SDK (numbers stay numbers for qty). */
export interface PurchaseOrderDraft {
  po_no?: string | null;
  supplier_id: string;
  warehouse_id: string;
  currency?: string;
  status?: "draft" | "ordered";
  expected_arrival_date?: string | null;
  notes?: string | null;
  items: Array<{ variant_id: string; qty_ordered: number; unit_cost: string }>;
}

export function listLeads(status?: LeadRead["status"]): Promise<PaginatedResponseLeadRead> {
  return listLeadsApiV1B2bLeadsGet({
    client: client(),
    query: { limit: 50, lead_status: status },
  }).then((r) => r.data as PaginatedResponseLeadRead);
}

export function updateLead(leadId: string, payload: LeadUpdate): Promise<LeadRead> {
  return patchLeadApiV1B2bLeadsLeadIdPatch({
    client: client(),
    path: { lead_id: leadId },
    body: payload,
  }).then((r) => r.data as LeadRead);
}

export function listOpportunities(): Promise<PaginatedResponseOpportunityRead> {
  return listOpportunitiesApiV1B2bOpportunitiesGet({ client: client(), query: { limit: 50 } }).then(
    (r) => r.data as PaginatedResponseOpportunityRead
  );
}

export function updateOpportunityStage(
  opportunityId: string,
  stage: OpportunityRead["stage"]
): Promise<OpportunityRead> {
  return updateOpportunityStageApiV1B2bOpportunitiesOpportunityIdPatch({
    client: client(),
    path: { opportunity_id: opportunityId },
    body: { stage },
  }).then((r) => r.data as OpportunityRead);
}

export function listFollowups(customerId?: string): Promise<PaginatedResponseFollowupRead> {
  return listFollowupsApiV1B2bFollowupsGet({
    client: client(),
    query: { limit: 50, customer_id: customerId },
  }).then((r) => r.data as PaginatedResponseFollowupRead);
}

export function createFollowup(payload: FollowupCreate): Promise<FollowupRead> {
  return createFollowupApiV1B2bFollowupsPost({ client: client(), body: payload }).then(
    (r) => r.data as FollowupRead
  );
}

export function updateFollowup(
  followupId: string,
  payload: FollowupUpdate
): Promise<FollowupRead> {
  return updateFollowupApiV1B2bFollowupsFollowupIdPatch({
    client: client(),
    path: { followup_id: followupId },
    body: payload,
  }).then((r) => r.data as FollowupRead);
}

export function listTasks(status?: TaskRead["status"]): Promise<PaginatedResponseTaskRead> {
  return listTasksApiV1B2bTasksGet({
    client: client(),
    query: { limit: 50, task_status: status },
  }).then((r) => r.data as PaginatedResponseTaskRead);
}

export function createTask(payload: TaskCreate): Promise<TaskRead> {
  return createTaskApiV1B2bTasksPost({ client: client(), body: payload }).then(
    (r) => r.data as TaskRead
  );
}

export function updateTask(taskId: string, payload: TaskUpdate): Promise<TaskRead> {
  return updateTaskApiV1B2bTasksTaskIdPatch({
    client: client(),
    path: { task_id: taskId },
    body: payload,
  }).then((r) => r.data as TaskRead);
}

export function listSuppliers(): Promise<PaginatedResponseSupplierRead> {
  return listSuppliersApiV1SupplyChainSuppliersGet({ client: client(), query: { limit: 50 } }).then(
    (r) => r.data as PaginatedResponseSupplierRead
  );
}

export function createSupplier(payload: SupplierCreate): Promise<SupplierRead> {
  return createSupplierApiV1SupplyChainSuppliersPost({ client: client(), body: payload }).then(
    (r) => r.data as SupplierRead
  );
}

export function updateSupplier(
  supplierId: string,
  payload: SupplierUpdate
): Promise<SupplierRead> {
  return updateSupplierApiV1SupplyChainSuppliersSupplierIdPatch({
    client: client(),
    path: { supplier_id: supplierId },
    body: payload,
  }).then((r) => r.data as SupplierRead);
}

export function listPurchaseOrders(): Promise<PaginatedResponsePurchaseOrderRead> {
  return listPurchaseOrdersApiV1SupplyChainPurchaseOrdersGet({
    client: client(),
    query: { limit: 50 },
  }).then((r) => r.data as PaginatedResponsePurchaseOrderRead);
}

export function createPurchaseOrder(
  draft: PurchaseOrderDraft
): Promise<PurchaseOrderRead> {
  const payload: PurchaseOrderCreate = {
    supplier_id: draft.supplier_id,
    warehouse_id: draft.warehouse_id,
    items: draft.items.map((item) => ({
      variant_id: item.variant_id,
      qty_ordered: item.qty_ordered,
      unit_cost: item.unit_cost,
    })),
    currency: draft.currency ?? "USD",
    status: draft.status ?? "ordered",
    expected_arrival_date: draft.expected_arrival_date ?? null,
    notes: draft.notes ?? null,
    po_no: draft.po_no ?? null,
  };
  return createPurchaseOrderApiV1SupplyChainPurchaseOrdersPost({
    client: client(),
    body: payload,
  }).then((r) => r.data as PurchaseOrderRead);
}

export function updatePurchaseOrder(
  poId: string,
  payload: PurchaseOrderUpdate
): Promise<PurchaseOrderRead> {
  return updatePurchaseOrderApiV1SupplyChainPurchaseOrdersPoIdPatch({
    client: client(),
    path: { po_id: poId },
    body: payload,
  }).then((r) => r.data as PurchaseOrderRead);
}

/** Receive all outstanding quantities. `receiptId` keeps retries idempotent. */
export function receivePurchaseOrder(
  poId: string,
  receiptId: string,
  items: ReceivePurchaseOrder["items"] = []
): Promise<PurchaseOrderRead> {
  return receivePurchaseOrderApiV1SupplyChainPurchaseOrdersPoIdReceivePost({
    client: client(),
    path: { po_id: poId },
    body: { receipt_id: receiptId, items },
  }).then((r) => r.data as PurchaseOrderRead);
}

export function listMovements(): Promise<PaginatedResponseMovementRead> {
  return listMovementsApiV1WmsMovementsGet({ client: client(), query: { limit: 50 } }).then(
    (r) => r.data as PaginatedResponseMovementRead
  );
}

export function createStocktake(payload: CreateStocktake): Promise<MovementRead> {
  return createStocktakeApiV1WmsMovementsPost({ client: client(), body: payload }).then(
    (r) => r.data as MovementRead
  );
}

export function listStock(): Promise<PaginatedResponseStockRead> {
  return listStockApiV1WmsStockGet({ client: client(), query: { limit: 50 } }).then(
    (r) => r.data as PaginatedResponseStockRead
  );
}

export function fetchDashboard(): Promise<DashboardRead> {
  return dashboardApiV1AnalyticsDashboardGet({ client: client() }).then(
    (r) => r.data as DashboardRead
  );
}

export function fetchOverview(): Promise<OverviewRead> {
  return overviewApiV1AnalyticsOverviewGet({ client: client() }).then(
    (r) => r.data as OverviewRead
  );
}

export function globalSearch(query: string): Promise<SearchRead> {
  return globalSearchApiV1SearchGet({ client: client(), query: { q: query } }).then(
    (r) => r.data as SearchRead
  );
}

export type { CustomerRead, StockRead, SupplierRead, PurchaseOrderRead };

export function listCustomers(): Promise<{ has_more: boolean; items: CustomerRead[]; limit: number; offset: number; total: number }> {
  return listCustomersApiV1CustomersGet({ client: client(), query: { limit: 100 } }).then(
    (r) => r.data as { has_more: boolean; items: CustomerRead[]; limit: number; offset: number; total: number }
  );
}

export function listWarehouses(): Promise<{
  default_warehouse_id: string | null;
  items: Array<{ id: string; is_default: boolean }>;
}> {
  return listWarehousesApiV1OrdersWarehousesGet({ client: client() }).then(
    (r) => r.data as { default_warehouse_id: string | null; items: Array<{ id: string; is_default: boolean }> }
  );
}

export function listVariants(): Promise<{
  items: Array<{ id: string; sku: string; retail_price_usd: string }>;
}> {
  return listVariantsApiV1CatalogVariantsGet({ client: client(), query: { limit: 50 } }).then(
    (r) => r.data as { items: Array<{ id: string; sku: string; retail_price_usd: string }> }
  );
}

export function createOpportunityFromLead(payload: {
  customer_id: string;
  lead_id?: string | null;
  owner_user_id: string;
  title: string;
  expected_amount: string;
  currency: string;
  stage: OpportunityRead["stage"];
}): Promise<OpportunityRead> {
  return createOpportunityApiV1B2bOpportunitiesPost({ client: client(), body: payload }).then(
    (r) => r.data as OpportunityRead
  );
}
