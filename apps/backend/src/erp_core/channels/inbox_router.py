from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.channels.inbox_schemas import (
    ConversationRead,
    InboxTabFilter,
    MessageRead,
    SendInboxMessageRequest,
    SendInboxMessageResponse,
    UpdateConversationRequest,
)
from erp_core.channels.inbox_service import (
    ConversationAccessDeniedError,
    InboxService,
    event_visible_to_agent,
)
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import ChannelType, UserRole
from erp_core.shared.pagination import PaginatedResponse
from erp_core.tenancy.dependencies import CurrentPrincipal, require_roles

inbox_router = APIRouter(prefix="/api/v1/inbox", tags=["inbox"])
_require_inbox_access = require_roles(
    UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT
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


@inbox_router.get("/conversations", response_model=PaginatedResponse[ConversationRead])
async def list_inbox_conversations(
    request: Request,
    principal: Annotated[CurrentPrincipal, Depends(_require_inbox_access)],
    tab: Annotated[InboxTabFilter | None, Query()] = None,
    filter: Annotated[InboxTabFilter | None, Query()] = None,
    channel_type: Annotated[ChannelType | None, Query()] = None,
    channel: Annotated[ChannelType | None, Query()] = None,
    assigned_user_id: Annotated[UUID | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PaginatedResponse[ConversationRead]:
    session_factory = _get_session_factory(request)
    service = InboxService()
    effective_tab: InboxTabFilter = tab or filter or "all"
    effective_channel = channel_type or channel
    async with tenant_session(session_factory, principal.tenant_id) as session:
        return await service.list_conversations(
            session=session,
            tenant_id=principal.tenant_id,
            tab=effective_tab,
            channel_type=effective_channel,
            assigned_user_id=assigned_user_id,
            q=q,
            limit=limit,
            offset=offset,
            principal=principal,
        )


@inbox_router.get(
    "/conversations/{conversation_id}/messages",
    response_model=PaginatedResponse[MessageRead],
)
async def list_conversation_messages(
    conversation_id: UUID,
    request: Request,
    principal: Annotated[CurrentPrincipal, Depends(_require_inbox_access)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    mark_read: Annotated[bool, Query()] = True,
) -> PaginatedResponse[MessageRead]:
    session_factory = _get_session_factory(request)
    redis = _get_redis(request)
    service = InboxService()
    async with tenant_session(session_factory, principal.tenant_id) as session:
        try:
            return await service.list_messages(
                session=session,
                redis=redis,
                tenant_id=principal.tenant_id,
                conversation_id=conversation_id,
                limit=limit,
                offset=offset,
                mark_read=mark_read,
                principal=principal,
            )
        except LookupError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ConversationAccessDeniedError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="ERR_CONVERSATION_ACCESS_DENIED",
            ) from exc


@inbox_router.post(
    "/conversations/{conversation_id}/messages",
    response_model=SendInboxMessageResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_conversation_message(
    conversation_id: UUID,
    payload: SendInboxMessageRequest,
    request: Request,
    principal: Annotated[CurrentPrincipal, Depends(_require_inbox_access)],
) -> SendInboxMessageResponse:
    session_factory = _get_session_factory(request)
    redis = _get_redis(request)
    enqueue_fn = getattr(request.app.state, "outbound_enqueue_fn", None)
    service = InboxService()
    async with tenant_session(session_factory, principal.tenant_id) as session:
        try:
            return await service.send_message(
                session=session,
                redis=redis,
                tenant_id=principal.tenant_id,
                principal=principal,
                conversation_id=conversation_id,
                req=payload,
                outbound_enqueue_fn=enqueue_fn,
            )
        except LookupError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ConversationAccessDeniedError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="ERR_CONVERSATION_ACCESS_DENIED",
            ) from exc
        except PermissionError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc


@inbox_router.patch(
    "/conversations/{conversation_id}",
    response_model=ConversationRead,
)
async def patch_conversation(
    conversation_id: UUID,
    payload: UpdateConversationRequest,
    request: Request,
    principal: Annotated[CurrentPrincipal, Depends(_require_inbox_access)],
) -> ConversationRead:
    session_factory = _get_session_factory(request)
    redis = _get_redis(request)
    service = InboxService()
    async with tenant_session(session_factory, principal.tenant_id) as session:
        try:
            return await service.update_conversation(
                session=session,
                redis=redis,
                tenant_id=principal.tenant_id,
                principal=principal,
                conversation_id=conversation_id,
                req=payload,
            )
        except LookupError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ConversationAccessDeniedError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="ERR_CONVERSATION_ACCESS_DENIED",
            ) from exc


@inbox_router.get("/stream")
@inbox_router.get("/events", include_in_schema=False)
async def stream_inbox_events(
    request: Request,
    principal: Annotated[CurrentPrincipal, Depends(_require_inbox_access)],
    max_events: Annotated[int | None, Query(ge=1)] = None,
) -> StreamingResponse:
    """Stream tenant-isolated real-time inbox events via Redis Pub/Sub SSE."""
    redis = _get_redis(request)
    channel_name = f"inbox:events:{principal.tenant_id}"

    async def _event_generator() -> AsyncIterator[str]:
        pubsub = redis.pubsub()
        await pubsub.subscribe(channel_name)
        yield f"event: connected\ndata: {json.dumps({'tenant_id': str(principal.tenant_id)})}\n\n"
        emitted = 0
        try:
            while True:
                if await request.is_disconnected():
                    break
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg and msg.get("type") == "message":
                    raw_data = msg["data"]
                    data_str = (
                        raw_data.decode("utf-8") if isinstance(raw_data, bytes) else str(raw_data)
                    )
                    parsed = json.loads(data_str)
                    if not event_visible_to_agent(principal, parsed):
                        continue
                    event_name = parsed.get("event", "message")
                    yield f"event: {event_name}\ndata: {data_str}\n\n"
                    emitted += 1
                    if max_events is not None and emitted >= max_events:
                        break
                else:
                    await asyncio.sleep(0.05)
        finally:
            await pubsub.unsubscribe(channel_name)
            await pubsub.aclose()  # type: ignore[no-untyped-call]

    return StreamingResponse(_event_generator(), media_type="text/event-stream")
