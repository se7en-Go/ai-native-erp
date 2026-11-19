// Phase 3B live adapters over the generated @hey-api/openapi-ts SDK.
// Every request goes through the shared BFF-configured client (@/lib/api), so
// session cookies and the 401 refresh flow behave exactly like bffFetch.
import { getBffClient } from "@/lib/api";
import { unwrap } from "@/lib/api-client/live-client";
import type {
  AutonomyDivisionItem,
  DivisionDestination,
  DigitalEmployee,
  WorkforceMetrics,
} from "@/components/ai-workforce/types";
import type {
  AiEmployeeRead,
  AiEmployeeUpdate,
  CreativeAssetCreate,
  CreativeAssetRead,
  CreativeAssetStatus,
  CreativeAssetUpdate,
  InboxAssistRequest,
  InboxAssistResponse,
  QuickReplyCreate,
  QuickReplyRead,
  WorkforceKpis,
  WorkforceMatrixResponse,
} from "@/lib/api-client/types.gen";
import {
  createAssetApiV1AiStudioAssetsPost,
  createQuickReplyApiV1AiQuickRepliesPost,
  deleteQuickReplyApiV1AiQuickRepliesTemplateIdDelete,
  inboxAssistApiV1AiInboxAssistPost,
  listAssetsApiV1AiStudioAssetsGet,
  listEmployeesApiV1AiEmployeesGet,
  listQuickRepliesApiV1AiQuickRepliesGet,
  updateAssetApiV1AiStudioAssetsAssetIdPatch,
  updateEmployeeApiV1AiEmployeesEmployeeIdPatch,
  workforceMatrixApiV1AiEmployeesMatrixGet,
} from "@/lib/api-client/sdk.gen";

const client = () => getBffClient();

export interface DigitalEmployeeWithActivity extends DigitalEmployee {
  actions_7d: number;
  tasks_7d: number;
  system_persona?: string | null;
}

/** The digital-employee contract row, flattened onto the UI card shape. */
export function mapEmployee(employee: AiEmployeeRead): DigitalEmployeeWithActivity {
  return {
    id: employee.id,
    display_name: employee.display_name,
    role_title: employee.role_title,
    department: employee.department,
    avatar_emoji: employee.avatar_emoji,
    description: employee.description,
    capability_codes: employee.capability_codes,
    autonomy_level: employee.autonomy_level,
    is_enabled: employee.is_enabled,
    tool_count: employee.tool_count,
    system_persona: employee.system_persona,
    actions_7d: employee.actions_7d ?? 0,
    tasks_7d: employee.tasks_7d ?? 0,
  };
}

const AUTONOMY_ORDER = ["L0_copilot", "L1_proposal", "L2_auto"] as const;

/** The workforce badge shows the level most employees actually run at. */
export function mapMetrics(kpis: WorkforceKpis): WorkforceMetrics {
  const summary = kpis.autonomy_summary;
  const counts: Array<[WorkforceMetrics["autonomyMode"], number]> = [
    ["L2_auto", summary.l2_auto],
    ["L1_proposal", summary.l1_proposal],
    ["L0_copilot", summary.l0_copilot],
  ];
  const [mode] = counts.reduce((best, candidate) => {
    if (candidate[1] !== best[1]) return candidate[1] > best[1] ? candidate : best;
    // Ties fall back to the more conservative level.
    return AUTONOMY_ORDER.indexOf(candidate[0]) < AUTONOMY_ORDER.indexOf(best[0])
      ? candidate
      : best;
  });
  return {
    totalEmployees: kpis.total_employees,
    enabledEmployees: kpis.active_employees,
    actionsLast7Days: kpis.actions_7d,
    // Decimal money stays a string end to end; Number() would lose cents.
    llmCostTodayUsd: typeof kpis.today_llm_cost_usd === "string" ? kpis.today_llm_cost_usd : null,
    autonomyMode: mode,
  };
}

const DESTINATION_KIND: Record<string, DivisionDestination["kind"]> = {
  L2_auto: "activity",
  L1_proposal: "proposal",
  L0_copilot: "policy",
};

