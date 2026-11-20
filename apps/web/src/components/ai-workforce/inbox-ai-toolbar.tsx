"use client";

import { useState } from "react";
import { Bot, ListChecks, MessageSquareText, Sparkles, TextQuote } from "lucide-react";
import { button, field } from "./presentation";
import type { InboxToolbarIntent, QuickReplyTemplate } from "./types";
import { WorkforceDialog } from "./workforce-dialog";

const actions = [
  { id: "customer_service", label: "AI 客服", icon: Bot },
  { id: "polish", label: "AI 润色", icon: Sparkles },
  { id: "summarize", label: "消息总结", icon: ListChecks },
  { id: "suggest_reply", label: "AI 建议", icon: MessageSquareText },
] as const;

function PhrasePicker({ phrases, onClose, onSelect }: {
  phrases: readonly QuickReplyTemplate[];
  onClose: () => void;
  onSelect: (phrase: QuickReplyTemplate) => void;
}) {
  const [query, setQuery] = useState("");
  const needle = query.trim().toLowerCase();
  const results = phrases.filter((phrase) => [phrase.title, phrase.category, phrase.language, phrase.body_text, phrase.shortcut_code ?? ""].some((text) => text.toLowerCase().includes(needle)));
  return (
    <WorkforceDialog title="快捷短语" closeLabel="关闭快捷短语" onClose={onClose}>
      <p className="mb-3 text-sm text-slate-600">选择后插入当前草稿，不会直接发送。</p>
      <input type="search" aria-label="搜索短语" value={query} onChange={(event) => setQuery(event.target.value)} className={field} />
      {results.length ? <ul className="mt-3 space-y-2">
        {results.map((phrase) => <li key={phrase.id}>
          <button type="button" onClick={() => onSelect(phrase)} className="w-full rounded-xl border border-slate-200 p-3 text-left hover:border-blue-300 hover:bg-blue-50">
            <span className="font-semibold">{phrase.title}</span>
            <span className="ml-2 text-xs text-slate-600">{phrase.category} · {phrase.language}{phrase.shortcut_code && " · " + phrase.shortcut_code}</span>
            <span className="mt-1 block line-clamp-2 break-words text-sm text-slate-700">{phrase.body_text}</span>
          </button>
        </li>)}
      </ul> : <p className="mt-3 text-sm text-slate-600">没有匹配的短语。</p>}
    </WorkforceDialog>
  );
}

export function InboxAiToolbar({ phrases, onAction, onInsertPhrase, disabled = false, pendingAction }: {
  phrases: readonly QuickReplyTemplate[];
  onAction: (action: InboxToolbarIntent) => void;
  onInsertPhrase: (phrase: QuickReplyTemplate) => void;
  disabled?: boolean;
  pendingAction?: InboxToolbarIntent | null;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div role="group" aria-label="收件箱 AI 快捷工具" className="flex flex-wrap items-center gap-2">
      {actions.map(({ id, label, icon: Icon }) => (
        <button key={id} type="button" disabled={disabled || !!pendingAction} aria-busy={pendingAction === id}
          onClick={() => onAction(id)} className={button + " inline-flex items-center gap-1.5"}>
          <Icon size={16} aria-hidden="true" className="text-blue-600" />{label}
        </button>
      ))}
      <button type="button" aria-haspopup="dialog" aria-expanded={open && !disabled} disabled={disabled} onClick={() => setOpen(true)} className={button + " inline-flex items-center gap-1.5"}>
        <TextQuote size={16} aria-hidden="true" className="text-blue-600" />快捷短语
      </button>
      {open && !disabled && <PhrasePicker phrases={phrases} onClose={() => setOpen(false)} onSelect={(phrase) => { onInsertPhrase(phrase); setOpen(false); }} />}
    </div>
  );
}
