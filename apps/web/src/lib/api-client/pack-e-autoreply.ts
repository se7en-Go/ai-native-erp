// Pack E（AI 查库自动回复 + AI 客户画像）专用 API 模块。
//
// 契约（`contracts/openapi.json`）已经冻结：
//   GET    /v1/ai/conversations/{id}/auto-reply             -> AutoReplySettingRead
//   PATCH  /v1/ai/conversations/{id}/auto-reply             -> AutoReplySettingRead
//   POST   /v1/ai/conversations/{id}/auto-reply/run         -> AutoReplyRunResponse
//   POST   /v1/ai/conversations/{id}/extract-profile        -> CustomerAiProfileRead
//   GET    /v1/ai/customers/{id}/profile                    -> CustomerAiProfileRead
//
// `types.gen.ts` 由后端统一重新生成，前端不得手改，因此这里只做两件事：
//   1. 用现成的 `bffFetch`（/api/bff -> FastAPI /api/v1/*，带 Cookie 与 401 刷新）
//      调用上面 5 个端点，并把响应安全归一化（字段可能缺省 / 数值可能是字符串）；
//   2. `NEXT_PUBLIC_USE_MOCK_API=true` 时提供完整可用的本地回退，保证零后端也能
//      演示「开关持久化 + 三种自动回复动作 + 画像提炼同步 CRM」。
import { bffFetch } from "@/lib/api";
import { shouldUseMockApi } from "@/lib/data-source";
import type {
  AutoReplyRunResponse,
  AutoReplySettingRead,
  CustomerAiProfileRead,
} from "@/lib/api-client/types.gen";

export type AutoReplyMode = "auto" | "proposal_only";
export type AutoReplyAction = AutoReplyRunResponse["action"];
export type AutoReplyRun = AutoReplyRunResponse;
export type AutoReplySetting = AutoReplySettingRead;
export type CustomerAiProfile = CustomerAiProfileRead;
export type BuyerType = NonNullable<CustomerAiProfileRead["buyer_type"]>;
export type PriceSensitivity = NonNullable<CustomerAiProfileRead["price_sensitivity"]>;
export type AutoReplyActionTone = "success" | "warning" | "neutral";

// ---------------------------------------------------------------------------
// 展示用文案（页面 / 抽屉 / 单测共用，避免三处各写一份）
// ---------------------------------------------------------------------------

const ACTION_LABELS: Record<AutoReplyAction, { zh: string; en: string }> = {
  auto_replied: {
    zh: "已自动回复客户",
    en: "Auto-replied to the buyer",
  },
  proposal_created: {
    zh: "触发安全红线已转 L1 提案",
    en: "Safety red line hit — escalated as an L1 proposal",
  },
  abstained: {
    zh: "信息不足已安全拒答",
    en: "Not enough grounded info — safely abstained",
  },
};

/** 「AI 自动回复」执行结果的一句话动作结论。 */
export function autoReplyActionLabel(
  action: AutoReplyAction,
  locale: "zh" | "en" = "zh",
): string {
  const label = ACTION_LABELS[action] ?? ACTION_LABELS.abstained;
  return locale === "en" ? label.en : label.zh;
}

/** 结果条的视觉语气：成功 / 需人工 / 中性。 */
export function autoReplyActionTone(action: AutoReplyAction): AutoReplyActionTone {
  if (action === "auto_replied") return "success";
  if (action === "proposal_created") return "warning";
  return "neutral";
}

const BUYER_TYPE_LABELS: Record<BuyerType, { zh: string; en: string }> = {
  b2b_buyer: { zh: "B2B 终端买家", en: "B2B buyer" },
  b2b_distributor: { zh: "B2B 渠道 / 批发商", en: "B2B distributor" },
  b2c_consumer: { zh: "B2C 零售消费者", en: "B2C consumer" },
  unknown: { zh: "待识别", en: "Unknown" },
};

