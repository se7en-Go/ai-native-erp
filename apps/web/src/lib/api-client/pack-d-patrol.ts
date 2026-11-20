// Pack D client module: AI proactive patrol + custom AI employees + 14-platform
// social playbooks.
//
// Every request travels through `bffFetch` (`/api/bff/v1/...`), so the session
// cookie, the 401 refresh flow and the BFF role gate behave exactly like the rest
// of the workbench. No ORM, no SQL, no client-side metric recomputation: the
// health score, the alert counts and the playbook text are all server truth.
//
// Mock mode (`NEXT_PUBLIC_USE_MOCK_API=true`) never reaches the network. The
// fixtures below are strict UI stand-ins that mirror the `--with-demo-data` seed
// so the screens stay reviewable without a backend; they are tagged with the
// real patrol codes / employee codes the API returns.
import { bffFetch } from "@/lib/api";
import { shouldUseMockApi } from "@/lib/data-source";
import type { AiCapability, AutonomyLevel } from "@/lib/contracts";
import type {
  AiEmployeeCreate,
  AiEmployeeRead,
  DailyPatrolBriefRead,
  PaginatedResponseAiEmployeeRead,
  PatrolAlertItem,
  PatrolDispatchRead,
  PatrolFindingItem,
  SocialPlaybookItem,
} from "@/lib/api-client/types.gen";
import { PLATFORM_PLAYBOOKS } from "@/components/social/platform-playbooks";
import type { SocialPlatform } from "@/components/social/types";
import { mockPhase3bEmployees } from "@/lib/mock-fixtures/phase3b";

export type SocialPlaybookRegion = "all" | "overseas" | "domestic";

/** Region filter pills, ordered exactly like the locked 7 + 7 platform split. */
export const SOCIAL_PLAYBOOK_REGIONS: ReadonlyArray<{
  value: SocialPlaybookRegion;
  label_zh: string;
  label_en: string;
}> = [
  { value: "all", label_zh: "全部", label_en: "All" },
  { value: "overseas", label_zh: "海外", label_en: "Overseas" },
  { value: "domestic", label_zh: "国内", label_en: "Domestic" },
];

/**
 * Roster row used by the patrol dispatch board. `AiEmployeeRead` carries the
 * backend `code` the dispatch endpoint needs, while the employee cards render the
 * UI shape, so the two are joined by `id` here.
 */
export interface PatrolRosterEntry {
  id: string;
  code: string;
  display_name: string;
  role_title: string;
  department: string;
  avatar_emoji: string | null;
}

export interface PatrolHealthDescriptor {
  zh: string;
  en: string;
  tone: "emerald" | "amber" | "rose";
}

/** The modal's editable fields for one operator-supplied digital employee. */
export interface CustomEmployeeForm {
  code: string;
  display_name: string;
  role_title: string;
  department: string;
  capability: AiCapability;
  autonomy_level: AutonomyLevel;
  persona_prompt: string;
}

/** Only a non-blank code / name / role / department quartet is submittable. */
export function isCustomEmployeeFormReady(form: CustomEmployeeForm): boolean {
  return [form.code, form.display_name, form.role_title, form.department].every(
    (value) => value.trim().length > 0
  );
}

/**
 * Modal form -> the generated `AiEmployeeCreate` contract, field for field. The
 * grant scope stays exactly the selected capability: nothing is widened here, and
 * `allowed_tools` is left to the server so it can echo the effective grant back.
 */
export function toAiEmployeeCreatePayload(form: CustomEmployeeForm): AiEmployeeCreate {
  const persona = form.persona_prompt.trim();
  return {
    code: form.code.trim(),
    display_name: form.display_name.trim(),
    role_title: form.role_title.trim(),
    department: form.department.trim(),
    description: `由运营在 AI 数字员工中心创建：职责能力 ${form.capability}，自治级别 ${form.autonomy_level}。`,
    capability_codes: [form.capability],
    autonomy_level: form.autonomy_level,
    system_persona: persona ? persona : null,
    is_enabled: true,
  };
}

