"""Grounded AI auto-reply guardrails plus deterministic AI customer profiling.

Design invariants
-----------------
1. **Evidence or silence.** A reply is only ever drafted from live ``ErpToolRegistry``
   SQL results and/or tenant-scoped knowledge-base chunks. With no evidence the service
   abstains and returns a clarification template that quotes no price, stock or date.
2. **Risk guardrail is absolute.** Messages touching refunds, chargebacks, disputes,
   compensation, forced price changes, bank-account changes or payment reconciliation are
   never auto-sent: they are downgraded to an L1 ``ai_proposals`` row with
   ``status='pending'`` for a human reviewer.
3. **Money is exact.** Every monetary value is :class:`decimal.Decimal` quantized
   ``ROUND_HALF_UP`` to ``0.01``; confidence scores are quantized to ``0.001`` to fit
   ``ai_proposals.confidence_score NUMERIC(4,3)``.
4. **Tenant + AGENT isolation.** Conversation loading and the AGENT mailbox visibility
   rule are delegated to :class:`AiProposalGeneratorService`, so this module cannot drift
   from the policy already enforced by ``POST /conversations/{id}/propose-reply``. The
   customer-level AI profile read applies the same rule with plain tenant-scoped SQL.
5. **Per-inbound-turn idempotency.** When a caller supplies ``source_message_id``, an
   outbound reply already stored for that inbound message is replayed instead of re-sent,
   and the inbound id is persisted as ``channel_metadata.in_reply_to_message_id``.

All drafting is deterministic (no LLM call), therefore every number in an outbound
message is traceable to a stored evidence row. Any LLM-polished candidate must additionally
pass the paired numeric guard: numbers *and* price mentions are verified separately, so a
stock count can never become a quoted price.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.ai_engine.ingestion import strip_dynamic_prices_for_answer
from erp_core.ai_engine.models import AiProposal
from erp_core.ai_engine.proposal_generator import (
    AiProposalGeneratorService,
    ConversationContext,
    LlmClientPort,
    RecentMessage,
)
from erp_core.ai_engine.rag_service import build_websearch_tsquery
from erp_core.ai_engine.router_llm import MODEL_CATALOG, AsyncMultiProviderClient
from erp_core.ai_engine.tools import ErpToolRegistry
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import AppError
from erp_core.tenancy.audit import record_audit_event
from erp_core.tenancy.dependencies import CurrentPrincipal

BuyerType = Literal["b2b_buyer", "b2b_distributor", "b2c_consumer", "unknown"]
PriceSensitivity = Literal["low", "medium", "high"]
ReplyAction = Literal["auto_replied", "proposal_created", "abstained"]
ReplyMode = Literal["auto", "proposal_only"]

_MONEY_Q = Decimal("0.01")
_CONFIDENCE_Q = Decimal("0.001")
_MAX_SKUS = 10
_MAX_KB_SNIPPET = 240


# --------------------------------------------------------------------------------------
# Request / response contracts
# --------------------------------------------------------------------------------------


class AutoReplySettingRead(BaseModel):
    """Per-conversation auto-reply toggle."""

    conversation_id: UUID
    ai_auto_reply_enabled: bool


class AutoReplySettingUpdate(BaseModel):
    enabled: bool


class AutoReplyRunRequest(BaseModel):
    mode: ReplyMode = "auto"
    max_kb_hits: int = Field(default=3, ge=1, le=20)


class AutoReplyRunResponse(BaseModel):
    conversation_id: UUID
    action: ReplyAction
    message_id: UUID | None = None
    proposal_id: UUID | None = None
    reply_text: str | None = None
    kb_hits: list[dict[str, Any]] = Field(default_factory=list)
    tool_evidence: list[dict[str, Any]] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    confidence: Decimal


class ProfileExtractionRequest(BaseModel):
    sync_to_crm: bool = True


class CustomerAiProfileRead(BaseModel):
    customer_id: UUID
    buyer_type: BuyerType = "unknown"
    company_name: str | None = None
    country_code: str | None = None
    preferred_language: str = "en"
    interested_skus: list[str] = Field(default_factory=list)
    estimated_order_value_usd: Decimal | None = None
    target_incoterm: str | None = None
    purchase_intent_score: int = 0
    price_sensitivity: PriceSensitivity = "low"
    key_requirements: list[str] = Field(default_factory=list)
    pain_points: list[str] = Field(default_factory=list)
    next_best_action: str = ""
    ai_summary: str = ""
    tags: list[str] = Field(default_factory=list)
    updated_at: datetime
    synced_lead_id: UUID | None = None
    synced_followup_id: UUID | None = None


# --------------------------------------------------------------------------------------
# Deterministic guardrail patterns
# --------------------------------------------------------------------------------------

#: Ordered ``(flag, pattern)`` pairs. Order defines the stable ``risk_flags`` sequence.
_RISK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "refund",
        re.compile(
            r"\brefund(?:s|ed|ing)?\b|退款|退钱|退还货款|返还货款|\breembols(?:o|os)\b",
            re.I,
        ),
    ),
    (
        "chargeback",
        re.compile(r"\bcharge[\s-]?back\b|拒付|退单|reversal|\bcontracargos?\b", re.I),
    ),
    (
        "dispute",
        re.compile(
            r"\bdispute\b|\bclaim\b|纠纷|投诉理赔|争议|\bdisputas?\b|\breclamo\s+legal\b",
            re.I,
        ),
    ),
    (
        "compensation",
        re.compile(
            r"\bcompensat(?:e|ion|ory)\b|\bdamages\b|赔付|赔偿"
            r"|\bcompensaci[oó]n(?:es)?\b|\bindemnizaci[oó]n(?:es)?\b",
            re.I,
        ),
    ),
    (
        "goods_return",
        re.compile(
            r"\breturn(?:s|ed|ing)?\b[^.?!]{0,40}\b(?:goods|order|money|items)\b|退货"
            r"|\bdevoluci[oó]n(?:es)?\b",
            re.I,
        ),
    ),
    (
        "price_override",
        re.compile(
            r"\bdiscount\s+override\b|\bextra\s+discount\b|\boverride\b[^.?!]{0,20}\bprice\b"
            r"|\b(?:force|forced|change|lower|reduce|drop)\w*\b[^.?!]{0,15}\b(?:price|pricing)\b"
            r"|强行改价|降价|改价|折扣"
            r"|\bdescuento\s+especial\b|\bbajar\s+el\s+precio\b",
            re.I,
        ),
    ),
    (
        "bank_account_change",
        re.compile(
            r"\bbank\s+(?:account|details?)\b[^.?!]{0,40}\b(?:chang|updat|new)\w*"
            r"|\b(?:change|update|switch)\w*\b[^.?!]{0,30}\bbank\s+(?:account|details?)\b"
            r"|\bbeneficiary\b[^.?!]{0,30}\bchang\w*"
            r"|改账户|换账户|更改账号|银行账号变更|变更账户"
            r"|\bcuenta\s+bancaria\b|\bcambiar\s+(?:de\s+)?banco\b",
            re.I,
        ),
    ),
    (
        "wire_transfer",
        re.compile(
            r"\bwire\s+transfer\b|\btelegraphic\s+transfer\b"
            r"|(?<![A-Za-z])T/T(?![A-Za-z])|电汇|汇款|\btransferencia\s+bancaria\b",
            re.I,
        ),
    ),
    (
        "payment_reconciliation",
        re.compile(r"\bpayment\s+(?:reconcil\w*|receipt)\b|付款核销|核销", re.I),
    ),
)

#: Redline flags whose explicit negation removes the risk ("no refund needed").
_NEGATABLE_FLAGS = frozenset({"refund", "dispute"})

#: Negation cues evaluated inside the window preceding a redline match.
_NEGATION_CUE = re.compile(
    r"\b(?:no|not|never|none|without|no\s+need(?:\s+for)?|don'?t|do\s+not|doesn'?t"
    r"|does\s+not|didn'?t|did\s+not|won'?t|will\s+not|isn'?t|is\s+not|aren'?t|are\s+not"
    r"|not\s+needed|no\s+longer)\b"
    r"|无需|不需要|不用|不必|没有|不需",
    re.I,
)

_SENTENCE_BOUNDARY = frozenset(".!?;。！？；\n")

_CJK = re.compile(r"[\u4e00-\u9fff]")
_SPANISH = re.compile(r"\b(?:hola|precio|precios|cotizacion|cotización|gracias|necesito)\b", re.I)
_SKU_TOKEN = re.compile(r"\b[A-Z][A-Z0-9]*(?:[-_/][A-Z0-9]+){1,6}\b")
_NON_SKU_TOKENS = frozenset({"T/T", "C/O", "B/L", "OEM/ODM", "FOB/CIF", "E/O", "P/O"})
_QUANTITY = re.compile(
    r"\b\d[\d,\s]*\s*(?:pcs|pieces|units|sets|meters|meter|mtr|k)\b|\d+\s*(?:个|米|件|套|只|箱)",
    re.I,
)
_PRICE_INTENT = re.compile(
    r"\b(?:price|pricing|quote|quotation|cost|discount|cheap)\b|价格|报价|单价|多少钱|折扣|优惠",
    re.I,
)
_INCOTERM = re.compile(r"\b(?:EXW|FOB|CFR|CIF|CPT|CIP|DAP|DDP)\b")
_PORT_HINT = re.compile(r"\bport\b|\bdestination\b|港口|目的港", re.I)
_DISTRIBUTOR = re.compile(
    r"\b(?:distributor|wholesaler|reseller|importer|trading\s+company|dealer)\b"
    r"|经销商|批发商|贸易商|进口商",
    re.I,
)
_PRICE_PRESSURE = re.compile(
    r"\b(?:too\s+expensive|cheaper|cheapest|best\s+price|competitive\s+price|cost\s+down)\b"
    r"|\b(?:price|pricing|cost|quote)\b[^.?!]{0,15}\btoo\s+high\b"
    r"|太贵|价格高|便宜|最低价|降价空间",
    re.I,
)

#: ``(canonical label, pattern)`` requirement detectors, evaluated on every inbound message.
_REQUIREMENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("certification", re.compile(r"\bcertificat(?:e|ion|ed)\b|认证|证书", re.I)),
    ("ce", re.compile(r"\bce\b", re.I)),
    ("rohs", re.compile(r"\brohs\b", re.I)),
    ("ul", re.compile(r"\bul\b", re.I)),
    ("ip_rating", re.compile(r"\bip\s?[4-6][0-9]\b|防水等级|ip 等级", re.I)),
    ("cri", re.compile(r"\bcri\b|显色指数", re.I)),
    ("dimmable", re.compile(r"\bdimm(?:able|ing)\b|调光", re.I)),
    ("voltage", re.compile(r"\bvoltage\b|\b\d{2,3}\s?v\b|电压", re.I)),
    ("wattage", re.compile(r"\bwatt(?:age)?\b|\b\d{1,3}\s?w\b|瓦数|功率", re.I)),
    ("warranty", re.compile(r"\bwarrant(?:y|ies)\b|质保|保修", re.I)),
    (
        "custom_packaging",
        re.compile(r"\b(?:custom|oem|odm)\b[^.?!]{0,25}\bpackag\w*|定制包装|彩盒", re.I),
    ),
    ("sample_order", re.compile(r"\bsample\b|样品", re.I)),
    ("lead_time", re.compile(r"\blead\s?time\b|交期|货期", re.I)),
)

#: ``(canonical label, pattern)`` pain-point detectors.
_PAIN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "price_pressure",
        re.compile(r"\bexpensive\b|\btoo\s+expensive\b|\bcost\s+down\b|太贵|价格高", re.I),
    ),
    (
        "slow_delivery",
        re.compile(r"\bdelay(?:ed|s)?\b|\blate\s+(?:delivery|shipment)\b|延迟|太慢|交期长", re.I),
    ),
    (
        "quality_concern",
        re.compile(
            r"\bquality\s+(?:issue|problem|concern|complaint)\b|\bdefect\w*\b|质量|不良", re.I
        ),
    ),
    ("spec_unclear", re.compile(r"\bnot\s+sure\b|\bunclear\b|不确定|不清楚", re.I)),
    (
        "moq_barrier",
        re.compile(r"\bmoq\b[^.?!]{0,25}\b(?:too\s+)?high\b|起订量高|起订量太高", re.I),
    ),
)

#: Country-name to ISO-3166 alpha-2 hints used only when the CRM record is blank.
_COUNTRY_HINTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bgermany\b|\bdeutschland\b|德国", re.I), "DE"),
    (re.compile(r"\bspain\b|\bespaña\b|西班牙", re.I), "ES"),
    (re.compile(r"\bfrance\b|法国", re.I), "FR"),
    (re.compile(r"\bitaly\b|意大利", re.I), "IT"),
    (re.compile(r"\bnetherlands\b|\bholland\b|荷兰", re.I), "NL"),
    (re.compile(r"\bpoland\b|波兰", re.I), "PL"),
    (re.compile(r"\bportugal\b|葡萄牙", re.I), "PT"),
    (re.compile(r"\bunited\s+kingdom\b|\buk\b|\bbritain\b|英国", re.I), "GB"),
    (re.compile(r"\bunited\s+states\b|\busa\b|\bu\.s\.\b|美国", re.I), "US"),
    (re.compile(r"\bcanada\b|加拿大", re.I), "CA"),
    (re.compile(r"\baustralia\b|澳大利亚|澳洲", re.I), "AU"),
    (re.compile(r"\bbrazil\b|巴西", re.I), "BR"),
    (re.compile(r"\bmexico\b|墨西哥", re.I), "MX"),
    (re.compile(r"\bjapan\b|日本", re.I), "JP"),
    (re.compile(r"\bkorea\b|韩国", re.I), "KR"),
    (re.compile(r"\bindia\b|印度", re.I), "IN"),
    (re.compile(r"\bturkey\b|土耳其", re.I), "TR"),
    (re.compile(r"\buae\b|\bdubai\b|阿联酋|迪拜", re.I), "AE"),
    (re.compile(r"\bsaudi\b|沙特", re.I), "SA"),
    (re.compile(r"\bsouth\s+africa\b|南非", re.I), "ZA"),
)


# --------------------------------------------------------------------------------------
# Small value objects
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AutoReplyDecision:
    """Pure decision output: what the run endpoint is allowed to do."""

    action: ReplyAction
    reason: str


@dataclass(frozen=True, slots=True)
class ProfileCustomer:
    customer_id: UUID
    display_name: str
    customer_type: str
    country_code: str | None
    preferred_language: str
    company_name: str | None
    company_country_code: str | None


@dataclass(frozen=True, slots=True)
class ProfileMessage:
    direction: str
    sender_type: str
    body_text: str


@dataclass(frozen=True, slots=True)
class ProfileOrder:
    order_no: str
    total_amount: Decimal
    currency: str


@dataclass(frozen=True, slots=True)
class ProfileQuote:
    incoterm: str | None
    total_amount: Decimal
    currency: str


@dataclass(frozen=True, slots=True)
class ProfileSources:
    """Everything profiling is allowed to read; assembled with tenant-scoped SQL."""

    customer: ProfileCustomer
    messages: tuple[ProfileMessage, ...]
    orders: tuple[ProfileOrder, ...] = ()
    quotes: tuple[ProfileQuote, ...] = ()


class KbSearchPort(Protocol):
    """Adapter seam for :class:`HybridRagService` (embedding search) when configured."""

    async def search(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        query: str,
        top_k: int = 3,
    ) -> Any:
        """Return an object exposing ``hits`` with ``chunk_id``/``doc_id``/``content``."""
        ...


# --------------------------------------------------------------------------------------
# Pure guardrail helpers (unit-testable without a database)
# --------------------------------------------------------------------------------------


def _is_negated_match(message_text: str, match_start: int) -> bool:
    """True when an explicit negation cue governs the redline match at ``match_start``."""
    window = message_text[max(0, match_start - 48) : match_start]
    boundary = max((window.rfind(char) for char in _SENTENCE_BOUNDARY), default=-1)
    if boundary != -1:
        window = window[boundary + 1 :]
    return bool(_NEGATION_CUE.search(window))


def detect_risk_flags(message_text: str) -> list[str]:
    """Return the ordered risk flags forcing this message down to L1 proposal review.

    Refund/dispute redlines are skipped when *every* occurrence is explicitly negated
    (``no refund needed``, ``不需要退款``, ``no disputa``), so a non-risky enquiry is not
    falsely escalated. All other redlines stay absolute.
    """
    if not message_text.strip():
        return []
    flags: list[str] = []
    for flag, pattern in _RISK_PATTERNS:
        matches = list(pattern.finditer(message_text))
        if not matches:
            continue
        if flag in _NEGATABLE_FLAGS and all(
            _is_negated_match(message_text, match.start()) for match in matches
        ):
            continue
        flags.append(flag)
    return flags


def detect_reply_language(message_text: str) -> str:
    """Mirror the reply-language detection used by the grounded proposal generator."""
    if _CJK.search(message_text):
        return "zh"
    if _SPANISH.search(message_text):
        return "es"
    return "en"


def extract_sku_tokens(message_text: str) -> list[str]:
    """Extract literal SKU-like tokens (never invented, uppercased, order preserved)."""
    found: list[str] = []
    for raw in _SKU_TOKEN.findall(message_text.upper()):
        token = raw.strip("-_/")
        if len(token) < 6 or token in _NON_SKU_TOKENS:
            continue
        if "-" not in token and "_" not in token:
            continue
        if token not in found:
            found.append(token)
    return found[:_MAX_SKUS]


def normalize_country_code(value: str | None) -> str | None:
    """Return a 2-letter uppercase ISO code, or ``None`` when the value is unusable."""
    if value is None:
        return None
    cleaned = value.strip().upper()
    if len(cleaned) != 2 or not cleaned.isalpha():
        return None
    return cleaned


def infer_country_code(message_text: str) -> str | None:
    """Infer an alpha-2 country code from explicit country names in the thread."""
    for pattern, code in _COUNTRY_HINTS:
        if pattern.search(message_text):
            return code
    return None


def extract_incoterm(message_text: str) -> str | None:
    match = _INCOTERM.search(message_text.upper())
    return match.group(0) if match is not None else None


def usd_order_total(orders: Sequence[ProfileOrder]) -> Decimal | None:
    """Sum only USD orders: no FX rate is invented, so other currencies are ignored."""
    total = Decimal("0.00")
    seen = False
    for order in orders:
        if order.currency.strip().upper() != "USD":
            continue
        total += order.total_amount
        seen = True
    if not seen:
        return None
    return total.quantize(_MONEY_Q, rounding=ROUND_HALF_UP)


_COURTESY_PATTERN = re.compile(
    r"^(?:"
    r"(?:ok|okay|k|kk|got\s+it|noted|sure|alright|sounds\s+good|great|perfect|awesome)"
    r"(?:[\s,!.~]*(?:thx|tx|ty|tysm|thanks|thank\s+you|much\s+appreciated))?"
    r"|(?:thx|tx|ty|tysm|thanks|thank\s+you|appreciate\s+it|much\s+appreciated)"
    r"|(?:hi|hello|hey|good\s+(?:morning|afternoon|evening)|hola|buenos\s+dias|gracias)"
    r"|(?:你好|您好|哈喽|嗨|在吗|谢谢|多谢|感谢|好的|好哒|收到|明白|了解|知道了|ok谢谢|好的谢谢)"
    r")[\s!.,~。！？，]*$",
    re.IGNORECASE,
)


def is_courtesy_message(message_text: str) -> bool:
    """True only for short pure greetings or acknowledgements containing no product/order claims."""
    cleaned = message_text.strip()
    if not cleaned or len(cleaned) > 48 or "?" in cleaned or "？" in cleaned:
        return False
    if any(ch.isdigit() for ch in cleaned):
        return False
    return bool(_COURTESY_PATTERN.fullmatch(cleaned))


def decide_auto_reply(
    *,
    mode: ReplyMode,
    auto_reply_enabled: bool,
    has_evidence: bool,
    has_kb: bool,
    risk_flags: Sequence[str],
    is_courtesy: bool = False,
) -> AutoReplyDecision:
    """The single authority deciding auto-send vs. L1 proposal vs. abstention."""
    if risk_flags:
        return AutoReplyDecision("proposal_created", "risk_guardrail_forced_l1")
    if not has_evidence and not has_kb:
        if is_courtesy and mode == "auto" and auto_reply_enabled:
            return AutoReplyDecision("auto_replied", "courtesy_auto_reply")
        return AutoReplyDecision("abstained", "no_grounding_evidence")
    if mode != "auto":
        return AutoReplyDecision("proposal_created", "proposal_only_mode")
    if not auto_reply_enabled:
        return AutoReplyDecision("proposal_created", "auto_reply_disabled")
    return AutoReplyDecision("auto_replied", "grounded_auto_reply")


def compute_confidence(
    evidence: Sequence[dict[str, Any]],
    kb_hits: Sequence[dict[str, Any]],
    risk_flags: Sequence[str],
) -> Decimal:
    """Quantize a grounded confidence score into ``ai_proposals.confidence_score`` range."""
    score = Decimal("0.30")
    if evidence:
        score += Decimal("0.35")
        score += min(Decimal("0.10"), Decimal("0.02") * Decimal(len(evidence) - 1))
    if kb_hits:
        score += Decimal("0.10")
    if not evidence and not kb_hits:
        score = Decimal("0.10")
    if risk_flags:
        score = min(score, Decimal("0.35"))
    score = min(score, Decimal("0.95"))
    return score.quantize(_CONFIDENCE_Q, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------------------
# Deterministic drafting (no LLM, no invented figures)
# --------------------------------------------------------------------------------------


def evidence_summary_lines(evidence: Sequence[dict[str, Any]]) -> list[str]:
    """Render evidence as sentences whose numbers come strictly from ``data`` values."""
    lines: list[str] = []
    for item in evidence:
        tool_name = str(item.get("tool_name", ""))
        data = item.get("data")
        if not isinstance(data, dict):
            continue
        if tool_name == "get_live_sku_pricing_and_stock":
            tier = data.get("applied_moq_tier")
            tier_text = f", MOQ tier {tier} pcs" if isinstance(tier, int) else ""
            lines.append(
                f"SKU {data.get('sku')}: {data.get('applied_unit_price_usd')} USD/pc"
                f"{tier_text} for {data.get('qty_requested')} pcs, "
                f"{data.get('available_qty')} pcs available, "
                f"lead time {data.get('lead_time_days')} days ({data.get('incoterm')})"
            )
        elif tool_name == "get_live_order_tracking":
            shipments = data.get("shipments")
            tracking = ""
            if isinstance(shipments, list) and shipments:
                latest = shipments[0]
                if isinstance(latest, dict):
                    tracking = (
                        f", shipped via {latest.get('carrier_code')} "
                        f"({latest.get('tracking_number')}) with status "
                        f"{latest.get('standardized_status')}"
                    )
            lines.append(
                f"Order {data.get('order_no')}: fulfillment "
                f"{data.get('fulfillment_status')}, payment {data.get('payment_status')}"
                f"{tracking}"
            )
    return lines


_CHAT_CHANNELS = frozenset(
    {
        "telegram",
        "whatsapp_cloud",
        "whatsapp_coexistence",
        "messenger",
        "instagram_dm",
        "web_livechat",
    }
)
_NUM_TOKEN = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")

#: Evidence keys that carry an *explicit* unit/line price (never a quantity or stock count).
_PRICE_FIELDS = frozenset(
    {
        "applied_unit_price_usd",
        "line_total_usd",
        "retail_price_usd",
        "unit_price_usd",
    }
)

_PRICE_SYMBOLS = r"US\$|USD|EUR|CNY|RMB|GBP|JPY|AUD|CAD|HKD|[$¥€£]"

#: Price-shaped mentions in a drafted reply (currency prefixed, suffixed, or per-unit).
_PRICE_MENTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"(?:{_PRICE_SYMBOLS})\s*(\d[\d,]*(?:\.\d+)?)", re.I),
    re.compile(rf"(\d[\d,]*(?:\.\d+)?)\s*(?:{_PRICE_SYMBOLS})", re.I),
    re.compile(
        r"(\d[\d,]*(?:\.\d+)?)\s*(?:/|per\s+)\s*"
        r"(?:pc|pcs|piece|pieces|unit|units|set|sets|roll|rolls|meter|meters|m|kg)\b",
        re.I,
    ),
)

#: Cents-only price mentions such as ``$.50`` / ``price .50`` (captured as ``0.<cents>``).
_PRICE_CENTS_ONLY = re.compile(
    rf"(?:{_PRICE_SYMBOLS}|price|pricing|cost|quote)\W{{0,4}}\.(\d{{2}})(?!\d)", re.I
)


def _as_decimal(value: Any) -> Decimal | None:
    """Parse one evidence/mention value into an exact :class:`Decimal` (never a float)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int | float | str):
        raw = str(value).replace(",", "").strip()
        if not raw:
            return None
        try:
            parsed = Decimal(raw)
        except InvalidOperation:
            return None
        return parsed if parsed.is_finite() else None
    return None


