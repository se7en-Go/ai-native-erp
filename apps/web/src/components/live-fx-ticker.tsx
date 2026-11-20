"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { RefreshCw, TrendingUp } from "lucide-react";
import { fetchLiveFxRates, mockLiveFxRates } from "@/lib/api-client/live-fx";
import type { LiveFxRatesRead } from "@/lib/api-client/types.gen";
import { useI18n } from "@/lib/i18n";

let cachedFx: { data: LiveFxRatesRead; expiresAt: number } | null = null;
const CLIENT_CACHE_MS = 15 * 60_000;

function formatRate(raw: string | number | undefined): string {
  const num = Number(raw);
  if (!Number.isFinite(num) || num <= 0) return "—";
  return num.toFixed(4);
}

export function LiveFxTicker({ enabled = true }: { enabled?: boolean }) {
  const { bi } = useI18n();
  const [fx, setFx] = useState<LiveFxRatesRead | null>(() =>
    cachedFx && cachedFx.expiresAt > Date.now() ? cachedFx.data : null
  );
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  const load = useCallback(
    async (forceRefresh = false) => {
      if (!enabled) return;
      if (!forceRefresh && cachedFx && cachedFx.expiresAt > Date.now()) {
        setFx(cachedFx.data);
        return;
      }
      setLoading(true);
      try {
        const res = await fetchLiveFxRates({ base: "USD", forceRefresh });
        cachedFx = { data: res, expiresAt: Date.now() + CLIENT_CACHE_MS };
        setFx(res);
      } catch {
        setFx((prev) => prev ?? { ...mockLiveFxRates, is_live: false, source: "reference_fallback" });
      } finally {
        setLoading(false);
      }
    },
    [enabled]
  );

  useEffect(() => {
    void load(false);
  }, [load]);

  useEffect(() => {
    if (!open) return;
    function onDocClick(event: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, [open]);

  const usdCnyText = fx ? formatRate(fx.usd_cny) : "…";
  const isLive = Boolean(fx?.is_live);

  return (
    <div ref={containerRef} className="relative ml-2 hidden xl:inline-flex">
      <button
        type="button"
        data-testid="live-fx-ticker-btn"
        onClick={() => setOpen((prev) => !prev)}
        title={
          fx
            ? bi(
                `${isLive ? "实时/每日参考汇率" : "缓存参考汇率"} · 来源 ${fx.source} (${fx.as_of_date})，点击查看多币种或刷新`,
                `${isLive ? "Live/daily FX" : "Cached FX"} · ${fx.source} (${fx.as_of_date})`
              )
            : bi("加载实时汇率…", "Loading live FX…")
        }
        className="inline-flex items-center gap-1.5 rounded-full border border-blue-200/80 bg-blue-50 px-2.5 py-0.5 text-[11.5px] font-semibold text-blue-700 tabular-nums transition-colors hover:border-blue-300 hover:bg-blue-100/80 dark:border-blue-800 dark:bg-blue-900/30 dark:text-blue-300 dark:hover:bg-blue-900/50"
      >
        <span
          className={`h-1.5 w-1.5 rounded-full ${
            isLive ? "bg-emerald-500" : "bg-amber-400"
          }`}
        />
        <span>USD/CNY</span>
        <span>{usdCnyText}</span>
        <TrendingUp className="h-3 w-3 text-blue-500 dark:text-blue-400" />
      </button>

      {open && (
        <div
          data-testid="live-fx-popover"
          className="absolute top-9 left-0 z-50 w-72 rounded-xl border border-gray-200 bg-white p-3.5 shadow-xl dark:border-gray-700 dark:bg-gray-900"
        >
          <div className="mb-2.5 flex items-center justify-between gap-2">
            <div>
              <p className="text-[12.5px] font-semibold text-gray-900 dark:text-gray-100">
                {bi("实时外贸参考汇率", "Live export FX rates")}
              </p>
              <p className="text-[11px] text-gray-500 dark:text-gray-400">
                {fx
                  ? `${fx.source} · ${fx.as_of_date}`
                  : bi("正在同步…", "Syncing…")}
              </p>
            </div>
            <button
              type="button"
              data-testid="live-fx-refresh-btn"
              disabled={loading}
              onClick={() => void load(true)}
              aria-label={bi("刷新汇率", "Refresh FX rates")}
              className="inline-flex items-center gap-1 rounded-lg border border-gray-200 bg-gray-50 px-2 py-1 text-[11px] font-semibold text-gray-700 transition-colors hover:bg-gray-100 disabled:opacity-50 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
            >
              <RefreshCw className={`h-3 w-3 ${loading ? "animate-spin" : ""}`} />
              <span>{bi("刷新", "Refresh")}</span>
            </button>
          </div>

          <div className="space-y-1.5">
            {(fx?.rates ?? mockLiveFxRates.rates).map((item) => (
              <div
                key={item.pair}
                className="flex items-center justify-between rounded-lg bg-gray-50 px-2.5 py-1.5 text-[12.5px] dark:bg-gray-800/80"
              >
                <span className="font-medium text-gray-700 dark:text-gray-300">{item.pair}</span>
                <span className="font-bold text-gray-900 tabular-nums dark:text-gray-100">
                  {formatRate(item.rate)}
                </span>
              </div>
            ))}
          </div>

          <p className="mt-2.5 border-t border-gray-100 pt-2 text-[11px] leading-4 text-gray-500 dark:border-gray-800 dark:text-gray-400">
            {bi(
              "服务端每小时自动向 ECB / ExchangeRate-API 拉取最新汇率并写入 Redis 缓存，报价锁汇以生成 PI 时快照为准。",
              "Server refreshes hourly from ECB / ExchangeRate-API with Redis caching; quotes lock the snapshot at PI generation."
            )}
          </p>
        </div>
      )}
    </div>
  );
}
