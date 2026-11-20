"use client";

import React, { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { AppShell } from "@/components/app-shell";
import { fetchPendingProposalsCount } from "@/lib/data-source";
import { addPhase3aTask, fetchPhase3aTasks, patchPhase3aTask } from "@/lib/phase3a-data";
import { useI18n } from "@/lib/i18n";

export default function TasksPage() {
  const { bi } = useI18n();
  const queryClient = useQueryClient();
  const [title, setTitle] = useState("");
  const [priority, setPriority] = useState<"low" | "normal" | "high">("normal");
  const [showCompleted, setShowCompleted] = useState(false);

  const proposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () => fetchPendingProposalsCount(),
  });
  const tasksQuery = useQuery({
    queryKey: ["crm", "tasks", showCompleted],
    queryFn: () => fetchPhase3aTasks(showCompleted ? "done" : "open"),
  });
  const createTask = useMutation({
    mutationFn: () => addPhase3aTask({ title: title.trim(), priority }),
    onSuccess: () => {
      setTitle("");
      void queryClient.invalidateQueries({ queryKey: ["crm", "tasks"] });
    },
  });
  const toggleTask = useMutation({
    mutationFn: ({ id, status }: { id: string; status: "open" | "done" }) =>
      patchPhase3aTask(id, { status }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["crm", "tasks"] }),
  });

  const tasks = tasksQuery.data?.items ?? [];
  const error = createTask.error ?? toggleTask.error ?? tasksQuery.error;

  return (
    <AppShell currentPath="/tasks" pendingProposalsCount={proposalsQuery.data?.total ?? 0}>
      <div className="erp-page space-y-4">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="text-xl font-semibold tracking-tight text-slate-900">{bi("任务提醒", "Tasks")}</h1>
            <p className="text-xs text-slate-500">
              {bi("与客户、商机、报价或采购单关联的待办；完成后保留历史记录。", "Follow-ups linked to customers, deals, quotes or purchase orders; history is kept after completion.")}
            </p>
          </div>
          <label className="flex items-center gap-1.5 text-xs text-slate-600">
            <input
              type="checkbox"
              data-testid="tasks-show-completed"
              checked={showCompleted}
              onChange={(event) => setShowCompleted(event.target.checked)}
            />
            {bi("查看已完成", "Show completed")}
          </label>
        </div>

        <form
          data-testid="task-form"
          onSubmit={(event) => {
            event.preventDefault();
            if (title.trim()) createTask.mutate();
          }}
          className="erp-card grid gap-2 p-3 sm:grid-cols-[2fr_140px_auto]"
        >
          <input
            aria-label="Task title"
            required
            maxLength={255}
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            placeholder="例如：跟进 Hans 的样品确认"
            className="rounded border border-slate-300 px-2 py-1 text-xs"
          />
          <select
            aria-label="Task priority"
            value={priority}
            onChange={(event) => setPriority(event.target.value as "low" | "normal" | "high")}
            className="rounded border border-slate-300 px-2 py-1 text-xs"
          >
            <option value="low">{bi("低优先级", "Low")}</option>
            <option value="normal">{bi("普通", "Normal")}</option>
            <option value="high">{bi("高优先级", "High")}</option>
          </select>
          <button
            type="submit"
            data-testid="task-submit"
            disabled={!title.trim() || createTask.isPending}
            className="rounded bg-blue-600 px-3 py-1 text-xs font-medium text-white hover:bg-blue-700 disabled:opacity-40"
          >
            {bi("新建任务", "Create task")}
          </button>
          {error && (
            <p role="alert" className="sm:col-span-3 text-xs text-rose-700">
              {error.message}
            </p>
          )}
        </form>

        <ul data-testid="task-list" className="space-y-2">
          {tasks.map((task) => (
            <li
              key={task.id}
              data-testid={`task-row-${task.id}`}
              className="erp-card flex items-center justify-between gap-3 p-3 text-[13px]"
            >
              <div className="min-w-0">
                <p
                  className={`truncate font-medium ${
                    task.status === "done" ? "text-slate-400 line-through" : "text-slate-800"
                  }`}
                >
                  {task.title}
                </p>
                <p className="text-[11px] text-slate-500">
                  {task.priority === "high" ? bi("高优先级", "High") : task.priority === "low" ? bi("低优先级", "Low") : bi("普通", "Normal")}
                  {task.due_at ? ` · 到期 ${new Date(task.due_at).toLocaleDateString()}` : ""}
                  {task.linked_entity_type ? ` · ${task.linked_entity_type}` : ""}
                </p>
              </div>
              <button
                type="button"
                data-testid={`toggle-task-${task.id}`}
                onClick={() =>
                  toggleTask.mutate({
                    id: task.id,
                    status: task.status === "done" ? "open" : "done",
                  })
                }
                className="rounded border border-slate-200 px-2 py-1 text-[11px] font-medium text-slate-700 hover:bg-slate-100"
              >
                {task.status === "done" ? bi("重开", "Reopen") : bi("完成", "Done")}
              </button>
            </li>
          ))}
          {tasks.length === 0 && (
            <li className="rounded-lg border border-dashed border-slate-200 p-4 text-center text-xs text-slate-400">
              {tasksQuery.isLoading ? bi("加载中…", "Loading…") : bi("暂无任务", "No tasks")}
            </li>
          )}
        </ul>
      </div>
    </AppShell>
  );
}