/** Real matrix rows mapped onto the three-column governance board. */
export function mapDivisionItems(matrix: WorkforceMatrixResponse): AutonomyDivisionItem[] {
  return [
    ...matrix.l2_auto.map((row) => ({
      id: `l2-${row.capability}`,
      level: "L2_auto" as const,
      title: row.display_name,
      description: `近 7 天自动执行 ${row.auto_executed_7d} 次`,
      count: row.auto_executed_7d,
      target: { kind: DESTINATION_KIND.L2_auto, id: row.capability },
    })),
    ...matrix.l1_pending.map((row) => ({
      id: `l1-${row.proposal_type}`,
      level: "L1_proposal" as const,
      title: row.label,
      description: `${row.proposal_type} · 待人工审核`,
      count: row.count,
      target: { kind: DESTINATION_KIND.L1_proposal, id: row.proposal_type },
    })),
    ...matrix.l0_locked.map((row) => ({
      id: `l0-${row.code}`,
      level: "L0_copilot" as const,
      title: row.title,
      description: row.description,
      target: { kind: DESTINATION_KIND.L0_copilot, id: row.code },
    })),
  ];
}

/** Where a matrix tile navigates; every destination is a real, RBAC-guarded page. */
export function destinationHref(target: DivisionDestination): string {
  if (target.kind === "policy") return "/ai/capabilities";
  return "/ai/proposals";
}

export function listEmployees(): Promise<DigitalEmployeeWithActivity[]> {
  return listEmployeesApiV1AiEmployeesGet({ client: client(), query: { limit: 100 } }).then(
    (r) => unwrap(r).items.map(mapEmployee)
  );
}

export function setEmployeeEnabled(
  employeeId: string,
  isEnabled: boolean
): Promise<DigitalEmployeeWithActivity> {
  const body: AiEmployeeUpdate = { is_enabled: isEnabled };
  return updateEmployeeApiV1AiEmployeesEmployeeIdPatch({
    client: client(),
    path: { employee_id: employeeId },
    body,
  }).then((r) => mapEmployee(unwrap(r)));
}

export function fetchWorkforceMatrix(): Promise<WorkforceMatrixResponse> {
  return workforceMatrixApiV1AiEmployeesMatrixGet({ client: client() }).then((r) =>
    unwrap(r) as WorkforceMatrixResponse
  );
}

export function assistInbox(body: InboxAssistRequest): Promise<InboxAssistResponse> {
  return inboxAssistApiV1AiInboxAssistPost({ client: client(), body }).then(
    (r) => unwrap(r) as InboxAssistResponse
  );
}

export function listQuickReplies(): Promise<QuickReplyRead[]> {
  return listQuickRepliesApiV1AiQuickRepliesGet({ client: client(), query: { limit: 100 } }).then(
    (r) => unwrap(r).items
  );
}

export function createQuickReply(body: QuickReplyCreate): Promise<QuickReplyRead> {
  return createQuickReplyApiV1AiQuickRepliesPost({ client: client(), body }).then(
    (r) => unwrap(r) as QuickReplyRead
  );
}

export function deleteQuickReply(templateId: string): Promise<void> {
  return deleteQuickReplyApiV1AiQuickRepliesTemplateIdDelete({
    client: client(),
    path: { template_id: templateId },
  }).then((r) => {
    unwrap(r);
    return undefined;
  });
}

export function listAssets(): Promise<CreativeAssetRead[]> {
  return listAssetsApiV1AiStudioAssetsGet({ client: client(), query: { limit: 100 } }).then(
    (r) => unwrap(r).items
  );
}

export function createAsset(body: CreativeAssetCreate): Promise<CreativeAssetRead> {
  return createAssetApiV1AiStudioAssetsPost({ client: client(), body }).then(
    (r) => unwrap(r) as CreativeAssetRead
  );
}

export function updateAsset(
  assetId: string,
  status: CreativeAssetStatus
): Promise<CreativeAssetRead> {
  const body: CreativeAssetUpdate = { status };
  return updateAssetApiV1AiStudioAssetsAssetIdPatch({
    client: client(),
    path: { asset_id: assetId },
    body,
  }).then((r) => unwrap(r) as CreativeAssetRead);
}

export type {
  CreativeAssetCreate,
  CreativeAssetRead,
  InboxAssistResponse,
  QuickReplyRead,
  WorkforceMatrixResponse,
};
