from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID, uuid4

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.ai_engine.models import AiProposal
from erp_core.ai_engine.router_llm import (
    MODEL_CATALOG,
    AsyncMultiProviderClient,
    LlmProviderResponse,
)
from erp_core.ai_engine.schemas import AiProposalRead
from erp_core.ai_engine.tools import ErpToolRegistry, ToolResult
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import AppError
from erp_core.tenancy.dependencies import CurrentPrincipal

_CATALOG_INTENT = re.compile(
    r"\b(?:sku|led|cob|strip|price|pricing|quote|quotation|fob|cif|ddp|exw|stock|"
    r"inventory|moq|meter|meters|mtr|pcs|piece|pieces|units|lead\s*time|delivery)\b"
    r"|报价|价格|库存|起订量|交期|货期|现货|单价|数量",
    re.I,
)
_ORDER_INTENT = re.compile(
    r"\b(?:order|tracking|track|ship|shipped|shipment|delivery|dhl|fedex|ups|ord-|demo-)\b"
    r"|订单|物流|发货|单号|快递|运单",
    re.I,
)
_ORDER_NO = re.compile(r"\b(?:ORD|DEMO|PI|SO)-[A-Z0-9][A-Z0-9-]{2,}\b", re.I)
# Case-insensitive so buyers typing `led-cob-24v-5m` get the same SKU treatment as
# `LED-COB-24V-5M` (stripped before quantity parsing, matched by `_search_terms`).
_SKU_TOKEN = re.compile(r"\b[A-Z][A-Z0-9]*(?:[-_/][A-Z0-9]+){1,6}\b", re.I)
# Primary count units: they always win over length units ("200 rolls of 5M strip").
_COUNT_UNITS = (
    r"cartons|carton|boxes|box|pcs|pieces|piece|units|unit|sets|set|rolls|roll|"
    r"reels|reel|件|个|套|卷|箱|台"
)
# Secondary length units: only consulted when no count unit matched.
_LENGTH_UNITS = r"meters|meter|mtr|mts|米|条"
# Product nouns that turn a bare `m` into a spec ("5M strip"), never an order quantity.
_LENGTH_NOUNS = r"strip|stripes|rolls|roll|reels|reel|tape|cable|cables|bar|bars|tube|tubes|led"
_QUANTITY_NUMBER = r"(\d{1,3}(?:[,\s]\d{3})+|\d+)"
# The `(?<![A-Za-z0-9\-_/.])` lookbehind keeps the fractional part of `1.5 meters`
# from ever being read as quantity 5.
_COUNT_QUANTITY = re.compile(
    r"(?<![A-Za-z0-9\-_/.])" + _QUANTITY_NUMBER + r"\s*(?:" + _COUNT_UNITS + r")(?![A-Za-z0-9])",
    re.I,
)
_LENGTH_QUANTITY = re.compile(
    r"(?<![A-Za-z0-9\-_/.])"
    + _QUANTITY_NUMBER
    + r"(?:\s*(?:"
    + _LENGTH_UNITS
    + r")(?![A-Za-z0-9])|\s*m(?![A-Za-z0-9])(?!\s*(?:"
    + _LENGTH_NOUNS
    + r")\b))",
    re.I,
)
_CJK = re.compile(r"[\u4e00-\u9fff]")
_SPANISH = re.compile(r"\b(?:hola|precio|precios|cotizacion|cotización|gracias|necesito)\b", re.I)

_GENERIC_CATALOG_TERMS = (
    "led",
    "cob",
    "strip",
    "floodlight",
    "highbay",
    "high-bay",
    "panel",
    "downlight",
    "bulb",
    "tube",
    "track",
    "driver",
)

@dataclass(frozen=True, slots=True)
class ConversationContext:
    """Read-only conversation + mailbox scope resolved with tenant-scoped SQL."""

    id: UUID
    customer_id: UUID
    channel_type: str
    assigned_user_id: UUID | None
    visibility_scope: str
    owner_user_id: UUID | None


@dataclass(frozen=True, slots=True)
class RecentMessage:
    direction: str
    sender_type: str
    body_text: str


