"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { Search } from "lucide-react";

import { useAuthSession } from "@/components/providers";
import { canAccessRoute } from "@/lib/auth-cookie";
import { useI18n } from "@/lib/i18n";
import {
  ALL_NAV_ITEMS,
  commandPaletteEntries,
  filterCommandEntries,
  type CommandEntry,
} from "@/lib/nav-registry";
import { searchPhase3a } from "@/lib/phase3a-data";

export interface CommandPaletteState {
  open: boolean;
  setOpen: (open: boolean) => void;
  toggle: () => void;
}

/** Registers Ctrl/Cmd+K and exposes the palette open state for the shell button. */
export function useCommandPalette(): CommandPaletteState {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setOpen((previous) => !previous);
      }
      if (event.key === "Escape") {
        setOpen(false);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);
  return {
    open,
    setOpen,
    toggle: useCallback(() => setOpen((previous) => !previous), []),
  };
}

export function CommandPalette({ open, onClose }: { open: boolean; onClose: () => void }) {
  const router = useRouter();
  const { principal } = useAuthSession();
  const { t } = useI18n();
  const [query, setQuery] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  const role = principal?.role ?? null;
  const labels = useMemo(() => {
    const map: Record<string, string> = {};
    for (const item of ALL_NAV_ITEMS) {
      map[item.href] = t(item.labelKey);
    }
    return map;
  }, [t]);

  const commands = useMemo(
    () => filterCommandEntries(commandPaletteEntries(role), query, labels),
    [role, query, labels]
  );

  const isEntityQuery = query.trim().length >= 2;
  const entityQuery = useQuery({
    queryKey: ["search", "global", query],
    queryFn: () => searchPhase3a(query.trim()),
    enabled: open && isEntityQuery,
    staleTime: 5_000,
  });

  useEffect(() => {
    if (open) {
      setQuery("");
      const timer = window.setTimeout(() => inputRef.current?.focus(), 0);
      return () => window.clearTimeout(timer);
    }
  }, [open]);

  const go = useCallback(
    (href: string) => {
      onClose();
      router.push(href);
    },
    [onClose, router]
  );

  if (!open) return null;

  const results = entityQuery.data?.items ?? [];

  return (
    <div
      data-testid="command-palette"
      className="fixed inset-0 z-50 flex items-start justify-center bg-slate-900/40 p-6 pt-24"
      onClick={onClose}
      role="presentation"
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={t("commandPaletteTitle")}
        className="w-full max-w-xl overflow-hidden rounded-xl border border-slate-200 bg-white shadow-2xl"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex items-center gap-2 border-b border-slate-200 px-4 py-3">
          <Search className="h-4 w-4 text-slate-400" />
          <input
            ref={inputRef}
            data-testid="command-palette-input"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={t("commandPalettePlaceholder")}
            className="w-full bg-transparent text-sm outline-none"
          />
          <kbd className="rounded border border-slate-200 px-1.5 py-0.5 text-[10px] text-slate-500">
            Esc
          </kbd>
        </div>

        <div className="max-h-80 overflow-auto py-2">
          {commands.length === 0 && results.length === 0 && (
            <p data-testid="command-palette-empty" className="px-4 py-3 text-xs text-slate-500">
              {t("commandPaletteEmpty")}
            </p>
          )}
          {commands.length > 0 && (
            <div className="px-2">
              <p className="px-2 py-1 text-[10px] font-semibold uppercase tracking-wide text-slate-400">
                {t("commandPaletteTitle")}
              </p>
              {commands.map((entry: CommandEntry) => (
                <button
                  key={entry.id}
                  type="button"
                  data-testid={`command-entry-${entry.id}`}
                  disabled={!role || !canAccessRoute(role, entry.href)}
                  onClick={() => go(entry.href)}
                  className="flex w-full items-center justify-between rounded-md px-2 py-1.5 text-left text-sm text-slate-700 hover:bg-slate-100 disabled:opacity-40"
                >
                  <span>{labels[entry.href] ?? entry.label}</span>
                  <span className="text-[10px] uppercase text-slate-400">{entry.group}</span>
                </button>
              ))}
            </div>
          )}
          {results.length > 0 && (
            <div className="mt-1 px-2">
              <p className="px-2 py-1 text-[10px] font-semibold uppercase tracking-wide text-slate-400">
                ERP
              </p>
              {results.map((hit) => (
                <button
                  key={`${hit.kind}-${hit.id}`}
                  type="button"
                  data-testid={`command-hit-${hit.kind}`}
                  onClick={() => go(searchTarget(hit))}
                  className="flex w-full items-center justify-between rounded-md px-2 py-1.5 text-left text-sm text-slate-700 hover:bg-slate-100"
                >
                  <span>{hit.label}</span>
                  <span className="text-[10px] uppercase text-slate-400">{hit.kind}</span>
                </button>
              ))}
            </div>
          )}
        </div>

        <div className="border-t border-slate-200 px-4 py-2 text-[10px] text-slate-400">
          {t("commandPaletteHint")}
        </div>
      </div>
    </div>
  );
}

/** Tenant-scoped record → owning page. Keeps search results actionable. */
export function searchTarget(hit: { kind: "customer" | "order" | "quote" | "sku"; id: string }): string {
  switch (hit.kind) {
    case "customer":
      return `/crm/customers?customer=${hit.id}`;
    case "order":
      return `/orders?order=${hit.id}`;
    case "quote":
      return `/b2b/quotes?quote=${hit.id}`;
    case "sku":
      return `/catalog?sku=${hit.id}`;
  }
}