/** Deterministic health band so the copilot, the roster and the brief agree. */
export function patrolHealthDescriptor(score: number): PatrolHealthDescriptor {
  if (!Number.isFinite(score)) return { zh: "未知", en: "Unknown", tone: "amber" };
  if (score >= 85) return { zh: "运行良好", en: "Healthy", tone: "emerald" };
  if (score >= 70) return { zh: "需关注", en: "Needs attention", tone: "amber" };
  return { zh: "急需介入", en: "Action required", tone: "rose" };
}

// ---------------------------------------------------------------------------
// Live calls
// ---------------------------------------------------------------------------

/** `GET /v1/ai/patrol/daily-brief` — tenant-scoped SQL patrol, never hard-coded. */
export function fetchDailyPatrolBrief(): Promise<DailyPatrolBriefRead> {
  if (shouldUseMockApi()) {
    return Promise.resolve({ ...mockDailyPatrolBrief, alerts: [...mockDailyPatrolBrief.alerts] });
  }
  return bffFetch<DailyPatrolBriefRead>("/v1/ai/patrol/daily-brief");
}

/**
 * `POST /v1/ai/patrol/dispatch/{employee_code}` — runs one registered digital
 * employee over live ERP rows and returns its findings plus the follow-up task.
 */
export function dispatchPatrolEmployee(employeeCode: string): Promise<PatrolDispatchRead> {
  const code = employeeCode.trim();
  if (!code) return Promise.reject(new Error("employeeCode is required"));
  if (shouldUseMockApi()) return Promise.resolve(mockDispatchFor(code));
  return bffFetch<PatrolDispatchRead>(
    `/v1/ai/patrol/dispatch/${encodeURIComponent(code)}`,
    { method: "POST" }
  );
}

/**
 * `POST /v1/ai/employees` — operator-supplied digital employee. The payload is the
 * generated `AiEmployeeCreate` contract verbatim: no extra field is invented and no
 * capability/tool scope is widened client-side.
 */