def _extract_normalized_numbers(text_value: str) -> set[Decimal]:
    numbers: set[Decimal] = set()
    for match in _NUM_TOKEN.findall(text_value):
        try:
            numbers.add(Decimal(match.replace(",", "")).normalize())
        except InvalidOperation:
            continue
    return numbers


def _collect_verified_prices(evidence: Sequence[dict[str, Any]]) -> set[Decimal]:
    """Collect prices **only** from explicit price fields of verified ERP evidence."""
    prices: set[Decimal] = set()

    def walk(node: Any, key: str) -> None:
        if isinstance(node, dict):
            for child_key, child in node.items():
                walk(child, str(child_key).lower())
            return
        if isinstance(node, list | tuple):
            for item in node:
                walk(item, key)
            return
        if key in _PRICE_FIELDS:
            amount = _as_decimal(node)
            if amount is not None:
                prices.add(amount.normalize())

    for item in evidence:
        walk(item, "")
    return prices


def _extract_price_mentions(text_value: str) -> set[Decimal]:
    """Extract every price-formatted mention (``$8.50``, ``USD 100/pc``, ``¥32.50``, ``.50``)."""
    mentions: set[Decimal] = set()
    for pattern in _PRICE_MENTION_PATTERNS:
        for raw in pattern.findall(text_value):
            amount = _as_decimal(raw)
            if amount is not None:
                mentions.add(amount.normalize())
    for cents in _PRICE_CENTS_ONLY.findall(text_value):
        amount = _as_decimal(f"0.{cents}")
        if amount is not None:
            mentions.add(amount.normalize())
    return mentions


