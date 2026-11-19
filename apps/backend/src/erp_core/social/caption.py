"""Injectable caption-generation contract owned by the social module.

`erp_core.social` must never import `erp_core.ai_engine` (import-linter contract
`social_isolated`), so the AI work is expressed as a protocol here and the concrete
implementation lives in `erp_core.ai_engine.social_caption`, wired only at the
composition root (`api_main`). Tests inject a stub through `app.state`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class CaptionRequest:
    platform: str
    prompt: str
    sku_ref: str | None = None
    language: str = "zh"
    tone: str | None = None


@dataclass(frozen=True)
class CaptionResult:
    text: str
    model_used: str
    credits_consumed: Decimal
    # Set only when a deterministic ERP tool produced the facts quoted in the copy.
    grounded_tool_call_id: str | None = None


class CaptionProvider(Protocol):
    """Generate platform copy; raise `RuntimeError` when no provider is configured."""

    async def generate_caption(
        self, session: AsyncSession, tenant_id: UUID, request: CaptionRequest
    ) -> CaptionResult: ...
