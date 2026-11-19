// Plan 04 bridge: every frontend call whose endpoint is NOT in
// contracts/openapi.json yet (Plan 04 Batch 7 is still landing on main).
// These go through the same BFF proxy (bffFetch) with the exact paths and
// payloads the UI already uses, so mock mode and the E2E suite keep working
// unchanged. The day Plan 04 merges, each function below is a one-step
// switch to the generated SDK — the function names and return shapes are
// already the ones the live adapter will expose.
//
// Sections:
//   A. Plan 04 AI / billing surface (locked contract in
//      docs/superpowers/plans/2026-11-19-00-phase1-2-master-plan-index.md)
//   B. Customer identity context (no contract equivalent; closest is
//      GET /customers/{id} -> CustomerRead, a different shape)
//   C. Onboarding mutations with no contract endpoint yet
//   D. Channel list endpoints with no contract operation yet (only the DLQ
//      replay POST exists in the contract; the lists are a backend gap)
import { bffFetch } from "@/lib/api";
import type {
  AiCapability,
  AutonomyLevel,
  PaginatedResponse,
  ProposalStatus,
  ProposalType,
} from "@/lib/contracts";
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

// ---------------------------------------------------------------------------
// Section A — Plan 04 AI / billing
// ---------------------------------------------------------------------------

export interface CapabilityConfigItem {
  capability: AiCapability;
  configured_autonomy: AutonomyLevel;
  max_allowed_autonomy: AutonomyLevel;
}

export interface KbDocumentItem {
  id: string;
  title: string;
  chunk_count: number;
  status: "pending" | "indexed" | "failed";
  created_at: string;
}

export interface BillingWalletResponse {
  plan_tier: string;
  included_credits_remaining: number;
  topup_credits_balance: number;
  auto_downgrade_to_l0?: boolean;
}

export interface AssistantChatResult {
  reply_text: string;
  citations: Array<{ doc_id: string; chunk_id: string; score: number }>;
  tool_calls?: Array<{ tool_call_id?: string; tool_name?: string }>;
  should_abstain: boolean;
  degraded_to_l0?: boolean;
}

export function listProposals<T>(
  status: ProposalStatus,
  typeFilter: "" | ProposalType,
): Promise<PaginatedResponse<T>> {
  const params = new URLSearchParams({ status, limit: "20", offset: "0" });
  if (typeFilter) params.set("proposal_type", typeFilter);
  return bffFetch<PaginatedResponse<T>>(`/v1/ai/proposals?${params.toString()}`);
}

export function fetchPendingProposalsCount(): Promise<{ total: number }> {
  return bffFetch<{ total: number }>(
    "/v1/ai/proposals?status=pending&limit=1&offset=0",
  );
}

export function approveProposal(id: string) {
  return bffFetch(`/v1/ai/proposals/${id}/approve`, { method: "POST" });
}

export function editApproveProposal(
  id: string,
  editedPayload: Record<string, unknown>,
) {
  return bffFetch(`/v1/ai/proposals/${id}/edit-approve`, {
    method: "POST",
    body: JSON.stringify({ edited_payload: editedPayload }),
  });
}

export function rejectProposal(id: string, reason: string) {
  return bffFetch(`/v1/ai/proposals/${id}/reject`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

export function fetchCapabilities(): Promise<{
  capabilities: CapabilityConfigItem[];
}> {
  return bffFetch<{ capabilities: CapabilityConfigItem[] }>("/v1/ai/capabilities");
}

export function updateCapability(
  capability: AiCapability,
  autonomyLevel: AutonomyLevel,
) {
  return bffFetch("/v1/ai/capabilities", {
    method: "PATCH",
    body: JSON.stringify({ settings: { [capability]: autonomyLevel } }),
  });
}

export function fetchWallet(): Promise<BillingWalletResponse> {
  return bffFetch<BillingWalletResponse>("/v1/billing/wallet");
}

export function fetchKbDocuments(): Promise<PaginatedResponse<KbDocumentItem>> {
  return bffFetch<PaginatedResponse<KbDocumentItem>>("/v1/ai/kb/documents");
}

export function uploadKbDocument(form: FormData) {
  return bffFetch("/v1/ai/kb/documents", { method: "POST", body: form });
}

export function deleteKbDocument(id: string) {
  return bffFetch(`/v1/ai/kb/documents/${id}`, { method: "DELETE" });
}

export function assistantChat(body: {
  capability: string;
  conversation_id?: string;
  message: string;
  strict_rag: boolean;
  channel_type: string;
  context: Record<string, unknown>;
}): Promise<AssistantChatResult> {
  return bffFetch<AssistantChatResult>("/v1/ai/assistant/chat", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function translateText(
  text: string,
  targetLang: string,
): Promise<{ translated_text: string }> {
  return bffFetch<{ translated_text: string }>("/v1/ai/translate", {
    method: "POST",
    body: JSON.stringify({ text, target_lang: targetLang }),
  });
}

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
