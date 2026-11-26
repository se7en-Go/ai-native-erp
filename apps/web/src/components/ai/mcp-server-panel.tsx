"use client";

import React, { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  CheckCircle2,
  Copy,
  KeyRound,
  Loader2,
  Plug,
  Plus,
  ShieldOff,
  Trash2,
  Wrench,
} from "lucide-react";
import { Badge, Card, CardHeader, EmptyState } from "@/components/ui/page";
import { useAuthSession } from "@/components/providers";
import {
  MCP_SCOPE_OPTIONS,
  MCP_SCOPE_READ,
  catalogConfigSnippet,
  createMcpToken,
  fetchMcpToolCatalog,
  fetchMcpTokens,
  mcpAutonomyCopy,
  revokeMcpToken,
} from "@/lib/api-client/track4-mcp";
import type { McpTokenCreateRequest, McpTokenRead } from "@/lib/api-client/types.gen";
import { useI18n } from "@/lib/i18n";

const TOKENS_QUERY_KEY = ["ai", "mcp", "tokens"] as const;
const CATALOG_QUERY_KEY = ["ai", "mcp", "tools"] as const;

function tokenStatusCopy(token: McpTokenRead): { zh: string; en: string; className: string } {
  if (token.revoked_at) {
    return {
      zh: "已吊销",
      en: "Revoked",
      className:
        "rounded-full bg-slate-100 px-2 py-0.5 text-[10.5px] font-medium text-slate-500 dark:bg-slate-800 dark:text-slate-400",
    };
  }
  if (token.expires_at && new Date(token.expires_at).getTime() <= Date.now()) {
    return {
      zh: "已过期",
      en: "Expired",
      className:
        "rounded-full bg-amber-50 px-2 py-0.5 text-[10.5px] font-semibold text-amber-800 dark:bg-amber-950/40 dark:text-amber-300",
    };
  }
  return {
    zh: "有效",
    en: "Active",
    className:
      "rounded-full bg-emerald-50 px-2 py-0.5 text-[10.5px] font-semibold text-emerald-700 dark:bg-emerald-950/50 dark:text-emerald-300",
  };
}

/**
 * Track 4 operator console: mint/revoke tenant MCP tokens, inspect the exposed
 * tool catalog (L2 read / L1 draft / L0 blocked) and copy an MCP client config.
 *
 * The plaintext token is shown exactly once — the backend only persists its
 * SHA-256 digest and a display prefix.
 */
