from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, ClassVar, cast

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.channels.adapters.base import (
    MessagingChannelAdapter,
    NormalizedAttachment,
    NormalizedInboundEvent,
    NormalizedStatusEvent,
    NormalizedWebhookBatch,
    OutboundMessageRequest,
    OutboundSendResult,
)
from erp_core.channels.consent_service import ConsentAndSuppressionService
from erp_core.shared.config import get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import (
    ChannelStatus,
    ChannelType,
    ConversationMode,
    DeliveryStatus,
    IdentityType,
    MessageContentType,
    MessageDirection,
    MessageSenderType,
    UserRole,
)
from erp_core.tenancy.audit import record_audit_event
from erp_core.tenancy.dependencies import CurrentPrincipal, require_roles

whatsapp_router = APIRouter(prefix="/api/v1/channels/whatsapp", tags=["whatsapp"])
_require_channel_admin = require_roles(UserRole.OWNER, UserRole.ADMIN)
_require_consent_writer = require_roles(
    UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT
)

_STOP_KEYWORDS: frozenset[str] = frozenset(
    {"stop", "unsubscribe", "cancel", "opt out", "opt-out", "stop promotions", "退订"}
)
_WA_FREE_MONTHLY_SERVICE_QUOTA = 1000
_WA_RETRYABLE_CODES: frozenset[int] = frozenset({131056, 130429, 131049})


class WhatsAppSendPolicyDecision(BaseModel):
    allowed: bool
    block_reason: str | None = None
    pricing_category: str | None = None
    estimated_cost_usd: Decimal = Decimal("0.000000")
    csw_active: bool = False
    fep_active: bool = False


class EmbeddedSignupRequest(BaseModel):
    code: str = Field(min_length=4)
    waba_id: str = Field(min_length=3)
    phone_number_id: str = Field(min_length=3)
    is_coexistence: bool = False
    display_name: str = "WhatsApp Business"


class EmbeddedSignupResponse(BaseModel):
    channel_account_id: uuid.UUID
    channel_type: ChannelType
    mps_cap: int


class ConsentOptInRequest(BaseModel):
    customer_id: uuid.UUID
    normalized_phone: str
    proof_source: str = "manual_verified_form"
    disclosed_business_name: str
    scope: list[str] = Field(default_factory=lambda: ["order_updates", "promotions"])


