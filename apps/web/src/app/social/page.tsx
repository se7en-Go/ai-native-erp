"use client";

import React, { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CalendarClock,
  Globe2,
  Layers,
  Library,
  MessageCircle,
  Send,
  ShieldCheck,
  Sparkles,
  Target,
} from "lucide-react";

import { AppShell } from "@/components/app-shell";
import { BentoStatCard } from "@/components/ui/bento-stat-card";
import { Page, PageHeader } from "@/components/ui/page";
import { SegmentedTabs } from "@/components/ui/segmented-tabs";
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
import { useI18n } from "@/lib/i18n";
import { isProviderUnavailable } from "@/lib/phase3b-data";
import {
  fetchSocialPlaybooks,
  SOCIAL_PLAYBOOK_REGIONS,
  type SocialPlaybookItem,
  type SocialPlaybookRegion,
} from "@/lib/api-client/pack-d-patrol";
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

type TabId = "matrix" | "playbooks" | "calendar" | "editor" | "listening";

const TABS: ReadonlyArray<{ id: TabId; label: string; icon: typeof Globe2 }> = [
  { id: "matrix", label: "平台矩阵", icon: Globe2 },
  { id: "playbooks", label: "平台玩法矩阵", icon: Layers },
  { id: "calendar", label: "发布中心与内容日历", icon: CalendarClock },
  { id: "editor", label: "贴文编辑器与 AI 文案工坊", icon: Sparkles },
  { id: "listening", label: "舆情监听与转线索", icon: MessageCircle },
];

const REGION_LABEL: Record<Exclude<SocialPlaybookRegion, "all">, string> = {
  overseas: "海外",
  domestic: "国内",
};

const PLAYBOOK_TONE: Record<Exclude<SocialPlaybookRegion, "all">, string> = {
  overseas:
    "border-blue-200 bg-blue-50 text-blue-800 dark:border-blue-800/70 dark:bg-blue-950/40 dark:text-blue-300",
  domestic:
    "border-rose-200 bg-rose-50 text-rose-800 dark:border-rose-800/70 dark:bg-rose-950/40 dark:text-rose-300",
};

