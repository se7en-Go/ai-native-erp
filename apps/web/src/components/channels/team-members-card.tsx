"use client";

import React, { useState } from "react";
import { CheckCircle2, Loader2, Mail, Plus, ShieldCheck, UserPlus, Users } from "lucide-react";

import { useI18n } from "@/lib/i18n";
import type {
  ChannelAccountApiItem,
  InviteTeamMemberPayload,
  TeamMemberApiItem,
} from "@/lib/api-client/live-client";
import type { UserRole } from "@/lib/contracts";

const ROLE_COPY: Record<UserRole, { zh: string; en: string; tone: string }> = {
  owner: {
    zh: "企业所有者",
    en: "Owner",
    tone: "border-emerald-300 bg-emerald-50 text-emerald-800",
  },
  admin: {
    zh: "管理员",
    en: "Admin",
    tone: "border-indigo-300 bg-indigo-50 text-indigo-800",
  },
  manager: {
    zh: "业务经理",
    en: "Manager",
    tone: "border-blue-300 bg-blue-50 text-blue-800",
  },
  agent: {
    zh: "外贸业务员",
    en: "Sales agent",
    tone: "border-amber-300 bg-amber-50 text-amber-900",
  },
  finance: {
    zh: "财务",
    en: "Finance",
    tone: "border-slate-300 bg-slate-100 text-slate-700",
  },
};

const INVITE_ROLES: UserRole[] = ["agent", "manager", "finance"];

export function isInviteDraftValid(draft: {
  full_name: string;
  email: string;
  password: string;
}): boolean {
  return (
    draft.full_name.trim().length >= 2 &&
    /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(draft.email.trim()) &&
    draft.password.trim().length >= 8
  );
}

/** 每个成员已绑定的个人专属渠道数（公海渠道不计入任何人）。 */
export function personalChannelCount(
  accounts: ChannelAccountApiItem[],
  userId: string,
): number {
  return accounts.filter(
    (account) =>
      account.visibility_scope === "personal" && account.owner_user_id === userId,
  ).length;
}

export type TeamMembersCardProps = {
  members: TeamMemberApiItem[];
  accounts?: ChannelAccountApiItem[];
  isLoading?: boolean;
  onInvite: (payload: InviteTeamMemberPayload) => Promise<TeamMemberApiItem | void>;
};

/**
 * 「团队成员与业务员账号」面板（owner/admin 可见）。
 * 一个表单搞定“给业务员开号 + 建个人专属渠道”，列表同时显示个人渠道数。
 */
