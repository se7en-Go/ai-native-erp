from __future__ import annotations

from enum import StrEnum


class UserRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MANAGER = "manager"
    AGENT = "agent"
    FINANCE = "finance"


class OnboardingStage(StrEnum):
    WORKSPACE_CREATED = "WorkspaceCreated"
    CHANNEL_CONNECTING = "ChannelConnecting"
    CHANNEL_VERIFIED = "ChannelVerified"
    CATALOG_SYNCING = "CatalogSyncing"
    AI_SANDBOX_READY = "AISandboxReady"
    LIVE_PRODUCTION = "LiveProduction"


class ChannelType(StrEnum):
    WHATSAPP_CLOUD = "whatsapp_cloud"
    WHATSAPP_COEXISTENCE = "whatsapp_coexistence"
    EMAIL = "email"
    WEB_LIVECHAT = "web_livechat"
    SHOPIFY = "shopify"
    WOOCOMMERCE = "woocommerce"
    SHOPLINE = "shopline"
    SHOPLAZZA = "shoplazza"
    TIKTOK_SHOP = "tiktok_shop"
    MADE_IN_CHINA_EMAIL = "made_in_china_email"
    ALIBABA_EMAIL = "alibaba_email"
    MESSENGER = "messenger"
    INSTAGRAM_DM = "instagram_dm"
    TELEGRAM = "telegram"
    AMAZON_SP = "amazon_sp"
    EBAY = "ebay"
    MERCADO_LIBRE = "mercado_libre"
    TEMU = "temu"
    WALMART = "walmart"
    SHOPEE = "shopee"
    LAZADA = "lazada"
    SHEIN = "shein"
    ALIEXPRESS = "aliexpress"
    OZON = "ozon"


class ChannelVisibilityScope(StrEnum):
    SHARED = "shared"
    PERSONAL = "personal"


class ChannelStatus(StrEnum):
    ACTIVE = "active"
    TOKEN_EXPIRED = "token_expired"
    RATE_LIMITED = "rate_limited"
    DISCONNECTED = "disconnected"


class ConversationMode(StrEnum):
    B2C_SUPPORT = "b2c_support"
    B2B_INQUIRY = "b2b_inquiry"
    MIXED = "mixed"


class ConversationStatus(StrEnum):
    OPEN = "open"
    PENDING_HUMAN = "pending_human"
    SNOOZED = "snoozed"
    RESOLVED = "resolved"


class MessageDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    INTERNAL_NOTE = "internal_note"


class MessageSenderType(StrEnum):
    CUSTOMER = "customer"
    HUMAN_AGENT = "human_agent"
    AI_AGENT = "ai_agent"
    SYSTEM = "system"


class MessageContentType(StrEnum):
    TEXT = "text"
    RICH_HTML = "rich_html"
    TEMPLATE = "template"
    MEDIA = "media"
    QUOTE_CARD = "quote_card"
    ORDER_CARD = "order_card"


class DeliveryStatus(StrEnum):
    QUEUED = "queued"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"


class IdentityType(StrEnum):
    EMAIL = "email"
    PHONE_E164 = "phone_e164"
    WHATSAPP_WAID = "whatsapp_waid"
    WHATSAPP_BSUID = "whatsapp_bsuid"
    IG_PSID = "ig_psid"
    FB_PSID = "fb_psid"
    SHOPIFY_CUSTOMER_ID = "shopify_customer_id"
    TIKTOK_BUYER_ID = "tiktok_buyer_id"
    AMAZON_BUYER_EMAIL = "amazon_buyer_email"
    WEB_VISITOR_ID = "web_visitor_id"
    MARKETPLACE_BUYER_ID = "marketplace_buyer_id"


class ConfidenceLevel(StrEnum):
    DETERMINISTIC = "deterministic"
    HUMAN_VERIFIED = "human_verified"
    PROBABILISTIC = "probabilistic"


class CustomerType(StrEnum):
    B2C_CONSUMER = "b2c_consumer"
    B2B_CONTACT = "b2b_contact"
    B2B_COMPANY_REP = "b2b_company_rep"


class LeadStatus(StrEnum):
    NEW = "new"
    CONTACTED = "contacted"
    QUALIFIED = "qualified"
    DISQUALIFIED = "disqualified"
    CONVERTED = "converted"


class OpportunityStage(StrEnum):
    DISCOVERY = "discovery"
    SAMPLING = "sampling"
    QUOTING = "quoting"
    NEGOTIATING = "negotiating"
    CLOSED_WON = "closed_won"
    CLOSED_LOST = "closed_lost"


class Incoterm(StrEnum):
    EXW = "EXW"
    FOB = "FOB"
    CIF = "CIF"
    DDP = "DDP"
    DAP = "DAP"


class QuoteStatus(StrEnum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    SENT = "sent"
    ACCEPTED = "accepted"
    EXPIRED = "expired"
    CONVERTED = "converted"


class OrderMode(StrEnum):
    B2C_RETAIL = "b2c_retail"
    B2B_SAMPLE = "b2b_sample"
    B2B_WHOLESALE = "b2b_wholesale"


class FulfillmentStatus(StrEnum):
    PENDING_PAYMENT = "pending_payment"
    CONFIRMED_RESERVED = "confirmed_reserved"
    IN_PRODUCTION = "in_production"
    PARTIALLY_SHIPPED = "partially_shipped"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    EXCEPTION = "exception"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class PaymentStatus(StrEnum):
    UNPAID = "unpaid"
    DEPOSIT_PAID = "deposit_paid"
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"
    REFUNDED = "refunded"


class InventoryEventType(StrEnum):
    INBOUND_PO = "inbound_po"
    RESERVE_ORDER = "reserve_order"
    RELEASE_CANCEL = "release_cancel"
    DEDUCT_SHIPPED = "deduct_shipped"
    STOCKTAKE_ADJUST = "stocktake_adjust"


class TrackingProvider(StrEnum):
    TRACK17 = "17track"
    AFTERSHIP = "aftership"
    MANUAL = "manual"


class ProposalType(StrEnum):
    REPLY_DRAFT = "reply_draft"
    CRM_ENTITY_EXTRACT = "crm_entity_extract"
    QUOTE_DRAFT = "quote_draft"
    FOLLOW_UP_SUGGESTION = "follow_up_suggestion"
    IDENTITY_MERGE = "identity_merge"
    REFUND_OR_RESHIP = "refund_or_reship"
    PAYMENT_MATCH = "payment_match"


class ProposalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    EDITED_AND_APPROVED = "edited_and_approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    FAILED = "failed"


class AutonomyLevel(StrEnum):
    L0_COPILOT = "L0_copilot"
    L1_PROPOSAL = "L1_proposal"
    L2_AUTO = "L2_auto"


class AiCapability(StrEnum):
    CS_SUPPORT = "cs_support"
    SALES_QUOTING = "sales_quoting"
    ORDER_LOGISTICS = "order_logistics"
    CONTENT_STUDIO = "content_studio"
    BIZ_ANALYTICS = "biz_analytics"
    FINANCE_RECON = "finance_recon"


class UsageMetricType(StrEnum):
    AI_CREDIT = "ai_credit"
    WHATSAPP_TEMPLATE = "whatsapp_template"
    WHATSAPP_SERVICE = "whatsapp_service"
    TRACKING_QUOTA = "tracking_quota"
