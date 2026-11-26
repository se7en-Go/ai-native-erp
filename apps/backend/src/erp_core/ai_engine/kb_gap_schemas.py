from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

RedlineMatchType = Literal["keyword", "regex"]
RedlineAction = Literal["block", "warn"]
KbGapStatus = Literal["open", "resolved", "dismissed"]


class RedlineRuleCreate(BaseModel):
    """Rule authoring payload.

    `pattern` carries only a transport cap here; the service enforces the real
    200-character policy limit and returns `ERR_INVALID_REDLINE_PATTERN` /
    `ERR_REDLINE_PATTERN_TOO_LONG` so the operator sees why a rule was refused.
    """

    name: str = Field(min_length=1, max_length=120)
    pattern: str = Field(min_length=1, max_length=2000)
    match_type: RedlineMatchType = "keyword"
    action: RedlineAction = "block"
    is_enabled: bool = True


class RedlineRuleRead(BaseModel):
    id: UUID
    tenant_id: UUID
    name: str
    pattern: str
    match_type: RedlineMatchType
    action: RedlineAction
    is_enabled: bool
    created_at: datetime


class RedlineMatchedRule(BaseModel):
    id: UUID
    name: str
    match_type: RedlineMatchType
    action: RedlineAction
    matched_text: str


class RedlineCheckRequest(BaseModel):
    content: str = Field(min_length=1, max_length=8000)


class RedlineCheckResult(BaseModel):
    blocked: bool
    blocks: list[str]
    warnings: list[str]
    matched_rules: list[RedlineMatchedRule]


class KbGapCreateRequest(BaseModel):
    customer_question: str = Field(min_length=3, max_length=2000)
    conversation_id: UUID | None = None
    detected_language: str = Field(default="en", min_length=2, max_length=16)


class KbGapRead(BaseModel):
    id: UUID
    tenant_id: UUID
    conversation_id: UUID | None = None
    customer_question: str
    detected_language: str
    frequency_count: int
    status: KbGapStatus
    resolved_answer: str | None = None
    promoted_kb_document_id: UUID | None = None
    created_at: datetime
    updated_at: datetime


class KbGapPromoteRequest(BaseModel):
    resolved_answer: str = Field(min_length=3, max_length=8000)


class KbGapPromoteResult(BaseModel):
    gap: KbGapRead
    document_id: UUID
    chunks_created: int


class AdoptionTypeMetrics(BaseModel):
    proposal_type: str
    total: int
    pending: int
    approved: int
    edited_and_approved: int
    rejected: int
    adoption_rate_pct: Decimal


class AiAdoptionMetrics(BaseModel):
    tenant_id: UUID
    window_days: int
    total: int
    pending: int
    approved: int
    edited_and_approved: int
    rejected: int
    expired: int
    failed: int
    adopted: int
    review_base: int
    adoption_rate_pct: Decimal
    direct_adopt_rate_pct: Decimal
    edit_adopt_rate_pct: Decimal
    rejection_rate_pct: Decimal
    by_type: list[AdoptionTypeMetrics]