class LlmClientPort(Protocol):
    """Minimal chat-completion surface so callers can inject a deterministic client."""

    def has_key(self, provider: str) -> bool:
        """Whether a platform-gateway credential exists for this provider."""
        ...

    async def complete(
        self,
        *,
        model_id: str,
        provider: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float,
    ) -> LlmProviderResponse:
        """Execute one grounded chat completion."""
        ...


_SYSTEM_PROMPT = (
    "You are the export sales assistant of a B2B LED manufacturer. Draft ONE short, "
    "professional customer reply.\n"
    "Absolute rules:\n"
    "1. Use ONLY the numbers, prices, MOQ tiers, stock levels, lead times and tracking "
    "events that appear in the TOOL_EVIDENCE JSON. Never invent, estimate, round or "
    "extrapolate any figure.\n"
    "2. Reply in the buyer's language (English, Chinese or Spanish).\n"
    "3. If TOOL_EVIDENCE contains no matching SKU or order, do NOT quote any number: "
    "politely ask for the SKU, destination port, required specifications or the order "
    "number instead.\n"
    "4. Never promise discounts, refunds, certificates, delivery dates or payment terms "
    "that are absent from TOOL_EVIDENCE.\n"
    "5. Keep it under 160 words, no markdown tables, no emoji, end with a clear question "
    "or next step and sign as Export Sales Team.\n"
    "6. Output the reply body only."
)


def _tool_call_uuid(tool_call_id: str) -> UUID:
    """Map an `ErpToolRegistry` `tc_<hex>` trace id onto its UUID provenance value."""
    raw = tool_call_id.strip()
    if raw.startswith("tc_"):
        raw = raw[3:]
    try:
        return UUID(raw)
    except ValueError:
        return uuid4()


def _detect_language(text: str) -> str:
    if _CJK.search(text):
        return "zh"
    if _SPANISH.search(text):
        return "es"
    return "en"


def _as_quantity(match: re.Match[str] | None) -> int | None:
    if match is None:
        return None
    digits = re.sub(r"[^\d]", "", match.group(1))
    try:
        qty = int(digits)
    except ValueError:
        return None
    return qty if 0 < qty <= 2_147_483_647 else None


def _parse_quantity(text: str) -> int:
    # Strip SKU / order tokens (e.g. LED-COB-24V-5M) so embedded lengths/voltages
    # like "5M" or "24V" are never mistaken for the buyer's requested quantity.
    cleaned = _SKU_TOKEN.sub(" ", text)
    cleaned = _ORDER_NO.sub(" ", cleaned)
    # Count units win: "5M strip, 200 rolls" is 200 rolls, not 5 metres of strip.
    count_qty = _as_quantity(_COUNT_QUANTITY.search(cleaned))
    if count_qty is not None:
        return count_qty
    # Length units are only a fallback, and a bare `m` before a product noun
    # ("5M strip") is a specification, never an order quantity.
    length_qty = _as_quantity(_LENGTH_QUANTITY.search(cleaned))
    if length_qty is not None:
        return length_qty
    return 1


