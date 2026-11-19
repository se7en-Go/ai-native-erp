// Phase 3B data layer: generated-SDK calls by default, fixture-backed responses
// only when NEXT_PUBLIC_USE_MOCK_API=true (same switch as data-source.ts).
import * as live from "@/lib/api-client/phase3b";
import type {
  CreativeAssetRead,
  CreativeAssetStatus,
  InboxAssistRequest,
  InboxAssistResponse,
  QuickReplyRead,
  WorkforceMatrixResponse,
} from "@/lib/api-client/types.gen";
import type {
  CreativeAsset,
  CreativeAssetRequest,
  DivisionDestination,
  InboxToolbarIntent,
} from "@/components/ai-workforce/types";
import {
  mockGeneratedAsset,
  mockPhase3bAssets,
  mockPhase3bEmployees,
  mockPhase3bMatrix,
  mockPhase3bQuickReplies,
} from "@/lib/mock-fixtures/phase3b";
import { shouldUseMockApi } from "@/lib/data-source";

export type { CreativeAssetRead, WorkforceMatrixResponse };
export { destinationHref } from "@/lib/api-client/phase3b";

// Stateful only in mock mode: mutations land here so the page re-reads what it just wrote.
const mockState = {
  employees: mockPhase3bEmployees.map((row) => ({ ...row })),
  assets: mockPhase3bAssets.map((row) => ({ ...row })),
  quickReplies: [...mockPhase3bQuickReplies],
};

export function fetchWorkforceEmployees(): Promise<live.DigitalEmployeeWithActivity[]> {
  if (shouldUseMockApi()) {
    return Promise.resolve(mockState.employees.map(live.mapEmployee));
  }
  return live.listEmployees();
}

export function setWorkforceEmployeeEnabled(
  employeeId: string,
  isEnabled: boolean
): Promise<live.DigitalEmployeeWithActivity> {
  if (shouldUseMockApi()) {
    const row = mockState.employees.find((employee) => employee.id === employeeId);
    if (!row) return Promise.reject(new Error("Employee not found"));
    row.is_enabled = isEnabled;
    return Promise.resolve(live.mapEmployee(row));
  }
  return live.setEmployeeEnabled(employeeId, isEnabled);
}

export function fetchWorkforceMatrix(): Promise<WorkforceMatrixResponse> {
  if (shouldUseMockApi()) {
    return Promise.resolve(mockPhase3bMatrix);
  }
  return live.fetchWorkforceMatrix();
}

export function fetchQuickReplies(): Promise<QuickReplyRead[]> {
  if (shouldUseMockApi()) {
    return Promise.resolve(mockState.quickReplies);
  }
  return live.listQuickReplies();
}

/**
 * Inbox assistant entry point. `customer_service` is a UI intent that asks for a
 * full reply suggestion, so it maps onto the real `suggest_reply` action.
 */
export function fetchInboxAssist(
  action: InboxToolbarIntent,
  payload: { text?: string; conversationId?: string | null; language?: string } = {}
): Promise<InboxAssistResponse> {
  if (shouldUseMockApi()) {
    return Promise.resolve(mockInboxAssist(action, payload.text ?? ""));
  }
  const body: InboxAssistRequest = {
    action: action === "customer_service" ? "suggest_reply" : action,
    conversation_id: payload.conversationId ?? null,
    text: payload.text ?? null,
    language: payload.language ?? "zh",
  };
  return live.assistInbox(body);
}

