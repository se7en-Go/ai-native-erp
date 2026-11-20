// Pack C live adapters: real overseas lead collection (multi-city / multi-keyword
// batch recipe), website e-mail enrichment and real email outreach
// (Resend / SMTP) on top of the generated `@hey-api/openapi-ts` SDK.
//
// Same rules as phase3c.ts:
// * every call goes through the shared BFF-configured client (`@/lib/api`) so
//   session cookies and the 401-refresh flow behave exactly like bffFetch;
// * the browser only ever sees the typed `/api/v1/growth` contract — zero SQL,
//   zero ORM, no provider credentials;
// * fixtures are used only when `shouldUseMockApi()` is true, never silently on
//   the live path.
import { getBffClient } from "@/lib/api";
import { unwrap } from "@/lib/api-client/live-client";
import type { ChannelAccountApiItem } from "@/lib/api-client/live-client";
import type {
  EnrichedProspectItem,
  EnrichProspectEmailsResponse,
  LeadCollectionJobCreate,
  LeadCollectionJobRead,
  ProspectRead,
  SendRealCampaignRequest,
  SendRealCampaignResponse,
} from "@/lib/api-client/types.gen";
import {
  createJobApiV1GrowthJobsPost,
  enrichProspectEmailsApiV1GrowthProspectsEnrichEmailsPost,
  listJobsApiV1GrowthJobsGet,
  listProspectsApiV1GrowthProspectsGet,
  runJobApiV1GrowthJobsJobIdRunPost,
  sendCampaignRealApiV1GrowthCampaignsCampaignIdSendRealPost,
} from "@/lib/api-client/sdk.gen";
import { fetchChannelAccounts, shouldUseMockApi } from "@/lib/data-source";
import { mockGrowthPreview, mockGrowthProspects } from "@/lib/mock-fixtures/phase3c";

// ---------------------------------------------------------------------------
// Provider modes and data provenance
// ---------------------------------------------------------------------------

/** `LeadCollectionJobCreate.provider_mode` — the requested collection recipe. */
export type CollectionProviderMode = "auto" | "overpass" | "google_places" | "synthetic";

/** Provider keys the backend reports per record (`job.sources`, `raw_metadata.provider`). */
export type SourceProviderKey =
  | "google_places"
  | "overpass_osm"
  | "overture_poi"
  | "csv_import"
  | "fallback_catalog"
  | (string & {});

const SOURCE_LABEL: Record<string, { zh: string; en: string }> = {
  google_places: { zh: "Google Places", en: "Google Places" },
  overpass_osm: { zh: "OSM Overpass", en: "OSM Overpass" },
  overture_poi: { zh: "Overture POI", en: "Overture POI" },
  csv_import: { zh: "CSV 导入", en: "CSV import" },
  fallback_catalog: { zh: "示例目录", en: "Sample catalogue" },
  synthetic: { zh: "示例目录", en: "Sample catalogue" },
};

/** Human label for a provenance key; unknown keys are surfaced verbatim, never hidden. */
export function sourceLabel(key: string, locale: "zh" | "en" = "zh"): string {
  return SOURCE_LABEL[key]?.[locale] ?? key;
}

/** `SendRealCampaignResponse.provider` → the engine label operators recognise. */
export function emailEngineLabel(provider: string): string {
  const normalized = provider.trim().toLowerCase();
  if (!normalized) return "Resend / SMTP";
  if (normalized.startsWith("smtp")) return "SMTP";
  if (normalized === "resend") return "Resend";
  return provider;
}

/**
 * Pills for one job: the *actual* providers behind its records when the backend
 * reports them, otherwise the requested source that produced the job.
 */
export function jobSourcePills(job: {
  source_provider: string;
  sources?: Record<string, number> | null;
}): { key: string; count: number | null }[] {
  const counts = job.sources ?? {};
  const keys = Object.keys(counts).filter((key) => key && key !== "unknown");
  if (keys.length) {
    return keys
      .sort((left, right) => (counts[right] ?? 0) - (counts[left] ?? 0) || left.localeCompare(right))
      .map((key) => ({ key, count: counts[key] ?? 0 }));
  }
  return [{ key: job.source_provider, count: null }];
}

// ---------------------------------------------------------------------------
// Multi-value parsing (cities / keywords)
// ---------------------------------------------------------------------------

// Mirrors the server-side contract: `CityList` max 10, `keywords` max 20.
export const MAX_BATCH_CITIES = 10;
export const MAX_BATCH_KEYWORDS = 20;

