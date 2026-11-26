"""HTTP surface for external notifications and event-driven automation rules.

Mounted under `/api/v1/ai`, so the existing Next.js BFF prefix `/api/v1/ai` already
proxies it and no BFF route needs to change.

RBAC:

* read (`GET`) - OWNER, ADMIN, MANAGER, AGENT, FINANCE
* manage (`POST`, `PATCH`, `DELETE`, evaluate) - OWNER, ADMIN, MANAGER

Routes never accept or return the plaintext signing secret; creation may set it and the
read model only reports `has_signing_secret` plus a masked URL preview.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.ai_engine.automation_service import AutomationService
from erp_core.ai_engine.notification_schemas import (
    AutomationEvaluationReport,
    AutomationEvaluationRequest,
    AutomationRuleRead,
    AutomationRuleUpdate,
    AutomationSeedResult,
    DeliveryRetrySweepResult,
    DeliveryStatus,
    NotificationChannelCreate,
    NotificationChannelRead,
    NotificationChannelUpdate,
    NotificationDeliveryRead,
    NotificationTestResult,
)
from erp_core.ai_engine.notification_service import NotificationService
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.enums import UserRole
from erp_core.shared.pagination import PaginatedResponse
from erp_core.tenancy.dependencies import (
    CurrentPrincipal,
    get_current_principal,
    get_tenant_db,
    require_roles,
)

router = APIRouter(prefix="/api/v1/ai", tags=["ai-notifications", "ai-automation"])

Principal = Annotated[CurrentPrincipal, Depends(get_current_principal)]
NotificationReader = Annotated[
    CurrentPrincipal,
    Depends(
        require_roles(
            UserRole.OWNER,
            UserRole.ADMIN,
            UserRole.MANAGER,
            UserRole.AGENT,
            UserRole.FINANCE,
        )
    ),
]
NotificationManager = Annotated[
    CurrentPrincipal,
    Depends(require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER)),
]
Db = Annotated[AsyncSession, Depends(get_tenant_db)]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]


def _notification_service(request: Request) -> NotificationService:
    """Factory seam: tests inject a mock transport, production reuses the app crypto."""
    override = getattr(request.app.state, "notification_service", None)
    if isinstance(override, NotificationService):
        return override
    crypto = getattr(request.app.state, "crypto_service", None)
    return NotificationService(
        crypto=crypto if isinstance(crypto, EnvelopeCryptoService) else None
    )


def _automation_service(request: Request) -> AutomationService:
    override = getattr(request.app.state, "automation_service", None)
    if isinstance(override, AutomationService):
        return override
    return AutomationService()


Notification = Annotated[NotificationService, Depends(_notification_service)]
Automation = Annotated[AutomationService, Depends(_automation_service)]


def _client_context(request: Request) -> tuple[str | None, str | None]:
    ip_address = request.client.host if request.client is not None else None
    return ip_address, request.headers.get("user-agent")


# -- notification channels --------------------------------------------------


@router.get("/notifications/channels", response_model=list[NotificationChannelRead])
async def list_notification_channels(
    session: Db,
    principal: NotificationReader,
    service: Notification,
) -> list[NotificationChannelRead]:
    """Tenant-scoped channels, secret-free (masked URL preview only)."""
    return await service.list_channels(session, principal.tenant_id)


@router.post(
    "/notifications/channels",
    response_model=NotificationChannelRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_notification_channel(
    body: NotificationChannelCreate,
    request: Request,
    session: Db,
    principal: NotificationManager,
    service: Notification,
) -> NotificationChannelRead:
    """Create a Feishu / DingTalk / WeCom / generic webhook channel (SSRF-validated)."""
    ip_address, user_agent = _client_context(request)
    return await service.create_channel(
        session,
        principal.tenant_id,
        body,
        actor_id=principal.user_id,
        ip_address=ip_address,
        user_agent=user_agent,
    )


@router.patch("/notifications/channels/{channel_id}", response_model=NotificationChannelRead)
async def update_notification_channel(
    channel_id: UUID,
    body: NotificationChannelUpdate,
    request: Request,
    session: Db,
    principal: NotificationManager,
    service: Notification,
) -> NotificationChannelRead:
    """Update name / URL / secret / minimum priority / enabled flag."""
    ip_address, user_agent = _client_context(request)
    return await service.update_channel(
        session,
        principal.tenant_id,
        channel_id,
        body,
        actor_id=principal.user_id,
        ip_address=ip_address,
        user_agent=user_agent,
    )


@router.delete("/notifications/channels/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_notification_channel(
    channel_id: UUID,
    request: Request,
    session: Db,
    principal: NotificationManager,
    service: Notification,
) -> Response:
    """Delete a channel and (by FK cascade) its delivery ledger."""
    ip_address, user_agent = _client_context(request)
    await service.delete_channel(
        session,
        principal.tenant_id,
        channel_id,
        actor_id=principal.user_id,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/notifications/channels/{channel_id}/test", response_model=NotificationTestResult
)
async def test_notification_channel(
    channel_id: UUID,
    request: Request,
    session: Db,
    principal: NotificationManager,
    service: Notification,
) -> NotificationTestResult:
    """Send one real test ping and record the provider's HTTP outcome."""
    ip_address, user_agent = _client_context(request)
    return await service.test_channel(
        session,
        principal.tenant_id,
        channel_id,
        actor_id=principal.user_id,
        ip_address=ip_address,
        user_agent=user_agent,
    )


