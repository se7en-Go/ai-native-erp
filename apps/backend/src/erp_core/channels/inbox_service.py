from __future__ import annotations

import base64
import binascii
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from redis.asyncio import Redis
from sqlalchemy import ColumnElement, and_, func, literal_column, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.channels.adapters.base import OutboundMessageRequest
from erp_core.channels.adapters.whatsapp import WhatsAppAdapter
from erp_core.channels.inbox_schemas import (
    CONVERSATION_PINNED_STATE_KEY,
    CONVERSATION_TAGS_STATE_KEY,
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
    MessageContentType,
    MessageDirection,
    MessageSenderType,
    UserRole,
)
from erp_core.shared.errors import AppError
from erp_core.shared.pagination import PaginatedResponse
from erp_core.tenancy.audit import record_audit_event
from erp_core.tenancy.dependencies import CurrentPrincipal

# Inbox attachment delivery limits (mirrored by the router's 422 responses).
MAX_INBOX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_INBOX_ATTACHMENT_FILENAME_LENGTH = 120
ALLOWED_INBOX_ATTACHMENT_MIME_TYPES: frozenset[str] = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/gif",
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "text/csv",
        "text/plain",
    }
)
# When object storage is unreachable (local unit tests / degraded MinIO) the outbound
# attachment still needs to be renderable and dispatchable, so a small payload is kept
# inline inside `messages.attachments`. Larger payloads must never be shrunk silently.
INBOX_ATTACHMENT_INLINE_FALLBACK_BYTES = 1024 * 1024
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
# Pinned conversations sort first. `->>` yields text for any JSON value, so this
# cannot raise the way a raw `::boolean` cast would on malformed legacy state.
_PINNED_ORDER_EXPRESSION = (
    "COALESCE(conversations.channel_policy_state ->> 'is_pinned' = 'true', false)"
)
_PINNED_ORDER_COLUMN: ColumnElement[bool] = literal_column(_PINNED_ORDER_EXPRESSION)


class AttachmentStore(Protocol):
    """The two ObjectStorageService calls the inbox attachment pipeline needs."""

    async def upload_bytes(
        self, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> str:
        raise NotImplementedError

    async def download_bytes(self, key: str) -> bytes:
        raise NotImplementedError


class AttachmentValidationError(Exception):
    """Raised when an uploaded file fails the MIME/type or size gate (HTTP 422)."""


class AttachmentUnavailableError(Exception):
    """Raised when attachment bytes cannot be persisted or read back (HTTP 503/404)."""


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


def conversation_is_pinned(policy_state: Any) -> bool:
    """Read the pin flag out of `conversations.channel_policy_state` without a migration."""
    if not isinstance(policy_state, dict):
        return False
    return policy_state.get(CONVERSATION_PINNED_STATE_KEY) is True


def conversation_tags(policy_state: Any) -> list[str]:
    """Read the tag list out of `channel_policy_state`, tolerating legacy/garbage values."""
    if not isinstance(policy_state, dict):
        return []
    raw = policy_state.get(CONVERSATION_TAGS_STATE_KEY)
    if not isinstance(raw, list):
        return []
    return [tag for tag in raw if isinstance(tag, str) and tag]


def merged_policy_state(
    policy_state: Any,
    *,
    is_pinned: bool | None,
    tags: list[str] | None,
) -> dict[str, Any] | None:
    """Return a new JSONB document that preserves every unrelated policy key."""
    if is_pinned is None and tags is None:
        return None
    state: dict[str, Any] = dict(policy_state) if isinstance(policy_state, dict) else {}
    if is_pinned is not None:
        state[CONVERSATION_PINNED_STATE_KEY] = is_pinned
    if tags is not None:
        state[CONVERSATION_TAGS_STATE_KEY] = list(tags)
    return state


def safe_attachment_filename(filename: str) -> str:
    """Keep object keys and Content-Disposition headers free of traversal/control bytes."""
    base = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].strip()
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", base)
    cleaned = cleaned.lstrip(".").strip() or "attachment"
    return cleaned[:MAX_INBOX_ATTACHMENT_FILENAME_LENGTH]


