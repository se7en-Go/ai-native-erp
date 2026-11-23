import { z } from "zod";

export const UserRoleSchema = z.enum(["owner", "admin", "manager", "agent", "finance"]);
export type UserRole = z.infer<typeof UserRoleSchema>;

export const OnboardingStageSchema = z.enum([
  "WorkspaceCreated",
  "ChannelConnecting",
  "ChannelVerified",
  "CatalogSyncing",
  "AISandboxReady",
  "LiveProduction",
]);
export type OnboardingStage = z.infer<typeof OnboardingStageSchema>;

export const ChannelTypeSchema = z.enum([
  "whatsapp_cloud",
  "whatsapp_coexistence",
  "email",
  "web_livechat",
  "shopify",
  "woocommerce",
  "shopline",
  "shoplazza",
  "tiktok_shop",
  "made_in_china_email",
  "alibaba_email",
  "messenger",
  "instagram_dm",
  "telegram",
  "amazon_sp",
  "ebay",
  "mercado_libre",
  "temu",
  "walmart",
  "shopee",
  "lazada",
  "shein",
  "aliexpress",
  "ozon",
]);
export type ChannelType = z.infer<typeof ChannelTypeSchema>;

export const ChannelStatusSchema = z.enum(["active", "token_expired", "rate_limited", "disconnected"]);
export type ChannelStatus = z.infer<typeof ChannelStatusSchema>;

export const ConversationModeSchema = z.enum(["b2c_support", "b2b_inquiry", "mixed"]);
export type ConversationMode = z.infer<typeof ConversationModeSchema>;

export const ConversationStatusSchema = z.enum(["open", "pending_human", "snoozed", "resolved"]);
export type ConversationStatus = z.infer<typeof ConversationStatusSchema>;

export const MessageDirectionSchema = z.enum(["inbound", "outbound", "internal_note"]);
export type MessageDirection = z.infer<typeof MessageDirectionSchema>;

export const MessageSenderTypeSchema = z.enum(["customer", "human_agent", "ai_agent", "system"]);
export type MessageSenderType = z.infer<typeof MessageSenderTypeSchema>;

export const MessageContentTypeSchema = z.enum([
  "text",
  "rich_html",
  "template",
  "media",
  "quote_card",
  "order_card",
]);
export type MessageContentType = z.infer<typeof MessageContentTypeSchema>;

export const DeliveryStatusSchema = z.enum(["queued", "sent", "delivered", "read", "failed"]);
export type DeliveryStatus = z.infer<typeof DeliveryStatusSchema>;

export const IdentityTypeSchema = z.enum([
  "email",
  "phone_e164",
  "whatsapp_waid",
  "whatsapp_bsuid",
  "ig_psid",
  "fb_psid",
  "shopify_customer_id",
  "tiktok_buyer_id",
  "amazon_buyer_email",
  "web_visitor_id",
]);
export type IdentityType = z.infer<typeof IdentityTypeSchema>;

export const ConfidenceLevelSchema = z.enum(["deterministic", "human_verified", "probabilistic"]);
export type ConfidenceLevel = z.infer<typeof ConfidenceLevelSchema>;

export const CustomerTypeSchema = z.enum(["b2c_consumer", "b2b_contact", "b2b_company_rep"]);
export type CustomerType = z.infer<typeof CustomerTypeSchema>;

export const OpportunityStageSchema = z.enum([
  "discovery",
  "sampling",
  "quoting",
  "negotiating",
  "closed_won",
  "closed_lost",
]);
export type OpportunityStage = z.infer<typeof OpportunityStageSchema>;

export const IncotermSchema = z.enum(["EXW", "FOB", "CIF", "DDP", "DAP"]);
export type Incoterm = z.infer<typeof IncotermSchema>;

export const QuoteStatusSchema = z.enum([
  "draft",
  "pending_approval",
  "sent",
  "accepted",
  "expired",
  "converted",
]);
export type QuoteStatus = z.infer<typeof QuoteStatusSchema>;

export const OrderModeSchema = z.enum(["b2c_retail", "b2b_sample", "b2b_wholesale"]);
export type OrderMode = z.infer<typeof OrderModeSchema>;

export const FulfillmentStatusSchema = z.enum([
  "pending_payment",
  "confirmed_reserved",
  "in_production",
  "partially_shipped",
  "shipped",
  "delivered",
  "exception",
  "cancelled",
  "refunded",
]);
export type FulfillmentStatus = z.infer<typeof FulfillmentStatusSchema>;