export function TeamMembersCard({
  members,
  accounts = [],
  isLoading = false,
  onInvite,
}: TeamMembersCardProps) {
  const { bi } = useI18n();
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<UserRole>("agent");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<string | null>(null);

  const valid = isInviteDraftValid({ full_name: fullName, email, password });

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!valid || submitting) return;
    setSubmitting(true);
    setError(null);
    setCreated(null);
    try {
      await onInvite({
        email: email.trim(),
        full_name: fullName.trim(),
        password,
        role,
      });
      setCreated(email.trim());
      setFullName("");
      setEmail("");
      setPassword("");
      setRole("agent");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "ERR_MEMBER_INVITE_FAILED");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section
      data-testid="team-members-card"
      className="rounded-2xl border border-slate-200/90 bg-white shadow-[0_1px_2px_rgba(15,23,42,0.04)]"
    >
      <div className="flex flex-wrap items-start justify-between gap-2 border-b border-slate-200/90 px-4 py-3">
        <div className="min-w-0">
          <h2 className="flex items-center gap-2 text-[15px] font-semibold text-slate-950">
            <Users className="h-4 w-4 text-emerald-700" />
            {bi("团队成员与业务员账号", "Team & salesperson accounts")}
          </h2>
          <p className="mt-1 text-[12.5px] leading-5 text-slate-600">
            {bi(
              "给业务员开号后，他们即可在「快捷绑定」里自助绑定个人 Telegram 与工作邮箱；客户回复只进各自的 Inbox。",
              "Invite salespeople so they can self-serve personal Telegram and work-email bindings; replies land only in their inbox."
            )}
          </p>
        </div>
        <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[10.5px] font-semibold text-emerald-800">
          <ShieldCheck className="h-3 w-3" />
          {members.length} {bi("位成员", "members")}
        </span>
      </div>

      <div className="divide-y divide-slate-100">
        {isLoading && (
          <div className="flex items-center gap-2 px-4 py-3 text-[12px] text-slate-500">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            {bi("正在加载成员…", "Loading members…")}
          </div>
        )}
        {!isLoading && members.length === 0 && (
          <div className="px-4 py-4 text-[12px] font-medium text-slate-500">
            {bi("还没有其他成员，先在下面开一个业务员账号。", "No members yet — create a salesperson below.")}
          </div>
        )}
        {members.map((member) => {
          const roleCopy = ROLE_COPY[member.role];
          const bound = personalChannelCount(accounts, member.user_id);
          return (
            <div
              key={member.membership_id}
              data-testid={`team-member-row-${member.membership_id}`}
              className="flex flex-wrap items-center justify-between gap-2 px-4 py-2.5"
            >
              <div className="flex min-w-0 items-center gap-2.5">
                <span
                  aria-hidden="true"
                  className="grid h-8 w-8 shrink-0 place-items-center rounded-full bg-[#F4F2EE] text-[11.5px] font-bold text-stone-700 ring-1 ring-[#E5E2DC]"
                >
                  {(member.full_name || member.email).slice(0, 2).toUpperCase()}
                </span>
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="truncate text-[13px] font-semibold text-slate-900">
                      {member.full_name || member.email}
                    </span>
                    <span
                      className={`rounded-full border px-1.5 py-0.5 text-[10.5px] font-semibold ${roleCopy.tone}`}
                    >
                      {bi(roleCopy.zh, roleCopy.en)}
                    </span>
                    {!member.is_active && (
                      <span className="rounded-full border border-rose-200 bg-rose-50 px-1.5 py-0.5 text-[10.5px] font-semibold text-rose-700">
                        {bi("已停用", "inactive")}
                      </span>
                    )}
                  </div>
                  <div className="mt-0.5 flex items-center gap-1 text-[11.5px] text-slate-500">
                    <Mail className="h-3 w-3" />
                    <span className="truncate">{member.email}</span>
                  </div>
                </div>
              </div>
              <span
                data-testid={`team-member-channels-${member.membership_id}`}
                className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10.5px] font-semibold ${
                  bound > 0
                    ? "border-emerald-200 bg-emerald-50 text-emerald-800"
                    : "border-slate-200 bg-slate-50 text-slate-600"
                }`}
              >
                {bound} {bi("个个人渠道", "personal channels")}
              </span>
            </div>
          );
        })}
      </div>

      <form onSubmit={submit} className="space-y-2 border-t border-slate-200/90 bg-[#FAF8F5] px-4 py-3">
        <div className="flex items-center gap-1.5 text-[12px] font-semibold text-stone-700">
          <UserPlus className="h-3.5 w-3.5 text-emerald-700" />
          {bi("新建业务员 / 团队成员", "New salesperson / team member")}
        </div>
        <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-4">
          <label className="block">
            <span className="erp-label">{bi("姓名", "Full name")}</span>
            <input
              data-testid="member-invite-name"
              type="text"
              value={fullName}
              placeholder="李明伦"
              onChange={(event) => setFullName(event.target.value)}
              className="erp-input mt-1 rounded-xl"
            />
          </label>
          <label className="block">
            <span className="erp-label">{bi("登录邮箱", "Email")}</span>
            <input
              data-testid="member-invite-email"
              type="email"
              value={email}
              placeholder="sales.li@company.com"
              onChange={(event) => setEmail(event.target.value)}
              className="erp-input mt-1 rounded-xl"
            />
          </label>
          <label className="block">
            <span className="erp-label">{bi("初始密码（至少 8 位）", "Password (min 8 chars)")}</span>
            <input
              data-testid="member-invite-password"
              type="password"
              autoComplete="new-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              className="erp-input mt-1 rounded-xl"
            />
          </label>
          <label className="block">
            <span className="erp-label">{bi("角色", "Role")}</span>
            <select
              data-testid="member-invite-role"
              value={role}
              onChange={(event) => setRole(event.target.value as UserRole)}
              className="erp-input mt-1 rounded-xl"
            >
              {INVITE_ROLES.map((option) => (
                <option key={option} value={option}>
                  {bi(ROLE_COPY[option].zh, ROLE_COPY[option].en)}
                </option>
              ))}
            </select>
          </label>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="submit"
            data-testid="member-invite-submit"
            disabled={!valid || submitting}
            className="erp-btn rounded-xl bg-[#18181B] font-semibold text-white shadow-xs hover:bg-zinc-800 disabled:bg-slate-300 disabled:shadow-none"
          >
            {submitting ? (
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : (
              <Plus className="h-3 w-3" />
            )}
            <span>{bi("开通账号", "Create account")}</span>
          </button>
          <span className="text-[11px] text-stone-500">
            {bi(
              "业务员登录后即可自助绑定个人渠道，无需管理员代操作。",
              "Salespeople self-serve their own channel bindings after signing in."
            )}
          </span>
        </div>
        {created && (
          <div
            data-testid="member-invite-success"
            className="flex items-center gap-1.5 rounded-xl border border-emerald-200 bg-emerald-50 px-2.5 py-1.5 text-[11px] font-medium text-emerald-800"
          >
            <CheckCircle2 className="h-3.5 w-3.5" />
            {bi(`已开通：${created}`, `Account created: ${created}`)}
          </div>
        )}
        {error && (
          <div
            role="alert"
            data-testid="member-invite-error"
            className="rounded-xl border border-rose-200 bg-rose-50 px-2.5 py-1.5 text-[11px] font-medium text-rose-700"
          >
            {error}
          </div>
        )}
      </form>
    </section>
  );
}