# -- delivery ledger --------------------------------------------------------


@router.get(
    "/notifications/deliveries", response_model=PaginatedResponse[NotificationDeliveryRead]
)
async def list_notification_deliveries(
    session: Db,
    principal: NotificationReader,
    service: Notification,
    limit: Limit = 20,
    offset: Offset = 0,
    delivery_status: Annotated[DeliveryStatus | None, Query(alias="status")] = None,
    channel_id: UUID | None = None,
) -> PaginatedResponse[NotificationDeliveryRead]:
    """Newest-first delivery log with the honest status, attempts and HTTP code."""
    return await service.list_deliveries(
        session,
        principal.tenant_id,
        limit=limit,
        offset=offset,
        status=delivery_status,
        channel_id=channel_id,
    )


@router.post(
    "/notifications/deliveries/retry-due", response_model=DeliveryRetrySweepResult
)
async def retry_due_notification_deliveries(
    session: Db,
    principal: NotificationManager,
    service: Notification,
) -> DeliveryRetrySweepResult:
    """Dispatch every `retrying` delivery whose 10-minute backoff elapsed."""
    return await service.retry_due_deliveries(session, principal.tenant_id)


# -- automation rules -------------------------------------------------------


@router.get("/automation-rules", response_model=list[AutomationRuleRead])
async def list_automation_rules(
    session: Db,
    principal: NotificationReader,
    service: Automation,
) -> list[AutomationRuleRead]:
    """Current rule rows (empty until `seed-defaults` or the first non-dry evaluation)."""
    return await service.list_rules(session, principal.tenant_id)


@router.post(
    "/automation-rules/seed-defaults",
    response_model=AutomationSeedResult,
    status_code=status.HTTP_201_CREATED,
)
async def seed_default_automation_rules(
    session: Db,
    principal: NotificationManager,
    service: Automation,
) -> AutomationSeedResult:
    """Idempotently create the five default templates for this tenant."""
    return await service.seed_default_rules(session, principal.tenant_id)


@router.patch("/automation-rules/{rule_id}", response_model=AutomationRuleRead)
async def update_automation_rule(
    rule_id: UUID,
    body: AutomationRuleUpdate,
    session: Db,
    principal: NotificationManager,
    service: Automation,
) -> AutomationRuleRead:
    """Toggle a rule, change its priority / autonomy level or edit thresholds."""
    return await service.update_rule(
        session,
        principal.tenant_id,
        rule_id,
        name=body.name,
        is_enabled=body.is_enabled,
        autonomy_level=body.autonomy_level,
        priority=body.priority,
        config_json=body.config_json,
    )


@router.post("/automation-rules/evaluate", response_model=AutomationEvaluationReport)
async def evaluate_automation_rules(
    session: Db,
    principal: NotificationManager,
    service: Automation,
    notification_service: Notification,
    body: AutomationEvaluationRequest | None = None,
) -> AutomationEvaluationReport:
    """Run all enabled rules; `dry_run=true` returns real matched counts and writes nothing."""
    dry_run = body.dry_run if body is not None else False
    return await service.evaluate_rules(
        session,
        principal.tenant_id,
        dry_run=dry_run,
        notification_service=notification_service,
    )
