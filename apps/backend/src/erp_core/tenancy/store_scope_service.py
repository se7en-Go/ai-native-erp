from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Row, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.shared.enums import UserRole
from erp_core.shared.errors import AppError
from erp_core.tenancy.audit import record_audit_event
from erp_core.tenancy.dependencies import CurrentPrincipal
from erp_core.tenancy.store_scope_schemas import (
    HandoverResult,
    StoreScopeAssignRequest,
    StoreScopeRead,
)

_WRITE_ROLES = frozenset({UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER})
_HANDOVER_ROLES = frozenset({UserRole.OWNER, UserRole.ADMIN})

_SCOPE_COLUMNS = """
    SELECT s.id,
           s.tenant_id,
           s.user_id,
           u.email AS user_email,
           u.full_name AS user_full_name,
           s.channel_account_id,
           ca.channel_type AS channel_type,
           ca.display_name AS channel_display_name,
           s.access_level,
           s.created_at
    FROM member_channel_scopes s
    LEFT JOIN users u ON u.id = s.user_id
    LEFT JOIN channel_accounts ca
           ON ca.id = s.channel_account_id AND ca.tenant_id = s.tenant_id
"""


def _row_to_scope(row: Row[Any]) -> StoreScopeRead:
    mapping = row._mapping
    return StoreScopeRead(
        id=mapping["id"],
        tenant_id=mapping["tenant_id"],
        user_id=mapping["user_id"],
        user_email=mapping["user_email"],
        user_full_name=mapping["user_full_name"],
        channel_account_id=mapping["channel_account_id"],
        channel_type=mapping["channel_type"],
        channel_display_name=mapping["channel_display_name"],
        access_level=mapping["access_level"],
        created_at=mapping["created_at"],
    )


def _forbidden(action: str) -> AppError:
    return AppError(
        slug="insufficient-role",
        status_code=403,
        code="ERR_INSUFFICIENT_ROLE",
        title="Insufficient Role Permissions",
        detail=f"This role is not authorized to {action}.",
    )


