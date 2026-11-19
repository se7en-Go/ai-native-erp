import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { SocialPlatformMatrix } from "@/components/social/social-platform-matrix";
import { SocialContentCalendar } from "@/components/social/social-content-calendar";
import { SocialPostEditor } from "@/components/social/social-post-editor";
import { SocialMentionsFeed } from "@/components/social/social-mentions-feed";
import { PLATFORM_PLAYBOOKS } from "@/components/social/platform-playbooks";
import type { SocialAccount, SocialMention, SocialPost } from "@/components/social/types";

const accounts: SocialAccount[] = [
  { id: "a1", platform: "instagram", region: "overseas", handle: "@lumenlight", display_name: "Lumen Light", status: "connected", followers_count: 12345 },
  { id: "a2", platform: "xiaohongshu", region: "domestic", handle: "流明照明", display_name: "流明照明", status: "connected", followers_count: 800 },
  { id: "a3", platform: "wechat_oa", region: "domestic", handle: "lumen-oa", display_name: "流明公众号", status: "token_expired", followers_count: 320 },
];

const posts: SocialPost[] = [
  { id: "p1", platform: "instagram", title: "Work light reel", caption: "IP65 work light", hashtags: ["#worklight"], sku_ref: "LED-1", status: "scheduled", scheduled_at: "2026-11-19T09:30:00Z", published_at: null },
  { id: "p2", platform: "xiaohongshu", title: "工地照明笔记", caption: "工地照明怎么选", hashtags: [], sku_ref: null, status: "draft", scheduled_at: "2026-11-02T02:00:00Z", published_at: null },
  { id: "p3", platform: "tiktok", title: "Factory tour", caption: "Inside the factory", hashtags: [], sku_ref: null, status: "published", scheduled_at: "2026-11-20T12:00:00Z", published_at: "2026-11-20T12:05:00Z" },
  { id: "p4", platform: "youtube", title: "Install guide", caption: "Install guide", hashtags: [], sku_ref: null, status: "failed", scheduled_at: "2026-11-21T12:00:00Z", published_at: null },
];

const mentions: SocialMention[] = [
  { id: "m1", platform: "instagram", author_handle: "@amy", content_text: "Great lamp!", sentiment: "positive", occurred_at: "2026-11-18T08:00:00Z", converted_lead_id: null },
  { id: "m2", platform: "tiktok", author_handle: "@bulk_buyer", content_text: "Where can I buy in bulk for my shop?", sentiment: "lead_intent", occurred_at: "2026-11-19T08:00:00Z", converted_lead_id: null },
  { id: "m3", platform: "x", author_handle: "@late_order", content_text: "Shipment is late again", sentiment: "negative", occurred_at: "2026-11-17T08:00:00Z", converted_lead_id: "lead-9" },
];

