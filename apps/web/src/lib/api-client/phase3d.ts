// Phase 3D live adapters over the generated @hey-api/openapi-ts SDK.
// Every request goes through the shared BFF-configured client (@/lib/api), so
// session cookies and the 401 refresh flow behave exactly like bffFetch.
// Zero SQL/ORM: the browser only ever sees the typed /api/v1/social contract, and
// the OAuth token is write-only (AccountRead exposes only `has_stored_token`).
import { getBffClient } from "@/lib/api";
import { unwrap } from "@/lib/api-client/live-client";
import type {
  AccountConnect,
  AccountRead,
  CaptionGenerate,
  CaptionRead,
  ConvertLeadResult,
  MentionCreate,
  MentionRead,
  PlatformRead,
  PostCreate,
  PostPublish,
  PostRead,
  PostUpdate,
  SocialSummary,
} from "@/lib/api-client/types.gen";
import {
  connectAccountApiV1SocialAccountsConnectPost,
  convertMentionApiV1SocialMentionsMentionIdConvertLeadPost,
  createMentionApiV1SocialMentionsPost,
  createPostApiV1SocialPostsPost,
  disconnectAccountApiV1SocialAccountsAccountIdDisconnectPost,
  generateCaptionApiV1SocialCaptionGeneratePost,
  listAccountsApiV1SocialAccountsGet,
  listMentionsApiV1SocialMentionsGet,
  listPlatformsApiV1SocialPlatformsGet,
  listPostsApiV1SocialPostsGet,
  publishPostApiV1SocialPostsPostIdPublishPost,
  socialSummaryApiV1SocialSummaryGet,
  updatePostApiV1SocialPostsPostIdPatch,
} from "@/lib/api-client/sdk.gen";
import type {
  PlatformPlaybook,
  SocialAccount,
  SocialMention,
  SocialPlatform,
  SocialPost,
} from "@/components/social/types";

const client = () => getBffClient();

/** The panel KPI shape, mapped 1:1 from the server aggregate (never recomputed here). */
export interface SocialKpis {
  total_posts: number;
  published_posts: number;
  scheduled_posts: number;
  draft_posts: number;
  social_leads: number;
  converted_leads: number;
  mentions_total: number;
  connected_accounts: number;
  supported_platforms: number;
}

export function mapSummary(row: SocialSummary): SocialKpis {
  return {
    total_posts: row.total_posts,
    published_posts: row.published_posts,
    scheduled_posts: row.scheduled_posts,
    draft_posts: row.draft_posts,
    social_leads: row.social_leads,
    converted_leads: row.converted_leads,
    mentions_total: row.mentions_total,
    connected_accounts: row.connected_accounts,
    supported_platforms: row.supported_platforms,
  };
}

/** Contract account -> matrix card shape. The token envelope is never exposed. */
export function mapAccount(row: AccountRead): SocialAccount {
  return {
    id: row.id,
    platform: row.platform as SocialPlatform,
    region: row.region,
    handle: row.handle,
    display_name: row.display_name,
    status: row.status,
    followers_count: row.followers_count,
  };
}

export function mapPlatform(row: PlatformRead): PlatformPlaybook {
  return {
    platform: row.platform as SocialPlatform,
    region: row.region,
    label: row.label,
    emoji: row.emoji,
    audience: row.audience,
    acquisition_plays: row.acquisition_plays,
    best_practices: row.best_practices,
    content_formats: row.content_formats,
    publish_mode: row.publish_mode,
    compliance_note: row.compliance_note,
  };
}

export function mapPost(row: PostRead): SocialPost {
  return {
    id: row.id,
    platform: row.platform as SocialPlatform,
    title: row.title ?? "",
    caption: row.caption,
    hashtags: row.hashtags,
    sku_ref: row.sku_ref,
    status: row.status,
    scheduled_at: row.scheduled_at,
    published_at: row.published_at,
  };
}

export function mapMention(row: MentionRead): SocialMention {
  return {
    id: row.id,
    platform: row.platform as SocialPlatform,
    author_handle: row.author_handle,
    content_text: row.content_text,
    sentiment: row.sentiment,
    occurred_at: row.occurred_at,
    converted_lead_id: row.converted_lead_id,
  };
}

export function fetchSocialSummary(): Promise<SocialKpis> {
  return socialSummaryApiV1SocialSummaryGet({ client: client() }).then((response) =>
    mapSummary(unwrap(response))
  );
}

export function fetchSocialPlatforms(): Promise<PlatformPlaybook[]> {
  return listPlatformsApiV1SocialPlatformsGet({ client: client() }).then((response) =>
    unwrap(response).map(mapPlatform)
  );
}

export function fetchSocialAccounts(): Promise<SocialAccount[]> {
  return listAccountsApiV1SocialAccountsGet({ client: client(), query: { limit: 50 } }).then(
    (response) => unwrap(response).items.map(mapAccount)
  );
}