class StoreScopeService:
    """Team store/channel permission grants plus the offboarding handover.

    `erp_core.tenancy` must never import a downstream domain module, so every
    cross-domain read or write touches `channel_accounts`, `opportunities`,
    `conversations`, `member_channel_scopes`, and `tenant_memberships` through a
    parameterised `sqlalchemy.text(...)` statement inside the caller's
    RLS-scoped `tenant_session`. No ORM relationship crosses the module boundary.
    """

    # ------------------------------------------------------------------ guards

    @staticmethod
    async def _assert_member(session: AsyncSession, tenant_id: UUID, user_id: UUID) -> None:
        found = await session.scalar(
            text("SELECT 1 FROM tenant_memberships WHERE tenant_id = :tid AND user_id = :uid"),
            {"tid": str(tenant_id), "uid": str(user_id)},
        )
        if found is None:
            raise AppError(
                slug="unknown-member",
                status_code=404,
                code="ERR_UNKNOWN_MEMBER",
                title="Unknown Team Member",
                detail=f"User '{user_id}' is not a member of this workspace.",
            )

    @staticmethod
    async def _assert_channel_account(
        session: AsyncSession, tenant_id: UUID, channel_account_id: UUID
    ) -> None:
        found = await session.scalar(
            text("SELECT 1 FROM channel_accounts WHERE tenant_id = :tid AND id = :caid"),
            {"tid": str(tenant_id), "caid": str(channel_account_id)},
        )
        if found is None:
            raise AppError(
                slug="unknown-channel-account",
                status_code=404,
                code="ERR_UNKNOWN_CHANNEL_ACCOUNT",
                title="Unknown Channel Account",
                detail=(
                    f"Channel account '{channel_account_id}' is not connected to this workspace."
                ),
            )

    # ------------------------------------------------------------- store scopes

    async def assign_scope(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        principal: CurrentPrincipal,
        payload: StoreScopeAssignRequest,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> StoreScopeRead:
        if principal.role not in _WRITE_ROLES:
            raise _forbidden("grant store permissions")

        await self._assert_member(session, tenant_id, payload.user_id)
        await self._assert_channel_account(session, tenant_id, payload.channel_account_id)

        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO member_channel_scopes (
                        tenant_id, user_id, channel_account_id, access_level
                    ) VALUES (:tid, :uid, :caid, :level)
                    ON CONFLICT (tenant_id, user_id, channel_account_id)
                    DO UPDATE SET access_level = EXCLUDED.access_level
                    RETURNING id
                    """
                ),
                {
                    "tid": str(tenant_id),
                    "uid": str(payload.user_id),
                    "caid": str(payload.channel_account_id),
                    "level": payload.access_level,
                },
            )
        ).one()

        scope_row = (
            await session.execute(
                text(f"{_SCOPE_COLUMNS} WHERE s.tenant_id = :tid AND s.id = :sid"),
                {"tid": str(tenant_id), "sid": str(row.id)},
            )
        ).one()

        await record_audit_event(
            session,
            tenant_id,
            "human_user",
            str(principal.user_id),
            "tenancy.store_scope.assigned",
            "member_channel_scope",
            row.id,
            after_state={
                "user_id": str(payload.user_id),
                "channel_account_id": str(payload.channel_account_id),
                "access_level": payload.access_level,
            },
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return _row_to_scope(scope_row)

    async def list_scopes(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        principal: CurrentPrincipal,
        *,
        user_id: UUID | None = None,
    ) -> list[StoreScopeRead]:
        """Editors read the whole workspace; everyone else only their own grants."""
        if principal.role in _WRITE_ROLES:
            params: dict[str, object] = {"tid": str(tenant_id)}
            filter_sql = ""
            if user_id is not None:
                filter_sql = " AND s.user_id = :uid"
                params["uid"] = str(user_id)
        else:
            if user_id is not None and user_id != principal.user_id:
                raise _forbidden("read another member's store permissions")
            params = {"tid": str(tenant_id), "uid": str(principal.user_id)}
            filter_sql = " AND s.user_id = :uid"

        rows = (
            await session.execute(
                text(
                    f"{_SCOPE_COLUMNS} WHERE s.tenant_id = :tid{filter_sql} "
                    "ORDER BY u.email ASC NULLS LAST, ca.display_name ASC NULLS LAST, s.id ASC"
                ),
                params,
            )
        ).all()
        return [_row_to_scope(row) for row in rows]

    async def remove_scope(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        principal: CurrentPrincipal,
        scope_id: UUID,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        if principal.role not in _WRITE_ROLES:
            raise _forbidden("revoke store permissions")

        deleted = await session.execute(
            text(
                "DELETE FROM member_channel_scopes "
                "WHERE tenant_id = :tid AND id = :sid "
                "RETURNING id, user_id, channel_account_id, access_level"
            ),
            {"tid": str(tenant_id), "sid": str(scope_id)},
        )
        removed = deleted.first()
        if removed is None:
            raise AppError(
                slug="store-scope-not-found",
                status_code=404,
                code="ERR_STORE_SCOPE_NOT_FOUND",
                title="Store Permission Not Found",
                detail=f"Store permission '{scope_id}' does not exist in this workspace.",
            )

        await record_audit_event(
            session,
            tenant_id,
            "human_user",
            str(principal.user_id),
            "tenancy.store_scope.revoked",
            "member_channel_scope",
            scope_id,
            before_state={
                "user_id": str(removed.user_id),
                "channel_account_id": str(removed.channel_account_id),
                "access_level": str(removed.access_level),
            },
            ip_address=ip_address,
            user_agent=user_agent,
        )

    # --------------------------------------------------------------- handover

    @staticmethod
    async def _count_owned(session: AsyncSession, tenant_id: UUID, user_id: UUID) -> dict[str, int]:
        totals = (
            await session.execute(
                text(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM opportunities
                          WHERE tenant_id = :tid AND owner_user_id = :uid) AS opportunities,
                        (SELECT COUNT(*) FROM conversations
                          WHERE tenant_id = :tid AND assigned_user_id = :uid) AS conversations,
                        (SELECT COUNT(*) FROM channel_accounts
                          WHERE tenant_id = :tid AND owner_user_id = :uid) AS channel_accounts,
                        (SELECT COUNT(*) FROM member_channel_scopes
                          WHERE tenant_id = :tid AND user_id = :uid) AS store_scopes
                    """
                ),
                {"tid": str(tenant_id), "uid": str(user_id)},
            )
        ).one()
        return {
            "opportunities": int(totals.opportunities),
            "conversations": int(totals.conversations),
            "channel_accounts": int(totals.channel_accounts),
            "store_scopes": int(totals.store_scopes),
        }

    @staticmethod
    def _rowcount(result: object) -> int:
        return int(result.rowcount or 0) if isinstance(result, CursorResult) else 0

    async def handover_member_assets(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        principal: CurrentPrincipal,
        *,
        from_user_id: UUID,
        to_user_id: UUID,
        deactivate_from_user: bool = False,
        dry_run: bool = False,
    ) -> HandoverResult:
        """Atomically move every asset owned by a departing member to a successor.

        `dry_run=True` performs the same validation and the same counting query,
        but issues no `UPDATE`/`DELETE` at all - the operator sees exactly how
        many records the real handover would touch before committing to it.
        """
        if principal.role not in _HANDOVER_ROLES:
            raise _forbidden("hand over member assets")
        if from_user_id == to_user_id:
            raise AppError(
                slug="invalid-handover-target",
                status_code=422,
                code="ERR_INVALID_HANDOVER_TARGET",
                title="Invalid Handover Target",
                detail="The departing member and the successor must be different users.",
            )

        await self._assert_member(session, tenant_id, from_user_id)
        await self._assert_member(session, tenant_id, to_user_id)

        counts = await self._count_owned(session, tenant_id, from_user_id)

        if dry_run:
            return HandoverResult(
                tenant_id=tenant_id,
                from_user_id=from_user_id,
                to_user_id=to_user_id,
                dry_run=True,
                opportunities_reassigned=counts["opportunities"],
                conversations_reassigned=counts["conversations"],
                channel_accounts_reassigned=counts["channel_accounts"],
                store_scopes_reassigned=counts["store_scopes"],
                total_reassigned=sum(counts.values()),
                from_user_deactivated=deactivate_from_user,
            )

        params = {"tid": str(tenant_id), "from_uid": str(from_user_id), "to_uid": str(to_user_id)}

        opportunities = self._rowcount(
            await session.execute(
                text(
                    "UPDATE opportunities SET owner_user_id = :to_uid "
                    "WHERE tenant_id = :tid AND owner_user_id = :from_uid"
                ),
                params,
            )
        )
        conversations = self._rowcount(
            await session.execute(
                text(
                    "UPDATE conversations SET assigned_user_id = :to_uid "
                    "WHERE tenant_id = :tid AND assigned_user_id = :from_uid"
                ),
                params,
            )
        )
        channel_accounts = self._rowcount(
            await session.execute(
                text(
                    "UPDATE channel_accounts SET owner_user_id = :to_uid, updated_at = now() "
                    "WHERE tenant_id = :tid AND owner_user_id = :from_uid"
                ),
                params,
            )
        )
        # Collapse grants the successor already holds on the same store first, so
        # the unique (tenant, user, channel_account) constraint can never abort the
        # handover; the successor keeps the higher of the two access levels.
        merged_scopes = self._rowcount(
            await session.execute(
                text(
                    """
                    DELETE FROM member_channel_scopes src
                    USING member_channel_scopes dst
                    WHERE src.tenant_id = :tid
                      AND src.user_id = :from_uid
                      AND dst.tenant_id = :tid
                      AND dst.user_id = :to_uid
                      AND dst.channel_account_id = src.channel_account_id
                    """
                ),
                params,
            )
        )
        moved_scopes = self._rowcount(
            await session.execute(
                text(
                    "UPDATE member_channel_scopes SET user_id = :to_uid "
                    "WHERE tenant_id = :tid AND user_id = :from_uid"
                ),
                params,
            )
        )
        store_scopes = merged_scopes + moved_scopes

        deactivated = False
        if deactivate_from_user:
            deactivations = self._rowcount(
                await session.execute(
                    text(
                        "UPDATE tenant_memberships SET is_active = false "
                        "WHERE tenant_id = :tid AND user_id = :from_uid AND is_active = true"
                    ),
                    params,
                )
            )
            deactivated = deactivations > 0

        result = HandoverResult(
            tenant_id=tenant_id,
            from_user_id=from_user_id,
            to_user_id=to_user_id,
            dry_run=False,
            opportunities_reassigned=opportunities,
            conversations_reassigned=conversations,
            channel_accounts_reassigned=channel_accounts,
            store_scopes_reassigned=store_scopes,
            total_reassigned=(opportunities + conversations + channel_accounts + store_scopes),
            from_user_deactivated=deactivated,
        )

        await record_audit_event(
            session,
            tenant_id,
            "human_user",
            str(principal.user_id),
            "tenancy.member.handover",
            "user",
            from_user_id,
            after_state={
                "to_user_id": str(to_user_id),
                "opportunities_reassigned": opportunities,
                "conversations_reassigned": conversations,
                "channel_accounts_reassigned": channel_accounts,
                "store_scopes_reassigned": store_scopes,
                "deactivate_from_user": deactivate_from_user,
                "from_user_deactivated": deactivated,
            },
        )
        return result
