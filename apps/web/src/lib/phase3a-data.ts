// Phase 3A data layer: live generated-SDK calls by default, fixture-backed
// responses only when NEXT_PUBLIC_USE_MOCK_API=true (same switch as data-source.ts).
import * as live from "@/lib/api-client/phase3a";
import type {
  CreateStocktake,
  DashboardRead,
  FollowupCreate,
  FollowupRead,
  LeadRead,
  LeadUpdate,
  MovementRead,
  OpportunityRead,
  OverviewRead,
  PaginatedResponseCustomerRead,
  PaginatedResponseFollowupRead,
  PaginatedResponseLeadRead,
  PaginatedResponseMovementRead,
  PaginatedResponseOpportunityRead,
  PaginatedResponsePurchaseOrderRead,
  PaginatedResponseStockRead,
  PaginatedResponseSupplierRead,
  PaginatedResponseTaskRead,
  PurchaseOrderRead,
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
  mockCustomers,
  mockDashboard,
  mockFollowups,
  mockLeads,
  mockMovements,
  mockOpportunities,
  mockOverview,
  mockPhase3aPages,
  mockPurchaseOrders,
  mockSearch,
  mockSuppliers,
  mockTasks,
} from "@/lib/mock-fixtures/phase3a";
import { shouldUseMockApi } from "@/lib/data-source";

export type { CustomerRead, PurchaseOrderDraft } from "@/lib/api-client/phase3a";

// Stateful only in mock mode: creates land in these arrays so a page can re-query
// and see them, exactly like the real API would after the mutation.
const mockState = {
  leads: [...mockLeads],
  customers: [...mockCustomers],
  followups: [...mockFollowups],
  tasks: [...mockTasks],
  suppliers: [...mockSuppliers],
  purchaseOrders: [...mockPurchaseOrders],
  movements: [...mockMovements],
};

function mock<T>(value: T): Promise<T> {
  return Promise.resolve(value);
}

/** Filter fixtures the way the API would, so mock mode exercises the same UI states. */
function page<T>(items: T[]): { has_more: boolean; items: T[]; limit: number; offset: number; total: number } {
  return { items, total: items.length, limit: 50, offset: 0, has_more: false };
}

export function fetchPhase3aLeads(status?: LeadRead["status"]): Promise<PaginatedResponseLeadRead> {
  if (shouldUseMockApi()) {
    const items = status ? mockState.leads.filter((lead) => lead.status === status) : mockState.leads;
    return mock(page(items) as PaginatedResponseLeadRead);
  }
  return live.listLeads(status);
}

export function patchPhase3aLead(leadId: string, payload: LeadUpdate): Promise<LeadRead> {
  if (shouldUseMockApi()) {
    const index = Math.max(
      0,
      mockState.leads.findIndex((candidate) => candidate.id === leadId)
    );
    const lead = mockState.leads[index] ?? mockLeads[0];
    const updated: LeadRead = {
      ...lead,
      status: payload.status ?? lead.status,
      qualification_score: payload.qualification_score ?? lead.qualification_score,
    };
    mockState.leads[index] = updated;
    return mock(updated);
  }
  return live.updateLead(leadId, payload);
}

export function fetchPhase3aOpportunities(): Promise<PaginatedResponseOpportunityRead> {
  if (shouldUseMockApi()) {
    return mock(mockPhase3aPages.opportunities);
  }
  return live.listOpportunities();
}

export function patchPhase3aOpportunityStage(
  opportunityId: string,
  stage: OpportunityRead["stage"]
): Promise<OpportunityRead> {
  if (shouldUseMockApi()) {
    const opportunity =
      mockOpportunities.find((candidate) => candidate.id === opportunityId) ?? mockOpportunities[0];
    return mock({ ...opportunity, stage });
  }
  return live.updateOpportunityStage(opportunityId, stage);
}

export function fetchPhase3aFollowups(customerId?: string): Promise<PaginatedResponseFollowupRead> {
  if (shouldUseMockApi()) {
    const items = customerId
      ? mockState.followups.filter((row) => row.customer_id === customerId)
      : mockState.followups;
    return mock(page(items) as PaginatedResponseFollowupRead);
  }
  return live.listFollowups(customerId);
}

export function addPhase3aFollowup(payload: FollowupCreate): Promise<FollowupRead> {
  if (shouldUseMockApi()) {
    const created: FollowupRead = {
      id: `followup-${Date.now()}`,
      customer_id: payload.customer_id,
      opportunity_id: payload.opportunity_id ?? null,
      author_user_id: "11111111-1111-1111-1111-111111111111",
      followup_type: payload.followup_type,
      summary: payload.summary,
      next_action_at: payload.next_action_at ?? null,
      created_at: new Date().toISOString(),
    };
    mockState.followups = [created, ...mockState.followups];
    return mock(created);
  }
  return live.createFollowup(payload);
}

