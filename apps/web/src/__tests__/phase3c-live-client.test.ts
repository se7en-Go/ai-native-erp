// Phase 3C live adapter tests: the generated SDK must hit the exact BFF URLs
// from contracts/openapi.json, keep Decimal ratings as strings, map the contract
// onto the growth UI shapes, and never serialize provider data in the browser.
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

import type {
  CampaignPreview,
  CampaignRead,
  LeadCollectionJobRead,
  ProspectRead,
} from "@/lib/api-client/types.gen";
import {
  cleanProspects,
  createCampaign,
  createJob,
  fetchCampaigns,
  fetchCollectionSummary,
  fetchJobs,
  fetchProspects,
  fetchProspectsCsv,
  launchCampaign,
  mapCampaign,
  mapJob,
  mapPreview,
  mapProspect,
  previewCampaign,
  syncProspectsToCrm,
  toJobPayload,
} from "@/lib/api-client/phase3c";

const BFF = "http://localhost:3000/api/bff";

interface CapturedCall {
  url: string;
  method: string | undefined;
  bodyText: string | null;
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function stubFetch(handler: (call: CapturedCall) => Response) {
  const captured: CapturedCall[] = [];
  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const call: CapturedCall =
      input instanceof Request
        ? { url: input.url, method: input.method, bodyText: await input.text() }
        : {
            url: String(input),
            method: init?.method,
            bodyText: typeof init?.body === "string" ? init.body : null,
          };
    captured.push(call);
    return handler(call);
  });
  vi.stubGlobal("fetch", mock);
  return captured;
}

const page = (items: unknown[]) => ({
  items,
  total: items.length,
  limit: 50,
  offset: 0,
  has_more: false,
});

const jobRow: LeadCollectionJobRead = {
  id: "job-1",
  tenant_id: "tenant-1",
  source_provider: "google_places",
  keywords: ["led lighting"],
  target_region: "Milan, IT",
  max_results: 50,
  min_rating: "4.50",
  require_phone: true,
  status: "completed",
  collected_count: 6,
  cleaned_count: 5,
  synced_count: 1,
  error_message: null,
  created_by: "user-1",
  created_at: "2026-11-19T09:00:00Z",
  completed_at: "2026-11-19T09:00:05Z",
};

const prospectRow: ProspectRead = {
  id: "prospect-1",
  tenant_id: "tenant-1",
  job_id: "job-1",
  company_name: "Milan Illuminazione SRL",
  contact_name: "Marco Rossi",
  phone_e164: "+390255550111",
  email: "info@milanilluminazione.example.it",
  website: "https://milanilluminazione.example.it",
  country: "IT",
  city: "Milan",
  latitude: 45.4642,
  longitude: 9.19,
  rating: "4.60",
  grade: "A",
  whatsapp_reachable: true,
  sync_status: "cleaned",
  crm_lead_id: null,
  raw_metadata: { provider: "google_places_new", google_place_id: "place-42" },
  created_at: "2026-11-19T09:00:00Z",
};

const campaignRow: CampaignRead = {
  id: "campaign-1",
  tenant_id: "tenant-1",
  name: "Dubai LED wholesale outreach",
  channel_type: "email",
  subject: "COB strip catalogue",
  body_template: "Hello {{company_name}},",
  status: "draft",
  total_targets: 3,
  sent_count: 0,
  suppressed_count: 0,
  replied_count: 0,
  created_by: "user-1",
  created_at: "2026-11-19T09:00:00Z",
  updated_at: "2026-11-19T09:00:00Z",
};

const previewRow: CampaignPreview = {
  campaign_id: "campaign-1",
  channel_type: "email",
  eligible_count: 1,
  suppressed_count: 2,
  reasons: [
    { reason: "命中退订/抑制名单", count: 1 },
    { reason: "地址缺失", count: 1 },
  ],
  total_targets: 3,
  list_unsubscribe_header: "<https://erp.local/u?token={token}>, <mailto:u@erp.local>",
  rate_limit_remaining: null,
};

