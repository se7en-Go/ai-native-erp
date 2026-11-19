from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from erp_core.shared.enums import LeadStatus

FollowupType = Literal["email", "call", "meeting", "note"]
TaskPriority = Literal["low", "normal", "high"]
TaskStatus = Literal["open", "done", "cancelled"]
LinkedEntity = Literal["customer", "lead", "opportunity", "quote", "order", "purchase_order"]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
Summary = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=5000)]


class LeadUpdate(BaseModel):
    status: LeadStatus | None = None
    qualification_score: int | None = Field(default=None, ge=0, le=100)


class FollowupCreate(BaseModel):
    customer_id: UUID
    opportunity_id: UUID | None = None
    followup_type: FollowupType
    summary: Summary
    next_action_at: AwareDatetime | None = None


class FollowupUpdate(BaseModel):
    followup_type: FollowupType | None = None
    summary: Summary | None = None
    next_action_at: AwareDatetime | None = None


class FollowupRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    customer_id: UUID
    opportunity_id: UUID | None
    author_user_id: UUID
    followup_type: FollowupType
    summary: str
    next_action_at: datetime | None
    created_at: datetime


class TaskCreate(BaseModel):
    title: Title
    due_at: AwareDatetime | None = None
    priority: TaskPriority = "normal"
    linked_entity_type: LinkedEntity | None = None
    linked_entity_id: UUID | None = None
    assignee_user_id: UUID | None = None

    @model_validator(mode="after")
    def validate_link(self) -> TaskCreate:
        if (self.linked_entity_type is None) != (self.linked_entity_id is None):
            raise ValueError("linked_entity_type and linked_entity_id must be supplied together")
        return self


class TaskUpdate(BaseModel):
    title: Title | None = None
    due_at: AwareDatetime | None = None
    priority: TaskPriority | None = None
    status: TaskStatus | None = None
    assignee_user_id: UUID | None = None


class TaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    due_at: datetime | None
    priority: TaskPriority
    status: TaskStatus
    linked_entity_type: LinkedEntity | None
    linked_entity_id: UUID | None
    assignee_user_id: UUID | None
    created_at: datetime
