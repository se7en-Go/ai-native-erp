from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from redis.asyncio import Redis
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.channels.adapters.base import OutboundMessageRequest
from erp_core.channels.adapters.whatsapp import WhatsAppAdapter
from erp_core.channels.inbox_schemas import (
    ConversationRead,
    InboxTabFilter,
    MessageRead,
    SendInboxMessageRequest,
    SendInboxMessageResponse,
    UpdateConversationRequest,
)
from erp_core.channels.models import ChannelAccount, Conversation, Message
from erp_core.identity.service import IdentityResolverService
from erp_core.shared.enums import (
    AutonomyLevel,
    ChannelType,
    ConversationMode,
    ConversationStatus,
    DeliveryStatus,
    MessageDirection,
    MessageSenderType,
    UserRole,
)
from erp_core.shared.pagination import PaginatedResponse
from erp_core.tenancy.audit import record_audit_event
from erp_core.tenancy.dependencies import CurrentPrincipal


class ConversationAccessDeniedError(Exception):
    """Raised when a private channel's conversation is outside the caller's scope."""


def agent_can_access_conversation(
    principal: CurrentPrincipal,
    conversation: Conversation,
    account: ChannelAccount,
) -> bool:
    """Owner/admin/manager see everything; agents only their own or unowned shared work."""
    if principal.role != UserRole.AGENT:
        return True
    if conversation.assigned_user_id is not None:
        return conversation.assigned_user_id == principal.user_id
    return account.visibility_scope == "shared" and account.owner_user_id is None


def event_visible_to_agent(principal: CurrentPrincipal, parsed: dict[str, Any]) -> bool:
    """SSE privacy filter: never leak another salesperson's private conversation."""
    if principal.role != UserRole.AGENT:
        return True
    assigned = parsed.get("assigned_user_id")
    scope = parsed.get("visibility_scope")
    mine = str(principal.user_id)
    if assigned is not None and assigned != mine:
        return False
    return not (scope == "personal" and assigned != mine)


class TranslationHook(Protocol):
    async def translate_preview(self, text: str, target_lang: str) -> str:
        raise NotImplementedError


class DefaultTranslationPreviewHook:
    """Deterministic preview hook replaced or wrapped by Plan 04 AI translation router."""

    async def translate_preview(self, text: str, target_lang: str) -> str:
        return f"[{target_lang.lower()}] {text}"


