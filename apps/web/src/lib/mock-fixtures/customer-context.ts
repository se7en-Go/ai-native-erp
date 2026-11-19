import type {
  ConfidenceLevel,
  CustomerType,
  IdentityType,
  Incoterm,
} from "@/lib/contracts";

// Structural mirror of CustomerContextPayload from
// components/inbox/context-drawer (defined here so fixtures stay usable
// before Task 5.3 lands).
export interface CustomerContextPayload {
  customer: {
    id: string;
    display_name: string;
    customer_type: CustomerType;
    ltv_usd: string | number;
    lifecycle_stage: string;
  };
  company?: {
    id: string;
    legal_name: string;
    country_code: string;
    credit_tier: string;
    default_incoterm: Incoterm;
  } | null;
  identities: Array<{
    id: string;
    identity_type: IdentityType;
    identity_value: string;
    confidence_level: ConfidenceLevel;
  }>;
  probable_duplicates: Array<{
    candidate_customer_id: string;
    display_name: string;
    matched_signal: string;
    similarity_score: number;
  }>;
  merge_events: Array<{
    id: string;
    source_customer_id: string;
    target_customer_id: string;
    action: "merge" | "split";
    reason?: string | null;
  }>;
  recent_orders: Array<{
    id: string;
    order_no: string;
    order_mode: string;
    fulfillment_status: string;
    payment_status: string;
    total_amount: string | number;
    currency: string;
    tracking_number?: string | null;
    standardized_status?: string | null;
    sub_status?: string | null;
    is_stagnant_72h?: boolean;
  }>;
  recent_quotes: Array<{
    id: string;
    quote_no: string;
    version: number;
    incoterm: Incoterm;
    total_amount: string | number;
    quote_currency: string;
    status: string;
  }>;
}

export const mockCustomerContext: CustomerContextPayload = {
  customer: {
    id: "cust-target",
    display_name: "Hans Weber",
    customer_type: "b2b_company_rep",
    ltv_usd: "42500.00",
    lifecycle_stage: "qualified",
  },
  company: {
    id: "comp-1",
    legal_name: "Berlin Lighting GmbH",
    country_code: "DE",
    credit_tier: "prepaid",
    default_incoterm: "FOB",
  },
  identities: [
    {
      id: "id-1",
      identity_type: "whatsapp_bsuid",
      identity_value: "bsuid_de_998122",
      confidence_level: "deterministic",
    },
    {
      id: "id-2",
      identity_type: "email",
      identity_value: "hans.weber@berlin-lighting.de",
      confidence_level: "deterministic",
    },
  ],
  probable_duplicates: [
    {
      candidate_customer_id: "cust-dup-9",
      display_name: "H. Weber",
      matched_signal: "email_domain + postal_code",
      similarity_score: 0.91,
    },
  ],
  merge_events: [],
  recent_orders: [
    {
      id: "ord-1",
      order_no: "ORD-2026-001",
      order_mode: "b2c_retail",
      fulfillment_status: "shipped",
      payment_status: "paid",
      total_amount: "189.00",
      currency: "USD",
      tracking_number: "YT26119000123",
      standardized_status: "InTransit",
      sub_status: "InTransit_CustomsCleared",
      is_stagnant_72h: false,
    },
  ],
  recent_quotes: [
    {
      id: "q-1",
      quote_no: "PI-202611-0042",
      version: 1,
      incoterm: "FOB",
      total_amount: "7100.00",
      quote_currency: "USD",
      status: "sent",
    },
  ],
};

export const mockLiveSkuCheck = {
  sku: "LED-COB-24V-5M",
  available_qty: 1240,
  unit_price_quote_ccy: "14.20",
  moq_tier_applied: 500,
  incoterm: "FOB" as const,
  source_tool_call_id: "sql-sku-99",
};
