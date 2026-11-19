from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import re
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, ClassVar, cast

import boto3  # type: ignore[import-untyped]
import httpx
import nh3
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.channels.adapters.base import (
    MessagingChannelAdapter,
    NormalizedAttachment,
    NormalizedInboundEvent,
    NormalizedStatusEvent,
    NormalizedWebhookBatch,
    OutboundMessageRequest,
    OutboundSendResult,
)
from erp_core.channels.parsers.b2b_inquiry_email import B2BInquiryEmailParser
from erp_core.shared.enums import (
    ChannelType,
    ConversationMode,
    DeliveryStatus,
    IdentityType,
    MessageContentType,
    MessageDirection,
    MessageSenderType,
)

_SUBADDRESS_THREAD_RE = re.compile(
    r"\+th_([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})@"
)
_TRACKING_PIXEL_IMG_RE = re.compile(
    r"<img\b[^>]*(?:width\s*=\s*[\"']?[01][\"']?[^>]*height\s*=\s*[\"']?[01][\"']?|"
    r"height\s*=\s*[\"']?[01][\"']?[^>]*width\s*=\s*[\"']?[01][\"']?|"
    r"display\s*:\s*none)[^>]*>",
    re.IGNORECASE,
)
_SAFE_HTML_TAGS: set[str] = {
    "p",
    "br",
    "b",
    "strong",
    "i",
    "em",
    "u",
    "ul",
    "ol",
    "li",
    "blockquote",
    "pre",
    "code",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "h1",
    "h2",
    "h3",
    "h4",
    "a",
    "span",
    "div",
    "img",
}
_SAFE_HTML_ATTRS: dict[str, set[str]] = {
    "a": {"href", "title", "target"},
    "img": {"alt", "width", "height", "data-blocked-src"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}


class S3AttachmentStore:
    """Uploads inbound email attachments to Cloudflare R2 / MinIO S3 under tenant isolation."""

    async def upload_bytes(self, *, key: str, data: bytes, content_type: str) -> str:
        endpoint = os.environ.get("S3_ENDPOINT_URL", "http://localhost:9000")
        bucket = os.environ.get("S3_BUCKET", "erp-assets")
        access_key = os.environ.get("S3_ACCESS_KEY", "minioadmin")
        secret_key = os.environ.get("S3_SECRET_KEY", "minioadmin")
        region = os.environ.get("S3_REGION", "us-east-1")

        def _put() -> None:
            client = boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                region_name=region,
            )
            client.put_object(
                Bucket=bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
            )

        await asyncio.to_thread(_put)
        return key


def _sanitize_img_attribute(tag: str, attr: str, value: str) -> str | None:
    if tag == "img" and attr == "src":
        # Proxy/block external tracking images by default
        return None
    if tag == "a" and attr == "href" and value.strip().lower().startswith("javascript:"):
        return None
    return value


def _payload_bytes(payload: Any) -> bytes:
    """Return an email part payload as bytes (untyped email.message API)."""
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, str):
        return payload.encode("utf-8")
    return b""


