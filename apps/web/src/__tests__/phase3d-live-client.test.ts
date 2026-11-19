// Phase 3D live adapter tests: the generated SDK must hit the exact BFF URLs from
// contracts/openapi.json, map the contract onto the social UI shapes, never carry an
// OAuth token back into the page, and surface domain error codes unchanged.
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

import type {
  AccountRead,
  MentionRead,
  PlatformRead,
  PostRead,
  SocialSummary,
} from "@/lib/api-client/types.gen";
import {
  connectSocialAccount,
  convertMentionToLead,
  createMention,
  createPost,
  disconnectSocialAccount,
  fetchSocialAccounts,
  fetchSocialMentions,
  fetchSocialPlatforms,
  fetchSocialPosts,
  fetchSocialSummary,
  generateSocialCaption,
  mapAccount,
  mapMention,
  mapPost,
  mapSummary,
  publishPost,
  updatePost,
} from "@/lib/api-client/phase3d";

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

const summaryRow: SocialSummary = {
  total_posts: 3,
  published_posts: 1,
  scheduled_posts: 1,
  draft_posts: 1,
  social_leads: 1,
  converted_leads: 0,
  mentions_total: 3,
  connected_accounts: 3,
  supported_platforms: 14,
};

const accountRow: AccountRead = {
  id: "acc-1",
  platform: "instagram",
  region: "overseas",
  handle: "@quanzhou_smart_led",
  display_name: "Quanzhou Smart LED",
  external_account_id: null,
  status: "connected",
  followers_count: 4820,
  config: { demo: true },
  has_stored_token: true,
  created_at: "2026-11-19T09:00:00Z",
  updated_at: "2026-11-19T09:00:00Z",
};

const postRow: PostRead = {
  id: "post-1",
  social_account_id: "acc-1",
  platform: "instagram",
  title: "IP65 workshop light",
  caption: "IP65 work light, factory direct",
  hashtags: ["#worklight"],
  media_urls: [],
  sku_ref: "LED-COB-24V-5M",
  status: "scheduled",
  scheduled_at: "2026-11-20T09:30:00Z",
  published_at: null,
  external_post_id: null,
  metrics: {},
  created_by: "user-1",
  created_at: "2026-11-19T09:00:00Z",
  updated_at: "2026-11-19T09:00:00Z",
};

const mentionRow: MentionRead = {
  id: "mention-1",
  social_account_id: "acc-2",
  platform: "tiktok",
  author_handle: "@dubai_build_supply",
  content_text: "Need 500 pcs of your IP65 work light.",
  sentiment: "lead_intent",
  converted_lead_id: null,
  occurred_at: "2026-11-19T08:00:00Z",
  created_at: "2026-11-19T08:00:00Z",
};

const platformRow: PlatformRead = {
  platform: "xiaohongshu",
  region: "domestic",
  label: "小红书",
  emoji: "📕",
  audience: "一二线城市年轻女性与家居美学人群",
  acquisition_plays: ["达人种草与合集"],
  best_practices: ["3:4 封面 + 20 字内标题"],
  content_formats: ["图文笔记"],
  publish_mode: "manual_export",
  supports_scheduled_publish: false,
  compliance_note: "无 write_notes 权限，需人工在创作中心发布。",
};

