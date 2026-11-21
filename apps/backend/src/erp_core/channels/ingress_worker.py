from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, cast

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from taskiq_redis import RedisStreamBroker

from erp_core.channels.adapters.base import (
    AdapterRegistry,
    NormalizedInboundEvent,
    NormalizedStatusEvent,
    NormalizedWebhookBatch,
    OutboundMessageRequest,
)
from erp_core.channels.rate_limiter import DualTokenBucketLimiter
from erp_core.channels.webhook_router import build_default_adapter_registry
from erp_core.channels.webhook_store import (
    claim_raw_webhook_event,
    list_unenqueued_raw_webhook_ids,
    set_raw_webhook_processing_state,
)
from erp_core.shared.config import get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import system_session, tenant_session
from erp_core.shared.enums import (
    ChannelType,
    ConversationStatus,
    DeliveryStatus,
    IdentityType,
    MessageDirection,
    UserRole,
)
from erp_core.tenancy.audit import record_audit_event
from erp_core.tenancy.dependencies import CurrentPrincipal, require_roles

logger = logging.getLogger(__name__)
dlq_router = APIRouter(prefix="/api/v1/channels/dlq", tags=["channels-dlq"])
_require_dlq_operator = require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER)

MAX_INGRESS_ATTEMPTS = 5
MAX_OUTBOUND_ATTEMPTS = 6
_WORKER_CHANNELS: tuple[ChannelType, ...] = (
    ChannelType.WHATSAPP_CLOUD,
    ChannelType.WHATSAPP_COEXISTENCE,
    ChannelType.EMAIL,
    ChannelType.WEB_LIVECHAT,
    ChannelType.MESSENGER,
    ChannelType.INSTAGRAM_DM,
    ChannelType.TELEGRAM,
)


def _defer_delay_seconds() -> float:
    raw = os.environ.get("WEBHOOK_DEFER_DELAY_SECONDS", "30")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 30.0


class OutOfOrderParentError(Exception):
    """Raised when a child webhook arrives prior to its parent entity/message."""


def filter_webhook_batch_for_row(
    batch: NormalizedWebhookBatch,
    *,
    expected_account_id: str | None,
    expected_event_id: str | None,
) -> NormalizedWebhookBatch:
    """Keep only the events that belong to the raw webhook row being processed.

    One inbound envelope (a single Meta/WhatsApp/Telegram POST) can be split into several
    ``raw_webhook_events`` rows, one per message and per external channel account, and the
    worker resolves the tenant from the row's ``external_account_id``. Every row therefore
    has to drop events routed to a different external account, otherwise Tenant A's payload
    writes customer identities, conversations and messages into Tenant B's tenant session.

    Rules (deliberately conservative so legitimate single-envelope events survive):

    * Always enforce ``external_account_id``: an event carrying a non-empty account that
      differs from the row's account is dropped. Events with an empty account are kept.
    * Narrow by ``external_event_id`` only when the batch actually contains a matching
      ``external_message_id`` (inbound) or ``source_external_event_id``/``external_message_id``
      (status). Synthetic/composite row ids (``sha256`` digests, svix ids, ``status:...``
      keys) never match and therefore never shrink the batch.

    The input batch is not mutated; a filtered copy is returned, with the same
    ``account_updates``. Only account/message identifiers are logged, never payloads.
    """
    expected_account = (expected_account_id or "").strip()
    expected_event = (expected_event_id or "").strip()

    inbound_events: list[NormalizedInboundEvent] = []
    for inbound in batch.inbound_events:
        event_account = (inbound.external_account_id or "").strip()
        if (
            expected_account
            and event_account
            and event_account != expected_account
            and inbound.resolved_conversation_id is None
        ):
            logger.warning(
                "dropping inbound webhook event for foreign channel account "
                "(expected_account=%s event_account=%s external_event_id=%s)",
                expected_account,
                event_account,
                inbound.external_event_id,
            )
            continue
        inbound_events.append(inbound)

    status_events: list[NormalizedStatusEvent] = []
    for status_event in batch.status_events:
        event_account = (status_event.external_account_id or "").strip()
        if expected_account and event_account and event_account != expected_account:
            logger.warning(
                "dropping status webhook event for foreign channel account "
                "(expected_account=%s event_account=%s external_message_id=%s)",
                expected_account,
                event_account,
                status_event.external_message_id,
            )
            continue
        status_events.append(status_event)

    if expected_event:
        matching_inbound = [
            event for event in inbound_events if event.external_message_id == expected_event
        ]
        if matching_inbound:
            inbound_events = matching_inbound
        matching_status = [
            status_event
            for status_event in status_events
            if expected_event
            in (status_event.source_external_event_id, status_event.external_message_id)
        ]
        if matching_status:
            status_events = matching_status

    return NormalizedWebhookBatch(
        inbound_events=inbound_events,
        status_events=status_events,
        account_updates=list(batch.account_updates),
    )


def _get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        engine = create_async_engine(os.environ["DATABASE_RUNTIME_URL"], future=True)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        request.app.state.session_factory = factory
    return factory


def _get_redis(request: Request) -> aioredis.Redis:
    client = getattr(request.app.state, "redis", None)
    if client is None:
        client = aioredis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
        request.app.state.redis = client
    return client


async def _resolve_identity_customer(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    identity_type: IdentityType,
    raw_value: str,
    display_name: str,
    country_hint: str | None = None,
) -> tuple[uuid.UUID, bool]:
    """Delegate to IdentityResolverService, else a deterministic fallback."""
    try:
        from erp_core.identity.service import IdentityResolverService

        service = IdentityResolverService()
        return await service.resolve_or_create_customer(
            session=session,
            tenant_id=tenant_id,
            identity_type=identity_type,
            raw_value=raw_value,
            display_name=display_name,
            country_hint=country_hint,
        )
    except ImportError:
        if identity_type == IdentityType.EMAIL:
            normalized = raw_value.strip().lower()
        else:
            normalized = raw_value.strip()
        existing = (
            await session.execute(
                text(
                    "SELECT ci.customer_id FROM customer_identities ci "
                    "WHERE ci.tenant_id = :tid AND ci.identity_type = :itype "
                    "AND ci.identity_value = :ival AND ci.is_active = true LIMIT 1"
                ),
                {"tid": tenant_id, "itype": identity_type.value, "ival": normalized},
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing, False
        cust_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO customers (id, tenant_id, customer_type, display_name) "
                "VALUES (:id, :tid, 'b2c_consumer', :name)"
            ),
            {"id": cust_id, "tid": tenant_id, "name": display_name or normalized},
        )
        await session.execute(
            text(
                "INSERT INTO customer_identities "
                "(id, tenant_id, customer_id, identity_type, identity_value, confidence_level) "
                "VALUES (:id, :tid, :cid, :itype, :ival, 'deterministic') "
                "ON CONFLICT (tenant_id, identity_type, identity_value) DO NOTHING"
            ),
            {
                "id": uuid.uuid4(),
                "tid": tenant_id,
                "cid": cust_id,
                "itype": identity_type.value,
                "ival": normalized,
            },
        )
        return cust_id, True


