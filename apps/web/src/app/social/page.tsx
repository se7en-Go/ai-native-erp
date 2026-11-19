"use client";

import React, { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarClock, Globe2, Library, MessageCircle, Send, Sparkles } from "lucide-react";

import { AppShell } from "@/components/app-shell";
import { SocialPlatformMatrix } from "@/components/social/social-platform-matrix";
import { SocialContentCalendar } from "@/components/social/social-content-calendar";
import { SocialPostEditor } from "@/components/social/social-post-editor";
import { SocialMentionsFeed } from "@/components/social/social-mentions-feed";
import { getPlaybook } from "@/components/social/platform-playbooks";
import { count, panel, postStatusLabel, postStatusTone, platformLabel, primary } from "@/components/social/presentation";
import type {
  SkuOption,
  SocialPlatform,
  SocialPost,
  SocialPostDraft,
} from "@/components/social/types";
import { fetchCatalogVariants, fetchPendingProposalsCount } from "@/lib/data-source";
import { isProviderUnavailable } from "@/lib/phase3b-data";
import {
  connectSocialAccount,
  convertSocialMention,
  disconnectSocialAccount,
  fetchSocialAccounts,
  fetchSocialKpis,
  fetchSocialMentions,
  fetchSocialPosts,
  generateSocialCaption,
  publishSocialPost,
  saveSocialPosts,
} from "@/lib/phase3d-data";

type TabId = "matrix" | "calendar" | "editor" | "listening";

const TABS: ReadonlyArray<{ id: TabId; label: string; icon: typeof Globe2 }> = [
  { id: "matrix", label: "平台矩阵", icon: Globe2 },
  { id: "calendar", label: "发布中心与内容日历", icon: CalendarClock },
  { id: "editor", label: "贴文编辑器与 AI 文案工坊", icon: Sparkles },
  { id: "listening", label: "舆情监听与转线索", icon: MessageCircle },
];

/** Domestic networks without a publishing API register the brand account only. */
function suggestedHandle(platform: SocialPlatform): string {
  const playbook = getPlaybook(platform);
  return playbook.region === "domestic" ? playbook.label : `@${platform}`;
}