describe("Phase 3D live adapters", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "false");
  });
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("maps the summary and accounts without leaking the stored token", () => {
    expect(mapSummary(summaryRow)).toEqual({
      total_posts: 3,
      published_posts: 1,
      scheduled_posts: 1,
      draft_posts: 1,
      social_leads: 1,
      converted_leads: 0,
      mentions_total: 3,
      connected_accounts: 3,
      supported_platforms: 14,
    });
    const account = mapAccount(accountRow);
    expect(account).toEqual({
      id: "acc-1",
      platform: "instagram",
      region: "overseas",
      handle: "@quanzhou_smart_led",
      display_name: "Quanzhou Smart LED",
      status: "connected",
      followers_count: 4820,
    });
    expect(JSON.stringify(account)).not.toContain("token");
  });

  it("maps posts and mentions onto the calendar/feed shapes", () => {
    expect(mapPost(postRow)).toMatchObject({
      id: "post-1",
      platform: "instagram",
      status: "scheduled",
      hashtags: ["#worklight"],
      sku_ref: "LED-COB-24V-5M",
    });
    expect(mapMention(mentionRow)).toEqual({
      id: "mention-1",
      platform: "tiktok",
      author_handle: "@dubai_build_supply",
      content_text: "Need 500 pcs of your IP65 work light.",
      sentiment: "lead_intent",
      occurred_at: "2026-11-19T08:00:00Z",
      converted_lead_id: null,
    });
  });

  it("reads KPIs, the platform matrix, accounts, posts and mentions from /api/v1/social", async () => {
    const captured = stubFetch((call) => {
      if (call.url.includes("/social/summary")) return jsonResponse(summaryRow);
      if (call.url.includes("/social/platforms")) return jsonResponse([platformRow]);
      if (call.url.includes("/social/accounts")) return jsonResponse(page([accountRow]));
      if (call.url.includes("/social/posts")) return jsonResponse(page([postRow]));
      if (call.url.includes("/social/mentions")) return jsonResponse(page([mentionRow]));
      throw new Error(`unexpected call: ${call.url}`);
    });

    expect(await fetchSocialSummary()).toMatchObject({ supported_platforms: 14 });
    const platforms = await fetchSocialPlatforms();
    expect(platforms).toHaveLength(1);
    expect(platforms[0]).toMatchObject({ platform: "xiaohongshu", publish_mode: "manual_export" });
    expect(await fetchSocialAccounts()).toHaveLength(1);
    expect(await fetchSocialPosts()).toHaveLength(1);
    expect(await fetchSocialMentions()).toHaveLength(1);

    expect(captured.map((call) => call.url)).toEqual([
      `${BFF}/api/v1/social/summary`,
      `${BFF}/api/v1/social/platforms`,
      `${BFF}/api/v1/social/accounts?limit=50`,
      `${BFF}/api/v1/social/posts?limit=100`,
      `${BFF}/api/v1/social/mentions?limit=100`,
    ]);
  });

  it("connects and disconnects accounts through the generated SDK", async () => {
    const captured = stubFetch(() => jsonResponse(accountRow, 201));

    await connectSocialAccount({
      platform: "instagram",
      handle: "@quanzhou_smart_led",
      display_name: "Quanzhou Smart LED",
      access_token: "IGQVJ-secret",
    });
    await disconnectSocialAccount("acc-1");

    expect(captured[0].url).toBe(`${BFF}/api/v1/social/accounts/connect`);
    expect(captured[0].method).toBe("POST");
    expect(JSON.parse(captured[0].bodyText ?? "{}")).toMatchObject({
      platform: "instagram",
      handle: "@quanzhou_smart_led",
    });
    expect(captured[1].url).toBe(`${BFF}/api/v1/social/accounts/acc-1/disconnect`);
    expect(captured[1].method).toBe("POST");
  });

  it("creates, patches and publishes posts including manual confirmation", async () => {
    const captured = stubFetch(() =>
      jsonResponse({ ...postRow, status: "published", published_at: "2026-11-19T10:00:00Z" })
    );

    await createPost({
      platform: "instagram",
      caption: "IP65 work light, factory direct",
      hashtags: ["#worklight"],
      sku_ref: "LED-COB-24V-5M",
      status: "scheduled",
      scheduled_at: "2026-11-20T09:30:00Z",
    });
    await updatePost("post-1", { caption: "edited caption" });
    await publishPost("post-1", { manual_confirmation: true });

    expect(captured[0].url).toBe(`${BFF}/api/v1/social/posts`);
    expect(JSON.parse(captured[0].bodyText ?? "{}")).toMatchObject({
      platform: "instagram",
      status: "scheduled",
      hashtags: ["#worklight"],
    });
    expect(captured[1].url).toBe(`${BFF}/api/v1/social/posts/post-1`);
    expect(captured[1].method).toBe("PATCH");
    expect(captured[2].url).toBe(`${BFF}/api/v1/social/posts/post-1/publish`);
    expect(JSON.parse(captured[2].bodyText ?? "{}")).toEqual({ manual_confirmation: true });
  });

  it("generates captions and converts mentions into CRM leads", async () => {
    const captured = stubFetch((call) => {
      if (call.url.includes("/caption/generate")) {
        return jsonResponse({
          platform: "instagram",
          text: "Factory-direct IP65 work light.",
          language: "zh",
          sku_ref: "LED-COB-24V-5M",
          model_used: "stub-model",
          credits_consumed: "2.0000",
          grounded_tool_call_id: "tc_1",
        });
      }
      if (call.url.endsWith("/convert-lead")) {
        return jsonResponse({
          mention_id: "mention-1",
          lead_id: "lead-1",
          customer_id: "customer-1",
          already_converted: false,
        });
      }
      return jsonResponse(mentionRow, 201);
    });

    expect(
      await generateSocialCaption({
        platform: "instagram",
        prompt: "突出 IP65 与工厂直供",
        sku_ref: "LED-COB-24V-5M",
        language: "zh",
      })
    ).toMatchObject({ text: "Factory-direct IP65 work light.", model_used: "stub-model" });
    await createMention({
      platform: "tiktok",
      author_handle: "@dubai_build_supply",
      content_text: "Need 500 pcs.",
      sentiment: "lead_intent",
    });
    expect(await convertMentionToLead("mention-1")).toMatchObject({
      lead_id: "lead-1",
      already_converted: false,
    });

    expect(captured[0].url).toBe(`${BFF}/api/v1/social/caption/generate`);
    expect(captured[1].url).toBe(`${BFF}/api/v1/social/mentions`);
    expect(captured[2].url).toBe(`${BFF}/api/v1/social/mentions/mention-1/convert-lead`);
  });

  it("surfaces the manual-publish domain code instead of a generic failure", async () => {
    stubFetch(() =>
      jsonResponse(
        {
          type: "https://erp.local/errors/social-manual-publish-only",
          title: "Manual Publishing Required",
          status: 422,
          detail: "小红书 exposes no third-party publishing API.",
          instance: "/api/v1/social/posts",
          code: "ERR_SOCIAL_MANUAL_PUBLISH_ONLY",
        },
        422
      )
    );

    await expect(
      createPost({ platform: "xiaohongshu", caption: "笔记", status: "scheduled" })
    ).rejects.toMatchObject({ code: "ERR_SOCIAL_MANUAL_PUBLISH_ONLY" });
  });
});