async def _link_extra_identity(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    customer_id: uuid.UUID,
    identity_type: IdentityType,
    raw_value: str,
) -> None:
    if identity_type == IdentityType.EMAIL:
        normalized = raw_value.strip().lower()
    else:
        normalized = raw_value.strip()
    if not normalized:
        return
    await session.execute(
        text(
            "INSERT INTO customer_identities "
            "(id, tenant_id, customer_id, identity_type, identity_value, confidence_level) "
            "VALUES (:id, :tid, :cid, :itype, :ival, 'deterministic') "
            "ON CONFLICT (tenant_id, identity_type, identity_value) DO NOTHING"
        ),
        {
            "id": uuid.uuid4(),
            "tid": tenant_id,
            "cid": customer_id,
            "itype": identity_type.value,
            "ival": normalized,
        },
    )


async def _resolve_telegram_customer(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    thread: str,
    display_name: str,
) -> uuid.UUID:
    # The schema has no Telegram IdentityType. Serialize first contact per scoped
    # thread and reuse that customer even after the previous ticket was resolved.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"telegram:{tenant_id}:{account_id}:{thread}"},
    )
    existing = await session.scalar(
        text(
            "SELECT customer_id FROM conversations WHERE tenant_id = :tid "
            "AND channel_account_id = :aid AND external_thread_id = :thread "
            "ORDER BY updated_at DESC, id DESC LIMIT 1"
        ),
        {"tid": tenant_id, "aid": account_id, "thread": thread},
    )
    if existing is not None:
        return cast(uuid.UUID, existing)
    customer_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO customers (id, tenant_id, customer_type, display_name) "
            "VALUES (:id, :tid, 'b2c_consumer', :name)"
        ),
        {"id": customer_id, "tid": tenant_id, "name": display_name},
    )
    return customer_id


