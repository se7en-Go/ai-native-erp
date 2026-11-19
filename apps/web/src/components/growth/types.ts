export type LeadSourceProvider = "google_places" | "overpass_osm" | "overture_poi" | "csv_import";

interface LeadCollectionFilters {
  keywords: readonly string[];
  target_region: string;
  max_results: number;
  /** Decimal represented as text at the API boundary. */
  min_rating: string | null;
  require_phone: boolean;
}

export type LeadCollectionRequest = LeadCollectionFilters & (
  { source_provider: Exclude<LeadSourceProvider, "csv_import"> }
  | { source_provider: "csv_import"; file: File }
);

export interface LeadCollectionJob extends LeadCollectionFilters {
  id: string;
  source_provider: LeadSourceProvider;
  status: string;
  collected_count: number;
  cleaned_count: number;
  synced_count: number;
  error_message?: string | null;
}

export interface CollectionMetrics { collected: number; cleaned: number; synced: number; jobs: number }

export type ProspectGrade = "A" | "B" | "C";
export type ProspectSyncStatus = "raw" | "cleaned" | "synced_to_crm" | "discarded";
export interface Prospect {
  id: string;
  company_name: string;
  city?: string | null;
  country?: string | null;
  contact_name?: string | null;
  phone_e164?: string | null;
  email?: string | null;
  website?: string | null;
  latitude: number | null;
  longitude: number | null;
  grade: ProspectGrade;
  rating: string | null;
  sync_status: ProspectSyncStatus;
  whatsapp_reachable: boolean | null;
  crm_lead_id?: string | null;
}

export type CampaignChannel = "email" | "whatsapp_cloud";
export type CampaignStatus = "draft" | "scheduled" | "running" | "paused" | "completed";
export interface CampaignDraft {
  name: string;
  channel_type: CampaignChannel;
  subject: string | null;
  body_template: string;
  target_ids: readonly string[];
}

export interface MarketingCampaign extends Omit<CampaignDraft, "target_ids"> {
  id: string;
  status: CampaignStatus;
  total_targets: number;
  sent_count: number;
  suppressed_count: number;
  replied_count: number;
}

/** Authoritative snapshot from the backend; never a delivery authorization. */
export interface SuppressionPreview {
  eligible_count: number;
  suppressed_count: number;
  reasons: readonly { reason: string; count: number }[];
}
