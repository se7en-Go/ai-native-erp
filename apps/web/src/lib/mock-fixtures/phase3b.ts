/**
 * Phase 3B mock fixtures. Strictly UI fixtures for `NEXT_PUBLIC_USE_MOCK_API=true`;
 * the live path consumes the generated `@hey-api/openapi-ts` SDK instead.
 *
 * Values mirror what the real backend derives from `ai_proposals` / `usage_ledger`
 * so the mocked screens show the same shapes (Decimal cost stays a string).
 */
import type {
  AiEmployeeRead,
  CreativeAssetRead,
  QuickReplyRead,
  WorkforceMatrixResponse,
} from "@/lib/api-client/types.gen";

const NOW = "2026-11-19T09:00:00Z";
const TENANT = "22222222-2222-2222-2222-222222222222";
const OWNER = "11111111-1111-1111-1111-111111111111";

function employee(
  id: string,
  code: string,
  displayName: string,
  roleTitle: string,
  department: string,
  avatar: string,
  description: string,
  capabilityCodes: AiEmployeeRead["capability_codes"],
  autonomyLevel: AiEmployeeRead["autonomy_level"],
  toolCount: number,
  activity: { actions_7d: number; tasks_7d: number },
  isEnabled = true
): AiEmployeeRead {
  return {
    id,
    tenant_id: TENANT,
    code,
    display_name: displayName,
    role_title: roleTitle,
    department,
    avatar_emoji: avatar,
    description,
    capability_codes: capabilityCodes,
    autonomy_level: autonomyLevel,
    is_enabled: isEnabled,
    tool_count: toolCount,
    system_persona: null,
    created_at: NOW,
    updated_at: NOW,
    ...activity,
  };
}

export const mockPhase3bEmployees: AiEmployeeRead[] = [
  employee("emp-b2b-sales", "b2b-sales", "小光", "B2B 外贸业务员", "商务中心", "💡",
    "识别 B2B 询盘需求，整理 RFQ 明细并起草报价草案，价格一律取实时 ERP 数据。",
    ["sales_quoting", "cs_support"], "L1_proposal", 6, { actions_7d: 18, tasks_7d: 2 }),
  employee("emp-b2b-order-desk", "b2b-order-desk", "小单", "B端跟单专员", "履约中心", "📋",
    "跟进 B 端订单进度、生产交期与样品确认，异常自动升级给人工。",
    ["sales_quoting", "order_logistics"], "L1_proposal", 5, { actions_7d: 11, tasks_7d: 1 }),
  employee("emp-logistics", "logistics-forwarder", "小海", "物流出船专员", "履约中心", "🚢",
    "查询 17TRACK 实时节点、整理出船文件，货件停滞 72 小时自动预警。",
    ["order_logistics"], "L2_auto", 4, { actions_7d: 64, tasks_7d: 0 }),
  employee("emp-growth", "growth-scout", "小拓", "获客专员", "增长中心", "🧭",
    "整理合规线索池、清洗重复买家并生成首触达话术，禁止越权抓取。",
    ["sales_quoting", "cs_support"], "L1_proposal", 4, { actions_7d: 7, tasks_7d: 3 }),
  employee("emp-b2c-cs", "b2c-cs", "小暖", "B2C 贴心客服", "客户成功", "🌷",
    "回答零售买家售前售后问题，引用知识库与订单事实，退款一律转人工。",
    ["cs_support"], "L2_auto", 5, { actions_7d: 132, tasks_7d: 0 }),
  employee("emp-finance", "finance-assistant", "小账", "财务助理", "财务中心", "📒",
    "按金额匹配应收与银行水单候选，核销动作永远由人工确认。",
    ["finance_recon"], "L0_copilot", 3, { actions_7d: 5, tasks_7d: 1 }),
  employee("emp-supply", "supply-planner", "小供", "供应链专员", "供应链中心", "📦",
    "跟踪安全库存、采购在途与入库进度，输出补货建议。",
    ["biz_analytics", "order_logistics"], "L1_proposal", 4, { actions_7d: 9, tasks_7d: 1 }),
  employee("emp-analyst", "biz-analyst", "小析", "经营分析师", "经营分析", "📈",
    "基于只读 ERP 报表输出毛利、渠道结构与库存周转分析。",
    ["biz_analytics"], "L2_auto", 3, { actions_7d: 26, tasks_7d: 0 }),
  employee("emp-content", "content-studio", "小文", "文案与社媒专员", "品牌营销", "✍️",
    "为真实 Catalog SKU 生成多语言商品文案、Listing 与社媒配文草稿。",
    ["content_studio"], "L1_proposal", 3, { actions_7d: 14, tasks_7d: 2, },
    false),
];

