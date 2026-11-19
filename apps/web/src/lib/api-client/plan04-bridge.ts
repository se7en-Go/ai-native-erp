// Compatibility for UI routes not yet in the backend contract.
import { bffFetch } from "@/lib/api";
import type { PaginatedResponse } from "@/lib/contracts";
import type {
  ChannelAccountApiItem,
  ConsentLogApiItem,
  DlqEventApiItem,
  SuppressionLogApiItem,
} from "./live-client";
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

export function verifyEmailDns(
  domain: string,
): Promise<{ spf_verified: boolean; dkim_verified: boolean; return_path_verified: boolean }> {
  return bffFetch<{
    spf_verified: boolean;
    dkim_verified: boolean;
    return_path_verified: boolean;
  }>("/v1/onboarding/email/verify-dns", {
    method: "POST",
    body: JSON.stringify({ domain }),
  });
}

export function runSandboxTest(
  scenarioId: string,
  message: string,
): Promise<{ passed: boolean; reply_text: string }> {
  return bffFetch<{ passed: boolean; reply_text: string }>(
    "/v1/onboarding/sandbox-test",
    { method: "POST", body: JSON.stringify({ scenario_id: scenarioId, message }) },
  );
}

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

// ---------------------------------------------------------------------------
// Section D — channel lists with no contract operation yet
// (GET /api/v1/channels/dlq, /accounts, /consents, /suppressions are absent;
// only POST /api/v1/channels/dlq/{event_id}/replay exists, and it lives in
// the generated SDK — see live-client.replayDlqEvent.)
// ---------------------------------------------------------------------------

export function fetchChannelAccounts(): Promise<
  PaginatedResponse<ChannelAccountApiItem>
> {
  return bffFetch<PaginatedResponse<ChannelAccountApiItem>>(
    "/v1/channels/accounts",
  );
}

export function fetchChannelDlq(): Promise<PaginatedResponse<DlqEventApiItem>> {
  return bffFetch<PaginatedResponse<DlqEventApiItem>>("/v1/channels/dlq");
}

export function fetchChannelConsents(): Promise<
  PaginatedResponse<ConsentLogApiItem>
> {
  return bffFetch<PaginatedResponse<ConsentLogApiItem>>("/v1/channels/consents");
}

export function fetchChannelSuppressions(): Promise<
  PaginatedResponse<SuppressionLogApiItem>
> {
  return bffFetch<PaginatedResponse<SuppressionLogApiItem>>(
    "/v1/channels/suppressions",
  );
}