/** 14 平台玩法矩阵：每张卡片都是可直接执行的运营配方，不是宣传语。 */
function SocialPlaybooksBoard() {
  const { bi, locale } = useI18n();
  const [region, setRegion] = useState<SocialPlaybookRegion>("all");

  // Counts come from the unfiltered call so the pills can show the real split.
  const allQuery = useQuery({
    queryKey: ["social", "playbooks", "all"],
    queryFn: () => fetchSocialPlaybooks("all"),
  });
  const regionQuery = useQuery({
    queryKey: ["social", "playbooks", region],
    queryFn: () => fetchSocialPlaybooks(region),
    enabled: region !== "all",
  });

  const all = allQuery.data ?? [];
  const items: readonly SocialPlaybookItem[] = region === "all" ? all : regionQuery.data ?? [];
  const counts: Record<SocialPlaybookRegion, number> = {
    all: all.length,
    overseas: all.filter((row) => row.region === "overseas").length,
    domestic: all.filter((row) => row.region === "domestic").length,
  };
  const error = allQuery.error ?? regionQuery.error;
  const loading = region === "all" ? allQuery.isPending : regionQuery.isPending;

  return (
    <section aria-label="平台玩法矩阵" data-testid="social-playbooks-board" className="space-y-4">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="flex items-center gap-2 text-[15px] font-bold tracking-tight text-slate-950 dark:text-slate-100">
            <span className="grid h-7 w-7 place-items-center rounded-lg bg-blue-50 text-blue-600 ring-1 ring-blue-100 dark:bg-blue-900/30 dark:text-blue-400 dark:ring-blue-800/60">
              <Layers aria-hidden="true" size={15} />
            </span>
            {bi("平台玩法矩阵", "Platform playbooks")}
          </h2>
          <p className="mt-1 max-w-3xl text-[12.5px] leading-6 text-slate-600 dark:text-slate-400">
            {bi(
              "海外 7 个 + 国内 7 个平台的受众画像、获客漏斗与实战要点；能否官方 API 直发由平台授权决定，其余一律导出后在官方后台人工发布。",
              "Audience, acquisition funnel and hands-on practices for 7 overseas and 7 domestic platforms; whether a platform supports direct publishing is decided by its official API, everything else is exported and published manually in the official console."
            )}
          </p>
        </div>

        <div role="group" aria-label={bi("玩法地区筛选", "Playbook region filter")} className="flex flex-wrap items-center gap-2">
          {SOCIAL_PLAYBOOK_REGIONS.map((option) => {
            const active = option.value === region;
            return (
              <button
                key={option.value}
                type="button"
                data-testid={`playbooks-region-${option.value}`}
                aria-pressed={active}
                onClick={() => setRegion(option.value)}
                className={
                  "inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-[12.5px] font-semibold transition-colors " +
                  (active
                    ? "border-blue-600 bg-blue-600 text-white shadow-xs dark:border-blue-500 dark:bg-blue-600"
                    : "border-slate-200 bg-white text-slate-700 hover:border-slate-300 hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300 dark:hover:border-slate-600 dark:hover:bg-slate-800")
                }
              >
                {bi(option.label_zh, option.label_en)}
                <span
                  className={
                    "rounded-full px-1.5 py-0.2 text-[10.5px] font-bold tabular-nums " +
                    (active
                      ? "bg-white/20 text-white"
                      : "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300")
                  }
                >
                  ({counts[option.value]})
                </span>
              </button>
            );
          })}
        </div>
      </header>

      {error ? (
        <p role="alert" data-testid="social-playbooks-error" className="rounded-xl border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800 dark:border-rose-800/70 dark:bg-rose-950/40 dark:text-rose-300">
          {bi("平台玩法加载失败，请稍后重试。", "Failed to load platform playbooks. Please retry.")}
        </p>
      ) : null}

      {loading && !items.length ? (
        <p data-testid="social-playbooks-loading" className="text-sm text-slate-500 dark:text-slate-400">
          {bi("正在加载平台玩法…", "Loading platform playbooks…")}
        </p>
      ) : null}

      {!loading && !items.length ? (
        <p data-testid="social-playbooks-empty" className="rounded-xl border border-dashed border-slate-300 bg-slate-50 p-4 text-sm text-slate-600 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-400">
          {bi("该地区暂无平台玩法。", "No playbook for this region yet.")}
        </p>
      ) : null}

      {/* 14 张玩法卡：两列 7×2 刚好齐平，三列会导致末行留 1 个空洞。 */}
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        {items.map((row) => (
          <article
            key={row.platform}
            data-testid={`playbook-card-${row.platform}`}
            className="flex min-w-0 flex-col rounded-2xl border border-slate-200/90 bg-white p-4 shadow-[0_1px_3px_rgba(15,23,42,0.06),0_6px_16px_-6px_rgba(15,23,42,0.05)] transition-all duration-200 hover:-translate-y-0.5 hover:border-blue-300 hover:shadow-[0_6px_20px_-8px_rgba(37,99,235,0.28)] dark:border-slate-800 dark:bg-slate-900 dark:hover:border-blue-700"
          >
            <header className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <h3 className="truncate text-[14.5px] font-bold tracking-tight text-slate-950 dark:text-slate-100">
                  {bi(row.name_zh, row.name_en)}
                </h3>
                <p className="mt-0.5 truncate text-[11.5px] text-slate-600 dark:text-slate-400">
                  {bi(row.category_zh, row.category_en)}
                </p>
              </div>
              <div className="flex shrink-0 flex-col items-end gap-1">
                <span
                  className={
                    "rounded-full border px-2 py-0.5 text-[10.5px] font-semibold " +
                    PLAYBOOK_TONE[row.region]
                  }
                >
                  {REGION_LABEL[row.region]}
                </span>
                <span
                  className={
                    "rounded-full border px-2 py-0.5 text-[10.5px] font-semibold " +
                    (row.official_api_supported
                      ? "border-emerald-200 bg-emerald-50 text-emerald-800 dark:border-emerald-800/70 dark:bg-emerald-950/40 dark:text-emerald-300"
                      : "border-amber-200 bg-amber-50 text-amber-900 dark:border-amber-800/70 dark:bg-amber-950/40 dark:text-amber-300")
                  }
                >
                  {row.official_api_supported
                    ? bi("官方 API 直发", "Official API publish")
                    : bi("导出发布", "Export & publish")}
                </span>
              </div>
            </header>

            <dl className="mt-3 space-y-2.5 text-[11.5px] leading-5">
              <div>
                <dt className="flex items-center gap-1 font-semibold text-emerald-800 dark:text-emerald-400">
                  <Target aria-hidden="true" size={12} />
                  {bi("受众画像", "Audience")}
                </dt>
                <dd className="mt-0.5 text-slate-700 dark:text-slate-300">
                  {bi(row.audience_zh, row.audience_en)}
                </dd>
              </div>
              <div>
                <dt className="flex items-center gap-1 font-semibold text-blue-800 dark:text-blue-400">
                  <Send aria-hidden="true" size={12} />
                  {bi("获客漏斗", "Lead funnel")}
                </dt>
                <dd className="mt-0.5 text-slate-700 dark:text-slate-300">
                  {bi(row.lead_funnel_zh, row.lead_funnel_en)}
                </dd>
              </div>
              <div>
                <dt className="flex items-center gap-1 font-semibold text-slate-800 dark:text-slate-200">
                  <Sparkles aria-hidden="true" size={12} />
                  {bi("实战要点", "Hands-on practices")}
                </dt>
                <dd className="mt-1">
                  <ul className="space-y-1">
                    {(locale === "zh" ? row.best_practices_zh : row.best_practices_en).map(
                      (practice) => (
                        <li
                          key={practice}
                          className="flex items-start gap-1.5 text-slate-700 dark:text-slate-300"
                        >
                          <span
                            aria-hidden="true"
                            className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-emerald-500"
                          />
                          <span>{practice}</span>
                        </li>
                      )
                    )}
                  </ul>
                </dd>
              </div>
            </dl>

            <footer className="mt-auto border-t border-slate-100 pt-2.5 dark:border-slate-800">
              <p className="flex items-start gap-1 text-[10.5px] leading-4 text-slate-500 dark:text-slate-400">
                <ShieldCheck
                  aria-hidden="true"
                  size={12}
                  className="mt-0.5 shrink-0 text-slate-400 dark:text-slate-500"
                />
                <span>{bi(row.compliance_note_zh, row.compliance_note_en)}</span>
              </p>
            </footer>
          </article>
        ))}
      </div>
    </section>
  );
}

