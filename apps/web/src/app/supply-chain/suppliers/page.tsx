"use client";

import React, { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { AppShell } from "@/components/app-shell";
import type { SupplierCreate } from "@/lib/api-client/types.gen";
import { fetchPendingProposalsCount } from "@/lib/data-source";
import { addPhase3aSupplier, fetchPhase3aSuppliers, patchPhase3aSupplier } from "@/lib/phase3a-data";

const EMPTY_FORM = {
  code: "",
  name: "",
  contact_name: "",
  contact_email: "",
  country: "",
  lead_time_days: "14",
};

export default function SuppliersPage() {
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

  return (
    <AppShell currentPath="/supply-chain/suppliers" pendingProposalsCount={proposalsQuery.data?.total ?? 0}>
      <div className="mx-auto max-w-6xl space-y-4">
        <div>
          <h1 className="text-lg font-bold text-slate-900">供应商名录 / Suppliers</h1>
          <p className="text-xs text-slate-500">
            供应商代码在租户内唯一；停用（inactive）后不可再开新采购单，历史单据保留。
          </p>
        </div>

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
          className="grid gap-2 rounded-lg border border-slate-200 bg-white p-3 sm:grid-cols-6"
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
            className="rounded bg-indigo-600 px-3 py-1 text-xs font-medium text-white hover:bg-indigo-700 disabled:opacity-40"
          >
            新建供应商
          </button>
          {createSupplier.isError && (
            <p role="alert" className="sm:col-span-6 text-xs text-rose-700">
              {createSupplier.error.message}
            </p>
          )}
        </form>

        <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
          <table data-testid="suppliers-table" className="w-full text-left text-xs">
            <thead className="border-b border-slate-200 bg-slate-50 font-semibold text-slate-600">
              <tr>
                <th className="px-3 py-2.5">编码 / 名称</th>
                <th className="px-3 py-2.5">联系人</th>
                <th className="px-3 py-2.5">交期 (天)</th>
                <th className="px-3 py-2.5">状态</th>
                <th className="px-3 py-2.5">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {suppliers.map((supplier) => (
                <tr key={supplier.id} data-testid={`supplier-row-${supplier.id}`}>
                  <td className="px-3 py-2">
                    <div className="font-medium text-slate-800">{supplier.name}</div>
                    <div className="text-slate-500">{supplier.code}</div>
                  </td>
                  <td className="px-3 py-2 text-slate-600">
                    {supplier.contact_name ?? "—"}
                    <div className="text-slate-400">{supplier.contact_email ?? ""}</div>
                  </td>
                  <td className="px-3 py-2 text-slate-600">{supplier.lead_time_days ?? 0}</td>
                  <td className="px-3 py-2">
                    <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] uppercase text-slate-700">
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
                      className="rounded border border-slate-200 px-2 py-1 text-[11px] font-medium text-slate-700 hover:bg-slate-100"
                    >
                      {supplier.status === "active" ? "停用" : "启用"}
                    </button>
                  </td>
                </tr>
              ))}
              {suppliers.length === 0 && (
                <tr>
                  <td colSpan={5} className="px-3 py-6 text-center text-slate-400">
                    {suppliersQuery.isLoading ? "加载中…" : "暂无供应商"}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </AppShell>
  );
}
