// Phase 3C live adapters over the generated @hey-api/openapi-ts SDK.
// Every request goes through the shared BFF-configured client (@/lib/api), so
// session cookies and the 401 refresh flow behave exactly like bffFetch.
// Zero SQL/ORM: the browser only ever sees the typed /api/v1/growth contract.
import { getBffClient } from "@/lib/api";
import { unwrap } from "@/lib/api-client/live-client";
import type {
  CampaignCreate,
  CampaignLaunchResult,
  CampaignPreview,
  CampaignRead,
  CampaignUpdate,
  CollectionSummary,
  LeadCollectionJobCreate,
  LeadCollectionJobRead,
  ProspectCleanResult,
  ProspectRead,
  SyncToCrmResult,
} from "@/lib/api-client/types.gen";
import {
  cleanProspectsApiV1GrowthProspectsCleanPost,
  collectionSummaryApiV1GrowthSummaryGet,
  createCampaignApiV1GrowthCampaignsPost,
  createJobApiV1GrowthJobsPost,
  exportProspectsApiV1GrowthProspectsExportCsvGet,
  launchCampaignApiV1GrowthCampaignsCampaignIdLaunchPost,
  listCampaignsApiV1GrowthCampaignsGet,
  listJobsApiV1GrowthJobsGet,
  listProspectsApiV1GrowthProspectsGet,
  previewCampaignApiV1GrowthCampaignsCampaignIdPreviewPost,
  runJobApiV1GrowthJobsJobIdRunPost,
  syncToCrmApiV1GrowthProspectsSyncCrmPost,
  updateCampaignApiV1GrowthCampaignsCampaignIdPatch,
} from "@/lib/api-client/sdk.gen";
import type {
  CampaignDraft,
  LeadCollectionJob,
  LeadCollectionRequest,
  MarketingCampaign,
  Prospect,
  SuppressionPreview,
} from "@/components/growth/types";

const client = () => getBffClient();

/** The contract row mapped onto the collection panel/table shapes. */
export function mapJob(job: LeadCollectionJobRead): LeadCollectionJob {
  return {
    id: job.id,
    source_provider: job.source_provider,
    keywords: job.keywords,
    target_region: job.target_region ?? "",
    max_results: job.max_results,
    // Decimal ratings stay strings end to end; Number() would lose the second digit.
    min_rating: typeof job.min_rating === "string" ? job.min_rating : null,
    require_phone: job.require_phone,
    status: job.status,
    collected_count: job.collected_count,
    cleaned_count: job.cleaned_count,
    synced_count: job.synced_count,
    error_message: job.error_message,
  };
}

export function mapProspect(row: ProspectRead): Prospect {
  return {
    id: row.id,
    company_name: row.company_name,
    contact_name: row.contact_name,
    phone_e164: row.phone_e164,
    email: row.email,
    website: row.website,
    country: row.country,
    city: row.city,
    latitude: row.latitude,
    longitude: row.longitude,
    grade: row.grade,
    rating: row.rating,
    sync_status: row.sync_status,
    whatsapp_reachable: row.whatsapp_reachable,
    crm_lead_id: row.crm_lead_id,
  };
}

export function mapCampaign(row: CampaignRead): MarketingCampaign {
  return {
    id: row.id,
    name: row.name,
    channel_type: row.channel_type,
    subject: row.subject,
    body_template: row.body_template,
    status: row.status,
    total_targets: row.total_targets,
    sent_count: row.sent_count,
    suppressed_count: row.suppressed_count,
    replied_count: row.replied_count,
  };
}

/** A preview is an authoritative snapshot, never a delivery authorization. */
export function mapPreview(preview: CampaignPreview): SuppressionPreview {
  return {
    eligible_count: preview.eligible_count,
    suppressed_count: preview.suppressed_count,
    reasons: preview.reasons.map((row) => ({ reason: row.reason, count: row.count })),
  };
}

/** Read the CSV text; older jsdom/File implementations only expose FileReader. */
function readFileText(file: File): Promise<string> {
  if (typeof file.text === "function") return file.text();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () => reject(reader.error ?? new Error("Unable to read the CSV file"));
    reader.readAsText(file);
  });
}

/** Build the create payload from the panel request; CSV bytes are read client-side. */
export async function toJobPayload(request: LeadCollectionRequest): Promise<LeadCollectionJobCreate> {
  const filters = {
    keywords: [...request.keywords],
    target_region: request.target_region || null,
    max_results: request.max_results,
    min_rating: request.min_rating,
    require_phone: request.require_phone,
  };
  if (request.source_provider === "csv_import") {
    return {
      ...filters,
      source_provider: "csv_import",
      csv_content: await readFileText(request.file),
    };
  }
  return { ...filters, source_provider: request.source_provider };
}