def _collect_allowed_numbers(
    *,
    evidence: Sequence[dict[str, Any]],
    kb_hits: Sequence[dict[str, Any]],
) -> set[Decimal]:
    """Numbers a reply may quote: only those stored in evidence/KB (never the buyer text)."""
    allowed = _extract_normalized_numbers(json.dumps(list(evidence), default=str))
    for hit in kb_hits:
        allowed.update(_extract_normalized_numbers(str(hit.get("snippet") or "")))
        allowed.update(_extract_normalized_numbers(str(hit.get("doc_title") or "")))
    return allowed


def verify_reply_numbers_grounded(
    candidate: str,
    *,
    evidence: Sequence[dict[str, Any]],
    kb_hits: Sequence[dict[str, Any]],
    buyer_message: str,
) -> bool:
    """Return True only if every numeric figure in ``candidate`` is grounded in facts.

    Two paired checks must both pass:

    1. **Numbers.** Every digit group in ``candidate`` must appear in the tool evidence or
       KB hits. ``buyer_message`` is accepted for call-site compatibility but is deliberately
       *not* merged: a buyer's counter-offer (``$8.50``) is not a verified price.
    2. **Prices.** Every price-formatted mention must match an explicit price field of the
       evidence (``applied_unit_price_usd``, ``line_total_usd``, ``retail_price_usd``,
       ``b2b_moq_tiers[*].unit_price_usd``). This stops a stock count (``100`` pcs) from being
       rendered as ``USD 100/pc`` and stops a buyer's counter-offer from being echoed back.
    """
    del buyer_message  # intentionally unused: buyer numbers never widen the allowed set
    cleaned = candidate.strip()
    if not cleaned or len(cleaned) > 1200:
        return False
    allowed = _collect_allowed_numbers(evidence=evidence, kb_hits=kb_hits)
    candidate_numbers = _extract_normalized_numbers(cleaned)
    if not candidate_numbers.issubset(allowed):
        return False
    verified_prices = _collect_verified_prices(evidence)
    return _extract_price_mentions(cleaned).issubset(verified_prices)


