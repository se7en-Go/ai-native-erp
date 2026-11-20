"use client";

import React from "react";

export interface SegmentedTabOption<T extends string = string> {
  value: T;
  label: React.ReactNode;
  badge?: React.ReactNode;
  testId?: string;
}

/**
 * 21st.dev / Vercel 风格胶囊分段控制器。
 */
export function SegmentedTabs<T extends string = string>({
  options,
  value,
  onChange,
  size = "md",
  className = "",
  ariaLabel,
}: {
  options: SegmentedTabOption<T>[];
  value: T;
  onChange: (value: T) => void;
  size?: "sm" | "md";
  className?: string;
  ariaLabel?: string;
}) {
  const pad = size === "sm" ? "px-2.5 py-1 text-[12px]" : "px-3 py-1.5 text-[12.5px]";

  return (
    <div
      role="tablist"
      aria-label={ariaLabel}
      className={`inline-flex items-center gap-0.5 rounded-lg border border-gray-200 bg-gray-100 p-1 dark:border-gray-700 dark:bg-gray-800 ${className}`}
    >
      {options.map((opt) => {
        const active = opt.value === value;
        return (
          <button
            key={opt.value}
            type="button"
            role="tab"
            aria-selected={active}
            data-testid={opt.testId}
            onClick={() => onChange(opt.value)}
            className={`inline-flex items-center gap-1.5 rounded-md font-semibold transition-all duration-150 ${pad} ${
              active
                ? "bg-blue-600 text-white shadow-xs"
                : "text-gray-600 hover:bg-white/80 hover:text-gray-900 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-gray-100"
            }`}
          >
            <span>{opt.label}</span>
            {opt.badge !== undefined && opt.badge !== null && (
              <span
                className={`rounded-full px-1.5 py-0.2 text-[10.5px] font-semibold tabular-nums ${
                  active ? "bg-white/20 text-white" : "bg-slate-200/80 text-slate-700"
                }`}
              >
                {opt.badge}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}