const PRICE_SENSITIVITY_LABELS: Record<PriceSensitivity, { zh: string; en: string }> = {
  low: { zh: "不敏感（重品质/交期）", en: "Low (quality & lead time first)" },
  medium: { zh: "中等（会比较阶梯价）", en: "Medium (compares tier prices)" },
  high: { zh: "高（强烈压价）", en: "High (pushes hard on price)" },
};

export function buyerTypeLabel(
  buyerType: BuyerType | null | undefined,
  locale: "zh" | "en" = "zh",
): string {
  const label = BUYER_TYPE_LABELS[buyerType ?? "unknown"] ?? BUYER_TYPE_LABELS.unknown;
  return locale === "en" ? label.en : label.zh;
}

export function priceSensitivityLabel(
  sensitivity: PriceSensitivity | null | undefined,
  locale: "zh" | "en" = "zh",
): string {
  const label =
    PRICE_SENSITIVITY_LABELS[sensitivity ?? "low"] ?? PRICE_SENSITIVITY_LABELS.low;
  return locale === "en" ? label.en : label.zh;
}

/** 采购意向分恒定裁剪到 0–100（后端理论上已保证，前端仍兜底）。 */
export function clampIntentScore(value: number | null | undefined): number {
  if (typeof value !== "number" || !Number.isFinite(value)) return 0;
  return Math.min(100, Math.max(0, Math.round(value)));
}

/** Decimal 置信度（"0.8600" / 0.86）统一显示成整数百分比。 */
export function formatConfidencePercent(
  confidence: string | number | null | undefined,
): string {
  const num = typeof confidence === "number" ? confidence : Number(confidence ?? 0);
  if (!Number.isFinite(num) || num < 0) return "0%";
  return `${Math.round(Math.min(1, num) * 100)}%`;
}

// ---------------------------------------------------------------------------
// 归一化
// ---------------------------------------------------------------------------

function toStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string" && item.length > 0);
}

function toOptionalCount(value: unknown): number {
  return Array.isArray(value) ? value.length : 0;
}

/** 后端响应可能是部分字段 / 空对象（BFF 兜底），统一收敛成契约形状。 */
export function normalizeAutoReplySetting(
  raw: Partial<AutoReplySettingRead> | null | undefined,
  conversationId: string,
): AutoReplySettingRead {
  return {
    conversation_id:
      typeof raw?.conversation_id === "string" && raw.conversation_id
        ? raw.conversation_id
        : conversationId,
    ai_auto_reply_enabled: raw?.ai_auto_reply_enabled === true,
  };
}

export function normalizeAutoReplyRun(
  raw: (Partial<AutoReplyRunResponse> & Record<string, unknown>) | null | undefined,
  conversationId: string,
): AutoReplyRunResponse {
  const action: AutoReplyAction =
    raw?.action === "auto_replied" || raw?.action === "proposal_created"
      ? raw.action
      : "abstained";
  const confidence =
    typeof raw?.confidence === "string" || typeof raw?.confidence === "number"
      ? String(raw.confidence)
      : "0";
  return {
    action,
    confidence,
    conversation_id:
      typeof raw?.conversation_id === "string" && raw.conversation_id
        ? raw.conversation_id
        : conversationId,
    message_id: typeof raw?.message_id === "string" ? raw.message_id : null,
    proposal_id: typeof raw?.proposal_id === "string" ? raw.proposal_id : null,
    reply_text: typeof raw?.reply_text === "string" ? raw.reply_text : null,
    risk_flags: toStringArray(raw?.risk_flags),
    kb_hits: Array.isArray(raw?.kb_hits) ? raw.kb_hits : [],
    tool_evidence: Array.isArray(raw?.tool_evidence) ? raw.tool_evidence : [],
  };
}