class WhatsAppAdapter(MessagingChannelAdapter):
    """Meta WhatsApp Cloud API + Coexistence adapter enforcing 2026-10 pricing and compliance."""

    channel_types: ClassVar[tuple[ChannelType, ...]] = (
        ChannelType.WHATSAPP_CLOUD,
        ChannelType.WHATSAPP_COEXISTENCE,
    )

    def __init__(
        self,
        *,
        graph_base_url: str = "https://graph.facebook.com/v24.0",
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._graph_base_url = graph_base_url.rstrip("/")
        self._http_transport = http_transport
        self._consent_service = ConsentAndSuppressionService()

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=15.0, transport=self._http_transport)

    def verify_webhook_signature(
        self, raw_body: bytes, headers: Mapping[str, str], secret: str
    ) -> bool:
        sig_header = headers.get("x-hub-signature-256", "")
        if not sig_header.startswith("sha256="):
            return False
        expected = (
            "sha256=" + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        )
        return hmac.compare_digest(sig_header, expected)

    def extract_external_event_ids(
        self, payload: dict[str, Any], raw_body: bytes, headers: Mapping[str, str]
    ) -> list[tuple[str, str | None]]:
        events: list[tuple[str, str | None]] = []
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                field = change.get("field", "")
                val = change.get("value", {})
                phone_id = (val.get("metadata") or {}).get("phone_number_id")
                if field == "messages":
                    for msg in val.get("messages", []):
                        events.append((str(msg["id"]), phone_id))
                    for st in val.get("statuses", []):
                        events.append(
                            (
                                f"status:{st['id']}:{st.get('status')}:{st.get('timestamp', '')}",
                                phone_id,
                            )
                        )
                elif field == "smb_message_echoes":
                    for echo in val.get("message_echoes", []):
                        events.append((str(echo["id"]), phone_id))
                elif field == "history":
                    for hist in val.get("history", []):
                        phase = hist.get("phase", 0)
                        chunk = hist.get("chunk_order", 0)
                        events.append((f"history:{phone_id}:{phase}:{chunk}", phone_id))
                elif field in (
                    "smb_app_state_sync",
                    "message_template_status_update",
                    "phone_number_quality_update",
                    "account_update",
                ):
                    digest = hashlib.sha256(
                        json.dumps(change, sort_keys=True).encode("utf-8")
                    ).hexdigest()[:24]
                    events.append((f"{field}:{digest}", phone_id or entry.get("id")))
        if not events:
            events.append((hashlib.sha256(raw_body).hexdigest(), None))
        return events

    def _detect_stop_command(self, msg: dict[str, Any]) -> bool:
        mtype = msg.get("type")
        candidate = ""
        if mtype == "text":
            candidate = (msg.get("text") or {}).get("body", "")
        elif mtype == "button":
            candidate = (msg.get("button") or {}).get("payload", "") or (
                msg.get("button") or {}
            ).get("text", "")
        elif mtype == "interactive":
            ir = msg.get("interactive") or {}
            candidate = (ir.get("button_reply") or {}).get("id", "") or (
                ir.get("button_reply") or {}
            ).get("title", "")
        return candidate.strip().lower() in _STOP_KEYWORDS

    def _extract_body_and_attachments(
        self, msg: dict[str, Any]
    ) -> tuple[MessageContentType, str | None, list[NormalizedAttachment]]:
        mtype = msg.get("type", "text")
        if mtype == "text":
            return MessageContentType.TEXT, (msg.get("text") or {}).get("body"), []
        if mtype == "button":
            return MessageContentType.TEXT, (msg.get("button") or {}).get("text"), []
        if mtype == "interactive":
            ir = msg.get("interactive") or {}
            title = (ir.get("button_reply") or ir.get("list_reply") or {}).get("title")
            return MessageContentType.TEXT, title, []
        if mtype in ("image", "document", "audio", "video"):
            media = msg.get(mtype) or {}
            caption = media.get("caption") or f"[{mtype}]"
            att = NormalizedAttachment(
                filename=media.get("filename") or f"whatsapp_{mtype}_{media.get('id', 'file')}",
                mime_type=media.get("mime_type") or f"{mtype}/octet-stream",
                external_media_id=media.get("id"),
            )
            return MessageContentType.MEDIA, caption, [att]
        return MessageContentType.TEXT, f"[{mtype}]", []

    async def parse_webhook(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        channel_account_id: uuid.UUID,
        payload: dict[str, Any],
        headers: Mapping[str, str],
    ) -> NormalizedWebhookBatch:
        inbound: list[NormalizedInboundEvent] = []
        statuses: list[NormalizedStatusEvent] = []
        account_updates: list[dict[str, Any]] = []

        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                field = change.get("field", "")
                val = change.get("value") or {}
                phone_id = str((val.get("metadata") or {}).get("phone_number_id") or "")

                contacts_by_key: dict[str, dict[str, Any]] = {}
                for c in val.get("contacts", []):
                    if c.get("wa_id"):
                        contacts_by_key[str(c["wa_id"])] = c
                    if c.get("user_id"):
                        contacts_by_key[str(c["user_id"])] = c

                if field == "messages":
                    for msg in val.get("messages", []):
                        wa_id = msg.get("from")
                        bsuid = msg.get("from_user_id")
                        contact = (
                            contacts_by_key.get(str(wa_id)) or contacts_by_key.get(str(bsuid)) or {}
                        )
                        if not bsuid:
                            bsuid = contact.get("user_id")
                        if not wa_id:
                            wa_id = contact.get("wa_id")

                        if wa_id:
                            primary_type = IdentityType.WHATSAPP_WAID
                            primary_val = str(wa_id)
                            extras = [(IdentityType.WHATSAPP_BSUID, str(bsuid))] if bsuid else []
                        elif bsuid:
                            primary_type = IdentityType.WHATSAPP_BSUID
                            primary_val = str(bsuid)
                            extras = []
                        else:
                            continue

                        display_name = (contact.get("profile") or {}).get("name") or primary_val
                        referral = msg.get("referral") or {}
                        ctwa_clid = referral.get("ctwa_clid")
                        referral_ad_id = referral.get("source_id")
                        ctype, body_text, attachments = self._extract_body_and_attachments(msg)

                        inbound.append(
                            NormalizedInboundEvent(
                                external_event_id=str(msg["id"]),
                                external_account_id=phone_id,
                                external_message_id=str(msg["id"]),
                                external_thread_id=primary_val,
                                sender_identity_type=primary_type,
                                sender_identity_value=primary_val,
                                sender_display_name=display_name,
                                direction=MessageDirection.INBOUND,
                                sender_type=MessageSenderType.CUSTOMER,
                                content_type=ctype,
                                body_text=body_text,
                                attachments=attachments,
                                source_timestamp=datetime.fromtimestamp(
                                    int(msg["timestamp"]), tz=UTC
                                ),
                                conversation_mode=ConversationMode.B2C_SUPPORT,
                                channel_metadata={
                                    "bsuid": bsuid,
                                    "wa_id": wa_id,
                                    "referral": referral or None,
                                },
                                ctwa_clid=ctwa_clid,
                                referral_ad_id=referral_ad_id,
                                extra_identities=extras,
                                is_stop_command=self._detect_stop_command(msg),
                                opens_csw=True,
                            )
                        )

                    for st in val.get("statuses", []):
                        raw_status = str(st.get("status", "sent")).lower()
                        mapped_status = DeliveryStatus(
                            raw_status
                            if raw_status in {"sent", "delivered", "read", "failed"}
                            else "sent"
                        )
                        err = (st.get("errors") or [{}])[0]
                        statuses.append(
                            NormalizedStatusEvent(
                                external_message_id=str(st["id"]),
                                external_account_id=phone_id,
                                delivery_status=mapped_status,
                                timestamp=datetime.fromtimestamp(
                                    int(st.get("timestamp", 0)), tz=UTC
                                ),
                                error_code=err.get("code"),
                                error_message=err.get("title") or err.get("message"),
                                pricing_metadata=st.get("pricing") or {},
                                # Match the gateway adapter: an orphan status must raise
                                # OutOfOrderParentError in the worker so it lands in the DLQ.
                                require_existing_message=True,
                            )
                        )

                elif field == "smb_message_echoes":
                    for echo in val.get("message_echoes", []):
                        to_waid = echo.get("to")
                        to_bsuid = echo.get("to_user_id")
                        if to_waid:
                            p_type, p_val = IdentityType.WHATSAPP_WAID, str(to_waid)
                            extras = (
                                [(IdentityType.WHATSAPP_BSUID, str(to_bsuid))] if to_bsuid else []
                            )
                        elif to_bsuid:
                            p_type, p_val = IdentityType.WHATSAPP_BSUID, str(to_bsuid)
                            extras = []
                        else:
                            continue

                        ctype, body_text, attachments = self._extract_body_and_attachments(echo)
                        inbound.append(
                            NormalizedInboundEvent(
                                external_event_id=str(echo["id"]),
                                external_account_id=phone_id,
                                external_message_id=str(echo["id"]),
                                external_thread_id=p_val,
                                sender_identity_type=p_type,
                                sender_identity_value=p_val,
                                sender_display_name=p_val,
                                direction=MessageDirection.OUTBOUND,
                                sender_type=MessageSenderType.HUMAN_AGENT,
                                content_type=ctype,
                                body_text=body_text,
                                attachments=attachments,
                                source_timestamp=datetime.fromtimestamp(
                                    int(echo["timestamp"]), tz=UTC
                                ),
                                channel_metadata={
                                    "coexistence_echo": True,
                                    "opens_csw": False,
                                },
                                extra_identities=extras,
                                opens_csw=False,
                            )
                        )

                elif field == "history":
                    biz_phone = str((val.get("metadata") or {}).get("display_phone_number") or "")
                    for hist in val.get("history", []):
                        phase = hist.get("phase", 0)
                        chunk_order = hist.get("chunk_order", 0)
                        for thread in hist.get("threads", []):
                            thread_id = str(thread.get("id") or "")
                            for msg in thread.get("messages", []):
                                is_from_biz = str(msg.get("from", "")) == biz_phone
                                peer_val = str(
                                    msg.get("to") if is_from_biz else msg.get("from", thread_id)
                                )
                                ctype, body_text, attachments = self._extract_body_and_attachments(
                                    msg
                                )
                                inbound.append(
                                    NormalizedInboundEvent(
                                        external_event_id=str(msg["id"]),
                                        external_account_id=phone_id,
                                        external_message_id=str(msg["id"]),
                                        external_thread_id=peer_val,
                                        sender_identity_type=IdentityType.WHATSAPP_WAID,
                                        sender_identity_value=peer_val,
                                        sender_display_name=peer_val,
                                        direction=(
                                            MessageDirection.OUTBOUND
                                            if is_from_biz
                                            else MessageDirection.INBOUND
                                        ),
                                        sender_type=(
                                            MessageSenderType.HUMAN_AGENT
                                            if is_from_biz
                                            else MessageSenderType.CUSTOMER
                                        ),
                                        content_type=ctype,
                                        body_text=body_text,
                                        attachments=attachments,
                                        source_timestamp=datetime.fromtimestamp(
                                            int(msg["timestamp"]), tz=UTC
                                        ),
                                        channel_metadata={
                                            "coexistence_history": True,
                                            "phase": phase,
                                            "chunk_order": chunk_order,
                                        },
                                        opens_csw=False,
                                    )
                                )

                elif field in (
                    "smb_app_state_sync",
                    "message_template_status_update",
                    "phone_number_quality_update",
                    "account_update",
                ):
                    account_updates.append({"field": field, "value": val})
                    if field == "account_update" and val.get("event") in (
                        "PARTNER_REMOVED",
                        "PRIMARY_INACTIVITY",
                        "COMPANION_INACTIVITY",
                    ):
                        await session.execute(
                            text(
                                "UPDATE channel_accounts SET status = :st, updated_at = now() "
                                "WHERE tenant_id = :tid AND id = :aid"
                            ),
                            {
                                "tid": tenant_id,
                                "aid": channel_account_id,
                                "st": ChannelStatus.DISCONNECTED.value,
                            },
                        )

        return NormalizedWebhookBatch(
            inbound_events=inbound,
            status_events=statuses,
            account_updates=account_updates,
        )

    async def complete_embedded_signup(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        crypto_service: EnvelopeCryptoService,
        code: str,
        waba_id: str,
        phone_number_id: str,
        is_coexistence: bool = False,
        display_name: str = "WhatsApp Business",
    ) -> uuid.UUID:
        """Complete Embedded Signup v4 code exchange, WABA subscription, and Coexistence sync."""
        app_id = os.environ["WHATSAPP_APP_ID"]
        app_secret = os.environ["WHATSAPP_APP_SECRET"]

        async with self._client() as client:
            tok_resp = await client.get(
                f"{self._graph_base_url}/oauth/access_token",
                params={
                    "client_id": app_id,
                    "client_secret": app_secret,
                    "code": code,
                },
            )
            tok_resp.raise_for_status()
            access_token = tok_resp.json()["access_token"]
            auth_headers = {"Authorization": f"Bearer {access_token}"}

            sub_resp = await client.post(
                f"{self._graph_base_url}/{waba_id}/subscribed_apps",
                headers=auth_headers,
            )
            sub_resp.raise_for_status()

            if is_coexistence:
                # Coexistence MUST NOT call /register; verify status & trigger 24h sync
                chk_resp = await client.get(
                    f"{self._graph_base_url}/{phone_number_id}",
                    params={"fields": "is_on_biz_app,platform_type"},
                    headers=auth_headers,
                )
                chk_resp.raise_for_status()
                for sync_type in ("smb_app_state_sync", "history"):
                    sync_resp = await client.post(
                        f"{self._graph_base_url}/{phone_number_id}/smb_app_data",
                        headers=auth_headers,
                        json={"messaging_product": "whatsapp", "sync_type": sync_type},
                    )
                    sync_resp.raise_for_status()

        channel_type = (
            ChannelType.WHATSAPP_COEXISTENCE if is_coexistence else ChannelType.WHATSAPP_CLOUD
        )
        rate_profile = {"mps": 20 if is_coexistence else 80, "coexistence": is_coexistence}
        cfg = {
            "waba_id": waba_id,
            "phone_number_id": phone_number_id,
            "is_on_biz_app": is_coexistence,
            "service_msg_period_ym": datetime.now(UTC).strftime("%Y-%m"),
            "service_msg_monthly_count": 0,
        }

        channel_account_id = uuid.uuid4()
        upserted_id = (
            await session.execute(
                text(
                    "INSERT INTO channel_accounts ("
                    "id, tenant_id, channel_type, external_account_id, display_name, status, "
                    "rate_limit_profile, config"
                    ") VALUES ("
                    ":id, :tid, :ctype, :ext, :dname, 'active', "
                    "CAST(:rate AS jsonb), CAST(:cfg AS jsonb)"
                    ") ON CONFLICT (tenant_id, channel_type, external_account_id) "
                    "DO UPDATE SET display_name = EXCLUDED.display_name, status = 'active', "
                    "rate_limit_profile = EXCLUDED.rate_limit_profile, config = EXCLUDED.config, "
                    "updated_at = now() "
                    "RETURNING id"
                ),
                {
                    "id": channel_account_id,
                    "tid": tenant_id,
                    "ctype": channel_type.value,
                    "ext": phone_number_id,
                    "dname": display_name,
                    "rate": json.dumps(rate_profile),
                    "cfg": json.dumps(cfg),
                },
            )
        ).scalar_one()

        await session.execute(
            text(
                "INSERT INTO channel_routing_directory "
                "(channel_type, external_account_id, tenant_id, channel_account_id, is_active) "
                "VALUES (:ctype, :ext, :tid, :aid, true) "
                "ON CONFLICT (channel_type, external_account_id) DO UPDATE SET "
                "tenant_id = EXCLUDED.tenant_id, channel_account_id = EXCLUDED.channel_account_id, "
                "is_active = true"
            ),
            {
                "ctype": channel_type.value,
                "ext": phone_number_id,
                "tid": tenant_id,
                "aid": upserted_id,
            },
        )

        ciphertext, nonce, auth_tag, key_version = await crypto_service.encrypt_secret(
            session=session,
            tenant_id=tenant_id,
            account_id=upserted_id,
            cred_type="oauth_token",
            plaintext=json.dumps({"access_token": access_token, "waba_id": waba_id}),
        )
        await session.execute(
            text(
                "INSERT INTO channel_credentials ("
                "id, tenant_id, channel_account_id, credential_type, "
                "ciphertext, nonce, auth_tag, key_version"
                ") VALUES ("
                ":id, :tid, :aid, 'oauth_token', :ct, :nonce, :tag, :kv"
                ") ON CONFLICT (tenant_id, channel_account_id, credential_type) "
                "DO UPDATE SET ciphertext = EXCLUDED.ciphertext, nonce = EXCLUDED.nonce, "
                "auth_tag = EXCLUDED.auth_tag, key_version = EXCLUDED.key_version, updated_at = "
                "now()"
            ),
            {
                "id": uuid.uuid4(),
                "tid": tenant_id,
                "aid": upserted_id,
                "ct": ciphertext,
                "nonce": nonce,
                "tag": auth_tag,
                "kv": key_version,
            },
        )
        return cast(uuid.UUID, upserted_id)

    async def evaluate_send_policy(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        conversation_id: uuid.UUID,
        customer_id: uuid.UUID,
        recipient_address: str,
        content_type: MessageContentType,
        template_category: str | None = None,
        now_utc: datetime | None = None,
    ) -> WhatsAppSendPolicyDecision:
        """Enforce suppression_list, 24h CSW vs 7-day CTWA FEP, consent, and Oct 2026 pricing."""
        ref_now = now_utc or datetime.now(UTC)

        if await self._consent_service.is_suppressed(
            session, tenant_id, "whatsapp", recipient_address
        ):
            return WhatsAppSendPolicyDecision(
                allowed=False,
                block_reason="ERR_RECIPIENT_SUPPRESSED",
            )

        conv_row = (
            (
                await session.execute(
                    text(
                        "SELECT conv.csw_expires_at, conv.fep_expires_at, acct.config "
                        "FROM conversations conv "
                        "JOIN channel_accounts acct ON acct.id = conv.channel_account_id "
                        "WHERE conv.tenant_id = :tid AND conv.id = :cid"
                    ),
                    {"tid": tenant_id, "cid": conversation_id},
                )
            )
            .mappings()
            .first()
        )
        if conv_row is None:
            return WhatsAppSendPolicyDecision(
                allowed=False, block_reason="ERR_CONVERSATION_NOT_FOUND"
            )

        csw_exp = conv_row["csw_expires_at"]
        fep_exp = conv_row["fep_expires_at"]
        csw_active = csw_exp is not None and csw_exp > ref_now
        fep_active = fep_exp is not None and fep_exp > ref_now

        if content_type != MessageContentType.TEMPLATE:
            if not (csw_active or fep_active):
                return WhatsAppSendPolicyDecision(
                    allowed=False,
                    block_reason="ERR_WHATSAPP_CSW_EXPIRED",
                    csw_active=False,
                    fep_active=False,
                )
            if fep_active:
                return WhatsAppSendPolicyDecision(
                    allowed=True,
                    pricing_category="free_entry_point",
                    estimated_cost_usd=Decimal("0.000000"),
                    csw_active=csw_active,
                    fep_active=True,
                )
            cfg = conv_row["config"] or {}
            current_ym = ref_now.strftime("%Y-%m")
            used_count = (
                int(cfg.get("service_msg_monthly_count", 0))
                if cfg.get("service_msg_period_ym") == current_ym
                else 0
            )
            if used_count < _WA_FREE_MONTHLY_SERVICE_QUOTA:
                return WhatsAppSendPolicyDecision(
                    allowed=True,
                    pricing_category="free_service_tier",
                    estimated_cost_usd=Decimal("0.000000"),
                    csw_active=True,
                    fep_active=False,
                )
            return WhatsAppSendPolicyDecision(
                allowed=True,
                pricing_category="billable_service_2026_10",
                estimated_cost_usd=Decimal("0.008000"),
                csw_active=True,
                fep_active=False,
            )

        category = (template_category or "utility").lower()
        if category == "marketing":
            norm_addr = self._consent_service.normalize_address("whatsapp", recipient_address)
            if norm_addr.startswith("+1"):
                return WhatsAppSendPolicyDecision(
                    allowed=False,
                    block_reason="ERR_WHATSAPP_US_MARKETING_PROHIBITED",
                )
            if not (csw_active or fep_active):
                has_opt_in = await self._consent_service.has_valid_opt_in(
                    session, tenant_id, customer_id, "whatsapp"
                )
                if not has_opt_in:
                    return WhatsAppSendPolicyDecision(
                        allowed=False,
                        block_reason="ERR_WHATSAPP_OPT_IN_REQUIRED",
                    )

        if fep_active:
            return WhatsAppSendPolicyDecision(
                allowed=True,
                pricing_category="free_entry_point",
                estimated_cost_usd=Decimal("0.000000"),
                csw_active=csw_active,
                fep_active=True,
            )

        # Oct 2026 rule: utility templates inside CSW are billed as regular
        return WhatsAppSendPolicyDecision(
            allowed=True,
            pricing_category="billable_regular_2026_10",
            estimated_cost_usd=Decimal("0.020000")
            if category == "utility"
            else Decimal("0.050000"),
            csw_active=csw_active,
            fep_active=False,
        )

    async def send_message(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        credentials: dict[str, Any],
        request: OutboundMessageRequest,
    ) -> OutboundSendResult:
        conv_cust = (
            await session.execute(
                text("SELECT customer_id FROM conversations WHERE tenant_id = :tid AND id = :cid"),
                {"tid": tenant_id, "cid": request.conversation_id},
            )
        ).scalar_one_or_none()
        if conv_cust is None:
            return OutboundSendResult(
                success=False, error_message="ERR_CONVERSATION_NOT_FOUND", retryable=False
            )

        decision = await self.evaluate_send_policy(
            session=session,
            tenant_id=tenant_id,
            conversation_id=request.conversation_id,
            customer_id=conv_cust,
            recipient_address=request.recipient_id,
            content_type=request.content_type,
            template_category=request.metadata.get("template_category"),
        )
        if not decision.allowed:
            return OutboundSendResult(
                success=False,
                error_message=decision.block_reason,
                retryable=False,
            )

        access_token = str(credentials.get("access_token", ""))
        body: dict[str, Any] = {"messaging_product": "whatsapp", "recipient_type": "individual"}
        norm = self._consent_service.normalize_address("whatsapp", request.recipient_id)
        if any(ch.isalpha() for ch in norm):
            body["recipient"] = norm
        else:
            body["to"] = norm.lstrip("+")

        if request.content_type == MessageContentType.TEMPLATE and request.template_name:
            body["type"] = "template"
            body["template"] = {
                "name": request.template_name,
                "language": {"code": request.template_language or "en_US"},
                "components": request.template_components,
            }
        else:
            body["type"] = "text"
            body["text"] = {"preview_url": False, "body": request.body_text or ""}

        async with self._client() as client:
            resp = await client.post(
                f"{self._graph_base_url}/{request.external_account_id}/messages",
                headers={"Authorization": f"Bearer {access_token}"},
                json=body,
            )

        if resp.status_code >= 400:
            err_data = resp.json().get("error", {}) if resp.content else {}
            err_code = err_data.get("code")
            retryable = (
                resp.status_code >= 500
                or resp.status_code == 429
                or err_code in _WA_RETRYABLE_CODES
            )
            return OutboundSendResult(
                success=False,
                error_code=int(err_code) if isinstance(err_code, int) else resp.status_code,
                error_message=err_data.get("message") or resp.text,
                retryable=retryable,
            )

        wamid = ((resp.json().get("messages") or [{}])[0]).get("id")
        if request.content_type != MessageContentType.TEMPLATE:
            current_ym = datetime.now(UTC).strftime("%Y-%m")
            await session.execute(
                text(
                    "UPDATE channel_accounts SET config = jsonb_set("
                    "jsonb_set(config, '{service_msg_period_ym}', to_jsonb(CAST(:ym AS text))), "
                    "'{service_msg_monthly_count}', "
                    "to_jsonb(CASE WHEN config->>'service_msg_period_ym' = :ym "
                    "THEN COALESCE((config->>'service_msg_monthly_count')::int, 0) + 1 ELSE 1 END)"
                    "), updated_at = now() WHERE tenant_id = :tid AND id = :aid"
                ),
                {"tid": tenant_id, "aid": request.channel_account_id, "ym": current_ym},
            )

        return OutboundSendResult(
            success=True,
            external_message_id=wamid,
            estimated_cost_usd=decision.estimated_cost_usd,
            pricing_category=decision.pricing_category,
        )