class AiProposalGeneratorService:
    """Grounded auto-proposal generator: deterministic ERP SQL first, LLM second."""

    def __init__(
        self,
        tool_registry: ErpToolRegistry | None = None,
        llm_client: LlmClientPort | None = None,
    ) -> None:
        self.tools = tool_registry or ErpToolRegistry()
        self._llm_client = llm_client

    # ------------------------------------------------------------------ loading

    async def load_conversation(
        self, session: AsyncSession, tenant_id: UUID, conversation_id: UUID
    ) -> ConversationContext:
        row = (
            (
                await session.execute(
                    text(
                        """
                        SELECT c.id, c.customer_id, c.channel_type, c.assigned_user_id,
                               a.visibility_scope, a.owner_user_id
                        FROM conversations c
                        JOIN channel_accounts a
                          ON a.tenant_id = c.tenant_id AND a.id = c.channel_account_id
                        WHERE c.tenant_id = :tid AND c.id = :cid
                        """
                    ),
                    {"tid": tenant_id, "cid": conversation_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise AppError(
                slug="conversation-not-found",
                title="Conversation Not Found",
                status_code=404,
                code="ERR_CONVERSATION_NOT_FOUND",
                detail=f"Conversation '{conversation_id}' not found.",
            )
        return ConversationContext(
            id=row["id"],
            customer_id=row["customer_id"],
            channel_type=str(row["channel_type"]),
            assigned_user_id=row["assigned_user_id"],
            visibility_scope=str(row["visibility_scope"]),
            owner_user_id=row["owner_user_id"],
        )

    @staticmethod
    def assert_visibility(principal: CurrentPrincipal, conversation: ConversationContext) -> None:
        """Agents only see their own or unowned shared conversations.

        Mirrors `InboxService`'s `agent_can_access_conversation` policy without importing
        another domain's ORM models (see `test_infra_smoke`).
        """
        if principal.role != UserRole.AGENT:
            return
        if conversation.assigned_user_id is not None:
            allowed = conversation.assigned_user_id == principal.user_id
        else:
            allowed = (
                conversation.visibility_scope == "shared"
                and conversation.owner_user_id is None
            )
        if not allowed:
            raise AppError(
                slug="conversation-access-denied",
                title="Conversation Access Denied",
                status_code=403,
                code="ERR_CONVERSATION_ACCESS_DENIED",
                detail="This conversation belongs to another agent's private mailbox.",
            )

    async def _recent_messages(
        self, session: AsyncSession, tenant_id: UUID, conversation_id: UUID
    ) -> list[RecentMessage]:
        rows = (
            (
                await session.execute(
                    text(
                        """
                        SELECT direction, sender_type, COALESCE(body_text, '') AS body_text
                        FROM messages
                        WHERE tenant_id = :tid AND conversation_id = :cid
                        ORDER BY source_timestamp DESC, created_at DESC, id DESC
                        LIMIT 8
                        """
                    ),
                    {"tid": tenant_id, "cid": conversation_id},
                )
            )
            .mappings()
            .all()
        )
        return [
            RecentMessage(
                direction=str(row["direction"]),
                sender_type=str(row["sender_type"]),
                body_text=str(row["body_text"]),
            )
            for row in reversed(rows)
        ]

    async def _customer_summary(
        self, session: AsyncSession, tenant_id: UUID, customer_id: UUID
    ) -> dict[str, str]:
        row = (
            await session.execute(
                text(
                    """
                    SELECT c.display_name, c.primary_email, c.primary_phone_e164,
                           bc.legal_name, bc.country_code
                    FROM customers c
                    LEFT JOIN b2b_companies bc ON bc.id = c.company_id
                    WHERE c.tenant_id = :tid AND c.id = :cid
                    """
                ),
                {"tid": tenant_id, "cid": customer_id},
            )
        ).one_or_none()
        if row is None:
            return {"name": "", "company": "", "email": "", "phone": "", "country": ""}
        return {
            "name": str(row.display_name or ""),
            "company": str(row.legal_name or ""),
            "email": str(row.primary_email or ""),
            "phone": str(row.primary_phone_e164 or ""),
            "country": str(row.country_code or ""),
        }

    # ---------------------------------------------------------------- tool input

    @staticmethod
    def _search_terms(message_text: str) -> list[str]:
        terms: list[str] = []
        for token in _SKU_TOKEN.findall(message_text):
            cleaned = token.strip().upper()
            if len(cleaned) >= 4 and cleaned.lower() not in _GENERIC_CATALOG_TERMS:
                terms.append(cleaned)
        for word in re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", message_text):
            lowered = word.lower()
            if lowered in _GENERIC_CATALOG_TERMS:
                terms.append(lowered)
        deduped: list[str] = []
        for term in terms:
            if term not in deduped:
                deduped.append(term)
        return deduped[:8]

    async def _match_skus(
        self, session: AsyncSession, tenant_id: UUID, message_text: str
    ) -> list[str]:
        terms = self._search_terms(message_text)
        if not terms:
            # No SKU/catalog term in the message: returning nothing is correct.
            # Blindly defaulting to `['led']` priced an unrelated SKU for every
            # message that merely mentioned orders, shipping or delivery.
            return []
        params: dict[str, Any] = {"tid": tenant_id}
        clauses: list[str] = []
        exact_clauses: list[str] = []
        for index, term in enumerate(terms):
            key = f"pattern{index}"
            params[key] = f"%{term}%"
            exact_key = f"exact{index}"
            params[exact_key] = term.upper()
            clauses.append(
                f"(pv.sku ILIKE :{key} OR p.spu_code ILIKE :{key} OR p.category ILIKE :{key})"
            )
            exact_clauses.append(f"UPPER(pv.sku) = :{exact_key}")
        rows = (
            await session.execute(
                text(
                    "SELECT pv.sku FROM product_variants pv "
                    "JOIN products p ON p.id = pv.product_id AND p.tenant_id = pv.tenant_id "
                    "WHERE pv.tenant_id = :tid AND (" + " OR ".join(clauses) + ") "
                    "ORDER BY (CASE WHEN "
                    + " OR ".join(exact_clauses)
                    + " THEN 0 ELSE 1 END), length(pv.sku), pv.sku LIMIT 3"
                ),
                params,
            )
        ).scalars().all()
        return [str(sku) for sku in rows]

    async def _match_order_no(
        self, session: AsyncSession, tenant_id: UUID, customer_id: UUID, message_text: str
    ) -> str | None:
        explicit = _ORDER_NO.search(message_text)
        if explicit is not None:
            candidate = explicit.group(0).upper()
            owned = await session.scalar(
                text(
                    "SELECT order_no FROM orders "
                    "WHERE tenant_id = :tid AND customer_id = :cid AND order_no = :order_no "
                    "LIMIT 1"
                ),
                {"tid": tenant_id, "cid": customer_id, "order_no": candidate},
            )
            # The buyer named a specific order: answer it or nothing at all.
            # Falling through to "their latest order" would answer a different
            # order than the one asked about (and leak existence of others).
            return str(owned) if owned is not None else None
        latest = await session.scalar(
            text(
                "SELECT order_no FROM orders "
                "WHERE tenant_id = :tid AND customer_id = :cid "
                "ORDER BY created_at DESC, id LIMIT 1"
            ),
            {"tid": tenant_id, "cid": customer_id},
        )
        return str(latest) if latest is not None else None

    @staticmethod
    def _summarize(tool: ToolResult) -> str:
        data = tool.data
        if tool.tool_name == "get_live_sku_pricing_and_stock":
            tier = data.get("applied_moq_tier")
            tier_text = f" (MOQ tier {tier} pcs)" if tier else ""
            return (
                f"{data.get('sku')}: {data.get('applied_unit_price_usd')} USD/pc"
                f"{tier_text}, {data.get('available_qty')} pcs available, "
                f"lead time {data.get('lead_time_days')} days, incoterm {data.get('incoterm')}."
            )
        if tool.tool_name == "get_live_order_tracking":
            shipments = data.get("shipments") or []
            if not shipments:
                return (
                    f"Order {data.get('order_no')} is {data.get('fulfillment_status')} "
                    "with no tracking record yet."
                )
            latest = shipments[0]
            return (
                f"Order {data.get('order_no')} {latest.get('standardized_status')} "
                f"via {latest.get('carrier_code')} ({latest.get('tracking_number')})."
            )
        return f"{tool.tool_name} returned verified data."

    async def _collect_evidence(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        customer_id: UUID,
        message_text: str,
    ) -> list[dict[str, Any]]:
        evidence: list[dict[str, Any]] = []
        if _CATALOG_INTENT.search(message_text) or not message_text.strip():
            quantity = _parse_quantity(message_text)
            for sku in await self._match_skus(session, tenant_id, message_text):
                tool = await self.tools.get_live_sku_pricing_and_stock(
                    session, tenant_id, sku, quantity
                )
                if tool.ok:
                    evidence.append(self._evidence_payload(tool))
        if _ORDER_INTENT.search(message_text):
            order_no = await self._match_order_no(session, tenant_id, customer_id, message_text)
            if order_no is not None:
                tracking = await self.tools.get_live_order_tracking(
                    session, tenant_id, order_no, customer_id=customer_id
                )
                if tracking.ok:
                    evidence.append(self._evidence_payload(tracking))
        return evidence

    def _evidence_payload(self, tool: ToolResult) -> dict[str, Any]:
        return {
            "tool_call_id": tool.tool_call_id,
            "tool_name": tool.tool_name,
            "summary": self._summarize(tool),
            "data": tool.data,
        }

    # ------------------------------------------------------------------- drafting

    def _deterministic_draft(
        self,
        language: str,
        customer: dict[str, str],
        message_text: str,
        evidence: list[dict[str, Any]],
    ) -> str:
        greeting_name = customer["name"] or "there"
        if evidence:
            lines = [f"SKU {item['data'].get('sku')}: {item['summary']}" for item in evidence]
            if language == "zh":
                return (
                    f"您好 {greeting_name}，\n\n"
                    "感谢您的询价。以下为系统实时核实的报价与库存信息：\n"
                    + "\n".join(f"- {line}" for line in lines)
                    + "\n\n以上价格基于您所询问的数量，最终以正式形式发票（PI）为准。"
                    "请告知目的港以及需要的认证，我们即刻为您出具 PI。\n\n"
                    "顺祝商祺，\n外贸销售团队"
                )
            if language == "es":
                return (
                    f"Hola {greeting_name},\n\n"
                    "Gracias por su consulta. Estos son los datos verificados en tiempo real:"
                    "\n"
                    + "\n".join(f"- {line}" for line in lines)
                    + "\n\nLos precios corresponden a la cantidad solicitada y se confirman en la "
                    "Factura Proforma. Indiquenos el puerto de destino y las certificaciones "
                    "requeridas para emitir la PI.\n\nSaludos cordiales,\nEquipo de Ventas de "
                    "Exportacion"
                )
            return (
                f"Hello {greeting_name},\n\n"
                "Thank you for your inquiry. Here is the live verified information from our ERP:"
                "\n"
                + "\n".join(f"- {line}" for line in lines)
                + "\n\nThese figures apply to the quantity you asked about and are confirmed on "
                "the formal Proforma Invoice. Please share your destination port and required "
                "certifications so we can issue the PI right away.\n\n"
                "Best regards,\nExport Sales Team"
            )

        if language == "zh":
            return (
                f"您好 {greeting_name}，\n\n"
                "感谢您的来信。为准确报价，请提供具体 SKU 或产品规格、目的港，以及目标数量与"
                "所需认证。我们在收到信息后立即提供正式报价与形式发票。\n\n"
                "顺祝商祺，\n外贸销售团队"
            )
        if language == "es":
            return (
                f"Hola {greeting_name},\n\n"
                "Gracias por su mensaje. Para cotizar con precision, indiquenos el SKU o las "
                "especificaciones, el puerto de destino, la cantidad objetivo y las "
                "certificaciones requeridas. Enviaremos la cotizacion y la Factura Proforma "
                "inmediatamente.\n\nSaludos cordiales,\nEquipo de Ventas de Exportacion"
            )
        return (
            f"Hello {greeting_name},\n\n"
            "Thank you for reaching out. To quote accurately, could you share the exact SKU or "
            "specification, your destination port, the target quantity and any required "
            "certifications? We will send the formal quotation and Proforma Invoice right away."
            "\n\nBest regards,\nExport Sales Team"
        )

    def _llm_available(self, llm_client: LlmClientPort | None) -> bool:
        if llm_client is not None:
            return True
        return bool(os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY"))

    def _provider_and_model(self, client: LlmClientPort) -> tuple[str, str]:
        if client.has_key("deepseek"):
            return "deepseek", MODEL_CATALOG["deepseek-flash"].model_id
        if client.has_key("openai"):
            return "openai", MODEL_CATALOG["gpt-5-mini"].model_id
        return "", ""

    async def _draft_with_llm(
        self,
        llm_client: LlmClientPort | None,
        language: str,
        customer: dict[str, str],
        message_text: str,
        evidence: list[dict[str, Any]],
    ) -> str | None:
        client = llm_client or AsyncMultiProviderClient()
        provider, model_id = self._provider_and_model(client)
        if not provider:
            return None
        customer_block = (
            f"Buyer name: {customer['name']}\n"
            f"Buyer company: {customer['company']}\n"
            f"Buyer country: {customer['country']}\n"
            f"Reply language: {language}"
        )
        evidence_block = json.dumps(evidence, ensure_ascii=False, default=str)
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"{customer_block}\n\n"
                    f"BUYER_MESSAGE:\n{message_text}\n\n"
                    f"TOOL_EVIDENCE:\n{evidence_block}"
                ),
            },
        ]
        try:
            response: LlmProviderResponse = await client.complete(
                model_id=model_id,
                provider=provider,
                messages=messages,
                max_tokens=700,
                temperature=0.1,
            )
        except (RuntimeError, ValueError, TypeError, KeyError):
            return None
        draft = response.text.strip()
        return draft or None

    def _reasoning_summary(self, evidence: list[dict[str, Any]], used_llm: bool) -> str:
        if not evidence:
            return (
                "No verified SKU or order matched this message; the draft asks for the missing "
                "SKU, destination port, quantity or order number instead of inventing figures."
            )
        tools = ", ".join(sorted({str(item["tool_name"]) for item in evidence}))
        grounding = (
            "Grounded on live ERP SQL results"
            if not used_llm
            else "LLM draft constrained to live ERP SQL results"
        )
        return f"{grounding} ({tools}); every number in the draft is traceable to a tool call."

    # ----------------------------------------------------------------- generating

    async def generate_reply_proposal(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        conversation_id: UUID,
        *,
        redis: Redis | None = None,
        llm_client: LlmClientPort | None = None,
    ) -> AiProposalRead:
        conversation = await self.load_conversation(session, tenant_id, conversation_id)
        messages = await self._recent_messages(session, tenant_id, conversation_id)
        customer = await self._customer_summary(session, tenant_id, conversation.customer_id)

        inbound = [m for m in messages if m.direction == "inbound"]
        latest_inbound = inbound[-1] if inbound else None
        message_text = (latest_inbound.body_text if latest_inbound else "") or ""

        evidence = await self._collect_evidence(
            session, tenant_id, conversation.customer_id, message_text
        )
        primary_tool_call_id = (
            _tool_call_uuid(str(evidence[0]["tool_call_id"])) if evidence else None
        )

        language = _detect_language(message_text)
        draft: str | None = None
        if self._llm_available(llm_client):
            draft = await self._draft_with_llm(
                llm_client, language, customer, message_text, evidence
            )
        used_llm = draft is not None
        if draft is None:
            draft = self._deterministic_draft(language, customer, message_text, evidence)

        payload: dict[str, Any] = {
            "body_text": draft,
            "channel_type": conversation.channel_type,
            "customer_name": customer["name"] or customer["company"],
            "reasoning_summary": self._reasoning_summary(evidence, used_llm),
            "verified_tool_call_ids": [str(item["tool_call_id"]) for item in evidence],
            "tool_evidence": evidence,
        }
        proposal = AiProposal(
            tenant_id=tenant_id,
            capability="cs_support",
            proposal_type="reply_draft",
            conversation_id=conversation.id,
            source_tool_call_id=primary_tool_call_id,
            proposed_payload=payload,
            citations=[
                {
                    "source_tool_call_id": str(item["tool_call_id"]),
                    "tool_name": str(item["tool_name"]),
                }
                for item in evidence
            ],
            confidence_score=Decimal("0.92") if primary_tool_call_id else Decimal("0.72"),
            status="pending",
        )
        session.add(proposal)
        await session.flush()

        if redis is not None:
            await redis.publish(
                f"inbox:events:{tenant_id}",
                json.dumps(
                    {
                        "event": "proposal.created",
                        "proposal_id": str(proposal.id),
                        "conversation_id": str(conversation.id),
                        "assigned_user_id": (
                            str(conversation.assigned_user_id)
                            if conversation.assigned_user_id
                            else None
                        ),
                        "visibility_scope": conversation.visibility_scope,
                    }
                ),
            )
        return AiProposalRead.model_validate(proposal)
