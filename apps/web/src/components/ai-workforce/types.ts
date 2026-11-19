import type { AiCapability, AutonomyLevel } from "@/lib/contracts";

export interface DigitalEmployee {
  id: string;
  display_name: string;
  role_title: string;
  department: string;
  avatar_emoji?: string | null;
  description: string;
  capability_codes: readonly AiCapability[];
  autonomy_level: AutonomyLevel;
  is_enabled: boolean;
  tool_count: number;
}

export interface WorkforceMetrics {
  totalEmployees: number;
  enabledEmployees: number;
  actionsLast7Days: number;
  /** Decimal from the usage ledger; never convert to a JS number. */
  llmCostTodayUsd: string | null;
  autonomyMode: AutonomyLevel;
}

export type DivisionDestination = { kind: "proposal" | "policy" | "activity"; id: string };
export interface AutonomyDivisionItem {
  id: string;
  level: AutonomyLevel;
  title: string;
  description?: string;
  count?: number;
  target?: DivisionDestination;
}

export type InboxAssistAction = "polish" | "summarize" | "suggest_reply" | "daily_brief";
/** Entry point for the customer-service UI, not a new backend assist action. */
export type InboxToolbarIntent = InboxAssistAction | "customer_service";

export interface QuickReplyTemplate {
  id: string;
  title: string;
  body_text: string;
  category: string;
  language: string;
  shortcut_code?: string | null;
}

export type CreativeAssetType = "product_copy" | "listing" | "email_template" | "social_caption" | "ad_hook";
export interface CreativeAssetRequest {
  asset_type: CreativeAssetType;
  sku_ref: string | null;
  languages: readonly string[];
  prompt: string;
}

export interface CreativeAsset {
  id?: string;
  asset_type: CreativeAssetType;
  title: string;
  language: string;
  sku_ref: string | null;
  prompt?: string;
  content_text: string;
  status?: string;
}

export interface SkuOption {
  sku: string;
  label: string;
}
