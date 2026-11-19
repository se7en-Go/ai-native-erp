from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.shared.enums import OnboardingStage, UserRole
from erp_core.shared.errors import AppError
from erp_core.tenancy.audit import record_audit_event
from erp_core.tenancy.dependencies import CurrentPrincipal, get_tenant_db, require_roles

ALLOWED_PRESETS: frozenset[str] = frozenset(
    {"hybrid_factory_b2b_b2c", "dtc_brand", "pure_b2b_inquiry"}
)

LINEAR_TRANSITIONS: dict[OnboardingStage, OnboardingStage] = {
    OnboardingStage.WORKSPACE_CREATED: OnboardingStage.CHANNEL_CONNECTING,
    OnboardingStage.CHANNEL_CONNECTING: OnboardingStage.CHANNEL_VERIFIED,
    OnboardingStage.CHANNEL_VERIFIED: OnboardingStage.CATALOG_SYNCING,
    OnboardingStage.CATALOG_SYNCING: OnboardingStage.AI_SANDBOX_READY,
    OnboardingStage.AI_SANDBOX_READY: OnboardingStage.LIVE_PRODUCTION,
}


class OnboardingStateRead(BaseModel):
    tenant_id: UUID
    current_stage: OnboardingStage
    preset_type: str
    completed_steps: list[str]
    metadata_json: dict[str, Any]
    updated_at: datetime


class AdvanceOnboardingRequest(BaseModel):
    target_stage: OnboardingStage
    step_metadata: dict[str, Any] = Field(default_factory=dict)
    skip_catalog_for_b2b: bool = False


class UpdateOnboardingPresetRequest(BaseModel):
    preset_type: Literal["hybrid_factory_b2b_b2c", "dtc_brand", "pure_b2b_inquiry"]


