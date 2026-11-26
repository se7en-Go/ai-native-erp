"""`/api/v1/catalog/visual-jobs` - sourcing visual-AI processing.

The router owns the fully-qualified path (already admitted by the operator BFF
allowlist via the `catalog` prefix), so the composition root only needs
``include_router(visual_router)``.

RBAC: every tenant member may read jobs (`OWNER`/`ADMIN`/`MANAGER`/`AGENT`/
`FINANCE`), while only `OWNER`/`ADMIN`/`MANAGER`/`AGENT` may create or delete
one - `FINANCE` has no business rendering product imagery.

`POST` runs the provider inline: `sandbox` always succeeds offline, and an
external provider without a configured API key is recorded as a `failed` job
with a clear non-secret reason instead of a 5xx.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.catalog.visual_schemas import VisualJobCreate, VisualJobRead
from erp_core.catalog.visual_service import VisualJobService
from erp_core.shared.enums import UserRole
from erp_core.shared.storage import ObjectStorageService
from erp_core.tenancy.dependencies import CurrentPrincipal, get_tenant_db, require_roles

router = APIRouter(prefix="/api/v1/catalog/visual-jobs", tags=["catalog-visual"])

#: Roles allowed to see the studio job list.
VISUAL_READ_ROLES: tuple[UserRole, ...] = (
    UserRole.OWNER,
    UserRole.ADMIN,
    UserRole.MANAGER,
    UserRole.AGENT,
    UserRole.FINANCE,
)
#: Roles allowed to burn provider credits by creating or deleting a job.
VISUAL_WRITE_ROLES: tuple[UserRole, ...] = (
    UserRole.OWNER,
    UserRole.ADMIN,
    UserRole.MANAGER,
    UserRole.AGENT,
)

TenantDB = Annotated[AsyncSession, Depends(get_tenant_db)]
VisualReader = Annotated[CurrentPrincipal, Depends(require_roles(*VISUAL_READ_ROLES))]
VisualWriter = Annotated[CurrentPrincipal, Depends(require_roles(*VISUAL_WRITE_ROLES))]


def _service(request: Request) -> VisualJobService:
    """Use the composition root's provider registry when it injected one.

    Tests replace `app.state.visual_providers` with providers carrying an
    `httpx.MockTransport`, which is how the remove.bg / OpenAI adapters are
    exercised deterministically without a network call.
    """
    providers = getattr(request.app.state, "visual_providers", None)
    return VisualJobService(providers=providers)


def _storage(request: Request) -> ObjectStorageService | None:
    storage = getattr(request.app.state, "storage", None)
    return storage if isinstance(storage, ObjectStorageService) else None


@router.get("", response_model=list[VisualJobRead])
async def list_visual_jobs(
    request: Request,
    principal: VisualReader,
    session: TenantDB,
    limit: int = Query(default=50, ge=1, le=50),
) -> list[VisualJobRead]:
    """Latest visual jobs for the caller's tenant, newest first (capped at 50)."""
    return await _service(request).list_jobs(session, principal.tenant_id, limit=limit)


@router.post("", response_model=VisualJobRead, status_code=201)
async def create_visual_job(
    request: Request,
    payload: VisualJobCreate,
    principal: VisualWriter,
    session: TenantDB,
) -> VisualJobRead:
    """Validate the source image, run the provider and persist the terminal state."""
    return await _service(request).create_job(
        session,
        principal.tenant_id,
        principal.user_id,
        payload,
        storage=_storage(request),
    )


@router.get("/{job_id}", response_model=VisualJobRead)
async def get_visual_job(
    request: Request,
    job_id: UUID,
    principal: VisualReader,
    session: TenantDB,
) -> VisualJobRead:
    return await _service(request).get_job(session, principal.tenant_id, job_id)


@router.delete("/{job_id}", status_code=204)
async def delete_visual_job(
    request: Request,
    job_id: UUID,
    principal: VisualWriter,
    session: TenantDB,
) -> None:
    """Delete the job row and best-effort drop its stored object."""
    await _service(request).delete_job(
        session,
        principal.tenant_id,
        job_id,
        storage=_storage(request),
    )