async def _persist_inbound_event(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    channel_account_id: uuid.UUID,
    base_channel_type: str,
    event: NormalizedInboundEvent,
) -> tuple[uuid.UUID, uuid.UUID | None]:
    if event.parent_external_id:
        parent_exists = (
            await session.execute(
                text(
                    "SELECT 1 FROM messages WHERE tenant_id = :tid "
                    "AND external_message_id = :pid LIMIT 1"
                ),
                {"tid": tenant_id, "pid": event.parent_external_id},
            )
        ).scalar_one_or_none()
        if parent_exists is None:
            raise OutOfOrderParentError(
                f"Parent message {event.parent_external_id} not yet arrived"
            )

    identity_id = None
    if event.sender_identity_type is None:
        if base_channel_type != ChannelType.TELEGRAM or not event.external_thread_id:
            raise ValueError("Channel inbound event has no supported customer identity")
        customer_id = await _resolve_telegram_customer(
            session,
            tenant_id,
            channel_account_id,
            event.external_thread_id,
            event.sender_display_name,
        )
    else:
        customer_id, _ = await _resolve_identity_customer(
            session=session,
            tenant_id=tenant_id,
            identity_type=event.sender_identity_type,
            raw_value=event.sender_identity_value,
            display_name=event.sender_display_name,
            country_hint=event.country_hint,
        )
        for extra_type, extra_val in event.extra_identities:
            await _link_extra_identity(session, tenant_id, customer_id, extra_type, extra_val)
        identity_id = (
            await session.execute(
                text(
                    "SELECT id FROM customer_identities "
                    "WHERE tenant_id = :tid AND customer_id = :cid "
                    "AND identity_type = :itype LIMIT 1"
                ),
                {
                    "tid": tenant_id,
                    "cid": customer_id,
                    "itype": event.sender_identity_type.value,
                },
            )
        ).scalar_one_or_none()

    effective_channel = (
        event.override_channel_type.value if event.override_channel_type else base_channel_type
    )

    # Private channel ownership drives inbound auto-assignment and inbox visibility.
    account_owner = (
        (
            await session.execute(
                text(
                    "SELECT owner_user_id, visibility_scope FROM channel_accounts "
                    "WHERE tenant_id = :tid AND id = :aid"
                ),
                {"tid": tenant_id, "aid": channel_account_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    owner_user_id: uuid.UUID | None = (
        account_owner["owner_user_id"] if account_owner is not None else None
    )

    # Link B2B company if inquiry parser extracted one
    if event.b2b_inquiry_data and event.b2b_inquiry_data.get("company_name"):
        try:
            from erp_core.identity.service import IdentityResolverService

            await IdentityResolverService().resolve_or_create_b2b_company(
                session=session,
                tenant_id=tenant_id,
                customer_id=customer_id,
                legal_name=event.b2b_inquiry_data["company_name"],
                country_code=(event.country_hint or "US")[:2].upper(),
            )
        except (ImportError, AttributeError):
            logger.debug(
                "IdentityResolverService.resolve_or_create_b2b_company unavailable; "
                "skipping company link"
            )

    conv_id: uuid.UUID | None = event.resolved_conversation_id
    if conv_id is None and event.external_thread_id:
        conv_id = (
            await session.execute(
                text(
                    "SELECT id FROM conversations "
                    "WHERE tenant_id = :tid AND channel_account_id = :aid "
                    "AND external_thread_id = :thread AND status != 'resolved' "
                    "ORDER BY updated_at DESC LIMIT 1"
                ),
                {
                    "tid": tenant_id,
                    "aid": channel_account_id,
                    "thread": event.external_thread_id,
                },
            )
        ).scalar_one_or_none()

    if conv_id is None and base_channel_type != ChannelType.TELEGRAM:
        conv_id = (
            await session.execute(
                text(
                    "SELECT id FROM conversations "
                    "WHERE tenant_id = :tid AND customer_id = :cid "
                    "AND channel_account_id = :aid AND status != 'resolved' "
                    "ORDER BY updated_at DESC LIMIT 1"
                ),
                {"tid": tenant_id, "cid": customer_id, "aid": channel_account_id},
            )
        ).scalar_one_or_none()

    now_ts = event.source_timestamp
    opens_window = event.direction == MessageDirection.INBOUND and event.opens_csw
    csw_until = now_ts + timedelta(hours=24) if opens_window else None
    fep_until = now_ts + timedelta(days=7) if event.ctwa_clid else None
    human_until = (
        now_ts + timedelta(days=7)
        if opens_window and effective_channel in (ChannelType.MESSENGER, ChannelType.INSTAGRAM_DM)
        else None
    )

    if conv_id is None:
        conv_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO conversations ("
                "id, tenant_id, customer_id, channel_account_id, channel_type, external_thread_id, "
                "subject, conversation_mode, status, assigned_user_id, csw_expires_at, "
                "fep_expires_at, ctwa_clid, "
                "human_agent_window_expires_at, "
                "last_inbound_at, last_message_preview, unread_count, updated_at"
                ") VALUES ("
                ":id, :tid, :cid, :aid, :ctype, :thread, :subj, :mode, :status, :assignee, "
                ":csw, :fep, :ctwa, :human, :last_in, :preview, :unread, now()"
                ")"
            ),
            {
                "id": conv_id,
                "tid": tenant_id,
                "cid": customer_id,
                "aid": channel_account_id,
                "ctype": effective_channel,
                "thread": event.external_thread_id,
                "subj": event.subject,
                "mode": event.conversation_mode.value,
                "status": ConversationStatus.OPEN.value,
                "assignee": owner_user_id,
                "csw": csw_until,
                "fep": fep_until,
                "ctwa": event.ctwa_clid,
                "human": human_until,
                "last_in": now_ts if event.direction == MessageDirection.INBOUND else None,
                "preview": (event.body_text or "")[:180],
                "unread": 1 if event.direction == MessageDirection.INBOUND else 0,
            },
        )
    else:
        await session.execute(
            text(
                "UPDATE conversations SET "
                "csw_expires_at = GREATEST(:csw, csw_expires_at), "
                "human_agent_window_expires_at = GREATEST(:human, human_agent_window_expires_at), "
                "fep_expires_at = COALESCE(:fep, fep_expires_at), "
                "ctwa_clid = COALESCE(:ctwa, ctwa_clid), "
                "last_inbound_at = CASE WHEN :is_in THEN "
                "GREATEST(COALESCE(last_inbound_at, :ts), :ts) ELSE last_inbound_at END, "
                "last_outbound_at = CASE WHEN :is_out THEN "
                "GREATEST(COALESCE(last_outbound_at, :ts), :ts) ELSE last_outbound_at END, "
                "last_message_preview = :preview, "
                "unread_count = unread_count + CASE WHEN :is_in THEN 1 ELSE 0 END, "
                "status = CASE WHEN status = 'resolved' AND :is_in THEN 'open' ELSE status END, "
                "assigned_user_id = COALESCE(assigned_user_id, :assignee), "
                "updated_at = now() "
                "WHERE tenant_id = :tid AND id = :cid"
            ),
            {
                "tid": tenant_id,
                "cid": conv_id,
                "csw": csw_until,
                "fep": fep_until,
                "ctwa": event.ctwa_clid,
                "human": human_until,
                "is_in": event.direction == MessageDirection.INBOUND,
                "is_out": event.direction == MessageDirection.OUTBOUND,
                "ts": now_ts,
                "preview": (event.body_text or "")[:180],
                "assignee": owner_user_id,
            },
        )

    msg_id = uuid.uuid4()
    inserted_msg_id = (
        await session.execute(
            text(
                "INSERT INTO messages ("
                "id, tenant_id, conversation_id, customer_id, customer_identity_id, "
                "channel_type, external_message_id, external_sender_id, direction, sender_type, "
                "sender_id, content_type, body_text, body_sanitized_html, attachments, "
                "channel_metadata, delivery_status, source_timestamp"
                ") VALUES ("
                ":id, :tid, :conv_id, :cid, :ciid, :ctype, :emid, :esid, :dir, :stype, "
                ":sid, :cont, :btext, :bhtml, CAST(:att AS jsonb), "
                "CAST(:meta AS jsonb), :dstat, :ts"
                ") ON CONFLICT (tenant_id, channel_type, external_message_id) "
                "WHERE external_message_id IS NOT NULL DO NOTHING "
                "RETURNING id"
            ),
            {
                "id": msg_id,
                "tid": tenant_id,
                "conv_id": conv_id,
                "cid": customer_id,
                "ciid": identity_id,
                "ctype": effective_channel,
                "emid": event.external_message_id,
                "esid": event.sender_identity_value,
                "dir": event.direction.value,
                "stype": event.sender_type.value,
                "sid": (
                    str(customer_id)
                    if event.direction == MessageDirection.INBOUND
                    else "coexistence_mobile"
                ),
                "cont": event.content_type.value,
                "btext": event.body_text,
                "bhtml": event.body_sanitized_html,
                "att": json.dumps([a.model_dump(mode="json") for a in event.attachments]),
                "meta": json.dumps(event.channel_metadata),
                "dstat": DeliveryStatus.DELIVERED.value,
                "ts": event.source_timestamp,
            },
        )
    ).scalar_one_or_none()

    if inserted_msg_id is not None and event.is_stop_command:
        try:
            from erp_core.channels.consent_service import (
                ConsentAndSuppressionService,
            )

            await ConsentAndSuppressionService().record_stop_opt_out(
                session=session,
                tenant_id=tenant_id,
                customer_id=customer_id,
                channel_type=effective_channel,
                normalized_address=event.sender_identity_value,
                reason="inbound_stop_keyword",
                source_message_id=inserted_msg_id,
            )
        except ImportError:
            logger.debug("ConsentAndSuppressionService unavailable; skipping STOP opt-out hook")

    return conv_id, inserted_msg_id


async def _apply_status_event(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    status_event: NormalizedStatusEvent,
    channel_type: str | None = None,
) -> uuid.UUID | None:
    if status_event.read_through_recipient_id and channel_type:
        # Messenger/IG receipts may carry only a watermark for the thread. Advance every
        # outbound message at or before it in this customer's conversation, never later ones.
        await session.execute(
            text(
                "UPDATE messages m SET delivery_status = :dstat FROM conversations c "
                "WHERE m.tenant_id = :tid AND c.tenant_id = m.tenant_id "
                "AND c.id = m.conversation_id AND m.channel_type = :channel "
                "AND m.direction = 'outbound' AND c.external_thread_id = :thread "
                "AND m.source_timestamp <= :ts "
                "AND m.delivery_status IN ('queued', 'sent', 'delivered')"
            ),
            {
                "dstat": status_event.delivery_status.value,
                "tid": tenant_id,
                "channel": channel_type,
                "thread": status_event.read_through_recipient_id,
                "ts": status_event.timestamp,
            },
        )
    updated_id = (
        await session.execute(
            text(
                "UPDATE messages SET "
                "delivery_status = :dstat, "
                "channel_metadata = channel_metadata || CAST(:meta AS jsonb) "
                "WHERE tenant_id = :tid AND external_message_id = :emid "
                "RETURNING id"
            ),
            {
                "tid": tenant_id,
                "emid": status_event.external_message_id,
                "dstat": status_event.delivery_status.value,
                "meta": json.dumps(
                    {
                        "status_error_code": status_event.error_code,
                        "status_error_message": status_event.error_message,
                        "pricing": status_event.pricing_metadata,
                    }
                ),
            },
        )
    ).scalar_one_or_none()

    if updated_id is None and status_event.require_existing_message:
        raise OutOfOrderParentError(
            f"Status update arrived before message {status_event.external_message_id}"
        )
    return updated_id


async def _move_to_dlq(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID | None,
    raw_event_id: uuid.UUID | None,
    channel_type: str,
    direction: str,
    external_event_id: str | None,
    payload: dict[str, Any],
    error_code: str | None,
    error_detail: str,
    attempt_count: int,
    broker_session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    if raw_event_id is not None:
        if broker_session_factory is None:
            raise RuntimeError("webhook broker session is not configured")
        async with system_session(broker_session_factory) as sys_sess:
            await set_raw_webhook_processing_state(
                sys_sess,
                raw_event_id=raw_event_id,
                status="dead_letter",
                attempt=attempt_count,
                error_message=error_detail[:500],
            )
    if tenant_id is not None:
        async with tenant_session(session_factory, tenant_id) as t_sess:
            await t_sess.execute(
                text(
                    "INSERT INTO dead_letter_events ("
                    "id, tenant_id, raw_webhook_event_id, channel_type, direction, "
                    "external_event_id, payload, error_code, error_detail, attempt_count, status"
                    ") VALUES ("
                    ":id, :tid, :rid, :ctype, :dir, :eeid, CAST(:payload AS jsonb), "
                    ":ecode, :edetail, :att, 'open'"
                    ")"
                ),
                {
                    "id": uuid.uuid4(),
                    "tid": tenant_id,
                    "rid": raw_event_id,
                    "ctype": channel_type,
                    "dir": direction,
                    "eeid": external_event_id,
                    "payload": json.dumps(payload),
                    "ecode": error_code,
                    "edetail": error_detail[:2000],
                    "att": attempt_count,
                },
            )


def _auto_ai_proposal_enabled() -> bool:
    """Auto-drafting is opt-out via AUTO_AI_PROPOSAL_ENABLED and needs a real LLM key."""
    if os.environ.get("AUTO_AI_PROPOSAL_ENABLED", "true").strip().lower() != "true":
        return False
    return bool(os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY"))


def _auto_proposal_service() -> Any:
    """Factory seam so tests can inject a deterministic offline LLM client."""
    from erp_core.ai_engine.proposal_generator import AiProposalGeneratorService

    return AiProposalGeneratorService()


def _auto_reply_service() -> Any:
    """Factory seam so tests can inject a deterministic offline AutoReplyService."""
    from erp_core.ai_engine.auto_reply_service import AutoReplyService

    return AutoReplyService()


async def _maybe_run_auto_reply(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> bool:
    """Run guarded AI auto-reply when enabled on the conversation.

    Returns ``True`` when the inbound turn was handled (either auto-sent to the channel
    or held as an L1 proposal), so the caller skips duplicate proposal generation.
    AI failures are logged and never fail or DLQ the webhook.
    """
    from erp_core.ai_engine.auto_reply_service import (
        AutoReplyRunRequest,
        publish_auto_reply_dispatch,
    )

    try:
        dispatch_bundle: Any = None
        action: str = "abstained"
        async with tenant_session(session_factory, tenant_id) as ai_sess:
            enabled = await ai_sess.scalar(
                text(
                    "SELECT ai_auto_reply_enabled FROM conversations "
                    "WHERE tenant_id = :tid AND id = :cid"
                ),
                {"tid": tenant_id, "cid": conversation_id},
            )
            if not enabled:
                return False
            system_principal = CurrentPrincipal(
                user_id=uuid.UUID(int=0),
                tenant_id=tenant_id,
                role=UserRole.OWNER,
                email="ai-auto-reply@system.local",
            )
            result, dispatch_bundle = await _auto_reply_service().run_with_dispatch(
                ai_sess,
                system_principal,
                conversation_id,
                AutoReplyRunRequest(mode="auto"),
                prepare_dispatch=True,
            )
            action = str(result.action)
        # Transaction is now committed; safely enqueue outbound delivery & SSE push.
        if dispatch_bundle is not None:
            await publish_auto_reply_dispatch(redis, dispatch_bundle)
        return action in ("auto_replied", "proposal_created")
    except Exception:  # noqa: BLE001 - webhook durability outranks auto-reply
        logger.warning(
            "auto AI reply execution failed for conversation %s",
            conversation_id,
            exc_info=True,
        )
        return False


async def _maybe_generate_auto_proposal(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> None:
    """Best-effort grounded draft; AI failures must never fail or DLQ the webhook."""
    try:
        async with tenant_session(session_factory, tenant_id) as ai_sess:
            await _auto_proposal_service().generate_reply_proposal(
                ai_sess, tenant_id, conversation_id, redis=redis
            )
    except Exception:  # noqa: BLE001 - webhook durability outranks drafting
        logger.warning(
            "auto AI proposal generation failed for conversation %s",
            conversation_id,
            exc_info=True,
        )


async def process_raw_webhook_event(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    raw_event_id: uuid.UUID,
    adapter_registry: AdapterRegistry | None = None,
    attempt: int = 1,
    broker_session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> str:
    """Process one persisted raw webhook event with out-of-order retry and DLQ fallback."""
    if broker_session_factory is None:
        raise RuntimeError("webhook broker session is not configured")
    registry = adapter_registry or build_default_adapter_registry()

    async with system_session(broker_session_factory) as sys_sess:
        row = await claim_raw_webhook_event(sys_sess, raw_event_id)

    if row is None:
        return "missing"
    if row["processing_status"] == "processed":
        return "already_processed"

    channel_type = str(row["channel_type"])
    tenant_id: uuid.UUID | None = row["tenant_id"]
    ext_acct_id: str | None = row["external_account_id"]
    headers: dict[str, Any] = dict(row["headers"] or {})
    payload: dict[str, Any] = dict(row["raw_payload"] or {})
    channel_account_id: uuid.UUID | None = None

    async with system_session(session_factory) as sys_sess:
        if ext_acct_id:
            dir_row = (
                await sys_sess.execute(
                    text(
                        "SELECT tenant_id, channel_account_id "
                        "FROM resolve_tenant_by_channel_external_id("
                        "CAST(:ctype AS text), CAST(:ext_id AS text))"
                    ),
                    {"ctype": channel_type, "ext_id": ext_acct_id},
                )
            ).first()
            if dir_row is not None:
                tenant_id = dir_row[0]
                channel_account_id = dir_row[1]

    if tenant_id is None or channel_account_id is None:
        await _move_to_dlq(
            session_factory,
            tenant_id=tenant_id,
            raw_event_id=raw_event_id,
            channel_type=channel_type,
            direction="inbound",
            external_event_id=row["external_event_id"],
            payload=payload,
            error_code="ERR_UNRESOLVED_CHANNEL_TENANT",
            error_detail=f"Cannot resolve tenant/channel_account for {channel_type}:{ext_acct_id}",
            attempt_count=attempt,
            broker_session_factory=broker_session_factory,
        )
        return "dead_letter"

    adapter = registry.get(channel_type)
    if adapter is None:
        await _move_to_dlq(
            session_factory,
            tenant_id=tenant_id,
            raw_event_id=raw_event_id,
            channel_type=channel_type,
            direction="inbound",
            external_event_id=row["external_event_id"],
            payload=payload,
            error_code="ERR_ADAPTER_MISSING",
            error_detail=f"No adapter registered for {channel_type}",
            attempt_count=attempt,
            broker_session_factory=broker_session_factory,
        )
        return "dead_letter"

    published_events: list[dict[str, Any]] = []
    auto_proposal_conversations: list[uuid.UUID] = []
    try:
        async with tenant_session(session_factory, tenant_id) as t_sess:
            parsed_batch = await adapter.parse_webhook(
                session=t_sess,
                tenant_id=tenant_id,
                channel_account_id=channel_account_id,
                payload=payload,
                headers={str(k).lower(): str(v) for k, v in headers.items()},
            )
            # One envelope can be split across rows of several tenants: never apply
            # events routed to another external account inside this tenant session.
            ext_event_id: str | None = row["external_event_id"]
            batch = filter_webhook_batch_for_row(
                parsed_batch,
                expected_account_id=ext_acct_id,
                expected_event_id=ext_event_id,
            )
            if (
                len(batch.inbound_events) != len(parsed_batch.inbound_events)
                or len(batch.status_events) != len(parsed_batch.status_events)
            ):
                logger.warning(
                    "webhook %s (%s account=%s event=%s) filtered events: "
                    "inbound %d->%d, status %d->%d",
                    raw_event_id,
                    channel_type,
                    ext_acct_id,
                    ext_event_id,
                    len(parsed_batch.inbound_events),
                    len(batch.inbound_events),
                    len(parsed_batch.status_events),
                    len(batch.status_events),
                )

            for inbound in batch.inbound_events:
                conv_id, msg_id = await _persist_inbound_event(
                    session=t_sess,
                    tenant_id=tenant_id,
                    channel_account_id=channel_account_id,
                    base_channel_type=channel_type,
                    event=inbound,
                )
                if msg_id is not None:
                    scope_row = (
                        (
                            await t_sess.execute(
                                text(
                                    "SELECT c.assigned_user_id, a.visibility_scope "
                                    "FROM conversations c JOIN channel_accounts a "
                                    "ON a.tenant_id = c.tenant_id AND a.id = c.channel_account_id "
                                    "WHERE c.tenant_id = :tid AND c.id = :cid"
                                ),
                                {"tid": tenant_id, "cid": conv_id},
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    assigned = scope_row["assigned_user_id"] if scope_row else None
                    if (
                        inbound.direction == MessageDirection.INBOUND
                        and not inbound.is_stop_command
                        and conv_id not in auto_proposal_conversations
                    ):
                        auto_proposal_conversations.append(conv_id)
                    published_events.append(
                        {
                            "event": "message.created",
                            "conversation_id": str(conv_id),
                            "message_id": str(msg_id),
                            "channel_type": (
                                inbound.override_channel_type.value
                                if inbound.override_channel_type
                                else channel_type
                            ),
                            "assigned_user_id": str(assigned) if assigned else None,
                            "visibility_scope": (
                                scope_row["visibility_scope"] if scope_row else "shared"
                            ),
                        }
                    )

            for status_ev in batch.status_events:
                updated_msg_id = await _apply_status_event(
                    session=t_sess,
                    tenant_id=tenant_id,
                    status_event=status_ev,
                    channel_type=channel_type,
                )
                if updated_msg_id is not None:
                    published_events.append(
                        {
                            "event": "message.status_updated",
                            "message_id": str(updated_msg_id),
                            "delivery_status": status_ev.delivery_status.value,
                        }
                    )

        async with system_session(broker_session_factory) as sys_sess:
            await set_raw_webhook_processing_state(
                sys_sess,
                raw_event_id=raw_event_id,
                status="processed",
                tenant_id=tenant_id,
                attempt=attempt,
                error_message=None,
            )

        for ev in published_events:
            await redis.publish(f"inbox:events:{tenant_id}", json.dumps(ev))

        if auto_proposal_conversations:
            for conv_id in auto_proposal_conversations:
                handled = await _maybe_run_auto_reply(
                    session_factory, redis, tenant_id, conv_id
                )
                if not handled and _auto_ai_proposal_enabled():
                    await _maybe_generate_auto_proposal(
                        session_factory, redis, tenant_id, conv_id
                    )
        return "processed"

    except OutOfOrderParentError as exc:
        if attempt <= MAX_INGRESS_ATTEMPTS:
            async with system_session(broker_session_factory) as sys_sess:
                await set_raw_webhook_processing_state(
                    sys_sess,
                    raw_event_id=raw_event_id,
                    status="deferred",
                    attempt=attempt,
                    error_message=str(exc)[:500],
                )
            retry_at = datetime.now(UTC).timestamp() + _defer_delay_seconds()
            await redis.zadd(
                "queue:webhooks:deferred",
                {
                    json.dumps(
                        {
                            "raw_event_id": str(raw_event_id),
                            "channel_type": channel_type,
                            "attempt": attempt + 1,
                        }
                    ): retry_at
                },
            )
            return "deferred"

        await _move_to_dlq(
            session_factory,
            tenant_id=tenant_id,
            raw_event_id=raw_event_id,
            channel_type=channel_type,
            direction="inbound",
            external_event_id=row["external_event_id"],
            payload=payload,
            error_code="ERR_OUT_OF_ORDER_EXHAUSTED",
            error_detail=str(exc),
            attempt_count=attempt,
            broker_session_factory=broker_session_factory,
        )
        return "dead_letter"

    except Exception as exc:
        await _move_to_dlq(
            session_factory,
            tenant_id=tenant_id,
            raw_event_id=raw_event_id,
            channel_type=channel_type,
            direction="inbound",
            external_event_id=row["external_event_id"],
            payload=payload,
            error_code="ERR_WEBHOOK_PROCESSING_FAILED",
            error_detail=f"{type(exc).__name__}: {exc}",
            attempt_count=attempt,
            broker_session_factory=broker_session_factory,
        )
        return "dead_letter"


def _decode_stream_field(fields: dict[Any, Any], key: str) -> str:
    raw = fields.get(key.encode(), fields.get(key))
    if isinstance(raw, bytes):
        return raw.decode()
    return "" if raw is None else str(raw)


async def _ack_processed_entries(
    redis: aioredis.Redis,
    session_factory: async_sessionmaker[AsyncSession],
    stream_key: str,
    group_name: str,
    messages: list[tuple[Any, dict[Any, Any]]],
    adapter_registry: AdapterRegistry | None,
    broker_session_factory: async_sessionmaker[AsyncSession],
) -> int:
    processed = 0
    for stream_msg_id, fields in messages:
        raw_id_str = _decode_stream_field(fields, "raw_event_id")
        attempt_raw = _decode_stream_field(fields, "attempt") or "1"
        if not raw_id_str:
            await redis.xack(stream_key, group_name, stream_msg_id)
            continue
        await process_raw_webhook_event(
            session_factory=session_factory,
            redis=redis,
            raw_event_id=uuid.UUID(raw_id_str),
            adapter_registry=adapter_registry,
            attempt=int(attempt_raw),
            broker_session_factory=broker_session_factory,
        )
        await redis.xack(stream_key, group_name, stream_msg_id)
        processed += 1
    return processed


async def consume_webhook_stream_batch(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    channel_type: ChannelType,
    *,
    group_name: str = "erp_webhook_workers",
    consumer_name: str = "worker-1",
    count: int = 20,
    block_ms: int = 50,
    min_idle_ms: int = 15_000,
    adapter_registry: AdapterRegistry | None = None,
    broker_session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> int:
    """Read new stream entries and reclaim idle pending entries after a crash."""
    if broker_session_factory is None:
        raise RuntimeError("webhook broker session is not configured")
    stream_key = f"stream:webhooks:{channel_type.value}"
    try:
        await redis.xgroup_create(stream_key, group_name, id="0", mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise

    processed = 0
    claimed = await redis.xautoclaim(
        stream_key,
        group_name,
        consumer_name,
        min_idle_ms,
        start_id="0-0",
        count=count,
    )
    claimed_messages = cast(list[tuple[Any, dict[Any, Any]]], claimed[1] if claimed else [])
    processed += await _ack_processed_entries(
        redis,
        session_factory,
        stream_key,
        group_name,
        claimed_messages,
        adapter_registry,
        broker_session_factory,
    )

    entries = await redis.xreadgroup(
        groupname=group_name,
        consumername=consumer_name,
        streams={stream_key: ">"},
        count=count,
        block=block_ms,
    )
    stream_batches = cast(
        list[tuple[Any, list[tuple[Any, dict[Any, Any]]]]],
        entries or [],
    )
    for _, messages in stream_batches:
        processed += await _ack_processed_entries(
            redis,
            session_factory,
            stream_key,
            group_name,
            messages,
            adapter_registry,
            broker_session_factory,
        )
    return processed


async def compensate_unenqueued_webhooks(
    broker_session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    *,
    limit: int = 20,
) -> int:
    """Enqueue raw rows left in `received` when Redis failed after the DB commit."""
    async with system_session(broker_session_factory) as session:
        pending = await list_unenqueued_raw_webhook_ids(session, limit)
    enqueued = 0
    for raw_id, channel_type in pending:
        await redis.xadd(
            f"stream:webhooks:{channel_type}",
            {"raw_event_id": str(raw_id), "channel_type": channel_type, "attempt": "1"},
        )
        async with system_session(broker_session_factory) as session:
            await set_raw_webhook_processing_state(
                session,
                raw_event_id=raw_id,
                status="queued",
                attempt=1,
            )
        enqueued += 1
    return enqueued


async def drain_deferred_webhooks(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    broker_session_factory: async_sessionmaker[AsyncSession],
) -> int:
    now_score = datetime.now(UTC).timestamp()
    due_items = cast(
        list[bytes | str],
        await redis.zrangebyscore("queue:webhooks:deferred", "-inf", now_score),
    )
    count = 0
    for raw in due_items:
        removed = await redis.zrem("queue:webhooks:deferred", raw)
        if not removed:
            continue
        payload = raw.decode() if isinstance(raw, bytes) else raw
        item = json.loads(payload)
        await process_raw_webhook_event(
            session_factory=session_factory,
            redis=redis,
            raw_event_id=uuid.UUID(item["raw_event_id"]),
            attempt=int(item.get("attempt", 1)),
            broker_session_factory=broker_session_factory,
        )
        count += 1
    return count


async def consume_outbound_stream_batch(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    *,
    group_name: str = "erp_outbound_workers",
    consumer_name: str = "worker-1",
    count: int = 20,
    block_ms: int = 20,
    min_idle_ms: int = 15_000,
    adapter_registry: AdapterRegistry | None = None,
) -> int:
    """Consume queued outbound replies from `stream:outbound:messages` and dispatch them."""
    stream_key = "stream:outbound:messages"
    try:
        await redis.xgroup_create(stream_key, group_name, id="0", mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise

    crypto = EnvelopeCryptoService(get_settings())
    processed = 0

    async def _dispatch_entries(entries_list: list[tuple[Any, dict[Any, Any]]]) -> int:
        done = 0
        for stream_msg_id, fields in entries_list:
            raw_payload = _decode_stream_field(fields, "payload")
            if not raw_payload:
                await redis.xack(stream_key, group_name, stream_msg_id)
                continue
            try:
                req_obj = OutboundMessageRequest.model_validate(json.loads(raw_payload))
                await send_outbound_message(
                    session_factory=session_factory,
                    redis=redis,
                    crypto_service=crypto,
                    request_payload=req_obj,
                    adapter_registry=adapter_registry,
                )
            except Exception:
                logger.exception("Failed dispatching outbound message %s", stream_msg_id)
            await redis.xack(stream_key, group_name, stream_msg_id)
            done += 1
        return done

    claimed = await redis.xautoclaim(
        stream_key,
        group_name,
        consumer_name,
        min_idle_ms,
        start_id="0-0",
        count=count,
    )
    claimed_messages = cast(list[tuple[Any, dict[Any, Any]]], claimed[1] if claimed else [])
    processed += await _dispatch_entries(claimed_messages)

    raw_entries = cast(
        list[tuple[Any, list[tuple[Any, dict[Any, Any]]]]],
        await redis.xreadgroup(
            groupname=group_name,
            consumername=consumer_name,
            streams={stream_key: ">"},
            count=count,
            block=block_ms,
        ),
    )
    for _stream_name, messages in raw_entries:
        processed += await _dispatch_entries(messages)
    return processed


async def drain_delayed_outbound(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    adapter_registry: AdapterRegistry | None = None,
) -> int:
    """Dispatch rate-limited/retried outbound messages whose delay has elapsed."""
    now_score = datetime.now(UTC).timestamp()
    due_items = cast(
        list[bytes | str],
        await redis.zrangebyscore("queue:outbound:delayed", "-inf", now_score),
    )
    if not due_items:
        return 0
    crypto = EnvelopeCryptoService(get_settings())
    count = 0
    for raw in due_items:
        removed = await redis.zrem("queue:outbound:delayed", raw)
        if not removed:
            continue
        payload = raw.decode() if isinstance(raw, bytes) else raw
        item = json.loads(payload)
        req_obj = OutboundMessageRequest.model_validate(item["payload"])
        await send_outbound_message(
            session_factory=session_factory,
            redis=redis,
            crypto_service=crypto,
            request_payload=req_obj,
            adapter_registry=adapter_registry,
            attempt=int(item.get("attempt", 1)),
        )
        count += 1
    return count


async def run_webhook_worker_once(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    *,
    min_idle_ms: int = 15_000,
    adapter_registry: AdapterRegistry | None = None,
    broker_session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> int:
    """One production pass: compensate, reclaim, read, drain deferred retries and outbound."""
    if broker_session_factory is None:
        raise RuntimeError("webhook broker session is not configured")
    total = await compensate_unenqueued_webhooks(broker_session_factory, redis)
    for channel_type in _WORKER_CHANNELS:
        total += await consume_webhook_stream_batch(
            session_factory,
            redis,
            channel_type,
            min_idle_ms=min_idle_ms,
            adapter_registry=adapter_registry,
            broker_session_factory=broker_session_factory,
        )
    total += await drain_deferred_webhooks(session_factory, redis, broker_session_factory)
    total += await consume_outbound_stream_batch(
        session_factory,
        redis,
        min_idle_ms=min_idle_ms,
        adapter_registry=adapter_registry,
    )
    total += await drain_delayed_outbound(
        session_factory,
        redis,
        adapter_registry=adapter_registry,
    )
    return total


async def run_webhook_worker_loop(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    stop_event: asyncio.Event,
    *,
    min_idle_ms: int = 15_000,
    poll_interval: float = 0.05,
    adapter_registry: AdapterRegistry | None = None,
    broker_session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Run until stop_event is set. This is the production webhook worker loop."""
    while not stop_event.is_set():
        await run_webhook_worker_once(
            session_factory,
            redis,
            min_idle_ms=min_idle_ms,
            adapter_registry=adapter_registry,
            broker_session_factory=broker_session_factory,
        )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
        except TimeoutError:
            continue


async def send_outbound_message(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    crypto_service: EnvelopeCryptoService,
    request_payload: OutboundMessageRequest,
    adapter_registry: AdapterRegistry | None = None,
    attempt: int = 1,
) -> str:
    """Rate-limited outbound dispatch with 4^X retry scheduling and DLQ fallback."""
    registry = adapter_registry or build_default_adapter_registry()
    limiter = DualTokenBucketLimiter(redis)

    async with tenant_session(session_factory, request_payload.tenant_id) as t_sess:
        acct = (
            await t_sess.execute(
                text(
                    "SELECT channel_type, external_account_id, rate_limit_profile "
                    "FROM channel_accounts WHERE tenant_id = :tid AND id = :aid"
                ),
                {
                    "tid": request_payload.tenant_id,
                    "aid": request_payload.channel_account_id,
                },
            )
        ).mappings().first()
        if acct is None:
            return "channel_account_missing"

        is_coexistence = bool((acct["rate_limit_profile"] or {}).get("coexistence", False))
        allowed, wait_ms = await limiter.acquire(
            channel_type=request_payload.channel_type,
            account_id=str(acct["external_account_id"]),
            recipient_id=request_payload.recipient_id,
            is_coexistence=is_coexistence,
        )
        if not allowed:
            retry_at = datetime.now(UTC).timestamp() + (max(wait_ms, 500) / 1000.0)
            await redis.zadd(
                "queue:outbound:delayed",
                {
                    json.dumps(
                        {
                            "payload": request_payload.model_dump(mode="json"),
                            "attempt": attempt,
                        }
                    ): retry_at
                },
            )
            return "rate_limited_requeued"

        cred_row = (
            await t_sess.execute(
                text(
                    "SELECT ciphertext, nonce, auth_tag, key_version, credential_type "
                    "FROM channel_credentials "
                    "WHERE tenant_id = :tid AND channel_account_id = :aid "
                    "ORDER BY updated_at DESC LIMIT 1"
                ),
                {
                    "tid": request_payload.tenant_id,
                    "aid": request_payload.channel_account_id,
                },
            )
        ).mappings().first()

        credentials: dict[str, Any] = {}
        if cred_row is not None:
            decrypted = await crypto_service.decrypt_secret(
                session=t_sess,
                tenant_id=request_payload.tenant_id,
                account_id=request_payload.channel_account_id,
                cred_type=str(cred_row["credential_type"]),
                ciphertext=bytes(cred_row["ciphertext"]),
                nonce=bytes(cred_row["nonce"]),
                auth_tag=bytes(cred_row["auth_tag"]),
                key_version=int(cred_row["key_version"]),
            )
            try:
                credentials = json.loads(decrypted)
            except json.JSONDecodeError:
                credentials = {"access_token": decrypted}

        adapter = registry.get(request_payload.channel_type.value)
        if adapter is None:
            raise RuntimeError(f"Missing adapter for {request_payload.channel_type.value}")

        result = await adapter.send_message(
            session=t_sess,
            tenant_id=request_payload.tenant_id,
            credentials=credentials,
            request=request_payload,
        )

        if result.success:
            await t_sess.execute(
                text(
                    "UPDATE messages SET "
                    "delivery_status = 'sent', "
                    "external_message_id = COALESCE(:emid, external_message_id), "
                    "channel_msg_cost = :cost, "
                    "channel_metadata = channel_metadata || CAST(:meta AS jsonb) "
                    "WHERE tenant_id = :tid AND id = :mid"
                ),
                {
                    "tid": request_payload.tenant_id,
                    "mid": request_payload.message_id,
                    "emid": result.external_message_id,
                    "cost": result.estimated_cost_usd,
                    "meta": json.dumps({"pricing_category": result.pricing_category}),
                },
            )
            await redis.publish(
                f"inbox:events:{request_payload.tenant_id}",
                json.dumps(
                    {
                        "event": "message.status_updated",
                        "message_id": str(request_payload.message_id),
                        "delivery_status": DeliveryStatus.SENT.value,
                    }
                ),
            )
            return "sent"

        if result.retryable and attempt < MAX_OUTBOUND_ATTEMPTS:
            backoff_sec = limiter.compute_backoff_seconds(attempt, result.error_code)
            retry_at = datetime.now(UTC).timestamp() + backoff_sec
            await redis.zadd(
                "queue:outbound:delayed",
                {
                    json.dumps(
                        {
                            "payload": request_payload.model_dump(mode="json"),
                            "attempt": attempt + 1,
                        }
                    ): retry_at
                },
            )
            return "retry_scheduled"

        await t_sess.execute(
            text(
                "UPDATE messages SET delivery_status = 'failed', "
                "channel_metadata = channel_metadata || CAST(:meta AS jsonb) "
                "WHERE tenant_id = :tid AND id = :mid"
            ),
            {
                "tid": request_payload.tenant_id,
                "mid": request_payload.message_id,
                "meta": json.dumps(
                    {
                        "error_code": result.error_code,
                        "error_message": result.error_message,
                    }
                ),
            },
        )

    await _move_to_dlq(
        session_factory,
        tenant_id=request_payload.tenant_id,
        raw_event_id=None,
        channel_type=request_payload.channel_type.value,
        direction="outbound",
        external_event_id=str(request_payload.message_id),
        payload=request_payload.model_dump(mode="json"),
        error_code=str(result.error_code) if result.error_code else "ERR_OUTBOUND_FAILED",
        error_detail=result.error_message or "Outbound delivery permanently failed",
        attempt_count=attempt,
    )
    return "dead_letter"


@dlq_router.post("/{event_id}/replay")
async def replay_dead_letter_event(
    event_id: uuid.UUID,
    request: Request,
    principal: Annotated[CurrentPrincipal, Depends(_require_dlq_operator)],
) -> dict[str, Any]:
    """Replay a failed inbound or outbound dead-letter event within the current tenant."""
    session_factory = _get_session_factory(request)
    redis = _get_redis(request)
    async with tenant_session(session_factory, principal.tenant_id) as t_sess:
        dlq_row = (
            await t_sess.execute(
                text(
                    "SELECT id, raw_webhook_event_id, channel_type, direction, payload, status "
                    "FROM dead_letter_events WHERE tenant_id = :tid AND id = :id"
                ),
                {"tid": principal.tenant_id, "id": event_id},
            )
        ).mappings().first()
        if dlq_row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Dead letter event not found",
            )

        await t_sess.execute(
            text(
                "UPDATE dead_letter_events SET status = 'replayed', replayed_at = now() "
                "WHERE tenant_id = :tid AND id = :id"
            ),
            {"tid": principal.tenant_id, "id": event_id},
        )
        await record_audit_event(
            session=t_sess,
            tenant_id=principal.tenant_id,
            actor_type="human_user",
            actor_id=str(principal.user_id),
            action="channel.dlq_replayed",
            resource_type="dead_letter_event",
            resource_id=event_id,
            before_state={"status": dlq_row["status"]},
            after_state={"status": "replayed"},
        )

    outcome = "queued"
    if dlq_row["direction"] == "inbound" and dlq_row["raw_webhook_event_id"] is not None:
        raw_id = dlq_row["raw_webhook_event_id"]
        broker_factory = getattr(request.app.state, "broker_session_factory", None)
        if broker_factory is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="webhook broker is not configured",
            )
        async with system_session(broker_factory) as sys_sess:
            await set_raw_webhook_processing_state(
                sys_sess,
                raw_event_id=raw_id,
                status="queued",
                attempt=1,
                error_message=None,
            )
        await redis.xadd(
            f"stream:webhooks:{dlq_row['channel_type']}",
            {
                "raw_event_id": str(raw_id),
                "channel_type": str(dlq_row["channel_type"]),
                "attempt": "1",
            },
        )
    elif dlq_row["direction"] == "outbound":
        await redis.xadd(
            "stream:outbound:replay",
            {"payload": json.dumps(dlq_row["payload"])},
        )

    return {
        "event_id": str(event_id),
        "status": "replayed",
        "outcome": outcome,
    }


def register_channel_worker_tasks(
    broker: RedisStreamBroker,
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    broker_session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    """Attach Taskiq webhook ingress and outbound dispatch tasks to the shared broker."""

    @broker.task
    async def process_webhook_event_task(raw_event_id: str, attempt: int = 1) -> str:
        return await process_raw_webhook_event(
            session_factory=session_factory,
            redis=redis,
            raw_event_id=uuid.UUID(raw_event_id),
            attempt=attempt,
            broker_session_factory=broker_session_factory,
        )

    @broker.task
    async def send_outbound_message_task(request_json: dict[str, Any], attempt: int = 1) -> str:
        req = OutboundMessageRequest.model_validate(request_json)
        crypto = EnvelopeCryptoService(get_settings())
        return await send_outbound_message(
            session_factory=session_factory,
            redis=redis,
            crypto_service=crypto,
            request_payload=req,
            attempt=attempt,
        )

    @broker.task
    async def drain_delayed_queues_task() -> int:
        return await drain_deferred_webhooks(session_factory, redis, broker_session_factory)

    @broker.task
    async def consume_webhook_streams_task() -> int:
        return await run_webhook_worker_once(
            session_factory,
            redis,
            broker_session_factory=broker_session_factory,
        )

    return {
        "process_webhook_event_task": process_webhook_event_task,
        "send_outbound_message_task": send_outbound_message_task,
        "drain_delayed_queues_task": drain_delayed_queues_task,
        "consume_webhook_streams_task": consume_webhook_streams_task,
    }
