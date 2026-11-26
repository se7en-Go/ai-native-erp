// Track 1 client: external notification channels (Feishu / DingTalk / WeCom / generic
// webhook) and the five event-driven automation rules.
//
// Every call travels through `bffFetch` (`/api/bff/v1/ai/...`), so the session cookie,
// the 401 refresh flow and the BFF `/api/v1/ai` prefix allowlist behave exactly like the
// rest of the workbench. No secret ever reaches this module: the backend only returns a
// masked URL preview plus `has_signing_secret`.
//
// `NEXT_PUBLIC_USE_MOCK_API=true` short-circuits every function to a deterministic local
// fixture set so `e2e-mock` keeps rendering the panel without a backend.
import { bffFetch } from "@/lib/api";
import { shouldUseMockApi } from "@/lib/data-source";
import type {
  AutomationEvaluationReport,
  AutomationRuleEvaluationItem,
  AutomationRuleRead,
  AutomationSeedResult,
  DeliveryRetrySweepResult,
  NotificationChannelRead,
  NotificationDeliveryRead,
  NotificationTestResult,
} from "@/lib/api-client/types.gen";

export type NotificationProvider = NotificationChannelRead["provider"];
export type NotificationPriority = NotificationChannelRead["min_priority"];
export type DeliveryStatus = NotificationDeliveryRead["status"];
export type AutomationTemplateCode = AutomationRuleRead["template_code"];
export type NotificationChannel = NotificationChannelRead;
export type NotificationDelivery = NotificationDeliveryRead;
export type AutomationRule = AutomationRuleRead;
export type AutomationRuleEvaluation = AutomationRuleEvaluationItem;
export type AutomationReport = AutomationEvaluationReport;

export interface NotificationChannelInput {
  name: string;
  provider: NotificationProvider;
  webhook_url: string;
  secret?: string | null;
  min_priority: NotificationPriority;
  is_enabled?: boolean;
}

export interface NotificationChannelPatch {
  name?: string;
  webhook_url?: string;
  secret?: string | null;
  min_priority?: NotificationPriority;
  is_enabled?: boolean;
}

