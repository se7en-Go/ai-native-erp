"use client";

import React, { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Building2, Factory, Globe2, Timer } from "lucide-react";

import { AppShell } from "@/components/app-shell";
import { BentoStatCard } from "@/components/ui/bento-stat-card";
import { EmptyState, Page, PageHeader, TableWrap } from "@/components/ui/page";
import type { SupplierCreate } from "@/lib/api-client/types.gen";
import { fetchPendingProposalsCount } from "@/lib/data-source";
import { addPhase3aSupplier, fetchPhase3aSuppliers, patchPhase3aSupplier } from "@/lib/phase3a-data";
import { useI18n } from "@/lib/i18n";

const EMPTY_FORM = {
  code: "",
  name: "",
  contact_name: "",
  contact_email: "",
  country: "",
  lead_time_days: "14",
};

export default function SuppliersPage() {
  const { bi } = useI18n();
  const queryClient = useQueryClient();
  const [form, setForm] = useState(EMPTY_FORM);

  const proposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () => fetchPendingProposalsCount(),
  });
  const suppliersQuery = useQuery({
    queryKey: ["supply-chain", "suppliers"],
    queryFn: () => fetchPhase3aSuppliers(),
  });
  const createSupplier = useMutation({
    mutationFn: (payload: SupplierCreate) => addPhase3aSupplier(payload),
    onSuccess: () => {
      setForm(EMPTY_FORM);
      void queryClient.invalidateQueries({ queryKey: ["supply-chain", "suppliers"] });
    },
  });
  const toggleStatus = useMutation({
    mutationFn: ({ id, status }: { id: string; status: "active" | "inactive" }) =>
      patchPhase3aSupplier(id, { status }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["supply-chain", "suppliers"] }),
  });

  const suppliers = suppliersQuery.data?.items ?? [];
  const canSubmit = form.code.trim().length > 0 && form.name.trim().length > 0;

  const summary = useMemo(() => {
    const active = suppliers.filter((s) => (s.status ?? "active") === "active").length;
    const countries = new Set(
      suppliers.map((s) => (s as { country?: string | null }).country).filter(Boolean)
    );
    const leadTimes = suppliers
      .map((s) => Number(s.lead_time_days ?? 0))
      .filter((days) => Number.isFinite(days) && days > 0);
    const avgLead =
      leadTimes.length === 0
        ? 0
        : Math.round(leadTimes.reduce((a, b) => a + b, 0) / leadTimes.length);
    return { active, countries: countries.size, avgLead };
  }, [suppliers]);

  return (
    <AppShell currentPath="/supply-chain/suppliers" pendingProposalsCount={proposalsQuery.data?.total ?? 0}>
      <Page>
        <PageHeader
          title={bi("供应商名录", "Suppliers")}
          description={bi(
            "供应商代码在租户内唯一；停用（inactive）后不可再开新采购单，历史单据保留。",
            "Supplier codes are unique per tenant; inactive suppliers cannot open new purchase orders while history is retained."
          )}
        />

        <section
          aria-label={bi("供应商关键指标", "Supplier key metrics")}
          className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"
        >
          <BentoStatCard
            label={bi("供应商总数", "Total suppliers")}
            value={suppliers.length}
            icon={Building2}
            accent="blue"
            hint={bi("代码在租户内唯一", "Codes are unique per tenant")}
          />
          <BentoStatCard
            label={bi("合作中", "Active")}
            value={summary.active}
            icon={Factory}
            accent="emerald"
            badge={`${suppliers.length - summary.active} ${bi("停用", "inactive")}`}
            badgeTone="slate"
            hint={bi("停用后不可开新采购单", "Inactive suppliers cannot open new POs")}
          />
          <BentoStatCard
            label={bi("平均交期", "Average lead time")}
            value={`${summary.avgLead} ${bi("天", "days")}`}
            icon={Timer}
            accent="amber"
            hint={bi("用于采购单预计到货推算", "Drives purchase order ETA")}
          />
          <BentoStatCard
            label={bi("覆盖国家", "Countries covered")}
            value={summary.countries}
            icon={Globe2}
            accent="violet"
            hint={bi("按供应商登记国家统计", "Counted from registered countries")}
          />
        </section>

        <form
          data-testid="supplier-form"
          onSubmit={(event) => {
            event.preventDefault();
            createSupplier.mutate({
              code: form.code.trim(),
              name: form.name.trim(),
              contact_name: form.contact_name.trim() || null,
              contact_email: form.contact_email.trim() || null,
              country: form.country.trim().toUpperCase() || null,
              lead_time_days: Number(form.lead_time_days) || 0,
            });
          }}
          className="erp-card grid gap-2 p-3.5 sm:grid-cols-6"
        >
          <input
            aria-label="Supplier code"
            required
            value={form.code}
            onChange={(event) => setForm({ ...form, code: event.target.value })}
            placeholder="编码"
            className="rounded border border-slate-300 px-2 py-1 text-xs"
          />
          <input
            aria-label="Supplier name"
            required
            value={form.name}
            onChange={(event) => setForm({ ...form, name: event.target.value })}
            placeholder="供应商名称"
            className="rounded border border-slate-300 px-2 py-1 text-xs"
          />
          <input
            aria-label="Contact name"
            value={form.contact_name}
            onChange={(event) => setForm({ ...form, contact_name: event.target.value })}
            placeholder="联系人"
            className="rounded border border-slate-300 px-2 py-1 text-xs"
          />
          <input
            aria-label="Contact email"
            type="email"
            value={form.contact_email}
            onChange={(event) => setForm({ ...form, contact_email: event.target.value })}
            placeholder="邮箱"
            className="rounded border border-slate-300 px-2 py-1 text-xs"
          />
          <input
            aria-label="Country"
            maxLength={2}
            value={form.country}
            onChange={(event) => setForm({ ...form, country: event.target.value })}
            placeholder="国家 (CN)"
            className="rounded border border-slate-300 px-2 py-1 text-xs"
          />
          <button
            type="submit"
            data-testid="supplier-submit"
            disabled={!canSubmit || createSupplier.isPending}
            className="erp-btn erp-btn-primary py-1 disabled:opacity-40"
          >
            {bi("新建供应商", "Create supplier")}
          </button>
          {createSupplier.isError && (
            <p role="alert" className="sm:col-span-6 text-xs text-rose-700">
              {createSupplier.error.message}
            </p>
          )}
        </form>

        <TableWrap className="rounded-2xl">
          <table data-testid="suppliers-table" className="erp-table">
            <thead className="border-b border-slate-200 bg-slate-50 font-semibold text-slate-600">
              <tr>
                <th>{bi("编码 / 名称", "Code / name")}</th>
                <th>{bi("联系人", "Contact")}</th>
                <th className="text-right">{bi("交期（天）", "Lead time (days)")}</th>
                <th>{bi("状态", "Status")}</th>
                <th className="text-right">{bi("操作", "Action")}</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {suppliers.map((supplier) => (
                <tr key={supplier.id} data-testid={`supplier-row-${supplier.id}`}>
                  <td className="px-3 py-2">
                    <div className="flex items-center gap-2">
                      <span
                        aria-hidden="true"
                        className="grid h-7 w-7 shrink-0 place-items-center rounded-lg bg-violet-50 text-violet-600 ring-1 ring-violet-100"
                      >
                        <Building2 className="h-3.5 w-3.5" />
                      </span>
                      <div className="min-w-0">
                        <div className="font-medium text-slate-900">{supplier.name}</div>
                        <div className="font-mono text-[11px] text-slate-500">{supplier.code}</div>
                      </div>
                    </div>
                  </td>
                  <td className="px-3 py-2 text-slate-700">
                    {supplier.contact_name ?? "—"}
                    <div className="text-[11px] text-slate-500">{supplier.contact_email ?? ""}</div>
                  </td>
                  <td className="px-3 py-2 font-medium tabular-nums text-slate-700">
                    {supplier.lead_time_days ?? 0}
                  </td>
                  <td className="px-3 py-2">
                    <span
                      className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[10.5px] font-semibold ${
                        (supplier.status ?? "active") === "active"
                          ? "border-emerald-200 bg-emerald-50 text-emerald-700"
                          : "border-slate-200 bg-slate-100 text-slate-600"
                      }`}
                    >
                      <span
                        aria-hidden="true"
                        className={`h-1.5 w-1.5 rounded-full ${
                          (supplier.status ?? "active") === "active"
                            ? "bg-emerald-500"
                            : "bg-slate-400"
                        }`}
                      />
                      {supplier.status ?? "active"}
                    </span>
                  </td>
                  <td className="px-3 py-2">
                    <button
                      type="button"
                      data-testid={`toggle-supplier-${supplier.id}`}
                      onClick={() =>
                        toggleStatus.mutate({
                          id: supplier.id,
                          status: supplier.status === "active" ? "inactive" : "active",
                        })
                      }
                      className="rounded-lg border border-slate-200 bg-white px-2 py-1 text-[11px] font-semibold text-slate-700 transition-colors hover:border-slate-300 hover:bg-slate-50"
                    >
                      {supplier.status === "active" ? bi("停用", "Disable") : bi("启用", "Enable")}
                    </button>
                  </td>
                </tr>
              ))}
              {suppliers.length === 0 && (
                <tr>
                  <td colSpan={5} className="px-3 py-6 text-center text-slate-400">
                    {suppliersQuery.isLoading ? bi("加载中…", "Loading…") : bi("暂无供应商", "No suppliers")}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </TableWrap>
        {suppliers.length === 0 && (
          <EmptyState>
            {suppliersQuery.isLoading
              ? bi("正在加载供应商…", "Loading suppliers…")
              : bi("暂无供应商，请先新建一家。", "No suppliers yet, create the first one.")}
          </EmptyState>
        )}
      </Page>
    </AppShell>
  );
}
