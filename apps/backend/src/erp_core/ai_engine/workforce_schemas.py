from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from erp_core.shared.enums import AiCapability, AutonomyLevel, ProposalType

_PROPOSAL_TYPE_LABELS: dict[ProposalType, str] = {
    ProposalType.REPLY_DRAFT: "回复草稿",
    ProposalType.CRM_ENTITY_EXTRACT: "客户信息提取",
    ProposalType.QUOTE_DRAFT: "报价草稿",
    ProposalType.FOLLOW_UP_SUGGESTION: "跟进建议",
    ProposalType.IDENTITY_MERGE: "客户合并",
    ProposalType.REFUND_OR_RESHIP: "退款/补发",
    ProposalType.PAYMENT_MATCH: "收款匹配",
}
PROPOSAL_TYPE_LABELS = _PROPOSAL_TYPE_LABELS

PROPOSALS_HREF = "/ai/proposals"
CAPABILITIES_HREF = "/ai/capabilities"
PatrolSeverity = Literal["high", "medium", "info"]


class AiEmployeeCreate(BaseModel):
    """Operator-supplied digital employee definition."""

    code: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=120)
    role_title: str = Field(min_length=1, max_length=120)
    department: str = Field(min_length=1, max_length=120)
    avatar_emoji: str | None = Field(default=None, max_length=16)
    description: str = Field(min_length=1, max_length=2000)
    capability_codes: list[AiCapability] = Field(default_factory=list, max_length=6)
    autonomy_level: AutonomyLevel = AutonomyLevel.L1_PROPOSAL
    is_enabled: bool = True
    tool_count: int = Field(default=0, ge=0, le=200)
    system_persona: str | None = Field(default=None, max_length=4000)
    # Optional narrowing request. Tool grants follow the declared capability scope, so every
    # requested tool must already be covered by `capability_codes`; the effective grant set is
    # echoed back in `AiEmployeeRead.allowed_tools` and never silently widened or shrunk.
    allowed_tools: list[str] | None = Field(default=None, max_length=32)

    @field_validator("allowed_tools")
    @classmethod
    def _tools_not_blank(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned: list[str] = []
        for tool in value:
            name = tool.strip()
            if not name:
                raise ValueError("tool names must not be blank")
            if name not in cleaned:
                cleaned.append(name)
        return cleaned


class AiEmployeeUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    role_title: str | None = Field(default=None, min_length=1, max_length=120)
    department: str | None = Field(default=None, min_length=1, max_length=120)
    avatar_emoji: str | None = Field(default=None, max_length=16)
    description: str | None = Field(default=None, min_length=1, max_length=2000)
    capability_codes: list[AiCapability] | None = Field(default=None, max_length=6)
    autonomy_level: AutonomyLevel | None = None
    is_enabled: bool | None = None
    tool_count: int | None = Field(default=None, ge=0, le=200)
    system_persona: str | None = Field(default=None, max_length=4000)


class AiEmployeeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    code: str
    display_name: str
    role_title: str
    department: str
    avatar_emoji: str | None
    description: str
    capability_codes: list[AiCapability]
    autonomy_level: AutonomyLevel
    is_enabled: bool
    tool_count: int
    system_persona: str | None
    created_at: datetime
    updated_at: datetime
    # Real tool grant: the deterministic union of tools implied by `capability_codes`.
    allowed_tools: list[str] = Field(default_factory=list)
    # Real activity: ai_proposals created for this employee's capabilities in the last 7 days.
    actions_7d: int = 0
    # Real open work: ai_proposals still pending review for those capabilities.
    tasks_7d: int = 0


class AutonomySummary(BaseModel):
    l2_auto: int
    l1_proposal: int
    l0_copilot: int


class WorkforceKpis(BaseModel):
    total_employees: int
    active_employees: int
    actions_7d: int
    # Decimal money travels as a string; a float would silently lose cents.
    today_llm_cost_usd: str
    autonomy_summary: AutonomySummary


class AutoExecutedCapability(BaseModel):
    capability: AiCapability
    display_name: str
    auto_executed_7d: int
    href: str


class PendingProposalGroup(BaseModel):
    proposal_type: ProposalType
    label: str
    count: int
    href: str


class LockedGuardrail(BaseModel):
    code: str
    title: str
    description: str
    href: str


class WorkforceMatrixResponse(BaseModel):
    kpis: WorkforceKpis
    l2_auto: list[AutoExecutedCapability]
    l1_pending: list[PendingProposalGroup]
    l0_locked: list[LockedGuardrail]


class PatrolAlertItem(BaseModel):
    """One live, tenant-scoped patrol finding with the employee who owns the follow-up."""

    code: str
    severity: PatrolSeverity
    title_zh: str
    title_en: str
    detail_zh: str
    detail_en: str
    employee_code: str
    employee_name: str
    action_href: str
    count: int


class DailyPatrolBriefRead(BaseModel):
    generated_at: datetime
    health_score: int = Field(ge=0, le=100)
    summary_zh: str
    summary_en: str
    metrics: dict[str, int]
    alerts: list[PatrolAlertItem]


class PatrolFindingItem(BaseModel):
    """A single live SQL finding returned by a dispatcher run."""

    code: str
    title_zh: str
    title_en: str
    detail_zh: str
    detail_en: str
    count: int
    action_href: str
    samples: list[dict[str, Any]] = Field(default_factory=list)


class PatrolDispatchRead(BaseModel):
    employee_code: str
    employee_name: str
    employee_role_title: str
    generated_at: datetime
    action_summary_zh: str
    action_summary_en: str
    metrics: dict[str, int]
    findings: list[PatrolFindingItem]
    # A real `task_reminders` follow-up row when the dispatched action is actionable.
    created_task_id: UUID | None = None
    recommended_href: str


class InboxAssistAction(StrEnum):
    POLISH = "polish"
    SUMMARIZE = "summarize"
    SUGGEST_REPLY = "suggest_reply"
    DAILY_BRIEF = "daily_brief"


class InboxAssistRequest(BaseModel):
    action: InboxAssistAction
    conversation_id: UUID | None = None
    text: str | None = Field(default=None, max_length=8000)
    language: str = Field(default="zh", pattern="^(zh|en)$")
    strict_rag: bool = False


class InboxAssistResponse(BaseModel):
    action: InboxAssistAction
    text: str
    abstained: bool = False
    requires_human_review: bool = False
    numeric_guard_passed: bool = True
    model_used: str = ""
    credits_consumed: str = "0"
    citations: list[dict[str, Any]] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    data: dict[str, Any] | None = None


class QuickReplyCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    category: str = Field(min_length=1, max_length=60)
    language: str = Field(default="en", pattern="^(zh|en)$")
    body_text: str = Field(min_length=1, max_length=4000)
    shortcut_code: str = Field(min_length=1, max_length=60)


class QuickReplyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    title: str
    category: str
    language: str
    body_text: str
    shortcut_code: str
    created_at: datetime


class CreativeAssetType(StrEnum):
    PRODUCT_COPY = "product_copy"
    LISTING = "listing"
    EMAIL_TEMPLATE = "email_template"
    SOCIAL_CAPTION = "social_caption"
    AD_HOOK = "ad_hook"


class CreativeAssetStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    ARCHIVED = "archived"


class CreativeAssetCreate(BaseModel):
    asset_type: CreativeAssetType
    title: str | None = Field(default=None, max_length=200)
    language: str = Field(default="en", min_length=2, max_length=12)
    sku_ref: str | None = Field(default=None, max_length=120)
    prompt: str | None = Field(default=None, max_length=4000)
    content_text: str | None = Field(default=None, max_length=20000)
    # Optional AI generation; requires a configured provider (503 otherwise).
    generate: bool = False


class CreativeAssetUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    language: str | None = Field(default=None, min_length=2, max_length=12)
    content_text: str | None = Field(default=None, min_length=1, max_length=20000)
    status: CreativeAssetStatus | None = None


class CreativeAssetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    asset_type: CreativeAssetType
    title: str
    language: str
    sku_ref: str | None
    prompt: str | None
    content_text: str
    status: CreativeAssetStatus
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    numeric_guard_passed: bool = True
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
