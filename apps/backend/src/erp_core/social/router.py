"""Phase 3D social marketing API: 14-platform matrix, calendar and mention listening.

Every route runs inside a tenant-scoped transaction (`get_tenant_db`) so the
`RESTRICTIVE` RLS policies enforce cross-tenant isolation, and RBAC is enforced by
`require_roles`: writes are Owner/Admin/Manager, reads also allow Agent/Finance.

AI caption generation is composed here through the `CaptionProvider` protocol so
`erp_core.social` never imports `erp_core.ai_engine` (import-linter `social_isolated`).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.enums import UserRole
from erp_core.shared.pagination import PaginatedResponse
from erp_core.social.caption import CaptionProvider, CaptionRequest
from erp_core.social.schemas import (
    AccountConnect,
    AccountRead,
    CaptionGenerate,
    CaptionRead,
    ConvertLeadResult,
    MentionCreate,
    MentionRead,
    PlatformRead,
    PostCreate,
    PostPublish,
    PostRead,
    PostUpdate,
    SocialSummary,
)
from erp_core.social.service import SocialService
from erp_core.tenancy.dependencies import CurrentPrincipal, get_tenant_db, require_roles

router = APIRouter(prefix="/social", tags=["social"])

_MANAGERS = (UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER)
Operator = Annotated[CurrentPrincipal, Depends(require_roles(*_MANAGERS))]
Reader = Annotated[CurrentPrincipal, Depends(require_roles(*UserRole))]
TenantDB = Annotated[AsyncSession, Depends(get_tenant_db)]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]


def _service(request: Request) -> SocialService:
    crypto = getattr(request.app.state, "crypto_service", None)
    if crypto is None:  # pragma: no cover - the composition root always sets it
        from erp_core.shared.config import get_settings

        crypto = EnvelopeCryptoService(get_settings())
    return SocialService(crypto)


def _caption_provider(request: Request) -> CaptionProvider:
    provider = getattr(request.app.state, "social_caption_provider", None)
    if provider is None:
        from erp_core.social.service import caption_provider_unavailable

        raise caption_provider_unavailable()
    return provider  # type: ignore[no-any-return]


Service = Annotated[SocialService, Depends(_service)]
Caption = Annotated[CaptionProvider, Depends(_caption_provider)]


@router.get("/summary")
async def social_summary(session: TenantDB, principal: Reader, service: Service) -> SocialSummary:
    return await service.summary(session, principal.tenant_id)


@router.get("/platforms")
async def list_platforms(principal: Reader, service: Service) -> list[PlatformRead]:
    """The locked 14-platform matrix with honest publishing capability per platform."""
    return service.platforms()


@router.get("/accounts")
async def list_accounts(
    session: TenantDB,
    principal: Reader,
    service: Service,
    limit: Limit = 50,
    offset: Offset = 0,
    platform: str | None = Query(default=None),
    status: str | None = Query(default=None, pattern="^(connected|disconnected|token_expired)$"),
) -> PaginatedResponse[AccountRead]:
    return await service.list_accounts(
        session,
        principal.tenant_id,
        limit=limit,
        offset=offset,
        platform=platform,
        status=status,
    )


@router.post("/accounts/connect")
async def connect_account(
    payload: AccountConnect,
    session: TenantDB,
    principal: Operator,
    service: Service,
    response: Response,
) -> AccountRead:
    """Authorize (or re-authorize) an account. The token is write-only and encrypted."""
    account, created = await service.connect_account(
        session, principal.tenant_id, principal.user_id, payload
    )
    response.status_code = 201 if created else 200
    return account


@router.post("/accounts/{account_id}/disconnect")
async def disconnect_account(
    account_id: UUID, session: TenantDB, principal: Operator, service: Service
) -> AccountRead:
    return await service.disconnect_account(session, principal.tenant_id, account_id)


@router.get("/posts")
async def list_posts(
    session: TenantDB,
    principal: Reader,
    service: Service,
    limit: Limit = 50,
    offset: Offset = 0,
    platform: str | None = Query(default=None),
    status: str | None = Query(default=None, pattern="^(draft|scheduled|published|failed)$"),
) -> PaginatedResponse[PostRead]:
    return await service.list_posts(
        session,
        principal.tenant_id,
        limit=limit,
        offset=offset,
        platform=platform,
        status=status,
    )


@router.post("/posts", status_code=201)
async def create_post(
    payload: PostCreate, session: TenantDB, principal: Operator, service: Service
) -> PostRead:
    return await service.create_post(session, principal.tenant_id, principal.user_id, payload)


@router.patch("/posts/{post_id}")
async def update_post(
    post_id: UUID,
    payload: PostUpdate,
    session: TenantDB,
    principal: Operator,
    service: Service,
) -> PostRead:
    return await service.update_post(session, principal.tenant_id, post_id, payload)


@router.post("/posts/{post_id}/publish")
async def publish_post(
    post_id: UUID,
    payload: PostPublish,
    session: TenantDB,
    principal: Operator,
    service: Service,
) -> PostRead:
    return await service.publish_post(session, principal.tenant_id, post_id, payload)


@router.post("/caption/generate")
async def generate_caption(
    payload: CaptionGenerate,
    session: TenantDB,
    principal: Operator,
    provider: Caption,
) -> CaptionRead:
    """AI copywriting; fails closed (503) when no LLM provider is configured."""
    result = await provider.generate_caption(
        session,
        principal.tenant_id,
        CaptionRequest(
            platform=payload.platform,
            prompt=payload.prompt,
            sku_ref=payload.sku_ref,
            language=payload.language,
            tone=payload.tone,
        ),
    )
    return CaptionRead(
        platform=payload.platform,
        text=result.text,
        language=payload.language,
        sku_ref=payload.sku_ref,
        model_used=result.model_used,
        credits_consumed=str(result.credits_consumed),
        grounded_tool_call_id=result.grounded_tool_call_id,
    )


@router.get("/mentions")
async def list_mentions(
    session: TenantDB,
    principal: Reader,
    service: Service,
    limit: Limit = 50,
    offset: Offset = 0,
    platform: str | None = Query(default=None),
    sentiment: str | None = Query(
        default=None, pattern="^(positive|neutral|negative|lead_intent)$"
    ),
) -> PaginatedResponse[MentionRead]:
    return await service.list_mentions(
        session,
        principal.tenant_id,
        limit=limit,
        offset=offset,
        platform=platform,
        sentiment=sentiment,
    )


@router.post("/mentions", status_code=201)
async def create_mention(
    payload: MentionCreate, session: TenantDB, principal: Operator, service: Service
) -> MentionRead:
    return await service.create_mention(session, principal.tenant_id, payload)


@router.post("/mentions/{mention_id}/convert-lead")
async def convert_mention(
    mention_id: UUID, session: TenantDB, principal: Operator, service: Service
) -> ConvertLeadResult:
    return await service.convert_mention_to_lead(session, principal.tenant_id, mention_id)
