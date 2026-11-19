// Structural mirrors of the order shapes used by /orders (OrderSummaryRow,
// OrderDetailPayload, InventoryBalanceItem, InventoryLedgerItem).
export interface MockOrderSummary {
  id: string;
  order_no: string;
  order_mode: string;
  customer_name?: string | null;
  fulfillment_status: string;
  payment_status: string;
  currency: string;
  total_amount: string;
  has_72h_stagnation_alert?: boolean;
  shipment_blocked_70_unpaid?: boolean;
}

export interface MockTrackingEvent {
  time_utc: string;
  location?: string | null;
  description: string;
}

export interface MockShipment {
  id: string;
  tracking_provider: string;
  tracking_number: string;
  carrier_code: string;
  shipment_mode: string;
  standardized_status: string;
  sub_status?: string | null;
  is_stagnant_72h: boolean;
  tracking_events: MockTrackingEvent[];
}

export interface MockOrderDetail extends MockOrderSummary {
  shipments: MockShipment[];
}

export interface MockInventoryBalance {
  warehouse_id: string;
  variant_id: string;
  sku: string;
  on_hand_qty: number;
  reserved_qty: number;
}

export interface MockInventoryLedgerEntry {
  id: number;
  variant_id: string;
  event_type: string;
  qty_delta_on_hand: number;
  qty_delta_reserved: number;
  reference_type: string;
  reference_id: string;
  idempotency_key: string;
  created_at: string;
}

export const mockOrders: MockOrderSummary[] = [
  {
    id: "ord-b2b-1",
    order_no: "ORD-B2B-2026-01",
    order_mode: "b2b_wholesale",
    customer_name: "Berlin Lighting GmbH",
    fulfillment_status: "in_production",
    payment_status: "deposit_paid",
    currency: "USD",
    total_amount: "7350.00",
    has_72h_stagnation_alert: true,
    shipment_blocked_70_unpaid: true,
  },
  {
    id: "ord-b2c-1",
    order_no: "ORD-B2C-2026-88",
    order_mode: "b2c_retail",
    customer_name: "Marta Kowalska",
    fulfillment_status: "shipped",
    payment_status: "paid",
    currency: "USD",
    total_amount: "59.80",
    has_72h_stagnation_alert: false,
    shipment_blocked_70_unpaid: false,
  },
];

export const mockOrderDetail: MockOrderDetail = {
  ...mockOrders[0],
  shipments: [
    {
      id: "ship-1",
      tracking_provider: "17track",
      tracking_number: "DHL99887766",
      carrier_code: "DHL",
      shipment_mode: "air_freight",
      standardized_status: "Alert",
      sub_status: "CustomsHold",
      is_stagnant_72h: true,
      tracking_events: [
        {
          time_utc: "2026-11-16T06:00:00Z",
          location: "Frankfurt DE",
          description: "Awaiting commercial invoice clearance",
        },
      ],
    },
  ],
};

export const mockInventoryBalances: MockInventoryBalance[] = [
  {
    warehouse_id: "wh-sz",
    variant_id: "var-1",
    sku: "LED-COB-24V-5M",
    on_hand_qty: 1500,
    reserved_qty: 500,
  },
];

export const mockInventoryLedger: MockInventoryLedgerEntry[] = [
  {
    id: 101,
    variant_id: "var-1",
    event_type: "reserve_order",
    qty_delta_on_hand: 0,
    qty_delta_reserved: 500,
    reference_type: "order",
    reference_id: "ord-b2b-1",
    idempotency_key: "reserve:ord-b2b-1:var-1",
    created_at: "2026-11-19T08:00:00Z",
  },
];

export function mockOrdersPage(orderMode = ""): {
  items: MockOrderSummary[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
} {
  const items = orderMode
    ? mockOrders.filter((o) => o.order_mode === orderMode)
    : mockOrders;
  return { items, total: items.length, limit: 20, offset: 0, has_more: false };
}

export function mockInventoryBalancesPage(): {
  items: MockInventoryBalance[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
} {
  return {
    items: mockInventoryBalances,
    total: mockInventoryBalances.length,
    limit: 20,
    offset: 0,
    has_more: false,
  };
}

export function mockInventoryLedgerPage(): {
  items: MockInventoryLedgerEntry[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
} {
  return {
    items: mockInventoryLedger,
    total: mockInventoryLedger.length,
    limit: 20,
    offset: 0,
    has_more: false,
  };
}
