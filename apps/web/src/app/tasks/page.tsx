"use client";

import React, { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { AppShell } from "@/components/app-shell";
import { fetchPendingProposalsCount } from "@/lib/data-source";
import { addPhase3aTask, fetchPhase3aTasks, patchPhase3aTask } from "@/lib/phase3a-data";

export default function TasksPage() {
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
      <div className="mx-auto max-w-4xl space-y-4">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="text-lg font-bold text-slate-900">任务提醒 / Tasks</h1>
            <p className="text-xs text-slate-500">
              与客户、商机、报价或采购单关联的待办；完成后保留历史。
            </p>
          </div>
          <label className="flex items-center gap-1.5 text-xs text-slate-600">
            <input
              type="checkbox"
              data-testid="tasks-show-completed"
              checked={showCompleted}
              onChange={(event) => setShowCompleted(event.target.checked)}
            />
            查看已完成
          </label>
        </div>

        <form
          data-testid="task-form"
          onSubmit={(event) => {
            event.preventDefault();
            if (title.trim()) createTask.mutate();
          }}
          className="grid gap-2 rounded-lg border border-slate-200 bg-white p-3 sm:grid-cols-[2fr_140px_auto]"
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
            <option value="low">low</option>
            <option value="normal">normal</option>
            <option value="high">high</option>
          </select>
          <button
            type="submit"
            data-testid="task-submit"
            disabled={!title.trim() || createTask.isPending}
            className="rounded bg-indigo-600 px-3 py-1 text-xs font-medium text-white hover:bg-indigo-700 disabled:opacity-40"
          >
            新建任务
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
              className="flex items-center justify-between gap-3 rounded-lg border border-slate-200 bg-white p-3 text-xs"
            >
              <div className="min-w-0">
                <p
                  className={`truncate font-medium ${
                    task.status === "done" ? "text-slate-400 line-through" : "text-slate-800"
                  }`}
                >
                  {task.title}
                </p>
                <p className="text-[10px] text-slate-500">
                  {task.priority}
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
                {task.status === "done" ? "重开" : "完成"}
              </button>
            </li>
          ))}
          {tasks.length === 0 && (
            <li className="rounded-lg border border-dashed border-slate-200 p-4 text-center text-xs text-slate-400">
              {tasksQuery.isLoading ? "加载中…" : "暂无任务"}
            </li>
          )}
        </ul>
      </div>
    </AppShell>
  );
}
