"use client";

import React, { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, ListChecks, Plus, RotateCcw, Zap } from "lucide-react";

import { AppShell } from "@/components/app-shell";
import { BentoStatCard } from "@/components/ui/bento-stat-card";
import { EmptyState, Page, PageHeader } from "@/components/ui/page";
import { Switch } from "@/components/ui/switch";
import { fetchPendingProposalsCount } from "@/lib/data-source";
import { addPhase3aTask, fetchPhase3aTasks, patchPhase3aTask } from "@/lib/phase3a-data";
import { useI18n } from "@/lib/i18n";

const PRIORITY_DOT: Record<string, string> = {
  high: "bg-rose-500",
  normal: "bg-blue-500",
  low: "bg-slate-400",
};

const PRIORITY_PILL: Record<string, string> = {
  high: "border-rose-200 bg-rose-50 text-rose-700",
  normal: "border-blue-200 bg-blue-50 text-blue-700",
  low: "border-slate-200 bg-slate-100 text-slate-600",
};

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

  const summary = useMemo(() => {
    const high = tasks.filter((task) => task.priority === "high" && task.status !== "done").length;
    const done = tasks.filter((task) => task.status === "done").length;
    const overdue = tasks.filter(
      (task) =>
        task.status !== "done" && task.due_at !== null && task.due_at !== undefined
          ? new Date(task.due_at).getTime() < Date.now()
          : false
    ).length;
    return { high, done, overdue };
  }, [tasks]);

  return (
    <AppShell currentPath="/tasks" pendingProposalsCount={proposalsQuery.data?.total ?? 0}>
      <Page>
        <PageHeader
          title={bi("任务提醒", "Tasks")}
          description={bi(
            "与客户、商机、报价或采购单关联的待办；完成后保留历史记录。",
            "Follow-ups linked to customers, deals, quotes or purchase orders; history is kept after completion."
          )}
          actions={
            <label className="inline-flex items-center gap-2 rounded-xl border border-slate-200 bg-white px-3 py-1.5 text-[12px] font-semibold text-slate-700 shadow-xs">
              <Switch
                size="sm"
                data-testid="tasks-show-completed"
                aria-label={bi("查看已完成", "Show completed")}
                checked={showCompleted}
                onCheckedChange={setShowCompleted}
              />
              <span>{bi("查看已完成", "Show completed")}</span>
            </label>
          }
        />

        <section
          aria-label={bi("任务关键指标", "Task key metrics")}
          className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"
        >
          <BentoStatCard
            label={bi("任务总数", "Total tasks")}
            value={tasks.length}
            icon={ListChecks}
            accent="blue"
            hint={bi("按当前视图统计", "Counted for the current view")}
          />
          <BentoStatCard
            label={bi("高优先级", "High priority")}
            value={summary.high}
            icon={Zap}
            accent="rose"
            hint={bi("需要今天内响应", "Needs a response today")}
          />
          <BentoStatCard
            label={bi("已完成", "Completed")}
            value={summary.done}
            icon={CheckCircle2}
            accent="emerald"
            hint={bi("完成后保留历史记录", "History is retained after completion")}
          />
          <BentoStatCard
            label={bi("已逾期", "Overdue")}
            value={summary.overdue}
            icon={RotateCcw}
            accent="amber"
            hint={bi("按到期时间判定", "Based on due date")}
          />
        </section>

        <form
          data-testid="task-form"
          onSubmit={(event) => {
            event.preventDefault();
            if (title.trim()) createTask.mutate();
          }}
          className="erp-card grid gap-2 p-3.5 sm:grid-cols-[2fr_150px_auto]"
        >
          <input
            aria-label="Task title"
            required
            maxLength={255}
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            placeholder="例如：跟进 Hans 的样品确认"
            className="rounded-xl border border-slate-200 bg-slate-50/80 px-2.5 py-1.5 text-[12.5px] text-slate-800 transition-colors placeholder:text-slate-400 focus:border-blue-500 focus:bg-white focus:outline-none focus:ring-[3px] focus:ring-blue-500/12"
          />
          <select
            aria-label="Task priority"
            value={priority}
            onChange={(event) => setPriority(event.target.value as "low" | "normal" | "high")}
            className="rounded-xl border border-slate-200 bg-slate-50/80 px-2.5 py-1.5 text-[12.5px] font-medium text-slate-800 transition-colors focus:border-blue-500 focus:bg-white focus:outline-none focus:ring-[3px] focus:ring-blue-500/12"
          >
            <option value="low">{bi("低优先级", "Low")}</option>
            <option value="normal">{bi("普通", "Normal")}</option>
            <option value="high">{bi("高优先级", "High")}</option>
          </select>
          <button
            type="submit"
            data-testid="task-submit"
            disabled={!title.trim() || createTask.isPending}
            className="inline-flex items-center justify-center gap-1.5 rounded-xl bg-blue-600 px-3 py-1.5 text-[12px] font-semibold text-white shadow-[0_2px_8px_-2px_rgba(37,99,235,0.55)] transition-colors hover:bg-blue-700 disabled:shadow-none disabled:opacity-40"
          >
            <Plus className="h-3.5 w-3.5" />
            {bi("新建任务", "Create task")}
          </button>
          {error && (
            <p role="alert" className="text-[12px] font-medium text-rose-700 sm:col-span-3">
              {error.message}
            </p>
          )}
        </form>

        {tasks.length === 0 ? (
          <EmptyState>
            {tasksQuery.isLoading ? bi("加载中…", "Loading…") : bi("暂无任务", "No tasks")}
          </EmptyState>
        ) : (
          <ul data-testid="task-list" className="space-y-2">
            {tasks.map((task) => {
              const done = task.status === "done";
              return (
                <li
                  key={task.id}
                  data-testid={`task-row-${task.id}`}
                  className={`erp-card flex items-center justify-between gap-3 p-3.5 text-[13px] transition-shadow hover:shadow-[0_4px_14px_rgba(15,23,42,0.08)] ${
                    done ? "opacity-70" : ""
                  }`}
                >
                  <div className="flex min-w-0 items-start gap-2.5">
                    <span
                      aria-hidden="true"
                      className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${
                        PRIORITY_DOT[task.priority] ?? "bg-slate-400"
                      }`}
                    />
                    <div className="min-w-0">
                      <p
                        className={`truncate font-semibold ${
                          done ? "text-slate-400 line-through" : "text-slate-900"
                        }`}
                      >
                        {task.title}
                      </p>
                      <div className="mt-1 flex flex-wrap items-center gap-1.5">
                        <span
                          className={`rounded-full border px-2 py-0.5 text-[10.5px] font-semibold ${
                            PRIORITY_PILL[task.priority] ?? PRIORITY_PILL.normal
                          }`}
                        >
                          {task.priority === "high"
                            ? bi("高优先级", "High")
                            : task.priority === "low"
                              ? bi("低优先级", "Low")
                              : bi("普通", "Normal")}
                        </span>
                        {task.due_at && (
                          <span className="rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-[10.5px] font-medium tabular-nums text-slate-600">
                            {bi("到期", "Due")} {new Date(task.due_at).toLocaleDateString()}
                          </span>
                        )}
                        {task.linked_entity_type && (
                          <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5 text-[10.5px] font-medium text-slate-600">
                            {task.linked_entity_type}
                          </span>
                        )}
                      </div>
                    </div>
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
                    className={`shrink-0 rounded-xl border px-2.5 py-1 text-[11px] font-semibold transition-colors ${
                      done
                        ? "border-slate-200 bg-white text-slate-700 hover:bg-slate-50"
                        : "border-emerald-200 bg-emerald-50 text-emerald-800 hover:bg-emerald-100"
                    }`}
                  >
                    {task.status === "done" ? bi("重开", "Reopen") : bi("完成", "Done")}
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </Page>
    </AppShell>
  );
}
