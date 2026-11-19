// Phase 3C data layer: generated-SDK calls by default, fixture-backed responses
// only when NEXT_PUBLIC_USE_MOCK_API=true (same switch as data-source.ts).
import * as live from "@/lib/api-client/phase3c";
import type {
  CampaignLaunchResult,
  CollectionSummary,
  ProspectCleanResult,
} from "@/lib/api-client/types.gen";
import type {
  CampaignDraft,
  CollectionMetrics,
  LeadCollectionJob,
  LeadCollectionRequest,
  MarketingCampaign,
  Prospect,
  SuppressionPreview,
} from "@/components/growth/types";
import {
  mockGrowthCampaign,
  mockGrowthJob,
  mockGrowthPreview,
  mockGrowthProspects,
  mockGrowthSummary,
} from "@/lib/mock-fixtures/phase3c";
import { shouldUseMockApi } from "@/lib/data-source";

/** Stateful only in mock mode: mutations land here so the page re-reads what it wrote. */
const mockState = {
  summary: { ...mockGrowthSummary } as CollectionSummary,
  jobs: [{ ...mockGrowthJob }] as LeadCollectionJob[],
  prospects: mockGrowthProspects.map((row) => ({ ...row })),
  campaigns: [{ ...mockGrowthCampaign }] as MarketingCampaign[],
};

function recomputeMockSummary(): void {
  mockState.summary = {
    collected: mockState.prospects.length,
    cleaned: mockState.prospects.filter(
      (row) => row.sync_status === "cleaned" || row.sync_status === "synced_to_crm"
    ).length,
    synced: mockState.prospects.filter((row) => row.sync_status === "synced_to_crm").length,
    jobs: mockState.jobs.length,
  };
}

export function fetchCollectionMetrics(): Promise<CollectionMetrics> {
  if (shouldUseMockApi()) return Promise.resolve({ ...mockState.summary });
  return live.fetchCollectionSummary();
}

export function fetchLeadJobs(): Promise<LeadCollectionJob[]> {
  if (shouldUseMockApi()) return Promise.resolve(mockState.jobs.map((row) => ({ ...row })));
  return live.fetchJobs();
}

export function fetchGrowthProspects(query: live.ProspectQuery = {}): Promise<Prospect[]> {
  if (shouldUseMockApi()) {
    const rows = mockState.prospects.map(live.mapProspect);
    return Promise.resolve(
      rows.filter(
        (row) =>
          (!query.grade || row.grade === query.grade) &&
          (!query.sync_status || row.sync_status === query.sync_status) &&
          (!query.job_id || true) &&
          (!query.q ||
            `${row.company_name} ${row.city ?? ""} ${row.email ?? ""}`
              .toLowerCase()
              .includes(query.q.toLowerCase()))
      )
    );
  }
  return live.fetchProspects(query);
}

/**
 * Panel entry point: create the job, then (for map/POI sources) run it and clean the
 * new rows so the table and map show graded prospects immediately.
 */
export async function startCollection(request: LeadCollectionRequest): Promise<LeadCollectionJob> {
  if (shouldUseMockApi()) {
    const job: LeadCollectionJob = {
      id: `job-mock-${mockState.jobs.length + 1}`,
      source_provider: request.source_provider,
      keywords: [...request.keywords],
      target_region: request.target_region,
      max_results: request.max_results,
      min_rating: request.min_rating,
      require_phone: request.require_phone,
      status: "completed",
      collected_count: request.source_provider === "csv_import" ? 2 : 1,
      cleaned_count: 0,
      synced_count: 0,
      error_message: null,
    };
    mockState.jobs = [job, ...mockState.jobs];
    if (request.source_provider === "csv_import") {
      const company = request.file.name.replace(/\.csv$/i, "") || "CSV 导入企业";
      mockState.prospects = [
        ...mockState.prospects,
        {
          ...mockGrowthProspects[4],
          id: `prospect-mock-${mockState.prospects.length + 1}`,
          company_name: company,
          sync_status: "raw",
          crm_lead_id: null,
        },
      ];
    }
    recomputeMockSummary();
    return job;
  }
  const created = await live.createJob(request);
  if (created.status === "completed") {
    // CSV imports execute at creation because the payload is request-scoped.
    await live.cleanProspects();
    return created;
  }
  const completed = await live.runJob(created.id);
  await live.cleanProspects();
  return completed;
}

