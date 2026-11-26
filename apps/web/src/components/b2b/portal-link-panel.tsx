"use client";

import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Check, Copy, Link2, Loader2, ShieldOff } from "lucide-react";

import { Badge, Card, CardHeader, EmptyState } from "@/components/ui/page";
import {
  createQuotePortalLink,
  fetchQuotePortalLinks,
  revokeQuotePortalLink,
  type PortalLink,
} from "@/lib/api-client/track2-portal";
import { BffApiError } from "@/lib/contracts";
import { useI18n } from "@/lib/i18n";

/**
 * Operator-side magic-link control panel for the B2B buyer portal.
 *
 * Mentors a quote into a one-time `/portal/<token>` URL, shows the active/revoked
 * links for that quote, and revokes on demand. The plaintext token is only ever
 * shown once (right after creation) — the API persists a SHA-256 hash only.
 */

export interface PortalQuoteOption {
  id: string;
  quote_no: string;
  version: number;
  status: string;
}

export interface PortalLinkPanelProps {
  quotes: PortalQuoteOption[];
  initialQuoteId?: string | null;
}

function errorDetail(error: unknown): string {
  if (error instanceof BffApiError) return error.detail || error.code;
  if (error instanceof Error) return error.message;
  return "Unexpected error";
}

function absolutePortalUrl(portalPath: string): string {
  if (typeof window === "undefined" || !window.location?.origin) return portalPath;
  return `${window.location.origin}${portalPath}`;
}

function formatTimestamp(raw: string | null | undefined): string {
  if (!raw) return "—";
  const parsed = new Date(raw);
  return Number.isNaN(parsed.getTime()) ? "—" : parsed.toLocaleString();
}

