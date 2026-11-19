"""AI caption generation for the social marketing center.

This adapter lives in `erp_core.ai_engine` on purpose: `erp_core.social` is forbidden
from importing the AI engine (import-linter `social_isolated`), so the social module
owns a `CaptionProvider` protocol and the composition root injects this implementation.
Output is grounded: any figure quoted in the copy must come from a deterministic
`ErpToolRegistry` tool call, and the tool call id travels back with the text.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.ai_engine.router_llm import (
    MODEL_CATALOG,
    TieredModelRouter,
    calculate_cost_and_credits,
)
from erp_core.ai_engine.tools import ErpToolRegistry
from erp_core.billing.wallet_service import CreditWalletService
from erp_core.shared.enums import AiCapability, Incoterm, UsageMetricType
from erp_core.shared.errors import AppError
from erp_core.social.caption import CaptionRequest, CaptionResult
from erp_core.social.service import caption_provider_unavailable

_CREDITS_QUANTUM = Decimal("0.0001")
_SKU_PATTERN = re.compile(r"\b[A-Z0-9]{2,}(?:-[A-Z0-9]{2,}){1,3}\b")

_CAPTION_SYSTEM = (
    "You are the copywriter of an omnichannel hybrid ERP tenant. Write one ready-to-post "
    "social caption for the requested platform and language. Never invent prices, MOQ, "
    "stock or delivery dates: quote only the verified ERP facts given in the context block. "
    "Return the caption text only, with no preamble and no hashtag spam."
)


class AiEngineCaptionProvider:
    """`erp_core.social.caption.CaptionProvider` backed by the tiered LLM router."""

    def __init__(
        self,
        *,
        llm_router: TieredModelRouter,
        wallet_service: CreditWalletService | None = None,
        tool_registry: ErpToolRegistry | None = None,
    ) -> None:
        self._llm_router = llm_router
        self._wallet = wallet_service or CreditWalletService()
        self._tools = tool_registry or ErpToolRegistry()

    async def generate_caption(
        self, session: AsyncSession, tenant_id: UUID, request: CaptionRequest
    ) -> CaptionResult:
        context_lines: list[str] = []
        tool_call_id: str | None = None
        sku = (request.sku_ref or "").strip()
        if not sku:
            found = _SKU_PATTERN.search(request.prompt)
            sku = found.group() if found is not None else ""
        if sku:
            live = await self._tools.get_live_sku_pricing_and_stock(
                session, tenant_id, sku, qty=1, incoterm=Incoterm.FOB
            )
            if live.ok:
                tool_call_id = live.tool_call_id
                context_lines.append(
                    "Verified ERP facts (quote them exactly, do not reformat): "
                    f"sku={live.data['sku']}, available_qty={live.data['available_qty']}, "
                    f"moq={live.data['applied_moq_tier']}, "
                    f"unit_price_usd={live.data['applied_unit_price_usd']}"
                )

        language = "Chinese" if request.language == "zh" else "English"
        instruction = (
            f"Platform: {request.platform}. Language: {language}. "
            f"Tone: {request.tone or 'professional, concrete, no hype'}."
        )
        generation = await self._generate(
            session,
            tenant_id,
            query=f"{instruction}\n\nBrief:\n{request.prompt}",
            context="\n".join(context_lines),
        )
        text = generation.text.strip()
        if not text:
            # An empty completion is a provider failure, never a fake caption.
            raise caption_provider_unavailable()
        return CaptionResult(
            text=text,
            model_used=generation.model_id,
            credits_consumed=generation.credits_consumed.quantize(
                _CREDITS_QUANTUM, rounding=ROUND_HALF_UP
            ),
            grounded_tool_call_id=tool_call_id,
        )

    async def _generate(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        *,
        query: str,
        context: str,
        max_tokens: int = 500,
    ) -> Any:
        await self._wallet.ensure_wallet(session, tenant_id=tenant_id)
        tier = self._llm_router.select_tier(
            capability=AiCapability.CONTENT_STUDIO, intent="social_caption", text_length=len(query)
        )
        prompt_upper_bound = len((_CAPTION_SYSTEM + query + context).encode()) + 128
        estimate = max(
            calculate_cost_and_credits(model_id, prompt_upper_bound, 0, max_tokens)[1]
            for model_id, spec in MODEL_CATALOG.items()
            if spec.tier == tier
        )
        if not await self._wallet.lock_budget(session, tenant_id, max(Decimal(1), estimate)):
            raise AppError(
                slug="ai-credits-depleted",
                title="AI Credits Depleted",
                status_code=402,
                detail="Top up the credit wallet to resume AI copywriting.",
                code="ERR_CREDITS_DEPLETED",
            )
        try:
            generation = await self._llm_router.generate(
                tenant_id=tenant_id,
                capability=AiCapability.CONTENT_STUDIO,
                system_prefix=_CAPTION_SYSTEM,
                context_block=context,
                user_query=query,
                intent="social_caption",
                cacheable_faq=False,
                max_tokens=max_tokens,
            )
        except RuntimeError:
            # Missing/unreachable provider: fail closed with an explicit 503.
            raise caption_provider_unavailable() from None
        if not generation.cache_hit:
            allowed, _ = await self._wallet.try_reserve_and_record_spend(
                session,
                tenant_id,
                f"ai:{uuid4()}",
                UsageMetricType.AI_CREDIT,
                generation.credits_consumed,
                generation.cost_usd,
                generation.model_id,
            )
            if not allowed:
                raise AppError(
                    slug="ai-credits-depleted",
                    title="AI Credits Depleted",
                    status_code=402,
                    detail="Top up the credit wallet to resume AI copywriting.",
                    code="ERR_CREDITS_DEPLETED",
                )
        return generation
