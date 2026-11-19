import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { EmployeeCardGrid } from "@/components/ai-workforce/employee-card-grid";
import { AutonomyDivisionBoard } from "@/components/ai-workforce/autonomy-division-board";
import { InboxAiToolbar } from "@/components/ai-workforce/inbox-ai-toolbar";
import { CreativeStudioPanel } from "@/components/ai-workforce/creative-studio-panel";
import type { DigitalEmployee, CreativeAsset } from "@/components/ai-workforce/types";

const employees: DigitalEmployee[] = [
  { id: "e1", display_name: "小光", role_title: "B2B 外贸业务员", department: "商务", avatar_emoji: "💡", description: "协助核对报价草稿", capability_codes: ["sales_quoting"], autonomy_level: "L1_proposal", is_enabled: true, tool_count: 3 },
  { id: "e2", display_name: "小海", role_title: "物流出船专员", department: "履约", description: "追踪货运进度", capability_codes: ["order_logistics"], autonomy_level: "L2_auto", is_enabled: false, tool_count: 2 },
];
const metrics = { totalEmployees: 2, enabledEmployees: 1, actionsLast7Days: 540, llmCostTodayUsd: "9007199254740993.000010", autonomyMode: "L1_proposal" } as const;

describe("AI workforce", () => {
  it("renders all five KPI cards without coercing Decimal USD", () => {
    render(<EmployeeCardGrid employees={employees} metrics={metrics} onToggleEnabled={vi.fn()} />);
    expect(screen.getByRole("region", { name: "数字员工关键指标" })).toHaveTextContent("今日 LLM 成本");
    expect(screen.getByText("9007199254740993.000010 USD")).toBeVisible();
    expect(screen.getByText("540")).toBeVisible();
    expect(screen.getByText("自治模式")).toBeVisible();
    expect(document.body).not.toHaveTextContent("[object Object]");
  });

  it("filters by role, opens employee details, and delegates enable changes", async () => {
    const user = userEvent.setup();
    const toggle = vi.fn();
    render(<EmployeeCardGrid employees={employees} metrics={metrics} onToggleEnabled={toggle} />);
    await user.selectOptions(screen.getByLabelText("角色筛选"), "物流出船专员");
    expect(screen.queryByText("小光")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "查看小海详情" }));
    const drawer = screen.getByRole("dialog", { name: "小海" });
    expect(within(drawer).getByText("追踪货运进度")).toBeVisible();
    await user.click(within(drawer).getByRole("button", { name: "关闭详情" }));
    await user.click(screen.getByRole("switch", { name: "启用小海" }));
    expect(toggle).toHaveBeenCalledWith("e2", true);
    expect(screen.getByRole("switch", { name: "启用小海" })).not.toBeChecked();
  });

  it("keeps all autonomy levels visible and routes to the exact review target", async () => {
    const user = userEvent.setup();
    const open = vi.fn();
    const target = { kind: "proposal", id: "p-1" } as const;
    render(<AutonomyDivisionBoard items={[
      { id: "s", level: "L2_auto", title: "标准答疑", count: 0 },
      { id: "q", level: "L1_proposal", title: "待审报价", count: 2, target },
      { id: "h", level: "L0_copilot", title: "确认收款" },
    ]} onNavigate={open} />);
    expect(screen.getAllByRole("heading", { level: 3 }).map((el) => el.textContent)).toEqual([
      "L2 全接管·AI 自动干", "L1 待确认·AI 提案人审", "L0 人工把关·AI 不碰",
    ]);
    expect(screen.getByText("0")).toBeVisible();
    await user.click(screen.getByRole("button", { name: /待审报价/ }));
    expect(open).toHaveBeenCalledWith(target);
    expect(screen.queryByRole("button", { name: /确认收款/ })).not.toBeInTheDocument();
  });

  it("dispatches the four AI intents separately and inserts a chosen phrase into the draft", async () => {
    const user = userEvent.setup();
    const action = vi.fn();
    const insert = vi.fn();
    const phrase = { id: "r1", title: "收到询价", body_text: "Thanks for your inquiry!", category: "售前", language: "en" };
    render(<InboxAiToolbar phrases={[phrase]} onAction={action} onInsertPhrase={insert} />);
    for (const name of ["AI 客服", "AI 润色", "消息总结", "AI 建议"]) await user.click(screen.getByRole("button", { name }));
    expect(action.mock.calls).toEqual([["customer_service"], ["polish"], ["summarize"], ["suggest_reply"]]);
    await user.click(screen.getByRole("button", { name: "快捷短语" }));
    const popup = screen.getByRole("dialog", { name: "快捷短语" });
    await user.click(within(popup).getByRole("button", { name: /收到询价/ }));
    expect(insert).toHaveBeenCalledWith(phrase);
    expect(popup).not.toBeInTheDocument();
  });

  it("generates for selected languages and sends the chosen editable result to copy and save", async () => {
    const user = userEvent.setup();
    const asset: CreativeAsset = { title: "灯具展示", asset_type: "listing", sku_ref: "LED-1", language: "en", content_text: "LED work light" };
    const generate = vi.fn().mockResolvedValue([asset, { ...asset, language: "es", content_text: "Luz de trabajo LED" }]);
    const save = vi.fn();
    const copy = vi.fn();
    render(<CreativeStudioPanel skus={[{ sku: "LED-1", label: "工作灯" }]} onGenerate={generate} onSave={save} onCopy={copy} />);
    await user.selectOptions(screen.getByLabelText("素材类型"), "listing");
    await user.selectOptions(screen.getByLabelText("关联 SKU"), "LED-1");
    await user.click(screen.getByRole("checkbox", { name: "西班牙语" }));
    await user.type(screen.getByLabelText("创作要求"), "灯具展示");
    await user.click(screen.getByRole("button", { name: "生成文案" }));
    expect(generate).toHaveBeenCalledWith({ asset_type: "listing", sku_ref: "LED-1", languages: ["en", "es"], prompt: "灯具展示" });
    expect(await screen.findByDisplayValue("LED work light")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "西班牙语" }));
    fireEvent.change(screen.getByLabelText("生成结果"), { target: { value: "Luz LED" } });
    await user.click(screen.getByRole("button", { name: "复制" }));
    expect(copy).toHaveBeenCalledWith("Luz LED");
    await user.click(screen.getByRole("button", { name: "保存素材" }));
    expect(save).toHaveBeenCalledWith({ ...asset, language: "es", content_text: "Luz LED" });
  });

  it("shows failed generation without exposing backend error details", async () => {
    const user = userEvent.setup();
    render(<CreativeStudioPanel skus={[]} onGenerate={vi.fn().mockRejectedValue(new Error("internal error details"))} onSave={vi.fn()} />);
    await user.type(screen.getByLabelText("创作要求"), "根据目录生成");
    await user.click(screen.getByRole("button", { name: "生成文案" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("生成失败");
    expect(screen.queryByText("internal error details")).not.toBeInTheDocument();
  });
});
