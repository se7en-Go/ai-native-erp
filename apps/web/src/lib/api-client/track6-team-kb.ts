// Track 6 client module: team store-scope governance + offboarding handover,
// outbound red-line guard, knowledge-base gap backlog, and AI adoption metrics.
//
// Every request travels through `bffFetch` (`/api/bff/v1/...`), so the session
// cookie, the 401 refresh flow and the BFF role gate behave exactly like the
// rest of the workbench — `/api/v1/tenants` and `/api/v1/ai` are both on the
// operator allowlist. The browser never sees SQL, an ORM, or a recomputed
// metric: every count and every rate is server truth serialised as an exact
// decimal string.
//
// Mock mode (`NEXT_PUBLIC_USE_MOCK_API=true`) never reaches the network. The
// fixtures below are mutable, so a mock grant / revoke / handover / promotion
// really moves the state the panels read.
import { bffFetch } from "@/lib/api";
import { shouldUseMockApi } from "@/lib/data-source";
import type {
  AiAdoptionMetrics,
  HandoverRequest,
  HandoverResult,
  KbGapCreateRequest,
  KbGapPromoteResult,
  KbGapRead,
  PaginatedResponseKbGapRead,
  RedlineCheckResult,
  RedlineRuleCreate,
  RedlineRuleRead,
  StoreScopeAssignRequest,
  StoreScopeRead,
} from "@/lib/api-client/types.gen";

export type StoreAccessLevel = StoreScopeRead["access_level"];
export type RedlineMatchType = RedlineRuleRead["match_type"];
export type RedlineAction = RedlineRuleRead["action"];

/** Lowest → highest. Mutations require `manage`, operations accept `operate`. */
export const STORE_ACCESS_LEVELS: readonly StoreAccessLevel[] = [
  "read",
  "operate",
  "manage",
] as const;

/** Exact ordering rank of an access level (0 = read, 2 = manage). */
export function accessLevelRank(level: StoreAccessLevel): number {
  return STORE_ACCESS_LEVELS.indexOf(level);
}

/** True when the grant is enough to actually answer customers on the store. */
export function canOperateStore(level: StoreAccessLevel | null | undefined): boolean {
  if (!level) return false;
  return accessLevelRank(level) >= accessLevelRank("operate");
}

/** True when the grant allows re-configuring the store (ownership, tokens). */
export function canManageStore(level: StoreAccessLevel | null | undefined): boolean {
  if (!level) return false;
  return accessLevelRank(level) >= accessLevelRank("manage");
}

/** `"66.67"` -> `"66.67%"`, without ever touching a float. */
export function formatRatePct(value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "0.00%";
  return `${value}%`;
}

export interface HandoverImpactRow {
  key: "opportunities" | "conversations" | "channel_accounts" | "store_scopes";
  label_zh: string;
  label_en: string;
  count: number;
}

/** The exact per-asset impact cards the handover panel renders. */
export function handoverImpactRows(result: HandoverResult): HandoverImpactRow[] {
  return [
    {
      key: "opportunities",
      label_zh: "商机",
      label_en: "Opportunities",
      count: result.opportunities_reassigned,
    },
    {
      key: "conversations",
      label_zh: "会话",
      label_en: "Conversations",
      count: result.conversations_reassigned,
    },
    {
      key: "channel_accounts",
      label_zh: "渠道账号",
      label_en: "Channel accounts",
      count: result.channel_accounts_reassigned,
    },
    {
      key: "store_scopes",
      label_zh: "门店权限",
      label_en: "Store grants",
      count: result.store_scopes_reassigned,
    },
  ];
}

export interface RedlineDraft {
  name: string;
  pattern: string;
  match_type: RedlineMatchType;
  action: RedlineAction;
}

export const REDLINE_PATTERN_LIMIT = 200;

/**
 * Client-side mirror of the backend red-line validation, so the operator gets
 * instant feedback instead of a 422 round-trip. The server remains
 * authoritative (`ERR_INVALID_REDLINE_PATTERN` / `ERR_REDLINE_PATTERN_TOO_LONG`).
 */