class EmailAdapter(MessagingChannelAdapter):
    """Zero-CASA transactional Email adapter (Resend/Postmark/SES) with RFC 5322/5233 threading."""

    channel_types: ClassVar[tuple[ChannelType, ...]] = (
        ChannelType.EMAIL,
        ChannelType.MADE_IN_CHINA_EMAIL,
        ChannelType.ALIBABA_EMAIL,
    )

    def __init__(
        self,
        *,
        storage_store: S3AttachmentStore | None = None,
        http_transport: httpx.AsyncBaseTransport | None = None,
        resend_base_url: str = "https://api.resend.com",
    ) -> None:
        self._storage = storage_store or S3AttachmentStore()
        self._http_transport = http_transport
        self._resend_base_url = resend_base_url.rstrip("/")
        self._b2b_parser = B2BInquiryEmailParser()

    def sanitize_email_html(self, raw_html: str) -> str:
        if not raw_html:
            return ""
        without_pixels = _TRACKING_PIXEL_IMG_RE.sub("", raw_html)
        return nh3.clean(
            without_pixels,
            tags=_SAFE_HTML_TAGS,
            attributes=_SAFE_HTML_ATTRS,
            attribute_filter=_sanitize_img_attribute,
            link_rel="noopener noreferrer nofollow",
            strip_comments=True,
        )

    def verify_webhook_signature(
        self, raw_body: bytes, headers: Mapping[str, str], secret: str
    ) -> bool:
        svix_id = headers.get("svix-id")
        svix_ts = headers.get("svix-timestamp")
        svix_sig = headers.get("svix-signature")
        if svix_id and svix_ts and svix_sig:
            key_bytes = (
                base64.b64decode(secret.removeprefix("whsec_"))
                if secret.startswith("whsec_")
                else secret.encode("utf-8")
            )
            signed_payload = f"{svix_id}.{svix_ts}.".encode() + raw_body
            expected_b64 = base64.b64encode(
                hmac.new(key_bytes, signed_payload, hashlib.sha256).digest()
            ).decode("utf-8")
            for token in svix_sig.split():
                if "," in token:
                    _, sig_val = token.split(",", 1)
                    if hmac.compare_digest(sig_val, expected_b64):
                        return True
            return False

        direct_sig = headers.get("x-email-signature", headers.get("x-webhook-signature", ""))
        if direct_sig:
            raw_sig = direct_sig.removeprefix("sha256=")
            expected_hex = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
            return hmac.compare_digest(raw_sig, expected_hex)
        return False

    def extract_external_event_ids(
        self, payload: dict[str, Any], raw_body: bytes, headers: Mapping[str, str]
    ) -> list[tuple[str, str | None]]:
        svix_id = headers.get("svix-id")
        data = payload.get("data") or payload
        to_list = data.get("to") or []
        if isinstance(to_list, str):
            to_list = [to_list]
        ext_acct = None
        for addr in to_list:
            _, parsed_email = parseaddr(str(addr))
            if parsed_email and "+th_" not in parsed_email:
                ext_acct = parsed_email.lower()
                break
        if ext_acct is None and to_list:
            _, parsed_email = parseaddr(str(to_list[0]))
            if parsed_email:
                ext_acct = _SUBADDRESS_THREAD_RE.sub("@", parsed_email.lower())

        event_id = (
            svix_id
            or data.get("message_id")
            or data.get("email_id")
            or hashlib.sha256(raw_body).hexdigest()
        )
        return [(str(event_id), ext_acct)]

    async def resolve_conversation_id(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        to_addresses: list[str],
        in_reply_to: str | None,
        references: list[str],
    ) -> uuid.UUID | None:
        """Resolve thread strictly via RFC 5233 +th_<uuid>, In-Reply-To, or 
        References (never Subject)."""
        for addr in to_addresses:
            match = _SUBADDRESS_THREAD_RE.search(addr)
            if match:
                candidate_id = uuid.UUID(match.group(1))
                exists = (
                    await session.execute(
                        text(
                            "SELECT id FROM conversations WHERE tenant_id = :tid AND id = :cid "
                            "LIMIT 1"
                        ),
                        {"tid": tenant_id, "cid": candidate_id},
                    )
                ).scalar_one_or_none()
                if exists is not None:
                    return cast(uuid.UUID | None, exists)

        candidate_msg_ids: list[str] = []
        if in_reply_to:
            candidate_msg_ids.append(in_reply_to.strip())
        for ref in reversed(references):
            cleaned = ref.strip()
            if cleaned and cleaned not in candidate_msg_ids:
                candidate_msg_ids.append(cleaned)

        for ref_msg_id in candidate_msg_ids:
            conv_id = (
                await session.execute(
                    text(
                        "SELECT conversation_id FROM messages "
                        "WHERE tenant_id = :tid AND external_message_id = :emid LIMIT 1"
                    ),
                    {"tid": tenant_id, "emid": ref_msg_id},
                )
            ).scalar_one_or_none()
            if conv_id is not None:
                return cast(uuid.UUID | None, conv_id)
        return None

    async def _upload_attachments(
        self, tenant_id: uuid.UUID, raw_attachments: list[dict[str, Any]]
    ) -> list[NormalizedAttachment]:
        stored: list[NormalizedAttachment] = []
        for item in raw_attachments:
            filename = str(item.get("filename") or "attachment.bin").replace("/", "_")
            mime = str(item.get("content_type") or "application/octet-stream")
            raw_b64 = item.get("content_base64") or item.get("content")
            raw_bytes = base64.b64decode(raw_b64) if raw_b64 else b""
            r2_key = f"tenants/{tenant_id}/channels/email/attachments/{uuid.uuid4()}/{filename}"
            if raw_bytes:
                await self._storage.upload_bytes(key=r2_key, data=raw_bytes, content_type=mime)
            stored.append(
                NormalizedAttachment(
                    filename=filename,
                    mime_type=mime,
                    size_bytes=len(raw_bytes),
                    r2_key=r2_key,
                )
            )
        return stored

    async def parse_webhook(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        channel_account_id: uuid.UUID,
        payload: dict[str, Any],
        headers: Mapping[str, str],
    ) -> NormalizedWebhookBatch:
        event_type = str(payload.get("type") or "email.received")
        data = payload.get("data") or payload

        if event_type in ("email.sent", "email.delivered", "email.bounced", "email.complained"):
            status_map = {
                "email.sent": DeliveryStatus.SENT,
                "email.delivered": DeliveryStatus.DELIVERED,
                "email.bounced": DeliveryStatus.FAILED,
                "email.complained": DeliveryStatus.FAILED,
            }
            msg_id_ref = str(data.get("message_id") or data.get("email_id") or "")
            return NormalizedWebhookBatch(
                status_events=[
                    NormalizedStatusEvent(
                        external_message_id=msg_id_ref,
                        external_account_id="",
                        delivery_status=status_map[event_type],
                        timestamp=datetime.now(UTC),
                    )
                ]
            )

        if data.get("raw_mime_base64"):
            mime_bytes = base64.b64decode(data["raw_mime_base64"])
            parsed_msg = BytesParser(policy=policy.default).parsebytes(mime_bytes)
            from_header = str(parsed_msg.get("From", ""))
            to_headers = [str(v) for v in parsed_msg.get_all("To", [])]
            reply_to_list = (
                [str(parsed_msg.get("Reply-To", ""))] if parsed_msg.get("Reply-To") else []
            )
            subject = str(parsed_msg.get("Subject", ""))
            message_id = str(parsed_msg.get("Message-ID", f"<{uuid.uuid4()}@inbound>"))
            in_reply_to = parsed_msg.get("In-Reply-To")
            references_raw = str(parsed_msg.get("References", ""))
            text_part = parsed_msg.get_body(preferencelist=("plain",))
            html_part = parsed_msg.get_body(preferencelist=("html",))
            body_text = text_part.get_content() if text_part else ""
            raw_html = html_part.get_content() if html_part else ""
            raw_attachments = []
            for part in parsed_msg.iter_attachments():
                raw_attachments.append(
                    {
                        "filename": part.get_filename() or "attachment.bin",
                        "content_type": part.get_content_type(),
                        "content_base64": base64.b64encode(
                            _payload_bytes(part.get_payload(decode=True))
                        ).decode(),
                    }
                )
            date_hdr = parsed_msg.get("Date")
            source_ts = parsedate_to_datetime(date_hdr) if date_hdr else datetime.now(UTC)
        else:
            from_header = str(data.get("from", ""))
            to_raw = data.get("to") or []
            to_headers = [to_raw] if isinstance(to_raw, str) else [str(x) for x in to_raw]
            reply_raw = data.get("reply_to") or []
            reply_to_list = (
                [reply_raw] if isinstance(reply_raw, str) else [str(x) for x in reply_raw]
            )
            subject = str(data.get("subject", ""))
            hdrs = {str(k).lower(): str(v) for k, v in (data.get("headers") or {}).items()}
            message_id = str(
                data.get("message_id")
                or hdrs.get("message-id")
                or f"<{data.get('email_id', uuid.uuid4())}@inbound>"
            )
            in_reply_to = data.get("in_reply_to") or hdrs.get("in-reply-to")
            references_raw = str(data.get("references") or hdrs.get("references") or "")
            body_text = str(data.get("text") or "")
            raw_html = str(data.get("html") or "")
            raw_attachments = list(data.get("attachments") or [])
            created_at_str = data.get("created_at")
            source_ts = (
                datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                if created_at_str
                else datetime.now(UTC)
            )

        sender_name, sender_email = parseaddr(from_header)
        sender_email = sender_email.strip().lower()
        _, reply_to_email = parseaddr(reply_to_list[0]) if reply_to_list else ("", "")

        b2b_inquiry = self._b2b_parser.parse(
            sender_email=sender_email,
            subject=subject,
            body_text=body_text,
            reply_to=reply_to_email or None,
        )

        effective_email = (
            b2b_inquiry.buyer_email if (b2b_inquiry and b2b_inquiry.buyer_email) else sender_email
        )
        effective_name = (
            b2b_inquiry.buyer_name
            if (b2b_inquiry and b2b_inquiry.buyer_name)
            else (sender_name or effective_email)
        )
        country_hint = b2b_inquiry.country if b2b_inquiry else None
        if country_hint and len(country_hint) > 2:
            country_map = {"germany": "DE", "sweden": "SE", "united states": "US", "uk": "GB"}
            country_hint = country_map.get(country_hint.lower(), "US")

        references = [r for r in references_raw.split() if r.strip()]
        resolved_conv_id = await self.resolve_conversation_id(
            session=session,
            tenant_id=tenant_id,
            to_addresses=to_headers,
            in_reply_to=in_reply_to,
            references=references,
        )

        attachments = await self._upload_attachments(tenant_id, raw_attachments)
        sanitized_html = self.sanitize_email_html(raw_html) if raw_html else None

        ext_account = ""
        for addr in to_headers:
            _, parsed_to = parseaddr(addr)
            if parsed_to:
                ext_account = parsed_to.lower()
                break

        inbound_event = NormalizedInboundEvent(
            external_event_id=message_id,
            external_account_id=ext_account,
            external_message_id=message_id,
            external_thread_id=str(resolved_conv_id) if resolved_conv_id else message_id,
            subject=subject,
            sender_identity_type=IdentityType.EMAIL,
            sender_identity_value=effective_email,
            sender_display_name=effective_name,
            country_hint=country_hint,
            direction=MessageDirection.INBOUND,
            sender_type=MessageSenderType.CUSTOMER,
            content_type=MessageContentType.RICH_HTML
            if sanitized_html
            else MessageContentType.TEXT,
            body_text=body_text,
            body_sanitized_html=sanitized_html,
            attachments=attachments,
            source_timestamp=source_ts,
            conversation_mode=(
                ConversationMode.B2B_INQUIRY if b2b_inquiry else ConversationMode.B2C_SUPPORT
            ),
            channel_metadata={
                "rfc5322": {
                    "message_id": message_id,
                    "in_reply_to": in_reply_to,
                    "references": references,
                    "subject": subject,
                },
                "b2b_inquiry": b2b_inquiry.model_dump(mode="json") if b2b_inquiry else None,
            },
            b2b_inquiry_data=b2b_inquiry.model_dump(mode="json") if b2b_inquiry else None,
            resolved_conversation_id=resolved_conv_id,
            override_channel_type=b2b_inquiry.channel_type if b2b_inquiry else None,
        )
        return NormalizedWebhookBatch(inbound_events=[inbound_event])

    async def send_message(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        credentials: dict[str, Any],
        request: OutboundMessageRequest,
    ) -> OutboundSendResult:
        api_key = str(credentials.get("api_key") or os.environ.get("RESEND_API_KEY", ""))
        inbound_domain = os.environ.get("INBOUND_EMAIL_DOMAIN", "inbound.mail.example.com")
        generated_message_id = f"<{uuid.uuid4()}@{inbound_domain}>"
        if "@" in request.external_account_id:
            acct_local, acct_domain = request.external_account_id.split("@", 1)
            default_reply_to = f"{acct_local}+th_{request.conversation_id}@{acct_domain}"
        else:
            default_reply_to = f"support+th_{request.conversation_id}@{inbound_domain}"
        reply_to = request.reply_to_address or default_reply_to

        headers_dict: dict[str, str] = {"Message-ID": generated_message_id}
        if request.in_reply_to:
            headers_dict["In-Reply-To"] = request.in_reply_to
        if request.references:
            headers_dict["References"] = " ".join(request.references)

        payload = {
            "from": request.from_address or f"Support <support@{inbound_domain}>",
            "to": [request.recipient_id],
            "reply_to": reply_to,
            "subject": request.subject or "Re: Your Inquiry",
            "text": request.body_text or "",
            "html": request.body_html,
            "headers": headers_dict,
        }
        async with httpx.AsyncClient(timeout=15.0, transport=self._http_transport) as client:
            resp = await client.post(
                f"{self._resend_base_url}/emails",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
            )

        if resp.status_code >= 400:
            return OutboundSendResult(
                success=False,
                error_code=resp.status_code,
                error_message=resp.text,
                retryable=resp.status_code >= 500 or resp.status_code == 429,
            )
        return OutboundSendResult(
            success=True,
            external_message_id=generated_message_id,
        )