function mockInboxAssist(action: InboxToolbarIntent, text: string): InboxAssistResponse {
  const resolved = action === "customer_service" ? "suggest_reply" : action;
  if (resolved === "daily_brief") {
    return {
      action: "daily_brief",
      text: [
        "今日 AI 日报",
        "- 待审提案：7 条（报价草稿 2、回复草稿 4、跟进建议 1）",
        "- 近 7 天已执行：222 条",
        "- 今日 LLM 成本：0.184500 USD",
        "- 待处理会话：5 个",
        "- 库存预警 SKU：2 个",
        "- 履约异常订单：1 单",
      ].join("\n"),
      model_used: "deterministic_sql_brief",
      numeric_guard_passed: true,
      data: { pending_proposals_total: 7, today_llm_cost_usd: "0.184500" },
    };
  }
  if (resolved === "summarize") {
    return {
      action: "summarize",
      text: "• 买家询问 500 条 LED-COB-24V-5M 的 FOB 报价\n• 需要确认交期与包装方式",
      model_used: "mock-model",
      credits_consumed: "0.2500",
      numeric_guard_passed: false,
      requires_human_review: true,
    };
  }
  return {
    action: resolved,
    text:
      resolved === "polish"
        ? `感谢您的来信，我们就您提到的问题回复如下：${text.trim() || "请查收我们的方案。"}`
        : "您好，感谢询价。该型号起订量 100 条起，单价与库存以实时报价单为准，我方可先安排样品确认。",
    model_used: "mock-model",
    credits_consumed: "0.2500",
    numeric_guard_passed: false,
    requires_human_review: true,
  };
}

export function fetchStudioAssets(): Promise<CreativeAssetRead[]> {
  if (shouldUseMockApi()) {
    return Promise.resolve([...mockState.assets]);
  }
  return live.listAssets();
}

export function setStudioAssetStatus(
  assetId: string,
  status: CreativeAssetStatus
): Promise<CreativeAssetRead> {
  if (shouldUseMockApi()) {
    const row = mockState.assets.find((asset) => asset.id === assetId);
    if (!row) return Promise.reject(new Error("Asset not found"));
    row.status = status;
    return Promise.resolve({ ...row });
  }
  return live.updateAsset(assetId, status);
}

/** Save a human-edited asset (never auto-approved). */
export function saveStudioAsset(asset: CreativeAsset): Promise<CreativeAssetRead> {
  if (shouldUseMockApi()) {
    const row: CreativeAssetRead = {
      id: asset.id ?? `asset-saved-${mockState.assets.length + 1}`,
      tenant_id: mockState.assets[0]?.tenant_id ?? "",
      asset_type: asset.asset_type,
      title: asset.title,
      language: asset.language,
      sku_ref: asset.sku_ref,
      prompt: asset.prompt ?? null,
      content_text: asset.content_text,
      status: "draft",
      created_by: "",
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      numeric_guard_passed: false,
      tool_calls: [],
    };
    mockState.assets = [row, ...mockState.assets];
    return Promise.resolve(row);
  }
  return live.createAsset({
    asset_type: asset.asset_type,
    title: asset.title,
    language: asset.language,
    sku_ref: asset.sku_ref,
    prompt: asset.prompt ?? null,
    content_text: asset.content_text,
  });
}

/** One server-side generated asset per requested language (real provider or 503). */
export async function generateStudioAssets(
  request: CreativeAssetRequest
): Promise<CreativeAsset[]> {
  if (shouldUseMockApi()) {
    return request.languages.map((language) => ({
      id: `asset-gen-${language}`,
      asset_type: request.asset_type,
      title: `${request.asset_type} · ${request.sku_ref ?? language}`,
      language,
      sku_ref: request.sku_ref,
      content_text: mockGeneratedAsset(language, request.asset_type, request.sku_ref).content_text,
    }));
  }
  const generated = await Promise.all(
    request.languages.map((language) =>
      live.createAsset({
        asset_type: request.asset_type,
        language,
        sku_ref: request.sku_ref,
        prompt: request.prompt,
        generate: true,
      })
    )
  );
  return generated.map((asset) => ({
    id: asset.id,
    asset_type: asset.asset_type,
    title: asset.title,
    language: asset.language,
    sku_ref: asset.sku_ref,
    prompt: asset.prompt ?? undefined,
    content_text: asset.content_text,
    status: asset.status,
  }));
}

export function isProviderUnavailable(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "code" in error &&
    (error as { code?: unknown }).code === "ERR_AI_PROVIDER_UNAVAILABLE"
  );
}

export type { DivisionDestination };
