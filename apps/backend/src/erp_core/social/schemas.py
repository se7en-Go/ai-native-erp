"""Request/response contracts for the `/api/v1/social` marketing center."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from erp_core.social.playbooks import PublishMode, SocialPlatform, SocialRegion

AccountStatus = Literal["connected", "disconnected", "token_expired"]
PostStatus = Literal["draft", "scheduled", "published", "failed"]
# How a post actually reached the outside world. The database status column stays the
# four-value lifecycle token; this literal tells the truth about delivery.
DeliveryMode = Literal["recorded_local", "manual_confirmed", "external_api"]
Sentiment = Literal["positive", "neutral", "negative", "lead_intent"]


class PlatformRead(BaseModel):
    """Playbook metadata plus the honest publishing capability for one platform."""

    platform: SocialPlatform
    region: SocialRegion
    label: str
    emoji: str
    audience: str
    acquisition_plays: list[str]
    best_practices: list[str]
    content_formats: list[str]
    publish_mode: PublishMode
    supports_scheduled_publish: bool
    compliance_note: str


class SocialPlaybookItem(BaseModel):
    """Bilingual operating card for one platform: audience, funnel, practices, compliance."""

    platform: SocialPlatform
    region: SocialRegion
    name_zh: str
    name_en: str
    category_zh: str
    category_en: str
    audience_zh: str
    audience_en: str
    lead_funnel_zh: str
    lead_funnel_en: str
    best_practices_zh: list[str]
    best_practices_en: list[str]
    # Derived from the locked publish_mode: no honest route exists without an official API.
    official_api_supported: bool
    compliance_note_zh: str
    compliance_note_en: str


class SocialSummary(BaseModel):
    total_posts: int
    published_posts: int
    scheduled_posts: int
    draft_posts: int
    social_leads: int
    converted_leads: int
    mentions_total: int
    connected_accounts: int
    supported_platforms: int


class AccountRead(BaseModel):
    """Never carries the token: the response exposes only whether one is stored."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    platform: SocialPlatform
    region: SocialRegion
    handle: str
    display_name: str
    external_account_id: str | None
    status: AccountStatus
    followers_count: int
    config: dict[str, Any]
    has_stored_token: bool
    created_at: datetime
    updated_at: datetime


class AccountConnect(BaseModel):
    platform: SocialPlatform
    handle: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)
    external_account_id: str | None = Field(default=None, max_length=200)
    followers_count: int = Field(default=0, ge=0)
    config: dict[str, Any] = Field(default_factory=dict)
    # Secrets are write-only: encrypted with AAD f"{tenant_id}:{account_id}:social_oauth".
    access_token: str | None = Field(default=None, min_length=1, max_length=8192)


class PostCreate(BaseModel):
    platform: SocialPlatform
    social_account_id: UUID | None = None
    title: str | None = Field(default=None, max_length=200)
    caption: str = Field(min_length=1, max_length=20000)
    hashtags: list[str] = Field(default_factory=list, max_length=30)
    media_urls: list[str] = Field(default_factory=list, max_length=20)
    sku_ref: str | None = Field(default=None, max_length=120)
    status: Literal["draft", "scheduled"] = "draft"
    scheduled_at: datetime | None = None

    @field_validator("caption")
    @classmethod
    def _caption_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("caption must not be blank")
        return value

    @field_validator("hashtags")
    @classmethod
    def _normalize_hashtags(cls, value: list[str]) -> list[str]:
        seen: list[str] = []
        for tag in value:
            cleaned = tag.strip()
            if not cleaned:
                continue
            if not cleaned.startswith("#"):
                cleaned = f"#{cleaned}"
            if cleaned.casefold() not in {item.casefold() for item in seen}:
                seen.append(cleaned)
        return seen


class PostUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    caption: str | None = Field(default=None, min_length=1, max_length=20000)
    hashtags: list[str] | None = Field(default=None, max_length=30)
    media_urls: list[str] | None = Field(default=None, max_length=20)
    sku_ref: str | None = Field(default=None, max_length=120)
    # Any lifecycle token is accepted here so the service can answer with a precise
    # domain error (ERR_INVALID_POST_STATUS) instead of a generic validation 422.
    status: PostStatus | None = None
    scheduled_at: datetime | None = None


class PostRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    social_account_id: UUID | None
    platform: SocialPlatform
    title: str | None
    caption: str
    hashtags: list[str]
    media_urls: list[str]
    sku_ref: str | None
    status: PostStatus
    scheduled_at: datetime | None
    published_at: datetime | None
    external_post_id: str | None
    metrics: dict[str, Any]
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    # Honest delivery state: a `published` row without an external post id was only
    # recorded locally (or manually confirmed in the platform's own console).
    delivery_mode: DeliveryMode = "recorded_local"
    external_publish_confirmed: bool = False


class PostPublish(BaseModel):
    """How a publish was confirmed, and whether the platform API should be called.

    `manual_confirmation` is the only honest path for `manual_export` platforms: a human
    posts in the official creation console and records the result here. `external_post_id`
    records a post id produced outside this workspace (also used for API platforms whose
    console was used instead of the API).

    `execute_remote` opts into a *real* outbound publish through the platform's official
    API: the stored OAuth token is decrypted and used to call the vendor endpoint, and the
    returned receipt becomes the post's `external_post_id`. It defaults to `False` so the
    historical record-only behaviour is never changed by an older caller.
    """

    manual_confirmation: bool = False
    external_post_id: str | None = Field(default=None, max_length=200)
    execute_remote: bool = False


class CaptionGenerate(BaseModel):
    platform: SocialPlatform
    prompt: str = Field(min_length=1, max_length=4000)
    sku_ref: str | None = Field(default=None, max_length=120)
    language: Literal["zh", "en"] = "zh"
    tone: str | None = Field(default=None, max_length=60)

    @field_validator("prompt")
    @classmethod
    def _prompt_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prompt must not be blank")
        return value


class CaptionRead(BaseModel):
    platform: SocialPlatform
    text: str
    language: str
    sku_ref: str | None
    model_used: str
    credits_consumed: str
    grounded_tool_call_id: str | None


class MentionCreate(BaseModel):
    platform: SocialPlatform
    social_account_id: UUID | None = None
    author_handle: str = Field(min_length=1, max_length=200)
    content_text: str = Field(min_length=1, max_length=10000)
    sentiment: Sentiment = "neutral"
    occurred_at: datetime | None = None

    @field_validator("content_text", "author_handle")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value


class MentionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    social_account_id: UUID | None
    platform: SocialPlatform
    author_handle: str
    content_text: str
    sentiment: Sentiment
    converted_lead_id: UUID | None
    occurred_at: datetime
    created_at: datetime


class ConvertLeadResult(BaseModel):
    mention_id: UUID
    lead_id: UUID
    customer_id: UUID
    already_converted: bool
