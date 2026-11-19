"use client";

import { type ReactNode, useEffect, useId, useRef } from "react";
import { X } from "lucide-react";

export function WorkforceDialog({ title, closeLabel, drawer = false, onClose, children }: {
  title: string;
  closeLabel: string;
  drawer?: boolean;
  onClose: () => void;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const headingId = useId();
  useEffect(() => {
    const dialog = ref.current;
    const trigger = document.activeElement;
    if (!dialog) return;
    if (dialog.showModal) dialog.showModal();
    else dialog.setAttribute("open", "");
    return () => {
      if (dialog.close) dialog.close();
      else dialog.removeAttribute("open");
      if (trigger instanceof HTMLElement && trigger.isConnected) trigger.focus();
    };
  }, []);

  return (
    <dialog ref={ref} aria-labelledby={headingId} onCancel={(event) => { event.preventDefault(); onClose(); }}
      className={drawer
        ? "fixed inset-y-0 right-0 left-auto m-0 h-full max-h-none w-[min(100%,28rem)] border-l border-slate-200 bg-white p-6 text-slate-900 shadow-2xl backdrop:bg-slate-900/45"
        : "m-auto max-h-[80dvh] w-[min(92vw,32rem)] rounded-2xl bg-white p-5 text-slate-900 shadow-2xl backdrop:bg-slate-900/45"}>
      <div className="mb-4 flex items-center justify-between gap-4 border-b border-slate-200 pb-3">
        <h2 id={headingId} className="text-lg font-bold">{title}</h2>
        <button type="button" aria-label={closeLabel} onClick={onClose} className="rounded-lg p-2 text-slate-600 hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-indigo-600">
          <X aria-hidden="true" size={20} />
        </button>
      </div>
      {children}
    </dialog>
  );
}