export function fetchCollectionSummary(): Promise<CollectionSummary> {
  return collectionSummaryApiV1GrowthSummaryGet({ client: client() }).then(
    (response) => unwrap(response) as CollectionSummary
  );
}

export function fetchJobs(): Promise<LeadCollectionJob[]> {
  return listJobsApiV1GrowthJobsGet({ client: client(), query: { limit: 20 } }).then(
    (response) => unwrap(response).items.map(mapJob)
  );
}

export async function createJob(request: LeadCollectionRequest): Promise<LeadCollectionJob> {
  const body = await toJobPayload(request);
  const created = await createJobApiV1GrowthJobsPost({ client: client(), body });
  return mapJob(unwrap(created));
}

export function runJob(jobId: string): Promise<LeadCollectionJob> {
  return runJobApiV1GrowthJobsJobIdRunPost({
    client: client(),
    path: { job_id: jobId },
  }).then((response) => mapJob(unwrap(response)));
}

export interface ProspectQuery {
  grade?: string;
  sync_status?: string;
  job_id?: string;
  q?: string;
}

export function fetchProspects(query: ProspectQuery = {}): Promise<Prospect[]> {
  return listProspectsApiV1GrowthProspectsGet({
    client: client(),
    query: { limit: 100, ...query },
  }).then((response) => unwrap(response).items.map(mapProspect));
}

export function cleanProspects(prospectIds?: readonly string[]): Promise<ProspectCleanResult> {
  return cleanProspectsApiV1GrowthProspectsCleanPost({
    client: client(),
    body: { prospect_ids: prospectIds ? [...prospectIds] : null },
  }).then((response) => unwrap(response) as ProspectCleanResult);
}

export function syncProspectsToCrm(prospectIds: readonly string[]): Promise<SyncToCrmResult> {
  return syncToCrmApiV1GrowthProspectsSyncCrmPost({
    client: client(),
    body: { prospect_ids: [...prospectIds] },
  }).then((response) => unwrap(response) as SyncToCrmResult);
}

/** Server-rendered RFC 4180 CSV (formula-injection neutralized) for download. */
export function fetchProspectsCsv(prospectIds?: readonly string[]): Promise<string> {
  return exportProspectsApiV1GrowthProspectsExportCsvGet({
    client: client(),
    query: prospectIds?.length ? { ids: [...prospectIds] } : undefined,
    parseAs: "text",
  }).then((response) => unwrap(response) as unknown as string);
}

export function fetchCampaigns(): Promise<MarketingCampaign[]> {
  return listCampaignsApiV1GrowthCampaignsGet({ client: client(), query: { limit: 50 } }).then(
    (response) => unwrap(response).items.map(mapCampaign)
  );
}

export function createCampaign(draft: CampaignDraft): Promise<MarketingCampaign> {
  const body: CampaignCreate = {
    name: draft.name,
    channel_type: draft.channel_type,
    subject: draft.subject,
    body_template: draft.body_template,
    target_ids: [...draft.target_ids],
  };
  return createCampaignApiV1GrowthCampaignsPost({ client: client(), body }).then((response) =>
    mapCampaign(unwrap(response))
  );
}

export function updateCampaign(
  campaignId: string,
  patch: CampaignUpdate
): Promise<MarketingCampaign> {
  return updateCampaignApiV1GrowthCampaignsCampaignIdPatch({
    client: client(),
    path: { campaign_id: campaignId },
    body: patch,
  }).then((response) => mapCampaign(unwrap(response)));
}

export function previewCampaign(campaignId: string): Promise<SuppressionPreview> {
  return previewCampaignApiV1GrowthCampaignsCampaignIdPreviewPost({
    client: client(),
    path: { campaign_id: campaignId },
  }).then((response) => mapPreview(unwrap(response)));
}

export function launchCampaign(campaignId: string): Promise<CampaignLaunchResult> {
  return launchCampaignApiV1GrowthCampaignsCampaignIdLaunchPost({
    client: client(),
    path: { campaign_id: campaignId },
  }).then((response) => unwrap(response) as CampaignLaunchResult);
}

export type {
  CampaignLaunchResult,
  CollectionSummary,
  ProspectCleanResult,
  SyncToCrmResult,
};