describe("Phase 3C live adapters", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "false");
  });
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("maps the job contract without inventing counts and keeps Decimals as strings", () => {
    const job = mapJob(jobRow);
    expect(job).toMatchObject({
      id: "job-1",
      source_provider: "google_places",
      target_region: "Milan, IT",
      min_rating: "4.50",
      collected_count: 6,
      cleaned_count: 5,
      synced_count: 1,
      error_message: null,
    });
    const prospect = mapProspect(prospectRow);
    expect(prospect.rating).toBe("4.60");
    expect(prospect.grade).toBe("A");
    expect(prospect.sync_status).toBe("cleaned");
    expect(prospect.latitude).toBe(45.4642);
  });

  it("maps campaigns and the compliance preview without dropping reasons", () => {
    expect(mapCampaign(campaignRow)).toMatchObject({
      id: "campaign-1",
      channel_type: "email",
      total_targets: 3,
      suppressed_count: 0,
    });
    expect(mapPreview(previewRow)).toEqual({
      eligible_count: 1,
      suppressed_count: 2,
      reasons: [
        { reason: "命中退订/抑制名单", count: 1 },
        { reason: "地址缺失", count: 1 },
      ],
    });
  });

  it("reads the KPI summary and lists jobs/prospects from the growth endpoints", async () => {
    const captured = stubFetch((call) => {
      if (call.url.includes("/growth/summary")) {
        return jsonResponse({ collected: 6, cleaned: 5, synced: 1, jobs: 2 });
      }
      if (call.url.includes("/growth/jobs")) return jsonResponse(page([jobRow]));
      return jsonResponse(page([prospectRow]));
    });

    expect(await fetchCollectionSummary()).toEqual({
      collected: 6,
      cleaned: 5,
      synced: 1,
      jobs: 2,
    });
    expect(await fetchJobs()).toHaveLength(1);
    expect(await fetchProspects({ grade: "A", sync_status: "cleaned" })).toHaveLength(1);

    expect(captured[0].url).toBe(`${BFF}/api/v1/growth/summary`);
    expect(captured[1].url).toBe(`${BFF}/api/v1/growth/jobs?limit=20`);
    expect(captured[2].url).toBe(
      `${BFF}/api/v1/growth/prospects?limit=100&grade=A&sync_status=cleaned`
    );
  });

  it("creates a map job without csv_content and a CSV job with the file text inline", async () => {
    const captured = stubFetch(() => jsonResponse(jobRow, 201));

    await createJob({
      source_provider: "overpass_osm",
      keywords: ["lighting"],
      target_region: "Berlin, DE",
      max_results: 15,
      min_rating: "4.25",
      require_phone: true,
    });
    expect(captured[0].url).toBe(`${BFF}/api/v1/growth/jobs`);
    expect(captured[0].method?.toLowerCase()).toBe("post");
    expect(JSON.parse(captured[0].bodyText ?? "{}")).toEqual({
      keywords: ["lighting"],
      target_region: "Berlin, DE",
      max_results: 15,
      min_rating: "4.25",
      require_phone: true,
      source_provider: "overpass_osm",
    });

    const file = new File(["company_name\nClinic"], "leads.csv", { type: "text/csv" });
    const payload = await toJobPayload({
      source_provider: "csv_import",
      keywords: [],
      target_region: "",
      max_results: 10,
      min_rating: null,
      require_phone: false,
      file,
    });
    expect(payload.csv_content).toBe("company_name\nClinic");
    expect(payload.source_provider).toBe("csv_import");
  });

  it("cleans, syncs and exports through the growth endpoints", async () => {
    const captured = stubFetch((call) => {
      if (call.url.includes("export.csv")) {
        return new Response("company_name\r\n\"Gulf\"\r\n", {
          status: 200,
          headers: { "Content-Type": "text/csv" },
        });
      }
      if (call.url.includes("sync-crm")) {
        return jsonResponse({
          synced: 1,
          skipped: 0,
          leads: [
            {
              prospect_id: "prospect-1",
              crm_lead_id: "lead-1",
              customer_id: "customer-1",
              company_id: "company-1",
            },
          ],
        });
      }
      return jsonResponse({
        cleaned: 1,
        discarded_duplicates: 0,
        graded: { A: 1, B: 0, C: 0 },
        items: [prospectRow],
      });
    });

    const cleaned = await cleanProspects(["prospect-1"]);
    expect(cleaned.graded.A).toBe(1);
    expect(captured[0].url).toBe(`${BFF}/api/v1/growth/prospects/clean`);
    expect(JSON.parse(captured[0].bodyText ?? "{}")).toEqual({
      prospect_ids: ["prospect-1"],
    });

    const synced = await syncProspectsToCrm(["prospect-1"]);
    expect(synced.synced).toBe(1);
    expect(captured[1].url).toBe(`${BFF}/api/v1/growth/prospects/sync-crm`);

    const csv = await fetchProspectsCsv(["prospect-1"]);
    expect(csv).toContain("Gulf");
    expect(captured[2].url).toBe(
      `${BFF}/api/v1/growth/prospects/export.csv?ids=prospect-1`
    );
  });

  it("creates campaigns and runs the server-side preview and launch", async () => {
    const captured = stubFetch((call) => {
      if (call.url.endsWith("/preview")) return jsonResponse(previewRow);
      if (call.url.endsWith("/launch")) {
        return jsonResponse({
          campaign_id: "campaign-1",
          status: "completed",
          sent: 1,
          suppressed: 2,
          queued_remaining: 0,
          reasons: previewRow.reasons,
        });
      }
      if (call.method?.toLowerCase() === "patch") return jsonResponse(campaignRow);
      if (call.url.includes("?")) return jsonResponse(page([campaignRow]));
      return jsonResponse(campaignRow, 201);
    });

    expect(await fetchCampaigns()).toHaveLength(1);
    expect(captured[0].url).toBe(`${BFF}/api/v1/growth/campaigns?limit=50`);

    await createCampaign({
      name: "Dubai LED wholesale outreach",
      channel_type: "email",
      subject: "COB strip catalogue",
      body_template: "Hello {{company_name}},",
      target_ids: ["prospect-1", "customer-1"],
    });
    expect(captured[1].url).toBe(`${BFF}/api/v1/growth/campaigns`);
    expect(JSON.parse(captured[1].bodyText ?? "{}")).toEqual({
      name: "Dubai LED wholesale outreach",
      channel_type: "email",
      subject: "COB strip catalogue",
      body_template: "Hello {{company_name}},",
      target_ids: ["prospect-1", "customer-1"],
    });

    expect((await previewCampaign("campaign-1")).suppressed_count).toBe(2);
    expect(captured[2].url).toBe(`${BFF}/api/v1/growth/campaigns/campaign-1/preview`);
    expect(captured[2].method?.toLowerCase()).toBe("post");

    const launched = await launchCampaign("campaign-1");
    expect(launched.sent).toBe(1);
    expect(captured[3].url).toBe(`${BFF}/api/v1/growth/campaigns/campaign-1/launch`);
  });
});