export function normalizeCustomerAiProfile(
  raw: (Partial<CustomerAiProfileRead> & Record<string, unknown>) | null | undefined,
  fallbackCustomerId: string,
): CustomerAiProfile {
  const buyerType = raw?.buyer_type;
  const priceSensitivity = raw?.price_sensitivity;
  return {
    customer_id:
      typeof raw?.customer_id === "string" && raw.customer_id
        ? raw.customer_id
        : fallbackCustomerId,
    buyer_type:
      buyerType === "b2b_buyer" ||
      buyerType === "b2b_distributor" ||
      buyerType === "b2c_consumer"
        ? buyerType
        : "unknown",
    company_name: typeof raw?.company_name === "string" ? raw.company_name : null,
    country_code: typeof raw?.country_code === "string" ? raw.country_code : null,
    preferred_language:
      typeof raw?.preferred_language === "string" && raw.preferred_language
        ? raw.preferred_language
        : "en",
    interested_skus: toStringArray(raw?.interested_skus),
    estimated_order_value_usd:
      typeof raw?.estimated_order_value_usd === "string"
        ? raw.estimated_order_value_usd
        : typeof raw?.estimated_order_value_usd === "number"
          ? String(raw.estimated_order_value_usd)
          : null,
    target_incoterm: typeof raw?.target_incoterm === "string" ? raw.target_incoterm : null,
    purchase_intent_score: clampIntentScore(raw?.purchase_intent_score as number | undefined),
    price_sensitivity:
      priceSensitivity === "medium" || priceSensitivity === "high" ? priceSensitivity : "low",
    key_requirements: toStringArray(raw?.key_requirements),
    pain_points: toStringArray(raw?.pain_points),
    next_best_action:
      typeof raw?.next_best_action === "string" && raw.next_best_action
        ? raw.next_best_action
        : "先补齐买家采购意向，再生成有 ERP 证据的回复。",
    ai_summary:
      typeof raw?.ai_summary === "string" && raw.ai_summary
        ? raw.ai_summary
        : "暂无 AI 摘要。",
    tags: toStringArray(raw?.tags),
    updated_at:
      typeof raw?.updated_at === "string" && raw.updated_at
        ? raw.updated_at
        : new Date().toISOString(),
    synced_lead_id: typeof raw?.synced_lead_id === "string" ? raw.synced_lead_id : null,
    synced_followup_id:
      typeof raw?.synced_followup_id === "string" ? raw.synced_followup_id : null,
  };
}

// ---------------------------------------------------------------------------
// Mock 状态（仅 mock 模式使用，进程内保存以便开关 / 画像跨组件一致）
// ---------------------------------------------------------------------------

const mockAutoReplyEnabled = new Map<string, boolean>();
const mockProfiles = new Map<string, CustomerAiProfile>();
const mockRunCounts = new Map<string, number>();

/** mock 模式下新建会话默认开启自动回复（演示时先看到「开」更有信息量）。 */
const MOCK_AUTO_REPLY_DEFAULT = true;

/** 三种动作按执行次序轮转，一个会话连点三次即可演示全部结果态。 */
const MOCK_RUN_ACTIONS: AutoReplyAction[] = [
  "auto_replied",
  "proposal_created",
  "abstained",
];

function mockCustomerIdForConversation(conversationId: string): string {
  return conversationId.startsWith("conv-")
    ? `cust-${conversationId.slice("conv-".length)}`
    : `cust-${conversationId}`;
}

function buildMockProfile(customerId: string): CustomerAiProfile {
  return {
    customer_id: customerId,
    buyer_type: "b2b_distributor",
    company_name: "Berlin Lighting GmbH",
    country_code: "DE",
    preferred_language: "de",
    interested_skus: ["LED-COB-24V-5M", "LED-STRIP-5050-IP65", "LED-DRIVER-100W"],
    estimated_order_value_usd: "18400.00",
    target_incoterm: "FOB",
    purchase_intent_score: 82,
    price_sensitivity: "medium",
    key_requirements: [
      "CE / RoHS 认证文件齐全",
      "30 天内到汉堡港",
      "外箱需英文唛头与防潮包装",
    ],
    pain_points: ["上批货色差被终端客户投诉", "上次样品周期 21 天影响上架节奏"],
    next_best_action: "今天内发 FOB 阶梯价 PI，并附 CE 证书与 7 天交期承诺。",
    ai_summary:
      "德国 LED 批发商，正在比价 24V COB 灯带，关注认证、交期与色差一致性；价格中等敏感。",
    tags: ["高意向", "欧洲批发"],
    updated_at: "2026-11-21T02:00:00.000Z",
    synced_lead_id: null,
    synced_followup_id: null,
  };
}