export function createCustomAiEmployee(payload: AiEmployeeCreate): Promise<AiEmployeeRead> {
  if (shouldUseMockApi()) return Promise.resolve(mockCreatedEmployee(payload));
  return bffFetch<AiEmployeeRead>("/v1/ai/employees", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

/** `GET /v1/social/playbooks?region=...` — bilingual operating cards, 7 + 7. */
export function fetchSocialPlaybooks(
  region: SocialPlaybookRegion = "all"
): Promise<SocialPlaybookItem[]> {
  if (shouldUseMockApi()) {
    return Promise.resolve(
      mockSocialPlaybooks
        .filter((row) => region === "all" || row.region === region)
        .map((row) => ({
          ...row,
          best_practices_zh: [...row.best_practices_zh],
          best_practices_en: [...row.best_practices_en],
        }))
    );
  }
  return bffFetch<SocialPlaybookItem[]>(
    `/v1/social/playbooks?region=${encodeURIComponent(region)}`
  );
}

/** `GET /v1/ai/employees` — only used to resolve the patrol `code` per roster row. */
export function fetchPatrolRoster(): Promise<PatrolRosterEntry[]> {
  if (shouldUseMockApi()) {
    return Promise.resolve(
      mockPhase3bEmployees.map((row) => ({
        id: row.id,
        code: row.code,
        display_name: row.display_name,
        role_title: row.role_title,
        department: row.department,
        avatar_emoji: row.avatar_emoji,
      }))
    );
  }
  return bffFetch<PaginatedResponseAiEmployeeRead>("/v1/ai/employees?limit=100").then((page) =>
    page.items.map((row) => ({
      id: row.id,
      code: row.code,
      display_name: row.display_name,
      role_title: row.role_title,
      department: row.department,
      avatar_emoji: row.avatar_emoji,
    }))
  );
}

/** True when the generated contract marks this region split as 7 + 7. */
export function isOfficialApiPlatform(row: SocialPlaybookItem): boolean {
  return row.official_api_supported;
}

// ---------------------------------------------------------------------------
// Mock fixtures (UI-only, `NEXT_PUBLIC_USE_MOCK_API=true`)
// ---------------------------------------------------------------------------

const MOCK_GENERATED_AT = "2026-11-19T09:20:00.000Z";
const MOCK_TENANT = "22222222-2222-2222-2222-222222222222";

export const mockDailyPatrolBrief: DailyPatrolBriefRead = {
  generated_at: MOCK_GENERATED_AT,
  health_score: 78,
  summary_zh:
    "今日巡检覆盖 6 类经营风险：3 张报价锁汇 72 小时内到期、4 个 SKU 低于安全库存、2 票货件停滞超 72 小时。建议先派 B2B 业务员锁汇，再让供应链专员补货。",
  summary_en:
    "Today's patrol covers six risk classes: 3 quotes lose their locked FX rate within 72 hours, 4 SKUs sit below safety stock and 2 shipments have stalled past 72 hours. Dispatch the B2B rep on FX first, then let the supply planner reorder.",
  metrics: {
    expiring_quotes_count: 3,
    low_stock_skus_count: 4,
    stalled_shipments_72h_count: 2,
    pending_payments_count: 2,
    pending_proposals_count: 5,
    unread_conversations_count: 7,
  },
  alerts: [
    {
      code: "expiring_quotes",
      severity: "high",
      title_zh: "锁汇即将到期的报价",
      title_en: "Quotes with expiring FX locks",
      detail_zh: "3 张草稿/已发送报价的锁汇将在 72 小时内到期。",
      detail_en: "3 draft/sent quotes lose their locked FX rate within 72 hours.",
      employee_code: "b2b-sales",
      employee_name: "小光",
      action_href: "/b2b/quotes",
      count: 3,
    },
    {
      code: "low_stock_skus",
      severity: "high",
      title_zh: "低于安全库存的 SKU",
      title_en: "SKUs below safety stock",
      detail_zh: "4 个 SKU 可用库存低于安全库存，需立即补货。",
      detail_en: "4 SKUs have available quantity below safety stock and need reordering.",
      employee_code: "supply-planner",
      employee_name: "小供",
      action_href: "/wms",
      count: 4,
    },
    {
      code: "stalled_shipments",
      severity: "medium",
      title_zh: "72 小时停滞的货件",
      title_en: "Shipments stalled for 72 hours",
      detail_zh: "2 票在途货件超过 72 小时没有新的物流节点。",
      detail_en: "2 in-transit shipments have had no tracking event for over 72 hours.",
      employee_code: "logistics-forwarder",
      employee_name: "小海",
      action_href: "/orders",
      count: 2,
    },
    {
      code: "pending_payments",
      severity: "high",
      title_zh: "待核销的银行水单",
      title_en: "Bank receipts awaiting settlement",
      detail_zh: "2 张银行水单待人工确认核销。",
      detail_en: "2 bank receipts still need a human-confirmed settlement.",
      employee_code: "finance-assistant",
      employee_name: "小账",
      action_href: "/finance",
      count: 2,
    },
    {
      code: "pending_proposals",
      severity: "medium",
      title_zh: "待审的 AI 提案",
      title_en: "AI proposals waiting for review",
      detail_zh: "5 条 AI 提案仍在 L1 待审队列。",
      detail_en: "5 AI proposals are still queued for L1 review.",
      employee_code: "biz-analyst",
      employee_name: "小析",
      action_href: "/ai/proposals",
      count: 5,
    },
    {
      code: "unread_conversations",
      severity: "medium",
      title_zh: "有未读消息的会话",
      title_en: "Conversations with unread messages",
      detail_zh: "7 个进行中的会话仍有未读消息。",
      detail_en: "7 live conversations still have unread messages.",
      employee_code: "b2c-cs",
      employee_name: "小暖",
      action_href: "/inbox",
      count: 7,
    },
  ],
};

interface MockDispatchSeed {
  employee_name: string;
  employee_role_title: string;
  action_summary_zh: string;
  action_summary_en: string;
  recommended_href: string;
  findings: PatrolFindingItem[];
}

const MOCK_DISPATCH: Record<string, MockDispatchSeed> = {
  "b2b-sales": {
    employee_name: "小光",
    employee_role_title: "B2B 外贸业务员",
    action_summary_zh: "已生成 3 张锁汇到期的跟进任务，客户名单来自真实报价单。",
    action_summary_en: "Created 3 follow-up tasks for expiring FX locks from live quote rows.",
    recommended_href: "/b2b/quotes",
    findings: [
      {
        code: "expiring_quotes",
        title_zh: "锁汇 72 小时内到期",
        title_en: "FX locks expiring within 72 hours",
        detail_zh: "PI-202611-0042、PI-202611-0047 等 3 张报价需要今天就确认汇率。",
        detail_en: "PI-202611-0042, PI-202611-0047 and one more quote need an FX decision today.",
        count: 3,
        action_href: "/b2b/quotes",
        samples: [{ quote_no: "PI-202611-0042", total_amount: "7100.00", currency: "USD" }],
      },
    ],
  },
  "supply-planner": {
    employee_name: "小供",
    employee_role_title: "供应链专员",
    action_summary_zh: "已按安全库存差额生成 4 行补货建议。",
    action_summary_en: "Generated 4 reorder suggestions from the safety-stock gap.",
    recommended_href: "/wms",
    findings: [
      {
        code: "low_stock_skus",
        title_zh: "低于安全库存",
        title_en: "Below safety stock",
        detail_zh: "LED-COB-24V-5M 等 4 个 SKU 可用量已低于安全库存。",
        detail_en: "LED-COB-24V-5M and 3 other SKUs fell below their safety stock.",
        count: 4,
        action_href: "/wms",
        samples: [{ sku: "LED-COB-24V-5M", available_qty: 120, safety_stock_qty: 400 }],
      },
    ],
  },
  "logistics-forwarder": {
    employee_name: "小海",
    employee_role_title: "物流出船专员",
    action_summary_zh: "已对 2 票停滞货件发起 17TRACK 即时刷新。",
    action_summary_en: "Triggered an immediate 17TRACK refresh for 2 stalled shipments.",
    recommended_href: "/orders",
    findings: [
      {
        code: "stalled_shipments",
        title_zh: "货件停滞超 72 小时",
        title_en: "Shipments stalled past 72 hours",
        detail_zh: "YT26119000123 与 YT26119000456 超过 72 小时没有新节点。",
        detail_en: "YT26119000123 and YT26119000456 have had no new tracking node for 72 hours.",
        count: 2,
        action_href: "/orders",
        samples: [{ tracking_number: "YT26119000123", standardized_status: "InTransit" }],
      },
    ],
  },
  "finance-assistant": {
    employee_name: "小账",
    employee_role_title: "财务助理",
    action_summary_zh: "已完成 2 张银行水单的金额匹配，核销仍需人工确认。",
    action_summary_en: "Matched 2 bank receipts by amount; settlement still needs human approval.",
    recommended_href: "/finance",
    findings: [
      {
        code: "pending_payments",
        title_zh: "待人工核销的水单",
        title_en: "Receipts awaiting settlement",
        detail_zh: "2 张水单已匹配到应收发票，等待人工确认核销。",
        detail_en: "2 receipts were matched to receivables and await human settlement.",
        count: 2,
        action_href: "/finance",
        samples: [{ receivable_no: "AR-202611-0007", amount: "7100.00", currency: "USD" }],
      },
    ],
  },
  "biz-analyst": {
    employee_name: "小析",
    employee_role_title: "经营分析师",
    action_summary_zh: "已归集 5 条待审提案的影响面与金额。",
    action_summary_en: "Summarised the impact and value of 5 pending proposals.",
    recommended_href: "/ai/proposals",
    findings: [
      {
        code: "pending_proposals",
        title_zh: "待审 AI 提案",
        title_en: "Pending AI proposals",
        detail_zh: "5 条 L1 提案等待人工审阅，最早一条已排队 9 小时。",
        detail_en: "5 L1 proposals await review; the oldest has been queued for 9 hours.",
        count: 5,
        action_href: "/ai/proposals",
        samples: [{ proposal_type: "quote_draft", count: 2 }],
      },
    ],
  },
  "b2c-cs": {
    employee_name: "小暖",
    employee_role_title: "B2C 贴心客服",
    action_summary_zh: "已在收件箱为 7 个未读会话生成回复草稿。",
    action_summary_en: "Drafted replies for 7 unread conversations in the inbox.",
    recommended_href: "/inbox",
    findings: [
      {
        code: "unread_conversations",
        title_zh: "未读会话",
        title_en: "Unread conversations",
        detail_zh: "7 个会话仍有未读消息，其中 2 个来自 WhatsApp。",
        detail_en: "7 conversations still hold unread messages, 2 of them on WhatsApp.",
        count: 7,
        action_href: "/inbox",
        samples: [{ channel_type: "whatsapp", unread_count: 3 }],
      },
    ],
  },
};

function mockDispatchFor(employeeCode: string): PatrolDispatchRead {
  const seed = MOCK_DISPATCH[employeeCode];
  const roster = mockPhase3bEmployees.find((row) => row.code === employeeCode);
  const fallback: MockDispatchSeed = {
    employee_name: roster?.display_name ?? employeeCode,
    employee_role_title: roster?.role_title ?? "AI 数字员工",
    action_summary_zh: "已完成一次只读巡检，未发现需要立即处理的风险。",
    action_summary_en: "Patrol finished read-only; no risk required immediate action.",
    recommended_href: "/ai/employees",
    findings: [
      {
        code: `${employeeCode}_patrol`,
        title_zh: "巡检完成",
        title_en: "Patrol completed",
        detail_zh: "本次巡检读取了租户范围内的 ERP 数据，未产生新的待办。",
        detail_en: "This run read tenant-scoped ERP rows and produced no new follow-up.",
        count: 0,
        action_href: "/ai/employees",
        samples: [],
      },
    ],
  };
  const chosen = seed ?? fallback;
  return {
    employee_code: employeeCode,
    employee_name: chosen.employee_name,
    employee_role_title: chosen.employee_role_title,
    generated_at: MOCK_GENERATED_AT,
    action_summary_zh: chosen.action_summary_zh,
    action_summary_en: chosen.action_summary_en,
    metrics: { findings_count: chosen.findings.length },
    findings: chosen.findings.map((finding) => ({ ...finding, samples: [...(finding.samples ?? [])] })),
    created_task_id: "33333333-3333-3333-3333-333333333333",
    recommended_href: chosen.recommended_href,
  };
}

let mockEmployeeSeq = 0;

function mockCreatedEmployee(payload: AiEmployeeCreate): AiEmployeeRead {
  mockEmployeeSeq += 1;
  const now = new Date().toISOString();
  return {
    id: `emp-custom-${mockEmployeeSeq}`,
    tenant_id: MOCK_TENANT,
    code: payload.code,
    display_name: payload.display_name,
    role_title: payload.role_title,
    department: payload.department,
    avatar_emoji: payload.avatar_emoji ?? null,
    description: payload.description,
    capability_codes: [...(payload.capability_codes ?? [])],
    autonomy_level: payload.autonomy_level ?? "L1_proposal",
    is_enabled: payload.is_enabled ?? true,
    tool_count: payload.tool_count ?? 0,
    system_persona: payload.system_persona ?? null,
    created_at: now,
    updated_at: now,
    allowed_tools: [...(payload.allowed_tools ?? [])],
    actions_7d: 0,
    tasks_7d: 0,
  };
}

interface PlaybookExtra {
  name_en: string;
  category_zh: string;
  category_en: string;
  audience_en: string;
  lead_funnel_en: string;
  best_practices_en: readonly string[];
  compliance_note_en: string;
}

/**
 * English half of each operating card. The Chinese audience / practices /
 * compliance text and the 7 + 7 region split stay in the single shared
 * `PLATFORM_PLAYBOOKS` table so a platform's publish mode can never disagree with
 * `official_api_supported`.
 */
const PLAYBOOK_EXTRA: Record<SocialPlatform, PlaybookExtra> = {
  instagram: {
    name_en: "Instagram",
    category_zh: "视觉种草 / 内容社区",
    category_en: "Visual discovery community",
    audience_en:
      "Young consumers in North America and Europe, plus home, apparel and print-on-demand store owners who decide fast and judge by visuals.",
    lead_funnel_en:
      "Reels and carousels create demand → profile CTA to DM or landing page → DM captures the RFQ → the thread becomes a CRM lead.",
    best_practices_en: [
      "Authorise through Unified Login, never a scraped session",
      "Lead the carousel with a styled shot, keep specs on slide two",
      "Answer comments within the hour to keep the DM window open",
    ],
    compliance_note_en:
      "Re-opened threads need a human HUMAN_AGENT tag within 7 days; the AI may never trigger an out-of-window message.",
  },
  tiktok: {
    name_en: "TikTok",
    category_zh: "短视频内容电商",
    category_en: "Short-video commerce",
    audience_en:
      "Content-driven shoppers in North America, Europe and South-East Asia who respond to factory-floor footage and fast demos.",
    lead_funnel_en:
      "Factory demo clip → profile or pinned comment CTA → comment questions routed to DM → RFQ captured as a lead.",
    best_practices_en: [
      "Pass Content Posting API review before expecting public reach",
      "Show the finished lighting effect in the first three seconds",
      "Publish native 9:16 with burned-in subtitles",
    ],
    compliance_note_en:
      "Re-uploading another platform's watermark is rejected; an unaudited app can only post self-view content.",
  },
  youtube: {
    name_en: "YouTube",
    category_zh: "长内容选型中心",
    category_en: "Long-form selection hub",
    audience_en:
      "High-ticket buyers and engineering procurement in North America and Europe who shortlist suppliers from long-form video.",
    lead_funnel_en:
      "Installation or selection video → Shorts slice for reach → B2B inquiry link in the description → quote request.",
    best_practices_en: [
      "Put specification and scenario keywords in the title",
      "Add chapter markers so buyers can jump to the spec section",
      "Budget the per-project upload quota before a launch week",
    ],
    compliance_note_en:
      "Data API uploads inherit the project's audit state; an unaudited project can be limited to private uploads.",
  },
  facebook: {
    name_en: "Facebook",
    category_zh: "社区与区域分销",
    category_en: "Community and regional resale",
    audience_en:
      "Neighbourhood consumers and small business owners in North America and Europe, strong for regional distribution and local service.",
    lead_funnel_en:
      "Page post shared into groups → comment auto private reply → Messenger inquiry lands in the inbox → CRM lead.",
    best_practices_en: [
      "Authorise the page and the linked Instagram account together",
      "End every post with one explicit next action",
      "Publish the group etiquette before the first campaign",
    ],
    compliance_note_en:
      "Private replies must follow the Private Replies rules; scraping group member lists for cold DMs is prohibited.",
  },
  linkedin: {
    name_en: "LinkedIn",
    category_zh: "B2B 决策链",
    category_en: "B2B decision chain",
    audience_en:
      "Procurement, distributors and engineering firms in North America and Europe who buy on certifications and delivery record.",
    lead_funnel_en:
      "Company page case study → industry topic comment threads → employee reshares → inbound RFQ.",
    best_practices_en: [
      "Community Management API needs Vetted Review approval first",
      "Back every case with certifications and delivery metrics",
      "Skip marketing tone, document delivery capability instead",
    ],
    compliance_note_en:
      "Reading personal member feeds is closed to third parties; only owned pages and authorised content can be managed.",
  },
  pinterest: {
    name_en: "Pinterest",
    category_zh: "搜索型灵感流量",
    category_en: "Search-driven inspiration",
    audience_en:
      "Home, lighting, apparel and print-on-demand sellers in North America and Europe searching for scene-led product inspiration.",
    lead_funnel_en:
      "Scene Pin → selection guide board → landing page → RFQ form → CRM lead with the Pin as the source.",
    best_practices_en: [
      "Standard Access is required before public publishing",
      "Use 2:3 vertical pins with keyword-rich descriptions",
      "Keep the landing page theme identical to the pin",
    ],
    compliance_note_en:
      "An account without Standard Access can only publish in a restricted mode until the app review passes.",
  },
  x: {
    name_en: "X",
    category_zh: "行业观点快讯",
    category_en: "Industry commentary",
    audience_en:
      "Technology, trade and supply-chain practitioners in North America and Europe who follow industry news and sourcing takes.",
    lead_funnel_en:
      "Product news thread → industry conversation → case retweet → direct message inquiry → CRM lead.",
    best_practices_en: [
      "Keep each post inside the character limit",
      "Evaluate per-link pricing before every budget cycle",
      "Lead the thread with the conclusion, then the evidence",
    ],
    compliance_note_en:
      "The API is metered; posts carrying URLs cost more, so per-tenant usage must be budgeted and monitored.",
  },
  xiaohongshu: {
    name_en: "Xiaohongshu",
    category_zh: "美学种草社区",
    category_en: "Aesthetic discovery",
    audience_en:
      "Young urban women and home-aesthetic buyers who trust first-hand experience and clean visuals over ad copy.",
    lead_funnel_en:
      "Creator note collection → comment consultation → professional account private domain → RFQ to CRM.",
    best_practices_en: [
      "3:4 cover with a title under twenty characters",
      "Write the real scene and the pitfalls, not the parameters",
      "Keep five to eight precise topic tags",
    ],
    compliance_note_en:
      "A generic marketing SaaS has no write_notes scope: export the cut and publish manually in the official creator centre. Bulk DM tooling is prohibited.",
  },
  douyin: {
    name_en: "Douyin",
    category_zh: "兴趣电商 / 直播",
    category_en: "Interest commerce and livestream",
    audience_en:
      "Domestic interest-commerce shoppers and small distributors who buy from live demos and short factory clips.",
    lead_funnel_en:
      "Live demo → storefront or comment CTA → direct message consultation → RFQ captured → CRM lead.",
    best_practices_en: [
      "Keep a consistent posting cadence during the live window",
      "State the wholesale MOQ in the first three seconds",
      "Never repost a clip that carries another platform watermark",
    ],
    compliance_note_en:
      "Enterprise customisation is required for API publishing; otherwise export the finished cut and publish in the Douyin console.",
  },
  kuaishou: {
    name_en: "Kuaishou",
    category_zh: "老铁信任电商",
    category_en: "Trust-based commerce",
    audience_en:
      "Lower-tier-city consumers and regional resellers who buy repeatedly from creators they trust.",
    lead_funnel_en:
      "Trusted creator clip → live room inquiry → private message → wholesale RFQ → CRM lead.",
    best_practices_en: [
      "Keep one host voice across the whole series",
      "Quote the wholesale price ladder openly in the stream",
      "Reuse the same product code as the catalog",
    ],
    compliance_note_en:
      "No third-party publishing scope: export the cut and confirm manually in the Kuaishou creator console.",
  },
  bilibili: {
    name_en: "Bilibili",
    category_zh: "深度评测与知识",
    category_en: "Deep review and knowledge",
    audience_en:
      "Engineering students, technical buyers and DIY installers who shortlist hardware after long technical reviews.",
    lead_funnel_en:
      "Technical teardown video → column article → comment Q&A → private message → sample request.",
    best_practices_en: [
      "Show the measurement data behind every claim",
      "Publish a matching column article for search traffic",
      "Answer technical comments with the real datasheet value",
    ],
    compliance_note_en:
      "No open publishing API for third parties: export the finished video and submit it in the Bilibili creator centre.",
  },
  wechat_video: {
    name_en: "WeChat Channels",
    category_zh: "私域视频号",
    category_en: "Private-domain video",
    audience_en:
      "Existing WeChat contacts, dealers and repeat buyers who already hold the brand in their contact list.",
    lead_funnel_en:
      "Channels clip → official account article → mini-program or group entry → private domain consult → order.",
    best_practices_en: [
      "Bind the Channels account to the certified official account",
      "Keep vertical 9:16 with a clear first frame",
      "Push the clip into the customer group the same day",
    ],
    compliance_note_en:
      "Publishing runs through the official Channels console; third-party automation of private-domain messaging is not permitted.",
  },
  wechat_oa: {
    name_en: "WeChat Official Account",
    category_zh: "私域沉淀与复购",
    category_en: "Private-domain retention",
    audience_en:
      "Subscribers, dealers and past buyers who accept a slower, deeper read and convert on repeat purchase.",
    lead_funnel_en:
      "Service notification → long-form article → mini-program landing → consultant add → order.",
    best_practices_en: [
      "Send within the monthly template-message quota",
      "Lead the article with the buyer's actual problem",
      "Route every CTA to a tracked mini-program page",
    ],
    compliance_note_en:
      "Template messages and drafts must respect the official account quotas; a mock fixture never replaces a real publish receipt.",
  },
  weibo: {
    name_en: "Weibo",
    category_zh: "公域话题扩散",
    category_en: "Public topic amplification",
    audience_en:
      "Domestic trend followers and media accounts that amplify a topic faster than any other domestic channel.",
    lead_funnel_en:
      "Topic post → hot-search participation → brand account follow → private message → CRM lead.",
    best_practices_en: [
      "Attach one topic tag that already has traffic",
      "Post within the first hour of a trend spike",
      "Keep the official account voice consistent across campaigns",
    ],
    compliance_note_en:
      "Paid amplification must be declared; third-party auto-posting without an authorised app is prohibited.",
  },
};

function mockPlaybook(row: (typeof PLATFORM_PLAYBOOKS)[number]): SocialPlaybookItem {
  const extra = PLAYBOOK_EXTRA[row.platform];
  return {
    platform: row.platform,
    region: row.region,
    name_zh: row.label,
    name_en: extra.name_en,
    category_zh: extra.category_zh,
    category_en: extra.category_en,
    audience_zh: row.audience,
    audience_en: extra.audience_en,
    lead_funnel_zh: row.acquisition_plays.join(" → "),
    lead_funnel_en: extra.lead_funnel_en,
    best_practices_zh: [...row.best_practices],
    best_practices_en: [...extra.best_practices_en],
    compliance_note_zh: row.compliance_note,
    compliance_note_en: extra.compliance_note_en,
    official_api_supported: row.publish_mode === "api",
  };
}

export const mockSocialPlaybooks: SocialPlaybookItem[] = PLATFORM_PLAYBOOKS.map(mockPlaybook);

export type { DailyPatrolBriefRead, PatrolAlertItem, PatrolDispatchRead, SocialPlaybookItem };
