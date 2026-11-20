"use client";

import React, { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { AppShell } from "@/components/app-shell";
import { fetchPendingProposalsCount } from "@/lib/data-source";
import { formatMoney } from "@/lib/contracts";
import { fetchPhase3aCustomers } from "@/lib/phase3a-data";
import { useI18n } from "@/lib/i18n";

export default function CrmCustomersPage() {
  const { bi } = useI18n();
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
      <div className="erp-page space-y-4">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-slate-900">{bi("客户档案", "Customers")}</h1>
          <p className="text-xs text-slate-500">
            {bi("跨渠道身份合流后的统一客户档案，包含买家当地时区与生命周期阶段。", "Unified customer profiles after cross-channel identity merge, including buyer local time and lifecycle stage.")}
          </p>
        </div>

        {customersQuery.isError && (
          <p role="alert" className="rounded border border-rose-200 bg-rose-50 p-2 text-xs text-rose-800">
            {customersQuery.error.message}
          </p>
        )}

        <div className="grid gap-4 xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
          <div className="overflow-x-auto rounded-xl border border-slate-200/80 bg-white">
            <table data-testid="customers-table" className="erp-table">
              <thead className="border-b border-slate-200 bg-slate-50 font-semibold text-slate-600">
                <tr>
                  <th>{bi("客户", "Customer")}</th>
                  <th>{bi("类型", "Type")}</th>
                  <th>{bi("国家 / 时区", "Country / timezone")}</th>
                  <th className="text-right">LTV</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {customers.map((customer) => (
                  <tr
                    key={customer.id}
                    data-testid={`customer-row-${customer.id}`}
                    onClick={() => setSelectedId(customer.id)}
                    className={`cursor-pointer ${
                      selected?.id === customer.id ? "bg-blue-50" : "hover:bg-slate-50"
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
                      {customersQuery.isLoading ? bi("加载中…", "Loading…") : bi("暂无客户", "No customers")}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          <aside
            data-testid="customer-detail"
            className="erp-card p-4 text-[13px]"
          >
            <h2 className="text-sm font-semibold text-slate-900">{bi("客户详情", "Customer detail")}</h2>
            {selected ? (
              <dl className="mt-2 space-y-1.5 text-slate-600">
                <div className="flex justify-between gap-2">
                  <dt>{bi("姓名", "Name")}</dt>
                  <dd className="font-medium text-slate-800">{selected.display_name}</dd>
                </div>
                <div className="flex justify-between gap-2">
                  <dt>{bi("邮箱", "Email")}</dt>
                  <dd className="truncate">{selected.primary_email ?? "—"}</dd>
                </div>
                <div className="flex justify-between gap-2">
                  <dt>{bi("电话", "Phone")}</dt>
                  <dd>{selected.primary_phone_e164 ?? "—"}</dd>
                </div>
                <div className="flex justify-between gap-2">
                  <dt>{bi("语言", "Language")}</dt>
                  <dd>{selected.preferred_language}</dd>
                </div>
                <div className="flex justify-between gap-2">
                  <dt>{bi("生命周期", "Lifecycle")}</dt>
                  <dd>{selected.lifecycle_stage}</dd>
                </div>
                <div className="flex justify-between gap-2">
                  <dt>{bi("当地时间", "Local time")}</dt>
                  <dd>{selected.buyer_local_time.formatted_time ?? "—"}</dd>
                </div>
                <div className="flex justify-between gap-2">
                  <dt>{bi("休息时段", "Quiet hours")}</dt>
                  <dd>{selected.buyer_local_time.is_quiet_hours ? bi("是", "Yes") : bi("否", "No")}</dd>
                </div>
              </dl>
            ) : (
              <p className="mt-2 text-slate-400">{bi("选择左侧客户查看详情", "Select a customer to see details")}</p>
            )}
          </aside>
        </div>
      </div>
    </AppShell>
  );
}
