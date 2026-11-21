from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator
from typing import Annotated, cast
from urllib.parse import quote
from uuid import UUID

import redis.asyncio as aioredis
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
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
    ALLOWED_INBOX_ATTACHMENT_MIME_TYPES,
    MAX_INBOX_ATTACHMENT_BYTES,
    AttachmentStore,
    AttachmentUnavailableError,
    AttachmentValidationError,
    ConversationAccessDeniedError,
    InboxService,
    event_visible_to_agent,
)
from erp_core.shared.config import get_settings
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import ChannelType, UserRole
from erp_core.shared.pagination import PaginatedResponse
from erp_core.shared.storage import ObjectStorageService
from erp_core.tenancy.dependencies import CurrentPrincipal, require_roles

# SSE comment frames keep idle proxies/CDNs from dropping the stream. The interval is a
# module constant so tests can shrink it without patching wall-clock behaviour.
SSE_HEARTBEAT_INTERVAL_SECONDS: float = 15.0

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


def _get_storage(request: Request) -> AttachmentStore | None:
    """Reuse the app-wide object storage, or degrade gracefully when S3 is absent."""
    storage = getattr(request.app.state, "storage", None)
    if storage is not None:
        return cast(AttachmentStore, storage)
    try:
        return ObjectStorageService(get_settings())
    except Exception:  # pragma: no cover - only when settings/boto3 are unavailable
        return None


async def _read_attachment_upload(file: UploadFile) -> tuple[str, bytes]:
    """Read the multipart body while refusing to buffer more than the 10 MB ceiling."""
    mime_type = (file.content_type or "application/octet-stream").split(";", 1)[0].strip().lower()
    if mime_type not in ALLOWED_INBOX_ATTACHMENT_MIME_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ERR_ATTACHMENT_TYPE_NOT_ALLOWED",
        )
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(1024 * 1024):
        total += len(chunk)
        if total > MAX_INBOX_ATTACHMENT_BYTES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="ERR_ATTACHMENT_TOO_LARGE",
            )
        chunks.append(chunk)
    return mime_type, b"".join(chunks)


@inbox_router.get("/conversations", response_model=PaginatedResponse[ConversationRead])
async def list_inbox_conversations(
    request: Request,
    principal: Annotated[CurrentPrincipal, Depends(_require_inbox_access)],
    tab: Annotated[InboxTabFilter | None, Query()] = None,
    filter: Annotated[InboxTabFilter | None, Query()] = None,
    channel_type: Annotated[ChannelType | None, Query()] = None,
    channel: Annotated[ChannelType | None, Query()] = None,
    channel_account_id: Annotated[UUID | None, Query()] = None,
    assigned_user_id: Annotated[UUID | None, Query()] = None,
    tag: Annotated[str | None, Query(max_length=32)] = None,
    pinned_only: Annotated[bool, Query()] = False,
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
            channel_account_id=channel_account_id,
            assigned_user_id=assigned_user_id,
            tag=tag,
            pinned_only=pinned_only,
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


@inbox_router.post(
    "/conversations/{conversation_id}/attachments",
    response_model=SendInboxMessageResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_conversation_attachment(
    conversation_id: UUID,
    request: Request,
    file: Annotated[UploadFile, File()],
    principal: Annotated[CurrentPrincipal, Depends(_require_inbox_access)],
    caption: Annotated[str | None, Form(max_length=1024)] = None,
) -> SendInboxMessageResponse:
    """Upload an image/PDF/CSV attachment and deliver it on the conversation's channel."""
    mime_type, content = await _read_attachment_upload(file)
    session_factory = _get_session_factory(request)
    redis = _get_redis(request)
    enqueue_fn = getattr(request.app.state, "outbound_enqueue_fn", None)
    service = InboxService()
    async with tenant_session(session_factory, principal.tenant_id) as session:
        try:
            return await service.upload_attachment(
                session=session,
                redis=redis,
                tenant_id=principal.tenant_id,
                principal=principal,
                conversation_id=conversation_id,
                filename=file.filename or "attachment",
                mime_type=mime_type,
                content=content,
                caption=caption,
                storage=_get_storage(request),
                outbound_enqueue_fn=enqueue_fn,
            )
        except LookupError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ConversationAccessDeniedError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="ERR_CONVERSATION_ACCESS_DENIED",
            ) from exc
        except AttachmentValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        except PermissionError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        except AttachmentUnavailableError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc


@inbox_router.get("/attachments/{message_id}/{attachment_index}")
async def get_inbox_attachment(
    message_id: UUID,
    attachment_index: int,
    request: Request,
    principal: Annotated[CurrentPrincipal, Depends(_require_inbox_access)],
) -> Response:
    """Stream one stored attachment back through the tenant/agent isolation gate."""
    session_factory = _get_session_factory(request)
    service = InboxService()
    async with tenant_session(session_factory, principal.tenant_id) as session:
        try:
            data, mime_type, filename = await service.download_attachment(
                session=session,
                tenant_id=principal.tenant_id,
                principal=principal,
                message_id=message_id,
                attachment_index=attachment_index,
                storage=_get_storage(request),
            )
        except LookupError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ConversationAccessDeniedError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="ERR_CONVERSATION_ACCESS_DENIED",
            ) from exc
        except AttachmentUnavailableError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc
    ascii_name = filename.encode("ascii", "replace").decode("ascii").replace('"', "_")
    headers = {
        "Content-Disposition": (
            f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
        ),
        "X-Content-Type-Options": "nosniff",
    }
    return Response(content=data, media_type=mime_type, headers=headers)


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
        last_activity = time.monotonic()
        try:
            while True:
                if await request.is_disconnected():
                    break
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg and msg.get("type") == "message":
                    last_activity = time.monotonic()
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
                    # Idle heartbeat. Keepalive frames are SSE comments, so they never
                    # count towards max_events and never surface as client events.
                    if time.monotonic() - last_activity >= SSE_HEARTBEAT_INTERVAL_SECONDS:
                        yield ": keepalive\n\n"
                        last_activity = time.monotonic()
        finally:
            await pubsub.unsubscribe(channel_name)
            await pubsub.aclose()  # type: ignore[no-untyped-call]

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )
