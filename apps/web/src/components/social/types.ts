export type SocialPlatform =
  | "instagram" | "tiktok" | "facebook" | "youtube" | "linkedin" | "pinterest" | "x"
  | "xiaohongshu" | "douyin" | "kuaishou" | "bilibili" | "wechat_video" | "wechat_oa" | "weibo";
export type SocialRegion = "overseas" | "domestic";
/** api: the platform exposes a publishing interface; manual_export: prepare assets for the official console. */
export type PublishMode = "api" | "manual_export";
export type SocialAccountStatus = "connected" | "disconnected" | "token_expired";
export type SocialPostStatus = "draft" | "scheduled" | "published" | "failed";
export type SocialSentiment = "positive" | "neutral" | "negative" | "lead_intent";

export interface PlatformPlaybook {
  platform: SocialPlatform;
  region: SocialRegion;
  label: string;
  emoji: string;
  /** Who buys on this platform. */
  audience: string;
  /** Repeatable acquisition plays for the reference workflow. */
  acquisition_plays: readonly string[];
  best_practices: readonly string[];
  content_formats: readonly string[];
  publish_mode: PublishMode;
  compliance_note: string;
}

export interface SocialAccount {
  id: string;
  platform: SocialPlatform;
  region: SocialRegion;
  handle: string;
  display_name: string;
  status: SocialAccountStatus;
  followers_count: number;
}

export interface SocialPost {
  id: string;
  platform: SocialPlatform;
  title: string;
  caption: string;
  hashtags: readonly string[];
  sku_ref: string | null;
  status: SocialPostStatus;
  scheduled_at: string | null;
  published_at: string | null;
}

/** One row per platform: social_posts stores a single platform per record. */
export interface SocialPostDraft {
  platform: SocialPlatform;
  title: string;
  caption: string;
  hashtags: readonly string[];
  sku_ref: string | null;
  scheduled_at: string | null;
}

export interface SocialMention {
  id: string;
  platform: SocialPlatform;
  author_handle: string;
  content_text: string;
  sentiment: SocialSentiment;
  occurred_at: string;
  converted_lead_id: string | null;
}

export interface SkuOption {
  sku: string;
  label: string;
}
