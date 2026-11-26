// Track 4: tenant-isolated MCP server API module.
//
// Everything goes through the same-origin `bffFetch` (`/api/bff` → FastAPI
// `/api/v1/*`), so the browser never talks to the MCP endpoint directly:
//   * `GET    /v1/ai/mcp/tokens`  — issued tokens (hash prefix only, never plaintext)
//   * `POST   /v1/ai/mcp/tokens`  — mint a token, plaintext revealed exactly once
//   * `DELETE /v1/ai/mcp/tokens/{id}` — revoke
//   * `GET    /v1/ai/mcp/tools`   — exposed L2/L1 tool catalog + MCP client config
//
// Contract types come from `types.gen.ts` (regenerated from the backend OpenAPI
// document); mock mode returns a local mirror so the panel renders with zero backend.
import { bffFetch } from "@/lib/api";
import { shouldUseMockApi } from "@/lib/data-source";
import type {
  McpBlockedTool,
  McpExposedTool,
  McpTokenCreateRequest,
  McpTokenCreateResponse,
  McpTokenRead,
  McpToolCatalogResponse,
} from "@/lib/api-client/types.gen";

/** The only two scopes the backend accepts (`mcp_token_service.MCP_VALID_SCOPES`). */
export const MCP_SCOPE_READ = "mcp:read";
export const MCP_SCOPE_PROPOSE = "mcp:propose";

export const MCP_SCOPE_OPTIONS: ReadonlyArray<{
  value: string;
  zh: string;
  en: string;
  hintZh: string;
  hintEn: string;
}> = [
  {
    value: MCP_SCOPE_READ,
    zh: "mcp:read · 只读",
    en: "mcp:read · read-only",
    hintZh: "调用确定性 SQL 工具读取实时价格、库存、订单、物流、应收与分析。",
    hintEn: "Deterministic SQL reads for live price, stock, orders, tracking, receivables.",
  },
  {
    value: MCP_SCOPE_PROPOSE,
    zh: "mcp:propose · 仅提案",
    en: "mcp:propose · draft only",
    hintZh: "写入一条 PENDING 提案等待人工审核，永不执行任何资金或物流动作。",
    hintEn: "Writes a PENDING proposal for human review; never executes money or shipping.",
  },
];

/** Every tool name the MCP token whitelist may contain. */
export const MCP_TOOL_NAMES: ReadonlyArray<string> = [
  "get_live_sku_pricing_and_stock",
  "get_live_order_tracking",
  "calc_b2b_quote_matrix",
  "match_open_receivables",
  "get_live_channel_sales_analytics",
  "query_readonly_analytics_sql",
  "create_l1_proposal_draft",
];

/** Autonomy badge copy; L0 never appears in the exposed catalog (it is blocked). */
export function mcpAutonomyCopy(level: string): {
  zh: string;
  en: string;
  tone: string;
} {
  switch (level) {
    case "L2_auto":
      return {
        zh: "L2 只读自动",
        en: "L2 auto read",
        tone: "border-emerald-300 bg-emerald-50 text-emerald-800 dark:border-emerald-700/60 dark:bg-emerald-950/40 dark:text-emerald-300",
      };
    case "L1_proposal":
      return {
        zh: "L1 提案人审",
        en: "L1 human review",
        tone: "border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-700/60 dark:bg-amber-950/40 dark:text-amber-200",
      };
    default:
      return {
        zh: "L0 永久封禁",
        en: "L0 blocked",
        tone: "border-rose-300 bg-rose-50 text-rose-700 dark:border-rose-700/60 dark:bg-rose-950/40 dark:text-rose-300",
      };
  }
}

/** `mcpServers` snippet for Claude Desktop / Cursor / OpenClaw clients. */
export function mcpServersSnippet(endpoint: string): string {
  return JSON.stringify(
    {
      mcpServers: {
        "ai-native-erp-mcp": {
          type: "http",
          url: endpoint,
          headers: {
            Authorization: "Bearer erp_mcp_<your-tenant-token>",
          },
        },
      },
    },
    null,
    2,
  );
}

const CATALOG_ENDPOINT = "/v1/ai/mcp/tools";

