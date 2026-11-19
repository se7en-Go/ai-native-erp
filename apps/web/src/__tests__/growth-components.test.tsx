import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { GlobalProspectMap } from "@/components/growth/global-prospect-map";
import { LeadCollectorPanel } from "@/components/growth/lead-collector-panel";
import { ProspectCleaningTable } from "@/components/growth/prospect-cleaning-table";
import { CampaignBuilderPanel } from "@/components/growth/campaign-builder-panel";
import type { Prospect } from "@/components/growth/types";

const prospects: Prospect[] = [
  { id: "berlin", company_name: "Berlin Light <img src=x>", country: "DE", city: "Berlin", latitude: 52.5, longitude: 13.4, grade: "A", rating: "4.80", sync_status: "cleaned", whatsapp_reachable: true },
  { id: "accra", company_name: "Accra Supply", country: "GH", city: "Accra", latitude: 5.6, longitude: -0.2, grade: "B", rating: null, sync_status: "raw", whatsapp_reachable: false },
  { id: "unknown", company_name: "Unknown Location", country: "JP", latitude: null, longitude: null, grade: "C", rating: "0.00", sync_status: "discarded", whatsapp_reachable: null },
  { id: "invalid", company_name: "Invalid Location", latitude: Infinity, longitude: 0, grade: "C", rating: null, sync_status: "synced_to_crm", whatsapp_reachable: null },
];

