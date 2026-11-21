from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.channels.adapters.whatsapp import WhatsAppAdapter
from erp_core.channels.consent_service import ConsentAndSuppressionService
from erp_core.shared.enums import ChannelType, MessageContentType, MessageSenderType

MARKETPLACE_CHANNELS = frozenset(
    {
        ChannelType.TIKTOK_SHOP,
        ChannelType.AMAZON_SP,
        ChannelType.SHOPEE,
        ChannelType.LAZADA,
        ChannelType.MERCADO_LIBRE,
        ChannelType.EBAY,
        ChannelType.TEMU,
        ChannelType.WALMART,
    }
)
WHATSAPP_CHANNELS = frozenset({ChannelType.WHATSAPP_CLOUD, ChannelType.WHATSAPP_COEXISTENCE})
_FORBIDDEN_TAGS = {
    "HUMAN_AGENT",
    "CONFIRMED_EVENT_UPDATE",
    "ACCOUNT_UPDATE",
    "POST_PURCHASE_UPDATE",
}
# Real contact handles: never sanitized, always blocked on marketplace channels.
_OFF_PLATFORM = re.compile(
    r"\b(?:whats\s*app|we\s*chat|telegram)\b|wa\.me\b|t\.me/|"
    r"\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b|"
    r"https?\s*://|\b(?:[a-z0-9-]+\.)+(?:com|net|org|io|shop|me|cn|example)\b",
    re.IGNORECASE,
)
# Kept separate from the URL/handle patterns so benign business numbers can be stripped first.
_PHONE_CONTACT = re.compile(r"\+?\d(?:[ ().-]*\d){7,14}\b")
# Benign marketplace identifiers that must never be mistaken for a phone number.
_STRUCTURED_BUSINESS_IDS = re.compile(
    r"\b(?:ORD|DEMO|PI|SO|PO|TRK|AWB|SKU|INV|LED)-[A-Z0-9-]+\b", re.IGNORECASE
)
_ISO_DATES = re.compile(r"\b20\d{2}[-/]\d{2}[-/]\d{2}\b")
_CURRENCY_AND_QUANTITY_AMOUNTS = re.compile(
    r"(?:[$]|USD|EUR|GBP|CNY|RMB)\s*\d[\d,]*(?:\.\d+)?|"
    r"\b\d[\d,]*(?:\.\d+)?\s*(?:USD|EUR|GBP|CNY|RMB|pcs|pieces|units|meters|rolls|sets)\b",
    re.IGNORECASE,
)


class ComplianceCheckResult(BaseModel):
    allowed: bool
    violations: list[str] = Field(default_factory=list)
    blocked_reason: str | None = None


def _decision(*violations: str) -> ComplianceCheckResult:
    return ComplianceCheckResult(
        allowed=not violations,
        violations=list(violations),
        blocked_reason=violations[0] if violations else None,
    )


def consolidate_whatsapp_reply(segments: list[str]) -> str:
    return "\n\n".join(segment.strip() for segment in segments if segment.strip())


def _strip_benign_business_numbers(body: str) -> str:
    """Blank out order/SKU IDs, ISO dates and money/quantity amounts.

    Marketplace replies legitimately contain long digit runs (``ORD-20261121-001``,
    ``2026-11-21``, ``12345678.90 USD``, ``10000000 pcs``) that would otherwise trip
    the phone-number heuristic. Only the phone check runs on this sanitized copy;
    emails, URLs and chat handles are still matched against the original text.
    """
    for pattern in (
        _STRUCTURED_BUSINESS_IDS,
        _ISO_DATES,
        _CURRENCY_AND_QUANTITY_AMOUNTS,
    ):
        body = pattern.sub(" ", body)
    return body


def _normalize(body: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKC", body).casefold() if unicodedata.category(c) != "Cf"
    )


@dataclass(frozen=True, slots=True)
class OutboundConversation:
    id: UUID
    customer_id: UUID
    channel_type: ChannelType
    recipient: str
    autonomy_override: str | None


class OutboundComplianceFilter:
    def __init__(self) -> None:
        self.consent = ConsentAndSuppressionService()
        self.whatsapp = WhatsAppAdapter()

    def check_outbound(
        self,
        channel_type: ChannelType,
        body_text: str,
        sender_type: MessageSenderType = MessageSenderType.AI_AGENT,
        channel_metadata: dict[str, Any] | None = None,
    ) -> ComplianceCheckResult:
        metadata = channel_metadata or {}
        body = _normalize(body_text)
        violations = []
        tag = str(metadata.get("message_tag", "")).strip().upper()
        if tag in _FORBIDDEN_TAGS and sender_type == MessageSenderType.AI_AGENT:
            violations.append(f"ai_forbidden_{tag.lower()}_tag")
        # A conservative allowlist: generated marketplace messages may not contain links
        # or off-platform contact handles. Phone detection ignores benign business
        # numbers (order IDs, dates, amounts, quantities) to avoid false positives.
        if channel_type in MARKETPLACE_CHANNELS and (
            _OFF_PLATFORM.search(body)
            or _PHONE_CONTACT.search(_strip_benign_business_numbers(body))
        ):
            violations.append("marketplace_off_platform_contact")
        terms = metadata.get("blocked_terms")
        if isinstance(terms, list) and any(
            isinstance(word, str) and word.strip() and _normalize(word.strip()) in body
            for word in terms
        ):
            violations.append("blocked_term")
        return _decision(*violations)

    async def load_conversation(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        conversation_id: UUID,
    ) -> OutboundConversation | None:
        row = (
            (
                await session.execute(
                    text("""
            SELECT c.id, c.customer_id, c.channel_type, c.ai_autonomy_override,
                   CASE WHEN c.channel_type LIKE 'whatsapp%' THEN
                     coalesce(nullif(c.external_thread_id, ''), u.primary_phone_e164, '')
                   ELSE coalesce(u.primary_email, c.external_thread_id, '') END AS recipient
            FROM conversations c JOIN customers u
                ON u.tenant_id = c.tenant_id AND u.id = c.customer_id
            WHERE c.tenant_id = :tid AND c.id = :cid
        """),
                    {"tid": tenant_id, "cid": conversation_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return OutboundConversation(
            row.id,
            row.customer_id,
            ChannelType(row.channel_type),
            row.recipient,
            row.ai_autonomy_override,
        )

    async def check_context(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        conversation: OutboundConversation,
        channel_metadata: dict[str, Any],
    ) -> ComplianceCheckResult:
        channel = conversation.channel_type
        recipient = conversation.recipient
        if channel in WHATSAPP_CHANNELS:
            if not recipient:
                return _decision("missing_recipient")
            category = channel_metadata.get("template_category")
            decision = await self.whatsapp.evaluate_send_policy(
                session,
                tenant_id,
                conversation.id,
                conversation.customer_id,
                recipient,
                MessageContentType.TEMPLATE if category else MessageContentType.TEXT,
                template_category=str(category) if category else None,
            )
            if not decision.allowed:
                return _decision(decision.block_reason or "whatsapp_send_policy")
            if category:
                # AI drafts are free text, not approved provider templates.
                return _decision("ai_cannot_send_templates")
        elif recipient and await self.consent.is_suppressed(session, tenant_id, channel, recipient):
            return _decision("ERR_RECIPIENT_SUPPRESSED")
        return _decision()