describe("social components", () => {
  it("ships all fourteen playbooks with the locked platform tokens", () => {
    expect(PLATFORM_PLAYBOOKS).toHaveLength(14);
    expect(PLATFORM_PLAYBOOKS.filter((item) => item.region === "domestic")).toHaveLength(7);
    expect(PLATFORM_PLAYBOOKS.filter((item) => item.region === "overseas")).toHaveLength(7);
    expect(PLATFORM_PLAYBOOKS.map((item) => item.platform).sort()).toEqual([
      "bilibili", "douyin", "facebook", "instagram", "kuaishou", "linkedin", "pinterest",
      "tiktok", "wechat_oa", "wechat_video", "weibo", "x", "xiaohongshu", "youtube",
    ]);
    expect(PLATFORM_PLAYBOOKS.every((item) => item.audience && item.acquisition_plays.length > 0 && item.best_practices.length > 0)).toBe(true);
    expect(PLATFORM_PLAYBOOKS.find((item) => item.platform === "xiaohongshu")?.publish_mode).toBe("manual_export");
    expect(PLATFORM_PLAYBOOKS.find((item) => item.platform === "instagram")?.publish_mode).toBe("api");
  });

  it("renders KPI cards, filters by region and search, and delegates auth actions", async () => {
    const user = userEvent.setup();
    const connect = vi.fn(), disconnect = vi.fn();
    render(<SocialPlatformMatrix accounts={accounts} onConnect={connect} onDisconnect={disconnect} />);
    const matrix = screen.getByRole("region", { name: "社媒平台矩阵" });
    expect(within(matrix).getByText("14")).toBeVisible();
    expect(within(matrix).getByText("13,465")).toBeVisible();
    expect(screen.getByText("授权过期")).toBeVisible();
    expect(screen.getAllByRole("article")).toHaveLength(14);
    expect(within(screen.getByRole("article", { name: "小红书" })).getByText("手动导出")).toBeVisible();
    expect(within(screen.getByRole("article", { name: "Instagram" })).getByText("API 自动发布")).toBeVisible();
    await user.click(within(matrix).getByRole("button", { name: "国内" }));
    expect(screen.getAllByRole("article")).toHaveLength(7);
    await user.click(within(matrix).getByRole("button", { name: "海外" }));
    expect(screen.getAllByRole("article")).toHaveLength(7);
    await user.click(within(matrix).getByRole("button", { name: "全部" }));
    await user.type(within(matrix).getByRole("searchbox", { name: "搜索平台" }), "Pinterest");
    expect(screen.getAllByRole("article")).toHaveLength(1);
    expect(screen.getByRole("article", { name: "Pinterest" })).toBeVisible();
    await user.clear(within(matrix).getByRole("searchbox", { name: "搜索平台" }));
    await user.click(within(screen.getByRole("article", { name: "Instagram" })).getByRole("button", { name: "断开 Instagram" }));
    expect(disconnect).toHaveBeenCalledWith("a1");
    await user.click(within(screen.getByRole("article", { name: "Pinterest" })).getByRole("button", { name: "去授权 Pinterest" }));
    expect(connect).toHaveBeenCalledWith("pinterest");
    expect(document.body).not.toHaveTextContent("[object Object]");
  });

  it("switches between month and week calendar views with status labels", async () => {
    const user = userEvent.setup();
    const select = vi.fn();
    render(<SocialContentCalendar posts={posts} anchor="2026-11-19" onSelectPost={select} />);
    const calendar = screen.getByRole("grid", { name: "内容日历" });
    expect(screen.getByText("2026 年 11 月")).toBeVisible();
    for (const label of ["草稿", "已排期", "已发布", "失败"]) expect(screen.getByText(label)).toBeVisible();
    expect(within(calendar).getByRole("gridcell", { name: /2026-11-02/ })).toHaveTextContent("小红书");
    expect(within(calendar).getByRole("gridcell", { name: /2026-11-19/ })).toHaveTextContent("Instagram");
    await user.click(within(calendar).getByRole("button", { name: /Instagram.*09:30/ }));
    expect(select).toHaveBeenCalledWith("p1");
    await user.click(screen.getByRole("button", { name: "周视图" }));
    const week = screen.getByRole("grid", { name: "内容日历" });
    expect(within(week).getAllByRole("gridcell")).toHaveLength(7);
    expect(within(week).queryByRole("gridcell", { name: /2026-11-02/ })).not.toBeInTheDocument();
    expect(within(week).getByRole("gridcell", { name: /2026-11-22/ })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "月视图" }));
    expect(screen.getByRole("grid", { name: "内容日历" })).toHaveTextContent("2026-11-02");
  });

  it("manages hashtags, AI captions, drafts, and blocks scheduling on manual platforms", async () => {
    const user = userEvent.setup();
    const generate = vi.fn().mockResolvedValue("工厂直供 IP65 工作灯，支持 OEM。");
    const saveDraft = vi.fn().mockResolvedValue(undefined);
    const schedule = vi.fn().mockResolvedValue(undefined);
    render(<SocialPostEditor accounts={accounts} skus={[{ sku: "LED-1", label: "工作灯" }]} onGenerateCaption={generate} onSaveDraft={saveDraft} onSchedule={schedule} />);
    expect(screen.getByRole("button", { name: "定时发布" })).toBeDisabled();
    for (const tag of ["lighting", "#Lighting", "led"]) {
      await user.type(screen.getByLabelText("Hashtag"), tag + "{enter}");
    }
    expect(screen.getAllByRole("button", { name: /移除标签/ })).toHaveLength(2);
    await user.selectOptions(screen.getByLabelText("关联 SKU"), "LED-1");
    await user.click(screen.getByRole("checkbox", { name: "Instagram" }));
    await user.type(screen.getByLabelText("正文"), "工厂直供 LED 工作灯");
    await user.click(screen.getByRole("button", { name: "AI 生成文案" }));
    expect(generate).toHaveBeenCalledWith({ platform: "instagram", sku_ref: "LED-1", prompt: "工厂直供 LED 工作灯" });
    expect(await screen.findByDisplayValue("工厂直供 IP65 工作灯，支持 OEM。")).toBeVisible();
    await user.type(screen.getByLabelText("贴文标题"), "OEM 工作灯");
    await user.click(screen.getByRole("button", { name: "保存草稿" }));
    expect(saveDraft).toHaveBeenCalledWith([{ platform: "instagram", title: "OEM 工作灯", caption: "工厂直供 IP65 工作灯，支持 OEM。", hashtags: ["#lighting", "#led"], sku_ref: "LED-1", scheduled_at: null }]);
    await user.click(screen.getByRole("checkbox", { name: "小红书" }));
    expect(screen.getByText(/需在官方创作中心人工确认发布/)).toBeVisible();
    expect(screen.getByRole("button", { name: "定时发布" })).toBeDisabled();
    await user.click(screen.getByRole("checkbox", { name: "小红书" }));
    fireEvent.change(screen.getByLabelText("定时发布时间"), { target: { value: "2026-11-25T10:30" } });
    await user.click(screen.getByRole("button", { name: "定时发布" }));
    expect(schedule).toHaveBeenCalledWith([expect.objectContaining({ platform: "instagram", scheduled_at: new Date("2026-11-25T10:30").toISOString() })]);
  });

  it("filters mentions by sentiment and converts only unconverted intent leads", async () => {
    const user = userEvent.setup();
    const convert = vi.fn();
    render(<SocialMentionsFeed mentions={mentions} onConvertToLead={convert} />);
    expect(within(screen.getByLabelText("情感筛选")).getByRole("option", { name: "正面" })).toBeInTheDocument();
    expect(screen.queryByText("lead_intent")).not.toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("情感筛选"), "lead_intent");
    expect(screen.getByText("Where can I buy in bulk for my shop?")).toBeVisible();
    expect(screen.queryByText("Great lamp!")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "转线索 @bulk_buyer" }));
    expect(convert).toHaveBeenCalledWith("m2");
    await user.selectOptions(screen.getByLabelText("情感筛选"), "negative");
    expect(screen.getByText("已转线索")).toBeVisible();
    expect(screen.queryByRole("button", { name: /转线索/ })).not.toBeInTheDocument();
  });
});