/** Mock mirror of `mcp_tools.MCP_TOOL_SPECS` (mock mode only). */
const MOCK_TOOLS: McpExposedTool[] = [
  {
    name: "get_live_sku_pricing_and_stock",
    required_scope: MCP_SCOPE_READ,
    autonomy_level: "L2_auto",
    description:
      "Deterministic live SKU price, MOQ tier, Incoterm surcharge and available stock.",
    input_schema: { type: "object", required: ["sku"] },
  },
  {
    name: "get_live_order_tracking",
    required_scope: MCP_SCOPE_READ,
    autonomy_level: "L2_auto",
    description: "Live order status, payment status and 17TRACK shipment events.",
    input_schema: { type: "object", required: ["order_no"] },
  },
  {
    name: "calc_b2b_quote_matrix",
    required_scope: MCP_SCOPE_READ,
    autonomy_level: "L2_auto",
    description: "Decimal-exact B2B quote matrix with MOQ tiers and margin-floor flags.",
    input_schema: { type: "object", required: ["items", "currency"] },
  },
  {
    name: "match_open_receivables",
    required_scope: MCP_SCOPE_READ,
    autonomy_level: "L2_auto",
    description: "Suggest open receivable milestones matching a received amount.",
    input_schema: { type: "object", required: ["amount", "currency"] },
  },
  {
    name: "get_live_channel_sales_analytics",
    required_scope: MCP_SCOPE_READ,
    autonomy_level: "L2_auto",
    description: "Cross-channel GMV, refunds, commission, ad spend, margin and ROAS.",
    input_schema: { type: "object" },
  },
  {
    name: "query_readonly_analytics_sql",
    required_scope: MCP_SCOPE_READ,
    autonomy_level: "L2_auto",
    description: "Bounded read-only SELECT over the approved analytics views.",
    input_schema: { type: "object", required: ["sql"] },
  },
  {
    name: "create_l1_proposal_draft",
    required_scope: MCP_SCOPE_PROPOSE,
    autonomy_level: "L1_proposal",
    description: "Insert a PENDING AI proposal that a human must review.",
    input_schema: { type: "object", required: ["capability", "proposal_type", "proposed_payload"] },
  },
];

/** Mock mirror of `mcp_tools.BLOCKED_L0_TOOLS` (never reachable over MCP). */
const MOCK_BLOCKED_TOOLS: McpBlockedTool[] = [
  {
    name: "settle_receivable",
    description: "Cash settlement of a receivable or payable milestone.",
    reason: "Financial settlement is L0 human-only (no MCP surface).",
  },
  {
    name: "record_payment_receipt",
    description: "Confirm an inbound payment against an invoice.",
    reason: "Money movement is L0 human-only (no MCP surface).",
  },
  {
    name: "approve_quote",
    description: "Approve and send a B2B quote or proforma invoice.",
    reason: "Commercial commitments stay with a human reviewer.",
  },
  {
    name: "create_shipment_label",
    description: "Buy a carrier label and register the tracking number.",
    reason: "Shipping spend is L0 human-only (no MCP surface).",
  },
  {
    name: "adjust_inventory",
    description: "Adjust on-hand inventory or release reservations.",
    reason: "Stock mutation is L0 human-only (no MCP surface).",
  },
  {
    name: "publish_listing",
    description: "Push a listing or price change to a sales channel.",
    reason: "External publishing is L0 human-only (no MCP surface).",
  },
];

let mockMcpTokens: McpTokenRead[] = [
  {
    id: "mcp-tok-mock-1",
    name: "Claude Desktop",
    token_prefix: "erp_mcp_demo12",
    scopes: [MCP_SCOPE_READ],
    allowed_tools: null,
    last_used_at: null,
    expires_at: null,
    revoked_at: null,
    created_at: "2026-11-26T08:00:00Z",
  },
];

/** Mock-mode token snapshot (read-only, for assertions). */
export function mockMcpTokensSnapshot(): McpTokenRead[] {
  return mockMcpTokens.map((token) => ({ ...token }));
}