def inbox_attachment_content_type(mime_type: str) -> MessageContentType:
    """Classify the outbound message; the attachment's `mime_type` picks the transport.

    `ck_messages_content_type` (Alembic 0002) only allows text/rich_html/template/media/
    quote_card/order_card, so images, PDFs and spreadsheets all persist as MEDIA and the
    adapter chooses sendPhoto vs sendDocument from the attachment MIME type.
    """
    return MessageContentType.MEDIA


def validate_attachment_upload(*, mime_type: str, size_bytes: int) -> None:
    if mime_type not in ALLOWED_INBOX_ATTACHMENT_MIME_TYPES:
        raise AttachmentValidationError(f"ERR_ATTACHMENT_TYPE_NOT_ALLOWED: {mime_type}")
    if size_bytes <= 0:
        raise AttachmentValidationError("ERR_ATTACHMENT_EMPTY")
    if size_bytes > MAX_INBOX_ATTACHMENT_BYTES:
        raise AttachmentValidationError("ERR_ATTACHMENT_TOO_LARGE")


def _inline_attachment_bytes(attachment: dict[str, Any]) -> bytes | None:
    encoded = attachment.get("inline_bytes_b64")
    if not isinstance(encoded, str) or not encoded:
        return None
    try:
        return base64.b64decode(encoded.encode("ascii"), validate=True)
    except (binascii.Error, ValueError):
        return None


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
        channel_account_id: uuid.UUID | None = None,
        assigned_user_id: uuid.UUID | None = None,
        tag: str | None = None,
        pinned_only: bool = False,
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
        if channel_account_id is not None:
            stmt = stmt.where(Conversation.channel_account_id == channel_account_id)
        if assigned_user_id is not None:
            stmt = stmt.where(Conversation.assigned_user_id == assigned_user_id)
        if tag is not None and tag.strip():
            stmt = stmt.where(
                text(
                    "conversations.channel_policy_state -> 'tags' @> CAST(:tag_json AS jsonb)"
                ).bindparams(tag_json=json.dumps([tag.strip()]))
            )
        if pinned_only:
            stmt = stmt.where(_PINNED_ORDER_COLUMN.is_(True))
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
                _PINNED_ORDER_COLUMN.desc(),
                Conversation.sla_due_at.asc().nulls_last(),
                Conversation.updated_at.desc(),
            )
        else:
            ordered = stmt.order_by(
                _PINNED_ORDER_COLUMN.desc(),
                Conversation.updated_at.desc(),
            )

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
                    text("SELECT id, full_name FROM users WHERE id = ANY(:ids)"),
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
                    is_pinned=conversation_is_pinned(c.channel_policy_state),
                    tags=conversation_tags(c.channel_policy_state),
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

    async def _recipient_id(
        self, session: AsyncSession, tenant_id: uuid.UUID, conv: Conversation
    ) -> str:
        summaries = await self._identity_service.get_customer_summaries(
            session, tenant_id, [conv.customer_id]
        )
        cust_summary = summaries.get(conv.customer_id)
        return (
            conv.external_thread_id
            or (cust_summary.primary_phone_e164 if cust_summary else None)
            or (cust_summary.primary_email if cust_summary else None)
            or ""
        )

    async def _enqueue_outbound(
        self,
        redis: Redis,
        outbound_req: OutboundMessageRequest,
        outbound_enqueue_fn: Callable[[dict[str, Any]], Awaitable[None]] | None,
    ) -> None:
        req_dict = outbound_req.model_dump(mode="json")
        if outbound_enqueue_fn is not None:
            await outbound_enqueue_fn(req_dict)
        else:
            await redis.xadd("stream:outbound:messages", {"payload": json.dumps(req_dict)})

    async def _publish_message_created(
        self,
        redis: Redis,
        tenant_id: uuid.UUID,
        conv: Conversation,
        acct: ChannelAccount,
        *,
        message_id: uuid.UUID,
        direction: MessageDirection,
        extra: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "event": "message.created",
            "conversation_id": str(conv.id),
            "message_id": str(message_id),
            "direction": direction.value,
            **(extra or {}),
            **self._event_scope(conv, acct),
        }
        await redis.publish(f"inbox:events:{tenant_id}", json.dumps(payload))

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

        recipient_id = await self._recipient_id(session, tenant_id, conv)

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
        await self._enqueue_outbound(redis, outbound_req, outbound_enqueue_fn)
        await self._publish_message_created(
            redis,
            tenant_id,
            conv,
            acct,
            message_id=msg_id,
            direction=MessageDirection.OUTBOUND,
        )
        return SendInboxMessageResponse(
            message=MessageRead.model_validate(msg),
            translated_preview=translated_preview,
            queued_for_delivery=True,
        )

    async def upload_attachment(
        self,
        session: AsyncSession,
        redis: Redis,
        tenant_id: uuid.UUID,
        principal: CurrentPrincipal,
        conversation_id: uuid.UUID,
        *,
        filename: str,
        mime_type: str,
        content: bytes,
        caption: str | None = None,
        storage: AttachmentStore | None = None,
        outbound_enqueue_fn: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> SendInboxMessageResponse:
        """Persist an agent-uploaded file/image/PDF and queue it for channel delivery."""
        validate_attachment_upload(mime_type=mime_type, size_bytes=len(content))

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

        content_type = inbox_attachment_content_type(mime_type)
        now_ts = datetime.now(UTC)
        recipient_id = await self._recipient_id(session, tenant_id, conv)
        if conv.channel_type in (
            ChannelType.WHATSAPP_CLOUD.value,
            ChannelType.WHATSAPP_COEXISTENCE.value,
        ):
            # Media obeys the same 24h CSW / CTWA FEP gate as an agent text reply.
            decision = await WhatsAppAdapter().evaluate_send_policy(
                session=session,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                customer_id=conv.customer_id,
                recipient_address=recipient_id,
                content_type=content_type,
                now_utc=now_ts,
            )
            if not decision.allowed:
                raise PermissionError(decision.block_reason or "ERR_WHATSAPP_POLICY_BLOCKED")

        safe_name = safe_attachment_filename(filename)
        storage_key = f"tenants/{tenant_id}/inbox/{conversation_id}/{uuid.uuid4().hex}_{safe_name}"
        stored_key: str | None = None
        inline_bytes_b64: str | None = None
        storage_error: Exception | None = None
        if storage is not None:
            try:
                stored_key = await storage.upload_bytes(
                    key=storage_key, data=content, content_type=mime_type
                )
            except Exception as exc:  # provider outage must not 500 the agent's upload
                storage_error = exc
        if stored_key is None:
            if len(content) > INBOX_ATTACHMENT_INLINE_FALLBACK_BYTES:
                raise AttachmentUnavailableError("ERR_INBOX_STORAGE_UNAVAILABLE") from storage_error
            inline_bytes_b64 = base64.b64encode(content).decode("ascii")

        msg_id = uuid.uuid4()
        effective_caption = (caption or "").strip()
        body_text = effective_caption or safe_name
        attachment: dict[str, Any] = {
            "filename": safe_name,
            "mime_type": mime_type,
            "size_bytes": len(content),
            "storage_key": stored_key,
            "download_url": f"/api/v1/inbox/attachments/{msg_id}/0",
        }
        if inline_bytes_b64 is not None:
            attachment["inline_bytes_b64"] = inline_bytes_b64
        meta: dict[str, Any] = {
            "attachments": [dict(attachment)],
            "attachment_upload_source": "inbox_workbench",
        }
        if inline_bytes_b64 is not None:
            meta["storage_fallback"] = "inline"

        msg = Message(
            id=msg_id,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            customer_id=conv.customer_id,
            channel_type=conv.channel_type,
            direction=MessageDirection.OUTBOUND.value,
            sender_type=MessageSenderType.HUMAN_AGENT.value,
            sender_id=str(principal.user_id),
            content_type=content_type.value,
            body_text=body_text,
            attachments=[attachment],
            channel_metadata=meta,
            delivery_status=DeliveryStatus.QUEUED.value,
            channel_msg_cost=Decimal("0.000000"),
            source_timestamp=now_ts,
        )
        session.add(msg)
        conv.last_outbound_at = now_ts
        conv.last_message_preview = body_text[:180]
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
            content_type=content_type,
            body_text=effective_caption or None,
            subject=conv.subject,
            metadata=meta,
        )
        await self._enqueue_outbound(redis, outbound_req, outbound_enqueue_fn)
        await self._publish_message_created(
            redis,
            tenant_id,
            conv,
            acct,
            message_id=msg_id,
            direction=MessageDirection.OUTBOUND,
            extra={"content_type": content_type.value, "attachment_count": 1},
        )
        return SendInboxMessageResponse(
            message=MessageRead.model_validate(msg),
            translated_preview=None,
            queued_for_delivery=True,
        )

    async def download_attachment(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        principal: CurrentPrincipal,
        message_id: uuid.UUID,
        attachment_index: int,
        *,
        storage: AttachmentStore | None,
    ) -> tuple[bytes, str, str]:
        """Return ``(bytes, mime_type, filename)`` for one indexed message attachment."""
        msg = (
            await session.execute(
                select(Message).where(
                    Message.tenant_id == tenant_id,
                    Message.id == message_id,
                )
            )
        ).scalar_one_or_none()
        if msg is None:
            raise LookupError("Message not found")

        conv = (
            await session.execute(
                select(Conversation).where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.id == msg.conversation_id,
                )
            )
        ).scalar_one_or_none()
        if conv is None:
            raise LookupError("Conversation not found")
        acct = await self._account(session, tenant_id, conv.channel_account_id)
        if not agent_can_access_conversation(principal, conv, acct):
            raise ConversationAccessDeniedError("ERR_CONVERSATION_ACCESS_DENIED")

        attachments = msg.attachments if isinstance(msg.attachments, list) else []
        if attachment_index < 0 or attachment_index >= len(attachments):
            raise LookupError("Attachment not found")
        attachment = attachments[attachment_index]
        if not isinstance(attachment, dict):
            raise LookupError("Attachment not found")
        mime_type = str(attachment.get("mime_type") or "application/octet-stream")
        filename = safe_attachment_filename(str(attachment.get("filename") or "attachment"))

        storage_key = attachment.get("storage_key")
        if isinstance(storage_key, str) and storage_key and storage is not None:
            try:
                return await storage.download_bytes(storage_key), mime_type, filename
            except Exception as exc:
                raise AttachmentUnavailableError("ERR_INBOX_ATTACHMENT_STORAGE_ERROR") from exc
        inline = _inline_attachment_bytes(attachment)
        if inline is None:
            raise LookupError("Attachment not found")
        return inline, mime_type, filename

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
        if principal.role == UserRole.AGENT:
            # Autonomy switches and third-party reassignment are supervisory controls:
            # an AGENT may only claim work for itself, never widen AI autonomy or move a
            # conversation onto another salesperson (which would also leak the record).
            if req.ai_autonomy_override is not None:
                raise AppError(
                    slug="insufficient-role",
                    status_code=403,
                    code="ERR_INSUFFICIENT_ROLE",
                    title="Insufficient Role Permissions",
                    detail=(
                        "Role 'agent' cannot override conversation AI autonomy; "
                        "manager/admin/owner required."
                    ),
                )
            if (
                req.assigned_user_id is not None
                and req.assigned_user_id != principal.user_id
            ):
                raise ConversationAccessDeniedError(
                    "Agents cannot reassign conversations to other users"
                )

        before = {
            "status": conv.status,
            "assigned_user_id": str(conv.assigned_user_id) if conv.assigned_user_id else None,
            "ai_autonomy_override": conv.ai_autonomy_override,
            "is_pinned": conversation_is_pinned(conv.channel_policy_state),
            "tags": conversation_tags(conv.channel_policy_state),
        }
        if req.status is not None:
            conv.status = req.status.value
        if req.assigned_user_id is not None:
            conv.assigned_user_id = req.assigned_user_id
        if req.conversation_mode is not None:
            conv.conversation_mode = req.conversation_mode.value
        if req.ai_autonomy_override is not None:
            conv.ai_autonomy_override = req.ai_autonomy_override.value
        updated_state = merged_policy_state(
            conv.channel_policy_state, is_pinned=req.is_pinned, tags=req.tags
        )
        if updated_state is not None:
            # Reassign the whole document: JSONB has no in-place mutation tracking.
            conv.channel_policy_state = updated_state
        if req.mark_read:
            conv.unread_count = 0
        conv.updated_at = datetime.now(UTC)
        await session.flush()

        after = {
            "status": conv.status,
            "assigned_user_id": str(conv.assigned_user_id) if conv.assigned_user_id else None,
            "ai_autonomy_override": conv.ai_autonomy_override,
            "is_pinned": conversation_is_pinned(conv.channel_policy_state),
            "tags": conversation_tags(conv.channel_policy_state),
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
            is_pinned=conversation_is_pinned(conv.channel_policy_state),
            tags=conversation_tags(conv.channel_policy_state),
        )