export function PortalLinkPanel({ quotes, initialQuoteId }: PortalLinkPanelProps) {
  const { bi } = useI18n();
  const defaultQuoteId = initialQuoteId ?? quotes[0]?.id ?? "";
  const [selectedQuoteId, setSelectedQuoteId] = useState<string>(defaultQuoteId);
  const [links, setLinks] = useState<PortalLink[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [revokingId, setRevokingId] = useState<string | null>(null);
  const [freshLink, setFreshLink] = useState<{ token: string; path: string } | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!selectedQuoteId && defaultQuoteId) setSelectedQuoteId(defaultQuoteId);
  }, [defaultQuoteId, selectedQuoteId]);

  const loadLinks = useCallback(async (quoteId: string) => {
    if (!quoteId) {
      setLinks([]);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const payload = await fetchQuotePortalLinks(quoteId);
      // Defensive: a proxy/contract drift must never take the whole quote page
      // down — an unexpected body degrades to the empty state instead.
      setLinks(Array.isArray(payload) ? payload : []);
    } catch (cause) {
      setError(errorDetail(cause));
      setLinks([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadLinks(selectedQuoteId);
  }, [loadLinks, selectedQuoteId]);

  const onGenerate = useCallback(async () => {
    if (!selectedQuoteId) return;
    setCreating(true);
    setError(null);
    setCopied(false);
    try {
      const created = await createQuotePortalLink(selectedQuoteId, {});
      if (!created?.link || !created.portal_path) {
        setError(bi("门户链接创建失败。", "Could not create the buyer portal link."));
        return;
      }
      setFreshLink({ token: created.plain_token, path: created.portal_path });
      setLinks((current) => [created.link, ...current]);
    } catch (cause) {
      setError(errorDetail(cause));
    } finally {
      setCreating(false);
    }
  }, [bi, selectedQuoteId]);

  const onCopy = useCallback(async () => {
    if (!freshLink) return;
    const url = absolutePortalUrl(freshLink.path);
    try {
      await navigator.clipboard?.writeText(url);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }, [freshLink]);

  const onRevoke = useCallback(
    async (tokenId: string) => {
      setRevokingId(tokenId);
      setError(null);
      try {
        const revoked = await revokeQuotePortalLink(tokenId);
        setLinks((current) =>
          current.map((link) => (link.id === revoked.id ? revoked : link))
        );
        setFreshLink((current) => (current && revoked.is_active ? current : null));
      } catch (cause) {
        setError(errorDetail(cause));
      } finally {
        setRevokingId(null);
      }
    },
    []
  );

  const activeCount = useMemo(() => links.filter((link) => link.is_active).length, [links]);

  return (
    <Card data-testid="portal-link-panel" className="p-5">
      <CardHeader
        icon={Link2}
        title={bi("买家自助门户链接", "Buyer portal magic links")}
        description={bi(
          "为一张已发送的 PI 生成一次性买家自助链接：买家无需登录即可查看报价、确认 PI、上传水单并下载 PI/CI/PL。",
          "Mint a one-time self-service link for a sent PI: the buyer can review the quote, accept it, upload a T/T slip and download PI/CI/PL without an ERP login."
        )}
        actions={
          <button
            type="button"
            data-testid="portal-generate-button"
            onClick={() => void onGenerate()}
            disabled={!selectedQuoteId || creating}
            className="inline-flex items-center gap-1.5 rounded-xl bg-[#18181B] px-3.5 py-2 text-[12.5px] font-semibold text-[#FAFAF9] transition-colors hover:bg-[#065F46] disabled:opacity-50"
          >
            {creating ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
            ) : (
              <Link2 className="h-3.5 w-3.5" aria-hidden="true" />
            )}
            {bi("生成 Magic Link", "Generate magic link")}
          </button>
        }
      />

      <div className="mt-3 flex flex-wrap items-end gap-3">
        <label className="text-[12px] font-medium text-gray-600 dark:text-gray-400">
          {bi("选择报价单", "Select quote")}
          <select
            data-testid="portal-quote-select"
            value={selectedQuoteId}
            onChange={(event) => {
              setSelectedQuoteId(event.target.value);
              setFreshLink(null);
              setCopied(false);
            }}
            className="mt-1 block min-w-[240px] rounded-xl border border-gray-200 bg-white px-3 py-2 text-[13px] text-gray-900 outline-none transition-colors focus:border-blue-500 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-100"
          >
            <option value="">{bi("请选择报价单", "Choose a quote")}</option>
            {quotes.map((quote) => (
              <option key={quote.id} value={quote.id}>
                {quote.quote_no} · v{quote.version} · {quote.status}
              </option>
            ))}
          </select>
        </label>
        <span className="pb-2 text-[11.5px] text-gray-500 dark:text-gray-400">
          {bi(`生效中链接 ${activeCount} 个`, `${activeCount} active link(s)`)}
        </span>
      </div>

      {freshLink && (
        <div
          data-testid="portal-fresh-link"
          className="mt-3 rounded-xl border border-blue-200 bg-blue-50/70 p-3 dark:border-blue-500/30 dark:bg-blue-500/10"
        >
          <p className="text-[12px] font-semibold text-blue-800 dark:text-blue-300">
            {bi(
              "链接仅显示一次，请立即复制并发送给买家",
              "This link is shown once — copy it now and send it to the buyer"
            )}
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <code
              data-testid="portal-fresh-link-url"
              className="min-w-0 flex-1 overflow-x-auto rounded-lg bg-white px-2.5 py-1.5 font-mono text-[11.5px] text-gray-800 dark:bg-gray-900 dark:text-gray-200"
            >
              {absolutePortalUrl(freshLink.path)}
            </code>
            <button
              type="button"
              data-testid="portal-copy-button"
              onClick={() => void onCopy()}
              className="inline-flex items-center gap-1.5 rounded-lg border border-blue-300 bg-white px-2.5 py-1.5 text-[11.5px] font-medium text-blue-700"
            >
              {copied ? (
                <Check className="h-3.5 w-3.5" aria-hidden="true" />
              ) : (
                <Copy className="h-3.5 w-3.5" aria-hidden="true" />
              )}
              {copied ? bi("已复制", "Copied") : bi("复制", "Copy")}
            </button>
          </div>
        </div>
      )}

      {error && (
        <p data-testid="portal-link-error" className="mt-3 text-[12px] text-rose-600">
          {error}
        </p>
      )}

      <div className="mt-3">
        {loading ? (
          <p className="flex items-center gap-2 rounded-xl border border-dashed border-gray-200 px-3 py-6 text-[12.5px] text-gray-500 dark:border-gray-700 dark:text-gray-400">
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
            {bi("正在加载链接…", "Loading links…")}
          </p>
        ) : links.length === 0 ? (
          <EmptyState testId="portal-link-empty">
            {bi(
              "该报价单还没有生成买家自助链接。",
              "No buyer portal link has been generated for this quote yet."
            )}
          </EmptyState>
        ) : (
          <ul data-testid="portal-link-list" className="space-y-2">
            {links.map((link) => (
              <li
                key={link.id}
                className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-gray-200 bg-gray-50/70 px-3 py-2 dark:border-gray-800 dark:bg-gray-800/40"
              >
                <div className="min-w-0">
                  <p className="font-mono text-[11.5px] font-medium text-gray-800 dark:text-gray-200">
                    {link.token_prefix}…
                  </p>
                  <p className="text-[11px] text-gray-500 dark:text-gray-400">
                    {bi("创建", "Created")}: {formatTimestamp(link.created_at)} ·{" "}
                    {bi("最近访问", "Last used")}: {formatTimestamp(link.last_accessed_at)}
                    {link.accepted_by_name
                      ? ` · ${bi("买家确认", "Accepted by")}: ${link.accepted_by_name}`
                      : ""}
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <Badge tone={link.is_active ? "success" : "neutral"}>
                    {link.is_active ? bi("生效中", "Active") : bi("已撤销", "Revoked")}
                  </Badge>
                  {link.accepted_at && (
                    <Badge tone="info">{bi("已确认", "Accepted")}</Badge>
                  )}
                  <button
                    type="button"
                    data-testid={`portal-revoke-${link.id}`}
                    onClick={() => void onRevoke(link.id)}
                    disabled={!link.is_active || revokingId === link.id}
                    className="inline-flex items-center gap-1 rounded-lg border border-rose-200 bg-white px-2 py-1 text-[11.5px] font-medium text-rose-700 disabled:opacity-40"
                  >
                    <ShieldOff className="h-3.5 w-3.5" aria-hidden="true" />
                    {bi("撤销", "Revoke")}
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Card>
  );
}
