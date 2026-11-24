"""LLM-backed sourcing copy rewriting for marketplace listings.

This adapter lives in ``erp_core.ai_engine`` on purpose: ``erp_core.catalog`` is
forbidden from importing the AI engine (``infra/importlinter.ini`` contracts
``catalog_isolated`` and ``domain_modules_never_import_ai_engine``), so catalog owns the
``SourcingCopyRewriter`` protocol plus the deterministic copywriter and the composition
root injects this implementation, exactly like ``AiEngineCaptionProvider``.

Honesty rules enforced here:

* The router call is budget-locked before it happens, and credits are only *recorded*
  after a genuinely valid LLM JSON payload came back.
* If no BYOK/platform provider key is configured (``RuntimeError``), or the completion is
  empty/unparseable/contradicts the payload's money, the caller gets the deterministic
  localized copy with ``rewrite_mode="deterministic_fallback"`` and
  ``credits_consumed=Decimal("0.0000")`` — never a fake "llm" result and never a charge.
* Every USD figure quoted by the model must equal the draft's ``target_price_usd`` or one
  of its MOQ tier unit prices, byte-for-value; invented prices are rejected.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.ai_engine.router_llm import (
    MODEL_CATALOG,
    LlmGenerationResult,
    TieredModelRouter,
    calculate_cost_and_credits,
)
from erp_core.billing.wallet_service import CreditWalletService
from erp_core.catalog.sourcing_copy_protocol import (
    PLATFORM_COPY_GUIDELINES,
    SUPPORTED_REWRITE_LANGUAGES,
    SourcingCopyRewriteInput,
    SourcingCopyRewriteOutput,
    build_deterministic_localized_copy,
)
from erp_core.shared.enums import AiCapability, UsageMetricType
from erp_core.shared.errors import AppError

_CREDITS_QUANTUM = Decimal("0.0001")
_MAX_TOKENS = 900
_MAX_TITLE_CHARS = 200
_MAX_DESCRIPTION_CHARS = 4000
_MAX_KEYWORDS = 12
_EXPECTED_BULLETS = 5

_SOURCING_COPY_SYSTEM = (
    "You are the marketplace merchandising copywriter of an omnichannel hybrid ERP tenant. "
    "Rewrite one supplier product draft into a ready-to-publish localized listing that obeys "
    "the target platform's merchandising rules. "
    "Reply with ONE JSON object and nothing else: no markdown, no code fences, no preamble, "
    "no trailing commentary. The object must use exactly these keys: "
    '{"title": string, "bullet_points": [exactly 5 strings], "description": string, '
    '"seo_keywords": [strings]}. '
    "Write title, bullet_points, description and seo_keywords in the requested target "
    "language only. "
    "Money and quantity fidelity is absolute: the context block lists the only permitted "
    "target_price_usd and MOQ tier unit prices. Quote them verbatim or omit them. Never "
    "invent, round, discount, convert or recompute a price, never invent a MOQ quantity, "
    "certification, warranty length, stock level, lead time or delivery date."
)

_TONE_INSTRUCTIONS: dict[str, str] = {
    "conversion": "Tone: conversion-led, benefit first, concrete and free of hype.",
    "seo": "Tone: search-optimised, front-load the buyer's search terms naturally.",
    "b2b_wholesale": "Tone: B2B wholesale quotation, precise and procurement-friendly.",
    "social_viral": "Tone: social-first, short punchy lines with a clear call to action.",
}

_FENCE_RE = re.compile(r"```(?:json)?\s*(?P<body>.*?)\s*```", re.DOTALL | re.IGNORECASE)
_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_WHITESPACE_RE = re.compile(r"\s+")
_PREFIX_PRICE_RE = re.compile(
    r"(?:US\$|\$|USD)\s?([0-9][0-9,]*(?:\.[0-9]+)?)", re.IGNORECASE
)
_SUFFIX_PRICE_RE = re.compile(
    r"(?<![\w.])([0-9][0-9,]*(?:\.[0-9]+)?)\s?(?:USD|US\$|\$)", re.IGNORECASE
)


def _credits_depleted() -> AppError:
    return AppError(
        slug="ai-credits-depleted",
        title="AI Credits Depleted",
        status_code=402,
        detail="Top up the credit wallet to resume AI copy rewriting.",
        code="ERR_CREDITS_DEPLETED",
    )


def _collapse(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return _WHITESPACE_RE.sub(" ", value).strip()


def _language_display(code: str) -> str:
    normalized = (code or "en").strip().lower()
    return SUPPORTED_REWRITE_LANGUAGES.get(normalized, normalized)


def _money_text(value: Any) -> str:
    if isinstance(value, Decimal) and value.is_finite():
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    return str(value)


def _as_price(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, (int, str)):
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        return parsed if parsed.is_finite() else None
    return None


def _price_values(payload: SourcingCopyRewriteInput) -> tuple[Decimal, ...]:
    """Every USD amount the model is allowed to quote, straight from the priced draft."""
    allowed: list[Decimal] = []
    target_price = _as_price(payload.target_price_usd)
    if target_price is not None:
        allowed.append(target_price)
    for tier in payload.moq_tiers:
        if not isinstance(tier, dict):
            continue
        for key in ("unit_price_usd", "unit_price", "price_usd"):
            if key not in tier:
                continue
            unit_price = _as_price(tier[key])
            if unit_price is not None:
                allowed.append(unit_price)
            break
    return tuple(allowed)


def _ladder_text(payload: SourcingCopyRewriteInput) -> str:
    entries: list[str] = []
    for tier in payload.moq_tiers:
        if not isinstance(tier, dict):
            continue
        quantity = tier.get("min_qty", tier.get("quantity", tier.get("qty")))
        price = tier.get("unit_price_usd", tier.get("unit_price"))
        if quantity is None or price is None:
            continue
        entries.append(f"{quantity} units at ${_money_text(price)}")
    return " / ".join(entries) if entries else "no MOQ ladder supplied"


def _build_context_block(payload: SourcingCopyRewriteInput) -> str:
    """Authoritative draft facts. The prompt declares this block as the only money source."""
    lines = [
        f"target_channel={payload.target_channel}",
        f"target_language={_language_display(payload.target_language)} "
        f"({payload.target_language})",
        f"tone={payload.tone}",
        f"category={payload.category}",
        f"hs_code={payload.hs_code or 'unspecified'}",
        f"target_price_usd=${_money_text(payload.target_price_usd)}",
        f"moq_ladder={_ladder_text(payload)}",
        f"keywords={', '.join(payload.keywords) if payload.keywords else 'none'}",
        f"source_title={payload.raw_title}",
    ]
    description = _collapse(payload.source_description)
    if description:
        lines.append(f"source_description={description[:1500]}")
    bullets = [_collapse(item) for item in payload.existing_bullets if _collapse(item)]
    if bullets:
        lines.append(f"supplier_selling_points={' | '.join(bullets[:5])}")
    return "\n".join(lines)


def _build_user_query(payload: SourcingCopyRewriteInput) -> str:
    channel = (payload.target_channel or "").strip().lower()
    guideline = PLATFORM_COPY_GUIDELINES.get(
        channel, "Use a clear marketplace structure: title, five bullets, description."
    )
    tone = _TONE_INSTRUCTIONS.get(payload.tone, _TONE_INSTRUCTIONS["conversion"])
    keyword_line = (
        f"Include these merchandising keywords where they read naturally: "
        f"{', '.join(payload.keywords)}."
        if payload.keywords
        else "No extra merchandising keywords were supplied."
    )
    return (
        f"Target platform: {channel or 'marketplace'}. "
        f"Target language: {_language_display(payload.target_language)}. "
        f"{tone} {keyword_line}\n"
        f"Platform merchandising rules: {guideline}\n"
        "Return exactly five bullet_points and reproduce every USD figure and MOQ quantity "
        "from the context block verbatim."
    )


def _extract_json_object(text: str) -> dict[str, Any] | None:
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped:
        return None
    fence = _FENCE_RE.search(stripped)
    candidates = [fence.group("body")] if fence is not None else []
    candidates.append(stripped)
    braced = _OBJECT_RE.search(stripped)
    if braced is not None:
        candidates.append(braced.group(0))
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _quoted_prices(text: str) -> list[Decimal]:
    values: list[Decimal] = []
    for pattern in (_PREFIX_PRICE_RE, _SUFFIX_PRICE_RE):
        for match in pattern.finditer(text):
            raw = match.group(1).replace(",", "")
            try:
                parsed = Decimal(raw)
            except (InvalidOperation, ValueError):
                continue
            if parsed.is_finite():
                values.append(parsed)
    return values


def _numeric_fidelity_ok(text: str, payload: SourcingCopyRewriteInput) -> bool:
    allowed = _price_values(payload)
    return all(any(quoted == permitted for permitted in allowed) for quoted in _quoted_prices(text))


@dataclass(frozen=True, slots=True)
class _ValidatedCopy:
    title: str
    bullet_points: list[str]
    description: str
    seo_keywords: list[str]


def _clean_bullets(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    bullets: list[str] = []
    for item in raw:
        cleaned = _collapse(item)
        if not cleaned or cleaned in bullets:
            continue
        bullets.append(cleaned)
        if len(bullets) == _EXPECTED_BULLETS:
            break
    return bullets


def _clean_keywords(raw: Any, payload: SourcingCopyRewriteInput) -> list[str]:
    keywords: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            cleaned = _collapse(item)
            if not cleaned or cleaned in keywords:
                continue
            keywords.append(cleaned)
            if len(keywords) == _MAX_KEYWORDS:
                break
    if keywords:
        return keywords
    for candidate in [payload.category, *payload.keywords]:
        cleaned = _collapse(candidate)
        if cleaned and cleaned not in keywords:
            keywords.append(cleaned)
    return keywords[:_MAX_KEYWORDS]


def _validate_llm_output(
    text: str, payload: SourcingCopyRewriteInput
) -> _ValidatedCopy | None:
    """Return the accepted copy, or ``None`` when the completion must not be trusted."""
    data = _extract_json_object(text)
    if data is None:
        return None
    title = _collapse(data.get("title"))[:_MAX_TITLE_CHARS]
    if not title:
        return None
    description = _collapse(data.get("description"))[:_MAX_DESCRIPTION_CHARS]
    if not description:
        return None
    bullets = _clean_bullets(data.get("bullet_points"))
    if not bullets:
        return None
    if not _numeric_fidelity_ok(" ".join([title, *bullets, description]), payload):
        return None
    if len(bullets) < _EXPECTED_BULLETS:
        # Pad from the deterministic writer, which only ever re-uses payload numbers.
        for candidate in build_deterministic_localized_copy(payload).bullet_points:
            if candidate not in bullets:
                bullets.append(candidate)
            if len(bullets) == _EXPECTED_BULLETS:
                break
    return _ValidatedCopy(
        title=title,
        bullet_points=bullets,
        description=description,
        seo_keywords=_clean_keywords(data.get("seo_keywords"), payload),
    )


class AiEngineSourcingCopyRewriter:
    """``erp_core.catalog.sourcing_copy_protocol.SourcingCopyRewriter`` backed by the router."""

    def __init__(
        self,
        llm_router: TieredModelRouter,
        wallet_service: CreditWalletService | None = None,
    ) -> None:
        self._llm_router = llm_router
        self._wallet = wallet_service or CreditWalletService()

    async def rewrite_copy(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        payload: SourcingCopyRewriteInput,
    ) -> SourcingCopyRewriteOutput:
        await self._wallet.ensure_wallet(session, tenant_id=tenant_id)
        query = _build_user_query(payload)
        context = _build_context_block(payload)
        tier = self._llm_router.select_tier(
            capability=AiCapability.CONTENT_STUDIO,
            intent="sourcing_copy",
            text_length=len(payload.raw_title),
        )
        prompt_upper_bound = len((_SOURCING_COPY_SYSTEM + query + context).encode()) + 128
        estimate = max(
            (
                calculate_cost_and_credits(model_id, prompt_upper_bound, 0, _MAX_TOKENS)[1]
                for model_id, spec in MODEL_CATALOG.items()
                if spec.tier == tier
            ),
            default=Decimal(1),
        )
        if not await self._wallet.lock_budget(session, tenant_id, max(Decimal(1), estimate)):
            raise _credits_depleted()

        try:
            generation = await self._generate(tenant_id, query, context)
        except RuntimeError:
            # No BYOK/platform key, or the provider is unreachable: honest local copy, no charge.
            return build_deterministic_localized_copy(
                payload, reason="llm_provider_unavailable"
            )

        validated = _validate_llm_output(generation.text, payload)
        if validated is None:
            return build_deterministic_localized_copy(payload, reason="llm_invalid_output")

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
                raise _credits_depleted()

        return SourcingCopyRewriteOutput(
            target_channel=payload.target_channel,
            target_language=payload.target_language,
            tone=payload.tone,
            title=validated.title,
            bullet_points=validated.bullet_points,
            description=validated.description,
            seo_keywords=validated.seo_keywords,
            rewrite_mode="llm",
            model_used=generation.model_id,
            credits_consumed=generation.credits_consumed.quantize(
                _CREDITS_QUANTUM, rounding=ROUND_HALF_UP
            ),
            fallback_reason=None,
            generated_at=datetime.now(UTC),
        )

    async def _generate(
        self, tenant_id: UUID, query: str, context: str
    ) -> LlmGenerationResult:
        return await self._llm_router.generate(
            tenant_id=tenant_id,
            capability=AiCapability.CONTENT_STUDIO,
            system_prefix=_SOURCING_COPY_SYSTEM,
            context_block=context,
            user_query=query,
            intent="sourcing_copy",
            cacheable_faq=False,
            max_tokens=_MAX_TOKENS,
        )
