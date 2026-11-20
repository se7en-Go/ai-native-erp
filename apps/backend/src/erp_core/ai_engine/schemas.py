from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from erp_core.ai_engine.runtime import CapabilityExecutionResult
from erp_core.shared.enums import (
    AiCapability,
    AutonomyLevel,
    ChannelType,
    ProposalStatus,
    ProposalType,
)


class AiProposalRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    capability: AiCapability
    proposal_type: ProposalType
    conversation_id: UUID | None
    target_entity_type: str | None
    target_entity_id: UUID | None
    proposed_payload: dict[str, Any]
    citations: list[dict[str, Any]]
    source_tool_call_id: UUID | None = None
    confidence_score: Decimal
    estimated_margin_pct: Decimal | None
    status: ProposalStatus
    reviewed_by_user_id: UUID | None
    edited_payload: dict[str, Any] | None
    rejection_reason: str | None
    execution_result: dict[str, Any] | None
    expires_at: datetime | None
    created_at: datetime
    reviewed_at: datetime | None


class ProposalEditApproveRequest(BaseModel):
    edited_payload: dict[str, Any]
    feedback_note: str | None = Field(default=None, max_length=1000)


class ProposalRejectRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class ProposalFeedbackRequest(BaseModel):
    proposal_id: UUID
    feedback_note: str = Field(min_length=1, max_length=1000)


class ProposalFeedbackResponse(BaseModel):
    proposal_id: UUID
    feedback_id: UUID


class AssistantChatRequest(BaseModel):
    capability: AiCapability = AiCapability.CS_SUPPORT
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: UUID | None = None
    channel_type: ChannelType = ChannelType.WEB_LIVECHAT
    strict_rag: bool = False
    context: dict[str, Any] = Field(default_factory=dict)


class AssistantChatResponse(CapabilityExecutionResult):
    """Result of a single authenticated assistant turn."""


class CapabilityPolicyItem(BaseModel):
    capability: AiCapability
    display_name: str
    configured_autonomy: AutonomyLevel
    max_allowed_autonomy: AutonomyLevel
    pending_proposals_7d: int
    executed_actions_7d: int


class CapabilitiesOverviewResponse(BaseModel):
    capabilities: list[CapabilityPolicyItem]


class CapabilitiesUpdateRequest(BaseModel):
    settings: dict[AiCapability, AutonomyLevel]


class TranslateRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    target_lang: str = Field(pattern=r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{2,8})?$", max_length=17)


class TranslateResponse(BaseModel):
    translated_text: str
    target_lang: str
    model_used: str
    credits_consumed: Decimal