export default function SocialMarketingPage() {
  const queryClient = useQueryClient();
  const [tab, setTab] = useState<TabId>("matrix");
  const [pendingPlatforms, setPendingPlatforms] = useState<readonly SocialPlatform[]>([]);
  const [feedback, setFeedback] = useState<{ error: boolean; text: string } | null>(null);
  const [publishPending, setPublishPending] = useState<string | null>(null);

  const proposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () => fetchPendingProposalsCount(),
  });
  const kpisQuery = useQuery({ queryKey: ["social", "summary"], queryFn: fetchSocialKpis });
  const accountsQuery = useQuery({ queryKey: ["social", "accounts"], queryFn: fetchSocialAccounts });
  const postsQuery = useQuery({ queryKey: ["social", "posts"], queryFn: fetchSocialPosts });
  const mentionsQuery = useQuery({
    queryKey: ["social", "mentions"],
    queryFn: fetchSocialMentions,
  });
  const variantsQuery = useQuery({
    queryKey: ["catalog", "variants", "social"],
    queryFn: () => fetchCatalogVariants(""),
  });

  const invalidate = () => {
    for (const key of ["summary", "accounts", "posts", "mentions"]) {
      void queryClient.invalidateQueries({ queryKey: ["social", key] });
    }
  };

  const connect = useMutation({
    mutationFn: (platform: SocialPlatform) => {
      const existing = (accountsQuery.data ?? []).find((row) => row.platform === platform);
      return connectSocialAccount({
        platform,
        handle: existing?.handle ?? suggestedHandle(platform),
        display_name: existing?.display_name ?? getPlaybook(platform).label,
        followers_count: existing?.followers_count ?? 0,
      });
    },
    onMutate: (platform) => {
      setPendingPlatforms((prev) => [...prev, platform]);
      setFeedback(null);
    },
    onSuccess: (account) => {
      setFeedback({
        error: false,
        text: `${platformLabel(account.platform)} 已登记为「${account.display_name}」；未回传 OAuth 令牌时只能人工在官方后台发布。`,
      });
    },
    onError: () => setFeedback({ error: true, text: "授权失败，请重试。" }),
    onSettled: (_data, _error, platform) => {
      setPendingPlatforms((prev) => prev.filter((item) => item !== platform));
      invalidate();
    },
  });

  const disconnect = useMutation({
    mutationFn: (accountId: string) => disconnectSocialAccount(accountId),
    onMutate: () => setFeedback(null),
    onSuccess: (account) => {
      setFeedback({
        error: false,
        text: `${platformLabel(account.platform)} 已断开，服务端同时销毁了令牌密文。`,
      });
    },
    onError: () => setFeedback({ error: true, text: "断开失败，请重试。" }),
    onSettled: invalidate,
  });

  const saveDrafts = useMutation({
    mutationFn: (drafts: readonly SocialPostDraft[]) => saveSocialPosts(drafts, "draft"),
    onSuccess: (created) => {
      setFeedback({ error: false, text: `已保存 ${created.length} 条草稿。` });
    },
    onError: () => setFeedback({ error: true, text: "保存失败，请重试。" }),
    onSettled: invalidate,
  });

  const schedule = useMutation({
    mutationFn: (drafts: readonly SocialPostDraft[]) => saveSocialPosts(drafts, "scheduled"),
    onSuccess: (created) => {
      setFeedback({ error: false, text: `已提交 ${created.length} 条定时发布。` });
    },
    onError: () => setFeedback({ error: true, text: "定时发布失败，请重试。" }),
    onSettled: invalidate,
  });

  const publish = useMutation({
    mutationFn: (post: SocialPost) =>
      publishSocialPost(post.id, getPlaybook(post.platform).publish_mode === "manual_export"),
    onMutate: (post) => setPublishPending(post.id),
    onSuccess: (post) => {
      setFeedback({ error: false, text: `${platformLabel(post.platform)} 已标记为已发布。` });
    },
    onError: () => setFeedback({ error: true, text: "标记发布失败，请重试。" }),
    onSettled: () => {
      setPublishPending(null);
      invalidate();
    },
  });

  const convert = useMutation({
    mutationFn: (mentionId: string) => convertSocialMention(mentionId),
    onSuccess: () => {
      setFeedback({ error: false, text: "已写入 ERP 客户与线索池。" });
    },
    onError: () => setFeedback({ error: true, text: "转线索失败，请重试。" }),
    onSettled: invalidate,
  });

  const accounts = useMemo(() => accountsQuery.data ?? [], [accountsQuery.data]);
  const posts = useMemo(() => postsQuery.data ?? [], [postsQuery.data]);
  const mentions = mentionsQuery.data ?? [];
  const kpis = kpisQuery.data;

  const skus: SkuOption[] = useMemo(
    () =>
      (variantsQuery.data?.items ?? []).map((variant) => ({
        sku: variant.sku,
        label: variant.spu_code || variant.category || variant.sku,
      })),
    [variantsQuery.data]
  );

  // Open the calendar on the month that actually holds content.
  const calendarAnchor = useMemo(() => {
    const dates = posts
      .map((post) => post.scheduled_at ?? post.published_at)
      .filter((value): value is string => Boolean(value))
      .sort();
    return dates[0] ?? new Date().toISOString();
  }, [posts]);

  const queue = posts.filter((post) => post.status === "draft" || post.status === "scheduled");

  async function handleGenerateCaption(request: {
    platform: SocialPlatform;
    sku_ref: string | null;
    prompt: string;
  }): Promise<string> {
    try {
      return await generateSocialCaption(request);
    } catch (error) {
      if (isProviderUnavailable(error)) {
        setFeedback({
          error: true,
          text: "未配置 AI 供应商密钥，文案生成已按失败处理；请先用人工撰写或配置 BYOK。",
        });
      }
      throw error;
    }
  }

  const kpiCards = [
    { label: "内容总数", value: count(kpis?.total_posts ?? 0) },
    { label: "已发布", value: count(kpis?.published_posts ?? 0) },
    { label: "待发布", value: count(kpis?.scheduled_posts ?? 0) },
    { label: "社媒线索", value: count(kpis?.social_leads ?? 0) },
    { label: "已转线索", value: count(kpis?.converted_leads ?? 0) },
    { label: "已授权账号", value: count(kpis?.connected_accounts ?? 0) },
    { label: "支持平台", value: count(kpis?.supported_platforms ?? 14) },
  ];

  const error = kpisQuery.error ?? accountsQuery.error ?? postsQuery.error ?? mentionsQuery.error;

  return (
    <AppShell currentPath="/social" pendingProposalsCount={proposalsQuery.data?.total ?? 0}>
      <div className="mx-auto max-w-7xl space-y-5">
        <header className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="text-lg font-bold text-slate-900">社媒营销中心 / Social Marketing</h1>
            <p className="text-xs text-slate-500">
              国内外 14 平台矩阵、内容日历、AI 文案与舆情转线索。仅对具备发布接口的平台开放定时发布；
              小红书、抖音、快手、B站、视频号、微博走成片导出与官方后台人工发布。
            </p>
          </div>
          <span className="inline-flex items-center gap-1.5 rounded-full border border-fuchsia-200 bg-fuchsia-50 px-2.5 py-1 text-[11px] font-semibold text-fuchsia-900">
            <Send className="h-3.5 w-3.5" aria-hidden="true" />
            授权令牌信封加密 · 零未授权外网发帖
          </span>
        </header>

        <section aria-label="社媒营销概览指标" role="group" className="grid grid-cols-2 gap-2 lg:grid-cols-4 xl:grid-cols-7">
          {kpiCards.map((card) => (
            <div key={card.label} className="min-w-0 rounded-xl border border-fuchsia-100 bg-fuchsia-50/60 p-3">
              <p className="text-xs text-fuchsia-900">{card.label}</p>
              <p className="mt-1 break-all text-lg font-bold tabular-nums text-slate-900">{card.value}</p>
            </div>
          ))}
        </section>

        {error ? (
          <p role="alert" className="rounded-xl border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800">
            社媒数据加载失败，请稍后重试。
          </p>
        ) : null}
        {feedback ? (
          <p
            role={feedback.error ? "alert" : "status"}
            className={
              "rounded-xl border p-3 text-sm " +
              (feedback.error
                ? "border-rose-200 bg-rose-50 text-rose-800"
                : "border-fuchsia-200 bg-fuchsia-50 text-fuchsia-900")
            }
          >
            {feedback.text}
          </p>
        ) : null}

        <div role="tablist" aria-label="社媒功能分区" className="flex flex-wrap gap-1 rounded-xl border border-slate-200 bg-white p-1">
          {TABS.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              type="button"
              role="tab"
              id={`social-tab-${id}`}
              aria-selected={tab === id}
              aria-controls={`social-panel-${id}`}
              onClick={() => setTab(id)}
              className={
                "inline-flex items-center gap-1.5 rounded-lg px-3 py-2 text-sm font-medium focus-visible:outline-2 focus-visible:outline-fuchsia-700 " +
                (tab === id ? "bg-fuchsia-700 text-white" : "text-slate-700 hover:bg-slate-100")
              }
            >
              <Icon aria-hidden="true" size={15} />
              {label}
            </button>
          ))}
        </div>

        <div role="tabpanel" id={`social-panel-${tab}`} aria-labelledby={`social-tab-${tab}`}>
          {tab === "matrix" ? (
            <SocialPlatformMatrix
              accounts={accounts}
              onConnect={(platform) => connect.mutate(platform)}
              onDisconnect={(accountId) => disconnect.mutate(accountId)}
              pendingPlatforms={pendingPlatforms}
            />
          ) : null}

          {tab === "calendar" ? (
            <div className="space-y-4">
              <SocialContentCalendar
                posts={posts}
                anchor={calendarAnchor}
                onSelectPost={(postId) => {
                  const target = posts.find((post) => post.id === postId);
                  if (target) setFeedback({ error: false, text: `${platformLabel(target.platform)}：${target.caption}` });
                }}
                onCreatePost={() => setTab("editor")}
              />
              <section aria-label="待发布队列" className={panel}>
                <header className="mb-3">
                  <h2 className="flex items-center gap-2 text-lg font-bold">
                    <Library aria-hidden="true" size={18} />
                    发布中心
                  </h2>
                  <p className="text-sm text-slate-600">
                    标记发布只登记真实回执，不会代你在任何平台发起未授权发帖；人工发布平台需先在官方后台完成发布。
                  </p>
                </header>
                {!queue.length ? (
                  <p className="rounded-xl border border-dashed border-slate-300 p-4 text-sm text-slate-600">
                    当前没有待发布内容。
                  </p>
                ) : (
                  <ul className="space-y-2">
                    {queue.map((post) => (
                      <li key={post.id} className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-slate-200 p-3">
                        <div className="min-w-0">
                          <p className="flex flex-wrap items-center gap-1.5 text-xs text-slate-600">
                            <span className="rounded bg-slate-100 px-1.5 py-0.5 font-semibold text-slate-800">
                              {platformLabel(post.platform)}
                            </span>
                            <span className={"rounded-full border px-2 py-0.5 font-medium " + postStatusTone[post.status]}>
                              {postStatusLabel[post.status]}
                            </span>
                            <span>{getPlaybook(post.platform).publish_mode === "api" ? "API 发布" : "人工发布"}</span>
                          </p>
                          <p className="mt-1 break-words text-sm text-slate-800">{post.caption}</p>
                        </div>
                        <button
                          type="button"
                          disabled={publishPending === post.id}
                          className={primary}
                          aria-label={"标记已发布 " + platformLabel(post.platform) + " " + post.id}
                          onClick={() => publish.mutate(post)}
                        >
                          <Send aria-hidden="true" size={15} />
                          标记已发布
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </section>
            </div>
          ) : null}

          {tab === "editor" ? (
            <SocialPostEditor
              accounts={accounts}
              skus={skus}
              onGenerateCaption={handleGenerateCaption}
              onSaveDraft={(drafts) => saveDrafts.mutateAsync(drafts).then(() => undefined)}
              onSchedule={(drafts) => schedule.mutateAsync(drafts).then(() => undefined)}
            />
          ) : null}

          {tab === "listening" ? (
            <SocialMentionsFeed
              mentions={mentions}
              pendingId={convert.isPending ? convert.variables ?? null : null}
              onConvertToLead={(mentionId) => convert.mutateAsync(mentionId).then(() => undefined)}
            />
          ) : null}
        </div>
      </div>
    </AppShell>
  );
}