/** Domestic networks without a publishing API register the brand account only. */
function suggestedHandle(platform: SocialPlatform): string {
  const playbook = getPlaybook(platform);
  return playbook.region === "domestic" ? playbook.label : `@${platform}`;
}

export default function SocialMarketingPage() {
  const { bi } = useI18n();
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
    { label: "内容总数", value: count(kpis?.total_posts ?? 0), icon: Globe2, accent: "blue" as const },
    { label: "已发布", value: count(kpis?.published_posts ?? 0), icon: Send, accent: "emerald" as const },
    {
      label: "待发布",
      value: count(kpis?.scheduled_posts ?? 0),
      icon: CalendarClock,
      accent: "amber" as const,
    },
    {
      label: "社媒线索",
      value: count(kpis?.social_leads ?? 0),
      icon: MessageCircle,
      accent: "violet" as const,
    },
    {
      label: "已转线索",
      value: count(kpis?.converted_leads ?? 0),
      icon: Sparkles,
      accent: "rose" as const,
    },
    {
      label: "已授权账号",
      value: count(kpis?.connected_accounts ?? 0),
      icon: Send,
      accent: "slate" as const,
    },
    {
      label: "支持平台",
      value: count(kpis?.supported_platforms ?? 14),
      icon: Globe2,
      accent: "blue" as const,
    },
  ];

  const error = kpisQuery.error ?? accountsQuery.error ?? postsQuery.error ?? mentionsQuery.error;

  return (
    <AppShell currentPath="/social" pendingProposalsCount={proposalsQuery.data?.total ?? 0}>
      <Page full className="space-y-5">
        <PageHeader
          title={bi("社媒营销中心", "Social marketing")}
          description={bi(
            "国内外 14 平台矩阵、内容日历、AI 文案与舆情转线索。仅对具备发布接口的平台开放定时发布；小红书、抖音、快手、B站、视频号、微博走成片导出与官方后台人工发布。",
            "A 14-platform matrix, content calendar, AI copywriting and social listening to CRM leads. Scheduled publishing is only enabled for platforms with an API; Xiaohongshu, Douyin, Kuaishou, Bilibili, Channels and Weibo use export plus manual publishing in the official console."
          )}
          badge={
            <span className="inline-flex items-center gap-1.5 rounded-full border border-blue-200 bg-blue-50 px-2.5 py-1 text-[11px] font-semibold text-blue-900 dark:border-blue-800/70 dark:bg-blue-950/40 dark:text-blue-300">
              <Send className="h-3.5 w-3.5" aria-hidden="true" />
              {bi("授权令牌信封加密 · 零未授权外网发帖", "Envelope-encrypted tokens · zero unauthorised posts")}
            </span>
          }
        />

        <section
          aria-label="社媒营销概览指标"
          role="group"
          className="grid grid-cols-2 gap-2.5 lg:grid-cols-4 xl:grid-cols-7"
        >
          {kpiCards.map((card) => (
            <BentoStatCard
              key={card.label}
              label={card.label}
              value={card.value}
              icon={card.icon}
              accent={card.accent}
            />
          ))}
        </section>

        {error ? (
          <p role="alert" className="rounded-xl border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800 dark:border-rose-800/70 dark:bg-rose-950/40 dark:text-rose-300">
            {bi("社媒数据加载失败，请稍后重试。", "Failed to load social data. Please retry.")}
          </p>
        ) : null}
        {feedback ? (
          <p
            role={feedback.error ? "alert" : "status"}
            className={
              "rounded-xl border p-3 text-sm " +
              (feedback.error
                ? "border-rose-200 bg-rose-50 text-rose-800 dark:border-rose-800/70 dark:bg-rose-950/40 dark:text-rose-300"
                : "border-blue-200 bg-blue-50 text-blue-900 dark:border-blue-800/70 dark:bg-blue-950/40 dark:text-blue-200")
            }
          >
            {feedback.text}
          </p>
        ) : null}

        <div className="flex flex-wrap items-center gap-2">
          <SegmentedTabs
            ariaLabel="社媒功能分区"
            value={tab}
            onChange={setTab}
            options={TABS.map(({ id, label, icon: Icon }) => ({
              value: id,
              testId: id === "playbooks" ? "social-playbooks-tab" : undefined,
              label: (
                <span className="inline-flex items-center gap-1.5">
                  <Icon aria-hidden="true" size={15} />
                  {label}
                </span>
              ),
            }))}
          />
          <span className="rounded-full border border-slate-200 bg-white px-2.5 py-0.5 text-[11px] font-semibold tabular-nums text-slate-700 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300">
            {queue.length} 条待发布
          </span>
        </div>

        <div role="tabpanel" aria-label={TABS.find((item) => item.id === tab)?.label}>
          {tab === "matrix" ? (
            <SocialPlatformMatrix
              accounts={accounts}
              onConnect={(platform) => connect.mutate(platform)}
              onDisconnect={(accountId) => disconnect.mutate(accountId)}
              pendingPlatforms={pendingPlatforms}
            />
          ) : null}

          {tab === "playbooks" ? <SocialPlaybooksBoard /> : null}

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
              <section
                aria-label="待发布队列"
                className={panel + " dark:border-slate-800 dark:bg-slate-900 dark:text-slate-100"}
              >
                <header className="mb-3">
                  <h2 className="flex items-center gap-2 text-[15px] font-bold text-slate-900 dark:text-slate-100">
                    <span className="grid h-7 w-7 place-items-center rounded-lg bg-blue-50 text-blue-600 ring-1 ring-blue-100 dark:bg-blue-900/30 dark:text-blue-400 dark:ring-blue-800/60">
                      <Library aria-hidden="true" size={15} />
                    </span>
                    发布中心
                  </h2>
                  <p className="mt-1 text-[12.5px] leading-6 text-slate-600 dark:text-slate-400">
                    标记发布只登记真实回执，不会代你在任何平台发起未授权发帖；人工发布平台需先在官方后台完成发布。
                  </p>
                </header>
                {!queue.length ? (
                  <p className="rounded-xl border border-dashed border-slate-300 p-4 text-sm text-slate-600 dark:border-slate-700 dark:text-slate-400">
                    当前没有待发布内容。
                  </p>
                ) : (
                  <ul className="space-y-2">
                    {queue.map((post) => (
                      <li
                        key={post.id}
                        className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-slate-200 bg-white p-3 shadow-[0_1px_2px_rgba(15,23,42,0.04)] dark:border-slate-700 dark:bg-slate-800/60"
                      >
                        <div className="min-w-0">
                          <p className="flex flex-wrap items-center gap-1.5 text-xs text-slate-600 dark:text-slate-400">
                            <span className="rounded bg-slate-100 px-1.5 py-0.5 font-semibold text-slate-800 dark:bg-slate-700 dark:text-slate-200">
                              {platformLabel(post.platform)}
                            </span>
                            <span className={"rounded-full border px-2 py-0.5 font-medium " + postStatusTone[post.status]}>
                              {postStatusLabel[post.status]}
                            </span>
                            <span>{getPlaybook(post.platform).publish_mode === "api" ? "API 发布" : "人工发布"}</span>
                          </p>
                          <p className="mt-1 break-words text-sm text-slate-800 dark:text-slate-200">
                            {post.caption}
                          </p>
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
      </Page>
    </AppShell>
  );
}