def _kb_lines(kb_hits: Sequence[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for hit in kb_hits:
        title = str(hit.get("doc_title") or "").strip()
        snippet = str(hit.get("snippet") or "").strip()
        if not snippet:
            continue
        lines.append(f"{title}: {snippet}" if title else snippet)
    return lines


def build_grounded_reply(
    *,
    language: str,
    customer_name: str,
    evidence: Sequence[dict[str, Any]],
    kb_hits: Sequence[dict[str, Any]],
    channel_type: str = "email",
) -> str:
    """Compose a natural, evidence-cited reply. Every digit is copied from stored evidence."""
    name = customer_name.strip()
    is_chat = channel_type in _CHAT_CHANNELS
    evidence_lines = evidence_summary_lines(evidence)
    knowledge_lines = _kb_lines(kb_hits)

    if language == "zh":
        greeting = f"{name} 您好！" if name else "您好！"
        parts = [greeting]
        if evidence_lines:
            parts.append("帮您核实了最新库存与报价：\n" + "\n".join(evidence_lines))
        if knowledge_lines:
            parts.append("\n".join(knowledge_lines))
        if evidence_lines:
            parts.append("方便告知一下目的港和所需认证吗？确认后马上给您出正式 PI。")
        else:
            parts.append("方便发一下具体型号、预计数量和目的港吗？我这边马上帮您核价出 PI。")
        if not is_chat:
            parts.append("顺祝商祺，\n外贸销售团队")
        return "\n\n".join(parts)

    if language == "es":
        greeting = f"Hola {name}," if name else "Hola,"
        parts = [greeting]
        if evidence_lines:
            parts.append(
                "Acabo de verificar stock y precio en tiempo real:\n"
                + "\n".join(evidence_lines)
            )
        if knowledge_lines:
            parts.append("\n".join(knowledge_lines))
        if evidence_lines:
            parts.append("¿Qué puerto de destino y certificaciones necesita para prepararle la PI?")
        else:
            parts.append(
                "¿Podría confirmarnos el SKU, la cantidad y el puerto de destino para cotizarle?"
            )
        if not is_chat:
            parts.append("Saludos cordiales,\nEquipo de Ventas de Exportacion")
        return "\n\n".join(parts)

    greeting = f"Hi {name}," if name else "Hi there,"
    parts = [greeting]
    if evidence_lines:
        parts.append(
            "Yes, we have this ready — here are the current details:\n"
            + "\n".join(evidence_lines)
        )
    if knowledge_lines:
        parts.append("\n".join(knowledge_lines))
    if evidence_lines:
        parts.append(
            "Which destination port and certifications do you need? "
            "Happy to prepare a formal PI for you."
        )
    else:
        parts.append(
            "Could you share the target quantity and destination port "
            "so I can prepare an exact quote and PI?"
        )
    if not is_chat:
        parts.append("Best regards,\nExport Sales Team")
    return "\n\n".join(parts)


def build_risk_hold_reply(*, language: str, customer_name: str) -> str:
    """Escalation template used when the risk guardrail forces L1 review (no figures)."""
    name = customer_name.strip() or "there"
    if language == "zh":
        return (
            f"您好 {name}，\n\n"
            "感谢您的来信。您的信息涉及退款、赔付、价格调整或付款/银行账户变更，"
            "按照公司政策须由人工专员复核确认后才能答复。我们的外贸销售团队会尽快与您"
            "确认具体处理方式。本消息不构成任何价格或赔付承诺。\n\n"
            "顺祝商祺，\n外贸销售团队"
        )
    if language == "es":
        return (
            f"Hola {name},\n\n"
            "Gracias por su mensaje. Su solicitud implica reembolsos, compensaciones, ajustes "
            "de precio o cambios de pago/cuenta bancaria, por lo que un especialista humano "
            "debe revisarla y aprobarla antes de cualquier respuesta. Nuestro equipo de ventas "
            "de exportacion le confirmara el procedimiento. Este mensaje no constituye ninguna "
            "promesa de precio ni compensacion.\n\n"
            "Saludos cordiales,\nEquipo de Ventas de Exportacion"
        )
    return (
        f"Hello {name},\n\n"
        "Thank you for your message. Because it involves refunds, compensation, price "
        "adjustments or payment/bank-account changes, company policy requires a human "
        "specialist to review and approve it before any commitment is made. A member of our "
        "export sales team will confirm the exact handling with you shortly. This message "
        "confirms no figures, prices or compensation.\n\n"
        "Best regards,\nExport Sales Team"
    )


def build_abstain_reply(*, language: str, customer_name: str) -> str:
    """Clarification template for the no-evidence path (asks questions, quotes nothing)."""
    name = customer_name.strip() or "there"
    if language == "zh":
        return (
            f"您好 {name}，\n\n"
            "感谢您的来信。为准确报价，请提供具体 SKU 或产品规格、目的港，以及目标数量与"
            "所需认证。我们在收到信息后立即提供正式报价与形式发票。\n\n"
            "顺祝商祺，\n外贸销售团队"
        )
    if language == "es":
        return (
            f"Hola {name},\n\n"
            "Gracias por su mensaje. Para cotizar con precision, indiquenos el SKU o las "
            "especificaciones, el puerto de destino, la cantidad objetivo y las certificaciones "
            "requeridas. Enviaremos la cotizacion y la Factura Proforma inmediatamente.\n\n"
            "Saludos cordiales,\nEquipo de Ventas de Exportacion"
        )
    return (
        f"Hello {name},\n\n"
        "Thank you for reaching out. To quote accurately, could you share the exact SKU or "
        "specification, your destination port, the target quantity and any required "
        "certifications? We will send the formal quotation and Proforma Invoice right away."
        "\n\nBest regards,\nExport Sales Team"
    )


def build_courtesy_reply(*, language: str, customer_name: str) -> str:
    """Short zero-claim acknowledgement for pure greetings or thank-you messages."""
    name = customer_name.strip()
    if language == "zh":
        prefix = f"{name}，" if name else ""
        return f"{prefix}不客气！随时告诉我需要查什么型号的库存、报价或出 PI，我一直在。"
    if language == "es":
        prefix = f"{name}, " if name else ""
        return f"¡Con gusto, {prefix}avísame cuando necesites precios, stock o una PI!"
    prefix = f", {name}" if name else ""
    return f"You're welcome{prefix}! Let me know anytime if you need a quote, stock check, or a PI."


@dataclass(frozen=True, slots=True)
class AutoReplyDispatchBundle:
    """Outbound stream payload + SSE event bundle emitted after transaction commit."""

    tenant_id: UUID
    outbound_payload: dict[str, Any] | None = None
    sse_event: dict[str, Any] | None = None


async def publish_auto_reply_dispatch(redis: Any, bundle: AutoReplyDispatchBundle | None) -> None:
    """Enqueue outbound channel delivery and publish real-time inbox SSE notifications."""
    if redis is None or bundle is None:
        return
    if bundle.outbound_payload is not None:
        await redis.xadd(
            "stream:outbound:messages",
            {"payload": json.dumps(bundle.outbound_payload, ensure_ascii=False, default=str)},
        )
    if bundle.sse_event is not None:
        await redis.publish(
            f"inbox:events:{bundle.tenant_id}",
            json.dumps(bundle.sse_event, ensure_ascii=False, default=str),
        )


# --------------------------------------------------------------------------------------
# Deterministic customer profiling
# --------------------------------------------------------------------------------------


def _matched_labels(text_value: str, patterns: Sequence[tuple[str, re.Pattern[str]]]) -> list[str]:
    return [label for label, pattern in patterns if pattern.search(text_value)]


def _buyer_type(customer: ProfileCustomer, transcript: str) -> BuyerType:
    if _DISTRIBUTOR.search(transcript):
        return "b2b_distributor"
    if customer.company_name or customer.customer_type.startswith("b2b"):
        return "b2b_buyer"
    if customer.customer_type == "b2c_consumer":
        return "b2c_consumer"
    return "unknown"


def _intent_score(
    *,
    transcript: str,
    skus: Sequence[str],
    orders: Sequence[ProfileOrder],
    quotes: Sequence[ProfileQuote],
    inbound_count: int,
    requirements: Sequence[str],
) -> int:
    score = 5
    if skus:
        score += 25
    if _QUANTITY.search(transcript):
        score += 20
    if _PRICE_INTENT.search(transcript):
        score += 15
    if extract_incoterm(transcript) or _PORT_HINT.search(transcript):
        score += 10
    if requirements:
        score += 5
    if orders or quotes:
        score += 10
    if inbound_count >= 3:
        score += 5
    return max(0, min(100, score))


def _price_sensitivity(transcript: str) -> PriceSensitivity:
    if _PRICE_PRESSURE.search(transcript):
        return "high"
    if re.search(r"\bdiscount\b|折扣|优惠|best\s+price|最低价", transcript, re.I):
        return "high"
    if _PRICE_INTENT.search(transcript):
        return "medium"
    return "low"


def _next_best_action(*, score: int, risk_flags: Sequence[str], skus: Sequence[str]) -> str:
    if risk_flags:
        return (
            "Human owner must review the refund/compensation/payment-sensitive request before "
            "sending any reply or commitment."
        )
    if score >= 70:
        return (
            "Send a grounded PI with the MOQ-tier price and availability for the requested SKUs "
            "and confirm the destination port today."
        )
    if score >= 40:
        return (
            "Follow up with the exact SKU specification, quantity tier and certification list, "
            "and confirm the target delivery window."
        )
    if skus:
        return (
            "Confirm the requested SKU specifications and target quantity, then offer a grounded "
            "quotation."
        )
    return "Ask for the exact SKU, target quantity and destination port in the next touch."


def _summary(
    *,
    customer: ProfileCustomer,
    buyer_type: BuyerType,
    country_code: str | None,
    skus: Sequence[str],
    score: int,
    order_value: Decimal | None,
    language: str,
) -> str:
    who = customer.company_name or customer.display_name or str(customer.customer_id)
    country = country_code or "an unconfirmed country"
    sku_part = ", ".join(skus) if skus else "no specific SKU yet"
    value_part = (
        f"Historic USD order value {order_value} across on-record orders."
        if order_value is not None
        else "No USD order history on record."
    )
    return (
        f"{who} from {country} is a {buyer_type.replace('_', ' ')} working in {language}. "
        f"Interest: {sku_part}. Purchase intent score {score}/100. {value_part}"
    )


def extract_profile_from_sources(
    sources: ProfileSources, *, now: datetime
) -> CustomerAiProfileRead:
    """Deterministically derive the structured AI profile (pure, no I/O)."""
    customer = sources.customer
    inbound_texts = [
        message.body_text
        for message in sources.messages
        if message.direction == "inbound" and message.body_text.strip()
    ]
    transcript = "\n".join(inbound_texts)
    all_text = "\n".join(message.body_text for message in sources.messages)

    skus = extract_sku_tokens(all_text)
    buyer_type = _buyer_type(customer, all_text)
    country_code = (
        normalize_country_code(customer.country_code)
        or normalize_country_code(customer.company_country_code)
        or infer_country_code(all_text)
    )
    language = detect_reply_language(transcript) if transcript.strip() else ""
    preferred_language = language or customer.preferred_language or "en"
    requirements = _matched_labels(transcript, _REQUIREMENT_PATTERNS)
    pain_points = _matched_labels(transcript, _PAIN_PATTERNS)
    risk_flags = detect_risk_flags(transcript)
    score = _intent_score(
        transcript=transcript,
        skus=skus,
        orders=sources.orders,
        quotes=sources.quotes,
        inbound_count=len(inbound_texts),
        requirements=requirements,
    )
    sensitivity = _price_sensitivity(transcript)
    order_value = usd_order_total(sources.orders)
    target_incoterm = extract_incoterm(transcript)
    if target_incoterm is None:
        for quote in sources.quotes:
            if quote.incoterm and quote.incoterm.strip():
                target_incoterm = quote.incoterm.strip().upper()
                break
    tags = ["ai-profile", buyer_type]
    if score >= 70:
        tags.append("intent-high")
    elif score >= 40:
        tags.append("intent-medium")
    else:
        tags.append("intent-low")
    tags.append(f"price-sensitivity-{sensitivity}")
    if country_code:
        tags.append(f"country-{country_code.lower()}")
    if risk_flags:
        tags.append("risk-review")
    if skus:
        tags.append("sku-specific")

    return CustomerAiProfileRead(
        customer_id=customer.customer_id,
        buyer_type=buyer_type,
        company_name=customer.company_name,
        country_code=country_code,
        preferred_language=preferred_language,
        interested_skus=skus,
        estimated_order_value_usd=order_value,
        target_incoterm=target_incoterm,
        purchase_intent_score=score,
        price_sensitivity=sensitivity,
        key_requirements=requirements,
        pain_points=pain_points,
        next_best_action=_next_best_action(score=score, risk_flags=risk_flags, skus=skus),
        ai_summary=_summary(
            customer=customer,
            buyer_type=buyer_type,
            country_code=country_code,
            skus=skus,
            score=score,
            order_value=order_value,
            language=preferred_language,
        ),
        tags=tags,
        updated_at=now,
    )


def default_profile(customer_id: UUID, *, now: datetime) -> CustomerAiProfileRead:
    """Empty-but-valid structure returned before the first profiling run."""
    return CustomerAiProfileRead(customer_id=customer_id, updated_at=now)


def profile_to_json(profile: CustomerAiProfileRead) -> dict[str, Any]:
    """Serialize with money as an exact decimal string (never an IEEE-754 float)."""
    return {
        "customer_id": str(profile.customer_id),
        "buyer_type": profile.buyer_type,
        "company_name": profile.company_name,
        "country_code": profile.country_code,
        "preferred_language": profile.preferred_language,
        "interested_skus": list(profile.interested_skus),
        "estimated_order_value_usd": (
            str(profile.estimated_order_value_usd)
            if profile.estimated_order_value_usd is not None
            else None
        ),
        "target_incoterm": profile.target_incoterm,
        "purchase_intent_score": profile.purchase_intent_score,
        "price_sensitivity": profile.price_sensitivity,
        "key_requirements": list(profile.key_requirements),
        "pain_points": list(profile.pain_points),
        "next_best_action": profile.next_best_action,
        "ai_summary": profile.ai_summary,
        "tags": list(profile.tags),
        "updated_at": profile.updated_at.isoformat(),
    }


# --------------------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------------------


def _conversation_not_found(conversation_id: UUID) -> AppError:
    return AppError(
        slug="conversation-not-found",
        title="Conversation Not Found",
        status_code=404,
        code="ERR_CONVERSATION_NOT_FOUND",
        detail=f"Conversation '{conversation_id}' not found.",
    )


def _customer_not_found(customer_id: UUID) -> AppError:
    return AppError(
        slug="customer-not-found",
        title="Customer Not Found",
        status_code=404,
        code="ERR_CUSTOMER_NOT_FOUND",
        detail=f"Customer '{customer_id}' not found.",
    )


class AutoReplyService:
    """Grounded auto-reply execution, risk guardrails, and AI customer profiling."""

    def __init__(
        self,
        tool_registry: ErpToolRegistry | None = None,
        rag_service: KbSearchPort | None = None,
        llm_client: LlmClientPort | None = None,
        *,
        enable_llm_polish: bool = True,
    ) -> None:
        self.tools = tool_registry or ErpToolRegistry()
        self.rag = rag_service
        self._llm_client = llm_client
        self._enable_llm_polish = enable_llm_polish
        self._scope_service = AiProposalGeneratorService(tool_registry=self.tools)

    # ------------------------------------------------------------- scope / security

    async def load_conversation(
        self, session: AsyncSession, tenant_id: UUID, conversation_id: UUID
    ) -> ConversationContext:
        return await self._scope_service.load_conversation(session, tenant_id, conversation_id)

    def assert_visibility(
        self, principal: CurrentPrincipal, conversation: ConversationContext
    ) -> None:
        """Reuse the exact AGENT mailbox isolation rule of the proposal generator."""
        self._scope_service.assert_visibility(principal, conversation)

    async def assert_customer_visibility(
        self, session: AsyncSession, principal: CurrentPrincipal, customer_id: UUID
    ) -> None:
        """Apply the same AGENT personal-mailbox isolation to a customer-level read.

        Mirrors ``identity.router``'s customer visibility rule (and
        ``InboxService.agent_can_access_conversation``) with plain tenant-scoped SQL so no
        cross-domain import is introduced: an AGENT may read a customer when at least one of
        its conversations is workable by them, or when the customer has no conversations at
        all. Other tenant roles are unaffected.
        """
        if principal.role != UserRole.AGENT:
            return
        visible = await session.scalar(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM customers c
                    WHERE c.tenant_id = :tid
                      AND c.id = :cid
                      AND (
                            EXISTS (
                                SELECT 1
                                FROM conversations conv
                                JOIN channel_accounts ca
                                  ON ca.id = conv.channel_account_id
                                 AND ca.tenant_id = conv.tenant_id
                                WHERE conv.tenant_id = :tid
                                  AND conv.customer_id = c.id
                                  AND (
                                        conv.assigned_user_id = :uid
                                        OR (
                                            conv.assigned_user_id IS NULL
                                            AND ca.visibility_scope = 'shared'
                                            AND ca.owner_user_id IS NULL
                                        )
                                      )
                            )
                            OR NOT EXISTS (
                                SELECT 1
                                FROM conversations any_conv
                                WHERE any_conv.tenant_id = :tid
                                  AND any_conv.customer_id = c.id
                            )
                          )
                )
                """
            ),
            {"tid": principal.tenant_id, "cid": customer_id, "uid": principal.user_id},
        )
        if not visible:
            raise AppError(
                slug="conversation-access-denied",
                title="Conversation Access Denied",
                status_code=403,
                code="ERR_CONVERSATION_ACCESS_DENIED",
                detail="This customer belongs to another agent's private mailbox.",
            )

    # ------------------------------------------------------------------- setting I/O

    async def _load_auto_reply_flag(
        self, session: AsyncSession, tenant_id: UUID, conversation_id: UUID
    ) -> bool:
        value = await session.scalar(
            text(
                "SELECT ai_auto_reply_enabled FROM conversations "
                "WHERE tenant_id = :tid AND id = :cid"
            ),
            {"tid": tenant_id, "cid": conversation_id},
        )
        if value is None:
            raise _conversation_not_found(conversation_id)
        return bool(value)

    async def _recent_messages(
        self, session: AsyncSession, tenant_id: UUID, conversation_id: UUID
    ) -> list[RecentMessage]:
        return await self._scope_service._recent_messages(session, tenant_id, conversation_id)

    async def _customer_summary(
        self, session: AsyncSession, tenant_id: UUID, customer_id: UUID
    ) -> dict[str, str]:
        return await self._scope_service._customer_summary(session, tenant_id, customer_id)

    async def _collect_tool_evidence(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        customer_id: UUID,
        message_text: str,
    ) -> list[dict[str, Any]]:
        return await self._scope_service._collect_evidence(
            session, tenant_id, customer_id, message_text
        )

    async def get_setting(
        self, session: AsyncSession, principal: CurrentPrincipal, conversation_id: UUID
    ) -> AutoReplySettingRead:
        conversation = await self.load_conversation(session, principal.tenant_id, conversation_id)
        self.assert_visibility(principal, conversation)
        enabled = await self._load_auto_reply_flag(session, principal.tenant_id, conversation_id)
        return AutoReplySettingRead(conversation_id=conversation.id, ai_auto_reply_enabled=enabled)

    async def update_setting(
        self,
        session: AsyncSession,
        principal: CurrentPrincipal,
        conversation_id: UUID,
        enabled: bool,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> AutoReplySettingRead:
        conversation = await self.load_conversation(session, principal.tenant_id, conversation_id)
        self.assert_visibility(principal, conversation)
        before = await self._load_auto_reply_flag(session, principal.tenant_id, conversation_id)
        updated = await session.scalar(
            text(
                "UPDATE conversations SET ai_auto_reply_enabled = :enabled, updated_at = now() "
                "WHERE tenant_id = :tid AND id = :cid RETURNING ai_auto_reply_enabled"
            ),
            {"enabled": enabled, "tid": principal.tenant_id, "cid": conversation_id},
        )
        if updated is None:
            raise _conversation_not_found(conversation_id)
        after = bool(updated)
        if after != before:
            await record_audit_event(
                session=session,
                tenant_id=principal.tenant_id,
                actor_type="human_user",
                actor_id=str(principal.user_id),
                action="ai.auto_reply.updated",
                resource_type="conversation",
                resource_id=conversation_id,
                before_state={"ai_auto_reply_enabled": before},
                after_state={"ai_auto_reply_enabled": after},
                ip_address=ip_address,
                user_agent=user_agent,
            )
        return AutoReplySettingRead(conversation_id=conversation_id, ai_auto_reply_enabled=after)

    # ------------------------------------------------------------------ knowledge base

    async def _search_kb(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        query: str,
        top_k: int,
    ) -> list[dict[str, Any]]:
        """Tenant-scoped keyword retrieval over ``kb_documents``/``kb_chunks``.

        Returns an empty list when the tenant has no active documents (or the optional
        embedding-backed :class:`HybridRagService` is unavailable) instead of raising.
        """
        query_text = query.strip()
        if not query_text:
            return []
        if self.rag is not None:
            try:
                result = await self.rag.search(session, tenant_id, query_text, top_k=top_k)
            except (RuntimeError, ValueError, TypeError, SQLAlchemyError):
                return []
            hits: list[dict[str, Any]] = []
            for hit in getattr(result, "hits", []) or []:
                hits.append(
                    {
                        "chunk_id": str(getattr(hit, "chunk_id", "")),
                        "doc_id": str(getattr(hit, "doc_id", "")),
                        "doc_title": str(getattr(hit, "doc_title", "")),
                        "snippet": str(getattr(hit, "content", ""))[:_MAX_KB_SNIPPET],
                        "score": float(getattr(hit, "rerank_score", 0.0)),
                    }
                )
            return hits[:top_k]

        ts_query = build_websearch_tsquery(query_text)
        if ts_query is None:
            return []
        try:
            async with session.begin_nested():
                rows = (
                    (
                        await session.execute(
                            text(
                                """
                                SELECT c.id AS chunk_id,
                                       c.document_id AS doc_id,
                                       d.title AS doc_title,
                                       c.content,
                                       c.has_dynamic_price_disclaimer
                                FROM kb_chunks c
                                JOIN kb_documents d
                                  ON d.tenant_id = c.tenant_id AND d.id = c.document_id
                                WHERE c.tenant_id = :tid
                                  AND d.is_active
                                  AND c.tsv @@ websearch_to_tsquery('simple', :ws_query)
                                ORDER BY ts_rank(c.tsv, websearch_to_tsquery('simple', :ws_query))
                                         DESC, c.id
                                LIMIT :limit
                                """
                            ),
                            {"tid": tenant_id, "ws_query": ts_query, "limit": top_k},
                        )
                    )
                    .mappings()
                    .all()
                )
        except SQLAlchemyError:
            return []

        hits = []
        for row in rows:
            content = str(row["content"])
            if bool(row["has_dynamic_price_disclaimer"]):
                content = strip_dynamic_prices_for_answer(content)
            hits.append(
                {
                    "chunk_id": str(row["chunk_id"]),
                    "doc_id": str(row["doc_id"]),
                    "doc_title": str(row["doc_title"]),
                    "snippet": content[:_MAX_KB_SNIPPET],
                    "score": 1.0,
                }
            )
        return hits

    # ------------------------------------------------------------------ write helpers

    async def _insert_outbound_message(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        conversation: ConversationContext,
        body_text: str,
        metadata: dict[str, Any],
    ) -> UUID:
        message_id = uuid4()
        await session.execute(
            text(
                """
                INSERT INTO messages (
                    id, tenant_id, conversation_id, customer_id, channel_type,
                    direction, sender_type, content_type, body_text,
                    channel_metadata, delivery_status, source_timestamp
                ) VALUES (
                    :id, :tid, :cid, :cust, :channel,
                    'outbound', 'ai_agent', 'text', :body,
                    CAST(:meta AS jsonb), 'queued', now()
                )
                """
            ),
            {
                "id": message_id,
                "tid": tenant_id,
                "cid": conversation.id,
                "cust": conversation.customer_id,
                "channel": conversation.channel_type,
                "body": body_text,
                "meta": json.dumps(metadata, ensure_ascii=False, default=str),
            },
        )
        await session.execute(
            text(
                "UPDATE conversations SET last_outbound_at = now(), updated_at = now(), "
                "last_message_preview = left(:preview, 280) "
                "WHERE tenant_id = :tid AND id = :cid"
            ),
            {"preview": body_text, "tid": tenant_id, "cid": conversation.id},
        )
        return message_id

    async def _find_existing_reply_for_inbound(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        conversation_id: UUID,
        source_message_id: UUID | None,
    ) -> tuple[UUID, str] | None:
        """Return the outbound reply already produced for this inbound turn, if any.

        Idempotency anchor: the outbound message stores ``in_reply_to_message_id`` inside
        ``channel_metadata``, so a redelivered webhook (or a retried job) replays the stored
        reply instead of sending a duplicate. Stub services that override the I/O seams may
        leave ``session`` inert: any SQL failure simply means "no reply found yet".
        """
        if source_message_id is None:
            return None
        try:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT id, COALESCE(body_text, '') AS body_text
                        FROM messages
                        WHERE tenant_id = :tid
                          AND conversation_id = :cid
                          AND direction = 'outbound'
                          AND channel_metadata->>'in_reply_to_message_id' = :smid
                        ORDER BY source_timestamp ASC, created_at ASC, id ASC
                        LIMIT 1
                        """
                    ),
                    {
                        "tid": tenant_id,
                        "cid": conversation_id,
                        "smid": str(source_message_id),
                    },
                )
            ).first()
        except Exception:  # noqa: BLE001 - stub sessions without SQL simply skip the check
            return None
        if row is None:
            return None
        try:
            return UUID(str(row[0])), str(row[1] or "")
        except (TypeError, ValueError):
            return None

    async def _latest_inbound_message_id(
        self, session: AsyncSession, tenant_id: UUID, conversation_id: UUID
    ) -> UUID | None:
        """Fallback turn id when the caller did not supply one (never fatal)."""
        try:
            value = (
                await session.execute(
                    text(
                        """
                        SELECT id
                        FROM messages
                        WHERE tenant_id = :tid
                          AND conversation_id = :cid
                          AND direction = 'inbound'
                        ORDER BY source_timestamp DESC, created_at DESC, id DESC
                        LIMIT 1
                        """
                    ),
                    {"tid": tenant_id, "cid": conversation_id},
                )
            ).first()
        except Exception:  # noqa: BLE001 - stub sessions are allowed to have no storage
            return None
        if value is None:
            return None
        try:
            return UUID(str(value[0]))
        except (TypeError, ValueError):
            return None

    async def _create_proposal(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        conversation: ConversationContext,
        payload: dict[str, Any],
        confidence: Decimal,
        citations: list[dict[str, Any]],
        source_tool_call_id: UUID | None,
    ) -> UUID:
        proposal_id = uuid4()
        proposal = AiProposal(
            id=proposal_id,
            tenant_id=tenant_id,
            capability="cs_support",
            proposal_type="reply_draft",
            conversation_id=conversation.id,
            source_tool_call_id=source_tool_call_id,
            proposed_payload=payload,
            citations=citations,
            confidence_score=confidence,
            status="pending",
        )
        session.add(proposal)
        await session.flush()
        return proposal_id

    async def _record_audit(
        self,
        session: AsyncSession,
        principal: CurrentPrincipal,
        action: str,
        resource_id: UUID,
        *,
        after_state: dict[str, Any],
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        await record_audit_event(
            session=session,
            tenant_id=principal.tenant_id,
            actor_type="ai_agent",
            actor_id="ai_auto_reply",
            action=action,
            resource_type="conversation",
            resource_id=resource_id,
            after_state=after_state,
            ip_address=ip_address,
            user_agent=user_agent,
        )

    async def _build_outbound_dispatch(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        conversation: ConversationContext,
        message_id: UUID,
        body_text: str,
        metadata: dict[str, Any],
    ) -> AutoReplyDispatchBundle | None:
        """Build the Redis outbound-stream + SSE notification bundle for an auto-sent reply."""
        row = (
            (
                await session.execute(
                    text(
                        """
                        SELECT c.channel_account_id,
                               c.external_thread_id,
                               c.assigned_user_id,
                               c.subject,
                               a.external_account_id,
                               a.visibility_scope
                        FROM conversations c
                        JOIN channel_accounts a
                          ON a.tenant_id = c.tenant_id AND a.id = c.channel_account_id
                        WHERE c.tenant_id = :tid AND c.id = :cid
                        """
                    ),
                    {"tid": tenant_id, "cid": conversation.id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None

        recipient_id = str(row["external_thread_id"] or "").strip()
        if not recipient_id:
            ident = (
                await session.execute(
                    text(
                        """
                        SELECT identity_value
                        FROM customer_identities
                        WHERE tenant_id = :tid AND customer_id = :cust AND is_active = true
                        ORDER BY CASE identity_type
                            WHEN 'phone_e164' THEN 1
                            WHEN 'email' THEN 2
                            ELSE 3
                        END, created_at ASC
                        LIMIT 1
                        """
                    ),
                    {"tid": tenant_id, "cust": conversation.customer_id},
                )
            ).scalar_one_or_none()
            recipient_id = str(ident or "").strip()

        outbound_payload: dict[str, Any] | None = None
        if recipient_id:
            outbound_payload = {
                "tenant_id": str(tenant_id),
                "channel_account_id": str(row["channel_account_id"]),
                "channel_type": conversation.channel_type,
                "external_account_id": str(row["external_account_id"]),
                "recipient_id": recipient_id,
                "conversation_id": str(conversation.id),
                "message_id": str(message_id),
                "content_type": "text",
                "body_text": body_text,
                "subject": row["subject"],
                "metadata": metadata,
            }

        sse_event = {
            "event": "message.created",
            "conversation_id": str(conversation.id),
            "message_id": str(message_id),
            "direction": "outbound",
            "channel_type": conversation.channel_type,
            "assigned_user_id": (
                str(row["assigned_user_id"]) if row["assigned_user_id"] is not None else None
            ),
            "visibility_scope": str(row["visibility_scope"] or "shared"),
        }
        return AutoReplyDispatchBundle(
            tenant_id=tenant_id,
            outbound_payload=outbound_payload,
            sse_event=sse_event,
        )

    async def _build_proposal_dispatch(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        conversation: ConversationContext,
        proposal_id: UUID,
    ) -> AutoReplyDispatchBundle:
        row = (
            (
                await session.execute(
                    text(
                        """
                        SELECT c.assigned_user_id, a.visibility_scope
                        FROM conversations c
                        JOIN channel_accounts a
                          ON a.tenant_id = c.tenant_id AND a.id = c.channel_account_id
                        WHERE c.tenant_id = :tid AND c.id = :cid
                        """
                    ),
                    {"tid": tenant_id, "cid": conversation.id},
                )
            )
            .mappings()
            .one_or_none()
        )
        return AutoReplyDispatchBundle(
            tenant_id=tenant_id,
            outbound_payload=None,
            sse_event={
                "event": "proposal.created",
                "conversation_id": str(conversation.id),
                "proposal_id": str(proposal_id),
                "assigned_user_id": (
                    str(row["assigned_user_id"])
                    if row is not None and row["assigned_user_id"] is not None
                    else None
                ),
                "visibility_scope": (
                    str(row["visibility_scope"])
                    if row is not None and row["visibility_scope"]
                    else "shared"
                ),
            },
        )

    async def _polish_reply_with_llm(
        self,
        *,
        language: str,
        channel_type: str,
        customer_name: str,
        buyer_message: str,
        recent_messages: Sequence[RecentMessage],
        evidence: Sequence[dict[str, Any]],
        kb_hits: Sequence[dict[str, Any]],
    ) -> str | None:
        """Render verified ERP/KB facts in warm, natural human sales tone with numeric guard."""
        if not self._enable_llm_polish:
            return None
        if os.environ.get("AUTO_REPLY_LLM_POLISH", "true").strip().lower() != "true":
            return None
        if self._llm_client is None and not (
            os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")
        ):
            return None
        client = self._llm_client or AsyncMultiProviderClient()
        if client.has_key("deepseek"):
            provider, model_id = "deepseek", MODEL_CATALOG["deepseek-flash"].model_id
        elif client.has_key("openai"):
            provider, model_id = "openai", MODEL_CATALOG["gpt-5-mini"].model_id
        else:
            return None

        is_chat = channel_type in _CHAT_CHANNELS
        style_hint = (
            "Write like a real, friendly, experienced B2B export sales rep on Telegram/WhatsApp: "
            "2-4 concise natural sentences, conversational and direct. Do NOT use formal email "
            "sign-offs like 'Best regards, Export Sales Team', and NEVER write robotic headings "
            "like 'Verified live information:', 'Knowledge base:', or '- SKU ...:'."
            if is_chat
            else "Write a crisp, natural, human B2B export sales email. Avoid robotic headings "
            "like 'Verified live information:' or 'Knowledge base:'."
        )
        system_prompt = (
            "You are a senior export sales specialist for a B2B LED lighting factory.\n"
            f"{style_hint}\n"
            "STRICT FACTUAL RULES:\n"
            "1. Only mention prices, MOQ tiers, stock quantities, lead times, tracking numbers, or "
            "product specs that appear verbatim in VERIFIED_ERP_FACTS or KB_FACTS.\n"
            "2. NEVER invent, estimate, round, or extrapolate ANY number, discount, or date.\n"
            "3. Reply in the buyer's language (en / zh / es) and directly answer what they asked.\n"
            "4. Keep it under 110 words, plain text only (no markdown bold/tables/bullet dumps), "
            "and end with one natural helpful follow-up question.\n"
            "5. Output ONLY the message body."
        )
        history_lines = [
            f"{'Buyer' if m.direction == 'inbound' else 'Sales'}: {m.body_text.strip()}"
            for m in list(recent_messages)[-5:]
            if m.body_text.strip()
        ]
        erp_json = json.dumps(list(evidence), ensure_ascii=False, default=str)
        kb_json = json.dumps(list(kb_hits), ensure_ascii=False, default=str)
        user_prompt = (
            f"Buyer name: {customer_name or 'Customer'}\n"
            f"Target language: {language}\n"
            f"Channel: {channel_type}\n\n"
            f"RECENT_CHAT:\n" + ("\n".join(history_lines) or buyer_message) + "\n\n"
            f"VERIFIED_ERP_FACTS:\n{erp_json}\n\n"
            f"KB_FACTS:\n{kb_json}"
        )
        try:
            resp = await client.complete(
                model_id=model_id,
                provider=provider,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=320,
                temperature=0.35,
            )
        except Exception:  # noqa: BLE001 - deterministic fallback guarantees delivery
            return None

        candidate = (resp.text or "").strip()
        if not verify_reply_numbers_grounded(
            candidate,
            evidence=evidence,
            kb_hits=kb_hits,
            buyer_message=buyer_message,
        ):
            return None
        return candidate

    # ---------------------------------------------------------------------- execution

    async def run_with_dispatch(
        self,
        session: AsyncSession,
        principal: CurrentPrincipal,
        conversation_id: UUID,
        request: AutoReplyRunRequest,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
        prepare_dispatch: bool = True,
        source_message_id: UUID | None = None,
    ) -> tuple[AutoReplyRunResponse, AutoReplyDispatchBundle | None]:
        """Execute one guarded auto-reply attempt and optionally prepare its Redis dispatch.

        ``source_message_id`` makes the attempt idempotent per inbound turn: when a reply was
        already stored for that inbound message, the stored reply is returned with
        ``dispatch_bundle=None`` so nothing is re-sent or re-published.
        """
        conversation = await self.load_conversation(session, principal.tenant_id, conversation_id)
        self.assert_visibility(principal, conversation)

        if source_message_id is not None:
            existing = await self._find_existing_reply_for_inbound(
                session, principal.tenant_id, conversation_id, source_message_id
            )
            if existing is not None:
                existing_id, existing_body = existing
                return (
                    AutoReplyRunResponse(
                        conversation_id=conversation.id,
                        action="auto_replied",
                        message_id=existing_id,
                        reply_text=existing_body,
                        confidence=compute_confidence((), (), ()),
                    ),
                    None,
                )

        enabled = await self._load_auto_reply_flag(session, principal.tenant_id, conversation_id)

        messages = await self._recent_messages(session, principal.tenant_id, conversation_id)
        inbound = [message for message in messages if message.direction == "inbound"]
        latest_inbound = inbound[-1] if inbound else None
        message_text = (latest_inbound.body_text if latest_inbound else "") or ""

        risk_flags = detect_risk_flags(message_text)
        kb_hits = await self._search_kb(
            session, principal.tenant_id, message_text, request.max_kb_hits
        )
        evidence = await self._collect_tool_evidence(
            session, principal.tenant_id, conversation.customer_id, message_text
        )
        customer = await self._customer_summary(
            session, principal.tenant_id, conversation.customer_id
        )

        courtesy = is_courtesy_message(message_text)
        decision = decide_auto_reply(
            mode=request.mode,
            auto_reply_enabled=enabled,
            has_evidence=bool(evidence),
            has_kb=bool(kb_hits),
            risk_flags=risk_flags,
            is_courtesy=courtesy,
        )
        language = detect_reply_language(message_text)
        customer_name = customer.get("name") or customer.get("company") or ""
        confidence = (
            Decimal("0.850")
            if decision.reason == "courtesy_auto_reply"
            else compute_confidence(evidence, kb_hits, risk_flags)
        )

        if decision.action == "abstained":
            return (
                AutoReplyRunResponse(
                    conversation_id=conversation.id,
                    action="abstained",
                    reply_text=build_abstain_reply(language=language, customer_name=customer_name),
                    kb_hits=kb_hits,
                    tool_evidence=evidence,
                    risk_flags=risk_flags,
                    confidence=confidence,
                ),
                None,
            )

        llm_polished = False
        if risk_flags:
            reply_text = build_risk_hold_reply(language=language, customer_name=customer_name)
        elif decision.reason == "courtesy_auto_reply":
            reply_text = build_courtesy_reply(language=language, customer_name=customer_name)
        else:
            polished = await self._polish_reply_with_llm(
                language=language,
                channel_type=conversation.channel_type,
                customer_name=customer_name,
                buyer_message=message_text,
                recent_messages=messages,
                evidence=evidence,
                kb_hits=kb_hits,
            )
            if polished is not None:
                reply_text = polished
                llm_polished = True
            else:
                reply_text = build_grounded_reply(
                    language=language,
                    customer_name=customer_name,
                    evidence=evidence,
                    kb_hits=kb_hits,
                    channel_type=conversation.channel_type,
                )

        kb_citations = [
            {
                "doc_id": str(hit.get("doc_id", "")),
                "chunk_id": str(hit.get("chunk_id", "")),
                "doc_title": str(hit.get("doc_title", "")),
            }
            for hit in kb_hits
        ]
        citations: list[dict[str, Any]] = [
            {
                "source_tool_call_id": str(item.get("tool_call_id", "")),
                "tool_name": str(item.get("tool_name", "")),
            }
            for item in evidence
        ] + kb_citations
        primary_tool_call_id: UUID | None = None
        if evidence:
            raw_trace = str(evidence[0].get("tool_call_id", "")).removeprefix("tc_")
            try:
                primary_tool_call_id = UUID(raw_trace)
            except ValueError:
                primary_tool_call_id = None

        if decision.action == "auto_replied":
            metadata: dict[str, Any] = {
                "ai_auto_reply": True,
                "llm_polished": llm_polished,
                "guardrail_decision": decision.reason,
                "grounded_evidence": evidence,
                "kb_citations": kb_citations,
                "risk_flags": risk_flags,
                "confidence": str(confidence),
            }
            in_reply_to_message_id = source_message_id
            if in_reply_to_message_id is None:
                in_reply_to_message_id = await self._latest_inbound_message_id(
                    session, principal.tenant_id, conversation.id
                )
            if in_reply_to_message_id is not None:
                metadata["in_reply_to_message_id"] = str(in_reply_to_message_id)
            message_id = await self._insert_outbound_message(
                session,
                tenant_id=principal.tenant_id,
                conversation=conversation,
                body_text=reply_text,
                metadata=metadata,
            )
            await self._record_audit(
                session,
                principal,
                "ai.auto_reply.sent",
                conversation.id,
                after_state={
                    "message_id": str(message_id),
                    "confidence": str(confidence),
                    "verified_tool_call_ids": [
                        str(item.get("tool_call_id", "")) for item in evidence
                    ],
                    "kb_chunk_ids": [str(hit.get("chunk_id", "")) for hit in kb_hits],
                },
                ip_address=ip_address,
                user_agent=user_agent,
            )
            dispatch_bundle = (
                await self._build_outbound_dispatch(
                    session,
                    tenant_id=principal.tenant_id,
                    conversation=conversation,
                    message_id=message_id,
                    body_text=reply_text,
                    metadata=metadata,
                )
                if prepare_dispatch
                else None
            )
            return (
                AutoReplyRunResponse(
                    conversation_id=conversation.id,
                    action="auto_replied",
                    message_id=message_id,
                    proposal_id=None,
                    reply_text=reply_text,
                    kb_hits=kb_hits,
                    tool_evidence=evidence,
                    risk_flags=risk_flags,
                    confidence=confidence,
                ),
                dispatch_bundle,
            )

        payload: dict[str, Any] = {
            "body_text": reply_text,
            "channel_type": conversation.channel_type,
            "customer_name": customer_name,
            "reasoning_summary": (
                "Risk guardrail matched "
                + ", ".join(risk_flags)
                + "; the reply is held for human approval and quotes no figures."
                if risk_flags
                else f"Grounded on {len(evidence)} verified ERP tool call(s) and "
                f"{len(kb_hits)} knowledge-base hit(s)."
            ),
            "verified_tool_call_ids": [str(item.get("tool_call_id", "")) for item in evidence],
            "tool_evidence": evidence,
            "kb_citations": kb_citations,
            "risk_flags": risk_flags,
            "guardrail_reason": decision.reason,
        }
        proposal_id = await self._create_proposal(
            session,
            tenant_id=principal.tenant_id,
            conversation=conversation,
            payload=payload,
            confidence=confidence,
            citations=citations,
            source_tool_call_id=primary_tool_call_id,
        )
        await self._record_audit(
            session,
            principal,
            "ai.auto_reply.proposal_created",
            conversation.id,
            after_state={
                "proposal_id": str(proposal_id),
                "guardrail_reason": decision.reason,
                "risk_flags": risk_flags,
            },
            ip_address=ip_address,
            user_agent=user_agent,
        )
        proposal_bundle = (
            await self._build_proposal_dispatch(
                session,
                tenant_id=principal.tenant_id,
                conversation=conversation,
                proposal_id=proposal_id,
            )
            if prepare_dispatch
            else None
        )
        return (
            AutoReplyRunResponse(
                conversation_id=conversation.id,
                action="proposal_created",
                message_id=None,
                proposal_id=proposal_id,
                reply_text=reply_text,
                kb_hits=kb_hits,
                tool_evidence=evidence,
                risk_flags=risk_flags,
                confidence=confidence,
            ),
            proposal_bundle,
        )

    async def run(
        self,
        session: AsyncSession,
        principal: CurrentPrincipal,
        conversation_id: UUID,
        request: AutoReplyRunRequest,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
        redis: Any = None,
        source_message_id: UUID | None = None,
    ) -> AutoReplyRunResponse:
        """Execute one guarded auto-reply attempt and optionally dispatch via Redis."""
        response, bundle = await self.run_with_dispatch(
            session,
            principal,
            conversation_id,
            request,
            ip_address=ip_address,
            user_agent=user_agent,
            prepare_dispatch=redis is not None,
            source_message_id=source_message_id,
        )
        if redis is not None and bundle is not None:
            await publish_auto_reply_dispatch(redis, bundle)
        return response

    # ---------------------------------------------------------------------- profiling

    async def _load_profile_sources(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        conversation_id: UUID,
        customer_id: UUID,
    ) -> ProfileSources:
        customer_row = (
            await session.execute(
                text(
                    """
                    SELECT c.id, c.display_name, c.customer_type, c.country_code,
                           c.preferred_language, bc.legal_name, bc.country_code
                               AS company_country_code
                    FROM customers c
                    LEFT JOIN b2b_companies bc
                      ON bc.id = c.company_id AND bc.tenant_id = c.tenant_id
                    WHERE c.tenant_id = :tid AND c.id = :cid
                    """
                ),
                {"tid": tenant_id, "cid": customer_id},
            )
        ).first()
        if customer_row is None:
            raise _customer_not_found(customer_id)

        message_rows = (
            (
                await session.execute(
                    text(
                        """
                        SELECT direction, sender_type, COALESCE(body_text, '') AS body_text
                        FROM messages
                        WHERE tenant_id = :tid AND conversation_id = :convo
                        ORDER BY source_timestamp ASC, created_at ASC, id ASC
                        LIMIT 200
                        """
                    ),
                    {"tid": tenant_id, "convo": conversation_id},
                )
            )
            .mappings()
            .all()
        )
        order_rows = (
            (
                await session.execute(
                    text(
                        """
                        SELECT order_no, total_amount, currency
                        FROM orders
                        WHERE tenant_id = :tid AND customer_id = :cid
                        ORDER BY created_at DESC, id DESC
                        LIMIT 50
                        """
                    ),
                    {"tid": tenant_id, "cid": customer_id},
                )
            )
            .mappings()
            .all()
        )

        quote_rows: Sequence[Any] = ()
        try:
            async with session.begin_nested():
                quote_rows = (
                    (
                        await session.execute(
                            text(
                                """
                                SELECT q.incoterm, q.total_amount, q.quote_currency
                                FROM quotes q
                                JOIN opportunities o
                                  ON o.tenant_id = q.tenant_id AND o.id = q.opportunity_id
                                WHERE q.tenant_id = :tid AND o.customer_id = :cid
                                ORDER BY q.created_at DESC, q.id DESC
                                LIMIT 20
                                """
                            ),
                            {"tid": tenant_id, "cid": customer_id},
                        )
                    )
                    .mappings()
                    .all()
                )
        except SQLAlchemyError:
            quote_rows = ()

        return ProfileSources(
            customer=ProfileCustomer(
                customer_id=customer_row[0],
                display_name=str(customer_row[1] or ""),
                customer_type=str(customer_row[2] or ""),
                country_code=str(customer_row[3]) if customer_row[3] else None,
                preferred_language=str(customer_row[4] or "en"),
                company_name=str(customer_row[5]) if customer_row[5] else None,
                company_country_code=str(customer_row[6]) if customer_row[6] else None,
            ),
            messages=tuple(
                ProfileMessage(
                    direction=str(row["direction"]),
                    sender_type=str(row["sender_type"]),
                    body_text=str(row["body_text"] or ""),
                )
                for row in message_rows
            ),
            orders=tuple(
                ProfileOrder(
                    order_no=str(row["order_no"]),
                    total_amount=Decimal(str(row["total_amount"])),
                    currency=str(row["currency"]),
                )
                for row in order_rows
            ),
            quotes=tuple(
                ProfileQuote(
                    incoterm=str(row["incoterm"]) if row["incoterm"] else None,
                    total_amount=Decimal(str(row["total_amount"])),
                    currency=str(row["quote_currency"]),
                )
                for row in quote_rows
            ),
        )

    async def _persist_profile(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        profile: CustomerAiProfileRead,
    ) -> None:
        payload = json.dumps(profile_to_json(profile), ensure_ascii=False)
        updated = await session.scalar(
            text(
                """
                UPDATE customers
                SET ai_profile = CAST(:profile AS jsonb),
                    ai_profile_updated_at = :ts,
                    country_code = CASE
                        WHEN country_code IS NULL OR btrim(country_code) = '' THEN :cc
                        ELSE country_code
                    END,
                    updated_at = now()
                WHERE tenant_id = :tid AND id = :cid
                RETURNING id
                """
            ),
            {
                "profile": payload,
                "ts": profile.updated_at,
                "cc": profile.country_code,
                "tid": tenant_id,
                "cid": profile.customer_id,
            },
        )
        if updated is None:
            raise _customer_not_found(profile.customer_id)

    async def _sync_crm_lead(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        profile: CustomerAiProfileRead,
    ) -> UUID:
        existing = await session.scalar(
            text(
                "SELECT id FROM leads WHERE tenant_id = :tid AND customer_id = :cid "
                "ORDER BY created_at DESC, id DESC LIMIT 1"
            ),
            {"tid": tenant_id, "cid": profile.customer_id},
        )
        if existing is not None:
            return UUID(str(existing))

        lead_id = uuid4()
        lead_type = "b2b_inquiry" if profile.buyer_type.startswith("b2b") else "b2c_vip_pre_sale"
        status = "qualified" if profile.purchase_intent_score >= 70 else "new"
        await session.execute(
            text(
                """
                INSERT INTO leads (
                    id, tenant_id, customer_id, source_channel, utm_campaign, lead_type,
                    qualification_score, extracted_requirements, status
                ) VALUES (
                    :id, :tid, :cid, 'ai_profile', 'ai_customer_profiling', :lead_type,
                    :score, CAST(:req AS jsonb), :status
                )
                """
            ),
            {
                "id": lead_id,
                "tid": tenant_id,
                "cid": profile.customer_id,
                "lead_type": lead_type,
                "score": profile.purchase_intent_score,
                "req": json.dumps({"ai_profile": profile_to_json(profile)}, ensure_ascii=False),
                "status": status,
            },
        )
        return lead_id

    async def _sync_crm_followup(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        profile: CustomerAiProfileRead,
        author_user_id: UUID,
        *,
        now: datetime,
    ) -> UUID:
        summary = (
            f"AI customer profile: {profile.ai_summary} | "
            f"Next best action: {profile.next_best_action}"
        )[:1000]
        next_action_at = (
            (now + timedelta(days=1)).replace(microsecond=0)
            if profile.purchase_intent_score >= 40
            else None
        )
        # One profile note per customer, rewritten in place: re-running profiling must
        # never spam the follow-up queue with duplicate "AI customer profile:" rows.
        existing_id = await session.scalar(
            text(
                """
                SELECT id FROM customer_followups
                WHERE tenant_id = :tid
                  AND customer_id = :cid
                  AND followup_type = 'note'
                  AND summary LIKE :summary_prefix
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """
            ),
            {
                "tid": tenant_id,
                "cid": profile.customer_id,
                "summary_prefix": "AI customer profile:%",
            },
        )
        if existing_id is not None:
            followup_id = UUID(str(existing_id))
            await session.execute(
                text(
                    """
                    UPDATE customer_followups
                    SET summary = :summary,
                        next_action_at = :next_action_at,
                        author_user_id = :author
                    WHERE tenant_id = :tid AND id = :id
                    """
                ),
                {
                    "id": followup_id,
                    "tid": tenant_id,
                    "author": author_user_id,
                    "summary": summary,
                    "next_action_at": next_action_at,
                },
            )
            return followup_id
        followup_id = uuid4()
        await session.execute(
            text(
                """
                INSERT INTO customer_followups (
                    id, tenant_id, customer_id, opportunity_id, author_user_id,
                    followup_type, summary, next_action_at
                ) VALUES (
                    :id, :tid, :cid, NULL, :author,
                    'note', :summary, :next_action_at
                )
                """
            ),
            {
                "id": followup_id,
                "tid": tenant_id,
                "cid": profile.customer_id,
                "author": author_user_id,
                "summary": summary,
                "next_action_at": next_action_at,
            },
        )
        return followup_id

    async def extract_profile(
        self,
        session: AsyncSession,
        principal: CurrentPrincipal,
        conversation_id: UUID,
        *,
        sync_to_crm: bool = True,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> CustomerAiProfileRead:
        conversation = await self.load_conversation(session, principal.tenant_id, conversation_id)
        self.assert_visibility(principal, conversation)
        sources = await self._load_profile_sources(
            session,
            principal.tenant_id,
            conversation_id,
            conversation.customer_id,
        )
        now = datetime.now(UTC)
        profile = extract_profile_from_sources(sources, now=now)
        await self._persist_profile(session, principal.tenant_id, profile)

        synced_lead_id: UUID | None = None
        synced_followup_id: UUID | None = None
        if sync_to_crm:
            synced_lead_id = await self._sync_crm_lead(session, principal.tenant_id, profile)
            synced_followup_id = await self._sync_crm_followup(
                session,
                principal.tenant_id,
                profile,
                principal.user_id,
                now=now,
            )
        await self._record_audit(
            session,
            principal,
            "ai.customer_profile.extracted",
            conversation.id,
            after_state={
                "customer_id": str(profile.customer_id),
                "purchase_intent_score": profile.purchase_intent_score,
                "buyer_type": profile.buyer_type,
                "synced_lead_id": str(synced_lead_id) if synced_lead_id else None,
                "synced_followup_id": (str(synced_followup_id) if synced_followup_id else None),
            },
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return profile.model_copy(
            update={
                "synced_lead_id": synced_lead_id,
                "synced_followup_id": synced_followup_id,
            }
        )

    async def read_profile(
        self,
        session: AsyncSession,
        principal: CurrentPrincipal,
        customer_id: UUID,
    ) -> CustomerAiProfileRead:
        """Return the stored AI profile, enforcing AGENT mailbox isolation first."""
        await self.assert_customer_visibility(session, principal, customer_id)
        row = (
            await session.execute(
                text(
                    """
                    SELECT ai_profile::text AS profile_json, ai_profile_updated_at
                    FROM customers
                    WHERE tenant_id = :tid AND id = :cid
                    """
                ),
                {"tid": principal.tenant_id, "cid": customer_id},
            )
        ).first()
        if row is None:
            raise _customer_not_found(customer_id)

        now = datetime.now(UTC)
        raw_profile: Any = None
        if row[0]:
            try:
                raw_profile = json.loads(str(row[0]), parse_float=Decimal)
            except (TypeError, ValueError):
                raw_profile = None
        if not isinstance(raw_profile, dict) or not raw_profile:
            return default_profile(customer_id, now=now)

        defaults = profile_to_json(default_profile(customer_id, now=now))
        merged = {**defaults, **raw_profile, "customer_id": str(customer_id)}
        if not isinstance(merged.get("updated_at"), str):
            merged["updated_at"] = (row[1] or now).isoformat()
        return CustomerAiProfileRead.model_validate(merged)


__all__ = [
    "AutoReplyDecision",
    "AutoReplyRunRequest",
    "AutoReplyRunResponse",
    "AutoReplyService",
    "AutoReplyDispatchBundle",
    "AutoReplySettingRead",
    "AutoReplySettingUpdate",
    "CustomerAiProfileRead",
    "ProfileCustomer",
    "ProfileExtractionRequest",
    "ProfileMessage",
    "ProfileOrder",
    "ProfileQuote",
    "ProfileSources",
    "build_abstain_reply",
    "build_courtesy_reply",
    "build_grounded_reply",
    "build_risk_hold_reply",
    "compute_confidence",
    "decide_auto_reply",
    "default_profile",
    "detect_reply_language",
    "detect_risk_flags",
    "evidence_summary_lines",
    "extract_incoterm",
    "extract_profile_from_sources",
    "extract_sku_tokens",
    "infer_country_code",
    "is_courtesy_message",
    "normalize_country_code",
    "profile_to_json",
    "publish_auto_reply_dispatch",
    "usd_order_total",
    "verify_reply_numbers_grounded",
]