class OnboardingService:
    async def get_state(self, session: AsyncSession, tenant_id: UUID) -> OnboardingStateRead:
        row = (
            await session.execute(
                text(
                    "SELECT tenant_id, current_stage, preset_type, completed_steps, "
                    "metadata_json, updated_at "
                    "FROM onboarding_states WHERE tenant_id = :tid"
                ),
                {"tid": str(tenant_id)},
            )
        ).first()
        if row is None:
            raise AppError(
                slug="onboarding-state-missing",
                title="Onboarding State Not Found",
                status_code=404,
                detail="No onboarding state record exists for tenant",
                code="ERR_NOT_FOUND",
            )
        return OnboardingStateRead(
            tenant_id=UUID(str(row.tenant_id)),
            current_stage=OnboardingStage(str(row.current_stage)),
            preset_type=str(row.preset_type),
            completed_steps=list(row.completed_steps or []),
            metadata_json=dict(row.metadata_json or {}),
            updated_at=row.updated_at,
        )

    async def update_preset(
        self,
        session: AsyncSession,
        principal: CurrentPrincipal,
        req: UpdateOnboardingPresetRequest,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> OnboardingStateRead:
        current = await self.get_state(session, principal.tenant_id)
        if req.preset_type not in ALLOWED_PRESETS:
            raise AppError(
                slug="invalid-onboarding-preset",
                title="Invalid Business Preset",
                status_code=422,
                detail=f"Unsupported preset_type '{req.preset_type}'",
                code="ERR_INVALID_ONBOARDING_PRESET",
            )
        await session.execute(
            text(
                "UPDATE tenants SET business_preset = :preset, updated_at = now() WHERE id = :tid"
            ),
            {"preset": req.preset_type, "tid": str(principal.tenant_id)},
        )
        row = (
            await session.execute(
                text(
                    "UPDATE onboarding_states "
                    "SET preset_type = :preset, updated_at = now() "
                    "WHERE tenant_id = :tid "
                    "RETURNING tenant_id, current_stage, preset_type, completed_steps, "
                    "metadata_json, updated_at"
                ),
                {"preset": req.preset_type, "tid": str(principal.tenant_id)},
            )
        ).one()
        await record_audit_event(
            session,
            principal.tenant_id,
            "human_user",
            str(principal.user_id),
            "onboarding.preset_updated",
            "onboarding_state",
            principal.tenant_id,
            before_state={"preset_type": current.preset_type},
            after_state={"preset_type": req.preset_type},
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return OnboardingStateRead(
            tenant_id=UUID(str(row.tenant_id)),
            current_stage=OnboardingStage(str(row.current_stage)),
            preset_type=str(row.preset_type),
            completed_steps=list(row.completed_steps or []),
            metadata_json=dict(row.metadata_json or {}),
            updated_at=row.updated_at,
        )

    async def advance_stage(
        self,
        session: AsyncSession,
        principal: CurrentPrincipal,
        req: AdvanceOnboardingRequest,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> OnboardingStateRead:
        current = await self.get_state(session, principal.tenant_id)
        expected_next = LINEAR_TRANSITIONS.get(current.current_stage)
        b2b_shortcut_allowed = (
            current.current_stage == OnboardingStage.CHANNEL_VERIFIED
            and req.target_stage == OnboardingStage.AI_SANDBOX_READY
            and (req.skip_catalog_for_b2b or current.preset_type == "pure_b2b_inquiry")
        )

        if req.target_stage != expected_next and not b2b_shortcut_allowed:
            raise AppError(
                slug="invalid-onboarding-transition",
                title="Invalid Onboarding Stage Transition",
                status_code=422,
                detail=(
                    f"Cannot transition from '{current.current_stage.value}' "
                    f"to '{req.target_stage.value}'"
                ),
                code="ERR_INVALID_ONBOARDING_TRANSITION",
            )

        completed = list(current.completed_steps)
        if current.current_stage.value not in completed:
            completed.append(current.current_stage.value)
        if req.target_stage.value not in completed:
            completed.append(req.target_stage.value)

        merged_metadata = {**current.metadata_json, **req.step_metadata}
        if b2b_shortcut_allowed:
            merged_metadata["skipped_catalog_sync_for_b2b"] = True

        row = (
            await session.execute(
                text(
                    "UPDATE onboarding_states "
                    "SET current_stage = :stage, "
                    "    completed_steps = CAST(:steps AS jsonb), "
                    "    metadata_json = CAST(:meta AS jsonb), "
                    "    updated_at = now() "
                    "WHERE tenant_id = :tid "
                    "RETURNING tenant_id, current_stage, preset_type, completed_steps, "
                    "metadata_json, updated_at"
                ),
                {
                    "stage": req.target_stage.value,
                    "steps": json.dumps(completed),
                    "meta": json.dumps(merged_metadata, default=str),
                    "tid": str(principal.tenant_id),
                },
            )
        ).one()

        await record_audit_event(
            session,
            principal.tenant_id,
            "human_user",
            str(principal.user_id),
            "onboarding.stage_advanced",
            "onboarding_state",
            principal.tenant_id,
            before_state={"current_stage": current.current_stage.value},
            after_state={
                "current_stage": req.target_stage.value,
                "b2b_shortcut": b2b_shortcut_allowed,
            },
            ip_address=ip_address,
            user_agent=user_agent,
        )

        return OnboardingStateRead(
            tenant_id=UUID(str(row.tenant_id)),
            current_stage=OnboardingStage(str(row.current_stage)),
            preset_type=str(row.preset_type),
            completed_steps=list(row.completed_steps or []),
            metadata_json=dict(row.metadata_json or {}),
            updated_at=row.updated_at,
        )


onboarding_router = APIRouter(prefix="/api/v1/onboarding", tags=["onboarding"])


@onboarding_router.get("/state", response_model=OnboardingStateRead)
async def get_onboarding_state(
    principal: Annotated[
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
    ],
    session: Annotated[AsyncSession, Depends(get_tenant_db)],
) -> OnboardingStateRead:
    return await OnboardingService().get_state(session, principal.tenant_id)


@onboarding_router.post("/advance", response_model=OnboardingStateRead)
async def advance_onboarding_stage(
    payload: AdvanceOnboardingRequest,
    request: Request,
    principal: Annotated[CurrentPrincipal, Depends(require_roles(UserRole.OWNER, UserRole.ADMIN))],
    session: Annotated[AsyncSession, Depends(get_tenant_db)],
) -> OnboardingStateRead:
    return await OnboardingService().advance_stage(
        session,
        principal,
        payload,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


@onboarding_router.patch("/preset", response_model=OnboardingStateRead)
async def update_onboarding_preset(
    payload: UpdateOnboardingPresetRequest,
    request: Request,
    principal: Annotated[CurrentPrincipal, Depends(require_roles(UserRole.OWNER, UserRole.ADMIN))],
    session: Annotated[AsyncSession, Depends(get_tenant_db)],
) -> OnboardingStateRead:
    return await OnboardingService().update_preset(
        session,
        principal,
        payload,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