/** Split comma/newline/semicolon separated input, trim, de-duplicate, cap. */
export function parseMultiValue(raw: string, limit: number): string[] {
  const seen = new Set<string>();
  const values: string[] = [];
  for (const chunk of raw.split(/[\n,;，；、]+/)) {
    const value = chunk.trim().replace(/\s+/g, " ");
    if (!value) continue;
    const key = value.toLocaleLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    values.push(value);
    if (values.length >= limit) break;
  }
  return values;
}

export function parseCities(raw: string): string[] {
  return parseMultiValue(raw, MAX_BATCH_CITIES);
}

export function parseKeywords(raw: string): string[] {
  return parseMultiValue(raw, MAX_BATCH_KEYWORDS);
}

// ---------------------------------------------------------------------------
// Batch collection: create the recipe, then run it
// ---------------------------------------------------------------------------

export interface BatchCollectionRequest {
  cities: readonly string[];
  keywords: readonly string[];
  provider_mode?: CollectionProviderMode;
  max_results?: number;
  min_rating?: string | null;
  require_phone?: boolean;
}

/** The subset of `LeadCollectionJobRead` the collection UI renders. */
export interface BatchCollectionJob {
  id: string;
  source_provider: LeadCollectionJobRead["source_provider"];
  status: LeadCollectionJobRead["status"];
  keywords: string[];
  target_region: string | null;
  max_results: number;
  collected_count: number;
  cleaned_count: number;
  synced_count: number;
  sources: Record<string, number>;
  error_message: string | null;
}

export function mapBatchJob(row: LeadCollectionJobRead): BatchCollectionJob {
  return {
    id: row.id,
    source_provider: row.source_provider,
    status: row.status,
    keywords: row.keywords ?? [],
    target_region: row.target_region ?? null,
    max_results: row.max_results,
    collected_count: row.collected_count,
    cleaned_count: row.cleaned_count,
    synced_count: row.synced_count,
    sources: row.sources ?? {},
    error_message: row.error_message,
  };
}

/**
 * Build the request-scoped batch recipe. Only `cities`/`keywords`/`provider_mode`
 * are sent (plus the optional filters) so the server never has to reconcile a
 * pinned `source_provider` with a provider mode.
 */
export function buildBatchJobPayload(request: BatchCollectionRequest): LeadCollectionJobCreate {
  return {
    cities: parseCities(request.cities.join(",")),
    keywords: parseKeywords(request.keywords.join(",")),
    provider_mode: request.provider_mode ?? "auto",
    max_results: request.max_results ?? 100,
    min_rating: request.min_rating ?? null,
    require_phone: request.require_phone ?? false,
  };
}

const mockBatchJobs: BatchCollectionJob[] = [];

function mockBatchJob(payload: LeadCollectionJobCreate): BatchCollectionJob {
  const cities = payload.cities ?? [];
  const mode = payload.provider_mode ?? "auto";
  const sourceProvider: LeadCollectionJobRead["source_provider"] =
    mode === "google_places" ? "google_places" : "overpass_osm";
  const provenance = mode === "synthetic" ? "fallback_catalog" : sourceProvider;
  const collected = Math.min(
    payload.max_results ?? 100,
    Math.max(1, cities.length) * Math.max(1, (payload.keywords ?? []).length) * 2
  );
  const job: BatchCollectionJob = {
    id: `job-batch-mock-${mockBatchJobs.length + 1}`,
    source_provider: sourceProvider,
    status: "completed",
    keywords: [...(payload.keywords ?? [])],
    target_region: cities.length ? cities.join(" / ") : null,
    max_results: payload.max_results ?? 100,
    collected_count: collected,
    cleaned_count: 0,
    synced_count: 0,
    sources: { [provenance]: collected },
    error_message: null,
  };
  mockBatchJobs.push(job);
  return job;
}

/**
 * Create a batch collection job and run it in the same operator gesture.
 *
 * The backend executes a request-scoped recipe at creation time, so `run` is only
 * called for a job that is still `pending` (this keeps the action idempotent and
 * avoids a needless conflict).
 */
export async function createAndRunBatchCollection(
  request: BatchCollectionRequest
): Promise<BatchCollectionJob> {
  const payload = buildBatchJobPayload(request);
  if (shouldUseMockApi()) {
    const job = mockBatchJob(payload);
    return { ...job, sources: { ...job.sources } };
  }
  const created = mapBatchJob(
    unwrap(
      await createJobApiV1GrowthJobsPost({ client: getBffClient(), body: payload })
    ) as LeadCollectionJobRead
  );
  if (created.status !== "pending") return created;
  const completed = unwrap(
    await runJobApiV1GrowthJobsJobIdRunPost({
      client: getBffClient(),
      path: { job_id: created.id },
    })
  ) as LeadCollectionJobRead;
  return mapBatchJob(completed);
}

