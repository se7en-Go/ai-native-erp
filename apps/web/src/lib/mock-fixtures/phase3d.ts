/**
 * Phase 3D mock fixtures. Strictly UI fixtures for `NEXT_PUBLIC_USE_MOCK_API=true`;
 * the live path consumes the generated `@hey-api/openapi-ts` SDK instead.
 *
 * Values mirror the `--with-demo-data` seed (three authorized accounts, three
 * calendar posts across published/scheduled/draft, three listening mentions) so the
 * mocked screens match the demo stack. Platform metadata is derived from the shared
 * playbook constants instead of being duplicated here.
 */
import type {
  AccountRead,
  MentionRead,
  PlatformRead,
  PostRead,
  SocialSummary,
} from "@/lib/api-client/types.gen";
import { PLATFORM_PLAYBOOKS } from "@/components/social/platform-playbooks";

const TENANT = "22222222-2222-2222-2222-222222222222";
const OWNER = "11111111-1111-1111-1111-111111111111";

export const mockSocialSummary: SocialSummary = {
  total_posts: 4,
  published_posts: 1,
  scheduled_posts: 1,
  draft_posts: 2,
  social_leads: 1,
  converted_leads: 0,
  mentions_total: 3,
  connected_accounts: 2,
  supported_platforms: 14,
};

export const mockSocialPlatforms: PlatformRead[] = PLATFORM_PLAYBOOKS.map((playbook) => ({
  platform: playbook.platform,
  region: playbook.region,
  label: playbook.label,
  emoji: playbook.emoji,
  audience: playbook.audience,
  acquisition_plays: [...playbook.acquisition_plays],
  best_practices: [...playbook.best_practices],
  content_formats: [...playbook.content_formats],
  publish_mode: playbook.publish_mode,
  supports_scheduled_publish: playbook.publish_mode === "api",
  compliance_note: playbook.compliance_note,
}));

export const mockSocialAccounts: AccountRead[] = [
  {
    id: "acc-mock-instagram",
    platform: "instagram",
    region: "overseas",
    handle: "@lumenlight",
    display_name: "Lumen Light",
    external_account_id: null,
    status: "connected",
    followers_count: 12345,
    config: { demo: true },
    has_stored_token: true,
    created_at: "2026-11-01T09:00:00Z",
    updated_at: "2026-11-01T09:00:00Z",
  },
  {
    id: "acc-mock-xiaohongshu",
    platform: "xiaohongshu",
    region: "domestic",
    handle: "流明照明",
    display_name: "流明照明",
    external_account_id: null,
    status: "connected",
    followers_count: 800,
    config: { demo: true },
    has_stored_token: false,
    created_at: "2026-11-01T09:00:00Z",
    updated_at: "2026-11-01T09:00:00Z",
  },
  {
    id: "acc-mock-wechat-oa",
    platform: "wechat_oa",
    region: "domestic",
    handle: "lumen-oa",
    display_name: "流明公众号",
    external_account_id: null,
    status: "token_expired",
    followers_count: 320,
    config: { demo: true },
    has_stored_token: true,
    created_at: "2026-11-01T09:00:00Z",
    updated_at: "2026-11-01T09:00:00Z",
  },
];

/** Calendar fixtures are relative to "now" so the month view always has content. */
const DAY_MS = 86_400_000;
const now = Date.now();
const iso = (offsetDays: number): string =>
  new Date(now + offsetDays * DAY_MS).toISOString();

export const mockSocialPosts: PostRead[] = [
  {
    id: "post-mock-instagram-published",
    social_account_id: "acc-mock-instagram",
    platform: "instagram",
    title: "IP65 workshop light, factory direct",
    caption: "Our IP65 workshop light survives dust and water on the production floor.",
    hashtags: ["#worklight", "#ledlighting"],
    media_urls: [],
    sku_ref: "LED-COB-24V-5M",
    status: "published",
    scheduled_at: iso(-3),
    published_at: iso(-3),
    external_post_id: null,
    metrics: { publish_channel: "api" },
    created_by: OWNER,
    created_at: iso(-4),
    updated_at: iso(-3),
  },
  {
    id: "post-mock-tiktok-scheduled",
    social_account_id: null,
    platform: "tiktok",
    title: "Factory tour: COB strip line",
    caption: "60 seconds inside the COB strip line, from reel to IP65 finished work light.",
    hashtags: ["#factorytour"],
    media_urls: [],
    sku_ref: "LED-COB-24V-5M",
    status: "scheduled",
    scheduled_at: iso(2),
    published_at: null,
    external_post_id: null,
    metrics: {},
    created_by: OWNER,
    created_at: iso(-1),
    updated_at: iso(-1),
  },
  {
    id: "post-mock-xiaohongshu-draft",
    social_account_id: "acc-mock-xiaohongshu",
    platform: "xiaohongshu",
    title: "工地照明怎么选",
    caption: "工地照明避坑三点：色温、防护等级、驱动寿命。",
    hashtags: ["#工地照明"],
    media_urls: [],
    sku_ref: null,
    status: "draft",
    scheduled_at: null,
    published_at: null,
    external_post_id: null,
    metrics: {},
    created_by: OWNER,
    created_at: iso(-1),
    updated_at: iso(-1),
  },
  {
    id: "post-mock-youtube-failed",
    social_account_id: null,
    platform: "youtube",
    title: "Install guide (upload quota hit)",
    caption: "Installation walkthrough for the outdoor flood light series.",
    hashtags: [],
    media_urls: [],
    sku_ref: null,
    status: "failed",
    scheduled_at: null,
    published_at: null,
    external_post_id: null,
    metrics: {},
    created_by: OWNER,
    created_at: iso(-2),
    updated_at: iso(-2),
  },
];

export const mockSocialMentions: MentionRead[] = [
  {
    id: "mention-mock-lead",
    social_account_id: null,
    platform: "tiktok",
    author_handle: "@dubai_build_supply",
    content_text: "Need 500 pcs of your IP65 work light for a Dubai site. What is the FOB price?",
    sentiment: "lead_intent",
    converted_lead_id: null,
    occurred_at: iso(-1),
    created_at: iso(-1),
  },
  {
    id: "mention-mock-positive",
    social_account_id: "acc-mock-instagram",
    platform: "instagram",
    author_handle: "@lighting_home_lab",
    content_text: "Installed the COB strip in our showroom, the light is perfectly even.",
    sentiment: "positive",
    converted_lead_id: null,
    occurred_at: iso(-2),
    created_at: iso(-2),
  },
  {
    id: "mention-mock-negative",
    social_account_id: null,
    platform: "youtube",
    author_handle: "@late_freight",
    content_text: "My last shipment arrived two weeks late, please check the tracking.",
    sentiment: "negative",
    converted_lead_id: "lead-mock-old",
    occurred_at: iso(-4),
    created_at: iso(-4),
  },
];

export const MOCK_SOCIAL_TENANT = TENANT;
