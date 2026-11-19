// Compatibility for UI routes not yet in the backend contract.
import { bffFetch } from "@/lib/api";

import type {
  ConfidenceLevel,
  CustomerType,
  IdentityType,
  Incoterm,
} from "@/lib/contracts";

export function aiAssist(
  conversationId: string,
  body: {
    action: "draft" | "polish" | "summarize";
    draft_text?: string;
    strict_rag: boolean;
  },
): Promise<{ text: string; abstained: boolean }> {
  return bffFetch<{ text: string; abstained: boolean }>(
    `/v1/inbox/conversations/${conversationId}/ai-assist`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

// ---------------------------------------------------------------------------
// Section B — customer identity context (no contract equivalent)
// ---------------------------------------------------------------------------

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

export function fetchCustomerContext(
  customerId: string,
): Promise<CustomerContextPayload> {
  return bffFetch<CustomerContextPayload>(
    `/v1/identity/customers/${customerId}/context`,
  );
}

// ---------------------------------------------------------------------------
// Section C — onboarding mutations with no contract endpoint yet
// ---------------------------------------------------------------------------

export function connectStorefront(body: {
  platform: string;
  shop_domain: string;
  auth_mode: string;
  credentials: Record<string, string>;
}): Promise<unknown> {
  return bffFetch("/v1/onboarding/storefront/connect", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function saveWebchat(allowedOrigins: string[]): Promise<unknown> {
  return bffFetch("/v1/onboarding/webchat", {
    method: "PUT",
    body: JSON.stringify({ allowed_origins: allowedOrigins }),
  });
}