export interface AutomationRulePatch {
  name?: string;
  is_enabled?: boolean;
  priority?: NotificationPriority;
  autonomy_level?: AutomationRuleRead["autonomy_level"];
  config_json?: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// Bilingual presentation copy (shared by the panel and its tests)
// ---------------------------------------------------------------------------

export const PROVIDER_LABELS: Record<NotificationProvider, { zh: string; en: string }> = {
  feishu: { zh: "飞书", en: "Feishu" },
  dingtalk: { zh: "钉钉", en: "DingTalk" },
  wecom: { zh: "企业微信", en: "WeCom" },
  generic_webhook: { zh: "通用 Webhook", en: "Generic webhook" },
};

export const PRIORITY_LABELS: Record<NotificationPriority, { zh: string; en: string }> = {
  P0: { zh: "P0 紧急", en: "P0 urgent" },
  P1: { zh: "P1 重要", en: "P1 important" },
  P2: { zh: "P2 简报", en: "P2 digest" },
};

export const DELIVERY_STATUS_LABELS: Record<DeliveryStatus, { zh: string; en: string }> = {
  pending: { zh: "待发送", en: "Pending" },
  sent: { zh: "已送达", en: "Sent" },
  failed: { zh: "失败", en: "Failed" },
  retrying: { zh: "重试中", en: "Retrying" },
};

/** One primary threshold per template, so the panel edits the field the rule reads. */
export const RULE_THRESHOLD_FIELD: Record<
  AutomationTemplateCode,
  { key: string; zh: string; en: string; kind: "int" | "decimal" }
> = {
  low_doc_replenishment: { key: "doc_days", zh: "可售天数阈值", en: "Days-of-cover", kind: "int" },
  negative_margin_hold: {
    key: "min_margin_pct",
    zh: "毛利下限",
    en: "Margin floor",
    kind: "decimal",
  },
  stalled_shipment_soothe: {
    key: "stalled_hours",
    zh: "停滞小时",
    en: "Stalled hours",
    kind: "int",
  },
  cold_b2b_quote_followup: {
    key: "cold_hours",
    zh: "沉睡小时",
    en: "Cold hours",
    kind: "int",
  },
  daily_patrol_brief: {
    key: "brief_hour_utc",
    zh: "发送小时 (UTC)",
    en: "Brief hour (UTC)",
    kind: "int",
  },
};

/** Per-rule copy; the wording mirrors the backend template names. */
export const RULE_COPY: Record<
  AutomationTemplateCode,
  { zh: string; en: string; description_zh: string; description_en: string; href: string }
> = {
  low_doc_replenishment: {
    zh: "低库存补货",
    en: "Low stock replenishment",
    description_zh: "按安全库存与 30 天销量推算可售天数，低于阈值即生成 L1 补货建议。",
    description_en:
      "Projects days of cover from safety stock and 30-day sales velocity and drafts an L1 replenishment proposal.",
    href: "/wms",
  },
  negative_margin_hold: {
    zh: "负毛利拦截",
    en: "Negative margin hold",
    description_zh: "近 7 天订单净贡献毛利低于下限时立即发送 P0 告警。",
    description_en:
      "Fires a P0 alert the moment a recent order's net contribution margin falls below the floor.",
    href: "/finance",
  },
  stalled_shipment_soothe: {
    zh: "停滞货件安抚",
    en: "Stalled shipment soothe",
    description_zh: "未签收且超过阈值无新节点的货件，自动起草客户安抚与催查建议。",
    description_en:
      "For shipments silent past the threshold, drafts a reassurance and carrier-chase follow-up.",
    href: "/orders",
  },
  cold_b2b_quote_followup: {
    zh: "沉睡报价跟进",
    en: "Cold quote follow-up",
    description_zh: "已发送超时未转化的 B2B 报价，自动生成跟进建议。",
    description_en:
      "Drafts a follow-up for sent B2B quotes that went past the cold window without converting.",
    href: "/b2b/quotes",
  },
  daily_patrol_brief: {
    zh: "每日巡检简报",
    en: "Daily patrol brief",
    description_zh: "把真实巡检健康分与告警汇总成一条 P2 简报推送。",
    description_en:
      "Pushes the real patrol health score and alerts as one P2 briefing message.",
    href: "/ai/employees",
  },
};

export function templateLabel(
  code: AutomationTemplateCode,
  locale: "zh" | "en" = "zh",
): string {
  const copy = RULE_COPY[code];
  return locale === "en" ? copy.en : copy.zh;
}

/** Threshold value rendered as a stable string for the editable input. */
export function thresholdValue(rule: AutomationRule, key: string): string {
  const raw = rule.config_json?.[key];
  if (raw === null || raw === undefined) return "";
  return String(raw);
}

// ---------------------------------------------------------------------------
// Live calls
// ---------------------------------------------------------------------------

export function fetchNotificationChannels(): Promise<NotificationChannel[]> {
  if (shouldUseMockApi()) return Promise.resolve(mockChannels.map((row) => ({ ...row })));
  return bffFetch<NotificationChannel[]>("/v1/ai/notifications/channels");
}

export function createNotificationChannel(
  input: NotificationChannelInput,
): Promise<NotificationChannel> {
  if (shouldUseMockApi()) return Promise.resolve(mockCreateChannel(input));
  return bffFetch<NotificationChannel>("/v1/ai/notifications/channels", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function updateNotificationChannel(
  channelId: string,
  patch: NotificationChannelPatch,
): Promise<NotificationChannel> {
  if (shouldUseMockApi()) return Promise.resolve(mockUpdateChannel(channelId, patch));
  return bffFetch<NotificationChannel>(
    `/v1/ai/notifications/channels/${encodeURIComponent(channelId)}`,
    { method: "PATCH", body: JSON.stringify(patch) },
  );
}

export function deleteNotificationChannel(channelId: string): Promise<void> {
  if (shouldUseMockApi()) {
    mockDeleteChannel(channelId);
    return Promise.resolve();
  }
  return bffFetch<void>(
    `/v1/ai/notifications/channels/${encodeURIComponent(channelId)}`,
    { method: "DELETE", parseJson: false },
  );
}

export function testNotificationChannel(channelId: string): Promise<NotificationTestResult> {
  if (shouldUseMockApi()) return Promise.resolve(mockTestChannel(channelId));
  return bffFetch<NotificationTestResult>(
    `/v1/ai/notifications/channels/${encodeURIComponent(channelId)}/test`,
    { method: "POST" },
  );
}

export interface DeliveryPage {
  items: NotificationDelivery[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
}

export function fetchNotificationDeliveries(limit = 20): Promise<DeliveryPage> {
  if (shouldUseMockApi()) {
    return Promise.resolve({
      items: mockDeliveries.slice(0, limit).map((row) => ({ ...row })),
      total: mockDeliveries.length,
      limit,
      offset: 0,
      has_more: mockDeliveries.length > limit,
    });
  }
  return bffFetch<DeliveryPage>(`/v1/ai/notifications/deliveries?limit=${limit}`);
}

export function retryDueNotificationDeliveries(): Promise<DeliveryRetrySweepResult> {
  if (shouldUseMockApi()) {
    return Promise.resolve({ attempted: 0, sent: 0, retrying: 0, failed: 0 });
  }
  return bffFetch<DeliveryRetrySweepResult>("/v1/ai/notifications/deliveries/retry-due", {
    method: "POST",
  });
}

export function fetchAutomationRules(): Promise<AutomationRule[]> {
  if (shouldUseMockApi()) return Promise.resolve(mockRules.map((row) => ({ ...row })));
  return bffFetch<AutomationRule[]>("/v1/ai/automation-rules");
}

export function seedAutomationRules(): Promise<AutomationSeedResult> {
  if (shouldUseMockApi()) {
    return Promise.resolve({
      created: 0,
      existing: mockRules.length,
      rules: mockRules.map((row) => ({ ...row })),
    });
  }
  return bffFetch<AutomationSeedResult>("/v1/ai/automation-rules/seed-defaults", {
    method: "POST",
  });
}

export function updateAutomationRule(
  ruleId: string,
  patch: AutomationRulePatch,
): Promise<AutomationRule> {
  if (shouldUseMockApi()) return Promise.resolve(mockUpdateRule(ruleId, patch));
  return bffFetch<AutomationRule>(`/v1/ai/automation-rules/${encodeURIComponent(ruleId)}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

export function evaluateAutomationRules(dryRun: boolean): Promise<AutomationReport> {
  if (shouldUseMockApi()) return Promise.resolve(mockEvaluate(dryRun));
  return bffFetch<AutomationReport>("/v1/ai/automation-rules/evaluate", {
    method: "POST",
    body: JSON.stringify({ dry_run: dryRun }),
  });
}

// ---------------------------------------------------------------------------
// Mock fixtures (`NEXT_PUBLIC_USE_MOCK_API=true`)
// ---------------------------------------------------------------------------

const MOCK_TENANT_CHANNEL_ID = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa";
const MOCK_NOW = "2026-11-26T09:00:00.000Z";

function mockChannel(
  id: string,
  name: string,
  provider: NotificationProvider,
  minPriority: NotificationPriority,
  preview: string,
): NotificationChannel {
  return {
    id,
    name,
    provider,
    webhook_url_preview: preview,
    has_signing_secret: provider !== "wecom",
    min_priority: minPriority,
    is_enabled: true,
    last_tested_at: MOCK_NOW,
    last_error: null,
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
  };
}

const mockChannels: NotificationChannel[] = [
  mockChannel(MOCK_TENANT_CHANNEL_ID, "运营飞书群", "feishu", "P1", "https://open.feishu.cn/***3456"),
  mockChannel(
    "aaaaaaaa-2222-4222-8222-aaaaaaaaaaaa",
    "财务钉钉群",
    "dingtalk",
    "P0",
    "https://oapi.dingtalk.com/***3456",
  ),
];

let mockChannelSeq = 0;

function mockCreateChannel(input: NotificationChannelInput): NotificationChannel {
  mockChannelSeq += 1;
  const created = mockChannel(
    `mock-channel-${mockChannelSeq}`,
    input.name,
    input.provider,
    input.min_priority,
    "https://hooks.example.com/***mock",
  );
  created.has_signing_secret = Boolean(input.secret);
  mockChannels.unshift(created);
  return { ...created };
}

function mockUpdateChannel(
  channelId: string,
  patch: NotificationChannelPatch,
): NotificationChannel {
  const index = mockChannels.findIndex((row) => row.id === channelId);
  if (index < 0) throw new Error("Notification channel not found");
  const current = mockChannels[index];
  const next: NotificationChannel = {
    ...current,
    ...(patch.name !== undefined ? { name: patch.name } : {}),
    ...(patch.min_priority !== undefined ? { min_priority: patch.min_priority } : {}),
    ...(patch.is_enabled !== undefined ? { is_enabled: patch.is_enabled } : {}),
    ...(patch.secret ? { has_signing_secret: true } : {}),
    updated_at: MOCK_NOW,
  };
  mockChannels[index] = next;
  return { ...next };
}

function mockDeleteChannel(channelId: string): void {
  const index = mockChannels.findIndex((row) => row.id === channelId);
  if (index >= 0) mockChannels.splice(index, 1);
}

function mockTestChannel(channelId: string): NotificationTestResult {
  return {
    channel_id: channelId,
    ok: true,
    http_status: 200,
    error_message: null,
    tested_at: MOCK_NOW,
  };
}

function mockDelivery(
  id: string,
  channelId: string,
  channelName: string,
  provider: NotificationProvider,
  priority: NotificationPriority,
  eventType: string,
  title: string,
  status: DeliveryStatus,
  attempts: number,
  httpStatus: number | null,
): NotificationDelivery {
  return {
    id,
    channel_id: channelId,
    channel_name: channelName,
    provider,
    priority,
    event_type: eventType,
    title,
    body: title,
    deep_link: null,
    status,
    attempts,
    next_retry_at: status === "retrying" ? MOCK_NOW : null,
    http_status: httpStatus,
    error_message: status === "failed" ? "HTTP 500: upstream rejected" : null,
    created_at: MOCK_NOW,
    sent_at: status === "sent" ? MOCK_NOW : null,
  };
}

const mockDeliveries: NotificationDelivery[] = [
  mockDelivery(
    "dddddddd-1111-4111-8111-dddddddddddd",
    MOCK_TENANT_CHANNEL_ID,
    "运营飞书群",
    "feishu",
    "P0",
    "automation.negative_margin_hold",
    "P0 负毛利拦截：1 张订单毛利低于 0.00",
    "sent",
    1,
    200,
  ),
  mockDelivery(
    "dddddddd-2222-4222-8222-dddddddddddd",
    MOCK_TENANT_CHANNEL_ID,
    "运营飞书群",
    "feishu",
    "P1",
    "automation.low_doc_replenishment",
    "补货提醒：3 个 SKU 需要补货",
    "retrying",
    1,
    500,
  ),
];

function mockRule(
  id: string,
  templateCode: AutomationTemplateCode,
  priority: NotificationPriority,
  config: Record<string, unknown>,
): AutomationRule {
  return {
    id,
    template_code: templateCode,
    name: RULE_COPY[templateCode].zh,
    is_enabled: true,
    autonomy_level: "L1_proposal",
    priority,
    config_json: config,
    last_run_at: MOCK_NOW,
    last_triggered_count: 3,
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
  };
}

const mockRules: AutomationRule[] = [
  mockRule("rrrrrrrr-1111-4111-8111-rrrrrrrrrrrr", "low_doc_replenishment", "P1", {
    doc_days: 15,
    safety_stock_qty: 50,
  }),
  mockRule("rrrrrrrr-2222-4222-8222-rrrrrrrrrrrr", "negative_margin_hold", "P0", {
    min_margin_pct: "0.00",
  }),
  mockRule("rrrrrrrr-3333-4333-8333-rrrrrrrrrrrr", "stalled_shipment_soothe", "P1", {
    stalled_hours: 72,
  }),
  mockRule("rrrrrrrr-4444-4444-8444-rrrrrrrrrrrr", "cold_b2b_quote_followup", "P1", {
    cold_hours: 72,
  }),
  mockRule("rrrrrrrr-5555-4555-8555-rrrrrrrrrrrr", "daily_patrol_brief", "P2", {
    brief_hour_utc: 1,
  }),
];

/** Deterministic mock matched counts, keyed by template. */
const MOCK_MATCHED: Record<AutomationTemplateCode, number> = {
  low_doc_replenishment: 3,
  negative_margin_hold: 1,
  stalled_shipment_soothe: 2,
  cold_b2b_quote_followup: 2,
  daily_patrol_brief: 4,
};

function mockUpdateRule(ruleId: string, patch: AutomationRulePatch): AutomationRule {
  const index = mockRules.findIndex((row) => row.id === ruleId);
  if (index < 0) throw new Error("Automation rule not found");
  const current = mockRules[index];
  const next: AutomationRule = {
    ...current,
    ...(patch.name !== undefined ? { name: patch.name } : {}),
    ...(patch.is_enabled !== undefined ? { is_enabled: patch.is_enabled } : {}),
    ...(patch.priority !== undefined ? { priority: patch.priority } : {}),
    ...(patch.autonomy_level !== undefined ? { autonomy_level: patch.autonomy_level } : {}),
    ...(patch.config_json !== undefined
      ? { config_json: { ...current.config_json, ...patch.config_json } }
      : {}),
    updated_at: MOCK_NOW,
  };
  mockRules[index] = next;
  return { ...next };
}

function mockEvaluate(dryRun: boolean): AutomationReport {
  const rules: AutomationRuleEvaluation[] = mockRules.map((rule) => {
    const matched = rule.is_enabled ? MOCK_MATCHED[rule.template_code] : 0;
    return {
      template_code: rule.template_code,
      name: rule.name,
      is_enabled: rule.is_enabled,
      priority: rule.priority,
      autonomy_level: rule.autonomy_level,
      matched_count: matched,
      proposals_created: dryRun ? 0 : Math.min(matched, 3),
      notifications_queued: dryRun ? 0 : 1,
      items_preview: [],
      note: dryRun ? "dry_run: 未写入数据库。" : null,
    };
  });
  return {
    generated_at: MOCK_NOW,
    dry_run: dryRun,
    seeded_defaults: false,
    total_matched: rules.reduce((sum, rule) => sum + rule.matched_count, 0),
    total_proposals_created: rules.reduce((sum, rule) => sum + rule.proposals_created, 0),
    total_notifications_queued: rules.reduce(
      (sum, rule) => sum + rule.notifications_queued,
      0,
    ),
    rules,
  };
}

export const mockTrack1State = { channels: mockChannels, deliveries: mockDeliveries, rules: mockRules };