// ---------------------------------------------------------------------------
// Website e-mail enrichment
// ---------------------------------------------------------------------------

/** Derive the public sales mailbox a fixture website would publish (mock only). */
function demoEmailForWebsite(website: string): string | null {
  try {
    const host = new URL(website.startsWith("http") ? website : `https://${website}`).hostname;
    const label = host.replace(/^www\./, "");
    if (!label.includes(".")) return null;
    return `sales@${label}`;
  } catch {
    return null;
  }
}

function mockEnrichment(prospectIds: readonly string[]): EnrichProspectEmailsResponse {
  const items: EnrichedProspectItem[] = [];
  const seen = new Set<string>();
  for (const id of prospectIds) {
    if (seen.has(id)) continue;
    seen.add(id);
    const row = mockGrowthProspects.find((candidate) => candidate.id === id);
    if (!row) {
      items.push({
        prospect_id: id,
        company_name: "",
        website: null,
        email: null,
        previous_email: null,
        grade: "C",
        status: "not_found",
      });
      continue;
    }
    if (row.email) {
      items.push({
        prospect_id: id,
        company_name: row.company_name,
        website: row.website,
        email: row.email,
        previous_email: row.email,
        grade: row.grade,
        status: "already_has_email",
      });
      continue;
    }
    if (!row.website) {
      items.push({
        prospect_id: id,
        company_name: row.company_name,
        website: null,
        email: null,
        previous_email: null,
        grade: row.grade,
        status: "skipped_no_website",
      });
      continue;
    }
    const email = demoEmailForWebsite(row.website);
    items.push({
      prospect_id: id,
      company_name: row.company_name,
      website: row.website,
      email,
      previous_email: null,
      grade: email ? "A" : row.grade,
      status: email ? "enriched" : "no_email_found",
    });
  }
  const scanned = items.filter(
    (item) =>
      item.status === "enriched" ||
      item.status === "no_email_found" ||
      item.status === "blocked" ||
      item.status === "invalid_website"
  ).length;
  return {
    checked_count: scanned,
    enriched_count: items.filter((item) => item.status === "enriched").length,
    items,
  };
}

/**
 * Scan the websites of the given prospects and fill the e-mails they publish.
 *
 * The server only reads public home/contact pages, refuses private targets and
 * never invents an address; a prospect that already has one is skipped.
 */
export async function enrichProspectEmails(
  prospectIds: readonly string[],
  options: { force?: boolean } = {}
): Promise<EnrichProspectEmailsResponse> {
  const ids = [...new Set(prospectIds)];
  if (shouldUseMockApi()) return mockEnrichment(ids);
  return unwrap(
    await enrichProspectEmailsApiV1GrowthProspectsEnrichEmailsPost({
      client: getBffClient(),
      body: { prospect_ids: ids, force: options.force ?? false },
    })
  ) as EnrichProspectEmailsResponse;
}

// ---------------------------------------------------------------------------
// Provenance snapshot (per-record sources for jobs and prospects)
// ---------------------------------------------------------------------------

export interface ProvenanceSnapshot {
  /** job id → provider key → record count */
  jobs: Record<string, Record<string, number>>;
  /** prospect id → the provider that actually produced the record */
  prospects: Record<string, string>;
}

function providerOf(rawMetadata: ProspectRead["raw_metadata"] | undefined): string | null {
  const provider = rawMetadata?.provider;
  return typeof provider === "string" && provider.trim() ? provider.trim() : null;
}

/**
 * The backend records the provider that *actually* produced each row in
 * `job.sources` and `raw_metadata.provider`. Operators get that provenance
 * verbatim so live map data is never confused with the fallback catalogue.
 */
export async function fetchProvenance(): Promise<ProvenanceSnapshot> {
  if (shouldUseMockApi()) {
    const jobs: Record<string, Record<string, number>> = {};
    for (const row of mockGrowthProspects) {
      const provider = providerOf(row.raw_metadata) ?? "overpass_osm";
      const bucket = (jobs[row.job_id ?? "job-global-map"] ??= {});
      bucket[provider] = (bucket[provider] ?? 0) + 1;
    }
    const prospects: Record<string, string> = {};
    for (const row of mockGrowthProspects) {
      prospects[row.id] = providerOf(row.raw_metadata) ?? "overpass_osm";
    }
    return { jobs, prospects };
  }
  const [jobPage, prospectPage] = await Promise.all([
    listJobsApiV1GrowthJobsGet({ client: getBffClient(), query: { limit: 20 } }),
    listProspectsApiV1GrowthProspectsGet({ client: getBffClient(), query: { limit: 100 } }),
  ]);
  const jobs: Record<string, Record<string, number>> = {};
  for (const job of (unwrap(jobPage) as { items: LeadCollectionJobRead[] }).items) {
    jobs[job.id] = job.sources ?? {};
  }
  const prospects: Record<string, string> = {};
  for (const row of (unwrap(prospectPage) as { items: ProspectRead[] }).items) {
    const provider = providerOf(row.raw_metadata);
    if (provider) prospects[row.id] = provider;
  }
  return { jobs, prospects };
}