function buildMockRun(conversationId: string, mode: AutoReplyMode): AutoReplyRun {
  const runs = (mockRunCounts.get(conversationId) ?? 0) + 1;
  mockRunCounts.set(conversationId, runs);
  const action: AutoReplyAction =
    mode === "proposal_only"
      ? "proposal_created"
      : MOCK_RUN_ACTIONS[(runs - 1) % MOCK_RUN_ACTIONS.length];

  const kbHits =
    action === "auto_replied"
      ? [
          { chunk_id: "kb-chunk-1", title: "LED-COB 24V 规格与认证", score: "0.91" },
          { chunk_id: "kb-chunk-2", title: "FOB 深圳出货与包装标准", score: "0.84" },
        ]
      : action === "proposal_created"
        ? [{ chunk_id: "kb-chunk-7", title: "退款与赔付红线政策", score: "0.88" }]
        : [];

  const toolEvidence =
    action === "auto_replied"
      ? [
          {
            tool_name: "check_sku_stock",
            source_tool_call_id: "sql-sku-901",
            sku: "LED-COB-24V-5M",
            available_qty: 1240,
          },
          {
            tool_name: "get_moq_tier_price",
            source_tool_call_id: "sql-price-902",
            sku: "LED-COB-24V-5M",
            unit_price_usd: "14.20",
            moq_tier: 500,
          },
        ]
      : action === "proposal_created"
        ? [
            {
              tool_name: "get_order_status",
              source_tool_call_id: "sql-order-903",
              order_no: "ORD-2026-0311",
            },
          ]
        : [];

  return {
    action,
    confidence: action === "auto_replied" ? "0.86" : action === "proposal_created" ? "0.62" : "0.21",
    conversation_id: conversationId,
    message_id: action === "auto_replied" ? `mock-msg-${conversationId}` : null,
    proposal_id: action === "proposal_created" ? `mock-proposal-${conversationId}` : null,
    reply_text:
      action === "auto_replied"
        ? "500 pcs LED-COB-24V-5M 现货 1,240 pcs，FOB 深圳 $14.20/pc，交期 7 天，CE/RoHS 齐全。"
        : action === "proposal_created"
          ? "该会话涉及退款/赔付诉求，已为您生成 L1 提案，请人工确认后再发送。"
          : null,
    risk_flags: action === "proposal_created" ? ["refund"] : [],
    kb_hits: kbHits,
    tool_evidence: toolEvidence,
  };
}

// ---------------------------------------------------------------------------
// 读操作
// ---------------------------------------------------------------------------

/** GET /v1/ai/conversations/{id}/auto-reply —— 当前会话的自动回复开关。 */
export async function fetchAutoReplySetting(
  conversationId: string,
): Promise<AutoReplySettingRead> {
  if (!conversationId) {
    return { conversation_id: "", ai_auto_reply_enabled: false };
  }
  if (shouldUseMockApi()) {
    return {
      conversation_id: conversationId,
      ai_auto_reply_enabled:
        mockAutoReplyEnabled.get(conversationId) ?? MOCK_AUTO_REPLY_DEFAULT,
    };
  }
  const raw = await bffFetch<Partial<AutoReplySettingRead>>(
    `/v1/ai/conversations/${conversationId}/auto-reply`,
  );
  return normalizeAutoReplySetting(raw, conversationId);
}

/** GET /v1/ai/customers/{id}/profile —— 已落库的 AI 客户画像。 */
export async function fetchCustomerAiProfile(
  customerId: string,
): Promise<CustomerAiProfile> {
  if (!customerId) {
    return normalizeCustomerAiProfile(null, "");
  }
  if (shouldUseMockApi()) {
    const cached = mockProfiles.get(customerId);
    if (cached) return cached;
    const profile = buildMockProfile(customerId);
    mockProfiles.set(customerId, profile);
    return profile;
  }
  const raw = await bffFetch<Partial<CustomerAiProfileRead> & Record<string, unknown>>(
    `/v1/ai/customers/${customerId}/profile`,
  );
  return normalizeCustomerAiProfile(raw, customerId);
}

