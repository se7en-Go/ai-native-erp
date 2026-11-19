"use client";

import React, { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { AppShell } from "@/components/app-shell";
import { fetchPendingProposalsCount } from "@/lib/data-source";
import { formatMoney } from "@/lib/contracts";
import { fetchPhase3aCustomers } from "@/lib/phase3a-data";

export default function CrmCustomersPage() {
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const proposalsQuery = useQuery({
    queryKey: ["ai", "proposals", "pending-count"],
    queryFn: () => fetchPendingProposalsCount(),
  });
  const customersQuery = useQuery({
    queryKey: ["crm", "customers"],
    queryFn: () => fetchPhase3aCustomers(),
  });

  const customers = customersQuery.data?.items ?? [];
  const selected = customers.find((customer) => customer.id === (selectedId ?? customers[0]?.id));

  return (
    <AppShell currentPath="/crm/customers" pendingProposalsCount={proposalsQuery.data?.total ?? 0}>
      <div className="mx-auto max-w-6xl space-y-4">
        <div>
          <h1 className="text-lg font-bold text-slate-900">客户档案 / Customers</h1>
          <p className="text-xs text-slate-500">
            跨渠道身份合流后的统一客户档案，包含买家当地时区与生命周期阶段。
          </p>
        </div>

        {customersQuery.isError && (
          <p role="alert" className="rounded border border-rose-200 bg-rose-50 p-2 text-xs text-rose-800">
            {customersQuery.error.message}
          </p>
        )}

        <div className="grid gap-3 lg:grid-cols-[2fr_1fr]">
          <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
            <table data-testid="customers-table" className="w-full text-left text-xs">
              <thead className="border-b border-slate-200 bg-slate-50 font-semibold text-slate-600">
                <tr>
                  <th className="px-3 py-2.5">客户</th>
                  <th className="px-3 py-2.5">类型</th>
                  <th className="px-3 py-2.5">国家 / 时区</th>
                  <th className="px-3 py-2.5">LTV</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {customers.map((customer) => (
                  <tr
                    key={customer.id}
                    data-testid={`customer-row-${customer.id}`}
                    onClick={() => setSelectedId(customer.id)}
                    className={`cursor-pointer ${
                      selected?.id === customer.id ? "bg-indigo-50" : "hover:bg-slate-50"
                    }`}
                  >
                    <td className="px-3 py-2">
                      <div className="font-medium text-slate-800">{customer.display_name}</div>
                      <div className="text-slate-500">{customer.primary_email ?? "—"}</div>
                    </td>
                    <td className="px-3 py-2 text-slate-600">{customer.customer_type}</td>
                    <td className="px-3 py-2 text-slate-600">
                      {customer.country_code ?? "—"} · {customer.iana_timezone ?? "—"}
                    </td>
                    <td className="px-3 py-2 text-slate-600">
                      {formatMoney(customer.ltv_usd, "USD")}
                    </td>
                  </tr>
                ))}
                {customers.length === 0 && (
                  <tr>
                    <td colSpan={4} className="px-3 py-6 text-center text-slate-400">
                      {customersQuery.isLoading ? "加载中…" : "暂无客户"}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          <aside
            data-testid="customer-detail"
            className="rounded-lg border border-slate-200 bg-white p-4 text-xs"
          >
            <h2 className="text-sm font-semibold text-slate-900">客户详情</h2>
            {selected ? (
              <dl className="mt-2 space-y-1.5 text-slate-600">
                <div className="flex justify-between gap-2">
                  <dt>姓名</dt>
                  <dd className="font-medium text-slate-800">{selected.display_name}</dd>
                </div>
                <div className="flex justify-between gap-2">
                  <dt>邮箱</dt>
                  <dd className="truncate">{selected.primary_email ?? "—"}</dd>
                </div>
                <div className="flex justify-between gap-2">
                  <dt>电话</dt>
                  <dd>{selected.primary_phone_e164 ?? "—"}</dd>
                </div>
                <div className="flex justify-between gap-2">
                  <dt>语言</dt>
                  <dd>{selected.preferred_language}</dd>
                </div>
                <div className="flex justify-between gap-2">
                  <dt>生命周期</dt>
                  <dd>{selected.lifecycle_stage}</dd>
                </div>
                <div className="flex justify-between gap-2">
                  <dt>当地时间</dt>
                  <dd>{selected.buyer_local_time.formatted_time ?? "—"}</dd>
                </div>
                <div className="flex justify-between gap-2">
                  <dt>休息时段</dt>
                  <dd>{selected.buyer_local_time.is_quiet_hours ? "是" : "否"}</dd>
                </div>
              </dl>
            ) : (
              <p className="mt-2 text-slate-400">选择左侧客户查看详情</p>
            )}
          </aside>
        </div>
      </div>
    </AppShell>
  );
}
