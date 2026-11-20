"use client";

import React, { useMemo, useState } from "react";
import {
  CheckCircle2,
  ExternalLink,
  Link2,
  Loader2,
  LockKeyhole,
  Pencil,
  Unplug,
  Users,
  X,
} from "lucide-react";

import { useI18n } from "@/lib/i18n";
import type {
  ChannelAccountApiItem,
  ChannelVisibilityScope,
  TeamMemberApiItem,
  UpdateChannelOwnershipPayload,
} from "@/lib/api-client/live-client";

export type AccountsFilter = "all" | "mine" | "shared";

const FILTERS: Array<{ value: AccountsFilter; zh: string; en: string }> = [
  { value: "all", zh: "全部", en: "All" },
  { value: "mine", zh: "我的个人渠道", en: "My personal" },
  { value: "shared", zh: "公司公海", en: "Company shared" },
];

/** 归属胶囊文案：personal 显示归属人，shared 显示公海。 */
export function channelOwnershipLabel(
  account: Pick<ChannelAccountApiItem, "visibility_scope" | "owner_name" | "owner_email">,
): { zh: string; en: string; personal: boolean } {
  const owner = account.owner_name || account.owner_email || "";
  if (account.visibility_scope === "personal") {
    return {
      zh: owner ? `个人专属 · ${owner}` : "个人专属",
      en: owner ? `Personal · ${owner}` : "Personal",
      personal: true,
    };
  }
  return { zh: "公司公海", en: "Company shared", personal: false };
}

export type ConnectedAccountsCardProps = {
  accounts: ChannelAccountApiItem[];
  isAdmin: boolean;
  currentUserId?: string | null;
  members?: TeamMemberApiItem[];
  filter: AccountsFilter;
  onFilterChange: (filter: AccountsFilter) => void;
  onChangeOwnership: (
    accountId: string,
    payload: UpdateChannelOwnershipPayload,
  ) => Promise<void>;
  onDisconnect: (accountId: string) => Promise<void>;
};

/**
 * 已接入账号列表：归属徽标 + Telegram 机器人直链 + 管理员切换归属 / 断开连接。
 * 业务员只会在**自己**的个人渠道上看到断开按钮。
 */