export function validateRedlineDraft(draft: RedlineDraft): { ok: boolean; error: string | null } {
  if (draft.name.trim().length === 0) {
    return { ok: false, error: "请填写规则名称 / A rule name is required." };
  }
  if (draft.pattern.trim().length === 0) {
    return { ok: false, error: "请填写匹配内容 / A pattern is required." };
  }
  if (draft.pattern.length > REDLINE_PATTERN_LIMIT) {
    return {
      ok: false,
      error: `匹配内容最长 ${REDLINE_PATTERN_LIMIT} 字符 / Patterns are capped at ${REDLINE_PATTERN_LIMIT} characters.`,
    };
  }
  if (draft.match_type === "regex") {
    try {
      // eslint-disable-next-line no-new
      new RegExp(draft.pattern);
    } catch {
      return { ok: false, error: "正则表达式无法编译 / That regex does not compile." };
    }
  }
  return { ok: true, error: null };
}

// ---------------------------------------------------------------------------
// Defensive normalisation: a proxy/older backend must never crash a render.
// ---------------------------------------------------------------------------

function asArray<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

function asNumber(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function asText(value: unknown, fallback = "0.00"): string {
  return typeof value === "string" && value.length > 0 ? value : fallback;
}

function normalizeGapPage(raw: unknown): PaginatedResponseKbGapRead {
  const page = (raw ?? {}) as Partial<PaginatedResponseKbGapRead>;
  const items = asArray<KbGapRead>(page.items);
  return {
    items,
    total: asNumber(page.total, items.length),
    limit: asNumber(page.limit, 50),
    offset: asNumber(page.offset, 0),
    has_more: Boolean(page.has_more),
  };
}

export function emptyAdoptionMetrics(): AiAdoptionMetrics {
  return {
    tenant_id: "",
    window_days: 30,
    total: 0,
    pending: 0,
    approved: 0,
    edited_and_approved: 0,
    rejected: 0,
    expired: 0,
    failed: 0,
    adopted: 0,
    review_base: 0,
    adoption_rate_pct: "0.00",
    direct_adopt_rate_pct: "0.00",
    edit_adopt_rate_pct: "0.00",
    rejection_rate_pct: "0.00",
    by_type: [],
  };
}

function normalizeAdoptionMetrics(raw: unknown): AiAdoptionMetrics {
  const metrics = (raw ?? {}) as Partial<AiAdoptionMetrics>;
  const fallback = emptyAdoptionMetrics();
  return {
    ...fallback,
    tenant_id: asText(metrics.tenant_id, fallback.tenant_id),
    window_days: asNumber(metrics.window_days, 30),
    total: asNumber(metrics.total),
    pending: asNumber(metrics.pending),
    approved: asNumber(metrics.approved),
    edited_and_approved: asNumber(metrics.edited_and_approved),
    rejected: asNumber(metrics.rejected),
    expired: asNumber(metrics.expired),
    failed: asNumber(metrics.failed),
    adopted: asNumber(metrics.adopted),
    review_base: asNumber(metrics.review_base),
    adoption_rate_pct: asText(metrics.adoption_rate_pct),
    direct_adopt_rate_pct: asText(metrics.direct_adopt_rate_pct),
    edit_adopt_rate_pct: asText(metrics.edit_adopt_rate_pct),
    rejection_rate_pct: asText(metrics.rejection_rate_pct),
    by_type: asArray(metrics.by_type),
  };
}

function normalizeRedlineCheck(raw: unknown): RedlineCheckResult {
  const result = (raw ?? {}) as Partial<RedlineCheckResult>;
  const matchedRules = asArray<RedlineCheckResult["matched_rules"][number]>(result.matched_rules);
  return {
    blocked: Boolean(result.blocked),
    blocks: asArray<string>(result.blocks),
    warnings: asArray<string>(result.warnings),
    matched_rules: matchedRules,
  };
}

// ---------------------------------------------------------------------------
// Mock fixtures (mutable, so mock-mode mutations are visible)
// ---------------------------------------------------------------------------

const MOCK_STORE_SCOPES: StoreScopeRead[] = [
  {
    id: "scope-mock-1",
    tenant_id: "t1",
    user_id: "u-mock-agent",
    user_email: "agent@quanzhou-led.example.com",
    user_full_name: "Lin Wei",
    channel_account_id: "ch-telegram-1",
    channel_type: "telegram",
    channel_display_name: "QZ LED 外贸机器人",
    access_level: "operate",
    created_at: "2026-11-20T08:00:00Z",
  },
  {
    id: "scope-mock-2",
    tenant_id: "t1",
    user_id: "u-mock-manager",
    user_email: "manager@quanzhou-led.example.com",
    user_full_name: "Chen Yu",
    channel_account_id: "ch-web-1",
    channel_type: "web_livechat",
    channel_display_name: "官网客服",
    access_level: "manage",
    created_at: "2026-11-21T09:30:00Z",
  },
];

const MOCK_REDLINE_RULES: RedlineRuleRead[] = [
  {
    id: "rule-mock-1",
    tenant_id: "t1",
    name: "禁提押金 / No deposit talk",
    pattern: "deposit",
    match_type: "keyword",
    action: "block",
    is_enabled: true,
    created_at: "2026-11-18T02:00:00Z",
  },
  {
    id: "rule-mock-2",
    tenant_id: "t1",
    name: "禁承诺交期 / No guaranteed lead time",
    pattern: "\\bguarantee[ds]?\\b",
    match_type: "regex",
    action: "warn",
    is_enabled: true,
    created_at: "2026-11-18T02:05:00Z",
  },
];

const MOCK_KB_GAPS: KbGapRead[] = [
  {
    id: "gap-mock-1",
    tenant_id: "t1",
    conversation_id: "conv-1",
    customer_question: "Do you ship DDP to Chile?",
    detected_language: "en",
    frequency_count: 7,
    status: "open",
    resolved_answer: null,
    promoted_kb_document_id: null,
    created_at: "2026-11-19T04:00:00Z",
    updated_at: "2026-11-24T04:00:00Z",
  },
  {
    id: "gap-mock-2",
    tenant_id: "t1",
    conversation_id: null,
    customer_question: "IP67 版本能做吗？",
    detected_language: "zh",
    frequency_count: 3,
    status: "open",
    resolved_answer: null,
    promoted_kb_document_id: null,
    created_at: "2026-11-21T06:00:00Z",
    updated_at: "2026-11-23T06:00:00Z",
  },
];

const MOCK_ADOPTION_METRICS: AiAdoptionMetrics = {
  tenant_id: "t1",
  window_days: 30,
  total: 26,
  pending: 8,
  approved: 9,
  edited_and_approved: 5,
  rejected: 3,
  expired: 1,
  failed: 0,
  adopted: 14,
  review_base: 17,
  adoption_rate_pct: "82.35",
  direct_adopt_rate_pct: "52.94",
  edit_adopt_rate_pct: "35.71",
  rejection_rate_pct: "17.65",
  by_type: [
    {
      proposal_type: "reply_draft",
      total: 14,
      pending: 4,
      approved: 6,
      edited_and_approved: 3,
      rejected: 1,
      adoption_rate_pct: "90.00",
    },
    {
      proposal_type: "quote_draft",
      total: 7,
      pending: 2,
      approved: 2,
      edited_and_approved: 2,
      rejected: 1,
      adoption_rate_pct: "80.00",
    },
    {
      proposal_type: "crm_entity_extract",
      total: 5,
      pending: 2,
      approved: 1,
      edited_and_approved: 0,
      rejected: 1,
      adoption_rate_pct: "50.00",
    },
  ],
};

const mockState = {
  storeScopes: [...MOCK_STORE_SCOPES],
  redlineRules: [...MOCK_REDLINE_RULES],
  kbGaps: [...MOCK_KB_GAPS],
  adoption: MOCK_ADOPTION_METRICS,
};

/** Restore the mock fixtures (tests only; mock mode only). */
export function resetTrack6Mocks(): void {
  mockState.storeScopes = [...MOCK_STORE_SCOPES];
  mockState.redlineRules = [...MOCK_REDLINE_RULES];
  mockState.kbGaps = [...MOCK_KB_GAPS];
  mockState.adoption = MOCK_ADOPTION_METRICS;
}

/** Pure evaluation used by the mock mode live tester (mirrors the backend). */
export function evaluateOutboundTextMock(content: string, rules: RedlineRuleRead[]): RedlineCheckResult {
  const blocks: string[] = [];
  const warnings: string[] = [];
  const matched: RedlineCheckResult["matched_rules"] = [];
  for (const rule of rules) {
    if (!rule.is_enabled) continue;
    let matchedText: string | null = null;
    if (rule.match_type === "keyword") {
      if (content.toLowerCase().includes(rule.pattern.toLowerCase())) matchedText = rule.pattern;
    } else {
      try {
        const found = new RegExp(rule.pattern, "i").exec(content);
        matchedText = found ? found[0].slice(0, 200) : null;
      } catch {
        matchedText = null;
      }
    }
    if (matchedText === null) continue;
    matched.push({
      id: rule.id,
      name: rule.name,
      match_type: rule.match_type,
      action: rule.action,
      matched_text: matchedText,
    });
    const bucket = rule.action === "block" ? blocks : warnings;
    if (!bucket.includes(matchedText)) bucket.push(matchedText);
  }
  return { blocked: blocks.length > 0, blocks, warnings, matched_rules: matched };
}

// ---------------------------------------------------------------------------
// Store scopes & offboarding handover
// ---------------------------------------------------------------------------

export async function fetchStoreScopes(userId?: string): Promise<StoreScopeRead[]> {
  if (shouldUseMockApi()) {
    return mockState.storeScopes.filter((scope) => !userId || scope.user_id === userId);
  }
  const query = userId ? `?user_id=${encodeURIComponent(userId)}` : "";
  const raw = await bffFetch<unknown>(`/v1/tenants/store-scopes${query}`);
  return asArray<StoreScopeRead>(raw);
}

export async function assignStoreScope(
  payload: StoreScopeAssignRequest
): Promise<StoreScopeRead> {
  if (shouldUseMockApi()) {
    const existing = mockState.storeScopes.find(
      (scope) =>
        scope.user_id === payload.user_id &&
        scope.channel_account_id === payload.channel_account_id
    );
    const level = payload.access_level ?? "operate";
    if (existing) {
      existing.access_level = level;
      return existing;
    }
    const created: StoreScopeRead = {
      id: `scope-mock-${mockState.storeScopes.length + 1}`,
      tenant_id: "t1",
      user_id: payload.user_id,
      user_email: null,
      user_full_name: null,
      channel_account_id: payload.channel_account_id,
      channel_type: null,
      channel_display_name: null,
      access_level: level,
      created_at: new Date().toISOString(),
    };
    mockState.storeScopes.push(created);
    return created;
  }
  return await bffFetch<StoreScopeRead>("/v1/tenants/store-scopes", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function revokeStoreScope(scopeId: string): Promise<void> {
  if (shouldUseMockApi()) {
    mockState.storeScopes = mockState.storeScopes.filter((scope) => scope.id !== scopeId);
    return;
  }
  await bffFetch<unknown>(`/v1/tenants/store-scopes/${encodeURIComponent(scopeId)}`, {
    method: "DELETE",
    parseJson: false,
  });
}

export async function handoverMemberAssets(
  payload: HandoverRequest
): Promise<HandoverResult> {
  if (shouldUseMockApi()) {
    const fromUserId = payload.from_user_id;
    const toUserId = payload.to_user_id;
    const grants = mockState.storeScopes.filter((scope) => scope.user_id === fromUserId);
    if (!payload.dry_run) {
      for (const grant of grants) grant.user_id = toUserId;
    }
    return {
      tenant_id: "t1",
      from_user_id: fromUserId,
      to_user_id: toUserId,
      dry_run: Boolean(payload.dry_run),
      opportunities_reassigned: 4,
      conversations_reassigned: 12,
      channel_accounts_reassigned: 2,
      store_scopes_reassigned: grants.length,
      total_reassigned: 18 + grants.length,
      from_user_deactivated: Boolean(payload.deactivate_from_user),
    };
  }
  return await bffFetch<HandoverResult>("/v1/tenants/handover", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

// ---------------------------------------------------------------------------
// Outbound red-line guard
// ---------------------------------------------------------------------------

export async function fetchRedlineRules(): Promise<RedlineRuleRead[]> {
  if (shouldUseMockApi()) return [...mockState.redlineRules];
  return asArray<RedlineRuleRead>(await bffFetch<unknown>("/v1/ai/redline-rules"));
}

export async function createRedlineRule(payload: RedlineRuleCreate): Promise<RedlineRuleRead> {
  if (shouldUseMockApi()) {
    const created: RedlineRuleRead = {
      id: `rule-mock-${mockState.redlineRules.length + 1}`,
      tenant_id: "t1",
      name: payload.name,
      pattern: payload.pattern,
      match_type: payload.match_type ?? "keyword",
      action: payload.action ?? "block",
      is_enabled: payload.is_enabled ?? true,
      created_at: new Date().toISOString(),
    };
    mockState.redlineRules.push(created);
    return created;
  }
  return await bffFetch<RedlineRuleRead>("/v1/ai/redline-rules", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function deleteRedlineRule(ruleId: string): Promise<void> {
  if (shouldUseMockApi()) {
    mockState.redlineRules = mockState.redlineRules.filter((rule) => rule.id !== ruleId);
    return;
  }
  await bffFetch<unknown>(`/v1/ai/redline-rules/${encodeURIComponent(ruleId)}`, {
    method: "DELETE",
    parseJson: false,
  });
}

export async function checkOutboundText(content: string): Promise<RedlineCheckResult> {
  if (shouldUseMockApi()) {
    return evaluateOutboundTextMock(content, mockState.redlineRules);
  }
  return normalizeRedlineCheck(
    await bffFetch<unknown>("/v1/ai/redline-rules/check", {
      method: "POST",
      body: JSON.stringify({ content }),
    })
  );
}

// ---------------------------------------------------------------------------
// Knowledge-base gap backlog
// ---------------------------------------------------------------------------

export async function fetchKbGaps(status?: KbGapRead["status"]): Promise<PaginatedResponseKbGapRead> {
  if (shouldUseMockApi()) {
    const items = status
      ? mockState.kbGaps.filter((gap) => gap.status === status)
      : [...mockState.kbGaps];
    return { items, total: items.length, limit: 50, offset: 0, has_more: false };
  }
  const query = status ? `?status=${encodeURIComponent(status)}` : "";
  return normalizeGapPage(await bffFetch<unknown>(`/v1/ai/kb-gaps${query}`));
}

export async function createKbGap(payload: KbGapCreateRequest): Promise<KbGapRead> {
  if (shouldUseMockApi()) {
    const question = payload.customer_question.trim();
    const existing = mockState.kbGaps.find(
      (gap) => gap.status === "open" && gap.customer_question === question
    );
    if (existing) {
      existing.frequency_count += 1;
      existing.updated_at = new Date().toISOString();
      return existing;
    }
    const created: KbGapRead = {
      id: `gap-mock-${mockState.kbGaps.length + 1}`,
      tenant_id: "t1",
      conversation_id: payload.conversation_id ?? null,
      customer_question: question,
      detected_language: payload.detected_language ?? "en",
      frequency_count: 1,
      status: "open",
      resolved_answer: null,
      promoted_kb_document_id: null,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    };
    mockState.kbGaps.push(created);
    return created;
  }
  return await bffFetch<KbGapRead>("/v1/ai/kb-gaps", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function promoteKbGap(
  gapId: string,
  resolvedAnswer: string
): Promise<KbGapPromoteResult> {
  if (shouldUseMockApi()) {
    const gap = mockState.kbGaps.find((item) => item.id === gapId);
    if (!gap) throw new Error("KB gap not found");
    gap.status = "resolved";
    gap.resolved_answer = resolvedAnswer;
    gap.promoted_kb_document_id = `kb-doc-mock-${gap.id}`;
    gap.updated_at = new Date().toISOString();
    return { gap, document_id: gap.promoted_kb_document_id, chunks_created: 1 };
  }
  return await bffFetch<KbGapPromoteResult>(
    `/v1/ai/kb-gaps/${encodeURIComponent(gapId)}/promote`,
    { method: "POST", body: JSON.stringify({ resolved_answer: resolvedAnswer }) }
  );
}

export async function dismissKbGap(gapId: string): Promise<KbGapRead> {
  if (shouldUseMockApi()) {
    const gap = mockState.kbGaps.find((item) => item.id === gapId);
    if (!gap) throw new Error("KB gap not found");
    gap.status = "dismissed";
    gap.updated_at = new Date().toISOString();
    return gap;
  }
  return await bffFetch<KbGapRead>(`/v1/ai/kb-gaps/${encodeURIComponent(gapId)}/dismiss`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

// ---------------------------------------------------------------------------
// AI adoption metrics
// ---------------------------------------------------------------------------

export async function fetchAiAdoptionMetrics(days = 30): Promise<AiAdoptionMetrics> {
  if (shouldUseMockApi()) return mockState.adoption;
  return normalizeAdoptionMetrics(
    await bffFetch<unknown>(`/v1/ai/adoption-metrics?days=${encodeURIComponent(String(days))}`)
  );
}
