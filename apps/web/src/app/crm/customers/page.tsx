"use client";

import React, { useMemo, useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { CircleDollarSign, Globe2, Sparkles, Target, Users } from "lucide-react";

import { AppShell } from "@/components/app-shell";
import { BentoStatCard } from "@/components/ui/bento-stat-card";
import { Page, PageHeader, TableWrap } from "@/components/ui/page";
import { fetchPendingProposalsCount } from "@/lib/data-source";
import {
  buyerTypeLabel,
  clampIntentScore,
  fetchCustomerAiProfile,
  priceSensitivityLabel,
} from "@/lib/api-client/pack-e-autoreply";
import { formatMoney } from "@/lib/contracts";
import { fetchPhase3aCustomers } from "@/lib/phase3a-data";
import { useI18n } from "@/lib/i18n";
import { CrmAvatar, CrmStatusPill } from "../_components/crm-ui";

/**
 * Pack E：右侧详情面板里的精简版 AI 客户画像。
 * 无画像时引导去 Inbox 聚合沟通一键提炼（提炼接口需要会话上下文）。
 */
function AiProfileCard({ customerId }: { customerId: string }) {
  const { bi, locale } = useI18n();
  const profileQuery = useQuery({
    queryKey: ["ai", "customer-profile", customerId],
    queryFn: () => fetchCustomerAiProfile(customerId),
    retry: false,
  });
  const profile = profileQuery.data;
  const intentScore = clampIntentScore(profile?.purchase_intent_score);

  return (
    <section
      data-testid="crm-ai-profile-card"
      className="mt-3 rounded-xl border border-blue-200/70 bg-blue-50/40 p-3 dark:border-blue-500/30 dark:bg-blue-500/5"
    >
      <div className="flex items-center justify-between gap-2">
        <h3 className="inline-flex items-center gap-1 text-[12px] font-bold text-blue-900 dark:text-blue-200">
          <Sparkles className="h-3.5 w-3.5" />
          {bi("AI 客户画像", "AI buyer profile")}
        </h3>
        {profile && (
          <span className="rounded-full bg-white px-1.5 py-0.5 text-[10px] font-semibold text-blue-700 ring-1 ring-blue-200 dark:bg-blue-500/10 dark:text-blue-300 dark:ring-blue-500/30">
            {buyerTypeLabel(profile.buyer_type, locale)}
          </span>
        )}
      </div>

      {profile ? (
        <div className="mt-2 space-y-2 text-[11.5px]">
          <div>
            <div className="flex items-center justify-between gap-2">
              <span className="inline-flex items-center gap-1 font-medium text-slate-600 dark:text-gray-300">
                <Target className="h-3 w-3 text-blue-600 dark:text-blue-400" />
                {bi("采购意向", "Purchase intent")}
              </span>
              <span
                data-testid="crm-ai-intent-score"
                className="font-bold tabular-nums text-blue-700 dark:text-blue-300"
              >
                {intentScore}/100
              </span>
            </div>
            <div
              role="progressbar"
              aria-valuenow={intentScore}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-label={bi("采购意向分", "Purchase intent score")}
              className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-white dark:bg-gray-800"
            >
              <div
                className="h-full rounded-full bg-gradient-to-r from-blue-500 to-blue-600 dark:from-blue-400 dark:to-blue-500"
                style={{ width: `${intentScore}%` }}
              />
            </div>
          </div>

          <dl className="grid grid-cols-2 gap-1">
            {[
              [
                bi("预估采购额", "Est. order value"),
                profile.estimated_order_value_usd
                  ? formatMoney(profile.estimated_order_value_usd, "USD")
                  : "—",
              ],
              [bi("目标贸易术语", "Incoterm"), profile.target_incoterm ?? "—"],
              [
                bi("价格敏感度", "Price sensitivity"),
                priceSensitivityLabel(profile.price_sensitivity, locale),
              ],
              [
                bi("国家 / 语种", "Country / language"),
                `${profile.country_code ?? "—"} · ${(profile.preferred_language ?? "—").toUpperCase()}`,
              ],
            ].map(([term, value]) => (
              <div
                key={term}
                className="rounded-lg bg-white/80 px-2 py-1 dark:bg-gray-900/60"
              >
                <dt className="text-[10px] font-medium text-slate-500 dark:text-gray-400">
                  {term}
                </dt>
                <dd className="truncate font-semibold text-slate-900 dark:text-gray-100">
                  {value}
                </dd>
              </div>
            ))}
          </dl>

          {(profile.interested_skus ?? []).length > 0 && (
            <div className="flex flex-wrap gap-1">
              {(profile.interested_skus ?? []).map((sku) => (
                <span
                  key={sku}
                  className="rounded-md bg-white px-1.5 py-0.5 text-[10.5px] font-semibold text-blue-700 ring-1 ring-blue-200 dark:bg-blue-500/10 dark:text-blue-300 dark:ring-blue-500/30"
                >
                  {sku}
                </span>
              ))}
            </div>
          )}

          {profile.next_best_action && (
            <p
              data-testid="crm-ai-next-best-action"
              className="rounded-lg bg-white/80 px-2 py-1.5 leading-relaxed text-slate-700 dark:bg-gray-900/60 dark:text-gray-300"
            >
              <span className="font-semibold text-blue-800 dark:text-blue-300">
                {bi("下一步建议", "Next best action")}：
              </span>
              {profile.next_best_action}
            </p>
          )}
        </div>
      ) : (
        <p className="mt-2 text-[11.5px] font-medium text-slate-600 dark:text-gray-400">
          {profileQuery.isLoading
            ? bi("正在读取画像…", "Loading profile…")
            : bi("暂无 AI 画像。", "No AI profile yet.")}{" "}
          <Link
            data-testid="crm-ai-profile-extract-link"
            href="/inbox"
            className="font-semibold text-blue-700 underline-offset-2 hover:underline dark:text-blue-300"
          >
            {bi("去聚合沟通一键提炼 →", "Extract from the inbox thread →")}
          </Link>
        </p>
      )}
    </section>
  );
}

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

  const summary = useMemo(() => {
    const ltv = customers.reduce((total, customer) => total + Number(customer.ltv_usd || 0), 0);
    const countries = new Set(customers.map((c) => c.country_code).filter(Boolean));
    const quiet = customers.filter((c) => c.buyer_local_time.is_quiet_hours).length;
    return { ltv, countries: countries.size, quiet };
  }, [customers]);

  return (
    <AppShell currentPath="/crm/customers" pendingProposalsCount={proposalsQuery.data?.total ?? 0}>
      <Page>
        <PageHeader
          title={bi("客户档案", "Customers")}
          description={bi(
            "跨渠道身份合流后的统一客户档案，包含买家当地时区与生命周期阶段。",
            "Unified customer profiles after cross-channel identity merge, including buyer local time and lifecycle stage."
          )}
        />

        <section
          aria-label={bi("客户关键指标", "Customer key metrics")}
          className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"
        >
          <BentoStatCard
            label={bi("客户总数", "Total customers")}
            value={customers.length}
            icon={Users}
            accent="blue"
            hint={bi("身份合流后的唯一主体", "Deduplicated after identity merge")}
          />
          <BentoStatCard
            label={bi("累计 LTV", "Lifetime value")}
            value={formatMoney(summary.ltv.toFixed(2), "USD")}
            icon={CircleDollarSign}
            accent="emerald"
            hint={bi("按 USD 统一折算", "Normalised to USD")}
          />
          <BentoStatCard
            label={bi("覆盖国家", "Countries")}
            value={summary.countries}
            icon={Globe2}
            accent="violet"
            hint={bi("用于静默时段与币种判断", "Drives quiet hours and currency")}
          />
          <BentoStatCard
            label={bi("静默时段客户", "In quiet hours")}
            value={summary.quiet}
            icon={Globe2}
            accent="amber"
            hint={bi("21:00–08:00 不主动营销触达", "No proactive outreach 21:00–08:00")}
          />
        </section>

        {customersQuery.isError && (
          <p
            role="alert"
            className="rounded-xl border border-rose-200 bg-rose-50 p-2.5 text-[12px] font-medium text-rose-800"
          >
            {customersQuery.error.message}
          </p>
        )}

        <div className="grid gap-4 xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
          <TableWrap className="rounded-2xl">
            <table data-testid="customers-table" className="erp-table">
              <thead>
                <tr>
                  <th>{bi("客户", "Customer")}</th>
                  <th>{bi("类型", "Type")}</th>
                  <th>{bi("国家 / 时区", "Country / timezone")}</th>
                  <th className="text-right">LTV</th>
                </tr>
              </thead>
              <tbody>
                {customers.map((customer) => (
                  <tr
                    key={customer.id}
                    data-testid={`customer-row-${customer.id}`}
                    onClick={() => setSelectedId(customer.id)}
                    className={`cursor-pointer ${
                      selected?.id === customer.id ? "bg-blue-50" : ""
                    }`}
                  >
                    <td>
                      <div className="flex items-center gap-2.5">
                        <CrmAvatar name={customer.display_name} size="sm" />
                        <div className="min-w-0">
                          <div className="font-medium text-slate-900">{customer.display_name}</div>
                          <div className="text-[11px] text-slate-500">
                            {customer.primary_email ?? "—"}
                          </div>
                        </div>
                      </div>
                    </td>
                    <td>
                      <span className="rounded-md bg-slate-100 px-1.5 py-0.5 text-[11px] font-medium text-slate-700">
                        {customer.customer_type}
                      </span>
                    </td>
                    <td className="text-slate-700">
                      {customer.country_code ?? "—"}
                      <span className="text-slate-400"> · </span>
                      <span className="text-[11.5px] text-slate-500">
                        {customer.iana_timezone ?? "—"}
                      </span>
                    </td>
                    <td className="text-right font-semibold tabular-nums text-slate-900">
                      {formatMoney(customer.ltv_usd, "USD")}
                    </td>
                  </tr>
                ))}
                {customers.length === 0 && (
                  <tr>
                    <td colSpan={4} className="px-3 py-6 text-center font-medium text-slate-500">
                      {customersQuery.isLoading
                        ? bi("加载中…", "Loading…")
                        : bi("暂无客户", "No customers")}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </TableWrap>

          <aside
            data-testid="customer-detail"
            className="erp-card h-fit overflow-hidden p-0 text-[13px]"
          >
            <div className="border-b border-slate-200/90 bg-slate-50/80 px-4 py-3">
              <h2 className="text-[13.5px] font-bold text-slate-900">
                {bi("客户详情", "Customer detail")}
              </h2>
            </div>
            {selected ? (
              <div className="p-4">
                <div className="flex items-center gap-3">
                  <CrmAvatar name={selected.display_name} size="lg" />
                  <div className="min-w-0">
                    <div className="truncate text-[13.5px] font-bold text-slate-900">
                      {selected.display_name}
                    </div>
                    <div className="mt-1 flex flex-wrap items-center gap-1.5">
                      <CrmStatusPill status={selected.lifecycle_stage} />
                      {selected.buyer_local_time.is_quiet_hours && (
                        <span className="rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-[10.5px] font-semibold text-amber-800">
                          {bi("静默时段", "Quiet hours")}
                        </span>
                      )}
                    </div>
                  </div>
                </div>

                <dl className="mt-3.5 space-y-1.5">
                  {[
                    [bi("邮箱", "Email"), selected.primary_email ?? "—"],
                    [bi("电话", "Phone"), selected.primary_phone_e164 ?? "—"],
                    [bi("语言", "Language"), selected.preferred_language],
                    [bi("当地时间", "Local time"), selected.buyer_local_time.formatted_time ?? "—"],
                    [
                      bi("休息时段", "Quiet hours"),
                      selected.buyer_local_time.is_quiet_hours ? bi("是", "Yes") : bi("否", "No"),
                    ],
                    [bi("累计 LTV", "Lifetime value"), formatMoney(selected.ltv_usd, "USD")],
                  ].map(([term, value]) => (
                    <div
                      key={term}
                      className="flex items-baseline justify-between gap-2 rounded-lg bg-slate-50/80 px-2.5 py-1.5"
                    >
                      <dt className="text-[11.5px] font-medium text-slate-600">{term}</dt>
                      <dd className="truncate text-[12px] font-semibold text-slate-900">{value}</dd>
                    </div>
                  ))}
                </dl>

                <AiProfileCard customerId={selected.id} />
              </div>
            ) : (
              <p className="p-4 text-[12.5px] font-medium text-slate-500">
                {bi("选择左侧客户查看详情", "Select a customer to see details")}
              </p>
            )}
          </aside>
        </div>
      </Page>
    </AppShell>
  );
}