export function fetchPhase3aTasks(status?: TaskRead["status"]): Promise<PaginatedResponseTaskRead> {
  if (shouldUseMockApi()) {
    const items = status
      ? mockState.tasks.filter((task) => task.status === status)
      : mockState.tasks;
    return mock(page(items) as PaginatedResponseTaskRead);
  }
  return live.listTasks(status);
}

export function addPhase3aTask(payload: TaskCreate): Promise<TaskRead> {
  if (shouldUseMockApi()) {
    const created: TaskRead = {
      id: `task-${Date.now()}`,
      title: payload.title,
      due_at: payload.due_at ?? null,
      priority: payload.priority ?? "normal",
      status: "open",
      linked_entity_type: payload.linked_entity_type ?? null,
      linked_entity_id: payload.linked_entity_id ?? null,
      assignee_user_id: payload.assignee_user_id ?? null,
      created_at: new Date().toISOString(),
    };
    mockState.tasks = [created, ...mockState.tasks];
    return mock(created);
  }
  return live.createTask(payload);
}

export function patchPhase3aTask(taskId: string, payload: TaskUpdate): Promise<TaskRead> {
  if (shouldUseMockApi()) {
    const index = Math.max(0, mockState.tasks.findIndex((candidate) => candidate.id === taskId));
    const updated = { ...mockState.tasks[index], ...payload } as TaskRead;
    mockState.tasks[index] = updated;
    return mock(updated);
  }
  return live.updateTask(taskId, payload);
}

export function fetchPhase3aSuppliers(): Promise<PaginatedResponseSupplierRead> {
  if (shouldUseMockApi()) {
    return mock(page(mockState.suppliers) as PaginatedResponseSupplierRead);
  }
  return live.listSuppliers();
}

export function addPhase3aSupplier(payload: SupplierCreate): Promise<SupplierRead> {
  if (shouldUseMockApi()) {
    const now = new Date().toISOString();
    const created = {
      ...payload,
      id: `supplier-${Date.now()}`,
      contact_name: payload.contact_name ?? null,
      contact_email: payload.contact_email ?? null,
      contact_phone: payload.contact_phone ?? null,
      country: payload.country ?? null,
      payment_terms: payload.payment_terms ?? null,
      rating: payload.rating ? String(payload.rating) : null,
      status: payload.status ?? "active",
      created_at: now,
      updated_at: now,
    } as SupplierRead;
    mockState.suppliers = [created, ...mockState.suppliers];
    return mock(created);
  }
  return live.createSupplier(payload);
}

export function patchPhase3aSupplier(
  supplierId: string,
  payload: SupplierUpdate
): Promise<SupplierRead> {
  if (shouldUseMockApi()) {
    const index = Math.max(
      0,
      mockState.suppliers.findIndex((candidate) => candidate.id === supplierId)
    );
    const updated = { ...mockState.suppliers[index], ...payload } as SupplierRead;
    mockState.suppliers[index] = updated;
    return mock(updated);
  }
  return live.updateSupplier(supplierId, payload);
}

export function fetchPhase3aPurchaseOrders(): Promise<PaginatedResponsePurchaseOrderRead> {
  if (shouldUseMockApi()) {
    return mock(page(mockState.purchaseOrders) as PaginatedResponsePurchaseOrderRead);
  }
  return live.listPurchaseOrders();
}

export function addPhase3aPurchaseOrder(
  draft: live.PurchaseOrderDraft
): Promise<PurchaseOrderRead> {
  if (shouldUseMockApi()) {
    const now = new Date().toISOString();
    const total = draft.items
      .reduce((sum, item) => sum + item.qty_ordered * Number(item.unit_cost), 0)
      .toFixed(2);
    const created: PurchaseOrderRead = {
      id: `po-${Date.now()}`,
      po_no: draft.po_no ?? `PO-MOCK-${Date.now()}`,
      supplier_id: draft.supplier_id,
      warehouse_id: draft.warehouse_id,
      currency: draft.currency ?? "USD",
      total_amount: total,
      status: draft.status ?? "ordered",
      expected_arrival_date: draft.expected_arrival_date ?? null,
      notes: draft.notes ?? null,
      created_by: "11111111-1111-1111-1111-111111111111",
      created_at: now,
      updated_at: now,
      items: draft.items.map((item, index) => ({
        id: `poi-${Date.now()}-${index}`,
        variant_id: item.variant_id,
        sku: "LED-COB-24V-5M",
        qty_ordered: item.qty_ordered,
        qty_received: 0,
        unit_cost: item.unit_cost,
      })),
    };
    mockState.purchaseOrders = [created, ...mockState.purchaseOrders];
    return mock(created);
  }
  return live.createPurchaseOrder(draft);
}

