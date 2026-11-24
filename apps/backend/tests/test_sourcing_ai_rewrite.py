"""Sourcing AI rewrite: protocol boundaries, deterministic localization, honest billing.

The LLM is always a deterministic in-process stub: nothing here touches the network, and
the wallet is a recorder so credit accounting is asserted exactly (``Decimal``, no floats).
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.ai_engine.router_llm import (
    LlmProviderResponse,
    TieredModelRouter,
    calculate_cost_and_credits,
)
from erp_core.ai_engine.sourcing_copy import AiEngineSourcingCopyRewriter
from erp_core.billing.wallet_service import CreditWalletService
from erp_core.catalog.sourcing_copy_protocol import (
    CHANNEL_DISPLAY_NAMES,
    DEFAULT_LANGUAGE_BY_CHANNEL,
    PLATFORM_COPY_GUIDELINES,
    SUPPORTED_REWRITE_LANGUAGES,
    SourcingCopyRewriteInput,
    build_deterministic_localized_copy,
)
from erp_core.shared.enums import AutonomyLevel, UsageMetricType
from erp_core.shared.errors import AppError

PROTOCOL_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "erp_core"
    / "catalog"
    / "sourcing_copy_protocol.py"
)

PROMPT_TOKENS = 240
COMPLETION_TOKENS = 320
TARGET_PRICE = Decimal("18.47")
MOQ_TIERS: list[dict[str, Any]] = [
    {"min_qty": 100, "unit_price_usd": Decimal("18.47")},
    {"min_qty": 500, "unit_price_usd": Decimal("17.55")},
    {"min_qty": 2000, "unit_price_usd": Decimal("16.62")},
]

_SESSION = cast(AsyncSession, object())


class StubProvider:
    """Deterministic stand-in for a configured LLM provider (no network, no API key)."""

    def __init__(self, text: str = "", *, error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        *,
        model_id: str,
        provider: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float,
    ) -> LlmProviderResponse:
        self.calls.append(
            {
                "model_id": model_id,
                "provider": provider,
                "messages": messages,
                "max_tokens": max_tokens,
            }
        )
        if self.error is not None:
            raise self.error
        return LlmProviderResponse(
            text=self.text,
            prompt_tokens=PROMPT_TOKENS,
            cached_tokens=0,
            completion_tokens=COMPLETION_TOKENS,
            model_id=model_id,
            provider=provider,
        )


class StubWallet:
    """Records every wallet interaction so credit accounting can be asserted exactly."""

    def __init__(self, *, lock_ok: bool = True, spend_ok: bool = True) -> None:
        self.lock_ok = lock_ok
        self.spend_ok = spend_ok
        self.ensured: list[UUID] = []
        self.locks: list[Decimal] = []
        self.spends: list[dict[str, Any]] = []

    async def ensure_wallet(
        self, session: AsyncSession, *, tenant_id: UUID
    ) -> None:
        self.ensured.append(tenant_id)

    async def lock_budget(
        self, session: AsyncSession, tenant_id: UUID, estimated_credits: Decimal
    ) -> bool:
        self.locks.append(estimated_credits)
        return self.lock_ok

    async def try_reserve_and_record_spend(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        idempotency_key: str,
        metric_type: UsageMetricType,
        units: Decimal,
        raw_cost_usd: Decimal,
        provider: str,
    ) -> tuple[bool, AutonomyLevel | None]:
        if not self.spend_ok:
            return False, AutonomyLevel.L0_COPILOT
        self.spends.append(
            {
                "tenant_id": tenant_id,
                "idempotency_key": idempotency_key,
                "metric_type": metric_type,
                "units": units,
                "raw_cost_usd": raw_cost_usd,
                "provider": provider,
            }
        )
        return True, None


def make_payload(**overrides: Any) -> SourcingCopyRewriteInput:
    base: dict[str, Any] = {
        "draft_id": uuid4(),
        "raw_title": "LED投光灯 50W IP65 防水户外 遥控 太阳能",
        "source_description": "工厂直供，出口级做工，支持贴牌定制。",
        "category": "LED Lighting",
        "hs_code": "9405.40",
        "target_price_usd": TARGET_PRICE,
        "moq_tiers": [dict(tier) for tier in MOQ_TIERS],
        "existing_bullets": ["CE/RoHS certified", "IP65 outdoor rated"],
        "target_channel": "ozon",
        "target_language": "ru",
        "tone": "conversion",
        "keywords": ["waterproof", "outdoor"],
    }
    base.update(overrides)
    return SourcingCopyRewriteInput(**base)


def llm_json(
    *,
    title: str = "Светодиодный прожектор 50W IP65",
    bullets: list[str] | None = None,
    description: str = "Розничная цена $18.47, опт от 100 шт.",
    seo_keywords: list[str] | None = None,
    fenced: bool = True,
    preamble: str = "",
) -> str:
    body = json.dumps(
        {
            "title": title,
            "bullet_points": bullets
            if bullets is not None
            else [
                "Готово к продаже под вашим брендом",
                "Водонепроницаемый корпус IP65 для улицы",
                "Розничная цена $18.47 (USD)",
                "Опт: 100 шт. по $18.47, 500 шт. по $17.55, 2000 шт. по $16.62",
                "Гарантия 12 месяцев и документы CE/RoHS",
            ],
            "description": description,
            "seo_keywords": seo_keywords if seo_keywords is not None else ["прожектор", "LED"],
        },
        ensure_ascii=False,
    )
    if not fenced:
        return f"{preamble}{body}"
    return f"{preamble}```json\n{body}\n```"


def rewriter(
    provider: StubProvider, wallet: StubWallet | None = None
) -> tuple[AiEngineSourcingCopyRewriter, StubWallet]:
    recorder = wallet or StubWallet()
    adapter = AiEngineSourcingCopyRewriter(
        TieredModelRouter(provider_client=provider),
        cast(CreditWalletService, recorder),
    )
    return adapter, recorder


# --------------------------------------------------------------------------------------
# Deterministic fallback: real localized copy in every supported language
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("language", sorted(SUPPORTED_REWRITE_LANGUAGES))
def test_deterministic_fallback_writes_all_twelve_languages(language: str) -> None:
    marker = {
        "en": "with",
        "zh": "的",
        "es": " con ",
        "pt": " com ",
        "ru": "Шкала",
        "th": "ราคา",
        "vi": " với ",
        "id": " dengan ",
        "ms": " dengan ",
        "fr": " avec ",
        "de": " mit ",
        "ja": "の",
    }[language]
    channel = "ozon" if language == "ru" else "shopify"
    payload = make_payload(target_language=language, target_channel=channel)

    output = build_deterministic_localized_copy(payload)

    assert output.rewrite_mode == "deterministic_fallback"
    assert output.model_used == "deterministic-rule-v1"
    assert output.credits_consumed == Decimal("0.0000")
    assert output.fallback_reason is None
    assert output.target_language == language
    assert output.title.strip()
    assert len(output.bullet_points) == 5
    body = " ".join([output.title, *output.bullet_points, output.description])
    # Exact payload money survives, in every language.
    assert "$18.47" in body
    assert "$17.55" in body and "$16.62" in body
    assert "2,000" in body
    assert output.seo_keywords
    assert marker in body, f"expected the {language} template, got: {body[:120]}"
    assert CHANNEL_DISPLAY_NAMES[channel] in output.description


@pytest.mark.parametrize(
    ("language", "expected_text"),
    [
        ("zh", "照明"),
        ("ja", "照明"),
        ("ru", "Светодиодное"),
        ("th", "ไฟ LED"),
        ("fr", "Éclairage"),
    ],
)
def test_fallback_uses_the_requested_script(
    language: str, expected_text: str
) -> None:
    payload = make_payload(target_language=language, target_channel="shopee")

    output = build_deterministic_localized_copy(payload)
    rendered = output.title + output.description + "".join(output.bullet_points)

    assert expected_text in rendered


def test_fallback_preserves_exact_decimal_price_and_moq_ladder() -> None:
    payload = make_payload(
        target_price_usd=Decimal("1999.9900"),
        moq_tiers=[
            {"min_qty": 50, "unit_price_usd": Decimal("1999.99")},
            {"min_qty": 250, "unit_price_usd": Decimal("1899.125")},
            {"min_qty": 1500, "unit_price_usd": Decimal("1799.00")},
        ],
        target_channel="alibaba_email",
        target_language="en",
    )

    output = build_deterministic_localized_copy(payload, reason="llm_invalid_output")
    rendered = " ".join([output.title, *output.bullet_points, output.description])

    assert output.fallback_reason == "llm_invalid_output"
    assert "$1999.99" in rendered
    assert "$1899.125" in rendered, "extra Decimal precision must never be rounded away"
    assert "$1799" in rendered
    assert "1,500" in rendered
    # No invented or rounded money may appear.
    assert "$2000" not in rendered
    assert "$1899.13" not in rendered


def test_fallback_localizes_category_and_attributes() -> None:
    payload = make_payload(target_channel="shopee", target_language="th")

    output = build_deterministic_localized_copy(payload)

    assert "ไฟ LED" in output.title
    assert "กันน้ำ" in "".join(output.bullet_points)


def test_platform_guidelines_cover_every_ecommerce_channel() -> None:
    assert len(DEFAULT_LANGUAGE_BY_CHANNEL) == 16
    assert set(DEFAULT_LANGUAGE_BY_CHANNEL) == set(PLATFORM_COPY_GUIDELINES)
    assert set(DEFAULT_LANGUAGE_BY_CHANNEL) == set(CHANNEL_DISPLAY_NAMES)
    for language in DEFAULT_LANGUAGE_BY_CHANNEL.values():
        assert language in SUPPORTED_REWRITE_LANGUAGES
    assert DEFAULT_LANGUAGE_BY_CHANNEL["ozon"] == "ru"
    assert DEFAULT_LANGUAGE_BY_CHANNEL["shopee"] == "th"
    assert DEFAULT_LANGUAGE_BY_CHANNEL["mercado_libre"] == "es"
    for guideline in PLATFORM_COPY_GUIDELINES.values():
        assert len(guideline) > 40


# --------------------------------------------------------------------------------------
# LLM path: fenced JSON, all locales, exact Decimal credit accounting
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("channel", "language"),
    [
        ("ozon", "ru"),
        ("shopee", "th"),
        ("mercado_libre", "es"),
        ("amazon_sp", "en"),
        ("alibaba_email", "en"),
    ],
)
async def test_llm_rewrite_succeeds_across_locales(channel: str, language: str) -> None:
    provider = StubProvider(llm_json())
    adapter, wallet = rewriter(provider)
    payload = make_payload(target_channel=channel, target_language=language)
    tenant_id = uuid4()

    output = await adapter.rewrite_copy(_SESSION, tenant_id, payload)

    assert output.rewrite_mode == "llm"
    assert output.fallback_reason is None
    assert len(output.bullet_points) == 5
    assert output.title == "Светодиодный прожектор 50W IP65"
    assert output.target_channel == channel
    assert output.target_language == language
    assert output.model_used == "gemini-3.1-flash-lite"

    expected_credits = calculate_cost_and_credits(
        "gemini-3.1-flash-lite", PROMPT_TOKENS, 0, COMPLETION_TOKENS
    )[1].quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    assert output.credits_consumed == expected_credits
    assert isinstance(output.credits_consumed, Decimal)

    assert wallet.ensured == [tenant_id]
    assert len(wallet.locks) == 1
    assert wallet.locks[0] >= Decimal(1)
    assert len(wallet.spends) == 1
    spend = wallet.spends[0]
    assert spend["metric_type"] == UsageMetricType.AI_CREDIT
    assert spend["units"] == expected_credits
    assert spend["provider"] == "gemini-3.1-flash-lite"
    assert spend["raw_cost_usd"] == calculate_cost_and_credits(
        "gemini-3.1-flash-lite", PROMPT_TOKENS, 0, COMPLETION_TOKENS
    )[0]
    assert spend["idempotency_key"].startswith("ai:")


async def test_prompt_carries_platform_rules_language_and_price_guard() -> None:
    provider = StubProvider(llm_json())
    adapter, _ = rewriter(provider)
    payload = make_payload(target_channel="ozon", target_language="ru")

    await adapter.rewrite_copy(_SESSION, uuid4(), payload)

    call = provider.calls[0]
    system_prompt = call["messages"][0]["content"]
    user_prompt = call["messages"][1]["content"]
    assert "bullet_points" in system_prompt
    assert "Never invent, round, discount" in system_prompt
    assert "Ozon RU technical specification" in user_prompt
    assert "Russian" in user_prompt
    assert "target_price_usd=$18.47" in user_prompt
    assert "100 units at $18.47" in user_prompt


async def test_markdown_fenced_json_with_surrounding_prose_is_parsed() -> None:
    provider = StubProvider(llm_json(preamble="Here is the listing:\n"))
    adapter, wallet = rewriter(provider)

    output = await adapter.rewrite_copy(_SESSION, uuid4(), make_payload())

    assert output.rewrite_mode == "llm"
    assert len(wallet.spends) == 1


async def test_short_bullet_list_is_padded_to_exactly_five() -> None:
    provider = StubProvider(llm_json(bullets=["Один пункт", "Второй пункт"]))
    adapter, wallet = rewriter(provider)

    output = await adapter.rewrite_copy(_SESSION, uuid4(), make_payload())

    assert output.rewrite_mode == "llm"
    assert len(output.bullet_points) == 5
    assert output.bullet_points[0] == "Один пункт"
    rendered = " ".join(output.bullet_points)
    assert "$18.47" in rendered, "padding must reuse the payload price, never invent one"
    assert len(wallet.spends) == 1


async def test_overlong_bullet_list_is_truncated_to_five() -> None:
    provider = StubProvider(
        llm_json(bullets=[f"Пункт {index}" for index in range(8)])
    )
    adapter, _ = rewriter(provider)

    output = await adapter.rewrite_copy(_SESSION, uuid4(), make_payload())

    assert output.rewrite_mode == "llm"
    assert output.bullet_points == [f"Пункт {index}" for index in range(5)]


async def test_overlong_title_is_capped_at_two_hundred_characters() -> None:
    provider = StubProvider(llm_json(title="Ж" * 260))
    adapter, _ = rewriter(provider)

    output = await adapter.rewrite_copy(_SESSION, uuid4(), make_payload())

    assert output.rewrite_mode == "llm"
    assert len(output.title) == 200


# --------------------------------------------------------------------------------------
# Honest failure handling: no deduction, deterministic copy, explicit reason
# --------------------------------------------------------------------------------------


async def test_invented_price_falls_back_without_charging() -> None:
    provider = StubProvider(
        llm_json(description="Только сегодня $149.99 вместо $18.47!", bullets=[
            "Готово к продаже",
            "Водонепроницаемый корпус IP65",
            "Цена $149.99 за штуку",
            "Опт от 100 шт.",
            "Гарантия 12 месяцев",
        ])
    )
    adapter, wallet = rewriter(provider)

    output = await adapter.rewrite_copy(_SESSION, uuid4(), make_payload())

    assert output.rewrite_mode == "deterministic_fallback"
    assert output.fallback_reason == "llm_invalid_output"
    assert output.model_used == "deterministic-rule-v1"
    assert output.credits_consumed == Decimal("0.0000")
    assert wallet.spends == []


async def test_discounted_price_also_contradicts_the_payload() -> None:
    provider = StubProvider(
        llm_json(bullets=[
            "Готово к продаже",
            "Водонепроницаемый корпус IP65",
            "Цена $15.99 за штуку",
            "Опт от 100 шт.",
            "Гарантия 12 месяцев",
        ], description="Купите за $15.99.")
    )
    adapter, wallet = rewriter(provider)

    output = await adapter.rewrite_copy(_SESSION, uuid4(), make_payload())

    assert output.rewrite_mode == "deterministic_fallback"
    assert output.fallback_reason == "llm_invalid_output"
    assert wallet.spends == []


async def test_tier_price_quoted_verbatim_is_accepted() -> None:
    provider = StubProvider(
        llm_json(description="Опт от 500 шт. по $17.55.")
    )
    adapter, wallet = rewriter(provider)

    output = await adapter.rewrite_copy(_SESSION, uuid4(), make_payload())

    assert output.rewrite_mode == "llm"
    assert len(wallet.spends) == 1


@pytest.mark.parametrize(
    "completion",
    [
        "I am not able to help with that request.",
        "",
        "   ",
        '{"title": "only a title"}',
        '{"title": "", "bullet_points": ["a"], "description": "d"}',
        '{"title": "T", "bullet_points": ["a"], "description": ""}',
        '{"title": "T", "bullet_points": [], "description": "d"}',
        "[1, 2, 3]",
    ],
)
async def test_invalid_completion_falls_back_without_charging(completion: str) -> None:
    provider = StubProvider(completion)
    adapter, wallet = rewriter(provider)

    output = await adapter.rewrite_copy(_SESSION, uuid4(), make_payload())

    assert output.rewrite_mode == "deterministic_fallback"
    assert output.fallback_reason == "llm_invalid_output"
    assert output.credits_consumed == Decimal("0.0000")
    assert len(output.bullet_points) == 5
    assert wallet.spends == []


async def test_provider_runtime_error_returns_unbilled_fallback() -> None:
    provider = StubProvider(error=RuntimeError("Provider 'gemini' API key is not configured."))
    adapter, wallet = rewriter(provider)

    output = await adapter.rewrite_copy(_SESSION, uuid4(), make_payload())

    assert output.rewrite_mode == "deterministic_fallback"
    assert output.fallback_reason == "llm_provider_unavailable"
    assert output.model_used == "deterministic-rule-v1"
    assert output.credits_consumed == Decimal("0.0000")
    assert wallet.spends == []
    assert len(provider.calls) == 1


# --------------------------------------------------------------------------------------
# Wallet enforcement
# --------------------------------------------------------------------------------------


async def test_depleted_wallet_raises_402_before_calling_the_provider() -> None:
    provider = StubProvider(llm_json())
    adapter, wallet = rewriter(provider, StubWallet(lock_ok=False))

    with pytest.raises(AppError) as raised:
        await adapter.rewrite_copy(_SESSION, uuid4(), make_payload())

    error = raised.value
    assert error.status_code == 402
    assert error.code == "ERR_CREDITS_DEPLETED"
    assert error.slug == "ai-credits-depleted"
    assert error.title == "AI Credits Depleted"
    assert error.detail == "Top up the credit wallet to resume AI copy rewriting."
    assert provider.calls == []
    assert wallet.spends == []


async def test_rejected_spend_after_generation_raises_402() -> None:
    provider = StubProvider(llm_json())
    adapter, wallet = rewriter(provider, StubWallet(spend_ok=False))

    with pytest.raises(AppError) as raised:
        await adapter.rewrite_copy(_SESSION, uuid4(), make_payload())

    assert raised.value.status_code == 402
    assert raised.value.code == "ERR_CREDITS_DEPLETED"
    assert len(provider.calls) == 1
    assert wallet.spends == []


# --------------------------------------------------------------------------------------
# Import boundary: catalog must stay free of ai_engine/channels
# --------------------------------------------------------------------------------------


def test_protocol_module_imports_no_ai_engine_or_channel_symbols() -> None:
    source = PROTOCOL_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.append(node.module)

    assert imported, "expected the protocol module to import something"
    forbidden = ("erp_core.ai_engine", "erp_core.channels")
    violations = [module for module in imported if module.startswith(forbidden)]
    assert violations == []

    allowed_prefixes = (
        "__future__",
        "re",
        "dataclasses",
        "datetime",
        "decimal",
        "typing",
        "uuid",
        "pydantic",
        "sqlalchemy",
        "erp_core.shared",
    )
    unexpected = [
        module
        for module in imported
        if not module.startswith(allowed_prefixes)
    ]
    assert unexpected == []


def test_importing_the_protocol_module_never_loads_ai_engine() -> None:
    script = (
        "import sys; import erp_core.catalog.sourcing_copy_protocol; "
        "print('ai_engine' if 'erp_core.ai_engine' in sys.modules else 'clean'); "
        "print('channels' if 'erp_core.channels' in sys.modules else 'clean')"
    )
    env = {**os.environ, "PYTHONPATH": str(PROTOCOL_SOURCE.parents[2])}

    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )

    assert completed.stdout.split() == ["clean", "clean"]
