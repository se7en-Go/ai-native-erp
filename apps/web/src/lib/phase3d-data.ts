// Phase 3D data layer: generated-SDK calls by default, fixture-backed responses
// only when NEXT_PUBLIC_USE_MOCK_API=true (same switch as data-source.ts).
import * as live from "@/lib/api-client/phase3d";
import type { SocialKpis } from "@/lib/api-client/phase3d";
import type {
  SocialAccount,
  SocialPlatform,
  SocialPost,
  SocialPostDraft,
  SocialMention,
} from "@/components/social/types";
import { getPlaybook } from "@/components/social/platform-playbooks";
import {
  mockSocialAccounts,
  mockSocialMentions,
  mockSocialPosts,
  mockSocialSummary,
} from "@/lib/mock-fixtures/phase3d";
import { shouldUseMockApi } from "@/lib/data-source";

/** Stateful only in mock mode: mutations land here so the page re-reads what it wrote. */
const mockState = {
  summary: { ...mockSocialSummary } as SocialKpis,
  accounts: mockSocialAccounts.map((row) => ({ ...row })),
  posts: mockSocialPosts.map((row) => ({ ...row })),
  mentions: mockSocialMentions.map((row) => ({ ...row })),
};

function recomputeMockSummary(): void {
  const connected = mockState.accounts.filter((row) => row.status === "connected").length;
  mockState.summary = {
    total_posts: mockState.posts.length,
    published_posts: mockState.posts.filter((row) => row.status === "published").length,
    scheduled_posts: mockState.posts.filter((row) => row.status === "scheduled").length,
    draft_posts: mockState.posts.filter((row) => row.status === "draft").length,
    social_leads: mockState.mentions.filter((row) => row.sentiment === "lead_intent").length,
    converted_leads: mockState.mentions.filter((row) => row.converted_lead_id !== null).length,
    mentions_total: mockState.mentions.length,
    connected_accounts: connected,
    supported_platforms: 14,
  };
}

function toUiAccount(row: (typeof mockState.accounts)[number]): SocialAccount {
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

function toUiPost(row: (typeof mockState.posts)[number]): SocialPost {
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

function toUiMention(row: (typeof mockState.mentions)[number]): SocialMention {
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

export function fetchSocialKpis(): Promise<SocialKpis> {
  if (shouldUseMockApi()) {
    recomputeMockSummary();
    return Promise.resolve({ ...mockState.summary });
  }
  return live.fetchSocialSummary();
}

export function fetchSocialAccounts(): Promise<SocialAccount[]> {
  if (shouldUseMockApi()) return Promise.resolve(mockState.accounts.map(toUiAccount));
  return live.fetchSocialAccounts();
}

export interface SocialAccountAction {
  platform: SocialPlatform;
  handle: string;
  display_name: string;
  followers_count: number;
}

export function connectSocialAccount(input: SocialAccountAction): Promise<SocialAccount> {
  if (shouldUseMockApi()) {
    const existing = mockState.accounts.find(
      (row) => row.platform === input.platform && row.handle === input.handle
    );
    const row = existing ?? {
      id: `acc-mock-${input.platform}`,
      platform: input.platform,
      region: getPlaybook(input.platform).region,
      handle: input.handle,
      display_name: input.display_name,
      external_account_id: null,
      status: "connected" as const,
      followers_count: input.followers_count,
      config: { demo: true },
      has_stored_token: false,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    };
    row.display_name = input.display_name;
    row.followers_count = input.followers_count;
    row.status = "connected";
    if (existing === undefined) mockState.accounts = [...mockState.accounts, row];
    recomputeMockSummary();
    return Promise.resolve(toUiAccount(row));
  }
  return live.connectSocialAccount(input);
}

export function disconnectSocialAccount(accountId: string): Promise<SocialAccount> {
  if (shouldUseMockApi()) {
    const row = mockState.accounts.find((account) => account.id === accountId);
    if (!row) return Promise.reject(new Error("social account not found"));
    row.status = "disconnected";
    row.has_stored_token = false;
    recomputeMockSummary();
    return Promise.resolve(toUiAccount(row));
  }
  return live.disconnectSocialAccount(accountId);
}

export function fetchSocialPosts(): Promise<SocialPost[]> {
  if (shouldUseMockApi()) return Promise.resolve(mockState.posts.map(toUiPost));
  return live.fetchSocialPosts();
}

/** One API row per platform, exactly like the contract (`social_posts` is single-platform). */
export async function saveSocialPosts(
  drafts: readonly SocialPostDraft[],
  status: "draft" | "scheduled"
): Promise<SocialPost[]> {
  if (shouldUseMockApi()) {
    const created = drafts.map((draft, index) => ({
      id: `post-mock-new-${mockState.posts.length + index + 1}`,
      social_account_id: null,
      platform: draft.platform,
      title: draft.title || null,
      caption: draft.caption,
      hashtags: [...draft.hashtags],
      media_urls: [],
      sku_ref: draft.sku_ref,
      status,
      scheduled_at: draft.scheduled_at,
      published_at: null,
      external_post_id: null,
      metrics: {},
      created_by: "11111111-1111-1111-1111-111111111111",
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    }));
    mockState.posts = [...mockState.posts, ...created];
    recomputeMockSummary();
    return created.map(toUiPost);
  }
  const created: SocialPost[] = [];
  for (const draft of drafts) {
    created.push(
      await live.createPost({
        platform: draft.platform,
        title: draft.title || null,
        caption: draft.caption,
        hashtags: draft.hashtags,
        sku_ref: draft.sku_ref,
        status,
        scheduled_at: draft.scheduled_at,
      })
    );
  }
  return created;
}

export function publishSocialPost(
  postId: string,
  manualConfirmation: boolean
): Promise<SocialPost> {
  if (shouldUseMockApi()) {
    const row = mockState.posts.find((post) => post.id === postId);
    if (!row) return Promise.reject(new Error("social post not found"));
    row.status = "published";
    row.published_at = new Date().toISOString();
    recomputeMockSummary();
    return Promise.resolve(toUiPost(row));
  }
  return live.publishPost(postId, { manual_confirmation: manualConfirmation });
}

export interface CaptionRequestInput {
  platform: SocialPlatform;
  sku_ref: string | null;
  prompt: string;
}

export async function generateSocialCaption(
  request: CaptionRequestInput
): Promise<string> {
  if (shouldUseMockApi()) {
    // Mock mode never pretends a real model ran: the text is labelled as a fixture.
    return Promise.resolve(
      `【Mock AI 文案 · ${getPlaybook(request.platform).label}】${request.prompt}`
    );
  }
  const result = await live.generateSocialCaption({
    platform: request.platform,
    prompt: request.prompt,
    sku_ref: request.sku_ref,
    language: "zh",
  });
  return result.text;
}

export function fetchSocialMentions(): Promise<SocialMention[]> {
  if (shouldUseMockApi()) return Promise.resolve(mockState.mentions.map(toUiMention));
  return live.fetchSocialMentions();
}

export async function convertSocialMention(mentionId: string): Promise<void> {
  if (shouldUseMockApi()) {
    const row = mockState.mentions.find((mention) => mention.id === mentionId);
    if (!row) throw new Error("social mention not found");
    row.converted_lead_id = row.converted_lead_id ?? `lead-mock-${mentionId}`;
    recomputeMockSummary();
    return;
  }
  await live.convertMentionToLead(mentionId);
}

export type { SocialKpis };