export const mockPhase3bMatrix: WorkforceMatrixResponse = {
  kpis: {
    total_employees: mockPhase3bEmployees.length,
    active_employees: mockPhase3bEmployees.filter((row) => row.is_enabled).length,
    actions_7d: 286,
    today_llm_cost_usd: "0.184500",
    autonomy_summary: { l2_auto: 3, l1_proposal: 5, l0_copilot: 1 },
  },
  l2_auto: [
    { capability: "cs_support", display_name: "Customer support", auto_executed_7d: 132,
      href: "/ai/proposals" },
    { capability: "order_logistics", display_name: "Order logistics", auto_executed_7d: 64,
      href: "/ai/proposals" },
    { capability: "biz_analytics", display_name: "Business analytics", auto_executed_7d: 26,
      href: "/ai/proposals" },
  ],
  l1_pending: [
    { proposal_type: "quote_draft", label: "报价草稿", count: 2, href: "/ai/proposals" },
    { proposal_type: "reply_draft", label: "回复草稿", count: 4, href: "/ai/proposals" },
    { proposal_type: "follow_up_suggestion", label: "跟进建议", count: 1, href: "/ai/proposals" },
  ],
  l0_locked: [
    { code: "send_formal_quote_or_email", title: "发送正式报价与对外邮件",
      description: "正式报价、PI 与对外邮件必须由人工确认后发送。", href: "/ai/capabilities" },
    { code: "finance_receipt_settlement", title: "财务收付与核销",
      description: "收款匹配、银行水单核销与结算仅允许人工执行。", href: "/ai/capabilities" },
    { code: "refund_and_high_discount", title: "退款与高额折扣",
      description: "退款、补发与超额折扣必须人工审批，AI 只读不写。", href: "/ai/capabilities" },
  ],
};

export const mockPhase3bQuickReplies: QuickReplyRead[] = [
  { id: "qr-1", tenant_id: TENANT, title: "MOQ 起订量回复", category: "报价", language: "zh",
    body_text: "您好，该型号起订量为 100 条起，500 条起可享阶梯价，具体单价以实时报价单为准。",
    shortcut_code: "moq-zh", created_at: NOW },
  { id: "qr-2", tenant_id: TENANT, title: "MOQ reply", category: "quoting", language: "en",
    body_text: "Our MOQ starts at 100 units, with tiered pricing from 500 units.",
    shortcut_code: "moq-en", created_at: NOW },
  { id: "qr-3", tenant_id: TENANT, title: "交期回复", category: "履约", language: "zh",
    body_text: "常规交期为收到定金后 15-20 天，海运拼箱另计约 25-35 天。",
    shortcut_code: "lead-time-zh", created_at: NOW },
  { id: "qr-4", tenant_id: TENANT, title: "Lead time reply", category: "fulfillment",
    language: "en",
    body_text: "Standard lead time is 15-20 days after deposit; LCL sea freight adds 25-35 days.",
    shortcut_code: "lead-time-en", created_at: NOW },
  { id: "qr-5", tenant_id: TENANT, title: "验厂邀请", category: "信任", language: "zh",
    body_text: "欢迎到泉州工厂实地验厂，我们可安排产线参观与第三方验货对接。",
    shortcut_code: "factory-visit-zh", created_at: NOW },
];

export const mockPhase3bAssets: CreativeAssetRead[] = [
  {
    id: "asset-1",
    tenant_id: TENANT,
    asset_type: "product_copy",
    title: "LED-COB-24V-5M 德语商品文案",
    language: "de",
    sku_ref: "LED-COB-24V-5M",
    prompt: "为德国经销商撰写 5 米 COB 灯带的商品文案。",
    content_text:
      "Flexibles COB-LED-Band, 24 V, 5 m Rolle. Gleichmäßiges Licht ohne sichtbare Punkte.",
    status: "draft",
    created_by: OWNER,
    created_at: NOW,
    updated_at: NOW,
    numeric_guard_passed: false,
    tool_calls: [{ tool_name: "get_live_sku_pricing_and_stock", ok: true }],
  },
  {
    id: "asset-2",
    tenant_id: TENANT,
    asset_type: "listing",
    title: "Amazon Listing bullets",
    language: "en",
    sku_ref: "LED-COB-24V-5M",
    prompt: "Write five marketplace bullet points.",
    content_text:
      "- Seamless COB light line\n- Cuttable at marked segments\n- 24 V DC operation",
    status: "approved",
    created_by: OWNER,
    created_at: NOW,
    updated_at: NOW,
    numeric_guard_passed: true,
    tool_calls: [],
  },
];

/** Deterministic stand-in for the LLM copy generator in mock mode. */
export function mockGeneratedAsset(
  language: string,
  assetType: string,
  skuRef: string | null
): CreativeAssetRead {
  const copy: Record<string, string> = {
    en: `Seamless COB light line for ${skuRef ?? "your next installation"}. Durable, cuttable and factory-direct from Quanzhou.`,
    zh: `${skuRef ?? "该型号"}采用无光斑 COB 灯带，可裁剪分段安装，泉州工厂直供。`,
    de: `Gleichmäßiges COB-Lichtband für ${skuRef ?? "Ihre Installation"}, teilbar und direkt ab Werk Quanzhou.`,
  };
  return {
    id: `asset-gen-${language}-${assetType}`,
    tenant_id: TENANT,
    asset_type: assetType as CreativeAssetRead["asset_type"],
    title: `${assetType} · ${skuRef ?? language}`,
    language,
    sku_ref: skuRef,
    prompt: null,
    content_text: copy[language] ?? copy.en,
    status: "draft",
    created_by: OWNER,
    created_at: NOW,
    updated_at: NOW,
    // Generated prose is never presented as tool-verified in mock mode.
    numeric_guard_passed: false,
    tool_calls: skuRef ? [{ tool_name: "get_live_sku_pricing_and_stock", ok: true }] : [],
  };
}
