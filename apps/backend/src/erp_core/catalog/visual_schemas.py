"""Request/response contracts for the sourcing visual-AI job router."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

VisualProviderName = Literal["sandbox", "remove_bg", "openai_image"]
VisualOperationName = Literal["remove_bg", "scene_replace", "translate_overlay"]
VisualJobStatus = Literal["queued", "processing", "completed", "failed"]

#: Longest accepted `source_image_url` (a `data:` preview URL is intentionally allowed).
MAX_SOURCE_IMAGE_URL_CHARS = 2_000_000


class VisualJobCreate(BaseModel):
    """Create one visual job. `source_image_url` accepts https, `data:image/*` or `/media/*`."""

    provider: VisualProviderName = "sandbox"
    operation: VisualOperationName
    source_image_url: str = Field(min_length=1, max_length=MAX_SOURCE_IMAGE_URL_CHARS)
    variant_id: UUID | None = None
    target_locale: str | None = Field(default=None, min_length=2, max_length=16)
    prompt: str | None = Field(default=None, max_length=2000)

    @field_validator("source_image_url")
    @classmethod
    def _strip_source(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("source_image_url must not be blank")
        return stripped


class VisualJobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    variant_id: UUID | None = None
    provider: VisualProviderName
    operation: VisualOperationName
    source_image_url: str
    target_locale: str | None = None
    prompt: str | None = None
    status: VisualJobStatus
    output_image_url: str | None = None
    output_mime_type: str | None = None
    output_r2_key: str | None = None
    error_message: str | None = None
    created_by: UUID | None = None
    created_at: datetime
    completed_at: datetime | None = None