// ---------------------------------------------------------------------------
// Email sender mailboxes (real outreach)
// ---------------------------------------------------------------------------

export interface EmailSenderAccount {
  id: string;
  display_name: string;
  mailbox: string;
  label: string;
}

/** Demo mailboxes so the mock workbench still shows the real-send flow. */
export const DEMO_EMAIL_SENDER_ACCOUNTS: readonly EmailSenderAccount[] = [
  {
    id: "mock-email-account-301go",
    display_name: "301Go Export Sales",
    mailbox: "sales@301go.net",
    label: "301Go Export Sales (sales@301go.net)",
  },
  {
    id: "mock-email-account-gmail",
    display_name: "Personal Gmail SMTP",
    mailbox: "sales.li@gmail.com",
    label: "Personal Gmail SMTP (sales.li@gmail.com)",
  },
];

function toSenderAccount(item: ChannelAccountApiItem): EmailSenderAccount {
  const mailbox = (item.external_account_id ?? "").trim();
  const displayName = (item.display_name ?? "").trim();
  const name = displayName || mailbox || item.id;
  return {
    id: item.id,
    display_name: name,
    mailbox,
    label: mailbox ? `${name} (${mailbox})` : name,
  };
}

/**
 * Active email channel accounts of the workspace. An empty list on the live path
 * means "no mailbox connected yet" — the UI must say so instead of inventing one.
 */
export async function fetchEmailSenderAccounts(): Promise<EmailSenderAccount[]> {
  const page = await fetchChannelAccounts();
  const active = (page.items ?? [])
    .filter((item) => item.channel_type === "email" && item.status === "active")
    .map(toSenderAccount);
  if (active.length) return active;
  return shouldUseMockApi() ? DEMO_EMAIL_SENDER_ACCOUNTS.map((row) => ({ ...row })) : [];
}

// ---------------------------------------------------------------------------
// Real email outreach (Resend / SMTP)
// ---------------------------------------------------------------------------

async function mockSendReal(
  campaignId: string,
  payload: SendRealCampaignRequest
): Promise<SendRealCampaignResponse> {
  const accounts = await fetchEmailSenderAccounts();
  const chosen =
    accounts.find((account) => account.id === payload.channel_account_id) ?? accounts[0];
  const mailbox = chosen?.mailbox ?? "sales@301go.net";
  const fromEmail = chosen?.display_name ? `${chosen.display_name} <${mailbox}>` : mailbox;
  return {
    campaign_id: campaignId,
    provider: /gmail|smtp/i.test(`${chosen?.label ?? ""} ${mailbox}`) ? "smtp_imap" : "resend",
    from_email: fromEmail,
    sent_count: mockGrowthPreview.eligible_count,
    suppressed_count: mockGrowthPreview.suppressed_count,
    failed_count: 0,
    dry_run: payload.dry_run ?? false,
  };
}

/**
 * Deliver a queued email campaign through the tenant's own mailbox.
 *
 * A `dry_run` persists nothing and only reports what *would* be sent; the real
 * run still applies the zero-delay STOP suppression list per recipient.
 */
export async function sendCampaignReal(
  campaignId: string,
  payload: SendRealCampaignRequest = {}
): Promise<SendRealCampaignResponse> {
  const body: SendRealCampaignRequest = {
    channel_account_id: payload.channel_account_id ?? null,
    dry_run: payload.dry_run ?? false,
    ...(payload.max_send === undefined ? {} : { max_send: payload.max_send }),
  };
  if (shouldUseMockApi()) return mockSendReal(campaignId, body);
  return unwrap(
    await sendCampaignRealApiV1GrowthCampaignsCampaignIdSendRealPost({
      client: getBffClient(),
      path: { campaign_id: campaignId },
      body,
    })
  ) as SendRealCampaignResponse;
}

export type { EnrichProspectEmailsResponse, SendRealCampaignResponse };