/** GET /v1/ai/mcp/tokens — issued tokens for the current tenant. */
export async function fetchMcpTokens(): Promise<McpTokenRead[]> {
  if (shouldUseMockApi()) {
    return mockMcpTokens.map((token) => ({ ...token }));
  }
  return bffFetch<McpTokenRead[]>("/v1/ai/mcp/tokens", { method: "GET" });
}

/** POST /v1/ai/mcp/tokens — mint a token; `plain_token` is returned exactly once. */
export async function createMcpToken(
  body: McpTokenCreateRequest,
): Promise<McpTokenCreateResponse> {
  if (shouldUseMockApi()) {
    const suffix = String(mockMcpTokens.length + 1).padStart(2, "0");
    const plain = `erp_mcp_mock_${suffix}_${Math.random().toString(36).slice(2, 10)}`;
    const expiresAt =
      typeof body.expires_in_days === "number" && body.expires_in_days > 0
        ? new Date(Date.now() + body.expires_in_days * 86_400_000).toISOString()
        : null;
    const token: McpTokenRead = {
      id: `mcp-tok-mock-${suffix}`,
      name: body.name.trim() || `MCP client ${suffix}`,
      token_prefix: plain.slice(0, 16),
      scopes: body.scopes && body.scopes.length > 0 ? [...body.scopes] : [MCP_SCOPE_READ],
      allowed_tools: body.allowed_tools && body.allowed_tools.length > 0 ? [...body.allowed_tools] : null,
      last_used_at: null,
      expires_at: expiresAt,
      revoked_at: null,
      created_at: new Date().toISOString(),
    };
    mockMcpTokens = [token, ...mockMcpTokens];
    return { token, plain_token: plain };
  }
  return bffFetch<McpTokenCreateResponse>("/v1/ai/mcp/tokens", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** DELETE /v1/ai/mcp/tokens/{token_id} — revoke immediately (hash stays for audit). */
export async function revokeMcpToken(tokenId: string): Promise<McpTokenRead> {
  if (shouldUseMockApi()) {
    const now = new Date().toISOString();
    let updated: McpTokenRead | undefined;
    mockMcpTokens = mockMcpTokens.map((token) => {
      if (token.id !== tokenId) return token;
      updated = { ...token, revoked_at: token.revoked_at ?? now };
      return updated;
    });
    return (
      updated ?? {
        id: tokenId,
        name: "Revoked",
        token_prefix: "erp_mcp_mock",
        scopes: [MCP_SCOPE_READ],
        allowed_tools: null,
        last_used_at: null,
        expires_at: null,
        revoked_at: now,
        created_at: now,
      }
    );
  }
  return bffFetch<McpTokenRead>(`/v1/ai/mcp/tokens/${encodeURIComponent(tokenId)}`, {
    method: "DELETE",
  });
}

/** GET /v1/ai/mcp/tools — exposed tool catalog plus a copyable MCP client config. */
export async function fetchMcpToolCatalog(): Promise<McpToolCatalogResponse> {
  if (shouldUseMockApi()) {
    const endpoint =
      typeof window !== "undefined" && window.location?.origin
        ? `${window.location.origin}/api/v1/mcp`
        : "http://localhost:8000/api/v1/mcp";
    return {
      protocol_version: "2025-03-26",
      server: { name: "ai-native-erp-mcp", version: "1.0.0" },
      tools: MOCK_TOOLS.map((tool) => ({ ...tool })),
      blocked_tools: MOCK_BLOCKED_TOOLS.map((tool) => ({ ...tool })),
      config_snippet: JSON.parse(mcpServersSnippet(endpoint)) as {
        [key: string]: unknown;
      },
    };
  }
  return bffFetch<McpToolCatalogResponse>(CATALOG_ENDPOINT, { method: "GET" });
}

/** Render the backend `config_snippet` as pretty JSON (fallback to a local template). */
export function catalogConfigSnippet(catalog: McpToolCatalogResponse | undefined): string {
  if (!catalog) return "";
  const servers = catalog.config_snippet?.mcpServers;
  if (servers) return JSON.stringify({ mcpServers: servers }, null, 2);
  const endpoint =
    typeof window !== "undefined" && window.location?.origin
      ? `${window.location.origin}/api/v1/mcp`
      : "http://localhost:8000/api/v1/mcp";
  return mcpServersSnippet(endpoint);
}
