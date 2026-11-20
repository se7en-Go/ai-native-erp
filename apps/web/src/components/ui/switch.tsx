"use client";

import React from "react";

export interface SwitchProps {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  disabled?: boolean;
  id?: string;
  "aria-label"?: string;
  "data-testid"?: string;
  size?: "sm" | "md";
}

/**
 * 21st.dev / Linear 风格胶囊开关，替代原生浏览器 checkbox。
 * 完整支持键盘操作与 role="switch" 无障碍语义。
 */
export function Switch({
  checked,
  onCheckedChange,
  disabled = false,
  id,
  "aria-label": ariaLabel,
  "data-testid": testId,
  size = "md",
}: SwitchProps) {
  const trackSize = size === "sm" ? "h-5 w-9" : "h-6 w-11";
  const thumbSize = size === "sm" ? "h-4 w-4" : "h-5 w-5";
  const thumbTranslate =
    size === "sm"
      ? checked
        ? "translate-x-4"
        : "translate-x-0.5"
      : checked
        ? "translate-x-5"
        : "translate-x-0.5";

  return (
    <button
      id={id}
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={ariaLabel}
      data-testid={testId}
      disabled={disabled}
      onClick={() => !disabled && onCheckedChange(!checked)}
      className={`relative inline-flex shrink-0 cursor-pointer items-center rounded-full transition-colors duration-200 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-emerald-600 disabled:cursor-not-allowed disabled:opacity-50 ${trackSize} ${
        checked
          ? "bg-emerald-600 shadow-[inset_0_1px_2px_rgba(6,78,59,0.28)]"
          : "bg-stone-300 hover:bg-stone-400/80"
      }`}
    >
      <span
        className={`pointer-events-none inline-block rounded-full bg-white shadow-sm ring-1 ring-slate-900/5 transition-transform duration-200 ${thumbSize} ${thumbTranslate}`}
      />
    </button>
  );
}