export function cleanGrowthProspects(
  prospectIds?: readonly string[]
): Promise<ProspectCleanResult> {
  if (shouldUseMockApi()) {
    const scope = mockState.prospects.filter(
      (row) => row.sync_status === "raw" && (!prospectIds || prospectIds.includes(row.id))
    );
    for (const row of scope) row.sync_status = "cleaned";
    recomputeMockSummary();
    return Promise.resolve({
      cleaned: scope.length,
      discarded_duplicates: 0,
      graded: { A: 0, B: 0, C: scope.length },
      items: scope.map((row) => ({ ...row, sync_status: "cleaned" as const })),
    });
  }
  return live.cleanProspects(prospectIds);
}

export function syncGrowthProspectsToCrm(
  prospectIds: readonly string[]
): Promise<{ synced: number; skipped: number }> {
  if (shouldUseMockApi()) {
    const scope = mockState.prospects.filter(
      (row) => prospectIds.includes(row.id) && row.sync_status === "cleaned"
    );
    for (const row of scope) row.sync_status = "synced_to_crm";
    recomputeMockSummary();
    return Promise.resolve({ synced: scope.length, skipped: prospectIds.length - scope.length });
  }
  return live.syncProspectsToCrm(prospectIds);
}

/** Trigger a real browser download of the server-rendered CSV. */
export async function downloadGrowthProspectsCsv(prospectIds?: readonly string[]): Promise<void> {
  const text = shouldUseMockApi()
    ? 'company_name,phone_e164,email\r\n"Gulf Lighting Trading LLC","+97145550100","aisha@gulf-lighting.example.ae"\r\n'
    : await live.fetchProspectsCsv(prospectIds);
  if (typeof URL.createObjectURL !== "function") return;
  const url = URL.createObjectURL(new Blob([text], { type: "text/csv;charset=utf-8" }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "growth-prospects.csv";
  anchor.click();
  URL.revokeObjectURL(url);
}

export function fetchMarketingCampaigns(): Promise<MarketingCampaign[]> {
  if (shouldUseMockApi()) return Promise.resolve(mockState.campaigns.map((row) => ({ ...row })));
  return live.fetchCampaigns();
}

export async function saveCampaignDraft(draft: CampaignDraft): Promise<MarketingCampaign> {
  if (shouldUseMockApi()) {
    const campaign: MarketingCampaign = {
      id: `campaign-mock-${mockState.campaigns.length + 1}`,
      name: draft.name,
      channel_type: draft.channel_type,
      subject: draft.subject,
      body_template: draft.body_template,
      status: "draft",
      total_targets: draft.target_ids.length,
      sent_count: 0,
      suppressed_count: 0,
      replied_count: 0,
    };
    mockState.campaigns = [campaign, ...mockState.campaigns];
    return campaign;
  }
  return live.createCampaign(draft);
}

export function previewMarketingCampaign(campaignId: string): Promise<SuppressionPreview> {
  if (shouldUseMockApi()) return Promise.resolve(mockGrowthPreview);
  return live.previewCampaign(campaignId);
}

export async function launchMarketingCampaign(campaignId: string): Promise<CampaignLaunchResult> {
  if (shouldUseMockApi()) {
    const campaign = mockState.campaigns.find((row) => row.id === campaignId);
    if (campaign) {
      campaign.status = "completed";
      campaign.sent_count = mockGrowthPreview.eligible_count;
      campaign.suppressed_count = mockGrowthPreview.suppressed_count;
    }
    return {
      campaign_id: campaignId,
      status: "completed",
      sent: mockGrowthPreview.eligible_count,
      suppressed: mockGrowthPreview.suppressed_count,
      queued_remaining: 0,
      reasons: mockGrowthPreview.reasons,
    };
  }
  return live.launchCampaign(campaignId);
}

/** Campaign targets come from the cleaned prospect pool (never from a hidden list). */
export function campaignTargetIds(prospects: readonly Prospect[]): string[] {
  return prospects
    .filter((row) => row.sync_status !== "discarded" && row.sync_status !== "raw")
    .map((row) => row.id);
}