def _get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        engine = create_async_engine(os.environ["DATABASE_RUNTIME_URL"], future=True)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        request.app.state.session_factory = factory
    return factory


@whatsapp_router.post("/embedded-signup", response_model=EmbeddedSignupResponse)
async def complete_whatsapp_embedded_signup(
    payload: EmbeddedSignupRequest,
    request: Request,
    principal: Annotated[CurrentPrincipal, Depends(_require_channel_admin)],
) -> EmbeddedSignupResponse:
    session_factory = _get_session_factory(request)
    crypto = EnvelopeCryptoService(get_settings())
    adapter = WhatsAppAdapter()
    async with tenant_session(session_factory, principal.tenant_id) as session:
        try:
            account_id = await adapter.complete_embedded_signup(
                session=session,
                tenant_id=principal.tenant_id,
                crypto_service=crypto,
                code=payload.code,
                waba_id=payload.waba_id,
                phone_number_id=payload.phone_number_id,
                is_coexistence=payload.is_coexistence,
                display_name=payload.display_name,
            )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Meta Embedded Signup v4 failed: {exc}",
            ) from exc

        await record_audit_event(
            session=session,
            tenant_id=principal.tenant_id,
            actor_type="human_user",
            actor_id=str(principal.user_id),
            action="channel.whatsapp_connected",
            resource_type="channel_account",
            resource_id=account_id,
            after_state={
                "waba_id": payload.waba_id,
                "phone_number_id": payload.phone_number_id,
                "is_coexistence": payload.is_coexistence,
            },
        )

    ctype = (
        ChannelType.WHATSAPP_COEXISTENCE if payload.is_coexistence else ChannelType.WHATSAPP_CLOUD
    )
    return EmbeddedSignupResponse(
        channel_account_id=account_id,
        channel_type=ctype,
        mps_cap=20 if payload.is_coexistence else 80,
    )


@whatsapp_router.post("/consent/opt-in")
async def record_whatsapp_opt_in(
    payload: ConsentOptInRequest,
    request: Request,
    principal: Annotated[CurrentPrincipal, Depends(_require_consent_writer)],
) -> dict[str, str]:
    session_factory = _get_session_factory(request)
    consent = ConsentAndSuppressionService()
    async with tenant_session(session_factory, principal.tenant_id) as session:
        rec_id = await consent.record_opt_in(
            session=session,
            tenant_id=principal.tenant_id,
            customer_id=payload.customer_id,
            channel_type="whatsapp",
            proof_source=payload.proof_source,
            scope=payload.scope,
            disclosed_business_name=payload.disclosed_business_name,
            proof_ip=request.client.host if request.client else None,
            normalized_address=payload.normalized_phone,
        )
    return {"consent_record_id": str(rec_id), "status": "opted_in"}