describe("growth components", () => {
  it("mounts real Leaflet markers without tiles, shows safe popup text, and highlights selection", async () => {
    const select = vi.fn();
    const { container, rerender } = render(<GlobalProspectMap prospects={prospects} tiles={null} onSelect={select} />);
    const berlin = await screen.findByRole("button", { name: prospects[0].company_name });
    expect(screen.getByText(/底图不可用/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Accra Supply" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Unknown Location|Invalid Location/ })).not.toBeInTheDocument();
    fireEvent.click(berlin);
    expect(select).toHaveBeenCalledWith("berlin");
    expect(berlin).toHaveAttribute("aria-pressed", "true");
    expect(container.querySelector(".leaflet-popup-content")).toHaveTextContent(prospects[0].company_name);
    expect(container.querySelector(".leaflet-popup-content img")).toBeNull();
    rerender(<GlobalProspectMap prospects={prospects} tiles={null} onSelect={select} selectedId="accra" />);
    expect(screen.getByRole("button", { name: "Accra Supply" })).toHaveAttribute("aria-pressed", "true");
    expect(berlin).toHaveAttribute("aria-pressed", "false");
  });

  it("keeps Leaflet usable when the configured tile source fails at runtime", async () => {
    const { container } = render(<GlobalProspectMap prospects={prospects} tiles={{ url: "/tiles/{z}/{x}/{y}.png", attribution: "Test provider" }} />);
    await screen.findByRole("button", { name: "Accra Supply" });
    const tile = container.querySelector("img.leaflet-tile");
    expect(tile).not.toBeNull();
    fireEvent.error(tile!);
    expect(screen.getByText(/底图不可用/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Accra Supply" }));
    expect(container.querySelector(".leaflet-popup-content")).toHaveTextContent("Accra Supply");
  });

  it("shows the source guidance and submits normalized collection options", async () => {
    const user = userEvent.setup();
    const start = vi.fn();
    render(<LeadCollectorPanel metrics={{ collected: 322, cleaned: 204, synced: 18, jobs: 4 }} onStart={start} />);
    expect(screen.getByText("322")).toBeVisible();
    await user.selectOptions(screen.getByLabelText("数据源"), "google_places");
    expect(screen.getByText(/自带密钥/)).toBeVisible();
    await user.type(screen.getByLabelText("关键词"), " lighting {enter}{enter} distributor ");
    await user.type(screen.getByLabelText("目标城市 / 国家"), "Berlin, DE");
    await user.clear(screen.getByLabelText("最大采集数"));
    await user.type(screen.getByLabelText("最大采集数"), "15");
    await user.type(screen.getByLabelText("最低评分"), "4.25");
    await user.click(screen.getByRole("switch", { name: "必须含电话" }));
    await user.click(screen.getByRole("button", { name: "启动采集" }));
    expect(start).toHaveBeenCalledWith({ source_provider: "google_places", keywords: ["lighting", "distributor"], target_region: "Berlin, DE", max_results: 15, min_rating: "4.25", require_phone: true });
  });

  it("requires an actual file for CSV import and passes it to the owner", async () => {
    const user = userEvent.setup();
    const start = vi.fn();
    render(<LeadCollectorPanel metrics={{ collected: 0, cleaned: 0, synced: 0, jobs: 0 }} onStart={start} />);
    await user.selectOptions(screen.getByLabelText("数据源"), "csv_import");
    expect(screen.getByRole("button", { name: "启动采集" })).toBeDisabled();
    const file = new File(["company_name\nClinic"], "leads.csv", { type: "text/csv" });
    await user.upload(screen.getByLabelText("CSV 文件"), file);
    await user.click(screen.getByRole("button", { name: "启动采集" }));
    expect(start).toHaveBeenCalledWith(expect.objectContaining({ source_provider: "csv_import", file }));
  });

  it("filters and delegates export and bulk operations for the displayed scope only", async () => {
    const user = userEvent.setup();
    const clean = vi.fn(), sync = vi.fn(), exportCsv = vi.fn(), select = vi.fn();
    render(<ProspectCleaningTable prospects={prospects} onClean={clean} onSync={sync} onExportCsv={exportCsv} onSelect={select} />);
    expect(screen.getByText("4.80")).toBeVisible();
    expect(screen.getByText("WhatsApp 可达")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "批量去重清洗" }));
    expect(clean).toHaveBeenCalledWith(["accra"]);
    await user.selectOptions(screen.getByLabelText("评级筛选"), "A");
    await user.selectOptions(screen.getByLabelText("状态筛选"), "raw");
    expect(screen.getByText("没有符合条件的线索。")).toBeVisible();
    await user.selectOptions(screen.getByLabelText("状态筛选"), "cleaned");
    await user.click(screen.getByRole("button", { name: /^Berlin Light/ }));
    expect(select).toHaveBeenCalledWith("berlin");
    await user.click(screen.getByRole("button", { name: "导出 CSV" }));
    await user.click(screen.getByRole("button", { name: "一键同步到 ERP 线索池" }));
    expect(exportCsv).toHaveBeenCalledWith(["berlin"]);
    expect(sync).toHaveBeenCalledWith(["berlin"]);
  });

  it("previews backend suppression and clears the result on channel changes", async () => {
    const user = userEvent.setup();
    const preview = vi.fn().mockResolvedValue({ eligible_count: 1, suppressed_count: 2, reasons: [{ reason: "已退订", count: 2 }] });
    const save = vi.fn();
    render(<CampaignBuilderPanel targetIds={["berlin", "accra", "unknown"]} onPreview={preview} onSaveDraft={save} />);
    expect(screen.getByText(/RFC 8058/)).toBeVisible();
    await user.type(screen.getByLabelText("战役名称"), "经销商新品");
    await user.type(screen.getByLabelText("邮件主题"), "新品邀请");
    await user.type(screen.getByLabelText("消息内容"), "新品介绍");
    await user.click(screen.getByRole("button", { name: "预览退订拦截" }));
    expect(preview).toHaveBeenCalledWith({ name: "经销商新品", channel_type: "email", subject: "新品邀请", body_template: "新品介绍", target_ids: ["berlin", "accra", "unknown"] });
    expect(await screen.findByText("已退订")).toBeVisible();
    expect(within(screen.getByRole("region", { name: "拦截结果" })).getByText("2")).toBeVisible();
    await user.selectOptions(screen.getByLabelText("战役渠道"), "whatsapp_cloud");
    expect(screen.queryByRole("region", { name: "拦截结果" })).not.toBeInTheDocument();
    expect(screen.getByText(/Opt-In/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "保存草稿" }));
    expect(save).toHaveBeenCalledWith(expect.objectContaining({ channel_type: "whatsapp_cloud", subject: null }));
  });
});