export function McpServerPanel() {
  const { bi } = useI18n();
  const { principal } = useAuthSession();
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<string[]>([MCP_SCOPE_READ]);
  const [allowedTools, setAllowedTools] = useState<string>("");
  const [expiryDays, setExpiryDays] = useState<string>("30");
  const [revealedToken, setRevealedToken] = useState<string | null>(null);
  const [copiedToken, setCopiedToken] = useState(false);
  const [copiedConfig, setCopiedConfig] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const canMint = principal?.role === "owner" || principal?.role === "admin";

  const tokensQuery = useQuery({ queryKey: TOKENS_QUERY_KEY, queryFn: () => fetchMcpTokens() });
  const catalogQuery = useQuery({
    queryKey: CATALOG_QUERY_KEY,
    queryFn: () => fetchMcpToolCatalog(),
  });

  const configSnippet = useMemo(
    () => catalogConfigSnippet(catalogQuery.data),
    [catalogQuery.data],
  );

  const createMutation = useMutation({
    mutationFn: (body: McpTokenCreateRequest) => createMcpToken(body),
    onSuccess: (result) => {
      setRevealedToken(result.plain_token);
      setCopiedToken(false);
      setName("");
      setFormError(null);
      void queryClient.invalidateQueries({ queryKey: TOKENS_QUERY_KEY });
    },
    onError: (error: Error) => setFormError(error.message),
  });

  const revokeMutation = useMutation({
    mutationFn: (tokenId: string) => revokeMcpToken(tokenId),
    onSuccess: () => {
      setFormError(null);
      void queryClient.invalidateQueries({ queryKey: TOKENS_QUERY_KEY });
    },
    onError: (error: Error) => setFormError(error.message),
  });

  const toggleScope = (scope: string) => {
    setScopes((current) =>
      current.includes(scope) ? current.filter((item) => item !== scope) : [...current, scope],
    );
  };

  const submitToken = () => {
    const cleanName = name.trim();
    if (!cleanName) {
      setFormError(bi("请先填写客户端名称。", "Give the client a name first."));
      return;
    }
    if (scopes.length === 0) {
      setFormError(bi("至少选择一个权限范围。", "Select at least one scope."));
      return;
    }
    const tools = allowedTools
      .split(",")
      .map((tool) => tool.trim())
      .filter(Boolean);
    const days = Number.parseInt(expiryDays, 10);
    createMutation.mutate({
      name: cleanName,
      scopes,
      allowed_tools: tools.length > 0 ? tools : null,
      expires_in_days: Number.isFinite(days) && days > 0 ? days : null,
    });
  };

  const copy = (value: string, mark: (flag: boolean) => void) => {
    if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
      void navigator.clipboard.writeText(value);
    }
    mark(true);
  };

  // Contract guards: a stubbed/partial BFF payload must never crash the console.
  const tokens = Array.isArray(tokensQuery.data) ? tokensQuery.data : [];
  const tools = Array.isArray(catalogQuery.data?.tools) ? catalogQuery.data.tools : [];
  const blockedTools = Array.isArray(catalogQuery.data?.blocked_tools)
    ? catalogQuery.data.blocked_tools
    : [];

  return (
    <Card data-testid="mcp-server-panel" className="rounded-2xl border border-slate-200/90">
      <CardHeader
        icon={Plug}
        title={bi("MCP 服务器（租户隔离 Streamable HTTP）", "MCP server (tenant-isolated Streamable HTTP)")}
        description={bi(
          "Claude Desktop / Cursor / OpenClaw 可以用授权码直连本租户 ERP：只读工具走确定性 SQL，提案工具只写 PENDING 草稿等待人工审核，资金与物流动作永久封禁。",
          "Claude Desktop / Cursor / OpenClaw can attach to this tenant's ERP with a token: read tools hit deterministic SQL, the draft tool only writes PENDING proposals for human review, and money/shipping actions stay blocked."
        )}
        actions={
          <Badge tone="info">
            <KeyRound className="h-3 w-3" />
            {tokens.length} {bi("个授权码", "tokens")}
          </Badge>
        }
      />

      {canMint ? (
        <div className="border-b border-slate-100 p-4 dark:border-slate-800">
          <div className="flex flex-wrap items-center gap-2">
            <input
              type="text"
              data-testid="mcp-token-name-input"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder={bi("例如：Claude Desktop 销售助手", "e.g. Claude Desktop sales copilot")}
              aria-label={bi("MCP 客户端名称", "MCP client name")}
              className="erp-input h-8 w-64 rounded-lg text-[12px]"
            />
            <input
              type="text"
              data-testid="mcp-token-allowed-tools"
              value={allowedTools}
              onChange={(event) => setAllowedTools(event.target.value)}
              placeholder={bi("可选：工具白名单，逗号分隔", "Optional: comma-separated tool whitelist")}
              aria-label={bi("工具白名单", "Tool whitelist")}
              className="erp-input h-8 w-72 rounded-lg text-[12px]"
            />
            <input
              type="number"
              min={1}
              max={365}
              data-testid="mcp-token-expiry-days"
              value={expiryDays}
              onChange={(event) => setExpiryDays(event.target.value)}
              aria-label={bi("有效天数", "Expiry days")}
              className="erp-input h-8 w-24 rounded-lg text-[12px]"
            />
            <button
              type="button"
              data-testid="mcp-token-create-btn"
              disabled={createMutation.isPending}
              onClick={submitToken}
              className="inline-flex h-8 items-center gap-1 rounded-lg bg-slate-900 px-3 text-[12px] font-semibold text-white transition hover:bg-slate-800 disabled:opacity-50 dark:bg-blue-600 dark:hover:bg-blue-500"
            >
              {createMutation.isPending ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Plus className="h-3.5 w-3.5" />
              )}
              {bi("签发授权码", "Issue token")}
            </button>
          </div>

          <div className="mt-2 flex flex-wrap gap-3">
            {MCP_SCOPE_OPTIONS.map((option) => (
              <label
                key={option.value}
                className="inline-flex items-center gap-1.5 text-[11.5px] font-medium text-slate-600 dark:text-slate-300"
                title={bi(option.hintZh, option.hintEn)}
              >
                <input
                  type="checkbox"
                  data-testid={`mcp-token-scope-${option.value}`}
                  checked={scopes.includes(option.value)}
                  onChange={() => toggleScope(option.value)}
                  className="h-3.5 w-3.5 rounded border-slate-300"
                />
                {bi(option.zh, option.en)}
              </label>
            ))}
          </div>

          {formError && (
            <p
              role="alert"
              data-testid="mcp-token-error"
              className="mt-2 rounded-lg border border-rose-200 bg-rose-50 px-3 py-1.5 text-[11.5px] font-medium text-rose-800 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-300"
            >
              {formError}
            </p>
          )}

          {revealedToken && (
            <div
              data-testid="mcp-token-revealed"
              className="mt-2.5 flex flex-wrap items-center justify-between gap-2 rounded-xl border border-emerald-200 bg-emerald-50 px-3 py-2 text-[12px] text-emerald-950 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-200"
            >
              <div className="min-w-0">
                <p className="font-semibold">
                  {bi(
                    "请立即复制（明文仅展示这一次）：",
                    "Copy now (shown exactly once):",
                  )}
                </p>
                <code className="break-all font-mono text-[11.5px]">{revealedToken}</code>
              </div>
              <button
                type="button"
                data-testid="mcp-token-copy-btn"
                onClick={() => copy(revealedToken, setCopiedToken)}
                className="inline-flex items-center gap-1 rounded-lg border border-emerald-300 bg-white px-2.5 py-1 text-[11.5px] font-semibold text-emerald-800 hover:bg-emerald-100 dark:border-emerald-700 dark:bg-slate-900 dark:text-emerald-200"
              >
                <Copy className="h-3.5 w-3.5" />
                {copiedToken ? bi("已复制", "Copied") : bi("复制", "Copy")}
              </button>
            </div>
          )}
        </div>
      ) : (
        <p
          data-testid="mcp-panel-readonly-note"
          className="border-b border-slate-100 px-4 py-2.5 text-[11.5px] font-medium text-amber-900 dark:border-slate-800 dark:text-amber-300"
        >
          {bi(
            "签发与吊销 MCP 授权码需要 owner / admin 权限；当前角色只能查看目录。",
            "Issuing and revoking MCP tokens needs owner / admin; this role can only review the catalog."
          )}
        </p>
      )}

      <div
        data-testid="mcp-token-list"
        className="divide-y divide-slate-100 dark:divide-slate-800"
      >
        {tokens.map((token) => {
          const status = tokenStatusCopy(token);
          return (
            <div
              key={token.id}
              data-testid={`mcp-token-row-${token.id}`}
              className="flex flex-wrap items-center justify-between gap-2 px-4 py-2.5 text-[12px]"
            >
              <div className="flex min-w-0 flex-wrap items-center gap-2">
                <span className="font-semibold text-slate-900 dark:text-slate-100">
                  {token.name}
                </span>
                <code className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-slate-700 dark:bg-slate-800 dark:text-slate-300">
                  {token.token_prefix}…
                </code>
                <span className={status.className}>{bi(status.zh, status.en)}</span>
                {token.scopes.map((scope) => (
                  <span
                    key={scope}
                    className="rounded-full border border-slate-200 px-2 py-0.5 text-[10.5px] font-medium text-slate-600 dark:border-slate-700 dark:text-slate-300"
                  >
                    {scope}
                  </span>
                ))}
                {token.allowed_tools && token.allowed_tools.length > 0 && (
                  <span className="text-[11px] text-slate-500 dark:text-slate-400">
                    {bi("白名单", "Whitelist")}: {token.allowed_tools.length}
                  </span>
                )}
                {token.last_used_at && (
                  <span className="text-[11px] text-slate-500 dark:text-slate-400">
                    {bi("最近使用", "Last used")}: {token.last_used_at.slice(0, 10)}
                  </span>
                )}
              </div>
              {canMint && !token.revoked_at && (
                <button
                  type="button"
                  data-testid={`mcp-token-revoke-${token.id}`}
                  disabled={revokeMutation.isPending}
                  onClick={() => revokeMutation.mutate(token.id)}
                  className="inline-flex items-center gap-1 rounded-lg border border-rose-200 px-2.5 py-1 text-[11px] font-semibold text-rose-700 hover:bg-rose-50 disabled:opacity-50 dark:border-rose-500/30 dark:text-rose-300 dark:hover:bg-rose-500/10"
                >
                  <Trash2 className="h-3 w-3" />
                  {bi("吊销", "Revoke")}
                </button>
              )}
            </div>
          );
        })}
        {tokens.length === 0 && (
          <EmptyState testId="mcp-token-empty" className="m-4">
            {bi(
              "还没有 MCP 授权码；签发一个即可让 Claude Desktop 接入。",
              "No MCP token yet — issue one to attach Claude Desktop."
            )}
          </EmptyState>
        )}
      </div>

      <div className="border-t border-slate-100 p-4 dark:border-slate-800">
        <h3 className="inline-flex items-center gap-1.5 text-[13px] font-bold text-slate-950 dark:text-slate-100">
          <Wrench className="h-4 w-4 text-blue-600 dark:text-blue-400" />
          {bi("已暴露的 MCP 工具", "Exposed MCP tools")}
        </h3>
        <div data-testid="mcp-tool-catalog" className="mt-2 grid gap-2 md:grid-cols-2">
          {tools.map((tool) => {
            const autonomy = mcpAutonomyCopy(tool.autonomy_level);
            return (
              <div
                key={tool.name}
                data-testid={`mcp-tool-row-${tool.name}`}
                className="rounded-xl border border-slate-200/90 p-3 dark:border-slate-800"
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <code className="font-mono text-[12px] font-semibold text-slate-900 dark:text-slate-100">
                    {tool.name}
                  </code>
                  <span
                    className={`inline-flex items-center whitespace-nowrap rounded-full border px-2 py-0.5 text-[10.5px] font-semibold ${autonomy.tone}`}
                  >
                    {bi(autonomy.zh, autonomy.en)}
                  </span>
                </div>
                <p className="mt-1 text-[11.5px] leading-5 text-slate-500 dark:text-slate-400">
                  {tool.description}
                </p>
                <p className="mt-1 font-mono text-[10.5px] text-slate-400">{tool.required_scope}</p>
              </div>
            );
          })}
        </div>

        {blockedTools.length > 0 && (
          <div
            data-testid="mcp-tool-blocked"
            className="mt-3 rounded-xl border border-rose-200 bg-rose-50/70 p-3 dark:border-rose-500/30 dark:bg-rose-500/10"
          >
            <p className="inline-flex items-center gap-1.5 text-[12px] font-bold text-rose-800 dark:text-rose-200">
              <ShieldOff className="h-3.5 w-3.5" />
              {bi("L0 永久封禁（MCP 永不暴露）", "L0 permanently blocked (never exposed over MCP)")}
            </p>
            <div className="mt-1.5 flex flex-wrap gap-1.5">
              {blockedTools.map((tool) => (
                <span
                  key={tool.name}
                  data-testid={`mcp-tool-blocked-${tool.name}`}
                  title={tool.reason}
                  className="rounded-full border border-rose-300 bg-white px-2 py-0.5 font-mono text-[10.5px] font-semibold text-rose-700 dark:border-rose-500/40 dark:bg-slate-900 dark:text-rose-300"
                >
                  {tool.name}
                </span>
              ))}
            </div>
          </div>
        )}

        {configSnippet && (
          <div className="mt-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="inline-flex items-center gap-1.5 text-[12px] font-semibold text-slate-800 dark:text-slate-200">
                <CheckCircle2 className="h-3.5 w-3.5 text-emerald-600" />
                {bi(
                  "Claude Desktop / Cursor / OpenClaw 配置",
                  "Claude Desktop / Cursor / OpenClaw config"
                )}
              </p>
              <button
                type="button"
                data-testid="mcp-config-copy-btn"
                onClick={() => copy(configSnippet, setCopiedConfig)}
                className="inline-flex items-center gap-1 rounded-lg border border-slate-200 px-2.5 py-1 text-[11px] font-semibold text-slate-700 hover:bg-slate-50 dark:border-slate-700 dark:text-slate-200 dark:hover:bg-slate-800"
              >
                <Copy className="h-3 w-3" />
                {copiedConfig ? bi("已复制", "Copied") : bi("复制 JSON", "Copy JSON")}
              </button>
            </div>
            <pre
              data-testid="mcp-config-snippet"
              className="mt-1.5 overflow-x-auto rounded-xl bg-[#0B1220] p-3 font-mono text-[11px] leading-5 text-slate-200"
            >
              {configSnippet}
            </pre>
            <p className="mt-1 flex items-start gap-1.5 text-[11px] text-slate-500 dark:text-slate-400">
              <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0 text-amber-500" />
              {bi(
                "把 erp_mcp_<your-tenant-token> 换成刚签发的明文授权码；服务端只存 SHA-256 摘要，丢失需重新签发。",
                "Replace erp_mcp_<your-tenant-token> with the plaintext token you just issued; the server keeps only a SHA-256 digest, so a lost token must be reissued."
              )}
            </p>
          </div>
        )}
      </div>
    </Card>
  );
}
