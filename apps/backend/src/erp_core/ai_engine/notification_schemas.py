"""Request/response contracts for notification channels, delivery ledger and automation.

The read models never carry the stored signing secret or the full webhook URL: external
bot URLs embed an access token in their path, so only a masked preview plus a
`has_signing_secret` flag leaves the server.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

NotificationProvider = Literal["feishu", "dingtalk", "wecom", "generic_webhook"]
NotificationPriority = Literal["P0", "P1", "P2"]
DeliveryStatus = Literal["pending", "sent", "failed", "retrying"]
AutomationTemplateCode = Literal[
    "low_doc_replenishment",
    "negative_margin_hold",
    "stalled_shipment_soothe",
    "cold_b2b_quote_followup",
    "daily_patrol_brief",
]
AutomationAutonomyLevel = Literal["L0_copilot", "L1_proposal", "L2_auto"]


class NotificationChannelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    provider: NotificationProvider
    webhook_url: str = Field(min_length=1, max_length=2048)
    # Optional Feishu/DingTalk signing secret (or generic webhook HMAC key). Encrypted at rest.
    secret: str | None = Field(default=None, max_length=512)
    min_priority: NotificationPriority = "P1"
    is_enabled: bool = True


class NotificationChannelUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    webhook_url: str | None = Field(default=None, min_length=1, max_length=2048)
    secret: str | None = Field(default=None, max_length=512)
    min_priority: NotificationPriority | None = None
    is_enabled: bool | None = None


class NotificationChannelRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    provider: NotificationProvider
    webhook_url_preview: str
    has_signing_secret: bool
    min_priority: NotificationPriority
    is_enabled: bool
    last_tested_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime


class NotificationDeliveryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    channel_id: UUID
    channel_name: str
    provider: NotificationProvider
    priority: NotificationPriority
    event_type: str
    title: str
    body: str
    deep_link: str | None = None
    status: DeliveryStatus
    attempts: int
    next_retry_at: datetime | None = None
    http_status: int | None = None
    error_message: str | None = None
    created_at: datetime
    sent_at: datetime | None = None


class NotificationTestResult(BaseModel):
    channel_id: UUID
    ok: bool
    http_status: int | None = None
    error_message: str | None = None
    tested_at: datetime


class DeliveryRetrySweepResult(BaseModel):
    attempted: int
    sent: int
    retrying: int
    failed: int


class AutomationRuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    template_code: AutomationTemplateCode
    name: str
    is_enabled: bool
    autonomy_level: AutomationAutonomyLevel
    priority: NotificationPriority
    config_json: dict[str, Any]
    last_run_at: datetime | None = None
    last_triggered_count: int
    created_at: datetime
    updated_at: datetime


class AutomationRuleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    is_enabled: bool | None = None
    autonomy_level: AutomationAutonomyLevel | None = None
    priority: NotificationPriority | None = None
    config_json: dict[str, Any] | None = None


class AutomationRuleEvaluationItem(BaseModel):
    """Per-rule outcome. Counts are real; a dry run creates nothing in the database."""

    template_code: AutomationTemplateCode
    name: str
    is_enabled: bool
    priority: NotificationPriority
    autonomy_level: AutomationAutonomyLevel
    matched_count: int
    proposals_created: int
    notifications_queued: int
    items_preview: list[dict[str, Any]] = Field(default_factory=list)
    note: str | None = None


class AutomationEvaluationReport(BaseModel):
    generated_at: datetime
    dry_run: bool
    seeded_defaults: bool
    total_matched: int
    total_proposals_created: int
    total_notifications_queued: int
    rules: list[AutomationRuleEvaluationItem]


class AutomationEvaluationRequest(BaseModel):
    dry_run: bool = False


class AutomationSeedResult(BaseModel):
    created: int
    existing: int
    rules: list[AutomationRuleRead]
