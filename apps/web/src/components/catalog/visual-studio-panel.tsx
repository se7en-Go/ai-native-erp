"use client";

import React, { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ImagePlus, Loader2, Sparkles, Trash2, Wand2 } from "lucide-react";

import { Badge, Card, CardHeader, EmptyState } from "@/components/ui/page";
import { useAuthSession } from "@/components/providers";
import { useI18n } from "@/lib/i18n";
import {
  VISUAL_JOB_STATUS_LABELS,
  VISUAL_OPERATIONS,
  VISUAL_PROVIDERS,
  canWriteVisualJobs,
  createVisualJob,
  deleteVisualJob,
  fetchVisualJobs,
  operationLabel,
  providerLabel,
  type VisualJobCreate,
  type VisualJobRead,
  type VisualOperation,
  type VisualProvider,
} from "@/lib/api-client/track9-visual-ads";

const SAMPLE_SOURCE =
  "https://cdn.example.com/products/led-panel-600.png";

/**
 * Sourcing visual studio: 去白底 / 换场景 / 图片文字翻译.
 *
 * Mounted on `/ai/studio`. The provider runs server-side; a vendor provider with
 * no API key comes back as a `failed` job with a readable reason instead of an
 * error toast, and every completed job renders its real `<img>` preview (a
 * presigned R2 URL, or the deterministic `data:image/png` the sandbox emits).
 */