class InboxService:
    """Unified omnichannel inbox query, outbound dispatch, and SSE event broadcaster."""

    def __init__(
        self,
        *,
        identity_service: IdentityResolverService | None = None,
        translation_hook: TranslationHook | None = None,
    ) -> None:
        self._identity_service = identity_service or IdentityResolverService()
        self._translator = translation_hook or DefaultTranslationPreviewHook()

    async def list_conversations(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        *,
        tab: InboxTabFilter = "all",
        channel_type: ChannelType | None = None,
        assigned_user_id: uuid.UUID | None = None,
        q: str | None = None,
        limit: int = 20,
        offset: int = 0,
        principal: CurrentPrincipal | None = None,
    ) -> PaginatedResponse[ConversationRead]:
        account = ChannelAccount
        stmt = (
            select(
                Conversation,
                account.visibility_scope,
                account.owner_user_id,
                account.display_name,
            )
            .join(
                account,
                and_(
                    account.tenant_id == Conversation.tenant_id,
                    account.id == Conversation.channel_account_id,
                ),
            )
            .where(Conversation.tenant_id == tenant_id)
        )
        if principal is not None and principal.role == UserRole.AGENT:
            # A sales agent only ever lists their own conversations plus unowned company ones.
            stmt = stmt.where(
                or_(
                    Conversation.assigned_user_id == principal.user_id,
                    and_(
                        Conversation.assigned_user_id.is_(None),
                        account.visibility_scope == "shared",
                        account.owner_user_id.is_(None),
                    ),
                )
            )
        if tab == "unread":
            stmt = stmt.where(Conversation.unread_count > 0)
        elif tab == "pending_human":
            stmt = stmt.where(Conversation.status == ConversationStatus.PENDING_HUMAN.value)
        elif tab == "sla_urgent":
            stmt = stmt.where(
                Conversation.sla_due_at.is_not(None),
                Conversation.status != ConversationStatus.RESOLVED.value,
            )

        if channel_type is not None:
            stmt = stmt.where(Conversation.channel_type == channel_type.value)
        if assigned_user_id is not None:
            stmt = stmt.where(Conversation.assigned_user_id == assigned_user_id)
        if q:
            pattern = f"%{q.strip()}%"
            stmt = stmt.where(
                or_(
                    Conversation.subject.ilike(pattern),
                    Conversation.last_message_preview.ilike(pattern),
                )
            )

        total = (
            await session.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()

        if tab == "sla_urgent":
            ordered = stmt.order_by(
                Conversation.sla_due_at.asc().nulls_last(),
                Conversation.updated_at.desc(),
            )
        else:
            ordered = stmt.order_by(Conversation.updated_at.desc())

        convs = (await session.execute(ordered.limit(limit).offset(offset))).all()

        # Batched lookups: zero N+1 queries and no cross-module ORM import.
        cust_ids = list({row[0].customer_id for row in convs})
        summaries = await self._identity_service.get_customer_summaries(
            session, tenant_id, cust_ids
        )
        assignee_ids = {row[0].assigned_user_id for row in convs if row[0].assigned_user_id}
        assignee_names: dict[uuid.UUID, str] = {}
        if assignee_ids:
            for uid, full_name in (
                await session.execute(
                    text(
                        "SELECT id, full_name FROM users WHERE id = ANY(:ids)"
                    ),
                    {"ids": list(assignee_ids)},
                )
            ).all():
                assignee_names[uid] = str(full_name)

        items = []
        for c, visibility_scope, owner_user_id, account_name in convs:
            cust_summary = summaries.get(c.customer_id)
            items.append(
                ConversationRead(
                    id=c.id,
                    tenant_id=c.tenant_id,
                    customer_id=c.customer_id,
                    channel_account_id=c.channel_account_id,
                    channel_type=ChannelType(c.channel_type),
                    external_thread_id=c.external_thread_id,
                    subject=c.subject,
                    conversation_mode=ConversationMode(c.conversation_mode),
                    status=ConversationStatus(c.status),
                    assigned_user_id=c.assigned_user_id,
                    ai_autonomy_override=(
                        AutonomyLevel(c.ai_autonomy_override) if c.ai_autonomy_override else None
                    ),
                    csw_expires_at=c.csw_expires_at,
                    fep_expires_at=c.fep_expires_at,
                    human_agent_window_expires_at=c.human_agent_window_expires_at,
                    sla_due_at=c.sla_due_at,
                    last_inbound_at=c.last_inbound_at,
                    last_outbound_at=c.last_outbound_at,
                    last_message_preview=c.last_message_preview,
                    unread_count=c.unread_count,
                    updated_at=c.updated_at,
                    customer_name=cust_summary.display_name if cust_summary else None,
                    country_code=cust_summary.country_code if cust_summary else None,
                    iana_timezone=(
                        cust_summary.buyer_local_time.iana_timezone if cust_summary else None
                    ),
                    customer=cust_summary,
                    assigned_user_name=(
                        assignee_names.get(c.assigned_user_id) if c.assigned_user_id else None
                    ),
                    channel_account_display_name=account_name,
                    channel_visibility_scope=visibility_scope,
                    channel_owner_user_id=owner_user_id,
                )
            )
        return PaginatedResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            has_more=(offset + len(items)) < total,
        )

    async def list_messages(
        self,
        session: AsyncSession,
        redis: Redis,
        tenant_id: uuid.UUID,
        conversation_id: uuid.UUID,
        *,
        limit: int = 50,
        offset: int = 0,
        mark_read: bool = True,
        principal: CurrentPrincipal | None = None,
    ) -> PaginatedResponse[MessageRead]:
        conv = (
            await session.execute(
                select(Conversation).where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.id == conversation_id,
                )
            )
        ).scalar_one_or_none()
        if conv is None:
            raise LookupError("Conversation not found")

        acct = await self._account(session, tenant_id, conv.channel_account_id)
        if principal is not None and not agent_can_access_conversation(principal, conv, acct):
            raise ConversationAccessDeniedError("ERR_CONVERSATION_ACCESS_DENIED")

        stmt = select(Message).where(
            Message.tenant_id == tenant_id,
            Message.conversation_id == conversation_id,
        )
        total = (
            await session.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()
        rows = (
            (
                await session.execute(
                    stmt.order_by(Message.source_timestamp.asc(), Message.created_at.asc())
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )

        if mark_read and conv.unread_count > 0:
            conv.unread_count = 0
            await session.flush()
            await redis.publish(
                f"inbox:events:{tenant_id}",
                json.dumps(
                    {
                        "event": "conversation.updated",
                        "conversation_id": str(conversation_id),
                        "unread_count": 0,
                        **self._event_scope(conv, acct),
                    }
                ),
            )

        items = [MessageRead.model_validate(m) for m in rows]
        return PaginatedResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            has_more=(offset + len(items)) < total,
        )

    def _event_scope(self, conv: Conversation, acct: ChannelAccount) -> dict[str, Any]:
        """Carry ownership on every inbox event so the SSE fan-out can filter per agent."""
        return {
            "assigned_user_id": str(conv.assigned_user_id) if conv.assigned_user_id else None,
            "visibility_scope": acct.visibility_scope,
        }

    @staticmethod
    async def _account(
        session: AsyncSession, tenant_id: uuid.UUID, account_id: uuid.UUID
    ) -> ChannelAccount:
        return (
            await session.execute(
                select(ChannelAccount).where(
                    ChannelAccount.tenant_id == tenant_id,
                    ChannelAccount.id == account_id,
                )
            )
        ).scalar_one()

    async def send_message(
        self,
        session: AsyncSession,
        redis: Redis,
        tenant_id: uuid.UUID,
        principal: CurrentPrincipal,
        conversation_id: uuid.UUID,
        req: SendInboxMessageRequest,
        *,
        outbound_enqueue_fn: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> SendInboxMessageResponse:
        conv = (
            await session.execute(
                select(Conversation).where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.id == conversation_id,
                )
            )
        ).scalar_one_or_none()
        if conv is None:
            raise LookupError("Conversation not found")

        acct = await self._account(session, tenant_id, conv.channel_account_id)
        if not agent_can_access_conversation(principal, conv, acct):
            raise ConversationAccessDeniedError("ERR_CONVERSATION_ACCESS_DENIED")

        target_lang = req.translate_to or (
            req.target_language if req.translate_before_send else None
        )
        translated_preview: str | None = None
        if target_lang and req.body_text:
            translated_preview = await self._translator.translate_preview(
                req.body_text, target_lang
            )

        meta = dict(req.metadata)
        if translated_preview is not None:
            meta["translate_to"] = target_lang
            meta["translated_preview"] = translated_preview

        now_ts = datetime.now(UTC)
        msg_id = uuid.uuid4()

        if req.direction == MessageDirection.INTERNAL_NOTE.value:
            msg = Message(
                id=msg_id,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                customer_id=conv.customer_id,
                channel_type=conv.channel_type,
                direction=MessageDirection.INTERNAL_NOTE.value,
                sender_type=MessageSenderType.HUMAN_AGENT.value,
                sender_id=str(principal.user_id),
                content_type=req.content_type.value,
                body_text=req.body_text,
                body_sanitized_html=req.body_html,
                channel_metadata=meta,
                delivery_status=DeliveryStatus.DELIVERED.value,
                channel_msg_cost=Decimal("0.000000"),
                source_timestamp=now_ts,
            )
            session.add(msg)
            await session.flush()
            await redis.publish(
                f"inbox:events:{tenant_id}",
                json.dumps(
                    {
                        "event": "message.created",
                        "conversation_id": str(conversation_id),
                        "message_id": str(msg_id),
                        "direction": "internal_note",
                        **self._event_scope(conv, acct),
                    }
                ),
            )
            return SendInboxMessageResponse(
                message=MessageRead.model_validate(msg),
                translated_preview=translated_preview,
                queued_for_delivery=False,
            )

        summaries = await self._identity_service.get_customer_summaries(
            session, tenant_id, [conv.customer_id]
        )
        cust_summary = summaries.get(conv.customer_id)
        recipient_id = (
            conv.external_thread_id
            or (cust_summary.primary_phone_e164 if cust_summary else None)
            or (cust_summary.primary_email if cust_summary else None)
            or ""
        )

        if conv.channel_type in (
            ChannelType.WHATSAPP_CLOUD.value,
            ChannelType.WHATSAPP_COEXISTENCE.value,
        ):
            decision = await WhatsAppAdapter().evaluate_send_policy(
                session=session,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                customer_id=conv.customer_id,
                recipient_address=recipient_id,
                content_type=req.content_type,
                template_category=meta.get("template_category"),
                now_utc=now_ts,
            )
            if not decision.allowed:
                raise PermissionError(decision.block_reason or "ERR_WHATSAPP_POLICY_BLOCKED")

        msg = Message(
            id=msg_id,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            customer_id=conv.customer_id,
            channel_type=conv.channel_type,
            direction=MessageDirection.OUTBOUND.value,
            sender_type=MessageSenderType.HUMAN_AGENT.value,
            sender_id=str(principal.user_id),
            content_type=req.content_type.value,
            body_text=req.body_text,
            body_sanitized_html=req.body_html,
            channel_metadata=meta,
            delivery_status=DeliveryStatus.QUEUED.value,
            channel_msg_cost=Decimal("0.000000"),
            source_timestamp=now_ts,
        )
        session.add(msg)
        conv.last_outbound_at = now_ts
        conv.last_message_preview = (req.body_text or "")[:180]
        conv.unread_count = 0
        conv.updated_at = now_ts
        await session.flush()

        outbound_req = OutboundMessageRequest(
            tenant_id=tenant_id,
            channel_account_id=conv.channel_account_id,
            channel_type=ChannelType(conv.channel_type),
            external_account_id=acct.external_account_id,
            recipient_id=recipient_id,
            conversation_id=conversation_id,
            message_id=msg_id,
            content_type=req.content_type,
            body_text=req.body_text,
            body_html=req.body_html,
            template_name=req.template_name,
            template_language=req.template_language,
            template_components=req.template_components,
            subject=conv.subject,
            metadata=meta,
        )
        req_dict = outbound_req.model_dump(mode="json")
        if outbound_enqueue_fn is not None:
            await outbound_enqueue_fn(req_dict)
        else:
            await redis.xadd("stream:outbound:messages", {"payload": json.dumps(req_dict)})

        await redis.publish(
            f"inbox:events:{tenant_id}",
            json.dumps(
                {
                    "event": "message.created",
                    "conversation_id": str(conversation_id),
                    "message_id": str(msg_id),
                    "direction": "outbound",
                    **self._event_scope(conv, acct),
                }
            ),
        )
        return SendInboxMessageResponse(
            message=MessageRead.model_validate(msg),
            translated_preview=translated_preview,
            queued_for_delivery=True,
        )

    async def update_conversation(
        self,
        session: AsyncSession,
        redis: Redis,
        tenant_id: uuid.UUID,
        principal: CurrentPrincipal,
        conversation_id: uuid.UUID,
        req: UpdateConversationRequest,
    ) -> ConversationRead:
        conv = (
            await session.execute(
                select(Conversation).where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.id == conversation_id,
                )
            )
        ).scalar_one_or_none()
        if conv is None:
            raise LookupError("Conversation not found")

        acct = await self._account(session, tenant_id, conv.channel_account_id)
        if not agent_can_access_conversation(principal, conv, acct):
            raise ConversationAccessDeniedError("ERR_CONVERSATION_ACCESS_DENIED")
        if (
            principal.role == UserRole.AGENT
            and req.assigned_user_id is not None
            and req.assigned_user_id != principal.user_id
        ):
            raise ConversationAccessDeniedError(
                "Agents cannot reassign conversations to other users"
            )

        before = {
            "status": conv.status,
            "assigned_user_id": str(conv.assigned_user_id) if conv.assigned_user_id else None,
            "ai_autonomy_override": conv.ai_autonomy_override,
        }
        if req.status is not None:
            conv.status = req.status.value
        if req.assigned_user_id is not None:
            conv.assigned_user_id = req.assigned_user_id
        if req.conversation_mode is not None:
            conv.conversation_mode = req.conversation_mode.value
        if req.ai_autonomy_override is not None:
            conv.ai_autonomy_override = req.ai_autonomy_override.value
        if req.mark_read:
            conv.unread_count = 0
        conv.updated_at = datetime.now(UTC)
        await session.flush()

        after = {
            "status": conv.status,
            "assigned_user_id": str(conv.assigned_user_id) if conv.assigned_user_id else None,
            "ai_autonomy_override": conv.ai_autonomy_override,
        }
        await record_audit_event(
            session=session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=str(principal.user_id),
            action="conversation.updated",
            resource_type="conversation",
            resource_id=conversation_id,
            before_state=before,
            after_state=after,
        )
        await redis.publish(
            f"inbox:events:{tenant_id}",
            json.dumps(
                {
                    "event": "conversation.updated",
                    "conversation_id": str(conversation_id),
                    "status": conv.status,
                    **self._event_scope(conv, acct),
                }
            ),
        )
        summaries = await self._identity_service.get_customer_summaries(
            session, tenant_id, [conv.customer_id]
        )
        cust_summary = summaries.get(conv.customer_id)
        return ConversationRead(
            id=conv.id,
            tenant_id=conv.tenant_id,
            customer_id=conv.customer_id,
            channel_account_id=conv.channel_account_id,
            channel_type=ChannelType(conv.channel_type),
            external_thread_id=conv.external_thread_id,
            subject=conv.subject,
            conversation_mode=ConversationMode(conv.conversation_mode),
            status=ConversationStatus(conv.status),
            assigned_user_id=conv.assigned_user_id,
            ai_autonomy_override=(
                AutonomyLevel(conv.ai_autonomy_override) if conv.ai_autonomy_override else None
            ),
            csw_expires_at=conv.csw_expires_at,
            fep_expires_at=conv.fep_expires_at,
            human_agent_window_expires_at=conv.human_agent_window_expires_at,
            sla_due_at=conv.sla_due_at,
            last_inbound_at=conv.last_inbound_at,
            last_outbound_at=conv.last_outbound_at,
            last_message_preview=conv.last_message_preview,
            unread_count=conv.unread_count,
            updated_at=conv.updated_at,
            customer_name=cust_summary.display_name if cust_summary else None,
            country_code=cust_summary.country_code if cust_summary else None,
            iana_timezone=(cust_summary.buyer_local_time.iana_timezone if cust_summary else None),
            customer=cust_summary,
            channel_account_display_name=acct.display_name,
            channel_visibility_scope=acct.visibility_scope,
            channel_owner_user_id=acct.owner_user_id,
        )