export function receivePhase3aPurchaseOrder(
  poId: string,
  receiptId: string
): Promise<PurchaseOrderRead> {
  if (shouldUseMockApi()) {
    const index = Math.max(
      0,
      mockState.purchaseOrders.findIndex((candidate) => candidate.id === poId)
    );
    const order = mockState.purchaseOrders[index];
    const received: PurchaseOrderRead = {
      ...order,
      status: "received",
      items: order.items.map((item) => ({ ...item, qty_received: item.qty_ordered })),
    };
    mockState.purchaseOrders[index] = received;
    mockState.movements = [
      {
        id: `movement-${Date.now()}`,
        movement_no: `RCV-${receiptId.slice(0, 8)}`,
        warehouse_id: order.warehouse_id,
        variant_id: order.items[0]?.variant_id ?? "",
        movement_type: "inbound_po",
        qty_delta: order.items.reduce((sum, item) => sum + item.qty_ordered, 0),
        reference_type: "purchase_order",
        reference_id: order.id,
        operator_user_id: null,
        notes: null,
        created_at: new Date().toISOString(),
      },
      ...mockState.movements,
    ];
    return mock(received);
  }
  return live.receivePurchaseOrder(poId, receiptId);
}

export function fetchPhase3aMovements(): Promise<PaginatedResponseMovementRead> {
  if (shouldUseMockApi()) {
    return mock(page(mockState.movements) as PaginatedResponseMovementRead);
  }
  return live.listMovements();
}

export function addPhase3aStocktake(payload: CreateStocktake): Promise<MovementRead> {
  if (shouldUseMockApi()) {
    const created: MovementRead = {
      id: `movement-${Date.now()}`,
      movement_no: `ADJ-${payload.request_id.slice(0, 8)}`,
      warehouse_id: payload.warehouse_id,
      variant_id: payload.variant_id,
      movement_type: "stocktake_adjust",
      qty_delta: payload.qty_delta,
      reference_type: "stocktake",
      reference_id: payload.request_id,
      operator_user_id: "11111111-1111-1111-1111-111111111111",
      notes: payload.notes ?? null,
      created_at: new Date().toISOString(),
    };
    mockState.movements = [created, ...mockState.movements];
    return mock(created);
  }
  return live.createStocktake(payload);
}

export function fetchPhase3aStock(): Promise<PaginatedResponseStockRead> {
  if (shouldUseMockApi()) {
    return mock(mockPhase3aPages.stock);
  }
  return live.listStock();
}

export function fetchPhase3aDashboard(): Promise<DashboardRead> {
  return shouldUseMockApi() ? mock(mockDashboard) : live.fetchDashboard();
}

export function fetchPhase3aOverview(): Promise<OverviewRead> {
  return shouldUseMockApi() ? mock(mockOverview) : live.fetchOverview();
}

export function searchPhase3a(query: string): Promise<SearchRead> {
  if (shouldUseMockApi()) {
    const needle = query.trim().toLowerCase();
    return mock({
      items: mockSearch.items.filter((hit) => hit.label.toLowerCase().includes(needle)),
    });
  }
  return live.globalSearch(query);
}

export type {
  DashboardRead,
  FollowupCreate,
  MovementRead,
  OverviewRead,
  PurchaseOrderRead,
  StockRead,
  SupplierRead,
  TaskRead,
};

export function fetchPhase3aCustomers(): Promise<PaginatedResponseCustomerRead> {
  if (shouldUseMockApi()) {
    return mock(page(mockState.customers) as PaginatedResponseCustomerRead);
  }
  return live.listCustomers();
}

export function fetchPhase3aWarehouses(): Promise<{
  default_warehouse_id: string | null;
  items: Array<{ id: string; is_default: boolean }>;
}> {
  if (shouldUseMockApi()) {
    return mock({
      default_warehouse_id: "33333333-3333-3333-3333-333333333333",
      items: [{ id: "33333333-3333-3333-3333-333333333333", is_default: true }],
    });
  }
  return live.listWarehouses();
}

export function fetchPhase3aVariants(): Promise<{
  items: Array<{ id: string; sku: string; retail_price_usd: string }>;
}> {
  if (shouldUseMockApi()) {
    return mock({
      items: [
        {
          id: "44444444-4444-4444-4444-444444444444",
          sku: "LED-COB-24V-5M",
          retail_price_usd: "29.90",
        },
      ],
    });
  }
  return live.listVariants();
}

export function convertPhase3aLeadToOpportunity(
  lead: LeadRead,
  ownerUserId: string
): Promise<OpportunityRead> {
  const payload = {
    customer_id: lead.customer_id,
    lead_id: lead.id,
    owner_user_id: ownerUserId,
    title: `Opportunity · ${lead.source_channel}`,
    expected_amount: "0.00",
    currency: "USD",
    stage: "discovery" as const,
  };
  if (shouldUseMockApi()) {
    return mock({
      id: `opp-${Date.now()}`,
      tenant_id: lead.tenant_id,
      lead_id: lead.id,
      company_id: null,
      customer_id: lead.customer_id,
      title: payload.title,
      stage: payload.stage,
      expected_amount: payload.expected_amount,
      currency: payload.currency,
      expected_close_date: null,
      owner_user_id: ownerUserId,
      created_at: new Date().toISOString(),
    });
  }
  return live.createOpportunityFromLead(payload);
}