export interface SocialAccountInput {
  platform: SocialPlatform;
  handle: string;
  display_name: string;
  followers_count?: number;
  external_account_id?: string | null;
  /** Write-only: encrypted server-side, never echoed back. */
  access_token?: string;
}

export function connectSocialAccount(input: SocialAccountInput): Promise<SocialAccount> {
  const body: AccountConnect = {
    platform: input.platform,
    handle: input.handle,
    display_name: input.display_name,
    followers_count: input.followers_count ?? 0,
    external_account_id: input.external_account_id ?? null,
    ...(input.access_token ? { access_token: input.access_token } : {}),
  };
  return connectAccountApiV1SocialAccountsConnectPost({ client: client(), body }).then(
    (response) => mapAccount(unwrap(response))
  );
}

export function disconnectSocialAccount(accountId: string): Promise<SocialAccount> {
  return disconnectAccountApiV1SocialAccountsAccountIdDisconnectPost({
    client: client(),
    path: { account_id: accountId },
  }).then((response) => mapAccount(unwrap(response)));
}

export function fetchSocialPosts(): Promise<SocialPost[]> {
  return listPostsApiV1SocialPostsGet({ client: client(), query: { limit: 100 } }).then(
    (response) => unwrap(response).items.map(mapPost)
  );
}

export interface SocialPostInput {
  platform: SocialPlatform;
  title?: string | null;
  caption: string;
  hashtags?: readonly string[];
  media_urls?: readonly string[];
  sku_ref?: string | null;
  status?: "draft" | "scheduled";
  scheduled_at?: string | null;
  social_account_id?: string | null;
}

export function createPost(input: SocialPostInput): Promise<SocialPost> {
  const body: PostCreate = {
    platform: input.platform,
    title: input.title ?? null,
    caption: input.caption,
    hashtags: input.hashtags ? [...input.hashtags] : [],
    media_urls: input.media_urls ? [...input.media_urls] : [],
    sku_ref: input.sku_ref ?? null,
    status: input.status ?? "draft",
    scheduled_at: input.scheduled_at ?? null,
    social_account_id: input.social_account_id ?? null,
  };
  return createPostApiV1SocialPostsPost({ client: client(), body }).then((response) =>
    mapPost(unwrap(response))
  );
}

export function updatePost(postId: string, patch: PostUpdate): Promise<SocialPost> {
  return updatePostApiV1SocialPostsPostIdPatch({
    client: client(),
    path: { post_id: postId },
    body: patch,
  }).then((response) => mapPost(unwrap(response)));
}

export function publishPost(postId: string, options: { manual_confirmation?: boolean } = {}) {
  const body: PostPublish = { ...(options.manual_confirmation ? { manual_confirmation: true } : {}) };
  return publishPostApiV1SocialPostsPostIdPublishPost({
    client: client(),
    path: { post_id: postId },
    body,
  }).then((response) => mapPost(unwrap(response)));
}

export interface CaptionInput {
  platform: SocialPlatform;
  prompt: string;
  sku_ref?: string | null;
  language?: "zh" | "en";
  tone?: string | null;
}

export function generateSocialCaption(input: CaptionInput): Promise<CaptionRead> {
  const body: CaptionGenerate = {
    platform: input.platform,
    prompt: input.prompt,
    sku_ref: input.sku_ref ?? null,
    language: input.language ?? "zh",
    tone: input.tone ?? null,
  };
  return generateCaptionApiV1SocialCaptionGeneratePost({ client: client(), body }).then(
    (response) => unwrap(response)
  );
}

export function fetchSocialMentions(): Promise<SocialMention[]> {
  return listMentionsApiV1SocialMentionsGet({ client: client(), query: { limit: 100 } }).then(
    (response) => unwrap(response).items.map(mapMention)
  );
}

export interface SocialMentionInput {
  platform: SocialPlatform;
  author_handle: string;
  content_text: string;
  sentiment?: "positive" | "neutral" | "negative" | "lead_intent";
  occurred_at?: string;
}

export function createMention(input: SocialMentionInput): Promise<SocialMention> {
  const body: MentionCreate = {
    platform: input.platform,
    author_handle: input.author_handle,
    content_text: input.content_text,
    sentiment: input.sentiment ?? "neutral",
    occurred_at: input.occurred_at ?? null,
  };
  return createMentionApiV1SocialMentionsPost({ client: client(), body }).then((response) =>
    mapMention(unwrap(response))
  );
}

export function convertMentionToLead(mentionId: string): Promise<ConvertLeadResult> {
  return convertMentionApiV1SocialMentionsMentionIdConvertLeadPost({
    client: client(),
    path: { mention_id: mentionId },
  }).then((response) => unwrap(response) as ConvertLeadResult);
}

export type { CaptionRead, ConvertLeadResult };