// ---------------------------------------------------------------------------
// 写操作
// ---------------------------------------------------------------------------

/** PATCH /v1/ai/conversations/{id}/auto-reply —— 开关本会话的 AI 自动回复。 */
export async function updateAutoReplySetting(
  conversationId: string,
  enabled: boolean,
): Promise<AutoReplySettingRead> {
  if (shouldUseMockApi()) {
    mockAutoReplyEnabled.set(conversationId, enabled);
    return { conversation_id: conversationId, ai_auto_reply_enabled: enabled };
  }
  const raw = await bffFetch<Partial<AutoReplySettingRead>>(
    `/v1/ai/conversations/${conversationId}/auto-reply`,
    {
      method: "PATCH",
      body: JSON.stringify({ enabled }),
    },
  );
  const setting = normalizeAutoReplySetting(raw, conversationId);
  // 后端若省略该字段，以请求值兜底，避免开关回弹。
  return typeof (raw as Record<string, unknown> | null)?.ai_auto_reply_enabled === "boolean"
    ? setting
    : { ...setting, ai_auto_reply_enabled: enabled };
}

/**
 * POST /v1/ai/conversations/{id}/auto-reply/run —— 一次「AI 查库自动回复」。
 * 后端会用 RAG 知识库 + 确定性 ERP 工具接地，并跑安全红线护栏；
 * 命中红线的会话只会产出 L1 提案，绝不自动发给客户。
 */
export async function runAiAutoReply(
  conversationId: string,
  mode: AutoReplyMode = "auto",
  maxKbHits = 3,
): Promise<AutoReplyRunResponse> {
  if (shouldUseMockApi()) {
    return buildMockRun(conversationId, mode);
  }
  const raw = await bffFetch<Partial<AutoReplyRunResponse> & Record<string, unknown>>(
    `/v1/ai/conversations/${conversationId}/auto-reply/run`,
    {
      method: "POST",
      body: JSON.stringify({ mode, max_kb_hits: maxKbHits }),
    },
  );
  return normalizeAutoReplyRun(raw, conversationId);
}

/**
 * POST /v1/ai/conversations/{id}/extract-profile —— 从整段沟通里提炼结构化画像；
 * `syncToCrm=true` 时后端会写入 crm_leads + crm_followups 并回传两个 ID。
 */
export async function extractCustomerAiProfile(
  conversationId: string,
  syncToCrm = true,
): Promise<CustomerAiProfile> {
  if (shouldUseMockApi()) {
    const customerId = mockCustomerIdForConversation(conversationId);
    const base = mockProfiles.get(customerId) ?? buildMockProfile(customerId);
    const profile: CustomerAiProfile = {
      ...base,
      updated_at: new Date().toISOString(),
      synced_lead_id: syncToCrm ? `mock-lead-${conversationId}` : null,
      synced_followup_id: syncToCrm ? `mock-followup-${conversationId}` : null,
    };
    mockProfiles.set(customerId, profile);
    return profile;
  }
  const raw = await bffFetch<Partial<CustomerAiProfileRead> & Record<string, unknown>>(
    `/v1/ai/conversations/${conversationId}/extract-profile`,
    {
      method: "POST",
      body: JSON.stringify({ sync_to_crm: syncToCrm }),
    },
  );
  return normalizeCustomerAiProfile(raw, raw?.customer_id ?? conversationId);
}

/** 结果条上展示的两个证据计数（知识库命中 / ERP 工具证据）。 */
export function autoReplyEvidenceCounts(run: AutoReplyRun): {
  kbHits: number;
  toolEvidence: number;
} {
  return {
    kbHits: toOptionalCount(run.kb_hits),
    toolEvidence: toOptionalCount(run.tool_evidence),
  };
}
