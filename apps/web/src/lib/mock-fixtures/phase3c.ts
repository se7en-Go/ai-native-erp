/**
 * Phase 3C mock fixtures. Strictly UI fixtures for `NEXT_PUBLIC_USE_MOCK_API=true`;
 * the live path consumes the generated `@hey-api/openapi-ts` SDK instead.
 *
 * Values mirror the `--with-demo-data` seed (one completed global-map job with six
 * graded prospects across Dubai / Los Angeles / Milan / Jakarta / Dublin / Lagos,
 * plus one compliant email campaign draft) so mocked screens match the demo stack.
 */
import type {
  CampaignPreview,
  CampaignRead,
  CollectionSummary,
  LeadCollectionJobRead,
  ProspectRead,
} from "@/lib/api-client/types.gen";

const NOW = "2026-11-19T09:00:00Z";
const TENANT = "22222222-2222-2222-2222-222222222222";
const OWNER = "11111111-1111-1111-1111-111111111111";

export const mockGrowthSummary: CollectionSummary = {
  collected: 6,
  cleaned: 5,
  synced: 0,
  jobs: 1,
};

export const mockGrowthJob: LeadCollectionJobRead = {
  id: "job-global-map",
  tenant_id: TENANT,
  source_provider: "overture_poi",
  keywords: ["led lighting distributor", "wholesale lighting"],
  target_region: "Dubai / Los Angeles / Milan / Jakarta",
  max_results: 50,
  min_rating: "4.00",
  require_phone: false,
  status: "completed",
  collected_count: 6,
  cleaned_count: 5,
  synced_count: 0,
  error_message: null,
  created_by: OWNER,
  created_at: NOW,
  completed_at: NOW,
};

function prospect(
  id: string,
  companyName: string,
  contactName: string | null,
  phone: string | null,
  email: string | null,
  website: string | null,
  country: string,
  city: string,
  latitude: number,
  longitude: number,
  rating: string | null,
  grade: ProspectRead["grade"],
  syncStatus: ProspectRead["sync_status"]
): ProspectRead {
  return {
    id,
    tenant_id: TENANT,
    job_id: mockGrowthJob.id,
    company_name: companyName,
    contact_name: contactName,
    phone_e164: phone,
    email,
    website,
    country,
    city,
    latitude,
    longitude,
    rating,
    grade,
    whatsapp_reachable: phone !== null,
    sync_status: syncStatus,
    crm_lead_id: null,
    raw_metadata: {
      provider: "overture_poi",
      license: "CDLA-Permissive-2.0",
      demo: true,
    },
    created_at: NOW,
  };
}

export const mockGrowthProspects: ProspectRead[] = [
  prospect(
    "prospect-gulf",
    "Gulf Lighting Trading LLC",
    "Aisha Khan",
    "+97145550100",
    "aisha@gulf-lighting.example.ae",
    "https://gulf-lighting.example.ae",
    "AE",
    "Dubai",
    25.2048,
    55.2708,
    "4.80",
    "A",
    "cleaned"
  ),
  prospect(
    "prospect-pacific",
    "Pacific LED Wholesale Inc.",
    "Dana Reed",
    "+12135550142",
    "buyers@pacificled.example.com",
    "https://pacificled.example.com",
    "US",
    "Los Angeles",
    34.0522,
    -118.2437,
    "4.50",
    "A",
    "cleaned"
  ),
  prospect(
    "prospect-milan",
    "Milan Illuminazione SRL",
    "Marco Rossi",
    "+390255550111",
    "info@milanilluminazione.example.it",
    "https://milanilluminazione.example.it",
    "IT",
    "Milan",
    45.4642,
    9.19,
    "4.20",
    "A",
    "cleaned"
  ),
  prospect(
    "prospect-jakarta",
    "Jakarta Cahaya Lighting",
    "Budi Santoso",
    "+622155501234",
    "budi@cahaya-lighting.example.id",
    "https://cahaya-lighting.example.id",
    "ID",
    "Jakarta",
    -6.2088,
    106.8456,
    "4.10",
    "A",
    "cleaned"
  ),
  prospect(
    "prospect-dublin",
    "Shannon Retail Lighting",
    "Aoife Byrne",
    "+35315550123",
    null,
    null,
    "IE",
    "Dublin",
    53.3498,
    -6.2603,
    "4.00",
    "B",
    "cleaned"
  ),
  prospect(
    "prospect-lagos",
    "Lagos Lighting Mart",
    null,
    null,
    null,
    "https://lagos-lighting.example.ng",
    "NG",
    "Lagos",
    6.5244,
    3.3792,
    null,
    "C",
    "raw"
  ),
];

export const mockGrowthCampaign: CampaignRead = {
  id: "campaign-dubai-milan",
  tenant_id: TENANT,
  name: "Dubai & Milan LED wholesale outreach",
  channel_type: "email",
  subject: "2026 COB strip catalogue and FOB pricing",
  body_template:
    "Hello {{company_name}}, our 2026 COB strip catalogue is ready. Reply STOP or use the one-click unsubscribe link to opt out.",
  status: "draft",
  total_targets: 6,
  sent_count: 0,
  suppressed_count: 0,
  replied_count: 0,
  created_by: OWNER,
  created_at: NOW,
  updated_at: NOW,
};

export const mockGrowthPreview: CampaignPreview = {
  campaign_id: mockGrowthCampaign.id,
  channel_type: "email",
  eligible_count: 4,
  suppressed_count: 2,
  reasons: [
    { reason: "地址缺失", count: 1 },
    { reason: "命中退订/抑制名单", count: 1 },
  ],
  total_targets: 6,
  list_unsubscribe_header: `<https://erp.local/api/v1/growth/unsubscribe?campaign=${mockGrowthCampaign.id}&token={token}>, <mailto:unsubscribe@erp.local?subject=unsubscribe-${mockGrowthCampaign.id}>`,
  rate_limit_remaining: null,
};
