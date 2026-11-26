from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

AccessLevel = Literal["read", "operate", "manage"]


class StoreScopeAssignRequest(BaseModel):
    """Grant (or re-level) one member's access to one connected store."""

    user_id: UUID
    channel_account_id: UUID
    access_level: AccessLevel = "operate"


class StoreScopeRead(BaseModel):
    id: UUID
    tenant_id: UUID
    user_id: UUID
    user_email: str | None = None
    user_full_name: str | None = None
    channel_account_id: UUID
    channel_type: str | None = None
    channel_display_name: str | None = None
    access_level: AccessLevel
    created_at: datetime


class HandoverRequest(BaseModel):
    """Offboarding handover: move every owned asset from one member to another."""

    from_user_id: UUID
    to_user_id: UUID
    deactivate_from_user: bool = False
    dry_run: bool = False


class HandoverResult(BaseModel):
    tenant_id: UUID
    from_user_id: UUID
    to_user_id: UUID
    dry_run: bool
    opportunities_reassigned: int = Field(ge=0)
    conversations_reassigned: int = Field(ge=0)
    channel_accounts_reassigned: int = Field(ge=0)
    store_scopes_reassigned: int = Field(ge=0)
    total_reassigned: int = Field(ge=0)
    from_user_deactivated: bool