export const PaymentStatusSchema = z.enum([
  "unpaid",
  "deposit_paid",
  "partially_paid",
  "paid",
  "refunded",
]);
export type PaymentStatus = z.infer<typeof PaymentStatusSchema>;

export const InventoryEventTypeSchema = z.enum([
  "inbound_po",
  "reserve_order",
  "release_cancel",
  "deduct_shipped",
  "stocktake_adjust",
]);
export type InventoryEventType = z.infer<typeof InventoryEventTypeSchema>;

export const ProposalTypeSchema = z.enum([
  "reply_draft",
  "crm_entity_extract",
  "quote_draft",
  "follow_up_suggestion",
  "identity_merge",
  "refund_or_reship",
  "payment_match",
]);
export type ProposalType = z.infer<typeof ProposalTypeSchema>;

export const ProposalStatusSchema = z.enum([
  "pending",
  "approved",
  "edited_and_approved",
  "rejected",
  "expired",
  "failed",
]);
export type ProposalStatus = z.infer<typeof ProposalStatusSchema>;

export const AutonomyLevelSchema = z.enum(["L0_copilot", "L1_proposal", "L2_auto"]);
export type AutonomyLevel = z.infer<typeof AutonomyLevelSchema>;

export const AiCapabilitySchema = z.enum([
  "cs_support",
  "sales_quoting",
  "order_logistics",
  "content_studio",
  "biz_analytics",
  "finance_recon",
]);
export type AiCapability = z.infer<typeof AiCapabilitySchema>;

export const PrincipalSchema = z.object({
  user_id: z.string(),
  tenant_id: z.string(),
  role: UserRoleSchema,
  email: z.string().email(),
  tenant_slug: z.string().optional(),
  tenant_name: z.string().optional(),
  full_name: z.string().optional(),
  can_view_cost: z.boolean().optional(),
  onboarding_stage: OnboardingStageSchema.optional(),
});
export type Principal = z.infer<typeof PrincipalSchema>;

export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
}

export interface ProblemDetail {
  type: string;
  title: string;
  status: number;
  detail: string;
  instance?: string;
  code?: string;
}

export class BffApiError extends Error {
  status: number;
  code: string;
  detail: string;

  constructor(problem: ProblemDetail) {
    super(problem.detail || problem.title);
    this.name = "BffApiError";
    this.status = problem.status;
    this.code = problem.code ?? "ERR_BFF_UPSTREAM";
    this.detail = problem.detail;
  }
}

export function formatMoney(
  rawValue: unknown,
  currency: string = "USD",
  locale: string = "en-US"
): string {
  if (rawValue === null || rawValue === undefined || typeof rawValue === "object") {
    return `${currency} 0.00`;
  }
  const normalized =
    typeof rawValue === "string" ? rawValue.replace(/,/g, "").trim() : rawValue;
  const num = typeof normalized === "number" ? normalized : Number(normalized);
  if (!Number.isFinite(num)) {
    return `${currency} 0.00`;
  }
  try {
    return new Intl.NumberFormat(locale, {
      style: "currency",
      currency,
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }).format(num);
  } catch {
    return `${currency} ${num.toFixed(2)}`;
  }
}

export interface BuyerLocalTimeDisplay {
  formattedTime: string;
  localHour24: number;
  isQuietHours: boolean;
  ianaTimezone: string;
}

export function computeBuyerLocalTimeInfo(
  ianaTimezone: string | null | undefined,
  now: Date = new Date()
): BuyerLocalTimeDisplay | null {
  if (!ianaTimezone) return null;
  try {
    const hourFmt = new Intl.DateTimeFormat("en-US", {
      timeZone: ianaTimezone,
      hour: "numeric",
      hour12: false,
    });
    const parsedHour = Number(hourFmt.format(now));
    const localHour24 = parsedHour === 24 ? 0 : parsedHour;

    const displayFmt = new Intl.DateTimeFormat("en-US", {
      timeZone: ianaTimezone,
      hour: "2-digit",
      minute: "2-digit",
      hour12: true,
    });

    return {
      formattedTime: displayFmt.format(now),
      localHour24,
      isQuietHours: localHour24 >= 21 || localHour24 < 8,
      ianaTimezone,
    };
  } catch {
    return null;
  }
}