export function VisualStudioPanel() {
  const { bi, locale } = useI18n();
  const { principal } = useAuthSession();
  const queryClient = useQueryClient();
  const canWrite = canWriteVisualJobs(principal?.role);

  const [provider, setProvider] = useState<VisualProvider>("sandbox");
  const [operation, setOperation] = useState<VisualOperation>("remove_bg");
  const [sourceUrl, setSourceUrl] = useState(SAMPLE_SOURCE);
  const [prompt, setPrompt] = useState("");
  const [targetLocale, setTargetLocale] = useState("");
  const [error, setError] = useState<string | null>(null);

  const jobsQuery = useQuery({
    queryKey: ["track9", "visual-jobs"],
    queryFn: fetchVisualJobs,
  });
  const jobs = jobsQuery.data ?? [];
  const completedCount = useMemo(
    () => jobs.filter((job) => job.status === "completed").length,
    [jobs],
  );
  const failedCount = useMemo(
    () => jobs.filter((job) => job.status === "failed").length,
    [jobs],
  );

  const create = useMutation({
    mutationFn: (payload: VisualJobCreate) => createVisualJob(payload),
    onSuccess: (job) => {
      setError(null);
      if (job.status === "failed") setError(job.error_message ?? "render failed");
      queryClient.setQueryData<VisualJobRead[]>(["track9", "visual-jobs"], (prev) =>
        [job, ...(prev ?? [])].slice(0, 50),
      );
    },
    onError: (err: Error) => setError(err.message),
  });

  const remove = useMutation({
    mutationFn: (jobId: string) => deleteVisualJob(jobId),
    onSuccess: (_data, jobId) => {
      queryClient.setQueryData<VisualJobRead[]>(["track9", "visual-jobs"], (prev) =>
        (prev ?? []).filter((job) => job.id !== jobId),
      );
    },
    onError: (err: Error) => setError(err.message),
  });

  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    create.mutate({
      provider,
      operation,
      source_image_url: sourceUrl.trim(),
      prompt: prompt.trim() === "" ? null : prompt.trim(),
      target_locale: targetLocale.trim() === "" ? null : targetLocale.trim(),
    });
  }

  return (
    <Card data-testid="visual-studio-panel" className="p-5">
      <CardHeader
        icon={Wand2}
        title={bi("视觉加工工作台", "Visual processing studio")}
        description={bi(
          "去白底、换场景、图片文字翻译由后端 provider 执行；沙箱 provider 离线即可产出真实 PNG 预览，商业 provider 未配置密钥时按失败记录原因。",
          "Background removal, scene replacement and caption translation run server-side; the sandbox provider renders a real PNG offline while a vendor provider without a key records a readable failure."
        )}
        actions={
          <div className="flex items-center gap-2">
            <Badge tone="success">
              {bi(`已完成 ${completedCount}`, `${completedCount} completed`)}
            </Badge>
            {failedCount > 0 && (
              <span data-testid="visual-failed-count">
                <Badge tone="danger">
                  {bi(`失败 ${failedCount}`, `${failedCount} failed`)}
                </Badge>
              </span>
            )}
          </div>
        }
      />

      <form
        data-testid="visual-studio-form"
        onSubmit={submit}
        className="mt-4 grid gap-3 rounded-xl border border-gray-200 bg-gray-50/70 p-4 dark:border-gray-700 dark:bg-gray-800/40 sm:grid-cols-2"
      >
        <label className="flex flex-col gap-1 text-[12px] font-semibold text-gray-700 dark:text-gray-300">
          {bi("加工服务商", "Provider")}
          <select
            data-testid="visual-provider-select"
            value={provider}
            onChange={(event) => setProvider(event.target.value as VisualProvider)}
            className="rounded-lg border border-gray-200 bg-white px-2.5 py-1.5 text-[12.5px] font-normal text-gray-900 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-100"
          >
            {VISUAL_PROVIDERS.map((option) => (
              <option key={option.value} value={option.value}>
                {locale === "zh" ? option.label_zh : option.label_en}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1 text-[12px] font-semibold text-gray-700 dark:text-gray-300">
          {bi("加工动作", "Operation")}
          <select
            data-testid="visual-operation-select"
            value={operation}
            onChange={(event) => setOperation(event.target.value as VisualOperation)}
            className="rounded-lg border border-gray-200 bg-white px-2.5 py-1.5 text-[12.5px] font-normal text-gray-900 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-100"
          >
            {VISUAL_OPERATIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {locale === "zh" ? option.label_zh : option.label_en}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1 text-[12px] font-semibold text-gray-700 dark:text-gray-300 sm:col-span-2">
          {bi("源图地址", "Source image URL")}
          <input
            data-testid="visual-source-input"
            type="text"
            required
            value={sourceUrl}
            onChange={(event) => setSourceUrl(event.target.value)}
            placeholder="https://… 或 /media/…"
            className="rounded-lg border border-gray-200 bg-white px-2.5 py-1.5 font-mono text-[12px] font-normal text-gray-900 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-100"
          />
        </label>

        <label className="flex flex-col gap-1 text-[12px] font-semibold text-gray-700 dark:text-gray-300">
          {bi("目标语言（文字翻译）", "Target locale (caption translation)")}
          <input
            data-testid="visual-locale-input"
            type="text"
            value={targetLocale}
            onChange={(event) => setTargetLocale(event.target.value)}
            placeholder="es-MX / ja-JP"
            className="rounded-lg border border-gray-200 bg-white px-2.5 py-1.5 font-mono text-[12px] font-normal text-gray-900 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-100"
          />
        </label>

        <label className="flex flex-col gap-1 text-[12px] font-semibold text-gray-700 dark:text-gray-300">
          {bi("场景提示词", "Scene prompt")}
          <input
            data-testid="visual-prompt-input"
            type="text"
            value={prompt}
            onChange={(event) => setPrompt(event.target.value)}
            placeholder={bi("暖光客厅场景 / warm living-room scene", "warm living-room scene")}
            className="rounded-lg border border-gray-200 bg-white px-2.5 py-1.5 text-[12.5px] font-normal text-gray-900 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-100"
          />
        </label>

        <div className="flex items-end sm:col-span-2">
          <button
            type="submit"
            data-testid="visual-submit"
            disabled={!canWrite || create.isPending}
            className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 px-3.5 py-2 text-[12.5px] font-semibold text-white transition-colors hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {create.isPending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
            ) : (
              <Sparkles className="h-3.5 w-3.5" aria-hidden="true" />
            )}
            {bi("开始加工图片", "Process image")}
          </button>
          {!canWrite && (
            <span className="ml-3 text-[12px] font-medium text-amber-700 dark:text-amber-400">
              {bi("当前角色只能查看加工结果。", "Your role can only review results.")}
            </span>
          )}
        </div>
      </form>

      {error && (
        <p
          role="alert"
          data-testid="visual-studio-error"
          className="mt-3 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-[12.5px] font-medium text-rose-800 dark:border-rose-800/70 dark:bg-rose-950/40 dark:text-rose-300"
        >
          {error}
        </p>
      )}

      <div className="mt-4">
        <h3 className="mb-2 text-[13px] font-semibold text-gray-800 dark:text-gray-200">
          {bi("最近加工记录", "Recent jobs")}
        </h3>
        {jobs.length === 0 ? (
          <EmptyState testId="visual-jobs-empty">
            {bi("还没有加工记录。", "No visual jobs yet.")}
          </EmptyState>
        ) : (
          <ul className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {jobs.map((job) => (
              <li
                key={job.id}
                data-testid={`visual-job-${job.id}`}
                className="overflow-hidden rounded-xl border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-900"
              >
                {job.output_image_url ? (
                  <img
                    data-testid={`visual-job-image-${job.id}`}
                    src={job.output_image_url}
                    alt={bi(
                      `${operationLabel(job.operation, locale)} 结果`,
                      `${operationLabel(job.operation, "en")} result`
                    )}
                    className="h-36 w-full bg-[repeating-conic-gradient(#f1f5f9_0%_25%,#ffffff_0%_50%)] bg-[length:16px_16px] object-contain"
                  />
                ) : (
                  <div className="grid h-36 w-full place-items-center bg-gray-50 text-gray-400 dark:bg-gray-800 dark:text-gray-500">
                    <ImagePlus className="h-6 w-6" aria-hidden="true" />
                  </div>
                )}
                <div className="space-y-1.5 p-3">
                  <div className="flex flex-wrap items-center justify-between gap-1.5">
                    <p className="truncate text-[12.5px] font-semibold text-gray-900 dark:text-gray-100">
                      {operationLabel(job.operation, locale)}
                    </p>
                    <Badge tone={job.status === "completed" ? "success" : job.status === "failed" ? "danger" : "neutral"}>
                      {VISUAL_JOB_STATUS_LABELS[job.status][locale]}
                    </Badge>
                  </div>
                  <p className="truncate text-[11.5px] text-gray-500 dark:text-gray-400">
                    {providerLabel(job.provider, locale)}
                    {job.target_locale ? ` · ${job.target_locale}` : ""}
                  </p>
                  {job.error_message && (
                    <p
                      data-testid={`visual-job-error-${job.id}`}
                      className="rounded-md bg-rose-50 px-2 py-1 text-[11px] font-medium text-rose-800 dark:bg-rose-950/40 dark:text-rose-300"
                    >
                      {job.error_message}
                    </p>
                  )}
                  <button
                    type="button"
                    data-testid={`visual-job-delete-${job.id}`}
                    disabled={!canWrite || remove.isPending}
                    onClick={() => remove.mutate(job.id)}
                    className="inline-flex items-center gap-1 rounded-md border border-gray-200 px-2 py-1 text-[11.5px] font-semibold text-gray-700 transition-colors hover:bg-gray-100 disabled:cursor-not-allowed disabled:opacity-40 dark:border-gray-700 dark:text-gray-300 dark:hover:bg-gray-800"
                  >
                    <Trash2 className="h-3 w-3" aria-hidden="true" />
                    {bi("删除", "Delete")}
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