export function ConnectedAccountsCard({
  accounts,
  isAdmin,
  currentUserId,
  members = [],
  filter,
  onFilterChange,
  onChangeOwnership,
  onDisconnect,
}: ConnectedAccountsCardProps) {
  const { bi } = useI18n();
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editScope, setEditScope] = useState<ChannelVisibilityScope>("shared");
  const [editOwner, setEditOwner] = useState("");
  const [reassign, setReassign] = useState(true);
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const visible = useMemo(() => {
    if (filter === "mine") {
      return accounts.filter(
        (account) =>
          account.visibility_scope === "personal" &&
          (!currentUserId || account.owner_user_id === currentUserId),
      );
    }
    if (filter === "shared") {
      return accounts.filter(
        (account) => account.visibility_scope !== "personal",
      );
    }
    return accounts;
  }, [accounts, currentUserId, filter]);

  const activeMembers = members.filter((member) => member.is_active);

  function startEdit(account: ChannelAccountApiItem) {
    setEditingId(account.id);
    setEditScope(account.visibility_scope === "personal" ? "personal" : "shared");
    setEditOwner(account.owner_user_id ?? "");
    setReassign(true);
    setError(null);
  }

  async function saveEdit(accountId: string) {
    if (editScope === "personal" && !editOwner) {
      setError(bi("请先选择归属业务员。", "Pick the owning salesperson first."));
      return;
    }
    setPendingId(accountId);
    setError(null);
    try {
      await onChangeOwnership(accountId, {
        visibility_scope: editScope,
        owner_user_id: editScope === "personal" ? editOwner : null,
        reassign_open_conversations: reassign,
      });
      setEditingId(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "ERR_OWNERSHIP_UPDATE_FAILED");
    } finally {
      setPendingId(null);
    }
  }

  async function disconnect(accountId: string) {
    setPendingId(accountId);
    setError(null);
    try {
      await onDisconnect(accountId);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "ERR_CHANNEL_DISCONNECT_FAILED");
    } finally {
      setPendingId(null);
    }
  }

  return (
    <section
      data-testid="connected-accounts-card"
      className="rounded-2xl border border-slate-200/90 bg-white shadow-[0_1px_2px_rgba(15,23,42,0.04)]"
    >
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200/90 px-4 py-3">
        <div>
          <h2 className="flex items-center gap-2 text-[15px] font-semibold text-slate-950">
            <Link2 className="h-4 w-4 text-emerald-700" />
            {bi("已接入渠道账号", "Connected channel accounts")}
          </h2>
          <p className="mt-1 text-[12.5px] leading-5 text-slate-600">
            {isAdmin
              ? bi(
                  "公海渠道由团队共同接管；个人专属渠道只对归属人与主管可见，可随时切换归属。",
                  "Shared channels are handled by the team; personal channels stay with their owner and managers and can be reassigned."
                )
              : bi(
                  "这里只显示你可见的渠道；个人专属渠道的客户消息只进你的 Inbox。",
                  "Only channels visible to you are listed; personal channel replies land in your inbox."
                )}
          </p>
        </div>
        <div
          role="tablist"
          aria-label={bi("渠道归属筛选", "Ownership filter")}
          className="inline-flex items-center gap-0.5 rounded-xl border border-[#E5E2DC] bg-[#EFECE6]/90 p-1"
        >
          {FILTERS.map((option) => {
            const active = filter === option.value;
            return (
              <button
                key={option.value}
                type="button"
                role="tab"
                aria-selected={active}
                data-testid={`accounts-filter-${option.value}`}
                onClick={() => onFilterChange(option.value)}
                className={`rounded-lg px-2.5 py-1 text-[11.5px] font-semibold transition-colors ${
                  active ? "bg-[#18181B] text-[#FAFAF9]" : "text-stone-600 hover:text-zinc-950"
                }`}
              >
                {bi(option.zh, option.en)}
              </button>
            );
          })}
        </div>
      </div>

      <div className="space-y-2 p-4">
        {visible.length === 0 && (
          <div className="rounded-xl border border-dashed border-slate-300 bg-slate-50 px-3 py-6 text-center text-[12px] font-medium text-slate-600">
            {bi("该归属下还没有渠道账号。", "No channel accounts under this ownership.")}
          </div>
        )}

        {visible.map((account) => {
          const ownership = channelOwnershipLabel(account);
          const isOwnPersonal =
            Boolean(currentUserId) && account.owner_user_id === currentUserId;
          const canDisconnect = isAdmin || (ownership.personal && isOwnPersonal);
          const editing = editingId === account.id;
          const busy = pendingId === account.id;

          return (
            <div
              key={account.id}
              data-testid={`channel-account-row-${account.id}`}
              className="rounded-xl border border-slate-200 bg-slate-50/60 px-3 py-2.5"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex min-w-0 flex-wrap items-center gap-2">
                  <span className="grid h-7 w-7 shrink-0 place-items-center rounded-lg bg-white text-emerald-700 ring-1 ring-slate-200">
                    <Link2 className="h-3.5 w-3.5" />
                  </span>
                  <span className="truncate text-[13px] font-semibold text-slate-900">
                    {account.display_name}
                  </span>
                  <span className="rounded-md bg-white px-1.5 py-0.5 text-[10.5px] font-semibold text-slate-600 ring-1 ring-slate-200">
                    {account.channel_type}
                  </span>
                  <span
                    data-testid={`account-ownership-badge-${account.id}`}
                    className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10.5px] font-semibold ${
                      ownership.personal
                        ? "border-emerald-200 bg-emerald-50 text-emerald-800"
                        : "border-slate-200 bg-slate-100 text-slate-700"
                    }`}
                  >
                    {ownership.personal ? (
                      <LockKeyhole className="h-2.5 w-2.5" />
                    ) : (
                      <Users className="h-2.5 w-2.5" />
                    )}
                    {bi(ownership.zh, ownership.en)}
                  </span>
                  {account.bot_username && (
                    <a
                      data-testid={`account-bot-link-${account.id}`}
                      href={`https://t.me/${account.bot_username}`}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex items-center gap-1 rounded-full border border-sky-200 bg-sky-50 px-2 py-0.5 text-[10.5px] font-semibold text-sky-800 hover:bg-sky-100"
                    >
                      @{account.bot_username}
                      <ExternalLink className="h-2.5 w-2.5" />
                    </a>
                  )}
                </div>

                <div className="flex flex-wrap items-center gap-1.5">
                  {account.rate_limit_profile?.mps && (
                    <span className="rounded-full border border-blue-200 bg-blue-50 px-2 py-0.5 text-[10.5px] font-medium text-blue-800">
                      {account.rate_limit_profile.mps} mps
                    </span>
                  )}
                  <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[10.5px] font-semibold text-emerald-800">
                    <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
                    {account.status}
                  </span>
                  {isAdmin && (
                    <button
                      type="button"
                      data-testid={`account-toggle-ownership-${account.id}`}
                      onClick={() => (editing ? setEditingId(null) : startEdit(account))}
                      className="erp-btn rounded-lg border border-slate-300 bg-white text-[11px] font-semibold text-slate-700 hover:bg-slate-50"
                    >
                      {editing ? <X className="h-3 w-3" /> : <Pencil className="h-3 w-3" />}
                      <span>{bi("切换归属", "Change owner")}</span>
                    </button>
                  )}
                  {canDisconnect && (
                    <button
                      type="button"
                      data-testid={`account-disconnect-${account.id}`}
                      disabled={busy}
                      onClick={() => void disconnect(account.id)}
                      className="erp-btn rounded-lg border border-rose-200 bg-rose-50 text-[11px] font-semibold text-rose-700 hover:bg-rose-100 disabled:opacity-60"
                    >
                      {busy ? (
                        <Loader2 className="h-3 w-3 animate-spin" />
                      ) : (
                        <Unplug className="h-3 w-3" />
                      )}
                      <span>{bi("断开连接", "Disconnect")}</span>
                    </button>
                  )}
                </div>
              </div>

              {editing && (
                <div className="mt-2 space-y-2 rounded-xl border border-dashed border-emerald-300 bg-emerald-50/40 px-2.5 py-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <button
                      type="button"
                      data-testid={`ownership-edit-shared-${account.id}`}
                      aria-pressed={editScope === "shared"}
                      onClick={() => setEditScope("shared")}
                      className={`rounded-lg px-2 py-0.5 text-[11px] font-semibold transition-colors ${
                        editScope === "shared"
                          ? "bg-[#18181B] text-[#FAFAF9]"
                          : "border border-slate-300 bg-white text-stone-600"
                      }`}
                    >
                      {bi("公司公海", "Company shared")}
                    </button>
                    <button
                      type="button"
                      data-testid={`ownership-edit-personal-${account.id}`}
                      aria-pressed={editScope === "personal"}
                      onClick={() => setEditScope("personal")}
                      className={`rounded-lg px-2 py-0.5 text-[11px] font-semibold transition-colors ${
                        editScope === "personal"
                          ? "bg-emerald-600 text-white"
                          : "border border-slate-300 bg-white text-stone-600"
                      }`}
                    >
                      {bi("指定业务员", "Assign to salesperson")}
                    </button>
                    {editScope === "personal" && (
                      <select
                        data-testid={`ownership-edit-owner-${account.id}`}
                        value={editOwner}
                        onChange={(event) => setEditOwner(event.target.value)}
                        className="erp-input w-auto rounded-lg text-[11.5px]"
                      >
                        <option value="">{bi("选择业务员…", "Select a salesperson…")}</option>
                        {activeMembers.map((member) => (
                          <option key={member.membership_id} value={member.user_id}>
                            {member.full_name || member.email}
                          </option>
                        ))}
                      </select>
                    )}
                  </div>
                  <label className="flex items-center gap-1.5 text-[11px] font-medium text-stone-700">
                    <input
                      type="checkbox"
                      data-testid={`ownership-reassign-${account.id}`}
                      checked={reassign}
                      onChange={(event) => setReassign(event.target.checked)}
                      className="h-3 w-3 rounded border-slate-300 text-emerald-600"
                    />
                    {bi("同步移交进行中的会话", "Reassign open conversations too")}
                  </label>
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      data-testid={`ownership-save-${account.id}`}
                      disabled={busy}
                      onClick={() => void saveEdit(account.id)}
                      className="erp-btn rounded-lg bg-[#059669] text-[11px] font-semibold text-white hover:bg-[#047857] disabled:opacity-60"
                    >
                      {busy ? (
                        <Loader2 className="h-3 w-3 animate-spin" />
                      ) : (
                        <CheckCircle2 className="h-3 w-3" />
                      )}
                      <span>{bi("保存归属", "Save ownership")}</span>
                    </button>
                    <button
                      type="button"
                      onClick={() => setEditingId(null)}
                      className="erp-btn rounded-lg border border-slate-300 bg-white text-[11px] font-semibold text-slate-700 hover:bg-slate-50"
                    >
                      {bi("取消", "Cancel")}
                    </button>
                  </div>
                </div>
              )}
            </div>
          );
        })}

        {error && (
          <div
            role="alert"
            className="rounded-xl border border-rose-200 bg-rose-50 px-2.5 py-1.5 text-[11px] font-medium text-rose-700"
          >
            {error}
          </div>
        )}
      </div>
    </section>
  );
}
